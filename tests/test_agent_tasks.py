import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from assistant.agent_backends.base import RunResult, AgentError
from assistant.agent_tasks import plan_task


class AgentTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_only_target_does_not_grant_repository_access(self):
        async def model(runner, request):
            data={'report':False,'publish_report':False,'deliver_files':False,'gitlab_mode':'read',
                  'repositories':False,'projects':[{'host':'gitlab.example.com','project':'team/repo'}]}
            return RunResult(json.dumps(data),data)
        with patch('assistant.agent_runner.AgentRunner.run',model):
            result=await plan_task({'GITLAB_SKILL_ENABLED':True,'GITLAB_HOST':'gitlab.example.com'},
                                   [],'query MR status',Path('.'))
        self.assertFalse(result['repositories'])

    async def test_plain_chat_without_optional_features_needs_no_scope_model(self):
        with patch('assistant.agent_runner.AgentRunner.run', side_effect=AssertionError('extra AI call')):
            result = await plan_task({}, [], 'hello', Path('.'))
        self.assertEqual(result['kind'], 'chat')
        self.assertEqual(result['gitlab_mode'], 'none')

    async def test_scope_uses_selected_backend_without_tools_and_is_feature_bounded(self):
        async def model(runner, request):
            self.assertEqual(runner.cfg['AGENT_BACKEND'], 'claudecode')
            self.assertEqual(request.task_kind, 'classify')
            data = {'report': True, 'publish_report': False, 'deliver_files': True, 'gitlab_mode': 'none',
                    'repositories': False, 'projects': []}
            return RunResult(json.dumps(data), data)
        with patch('assistant.agent_runner.AgentRunner.run', model):
            result = await plan_task({'AGENT_BACKEND':'claudecode','REPORT_SKILL_ENABLED':True},
                                     [], 'create report', Path('.'))
        self.assertEqual(result['kind'], 'report')
        self.assertTrue(result['deliver_files'])

    async def test_gitlab_scope_rejects_unknown_host_or_path_traversal(self):
        for host,project in [('elsewhere.example','group/repo'),('gitlab.example.com','group/../repo')]:
            async def model(runner, request):
                data={'report':False,'publish_report':False,'deliver_files':False,'gitlab_mode':'write',
                      'repositories':True,'projects':[{'host':host,'project':project}]}
                return RunResult(json.dumps(data),data)
            with patch('assistant.agent_runner.AgentRunner.run',model):
                with self.assertRaises(AgentError):
                    await plan_task({'GITLAB_SKILL_ENABLED':True,'GITLAB_HOST':'gitlab.example.com'},
                                    [], 'merge current MR', Path('.'))

    async def test_online_report_uses_configured_repository_and_branch(self):
        async def model(runner,request):
            self.assertIn('REPORT_PAGES_BRANCH',request.prompt)
            payload={'report':True,'publish_report':True,'deliver_files':True,
                     'gitlab_mode':'write','repositories':False,'projects':[]}
            return RunResult(json.dumps(payload),payload)
        cfg={'REPORT_SKILL_ENABLED':True,'REPORT_PUBLISH_ENABLED':True,'GITLAB_SKILL_ENABLED':True,
             'GITLAB_HOST':'gitlab.example.com','REPORT_PAGES_REPO_URL':'ssh://git@gitlab.example.com:2222/team/reports.git',
             'REPORT_PAGES_BRANCH':'pages'}
        with patch('assistant.agent_runner.AgentRunner.run',model):
            result=await plan_task(cfg,[],'create and publish report',Path('.'))
        self.assertTrue(result['publish_report'])
        self.assertTrue(result['repositories'])
        self.assertEqual(result['projects'],[{'host':'gitlab.example.com','project':'team/reports'}])
