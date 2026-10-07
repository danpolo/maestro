# Continue modelctl integration — after session 4 (event-backed adoption, init inheritance)

## Goal and read first

Continue the approved shared model policy/routing/adoption feature. Read, in order:
root `AGENTS.md`, `docs/plans/2026-09-30-modelctl-integration.md`,
`handoffs/2026-09-30-modelctl-implementation-milestone-next.md` (sections "Built so far",
"Modelctl owner seam", "Scope and execution guidance" still apply verbatim),
`handoffs/2026-09-30-modelctl-session2-next.md`, `handoffs/2026-09-30-modelctl-session3-next.md`,
then this file. Everything needed is in these files.

Worktree (do not recreate): `/home/dan/projects/maestro/.scratch/worktrees/modelctl-integration`,
branch `feat/modelctl-integration`, base/HEAD `a40cafc`. All work is **uncommitted**
(no commit/push/merge authorized). Ledger/evidence dir E =
`.superpowers/sdd/2026-09-30-modelctl-integration/`; read the tail of E/progress.md.
Python: `/home/dan/projects/maestro/.venv/bin/python` (the worktree has no `.venv`).

Standing constraints (unchanged): no Context Gate work; no live model switch, deployment,
daemon restart, paid inference, pilot continuation, HALT clearing; the modelctl owner
patch is already approved and applied — do not rerun its apply script or ask again;
missing-row registration stays disabled until owner hardening (item 8). The main
checkout's five uncommitted owner files (`maestro/backends/catalog.py`, `maestro/limits.py`,
`maestro/templates/project.yaml.tmpl`, `tests/backends/test_catalog.py`, `tests/test_limits.py`)
and untracked artifacts belong to someone else — leave them alone (edit only worktree copies).

## Done in session 4 (TDD; RED and GREEN logs in E)

