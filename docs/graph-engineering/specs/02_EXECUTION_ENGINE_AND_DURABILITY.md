# MAESTRO EXECUTION ENGINE & DURABILITY PROTOCOL (AGENT SPECIFICATION)

```
Document ID:     MGES-EXEC-DURABILITY-V2
Target Audience: Autonomous AI Coding Agents / LLM Implementation Engines
Domain:          Controller Event Loop, State Machines, Process Leases, Fencing
Base Revision:   Revision 2 (2026-09-08)
Status:          Authoritative Runtime Specification
```

---

## 1. CONTROLLER ARCHITECTURE & THE RECONCILIATION LOOP

Maestro operates as an embedded single-writer control plane. The controller maintains an asynchronous, non-blocking tick loop. All long-running scripts, verifications, git operations, and agent CLI processes execute out-of-process in supervised worker processes inside named TMUX windows.

### 1.1 The Canonical `tick(project)` Algorithm

```python
def tick(project: ProjectContext) -> None:
    """
    Single non-blocking execution cycle of the Maestro Controller.
    MUST execute atomically or with explicit savepoints.
    """
    # 1. Ingest idempotent external commands and worker completion envelopes
    ingest_idempotent_commands_and_results(project)

    # 2. Reconcile active writer leases, dead processes, and uncertain operations
    reconcile_claimed_running_and_uncertain_operations(project)

    # 3. Apply validated graph or policy commands (roadmap mutations, plan repairs)
    apply_validated_graph_or_policy_commands(project)

    # 4. Advance DAG frontiers and dispatch ready work
    for node in ready_frontier(project):
        inputs = bind_current_artifacts(node)
        route = resolve_agent_if_needed(node, inputs)
        claim = claim_resources_and_record_intent(node, inputs, route)
        if claim:
            dispatch_recorded_operation(claim)

    # 5. Export backwards-compatible state projections and pending human requests
    export_state_status_and_pending_operator_requests(project)
```

---

## 2. FORMAL STATE MACHINES & INVARIANTS

### 2.1 Task Run Lifecycle
```
[planned] ──► [active] ──► [accepted] (Terminal Success)
                │    ▲
                ▼    │
             [waiting] ──► [blocked] ──► [canceled] (Terminal Abort)
```
- `planned`: Task defined, waiting for hard roadmap prerequisites.
- `active`: Prerequisites satisfied; workflow compiled and executing.
- `waiting`: Paused on external dependency: human decision, async job, or quota reset.
- `blocked`: Deadlock, missing required tool/backend, or unresolvable validation failure.
- `accepted`: Terminal success; code integrated to target branch and acceptance committed.
- `canceled`: Terminal abort by operator command or task supersession.

### 2.2 Node Lifecycle
```
[pending] ──► [ready] ──► [claimed] ──► [running] ──► [succeeded] (Terminal Node Success)
                │                          │
                ▼                          ▼
            [skipped]             [failed] / [uncertain] ──► [retry / repair]
```
- `pending`: Predecessor nodes incomplete.
- `ready`: All incoming edges satisfied, input artifacts bound and validated.
- `claimed`: Resource lease and fencing token epoch committed to SQLite; process not yet launched.
- `running`: Process launched, writer PID recorded, periodic heartbeat updating.
- `succeeded`: `NodeResult` envelope validated against output schema; output artifacts committed.
- `failed`: Handler exited non-zero or output schema validation failed.
- `skipped`: Evaluated activation predicate returned false; records skip rationale.
- `uncertain`: Crash, timeout, or lost worker during non-idempotent side effect; requires reconciliation probe.

---

## 3. CONTROLLED-OPERATION PROTOCOL (6-STEP PROTOCOL)

Before an external side effect (agent invocation, test suite, git commit/push, build) occurs, the controller and worker MUST execute the following 6-step transactional protocol:

1. **Pre-Validation**: Validate node inputs, schema, tool permissions, and resource availability.
2. **Transactional Claim & Intent**:
   - In a single SQLite transaction:
     - Increment resource fencing epoch (`lease_epoch`).
     - Insert row into `writer_leases` with `writer_pid` lease.
     - Insert row into `operation_intents` with status `pending`.
     - Emit `NodeAttemptClaimed` event.
3. **Out-of-Transaction Dispatch**:
   - Controller launches worker subprocess inside named TMUX window:
     `tmux new-window -t agents -n 'maestro-{task_id}-{node_id}' '...'`
   - Update intent status to `dispatched`.
