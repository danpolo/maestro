#!/usr/bin/env bash
cd /tmp || exit 99
/home/dan/projects/maestro/.venv/bin/python /home/dan/projects/maestro/handoffs/2026-10-08-modelctl-release/selftest-candidate.py > /home/dan/projects/maestro/handoffs/2026-10-08-modelctl-release/candidate-selftest.log 2>&1
result=$?
printf '%s\n' "$result" > /home/dan/projects/maestro/handoffs/2026-10-08-modelctl-release/candidate-selftest.exit
exit "$result"
