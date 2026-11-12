/* Pure execution timeline model. All coordinates derive from actual timestamps. */
(function (root) {
  'use strict';
  const time = value => typeof value === 'string' && Number.isFinite(Date.parse(value)) ? Date.parse(value) / 1000 : null;
  function merge(intervals) {
    const result = [];
    for (const [a,b] of intervals.slice().sort((x,y) => x[0]-y[0])) {
      const last = result[result.length-1];
      if (last && a <= last[1]) last[1] = Math.max(last[1], b);
      else result.push([a,b]);
    }
    return result;
  }
  function model(record) {
    const groups = new Map(), seen = new Set(), starts = [], ends = [];
    for (const job of record.jobs || []) {
      const id = JSON.stringify([job.project_id,job.id]);
      if (seen.has(id)) continue;
      seen.add(id);
      const key = JSON.stringify([job.project_id,job.pipeline_id,job.stage || '未标注']);
      if (!groups.has(key)) groups.set(key, {key, project:job.project_id, pipeline:job.pipeline_id, name:job.stage || '未标注', jobs:[], intervals:[]});
      const group = groups.get(key), a = time(job.started_at), b = time(job.finished_at);
      const ran = a !== null || !['skipped','manual','created'].includes(job.status);
      const interval = ran && a !== null && b !== null && b >= a ? [a,b] : null;
      if (a !== null) starts.push(a);
      if (interval) {ends.push(b); group.intervals.push(interval);}
      group.jobs.push({job, interval, start:a, ran});
    }
    const stages = [...groups.values()];
    for (const g of stages) {
      const ranks = g.jobs.map(x => x.job.stage_index);
      g.rank = ranks.every(x => Number.isInteger(x) && x >= 0 && x === ranks[0]) ? ranks[0] : null;
      g.intervals = merge(g.intervals);
      g.seconds = g.intervals.length ? g.intervals.reduce((n,[a,b]) => n+b-a,0) : null;
      g.missing = g.jobs.filter(x => x.ran && !x.interval).length;
      g.jobs.sort((a,b) => (a.start ?? Infinity)-(b.start ?? Infinity) || a.job.id-b.job.id);
      g.fallback = g.name === '.pre' ? -1 : g.name === '.post' ? Infinity : Math.min(...g.jobs.map(x => x.job.id));
    }
    stages.sort((a,b) => Number(a.pipeline !== record.pipeline_id)-Number(b.pipeline !== record.pipeline_id)
      || (a.project||0)-(b.project||0) || (a.pipeline||0)-(b.pipeline||0)
      || Number(a.rank === null)-Number(b.rank === null) || (a.rank ?? a.fallback)-(b.rank ?? b.fallback));
    const origin = starts.length ? Math.min(...starts) : null;
    const span = origin !== null && ends.length ? Math.max(0,...ends.map(x=>x-origin)) : 0;
    return {stages, origin, span, extent:Math.max(span,1), orderKnown:stages.every(g=>g.rank !== null)};
  }
  function summary(stage) {
    const ran = stage.jobs.filter(x => x.ran);
    const valid = v => typeof v === 'number' && Number.isFinite(v) && v >= 0;
    const times = ran.filter(x => ['success','failed','canceled'].includes(x.job.status)).map(x=>x.job.duration).filter(valid);
    const queues = ran.map(x=>x.job.queued_duration).filter(valid);
    const sum = values => values.length ? values.reduce((a,b)=>a+b,0) : null;
    return {seconds:stage.seconds, total:sum(times), mean:times.length ? sum(times)/times.length : null,
      queue:sum(queues), samples:times.length, queueSamples:queues.length, ran:ran.length};
  }
  function defaultStage(stages) {
    return stages.reduce((best,stage) => best === null || (stage.seconds ?? 0) > (best.seconds ?? 0) ? stage : best,null);
  }
  const api = {model,merge,summary,defaultStage};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.PipelineTimeline = api;
})(globalThis);
