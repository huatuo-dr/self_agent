"""Standalone report-builder tests; no assistant checkout or credentials needed."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pipeline_build", ROOT / "scripts/build.py"
)
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)


class SiteTests(unittest.TestCase):
    def test_build_empty_site(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "scripts/dashboard.html").write_bytes(
                (ROOT / "scripts/dashboard.html").read_bytes()
            )
            for asset in ("analytics.js", "timeline.js", "dashboard.js"):
                (root / "scripts" / asset).write_bytes(
                    (ROOT / "scripts" / asset).read_bytes()
                )
            BUILDER.build(root, root / "public")
            self.assertTrue((root / "public/index.html").is_file())
            self.assertEqual(
                json.loads((root / "public/manifest.json").read_text())["tasks"], []
            )

    def test_builder_drops_unknown_fields_and_rejects_invalid_types(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "scripts/dashboard.html").write_bytes(
                (ROOT / "scripts/dashboard.html").read_bytes()
            )
            for asset in ("analytics.js", "timeline.js", "dashboard.js"):
                (root / "scripts" / asset).write_bytes(
                    (ROOT / "scripts" / asset).read_bytes()
                )
            task = root / "tasks" / ("a" * 32)
            task.mkdir(parents=True)
            manifest = dict(
                schema=1,
                task_id="a" * 32,
                manager="fixture",
                project_path="g/r",
                branches=["main"],
                months=["2026-11"],
                revision=1,
                updated=1,
                recipient="DEMO_NOT_REAL",
            )
            (task / "manifest.json").write_text(json.dumps(manifest))
            # Public input fixture belongs to this repository, not a producer's code.
            record = dict(
                execution_id="b" * 24,
                pipeline_id=7,
                sequence=1,
                config_version=1,
                metric_version=1,
                status="success",
                ref="main",
                source="push",
                tag=False,
                matched_branch="main",
                finished=1100,
                active_seconds=100,
                elapsed_seconds=100,
                queue_sum=1,
                queue_max=1,
                pipeline_queue=None,
                kind="initial",
                completeness="complete",
                jobs=[
                    dict(
                        id=10,
                        name="test",
                        status="success",
                        duration=100,
                        queued_duration=1,
                        secret="DEMO_NOT_REAL",
                    )
                ],
                historical_attempts=[],
                downstream=[],
                trace="DEMO_NOT_REAL",
                baseline=dict(samples=0, median=None, token="DEMO_NOT_REAL"),
                reasons=[
                    dict(
                        job_id=10,
                        raw="script_failure",
                        category="未分类",
                        explanation="待分析",
                        analysis="pending",
                        trace="DEMO_NOT_REAL",
                    )
                ],
                alerts=[
                    dict(
                        type="排队超时",
                        actual=601,
                        threshold=600,
                        password="DEMO_NOT_REAL",
                    )
                ],
            )
            (task / "2026-11.json").write_text(json.dumps([record]))
            BUILDER.build(root, root / "public")
            for path in (root / "public").rglob("*.json"):
                self.assertNotIn("DEMO_NOT_REAL", path.read_text())
            output = root / "public/tasks" / task.name / "2026-11.json"
            self.assertEqual(
                json.loads(output.read_text())[0]["jobs"][0]["duration"], 100
            )
            record["jobs"][0]["name"] = {"token": "DEMO_NOT_REAL"}
            (task / "2026-11.json").write_text(json.dumps([record]))
            with self.assertRaises(ValueError):
                BUILDER.build(root, root / "public")


if __name__ == "__main__":
    unittest.main()
