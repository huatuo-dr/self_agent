import asyncio
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from assistant.pipeline import (
    DEFAULTS,
    FORM,
    PipelineMonitor,
    dump,
    file_lock,
    parse_fields,
)
from assistant.pipeline_card import card
from assistant.pipeline_gitlab import GitLab, alerts, execution, iso, project_path
from assistant.pipeline_pages import Publisher, public_record
from assistant.service import database
from assistant.sessions import Sessions


def job(jid, start=1000, end=1100, **kw):
    return dict(
        id=jid,
        project_id=1,
        pipeline_id=7,
        name="test",
        stage="test",
        status="success",
        started_at=iso(start),
        finished_at=iso(end),
        duration=end - start,
        queued_duration=1,
        created_at=iso(start - 2),
        web_url=f"https://git.example/group/repo/-/jobs/{jid}",
        **kw,
    )


def snapshot(jobs=None, **kw):
    return dict(
        pipeline=dict(
            id=7,
            status="success",
            ref="main",
            source="push",
            tag=False,
            sha="a" * 40,
            finished_at=iso(1100),
            web_url="https://git.example/group/repo/-/pipelines/7",
            **kw,
        ),
        jobs=jobs if jobs is not None else [job(10)],
        downstream=[],
        complete=True,
    )


class Metrics(unittest.TestCase):
    def test_union_queue_and_unknown(self):
        r = execution(snapshot([job(10, 1000, 1100), job(11, 1050, 1150)]), [], 1)
        self.assertEqual(r["active_seconds"], 150)
        self.assertEqual(r["elapsed_seconds"], 150)
        self.assertEqual(r["queue_sum"], 2)
        j = job(12)
        j["started_at"] = None
        j["queued_duration"] = None
        r = execution(snapshot([j]), [], 1)
        self.assertIsNone(r["active_seconds"])
        self.assertIsNone(r["queue_sum"])

    def test_same_id_retry_preserves_failure_and_excludes_old_jobs(self):
        snap = snapshot()
        snap["pipeline"]["status"] = "failed"
        snap["jobs"][0]["status"] = "failed"
        first = execution(snap, [], 1)
        second = execution(
            snapshot([job(10, retried=True), job(11, 5000, 5100)]), [first], 1
        )
        self.assertEqual(second["sequence"], 2)
        self.assertEqual(second["active_seconds"], 100)
        self.assertEqual([j["id"] for j in second["jobs"]], [11])
        self.assertEqual(first["status"], "failed")
        self.assertIsNone(
            execution(
                snapshot([job(10, retried=True), job(11, 5000, 5100)]),
                [first, second],
                1,
            )
        )

    def test_historical_retry_does_not_invent_cycles(self):
        r = execution(snapshot([job(10, retried=True), job(11, 5000, 5100)]), [], 1)
        self.assertIsNone(r["active_seconds"])
        self.assertEqual(len(r["historical_attempts"]), 1)
        self.assertIn("历史", r["completeness"])

    def test_incomplete_child_does_not_notify(self):
        s = snapshot()
        s["complete"] = False
        self.assertIsNone(execution(s, [], 1))

    def test_alert_thresholds_and_comparison_scope(self):
        r = execution(snapshot([job(10, 1000, 3000)]), [], 1)
        r["jobs"][0]["queued_duration"] = 601
        history = [
            dict(r, finished=r["finished"] - i - 1, active_seconds=1000)
            for i in range(10)
        ]
        alerts(r, history, DEFAULTS["thresholds"])
        self.assertEqual(
            {a["type"] for a in r["alerts"]}, {"排队超时", "执行超时", "明显变慢"}
        )
        alerts(r, history[:9], DEFAULTS["thresholds"])
        self.assertNotIn("明显变慢", [a["type"] for a in r["alerts"]])
        alerts(r, [dict(h, config_version=2) for h in history], DEFAULTS["thresholds"])
        self.assertEqual(r["baseline"]["samples"], 0)

    def test_tags_and_metric_versions_are_separate_cohorts(self):
        r = execution(snapshot([job(10, 1000, 3000)]), [], 1)
        history = [
            dict(r, tag=False, finished=r["finished"] - i - 1, active_seconds=1000)
            for i in range(10)
        ]
        r["tag"] = True
        alerts(r, history, DEFAULTS["thresholds"])
        self.assertEqual(r["baseline"]["samples"], 0)
        self.assertNotIn("明显变慢", [a["type"] for a in r["alerts"]])
        alerts(
            r,
            [dict(h, tag=True, metric_version=2) for h in history],
            DEFAULTS["thresholds"],
        )
        self.assertEqual(r["baseline"]["samples"], 0)

    def test_http_error_metadata_is_safe_and_supports_retry_dates(self):
        from assistant.http_errors import HTTPCommandError, http_metadata

        error = HTTPCommandError(
            "glab",
            1,
            "HTTP/2.0 429 Too Many Requests\r\nRetry-After: 120\r\n\r\nDEMO_SECRET",
            "DEMO_SECRET",
        )
        self.assertEqual(error.http_status, 429)
        self.assertEqual(error.retry_after, 120)
        self.assertNotIn("DEMO_SECRET", str(error))
        self.assertNotIn("DEMO_SECRET", repr(vars(error)))
        with patch("assistant.http_errors.time.time", return_value=0):
            self.assertEqual(
                http_metadata(
                    "HTTP/1.1 429 Busy\nRetry-After: Thu, 01 Jan 1970 00:02:00 GMT"
                ),
                (429, 120),
            )

    def test_project_parser_and_fields(self):
        self.assertEqual(
            project_path("git@git.example:group/repo.git", "git.example"), "group/repo"
        )
        for url in [
            "https://evil.example/g/r",
            "https://git.example/g/r/-/pipelines/1",
            "https://x:pass@git.example/g/r",
        ]:
            with self.assertRaises(ValueError):
                project_path(url, "git.example")
        c, changed = parse_fields(
            "将监控间隔改为 5分钟\n监控分支：main, release\n是否包含 Tag Pipeline：否",
            DEFAULTS,
        )
        self.assertTrue(changed)
        self.assertEqual(c["interval_minutes"], 5)
        self.assertFalse(c["include_tags"])
        self.assertEqual(DEFAULTS["interval_minutes"], 10)
        with self.assertRaises(ValueError):
            parse_fields("监控间隔：0", DEFAULTS)

    def test_creation_template_keeps_strict_parsing_and_copyable_defaults(self):
        config, changed = parse_fields(FORM, DEFAULTS)
        self.assertTrue(changed)
        self.assertEqual(config, DEFAULTS)
        for value in ("从11.2号开始", "2026.11.02至2026.11.05"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "补采范围格式"),
            ):
                parse_fields("首次历史补采：" + value, DEFAULTS)
        config, _ = parse_fields(
            "监控分支：develop 和 release/0.6.0 两个分支", DEFAULTS
        )
        self.assertEqual(
            config["branches"], ["develop", "和", "release/0.6.0", "两个分支"]
        )
        config, _ = parse_fields("监控分支：develop,release/0.6.0", DEFAULTS)
        self.assertEqual(config["branches"], ["develop", "release/0.6.0"])

    def test_public_data_allowlist_and_card_escape(self):
        r = execution(snapshot([job(10)]), [], 1)
        r["pipeline"]["secret"] = "should-not-leak"
        r["jobs"][0]["token"] = "no"
        data = public_record(r)
        self.assertNotIn("secret", dump(data))
        self.assertNotIn("token", dump(data))
        task = dict(config=dict(DEFAULTS, project_path="<at id=all>"), data={})
        r["jobs"][0]["name"] = "<at id=all>"
        payload = card(task, r)
        self.assertEqual(payload["schema"], "2.0")
        self.assertTrue(any(e["tag"] == "chart" for e in payload["body"]["elements"]))
        self.assertNotIn("<at id=all>", dump(payload["body"]))

    def test_process_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            with file_lock(Path(directory) / "lock") as first:
                self.assertTrue(first)
                with file_lock(Path(directory) / "lock") as second:
                    self.assertFalse(second)

    def test_card_uses_forwarded_duration_chart_not_nightly_status_table(self):
        jobs = [job(1), job(2)]
        jobs[1].update(status="failed", allow_failure=True)
        record = execution(snapshot(jobs), [], 1)
        task = dict(config=dict(DEFAULTS, project_path="g/r"), data={})
        payload = card(task, record)
        body = payload["body"]["elements"]
        self.assertEqual(payload["header"]["template"], "default")
        self.assertNotIn('"tag":"table"', dump(body))
        self.assertIn("非阻塞异常", dump(body))
        charts = [e for e in body if e["tag"] == "chart"]
        self.assertEqual(len(charts), 2)  # stage overview and selected Job timeline
        spec = charts[0]["chart_spec"]
        self.assertEqual(spec["type"], "rangeColumn")
        self.assertEqual(spec["direction"], "horizontal")
        self.assertFalse(spec["stack"])
        self.assertNotIn("整体执行", spec["data"][0]["fields"]["row"]["domain"])
        self.assertTrue(spec["tooltip"]["visible"])
        self.assertTrue(
            all(not e["expanded"] for e in body if e["tag"] == "collapsible_panel")
        )

    def test_card_comparison_handles_small_zero_and_unknown_baselines(self):
        from assistant.pipeline_card import comparison

        record = dict(active_seconds=90, baseline=dict(samples=12, median=60))
        self.assertIn(
            "↑ 增加 30.0 秒 / 50.0%", comparison(record, DEFAULTS["thresholds"])
        )
        record["baseline"] = dict(samples=2, median=0)
        rendered = comparison(record, DEFAULTS["thresholds"])
        self.assertIn("基线为 0，不计算比例", rendered)
        self.assertIn("样本不足", rendered)
        record["active_seconds"] = None
        self.assertIn("暂无可比样本", comparison(record, DEFAULTS["thresholds"]))

    def test_card_large_details_preserve_summary_and_report_link(self):
        jobs = [job(i, 1000 + i, 1100 + i) for i in range(220)]
        for j in jobs:
            j["name"] = "很长的任务名称" * 20
        r = execution(snapshot(jobs), [], 1)
        task = dict(config=dict(DEFAULTS, project_path="g/r"), data={})
        payload = card(task, r, "https://report.example/tasks/a/")
        self.assertLess(len(dump(payload).encode()), 28000)
        self.assertTrue(any(e["tag"] == "chart" for e in payload["body"]["elements"]))
        self.assertIn("https://report.example/tasks/a/", dump(payload))
        self.assertIn("完整明细请查看", dump(payload))
        self.assertEqual(len(r["jobs"]), 220)


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = database(":memory:")
        self.cfg = dict(
            FEISHU_OWNER_CHAT_ID="oc_owner",
            FEISHU_OWNER_OPEN_ID="ou_owner",
            FEISHU_APP_ID="cli_app",
            GITLAB_HOST="git.example",
            GLAB_BIN="glab",
            LARK_CLI_BIN="lark",
            PIPELINE_PUBLISH_ENABLED=False,
        )
        self.run = AsyncMock(
            return_value=dump(dict(ok=True, data=dict(message_id="om_root")))
        )
        self.m = PipelineMonitor(self.cfg, self.db, self.run, self.root)
        self.m.validate = AsyncMock(
            side_effect=lambda c: dict(
                c,
                url="https://git.example/g/r",
                project_id=1,
                project_path="g/r",
                host="git.example",
            )
        )
        self.m.verify_identity = AsyncMock()
        self.sid = Sessions(self.db, "oc_owner").create("s", "监控")
        Sessions(self.db, "oc_owner").bind(self.sid, "om_root", "omt_thread")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def delivered(self, source):
        self.db.execute(
            "INSERT OR REPLACE INTO inbox(id,binding,state) VALUES(?,'oc_owner','done')",
            (source,),
        )
        self.db.commit()

    async def create(self, history="none"):
        self.db.execute(
            "INSERT INTO pipeline_drafts VALUES(?,?,?,?,?,?,0)",
            ("oc_owner", self.sid, "draft", "create", dump(DEFAULTS), None),
        )
        self.db.commit()
        await self.m.text(
            "仓库链接：https://git.example/g/r\n首次历史补采："
            + ("不补采" if history == "none" else "近 7 天"),
            "preview",
            self.sid,
        )
        self.delivered("preview")
        result = await self.m.text("确认创建", "confirm", self.sid)
        self.assertIn("已创建", result)
        return self.m.tasks()[0]

    async def test_preview_must_be_delivered_and_confirmation_idempotent(self):
        self.db.execute(
            "INSERT INTO pipeline_drafts VALUES(?,?,?,?,?,?,0)",
            ("oc_owner", self.sid, "draft", "create", dump(DEFAULTS), None),
        )
        self.db.commit()
        await self.m.text("仓库链接：https://git.example/g/r", "preview", self.sid)
        self.assertIn(
            "尚无已送达", await self.m.text("确认创建", "too-early", self.sid)
        )
        self.assertEqual(self.m.tasks(), [])
        self.delivered("preview")
        await self.m.text("确认创建", "confirm", self.sid)
        await self.m.text("确认创建", "confirm", self.sid)
        self.assertEqual(len(self.m.tasks()), 1)

    async def test_branch_validation_reports_offending_name_and_allows_field_correction(
        self,
    ):
        self.m.validate = PipelineMonitor.validate.__get__(self.m)
        self.m.gitlab.project = AsyncMock(
            return_value=dict(
                id=1,
                path_with_namespace="g/r",
                web_url="https://git.example/g/r",
            )
        )

        async def branch(endpoint):
            if endpoint.endswith("/develop") or endpoint.endswith("/release%2F0.6.0"):
                return {}
            raise RuntimeError("do not expose raw subprocess output")

        self.m.gitlab.api = AsyncMock(side_effect=branch)
        with self.db:
            self.db.execute(
                "INSERT INTO pipeline_drafts VALUES(?,?,?,?,?,?,0)",
                ("oc_owner", self.sid, "draft", "create", dump(DEFAULTS), None),
            )
        answer = await self.m.text(
            "仓库链接：https://git.example/g/r\n监控分支：develop 和 release/0.6.0 两个分支",
            "bad-branches",
            self.sid,
        )
        self.assertIn("无法读取 GitLab 分支“和”", answer)
        self.assertIn("develop,release/0.6.0", answer)
        self.assertNotIn("远端校验失败", answer)
        self.assertNotIn("raw subprocess", answer)
        self.assertEqual(
            self.db.execute("SELECT ready FROM pipeline_drafts").fetchone()[0], 0
        )
        answer = await self.m.text(
            "监控分支：develop,release/0.6.0", "correct-branches", self.sid
        )
        self.assertIn("请审阅创建配置", answer)
        self.assertEqual(
            self.db.execute("SELECT ready FROM pipeline_drafts").fetchone()[0], 1
        )
        self.assertEqual(self.m.tasks(), [])
        self.m.gitlab.api = AsyncMock(side_effect=asyncio.TimeoutError())
        with self.assertRaisesRegex(ValueError, "分支“develop”校验超时"):
            await self.m.validate(
                dict(DEFAULTS, url="https://git.example/g/r", branches=["develop"])
            )

    async def test_unrecognized_creation_field_has_explicit_feedback(self):
        with self.db:
            self.db.execute(
                "INSERT INTO pipeline_drafts VALUES(?,?,?,?,?,?,0)",
                ("oc_owner", self.sid, "draft", "create", dump(DEFAULTS), None),
            )
        answer = await self.m.text(
            "补采范围格式：2026-11-02 至 2026-11-05", "wrong-field", self.sid
        )
        self.assertIn("未识别", answer)
        self.assertIn("首次历史补采", answer)
        self.m.validate.assert_not_awaited()
        self.assertEqual(self.m.tasks(), [])

    async def test_real_cli_429_sets_shared_backoff_without_leaking_output(self):
        import sys
        from assistant.service import run_command
        from assistant.pipeline_gitlab import GitLabRateLimit

        script = self.root / "fake-glab"
        script.write_text(
            f"#!{sys.executable}\nimport sys\nprint('HTTP/2.0 429 Too Many Requests\\nRetry-After: 120\\n\\nDEMO_SECRET')\nsys.exit(1)\n"
        )
        script.chmod(0o700)
        g = GitLab(dict(self.cfg, GLAB_BIN=str(script)), run_command)
        before = time.time()
        with self.assertRaises(GitLabRateLimit) as raised:
            await g.api("projects/1/pipelines")
        self.assertGreaterEqual(raised.exception.retry_at, before + 120)
        self.assertIn("HTTP 429", str(raised.exception))
        self.assertNotIn("DEMO_SECRET", str(raised.exception))
        other_run = AsyncMock()
        other = GitLab(self.cfg, other_run)
        with self.assertRaises(GitLabRateLimit):
            await other.api("projects/2/pipelines")
        other_run.assert_not_awaited()

    async def test_gitlab_clients_share_request_pacing_and_strip_headers(self):
        from assistant import pipeline_gitlab

        starts = []

        async def run(*args):
            starts.append(time.monotonic())
            return 'HTTP/2.0 200 OK\r\nContent-Type: application/json\r\n\r\n{"id":1}'

        with patch.object(pipeline_gitlab, "REQUEST_INTERVAL", 0.03):
            first = GitLab(self.cfg, run)
            second = GitLab(self.cfg, run)
            results = await asyncio.gather(
                first.api("projects/1"),
                second.api("projects/2"),
                first.api("projects/3"),
            )
        self.assertEqual(results, [dict(id=1)] * 3)
        self.assertTrue(all(b - a >= 0.02 for a, b in zip(starts, starts[1:])))

    async def test_api_error_includes_status_and_path_but_not_raw_diagnostics(self):
        from assistant.http_errors import HTTPCommandError
        from assistant.pipeline_gitlab import GitLabAPIError

        run = AsyncMock(
            side_effect=HTTPCommandError(
                "glab", 1, "HTTP/2.0 403 Forbidden\n\nDEMO_SECRET", "DEMO_SECRET"
            )
        )
        g = GitLab(self.cfg, run)
        with self.assertRaises(GitLabAPIError) as raised:
            await g.api("projects/1/pipelines?page=1")
        self.assertIn("HTTP 403", str(raised.exception))
        self.assertIn("projects/1/pipelines", str(raised.exception))
        self.assertNotIn("DEMO_SECRET", str(raised.exception))
        self.assertNotIn("page=1", str(raised.exception))

    async def test_list_rate_limit_preserves_cursor_and_retry_after_across_restart(
        self,
    ):
        from assistant.pipeline_gitlab import GitLabRateLimit

        task = await self.create()
        error = GitLabRateLimit(
            "git.example", "projects/1/pipelines", time.time() + 600
        )
        self.m.gitlab.page = AsyncMock(side_effect=error)
        await self.m.tick()
        current = self.m.get(task["number"])
        self.assertEqual(current["data"]["cursor"], task["data"]["cursor"])
        self.assertEqual(current["data"]["cycle"]["page"], 1)
        self.assertEqual(current["next_scan"], error.retry_at)
        self.assertIn("HTTP 429", current["data"]["error"])
        self.assertEqual(current["data"]["api_error"]["status"], 429)
        await self.m.tick()
        self.assertEqual(self.m.gitlab.page.await_count, 1)
        restarted = PipelineMonitor(self.cfg, self.db, self.run, self.root)
        self.assertEqual(restarted.gitlab.not_before, error.retry_at)
        self.assertEqual(len(restarted.records(task["task_id"])), 0)

    async def test_scan_budget_yields_without_an_error_notification(self):
        task = await self.create()
        self.m.scan = AsyncMock(side_effect=asyncio.TimeoutError())
        await self.m.tick()
        current = self.m.get(task["number"])
        self.assertEqual(current["data"]["cursor"], task["data"]["cursor"])
        self.assertIn("300 秒", current["data"]["scan_progress"])
        self.assertNotIn("error", current["data"])
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM pipeline_deliveries").fetchone()[0], 0
        )

    async def test_detail_rate_limit_keeps_gap_and_does_not_advance_page(self):
        from assistant.pipeline_gitlab import GitLabRateLimit

        task = await self.create()
        error = GitLabRateLimit(
            "git.example", "projects/1/pipelines/7/jobs", time.time() + 600
        )
        self.m.gitlab.page = AsyncMock(return_value=[dict(id=7)])
        self.m.gitlab.collect = AsyncMock(side_effect=error)
        await self.m.tick()
        current = self.m.get(task["number"])
        self.assertEqual(current["data"]["cursor"], task["data"]["cursor"])
        self.assertEqual(current["data"]["cycle"]["page"], 1)
        self.assertEqual(current["next_scan"], error.retry_at)
        gap = json.loads(
            self.db.execute(
                "SELECT body FROM pipeline_gaps WHERE task_id=? AND pipeline_id=7",
                (task["task_id"],),
            ).fetchone()[0]
        )
        self.assertEqual(gap["retry_at"], error.retry_at)
        self.assertIn("HTTP 429", gap["error"])

    async def test_create_always_new_topic(self):
        self.run.return_value = dump(dict(ok=True, data=dict(message_id="om_new_root")))
        reply, sid = await self.m.command(
            "creat", "command-in-existing-topic", self.sid
        )
        self.assertNotEqual(sid, self.sid)
        self.assertIn("仓库链接", reply)
        self.assertIn("+messages-send", self.run.call_args.args[0])

    async def test_stop_start_and_delete_protect_history(self):
        task = await self.create()
        n = task["number"]
        task["data"]["cycle"] = dict(start=0, end=1, page=2)
        self.m.save(task)
        self.m.queue(task, "e", dict(markdown="result"))
        await self.m.command(f"stop {n}", "stop", self.sid)
        self.assertEqual(self.m.get(n)["state"], "paused")
        self.assertNotIn("cycle", self.m.get(n)["data"])
        self.assertEqual(
            self.db.execute("SELECT state FROM pipeline_deliveries").fetchone()[0],
            "suppressed",
        )
        await self.m.command(f"start {n}", "start", self.sid)
        self.assertEqual(self.m.get(n)["state"], "active")
        await self.m.command(f"delete {n}", "delete", self.sid)
        self.delivered("delete")
        await self.m.text("确认删除", "confirm-delete", self.sid)
        self.assertEqual(self.m.get(n)["state"], "deleted")
        self.assertIn(
            "不能重新启动",
            (await self.m.command(f"start {n}", "start-again", self.sid))[0],
        )

    async def test_uncertain_delivery_not_repeated_after_restart(self):
        task = await self.create()
        self.m.queue(task, "e", dict(markdown="result"))
        self.run.side_effect = asyncio.TimeoutError
        await self.m.deliver()
        self.assertEqual(
            self.db.execute(
                "SELECT state FROM pipeline_deliveries WHERE execution_id='e'"
            ).fetchone()[0],
            "uncertain",
        )
        self.run.reset_mock()
        self.m.recover()
        await self.m.deliver()
        # An owner diagnostic may be sent, but the ambiguous original never is.
        self.assertFalse(
            any("result" in call.args[0] for call in self.run.call_args_list)
        )

    async def test_per_recipient_success_never_resent(self):
        task = await self.create()
        task["config"]["recipients"] = ["oc_a", "oc_b"]
        self.m.save(task)
        self.m.queue(task, "e", dict(markdown="result"))
        self.run.side_effect = [
            dump(dict(ok=True, data=dict(message_id="om_sent"))),
            asyncio.TimeoutError,
        ]
        await self.m.deliver()
        states = dict(
            self.db.execute(
                "SELECT recipient,state FROM pipeline_deliveries WHERE execution_id='e'"
            )
        )
        self.assertEqual(states, {"oc_a": "sent", "oc_b": "uncertain"})

    async def test_collection_duplicates_and_backfill_silence(self):
        task = await self.create(history="7d")
        now = time.time()
        j = job(10, now - 200, now - 100)
        snap = snapshot([j])
        snap["pipeline"]["finished_at"] = iso(now - 100)
        self.m.gitlab.page = AsyncMock(return_value=[dict(id=7)])
        self.m.gitlab.collect = AsyncMock(return_value=snap)
        self.m.gitlab.scope = AsyncMock(return_value="main")
        await self.m.scan(task, now)
        self.assertEqual(len(self.m.records(task["task_id"])), 1)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM pipeline_deliveries").fetchone()[0], 0
        )
        await self.m.scan(self.m.get(task["number"]), now + 600)
        self.assertEqual(len(self.m.records(task["task_id"])), 1)

    async def test_live_execution_sends_once(self):
        task = await self.create()
        now = time.time()
        snap = snapshot([job(10, now, now + 1)])
        snap["pipeline"]["finished_at"] = iso(now + 1)
        self.m.gitlab.page = AsyncMock(return_value=[dict(id=7)])
        self.m.gitlab.collect = AsyncMock(return_value=snap)
        self.m.gitlab.scope = AsyncMock(return_value="main")
        await self.m.scan(task, now + 10)
        await self.m.scan(self.m.get(task["number"]), now + 600)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM pipeline_deliveries").fetchone()[0], 1
        )

    async def test_stopped_during_await_does_not_resurrect(self):
        task = await self.create()
        now = time.time()

        async def collect(*args):
            await self.m.command(
                "stop " + str(task["number"]), "stop-mid-scan", self.sid
            )
            return snapshot([job(10, now, now + 1)])

        self.m.gitlab.page = AsyncMock(return_value=[dict(id=7)])
        self.m.gitlab.collect = collect
        self.m.gitlab.scope = AsyncMock(return_value="main")
        await self.m.scan(task, now + 10)
        self.assertEqual(self.m.get(task["number"])["state"], "paused")
        self.assertEqual(self.m.records(task["task_id"]), [])

    async def test_publication_disabled_never_calls_git(self):
        await self.create()
        self.run.reset_mock()
        await Publisher(self.m).tick()
        self.run.assert_not_awaited()

    async def test_failed_page_keeps_watermark(self):
        task = await self.create()
        cursor = task["data"]["cursor"]
        self.m.gitlab.page = AsyncMock(side_effect=RuntimeError())
        with self.assertRaises(RuntimeError):
            await self.m.scan(task, time.time())
        self.assertEqual(self.m.get(task["number"])["data"]["cursor"], cursor)

    async def test_mr_target_and_tag_scope(self):
        g = GitLab(self.cfg, self.run)
        g.api = AsyncMock(
            return_value=dict(project_id=1, iid=1, target_branch="release")
        )
        g.all = AsyncMock(return_value=[dict(id=7, project_id=1)])
        config = dict(project_id=1, branches=["release"], include_tags=True)
        self.assertEqual(
            await g.scope(
                config,
                dict(
                    id=7, ref="refs/merge-requests/1/head", source="merge_request_event"
                ),
            ),
            "release",
        )
        g.api.assert_awaited_once_with("projects/1/merge_requests/1")
        g.all.assert_awaited_once_with("projects/1/merge_requests/1/pipelines")
        self.assertEqual(
            await g.scope(config, dict(id=8, ref="v1", tag=True, source="push")), "v1"
        )
        config["include_tags"] = False
        self.assertIsNone(await g.scope(config, dict(id=8, ref="v1", tag=True)))

    async def test_mr_commit_candidates_require_exact_pipeline_membership(self):
        g = GitLab(self.cfg, self.run)
        candidates = [
            dict(project_id=2, iid=9, target_branch="wrong"),
            dict(project_id=3, iid=10, target_branch="release"),
        ]
        g.all = AsyncMock(
            side_effect=[
                candidates,
                [dict(id=8, project_id=1)],
                [dict(id=7, project_id=1)],
            ]
        )
        pipeline = dict(id=7, ref="feature", sha="a" * 40, source="merge_request_event")
        self.assertEqual(
            await g.scope(
                dict(project_id=1, branches=["release"], include_tags=True), pipeline
            ),
            "release",
        )
        self.assertEqual(
            g.all.await_args_list[0].args[0],
            "projects/1/repository/commits/" + "a" * 40 + "/merge_requests",
        )
        g.all = AsyncMock(
            side_effect=[
                candidates,
                [dict(id=7, project_id=1)],
                [dict(id=7, project_id=1)],
            ]
        )
        with self.assertRaisesRegex(ValueError, "不能唯一"):
            await g.mr_target(1, pipeline)

    async def test_mr_ref_never_accepts_an_unrelated_pipeline_or_hides_api_failure(
        self,
    ):
        from assistant.pipeline_gitlab import GitLabRateLimit

        g = GitLab(self.cfg, self.run)
        g.api = AsyncMock(
            return_value=dict(project_id=1, iid=2, target_branch="release")
        )
        g.all = AsyncMock(return_value=[dict(id=999, project_id=1)])
        pipeline = dict(id=7, ref="refs/merge-requests/2/merge")
        with self.assertRaisesRegex(ValueError, "不能唯一"):
            await g.mr_target(1, pipeline)
        g.api.side_effect = GitLabRateLimit(
            "git.example", "projects/1/merge_requests/2", time.time() + 60
        )
        g.all.reset_mock()
        with self.assertRaises(GitLabRateLimit):
            await g.mr_target(1, pipeline)
        g.all.assert_not_awaited()

    async def test_worker_queue_does_not_use_pipeline_execution_budget(self):
        task = await self.create(history="7d")
        now = time.time()
        self.m.gitlab.page = AsyncMock(return_value=[dict(id=i) for i in range(7, 15)])
        self.m.gitlab.scope = AsyncMock(return_value="main")

        async def collect(project, pid):
            await asyncio.sleep(0.08)
            snap = snapshot([job(pid * 10, now - 100, now - 10)])
            snap["pipeline"]["id"] = pid
            return snap

        self.m.gitlab.collect = AsyncMock(side_effect=collect)
        with patch("assistant.pipeline.PIPELINE_WORK_SECONDS", 0.13):
            await self.m.scan(task, now)
        self.assertEqual(len(self.m.records(task["task_id"])), 8)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM pipeline_gaps").fetchone()[0], 0
        )

    async def test_interrupted_page_resumes_without_recollecting_completed_ids(self):
        task = await self.create(history="7d")
        now = time.time()
        batch = [dict(id=i) for i in range(7, 15)]
        self.m.gitlab.page = AsyncMock(return_value=batch)
        self.m.gitlab.scope = AsyncMock(return_value="main")
        blocked = asyncio.Event()
        release = asyncio.Event()

        async def collect(project, pid):
            if pid >= 11:
                blocked.set()
                await release.wait()
            snap = snapshot([job(pid * 10, now - 100, now - 10)])
            snap["pipeline"]["id"] = pid
            return snap

        self.m.gitlab.collect = AsyncMock(side_effect=collect)
        scan = asyncio.create_task(self.m.scan(task, now))
        await asyncio.wait_for(blocked.wait(), 2)
        scan.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await scan
        current = self.m.get(task["number"])
        self.assertEqual(current["data"]["cycle"]["processed"], [7, 8, 9, 10])
        self.assertEqual(current["data"]["cursor"], task["data"]["cursor"])
        self.m.gitlab.collect.reset_mock()
        release.set()
        await self.m.scan(current, now + 1)
        self.assertEqual(
            {c.args[1] for c in self.m.gitlab.collect.await_args_list}, {11, 12, 13, 14}
        )
        self.assertEqual(len(self.m.records(task["task_id"])), 8)
        self.assertNotIn("cycle", self.m.get(task["number"])["data"])

    async def test_git_publication_error_preserves_safe_step_and_reason(self):
        from assistant.http_errors import GitCommandError

        task = await self.create()
        error = GitCommandError(
            "push", 1, "DEMO_SECRET [rejected] main (non-fast-forward)"
        )
        self.m.issue(task, "报告发布失败", error)
        message = self.m.get(task["number"])["data"]["error"]
        self.assertIn("Git push", message)
        self.assertIn("远端分支已前进", message)
        self.assertNotIn("DEMO_SECRET", message)
        self.m.run = AsyncMock(side_effect=asyncio.TimeoutError())
        with self.assertRaisesRegex(GitCommandError, "Git fetch：执行超时"):
            await Publisher(self.m).git("fetch", "origin", "main")

    async def test_publication_block_reason_is_visible_but_raw_errors_stay_private(self):
        from assistant.http_errors import PublicationBlocked

        task = await self.create()
        for code, expected in [
            ("dirty", "未审阅改动"), ("branch", "PIPELINE_PAGES_BRANCH"),
            ("ahead", "未知本地提交"), ("missing_ci", "Pages CI"),
            ("shared", "审查发布变更"),
        ]:
            self.m.issue(task, "报告发布失败", PublicationBlocked(code))
            self.assertIn(expected, self.m.get(task["number"])["data"]["error"])
        for error in [ValueError("DEMO_SECRET"), PublicationBlocked("DEMO_SECRET")]:
            self.m.issue(task, "报告发布失败", error)
            self.assertNotIn("DEMO_SECRET", self.m.get(task["number"])["data"]["error"])

    async def test_successful_scan_clears_only_collection_errors_and_legacy_metadata(self):
        task = await self.create()
        self.m.gitlab.page = AsyncMock(return_value=[])
        collection = {"采集失败", "分页数据持续变化，保留水位重试"}
        for area in ["报告发布失败", "Pages 发布等待超时", "飞书投递结果不确定", *sorted(collection)]:
            with self.subTest(area=area):
                current = self.m.get(task["number"])
                message = area + "：RuntimeError"
                metadata = dict(status=503, endpoint="projects/1/pipelines")
                current["data"].update(error=message, api_error=metadata)
                self.m.save(current)
                await self.m.scan(current, time.time() + 1)
                data = self.m.get(task["number"])["data"]
                if area in collection:
                    self.assertNotIn("error", data)
                    self.assertNotIn("api_error", data)
                else:
                    self.assertEqual(data["error"], message)
                    self.assertEqual(data["api_error"], metadata)

    async def test_scan_recovery_respects_api_error_origin(self):
        from assistant.pipeline_gitlab import GitLabAPIError

        task = await self.create()
        self.m.gitlab.page = AsyncMock(return_value=[])
        cases = [(origin, latest, legacy)
                 for origin, latest in [("采集失败", "报告发布失败"), ("报告发布失败", "采集失败")]
                 for legacy in (False, True)]
        for origin, latest, legacy in cases:
            with self.subTest(origin=origin, legacy=legacy):
                self.m.issue(task, origin, GitLabAPIError("projects/1/pipelines", 503))
                if legacy:
                    current = self.m.get(task["number"])
                    current["data"]["api_error"].pop("area", None)
                    self.m.save(current)
                self.m.issue(task, latest, RuntimeError("DEMO_SECRET"))
                await self.m.scan(self.m.get(task["number"]), time.time() + 1)
                data = self.m.get(task["number"])["data"]
                if origin == "采集失败":
                    self.assertTrue(data["error"].startswith(latest + "："))
                    self.assertNotIn("api_error", data)
                else:
                    self.assertNotIn("error", data)
                    self.assertEqual(data["api_error"]["area"], origin)

    async def test_repeated_api_error_refreshes_metadata_without_duplicate_notice(self):
        from assistant.pipeline_gitlab import GitLabRateLimit

        task = await self.create()
        retry_at = int(time.time()) + 600
        for offset in (0.1, 0.2):
            self.m.issue(task, "采集失败", GitLabRateLimit(
                "git.example", "projects/1/pipelines", retry_at + offset))
        current = self.m.get(task["number"])
        self.assertEqual(current["data"]["api_error"]["retry_at"], retry_at + 0.2)
        count = self.db.execute(
            "SELECT count(*) FROM pipeline_deliveries WHERE execution_id LIKE 'error:%'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    async def test_changed_pagination_retains_old_watermark(self):
        task = await self.create()
        old = task["data"]["cursor"]
        self.m.gitlab.page = AsyncMock(side_effect=[[], [{"id": 99}]])
        await self.m.scan(task, time.time() + 1)
        current = self.m.get(task["number"])
        self.assertEqual(current["data"]["cursor"], old)
        self.assertEqual(current["data"]["cycle"]["page"], 1)
        self.assertEqual(current["data"]["cycle"]["restarts"], 1)

    async def test_outbox_failure_rolls_back_execution_and_revision(self):
        task = await self.create()
        now = time.time()
        snap = snapshot([job(10, now, now + 1)])
        snap["pipeline"]["finished_at"] = iso(now + 1)
        self.m.gitlab.page = AsyncMock(return_value=[{"id": 7}])
        self.m.gitlab.collect = AsyncMock(return_value=snap)
        self.m.gitlab.scope = AsyncMock(return_value="main")
        with patch.object(self.m, "queue", side_effect=RuntimeError("injected crash")):
            await self.m.scan(task, now + 10)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM pipeline_gaps").fetchone()[0], 1
        )
        self.assertEqual(self.m.records(task["task_id"]), [])
        self.assertEqual(self.m.get(task["number"])["data"]["revision"], 1)

    async def test_stop_before_task_lock_never_restores_active_state(self):
        stale = await self.create()
        await self.m.command(
            "stop " + str(stale["number"]), "stop-before-scan", self.sid
        )
        self.m.gitlab.page = AsyncMock()
        await self.m.scan(stale, time.time())
        self.m.gitlab.page.assert_not_awaited()
        self.assertEqual(self.m.get(stale["number"])["state"], "paused")

    async def test_removed_recipient_suppresses_pending_message(self):
        task = await self.create()
        task["config"]["recipients"] = ["oc_removed"]
        self.m.save(task)
        self.m.queue(task, "result", {"markdown": "pending"})
        task["config"]["recipients"] = []
        self.m.save(task)
        self.run.reset_mock()
        await self.m.deliver()
        self.run.assert_not_awaited()
        self.assertEqual(
            self.db.execute("SELECT state FROM pipeline_deliveries").fetchone()[0],
            "suppressed",
        )

    async def test_retry_identification_uses_two_job_lists(self):
        g = GitLab(self.cfg, self.run)
        g.api = AsyncMock(return_value=snapshot()["pipeline"])
        g.all = AsyncMock(side_effect=[[job(10), job(11)], [job(11)], []])
        result = await g.collect(1, 7)
        self.assertTrue(result["jobs"][0]["retried"])
        self.assertFalse(result["jobs"][1]["retried"])

    async def test_wrong_action_and_casual_confirmation_never_delete(self):
        task = await self.create()
        await self.m.command("delete", "delete-preview", self.sid)
        self.delivered("delete-preview")
        for i, text in enumerate(
            (
                "确认创建",
                "确认修改",
                "是",
                "同意",
                "确认",
                "好的",
                "可以",
                "确认独立创建",
            )
        ):
            self.assertIn("不匹配", await self.m.text(text, f"wrong-{i}", self.sid))
            self.assertEqual(self.m.get(task["number"])["state"], "active")
        await self.m.text("确认删除", "right-action", self.sid)
        self.assertEqual(self.m.get(task["number"])["state"], "deleted")

    async def test_duplicate_creation_requires_delivered_duplicate_preview(self):
        await self.create()
        sid = self.m.sessions.create("second", "第二个")
        with self.db:
            self.db.execute(
                "INSERT INTO pipeline_drafts VALUES(?,?,?,?,?,?,1)",
                (
                    self.m.binding,
                    sid,
                    "second-preview",
                    "create",
                    dump(self.m.tasks()[0]["config"]),
                    None,
                ),
            )
        self.delivered("second-preview")
        self.assertIn("不匹配", await self.m.text("确认独立创建", "bypass", sid))
        self.assertIn(
            "已有相同范围", await self.m.text("确认创建", "duplicate-preview", sid)
        )
        self.assertIn("尚无已送达", await self.m.text("确认独立创建", "early", sid))
        self.delivered("duplicate-preview")
        self.assertIn("已创建", await self.m.text("确认独立创建", "independent", sid))
        self.assertEqual(len(self.m.tasks()), 2)

    async def test_bad_pipeline_isolated_and_retried_after_cursor_advances(self):
        task = await self.create(history="7d")
        now = time.time()
        self.m.gitlab.page = AsyncMock(return_value=[dict(id=7), dict(id=8)])
        self.m.gitlab.scope = AsyncMock(return_value="main")

        def collect(project, pid):
            if pid == 7:
                raise RuntimeError("unreadable")
            snap = snapshot([job(80, now - 100, now - 10)])
            snap["pipeline"]["id"] = pid
            return snap

        self.m.gitlab.collect = AsyncMock(side_effect=collect)
        await self.m.scan(task, now)
        self.assertEqual(self.m.get(task["number"])["data"]["cursor"], now)
        self.assertEqual(len(self.m.records(task["task_id"])), 1)
        self.assertEqual(
            self.db.execute("SELECT pipeline_id FROM pipeline_gaps").fetchall(), [(7,)]
        )
        self.m.gitlab.page.return_value = []
        self.m.gitlab.collect = AsyncMock(
            return_value=snapshot([job(10, now - 90, now - 20)])
        )
        await self.m.scan(self.m.get(task["number"]), now + 600)
        self.assertEqual(len(self.m.records(task["task_id"])), 2)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM pipeline_gaps").fetchone()[0], 0
        )

    async def test_pause_during_gap_retry_keeps_gap_for_resume(self):
        task = await self.create(history="7d")
        now = time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO pipeline_gaps VALUES(?,?,?)",
                (task["task_id"], 7, dump(dict(error="RuntimeError", retry_at=0))),
            )
        self.m.gitlab.page = AsyncMock(return_value=[])

        async def collect(*args):
            await self.m.command("stop", "pause-retry", self.sid)
            return snapshot([job(10, now - 100, now - 10)])

        self.m.gitlab.collect = collect
        self.m.gitlab.scope = AsyncMock(return_value="main")
        await self.m.scan(task, now)
        self.assertEqual(self.m.get(task["number"])["state"], "paused")
        self.assertEqual(
            self.db.execute("SELECT pipeline_id FROM pipeline_gaps").fetchall(), [(7,)]
        )
        await self.m.command("start", "resume-retry", self.sid)
        self.m.gitlab.collect = AsyncMock(
            return_value=snapshot([job(10, now - 100, now - 10)])
        )
        await self.m.scan(self.m.get(task["number"]), now + 600)
        self.assertEqual(len(self.m.records(task["task_id"])), 1)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM pipeline_gaps").fetchone()[0], 0
        )

    async def test_child_timeout_then_completion_updates_report_without_second_card(
        self,
    ):
        from copy import deepcopy

        task = await self.create()
        now = time.time() + 10
        snap = snapshot([job(10, now - 5, now)])
        snap["pipeline"]["finished_at"] = iso(now)
        child = dict(
            job(20, now, now + 10), pipeline_id=8, status="running", finished_at=None
        )
        snap["jobs"].append(child)
        snap["downstream"] = [
            dict(
                id=8,
                project_id=1,
                status="running",
                web_url="https://git.example/g/r/-/pipelines/8",
            )
        ]
        snap["complete"] = False
        self.m.gitlab.page = AsyncMock(return_value=[dict(id=7)])
        self.m.gitlab.collect = AsyncMock(side_effect=lambda *a: deepcopy(snap))
        self.m.gitlab.scope = AsyncMock(return_value="main")
        await self.m.scan(task, now)
        self.assertEqual(self.m.records(task["task_id"]), [])
        await self.m.scan(self.m.get(task["number"]), now + 600)
        first = self.m.records(task["task_id"])[0]
        self.assertTrue(first["awaiting_children"])
        self.assertIsNone(first["active_seconds"])
        self.assertIn("下游", dump(card(task, first)))
        self.assertIn("pipelines/8", dump(card(task, first)))
        snap["complete"] = True
        child.update(status="success", finished_at=iso(now + 700), duration=700)
        snap["downstream"][0]["status"] = "success"
        # A newly visible child Job is completion evidence, not a parent retry.
        snap["jobs"].append(dict(job(21, now + 700, now + 710), pipeline_id=8))
        await self.m.scan(self.m.get(task["number"]), now + 1200)
        records = self.m.records(task["task_id"])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["execution_id"], first["execution_id"])
        self.assertFalse(records[0]["awaiting_children"])
        self.assertIsNotNone(records[0]["active_seconds"])
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM pipeline_deliveries").fetchone()[0], 1
        )
        await self.m.scan(self.m.get(task["number"]), now + 1800)
        self.assertEqual(len(self.m.records(task["task_id"])), 1)

    async def test_delete_disabled_publish_does_not_forget_ambiguous_push(self):
        task = await self.create()
        task["data"]["publication_state"] = "attempted"
        task["data"]["cycle"] = dict(start=0, end=1, page=2)
        self.m.save(task)
        await self.m.command("delete", "delete-preview", self.sid)
        self.delivered("delete-preview")
        await self.m.text("确认删除", "delete-confirm", self.sid)
        await Publisher(self.m).tick()
        self.assertEqual(self.m.get(task["number"])["state"], "deleting")
        self.assertNotIn("cycle", self.m.get(task["number"])["data"])

    async def test_verified_chat_mode_and_salutation(self):
        self.m.gitlab.project = AsyncMock(
            return_value=dict(
                id=1, path_with_namespace="g/r", web_url="https://git.example/g/r"
            )
        )
        for mode, name, expected in (
            ("p2p", "花名（真实姓名）", "花名"),
            ("group", "研发群", "各位"),
            ("topic", "项目话题", "各位"),
        ):
            self.run.return_value = dump(
                dict(ok=True, data=dict(name=name, chat_mode=mode))
            )
            config = await PipelineMonitor.validate(
                self.m,
                dict(DEFAULTS, url="https://git.example/g/r", recipients=["oc_a"]),
            )
            self.assertEqual(self.m.recipient_name(config, "oc_a"), expected)
        config["recipient_modes"]["oc_a"] = "p2p"
        config["recipient_salutations"] = {"oc_a": "华哥"}
        self.assertEqual(self.m.recipient_name(config, "oc_a"), "华哥")

    async def test_pagination_budget_and_deadline(self):
        g = GitLab(self.cfg, self.run)
        g.page = AsyncMock(return_value=[dict(id=i) for i in range(100)])
        with patch("assistant.pipeline_gitlab.MAX_PAGES", 2):
            with self.assertRaises(ValueError):
                await g.all("endpoint")
        self.assertEqual(g.page.await_count, 2)

        async def slow(*a, **kw):
            await asyncio.sleep(1)

        g.page = slow
        with patch("assistant.pipeline_gitlab.API_BUDGET_SECONDS", 0.01):
            with self.assertRaises(asyncio.TimeoutError):
                await g.all("endpoint")

    async def test_trace_is_bounded_before_loading_entire_stdout(self):
        import sys

        script = self.root / "fake-glab"
        script.write_text(
            f"#!{sys.executable}\nimport sys\nsys.stdout.buffer.write(b'x' * (3 * 1024 * 1024))\n"
        )
        script.chmod(0o700)
        g = GitLab(dict(self.cfg, GLAB_BIN=str(script)), self.run)
        with self.assertRaisesRegex(ValueError, "读取上限"):
            await g.trace(1, 7)
        script.write_text(f"#!{sys.executable}\nprint('no space left on device')\n")
        reason = await g.reason(
            1, dict(job(10), status="failed", failure_reason="script_failure")
        )
        self.assertEqual(reason["category"], "基础设施")
        self.assertNotIn("no space left on device", dump(reason))

    async def test_shared_child_reference_is_not_cycle_but_real_cycle_is(self):
        g = GitLab(self.cfg, self.run)

        async def api(endpoint, **kw):
            return dict(snapshot()["pipeline"], id=int(endpoint.split("/")[-1]))

        graph = {7: [8, 9], 8: [10], 9: [10], 10: []}

        async def all_rows(endpoint, **kw):
            pid = int(endpoint.split("/")[-2])
            if endpoint.endswith("/bridges"):
                return [
                    dict(downstream_pipeline=dict(id=k, project_id=1))
                    for k in graph[pid]
                ]
            return [dict(job(pid * 10), pipeline_id=pid)]

        g.api, g.all = api, all_rows
        snap = await g.collect(1, 7)
        self.assertTrue(snap["complete"])
        self.assertEqual(len(snap["jobs"]), 4)
        graph[10] = [7]
        with self.assertRaises(ValueError):
            await g.collect(1, 7)


