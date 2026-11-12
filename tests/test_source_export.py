import importlib.util
from pathlib import Path
import tarfile
import tempfile
import unittest


class SourceExportTests(unittest.TestCase):
    def exporter(self):
        spec = importlib.util.spec_from_file_location('source_export', Path(__file__).resolve().parents[1] / 'scripts/export_source.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.export_source

    def test_export_excludes_local_state_identity_history_and_report_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('README.md', '.env.example', 'assistant/service.py', 'assistant/skills.py',
                         'tests/test_skills.py', '.local/skills/current.json',
                         '.project/skills/skills/example/SKILL.md',
                         'config/owner-profile.example.json', 'config/owner-profile.json',
                         '.env', '.local/messages.sqlite3', '.project/credential/example.json',
                         '.git/config', 'agent-docs/01-private.html',
                         'docs/superpowers/plans/internal.md', 'templates/pipeline-pages/tasks/private.json',
                         'templates/self-report/reports/00-private.html', 'assistant/__pycache__/service.pyc'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('fixture')
            output = root / '.local/source.tar.gz'
            self.exporter()(root, output)
            with tarfile.open(output) as archive:
                names = {name.removeprefix('self-agent/') for name in archive.getnames()}
            self.assertEqual(names, {'README.md', '.env.example', 'assistant/service.py', 'assistant/skills.py',
                                     'tests/test_skills.py',
                                     'config/owner-profile.example.json'})
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_export_refuses_selected_symlink_and_existing_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'assistant').mkdir()
            (root / 'private').write_text('private')
            (root / 'assistant/service.py').symlink_to(root / 'private')
            output = root / 'source.tar.gz'
            with self.assertRaises(ValueError):
                self.exporter()(root, output)
            (root / 'assistant/service.py').unlink()
            output.write_text('keep')
            with self.assertRaises(FileExistsError):
                self.exporter()(root, output)
            self.assertEqual(output.read_text(), 'keep')
