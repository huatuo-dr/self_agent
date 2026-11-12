"""Host-side business operations scoped to one owner-authorized model task.

Only the copied client, Unix socket and sanitized outputs enter the sandbox.
The trusted scope is supplied by the service, never by a model tool request.
"""
import asyncio
from contextlib import asynccontextmanager, contextmanager
import copy
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import shlex
import stat
import tempfile
from urllib.parse import parse_qsl, quote, unquote, urlsplit

SOCKET_NAME = 'agent-tools.sock'
MAX_REQUEST = 65536
MAX_RESPONSE = 2_000_000
MAX_FILE = 50 * 1024 * 1024
RESERVED_PROJECT_DIRS = frozenset(('credential', 'pic', 'reports', 'files', 'skills'))
_REPOSITORY_LOCKS = {}
_UNSET = object()


class Denied(ValueError):
    pass


def _object(value, keys):
    if not isinstance(value, dict) or set(value) - set(keys):
        raise Denied('invalid_request')


def _plain(value, limit=2048):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and not any(ord(char) < 32 for char in value))


def _no_links(path):
    path = Path(os.path.abspath(path))
    return not any(part.is_symlink() for part in (path, *path.parents))


@contextmanager
def _open_directory(path, create=False):
    """Anchor each component with O_NOFOLLOW before touching a child."""
    absolute = Path(os.path.abspath(path))
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in absolute.parts[1:]:
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=directory)
                except FileExistsError:
                    pass
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        yield directory
    finally:
        os.close(directory)


def _open_regular(path):
    """Open an unaliased regular file through anchored directory descriptors."""
    absolute = Path(os.path.abspath(path))
    with _open_directory(absolute.parent) as directory:
        file = os.open(absolute.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                       dir_fd=directory)
        info = os.fstat(file)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE:
            os.close(file)
            raise Denied('invalid_file')
        return os.fdopen(file, 'rb')


