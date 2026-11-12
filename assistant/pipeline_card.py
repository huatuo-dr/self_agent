"""Chart-first Pipeline execution duration card."""

import json
import hashlib
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from assistant.settings import bot_name, get_timezone, message_signature

from assistant.pipeline_gitlab import number, stamp


def text(value):
    return "".join(
        f"&#{ord(c)};" if c in "*~<>[]()#:_`@&" else c
        for c in str(value if value is not None else "未知")[:250]
    )


def chart_text(value):
    # VChart handles plain text; Markdown entities would appear literally in tooltips.
    return str(value)[:250].replace("<", "＜").replace(">", "＞").replace("&", "＆")


def duration(value):
    if number(value) is None:
        return "未知"
    if value < 60:
        return f"{value:.1f} 秒"
    minutes, seconds = divmod(round(value), 60)
    return f"{minutes} 分 {seconds:02d} 秒"


def link(label, url):
    try:
        u = urlsplit(url or "")
        valid = (
            u.scheme == "https"
            and u.hostname
            and not u.username
            and not u.password
            and len(url) <= 2000
            and not any(c in url for c in "\n\r()<>")
        )
    except ValueError:
        valid = False
    return f"[{text(label)}]({url})" if valid else text(label)


def md(content, **style):
    return dict(tag="markdown", content=content, **style)


def panel(title, elements):
    return dict(
        tag="collapsible_panel",
        expanded=False,
        header=dict(title=dict(tag="plain_text", content=title)),
        elements=elements,
    )


def comparison(record, limits):
    baseline = record.get("baseline", {})
    samples = baseline.get("samples", 0)
    median, active = (
        number(baseline.get("median")),
        number(record.get("active_seconds")),
    )
    if median is None or active is None or not samples:
        return "历史参考：暂无可比样本。"
    delta = active - median
    change = (
        ("↑ 增加" if delta > 0 else "↓ 缩短" if delta < 0 else "→ 持平")
        + " "
        + duration(abs(delta))
    )
    ratio = f" / {abs(delta) / median:.1%}" if median > 0 else "；基线为 0，不计算比例"
    note = (
        f"；样本不足 {limits['min_samples']} 次，不判断明显变慢"
        if samples < limits["min_samples"]
        else ""
    )
    return f"近 7 天中位数 {duration(median)} · {samples} 次；{change}{ratio}{note}。"


# Preserve the phase palette in the owner's forwarded GS001 duration card.
PHASES = ["准备", "编译测试", "测试", "检查", "打包上传", "多阶段同时运行", "其他"]
COLORS = ["#A77952", "#3478E5", "#9860C6", "#22A6A8", "#5AAB62", "#E5A126", "#697586"]
STAGES = {
    "准备": {".pre", "prepare", "init", "validate", "setup"},
    "编译测试": {"build", "build_test", "compile"},
    "测试": {"test", "tests", "e2e_test", "device_test", "integration", "regression"},
    "检查": {
        "analysis",
        "quality-gate",
        "quality_gate",
        "issue_check",
        ".post",
        "check",
        "lint",
    },
    "打包上传": {"package", "publish", "deploy", "release", "upload"},
}


def phase(stage):
    return next((name for name, stages in STAGES.items() if stage in stages), "其他")


def merged_intervals(intervals):
    merged = []
    for a, b in sorted(intervals):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(b, merged[-1][1])
        else:
            merged.append([a, b])
    return merged


def union_seconds(intervals):
    return sum(b - a for a, b in merged_intervals(intervals))


