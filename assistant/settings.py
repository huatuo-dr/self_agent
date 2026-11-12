"""Pure helpers shared by user-configured display and messaging behavior."""

from datetime import datetime
import re
from string import Formatter
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_TIMEZONE = 'Asia/Shanghai'
DEFAULT_BOT_NAME = 'AI 助手'
DEFAULT_SIGNATURE = 'AI 助手代发'
DEFAULT_ACK = '收到，我会跟进这条消息。'


def get_timezone(cfg=None):
    name = (cfg or {}).get('TIMEZONE') or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError('TIMEZONE 必须是有效的 IANA 时区名称') from None


def local_now(cfg=None):
    return datetime.now(get_timezone(cfg))


def bot_name(cfg=None):
    name = (cfg or {}).get('BOT_NAME', DEFAULT_BOT_NAME)
    if (not isinstance(name, str) or not name.strip() or len(name) > 100
            or any(ord(c) < 32 for c in name)):
        raise ValueError('BOT_NAME 必须是 1 至 100 字符的单行名称')
    return name.strip()


def message_signature(cfg=None):
    cfg = cfg or {}
    template = cfg.get('MESSAGE_SIGNATURE_TEMPLATE', DEFAULT_SIGNATURE)
    if (not isinstance(template, str) or not template.strip() or len(template) > 500
            or any(ord(c) < 32 for c in template)):
        raise ValueError('MESSAGE_SIGNATURE_TEMPLATE 必须是有效的单行署名')
    try:
        for _, field, spec, conversion in Formatter().parse(template):
            if field is not None and (field not in ('bot_name', 'owner_name') or spec or conversion):
                raise ValueError()
        profile = cfg.get('OWNER_PROFILE') or {}
        owner = next((value.strip() for value in (profile.get('nickname'), profile.get('name'))
                      if isinstance(value, str) and value.strip()), '用户')
        rendered = template.format(bot_name=bot_name(cfg), owner_name=owner).strip()
    except (ValueError, KeyError, AttributeError, IndexError):
        raise ValueError('署名模板仅支持 {bot_name} 和 {owner_name}，不支持格式转换或属性访问') from None
    if not rendered:
        raise ValueError('MESSAGE_SIGNATURE_TEMPLATE 格式化后的署名不能为空')
    return rendered


def sign_message(body, cfg=None):
    body = body.strip()
    signature = message_signature(cfg)
    return body if body.endswith(signature) else body + '\n\n' + signature


def strip_bot_mention(text, cfg=None):
    return re.sub(r'^@' + re.escape(bot_name(cfg)) + r'\s+', '', text, count=1).strip()
