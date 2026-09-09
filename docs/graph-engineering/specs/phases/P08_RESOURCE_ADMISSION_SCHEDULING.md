# PHASE P8: DEEPEN RESOURCE ADMISSION & SCHEDULING (AGENT EXECUTION SPEC)

```
Phase ID:        PHASE-P08
Depends On:      P03, P06 (candidate scheduling after P07)
Output:          Greedy admission scheduler, multi-project host ledger (~/.maestro/)
Target Files:
  - Create:  maestro/scheduling.py
  - Create:  maestro/control/resources.py
  - Create:  tests/test_scheduling.py
  - Create:  tests/control/test_resources.py
  - Adapt:   maestro/orchestrator.py
Verification:    .venv/bin/python -m pytest -q tests/test_scheduling.py tests/control/test_resources.py tests/test_backend_exhaustion.py tests/test_quota_thresholds.py
```

---

## 1. OBJECTIVE & DELIVERABLES
Separate roadmap readiness and workflow readiness from physical resource admission. Implement explainable greedy scheduling and a host-local reservation ledger (`~/.maestro/`) for multi-project concurrency.

---

## 2. IMPLEMENTATION CHECKLIST
- [ ] **Admission Engine**: Implement `maestro/scheduling.py` managing CPU, RAM, GPU VRAM, agent slots, worktree writer leases, and target branch locks.
- [ ] **Two Ready Frontiers**:
  - Roadmap frontier: prerequisite tasks completed, no hold.
  - Workflow frontier: predecessor nodes completed, input artifacts bound.
- [ ] **Greedy Ranking Algorithm**:
  - Rank eligible nodes: explicit operator priority -> aging / starvation prevention -> downstream unblocking leverage -> resource fit.
- [ ] **Host Reservation Ledger**:
  - Implement `maestro/control/resources.py` managing `~/.maestro/reservations.json`. Reserve before launch; reconcile on heartbeat/restart; release on process death.

---

## 3. ACCEPTANCE CRITERIA & VERIFICATION
- **Acceptance Condition**:
  - A saturated inference pool cannot delay a ready deterministic job.
  - Two projects sharing an account pool share pressure without corrupting state.
  - Leases never oversubscribe under concurrent claim stress tests.
  - Continuous eligibility aging eventually admits starved tasks.
- **Execution Command**:
  ```bash
  .venv/bin/python -m pytest -q tests/test_scheduling.py tests/control/test_resources.py tests/test_backend_exhaustion.py tests/test_quota_thresholds.py
  ```\n