class PagesContract(unittest.TestCase):
    def test_publisher_output_builds_without_losing_public_fields(self):
        from assistant.pipeline_public import MANIFEST, RECORD

        report_root = Path(
            os.environ.get(
                "PIPELINE_PAGES_TEST_ROOT",
                Path(__file__).resolve().parents[1] / "templates/pipeline-pages",
            )
        ).resolve()
        spec = importlib.util.spec_from_file_location(
            "pipeline_pages_contract_build", report_root / "scripts/build.py"
        )
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        # Compare the data contract, not implementation text or file layout.
        self.assertEqual(RECORD, builder._schema.RECORD)
        self.assertEqual(MANIFEST, builder._schema.MANIFEST)
        record = public_record(execution(snapshot(), [], 1))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "scripts/dashboard.html").write_bytes(
                (report_root / "scripts/dashboard.html").read_bytes()
            )
            for asset in ("analytics.js", "timeline.js", "dashboard.js"):
                (root / "scripts" / asset).write_bytes(
                    (report_root / "scripts" / asset).read_bytes()
                )
            task_id = "a" * 32
            task = root / "tasks" / task_id
            task.mkdir(parents=True)
            manifest = dict(
                schema=1,
                task_id=task_id,
                manager="contract-test",
                project_path="g/r",
                url="https://git.example/g/r",
                branches=["main"],
                include_tags=True,
                state="active",
                revision=1,
                config_version=1,
                months=["2026-11"],
                updated=1,
            )
            (task / "manifest.json").write_text(dump(manifest))
            (task / "2026-11.json").write_text(dump([record]))
            builder.build(root, root / "public")
            result = root / "public/tasks" / task_id
            self.assertEqual(
                json.loads((result / "2026-11.json").read_text()), [record]
            )
            self.assertEqual(
                json.loads((result / "manifest.json").read_text()), manifest
            )
            self.assertTrue((result / "index.html").is_file())


