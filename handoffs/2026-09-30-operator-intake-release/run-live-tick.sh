#!/usr/bin/env bash
set -u
cd /tmp || exit 99
support=/home/dan/projects/maestro/handoffs/2026-09-30-operator-intake-release
export MAESTRO_BOOTSTRAPPED=1
export MAESTRO_TELEGRAM_LISTENER=0
export MAESTRO_REPO=/home/dan/projects/duetflow
export PYTHONPATH=/home/dan/.maestro/versions/f46ffd403785bcd9040e892961731c2009fc6220
/home/dan/projects/maestro/.venv/bin/python -m maestro.cli intake tick --repo /home/dan/projects/duetflow --telegram > "$support/live-tick.log" 2>&1
result=$?
printf '%s\n' "$result" > "$support/live-tick.exit"
exit "$result"
