# Continue modelctl integration — after session 5 (gpt-6.1-sol switch, doctor/status evidence)

## Goal and read first

Continue the approved shared model policy/routing/adoption feature. Read, in order:
root `AGENTS.md`, `docs/plans/2026-09-30-modelctl-integration.md`,
`handoffs/2026-09-30-modelctl-implementation-milestone-next.md` (the "Built so far",
"Modelctl owner seam" and "Scope and execution guidance" sections still apply verbatim),
`handoffs/2026-09-30-modelctl-session2-next.md`, `handoffs/2026-09-30-modelctl-session3-next.md`,
`handoffs/2026-10-01-modelctl-session4-next.md`, then this file. Everything you need is in
these files.

Worktree (do not recreate it): `/home/dan/projects/maestro/.scratch/worktrees/modelctl-integration`,
branch `feat/modelctl-integration`, base/HEAD `a40cafc`. All work is **uncommitted**.
No commit, push or merge is authorized. The ledger and evidence directory is
E = `.superpowers/sdd/2026-09-30-modelctl-integration/`. Read the tail of E/progress.md;
the two "Session 5" entries describe this session in full.
Python: `/home/dan/projects/maestro/.venv/bin/python`. The worktree has no `.venv`.
Always `cd` into the worktree with an absolute path. A `cd` into the main checkout
changes the session's working directory.

Standing constraints (unchanged):
- No Context Gate work.
- No live model switch, deployment, daemon restart, paid inference, pilot continuation or HALT clearing.
- The modelctl owner patch is already approved and applied. Do not rerun its apply script or ask again.
- Missing-row registration stays disabled until the owner hardens registration (item 8).
- The main checkout has five uncommitted owner files (`maestro/backends/catalog.py`,
  `maestro/limits.py`, `maestro/templates/project.yaml.tmpl`, `tests/backends/test_catalog.py`,
  `tests/test_limits.py`) plus three untracked paths. They belong to someone else. Leave them
  alone and edit only the worktree copies.
- No live `maestro models` switch on this host. Do not edit DuetFlow's `project.yaml`
  (its gpt-6-sol pins stay and are reported).

## Done in session 5 (TDD; RED and GREEN logs in E)

