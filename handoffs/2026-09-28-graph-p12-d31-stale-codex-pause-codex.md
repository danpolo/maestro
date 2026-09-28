# Handoff: D31, stale Codex pause blocking DuetFlow 07 proof review

Read `AGENTS.md`, `docs/graph-engineering/STATE.yaml` D31/D30/D4, and
`handoffs/2026-09-28-graph-p12-duetflow-07-run3-progress-codex.md`. Query the live
controller first; this is a 2026-09-28 23:02 IDT snapshot.

## Goal

Fix D31 from the root: a persisted pool pause must not keep a measured-available
backend blocked. Recover the live DuetFlow 07 proof review safely, then continue the
pilot through review, V-DAN, and acceptance/archive. Do not close D30 until a real
quota-available Claude agent proves `dontAsk` without classifier calls.

## Verified diagnosis and work

- Dan pointed out that Codex is available. A **fresh, token-free** Codex app-server
  account read at 22:47 IDT measured **5h 39%, 7d 58%**. The sandboxed probe returned
  no answer; rerunning that read with `require_escalated` succeeded. The same account's
  prior DuetFlow review-6 start sample was 5h 100%, 7d 52%; settle sample was 5h 0%,
  7d 52%, with `reset_crossing=1` in `control.sqlite3`.
- Review-6 CLI began just before 21:38 and failed at 21:38:46 IDT, repeating
  `try again at 9:38 PM`. `maestro/backends/codex.py::_try_again_epoch` treats an
  undated time already past as tomorrow. The worker returned
  `reset_at=2026-09-29T18:38:00Z`; the controller recorded both a
  `codex-subscription` `pool_pauses` row and `state.json.backend_exhausted.codex`
  through `WorkflowRunner._requeue_blocked` and `_graph_on_blocked`. The graph
  repeatedly rehydrates both records, so `proof_review` stays `ready` with
  `admission_waiting.reason=route_pool_paused`. This is why an available Codex is
  waiting. Pool pause 2026-09-29 21:38 IDT is stale; Claude's separate pause until
  2026-09-29 16:00 IDT is genuine according to its 7d 100% sample.
- A parser-only two-minute grace was written and then **removed** after Dan clarified
  that pilot defects need root fixes. It is not in the current code.
- Commit `ac72b9a` fixes the attempt-settle trust boundary: record the settle sample
  before choosing a pause; if a fresh account sample proves a window crossed and all
  measured windows are below 100%, choose a short unknown-reset backoff rather than
  the CLI's stale future reset. Integration test
  `test_a_reset_crossing_overrides_a_stale_cli_reset_time` failed before and passed
  after; targeted failure-ladder/driver/failures tests passed; the full
  `.venv/bin/python -m pytest -q` suite exited 0, with the one existing xfail.
  `git diff --check` passed. This prevents the observed case at ingest, but **does not
  yet reconcile a future pause already persisted**. D31 remains OPEN.
- Prior commit `8d8ebb2` made graph `/progress`, `/workflow`, `/explain` concise and
  usage-aware, but it also is not adopted live. Prior `7783b7d` fixed the D29 quota
  reader but is not adopted live. DuetFlow remains pinned to `768f781`.

## Live state and safe recovery constraints

- Run `run_jJMHnAdF8XpBC15y` was still `running`; `proof_review` was `ready` at
  attempt index 6. The last attempt
  `att_run_jJMHnAdF8XpBC15y_proof_review_06` failed `quota_exhausted` with no real
  review. Do not treat gate success as review approval. `agents:orchestrator` and
  `agents:codex-duetflow-07-run3-watch` were present. The monitor script is
  `/tmp/watch-duetflow-07-run3.py`; it sends Dan a notice on terminal, uncertain,
  HALT, V-DAN or D26 question.
- The controller process tree at 22:50 IDT was a `flock -n .../orchestrator.lock
  /home/dan/projects/duetflow/.venv/bin/maestro run` pane (PID 3055543) with a
  child `python -m maestro.cli run` (PID 3055545). `maestro-watchdog.service` was
  inactive, but check other watchdog ownership before recovery. **Never edit the
  control DB or state snapshot while this controller holds the single-writer lease.**
  `read_state()` from a second process tries to claim that lease; use read-only SQLite
  and `state.json` for diagnosis.
- Next root step: design and test a controller-owned way to revalidate durable future
  `pool_pauses` and `backend_exhausted` against newer fresh account usage, and clear
  only a demonstrably recovered pool. Both durable sources **and** the in-memory pool
  pause must be reconciled. Include a real controller integration test showing the
  waiting node gets another route without spending a quality attempt. Preserve
  genuine weekly exhaustion and unmeasurable usage as waits.
- Then recover the already-live pause at a safe halt/lease-free boundary. The repository
  cannot write `/home/dan/projects/duetflow` under the default sandbox; use a narrow
  escalated operation or one executable `/tmp/` repair script per AGENTS.md. Verify
  the fresh Codex sample again before clearing anything. Avoid adopting a new Maestro
  pin mid-task; the self-update contract says adoption is for task boundaries. After
  recovery, verify a real proof-review attempt and the live bot. Adopt `ac72b9a`
  plus earlier unadopted commits at a safe task boundary, amend
  `artifacts/graph-engineering/p12-pilots/manifest.json`, and run doctor.

## Scope

- In: D31 durable reconciliation, safe live recovery, 07 pilot review/acceptance,
  D30 Claude proof if capacity returns, status-command adoption at a task boundary.
- Out: Context Gate (AGENTS.md); D4 no-sentinel-to-uncertain fix; broad permission
  redesigns. Leave two preexisting untracked Maestro files
  `artifacts/graph-engineering/p12-pilots/duetflow.json` and
  `docs/GRAPH_ENGINEERING_ARTIFACT.md` untouched unless their origin is established.

## Verification and next actions

1. Read current run/node/attempt/pause rows and `state.json.backend_exhausted`; take
   another fresh Codex account sample with `CodexBackend().usage_from_app_server()`
   using escalation if the sandboxed local probe is silent. Report sample time and
   both percentages.
2. Test the durable pause recovery path red then green, including a restarted
   controller and a genuinely exhausted pool. Run the full suite using
   `monitor-long-running-tasks` (Dan's standing instruction from this turn) and
   `git diff --check`.
3. Safely correct the live stale Codex records while the controller is lease-free,
   preserving journal/evidence, and verify proof-review attempt 7 or later actually
   starts. If an agent fails again, inspect its fresh exit and usage rather than
   overriding a new genuine limit.
4. Continue the pilot per the previous handoff; report review verdict/findings,
   V-DAN result, accepted or archived status, final export path/SHA, and genuine D30
   Claude evidence. Keep D31 open until future and already-written pauses are both
   proven to recover.

Suggested skills: `systematic-debugging`, `test-driven-development`,
`verification-before-completion`, `monitor-long-running-tasks`. Dan's correction is
recorded in `tasks/lessons.md`: a pilot finding needs a root fix, not a timing patch.
