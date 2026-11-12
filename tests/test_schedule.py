import asyncio
from datetime import datetime, timedelta
import json
import unittest
from unittest.mock import AsyncMock

from assistant.schedule import Schedule, SHANGHAI
from assistant.service import database
from assistant.sessions import Sessions


class ScheduleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = database(':memory:')
        self.cfg = {'FEISHU_OWNER_CHAT_ID': 'oc_owner', 'LARK_CLI_BIN': 'lark-cli'}
        self.run = AsyncMock(return_value=json.dumps({'ok': True, 'data': {'message_id': 'om_sent'}}))
        self.jobs = Schedule(self.cfg, self.db, self.run)
        self.sessions = Sessions(self.db, 'oc_owner')
        self.sid = self.sessions.create('om_source', '提醒')
        self.sessions.bind(self.sid, 'om_root', 'omt_thread')
        self.db.execute("INSERT INTO inbox(id,binding,session_id,state) VALUES('om_source','oc_owner',?,'done')",
                        (self.sid,))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def proposal(self, repeat='none'):
        at = datetime.now(SHANGHAI) + timedelta(minutes=2)
        return {'kind': 'message', 'body': '提醒我检查结果',
                'run_at': at.isoformat(), 'repeat': repeat}

    async def test_one_time_delivery_is_persistent_and_idempotent(self):
        key, created = self.jobs.create(self.proposal(), 'om_source', self.sid)
        self.assertTrue(created)
        self.assertEqual(self.jobs.create(self.proposal(), 'om_source', self.sid), (key, False))
        due = self.db.execute('SELECT run_at FROM scheduled_jobs WHERE id=?', (key,)).fetchone()[0]
        await self.jobs.tick(due + 1)
        self.assertEqual(self.db.execute('SELECT state,sent_count FROM scheduled_jobs WHERE id=?',
                                         (key,)).fetchone(), ('sent', 1))
        self.assertIn('--reply-in-thread', self.run.call_args.args[0])
        self.assertEqual(self.db.execute('SELECT count(*) FROM scheduled_runs').fetchone()[0], 1)
        restarted = Schedule(self.cfg, self.db, self.run)
        await restarted.tick(due + 2)
        self.assertEqual(self.run.await_count, 1)
        self.assertIn('已送达', self.jobs.command(key, self.sid))

    async def test_two_reminders_from_one_message_deliver_independently(self):
        first = self.proposal()
        second = {**first, 'body': '提醒我买咖啡',
                  'run_at': (datetime.fromisoformat(first['run_at']) + timedelta(minutes=7)).isoformat()}
        created = self.jobs.create_many([first, second], 'om_source', self.sid)
        self.assertEqual(len(created), 2)
        self.assertNotEqual(created[0][0], created[1][0])
        self.assertEqual(self.jobs.create_many([first, second], 'om_source', self.sid),
                         [(created[0][0], False), (created[1][0], False)])
        rows = self.db.execute('SELECT id,body,run_at,request_source FROM scheduled_jobs ORDER BY run_at').fetchall()
        self.assertEqual([row[1] for row in rows], ['提醒我检查结果', '提醒我买咖啡'])
        self.assertEqual([row[3] for row in rows], ['om_source', 'om_source'])
        await self.jobs.tick(rows[0][2] + 1)
        self.assertEqual(self.run.await_count, 1)
        await self.jobs.tick(rows[1][2] + 1)
        self.assertEqual(self.run.await_count, 2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM scheduled_jobs WHERE state='sent'").fetchone()[0], 2)

    async def test_invalid_batch_does_not_create_partial_jobs(self):
        with self.assertRaises(ValueError):
            self.jobs.create_many([self.proposal(), {**self.proposal(), 'run_at': 'bad'}],
                                  'om_source', self.sid)
        self.assertEqual(self.db.execute('SELECT count(*) FROM scheduled_jobs').fetchone()[0], 0)

    async def test_default_list_only_shows_jobs_that_can_still_run(self):
        proposal = self.proposal()
        active, _ = self.jobs.create(proposal, 'om_active', self.sid)
        done, _ = self.jobs.create(proposal, 'om_done', self.sid)
        cancelled, _ = self.jobs.create(proposal, 'om_cancelled', self.sid)
        uncertain, _ = self.jobs.create(proposal, 'om_uncertain', self.sid)
        for key, state in ((done, 'sent'), (cancelled, 'cancelled'), (uncertain, 'uncertain')):
            self.db.execute('UPDATE scheduled_jobs SET state=? WHERE id=?', (state, key))
        listing = self.jobs.command('', self.sid)
        self.assertIn(active, listing)
        for key in (done, cancelled, uncertain):
            self.assertNotIn(key, listing)
            self.assertIn(key, self.jobs.command(key, self.sid))
        self.db.execute("UPDATE scheduled_jobs SET state='sent' WHERE id=?", (active,))
        self.assertIn('暂无生效中的任务', self.jobs.command('', self.sid))

    async def test_default_list_only_shows_armed_pipeline_notices(self):
        self.db.execute('CREATE TABLE issue_notices '
                        '(id TEXT, scope TEXT, session_id INTEGER, payload TEXT, state TEXT, '
                        'trigger_status TEXT, checked_at REAL, sent INTEGER)')
        payload = json.dumps({'trigger': {'url': 'https://example.com/-/pipelines/1'}})
        for key, state in (('1111111111', 'armed'), ('2222222222', 'sent'),
                           ('3333333333', 'cancelled'), ('4444444444', 'uncertain')):
            self.db.execute('INSERT INTO issue_notices VALUES(?,?,?,?,?,?,?,?)',
                            (key, 'oc_owner:notice', self.sid, payload, state, '', 0, 0))
        listing = self.jobs.command('', self.sid)
        self.assertIn('1111111111', listing)
        for key in ('2222222222', '3333333333', '4444444444'):
            self.assertNotIn(key, listing)
            self.assertIn(key, self.jobs.command(key, self.sid))

    async def test_uncertain_send_stops_future_runs(self):
        key, _ = self.jobs.create(self.proposal('daily'), 'om_source', self.sid)
        self.run.side_effect = asyncio.TimeoutError()
        due = self.db.execute('SELECT run_at FROM scheduled_jobs WHERE id=?', (key,)).fetchone()[0]
        await self.jobs.tick(due + 1)
        self.assertEqual(self.db.execute('SELECT state FROM scheduled_jobs WHERE id=?',
                                         (key,)).fetchone()[0], 'uncertain')
        self.run.reset_mock()
        await Schedule(self.cfg, self.db, self.run).tick(due + 86400)
        self.run.assert_not_awaited()

    async def test_cancel_and_validation(self):
        key, _ = self.jobs.create(self.proposal(), 'om_source', self.sid)
        self.assertIn('未找到', self.jobs.command('cancel ' + key, self.sid + 1))
        self.assertIn('已取消', self.jobs.command('cancel ' + key, self.sid))
        self.assertEqual(self.db.execute('SELECT state FROM scheduled_jobs WHERE id=?',
                                         (key,)).fetchone()[0], 'cancelled')
        with self.assertRaises(ValueError):
            self.jobs.create({'kind': 'message', 'body': 'x',
                              'run_at': '2026-10-17T09:00:00', 'repeat': 'none'},
                             'another', self.sid)

    async def test_repeat_uses_configured_local_calendar_across_dst(self):
        from zoneinfo import ZoneInfo
        zone = ZoneInfo('America/New_York')
        self.cfg['TIMEZONE'] = 'America/New_York'
        for repeat, before, after in [
                ('daily', datetime(2026, 3, 7, 9, tzinfo=zone), datetime(2026, 3, 8, 9, tzinfo=zone)),
                ('weekly', datetime(2026, 10, 25, 9, tzinfo=zone), datetime(2026, 11, 1, 9, tzinfo=zone))]:
            with self.subTest(repeat=repeat):
                key, _ = self.jobs.create(self.proposal(repeat), 'om_dst_' + repeat, self.sid)
                self.db.execute('UPDATE scheduled_jobs SET run_at=?,request_source=? WHERE id=?',
                                (before.timestamp(), 'om_source', key))
                await self.jobs.tick(before.timestamp() + 1)
                self.assertEqual(self.db.execute('SELECT run_at FROM scheduled_jobs WHERE id=?', (key,)).fetchone()[0],
                                 after.timestamp())
                self.assertIn('09:00', self.jobs.command(key, self.sid))
                self.assertIn('America/New_York', self.jobs.command(key, self.sid))
                self.db.execute("UPDATE scheduled_jobs SET state='cancelled' WHERE id=?", (key,))

    async def test_spring_gap_does_not_permanently_shift_daily_wall_time(self):
        from zoneinfo import ZoneInfo
        zone = ZoneInfo('America/New_York')
        self.cfg['TIMEZONE'] = 'America/New_York'
        key, _ = self.jobs.create(self.proposal('daily'), 'om_source', self.sid)
        initial = datetime(2026, 3, 7, 2, 30, tzinfo=zone).timestamp()
        self.db.execute('UPDATE scheduled_jobs SET run_at=? WHERE id=?', (initial, key))
        await self.jobs.tick(initial + 1)
        # The missing 02:30 runs at 03:30 on the transition day, then returns to 02:30.
        gap = datetime(2026, 3, 8, 3, 30, tzinfo=zone).timestamp()
        self.assertEqual(self.db.execute('SELECT run_at FROM scheduled_jobs WHERE id=?', (key,)).fetchone()[0], gap)
        restarted = Schedule(self.cfg, self.db, self.run)
        await restarted.tick(gap + 1)
        expected = datetime(2026, 3, 9, 2, 30, tzinfo=zone).timestamp()
        self.assertEqual(self.db.execute('SELECT run_at FROM scheduled_jobs WHERE id=?', (key,)).fetchone()[0], expected)

    async def test_issue_summary_uses_current_gitlab_data(self):
        from assistant.issues import IssueAssistant
        self.cfg.update(ISSUE_SKILL_ENABLED=True, GITLAB_HOST='gitlab.example.com')
        self.jobs = Schedule(self.cfg, self.db, self.run)
        self.jobs.issues.fetch_all = AsyncMock(return_value=[{
            'url': 'https://gitlab.example.com/group/project/-/issues/23',
            'title': '待处理', 'kind': 'issues', 'iid': 23,
            'reasons': ['指派给我'], 'author': {'name': '同事'}, 'assignees': []}])
        self.jobs.create({**self.proposal('weekly'), 'kind': 'issue_summary', 'body': ''},
                         'om_source', self.sid)
        due = self.db.execute('SELECT run_at FROM scheduled_jobs').fetchone()[0]
        await self.jobs.tick(due + 1)
        self.jobs.issues.fetch_all.assert_awaited_once_with(include_authored_issues=True)
        args = self.run.call_args.args[0]
        self.assertIn('待处理', args[args.index('--markdown') + 1])
        self.assertIn('Issue / MR 汇总', args[args.index('--markdown') + 1])
        self.assertEqual(self.db.execute('SELECT state,sent_count FROM scheduled_jobs').fetchone(),
                         ('pending', 1))


if __name__ == '__main__':
    unittest.main()
