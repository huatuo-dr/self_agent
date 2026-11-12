import sqlite3
from pathlib import Path
import tempfile
import unittest

from assistant import file_archive


class FileArchiveTests(unittest.TestCase):
    def test_archive_rejects_traversal_and_cleanup_removes_session_file(self):
        with tempfile.TemporaryDirectory() as directory:
            db = sqlite3.connect(':memory:')
            file_archive.initialize(db)
            files = file_archive.directory(directory)
            name = file_archive.archive_name(43, 'om_example', 'file_v3_example', '../../report.html')
            self.assertTrue(name.endswith('-report.html'))
            path = files / name
            path.write_text('report')
            item = file_archive.remember(db, 'oc_owner', 'om_example', 'file_v3_example',
                                         43, path, 'report.html')
            self.assertEqual(file_archive.cached(db, 'oc_owner', 'om_example', 'file_v3_example', files), item)
            self.assertIsNone(file_archive.cached(db, 'oc_other', 'om_example', 'file_v3_example', files))
            self.assertEqual(file_archive.session_files(db, 'oc_owner', 43, directory), [item])
            file_archive.queue_session_delete(db, 'oc_owner', 43)
            db.commit()
            self.assertEqual(file_archive.purge_deletions(db, directory), 1)
            self.assertFalse(path.exists())
            db.close()

    def test_file_size_and_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            files = file_archive.directory(directory)
            empty = files / 'empty'
            empty.touch()
            with self.assertRaises(ValueError):
                file_archive.inspect(empty)
            source = files / 'source'
            source.write_text('report')
            link = files / 'link'
            link.symlink_to(source)
            with self.assertRaises(ValueError):
                file_archive.inspect(link)
