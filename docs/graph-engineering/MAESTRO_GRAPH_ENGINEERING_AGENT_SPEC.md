# MAESTRO GRAPH ENGINEERING ARCHITECTURE: AUTONOMOUS AGENT SPECIFICATION

```
Document ID:     MGES-CONSOLIDATED-V2
Target Audience: Autonomous AI Coding Agents / LLM Implementation Engines
Domain:          Maestro Execution Engine & Graph Engineering Evolution
Base Revision:   Revision 2 (2026-09-08)
Status:          Authoritative Complete Implementation Specification (Agent-Faced)
Source Truth:    docs/graph-engineering/MAESTRO_GRAPH_ENGINEERING_ARCHITECTURE.html
Modular Suite:   docs/graph-engineering/specs/
```

---

## 0. CONTEXT ENGINEERING DIRECTIVE FOR AGENTS

This document is the consolidated, agent-optimized specification of the Maestro Graph Engineering plan. Human narrative framing, rhetorical persuasive justification, and visual layout instructions have been removed. In their place are formal schemas, state transition tables, deterministic invariants, explicit negative constraints, concrete repository anchors, and verifiable acceptance checks.

Agents utilizing this specification MUST adhere to the following operational behaviors:
1. **Enforce Negative Constraints**: Prohibitions marked `[STRICT_NEGATIVE_CONSTRAINT]` or `MUST NOT` take precedence over generic coding patterns.
2. **Execute Closed-World Verification**: Do not infer test success. Run test commands on isolated worktrees and inspect explicit process exit codes and structured evidence receipts.
3. **Respect Authority Boundaries**: Only the active controller may write to SQLite or fast-forward the target branch. Workers and subagents operate within isolated worktrees.


## MAESTRO GRAPH ENGINEERING: AGENT SYSTEM SPECIFICATION & CONTEXT DISPATCHER

```
Document ID:     MGES-SPEC-INDEX-V2
Target Audience: Autonomous AI Coding Agents / LLM Implementation Engines
Domain:          Maestro Execution Engine & Graph Engineering Evolution
Base Revision:   Revision 2 (2026-09-08)
Status:          Authoritative Implementation Specification (Agent-Faced)
Source Truth:    docs/graph-engineering/MAESTRO_GRAPH_ENGINEERING_ARCHITECTURE.html
```

---

## 1. CONTEXT ENGINEERING DIRECTIVE FOR IMPLEMENTING AGENTS

This specification suite is engineered specifically for **AI agents** implementing the Maestro Graph Architecture. It replaces human-oriented explanatory prose with machine-actionable domain contracts, explicit state machines, formal schemas, strictly typed interfaces, negative constraints, and verifiable acceptance gates.

### 1.1 Core Principles of Context Engineering
1. **High Signal-to-Noise Ratio**: Narrative framing, visual layout hints, and persuasive justifications are stripped. Only technical invariants, schemas, state transition rules, and verification criteria are provided.
2. **Context Isolation & Just-In-Time Loading**: Do NOT load the entire specification suite into your prompt at once. Doing so introduces context degradation ("lost-in-the-middle") and token waste. Consult the **Context Routing Matrix (§4)** to load only the exact domain specification and phase file required for your assigned task.
3. **Fail-Closed & Negative Constraints**: When a condition or boundary is violated, execution MUST fail closed. Negative constraints (marked `[STRICT_NEGATIVE_CONSTRAINT]` or `MUST NOT`) override all default coding behaviors.
4. **Verifiable Proof Over Agent Prose**: Claims, consensus, or agent-generated `tests.pass` flags are NOT evidence. Only controller-executed checks against frozen snapshot artifacts with cryptographic receipts constitute proof.

---

## 2. FOUNDATIONAL ARCHITECTURAL DECISIONS (D01 – D08)

The system architecture is governed by eight immutable architectural axioms:

| ID | Axiom | Formal Definition & Bound |
| :--- | :--- | :--- |
| **D01** | **Domain Separation** | Decompose domain into strictly segregated concerns: Roadmap Intent, Task Workflows, Lifecycle State, Repository Index, Validation Dependencies, and Causal History. Connect exclusively via opaque stable IDs (`task_id`, `run_id`, `attempt_id`) and content-addressed `ArtifactRef` pointers. Universal multi-model schemas are strictly prohibited. |
| **D02** | **Native Durable Control** | Exactly one controller process per project. SQLite in WAL mode with foreign keys and full synchronous durability manages commands, events, and state projections. External operations must use explicit intents and postcondition receipts. External workflow engines (Temporal, DBOS, Restate) are deferred until native recovery complexity proves insufficient under Experiment E6 (bake-off order: DBOS → Restate → Temporal). |
| **D03** | **Project Workflow Construction** | `maestro init` deterministically derives a project profile from immutable repository facts and selects versioned workflow templates. The workflow compiler specializes templates per task. An LLM setup adviser proposes only judgment-dependent policy modifications via single combined human review. Static monolithic project graphs are rejected. |
| **D04** | **Configured Agents as Nodes** | Every agent node is an instance of a versioned `AgentDefinition` (responsibilities, instructions, context rules, tool grants, permission boundaries, output schemas). Distinct from runtime `AgentBinding` (backend, model, effort). Credential boundary invariant: Maestro invokes official installed CLIs under supported logins and treats auth as opaque; never extract session/OAuth credentials. Backends must pass thirdparty integration verification before being supported. |
| **D05** | **Two Optimization Objectives** | **Paid Efficiency Mode**: Minimize subscription call consumption and tokens per verified engineering outcome; enforce single candidate and template-first compilation. **Free Quality Mode**: Maximize task quality and success via extra inference (multi-candidate exploration, deeper decomposition, independent test designers, diverse review) with zero token-cost penalty. Reached via API provider with free tier later; local serving is out of scope. Both share one runtime executor. |
| **D06** | **Deterministic Acceptance** | Acceptance is strictly determined by controller-executed checks on frozen snapshots, cryptographically identified evidence receipts, isolated staging merge verification, and declared policy rules. Agent assertions, reviewer consensus, and `DONE` sentinel files are untrusted claims. Unchanged public API signatures do NOT prove unchanged behavioral validity. |
| **D07** | **Conservative Repository Intelligence** | Layer 0: Index-free project facts, file fingerprints, bounded `rg`, Python AST. Optional structural intelligence shortlist: 1) Tree-sitter (incremental, parse-error tolerant for intermediate worktrees), 2) SCIP (deep precise navigation benchmark), 3) Joern (on-demand data-flow/security only). Stack Graphs is removed (upstream archived). `tree-sitter-graph` DSL is explicitly excluded. Search/read fallback is permanent. |
| **D08** | **Incremental Vertical Evolution** | Migration preserves existing runtime seams (`orchestrator.py`, `state.py`, `roadmap.py`, `watchdog.py`). Vertical slices migrate one authority at a time with backwards-compatible import/export projections. Legacy runners and graph runners are mutually exclusive per project run. |

---

## 3. GLOBAL SYSTEM INVARIANTS (NON-NEGOTIABLE RULES)

Every implementing agent MUST enforce the following global invariants across all phases:

- `[INV-01] SINGLE_WRITER_AUTHORITY`: `.orchestrator/control.sqlite3` MUST be written exclusively by the active Maestro controller process. All external actors (CLI, Telegram, Watchdog, Workers) communicate solely via the idempotent command inbox table.
- `[INV-02] PROCESS_LEASE_BINDING`: A worktree writer lease binds to the **actual process PID that writes**, NOT to the launcher PID that started it. A clean launcher exit (code 0) is NOT proof that the child writer has stopped. Ownership release requires positive proof of writer PID termination.
- `[INV-03] CREDENTIAL_BOUNDARY`: Maestro MUST invoke installed CLIs as opaque subprocesses. Maestro MUST NEVER read, extract, serialize, or reuse OAuth tokens, session cookies, or refresh credentials from `~/.claude/`, `~/.codex/`, or browser storage.
- `[INV-04] CLOSED_WORLD_VERIFICATION`: Acceptance criteria MUST be evaluated against deterministic test commands executed by the controller on an isolated, frozen worktree. Missing verification keys, empty reviewer findings, or self-asserted passes fail closed (`FAILED`).
- `[INV-05] TARGET_BRANCH_PROTECTION`: Only the controller may write or fast-forward the target branch (`project.main_branch`). Agents write exclusively to assigned namespaced task worktrees (`impl/<task>`, `selffix/<id>`, `redo/<id>`). Flat branch names are prohibited.
- `[INV-06] NO_CROSS_CANDIDATE_POLLUTION`: When Free Quality Mode evaluates multiple candidates, each candidate MUST execute in an isolated worktree. The selection join MUST select exactly one winning candidate; mixing patches, artifacts, or proofs across candidates is strictly prohibited.
- `[INV-07] TRANSACTIONAL_EFFECT_INTENT`: Before executing an external side-effect (agent call, build, git push, deployment), the controller MUST commit an `OperationIntent` to SQLite. Upon completion, it MUST commit an `OperationReceipt`. Adapters without idempotent postcondition probes MUST NOT be retried automatically.
- `[INV-08] RETRY_COUNTER_IMMUTABILITY`: A task retry, plan repair, crash recovery, or backend switch MUST NOT reset cumulative task attempt counters, timeout deadlines, or repair quotas.
- `[INV-09] PER_PROJECT_BOOTSTRAP_PIN`: Runtime version switching in `bootstrap.py` MUST parse `--repo` / `` directly from `argv` using the standard library before importing Maestro modules, ensuring one project cannot silently switch another project's active runtime.
- `[INV-10] TMUX_WINDOW_ISOLATION`: All secondary tasks, heavy background jobs, or model serving harnesses MUST run in named windows inside the `agents` TMUX session (`tmux new-window -t agents -n ...`). Raw shell backgrounding (`&`) is strictly forbidden.
- `[INV-11] SEPARATE_FILESYSTEM_BACKUP`: SQLite WAL backups and artifact manifests MUST NOT be written to the same filesystem as the active project database.
- `[INV-12] THIRDPARTY_VERIFICATION_GATE`: Any optional parser, grammar, or backend CLI must be registered in `maestro/thirdparty.json`. `maestro doctor --thirdparty` MUST exit 0 before the dependency is enabled in production.

