import asyncio
import json
from html import unescape
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from assistant.pipeline import DEFAULTS, PipelineMonitor, parse_fields
from assistant.pipeline_card import card
from assistant.service import database
from assistant.pipeline_gitlab import GitLabRateLimit
from tests.test_pipeline_card_charts import job, record


class PublicPipelineTests(unittest.TestCase):
    def test_rate_limit_notice_uses_configured_timezone(self):
        error = GitLabRateLimit('gitlab.example.com', 'projects/demo', 2000, {'TIMEZONE': 'UTC'})
        self.assertIn('00:33:20（UTC）', str(error))
        local = GitLabRateLimit('gitlab.example.com', 'projects/demo', 2000, {'TIMEZONE': 'Asia/Tokyo'})
        self.assertIn('09:33:20（Asia/Tokyo）', str(local))

    def test_monitor_without_publish_repository_uses_no_remote(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.local').mkdir()
            db = database(':memory:')
            run = AsyncMock(side_effect=AssertionError('No report remote required'))
            try:
                monitor = PipelineMonitor({'GITLAB_HOST': 'gitlab.example.com', 'GLAB_BIN': 'glab',
                                           'FEISHU_OWNER_CHAT_ID': 'oc_test'}, db, run, root)
                self.assertIsNone(monitor.pages_target)
                asyncio.run(monitor.output_tick())
                run.assert_not_awaited()
            finally:
                db.close()

    def test_card_uses_configured_name_signature_and_timezone(self):
        cfg = {'BOT_NAME': '小助手', 'TIMEZONE': 'UTC', 'MESSAGE_SIGNATURE_TEMPLATE': '{bot_name}代发'}
        payload = card({'config': dict(DEFAULTS, project_path='team/repo')},
                       record([job(1, 1000, 1600)]), greeting='你好，用户甲', cfg=cfg)
        content = unescape(json.dumps(payload, ensure_ascii=False))
        self.assertIn('小助手', content)
        self.assertIn('UTC', content)
        self.assertIn('00:33:20', content)
        self.assertIn('小助手代发', content)

    def test_history_date_range_uses_configured_local_midnights(self):
        fields, _ = parse_fields('首次历史补采：2020-03-08 至 2020-03-08', DEFAULTS,
                                 cfg={'TIMEZONE': 'America/New_York'})
        history = fields['history']
        self.assertEqual(history['end'] - history['start'], 23 * 3600)
