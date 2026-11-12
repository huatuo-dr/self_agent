"""Read-only GitLab collection and versioned Pipeline metrics.

Raw API fields are retained locally. Execution identity is built from observations,
not from job names or SHA. Unknown retry history is explicitly incomplete.
"""

import asyncio
import hashlib
import json
import math
import re
import statistics
import time
import weakref

from assistant.http_errors import http_metadata
from datetime import datetime, timezone
from assistant.settings import get_timezone
from urllib.parse import quote, urlencode, urlsplit

TERMINAL = {"success", "failed", "canceled", "skipped"}
METRIC_VERSION = 1
MAX_PAGES = 50
API_BUDGET_SECONDS = 120
TRACE_BYTE_LIMIT = 2 * 1024 * 1024


REQUEST_INTERVAL = 0.5  # Shared per host: at most two request starts per second.
_GATES = weakref.WeakKeyDictionary()


class GitLabAPIError(RuntimeError):
    def __init__(self, endpoint, status=None, reason="CLI 调用失败"):
        self.endpoint = endpoint.split("?", 1)[0]
        self.status = status
        code = f"HTTP {status}" if status else reason
        super().__init__(f"GitLab 请求失败（{code}）：{self.endpoint}")


class GitLabRateLimit(GitLabAPIError):
    def __init__(self, host, endpoint, retry_at, cfg=None):
        super().__init__(endpoint, 429)
        self.host, self.retry_at = host, retry_at
        self.zone = get_timezone(cfg)

    def __str__(self):
        until = datetime.fromtimestamp(
            self.retry_at, self.zone
        ).strftime("%H:%M:%S")
        return f"GitLab 限流（HTTP 429），{until}（{self.zone.key}）后自动重试；已保留采集进度。接口：{self.endpoint}"


class RequestGate:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.next_start = 0
        self.not_before = 0
        self.interval = REQUEST_INTERVAL

    async def wait(self, host, endpoint, cfg=None):
        async with self.lock:
            if time.time() < self.not_before:
                raise GitLabRateLimit(host, endpoint, self.not_before, cfg)
            delay = self.next_start - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if time.time() < self.not_before:
                raise GitLabRateLimit(host, endpoint, self.not_before, cfg)
            self.next_start = time.monotonic() + self.interval

    def limited(self, retry_after):
        self.not_before = max(self.not_before, time.time() + (retry_after or 60))
        self.interval = min(5, max(1, self.interval * 2))
        return self.not_before


def request_gate(host):
    gates = _GATES.setdefault(asyncio.get_running_loop(), {})
    return gates.setdefault(host, RequestGate())


def response_body(text):
    # glab --include prepends one or more HTTP header blocks.
    text = text.replace("\r\n", "\n")
    while text.startswith("HTTP/"):
        headers, separator, body = text.partition("\n\n")
        if not separator:
            raise ValueError("GitLab HTTP 响应头不完整")
        text = body
    return text


def stamp(value):
    if not value:
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return d.timestamp() if d.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def number(value):
    return (
        value
        if type(value) in (int, float) and math.isfinite(value) and value >= 0
        else None
    )


def project_path(url, host):
    if url.startswith(f"git@{host}:"):
        path = url.split(":", 1)[1]
    else:
        u = urlsplit(url)
        if (
            u.scheme != "https"
            or u.hostname != host
            or u.port
            or u.username
            or u.password
            or u.query
            or u.fragment
        ):
            raise ValueError("请提供配置的 GitLab 主机上的 HTTPS 或 SSH 仓库链接。")
        path = u.path.strip("/")
    path = path.removesuffix(".git")
    if not re.fullmatch(r"[\w.-]+(?:/[\w.-]+)+", path) or any(
        p in (".", "..", "-") for p in path.split("/")
    ):
        raise ValueError("仓库链接须指向项目，不能是 Pipeline、MR 或文件链接。")
    return path


