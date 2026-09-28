# Handoff: finish DuetFlow 07 and make `/progress` truthful for graph runs

Read `AGENTS.md`, `docs/graph-engineering/STATE.yaml` entries D30/D29/D4, and the prior
`handoffs/2026-09-28-graph-p12-duetflow-07-run2-codex.md` first. This is a snapshot;
query the live controller before acting.

## Goal

1. Finish the 07-reconciliation pilot through gate, proof review, V-DAN, acceptance or
   archive. Close D30 only after a quota-available **Claude** agent proves `dontAsk`
   without classifier calls. Leave D4's no-sentinel-to-uncertain bug open.
2. Make the Telegram `/progress` response for graph runs show the actual node/cycle,
   elapsed time, backend/model/effort, measured usage percentage (or unavailable), and
   an admission wait with its reason/reset. Dan requested this view; the current reply
   said `between steps` while the run was quota-paused.

## Verified work this session

- Dan approved both run-2 D29 messages 102/103. The controller recorded `Approve` for
  keys `670a3bbb0d7e9d80` and `f15097bd6b5f0d52`; gate attempt 4 succeeded.
- Diagnosed why all three run-2 D29 messages lacked recommendations. The gate worker
  called `agentcall.resolve_call()` -> `quota.exhausted_backend_resets()` ->
  `state.read_state()`. In a child process `read_state()` tries to claim the already-held
  controller writer lease and fails. `quota` silently returned no exhausted backends,
  so the recommender selected paused Claude. The previous 06 D29 message had a valid
  recommendation before Claude was paused. A live pre-fix probe returned `{}` and
  `('claude', 'claude-opus-5-5')`; a 60s Claude call timed out. The raw run-2 recommender
  reply was not retained, so this is the verified routing defect, not a claim about the
  exact CLI text for those historical calls.
- Fixed in Maestro commit `7783b7d`: quota readers use the exported `state.json`
  read-only instead of claiming the controller lease. The new regression test failed
  before the fix and passed after. A live read-only check with DuetFlow state returned
  `{'claude': '2026-09-28T17:40:00Z'}` and routed diagnoser to
  `('codex', 'gpt-6-sol')`. Targeted suite passed; full suite needed escalation for
  TMUX/local-socket tests, then **5215 passed, 1 xfailed**. `git diff --check` clean.
  This commit has NOT been adopted into DuetFlow because the pilot was active; its pin
  remains `768f781`. Adopt at a safe halted boundary, amend manifest, run doctor, and
  verify a real future recommendation before declaring the D29 fix live.
- Run 2 `run_VzB5dBsherYnBhnh` used three implementer attempts, three rejected proof
  reviews and one successful diagnose/plan repair, then became `canceled` as expected
  for the successor. Its old monitor wrote `/tmp/duetflow-07-run2-terminal.json` and
  exited. Do not mistake canceled for a failed pilot.
- Plan-repair successor run 3 `run_jJMHnAdF8XpBC15y` opened 20:10:09 IDT on revision
  2. Implementer attempt 4 (Codex gpt-6-luna, medium) succeeded; gate attempt 6
  succeeded. Proof-review attempt 4 hit Codex quota, attempt 5 reached Claude
  opus-5-5 but failed immediately on its weekly limit with zero real tokens (no D30
  proof), attempt 6 hit Codex quota. The node is `ready`, run `running`, with
  `admission_waiting` reason `route_pool_paused`. Authoritative pool pauses in
  `control.sqlite3`: Claude until **2026-09-29 16:00 IDT** (`2026-09-29T13:00:00Z`);
  Codex until **2026-09-29 21:38 IDT** (`2026-09-29T18:38:00Z`). Do not retry a quota
  stop until capacity actually returns. The controller is still in `agents:orchestrator`.
- A one-shot successor monitor is running in `agents:codex-duetflow-07-run3-watch` from
  `/tmp/watch-duetflow-07-run3.py`. It writes
  `/tmp/duetflow-07-run3-terminal.json` and sends Dan a text-only Telegram message on
  terminal state, uncertain, HALT, V-DAN or D26 question. It was verified alive after
  startup; it has not delivered yet. Stop/check it after delivery.

## `/progress` finding and next implementation

- `maestro/hitl/commands.py::_build_progress_report` lines ~209-232 renders graph
  runs using only `state.json["graph_runs"]` and their active node list. If no node is
  running, it says `between steps`; it does not consult `admission_waiting`, attempts,
  routing or usage. That is exactly the user-visible reply.
