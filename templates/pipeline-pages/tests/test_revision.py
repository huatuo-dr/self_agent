"""Verify latest-commit gating without API scopes or disclosing clone credentials."""

import importlib.util
import io
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

SPEC = importlib.util.spec_from_file_location(
    "check_revision", Path(__file__).resolve().parents[1] / "scripts/check_revision.py"
)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


def packet(value):
    return f"{len(value) + 4:04x}".encode() + value


def advertisement(sha):
    return (
        packet(b"# service=git-upload-pack\n")
        + b"0000"
        + packet((sha + " HEAD\0multi_ack\n").encode())
        + packet((sha + " refs/heads/release/main\n").encode())
        + b"0000"
    )


class RevisionTests(unittest.TestCase):
    def setUp(self):
        env = dict(
            CI_REPOSITORY_URL="https://gitlab-ci-token:fixture-token@git.example/g/r.git",
            CI_DEFAULT_BRANCH="release/main",
            CI_COMMIT_SHA="a" * 40,
        )
        environment = patch.dict(CHECK.os.environ, env)
        environment.start()
        self.addCleanup(environment.stop)

    def response(self, data):
        result = io.BytesIO(data)
        result.headers = Message()
        result.headers["Content-Type"] = "application/x-git-upload-pack-advertisement"
        return result

    def test_current_commit_uses_git_permission_and_header(self):
        opener = Mock()
        opener.open.return_value = self.response(advertisement("a" * 40))
        with patch.object(CHECK.urllib.request, "build_opener", return_value=opener):
            CHECK.check_revision()
        request = opener.open.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://git.example/g/r.git/info/refs?service=git-upload-pack",
        )
        self.assertTrue(request.get_header("Authorization").startswith("Basic "))
        self.assertNotIn("fixture-token", request.full_url)

    def test_obsolete_commit_refuses_deployment(self):
        opener = Mock()
        opener.open.return_value = self.response(advertisement("b" * 40))
        with patch.object(CHECK.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(SystemExit, "Obsolete"):
                CHECK.check_revision()

    def test_unreadable_head_refuses_deployment(self):
        opener = Mock()
        opener.open.side_effect = HTTPError(
            "https://git.example/g/r.git", 404, "Not Found", {}, None
        )
        with patch.object(CHECK.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(HTTPError):
                CHECK.check_revision()

    def test_malformed_or_missing_branch_refuses_deployment(self):
        for data in (
            b"<html>login</html>",
            advertisement("a" * 40)[:-5],
            advertisement("a" * 40).replace(b"release/main", b"release/none"),
        ):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    CHECK.advertised_head(data, "release/main")

    def test_large_advertisement_is_rejected(self):
        opener = Mock()
        opener.open.return_value = self.response(b"x" * 101)
        with (
            patch.object(CHECK.urllib.request, "build_opener", return_value=opener),
            patch.object(CHECK, "MAX_REF_BYTES", 100),
        ):
            with self.assertRaisesRegex(ValueError, "size limit"):
                CHECK.check_revision()

    def test_redirects_do_not_forward_credentials(self):
        self.assertIsNone(
            CHECK.NoRedirect().redirect_request(
                None, None, 302, "Found", {}, "https://other.example"
            )
        )