def _atomic_copy(source, target, *, exclusive=False, expected=None, expected_target=_UNSET):
    """Never truncate a destination inode, including raced hard/symbolic links."""
    target = Path(target)
    with _open_regular(source) as src, _open_directory(target.parent, create=True) as directory:
        source_info = os.fstat(src.fileno())
        try:
            existing = os.stat(target.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if exclusive or not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
                raise Denied('unsafe_repository_change')
        if expected_target is not _UNSET:
            if expected_target is None:
                if existing is not None:
                    raise Denied('repository_changed_concurrently')
            else:
                with _open_regular(target) as original:
                    before = os.fstat(original.fileno())
                    actual_target = (hashlib.sha256(original.read(MAX_FILE + 1)).hexdigest(),
                                     bool(before.st_mode & 0o111))
                if actual_target != expected_target or existing is None or (before.st_dev, before.st_ino) != (existing.st_dev, existing.st_ino):
                    raise Denied('repository_changed_concurrently')
        temporary = '.agent-copy-' + secrets.token_hex(16)
        file = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                       0o600, dir_fd=directory)
        try:
            digest, size = hashlib.sha256(), 0
            with os.fdopen(file, 'wb') as dst:
                for chunk in iter(lambda: src.read(65536), b''):
                    size += len(chunk)
                    if size > MAX_FILE:
                        raise Denied('invalid_file')
                    digest.update(chunk)
                    dst.write(chunk)
                os.fchmod(dst.fileno(), 0o755 if source_info.st_mode & 0o111 else 0o644)
            actual = (digest.hexdigest(), bool(source_info.st_mode & 0o111))
            if expected is not None and actual != expected:
                raise Denied('repository_changed_concurrently')
            # Revalidate the inode immediately before publication; replace itself
            # never follows a target link even if another actor wins the next race.
            try:
                present = os.stat(target.name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                present = None
            if (present is None) != (existing is None) or (present is not None and
                    (present.st_dev, present.st_ino, present.st_nlink, present.st_mode,
                     present.st_size, present.st_mtime_ns) !=
                    (existing.st_dev, existing.st_ino, existing.st_nlink, existing.st_mode,
                     existing.st_size, existing.st_mtime_ns)):
                raise Denied('repository_changed_concurrently')
            if exclusive:
                os.link(temporary, target.name, src_dir_fd=directory,
                        dst_dir_fd=directory, follow_symlinks=False)
            else:
                os.replace(temporary, target.name, src_dir_fd=directory, dst_dir_fd=directory)
            return actual
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def _safe_unlink(path, expected):
    with _open_directory(Path(path).parent) as directory:
        info = os.stat(Path(path).name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Denied('unsafe_repository_change')
        with _open_regular(path) as source:
            actual = (hashlib.sha256(source.read(MAX_FILE + 1)).hexdigest(),
                      bool(os.fstat(source.fileno()).st_mode & 0o111))
        present = os.stat(Path(path).name, dir_fd=directory, follow_symlinks=False)
        if actual != expected or present != info:
            raise Denied('repository_changed_concurrently')
        os.unlink(Path(path).name, dir_fd=directory)


def _safe_name(name):
    return (_plain(name, 255) and not name.lower().startswith('.env') and name.lower() != '.git' and name.lower() not in {
        '.local', 'credential', 'credentials', '.ssh', '.aws', '.config', '.npmrc',
        '.pypirc', '.netrc', 'node_modules', '__pycache__', '.venv', 'venv',
        '.project', '.codex', '.agents', '.agent', '.cache', '.npm', '.docker',
        '.kube', '.gnupg', 'runtime', 'secrets', 'owner-profile.json'}
        and not name.lower().endswith(('.pem', '.key', '.p12', '.pfx')))


class TaskTools:
    def __init__(self, cfg, root, scope, run, work, sandbox, db=None, input_files=()):
        self.cfg, self.root, self.scope = dict(cfg), Path(root).resolve(), copy.deepcopy(scope)
        self.run, self.work, self.sandbox, self.db = run, Path(work).resolve(), sandbox, db
        self.inputs = {Path(p).resolve() for p in input_files if _no_links(Path(p))}
        self.prepared = {}
        self.repository_info = {}
        self.repository_origins = {}
        self.preparation_requests = {}
        self.task_id = secrets.token_hex(6)
        self.baselines = {}
        self.delivered_files = set()
        self.lock = asyncio.Lock()

    def target(self, params):
        host, project = params.get('host'), params.get('project')
        if not _plain(host, 253) or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', host):
            raise Denied('invalid_target')
        if not _plain(project) or not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_-][A-Za-z0-9_.-]*)+', project):
            raise Denied('invalid_target')
        if any(part in ('.', '..') for part in project.split('/')):
            raise Denied('invalid_target')
        if {'host': host, 'project': project} not in self.scope.get('projects', []):
            raise Denied('target_not_authorized')
        if not self.cfg.get('GITLAB_SKILL_ENABLED') or self.scope.get('gitlab_mode') not in ('read', 'write'):
            raise Denied('gitlab_unavailable')
        return host, project

    def read_host(self, host):
        if not _plain(host, 253) or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', host):
            raise Denied('invalid_target')
        if not self.cfg.get('GITLAB_SKILL_ENABLED') or self.scope.get('gitlab_mode') != 'read':
            raise Denied('global_read_not_authorized')
        hosts = {item.get('host') for item in self.scope.get('projects', []) if isinstance(item, dict)}
        hosts.update(self.scope.get('hosts') or [])
        hosts.update(filter(None, (self.scope.get('host'), self.cfg.get('GITLAB_HOST'))))
        if host not in hosts:
            raise Denied('target_not_authorized')
        return host

    @staticmethod
    def scrub(value):
        if isinstance(value, dict):
            return {key: TaskTools.scrub(item) for key, item in value.items()
                    if not re.search(r'token|password|secret|credential|private.?key|access.?key|authorization|cookie',
                                     str(key), re.IGNORECASE)}
        if isinstance(value, list):
            return [TaskTools.scrub(item) for item in value]
        if isinstance(value, str):
            return re.sub(r'((?:https?|ssh)://)[^/\s?#]*@', r'\1', value)
        return value

    async def dispatch(self, operation, params):
        try:
            if not isinstance(params, dict):
                raise Denied('invalid_request')
            # Serialize task operations and SQLite access. No background DB thread.
            async with self.lock:
                handlers = {'history_search': self.history, 'images_search': self.images,
                            'feishu_read': self.feishu, 'contact_search': self.contact_search,
                            'gitlab_cli': self.gitlab,
                            'repository_prepare': self.prepare, 'repository_git': self.repository_git,
                            'send_file': self.send_file}
                if operation not in handlers:
                    raise Denied('unknown_operation')
                result = await handlers[operation](params)
            if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_RESPONSE - 1:
                raise Denied('response_too_large')
            return result
        except Denied as error:
            return {'ok': False, 'error': {'type': str(error)}}
        except (Exception,):
            # CLI stderr, filesystem paths, raw credential/auth errors stay host-side.
            return {'ok': False, 'error': {'type': 'operation_failed'}}

    async def command(self, args, cwd=None, timeout=60):
        result = await self.run(args, timeout, cwd=cwd or self.root)
        if not isinstance(result, str) or len(result.encode()) > MAX_RESPONSE - 1024:
            raise Denied('response_too_large')
        return result

    async def history(self, params):
        _object(params, {'query', 'page'})
        if self.db is None:
            raise Denied('history_unavailable')
        from assistant.history_search import search
        return {'ok': True, 'data': search(self.db, self.cfg.get('FEISHU_OWNER_CHAT_ID'),
                                          params.get('query'), params.get('page', 1))}

    async def images(self, params):
        _object(params, {'session_id', 'message_id', 'limit', 'copy'})
        if self.db is None:
            raise Denied('images_unavailable')
        from assistant.image_archive import search, format_of
        items = search(self.db, self.cfg.get('FEISHU_OWNER_CHAT_ID'), self.root,
                       session_id=params.get('session_id'), message_id=params.get('message_id'),
                       limit=params.get('limit', 10), cfg=self.cfg)
        output = []
        for item in items:
            item = dict(item)
            source = Path(item.pop('path'))
            if params.get('copy', True):
                if not _no_links(source) or source.parent != self.root / '.project/pic':
                    raise Denied('unsafe_image')
                format_of(source)
                target = self.work / 'images' / source.name
                while target.exists():
                    target = target.with_name('copy-' + target.name)
                if not _no_links(target):
                    raise Denied('unsafe_image')
                target.parent.mkdir(exist_ok=True)
                _atomic_copy(source, target, exclusive=True)
                item['path'] = str(self.sandbox.map_path(target))
            output.append(item)
        return {'ok': True, 'items': output}

    async def feishu(self, params):
        from assistant.feishu_im_read import execute
        return await asyncio.to_thread(execute, params, self.cfg['LARK_CLI_BIN'],
                                       self.cfg.get('FEISHU_OWNER_OPEN_ID'), self.cfg)

    async def contact_search(self, params):
        _object(params, {'query'})
        if not self.cfg.get('GITLAB_SKILL_ENABLED') or self.scope.get('gitlab_mode') != 'write':
            raise Denied('contact_lookup_not_authorized')
        query = params.get('query')
        if not _plain(query, 50) or not query.strip():
            raise Denied('invalid_query')
        status = json.loads(await self.command([self.cfg['LARK_CLI_BIN'], 'auth', 'status', '--json'], timeout=15))
        identity = (status.get('identities') or {}).get('user') or {}
        if not self.cfg.get('FEISHU_OWNER_OPEN_ID') or identity.get('openId') != self.cfg['FEISHU_OWNER_OPEN_ID']:
            raise Denied('owner_identity_mismatch')
        scopes = identity.get('scope') or []
        if isinstance(scopes, str):
            scopes = scopes.split()
        if 'contact:user:search' not in scopes:
            raise Denied('missing_contact_scope')
        payload = json.loads(await self.command([self.cfg['LARK_CLI_BIN'], 'contact', '+search-user',
            '--as', 'user', '--query', query.strip(), '--exclude-external-users',
            '--page-size', '20', '--format', 'json'], timeout=30))
        if payload.get('ok') is not True:
            raise Denied('contact_lookup_unconfirmed')
        data = payload.get('data') or {}
        users = data.get('users')
        if not isinstance(users, list):
            raise Denied('invalid_contact_response')
        fields = {'open_id', 'localized_name', 'email', 'enterprise_email', 'department', 'is_cross_tenant'}
        candidates = [{key: value for key, value in item.items() if key in fields}
                      for item in users[:20] if isinstance(item, dict) and not item.get('is_cross_tenant')
                      and isinstance(item.get('open_id'), str)
                      and re.fullmatch(r'ou_[A-Za-z0-9_]+', item['open_id'])]
        return {'ok': True, 'data': {'users': self.scrub(candidates),
                                    'has_more': bool(data.get('has_more')) or len(users) > 20}}

    def gitlab_args(self, params):
        _object(params, {'host', 'project', 'args'})
        args = params.get('args')
        if (not isinstance(args, list) or not 1 <= len(args) <= 80 or
                any(not _plain(value, 16000) for value in args)):
            raise Denied('invalid_arguments')
        user_lookup = (args[0] == 'api' and len(args) > 1 and
                       re.fullmatch(r'users(?:/[1-9][0-9]*)?', args[1].lstrip('/').split('?', 1)[0]))
        if user_lookup:
            if params.get('project'):
                host, _ = self.target(params)
            elif self.scope.get('gitlab_mode') == 'write':
                project = next((item.get('project') for item in self.scope.get('projects', [])
                                if item.get('host') == params.get('host')), None)
                host, _ = self.target({'host': params.get('host'), 'project': project})
            else:
                host = self.read_host(params.get('host'))
            return self.api_args(host, None, args[1:], users_only=True)
        if args[0] == 'api' and not params.get('project'):
            host = self.read_host(params.get('host'))
            return self.api_args(host, None, args[1:])
        host, project = self.target(params)
        if args in (['--help'], ['help']):
            return [self.cfg['GLAB_BIN'], '--help']
        if args[0] == 'api':
            return self.api_args(host, project, args[1:])
        normalized = self.ci_api(project, args)
        if normalized is not None:
            return self.api_args(host, project, normalized)
        reads = {'mr': {'list', 'view', 'diff'}, 'issue': {'list', 'view'},
                 'ci': {'list', 'view', 'status'}, 'job': {'list', 'view', 'trace'}}
        writes = {'mr': {'create', 'update', 'close', 'reopen', 'note', 'approve', 'merge'},
                  'issue': {'create', 'update', 'close', 'reopen', 'note'},
                  'ci': {'retry', 'cancel', 'run'}, 'job': {'retry', 'cancel', 'play'}}
        if len(args) < 2 or args[0] not in reads:
            raise Denied('unsupported_gitlab_command')
        family, verb = args[:2]
        write = verb in writes.get(family, set())
        if verb not in reads[family] and not write:
            raise Denied('unsupported_gitlab_command')
        if write and self.scope.get('gitlab_mode') != 'write':
            raise Denied('write_not_authorized')
        values = {'--output', '--page', '--per-page', '--search', '--state', '--label',
                  '--assignee', '--author', '--milestone', '--sort', '--order', '--target-branch',
                  '--source-branch', '--title', '--description', '--body', '--message',
                  '--reviewer', '--due-date', '--pipeline-id', '--branch'}
        switches = {'--all', '--closed', '--opened', '--merged', '--draft', '--confidential',
                    '--remove-source-branch', '--squash', '--yes', '--raw', '--no-color'}
        if not write:
            values -= {'--title', '--description', '--body', '--message', '--reviewer', '--due-date'}
            switches -= {'--remove-source-branch', '--squash', '--yes', '--confidential'}
        clean, position = [], 2
        while position < len(args):
            value = args[position]
            if value in values:
                if position + 1 >= len(args):
                    raise Denied('invalid_arguments')
                argument = args[position + 1]
                if argument.startswith(('-', '@')):
                    raise Denied('invalid_arguments')
                if value in {'--page', '--per-page', '--pipeline-id'} and not re.fullmatch(r'[1-9][0-9]{0,5}', argument):
                    raise Denied('invalid_arguments')
                if value == '--per-page' and int(argument) > 100:
                    raise Denied('invalid_arguments')
                if value == '--output' and argument != 'json':
                    raise Denied('invalid_arguments')
                clean += [value, argument]
                position += 2
            elif value in switches or re.fullmatch(r'[1-9][0-9]{0,10}', value):
                clean.append(value)
                position += 1
            else:
                raise Denied('unsupported_gitlab_argument')
        if verb == 'create' and not {'--title', '--description'}.issubset(clean):
            raise Denied('noninteractive_fields_required')
        if verb == 'note' and not {'--message', '--body'}.intersection(clean):
            raise Denied('noninteractive_fields_required')
        if verb == 'update' and not values.intersection(clean):
            raise Denied('noninteractive_fields_required')
        return [self.cfg['GLAB_BIN'], family, verb, *clean, '--repo', f'{host}/{project}']

    def ci_api(self, project, args):
        """Stable project APIs avoid glab versions that print job help with exit 0."""
        if len(args) < 2 or args[0] not in ('job', 'ci'):
            return None
        family, verb = args[:2]
        base = 'projects/' + quote(project, safe='')
        if family == 'job':
            if verb == 'list' and len(args) == 2:
                return [base + '/jobs']
            if verb not in ('view', 'trace', 'retry', 'cancel', 'play') or len(args) < 3 or not re.fullmatch(r'[1-9][0-9]{0,10}', args[2]):
                raise Denied('unsupported_job_command')
            if args[3:] not in ([], ['--output', 'json']):
                raise Denied('unsupported_job_argument')
            endpoint = base + '/jobs/' + args[2] + ('' if verb == 'view' else '/' + verb)
            return [endpoint, *(['--method', 'POST'] if verb in ('retry', 'cancel', 'play') else [])]
        if verb == 'cancel':
            if len(args) != 4 or args[2] not in ('pipeline', 'job') or not re.fullmatch(r'[1-9][0-9]{0,10}', args[3]):
                raise Denied('cancel_requires_explicit_pipeline_or_job')
            return [base + '/' + args[2] + 's/' + args[3] + '/cancel', '--method', 'POST']
        if verb == 'retry':
            if len(args) != 3 or not re.fullmatch(r'[1-9][0-9]{0,10}', args[2]):
                raise Denied('retry_requires_job_id')
            return [base + '/jobs/' + args[2] + '/retry', '--method', 'POST']
        if verb == 'run':
            if len(args) != 4 or args[2] not in ('--branch', '-b') or not self.valid_ref(args[3]):
                raise Denied('run_requires_explicit_branch')
            return [base + '/pipeline', '--method', 'POST', '--raw-field', 'ref=' + args[3]]
        if verb in ('get', 'view'):
            rest = list(args[2:])
            if rest[-2:] == ['--output', 'json']:
                rest = rest[:-2]
            if len(rest) == 1 and re.fullmatch(r'[1-9][0-9]{0,10}', rest[0]):
                return [base + '/pipelines/' + rest[0]]
            if len(rest) == 2 and rest[0] in ('--pipeline-id', '-p') and re.fullmatch(r'[1-9][0-9]{0,10}', rest[1]):
                return [base + '/pipelines/' + rest[1]]
            if len(rest) == 2 and rest[0] in ('--branch', '-b') and self.valid_ref(rest[1]):
                return [base + '/pipelines', '--raw-field', 'ref=' + rest[1]]
            if len(rest) == 1 and self.valid_ref(rest[0]):
                return [base + '/pipelines', '--raw-field', 'ref=' + rest[0]]
            if not rest:
                return [base + '/pipelines']
            raise Denied('unsupported_pipeline_argument')
        return None

    def api_args(self, host, project, args, users_only=False):
        if not args:
            raise Denied('invalid_endpoint')
        endpoint = args[0].lstrip('/')
        if '#' in endpoint or len(endpoint) > 4096:
            raise Denied('invalid_endpoint')
        query_fields = []
        if '?' in endpoint:
            endpoint, query = endpoint.split('?', 1)
            try:
                query_fields = [part for key, value in parse_qsl(query, strict_parsing=True, max_num_fields=30)
                                for part in ('--raw-field', key + '=' + value)]
            except ValueError:
                raise Denied('invalid_endpoint')
        args = [args[0], *query_fields, *args[1:]]
        segments = endpoint.split('/')
        global_read = project is None
        if global_read:
            allowed = (bool(re.fullmatch(r'users(?:/[1-9][0-9]*)?', endpoint)) if users_only
                       else endpoint in {'user', 'projects', 'merge_requests', 'issues'})
            metadata = (len(segments) == 2 and segments[0] == 'projects' and
                        (re.fullmatch(r'[1-9][0-9]*', segments[1]) or
                         re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+', unquote(segments[1]))))
            if users_only:
                metadata = False
            if metadata and any(part in ('.', '..') for part in unquote(segments[1]).split('/')):
                raise Denied('invalid_endpoint')
            if not allowed and not metadata:
                raise Denied('invalid_endpoint')
        if not global_read and (len(segments) < 2 or segments[0] != 'projects' or unquote(segments[1]) != project):
            raise Denied('invalid_endpoint')
        suffix = [] if global_read else segments[2:]
        member_route = bool(suffix and suffix[0] == 'members')
        if member_route and self.scope.get('gitlab_mode') != 'write':
            raise Denied('membership_scope_not_authorized')
        member_fields = {}
        # No secrets, arbitrary GraphQL, artifact downloads or local file output.
        pattern = r'(?:issues|merge_requests)(?:/[1-9][0-9]*(?:/(?:notes|discussions)(?:/[1-9][0-9]*)?|/changes|/commits|/pipelines)?)?|pipeline|pipelines(?:/[1-9][0-9]*(?:/jobs|/retry|/cancel)?)?|jobs(?:/[1-9][0-9]*(?:/trace|/retry|/cancel|/play)?)?|repository/(?:commits(?:/[0-9a-f]{40,64}(?:/diff|/statuses)?)?|branches/[A-Za-z0-9_.%~-]+)|members(?:/all)?(?:/[1-9][0-9]*)?|pages'
        if suffix and not re.fullmatch(pattern, '/'.join(suffix)):
            raise Denied('invalid_endpoint')
        if len(suffix) == 3 and suffix[:2] == ['repository', 'branches']:
            branch = unquote(suffix[2])
            if not self.valid_branch(branch) or '%' in branch:
                raise Denied('invalid_endpoint')
        method, clean, index = 'GET', [], 1
        while index < len(args):
            flag = args[index]
            if flag in ('--method', '-X', '--field', '-f', '--raw-field', '-F') and index + 1 < len(args):
                value = args[index + 1]
                if flag in ('--method', '-X'):
                    if value not in ('GET', 'POST', 'PUT', 'DELETE'):
                        raise Denied('invalid_method')
                    method = value
                    clean += ['--method', value]
                else:
                    if '=' not in value or value.split('=', 1)[1].startswith('@'):
                        raise Denied('invalid_field')
                    key, field = value.split('=', 1)
                    allowed = {'page', 'per_page', 'state', 'search', 'labels', 'author_username',
                               'title', 'description', 'body', 'state_event', 'target_branch', 'source_branch',
                               'membership', 'owned', 'scope', 'assignee_id', 'author_id', 'reviewer_id',
                               'updated_after', 'created_after', 'simple', 'order_by', 'sort',
                               'ref', 'ref_name', 'status', 'scope[]', 'username',
                               'user_id', 'access_level', 'expires_at'}
                    if member_route:
                        if key not in {'page', 'per_page', 'user_id', 'access_level', 'expires_at'} or key in member_fields:
                            raise Denied('invalid_membership_field')
                        member_fields[key] = field
                        if key == 'user_id' and not re.fullmatch(r'[1-9][0-9]{0,18}', field):
                            raise Denied('invalid_member_id')
                        if key == 'access_level' and field not in {'0', '5', '10', '15', '20', '30', '40', '50'}:
                            raise Denied('invalid_access_level')
                        if key == 'expires_at' and field:
                            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', field):
                                raise Denied('invalid_expiration_date')
                            try:
                                date.fromisoformat(field)
                            except ValueError:
                                raise Denied('invalid_expiration_date')
                    elif key in {'user_id', 'access_level', 'expires_at'}:
                        raise Denied('unsupported_api_field')
                    if users_only and key not in {'search', 'username', 'page', 'per_page'}:
                        raise Denied('unsupported_user_lookup_field')
                    if key not in allowed or (not field and not (member_route and key == 'expires_at')) or field.startswith('-'):
                        raise Denied('invalid_field')
                    if key in ('page', 'per_page') and (not field.isdigit() or not 1 <= int(field) <= 100):
                        raise Denied('invalid_field')
                    clean += ['--raw-field', value]
                index += 2
            elif flag == '--paginate':
                # One bounded page per request; caller can specify page.
                index += 1
            else:
                raise Denied('unsupported_api_argument')
        if global_read and method != 'GET':
            raise Denied('global_write_not_authorized')
        if method != 'GET' and self.scope.get('gitlab_mode') != 'write':
            raise Denied('write_not_authorized')
        if member_route:
            action = '/'.join(suffix)
            if method != 'GET' and set(member_fields) - {'user_id', 'access_level', 'expires_at'}:
                raise Denied('invalid_membership_write_field')
            if method == 'GET':
                if set(member_fields) - {'page', 'per_page'}:
                    raise Denied('unsupported_membership_read_field')
            elif method == 'POST':
                if action != 'members' or not {'user_id', 'access_level'}.issubset(member_fields):
                    raise Denied('invalid_membership_write')
            elif method in ('PUT', 'DELETE'):
                if not re.fullmatch(r'members/[1-9][0-9]*', action) or 'user_id' in member_fields:
                    raise Denied('invalid_membership_write')
                if method == 'DELETE' and member_fields:
                    raise Denied('invalid_membership_write')
                if method == 'PUT' and not set(member_fields).intersection({'access_level', 'expires_at'}):
                    raise Denied('invalid_membership_write')
        if method != 'GET' and not member_route and (not suffix or suffix[0] not in ('issues', 'merge_requests')):
            action = '/'.join(suffix)
            if method != 'POST' or not re.fullmatch(r'pipeline|pipelines/[1-9][0-9]*/(?:retry|cancel)|jobs/[1-9][0-9]*/(?:retry|cancel|play)', action):
                raise Denied('unsupported_write_endpoint')
        if method == 'GET' and any(value.split('=', 1)[0] in {'title', 'description', 'body', 'state_event'} for value in clean):
            raise Denied('write_field_requires_explicit_method')
        if method == 'GET' and not any(value.startswith('per_page=') for value in clean):
            clean += ['--raw-field', 'per_page=20']
        return [self.cfg['GLAB_BIN'], 'api', endpoint, '--hostname', host, '--method', method, *clean]

    async def gitlab(self, params):
        if params.get('args') == ['auth', 'status']:
            _object(params, {'host', 'project', 'args'})
            host, project = self.target(params)
            return {'ok': True, 'data': {'host': host, 'project': project,
                    'authentication': 'managed_by_host_service'}}
        output = await self.command(self.gitlab_args(params))
        try:
            data = json.loads(output)
        except ValueError:
            data = output
        return {'ok': True, 'data': self.scrub(data)}

    async def git(self, repo, args, timeout=60, expected_identity=None):
        base = ['git', '--no-pager', '--literal-pathspecs', '-c', 'core.hooksPath=/dev/null',
            '-c', 'core.fsmonitor=false', '-c', 'diff.external=', '-c', 'credential.helper=',
            '-c', 'core.sshCommand=ssh -oBatchMode=yes', '-c', 'protocol.allow=never',
            '-c', 'protocol.ssh.allow=always', '-c', 'protocol.file.allow=never',
            '-c', 'submodule.recurse=false', '-c', 'remote.origin.receivepack=git-receive-pack',
            '-c', 'remote.origin.uploadpack=git-upload-pack']
        # Read configuration in the actual repository: includeIf gitdir rules
        # may differ between .project and the freshly cloned destination.
        config = await self.command([*base, 'config', '--list'], cwd=repo)
        filters = set()
        for line in config.splitlines():
            match = re.match(r'filter\.([A-Za-z0-9_.-]+)\.(?:clean|smudge|process|required)(?:=|\s|$)', line)
            if match:
                filters.add(match[1])
            elif line.startswith('filter.'):
                raise Denied('unsupported_repository_filter')
            if re.match(r'url\..*\.(?:insteadof|pushinsteadof)=', line):
                raise Denied('unsafe_repository_configuration')
            if line.startswith('core.worktree=') and Path(line.split('=', 1)[1]).resolve() != Path(repo).resolve():
                raise Denied('unsafe_repository_configuration')
        for name in sorted(filters):
            base += ['-c', f'filter.{name}.clean=', '-c', f'filter.{name}.smudge=',
                     '-c', f'filter.{name}.process=', '-c', f'filter.{name}.required=false']
        if expected_identity is not None:
            self.assert_identity(repo, expected_identity)
        return await self.command([*base, *args], cwd=repo, timeout=timeout)

    def head_identity(self, repo):
        metadata = Path(repo) / '.git'
        if not metadata.is_dir():
            with _open_regular(metadata) as source:
                link = source.read(4097).decode().strip()
            if not link.startswith('gitdir: ') or len(link) > 4096:
                raise Denied('invalid_repository_metadata')
            metadata = Path(link[8:])
            if not metadata.is_absolute():
                metadata = Path(repo) / metadata
            metadata = metadata.resolve()
            if not metadata.is_relative_to(self.root / '.project') or not _no_links(metadata):
                raise Denied('unsafe_repository_metadata')
        with _open_regular(metadata / 'HEAD') as source:
            value = source.read(4097).decode().strip()
        if value.startswith('ref: '):
            ref = value[5:]
            if not _plain(ref, 1024) or not ref.startswith('refs/heads/'):
                raise Denied('unsupported_repository_ref')
            return {'ref': ref}
        if re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value):
            return {'detached': value}
        raise Denied('invalid_repository_head')

    def assert_identity(self, repo, expected):
        if self.head_identity(repo) != expected:
            raise Denied('repository_branch_changed_concurrently')

    @staticmethod
    def origin_target(origin):
        from assistant.pipeline_config import report_target
        try:
            target = report_target({'REPORT_PAGES_REPO_URL': origin.strip(),
                                    'REPORT_PAGES_BRANCH': 'main'})
        except (AttributeError, ValueError):
            return None
        identity = target.identity
        return identity['host'], identity['project'], identity['port']

    def configured_report_target(self, key):
        if not self.cfg.get('REPORT_PUBLISH_ENABLED'):
            return None
        from assistant.pipeline_config import report_target
        target = report_target(self.cfg)
        return target if (target.host, target.project) == (key[0].lower(), key[1]) else None

    @staticmethod
    def valid_branch(value):
        return (_plain(value, 240) and not value.startswith(('-', '/', '.'))
                and not value.endswith(('/', '.', '.lock')) and '..' not in value and '@{' not in value
                and not any(char in value for char in ' ~^:?*[\\')
                and all(part and not part.startswith('.') and not part.endswith('.lock')
                        for part in value.split('/')))

    @classmethod
    def valid_ref(cls, value):
        return cls.valid_branch(value) or bool(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value))

    async def prepare(self, params):
        _object(params, {'host', 'project', 'ref', 'source_branch', 'work_branch', 'push_branch'})
        host, project = self.target(params)
        if not self.scope.get('repositories'):
            raise Denied('repositories_not_authorized')
        repository_name = project.rsplit('/', 1)[-1]
        if repository_name in RESERVED_PROJECT_DIRS:
            raise Denied('repository_path_collision')
        key = (host, project)
        configured = self.configured_report_target(key)
        expected_origin = (host.lower(), project, configured.port if configured else 22)
        clone_origin = configured.origin if configured else f'git@{host}:{project}.git'
        def matches_origin(identity):
            return (identity is not None and identity[:2] == expected_origin[:2]
                    and (configured is None or identity == expected_origin))
        if key in self.prepared:
            if params != self.preparation_requests[key]:
                raise Denied('repository_already_prepared_with_different_ref')
            repo, export = self.prepared[key]
            return {'ok': True, 'path': str(self.sandbox.map_path(export)),
                    **self.repository_info[key]}
        for field in ('source_branch', 'work_branch', 'push_branch'):
            if params.get(field) is not None and not self.valid_branch(params[field]):
                raise Denied('invalid_branch')
        if params.get('ref') is not None and not self.valid_ref(params['ref']):
            raise Denied('invalid_ref')
        if self.scope.get('gitlab_mode') != 'write' and any(params.get(field) for field in ('work_branch', 'push_branch')):
            raise Denied('write_not_authorized')
        projects = self.root / '.project'
        if not _no_links(projects):
            raise Denied('unsafe_repository_directory')
        projects.mkdir(exist_ok=True)
        repo = projects / repository_name
        if not _no_links(repo):
            raise Denied('unsafe_repository_directory')
        if repo.exists():
            if not (repo / '.git').is_dir() or (repo / '.git').is_symlink():
                raise Denied('repository_path_collision')
            actual_origin = self.origin_target(await self.git(repo, ['remote', 'get-url', 'origin']))
            if not matches_origin(actual_origin):
                raise Denied('repository_origin_mismatch')
            expected_origin = actual_origin
        else:
            # Reuse old namespace layouts without following symlinks.
            found = None
            for parent, dirs, files in os.walk(projects, followlinks=False):
                dirs[:] = [name for name in dirs if not (Path(parent) / name).is_symlink()
                           and name not in RESERVED_PROJECT_DIRS]
                if '.git' in dirs:
                    dirs.remove('.git')
                    candidate = Path(parent)
                    candidate_origin = self.origin_target(await self.git(candidate, ['remote', 'get-url', 'origin']))
                    if matches_origin(candidate_origin):
                        expected_origin = candidate_origin
                        found = candidate
                        break
            if found:
                repo = found
            else:
                await self.git(projects, ['clone', '--no-checkout', '--', clone_origin, str(repo)], timeout=120)
                if not repo.is_dir() or not _no_links(repo) or self.origin_target(await self.git(repo, ['remote', 'get-url', 'origin'])) != expected_origin:
                    raise Denied('repository_clone_not_verified')
                await self.git(repo, ['reset', '--hard', 'HEAD'])
        self.repository_origins[key] = expected_origin
        original_repo = repo
        original_identity = self.head_identity(repo)
        publication = configured if configured and self.scope.get('kind') == 'report' else None
        source_branch = params.get('source_branch') or (publication.branch if publication else None) or original_identity.get('ref', '').removeprefix('refs/heads/') or None
        source = params.get('ref') or (('refs/heads/' + source_branch) if source_branch else 'HEAD')
        fetched_ref = 'refs/agent-tasks/' + self.task_id + '/source'
        if not params.get('ref') and (params.get('source_branch') or publication):
            # A persisted clone may lag a prior publication or MR update. Each
            # task resolves its own fetched ref, without shared FETCH_HEAD state.
            await self.git(repo, ['fetch', '--no-tags', '--no-write-fetch-head', '--recurse-submodules=no', 'origin',
                                  'refs/heads/' + source_branch + ':' + fetched_ref], timeout=120)
            source = fetched_ref
        try:
            source_oid = (await self.git(repo, ['rev-parse', '--verify', source + '^{commit}'])).strip()
        except Exception:
            if not params.get('ref') and not source_branch:
                raise
            # Retrieve only the selected commit/ref from the scoped SSH origin.
            selected = params.get('ref') or 'refs/heads/' + source_branch
            await self.git(repo, ['fetch', '--no-tags', '--no-write-fetch-head', '--recurse-submodules=no', 'origin',
                                  selected + ':' + fetched_ref], timeout=120)
            source_oid = (await self.git(repo, ['rev-parse', '--verify', fetched_ref + '^{commit}'])).strip()
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', source_oid):
            raise Denied('invalid_source_commit')
        work_branch = None
        push_branch = None
        if self.scope.get('gitlab_mode') == 'write':
            work_branch = params.get('work_branch') or 'assistant/' + self.task_id
            if work_branch == source_branch:
                raise Denied('work_branch_must_differ_from_source')
            push_branch = params.get('push_branch') or (publication.branch if publication else None) or work_branch
            if push_branch not in {work_branch, source_branch}:
                raise Denied('push_branch_not_authorized')
            if publication and (source_branch != publication.branch or push_branch != publication.branch):
                raise Denied('configured_publication_branch_required')
        # Ordinary reads retain existing tracked local edits. Requested revisions
        # and every write task use a service-only worktree; the primary checkout
        # and its current branch remain untouched.
        if work_branch is not None or params.get('ref') or params.get('source_branch') or publication is not None:
            workspace = original_repo / '.git/agent-workspaces' / self.task_id
            if workspace.exists() or not _no_links(workspace):
                raise Denied('repository_worktree_collision')
            with _open_directory(workspace.parent, create=True):
                pass
            options = ['-b', work_branch] if work_branch else ['--detach']
            await self.git(original_repo, ['worktree', 'add', *options, '--', str(workspace), source_oid], timeout=120)
            repo = workspace
        info = {'persistent_repository': str(original_repo.relative_to(self.root)),
                'source_branch': source_branch, 'source_commit': source_oid,
                'work_branch': work_branch, 'push_branch': push_branch}
        export = self.work / 'repositories' / original_repo.name
        if export.exists() or not _no_links(export):
            raise Denied('repository_export_collision')
        export.mkdir(parents=True)
        tracked = await self.git(repo, ['ls-files', '--stage', '-z'])
        total = 0
        for entry in filter(None, tracked.split('\0')):
            match = re.fullmatch(r'(100644|100755) [0-9a-f]{40,64} 0\t(.+)', entry)
            if not match:
                continue
            name = match[2]
            relative = Path(name)
            if relative.is_absolute() or any(part in ('.', '..') for part in relative.parts):
                raise Denied('unsafe_repository_path')
            if not all(_safe_name(part) for part in relative.parts):
                continue
            source = repo / relative
            if source.is_symlink() or not source.exists():
                continue
            with _open_regular(source) as opened:
                total += os.fstat(opened.fileno()).st_size
            if total > 300 * 1024 * 1024:
                raise Denied('repository_too_large')
            _atomic_copy(source, export / relative, exclusive=True)
        identity = self.head_identity(repo)
        self.prepared[key] = repo, export
        self.repository_info[key] = info
        self.preparation_requests[key] = dict(params)
        self.baselines[key] = {'identity': identity, 'head': (await self.git(repo, ['rev-parse', 'HEAD'])).strip(),
                               'status': await self.git(repo, ['status', '--porcelain=v1']),
                               'files': self.snapshot(export, strict=True), 'staged': None,
                               'committed': False}
        self.assert_identity(repo, identity)
        return {'ok': True, 'path': str(self.sandbox.map_path(export)),
                **info}

    async def repository_git(self, params):
        _object(params, {'host', 'project', 'args'})
        key = self.target(params)
        if not self.scope.get('repositories') or key not in self.prepared:
            raise Denied('repository_not_prepared')
        args = params.get('args')
        if not isinstance(args, list) or not args or any(not _plain(value, 1024) for value in args):
            raise Denied('invalid_arguments')
        verb = args[0]
        if verb in ('add', 'commit', 'push'):
            if self.scope.get('gitlab_mode') != 'write':
                raise Denied('write_not_authorized')
            repo = self.prepared[key][0]
            lock = _REPOSITORY_LOCKS.setdefault(str(repo), asyncio.Lock())
            async with lock:
                return await self.repository_write(key, args)
        if verb not in ('status', 'diff', 'log', 'show'):
            raise Denied('unsupported_git_command')
        clean = [verb]
        for value in args[1:]:
            if value in ('--stat', '--name-only', '--short', '--oneline', '--cached'):
                clean.append(value)
            elif re.fullmatch(r'--max-count=(?:[1-9]|[1-9][0-9]|100)', value):
                clean.append(value)
            elif re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_./~^:-]{0,200}', value) and '..' not in value and ':' not in value:
                clean.append(value)
            else:
                raise Denied('unsupported_git_argument')
        if verb in ('show', 'log') and not any(value.startswith('--max-count=') for value in clean):
            clean += ['--max-count=20']
        if verb in ('show', 'diff'):
            clean += ['--no-ext-diff', '--no-textconv']
        safe_paths = sorted(self.baselines[key]['files'])
        if not safe_paths:
            return {'ok': True, 'data': ''}
        clean += ['--', *safe_paths]
        output = await self.git(self.prepared[key][0], clean)
        return {'ok': True, 'data': output}

    @staticmethod
    def snapshot(directory, strict=False, allowed=None):
        files, total = {}, 0
        if not _no_links(directory):
            raise Denied('unsafe_repository_directory')
        for parent, dirs, names in os.walk(directory, followlinks=False):
            for name in list(dirs):
                path = Path(parent) / name
                if path.is_symlink() or not _safe_name(name):
                    if strict:
                        raise Denied('unsafe_repository_change')
                    dirs.remove(name)
            for name in names:
                path = Path(parent) / name
                if allowed is not None and str(path.relative_to(directory)) not in allowed:
                    continue
                if path.is_symlink() or not _safe_name(name) or not path.is_file():
                    if strict:
                        raise Denied('unsafe_repository_change')
                    continue
                info = path.stat()
                total += info.st_size
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE or total > 300 * 1024 * 1024:
                    raise Denied('repository_too_large')
                digest = hashlib.sha256()
                with _open_regular(path) as source:
                    info = os.fstat(source.fileno())
                    for data in iter(lambda: source.read(65536), b''):
                        digest.update(data)
                files[str(path.relative_to(directory))] = (digest.hexdigest(), bool(info.st_mode & 0o111))
        return files

    async def repository_write(self, key, args):
        repo, export = self.prepared[key]
        baseline = self.baselines[key]
        self.assert_identity(repo, baseline['identity'])
        if 'ref' not in baseline['identity']:
            raise Denied('detached_head_write_unavailable')
        if self.origin_target(await self.git(repo, ['remote', 'get-url', 'origin'])) != self.repository_origins[key]:
            raise Denied('repository_origin_mismatch')
        head = (await self.git(repo, ['rev-parse', 'HEAD'])).strip()
        if head != baseline['head']:
            raise Denied('repository_changed_concurrently')
        self.assert_identity(repo, baseline['identity'])
        if args[0] == 'add':
            if args not in (['add', '--all'], ['add', '-A'], ['add', '.']):
                raise Denied('unsupported_git_argument')
            if baseline['status'] or await self.git(repo, ['status', '--porcelain=v1']):
                raise Denied('repository_not_clean')
            if self.snapshot(repo, allowed=baseline['files']) != baseline['files']:
                raise Denied('repository_changed_concurrently')
            current = self.snapshot(export, strict=True)
            changed = sorted(name for name in set(current) | set(baseline['files'])
                             if current.get(name) != baseline['files'].get(name))
            if not changed:
                return {'ok': True, 'data': 'No source changes', 'changed_files': []}
            # Validate every target before changing any persistent source.
            for name in changed:
                target = repo / name
                if not _no_links(target):
                    raise Denied('unsafe_repository_change')
                if target.exists() and (not target.is_file() or name not in baseline['files']):
                    raise Denied('repository_path_collision')
            for name in changed:
                self.assert_identity(repo, baseline['identity'])
                target = repo / name
                if name not in current:
                    _safe_unlink(target, baseline['files'][name])
                else:
                    _atomic_copy(export / name, target, expected=current[name],
                                 expected_target=baseline['files'].get(name))
            # Compute Git's canonical blob IDs from immutable approved copies.
            # .gitattributes text/encoding rules may legitimately differ from the
            # exact working-file bytes; configured executable filters stay off.
            objects = {}
            with tempfile.TemporaryDirectory(prefix='agent-stage-') as temporary:
                for name in changed:
                    if name not in current:
                        continue
                    staged_source = Path(temporary) / name
                    _atomic_copy(repo / name, staged_source, exclusive=True, expected=current[name])
                    oid = (await self.git(repo, ['hash-object', '--path=' + name, '--', str(staged_source)],
                                          expected_identity=baseline['identity'])).strip()
                    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', oid):
                        raise Denied('invalid_repository_blob')
                    objects[name] = ('100755' if current[name][1] else '100644', oid)
            await self.git(repo, ['-c', 'core.filemode=true', 'add', '--', *changed], expected_identity=baseline['identity'])
            self.assert_identity(repo, baseline['identity'])
            await self.validate_index(repo, current, changed, objects)
            baseline['tree'] = (await self.git(repo, ['write-tree'])).strip()
            if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', baseline['tree']):
                raise Denied('invalid_repository_tree')
            await self.validate_tree(repo, baseline['tree'], baseline['head'], current, changed, objects)
            baseline['objects'] = objects
            baseline['staged'] = current
            baseline['changed'] = changed
            return {'ok': True, 'changed_files': changed}
        if args[0] == 'commit':
            if len(args) != 3 or args[1] not in ('-m', '--message') or not _plain(args[2], 4000):
                raise Denied('invalid_commit_message')
            if baseline['staged'] is None or self.snapshot(repo, allowed=baseline['staged']) != baseline['staged']:
                raise Denied('repository_changed_concurrently')
            staged = await self.git(repo, ['diff', '--cached', '--name-only', '-z'])
            if set(filter(None, staged.split('\0'))) != set(baseline['changed']):
                raise Denied('repository_index_changed_concurrently')
            await self.validate_index(repo, baseline['staged'], baseline['changed'], baseline['objects'])
            await self.validate_tree(repo, baseline['tree'], baseline['head'],
                                     baseline['staged'], baseline['changed'], baseline['objects'])
            if (await self.git(repo, ['write-tree'])).strip() != baseline['tree']:
                raise Denied('repository_index_changed_concurrently')
            # commit-tree consumes the approved immutable tree rather than a
            # mutable shared index. The ref update has an atomic expected-HEAD CAS.
            output = (await self.git(repo, ['-c', 'commit.gpgsign=false', 'commit-tree',
                baseline['tree'], '-p', baseline['head'], '-m', args[2]],
                expected_identity=baseline['identity'])).strip()
            if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', output):
                raise Denied('invalid_repository_commit')
            await self.git(repo, ['update-ref', '--no-deref', '-m', 'task-approved commit',
                                  baseline['identity']['ref'], output, baseline['head']],
                           expected_identity=baseline['identity'])
            self.assert_identity(repo, baseline['identity'])
            baseline['head'] = output
            baseline['files'] = baseline['staged']
            baseline['staged'] = None
            baseline['committed'] = True
            return {'ok': True, 'data': output}
        if args != ['push'] or not baseline['committed']:
            raise Denied('push_requires_task_commit')
        if await self.git(repo, ['status', '--porcelain=v1']) or self.snapshot(repo, allowed=baseline['files']) != baseline['files']:
            raise Denied('repository_changed_concurrently')
        push_target = await self.git(repo, ['remote', 'get-url', '--push', '--all', 'origin'])
        push_targets = push_target.splitlines()
        if len(push_targets) != 1 or self.origin_target(push_targets[0]) != self.repository_origins[key]:
            raise Denied('repository_push_origin_mismatch')
        output = await self.git(repo, ['-c', 'remote.origin.mirror=false', 'push', '--no-verify', '--no-follow-tags', '--recurse-submodules=no', 'origin', baseline['head'] + ':refs/heads/' + self.repository_info[key]['push_branch']],
                                timeout=120, expected_identity=baseline['identity'])
        return {'ok': True, 'data': output}

    async def validate_tree(self, repo, tree, head, expected, changed, objects=None):
        differences = await self.git(repo, ['diff-tree', '--no-commit-id', '--no-ext-diff',
            '--no-textconv', '--name-only', '-r', '-z', head, tree, '--'])
        if set(filter(None, differences.split('\0'))) != set(changed):
            raise Denied('repository_tree_changed_concurrently')
        raw = await self.git(repo, ['ls-tree', '-r', '-z', tree, '--', *changed])
        entries = {}
        for entry in filter(None, raw.split('\0')):
            match = re.fullmatch(r'(100644|100755) blob ([0-9a-f]{40}|[0-9a-f]{64})\t(.+)', entry)
            if not match or match[3] in entries:
                raise Denied('repository_tree_changed_concurrently')
            entries[match[3]] = (match[1], match[2])
        self.validate_entries(repo, entries, expected, changed, objects)

    async def validate_index(self, repo, expected, changed, objects=None):
        raw = await self.git(repo, ['ls-files', '--stage', '-z', '--', *changed])
        entries = {}
        for entry in filter(None, raw.split('\0')):
            match = re.fullmatch(r'(100644|100755) ([0-9a-f]{40}|[0-9a-f]{64}) 0\t(.+)', entry)
            if not match or match[3] in entries:
                raise Denied('repository_index_changed_concurrently')
            entries[match[3]] = (match[1], match[2])
        self.validate_entries(repo, entries, expected, changed, objects)

    @staticmethod
    def validate_entries(repo, entries, expected, changed, objects=None):
        for name in changed:
            if name not in expected:
                if name in entries:
                    raise Denied('repository_index_changed_concurrently')
                continue
            with _open_regular(repo / name) as source:
                content = source.read(MAX_FILE + 1)
            blob = b'blob ' + str(len(content)).encode() + b'\0' + content
            mode = '100755' if expected[name][1] else '100644'
            entry = entries.get(name)
            canonical = (objects or {}).get(name)
            if canonical is not None:
                valid = entry == canonical
            else:
                valid = entry is not None and entry[0] == mode and entry[1] == (
                    hashlib.sha256(blob).hexdigest() if len(entry[1]) == 64
                    else hashlib.sha1(blob).hexdigest())
            if not valid:
                raise Denied('repository_index_changed_concurrently')
            if hashlib.sha256(content).hexdigest() != expected[name][0]:
                raise Denied('repository_changed_concurrently')

    async def send_file(self, params):
        _object(params, {'path'})
        if not self.scope.get('deliver_files'):
            raise Denied('delivery_not_authorized')
        name = params.get('path')
        if not _plain(name, 4096):
            raise Denied('invalid_file')
        path = Path(name)
        for source in self.inputs:
            if str(self.sandbox.map_path(source)) == name:
                path = source
                break
        # Translate known sandbox roots without accepting arbitrary host paths.
        sandbox_work = Path(self.sandbox.work_path)
        if path.is_relative_to(sandbox_work):
            path = self.work / path.relative_to(sandbox_work)
        elif not path.is_absolute():
            path = self.work / path
        if not _no_links(path):
            raise Denied('unsafe_file')
        path = path.resolve()
        if not path.is_relative_to(self.work) and path not in self.inputs:
            raise Denied('file_not_authorized')
        if path.is_relative_to(self.work) and path.name in {
                'prompt.json', 'result.json', 'response.schema.json', 'tool_client.py', SOCKET_NAME}:
            raise Denied('task_internal_file')
        if path in self.delivered_files:
            return {'ok': True, 'already_delivered': True, 'file_name': path.name}
        if not path.is_file() or not 0 < path.stat().st_size <= MAX_FILE:
            raise Denied('invalid_file')
        thread = self.cfg.get('_delivery_root')
        if not _plain(thread, 256) or not thread.startswith('om_'):
            raise Denied('delivery_topic_unavailable')
        # The model may edit work while upload runs. Send a stable host-only copy;
        # the credential-bearing CLI never opens a mutable sandbox path.
        with tempfile.TemporaryDirectory(prefix='agent-delivery-') as temporary:
            staging = Path(temporary)
            with _open_regular(path) as source, (staging / path.name).open('xb') as destination:
                size = 0
                for chunk in iter(lambda: source.read(65536), b''):
                    size += len(chunk)
                    if size > MAX_FILE:
                        raise Denied('invalid_file')
                    destination.write(chunk)
            if not size:
                raise Denied('invalid_file')
            output = await self.command([self.cfg['LARK_CLI_BIN'], 'im', '+messages-reply',
                '--as', 'bot', '--message-id', thread, '--reply-in-thread', '--file', './' + path.name],
                cwd=staging, timeout=120)
        try:
            payload = json.loads(output)
            data = payload.get('data') or {}
            message = data.get('message') or data
            message_id = message.get('message_id') or message.get('id')
        except (ValueError, AttributeError):
            raise Denied('delivery_unconfirmed')
        if payload.get('ok') is not True or not _plain(message_id, 256):
            raise Denied('delivery_unconfirmed')
        self.delivered_files.add(path)
        return {'ok': True, 'message_id': message_id, 'file_name': path.name}


