"""Bind a Feishu private-chat sender to this project's .env using a one-time phrase."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def save_binding(app_id, event):
    values = {
        "FEISHU_APP_ID": app_id,
        "FEISHU_OWNER_OPEN_ID": event["sender_id"],
        "FEISHU_OWNER_CHAT_ID": event["chat_id"],
    }
    for key, value in values.items():
        prefix = "cli_" if key == "FEISHU_APP_ID" else "ou_" if key == "FEISHU_OWNER_OPEN_ID" else "oc_"
        if not re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_]+", value):
            raise ValueError("Invalid binding identifier")
    target = ROOT / ".env"
    if target.is_symlink():
        raise ValueError("Refusing to replace a symlink .env")
    lines = target.read_text().splitlines() if target.exists() else []
    # Preserve unrelated configuration; never evaluate .env as shell code.
    lines = [line for line in lines if not re.match(
        r"^\s*(?:export\s+)?(?:" + "|".join(values) + r")\s*=", line)]
    lines.extend(f"{key}={value}" for key, value in values.items())
    fd, name = tempfile.mkstemp(prefix=".binding-", dir=ROOT)
    try:
        with os.fdopen(fd, "w") as output:
            output.write("\n".join(lines) + "\n")
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phrase", required=True)
    parser.add_argument("--timeout", default="15m")
    args = parser.parse_args()
    status = subprocess.run(["lark-cli", "auth", "status", "--json", "--verify"],
                            capture_output=True, text=True, check=True)
    info = json.loads(status.stdout)
    if not info.get("identities", {}).get("bot", {}).get("verified"):
        raise SystemExit("Bot credentials are not verified")
    if not re.fullmatch(r"cli_[A-Za-z0-9_]+", info.get("appId", "")):
        raise SystemExit("Missing application ID")
    expression = 'select(.chat_type == "p2p" and .sender_type == "user" and .content == ' + json.dumps(args.phrase) + ')'
    process = subprocess.Popen([
        "lark-cli", "event", "consume", "im.message.receive_v1", "--as", "bot",
        "--timeout", args.timeout, "--jq", expression,
    ], cwd=ROOT, stdout=subprocess.PIPE, text=True)
    try:
        for line in process.stdout:
            event = json.loads(line)
            if event.get("chat_type") != "p2p" or event.get("sender_type") != "user" or event.get("content") != args.phrase:
                continue
            save_binding(info["appId"], event)
            print("BOUND: owner and private-chat IDs saved to .env", flush=True)
            return
        code = process.wait()
        raise SystemExit(f"No matching binding message received (CLI exit {code})")
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


if __name__ == "__main__":
    main()
