"""Export only public source files; never include local state or Git history."""

import argparse
import io
import os
from pathlib import Path
import stat
import tarfile


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ('feishu-private-assistant', 'gitlab-assistant', 'gitlab-target-watch',
          'group-message-watch', 'history-search', 'issue-assistant',
          'report-creation', 'schedule-assistant')


def source_files(root):
    selected = {root / name for name in (
        'README.md', 'LICENSE', 'AGENTS.md', '.env.example', '.gitignore', 'auto_run.sh',
        'config/owner-profile.example.json', 'config/workday-calendar.example.json',
        'docs/configuration.md', 'docs/pipeline-monitor.md')}
    for folder in ('assistant', 'scripts', 'tests'):
        selected.update((root / folder).rglob('*.py'))
    for skill in SKILLS:
        base = root / '.agents/skills' / skill
        selected.update(p for p in base.rglob('*') if p.suffix in ('.md', '.json', '.py', '.sh'))
    for template in ('pipeline-pages', 'self-report'):
        base = root / 'templates' / template
        selected.update(base / name for name in ('README.md', '.gitignore', '.gitlab-ci.yml',
                                                 'index.html', 'tasks/.gitkeep', 'reports/.gitkeep'))
        for folder in ('scripts', 'tests'):
            selected.update(p for p in (base / folder).rglob('*')
                            if p.suffix in ('.py', '.js', '.cjs', '.html'))
    for path in sorted(selected):
        if any(part in ('__pycache__', '.git', '.local', '.project', '.venv')
               for part in path.relative_to(root).parts):
            continue
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            raise ValueError('Refusing to export a source symlink')
        if path.exists():
            if not stat.S_ISREG(path.stat().st_mode):
                raise ValueError('Source must be a regular file')
            yield path


def export_source(root, output):
    root, output = Path(root).absolute(), Path(output).absolute()
    files = list(source_files(root))
    if output in files:
        raise ValueError('Destination must not replace source')
    output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as target, tarfile.open(fileobj=target, mode='w:gz') as archive:
            for path in files:
                content = path.read_bytes()
                entry = tarfile.TarInfo('self-agent/' + path.relative_to(root).as_posix())
                entry.size = len(content)
                entry.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                archive.addfile(entry, io.BytesIO(content))
    except BaseException:
        output.unlink()
        raise
    return len(files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        count = export_source(ROOT, args.output)
    except (OSError, ValueError) as error:
        parser.exit(1, f'Export failed: {error}\n')
    print(f'Exported {count} source files: {args.output}')


if __name__ == '__main__':
    main()
