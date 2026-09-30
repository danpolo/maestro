#!/usr/bin/env bash
set -uo pipefail
cd /home/dan/projects/duetflow || exit 90
export PYTHONPATH=/home/dan/.maestro/versions/5c94c9fead187efc05157272da4eec9b36b19fff
export MAESTRO_REPO=/home/dan/projects/duetflow
export MAESTRO_BOOTSTRAPPED=1
export PYTHONUNBUFFERED=1
/home/dan/projects/duetflow/.venv/bin/python /tmp/drain-duetflow-07-before-update.py > /tmp/duetflow-07-install-drain.log 2>&1
result=$?
printf '%s\n' "$result" > /tmp/duetflow-07-install-drain.exit
