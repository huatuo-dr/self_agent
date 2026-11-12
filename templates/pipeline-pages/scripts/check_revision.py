#!/usr/bin/env python3
"""Fail obsolete builds using the same Git read permission as Runner checkout."""

import base64
import os
import re
import urllib.parse
import urllib.request

MAX_REF_BYTES = 2 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward checkout credentials to another location.
        return None


def advertised_head(data, branch):
    """Parse a bounded Git smart-HTTP v0 ref advertisement, without fetching objects.

    https://git-scm.com/docs/http-protocol#_smart_clients
    """
    packets, offset = [], 0
    while offset < len(data):
        prefix = data[offset : offset + 4]
        if not re.fullmatch(rb"[0-9a-f]{4}", prefix):
            raise ValueError("Invalid Git reference packet")
        size = int(prefix, 16)
        if size == 0:
            packets.append(b"")
            offset += 4
            continue
        if size < 4 or offset + size > len(data):
            raise ValueError("Truncated Git reference packet")
        packets.append(data[offset + 4 : offset + size])
        offset += size
    if (
        len(packets) < 3
        or packets[0].rstrip(b"\n") != b"# service=git-upload-pack"
        or packets[1] != b""
        or packets[-1] != b""
    ):
        raise ValueError("Unexpected Git reference advertisement")
    wanted = ("refs/heads/" + branch).encode()
    matches = []
    for packet in packets[2:]:
        value = packet.split(b"\0", 1)[0].rstrip(b"\n")
        sha, _, ref = value.partition(b" ")
        if ref == wanted and re.fullmatch(rb"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            matches.append(sha.decode("ascii"))
    if len(matches) != 1:
        raise ValueError("Default branch could not be verified")
    return matches[0]


def check_revision():
    # CI_REPOSITORY_URL is Runner's existing authenticated clone URL. Credentials
    # are stripped from the request URL and used only in the Authorization header.
    repo = urllib.parse.urlsplit(os.environ["CI_REPOSITORY_URL"])
    if repo.scheme not in ("http", "https") or not repo.hostname or not repo.password:
        raise ValueError("Missing authenticated CI repository URL")
    url = urllib.parse.urlunsplit(
        (
            repo.scheme,
            repo.netloc.rsplit("@", 1)[-1],
            repo.path.rstrip("/") + "/info/refs",
            "service=git-upload-pack",
            "",
        )
    )
    credentials = (
        urllib.parse.unquote(repo.username or "")
        + ":"
        + urllib.parse.unquote(repo.password)
    )
    authorization = "Basic " + base64.b64encode(credentials.encode()).decode("ascii")
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": authorization,
            "Cache-Control": "no-cache",
        },
    )
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(request, timeout=30) as response:
        if (
            response.headers.get_content_type()
            != "application/x-git-upload-pack-advertisement"
        ):
            raise ValueError("Expected Git smart-HTTP ref response")
        data = response.read(MAX_REF_BYTES + 1)
    if len(data) > MAX_REF_BYTES:
        raise ValueError("Git reference response exceeds size limit")
    current = advertised_head(data, os.environ["CI_DEFAULT_BRANCH"])
    if current != os.environ["CI_COMMIT_SHA"]:
        raise SystemExit("Obsolete Pages commit: refusing deployment")


if __name__ == "__main__":
    check_revision()
