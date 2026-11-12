"""Private archive for files sent by the bound owner in Feishu messages."""

import hashlib
from pathlib import Path
import re
import stat
import time


MAX_BYTES = 50 * 1024 * 1024
ARCHIVE_NAME = re.compile(r'[1-9][0-9]*-[0-9a-f]{16}-[0-9a-f]{16}-[\w.\- ]{1,120}')


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS archived_files (
            binding TEXT NOT NULL, message_id TEXT NOT NULL, file_key TEXT NOT NULL,
            session_id INTEGER NOT NULL, file_name TEXT NOT NULL,
            display_name TEXT NOT NULL, archived_at REAL NOT NULL,
            size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
            PRIMARY KEY(binding,message_id,file_key));
        CREATE INDEX IF NOT EXISTS archived_files_session
            ON archived_files(binding,session_id);
        CREATE TABLE IF NOT EXISTS file_deletions (file_name TEXT PRIMARY KEY);
    ''')
    db.commit()


def directory(project_root):
    projects = Path(project_root) / '.project'
    if projects.is_symlink():
        raise ValueError('File archive parent is a symlink')
    projects.mkdir(mode=0o700, exist_ok=True)
    files = projects / 'files'
    if files.is_symlink():
        raise ValueError('File archive is a symlink')
    files.mkdir(mode=0o700, exist_ok=True)
    files.chmod(0o700)
    return files


def safe_name(display_name):
    name = str(display_name).replace('\\', '/').rsplit('/', 1)[-1]
    name = re.sub(r'[^\w.\- ]+', '_', name, flags=re.UNICODE).strip(' ._')[:120].rstrip(' ._')
    return name or 'attachment'


def archive_name(session_id, message_id, file_key, display_name):
    if type(session_id) is not int or session_id <= 0:
        raise ValueError('Invalid session ID')
    if not re.fullmatch(r'om_[A-Za-z0-9_]+', message_id):
        raise ValueError('Invalid message ID')
    if not re.fullmatch(r'file_[A-Za-z0-9_-]+', file_key):
        raise ValueError('Invalid file key')
    mid = hashlib.sha256(message_id.encode()).hexdigest()[:16]
    key = hashlib.sha256(file_key.encode()).hexdigest()[:16]
    return f'{session_id}-{mid}-{key}-{safe_name(display_name)}'


def inspect(path):
    if path.is_symlink():
        raise ValueError('File is a symlink')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BYTES:
        raise ValueError('Invalid file size')
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return info.st_size, digest.hexdigest()


def cached(db, binding, message_id, file_key, files):
    row = db.execute('''SELECT file_name,display_name,size_bytes,sha256 FROM archived_files
                        WHERE binding=? AND message_id=? AND file_key=?''',
                     (binding, message_id, file_key)).fetchone()
    if not row or not ARCHIVE_NAME.fullmatch(row[0]):
        return None
    path = files / row[0]
    try:
        size, digest = inspect(path)
    except (OSError, ValueError):
        return None
    return {'path': path, 'name': row[1], 'size_bytes': size} if (size, digest) == row[2:] else None


def remember(db, binding, message_id, file_key, session_id, path, display_name):
    if not ARCHIVE_NAME.fullmatch(path.name):
        raise ValueError('Invalid archive name')
    size, digest = inspect(path)
    db.execute('''INSERT INTO archived_files
                  (binding,message_id,file_key,session_id,file_name,display_name,archived_at,size_bytes,sha256)
                  VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(binding,message_id,file_key) DO UPDATE SET
                  file_name=excluded.file_name,display_name=excluded.display_name,
                  size_bytes=excluded.size_bytes,sha256=excluded.sha256''',
               (binding, message_id, file_key, session_id, path.name, display_name, time.time(), size, digest))
    db.commit()
    return {'path': path, 'name': display_name, 'size_bytes': size}


def session_files(db, binding, session_id, project_root, limit=20):
    files = Path(project_root) / '.project/files'
    if files.is_symlink() or files.parent.is_symlink():
        raise ValueError('Unsafe archive directory')
    rows = db.execute('''SELECT file_name,display_name,size_bytes FROM archived_files
                         WHERE binding=? AND session_id=? ORDER BY archived_at DESC LIMIT ?''',
                      (binding, session_id, limit)).fetchall()
    result = []
    for name, display_name, size in rows:
        path = files / name
        if ARCHIVE_NAME.fullmatch(name) and path.is_file() and not path.is_symlink():
            result.append({'path': path, 'name': display_name, 'size_bytes': size})
    return result


def queue_session_delete(db, binding, session_id):
    db.execute('INSERT OR IGNORE INTO file_deletions(file_name) '
               'SELECT file_name FROM archived_files WHERE binding=? AND session_id=?',
               (binding, session_id))
    db.execute('DELETE FROM archived_files WHERE binding=? AND session_id=?', (binding, session_id))


def purge_deletions(db, project_root):
    projects = Path(project_root) / '.project'
    files = projects / 'files'
    if projects.is_symlink() or files.is_symlink():
        return 0
    removed = 0
    for (name,) in db.execute('SELECT file_name FROM file_deletions').fetchall():
        if not ARCHIVE_NAME.fullmatch(name):
            continue
        if db.execute('SELECT 1 FROM archived_files WHERE file_name=? LIMIT 1', (name,)).fetchone():
            db.execute('DELETE FROM file_deletions WHERE file_name=?', (name,))
            continue
        try:
            (files / name).unlink(missing_ok=True)
        except OSError:
            continue
        db.execute('DELETE FROM file_deletions WHERE file_name=?', (name,))
        removed += 1
    db.commit()
    return removed
