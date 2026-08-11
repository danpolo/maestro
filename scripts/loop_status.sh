#!/usr/bin/env bash
# One-shot progress/health check for the Maestro overnight build loop.
# Usage: scripts/loop_status.sh
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

now="$(date +%s)"
age_min() { echo $(( (now - $(stat -c %Y "$1" 2>/dev/null || echo "$now")) / 60 )); }

echo "== programme status (docs/PROGRESS.md) =="
status="$(grep -m1 -oE '^PROGRAMME-STATUS:[[:space:]]+[A-Z-]+' docs/PROGRESS.md | awk '{print $2}')"
echo "  PROGRAMME-STATUS: ${status:-unknown}"
grep -m1 '^\*\*Current stage:\*\*' docs/PROGRESS.md | sed 's/^/  /'
case "$status" in
    ABORTED)  echo "  -> stopped itself on a safety check; read docs/PROGRESS.md's latest entry for why. Won't restart on its own." ;;
    COMPLETE) echo "  -> programme finished." ;;
esac

echo
echo "== driver =="
echo "  systemd: $(systemctl is-active maestro-build.service 2>&1)"
if tmux has-session -t maestro-build 2>/dev/null; then
    echo "  tmux session: alive"
    echo "  last driver lines (from tmux pane, not the raw session transcript - that can"
    echo "   mention past limit hits in its own prose and read as a false signal here):"
    tmux capture-pane -t maestro-build -p -S -20 2>/dev/null | grep '\[chain\]' | tail -3 | sed 's/^/    /'
else
    echo "  tmux session: gone"
fi

echo
echo "== latest commit =="
git log -1 --format='  %h  %ci  %s'

echo
echo "== is it actually moving? =="
echo "  uncommitted changes: $(git status --porcelain | wc -l | tr -d ' ') file(s)"
echo "  docs/PROGRESS.md last touched: $(age_min docs/PROGRESS.md) min ago"
echo "  (run this again a while later -- if the commit hash and PROGRESS.md timestamp"
echo "   haven't moved, PROGRAMME-STATUS is still IN-PROGRESS, and the last driver line"
echo "   isn't a fresh 'session N starting', something's actually stuck)"
