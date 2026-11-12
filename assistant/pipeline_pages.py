"""One immutable in-flight publication plus a coalescing local revision queue.

Only this module writes Git, and only when PIPELINE_PUBLISH_ENABLED is true.
Each push is a normal fast-forward push from an isolated, owned worktree.
"""

import asyncio
import hashlib
import json
import re
import shutil
import time
import uuid
from datetime import datetime
from urllib.parse import quote
from assistant.settings import get_timezone

from assistant.pipeline import dump, file_lock
from assistant.pipeline_gitlab import stamp
from assistant.http_errors import GitCommandError, PublicationBlocked
from assistant.pipeline_config import pages_target


def public_record(record):
    """Explicit allowlist: no raw traces, recipients, local cursors or authentication."""
    from assistant.pipeline_public import clean, RECORD

    value = dict(record)
    value.setdefault("historical_attempts", [])
    return clean(value, RECORD)


class Publisher:
    def __init__(self, monitor):
        self.m = monitor
        self.root = monitor.root
        self.target = monitor.pages_target
        self.branch = self.target.branch if self.target else "main"
        self.repo = self.root / ".project" / self.target.directory if self.target else None
        self.ref = "refs/remotes/origin/" + self.branch
        self.api_base = "projects/" + quote(self.target.project, safe="") if self.target else ""

    def finish_local_delete(self, task):
        with file_lock(self.root / ".local/pipeline-pages.lock") as locked:
            if not locked:
                return False
            task = self.m.get(task["number"])
            return self._finish_local_delete(task)

    def _finish_local_delete(self, task):
        flight = self.m.meta("flight") or {}
        if (
            task["data"].get("publication_state") != "never"
            or str(task["number"]) in flight.get("tasks", {})
            or task["data"].get("published_sha")
            or task["data"].get("write_baseline")
        ):
            return False
        task["state"], task["config"], task["data"] = (
            "deleted",
            {},
            {"sync": "已删除（从未发布）"},
        )
        with self.m.db:
            for table in (
                "pipeline_records",
                "pipeline_observations",
                "pipeline_gaps",
                "pipeline_deliveries",
            ):
                self.m.db.execute(
                    f"DELETE FROM {table} WHERE task_id=?", (task["task_id"],)
                )
            self.m.save(task)
        return True

    async def review_shared(self, text, source, sid):
        if not any(
            t["session_id"] == sid and t["state"] != "deleted" for t in self.m.tasks()
        ):
            return "请在本机监控任务话题中审查发布变更。"
        with file_lock(self.root / ".local/pipeline-pages.lock") as locked:
            if not locked:
                return "发布操作进行中，请稍后重试审查。"
            await self.prepare()
            shared = await self.git(
                "ls-tree", "-r", self.ref, "--", "scripts", ".gitlab-ci.yml"
            )
            digest = hashlib.sha256(shared.encode()).hexdigest()
            head = await self.git("rev-parse", self.ref)
            key = "shared_review:" + str(sid)
            if text.startswith("确认发布变更 "):
                review = self.m.meta(key) or {}
                delivered = self.m.db.execute(
                    "SELECT 1 FROM inbox WHERE id=? AND binding=? AND state='done'",
                    (review.get("source", ""), self.m.binding),
                ).fetchone()
                if (
                    not delivered
                    or review.get("digest") != digest
                    or text != "确认发布变更 " + digest
                ):
                    return "确认与已送达的审查版本不一致，或远端再次变化；请重新回复“审查发布变更”。"
                self.m.set_meta("shared_baseline", shared)
                self.m.set_meta("shared_ref", head)
                self.m.set_meta(key, None)
                self.m.set_meta("retry_at", 0)
                return "已接受审阅的生成器/CI 版本，恢复后续发布；任务目录冲突仍需单独处理。"
            known = self.m.meta("shared_baseline")
            if known == shared:
                return "共享生成器与 CI 未发生待确认变更。"
            base = self.m.meta("shared_ref")
            diff = (
                await self.git("diff", base, head, "--", "scripts", ".gitlab-ci.yml")
                if base
                else shared
            )
            self.m.set_meta(key, dict(source=source, digest=digest, head=head))
            url = (
                f"{self.target.web_url}/-/compare/{base}...{head}"
                if base
                else f"{self.target.web_url}/-/tree/{head}"
            )
            return (
                f"共享发布代码变更，影响所有本机任务。审阅版本：{head}\n完整变更：{url}\n"
                + "```diff\n"
                + diff[:12000]
                + "\n```\n"
                + (
                    "差异较长，完整内容请打开以上固定版本链接。\n"
                    if len(diff) > 12000
                    else ""
                )
                + f"接受这个确切版本后回复：确认发布变更 {digest}"
            )

    async def git(self, *args, cwd=None):
        try:
            return (await self.m.run(["git", *args], 90, cwd=cwd or self.repo)).strip()
        except GitCommandError:
            raise
        except asyncio.TimeoutError:
            raise GitCommandError(args[0], timed_out=True) from None
        except (OSError, RuntimeError):
            raise GitCommandError(args[0]) from None

    async def prepare(self):
        if self.target is None:
            raise ValueError("请配置 PIPELINE_PAGES_REPO_URL 后再发布报告")
        self.check_target()
        if (
            self.repo.parent.is_symlink()
            or self.repo.is_symlink()
            or not self.repo.is_dir()
        ):
            raise ValueError(
                f"请先按项目约定准备 .project/{self.target.directory} SSH 工作区，可用 templates/pipeline-pages 初始化新仓库"
            )
        actual = pages_target(
            dict(
                PIPELINE_PAGES_REPO_URL=await self.git("remote", "get-url", "origin"),
                PIPELINE_PAGES_BRANCH=self.branch,
            )
        )
        if actual.identity != self.target.identity:
            raise ValueError("报告仓库 origin 不匹配")
        await self.git("fetch", "origin", f"+refs/heads/{self.branch}:{self.ref}")

    def check_target(self):
        """Never interpret an old repository's publication ledger in a new target."""
        previous = self.m.meta("pages_target")
        has_history = bool(
            self.m.meta("flight") or self.m.meta("shared_baseline")
        ) or any(
            t["data"].get("published_sha")
            or t["data"].get("write_baseline")
            or t["data"].get("report_url")
            or t["data"].get("publication_state") == "attempted"
            for t in self.m.tasks()
        )
        if has_history and previous is None:
            raise ValueError('旧发布记录尚未绑定报告目标；请先核实原仓库并迁移绑定记录')
        expected = previous
        if expected and expected != self.target.identity and has_history:
            raise ValueError(
                "报告仓库或分支已变更，但本机仍有原目标的发布记录；请恢复原配置，迁移历史报告需单独处理"
            )
        if previous != self.target.identity:
            with self.m.db:
                self.m.db.execute("DELETE FROM pipeline_remote")
                self.m.set_meta("pages_target", self.target.identity)
                self.m.set_meta("remote_at", None)

    async def refresh(self):
        if self.target is None:
            return
        with file_lock(self.root / ".local/pipeline-pages.lock") as locked:
            if not locked:
                return
            try:
                await self.prepare()
                names = (
                    await self.git(
                        "ls-tree", "-r", "--name-only", self.ref, "--", "tasks"
                    )
                ).splitlines()
                rows = []
                for name in names:
                    if re.fullmatch(r"tasks/[a-f0-9]{32}/manifest.json", name):
                        data = json.loads(await self.git("show", f"{self.ref}:{name}"))
                        if data.get("task_id") != name.split("/")[1]:
                            raise ValueError("共享任务 ID 与路径不匹配")
                        rows.append(data)
                with self.m.db:
                    # Keep stable alias rows; mark removals rather than reuse their numbers.
                    for data in rows:
                        self.m.db.execute(
                            "INSERT INTO pipeline_remote(task_id,body) VALUES(?,?) "
                            "ON CONFLICT(task_id) DO UPDATE SET body=excluded.body",
                            (data["task_id"], dump(data)),
                        )
                    active = {r["task_id"] for r in rows}
                    for tid, body in self.m.db.execute(
                        "SELECT task_id,body FROM pipeline_remote"
                    ).fetchall():
                        if tid not in active:
                            data = json.loads(body)
                            data["state"] = "远端已移除"
                            self.m.db.execute(
                                "UPDATE pipeline_remote SET body=? WHERE task_id=?",
                                (dump(data), tid),
                            )
                self.m.set_meta("remote_at", time.strftime("%Y-%m-%d %H:%M:%S"))
                self.m.set_meta("remote_status", "已同步")
            except (OSError, ValueError, RuntimeError, asyncio.TimeoutError):
                self.m.set_meta("remote_status", "同步失败，显示缓存")
                raise

    async def tree(self, tid, ref=None):
        return await self.git("ls-tree", "-r", ref or self.ref, "--", "tasks/" + tid)

    async def check_deployment(self, flight):
        deployed_sha = flight["sha"]
        matching = False
        envs = await self.m.pages_gitlab.all(
            self.api_base + "/environments", name="pipeline-pages"
        )
        for env in envs:
            full = await self.m.pages_gitlab.api(
                self.api_base + f"/environments/{env['id']}"
            )
            deployed = full.get("last_deployment") or {}
            candidate = deployed.get("sha", "")
            if deployed.get("status") != "success" or not re.fullmatch(
                r"[a-f0-9]{40,64}", candidate
            ):
                continue
            if candidate == flight["sha"]:
                matching = True
            elif (
                await self.git("merge-base", candidate, flight["sha"]) == flight["sha"]
            ):
                # A newer external deployment may contain this entire immutable batch.
                same = True
                for number, version in flight["tasks"].items():
                    task = self.m.get(int(number))
                    if await self.tree(task["task_id"], candidate) != version["tree"]:
                        same = False
                        break
                if same:
                    deployed_sha, matching = candidate, True
        pipelines = await self.m.pages_gitlab.all(
            self.api_base + "/pipelines", sha=deployed_sha
        )
        if not pipelines:
            return False
        pipeline = max(pipelines, key=lambda p: p["id"])
        if pipeline["status"] in ("failed", "canceled", "skipped"):
            self.m.set_meta("retry_at", time.time() + 300)
            self.m.set_meta("flight", None)
            for n in flight["tasks"]:
                t = self.m.get(int(n))
                t["data"]["sync"] = "CI 失败，待重试"
                self.m.save(t)
            return False
        if pipeline["status"] != "success":
            return False
        info = await self.m.pages_gitlab.api(
            self.api_base + f"/pipelines/{pipeline['id']}"
        )
        jobs = await self.m.pages_gitlab.all(
            self.api_base + f"/pipelines/{pipeline['id']}/jobs"
        )
        pages_jobs = [
            j for j in jobs if j.get("name") == "pages" and j["status"] == "success"
        ]
        if not pages_jobs:
            return False
        pages = await self.m.pages_gitlab.api(self.api_base + "/pages")
        deployments = [
            d for d in pages.get("deployments", []) if not d.get("path_prefix")
        ]
        start = stamp(info.get("created_at"))
        live = any(
            start is not None and (stamp(d.get("created_at")) or 0) >= start
            for d in deployments
        )
        if (
            not matching
            or not live
            or not str(pages.get("url", "")).startswith("https://")
        ):
            return False
        # Check that this commit's task trees still match before marking versions deployed.
        for n, version in flight["tasks"].items():
            t = self.m.get(int(n))
            if not t:
                continue
            t["data"]["published_revision"] = version["revision"]
            t["data"]["published_data_revision"] = version.get(
                "data_revision", version["revision"]
            )
            t["data"]["published_sha"] = deployed_sha
            t["data"]["published_at"] = time.time()
            self.m.clear_issue(t, "报告发布失败", "Pages 发布等待超时")
            if version["state"] == "deleting":
                t["state"] = "deleted"
                t["config"] = {}
                t["data"] = {"sync": "已下架", "published_sha": deployed_sha}
                with self.m.db:
                    for table in (
                        "pipeline_records",
                        "pipeline_observations",
                        "pipeline_gaps",
                        "pipeline_deliveries",
                    ):
                        self.m.db.execute(
                            f"DELETE FROM {table} WHERE task_id=?", (t["task_id"],)
                        )
            else:
                t["data"]["report_url"] = (
                    pages["url"].rstrip("/") + "/tasks/" + t["task_id"] + "/"
                )
                t["data"]["sync"] = (
                    "已发布"
                    if t["data"]["revision"] == version["revision"]
                    else "待发布"
                )
                if (
                    not t["data"].get("initial")
                    and not t["data"].get("link_queued")
                    and t["data"].get("initial_revision", 10**18)
                    <= version.get("data_revision", version["revision"])
                    and t["state"] == "active"
                ):
                    self.m.queue(
                        t,
                        "initial-link",
                        dict(markdown="首次补采报告已发布：" + t["data"]["report_url"]),
                    )
                    t["data"]["link_queued"] = True
            self.m.save(t)
        self.m.set_meta("flight", None)
        self.m.set_meta("retry_at", 0)
        return True

    async def tick(self):
        for task in self.m.tasks():
            if task["state"] == "deleting":
                self.finish_local_delete(task)
        if not self.m.cfg.get("PIPELINE_PUBLISH_ENABLED", False):
            return
        with file_lock(self.root / ".local/pipeline-pages.lock") as locked:
            if not locked:
                return
            await self.prepare()
            flight = self.m.meta("flight")
            if flight:
                # Resolve an interrupted or timed-out push by reading the remote graph first.
                included = (
                    await self.git("merge-base", self.ref, flight["sha"])
                    == flight["sha"]
                )
                if not included:
                    failures = self.m.meta("push_failures", 0) + 1
                    self.m.set_meta("push_failures", failures)
                    for n in flight["tasks"]:
                        task = self.m.get(int(n))
                        task["data"]["sync"] = (
                            "推送重试已达 3 次；核查后使用 start 重试"
                            if failures >= 3
                            else "远端前进，待重做增量"
                        )
                        self.m.save(task)
                    self.m.set_meta("flight", None)
                    self.m.set_meta("retry_at", time.time() + 60)
                    return
                for n, v in flight["tasks"].items():
                    t = self.m.get(int(n))
                    t["data"]["write_baseline"] = v["tree"]
                    self.m.save(t)
                if not await self.check_deployment(flight):
                    if time.time() - flight["created"] > 1800:
                        for n in flight["tasks"]:
                            t = self.m.get(int(n))
                            t["data"]["sync"] = (
                                "发布等待超过 30 分钟，需检查 Runner/CI/部署验证"
                            )
                            self.m.save(t)
                            self.m.issue(t, "Pages 发布等待超时", RuntimeError())
                    return
            if self.m.meta("push_failures", 0) >= 3:
                return
            if time.time() < self.m.meta("retry_at", 0):
                return
            pending = [
                t
                for t in self.m.tasks()
                if t["state"] != "deleted"
                and t["data"].get("revision", 0)
                > t["data"].get("published_revision", 0)
            ]
            pending = [
                t
                for t in pending
                if not t["data"].get("cycle")
                and not self.m.db.execute(
                    "SELECT 1 FROM pipeline_deliveries WHERE task_id=? AND state IN ('pending','sending') AND result='' "
                    "AND execution_id NOT LIKE 'error:%' LIMIT 1",
                    (t["task_id"],),
                ).fetchone()
            ]
            if not pending:
                return
            # Review changes in the normal checkout must never be committed by a background worker.
            if await self.git("status", "--porcelain"):
                raise PublicationBlocked("dirty")
            if await self.git("branch", "--show-current") != self.branch:
                raise PublicationBlocked("branch")
            ahead = await self.git("rev-list", "--count", f"{self.ref}..HEAD")
            if ahead != "0":
                raise PublicationBlocked("ahead")
            await self.git("merge", "--ff-only", self.ref)
            shared = await self.git(
                "ls-tree", "-r", self.ref, "--", "scripts", ".gitlab-ci.yml"
            )
            if not shared or not (self.repo / "scripts/build.py").is_file():
                raise PublicationBlocked("missing_ci")
            known = self.m.meta("shared_baseline")
            if known is not None and known != shared:
                raise PublicationBlocked("shared")
            self.m.set_meta("shared_baseline", shared)
            self.m.set_meta("shared_ref", await self.git("rev-parse", self.ref))
            eligible = []
            for t in pending:
                tree = await self.tree(t["task_id"])
                if tree != t["data"].get("write_baseline", ""):
                    t["data"]["sync"] = "冲突：本机生成目录被外部修改"
                    t["data"]["conflict"] = {
                        "path": "tasks/" + t["task_id"],
                        "expected": t["data"].get("write_baseline", ""),
                        "actual": tree,
                    }
                    self.m.save(t)
                    self.m.issue(t, "报告目录冲突", ValueError())
                    continue
                eligible.append(t)
            if not eligible:
                return
            work = self.root / ".local" / ("pipeline-publish-" + uuid.uuid4().hex)
            await self.git("worktree", "add", "--detach", str(work), self.ref)
            try:
                versions = {}
                for t in eligible:
                    t = self.m.get(t["number"])
                    if t["state"] == "deleted":
                        continue
                    path = work / "tasks" / t["task_id"]
                    if path.exists():
                        if path.is_symlink() or any(
                            p.is_symlink() for p in path.rglob("*")
                        ):
                            raise ValueError("生成路径包含符号链接")
                        if t["state"] != "paused":
                            shutil.rmtree(path)
                    if t["state"] != "deleting":
                        path.mkdir(parents=True, exist_ok=True)
                        months = {}
                        if t["state"] == "paused" and (path / "manifest.json").exists():
                            # Retain the exact already-published data; even a late update to
                            # an existing row must wait for start. Only the manifest changes.
                            months = {
                                month: None
                                for month in json.loads(
                                    (path / "manifest.json").read_text()
                                )["months"]
                            }
                        display_timezone = get_timezone(self.m.cfg).key
                        if t["state"] == "paused" and (path / "manifest.json").exists():
                            display_timezone = json.loads(
                                (path / "manifest.json").read_text()
                            ).get("timezone", "Asia/Shanghai")
                        for r in self.m.records(t["task_id"]):
                            if t["state"] == "paused":
                                continue
                            month = datetime.fromtimestamp(
                                r["finished"], get_timezone(self.m.cfg)
                            ).strftime("%Y-%m")
                            months.setdefault(month, []).append(public_record(r))
                        for month, records in months.items():
                            if records is not None:
                                (path / (month + ".json")).write_text(
                                    dump(records) + "\n"
                                )
                        c = t["config"]
                        manifest = dict(
                            schema=1,
                            timezone=display_timezone,
                            task_id=t["task_id"],
                            manager=self.m.meta("instance"),
                            project_path=c["project_path"],
                            url=c["url"],
                            branches=c["branches"],
                            include_tags=c["include_tags"],
                            state=t["state"],
                            revision=t["data"]["revision"],
                            config_version=t["version"],
                            months=sorted(months),
                            updated=time.time(),
                        )
                        (path / "manifest.json").write_text(dump(manifest) + "\n")
                    versions[str(t["number"])] = dict(
                        revision=t["data"]["revision"],
                        state=t["state"],
                        data_revision=t["data"].get(
                            "published_data_revision",
                            t["data"].get("published_revision", 0),
                        )
                        if t["state"] == "paused"
                        else t["data"]["revision"],
                    )
                await self.git("add", "-A", "--", "tasks", cwd=work)
                if not await self.git("diff", "--cached", "--name-only", cwd=work):
                    # Failed CI on an identical tree still needs a new, traceable retry commit.
                    await self.git(
                        "commit",
                        "--allow-empty",
                        "-m",
                        "Retry pipeline report publication",
                        cwd=work,
                    )
                else:
                    await self.git(
                        "commit", "-m", "Update pipeline monitoring reports", cwd=work
                    )
                sha = await self.git("rev-parse", "HEAD", cwd=work)
                for n in versions:
                    versions[n]["tree"] = await self.tree(
                        self.m.get(int(n))["task_id"], sha
                    )
                flight = dict(sha=sha, created=time.time(), tasks=versions)
                for n in versions:
                    task = self.m.get(int(n))
                    task["data"]["publication_state"] = "attempted"
                    self.m.save(task)
                self.m.set_meta(
                    "flight", flight
                )  # persisted BEFORE a possibly ambiguous push
                await self.git(
                    "push", "origin", sha + ":refs/heads/" + self.branch, cwd=work
                )
                self.m.set_meta("push_failures", 0)
                for n, v in versions.items():
                    t = self.m.get(int(n))
                    t["data"]["write_baseline"] = v["tree"]
                    t["data"]["sync"] = "发布中"
                    self.m.save(t)
            finally:
                # Only remove the scratch worktree we created; never clean the user's checkout.
                if work.exists() and not await self.git(
                    "status", "--porcelain", cwd=work
                ):
                    await self.git("worktree", "remove", str(work))
