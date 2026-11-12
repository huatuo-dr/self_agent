"""Commands and usage feedback for the configured bot skills."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from assistant import service


class BotSkillTests(unittest.TestCase):
    def setUp(self):
        from agent_test_support import install_agent_fixture
        install_agent_fixture(self, service)

    def test_local_bot_skill_usage_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            async def run(args, timeout, input_text=None, cwd=None):
                schema = json.loads(Path(args[args.index('--output-schema') + 1]).read_text())
                self.assertIn('used_skills', schema['required'])
                self.assertIn('schedule', schema['required'])
                self.assertIn('history-search', input_text)
                self.assertIn('schedule-assistant', input_text)
                payload = {'reply': '已找到原话题', 'used_skills': ['history-search'], 'schedule': [],
                           'session': {'title': '检索历史', 'summary': '已找到',
                                       'status': '已完成', 'next_step': ''}}
                Path(args[args.index('--output-last-message') + 1]).write_text(json.dumps(payload))
                return ''
            cfg = {'AGENT_BACKEND': 'codex', 'CODEX_BIN': 'codex', 'AGENT_TIMEOUT_SECONDS': 10}
            with patch.object(service, 'STATE', Path(directory)), \
                    patch.object(service, 'run_command', run):
                result = json.loads(asyncio.run(service.answer(
                    cfg, [], '找之前的讨论', session={'id': 1, 'summary': ''})))
            self.assertIn('本次使用技能：history-search', result['reply'])
            self.assertEqual(result['session']['status'], '已完成')

    def test_help_restores_skill_query_when_enabled(self):
        reply = service.builtin_command({'SKILLS_REPO_ENABLED': True,
                                         'PIPELINE_MONITOR_ENABLED': True}, '/help')
        self.assertIn('/skill', reply)
        self.assertIn('/pipeline', reply)

    def test_disabled_skill_command_does_not_invoke_model(self):
        async def check():
            db = service.database(':memory:')
            cfg = {'FEISHU_OWNER_CHAT_ID': 'oc_test', 'FEISHU_OWNER_OPEN_ID': 'ou_test',
                   'AGENT_HISTORY_TURNS': 2, 'LARK_CLI_BIN': 'lark-cli',
                   'SKILLS_REPO_ENABLED': False}
            db.execute("INSERT INTO inbox(id,binding,content,kind,acknowledged,thread_checked) "
                       "VALUES('om_skill','oc_test','/skill issue','text',1,1)")
            db.commit()
            sent = asyncio.Event()
            async def run(args, *unused, **kwargs):
                self.assertIn('未启用', args[args.index('--markdown') + 1])
                sent.set()
                return '{"ok":true}'
            with patch.object(service, 'answer', AsyncMock(side_effect=AssertionError('No model'))), \
                    patch.object(service, 'run_command', run):
                task = asyncio.create_task(service.worker(cfg, db))
                try:
                    await asyncio.wait_for(sent.wait(), 2)
                    self.assertEqual(db.execute("SELECT state FROM inbox WHERE id='om_skill'").fetchone()[0], 'done')
                    self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0], 0)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    db.close()
        asyncio.run(check())
