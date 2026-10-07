# Continue approved modelctl integration after first implementation milestone

## Goal and read first

Complete the approved shared policy/routing/adoption layer, with modelctl new-model
**detection automatically creating a Maestro replacement proposal for approval**.
Dan answered the pending question with **Create a proposal for approval**. Never
infer permission to activate new models, deploy code, continue pilots or run paid probes.

Read root AGENTS.md, `docs/plans/2026-09-30-modelctl-integration.md`, then this handoff.
Continue in the existing worktree:
`/home/dan/projects/maestro/.scratch/worktrees/modelctl-integration`, branch
`feat/modelctl-integration`, base/HEAD `a40cafc44ea00051a1e609833d15fd710c61cf30`.
All new implementation/tests are **uncommitted**. Do not recreate the worktree or
repeat the completed comparison in `docs/reviews/2026-09-30-modelctl-maestro-coverage.md`.

The worktree ledger/evidence directory (called E below) is:
`.superpowers/sdd/2026-09-30-modelctl-integration/`.
Read E/progress.md and inspect the actual diff including untracked files. The short
approved plan serves as the execution brief; no architecture approval is needed.

## Built so far; feature not complete

- `maestro/model_policy.py`: strict routing-only schema (no lifecycle numbers),
  qualified identities, executing-source bootstrap provenance, immutable content
  addressed revisions with integrity checks, atomic current pointer and CAS;
  replacement transfers only shared references/backend preferences; explicit override
  provenance; adopted revision reads; task snapshot APIs; graph preference/catalog
  building and provider-specific existing-parser reads without cache writes.
- `model_inventory.py`: explicit validated project registry, immediate-child candidate
  inspection, source reader version marker, conservative activity proof from SQLite
  events/runs/tasks/attempts/leases plus legacy state, same-controller-lock adoption,
  actual updated/deferred/upgrade-required outcomes. HALT/configuration are untouched.
- `modelctl_bridge.py`: isolated bounded subprocess import of explicitly configured
  `MODELCTL_SOURCE`; existing Env/providers/latest_per_family/table adapters. Limits
  and discover are read-only. No scanner/inference/restart/registration. Secrets and
  raw provider exception messages are not forwarded. The upgraded owner implements outbox;
  legacy-owner refusal is tested using a disposable source copy without that API.
- `model_replacement.py`: registered destination preview with actual verified native
  backend/profile plus lifecycle validation, snapshot/config/registry/provider staleness
  checks, explicit confirmation, one policy publication then independent adoption.
  Missing row returns actual provider Model unknown suggestion/customization notice
  and stays disabled pending owner transaction/retention hardening.
- `model_detection.py`: persists one pending_approval proposal per qualified detected
  model, commits before acknowledgement, retries acknowledgement/delivery safely.
  Notification transport is optional; CLI consumption leaves notification_pending
  true until delivered. It never activates models or starts a second scanner.
- CLI: argument-free `maestro models` interactive current binding → harness → latest
  replacement → preview → explicit `yes`; optional status/register/candidates/adopt/
  consume/proposals verbs. JSON status does not create shared state. Role/capability
  selection goes through real consumers; registered disposable cross-harness flow works.
- Consumer wiring in roles, agentcall, implementer, orchestrator and workflows/routing:
  adopted shared roles; strict unusable bounded-call refusal; graph qualified
  preferences rank after hard constraints and before objective, with quota fallback
  explanations; graph shared catalog retains destination window/pool/effort metadata
  and eligible explicit older routes; idle controller boundary adoption, graph catalog
  refresh preserving its resources/statistics. Partial literal legacy pin refusal.

New tests: `tests/test_model_{policy,adoption,graph_routing,replacement,detection}.py`
and `tests/test_modelctl_bridge.py`. Test-first evidence logs are in E. Uses actual
policy/store/project/SQLite/role/router code; external metadata/transport doubles
are confined to their boundaries. Native table read tests use the actual owner source.

## Modelctl owner seam: concrete tested patch, applied with explicit approval

Dan explicitly approved the tested patch during close-out. The external
`/home/dan/projects/modelctl` now contains exactly the reviewed five-file source/test
patch; its staged index was verified byte-identical by the guarded application script.
The prior staged work remains staged; this patch was not staged or committed.
An isolated source copy at `/tmp/modelctl-maestro-candidate-20260930` implements:

- `modelctl/core/outbox.py` contract version 1, events/acknowledgements in existing
  owner State; durable idempotent per provider/model identity, family-relative older
  IDs supplied by owner metadata/state, locking ack with existing modelctl lock.
- `scan.py` queues first detection before configured early-return, including a model
  registered before first scan; does not label configured-only discovery VERIFIED.
- public `cli.py cmd_scan` calls consumer after scan/save and lock release. Consumer
  source comes from Env.maestro_src. No consumer means pending retained. No new timer.
- Five added regression/integration tests plus corrected initial configured-scan test.
  Public scan automatically created one Maestro pending proposal in disposable HOME;
  duplicate scans/ack retries and failed consumers retained the correct queue.

