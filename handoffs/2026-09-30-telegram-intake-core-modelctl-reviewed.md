# Telegram intake core verified; modelctl Maestro comparison complete

Read AGENTS.md, this handoff, docs/OPERATOR_INTAKE.md and
docs/plans/2026-09-30-operator-intake-and-modelctl-review.md first.
Original installed/pilot boundary:
handoffs/2026-09-30-graph-ask-installed-next-live-smoke.md.
Model comparison: docs/reviews/2026-09-30-modelctl-maestro-coverage.md.

## Objective and user decisions

Finish the **Maestro** Telegram bug/change/feature intake feature: report -> durable
receipt -> scoped proposal -> Dan approves the exact revision -> normal ROADMAP task
at a safe boundary -> normal implementation/checks/review/verification/acceptance.
Dan explicitly selected Telegram intake in the native structured question. Proceed
with implementation; do not reopen agent-assisted versus Telegram choice or introduce
another staged design approval. Ask only genuinely new material scope/cost/external
effect choices, together, after preparing the concrete work.

Dan corrected the scope: “you shouldnt resolve duetflow issues, you should just
establish the mechanism that allows maestro to deal with such bugs because duetflow
is a maestro pilot and you main goal is to follow the pilot and improve maestro along
the way”. Do not diagnose/fix DuetFlow's product behavior in this session continuation.
The song report is only intake evidence and a disposable verification example.

Modelctl comparison is done. Dan clarified its focus is Maestro requirements,
especially changing the current model across projects, and asked where the missing
parts belong. Recommendation already given: discovery/registration/context tables
stay in external modelctl; selection of effective Maestro defaults, routing,
project inventory/override provenance and safe propagation belong to a small
**Maestro-owned integration layer**, not a third model manager. Main shared-default
switch/propagation requirement is missing. Read the comparison, do not redo its
reconnaissance. Building that integration has not been requested/approved this turn.

## Verified work

- New **maestro/intake.py**: per-report atomic JSON mailbox, file locking, report
  capture, list/detail, approve/reject/amend/retry commands; raw reports remain intact;
  bounded fresh judge completion and task proposal validation; material questions
  block approval; old revisions cannot approve amended scope. Queue only with a
  controller store and no running/paused runs or pending/claimed/running attempts.
  Use taskgraph.apply + sync_to_store for a new task only; no other task re-revision.
  Partial ROADMAP publication gets a durable publishing marker and idempotent retry.
- **maestro/hitl/commands.py** routes /report, /reports, /intake in normal Telegram
  polling and in the halted watchdog. Capture performs no model call or worker launch.
- **tests/test_operator_intake.py**: 16 cases, meaningful red -> green observed.
  First 3 capture tests failed because reports were discarded; after capture passed,
  13 preparation/approval cases failed due missing process() before implementation.
- Latest focused command:
  `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -o addopts='' -q -p no:cacheprovider tests/test_operator_intake.py tests/graph_engineering/test_telegram_halted.py tests/characterization/test_commands.py`
  → **175 passed**, **2.37 s**, **exit 0**.
  Log: /tmp/maestro-operator-intake-focused.log; archived under this handoff's support dir.
  git diff --check also exit 0. Full suite **not run for the new intake feature**.
- Modelctl fresh tests: **130 passed**, 3.96 s, exit 0 from its own cwd. Both public
  add flows in disposable HOMEs verified real limits/hook/governor/cache behavior;
  provider network/inference/daemon effects simulated. Fresh real discovery metadata
  succeeded for Codex and Claude; no global scan, paid inference or notifications.
- Secondary modelctl defects reproduced: state-save exception after writes escapes
  rollback; first scan can call configured rows VERIFIED with no access check.
  These are secondary to missing Maestro shared-default switching/propagation.
- Read-only host timer check: modelctl-scan.timer **enabled/active**, next
  2026-10-01 05:46:50 IDT. Old modelctl handoff's “install timer” instruction is stale;
  don't send Dan to reinstall it. No edits to modelctl were made.

## Unfinished feature — next work

This is a verified core slice, **not a working installed bot feature**. No code is
committed, materialized or adopted for intake yet.

1. Review the new intake module and extend meaningful tests for uncovered failure
   paths. Particularly: drift/concurrent ROADMAP edits, malformed Dan steps, scope
   constraints, partial state/report persistence, duplicate IDs, failure-to-deliver
   proposal/retry behavior, redelivery after failures and corrupt report records.
   Ensure publishing collision/recovery never adopts someone else's task.
2. Wire intake.process into the existing graph/legacy controller at the control
   polling/idle boundary, without a second dispatcher or changing active specifications.
   There is currently **no caller** of intake.process in production.