---

## 4. CONTEXT ROUTING MATRIX (JUST-IN-TIME DELIVERY)

When assigned a specific implementation task or phase, load **ONLY** the relevant specification files into context:

| Assignment / Task | Primary Required Spec Files | Secondary Spec Files | Primary Verification Command |
| :--- | :--- | :--- | :--- |
| **System Architecture / Invariants** | `README.md` (this file) | `01_DATA_CONTRACTS_AND_SCHEMAS.md` | N/A |
| **P0: Baseline Characterization** | `phases/P00_MIGRATION_BASELINE.md` | `01_DATA_CONTRACTS_AND_SCHEMAS.md` | `.venv/bin/python -m pytest -q tests/graph_engineering/test_baseline_lanes.py` |
| **P1: Transactional Control & Store** | `phases/P01_TRANSACTIONAL_CONTROL.md`, `01_DATA_CONTRACTS_AND_SCHEMAS.md` | `02_EXECUTION_ENGINE_AND_DURABILITY.md` | `.venv/bin/python -m pytest -q tests/control` |
| **P2: Roadmap Dependency Model** | `phases/P02_ROADMAP_DEPENDENCY_MODEL.md`, `01_DATA_CONTRACTS_AND_SCHEMAS.md` | `02_EXECUTION_ENGINE_AND_DURABILITY.md` | `.venv/bin/python -m pytest -q tests/test_taskgraph.py tests/characterization/test_roadmap.py` |
| **P3: Workflow Nodes & Runner** | `phases/P03_WORKFLOW_NODES_AND_RUNNER.md`, `02_EXECUTION_ENGINE_AND_DURABILITY.md` | `01_DATA_CONTRACTS_AND_SCHEMAS.md` | `.venv/bin/python -m pytest -q tests/workflows tests/control/test_recovery.py` |
| **P4: Validation Staging & Acceptance** | `phases/P04_AUTHORITATIVE_EVIDENCE_STAGING.md`, `04_VALIDATION_STAGING_AND_ACCEPTANCE.md` | `01_DATA_CONTRACTS_AND_SCHEMAS.md` | `.venv/bin/python -m pytest -q tests/test_validation_evidence.py tests/test_integration_acceptance.py` |
| **P5: Project Policy & Roles** | `phases/P05_PROJECT_WORKFLOWS_AND_ROLES.md`, `03_BACKENDS_CATALOG_AND_ROUTING.md` | `01_DATA_CONTRACTS_AND_SCHEMAS.md` | `.venv/bin/python -m pytest -q tests/workflows/test_policy.py tests/workflows/test_compiler.py` |
| **P6: Capability Routing & Catalog** | `phases/P06_CAPABILITY_ROUTING_CATALOG.md`, `03_BACKENDS_CATALOG_AND_ROUTING.md` | `05_SCHEDULING_AND_RESOURCES.md` | `.venv/bin/python -m pytest -q tests/backends/test_catalog.py tests/test_routing.py` |
| **P7: Free-Model & Hybrid Execution** | `phases/P07_FREE_MODEL_AND_HYBRID.md`, `03_BACKENDS_CATALOG_AND_ROUTING.md` | `02_EXECUTION_ENGINE_AND_DURABILITY.md` | `.venv/bin/python -m pytest -q tests/backends/test_model_runtime.py tests/workflows/test_mode_policies.py` |
| **P8: Resource Admission & Scheduler** | `phases/P08_RESOURCE_ADMISSION_SCHEDULING.md`, `05_SCHEDULING_AND_RESOURCES.md` | `02_EXECUTION_ENGINE_AND_DURABILITY.md` | `.venv/bin/python -m pytest -q tests/test_scheduling.py tests/control/test_resources.py` |
| **P9: Repository Facts & Structural** | `phases/P09_REPOSITORY_FACTS_STRUCTURAL.md`, `06_REPOSITORY_INTELLIGENCE.md` | `01_DATA_CONTRACTS_AND_SCHEMAS.md` | `.venv/bin/python -m pytest -q tests/repository` |
| **P10: Affected Validation & Plan Repair** | `phases/P10_AFFECTED_VALIDATION_REPAIR.md`, `04_VALIDATION_STAGING_AND_ACCEPTANCE.md` | `02_EXECUTION_ENGINE_AND_DURABILITY.md` | `.venv/bin/python -m pytest -q tests/test_affected_validation.py tests/workflows/test_plan_repair.py` |
| **P11: Operator Views & Bootstrap Pin** | `phases/P11_OPERATOR_LIFECYCLE_BOOTSTRAP.md`, `02_EXECUTION_ENGINE_AND_DURABILITY.md` | `05_SCHEDULING_AND_RESOURCES.md` | `.venv/bin/python -m pytest -q tests/test_workflow_status.py tests/ops/test_graph_service_lifecycle.py` |
| **P12: Full System Qualification** | `phases/P12_QUALIFICATION_AND_RELEASE.md`, `phases/MIGRATION_GUARDRAILS.md` | `07_EXPERIMENTS_AMENDMENTS_AND_DISPOSITION.md` | `.venv/bin/python scripts/graph_qualification.py --repo /tmp/maestro-graph-qualification --fixture-backends` |

---

## 5. SYSTEM COMPONENT MAP & ARTIFACT DIRECTORY STRUCTURE

The target codebase architecture maps onto the existing repository structure as follows:

```
maestro/
├── control/                     # [P1] Transactional state authority & event store
│   ├── models.py                # Typed commands, events, projections
│   ├── store.py                 # SQLite WAL store & single-writer controller
│   ├── lifecycle.py             # FSM state reducers & fencing tokens
│   ├── artifacts.py             # Content-addressed artifact store (fs + sha256)
│   ├── reconcile.py             # [P3] Crash recovery & lease reconciliation
│   └── resources.py             # [P8] Host admission & pool reservation ledger
├── taskgraph.py                 # [P2] Roadmap DAG parser, cycle detector, CAS mutator
├── workflows/                   # [P3, P5] Workflow compilation & execution engine
│   ├── models.py                # WorkflowRevision, NodeSpec, EdgeSpec
│   ├── compiler.py              # Template specialization & DAG compiler
│   ├── runner.py                # tick() loop & async worker dispatcher
│   ├── handlers.py              # Node kind executors (agent, tool, function, wait)
│   └── policy.py                # [P5] Project engineering policy & quality gates
├── validation.py                # [P4, P10] Authoritative evidence evaluation & staging
├── backends/                    # [P6, P7] Capability catalog & backend runtimes
│   ├── catalog.py               # Capability catalog & empirical outcome statistics
│   ├── router.py                # resolve_agent() joint model/effort binder
│   ├── model_api.py             # [P7] Free-tier HTTP API adapter
│   └── model_runtime.py         # [P7] Bounded model/tool supervisor loop
├── repository/                  # [P9] Repository intelligence & context manifests
│   ├── queries.py               # Lexical/AST/reference query interface
│   ├── index.py                 # Disposable SQLite index (.orchestrator/repository.sqlite3)
│   ├── providers.py             # Tree-sitter, SCIP, Joern adapters
│   └── context.py               # Role-aware ContextManifest generator
└── scheduling.py                # [P8] Greedy admission scheduler & priority queues
```

---

## 6. EXECUTION DEPENDENCY GRAPH (CRITICAL PATH)

