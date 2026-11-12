"""Fail-closed Linux process isolation shared by all supported model CLIs.

The service must obtain approval before creating an execute policy. This module
accepts only that service-side reference; model text can never grant approval.
Source and repository views are sanitized copies. publish() is explicit and
checks concurrent host changes before applying changes to granted source files.
The network namespace has no external interface. HTTP(S) clients must honor the
provided proxy; clients that ignore it fail rather than gain direct networking.
"""
from collections import Counter
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile

from .agent_network import NetworkProxy
from .agent_policy import no_symlinks, protected, path_denied


class IsolationError(RuntimeError):
    pass


# Operating-system code only; PROJECT_ROOT and host HOME are overlaid below.
SYSTEM_RUNTIME_ROOTS = ('/usr',)


def _walk_error(error):
    raise IsolationError('Isolation cannot inspect a mounted directory') from error


def _runtime_protected(relative):
    return any(part in ('.local', '.project', '.git', '.ssh', '.aws', '.config',
                        '.codex', '.claude', '.grok', '.npmrc', '.netrc', '.pypirc',
                        '.credentials.json', 'credential') or
               part == '.env' or part.startswith('.env.')
               for part in Path(relative).parts)


def _validate_mount_tree(root, runtime=False, sensitive_paths=()):
    """Metadata-only validation; never dereference links or inspect credentials."""
    root = Path(root)
    check_name = _runtime_protected if runtime else protected
    paths = [root]
    if root.is_dir():
        for current, dirs, files in os.walk(root, followlinks=False, onerror=_walk_error):
            paths.extend(Path(current) / name for name in (*dirs, *files))
    metadata = [(path, path.lstat()) for path in paths]
    internal_links = Counter((info.st_dev, info.st_ino) for path, info in metadata
                             if stat.S_ISREG(info.st_mode))
    for path, info in metadata:
        relative = path.relative_to(root)
        if path_denied(path, sensitive_paths) or (path != root and check_name(relative)):
            raise IsolationError('Mount contains protected paths')
        if stat.S_ISLNK(info.st_mode):
            raise IsolationError('Mount contains a symbolic link')
        if stat.S_ISDIR(info.st_mode):
            continue
        expected_links = internal_links[(info.st_dev, info.st_ino)] if runtime else 1
        if (info.st_nlink != expected_links or
                not (stat.S_ISREG(info.st_mode) or
                     (not runtime and stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid()))):
            raise IsolationError('Mount contains a hardlink or unsupported file type')


_ENV_KEYS = {
    'codex': ('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'CODEX_API_KEY', 'OPENAI_ORG_ID',
              'OPENAI_PROJECT_ID'),
    'claudecode': ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
                  'CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_MODEL',
                  'ANTHROPIC_DEFAULT_HAIKU_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL',
                  'ANTHROPIC_DEFAULT_OPUS_MODEL'),
    'grok': ('XAI_API_KEY', 'GROK_API_KEY', 'XAI_BASE_URL', 'GROK_BASE_URL', 'GROK_API_BASE'),
}
_DEFAULT_AUTH = {
    'codex': ('.codex/auth.json',),
    'claudecode': ('.claude/.credentials.json',),
    'grok': ('.grok/auth.json',),
}


def _snapshot(path):
    # File capabilities/setuid/setgid/sticky bits are never part of an agent
    # publication. Ordinary permissions, including executability, are changes.
    return (hashlib.sha256(path.read_bytes()).hexdigest(),
            stat.S_IMODE(path.stat().st_mode) & 0o777)


def _files(root, strict=False, excluded=()):
    result = {}
    if not root.exists():
        return result
    for current, dirs, files in os.walk(root, followlinks=False, onerror=_walk_error):
        parent = Path(current)
        for name in list(dirs):
            path = parent / name
            rel = path.relative_to(root)
            if path.is_symlink() or protected(rel) or path_denied(rel, excluded):
                if strict:
                    raise IsolationError('Publication contains a protected path or symbolic link')
                dirs.remove(name)
        for name in files:
            path = parent / name
            rel = path.relative_to(root)
            if (path.is_symlink() or protected(rel) or path_denied(rel, excluded) or not stat.S_ISREG(path.lstat().st_mode)
                    or path.lstat().st_nlink != 1):
                if strict:
                    raise IsolationError('Publication contains a protected or nonregular file')
                continue
            result[rel] = _snapshot(path)
    return result


