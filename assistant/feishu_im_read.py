"""A small owner-identity IM read gateway outside the model sandbox."""

import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from assistant.settings import get_timezone


SOCKET_NAME = 'feishu-im-read.sock'
MAX_RESPONSE_BYTES = 2_000_000


def command_for(request, cli_bin, cfg=None):
    """Build only group and thread IM read operations."""
    if not isinstance(request, dict):
        raise ValueError('Request must be a JSON object')
    operation = request.get('operation')
    common = {'operation', 'page_size', 'page_token'}
    if operation == 'search':
        if set(request) - (common | {'query'}):
            raise ValueError('Unsupported search parameter')
        query = request.get('query')
        if (not isinstance(query, str) or not query.strip() or len(query) > 64
                or any(ord(char) < 32 for char in query)):
            raise ValueError('Invalid group query')
        args = [cli_bin, 'im', '+chat-search', '--as', 'user', '--query', query]
    elif operation in ('messages', 'threads'):
        allowed = common | ({'chat_id', 'start', 'end', 'order'} if operation == 'messages'
                            else {'thread_id', 'order'})
        if set(request) - allowed:
            raise ValueError('Unsupported message parameter')
        id_key = 'chat_id' if operation == 'messages' else 'thread_id'
        container_id = request.get(id_key)
        prefix = 'oc_' if operation == 'messages' else 'omt_'
        if not isinstance(container_id, str) or not re.fullmatch(prefix + r'[A-Za-z0-9_]+', container_id):
            raise ValueError('Invalid ' + id_key)
        params = {'container_id_type': 'chat' if operation == 'messages' else 'thread',
                  'container_id': container_id,
                  'card_msg_content_type': 'user_card_content'}
        for field in ('start', 'end') if operation == 'messages' else ():
            value = request.get(field)
            if value is not None:
                if (not isinstance(value, str) or len(value) > 40 or
                        not re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:T[0-9:+.Z-]+)?', value)):
                    raise ValueError(f'Invalid {field} time')
                try:
                    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                except ValueError as error:
                    raise ValueError(f'Invalid {field} time') from error
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=get_timezone(cfg))
                params[field + '_time'] = str(int(parsed.timestamp()))
        order = request.get('order', 'asc')
        if order not in ('asc', 'desc'):
            raise ValueError('Invalid message order')
        params['sort_type'] = 'ByCreateTimeAsc' if order == 'asc' else 'ByCreateTimeDesc'
    else:
        raise ValueError('Unsupported IM read operation')
    size = request.get('page_size', 50)
    if type(size) is not int or not 1 <= size <= 50:
        raise ValueError('Invalid page size')
    token = request.get('page_token')
    if token is not None:
        if (not isinstance(token, str) or not token or len(token) > 1024
                or any(ord(char) < 33 for char in token)):
            raise ValueError('Invalid page token')
    if operation == 'search':
        args += ['--page-size', str(size)]
        if token:
            args += ['--page-token', token]
        return args + ['--format', 'json']
    params['page_size'] = size
    if token:
        params['page_token'] = token
    # The shortcut checks P2P and reaction scopes even for a group with --no-reactions.
    # The raw read endpoint only requires the scopes relevant to this group request.
    return [cli_bin, 'api', 'GET', '/open-apis/im/v1/messages', '--as', 'user',
            '--params', json.dumps(params), '--format', 'json']


def execute(request, cli_bin, owner_open_id, cfg=None):
    try:
        args = command_for(request, cli_bin, cfg)
    except ValueError as error:
        return {'ok': False, 'error': {'type': 'invalid_request', 'message': str(error)}}
    try:
        status = subprocess.run([cli_bin, 'auth', 'status', '--json'], capture_output=True,
                                text=True, timeout=15)
        identity = (json.loads(status.stdout).get('identities') or {}).get('user') or {}
        if status.returncode or identity.get('openId') != owner_open_id:
            return {'ok': False, 'error': {'type': 'owner_identity_mismatch'}}
        scopes = identity.get('scope') or []
        if isinstance(scopes, str):
            scopes = scopes.split()
        required = ['im:chat:read'] if request['operation'] == 'search' else [
            'im:message:readonly', 'im:message.group_msg:get_as_user']
        missing = [scope for scope in required if scope not in scopes]
        if missing:
            return {'ok': False, 'error': {'type': 'authorization', 'subtype': 'missing_scope',
                                           'missing_scopes': missing}}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {'ok': False, 'error': {'type': 'owner_identity_unavailable'}}
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=40,
                                env={**os.environ, 'LARKSUITE_CLI_NO_UPDATE_NOTIFIER': '1',
                                     'LARKSUITE_CLI_NO_SKILLS_NOTIFIER': '1'})
    except (OSError, subprocess.TimeoutExpired):
        return {'ok': False, 'error': {'type': 'cli_unavailable'}}
    if result.returncode == 0:
        if len(result.stdout.encode()) > MAX_RESPONSE_BYTES:
            return {'ok': False, 'error': {'type': 'response_too_large'}}
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {'ok': False, 'error': {'type': 'invalid_cli_response'}}
        return data if isinstance(data, dict) else {'ok': False, 'error': {'type': 'invalid_cli_response'}}
    # The CLI may prefix stderr with status text. Keep only a few safe diagnostic fields.
    raw = result.stderr[result.stderr.find('{'):]
    try:
        error = json.loads(raw).get('error') or {}
    except (json.JSONDecodeError, AttributeError):
        error = {}
    return {'ok': False, 'error': {key: error[key] for key in
            ('type', 'subtype', 'code', 'missing_scopes') if key in error} or
            {'type': 'cli_error', 'exit_code': result.returncode}}


async def serve(path: Path, cli_bin: str, owner_open_id: str, cfg=None):
    """Accept local same-user read requests; never expose CLI credentials."""
    if path.is_symlink():
        raise ValueError('IM read socket must not be a symlink')
    if path.exists():
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise ValueError('IM read socket path is occupied')
        path.unlink()

    async def handle(reader, writer):
        try:
            raw = await asyncio.wait_for(reader.readline(), 5)
            if not raw or len(raw) > 4096:
                raise ValueError('Invalid request size')
            request = json.loads(raw)
            response = await asyncio.to_thread(execute, request, cli_bin, owner_open_id, cfg)
        except (ValueError, UnicodeError, asyncio.TimeoutError, asyncio.LimitOverrunError):
            response = {'ok': False, 'error': {'type': 'invalid_request'}}
        try:
            writer.write((json.dumps(response, ensure_ascii=False) + '\n').encode())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=str(path), limit=8192)
    path.chmod(0o600)
    try:
        async with server:
            await server.serve_forever()
    finally:
        server.close()
        await server.wait_closed()
        if path.exists() and stat.S_ISSOCK(path.lstat().st_mode):
            path.unlink()
