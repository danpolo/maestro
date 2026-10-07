# Automatic model class transitions — execution plan

Current status: candidate qualified by independent review/targeted confirmation and
fresh full serial gate (Maestro5537 passed/1 xfailed; owner197 passed; both exit0;
451 frozen inputs/preservation unchanged). Concrete guarded19-file owner package is
approved by Dan, applied and verified (197 owner tests pass, staged index preserved).
Native Astra data/Maestro integration/live rollout remain separate.
Application record: `docs/plans/2026-10-07-modelctl-owner-applied.md`.

Authority: accepted D-A–D-E in `2026-09-30-modelctl-integration.md`, continued
from session-7 handoff. Existing worktree/ledger; inline execution. No live switch,
deployment, paid inference, commit/merge, DuetFlow changes or Context Gate work.

1. Verify historical frozen candidate (done: 342 matching inputs, terminal exit 0).
   Capture fresh owner/main/operating baseline; preserve volatile usage observations.
2. Isolated modelctl candidate: retain predecessor rows; put smoke and state-save
   inside rollback; inherit existing-class lifecycle and enforced window automatically.
   New classes wait for supplied limits. Successful registration emits MODEL_SET,
   separate from already acknowledged discovery. No live registration tests.
3. Owner read contract supplies family/version, set status, predecessor and enforced
   window. Store identity/profile facts immutably with each policy; no Maestro model
   name parser. Unknown successors inherit only same-backend/same-family profiles,
   capped by owner window. New classes need verified or explicitly supplied grades.
4. Resolve project/fresh-task choices by family in adopted policy; preserve class
   and provenance. Freeze concrete versions throughout each task/retry/recovery.
   Reader-version bump makes old readers report upgrade_required.
5. Consume set events with automatic CAS publication and independent between-task
   adoption. New classes add routes without changing preferences. Reconcile published
   transitions after crashes; one durable switch notice supports transport retries.
6. Maestro dependency inventory covers every registered adopted policy and unfinished
   frozen task/run; unreadable/unknown state blocks deletion. Modelctl owns row cleanup
   and rechecks the inventory under its mutation lock. Registration always retains rows.
7. Prove contract, bounds, class following, new-class waiting, notice/recovery, deferred
   projects, frozen runs, smoke/state rollback and registration/selection partial success.
   Prepare reviewed owner patch and guarded executable application script, preserving
   staged index. Ask Dan once for application only after concrete verification.
8. Fresh independent review, then frozen serial full gate in named agents TMUX;
   disposable runtime/model state, recording send-to-me. Verify exact tests/exit,
   unchanged source/owner/main/host and operating-file set, diff check, live-state absence.

Python: /home/dan/projects/maestro/.venv/bin/python. Evidence and ledger:
.superpowers/sdd/2026-09-30-modelctl-integration/. Retain all historical evidence.
Owner application remains pending approval; old outbox patch must not be reapplied.
Native Astra capacity remains owner-data follow-up. Acceptance pending new review/gate.

Session 9 bounded milestone: 1374 related Maestro tests; 160 isolated owner tests, all
exit 0. Identity, current/superseded events, rollback, validation, concrete dependency
and public notice-retry regressions implemented. Controller/pruning concurrency, total
owner+consumer notice semantics, reporting, missing-row workflow, review/application
package and new full gate remain pending. Exact next actions and durable owner copy:
`handoffs/2026-10-07-modelctl-session9-next.md`. No acceptance-completion claim.

Session 10 bounded controller milestone: 1802 related Maestro tests and 170 isolated
owner tests pass, exit 0; 335 frozen inputs unchanged during the serial run. Shared
freeze/adopt/prune lock and renewed-lifetime reservation, frozen project/task choices,
real controller restart/retry, real subprocess owner inventory and combined one-notice
scan behavior are covered. Full legacy record/launch agreement and pruning interleavings
remain pending, along with profile/identity edges, missing-row workflow, reporting,
independent review, owner application package and the full acceptance gate. Continue
from `handoffs/2026-10-07-modelctl-session10-next.md`; candidate remains unreviewed and
unapplied. Durable owner is E/session10-owner-candidate/ (47 manifest inputs).

Session 11 bounded legacy/pruning/reporting milestone: 4274 expanded related Maestro tests
pass, exit 0; corrected full isolated owner suite 181 pass, no skips, exit 0. Both
335-input freezes unchanged during their runs. Legacy record/launch and detached task
calls preserve frozen choices; real pruning interleavings, shipped profile edits,
recovery/shape/identity guards, waiting-limits/class/switch reporting and owner fallback
limits are covered. Registration requirement mapping/documentation, remaining launch
interleavings/shape qualification, independent review, owner application package and
full acceptance gate remain pending. Candidate remains unreviewed and unapplied.
Next: `handoffs/2026-10-07-modelctl-session11-next.md`.

Session 12 qualification/review milestone: registration ownership mapping/documentation,
legacy reservation/launch/record consistency, task-model shape and historical literal
resolution are covered. Fresh independent review found three Important issues; pruning
and supported-role handling were fixed, and targeted confirmation's project eligibility
edge was fixed with129 focused passes. Configured-installation bootstrap remains OPEN
with a real owner integration requirement RED. First full serial gate5514 passed/1 xfailed
and owner191 passed; later frozen serial verification5518 passed/1 xfailed and owner191
passed/1 intended bootstrap failure, with400 inputs/preservation unchanged. The last
small eligibility fix postdates that full run. Candidate is incomplete and unapplied;
no application approval or qualified application script. Continue inline from
`handoffs/2026-10-07-modelctl-session12-next.md`; next task is bootstrap/migration,
then remaining qualification, targeted review, guarded package and fresh full gate.