def _copy_view(host, destination, excluded=()):
    destination.mkdir(parents=True, exist_ok=True)
    baseline = _files(host, excluded=excluded)
    for rel in baseline:
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(host / rel, target)
        target.chmod(baseline[rel][1])
    return baseline


class Sandbox:
    def __init__(self, policy, cfg, work, executable, staging, socket_path):
        self.policy = policy
        self.cfg = cfg
        self.host_work = no_symlinks(work)
        self.executable = Path(executable).resolve(strict=True)
        self.staging = staging
        self.socket_path = socket_path
        self.path_map = {policy.root / '.project': Path('/workspace/.project')}
        self.views = []
        self._published = False
        self.work_path = Path('/tmp/agent-work')
        if policy.kind == 'report':
            try:
                relative = self.host_work.relative_to(policy.root / '.project/reports')
            except ValueError:
                raise IsolationError('Report work must be a task directory under .project/reports') from None
            if not relative.parts:
                raise IsolationError('Report work must name a specific task directory')
            self.work_path = Path('/workspace/.project/reports') / relative
        if not self.host_work.is_dir():
            raise IsolationError('Task work directory does not exist')
        # Only dedicated temporary staging or a report task may be live-mounted.
        temporary_root = Path(tempfile.gettempdir()).resolve()
        report_work = (policy.kind == 'report' and
                       self.host_work.is_relative_to(policy.root / '.project/reports'))
        if (self.host_work == temporary_root or
                (not report_work and (not self.host_work.is_relative_to(temporary_root) or
                                      self.host_work.is_relative_to(policy.root))) or
                policy.root.is_relative_to(self.host_work) or
                Path.home().is_relative_to(self.host_work)):
            raise IsolationError('Task work must be a dedicated temporary or report directory')
        # A task work directory is trusted staging, never the service state tree.
        if self.host_work == policy.root or self.host_work == Path.home():
            raise IsolationError('Task work cannot expose the source root or HOME')
        parts = self.host_work.parts
        if '.local' in parts or 'credential' in parts or '.git' in parts:
            raise IsolationError('Task work cannot expose sensitive service paths')
        _validate_mount_tree(self.host_work, sensitive_paths=self.policy.sensitive_paths)
        self.path_map[self.host_work] = self.work_path
        if policy.source_readable:
            self._view(policy.root, Path('/workspace/source'), policy.source_writable)
        for index, host in enumerate(policy.read_only_dirs):
            _validate_mount_tree(host, sensitive_paths=self.policy.sensitive_paths)
            self._view(host, Path('/run/agent/skills') / str(index), False)
        for index, host in enumerate(policy.writable_roots):
            if host == self.host_work:
                continue
            alias = Path('/workspace/.project') / host.relative_to(policy.root / '.project')
            self._view(host, alias, True)
        self.inputs = []
        for index, host in enumerate(policy.input_files):
            # Copy inputs, so the sandbox cannot race host path replacement.
            target = staging / 'inputs' / str(index) / host.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(host, target)
            alias = Path('/run/agent/inputs') / str(index) / host.name
            self.inputs.append((target, alias))
            self.path_map[host] = alias

    def _view(self, host, alias, writable):
        copy = self.staging / 'views' / str(len(self.views))
        excluded = tuple(path.relative_to(host) for path in self.policy.sensitive_paths
                         if path.is_relative_to(host))
        if path_denied(host, self.policy.sensitive_paths):
            raise IsolationError('Authorized view intersects a sensitive configuration path')
        baseline = _copy_view(host, copy, excluded)
        self.views.append((host, copy, alias, writable, baseline, excluded))
        self.path_map[host] = alias

    def map_path(self, host):
        path = Path(host).absolute()
        for original, alias in sorted(self.path_map.items(), key=lambda item: len(item[0].parts), reverse=True):
            try:
                return alias / path.relative_to(original)
            except ValueError:
                pass
        raise IsolationError('Path is not authorized for this task')

    def _runtime(self):
        executable = self.executable
        parts = executable.parts
        roots = []
        if 'node_modules' in parts:
            idx = parts.index('node_modules') + 1
            end = idx + (2 if parts[idx].startswith('@') else 1)
            package = Path(*parts[:end])
            roots.append(package)
            if package.name == 'codex' and package.parent.name == '@openai':
                roots.extend(path for path in package.parent.glob('codex-*') if path.is_dir())
        elif executable.is_relative_to('/usr') or executable.is_relative_to('/bin'):
            return []
        else:
            # A single native/script executable does not grant its entire HOME.
            roots.append(executable)
        for root in roots:
            _validate_mount_tree(root, runtime=True, sensitive_paths=self.policy.sensitive_paths)
        return roots

    def _auth(self):
        configured = self.cfg.get('AGENT_AUTH_FILES', {})
        if self.policy.backend in configured:
            selected = configured[self.policy.backend]
            if not isinstance(selected, dict):
                raise IsolationError('AGENT_AUTH_FILES entries must map relative HOME targets to files')
        else:
            home_keys = {'codex': ('CODEX_HOME', '.codex'),
                         'claudecode': ('CLAUDE_CONFIG_DIR', '.claude'),
                         'grok': ('GROK_HOME', '.grok')}
            key, default = home_keys[self.policy.backend]
            native_home = Path(self.cfg.get(key) or os.environ.get(key) or Path.home() / default)
            selected = {rel: native_home / Path(rel).name
                        for rel in _DEFAULT_AUTH[self.policy.backend]
                        if (native_home / Path(rel).name).is_file()}
        mounts = []
        for relative, original in selected.items():
            rel = Path(relative)
            if (rel.is_absolute() or not rel.parts or '..' in rel.parts
                    or str(rel) not in _DEFAULT_AUTH[self.policy.backend]):
                raise IsolationError('Only selected CLI authentication filenames may be mounted')
            host = no_symlinks(original)
            if not host.is_file():
                raise IsolationError('Selected CLI authentication file is unavailable')
            mounts.append((host, Path('/home/agent') / rel))
        return mounts

    def wrap(self, argv):
        if not argv:
            raise IsolationError('Empty sandbox command')
        bwrap = shutil.which('bwrap')
        if not bwrap:
            raise IsolationError('bubblewrap is required; native execution is disabled')
        _validate_mount_tree(self.host_work, sensitive_paths=self.policy.sensitive_paths)
        command = [bwrap, '--unshare-all', '--die-with-parent', '--new-session',
                   '--cap-drop', 'ALL', '--clearenv']
        for runtime_root in SYSTEM_RUNTIME_ROOTS:
            command += ['--ro-bind', str(runtime_root), str(runtime_root)]
        for name in ('bin', 'sbin', 'lib', 'lib64'):
            host = Path('/') / name
            if host.is_symlink():
                command += ['--symlink', os.readlink(host), str(host)]
            elif host.exists():
                command += ['--ro-bind', str(host), str(host)]
        # A deployment or HOME can live beneath a trusted system runtime bind.
        # Hide its host spelling before exposing only explicitly authorized views.
        for hidden in sorted({self.policy.root, Path.home()}, key=lambda p: len(p.parts)):
            if hidden == Path('/') or hidden in (Path('/usr'), Path('/bin'), Path('/lib'), Path('/lib64')):
                raise IsolationError('Project/HOME overlaps an essential system runtime root')
            command += ['--tmpfs', str(hidden)]
        command += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
                    '--dir', '/home', '--dir', '/home/agent', '--dir', '/run/agent',
                    '--dir', '/etc']
        for relative in _DEFAULT_AUTH[self.policy.backend]:
            command += ['--dir', str(Path('/home/agent') / Path(relative).parent)]
        for path in ('/etc/ssl/certs', '/etc/ca-certificates', '/etc/ld.so.cache'):
            if Path(path).exists():
                command += ['--ro-bind', path, path]
        for runtime in self._runtime():
            command += ['--ro-bind', str(runtime), str(runtime)]
        denied_file = self.staging / 'denied-file'
        if not denied_file.exists():
            denied_file.touch(mode=0o000)
        for path in self.policy.sensitive_paths:
            if path.is_file() and not any(path.is_relative_to(base) for base in (self.policy.root, Path.home())):
                command += ['--ro-bind', str(denied_file), str(path)]
        for host, copied, alias, writable, baseline, excluded in self.views:
            command += ['--bind' if writable else '--ro-bind', str(copied), str(alias)]
        command += ['--bind', str(self.host_work), str(self.work_path)]
        # Keep the conventional work alias available to result/schema paths.
        if self.work_path != Path('/tmp/agent-work'):
            command += ['--symlink', str(self.work_path), '/tmp/agent-work']
        for host, alias in self.inputs + self._auth():
            command += ['--ro-bind', str(host), str(alias)]
        relay = Path(__file__).with_name('agent_relay.py')
        command += ['--ro-bind', str(relay), '/run/agent/relay.py',
                    '--ro-bind', str(self.socket_path), '/run/agent/network.sock']
        env = {'HOME': '/home/agent',
               'XDG_CONFIG_HOME': '/home/agent/.config',
               'XDG_CACHE_HOME': '/home/agent/.cache',
               'PATH': str(self.executable.parent) + ':/usr/local/bin:/usr/bin:/bin',
               'LANG': 'C.UTF-8', 'TMPDIR': '/tmp',
               'TZ': str(self.cfg.get('TIMEZONE', 'Asia/Shanghai')),
               'HTTP_PROXY': 'http://127.0.0.1:18080', 'HTTPS_PROXY': 'http://127.0.0.1:18080',
               'http_proxy': 'http://127.0.0.1:18080', 'https_proxy': 'http://127.0.0.1:18080',
               'ALL_PROXY': 'http://127.0.0.1:18080', 'NO_PROXY': '', 'no_proxy': ''}
        home_env = {'codex': ('CODEX_HOME', '.codex'),
                    'claudecode': ('CLAUDE_CONFIG_DIR', '.claude'),
                    'grok': ('GROK_HOME', '.grok')}
        home_key, home_directory = home_env[self.policy.backend]
        env[home_key] = '/home/agent/' + home_directory
        for key in _ENV_KEYS[self.policy.backend]:
            value = self.cfg.get(key, os.environ.get(key))
            if value:
                env[key] = str(value)
        for key, value in env.items():
            command += ['--setenv', key, value]
        actual = list(map(str, argv))
        if Path(actual[0]).resolve() == self.executable:
            actual[0] = str(self.executable)
        command += ['--chdir', str(self.work_path), '--', '/usr/bin/python3',
                    '/run/agent/relay.py', '/run/agent/network.sock', '18080', *actual]
        return command

    def publish(self):
        if self._published:
            raise IsolationError('Task changes have already been published')
        changes = []
        for host, copied, alias, writable, baseline, excluded in self.views:
            if not writable:
                continue
            current = _files(copied, strict=True, excluded=excluded)
            # Validate all host paths before writing any file.
            for rel in set(baseline) | set(current):
                target = no_symlinks(host / rel)
                if target.is_file() and target.stat().st_nlink != 1:
                    raise IsolationError('Host publication target became a hardlink')
                actual = _snapshot(target) if target.is_file() else None
                if actual != baseline.get(rel):
                    raise IsolationError('Host source changed while the agent was running')
            changes.append((host, copied, baseline, current))
        for host, copied, baseline, current in changes:
            for rel in baseline.keys() - current.keys():
                (host / rel).unlink()
            for rel, snapshot in current.items():
                if baseline.get(rel) == snapshot:
                    continue
                target = host / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(copied / rel, target)
                target.chmod(snapshot[1])
        self._published = True


@contextmanager
def isolated(policy, cfg, work, executable):
    if not shutil.which('bwrap'):
        raise IsolationError('bubblewrap is required; native execution is disabled')
    # Short AF_UNIX pathname even when PROJECT_ROOT is deeply nested.
    with tempfile.TemporaryDirectory(prefix='agent-isolation-') as temporary:
        staging = Path(temporary)
        socket_path = staging / 'network.sock'
        sandbox = Sandbox(policy, cfg, work, executable, staging, socket_path)
        with NetworkProxy(socket_path, policy.network_allowlist):
            yield sandbox