def phase_data(record, include_unrun=False):
    """Partition actual running intervals; neither parallel jobs nor gaps add time."""
    stages, events, seen = {}, defaultdict(list), set()
    for j in record["jobs"]:
        identity = (j.get("project_id"), j["id"])
        if identity in seen:
            continue
        seen.add(identity)
        unrun = j["status"] in {"skipped", "manual", "created"} and not j.get(
            "started_at"
        )
        if unrun and not include_unrun:
            continue
        key = (j.get("project_id"), j.get("pipeline_id"), j.get("stage") or "未标注")
        g = stages.setdefault(
            key,
            dict(
                key=key,
                name=key[-1],
                phase=phase(key[-1]),
                intervals=[],
                jobs=[],
                missing=0,
            ),
        )
        g["jobs"].append(j)
        if unrun:
            continue
        a, b = stamp(j.get("started_at")), stamp(j.get("finished_at"))
        if (
            a is None
            or b is None
            or not math.isfinite(a)
            or not math.isfinite(b)
            or b < a
        ):
            g["missing"] += 1
            continue
        g["intervals"].append((a, b))
        if b > a:
            events[a].append((key, 1))
            events[b].append((key, -1))
    active, totals, combinations = Counter(), Counter(), Counter()
    previous = None
    for when in sorted(events):
        keys = [key for key, count in active.items() if count > 0]
        if previous is not None and keys:
            seconds = when - previous
            category = stages[keys[0]]["phase"] if len(keys) == 1 else PHASES[5]
            totals[category] += seconds
            if len(keys) > 1:
                # Include child Pipeline identities instead of conflating equal stage names.
                names = tuple(sorted(f"#{k[1]} {k[2]}" for k in keys))
                combinations[names] += seconds
        for key, change in events[when]:
            active[key] += change
            if not active[key]:
                del active[key]
        previous = when
    groups = []
    for g in stages.values():
        indices = {
            j.get("stage_index")
            for j in g["jobs"]
            if type(j.get("stage_index")) is int and j["stage_index"] >= 0
        }
        complete_order = len(indices) == 1 and all(
            type(j.get("stage_index")) is int and j["stage_index"] >= 0
            for j in g["jobs"]
        )
        rank = next(iter(indices)) if complete_order else None
        groups.append(
            dict(
                g,
                seconds=union_seconds(g["intervals"]) if g["intervals"] else None,
                start=min((a for a, _ in g["intervals"]), default=math.inf),
                order=rank,
            )
        )

    def ordering(g):
        fallback = min(j["id"] for j in g["jobs"])
        if g["name"] == ".pre":
            fallback = -1
        elif g["name"] == ".post":
            fallback = math.inf
        return (
            g["key"][1] != record["pipeline_id"],
            g["key"][0] or 0,
            g["key"][1] or 0,
            g["order"] is None,
            g["order"] if g["order"] is not None else fallback,
        )

    groups.sort(key=ordering)
    return groups, totals, combinations


def chart(spec, height):
    return dict(tag="chart", height=f"{height}px", preview=True, chart_spec=spec)


def scale_max(seconds):
    value = max(seconds / 60, 0.05)
    power = 10 ** math.floor(math.log10(value))
    step = power / 2
    return math.ceil(value / step) * step


def bar_spec(values, rows, maximum, *, legend=False):
    """Use horizontal stacked bars, fine grid and exact hover values."""
    return dict(
        type="bar",
        direction="horizontal",
        xField="minutes",
        yField="row",
        seriesField="phase",
        stack=True,
        sortDataByAxis=True,
        media=[],
        data=[
            dict(
                values=values,
                fields={
                    "row": dict(domain=rows, sortIndex=0),
                    "phase": dict(domain=PHASES, sortIndex=1),
                },
            )
        ],
        padding=dict(top=0, left=0, right=8, bottom=8),
        color=COLORS,
        scales=[dict(id="phaseColors", type="ordinal", domain=PHASES, range=COLORS)],
        bar=dict(style=dict(fill=dict(field="phase", scale="phaseColors"))),
        barWidth=12,
        barMaxWidth=12,
        label=dict(visible=False),
        axes=[
            dict(
                orient="bottom",
                type="linear",
                min=0,
                max=maximum,
                nice=False,
                tickCount=5,
                domainLine=dict(visible=False),
                tick=dict(visible=True),
                grid=dict(visible=True, style=dict(stroke="#e2e8f0", lineWidth=0.7)),
                label=dict(flush=True, style=dict(fontSize=10, fill="#64748b")),
                title=dict(
                    visible=True,
                    text="有效执行耗时（分钟）",
                    textStyle=dict(fontSize=11),
                ),
            ),
            dict(
                orient="left",
                type="band",
                domain=rows,
                inverse=False,
                minWidth=112,
                maxWidth=112,
                paddingInner=0,
                paddingOuter=0,
                domainLine=dict(visible=False),
                tick=dict(visible=False),
                grid=dict(visible=False),
                label=dict(style=dict(fontSize=11, fill="#475569")),
            ),
        ],
        legends=dict(visible=legend),
        tooltip=dict(
            visible=True,
            activeType=["mark"],
            dimension=dict(visible=False),
            mark=dict(
                visible=True,
                title=dict(visible=False),
                content=[
                    dict(key="对象", value=dict(field="name")),
                    dict(key="阶段类型", value=dict(field="phase")),
                    dict(key="执行耗时", value=dict(field="duration")),
                    dict(key="占整体执行", value=dict(field="share")),
                    dict(key="最长 Job 排队", value=dict(field="queue")),
                    dict(key="明细", value=dict(field="detail")),
                ],
            ),
        ),
    )