The implementation critical path is strictly linear through the foundational milestones, with scheduling and structural intelligence branching conditionally:

```
[P0: Baseline Fixtures]
         │
         ▼
[P1: Transactional Control & Single Writer]
         │
         ▼
[P2: Roadmap Dependency Model (TaskGraph)]
         │
         ▼
[P3: Workflow Nodes & Runner (Lanes -> Graph)]
         │
         ▼
[P4: Authoritative Evidence & Staging Integration]
         │
         ▼
[P5: Project Policy & Agent Role Definitions]
         │
         ▼
[P6: Backend Capability Catalog & Routing]
         │
         ▼
[P7: Free-Model & Hybrid Execution Policy]
         │
         ├─────────────────────────────────────────┐
         │ (Core Critical Path)                    │ (Conditional Enhancements)
         ▼                                         ▼
[P11: Operator Views & Bootstrap Pin]      [P8: Resource Admission]  [P9: Repo Facts & AST]
         │                                         │                     │
         │                                         └──────────┬──────────┘
         │                                                    ▼
         │                                         [P10: Local Plan Repair]
         │                                                    │
         ▼                                                    ▼
[P12: Full System End-to-End Qualification & Release Verification]
```

---

## 7. AMENDMENT TRACEABILITY REFERENCE

All implementation rules incorporate amendments through **Revision 2 (2026-09-08)**:
- **A01 (D02/D04/D07)**: Durability bake-off order: DBOS → Restate → Temporal. Credential-boundary enforced. Stack Graphs removed.
- **A02 (D05/P7)**: Free mode uses API provider with free tier later; local serving out of scope. Free admission reuses P6 pool contract.
- **A03 (P0/P12)**: Fixtures cover fixed 6-class catalog. Raw task outcomes retained for B01/B18. Measured artifact retention volume.
- **A04 (P1/P3)**: Writer lease binds to writing PID, not launcher PID.
- **A05 (P4)**: Adapters declare `effect_class` and postcondition `probe`. Unprobed non-idempotent adapters never retry automatically.
- **A06 (P5)**: Templates composed from named versioned fragments. Setup records `unresolved[]` facts that fail closed.
- **A07 (P6)**: Grade acquisition path (push vs pull). `QuotaObservation` records `observed_at`, `written_at`, `staleness_bound`, `carried_forward`. `effort_delivery` specified. Shape keys added to statistics.
- **A08 (P7)**: Implementer branches namespaced (`impl/<task>`, `selffix/<id>`, `redo/<id>`). Directory enforcement granularity. 3 extra git paths for worktree commits.
- **A09 (P9/P11)**: Grammars verified per artifact. Bootstrap parses `--repo` from `argv` before re-exec.
- **A10 (E1/E2)**: Fixture calibration required before gating. E2 runs on Maestro's own repository mix.
- **A11 (Durability)**: Surviving disk loss is an explicit non-goal; backups must use a separate filesystem.
- **A12 (Gate)**: Integration verification gate (`maestro doctor --thirdparty`) applies to every phase.

## MAESTRO DATA CONTRACTS AND SCHEMAS (AGENT SPECIFICATION)

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

## MAESTRO EXECUTION ENGINE & DURABILITY PROTOCOL (AGENT SPECIFICATION)

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

## MAESTRO BACKEND CATALOG & MODEL ROUTING (AGENT SPECIFICATION)

```
Document ID:     MGES-BACKENDS-ROUTING-V2
Target Audience: Autonomous AI Coding Agents / LLM Implementation Engines
Domain:          Agent Roles, Routing Resolution, Capability Catalog, Sandbox
Base Revision:   Revision 2 (2026-09-08)
Status:          Authoritative Routing & Backend Interface Specification
```

---

## 1. THE CAPABILITY CATALOG

Maestro decouples agent role definitions from specific LLM vendors or CLI binaries. The catalog (`maestro/backends/catalog.py`) models backends as capability providers across six explicit dimensions:

```yaml
BackendCapabilityEntry:
  backend_id: str                   # 'claude_cli' | 'codex_cli' | 'antigravity_cli' | 'model_api'
  runtime_kind: str                 # 'agent_cli' | 'model_endpoint_harness'
  installed_version: str            # Output of CLI --version or API contract version
  billing_category: str             # 'subscription' | 'free_tier_api' | 'pay_per_token'
  usage_pool_id: str                # Unique account/pool identifier for quota tracking
  available_models: list[str]       # Exact concrete model IDs (e.g. ['claude-3-5-sonnet-20241022'])
  effort_controls:
    supported: bool                 # Whether reasoning effort controls are exposed
    delivery_mechanism: str         # 'flag' | 'config_key' | 'unsupported'
    native_values: list[str]        # Native vocabulary (e.g. ['low', 'medium', 'high'])
  context_limits:
    max_input_tokens: int           # Raw context window limit
    max_output_tokens: int          # Generation limit
    effective_context_ceiling: int  # Empirical quality ceiling (beyond which attention degrades)
  modalities: list[str]             # ['text'] or ['text', 'image']
  tool_delivery: str                # 'native_cli_tools' | 'json_schema_dispatch' | 'prompt_convention'
  instruction_delivery: str         # 'system_flag' | 'profile_file' | 'appended_prompt'
  structured_output_support: str    # 'json_mode' | 'tool_call' | 'regex_bounded'
  permission_enforcement: str       # 'physical_sandbox' | 'directory_isolation' | 'conventional'
  concurrency_limit: int            # Maximum concurrent active processes for this pool
  telemetry_quality: str            # 'token_exact' | 'window_percentage' | 'unobserved'
```

---

## 2. ROUTING RESOLUTION ENGINE (`resolve_agent`)

Scattered model lookups across scripts are replaced by one central routing function:

```python
def resolve_agent(
    node: NodeSpec,
    context_manifest: ContextManifest,
    resources: ResourcePoolState,
    catalog: CapabilityCatalog,
    policy: ProjectPolicy
) -> AgentBinding | RouteUnavailable:
    """
    Deterministically resolves an agent node to a concrete backend, model, and effort level.
    The resolved AgentBinding is persisted once and reused across all execution phases.
    """
```

### 2.1 The Resolution Pipeline
1. **Hard Constraint Filtering**: Eliminate backends that violate policy billing category, lacked required modalities, exceed context limits, or fail required tool/permission enforcement.
2. **Task Demand Estimation**: Evaluate task complexity, code churn, language, and historical failure rate. High-risk tasks mandate strong reasoning models; they are NEVER routed to low-effort small models.
3. **Joint Model & Effort Selection**:
   - **Paid Efficiency Mode**: Minimize expected token/call consumption while satisfying quality floors. Selects small/balanced models with moderate effort for routine tasks.
   - **Free Quality Mode**: Maximizes task success; ranks models purely by verified capability and deep reasoning effort without token cost penalties.
4. **Effort Delivery Mapping (`[INV-A07]`)**:
   - `effort_delivery` MUST be recorded: `'flag'` (e.g. `--effort=high`), `'config_key'` (e.g. `reasoning_effort: high`), or `'unsupported'`.
   - Adapters MUST NOT invent CLI flags that the installed version does not support. If effort is unsupported, record `effort_support='unsupported'` and select the best base model.
5. **Admission & Resource Check**: Verify usage pool has available quota and concurrency slots. If eligible backend is saturated, evaluate waiting vs. routing to an equally qualified alternate.

---

## 3. TWO EXECUTION OBJECTIVES & HYBRID POLICY

Maestro supports two distinct execution objectives within a single runtime architecture:

| Dimension | Paid Efficiency Policy | Free Quality Policy | Shared Invariant |
| :--- | :--- | :--- | :--- |
| **Primary Goal** | Minimize subscription usage and tokens per verified outcome. | Maximize task success and code quality; inference cost is zero. | Both must satisfy all required test and integration gates. |
| **Graph Topology** | Minimal sufficient graph. Single implementer; no planner/reviewer for routine tasks. | Deeper decomposition. Independent planner, test designer, and candidate branches. | Topology compiled by deterministic rules; planner proposes only. |
| **Candidate Count** | Exactly 1 candidate by default. | 2+ isolated candidates for non-trivial tasks. | Candidates run in strictly isolated worktrees; no proof mixing. |
| **Effort Control** | Bounded/targeted effort to conserve quota. | High/maximum reasoning effort permitted across nodes. | Native effort values must be validated against backend catalog. |
| **Context Window** | Tight, bounded context manifest; exact file slices. | Rich context: full module context, prior failed attempts, architectural notes. | Mandatory instructions and acceptance criteria are never truncated. |
| **Review Strategy** | Reviewer triggered only by high risk or test changes. | Diverse reviewers (e.g. separate security, architectural, logic review). | Reviewer consensus cannot override a failing test command. |

