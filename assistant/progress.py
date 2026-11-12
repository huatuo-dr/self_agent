"""Durable, content-free progress counters for long running chat requests."""
import time


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS task_runs (
            source TEXT PRIMARY KEY, binding TEXT NOT NULL, session_id INTEGER NOT NULL,
            state TEXT NOT NULL, started REAL NOT NULL, updated REAL NOT NULL,
            stage TEXT NOT NULL DEFAULT '启动任务', completed_events INTEGER NOT NULL DEFAULT 0,
            cancel_requested INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS task_updates (
            source TEXT NOT NULL, seq INTEGER NOT NULL, body TEXT NOT NULL,
            sent INTEGER NOT NULL DEFAULT 0, remote_id TEXT,
            PRIMARY KEY(source,seq));
    ''')


def start(db, source, binding, session_id):
    now = time.time()
    db.execute('INSERT OR IGNORE INTO task_runs(source,binding,session_id,state,started,updated) '
               'VALUES(?,?,?,?,?,?)', (source, binding, session_id, 'running', now, now))
    db.execute('UPDATE task_runs SET state=?,started=?,updated=?,stage=?,completed_events=0,'
               'cancel_requested=0 WHERE source=? AND binding=?',
               ('running', now, now, '启动任务', source, binding))
    db.commit()


def observe(db, source, event):
    """Store only event category and count; event text may contain credentials."""
    if not isinstance(event, dict) or event.get('type') != 'item.completed':
        return
    item = event.get('item') or {}
    kind = item.get('type') if isinstance(item, dict) else None
    stages = {
        'command_execution': '执行检查或操作',
        'file_change': '修改文件',
        'web_search': '检索资料',
        'mcp_tool_call': '调用外部工具',
        'agent_message': '整理阶段结果',
    }
    stage = stages.get(kind)
    if stage:
        db.execute('UPDATE task_runs SET stage=?,completed_events=completed_events+1,updated=? '
                   'WHERE source=? AND state=?', (stage, time.time(), source, 'running'))
        db.commit()


def snapshot(db, source):
    row = db.execute('SELECT session_id,state,started,updated,stage,completed_events,cancel_requested '
                     'FROM task_runs WHERE source=?', (source,)).fetchone()
    return dict(zip(('session_id', 'state', 'started', 'updated', 'stage', 'completed_events',
                     'cancel_requested'), row)) if row else None


def finish(db, source, state):
    db.execute('UPDATE task_runs SET state=?,updated=? WHERE source=?', (state, time.time(), source))
    db.commit()


def progress_text(sid, run, now=None):
    elapsed = max(0, int(((now if now is not None else time.time()) - run['started']) // 60))
    count = run['completed_events']
    activity = (f'已观察到 {count} 个完成的处理动作。' if count
                else '目前没有可确认的新阶段结果。')
    return (f'🔄 任务 #{sid} 仍在处理，已运行约 {elapsed} 分钟。\n'
            f'当前阶段：{run["stage"]}。{activity}\n'
            '这条是运行进度，最终结果会在本话题另行回复。')