3. Provide a bounded operator CLI intake tick to prepare a proposal while HALT stays
   set and without a controller loop/worker. Acquire the normal controller lock only
   when an approved task can safely be published; preparation can use None store.
   Do not make the halted watchdog automatically start paid calls merely by receiving
   a report. Choose and document the processor behavior consistently with user intent.
   Ensure paused controller still handles reports/approvals without resuming tasks.
4. Advertise commands in HELP_TEXT and complete end-to-end tests through the real
   command/processor path. Sample IDs in documentation are explanatory; the operator
   must receive actual IDs/revisions in receipts/proposals.
5. Surface queued/running/accepted/graduated task progress for an intake report.
   Current status remains queued after publication; do not call a report resolved
   merely because it was queued. Keep reviewer follow-ups in the existing mechanism.
6. Run focused tests and the full repository suite, review the final diff, and keep
   counts/exit codes/logs. Long secondary work must run in a named agents TMUX window.
   Preserve the five preexisting model-owner edits listed below.
7. Prepare safe rollout using the existing version materialization/adoption path.
   Do not change DuetFlow's pin or start watchers/controllers before satisfying its
   installed-boundary rules and any material external-effect decision. Keep HALT.
   A real bot test must use the actual bot identity and complete copy-ready command,
   expected receipt/proposal/approval behavior and no-reply instructions. Give Dan
   those details only after the bot is ready. No Telegram action is needed now.

The first core is about 370 lines. Stay inside the approved intake scope; no new
project-wide ticket tracker, Context Gate, model-switch Telegram UI or product repair.
Existing permission limits apply: writable=False on an unsandboxed completion is
advisory, as already documented by the /ask fix. Do not promise stronger confinement.

## Captured pilot example and protected boundary

Report: docs/intake/2026-09-30-duetflow-unavailable-song-versions.md.
Dan supplied:
- Playlist-unplayable URL: https://open.spotify.com/track/13HD3tYTiz8oSRxWzXEq7g?si=7fda636b1c1a4ab9
- Search/artist-playable URL: https://open.spotify.com/track/13HD3tYTiz8oSRxWzXEq7g?si=a6904a7cb23a4e70

Both have the **same track ID**. Dan sees all Ella Langley songs greyed out and
unclickable in Duet, but they play from search/artist page. Other listener's behavior
unknown. No API/playback diagnosis or substitution policy is established. A speculative
held repair YAML and one-time queue script were created before his scope correction,
then removed without running. No live report or bug task was queued.

Latest read-only verification: DuetFlow HALT true, no in_flight, zero running/paused
runs, 07-reconciliation accepted, pin ead3819ce9110debec25f12dc1fee66174b1367b,
zero live intake files. git status only its preexisting generated dependency_map.md.
Do not clear HALT or continue tasks until Dan agrees. Preserve 07 history follow-ups,
D30/D31/D33 and all installed-boundary rules from the original handoff. Actual /ask
SUPPORT delivery is still open and requires a genuine authorized waiting request;
do not manufacture one by launching tasks for a smoke test.

## Working tree / close-session record

Maestro branch feat/graph-engineering-foundation, HEAD **c711dd2** at last check.
Preserved preexisting edits:
maestro/backends/catalog.py, maestro/limits.py, maestro/templates/project.yaml.tmpl,
tests/backends/test_catalog.py, tests/test_limits.py.
Preserved original untracked artifacts/graph-engineering/p12-pilots/duetflow.json and
docs/GRAPH_ENGINEERING_ARTIFACT.md.

This session's uncommitted source: maestro/intake.py, tests/test_operator_intake.py,
maestro/hitl/commands.py. Docs: docs/OPERATOR_INTAKE.md, docs/intake/ example,
docs/reviews/2026-09-30-modelctl-maestro-coverage.md; ignored plan/STATE/handoff updated.
Handoffs/STATE stay durable on disk under the repo's existing ignored conventions;
do not force them into git. No modelctl modifications, global setting writes,
daemon restart, notification send, live Spotify write or runtime adoption occurred.

Close-session and handoff skills read and applied in that order. Developer's measured
context warning was **181711** tokens, within the close-out awareness window. Native
ctx_session is unavailable; no later exact count invented. Closed at first verified
core boundary. No long-lived jobs or subagents were started; all yielded short tool
commands completed. No failing focused tests remain; public processing integration
and full-suite evidence are explicitly unfinished, not claimed complete.

Evidence/support: handoffs/2026-09-30-operator-intake-support/.
Suggested skills: test-driven-development (read writing-good-tests),
verification-before-completion, requesting-code-review; context7-cli only for
unresolved current CLI/API/library questions. Use close-session/handoff for the
next lifecycle boundary. Follow AGENTS permission remediation and exact user steps.
