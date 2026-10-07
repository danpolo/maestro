# Continue modelctl integration — after session 3 (rollback, retirement, proposal review, publish race)

## Goal and read first

Continue the approved shared model policy/routing/adoption feature. Read, in order:
root `AGENTS.md`, `docs/plans/2026-09-30-modelctl-integration.md`,
`handoffs/2026-09-30-modelctl-implementation-milestone-next.md` (its sections "Built so
far", "Modelctl owner seam" and "Scope and execution guidance" still apply verbatim),
`handoffs/2026-09-30-modelctl-session2-next.md` (session 2: task freeze, pins,
portability), then this file. Everything needed is here or in those files.

Worktree (do not recreate): `/home/dan/projects/maestro/.scratch/worktrees/modelctl-integration`,
branch `feat/modelctl-integration`, base/HEAD `a40cafc`. All work is **uncommitted**
(no commit/push/merge authorized). Ledger/evidence dir E =
`.superpowers/sdd/2026-09-30-modelctl-integration/`. Read the tail of E/progress.md.
Python: `/home/dan/projects/maestro/.venv/bin/python` (the worktree has no `.venv`).

Standing constraints (unchanged): no Context Gate work; no live model switch, deployment,
daemon restart, paid inference, pilot continuation, HALT clearing; the modelctl owner
patch is already approved and applied — do not rerun its apply script or ask again;
missing-row registration stays disabled until owner hardening (item 8). The main
checkout's five uncommitted owner files (`maestro/backends/catalog.py`, `maestro/limits.py`,
`maestro/templates/project.yaml.tmpl`, `tests/backends/test_catalog.py`, `tests/test_limits.py`)
and untracked artifacts belong to someone else — leave them alone.

## Done in session 3 (TDD; RED and GREEN logs in E)

1. **Item 4 — rollback, retirement, status, proposal review (done).**
   - New `maestro/model_lifecycle.py`: shared `preview_base` / `verify_unchanged` /
     `publish_and_adopt` (now also used by `model_replacement.apply_preview`),
     `preview_rollback(store, target=None)` (default target = current policy's
     `source.previous_policy`; the shipped policy is reachable by its digest via new
     `PolicyStore.load_any`), `preview_retire(store, bridge, backend)`,
     `apply_policy_preview(preview, store, *, confirmed, bridge=None)`.
   - `model_policy`: `role_references`, `retire_routes` (a route still preferred by a
     shared role is refused — "replace it first"; preview lists it under
     `replacement_required`), `rollback_policy` (prior policy published as a **new**
     revision, `source.kind == 'rollback'`, retirement since then wins, refuses a target
     whose roles prefer a now-retired route).
   - Retirement signal = owner state `providers.<p>.models.<id>.removed is True` only.
     New bridge op/method `ModelctlBridge.retired(backend)` reads `state.json` directly
     (owner `State()` would move a corrupt file aside); any failure raises
     `PolicyError(... not treated as retirement)`. The removal-state hash is rechecked
     before publication.
   - `model_inventory.adoption_status(store)`: per registered project
     `current` / `behind` / `not_adopted` / `unreadable`; `maestro models status --json`
     now includes `adoption` and `retired`. Retry reconciliation = `maestro models adopt`.
   - `model_detection`: `reconcile_proposals` (an `approved` record becomes `applied` only
     if its content-addressed approved revision exists in the store, else back to
     `pending_approval` with `last_error`), `review_proposal` (recomputes related
     candidates on the current policy, `stale_policy` flag, explicit choice when several,
     only related eligible routes; `registration_required` recorded with suggested limits
     and kept open, never selected), `decide_proposal` (reject → `rejected`; approve →
     `approved` → apply → `applied` + `applied_revision`; failure → `pending_approval`
     + `last_error`).
   - CLI: `maestro models review --proposal <id>`, `rollback [--revision R]`,
     `retire --backend B`; all need a typed `yes`. `proposals` now reconciles first.
   - Tests: `tests/test_model_lifecycle.py` (20): E/lifecycle-red.log (20 failed) →
     E/lifecycle-green.log (20 passed). Real-owner `retired` test in
     `tests/test_modelctl_bridge.py` (5 passed) was written after the implementation,
     not RED-first.
