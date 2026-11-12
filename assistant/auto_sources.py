"""Bounded Work Item reads and redaction for automatic replies."""
import json
import re


SENSITIVE = (
    re.compile(r'(?i)(?:bearer|basic)\s+[A-Za-z0-9+/._=-]{8,}'),
    re.compile(r'(?i)\b(?:glpat-|ghp_|sk-)[A-Za-z0-9_-]{8,}\b'),
    re.compile(r'(?i)\b(?:password|passwd|secret|token|api[_ -]?key|private[_ -]?key)\s*[:=]\s*[^\s，。；;,]+'),
    re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----'),
    re.compile(r'\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b'),
    re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)'),
    re.compile(r'(?<!\d)\d{17}[\dXx](?!\d)'),
    re.compile(r'(?i)https?://[^/\s:@]+:[^@\s/]+@'),
    re.compile(r'\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]{5,})?\b'),
    re.compile(r'(?<!\d)(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))(?:\.\d{1,3}){2,3}(?!\d)'),
)


WORK_ITEM_QUERY = '''query WorkItem($path: ID!, $iid: String!, $cursor: String) {
  namespace(fullPath: $path) {
    workItem(iid: $iid) {
      iid title state updatedAt workItemType { name }
      widgets {
        ... on WorkItemWidgetDescription { description }
        ... on WorkItemWidgetNotes {
          discussions(first: 50, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              notes(last: 10) {
                pageInfo { hasPreviousPage }
                nodes { body createdAt system }
              }
            }
          }
        }
      }
    }
  }
}'''


def scrub(value):
    """Remove common credentials and personal identifiers before model input."""
    if isinstance(value, dict):
        return {str(k): scrub(v) for k, v in value.items()
                if not re.search(r'(?i)password|secret|token|credential|authorization|private.?key', str(k))}
    if isinstance(value, list):
        return [scrub(item) for item in value[:50]]
    if not isinstance(value, str):
        return value
    text = value[:6000]
    for pattern in SENSITIVE:
        text = pattern.sub('[已隐藏]', text)
    return text


async def work_item(cfg, run, project, number):
    project = '/' + project.strip('/')
    cursor = ''
    notes = []
    complete = False
    item = None
    for _ in range(5):
        args = [cfg['GLAB_BIN'], 'api', 'graphql', '--hostname',
                cfg['GITLAB_HOST'], '--method', 'POST',
                '-f', 'query=' + WORK_ITEM_QUERY,
                '-f', 'path=' + project.lstrip('/'), '-f', 'iid=' + str(number)]
        if cursor:
            args += ['-f', 'cursor=' + cursor]
        result = json.loads(await run(args, 30))
        if (not isinstance(result, dict) or result.get('errors') or
                not isinstance(result.get('data'), dict)):
            raise ValueError('GitLab Work Item 查询失败')
        namespace = result['data'].get('namespace')
        item = namespace.get('workItem') if isinstance(namespace, dict) else None
        if not isinstance(item, dict) or str(item.get('iid')) != str(number):
            raise ValueError('GitLab Work Item 不存在或无权读取')
        widgets = item.get('widgets') or []
        if not isinstance(widgets, list):
            raise ValueError('GitLab Work Item 内容无效')
        discussions = next((widget.get('discussions') for widget in widgets
                            if isinstance(widget, dict) and 'discussions' in widget), None)
        if not isinstance(discussions, dict):
            complete = True
            break
        for discussion in discussions.get('nodes') or []:
            note_page = discussion.get('notes') if isinstance(discussion, dict) else None
            if isinstance(note_page, dict):
                for note in note_page.get('nodes') or []:
                    if isinstance(note, dict) and note.get('system') is not True and isinstance(note.get('body'), str):
                        notes.append({'created_at': note.get('createdAt'), 'body': note['body'][:2200]})
        page = discussions.get('pageInfo') or {}
        if page.get('hasNextPage') is not True:
            complete = True
            break
        next_cursor = page.get('endCursor')
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
            raise ValueError('GitLab Work Item 讨论分页无效')
        cursor = next_cursor
    widgets = item.get('widgets') or []
    description = next((widget.get('description') for widget in widgets
                        if isinstance(widget, dict) and isinstance(widget.get('description'), str)), '')
    notes.sort(key=lambda note: note.get('created_at') or '', reverse=True)
    return {'source': 'GitLab', 'resource': 'work_item', 'facts': {
        'project': project.lstrip('/'), 'iid': number,
        'title': item.get('title'), 'type': (item.get('workItemType') or {}).get('name'),
        'state': item.get('state'), 'updated_at': item.get('updatedAt'),
        'description': description[:5200],
        'description_truncated': len(description) > 5200,
        'recent_comments': notes[:4], 'comments_complete': complete,
        'web_url': 'https://' + cfg['GITLAB_HOST'] + project + '/-/work_items/' + str(number)}}
