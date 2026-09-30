# Handoff: D31 recovery complete on 07 run 3; run 18 archived

Read `AGENTS.md`, `docs/graph-engineering/STATE.yaml` D31/D30/D4, and the prior
`handoffs/2026-09-28-graph-p12-d31-stale-codex-pause-codex.md`. Query live state first;
this records the 2026-09-29 early-morning IDT archive, not a live controller.

## Goal

Adopt the tested D31/D29/status fixes at this task boundary, then restart DuetFlow
`07-reconciliation` from its preserved code and continue through real proof review,
V-DAN, acceptance or another evidenced archive. D30 closes only after a quota-available
**Claude** agent actually exercises `dontAsk` without classifier calls.

## Verified this session

- A fresh token-free Codex app-server read at 23:13 IDT was 5h **70%**, 7d **63%**.
  The stale durable Codex pause still pointed to 2026-09-29 21:38 IDT. The controller
  was halted with pinned `768f781`, and its TMUX window and launcher lock were confirmed
  gone before any state repair.
- `/tmp/recover-duetflow-07-d31-codex.py` claimed the controller lease, backed up the
  database to `/tmp/duetflow-d31-before-recovery.sqlite3`, checked the exact pause and
  state mirror, then sampled Codex again at 23:28 IDT: 5h **83%**, 7d **65%**. It retired
  only attempt-6's stale `codex-subscription` row and the matching
  `backend_exhausted.codex`, recorded a journal event, and saved
  `/tmp/duetflow-d31-recovery-evidence.json`. The genuine Claude pause stayed intact.
- Restart on the original `768f781` pin claimed real proof-review attempt
  `att_run_jJMHnAdF8XpBC15y_proof_review_07` on Codex `gpt-6-sol`, medium. Its start
  sample was 5h **85%**, 7d **66%**. This proves the live stale pause was cleared, and
  the reviewer actually ran. It returned `changes_requested` with **three blocking**
  findings in the attempt's `review.json`: (1) A pending A Pin retains A attribution
  after B removes and re-adds X before Refresh; (2) removing an uncounted unattributed
  Pin creates false split holes and bypasses turnover; (3) a successful Spotify PUT
  followed by failed local persistence turns DuetFlow's own add/evict changes into
  Pins/Vetoes on retry. Read the full finding rules and proposed tests from the archived
  attempt file; do not paraphrase them into a narrower repair.
- Diagnose attempt 2 was refused `repair_budget_exhausted: 1 of 1 plan repairs already
  spent`; run `run_jJMHnAdF8XpBC15y` settled **failed** at proof_review. V-DAN, merge,
  and accept were skipped. The failed run was not an approval. The old watcher delivered
  its terminal notice and exited.
- Halted the controller again, exported run 2 plus run 3 (20 attempts, 2 runs, 0
  accepted) to `artifacts/graph-engineering/p12-pilots/duetflow-run18-archived.json`,
  SHA-256 `09789b3bc59a974cf6d4e2cb6a14e62296ebdfc4ed54c3a75d1e0f78cb8ae3fc`.
  The archive is `/home/dan/projects/duetflow/.orchestrator/archive/2026-09-29-run18/`
  with the SQLite DB+WAL+SHM, attempts, artifacts, and copies of HALT/state/journal.
  SQLite read-back showed run 2 canceled, run 3 failed, 20 attempts. The task branch is
  `archive/07-reconciliation-run2-3` at `92ac8a343e311a605fe47526df72f4f5c0a7d4b0`.
  HALT remains in the live `.orchestrator`; `state.json.graph_runs` is empty.
- Maestro `da26cd6` adds controller-owned recovery of future durable pauses based on
  newer fresh account telemetry. The integration test was red then green, proves route
  admission after restart with zero quality failure, and preserves a genuine weekly
  exhaustion. Follow-up commit `5c94c9f` closes the crash window between writing a
  recovery marker and clearing the legacy state mirror, and includes the run-18 export.
  Both full suites exited 0 with one existing xfail; final `git diff --check` was clean.

## Next actions, in order

1. Keep the two preexisting untracked Maestro files
   `artifacts/graph-engineering/p12-pilots/duetflow.json` and
   `docs/GRAPH_ENGINEERING_ARTIFACT.md` untouched. STATE, handoffs and lessons are
   intentionally gitignored local project records; the new entries are on disk.
2. At this archived task boundary, adopt Maestro `5c94c9f` into DuetFlow using
   `selfupdate.materialize_worktree` and `selfupdate.adopt`; add a `code_under_test`
   amendment in `artifacts/graph-engineering/p12-pilots/manifest.json`; run pinned
   `maestro doctor`. This also adopts `7783b7d` (D29 recommender) and `8d8ebb2`
   (`/progress`, `/workflow`, `/explain`). Verify those through the live bot once resumed.
   D31 remains OPEN until the adopted controller demonstrates its automatic durable
   reconciliation on a real pause; the live run used the guarded one-time repair.
3. Decide the next 07 run from `archive/07-reconciliation-run2-3` at `92ac8a3`, fixing
   the three exact review findings through the normal graph implementer/gate/review
   path. Do not edit DuetFlow code directly as a substitute for the pilot. Preserve
   existing D29 approved deny-list rulings only if the new control DB workflow identity
   and exact-line keys still make them valid; otherwise let Maestro ask Dan again.
4. Continue to V-DAN only after a real approving proof review. Report Dan's live test,
   merge/graduation, main SHA, and a final export SHA if accepted. If the run parks or
   becomes uncertain, halt, export, then archive before another retry.
5. D30 is still OPEN: Claude's prior attempt hit a real weekly limit before work. Do
   not retry it before capacity returns or infer `dontAsk` from Codex. D4's separate
   no-sentinel-to-uncertain bug remains open. Context Gate remains out of scope.

## Other open evidence

- `/home/dan/projects/duetflow` main has a generated change to
  `docs/dependency_map.md`; leave it until its origin is verified.
- `scope_widened_for_rework` journaled the same two files every 30 seconds while the
  review was quota-paused. Diagnose later as its own small Maestro finding if it recurs.
- The final suite wrote `/tmp/maestro-d31-final-suite.exit` = `0` and its one-shot
  monitor exited; no test or pilot watcher window remains.
- The runtime permission profile changed mid-session to `danger-full-access` with
  approval policy `never`; do not repeat earlier escalation requests.

## Scope and verification

- In: D31 adoption/proof, 07 finding rework and acceptance/archive, D29 live status
  checks, genuine D30 Claude proof, exact export and manifest evidence.
- Out: Context Gate, D4 no-sentinel fix, broad permission redesign.
- Report exact run/attempt IDs, review verdict and findings, quality-failure counts,
  gate checks, V-DAN decision, accepted/archived status, backend/model/effort and dated
  usage percentages, code pin and doctor result, export path and SHA.
- Suggested skills: `systematic-debugging`, `test-driven-development`,
  `verification-before-completion`, `monitor-long-running-tasks`.
