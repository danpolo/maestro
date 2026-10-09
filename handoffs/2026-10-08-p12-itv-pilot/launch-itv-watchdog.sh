#!/usr/bin/env bash
# Launch the instagram-to-value maestro watchdog in tmux session `itv-watchdog`
# (mirrors /home/dan/projects/duetflow/launch.sh; kept apart from itv-bot / itv-worker).
set -euo pipefail
REPO_PATH="/home/dan/projects/instagram-to-value"
SESSION="itv-watchdog"
tmux kill-session -t "$SESSION" 2>/dev/null || true
tmux new-session -d -s "$SESSION" -n main -c "$REPO_PATH" \
    "set -a; [ -f '$REPO_PATH/.env' ] && source '$REPO_PATH/.env'; set +a; MAESTRO_REPO='$REPO_PATH' '$REPO_PATH/.venv/bin/maestro' watchdog"
sleep 3
tmux list-sessions | grep -E '^itv-' || true
echo "launched; attach with: tmux a -t $SESSION"
