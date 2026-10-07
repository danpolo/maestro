# Modelctl integration — after final independent review

> **Updated scope from Dan, 2026-10-01:** read the integration plan's
> **"Dan's decision changes, 2026-10-01" (D-A–D-E)** first. These decisions supersede
> conflicting scope, pin, approval and profile rules in this handoff and its older chain.
> Session 7's passing review/gate proves the earlier candidate only; it does **not**
> implement or qualify D-A–D-E. Carry the pending decisions into every subsequent handoff
> until their implementation and verification are recorded.

## Goal and read first

Re-plan the remaining integration around Dan's accepted D-A–D-E decisions, then implement
and qualify that revised design in fresh sessions. The earlier registered-replacement
candidate passed its review and serial gate; preserve that work and evidence as the baseline.
Live rollout remains separately authorized.

Read root `AGENTS.md`, `docs/plans/2026-09-30-modelctl-integration.md`, then
`handoffs/2026-10-01-modelctl-session6-next.md` and its preceding chain for implementation,
decisions and evidence. Final review dispositions are in the worktree's
`docs/reviews/2026-10-01-modelctl-final-review.md`.

Worktree: `/home/dan/projects/maestro/.scratch/worktrees/modelctl-integration`.
Branch `feat/modelctl-integration`, base/HEAD `a40cafc44ea00051a1e609833d15fd710c61cf30`.
All implementation/tests remain uncommitted. No commit, push, merge, deployment or live
switch was authorized. E = worktree `.superpowers/sdd/2026-09-30-modelctl-integration/`.
Python: `/home/dan/projects/maestro/.venv/bin/python`.

## Parallel pilot ownership — current instructions

Dan explicitly reminded this session that the handoff had changed. The reread confirms
that another session owns `/home/dan/projects/duetflow` and its operating data. Its Codex
pins changed to `gpt-6.1-sol` with Dan's approval. Do not touch DuetFlow, HALT, task records
or controllers from modelctl work. Do not alter global/per-project installed code pointers,
provider tables or the owner's main-checkout source edits. Do not infer that an old
historical HALT/zero-active snapshot remains current.

The old release preservation script assumes a frozen DuetFlow config and task state;
it now exits 1 at `AssertionError: project.yaml`. This reflects the authorized parallel
pilot changes. Session 7's preservation checker excludes pilot-owned mutable data and
verifies candidate inputs, original owner files (accounting for the approved modelctl
patch), host pointers/tables, worktree operating files and absence of live model state.
Do not reset pilot changes to make the obsolete baseline pass.

## Verified review and fixes

Fresh independent reviewer reported four Important correctness findings, all valid:

1. Common provider lifecycle validation now refuses a ceiling exceeding verified native
   input capacity. Legacy pins, bounded calls, status and graph agree.
2. Publication repeats provider metadata and owner removal-state checks after obtaining
   the policy lock. Changes during lock waits refuse replacement/retirement.
3. First publication persists its referenced initial shipped policy, preserving rollback
   after a source upgrade changes shipped provenance/defaults.
4. Semantic role/backend/model value validation isolates malformed registered projects;
   valid projects still preview, publish and adopt.

No Critical/Minor/design-gap findings. Review limitation: task-freeze tests chiefly cover
helpers and revision equality, with limited evidence of a complete controller restart.
No second whole-branch review is needed absent further material changes.

Test-first evidence: `session7-review-fixes-red.log` (5 intended failures) →
`session7-review-fixes-green.log` (15 passed); `session7-review-more-red.log` (6 intended
failures) → `session7-review-more-green.log` (46 passed). Eleven new regression cases.
Related suite: `session7-related.log`, **1327 passed, exit 0**.

## Final gate and preservation

The first serial gate passed **5433 tests, 1 xfailed, exit 0, 376.32 s**, but its stricter
operating-byte check caught existing tests writing the worktree journal and usage file.
The three-file set stayed unchanged. Original journal bytes were recovered by matching
the pre-gate SHA256. Original usage bytes could not be recovered from the hash; the
overwritten test artifact is retained in `E/session7-test-polluted-operating/`. This
did not touch live project/host data. Do not claim original usage-byte preservation.

