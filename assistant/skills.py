"""Configured SSH skill repository, metadata search and checked task materials."""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import time
from urllib.parse import unquote, urlsplit
import zipfile
from zoneinfo import ZoneInfo

from .pipeline_config import repository_target
from .agent_policy import protected


MAX_ENTRIES = 10000
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024


class MissingSkillBranch(ValueError):
    pass


def _target(cfg):
    branch = cfg.get('SKILLS_REPO_BRANCH') or ''
    target = repository_target({**cfg, 'SKILLS_REPO_BRANCH': branch or 'main'},
                               'SKILLS_REPO_URL', 'SKILLS_REPO_BRANCH')
    return target, branch


def validate_config(cfg):
    if cfg.get('SKILLS_REPO_ENABLED'):
        _target(cfg)


def metadata(path, base):
    text = path.read_text(encoding='utf-8')
    front = re.match(r'^---\s*\n(.*?)\n---', text, re.S)
    if not front:
        return None
    fields = {}
    lines = front[1].splitlines()
    for i, line in enumerate(lines):
        match = re.match(r'^(name|description):\s*(.*)', line)
        if match:
            value = match[2].strip()
            if value in ('>', '|', '>-', '|-'):
                value = ' '.join(x.strip() for x in lines[i+1:] if x.startswith((' ', '\t')))
            fields[match[1]] = value.strip('\"\'')
    if not fields.get('name') or not fields.get('description'):
        return None
    return {**fields, 'path': str(path.relative_to(base))}


def tokens(text):
    text = text.casefold()
    result = set(re.findall(r'[a-z0-9][a-z0-9_-]*', text))
    for run in re.findall(r'[\u4e00-\u9fff]+', text):
        result.update(run[i:i+2] for i in range(len(run)-1))
        if len(run) == 1:
            result.add(run)
    return result


def search(index, query, limit=5):
    terms = tokens(query)
    identifiers = set(re.findall(r'[a-z][a-z0-9_-]*', query.casefold()))
    for aliases in [('提交', '创建', '新建', 'create'), ('查询', '查找', '查看', 'search'),
                    ('修改', '更新', 'update'), ('删除', '移除', 'delete')]:
        if any(word in query.casefold() for word in aliases):
            terms.update(aliases)
    ranked = []
    for item in index:
        name = item['name'].casefold()
        description = re.sub(r'不负责[^。；]*', '', item['description'].casefold())
        if identifiers and not any(t in name or t in description or t in item['path'].casefold() for t in identifiers):
            continue
        score = sum(8 if t in name else 2 if t in description else 1 if t in item['path'].casefold() else 0
                    for t in terms)
        if name in query.casefold():
            score += 20
        if score:
            ranked.append((score, item))
    return [item for _, item in sorted(ranked, key=lambda x: (-x[0], x[1]['path']))[:limit]]


def _relative(name):
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or '\\' in name or '\x00' in name
            or any(p in ('', '.', '..') for p in name.rstrip('/').split('/'))
            or protected(str(path).casefold()) or any(p.casefold() == 'credential' for p in path.parts)):
        raise ValueError('Unsafe skill file path')
    return Path(*path.parts)


def _directory(path):
    # All ancestors must be real directories; never follow state/clone symlinks.
    for ancestor in [*reversed(path.parents), path]:
        if ancestor.is_symlink() or (ancestor.exists() and not ancestor.is_dir()):
            raise ValueError('Unsafe skill directory')


def _read_file(base, name, digest=None, with_mode=False):
    relative = _relative(name)
    target = base / relative
    _directory(target.parent)
    descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE_BYTES:
            raise ValueError('Unsafe skill file')
        content = source.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES or (digest is not None and hashlib.sha256(content).hexdigest() != digest):
        raise ValueError('Skill snapshot has changed')
    return (content, info.st_mode) if with_mode else content


