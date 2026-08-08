#!/usr/bin/env python3
"""Identify the bot behind the operator's token file, without revealing the token.

Prints the bot's @username (so the operator knows which chat to message) and whether a
webhook is set — a webhook silently makes getUpdates return nothing, which is the one
non-obvious reason credential capture fails.

Usage: python3 scripts/telegram_whoami.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

TOKEN_FILE = Path(os.environ.get("MAESTRO_TOKEN_FILE", "~/.config/maestro/dev_bot_token")).expanduser()
TIMEOUT = 20


def _api(token: str, method: str) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"ERROR: {method} failed: {exc}", file=sys.stderr)
        raise SystemExit(3)


def main() -> int:
    if not TOKEN_FILE.is_file():
        print(f"ERROR: no token file at {TOKEN_FILE}", file=sys.stderr)
        return 2
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()

    me = _api(token, "getMe")
    if not me.get("ok"):
        print("ERROR: token rejected by getMe", file=sys.stderr)
        return 3
    bot = me["result"]
    print(f"bot: @{bot.get('username')}  (name: {bot.get('first_name')!r}, id: {bot.get('id')})")

    hook = _api(token, "getWebhookInfo").get("result", {})
    if hook.get("url"):
        print(f"WARNING: webhook set to {hook['url']} — getUpdates will always be empty.")
        print("         Clear it with the deleteWebhook API method, then re-run.")
    else:
        print("webhook: none (getUpdates is usable)")

    updates = _api(token, "getUpdates").get("result", [])
    msgs = [u for u in updates if "message" in u]
    print(f"pending updates: {len(updates)} ({len(msgs)} with a message)")
    if not msgs:
        print(f"=> send any message to @{bot.get('username')} from Telegram, then re-run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
