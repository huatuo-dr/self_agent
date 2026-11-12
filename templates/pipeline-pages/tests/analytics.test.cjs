const { test } = require("node:test");
const assert = require("node:assert/strict");
const A = require("../scripts/analytics.js");
const base = {
  execution_id: "a",
  finished: Date.parse("2026-11-04T16:00:00Z") / 1000,
  matched_branch: "main",
  source: "push",
  status: "success",
  kind: "initial",
  config_version: 1,
  metric_version: 1,
  active_seconds: 60,
  tag: false,
  jobs: [],
};
const r = (id, extra = {}) => ({ ...base, execution_id: id, ...extra });
test("arithmetic mean uses samples, not mean of daily means; missing is not zero", () => {
  const rows = [
    r("1"),
    r("2", { active_seconds: 120 }),
    r("3", { active_seconds: null }),
    r("4", { active_seconds: 0 }),
  ];
  const g = A.groups(rows)[0];
  assert.equal(g.mean, 60);
  assert.equal(g.count, 3);
  assert.equal(g.missing, 1);
  assert.equal(g.total, 4);
  assert.equal(A.stats([null, NaN, Infinity, -1]).mean, null);
});
test("success and failure, branch and source filters are explicit", () => {
  const rows = [
    r("1"),
    r("2", { status: "failed" }),
    r("3", { source: "merge_request_event" }),
    r("4", { matched_branch: "release" }),
  ];
  assert.equal(
    A.filter(rows, { status: "success", source: "push", branch: "main" })
      .length,
    1,
  );
  assert.equal(A.filter(rows, { status: "failed" }).length, 1);
  assert.equal(A.groups(rows.filter((r) => r.status === "success")).length, 3);
});
test("tag/retry/config/algorithm groups never mix; duplicate execution ignored", () => {
  const rows = [
    r("1"),
    r("1"),
    r("2", { tag: true }),
    r("3", { kind: "partial_retry" }),
    r("4", { config_version: 2 }),
    r("5", { metric_version: 2 }),
  ];
  const groups = A.groups(rows);
  assert.equal(groups.length, 5);
  assert.equal(
    groups.reduce((n, g) => n + g.total, 0),
    5,
  );
});
test("Shanghai day boundaries and inclusive end date", () => {
  assert.equal(A.day(base.finished), "2026-11-05");
  const rows = [
    r("1"),
    r("2", { finished: base.finished - 1 }),
    r("3", { finished: base.finished + 86400 }),
  ];
  assert.deepEqual(
    A.filter(rows, {
      status: "success",
      start: "2026-11-05",
      end: "2026-11-05",
    }).map((x) => x.execution_id),
    ["1"],
  );
});
test("job mean separates status/stage/project/matrix names and deduplicates attempts", () => {
  const j = {
    id: 1,
    project_id: 2,
    name: "build: [arm]",
    stage: "build",
    status: "success",
    duration: 60,
    queued_duration: null,
  };
  const rows = [
    r("1", { jobs: [j, { ...j, id: 2, duration: 120 }] }),
    r("2", {
      jobs: [
        j,
        { ...j, id: 3, status: "failed", duration: 5 },
        { ...j, id: 4, stage: "test" },
        { ...j, id: 5, name: "build: [x86]" },
        { ...j, project_id: 3 },
      ],
    }),
  ];
  const jobs = A.jobs(rows);
  assert.equal(jobs.length, 5);
  const main = jobs.find(
    (x) =>
      x.project === 2 &&
      x.stage === "build" &&
      x.status === "success" &&
      x.name === j.name,
  );
  assert.equal(main.mean, 90);
  assert.equal(main.count, 2);
  assert.equal(A.jobs(rows, "queued_duration")[0].mean, null);
});
test("unrun jobs and unknown historical attempts do not enter average", () => {
  const rows = [
    r("1", {
      jobs: [{ id: 1, name: "x", status: "skipped", duration: 0 }],
      historical_attempts: [
        { id: 2, name: "x", status: "failed", duration: 5 },
      ],
    }),
  ];
  assert.equal(A.jobs(rows).length, 0);
});
test("daily mean and failure reason filtering keep their own denominators", () => {
  const rows = [
    r("1"),
    r("2", { active_seconds: 120 }),
    r("3", { active_seconds: null }),
    r("4", { finished: base.finished + 172800, active_seconds: 300 }),
  ];
  const daily = A.daily(rows);
  assert.equal(daily.length, 2);
  assert.equal(daily[0].mean, 90);
  assert.equal(daily[0].count, 2);
  assert.equal(daily[1].mean, 300);
  assert.equal(A.groups(rows)[0].mean, 160);
  assert.equal(
    A.filter(
      [r("x", { status: "failed", reasons: [{ category: "测试失败" }] })],
      { status: "failed", reason: "测试失败" },
    ).length,
    1,
  );
});

