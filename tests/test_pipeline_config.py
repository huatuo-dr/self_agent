import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from assistant.pipeline import PipelineMonitor
from assistant.pipeline_config import pages_target
from assistant.service import database


class PagesConfiguration(unittest.TestCase):
    def test_defaults_and_ssh_forms(self):
        default = pages_target({"PIPELINE_PAGES_REPO_URL": "git@gitlab.example.com:reports/pipeline-pages.git"})
        with self.assertRaises(ValueError):
            pages_target({})
        self.assertEqual(
            (default.host, default.project, default.directory, default.branch),
            ("gitlab.example.com", "reports/pipeline-pages", "pipeline-pages", "main"),
        )
        self.assertEqual(
            default.identity,
            pages_target(
                dict(
                    PIPELINE_PAGES_REPO_URL="ssh://git@gitlab.example.com/reports/pipeline-pages.git"
                )
            ).identity,
        )
        custom = pages_target(
            dict(
                PIPELINE_PAGES_REPO_URL="ssh://git@reports.example:2222/team/sub/site.git",
                PIPELINE_PAGES_BRANCH="release/pages",
            )
        )
        self.assertEqual(
            (custom.project, custom.directory, custom.port, custom.branch),
            ("team/sub/site", "site", 2222, "release/pages"),
        )

    def test_invalid_urls_and_refs_are_rejected_without_echoing_input(self):
        for url in (
            "https://host/g/r.git",
            "/tmp/r",
            "git@host:../r.git",
            "git@host:g/../r.git",
            "git@host:g/credential.git",
            "git@host:g//r.git",
            "git@host:g/r.git\n",
            "ssh://git:secret@host/g/r",
            "ssh://git@host:0/g/r",
            "ssh://git@host:99999/g/r",
            "ssh://git@host/g/r?secret=value",
            "git@host:g/-r.git",
            "git@host:r.git",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError) as error:
                pages_target(dict(PIPELINE_PAGES_REPO_URL=url))
            self.assertNotIn("secret", str(error.exception))
        for branch in (
            "-main",
            "main..new",
            "main.lock",
            "a/.b",
            "a//b",
            "a/",
            "main:other",
            "HEAD",
            "a@{1}",
        ):
            with self.subTest(branch=branch), self.assertRaises(ValueError):
                pages_target(dict(PIPELINE_PAGES_REPO_URL="git@gitlab.example.com:reports/pipeline-pages.git", PIPELINE_PAGES_BRANCH=branch))

    def test_report_api_host_is_separate_from_monitored_repository_host(self):
        db = database(":memory:")
        try:
            monitor = PipelineMonitor(
                dict(
                    FEISHU_OWNER_CHAT_ID="oc_owner",
                    GITLAB_HOST="source.example",
                    GLAB_BIN="glab",
                    LARK_CLI_BIN="lark",
                    PIPELINE_PAGES_REPO_URL="git@reports.example:team/site.git",
                ),
                db,
                AsyncMock(),
                Path("/tmp/unused"),
            )
            self.assertEqual(monitor.gitlab.host, "source.example")
            self.assertEqual(monitor.pages_gitlab.host, "reports.example")
        finally:
            db.close()
