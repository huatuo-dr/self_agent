/* Browser presentation: aggregate-first navigation, execution details on demand. */
(() => {
  "use strict";
  const A = PipelineStats,
    $ = (id) => document.getElementById(id),
    state = {
      tasks: [],
      timezone: "Asia/Shanghai",
      task: null,
      records: [],
      groups: [],
      selected: null,
      job: null,
      gp: 0,
      jp: 0,
      rp: 0,
      version: 0,
    };
  const sourceNames = {
    merge_request_event: "MR 检查",
    push: "Push",
    schedule: "定时任务",
    web: "手动触发",
    api: "API",
    trigger: "Trigger",
    pipeline: "下游触发",
    parent_pipeline: "子流水线",
  };
  const format = (v) =>
    A.valid(v)
      ? v < 60
        ? `${v.toFixed(1)} 秒`
        : `${(v / 60).toFixed(1)} 分钟`
      : "无有效数据";
  const statusName = (s) =>
    ({
      success: "成功",
      failed: "失败",
      canceled: "已取消",
      skipped: "已跳过",
    })[s] ||
    s ||
    "未知";
  const label = (g) =>
    `${g.tag ? "Tag " : ""}${g.branch || "未知分支"} · ${sourceNames[g.source] || g.source || "未知来源"}`;
  const version = (g) => {
    const peers = state.groups.filter(
      (x) =>
        x.branch === g.branch &&
        x.source === g.source &&
        x.tag === g.tag &&
        x.kind === g.kind,
    );
    return (
      (g.kind === "partial_retry" ? "重试执行" : "首次执行") +
      (peers.length > 1 ? ` · 配置 v${g.config} / 算法 v${g.algorithm}` : "")
    );
  };
  const el = (tag, text, cls) => {
    const n = document.createElement(tag);
    if (text !== undefined) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  };
  const empty = (node, msg) => {
    node.replaceChildren(el("div", msg, "empty"));
  };
  function link(text, url) {
    try {
      const u = new URL(url);
      if (u.protocol !== "https:" || u.username || u.password) throw Error();
      const a = el("a", text);
      a.href = u.href;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      return a;
    } catch {
      return el("span", text);
    }
  }
  function options(node, values, first) {
    const previous = node.value;
    node.replaceChildren(new Option(first, ""));
    values.forEach((v) => node.add(new Option(v.label ?? v, v.value ?? v)));
    if ([...node.options].some((o) => o.value === previous))
      node.value = previous;
  }
  async function get(url) {
    const r = await fetch(url, { cache: "no-cache" });
    if (!r.ok) throw Error(`报告文件读取失败（HTTP ${r.status}）`);
    return r.json();
  }
  function dates() {
    if ($("period").value === "custom") return;
    const end = new Date();
    $("end").value = A.day(end.getTime() / 1000, state.timezone);
    $("start").value =
      $("period").value === "all"
        ? ""
        : new Date(Date.parse($("end").value + "T00:00:00Z") - (Number($("period").value) - 1) * 86400000).toISOString().slice(0, 10);
  }
  function error(message) {
    $("error").textContent = message;
    $("error").hidden = !message;
  }
  function filters() {
    return {
      start: $("start").value,
      end: $("end").value,
      status: $("status").value,
      branch: $("branch").value,
      source: $("source").value,
      kind: $("kind").value,
      alert: $("alert").value,
      reason: $("status").value === "failed" ? $("reason").value : "",
    };
  }
  function chosen() {
    return state.groups.find((g) => g.id === state.selected);
  }
  function pager(prefix, page, total, size) {
    const pages = Math.max(1, Math.ceil(total / size));
    $(prefix + "-page").textContent = `${page + 1} / ${pages}`;
    $(prefix + "-prev").disabled = page === 0;
    $(prefix + "-next").disabled = page + 1 >= pages;
  }
  function bars(node, rows, page, size, max, selected, onSelect, job = false) {
    node.replaceChildren();
    if (!rows.length) {
      empty(
        node,
        job ? "所选分组没有符合条件的 Job 数据。" : "当前筛选没有执行记录。",
      );
      return;
    }
    for (const r of rows.slice(page * size, (page + 1) * size)) {
      const b = el(
        "button",
        undefined,
        "bar-row" + (r.id === selected ? " selected" : ""),
      );
      b.type = "button";
      b.setAttribute("aria-pressed", String(r.id === selected));
      const name = el("span", job ? r.name : label(r), "bar-name");
      name.append(
        el("small", job ? `${r.stage} · ${statusName(r.status)}` : version(r)),
      );
      const track = el("div", undefined, "track"),
        fill = el("div", undefined, job ? "fill job-fill" : "fill");
      fill.style.width = `${max && r.mean !== null ? Math.max(0, (100 * r.mean) / max) : 0}%`;
      track.append(fill);
      const number = el("span", format(r.mean), "bar-number");
      number.append(
        el(
          "small",
          `n=${r.count}${r.count > 0 && r.count < 5 ? " · 小样本" : ""}`,
        ),
      );
      b.title = `${job ? r.name : label(r)}；平均 ${format(r.mean)}；有效样本 ${r.count}；缺失 ${r.missing}；总样本 ${r.total}`;
      b.append(name, track, number);
      b.onclick = () => onSelect(r);
      node.append(b);
    }
  }
  function render() {
    if (state.loading || $("error").textContent) return;
    const f = filters();
    state.filtered = A.filter(state.records, {...f, timezone: state.timezone});
    state.groups = A.groups(state.filtered, $("pipeline-metric").value);
    if (!state.groups.some((g) => g.id === state.selected)) {
      state.selected =
        [...state.groups].sort((a, b) => b.count - a.count)[0]?.id || null;
      state.job = null;
    }
    state.gp = Math.min(
      state.gp,
      Math.max(0, Math.ceil(state.groups.length / 10) - 1),
    );
    $("content").hidden = false;
    $("runs").textContent = state.filtered.length;
    $("sample-note").textContent =
      `${f.status === "success" ? "成功" : "失败"} · ${f.start || "全部历史"} 至 ${f.end || "今天"}`;
    $("group-count").textContent = state.groups.length;
    const slow = A.groups(state.filtered, "active_seconds").find(
      (g) => g.mean !== null,
    );
    $("slowest").textContent = format(slow?.mean);
    $("slowest-name").textContent = slow
      ? label(slow) + " · n=" + slow.count
      : "暂无有效执行时长";
    const g = chosen(),
      s = A.stats((g?.records || []).map((r) => r.active_seconds));
    $("selected-mean").textContent = format(s.mean);
    $("selected-samples").textContent =
      `n=${s.count} · ${s.missing} 条执行时长缺失`;
    const max = Math.max(0, ...state.groups.map((g) => g.mean || 0));
    bars(
      $("pipelines"),
      state.groups,
      state.gp,
      10,
      max,
      state.selected,
      (r) => {
        state.selected = r.id;
        state.job = null;
        state.jp = state.rp = 0;
        $("stage").value = "";
        $("job-search").value = "";
        render();
      },
    );
    $("pipeline-max").textContent = format(max);
    pager("group", state.gp, state.groups.length, 10);
    $("selection").textContent = g
      ? `${label(g)} · ${version(g)}`
      : "先选择一个 Pipeline 分组";
    const allJobs = A.jobs(g?.records || [], $("job-metric").value);
    options(
      $("stage"),
      [...new Set(allJobs.map((j) => j.stage))].sort(),
      "全部阶段",
    );
    state.jobs = allJobs.filter(
      (j) =>
        (!$("stage").value || j.stage === $("stage").value) &&
        j.name.toLowerCase().includes($("job-search").value.toLowerCase()),
    );
    if (!state.jobs.some((j) => j.id === state.job)) state.job = null;
    state.jp = Math.min(
      state.jp,
      Math.max(0, Math.ceil(state.jobs.length / 10) - 1),
    );
    const jm = Math.max(0, ...state.jobs.map((j) => j.mean || 0));
    bars(
      $("jobs"),
      state.jobs,
      state.jp,
      10,
      jm,
      state.job,
      (r) => {
        state.job = r.id;
        render();
      },
      true,
    );
    $("job-max").textContent = format(jm);
    pager("job", state.jp, state.jobs.length, 10);
    $("clear-job").hidden = !state.job;
    trend(g);
    records(g);
  }
  function svg(tag, attrs = {}, text) {
    const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function trend(g) {
    const node = $("trend");
    node.replaceChildren();
    $("trend-point").textContent = "";
    $("job-legend").hidden = !state.job;
    $("pipeline-legend").textContent =
      "Pipeline · " + $("pipeline-metric").selectedOptions[0].textContent;
    $("job-legend-label").textContent =
      "Job · " + $("job-metric").selectedOptions[0].textContent;
    const job = state.jobs.find((j) => j.id === state.job);
    $("trend-description").textContent = g
      ? `${label(g)}${job ? " / " + job.name : ""} · 每日算术平均`
      : "选择分组后查看趋势";
    const series = [
      {
        name: "Pipeline",
        rows: A.daily(g?.records || [], $("pipeline-metric").value, null, state.timezone),
        job: false,
      },
    ];
    if (job)
      series.push({
        name: job.name,
        rows: A.daily(g.records, $("job-metric").value, job.id, state.timezone),
        job: true,
      });
    const points = series.flatMap((s) => s.rows).filter((p) => p.mean !== null);
    if (!points.length) {
      empty(node, "没有有效时长，暂不能绘制趋势。");
      return;
    }
    const dateNumber = (d) => Date.parse(d + "T00:00:00+08:00") / 86400000;
    const dates = points.map((p) => dateNumber(p.date));
    let lo = Math.min(...dates),
      hi = Math.max(...dates);
    const max = Math.max(1, ...points.map((p) => p.mean / 60));
    const W = Math.max(300, node.clientWidth),
      H = 220,
      left = 48,
      right = W - 20,
      x = (d) =>
        lo === hi
          ? (left + right) / 2
          : left + ((dateNumber(d) - lo) / (hi - lo)) * (right - left),
      y = (v) => 180 - (v / 60 / max) * 150;
    const canvas = svg("svg", {
      viewBox: `0 0 ${W} ${H}`,
      class: "trend",
      role: "img",
      "aria-label": "每日平均耗时趋势，单位分钟",
    });
    for (let i = 0; i < 4; i++) {
      const yy = 180 - i * 50;
      canvas.append(
        svg("line", { x1: left, y1: yy, x2: right, y2: yy, class: "grid" }),
      );
      canvas.append(
        svg(
          "text",
          { x: left - 8, y: yy + 4, "text-anchor": "end" },
          ((i * max) / 3).toFixed(1),
        ),
      );
    }
    canvas.append(svg("text", { x: 15, y: 17 }, "分钟"));
    const labels = [...new Set(points.map((p) => p.date))].sort();
    for (const idx of [
      ...new Set([0, Math.floor((labels.length - 1) / 2), labels.length - 1]),
    ])
      canvas.append(
        svg(
          "text",
          { x: x(labels[idx]), y: 210, "text-anchor": "middle" },
          labels[idx],
        ),
      );
    for (const seriesItem of series) {
      let path = "",
        previous = null;
      for (const p of seriesItem.rows) {
        if (p.mean === null) {
          previous = null;
          continue;
        }
        path += `${previous !== null && dateNumber(p.date) - previous === 1 ? " L" : " M"}${x(p.date)},${y(p.mean)}`;
        previous = dateNumber(p.date);
      }
      canvas.append(
        svg("path", {
          d: path,
          class: "line" + (seriesItem.job ? " job-line" : ""),
        }),
      );
      for (const p of seriesItem.rows.filter((p) => p.mean !== null)) {
        const desc = `${p.date} · ${seriesItem.name} · 平均 ${format(p.mean)} · n=${p.count}`;
        const circle = svg("circle", {
          cx: x(p.date),
          cy: y(p.mean),
          r: 4,
          fill: seriesItem.job ? "#399ca4" : "#4678da",
          tabindex: 0,
          role: "button",
          "aria-label": desc,
        });
        circle.append(svg("title", {}, desc));
        circle.onclick = () => {
          $("trend-point").textContent = desc;
        };
        circle.onfocus = circle.onclick;
        circle.onkeydown = (e) => {
          if (e.key === "Enter" || e.key === " ") circle.onclick();
        };
        canvas.append(circle);
      }
    }
    node.append(canvas);
  }
  function records(g) {
    const rows = [...(g?.records || [])].sort(
      (a, b) => b.finished - a.finished,
    );
    state.rp = Math.min(state.rp, Math.max(0, Math.ceil(rows.length / 20) - 1));
    $("execution-count").textContent = `${rows.length} 条`;
    $("records").replaceChildren();
    for (const r of rows.slice(state.rp * 20, (state.rp + 1) * 20)) {
      const tr = el("tr"),
        first = el("td");
      first.append(link(`#${r.pipeline_id} · 第 ${r.sequence} 次`, r.web_url));
      tr.append(first);
      for (const value of [
        new Date(r.finished * 1000).toLocaleString("zh-CN", {
          timeZone: state.timezone,
          hour12: false,
        }),
        format(r.active_seconds),
        format(r.elapsed_seconds),
        format(r.pipeline_queue),
        statusName(r.status),
      ])
        tr.append(el("td", value));
      const td = el("td"),
        b = el("button", "详情");
      b.onclick = () => detail(r);
      td.append(b);
      tr.append(td);
      $("records").append(tr);
    }
    pager("record", state.rp, rows.length, 20);
  }
  function executionTimeline(record) {
    const data = PipelineTimeline.model(record), wrap = el("section", undefined, "execution-analysis");
    const stages = el("section", undefined, "stage-overview");
    stages.append(el("h3", "阶段耗时统计"));
    stages.append(el("p", "按真实开始时间绘制，阶段耗时为并行 Job 运行区间的并集；点击阶段查看下方 Job。" +
      (data.orderKnown ? "" : " 历史阶段顺序未核实，按 Job 创建顺序显示（.pre 首、.post 尾）。"), "notice"));
    const date = value => new Date(value * 1000).toLocaleString("zh-CN", {timeZone:state.timezone,hour12:false});
    stages.append(el("p", data.origin === null ? "缺少开始时间，保留耗时明细，不推算条形位置。" : "时间原点：" + date(data.origin) + " " + state.timezone, "notice"));
    wrap.append(stages);
    if (!data.stages.length) {stages.append(el("div", "暂无阶段及 Job 数据。", "empty"));return wrap;}
    const palette = [
      ["准备", "#A77952", [".pre","prepare","init","validate","setup"]],
      ["编译测试", "#3478E5", ["build","build_test","compile"]],
      ["测试", "#9860C6", ["test","tests","e2e_test","device_test","integration","regression"]],
      ["检查", "#22A6A8", ["analysis","quality-gate","quality_gate","issue_check",".post","check","lint"]],
      ["打包上传", "#5AAB62", ["package","publish","deploy","release","upload"]],
      ["其他", "#697586", []],
    ];
    const phase = stage => palette.find(p=>p[2].includes(stage)) || palette[5];
    const mainProject = data.stages.find(g=>g.pipeline===record.pipeline_id)?.project;
    const stageName = g => g.name + (g.pipeline !== record.pipeline_id ? " · Pipeline #" + g.pipeline : "") +
      (g.project !== mainProject && g.project != null ? " · 项目 #" + g.project : "");
    const legend = el("div", undefined, "legend");
    for (const p of palette.filter(p=>data.stages.some(g=>phase(g.name)===p))) {
      const item = el("span", p[0]), dot = el("i", undefined, "dot");dot.style.background=p[1];item.prepend(dot);legend.append(item);
    }
    stages.append(legend);
    function track(intervals, color, description, failed=false, emptyText="无完整起止时间") {
      const lane = el("div", undefined, "tl-track");
      for (const [a,b] of intervals) {
        const bar = el("span", undefined, "tl-bar" + (failed ? " failed" : ""));
        bar.style.left = (a-data.origin)/data.extent*100 + "%";
        bar.style.width = (b-a)/data.extent*100 + "%";
        bar.style.backgroundColor = color;
        bar.title = description + "\n开始：" + date(a) + "\n结束：" + date(b) + "（" + state.timezone + "）";
        lane.append(bar);
      }
      if (!intervals.length) lane.append(el("span",emptyText,"tl-unknown"));
      return lane;
    }
    function timeline(parent) {
      const scroll=el("div",undefined,"execution-timeline"),grid=el("div",undefined,"tl-grid");
      scroll.append(grid);parent.append(scroll);return grid;
    }
    function axis(grid) {
      const row=el("div",undefined,"tl-row"),ticks=el("div",undefined,"tl-axis");
      for(let i=0;i<=4;i++)ticks.append(el("span",(data.extent*i/4/60).toFixed(1)));
      row.append(el("span"),ticks,el("span"));grid.append(row,el("p","距执行开始（分钟）","tl-caption"));
    }
    const grid=timeline(stages), buttons=new Map();
    const jobs=el("section",undefined,"selected-jobs"),heading=el("h3","Job 耗时详情");
    const toolbar=el("div",undefined,"toolbar"),label=el("label","选择阶段 "),select=el("select");
    select.id="detail-stage";select.setAttribute("aria-label","Job 详情阶段");label.append(select);toolbar.append(label);
    for (const g of data.stages) select.add(new Option(stageName(g)+" · "+g.jobs.length+" 个 Job",g.key));
    const view=el("div");view.setAttribute("aria-live","polite");
    jobs.append(heading,toolbar,view);wrap.append(jobs);
    let selected=PipelineTimeline.defaultStage(data.stages),page=0;
    function choose(g) {selected=g;page=0;renderJobs();}
    for (const g of data.stages) {
      const row=el("div",undefined,"tl-row"),name=el("button",stageName(g),"tl-name");
      name.type="button";name.title=stageName(g)+" · "+g.jobs.length+" 个 Job";
      name.setAttribute("aria-controls","detail-job-timeline");name.onclick=()=>choose(g);buttons.set(g.key,name);
      row.append(name,track(g.intervals,phase(g.name)[1],name.title+"\n阶段执行："+format(g.seconds)),el("span",format(g.seconds)+(g.missing?" *":""),"tl-time"));
      grid.append(row);
    }
    axis(grid);
    stages.append(el("p","* 表示阶段缺少部分起止时间，仅统计已知区间。", "notice"));
    function renderJobs() {
      const g=selected,summary=PipelineTimeline.summary(g),size=12,shown=g.jobs.slice(page*size,(page+1)*size);
      select.value=g.key;
      for(const [key,button] of buttons) {button.setAttribute("aria-pressed",String(key===g.key));button.closest(".tl-row").classList.toggle("selected",key===g.key);}
      heading.textContent="Job 耗时详情 · "+stageName(g);
      view.replaceChildren();
      const stats=el("div",undefined,"detail-grid stage-stats");
      for(const [name,value] of [["阶段执行"+(g.missing?"（已知区间）":""),summary.seconds],["Job 执行合计",summary.total],["每 Job 平均",summary.mean],["Job 排队合计",summary.queue]]) {
        const cell=el("div",name);cell.append(el("strong",format(value)));stats.append(cell);
      }
      view.append(stats,el("p",`运行 Job ${summary.ran} 个；执行耗时有效 ${summary.samples} 个，排队已知 ${summary.queueSamples} 个。并行 Job 的合计不等于阶段执行。`,"notice"));
      const jobGrid=timeline(view);jobGrid.parentElement.id="detail-job-timeline";
      for(const x of shown) {
        const j=x.job,row=el("div",undefined,"tl-row tl-job"),name=link((j.status==="failed"?"× ":"")+j.name,j.web_url);
        name.className="tl-name";name.title=j.name+" #"+j.id+" · "+statusName(j.status)+(j.allow_failure&&j.status==="failed"?"（非阻塞）":"")+"\n执行："+format(j.duration)+" · 排队："+format(j.queued_duration);
        row.append(name,track(x.interval?[x.interval]:[],phase(g.name)[1],name.title,j.status==="failed",x.ran?"缺少完整起止":"未运行"),el("span",format(j.duration),"tl-time"));jobGrid.append(row);
      }
      axis(jobGrid);
      view.append(el("p",`本页 ${shown.length} / 本阶段 ${g.jobs.length} 个 Job；按开始时间排列，与阶段总览共用时间原点及刻度。× 和红框表示失败。`,"notice"));
      if(g.jobs.length>size) {
        const pager=el("div",undefined,"pager"),previous=el("button","上一页"),next=el("button","下一页");
        previous.disabled=page===0;next.disabled=(page+1)*size>=g.jobs.length;
        previous.onclick=()=>{page--;renderJobs();};next.onclick=()=>{page++;renderJobs();};
        pager.append(previous,el("small",`${page+1} / ${Math.ceil(g.jobs.length/size)}`),next);view.append(pager);
      }
      const details=el("details",undefined,"job-detail-list"),scroll=el("div",undefined,"table-scroll"),table=el("table"),head=el("tr");
      details.append(el("summary",`所选阶段 Job 明细 · 本页 ${shown.length} 项`));
      for(const title of ["Job","状态","执行","排队"]) head.append(el("th",title));table.append(head);
      for(const x of shown) {
        const j=x.job,row=el("tr"),name=el("td");name.append(link(j.name+" #"+j.id,j.web_url));row.append(name);
        for(const text of [statusName(j.status)+(j.allow_failure&&j.status==="failed"?"（非阻塞）":""),format(j.duration),format(j.queued_duration)])row.append(el("td",text));
        table.append(row);
      }
      scroll.append(table);details.append(scroll);view.append(details);
    }
    select.onchange=()=>choose(data.stages.find(g=>g.key===select.value));
    renderJobs();return wrap;
  }

  function detail(r) {
    $("detail-title").textContent =
      `Pipeline #${r.pipeline_id} · 第 ${r.sequence} 次`;
    const body = $("detail-body");
    body.replaceChildren();
    body.append(link("在 GitLab 查看原始 Pipeline", r.web_url));
    const metrics = el("div", undefined, "detail-grid");
    for (const [name, value] of [
      ["有效执行", r.active_seconds],
      ["完整历时", r.elapsed_seconds],
      ["GitLab 排队", r.pipeline_queue],
    ]) {
      const cell = el("div", name);
      cell.append(el("strong", format(value)));
      metrics.append(cell);
    }
    body.append(
      metrics,
      el("p", `完整性：${r.completeness || "未知"}`, "notice"),
    );
    body.append(executionTimeline(r));
    if (r.alerts?.length) {
      body.append(el("h3", "耗时告警"));
      for (const a of r.alerts)
        body.append(
          el(
            "p",
            `${a.type} · ${format(a.actual)}${a.threshold !== undefined ? " / 阈值 " + format(a.threshold) : ""}`,
          ),
        );
    }
    if (r.reasons?.length) {
      body.append(el("h3", "失败原因"));
      for (const reason of r.reasons) {
        const p = el(
          "p",
          `${reason.category} · ${reason.explanation}${reason.allow_failure ? "（非阻塞）" : ""} `,
        );
        p.append(link(`Job #${reason.job_id}`, reason.evidence));
        body.append(p);
      }
    }
    $("detail").showModal();
  }
  async function load() {
    const current = ++state.version;
    state.loading = true;
    state.records = [];
    error("");
    $("content").hidden = true;
    $("busy").textContent = "正在读取数据…";
    $("apply").disabled = true;
    try {
      const task = state.tasks.find((t) => t.task_id === $("task").value);
      if (!task) return;
      state.task = task;
      if (
        $("start").value &&
        $("end").value &&
        $("start").value > $("end").value
      )
        throw Error("开始日期不能晚于结束日期。");
      const prefix = TASK_ID ? "../../" : "";
      const base = prefix + "tasks/" + task.task_id + "/";
      const manifest = await get(base + "manifest.json");
      if (current !== state.version) return;
      const displayTimezone = manifest.timezone || "Asia/Shanghai";
      new Intl.DateTimeFormat("en", {timeZone: displayTimezone});
      state.timezone = displayTimezone;
      dates();
      document.querySelectorAll("[data-timezone]").forEach(node => { node.textContent = state.timezone; });
      const months = manifest.months.filter(
        (m) =>
          (!$("start").value || m >= $("start").value.slice(0, 7)) &&
          (!$("end").value || m <= $("end").value.slice(0, 7)),
      );
      const batches = await Promise.all(
        months.map((m) => get(base + m + ".json")),
      );
      if (current !== state.version) return;
      if (batches.some((b) => !Array.isArray(b)))
        throw Error("报告数据格式不正确。");
      state.records = A.unique(batches.flat()).map(r => ({...r, timezone: displayTimezone}));
      state.selected = state.job = null;
      state.gp = state.jp = state.rp = 0;
      options(
        $("branch"),
        [
          ...new Set(
            state.records.map((r) => r.matched_branch || r.ref).filter(Boolean),
          ),
        ].sort(),
        "全部分支 / Tag",
      );
      options(
        $("source"),
        [...new Set(state.records.map((r) => r.source).filter(Boolean))]
          .sort()
          .map((s) => ({ value: s, label: sourceNames[s] || s })),
        "全部触发类型",
      );
      options(
        $("reason"),
        [
          ...new Set(
            state.records.flatMap((r) =>
              (r.reasons || []).map((x) => x.category),
            ),
          ),
        ].sort(),
        "全部原因",
      );
      $("task-state").textContent =
        manifest.state === "paused"
          ? "监控已暂停 · 历史可查看"
          : manifest.state === "active"
            ? "监控运行中"
            : manifest.state;
      state.loading = false;
      $("updated").textContent =
        "数据发布于 " +
        new Date(manifest.updated * 1000).toLocaleString("zh-CN", {
          timeZone: state.timezone,
          hour12: false,
        });
      render();
    } catch (e) {
      if (current === state.version)
        error(e.message || "加载失败，请稍后重试。");
    } finally {
      if (current === state.version) {
        state.loading = false;
        $("busy").textContent = "";
        $("apply").disabled = false;
      }
    }
  }
  $("period").onchange = () => {
    dates();
    load();
  };
  for (const id of ["start", "end"])
    $(id).onchange = () => {
      $("period").value = "custom";
      load();
    };
  $("apply").onclick = load;
  $("task").onchange = () => {
    $("branch").value = $("source").value = $("reason").value = "";
    load();
  };
  for (const id of [
    "status",
    "kind",
    "branch",
    "source",
    "reason",
    "alert",
    "pipeline-metric",
    "job-metric",
    "stage",
  ])
    $(id).onchange = () => {
      $("reason-label").hidden = $("status").value !== "failed";
      state.gp = state.jp = state.rp = 0;
      render();
    };
  $("job-search").oninput = () => {
    state.jp = 0;
    render();
  };
  $("clear-job").onclick = () => {
    state.job = null;
    render();
  };
  for (const [prefix, key] of [
    ["group", "gp"],
    ["job", "jp"],
    ["record", "rp"],
  ]) {
    $(prefix + "-prev").onclick = () => {
      state[key] = Math.max(0, state[key] - 1);
      render();
    };
    $(prefix + "-next").onclick = () => {
      state[key]++;
      render();
    };
  }
  $("close-detail").onclick = () => $("detail").close();
  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (!state.loading && !$("content").hidden) trend(chosen());
    }, 100);
  });
  (async () => {
    dates();
    try {
      const index = await get((TASK_ID ? "../../" : "") + "manifest.json");
      state.tasks = index.tasks || [];
      if (!state.tasks.length) {
        $("empty-site").hidden = false;
        $("task-state").textContent = "暂无任务";
        return;
      }
      for (const t of state.tasks)
        $("task").add(
          new Option(`${t.project_path} · ${t.task_id.slice(0, 6)}`, t.task_id),
        );
      if (TASK_ID) {
        if (!state.tasks.some((t) => t.task_id === TASK_ID))
          throw Error("该任务已不在报告清单中。");
        $("task").value = TASK_ID;
      }
      await load();
    } catch (e) {
      error(e.message);
    }
  })();
})();