Corrective gate isolation sets `MAESTRO_REPO` to a disposable project BEFORE imports.
The isolated repeat preserved operating bytes, but exposed three self-update tests
that expected global adoption while inheriting a project root: **5430 passed, 3 failed,
1 xfailed, exit 1, 375.81 s**. `tests/test_selfupdate.py::_sealed` now explicitly clears
that project setting. Existing tests requiring a project set one afterward. The
three gate failures are RED evidence; self-update plus halted Telegram tests then
passed together: **27 passed, exit 0** (`session7-isolation-green.log`).

The corrected qualified serial gate completed successfully. Runner:
`agents:codex-maestro-modelctl-session7-qualified`; terminal-only monitor:
`agents:codex-maestro-modelctl-session7-qualified-monitor`.

Evidence: `session7-qualified.log/.exit/.start/.end/.terminal`,
`session7-qualified-monitor-terminal.txt`, `session7-qualified-frozen.json` (342 inputs),
`session7-doc-inputs.json` (27 required documents), `session7-preservation.json`.
Operating pointer and model state are disposable; notices use a recording stub.
No source edits after freezing. Exact progress-line test count is required.

Runner/checker/monitor copies are retained under E as `session7-qualified.sh`,
`session7-evidence.py`, `session7-qualified-monitor.sh`. The final operating baseline
is `session7-final-operating-frozen.json`, with raw-byte backups in
`session7-final-operating-backup/`. Original first-gate manifests/logs are retained.
Only the self-update fixture changed after the first two gates; qualified source
and inputs are frozen. No production changes after the four review fixes.

## Remaining scope

The following historical follow-ups are subordinate to D-A–D-E. In particular, item 8
is now a required dependency of automatic transitions, rather than the entire remaining
scope. The fresh-session sequence below is the current next task.

- **Item 8:** registration stays disabled until modelctl's owner hardens state/save/smoke
  rollback and dependency retention. Maestro must provide dependency inventory, then prove
  accepted/custom fallback limits, missing/unsafe rows and registration/selection partial
  success. This session has not modified the owner checkout or enabled registration.
- **Native Astra data:** adopted graph routing excludes `codex:gpt-6-astra` when the
  provider ceiling exceeds its unverified/base native capacity. Obtain owner-verified
  `ContextLimits`; never clone another model's values.
- **Owner tests:** modelctl's two consumer handoff tests inherit PATH and would send real
  notices against this source. Always put a recording `send-to-me` stub first on PATH
  until the owner isolates the transport.
- **Integration/rollout:** needs separate authorization; preserve inheritance, GPT-6.1 Sol
  Codex defaults, pin-only GPT-6 Sol and the owner's independent Terra change when reconciling
  main-checkout owner edits. No Context Gate work.

## Verification for future work

Use the existing worktree and ledger. Verify the terminal gate and frozen manifest before
building on this candidate. Any subsequent code changes require relevant tests and a
fresh full candidate gate. Preserve the original owner hashes plus approved outbox patch;
do not run the already-applied owner patch script again. Report exact tests, exit/duration,
source-manifest comparison, `git diff --check`, operating-file preservation and live
`~/.maestro/models` absence. Exclude the parallel pilot's mutable data from modelctl claims.

Suggested skills: executing-plans, receiving-code-review/TDD for new findings,
monitor-long-running-tasks, verification-before-completion, close-session, handoff.

## Verified terminal addendum — session 7 closed

- Item 11 complete for the registered-replacement candidate. Fresh independent review's
  four Important findings resolved with 11 RED/GREEN regression cases; test environment
  correction proven by 27 focused tests and the corrected full gate.
- Qualified serial gate: **5433 passed, 1 xfailed, exit 0, 373.40 s** (374 wall seconds).
  Count independently confirmed using only progress lines. `session7-qualified-result.json`.
- Corrected gate preservation: **exit 0**. All 342 frozen inputs unchanged; original owner
  source files plus explicitly approved modelctl patch unchanged; host pointer/provider
  tables unchanged; main checkout status unchanged; final operating-byte baseline unchanged.
  Worktree `.orchestrator` still has only its three original filenames; first-gate usage
  artifact mutation remains explicitly recorded above. `~/.maestro/models` does not exist.
