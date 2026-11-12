import asyncio
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from assistant import service
from assistant import skills
from assistant.agent_backends.base import RunResult


class SkillIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_protected_cached_materials_degrade_before_agent_launch(self):
        for name in ('__pycache__', '.venv', 'node_modules', '.cache'):
            with self.subTest(name=name), tempfile.TemporaryDirectory(prefix='skill-snapshot-') as directory:
                root = Path(directory)
                documents = {'skills/issue/SKILL.md': '---\nname: issue\ndescription: Issue\n---\nFixture',
                             f'skills/issue/{name}/fixture.txt': 'synthetic fixture'}
                for path, body in documents.items():
                    target = root / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(body)
                repo = AsyncMock()
                repo.current.return_value = {
                    'snapshot': str(root),
                    'index': [{'name': 'issue', 'description': 'Issue', 'path': 'skills/issue/SKILL.md'}],
                    'files': {path: hashlib.sha256(body.encode()).hexdigest() for path, body in documents.items()}}

                async def model(runner, request):
                    self.assertEqual(request.read_only_dirs, ())
                    self.assertIn('仓库技能检索暂不可用', request.prompt)
                    return RunResult('{"reply":"done","used_skills":[]}', {'reply': 'done', 'used_skills': []})

                with patch.object(skills, 'repository', return_value=repo), \
                        patch('assistant.agent_runner.AgentRunner.run', model):
                    result = await service.answer({'SKILLS_REPO_ENABLED': True, 'AGENT_TIMEOUT_SECONDS': 10}, [], 'issue')
                self.assertEqual(result, 'done')

    async def test_skill_command_uses_repository_without_model_or_quote_lookup(self):
        db = service.database(':memory:')
        self.addCleanup(db.close)
        cfg = {'FEISHU_OWNER_CHAT_ID': 'oc_test', 'FEISHU_OWNER_OPEN_ID': 'ou_test',
               'LARK_CLI_BIN': 'fake', 'AGENT_HISTORY_TURNS': 2, 'SKILLS_REPO_ENABLED': True}
        db.execute("INSERT INTO inbox(id,binding,content,kind,acknowledged,thread_checked,reply_to) "
                   "VALUES('om_skill','oc_test','/skill issue','text',1,1,'om_reference')")
        db.commit()
        repo = AsyncMock()
        repo.handle_command.return_value = '技能查询结果'
        sent = asyncio.Event()

        async def send(args, *unused, **kwargs):
            self.assertIn('技能查询结果', args[args.index('--markdown') + 1])
            sent.set()
            return '{"ok":true}'

        with patch('assistant.skills.repository', return_value=repo), \
                patch.object(service, 'answer', AsyncMock(side_effect=AssertionError('No model'))), \
                patch.object(service, 'quoted_message', AsyncMock(side_effect=AssertionError('No quote'))), \
                patch.object(service, 'run_command', send):
            worker = asyncio.create_task(service.worker(cfg, db))
            try:
                await asyncio.wait_for(sent.wait(), 2)
                repo.handle_command.assert_awaited_once_with('issue')
                self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0], 0)
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    async def test_selected_skill_body_and_relative_resources_reach_each_backend(self):
        with tempfile.TemporaryDirectory(prefix='skill-input-') as directory:
            root = Path(directory)
            skill = root / 'skills/report'
            (skill / 'references').mkdir(parents=True)
            (skill / 'SKILL.md').write_text('Use the fixture report method and references/layout.txt.')
            (skill / 'references/layout.txt').write_text('fixture layout')
            candidate = {'name': 'fixture-report', 'description': 'report', 'path': 'skills/report/SKILL.md'}
            repo = AsyncMock()
            repo.current.return_value = {'commit': 'a' * 40, 'index': [candidate]}

            @contextmanager
            def materials(data, matches):
                yield root

            for backend in ('codex', 'grok', 'claudecode'):
                async def model(runner, request):
                    self.assertEqual(runner.cfg['AGENT_BACKEND'], backend)
                    self.assertIn('Use the fixture report method', request.prompt)
                    self.assertIn(str(skill), request.prompt)
                    self.assertEqual(request.read_only_dirs, (root,))
                    self.assertTrue((request.read_only_dirs[0] / candidate['path']).is_file())
                    payload = {'reply': 'done', 'used_skills': ['fixture-report'], 'schedule': [],
                               'session': {'title': 'report', 'summary': 'done', 'status': '已完成', 'next_step': ''}}
                    return RunResult(json.dumps(payload), payload)

                with patch('assistant.skills.repository', return_value=repo), \
                        patch('assistant.skills.task_materials', materials), \
                        patch('assistant.agent_runner.AgentRunner.run', model):
                    result = json.loads(await service.answer(
                        {'AGENT_BACKEND': backend, 'SKILLS_REPO_ENABLED': True, 'AGENT_TIMEOUT_SECONDS': 10},
                        [], 'report', session={'id': 1, 'summary': ''}))
                self.assertIn('本次使用技能：fixture-report', result['reply'])

    async def test_unavailable_repository_keeps_ordinary_task_running(self):
        async def model(runner, request):
            self.assertIn('仓库技能检索暂不可用', request.prompt)
            self.assertEqual(request.read_only_dirs, ())
            return RunResult('{"reply":"done","used_skills":[]}', {'reply': 'done', 'used_skills': []})
        with patch('assistant.skills.repository', side_effect=ValueError('private error')), \
                patch('assistant.agent_runner.AgentRunner.run', model):
            result = await service.answer({'SKILLS_REPO_ENABLED': True, 'AGENT_TIMEOUT_SECONDS': 10}, [], 'hello')
        self.assertEqual(result, 'done')

    async def test_no_session_keeps_plain_reply_or_issue_fields_as_before(self):
        repo = AsyncMock()
        repo.current.return_value = {'index': []}
        for issue in (False, True):
            async def model(runner, request):
                self.assertIn('used_skills', request.output_schema['required'])
                payload = {'reply': 'done', 'used_skills': []}
                if issue:
                    payload.update(issue_refs=[], notification=None)
                return RunResult(json.dumps(payload), payload)
            with patch('assistant.skills.repository', return_value=repo), \
                    patch('assistant.agent_runner.AgentRunner.run', model):
                result = await service.answer({'SKILLS_REPO_ENABLED': True, 'ISSUE_SKILL_ENABLED': issue,
                                               'AGENT_TIMEOUT_SECONDS': 10}, [], 'hello')
            if issue:
                self.assertEqual(json.loads(result)['issue_refs'], [])
                self.assertIsNone(json.loads(result)['notification'])
            else:
                self.assertEqual(result, 'done')
