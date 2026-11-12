import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_pages.py"


class BuildPagesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "reports").mkdir()
        (self.root / "index.html").write_text(
            "<html><body><!-- REPORTS_START --><p>暂无报告</p>"
            "<!-- REPORTS_END --></body></html>",
            encoding="utf-8",
        )

    def build(self):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--source", str(self.root)],
            capture_output=True,
            text=True,
        )

    def test_sorted_links_are_escaped_and_reports_are_published(self):
        reports = self.root / "reports"
        (reports / "01-第二篇.html").write_text("second", encoding="utf-8")
        (reports / "00-<第一篇>.html").write_text("first", encoding="utf-8")
        (reports / "image.png").write_bytes(b"image")

        result = self.build()

        self.assertEqual(result.returncode, 0, result.stderr)
        index = (self.root / "public" / "index.html").read_text(encoding="utf-8")
        self.assertLess(index.index("00-&lt;第一篇&gt;"), index.index("01-第二篇"))
        self.assertIn("reports/00-%3C%E7%AC%AC%E4%B8%80%E7%AF%87%3E.html", index)
        self.assertNotIn("<第一篇>", index)
        self.assertNotIn("暂无报告", index)
        self.assertEqual((self.root / "public" / "reports" / "01-第二篇.html").read_text(encoding="utf-8"), "second")
        self.assertEqual((self.root / "public" / "reports" / "image.png").read_bytes(), b"image")
        self.assertEqual((self.root / "index.html").read_text(encoding="utf-8"), index)

    def test_empty_reports_keep_helpful_message(self):
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("暂无报告", (self.root / "public" / "index.html").read_text(encoding="utf-8"))

    def test_invalid_filename_fails_build(self):
        (self.root / "reports" / "report.html").write_text("bad", encoding="utf-8")
        result = self.build()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("report.html", result.stderr)

    def test_duplicate_number_fails_build(self):
        (self.root / "reports" / "00-甲.html").write_text("a", encoding="utf-8")
        (self.root / "reports" / "00-乙.html").write_text("b", encoding="utf-8")
        result = self.build()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("00", result.stderr)

    def test_missing_number_fails_build(self):
        (self.root / "reports" / "00-甲.html").write_text("a", encoding="utf-8")
        (self.root / "reports" / "02-乙.html").write_text("b", encoding="utf-8")
        result = self.build()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("01", result.stderr)


if __name__ == "__main__":
    unittest.main()
