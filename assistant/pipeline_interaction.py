"""Bounded, recipient-scoped card interactions; no model calls or new messages."""

import asyncio
import json
import logging
import time
from pathlib import Path

from assistant.pipeline_card import job_choices, replace_job_section
from assistant.settings import get_timezone


class PipelineInteractions:
    def __init__(self, cfg, db, run):
        self.cfg, self.db, self.run = cfg, db, run
        self.binding = cfg["FEISHU_OWNER_CHAT_ID"]
        db.execute("""CREATE TABLE IF NOT EXISTS pipeline_card_views (
          message_id TEXT PRIMARY KEY, binding TEXT NOT NULL, recipient TEXT NOT NULL,
          task_id TEXT NOT NULL, record TEXT NOT NULL, card TEXT NOT NULL,
          last_ms INTEGER NOT NULL DEFAULT 0, last_event TEXT NOT NULL DEFAULT '')""")
        db.commit()
        self.lock = asyncio.Lock()

    def register(self, message_id, task_id, recipient, record, payload):
        if not any(
            e.get("element_id") == "job_selector" for e in payload["body"]["elements"]
        ):
            return
        with self.db:
            self.db.execute(
                """INSERT INTO pipeline_card_views
                (message_id,binding,recipient,task_id,record,card) VALUES(?,?,?,?,?,?)
                ON CONFLICT(message_id) DO UPDATE SET record=excluded.record,card=excluded.card
                WHERE pipeline_card_views.binding=excluded.binding AND pipeline_card_views.task_id=excluded.task_id
                AND pipeline_card_views.recipient=excluded.recipient""",
                (
                    message_id,
                    self.binding,
                    recipient,
                    task_id,
                    json.dumps(record, ensure_ascii=False),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

    async def handle(self, event):
        async with self.lock:
            return await self._handle(event)

    @staticmethod
    def is_stage_action(event):
        if not isinstance(event, dict):
            return False
        # Feishu may omit the optional name on standalone selects. The explicit
        # callback value is the action identity; a name remains a legacy fallback.
        name = event.get("action_name")
        if name not in (None, "", "pipeline_job_stage"):
            return False
        raw = event.get("action_value")
        if raw:
            try:
                value = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                return False
            return (
                isinstance(value, dict)
                and value.get("action") == "pipeline_job_stage"
                and type(value.get("version")) is int
                and value["version"] == 1
            )
        return name == "pipeline_job_stage"

    @staticmethod
    def timestamp_ms(value):
        # Event header create_time can be microseconds, although some CLI schemas
        # label the flattened source timestamp as milliseconds. Normalize explicitly.
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise ValueError("Invalid event timestamp")
        raw = str(value)
        if not raw.isascii() or not raw.isdigit():
            raise ValueError("Invalid event timestamp")
        if len(raw) not in (10, 13, 16, 19):
            raise ValueError("Unsupported event timestamp precision")
        value = int(raw)
        if len(raw) == 10:
            return value * 1000
        return value // (10 ** (len(raw) - 13))

    def rejected(self, reason):
        self.cfg.setdefault("_runtime", {})["pipeline_card_last_rejection"] = reason
        logging.info("Pipeline card callback ignored: %s", reason)
        return False

    async def _handle(self, event):
        if (
            not isinstance(event, dict)
            or event.get("type") != "card.action.trigger"
            or event.get("host") != "im_message"
            or event.get("action_tag") != "select_static"
            or not self.is_stage_action(event)
            or event.get("form_value")
        ):
            return self.rejected("action_mismatch")
        if not all(
            isinstance(event.get(k), str) and event[k]
            for k in (
                "event_id",
                "message_id",
                "operator_id",
                "chat_id",
                "token",
                "option",
            )
        ):
            return self.rejected("missing_required_field")
        try:
            timestamp = self.timestamp_ms(event.get("timestamp", ""))
        except (TypeError, ValueError):
            return self.rejected("invalid_timestamp")
        now = int(time.time() * 1000)
        if timestamp < now - 30 * 60 * 1000 or timestamp > now + 60 * 1000:
            return self.rejected("timestamp_out_of_window")
        row = self.db.execute(
            """SELECT recipient,task_id,record,card,last_ms,last_event
            FROM pipeline_card_views WHERE message_id=? AND binding=?""",
            (event["message_id"], self.binding),
        ).fetchone()
        if not row:
            return self.rejected("unregistered_message")
        recipient, tid, record, payload, last_ms, last_event = row
        chat = self.binding if recipient == "self" else recipient
        if event["chat_id"] != chat:
            return self.rejected("chat_mismatch")
        if timestamp <= last_ms or event["event_id"] == last_event:
            return self.rejected("duplicate_or_stale")
        if (
            recipient == "self"
            and event["operator_id"] != self.cfg["FEISHU_OWNER_OPEN_ID"]
        ):
            return self.rejected("operator_mismatch")
        task = self.db.execute(
            "SELECT state,config FROM pipeline_tasks WHERE task_id=? AND binding=?",
            (tid, self.binding),
        ).fetchone()
        if not task or task[0] in ("deleted", "deleting"):
            return self.rejected("task_unavailable")
        config = json.loads(task[1])
        if recipient != "self" and recipient not in config.get("recipients", []):
            return self.rejected("recipient_removed")
        record, payload = json.loads(record), json.loads(payload)
        record["timezone"] = get_timezone(self.cfg).key
        if event["option"] not in {c["value"] for c in job_choices(record)[:40]}:
            return self.rejected("unknown_option")
        # card_content is optional CLI enrichment and may be empty when its
        # message fetch fails. Our registered delivered snapshot is authoritative;
        # never execute or trust arbitrary callback card fields.
        updated = replace_job_section(payload, record, event["option"])
        loading = replace_job_section(payload, record, event["option"], loading=True)
        with self.db:
            self.db.execute(
                "UPDATE pipeline_card_views SET last_ms=?,last_event=? WHERE message_id=? AND binding=?",
                (timestamp, event["event_id"], event["message_id"], self.binding),
            )
        # Each callback token permits two updates: clear stale data, then show the
        # selected view. Keep the selector enabled so an interrupted update can
        # be recovered with a new interaction, without persisting/reusing tokens.
        try:
            await self.update_card(event, loading)
            logging.info("Pipeline Job chart loading state displayed")
        except (ValueError, RuntimeError, OSError, asyncio.TimeoutError) as error:
            logging.warning(
                "Pipeline Job loading state not confirmed (%s)", type(error).__name__
            )
        # Even when clearing fails, use the remaining attempt for the actual data.
        await self.update_card(event, updated)
        self.cfg.setdefault("_runtime", {})["pipeline_card_last_update"] = time.time()
        return True

    async def update_card(self, event, payload):
        # Token stays in memory/stdin, never in argv, logs, DB or diagnostics.
        result = json.loads(
            await self.run(
                [
                    self.cfg["LARK_CLI_BIN"],
                    "api",
                    "POST",
                    "/open-apis/interactive/v1/card/update",
                    "--as",
                    "bot",
                    "--data",
                    "-",
                    "--format",
                    "json",
                ],
                20,
                input_text=json.dumps(
                    dict(token=event["token"], card=payload), ensure_ascii=False
                ),
            )
        )
        if result.get("ok") is not True:
            raise RuntimeError("Pipeline card update not confirmed")
        with self.db:
            self.db.execute(
                "UPDATE pipeline_card_views SET card=? WHERE message_id=? AND binding=?",
                (
                    json.dumps(payload, ensure_ascii=False),
                    event["message_id"],
                    self.binding,
                ),
            )

    async def listen(self, root):
        from assistant.service import terminate

        runtime = self.cfg.setdefault("_runtime", {})
        while True:
            process, diagnostics = None, None
            runtime["pipeline_card_listener"] = "连接中"
            ready = asyncio.Event()
            try:
                process = await asyncio.create_subprocess_exec(
                    self.cfg["LARK_CLI_BIN"],
                    "event",
                    "consume",
                    "card.action.trigger",
                    "--as",
                    "bot",
                    cwd=Path(root),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                    limit=1024 * 1024,
                )

                async def stderr():
                    async for raw in process.stderr:
                        line = raw.decode(errors="replace")
                        if "[event] ready event_key=card.action.trigger" in line:
                            runtime["pipeline_card_listener"] = (
                                "监听就绪（后台回调开关需另行确认）"
                            )
                            ready.set()
                            logging.info("Pipeline card callback listener ready")
                        elif (
                            "disconnected" in line.casefold()
                            or "reconnecting" in line.casefold()
                        ):
                            runtime["pipeline_card_listener"] = "重连中"
                    # No raw diagnostics: callbacks can include temporary update tokens.

                diagnostics = asyncio.create_task(stderr())
                await asyncio.wait_for(ready.wait(), 30)
                async for raw in process.stdout:
                    try:
                        event = json.loads(raw)
                        if isinstance(event, dict):
                            try:
                                value = json.loads(event.get("action_value") or "{}")
                            except (TypeError, ValueError):
                                value = {}
                            logging.info(
                                "Card callback shape: %s",
                                json.dumps(
                                    {
                                        "type_ok": event.get("type")
                                        == "card.action.trigger",
                                        "host_ok": event.get("host") == "im_message",
                                        "select": event.get("action_tag")
                                        == "select_static",
                                        "name_ok": event.get("action_name")
                                        == "pipeline_job_stage",
                                        "name_missing": not event.get("action_name"),
                                        "marker_ok": isinstance(value, dict)
                                        and value.get("action") == "pipeline_job_stage",
                                        "message_present": bool(
                                            event.get("message_id")
                                        ),
                                        "chat_present": bool(event.get("chat_id")),
                                        "operator_present": bool(
                                            event.get("operator_id")
                                        ),
                                        "event_present": bool(event.get("event_id")),
                                        "timestamp_present": bool(
                                            event.get("timestamp")
                                        ),
                                        "token_present": bool(event.get("token")),
                                        "timestamp_digits": len(
                                            str(event.get("timestamp", ""))
                                        ),
                                        "version_ok": isinstance(value, dict)
                                        and type(value.get("version")) is int
                                        and value["version"] == 1,
                                        "card_present": bool(event.get("card_content")),
                                        "option_present": bool(event.get("option")),
                                        "form_present": bool(event.get("form_value")),
                                    },
                                    sort_keys=True,
                                ),
                            )
                        if await self.handle(event):
                            logging.info("Pipeline Job chart updated in place")
                    except (
                        ValueError,
                        TypeError,
                        KeyError,
                        RuntimeError,
                        OSError,
                        asyncio.TimeoutError,
                    ) as error:
                        runtime["pipeline_card_error"] = "卡片更新失败，请重新选择阶段"
                        logging.warning(
                            "Pipeline card callback failed (%s)", type(error).__name__
                        )
                await process.wait()
            except (OSError, asyncio.TimeoutError) as error:
                logging.warning(
                    "Pipeline card listener unavailable (%s)", type(error).__name__
                )
            finally:
                if diagnostics:
                    diagnostics.cancel()
                    await asyncio.gather(diagnostics, return_exceptions=True)
                if process:
                    await terminate(process)
                runtime["pipeline_card_listener"] = "已断开"
            await asyncio.sleep(5)
