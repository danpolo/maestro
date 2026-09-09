# PHASE P3: EXECUTE EXISTING LANES AS DURABLE WORKFLOW NODES (AGENT EXECUTION SPEC)

```
Phase ID:        PHASE-P03
Depends On:      P01, P02
Output:          Workflow models, compiler, runner, TMUX worker supervisor, crash recovery
Target Files:
  - Create:  maestro/workflows/models.py
  - Create:  maestro/workflows/compiler.py
  - Create:  maestro/workflows/runner.py
  - Create:  maestro/workflows/handlers.py
  - Create:  maestro/control/reconcile.py
  - Create:  tests/workflows/test_compatibility.py
  - Create:  tests/workflows/test_runner.py
  - Create:  tests/control/test_recovery.py
  - Adapt:   maestro/orchestrator.py
  - Adapt:   maestro/implementer.py
  - Adapt:   maestro/switch.py
  - Adapt:   maestro/parking.py
  - Adapt:   maestro/prep_actions.py
  - Adapt:   maestro/worktree.py
  - Adapt:   maestro/watchdog.py
Verification:    .venv/bin/python -m pytest -q tests/workflows tests/control/test_recovery.py tests/test_switch.py
```

---

## 1. OBJECTIVE & DELIVERABLES
Compile existing execution lanes (script, implementer, manual, async) into explicit DAGs composed of typed nodes (`NodeSpec`). Execute blocking tasks inside supervised TMUX workers. Implement the controlled-operation protocol, crash recovery, and lease reconciliation.

---

## 2. IMPLEMENTATION CHECKLIST
- [ ] **Workflow DAG Models**: Implement `WorkflowRevision`, `NodeSpec`, and `EdgeSpec` in `maestro/workflows/models.py`.
- [ ] **Supervised TMUX Workers**: Move blocking agent/script execution out of the controller loop into named TMUX windows (`tmux new-window -t agents -n ...`).
- [ ] **6-Step Controlled Operation Protocol**:
  - Enforce pre-validation -> SQLite claim + intent -> dispatch -> PID bind -> execute -> receipt commit.
- [ ] **Guarded Edges & Joins**: Support `on_success` / `on_failure` edge guards, AND joins, and skipped node state propagation.
- [ ] **Lease Reconciliation (`reconcile.py`)**:
  - On tick / restart: verify `writer_leases.writer_pid`. Reconcile dead workers vs surviving workers. Adopt surviving processes.
- [ ] **Sentinels & Compatibility**: Map legacy sentinel files (`DONE`, `FAILED`, `PAUSED`, `INCOMPLETE`, `SWITCH`) via compatibility facade into typed result envelopes.

---

## 3. ACCEPTANCE CRITERIA & VERIFICATION
- **Acceptance Condition**:
  - Crash after launch but before receipt cannot create a second concurrent writer.
  - Sibling branches survive independent branch failure.
  - Expired lease with a confirmed running PID blocks replacement writer.
  - Manual prep tasks cannot graduate without explicit human action receipt.
- **Execution Command**:
  ```bash
  .venv/bin/python -m pytest -q tests/workflows tests/control/test_recovery.py tests/test_switch.py
  ```\n