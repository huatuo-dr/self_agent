"""One execution path for every AI task, independent of CLI and business logic."""

import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
import json
import os
import re
from pathlib import Path
import shutil
import signal
import tempfile

from .agent_backends import get_backend
from .agent_backends.base import RunRequest, RunResult, AgentError
from .agent_policy import policy_for
from .agent_native_config import native_config
from .agent_isolation import isolated, IsolationError

MAX_OUTPUT_BYTES = 8 * 1024 * 1024


async def execute(args, timeout, input_text=None, cwd=None, on_event=None, env=None):
    """Bounded output, no raw stderr, and cancellation of the entire task group."""
    process = await asyncio.create_subprocess_exec(
        *map(str, args), cwd=cwd, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True, limit=MAX_OUTPUT_BYTES, env=env)
    async def stderr():
        while await process.stderr.read(65536):
            pass
    errors = asyncio.create_task(stderr())
    async def consume():
        if input_text is not None:
            process.stdin.write(input_text.encode())
            try:
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
        process.stdin.close()
        pieces, count = [], 0
        while True:
            chunk = await process.stdout.readline()
            if not chunk:
                break
            count += len(chunk)
            if count > MAX_OUTPUT_BYTES:
                raise AgentError('AI 输出超过大小限制')
            pieces.append(chunk)
            if on_event:
                try:
                    raw = json.loads(chunk)
                except (ValueError, UnicodeError):
                    continue
                on_event(raw)
        await process.wait()
        if process.returncode:
            raise AgentError('AI 工具执行失败（退出码 %s）' % process.returncode)
        return b''.join(pieces).decode('utf-8', errors='replace')
    try:
        return await asyncio.wait_for(consume(), timeout)
    finally:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), 1)
        except asyncio.TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        errors.cancel()
        await asyncio.gather(errors, return_exceptions=True)


@asynccontextmanager
async def _no_gateway(work, sandbox):
    yield ''


@contextmanager
def task_directory(report_parent=None):
    if report_parent is None:
        with tempfile.TemporaryDirectory(prefix='agent-task-') as directory:
            yield Path(directory)
    else:
        # Stable identities allow gateway send_file and service auto-delivery to
        # agree. Keep user outputs on failures; remove internal model payloads.
        work = Path(tempfile.mkdtemp(prefix='report-', dir=report_parent))
        try:
            yield work
        finally:
            for name in ('result.json', 'response.schema.json', 'prompt.json', 'tool_client.py'):
                path = work / name
                if path.is_file() or path.is_symlink():
                    path.unlink()



class AgentRunner:
    def __init__(self, cfg, root=None, state=None, run=None, gateway=None):
        self.cfg = cfg
        self.root = Path(root or Path(__file__).resolve().parents[1]).absolute()
        self.state = Path(state) if state else self.root / '.local'
        self.backend = get_backend(cfg)
        self.execute = run
        self.gateway = gateway or _no_gateway
        self._preflight_ready = False

    async def preflight(self):
        binary = shutil.which(self.backend.binary)
        if not binary:
            raise AgentError('找不到所选 AI 工具：' + self.backend.name)
        if not shutil.which('bwrap'):
            raise AgentError('统一 AI 隔离需要 bwrap；不会退回无隔离执行')
        if not self._preflight_ready:
            try:
                effective = native_config(self.cfg)
                with tempfile.TemporaryDirectory(prefix='agent-preflight-') as directory:
                    work = Path(directory)
                    with isolated(policy_for(effective, 'classify', self.root), effective, work, binary) as box:
                        await execute(box.wrap([binary, '--version']), 30, cwd=work)
            except (IsolationError, OSError, RuntimeError, ValueError, asyncio.TimeoutError):
                raise AgentError('所选 CLI 未通过统一隔离启动检查：' + self.backend.name) from None
            self._preflight_ready = True
        return binary

    async def run(self, request):
        if (not isinstance(request, RunRequest) or not isinstance(request.prompt, str)
                or not request.prompt.strip() or not 0 < request.timeout <= 3600):
            raise ValueError('Invalid agent request')
        # Service callers construct the permission request. Model output is never
        # fed back as an approval token or an unrestricted directory grant.
        effective_cfg = native_config(self.cfg)
        backend = get_backend(effective_cfg)
        policy = policy_for(effective_cfg, request.task_kind, self.root,
                            writable_roots=request.writable_roots,
                            input_files=(*request.input_files, *request.images),
                            approval_ref=request.approval_ref, read_only_dirs=request.read_only_dirs)
        parent = None
        if request.task_kind == 'report':
            parent = self.root / '.project/reports'
            if parent.is_symlink() or parent.parent.is_symlink():
                raise AgentError('报告工作区不能是符号链接')
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Never bind the service state directory into an AI process.
        with task_directory(parent) as work:
            binary = shutil.which(backend.binary) or backend.binary
            backend.binary = binary
            try:
                with isolated(policy, effective_cfg, work, binary) as sandbox:
                    replacements = getattr(sandbox, 'path_map', {})
                    prompt = request.prompt
                    for host, alias in sorted(replacements.items(), key=lambda item: len(str(item[0])), reverse=True):
                        prompt = prompt.replace(str(host), str(alias))
                    if request.task_kind == 'report':
                        report_dir = json.dumps(str(sandbox.work_path), ensure_ascii=False)
                        prompt = re.sub(r'^REPORTS_DIR=.*$', 'REPORTS_DIR=' + report_dir, prompt, flags=re.M)
                        prompt += '\n本轮只在报告工作区生成产物：' + report_dir + '。工作区不是助手源码目录。'
                    async with self.gateway(work, sandbox) as tools:
                        prepared = replace(request, prompt=prompt + (tools or ''))
                        invocation = backend.prepare(prepared, work, lambda p: str(sandbox.map_path(p)))
                        def event(raw):
                            safe = backend.event(raw)
                            if safe is not None and request.on_event:
                                request.on_event(safe)
                        command = sandbox.wrap(invocation.args)
                        if self.execute is None:
                            stdout = await execute(command, request.timeout, invocation.stdin,
                                                   cwd=work, on_event=event if request.on_event else None)
                        else:
                            stdout = await self.execute(command, request.timeout, invocation.stdin, cwd=work)
                        result = backend.result(invocation, stdout)
                        sandbox.publish()
                        if request.on_event:
                            event({'type': 'result'})
                        artifacts = self._reports(work) if request.task_kind == 'report' else ()
                        return RunResult(result.text, result.payload, artifacts)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                raise
            except AgentError:
                raise
            except (IsolationError, OSError, RuntimeError, ValueError) as error:
                # Diagnostics cannot include CLI stderr, arguments or credentials.
                raise AgentError('AI 执行或隔离失败（%s）；任务未确认完成' % type(error).__name__) from None

    def _reports(self, work):
        """Validate deliverables without changing their task-scoped identities."""
        artifacts = []
        excluded = {'result.json', 'response.schema.json', 'prompt.json', 'tool_client.py'}
        candidates = [p for p in work.iterdir() if p.name not in excluded and
                      p.suffix.lower() in ('.html', '.pdf', '.md', '.csv', '.txt', '.png', '.svg')]
        for candidate in candidates:
            info = candidate.lstat()
            import stat
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_size > 50 * 1024 * 1024):
                raise AgentError('报告产物路径或大小无效')
            artifacts.append(candidate)
        return tuple(artifacts)