def phase_legend():
    # Attach the legend to real stage data, never to a dummy chart/axis.
    return dict(
        visible=True,
        orient="top",
        padding=0,
        maxRow=2,
        autoPage=False,
        item=dict(
            spaceCol=4,
            spaceRow=2,
            label=dict(style=dict(fontSize=10)),
            shape=dict(space=2, style=dict(size=8)),
        ),
    )


def execution_chart(record):
    groups, _, _ = phase_data(record)
    active = number(record["active_seconds"])
    starts = [stamp(j.get("started_at")) for j in record["jobs"]]
    starts = [value for value in starts if value is not None and math.isfinite(value)]
    origin = min(starts, default=None)
    rows, values = [], []
    shown = groups[:12]
    zone = get_timezone({"TIMEZONE": record.get("timezone")})
    for i, g in enumerate(shown, 1):
        suffix = f" #{g['key'][1]}" if g["key"][1] != record["pipeline_id"] else ""
        row = f"{i}. {g['name'][:15]}{suffix}" + (" *" if g["missing"] else "")
        rows.append(row)
        queues = [number(j.get("queued_duration")) for j in g["jobs"]]
        queue = max((q for q in queues if q is not None), default=None)
        # Merge only overlapping intervals within this stage. Disjoint runs retain gaps.
        for start, end in merged_intervals(g["intervals"]):
            values.append(
                dict(
                    row=row,
                    name=chart_text(g["name"] + suffix),
                    phase=g["phase"],
                    start=(start - origin) / 60,
                    end=(end - origin) / 60,
                    began=datetime.fromtimestamp(start, zone).strftime(
                        "%Y-%m-%d %H:%M:%S.%f"
                    )[:-3],
                    ended=datetime.fromtimestamp(end, zone).strftime(
                        "%Y-%m-%d %H:%M:%S.%f"
                    )[:-3],
                    duration=duration(end - start),
                    total=duration(g["seconds"]),
                    share=f"{g['seconds'] / active:.1%}" if active else "未知",
                    queue=duration(queue),
                    detail=chart_text(
                        f"{len(g['jobs'])} 个 Job；阶段内并行去重"
                        + (
                            f"；{g['missing']} 个 Job 起止缺失，仅展示已知区间"
                            if g["missing"]
                            else ""
                        )
                    ),
                )
            )
    notes = []
    if values:
        maximum = scale_max(max(value["end"] for value in values) * 60)
        total_segments = len(values)
        if total_segments > 48:
            values = sorted(values, key=lambda v: (v["start"], v["end"]))[:48]
            notes.append(
                f"时间段较多，仅展示前 48 / {total_segments} 段，其余见完整报告"
            )
        spec = bar_spec(values, rows, maximum)
        spec.update(type="rangeColumn", xField=["start", "end"], stack=False)
        spec["axes"][0]["title"]["text"] = "距执行开始（分钟）"
        spec["tooltip"]["mark"]["content"] = [
            dict(key="阶段", value=dict(field="name")),
            dict(key=f"开始（{zone.key}）", value=dict(field="began")),
            dict(key=f"结束（{zone.key}）", value=dict(field="ended")),
            dict(key="本段执行", value=dict(field="duration")),
            dict(key="阶段累计执行", value=dict(field="total")),
            dict(key="最长 Job 排队", value=dict(field="queue")),
            dict(key="明细", value=dict(field="detail")),
        ]
        spec["legends"] = phase_legend()
        main = chart(spec, math.ceil(36 * 0.7 * len(rows)) + 56 + 48)
        main["element_id"] = "stage_timeline"
        elements = [main]
        notes.append(
            "0 = "
            + datetime.fromtimestamp(origin, zone).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            + " " + zone.key
        )
    else:
        elements = [md("Job 起止时间不足，暂不能绘制阶段时间线。")]
    if active is None or any(g["missing"] for g in groups):
        notes.append("数据不完整，以已知最早 Job 开始为 0，仅展示已知运行区间")
    if len(groups) > len(shown):
        notes.append(f"展示前 {len(shown)} 个阶段，其余阶段见完整报告")
    if any(g["order"] is None for g in groups):
        notes.append("阶段定义顺序未核实，暂按 Job 创建顺序排列")
    notes.append(
        "按真实起止位置绘制；空白为运行空档，阶段重叠可直接对照。悬停查看起止时间。"
    )
    note = md("；".join(notes), text_size="notation")
    note["element_id"] = "timeline_note"
    elements.append(note)
    return elements


