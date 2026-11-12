import asyncio
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import AsyncMock

from assistant.schedule import Schedule
from assistant.service import database
from assistant.sessions import Sessions
from assistant.target_watch import TargetWatch, flatten_notes, target_endpoint


ISSUE = 'https://gitlab.example.com/group/project/-/issues/75'
MR = 'https://gitlab.example.com/group/project/-/merge_requests/12'


def discussion(*notes):
    return [{'id': 'thread', 'notes': list(notes)}]


def note(nid, *, system=False, author='同事', created_at='2020-01-01T00:00:00Z'):
    return {'id': nid, 'system': system, 'author': {'name': author},
            'created_at': created_at, 'body': '回复'}


class TargetWatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = database(':memory:')
        self.cfg = {'FEISHU_OWNER_CHAT_ID': 'oc_owner', 'GITLAB_HOST': 'gitlab.example.com',
                    'GLAB_BIN': 'glab', 'LARK_CLI_BIN': 'lark-cli', 'ISSUE_SKILL_ENABLED': True}
        self.run = AsyncMock(return_value=json.dumps({'ok': True, 'data': {'message_id': 'om_notice'}}))
        self.jobs = Schedule(self.cfg, self.db, self.run)
        self.watch = TargetWatch(self.cfg, self.db, self.run)
        self.sessions = Sessions(self.db, 'oc_owner')
        self.sid = self.sessions.create('om_source', '监控 Issue')
        self.sessions.bind(self.sid, 'om_root', 'omt_thread')
        self.db.execute("INSERT INTO inbox(id,binding,session_id,state) VALUES('om_source','oc_owner',?,'done')",
                        (self.sid,))
        self.db.execute("UPDATE sessions SET status='处理中',next_step='监控 GitLab 目标' WHERE id=?",
                        (self.sid,))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def proposal(self, url=ISSUE, mode='once'):
        return {'url': url, 'event': 'new_comment', 'interval_minutes': 10, 'mode': mode}

    async def create(self, url=ISSUE, mode='once', baseline=0):
        self.watch.snapshot = AsyncMock(return_value=baseline)
        (key, created), = await self.watch.create_many([self.proposal(url, mode)], 'om_source', self.sid)
        self.assertTrue(created)
        return key, self.db.execute('SELECT next_check FROM target_watches WHERE id=?', (key,)).fetchone()[0]

    async def test_target_detail_uses_instance_timezone_without_changing_timestamps(self):
        key, due = await self.create()
        self.cfg['TIMEZONE'] = 'America/New_York'
        from zoneinfo import ZoneInfo
        local = datetime.fromtimestamp(due, ZoneInfo('America/New_York')).strftime('%Y-%m-%d %H:%M')
        self.assertIn(local, self.watch.detail(key, self.sid))
        other = TargetWatch({**self.cfg, 'TIMEZONE': 'UTC'}, self.db, self.run)
        utc = datetime.fromtimestamp(due, timezone.utc).strftime('%Y-%m-%d %H:%M')
        self.assertIn(utc, other.detail(key, self.sid))
        self.assertIn(local, self.watch.detail(key, self.sid))
        self.assertEqual(self.db.execute('SELECT next_check FROM target_watches WHERE id=?', (key,)).fetchone()[0], due)

    async def test_existing_comments_are_baseline_and_first_new_reply_completes(self):
        key, due = await self.create(baseline=9)
        self.watch.api = AsyncMock(return_value=discussion(note(8), note(9)))
        await self.watch.tick(due + 1)
        self.run.assert_not_awaited()
        self.assertEqual(self.db.execute('SELECT baseline_id,state FROM target_watches').fetchone(),
                         (9, 'active'))
        self.watch.api.return_value = discussion(note(8), note(9), note(10, system=True), note(11))
        await self.watch.tick(due + 601)
        self.assertEqual(self.db.execute('SELECT baseline_id,state FROM target_watches').fetchone(),
                         (11, 'sent'))
        self.assertEqual(self.run.await_count, 1)
        args = self.run.call_args.args[0]
        self.assertIn('--reply-in-thread', args)
        self.assertIn(ISSUE, args[args.index('--markdown')+1])
        self.assertIn('1 条', args[args.index('--markdown')+1])
        self.assertEqual(self.sessions.get(self.sid)['status'], '已完成')
        await self.watch.tick(due + 1201)
        self.assertEqual(self.run.await_count, 1)
        self.assertIn('sent', self.jobs.command(key, self.sid))

    async def test_mr_thread_reply_continues_and_system_notes_do_not_notify(self):
        key, due = await self.create(url=MR, mode='continuous', baseline=20)
        self.watch.api = AsyncMock(return_value=discussion(note(21, system=True)))
        await self.watch.tick(due + 1)
        self.assertEqual(self.db.execute('SELECT baseline_id FROM target_watches').fetchone(), (21,))
        self.run.assert_not_awaited()
        self.watch.api.return_value = discussion(note(21, system=True), note(22))
        await self.watch.tick(due + 601)
        self.assertEqual(self.db.execute('SELECT state,baseline_id FROM target_watches').fetchone(),
                         ('active', 22))
        self.assertIn('MR', self.run.call_args.args[0][self.run.call_args.args[0].index('--markdown')+1])
        self.assertIn(key, self.jobs.command('', self.sid))
        self.assertIn('已取消', self.jobs.command('cancel ' + key, self.sid))
        await self.watch.tick(due + 1201)
        self.assertEqual(self.run.await_count, 1)

    async def test_query_failure_retries_and_uncertain_delivery_stops(self):
        key, due = await self.create(baseline=5)
        self.watch.api = AsyncMock(side_effect=RuntimeError('GitLab unavailable'))
        await self.watch.tick(due + 1)
        self.assertEqual(self.db.execute('SELECT state FROM target_watches').fetchone(), ('active',))
        self.watch.api = AsyncMock(return_value=discussion(note(6)))
        self.run.side_effect = asyncio.TimeoutError()
        await self.watch.tick(due + 601)
        self.assertEqual(self.db.execute('SELECT state FROM target_watches').fetchone(), ('uncertain',))
        self.watch.recover()
        await self.watch.tick(due + 1201)
        self.assertEqual(self.run.await_count, 1)
        self.assertNotIn(key, self.jobs.command('', self.sid))
        self.assertIn('结果不确定', self.jobs.command(key, self.sid))

    async def test_creation_verifies_target_and_deduplicates(self):
        calls = []
        async def fake_api(endpoint, paginate=False):
            calls.append((endpoint, paginate))
            return discussion(note(7)) if paginate else {'iid': 75, 'web_url': ISSUE}
        self.watch.api = fake_api
        result = await self.watch.create_many([self.proposal(), self.proposal()], 'om_source', self.sid)
        self.assertEqual(result[0][0], result[1][0])
        self.assertEqual(result[1][1], False)
        self.assertEqual(self.db.execute('SELECT baseline_id FROM target_watches').fetchone(), (7,))
        self.assertEqual(calls[0][0], 'projects/group%2Fproject/issues/75')
        self.assertEqual(calls[1][0], 'projects/group%2Fproject/issues/75/discussions?per_page=100')
        self.assertEqual(await self.watch.create_many([self.proposal()], 'om_source', self.sid),
                         [result[0][:1] + (False,)])

    async def test_comment_arriving_during_request_is_not_baselined(self):
        from assistant.progress import start
        start(self.db, 'om_source', 'oc_owner', self.sid)
        future = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        async def fake_api(endpoint, paginate=False):
            return discussion(note(7), note(8, created_at=future)) if paginate else {
                'iid': 75, 'web_url': ISSUE}
        self.watch.api = fake_api
        await self.watch.create_many([self.proposal()], 'om_source', self.sid)
        self.assertEqual(self.db.execute('SELECT baseline_id FROM target_watches').fetchone(), (7,))

    async def test_pagination_and_invalid_targets(self):
        self.run.return_value = json.dumps(discussion(note(2))) + '\n' + json.dumps(discussion(note(3))[0])
        self.assertEqual(len(await self.watch.api('endpoint', True)), 2)
        self.assertEqual([x['id'] for x in flatten_notes(discussion(note(2), note(3)))], [2, 3])
        for url in ('https://other.example.com/group/project/-/issues/75',
                    ISSUE + '?foo=bar', 'https://gitlab.example.com/group/project/-/issues/0'):
            with self.assertRaises(ValueError):
                target_endpoint(url, self.cfg['GITLAB_HOST'])


if __name__ == '__main__':
    unittest.main()
