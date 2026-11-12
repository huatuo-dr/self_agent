"""Grok Build headless CLI mapping, including native image content blocks."""
import base64
import json
from .base import Backend, Invocation, final_result, AgentError, result_object


def image_block(image):
    from assistant.image_archive import format_of
    suffix, _ = format_of(image)
    mime = {'.png': 'image/png', '.jpg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp'}[suffix]
    return {'type': 'image', 'data': base64.b64encode(image.read_bytes()).decode(), 'mimeType': mime}


class GrokBackend(Backend):
    name = 'grok'

    def prepare(self, request, work, path):
        blocks = [{'type': 'text', 'text': request.prompt}] + [image_block(p) for p in request.images]
        prompt = work / 'prompt.json'
        prompt.write_text(json.dumps(blocks, ensure_ascii=False))
        no_tools = request.task_kind in ('classify', 'auto_query')
        tools = '' if no_tools else 'read_file,grep,list_dir,run_terminal_cmd,search_replace'
        # Grok's streaming terminal carries structuredOutput but omits text.
        # Unstructured replies retain final JSON, with the runner emitting the
        # completion milestone rather than discarding the answer's text.
        streaming = request.on_event and request.output_schema is not None
        args = [self.binary, '--prompt-file', path(prompt), '--output-format', 'streaming-json' if streaming else 'json',
                '--no-auto-update', '--no-plan', '--no-subagents', '--disable-web-search',
                '--sandbox', 'off', '--permission-mode', 'dontAsk', '--tools', tools]
        if not no_tools:
            for tool in ('Read', 'Grep', 'Bash', 'Edit'):
                args += ['--allow', tool]
        if request.output_schema is not None:
            args += ['--json-schema', json.dumps(request.output_schema)]
        if self.model:
            args += ['--model', self.model]
        return Invocation(args, None, request.output_schema)

    def result(self, invocation, stdout):
        data = result_object(stdout)
        if not isinstance(data, dict) or data.get('is_error') or data.get('isError'):
            raise AgentError('Grok 执行失败')
        text = data.get('text') or data.get('result') or ''
        return final_result(text, invocation.schema, data.get('structuredOutput'))
