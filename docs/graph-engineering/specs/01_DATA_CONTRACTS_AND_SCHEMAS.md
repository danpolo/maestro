# MAESTRO DATA CONTRACTS AND SCHEMAS (AGENT SPECIFICATION)

```
Document ID:     MGES-DATA-CONTRACTS-V2
Target Audience: Autonomous AI Coding Agents / LLM Implementation Engines
Domain:          Data Models, SQLite DDL, Typed Contracts, Artifact Referencing
Base Revision:   Revision 2 (2026-09-08)
Status:          Authoritative Interface & Data Definition Specification
```

---

## 1. IDENTITY ARCHITECTURE & ID NAMESPACING

All domain entities use opaque, globally unique, content-addressed or UUIDv7/nanoid identifiers. Entities MUST NOT derive ownership or identity from mutable strings, task titles, or TMUX window names.

| ID Type | Format / Generator | Scoping / Ownership |
| :--- | :--- | :--- |
| `task_id` | Sluggified kebab-case or alphanumeric prefix (`T-001`, `fix-auth-race`) | Stable across all runs, revisions, and retries. |
| `spec_revision` | Integer (1, 2, 3...) | Monotonically increasing revision of the task specification. |
| `run_id` | Opaque execution instance ID (`run_01j7abc...`) | Binds a single top-level attempt to execute a task. |
| `workflow_id` | Opaque workflow graph identifier | Binds a family of workflow graph revisions for a task. |
| `revision` | Integer (1, 2, 3...) | Immutable snapshot of a compiled workflow DAG. |
| `node_id` | Semantic path or slug within workflow (`planner`, `impl`, `test`, `verify`) | Scoped to a specific `WorkflowRevision`. |
| `attempt_id` | `att_{run_id}_{node_id}_{attempt_index}` | Physical invocation attempt of a specific node. |
| `artifact_id` | `art_{sha256_prefix}_{nanoid}` | Content-addressed reference to an immutable file/payload. |
| `event_id` | `evt_{sequence_number}_{nanoid}` | Strict sequential log entry in the control database. |
| `evidence_id` | `evi_{check_id}_{source_snapshot_id[:8]}_{nanoid}` | Cryptographic receipt of a verification check execution. |

---

## 2. CORE DOMAIN DATA SCHEMAS

### 2.1 Task Specification (`TaskSpec`)
The canonical parsed representation of a task from `ROADMAP.md`.

```yaml
TaskSpec:
  task_id: str                      # Opaque permanent task identifier
  spec_revision: int                # Monotonic revision number of this specification
  title: str                        # Human-readable summary
  goal: str                         # Concrete engineering objective
  acceptance_criteria: list[str]    # Discrete, verifiable criteria list
  depends_on: list[str]             # List of prerequisite task_ids (hard dependency edges)
  group_id: str | null              # Hierarchical parent task group ID (null if top-level)
  risk: str                         # 'low' | 'medium' | 'high' | 'critical'
  task_class: str                   # 'bug' | 'feature' | 'documentation' | 'difficult_debugging' | 'manual_prep' | 'recovery'
  execution_kind: str               # 'ordinary' | 'script' | 'manual' | 'async' | 'resumable'
  required_artifact_types: list[str]# Required output types (e.g. ['git_patch', 'test_results'])
  declared_effects: list[str]       # External effects: 'local_worktree' | 'git_push' | 'deploy'
  scope: list[str]                  # File/directory glob paths targeted by this task
  constraints: list[str]            # Hard negative constraints (e.g. 'no thirdparty dependencies')
  hold: bool                        # If true, controller pauses execution before dispatch
  self_modifying: bool              # If true, task modifies Maestro's own codebase/runtime
  legacy_fields: dict               # Preserved unmapped legacy attributes (lossless roundtrip)
```

### 2.2 Workflow Revision (`WorkflowRevision`)
The immutable compiled execution graph for a task run.

