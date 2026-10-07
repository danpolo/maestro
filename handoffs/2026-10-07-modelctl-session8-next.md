# Modelctl class transitions — after session 8 partial implementation

## Goal and authority

Finish Dan's accepted D-A–D-E automatic model transitions. Read root AGENTS.md,
`docs/plans/2026-09-30-modelctl-integration.md` (complete D-A–D-E section),
`docs/plans/2026-10-07-modelctl-class-transitions.md`, then this handoff.
The earlier chain starts at `handoffs/2026-10-01-modelctl-session7-next.md`.
Its ownership/scope/preservation constraints still apply. Session 7's full gate
qualifies the old registered-replacement design only.

Worktree: `/home/dan/projects/maestro/.scratch/worktrees/modelctl-integration`.
Branch `feat/modelctl-integration`, HEAD `a40cafc44ea00051a1e609833d15fd710c61cf30`.
All feature changes remain uncommitted. E = worktree
`.superpowers/sdd/2026-09-30-modelctl-integration/`. Read E/progress.md's Session 8 entries.
Python: `/home/dan/projects/maestro/.venv/bin/python`.

**The revised feature is incomplete and unreviewed.** Do not apply the new owner
candidate, enable live registration, run live consume/switch, or claim D-A–D-E done.
No new owner application approval was requested or granted.

## Verified milestone

- Before edits: all 342 session7-qualified frozen inputs matched; terminal exit 0;
  live `~/.maestro/models` absent. Captured E/session8-resume-frozen.json and main status.
- Maestro bounded related suite: **271 passed, exit 0** (progress lines counted),
  E/session8-related-final.log/.exit and session8-related-result.json.
  Command from worktree:
  `MAESTRO_REPO=/tmp/maestro-session8-test-runtime PYTHONDONTWRITEBYTECODE=1 /home/dan/projects/maestro/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_model*.py tests/test_roles.py tests/test_selfupdate.py`.
- Isolated modelctl complete existing+new suite: **146 passed, exit 0, 5.10 s**;
  E/session8-owner-final.log/.exit and session8-owner-result.json.
  Recording send-to-me stub first on PATH; no real notices/inference/restart.
- `git diff --check` exit 0. E/session8-preservation.json verifies external owner
  Python source/tests, main status, all three worktree operating bytes/file set unchanged;
  live models home absent. DuetFlow mutable state was neither inspected nor changed.
- New source snapshot: E/session8-close-source.json. This is a close-out fingerprint,
  not a frozen full-gate qualification manifest.

## Implementation added (partial candidate)

Maestro:
- `model_classes.py`: resolves family choices from immutable owner model/family/version
  facts in policy source; no model-name parser. Reads serialized same-backend profiles.
- `model_detection.policy_for_set`: replaces a same-class route with successor, stores
  owner facts and inherited profile, caps native input by predecessor and enforced window,
  validates lifecycle before publication. New class can add a route with explicit
  strength/cost/output profile; role preferences remain unchanged.
- `consume_set`: durable prepared/applied switch journal, CAS publication and adoption,
  retryable switch notice. Combined discovery/set events produce only one switch notice.
  Class-capable discovery without set records waiting_limits, excluded from open proposals.
- `model_policy`: reader marker **3**; merges class choices in effective_roles; dynamic
  profiles usable in graph defaults/provider lifecycle validation. check_task_pin returns
  resolved concrete model; agentcall and implementer now use that return value.
  Manual replacement/retirement carry class facts/profiles forward.
- `modelctl_bridge.configured_models`: isolated owner classes read operation.
- `model_inventory.dependency_inventory`: read-only conservative inventory of shared and
  adopted routes, explicit project choices, unfinished frozen task policies; active runs
  without a frozen binding block release; unreadable registered state blocks release.
- Tests: `tests/test_model_class_transitions.py` (7), `test_model_dependencies.py` (3).
  Reader-marker fixtures in test_model_adoption_record updated for marker 3.

Owner candidate (external live checkout unchanged):
- Editable copy: `/tmp/modelctl-class-candidate-20261007`.
- Durable complete copy: E/session8-owner-candidate/; SHA manifest
  E/session8-owner-candidate.json. Resume from durable copy if /tmp is gone.
- `run_add` snapshots state file and keeps smoke, FAILED refusal, record/state save inside
  rollback. Four new regression cases for smoke exceptions/partial state writes.
- Claude/Codex add plans retain all predecessor lifecycle/window rows. Verification no
  longer expects them absent. record_added no longer marks every predecessor superseded.
- `core/transitions.py`: owner-only family parsing and aliases; configured_models read
  contract 1; inherited_plan takes newest older configured family row and exact window;
  record_set creates a separate durable MODEL_SET identity with family/version,
  predecessor, enforced window and lifecycle. Explicit routing profile may accompany it.
- Scan uses inherited_plan for existing classes, run_add within current owner scan lock,
  refreshes scanner state, queues set event and sends limits/profile review notice.
  New classes remain unregistered until limits supplied.
- Successful manual add attempts existing Maestro delivery after releasing owner lock.
- `prune_model` API holds owner and shared policy locks, refuses unknown/dependent inventory
  or removal of current family member, transactionally removes unused predecessor rows.
  It is not wired to CLI or automatically invoked. **Not qualified for live use**.
- CLI accepts optional new-class `--strength`, `--relative-cost`, `--max-output` fields.
  This plumbing has no dedicated owner regression yet; qualify before review/application.
- Owner new tests: test_registration_boundary.py (4), test_class_registration.py (4),
  test_retention_guard.py (3). Existing two provider registration tests now assert retention.

