# Next session: implement approved modelctl integration

## Goal and read first

Implement Maestro's approved shared model policy/routing/adoption layer, with
**modelctl new-model detection as the trigger that starts the rest of the workflow**.
This trigger is Dan's latest correction, not optional future scope.

Read:
1. Root `AGENTS.md` and global instructions supplied with the session.
2. `docs/plans/2026-09-30-modelctl-integration.md` — architecture is now approved;
   includes the detection requirement, pending question and execution sequence.
3. `handoffs/2026-09-30-modelctl-integration-preview/status.json` and `inventory.json`.
4. `docs/reviews/2026-09-30-modelctl-maestro-coverage.md` — existing comparison and
   external modelctl defects. Do not rerun the entire completed comparison.
5. `handoffs/2026-09-30-modelctl-integration-next.md` for the preserved live boundary.

Do not ask for architecture/design/implementation-plan approval again. Dan explicitly
approved the delivered proposal, then said “continue”. He did not approve a live
model switch, code deployment, pilot continuation or paid-inference probe.

## One narrow trigger question is still pending

Dan's exact steering: “approved, just make sure that modelctl new model detection is
the trigger that starts the resr”. The ending looks truncated, but the detection
trigger requirement is clear. Native async question asked:

> Should modelctl detecting a new model start a Maestro replacement proposal for
> your approval, or automatically replace the shared model once its limits are registered?

Choices: proposal for approval (recommended), or automatic replacement after registration.
Dan's later “continue” did not identify either choice. No automatic activation was
implemented or inferred. Continue independent approved policy/routing work while
resolving this narrow question; do not make this an excuse to re-open the approved
architecture. Do not present a manual picker as the sole entry point and call the
complete feature done.

## Ready isolated workspace; no product code yet

- Main checkout `/home/dan/projects/maestro`, branch `feat/graph-engineering-foundation`,
  HEAD `a40cafc44ea00051a1e609833d15fd710c61cf30` remains unchanged.
- New clean worktree:
  `/home/dan/projects/maestro/.scratch/worktrees/modelctl-integration`
  branch **feat/modelctl-integration**, same base **a40cafc44ea00051a1e609833d15fd710c61cf30**.
  `.scratch/` is already Git-ignored. `git worktree add` completed, exit 0;
  `git status --short` there is empty. Worktree creation was sandbox-permitted.
- No production code, new tests, commits, baseline test runs or full suites have been
  written/run in this worktree. Preparation is complete; implementation is not.
- No SDD ledger/workspace created yet. Execution can start at plan step 1. Prefer the
  concise approved plan over another extended design exercise.
- Ignored required graph-engineering documents have **not** been copied to the
  worktree. Before any tests that consume them, copy the current 27 required inputs,
  following paths in
  `handoffs/2026-09-30-operator-intake-release/comment-example-fix/candidate-doc-inputs.json`.
  Use fresh hashes: root STATE.yaml now legitimately contains the approval/trigger
  update, so its old release hash is not the new candidate's baseline. Never run
  old release/adoption scripts just to copy inputs.
- `git diff f13b2c7 a40cafc -- maestro tests` was empty: the base's product/test source
  equals the previously gated release. Its gate was 5297 passed, 1 xfailed, exit 0;
  this is earlier evidence, not a new test run. Fresh tests remain required.

## Detection seam findings to carry forward

Current external source `/home/dan/projects/modelctl` is staged/uncommitted; it has
no repository AGENTS.md/CLAUDE.md. Ancestor instruction files checked were absent.
Source was inspected, not changed. Before writing there, recheck owner state.

- `modelctl/core/scan.py:scan_provider` produces `NEW_MODEL_DISCOVERED` and
  `MODEL_NOW_AVAILABLE`; `scan()` persists only rendered events into `last_scan`.
  A subsequent scan overwrites this list. It is **not** a durable event queue.
- Already-configured model rows take the early `if is_configured` branch before
  `NEW_MODEL_DISCOVERED`, so relying solely on that event currently misses first
  detection of a model registered before the scan.
- `core/state.py` stores provider/model identity, family, first_seen, status,
  fingerprint and removed flag. Use existing modelctl discovery/state ownership;
  do not add a second provider scanner or Maestro discovery timer.
- Need a durable acknowledged/idempotent event handoff that survives consumer,
  notification and scan retries. Prove one pending workflow per detection identity,
  including a newly detected configured model. Coordinate this owner seam explicitly;
  preserve staged work, never silently overwrite it.
- Existing registration rollback/state-save and false-VERIFIED findings still stand.
  Existing registered-model switches can be implemented independently. Automatic
  missing-row registration cannot be counted complete before owner hardening and
  referenced-row retention are verified. Discovery/configuration is not paid inference.

## Verified work and evidence

Read-only bounded inventory of immediate `/home/dan/projects` children, using
`handoffs/2026-09-30-modelctl-integration-preview/inventory.py`:
- Two operating project candidates: AbuAliArchive inherits legacy defaults, uses old
  global `2f555c2`, state P10 in progress. Codex fallback models unspecified. Needs
  policy-reader upgrade and full idle proof; zero in-flight records is insufficient.
