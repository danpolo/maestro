#!/usr/bin/env bash
# Event stream for the resumed DuetFlow pilot: journal events (minus idle noise) + attempt transitions.
O=/home/dan/projects/duetflow/.orchestrator
tail -n0 -F "$O/journal.ndjson" 2>/dev/null | grep --line-buffered -v -E '"event": "(launch_skipped_write_overlap|idle_gated|paused_poll_start|launch_waiting_admission)"' | cut --line-buffered -c1-300 &
q(){ sqlite3 -readonly "$O/control.sqlite3" "select 'attempt '||attempt_id||' '||node_id||' '||status||' '||coalesce(backend_id,'')||':'||coalesce(model_id,'') from attempts where run_id in (select run_id from task_runs where started_at>='2026-10-07T23:52') order by attempt_id; select 'run '||run_id||' '||task_id||' '||status from task_runs where started_at>='2026-10-07T23:52';" 2>/dev/null | sort; }
prev=$(q)
while true; do
  cur=$(q)
  [ -n "$cur" ] && comm -13 <(echo "$prev") <(echo "$cur")
  prev=$cur
  [ -f "$O/HALT" ] && [ ! -f /tmp/.pilot-halt-seen ] && echo "HALT present: $(cat "$O/HALT")" && touch /tmp/.pilot-halt-seen
  pgrep -f "maestro.cli run" >/dev/null || { echo "NO maestro run process"; sleep 60; }
  sleep 20
done
