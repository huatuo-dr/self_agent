from tests.test_auto_reply import FakeQuery
import asyncio
import json
import time
import unittest

from assistant.auto_live import FeishuAutoAdapter, normalize_message
from assistant.auto_reply import AdapterUnavailable, AutoReply, eligible
import sqlite3


CFG = {'FEISHU_OWNER_OPEN_ID': 'ou_owner', 'FEISHU_OWNER_CHAT_ID': 'oc_owner',
       'LARK_CLI_BIN': 'lark-cli'}


class FakeCLI:
    def __init__(self):
        self.calls = []
        self.fail = ''
        self.message_token = ''
        self.omit_sender_name = False
        self.self_mention = False
        self.drop_scope = ''
        self.thread_replies = False

    async def __call__(self, args, timeout):
        self.calls.append(args)
        if args[1:3] == ['auth', 'status']:
            scopes = ['im:chat:read', 'im:message:readonly',
                      'im:message.group_msg:get_as_user', 'im:message.p2p_msg:get_as_user',
                      'im:message', 'im:message.send_as_user', 'contact:user:search',
                      'im:message.reactions:write_only']
            return json.dumps({'identities': {'user': {'openId': 'ou_owner',
                'verified': True, 'tokenStatus': 'valid', 'scope':
                [scope for scope in scopes if scope != self.drop_scope]}}})
        if self.fail:
            raise RuntimeError('simulated timeout')
        if args[1:3] == ['contact', '+search-user']:
            ids = args[args.index('--user-ids') + 1].split(',')
            return json.dumps({'ok': True, 'data': {'users': [
                {'open_id': uid, 'localized_name': '用户甲' if uid == 'ou_owner' else '小明'}
                for uid in ids]}})
        if args[1:3] == ['im', '+chat-list']:
            chats = [{'chat_id': 'oc_dm', 'chat_mode': 'p2p'},
                     {'chat_id': 'oc_group', 'chat_mode': 'group'}]
            requested = args[args.index('--types') + 1]
            if requested != 'p2p,group':
                chats = [chat for chat in chats if chat['chat_mode'] == requested]
            return json.dumps({'ok': True, 'data': {'chats': chats, 'has_more': False}})
        if args[1:4] == ['api', 'GET', '/open-apis/im/v1/messages']:
            params = json.loads(args[args.index('--params') + 1])
            if params['container_id_type'] == 'thread':
                item = {'message_id': 'om_thread_reply', 'chat_id': 'oc_group',
                        'msg_type': 'text', 'content': json.dumps({'text': '@_user_1 请查状态'}),
                        'create_time': str(int(time.time() * 1000)),
                        'thread_id': 'omt_group', 'root_id': 'om_group',
                        'sender': {'id': {'open_id': 'ou_owner'}, 'sender_type': 'user',
                                   'sender_name': '用户甲'},
                        'mentions': [{'id': {'open_id': 'ou_owner'}, 'key': '@_user_1'}]}
                return json.dumps({'ok': True, 'data': {'items': [item], 'has_more': False}})
            if params['container_id'] == 'oc_group':
                item = {'message_id': 'om_group', 'chat_id': 'oc_group',
                        'msg_type': 'text', 'content': json.dumps({'text': '请查'}),
                        'create_time': str(int((time.time() - 3600 if self.thread_replies else time.time()) * 1000)),
                        'sender': {'id': {'open_id': 'ou_owner' if self.self_mention else 'ou_other'},
                                   'sender_type': 'user',
                                   'sender_name': '小明（姓名）'},
                        'mentions': [{'id': {'open_id': 'ou_owner'}}]}
                if self.thread_replies:
                    item['thread_id'] = 'omt_group'
            else:
                item = {'message_id': 'om_dm2' if params.get('page_token') else 'om_dm',
                        'chat_id': 'oc_dm',
                        'create_time': str(int(time.time() * 1000)),
                        'msg_type': 'text', 'content': json.dumps({'text': '请查'}),
                        'sender': {'id': {'open_id': 'ou_other'}, 'sender_type': 'user',
                                   'sender_name': '小明（姓名）'}}
            more = self.message_token == 'first' and params['container_id'] == 'oc_dm' \
                and not params.get('page_token')
            if self.omit_sender_name:
                item['sender'].pop('sender_name', None)
            return json.dumps({'ok': True, 'data': {'items': [item], 'has_more': more,
                                                     'page_token': 'next' if more else ''}})
        if args[1:3] == ['im', '+messages-reply']:
            return json.dumps({'ok': True, 'data': {'message_id': 'om_reply'}})
        if args[1:3] == ['im', '+messages-send']:
            return json.dumps({'ok': True, 'data': {'message_id': 'om_notice'}})
        if args[1:3] == ['im', 'reactions']:
            return json.dumps({'ok': True, 'data': {'reaction_id': 're_test'}})
        raise AssertionError(args)


