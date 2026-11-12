"""Validated publication target shared by configuration and the publisher."""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_PAGES_BRANCH = "main"


@dataclass(frozen=True)
class PagesTarget:
    origin: str
    host: str
    project: str
    branch: str
    port: int = 22

    @property
    def directory(self):
        return self.project.rsplit("/", 1)[-1]

    @property
    def identity(self):
        return dict(
            host=self.host, project=self.project, port=self.port, branch=self.branch
        )

    @property
    def web_url(self):
        return f"https://{self.host}/{self.project}"


def pages_target(cfg):
    return repository_target(cfg, 'PIPELINE_PAGES_REPO_URL', 'PIPELINE_PAGES_BRANCH')


def report_target(cfg):
    return repository_target(cfg, 'REPORT_PAGES_REPO_URL', 'REPORT_PAGES_BRANCH')


def repository_target(cfg, url_field, branch_field):
    origin = cfg.get(url_field) or ''
    branch = cfg.get(branch_field) or DEFAULT_PAGES_BRANCH
    message = f"{url_field} 必须是 git@主机:命名空间/仓库.git 或 ssh://git@主机[:端口]/命名空间/仓库.git"
    if not isinstance(origin, str) or any(c.isspace() for c in origin):
        raise ValueError(message)
    port = 22
    if origin.startswith("ssh://"):
        try:
            url = urlsplit(origin)
            if (
                url.username != "git"
                or url.password is not None
                or url.query
                or url.fragment
            ):
                raise ValueError(message)
            host, project = url.hostname, url.path.removeprefix("/")
            port = 22 if url.port is None else url.port
        except ValueError:
            raise ValueError(message) from None
    else:
        match = re.fullmatch(r"git@([^:]+):(.+)", origin)
        if not match:
            raise ValueError(message)
        host, project = match.groups()
    if (
        not host
        or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host)
        or ".." in host
    ):
        raise ValueError(message)
    if not 1 <= port <= 65535:
        raise ValueError(message)
    project = project.removesuffix(".git")
    parts = project.split("/")
    if (
        len(parts) < 2
        or any(
            not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", part)
            or part.endswith(".")
            or ".." in part
            for part in parts
        )
        or parts[-1].lower() == "credential"
    ):
        raise ValueError(message)
    if (
        not isinstance(branch, str)
        or any(
            not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", part)
            or ".." in part
            or part.endswith((".", ".lock"))
            for part in branch.split("/")
        )
        or branch == "HEAD"
    ):
        raise ValueError(f"{branch_field} 不是有效的发布分支名")
    return PagesTarget(origin, host.lower(), project, branch, port)