```yaml
WorkflowRevision:
  workflow_id: str                  # Workflow instance ID
  revision: int                     # Monotonic revision integer (retries don't increment; repair does)
  task_id: str                      # Target task ID
  task_spec_revision: int           # Pinned revision of TaskSpec used during compilation
  parent_revision: int | null       # Preceding revision if produced via local plan repair
  compiler_version: str             # Version/hash of the compiler that produced this DAG
  policy_hash: str                  # SHA256 of applied project.yaml engineering policy
  project_profile_hash: str         # SHA256 of discovered project profile facts
  code_base_sha: str                # Git commit SHA of target repository at compilation
  execution_objective: str          # 'paid_efficiency' | 'free_quality'
  backend_eligibility: list[str]    # Allowed backend IDs ('claude', 'codex', 'antigravity', 'model_api')
  catalog_snapshot_hash: str        # SHA256 of backend catalog snapshot used for routing
  nodes: list[NodeSpec]             # List of node specifications
  edges: list[EdgeSpec]             # Guarded directed edges connecting nodes
  terminal_requirements: list[str]  # Node IDs that MUST succeed for workflow acceptance
  mutation_reason: str | null       # Reason string if compiled via plan repair
```

### 2.3 Node Specification (`NodeSpec`)
An executable unit of work inside a workflow revision.

```yaml
NodeSpec:
  node_id: str                      # Unique ID within workflow (e.g. 'implementer')
  kind: str                         # 'agent' | 'function' | 'tool' | 'human' | 'wait' | 'decision' | 'join'
  handler_ref: str                  # Registered handler identifier (e.g. 'maestro.handlers.agent')
  handler_version: str              # Semver or digest of handler code
  agent_definition_ref: str | null  # ID of AgentDefinition if kind == 'agent'
  input_bindings: dict[str, str]    # Map of input ports to source output ports (e.g. {'patch': 'impl.patch'})
  output_schema_ref: str            # Schema ID validating node output (e.g. 'NodeResult.v1')
  activation_predicate: str | null  # Pure function name evaluating whether node triggers
  permissions_ref: str              # Granted permissions profile (e.g. 'task_worktree_writer')
  resource_request: dict            # CPU, RAM_MB, GPU_VRAM_MB, inference_slots, writer_lease (bool)
  retry_policy:                     # Local retry parameters
    max_attempts: int               # Maximum retry count (default: 1 in paid, 2 in free)
    backoff_seconds: float          # Exponential backoff base
    retryable_errors: list[str]     # Whitelist of error categories
  timeout_policy:
    execution_timeout_sec: int      # Hard execution wall-clock ceiling
    idle_heartbeat_timeout_sec: int # Timeout without process heartbeat
  effect_class: str                 # 'pure_derivation' | 'repeatable_local_check' | 'agent_editing_worktree' | 'external_side_effect'
  reconciliation_handler: str | null# Handler invoked if process/effect outcome is uncertain
  cache_policy: str                 # 'immutable_memoized' | 'recheck_required' | 'never'
  candidate_id: str | null          # Candidate identifier if part of multi-candidate group (e.g. 'cand_A')
```

### 2.4 Edge Specification (`EdgeSpec`)
Directed dependency relation connecting nodes.

```yaml
EdgeSpec:
  source_node_id: str               # Predecessor node
  source_port: str                  # Output port on predecessor (default: 'outcome')
  target_node_id: str               # Successor node
  target_port: str                  # Input port on successor (default: 'input')
  guard_predicate: str | null       # Named pure predicate: 'on_success' | 'on_failure' | 'is_valid'
  edge_kind: str                    # 'dependency' | 'data_flow'
```

### 2.5 Agent Definition (`AgentDefinition`)
A reusable, versioned role contract defining an agent's cognitive posture.

