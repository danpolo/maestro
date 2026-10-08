#!/bin/bash
# D31 cleanup (2026-10-08): put the DuetFlow watchdog back on its normal launch (launch.sh's
# command, no induced-fault wrapper on PATH). Run only while no graph run is active:
# HALT -> wait for the controller to exit -> relaunch the watchdog -> clear HALT.
set -euo pipefail
R=/home/dan/projects/duetflow
D=/home/dan/projects/maestro/handoffs/2026-10-08-p12-closeout/d31-induced
rm -f "$D/ARMED"
"$R/.venv/bin/maestro" ctl --repo "$R" halt
for _ in $(seq 1 60); do pgrep -f "maestro.cli run$" >/dev/null || break; sleep 2; done
pgrep -f "maestro.cli run$" >/dev/null && { echo "controller still running; HALT left in place"; exit 1; }
tmux kill-session -t duetflow-watchdog 2>/dev/null || true
sleep 2
tmux new-session -d -s duetflow-watchdog -n main -c "$R" \
  "set -a; [ -f '$R/.env' ] && source '$R/.env'; set +a; MAESTRO_REPO='$R' '$R/.venv/bin/maestro' watchdog"
sleep 3
P=$(pgrep -f "maestro.cli watchdog")
echo "watchdog pid=$P"
tr '\0' '\n' < "/proc/$P/environ" | grep '^PATH=' | cut -c1-120
rm -f "$R/.orchestrator/HALT"
echo "HALT removed; the watchdog relaunches maestro run within ~30-90s"
