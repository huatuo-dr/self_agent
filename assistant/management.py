"""Local management commands; cleanup is a bounded, reviewed snapshot transaction."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import time
from assistant.settings import get_timezone

from assistant.sessions import Sessions, brief, command

POLICIES = {1: '删除对话轮次小于 5 轮的对话', 2: '删除超过 30 天未使用的对话',
            3: '删除超过 7 天未使用的对话', 4: '删除所有未保存的对话'}


def record_error(db, event):
    db.execute('INSERT OR REPLACE INTO bot_health VALUES(?,?)',
               ('last_error', json.dumps({'time': time.time(), 'event': event}, ensure_ascii=False)))
    db.commit()


def format_time(value, cfg=None):
    return datetime.fromtimestamp(value, get_timezone(cfg)).strftime('%m-%d %H:%M') if value else '未知'


class Management:
    def __init__(self, db, binding, project_root=None):
        self.db, self.binding = db, binding
        self.sessions = Sessions(db, binding)
        self.project_root = Path(project_root) if project_root else Path(__file__).resolve().parents[1]

    def save(self, argument, saved, context_sid=None):
        if not argument and saved:
            sid = context_sid
        elif re.fullmatch(r'#?\d{1,19}', argument):
            sid = int(argument.lstrip('#'))
        else:
            return '用法：/save [编号]' if saved else '用法：/unsave 编号'
        if sid is None and not argument:
            return '请在任务话题内使用 /save，或在主聊天发送 /save 编号。'
        data = self.sessions.get(sid)
        if not data:
            return '未找到对话，请先使用 /session 查看编号。'
        self.db.execute('UPDATE sessions SET saved=? WHERE id=? AND binding=?', (int(saved), sid, self.binding))
        self.db.commit()
        return (f'⭐ 已保存 #{sid}：{data["title"]}，所有清理策略都会跳过此对话。' if saved
                else f'已取消 #{sid} 的保存保护，此后可按清理策略删除。')

    def busy(self, sid):
        data = self.sessions.get(sid)
        if not data or data['status'] == '处理中':
            return True
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='evolution_tasks'").fetchone():
            if self.db.execute("SELECT 1 FROM evolution_tasks WHERE binding=? AND session_id=? "
                               "AND (state IN ('discussing','planned','approved','running','repairing','testing','restarting','interrupted') "
                               "OR notified=0)",
                               (self.binding, sid)).fetchone():
                return True
        for session_id, thread_id, root_id in self.db.execute(
                "SELECT session_id,thread_id,root_id FROM inbox WHERE binding=? AND state IN ('pending','failed') AND fast_lane=0", (self.binding,)):
            if session_id == sid:
                return True
            item = self.sessions.thread(sid)
            if item and ((thread_id and item['thread_id'] == thread_id) or (root_id and item['root_id'] == root_id)):
                return True
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='issue_notices'").fetchone():
            if self.db.execute("SELECT 1 FROM issue_notices WHERE session_id=? "
                               "AND state IN ('resolving','draft','armed','sending','uncertain')", (sid,)).fetchone():
                return True
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='scheduled_jobs'").fetchone():
            if self.db.execute("SELECT 1 FROM scheduled_jobs WHERE binding=? AND session_id=? "
                               "AND state IN ('pending','sending','uncertain')",
                               (self.binding, sid)).fetchone():
                return True
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='group_watches'").fetchone():
            if self.db.execute("SELECT 1 FROM group_watches WHERE binding=? AND session_id=? AND state='active'",
                               (self.binding, sid)).fetchone():
                return True
            if self.db.execute("SELECT 1 FROM group_watch_hits h JOIN group_watches w ON w.id=h.watch_id "
                               "WHERE w.binding=? AND w.session_id=? AND h.state='uncertain' LIMIT 1",
                               (self.binding, sid)).fetchone():
                return True
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='target_watches'").fetchone():
            if self.db.execute("SELECT 1 FROM target_watches WHERE binding=? AND session_id=? "
                               "AND state IN ('active','sending','uncertain')",
                               (self.binding, sid)).fetchone():
                return True
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='pipeline_tasks'").fetchone():
            if self.db.execute("SELECT 1 FROM pipeline_tasks WHERE binding=? AND session_id=? "
                              "AND state != 'deleted' LIMIT 1", (self.binding, sid)).fetchone():
                return True
        return False

    def matches(self, data, policy, now):
        if policy == 1:
            turns = self.db.execute("SELECT count(*) FROM session_history WHERE binding=? AND session_id=? AND role='user'",
                                    (self.binding, data['id'])).fetchone()[0]
            return turns < 5
        if policy in (2, 3):
            return data['last_used'] < now - (30 if policy == 2 else 7)*86400
        return policy == 4

    def clean(self, argument, source):
        if not argument:
            return '\n'.join(f'/clean {p}：{description}' for p, description in POLICIES.items()) + (
                '\n\n所有策略跳过已保存、处理中、待发送及有未结束通知的对话。'
                '\n先预览，再按清理编号确认；归档图片随对话清理，飞书消息和数据库备份不删除。')
        confirm = re.fullmatch(r'confirm\s+([0-9a-f]{12})', argument, re.I)
        if confirm:
            return self.confirm(confirm[1].lower())
        if argument not in ('1', '2', '3', '4'):
            return '参数无效。使用 /clean 查看策略，或 /clean confirm 清理编号 确认预览。'
        previous = self.db.execute('SELECT preview FROM clean_plans WHERE source=? AND binding=?', (source, self.binding)).fetchone()
        if previous:
            return previous[0]
        policy, now = int(argument), time.time()
        candidates, saved, busy = [], 0, 0
        for (sid,) in self.db.execute('SELECT id FROM sessions WHERE binding=? ORDER BY id', (self.binding,)).fetchall():
            data = self.sessions.get(sid)
            if not self.matches(data, policy, now):
                continue
            if data['saved']:
                saved += 1
            elif self.busy(sid):
                busy += 1
            else:
                candidates.append({'id': sid, 'updated': data['updated'], 'last_used': data['last_used']})
        if not candidates:
            return f'没有可清理的对话。已跳过保存保护 {saved} 个、处理中或待发送 {busy} 个。'
        token = hashlib.sha256(f'{self.binding}:{source}:{policy}'.encode()).hexdigest()[:12]
        lines = [f'清理预览：{POLICIES[policy]}', f'待删除 {len(candidates)} 个；跳过已保存 {saved} 个、处理中或待发送 {busy} 个。']
        lines += [f'#{x["id"]} {brief(self.sessions.get(x["id"])["title"], 30)}' for x in candidates[:10]]
        if len(candidates) > 10:
            lines += [f'另有 {len(candidates)-10} 个，合计 {len(candidates)} 个。']
        lines += ['将删除这些对话的本地历史、摘要、消息关联和归档图片；飞书消息和备份保留。',
                  f'确认请发送：/clean confirm {token}（10 分钟内有效）']
        preview = '\n'.join(lines)
        self.db.execute('INSERT INTO clean_plans VALUES(?,?,?,?,?,?,?,NULL)',
                        (token, self.binding, source, policy, json.dumps(candidates), now, preview))
        self.db.commit()
        return preview

    def confirm(self, token):
        # No awaits: validation, deletions, tombstones and receipt commit together.
        with self.db:
            if not self.db.in_transaction:
                self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT source,policy,candidates,created,result FROM clean_plans WHERE token=? AND binding=?',
                                  (token, self.binding)).fetchone()
            if not row:
                return '没有找到此清理预览，请重新发送 /clean 策略编号。'
            source, policy, raw, created, result = row
            if result:
                return result
            if time.time() - created > 600:
                return '清理预览已过期，请重新发送 /clean 策略编号。'
            delivered = self.db.execute("SELECT 1 FROM inbox WHERE id=? AND binding=? AND state='done'", (source, self.binding)).fetchone()
            if not delivered:
                return '清理预览尚未成功送达，未删除任何对话。'
            deleted, skipped = [], 0
            for item in json.loads(raw):
                data = self.sessions.get(item['id'])
                if (not data or data['saved'] or self.busy(data['id']) or data['updated'] != item['updated']
                        or data['last_used'] != item['last_used'] or not self.matches(data, policy, time.time())):
                    skipped += 1
                    continue
                self.delete(data['id'])
                deleted.append(data['id'])
            result = f'已删除 {len(deleted)} 个对话，跳过状态已变化或受保护的 {skipped} 个对话。'
            if deleted:
                result += '\n编号：' + '、'.join('#'+str(sid) for sid in deleted[:20])
                if len(deleted) > 20:
                    result += '等'
            self.db.execute('UPDATE clean_plans SET result=? WHERE token=?', (result, token))
        from assistant.image_archive import purge_deletions
        purge_deletions(self.db, self.project_root)
        from assistant.file_archive import purge_deletions as purge_files
        purge_files(self.db, self.project_root)
        return result

    def delete(self, sid):
        from assistant.image_archive import queue_session_delete
        queue_session_delete(self.db, self.binding, sid)
        from assistant.file_archive import queue_session_delete as queue_file_delete
        queue_file_delete(self.db, self.binding, sid)
        now = time.time()
        self.db.execute('INSERT OR IGNORE INTO deleted_session_messages SELECT binding,message_id,? FROM session_messages WHERE binding=? AND session_id=?',
                        (now, self.binding, sid))
        self.db.execute('INSERT OR IGNORE INTO deleted_session_messages SELECT binding,id,? FROM inbox WHERE binding=? AND session_id=?',
                        (now, self.binding, sid))
        self.db.execute('INSERT OR IGNORE INTO deleted_session_messages SELECT binding,root_id,? FROM session_threads WHERE binding=? AND session_id=?',
                        (now, self.binding, sid))
        self.db.execute('UPDATE session_threads SET session_id=NULL,link=NULL,deleted_at=? WHERE binding=? AND session_id=?',
                        (now, self.binding, sid))
        self.db.execute('DELETE FROM session_history WHERE binding=? AND session_id=?', (self.binding, sid))
        self.db.execute('DELETE FROM session_messages WHERE binding=? AND session_id=?', (self.binding, sid))
        self.db.execute('DELETE FROM task_updates WHERE source IN '
                        '(SELECT source FROM task_runs WHERE binding=? AND session_id=?)', (self.binding, sid))
        self.db.execute('DELETE FROM task_runs WHERE binding=? AND session_id=?', (self.binding, sid))
        # Keep message IDs for deduplication; remove the conversation content itself.
        self.db.execute("UPDATE inbox SET content='',reply='',quote=NULL,issue_refs=NULL,kind='deleted',session_id=NULL,route_reason='cleaned',state='done' WHERE binding=? AND session_id=?",
                        (self.binding, sid))
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='issue_notices'").fetchone():
            self.db.execute("UPDATE issue_notices SET payload='{}',reviewed=0,state='cleaned',session_id=NULL WHERE session_id=?", (sid,))
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='scheduled_jobs'").fetchone():
            self.db.execute("UPDATE scheduled_jobs SET body='',session_id=NULL WHERE binding=? AND session_id=?",
                            (self.binding, sid))
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='group_watches'").fetchone():
            self.db.execute("UPDATE group_watches SET session_id=NULL,chat_name='',last_error='',policy='{}' "
                            "WHERE binding=? AND session_id=?", (self.binding, sid))
            self.db.execute("UPDATE group_watch_hits SET content='',reason='' WHERE watch_id IN "
                            "(SELECT id FROM group_watches WHERE binding=? AND session_id IS NULL)",
                            (self.binding,))
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='target_watches'").fetchone():
            self.db.execute("UPDATE target_watches SET session_id=NULL,url='',last_result='' "
                            "WHERE binding=? AND session_id=?", (self.binding, sid))
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='evolution_tasks'").fetchone():
            self.db.execute("DELETE FROM evolution_tasks WHERE binding=? AND session_id=?", (self.binding, sid))
        self.db.execute('DELETE FROM sessions WHERE binding=? AND id=?', (self.binding, sid))

    def status(self, cfg, state):
        runtime = cfg.get('_runtime', {})
        uptime = max(0, int(time.monotonic()-runtime['started'])) if 'started' in runtime else None
        duration = f'{uptime//3600}小时{uptime%3600//60}分' if uptime is not None else '未知'
        active = runtime.get('active')
        pending = self.db.execute("SELECT count(*) FROM inbox WHERE binding=? AND state='pending' AND fast_lane=0 AND id!=?",
                                  (self.binding, active['message_id'] if active else '')).fetchone()[0]
        task = (f'{active["stage"]}' + (f' #{active["session_id"]}' if active.get('session_id') else '')
                + f'（{int(time.monotonic()-active["started"])}秒）') if active else '空闲'
        if active and active.get('message_id'):
            row = self.db.execute('SELECT stage,completed_events FROM task_runs WHERE source=? AND state=?',
                                  (active['message_id'], 'running')).fetchone()
            if row:
                task += f'｜{row[0]}，已确认 {row[1]} 个处理动作'
        total, saved = self.db.execute('SELECT count(*),coalesce(sum(saved),0) FROM sessions WHERE binding=?', (self.binding,)).fetchone()
        backend = cfg.get('AGENT_BACKEND', '未知')
        model = cfg.get(backend.upper()+'_MODEL') or 'CLI 默认（未显式指定）'
        error = self.db.execute("SELECT value FROM bot_health WHERE key='last_error'").fetchone()
        last_error = '无记录'
        info = json.loads(error[0]) if error else None
        if info:
            last_error = f'{format_time(info["time"], cfg)} {info["event"]}'
        evolution = self.db.execute("SELECT session_id,state FROM evolution_tasks WHERE binding=? "
                                    "AND state IN ('approved','running','repairing','testing','restarting') "
                                    "ORDER BY updated LIMIT 1", (self.binding,)).fetchone()
        labels = {'approved': '等待开始', 'running': '开发中', 'repairing': '自动修复中',
                  'testing': '验证中', 'restarting': '重启中'}
        evolution_line = (f'开发：#{evolution[0]} {labels[evolution[1]]}' if evolution
                          else '开发：当前无执行中的任务')
        return '\n'.join([f'运行：服务在线｜已运行 {duration}', f'飞书：{runtime.get("listener", "状态未知")}',
                          f'任务：{task}｜排队 {pending} 条', f'模型：{backend} / {model}',
                          f'会话：共 {total} 个｜已保存 {saved} 个｜按话题隔离',
                          evolution_line, f'最近异常：{last_error}'])
