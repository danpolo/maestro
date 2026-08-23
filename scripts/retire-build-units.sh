#!/usr/bin/env bash
# Retire the maestro build's own supervision units, now that the programme is COMPLETE.
#
# Run as:  sudo bash scripts/retire-build-units.sh
#
# WHY, and why only one of the two units is actually disabled:
#
#   maestro-watchdog.timer  — DISABLED here.
#     Ticks every 20 minutes forever. On COMPLETE it is a harmless no-op (it sees
#     `reported_terminal_status: COMPLETE` in .run/watchdog_state.json and returns), but
#     its IN-PROGRESS branch is not something to leave armed on a finished programme: if
#     PROGRAMME-STATUS is ever edited back to IN-PROGRESS while the driver is inactive, it
#     spawns an unattended `claude -p --dangerously-skip-permissions` fixer session with a
#     30-minute budget and a brief that tells it to diagnose and commit. On a repo that is
#     now being worked on by hand, that is an autonomous agent racing the operator.
#
#   maestro-build.service  — LEFT ENABLED, deliberately.
#     `Restart=on-failure` and it exited 0, so systemd will not relaunch it. It is still
#     WantedBy=multi-user.target, so it does start on boot — but run_overnight.sh reads
#     PROGRAMME-STATUS as the first statement inside its loop (scripts/run_overnight.sh:93)
#     and breaks before computing a log path or spawning any session. So a boot costs one
#     no-op process and, contrary to an earlier note, overwrites no .run/session-*.log.
#     Leaving it enabled means simply flipping PROGRAMME-STATUS back to IN-PROGRESS is
#     enough to resume the chain, which is worth keeping.
#
# Reversible: `sudo systemctl enable --now maestro-watchdog.timer` puts it back.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "This script must run as root: sudo bash $0" >&2
    exit 1
fi

echo "== before =="
systemctl is-enabled maestro-watchdog.timer || true
systemctl is-active  maestro-watchdog.timer || true

systemctl disable --now maestro-watchdog.timer

# The fixer's passwordless sudo grant exists only for that fixer. With the timer off,
# nothing invokes it, so the grant is removed too rather than left standing.
if [[ -f /etc/sudoers.d/maestro-watchdog ]]; then
    rm -f /etc/sudoers.d/maestro-watchdog
    echo "removed /etc/sudoers.d/maestro-watchdog"
fi

echo
echo "== after =="
systemctl is-enabled maestro-watchdog.timer || echo "maestro-watchdog.timer: disabled"
systemctl is-active  maestro-watchdog.timer || echo "maestro-watchdog.timer: inactive"
echo
echo "maestro-build.service left enabled on purpose (see the header of this script):"
systemctl is-enabled maestro-build.service || true
systemctl is-active  maestro-build.service || true
