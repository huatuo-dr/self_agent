"""Read only selected native settings needed for authentication compatibility.

Native hooks, plugins, tool permissions and sandbox settings are never imported.
The service config has precedence over process environment and native defaults.
"""
import json
import os
from pathlib import Path
import stat

MANAGED_FILES = {
    'codex': (Path('/etc/codex/requirements.toml'),),
    'grok': (Path('/etc/grok/requirements.toml'),),
    'claudecode': (Path('/etc/claude-code/managed-settings.json'),),
}
CLAUDE_ENV = ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
              'CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_MODEL',
              'ANTHROPIC_DEFAULT_HAIKU_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL',
              'ANTHROPIC_DEFAULT_OPUS_MODEL')


def _settings(path):
    if not path.exists() and not path.is_symlink():
        return {}
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('所选 CLI 设置文件不能经过符号链接')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
                raise ValueError('所选 CLI 设置文件无效')
            data = json.loads(stream.read(1024 * 1024 + 1))
        if not isinstance(data, dict):
            raise ValueError('所选 CLI 设置必须是 JSON 对象')
        return data
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError('无法安全读取所选 CLI 设置') from None


def native_config(cfg):
    result = dict(cfg)
    backend = cfg.get('AGENT_BACKEND', 'codex')
    for path in MANAGED_FILES.get(backend, ()):
        if path.exists() or path.is_symlink():
            # Do not evade managed policy by hiding its file in the namespace.
            # A managed policy requires a separately supported integration.
            raise ValueError('所选 CLI 存在受管配置，无法确认厂商沙箱可关闭')
    if backend != 'claudecode':
        return result
    configured = cfg.get('CLAUDECODE_SETTINGS_FILE')
    home = cfg.get('CLAUDE_CONFIG_DIR') or os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude'
    path = Path(configured) if configured else Path(home) / 'settings.json'
    data = _settings(path.expanduser().absolute())
    values = data.get('env', {})
    if not isinstance(values, dict):
        raise ValueError('Claude Code env 设置无效')
    for key in CLAUDE_ENV:
        value = cfg.get(key) or os.environ.get(key) or values.get(key)
        if value:
            if not isinstance(value, str):
                raise ValueError('Claude Code 认证或模型环境设置必须为字符串')
            result[key] = value
    if not result.get('CLAUDECODE_MODEL'):
        model = result.get('ANTHROPIC_MODEL') or data.get('model')
        if isinstance(model, str):
            result['CLAUDECODE_MODEL'] = model
    return result
