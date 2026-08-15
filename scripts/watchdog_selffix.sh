#!/usr/bin/env bash
# Detects a maestro-build driver that has stopped while PROGRAMME-STATUS is still
# IN-PROGRESS, or a COMPLETE/ABORTED status that hasn't been reported yet, and acts.
# Entry point for maestro-watchdog.service (see systemd/maestro-watchdog.{timer,service}).
# See docs/superpowers/specs/2026-08-14-maestro-build-watchdog-design.md.
#
# Env overrides (for tests and manual runs): MAESTRO_REPO, MAESTRO_WATCHDOG_STATE,
# MAESTRO_WATCHDOG_LOCK, MAESTRO_NOTIFY_SCRIPT, MAESTRO_FIXER_TIMEOUT,
# MAESTRO_FIXER_MODEL, MAESTRO_BUILD_UNIT, MAESTRO_CLAUDE_BIN, MAESTRO_STALL_COOLDOWN
set -uo pipefail

REPO="${MAESTRO_REPO:-$HOME/projects/maestro}"
PROGRESS="$REPO/docs/PROGRESS.md"
LOGDIR="$REPO/.run"
STATE_FILE="${MAESTRO_WATCHDOG_STATE:-$LOGDIR/watchdog_state.json}"
LOCK_FILE="${MAESTRO_WATCHDOG_LOCK:-$LOGDIR/watchdog.lock}"
NOTIFY="${MAESTRO_NOTIFY_SCRIPT:-$REPO/scripts/notify_telegram_bridge.sh}"
FIXER_TIMEOUT="${MAESTRO_FIXER_TIMEOUT:-1800}"
FIXER_MODEL="${MAESTRO_FIXER_MODEL:-claude-sonnet-5}"
UNIT="${MAESTRO_BUILD_UNIT:-maestro-build.service}"
CLAUDE_BIN="${MAESTRO_CLAUDE_BIN:-claude}"
STALL_COOLDOWN="${MAESTRO_STALL_COOLDOWN:-5400}"   # 90 min
LIMIT_SLEEP=3600

mkdir -p "$LOGDIR"
# shellcheck source=scripts/lib_maestro_ops.sh
source "$REPO/scripts/lib_maestro_ops.sh"

state_get() {  # state_get <key> -> prints value or empty string
    python3 - "$STATE_FILE" "$1" <<'PYEOF'
import json, sys
path, key = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(path, encoding="utf-8"))
except Exception:
    data = {}
val = data.get(key)
print(val if val is not None else "")
PYEOF
}

state_set() {  # state_set <key> <value>
    python3 - "$STATE_FILE" "$1" "$2" <<'PYEOF'
import json, sys
path, key, value = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    data = json.load(open(path, encoding="utf-8"))
except Exception:
    data = {}
data[key] = value
json.dump(data, open(path, "w", encoding="utf-8"))
PYEOF
}

acquire_lock() {
    if [ -f "$LOCK_FILE" ]; then
        old_pid="$(cat "$LOCK_FILE" 2>/dev/null || true)"
        if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
            echo "[watchdog] $(ts) another run (pid $old_pid) still active; exiting" >&2
            exit 0
        fi
        echo "[watchdog] $(ts) stale lock (pid $old_pid); taking over" >&2
    fi
    echo "$$" > "$LOCK_FILE"
    trap 'rm -f "$LOCK_FILE"' EXIT
}

past() {  # past <iso-ts-or-empty> -> success (0) if empty or already in the past
    local when="$1"
    [ -z "$when" ] && return 0
    python3 - "$when" <<'PYEOF'
import sys
from datetime import datetime, timezone
when = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
sys.exit(0 if datetime.now(timezone.utc) >= when else 1)
PYEOF
}

handle_terminal_status() {
    local st="$1"
    local reported
    reported="$(state_get reported_terminal_status)"
    if [ "$reported" = "$st" ]; then
        return 0
    fi
    case "$st" in
        COMPLETE) "$NOTIFY" "🎉 maestro-build: PROGRAMME-STATUS is COMPLETE." ;;
        ABORTED)  "$NOTIFY" "⛔ maestro-build: PROGRAMME-STATUS is ABORTED — needs your review (docs/PROGRESS.md). The watchdog will not attempt to fix or resume an abort." ;;
    esac
    state_set reported_terminal_status "$st"
}

driver_active() {
    systemctl is-active --quiet "$UNIT" 2>/dev/null
}

handle_stall() {
    local retry_not_before
    retry_not_before="$(state_get retry_not_before)"
    if ! past "$retry_not_before"; then
        echo "[watchdog] $(ts) in usage-limit cooldown until $retry_not_before; skipping" >&2
        return 0
    fi

    local last_attempt
    last_attempt="$(state_get last_fix_attempt_at)"
    if [ -n "$last_attempt" ]; then
        local cooldown_until
        cooldown_until="$(python3 - "$last_attempt" "$STALL_COOLDOWN" <<'PYEOF'
import sys
from datetime import datetime, timedelta, timezone
last = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
print((last + timedelta(seconds=int(sys.argv[2]))).strftime("%Y-%m-%dT%H:%M:%SZ"))
PYEOF
)"
        if ! past "$cooldown_until"; then
            echo "[watchdog] $(ts) last fix attempt was recent; in cooldown until $cooldown_until; skipping" >&2
            return 0
        fi
    fi

    state_set last_fix_attempt_at "$(ts)"
    dispatch_fixer
}