def history_chart(record, limits):
    elements = [md(comparison(record, limits))]
    baseline = record.get("baseline", {})
    daily = baseline.get("daily")
    if daily:
        start = datetime.fromtimestamp(
            record["finished"] - 7 * 86400, get_timezone({"TIMEZONE": record.get("timezone")})
        ).date()
        end = datetime.fromtimestamp(
            record["finished"], get_timezone({"TIMEZONE": record.get("timezone")})
        ).date()
        days = [
            (start + timedelta(days=i)).isoformat()
            for i in range((end - start).days + 1)
        ]
        values = [
            dict(
                day=d["day"][5:],
                row="历史中位数",
                minutes=round(d["median"] / 60, 1),
                duration=duration(d["median"]),
                samples=str(d["samples"]),
            )
            for d in daily
            if d["day"] in days and number(d.get("median")) is not None
        ]
        if values:
            elements.append(
                chart(
                    dict(
                        type="heatmap",
                        xField="day",
                        yField="row",
                        valueField="minutes",
                        data=dict(values=values),
                        padding=dict(top=4, left=0, right=8, bottom=12),
                        color=["#edf3ff", "#3478E5"],
                        legends=dict(visible=False),
                        cell=dict(
                            style=dict(fill="#e1edff", stroke="#ffffff", lineWidth=2)
                        ),
                        label=dict(
                            visible=True,
                            style=dict(fontSize=11, fill="#334155"),
                        ),
                        axes=[
                            dict(
                                orient="bottom",
                                type="band",
                                domain=[d[5:] for d in days],
                                domainLine=dict(visible=False),
                                tick=dict(visible=False),
                                label=dict(style=dict(fontSize=10)),
                            ),
                            dict(orient="left", type="band", visible=False),
                        ],
                        tooltip=dict(
                            mark=dict(
                                title=dict(visible=False),
                                content=[
                                    dict(key="日期", value=dict(field="day")),
                                    dict(key="中位耗时", value=dict(field="duration")),
                                    dict(key="有效样本", value=dict(field="samples")),
                                ],
                            )
                        ),
                    ),
                    96,
                )
            )
            elements.append(
                md(
                    f"过去 168 小时的可比历史，按 {get_timezone({'TIMEZONE': record.get('timezone')}).key} 日期取中位数；不含本次，空白无样本。格内单位：分钟。",
                    text_size="notation",
                )
            )
    elif (
        number(baseline.get("median")) is not None
        and number(record.get("active_seconds")) is not None
    ):
        values = [
            dict(
                row=name,
                phase="检查",
                minutes=seconds / 60,
                name=name,
                duration=duration(seconds),
                share="—",
                queue="—",
                detail="同分支、来源、状态、执行类型与版本",
            )
            for name, seconds in [
                ("近7天中位数", baseline["median"]),
                ("本次执行", record["active_seconds"]),
            ]
        ]
        elements.append(
            chart(
                bar_spec(
                    values,
                    [v["row"] for v in values],
                    scale_max(max(baseline["median"], record["active_seconds"])),
                ),
                144,
            )
        )
        elements.append(
            md(
                "此历史记录未保存每日汇总，仅展示整体基线与本次对比。",
                text_size="notation",
            )
        )
    return panel("近 7 天整体耗时对比", elements)


