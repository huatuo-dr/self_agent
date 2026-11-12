import asyncio
import json
import unittest

from assistant.auto_sources import work_item, scrub


CFG = {'GITLAB_HOST': 'gitlab.example.com', 'GLAB_BIN': 'glab'}


def item_page(notes, more=False, cursor=None):
    return json.dumps({'data': {'namespace': {'workItem': {
        'iid': '218', 'title': 'Example work item', 'state': 'OPEN',
        'workItemType': {'name': 'Task'},
        'widgets': [{'description': '检查项目构建。'}, {'discussions': {
            'pageInfo': {'hasNextPage': more, 'endCursor': cursor},
            'nodes': [{'notes': {'nodes': notes}}]}}]}}}})


class AutoSourceTests(unittest.TestCase):
    def test_work_item_keeps_description_and_recent_non_system_comments(self):
        calls = []
        async def run(args, timeout):
            calls.append(args)
            return item_page([
                {'body': 'new comment', 'createdAt': '2026-01-02', 'system': False},
                {'body': 'old comment', 'createdAt': '2026-01-01', 'system': False},
                {'body': 'system event', 'createdAt': '2026-01-03', 'system': True},
            ])
        result = asyncio.run(work_item(CFG, run, 'team/repo', 218))
        self.assertEqual(result['facts']['description'], '检查项目构建。')
        self.assertEqual([note['body'] for note in result['facts']['recent_comments']],
                         ['new comment', 'old comment'])
        self.assertTrue(result['facts']['comments_complete'])
        self.assertEqual(result['facts']['web_url'], 'https://gitlab.example.com/team/repo/-/work_items/218')
        self.assertEqual(len(calls), 1)
        self.assertIn('query WorkItem(', ' '.join(calls[0]))
        self.assertNotIn('mutation', ' '.join(calls[0]))

    def test_work_item_follows_discussion_cursor_and_rejects_stalled_cursor(self):
        calls = []
        async def run(args, timeout):
            calls.append(args)
            if len(calls) == 1:
                return item_page([], True, 'next')
            return item_page([{'body': 'last page', 'system': False}])
        result = asyncio.run(work_item(CFG, run, 'team/repo', 218))
        self.assertTrue(result['facts']['comments_complete'])
        self.assertIn('cursor=next', calls[1])
        async def stalled(args, timeout):
            return item_page([], True, 'same')
        with self.assertRaisesRegex(ValueError, '分页'):
            asyncio.run(work_item(CFG, stalled, 'team/repo', 218))

    def test_scrub_credentials_and_nested_identifiers(self):
        value = scrub({'token': 'fixture-secret', 'body': 'token=abc123secret user@example.com',
                       'items': [{'password': 'fixture-password', 'title': 'public result'}]})
        self.assertEqual(value['items'], [{'title': 'public result'}])
        self.assertNotIn('abc123secret', str(value))
        self.assertNotIn('user@example.com', str(value))
        self.assertNotIn('token', value)
