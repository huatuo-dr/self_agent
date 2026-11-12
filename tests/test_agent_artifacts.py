from pathlib import Path
import tempfile
import unittest
from assistant.service import database
from assistant.agent_artifacts import remember, files


class ArtifactTests(unittest.TestCase):
    def test_reports_are_bound_to_owner_and_session_and_missing_files_are_ignored(self):
        db=database(':memory:')
        try:
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory);path=root/'.project/reports/task/report.html'
                path.parent.mkdir(parents=True);path.write_text('<html>report</html>')
                remember(db,'oc_owner',4,[path],root)
                self.assertEqual(files(db,'oc_owner',4,root)[0]['path'],path)
                self.assertEqual(files(db,'oc_other',4,root),[])
                self.assertEqual(files(db,'oc_owner',5,root),[])
                path.unlink()
                self.assertEqual(files(db,'oc_owner',4,root),[])
        finally:db.close()

    def test_external_or_linked_report_is_not_registered(self):
        db=database(':memory:')
        try:
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory);outside=root/'secret';outside.write_text('fixture')
                with self.assertRaises(ValueError):remember(db,'oc_owner',4,[outside],root)
        finally:db.close()