- `/workflow <id>` exists (`maestro/status.py::workflow_view` and
  `render_workflow_text`): it shows graph nodes, phases, attempt IDs and checks, but not
  backend/model/effort/usage in its text rendering. `/explain <id>` shows recorded
  model/effort in evidence chains, but is not the requested live summary. No other
  Telegram command currently gives the requested view.
- The control DB `attempts` rows already have `started_at`, `backend_id`, `model_id`,
  `effort`, `usage_start_json`, `usage_settle_json`, `wall_time_sec`, `failure_kind`;
  node states have `phase`, `attempt_index`, `updated_at`. `state.json` carries
  `admission_waiting[task_id]` with `reason`, `detail`, `since`, `retry_after_sec`.
  Usage windows are `300` (5h) and `10080` (7d), with `used_pct` and `resets_at`;
  live/settle snapshots must be labeled by time/source, not presented as current if
  stale. The latest 07 Codex review usage start was 5h 100%, 7d 52%; Claude review
  start was 7d 100%.
- Add a bounded graph progress reader/renderer using a read-only `ControlStore` client
  or `status.workflow_view` and the already-read projection. Preserve the legacy
  `in_flight` branch. Say `waiting: route_pool_paused` with the reset in local time
  rather than `between steps` for this state; show last attempt separately when no
  attempt is active. Do not imply model quota is live when only a start/settle sample
  exists. Prefer one concise Telegram message for one task.
- Use TDD: a real control-store fixture should prove a running implementer cycle with
  model/effort/elapsed/usage and a ready proof review waiting on quota. Keep a no-usage
  case explicit. Existing graph parity test is
  `tests/graph_engineering/test_legacy_parity.py::test_status_and_progress_and_the_docs_count_a_live_graph_run`;
  `tests/test_workflow_status.py` has read-only projection fixtures. Verify full suite.
- After verification, commit, adopt only when DuetFlow is safely halted, amend the
  qualification manifest, run doctor, then test `/progress` through the live bot.

## Open pilot details

- Inspect run-2 review attempts 2/3 and run-3 proof review 4 to determine the remaining
  blocking finding and whether the plan repair addressed it. Do not infer acceptance
  from the branch or DONE. Run-3 proof review remains unapproved.
- The journal repeatedly emits `scope_widened_for_rework` for the same two files while
  review is waiting on capacity, roughly every 30s. Diagnose this as a separate small
  follow-up if it continues; it is producing journal noise. Do not bundle its fix into
  `/progress` without a specific cause and test.
- On acceptance: verify run status, merge/graduation, journal, DuetFlow main status and
  export final pilot evidence. If the run parks or becomes `uncertain`, halt, export
  first, archive by the established run protocol, then diagnose before retrying.
- D30 remains OPEN. The run-3 Claude proof review failed on weekly quota before real
  work, so it proves no `dontAsk` behavior. Only after a genuine live Claude attempt
  may D30 close and D4's *classifier-outage retry proposal* be marked superseded.
  D4's no-sentinel-to-uncertain bug remains open. Context Gate is out of scope.

## Scope and workspace cautions

- In scope: `/progress` truthfulness, continuing 07, evidence/export, adoption of
  `7783b7d` at a safe halt, narrow fixes found by the run, D30/D4 state updates as
  allowed above.
- Out of scope: Context Gate; broad Codex/Antigravity permission changes; D4's
  no-sentinel fix.
- Maestro main branch is `feat/graph-engineering-foundation` at `7783b7d`. Preexisting
  untracked `artifacts/graph-engineering/p12-pilots/duetflow.json` and
  `docs/GRAPH_ENGINEERING_ARTIFACT.md` were left untouched. DuetFlow main has generated
  modifications in `docs/UPCOMING.md`, `docs/dependency_map.md`, and its PNG; these
  were also left untouched.
- Use Israel local time in user updates; no privileged commands directly. The
  `set_session_progress` tool was unavailable this session.

## Verification and suggested skills

Report `/progress` live output, exact run/attempt IDs, backend/model/effort and usage
sample freshness, review verdicts/findings, gate checks, V-DAN outcome, accepted or
parked result, export path and SHA, and D30 Claude evidence. Use `systematic-debugging`,
`test-driven-development`, `verification-before-completion` for code; use
`monitor-long-running-tasks` for the live pilot; use `close-session` when finishing.
