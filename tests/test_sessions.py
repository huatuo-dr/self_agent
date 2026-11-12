import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from assistant import service
from assistant.group_watch import GroupWatch
from assistant.sessions import Sessions, ThreadError, HINT


class SessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = service.database(':memory:')
        self.s = Sessions(self.db, 'oc_owner')
        self.cfg = {'FEISHU_OWNER_CHAT_ID': 'oc_owner', 'FEISHU_OWNER_OPEN_ID': 'ou_owner',
                    'AGENT_HISTORY_TURNS': 10, 'LARK_CLI_BIN': 'fake'}

    def tearDown(self):
        self.db.close()

    def incoming(self, mid, content='任务', thread=None, root=None, reference=None, kind='text'):
        self.db.execute('INSERT INTO inbox(id,binding,content,kind,acknowledged,reply_to,thread_id,root_id,thread_checked) '
                        'VALUES(?,?,?,?,1,?,?,?,1)', (mid, 'oc_owner', content, kind, reference, thread, root))
        self.db.commit()

    def create(self, source, title='任务', thread=None):
        sid = self.s.create(source, title)
        if thread:
            self.s.bind(sid, source, thread)
        self.db.commit()
        return sid

    async def run_worker(self, send, answer=None, done_count=None):
        done = asyncio.Event()
        sent = []
        async def run(args, *a, **kw):
            result = await send(args)
            if '--markdown' in args:
                sent.append(args)
                if done_count and len(sent) >= done_count:
                    done.set()
            return result
        answer = answer or AsyncMock(side_effect=AssertionError('No model for management commands'))
        with patch.object(service, 'run_command', run), patch.object(service, 'answer', answer):
            task = asyncio.create_task(service.worker(self.cfg, self.db))
            try:
                await asyncio.wait_for(done.wait(), 3)
                await asyncio.sleep(0.02)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return sent

    def test_topic_isolation_and_quotes_do_not_select_a_session(self):
        a = self.create('om_a', '任务甲', 'omt_a')
        b = self.create('om_b', '任务乙', 'omt_b')
        self.s.map_message('om_quote', b, 'om_b', '任务乙回复', 'assistant')
        self.incoming('om_next', reference='om_quote', thread='omt_a', root='om_a')
        self.assertEqual(self.s.route('om_next', '继续', 'omt_a', 'om_a'), (a, 'thread'))
        self.incoming('om_main', reference='om_quote')
        c, reason = self.s.route('om_main', '新的具体问题')
        self.assertNotIn(c, (a, b))
        self.assertEqual(reason, 'new')
        self.s.finish(a, 'om_next', '继续', '回复')
        self.assertEqual(self.s.history(b, 10), [])
        self.assertIsNone(Sessions(self.db, 'oc_other').context('omt_a', 'om_a'))

    def test_main_followup_never_uses_current_or_ai(self):
        old = self.create('om_old', thread='omt_old')
        for text in ('继续', '好的', '确认删除', '同意发送'):
            with self.assertRaises(ThreadError):
                self.s.route('om_new', text)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sessions').fetchone()[0], 1)
        self.assertFalse(hasattr(service, 'choose_session'))
        self.assertFalse(hasattr(self.s, 'current'))

    def test_idempotency_pending_binding_unknown_thread_and_conflicts(self):
        self.incoming('om_first')
        sid, _ = self.s.route('om_first', '分析任务')
        self.assertEqual(self.s.route('om_first', '分析任务')[0], sid)
        self.assertEqual(self.s.context('omt_new', 'om_first'), sid)
        with self.assertRaises(ThreadError):
            self.s.context('omt_other', 'om_first')
        with self.assertRaises(ThreadError):
            self.s.context('omt_new', 'om_bad')
        with self.assertRaises(ThreadError):
            self.s.context('omt_unknown')
        self.incoming('om_else', thread='omt_else', root='om_root')
        other, _ = self.s.route('om_else', '新话题任务', 'omt_else', 'om_root')
        self.assertNotEqual(other, sid)

    def test_directory_pages_without_legacy_resume(self):
        ids = [self.create('om_s'+str(i), str(i)) for i in range(12)]
        page = self.s.handle('session', '', 'view')[0]
        self.assertIn('第 1/2 页', page)
        self.assertNotIn('resume', page)
        self.assertIn('#1 ', self.s.handle('session', 'page 2', 'view')[0])
        self.assertFalse(hasattr(self.s, 'resume'))
        self.assertIn('用法', self.s.handle('session', 'resume 1', 'view')[0])
        self.assertIsNone(self.s.context('omt_legacy', 'om_s0'))
        sid = ids[0]
        self.s.bind(sid, 'om_s0', 'omt_s0', 'https://applink.feishu.cn/example')
        self.assertIn('进入话题', self.s.handle('session', str(sid), 'view')[0])
        self.assertIn('未找到', self.s.handle('session', '9'*100, 'view')[0])

    async def test_worker_commands_and_main_followups_never_call_model(self):
        commands = ['/help', 'help', '/session', '/session 999',
                    '/skill issue', '/issue', '/unknown', '好的', '/save', '/session resume 1']
        for i, content in enumerate(commands):
            self.incoming('om_cmd'+str(i), content)
        async def send(args):
            self.assertIn('+messages-send', args)
            return '{"ok":true,"data":{"message_id":"om_cmdreply"}}'
        await self.run_worker(send, done_count=len(commands))
        self.assertEqual(self.db.execute('SELECT count(*) FROM sessions').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM session_history').fetchone()[0], 0)

    async def test_natural_watch_status_reads_current_thread_without_model(self):
        self.cfg['GROUP_WATCH_SKILL_ENABLED'] = True
        sid = self.create('om_watch_root', '监控群', 'omt_watch')
        watch = GroupWatch(self.cfg, self.db, AsyncMock(),
                           read=lambda request: {'ok': True, 'data': {'items': []}})
        key = (await watch.create_many([{'chat_id': 'oc_group', 'chat_name': '项目群',
                                         'allow_thread': False}], 'om_watch_root', sid))[0][0]
        self.db.execute('UPDATE group_watches SET last_checked=? WHERE id=?',
                        (datetime.now().timestamp(), key))
        self.db.commit()
        self.incoming('om_watch_status', '是否还在监控群消息', 'omt_watch', 'om_watch_root')
        async def send(args):
            body = args[args.index('--markdown') + 1]
            self.assertIn('项目群', body)
            self.assertIn(key, body)
            self.assertIn('检查正常', body)
            return json.dumps({'ok': True, 'data': {'message_id': 'om_status_reply',
                                                   'thread_id': 'omt_watch'}})
        await self.run_worker(send, done_count=1)
        self.assertEqual(self.db.execute('SELECT session_id FROM inbox WHERE id=?',
                                         ('om_watch_status',)).fetchone()[0], sid)

    async def test_worker_updates_watch_hit_notices_from_owner_request(self):
        self.cfg['GROUP_WATCH_SKILL_ENABLED'] = True
        sid = self.create('om_watch_root', '监控群', 'omt_watch')
        watch = GroupWatch(self.cfg, self.db, AsyncMock(),
                           read=lambda request: {'ok': True, 'data': {'items': []}})
        key = (await watch.create_many([{'chat_id': 'oc_group', 'chat_name': '项目群',
                                         'allow_thread': False}], 'om_watch_root', sid))[0][0]
        self.incoming('om_mute_hits', '后面再有@我的消息，就不需要再通知我【群监控命中】了，直接回复',
                      'omt_watch', 'om_watch_root')
        async def send(args):
            self.assertIn('已关闭', args[args.index('--markdown') + 1])
            return json.dumps({'ok': True, 'data': {'message_id': 'om_mute_reply',
                                                   'thread_id': 'omt_watch'}})
        await self.run_worker(send, done_count=1)
        self.assertEqual(self.db.execute('SELECT notify_hits,state FROM group_watches WHERE id=?',
                                         (key,)).fetchone(), (0, 'active'))

    async def test_worker_applies_model_watch_notification_update(self):
        self.cfg['GROUP_WATCH_SKILL_ENABLED'] = True
        sid = self.create('om_watch_root', '监控群', 'omt_watch')
        watch = GroupWatch(self.cfg, self.db, AsyncMock(),
                           read=lambda request: {'ok': True, 'data': {'items': []}})
        key = (await watch.create_many([{'chat_id': 'oc_group', 'chat_name': '项目群',
                                         'allow_thread': False}], 'om_watch_root', sid))[0][0]
        self.incoming('om_quiet_hits', '群里照常答，后续私聊就免了', 'omt_watch', 'om_watch_root')
        async def answer(cfg, history, text, quote, session, **kwargs):
            self.assertEqual(cfg['_group_watch_status'][0]['id'], key)
            return json.dumps({'reply': '正在修改提醒设置',
                               'group_watch_updates': [{'watch_id': key, 'notify_hits': False}],
                               'session': {'title': '监控群', 'summary': '已要求停发命中提醒',
                                           'status': '处理中', 'next_step': '监控群消息'}})
        async def send(args):
            self.assertIn('群监控提醒设置已更新', args[args.index('--markdown') + 1])
            return json.dumps({'ok': True, 'data': {'message_id': 'om_quiet_reply',
                                                   'thread_id': 'omt_watch'}})
        await self.run_worker(send, answer=answer, done_count=1)
        self.assertEqual(self.db.execute('SELECT notify_hits,state FROM group_watches WHERE id=?',
                                         (key,)).fetchone(), (0, 'active'))

    async def test_worker_updates_watch_from_another_topic(self):
        self.cfg['GROUP_WATCH_SKILL_ENABLED'] = True
        original_sid = self.create('om_watch_root', '监控群', 'omt_watch')
        watch = GroupWatch(self.cfg, self.db, AsyncMock(),
                           read=lambda request: {'ok': True, 'data': {'items': []}})
        key = (await watch.create_many([{'chat_id': 'oc_group', 'chat_name': '项目群',
                                         'allow_thread': False}], 'om_watch_root', original_sid))[0][0]
        self.incoming('om_new_topic', '项目群照常回复，但命中后别在私聊打断我')
        async def answer(cfg, history, text, quote, session, **kwargs):
            self.assertEqual(cfg['_group_watch_status'][0]['id'], key)
            return json.dumps({'reply': '正在修改提醒设置',
                               'group_watch_updates': [{'watch_id': key, 'notify_hits': False}],
                               'session': {'title': '调整项目群监控', 'summary': '停发命中提醒',
                                           'status': '已完成', 'next_step': ''}})
        async def send(args):
            self.assertIn('群监控提醒设置已更新', args[args.index('--markdown') + 1])
            return json.dumps({'ok': True, 'data': {'message_id': 'om_update_reply',
                                                   'thread_id': 'omt_new'}})
        await self.run_worker(send, answer=answer, done_count=1)
        self.assertEqual(self.db.execute('SELECT notify_hits FROM group_watches WHERE id=?',
                                         (key,)).fetchone(), (0,))
        new_sid = self.db.execute("SELECT session_id FROM inbox WHERE id='om_new_topic'").fetchone()[0]
        self.assertEqual(self.s.get(new_sid)['status'], '已完成')

    async def test_worker_cancels_watch_from_natural_language(self):
        self.cfg['GROUP_WATCH_SKILL_ENABLED'] = True
        sid = self.create('om_watch_root', '参与群活动', 'omt_watch')
        watch = GroupWatch(self.cfg, self.db, AsyncMock(),
                           read=lambda request: {'ok': True, 'data': {'items': []}})
        key = (await watch.create_many([{'chat_id': 'oc_group', 'chat_name': '项目群',
                                         'allow_thread': False}], 'om_watch_root', sid))[0][0]
        self.incoming('om_stop_activity', '可以停止参与活动了', 'omt_watch', 'om_watch_root')
        async def send(args):
            self.assertIn('已停止监控', args[args.index('--markdown') + 1])
            return json.dumps({'ok': True, 'data': {'message_id': 'om_stop_reply',
                                                   'thread_id': 'omt_watch'}})
        await self.run_worker(send, done_count=1)
        self.assertEqual(self.db.execute('SELECT state FROM group_watches WHERE id=?',
                                         (key,)).fetchone(), ('cancelled',))

    async def test_worker_applies_model_group_watch_cancel(self):
        self.cfg['GROUP_WATCH_SKILL_ENABLED'] = True
        sid = self.create('om_watch_root', '参与群活动', 'omt_watch')
        watch = GroupWatch(self.cfg, self.db, AsyncMock(),
                           read=lambda request: {'ok': True, 'data': {'items': []}})
        key = (await watch.create_many([{'chat_id': 'oc_group', 'chat_name': '项目群',
                                         'allow_thread': False}], 'om_watch_root', sid))[0][0]
        self.incoming('om_end_participation', '项目群那边不用继续了，后面别替我发言',
                      'omt_watch', 'om_watch_root')
        async def answer(cfg, history, text, quote, session, **kwargs):
            self.assertEqual(cfg['_group_watch_status'][0]['id'], key)
            return json.dumps({'reply': '正在停止群里代答', 'group_watch_cancels': [key],
                               'session': {'title': '参与群活动', 'summary': '停止群监控',
                                           'status': '处理中', 'next_step': '监控群消息'}})
        async def send(args):
            self.assertIn('群监控已取消', args[args.index('--markdown') + 1])
            return json.dumps({'ok': True, 'data': {'message_id': 'om_end_reply',
                                                   'thread_id': 'omt_watch'}})
        await self.run_worker(send, answer=answer, done_count=1)
        self.assertEqual(self.db.execute('SELECT state FROM group_watches WHERE id=?',
                                         (key,)).fetchone(), ('cancelled',))
        self.assertEqual(self.s.get(sid)['status'], '已完成')

    async def test_complex_watch_question_receives_live_state_snapshot(self):
        self.cfg['GROUP_WATCH_SKILL_ENABLED'] = True
        sid = self.create('om_watch_root', '监控群', 'omt_watch')
        watch = GroupWatch(self.cfg, self.db, AsyncMock(),
                           read=lambda request: {'ok': True, 'data': {'items': []}})
        key = (await watch.create_many([{'chat_id': 'oc_group', 'chat_name': '项目群',
                                         'allow_thread': False}], 'om_watch_root', sid))[0][0]
        self.incoming('om_watch_why', '为什么监控状态不正常？', 'omt_watch', 'om_watch_root')
        async def answer(cfg, history, text, quote, session, **kwargs):
            self.assertEqual(cfg['_group_watch_status'][0]['id'], key)
            self.assertEqual(cfg['_group_watch_status'][0]['state'], 'active')
            return json.dumps({'reply': '正在排查', 'session': {'title': '监控群',
                'summary': '监控状态排查', 'status': '等待回复', 'next_step': '继续排查'}})
        async def send(args):
            return json.dumps({'ok': True, 'data': {'message_id': 'om_why_reply',
                                                   'thread_id': 'omt_watch'}})
        await self.run_worker(send, answer=answer, done_count=1)

    async def test_history_command_returns_original_thread_without_new_session(self):
        sid = self.create('om_original', '仓库排障', 'omt_original')
        self.s.bind(sid, 'om_original', 'omt_original', 'https://applink.feishu.cn/example/original')
        self.s.finish(sid, 'om_original', '排查固件构建失败', '已定位编译错误')
        self.db.commit()
        self.incoming('om_lookup', '/history 固件')

        async def send(args):
            body = args[args.index('--markdown') + 1]
            self.assertIn('仓库排障', body)
            self.assertIn('https://applink.feishu.cn/example/original', body)
            self.assertIn('请进入原话题继续任务', body)
            return '{"ok":true,"data":{"message_id":"om_searchreply"}}'

        await self.run_worker(send, done_count=1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sessions').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM session_history').fetchone()[0], 2)

    async def test_card_waits_for_instruction_in_its_own_thread(self):
        card = '{"header":{"title":{"content":"交付通知"}},"elements":[{"text":{"content":"周五交付"}}]}'
        self.incoming('om_card', card, kind='interactive')

        async def send_card_reply(args):
            self.assertIn('+messages-reply', args)
            self.assertEqual(args[args.index('--message-id') + 1], 'om_card')
            self.assertIn('--reply-in-thread', args)
            self.assertIn('已收到这张卡片', args[args.index('--markdown') + 1])
            return json.dumps({'ok': True, 'data': {'message_id': 'om_card_reply',
                                                    'thread_id': 'omt_card'}})

        await self.run_worker(send_card_reply, done_count=1)
        sid = self.db.execute("SELECT session_id FROM inbox WHERE id='om_card'").fetchone()[0]
        self.assertEqual(self.s.get(sid)['status'], '等待回复')
        self.assertEqual(self.s.thread(sid)['root_id'], 'om_card')
        self.assertIn('周五交付', self.s.history(sid, 10)[0][1])
        self.assertEqual(self.db.execute("SELECT state FROM inbox WHERE id='om_card'").fetchone()[0], 'done')

        self.incoming('om_card_question', '帮我总结一下消息内容', thread='omt_card', root='om_card')
        async def answer(cfg, history, text, quote, session):
            self.assertEqual(session['id'], sid)
            self.assertIn('周五交付', history[0][1])
            self.assertEqual(text, '帮我总结一下消息内容')
            return json.dumps({'reply': '卡片写明周五交付。',
                               'session': {'title': '飞书卡片', 'summary': '周五交付',
                                           'status': '已完成', 'next_step': ''}})
        async def send_answer(args):
            self.assertEqual(args[args.index('--message-id') + 1], 'om_card')
            self.assertIn('周五交付', args[args.index('--markdown') + 1])
            return json.dumps({'ok': True, 'data': {'message_id': 'om_card_answer',
                                                    'thread_id': 'omt_card'}})
        await self.run_worker(send_answer, answer, done_count=1)
        self.assertEqual(self.db.execute("SELECT session_id FROM inbox WHERE id='om_card_question'").fetchone()[0], sid)

    async def test_merged_forward_is_loaded_and_waits_in_its_own_thread(self):
        self.incoming('om_forward', 'Merged and Forwarded Message', kind='merge_forward')
        transcript = '<forwarded_messages>\n甲：周五交付\n乙：我来验收\n</forwarded_messages>'
        async def send_ack(args):
            self.assertIn('+messages-reply', args)
            self.assertEqual(args[args.index('--message-id') + 1], 'om_forward')
            self.assertIn('已收到这段群聊会话记录', args[args.index('--markdown') + 1])
            return json.dumps({'ok': True, 'data': {'message_id': 'om_forward_ack',
                                                    'thread_id': 'omt_forward'}})
        metadata = {'om_forward': {'message_id': 'om_forward', 'msg_type': 'merge_forward',
                                   'content': transcript}}
        with patch.object(service, 'message_metadata', AsyncMock(return_value=metadata)):
            await self.run_worker(send_ack, done_count=1)
        sid = self.db.execute("SELECT session_id FROM inbox WHERE id='om_forward'").fetchone()[0]
        self.assertEqual(self.s.get(sid)['status'], '等待回复')
        self.assertIn('乙：我来验收', self.s.history(sid, 10)[0][1])
        self.assertEqual(self.db.execute("SELECT content FROM inbox WHERE id='om_forward'").fetchone()[0], transcript)
        self.assertEqual(self.s.thread(sid)['root_id'], 'om_forward')

        self.incoming('om_forward_question', '总结并列出待办', thread='omt_forward', root='om_forward')
        async def answer(cfg, history, text, quote, session):
            self.assertEqual(session['id'], sid)
            self.assertIn('甲：周五交付', history[0][1])
            return json.dumps({'reply': '周五交付，乙负责验收。',
                               'session': {'title': '交付讨论', 'summary': '周五交付',
                                           'status': '已完成', 'next_step': ''}})
        async def send_summary(args):
            self.assertEqual(args[args.index('--message-id') + 1], 'om_forward')
            return json.dumps({'ok': True, 'data': {'message_id': 'om_forward_summary',
                                                    'thread_id': 'omt_forward'}})
        await self.run_worker(send_summary, answer, done_count=1)
        self.assertEqual(self.db.execute("SELECT session_id FROM inbox WHERE id='om_forward_question'").fetchone()[0], sid)

    async def test_quote_of_earlier_unsupported_merged_forward_fetches_full_record(self):
        self.incoming('om_old_forward', 'Merged and Forwarded Message', kind='merge_forward')
        metadata = {'om_old_forward': {'message_id': 'om_old_forward', 'msg_type': 'merge_forward',
                                       'content': '<forwarded_messages>\n甲：请验收\n</forwarded_messages>'}}
        with patch.object(service, 'message_metadata', AsyncMock(return_value=metadata)):
            quote = await service.quoted_message(self.cfg, self.db, 'om_old_forward')
        self.assertIn('请验收', quote['content'])

    async def test_worker_sends_all_parts_to_same_thread_and_retains_history(self):
        self.incoming('om_task', '分析构建')
        mids = []
        async def answer(cfg, history, text, quote, session):
            self.assertEqual(cfg['_delivery_root'], 'om_task')
            return json.dumps({'reply': '结果'*1500, 'session': {'title':'构建排查','summary':'缺日志','status':'等待回复','next_step':'补日志'}})
        async def send(args):
            self.assertIn('+messages-reply', args)
            self.assertIn('--reply-in-thread', args)
            self.assertEqual(args[args.index('--message-id')+1], 'om_task')
            self.assertIn('session ID：#1', args[args.index('--markdown')+1])
            mid = 'om_part'+str(len(mids))
            mids.append(mid)
            return json.dumps({'ok': True, 'data': {'message_id': mid, 'thread_id': 'omt_task'}})
        await self.run_worker(send, answer, done_count=2)
        self.assertEqual(len(self.s.history(1, 10)), 2)
        self.assertEqual(self.s.thread(1)['thread_id'], 'omt_task')
        for mid in mids:
            self.assertEqual(self.s.reference(mid)['session_id'], 1)
        self.assertEqual(self.db.execute("SELECT state FROM inbox WHERE id='om_task'").fetchone()[0], 'done')

    async def test_worker_persists_model_schedule_before_reply(self):
        self.incoming('om_reminder', '明天提醒我检查结果')
        when = (datetime.now(ZoneInfo('Asia/Shanghai')) + timedelta(days=1)).isoformat()
        async def answer(cfg, history, text, quote, session):
            return json.dumps({'reply': '已准备提醒', 'schedule': {
                'kind': 'message', 'body': '检查结果', 'run_at': when, 'repeat': 'none'},
                'session': {'title': '结果提醒', 'summary': '待定时发送',
                            'status': '处理中', 'next_step': '等待定时消息发送'}})
        async def send(args):
            self.assertIn('定时任务已创建', args[args.index('--markdown')+1])
            return '{"ok":true,"data":{"message_id":"om_reminder_reply","thread_id":"omt_reminder"}}'
        await self.run_worker(send, answer, done_count=1)
        self.assertEqual(self.db.execute('SELECT state,body FROM scheduled_jobs').fetchone(),
                         ('pending', '检查结果'))
        self.assertEqual(self.s.get(1)['next_step'], '等待定时消息发送')

    async def test_worker_creates_gitlab_target_watch_before_reply(self):
        from assistant.target_watch import TargetWatch
        url = 'https://gitlab.example.com/group/project/-/issues/75'
        self.cfg.update(ISSUE_SKILL_ENABLED=True, GITLAB_HOST='gitlab.example.com', GLAB_BIN='glab')
        self.incoming('om_watch_request', '每 10 分钟监控这个 Issue 的新回复')
        async def answer(cfg, history, text, quote, session):
            return json.dumps({'reply': '正在建立监控', 'issue_refs': [url], 'notification': None,
                               'target_watch': [{'url': url, 'event': 'new_comment',
                                                 'interval_minutes': 10, 'mode': 'once'}],
                               'session': {'title': '监控 Issue 回复', 'summary': '准备监控 #75',
                                           'status': '处理中', 'next_step': '监控 GitLab 目标'}})
        async def send(args):
            self.assertIn('GitLab 目标监控已创建', args[args.index('--markdown')+1])
            return '{"ok":true,"data":{"message_id":"om_watch_reply","thread_id":"omt_watch"}}'
        with patch.object(TargetWatch, 'snapshot', AsyncMock(return_value=3)):
            await self.run_worker(send, answer, done_count=1)
        self.assertEqual(self.db.execute('SELECT url,baseline_id,state FROM target_watches').fetchone(),
                         (url, 3, 'active'))
        self.assertEqual(self.s.get(1)['next_step'], '监控 GitLab 目标')

    async def test_rich_text_numbered_list_creates_two_reminders(self):
        message = ('帮我执行两个定时任务，\n1. 3分钟后给我发消息通知我写日报；\n'
                   '2. 10分钟后给我发消息通知我买咖啡；')
        self.incoming('om_rich', message, kind='post')
        now = datetime.now(ZoneInfo('Asia/Shanghai'))
        proposals = [
            {'kind': 'message', 'body': '写日报',
             'run_at': (now + timedelta(minutes=3)).isoformat(), 'repeat': 'none'},
            {'kind': 'message', 'body': '买咖啡',
             'run_at': (now + timedelta(minutes=10)).isoformat(), 'repeat': 'none'}]
        async def answer(cfg, history, text, quote, session):
            self.assertEqual(text, message)
            return json.dumps({'reply': '正在创建提醒', 'schedule': proposals,
                               'session': {'title': '两个提醒', 'summary': '待定时发送',
                                           'status': '处理中', 'next_step': '等待定时消息发送'}})
        async def send(args):
            body = args[args.index('--markdown') + 1]
            self.assertIn('定时任务已创建', body)
            self.assertNotIn('目前支持文字消息', body)
            return '{"ok":true,"data":{"message_id":"om_rich_reply","thread_id":"omt_rich"}}'
        await self.run_worker(send, answer, done_count=1)
        self.assertEqual(self.db.execute('SELECT body FROM scheduled_jobs ORDER BY run_at').fetchall(),
                         [('写日报',), ('买咖啡',)])

    async def test_image_message_reaches_model_and_keeps_key_out_of_history(self):
        self.incoming('om_photo', '[Image: img_v3_private]', kind='image')
        async def answer(cfg, history, text, quote, session, image_message_ids, image_db):
            self.assertEqual(image_message_ids, ['om_photo'])
            self.assertIs(image_db, self.db)
            self.assertIn('图片', text)
            self.assertNotIn('img_v3_private', text)
            return json.dumps({'reply': '图片中有一只猫', 'schedule': [],
                               'session': {'title': '查看图片', 'summary': '已描述图片',
                                           'status': '已完成', 'next_step': ''}})
        async def send(args):
            body = args[args.index('--markdown') + 1]
            self.assertIn('图片中有一只猫', body)
            self.assertNotIn('目前支持文字消息', body)
            return '{"ok":true,"data":{"message_id":"om_photo_reply","thread_id":"omt_photo"}}'
        await self.run_worker(send, answer, done_count=1)
        self.assertNotIn('img_v3_private', str(self.s.history(1, 10)))

    async def test_text_reply_to_image_attaches_quoted_image(self):
        self.incoming('om_photo', '[Image: img_v3_private]', kind='image')
        self.db.execute("UPDATE inbox SET state='done' WHERE id='om_photo'")
        self.incoming('om_question', '这张图是什么？', reference='om_photo')
        async def answer(cfg, history, text, quote, session, image_message_ids, image_db):
            self.assertEqual(text, '这张图是什么？')
            self.assertEqual(quote['content'], '[用户发送图片]')
            self.assertEqual(image_message_ids, ['om_photo'])
            return json.dumps({'reply': '图片内容', 'schedule': [],
                               'session': {'title': '图片提问', 'summary': '已回答',
                                           'status': '已完成', 'next_step': ''}})
        async def send(args):
            return '{"ok":true,"data":{"message_id":"om_question_reply","thread_id":"omt_question"}}'
        await self.run_worker(send, answer, done_count=1)

    async def test_thread_controls_reply_in_place_without_task_history(self):
        sid = self.create('om_root', thread='omt_root')
        self.incoming('om_save', '/save', 'omt_root', 'om_root')
        async def send(args):
            self.assertEqual(args[args.index('--message-id')+1], 'om_root')
            return '{"ok":true,"data":{"message_id":"om_saved","thread_id":"omt_root"}}'
        await self.run_worker(send, done_count=1)
        self.assertEqual(self.s.get(sid)['saved'], 1)
        self.assertEqual(self.s.history(sid, 10), [])

    async def test_cross_thread_reference_cannot_select_approval_scope(self):
        a = self.create('om_a', thread='omt_a')
        b = self.create('om_b', thread='omt_b')
        self.s.map_message('om_review_b', b, 'om_source_b', '另一话题的通知草稿', 'assistant')
        self.incoming('om_confirm', '好的', 'omt_a', 'om_a', 'om_review_b')
        self.cfg.update(ISSUE_SKILL_ENABLED=True, GITLAB_HOST='gitlab.example.com')
        async def control(instance, text, source, session_id=None, review_source=None):
            self.assertEqual(session_id, a)
            self.assertEqual(review_source, '__other_thread__')
            return '未发送'
        async def send(args):
            self.assertEqual(args[args.index('--message-id')+1], 'om_a')
            return '{"ok":true,"data":{"message_id":"om_denied","thread_id":"omt_a"}}'
        with patch('assistant.issues.IssueAssistant.handle_command', control):
            await self.run_worker(send, done_count=1)
        self.assertEqual(self.s.history(a, 10), [])

    async def test_thread_root_reply_approves_latest_review(self):
        from assistant.issues import IssueAssistant
        sid = self.create('om_root', thread='omt_root')
        self.s.map_message('om_root', sid, 'om_root', '任务起点', 'user')
        self.cfg.update(ISSUE_SKILL_ENABLED=True, GITLAB_HOST='gitlab.example.com')
        issues = IssueAssistant(self.cfg, self.db, AsyncMock(), service.ROOT, service.STATE)
        self.incoming('om_review', '准备通知', 'omt_root', 'om_root')
        self.db.execute("UPDATE inbox SET state='done',session_id=? WHERE id='om_review'", (sid,))
        self.db.execute('INSERT INTO issue_notices(id,scope,source,payload,session_id,reviewed) '
                        'VALUES(?,?,?,?,?,1)',
                        ('1234567890', issues.scope, 'om_review',
                         json.dumps({'body': '通知', 'recipients': [{'open_id': 'ou_colleague'}]}), sid))
        self.incoming('om_confirm', '@AI 助手 同意', 'omt_root', 'om_root', 'om_root')
        async def send(args):
            self.assertEqual(args[args.index('--message-id')+1], 'om_root')
            return '{"ok":true,"data":{"message_id":"om_sent","thread_id":"omt_root"}}'
        with patch.object(IssueAssistant, 'confirm', AsyncMock(return_value='已发送')) as confirm:
            await self.run_worker(send, done_count=1)
            confirm.assert_awaited_once_with('1234567890', sid)

    async def test_cleaned_topic_response_stays_in_topic_and_never_calls_model(self):
        from assistant.management import Management
        sid = self.create('om_root', thread='omt_root')
        Management(self.db, 'oc_owner').delete(sid)
        self.incoming('om_after_delete', '继续', 'omt_root', 'om_root')
        async def send(args):
            self.assertIn('--reply-in-thread', args)
            self.assertEqual(args[args.index('--message-id')+1], 'om_after_delete')
            self.assertIn('已清理', args[args.index('--markdown')+1])
            return '{"ok":true,"data":{"message_id":"om_deleted_reply","thread_id":"omt_root"}}'
        await self.run_worker(send, done_count=1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sessions').fetchone()[0], 0)

    async def test_old_unmapped_quote_in_cleaned_thread_does_not_restore_content(self):
        from assistant.management import Management
        sid = self.create('om_root', thread='omt_root')
        Management(self.db, 'oc_owner').delete(sid)
        response = {'ok': True, 'data': {'messages': [{'message_id': 'om_unseen', 'chat_id': 'oc_owner',
                    'thread_id': 'omt_root', 'content': 'old private context'}]}}
        with patch.object(service, 'run_command', AsyncMock(return_value=json.dumps(response))):
            result = await service.quoted_message(self.cfg, self.db, 'om_unseen')
        self.assertIn('已清理', result['error'])
        self.assertNotIn('old private context', str(result))

    async def test_failed_task_preserves_summary_in_original_thread(self):
        sid = self.create('om_old', '构建', 'omt_old')
        self.s.finish(sid, 'prior', '需求', '进展', {'summary':'已定位编译器版本','status':'等待回复'})
        self.incoming('om_fail', '继续', 'omt_old', 'om_old')
        async def send(args):
            self.assertEqual(args[args.index('--message-id')+1], 'om_old')
            self.assertIn('处理超过', args[args.index('--markdown')+1])
            return '{"ok":true,"data":{"message_id":"om_failure","thread_id":"omt_old"}}'
        await self.run_worker(send, AsyncMock(side_effect=asyncio.TimeoutError), 1)
        self.assertEqual(self.s.get(sid)['status'], '受阻')
        self.assertIn('已定位编译器版本', self.s.get(sid)['summary'])

    async def test_thread_lookup_timeout_names_thread_lookup_only(self):
        self.incoming('om_lookup', '新任务')
        async def send(args):
            body = args[args.index('--markdown')+1]
            self.assertIn('飞书消息信息超时', body)
            self.assertIn('任务尚未开始', body)
            return '{"ok":true}'
        with patch.object(service, 'thread_metadata', AsyncMock(side_effect=asyncio.TimeoutError)):
            await self.run_worker(send, done_count=1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sessions').fetchone()[0], 0)

    async def test_model_failure_does_not_blame_thread_lookup(self):
        self.incoming('om_model_failure', '新任务')
        async def send(args):
            body = args[args.index('--markdown')+1]
            self.assertIn('任务执行过程中发生异常', body)
            self.assertNotIn('话题归属', body)
            return json.dumps({'ok':True,'data':{'message_id':'om_model_failure_reply',
                                                 'thread_id':'omt_model_failure'}})
        await self.run_worker(send, AsyncMock(side_effect=RuntimeError('private detail')), 1)

    async def test_codex_failure_names_model_call_in_notice(self):
        self.incoming('om_codex_failure', '新任务')
        async def send(args):
            body = args[args.index('--markdown')+1]
            self.assertIn('AI 工具执行失败', body)
            self.assertIn('未自动重试', body)
            return json.dumps({'ok':True,'data':{'message_id':'om_codex_failure_reply',
                                                 'thread_id':'omt_codex_failure'}})
        await self.run_worker(send, AsyncMock(side_effect=service.AgentError('AI 工具执行失败')), 1)

    async def test_long_task_progress_and_fast_cancel_stay_in_original_thread(self):
        self.incoming('om_long', '处理大型任务')
        self.cfg.update(AGENT_PROGRESS_FIRST_SECONDS=0.03,
                        AGENT_PROGRESS_INTERVAL_SECONDS=0.3,
                        AGENT_TIMEOUT_SECONDS=1800)
        progress_sent, final_sent = asyncio.Event(), asyncio.Event()
        sent = []
        async def answer(*args, **kwargs):
            await asyncio.Event().wait()
        async def run(args, *unused, **kwargs):
            if '--markdown' in args:
                body = args[args.index('--markdown')+1]
                sent.append((args, body))
                if '仍在处理' in body:
                    progress_sent.set()
                if '任务已取消' in body:
                    final_sent.set()
                return json.dumps({'ok':True,'data':{'message_id':'om_sent'+str(len(sent)),
                                                     'thread_id':'omt_long'}})
            raise AssertionError(args)
        with patch.object(service, 'answer', answer), patch.object(service, 'run_command', run):
            normal = asyncio.create_task(service.worker(self.cfg, self.db))
            fast = asyncio.create_task(service.worker(self.cfg, self.db, status_only=True))
            try:
                await asyncio.wait_for(progress_sent.wait(), 2)
                self.incoming('om_cancel', '/cancel #1')
                self.db.execute("UPDATE inbox SET fast_lane=1 WHERE id='om_cancel'")
                self.db.commit()
                await asyncio.wait_for(final_sent.wait(), 2)
                self.assertEqual(self.db.execute("SELECT state FROM task_runs WHERE source='om_long'").fetchone()[0], 'failed')
                self.assertEqual(self.db.execute("SELECT count(*) FROM task_updates WHERE source='om_long' AND sent=1").fetchone()[0], 1)
                self.assertEqual(self.s.get(1)['status'], '受阻')
                self.assertEqual(len(self.s.history(1, 10)), 2)
                for args, body in sent:
                    if '仍在处理' in body or '任务已取消' in body:
                        self.assertEqual(args[args.index('--message-id')+1], 'om_long')
            finally:
                normal.cancel(); fast.cancel()
                await asyncio.gather(normal, fast, return_exceptions=True)

    async def test_interrupted_task_is_reported_without_replaying_model(self):
        self.incoming('om_interrupted', '执行可能有写操作的任务')
        sid, _ = self.s.route('om_interrupted', '执行可能有写操作的任务')
        from assistant.progress import start
        start(self.db, 'om_interrupted', 'oc_owner', sid)
        async def send(args):
            body = args[args.index('--markdown')+1]
            self.assertIn('中断', body)
            return json.dumps({'ok':True,'data':{'message_id':'om_interrupted_reply',
                                                 'thread_id':'omt_interrupted'}})
        await self.run_worker(send, AsyncMock(side_effect=AssertionError('must not replay')), 1)
        self.assertEqual(self.s.get(sid)['status'], '受阻')
        self.assertEqual(self.db.execute("SELECT state FROM task_runs WHERE source='om_interrupted'").fetchone()[0], 'failed')

    async def test_metadata_failure_after_send_does_not_resend_on_restart(self):
        self.incoming('om_task', '新任务')
        sid, _ = self.s.route('om_task', '新任务')
        self.db.execute("UPDATE inbox SET reply='结果',reply_version=2,sent=1 WHERE id='om_task'")
        self.db.commit()
        async def run(args, *a, **kw):
            self.assertIn('+messages-mget', args)
            return json.dumps({'ok':True,'data':{'messages':[{'message_id':'om_task','chat_id':'oc_owner','thread_id':'omt_recovered'}]}})
        with patch.object(service, 'run_command', run), patch.object(service, 'answer', AsyncMock(side_effect=AssertionError())):
            worker = asyncio.create_task(service.worker(self.cfg, self.db))
            try:
                for _ in range(30):
                    if self.db.execute("SELECT state FROM inbox WHERE id='om_task'").fetchone()[0] == 'done':
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(self.s.thread(sid)['thread_id'], 'omt_recovered')
                self.assertEqual(self.db.execute("SELECT state FROM inbox WHERE id='om_task'").fetchone()[0], 'done')
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    async def test_real_cli_shape_reads_thread_after_reply_and_builds_directory_link(self):
        self.incoming('om_task', '新任务')
        link = 'https://applink.feishu.cn/client/thread/open?open_thread_id=omt_task'
        metadata = {'ok': True, 'data': {'messages': [{'message_id': 'om_task', 'chat_id': 'oc_owner',
                    'thread_id': 'omt_task', 'message_app_link': link}]}}
        async def send(args):
            if '+messages-mget' in args:
                return json.dumps(metadata)
            return '{"ok":true,"data":{"message_id":"om_reply","chat_id":"oc_owner"}}'
        answer = AsyncMock(return_value=json.dumps({'reply': '完成', 'session': {'status': '已完成'}}))
        sent = await self.run_worker(send, answer, 1)
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.s.thread(1)['thread_id'], 'omt_task')
        with patch.object(service, 'run_command', AsyncMock(return_value=json.dumps(metadata))):
            await service.refresh_thread_links(self.cfg, self.s, [1])
        self.assertIn(link, self.s.handle('session', '', 'om_list')[0])

    async def test_missing_thread_metadata_is_verified_not_inferred_from_quote(self):
        self.incoming('om_quoted', reference='om_parent')
        async def run(*a, **kw):
            return json.dumps({'ok':True,'data':{'messages':[{'message_id':'om_quoted','chat_id':'oc_owner','root_id':'om_parent'}]}})
        with patch.object(service, 'run_command', run):
            self.assertEqual(await service.thread_metadata(self.cfg, self.db, 'om_quoted', None, 'om_parent', False), (None, 'om_parent'))
        self.create('om_parent', thread='omt_parent')
        response = {'ok': True, 'data': {'messages': [{'message_id': 'om_quoted', 'chat_id': 'oc_owner', 'thread_id': 'omt_parent'}]}}
        with patch.object(service, 'run_command', AsyncMock(return_value=json.dumps(response))):
            self.assertEqual(await service.thread_metadata(self.cfg, self.db, 'om_quoted', None, None, False), ('omt_parent', 'om_parent'))
        with patch.object(service, 'run_command', AsyncMock(side_effect=RuntimeError())):
            with self.assertRaises(RuntimeError):
                await service.thread_metadata(self.cfg, self.db, 'om_quoted', None, 'om_parent', False)

    def test_event_fields_and_tombstones_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'db'
            db = service.database(path)
            event = {'chat_type':'p2p','sender_type':'user','sender_id':'ou_owner','chat_id':'oc_owner',
                     'message_id':'om_event','message_type':'text','content':'继续','thread_id':'omt_event','root_id':'om_root'}
            service.enqueue(db, self.cfg, event)
            sessions = Sessions(db, 'oc_owner')
            sid, _ = sessions.route('om_event', '继续', 'omt_event', 'om_root')
            db.close()
            db = service.database(path)
            sessions = Sessions(db, 'oc_owner')
            self.assertEqual(sessions.context('omt_event', 'om_root'), sid)
            from assistant.management import Management
            Management(db, 'oc_owner').delete(sid)
            db.commit()
            db.close()
            db = service.database(path)
            with self.assertRaises(ThreadError):
                Sessions(db, 'oc_owner').context('omt_event', 'om_root')
            self.assertEqual(db.execute("SELECT content FROM inbox WHERE id='om_event'").fetchone()[0], '')
            db.close()
