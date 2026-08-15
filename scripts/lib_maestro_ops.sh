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
