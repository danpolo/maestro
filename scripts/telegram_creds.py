#!/usr/bin/env python3
"""Move Telegram dev-bot credentials from the operator's token file into a project's .env.

The point of this script is that **no agent ever sees the token**. It reads the token
file, validates it against the Telegram API, captures the chat id from the most recent
inbound message, and writes both into the target .env. Nothing sensitive is printed.

Setup, performed once by the operator:
    mkdir -p ~/.config/maestro
    printf '%s' '<token from @BotFather>' > ~/.config/maestro/dev_bot_token
    chmod 600 ~/.config/maestro/dev_bot_token
    # then send any message to that bot from Telegram, so getUpdates has a chat id

Usage:
    python3 scripts/telegram_creds.py /path/to/project/.env

Exit codes:
    0  credentials written
    2  token file missing, unreadable, or empty
    3  token rejected by the Telegram API
    4  no inbound message found, so no chat id could be captured
    5  target .env could not be written
"""
from __future__ import annotations

import json
import os
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path

TOKEN_FILE = Path(os.environ.get("MAESTRO_TOKEN_FILE", "~/.config/maestro/dev_bot_token")).expanduser()
API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 20


def _fail(code: int, message: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def _read_token() -> str:
    if not TOKEN_FILE.is_file():
        _fail(2, f"no token file at {TOKEN_FILE}")
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        _fail(2, f"cannot read {TOKEN_FILE}: {exc.strerror}")
    if not token:
        _fail(2, f"{TOKEN_FILE} is empty")
    mode = stat.S_IMODE(TOKEN_FILE.stat().st_mode)
    if mode & 0o077:
        print(f"WARNING: {TOKEN_FILE} is group/world readable (mode {mode:o}); chmod 600 recommended",
              file=sys.stderr)
    return token


def _api(token: str, method: str) -> dict:
    url = API.format(token=token, method=method)
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error_code": exc.code}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        _fail(3, f"{method} failed: {exc}")


def _capture_chat_id(token: str) -> int:
    payload = _api(token, "getUpdates")
    if not payload.get("ok"):
        _fail(3, "getUpdates rejected; is the token valid?")
    chat_ids = [
        u["message"]["chat"]["id"]
        for u in payload.get("result", [])
        if isinstance(u, dict) and "message" in u and "chat" in u["message"]
    ]
    if not chat_ids:
        _fail(4, "no inbound messages — send any message to the bot from Telegram, then re-run")
    return chat_ids[-1]


def _write_env(target: Path, token: str, chat_id: int) -> None:
    """Upsert the two keys, preserving every other line."""
    keys = {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_ALERT_CHAT_ID": str(chat_id)}
    lines: list[str] = []
    if target.exists():
        lines = target.read_text(encoding="utf-8").splitlines()
    seen = set()
    for i, line in enumerate(lines):
        name = line.split("=", 1)[0].strip()
        if name in keys:
            lines[i] = f"{name}={keys[name]}"
            seen.add(name)
    lines.extend(f"{k}={v}" for k, v in keys.items() if k not in seen)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        target.chmod(0o600)
    except OSError as exc:
        _fail(5, f"cannot write {target}: {exc.strerror}")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    target = Path(argv[1]).expanduser()

    token = _read_token()
    if not _api(token, "getMe").get("ok"):
        _fail(3, "token rejected by getMe")
    chat_id = _capture_chat_id(token)
    _write_env(target, token, chat_id)

    # chat id is not a secret; the token is never printed.
    print(f"ok: credentials written to {target} (chat_id={chat_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
