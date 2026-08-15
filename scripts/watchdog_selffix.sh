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

dispatch_fixer() {
    echo "[watchdog] $(ts) TODO(Task 5): spawn the fixer session here" >&2
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