class GitLab:
    def __init__(self, cfg, run):
        self.cfg, self.run = cfg, run
        self.host = cfg["GITLAB_HOST"]

    async def api(self, endpoint, raw=False, *, query=None):
        gate = request_gate(self.host)
        gate.not_before = max(gate.not_before, getattr(self, "not_before", 0))
        await gate.wait(self.host, endpoint, self.cfg)
        try:
            text = await self.run(
                [
                    self.cfg["GLAB_BIN"],
                    "api",
                    "--hostname",
                    self.host,
                    "--include",
                    endpoint,
                    *(["--raw-field", "query=" + query] if query is not None else []),
                ],
                60,
            )
        except asyncio.TimeoutError:
            raise GitLabAPIError(endpoint, reason="请求超时（60 秒）") from None
        except OSError:
            raise GitLabAPIError(endpoint, reason="无法启动 GitLab CLI") from None
        except RuntimeError as error:
            status = getattr(error, "http_status", None)
            if status == 429:
                until = gate.limited(getattr(error, "retry_after", None))
                raise GitLabRateLimit(self.host, endpoint, until, self.cfg) from None
            raise GitLabAPIError(endpoint, status) from None
        text = response_body(text)
        return text if raw else json.loads(text)

    async def page(self, endpoint, page=1, **params):
        result = await self.api(
            endpoint + "?" + urlencode(dict(per_page=100, page=page, **params))
        )
        if not isinstance(result, list):
            raise ValueError("GitLab 列表响应无效")
        return result

    async def all(self, endpoint, **params):
        async def fetch():
            rows = []
            for page in range(1, MAX_PAGES + 1):
                batch = await self.page(endpoint, page, **params)
                rows.extend(batch)
                if len(batch) < 100:
                    return rows
            raise ValueError("GitLab 分页超过采集上限，保留缺口待重试")

        return await asyncio.wait_for(fetch(), API_BUDGET_SECONDS)

    async def trace(self, project, jid):
        """Bound memory while reading; a server ignoring Range cannot exhaust memory."""
        from assistant.service import terminate

        endpoint = f"projects/{project}/jobs/{jid}/trace"
        gate = request_gate(self.host)
        gate.not_before = max(gate.not_before, getattr(self, "not_before", 0))
        await gate.wait(self.host, endpoint, self.cfg)
        process = await asyncio.create_subprocess_exec(
            self.cfg["GLAB_BIN"],
            "api",
            "--hostname",
            self.host,
            "--include",
            endpoint,
            "--header",
            "Range: bytes=-200000",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )

        async def read():
            tail, prefix, total = b"", b"", 0
            while chunk := await process.stdout.read(65536):
                prefix = (prefix + chunk)[:16384]
                total += len(chunk)
                if total > TRACE_BYTE_LIMIT:
                    raise ValueError("日志超出读取上限，保留平台原因及待分析标记")
                tail = (tail + chunk)[-200000:]
            if await process.wait():
                status, retry = http_metadata(prefix.decode("utf-8", errors="replace"))
                if status == 429:
                    raise GitLabRateLimit(self.host, endpoint, gate.limited(retry), self.cfg)
                raise GitLabAPIError(endpoint, status)
            return tail.decode("utf-8", errors="replace")

        try:
            return await asyncio.wait_for(read(), 30)
        finally:
            await terminate(process)

    async def project(self, url):
        path = project_path(url, self.host)
        data = await self.api("projects/" + quote(path, safe=""))
        if data.get("path_with_namespace") != path or type(data.get("id")) is not int:
            raise ValueError("GitLab 项目身份不匹配")
        samples = await self.page(f"projects/{data['id']}/pipelines")
        if samples:
            await self.page(
                f"projects/{data['id']}/pipelines/{samples[0]['id']}/jobs",
                include_retried="true",
            )
        return data

    async def scope(self, config, pipeline):
        if pipeline.get("source") == "parent_pipeline":
            return None
        if pipeline.get("tag"):
            return pipeline.get("ref") if config["include_tags"] else None
        branch = pipeline.get("ref")
        if pipeline.get("source") == "merge_request_event":
            branch = await self.mr_target(config["project_id"], pipeline)
        wanted = config["branches"]
        if wanted == ["default"]:
            project = await self.api(f"projects/{config['project_id']}")
            wanted = [project.get("default_branch")]
        return branch if wanted == ["all"] or branch in wanted else None

    async def mr_target(self, project, pipeline):
        # GitLab has MR -> pipelines, not pipelines/:id/merge_requests.
        # A commit can belong to several MRs: verify the exact pipeline ID.
        ref = re.fullmatch(
            r"refs/merge-requests/(\d+)/(?:head|merge|train)", pipeline.get("ref") or ""
        )
        candidates = []
        if ref:
            try:
                candidates = [
                    await self.api(f"projects/{project}/merge_requests/{ref[1]}")
                ]
            except GitLabAPIError as error:
                if error.status != 404:
                    raise
        if not candidates:
            sha = pipeline.get("sha", "")
            if not re.fullmatch(r"[a-fA-F0-9]{40,64}", sha):
                raise ValueError("MR Pipeline 缺少可核实的引用或提交")
            candidates = await self.all(
                f"projects/{project}/repository/commits/{sha}/merge_requests"
            )
        if len(candidates) > 20:
            raise ValueError("MR 候选超过核实上限，保留缺口待重试")
        matches = {}
        for mr in candidates:
            target_project, iid = mr.get("project_id"), mr.get("iid")
            if (
                type(target_project) is not int
                or type(iid) is not int
                or not mr.get("target_branch")
            ):
                raise ValueError("MR 身份或目标分支不完整")
            pipelines = await self.all(
                f"projects/{target_project}/merge_requests/{iid}/pipelines"
            )
            if any(
                p.get("id") == pipeline["id"] and p.get("project_id") == project
                for p in pipelines
            ):
                matches[(target_project, iid)] = mr["target_branch"]
        if len(matches) != 1:
            raise ValueError("MR 目标分支不能唯一核实")
        return next(iter(matches.values()))

    async def collect(self, project, pid):
        return await asyncio.wait_for(self._collect(project, pid, set(), {}), 120)

    async def stage_order(self, pipeline):
        """Read this exact pipeline's stage order; never infer it from start times."""
        pid, iid = pipeline.get("id"), pipeline.get("iid")
        if type(pid) is not int or type(iid) is not int:
            return {}
        try:
            url = urlsplit(pipeline.get("web_url") or "")
        except ValueError:
            return {}
        suffix = f"/-/pipelines/{pid}"
        if (
            url.scheme != "https"
            or url.netloc != self.host
            or not url.path.endswith(suffix)
        ):
            return {}
        path = url.path[: -len(suffix)].strip("/")

        async def fetch():
            names, cursor = [], None
            for _ in range(MAX_PAGES):
                query = (
                    "{project(fullPath:"
                    + json.dumps(path)
                    + "){pipeline(iid:"
                    + json.dumps(str(iid))
                    + "){id stages(first:100"
                    + (",after:" + json.dumps(cursor) if cursor else "")
                    + "){nodes{name} pageInfo{hasNextPage endCursor}}}}}"
                )
                result = await self.api("graphql", query=query)
                if not isinstance(result, dict) or result.get("errors"):
                    return {}
                data = result
                for key in ("data", "project", "pipeline"):
                    data = data.get(key) if isinstance(data, dict) else None
                if not isinstance(data, dict):
                    return {}
                if data.get("id") != f"gid://gitlab/Ci::Pipeline/{pid}":
                    return {}
                stages = data.get("stages") or {}
                if not isinstance(stages, dict):
                    return {}
                nodes, page = stages.get("nodes"), stages.get("pageInfo") or {}
                if (
                    not isinstance(nodes, list)
                    or not isinstance(page, dict)
                    or type(page.get("hasNextPage")) is not bool
                ):
                    return {}
                for node in nodes:
                    name = node.get("name") if isinstance(node, dict) else None
                    if not isinstance(name, str) or not name or name in names:
                        return {}
                    names.append(name)
                if not page["hasNextPage"]:
                    return {name: index for index, name in enumerate(names)}
                next_cursor = page.get("endCursor")
                if (
                    not isinstance(next_cursor, str)
                    or not next_cursor
                    or next_cursor == cursor
                    or len(next_cursor) > 4096
                ):
                    return {}
                cursor = next_cursor
            return {}

        try:
            return await asyncio.wait_for(fetch(), API_BUDGET_SECONDS)
        except GitLabRateLimit:
            raise
        except (GitLabAPIError, ValueError, asyncio.TimeoutError):
            # Jobs and execution metrics remain useful if optional GraphQL metadata is unavailable.
            return {}

    async def _collect(self, project, pid, seen, cache):
        if pid in seen or len(seen) >= 100 or len(cache) >= 100:
            raise ValueError("子流水线循环或过深，采集不完整")
        if pid in cache:
            return cache[pid]
        seen = seen | {pid}  # recursion path, not all nodes ever visited
        base = f"projects/{project}/pipelines/{pid}"
        pipeline = await self.api(base)
        jobs = await self.all(base + "/jobs", include_retried="true")
        current_jobs = await self.all(base + "/jobs", include_retried="false")
        current_ids = {j["id"] for j in current_jobs}
        stage_order = await self.stage_order(pipeline)
        for j in jobs:
            j["retried"] = j["id"] not in current_ids
            j["project_id"] = project
            j["pipeline_id"] = pid
            j["stage_index"] = stage_order.get(j.get("stage"))
            if j.get("status") == "failed" and not j.get("failure_reason"):
                detail = await self.api(f"projects/{project}/jobs/{j['id']}")
                j["failure_reason"] = detail.get("failure_reason")
        bridges = await self.all(base + "/bridges")
        downstream, complete = [], pipeline.get("status") in TERMINAL
        for bridge in bridges:
            child = bridge.get("downstream_pipeline")
            if not child:
                continue
            downstream.append(child)
            if child.get("project_id") == project:
                collected = await self._collect(project, child["id"], seen, cache)
                jobs.extend(collected["jobs"])
                downstream.extend(collected["downstream"])
                # Historical retried bridges do not keep a current parent open.
                if not bridge.get("retried"):
                    complete = complete and collected["complete"]
        jobs = list({job_key(j): j for j in jobs}.values())
        downstream = list(
            {(d.get("project_id"), d["id"]): d for d in downstream}.values()
        )
        latest = await self.api(base)
        stable = not any(
            latest.get(k) != pipeline.get(k)
            for k in ("updated_at", "status", "finished_at")
        )
        if not stable:
            complete = False  # Retry a consistent snapshot.
        cache[pid] = dict(
            pipeline=pipeline,
            jobs=jobs,
            downstream=downstream,
            complete=complete,
            stable=stable,
        )
        return cache[pid]

    async def reason(self, project, job):
        raw = job.get("failure_reason") or "unknown"
        category = {
            "runner_system_failure": "基础设施",
            "stuck_or_timeout_failure": "超时",
            "job_execution_timeout": "超时",
            "scheduler_failure": "基础设施",
            "api_failure": "基础设施",
            "missing_dependency_failure": "脚本配置",
        }.get(raw, "未分类")
        explanation = "GitLab 原始原因：" + raw + "；具体根因未核实"
        # Never persist or reproduce arbitrary trace text (it may contain credentials).
        trace = await self.trace(project, job["id"])
        for pattern, label, message in [
            (
                r"(?i)OOMKilled|out of memory|evicted",
                "基础设施",
                "日志出现内存不足或驱逐标记",
            ),
            (r"(?i)no space left on device", "基础设施", "日志出现磁盘空间不足标记"),
            (
                r"(?i)\b(?:AssertionError|FAILED tests?/|tests? failed)\b",
                "测试失败",
                "日志出现测试失败标记",
            ),
            (
                r"(?i)command not found|syntax error",
                "脚本配置",
                "日志出现命令不存在或语法错误标记",
            ),
            (
                r"(?i)too many requests|rate limit exceeded",
                "门禁工具/限流",
                "日志出现限流标记",
            ),
        ]:
            if re.search(pattern, trace[-200000:]):
                category, explanation = (
                    label,
                    message + "；匹配规则不代表已核实最终根因",
                )
                break
        return dict(
            job_id=job["id"],
            category=category,
            raw=raw,
            explanation=explanation,
            evidence=job.get("web_url"),
            allow_failure=bool(job.get("allow_failure")),
            analysis="complete",
        )


