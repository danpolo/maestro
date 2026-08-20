#!/usr/bin/env bash
# M5 — install the AbuAliArchive maestro watchdog systemd unit.
#
# Per the operator's global privileged-commands rule (never run sudo/su/doas/pkexec
# from the agent shell) and docs/plans/2026-08-18-m4c-superseded-sidecars.md §7: this
# script packages the three commands `maestro init` prints but never runs itself. The
# build does not execute this — the operator runs it by hand after the cutover session
# reports M5 done, with `sudo bash scripts/m5-install-unit.sh`.
#
# The cutover itself does NOT need this: the loop can be started by running the
# generated launch.sh directly (as the M5 verification step does). This unit only
# makes the loop survive a reboot, matching how abuali-watchdog.service worked before.
#
# Idempotent: safe to re-run (cp/enable/daemon-reload are all idempotent).

set -euo pipefail

REPO="/home/dan/projects/AbuAliArchive"
UNIT_SRC="${REPO}/systemd/AbuAliArchive-watchdog.service"
UNIT_NAME="AbuAliArchive-watchdog.service"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "this script must be run with sudo: sudo bash $0" >&2
  exit 1
fi

if [[ ! -f "$UNIT_SRC" ]]; then
  echo "refusing to run: $UNIT_SRC not found — has the cutover (M5 step 6) run yet?" >&2
  echo "(the unit's exact filename derives from project_name — check systemd/*.service" >&2
  echo "under $REPO if this exact name has drifted.)" >&2
  exit 1
fi

echo "--- disabling the old, now-superseded abuali-watchdog.service (if present) ---"
systemctl disable --now abuali-watchdog.service 2>/dev/null || true

echo "--- installing $UNIT_NAME ---"
cp "$UNIT_SRC" "/etc/systemd/system/${UNIT_NAME}"
systemctl daemon-reload
systemctl enable --now "$UNIT_NAME"

echo "--- status ---"
systemctl status "$UNIT_NAME" --no-pager || true

echo "done. Audit the running loop with: tmux a -t AbuAliArchive-watchdog"