```yaml
AgentDefinition:
  role_id: str                      # 'implementer' | 'planner' | 'reviewer' | 'test_designer' | 'diagnoser' | 'setup_adviser'
  version: str                      # Semver of role definition
  responsibility: str               # Explicit statement of what the role owns
  exclusions: list[str]             # Strict negative boundaries (what it MUST NOT do)
  instruction_layers:
    system_contract: str            # Maestro operational execution contract
    project_rules: str              # Project engineering rules from workflows/project.yaml
    role_instructions: str          # Role-specific engineering guidelines
  context_assembly_rules: dict      # Budget limits, AST inclusion policy, prior handoff depth
  skill_references: list[str]       # Exact skill IDs and content hashes required
  logical_tools: list[str]          # Tool names exposed (e.g. ['read_file', 'write_file', 'run_command'])
  permission_requirements: list[str]# Enforced permissions required (e.g. ['isolated_worktree'])
  permitted_backend_capabilities:   # Backend requirements
    min_reasoning_strength: str     # 'small' | 'balanced' | 'strong'
    modalities: list[str]           # ['text'] or ['text', 'image']
  effort_policy:
    paid_default: str               # Native effort setting for paid mode ('low', 'medium', 'high')
    free_default: str               # Native effort setting for free mode
  success_criteria: list[str]       # Validation checks required on output
  memory_handoff_policy: str        # 'stateless' | 'read_prior_attempt' | 'full_causal'
```

### 2.6 Agent Runtime Binding (`AgentBinding`)
The immutable resolution of an agent node to an active backend, model, and effort level.

```yaml
AgentBinding:
  backend_id: str                   # 'claude_cli' | 'codex_cli' | 'antigravity_cli' | 'model_api'
  runtime_instance_id: str          # Unique ID of backend instance (path, process handle, endpoint)
  model_id: str                     # Concrete model string (e.g. 'claude-3-5-sonnet-20241022')
  effort_native_value: str | null   # Backend native effort control value (e.g. 'high', 3, null)
  effort_delivery: str              # 'flag' | 'config_key' | 'unsupported'
  effort_support: str               # 'native' | 'mapped' | 'unsupported'
  catalog_revision: str             # Hash/version of the catalog entry resolved
  instruction_delivery: str         # 'system_flag' | 'profile_file' | 'appended_prompt'
  permission_enforcement: str       # 'physical_sandbox' | 'directory_isolation' | 'conventional'
  selection_reason: str             # Justification string for audit
  rejected_routes: list[dict]       # Evaluated routes that were rejected, with failure reasons
```

### 2.7 Attempt & Process Tracking (`Attempt`)
Physical execution attempt of a node.

```yaml
Attempt:
  attempt_id: str                   # 'att_run01_impl_01'
  run_id: str                       # Enclosing run ID
  workflow_revision: int            # Active workflow revision
  node_id: str                      # Executing node
  candidate_id: str | null          # Candidate ID if in candidate group
  binding: AgentBinding | null      # Applied binding (null for non-agent nodes)
  input_hash: str                   # SHA256 of all resolved input artifacts and parameters
  lease_epoch: int                  # Monotonic fencing token epoch
  writer_pid: int | null            # Physical PID of writing process (MUST NOT be launcher PID)
  status: str                       # 'pending' | 'claimed' | 'running' | 'succeeded' | 'failed' | 'uncertain'
  started_at: str                   # ISO8601 UTC timestamp
  heartbeat_at: str                 # ISO8601 UTC timestamp of last verified liveness signal
  deadline_at: str                  # ISO8601 UTC timestamp of hard timeout
  checkpoint_ref: str | null        # Artifact ID of last valid execution checkpoint
  outcome_ref: str | null           # Artifact ID of NodeResult envelope
```

### 2.8 Content-Addressed Artifact Reference (`ArtifactRef`)
Immutable reference to external content.

```yaml
ArtifactRef:
  artifact_id: str                  # 'art_a1b2c3d4_...'
  type: str                         # 'git_patch' | 'test_log' | 'node_result' | 'checkpoint' | 'evidence'
  schema_version: str               # Schema version of payload if structured
  content_hash: str                 # SHA256 hex digest of file payload
  storage_ref: str                  # Storage path relative to .orchestrator/artifacts/
  source_snapshot_id: str | null    # Git commit SHA of source tree if artifact is code/diff
  producer_attempt_id: str | null   # Attempt ID that generated this artifact
  validity_dependencies: list[str]  # List of artifact_ids this artifact logically depends on
```

### 2.9 Domain Event Envelope (`Event`)
Append-only log entry recording all state transitions.

