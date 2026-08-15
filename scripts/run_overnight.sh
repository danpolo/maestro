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

This is a headless, one-shot `claude -p` process: once you stop generating, the process
exits - there is no later turn in which a background task's completion notification can
reach you. If you start a Workflow or any `run_in_background` command, you MUST poll/wait
for it to finish and act on its result before ending your turn. Do NOT end your turn on
work you expect to "report on later" - the harness gives background tasks a bounded grace
period, then force-terminates them and exits the process, and nothing you started gets
committed. (Observed 2026-08-12: a session fired off a 7-agent Workflow, signed off with
"I'll report when it lands", and was killed 600s later mid-workflow, leaving
docs/PROGRESS.md stale and the chain stalled for ~22h with no restart.) If a stage
genuinely needs a multi-agent Workflow, either wait for it synchronously in this same
turn, or don't start it and leave the stage for the next session in the chain instead.

Before exiting you MUST set the PROGRAMME-STATUS line in docs/PROGRESS.md to exactly one
of: IN-PROGRESS, COMPLETE, ABORTED. The driver reads that line to decide whether to
relaunch you. Setting COMPLETE while stages remain pending silently ends the build.
EOF

mkdir -p "$LOGDIR"
cd "$REPO" || { echo "[chain] cannot cd to $REPO"; exit 1; }

# shellcheck source=scripts/lib_maestro_ops.sh
source "$REPO/scripts/lib_maestro_ops.sh"

determine_model() {
    if [[ -n "${MAESTRO_MODEL:-}" ]]; then
        echo "$MAESTRO_MODEL"
        return
    fi
    python3 - <<'PYEOF'
import re
try:
    progress = open("docs/PROGRESS.md", "r", encoding="utf-8").read()
    m = re.search(r"\*\*Current stage:\*\*\s*(M\d+)", progress)
    stage = m.group(1) if m else ""
    
    # Opus for heavy architecture/refactoring/core extraction/cutover stages
    # Sonnet for standard implementation/testing/setup/doctor/validation stages
    opus_stages = {"M1", "M2", "M5"}
    if stage in opus_stages:
        print("claude-opus-5")
    else:
        print("claude-sonnet-5")
except Exception:
    print("claude-sonnet-5")
PYEOF
}

echo "[chain] $(ts) starting; repo=$REPO max_iter=$MAX_ITER"
stale=0
resume_next=0

for i in $(seq 1 "$MAX_ITER"); do
    st="$(status)"
    if [[ "$st" == "COMPLETE" || "$st" == "ABORTED" ]]; then
        echo "[chain] $(ts) programme $st after $((i - 1)) session(s)"
        break
    fi

    before="$(hash_p)"
    log="$LOGDIR/session-$(printf '%02d' "$i").log"
    model="$(determine_model)"
    session_uuid_file="$LOGDIR/current_session_uuid"

    if [[ "$resume_next" == "1" && -f "$session_uuid_file" ]]; then
        curr_uuid="$(cat "$session_uuid_file")"
        echo "[chain] $(ts) session $i resuming session $curr_uuid (model=$model) -> $log"
        resume_prompt="The usage limit has reset. Please resume working on the current stage in docs/PROGRESS.md. Continue where you left off in the previous session."
        claude -p --model "$model" --dangerously-skip-permissions --resume "$curr_uuid" "$resume_prompt" >"$log" 2>&1
        rc=$?
        echo "[chain] $(ts) session $i (resume) exited rc=$rc"
        if grep -qiE "no conversation found|invalid session|session not found" "$log"; then
            echo "[chain] $(ts) resume failed; launching fresh session"
            curr_uuid=$(python3 -c 'import uuid; print(uuid.uuid4())')
            echo "$curr_uuid" > "$session_uuid_file"
            claude -p --model "$model" --dangerously-skip-permissions --session-id "$curr_uuid" "$PROMPT" >"$log" 2>&1
            rc=$?
        fi
    else
        curr_uuid=$(python3 -c 'import uuid; print(uuid.uuid4())')
        echo "$curr_uuid" > "$session_uuid_file"
        echo "[chain] $(ts) session $i starting fresh session $curr_uuid (model=$model) -> $log"
        claude -p --model "$model" --dangerously-skip-permissions --session-id "$curr_uuid" "$PROMPT" >"$log" 2>&1
        rc=$?
        echo "[chain] $(ts) session $i exited rc=$rc"
    fi

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
        resume_next=1
        sleep "$wait_s"
        continue
    fi
    resume_next=0

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
