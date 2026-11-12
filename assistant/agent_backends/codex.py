"""Codex argument/result mapping; process isolation belongs to the runner."""
import json
from .base import Backend, Invocation, final_result, AgentError, read_result


class CodexBackend(Backend):
    name = 'codex'

    def prepare(self, request, work, path):
        output = work / 'result.json'
        args = [self.binary, 'exec', '--skip-git-repo-check', '--ephemeral',
                '--ignore-user-config', '--ignore-rules', '--sandbox', 'danger-full-access',
                '-c', 'approval_policy="never"', '-c', 'web_search="disabled"',
                '--output-last-message', path(output)]
        if request.task_kind in ('classify', 'auto_query'):
            args += ['--disable', 'shell_tool']
        if request.output_schema is not None:
            schema = work / 'response.schema.json'
            schema.write_text(json.dumps(request.output_schema))
            args += ['--output-schema', path(schema)]
        if self.model:
            args += ['--model', self.model]
        for image in request.images:
            args += ['--image', path(image)]
        if request.on_event:
            args += ['--json']
        args.append('-')
        return Invocation(args, request.prompt, request.output_schema, output)

    def result(self, invocation, stdout):
        try:
            text = read_result(invocation.output_path)
        except OSError:
            raise AgentError('Codex 未生成最终结果') from None
        return final_result(text, invocation.schema)