```yaml
Event:
  event_id: str                     # 'evt_000001_...'
  sequence: int                     # Monotonically increasing 64-bit integer
  schema_version: int               # Event envelope schema (1)
  type: str                         # Event type name (e.g. 'NodeAttemptClaimed')
  aggregate_id: str                 # Aggregate root ID (e.g. task_id or run_id)
  run_id: str | null                # Associated run ID
  attempt_id: str | null            # Associated attempt ID
  causation_id: str | null          # Event ID or Command ID that directly caused this event
  correlation_id: str               # Top-level operation/command correlation ID
  recorded_at: str                  # ISO8601 UTC timestamp
  payload: dict                     # Typed event payload
  artifact_refs: list[str]          # IDs of artifacts referenced in payload
```

### 2.10 Verification Specification & Evidence (`VerificationSpec`, `VerificationEvidence`)
Contracts for authoritative quality gating.

```yaml
VerificationSpec:
  check_id: str                     # 'pytest_unit' | 'ruff_lint' | 'mypy_typecheck'
  handler_ref: str                  # Verification handler identifier
  argv_or_adapter_ref: list[str]    # Command line arguments or adapter reference
  cwd_scope: str                    # Working directory relative to worktree root
  timeout: int                      # Wall-clock timeout seconds
  required: bool                    # True if check MUST pass for acceptance
  acceptance_criterion_ids: list[str]# Linked TaskSpec criteria discharged by this check
  expected_output_schema: str       # Output format schema
  input_dependencies: list[str]     # File globs determining cache validity (e.g. ['src/**/*.py'])
  dependency_completeness: str      # 'exact' | 'conservative_globs' | 'unknown'
  environment_contract: dict        # Required env vars, python version, toolchain hashes
  external_state_contract: dict|null# Pinned snapshot of database/network if applicable
  cache_policy: str                 # 'exact_snapshot_only' | 'affected_shadow' | 'never'

VerificationEvidence:
  evidence_id: str                  # 'evi_pytest_a1b2c3...'
  check_id: str                     # Linked VerificationSpec check_id
  source_snapshot_id: str           # Git commit SHA of the exact evaluated worktree
  attempt_id: str                   # Attempt ID during which verification executed
  verifier_version: str             # Version/hash of test runner executable
  command_and_adapter_hash: str     # SHA256 of command argv and adapter code
  input_fingerprint: str            # Combined SHA256 of all input dependencies
  environment_fingerprint: str      # Combined SHA256 of toolchain/environment contract
  external_snapshot_id: str | null  # Snapshot ID of external mock/state
  started_at: str                   # ISO8601 UTC timestamp
  finished_at: str                  # ISO8601 UTC timestamp
  exit_status: int                  # Process exit code (0 == pass)
  outcome: str                      # 'passed' | 'failed' | 'timed_out' | 'errored'
  output_artifact_refs: list[str]   # Stdout/stderr log ArtifactRefs
  reused_from: str | null           # Original evidence_id if reused from cache
  reuse_reason: str | null          # Rationale for cache hit
  validity: str                     # 'current' | 'stale' | 'invalidated'

AcceptanceDecision:
  task_id: str                      # Target task
  run_id: str                       # Target run
  task_spec_revision: int           # Evaluated task spec revision
  policy_hash: str                  # Applied project policy hash
  integrated_source_snapshot: str   # Git commit SHA of integrated target branch
  required_evidence_ids: list[str]  # Evidence IDs proving all required checks passed
  review_dispositions: list[dict]   # Reviewer findings and formal approvals
  exemptions: list[dict]            # Explicit authorized exemptions (must include reason & scope)
  required_effect_receipts: list[str]# Receipts for external side effects
  decision: str                     # 'accepted' | 'rejected'
  reason: str                       # Detailed justification
  event_id: str                     # Commit event ID
```

---

## 3. SQLITE CONTROL DATABASE DDL (`.orchestrator/control.sqlite3`)

The control database MUST operate in WAL mode with foreign key enforcement enabled:

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 10000;

-- Idempotent Command Inbox
CREATE TABLE command_inbox (
    command_id TEXT PRIMARY KEY,
    command_type TEXT NOT NULL,
    idempotency_key TEXT UNIQUE NOT NULL,
    received_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('received', 'processing', 'committed', 'rejected')),
    payload TEXT NOT NULL,
    error TEXT
);

