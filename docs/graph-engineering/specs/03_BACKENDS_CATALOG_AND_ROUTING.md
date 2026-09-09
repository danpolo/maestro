# MAESTRO BACKEND CATALOG & MODEL ROUTING (AGENT SPECIFICATION)

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