2. **Item 5 — partial.** `PolicyStore.publish(..., check=)` re-runs `verify_unchanged`
   under the policy lock (registration takes the same lock), closing the
   check-then-publish race. `preview_project` reports `invalid_routes` via new
   `model_policy.invalid_routes` (`runtime_roles` refactored onto it): explicit/effective
   role models the new policy cannot serve — reported, not blocking.
   Tests: `tests/test_model_switch_races.py` (2): E/races-{red,green}.log.

Related suites (model*, modelctl bridge, roles, one_shot, agentcall, no_name_branching,
orchestrator_*, workflows, control, backends): **1314 passed, exit 0** (E/item5-related.log).
Empty-HOME portability (model*, bridge, roles, agentcall, one_shot): **216 passed, exit 0**
(E/portability-empty-home-s3.log). Worktree `.orchestrator/` gained no files.

Full-suite gate for this session: `.venv/bin/python -m pytest -p no:cacheprovider -q -n auto`
in the worktree (xdist, 132 s; not the named-TMUX serial gate required for item 11):
**5386 passed (progress-dot count), 1 xfailed, exit 0**. E/session3-full.log / .exit;
source fingerprint E/session3-gate-source.sha re-checked after the gate: unchanged.
`git diff --check` passed. Release preservation script from the main checkout: exit 0;
main checkout status unchanged (five owner files + three untracked paths only).

## Remaining work (numbering from the milestone handoff)

- **4 leftover:** proposal notification transport still unfinished (records stay
  `notification_pending`); doctor view of adoption/proposals is item 6.
- **5 leftover:** `adopt` still writes the `.orchestrator/model_policy.json` pointer file
  inside the controller transaction, not an event-backed adopted-revision record (check
  whether the control store has an event append usable in that transaction; keep one
  source of truth). Source-reader compatibility proof before broad installed-version claims
  (`model_inventory.supports_reader`).
- **6** Future init templates: explicit inheritance instead of role pins (do not edit
  existing project configs; the template is also a main-checkout owner-edited file — edit
  only the worktree copy); wire init/register; bounded-call hardcoded defaults
  (`selfheal.diagnose.JUDGE_MODEL` is documentation-only, not passed); public CLI/doctor
  evidence including `adoption_status` and open proposals.
- **7** Unknown destination native profile support, or keep the truthful refusal
  (`catalog_defaults` refuses IDs lacking a shipped verified native profile, so a detection
  proposal for e.g. `gpt-6.1-sol` previews as an error today).
- **8** Missing registration stays disabled pending owner hardening (unchanged).
- **10** Registered same-harness flow, crash recovery, lock contention, unsupported
  version, malformed config, retirement/rollback end-to-end, genuine interactive disposable
  evidence.
- **11** Fresh independent whole-branch review, then a full candidate gate in a named
  TMUX window with source/input freeze and operating-pointer isolation; re-run
  `.venv/bin/python handoffs/2026-09-30-operator-intake-release/verify-preservation.py`
  from the main checkout.

Suggested next unit: finish item 5 leftovers, then 6, 7 (decide: truthful refusal is
acceptable), 10, 11.

## Verification to report back

Per unit: RED log, GREEN log, focused command + exact pass count/exit, related-suite
count, and whether `.orchestrator/` in the worktree gained files (it must not; it
pre-contains `journal.ndjson`, `model_limits.json`, `usage.json`). At the end: full gate
counts/exit/duration, preservation script exit, `git diff --check`.
The repo's pytest config suppresses the summary line; count progress dots
(`grep -o '[.]' log | wc -l`).
