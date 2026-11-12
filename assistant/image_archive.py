"""Private, durable index for images received in the owner's chat."""

from datetime import datetime
import os
from pathlib import Path
import re
import stat
import time
from assistant.settings import get_timezone


MAX_BYTES = 20 * 1024 * 1024
FILE_NAME = re.compile(r'[1-9][0-9]*-[0-9]{8}T[0-9]{12}\.(?:png|jpg|webp|gif)')
LEGACY_FILE_NAME = re.compile(r'[0-9a-f]{32}\.(?:png|jpg|webp|gif)')

FORMATS = (
    (lambda h: h.startswith(b'\x89PNG\r\n\x1a\n'), '.png'),
    (lambda h: h.startswith(b'\xff\xd8\xff'), '.jpg'),
    (lambda h: h.startswith((b'GIF87a', b'GIF89a')), '.gif'),
    (lambda h: h.startswith(b'RIFF') and h[8:12] == b'WEBP', '.webp'),
)


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS archived_images (
            binding TEXT NOT NULL, message_id TEXT NOT NULL, session_id INTEGER,
            file_name TEXT NOT NULL, archived_at REAL NOT NULL, size_bytes INTEGER NOT NULL,
            PRIMARY KEY(binding,message_id));
        CREATE INDEX IF NOT EXISTS archived_images_recent
            ON archived_images(binding,archived_at DESC);
        CREATE INDEX IF NOT EXISTS archived_images_session
            ON archived_images(binding,session_id);
        CREATE TABLE IF NOT EXISTS image_deletions (file_name TEXT PRIMARY KEY);
    ''')
    db.commit()


def directory(project_root):
    projects = Path(project_root) / '.project'
    if projects.is_symlink():
        raise ValueError('Image archive parent is a symlink')
    projects.mkdir(mode=0o700, exist_ok=True)
    pictures = projects / 'pic'
    if pictures.is_symlink():
        raise ValueError('Image archive is a symlink')
    pictures.mkdir(mode=0o700, exist_ok=True)
    pictures.chmod(0o700)
    return pictures


def format_of(path):
    if path.is_symlink():
        raise ValueError('Image is a symlink')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BYTES:
        raise ValueError('Invalid image size')
    with path.open('rb') as file:
        header = file.read(16)
    for matches, extension in FORMATS:
        if matches(header):
            return extension, info.st_size
    raise ValueError('Unsupported image format')


def file_name(session_id, timestamp_ns, extension, cfg=None):
    if type(session_id) is not int or session_id <= 0 or type(timestamp_ns) is not int or timestamp_ns <= 0:
        raise ValueError('Invalid image archive name input')
    if extension not in ('.png', '.jpg', '.webp', '.gif'):
        raise ValueError('Invalid image extension')
    instant = datetime.fromtimestamp(timestamp_ns // 1_000_000_000, get_timezone(cfg))
    return f'{session_id}-{instant:%Y%m%dT%H%M%S}{timestamp_ns // 1000 % 1_000_000:06d}{extension}'


def valid_name(name):
    return bool(FILE_NAME.fullmatch(name) or LEGACY_FILE_NAME.fullmatch(name))


def cached(db, binding, message_id, pictures):
    row = db.execute('SELECT file_name FROM archived_images WHERE binding=? AND message_id=?',
                     (binding, message_id)).fetchone()
    if not row or not valid_name(row[0]):
        return None
    path = pictures / row[0]
    try:
        extension, _ = format_of(path)
        if extension == path.suffix:
            return path
    except (OSError, ValueError):
        pass
    return None


def remember(db, binding, message_id, session_id, path, archived_at=None):
    if not valid_name(path.name):
        raise ValueError('Invalid archive name')
    _, size = format_of(path)
    db.execute('''INSERT INTO archived_images(binding,message_id,session_id,file_name,archived_at,size_bytes)
                  VALUES(?,?,?,?,?,?) ON CONFLICT(binding,message_id) DO UPDATE SET
                  file_name=excluded.file_name,size_bytes=excluded.size_bytes,
                  session_id=coalesce(archived_images.session_id,excluded.session_id)''',
               (binding, message_id, session_id, path.name,
                time.time() if archived_at is None else archived_at, size))
    db.commit()


def migrate_filenames(db, project_root, cfg=None):
    """Rename older hash-named images, recovering a rename interrupted before DB update."""
    pictures = directory(project_root)
    rows = db.execute('SELECT binding,message_id,session_id,file_name,archived_at FROM archived_images').fetchall()
    migrated = 0
    for binding, message_id, session_id, old_name, archived_at in rows:
        if not LEGACY_FILE_NAME.fullmatch(old_name) or session_id is None:
            continue
        old = pictures / old_name
        if old.is_symlink():
            continue
        timestamp_ns = int(archived_at * 1_000_000_000)
        target = pictures / file_name(session_id, timestamp_ns, old.suffix, cfg)
        if old.is_file():
            if target.exists():
                # Another archived image may share the same microsecond.
                while target.exists():
                    timestamp_ns += 1000
                    target = pictures / file_name(session_id, timestamp_ns, old.suffix, cfg)
            old.rename(target)
        else:
            # A previous run may have renamed the file before updating SQLite.
            while target.is_file() and not target.is_symlink() and db.execute(
                    'SELECT 1 FROM archived_images WHERE file_name=?', (target.name,)).fetchone():
                timestamp_ns += 1000
                target = pictures / file_name(session_id, timestamp_ns, old.suffix, cfg)
            if not target.is_file() or target.is_symlink():
                continue
        db.execute('UPDATE archived_images SET file_name=? WHERE binding=? AND message_id=?',
                   (target.name, binding, message_id))
        db.execute('UPDATE image_deletions SET file_name=? WHERE file_name=?',
                   (target.name, old_name))
        db.commit()
        migrated += 1
    return migrated


def queue_session_delete(db, binding, session_id):
    db.execute('INSERT OR IGNORE INTO image_deletions(file_name) '
               'SELECT file_name FROM archived_images WHERE binding=? AND session_id=?',
               (binding, session_id))
    db.execute('DELETE FROM archived_images WHERE binding=? AND session_id=?',
               (binding, session_id))


def purge_deletions(db, project_root):
    projects = Path(project_root) / '.project'
    pictures = projects / 'pic'
    if projects.is_symlink() or pictures.is_symlink():
        return 0
    removed = 0
    for (name,) in db.execute('SELECT file_name FROM image_deletions').fetchall():
        if not valid_name(name):
            continue
        if db.execute('SELECT 1 FROM archived_images WHERE file_name=? LIMIT 1', (name,)).fetchone():
            db.execute('DELETE FROM image_deletions WHERE file_name=?', (name,))
            continue
        try:
            (pictures / name).unlink(missing_ok=True)
        except OSError:
            continue
        db.execute('DELETE FROM image_deletions WHERE file_name=?', (name,))
        removed += 1
    db.commit()
    return removed


def search(db, binding, project_root, session_id=None, message_id=None, limit=20, cfg=None):
    if not binding or type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('Invalid image search input')
    if session_id is not None and (type(session_id) is not int or session_id <= 0):
        raise ValueError('Invalid session ID')
    if message_id is not None and not re.fullmatch(r'om_[A-Za-z0-9_]+', message_id):
        raise ValueError('Invalid message ID')
    conditions, values = ['a.binding=?'], [binding]
    if session_id is not None:
        conditions.append('a.session_id=?')
        values.append(session_id)
    if message_id is not None:
        conditions.append('a.message_id=?')
        values.append(message_id)
    rows = db.execute('''SELECT a.message_id,a.session_id,a.file_name,a.archived_at,a.size_bytes,
                                s.title,t.link FROM archived_images a
                         LEFT JOIN sessions s ON s.id=a.session_id AND s.binding=a.binding
                         LEFT JOIN session_threads t ON t.session_id=a.session_id AND t.binding=a.binding
                         WHERE ''' + ' AND '.join(conditions) +
                      ' ORDER BY a.archived_at DESC LIMIT ?', (*values, limit)).fetchall()
    pictures = Path(project_root) / '.project/pic'
    if pictures.is_symlink() or pictures.parent.is_symlink():
        raise ValueError('Unsafe image archive directory')
    items = []
    for mid, sid, name, archived_at, size, title, link in rows:
        if not valid_name(name) or (sid is not None and title is None):
            continue
        path = pictures / name
        if not path.is_file() or path.is_symlink():
            continue
        items.append({'message_id': mid, 'session_id': sid, 'session_title': title,
                      'archived_at': datetime.fromtimestamp(archived_at, get_timezone(cfg)).isoformat(),
                      'size_bytes': size, 'path': str(path), 'thread_link': link})
    return items
