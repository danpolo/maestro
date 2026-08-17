#!/usr/bin/env bash
# Shared helpers for scripts/run_overnight.sh and scripts/watchdog_selffix.sh.
# Source this file after setting the variables each function needs.
#
# ts()                     no inputs.
# status()                 requires: PROGRESS (path to docs/PROGRESS.md)
# hash_p()                 requires: PROGRESS
# seconds_until_reset()    requires: LIMIT_SLEEP (fallback seconds if the usage
#                          API lookup fails). Optional: MAESTRO_CREDENTIALS_FILE
#                          (default ~/.claude/.credentials.json).
# is_live_usage_limit()    arg1: path to a session log.

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

status() {
    grep -oE '^PROGRAMME-STATUS:[[:space:]]+[A-Z-]+' "$PROGRESS" 2>/dev/null \
        | head -1 | awk '{print $2}'
}

hash_p() {
    md5sum "$PROGRESS" 2>/dev/null | cut -d' ' -f1
}

# "You've hit your monthly spend limit" is shown for the regular 5h *and* weekly
# caps too (confirmed 2026-08-09/11 - not a distinct billing cap most of the time).
# A flat hourly retry is fine for a 5h window but wastes up to a week of hourly
# wake-ups against a weekly one (observed: 24 straight hourly misses on
# 2026-08-11). The Anthropic usage API reports exact reset times for both windows,
# so ask it which one is actually binding (highest utilization) and sleep until
# that reset instead of guessing. Falls back to $LIMIT_SLEEP on any failure -
# same behaviour as before this existed.
seconds_until_reset() {
    python3 - "$LIMIT_SLEEP" <<'PYEOF'
import json, os, subprocess, sys
from datetime import datetime, timezone

fallback = int(sys.argv[1])
creds_path = os.environ.get("MAESTRO_CREDENTIALS_FILE", "/home/dan/.claude/.credentials.json")
try:
    creds = json.load(open(creds_path))
    token = creds["claudeAiOauth"]["accessToken"]
    result = subprocess.run([
        "curl", "-s",
        "-H", f"Authorization: Bearer {token}",
        "-H", "Content-Type: application/json",
        "-H", "anthropic-beta: oauth-2025-04-20",
        "-H", "User-Agent: claude-code/2.1.41",
        "https://api.anthropic.com/api/oauth/usage",
    ], capture_output=True, text=True, timeout=10)
    data = json.loads(result.stdout)

    candidates = []
    for key in ("five_hour", "seven_day"):
        u = data.get(key)
        if u and u.get("resets_at") is not None:
            candidates.append((u.get("utilization") or 0, u["resets_at"]))
    if not candidates:
        raise ValueError("no usage windows in API response")

    # The binding limit is whichever window is most utilized right now.
    _, resets_at = max(candidates, key=lambda c: c[0])
    resets = datetime.fromisoformat(resets_at)
    secs = int((resets - datetime.now(timezone.utc)).total_seconds()) + 60
    # Sanity floor/ceiling: never busy-loop, never trust a >8-day figure.
    secs = max(secs, 60)
    secs = min(secs, 8 * 24 * 3600)
    print(secs)
except Exception:
    print(fallback)
PYEOF
}

# A LIVE usage-limit block prints the CLI's message and nothing else: the turn
# stops right there. Confirmed 2026-08-17 against 20 real hits in this build's
# .run/session-*.log history - every one is a short log ending in the literal
# message, e.g.:
#   You've hit your monthly spend limit · raise it at claude.ai/settings/usage...
# A session that finishes normally can still contain that same phrase - e.g.
# narrating a *sub-agent* that hit the limit earlier and was recovered from
# within the same session (observed: session-08.log, 31 lines, match on line
# 26, five more lines of report follow) - which false-matched a whole-file
# grep and cost a ~44h stall waiting on a reset nothing was blocked on.
# Restricting the check to the log's last non-empty line tells a live block
# apart from narration: narration is never the last thing a finished report
# says.
is_live_usage_limit() {
    local log="$1"
    grep -v '^[[:space:]]*$' "$log" 2>/dev/null | tail -1 \
        | grep -qiE "usage limit|hit (your|the) .*limit|rate.?limit|resets at|spend limit"
}