def job_key(job):
    return f"{job['project_id']}:{job['id']}"


def execution(snapshot, previous, config_version):
    """Return a new completed execution, or None. Previously assigned jobs stay immutable."""
    if not snapshot["complete"]:
        return None
    p, all_jobs = snapshot["pipeline"], snapshot["jobs"]
    used = {job_key(j) for r in previous for j in r["jobs"]}
    jobs = [j for j in all_jobs if job_key(j) not in used]
    old_all = {k for r in previous for k in r.get("observed_job_keys", [])}
    fresh = [j for j in jobs if job_key(j) not in old_all]
    if previous and not fresh:
        return None
    if previous:
        jobs = fresh
    elif any(j.get("retried") for j in jobs):
        # Preserve all attempts as evidence; do not invent historical execution cycles.
        jobs = [j for j in jobs if not j.get("retried")]
    finished = stamp(p.get("finished_at"))
    end_times = [stamp(j.get("finished_at")) for j in jobs]
    end_times = [v for v in end_times if v is not None]
    finished = max(
        end_times + ([finished] if finished is not None else []), default=None
    )
    finish_missing = finished is None
    if finish_missing:
        finished = stamp(p.get("updated_at"))
        if finished is None:
            return None
    fingerprint = ",".join(sorted(job_key(j) for j in jobs)) or str(
        p.get("finished_at")
    )
    eid = hashlib.sha256(f"{p['id']}:{fingerprint}".encode()).hexdigest()[:24]
    if any(r["execution_id"] == eid for r in previous):
        return None
    intervals, missing = [], False
    for j in jobs:
        if j.get("status") in {"skipped", "manual", "created"} and not j.get(
            "started_at"
        ):
            continue
        a, b = stamp(j.get("started_at")), stamp(j.get("finished_at"))
        if a is None or b is None or b < a:
            missing = True
        else:
            intervals.append((a, b))
    union = []
    for a, b in sorted(intervals):
        if union and a <= union[-1][1]:
            union[-1][1] = max(b, union[-1][1])
        else:
            union.append([a, b])
    missing = missing or finish_missing
    uncertain_history = not previous and any(j.get("retried") for j in all_jobs)
    active = (
        sum(b - a for a, b in union)
        if union and not missing and not uncertain_history
        else None
    )
    queues = [number(j.get("queued_duration")) for j in jobs]
    queue_known = [q for q in queues if q is not None]
    return dict(
        execution_id=eid,
        pipeline_id=p["id"],
        sequence=len(previous) + 1,
        config_version=config_version,
        metric_version=METRIC_VERSION,
        status=p["status"],
        ref=p.get("ref"),
        matched_branch=p.get("ref"),
        source=p.get("source"),
        sha=p.get("sha"),
        tag=bool(p.get("tag")),
        web_url=p.get("web_url"),
        finished=finished,
        pipeline=p,
        jobs=jobs,
        downstream=snapshot["downstream"],
        observed_job_keys=[job_key(j) for j in all_jobs],
        historical_attempts=(
            [j for j in all_jobs if j.get("retried")] if uncertain_history else []
        ),
        completeness="历史轮次无法完整还原"
        if uncertain_history
        else "字段缺失（完成时间缺失时按最后更新时间记录）"
        if missing or not union
        else "complete",
        kind="partial_retry" if previous else "initial",
        active_seconds=active,
        elapsed_seconds=(
            union[-1][1] - union[0][0]
            if union and not missing and not uncertain_history
            else None
        ),
        queue_sum=(
            sum(queue_known) if queues and len(queue_known) == len(queues) else None
        ),
        queue_max=max(queue_known, default=None),
        pipeline_queue=number(p.get("queued_duration")),
        reasons=[],
        alerts=[],
    )


