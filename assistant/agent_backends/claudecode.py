"""Claude Code CLI mapping with explicit settings and no native sandbox."""
import json
from .base import Backend, Invocation, final_result, AgentError, result_object
from .grok import image_block


class ClaudeCodeBackend(Backend):
    name = 'claudecode'

    def prepare(self, request, work, path):
        no_tools = request.task_kind in ('classify', 'auto_query')
        tools = '' if no_tools else 'Read,Glob,Grep,Bash,Edit,Write'
        settings = {'sandbox': {'enabled': False}, 'disableAllHooks': True,
                    'enableAllProjectMcpServers': False, 'permissions': {'defaultMode': 'dontAsk'}}
        args = [self.binary, '--print', '--output-format', 'stream-json' if request.on_event else 'json', '--no-session-persistence',
                '--settings', json.dumps(settings), '--setting-sources', '',
                '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                '--disable-slash-commands', '--permission-mode', 'dontAsk', '--tools', tools]
        if request.on_event:
            args += ['--verbose']
        if tools:
            args += ['--allowedTools', tools]
        if request.output_schema is not None:
            args += ['--json-schema', json.dumps(request.output_schema)]
        if self.model:
            args += ['--model', self.model]
        stdin = request.prompt
        if request.images:
            blocks = [{'type': 'text', 'text': request.prompt}]
            for image in request.images:
                raw = image_block(image)
                blocks.append({'type': 'image', 'source': {'type': 'base64',
                               'media_type': raw['mimeType'], 'data': raw['data']}})
            stdin = json.dumps({'type': 'user', 'message': {'role': 'user', 'content': blocks}}) + '\n'
            args += ['--input-format', 'stream-json']
        return Invocation(args, stdin, request.output_schema)

    def result(self, invocation, stdout):
        data = result_object(stdout)
        if not isinstance(data, dict) or data.get('is_error') or data.get('subtype', '').startswith('error'):
            raise AgentError('Claude Code 执行失败')
        return final_result(data.get('result', ''), invocation.schema, data.get('structured_output'))
