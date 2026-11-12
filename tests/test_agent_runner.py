import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from assistant.agent_backends.base import RunRequest, AgentError
from assistant.agent_runner import AgentRunner, execute


@contextmanager
def passthrough(policy, cfg, work, executable):
    class Sandbox:
        work_path = work
        def map_path(self, path): return str(path)
        def wrap(self, args): return ['outer-isolation', *args]
        def publish(self): pass
    yield Sandbox()


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_preflight_exercises_isolation_before_enabling_ai(self):
        from assistant.agent_isolation import IsolationError
        with tempfile.TemporaryDirectory() as directory, \
                patch('assistant.agent_runner.shutil.which', return_value='/usr/bin/python3'), \
                patch('assistant.agent_runner.isolated', side_effect=IsolationError('fixture')):
            with self.assertRaises(AgentError):
                await AgentRunner({'AGENT_BACKEND':'grok'},Path(directory)).preflight()

    async def test_cli_name_is_resolved_before_isolation_command_construction(self):
        async def run(args, timeout, input_text=None, cwd=None):
            self.assertEqual(args[1], '/fixture/selected-grok')
            return '{"text":"ok"}'
        with tempfile.TemporaryDirectory() as directory, patch('assistant.agent_runner.isolated', passthrough), \
                patch('assistant.agent_runner.shutil.which', return_value='/fixture/selected-grok'):
            await AgentRunner({'AGENT_BACKEND':'grok'},Path(directory),run=run).run(RunRequest('chat','hello'))

    async def test_all_backends_are_always_wrapped_and_schema_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('codex', 'grok', 'claudecode'):
                calls = []
                async def run(args, timeout, input_text=None, cwd=None):
                    calls.append(args)
                    self.assertEqual(args[0], 'outer-isolation')
                    if name == 'codex':
                        Path(args[args.index('--output-last-message') + 1]).write_text('{"reply":"ok"}')
                        return ''
                    return json.dumps({'result': '{"reply":"ok"}'})
                cfg = {'AGENT_BACKEND': name, name.upper() + '_BIN': name}
                schema = {'type': 'object', 'properties': {'reply': {'type': 'string'}},
                          'required': ['reply'], 'additionalProperties': False}
                with patch('assistant.agent_runner.isolated', passthrough):
                    result = await AgentRunner(cfg, root, root, run=run).run(
                        RunRequest('classify', 'question', output_schema=schema))
                self.assertEqual(result.payload, {'reply': 'ok'})
                self.assertEqual(len(calls), 1)

    async def test_model_failure_is_not_replayed_and_raw_errors_are_not_exposed(self):
        calls = []
        async def run(*args, **kwargs):
            calls.append(args)
            raise RuntimeError('private credential in stderr')
        with tempfile.TemporaryDirectory() as directory, patch('assistant.agent_runner.isolated', passthrough):
            with self.assertRaises(AgentError) as error:
                await AgentRunner({'AGENT_BACKEND': 'grok', 'GROK_BIN': 'grok'},
                                  Path(directory), Path(directory), run=run).run(RunRequest('chat', 'hello'))
        self.assertEqual(len(calls), 1)
        self.assertNotIn('private credential', str(error.exception))

    async def test_unapproved_development_never_starts_a_cli(self):
        async def forbidden(*args, **kwargs): raise AssertionError('CLI started')
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises((ValueError, AgentError)):
                await AgentRunner({}, Path(directory), Path(directory), run=forbidden).run(
                    RunRequest('develop_execute', 'make a change'))

    async def test_execute_timeout_kills_process_group(self):
        with self.assertRaises(asyncio.TimeoutError):
            await execute(['/usr/bin/python3', '-c', 'import time;time.sleep(30)'], .05)

    async def test_execute_cancel_does_not_leave_process_running(self):
        task = asyncio.create_task(execute(['/usr/bin/python3', '-c', 'import time;time.sleep(30)'], 30))
        await asyncio.sleep(.03)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task

    async def test_execute_drops_raw_stderr_and_limits_stdout(self):
        with self.assertRaises(AgentError) as error:
            await execute(['/usr/bin/python3', '-c',
                'import sys;print("sensitive-token",file=sys.stderr);sys.exit(2)'], 5)
        self.assertNotIn('sensitive-token', str(error.exception))

    async def test_report_paths_stay_stable_after_gateway_delivery(self):
        generated=[]
        async def model(args,timeout,input_text=None,cwd=None):
            path=cwd/'report.html';path.write_text('<html>report</html>')
            generated.append(path)
            return '{"text":"done"}'
        with tempfile.TemporaryDirectory() as directory,patch('assistant.agent_runner.isolated',passthrough):
            root=Path(directory)
            result=await AgentRunner({'AGENT_BACKEND':'grok','GROK_BIN':'grok'},root,run=model).run(
                RunRequest('report','create report'))
            self.assertEqual(result.artifacts,tuple(generated))
            self.assertTrue(result.artifacts[0].is_file())
            self.assertFalse((result.artifacts[0].parent/'prompt.json').exists())
