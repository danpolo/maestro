# MAESTRO SCHEDULING, RESOURCE ADMISSION & OPERATOR CONTROL (AGENT SPECIFICATION)

```
Document ID:     MGES-SCHEDULING-OPS-V2
Target Audience: Autonomous AI Coding Agents / LLM Implementation Engines
Domain:          Frontier Evaluation, Resource Admission, Multi-Project Ledger, Watchdog
Base Revision:   Revision 2 (2026-09-08)
Status:          Authoritative Scheduling & Operations Specification
```

---

## 1. THE TWO READY FRONTIERS

Maestro strictly separates logical readiness from physical resource admission:

1. **Roadmap Readiness Frontier**:
   - Evaluated over `TaskSpec` nodes in the roadmap DAG.
   - A task is roadmap-ready IF AND ONLY IF all `depends_on` tasks have transitioned to `accepted`, no active `hold` exists, and project policy permits dispatch.
2. **Workflow Readiness Frontier**:
   - Evaluated over `NodeSpec` nodes in active `WorkflowRevision` DAGs.
   - A node is workflow-ready IF AND ONLY IF all incoming activated dependency edges have completed successfully and all required input artifacts are bound.
3. **Resource Admission Separation**:
   - An eligible node on the workflow readiness frontier is NOT automatically dispatched.
   - It MUST pass physical resource admission (CPU, memory, GPU VRAM, inference pool capacity, worktree writer lease availability).
   - **Negative Invariant**: A saturated agent/inference pool MUST NOT block or delay a ready deterministic job (e.g. test runner, linter, git operation). A full agent pool must never turn an unavailable model into a fake dependency edge.

---

## 2. RESOURCE ADMISSION MODEL & CONCURRENCY DISCIPLINE

The admission engine manages six distinct resource pools:

| Resource Pool | Management Mechanism | Scope |
| :--- | :--- | :--- |
| **Worktree Writer Leases** | Exclusive single-writer lock (`writer_leases` in SQLite). | Per task worktree (`impl/<task>`). |
| **Target Integration Lease** | Exclusive single-writer lock (`target_branch`). | Project-wide (`project.main_branch`). |
| **System CPU & RAM** | Bounded concurrency slots based on core count and memory pressure. | Host-local. |
| **GPU / VRAM** | Exclusive or fractional memory reservation for local inference/models. | Host-local. |
| **Agent CLI Concurrency** | Max concurrent subprocesses per backend CLI (e.g. max 2 Claude sessions). | Host-local. |
| **Subscription Quota Pools** | Multi-window rate limit and usage tracking (5-hour, weekly). | Shared account / pool ID. |

### 2.1 Scheduling Priority & Heuristics
The scheduler implements an explainable, deterministic greedy ranking:
1. Operator explicit priority (CLI/Telegram overrides).
2. Aging and starvation prevention (continuously eligible tasks gain priority).
3. Downstream unblocking leverage (tasks with high out-degree of blocked dependents).
4. Resource fit and predicted conflict minimization.
5. Learned schedulers (HEFT, CP-SAT) are deferred until empirical metrics justify them under Experiment E5.

---

## 3. MULTI-PROJECT HOST ADMISSION LEDGER (`~/.maestro/`)

When multiple Maestro projects run concurrently on a single host sharing common accounts or subscription pools:

1. **Shared Reservation Ledger**: Stored at `~/.maestro/reservations.json` (or host-local SQLite `~/.maestro/host_admission.sqlite3`).
2. **Reservation Protocol**:
   - Before launching an agent invocation, the project controller acquires a short-lived reservation lease for the account pool.
   - The reservation records `project_id`, `run_id`, `pool_id`, `writer_pid`, and `lease_expiry`.
   - The lease is refreshed via worker heartbeat.
   - Released upon verified worker process termination.
3. **Recovery & Reconciliation**: On startup or during crash recovery, dead PID reservations are purged automatically.
4. **Negative Invariant**: The host reservation ledger is purely coordination metadata; it is NOT a distributed transaction coordinator and NEVER stores task or domain state.

---

## 4. OPERATOR INTERFACES & WATCHDOG INTEGRATION

### 4.1 CLI and Telegram Command Dispatch
All operator interactions (CLI commands, Telegram messages) are enqueued into the controller's `command_inbox` table:
- `/pause`, `/resume`, `/halt`
- `override_backend --task <id> --backend <name>`
- `approve_step --task <id> --action <id>`
- `reject_step --task <id> --action <id>`
- `self_fix --task <id>`, `redo --task <id>`
- `unpark_task --task <id>`

**Core Independence Invariant**: The system MUST be fully functional via the standard CLI without Telegram or network services configured.

### 4.2 Watchdog Responsibilities & Boundaries
- The watchdog process (`maestro/watchdog.py`) monitors controller process liveness.
- It MUST NOT directly modify task states or bypass controller authority.
- If the controller process dies:
  1. Watchdog verifies death via `/proc/<pid>`.
  2. Launches a single restart of the controller.
  3. Controller performs quiescent recovery and worker PID adoption.
- **The HALT Sentinel**: A persistent filesystem sentinel `.orchestrator/HALT` signals immediate, unconditional cessation of dispatch. The watchdog and controller MUST NOT clear or ignore this sentinel.

### 4.3 Provenance & Operator Observability
The controller derives clean, explainable operator projections:
- `maestro workflow show <task_id> --html`: Emits a static, self-contained interactive timeline and graph view (zero external telemetry service dependencies).
- `maestro explain <task_id>`: Emits causal trace detailing criteria, selected backend, binding rationale, verification evidence receipts, and acceptance decisions.