classify_and_report() {
    local log="$1" uuid="$2" rc="$3" before_head="$4"
    local out after_head
    out="$(cat "$log" 2>/dev/null || true)"

    if [ "$rc" = "124" ]; then
        "$NOTIFY" "$(printf '⏱️ maestro-build watchdog: fixer session timed out after %ss.\nSession: %s\nResume: claude --resume %s   (cwd: %s)\nFull log: %s' \
            "$FIXER_TIMEOUT" "$uuid" "$uuid" "$REPO" "$log")"
        return
    fi

    after_head="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo '')"
    local no_new_commit=1
    [ -n "$after_head" ] && [ "$after_head" != "$before_head" ] && no_new_commit=0

    if [ "$no_new_commit" -ne 0 ] \
       && echo "$out" | grep -qiE "usage limit|hit (your|the) .*limit|rate.?limit|resets at|spend limit"; then
        local wait_s wake
        wait_s="$(seconds_until_reset)"
        wake="$(date -u -d "+${wait_s} seconds" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)"
        state_set retry_not_before "$wake"
        "$NOTIFY" "$(printf '⏳ maestro-build watchdog: hit a Claude usage limit trying to fix it. Resets at %s. Will retry automatically after that.\nSession: %s\nFull log: %s' \
            "$wake" "$uuid" "$log")"
        return
    fi

    if [ -z "$out" ]; then
        "$NOTIFY" "$(printf '🛑 maestro-build watchdog: fixer session (rc=%s) produced no output.\nSession: %s\nResume: claude --resume %s   (cwd: %s)\nFull log: %s' \
            "$rc" "$uuid" "$uuid" "$REPO" "$log")"
        return
    fi

    "$NOTIFY" "$(printf '🔧 maestro-build watchdog — driver was down, PROGRAMME-STATUS still IN-PROGRESS\nFixer session: %s\n\n%s\n\nResume if needed: claude --resume %s   (cwd: %s)\nFull log: %s' \
        "$uuid" "$out" "$uuid" "$REPO" "$log")"
}

dispatch_fixer() {
    local uuid log rc before_head
    uuid=$(python3 -c 'import uuid; print(uuid.uuid4())')
    log="$LOGDIR/watchdog-fix-$(date -u +%Y%m%dT%H%M%SZ).log"
    before_head="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo '')"

    read -r -d '' brief <<EOF
ultracode

The maestro-build driver (systemd unit $UNIT) is not running while
docs/PROGRESS.md's PROGRAMME-STATUS is still IN-PROGRESS. You are an
unattended fixer session, invoked by scripts/watchdog_selffix.sh — not a
normal chain session; do not try to continue the M2/build work itself.

Diagnose the root cause first (systematic debugging: read
scripts/loop_status.sh's checks, \`journalctl -u $UNIT\`, .run/session-*.log,
\`git log\`, and docs/PROGRESS.md before proposing a fix — do not guess).
Apply the minimal fix. If recovering uncommitted work, verify the full test
suite is green before committing anything; never push.

You are a headless, one-shot process exactly like a chain session: do not end
your turn on unresolved background work (a Workflow call, a run_in_background
command) — wait for it synchronously or don't start it, since nothing will
receive its completion notification after you exit.

You have passwordless sudo for exactly \`systemctl restart $UNIT\` and
\`systemctl status $UNIT\` — nothing else. If the fix requires restarting the
driver, do so and confirm via scripts/loop_status.sh-equivalent checks that
it is actually iterating again before reporting success. For any other
privileged action, follow the standing rule: write a /tmp script and report
the exact command for the operator to run — do not attempt it yourself.

End your final message in exactly this structure:

## What happened
## What I fixed
## Open questions
## Action needed from you
EOF

    set +e
    timeout "$FIXER_TIMEOUT" "$CLAUDE_BIN" -p --model "$FIXER_MODEL" \
        --dangerously-skip-permissions --session-id "$uuid" "$brief" \
        >"$log" 2>&1
    rc=$?
    set -e

    classify_and_report "$log" "$uuid" "$rc" "$before_head"
}

main() {
    acquire_lock
    if [ ! -f "$PROGRESS" ]; then
        echo "[watchdog] $(ts) $PROGRESS not found; nothing to check" >&2
        return 0
    fi

    local st
    st="$(status)"
    case "$st" in
        COMPLETE|ABORTED)
            handle_terminal_status "$st"
            ;;
        IN-PROGRESS)
            if driver_active; then
                echo "[watchdog] $(ts) driver active; nothing to do" >&2
            else
                echo "[watchdog] $(ts) driver inactive while IN-PROGRESS; handling stall" >&2
                handle_stall
            fi
            ;;
        *)
            echo "[watchdog] $(ts) unrecognized PROGRAMME-STATUS '$st'; nothing to do" >&2
            ;;
    esac
}

if [[ "${BASH_SOURCE[0]:-$0}" == "${0}" ]]; then
    main "$@"
fi
