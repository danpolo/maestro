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

    # Quota exhaustion is not a failure: wait for the reset and try again.
    if grep -qiE "usage limit|hit your limit|rate.?limit|resets at" "$log"; then
        echo "[chain] $(ts) usage limit detected; sleeping ${LIMIT_SLEEP}s"
        sleep "$LIMIT_SLEEP"
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

# Safety net: never leave the reference project's live loop halted.
HALT="$HOME/projects/AbuAliArchive/.orchestrator/HALT"
if [[ -f "$HALT" ]]; then
    echo "[chain] WARNING: HALT sentinel present at $HALT - removing it"
    rm -f "$HALT"
fi
if ! pgrep -f orchestrator_run.py >/dev/null; then
    echo "[chain] WARNING: reference project's orchestrator is NOT running"
fi
