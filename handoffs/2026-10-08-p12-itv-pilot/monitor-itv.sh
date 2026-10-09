#!/usr/bin/env bash
# Event stream for the itv pilot: journal events (minus idle noise) + attempt/run transitions. No pgrep.
O=/home/dan/projects/instagram-to-value/.orchestrator
tail -n0 -F "$O/journal.ndjson" 2>/dev/null | grep --line-buffered -v -E '"event": "(launch_skipped_write_overlap|idle_gated|paused_poll_start|launch_waiting_admission)"' | cut --line-buffered -c1-300 &
q(){ sqlite3 -readonly "$O/control.sqlite3" "select 'attempt '||attempt_id||' '||node_id||' '||status||' '||coalesce(backend_id,'')||':'||coalesce(model_id,'') from attempts; select 'run '||run_id||' '||task_id||' '||status from task_runs;" 2>/dev/null | sort; }
prev=$(q)
while true; do
  cur=$(q)
  [ -n "$cur" ] && comm -13 <(echo "$prev") <(echo "$cur")
  prev=$cur
  [ -f "$O/HALT" ] && [ ! -f "/home/dan/projects/maestro/handoffs/2026-10-08-p12-itv-pilot/.halt-seen" ] && echo "HALT present: $(cat "$O/HALT")" && touch "/home/dan/projects/maestro/handoffs/2026-10-08-p12-itv-pilot/.halt-seen"
  sleep 20
done
