"""Explicit backend registry. Never discover an unselected CLI or credential."""
from .codex import CodexBackend
from .grok import GrokBackend
from .claudecode import ClaudeCodeBackend

BACKENDS = {'codex': (CodexBackend, 'CODEX_BIN', 'CODEX_MODEL', 'codex'),
            'grok': (GrokBackend, 'GROK_BIN', 'GROK_MODEL', 'grok'),
            'claudecode': (ClaudeCodeBackend, 'CLAUDECODE_BIN', 'CLAUDECODE_MODEL', 'claude')}


def get_backend(cfg):
    name = cfg.get('AGENT_BACKEND', 'codex')
    if name not in BACKENDS:
        raise ValueError('AGENT_BACKEND must be claudecode, codex or grok')
    cls, binary_key, model_key, default = BACKENDS[name]
    binary = cfg.get(binary_key) or default
    return cls(binary, cfg.get(model_key) or '')
