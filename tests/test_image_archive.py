import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant.image_archive import (directory, file_name, migrate_filenames, purge_deletions, queue_session_delete,
                                     remember, search)
from assistant.management import Management
from assistant.service import database
from assistant.sessions import Sessions


class ImageArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = database(':memory:')
        self.sessions = Sessions(self.db, 'oc_owner')
        self.sid = self.sessions.create('om_photo', '图片话题')
        self.sessions.finish(self.sid, 'om_photo', '[用户发送图片]', '已经看过图片',
                             {'status': '已完成'})
        self.db.commit()
        self.pictures = directory(self.root)
        self.path = self.pictures / file_name(self.sid, 1792118400123456000, '.png')
        self.path.write_bytes(b'\x89PNG\r\n\x1a\n' + b'0' * 16)
        self.path.chmod(0o600)
        remember(self.db, 'oc_owner', 'om_photo', self.sid, self.path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_search_is_owner_scoped_and_returns_existing_path(self):
        found = search(self.db, 'oc_owner', self.root, session_id=self.sid)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['message_id'], 'om_photo')
        self.assertEqual(found[0]['path'], str(self.path))
        self.assertEqual(search(self.db, 'oc_other', self.root), [])
        self.assertEqual(search(self.db, 'oc_owner', self.root, message_id='om_other'), [])

    def test_name_has_session_and_beijing_timestamp(self):
        self.assertEqual(self.path.name, f'{self.sid}-20261016T104000123456.png')

    def test_legacy_file_is_renamed_without_losing_index(self):
        old_name = hashlib.sha256(b'oc_owner:om_photo').hexdigest()[:32] + '.png'
        old = self.pictures / old_name
        self.path.rename(old)
        self.db.execute('UPDATE archived_images SET file_name=?,archived_at=? WHERE message_id=?',
                        (old_name, 1792118400.123456, 'om_photo'))
        self.db.commit()
        self.assertEqual(migrate_filenames(self.db, self.root), 1)
        new = Path(search(self.db, 'oc_owner', self.root)[0]['path'])
        self.assertEqual(new.name, f'{self.sid}-20261016T104000123456.png')
        self.assertFalse(old.exists())
        self.assertEqual(migrate_filenames(self.db, self.root), 0)

    def test_clean_commits_then_removes_archived_picture(self):
        management = Management(self.db, 'oc_owner', self.root)
        self.db.execute("INSERT INTO inbox(id,binding,content,kind,state) "
                        "VALUES('om_clean','oc_owner','/clean 4','text','done')")
        self.db.commit()
        preview = management.clean('4', 'om_clean')
        self.assertIn('归档图片', preview)
        token = self.db.execute("SELECT token FROM clean_plans WHERE source='om_clean'").fetchone()[0]
        self.assertIn('已删除 1 个', management.confirm(token))
        self.assertFalse(self.path.exists())
        self.assertEqual(search(self.db, 'oc_owner', self.root), [])
        self.assertEqual(self.db.execute('SELECT count(*) FROM image_deletions').fetchone()[0], 0)

    def test_pending_deletion_can_retry_after_commit(self):
        queue_session_delete(self.db, 'oc_owner', self.sid)
        self.db.commit()
        self.assertEqual(purge_deletions(self.db, self.root), 1)
        self.assertFalse(self.path.exists())
        self.assertEqual(purge_deletions(self.db, self.root), 0)

    def test_failed_clean_transaction_keeps_picture(self):
        management = Management(self.db, 'oc_owner', self.root)
        self.db.execute("INSERT INTO inbox(id,binding,content,kind,state) "
                        "VALUES('om_clean','oc_owner','/clean 4','text','done')")
        self.db.commit()
        management.clean('4', 'om_clean')
        token = self.db.execute("SELECT token FROM clean_plans WHERE source='om_clean'").fetchone()[0]
        original = management.delete
        def fail(sid):
            original(sid)
            raise RuntimeError('simulated database failure')
        with patch.object(management, 'delete', fail), self.assertRaises(RuntimeError):
            management.confirm(token)
        self.assertTrue(self.path.exists())
        self.assertEqual(len(search(self.db, 'oc_owner', self.root)), 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM image_deletions').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
