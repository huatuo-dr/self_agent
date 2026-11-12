import copy
import json
import sqlite3
import time
import unittest
from unittest.mock import AsyncMock

from assistant.pipeline import DEFAULTS
from assistant.pipeline_card import card, job_choices, job_section, replace_job_section
from assistant.pipeline_interaction import PipelineInteractions
from test_pipeline_card_charts import job, record


class JobCharts(unittest.TestCase):
    def test_loading_removes_previous_jobs_and_details_but_keeps_selector(self):
        r = record([job(1, 1000, 1030, "prepare"), job(2, 1060, 1180, "build")])
        p = card(dict(config=dict(DEFAULTS, project_path="g/r")), r)
        option = job_choices(r)[0]["value"]
        empty = replace_job_section(p, r, option, loading=True)
        section = {
            e["element_id"]: e
            for e in empty["body"]["elements"]
            if e.get("element_id", "").startswith("job_")
        }
        self.assertEqual(set(section), {"job_heading", "job_selector", "job_note"})
        self.assertEqual(section["job_selector"]["initial_option"], option)
        self.assertFalse(section["job_selector"].get("disabled", False))
        self.assertIn("正在更新", section["job_note"]["content"])
        self.assertEqual(
            replace_job_section(empty, r, option), replace_job_section(p, r, option)
        )

    def test_axis_caption_outside_canvas_for_small_job_counts(self):
        for count in (1, 2, 3):
            with self.subTest(count=count):
                elements = job_section(
                    record([job(i, 1000, 1060, "build") for i in range(count)])
                )
                graph = next(
                    e for e in elements if e.get("element_id") == "job_timeline"
                )
                caption = next(
                    e for e in elements if e.get("element_id") == "job_axis_caption"
                )
                self.assertFalse(graph["chart_spec"]["axes"][0]["title"]["visible"])
                self.assertEqual(caption["content"], "距执行开始（分钟）")
                self.assertEqual(elements.index(caption), elements.index(graph) + 1)

    def test_switch_preserves_other_content_and_shared_axis(self):
        r = record([job(1, 1000, 1030, "prepare"), job(2, 1060, 1180, "build")])
        p = card(dict(config=dict(DEFAULTS, project_path="g/r")), r)
        p["header"]["title"]["content"] = "preview"
        before = copy.deepcopy(p)
        options = job_choices(r)
        updated = replace_job_section(p, r, options[1]["value"])
        self.assertEqual(p, before)
        self.assertEqual(updated["header"], p["header"])
        other = lambda c: [
            e
            for e in c["body"]["elements"]
            if not e.get("element_id", "").startswith("job_")
        ]
        self.assertEqual(other(updated), other(p))
        graphs = [
            e["chart_spec"] for e in updated["body"]["elements"] if e["tag"] == "chart"
        ]
        self.assertEqual(graphs[0]["axes"][0]["max"], graphs[1]["axes"][0]["max"])
        self.assertEqual(graphs[1]["data"][0]["values"][0]["start"], 1)
        with self.assertRaises(ValueError):
            replace_job_section(p, r, "unknown-stage")

    def test_child_identity_pagination_missing_zero_failure(self):
        jobs = [job(i, 1000 + i, 1060 + i, "build") for i in range(25)]
        child = job(40, 1000, 1000, "build")
        child.update(pipeline_id=8, status="failed", allow_failure=True)
        missing = job(41, 1000, 1100, "test")
        missing["finished_at"] = None
        r = record(jobs + [child, missing])
        choices = job_choices(r)
        self.assertEqual(len(choices), 5)
        self.assertEqual([len(c["jobs"]) for c in choices[:3]], [12, 12, 1])
        self.assertEqual(len({c["value"] for c in choices}), 5)
        child_choice = next(c for c in choices if c["group"]["key"][1] == 8)
        elements = job_section(r, child_choice["value"])
        value = next(e for e in elements if e.get("element_id") == "job_timeline")[
            "chart_spec"
        ]["data"][0]["values"][0]
        self.assertEqual((value["start"], value["end"]), (0, 0))
        self.assertIn("非阻塞", value["status"])
        choice = next(c for c in choices if c["group"]["name"] == "test")
        elements = job_section(r, choice["value"])
        self.assertFalse(any(e["tag"] == "chart" for e in elements))
        self.assertIn("缺少完整起止", json.dumps(elements, ensure_ascii=False))


