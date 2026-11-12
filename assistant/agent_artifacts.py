"""Owner/session-scoped references to generated reports, without exposing state."""
from pathlib import Path
import stat
import time

from .agent_policy import no_symlinks


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS agent_artifacts (
        binding TEXT NOT NULL, session_id INTEGER NOT NULL, path TEXT NOT NULL,
        created REAL NOT NULL, PRIMARY KEY(binding,session_id,path))''')


def _file(path, root):
    path = no_symlinks(path)
    if not path.is_relative_to(Path(root).absolute() / '.project/reports'):
        raise ValueError('Report path outside report directory')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= 50 * 1024 * 1024:
        raise ValueError('Report file is unavailable')
    return {'path': path, 'name': path.name, 'size_bytes': info.st_size}


def remember(db, binding, session_id, paths, root):
    if not binding or not session_id:
        return
    checked = [_file(path, root) for path in paths]
    initialize(db)
    db.executemany('INSERT OR IGNORE INTO agent_artifacts VALUES(?,?,?,?)',
                   [(binding,session_id,str(p['path'].relative_to(root)),time.time()) for p in checked])
    db.commit()


def files(db, binding, session_id, root):
    if not binding or not session_id:
        return []
    initialize(db)
    rows = db.execute('SELECT path FROM agent_artifacts WHERE binding=? AND session_id=? '
                      'ORDER BY created DESC LIMIT 20',(binding,session_id)).fetchall()
    result = []
    for (relative,) in rows:
        if Path(relative).is_absolute() or '..' in Path(relative).parts:
            continue
        try:
            result.append(_file(Path(root) / relative, root))
        except (OSError, ValueError):
            pass
    return result