Isolated owner suite: **135 passed, exit 0**, 4.93 seconds. Evidence
E/modelctl-candidate-full.log; E/modelctl-outbox.patch; E/modelctl-patch-manifest.json;
E/owner-patch-files/ contains five concrete review files, so /tmp is not the only copy.

Dan answered **Apply the tested modelctl patch**. Application command:
`/home/dan/projects/maestro/.venv/bin/python /tmp/apply-modelctl-maestro-outbox.py`
with sandbox escalation for its explicitly authorized external writes → exit 0,
`Applied 5 reviewed files; staged index unchanged.` No permission question remains.
The guarded script's durable copy is E/apply-modelctl-maestro-outbox.py; **do not rerun**
it on already patched source. It intentionally refuses if original hashes no longer match.

Post-application external owner suite:
`MODELCTL_MAESTRO_TEST_SOURCE=/home/dan/projects/maestro/.scratch/worktrees/modelctl-integration PYTHONDONTWRITEBYTECODE=1 /home/dan/projects/maestro/.venv/bin/python -m pytest -q -p no:cacheprovider tests --basetemp=/tmp/modelctl-applied-outbox-pytest-20260930`
→ **135 passed**, 5.09s, exit 0; E/modelctl-applied-full.log.
All five applied hashes match E/modelctl-patch-manifest.json new_sha256. Preserve the
original E/preserved.json as baseline; approved differences in those files are expected.

No missing-row registration hardening is in this patch. That path remains disabled.
The current external owner scanner/timer and installed consumer source are not claimed
integrated or deployed. A candidate-to-candidate public-scan proof is the verified scope.

## Verification and close-out

- Focused milestone command:
  `/home/dan/projects/maestro/.venv/bin/python -m pytest tests/test_model_adoption.py tests/test_model_policy.py tests/test_modelctl_bridge.py tests/test_model_graph_routing.py tests/test_model_replacement.py tests/test_model_detection.py tests/test_one_shot_agent_calls.py tests/test_roles.py`
  → **159 passed**, 5.97 seconds, exit 0; E/milestone-focused.log.
- Initial original collected suite: **5297 passed, 1 xfailed**, 428.16 s, exit 0;
  E/baseline.log and .exit. Required doc copy was corrected while this suite ran,
  and early source work also proceeded during it; do not describe it as an immutable
  pristine source baseline. The final candidate suite is the authoritative milestone gate.
- Full candidate gate: E/milestone-full.log and E/milestone-full.exit. Its runner is
  `/tmp/maestro-modelctl-milestone-gate.sh`, named agents TMUX window
  `codex-maestro-modelctl-gate`. Startup healthy; no source edits after gate launch.
  First gate completed: **1 failed (test_no_backend_name_branching_in_maestro),
  5338 passed, 1 xfailed**, 391.26s, exit 1. Three name branches were corrected
  using registry.DriverRef.modelctl_provider declared adapter capability;
  gate plus focused suite **192 passed**, 6.63s, exit 0 (E/gate-fix-green.log).
  Final repeat suite is E/milestone-final.log/.exit, runner
  `/tmp/maestro-modelctl-milestone-final.sh`, TMUX `codex-maestro-modelctl-final`.
  E/milestone-final-source-hashes.json freezes its source. Terminal addendum below.
- E/milestone-source-hashes.json records source/test hashes; E/doc-inputs.json captures
  current 27 required graph inputs, including legitimately updated root source STATE.
  All copied required inputs hash-match; do not use old release hash as current baseline.
- `git diff --check` passed. Root release preservation command exited 0:
  `.venv/bin/python handoffs/2026-09-30-operator-intake-release/verify-preservation.py`.
  DuetFlow f13b2c7 pin, HALT, global pointer, product records, five Maestro owner files
  all unchanged; I-211702189 remains rejected, revision 1, no notification pending.
- E/preserved.json: five main Maestro owner files and 28 original modelctl Python
  sources were hash-identical before the approved owner patch; afterwards only
  the three existing files and two added files in the owner manifest differ. No global routing/provider table,
  operating registry, deployment, paid inference, restart, commit, push or merge.
- Native ctx_session/set_session_progress unavailable. Developer last measured
  **180356 active context tokens**, awareness window; finish this verified milestone,
  close-session then handoff. Do not invent a newer counter. No native compaction.

## Remaining work / next actions

1. Verify this handoff's terminal gate addendum; address any failures with TDD. Resume
   actual branch edits, never main owner's five files. The owner patch is already explicitly approved and applied; do not ask again.
2. Complete task/run freeze integration: `freeze_task`/`task_policy` are implemented
   and tested as APIs, but are **not yet consumed by graph start_run/recovery or legacy
   launch/retry**. Current adopted pointer is conservatively stable while active work
   exists; prove complete retry/recovery/paused/approval behavior and store the chosen
   revision with actual task/run records. Graph routing built at startup alone is not
   final proof. Do not add Context Gate logic or a second fresh/resume seam.