-- Append-Only Causal Event Store
CREATE TABLE events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,
    schema_version INTEGER NOT NULL,
    type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    run_id TEXT,
    attempt_id TEXT,
    causation_id TEXT,
    correlation_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX idx_events_aggregate ON events(aggregate_id, sequence);
CREATE INDEX idx_events_run ON events(run_id);

-- Tasks & Roadmap State
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    current_spec_revision INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('planned', 'active', 'waiting', 'blocked', 'accepted', 'canceled')),
    hold BOOLEAN NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE task_revisions (
    task_id TEXT NOT NULL,
    spec_revision INTEGER NOT NULL,
    spec_yaml TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    PRIMARY KEY (task_id, spec_revision),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

CREATE TABLE task_dependencies (
    task_id TEXT NOT NULL,
    depends_on_task_id TEXT NOT NULL,
    spec_revision INTEGER NOT NULL,
    PRIMARY KEY (task_id, depends_on_task_id, spec_revision),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

-- Workflow Execution Graph
CREATE TABLE workflow_revisions (
    workflow_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    task_spec_revision INTEGER NOT NULL,
    parent_revision INTEGER,
    compiler_version TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    code_base_sha TEXT NOT NULL,
    execution_objective TEXT NOT NULL,
    dag_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (workflow_id, revision),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

-- Task Runs
CREATE TABLE task_runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    workflow_revision INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running', 'paused', 'succeeded', 'failed', 'canceled')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

-- Active Writer Leases & Process Fencing
CREATE TABLE writer_leases (
    resource_id TEXT PRIMARY KEY,       -- e.g. 'worktree:impl/T-001' or 'target_branch'
    lease_epoch INTEGER NOT NULL,       -- Fencing token
    owner_attempt_id TEXT NOT NULL,
    writer_pid INTEGER NOT NULL,        -- Actual OS process PID performing writes
    claimed_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- Controlled Operation Intents and Receipts
CREATE TABLE operation_intents (
    operation_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL,
    effect_class TEXT NOT NULL,
    intent_payload TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'dispatched', 'reconciled', 'failed'))
);

CREATE TABLE operation_receipts (
    receipt_id TEXT PRIMARY KEY,
    operation_id TEXT UNIQUE NOT NULL,
    attempt_id TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    exit_status INTEGER NOT NULL,
    receipt_payload TEXT NOT NULL,
    FOREIGN KEY (operation_id) REFERENCES operation_intents(operation_id)
);

-- Verification Evidence Registry
CREATE TABLE verification_evidence (
    evidence_id TEXT PRIMARY KEY,
    check_id TEXT NOT NULL,
    source_snapshot_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    command_hash TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    environment_fingerprint TEXT NOT NULL,
    exit_status INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    validity TEXT NOT NULL CHECK(validity IN ('current', 'stale', 'invalidated')),
    recorded_at TEXT NOT NULL
);
CREATE INDEX idx_evidence_fingerprint ON verification_evidence(check_id, input_fingerprint, environment_fingerprint);

-- Content-Addressed Artifacts
CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    type TEXT NOT NULL,
    storage_rel_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
```

---

## 4. CONTENT-ADDRESSED ARTIFACT STORAGE PROTOCOL

All files, patches, logs, and outputs are managed by `maestro/control/artifacts.py`:

1. **Storage Root**: `.orchestrator/artifacts/sha256/{first_2_chars}/{next_2_chars}/{full_sha256}`
2. **Atomic Write Protocol**:
   - Step 1: Write bytes to temporary file in `.orchestrator/artifacts/tmp/tmp_{nanoid}`.
   - Step 2: Flush and `os.fsync(fd)`.
   - Step 3: Compute SHA256 hex digest.
   - Step 4: Atomic rename to target content-addressed path via `os.replace()`.
   - Step 5: Directory fsync on parent directory.
   - Step 6: Commit `ArtifactRef` row to `control.sqlite3`.
3. **Immutability Invariant**: Artifacts are strictly read-only after creation. Files in the artifact store MUST NEVER be modified in place.
4. **Orphan Safety Rule**: A crash between artifact creation and database commit leaves an unreferenced orphan file on disk. Orphan files are harmless and periodically collected by an offline scavenger. Missing referenced artifacts block execution; an empty-dict or null fallback is strictly prohibited.
