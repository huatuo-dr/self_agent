import json
from pathlib import Path
import tempfile
import unittest

from assistant.agent_backends.base import RunRequest, AgentError, validate_payload
from assistant.agent_backends import get_backend


SCHEMA = {'type': 'object', 'properties': {'reply': {'type': 'string'}},
          'required': ['reply'], 'additionalProperties': False}


class AgentBackendTests(unittest.TestCase):
    def test_selected_backend_registry_does_not_touch_other_binaries(self):
        for name, binary in [('codex', 'codex'), ('grok', 'grok'), ('claudecode', 'claude')]:
            with self.subTest(name=name):
                backend = get_backend({'AGENT_BACKEND': name, name.upper() + '_BIN': binary})
                self.assertEqual(backend.name, name)
                self.assertEqual(backend.binary, binary)
        self.assertEqual(get_backend({'CODEX_BIN': 'codex'}).name, 'codex')
        for invalid in ('', 'other'):
            with self.assertRaises(ValueError):
                get_backend({'AGENT_BACKEND': invalid})

    def test_commands_disable_vendor_sandbox_without_global_approval_bypass(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            request = RunRequest('report', 'create a report', output_schema=SCHEMA)
            for name in ('codex', 'grok', 'claudecode'):
                backend = get_backend({'AGENT_BACKEND': name, name.upper() + '_BIN': name})
                invocation = backend.prepare(request, work, lambda path: str(path))
                args = invocation.args
                if name == 'codex':
                    self.assertEqual(args[args.index('--sandbox') + 1], 'danger-full-access')
                    self.assertIn('--ignore-user-config', args)
                    self.assertIn('--output-schema', args)
                elif name == 'grok':
                    self.assertEqual(args[args.index('--sandbox') + 1], 'off')
                    self.assertIn('--no-plan', args)
                    self.assertIn('--no-subagents', args)
                    self.assertEqual(args[args.index('--permission-mode') + 1], 'dontAsk')
                else:
                    settings = json.loads(args[args.index('--settings') + 1])
                    self.assertFalse(settings['sandbox']['enabled'])
                    self.assertIn('--strict-mcp-config', args)
                    self.assertNotIn('--bare', args)
                    self.assertIn('--print', args)
                self.assertNotIn('--dangerously-skip-permissions', args)
                self.assertNotIn('--always-approve', args)

    def test_classification_has_no_native_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ('codex', 'grok', 'claudecode'):
                backend = get_backend({'AGENT_BACKEND': name, name.upper() + '_BIN': name})
                invocation = backend.prepare(RunRequest('classify', 'classify', output_schema=SCHEMA),
                                             Path(directory), str)
                if name == 'codex':
                    self.assertIn('shell_tool', invocation.args)
                    self.assertIn('web_search="disabled"', invocation.args)
                else:
                    self.assertEqual(invocation.args[invocation.args.index('--tools') + 1], '')

    def test_all_backends_attach_image_bytes_or_native_image_path(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            picture = work / 'input.png'
            picture.write_bytes(b'\x89PNG\r\n\x1a\n' + b'fixture')
            request = RunRequest('chat', 'describe', images=(picture,))
            for name in ('codex', 'grok', 'claudecode'):
                backend = get_backend({'AGENT_BACKEND': name, name.upper() + '_BIN': name})
                invocation = backend.prepare(request, work, str)
                if name == 'codex':
                    self.assertEqual(invocation.args[invocation.args.index('--image') + 1], str(picture))
                elif name == 'grok':
                    blocks = json.loads((work / 'prompt.json').read_text())
                    self.assertEqual(blocks[-1]['type'], 'image')
                    self.assertEqual(blocks[-1]['mimeType'], 'image/png')
                    self.assertIn('--prompt-file', invocation.args)
                    self.assertNotIn('--prompt-json', invocation.args)
                else:
                    message = json.loads(invocation.stdin)
                    self.assertEqual(message['message']['content'][-1]['source']['media_type'], 'image/png')
                    self.assertIn('stream-json', invocation.args)

    def test_backend_results_are_normalized_and_errors_do_not_leak_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            request = RunRequest('classify', 'question', output_schema=SCHEMA)
            cases = [('grok', {'structuredOutput': {'reply': 'ok'}, 'is_error': False}),
                     ('claudecode', {'structured_output': {'reply': 'ok'}, 'is_error': False})]
            for name, raw in cases:
                backend = get_backend({'AGENT_BACKEND': name, name.upper() + '_BIN': name})
                invocation = backend.prepare(request, work, str)
                result = backend.result(invocation, json.dumps(raw))
                self.assertEqual(result.payload, {'reply': 'ok'})
                with self.assertRaises(AgentError) as error:
                    backend.result(invocation, '{"is_error":true,"text":"private-token-value"}')
                self.assertNotIn('private-token-value', str(error.exception))

    def test_schema_validation_enforces_nested_types_enum_and_unknown_fields(self):
        schema = {'type': 'object', 'properties': {
            'matches': {'type': 'array', 'items': {'type': 'object',
                'properties': {'kind': {'type': 'string', 'enum': ['owner']}},
                'required': ['kind'], 'additionalProperties': False}}},
            'required': ['matches'], 'additionalProperties': False}
        validate_payload({'matches': [{'kind': 'owner'}]}, schema)
        for invalid in ({'matches': [{'kind': 'other'}]}, {'matches': 'owner'},
                        {'matches': [], 'extra': True}, {'matches': [{}]}):
            with self.assertRaises(AgentError):
                validate_payload(invalid, schema)

    def test_safe_events_never_forward_tool_output_or_thinking(self):
        for name in ('codex', 'grok', 'claudecode'):
            backend = get_backend({'AGENT_BACKEND': name, name.upper() + '_BIN': name})
            for raw in ({'type': 'thinking', 'text': 'private'},
                        {'type': 'tool_result', 'content': 'private'},
                        {'type': 'item.completed', 'item': {'type': 'reasoning', 'text': 'private'}}):
                self.assertIsNone(backend.event(raw))

    def test_codex_result_rejects_links_and_nonregular_outputs_before_host_read(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory);secret=work/'secret';secret.write_text('private fixture')
            backend=get_backend({'CODEX_BIN':'codex'})
            invocation=backend.prepare(RunRequest('chat','hello'),work,str)
            output=invocation.output_path
            output.symlink_to(secret)
            with self.assertRaises(AgentError):backend.result(invocation,'')
            output.unlink();os.link(secret,output)
            with self.assertRaises(AgentError):backend.result(invocation,'')
            output.unlink();os.mkfifo(output)
            with self.assertRaises(AgentError):backend.result(invocation,'')

    def test_streaming_adapters_parse_only_final_result_and_expose_safe_milestones(self):
        with tempfile.TemporaryDirectory() as directory:
            for name,key in [('grok','structuredOutput'),('claudecode','structured_output')]:
                backend=get_backend({'AGENT_BACKEND':name,name.upper()+'_BIN':name})
                request=RunRequest('chat','question',output_schema=SCHEMA,on_event=lambda event:None)
                invocation=backend.prepare(request,Path(directory),str)
                self.assertIn('streaming-json' if name=='grok' else 'stream-json',invocation.args)
                raw='\n'.join(json.dumps(x) for x in [
                    {'type':'thinking','text':'private'},
                    ({'type':'done','num_turns':1,'stopReason':'end_turn',key:{'reply':'ok'}} if name=='grok'
                     else {'type':'result','is_error':False,key:{'reply':'ok'}})])
                self.assertEqual(backend.result(invocation,raw).payload,{'reply':'ok'})

    def test_grok_plain_text_progress_keeps_final_text_in_json_result(self):
        with tempfile.TemporaryDirectory() as directory:
            backend=get_backend({'AGENT_BACKEND':'grok'})
            invocation=backend.prepare(RunRequest('chat','hello',on_event=lambda event:None),Path(directory),str)
            self.assertEqual(invocation.args[invocation.args.index('--output-format')+1],'json')
            self.assertEqual(backend.result(invocation,'{"text":"hello"}').text,'hello')
