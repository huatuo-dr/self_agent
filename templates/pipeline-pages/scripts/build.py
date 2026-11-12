#!/usr/bin/env python3
"""Dependency-free Pages builder. Input is reviewed task manifests and JSON only."""

import argparse
import json
import math
import os
import re
import shutil
from pathlib import Path
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "pipeline_public_schema", Path(__file__).with_name("public_schema.py")
)
_schema = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_schema)


def build(root, output):
    if Path(output).is_symlink():
        raise ValueError("Symlink output")
    root, output = Path(root).resolve(), Path(output).resolve()
    if output == root or root.is_relative_to(output):
        raise ValueError("Unsafe output directory")
    if output.exists() and any(output.iterdir()):
        if not (output / ".pipeline-pages-output").is_file():
            raise ValueError("Output contains unmanaged files")
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / ".pipeline-pages-output").write_text("generated\n")
    template = (root / "scripts/dashboard.html").read_text()
    for marker, name in (
        ("/*ANALYTICS*/", "analytics.js"),
        ("/*TIMELINE*/", "timeline.js"),
        ("/*DASHBOARD*/", "dashboard.js"),
    ):
        template = template.replace(marker, (root / "scripts" / name).read_text())
    tasks = []
    for path in sorted((root / "tasks").glob("*/manifest.json")):
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("Symlink in task input")
        m = _schema.clean(json.loads(path.read_text()), _schema.MANIFEST)
        if (
            m.get("schema") != 1
            or not re.fullmatch("[a-f0-9]{32}", m.get("task_id", ""))
            or m["task_id"] != path.parent.name
        ):
            raise ValueError("Invalid task identity")
        if (
            not isinstance(m.get("months"), list)
            or not isinstance(m.get("branches"), list)
            or type(m.get("revision")) is not int
            or type(m.get("updated")) not in (int, float)
            or not isinstance(m.get("project_path"), str)
            or not isinstance(m.get("manager"), str)
        ):
            raise ValueError("Invalid manifest fields")
        tasks.append(m)
        dest = output / "tasks" / m["task_id"]
        dest.mkdir(parents=True, exist_ok=True)
        for month in m["months"]:
            if not re.fullmatch(r"\d{4}-\d\d", month):
                raise ValueError("Invalid month")
            source = path.parent / (month + ".json")
            if source.is_symlink():
                raise ValueError("Symlink in month input")
            records = json.loads(source.read_text())
            if not isinstance(records, list):
                raise ValueError("Invalid records")
            records = [_schema.clean(record, _schema.RECORD) for record in records]
            seen = set()
            for record in records:
                if not re.fullmatch("[a-f0-9]{24}", record.get("execution_id", "")):
                    raise ValueError("Invalid execution ID")
                for key in (
                    "pipeline_id",
                    "sequence",
                    "config_version",
                    "metric_version",
                ):
                    if type(record.get(key)) is not int or record[key] < 1:
                        raise ValueError("Invalid numeric identity")
                for key in (
                    "finished",
                    "active_seconds",
                    "elapsed_seconds",
                    "queue_sum",
                    "queue_max",
                    "pipeline_queue",
                ):
                    v = record.get(key)
                    if v is not None and (
                        type(v) not in (int, float) or not math.isfinite(v) or v < 0
                    ):
                        raise ValueError("Invalid duration/time")
                for key in ("jobs", "reasons", "alerts", "downstream"):
                    if not isinstance(record.get(key), list):
                        raise ValueError("Invalid list")
                for job in record["jobs"] + record.get("historical_attempts", []):
                    if type(job.get("id")) is not int or not isinstance(
                        job.get("name"), str
                    ):
                        raise ValueError("Invalid job")
                for d in record["downstream"]:
                    if type(d.get("id")) is not int:
                        raise ValueError("Invalid downstream")
                if record["execution_id"] in seen:
                    raise ValueError("Duplicate execution")
                seen.add(record["execution_id"])
                if not isinstance(record.get("jobs"), list):
                    raise ValueError("Missing jobs")
            (dest / source.name).write_text(
                json.dumps(records, ensure_ascii=False, allow_nan=False)
            )
        (dest / "manifest.json").write_text(json.dumps(m, ensure_ascii=False))
        (dest / "index.html").write_text(
            re.sub(
                r"/\*TASK_ID\*/\s*null", lambda _: json.dumps(m["task_id"]), template
            )
        )
    (output / "index.html").write_text(template)
    (output / "manifest.json").write_text(
        json.dumps({"schema": 1, "tasks": tasks}, ensure_ascii=False)
    )
    (output / "deployment.json").write_text(
        json.dumps(
            {
                "sha": os.environ.get("CI_COMMIT_SHA", "local-preview"),
                "tasks": {t["task_id"]: t["revision"] for t in tasks},
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", default="public")
    args = parser.parse_args()
    build(args.root, args.output)