def _document_references(name, content):
    if Path(name).suffix.casefold() not in ('.md', '.markdown'):
        return
    try:
        text = content.decode('utf-8')
    except UnicodeDecodeError:
        return
    # Markdown links/images and reference-link definitions are explicit inputs;
    # prose, commands and remote destinations never expand task access.
    destinations = re.findall(r'!?\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|([^\s)]+))', text)
    destinations += re.findall(r'^\s{0,3}\[[^\]\n]+\]:\s*(?:<([^>\n]+)>|(\S+))', text, re.M)
    for enclosed, plain in destinations:
        destination = urlsplit(enclosed or plain)
        if destination.scheme or destination.netloc or not destination.path:
            continue
        path = PurePosixPath(unquote(destination.path))
        if path.is_absolute():
            raise ValueError('Task skill reference escapes the snapshot')
        parts = list(PurePosixPath(name).parent.parts)
        for part in path.parts:
            if part == '..':
                if not parts:
                    raise ValueError('Task skill reference escapes the snapshot')
                parts.pop()
            elif part != '.':
                parts.append(part)
        yield str(_relative('/'.join(parts)))


@contextmanager
def task_materials(data, matches):
    if not data or not matches:
        yield None
        return
    snapshot = Path(data['snapshot'])
    _directory(snapshot)
    known = {item['path']: item for item in data['index']}
    folders = set()
    for item in matches:
        name = item.get('path', '')
        relative = _relative(name)
        if name not in known or relative.name != 'SKILL.md' or len(relative.parts) < 2 or relative.parts[0] != 'skills':
            raise ValueError('Unknown task skill')
        folders.add(relative.parent)
    files = data['files']
    if len(files) > MAX_ENTRIES:
        raise ValueError('Skill snapshot is too large')
    paths = {name: _relative(name) for name in files}
    # A nested SKILL.md starts a separate skill, including definitions that do
    # not have valid metadata. Selecting a parent never grants that subtree.
    roots = {path.parent for path in paths.values() if path.name == 'SKILL.md'}
    owners = {name: next((parent for parent in path.parents if parent in roots), None)
              for name, path in paths.items()}
    scheduled = {name for name, owner in owners.items() if owner in folders}
    pending = list(sorted(scheduled, reverse=True))
    with tempfile.TemporaryDirectory(prefix='assistant-skills-') as temporary:
        staging = Path(temporary)
        total = 0
        while pending:
            name = pending.pop()
            relative = paths[name]
            content, mode = _read_file(snapshot, name, files[name], with_mode=True)
            total += len(content)
            if total > MAX_TOTAL_BYTES:
                raise ValueError('Task skills are too large')
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with target.open('xb') as output:
                output.write(content)
            target.chmod(0o500 if mode & stat.S_IXUSR else 0o400)
            for reference in _document_references(name, content):
                if reference not in files:
                    continue
                additions = {reference}
                if reference in known and paths[reference].name == 'SKILL.md':
                    folder = paths[reference].parent
                    additions.update(candidate for candidate, owner in owners.items() if owner == folder)
                pending.extend(sorted(additions - scheduled, reverse=True))
                scheduled.update(additions)
        if any(not (staging / folder / 'SKILL.md').is_file() for folder in folders):
            raise ValueError('Task skill is missing')
        yield staging


