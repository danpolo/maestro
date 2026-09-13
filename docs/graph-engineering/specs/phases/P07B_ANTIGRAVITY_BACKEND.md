# PHASE P07B: INTEGRATE ANTIGRAVITY (AGY) AS A LIGHT / BALANCED BACKEND (AGENT EXECUTION SPEC)

```
Phase ID:        PHASE-P07B
Depends On:      P06 (capability catalog, router), P07 (billing objectives, hybrid policy)
Output:          agy driver, catalog row grading agy's models light / balanced, verified thirdparty row
Target Files:
  - Create:  maestro/backends/antigravity.py
  - Extend:  maestro/backends/registry.py
  - Extend:  maestro/backends/catalog.py
  - Extend:  maestro/thirdparty.json
  - Extend:  maestro/templates/roles/
  - Create:  tests/backends/test_antigravity.py
  - Extend:  tests/test_routing.py
Verification:    .venv/bin/python -m pytest -q tests/backends/test_antigravity.py tests/test_routing.py tests/backends/test_catalog.py tests/test_thirdparty.py
```

---

## 1. OBJECTIVE & DELIVERABLES
Make Antigravity (`agy`) a third routable backend beside claude and codex, deliberately graded below them so the router hands it simpler work. The routing machinery already exists (P06/P07): each model carries a strength rung (`light` / `balanced` / `strong`) and a task only binds to a model whose rung meets its demand floor. This phase adds the driver and grades agy's models; it does not change how strength or effort are chosen.

**Grading rule (Dan, 2026-09-13):**
- **Gemini 3.8 Flash → `balanced`.** Only this model — no other Flash variant.
- **Every other agy model → `light`**, including other Flash models and the `3p` pool models.
- **No agy model is `strong`.**
- Strength stays per model and independent of effort. Effort is still chosen by the existing demand estimate, never coupled to the model's rung.

---

## 2. IMPLEMENTATION CHECKLIST
- [ ] **Driver** `maestro/backends/antigravity.py` implementing `backends.base` (`capabilities`, `launch`, `resume` via `--conversation`, `complete`, `parse_exit`, `usage`), matching `claude.py` / `codex.py`. Headless only; no interactive status-line render.
- [ ] **Registry**: add a `DriverRef` named `antigravity` (binary `agy`) in `maestro/backends/registry.py`. `DEFAULT_BACKEND` stays `claude`.
- [ ] **Catalog row**: `backend_id` `antigravity_cli`, `billing_category` `subscription` (so under `paid_efficiency` it is interchangeable with claude/codex and the router's alternate walk can hand its work to them), `usage_pool_id`s from the observed pools (`gemini-5h`, `gemini-weekly`, `3p-5h`, `3p-weekly`).
- [ ] **Model profiles**: record the exact model id the installed CLI reports for Gemini 3.8 Flash and set `strength="balanced"` for that id only. Every other listed agy model gets an explicit `strength="light"` profile — do not rely on the catalog's middle-rung default (`CapabilityEntry.profile`), which would silently make new agy models `balanced`. A test must fail if a newly listed agy model has no explicit profile.
- [ ] **Effort**: exercise `agy --effort` and record its native values in `EffortControls`. Canonical `low/medium/high` reach agy only through a native match or a measured mapping; otherwise `effort_support="unsupported"` (B06 — no cross-vendor label guessing).
- [ ] **Quota**: re-capture the four-pool `quota` payload from the installed version headlessly, or record that it is unobtainable and set `telemetry_quality` accordingly (the B02 open question).
- [ ] **Light-demand role**: shipped roles only state `balanced` / `strong`, and the demand estimate only yields `light` for low-risk, ≤1-file-churn tasks. Confirm such tasks reach `light` routes end to end, so agy's light models actually receive work.
- [ ] **Thirdparty row**: re-verify agy against the installed version, set `maestro_driver: true`, move `scope` from `optional` to `runtime`, close the resolved `open_questions`.
- [ ] **Limits table**: `limits.AGENT_TABLE_DIRS` already reads `~/.gemini/model_context_limits.md`; confirm the path with Dan and add Gemini 3.8 Flash's row.

---

## 3. ACCEPTANCE CRITERIA & VERIFICATION
- **Acceptance Condition**:
  - A `light` task can bind to an agy light model; a `balanced` task can bind to Gemini 3.8 Flash and to no other agy model.
  - A `strong` task never binds to agy.
  - When agy's pool is exhausted, a `balanced` task binds to claude or codex in the same resolution instead of waiting; it waits only when every qualifying route is full.
  - A newly listed agy model without an explicit profile fails the catalog test rather than defaulting to `balanced`.
  - No effort value is sent to agy that its CLI never showed.
  - `maestro doctor --thirdparty` passes with agy at `runtime` scope.
- **Execution Command**:
  ```bash
  .venv/bin/python -m pytest -q tests/backends/test_antigravity.py tests/test_routing.py tests/backends/test_catalog.py tests/test_thirdparty.py
  ```