### 3.1 Hybrid Policy
Hybrid execution is an eligibility and quota allocation overlay, NOT a third execution engine:
- Paid subscription capacity is strictly reserved for high-leverage nodes (e.g. complex architectural planning or critical debugging).
- Free tier capacity handles routine implementation, candidate generation, and test design.
- Free nodes MUST NOT consume reserved paid quotas.

---

## 4. AGENT SANDBOX CONFINEMENT & NAMESPACING

### 4.1 Branch Namespacing Invariant (`[INV-A08]`)
All implementer worktrees and branches MUST use strict hierarchical namespacing:
- Implementation branches: `impl/{task_id}`
- Self-fix branches: `selffix/{task_id}_{attempt_id}`
- Redo branches: `redo/{task_id}_{attempt_id}`
- **Negative Invariant**: Flat branch names (e.g. `task-01`) are STRICTLY FORBIDDEN because they prevent directory-level git ref confinement.

### 4.2 Git Commit Boundary & Confinement Invariant
An agent running under physical sandbox confinement (e.g. Linux namespaces, bubblewrap, or firejail) must be permitted to write ONLY to:
1. The assigned task worktree directory: `/path/to/project/.orchestrator/worktrees/{task_id}/`
2. The worktree's own local git directory: `.../{task_id}/.git`
3. The shared git object store: `/path/to/project/.git/objects/` (write permissions required for object creation, safe and non-destructive).
4. The namespaced branch ref and reflog: `/path/to/project/.git/refs/heads/impl/{task_id}` and `.../logs/refs/heads/impl/{task_id}`.

**Security Boundaries**:
- Writing to `/path/to/project/.git/refs/heads/main` or any non-namespaced ref is BLOCKED at the filesystem level.
- Any attempt to write outside the worktree fails closed.
- Masking a parent directory while binding a child leaves a writable tmpfs between them; any write that vanishes on unmount is treated as an execution failure.

---

## 5. MODEL AGENT RUNTIME (`ModelAgentRuntime`)

For bare API model endpoints (e.g. free-tier Google Gemini / OpenAI-compatible endpoints), Maestro provides a supervised, bounded harness:
1. **Event/Tool Loop**: Executes in `maestro/backends/model_runtime.py`. Dispatches strictly allowlisted tools (`read_file`, `write_file`, `run_test`, `grep`).
2. **Turn Checkpointing**: Checkpoints intermediate tool turns and file diffs to `.orchestrator/artifacts/` after every tool execution.
3. **Budget Ceilings**: Enforces hard caps on tool turns (default: 25) and wall-clock execution time.
4. **Transport**: Uses standard `requests` or `httpx` over HTTPS; zero provider-specific SDK dependencies.

## MAESTRO VALIDATION, STAGING & ACCEPTANCE (AGENT SPECIFICATION)

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

## MAESTRO SCHEDULING, RESOURCE ADMISSION & OPERATOR CONTROL (AGENT SPECIFICATION)

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

## MAESTRO REPOSITORY INTELLIGENCE & CONTEXT MANIFESTS (AGENT SPECIFICATION)

```
Document ID:     MGES-REPO-CONTEXT-V2
Target Audience: Autonomous AI Coding Agents / LLM Implementation Engines
Domain:          Code Navigation, AST Extraction, Context Assembly, Query Interfaces
Base Revision:   Revision 2 (2026-09-08)
Status:          Authoritative Repository & Context Specification
```

---

## 1. THE MULTI-LAYER REPOSITORY INTELLIGENCE MODEL

Maestro avoids bloated universal code graphs or heavyweight semantic indexes by establishing a tiered, progressive intelligence architecture:

```
[Layer 0: Index-Free Facts] ──► Git tree, rg, project manifests, Python AST
             │
             ▼
[Layer 1: Tree-Sitter AST]  ──► Parse-error tolerant syntax tree, symbol definitions
             │
             ▼ (Gated by Experiment E2)
[Layer 2: SCIP Navigation]  ──► Precise definition/reference cross-file navigation
             │
             ▼ (On-Demand Gated)
[Layer 3: Joern Data-Flow]  ──► Code property graphs for security/taint analysis only
```

---

## 2. PROVIDER EVALUATION & SELECTION RULES

### 2.1 Provider Order & Technical Rationale (D07)
1. **Layer 0 (Index-Free Baseline)**:
   - Always available, zero setup.
   - Bounded `rg`, `git diff`, Python standard library `ast`, package configuration files.
2. **Layer 1 (Tree-sitter)**:
   - First optional structural provider.
   - **Critical Property**: Exceptional tolerance for syntactically broken or incomplete code states that agent worktrees naturally produce during intermediate edits.
   - Extractor emits parse-error and completeness metadata with every result.
3. **Layer 2 (SCIP - Sourcegraph Code Intelligence Protocol)**:
   - Primary benchmark rival for precise cross-file reference resolution.
   - Requires isolated worktree overlay handling.
4. **Layer 3 (Joern Code Property Graph)**:
   - Invoked exclusively on-demand for specific complex refactoring, data-flow analysis, or security vulnerability tasks.
   - NEVER used as an always-on navigation layer.

### 2.2 Strict Negative Invariants on Repository Intelligence
- `[STRICT_NEGATIVE_CONSTRAINT] NO_TREE_SITTER_GRAPH`: The `tree-sitter-graph` DSL is explicitly rejected; language semantics are our responsibility, and it adds external DSL overhead without semantic benefit.
- `[STRICT_NEGATIVE_CONSTRAINT] NO_SYMDEX`: SymDex indexing MUST NEVER be executed directly on the host server outside an isolated, contained container/worktree.
- `[STRICT_NEGATIVE_CONSTRAINT] NO_LLM_KNOWLEDGE_GRAPHS`: Do NOT extract vector embeddings, neo4j graphs, or LLM-summarized knowledge bases for source code.
- `[STRICT_NEGATIVE_CONSTRAINT] SEARCH_READ_FALLBACK_PERMANENT`: Textual search (`rg`, `find`) and file reading MUST remain permanently supported. Structural providers are purely optional accelerators.

### 2.3 Licensing & Third-Party Gates (`[INV-A09]`)
- Each parser and **each individual language grammar** must have its own verification entry in `maestro/thirdparty.json`.
- Grammars ship independently from parsers and carry distinct open-source licenses.
- `maestro doctor --thirdparty` MUST exit 0 before a grammar is loaded.

---

## 3. THE REPOSITORY QUERY API (`RepositoryQueries`)

Managed by `maestro/repository/queries.py` and backed by the disposable `.orchestrator/repository.sqlite3` index:

```yaml
RepositoryQueries:
  definitions(name: str, snapshot: str) -> list[SymbolDefinition]
  references(symbol: str, snapshot: str, direction: str, depth: int) -> list[Reference]
  importers(file: str, snapshot: str) -> list[ImportRelation]
  affected(changed_inputs: list[str], snapshot: str) -> AffectedAnalysisResult
  tests_for(subjects: list[str], snapshot: str) -> list[TestLinkage]
  context(task: TaskSpec, role: AgentDefinition, snapshot: str, policy: Policy) -> ContextManifest
```

---

## 4. CONTEXT MANIFEST GENERATION (`ContextManifest`)

Agents MUST NOT be fed raw uncurated directories. The context builder (`maestro/repository/context.py`) compiles a role-tailored, budget-bounded `ContextManifest`:

```yaml
ContextManifest:
  snapshot_id: str                  # Pinned Git commit SHA
  task_id: str                      # Target task ID
  role_definition_hash: str         # Applied role contract hash
  policy_hash: str                  # Applied project policy hash
  mandatory_instructions: list[str] # Top-priority system constraints (NEVER truncated)
  acceptance_criteria: list[str]    # Verifiable task goals (NEVER truncated)
  entrypoints: list[str]            # Primary files targeted for edit
  source_refs: list[dict]           # Exact file paths with line ranges and content hashes
  related_tests_and_contracts: list[dict] # Pinned test files and schema references
  relevant_decisions: list[dict]    # Historical architecture decisions applicable to this task
  prior_failure_refs: list[str]     # Diagnostic evidence from earlier failed attempts (if any)
  retrieval_paths: list[str]        # Explored search paths
  freshness: str                    # 'exact_match' | 'partial' | 'stale'
  unresolved_queries: list[str]     # Queries that failed or produced partial results
  omitted_material: list[str]       # Content pruned due to context budget limits
  context_estimate_tokens: int      # Computed prompt token volume
```

### 4.1 Role-Specific Context Bounding Rules
- **Implementer**: Receives mandatory instructions, acceptance criteria, target file slices, local test fixtures, and prior attempt diagnosis. Broad architectural history is omitted.
- **Planner**: Receives system architecture decisions, component dependency maps, public interface signatures, and failure classes. Full source bodies are omitted.
- **Reviewer**: Receives immutable diff between base and candidate, acceptance criteria, and controller test results. Implementer conversational narrative is withheld to avoid confirmation bias.
- **Test Designer**: Receives acceptance criteria and public interfaces. Implementer candidate patch is withheld to prevent copying incorrect implementation assumptions.