class AutoLiveTests(unittest.TestCase):
    def test_reaction_id_is_opaque_and_preserved_for_cleanup(self):
        fake = FakeCLI()
        adapter = FeishuAutoAdapter(CFG, fake)
        opaque = 'opaque+/id=='
        asyncio.run(adapter.withdraw_reaction('om_dm', opaque))
        removal = next(args for args in fake.calls if args[1:4] == ['im', 'reactions', 'delete'])
        self.assertEqual(json.loads(removal[removal.index('--params') + 1])['reaction_id'], opaque)

    def test_failed_discovery_identifies_interface_stage(self):
        fake = FakeCLI()
        fake.fail = 'timeout'
        adapter = FeishuAutoAdapter(CFG, fake)
        with self.assertRaisesRegex(AdapterUnavailable, '会话列表失败'):
            asyncio.run(adapter.discover(json.dumps({'since': 100})))


    def test_missing_reaction_scope_allows_answers(self):
        fake = FakeCLI()
        fake.drop_scope = 'im:message.reactions:write_only'
        db = sqlite3.connect(':memory:')
        async def query(body, context):
            return {'answer': '公开结果', 'public': True}
        auto = AutoReply(db, 'ou_owner', 'oc_owner', FeishuAutoAdapter(CFG, fake), FakeQuery(query))
        self.assertIn('消息表情权限尚未就绪', asyncio.run(auto.enable()))
        self.assertTrue(auto.ingest({'message_id': 'om_test', 'chat_id': 'oc_group',
            'chat_type': 'group', 'sender_id': 'ou_owner', 'sender_type': 'user',
            'sender_name': '用户甲', 'message_type': 'text', 'body': '@_user_1 请查',
            'mentions': [{'id': {'open_id': 'ou_owner'}}], 'source_verified': True,
            'sender_verified': True, 'body_verified': True}))
        self.assertTrue(asyncio.run(auto.process_once()))
        self.assertTrue(auto.active())
        self.assertEqual(db.execute("SELECT state FROM auto_messages WHERE message_id='om_test'").fetchone()[0], 'replied')
        db.close()

    def test_hot_scan_recovers_sender_name_for_self_mention(self):
        fake = FakeCLI()
        fake.omit_sender_name = True
        fake.self_mention = True
        adapter = FeishuAutoAdapter({**CFG, 'OWNER_PROFILE': {'nickname': '用户甲'}}, fake)
        messages = asyncio.run(adapter.discover_hot(json.dumps({'since': 100})))
        group = next(m for m in messages if m['message_id'] == 'om_group')
        self.assertEqual(group['sender_name'], '用户甲')
        self.assertIsNone(eligible(group, 'ou_owner', 'oc_owner', set()))
        self.assertTrue(any(args[1:3] == ['contact', '+search-user'] for args in fake.calls))
        self.assertEqual(asyncio.run(adapter.discover_hot(json.dumps({'since': 100}))), [])

    def test_hot_scan_finds_new_reply_in_older_thread(self):
        fake = FakeCLI()
        fake.thread_replies = True
        adapter = FeishuAutoAdapter(CFG, fake)
        messages = asyncio.run(adapter.discover_hot(json.dumps({'since': int(time.time()) - 30})))
        self.assertEqual([m['message_id'] for m in messages if m['chat_id'] == 'oc_group'],
                         ['om_thread_reply'])
        reply = next(m for m in messages if m['message_id'] == 'om_thread_reply')
        self.assertEqual(reply['root_id'], 'om_group')
        self.assertIsNone(eligible(reply, 'ou_owner', 'oc_owner', set()))
        self.assertTrue(any(json.loads(args[args.index('--params') + 1]).get('container_id_type') == 'thread'
                            for args in fake.calls if args[1:4] == ['api', 'GET', '/open-apis/im/v1/messages']))

    def test_self_mention_normalization_requires_verified_identity_and_structured_target(self):
        chat = {'chat_id': 'oc_group', 'chat_mode': 'group'}
        item = {'message_id': 'om_self', 'chat_id': 'oc_group', 'msg_type': 'text',
                'content': json.dumps({'text': '@_user_1 请查状态'}),
                'sender': {'id': {'open_id': 'ou_owner'}, 'sender_type': 'user',
                           'sender_name': '用户甲（姓名）'},
                'mentions': [{'id': {'open_id': 'ou_owner'}, 'key': '@_user_1'}]}
        normalized = normalize_message(item, chat)
        self.assertTrue(normalized['sender_verified'])
        self.assertTrue(normalized['body_verified'])
        self.assertIsNone(eligible(normalized, 'ou_owner', 'oc_owner', set()))
        item['mentions'] = [{'id': {'open_id': 'ou_other'}, 'key': '@_user_1'}]
        self.assertEqual(eligible(normalize_message(item, chat), 'ou_owner', 'oc_owner', set()), 'owner')
        item['mentions'] = [{'id': {'open_id': 'ou_owner'}, 'key': '@_user_1'}]
        item['sender']['id'] = {'user_id': 'unverified'}
        self.assertEqual(eligible(normalize_message(item, chat), 'ou_owner', 'oc_owner', set()),
                         'identity_unknown')

    def test_cursor_pages_and_provenance(self):
        fake = FakeCLI()
        adapter = FeishuAutoAdapter(CFG, fake)
        async def check():
            first, cursor = await adapter.discover(json.dumps({'since': 100}))
            self.assertEqual([m['message_id'] for m in first], ['om_dm'])
            self.assertTrue(first[0]['sender_verified'])
            second, cursor = await adapter.discover(cursor)
            self.assertEqual([m['message_id'] for m in second], ['om_group'])
            self.assertEqual(second[0]['mentions'][0]['id']['open_id'], 'ou_owner')
            last, cursor = await adapter.discover(cursor)
            self.assertEqual(last, [])
            self.assertNotIn('until', json.loads(cursor))
        asyncio.run(check())
        bad = normalize_message({'message_id': 'om_bad', 'msg_type': 'text',
            'content': '{"text":"hello"}', 'sender': {'id': {'open_id': 'ou_other'},
            'sender_type': 'user'}}, {'chat_id': 'oc_dm', 'chat_mode': 'p2p'})
        self.assertTrue(bad['sender_verified'])
        self.assertEqual(eligible(bad, 'ou_owner', 'oc_owner', set()), 'sender_name_unknown')

    def test_send_confirmations_and_uncertain_no_retry(self):
        fake = FakeCLI()
        adapter = FeishuAutoAdapter(CFG, fake)
        async def check():
            reaction_id = await adapter.react_thinking('om_dm')
            self.assertEqual(reaction_id, 're_test')
            await adapter.withdraw_reaction('om_dm', reaction_id)
            self.assertEqual(await adapter.reply_as_owner({'message_id': 'om_dm'}, '你好，小明'), 'om_reply')
            notice = await adapter.notify_owner('需要确认 om_dm')
            self.assertEqual(notice, 'om_notice')
        asyncio.run(check())
        reaction = next(args for args in fake.calls if args[1:4] == ['im', 'reactions', 'create'])
        self.assertEqual(reaction[reaction.index('--as') + 1], 'user')
        self.assertEqual(json.loads(reaction[reaction.index('--params') + 1]), {'message_id': 'om_dm'})
        removal = next(args for args in fake.calls if args[1:4] == ['im', 'reactions', 'delete'])
        self.assertEqual(json.loads(removal[removal.index('--params') + 1]),
                         {'message_id': 'om_dm', 'reaction_id': 're_test'})
        reply = next(args for args in fake.calls if args[1:3] == ['im', '+messages-reply'])
        self.assertEqual(reply[reply.index('--as') + 1], 'user')
        self.assertIn('--idempotency-key', reply)
        fake.fail = 'send'
        with self.assertRaises(RuntimeError):
            asyncio.run(adapter.reply_as_owner({'message_id': 'om_dm'}, '你好，小明'))

    def test_message_page_cursor_is_resumed(self):
        fake = FakeCLI()
        fake.message_token = 'first'
        adapter = FeishuAutoAdapter(CFG, fake)
        async def check():
            first, cursor = await adapter.discover(json.dumps({'since': 100}))
            self.assertEqual(first[0]['message_id'], 'om_dm')
            self.assertEqual(json.loads(cursor)['message_token'], 'next')
            second, cursor = await adapter.discover(cursor)
            self.assertEqual(second[0]['message_id'], 'om_dm2')
            self.assertEqual(json.loads(cursor)['offset'], 1)
            self.assertNotIn('message_token', json.loads(cursor))
        asyncio.run(check())

    def test_default_off_does_not_touch_transport(self):
        fake = FakeCLI()
        db = sqlite3.connect(':memory:')
        adapter = FeishuAutoAdapter(CFG, fake)
        auto = AutoReply(db, 'ou_owner', 'oc_owner', adapter)
        self.assertEqual(auto.command('status'), '/auto：已关闭')
        self.assertFalse(auto.active())
        self.assertEqual(asyncio.run(auto.poll_once()), 0)
        self.assertEqual(fake.calls, [])
        db.close()

    def test_simulated_scan_delivery_and_restart_do_not_repeat_send(self):
        fake = FakeCLI()
        db = sqlite3.connect(':memory:')
        async def query(body, context):
            return {'answer': '公开状态：正常', 'public': True}
        auto = AutoReply(db, 'ou_owner', 'oc_owner', FeishuAutoAdapter(CFG, fake), FakeQuery(query))
        self.assertIn('已开启', asyncio.run(auto.enable()))
        async def check():
            self.assertEqual(await auto.poll_once(), 2)
            self.assertEqual(await auto.poll_once(), 0)
            self.assertEqual(await auto.poll_once(), 0)
            self.assertFalse(await auto.process_once())
            auto.recover()
            self.assertFalse(await auto.process_once())
        asyncio.run(check())
        replies = [args for args in fake.calls if args[1:3] == ['im', '+messages-reply']]
        self.assertEqual(len(replies), 2)
        self.assertEqual(sum('--reply-in-thread' in args for args in replies), 1)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM auto_messages WHERE state='replied'").fetchone()[0], 2)
        db.close()


if __name__ == '__main__':
    unittest.main()
