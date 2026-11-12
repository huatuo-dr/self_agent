"""Feishu transport for owner-controlled /auto replies."""

import asyncio
import hashlib
import json
import re
import time

from assistant.auto_reply import AdapterUnavailable


def _data(payload):
    if not isinstance(payload, dict) or payload.get('ok') is not True or not isinstance(payload.get('data'), dict):
        raise AdapterUnavailable('飞书接口未确认成功')
    return payload['data']


def _message_id(payload):
    data = _data(payload)
    item = data.get('message') if isinstance(data.get('message'), dict) else data
    mid = item.get('message_id')
    if not isinstance(mid, str) or not re.fullmatch(r'om_[A-Za-z0-9_]+', mid):
        raise AdapterUnavailable('发送回执缺少有效消息 ID')
    return mid


def _created_since(item, since):
    if not isinstance(item, dict):
        return False
    try:
        return int(item.get('create_time')) >= since * 1000
    except (TypeError, ValueError):
        return False


def normalize_message(item, chat):
    """Fail closed on identities, body and mentions that cannot be established."""
    if not isinstance(item, dict) or item.get('deleted') is True:
        return None
    mid, chat_id = item.get('message_id'), chat.get('chat_id')
    if not isinstance(mid, str) or not re.fullmatch(r'om_[A-Za-z0-9_]+', mid):
        return None
    if item.get('chat_id') not in (None, chat_id):
        return None
    sender = item.get('sender')
    sender = sender if isinstance(sender, dict) else {}
    identity = sender.get('id')
    if isinstance(identity, dict):
        identity = identity.get('open_id')
    name = sender.get('name') or sender.get('sender_name')
    if name == identity:
        name = None
    kind = item.get('msg_type') or item.get('message_type')
    raw = item.get('body') if isinstance(item.get('body'), dict) else item.get('content')
    raw = raw.get('content') if isinstance(raw, dict) else raw
    try:
        content = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        content = None
    body = content.get('text') if isinstance(content, dict) and kind == 'text' else None
    mentions = item.get('mentions', [])
    return {'message_id': mid, 'chat_id': chat_id,
            'chat_type': 'p2p' if chat.get('chat_mode') == 'p2p' else 'group',
            'sender_id': identity, 'sender_type': sender.get('sender_type'),
            'sender_name': name, 'message_type': kind, 'body': body,
            'mentions': mentions, 'thread_id': item.get('thread_id'),
            'root_id': item.get('root_id'), 'source_verified': True,
            'sender_verified': isinstance(identity, str) and bool(re.fullmatch(r'ou_[A-Za-z0-9_]+', identity)),
            'body_verified': isinstance(body, str) and bool(body.strip()) and len(body) <= 6000}