- `git diff --check` clean. Old release preservation script **exit 1 at project.yaml**,
  because its pre-pilot baseline is obsolete; do not confuse this with a modelctl change.
- No gate/monitor scripts left running. Disposable runtime directories archived under E
  before cleanup. Branch remains `feat/modelctl-integration`, HEAD `a40cafc`; all feature
  changes uncommitted. No commit, push, merge, live switch, deployment, inference or restart.
- No native context/dashboard counter available; no automatic compaction occurred.
- Next task is the already documented **owner-dependent item 8 registration hardening and
  dependency contract**, when separately scoped with the owner. Registration remains disabled.
  Native Astra capacity and owner notice-test isolation also remain recorded follow-ups.

### Close-out observer update and context boundary

After the qualified gate's successful byte-preservation check, `usage.json` received a
new Opus 5.5 statusline reading (timestamp `2026-10-01T04:45:52Z`). A strict later check
therefore exited 1 on this volatile metrics file. Do not overwrite the observer's update
or describe the file as permanently immutable. The gate's authoritative preservation
result remains `session7-qualified-preservation.log/.exit` (**exit 0**). The fresh scoped
`session7-close-preservation.json` proves candidate/host/main status, non-usage operating
bytes and exact three-file set unchanged, with live models state absent.

Developer measured **181802 context tokens**, entering close-out awareness. Close-session
completed at the verified item 11 milestone; no automatic compaction. Start a fresh
Codex session from this handoff for any further implementation. No unfinished local fix
loop remains in the earlier candidate. The newly accepted D-A–D-E work below supersedes
the earlier instruction to continue only owner-dependent item 8.

## Current next task — carry forward D-A–D-E

Authoritative requirements are in `docs/plans/2026-09-30-modelctl-integration.md`,
section "Dan's decision changes, 2026-10-01"; the updated session 6 handoff points there
too. Read the complete section rather than relying only on this summary.

- **D-A / D-E (Maestro):** project/task choices preserve the model class, resolved from
  modelctl `family` metadata, and follow its current version. Same-backend, same-class
  successors inherit predecessor strength/cost/effort, with native capacity capped by
  modelctl's enforced window. No extra model-name parser or cross-backend profile copying.
- **D-B / D-D (modelctl → Maestro):** existing-class successors automatically register
  using predecessor lifecycle/window limits, pass the owner's smoke/transaction boundary,
  and become live when limits are set. A new class stays invisible until Dan supplies
  limits. Maestro automatically publishes the set model, updates existing-class references
  or adds a new class without changing role preferences, and sends one switch notice.
- **D-C (both owners):** add successor rows alongside predecessors. Remove an old row
  only when every registered project has moved off it and no frozen task/run uses it.
  Maestro supplies the dependency inventory; modelctl owns row retention/removal.

Next session:

1. Reconcile the approved plan and existing implementation against D-A–D-E, recording a
   concise executable plan in the established `docs/plans/` location. Keep historical
   gates as baseline evidence, not acceptance of the revised behavior.
2. Establish the owner contract for family/predecessor identity, a model being set,
   enforced window and dependency inventory; then implement Maestro D-A/D-D/D-E and
   its D-C inventory, alongside reviewed modelctl D-B/D-C transaction hardening.
3. Prepare and test any new modelctl patch in an isolated candidate before requesting
   **one application approval** from Dan, as the governing plan requires; preserve the
   owner's existing staged index. The earlier outbox application
   approval covers only that already-applied patch; never rerun its application script.
4. Prove class-following overrides, existing/new-class activation differences, profile
   inheritance bounds, one notice per switch, row retention for deferred projects and
   frozen tasks/runs, retry/recovery, and owner transaction failure/rollback. Then conduct
   a fresh independent review and frozen full gate for the revised candidate.

Still binding: between-task adoption, concrete version freezing throughout a task and
its retries, `upgrade_required` for older readers, no Context Gate, no unapproved live
switch/deployment/commit/merge, and parallel DuetFlow ownership. DuetFlow's current pins
remain as the pilot set them; they become class choices only after a D-A-capable release.
Do not resume or change that pilot from this integration session.