## MAESTRO EXPERIMENTS, RESEARCH DISPOSITION & AMENDMENTS (AGENT SPECIFICATION)

```
Document ID:     MGES-EXP-AMENDMENTS-V2
Target Audience: Autonomous AI Coding Agents / LLM Implementation Engines
Domain:          Promotion Experiments, Negative Dispositions, Amendment Traceability
Base Revision:   Revision 2 (2026-09-08)
Status:          Authoritative Evidence & Boundary Specification
```

---

## 1. FOCUSED PROMOTION EXPERIMENTS (E0 – E7)

Uncertain optimizations are NOT pre-implemented. They MUST qualify through focused, empirical promotion experiments:

| ID | Experiment / Owner Phase | Comparison & Measurements | Promotion Rule |
| :--- | :--- | :--- | :--- |
| **E0** | **Backend Qualification**<br>*(P6–P7 / B03, B06–B08)* | Compare installed subscription CLIs and one selected free API endpoint against fixture tasks (exact model transmission, effort control, tool bounds, malformed outputs, quota signals). | Backend promoted ONLY if: zero false capability/enforcement claims; verified effort delivery; clean recovery on malformed output; passes `maestro doctor --thirdparty`. |
| **E1** | **Route & Graph Policy**<br>*(P6–P7 / B02, B04, B14)* | **Paid**: Single implementer vs. Planner + Implementer + Reviewer on token cost, wall time, and pass rates across 6 task classes.<br>**Free**: Single pass vs. Multi-candidate (2+) + Test Designer. | **Paid**: Additional nodes promoted ONLY if they reduce overall task token cost or failure rework.<br>**Free**: Promoted if candidate winner selection increases first-pass acceptance without exceeding latency limits. |
| **E2** | **Repository Context Value**<br>*(P9 / B05, B10)* | Compare baseline lexical/file facts (`rg`) against Tree-sitter AST and SCIP navigation on context size, token efficiency, and implementation accuracy. | Promoted ONLY if structural context demonstrably reduces implementer search loops and context volume on multi-file tasks. |
| **E3** | **Validation Cache & Invalidation**<br>*(P4, P10 / B11, B12)* | Conservative whole-snapshot caching vs. fine-grained affected file/symbol invalidation on cache hit rate and test execution time. | Affected invalidation promoted from shadow mode ONLY if zero false cache hits (zero missed regressions) occur across 50+ consecutive commits. |
| **E4** | **Plan Repair vs. Fresh Retry**<br>*(P10 / B13)* | Compare targeted graph patch (preserving valid upstream nodes) vs. full workflow restart on token consumption and time-to-fix. | Local repair promoted ONLY if it consumes fewer tokens and demonstrates higher fix rate than clean retry. |
| **E5** | **Scheduling Heuristics**<br>*(P8 / B08)* | Compare FIFO queue vs. explainable greedy priority (aging, downstream leverage, resource conflict avoidance). | Greedy scheduler promoted ONLY if it eliminates starvation and reduces median task latency. |
| **E6** | **Durable Substrate Bake-Off**<br>*(P3 / B16)* | **Trigger**: Triggered ONLY if native SQLite WAL + process leases exhibit irreconcilable state corruption or deadlocks under load.<br>**Bake-Off Order**: 1) DBOS, 2) Restate, 3) Temporal. | External engine adopted ONLY if measured crash recovery complexity exceeds native implementation maintenance cost. |
| **E7** | **Operator Autonomy & Telemetry**<br>*(P11 / B15)* | Measure operator interruption rate, false alarms, and Telegram notification relevance. | Promoted if operator interventions decrease while task visibility remains complete. |

---

## 2. RESEARCH DISPOSITION & BOUNDARIES (WHAT IS EXPLICITLY NOT IMPLEMENTED)

| Research Theme | Decision & Seam | Explicit Negative Constraint / Exclusion |
| :--- | :--- | :--- |
| **Durable Execution Engines** | Adopt native SQLite WAL, operation intents/receipts, and process leasing (`P1`/`P3`). | **DO NOT** install Temporal, DBOS, or Restate initially. Do not claim opaque agent internals can be replayed from history. |
| **General Agent Frameworks** | Adopt configured role nodes (`AgentDefinition`) compiled into DAGs (`P5`). | **DO NOT** import LangGraph, AutoGen, CrewAI, or general multi-agent frameworks. |
| **Universal Graph Databases** | Store execution graphs and repository indexes in SQLite (`P1`/`P9`). | **DO NOT** install Neo4j, Memgraph, or RDF/SPARQL engines. |
| **Code Property Graphs** | Tree-sitter for AST (`P9`); on-demand Joern only for deep security tasks. | **DO NOT** build universal CPGs. `tree-sitter-graph` DSL is strictly excluded. |
| **Vector DBs & Embeddings** | Deterministic lexical and AST queries (`P9`). | **DO NOT** introduce vector databases (Chroma, Pinecone, Qdrant) or semantic embedding pipelines. |
| **Automatic Git Cleanups** | Quarantine failed worktrees; mark uncertain for operator review. | **DO NOT** execute automatic destructive `git reset --hard` or prune worktrees without positive proof of quiescence. |
| **Global Host Upgrades** | Per-project adopted version manifests resolved in `bootstrap.py` (`P11`). | **DO NOT** allow global version pointers (`~/.maestro/current`) to silently move an active running project. |

---

## 3. AMENDMENT LOG (REVISION 2, 2026-09-08)

| Target Area | Applied Architectural Change | Concrete Evidence & Rationale |
| :--- | :--- | :--- |
| **D02, D04, D07** | Durability bake-off ordering: DBOS → Restate → Temporal. Credential boundary strictly enforced. Stack Graphs removed from shortlist (upstream archived). | External research review corroborated locally by duplicated external effect and detached child process outliving launcher. |
| **D05, P7** | Free mode reached via API provider with free tier later; local model serving out of scope. Free admission reuses P6 pool contract. | Operator directive (2026-09-08). Local serving benchmark answered wrong operational question. |
| **P0, P12** | Fixtures cover fixed 6-class catalog. Instrumentation captures raw per-task outcomes for B01/B18. P12 measures actual artifact retention volume. | B01 and B18 are evidence-blocked; summaries cannot be re-analyzed. |
| **P1, P3** | Writer lease binds to actual writing PID, NOT launcher PID. | Measured: launcher returned 0 with child process still running; lock was erroneously granted to replacement. |
| **P4** | Adapters declare `effect_class` and non-idempotent adapters require postcondition `probe`. Unprobed non-idempotent adapters never retry automatically. | Measured: blind retry caused duplicate external effect; receipt-guarded retry reconciled cleanly. |
| **P5** | Templates composed from named versioned fragments. Setup records `unresolved[]` facts that fail closed. | Operator clarification on template modularity; 8 init fixtures evaluated. |
| **P6** | Grade acquisition paths (push vs pull). `QuotaObservation` records `observed_at`, `written_at`, `staleness_bound`, `carried_forward`. `effort_delivery` specified. Shape keys added to statistics. | Source inspection of Antigravity status-line payload, quota manager, and installed CLI control surfaces. |
| **P7** | Enforced profiles must permit required writes (commit to own branch). Implementer branches namespaced (`impl/<task>`). Commit boundary experiment proved 3 extra git paths suffice. | Confinement commit experiment: directory-level enforcement requires namespaced branches. |
| **P9, P11** | Parsers and grammars require individual verification rows in `maestro/thirdparty.json`. `bootstrap.py` parses `--repo` from `argv` before re-exec. | License terms apply per artifact. Global pointer previously moved active sibling projects. |
| **E1, E2** | Fixtures qualify as gates only after recorded calibration run. E2 runs on Maestro's own repository and language mix. | Saturated defect fixtures measure fixture sensitivity, not model routing strength. |
| **Durability** | Surviving disk/host loss is an explicit non-goal; backup destination MUST NOT be the same filesystem. | Local process crash durability verified; off-host recovery is out of scope by design. |


---

# PHASE-BY-PHASE IMPLEMENTATION SPECIFICATIONS (P00 – P12)


### PHASE P0: ESTABLISH THE MIGRATION BASELINE (AGENT EXECUTION SPEC)

```
Phase ID:        PHASE-P00
Depends On:      None
Output:          Behavioral fixture suite, measurable baseline metrics, cases.json
Target Files:
  - Create:  tests/graph_engineering/conftest.py
  - Create:  tests/graph_engineering/test_baseline_lanes.py
  - Create:  tests/graph_engineering/fixtures/cases.json
  - Create:  scripts/graph_baseline.py
  - Extend:  maestro/metrics.py
  - Update:  docs/PROGRESS.md
  - Update:  docs/EXECUTION.md
Verification:    .venv/bin/python -m pytest -q tests/graph_engineering/test_baseline_lanes.py
```

