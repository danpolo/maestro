# Continue modelctl integration — after session 2 (task freeze + pins + portability)

## Goal and read first

Continue the approved shared model policy/routing/adoption feature. Read, in order:
root `AGENTS.md`, `docs/plans/2026-09-30-modelctl-integration.md`, the previous handoff
`handoffs/2026-09-30-modelctl-implementation-milestone-next.md` (its sections "Built so
far", "Modelctl owner seam", "Scope and execution guidance" still apply verbatim), then
this file. Everything needed is here or in those files.

Worktree (do not recreate): `/home/dan/projects/maestro/.scratch/worktrees/modelctl-integration`,
branch `feat/modelctl-integration`, base/HEAD `a40cafc`. All work is **uncommitted**
(no commit/push/merge authorized). Ledger/evidence dir E =
`.superpowers/sdd/2026-09-30-modelctl-integration/` — read the tail of E/progress.md.

Standing constraints (unchanged): no Context Gate work; no live model switch, deployment,
daemon restart, paid inference, pilot continuation, HALT clearing; the modelctl owner
patch is already approved and applied — do not rerun its apply script or ask again;
missing-row registration stays disabled until owner hardening (prev. handoff item 8).
Main checkout's five uncommitted owner files (`maestro/backends/catalog.py`,
`maestro/limits.py`, `maestro/templates/project.yaml.tmpl`, `tests/backends/test_catalog.py`,
`tests/test_limits.py`) and untracked artifacts belong to someone else — leave them alone.

## Done in session 2 (all TDD, RED then GREEN logs in E)

1. **Task/run policy freeze wired (prev. item 2 — done).**
   - `model_policy.freeze_task(repo, task_id, store, *, renew=False)`: `renew` starts a
     new task lifetime; otherwise the stored binding must equal the adopted revision or
     it raises `PolicyError(... mid-task ...)`. Adoption already defers while any task is
     active/waiting/blocked, so a mismatch only means an out-of-band pointer change.
   - `orchestrator._graph_bind_model_policy(store, task_id, routing_revision)`: renews for
     unregistered/planned/terminal tasks, keeps for active/waiting/blocked; refuses
     routing built from another revision. Called before `start_run` (refusal →
     journal `model_policy_refused`, task added to `compile_refused`) and for every
     recovered running run at startup (refusal → `graph_run_unresumable`, not resumed).
   - `Lifecycle.start_run(..., model_policy_revision=None)` puts the revision in the
     `RUN_STARTED` payload (only when not None); `runner.start_run` passes it through.
   - `orchestrator._legacy_bind_model_policy(task_id, *, renew=False)`: fresh legacy
     launch renews before worktree creation (refusal releases slot/resources, journals,
     `park_failed`); `_do_retry` keeps the binding (refusal → existing retry-failure park).
     Both `in_flight` entries carry `model_policy_revision`.
   - Structural key-order tests in `tests/test_orchestrator_switch_hooks.py` re-baselined
     with one more required key; retry/scheduling fixtures stub the binding so tests no
     longer write into the checkout's `.orchestrator/model_tasks`.
   - Tests: `tests/test_model_task_freeze.py` (11). E/freeze-wiring-{red,green}.log.
2. **Literal pins / bypasses (prev. item 3 — done for legacy + bounded; graph N/A).**
   - `model_policy.check_task_pin(project, policy, driver, model)`: verified shipped
     native model for that driver, not retired, and (modelctl providers) a safe provider
     lifecycle row; else `PolicyError`.
   - `implementer.resolve_launch_model`: keywords validated as the id they resolve to
     (fixes a false refusal of aliases like `opus5`); literals as written.
   - `implementer._implementer_backend`: strict (shared-policy) unusable resolution raises;
     returns `RoleConfig.eligible_models` (new property excluding `invalid_models`), also
     used as the default table in `resolve_launch_model`.
   - `agentcall.resolve_call(model=...)`: explicit model checked on the backend that won.
   - Graph has no task `model:` field (only allowlists + preferences) — nothing to enforce.
   - Tests: `tests/test_model_task_pins.py` (7). E/pins-{red,green}.log.
3. **Portability (prev. item 9 — done for known cases).** `test_model_adoption.py` and
   `test_model_graph_routing.py` now write disposable provider tables into `project.yaml`
   `model_limits`. Model/roles/agentcall suites pass with an empty `HOME`:
   **193 passed, exit 0** (E/portability-empty-home.log).

Related suites after all changes (model, modelctl bridge, control, workflows,
orchestrator_*, no_name_branching, roles, one_shot, agentcall, characterization,
backends): all green (3122 passed before the pins unit; rerun green after it).

Full-suite gate for this session: E/session2-full.log / E/session2-full.exit,
source fingerprint E/session2-gate-source.sha. **Result: 5357 passed, 1 xfailed, exit 0.**

## Remaining work (numbering from the previous handoff)

- **4** Rollback publication, retry reconciliation, retirement from owner metadata
  (metadata failure ≠ retirement), truthful status/doctor views; proposal review/selection
  through the existing replacement use case, marking proposals approved/applied
  truthfully; old-policy staleness and failed registration/selection reporting.
  Proposal notification transport still unfinished (records stay `notification_pending`).
- **5** Switch transaction race checks; validate effective overrides/dependencies before
  publishing; `adopt` writes a pointer file, not an event-backed adopted-revision record;
  source-reader compatibility proof.
- **6** Future init templates: explicit inheritance instead of role pins (do not edit
  existing project configs; note the template file is also one of the main checkout's
  owner-edited files — edit only the worktree copy); wire init/register; bounded-call
  hardcoded defaults (`selfheal.diagnose.JUDGE_MODEL` is documentation-only, not passed);
  public CLI/doctor evidence.
- **7** Unknown destination native profile support, or keep the truthful refusal.
- **8** Missing registration stays disabled pending owner hardening (unchanged).
- **10** Registered same-harness flow, crash recovery, lock contention, unsupported
  version, malformed config, retirement/rollback, genuine interactive disposable evidence.
- **11** Fresh independent whole-branch review, then a full candidate gate in a named
  TMUX window with source/input freeze and operating-pointer isolation; re-run
  `.venv/bin/python handoffs/2026-09-30-operator-intake-release/verify-preservation.py`.

Suggested next unit: item 4 (proposal review/selection + rollback), then 5, 6, 10, 11.

## Verification to report back

Per unit: RED log, GREEN log, focused command + exact pass count/exit, related-suite
count, and whether `.orchestrator/` in the worktree gained files (it must not; it
pre-contains `journal.ndjson`, `model_limits.json`, `usage.json` from older tests).
At the end: full gate counts/exit/duration, preservation script exit, `git diff --check`.

## Addendum — session 2 full gate

- Command: `.venv/bin/python -m pytest -p no:cacheprovider -q -n auto` in the worktree
  (xdist, ~2m48s; not the named-TMUX serial gate required for item 11).
- **5357 passed, 1 xfailed, exit 0** (previous milestone 5339 + 18 new tests). The
  repo's pytest config suppresses the summary line; the count comes from the progress chars.
- Source fingerprint re-checked after the gate: unchanged. `git diff --check` passed.
- Worktree `.orchestrator/` gained no files.
- Release preservation script re-run from the main checkout: exit 0. Main checkout
  status unchanged (five owner files + three untracked paths only).
