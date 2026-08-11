#!/usr/bin/env bash
# Overnight handoff chain for the Maestro build.
#
# The context ceiling cannot be raised, so instead of one long session this driver runs
# a CHAIN of fresh sessions. Each session does as much as it can, writes
# docs/PROGRESS.md, and exits; the driver relaunches a brand-new session that resumes
# from PROGRESS.md with a clean context. Repeats until the programme reports COMPLETE
# or ABORTED.
#
# This is deliberately the same handoff-not-compaction pattern Maestro itself
# implements (docs/DESIGN.md §3).
#
# Run it in a tmux window so it can be audited from a phone:
#   tmux new-window -t agents -n 'maestro-build' \
#     'cd ~/projects/maestro && bash scripts/run_overnight.sh'
#
# Env overrides: MAESTRO_REPO, MAESTRO_MODEL, MAX_ITER, LIMIT_SLEEP
set -uo pipefail

REPO="${MAESTRO_REPO:-$HOME/projects/maestro}"
PROGRESS="$REPO/docs/PROGRESS.md"
LOGDIR="$REPO/.run"
MAX_ITER="${MAX_ITER:-24}"
MODEL="${MAESTRO_MODEL:-claude-opus-5}"
# Fallback only: used when the reset-time lookup below fails (network hiccup,
# expired creds, unexpected API shape). Otherwise we sleep until the real reset.
LIMIT_SLEEP="${LIMIT_SLEEP:-3600}"

read -r -d '' PROMPT <<'EOF'
ultracode

Read docs/EXECUTION.md and execute the Maestro build from it, resuming at the current
stage in docs/PROGRESS.md. Run unattended per its "Unattended operation" section - do
not stop to ask permission.

You are one link in an automated handoff chain: a driver relaunches a fresh session
after you exit, so do NOT try to finish the whole programme in this session. Complete as
many stages as you can while your context stays under 150K, then update
docs/PROGRESS.md, commit, and exit cleanly.

Before exiting you MUST set the PROGRAMME-STATUS line in docs/PROGRESS.md to exactly one
of: IN-PROGRESS, COMPLETE, ABORTED. The driver reads that line to decide whether to
relaunch you. Setting COMPLETE while stages remain pending silently ends the build.
EOF

mkdir -p "$LOGDIR"
cd "$REPO" || { echo "[chain] cannot cd to $REPO"; exit 1; }

ts()     { date -u +%Y-%m-%dT%H:%M:%SZ; }
status() { grep -oE '^PROGRAMME-STATUS:[[:space:]]+[A-Z-]+' "$PROGRESS" 2>/dev/null \
             | head -1 | awk '{print $2}'; }
hash_p() { md5sum "$PROGRESS" 2>/dev/null | cut -d' ' -f1; }

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
import json, subprocess, sys
from datetime import datetime, timezone

fallback = int(sys.argv[1])
try:
    creds = json.load(open("/home/dan/.claude/.credentials.json"))
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

echo "[chain] $(ts) starting; repo=$REPO model=$MODEL max_iter=$MAX_ITER"
stale=0

for i in $(seq 1 "$MAX_ITER"); do
    st="$(status)"
    if [[ "$st" == "COMPLETE" || "$st" == "ABORTED" ]]; then
        echo "[chain] $(ts) programme $st after $((i - 1)) session(s)"
        break
    fi

    before="$(hash_p)"
    log="$LOGDIR/session-$(printf '%02d' "$i").log"
    echo "[chain] $(ts) session $i starting -> $log"

    claude -p --model "$MODEL" --dangerously-skip-permissions "$PROMPT" >"$log" 2>&1
    echo "[chain] $(ts) session $i exited rc=$?"

    # Check for a terminal status BEFORE the usage-limit scan below: a session that
    # legitimately finishes (COMPLETE/ABORTED) commonly narrates past limit hits in
    # its own report ("the monthly spend limit blocked sessions N-M"), which false-
    # matches the usage-limit regex just as well as a real fresh hit does. Without
    # this check that session would sleep for hours waiting on a reset nobody needs,
    # only to break on the *next* loop's status() check anyway - harmless but makes
    # a finished/aborted programme look like it's still grinding for hours. Observed
    # 2026-08-11: the ABORT report's own "spend limit" narration triggered exactly
    # this.
    st="$(status)"
    if [[ "$st" == "COMPLETE" || "$st" == "ABORTED" ]]; then
        echo "[chain] $(ts) programme $st after session $i"
        break
    fi

    # Quota exhaustion is not a failure: wait for the reset and try again.
    # Real observed message: "You've hit your monthly spend limit ..." -- shown for
    # 5h/weekly caps too, not just a distinct billing cap (confirmed 2026-08-09). The
    # original pattern required the literal phrase "hit your limit" and never matched
    # it, so real usage-limit hits were falling through to the stale-session guard
    # instead of sleeping and retrying. Broadened to catch phrasing variants.
    if grep -qiE "usage limit|hit (your|the) .*limit|rate.?limit|resets at|spend limit" "$log"; then
        wait_s="$(seconds_until_reset)"
        wake="$(date -u -d "+${wait_s} seconds" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)"
        echo "[chain] $(ts) usage limit detected; sleeping ${wait_s}s (until ~$wake, per the reset time reported by the usage API)"
        sleep "$wait_s"
        continue
    fi

    # Stall guard: two consecutive sessions that change nothing means something is
    # wrong that another session will not fix. Stop rather than burn the night.
    if [[ "$before" == "$(hash_p)" ]]; then
        stale=$((stale + 1))
        echo "[chain] $(ts) session $i made no change to PROGRESS.md (stale=$stale)"
        if [[ $stale -ge 2 ]]; then
            echo "[chain] $(ts) two sessions with no progress; stopping"
            break
        fi
        sleep 60
    else
        stale=0
    fi
done

echo "[chain] $(ts) finished; final status: $(status)"
echo "[chain] logs: $LOGDIR"

# Reference-project HALT sentinel: as of 2026-08-09 AbuAliArchive is deliberately paused by
# operator decision until this extraction finishes - see
# handoffs/2026-08-09_pause-reference-loop-and-harden-invariant.md. This driver must NOT auto-remove
# the sentinel; only the operator (or an explicit follow-up handoff) lifts the pause. Report state
# only.
HALT="$HOME/projects/AbuAliArchive/.orchestrator/HALT"
if [[ -f "$HALT" ]]; then
    echo "[chain] NOTE: HALT sentinel present at $HALT - reference loop is deliberately paused; leaving it in place"
elif ! pgrep -f orchestrator_run.py >/dev/null; then
    echo "[chain] WARNING: reference project's orchestrator is NOT running and no HALT sentinel is present"
fi
