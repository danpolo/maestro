#!/usr/bin/env bash
set -euo pipefail
project=/home/dan/projects/duetflow
printf '%s\n' 'Preparing task-boundary installation drain' > "$project/.orchestrator/HALT"
for attempt in {1..15}; do
  if flock -n "$project/.orchestrator/orchestrator.lock" true; then
    exec bash /tmp/run-duetflow-07-install-drain.sh
  fi
  sleep 5
done
printf '%s\n' 'Original controller did not release its lock; HALT retained' > /tmp/duetflow-07-install-drain.log
exit 1