class PublisherIntegration(unittest.IsolatedAsyncioTestCase):
    """Git writes here are confined to disposable test repositories, never real remotes."""

    def setUp(self):
        import subprocess

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.remote = self.root / "remote.git"
        self.repo = self.root / ".project/pipeline-pages"
        self.repo.parent.mkdir(parents=True)

        def git(*args, cwd=None):
            return subprocess.check_output(
                ["git", *args], cwd=cwd, stderr=subprocess.DEVNULL, text=True
            ).strip()

        self.git = git
        git("init", "--bare", "--initial-branch=main", str(self.remote))
        git("clone", str(self.remote), str(self.repo))
        git("config", "user.email", "fixture@example.invalid", cwd=self.repo)
        git("config", "user.name", "Fixture", cwd=self.repo)
        # The publisher needs a versioned shared tree, not the real site builder.
        # These placeholders are never executed; deployment APIs are mocked below.
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts/build.py").write_text("# Git publication test fixture\n")
        (self.repo / ".gitlab-ci.yml").write_text("# Git publication test fixture\n")
        git("add", ".", cwd=self.repo)
        git("commit", "-m", "Fixture base", cwd=self.repo)
        git("push", "origin", "main", cwd=self.repo)
        self.db = database(":memory:")
        self.cfg = dict(
            FEISHU_OWNER_CHAT_ID="oc_owner",
            GITLAB_HOST="git.example",
            GLAB_BIN="glab",
            LARK_CLI_BIN="lark",
            PIPELINE_PUBLISH_ENABLED=True,
            PIPELINE_PAGES_REPO_URL="git@gitlab.example.com:reports/pipeline-pages.git",
        )

        async def run(args, timeout, cwd=None):
            # Transport is a disposable local bare repo; expose its configured
            # SSH identity to the production origin validation.
            if args == ["git", "remote", "get-url", "origin"]:
                return self.m.pages_target.origin
            completed = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
            if completed.returncode:
                raise RuntimeError("Fixture Git command failed")
            return completed.stdout

        self.run = run
        self.m = PipelineMonitor(self.cfg, self.db, run, self.root)
        sid = Sessions(self.db, "oc_owner").create("s", "监控")
        Sessions(self.db, "oc_owner").bind(sid, "om_root")
        self.db.commit()
        c = dict(
            DEFAULTS,
            url="https://git.example/g/r",
            project_id=1,
            project_path="g/r",
            host="git.example",
        )
        with self.db:
            self.db.execute(
                "INSERT INTO pipeline_tasks(task_id,binding,session_id,state,config,created,next_scan,data) VALUES(?,?,?,?,?,?,?,?)",
                (
                    "a" * 32,
                    "oc_owner",
                    sid,
                    "active",
                    dump(c),
                    time.time(),
                    time.time(),
                    dump(
                        dict(
                            revision=1,
                            sync="待发布",
                            cursor=0,
                            initial=False,
                            initial_revision=1,
                        )
                    ),
                ),
            )
        self.n = self.m.tasks()[0]["number"]
        self.status = "running"

        async def all_api(path, **params):
            if path.endswith("/pipelines"):
                return [dict(id=99, status=self.status)]
            if path.endswith("/jobs"):
                return [dict(id=1, name="pages", status="success")]
            if path.endswith("/environments"):
                return [dict(id=1)]
            raise AssertionError(path)

        async def api(path):
            if path.endswith("/pipelines/99"):
                return dict(created_at=iso(time.time() - 100))
            if path.endswith("/pages"):
                return dict(
                    url="https://pages.example/report",
                    deployments=[dict(path_prefix="", created_at=iso(time.time()))],
                )
            if path.endswith("/environments/1"):
                return dict(
                    last_deployment=dict(
                        sha=self.m.meta("flight")["sha"], status="success"
                    )
                )
            raise AssertionError(path)

        self.m.pages_gitlab = self.m.gitlab
        self.m.gitlab.all = all_api
        self.m.gitlab.api = api

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    async def test_configured_repository_directory_and_branch_publish_and_refresh(self):
        from assistant.pipeline_config import pages_target

        self.m.pages_target = pages_target(
            dict(
                PIPELINE_PAGES_REPO_URL="ssh://git@reports.example:2222/team/sub/reports.git",
                PIPELINE_PAGES_BRANCH="release/reports",
            )
        )
        moved = self.repo.with_name("reports")
        self.repo.rename(moved)
        self.repo = moved
        self.git("checkout", "-b", "release/reports", cwd=self.repo)
        self.git("push", "origin", "release/reports", cwd=self.repo)
        pub = Publisher(self.m)
        self.assertEqual(pub.repo, moved)
        self.assertEqual(pub.api_base, "projects/team%2Fsub%2Freports")
        await pub.tick()
        flight = self.m.meta("flight")
        self.assertIsNotNone(flight)
        self.assertEqual(
            self.git("rev-parse", "release/reports", cwd=self.remote), flight["sha"]
        )
        self.assertNotEqual(
            self.git("rev-parse", "main", cwd=self.remote), flight["sha"]
        )
        await pub.refresh()
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM pipeline_remote").fetchone()[0], 1
        )

    async def test_target_change_with_publication_state_is_rejected_before_git(self):
        from assistant.pipeline_config import pages_target

        await Publisher(self.m).tick()
        self.m.pages_target = pages_target(
            dict(PIPELINE_PAGES_REPO_URL="git@elsewhere.example:team/reports.git")
        )
        self.m.run = AsyncMock()
        with self.assertRaisesRegex(ValueError, "原目标的发布记录"):
            await Publisher(self.m).prepare()
        self.m.run.assert_not_awaited()

    async def test_unbound_legacy_publication_requires_explicit_migration(self):
        self.m.set_meta("shared_baseline", "legacy tree")
        self.m.run = AsyncMock()
        with self.assertRaisesRegex(ValueError, "旧发布记录尚未绑定"):
            await Publisher(self.m).prepare()
        self.m.run.assert_not_awaited()
        self.assertIsNone(self.m.meta("pages_target"))

    async def test_wrong_origin_does_not_fetch(self):
        self.m.run = AsyncMock(return_value="git@other.example:team/pipeline-pages.git")
        with self.assertRaisesRegex(ValueError, "origin 不匹配"):
            await Publisher(self.m).prepare()
        self.assertEqual(self.m.run.await_count, 1)

    def mutate_remote(self, path, content):
        other = self.root / "other"
        if not other.exists():
            self.git("clone", str(self.remote), str(other))
            self.git("config", "user.email", "fixture@example.invalid", cwd=other)
            self.git("config", "user.name", "Fixture", cwd=other)
        else:
            self.git("pull", "--ff-only", cwd=other)
        target = other / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        self.git("add", ".", cwd=other)
        self.git("commit", "-m", "External fixture change", cwd=other)
        self.git("push", "origin", "main", cwd=other)

    async def test_busy_publication_coalesces_revisions(self):
        p = Publisher(self.m)
        await p.tick()
        first = self.m.meta("flight")["sha"]
        t = self.m.get(self.n)
        self.m.changed(t)
        self.m.changed(t)
        await p.tick()
        self.assertEqual(self.m.meta("flight")["sha"], first)
        self.status = "success"
        await p.tick()
        # Initial link queued blocks the next push until its send has been attempted.
        with self.db:
            self.db.execute("UPDATE pipeline_deliveries SET state='sent'")
        await p.tick()
        second = self.m.meta("flight")["sha"]
        self.assertNotEqual(first, second)
        self.assertEqual(self.m.meta("flight")["tasks"][str(self.n)]["revision"], 3)

    async def test_disjoint_remote_change_preserved(self):
        p = Publisher(self.m)
        await p.tick()
        self.status = "success"
        await p.tick()
        other = "b" * 32
        self.mutate_remote(
            "tasks/" + other + "/manifest.json",
            dump(dict(schema=1, task_id=other, months=[], project_path="other/repo")),
        )
        t = self.m.get(self.n)
        self.m.changed(t)
        with self.db:
            self.db.execute("UPDATE pipeline_deliveries SET state='sent'")
        await p.tick()
        saved = self.git(
            "show", "main:tasks/" + other + "/manifest.json", cwd=self.remote
        )
        self.assertEqual(json.loads(saved)["task_id"], other)

    async def test_owned_external_change_is_conflict(self):
        p = Publisher(self.m)
        await p.tick()
        self.status = "success"
        await p.tick()
        self.mutate_remote("tasks/" + "a" * 32 + "/manual.txt", "external edit")
        head = self.git("rev-parse", "main", cwd=self.remote)
        t = self.m.get(self.n)
        self.m.changed(t)
        with self.db:
            self.db.execute("UPDATE pipeline_deliveries SET state='sent'")
        await p.tick()
        self.assertIn("冲突", self.m.get(self.n)["data"]["sync"])
        self.assertEqual(self.git("rev-parse", "main", cwd=self.remote), head)

    async def test_delete_waits_for_inflight_and_never_resurrects(self):
        p = Publisher(self.m)
        await p.tick()
        old = self.m.meta("flight")["sha"]
        t = self.m.get(self.n)
        t["state"] = "deleting"
        self.m.changed(t)
        await p.tick()
        self.assertEqual(self.m.meta("flight")["sha"], old)
        self.status = "success"
        await p.tick()
        self.assertEqual(self.m.get(self.n)["state"], "deleting")
        self.assertNotEqual(self.m.meta("flight")["sha"], old)
        await p.tick()
        self.assertEqual(self.m.get(self.n)["state"], "deleted")
        self.assertEqual(
            self.git(
                "ls-tree", "-r", "main", "--", "tasks/" + "a" * 32, cwd=self.remote
            ),
            "",
        )
        await p.tick()
        self.assertIsNone(self.m.meta("flight"))

    async def test_ambiguous_push_reconciled_without_duplicate(self):
        from assistant.http_errors import GitCommandError

        original = self.m.run

        async def uncertain(args, timeout, cwd=None):
            result = await original(args, timeout, cwd)
            if args[:2] == ["git", "push"]:
                raise asyncio.TimeoutError()
            return result

        self.m.run = uncertain
        with self.assertRaisesRegex(GitCommandError, "Git push：执行超时"):
            await Publisher(self.m).tick()
        first = self.m.meta("flight")["sha"]
        self.m.run = original
        await Publisher(self.m).tick()
        self.assertEqual(self.m.meta("flight")["sha"], first)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.remote), first)

    async def test_wrong_branch_and_changed_generator_block_without_push(self):
        from assistant.http_errors import PublicationBlocked

        before = self.git("rev-parse", "main", cwd=self.remote)
        self.git("switch", "-c", "development", cwd=self.repo)
        with self.assertRaisesRegex(PublicationBlocked, "发布分支"):
            await Publisher(self.m).tick()
        self.git("switch", "main", cwd=self.repo)
        self.m.set_meta("shared_baseline", "previously reviewed tree")
        with self.assertRaisesRegex(PublicationBlocked, "审查发布变更"):
            await Publisher(self.m).tick()
        self.assertIsNone(self.m.meta("flight"))
        self.assertEqual(self.git("rev-parse", "main", cwd=self.remote), before)

    async def test_confirmed_deployment_clears_only_publication_errors_and_legacy_metadata(self):
        for area in ["报告发布失败", "Pages 发布等待超时", "采集失败", "飞书投递结果不确定"]:
            with self.subTest(area=area):
                task = self.m.get(self.n)
                message = area + "：RuntimeError"
                metadata = dict(status=503, endpoint="projects/1/pages")
                task["data"].update(error=message, api_error=metadata)
                task["data"]["revision"] += 1
                self.m.save(task)
                self.status = "running"
                await Publisher(self.m).tick()
                self.assertEqual(self.m.get(self.n)["data"]["error"], message)
                self.status = "success"
                await Publisher(self.m).tick()
                data = self.m.get(self.n)["data"]
                if area in ("报告发布失败", "Pages 发布等待超时"):
                    self.assertNotIn("error", data)
                    self.assertNotIn("api_error", data)
                else:
                    self.assertEqual(data["error"], message)
                    self.assertEqual(data["api_error"], metadata)
                with self.db:
                    self.db.execute("UPDATE pipeline_deliveries SET state='sent'")

    async def test_deployment_recovery_respects_api_error_origin(self):
        from assistant.pipeline_gitlab import GitLabAPIError

        for origin, latest in [("报告发布失败", "采集失败"), ("采集失败", "报告发布失败")]:
            with self.subTest(origin=origin):
                task = self.m.get(self.n)
                task["data"]["revision"] += 1
                self.m.save(task)
                self.status = "running"
                await Publisher(self.m).tick()
                self.m.issue(task, origin, GitLabAPIError("projects/1/pages", 503))
                self.m.issue(task, latest, RuntimeError("DEMO_SECRET"))
                self.status = "success"
                await Publisher(self.m).tick()
                data = self.m.get(self.n)["data"]
                if origin == "报告发布失败":
                    self.assertTrue(data["error"].startswith(latest + "："))
                    self.assertNotIn("api_error", data)
                else:
                    self.assertNotIn("error", data)
                    self.assertEqual(data["api_error"]["area"], origin)
                with self.db:
                    self.db.execute("UPDATE pipeline_deliveries SET state='sent'")

    async def test_deployment_clears_owned_api_metadata_after_scan_clears_latest_error(self):
        from assistant.pipeline_gitlab import GitLabAPIError

        publisher = Publisher(self.m)
        await publisher.tick()
        task = self.m.get(self.n)
        self.m.issue(task, "报告发布失败", GitLabAPIError("projects/1/pages", 503))
        self.m.issue(task, "采集失败", RuntimeError())
        self.m.gitlab.page = AsyncMock(return_value=[])
        await self.m.scan(self.m.get(self.n), time.time() + 1)
        data = self.m.get(self.n)["data"]
        self.assertNotIn("error", data)
        self.assertEqual(data["api_error"]["area"], "报告发布失败")
        self.status = "success"
        await publisher.tick()
        self.assertNotIn("api_error", self.m.get(self.n)["data"])

    async def test_dirty_real_checkout_never_committed(self):
        (self.repo / "review.txt").write_text("unreviewed")
        before = self.git("rev-parse", "main", cwd=self.remote)
        with self.assertRaises(ValueError):
            await Publisher(self.m).tick()
        self.assertEqual(self.git("rev-parse", "main", cwd=self.remote), before)

    async def test_paused_manifest_does_not_ack_or_drop_pending_data(self):
        p = Publisher(self.m)
        await p.tick()
        self.status = "success"
        await p.tick()
        task = self.m.get(self.n)
        task["data"]["link_queued"] = True
        with self.db:
            self.db.execute("UPDATE pipeline_deliveries SET state='sent'")
        record = execution(snapshot(), [], 1)
        record["data_revision"] = 2
        with self.db:
            self.db.execute(
                "INSERT INTO pipeline_records VALUES(?,?,?,?,?)",
                (
                    task["task_id"],
                    record["execution_id"],
                    7,
                    record["finished"],
                    dump(record),
                ),
            )
        task["data"]["revision"] = 2
        task["state"] = "paused"
        self.m.cfg["TIMEZONE"] = "UTC"
        self.m.changed(task)
        await p.tick()
        self.assertEqual(
            self.m.meta("flight")["tasks"][str(self.n)]["data_revision"], 1
        )
        await p.tick()
        task = self.m.get(self.n)
        self.assertEqual(task["data"]["published_revision"], 3)
        self.assertEqual(task["data"]["published_data_revision"], 1)
        manifest = json.loads(
            self.git(
                "show",
                "main:tasks/" + task["task_id"] + "/manifest.json",
                cwd=self.remote,
            )
        )
        self.assertEqual(manifest["months"], [])
        self.assertEqual(manifest["timezone"], "Asia/Shanghai")
        await p.tick()
        self.assertIsNone(self.m.meta("flight"))
        task["state"] = "active"
        self.m.changed(task)
        await p.tick()
        await p.tick()
        task = self.m.get(self.n)
        self.assertEqual(task["data"]["published_data_revision"], 4)
        manifest = json.loads(
            self.git(
                "show",
                "main:tasks/" + task["task_id"] + "/manifest.json",
                cwd=self.remote,
            )
        )
        self.assertEqual(len(manifest["months"]), 1)
        month = manifest["months"][0]
        rows = json.loads(
            self.git(
                "show",
                "main:tasks/" + task["task_id"] + "/" + month + ".json",
                cwd=self.remote,
            )
        )
        self.assertEqual(rows[0]["execution_id"], record["execution_id"])

    async def test_shared_change_requires_exact_delivered_review_and_rechecks_remote(
        self,
    ):
        p = Publisher(self.m)
        await p.tick()
        self.status = "success"
        await p.tick()
        task = self.m.get(self.n)
        with self.db:
            self.db.execute("UPDATE pipeline_deliveries SET state='sent'")
        self.m.changed(task)
        self.mutate_remote("scripts/review.txt", "first shared change")
        with self.assertRaises(ValueError):
            await p.tick()
        sid = task["session_id"]
        review = await self.m.text("审查发布变更", "review-one", sid)
        token = review.split("确认发布变更 ")[-1]
        self.assertIn(
            "不一致", await self.m.text("确认发布变更 " + token, "early", sid)
        )
        with self.db:
            self.db.execute(
                "INSERT INTO inbox(id,binding,state) VALUES('review-one','oc_owner','done')"
            )
        self.mutate_remote("scripts/review.txt", "second shared change")
        self.assertIn(
            "不一致", await self.m.text("确认发布变更 " + token, "stale", sid)
        )
        review = await self.m.text("审查发布变更", "review-two", sid)
        token = review.split("确认发布变更 ")[-1]
        with self.db:
            self.db.execute(
                "INSERT INTO inbox(id,binding,state) VALUES('review-two','oc_owner','done')"
            )
        self.assertIn(
            "已接受", await self.m.text("确认发布变更 " + token, "exact", sid)
        )
        await p.tick()
        self.assertIsNotNone(self.m.meta("flight"))


class ServiceIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_create_from_existing_thread_binds_new_session(self):
        from assistant import service

        with tempfile.TemporaryDirectory() as directory:
            db = database(":memory:")
            sessions = Sessions(db, "oc_owner")
            old = sessions.create("om_old", "既有话题")
            sessions.bind(old, "om_old", "omt_old")
            db.execute(
                "INSERT INTO inbox(id,binding,content,kind,acknowledged,thread_id,root_id,thread_checked) "
                "VALUES('om_create','oc_owner','/pipeline creat','text',1,'omt_old','om_old',1)"
            )
            db.commit()
            cfg = dict(
                FEISHU_OWNER_CHAT_ID="oc_owner",
                FEISHU_OWNER_OPEN_ID="ou_owner",
                GITLAB_HOST="git.example",
                GLAB_BIN="glab",
                LARK_CLI_BIN="lark",
                PIPELINE_MONITOR_ENABLED=True,
            )
            sent = asyncio.Event()
            calls = []

            async def run(args, *a, **kw):
                calls.append(args)
                if "+messages-send" in args:
                    return dump(dict(ok=True, data=dict(message_id="om_new")))
                if "+messages-reply" in args:
                    self.assertEqual(args[args.index("--message-id") + 1], "om_new")
                    sent.set()
                    return dump(
                        dict(
                            ok=True,
                            data=dict(message_id="om_reply", thread_id="omt_new"),
                        )
                    )
                raise AssertionError(args)

            with (
                patch.object(service, "run_command", run),
                patch.object(service, "ROOT", Path(directory)),
            ):
                worker = asyncio.create_task(service.worker(cfg, db))
                try:
                    await asyncio.wait_for(sent.wait(), 2)
                    await asyncio.sleep(0)
                    row = db.execute(
                        "SELECT state,session_id FROM inbox WHERE id='om_create'"
                    ).fetchone()
                    self.assertEqual(row[0], "done")
                    self.assertNotEqual(row[1], old)
                    self.assertEqual(sessions.thread(row[1])["root_id"], "om_new")
                finally:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                    db.close()
