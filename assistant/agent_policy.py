"""Trusted task capabilities shared by every model backend.

Network entries are exact host/port pairs, never wildcard domains. Additional
entries use AGENT_NETWORK_ALLOWLIST (comma separated host[:port]). Only the
selected backend's endpoint configuration participates in the policy.
"""
from dataclasses import dataclass
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

TASK_KINDS = frozenset(('chat', 'classify', 'auto_query', 'report',
                        'develop_discuss', 'develop_execute'))
BACKEND_ENDPOINTS = {
    'codex': ('api.openai.com', 'chatgpt.com', 'auth.openai.com'),
    'claudecode': ('api.anthropic.com', 'platform.claude.com', 'console.anthropic.com'),
    'grok': ('api.x.ai', 'cli-chat-proxy.grok.com', 'auth.x.ai', 'accounts.x.ai'),
}
ENDPOINT_KEYS = {
    'codex': ('CODEX_BASE_URL', 'OPENAI_BASE_URL'),
    'claudecode': ('CLAUDECODE_BASE_URL', 'ANTHROPIC_BASE_URL'),
    'grok': ('GROK_BASE_URL', 'XAI_BASE_URL', 'GROK_API_BASE'),
}


def protected(relative):
    """Paths omitted from model source views and forbidden during publication."""
    return any(part in ('.local', '.project', '.git', '.venv', 'node_modules',
                        '__pycache__', '.cache') or part == '.env' or part.startswith('.env.')
               for part in Path(relative).parts)


def no_symlinks(path):
    path = Path(path).absolute()
    if '..' in path.parts:
        raise ValueError('Isolation paths must not contain parent traversal')
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('Isolation paths must not contain symbolic links')
    return path


def path_denied(path, sensitive_paths):
    return any(path == denied or path.is_relative_to(denied) for denied in sensitive_paths)


def _sensitive(path, root, sensitive_paths=()):
    # Explicit grants under .project are permitted, except the credential store.
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts
    return (path_denied(path, sensitive_paths) or '.local' in parts or '.git' in parts or
            any(p == '.env' or p.startswith('.env.') for p in parts) or
            ('credential' in parts and '.project' in parts))


def _endpoint(value):
    value = str(value).strip()
    parsed = urlsplit(value if '://' in value else 'https://' + value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Invalid agent network endpoint')
    try:
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    except ValueError:
        raise ValueError('Invalid agent network port') from None
    if not 0 < port < 65536:
        raise ValueError('Invalid agent network port')
    return parsed.hostname.lower().rstrip('.'), port


@dataclass(frozen=True)
class AgentPolicy:
    kind: str
    root: Path
    writable_roots: tuple
    input_files: tuple
    source_readable: bool
    source_writable: bool
    approval_ref: str | None
    backend: str
    network_allowlist: tuple
    sensitive_paths: tuple = ()
    read_only_dirs: tuple = ()


def policy_for(cfg, kind, root, writable_roots=(), input_files=(), approval_ref=None, read_only_dirs=()):
    if kind not in TASK_KINDS:
        raise ValueError('Unknown agent task kind')
    root = no_symlinks(root)
    if not root.is_dir():
        raise ValueError('Agent project root must be a directory')
    if kind == 'develop_execute' and (not isinstance(approval_ref, str) or not approval_ref.strip()):
        raise ValueError('develop_execute requires a server-verified approval reference')
    backend = cfg.get('AGENT_BACKEND', 'codex')
    if backend not in BACKEND_ENDPOINTS:
        raise ValueError('Unknown agent backend')
    if writable_roots and kind in ('classify', 'auto_query', 'develop_discuss'):
        raise ValueError('Read-only tasks cannot receive writable directory grants')
    profile = Path(cfg.get('OWNER_PROFILE_PATH') or '.local/owner-profile.json')
    profile = profile if profile.is_absolute() else root / profile
    sensitive_paths = tuple(dict.fromkeys((profile.absolute(), profile.resolve())))
    grants = []
    for item in writable_roots:
        path = no_symlinks(item)
        if path == root and kind == 'develop_execute':
            continue  # Source is copied separately; never mount the live runtime.
        try:
            rel = path.relative_to(root / '.project')
        except ValueError:
            raise ValueError('Writable grants must name a task directory under .project') from None
        if not rel.parts or rel.parts == ('reports',) or _sensitive(path, root, sensitive_paths):
            raise ValueError('Broad or sensitive writable grant rejected')
        if path.exists() and not path.is_dir():
            raise ValueError('Writable grants must be directories')
        grants.append(path)
    inputs = []
    for item in input_files:
        path = no_symlinks(item)
        if _sensitive(path, root, sensitive_paths) or not path.is_file() or path.stat().st_nlink != 1:
            raise ValueError('Input must be an ordinary nonsensitive file')
        inputs.append(path)
    readonly = []
    if read_only_dirs and kind not in ('chat', 'report'):
        raise ValueError('Skill directories only belong to ordinary tasks')
    temporary_root = Path(tempfile.gettempdir()).resolve()
    for item in read_only_dirs:
        path = no_symlinks(item)
        if (path == temporary_root or not path.is_relative_to(temporary_root)
                or path.is_relative_to(root) or root.is_relative_to(path)
                or path.is_relative_to(Path.home()) or Path.home().is_relative_to(path)
                or _sensitive(path, root, sensitive_paths) or not path.is_dir()):
            raise ValueError('Read-only directories must be dedicated temporary inputs')
        readonly.append(path)
    network = {(host, 443) for host in BACKEND_ENDPOINTS[backend]}
    for key in ENDPOINT_KEYS[backend]:
        if cfg.get(key) or os.environ.get(key):
            network.add(_endpoint(cfg.get(key) or os.environ[key]))
    configured = cfg.get('AGENT_NETWORK_ALLOWLIST', '')
    entries = configured.split(',') if isinstance(configured, str) else configured
    for entry in entries:
        if str(entry).strip():
            network.add(_endpoint(entry))
    return AgentPolicy(kind, root, tuple(dict.fromkeys(grants)),
                       tuple(dict.fromkeys(inputs)), kind.startswith('develop_'),
                       kind == 'develop_execute', approval_ref, backend,
                       tuple(sorted(network)), sensitive_paths, tuple(dict.fromkeys(readonly)))