---

## 1. OBJECTIVE & DELIVERABLES
Establish a rigorous, reproducible behavioral baseline of existing Maestro execution lanes before modifying control or state architecture. Capture execution traces, duplicate calls, waits, test runs, touched snapshots, and lane transitions under isolated fixtures.

---

## 2. IMPLEMENTATION CHECKLIST
- [ ] **Characterization Fixtures**: Implement `tests/graph_engineering/fixtures/cases.json` covering the complete fixed six-class task catalog:
  1. `bug`: Ordinary bugfix in existing code.
  2. `feature`: Standard feature addition with new test.
  3. `documentation`: Markdown/doc update.
  4. `difficult_debugging`: Complex multi-file failure requiring diagnostic reasoning.
  5. `manual_prep`: Human-in-the-loop task requiring manual step.
  6. `recovery`: Interrupted run or quota exhaustion recovery.
- [ ] **Lane Capture Harness**: Capture behavior across current execution lanes (`maestro/orchestrator.py:1924`):
  - Ordinary change lane
  - Script / no-op lane
  - Manual preparation lane
  - Async wait lane
  - Resumable work lane
  - Quota switch and context rotation
  - Risky / proof review and graduation
- [ ] **Instrumentation & Metrics**: Extend `maestro/metrics.py` to record:
  - Duplicate agent invocations
  - Unnecessary test re-runs
  - Idle wait time vs active compute
  - State file read/write operations
  - Inferences vs verified deterministic observations
- [ ] **Scratch Project Pilot**: Run an instrumented setup rehearsal and one real coding task on an authorized scratch project. Retain raw per-task outcomes (every rework attempt and human intervention) to close the first half of B01.

---

## 3. ACCEPTANCE CRITERIA & VERIFICATION
- **Acceptance Condition**:
  - Each lane produces a deterministic, reproducible trace.
  - Fixture execution is strictly isolated and CANNOT write to any live project directory.
  - Metrics distinguish empirical observation from inference.
- **Focused Regression Trigger**:
  - A fake async wait MUST NOT consume an agent slot.
  - A no-op script MUST NOT trigger an agent retry.
- **Execution Command**:
  ```bash
  .venv/bin/python -m pytest -q tests/graph_engineering/test_baseline_lanes.py
  ```\n

### PHASE P1: INTRODUCE TRANSACTIONAL CONTROL & SINGLE WRITER (AGENT EXECUTION SPEC)

```
Phase ID:        PHASE-P01
Depends On:      P00
Output:          SQLite WAL command/event store, content-addressed artifact store, single writer
Target Files:
  - Create:  maestro/control/models.py
  - Create:  maestro/control/store.py
  - Create:  maestro/control/lifecycle.py
  - Create:  maestro/control/artifacts.py
  - Create:  tests/control/test_store.py
  - Create:  tests/control/test_migration.py
  - Create:  tests/control/test_artifacts.py
  - Modify:  maestro/state.py
  - Modify:  maestro/paths.py
  - Modify:  maestro/hitl/commands.py
  - Modify:  maestro/watchdog.py
Verification:    .venv/bin/python -m pytest -q tests/control
```

---

## 1. OBJECTIVE & DELIVERABLES
Introduce `.orchestrator/control.sqlite3` as the single transactional state authority. Move state and event mutation behind a single-writer controller with an idempotent command inbox and append-only event store. Implement content-addressed artifact storage.

---

## 2. IMPLEMENTATION CHECKLIST
- [ ] **Database & Schema**: Implement `maestro/control/store.py` initializing `.orchestrator/control.sqlite3` in WAL mode with foreign keys and `busy_timeout=10000`.
- [ ] **Idempotent Command Inbox**: Create `command_inbox` table. External callers (CLI, Telegram) enqueue commands; controller ingests and executes them idempotently.
- [ ] **Event Sourcing & Projections**: Implement `events` table and reducers in `lifecycle.py` for task runs, node phases, attempts, and leases.
- [ ] **Content-Addressed Artifacts**: Implement `maestro/control/artifacts.py` storing files by SHA256 in `.orchestrator/artifacts/sha256/xx/yy/hash`. Enforce atomic tempfile write -> fsync -> rename.
- [ ] **Process Leasing & PID Binding**: Implement `writer_leases` with `writer_pid` binding to leaf process PID, not launcher PID (`[INV-02]`).
- [ ] **Legacy Shadowing & Export**: Shadow legacy `state.json` and journal; export sequence-stamped JSON projections after each SQLite commit. Legacy files become read-only outputs.

---

## 3. ACCEPTANCE CRITERIA & ROLLBACK
- **Acceptance Condition**:
  - Duplicate commands commit once; stale revisions fail CAS checks.
  - Replay of event stream reconstructs exact projections without external I/O.
  - A crash between artifact creation and SQL commit leaves only an unreferenced orphan artifact, never a missing referenced one.
  - Legacy files cannot independently overwrite SQLite authority.
- **Rollback Protocol**:
  - Prior to new execution side effects: restore quiescent legacy backup.
  - After side effects begin: down-export current state at a quiescent boundary.
- **Execution Command**:
  ```bash
  .venv/bin/python -m pytest -q tests/control
  ```\n

### PHASE P2: MAKE ROADMAP A VALIDATED, REVISIONED DEPENDENCY MODEL (AGENT EXECUTION SPEC)

```
Phase ID:        PHASE-P02
Depends On:      P01
Output:          Strict roadmap parser, mutation API, cycle detector, CAS projection
Target Files:
  - Create:  maestro/taskgraph.py
  - Create:  tests/test_taskgraph.py
  - Adapt:   maestro/docs/roadmap.py
  - Adapt:   maestro/docs/complete.py
  - Adapt:   maestro/docs/consistency.py
  - Adapt:   maestro/docs/depmap.py
  - Adapt:   maestro/docs/upcoming.py
  - Adapt:   maestro/status.py
  - Adapt:   maestro/cli.py
Verification:    .venv/bin/python -m pytest -q tests/test_taskgraph.py tests/characterization/test_roadmap.py tests/characterization/test_consistency.py
```

---

## 1. OBJECTIVE & DELIVERABLES
Replace loose regex/text parsing of `ROADMAP.md` with a strict AST and dependency model (`maestro/taskgraph.py`). Persist task definitions and dependency graphs in SQLite with revision control, cycle detection, and CAS document synchronization.

---

## 2. IMPLEMENTATION CHECKLIST
- [ ] **Normalization & Schema**: Normalize roadmap markdown YAML blocks into `TaskSpec`. Preserve surrounding free-form prose and unknown fields in `legacy_fields`.
- [ ] **Strict Validation**: Fail with exact line/block locations for malformed YAML, duplicate IDs, missing prerequisites, or cycles using `graphlib.TopologicalSorter`.
- [ ] **Mutation API**: Implement transactional roadmap mutation commands:
  - `add_task(spec)`
  - `add_dependency(child, parent)`
  - `split_task(parent_id, child_specs[])`: Parent becomes a group; entry children inherit prerequisites, exit children satisfy downstream edges.
  - `supersede_task(old_id, new_id)`
  - `invalidate_inputs(task_id)`
- [ ] **CLI Commands**: Implement `maestro roadmap validate` and `maestro roadmap apply` with compare-and-swap (CAS) document hash validation.
- [ ] **Conflict Handling**: If `ROADMAP.md` is hand-edited while controller is active, flag document drift conflict and block affected dispatch; NEVER overwrite human edits.

---

## 3. ACCEPTANCE CRITERIA & VERIFICATION
- **Acceptance Condition**:
  - Cyclic dependencies (e.g. `A -> B -> C -> A`) fail with exact cycle paths.
  - Task splits preserve all incoming and outgoing obligations.
  - Legacy completions imported as `legacy_accepted`; they satisfy roadmap dependencies but CANNOT manufacture fresh test evidence or cache hits.
- **Execution Command**:
  ```bash
  .venv/bin/python -m pytest -q tests/test_taskgraph.py tests/characterization/test_roadmap.py tests/characterization/test_consistency.py
  ```\n

### PHASE P3: EXECUTE EXISTING LANES AS DURABLE WORKFLOW NODES (AGENT EXECUTION SPEC)

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

### PHASE P4: MAKE EVIDENCE AND STAGING INTEGRATION AUTHORITATIVE (AGENT EXECUTION SPEC)

