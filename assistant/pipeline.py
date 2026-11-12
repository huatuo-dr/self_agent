"""Persistent Pipeline monitoring, commands and owner-confirmed configuration."""

import asyncio
import fcntl
import hashlib
import json
import logging
import re
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from assistant.settings import get_timezone, message_signature
from assistant.http_errors import GitCommandError, PublicationBlocked
from assistant.pipeline_card import card
from assistant.pipeline_gitlab import (
    GitLab,
    GitLabAPIError,
    GitLabRateLimit,
    alerts,
    execution,
    iso,
)
from assistant.sessions import Sessions

PIPELINE_WORK_SECONDS = 240

DEFAULTS = dict(
    interval_minutes=10,
    monitor_failed=True,
    branches=["default"],
    include_tags=True,
    history="7d",
    recipients=[],
    thresholds=dict(
        queue_seconds=600,
        execution_seconds=1800,
        slow_ratio=0.5,
        slow_seconds=300,
        min_samples=10,
    ),
)
FORM = (
    "请按以下模板填写；字段名称请照抄，可分多次补充，未填写项使用默认值。\n"
    "只修改冒号后的值，不要把说明文字放进字段值。\n\n"
    "仓库链接：\n监控间隔（分钟）：10\n是否监控失败任务：是\n"
    "监控分支：默认分支\n是否包含 Tag Pipeline：是\n"
    "首次历史补采：近 7 天\n汇报对象：自己\n\n"
    "填写规则：\n"
    "• 仓库链接：填写项目的 HTTPS 或 SSH 地址，不填 Pipeline、MR 或文件链接。\n"
    "• 监控间隔（分钟）：1～1440 的整数；是/否选项只填“是”或“否”。\n"
    "• 监控分支：填“默认分支”“全部”或实际分支名；多个分支用逗号分隔，例如：develop,release/0.6.0。不要加入“和”“两个分支”等说明文字。\n"
    "• 首次历史补采：填“不补采”“近 7 天”或“YYYY-MM-DD 至 YYYY-MM-DD”，例如：2026-11-02 至 2026-11-05（含结束日，配置时区，不晚于今天）。日期用横线，不用点号或“从11.2号开始”。字段名称仍为“首次历史补采”。\n"
    "• 汇报对象：填“自己”或 oc_ 开头的飞书会话 ID；多个 ID 用逗号分隔，最多 20 个。\n"
    "默认告警：Job 排队 >10 分钟；有效执行 >30 分钟；近 7 天至少 10 个可比样本，变慢 ≥50% 且 ≥5 分钟。"
)


