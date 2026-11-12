import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from assistant.agent_backends.base import RunResult
from assistant import evolution, group_watch
from assistant.auto_query import AutoReadOnlyQuery
from assistant.service import database


class SelectedBackendIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_queries_selected_backend_instead_of_codex(self):
        for name in ('grok', 'claudecode', 'codex'):
            calls = []
            async def model(runner, request):
                calls.append((runner.cfg['AGENT_BACKEND'], request))
                payload = {'action': 'answer', 'service': 'gitlab', 'tool': '', 'arguments': '{}', 'text': 'ok'}
                return RunResult(json.dumps(payload), payload)
            with tempfile.TemporaryDirectory() as directory, \
                    patch('assistant.agent_runner.AgentRunner.run', model), \
                    patch('assistant.agent_runner.AgentRunner.preflight'):
                query = AutoReadOnlyQuery({'AGENT_BACKEND': name, name.upper()+'_BIN': name}, Path(directory))
                from assistant.auto_agent import SCHEMA
                result = await query.decide('question', SCHEMA)
                self.assertEqual(result['text'], 'ok')
                self.assertEqual(calls[0][0], name)
                self.assertEqual(calls[0][1].task_kind, 'auto_query')

    async def test_group_classification_and_reply_use_selected_backend(self):
        db = database(':memory:')
        try:
            for name in ('grok', 'claudecode', 'codex'):
                calls = []
                async def model(runner, request):
                    calls.append((runner.cfg['AGENT_BACKEND'], request))
                    payload = ({'matches': [{'id': 'om_message', 'reason': 'needs owner'}]}
                               if len(calls)==1 else {'reply': 'ok'})
                    return RunResult(json.dumps(payload), payload)
                cfg = {'AGENT_BACKEND': name, 'FEISHU_OWNER_CHAT_ID': 'oc_owner',
                       'FEISHU_OWNER_OPEN_ID': 'ou_owner', 'LARK_CLI_BIN': 'lark-cli'}
                with patch('assistant.agent_runner.AgentRunner.run', model):
                    watch = group_watch.GroupWatch(cfg, db, None)
                    self.assertEqual((await watch.classify([{'id': 'om_message','text': 'hello'}]))['om_message'], 'needs owner')
                    self.assertIn('ok', await watch.compose_reply('reply', 'hello', 'sender'))
                self.assertEqual([x[0] for x in calls], [name, name])
                self.assertEqual([x[1].task_kind for x in calls], ['classify', 'classify'])
        finally: db.close()

    async def test_evolution_discussion_is_distinct_read_only_task(self):
        async def model(runner, request):
            self.assertEqual(runner.cfg['AGENT_BACKEND'], 'grok')
            self.assertEqual(request.task_kind, 'develop_discuss')
            self.assertFalse(request.writable_roots)
            payload = {'reply': 'need details', 'plan': '', 'ready': False}
            return RunResult(json.dumps(payload), payload)
        with patch('assistant.agent_runner.AgentRunner.run', model):
            result = await evolution.discuss({'AGENT_BACKEND':'grok','AGENT_TIMEOUT_SECONDS':10},
                                             [], '', 'discuss', '', None)
        self.assertFalse(result['ready'])

    async def test_service_answer_uses_runner_for_each_backend(self):
        from assistant import service
        for name in ('grok','claudecode','codex'):
            async def model(runner, request):
                self.assertEqual(runner.cfg['AGENT_BACKEND'],name)
                self.assertEqual(request.task_kind,'chat')
                payload={'reply':'ok','schedule':[],'used_skills':[],
                         'session':{'title':'topic','summary':'done','status':'已完成','next_step':''}}
                self.assertIn('AGENTS',request.prompt)
                return RunResult(json.dumps(payload),payload)
            with patch('assistant.agent_runner.AgentRunner.run',model):
                result=await service.answer({'AGENT_BACKEND':name,'AGENT_TIMEOUT_SECONDS':10},
                                            [],'hello',session={'id':1,'summary':''})
            self.assertEqual(json.loads(result)['reply'],'ok')

    async def test_approved_worker_calls_selected_backend_with_execute_policy(self):
        from assistant import evolution_worker
        for name in ('grok','claudecode','codex'):
            async def model(runner,request):
                self.assertEqual(runner.cfg['AGENT_BACKEND'],name)
                self.assertEqual(request.task_kind,'develop_execute')
                self.assertEqual(request.approval_ref,'server-approved')
                return RunResult('done')
            with tempfile.TemporaryDirectory() as directory,patch('assistant.agent_runner.AgentRunner.run',model):
                output=Path(directory)/'result.txt'
                code=await __import__('asyncio').to_thread(evolution_worker._run_agent,
                    {'AGENT_BACKEND':name},'implement',30,output,'server-approved')
                self.assertEqual(code,0)
                self.assertEqual(output.read_text(),'done')

    async def test_business_gateway_does_not_reuse_generic_token_for_another_host(self):
        from assistant import service
        import os
        seen=[]
        async def command(args,timeout,input_text=None,cwd=None,env=None):
            seen.append(env);return '{}'
        with patch.dict(os.environ,{'GITLAB_TOKEN':'fixture-token','GIT_CONFIG_COUNT':'1'}), \
                patch('assistant.agent_runner.execute',command):
            await service.run_agent_business({'GITLAB_HOST':'gitlab.example.com'},
                ['glab','api','projects','--hostname','another.example'],10)
            await service.run_agent_business({'GITLAB_HOST':'gitlab.example.com'},
                ['glab','api','projects','--hostname','gitlab.example.com'],10)
        self.assertNotIn('GITLAB_TOKEN',seen[0])
        self.assertEqual(seen[1]['GITLAB_TOKEN'],'fixture-token')
        self.assertNotIn('GIT_CONFIG_COUNT',seen[0])