```
Phase ID:        PHASE-P04
Depends On:      P03
Output:          Validation engine, cryptographic evidence registry, isolated staging worktree
Target Files:
  - Create:  maestro/validation.py
  - Create:  tests/test_validation_evidence.py
  - Create:  tests/test_integration_acceptance.py
  - Adapt:   maestro/verifications.py
  - Adapt:   maestro/gates.py
  - Adapt:   maestro/merge.py
  - Adapt:   maestro/adapters.py
Verification:    .venv/bin/python -m pytest -q tests/test_validation_evidence.py tests/test_integration_acceptance.py tests/characterization/test_gates.py tests/characterization/test_merge.py
```

---

## 1. OBJECTIVE & DELIVERABLES
Establish authoritative quality gating (`maestro/validation.py`). Eliminate self-reported agent "passes". Execute checks on frozen snapshots, generate cryptographic evidence receipts, enforce isolated staging merges, and protect the target branch.

---

## 2. IMPLEMENTATION CHECKLIST
- [ ] **Evidence Data Structures**: Implement `VerificationSpec`, `VerificationEvidence`, and `AcceptanceDecision`.
- [ ] **Closed-World Evaluation**: Validate checks against required criterion keys. Missing proof keys or empty reviewer outputs fail closed (`FAILED`).
- [ ] **Exact Snapshot Fingerprinting**: Cache check results ONLY for identical `(source_snapshot_id, command_hash, input_fingerprint, environment_fingerprint)` tuples.
- [ ] **5-Stage Staging Pipeline**:
  1. Freeze candidate worktree.
  2. Merge candidate into fresh target branch snapshot inside `.orchestrator/worktrees/staging/`.
  3. Run mandatory integration checks on merged snapshot.
  4. Fast-forward `project.main_branch` under exclusive target lease.
  5. Execute external side-effect nodes (push/deploy) with intent/receipt.
- [ ] **Postcondition Probes (`[INV-A05]`)**: Non-idempotent adapters without postcondition probes MUST NOT be retried automatically.

---

## 3. ACCEPTANCE CRITERIA & VERIFICATION
- **Acceptance Condition**:
  - An agent-authored `DONE` or `tests.pass` file CANNOT accept a task.
  - Source code drift immediately invalidates cached check evidence.
  - Two candidate worktrees cannot cross-pollinate proofs or patches.
  - Advancement of target branch during validation forces re-integration on fresh target head.
  - Failed staging check leaves target branch completely untouched.
- **Execution Command**:
  ```bash
  .venv/bin/python -m pytest -q tests/test_validation_evidence.py tests/test_integration_acceptance.py tests/characterization/test_gates.py tests/characterization/test_merge.py
  ```\n

### PHASE P5: CONSTRUCT PROJECT WORKFLOWS & DISTINCT AGENT DEFINITIONS (AGENT EXECUTION SPEC)

```
Phase ID:        PHASE-P05
Depends On:      P04
Output:          Applied project policy, safe maestro init, distinct role definitions
Target Files:
  - Create:  maestro/workflows/policy.py
  - Create:  maestro/templates/workflows/project.yaml
  - Create:  role profile templates (implementer, planner, reviewer, test_designer, diagnoser)
  - Create:  tests/workflows/test_policy.py
  - Create:  tests/workflows/test_compiler.py
  - Create:  tests/test_init_workflows.py
  - Modify:  maestro/cli.py
  - Modify:  maestro/config.py
Verification:    .venv/bin/python -m pytest -q tests/workflows/test_policy.py tests/workflows/test_compiler.py tests/test_init_workflows.py tests/test_templates.py
```

---

## 1. OBJECTIVE & DELIVERABLES
Implement project engineering policy (`workflows/project.yaml`), safe repository fact discovery in `maestro init`, and distinct, reusable `AgentDefinition` contracts for specialized cognitive roles.

---

## 2. IMPLEMENTATION CHECKLIST
- [ ] **Project Policy Schema**: Implement `maestro/workflows/policy.py` parsing `workflows/project.yaml` (templates, quality gates, permissions, objective).
- [ ] **Safe `maestro init` Discovery**:
  - Discover project facts from tracked manifests (pyproject.toml, package.json, Makefile, CI workflows).
  - **Negative Invariant**: MUST NOT execute arbitrary project code during discovery.
  - Record facts with source path, hash, and confidence. Record `unresolved[]` facts that fail closed.
  - Surface proposed changes as `.new` diffs; never overwrite existing project files.
- [ ] **Modular Template Composition (`[INV-A06]`)**: Compose templates from named, versioned fragments. The compiler's invariant is the minimal shape that satisfies required criteria.
- [ ] **Specialized Agent Roles**:
  - Implementer: Owns code edit in assigned worktree.
  - Planner: Read-only context analysis; emits `PlanProposal`.
  - Reviewer: Scrutinizes diff against criteria; emits findings; no write permissions.
  - Test Designer: Generates regression/adversarial tests in scratch workspace.
  - Diagnoser: Formulates falsifiable failure hypotheses on repeat errors.

---

## 3. ACCEPTANCE CRITERIA & VERIFICATION
- **Acceptance Condition**:
  - Identical inputs produce identical compiled workflow content hashes.
  - Rerunning `maestro init` leaves active flags and existing files untouched.
  - A low-risk task omits planner and reviewer in Paid Efficiency Mode.
  - High-risk changes enforce mandatory independent review.
- **Execution Command**:
  ```bash
  .venv/bin/python -m pytest -q tests/workflows/test_policy.py tests/workflows/test_compiler.py tests/test_init_workflows.py tests/test_templates.py
  ```\n

### PHASE P6: ROUTE EVERY AI CALL BY CAPABILITY, MODEL & EFFORT (AGENT EXECUTION SPEC)

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

### PHASE P7: DELIVER FREE-MODEL & HYBRID EXECUTION (AGENT EXECUTION SPEC)

```
Phase ID:        PHASE-P07
Depends On:      P06
Output:          ModelAgentRuntime, multi-candidate groups, sandbox branch namespacing
Target Files:
  - Create:  maestro/backends/model_api.py
  - Create:  maestro/backends/model_runtime.py
  - Create:  tests/backends/test_model_runtime.py
  - Create:  tests/workflows/test_mode_policies.py
  - Create:  tests/workflows/test_candidates.py
  - Create:  tests/test_confinement.py
Verification:    .venv/bin/python -m pytest -q tests/backends/test_model_runtime.py tests/workflows/test_mode_policies.py tests/workflows/test_candidates.py tests/test_confinement.py
```

---

## 1. OBJECTIVE & DELIVERABLES
Implement `ModelAgentRuntime` for API endpoints with free tiers. Enable Free Quality Mode with multi-candidate exploration (2+ isolated candidates). Enforce branch namespacing and directory-level sandbox confinement.

---

## 2. IMPLEMENTATION CHECKLIST
- [ ] **`ModelAgentRuntime` Loop**: Implement bounded model/tool supervisor loop using standard HTTPS transport; handle turn checkpointing and strict tool allowlists.
- [ ] **Free Quality Compilation**:
  - Compile workflows with independent test designers, 2+ isolated candidate branches, and diverse reviewers.
  - Zero token-cost penalty in free mode.
- [ ] **Branch Namespacing (`[INV-A08]`)**:
  - Namespace all implementer branches: `impl/<task>`, `selffix/<id>`, `redo/<id>`. Prohibit flat branch names.
- [ ] **Commit Boundary Confinement**:
  - Confined implementer can write ONLY to: task worktree, worktree `.git`, shared `.git/objects/`, and its own namespaced ref `refs/heads/impl/<task>`.
  - Target branch `refs/heads/main` is physically blocked.
- [ ] **Candidate Selection Join**:
  - Candidate join evaluates all candidates and selects exactly one winner. NEVER mix patches or proofs across candidates (`[INV-06]`).

---

## 3. ACCEPTANCE CRITERIA & VERIFICATION
- **Acceptance Condition**:
  - Identical task spec succeeds across paid, free, and hybrid fixture backends.
  - Free compilation has zero token cost penalty.
  - Candidates never share writer worktrees.
  - Zero paid capacity in free mode never launches a subscription call.
- **Execution Command**:
  ```bash
  .venv/bin/python -m pytest -q tests/backends/test_model_runtime.py tests/workflows/test_mode_policies.py tests/workflows/test_candidates.py tests/test_confinement.py
  ```\n

### PHASE P8: DEEPEN RESOURCE ADMISSION & SCHEDULING (AGENT EXECUTION SPEC)

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

### PHASE P9: ADD REPOSITORY FACTS & STRUCTURAL CONTEXT (AGENT EXECUTION SPEC)

```
Phase ID:        PHASE-P09
Depends On:      P04, P05 (routed context after P06)
Output:          Repository query API, Tree-sitter provider, ContextManifest generator
Target Files:
  - Create:  maestro/repository/queries.py
  - Create:  maestro/repository/index.py
  - Create:  maestro/repository/providers.py
  - Create:  maestro/repository/context.py
  - Create:  tests/repository/test_queries.py
  - Create:  tests/repository/test_overlays.py
  - Create:  tests/repository/test_context.py
Verification:    .venv/bin/python -m pytest -q tests/repository
```

