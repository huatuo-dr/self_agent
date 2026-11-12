"""Generate the report index and the GitLab Pages public directory."""

import argparse
import html
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import quote


REPORT_NAME = re.compile(r"([0-9]{2})-.+\.html")
REPORT_SECTION = re.compile(
    r"(?<=<!-- REPORTS_START -->).*?(?=<!-- REPORTS_END -->)", re.DOTALL
)


def build(source: Path) -> None:
    source = source.resolve()
    output = source / "public"
    reports = source / "reports"
    index_path = source / "index.html"

    report_files = sorted(
        path for path in reports.iterdir() if path.is_file() and path.suffix == ".html"
    )
    numbers = set()
    for path in report_files:
        match = REPORT_NAME.fullmatch(path.name)
        if match is None:
            raise ValueError(f"报告文件名格式错误：{path.name}（应为 00-名称.html）")
        number = match.group(1)
        if number in numbers:
            raise ValueError(f"报告序号重复：{number}（文件：{path.name}）")
        numbers.add(number)
    for expected, actual in enumerate(sorted(numbers)):
        if actual != f"{expected:02d}":
            raise ValueError(f"报告序号应从 00 连续递增，缺少 {expected:02d}")

    if report_files:
        items = [
            f'        <li><a href="reports/{quote(path.name)}">'
            f"{html.escape(path.name)}</a></li>"
            for path in report_files
        ]
        content = '\n      <ul class="report-list">\n' + "\n".join(items) + "\n      </ul>\n      "
    else:
        content = '\n      <p class="empty">暂无报告。请将 00-使用说明.html 等文件放入 reports/。</p>\n      '

    original = index_path.read_text(encoding="utf-8")
    updated, count = REPORT_SECTION.subn(lambda _: content, original)
    if count != 1:
        raise ValueError("index.html 中必须包含一组 REPORTS_START / REPORTS_END 标记")

    index_path.write_text(updated, encoding="utf-8")
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    (output / "index.html").write_text(updated, encoding="utf-8")
    shutil.copytree(reports, output / "reports")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        build(args.source)
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
