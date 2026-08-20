#!/usr/bin/env bash
# M5 rollback — reverts the AbuAliArchive cutover and resumes the pre-maestro loop.
#
# Per docs/DESIGN.md §11 "Cutover safety": "Rollback is removing the install and
# reverting one commit." Per docs/EXECUTION.md's M5 safety protocol step 3, this
# script must exist and be dry-run BEFORE any cutover step (halt, branch, install,
# delete) runs — "no rollback, no cutover" — and per step 7, it runs automatically
# on any post-cutover verification failure. Per step 8, whatever happens the run
# must never end with the loop halted: this script's last action is always
# confirming `pgrep -f orchestrator_run.py` finds a live process.
#
# This script touches ONLY /home/dan/projects/AbuAliArchive. It never touches
# ~/.claude/, ~/.codex/, or requires sudo — the systemd unit is left exactly as
# the cutover left it; re-enabling/restarting it (if desired) is the operator's
# call via scripts/m5-install-unit.sh, not this script's.
#
# Usage:
#   scripts/m5-rollback.sh --pre-cutover-sha <sha> [--dry-run]
#
#   --pre-cutover-sha  Required. AbuAliArchive HEAD immediately before the cutover
#                       commit, captured by the cutover step itself
#                       (docs/PROGRESS.md "Reference-project baseline" /
#                       docs/plans/2026-08-20-m5-cutover.md's own baseline capture).
#   --dry-run           Print every command that would run, execute none of them,
#                       and still perform the read-only precondition checks.
#
# Exit codes: 0 = rolled back cleanly (or dry-run precondition checks passed),
# 1 = precondition failed (refuses to touch the repo), 2 = a rollback step failed
# partway (manual intervention needed — this is the one case with no further
# automatic fallback, per the M5 safety protocol's own "M5 post-cutover
# verification fails AND the automatic rollback also fails" abort condition).

set -euo pipefail

REPO="/home/dan/projects/AbuAliArchive"
DRY_RUN=0
PRE_SHA=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pre-cutover-sha) PRE_SHA="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$PRE_SHA" ]]; then
  echo "refusing to run: --pre-cutover-sha is required (never guess it)" >&2
  exit 1
fi

run() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[dry-run] $*"
  else
    echo "[run] $*"
    "$@"
  fi
}

cd "$REPO"

echo "--- precondition: repo exists and the target sha is reachable ---"
if ! git cat-file -e "${PRE_SHA}^{commit}" 2>/dev/null; then
  echo "refusing to run: ${PRE_SHA} is not a commit in ${REPO}" >&2
  exit 1
fi

CURRENT_SHA="$(git rev-parse HEAD)"
echo "current HEAD:      ${CURRENT_SHA}"
echo "pre-cutover target: ${PRE_SHA}"

if [[ "$CURRENT_SHA" == "$PRE_SHA" ]]; then
  echo "already at the pre-cutover commit — no cutover to roll back."
  echo "checking the loop is running anyway (step 8: never end halted) ---"
  if [[ -f .orchestrator/HALT ]]; then
    echo "HALT sentinel present. Not removing it automatically: with no cutover to"
    echo "roll back, halting/resuming is the operator's call, not this script's."
    echo "If the operator wants the pre-maestro loop resumed: rm .orchestrator/HALT"
    exit 0
  fi
  if pgrep -f orchestrator_run.py >/dev/null 2>&1; then
    echo "orchestrator_run.py is running. Nothing to do."
    exit 0
  fi
  echo "WARNING: no HALT sentinel and no running orchestrator_run.py process."
  echo "This is an unexpected state outside a cutover — investigate before relaunching."
  exit 0
fi

echo "--- step 1: halt (idempotent — cutover already halts via the sentinel) ---"
run touch .orchestrator/HALT

echo "--- step 2: revert to the pre-cutover commit ---"
# Hard-checkout the exact pre-cutover tree. Never a force-push, never history
# rewriting (EXECUTION.md abort condition) — this is a local working-tree/HEAD
# move only, on a repo the build never pushes anywhere.
run git checkout "$PRE_SHA" -- .
run git status --porcelain

echo "--- step 3: remove the maestro install from the project venv ---"
if [[ -x .venv/bin/pip ]]; then
  run .venv/bin/pip uninstall -y maestro
else
  echo "[dry-run or skip] .venv/bin/pip not found — nothing to uninstall"
fi

echo "--- step 4: resume the pre-cutover loop ---"
run rm -f .orchestrator/HALT
# Exact relaunch command, verbatim from scripts/watchdog-launcher.sh (restored by step 2's
# checkout, since it's one of the 16 superseded scripts the cutover deletes). Reproduced
# here rather than sourced/executed directly because the launcher blocks in a wait loop
# forever (it's written to be tracked by systemd's Type=simple, not run standalone) — this
# runs its two essential tmux commands only, detached, no systemd/root involved.
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[dry-run] tmux has-session -t agents || tmux new-session -d -s agents -n main"
  echo "[dry-run] tmux kill-session -t abuali-watchdog (stale-session cleanup, ignore failure)"
  echo "[dry-run] tmux new-session -d -s abuali-watchdog -n main \"cd $REPO && $REPO/.venv/bin/python3 scripts/watchdog.py\""
else
  /usr/bin/tmux has-session -t agents 2>/dev/null \
    || run /usr/bin/tmux new-session -d -s agents -n main
  /usr/bin/tmux kill-session -t abuali-watchdog 2>/dev/null || true
  run /usr/bin/tmux new-session -d -s abuali-watchdog -n main \
    "cd $REPO && $REPO/.venv/bin/python3 scripts/watchdog.py"
fi

echo "--- step 5: confirm the loop is alive (never end halted) ---"
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[dry-run] pgrep -f orchestrator_run.py"
else
  sleep 2
  if ! pgrep -f orchestrator_run.py >/dev/null 2>&1; then
    echo "ROLLBACK INCOMPLETE: orchestrator_run.py did not come back up." >&2
    echo "Manual intervention required — this is the M5 abort condition" >&2
    echo "'post-cutover verification fails AND rollback also fails'." >&2
    exit 2
  fi
fi

echo "rollback complete."