---

## 1. OBJECTIVE & DELIVERABLES
Implement Layer 0 (index-free file/git facts) and optional Layer 1 (Tree-sitter AST extraction) repository intelligence. Build role-tailored `ContextManifest` objects. Enforce grammar licensing verification.

---

## 2. IMPLEMENTATION CHECKLIST
- [ ] **Layer 0 Index-Free Queries**: Implement baseline queries (`definitions`, `references`, `importers`) using `rg`, `git`, and Python `ast`.
- [ ] **Layer 1 Tree-Sitter Integration**:
  - Integrate Tree-sitter parser. Enforce parse-error tolerance for intermediate code.
  - Overlay worktree edits on top of base snapshot index.
- [ ] **Grammar Licensing Gate (`[INV-A09]`)**:
  - Add individual verification entries for the parser and EACH enabled grammar in `maestro/thirdparty.json`. Verify `maestro doctor --thirdparty` exits 0.
- [ ] **ContextManifest Assembly**:
  - Implement `maestro/repository/context.py` building bounded manifests tailored to role contracts.
  - Mandatory instructions and criteria are NEVER truncated.

---

## 3. ACCEPTANCE CRITERIA & VERIFICATION
- **Acceptance Condition**:
  - Stale queries are rejected or flagged partial.
  - Worktree overlays cannot cross-contaminate.
  - Malformed syntax falls back safely to search/read.
  - Search and read fallbacks remain fully functional if Tree-sitter is absent.
- **Execution Command**:
  ```bash
  .venv/bin/python -m pytest -q tests/repository
  ```\n

### PHASE P10: ENABLE QUALIFIED AFFECTED VALIDATION & LOCAL PLAN REPAIR (AGENT EXECUTION SPEC)

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

### PHASE P11: HARDEN OPERATOR VIEWS, VERSION ADOPTION & SERVICE LIFECYCLE (AGENT EXECUTION SPEC)

```
Phase ID:        PHASE-P11
Depends On:      P01–P07
Output:          CLI/HTML workflow views, per-project bootstrap version pin, Type=forking TMUX service
Target Files:
  - Modify:  maestro/status.py
  - Modify:  maestro/cli.py
  - Modify:  maestro/hitl/commands.py
  - Modify:  maestro/hitl/telegram.py
  - Modify:  maestro/hitl/dan_request.py
  - Modify:  maestro/selfupdate.py
  - Modify:  maestro/bootstrap.py
  - Modify:  maestro/watchdog.py
  - Create:  tests/test_workflow_status.py
  - Create:  tests/test_graph_update_compatibility.py
  - Create:  tests/ops/test_graph_service_lifecycle.py
Verification:    .venv/bin/python -m pytest -q tests/test_workflow_status.py tests/test_graph_update_compatibility.py tests/ops/test_graph_service_lifecycle.py tests/test_selfupdate.py tests/test_bootstrap.py
```

---

## 1. OBJECTIVE & DELIVERABLES
Harden operator visibility tools (`workflow show --html`, `explain`). Implement per-project runtime version pinning in `bootstrap.py` to prevent cross-project update drift. Generate TMUX-isolated systemd service definitions.

---

## 2. IMPLEMENTATION CHECKLIST
- [ ] **Operator Projections**:
  - Implement `maestro workflow show <task_id> --html` (self-contained static interactive graph/timeline).
  - Implement `maestro explain <task_id>` (causal trace of criteria -> binding -> evidence -> acceptance).
- [ ] **Per-Project Bootstrap Pin (`[INV-09]`, `[INV-A09]`)**:
  - Modify `bootstrap.reexec_into_adopted_version`: parse `--repo` / `$MAESTRO_REPO` directly from `argv` using standard library before importing any Maestro modules. Prefer per-project pin over machine-global `~/.maestro/current`.
- [ ] **TMUX Service Generation (`[INV-10]`)**:
  - Generate systemd service adhering to: `Type=forking` and `ExecStart=/usr/bin/tmux new-session -d -s maestro -n main 'cd /app && ./run.sh'`.

---

## 3. ACCEPTANCE CRITERIA & VERIFICATION
- **Acceptance Condition**:
  - Adopting a new Maestro version in one project CANNOT alter another project's active runtime.
  - Service restart or controller crash never duplicates active workers.
  - HTML timeline export contains zero secret-bearing artifacts or credentials.
- **Execution Command**:
  ```bash
  .venv/bin/python -m pytest -q tests/test_workflow_status.py tests/test_graph_update_compatibility.py tests/ops/test_graph_service_lifecycle.py tests/test_selfupdate.py tests/test_bootstrap.py
  ```\n

### PHASE P12: QUALIFY COMPLETE EVOLUTION BEFORE LIVE ADOPTION (AGENT EXECUTION SPEC)

```
Phase ID:        PHASE-P12
Depends On:      Core phases + P11 (all feature experiment outcomes recorded)
Output:          End-to-end qualification report, raw task outcomes for B01/B18, release tag
Target Files:
  - Create:  tests/graph_engineering/test_end_to_end.py
  - Create:  scripts/graph_qualification.py
  - Update:  docs/DESIGN.md
  - Update:  docs/EXECUTION.md
  - Update:  docs/PROGRESS.md
Verification:    .venv/bin/python -m pytest -q && .venv/bin/python scripts/graph_qualification.py --repo /tmp/maestro-graph-qualification --fixture-backends
```

---

## 1. OBJECTIVE & DELIVERABLES
Execute full-system end-to-end qualification across script, subscription, free, and hybrid fixture backends. Execute real tasks on two scratch projects. Retain raw per-task outcomes to close B01 and B18. Measure actual artifact volume to establish retention defaults (B15).

---

## 2. IMPLEMENTATION CHECKLIST
- [ ] **End-to-End Test Suite**: Run full pytest suite and fault matrix (transaction rollback, lost controller, writer crash, quota switch, interrupted integration).
- [ ] **Scratch Project Pilot**:
  - Run representative tasks on two independent scratch repositories under each qualified mode.
  - Retain raw per-task outcomes (transcripts, rework counts, tool logs) rather than summaries.
- [ ] **Artifact Volume Measurement (`[INV-A03]`, B15)**:
  - Measure actual bytes of artifacts and transcripts produced during qualification run.
  - Set default retention settings in `maestro init` based on empirical measurement.
- [ ] **Qualification Script**: Implement `scripts/graph_qualification.py`. Refuse non-scratch target directory unless explicit deployment flag is provided.

---

## 3. ACCEPTANCE CRITERIA & VERIFICATION
- **Acceptance Condition**:
  - All 12 system invariants pass.
  - Every uncertain optimization has an enabled/disabled decision backed by empirical evidence.
  - Unqualified backends remain labeled unqualified; mock passes cannot claim production readiness.
- **Execution Command**:
  ```bash
  .venv/bin/python -m pytest -q
  .venv/bin/python scripts/graph_qualification.py --repo /tmp/maestro-graph-qualification --fixture-backends
  ```\n

### MIGRATION GUARDRAILS (CROSS-CUTTING INVARIANTS FOR EVERY PHASE)

```
Document ID:     MGES-GUARDRAILS-V2
Target Audience: Autonomous AI Coding Agents / LLM Implementation Engines
Scope:           Mandatory rules governing all phase implementations P00 through P12
```

---

## 1. THE THIRD-PARTY VERIFICATION GATE (`maestro doctor --thirdparty`)
- Any new or materially updated backend CLI, or any optional external dependency/grammar, MUST pass implementation-time capability, version, licensing, and conformance verification before being marked supported.
- Verification is recorded against the exact installed version in `maestro/thirdparty.json`.
- `maestro doctor --thirdparty` is the gate: it MUST exit 0. It exits non-zero on an unverified or drifted subject that is in use.

---

## 2. SHADOW MODE & MUTUAL EXCLUSION
- Shadow mode observes; it NEVER executes a second copy of an external side effect.
- The legacy runner and graph runner are **mutually exclusive** per project run.
- Compatibility facades are retained only while callers are being migrated; duplicate state writers and duplicate roadmap parsers MUST be deleted at their cutover.
- Existing project data and uncommitted work MUST be preserved.

---

## 3. STRICT BOUNDARIES
- No optimization may lower established quality gates to meet a performance or token target.
- The critical path is strictly: `P0 -> P1 -> P2 -> P3 -> P4 -> P5 -> P6 -> P7 -> P11 -> P12`.
- P8–P10 are developed only after their prerequisite inputs exist and remain OFF by default until qualified under Experiments E1–E5.\n
