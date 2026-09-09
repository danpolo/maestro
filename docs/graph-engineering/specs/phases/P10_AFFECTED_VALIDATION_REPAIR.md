# PHASE P10: ENABLE QUALIFIED AFFECTED VALIDATION & LOCAL PLAN REPAIR (AGENT EXECUTION SPEC)

```
Phase ID:        PHASE-P10
Depends On:      P02, P04 (P09 supplies optional structural impact facts)
Output:          Safe incremental verification caching, evidence-driven DAG plan repair
Target Files:
  - Extend:  maestro/validation.py
  - Extend:  maestro/taskgraph.py
  - Extend:  maestro/workflows/compiler.py
  - Create:  tests/test_affected_validation.py
  - Create:  tests/workflows/test_plan_repair.py
Verification:    .venv/bin/python -m pytest -q tests/test_affected_validation.py tests/workflows/test_plan_repair.py
```

---

## 1. OBJECTIVE & DELIVERABLES
Graduate affected-check validation from shadow mode to authoritative caching per Experiment E3. Implement local workflow plan repair: patch active DAGs for discovered prerequisites or partial failures without losing historical evidence.

---

## 2. IMPLEMENTATION CHECKLIST
- [ ] **Affected Check Validation**:
  - Select affected checks via exact input file dependencies.
  - Changes to shared config, fixtures, or lockfiles expand to full suite.
- [ ] **Local Plan Repair Protocol**:
  - Apply revision-checked patches to active `WorkflowRevision` (create revision `N + 1`).
  - Carry forward valid completed nodes and artifacts.
  - Cancel active consumers before they integrate obsolete outputs.
  - Cumulative attempt and repair counters MUST NOT be reset (`[INV-08]`).

---

## 3. ACCEPTANCE CRITERIA & VERIFICATION
- **Acceptance Condition**:
  - Internal behavior change with unchanged public signature still invalidates verification evidence.
  - Unknown dependency coverage falls back to full required suite.
  - Discovered prerequisite creating a cycle is detected and blocked.
- **Execution Command**:
  ```bash
  .venv/bin/python -m pytest -q tests/test_affected_validation.py tests/workflows/test_plan_repair.py
  ```\n