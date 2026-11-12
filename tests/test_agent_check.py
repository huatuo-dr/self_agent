from pathlib import Path
import unittest
from unittest.mock import patch
from assistant.agent_backends.base import RunResult,AgentError
from assistant.agent_check import live_check


class AgentCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_check_uses_temporary_fixture_and_selected_backend_without_business_tools(self):
        calls=[]
        async def model(runner,request):
            calls.append(request)
            self.assertNotEqual(runner.root,Path.cwd())
            self.assertEqual(runner.cfg['AGENT_BACKEND'],'grok')
            self.assertTrue(request.images[0].is_file())
            self.assertEqual(request.task_kind,'classify')
            return RunResult('{"left":"red","right":"blue"}',{'left':'red','right':'blue'})
        with patch('assistant.agent_runner.AgentRunner.run',model):
            result=await live_check({'AGENT_BACKEND':'grok'})
        self.assertEqual(result['model_and_image'],'passed')
        self.assertEqual(len(calls),1)

    async def test_live_check_does_not_call_another_backend_when_model_fails(self):
        async def fail(*a,**kw):raise AgentError('model unavailable')
        with patch('assistant.agent_runner.AgentRunner.run',fail):
            with self.assertRaises(AgentError):await live_check({'AGENT_BACKEND':'claudecode'})
