"""Small request/result contract shared by local agent CLI adapters."""

from dataclasses import dataclass, field
import json
import os
import stat
from pathlib import Path
import re


class AgentError(RuntimeError):
    """Safe user-facing failure; never includes raw CLI output or credentials."""


@dataclass(frozen=True)
class RunRequest:
    task_kind: str
    prompt: str
    output_schema: dict | None = None
    input_files: tuple = ()
    images: tuple = ()
    writable_roots: tuple = ()
    approval_ref: str | None = None
    timeout: float = 90
    on_event: object = None
    read_only_dirs: tuple = ()


@dataclass(frozen=True)
class RunResult:
    text: str
    payload: object = None
    artifacts: tuple = ()


@dataclass(frozen=True)
class Invocation:
    args: list
    stdin: str | None
    schema: dict | None = None
    output_path: Path | None = None


def validate_payload(value, schema, depth=0):
    """Validate the JSON Schema subset used by our service, not CLI claims."""
    if depth > 40 or not isinstance(schema, dict):
        raise AgentError('AI 输出 schema 无效')
    for key, exact in (('anyOf', False), ('oneOf', True)):
        if key in schema:
            matches = 0
            for candidate in schema[key]:
                try:
                    validate_payload(value, candidate, depth + 1)
                    matches += 1
                except AgentError:
                    pass
            if not matches or (exact and matches != 1):
                raise AgentError('AI 输出不符合 schema 分支')
    types = {'object': lambda x: isinstance(x, dict), 'array': lambda x: isinstance(x, list),
             'string': lambda x: isinstance(x, str), 'boolean': lambda x: type(x) is bool,
             'integer': lambda x: type(x) is int,
             'number': lambda x: type(x) in (int, float), 'null': lambda x: x is None}
    expected = schema.get('type')
    if expected is not None:
        expected = expected if isinstance(expected, list) else [expected]
        if not any(t in types and types[t](value) for t in expected):
            raise AgentError('AI 输出字段类型无效')
    if 'enum' in schema and value not in schema['enum']:
        raise AgentError('AI 输出枚举值无效')
    if 'const' in schema and value != schema['const']:
        raise AgentError('AI 输出固定值无效')
    if isinstance(value, dict):
        if any(k not in value for k in schema.get('required', [])):
            raise AgentError('AI 输出缺少必要字段')
        properties = schema.get('properties', {})
        additional = schema.get('additionalProperties', True)
        for key, item in value.items():
            child = properties.get(key)
            if child is None:
                if additional is False:
                    raise AgentError('AI 输出包含未知字段')
                child = additional if isinstance(additional, dict) else None
            if child is not None:
                validate_payload(item, child, depth + 1)
    if isinstance(value, list):
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', 1000000):
            raise AgentError('AI 输出数组长度无效')
        if 'items' in schema:
            for item in value:
                validate_payload(item, schema['items'], depth + 1)
    if isinstance(value, str):
        if not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', 10000000):
            raise AgentError('AI 输出文本长度无效')
        if 'pattern' in schema and not re.search(schema['pattern'], value):
            raise AgentError('AI 输出文本格式无效')
    if type(value) in (int, float):
        if value < schema.get('minimum', float('-inf')) or value > schema.get('maximum', float('inf')):
            raise AgentError('AI 输出数值越界')


def result_object(stdout):
    try:
        value = json.loads(stdout)
    except (ValueError, TypeError):
        value = None
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and (event.get('type') == 'result' or
                    'stopReason' in event and 'num_turns' in event):
                value = event
        if value is None:
            raise AgentError('AI 工具未返回有效的最终结果事件') from None
    if not isinstance(value, dict):
        raise AgentError('AI 工具结果格式无效')
    return value


def read_result(path, limit=8 * 1024 * 1024):
    """AI-created output is untrusted even after its process has exited."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
                raise AgentError('AI 最终结果不是有效的普通文件')
            content = source.read(limit + 1)
        if len(content) > limit:
            raise AgentError('AI 最终结果超过大小限制')
        return content.decode('utf-8')
    except (OSError, UnicodeError):
        raise AgentError('AI 最终结果文件不可安全读取') from None


def final_result(text, schema=None, payload=None):
    if schema is not None:
        if payload is None:
            try:
                payload = json.loads(text)
            except (ValueError, TypeError):
                raise AgentError('AI 未返回有效的结构化结果') from None
        validate_payload(payload, schema)
        return RunResult(json.dumps(payload, ensure_ascii=False), payload)
    if not isinstance(text, str) or not text.strip():
        raise AgentError('AI 未返回最终答复')
    return RunResult(text.strip())


class Backend:
    def __init__(self, binary, model=''):
        self.binary, self.model = binary, model

    def event(self, raw):
        """Content-free events compatible with the existing progress counters."""
        if not isinstance(raw, dict):
            return None
        if raw.get('type') == 'item.completed':
            item = raw.get('item')
            kind = item.get('type') if isinstance(item, dict) else None
            if kind in ('command_execution', 'file_change', 'web_search', 'mcp_tool_call', 'agent_message'):
                return {'type': 'item.completed', 'item': {'type': kind}}
        if raw.get('type') in ('result', 'message_stop'):
            return {'type': 'item.completed', 'item': {'type': 'agent_message'}}
        return None
