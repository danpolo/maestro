# MAESTRO VALIDATION, STAGING & ACCEPTANCE (AGENT SPECIFICATION)

```
Document ID:     MGES-VALIDATION-ACCEPTANCE-V2
Target Audience: Autonomous AI Coding Agents / LLM Implementation Engines
Domain:          Authoritative Verification, Staging Worktrees, Target Gating
Base Revision:   Revision 2 (2026-09-08)
Status:          Authoritative Quality & Integration Specification
```

---

## 1. THREE DISTINCT VALIDATION QUESTIONS

The validation subsystem (`maestro/validation.py`) strictly separates three distinct operational questions:
1. **What must be checked?**: Determined dynamically by the task's acceptance criteria, declared project policies, and conservative impact analysis.
2. **Is existing evidence still valid?**: Determined deterministically by computing cryptographic fingerprints of inputs, code snapshots, toolchains, and environments.
3. **Are task outcomes satisfied?**: Evaluated as a closed-world boolean function over verified evidence receipts, reviewer records, and required effect receipts.

---

## 2. FINGERPRINTING & EVIDENCE REUSE

### 2.1 Cryptographic Input Fingerprinting
A cache hit for a verification check requires an exact match across four distinct hashes:
1. `source_snapshot_id`: Full git tree SHA or commit SHA of the evaluated worktree.
2. `command_hash`: SHA256 of the exact command argv, options, and adapter code.
3. `input_fingerprint`: Combined SHA256 of all declared input dependencies (files, fixtures, lockfiles, schemas).
4. `environment_fingerprint`: Combined SHA256 of toolchain version, OS details, Python version, and non-secret configuration.

### 2.2 Evidence Invalidation Rules (`[INV-A05]`)
- **Behavior vs. Signature**: An unchanged public API signature does NOT imply unchanged behavioral validity. Any change to implementation bytes, dependencies, or test fixtures invalidates all downstream verification evidence.
- **Shadow Affected-Check Mode**: In early phases (P4–P9), affected-check selection runs in **shadow mode** against the full test suite. Both run, and any discrepancies are recorded. Full test suites MUST run before acceptance until qualified under Experiment E3.
- **Non-Cacheable Checks**: Network calls, end-to-end browser tests, nondeterministic evaluations, and staging smoke tests are strictly non-cacheable unless they declare an explicit pinned snapshot contract.

---

## 3. THE 5-STAGE INTEGRATION & PROMOTION PIPELINE

Acceptance requires advancing code through an isolated staging pipeline. Direct writes to the target branch are physically prohibited.

```
[Candidate Worktree]
         │  1. Freeze candidate (read-only snapshot)
         ▼
[Dedicated Staging Worktree]
         │  2. Merge candidate into fresh target snapshot
         │  3. Execute required integration checks on merged tree
         ▼
[Target Branch Fast-Forward]
         │  4. Fast-forward project.main_branch under target lease
         ▼
[Required Side Effects]
         │  5. Execute push/deploy nodes with receipt/probe confirmation
         ▼
[Commit Acceptance Transaction in SQLite]
```

### 3.1 Step-by-Step Staging Procedure
1. **Freeze Candidate**:
   - Controller stops all source writers on the task worktree.
   - Computes candidate commit SHA and tree SHA.
   - Verifies all candidate-level checks passed.
2. **Prepare Staging Snapshot**:
   - Controller claims the exclusive `target_branch` lease.
   - Reads the latest target branch commit (`project.main_branch`).
   - In a dedicated staging worktree (`.orchestrator/worktrees/staging/`), merges candidate into target branch.
   - Invariant: If a merge conflict occurs, the integration aborts. Merges MUST NOT be resolved automatically by LLMs without explicit plan repair.
3. **Execute Integration Verification**:
   - Controller executes all mandatory integration test suites on the **merged snapshot**.
   - Generates `VerificationEvidence` records tied to the merged snapshot SHA.
4. **Target Ref Fast-Forward**:
   - Controller verifies target ref has not drifted during integration testing.
   - Fast-forwards `project.main_branch` to the verified integration commit.
5. **Execute External Side Effects**:
   - If task declares external effects (git push, deploy, canary), controller invokes effect handlers.
   - Each effect emits an `OperationIntent` before execution and an `OperationReceipt` upon completion.
   - Postcondition probes verify external state (e.g. remote git ref updated).
6. **Commit Acceptance**:
   - In a single SQLite transaction, controller writes `AcceptanceDecision` referencing all evidence IDs, updates task status to `accepted`, and updates roadmap projections.

---

## 4. ADAPTER EFFECT CLASSES & POSTCONDITION PROBES

All project adapters declare their effect class and recovery contract:

| Effect Class | Recovery Contract | Idempotent? | Automatic Retry Allowed? |
| :--- | :--- | :--- | :--- |
| **Pure Derivation** | Memoize by complete input/version hash. | Yes | Yes (pure re-execution) |
| **Repeatable Local Check** | Reuse exact valid evidence or rerun on identical snapshot. | Yes | Yes |
| **Agent Editing Worktree** | Adopt live process or preserve dirty worktree and reconcile git state. | Conditional | Only with worktree lease reconciliation |
| **External Side Effect (Push/Deploy)** | Require `OperationIntent` + `OperationReceipt` + postcondition `probe`. | No | **STRICTLY FORBIDDEN** without successful postcondition probe |

### 4.1 The Postcondition Probe Rule (`[INV-A05]`)
- An adapter with an external side-effect that is not inherently idempotent MUST provide a postcondition `probe(context) -> bool`.
- If a worker crashes, times out, or loses network connection during execution, the controller invokes `probe()`.
- If the probe returns `True` (effect succeeded externally), the controller records an `OperationReceipt` and marks the node succeeded.
- If the probe returns `False` or is not defined, automatic retry is **STRICTLY PROHIBITED**. The state is marked `uncertain` and escalated to the operator.
- **Rationale**: Blind replay of an unprobed external effect duplicates actions (e.g. double git pushes, duplicate deployments, duplicate API billing).

---

## 5. HUMAN APPROVAL BINDINGS & EXPIRY

Human-in-the-loop (HITL) approvals are first-class durable artifacts:
1. **Cryptographic Subject Binding**: An approval binds strictly to:
   - `task_id` and `run_id`
   - Specific action or patch SHA
   - Policy revision hash
   - Expiration timestamp
2. **Invalidation Invariant**: If the underlying source patch or policy changes before execution, the existing human approval is automatically invalidated. Stale or duplicate human responses CANNOT approve a modified subject.
