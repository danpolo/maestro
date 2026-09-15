# Handoff — P07B: Antigravity (agy) as a light / balanced backend (session 4)

Paste this into a fresh Claude Code session in `/home/dan/projects/maestro`.

---

## Goal

Finish Phase P07B. Session 3 landed plan steps 1–3 in commit `83f880c`: the driver, the registry row, and agy-scoped limits. **This session does steps 4–7:** the catalog, the thirdparty row, the routing tests, and the STATE close-out. Every design point is already settled, so don't re-derive it.

**Read first, in this order:**

1. `handoffs/2026-09-15-graph-p07b-antigravity-backend-s3.md`. Its **New decisions and facts** #1, #2, #5, #6 and #7 and **Implementation plan** steps 4–7 are the spec for this session. Decisions #3, #4, #8, #9, #10 and #11 are already implemented.
2. `docs/graph-engineering/specs/phases/P07B_ANTIGRAVITY_BACKEND.md` §3 (acceptance).
3. `artifacts/graph-engineering/p07b-agy-1.2.2-probe.json`: the evidence for the thirdparty row and the 14 model ids.
4. Run `.venv/bin/python scripts/next_graph_prompt.py` for the §4A threads and the §6 close-out rules.

## What session 3 established (don't re-check)

- **Done in `83f880c`:**
  - `DriverRef.in_default_chain`. The antigravity row sets it `False`, and `roles.default_chain()` returns `('claude', 'codex')`.
  - `thirdparty.required_subjects()` and `test_every_registered_backend_has_a_verification_row` key on `ref.binary`.
  - The `limits._lookup` variant fold. All 14 probe ids plus `Gemini 3.8 Flash (High)` resolve to their `agy/` row, and a backend-less lookup returns `None`. The tests are in `tests/test_limits.py`.
  - `switch.context_crossed(..., backend=)`, plus the orchestrator and doctor call sites.
  - `base.TokenSplit`, `Usage.token_split` and `Usage.cli_version`.
  - `maestro/backends/antigravity.py`, with 45 tests in `tests/backends/test_antigravity.py`.
- **The stream-json discriminator is `"event"`.** The payload sits under the event's own name (`{"event":"step_update","step_update":{...}}`). `init` carries `conversation_id` at the top level and `init.model` nested. The driver also tolerates `"type"`.
- **The driver's quota `reset_at`** comes from the earliest future reset of an exhausted Gemini pool in the status-line dump, else now + 1h. It does not use `quota._resolve_limit_reset`, which reads claude's `usage.json`.
- **Test fallout from the registry change is already fixed.** `tests/test_roles.py` now derives `OTHER` from `roles.default_chain()`, and so do its exhausted/unavailable loops.
- **pytest's summary line is suppressed in this repo's config.** `tail` shows only dots. To get pass counts, use `--junit-xml=<scratch>/r.xml` and read `tests`/`failures` off the root `testsuite`.
- **`maestro/backends/catalog.py` is 48 KB.** Don't cat it. Grep for `def catalog_from_register`, `class ModelProfile`, `class ContextLimits`, `class CapabilityEntry` and `def effort_controls_from_findings`, then read only those bodies.
- `tests/test_routing.py::shipped_catalog` (L31–71) builds the catalog via `cat.catalog_from_register(thirdparty.load_register(), defaults={claude:…, codex:…})`. Merge `cat.ANTIGRAVITY_CATALOG_DEFAULTS` into that `defaults` dict. Existing routing tests: `test_no_effort_flag_is_invented_...` (L121), `test_an_exhausted_pool_does_not_block_a_healthy_one` (L286), and `test_a_strong_only_role_is_never_given_a_balanced_model` (L111).
- `maestro doctor --thirdparty` has **not** been run yet. It needs the step 5 row.

## Remaining plan (from s3, unchanged)

4. **Catalog.** Add a `backend_id` override in `catalog_from_register` (`base.get("backend_id") or subject`). Add `ANTIGRAVITY_CATALOG_DEFAULTS = {"agy": {...}}` per s3 decision #6, with explicit `ModelProfile`s for all 14 ids and only `gemini-3.8-flash-high` graded `balanced`. Add a catalog test that fails for any listed agy id without an explicit profile and that matches the probe's id list.
5. **Thirdparty `agy` row.** Set `scope: runtime`, `maestro_driver: true`, `verified_version: "1.2.3"` (exact `agy --version`). Add `findings.effort_control` `{kind: flag, observed: …}` with **no** `native_values`. Record the resume and compaction facts, prune `open_questions`, and mark quota as push-only. `verified_by` must say that the 1.2.3 re-check (`agy models` plus a client-side-failing `--effort`) cost no quota.
6. **Routing tests** in `tests/test_routing.py`:
   - With an agy-only catalog, balanced work binds only `gemini-3.8-flash-high`.
   - Strong work never binds agy.
   - With the `antigravity-gemini` pool paused, balanced work goes to claude/codex.
   - The light path goes through a **test-local** role (`min_reasoning_strength: light`, `risk="low"`, `churn_files<=1`).
   - An agy binding has `plan.effort_args == ()`.
7. **STATE.yaml** (targeted edits only):
   - Mark P07B complete per §6: `current_phase: P12A`, `in_progress_details: null`.
   - Close `_p08_open_threads` #5 (the `agy/` prefix, owner P07B) and #6 (no agy driver) with evidence.
   - Add `_p07b_open_threads`:
     - 3p pool misattribution → P12A
     - catalog `antigravity_cli` / register `agy` / registry `antigravity` backend_id mapping → P12A
     - no shipped role yields light → `accepted`
     - `--sandbox`/permission mode for headless edits never exercised → P12A

## Parallel-session coordination

- Branch `feat/graph-engineering-foundation`. It has no upstream, so `git pull` fails; run `git log --oneline -3` instead. HEAD should be the commit carrying this file, on top of `83f880c`.
- Leave `docs/graph-engineering/specs/phases/P12A_*` alone. Edit STATE.yaml with targeted replacements only.

## In scope / Out of scope

- **In:** plan steps 4–7, `_p08_open_threads` #5/#6, and closing P07B.
- **Out:** the Context Gate, JSONL logs, compaction control, effort in `LaunchSpec`, an agy quota pull path, shipped role floor changes, an agy doctor model-id probe, and any rework of steps 1–3 unless a step 4–6 test exposes a real defect.

## Verification (report the numbers)

```bash
.venv/bin/python -m pytest -q tests/backends/test_antigravity.py tests/test_routing.py tests/backends/test_catalog.py tests/test_thirdparty.py tests/test_limits.py   # s3 baseline: 193 passed
.venv/bin/python -m pytest -q tests/test_no_name_branching.py tests/backends tests/test_switch.py tests/test_orchestrator_switch_hooks.py tests/test_roles.py tests/workflows/test_policy.py tests/test_per_session_usage.py   # s3 baseline: 798 passed (+1 cli test = 799)
.venv/bin/maestro doctor --thirdparty   # agy verified at 1.2.3, no drift
```

Report the pass counts for both runs (via junit), doctor's agy line, the balanced/strong/paused-pool/light routing results, and the final STATE.yaml `current_phase`.