## Next task — complete the contract before broad qualification

Continue implementation, not another planning approval. Start with candidate-to-candidate
public-scan/consumer tests and the concrete gaps below. Some are known unfinished wiring;
some are risks exposed by bounded inspection and need requirement-based regressions.

1. **Owner identity and new-class input**: prove Claude aliases across table display,
   owner API IDs and Maestro shipped IDs (including dated Haiku), and exact predecessor
   selection. policy_for_set's inferred predecessor currently matches eligible routes by
   exact model_id; owner aliases need the same treatment there. Current/no-op set events,
   profile/limits edits, and multiple eligible old class routes need proof. New-class
   registration may currently save limits without a routing profile, after which selection
   is refused; fulfill D-D's activation timing with the required operator inputs in the
   same registration flow. Do not invent output capacity or a new-class grade.
2. **Owner failure boundaries**: test actual provider smoke exceptions and state-save
   failure during automatic scan, including scanner in-memory state after rollback;
   unsafe successor windows, account availability failure, custom inherited values,
   quota inconclusive versus unsupported, manual registration/selection partial success.
   supplied_profile CLI/state plumbing was added before dedicated RED evidence; shelve
   that portion for a faithful test-first cycle rather than claiming it was test-first.
3. **Inventory completeness/pruning**: include concrete active attempt bindings,
   legacy in-flight pinned models and task/run choice dependencies, not only policy routes.
   A task pin outside eligible routes can be missing from the current inventory. Prove
   paused/recovery runs, terminal task release, malformed snapshots/unknown source and
   concurrent publish/adoption/freeze. Use actual task enum spelling `canceled` (current
   inventory terminal list incorrectly includes `cancelled`). Prune API tests use a
   supplied inventory double; real subprocess inventory and race proof remain pending.
   Never use candidate prune on live rows before this is qualified.
4. **Consumer recovery/idempotence**: prove crash after current publication but before
   switch journal update, adoption reconciliation, acknowledgement failures, concurrent
   consumers, changed owner metadata during lock wait. Superseded MODEL_SET currently
   raises and remains pending rather than reaching a terminal acknowledged disposition.
   Metadata/event shapes need strict validation and safe PolicyError conversion.
5. **Immutable class/profile behavior**: prove project chose another family, fresh task
   class choice, actual legacy/bounded launches, graph hard constraints and controller
   restart/retry freezing. Current new tests largely exercise public domain helpers;
   whole-controller proof is still missing. Rollback currently constructs a source that
   does not carry root class_metadata/inherited_profiles from its target; fix/test this.
   Validate serialized profile cost and context shapes fully; no cross-backend copying.
   Legacy-owner compatibility still creates approval proposals; confirm intended migration.
6. Enable missing-row replacement only against a verified owner contract; the old
   preview_replace registration_required path is still disabled. Prove accepted/custom
   fallback limits, missing/unsafe rows and truthful partial success as the governing
   plan requires, or record any design supersession against D-B explicitly.
7. Update status/doctor/documentation for class following and switches (current public
   reports still use historical proposal/override-preserved terminology). Fresh independent
   whole-branch review is explicitly allowed by executing-plans/handoff; no implementer
   delegation was authorized. Prepare a concrete owner patch, guarded executable apply
   script preserving staged index, and request **one** application approval after review
   and candidate qualification. Never rerun the prior approved outbox apply script.
8. Freeze all source/docs inputs and run new full Maestro gate serially in a named agents
   TMUX window. Start from E/session7-qualified.sh with new filenames and fresh manifests;
   disposable MAESTRO_REPO/model state, recording send-to-me. Required docs including root
   AGENTS.md must be present (worktree currently has no AGENTS.md; existing hash helpers skip
   absent files). Exact dot count/exit/duration, preservation and no live models state.

## Decisions and debugging notes

An optional structured question asked whether a new family should require strength/cost
with limits (recommended) or default balanced/cost 1. No answer arrived before close.
Candidate follows the previously approved explicit-profile rule; **this is not a new Dan
approval**. Check any late answer before proceeding. Output capacity remains verified input.

The failed terminal-release test used invalid task status `completed`. SQLite's
INSERT OR IGNORE omitted the row while recording its event; this was a test-fixture
error, not controller replay deletion. Changed to actual terminal `accepted`; passes.
No projection bug/fix should be reported. Owner initial full runs failed only old
row-removal/order/governor assertions; all now reflect retained predecessors and pass.

RED logs in E include session8-class-red, pin-red, consume-red, unsafe-red,
one-notice-red, new-class-red, dependencies-red and copied owner boundary/registration/
retention logs. Focused outputs for some early owner GREEN runs are in tool history only;
final owner log is durable. Do not imply a fresh whole-gate ran.

## Scope and lifecycle

In scope: complete D-A/D-D/D-E and inventory D-C in this worktree; isolated reviewed owner
D-B/D-C patch; disposable proof, independent review, new gate and guarded application.
Out of scope: live switches, deployment/restart, paid inference, commit/push/merge,
Context Gate, owner staged-index mutation, DuetFlow pilot/config/HALT changes.
Native Astra capacity stays an owner-data follow-up; no copied native Astra profile.

Developer measured **180069 active context tokens** entering close-out awareness.
close-session and handoff used at verified bounded milestone; no automatic compaction.
No bounded test processes remain running. Start a fresh Codex session from this file.
Suggested skills: executing-plans (inline), TDD, systematic-debugging for unknown causes,
verification-before-completion; spec-code-review/monitor-long-running-tasks when ready.