def speed_label(record, limits):
    """Presentation buckets, independent from persisted alert thresholds and sends."""
    active = number(record.get("active_seconds"))
    base = record.get("baseline", {})
    median, samples = number(base.get("median")), base.get("samples", 0)
    if active is None or median is None:
        return "暂无对比", "neutral", ""
    if samples < limits["min_samples"]:
        return "样本不足", "neutral", f"{samples}/{limits['min_samples']} 次"
    if median == 0:
        return (
            ("正常", "neutral", "均为 0")
            if active == 0
            else ("无法比较", "neutral", "历史基线为 0")
        )
    delta = active - median
    ratio = delta / median
    detail = "较历史中位数 " + (
        f"↑{ratio:.1%}" if delta > 0 else f"↓{-ratio:.1%}" if delta < 0 else "持平"
    )
    if delta >= limits["slow_seconds"] and ratio >= limits["slow_ratio"]:
        return "极慢", "red", detail
    if delta >= 60 and ratio >= 0.2:
        return "慢", "yellow", detail
    if delta <= -60 and ratio <= -0.2:
        return "快", "green", detail
    return "正常", "neutral", detail


def stage_job_summary(g):
    ran = [
        j
        for j in g["jobs"]
        if j.get("started_at") or j["status"] not in {"manual", "created", "skipped"}
    ]
    times = [
        number(j.get("duration"))
        for j in ran
        if j["status"] in {"success", "failed", "canceled"}
    ]
    known = [t for t in times if t is not None]
    queues = [number(j.get("queued_duration")) for j in ran]
    known_queues = [q for q in queues if q is not None]
    stage_duration = duration(g["seconds"]) + ("（已知区间）" if g["missing"] else "")
    summary = (
        f"阶段执行：{stage_duration}；Job 执行合计：{duration(sum(known) if known else None)}；"
        f"每 Job 平均：{duration(sum(known) / len(known) if known else None)}（{len(known)}/{len(ran)} 个运行 Job 有效）。\n"
        f"Job 排队合计：{duration(sum(known_queues) if known_queues else None)}（{len(known_queues)}/{len(ran)} 项已知）；"
        "并行 Job 的合计不等于阶段执行。"
    )
    return summary


def job_choices(record):
    """Bounded options: stage/page identities include child project and pipeline."""
    choices = []
    for g in phase_data(record, include_unrun=True)[0]:
        jobs = sorted(
            g["jobs"],
            key=lambda j: (
                stamp(j.get("started_at"))
                if stamp(j.get("started_at")) is not None
                else math.inf,
                j["id"],
            ),
        )
        pages = math.ceil(len(jobs) / 12)
        suffix = f" · #{g['key'][1]}" if g["key"][1] != record["pipeline_id"] else ""
        for page in range(pages):
            token = hashlib.sha256(json.dumps([g["key"], page]).encode()).hexdigest()[
                :20
            ]
            label = (
                g["name"][:40]
                + suffix
                + (f" · {page + 1}/{pages} 页" if pages > 1 else "")
            )
            choices.append(
                dict(
                    value=token,
                    label=label,
                    group=g,
                    jobs=jobs[page * 12 : (page + 1) * 12],
                )
            )
    return choices