3. Finish literal task-pin hard constraints for graph and all legacy/bounded callers.
   Current legacy helper rejects a literal known to the wrong backend once shared policy
   exists; it does not completely validate pins/keyword/fallback through provider rows.
   `_implementer_backend` still ignores unusable Resolution; direct settings.models can
   bypass invalid_models. Fix with observed RED tests before claiming refusal complete.
4. Finish rollback publication, retry reconciliation, retirement handling from owner
   metadata and truthful status/doctor views. Metadata failure never means retirement.
   Proposal notification transport remains unfinished (records stay notification_pending);
   current CLI proposals are list/consume only; add explicit proposal review/selection
   through the existing replacement use case, mark proposal approved/applied truthfully,
   and prove old-policy staleness and failed registration/selection reporting.
5. Improve switch transaction checks: initial project/provider/registry hashes are
   checked before publish; inspect races and validate effective overrides/dependencies
   before publishing. `adopt` currently publishes a pointer file under the controller
   transaction, not an event-backed adopted-revision record. Examine source reader
   compatibility proof before broad installed-version claims.
6. Future init templates still contain explicit role pins; change to explicit inheritance
   without editing existing project configs. Wire init/register and inspect bounded-call
   hardcoded defaults (including selfheal.JUDGE_MODEL). Add public CLI/doctor evidence.
7. Complete unknown destination profile/native metadata support or truthfully retain its
   refusal. Current policy allows explicit strength/cost for new IDs, but catalog_defaults
   refuses IDs lacking a shipped verified native profile. Do not clone an old vendor's
   window/effort/pool to make new-model switching pass. Use provider-owned native metadata
   and verified backend controls. For known destinations keep their own grades.
8. Missing registration stays disabled until modelctl owner hardens smoke exceptions and
   state recording inside rollback, snapshots state along with files/cache, and refuses
   removing rows/windows referenced by registered projects or active/frozen tasks.
   Maestro must supply dependency inventory; no ownership duplication. Acceptance/custom
   unknown limits, unsafe windows, partial-success reporting still need tests.
9. Portability: some new consumer tests currently use real host default provider tables
   to exercise adopted limits; move those to explicit disposable model_limits files so
   future CI does not depend on installed rows. Keep real-owner bridge test separately
   skip-aware if external checkout missing. Do not weaken behavior assertions.
10. Complete registered same-harness flow, crash recovery, lock contention, unsupported
    version, malformed config, retirement/rollback and genuine interactive disposable
    session evidence. Current public interactive test is in-process with scripted input
    and metadata double; do not report it as real account discovery or live routing.
11. Fresh independent whole-branch review allowed by executing-plans, then full candidate
    gate in named TMUX with required inputs, source/input freeze and operating-pointer
    isolation. Preserve logs and exact counts/exit. Re-run release preservation.

## Scope and execution guidance

In scope: approved shared policy, detection proposals, modelctl adapter/outbox seam,
qualified routing and provenance, graph/legacy agreement, idle adoption, disposable
verification, owner registration/retention contract coordination.
Out of scope: live switches/deployments/global code adoption, DuetFlow repair/resume,
HALT clearing, Spotify writes, new Telegram selection UI, Context Gate, Antigravity
adapter development, daemon restart and unrequested paid inference.

Skills already applied: executing-plans, test-driven-development + writing-good-tests,
using-git-worktrees (existing isolation), systematic-debugging, verification-before-
completion, monitor-long-running-tasks, close-session, handoff. Load applicable execution
skills anew next session. Context7 only for unresolved current vendor/library/API questions;
ordinary business-logic/source work does not require it. Do not spawn implementation
agents absent authorization; final whole-branch reviewer is allowed by executing-plans.

User's latest answer settled proposal semantics. Owner patch permission is resolved and its source/test patch is applied.
Continue independent approved work without requesting design approval again.

## Final verified milestone addendum

- Final gate: **5339 passed, 1 xfailed**, **380.77s**, **exit 0**.
  E/milestone-final.log/.exit and E/milestone-monitor-terminal.txt.
  E/milestone-final-source-hashes.json still matches every source/test input.
- Gate correction was observed RED then GREEN; no remaining failed test at close.
- Owner patch is explicitly approved/applied; its post-application suite passes
  **135 tests**, exit 0, and its staged index remains unchanged. No permission pending.
- E/close-preservation.json verifies frozen candidate, all original owner files except
  the five explicitly approved patch files, and exact approved patch content.
- Main branch still has its five original owner edits and original untracked artifacts.
  Worktree product/test changes remain uncommitted on feat/modelctl-integration.
  No source changes were made after the final gate launched.
- Next unit: wire real task/run policy freezing and consumer refusal/recovery, then
  continue the remaining approved sequence. Do not report this milestone as the
  completed feature or as a live rollout.

Final release preservation re-run exited 0; every preservation flag true. Session runner/monitor sweep found zero remaining scripts. E/milestone-status.json records the closed session and unfinished feature accurately.