test("alerts filter narrows both aggregate and execution samples", () => {
  const rows = [
    r("1", { alerts: [{ type: "执行超时" }] }),
    r("2", { alerts: [{ type: "排队超时" }] }),
    r("3"),
  ];
  assert.equal(A.filter(rows, { status: "success", alert: "any" }).length, 2);
  assert.deepEqual(
    A.filter(rows, { status: "success", alert: "排队超时" }).map(
      (x) => x.execution_id,
    ),
    ["2"],
  );
});

const T = require('../scripts/timeline.js');
const tj = (id, stage, a, b, extra={}) => ({id,project_id:1,pipeline_id:7,name:`job-${id}`,stage,status:'success',started_at:a===null?null:new Date(a*1000).toISOString(),finished_at:b===null?null:new Date(b*1000).toISOString(),...extra});
test('execution timeline keeps defined order, actual offsets, parallel union and gaps',()=>{
 const a=tj(3,'build',1000,1060,{stage_index:1});
 const m=T.model({pipeline_id:7,jobs:[a,a,tj(4,'build',1030,1090,{stage_index:1}),tj(5,'build',1120,1150,{stage_index:1}),tj(1,'.pre',1005,1020,{stage_index:0})]});
 assert.deepEqual(m.stages.map(g=>g.name),['.pre','build']);assert.equal(m.origin,1000);assert.equal(m.span,150);
 assert.deepEqual(m.stages[1].intervals,[[1000,1090],[1120,1150]]);assert.equal(m.stages[1].seconds,120);assert.equal(m.stages[1].jobs.length,3);assert.equal(m.orderKnown,true);
});
test('execution timeline separates children and preserves missing, zero and skipped jobs',()=>{
 const m=T.model({pipeline_id:7,jobs:[tj(1,'test',1000,1000),tj(2,'test',1010,null),tj(3,'test',null,null,{status:'skipped'}),tj(4,'test',1000,1060,{pipeline_id:8})]});
 assert.equal(m.stages.length,2);assert.equal(m.stages[0].seconds,0);assert.equal(m.stages[0].missing,1);assert.equal(m.orderKnown,false);
 assert.equal(m.stages[0].jobs[2].ran,false);assert.equal(T.model({jobs:[]}).origin,null);
});
test('stage summary matches card semantics for parallel, missing, zero and unrun Jobs',()=>{
 const m=T.model({pipeline_id:7,jobs:[
   tj(1,'build',1000,1060,{duration:60,queued_duration:0}),
   tj(2,'build',1030,1090,{status:'failed',allow_failure:true,duration:60,queued_duration:10}),
   tj(3,'build',1100,1100,{duration:0,queued_duration:null}),
   tj(4,'build',1110,null,{duration:null,queued_duration:5}),
   tj(5,'build',null,null,{status:'manual',duration:999,queued_duration:999}),
   tj(6,'test',null,null,{status:'skipped'})
 ]});
 assert.deepEqual(T.summary(m.stages[0]),{seconds:90,total:120,mean:40,queue:15,samples:3,queueSamples:3,ran:4});
 assert.deepEqual(T.summary(m.stages[1]),{seconds:null,total:null,mean:null,queue:null,samples:0,queueSamples:0,ran:0});
 assert.equal(T.defaultStage(m.stages).name,'build');
 assert.equal(T.defaultStage([]),null);
 assert.equal(T.defaultStage([{seconds:null,key:'a'},{seconds:0,key:'b'}]).key,'a');
});

test("configured timezone controls dates, filtering and daily aggregation", () => {
  assert.equal(A.day(base.finished, "UTC"), "2026-11-04");
  assert.equal(A.day(Date.parse("2024-03-10T04:30:00Z") / 1000, "America/New_York"), "2024-03-09");
  const rows = [r("utc", {timezone: "UTC"})];
  assert.equal(A.filter(rows, {status: "success", start: "2026-11-04", end: "2026-11-04", timezone: "UTC"}).length, 1);
  assert.equal(A.daily(rows)[0].date, "2026-11-04");
});
