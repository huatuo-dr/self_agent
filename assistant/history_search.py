"""Read-only search over active conversations in the bound private chat."""

from datetime import datetime
import re
from assistant.settings import get_timezone


PAGE_SIZE = 5
MAX_QUERY_LENGTH = 100
MATCH = '''s.binding=? AND (
    instr(lower(s.title),lower(?))>0 OR
    instr(lower(s.summary),lower(?))>0 OR
    instr(lower(s.next_step),lower(?))>0 OR
    EXISTS (SELECT 1 FROM session_history h
            WHERE h.binding=s.binding AND h.session_id=s.id
              AND instr(lower(h.content),lower(?))>0)
)'''


def parse_argument(argument):
    """Parse `/history words [--page N]` without treating SQL wildcards specially."""
    value = argument.strip()
    match = re.fullmatch(r'(?s)(.*?)\s+--page\s+([1-9]\d{0,3})', value)
    query, page = (match[1].strip(), int(match[2])) if match else (value, 1)
    if not query or len(query) > MAX_QUERY_LENGTH or '\x00' in query or ('--page' in value and not match):
        raise ValueError('用法：/history 关键词 [--page 页码]（关键词不超过 100 字）')
    return query, page


def excerpt(value, query, limit=110):
    value = ' '.join((value or '').split())
    position = value.casefold().find(query.casefold())
    start = max(0, position - 35) if position >= 0 else 0
    end = min(len(value), start + limit)
    return ('…' if start else '') + value[start:end] + ('…' if end < len(value) else '')


def search(db, binding, query, page=1):
    if not binding or not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_LENGTH or '\x00' in query:
        raise ValueError('Invalid history search input')
    if type(page) is not int or not 1 <= page <= 9999:
        raise ValueError('Invalid history search page')
    query = query.strip()
    parameters = (binding, query, query, query, query)
    total = db.execute(f'SELECT count(*) FROM sessions s WHERE {MATCH}', parameters).fetchone()[0]
    rows = db.execute(f'''SELECT s.id,s.title,s.summary,s.status,s.next_step,s.updated,s.saved,t.link
                          FROM sessions s LEFT JOIN session_threads t
                            ON t.binding=s.binding AND t.session_id=s.id
                          WHERE {MATCH}
                          ORDER BY s.updated DESC,s.id DESC LIMIT ? OFFSET ?''',
                      (*parameters, PAGE_SIZE, (page - 1) * PAGE_SIZE)).fetchall()
    items = []
    for sid, title, summary, status, next_step, updated, saved, link in rows:
        hit = db.execute('''SELECT role,content FROM session_history
                            WHERE binding=? AND session_id=? AND instr(lower(content),lower(?))>0
                            ORDER BY seq DESC LIMIT 1''', (binding, sid, query)).fetchone()
        source, content = ((('用户' if hit[0] == 'user' else 'bot'), hit[1]) if hit else
                           ('摘要', summary) if query.casefold() in summary.casefold() else
                           ('下一步', next_step) if query.casefold() in next_step.casefold() else
                           ('标题', title))
        items.append({'id': sid, 'title': title, 'status': status, 'saved': bool(saved),
                      'updated': updated, 'match_source': source,
                      'excerpt': excerpt(content, query), 'link': link})
    return {'query': query, 'page': page, 'pages': max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
            'total': total, 'items': items}


def markdown(value):
    return re.sub(r'([\\`*_{}\[\]()#+.!|>])', r'\\\1', str(value))


def format_result(result, cfg=None):
    query = markdown(result['query'])
    if not result['items']:
        if result['total']:
            return f'“{query}”共有 {result["total"]} 个命中，第 {result["page"]} 页没有结果。请使用较小页码。'
        return f'未找到包含“{query}”的未清理对话。可换关键词重试；已清理的本地正文不会恢复。'
    lines = [f'找到 {result["total"]} 个包含“{query}”的对话：']
    for item in result['items']:
        updated = datetime.fromtimestamp(item['updated'], get_timezone(cfg)).strftime('%Y-%m-%d %H:%M')
        line = (f'#{item["id"]} {markdown(item["title"])}｜{markdown(item["status"])}｜{updated}'
                + ('｜⭐ 已保存' if item['saved'] else '')
                + f'\n命中 {item["match_source"]}：{markdown(item["excerpt"])}')
        line += (f'\n[进入原话题]({item["link"]})' if item['link']
                 else f'\n话题入口暂不可用，可发送 /session {item["id"]} 重试获取')
        lines.append(line)
    lines.append(f'第 {result["page"]}/{result["pages"]} 页；在原命令后加 --page {result["page"] + 1} 查看下一页'
                 if result['page'] < result['pages'] else f'第 {result["page"]}/{result["pages"]} 页')
    lines.append('请进入原话题继续任务；在主聊天发新任务会创建新话题。')
    return '\n\n'.join(lines)