4. **Physical Process PID Binding**:
   - Worker identifies its **actual writing process PID** and writes an operational envelope containing PID to its attempt inbox.
   - Controller updates `writer_leases.writer_pid` to this verified PID.
5. **Execution & Checkpoint Collection**:
   - Worker executes action. Emits periodic heartbeat updates to SQLite or attempt heartbeat file.
   - For long-running agent loops, worker persists turn checkpoints to `.orchestrator/artifacts/`.
6. **Receipt & Reconciliation Commit**:
   - Worker writes `operation_receipts` row with exit status and output artifact hashes.
   - Controller ingests receipt in next tick, verifies outputs against schema, releases lease, and advances node to `succeeded` or `failed`.

---

## 4. PROCESS LEASING & WRITER CONFINEMENT INVARIANTS

### 4.1 The PID-Binding Invariant (`[INV-02]`)
- **Vulnerability**: A launcher process (e.g. bash wrapper or CLI launcher) can terminate with exit code 0 while its child worker or agent process continues running in the background. If a lease expires or relies on launcher exit, a second worker can be dispatched to the same worktree, corrupting git state.
- **Enforcement**:
  1. The lease MUST bind to the leaf writing PID.
  2. A clean exit of a parent launcher is NOT evidence of child termination.
  3. Before granting a lease to a replacement attempt, the controller MUST verify via `kill(pid, 0)` or `/proc/<pid>` that the previous writing PID has ceased execution.
  4. If a PID remains alive after lease expiry, the controller MUST issue `SIGTERM`, wait a bounded grace period, issue `SIGKILL`, and verify termination before reallocating the lease.

---

## 5. BRANCHING, JOINS, AND LOCAL PLAN REPAIR

### 5.1 Branching & Guarded Edges
- Edges express `source_port -> target_port` with optional guard predicates (`on_success`, `on_failure`, `custom_predicate`).
- If an edge guard evaluates to false, the target node is evaluated: if all incoming required edges are false/skipped, the target node transitions to `skipped`. A skipped node MUST NOT satisfy a required terminal verification check.

### 5.2 Join Semantics
1. **AND Join**: Requires all incoming active edges to complete successfully. Artifacts from all predecessor nodes are merged into the target node's input context.
2. **Candidate Selection Join**:
   - Evaluates outputs from multiple isolated candidate branches (e.g. `cand_A`, `cand_B`).
   - A deterministic evaluator or designated reviewer selects **exactly one winner**.
   - The join binds the winner's artifacts to downstream nodes.
   - **Negative Invariant**: It is STRICTLY PROHIBITED to cherry-pick patches or mix test proofs across candidates.

### 5.3 Local Plan Repair Protocol
When an active task encounters an unexpected failure, discovered prerequisite, or scope change:
1. The planner or diagnoser node emits a `PlanRepairProposal`.
2. The controller compiles a new `WorkflowRevision` (revision `N + 1`), setting `parent_revision = N`.
3. Invariant: Unaffected completed nodes from revision `N` and their verified artifacts are carried forward.
4. Active or stale downstream nodes are canceled before they consume obsolete outputs.
5. Invariant: Logical task retry and repair counters are NEVER reset.

---

## 6. CRASH RECOVERY, TIMEOUTS, AND LEASE RECONCILIATION

When the controller process restarts after a crash or power failure:
1. **Quiescent Recovery Scan**: Read `writer_leases` and active `task_runs`.
2. **Process Probing**: For each claimed lease, probe the recorded `writer_pid`.
   - If PID is dead: Check if an uningested result envelope or receipt exists on disk. If yes, ingest it; if no, mark attempt as `uncertain` or `failed`.
   - If PID is alive: Adopt the running process, update heartbeat, and allow it to continue until deadline.
3. **Fencing Protection**: Any late-arriving write from an attempt with a superseded `lease_epoch` is rejected by SQLite check constraints.
4. **Bake-Off Trigger**: If native SQLite + lease recovery demonstrates unmanageable complexity in production (evidenced by state corruption or deadlock under test matrix), an external durable engine bake-off is triggered. The evaluation order is strictly:
   1. **DBOS** (lightweight Python transactional substrate)
   2. **Restate** (event-driven durable RPC)
   3. **Temporal** (heavyweight external orchestration cluster)
