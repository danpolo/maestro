> Completed in continuation: final f13b2c7 candidate gated (5297 passed, 1 xfailed),
> pin-only installed, genuine I-211702189 receipt/proposal/rejection verified.
> Temporary listener stopped, no DuetFlow continuation. See
> handoffs/2026-09-30-operator-intake-release/RELEASE_STATUS.md for final evidence.
> The instructions below are the preserved original handoff; do not rerun its rollout.

# Reviewed intake: final exact gate, installation and genuine bot proof

Read AGENTS.md, docs/OPERATOR_INTAKE.md, docs/reviews/2026-09-30-telegram-intake.md,
and docs/plans/2026-09-30-operator-intake-and-modelctl-review.md. Earlier boundaries:
handoffs/2026-09-30-telegram-intake-core-modelctl-reviewed.md and
handoffs/2026-09-30-graph-ask-installed-next-live-smoke.md.

## Goal and approved scope

Finish approved **Maestro** Telegram intake. Code/integration/review are complete;
final exact-version gate, installation and actual Telegram proof remain. Continue
directly, without reopening design or adding staged implementation approvals.

In scope: durable capture/proposal/exact approval -> normal ROADMAP task at idle ->
existing engineering/checks/review/Dan verification/acceptance/graduation; safe pin-only
installation retaining HALT; temporary intake-only listener and receipt/proposal/rejection.
Out of scope: DuetFlow diagnosis/repair/Spotify writes, clearing HALT or starting/resuming
product tasks without Dan's agreement; manufactured /ask wait; Context Gate; model UI.
Preserve docs/reviews/2026-09-30-modelctl-maestro-coverage.md recommendation: external
modelctl owns discovery/registration/context tables; Maestro should own effective
defaults/routing/override provenance/safe propagation. That layer is not built here.

## Commits and evidence

Branch feat/graph-engineering-foundation, HEAD **f46ffd403785bcd9040e892961731c2009fc6220**.
Core **9b4585683eb21d6dda0a12f4206254a8826828cf** adds complete processor integration,
CLI `intake tick [--publish] [--telegram]`, help, durable confirmed notifications/retry,
approval/action replay, corruption isolation, scope/symlink/check validation,
collision/CAS/drift/partial-persistence recovery, progress and public workflow tests.
Final f46ffd4 reads authoritative legacy progress through a client connection when no
graph run exists, with running/waiting/graduation regression. No uncommitted intake code.

- Final independent feature/rollout review ready for remaining gates, no Important or
  Critical findings: **52 passed**, 4.93 s, exit 0; diff check passed.
- Earlier broad focused **484 passed**, 14.41 s, exit 0; source full **5281 passed,
  1 xfailed**, 406.33 s, exit 0. Later regressions mean these are not final exact evidence.
- Base exact 9b45856 selfupdate gate **5290 passed, 1 xfailed**, 390.99 s, exit 0.
  **Final f46ffd4 exact gate has NOT run. Do not adopt/count the base as final.**
- Initial base gate: **5 failed, 5284 passed, 1 skipped, 1 xfailed**, absent ignored
  specs/STATE inputs. Prior /ask release used unchanged-document staging too. Copied
  **27** inputs hash-identically; 13 phase-prompt checks and full gate passed afterward.
  Failure evidence retained; no code/test removed to hide failure.
- Boundary fixtures refuse active run, pending attempt, live writer, missing HALT.
- Intake-only listener rehearsal traps controller opening/model/publication/worker;
  receipt/list/detail/reject/foreign-chat paths pass with **zero tasks/events/leases**
  and unchanged HALT. Generic halted router was removed because help/status could
  journal/take a controller lease. Release Python compiles; shell syntax passes.

All important fixes had red -> green regressions (see review doc). Preserve old
notify_telegram None contract: changing it to bool broke seven characterization tests;
notify_intake is the separate confirmed delivery function. Final legacy event type
is a string constant, not an enum requiring .value.

## Actual boundary

DuetFlow still pinned ead3819ce9110debec25f12dc1fee66174b1367b, HALT true, accepted 07,
zero in_flight/active runs/pending-or-active attempts/intake files. Only its preexisting
docs/dependency_map.md is modified. Preserve history follow-ups and D30/D31/D33.
No pilot pin/file/report/task/model override/global pointer changed; no paid call,
installation or live listener ran. Read-only identity: **@MaestroGenericTestingBot**,
ID 8764245904, no webhook. Identity probe fetched no updates/sent no messages.
Do not tell Dan intake is live or ask him to message it before readiness.

