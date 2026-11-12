import asyncio
from datetime import date, datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from assistant.issues import IssueAssistant, approves_pipeline_send, is_workday, validate_calendar
from assistant.settings import DEFAULT_SIGNATURE as SIGNATURE, get_timezone
from assistant.service import database
from assistant import service


URL = 'https://gitlab.example.com/group/project/-/issues/23'
SHANGHAI = get_timezone()
MR = 'https://gitlab.example.com/group/project/-/merge_requests/23'
CALENDAR = {'year': 2026, 'holidays': ['2026-10-01', '2026-10-02'],
            'workdays': ['2026-10-10'], 'sources': ['https://www.gov.cn/notice']}


class IssueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = database(self.root / 'messages.db')
        self.run = AsyncMock()
        self.cfg = {'GITLAB_HOST': 'gitlab.example.com', 'GLAB_BIN': 'glab',
                    'LARK_CLI_BIN': 'lark-cli', 'FEISHU_OWNER_CHAT_ID': 'oc_owner',
                    'FEISHU_OWNER_OPEN_ID': 'ou_owner', 'FEISHU_APP_ID': 'cli_test',
                    'AGENT_TIMEOUT_SECONDS': 30}
        self.issues = IssueAssistant(self.cfg, self.db, self.run, self.root, self.root)
        self.item = {'url': URL, 'title': 'Issue title', 'kind': 'issues', 'iid': 23,
                     'reasons': ['指派给我']}

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_conditional_approval_requires_affirmative_message(self):
        self.assertTrue(approves_pipeline_send('消息没有问题，等 pipeline 完成后发送'))
        self.assertFalse(approves_pipeline_send('消息没有问题吗？等 pipeline 完成后发送？'))
        self.assertFalse(approves_pipeline_send('消息没有问题，但暂时不要等 pipeline 完成就发送'))

    def add_inbox(self, key):
        self.db.execute('INSERT INTO inbox(id,binding) VALUES(?,?)', (key, 'oc_owner'))
        self.db.commit()

    async def test_paginated_all_categories_and_union(self):
        mr = {'web_url': MR, 'title': 'Draft MR', 'iid': 23, 'state': 'opened',
              'author': {'name': 'MR 创建人'}}
        issue = {'web_url': URL, 'title': 'Issue', 'iid': 23, 'state': 'opened',
                 'author': {'name': 'Issue 创建人'}}
        calls = []
        async def run(args, timeout):
            endpoint = args[args.index('--hostname') + 2]
            calls.append((endpoint, args))
            if endpoint == 'user':
                return '{"id":42}'
            if endpoint.startswith('issues?'):
                second = {**issue, 'iid': 24, 'web_url': URL[:-2] + '24'}
                return json.dumps(issue) + '\n' + json.dumps(second)
            return json.dumps(mr) + '\n' + json.dumps({**mr, 'state': 'merged'})
        self.run.side_effect = run
        items = await self.issues.fetch_all()
        self.assertEqual(len(items), 3)
        summary = self.issues.format_items(items, '汇总', group_issue_roles=True)
        self.assertIn('（创建者：Issue 创建人）', summary)
        self.assertIn('（创建者：MR 创建人）', summary)
        scheduled = self.issues.format_items(items, '增量提醒')
        self.assertEqual(scheduled.count('【Issue 创建人创建】'), 2)
        self.assertNotIn('（指派给我）', scheduled)
        self.assertIn('（需要我审批、我创建的）', scheduled)
        self.assertEqual(len([i for i in items if i['kind'] == 'merge_requests'][0]['reasons']), 2)
        self.assertTrue(all('--paginate' in args and 'scope=all' in endpoint for endpoint, args in calls[1:]))
        self.assertFalse(any(endpoint.startswith('issues?') and 'author_id=' in endpoint
                             for endpoint, _ in calls))
        self.assertFalse(any(endpoint.startswith('merge_requests?') and 'assignee_id=' in endpoint
                             for endpoint, _ in calls))

    async def test_manual_includes_authored_issues_and_merges_duplicate_reasons(self):
        self.add_inbox('om_manual')
        assigned = {'web_url': URL, 'title': 'Assigned issue', 'iid': 23, 'state': 'opened'}
        authored = {**assigned, 'web_url': URL[:-2] + '24', 'iid': 24,
                    'title': 'Assigned to someone else', 'assignees': [{'id': 99, 'name': '同事甲'},
                                                                          {'username': 'colleague-b'}]}
        async def api(endpoint, paginate=False):
            if endpoint == 'user':
                return {'id': 42}
            self.assertTrue(paginate)
            if endpoint.startswith('issues?'):
                if 'author_id=42' in endpoint:
                    return [assigned, authored, {**authored, 'state': 'closed',
                                                'web_url': URL[:-2] + '25'}]
                return [assigned]
            return []
        self.issues.api = AsyncMock(side_effect=api)
        scheduled = await self.issues.fetch_all()
        self.assertEqual([item['url'] for item in scheduled], [URL])
        reply = await self.issues.handle_command('/issue', 'om_manual')
        self.assertIn('Issue：2 条', reply)
        self.assertIn('我创建的 Issue：2 条', reply)
        self.assertIn('指派给我的 Issue：0 条', reply)
        self.assertEqual(reply.count(URL), 1)
        self.assertIn('（未指派）', reply)
        self.assertIn('（指派给：同事甲、colleague-b）', reply)
        self.assertIn(authored['web_url'], reply)
        self.assertNotIn(URL[:-2] + '25', reply)
        self.issues.delivered('om_manual')
        self.assertEqual(self.db.execute('SELECT count(*) FROM issue_pool').fetchone()[0], 2)

    async def test_manual_four_sections_prioritize_authored_mr(self):
        self.add_inbox('om_sections')
        both = {**self.item, 'url': URL[:-2] + '24', 'reasons': ['指派给我', '我创建的']}
        self.item['author'] = {'name': 'Issue 创建人'}
        created_mr = {**self.item, 'url': MR, 'kind': 'merge_requests',
                      'reasons': ['我创建的', '需要我审批'], 'author': {'username': 'mr-author'}}
        review_mr = {**created_mr, 'url': MR[:-2] + '24', 'reasons': ['需要我审批'],
                     'author': {'name': '同事甲'}}
        self.issues.fetch_all = AsyncMock(return_value=[self.item, both, created_mr, review_mr])
        reply = await self.issues.handle_command('/issue', 'om_sections')
        created, rest = reply.split('**指派给我的 Issue：1 条**')
        assigned, rest = rest.split('**我创建的 MR：1 条**')
        authored_mrs, review_mrs = rest.split('**需要我审批的 MR：1 条**')
        self.assertIn('**我创建的 Issue：1 条**', created)
        self.assertIn(both['url'], created)
        self.assertNotIn(URL, created)
        self.assertIn(URL, assigned)
        self.assertNotIn(both['url'], assigned)
        self.assertIn(MR, authored_mrs)
        self.assertNotIn(review_mr['url'], authored_mrs)
        self.assertIn(review_mr['url'], review_mrs)
        self.assertNotIn(MR, review_mrs)
        self.assertIn('（创建者：Issue 创建人）', assigned)
        self.assertIn('（创建者：mr-author）', authored_mrs)
        self.assertIn('（创建者：同事甲）', review_mrs)
        self.assertEqual(reply.count(both['url']), 1)
        self.assertEqual(reply.count(created_mr['url']), 1)
        scheduled = self.issues.format_items([self.item, created_mr, review_mr], '定时通知')
        self.assertIn('**Issue：1 条**', scheduled)
        self.assertNotIn('**我创建的 Issue', scheduled)
        self.assertIn('【Issue 创建人创建】', scheduled)

    async def test_authored_query_failure_preserves_manual_pool(self):
        self.add_inbox('om_manual')
        self.issues.remember([URL], 'asked')
        async def api(endpoint, paginate=False):
            if endpoint == 'user':
                return {'id': 42}
            if endpoint.startswith('issues?') and 'author_id=42' in endpoint:
                raise RuntimeError('authored query failed')
            return []
        self.issues.api = AsyncMock(side_effect=api)
        with self.assertRaisesRegex(RuntimeError, 'authored query failed'):
            await self.issues.handle_command('/issue', 'om_manual')
        self.assertEqual(self.issues.unseen([self.item]), [])
        self.assertIsNone(self.db.execute('SELECT issue_refs FROM inbox').fetchone()[0])

    async def test_partial_query_failure_does_not_clear_pool(self):
        self.issues.remember([URL], 'asked')
        self.issues.calendar = AsyncMock(return_value=CALENDAR)
        self.issues.fetch_all = AsyncMock(side_effect=RuntimeError('one category failed'))
        with self.assertRaises(RuntimeError):
            await self.issues.tick(datetime(2026, 10, 13, 9, tzinfo=SHANGHAI))
        self.assertEqual(self.issues.unseen([self.item]), [])
        self.assertEqual(self.db.execute('SELECT reset FROM issue_days').fetchone()[0], 0)

    async def test_daily_reset_once_restart_and_incremental(self):
        self.issues.calendar = AsyncMock(return_value=CALENDAR)
        self.issues.fetch_all = AsyncMock(return_value=[self.item])
        self.issues.send_bot = AsyncMock()
        self.issues.remember([URL], 'asked')
        await self.issues.tick(datetime(2026, 10, 13, 9, tzinfo=SHANGHAI))
        self.issues.fetch_all.assert_awaited_once_with(include_authored_issues=True)
        self.assertEqual(self.issues.send_bot.await_count, 1)
        other = {**self.item, 'url': URL[:-2]+'24', 'iid': 24}
        restarted = IssueAssistant(self.cfg, self.db, self.run, self.root, self.root)
        restarted.calendar = AsyncMock(return_value=CALENDAR)
        restarted.fetch_all = AsyncMock(return_value=[self.item, other])
        restarted.send_bot = AsyncMock()
        await restarted.tick(datetime(2026, 10, 13, 9, 9, tzinfo=SHANGHAI))
        self.assertEqual(restarted.fetch_all.await_count, 0)
        await restarted.tick(datetime(2026, 10, 13, 9, 10, tzinfo=SHANGHAI))
        self.assertEqual(restarted.send_bot.await_count, 1)
        self.assertNotIn('(https://gitlab.example.com/group/project/-/issues/23)', restarted.send_bot.call_args.args[0])

    async def test_failed_delivery_retries_outbox_without_new_query(self):
        self.issues.calendar = AsyncMock(return_value=CALENDAR)
        self.issues.fetch_all = AsyncMock(return_value=[self.item])
        self.issues.send_bot = AsyncMock(side_effect=RuntimeError('failed'))
        current = datetime(2026, 10, 13, 9, tzinfo=SHANGHAI)
        with self.assertRaises(RuntimeError):
            await self.issues.tick(current)
        self.assertEqual(self.issues.unseen([self.item]), [self.item])
        self.issues.send_bot.side_effect = None
        await self.issues.tick(current)
        self.assertEqual(self.issues.fetch_all.await_count, 1)
        self.assertEqual(self.issues.unseen([self.item]), [])

    async def test_daily_and_manual_share_full_content_and_rebuild_pool_after_delivery(self):
        authored = {**self.item, 'url': URL[:-2]+'24', 'iid': 24, 'reasons': ['我创建的', '指派给我']}
        self.issues.calendar = AsyncMock(return_value=CALENDAR)
        self.issues.fetch_all = AsyncMock(return_value=[self.item, authored])
        self.issues.send_bot = AsyncMock()
        self.issues.remember([URL[:-2]+'99'], 'asked')
        await self.issues.tick(datetime(2026, 10, 13, 9, tzinfo=SHANGHAI))
        daily = self.issues.send_bot.call_args.args[0]
        self.assertIn('我创建的 Issue：1 条', daily)
        self.assertIn('指派给我的 Issue：1 条', daily)
        self.assertIn('我创建的 MR：0 条', daily)
        self.assertIn('需要我审批的 MR：0 条', daily)
        self.assertEqual(self.db.execute('SELECT count(*) FROM issue_pool').fetchone()[0], 2)
        self.add_inbox('om_manual')
        manual = await self.issues.handle_command('/issue', 'om_manual')
        self.assertEqual(manual, daily)
        self.assertEqual(self.db.execute('SELECT count(*) FROM issue_pool').fetchone()[0], 0)
        self.issues.delivered('om_manual')
        self.assertEqual(self.db.execute('SELECT count(*) FROM issue_pool').fetchone()[0], 2)
        for call in self.issues.fetch_all.await_args_list:
            self.assertEqual(call.kwargs, {'include_authored_issues': True})

    async def test_holiday_and_outside_window_do_not_query(self):
        self.issues.calendar = AsyncMock(return_value=CALENDAR)
        self.issues.fetch_all = AsyncMock()
        for current in [datetime(2026, 10, 1, 9, tzinfo=SHANGHAI),
                        datetime(2026, 10, 13, 8, 59, tzinfo=SHANGHAI),
                        datetime(2026, 10, 13, 21, 1, tzinfo=SHANGHAI)]:
            await self.issues.tick(current)
        self.issues.fetch_all.assert_not_awaited()
        self.assertEqual(self.issues.calendar.await_count, 1)

    async def test_manual_always_full_and_marks_only_after_delivery(self):
        self.add_inbox('om_manual')
        self.issues.fetch_all = AsyncMock(return_value=[self.item])
        self.issues.remember([URL], 'asked')
        self.issues.remember([MR], 'asked')
        reply = await self.issues.handle_command('  /issue \n', 'om_manual')
        self.issues.fetch_all.assert_awaited_once_with(include_authored_issues=True)
        self.assertIn(URL, reply)
        self.assertIsNone(await self.issues.handle_command('/issue something', 'om_other'))
        self.assertEqual(self.db.execute('SELECT count(*) FROM issue_pool').fetchone()[0], 0)
        self.issues.delivered('om_manual')
        self.assertEqual(self.db.execute('SELECT reason FROM issue_pool').fetchone()[0], 'manual')
        self.assertEqual(self.db.execute('SELECT count(*) FROM issue_pool').fetchone()[0], 1)

    async def test_manual_fetch_failure_preserves_pool(self):
        self.add_inbox('om_manual')
        self.issues.remember([URL, MR], 'asked')
        self.issues.fetch_all = AsyncMock(side_effect=RuntimeError('query failed'))
        with self.assertRaises(RuntimeError):
            await self.issues.handle_command('/issue', 'om_manual')
        self.assertEqual(self.db.execute('SELECT count(*) FROM issue_pool').fetchone()[0], 2)

    async def test_manual_refresh_pauses_poll_and_supersedes_old_outbox(self):
        self.add_inbox('om_manual')
        self.issues.queue('2026-10-13', 1, [{**self.item, 'url': MR}], '旧提醒')
        self.db.commit()
        self.issues.fetch_all = AsyncMock(return_value=[self.item])
        await self.issues.handle_command('/issue', 'om_manual')
        self.issues.calendar = AsyncMock(side_effect=AssertionError('Pause during full reply'))
        await self.issues.tick(datetime(2026, 10, 13, 9, 10, tzinfo=SHANGHAI))
        self.issues.send_bot = AsyncMock()
        await self.issues.flush('2026-10-13')
        self.issues.send_bot.assert_not_awaited()
        self.issues.delivered('om_manual')
        self.assertEqual(self.issues.unseen([self.item]), [])

    async def test_interval_reload_rebases_slots_and_survives_restart(self):
        self.issues.fetch_all = AsyncMock(return_value=[self.item])
        self.issues.send_bot = AsyncMock()
        await self.issues.tick(datetime(2026, 10, 13, 9, tzinfo=SHANGHAI))
        other = {**self.item, 'url': URL[:-2] + '24', 'iid': 24}
        self.issues.fetch_all.return_value = [self.item, other]
        await self.issues.tick(datetime(2026, 10, 13, 9, 40, tzinfo=SHANGHAI))
        await self.issues.tick(datetime(2026, 10, 13, 10, tzinfo=SHANGHAI))
        self.cfg['ISSUE_WATCH_INTERVAL_SECONDS'] = 1200
        restarted = IssueAssistant(self.cfg, self.db, self.run, self.root, self.root)
        third = {**self.item, 'url': URL[:-2] + '25', 'iid': 25}
        restarted.fetch_all = AsyncMock(return_value=[self.item, other, third])
        restarted.send_bot = AsyncMock()
        await restarted.tick(datetime(2026, 10, 13, 10, 20, tzinfo=SHANGHAI))
        restarted.fetch_all.assert_awaited_once_with()
        restarted.send_bot.assert_awaited_once()
        self.assertIn(third['url'], restarted.send_bot.call_args.args[0])
        self.assertEqual(self.db.execute('SELECT count(*) FROM issue_pool').fetchone()[0], 3)
        again = IssueAssistant(self.cfg, self.db, self.run, self.root, self.root)
        again.fetch_all = restarted.fetch_all
        again.send_bot = restarted.send_bot
        await again.tick(datetime(2026, 10, 13, 10, 21, tzinfo=SHANGHAI))
        self.assertEqual(again.fetch_all.await_count, 1)
        await again.tick(datetime(2026, 10, 13, 10, 40, tzinfo=SHANGHAI))
        self.assertEqual(again.fetch_all.await_count, 2)
        self.assertTrue(all(not call.kwargs for call in again.fetch_all.await_args_list))
        self.assertEqual(self.db.execute('SELECT reset FROM issue_days').fetchone()[0], 1)

    async def test_start_reload_rebases_incremental_without_repeating_summary(self):
        self.issues.fetch_all = AsyncMock(return_value=[self.item])
        self.issues.send_bot = AsyncMock()
        await self.issues.tick(datetime(2026, 10, 13, 10, tzinfo=SHANGHAI))
        self.cfg.update(ISSUE_WATCH_START_TIME='10:00', ISSUE_SUMMARY_TIME='10:00')
        restarted = IssueAssistant(self.cfg, self.db, self.run, self.root, self.root)
        restarted.fetch_all = AsyncMock(return_value=[self.item])
        restarted.send_bot = AsyncMock()
        await restarted.tick(datetime(2026, 10, 13, 10, 10, tzinfo=SHANGHAI))
        restarted.fetch_all.assert_awaited_once_with()
        restarted.send_bot.assert_not_awaited()
        self.assertEqual(self.db.execute('SELECT count(*) FROM issue_pool').fetchone()[0], 1)
        await restarted.tick(datetime(2026, 10, 13, 10, 11, tzinfo=SHANGHAI))
        self.assertEqual(restarted.fetch_all.await_count, 1)

    async def test_timezone_reload_rebases_same_day_without_repeating_summary(self):
        from zoneinfo import ZoneInfo
        self.issues.fetch_all = AsyncMock(return_value=[self.item])
        self.issues.send_bot = AsyncMock()
        await self.issues.tick(datetime(2026, 10, 13, 10, tzinfo=SHANGHAI))
        self.cfg['TIMEZONE'] = 'UTC'
        restarted = IssueAssistant(self.cfg, self.db, self.run, self.root, self.root)
        restarted.fetch_all = AsyncMock(return_value=[self.item])
        restarted.send_bot = AsyncMock()
        await restarted.tick(datetime(2026, 10, 13, 9, 10, tzinfo=ZoneInfo('UTC')))
        restarted.fetch_all.assert_awaited_once_with()
        restarted.send_bot.assert_not_awaited()

    async def test_legacy_default_slots_rebase_when_interval_changes(self):
        self.db.execute('INSERT INTO issue_days(scope,day,calendar,workday,reset,last_slot) VALUES(?,?,?,?,?,?)',
                        (self.issues.scope, '2026-10-13', '{}', 1, 1, 6))
        self.issues.remember([URL], 'asked')
        self.cfg['ISSUE_WATCH_INTERVAL_SECONDS'] = 1200
        self.issues.fetch_all = AsyncMock(return_value=[self.item])
        self.issues.send_bot = AsyncMock()
        await self.issues.tick(datetime(2026, 10, 13, 10, 20, tzinfo=SHANGHAI))
        self.issues.fetch_all.assert_awaited_once_with()
        self.issues.send_bot.assert_not_awaited()
        self.assertEqual(self.db.execute('SELECT reason FROM issue_pool').fetchone()[0], 'asked')

    async def test_same_day_mode_change_rechecks_previously_closed_workday(self):
        current = datetime(2026, 10, 11, 9, tzinfo=SHANGHAI)
        self.issues.fetch_all = AsyncMock(return_value=[self.item])
        self.issues.send_bot = AsyncMock()
        await self.issues.tick(current)
        self.issues.fetch_all.assert_not_awaited()
        self.assertEqual(self.db.execute('SELECT workday,reset FROM issue_days').fetchone(), (0, 0))
        self.cfg['WORKDAY_MODE'] = 'everyday'
        restarted = IssueAssistant(self.cfg, self.db, self.run, self.root, self.root)
        restarted.fetch_all = self.issues.fetch_all
        restarted.send_bot = self.issues.send_bot
        await restarted.tick(current)
        restarted.fetch_all.assert_awaited_once_with(include_authored_issues=True)
        self.assertEqual(self.db.execute('SELECT workday,reset FROM issue_days').fetchone(), (1, 1))
        self.assertEqual(json.loads(self.db.execute('SELECT calendar FROM issue_days').fetchone()[0])['mode'], 'everyday')

    async def test_same_day_calendar_recheck_preserves_completed_summary_and_slot(self):
        self.cfg['WORKDAY_MODE'] = 'everyday'
        self.db.execute('INSERT INTO issue_days(scope,day,calendar,workday,reset,last_slot) VALUES(?,?,?,?,?,?)',
                        (self.issues.scope, '2026-10-11', '{}', 0, 1, 5))
        self.issues.remember([URL], 'asked')
        self.issues.fetch_all = AsyncMock(return_value=[self.item])
        self.issues.send_bot = AsyncMock()
        await self.issues.tick(datetime(2026, 10, 11, 9, 49, tzinfo=SHANGHAI))
        self.assertEqual(self.db.execute('SELECT workday,reset,last_slot FROM issue_days').fetchone(), (1, 1, 5))
        self.issues.fetch_all.assert_not_awaited()
        self.issues.send_bot.assert_not_awaited()
        await self.issues.tick(datetime(2026, 10, 11, 10, tzinfo=SHANGHAI))
        self.issues.fetch_all.assert_awaited_once_with()
        self.issues.send_bot.assert_not_awaited()
        self.assertEqual(self.db.execute('SELECT reason FROM issue_pool').fetchone()[0], 'asked')
        self.assertEqual(self.db.execute('SELECT reset,last_slot FROM issue_days').fetchone(), (1, 6))

    async def test_existing_day_never_bypasses_changed_calendar_file_validation(self):
        path = self.root / 'annual.json'
        path.write_text(json.dumps(CALENDAR))
        self.cfg.update(WORKDAY_MODE='calendar_file', WORKDAY_CALENDAR_FILE='annual.json')
        self.issues.fetch_all = AsyncMock(return_value=[])
        self.issues.send_bot = AsyncMock()
        current = datetime(2026, 10, 13, 9, tzinfo=SHANGHAI)
        await self.issues.tick(current)
        for raw in ('not-json', json.dumps({**CALENDAR, 'year': 2025})):
            with self.subTest(raw=raw):
                path.write_text(raw)
                with self.assertRaisesRegex(ValueError, 'WORKDAY_CALENDAR_FILE'):
                    await self.issues.tick(current)
        path.unlink()
        with self.assertRaisesRegex(ValueError, 'WORKDAY_CALENDAR_FILE'):
            await self.issues.tick(current)
        self.assertEqual(self.issues.fetch_all.await_count, 1)
        self.assertEqual(self.db.execute('SELECT reset,last_slot FROM issue_days').fetchone(), (1, 0))

    async def test_local_calendar_modes_do_not_call_agent(self):
        self.run.side_effect = AssertionError('workday checks must remain local')
        self.cfg['WORKDAY_CALENDAR_FILE'] = 'missing.json'
        result = await self.issues.calendar(date(2026, 10, 13))
        self.assertTrue(is_workday(date(2026, 10, 13), result))
        self.assertFalse(is_workday(date(2026, 10, 11), result))
        self.cfg['WORKDAY_MODE'] = 'everyday'
        self.assertTrue(is_workday(date(2026, 10, 11), await self.issues.calendar(date(2026, 10, 11))))
        self.cfg['WORKDAY_MODE'] = 'calendar_file'
        with self.assertRaisesRegex(ValueError, 'WORKDAY_CALENDAR_FILE'):
            await self.issues.calendar(date(2026, 10, 13))
        path = self.root / 'annual.json'
        path.write_text(json.dumps(CALENDAR))
        self.cfg['WORKDAY_CALENDAR_FILE'] = 'annual.json'
        result = await self.issues.calendar(date(2026, 10, 13))
        self.assertTrue(is_workday(date(2026, 10, 10), result))
        with self.assertRaisesRegex(ValueError, 'year|年份'):
            await self.issues.calendar(date(2027, 1, 1))
        self.run.assert_not_awaited()

    async def test_workday_calendar_requires_nonconflicting_year(self):
        self.assertFalse(is_workday(date(2026, 10, 1), CALENDAR))
        self.assertFalse(is_workday(date(2026, 10, 11), CALENDAR))
        empty = {'year': 2026, 'holidays': [], 'workdays': []}
        self.assertEqual(validate_calendar(empty, 2026), empty)
        for changes in [{'year': 2025}, {'holidays': None}, {'workdays': ['bad']},
                        {'workdays': ['2026-10-01']}, {'holidays': ['2025-10-01']},
                        {'workdays': [1]}, {'holidays': ['2026-1-1']}]:
            with self.assertRaises(ValueError):
                validate_calendar({**CALENDAR, **changes}, 2026)

    async def test_configured_window_timezone_summary_and_seconds(self):
        from zoneinfo import ZoneInfo
        self.cfg.update(TIMEZONE='America/New_York', ISSUE_WATCH_START_TIME='08:00',
                        ISSUE_SUMMARY_TIME='08:01', ISSUE_WATCH_END_TIME='09:00',
                        ISSUE_WATCH_INTERVAL_SECONDS=37)
        self.issues.fetch_all = AsyncMock(return_value=[self.item])
        self.issues.send_bot = AsyncMock()
        def local(h, m, sec=0):
            return datetime(2026, 10, 13, h, m, sec, tzinfo=ZoneInfo('America/New_York'))
        await self.issues.tick(local(7, 59))
        self.issues.fetch_all.assert_not_awaited()
        await self.issues.tick(local(8, 0, 20))
        self.issues.fetch_all.assert_awaited_once_with()
        await self.issues.tick(local(8, 0, 36))
        self.assertEqual(self.issues.fetch_all.await_count, 1)
        await self.issues.tick(local(8, 0, 37))
        self.assertEqual(self.issues.fetch_all.await_count, 2)
        await self.issues.tick(local(8, 1))
        self.issues.fetch_all.assert_awaited_with(include_authored_issues=True)
        self.assertEqual(self.issues.fetch_all.await_count, 3)
        await self.issues.tick(local(8, 1, 1))
        self.assertEqual(self.issues.fetch_all.await_count, 3)
        await self.issues.tick(local(9, 0, 1))
        self.assertEqual(self.issues.fetch_all.await_count, 3)

    def test_inspection_configuration_rejects_overnight_or_outside_summary(self):
        from assistant.issues import validate_issue_config
        for changes in [{'ISSUE_WATCH_START_TIME': '22:00'},
                        {'ISSUE_SUMMARY_TIME': '08:59'},
                        {'ISSUE_WATCH_END_TIME': '08:00'},
                        {'ISSUE_WATCH_INTERVAL_SECONDS': 0},
                        {'ISSUE_SUMMARY_TIME': '9:00'}, {'WORKDAY_MODE': 'network'}]:
            with self.assertRaises(ValueError):
                validate_issue_config({**self.cfg, **changes})

    async def test_configured_signature_frozen_before_review(self):
        self.cfg['MESSAGE_SIGNATURE_TEMPLATE'] = '{bot_name}代{owner_name}通知'
        self.cfg['BOT_NAME'] = '小助手'
        self.cfg['OWNER_PROFILE'] = {'nickname': '星河'}
        key, _ = await self.draft()
        before = self.db.execute('SELECT payload FROM issue_notices WHERE id=?', (key,)).fetchone()[0]
        self.assertIn('小助手代星河通知', before)
        self.cfg['MESSAGE_SIGNATURE_TEMPLATE'] = '新的署名'
        self.run.return_value = json.dumps({'ok': True, 'data': {'message_id': 'om_notice'}})
        self.issues.verify_sender = AsyncMock()
        self.issues.delivered('om_draft')
        self.db.execute("UPDATE inbox SET state='done' WHERE id='om_draft'")
        await self.issues.handle_command('@小助手 同意', 'om_confirm')
        sent = self.run.call_args.args[0]
        self.assertIn('小助手代星河通知', sent[sent.index('--text') + 1])
        self.assertNotIn('新的署名', sent)

    async def draft(self, source='om_draft', email='colleague@example.com', session_id=None):
        self.add_inbox(source)
        self.db.execute('UPDATE inbox SET session_id=? WHERE id=?', (session_id, source))
        self.db.commit()
        self.run.return_value = json.dumps({'ok': True, 'data': {'users': [
            {'open_id': 'ou_colleague', 'localized_name': '同事', 'email': email}], 'has_more': False}})
        reply = await self.issues.process_answer(json.dumps({'reply': '准备通知', 'issue_refs': [URL],
            'notification': {'body': '处理结果\n'+URL, 'recipients': [{'name': '同事', 'email': email}]}}), source)
        key = self.db.execute('SELECT id FROM issue_notices WHERE source=?', (source,)).fetchone()[0]
        return key, reply

    async def greeting_draft(self, recipients, body='处理结果', source='om_greeting'):
        self.add_inbox(source)
        reply = await self.issues.process_answer(json.dumps({'reply': '准备通知',
            'notification': {'body': body, 'recipients': recipients}}), source)
        row = self.db.execute('SELECT id,payload FROM issue_notices WHERE source=?', (source,)).fetchone()
        return row[0], json.loads(row[1]), reply

    async def test_owner_address_survives_gitlab_resolution_and_is_sent_as_reviewed(self):
        self.issues.api = AsyncMock(return_value={'name': '测试乙', 'public_email': ''})
        self.run.return_value = json.dumps({'ok': True, 'data': {'users': [
            {'open_id': 'ou_colleague', 'localized_name': '测试乙', 'email': 'xz@example.com'}]}})
        key, payload, review = await self.greeting_draft([
            {'gitlab_id': 12, 'name': '测试乙', 'address_as': '小乙'}], '你好，测试乙，这两个问题已关闭。')
        expected = '你好，小乙，这两个问题已关闭。\n\n' + SIGNATURE
        self.assertEqual(payload['recipients'][0]['body'], expected)
        self.assertIn(expected, review)
        self.assertEqual([call.args[0][call.args[0].index('--query') + 1]
                          for call in self.run.call_args_list], ['测试乙', 'xz@example.com'])
        self.issues.delivered('om_greeting')
        self.run.side_effect = [json.dumps(self.sender_status()), '{"ok":true}']
        await self.issues.confirm(key)
        argv = self.run.call_args.args[0]
        self.assertEqual(argv[argv.index('--text') + 1], expected)

    async def test_greeting_defaults_to_feishu_name_without_inventing_alias(self):
        for index, (name, expected) in enumerate([
                ('测试乙', '测试乙'), ('小乙（测试乙）', '小乙'), ('小乙(测试乙)', '小乙')]):
            with self.subTest(name=name):
                self.run.return_value = json.dumps({'ok': True, 'data': {'users': [
                    {'open_id': 'ou_colleague', 'localized_name': name, 'email': 'xz@example.com'}]}})
                _, payload, review = await self.greeting_draft([
                    {'name': 'GitLab 姓名', 'email': 'xz@example.com'}],
                    '你好，模型猜测的称呼\n处理结果', source=f'om_fallback_{index}')
                self.assertEqual(payload['recipients'][0]['body'], f'你好，{expected}\n处理结果\n\n{SIGNATURE}')
                self.assertNotIn('模型猜测的称呼', review)

    async def test_each_recipient_has_separate_reviewed_greeting(self):
        people = [
            {'open_id': 'ou_first', 'localized_name': '测试乙', 'email': 'xz@example.com'},
            {'open_id': 'ou_second', 'localized_name': '测试丙', 'email': 'q@example.com'}]
        self.run.side_effect = [json.dumps({'ok': True, 'data': {'users': [p]}}) for p in people]
        key, payload, review = await self.greeting_draft([
            {'name': '测试乙', 'email': 'xz@example.com', 'address_as': '小乙'},
            {'name': '测试丙', 'email': 'q@example.com', 'address_as': '小丙'}])
        expected = ['你好，小乙\n处理结果\n\n' + SIGNATURE, '你好，小丙\n处理结果\n\n' + SIGNATURE]
        self.assertEqual([p['body'] for p in payload['recipients']], expected)
        for body in expected:
            self.assertIn(body, review)
        self.issues.delivered('om_greeting')
        self.run.reset_mock()
        self.run.side_effect = [json.dumps(self.sender_status()), '{"ok":true}', '{"ok":true}']
        await self.issues.confirm(key)
        sends = [call.args[0] for call in self.run.call_args_list if '--text' in call.args[0]]
        self.assertEqual([a[a.index('--text') + 1] for a in sends], expected)

    async def test_address_survives_candidate_selection_and_email_correction(self):
        self.issues.api = AsyncMock(return_value={'name': '测试丙', 'public_email': ''})
        people = [{'open_id': f'ou_{i}', 'localized_name': '测试丙', 'email': f'q{i}@example.com'}
                  for i in range(2)]
        self.run.return_value = json.dumps({'ok': True, 'data': {'users': people}})
        key, _, review = await self.greeting_draft([{'gitlab_id': 12, 'name': '测试丙', 'address_as': '小丙'}])
        self.assertNotIn('拟发送全文', review)
        self.run.return_value = json.dumps({'ok': True, 'data': {'users': [people[1]]}})
        self.add_inbox('om_select')
        review = await self.issues.choose_recipient(key, 0, 1, 'om_select')
        self.assertIn('你好，小丙\n处理结果', review)
        self.add_inbox('om_email')
        review = await self.issues.add_email(key, 0, 'q1@example.com', 'om_email')
        self.assertIn('你好，小丙\n处理结果', review)
        self.assertEqual(self.db.execute('SELECT reviewed FROM issue_notices WHERE id=?', (key,)).fetchone()[0], 0)

    async def test_changed_greeting_cannot_reuse_pipeline_approval(self):
        key, _ = await self.draft('om_reviewed', session_id=1)
        self.issues.delivered('om_reviewed')
        self.db.execute("UPDATE inbox SET state='done' WHERE id='om_reviewed'")
        self.add_inbox('om_defer')
        self.db.execute("UPDATE inbox SET session_id=1,content=? WHERE id='om_defer'",
                        ('消息没有问题，等 pipeline 成功后发送',))
        self.issues.api = AsyncMock(return_value={'id': 44})
        review = await self.issues.process_answer(json.dumps({'reply': '准备通知', 'notification': {
            'body': '处理结果\n' + URL,
            'recipients': [{'name': '同事', 'email': 'colleague@example.com', 'address_as': '小丙'}],
            'trigger': {'kind': 'gitlab_pipeline', 'url': 'https://gitlab.example.com/group/project/-/pipelines/44'}
        }}), 'om_defer')
        self.assertIn('拟发送全文', review)
        self.assertIn('你好，小丙', review)
        self.assertEqual(self.db.execute('SELECT state FROM issue_notices WHERE id=?', (key,)).fetchone()[0], 'superseded')
        self.assertEqual(self.db.execute("SELECT count(*) FROM issue_notices WHERE state='armed'").fetchone()[0], 0)

    async def test_legacy_reviewed_body_is_not_rewritten_at_send(self):
        key, _ = await self.draft()
        payload = {'body': '旧版已审阅正文', 'recipients': [
            {'open_id': 'ou_colleague', 'email': 'colleague@example.com', 'feishu_name': '测试乙'}]}
        self.db.execute('UPDATE issue_notices SET payload=? WHERE id=?', (json.dumps(payload), key))
        self.issues.delivered('om_draft')
        self.run.side_effect = [json.dumps(self.sender_status()), '{"ok":true}']
        await self.issues.confirm(key)
        argv = self.run.call_args.args[0]
        self.assertEqual(argv[argv.index('--text') + 1], '旧版已审阅正文')

    async def test_deferred_greeting_is_preserved_after_restart(self):
        self.run.return_value = json.dumps({'ok': True, 'data': {'users': [
            {'open_id': 'ou_colleague', 'localized_name': '测试丙', 'email': 'q@example.com'}]}})
        key, payload, _ = await self.greeting_draft([
            {'name': '测试丙', 'email': 'q@example.com', 'address_as': '小丙'}])
        payload['trigger'] = {'kind': 'gitlab_pipeline',
                              'url': 'https://gitlab.example.com/group/project/-/pipelines/44'}
        self.db.execute('UPDATE issue_notices SET payload=? WHERE id=?', (json.dumps(payload), key))
        self.issues.delivered('om_greeting')
        self.run.return_value = json.dumps(self.sender_status())
        await self.issues.confirm(key)
        restarted = IssueAssistant(self.cfg, self.db, self.run, self.root, self.root)
        restarted.api = AsyncMock(return_value={'id': 44, 'status': 'success'})
        self.run.reset_mock()
        self.run.side_effect = [json.dumps(self.sender_status()), '{"ok":true}',
                                '{"ok":true,"data":{"message_id":"om_report"}}']
        await restarted.tick_deferred()
        sends = [call.args[0] for call in self.run.call_args_list if '--user-id' in call.args[0]]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][sends[0].index('--text') + 1], payload['recipients'][0]['body'])
        self.assertTrue(payload['recipients'][0]['body'].startswith('你好，小丙\n'))

    async def test_session_drafts_are_isolated_and_quote_selects_review(self):
        first, _ = await self.draft('om_first', session_id=1)
        self.issues.delivered('om_first')
        self.db.execute("UPDATE inbox SET state='done' WHERE id='om_first'")
        second, _ = await self.draft('om_second', session_id=2)
        self.issues.delivered('om_second')
        self.db.execute("UPDATE inbox SET state='done' WHERE id='om_second'")
        self.db.commit()
        self.assertEqual(self.db.execute("SELECT count(*) FROM issue_notices WHERE state='draft'").fetchone()[0], 2)
        self.run.reset_mock()
        reply = await self.issues.handle_command('同意发送 '+first, 'om_wrong', session_id=2)
        self.assertIn('不属于当前对话', reply)
        reply = await self.issues.handle_command('好的', 'om_wrong', session_id=None)
        self.assertIn('未发送', reply)
        self.run.assert_not_awaited()
        self.run.side_effect = [json.dumps(self.sender_status()), '{"ok":true}']
        reply = await self.issues.handle_command('好的', 'om_right', session_id=1)
        self.assertIn('已以你的身份', reply)
        self.assertEqual(self.db.execute('SELECT state FROM issue_notices WHERE id=?', (second,)).fetchone()[0], 'draft')

    async def test_session_email_and_cancel_cannot_change_other_draft(self):
        key, _ = await self.draft('om_first', session_id=1)
        self.run.reset_mock()
        reply = await self.issues.handle_command('邮箱 '+key+' 1 other@example.com', 'om_mail', session_id=2)
        self.assertIn('不属于当前对话', reply)
        reply = await self.issues.handle_command('取消通知 '+key, 'om_cancel', session_id=2)
        self.assertIn('不属于当前对话', reply)
        self.assertEqual(self.db.execute('SELECT state FROM issue_notices WHERE id=?',(key,)).fetchone()[0], 'draft')
        self.run.assert_not_awaited()

    def sender_status(self, owner='ou_owner'):
        return {'appId': 'cli_test', 'identities': {'user': {'openId': owner,
            'tokenStatus': 'valid', 'scope': 'im:message im:message.send_as_user'}}}

    async def test_draft_does_not_send_before_review_and_exact_confirmation(self):
        key, reply = await self.draft()
        self.assertIn(SIGNATURE, reply)
        self.assertIn('colleague@example.com', reply)
        self.assertTrue(all('im' not in call.args[0] for call in self.run.call_args_list))
        self.assertIn('未发送', await self.issues.confirm(key))
        self.issues.delivered('om_draft')
        self.assertIsNone(await self.issues.handle_command('好的', 'om_ok'))
        self.run.side_effect = [json.dumps(self.sender_status()), '{"ok":true}']
        result = await self.issues.handle_command('同意发送 '+key, 'om_confirm')
        self.assertIn('已以你的身份', result)
        args = self.run.call_args.args[0]
        self.assertEqual(args[args.index('--as')+1], 'user')
        self.assertTrue(args[args.index('--text')+1].endswith(SIGNATURE))
        self.run.reset_mock()
        self.assertIn('不会重复发送', await self.issues.confirm(key))
        self.run.assert_not_awaited()

    async def test_sender_identity_mismatch_never_sends(self):
        key, _ = await self.draft()
        self.issues.delivered('om_draft')
        self.run.reset_mock()
        self.run.return_value = json.dumps(self.sender_status('ou_someone_else'))
        self.assertIn('未发送', await self.issues.confirm(key))
        self.assertEqual(self.run.await_count, 1)

    async def test_short_approvals_send_only_the_preceding_review(self):
        for index, word in enumerate(['同意', 'OK', '好的', 'ok！', '@AI 助手 同意']):
            with self.subTest(word=word):
                source = 'om_review_' + str(index)
                key, _ = await self.draft(source)
                self.issues.delivered(source)
                self.db.execute("UPDATE inbox SET state='done' WHERE id=?", (source,))
                self.db.commit()
                self.run.side_effect = [json.dumps(self.sender_status()), '{"ok":true}']
                result = await self.issues.handle_command(word, 'om_confirm')
                self.assertIn('已以你的身份', result)
                self.run.side_effect = None

    async def test_pending_notice_has_actual_status_and_recovery_command(self):
        key, _ = await self.draft(session_id=1)
        self.add_inbox('om_followup')
        self.db.execute("UPDATE inbox SET session_id=1 WHERE id='om_followup'")
        self.run.reset_mock()
        reply = await self.issues.process_answer(json.dumps({'reply': '回复', 'notification': None}), 'om_followup')
        self.assertIn('尚未触发发送', reply)
        self.assertIn('同意发送 '+key, reply)
        self.run.assert_not_awaited()
        self.db.execute("UPDATE inbox SET session_id=2 WHERE id='om_followup'")
        reply = await self.issues.process_answer(json.dumps({'reply': '回复', 'notification': None}), 'om_followup')
        self.assertEqual(reply, '回复')

    async def test_short_approval_after_unrelated_reply_does_not_send(self):
        key, _ = await self.draft()
        self.issues.delivered('om_draft')
        self.db.execute("UPDATE inbox SET state='done' WHERE id='om_draft'")
        self.add_inbox('om_other_topic')
        self.db.execute("UPDATE inbox SET state='done' WHERE id='om_other_topic'")
        self.db.commit()
        self.run.reset_mock()
        self.assertIsNone(await self.issues.handle_command('好的', 'om_ok'))
        self.run.assert_not_awaited()

    async def test_short_approval_does_not_bypass_unmatched_recipient(self):
        key, _ = await self.draft()
        raw = json.loads(self.db.execute('SELECT payload FROM issue_notices WHERE id=?', (key,)).fetchone()[0])
        raw['recipients'][0]['open_id'] = None
        self.db.execute('UPDATE issue_notices SET payload=? WHERE id=?', (json.dumps(raw), key))
        self.issues.delivered('om_draft')
        self.db.execute("UPDATE inbox SET state='done' WHERE id='om_draft'")
        self.db.commit()
        self.run.reset_mock()
        self.assertIn('未发送', await self.issues.handle_command('同意', 'om_ok'))
        self.run.assert_not_awaited()

    async def test_email_update_deduplicates_recipients_before_review(self):
        key, _ = await self.draft()
        raw = self.db.execute('SELECT payload FROM issue_notices WHERE id=?', (key,)).fetchone()[0]
        payload = json.loads(raw)
        payload['recipients'].append({'name': '别名', 'gitlab_id': 123, 'email': '', 'open_id': None})
        self.db.execute('UPDATE issue_notices SET payload=? WHERE id=?', (json.dumps(payload), key))
        self.db.commit()
        self.add_inbox('om_email')
        reply = await self.issues.add_email(key, 1, 'colleague@example.com', 'om_email')
        updated = json.loads(self.db.execute('SELECT payload FROM issue_notices WHERE id=?', (key,)).fetchone()[0])
        self.assertEqual(len(updated['recipients']), 1)
        self.assertIn('同意发送', reply)

    async def test_uncertain_send_never_automatically_retries(self):
        key, _ = await self.draft()
        self.issues.delivered('om_draft')
        self.run.side_effect = [json.dumps(self.sender_status()), asyncio.TimeoutError()]
        self.assertIn('不会自动重发', await self.issues.confirm(key))
        self.run.reset_mock()
        self.assertIn('uncertain', await self.issues.confirm(key))
        self.run.assert_not_awaited()

    async def test_email_continuation_requires_new_review_and_supersedes_old_draft(self):
        self.issues.api = AsyncMock(return_value={'public_email': None})
        self.add_inbox('om_unknown')
        result = await self.issues.process_answer(json.dumps({'reply': '待通知', 'notification': {
            'body': '结果', 'recipients': [{'gitlab_id': 12, 'name': '同事', 'email': ''}]}}), 'om_unknown')
        self.assertIn('补充邮箱', result)
        key = self.db.execute('SELECT id FROM issue_notices').fetchone()[0]
        self.issues.delivered('om_unknown')
        self.add_inbox('om_email')
        self.run.return_value = json.dumps({'ok': True, 'data': {'users': [
            {'open_id': 'ou_colleague', 'localized_name': '同事', 'email': 'colleague@example.com'}]}})
        reply = await self.issues.handle_command('colleague@example.com', 'om_email')
        self.assertIn('同意、OK 或 好的', reply)
        self.assertIn('取消通知 '+key, reply)
        self.assertIn('未发送', await self.issues.confirm(key))
        self.issues.delivered('om_email')
        self.assertEqual(self.db.execute('SELECT reviewed FROM issue_notices').fetchone()[0], 1)
        newkey, _ = await self.draft('om_changed')
        self.assertNotEqual(key, newkey)
        self.assertIn('superseded', await self.issues.confirm(key))

    async def test_direct_message_resolves_unique_contact_name_without_email(self):
        self.add_inbox('om_direct')
        self.run.return_value = json.dumps({'ok': True, 'data': {'users': [
            {'open_id': 'ou_colleague', 'localized_name': '星河', 'department': '示例小组'}],
            'has_more': False}})
        reply = await self.issues.process_answer(json.dumps({'reply': '准备发送',
            'notification': {'body': '你好，星河\n请看一下结果',
                             'recipients': [{'name': '星河', 'email': ''}]}}), 'om_direct')
        key, state, raw = self.db.execute('SELECT id,state,payload FROM issue_notices').fetchone()
        self.assertEqual(state, 'draft')
        self.assertEqual(json.loads(raw)['recipients'][0]['open_id'], 'ou_colleague')
        self.assertIn('拟发送全文', reply)
        self.assertIn('同意发送', reply)
        self.assertEqual(self.run.await_count, 1)
        self.assertIn('--exclude-external-users', self.run.call_args.args[0])
        self.assertIn('未发送', await self.issues.confirm(key))

    async def test_reviewed_draft_can_be_approved_for_pipeline_without_second_review(self):
        from assistant.sessions import Sessions
        sid = Sessions(self.db, 'oc_owner').create('om_session', '流水线通知')
        key, _ = await self.draft('om_reviewed', session_id=sid)
        self.issues.delivered('om_reviewed')
        self.db.execute("UPDATE inbox SET state='done' WHERE id='om_reviewed'")
        self.add_inbox('om_defer')
        self.db.execute("UPDATE inbox SET session_id=?,content=? WHERE id='om_defer'",
                        (sid, '消息没有问题，等 pipeline 成功后发送'))
        self.db.commit()
        trigger_url = 'https://gitlab.example.com/group/project/-/pipelines/44'
        self.issues.api = AsyncMock(return_value={'id': 44, 'status': 'running'})
        self.run.return_value = json.dumps({'ok': True, 'data': {'users': [
            {'open_id': 'ou_colleague', 'localized_name': '同事', 'email': 'colleague@example.com'}]}})
        reply = await self.issues.process_answer(json.dumps({
            'reply': '监控流水线', 'notification': {
                'body': '处理结果\n' + URL,
                'recipients': [{'name': '同事', 'email': 'colleague@example.com'}],
                'trigger': {'kind': 'gitlab_pipeline', 'url': trigger_url}}}), 'om_defer')
        self.assertIn('自动发送', reply)
        self.assertEqual(self.db.execute('SELECT state FROM issue_notices WHERE id=?',
                                         (key,)).fetchone()[0], 'armed')
        self.assertEqual(self.db.execute('SELECT count(*) FROM issue_notices').fetchone()[0], 1)
        self.db.execute("UPDATE inbox SET state='done' WHERE id='om_defer'")
        self.db.commit()
        self.issues.api = AsyncMock(return_value={'id': 44, 'status': 'running'})
        self.run.reset_mock()
        await self.issues.tick_deferred()
        self.run.assert_not_awaited()
        self.issues.api.return_value = {'id': 44, 'status': 'success'}
        self.run.side_effect = [json.dumps(self.sender_status()), '{"ok":true}',
                                '{"ok":true,"data":{"message_id":"om_report"}}']
        await self.issues.tick_deferred()
        self.assertEqual(self.db.execute('SELECT state,report_state FROM issue_notices WHERE id=?',
                                         (key,)).fetchone(), ('sent', 'sent'))
        self.assertEqual(self.db.execute('SELECT count(*) FROM issue_notices').fetchone()[0], 1)
        self.run.reset_mock()
        await self.issues.tick_deferred()
        self.run.assert_not_awaited()

    async def test_deferred_pipeline_failure_does_not_send_notice(self):
        key, _ = await self.draft('om_deferred')
        raw = json.loads(self.db.execute('SELECT payload FROM issue_notices WHERE id=?', (key,)).fetchone()[0])
        raw['trigger'] = {'kind': 'gitlab_pipeline',
                          'url': 'https://gitlab.example.com/group/project/-/pipelines/44'}
        self.db.execute('UPDATE issue_notices SET payload=? WHERE id=?', (json.dumps(raw), key))
        self.issues.delivered('om_deferred')
        self.run.return_value = json.dumps(self.sender_status())
        self.assertIn('自动发送', await self.issues.confirm(key))
        self.issues.api = AsyncMock(return_value={'id': 44, 'status': 'failed'})
        self.run.reset_mock()
        self.run.return_value = '{"ok":true,"data":{"message_id":"om_report"}}'
        await self.issues.tick_deferred()
        self.assertEqual(self.db.execute('SELECT state FROM issue_notices WHERE id=?',
                                         (key,)).fetchone()[0], 'condition_failed')
        self.assertFalse(any('--as' in call.args[0] and 'user' in call.args[0]
                             for call in self.run.call_args_list))

    async def test_ambiguous_contact_requires_selection_before_draft(self):
        self.add_inbox('om_ambiguous')
        users = [{'open_id': 'ou_first', 'localized_name': '小李',
                  'enterprise_email': 'first@example.com', 'department': '研发一部'},
                 {'open_id': 'ou_second', 'localized_name': '小李',
                  'enterprise_email': 'second@example.com', 'department': '研发二部'}]
        self.run.return_value = json.dumps({'ok': True, 'data': {'users': users, 'has_more': False}})
        secret_body = '你好，小李\n请处理此事'
        response = json.dumps({'reply': '草稿已经准备好，可以同意发送',
            'notification': {'body': secret_body, 'recipients': [{'name': '小李'}]}})
        reply = await self.issues.process_answer(response, 'om_ambiguous')
        key, state = self.db.execute('SELECT id,state FROM issue_notices').fetchone()
        self.assertEqual(state, 'resolving')
        self.assertIn('候选 1', reply)
        self.assertIn('候选 2', reply)
        self.assertIn(f'选择收件人 {key} 1 候选序号', reply)
        self.assertNotIn(secret_body, reply)
        self.assertNotIn('草稿已经准备好', reply)
        self.assertNotIn('拟发送全文', reply)
        self.issues.delivered('om_ambiguous')
        self.run.reset_mock()
        self.assertIn('没有可同意发送的草稿', await self.issues.handle_command('同意', 'om_approve'))
        self.assertIn('收件人尚未确认', await self.issues.handle_command('同意发送 '+key, 'om_approve'))
        self.run.assert_not_awaited()
        self.add_inbox('om_choice')
        reply = await self.issues.handle_command(f'选择收件人 {key} 1 2', 'om_choice')
        self.assertIn('拟发送全文', reply)
        self.assertIn(secret_body, reply)
        self.assertIn('second@example.com', reply)
        self.assertEqual(self.db.execute('SELECT state FROM issue_notices').fetchone()[0], 'draft')
        self.assertIn('未发送', await self.issues.confirm(key))
        self.issues.delivered('om_choice')
        self.run.assert_not_awaited()

    async def test_existing_unresolved_draft_is_migrated(self):
        self.add_inbox('om_legacy')
        self.db.execute('INSERT INTO issue_notices(id,scope,source,payload,reviewed,state) '
                        'VALUES(?,?,?,?,1,?)',
                        ('0123456789', self.issues.scope, 'om_legacy',
                         json.dumps({'body': '请处理', 'recipients': [{'name': '同事', 'open_id': None}]}),
                         'draft'))
        self.db.commit()
        restarted = IssueAssistant(self.cfg, self.db, self.run, self.root, self.root)
        self.assertEqual(self.db.execute('SELECT state,reviewed FROM issue_notices').fetchone(),
                         ('resolving', 0))
        self.assertIn('收件人尚未确认', await restarted.confirm('0123456789'))

    async def test_followup_cannot_present_unresolved_notice_as_approved_draft(self):
        self.add_inbox('om_initial')
        self.db.execute('UPDATE inbox SET session_id=7 WHERE id=?', ('om_initial',))
        self.run.return_value = json.dumps({'ok': True, 'data': {'users': [], 'has_more': False}})
        first = await self.issues.process_answer(json.dumps({'reply': '已备好草稿',
            'notification': {'body': '你好，陌生人\n内容', 'recipients': [{'name': '陌生人'}]}}),
            'om_initial')
        self.assertNotIn('已备好草稿', first)
        self.add_inbox('om_followup')
        self.db.execute('UPDATE inbox SET session_id=7 WHERE id=?', ('om_followup',))
        self.db.commit()
        followup = await self.issues.process_answer(json.dumps({
            'reply': '现在可以同意发送了', 'notification': None}), 'om_followup')
        self.assertIn('收件人尚未确认', followup)
        self.assertNotIn('现在可以同意发送了', followup)
        self.assertNotIn('你好，陌生人', followup)

    async def test_model_name_without_gitlab_verification_is_not_used(self):
        self.issues.api = AsyncMock(return_value={'public_email': None})
        person = await self.issues.resolve({'gitlab_id': 12, 'name': '同事', 'email': ''})
        self.assertFalse(person['open_id'])
        self.run.assert_not_awaited()
        self.run.return_value = json.dumps({'ok': True, 'data': {'users': [
            {'open_id': 'ou_wrong', 'email': 'wrong@example.com', 'localized_name': '同事'}]}})
        person = await self.issues.resolve({'name': '同事', 'email': 'colleague@example.com'})
        self.assertFalse(person['open_id'])

    async def test_gitlab_hidden_email_falls_back_to_unique_feishu_name_and_rechecks(self):
        self.issues.api = AsyncMock(return_value={'name': '测试丁', 'username': 'tester-d', 'public_email': ''})
        person = {'localized_name': '测试丁', 'open_id': 'ou_colleague',
                  'email': 'tester-d@example.com', 'enterprise_email': 'tester-d@example.com', 'department': '示例测试部门'}
        self.run.return_value = json.dumps({'ok': True, 'data': {'users': [person], 'has_more': False}})
        result = await self.issues.resolve({'gitlab_id': 233, 'name': '不采用模型猜测姓名', 'email': ''})
        self.assertEqual(result['open_id'], 'ou_colleague')
        self.assertEqual(result['email'], 'tester-d@example.com')
        self.assertIn('同名候选', result['match_source'])
        self.assertEqual([call.args[0][call.args[0].index('--query')+1] for call in self.run.call_args_list],
                         ['测试丁', 'tester-d@example.com'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM issue_contacts').fetchone()[0], 0)

    async def test_same_name_multiple_candidates_requires_owner_choice(self):
        self.issues.api = AsyncMock(return_value={'name': '同事', 'public_email': ''})
        self.run.return_value = json.dumps({'ok': True, 'data': {'users': [
            {'localized_name': '同事', 'open_id': 'ou_one', 'email': 'one@example.com'},
            {'localized_name': '同事', 'open_id': 'ou_two', 'email': 'two@example.com'}]}})
        result = await self.issues.resolve({'gitlab_id': 12, 'name': '同事', 'email': ''})
        self.assertIsNone(result['open_id'])
        self.assertEqual(len(result['candidates']), 2)
        self.assertEqual(self.run.await_count, 1)

    async def test_name_and_email_lookup_disagreement_does_not_resolve(self):
        self.issues.api = AsyncMock(return_value={'name': '同事', 'public_email': ''})
        person = {'localized_name': '同事', 'open_id': 'ou_one', 'email': 'one@example.com'}
        self.run.side_effect = [json.dumps({'ok': True, 'data': {'users': [person]}}),
                                json.dumps({'ok': True, 'data': {'users': [{**person, 'open_id': 'ou_two'}]}})]
        result = await self.issues.resolve({'gitlab_id': 12, 'name': '同事', 'email': ''})
        self.assertIsNone(result['open_id'])
        self.assertIn('不一致', result['error'])

    async def test_worker_routes_manual_command_and_commits_delivered_pool(self):
        self.add_inbox('om_manual')
        self.db.execute("UPDATE inbox SET content='/issue',kind='text',acknowledged=1,thread_checked=1 WHERE id='om_manual'")
        self.db.commit()
        cfg = {**self.cfg, 'ISSUE_SKILL_ENABLED': True, 'AGENT_HISTORY_TURNS': 2}
        delivered = asyncio.Event()
        async def send(*args, **kwargs):
            self.assertEqual(args[0][args[0].index('--as')+1], 'bot')
            delivered.set()
            return '{"ok":true}'
        with patch.object(IssueAssistant, 'fetch_all', AsyncMock(return_value=[self.item])), \
                patch.object(service, 'run_command', send), \
                patch.object(service, 'answer', AsyncMock(side_effect=AssertionError('Command bypasses model'))):
            task = asyncio.create_task(service.worker(cfg, self.db))
            try:
                await asyncio.wait_for(delivered.wait(), 2)
                self.assertEqual(self.db.execute("SELECT state FROM inbox WHERE id='om_manual'").fetchone()[0], 'done')
                self.assertEqual(self.issues.unseen([self.item]), [])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
