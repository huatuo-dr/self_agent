import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from assistant.auto_reply import AutoReply, eligible, safe_result
from assistant.service import database, enqueue


class FakeAdapter:
    def __init__(self):
        self.messages = []
        self.replies = []
        self.reactions = []
        self.withdrawn_reactions = []
        self.notices = []
        self.fail = ''

    async def preflight(self):
        if self.fail == 'preflight':
            raise RuntimeError('unavailable')

    async def discover(self, cursor):
        if self.fail == 'discover':
            raise RuntimeError('unavailable')
        return self.messages, 'next'

    async def reply_as_owner(self, message, content):
        self.replies.append(content)
        if self.fail == 'reply':
            raise RuntimeError('timeout')
        return 'om_reply_' + message['message_id']

    async def react_thinking(self, message_id):
        self.reactions.append(message_id)
        if self.fail == 'reaction':
            raise RuntimeError('timeout')
        return 're_' + message_id

    async def withdraw_reaction(self, message_id, reaction_id):
        self.withdrawn_reactions.append((message_id, reaction_id))

    async def notify_owner(self, content):
        if self.fail == 'notice':
            raise RuntimeError('timeout')
        self.notices.append(content)
        return 'om_notice'


class FakeQuery:
    """Exercise the same prepare/execute interface as the production agent."""
    def __init__(self, callback, decisions=None):
        self.callback = callback
        self.decisions = iter(decisions) if decisions is not None else None

    async def preflight(self):
        pass

    async def prepare(self, body, context):
        return next(self.decisions) if self.decisions is not None else {'action': 'read', 'service': 'gitlab'}

    async def execute(self, body, context, decision):
        if decision['action'] == 'authorize':
            return {'needs_owner': True, 'authorization_detail': '请确认指定操作。'}
        return await self.callback(body, context)


def message(mid='om_1', body='请查状态', chat='oc_dm', kind='p2p', **extra):
    item = dict(message_id=mid, chat_id=chat, chat_type=kind, sender_id='ou_other',
                sender_type='user', sender_name='小明（姓名）', message_type='text',
                body=body, mentions=[], source_verified=True, sender_verified=True,
                body_verified=True)
    item.update(extra)
    return item


def owner_group_message(mid='om_self', **extra):
    item = message(mid=mid, body='@_user_1 请查状态', chat='oc_group', kind='group',
                   sender_id='ou_owner', sender_name='星河（姓名）',
                   mentions=[{'id': {'open_id': 'ou_owner'}, 'key': '@_user_1'}])
    item.update(extra)
    return item