class Interactions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE pipeline_tasks(task_id TEXT,binding TEXT,state TEXT,config TEXT)"
        )
        self.db.execute(
            "INSERT INTO pipeline_tasks VALUES(?,?,?,?)",
            ("t", "oc_owner", "paused", json.dumps(dict(recipients=["oc_group"]))),
        )
        self.cfg = dict(
            FEISHU_OWNER_CHAT_ID="oc_owner",
            FEISHU_OWNER_OPEN_ID="ou_owner",
            LARK_CLI_BIN="lark",
        )
        self.run = AsyncMock(return_value=json.dumps(dict(ok=True)))
        self.i = PipelineInteractions(self.cfg, self.db, self.run)
        self.r = record([job(1, 1000, 1060, "prepare"), job(2, 1060, 1200, "build")])
        self.p = card(dict(config=dict(DEFAULTS, project_path="g/r")), self.r)
        self.i.register("om_card", "t", "self", self.r, self.p)
        self.e = dict(
            type="card.action.trigger",
            host="im_message",
            action_tag="select_static",
            action_name="pipeline_job_stage",
            form_value="",
            event_id="ev1",
            message_id="om_card",
            operator_id="ou_owner",
            chat_id="oc_owner",
            token="secret-update-token",
            card_content=json.dumps(self.p),
            option=job_choices(self.r)[0]["value"],
            timestamp=str(int(time.time() * 1000)),
        )

    def tearDown(self):
        self.db.close()

    async def test_update_and_restart_dedup_never_persist_token(self):
        self.assertTrue(await self.i.handle(self.e))
        args = self.run.call_args.args[0]
        self.assertIn("/open-apis/interactive/v1/card/update", args)
        self.assertNotIn(self.e["token"], str(args))
        sent = json.loads(self.run.call_args.kwargs["input_text"])
        self.assertEqual(sent["token"], self.e["token"])
        self.assertEqual(sent["card"]["header"], self.p["header"])
        restarted = PipelineInteractions(self.cfg, self.db, self.run)
        self.assertFalse(await restarted.handle(self.e))
        self.assertEqual(self.run.await_count, 2)
        loading = json.loads(self.run.call_args_list[0].kwargs["input_text"])["card"]
        self.assertFalse(
            any(
                e.get("element_id") in ("job_timeline", "job_details")
                for e in loading["body"]["elements"]
            )
        )
        self.assertNotIn(self.e["token"], "\n".join(self.db.iterdump()))

    async def test_standalone_select_without_name_uses_explicit_action_value(self):
        event = dict(
            self.e,
            action_value=json.dumps(dict(action="pipeline_job_stage", version=1)),
        )
        del event["action_name"]
        self.assertTrue(await self.i.handle(event))
        updated = json.loads(self.run.call_args.kwargs["input_text"])["card"]
        selector = next(
            e
            for e in updated["body"]["elements"]
            if e.get("element_id") == "job_selector"
        )
        self.assertEqual(selector["initial_option"], event["option"])

    async def test_missing_name_does_not_accept_arbitrary_action_values(self):
        for value in (
            "{}",
            "not-json",
            "[]",
            json.dumps(dict(action="other", version=1)),
            json.dumps(dict(action="pipeline_job_stage", version=True)),
        ):
            self.assertFalse(
                await self.i.handle(dict(self.e, action_name="", action_value=value))
            )
        self.assertFalse(
            await self.i.handle(
                dict(self.e, action_value=json.dumps(dict(action="other", version=1)))
            )
        )
        self.run.assert_not_awaited()

    async def test_optional_callback_card_content_uses_registered_snapshot(self):
        for index, content in enumerate([None, "", '{"untrusted":"DEMO_INJECTION"}']):
            with self.subTest(content=content):
                option = job_choices(self.r)[index % 2]["value"]
                event = dict(
                    self.e,
                    event_id=f"optional-card-{index}",
                    timestamp=str(int(self.e["timestamp"]) + index),
                    action_name="",
                    action_value=json.dumps(dict(action="pipeline_job_stage", version=1)),
                    option=option,
                )
                if content is None:
                    event.pop("card_content")
                else:
                    event["card_content"] = content
                self.assertTrue(await self.i.handle(event))
                updated = json.loads(self.run.call_args.kwargs["input_text"])["card"]
                selector = next(e for e in updated["body"]["elements"] if e.get("element_id") == "job_selector")
                self.assertEqual(selector["initial_option"], option)
                self.assertEqual(updated["header"], self.p["header"])
                self.assertNotIn("DEMO_INJECTION", json.dumps(updated))
        self.assertEqual(self.run.await_count, 6)

    async def test_microsecond_callback_preserves_freshness_and_dedup(self):
        event = dict(
            self.e,
            action_name="",
            action_value=json.dumps(dict(action="pipeline_job_stage", version=1)),
            timestamp=str(int(self.e["timestamp"]) * 1000 + 321),
        )
        self.assertTrue(await self.i.handle(event))
        saved = self.db.execute("SELECT last_ms FROM pipeline_card_views").fetchone()[0]
        self.assertEqual(saved, int(self.e["timestamp"]))
        self.assertFalse(await self.i.handle(event))
        self.assertEqual(
            self.cfg["_runtime"]["pipeline_card_last_rejection"], "duplicate_or_stale"
        )
        old = dict(
            event,
            event_id="old",
            timestamp=str((int(self.e["timestamp"]) - 31 * 60 * 1000) * 1000),
        )
        self.assertFalse(await self.i.handle(old))
        self.assertEqual(
            self.cfg["_runtime"]["pipeline_card_last_rejection"],
            "timestamp_out_of_window",
        )

    def test_timestamp_normalization_accepts_explicit_epoch_units_only(self):
        ms = int(self.e["timestamp"])
        for raw, expected in [
            (str(ms), ms),
            (str(ms * 1000 + 999), ms),
            (str(ms * 1000000 + 999999), ms),
            (str(ms // 1000), ms // 1000 * 1000),
        ]:
            self.assertEqual(PipelineInteractions.timestamp_ms(raw), expected)
        for raw in (None, True, "NaN", "1e13", "12345678901234", str(ms) + ".1"):
            with self.assertRaises(ValueError):
                PipelineInteractions.timestamp_ms(raw)

    async def test_scope_and_invalid_inputs_do_not_update(self):
        for change in [
            dict(type="other"),
            dict(message_id="om_other"),
            dict(chat_id="oc_other"),
            dict(operator_id="ou_other"),
            dict(option="forged"),
            dict(token=""),
            dict(timestamp="0"),
            dict(action_name="other"),
            dict(form_value="{}"),
        ]:
            with self.subTest(change=change):
                event = dict(self.e, **change)
                event.pop("card_content")
                self.assertFalse(await self.i.handle(event))
        self.run.assert_not_awaited()
        self.db.execute("UPDATE pipeline_tasks SET state='deleted'")
        self.assertFalse(await self.i.handle(self.e))

    async def test_group_requires_registered_recipient_and_current_scope(self):
        self.i.register("om_group", "t", "oc_group", self.r, self.p)
        event = dict(
            self.e, message_id="om_group", operator_id="ou_member", chat_id="oc_group"
        )
        self.assertTrue(await self.i.handle(event))
        self.db.execute(
            "UPDATE pipeline_tasks SET config=?", (json.dumps(dict(recipients=[])),)
        )
        self.assertFalse(
            await self.i.handle(
                dict(event, event_id="ev2", timestamp=str(int(event["timestamp"]) + 1))
            )
        )

    async def test_failed_update_does_not_claim_success_or_repeat_same_event(self):
        self.run.return_value = json.dumps(dict(ok=False))
        with self.assertRaises(RuntimeError):
            await self.i.handle(self.e)
        self.assertNotIn("pipeline_card_last_update", self.cfg.get("_runtime", {}))
        self.assertFalse(await self.i.handle(self.e))
        self.run.return_value = json.dumps(dict(ok=True))
        self.assertTrue(
            await self.i.handle(
                dict(
                    self.e, event_id="ev2", timestamp=str(int(self.e["timestamp"]) + 1)
                )
            )
        )

    async def test_loading_failure_still_attempts_final_data_once(self):
        self.run.side_effect = [RuntimeError("network"), json.dumps(dict(ok=True))]
        self.assertTrue(await self.i.handle(self.e))
        self.assertEqual(self.run.await_count, 2)
        saved = json.loads(
            self.db.execute("SELECT card FROM pipeline_card_views").fetchone()[0]
        )
        self.assertTrue(
            any(
                e.get("element_id") == "job_timeline" for e in saved["body"]["elements"]
            )
        )

    async def test_final_failure_keeps_empty_recoverable_view(self):
        self.run.side_effect = [json.dumps(dict(ok=True)), RuntimeError("network")]
        with self.assertRaises(RuntimeError):
            await self.i.handle(self.e)
        self.assertEqual(self.run.await_count, 2)
        self.assertNotIn("pipeline_card_last_update", self.cfg.get("_runtime", {}))
        saved = json.loads(
            self.db.execute("SELECT card FROM pipeline_card_views").fetchone()[0]
        )
        self.assertFalse(
            any(
                e.get("element_id") == "job_timeline" for e in saved["body"]["elements"]
            )
        )
        self.assertIn("重新选择阶段", json.dumps(saved, ensure_ascii=False))
        self.run.side_effect = None
        self.assertTrue(
            await self.i.handle(
                dict(
                    self.e,
                    event_id="retry",
                    timestamp=str(int(self.e["timestamp"]) + 1),
                )
            )
        )