## Exact next actions, owned by next agent

Support: `/home/dan/projects/maestro/handoffs/2026-09-30-operator-intake-release`.
Read scripts; all now target final f46ffd4. Expected outcomes below are not verified yet.

1. Run `.venv/bin/python handoffs/2026-09-30-operator-intake-release/stage-candidate.py`
   from Maestro. Expected staged exact f46ffd4 with verified unchanged document inputs,
   no pin change. Uses normal materialize_worktree and 27 hashed inputs from clean
   tested base worktree. Narrow sandbox escalation needed for ~/.maestro/Git metadata;
   use AGENTS remediation if denied. No privileged service commands required.
2. Launch gate with `tmux new-window -t agents -n 'codex-maestro-intake-finalgate' 'bash /home/dan/projects/maestro/handoffs/2026-09-30-operator-intake-release/run-candidate-gate.sh'`.
   Expected candidate-selftest.json passed:true, exact SHA/worktree, .exit 0; expected
   **5291 passed, 1 xfailed**, report actual. ~6.5 minutes. Startup check once, terminal
   monitor; also verify candidate clean SHA and document hashes.
3. Recheck live idle/HALT/07/leases; run `bash /home/dan/projects/maestro/handoffs/2026-09-30-operator-intake-release/install-at-boundary.sh`.
   Expected `Installed f46ffd403785bcd9040e892961731c2009fc6220; pinned doctor exit 0; HALT retained`.
   Script checks exact gate, original pin, clean code/doc inputs, accepted 07/decision,
   active work/live writer/orchestration lock; uses selfupdate.adopt, checks doctor/global
   unchanged, rolls back on failure. No resume or worker. Reversible feature rollout
   is within finishing request; do not broaden into product continuation.
4. Verify installed pin/state/doctor/HALT/global unchanged. Run `bash /home/dan/projects/maestro/handoffs/2026-09-30-operator-intake-release/start-listener.sh`.
   Expected named window codex-maestro-intake-smoke; listener.log says
   `Ready: @MaestroGenericTestingBot; capture only; HALT retained; 10 minute limit`.
   Never start a watchdog/controller for the smoke. Verify readiness before Dan action.
5. Give Dan actual bot plus this complete message only after readiness:

   ```text
   /report feature Intake smoke test: propose a regression check that rejecting an operator report starts no work. Preserve DuetFlow behavior. This is only a smoke test; I will reject the proposal. Do not approve or implement anything.
   ```

   Expect Saved I-<actual update ID> receipt/detail/list instructions. Have him report
   Sent/receipt. If no reply in 30 seconds, tell him to report that; inspect listener,
   delivery/report, keep HALT. Never invent IDs or instruct /resume.
6. Explicitly run one installed `intake tick --telegram` for the captured smoke while
   HALT remains (configured judge, 60 second bound). Check actual proposal delivery;
   no model override change to force success. Preserve/report quota/network failures.
   Give Dan actual `/intake reject I-… Smoke complete` using real ID. Verify rejected,
   raw text preserved, no ROADMAP task/run/attempt/product change, HALT unchanged.
   Stop temporary listener and verify exit. This does not close actual /ask/D30/D31.
7. Update operator/review docs, plan and release evidence to actual installed status;
   evidence commit may follow tested runtime separately. Report code/gate/live proof
   distinctly. Give exact command/expected output whenever Dan must run something.

## Close-session / preservation

Five preserved model-owner modifications: catalog.py, limits.py, project.yaml.tmpl,
test_catalog.py, test_limits.py. Original untracked duetflow.json and
docs/GRAPH_ENGINEERING_ARTIFACT.md unchanged; model comparison hash unchanged.
Eight-file hashes in support/preserved-files.json. Never stage them as intake changes.
docs/reviews/ is an untracked evidence class (git ls-files returned none), retained
durably; handoffs/plans/STATE stay ignored per convention, do not force into Git.
No push. All created test/monitor processes exited; anchored host process checks/TMUX
showed none left, only unrelated bash window. Reviewer finished.

close-session then handoff applied at reviewed/committed, base-full-gate boundary.
Developer measured **180911** context tokens at awareness warning; ctx_session unavailable,
no invented later count. Start fresh. Suggested skills: verification-before-completion,
monitor-long-running-tasks; requesting-code-review if new code; systematic-debugging
if actual failure. Context7 only for unresolved current library/CLI/API questions.