class AutoReplyTests(unittest.TestCase):

    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.adapter = FakeAdapter()
        async def query(body, context):
            return {'answer': '公开状态：运行中', 'public': True}
        self.auto = AutoReply(self.db, 'ou_owner', 'oc_owner', self.adapter, FakeQuery(query))

    def test_configured_reply_signature(self):
        self.auto.cfg = {'BOT_NAME': '小助手', 'MESSAGE_SIGNATURE_TEMPLATE': '{bot_name}代{owner_name}回复',
                         'OWNER_PROFILE': {'nickname': '星河'}}
        self.enable_for_simulation()
        self.assertTrue(self.auto.ingest(message()))
        asyncio.run(self.auto.process_once())
        self.assertTrue(self.adapter.replies[0].endswith('小助手代星河回复'))

    def tearDown(self):
        self.db.close()

    def enable_for_simulation(self):
        self.assertIn('已开启', asyncio.run(self.auto.enable()))

    def state(self, mid='om_1'):
        return self.db.execute('SELECT state FROM auto_messages WHERE message_id=?', (mid,)).fetchone()[0]

    def test_default_off_and_preflight(self):
        self.assertEqual(self.auto.command('status'), '/auto：已关闭')
        self.assertFalse(self.auto.active())
        self.assertFalse(self.auto.ingest(message()))
        self.adapter.fail = 'preflight'
        self.assertIn('启动失败', asyncio.run(self.auto.enable()))
        self.assertIn('原因', self.auto.command('status'))
        self.assertFalse(self.auto.active())

    def test_owner_only_command_route_and_fast_off(self):
        db = database(':memory:')
        cfg = {'FEISHU_OWNER_OPEN_ID': 'ou_owner', 'FEISHU_OWNER_CHAT_ID': 'oc_owner'}
        event = dict(message_id='om_off', chat_type='p2p', sender_type='user',
                     sender_id='ou_owner', chat_id='oc_owner', message_type='text',
                     content='/auto off')
        self.assertTrue(enqueue(db, cfg, event))
        self.assertEqual(db.execute("SELECT fast_lane FROM inbox WHERE id='om_off'").fetchone()[0], 1)
        self.assertFalse(enqueue(db, cfg, {**event, 'message_id': 'om_other', 'sender_id': 'ou_other'}))
        db.close()

    def test_filter_commands_mentions_and_monitored_group(self):
        self.assertEqual(eligible(message(body=' /unknown'), 'ou_owner', 'oc_owner', set()), 'command')
        self.assertEqual(eligible(message(body='help'), 'ou_owner', 'oc_owner', set()), 'command')
        self.assertIsNone(eligible(message(body='请查状态', quote='/evolution'), 'ou_owner', 'oc_owner', set()))
        group = message(chat='oc_group', kind='group', mentions=[{'id': {'open_id': 'ou_owner'}}])
        self.assertIsNone(eligible(group, 'ou_owner', 'oc_owner', set()))
        self.assertEqual(eligible(group, 'ou_owner', 'oc_owner', {'oc_group'}), 'watched_or_unknown_chat')
        group['mentions'].append({'all': True, 'id': 'all'})
        self.assertEqual(eligible(group, 'ou_owner', 'oc_owner', set()), 'everyone')
        self.assertEqual(eligible(message(sender_type='app'), 'ou_owner', 'oc_owner', set()), 'sender_unknown_or_bot')
        self.assertEqual(eligible(message(sender_verified=False), 'ou_owner', 'oc_owner', set()),
                         'provenance_unknown')
        self.enable_for_simulation()
        self.db.execute("CREATE TABLE group_watches(binding TEXT,chat_id TEXT,state TEXT)")
        self.db.execute("INSERT INTO group_watches VALUES('oc_owner','oc_group','active')")
        self.assertFalse(self.auto.ingest(message(chat='oc_group', kind='group',
                                                  mentions=[{'id': {'open_id': 'ou_owner'}}])))

    def test_owner_self_mention_only_in_unwatched_group(self):
        self.assertIsNone(eligible(owner_group_message(), 'ou_owner', 'oc_owner', set()))
        self.assertEqual(eligible(owner_group_message(mentions=[]), 'ou_owner', 'oc_owner', set()), 'owner')
        self.assertEqual(eligible(owner_group_message(body='/auto on'), 'ou_owner', 'oc_owner', set()), 'command')
        self.assertEqual(eligible(owner_group_message(body='help'), 'ou_owner', 'oc_owner', set()), 'command')
        self.assertEqual(eligible(owner_group_message(body=' '), 'ou_owner', 'oc_owner', set()), 'body_unknown')
        self.assertEqual(eligible(owner_group_message(sender_verified=False), 'ou_owner', 'oc_owner', set()),
                         'provenance_unknown')
        self.assertEqual(eligible(owner_group_message(sender_type='app'), 'ou_owner', 'oc_owner', set()),
                         'sender_unknown_or_bot')
        self.assertEqual(eligible(owner_group_message(mentions=[{'id': {'open_id': 'ou_other'}}]),
                                  'ou_owner', 'oc_owner', set()), 'owner')
        self.assertEqual(eligible(owner_group_message(mentions=[{'all': True, 'id': 'all'}]),
                                  'ou_owner', 'oc_owner', set()), 'everyone')
        self.assertEqual(eligible(owner_group_message(), 'ou_owner', 'oc_owner', {'oc_group'}),
                         'watched_or_unknown_chat')
        self.assertEqual(eligible(message(sender_id='ou_owner', sender_name='星河'),
                                  'ou_owner', 'oc_owner', set()), 'owner')
        self.assertEqual(eligible(message(chat='oc_owner', kind='p2p', sender_id='ou_owner'),
                                  'ou_owner', 'oc_owner', set()), 'owner')

    def test_self_mention_reply_id_cannot_reenter_queue(self):
        self.enable_for_simulation()
        self.assertTrue(self.auto.ingest(owner_group_message()))
        self.assertTrue(asyncio.run(self.auto.process_once()))
        reply_id = self.db.execute("SELECT reply_id FROM auto_messages WHERE message_id='om_self'").fetchone()[0]
        self.assertTrue(reply_id)
        self.assertFalse(self.auto.ingest(owner_group_message(mid=reply_id)))
        self.assertFalse(self.auto.ingest(owner_group_message()))
        restarted = AutoReply(self.db, 'ou_owner', 'oc_owner', self.adapter, self.auto.query)
        restarted.recover()
        self.assertFalse(restarted.ingest(owner_group_message(mid=reply_id)))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM auto_messages').fetchone()[0], 1)
        self.assertEqual(len(self.adapter.replies), 1)

    def test_dedupe_restart_context_and_sensitive_fallback(self):
        self.enable_for_simulation()
        self.adapter.messages = [message()]
        self.assertEqual(asyncio.run(self.auto.poll_once()), 1)
        self.assertEqual(asyncio.run(self.auto.poll_once()), 0)
        self.assertTrue(asyncio.run(self.auto.process_once()))
        self.assertEqual(self.state(), 'replied')
        self.assertTrue(self.adapter.replies[0].startswith('你好，小明'))
        self.assertEqual(len(self.auto.context('dm:oc_dm')), 2)
        self.assertEqual(self.auto.context('dm:oc_other'), [])
        self.auto = AutoReply(self.db, 'ou_owner', 'oc_owner', self.adapter, self.auto.query)
        self.auto.recover()
        self.assertFalse(asyncio.run(self.auto.process_once()))
        self.assertIn('服务重启后默认关闭', self.auto.command('status'))
        self.enable_for_simulation()
        async def private_query(body, context):
            return {'answer': 'password=hidden', 'public': True}
        self.auto.query = FakeQuery(private_query)
        self.assertTrue(self.auto.ingest(message(mid='om_2')))
        self.assertTrue(asyncio.run(self.auto.process_once()))
        self.assertEqual(self.state('om_2'), 'ignored')
        self.assertEqual(self.adapter.withdrawn_reactions[-1], ('om_2', 're_om_2'))
        self.assertNotIn('hidden', self.adapter.replies[-1])
        self.assertFalse(safe_result('user@example.com', public=True))
        self.assertFalse(safe_result('公开', public=False))
        self.assertFalse(safe_result('修复已合并', public=True))
        self.assertTrue(safe_result('修复已合并', public=True, verified_source=True))
        self.assertFalse(safe_result('token=hidden 已合并', public=True, verified_source=True))

    def test_all_senders_get_reaction_before_query_and_failure_does_not_block_reply(self):
        self.enable_for_simulation()
        async def checked_query(body, context):
            self.assertEqual(self.adapter.reactions[0], 'om_first')
            return {'answer': '公开状态：运行中', 'public': True}
        self.auto.query = FakeQuery(checked_query)
        self.assertTrue(self.auto.ingest(message(mid='om_first')))
        self.assertTrue(asyncio.run(self.auto.process_once()))
        self.assertEqual(self.state('om_first'), 'replied')

        self.adapter.fail = 'reaction'
        self.assertTrue(self.auto.ingest(message(mid='om_second')))
        self.assertTrue(asyncio.run(self.auto.process_once()))
        self.assertEqual(self.state('om_second'), 'replied')
        self.assertTrue(self.auto.active())
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM auto_errors WHERE stage='reaction_failed'").fetchone()[0], 1)

        self.adapter.fail = ''
        self.assertTrue(self.auto.ingest(owner_group_message(mid='om_owner')))
        self.assertTrue(asyncio.run(self.auto.process_once()))
        self.assertEqual(self.adapter.reactions, ['om_first', 'om_second', 'om_owner'])

    def test_unrelated_message_is_silent_and_action_only_notifies_owner(self):
        class ServiceQuery:
            async def __call__(self, body, context):
                return {'answer': '查询结果已核实。', 'public': True}
        self.auto.query = FakeQuery(ServiceQuery(), [
            {'action': 'ignore', 'service': 'none'},
            {'action': 'read', 'service': 'gitlab'},
            {'action': 'authorize', 'service': 'gitlab'},
            {'action': 'read', 'service': 'gitlab'},
        ])
        self.enable_for_simulation()
        self.assertTrue(self.auto.ingest(message(mid='om_chat', body='明天一起吃饭吗？')))
        self.assertTrue(asyncio.run(self.auto.process_once()))
        self.assertEqual(self.state('om_chat'), 'ignored')
        self.assertEqual(self.adapter.reactions, [])
        self.assertEqual(self.adapter.replies, [])
        self.assertEqual(self.adapter.notices, [])

        self.assertTrue(self.auto.ingest(message(mid='om_secret', body='GitLab token=secret-value')))
        self.assertTrue(asyncio.run(self.auto.process_once()))
        self.assertEqual(self.state('om_secret'), 'ignored')
        self.assertEqual(self.adapter.reactions, [])

        self.assertTrue(self.auto.ingest(message(mid='om_action', body='请合并 GitLab MR !9')))
        self.assertTrue(asyncio.run(self.auto.process_once()))
        self.assertEqual(self.state('om_action'), 'notified')
        self.assertEqual(self.adapter.reactions, ['om_action'])
        self.assertEqual(self.adapter.replies, [])
        self.assertEqual(len(self.adapter.notices), 1)
        self.assertIn('请合并 GitLab MR !9', self.adapter.notices[0])

        self.assertTrue(self.auto.ingest(message(mid='om_read', body='GitLab MR !9 状态？')))
        self.assertTrue(asyncio.run(self.auto.process_once()))
        self.assertEqual(self.state('om_read'), 'replied')
        self.assertEqual(self.adapter.reactions, ['om_action', 'om_read'])

    def test_verified_read_only_status_can_report_completed_external_work(self):
        self.enable_for_simulation()
        async def query(body, context):
            return {'answer': '修复 MR 已合并；部署仍待核验。',
                    'public': True, 'needs_owner': False, 'verified_source': True}
        self.auto.query = FakeQuery(query)
        self.assertTrue(self.auto.ingest(message(mid='om_status')))
        self.assertTrue(asyncio.run(self.auto.process_once()))
        self.assertEqual(self.state('om_status'), 'replied')
        self.assertIn('已合并', self.adapter.replies[-1])

    def test_failure_states(self):
        self.enable_for_simulation()
        self.adapter.fail = 'discover'
        self.assertEqual(asyncio.run(self.auto.poll_once()), 0)
        self.assertEqual(self.db.execute('SELECT stage FROM auto_errors').fetchone()[0], 'discovery_failed')
        self.assertTrue(self.auto.active())
        self.assertEqual(asyncio.run(self.auto.poll_once()), 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM auto_errors').fetchone()[0], 1)
        self.auto.discovery_retry_at = 0
        self.assertEqual(asyncio.run(self.auto.poll_once()), 0)
        self.assertTrue(self.auto.active())
        self.auto.discovery_retry_at = 0
        self.assertEqual(asyncio.run(self.auto.poll_once()), 0)
        self.assertFalse(self.auto.active())
        self.assertIn('消息发现连续失败 3 次', self.auto.command('status'))
        self.adapter.fail = ''
        self.enable_for_simulation()
        self.adapter.fail = 'reply'
        self.assertTrue(self.auto.ingest(message()))
        asyncio.run(self.auto.process_once())
        self.assertEqual(self.state(), 'uncertain')
        self.auto.recover()
        self.assertFalse(asyncio.run(self.auto.process_once()))
        self.enable_for_simulation()
        async def authorization(body, context):
            return {'handled': False, 'needs_owner': True,
                    'authorization_detail': '缺少只读权限'}
        self.auto.query = FakeQuery(authorization)
        self.adapter.fail = 'notice'
        self.auto.ingest(message(mid='om_2'))
        asyncio.run(self.auto.process_once())
        self.assertEqual(self.state('om_2'), 'notice_uncertain')
        self.assertTrue(self.auto.active())
        self.adapter.fail = ''
        async def public_query(body, context):
            return {'answer': '公开状态：运行中', 'public': True}
        self.auto.query = FakeQuery(public_query)
        self.auto.ingest(message(mid='om_4'))
        asyncio.run(self.auto.process_once())
        self.assertEqual(self.state('om_4'), 'replied')

    def test_transient_discovery_error_recovers_without_stopping(self):
        self.enable_for_simulation()
        self.adapter.fail = 'discover'
        self.assertEqual(asyncio.run(self.auto.poll_once()), 0)
        self.assertTrue(self.auto.active())
        self.adapter.fail = ''
        self.auto.discovery_retry_at = 0
        self.assertEqual(asyncio.run(self.auto.poll_once()), 0)
        self.assertEqual(self.auto.discovery_failures, 0)
        self.assertTrue(self.auto.active())

    def test_hot_message_reacts_before_full_scan_and_answer(self):
        events = []
        hot_message = message(body='GitLab MR 状态？')
        class OrderedAdapter(FakeAdapter):
            async def discover_hot(self, cursor, on_messages=None):
                await on_messages([hot_message])
                return [hot_message]
            async def discover(self, cursor):
                events.append('full_scan')
                return [], 'next'
            async def react_thinking(self, message_id):
                events.append('reaction')
                return await super().react_thinking(message_id)
        class ServiceQuery:
            async def __call__(self, body, context):
                events.append('query')
                return {'answer': '公开状态：运行中', 'public': True}
        self.auto.adapter = OrderedAdapter()
        self.auto.query = FakeQuery(ServiceQuery())
        self.enable_for_simulation()
        self.assertEqual(asyncio.run(self.auto.poll_once()), 1)
        self.assertEqual(events, ['reaction', 'query', 'full_scan'])
        self.assertEqual(self.state(), 'replied')

    def test_query_failure_is_silent_without_impersonating_owner(self):
        self.enable_for_simulation()
        async def broken_query(body, context):
            raise RuntimeError('model unavailable')
        self.auto.query = FakeQuery(broken_query)
        self.assertTrue(self.auto.ingest(message()))
        self.assertTrue(asyncio.run(self.auto.process_once()))
        self.assertEqual(self.state(), 'ignored')
        self.assertEqual(self.adapter.withdrawn_reactions, [('om_1', 're_om_1')])
        self.assertEqual(self.adapter.replies, [])
        self.assertTrue(self.auto.active())


    def test_sensitive_fragment_is_removed_from_useful_read_only_answer(self):
        self.enable_for_simulation()
        async def query(body, context):
            return {'answer': '构建失败，token=abc123secret；原因是依赖下载超时。',
                    'public': True, 'needs_owner': False}
        self.auto.query = FakeQuery(query)
        self.assertTrue(self.auto.ingest(message()))
        self.assertTrue(asyncio.run(self.auto.process_once()))
        self.assertEqual(self.state(), 'replied')
        self.assertIn('依赖下载超时', self.adapter.replies[0])
        self.assertNotIn('abc123secret', self.adapter.replies[0])


    def test_disk_restart_dedupe_and_off_during_query(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'auto.sqlite3'
            db = sqlite3.connect(path)
            first = AutoReply(db, 'ou_owner', 'oc_owner', self.adapter, self.auto.query)
            asyncio.run(first.enable())
            self.assertTrue(first.ingest(message()))
            db.close()
            db = sqlite3.connect(path)
            second = AutoReply(db, 'ou_owner', 'oc_owner', self.adapter, self.auto.query)
            self.assertFalse(second.ingest(message()))
            async def off_while_querying(body, context):
                second.command('off')
                return {'answer': '公开结果', 'public': True}
            second.query = FakeQuery(off_while_querying)
            self.assertTrue(asyncio.run(second.process_once()))
            self.assertEqual(db.execute("SELECT state FROM auto_messages WHERE message_id='om_1'").fetchone()[0],
                             'cancelled')
            self.assertFalse(self.adapter.replies)
            self.assertFalse(second.active())
            db.close()


if __name__ == '__main__':
    unittest.main()
