"""Public Pages schema. Unknown fields are dropped at every nesting level.

The publisher owns this output allowlist. The independent pipeline-pages repository
owns its input schema; the opt-in PagesContract test verifies their data contract.
"""

import math


def fields(names, kind):
    return {name: kind for name in names.split()}


JOB = {
    **fields("id pipeline_id project_id stage_index", int),
    **fields("name stage status started_at finished_at web_url failure_reason", str),
    **fields("duration queued_duration", float),
    "allow_failure": bool,
}
RECORD = {
    **fields(
        "execution_id status ref source sha web_url completeness kind matched_branch timezone",
        str,
    ),
    **fields("pipeline_id sequence config_version metric_version", int),
    **fields(
        "finished active_seconds elapsed_seconds queue_sum queue_max pipeline_queue",
        float,
    ),
    "tag": bool,
    "jobs": [JOB],
    "historical_attempts": [JOB],
    "downstream": [{**fields("id project_id", int), **fields("status web_url", str)}],
    "reasons": [
        {
            **fields("job_id", int),
            **fields("category raw explanation evidence analysis", str),
            "allow_failure": bool,
        }
    ],
    "alerts": [
        {
            "type": str,
            "job_id": int,
            **fields("actual threshold median samples ratio delta", float),
        }
    ],
    "baseline": {"samples": int, "median": float},
    "thresholds": {
        **fields("queue_seconds execution_seconds slow_ratio slow_seconds", float),
        "min_samples": int,
    },
}
MANIFEST = {
    **fields("schema revision config_version", int),
    **fields("task_id manager project_path url state timezone", str),
    "branches": [str],
    "months": [str],
    "include_tags": bool,
    "updated": float,
}


def clean(value, schema):
    if isinstance(schema, dict):
        if not isinstance(value, dict):
            raise ValueError("Invalid public object")
        return {k: clean(v, schema[k]) for k, v in value.items() if k in schema}
    if isinstance(schema, list):
        if not isinstance(value, list):
            raise ValueError("Invalid public list")
        return [clean(v, schema[0]) for v in value]
    if value is None:
        return None
    if schema is float:
        if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
            raise ValueError("Invalid public number")
    elif type(value) is not schema:
        raise ValueError("Invalid public scalar")
    return value
