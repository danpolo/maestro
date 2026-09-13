# MAESTRO GRAPH ENGINEERING: AGENT SYSTEM SPECIFICATION & CONTEXT DISPATCHER

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
| **P10B: Host Resource Admission** | `phases/P10B_HOST_RESOURCE_ADMISSION.md`, `05_SCHEDULING_AND_RESOURCES.md` | `02_EXECUTION_ENGINE_AND_DURABILITY.md` | `.venv/bin/python -m pytest -q tests/test_host_resource_admission.py tests/test_scheduling.py tests/control/test_resources.py` |
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
         │                                                    ▼
         │                                         [P10B: Host Resource Admission]
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
