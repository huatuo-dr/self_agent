"""Numeric and rendering contracts for Pipeline duration cards."""

import copy
import json
import unittest
from datetime import datetime, timezone

from assistant.pipeline import DEFAULTS
from assistant.pipeline_card import card, phase_data
from assistant.pipeline_gitlab import alerts, execution, iso


def job(identity, start, end, stage="test", **extra):
    return dict(
        id=identity,
        project_id=1,
        pipeline_id=7,
        name=f"job-{identity}",
        stage=stage,
        status="success",
        started_at=iso(start),
        finished_at=iso(end),
        duration=end - start,
        queued_duration=0,
        **extra,
    )


def record(jobs):
    return execution(
        dict(
            complete=True,
            pipeline=dict(
                id=7, ref="main", status="success", source="push", finished_at=iso(2000)
            ),
            jobs=jobs,
            downstream=[],
        ),
        [],
        1,
    )


class ChartTests(unittest.TestCase):
    def test_legend_has_no_auxiliary_series_or_axis(self):
        r = record([job(1, 1000, 1600, "build_test")])
        payload = card(dict(config=dict(DEFAULTS, project_path="g/r")), r)
        graphs = [e for e in payload["body"]["elements"] if e["tag"] == "chart"]
        self.assertEqual(len(graphs), 2)
        spec = graphs[0]["chart_spec"]
        self.assertTrue(spec["legends"]["visible"])
        self.assertEqual(spec["type"], "rangeColumn")
        self.assertEqual(spec["data"][0]["fields"]["row"]["domain"], ["1. build_test"])
        self.assertEqual(spec["data"][0]["values"][0]["end"], 10)
        self.assertNotIn('"legend"', json.dumps(spec["data"]))

    def test_phase_partition_deduplicates_jobs_parallel_stages_and_gaps(self):
        a = job(1, 1000, 1060, "build_test")
        r = record(
            [
                a,
                a.copy(),
                job(2, 1030, 1090, "test"),
                job(3, 1040, 1080, "test"),
                job(4, 1120, 1150, "test"),
            ]
        )
        groups, totals, combinations = phase_data(r)
        self.assertEqual(len(groups), 2)
        self.assertEqual(
            sum(totals.values()), 120
        )  # 90 sec running, gap, 30 sec running
        self.assertEqual(totals["编译测试"], 30)
        self.assertEqual(totals["测试"], 60)
        self.assertEqual(totals["多阶段同时运行"], 30)
        self.assertEqual(sum(combinations.values()), 30)
        self.assertEqual([g["seconds"] for g in groups], [60, 90])
        self.assertEqual([len(g["jobs"]) for g in groups], [1, 3])

    def test_child_pipeline_stages_are_not_merged(self):
        a, b = job(1, 1000, 1060), job(2, 1030, 1090)
        b["pipeline_id"] = 8
        groups, totals, _ = phase_data(record([a, b]))
        self.assertEqual(len(groups), 2)
        self.assertEqual(totals["多阶段同时运行"], 30)
        self.assertEqual(sum(totals.values()), 90)

    def test_unknown_timing_does_not_invent_complete_pipeline_bar(self):
        a, b = job(1, 1000, 1060), job(2, 1000, 1090)
        b["finished_at"] = None
        r = record([a, b])
        task = dict(config=dict(DEFAULTS, project_path="g/r"))
        payload = card(task, r)
        graphs = [e for e in payload["body"]["elements"] if e["tag"] == "chart"]
        rows = graphs[0]["chart_spec"]["data"][0]["fields"]["row"]["domain"]
        self.assertNotIn("整体执行", rows)
        self.assertTrue(any("*" in row for row in rows))
        self.assertIn("已知", json.dumps(payload, ensure_ascii=False))
        r["jobs"] = [b]
        payload = card(task, r)
        self.assertFalse(any(e["tag"] == "chart" for e in payload["body"]["elements"]))

    def test_daily_history_is_frozen_comparable_and_missing_is_not_zero(self):
        r = record([job(1, 1000, 1060)])
        r["finished"] = datetime(2026, 11, 9, 4, tzinfo=timezone.utc).timestamp()

        def past(delta, seconds, **extra):
            return dict(
                r, finished=r["finished"] - delta, active_seconds=seconds, **extra
            )

        history = [
            past(60, 0),
            past(120, 120),
            past(2 * 86400, 240),
            past(2 * 86400, None),
            past(8 * 86400, 999),
        ]
        for field, value in [
            ("status", "failed"),
            ("tag", True),
            ("source", "web"),
            ("kind", "partial_retry"),
            ("matched_branch", "release"),
            ("config_version", 2),
            ("metric_version", 2),
        ]:
            entry = past(600, 999)
            entry[field] = value
            history.append(entry)
        history.append(past(-1, 999))
        alerts(r, history, DEFAULTS["thresholds"])
        self.assertEqual(r["baseline"]["samples"], 3)
        self.assertEqual(r["baseline"]["median"], 120)
        self.assertEqual(
            r["baseline"]["daily"],
            [
                dict(day="2026-11-07", median=240, samples=1),
                dict(day="2026-11-09", median=60, samples=2),
            ],
        )
        saved = copy.deepcopy(r["baseline"])
        history[0]["active_seconds"] = 9000
        self.assertEqual(r["baseline"], saved)
        payload = card(dict(config=dict(DEFAULTS, project_path="g/r")), r)
        panels = [
            e for e in payload["body"]["elements"] if e["tag"] == "collapsible_panel"
        ]
        chart = panels[0]["elements"][1]["chart_spec"]
        self.assertEqual(chart["type"], "heatmap")
        self.assertEqual(len(chart["data"]["values"]), 2)  # Missing days stay blank.
        self.assertEqual(sum(int(v["samples"]) for v in chart["data"]["values"]), 3)

    def test_large_many_stage_card_preserves_chart_and_stays_under_budget(self):
        jobs = [
            job(i, 1000 + i, 1100 + i, "stage-" + str(i) + "很长名称" * 30)
            for i in range(200)
        ]
        for j in jobs:
            j["name"] = "<at id=all>" + "长名称" * 150
        r = record(jobs)
        r["alerts"] = [
            dict(type="执行超时", actual=9000, threshold=1800) for _ in range(90)
        ]
        r["reasons"] = [
            dict(job_id=i, category="未分类", explanation="原因" * 200, evidence="")
            for i in range(90)
        ]
        original = copy.deepcopy(r)
        payload = card(
            dict(config=dict(DEFAULTS, project_path="g/r")),
            r,
            "https://report.example/",
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertLess(len(serialized.encode()), 28000)
        self.assertIn("https://report.example/", serialized)
        self.assertIn("其余阶段", serialized)
        self.assertNotIn("<at id=all>", json.dumps(payload["body"], ensure_ascii=False))
        self.assertTrue(any(e["tag"] == "chart" for e in payload["body"]["elements"]))
        self.assertEqual(r, original)

    def test_tooltip_text_keeps_ids_readable_and_neutralizes_markup(self):
        from assistant.pipeline_card import chart_text

        self.assertEqual(
            chart_text("Pipeline #7 · build_test"), "Pipeline #7 · build_test"
        )
        self.assertNotIn("<", chart_text("<img src=x onerror=alert(1)>"))
        r = record([job(1, 1000, 1060)])
        payload = card(dict(config=dict(DEFAULTS, project_path="g/r")), r)
        spec = next(
            e["chart_spec"]
            for e in payload["body"]["elements"]
            if e["tag"] == "chart" and e["chart_spec"]["tooltip"]["visible"]
        )
        self.assertEqual(spec["data"][0]["values"][0]["name"], "test")
        self.assertFalse(spec["axes"][1]["inverse"])

    def test_zero_duration_is_known_and_no_nan_is_serialized(self):
        r = record([job(1, 1000, 1000)])
        payload = card(dict(config=dict(DEFAULTS, project_path="g/r")), r)
        json.dumps(payload, allow_nan=False)
        main = next(
            e
            for e in payload["body"]["elements"]
            if e.get("element_id") == "stage_timeline"
        )
        values = main["chart_spec"]["data"][0]["values"]
        self.assertEqual((values[0]["start"], values[0]["end"]), (0, 0))

    def test_stage_timeline_uses_actual_offsets_and_preserves_gaps(self):
        r = record(
            [
                job(1, 1000, 1060, "prepare"),
                job(2, 1030, 1090, "test"),
                job(3, 1040, 1080, "test"),
                job(4, 1180, 1210, "test"),
            ]
        )
        payload = card(dict(config=dict(DEFAULTS, project_path="g/r")), r)
        main = next(
            e
            for e in payload["body"]["elements"]
            if e.get("element_id") == "stage_timeline"
        )
        spec = main["chart_spec"]
        self.assertEqual(spec["type"], "rangeColumn")
        self.assertEqual(spec["xField"], ["start", "end"])
        self.assertFalse(spec["stack"])
        self.assertNotIn("整体执行", spec["data"][0]["fields"]["row"]["domain"])
        values = spec["data"][0]["values"]
        test_runs = [v for v in values if v["name"] == "test"]
        self.assertEqual(
            [(v["start"], v["end"]) for v in test_runs], [(0.5, 1.5), (3, 3.5)]
        )
        self.assertEqual(values[0]["start"], 0)
        self.assertGreaterEqual(spec["axes"][0]["max"], 3.5)
        self.assertEqual(main["height"], "155px")  # 70% row height + axes + legend
        self.assertIn("08:17:10", test_runs[0]["began"])

    def test_timeline_origin_includes_known_start_with_missing_end(self):
        missing = job(1, 1000, 1100, "prepare")
        missing["finished_at"] = None
        r = record([missing, job(2, 1060, 1120, "test")])
        payload = card(dict(config=dict(DEFAULTS, project_path="g/r")), r)
        main = next(
            e
            for e in payload["body"]["elements"]
            if e.get("element_id") == "stage_timeline"
        )
        self.assertEqual(main["chart_spec"]["data"][0]["values"][0]["start"], 1)

    def test_fragmented_timeline_keeps_gaps_when_truncated_to_card_budget(self):
        r = record(
            [job(i, 1000 + i * 100, 1010 + i * 100, "阶段" * 100) for i in range(150)]
        )
        payload = card(dict(config=dict(DEFAULTS, project_path="g/r")), r)
        self.assertLess(len(json.dumps(payload, ensure_ascii=False).encode()), 28000)
        main = next(
            e
            for e in payload["body"]["elements"]
            if e.get("element_id") == "stage_timeline"
        )
        values = main["chart_spec"]["data"][0]["values"]
        self.assertLess(len(values), 150)
        self.assertTrue(
            all(abs((v["end"] - v["start"]) * 60 - 10) < 0.0001 for v in values)
        )
        self.assertIn("完整报告", json.dumps(payload, ensure_ascii=False))


class CardRefinements(unittest.TestCase):
    def test_defined_stage_order_preserves_actual_offsets(self):
        r = record(
            [
                job(3, 1000, 1120, "build_test", stage_index=2),
                job(1, 1005, 1020, ".pre", stage_index=0),
                job(2, 1006, 1030, "validate", stage_index=1),
            ]
        )
        groups, _, _ = phase_data(r)
        self.assertEqual(
            [g["name"] for g in groups], [".pre", "validate", "build_test"]
        )
        payload = card(dict(config=dict(DEFAULTS, project_path="g/r")), r)
        timeline = next(
            e
            for e in payload["body"]["elements"]
            if e.get("element_id") == "stage_timeline"
        )
        values = timeline["chart_spec"]["data"][0]["values"]
        self.assertEqual(
            [v["name"] for v in values], [".pre", "validate", "build_test"]
        )
        self.assertAlmostEqual(values[0]["start"], 5 / 60)
        self.assertEqual(values[2]["start"], 0)

    def test_missing_order_has_explicit_fallback_note(self):
        r = record([job(3, 1000, 1020, "build"), job(1, 1005, 1020, ".pre")])
        payload = card(dict(config=dict(DEFAULTS, project_path="g/r")), r)
        self.assertEqual(phase_data(r)[0][0]["name"], ".pre")
        self.assertIn("阶段定义顺序未核实", json.dumps(payload, ensure_ascii=False))

    def test_speed_buckets_and_insufficient_baseline(self):
        from assistant.pipeline_card import speed_label

        r = dict(active_seconds=600, baseline=dict(median=600, samples=10))
        for seconds, expected in [
            (480, ("快", "green")),
            (481, ("正常", "neutral")),
            (719, ("正常", "neutral")),
            (720, ("慢", "yellow")),
            (899, ("慢", "yellow")),
            (900, ("极慢", "red")),
        ]:
            with self.subTest(seconds=seconds):
                r["active_seconds"] = seconds
                self.assertEqual(speed_label(r, DEFAULTS["thresholds"])[:2], expected)
        r["baseline"]["samples"] = 1
        self.assertEqual(speed_label(r, DEFAULTS["thresholds"])[0], "样本不足")
        r["baseline"] = dict(samples=10, median=0)
        self.assertEqual(speed_label(r, DEFAULTS["thresholds"])[0], "无法比较")
        r["active_seconds"] = 0
        self.assertEqual(speed_label(r, DEFAULTS["thresholds"])[0], "正常")
        r["active_seconds"] = None
        self.assertEqual(speed_label(r, DEFAULTS["thresholds"])[0], "暂无对比")

    def test_status_tags_and_three_header_lines(self):
        r = record([job(1, 1000, 1060)])
        r["jobs"][0].update(status="failed", allow_failure=True)
        task = dict(config=dict(DEFAULTS, project_path="g/r"))
        header = card(task, r)["body"]["elements"][0]["content"]
        self.assertIn("<text_tag color='green'>成功</text_tag> · 有非阻塞异常", header)
        self.assertEqual(len(header.splitlines()), 3)
        self.assertTrue(header.splitlines()[1].startswith("耗时对比："))
        r["jobs"][0]["allow_failure"] = False
        self.assertIn(
            "<text_tag color='red'>失败</text_tag>",
            card(task, r)["body"]["elements"][0]["content"],
        )


    def test_selected_job_metrics_parallel_missing_and_unrun(self):
        from assistant.pipeline_card import job_section

        a, b = job(1, 1000, 1060), job(2, 1030, 1090)
        b["queued_duration"] = None
        c = job(3, 1000, 1000)
        c.update(status="skipped", started_at=None, finished_at=None,
                 duration=None, queued_duration=None)
        elements = job_section(record([a, b, c]))
        details = next(element for element in elements if element.get("element_id") == "job_details")
        summary = details["elements"][0]["content"]
        self.assertIn("阶段执行：1 分 30 秒", summary)
        self.assertIn("Job 执行合计：2 分 00 秒", summary)
        self.assertIn("每 Job 平均：1 分 00 秒（2/2", summary)
        self.assertIn("Job 排队合计：0.0 秒（1/2", summary)
        self.assertIn("已跳过", json.dumps(details, ensure_ascii=False))


class StageMetadata(unittest.IsolatedAsyncioTestCase):
    async def test_exact_pipeline_paginated_stage_order(self):
        from unittest.mock import AsyncMock
        from assistant.pipeline_gitlab import GitLab

        g = GitLab(dict(GITLAB_HOST="git.example"), None)

        def response(names, more=False, cursor=None, pid=7):
            return dict(
                data=dict(
                    project=dict(
                        pipeline=dict(
                            id=f"gid://gitlab/Ci::Pipeline/{pid}",
                            stages=dict(
                                nodes=[dict(name=n) for n in names],
                                pageInfo=dict(hasNextPage=more, endCursor=cursor),
                            ),
                        )
                    )
                )
            )

        p = dict(id=7, iid=2, web_url="https://git.example/g/r/-/pipelines/7")
        g.api = AsyncMock(
            side_effect=[
                response([".pre", "validate"], True, "abc"),
                response(["build", ".post"]),
            ]
        )
        self.assertEqual(
            await g.stage_order(p), {".pre": 0, "validate": 1, "build": 2, ".post": 3}
        )
        self.assertIn('after:"abc"', g.api.call_args.kwargs["query"])
        for bad in [
            response([".pre"], pid=8),
            dict(errors=["not supported"]),
            [],
            dict(data=dict(project=[])),
            response(["a", "a"]),
        ]:
            g.api = AsyncMock(return_value=bad)
            self.assertEqual(await g.stage_order(p), {})

    async def test_optional_order_preserves_rate_limit_backoff(self):
        from unittest.mock import AsyncMock
        from assistant.pipeline_gitlab import GitLab, GitLabAPIError, GitLabRateLimit

        g = GitLab(dict(GITLAB_HOST="git.example"), None)
        p = dict(id=7, iid=2, web_url="https://git.example/g/r/-/pipelines/7")
        g.api = AsyncMock(side_effect=GitLabAPIError("graphql", 403))
        self.assertEqual(await g.stage_order(p), {})
        g.api = AsyncMock(side_effect=GitLabRateLimit("git.example", "graphql", 100))
        with self.assertRaises(GitLabRateLimit):
            await g.stage_order(p)
        g.api.reset_mock()
        self.assertEqual(await g.stage_order(dict(id=7)), {})
        g.api.assert_not_awaited()

    async def test_collection_retains_order_on_execution_jobs(self):
        from unittest.mock import AsyncMock
        from assistant.pipeline_gitlab import GitLab

        g = GitLab(dict(GITLAB_HOST="git.example"), None)
        p = dict(
            id=7, status="success", ref="main", source="push", finished_at=iso(1100)
        )
        g.api = AsyncMock(return_value=p)
        g.all = AsyncMock(side_effect=[[job(1, 1000, 1100)], [job(1, 1000, 1100)], []])
        g.stage_order = AsyncMock(return_value={"test": 3})
        snap = await g.collect(1, 7)
        self.assertEqual(execution(snap, [], 1)["jobs"][0]["stage_index"], 3)