1. **Item 7: gpt-6.1-sol replaces gpt-6-sol in the branch (done).**
   - The catalog gains the owner-verified `gpt-6.1-sol` profile, taken from the main
     checkout owner's diff: strong, cost 1.0, `ContextLimits(420_000, 32_000, 240_000)`.
     The owner's gpt-5.6-terra swap was not taken.
   - New `catalog.PIN_ONLY_MODELS = {"codex": {"gpt-6-sol"}}`. These models are verified and
     serve explicit pins, but are never a shared or default route:
     - `shipped_policy` skips them.
     - `catalog_defaults(None, …)` now goes through `shipped_policy()`. Without this, the
       router's model-id tie-break would pick `gpt-6-sol` over `gpt-6.1-sol`.
     - Explicit pins keep `gpt-6-sol` with its own verified profile.
   - `limits.DEFAULT_CODEX_REVIEW_MODEL_NAME = "GPT-6.1 Sol"`.
     `roles._verified_model_id` maps a table name to the catalog's id. `model_slug` alone
     produced `gpt-6-1-sol`.
   - `model_inventory.preview_project` reports `overrides_preserved` (the project's explicit
     entries) on every preview and adopt outcome. Status names are unchanged.
   - `model_lifecycle.preview_retire` now also retires owner-removed models that are reachable
     only through pins. Before this fix, an owner removal of `gpt-6-sol` would never have
     retired it once it left the shared routes.
   - Disposable end-to-end proof in `tests/test_model_sol_replacement.py` (7 tests), starting
     from a store bootstrapped by the pre-change release:
     - A detection event produces a proposal; review, approve and one publication follow.
     - The inheriting project adopts. Its legacy `role_config` and the production
       `orchestrator._graph_routing` (catalog, preferences and revision) both resolve
       `gpt-6.1-sol`.
     - The pinned project (DuetFlow's shape) keeps `gpt-6-sol` in both paths and reports
       `overrides_preserved`.
     - The active project is deferred on its old revision.
     - The owner's `removed: true` then retires `gpt-6-sol`, and the pins are reported and refused.
     - Without that signal, or with unreadable owner state, nothing is retired.
   - Logs: E/sol-red.log (5 failed for the right reasons; 2 preservation guards passed) →
     E/sol-green.log (7 passed).
   - Re-baselined tests (decided behaviour; no assertion was relaxed): test_model_detection
     (its event is now a future `gpt-6.2-sol`), test_model_init_inheritance TODAY,
     test_model_lifecycle Sol references, the test_model_switch_races table row and the
     backends/test_catalog graded table.
   - DESIGN.md §5 and the template example comment now match inheritance.
2. **Item 6 leftover: doctor/status evidence (done).**
   - New read-only `model_inventory.project_report(repo, store)` reports:
     - shared vs adopted revision and the adoption state;
     - whether the project is registered;
     - reader: supported or upgrade_required;
     - effective role pairs with their origin (project/shared/shipped);
     - `invalid_routes`;
     - `graph_routes`, from new `model_policy.graph_catalog_defaults`, the builder
       `orchestrator._graph_routing` now uses;
     - `open_proposals` (new `model_detection.open_proposals`).
   - New optional doctor check `cli._check_model_policy`. It warns and never fails doctor's
     exit code.
   - `maestro models status` JSON gains `open_proposals`. The text view lists each proposal
     with its review command.
   - Tests: `tests/test_model_status_report.py` (5). E/report-red.log (5 failed) →
     E/report-green.log (5 passed).

Related suites: E/sol-related.log 1588 passed, exit 0; E/report-related.log 1598 passed, exit 0.

Session 5 gate, `-n auto` full suite in the worktree:
- **5410 passed, 1 xfailed, exit 0, 160 s** (E/session5-full.log/.exit).
- Count method: only progress lines are counted
  (`grep -E '^[.sxFE]+( +\[ *[0-9]+%\])?$' log | tr -cd '.' | wc -c`). Earlier sessions
  counted every dot, which added the 6 dots in xdist's "bringing up nodes..." line; session
  4's 5404 is really 5398, and 5398 + 12 new tests = 5410.
- Tree fingerprint E/session5-gate-tree.sha was unchanged after the gate.
- `git diff --check` is clean.
- The preservation script (run from the main checkout) exited 0. The main checkout status
  is unchanged.
- The worktree `.orchestrator/` still holds only its 3 pre-existing files.
- `~/.maestro/models` does not exist.

## Remaining work

- **4 leftover: proposal notification transport.** Records stay `notification_pending`.
  `model_detection.consume(store, bridge, notify=None)` already has the optional `notify`
  seam. Wire a transport through the existing Maestro notification path. It must be
  retry-safe: never recreate the proposal, and clear `notification_pending` only after
  delivery. Telegram *selection UI* is out of scope; a notice is allowed only if an
  existing notifier supports it without new UI. If that is unclear, ask Dan.
- **8:** missing-row registration stays disabled pending owner hardening (unchanged).
- **10:** registered same-harness flow, crash recovery, lock contention, unsupported version,
  malformed config, retirement/rollback end-to-end (partly covered now by
  test_model_sol_replacement and test_model_lifecycle; check the gaps first), and genuine
  interactive disposable evidence.
- **11:** a fresh independent whole-branch review, then a full candidate gate in a named TMUX
  window with a source/input freeze and operating-pointer isolation. Then re-run
  `.venv/bin/python handoffs/2026-09-30-operator-intake-release/verify-preservation.py`
  from the main checkout.
- **Merge note (from session 4, extended):** the owner's main-checkout edits conflict with
  the worktree in `limits.py`, the template and `tests/backends/test_catalog.py`. On merge:
  - keep inheritance in the template;
  - set the judge default to latest Opus in the owner's scheme;
  - keep the Codex column with `GPT-6.1 Sol`;
  - keep `gpt-6-sol` in the catalog as pin-only (the owner deleted it, which would break
    DuetFlow's pins);
  - the owner's gpt-5.6-terra swap is theirs to reconcile.

Suggested next unit: 4 leftover, then 10, then 11.

## Verification to report back

For each unit, report:
- the RED log and the GREEN log;
- the focused command with its exact pass count and exit code;
- the related-suite count;
- whether the worktree `.orchestrator/` gained files (it must not);
- whether `~/.maestro/models` exists (it must not).

At the end, report the full-gate counts, exit code and duration (use the progress-line
count method above), the preservation script's exit code, and `git diff --check`.

## Suggested skills

executing-plans, test-driven-development, systematic-debugging (only if a failure's cause
is unknown), verification-before-completion, monitor-long-running-tasks (for the item 11
TMUX gate), close-session, handoff.