def job_section(record, selected=None, *, loading=False):
    choices = job_choices(record)
    if not choices:
        return [md("暂无 Job 数据。", element_id="job_note")]
    shown = choices[:40]
    chosen = next((c for c in shown if c["value"] == selected), None)
    if selected is not None and chosen is None:
        raise ValueError("阶段选择无效或超出单卡范围")
    if chosen is None:
        chosen = max(shown, key=lambda c: c["group"]["seconds"] or 0)
    elements = [
        md("**Job 耗时 · " + text(chosen["label"]) + "**", element_id="job_heading")
    ]
    elements.append(
        dict(
            tag="select_static",
            name="pipeline_job_stage",
            element_id="job_selector",
            width="fill",
            placeholder=dict(tag="plain_text", content="选择阶段查看 Job"),
            options=[
                dict(text=dict(tag="plain_text", content=c["label"]), value=c["value"])
                for c in shown
            ],
            initial_option=chosen["value"],
            behaviors=[
                dict(
                    type="callback", value=dict(action="pipeline_job_stage", version=1)
                )
            ],
        )
    )
    if loading:
        elements.append(
            md(
                "暂无数据 · 正在更新所选阶段…\n如长时间未完成，请重新选择阶段。",
                element_id="job_note",
                text_size="notation",
            )
        )
        return elements
    starts = [stamp(j.get("started_at")) for j in record["jobs"]]
    starts = [v for v in starts if v is not None and math.isfinite(v)]
    origin = min(starts, default=None)
    ends = [b for g in phase_data(record)[0] for _, b in g["intervals"]]
    ends = [
        v
        for v in ends
        if v is not None and math.isfinite(v) and origin is not None and v >= origin
    ]
    maximum = scale_max(max(ends, default=origin or 0) - (origin or 0))
    rows, values = [], []
    details = [md(stage_job_summary(chosen["group"]), text_size="notation")]
    zone = get_timezone({"TIMEZONE": record.get("timezone")})
    for i, j in enumerate(chosen["jobs"], 1):
        failed = j["status"] == "failed"
        status = {
            "success": "成功",
            "failed": "失败",
            "canceled": "已取消",
            "skipped": "已跳过",
            "manual": "手动未运行",
        }.get(j["status"], j["status"])
        if failed and j.get("allow_failure"):
            status += "（非阻塞）"
        row = f"{i}. {'× ' if failed else ''}{chart_text(j['name'][:15])}"
        rows.append(row)
        a, b = stamp(j.get("started_at")), stamp(j.get("finished_at"))
        if (
            origin is not None
            and a is not None
            and b is not None
            and math.isfinite(a)
            and math.isfinite(b)
            and b >= a
        ):
            values.append(
                dict(
                    row=row,
                    name=chart_text(j["name"] + f" #{j['id']}"),
                    phase=phase(j.get("stage")),
                    start=(a - origin) / 60,
                    end=(b - origin) / 60,
                    began=datetime.fromtimestamp(a, zone).strftime("%Y-%m-%d %H:%M:%S"),
                    ended=datetime.fromtimestamp(b, zone).strftime("%Y-%m-%d %H:%M:%S"),
                    duration=duration(j.get("duration")),
                    queue=duration(j.get("queued_duration")),
                    status=status,
                )
            )
        details.append(
            md(
                f"{link(j['name'] + ' #' + str(j['id']), j.get('web_url'))} · {text(status)}\n执行 {duration(j.get('duration'))} · 排队 {duration(j.get('queued_duration'))}"
            )
        )
    if values:
        spec = bar_spec(values, rows, maximum)
        spec.update(type="rangeColumn", xField=["start", "end"], stack=False)
        # A canvas axis title can be clipped when there are only one or two Jobs.
        # Keep the unit caption outside the chart's height-dependent layout.
        spec["axes"][0]["title"]["visible"] = False
        spec["label"] = dict(
            visible=True,
            position="right",
            formatter="{duration}",
            style=dict(fontSize=10, fill="#475569", stroke="#ffffff", lineWidth=2),
        )
        spec["tooltip"]["mark"]["content"] = [
            dict(key=k, value=dict(field=v))
            for k, v in [
                ("Job", "name"),
                ("状态", "status"),
                (f"开始（{zone.key}）", "began"),
                (f"结束（{zone.key}）", "ended"),
                ("执行耗时", "duration"),
                ("排队", "queue"),
            ]
        ]
        graph = chart(spec, math.ceil(25.2 * len(rows)) + 56)
        graph["element_id"] = "job_timeline"
        elements.append(graph)
        elements.append(
            md(
                "距执行开始（分钟）",
                element_id="job_axis_caption",
                text_size="notation",
                text_align="center",
            )
        )
    note = f"本页 {len(chosen['jobs'])} / 本阶段 {len(chosen['group']['jobs'])} 个 Job；按开始时间排列，与阶段总览共用时间原点及刻度。× 表示失败。"
    if len(values) < len(rows):
        note += "未运行或缺少完整起止时间的 Job 不绘制条形，详情保留原始耗时。"
    if len(chosen["group"]["jobs"]) > len(chosen["jobs"]):
        note += "可在阶段选择器中切换分页，完整明细请查看报告。"
    if len(choices) > len(shown):
        note += "阶段/分页较多，单卡仅列前 40 项，其余请查看长期报告。"
    elements.append(md(note, text_size="notation", element_id="job_note"))
    detail = panel("所选阶段 Job 明细", details)
    detail["element_id"] = "job_details"
    elements.append(detail)
    return elements


