import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
import time
import unittest
from unittest.mock import AsyncMock

from assistant.group_watch import GroupWatch, mentioned_owner, message_text, relevant_alias, watch_status_query
from assistant.settings import DEFAULT_ACK, get_timezone, sign_message
from assistant.schedule import Schedule
from assistant.service import database
from assistant.sessions import Sessions

ACK = sign_message(DEFAULT_ACK)
SHANGHAI = get_timezone()


class GroupWatchTests(unittest.IsolatedAsyncioTestCase):
    def mock_empty_classifier(self):
        from unittest.mock import patch
        from assistant.agent_backends.base import RunResult
        self.cfg.update(AGENT_BACKEND='grok', GROK_BIN='grok')
        p = patch('assistant.agent_runner.AgentRunner.run',
                  AsyncMock(return_value=RunResult('{"matches":[]}', {'matches': []})))
        p.start()
        self.addCleanup(p.stop)

    def setUp(self):
        self.db = database(':memory:')
        self.cfg = {'FEISHU_OWNER_CHAT_ID': 'oc_owner', 'FEISHU_OWNER_OPEN_ID': 'ou_owner',
                    'LARK_CLI_BIN': 'lark-cli', 'OWNER_PROFILE': {'name': '测试甲',
                    'nickname': '星河', 'aliases': ['星哥', '测试甲', '甲哥', '测试佳', '测试嘉', '星辰', '小星']}}
        self.sent = AsyncMock(return_value=json.dumps({'ok': True, 'data': {'message_id': 'om_sent'}}))
        self.messages = []
        def read(request):
            if request.get('page_size') == 1:
                return {'ok': True, 'data': {'items': []}}
            return {'ok': True, 'data': {'items': self.messages, 'has_more': False}}
        self.watch = GroupWatch(self.cfg, self.db, self.sent, read=read)
        self.watch.verify_owner_sender = AsyncMock()
        self.sessions = Sessions(self.db, 'oc_owner')
        self.sid = self.sessions.create('om_source', '监控群')
        self.sessions.bind(self.sid, 'om_root', 'omt_thread')
        self.db.execute("INSERT INTO inbox(id,binding,session_id,state) VALUES('om_source','oc_owner',?,'done')",
                        (self.sid,))
        self.db.commit()

    async def test_configured_ack_and_partial_profile(self):
        from assistant.group_watch import aliases, time_label
        self.assertEqual(aliases({'nickname': '星河'}), ['星河'])
        self.cfg.update(GROUP_WATCH_ACK_TEXT='已收到，会及时查看。',
                        MESSAGE_SIGNATURE_TEMPLATE='{bot_name}代发', BOT_NAME='小助手', TIMEZONE='UTC')
        self.assertEqual(time_label(1791849600, self.cfg), '10-13 00:00:00')
        await self.create()
        self.messages = [self.message('om_configured', '@星河 请查看', True)]
        await self.watch.tick()
        replies = [call.args[0] for call in self.sent.call_args_list if '--as' in call.args[0]
                   and call.args[0][call.args[0].index('--as') + 1] == 'user']
        self.assertEqual(len(replies), 1)
        self.assertIn('已收到，会及时查看。\n\n小助手代发', replies[0])

    def tearDown(self):
        self.db.close()

    async def create(self, allow_thread=False):
        result = await self.watch.create_many([{'chat_id': 'oc_group', 'chat_name': '项目群',
                                               'allow_thread': allow_thread}], 'om_source', self.sid)
        return result[0][0]

    async def create_custom(self, *, source='om_custom', sid=None, end_at=None):
        sid = self.sid if sid is None else sid
        deadline = end_at or (datetime.now(SHANGHAI) + timedelta(minutes=30)).isoformat()
        item = {'chat_id': 'oc_group', 'chat_name': '项目群', 'allow_thread': False,
                'end_at': deadline, 'policy': {'kind': 'custom',
                'other': '扮演坏学生，针对题目顶嘴，不按题目要求回答。',
                'self': '认真回答题目，写清楚依据。'}}
        (key, created), = await self.watch.create_many([item], source, sid)
        self.assertTrue(created)
        return key

    def message(self, mid, content, mention=False):
        return {'message_id': mid, 'create_time': str(int(time.time() * 1000) + 500),
                'sender': {'sender_type': 'user', 'id': {'open_id': 'ou_colleague'}},
                'body': {'content': json.dumps({'text': content}, ensure_ascii=False)},
                'mentions': [{'id': {'open_id': 'ou_owner'}}] if mention else []}

    def card(self, mid, text, summary=None):
        item = self.message(mid, '')
        item['msg_type'] = 'interactive'
        item['body']['content'] = json.dumps({
            'body': {'elements': [{'tag': 'column_set', 'columns': [{
                'tag': 'column', 'elements': [{'tag': 'markdown', 'content': text}]}]}]},
            'config': {'summary': {'content': summary or text}},
            'schema': '2.0'}, ensure_ascii=False)
        return item

    async def test_structured_mention_quotes_without_thread_then_notifies_once(self):
        key = await self.create()
        self.messages = [self.message('om_group_msg', '@星河 请看一下', True)]
        self.watch.classify = AsyncMock(return_value={})
        await self.watch.tick()
        self.assertEqual(self.sent.await_count, 2)
        group_args = self.sent.await_args_list[0].args[0]
        private_args = self.sent.await_args_list[1].args[0]
        self.assertEqual(group_args[group_args.index('--message-id') + 1], 'om_group_msg')
        self.assertEqual(group_args[group_args.index('--as') + 1], 'user')
        self.assertNotIn('--reply-in-thread', group_args)
        self.assertEqual(group_args[group_args.index('--text') + 1], ACK)
        self.assertEqual(private_args[private_args.index('--message-id') + 1], 'om_root')
        self.assertEqual(private_args[private_args.index('--as') + 1], 'bot')
        self.assertIn('--reply-in-thread', private_args)
        self.assertEqual(self.db.execute('SELECT state FROM group_watch_hits').fetchone()[0], 'done')
        await self.watch.tick()
        self.assertEqual(self.sent.await_count, 2)
        self.assertIn(key, Schedule(self.cfg, self.db, self.sent).command('', self.sid))

    async def test_alias_only_notifies_and_does_not_group_reply(self):
        await self.create()
        self.mock_empty_classifier()
        self.messages = [self.message('om_alias', '星哥，麻烦看看这条流水线'),
                         self.message('om_xilaile', '星辰，麻烦处理这条流水线'),
                         self.message('om_shenyi', '小星，帮我确认一下'),
                         self.message('om_plain', '星辰上线了')]
        self.messages[1]['mentions'] = [{'id': {'open_id': 'ou_bot'}}]
        await self.watch.tick()
        self.assertEqual(self.sent.await_count, 3)
        self.assertTrue(all(call.args[0][call.args[0].index('--message-id') + 1] == 'om_root'
                            for call in self.sent.await_args_list))
        self.assertEqual({row[0] for row in self.db.execute('SELECT message_id FROM group_watch_hits')},
                         {'om_alias', 'om_xilaile', 'om_shenyi'})
        self.assertTrue(relevant_alias('测试嘉请处理一下', ['测试嘉']))
        self.assertTrue(relevant_alias('小星请处理一下', ['小星']))
        self.assertFalse(mentioned_owner(self.messages[0], 'ou_owner'))
        self.assertFalse(mentioned_owner(self.messages[1], 'ou_owner'))

    async def test_card_question_notifies_once_without_group_reply(self):
        key = await self.create()
        self.mock_empty_classifier()
        question = ('星河，来一道初中数学题，欢迎自愿作答：\n\n'
                    '已知长方形的周长为 34 厘米，长比宽多 3 厘米，求它的面积。\n\n'
                    '请写出计算过程，并注明由本人还是 AI 助手作答。')
        card = self.card('om_math_card', question)
        self.messages = [card, self.card('om_unrelated_card', '本周活动安排')]
        self.assertEqual(message_text(card), question)
        await self.watch.tick()
        self.assertEqual(self.sent.await_count, 1)
        args = self.sent.await_args.args[0]
        self.assertEqual(args[args.index('--as') + 1], 'bot')
        self.assertEqual(args[args.index('--message-id') + 1], 'om_root')
        self.assertIn(question, args[args.index('--text') + 1])
        self.assertEqual(self.db.execute('SELECT message_id,mentioned,state FROM group_watch_hits '
                                         'WHERE watch_id=?', (key,)).fetchall(),
                         [('om_math_card', 0, 'done')])
        self.assertEqual(self.db.execute('SELECT last_error FROM group_watches WHERE id=?',
                                         (key,)).fetchone()[0], '')
        await self.watch.tick()
        self.assertEqual(self.sent.await_count, 1)

    async def test_card_summary_fallback_and_unreadable_diagnostic(self):
        key = await self.create()
        self.mock_empty_classifier()
        summary_card = self.card('om_summary_card', '')
        summary_card['body']['content'] = json.dumps({
            'body': {'elements': []},
            'config': {'summary': {'content': '小星，请处理这条消息'}}})
        unreadable = self.card('om_empty_card', '')
        unreadable['body']['content'] = json.dumps({'body': {'elements': []}, 'schema': '2.0'})
        self.messages = [summary_card, unreadable]
        self.assertEqual(message_text(summary_card), '小星，请处理这条消息')
        with self.assertLogs(level='WARNING') as logs:
            await self.watch.tick()
        self.assertEqual(self.sent.await_count, 1)
        self.assertIn('om_empty_card', '\n'.join(logs.output))
        error = self.db.execute('SELECT last_error FROM group_watches WHERE id=?', (key,)).fetchone()[0]
        self.assertIn('om_empty_card', error)
        self.assertIn('有未解析卡片', self.watch.status_text(session_id=self.sid))
        self.assertEqual(self.db.execute('SELECT message_id FROM group_watch_hits').fetchall(),
                         [('om_summary_card',)])

    async def test_owner_self_mention_triggers_but_other_own_and_bot_messages_do_not(self):
        await self.create()
        self_mention = self.message('om_self_mention', '@_user_1 看一下', True)
        self_mention['sender']['id']['open_id'] = 'ou_owner'
        self_mention['mentions'] = [{'id': 'ou_owner'}]
        own_plain = self.message('om_own_plain', '我自己看一下')
        own_plain['sender']['id']['open_id'] = 'ou_owner'
        bot_mention = self.message('om_bot_mention', '@_user_1 看一下', True)
        bot_mention['sender']['sender_type'] = 'app'
        self.messages = [self_mention, own_plain, bot_mention]
        self.watch.classify = AsyncMock(return_value={})
        await self.watch.tick()
        self.assertEqual(self.sent.await_count, 2)
        self.assertEqual(self.db.execute('SELECT message_id FROM group_watch_hits').fetchall(),
                         [('om_self_mention',)])

    async def test_custom_policy_overrides_default_and_branches_on_sender(self):
        default_key = await self.create()
        custom_key = await self.create_custom()
        self.watch.compose_reply = AsyncMock(side_effect=lambda instruction, content, sender:
            '认真作答\n\nAI 助手代发' if '认真' in instruction else
            '坏学生作答\n\nAI 助手代发')
        other = self.message('om_other_mention', '老师：请回答这道题', True)
        mine = self.message('om_self_mention_custom', '我：请认真回答这道题', True)
        mine['sender']['id']['open_id'] = 'ou_owner'
        self.messages = [other, mine]
        self.watch.classify = AsyncMock(return_value={})
        await self.watch.tick()
        rows = self.db.execute('SELECT watch_id,message_id,owner_sender FROM group_watch_hits '
                               'ORDER BY message_id').fetchall()
        self.assertEqual(rows, [(custom_key, 'om_other_mention', 0),
                                (custom_key, 'om_self_mention_custom', 1)])
        self.assertEqual(self.sent.await_count, 4)
        group_replies = [call.args[0][call.args[0].index('--text') + 1]
                         for call in self.sent.await_args_list if '--as' in call.args[0]
                         and call.args[0][call.args[0].index('--as') + 1] == 'user']
        self.assertEqual(group_replies, ['坏学生作答\n\nAI 助手代发',
                                         '认真作答\n\nAI 助手代发'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM group_watch_hits WHERE watch_id=?',
                                         (default_key,)).fetchone()[0], 0)

    async def test_owner_can_mute_hit_notices_without_stopping_group_replies(self):
        key = await self.create_custom()
        self.watch.compose_reply = AsyncMock(return_value='坏学生作答\n\nAI 助手代发')
        request = '后面再有@ 我的消息，你就不需要再通知我【群监控命中】的消息了，你直接回复就行'
        self.assertIn('已关闭', self.watch.notification_preference_text(request, self.sid))
        self.assertFalse(self.watch.status_snapshot(self.sid)[0]['notify_hits'])
        self.messages = [self.message('om_muted_mention', '请回答', True)]
        self.watch.classify = AsyncMock(return_value={})
        await self.watch.tick()
        self.assertEqual(self.sent.await_count, 1)
        args = self.sent.await_args.args[0]
        self.assertEqual(args[args.index('--as') + 1], 'user')
        self.assertEqual(self.db.execute('SELECT state FROM group_watch_hits WHERE watch_id=?',
                                         (key,)).fetchone(), ('done',))
        self.assertIn('命中私聊提醒已关闭', self.watch.status_text(session_id=self.sid))
        self.assertIn('已开启', self.watch.notification_preference_text('恢复群监控命中提醒', self.sid))
        self.assertIsNone(self.watch.notification_preference_text('为什么停止群监控命中提醒？', self.sid))

    async def test_notice_preference_can_change_from_another_topic_when_target_is_unique(self):
        key = await self.create_custom()
        other_sid = self.sessions.create('om_other_topic', '其他话题')
        self.sessions.bind(other_sid, 'om_other_topic', 'omt_other_topic')
        self.assertIn('已关闭', self.watch.notification_preference_text('关闭项目群的群监控命中提醒',
                                                                      other_sid))
        self.assertEqual(self.db.execute('SELECT notify_hits FROM group_watches WHERE id=?',
                                         (key,)).fetchone(), (0,))
        self.assertEqual(self.watch.update_notifications(
            [{'watch_id': key, 'notify_hits': True}], other_sid, '恢复项目群的群监控提醒'),
            [(key, '项目群', True)])

    async def test_cross_topic_notice_change_requires_unique_target(self):
        first = await self.create_custom()
        second_sid = self.sessions.create('om_second_topic', '第二个监控')
        await self.watch.create_many([{'chat_id': 'oc_second', 'chat_name': '另一个群',
                                       'allow_thread': False}], 'om_second_topic', second_sid)
        unrelated_sid = self.sessions.create('om_unrelated', '无关话题')
        self.assertIn('有多个', self.watch.notification_preference_text('关闭群监控命中提醒',
                                                                      unrelated_sid))
        with self.assertRaisesRegex(ValueError, '指定唯一'):
            self.watch.update_notifications([{'watch_id': first, 'notify_hits': False}],
                                            unrelated_sid, '别再发群监控命中提醒')
        self.assertEqual(self.db.execute('SELECT notify_hits FROM group_watches WHERE id=?',
                                         (first,)).fetchone(), (1,))

    async def test_muted_watch_reports_group_reply_failure_as_exception(self):
        await self.create_custom()
        self.watch.notification_preference_text('关闭群监控命中提醒', self.sid)
        self.messages = [self.message('om_muted_failure', '请回答', True)]
        self.watch.compose_reply = AsyncMock(side_effect=ValueError('generation failed'))
        self.watch.classify = AsyncMock(return_value={})
        await self.watch.tick()
        self.assertEqual(self.sent.await_count, 1)
        args = self.sent.await_args.args[0]
        self.assertEqual(args[args.index('--as') + 1], 'bot')
        note = args[args.index('--text') + 1]
        self.assertIn('群监控回复异常', note)
        self.assertNotIn('群监控命中', note)

    async def test_custom_watch_expires_and_default_resumes(self):
        default_key = await self.create()
        custom_key = await self.create_custom()
        ended = time.time() - 1
        self.db.execute('UPDATE group_watches SET end_at=?,cursor_sec=?,created=? WHERE id=?',
                        (ended, int(ended) - 10, ended - 20, custom_key))
        self.db.commit()
        await self.watch.tick()
        self.assertEqual(self.db.execute('SELECT state FROM group_watches WHERE id=?',
                                         (custom_key,)).fetchone(), ('expired',))
        self.assertEqual(self.db.execute('SELECT state FROM group_watches WHERE id=?',
                                         (default_key,)).fetchone(), ('active',))
        self.assertGreaterEqual(self.db.execute('SELECT cursor_sec FROM group_watches WHERE id=?',
                                                (default_key,)).fetchone()[0], int(ended))

    async def test_explicit_thread_permission_and_cancel_routes(self):
        key = await self.create(allow_thread=True)
        self.messages = [self.message('om_thread', '请看一下', True)]
        self.watch.classify = AsyncMock(return_value={})
        await self.watch.tick()
        self.assertIn('--reply-in-thread', self.sent.await_args_list[0].args[0])
        self.assertIn('已停止监控', self.watch.cancel_text('停止监控', self.sid))
        self.assertNotIn(key, Schedule(self.cfg, self.db, self.sent).command('', self.sid))

    async def test_activity_stop_phrase_cancels_existing_watch(self):
        key = await self.create_custom()
        self.assertIsNone(self.watch.cancel_text('停止监控了吗', self.sid))
        self.assertIn('已停止监控', self.watch.cancel_text('可以停止参与活动了', self.sid))
        self.assertEqual(self.db.execute('SELECT state FROM group_watches WHERE id=?',
                                         (key,)).fetchone(), ('cancelled',))

    async def test_stop_hit_reminders_does_not_cancel_watch(self):
        key = await self.create_custom()
        self.assertIsNone(self.watch.cancel_text('停止群监控命中提醒', self.sid))
        self.assertIn('已关闭', self.watch.notification_preference_text('停止群监控命中提醒', self.sid))
        self.assertEqual(self.db.execute('SELECT state FROM group_watches WHERE id=?',
                                         (key,)).fetchone(), ('active',))

    async def test_model_cancel_requires_unambiguous_target(self):
        first = await self.create_custom()
        other_sid = self.sessions.create('om_other_watch', '第二个群监控')
        await self.watch.create_many([{'chat_id': 'oc_second', 'chat_name': '另一个群',
                                       'allow_thread': False}], 'om_other_watch', other_sid)
        unrelated_sid = self.sessions.create('om_unrelated', '其他话题')
        with self.assertRaisesRegex(ValueError, '指定唯一'):
            self.watch.cancel_many([first], unrelated_sid, '把群监控停了')
        self.assertEqual(self.watch.cancel_many([first], unrelated_sid, '机器人学校监控 ' + first),
                         [(first, '项目群', self.sid)])

    async def test_uncertain_group_reply_is_not_repeated_after_restart(self):
        key = await self.create()
        self.db.execute('INSERT INTO group_watch_hits '
                        '(watch_id,message_id,reason,content,mentioned,state,created) '
                        "VALUES(?,?,?,?,1,'replying',?)",
                        (key, 'om_uncertain', '消息明确 @你', '请看', time.time()))
        self.db.commit()
        self.watch.recover()
        self.watch.classify = AsyncMock(return_value={})
        await self.watch.tick()
        self.assertEqual(self.sent.await_count, 1)
        note = self.sent.await_args.args[0]
        self.assertEqual(note[note.index('--message-id') + 1], 'om_root')
        self.assertIn('群回复失败或结果不确定', note[note.index('--text') + 1])

    async def test_schedule_cancel_stops_watch(self):
        key = await self.create()
        result = Schedule(self.cfg, self.db, self.sent).command('cancel ' + key, self.sid)
        self.assertIn('已取消', result)
        self.assertEqual(self.db.execute('SELECT state FROM group_watches WHERE id=?', (key,)).fetchone()[0],
                         'cancelled')

    async def test_status_query_reports_health_and_stopped_state(self):
        self.assertTrue(watch_status_query('是否还在监控群消息'))
        self.assertTrue(watch_status_query('这个群还盯着吗？'))
        self.assertFalse(watch_status_query('帮我监控这个群'))
        self.assertFalse(watch_status_query('你是否可以监控这个群？'))
        self.assertFalse(watch_status_query('为什么监控状态不正常？'))
        key = await self.create()
        other_sid = self.sessions.create('om_other_source', '另一个监控话题')
        self.sessions.bind(other_sid, 'om_other_root', 'omt_other')
        await self.watch.create_many([{'chat_id': 'oc_else', 'chat_name': '其他群',
                                       'allow_thread': False}], 'om_other_source', other_sid)
        self.assertEqual([item['id'] for item in self.watch.status_snapshot(self.sid)], [key])
        now = time.time()
        self.db.execute('UPDATE group_watches SET last_checked=? WHERE id=?', (now - 10, key))
        self.assertIn('检查正常', self.watch.status_text(session_id=self.sid, now=now))
        self.db.execute('UPDATE group_watches SET last_checked=? WHERE id=?', (now - 200, key))
        self.assertIn('检查停滞', self.watch.status_text(session_id=self.sid, now=now))
        self.db.execute("UPDATE group_watches SET last_error='读取群消息失败' WHERE id=?", (key,))
        self.assertIn('最近检查异常', self.watch.status_text(session_id=self.sid, now=now))
        self.watch.cancel(key, self.sid)
        self.assertIn('已停止', self.watch.status_text(session_id=self.sid, now=now))

    async def test_classifier_handles_implicit_request_without_group_reply(self):
        from unittest.mock import patch
        from assistant.agent_backends.base import RunResult
        self.cfg.update(AGENT_BACKEND='codex',CODEX_BIN='codex')
        async def model(runner,request):
            self.assertEqual(runner.cfg['AGENT_BACKEND'],'codex')
            self.assertEqual(request.task_kind,'classify')
            self.assertFalse(request.writable_roots)
            payload={'matches':[{'id':'om_implicit','reason':'明确请负责人处理'}]}
            return RunResult(json.dumps(payload),payload)
        with patch('assistant.agent_runner.AgentRunner.run',model):
            matches=await self.watch.classify([{'id':'om_implicit','text':'负责人今天处理一下'}])
        self.assertEqual(matches,{'om_implicit':'明确请负责人处理'})

    async def test_grok_chat_classifies_with_grok(self):
        from unittest.mock import patch
        from assistant.agent_backends.base import RunResult
        self.cfg.update(AGENT_BACKEND='grok',GROK_BIN='grok')
        async def model(runner,request):
            self.assertEqual(runner.cfg['AGENT_BACKEND'],'grok')
            payload={'matches':[{'id':'om_implicit','reason':'明确请负责人处理'}]}
            return RunResult(json.dumps(payload),payload)
        with patch('assistant.agent_runner.AgentRunner.run',model):
            self.assertEqual(await self.watch.classify([{'id':'om_implicit','text':'负责人今天处理一下'}]),
                             {'om_implicit':'明确请负责人处理'})

    async def test_custom_reply_generation_uses_stored_instruction_and_signature(self):
        from unittest.mock import patch
        from assistant.agent_backends.base import RunResult
        self.cfg['AGENT_BACKEND']='claudecode'
        async def model(runner,request):
            self.assertEqual(runner.cfg['AGENT_BACKEND'],'claudecode')
            self.assertEqual(request.task_kind,'classify')
            self.assertIn('认真回答题目',request.prompt)
            self.assertIn('2 加 2 是多少',request.prompt)
            return RunResult('{"reply":"2 加 2 等于 4。"}', {'reply':'2 加 2 等于 4。'})
        with patch('assistant.agent_runner.AgentRunner.run',model):
            reply=await self.watch.compose_reply('认真回答题目','2 加 2 是多少','星河')
        self.assertEqual(reply,'2 加 2 等于 4。\n\nAI 助手代发')

    async def test_group_reply_failure_still_notifies_owner(self):
        await self.create()
        self.messages = [self.message('om_failure', '请看一下', True)]
        self.watch.classify = AsyncMock(return_value={})
        calls = []
        async def send(args, timeout, *rest, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise asyncio.TimeoutError()
            return json.dumps({'ok': True, 'data': {'message_id': 'om_notice'}})
        self.watch.run = send
        await self.watch.tick()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][calls[1].index('--message-id') + 1], 'om_root')
        self.assertIn('群回复失败或结果不确定', calls[1][calls[1].index('--text') + 1])
        self.assertEqual(self.db.execute('SELECT state FROM group_watch_hits').fetchone()[0], 'done')

    async def test_owner_identity_mismatch_prevents_group_reply(self):
        await self.create()
        self.messages = [self.message('om_wrong_identity', '请看一下', True)]
        self.watch.classify = AsyncMock(return_value={})
        self.watch.verify_owner_sender.side_effect = RuntimeError('owner mismatch')
        await self.watch.tick()
        self.assertEqual(self.sent.await_count, 1)
        self.assertEqual(self.sent.await_args.args[0][self.sent.await_args.args[0].index('--as') + 1], 'bot')
        self.assertEqual(self.db.execute('SELECT state FROM group_watch_hits').fetchone()[0], 'done')

    async def test_sender_verification_requires_bound_user_and_scopes(self):
        self.sent.return_value = json.dumps({'identities': {'user': {
            'openId': 'ou_owner', 'verified': True,
            'scope': 'im:message.send_as_user im:message'}}})
        await GroupWatch.verify_owner_sender(self.watch)
        self.sent.return_value = json.dumps({'identities': {'user': {
            'openId': 'ou_other', 'verified': True,
            'scope': 'im:message.send_as_user im:message'}}})
        with self.assertRaisesRegex(RuntimeError, '主人用户身份未核实'):
            await GroupWatch.verify_owner_sender(self.watch)


if __name__ == '__main__':
    unittest.main()
