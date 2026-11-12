"""Retain only safe HTTP status/backoff metadata from a failed CLI request."""

import re
import time
from email.utils import parsedate_to_datetime


def http_metadata(text):
    statuses = re.findall(r"(?m)^HTTP/\S+\s+(\d{3})\b", text)
    if not statuses:
        statuses = re.findall(r"\(HTTP (\d{3})\)", text)
    status = int(statuses[-1]) if statuses else None
    retry = None
    values = re.findall(r"(?im)^Retry-After:\s*([^\r\n]+)", text)
    if values:
        value = values[-1].strip()
        try:
            retry = (
                int(value)
                if value.isdecimal()
                else parsedate_to_datetime(value).timestamp() - time.time()
            )
            retry = max(1, retry)
        except (ValueError, TypeError, OverflowError):
            pass
    return status, retry


class HTTPCommandError(RuntimeError):
    def __init__(self, program, exit_code, output, error):
        self.http_status, self.retry_after = http_metadata(output + "\n" + error)
        super().__init__(f"{program} failed (exit {exit_code})")


class PublicationBlocked(ValueError):
    """Only fixed, public diagnostics may cross the notification boundary."""

    REASONS = {
        "dirty": "报告工作区有未审阅改动，暂停自动提交",
        "branch": "报告工作区不是配置的发布分支；请核对 PIPELINE_PAGES_BRANCH 并切回发布分支",
        "ahead": "报告工作区存在未知本地提交，需核对后恢复发布",
        "missing_ci": "基础 Pages CI 尚未安装",
        "shared": "共享生成器或 CI 已被修改；请在监控话题回复“审查发布变更”，审阅后按提示确认",
    }

    def __init__(self, code):
        super().__init__(self.REASONS.get(code, "报告发布被阻止，请核查本地发布状态"))


class GitCommandError(RuntimeError):
    """Safe, fixed-vocabulary diagnostics; never retain Git's raw output."""

    def __init__(self, action, exit_code=None, diagnostic="", timed_out=False):
        actions = {
            "fetch",
            "push",
            "merge",
            "merge-base",
            "rev-parse",
            "rev-list",
            "status",
            "branch",
            "remote",
            "ls-tree",
            "show",
            "diff",
            "add",
            "commit",
            "worktree",
        }
        action = action if action in actions else "操作"
        reason = "命令执行失败"
        for pattern, description in [
            (r"non-fast-forward|fetch first", "远端分支已前进，需重新同步"),
            (
                r"permission denied|authentication failed|access denied",
                "SSH 认证或仓库访问被拒绝",
            ),
            (r"could not resolve|name or service not known", "无法解析 GitLab 主机名"),
            (
                r"connection timed out|connection reset|connection refused|remote end hung up",
                "GitLab 网络连接失败",
            ),
            (r"index.lock.*file exists", "Git 索引锁已存在"),
            (r"bad object|not a valid object|unknown revision", "Git 对象或引用不可用"),
            (
                r"would be overwritten|not possible to fast-forward",
                "本地改动或分支历史阻止快进更新",
            ),
        ]:
            if re.search(pattern, diagnostic, re.I | re.S):
                reason = description
                break
        if timed_out:
            reason = "执行超时"
        code = f"，退出码 {exit_code}" if exit_code is not None else ""
        super().__init__(f"Git {action}：{reason}{code}")