class FeishuAutoAdapter:
    """One bounded chat/message page per scan; cursor is committed by AutoReply."""

    def __init__(self, cfg, run):
        self.cfg, self.run = cfg, run
        self.owner_id = cfg['FEISHU_OWNER_OPEN_ID']
        self.owner_chat = cfg['FEISHU_OWNER_CHAT_ID']
        self.cli = cfg['LARK_CLI_BIN']
        profile = cfg.get('OWNER_PROFILE') or {}
        owner_name = profile.get('nickname') or profile.get('name')
        self.names = {self.owner_id: owner_name} if isinstance(owner_name, str) and owner_name.strip() else {}
        self.last_hot = 0
        self.thread_offsets = {}

    async def _call(self, args, timeout=40):
        if args[:2] == ['im', '+chat-list']:
            stage = '会话列表'
        elif args[:3] == ['api', 'GET', '/open-apis/im/v1/messages']:
            stage = '消息列表'
        elif args[:2] == ['contact', '+search-user']:
            stage = '发送者姓名查询'
        elif args[:2] == ['auth', 'status']:
            stage = '用户授权检查'
        else:
            stage = '飞书调用'
        try:
            return json.loads(await self.run([self.cli, *args, '--format', 'json'], timeout))
        except (RuntimeError, OSError, asyncio.TimeoutError, json.JSONDecodeError) as error:
            raise AdapterUnavailable(stage + '失败（' + type(error).__name__ + '）') from error

    async def _verify_user(self, scopes):
        status = json.loads(await self.run([self.cli, 'auth', 'status', '--json', '--verify'], 20))
        user = (status.get('identities') or {}).get('user') or {}
        granted = user.get('scope') or []
        if isinstance(granted, str):
            granted = granted.split()
        if (user.get('openId') != self.owner_id or user.get('verified') is not True
                or user.get('tokenStatus') != 'valid'):
            raise AdapterUnavailable('主人用户身份未核实')
        missing = sorted(set(scopes) - set(granted))
        if missing:
            raise AdapterUnavailable('缺少用户权限：' + '、'.join(missing))
        return set(granted)

    async def preflight(self):
        scopes = await self._verify_user(('im:chat:read', 'im:message:readonly',
                                          'im:message.group_msg:get_as_user',
                                          'im:message.p2p_msg:get_as_user',
                                          'im:message', 'im:message.send_as_user',
                                          'contact:user:search'))
        await self.discover(json.dumps({'since': int(time.time())}))
        return {'reaction_ready': 'im:message.reactions:write_only' in scopes}

    async def _enrich_names(self, messages):
        missing = sorted({m['sender_id'] for m in messages if m['sender_verified']
                          and not m.get('sender_name') and m['sender_id'] not in self.names})
        if missing:
            data = _data(await self._call(['contact', '+search-user', '--as', 'user',
                                           '--user-ids', ','.join(missing)]))
            users = data.get('users')
            if not isinstance(users, list):
                raise AdapterUnavailable('发送者姓名查询结果不完整')
            for user in users:
                if not isinstance(user, dict) or user.get('open_id') not in missing:
                    continue
                name = user.get('localized_name')
                if isinstance(name, str) and name.strip() and name != user['open_id']:
                    self.names[user['open_id']] = name
        for message in messages:
            if not message.get('sender_name'):
                message['sender_name'] = self.names.get(message['sender_id'])
        return messages

    async def discover_hot(self, cursor, on_messages=None):
        """Check recently active chats first; the stable full sweep still supplies coverage."""
        if time.monotonic() - self.last_hot < 10:
            return []
        try:
            since = int((json.loads(cursor) if cursor else {}).get('since', time.time()))
            if since < 0:
                raise ValueError
        except (ValueError, TypeError) as error:
            raise AdapterUnavailable('自动收取游标无效') from error
        chats = []
        seen_chats = set()
        for chat_type in ('group', 'p2p'):
            page = _data(await self._call(['im', '+chat-list', '--as', 'user',
                                           '--types', chat_type, '--sort', 'active_time',
                                           '--page-size', '5']))
            recent = page.get('chats')
            if not isinstance(recent, list) or len(recent) > 5:
                raise AdapterUnavailable('活跃会话列表不完整')
            for chat in recent:
                if isinstance(chat, dict) and chat.get('chat_id') not in seen_chats:
                    chats.append(chat)
                    seen_chats.add(chat.get('chat_id'))
        messages = []
        async def deliver(batch):
            if not batch:
                return
            await self._enrich_names(batch)
            messages.extend(batch)
            if on_messages is not None:
                await on_messages(batch)
        for chat in chats:
            chat_id = chat.get('chat_id') if isinstance(chat, dict) else None
            if not isinstance(chat_id, str) or not re.fullmatch(r'oc_[A-Za-z0-9_]+', chat_id):
                raise AdapterUnavailable('活跃会话身份无法核实')
            if chat_id == self.owner_chat:
                continue
            group = chat.get('chat_mode') in ('group', 'topic')
            params = {'container_id_type': 'chat', 'container_id': chat_id,
                      'end_time': str(int(time.time())),
                      'sort_type': 'ByCreateTimeDesc', 'page_size': 50}
            if not group:
                params['start_time'] = str(since)
            data = _data(await self._call(['api', 'GET', '/open-apis/im/v1/messages',
                                           '--as', 'user', '--params', json.dumps(params)]))
            items = data.get('items')
            if not isinstance(items, list) or len(items) > 50:
                raise AdapterUnavailable('活跃会话消息页不完整')
            await deliver([m for item in items if _created_since(item, since)
                           if (m := normalize_message(item, chat)) is not None])
            if not group:
                continue
            thread_ids = list(dict.fromkeys(item['thread_id'] for item in items
                              if isinstance(item, dict) and isinstance(item.get('thread_id'), str)
                              and re.fullmatch(r'omt_[A-Za-z0-9_]+', item['thread_id'])))
            if not thread_ids:
                continue
            offset = self.thread_offsets.get(chat_id, 0) % len(thread_ids)
            batch_size = min(2, len(thread_ids))
            self.thread_offsets[chat_id] = (offset + batch_size) % len(thread_ids)
            for index in range(batch_size):
                thread_id = thread_ids[(offset + index) % len(thread_ids)]
                thread_params = {'container_id_type': 'thread', 'container_id': thread_id,
                                 'sort_type': 'ByCreateTimeDesc', 'page_size': 50}
                thread_page = _data(await self._call(['api', 'GET', '/open-apis/im/v1/messages',
                                                     '--as', 'user', '--params', json.dumps(thread_params)]))
                replies = thread_page.get('items')
                if not isinstance(replies, list) or len(replies) > 50:
                    raise AdapterUnavailable('活跃话题消息页不完整')
                await deliver([m for item in replies if _created_since(item, since)
                               if (m := normalize_message(item, chat)) is not None])
        self.last_hot = time.monotonic()
        return messages

    async def discover(self, cursor):
        await self._verify_user(('im:chat:read', 'im:message:readonly',
                                 'im:message.group_msg:get_as_user',
                                 'im:message.p2p_msg:get_as_user'))
        try:
            state = json.loads(cursor) if cursor else {}
            if not isinstance(state, dict):
                raise ValueError
            since = int(state.get('since', time.time()))
            until = int(state.get('until') or time.time())
            offset = int(state.get('offset', 0))
            chat_token = state.get('chat_token') or ''
            message_token = state.get('message_token') or ''
            if (since < 0 or until < since or offset < 0 or offset > 100 or
                    not all(isinstance(x, str) and len(x) <= 1024 for x in (chat_token, message_token))):
                raise ValueError
        except (ValueError, TypeError) as error:
            raise AdapterUnavailable('自动收取游标无效') from error
        args = ['im', '+chat-list', '--as', 'user', '--types', 'p2p,group',
                '--sort', 'create_time', '--page-size', '100']
        if chat_token:
            args += ['--page-token', chat_token]
        page = _data(await self._call(args))
        chats = page.get('chats')
        if not isinstance(chats, list) or len(chats) > 100 or offset > len(chats):
            raise AdapterUnavailable('会话列表不完整')
        if offset == len(chats):
            if page.get('has_more') is True:
                next_token = page.get('page_token')
                if not isinstance(next_token, str) or not next_token or next_token == chat_token:
                    raise AdapterUnavailable('会话分页缺少游标')
                return [], json.dumps({'since': since, 'until': until, 'chat_token': next_token})
            return [], json.dumps({'since': max(since, until - 1)})
        chat = chats[offset]
        chat_id = chat.get('chat_id') if isinstance(chat, dict) else None
        if (not isinstance(chat_id, str) or not re.fullmatch(r'oc_[A-Za-z0-9_]+', chat_id)
                or chat.get('chat_mode') not in ('p2p', 'group', 'topic')):
            raise AdapterUnavailable('会话身份或类型无法核实')
        params = {'container_id_type': 'chat', 'container_id': chat_id,
                  'start_time': str(since), 'end_time': str(until),
                  'sort_type': 'ByCreateTimeAsc', 'page_size': 50}
        if message_token:
            params['page_token'] = message_token
        page = _data(await self._call(['api', 'GET', '/open-apis/im/v1/messages', '--as', 'user',
                                       '--params', json.dumps(params)]))
        items = page.get('items')
        if not isinstance(items, list) or len(items) > 50:
            raise AdapterUnavailable('消息页不完整')
        messages = [normalized for item in items
                    if (normalized := normalize_message(item, chat)) is not None]
        await self._enrich_names(messages)
        state = {'since': since, 'until': until, 'chat_token': chat_token, 'offset': offset}
        if page.get('has_more') is True:
            next_token = page.get('page_token')
            if not isinstance(next_token, str) or not next_token or next_token == message_token:
                raise AdapterUnavailable('消息分页缺少游标')
            state['message_token'] = next_token
        else:
            state['offset'] = offset + 1
        return messages, json.dumps(state)

    async def reply_as_owner(self, message, content):
        await self._verify_user(('im:message', 'im:message.send_as_user'))
        mid = message.get('message_id')
        if not isinstance(mid, str) or not re.fullmatch(r'om_[A-Za-z0-9_]+', mid):
            raise ValueError('原消息 ID 无效')
        key = hashlib.sha256(('auto:reply:' + mid).encode()).hexdigest()[:40]
        args = ['im', '+messages-reply', '--as', 'user', '--message-id', mid,
                '--text', content, '--idempotency-key', key]
        if message.get('reply_in_thread') is True:
            args.append('--reply-in-thread')
        return _message_id(await self._call(args))

    async def react_thinking(self, message_id):
        await self._verify_user(('im:message.reactions:write_only',))
        if not isinstance(message_id, str) or not re.fullmatch(r'om_[A-Za-z0-9_]+', message_id):
            raise ValueError('原消息 ID 无效')
        data = _data(await self._call([
            'im', 'reactions', 'create', '--as', 'user',
            '--params', json.dumps({'message_id': message_id}),
            '--data', json.dumps({'reaction_type': {'emoji_type': 'Status_PrivateMessage'}}),
        ], timeout=8))
        reaction_id = data.get('reaction_id')
        if not isinstance(reaction_id, str) or not reaction_id:
            raise AdapterUnavailable('表情回执缺少 ID')
        return reaction_id

    async def withdraw_reaction(self, message_id, reaction_id):
        await self._verify_user(('im:message.reactions:write_only',))
        if not isinstance(message_id, str) or not re.fullmatch(r'om_[A-Za-z0-9_]+', message_id):
            raise ValueError('原消息 ID 无效')
        # Feishu returns an opaque ID, which can contain base64 punctuation.
        if (not isinstance(reaction_id, str) or not reaction_id or len(reaction_id) > 4096
                or any(ord(char) < 32 for char in reaction_id)):
            raise ValueError('表情 ID 无效')
        await self._call([
            'im', 'reactions', 'delete', '--as', 'user',
            '--params', json.dumps({'message_id': message_id, 'reaction_id': reaction_id}),
        ], timeout=8)

    async def notify_owner(self, content):
        key = hashlib.sha256(('auto:notice:' + content).encode()).hexdigest()[:40]
        return _message_id(await self._call(['im', '+messages-send', '--as', 'bot',
            '--chat-id', self.owner_chat, '--text', content, '--idempotency-key', key]))
