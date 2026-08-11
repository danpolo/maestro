#!/usr/bin/env bash
# Polled by cron (see setup below) while the Maestro build is running.
#
# 2026-08-11: AbuAliArchive's nightly eval timer (abuali-nightly-eval.timer, a
# user-level unit unrelated to the halted orchestrator/watchdog loop) was paused
# with `systemctl --user disable --now`, because its nightly one-line append to
# eval/history.jsonl was tripping the build's reference-project baseline check
# and aborting the programme. See docs/PROGRESS.md's 2026-08-11 entry.
#
# This script re-enables that timer once docs/PROGRESS.md's PROGRAMME-STATUS
# reaches COMPLETE, then removes its own cron entry so it stops polling.
#
# Deliberately does NOT trigger on ABORTED: an abort (like the one that caused
# this pause in the first place) is a request for the operator to decide
# something, commonly followed by flipping PROGRAMME-STATUS back to
# IN-PROGRESS and relaunching - not a final stop. Auto-resuming the eval timer
# on ABORTED would race that decision (caught this in testing: the very abort
# that led to this pause counts as ABORTED, which would have undone the pause
# before the operator ever acted on it).
set -uo pipefail

# cron's environment is minimal and does not include the session bus location
# `systemctl --user` needs, even with lingering enabled for this user.
export XDG_RUNTIME_DIR="/run/user/$(id -u)"

REPO="/home/dan/projects/maestro"
PROGRESS="$REPO/docs/PROGRESS.md"
CRON_MARKER="# maestro-resume-eval"

status="$(grep -m1 -oE '^PROGRAMME-STATUS:[[:space:]]+[A-Z-]+' "$PROGRESS" 2>/dev/null | awk '{print $2}')"

if [[ "$status" == "COMPLETE" ]]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) build reached $status; resuming abuali-nightly-eval.timer"
    /usr/bin/systemctl --user enable --now abuali-nightly-eval.timer
    /usr/bin/systemctl --user is-active abuali-nightly-eval.timer

    # Self-remove: this check is only needed while the build is actively running.
    /usr/bin/crontab -l 2>/dev/null | grep -vF "$CRON_MARKER" | /usr/bin/crontab -
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) removed own cron entry"
else
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) build still $status; eval timer stays paused"
fi