- DuetFlow explicitly configures role models, HALTed on f13b2c7, idle projection,
  DB contains one succeeded run and 10 succeeded/4 failed attempts, no active statuses.
  Existing graph ignores roles; proposed integration uses eligible role preferences.
  Preserve its explicit Sonnet 5 and GPT-6 Sol values, report invalid/retired references.
- Maestro's own evidence `.orchestrator` has no operating config/state; exclude.
- Installed-source role probes both exited 0. First AbuAliArchive probe failed because
  its old version lacks `maestro.backends.catalog`; the bounded probe was corrected
  to report that explicitly. This was an inventory-script fix, not a runtime repair.
- Repeated inventory matched project, provider-table, modelctl-source and model-owner
  hashes. Both JSONs available: retained inventory.json and
  `/tmp/maestro-modelctl-inventory-verification.json` (temporary secondary evidence).
- Human review HTML `/tmp/maestro-modelctl-integration.html` was verified in a browser:
  desktop 1280x800/mobile 390x844, no horizontal overflow, primary map fully visible,
  two diagrams, zero external assets or page errors; light/dark screenshots inspected.
  Sent privately with `send-to-me --caption TEXT ABSOLUTE_PATH`, exit 0; sender checks
  Telegram `ok:true`. Hash, logs and screenshots are in the preview evidence directory.
  Dan approved it. The reviewed HTML is outside Git; preserve the Markdown plan.

Close-session then handoff were applied. No native ctx_session/dashboard tool exists.
Developer last measured **191304 active context tokens** at the clean-worktree boundary,
inside the 180K–200K awareness window. That is the last measurement, not an invented
final count. No automatic compaction occurred. This is the clean execution handoff.

## Preservation and scope

In scope: approved shared policy, local interaction, modelctl detection trigger,
external adapter reuse, override provenance, graph/legacy agreement, idle adoption,
disposable end-to-end verification and owner seam coordination.

Out of scope: DuetFlow repair/resume, clearing HALT, Spotify writes, new Telegram
selection UI, Context Gate, Antigravity adapter development, daemon restart, global
code-version adoption and unrequested paid inference. No third model manager.

Main checkout retains the same five unstaged owner files:
`maestro/backends/catalog.py`, `maestro/limits.py`,
`maestro/templates/project.yaml.tmpl`, `tests/backends/test_catalog.py`,
`tests/test_limits.py`. Original untracked duetflow pilot artifact,
docs/GRAPH_ENGINEERING_ARTIFACT.md and docs/reviews remain untouched.
New plan/evidence/handoff and STATE changes are ignored process documentation by
convention. No commit, push or merge. No source/model settings in modelctl changed.

Final release preservation command exited 0:
`.venv/bin/python handoffs/2026-09-30-operator-intake-release/verify-preservation.py`
Pin f13b2c7; HALT/global-pointer/model-owner preservation true; product state retained
except prior installed metadata; I-211702189 still rejected, revision 1, delivery
not pending. Host process sweep found zero session browser/verifier processes.
No test/agent/controller/watchdog/listener started in this continuation.

## Next actions and checks

1. Start from the ready worktree. Read approved plan/root AGENTS and preserve owner
   hashes; copy required ignored graph docs. Establish appropriate fresh baseline.
2. Load TDD and its full writing-good-tests.md. Write the first public CLI/vertical
   policy test, observe expected RED, implement and prove GREEN. Do not claim the
   shared layer exists until real consumers use it.
3. Follow the approved implementation sequence; record task progress and rulings
   durably. Resolve only the narrow trigger semantics, plus actual newly discovered
   material owner-interface facts; no staged re-approval of the approved contract.
4. Use registered same/cross-harness disposable switches, preserved explicit overrides,
   active/restart deferral, task-revision freezing, old-version refusal, retirement,
   rollback and idempotent detection delivery to prove outcomes. Report actual
   backend/model/revision, exact test counts and exit statuses.
5. Full candidate gate needs all required ignored docs and proper operating-pointer
   isolation. Run long work in named agents TMUX windows with startup/terminal proof,
   using monitor-long-running-tasks. Fresh final review per executing-plans is allowed
   by that skill; do not spawn implementation agents absent authorization.
6. Re-run preservation, then accurately report any external owner dependency still
   unfinished. No live deployment unless separately authorized.

Skills read this session: brainstorming, visual-explainer, agent-browser/core,
systematic-debugging (probe/environment failures), verification-before-completion,
test-driven-development + writing-good-tests, using-git-worktrees, writing-plans,
executing-plans, monitor-long-running-tasks, close-session, handoff. Implementation
agents should load applicable execution skills anew; user instructions override
staged approvals and skill defaults. Context7 is needed for unresolved current
vendor/library/CLI/API questions, not ordinary source/business-logic inspection.