def replace_job_section(payload, record, selected, *, loading=False):
    """Update only the selected Job view, preserving preview labels and other panels."""
    from copy import deepcopy

    result = deepcopy(payload)
    elements = result["body"]["elements"]
    indices = [
        i for i, e in enumerate(elements) if e.get("element_id", "").startswith("job_")
    ]
    if not indices:
        raise ValueError("卡片没有 Job 交互区域")
    elements[indices[0] : indices[-1] + 1] = job_section(
        record, selected, loading=loading
    )
    return fit_card(result)


def fit_card(result):
    """Cap payload across initial render and every interactive update."""

    def size():
        return len(json.dumps(result, ensure_ascii=False).encode())

    elements = result["body"]["elements"]
    for e in elements:
        if size() <= 27000:
            break
        if e.get("tag") == "collapsible_panel" and e.get("element_id") == "job_details":
            e["elements"] = [md("单卡长度限制，完整 Job 明细请查看报告。")]
    for e in elements:
        if size() <= 27000:
            break
        if (
            e.get("tag") == "collapsible_panel"
            and e.get("header", {}).get("title", {}).get("content") == "告警与失败原因"
        ):
            e["elements"] = e["elements"][:2] + [
                md("更多告警和失败原因请查看完整报告。")
            ]
    for element_id, note_id in [
        ("stage_timeline", "timeline_note"),
        ("job_timeline", "job_note"),
    ]:
        timeline = next(
            (e for e in elements if e.get("element_id") == element_id), None
        )
        if not timeline:
            continue
        values = timeline["chart_spec"]["data"][0]["values"]
        while size() > 27000 and len(values) > 1:
            del values[max(1, len(values) // 2) :]
            note = next(e for e in elements if e.get("element_id") == note_id)
            if not note["content"].startswith("单卡长度限制"):
                note["content"] = (
                    "单卡长度限制，部分运行区间未展示，请查看完整报告。"
                    + note["content"]
                )
    if size() > 29000:
        raise ValueError("卡片内容超过单卡大小限制")
    return result


def card(task, record, report_url="", greeting="", cfg=None):
    record = dict(record, timezone=get_timezone(cfg).key)
    config, p = task["config"], record
    failed = [j for j in p["jobs"] if j["status"] == "failed"]
    allowed = [j for j in failed if j.get("allow_failure")]
    blocking = len(failed) - len(allowed)
    status = {
        "success": "成功",
        "failed": "失败",
        "canceled": "已取消",
        "skipped": "已跳过",
    }.get(p["status"], "未知")
    if blocking:
        status = "失败"
    source = {
        "merge_request_event": "MR 检查",
        "push": "Push",
        "web": "手动触发",
        "schedule": "定时任务",
        "api": "API",
        "trigger": "Trigger",
    }.get(p["source"], p["source"])
    finished = datetime.fromtimestamp(p["finished"], get_timezone({"TIMEZONE": record.get("timezone")}))
    title = f"{config['project_path']} · 流水线耗时"
    elements = [md(text(greeting))] if greeting else []
    status_color = (
        "green" if status == "成功" else "red" if status == "失败" else "neutral"
    )
    speed, speed_color, speed_detail = speed_label(
        p, p.get("thresholds", config["thresholds"])
    )
    elements.append(
        md(
            f"{link('#' + str(p['pipeline_id']), p.get('web_url'))} · {text(source)} · <text_tag color='{status_color}'>{status}</text_tag>"
            + (" · 有非阻塞异常" if allowed else "")
            + "\n"
            + f"耗时对比：<text_tag color='{speed_color}'>{speed}</text_tag>"
            + (" · " + speed_detail if speed_detail else "")
            + "\n"
            + f"{text(p.get('matched_branch') or p['ref'])}{' · Tag' if p.get('tag') else ''} · 第 {p['sequence']} 次执行"
        )
    )
    elements.append(
        md(
            f"**有效执行 {duration(p['active_seconds'])}** · 最长 Job 排队 {duration(p['queue_max'])}"
        )
    )
    elements.extend(execution_chart(p))
    attention = []
    if blocking:
        attention.append(f"{blocking} 项必过任务失败")
    if allowed:
        attention.append(f"{len(allowed)} 项非阻塞异常")
    if p["alerts"]:
        attention.append(f"{len(p['alerts'])} 项耗时告警")
    if p["status"] == "failed" and not failed:
        attention.append("Pipeline 失败，未返回失败 Job")
    if p["completeness"] != "complete":
        attention.append("数据不完整")
    if attention:
        elements.append(md("**需要关注** · " + "；".join(attention)))
    elements.append(history_chart(p, p.get("thresholds", config["thresholds"])))
    evidence = []
    for a in p["alerts"][:15]:
        reference = (
            f"阈值 {duration(a['threshold'])}"
            if "threshold" in a
            else f"历史中位数 {duration(a['median'])} / {a['samples']} 次"
        )
        target = f" · Job #{a['job_id']}" if a.get("job_id") else ""
        evidence.append(
            md(f"• {text(a['type'])}{target} · {duration(a['actual'])} / {reference}")
        )
    for reason in p["reasons"][:15]:
        evidence.append(
            md(
                f"• {link('Job ' + str(reason['job_id']), reason.get('evidence'))} · {text(reason['category'])}"
                f"{'（非阻塞）' if reason.get('allow_failure') else ''}\n{text(reason['explanation'])}"
            )
        )
    if len(p["alerts"]) > 15 or len(p["reasons"]) > 15:
        evidence.append(md("其余异常请查看完整报告。"))
    if evidence:
        elements.append(panel("告警与失败原因", evidence))
    elements.extend(job_section(p))
    method = [
        md(
            f"完整历时 {duration(p['elapsed_seconds'])}；GitLab 排队 {duration(p['pipeline_queue'])}；Job 累计排队 {duration(p['queue_sum'])}。\n"
            "时间轴以本次执行最早已知 Job 开始为 0；每段按真实开始、结束偏移绘制，不将各阶段挪到 0。"
            "同阶段重叠区间合并，分离区间保留空档；各阶段重叠不能相加当作整体耗时。\n"
            "准备、编译测试、测试、检查、打包上传采用固定 stage 映射，未知 stage 归其他。"
            "阶段标 * 表示仅有部分起止时间；子 Pipeline 同名阶段按 Pipeline ID 分开。\n"
            f"数据完整性：{text(p['completeness'])}。历史只比较同分支/Tag、来源、状态、执行类型、配置及算法版本。"
        )
    ]
    if p["downstream"]:
        method.append(
            md(
                "下游："
                + " · ".join(
                    link("#" + str(d["id"]), d.get("web_url"))
                    for d in p["downstream"][:10]
                )
            )
        )
    elements.append(panel("统计口径", method))
    elements.append(
        md(
            link("查看 Pipeline", p.get("web_url"))
            + " · "
            + (link("查看长期报告", report_url) if report_url else "长期报告待发布"),
            text_size="notation",
        )
    )
    footer = f"📊 {finished.strftime('%Y-%m-%d %H:%M:%S')} · {bot_name(cfg)} · {get_timezone(cfg).key}"
    if greeting:
        footer += "\n" + message_signature(cfg)
    elements.append(
        md("<font color='grey'>" + text(footer) + "</font>", text_size="notation")
    )
    result = dict(
        schema="2.0",
        config=dict(
            update_multi=True,
            width_mode="default",
            summary=dict(
                content=f"{config['project_path']} #{p['pipeline_id']} · {status} · {duration(p['active_seconds'])}"
            ),
        ),
        header=dict(template="default", title=dict(tag="plain_text", content=title)),
        body=dict(
            direction="vertical",
            padding="8px 12px 12px 12px",
            vertical_spacing="8px",
            elements=elements,
        ),
    )
    return fit_card(result)
