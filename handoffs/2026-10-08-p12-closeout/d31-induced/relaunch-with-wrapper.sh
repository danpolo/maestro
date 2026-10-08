#!/bin/bash
# D31 option (b), Dan 2026-10-08: relaunch the DuetFlow watchdog with the induced-fault
# wrapper dir first on PATH, then clear the HALT. The wrapper is a pass-through until
# the ARMED file exists, so this changes nothing until it is armed.
set -euo pipefail
R=/home/dan/projects/duetflow
W=/home/dan/projects/maestro/handoffs/2026-10-08-p12-closeout/d31-induced/bin
tmux kill-session -t duetflow-watchdog 2>/dev/null || true
sleep 2
tmux new-session -d -s duetflow-watchdog -n main -c "$R" \
  "export PATH='$W':\"\$PATH\"; set -a; [ -f '$R/.env' ] && source '$R/.env'; set +a; MAESTRO_REPO='$R' '$R/.venv/bin/maestro' watchdog"
sleep 3
P=$(pgrep -f "maestro.cli watchdog")
echo "watchdog pid=$P"
tr '\0' '\n' < "/proc/$P/environ" | grep '^PATH=' | cut -c1-140
rm -f "$R/.orchestrator/HALT"
echo "HALT removed; the watchdog relaunches maestro run within ~30s"