class SkillRepository:
    def __init__(self, cfg, root, state, run):
        target, self.branch = _target(cfg)
        self.repo = Path(root) / '.project/skills'
        self.state = Path(state) / 'skills'
        self.url = target.origin
        self.source = {**target.identity, 'branch': self.branch}
        self.key = hashlib.sha256(json.dumps(self.source, sort_keys=True).encode()).hexdigest()[:20]
        self.timezone = ZoneInfo(cfg.get('TIMEZONE') or 'Asia/Shanghai')
        self.run = run
        self.lock = asyncio.Lock()
        self.timeout = 120

    async def git(self, *args, cwd=None):
        # Preserve ordinary SSH authentication, without unrelated service secrets
        # or inherited Git command/config/template overrides.
        env = {key: os.environ[key] for key in ('PATH', 'HOME', 'USER', 'LOGNAME',
               'SSH_AUTH_SOCK', 'SSH_AGENT_PID', 'LANG', 'LC_ALL', 'TMPDIR') if key in os.environ}
        env.update(GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1',
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_ATTR_NOSYSTEM='1')
        command = ['git', '-c', 'core.sshCommand=ssh -o BatchMode=yes', '-c', 'core.hooksPath=/dev/null',
                   '-c', 'core.fsmonitor=false', '-c', 'core.attributesFile=/dev/null',
                   '-c', 'submodule.recurse=false', '-c', 'protocol.allow=never', '-c', 'protocol.ssh.allow=always', *args]
        try:
            return (await self.run(command, self.timeout, cwd=cwd or self.repo, env=env)).strip()
        except (RuntimeError, OSError, asyncio.TimeoutError):
            raise RuntimeError('Skill repository Git operation failed') from None

    def _write_json(self, name, data):
        _directory(self.state)
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile('w', dir=self.state, prefix='write-', delete=False) as output:
            temporary = Path(output.name)
            try:
                json.dump(data, output, ensure_ascii=False)
                output.flush()
                temporary.replace(self.state / name)
            finally:
                temporary.unlink(missing_ok=True)

    def sync_status(self, status, success=False):
        data = {'status': status, 'last_attempt': time.time(), 'source': self.source}
        if success:
            data['last_success'] = time.time()
        self._write_json('sync-status.json', data)

    async def sync(self):
        async with self.lock:
            self.sync_status('同步中')
            try:
                result = await self._sync()
            except asyncio.CancelledError:
                self.sync_status('同步中断')
                raise
            except (RuntimeError, ValueError, OSError, asyncio.TimeoutError):
                self.sync_status('同步失败')
                raise
            self.sync_status('同步成功', success=True)
            return result

    async def _sync(self):
        _directory(self.repo)
        self.repo.parent.mkdir(parents=True, exist_ok=True)
        if not self.repo.exists():
            with tempfile.TemporaryDirectory(dir=self.repo.parent, prefix='skills-clone-') as temporary:
                target = Path(temporary) / 'repo'
                if self.branch:
                    refs = await self.git('ls-remote', '--heads', '--', self.url,
                                          f'refs/heads/{self.branch}', cwd=self.repo.parent)
                    if not refs:
                        raise MissingSkillBranch(self.branch)
                branch_args = ['--branch', self.branch] if self.branch else []
                await self.git('clone', '--no-checkout', '--template=', '--no-recurse-submodules',
                               *branch_args, '--', self.url, str(target), cwd=self.repo.parent)
                data = await self._sync_checkout(target, initial=True)
                target.rename(self.repo)
        else:
            data = await self._sync_checkout(self.repo)
        self._write_json('current.json', data)
        return data

    async def _sync_checkout(self, repo, initial=False):
        async def git(*args):
            return await self.git(*args, cwd=repo)

        git_directory = repo / '.git'
        if not git_directory.is_dir() or git_directory.is_symlink():
            raise ValueError('Skill directory is not a regular Git repository')
        if (git_directory / 'commondir').exists() or (git_directory / 'gitdir').exists():
            raise ValueError('Skill repository Git metadata is redirected')
        pending = [git_directory]
        while pending:
            for path in pending.pop().iterdir():
                info = path.lstat()
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError('Unsafe skill repository Git metadata')
                # Local clones may share immutable content-addressed objects.
                # Mutable refs, logs, configuration and other metadata must not
                # be linked to files outside the fixed clone.
                relative = path.relative_to(git_directory).as_posix()
                readonly_object = (not info.st_mode & 0o222 and re.fullmatch(
                    r'objects/(?:[0-9a-f]{2}/[0-9a-f]{38,62}|pack/pack-[0-9a-f]{40,64}\.(?:pack|idx|rev|bitmap))', relative))
                if info.st_nlink != 1 and not readonly_object:
                    raise ValueError('Skill repository Git metadata is hardlinked')
        # Local includes and filters can run arbitrary host commands during status/checkout.
        local = await git('config', '--local', '--no-includes', '--list')
        if any(line.casefold().startswith(('filter.', 'include.', 'includeif.', 'extensions.', 'core.worktree=')) for line in local.splitlines()):
            raise ValueError('Unsafe skill repository configuration')
        if (Path(await git('rev-parse', '--show-toplevel')).resolve() != repo.resolve()
                or Path(await git('rev-parse', '--absolute-git-dir')).resolve() != git_directory.resolve()):
            raise ValueError('Skill repository is outside the fixed clone directory')
        origin = await git('remote', 'get-url', 'origin')
        other, _ = _target({'SKILLS_REPO_URL': origin, 'SKILLS_REPO_BRANCH': self.branch})
        if {**other.identity, 'branch': self.branch} != self.source:
            raise ValueError('Skill repository origin differs from configured URL')
        if not initial and await git('status', '--porcelain', '--untracked-files=all'):
            raise ValueError('Skill repository has local changes; update skipped')
        await git('fetch', '--prune', 'origin')
        if self.branch:
            branch = self.branch
        else:
            await git('remote', 'set-head', 'origin', '--auto')
            branch = (await git('symbolic-ref', 'refs/remotes/origin/HEAD')).removeprefix('refs/remotes/origin/')
        if await git('symbolic-ref', '--short', 'HEAD') != branch:
            raise ValueError('Skill repository is not on the selected branch')
        remote = f'refs/remotes/origin/{branch}'
        if self.branch and remote not in (await git('for-each-ref', '--format=%(refname)', remote)).splitlines():
            raise MissingSkillBranch(self.branch)
        commit = await git('rev-parse', remote)
        if not re.fullmatch(r'[a-f0-9]{40,64}', commit):
            raise ValueError('Invalid skill repository revision')
        await git('merge-base', '--is-ancestor', 'HEAD', commit)
        # archive honors export-ignore; inspect the actual Git tree as well so
        # omitted unsafe files cannot reach the host during checkout.
        entries = (await git('ls-tree', '-rz', '--full-tree', commit)).split('\0')
        if len(entries) - 1 > MAX_ENTRIES:
            raise ValueError('Skill repository has too many entries')
        for entry in entries:
            if not entry:
                continue
            attributes, name = entry.split('\t', 1)
            mode, kind, _ = attributes.split()
            _relative(name)
            if mode not in ('100644', '100755') or kind != 'blob':
                raise ValueError('Unsafe skill repository file')
        snapshot = self.state / f'{self.key}-{commit}'
        _directory(snapshot)
        # Re-extract into a private directory to verify an existing snapshot too.
        with tempfile.TemporaryDirectory(dir=self.state, prefix='snapshot-') as temporary:
            temporary = Path(temporary)
            archive = temporary / 'repo.zip'
            await git('archive', '--format=zip', '--output', str(archive), commit)
            content = temporary / 'content'
            content.mkdir(mode=0o700)
            files = {}
            total = 0
            with zipfile.ZipFile(archive) as source:
                entries = source.infolist()
                if len(entries) > MAX_ENTRIES:
                    raise ValueError('Skill archive has too many entries')
                seen = set()
                for entry in entries:
                    relative = _relative(entry.filename)
                    if str(relative) in seen:
                        raise ValueError('Duplicate skill archive path')
                    seen.add(str(relative))
                    mode = entry.external_attr >> 16
                    if entry.is_dir():
                        if stat.S_IFMT(mode) not in (0, stat.S_IFDIR):
                            raise ValueError('Unsafe archive directory')
                        continue
                    if stat.S_IFMT(mode) not in (0, stat.S_IFREG) or entry.file_size > MAX_FILE_BYTES:
                        raise ValueError('Unsafe archive file')
                    total += entry.file_size
                    if total > MAX_TOTAL_BYTES:
                        raise ValueError('Skill archive is too large')
                    body = source.read(entry)
                    target = content / relative
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    target.write_bytes(body)
                    target.chmod(0o500 if mode & stat.S_IXUSR else 0o400)
                    files[str(relative)] = hashlib.sha256(body).hexdigest()
            index = []
            for path in sorted((content / 'skills').rglob('SKILL.md')):
                item = metadata(path, content)
                if item:
                    index.append(item)
            if not index:
                raise ValueError('No valid skills found')
            if snapshot.exists():
                for name, digest in files.items():
                    _read_file(snapshot, name, digest)
            else:
                content.rename(snapshot)
        # Only validated, pinned content may enter the persistent checkout.
        if initial:
            await git('reset', '--hard', commit)
        else:
            await git('merge', '--ff-only', commit)
        now = datetime.now(self.timezone)
        sync_day = now.date() if now.hour >= 9 else now.date() - timedelta(days=1)
        data = {'commit': commit, 'date': sync_day.isoformat(), 'index': index, 'files': files,
                'source': self.source, 'branch': branch, 'snapshot': str(snapshot)}
        return data

    def cached(self):
        try:
            raw = _read_file(self.state, 'current.json')
            data = json.loads(raw)
            expected = self.state / f'{self.key}-{data["commit"]}'
            if (data.get('source') != self.source or data.get('snapshot') != str(expected)
                    or not re.fullmatch(r'[a-f0-9]{40,64}', data['commit'])
                    or not isinstance(data.get('index'), list) or not isinstance(data.get('files'), dict)):
                return None
            _directory(expected)
            if not expected.is_dir():
                return None
            return data
        except (OSError, ValueError, KeyError, TypeError):
            return None

    async def current(self):
        return self.cached() or await self.sync()

    async def handle_command(self, query):
        if not query:
            return '用法：`/skill 需求描述`，例如 `/skill 提交issue`。此命令只查找技能，不执行任务。\n\n本次未使用任务技能。'
        try:
            data = await self.sync()
        except MissingSkillBranch:
            return (f'技能仓库更新失败：配置的分支 `{self.branch}` 在远端不存在，'
                    '请检查 SKILLS_REPO_BRANCH；本次未进行查找。\n\n本次未使用任务技能。')
        except (RuntimeError, ValueError, OSError, asyncio.TimeoutError):
            logging.warning('Skill command synchronization failed')
            return '技能仓库更新失败，无法确认最新内容，本次未进行查找。请检查仓库访问权限、网络或本地修改后重试。\n\n本次未使用任务技能。'
        matches = search(data['index'], query)
        lines = [f'已更新技能仓库（版本 {data["commit"][:10]}）。']
        if matches:
            lines += ['', '相关技能：'] + [f'- **{x["name"]}**：{x["description"][:240]}' for x in matches]
        else:
            lines += ['', '未找到相关技能，请尝试补充平台名称或具体操作。']
        return '\n'.join(lines) + '\n\n本次仅查找，未使用任务技能。'

    def due(self, now, data):
        return not data or (now.hour >= 9 and data.get('date') != now.date().isoformat())

    async def scheduler(self):
        while True:
            if self.due(datetime.now(self.timezone), self.cached()):
                try:
                    await self.sync()
                    logging.info('Skill repository synchronized')
                except (RuntimeError, ValueError, OSError, asyncio.TimeoutError):
                    logging.warning('Skill synchronization failed; retaining previous snapshot')
                    await asyncio.sleep(60)
                    continue
            await asyncio.sleep(30)


def repository(cfg, root, state, run):
    target, branch = _target(cfg)
    existing = cfg.get('_skill_repository')
    if (existing is None or existing.source != {**target.identity, 'branch': branch}
            or existing.repo != Path(root) / '.project/skills'
            or existing.state != Path(state) / 'skills'
            or str(existing.timezone) != (cfg.get('TIMEZONE') or 'Asia/Shanghai')):
        cfg['_skill_repository'] = SkillRepository(cfg, root, state, run)
    return cfg['_skill_repository']