def dump(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


@contextmanager
def file_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("锁文件不能为符号链接")
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def parse_fields(text, base, cfg=None):
    c, changed = deepcopy(base), False
    aliases = {
        "仓库链接": "url",
        "监控间隔（分钟）": "interval_minutes",
        "监控间隔": "interval_minutes",
        "是否监控失败任务": "monitor_failed",
        "监控分支": "branches",
        "是否包含 Tag Pipeline": "include_tags",
        "首次历史补采": "history",
        "汇报对象": "recipients",
        "排队告警（分钟）": "queue_seconds",
        "执行告警（分钟）": "execution_seconds",
        "变慢比例（%）": "slow_ratio",
        "变慢差值（分钟）": "slow_seconds",
        "最少样本数": "min_samples",
    }
    for line in text.splitlines():
        match = re.fullmatch(
            r"\s*(?:修改|把|将)?\s*(.*?)\s*(?:：|:|改为|改成|设为)\s*(.+?)\s*", line
        )
        if not match or match[1] not in aliases:
            continue
        key, value = aliases[match[1]], match[2]
        changed = True
        if key in ("monitor_failed", "include_tags"):
            if value not in ("是", "否"):
                raise ValueError("是/否选项请明确填写“是”或“否”。")
            c[key] = value == "是"
        elif key == "interval_minutes":
            value = value.removesuffix("分钟").strip()
            if not value.isdecimal() or not 1 <= int(value) <= 1440:
                raise ValueError("监控间隔须为 1～1440 分钟整数。")
            c[key] = int(value)
        elif key == "branches":
            c[key] = (
                ["default"]
                if value == "默认分支"
                else ["all"]
                if value == "全部"
                else re.split(r"[,，、\s]+", value)
            )
            if any(not b or len(b) > 255 or b.startswith("-") for b in c[key]):
                raise ValueError("分支名无效")
        elif key == "recipients":
            c[key] = (
                []
                if value in ("自己", "默认", "我")
                else list(dict.fromkeys(re.split(r"[,，、\s]+", value)))
            )
            if len(c[key]) > 20 or any(
                not re.fullmatch(r"oc_[A-Za-z0-9_]+", v) for v in c[key]
            ):
                raise ValueError("汇报对象须为飞书会话 ID（oc_），最多 20 个。")
        elif key == "history":
            if value in ("不补采", "近 7 天", "近7天"):
                c[key] = "none" if value == "不补采" else "7d"
            else:
                match = re.fullmatch(
                    r"(\d{4}-\d\d-\d\d)\s*(?:至|到)\s*(\d{4}-\d\d-\d\d)", value
                )
                if not match:
                    raise ValueError(
                        "补采范围格式：YYYY-MM-DD 至 YYYY-MM-DD（含结束日，配置时区）。"
                    )
                a, b = [
                    datetime.fromisoformat(v)
                    .replace(tzinfo=get_timezone(cfg))
                    .timestamp()
                    for v in match.groups()
                ]
                if a > b or b > time.time():
                    raise ValueError("补采起止日期无效或晚于今天。")
                c[key] = dict(start=a, end=(datetime.fromtimestamp(b, get_timezone(cfg)) + timedelta(days=1)).timestamp())
        elif key in c["thresholds"]:
            try:
                n = float(value.removesuffix("分钟").removesuffix("%"))
                if not 0 < n < 100000 or (key == "min_samples" and not n.is_integer()):
                    raise ValueError()
            except ValueError:
                raise ValueError("告警阈值须为有效正数；最少样本数须为整数。") from None
            c["thresholds"][key] = (
                int(n)
                if key == "min_samples"
                else n / 100
                if key == "slow_ratio"
                else n * 60
            )
        else:
            c[key] = value
    return c, changed


class PipelineMonitor:
    def __init__(self, cfg, db, run, root):
        self.cfg, self.db, self.run, self.root = cfg, db, run, Path(root)
        self.delivery_lock = asyncio.Lock()
        from assistant.pipeline_interaction import PipelineInteractions

        self.interactions = PipelineInteractions(cfg, db, run)
        self.binding = cfg["FEISHU_OWNER_CHAT_ID"]
        self.gitlab = GitLab(cfg, run)
        from assistant.pipeline_config import pages_target

        self.pages_target = pages_target(cfg) if cfg.get("PIPELINE_PAGES_REPO_URL") else None
        if cfg.get("PIPELINE_PUBLISH_ENABLED") and self.pages_target is None:
            raise ValueError("PIPELINE_PAGES_REPO_URL is required for publication")
        self.pages_gitlab = (GitLab(dict(cfg, GITLAB_HOST=self.pages_target.host), run)
                             if self.pages_target else None)
        self.sessions = Sessions(db, self.binding)
        db.executescript("""
          CREATE TABLE IF NOT EXISTS pipeline_tasks (
            number INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT UNIQUE NOT NULL,
            binding TEXT NOT NULL, session_id INTEGER NOT NULL, state TEXT NOT NULL,
            config TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
            created REAL NOT NULL, next_scan REAL NOT NULL, data TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS pipeline_due ON pipeline_tasks(binding,state,next_scan);
          CREATE TABLE IF NOT EXISTS pipeline_drafts (
            binding TEXT NOT NULL, session_id INTEGER NOT NULL, source TEXT NOT NULL,
            kind TEXT NOT NULL, config TEXT NOT NULL, number INTEGER, ready INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(binding,session_id));
          CREATE TABLE IF NOT EXISTS pipeline_records (
            task_id TEXT NOT NULL, execution_id TEXT NOT NULL, pipeline_id INTEGER NOT NULL,
            finished REAL NOT NULL, body TEXT NOT NULL, PRIMARY KEY(task_id,execution_id));
          CREATE TABLE IF NOT EXISTS pipeline_observations (
            task_id TEXT NOT NULL, pipeline_id INTEGER NOT NULL, body TEXT NOT NULL,
            PRIMARY KEY(task_id,pipeline_id));
          CREATE TABLE IF NOT EXISTS pipeline_gaps (
            task_id TEXT NOT NULL, pipeline_id INTEGER NOT NULL, body TEXT NOT NULL,
            PRIMARY KEY(task_id,pipeline_id));
          CREATE TABLE IF NOT EXISTS pipeline_deliveries (
            key TEXT PRIMARY KEY, task_id TEXT NOT NULL, execution_id TEXT NOT NULL,
            recipient TEXT NOT NULL, state TEXT NOT NULL, body TEXT NOT NULL,
            updated REAL NOT NULL, result TEXT NOT NULL DEFAULT '');
          CREATE TABLE IF NOT EXISTS pipeline_commands (
            binding TEXT NOT NULL, source TEXT NOT NULL, reply TEXT NOT NULL, session_id INTEGER,
            PRIMARY KEY(binding,source));
          CREATE TABLE IF NOT EXISTS pipeline_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS pipeline_remote (
            number INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT UNIQUE NOT NULL, body TEXT NOT NULL);
        """)
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS pipeline_task_session ON pipeline_tasks(binding,session_id)"
        )
        db.commit()
        if not self.meta("instance"):
            self.set_meta("instance", uuid.uuid4().hex)
        backoff = self.meta("gitlab_backoff", {})
        for client in filter(None, (self.gitlab, self.pages_gitlab)):
            client.not_before = backoff.get(client.host, 0)

    def meta(self, key, default=None):
        row = self.db.execute(
            "SELECT value FROM pipeline_meta WHERE key=?", (self.binding + ":" + key,)
        ).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        with self.db:
            self.db.execute(
                "INSERT INTO pipeline_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (self.binding + ":" + key, dump(value)),
            )

    def recover(self):
        with self.db:
            self.db.execute(
                "UPDATE pipeline_deliveries SET state='uncertain',result='服务中断，需核实投递结果' "
                "WHERE state='sending' AND task_id IN (SELECT task_id FROM pipeline_tasks WHERE binding=?)",
                (self.binding,),
            )

    def tasks(self):
        rows = self.db.execute(
            "SELECT number FROM pipeline_tasks WHERE binding=? ORDER BY number",
            (self.binding,),
        ).fetchall()
        return [self.get(r[0]) for r in rows]

    def get(self, number):
        row = self.db.execute(
            "SELECT number,task_id,session_id,state,config,version,created,next_scan,data FROM pipeline_tasks "
            "WHERE number=? AND binding=?",
            (number, self.binding),
        ).fetchone()
        if not row:
            return None
        result = dict(
            zip(
                (
                    "number",
                    "task_id",
                    "session_id",
                    "state",
                    "config",
                    "version",
                    "created",
                    "next_scan",
                    "data",
                ),
                row,
            )
        )
        result["config"] = json.loads(result["config"])
        result["data"] = json.loads(result["data"])
        return result

    def save(self, task):
        with self.db:
            self.db.execute(
                "UPDATE pipeline_tasks SET state=?,config=?,version=?,next_scan=?,data=? WHERE number=? AND binding=?",
                (
                    task["state"],
                    dump(task["config"]),
                    task["version"],
                    task["next_scan"],
                    dump(task["data"]),
                    task["number"],
                    self.binding,
                ),
            )

    def records(self, tid):
        return [
            json.loads(r[0])
            for r in self.db.execute(
                "SELECT body FROM pipeline_records WHERE task_id=? ORDER BY finished,execution_id",
                (tid,),
            )
        ]

    def changed(self, task):
        task["data"]["revision"] = task["data"].get("revision", 0) + 1
        task["data"]["sync"] = "待发布"
        self.save(task)

    def remember(self, source, reply, sid):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO pipeline_commands VALUES(?,?,?,?)",
                (self.binding, source, reply, sid),
            )
        return reply, sid

    async def begin(self, source):
        sid = self.sessions.create(source, "Pipeline 监控创建")
        self.db.commit()
        if not self.sessions.thread(sid):
            if self.meta("root:" + source):
                raise ValueError("话题根消息发送结果未确认，请先核实，不自动重复创建。")
            self.set_meta("root:" + source, "sending")
            # Always create a new root, even when /pipeline creat is sent inside another topic.
            result = json.loads(
                await self.run(
                    [
                        self.cfg["LARK_CLI_BIN"],
                        "im",
                        "+messages-send",
                        "--as",
                        "bot",
                        "--chat-id",
                        self.binding,
                        "--text",
                        "Pipeline 监控创建 · 请在此话题填写配置",
                        "--idempotency-key",
                        hashlib.sha256(
                            ("pipeline-root:" + source).encode()
                        ).hexdigest()[:40],
                    ],
                    30,
                )
            )
            from assistant.service import sent_message_id

            mid = sent_message_id(result)
            if result.get("ok") is not True or not mid:
                raise RuntimeError("创建监控话题未确认")
            self.sessions.bind(sid, mid)
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO pipeline_drafts VALUES(?,?,?,?,?,?,0)",
                (self.binding, sid, source, "create", dump(DEFAULTS), None),
            )
        return self.remember(source, FORM, sid)

    async def validate(self, c):
        if not c.get("url"):
            raise ValueError("请补充仓库链接。")
        project = await self.gitlab.project(c["url"])
        c.update(
            project_id=project["id"],
            project_path=project["path_with_namespace"],
            url=project["web_url"],
            host=self.gitlab.host,
        )
        for branch in c["branches"]:
            if branch not in ("all", "default"):
                from urllib.parse import quote

                try:
                    await self.gitlab.api(
                        f"projects/{project['id']}/repository/branches/{quote(branch, safe='')}"
                    )
                except GitLabRateLimit:
                    raise
                except asyncio.TimeoutError:
                    raise ValueError(f"分支“{branch}”校验超时，请稍后重试。") from None
                except (RuntimeError, OSError):
                    raise ValueError(
                        f"分支校验失败：无法读取 GitLab 分支“{branch}”。请确认分支名、读取权限和网络。\n"
                        "多个分支请用逗号分隔，例如：监控分支：develop,release/0.6.0；不要加入“和”“两个分支”等说明文字。"
                    ) from None
        c["recipient_names"] = {}
        c["recipient_modes"] = {}
        if c["recipients"]:
            await self.verify_identity("user")
            for recipient in c["recipients"]:
                result = json.loads(
                    await self.run(
                        [
                            self.cfg["LARK_CLI_BIN"],
                            "im",
                            "chats",
                            "get",
                            "--as",
                            "user",
                            "--params",
                            dump(dict(chat_id=recipient)),
                        ],
                        30,
                    )
                )
                data = result.get("data", {})
                if result.get("ok") is not True or not data.get("name"):
                    raise ValueError("无法核实汇报会话名称或可访问性。")
                mode = data.get("chat_mode")
                if mode not in ("p2p", "group", "topic"):
                    raise ValueError("无法核实会话是单聊还是群聊。")
                c["recipient_modes"][recipient] = mode
                c["recipient_names"][recipient] = (
                    re.split(r"[（(]", data["name"], maxsplit=1)[0].strip()
                    or data["name"]
                )
        return c

    @staticmethod
    def recipient_name(config, recipient):
        if config.get("recipient_modes", {}).get(recipient) in ("group", "topic"):
            return "各位"
        name = config.get("recipient_names", {}).get(recipient, "各位")
        return (
            config.get("recipient_salutations", {}).get(recipient)
            or re.split(r"[（(]", name, maxsplit=1)[0].strip()
        )

    async def verify_identity(self, identity):
        info = json.loads(
            await self.run(
                [self.cfg["LARK_CLI_BIN"], "auth", "status", "--json", "--verify"], 20
            )
        )
        ident = info.get("identities", {}).get(identity, {})
        if info.get("appId") != self.cfg["FEISHU_APP_ID"]:
            raise ValueError("飞书 CLI 应用与主人绑定不一致")
        if identity == "user":
            if (
                ident.get("openId") != self.cfg["FEISHU_OWNER_OPEN_ID"]
                or ident.get("tokenStatus") != "valid"
                or not {"im:message", "im:message.send_as_user"}
                <= set(ident.get("scope", "").split())
            ):
                raise ValueError("本人飞书身份或发送权限未核实")
        elif not ident.get("verified"):
            raise ValueError("bot 身份未核实")

    def preview(self, c, kind, old=None):
        names = c.get("recipient_names", {})
        recipients = (
            "；".join(f"{names.get(v, v)}（{v}，本人身份）" for v in c["recipients"])
            or "自己（bot，当前话题）"
        )
        result = (
            f"请审阅{'修改' if kind == 'edit' else '创建'}配置：\n仓库：{c['url']}\n间隔：{c['interval_minutes']} 分钟\n"
            f"监控失败：{'是' if c['monitor_failed'] else '否'}；Tag：{'是' if c['include_tags'] else '否'}\n"
            f"分支：{', '.join(c['branches'])}\n首次补采：{dump(c['history'])}\n汇报：{recipients}\n"
            f"告警阈值（秒/比例/样本数）：{dump(c['thresholds'])}\n"
            "配置时区；历史持续保留；逐次完成发卡片，补采只通知报告链接；发布繁忙时合并下一批。"
        )
        if old:
            result += "\n变更：\n" + "\n".join(
                f"{k}：{dump(old.get(k))} → {dump(v)}"
                for k, v in c.items()
                if old.get(k) != v and k != "recipient_names"
            )
        return (
            result
            + f"\n确认后立即运行。请在本话题回复“{'确认修改' if kind == 'edit' else '确认创建'}”；取消请回复“取消”。"
        )

    async def command(self, argument, source, sid=None):
        cached = self.db.execute(
            "SELECT reply,session_id FROM pipeline_commands WHERE binding=? AND source=?",
            (self.binding, source),
        ).fetchone()
        if cached:
            return cached
        parts = argument.split()
        verb = parts[0].lower() if parts else "list"
        if verb in ("creat", "create") and len(parts) == 1:
            return await self.begin(source)
        if verb == "list":
            from assistant.pipeline_pages import Publisher

            try:
                await Publisher(self).refresh()
            except (RuntimeError, ValueError, OSError, asyncio.TimeoutError):
                pass
            rows = [
                f"#{t['number']} · {t['config'].get('project_path', t['task_id'])} · 本机 {t['state']} / {t['data'].get('sync', '待同步')}"
                for t in self.tasks()
                if t["state"] != "deleted"
            ]
            local = {t["task_id"] for t in self.tasks()}
            for n, tid, body in self.db.execute(
                "SELECT number,task_id,body FROM pipeline_remote ORDER BY number"
            ):
                if tid not in local:
                    r = json.loads(body)
                    if r.get("state") == "远端已移除":
                        continue
                    rows.append(
                        f"外部 E{n} · {r.get('project_path', tid)} · 仅可查看 / {r.get('state', '未知')}"
                    )
            page = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 1
            reply = "\n".join(rows[(page - 1) * 10 : page * 10]) or "没有监控任务。"
            reply += f"\n第 {page} 页；共 {len(rows)} 项。远端缓存时间：{self.meta('remote_at', '尚未同步')}；同步：{self.meta('remote_status', '未知')}。"
            return self.remember(source, reply, sid)
        if len(parts) > 2 or verb not in ("status", "start", "stop", "delete"):
            return self.remember(
                source,
                "用法：/pipeline creat|list [页码]|status|start|stop|delete [编号]",
                sid,
            )
        token = parts[1].lstrip("#") if len(parts) == 2 else ""
        if token.startswith("E") and token[1:].isdigit():
            row = self.db.execute(
                "SELECT body FROM pipeline_remote WHERE number=?", (int(token[1:]),)
            ).fetchone()
            return self.remember(
                source,
                ("其他来源，只读；不自动接管。\n" + row[0])
                if row
                else "未找到共享任务。",
                sid,
            )
        candidates = [
            t
            for t in self.tasks()
            if t["session_id"] == sid and t["state"] != "deleted"
        ]
        task = (
            self.get(int(token))
            if token.isdecimal()
            else candidates[0]
            if not token and len(candidates) == 1
            else None
        )
        if not task:
            return self.remember(source, "请指定有效本机监控编号。", sid)
        if verb == "status":
            deliveries = self.db.execute(
                "SELECT state,count(*) FROM pipeline_deliveries WHERE task_id=? GROUP BY state",
                (task["task_id"],),
            ).fetchall()
            return self.remember(
                source,
                f"监控 #{task['number']}：{task['state']}\n配置：{dump(task['config'])}\n"
                f"记录数：{len(self.records(task['task_id']))}；采集缺口：{self.db.execute('SELECT count(*) FROM pipeline_gaps WHERE task_id=?', (task['task_id'],)).fetchone()[0]}\n下次扫描：{iso(task['next_scan'])}\n"
                f"发布与采集：{dump(task['data'])}\n投递：{dump(deliveries)}\n缺口（最多 20 条）：{dump(self.db.execute('SELECT pipeline_id,body FROM pipeline_gaps WHERE task_id=? LIMIT 20', (task['task_id'],)).fetchall())}\n共享 CI/生成器变更受阻时，在本话题回复“审查发布变更”获取具体版本及确认指令。",
                sid,
            )
        if task["state"] in ("deleted", "deleting"):
            return self.remember(source, "任务已删除或正在下架，不能重新启动。", sid)
        if verb == "delete":
            target = task["session_id"]
            if sid != target:
                return self.remember(
                    source,
                    f"请进入任务 session #{target} 后执行 /pipeline delete {task['number']} 并确认。",
                    sid,
                )
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO pipeline_drafts VALUES(?,?,?,?,?,?,1)",
                    (
                        self.binding,
                        sid,
                        source,
                        "delete",
                        dump(task["config"]),
                        task["number"],
                    ),
                )
            return self.remember(
                source,
                f"将停止 #{task['number']}，删除本地历史、报告仓库当前数据及在线报告；不重写 Git 历史、不撤回消息。\n请回复“确认删除”。",
                sid,
            )
        if verb == "start":
            self.set_meta("push_failures", 0)
            self.set_meta("retry_at", 0)
        if verb == "stop":
            task["state"] = "paused"
            task["data"].pop("cycle", None)
            task["data"]["stopped_at"] = time.time()
            with self.db:
                self.db.execute(
                    "UPDATE pipeline_deliveries SET state='suppressed' WHERE task_id=? AND state='pending'",
                    (task["task_id"],),
                )
        elif task["state"] == "paused":
            task["state"] = "active"
            task["next_scan"] = time.time()
            task["data"]["notify_after"] = time.time()
        self.changed(task)
        return self.remember(
            source,
            f"#{task['number']} 已{'暂停' if verb == 'stop' else '启动'}；报告状态异步同步。",
            sid,
        )

    async def text(self, text, source, sid):
        if sid is None:
            return None
        cached = self.db.execute(
            "SELECT reply FROM pipeline_commands WHERE binding=? AND source=?",
            (self.binding, source),
        ).fetchone()
        if cached:
            return cached[0]
        if text.strip() == "审查发布变更" or text.strip().startswith("确认发布变更 "):
            from assistant.pipeline_pages import Publisher

            answer = await Publisher(self).review_shared(text.strip(), source, sid)
            return self.remember(source, answer, sid)[0]
        row = self.db.execute(
            "SELECT source,kind,config,number,ready FROM pipeline_drafts WHERE binding=? AND session_id=?",
            (self.binding, sid),
        ).fetchone()
        tasks = [
            t
            for t in self.tasks()
            if t["session_id"] == sid and t["state"] not in ("deleted", "deleting")
        ]
        if not row and len(tasks) != 1:
            return None
        old = tasks[0]["config"] if tasks else None
        kind = row[1] if row else "edit"
        c = json.loads(row[2]) if row else deepcopy(old)
        tid = row[3] if row else tasks[0]["number"]
        answer = None
        if text.strip() == "取消":
            with self.db:
                self.db.execute(
                    "DELETE FROM pipeline_drafts WHERE binding=? AND session_id=?",
                    (self.binding, sid),
                )
            answer = "已取消本次配置确认；已有监控状态不变。"
        elif re.fullmatch(
            r"(确认(?:创建|修改|删除)?|同意|是|好的|可以|确认独立创建)[。！!]*",
            text.strip(),
        ):
            delivered = (
                self.db.execute(
                    "SELECT 1 FROM inbox WHERE id=? AND binding=? AND state='done'",
                    (row[0], self.binding),
                ).fetchone()
                if row
                else None
            )
            if not row or not row[4] or not delivered:
                answer = "尚无已送达且校验通过的配置，请先填写并审阅配置。"
            elif (
                text.strip().rstrip("。！!")
                != {
                    "create": "确认独立创建" if row[4] == 2 else "确认创建",
                    "edit": "确认修改",
                    "delete": "确认删除",
                }[kind]
            ):
                answer = "确认文字与当前操作不匹配，请按最新预览中的完整确认指令回复。"
            elif kind == "delete":
                task = self.get(tid)
                task["state"] = "deleting"
                task["data"].pop("cycle", None)
                with self.db:
                    self.db.execute(
                        "UPDATE pipeline_deliveries SET state='suppressed' WHERE task_id=? AND state='pending'",
                        (task["task_id"],),
                    )
                    self.db.execute(
                        "DELETE FROM pipeline_drafts WHERE binding=? AND session_id=?",
                        (self.binding, sid),
                    )
                self.changed(task)
                from assistant.pipeline_pages import Publisher

                if Publisher(self).finish_local_delete(task):
                    answer = "已删除任务及本地历史；该任务从未尝试发布，无需远端下架。"
                else:
                    answer = "已停止任务，历史报告待下架；下架核实前显示“删除中”。发布关闭时须恢复发布才能完成远端下架。"
            else:
                c = await self.validate(c)
                duplicates = [
                    t
                    for t in self.tasks()
                    if t["state"] not in ("deleted", "deleting")
                    and t["number"] != tid
                    and all(
                        t["config"].get(k) == c.get(k)
                        for k in ("project_id", "branches", "include_tags")
                    )
                ]
                if (
                    duplicates
                    and text.strip().rstrip("。！!") != "确认独立创建"
                    and kind == "create"
                ):
                    with self.db:
                        self.db.execute(
                            "UPDATE pipeline_drafts SET source=?,ready=2 WHERE binding=? AND session_id=?",
                            (source, self.binding, sid),
                        )
                    answer = f"已有相同范围的监控 #{duplicates[0]['number']}，建议复用。确需独立任务请回复“确认独立创建”。"
                else:
                    now = time.time()
                    if kind == "create":
                        history = c["history"]
                        since = (
                            now - 7 * 86400
                            if history == "7d"
                            else now
                            if history == "none"
                            else history["start"]
                        )
                        data = dict(
                            cursor=since,
                            notify_after=now,
                            initial=True,
                            revision=1,
                            sync="待发布",
                            publication_state="never",
                        )
                        with self.db:
                            tid = self.db.execute(
                                "INSERT INTO pipeline_tasks(task_id,binding,session_id,state,config,created,next_scan,data) "
                                "VALUES(?,?,?,?,?,?,?,?)",
                                (
                                    uuid.uuid4().hex,
                                    self.binding,
                                    sid,
                                    "active",
                                    dump(c),
                                    now,
                                    now,
                                    dump(data),
                                ),
                            ).lastrowid
                    else:
                        task = self.get(tid)
                        if not task or task["state"] not in ("active", "paused"):
                            raise ValueError("任务状态不允许修改")
                        if c["project_id"] != task["config"]["project_id"]:
                            raise ValueError(
                                "变更仓库请另建任务；当前任务可修改范围、间隔和收件人。"
                            )
                        task["config"] = c
                        task["version"] += 1
                        self.changed(task)
                    with self.db:
                        self.db.execute(
                            "DELETE FROM pipeline_drafts WHERE binding=? AND session_id=?",
                            (self.binding, sid),
                        )
                    answer = f"监控 #{tid} 已{'创建并启动' if kind == 'create' else '修改'}。采集不等待报告发布。"
        else:
            c, changed = parse_fields(text, c, self.cfg)
            if not changed:
                return (
                    "未识别到可填写的配置字段。请照抄模板中的字段名，例如“首次历史补采”，然后在冒号后填写值。\n\n"
                    + FORM
                    if row and kind == "create"
                    else None
                )
            if kind == "delete":
                kind = "edit"
            ready = 0
            try:
                c = await self.validate(c)
                ready = 1
                answer = self.preview(c, kind, old if kind == "edit" else None)
            except (ValueError, RuntimeError, OSError, asyncio.TimeoutError) as error:
                self.defer_gitlab(error)
                answer = (
                    (
                        str(error)
                        if isinstance(error, (ValueError, GitLabAPIError))
                        else "远端校验失败，请稍后重新提交配置。"
                    )
                    + "\n"
                    + FORM
                )
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO pipeline_drafts VALUES(?,?,?,?,?,?,?)",
                    (self.binding, sid, source, kind, dump(c), tid, ready),
                )
        return self.remember(source, answer, sid)[0]

    def queue(self, task, eid, body, recipients=None, commit=True):
        for recipient in (
            recipients
            if recipients is not None
            else task["config"]["recipients"] or ["self"]
        ):
            key = hashlib.sha256(
                f"{task['task_id']}:{eid}:{recipient}".encode()
            ).hexdigest()[:40]
            self.db.execute(
                "INSERT OR IGNORE INTO pipeline_deliveries VALUES(?,?,?,?,?,?,?,?)",
                (
                    key,
                    task["task_id"],
                    eid,
                    recipient,
                    "pending",
                    dump(body),
                    time.time(),
                    "",
                ),
            )
        if commit:
            self.db.commit()

    def defer_gitlab(self, error):
        if isinstance(error, GitLabRateLimit):
            backoff = self.meta("gitlab_backoff", {})
            backoff[error.host] = max(backoff.get(error.host, 0), error.retry_at)
            self.set_meta("gitlab_backoff", backoff)
            for client in filter(None, (self.gitlab, self.pages_gitlab)):
                if client.host == error.host:
                    client.not_before = backoff[error.host]

    def issue(self, task, area, error):
        # Only explicitly safe diagnostics supply detail; never expose raw errors.
        self.defer_gitlab(error)
        detail = (
            str(error)
            if isinstance(error, (GitLabAPIError, GitCommandError, PublicationBlocked))
            else type(error).__name__
        )
        message = f"{area}：{detail}；请用 /pipeline status {task['number']} 核查。"
        task = self.get(task["number"])
        # Preserve ownership before another stage replaces a legacy error text.
        previous_api = task["data"].get("api_error")
        if previous_api is not None:
            previous_api.setdefault(
                "area", task["data"].get("error", "").partition("：")[0]
            )
        if isinstance(error, GitLabAPIError):
            task["data"]["api_error"] = dict(
                status=error.status, endpoint=error.endpoint, area=area
            )
            if isinstance(error, GitLabRateLimit):
                task["data"]["api_error"]["retry_at"] = error.retry_at
        changed = task["data"].get("error") != message
        task["data"]["error"] = message
        self.save(task)
        if changed:
            self.queue(
                task,
                "error:" + hashlib.sha256(message.encode()).hexdigest()[:12],
                dict(markdown=message),
                ["self"],
            )

    @staticmethod
    def clear_issue(task, *areas):
        """Clear only recovered stages; the caller persists the updated task."""
        data = task["data"]
        area = data.get("error", "").partition("：")[0]
        api = data.get("api_error")
        if api is not None and api.get("area", area) in areas:
            data.pop("api_error", None)
        if area in areas:
            data.pop("error", None)

    async def scan(self, task, now):
        with file_lock(
            self.root / ".local" / f"pipeline-{task['task_id']}.lock"
        ) as acquired:
            if not acquired:
                return
            task = self.get(task["number"])
            if not task or task["state"] != "active":
                return
            tid = task["task_id"]
            c = task["config"]
            data = task["data"]
            cycle = data.get("cycle") or dict(start=data["cursor"], end=now, page=1)
            data["cycle"] = cycle
            self.save(task)
            # Fixed upper watermark and a verification pass prevent offset pagination shifts from losing rows.
            pages = 0
            while pages < 5:
                batch = await self.gitlab.page(
                    f"projects/{c['project_id']}/pipelines",
                    cycle["page"],
                    updated_after=iso(max(0, cycle["start"] - 120)),
                    updated_before=iso(cycle["end"]),
                    order_by="updated_at",
                    sort="asc",
                )
                fingerprint = hashlib.sha256(dump(batch).encode()).hexdigest()
                fingerprints = cycle.setdefault("fingerprints", {})
                if fingerprints.get(str(cycle["page"])) != fingerprint:
                    cycle.pop("processed", None)
                fingerprints[str(cycle["page"])] = fingerprint
                current = self.get(task["number"])
                current["data"]["cycle"] = cycle
                self.save(current)
                processed = set(cycle.get("processed", []))
                ids = {p["id"] for p in batch if p.get("source") != "parent_pipeline"}
                if cycle["page"] == 1:
                    ids.update(
                        pid
                        for pid, body in self.db.execute(
                            "SELECT pipeline_id,body FROM pipeline_gaps WHERE task_id=? ORDER BY json_extract(body,'$.retry_at') LIMIT 100",
                            (tid,),
                        )
                        if json.loads(body).get("retry_at", 0) <= now
                    )
                    ids.update(
                        r[0]
                        for r in self.db.execute(
                            "SELECT pipeline_id FROM pipeline_observations WHERE task_id=?",
                            (tid,),
                        )
                    )
                limit = asyncio.Semaphore(4)
                ids.difference_update(processed)

                def mark_processed(pid):
                    current = self.get(task["number"])
                    if (
                        current["state"] != "active"
                        or current["version"] != task["version"]
                    ):
                        return
                    processed.add(pid)
                    cycle["processed"] = sorted(processed)
                    current["data"]["cycle"] = cycle
                    self.save(current)

                async def consume(pid):
                    current = self.get(task["number"])
                    if (
                        current["state"] != "active"
                        or current["version"] != task["version"]
                    ):
                        return False
                    snap = await self.gitlab.collect(c["project_id"], pid)
                    branch = await self.gitlab.scope(c, snap["pipeline"])
                    current = self.get(task["number"])
                    if (
                        current["state"] != "active"
                        or current["version"] != task["version"]
                    ):
                        return False
                    if branch is None:
                        return
                    previous_snapshot = self.db.execute(
                        "SELECT body FROM pipeline_observations WHERE task_id=? AND pipeline_id=?",
                        (tid, pid),
                    ).fetchone()
                    previous_snapshot = (
                        json.loads(previous_snapshot[0]) if previous_snapshot else {}
                    )
                    root_keys = sorted(
                        j["id"] for j in snap["jobs"] if j.get("pipeline_id") == pid
                    )
                    old_keys = sorted(
                        j["id"]
                        for j in previous_snapshot.get("jobs", [])
                        if j.get("pipeline_id") == pid
                    )
                    if snap["pipeline"]["status"] in (
                        "success",
                        "failed",
                        "canceled",
                        "skipped",
                    ) and snap.get("stable", True):
                        snap["terminal_seen"] = (
                            previous_snapshot.get("terminal_seen", now)
                            if root_keys == old_keys
                            else now
                        )
                    with self.db:
                        self.db.execute(
                            "INSERT OR REPLACE INTO pipeline_observations VALUES(?,?,?)",
                            (tid, pid, dump(snap)),
                        )
                    old = [r for r in self.records(tid) if r["pipeline_id"] == pid]
                    awaiting = not snap["complete"]
                    if awaiting:
                        if (
                            "terminal_seen" not in snap
                            or now - snap["terminal_seen"] < c["interval_minutes"] * 60
                        ):
                            return
                        snap = dict(snap, complete=True)
                    fingerprint = hashlib.sha256(
                        dump(
                            {k: v for k, v in snap.items() if k != "terminal_seen"}
                        ).encode()
                    ).hexdigest()
                    last = old[-1] if old else None
                    update = False
                    if last and last.get("awaiting_children"):
                        previous_root = sorted(
                            j["id"] for j in last["jobs"] if j.get("pipeline_id") == pid
                        )
                        newly_retried = any(
                            j.get("retried")
                            and not any(
                                o.get("retried")
                                for o in last["jobs"]
                                if o["id"] == j["id"]
                            )
                            for j in snap["jobs"]
                            if any(o["id"] == j["id"] for o in last["jobs"])
                        )
                        update = root_keys == previous_root and not newly_retried
                        if update and last.get("snapshot_fingerprint") == fingerprint:
                            return
                    record = execution(
                        snap, old[:-1] if update else old, task["version"]
                    )
                    if record and update:
                        record.update(
                            execution_id=last["execution_id"],
                            sequence=last["sequence"],
                            kind=last["kind"],
                            config_version=last["config_version"],
                        )
                    if record:
                        record.update(
                            awaiting_children=awaiting,
                            snapshot_fingerprint=fingerprint,
                        )
                        if awaiting:
                            record.update(
                                completeness="子流水线等待超时；后续仅补全报告",
                                active_seconds=None,
                                elapsed_seconds=None,
                            )
                    if not record:
                        return
                    record["matched_branch"] = branch
                    history = c["history"]
                    in_history = (
                        history != "none"
                        and record["finished"]
                        >= (
                            task["created"] - 7 * 86400
                            if history == "7d"
                            else history["start"]
                        )
                        and (
                            not isinstance(history, dict)
                            or record["finished"] < history["end"]
                        )
                    )
                    if record["finished"] < task["created"] and not in_history:
                        return
                    if record["status"] == "failed" and not c["monitor_failed"]:
                        return
                    record["reasons"] = [
                        dict(
                            job_id=j["id"],
                            raw=j.get("failure_reason") or "unknown",
                            category="未分类",
                            explanation="平台原因："
                            + (j.get("failure_reason") or "未知")
                            + "；日志分析待完成",
                            evidence=j.get("web_url"),
                            allow_failure=bool(j.get("allow_failure")),
                            analysis="pending",
                        )
                        for j in record["jobs"]
                        if j["status"] == "failed"
                    ]
                    if record["status"] == "failed" and not record["reasons"]:
                        record["reasons"] = [
                            dict(
                                job_id=0,
                                raw="pipeline_failure",
                                category="脚本配置"
                                if record["pipeline"].get("yaml_errors")
                                else "未分类",
                                explanation="Pipeline 失败但未返回失败 Job；请从原始链接核实配置或平台原因",
                                evidence=record["web_url"],
                                allow_failure=False,
                                analysis="complete",
                            )
                        ]
                    record["timezone"] = get_timezone(self.cfg).key
                    record = alerts(
                        record,
                        [
                            r
                            for r in self.records(tid)
                            if r["execution_id"] != record["execution_id"]
                        ],
                        c["thresholds"],
                    )
                    await self.analyze(record, c["project_id"])
                    current = self.get(task["number"])
                    if (
                        current["state"] != "active"
                        or current["version"] != task["version"]
                    ):
                        return False
                    current["data"]["revision"] += 1
                    current["data"]["sync"] = "待发布"
                    record["data_revision"] = current["data"]["revision"]
                    # Record + outbox + revision are one transaction: a crash cannot lose its card.
                    with self.db:
                        inserted = self.db.execute(
                            "INSERT OR REPLACE INTO pipeline_records VALUES(?,?,?,?,?)"
                            if update
                            else "INSERT OR IGNORE INTO pipeline_records VALUES(?,?,?,?,?)",
                            (
                                tid,
                                record["execution_id"],
                                pid,
                                record["finished"],
                                dump(record),
                            ),
                        ).rowcount
                        if inserted:
                            if (
                                not update
                                and record["finished"] >= data["notify_after"]
                                and record["status"] in ("success", "failed")
                            ):
                                self.queue(
                                    current,
                                    record["execution_id"],
                                    dict(record=record),
                                    commit=False,
                                )
                            self.db.execute(
                                "UPDATE pipeline_tasks SET data=? WHERE number=?",
                                (dump(current["data"]), current["number"]),
                            )

                # Persist IDs before network work: cancellation/crash cannot lose a gap.
                with self.db:
                    for pid in ids:
                        self.db.execute(
                            "INSERT OR IGNORE INTO pipeline_gaps VALUES(?,?,?)",
                            (tid, pid, dump(dict(error="pending", retry_at=now))),
                        )

                async def guarded(pid):
                    try:
                        consumed = await asyncio.wait_for(
                            consume(pid), PIPELINE_WORK_SECONDS
                        )
                    except (
                        ValueError,
                        RuntimeError,
                        OSError,
                        asyncio.TimeoutError,
                        KeyError,
                        TypeError,
                    ) as error:
                        with self.db:
                            self.db.execute(
                                "UPDATE pipeline_gaps SET body=? WHERE task_id=? AND pipeline_id=?",
                                (
                                    dump(
                                        dict(
                                            error=str(error)
                                            if isinstance(error, GitLabAPIError)
                                            else type(error).__name__,
                                            retry_at=error.retry_at
                                            if isinstance(error, GitLabRateLimit)
                                            else now + 300,
                                        )
                                    ),
                                    tid,
                                    pid,
                                ),
                            )
                        if isinstance(error, GitLabRateLimit):
                            return error
                        mark_processed(pid)
                    else:
                        if consumed is False:
                            return
                        with self.db:
                            self.db.execute(
                                "DELETE FROM pipeline_gaps WHERE task_id=? AND pipeline_id=?",
                                (tid, pid),
                            )
                        mark_processed(pid)

                async def scheduled(pid):
                    # Queueing for a worker must not consume the per-Pipeline budget.
                    async with limit:
                        return await guarded(pid)

                outcomes = await asyncio.gather(
                    *(scheduled(pid) for pid in sorted(ids))
                )
                limited = next(
                    (v for v in outcomes if isinstance(v, GitLabRateLimit)), None
                )
                if limited:
                    raise limited
                pages += 1
                current = self.get(task["number"])
                if (
                    current["state"] != "active"
                    or current["version"] != task["version"]
                ):
                    return
                if len(batch) < 100:
                    stable = True
                    for page_no, expected in cycle["fingerprints"].items():
                        verify = await self.gitlab.page(
                            f"projects/{c['project_id']}/pipelines",
                            int(page_no),
                            updated_after=iso(max(0, cycle["start"] - 120)),
                            updated_before=iso(cycle["end"]),
                            order_by="updated_at",
                            sort="asc",
                        )
                        if (
                            hashlib.sha256(dump(verify).encode()).hexdigest()
                            != expected
                        ):
                            stable = False
                            break
                    current = self.get(task["number"])
                    if (
                        current["state"] != "active"
                        or current["version"] != task["version"]
                    ):
                        return
                    if not stable:
                        cycle["page"] = 1
                        cycle["fingerprints"] = {}
                        cycle.pop("processed", None)
                        cycle["restarts"] = cycle.get("restarts", 0) + 1
                        current["data"]["cycle"] = cycle
                        current["next_scan"] = now + 15
                        self.save(current)
                        if cycle["restarts"] >= 3:
                            self.issue(
                                current,
                                "分页数据持续变化，保留水位重试",
                                RuntimeError(),
                            )
                        return
                    current["data"]["cursor"] = cycle["end"]
                    current["data"].pop("cycle", None)
                    if current["data"].get("initial"):
                        current["data"]["initial"] = False
                        current["data"]["initial_revision"] = current["data"][
                            "revision"
                        ]
                        if c["history"] == "none":
                            current["data"]["link_queued"] = True
                    current["data"]["checked_at"] = now
                    self.clear_issue(current, "采集失败", "分页数据持续变化，保留水位重试")
                    current["data"].pop("scan_progress", None)
                    current["next_scan"] = now + c["interval_minutes"] * 60
                    self.save(current)
                    # Only pending/running parents need explicit rechecking, terminal updates use updated_after.
                    with self.db:
                        for pid, body in self.db.execute(
                            "SELECT pipeline_id,body FROM pipeline_observations WHERE task_id=?",
                            (tid,),
                        ).fetchall():
                            snapshot = json.loads(body)
                            if snapshot["complete"] and not any(
                                d.get("project_id") == c["project_id"]
                                for d in snapshot["downstream"]
                            ):
                                self.db.execute(
                                    "DELETE FROM pipeline_observations WHERE task_id=? AND pipeline_id=?",
                                    (tid, pid),
                                )
                    return
                cycle["page"] += 1
                cycle.pop("processed", None)
                current["data"]["cycle"] = cycle
                self.save(current)

    async def analyze(self, record, project):
        limit = asyncio.Semaphore(4)

        async def one(reason):
            job = next(j for j in record["jobs"] if j["id"] == reason["job_id"])
            async with limit:
                try:
                    return await self.gitlab.reason(project, job)
                except (
                    RuntimeError,
                    ValueError,
                    OSError,
                    asyncio.TimeoutError,
                ) as error:
                    self.defer_gitlab(error)
                    return reason

        tasks = [
            asyncio.create_task(one(r))
            for r in record["reasons"]
            if r["analysis"] == "pending"
        ]
        if not tasks:
            return
        record["analysis_after"] = time.time() + 300
        try:
            done, pending = await asyncio.wait(tasks, timeout=180)
            results = {t.result()["job_id"]: t.result() for t in done}
            record["reasons"] = [results.get(r["job_id"], r) for r in record["reasons"]]
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def deliver(self):
        async with self.delivery_lock:
            await self._deliver()

    async def _deliver(self):
        rows = self.db.execute(
            "SELECT d.key,t.number,d.execution_id,d.recipient,d.body FROM pipeline_deliveries d "
            "JOIN pipeline_tasks t ON t.task_id=d.task_id WHERE t.binding=? AND d.state='pending' ORDER BY d.updated LIMIT 50",
            (self.binding,),
        ).fetchall()
        for key, n, eid, recipient, body in rows:
            task = self.get(n)
            if (
                task["state"] in ("deleted", "deleting")
                or task["state"] == "paused"
                and not eid.startswith("error:")
            ):
                continue
            if not eid.startswith("error:") and recipient not in (
                task["config"]["recipients"] or ["self"]
            ):
                with self.db:
                    self.db.execute(
                        "UPDATE pipeline_deliveries SET state='suppressed',result='收件范围已修改' WHERE key=?",
                        (key,),
                    )
                continue
            body = json.loads(body)
            identity = "bot" if recipient == "self" else "user"
            try:
                await self.verify_identity(identity)
            except (RuntimeError, ValueError, OSError, asyncio.TimeoutError) as e:
                with self.db:
                    self.db.execute(
                        "UPDATE pipeline_deliveries SET result=? WHERE key=?",
                        ("发送前身份校验失败", key),
                    )
                self.issue(task, "飞书身份校验", e)
                continue
            current = self.get(n)
            if (
                current["state"] != task["state"]
                or current["version"] != task["version"]
            ):
                continue
            args = [self.cfg["LARK_CLI_BIN"], "im"]
            if recipient == "self":
                thread = self.sessions.thread(task["session_id"])
                if not thread:
                    continue
                args += [
                    "+messages-reply",
                    "--as",
                    "bot",
                    "--message-id",
                    thread["root_id"],
                    "--reply-in-thread",
                ]
            else:
                args += ["+messages-send", "--as", "user", "--chat-id", recipient]
            content = None
            if "record" in body:
                name = self.recipient_name(task["config"], recipient)
                content = card(
                    task,
                    body["record"],
                    task["data"].get("report_url", ""),
                    "你好，" + name if recipient != "self" else "",
                    cfg=self.cfg,
                )
                args += ["--msg-type", "interactive", "--content", dump(content)]
            else:
                message = body["markdown"]
                if recipient != "self":
                    message = (
                        "你好，"
                        + self.recipient_name(task["config"], recipient)
                        + "\n"
                        + message
                        + "\n"
                        + message_signature(self.cfg)
                    )
                args += ["--markdown", message]
            with self.db:
                claimed = self.db.execute(
                    "UPDATE pipeline_deliveries SET state='sending',updated=? WHERE key=? AND state='pending'",
                    (time.time(), key),
                ).rowcount
            if not claimed:
                continue
            try:
                result = json.loads(
                    await self.run(args + ["--idempotency-key", key], 30)
                )
                from assistant.service import sent_message_id

                mid = sent_message_id(result)
                state = "sent" if result.get("ok") is True and mid else "uncertain"
            except (RuntimeError, ValueError, OSError, asyncio.TimeoutError):
                state, mid = "uncertain", None
            with self.db:
                self.db.execute(
                    "UPDATE pipeline_deliveries SET state=?,updated=?,result=? WHERE key=?",
                    (state, time.time(), mid or "投递未确认；不自动重发，请核实", key),
                )
                if state == "sent" and content is not None:
                    self.interactions.register(
                        mid, task["task_id"], recipient, body["record"], content
                    )
            if state == "uncertain" and not eid.startswith("error:"):
                self.issue(task, "飞书投递结果不确定", RuntimeError())
            await asyncio.sleep(0.2)

    async def tick(self):
        now = time.time()
        semaphore = asyncio.Semaphore(4)

        async def check(t):
            async with semaphore:
                try:
                    await asyncio.wait_for(self.scan(t, now), 300)
                except asyncio.TimeoutError:
                    # The scan budget is a normal yield point for large backfills.
                    # Individual API timeouts carry a sanitized GitLabAPIError.
                    current = self.get(t["number"])
                    current["data"]["scan_progress"] = (
                        "本轮达到 300 秒采集预算，保留分页进度继续补采"
                    )
                    current["next_scan"] = time.time() + 15
                    self.save(current)
                except (
                    ValueError,
                    RuntimeError,
                    OSError,
                    asyncio.TimeoutError,
                    KeyError,
                    TypeError,
                ) as e:
                    self.issue(t, "采集失败", e)
                    current = self.get(t["number"])
                    current["next_scan"] = (
                        max(time.time() + 1, e.retry_at)
                        if isinstance(e, GitLabRateLimit)
                        else now + 60
                    )
                    self.save(current)

        await asyncio.gather(
            *(
                check(t)
                for t in self.tasks()
                if t["state"] == "active" and t["next_scan"] <= now
            )
        )
        # Late analysis is report-only and independently retryable.
        for task in self.tasks():
            if task["state"] != "active":
                continue
            pending = [
                r
                for r in self.records(task["task_id"])
                if r.get("analysis_after", 0) <= time.time()
                and any(x["analysis"] == "pending" for x in r["reasons"])
            ]
            for record in pending[:1]:
                before = dump(record)
                await self.analyze(record, task["config"]["project_id"])
                if dump(record) != before:
                    current = self.get(task["number"])
                    if current["state"] != "active":
                        continue
                    with self.db:
                        self.db.execute(
                            "UPDATE pipeline_records SET body=? WHERE task_id=? AND execution_id=?",
                            (dump(record), task["task_id"], record["execution_id"]),
                        )
                    self.changed(self.get(task["number"]))

    async def output_tick(self):
        from assistant.pipeline_pages import Publisher

        with file_lock(self.root / ".local/pipeline-delivery.lock") as acquired:
            if acquired:
                await self.deliver()
        try:
            await Publisher(self).tick()
        except (ValueError, RuntimeError, OSError, asyncio.TimeoutError) as error:
            for task in self.tasks():
                if task["state"] != "deleted":
                    self.issue(task, "报告发布失败", error)

    async def scheduler(self):
        # Only one scheduler instance may recover ambiguous sends and run this binding.
        binding_key = hashlib.sha256(self.binding.encode()).hexdigest()[:16]
        with file_lock(
            self.root / ".local" / f"pipeline-scheduler-{binding_key}.lock"
        ) as acquired:
            if not acquired:
                return
            self.recover()

            async def loop(action, seconds):
                while True:
                    try:
                        await action()
                    except Exception as error:
                        logging.warning(
                            "Pipeline scheduler failure: %s", type(error).__name__
                        )
                    await asyncio.sleep(seconds)

            await asyncio.gather(loop(self.tick, 15), loop(self.output_tick, 5))
