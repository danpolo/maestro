#!/usr/bin/env bash
set -euo pipefail
exec tmux new-window -t agents -n 'codex-maestro-intake-smoke' 'cd /tmp && /home/dan/projects/maestro/.venv/bin/python -u /home/dan/projects/maestro/handoffs/2026-09-30-operator-intake-release/listen-halted.py > /home/dan/projects/maestro/handoffs/2026-09-30-operator-intake-release/listener.log 2>&1'
