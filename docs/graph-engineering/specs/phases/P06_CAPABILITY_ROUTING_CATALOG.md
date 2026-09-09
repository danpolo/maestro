# PHASE P6: ROUTE EVERY AI CALL BY CAPABILITY, MODEL & EFFORT (AGENT EXECUTION SPEC)

```
Phase ID:        PHASE-P06
Depends On:      P05
Output:          Capability catalog, joint resolve_agent() router, multi-window quota accounting
Target Files:
  - Create:  maestro/backends/catalog.py
  - Create:  maestro/backends/router.py
  - Create:  tests/backends/test_catalog.py
  - Create:  tests/backends/test_router.py
  - Create:  tests/test_routing.py
  - Create:  tests/test_no_dead_backend_surface.py
  - Modify:  maestro/adapters.py
  - Modify:  maestro/metrics.py
Verification:    .venv/bin/python -m pytest -q tests/backends/test_catalog.py tests/backends/test_router.py tests/test_routing.py tests/test_no_dead_backend_surface.py
```

---

## 1. OBJECTIVE & DELIVERABLES
Implement the Backend Capability Catalog and the centralized `resolve_agent()` function. Bind each agent node to a concrete backend, model ID, and native effort setting. Track multi-window quota accounting.

---

## 2. IMPLEMENTATION CHECKLIST
- [ ] **Capability Catalog**: Implement `maestro/backends/catalog.py` modeling backends across the 6 core capability dimensions.
- [ ] **Resolution Function (`resolve_agent`)**:
  - Filter hard constraints -> estimate task demand -> joint model/effort selection -> resource admission -> persist `AgentBinding`.
- [ ] **Effort Delivery Mapping (`[INV-A07]`)**:
  - Record `effort_delivery` as `flag`, `config_key`, or `unsupported`. Never invent non-existent flags.
- [ ] **Multi-Window Quota Tracking**:
  - Track 5-hour and weekly subscription allowances per account pool.
  - Record `QuotaObservation` with `observed_at`, `written_at`, `staleness_bound`, and `carried_forward`.
- [ ] **Outcome Statistics with Shape Keys (`[INV-A07]`)**:
  - Track empirical success against `(backend, model, effort, role_version, task_class, template_id, template_version, active_fragment_set)`.

---

## 3. ACCEPTANCE CRITERIA & VERIFICATION
- **Acceptance Condition**:
  - The same resolved `AgentBinding` is used for dispatch, timeouts, token accounting, and status.
  - A routing failure produces an explicit typed wait/block, never silent fallback to an unapproved model or billing tier.
  - Quota exhaustion pauses pool tasks without killing healthy running workers.
- **Execution Command**:
  ```bash
  .venv/bin/python -m pytest -q tests/backends/test_catalog.py tests/backends/test_router.py tests/test_routing.py tests/test_no_dead_backend_surface.py
  ```\n