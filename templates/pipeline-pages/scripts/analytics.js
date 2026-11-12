/* Deterministic statistics shared by the report UI and Node tests. */
(function (root) {
  "use strict";
  const valid = (x) => typeof x === "number" && Number.isFinite(x) && x >= 0;
  function day(seconds, timeZone = "Asia/Shanghai") {
    if (!valid(seconds)) return "";
    const parts = new Intl.DateTimeFormat("en", {timeZone, year:"numeric", month:"2-digit", day:"2-digit"})
      .formatToParts(new Date(seconds * 1000));
    const value = type => parts.find(p => p.type === type).value;
    return `${value("year")}-${value("month")}-${value("day")}`;
  }
  function stats(values) {
    const known = values.filter(valid);
    return {
      mean: known.length
        ? known.reduce((a, b) => a + b, 0) / known.length
        : null,
      count: known.length,
      missing: values.length - known.length,
    };
  }
  function unique(records) {
    const seen = new Set();
    return records.filter((r) => {
      if (seen.has(r.execution_id)) return false;
      seen.add(r.execution_id);
      return true;
    });
  }
  function filter(records, f) {
    return records.filter(
      (r) =>
        valid(r.finished) &&
        (!f.start || day(r.finished, f.timezone || r.timezone) >= f.start) &&
        (!f.end || day(r.finished, f.timezone || r.timezone) <= f.end) &&
        r.status === f.status &&
        (!f.branch || (r.matched_branch || r.ref) === f.branch) &&
        (!f.source || r.source === f.source) &&
        (!f.alert ||
          (r.alerts || []).some(
            (a) => f.alert === "any" || a.type === f.alert,
          )) &&
        (!f.kind || r.kind === f.kind) &&
        (!f.reason || (r.reasons || []).some((x) => x.category === f.reason)),
    );
  }
  function key(r) {
    return JSON.stringify([
      r.matched_branch || r.ref,
      r.source,
      !!r.tag,
      r.kind,
      r.config_version,
      r.metric_version,
    ]);
  }
  function groups(records, metric = "active_seconds") {
    const map = new Map();
    for (const r of unique(records)) {
      const id = key(r);
      if (!map.has(id))
        map.set(id, {
          id,
          branch: r.matched_branch || r.ref,
          source: r.source,
          tag: !!r.tag,
          kind: r.kind,
          config: r.config_version,
          algorithm: r.metric_version,
          records: [],
        });
      map.get(id).records.push(r);
    }
    return [...map.values()]
      .map((g) => ({
        ...g,
        ...stats(g.records.map((r) => r[metric])),
        total: g.records.length,
      }))
      .sort((a, b) => (b.mean ?? -1) - (a.mean ?? -1));
  }
  function jobs(records, metric = "duration") {
    const map = new Map(),
      seen = new Set();
    for (const r of unique(records))
      for (const j of r.jobs || []) {
        if (!["success", "failed", "canceled"].includes(j.status)) continue;
        const attempt = JSON.stringify([j.project_id, j.id]);
        if (seen.has(attempt)) continue;
        seen.add(attempt);
        const id = JSON.stringify([j.project_id, j.stage, j.name, j.status]);
        if (!map.has(id))
          map.set(id, {
            id,
            name: j.name,
            stage: j.stage || "未标注阶段",
            status: j.status,
            project: j.project_id,
            jobs: [],
            executions: new Set(),
          });
        map.get(id).jobs.push(j);
        map.get(id).executions.add(r.execution_id);
      }
    return [...map.values()]
      .map((g) => ({
        ...g,
        ...stats(g.jobs.map((j) => j[metric])),
        total: g.jobs.length,
        executionCount: g.executions.size,
      }))
      .sort((a, b) => (b.mean ?? -1) - (a.mean ?? -1));
  }
  function daily(records, metric = "active_seconds", jobId = null, timeZone = null) {
    const map = new Map();
    for (const r of records) {
      const d = day(r.finished, timeZone || r.timezone);
      if (!d) continue;
      if (!map.has(d)) map.set(d, []);
      map.get(d).push(r);
    }
    return [...map]
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([date, rows]) => ({
        date,
        ...(jobId
          ? jobs(rows, metric).find((j) => j.id === jobId) || {
              mean: null,
              count: 0,
              missing: 0,
            }
          : stats(rows.map((r) => r[metric]))),
      }));
  }
  const api = { valid, day, stats, unique, filter, key, groups, jobs, daily };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.PipelineStats = api;
})(globalThis);
