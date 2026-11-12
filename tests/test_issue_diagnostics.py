import asyncio
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch


class IssueDiagnosticsTests(unittest.TestCase):
    def test_scheduler_honors_intervals_shorter_than_poll_default(self):
        from assistant.issues import IssueAssistant
        from assistant.service import database
        db = database(':memory:')
        try:
            with tempfile.TemporaryDirectory() as directory:
                task = IssueAssistant({'GITLAB_HOST': 'gitlab.example.com',
                                       'FEISHU_OWNER_CHAT_ID': 'oc_test',
                                       'ISSUE_WATCH_INTERVAL_SECONDS': 5}, db, None, directory, directory)
                sleeper = AsyncMock(side_effect=asyncio.CancelledError())
                with patch.object(task, 'tick', AsyncMock()), \
                        patch('assistant.issues.asyncio.sleep', sleeper):
                    with self.assertRaises(asyncio.CancelledError):
                        asyncio.run(task.scheduler())
                self.assertEqual(sleeper.call_args.args[0], 5)
        finally:
            db.close()

    def test_workday_diagnostic_handles_all_local_calendar_modes(self):
        spec = importlib.util.spec_from_file_location('issue_diagnostic', Path(__file__).resolve().parents[1] / 'scripts/issue_watch.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for mode in ('weekdays', 'everyday', 'calendar_file'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                calendar = root / 'calendar.json'
                from datetime import datetime
                from assistant.settings import get_timezone
                calendar.write_text(json.dumps({'year': datetime.now(get_timezone()).year,
                                                'holidays': [], 'workdays': []}))
                cfg = {'GITLAB_SKILL_ENABLED': True, 'GITLAB_HOST': 'gitlab.example.com',
                       'FEISHU_OWNER_CHAT_ID': 'oc_test', 'WORKDAY_MODE': mode,
                       'WORKDAY_CALENDAR_FILE': str(calendar)}
                output = io.StringIO()
                with patch.object(module, 'config', return_value=cfg), \
                        patch.object(module, 'STATE', root), \
                        patch('sys.argv', ['issue_watch.py', 'workday']), redirect_stdout(output):
                    asyncio.run(module.main())
                self.assertIsInstance(json.loads(output.getvalue())['workday'], bool)
