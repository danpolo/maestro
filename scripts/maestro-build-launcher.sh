#!/usr/bin/env bash
# systemd wrapper for scripts/run_overnight.sh.
#
# Runs as Type=simple so systemd tracks THIS process (not the tmux child) — same
# pattern as AbuAliArchive's scripts/watchdog-launcher.sh. Creates a dedicated,
# named tmux session so Dan can audit via: tmux a -t maestro-build
# Blocks in a wait loop; exits when the session ends, so systemd (Restart=on-failure)
# only relaunches on an actual crash, not on a clean COMPLETE/ABORTED/stale-guard stop.
#
# Reboot-proofing: this script is invoked by maestro-build.service, which is
# enabled at the multi-user.target — a reboot wipes the tmux server, but systemd
# starts this unit again on boot with no login required, which recreates the
# session and resumes the chain from docs/PROGRESS.md.
set -e
REPO="$(cd "$(dirname "$0")/.." && pwd)"

# Kill any stale session from a previous run (manual relaunch, prior boot, etc.).
/usr/bin/tmux kill-session -t maestro-build 2>/dev/null || true

# Launch the overnight chain driver in a named, detached session.
/usr/bin/tmux new-session -d -s maestro-build -n main \
    "cd $REPO && bash scripts/run_overnight.sh"

# Block here; systemd tracks this shell as the "service process".
# When run_overnight.sh exits (COMPLETE, ABORTED, stale-guard, or crash), the
# session disappears and we exit too.
while /usr/bin/tmux has-session -t maestro-build 2>/dev/null; do
    sleep 10
done
