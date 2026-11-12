import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from assistant import service
from assistant.owner_profile import load_owner_profile
from assistant.pipeline_config import pages_target


class PublicConfigTests(unittest.TestCase):
    def test_owner_profile_reads_only_default_or_explicit_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config').mkdir()
            (root / 'config/owner-profile.json').write_text(json.dumps({'name': '用户甲'}))
            self.assertIsNone(load_owner_profile(root, 'ou_test'))
            self.assertEqual(load_owner_profile(root, 'ou_test', 'config/owner-profile.json'),
                             {'owner_open_id': 'ou_test', 'name': '用户甲'})

    def test_enabled_skill_repository_reports_missing_git(self):
        values = {'FEISHU_APP_ID': 'cli_test', 'FEISHU_OWNER_OPEN_ID': 'ou_test',
                  'FEISHU_OWNER_CHAT_ID': 'oc_test', 'AGENT_BACKEND': 'grok',
                  'SKILLS_REPO_ENABLED': 'true', 'SKILLS_REPO_URL': 'git@git.example:team/library.git'}
        with tempfile.TemporaryDirectory() as directory, patch.object(service, 'ROOT', Path(directory)), \
                patch.object(service, 'read_env', return_value=values), \
                patch.object(service.shutil, 'which', side_effect=lambda name: None if name == 'git' else '/mock/' + name):
            with self.assertRaisesRegex(ValueError, 'SKILLS_REPO_ENABLED requires git'):
                service.config()

    def test_skill_repository_configuration_has_explicit_source_and_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = self.configured(Path(directory), AGENT_BACKEND='grok', SKILLS_REPO_ENABLED='true',
                                  SKILLS_REPO_URL='ssh://git@git.example:2222/team/library.git',
                                  SKILLS_REPO_BRANCH='release/skills')
            self.assertTrue(cfg['SKILLS_REPO_ENABLED'])
            self.assertEqual(cfg['SKILLS_REPO_BRANCH'], 'release/skills')
            self.assertIsNone(cfg.get('CODEX_BIN'))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                self.configured(Path(directory), AGENT_BACKEND='grok', SKILLS_REPO_ENABLED='true')

    def configured(self, root, **extra):
        for name in ('history-search', 'report-creation', 'gitlab-assistant', 'issue-assistant'):
            path = root / '.agents/skills' / name / 'SKILL.md'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('test skill')
        (root / 'scripts').mkdir(exist_ok=True)
        for name in ('search_history.py', 'search_images.py'):
            (root / 'scripts' / name).touch()
        data = dict(FEISHU_APP_ID='cli_test', FEISHU_OWNER_OPEN_ID='ou_test',
                    FEISHU_OWNER_CHAT_ID='oc_test', **extra)
        def executable(name):
            return None if name in ('codex', 'glab') else '/mock/' + name
        with patch.object(service, 'ROOT', root), patch.object(service, 'read_env', return_value=data), \
                patch.object(service.shutil, 'which', side_effect=executable):
            return service.config()

    def test_grok_chat_does_not_require_codex_or_report_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = self.configured(Path(directory), AGENT_BACKEND='grok')
        self.assertIsNone(cfg.get('CODEX_BIN'))
        self.assertFalse(cfg['EVOLUTION_ENABLED'])
        self.assertEqual(cfg['PIPELINE_PAGES_REPO_URL'], '')

    def test_local_reports_do_not_require_gitlab(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = self.configured(root, AGENT_BACKEND='codex', CODEX_BIN='fake-codex',
                                  REPORT_SKILL_ENABLED='true')
            self.assertTrue((root / '.project/reports').is_dir())
        self.assertTrue(cfg['REPORT_SKILL_ENABLED'])
        self.assertFalse(cfg['GITLAB_SKILL_ENABLED'])
        self.assertFalse(cfg['REPORT_PUBLISH_ENABLED'])

    def test_grok_report_only_configuration_uses_shared_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = self.configured(Path(directory), AGENT_BACKEND='grok', REPORT_SKILL_ENABLED='true')
        self.assertTrue(cfg['REPORT_SKILL_ENABLED'])
        self.assertIsNone(cfg.get('CODEX_BIN'))

    def test_grok_report_only_never_falls_back_when_shared_isolation_fails(self):
        from assistant.agent_backends.base import AgentError
        from assistant.agent_isolation import IsolationError
        cfg = {'AGENT_BACKEND':'grok','GROK_BIN':'grok','AGENT_TIMEOUT_SECONDS':10,
               'REPORT_SKILL_ENABLED':True}
        with patch('assistant.agent_runner.isolated',side_effect=IsolationError('unavailable')), \
                patch.object(service,'run_command',AsyncMock()) as run:
            with self.assertRaises(AgentError):
                asyncio.run(service.answer(cfg,[],'生成本地报告'))
            run.assert_not_awaited()

    def test_pages_target_has_no_company_default(self):
        with self.assertRaisesRegex(ValueError, 'PIPELINE_PAGES_REPO_URL'):
            pages_target({})

    def test_issue_watch_parses_environment_interval_without_codex(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = self.configured(Path(directory), AGENT_BACKEND='grok',
                                  GITLAB_SKILL_ENABLED='true', GLAB_BIN='fake-glab',
                                  GITLAB_HOST='gitlab.example.com', ISSUE_SKILL_ENABLED='true',
                                  ISSUE_WATCH_INTERVAL_SECONDS='37')
        self.assertEqual(cfg['ISSUE_WATCH_INTERVAL_SECONDS'], 37)
        self.assertIsNone(cfg.get('CODEX_BIN'))

    def test_explicit_owner_profile_allows_partial_optional_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'identity.json').write_text(json.dumps({'nickname': '用户甲'}))
            profile = load_owner_profile(root, 'ou_test', 'identity.json')
        self.assertEqual(profile, {'owner_open_id': 'ou_test', 'nickname': '用户甲'})

    def test_disabled_evolution_is_not_listed_in_help(self):
        self.assertNotIn('/evolution', service.builtin_command({}, '/help'))
        self.assertIn('/evolution', service.builtin_command({'EVOLUTION_ENABLED': True}, '/help'))

    def test_evolution_sandbox_accepts_root_user_home(self):
        from assistant.evolution_worker import _sandbox_command
        with patch('assistant.evolution_worker.shutil.which', return_value='/mock/bwrap'), \
                patch('assistant.evolution_worker.Path.home', return_value=Path('/root')):
            command = _sandbox_command(Path('/tmp/source'), ['python3', '-V'])
        self.assertIn('/root', command)

    def test_local_report_uses_report_task_without_gitlab(self):
        from assistant.agent_backends.base import RunResult
        async def model(runner, request):
            if request.task_kind == 'classify':
                payload={'report':True,'publish_report':False,'deliver_files':True,'gitlab_mode':'none',
                         'repositories':False,'projects':[]}
                return RunResult(json.dumps(payload),payload)
            self.assertEqual(request.task_kind,'report')
            self.assertFalse(request.writable_roots)
            self.assertIn('线上报告发布未启用',request.prompt)
            return RunResult('报告已生成')
        cfg={'AGENT_BACKEND':'codex','CODEX_BIN':'codex','AGENT_TIMEOUT_SECONDS':10,
             'REPORT_SKILL_ENABLED':True}
        with patch('assistant.agent_runner.AgentRunner.run',model):
            self.assertEqual(asyncio.run(service.answer(cfg,[],'生成本地报告')),'报告已生成')
