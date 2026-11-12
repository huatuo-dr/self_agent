import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from assistant.auto_agent import AutoAgentQuery, SCHEMA, TOOLS
from assistant.auto_reply import AutoReply
from tests.test_auto_reply import FakeAdapter, message


def decision(action, service='gitlab', tool='', arguments=None, text=''):
    return dict(action=action, service=service, tool=tool, arguments=json.dumps(arguments or {}), text=text)


class Model:
    def __init__(self, decisions, events=None):
        self.decisions = iter(decisions)
        self.prompts = []
        self.events = events if events is not None else []
    async def preflight(self):
        pass
    async def decide(self, prompt, schema):
        self.prompts.append(prompt)
        self.events.append('decision')
        return next(self.decisions)


class AutoAgentTests(unittest.TestCase):
    def test_only_gitlab_tools_and_services_are_exposed(self):
        self.assertEqual(set(TOOLS), {'gitlab_get', 'gitlab_work_item'})
        self.assertEqual(set(SCHEMA['properties']['service']['enum']), {'gitlab', 'none'})
        self.assertEqual(set(SCHEMA['properties']['tool']['enum']), {'', *TOOLS})


    def test_semantic_followup_multistep_and_reaction_precedes_tools(self):
        events = []
        model = Model([
            decision('read', tool='gitlab_get', arguments={'host': 'gitlab.test', 'path': 'projects/team%2Frepo'}),
            decision('read', tool='gitlab_get', arguments={'host': 'gitlab.test', 'path': 'projects/team%2Frepo/repository/branches/trunk'}),
            decision('answer', text='默认分支 trunk，作者甲。')], events)
        async def run(args, timeout):
            events.append('read')
            self.assertIn('GET', args)
            if args[-1].endswith('/trunk'):
                return json.dumps({'commit': {'author_name': '甲'}})
            return json.dumps({'default_branch': 'trunk'})
        class Adapter(FakeAdapter):
            async def react_thinking(self, mid):
                events.append('reaction')
                return await super().react_thinking(mid)
        with tempfile.TemporaryDirectory() as directory:
            query = AutoAgentQuery({'GITLAB_HOST': 'gitlab.test', 'GITLAB_SKILL_ENABLED': True,
                'GLAB_BIN': 'glab'}, model, run)
            db = sqlite3.connect(':memory:')
            auto = AutoReply(db, 'ou_owner', 'oc_owner', Adapter(), query)
            asyncio.run(auto.enable())
            # No domain keyword or URL: the model receives same-sender history.
            auto.ingest(message(mid='om_prior', body='https://gitlab.test/team/repo'))
            auto._set('om_prior', 'replied')
            auto.remember('dm:oc_dm', 'om_prior', 'user', 'https://gitlab.test/team/repo')
            auto.ingest(message(body='我说的是它现在指向的那个人'))
            asyncio.run(auto.process_once())
            self.assertEqual(events, ['decision', 'reaction', 'read', 'decision', 'read', 'decision'])
            self.assertIn('https://gitlab.test/team/repo', model.prompts[0])
            self.assertIn('作者甲', auto.adapter.replies[0])
            db.close()

    def test_ignore_is_silent_and_authorize_only_notifies(self):
        for action in ('ignore', 'authorize'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                model = Model([decision(action, service='none' if action == 'ignore' else 'gitlab',
                                        text='' if action == 'ignore' else '需要批准重跑构建')])
                query = AutoAgentQuery({}, model, None)
                db = sqlite3.connect(':memory:')
                adapter = FakeAdapter()
                auto = AutoReply(db, 'ou_owner', 'oc_owner', adapter, query)
                asyncio.run(auto.enable())
                auto.ingest(message(body='让它再跑一遍吧'))
                asyncio.run(auto.process_once())
                self.assertEqual(adapter.replies, [])
                self.assertEqual(len(adapter.notices), int(action == 'authorize'))
                self.assertEqual(len(adapter.reactions), int(action == 'authorize'))
                db.close()

    def test_tool_boundary_rejects_mutations_paths_and_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            query = AutoAgentQuery({'GITLAB_SKILL_ENABLED': True, 'GITLAB_HOST': 'gitlab.test'}, None, None)
            for path in ('https://evil/projects/a', 'projects/a/../../etc/passwd',
                         'projects/a/repository/%252e%252e/variables', 'projects/a/variables',
                         'projects/a/pipelines/3/retry', '-XPOST'):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    asyncio.run(query.tool('gitlab_get', {'host': 'gitlab.test', 'path': path}))
            with self.assertRaises(ValueError):
                asyncio.run(query.tool('shell', {'command': 'rm -rf /'}))
            with self.assertRaises(ValueError):
                asyncio.run(query.tool('gitlab_get', {'host': 'gitlab.test', 'path': 'projects/a', 'query': {'private_token': 'x'}}))

    def test_all_pages_sort_and_scrub_before_model(self):
        model = Model([
            decision('read', tool='gitlab_get', arguments={'host': 'gitlab.test', 'path': 'projects/team%2Frepo/repository/branches',
                'all_pages': True, 'sort_by': 'commit.committed_date'}),
            decision('answer', text='最新的是 newest，按提交时间比较。')])
        async def run(args, timeout):
            if args[-1].endswith('page=1'):
                return json.dumps([{'name': str(i), 'commit': {'committed_date': '2020-01-01T00:00:00Z'}} for i in range(100)])
            return json.dumps([{'name': 'newest', 'commit': {'committed_date': '2026-01-01T00:00:00Z',
                                                          'author_email': 'private@example.com'}}])
        with tempfile.TemporaryDirectory() as directory:
            q = AutoAgentQuery({'GITLAB_HOST': 'gitlab.test', 'GITLAB_SKILL_ENABLED': True,
                               'GLAB_BIN': 'glab'}, model, run)
            result = asyncio.run(q('谁最近有动静？', []))
            self.assertIn('newest', result['answer'])
            self.assertIn('"count": 101', model.prompts[-1])
            self.assertNotIn('private@example.com', model.prompts[-1])
            self.assertIn('"complete": true', model.prompts[-1])

    def test_failed_tool_can_be_replanned(self):
        model = Model([decision('read', tool='gitlab_get', arguments={'host': 'gitlab.test', 'path': 'projects/a/variables'}),
                       decision('ignore', service='none')])
        with tempfile.TemporaryDirectory() as directory:
            q = AutoAgentQuery({'GITLAB_SKILL_ENABLED': True, 'GITLAB_HOST': 'gitlab.test'}, model, None)
            self.assertEqual(asyncio.run(q('读取', [])), {'handled': False})
            self.assertIn('只读目录', model.prompts[-1])