def tool_gateway(cfg, root, scope, run, db=None, input_files=()):
    """Return the Runner's per-task async context manager factory."""
    @asynccontextmanager
    async def factory(work, sandbox):
        work = Path(work).resolve()
        socket = work / SOCKET_NAME
        client = work / 'tool_client.py'
        if not _no_links(socket) or socket.exists() or client.exists() or client.is_symlink():
            raise ValueError('Task tool path is occupied')
        source = Path(__file__).resolve().parents[1] / 'scripts/agent_tool.py'
        with source.open('rb') as src, client.open('xb') as dst:
            shutil.copyfileobj(src, dst)
        client.chmod(0o600)
        gateway = TaskTools(cfg, root, scope, run, work, sandbox, db, input_files)
        factory.tools = gateway
        active = set()
        async def handle(reader, writer):
            task = asyncio.current_task()
            active.add(task)
            try:
                try:
                    raw = await asyncio.wait_for(reader.readline(), 5)
                    if not raw.endswith(b'\n') or len(raw) > MAX_REQUEST:
                        raise ValueError('Invalid request')
                    request = json.loads(raw)
                    _object(request, {'operation', 'params'})
                    response = await gateway.dispatch(request.get('operation'), request.get('params', {}))
                except (ValueError, UnicodeError, asyncio.TimeoutError):
                    response = {'ok': False, 'error': {'type': 'invalid_request'}}
                writer.write((json.dumps(response, ensure_ascii=False) + '\n').encode())
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                active.discard(task)
        server = await asyncio.start_unix_server(handle, path=str(socket), limit=MAX_REQUEST + 1)
        socket.chmod(0o600)
        try:
            yield ('\n本任务业务工具由宿主服务代理，认证和业务配置不会进入任务沙箱。调用：\n'
                   f'python3 {shlex.quote(str(sandbox.work_path) + chr(47) + "tool_client.py")} 操作名 \'JSON对象\'\n'
                   '示例：python3 ' + shlex.quote(str(sandbox.work_path) + '/tool_client.py') +
                   ' history_search \'{"query":"关键词","page":1}\'\n'
                   '操作：history_search {query,page}；images_search {session_id,message_id,limit,copy}；'
                   'feishu_read {operation:search|messages|threads,...}（只读）；contact_search {query}（仅 GitLab 写授权任务的主人用户身份只读通讯录定位）；'
                   'gitlab_cli {host,project,args:[mr|issue|ci|job|api,...]}；'
                   'repository_prepare {host,project,ref?,source_branch?,work_branch?,push_branch?} 返回已脱敏的可编辑源码目录与分支信息；'
                   'repository_git {host,project,args:[status|diff|log|show|add|commit|push,...]} 读取宿主持久仓库；'
                   'send_file {path} 发送附件到当前任务话题。仅使用主人本轮授权项目，'
                   '写操作仅在本轮已授权时可用，不需要重复确认。仓库写任务默认使用独立 assistant/ 工作分支与工作树，原始检出保持不动；仅可推送到 prepare 返回的 push_branch。仓库发布依次调用 add --all、commit -m 审阅后说明、push；工具会检查任务基线与原仓库并发变化。'
                   '所有失败以工具结果为准，不得声称已完成。')
        finally:
            server.close()
            await server.wait_closed()
            for task in list(active):
                task.cancel()
            if active:
                await asyncio.gather(*list(active), return_exceptions=True)
            if socket.exists() and not socket.is_symlink() and stat.S_ISSOCK(socket.lstat().st_mode):
                socket.unlink()
    return factory
