#!/usr/bin/env bash
# Send a Telegram message via the same bot/chat used by the Claude Code Telegram
# bridge (~/.claude/channels/telegram/), independent of the live bridge process
# (which is guarded by services/claude-bridge/patch-telegram-plugin.sh against any
# non-bridge session using the plugin channel directly).
# Usage: notify_telegram_bridge.sh "<message text>"
#
# Reads TELEGRAM_BOT_TOKEN from $MAESTRO_TELEGRAM_ENV_FILE (default
# ~/.claude/channels/telegram/.env) and the chat id from the first entry of
# allowFrom in $MAESTRO_TELEGRAM_ACCESS_FILE (default
# ~/.claude/channels/telegram/access.json). Exits 0 and skips silently if either
# is missing, matching this environment's existing safe-skip-on-missing-config
# convention.
set -uo pipefail

ENV_FILE="${MAESTRO_TELEGRAM_ENV_FILE:-$HOME/.claude/channels/telegram/.env}"
ACCESS_FILE="${MAESTRO_TELEGRAM_ACCESS_FILE:-$HOME/.claude/channels/telegram/access.json}"
API_BASE="${MAESTRO_TELEGRAM_API_BASE:-https://api.telegram.org}"

if [ ! -f "$ENV_FILE" ] || [ ! -f "$ACCESS_FILE" ]; then
    echo "[notify_telegram_bridge] $ENV_FILE or $ACCESS_FILE not found — skipping." >&2
    exit 0
fi

TELEGRAM_BOT_TOKEN="$(grep -oE '^TELEGRAM_BOT_TOKEN=.*' "$ENV_FILE" | head -1 | cut -d= -f2-)"
if [ -z "$TELEGRAM_BOT_TOKEN" ]; then
    echo "[notify_telegram_bridge] TELEGRAM_BOT_TOKEN not set in $ENV_FILE — skipping." >&2
    exit 0
fi

export _NOTIFY_TOKEN="$TELEGRAM_BOT_TOKEN"
export _NOTIFY_ACCESS_FILE="$ACCESS_FILE"
export _NOTIFY_API_BASE="$API_BASE"
export _NOTIFY_MSG="$*"

python3 - <<'PYEOF'
import json, os, sys, urllib.request, urllib.parse

token       = os.environ["_NOTIFY_TOKEN"]
api_base    = os.environ["_NOTIFY_API_BASE"]
access_file = os.environ["_NOTIFY_ACCESS_FILE"]
msg         = os.environ.get("_NOTIFY_MSG", "")

try:
    access = json.load(open(access_file, encoding="utf-8"))
    chat_id = access["allowFrom"][0]
except Exception as e:
    print(f"[notify_telegram_bridge] cannot read chat id from {access_file}: {e}", file=sys.stderr)
    sys.exit(0)

LIMIT = 4096
if len(msg) > LIMIT:
    msg = msg[: LIMIT - 20] + "\n… [truncated]"

data = urllib.parse.urlencode({"chat_id": chat_id, "text": msg}).encode()
req = urllib.request.Request(
    f"{api_base}/bot{token}/sendMessage",
    data=data,
    headers={"Content-Type": "application/x-www-form-urlencoded"},
)
try:
    urllib.request.urlopen(req, timeout=10)
    print("[notify_telegram_bridge] Alert sent.")
except Exception as e:
    print(f"[notify_telegram_bridge] Error: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
