import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from assistant.auto_query import AutoReadOnlyQuery
from assistant.auto_agent import SCHEMA
from assistant.auto_reply import AdapterUnavailable
from assistant.agent_backends.base import RunResult


class AutoQueryTests(unittest.TestCase):
    def test_auto_preflight_checks_selected_backend_only(self):
        with tempfile.TemporaryDirectory() as directory:
            query = AutoReadOnlyQuery({'AGENT_BACKEND': 'grok', 'GROK_BIN': '/missing/grok'}, Path(directory))
            with self.assertRaisesRegex(AdapterUnavailable, 'grok'):
                asyncio.run(query.preflight())

    def test_decision_uses_shared_runner_without_repeating_enable_preflight(self):
        decision = {'action': 'ignore', 'service': 'none', 'tool': '', 'arguments': '{}', 'text': ''}
        async def model(runner, request):
            self.assertEqual(request.task_kind, 'auto_query')
            self.assertEqual(request.output_schema, SCHEMA)
            self.assertEqual(request.prompt, 'fixture decision and context')
            self.assertEqual(request.timeout, 90)
            return RunResult('', decision)
        with tempfile.TemporaryDirectory() as directory, \
                patch('assistant.agent_runner.AgentRunner.run', model), \
                patch('assistant.agent_runner.AgentRunner.preflight', AsyncMock()) as preflight:
            query = AutoReadOnlyQuery({'AGENT_BACKEND': 'grok'}, Path(directory))
            self.assertEqual(asyncio.run(query.decide('fixture decision and context', SCHEMA)), decision)
            preflight.assert_not_awaited()