1. **Item 5 leftovers — done.**
   - Adoption record is event-backed. A migrated project (has `.orchestrator/control.sqlite3`):
     `model_inventory.adopt` appends `EventType.MODEL_POLICY_ADOPTED` ("ModelPolicyAdopted",
     aggregate `model_policy`, payload `{revision, previous}`) in the same controller
     transaction as the idle proof; no pointer file is written.
   - New `model_policy.adopted_revision(repo, conn=None)`: latest adoption event wins; before
     the first event, a pointer file the project's own controller wrote pre-migration still
     counts (same D08 rule as `state.json`); malformed event or corrupt DB → `PolicyError`
     "unreadable adopted model policy". `adopted_policy(repo, store, conn=None)` uses it.
   - Un-migrated project: CLI `adopt` now **defers** ("no control store; the project adopts
     at its own controller boundary") instead of creating `control.sqlite3`. The old behaviour
     silently migrated the project (D08) with no lock shared with a running legacy controller.
     The running legacy controller adopts in-process (`adopt(..., owner=True)` from
     `orchestrator._model_policy_boundary` when `control_store()` is None), still behind the
     idle proof, into the pointer file.
   - Reader compatibility: `MODEL_POLICY_READER_VERSION = 2` marks the record format;
     `supports_reader` requires equality with the running marker (proven with installed-source
     copies carrying markers 1/2/3).
   - Tests: `tests/test_model_adoption_record.py` (11). E/record-red.log (9 failed, right
     reasons) → E/record-green.log (11 passed). Existing model fixtures now create a control
     store; out-of-band pointer moves append events. Related suites E/record-related.log:
     3699 dots, exit 0.
2. **Item 6 — partial.** Dan decided (2026-09-30) **"Inherit; keep today's models"**:
   - `maestro/templates/project.yaml.tmpl` (worktree copy): `implementer: {}`, `judge: {}`,
     `diagnoser: {}` — explicit inheritance, with a comment on how to override.
   - Shipped defaults take the template's former table: `limits.DEFAULT_JUDGE_MODEL_NAME =
     "Claude Opus 5.5"`; new `limits.DEFAULT_CODEX_IMPLEMENTER_MODEL_NAME = "GPT-6 Luna"`,
     `DEFAULT_CODEX_REVIEW_MODEL_NAME = "GPT-6 Sol"`, `DEFAULT_OTHER_BACKEND_MODEL_NAMES`
     (codex column per role), merged into `roles.DEFAULT_MODELS` (roles.py still names no
     backend; `test_roles_writes_down_no_backend_name` passes). `shipped_policy()` follows.
   - `implementer.resolve_launch_model`: fallback is the role's default column for the
     launch backend — never a Claude id handed to Codex.
   - `cli.cmd_init` registers the project in the shared registry (best-effort; prints
     `shared model policy: registered` or the reason and `maestro models register` hint).
   - `tests/conftest.py`: session-scoped `_isolated_model_policy_home` sets
     `MAESTRO_MODELS_HOME` on `os.environ` for the run (init runs in a module-scoped
     subprocess fixture). `~/.maestro/models` verified not created.
   - Re-baselined tests (behaviour change is the decision, nothing relaxed): test_roles
     (no-default case now uses a backend without a column; fallback test covers both),
     test_switch (two `unset` tests delete the Codex column via monkeypatch), test_templates
     diagnoser test, test_model_lifecycle (judge no longer references Sonnet/Luna),
     test_model_switch_races (disposable table gains gpt-6-luna/gpt-6-sol rows).
   - Tests: `tests/test_model_init_inheritance.py` (6) + `test_cli.py::test_init_registers_the_project_for_shared_model_policy`.
     E/init-red.log (4 failed) → E/init-green.log (7 passed).
   - **Merge note:** the main checkout owner's `limits.py` edit (default names read as the
     latest-per-family Claude rows) and template edit (`gpt-6.1-sol` pins) will conflict with
     these worktree changes. On merge: keep inheritance in the template; set the judge default
     to latest Opus in the owner's scheme; keep the Codex column.

Session 4 gate: `.venv/bin/python -m pytest -p no:cacheprovider -q -n auto` in the worktree:
**5404 passed (dot count), exit 0, 145 s** (E/session4-full.log/.exit). Fingerprint
E/session4-gate-source.sha unchanged after the gate. `git diff --check` clean. Preservation
script from main checkout: exit 0; main checkout status unchanged. Worktree `.orchestrator/`
gained no files.

## Remaining work

- **6 leftover:** public CLI/doctor evidence: `maestro doctor` (and/or `models status`)
  should show adoption state (`model_inventory.adoption_status`), open proposals
  (`model_detection` records not applied/rejected) and effective pairs with provenance.
  Bounded-call defaults were inspected: no hardcoded model id is passed to any bounded call;
  `selfheal.diagnose.JUDGE_MODEL` / `implementer._DEFAULT_IMPLEMENTER_MODEL` are
  documented shipped fallbacks pinned to `roles.DEFAULT_MODELS` by test_roles — nothing to change.
- **4 leftover:** proposal notification transport (records stay `notification_pending`).
- **7 — replace gpt-6-sol with gpt-6.1-sol in the branch, proven on disposable projects
  (Dan decided 2026-10-01: "Branch + tested switch").** Scope:
  1. Catalog: bring the gpt-6.1-sol native profile into the worktree's
     `maestro/backends/catalog.py`. The main checkout owner's uncommitted edit already has it
     (read `git diff maestro/backends/catalog.py` in the main checkout; do NOT modify the main
     checkout). Take only the gpt-6-sol → gpt-6.1-sol change and its verified profile values
     from that diff; the owner's other swaps (gpt-5.6-luna → gpt-5.6-terra) are theirs and are
     not in scope. Never clone gpt-6-sol's window/effort/pool; use the owner's verified values.
     The provider lifecycle row already exists: `~/.codex/model_context_limits.md` has
     `GPT-6.1 Sol | 180K-200K | 240K-270K | 350K`.
  2. Defaults: `limits.DEFAULT_CODEX_REVIEW_MODEL_NAME = "GPT-6.1 Sol"` (judge + diagnoser
     Codex column), so new/inheriting projects get gpt-6.1-sol. Re-baseline tests that name
     gpt-6-sol as the default (test_model_init_inheritance TODAY table etc.).
  3. Keep gpt-6-sol as a still-supported route for explicit pins until modelctl reports it
     removed (retirement comes only from owner state `removed: true`).
  4. Prove end-to-end on disposable projects: detection proposal for ('codex','gpt-6.1-sol')
     → review → approve → one published policy replacing ('codex','gpt-6-sol') in shared role
     preferences → inheriting idle project adopts (event-backed) and its judge/diagnoser
     resolve to gpt-6.1-sol on Codex, in legacy roles and the graph catalog → explicitly pinned
     project keeps gpt-6-sol and is reported as override-preserved → active project deferred.
  5. Out of scope (Dan): no live `maestro models` switch on this host, and no edit to DuetFlow's
     `project.yaml` pins (they stay gpt-6-sol and are reported). The live switch happens when
     the branch is rolled out.
- **8** Missing registration stays disabled pending owner hardening (unchanged).
- **10** Registered same-harness flow, crash recovery, lock contention, unsupported version,
  malformed config, retirement/rollback end-to-end, genuine interactive disposable evidence.
- **11** Fresh independent whole-branch review, then a full candidate gate in a named TMUX
  window with source/input freeze and operating-pointer isolation; re-run
  `.venv/bin/python handoffs/2026-09-30-operator-intake-release/verify-preservation.py`
  from the main checkout.

Suggested next unit: 7 (gpt-6.1-sol replacement), then 6 leftover (doctor/status evidence), 10, 11.

## Verification to report back

Per unit: RED log, GREEN log, focused command + exact pass count/exit, related-suite count,
and whether `.orchestrator/` in the worktree gained files (it must not; it pre-contains
`journal.ndjson`, `model_limits.json`, `usage.json`) and whether `~/.maestro/models` exists
(it must not). At the end: full gate counts/exit/duration, preservation script exit,
`git diff --check`. The repo's pytest config suppresses the summary line; count progress
dots (`grep -o '[.]' log | wc -l`).