def alerts(record, history, limits):
    comparable = [
        r
        for r in history
        if number(r.get("active_seconds")) is not None
        and 0 < record["finished"] - r["finished"] <= 7 * 86400
        and all(
            r.get(k) == record.get(k)
            for k in (
                "tag",
                "matched_branch",
                "source",
                "status",
                "kind",
                "config_version",
                "metric_version",
            )
        )
    ]
    baseline = [r["active_seconds"] for r in comparable]
    days = {}
    for row in comparable:
        day = (
            datetime.fromtimestamp(row["finished"], get_timezone({"TIMEZONE": record.get("timezone")}))
            .date()
            .isoformat()
        )
        days.setdefault(day, []).append(row["active_seconds"])
    daily = [
        dict(day=day, median=statistics.median(values), samples=len(values))
        for day, values in sorted(days.items())
    ]
    median = statistics.median(baseline) if baseline else None
    result = []
    for j in record["jobs"]:
        q = number(j.get("queued_duration"))
        if q is not None and q > limits["queue_seconds"]:
            result.append(
                dict(
                    type="排队超时",
                    job_id=j["id"],
                    actual=q,
                    threshold=limits["queue_seconds"],
                )
            )
    active = record["active_seconds"]
    if active is not None and active > limits["execution_seconds"]:
        result.append(
            dict(type="执行超时", actual=active, threshold=limits["execution_seconds"])
        )
    if (
        active is not None
        and median is not None
        and len(baseline) >= limits["min_samples"]
        and active - median >= limits["slow_seconds"]
        and active >= median * (1 + limits["slow_ratio"])
    ):
        result.append(
            dict(
                type="明显变慢",
                actual=active,
                median=median,
                samples=len(baseline),
                ratio=limits["slow_ratio"],
                delta=limits["slow_seconds"],
            )
        )
    record.update(
        alerts=result,
        baseline=dict(samples=len(baseline), median=median, daily=daily),
        thresholds=dict(limits),
    )
    return record
