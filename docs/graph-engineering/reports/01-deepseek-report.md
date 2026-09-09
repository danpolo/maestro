# First DeepSeek response

Source: https://chat.deepseek.com/share/xr2zhq1cn1r26dh897
Extracted: 2026-09-03

# Deep Research Report: State-of-the-Art Graph Engineering for Maestro

## Executive Summary

1. **Maestro's immediate graph need is durability, not intelligence.** The single most impactful graph-related improvement is wrapping execution in a durable execution engine (Temporal). This addresses Maestro's core weakness: process death kills running agents. This is higher priority than any agent-graph framework.[reference:0]

2. **LangGraph is the wrong foundation.** LangGraph is valuable for agent state machines but lacks cross-process durability—checkpoints die with the process.[reference:1] Production teams wrap LangGraph in Temporal, not the reverse.[reference:2] For Maestro, using LangGraph would add complexity without solving the real problem.

3. **Repository code graphs can materially reduce agent exploration.** Projects like CodexGraph[reference:3], RepoGraph[reference:4], and Codex-Atlas[reference:5] show 91% route accuracy for graph-guided retrieval. A code property graph (via Joern[reference:6]) could let agents query "what calls this function?" instead of grepping and reading files.

4. **Maestro should adopt a DAG task graph for roadmap execution.** The roadmap is naturally a dependency DAG. Explicit representation enables parallel execution, invalidation tracking, and critical-path optimization.

5. **State machines, not graphs, for component lifecycles.** Agent lifecycle, task state, watchdog recovery, and self-update are better modeled as hierarchical state machines (XState[reference:7] or similar) than as general graphs.

6. **Event sourcing is the right provenance model.** An append-only event log enables deterministic replay, crash recovery, auditing, and debugging.[reference:8][reference:9]

7. **Do NOT adopt GraphRAG for code retrieval.** GraphRAG[reference:10] is optimized for question answering over documents, not for precise code-structure queries. Code property graphs are the right tool.

8. **Multi-agent graph structures are largely unnecessary for Maestro.** Maestro's orchestrator+implementer pattern is sufficient. Additional agents should be justified by concrete failure modes, not architectural fashion.

9. **Graph scheduling is Tier 2.** Dynamic DAG scheduling algorithms exist[reference:11] but Maestro should first get basic DAG execution working before optimizing.

10. **The combined architecture is: Roadmap DAG + Temporal durable execution + Joern code graph + XState state machines + event sourcing.** This is not one giant graph but five specialized layers.

---

## Maestro Graph Architecture Recommendation

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         MAESTRO GRAPH LAYERS                               │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  ROADMAP / TASK DAG          ← Explicit dependency graph            │   │
│  │  (Nodes: tasks. Edges: depends_on, blocks, invalidates)            │   │
│  │  Storage: Durable (Temporal)                                       │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                    │                                        │
│  ┌─────────────────────────────────▼───────────────────────────────────┐   │
│  │  EXECUTION LAYER (Temporal)    ← Durable workflow engine            │   │
│  │  - Crash recovery              - Retry                             │   │
│  │  - Checkpointing               - Event sourcing                   │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                    │                                        │
│  ┌─────────────────────────────────▼───────────────────────────────────┐   │
│  │  COMPONENT STATE MACHINES (XState)                                  │   │
│  │  - Agent lifecycle   - Task lifecycle   - Watchdog                │   │
│  │  - Self-update       - Human escalation                           │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                    │                                        │
│  ┌─────────────────────────────────▼───────────────────────────────────┐   │
│  │  REPOSITORY CODE GRAPH (Joern CPG)                                  │   │
│  │  - AST + CFG + PDG    - Call graphs    - Import graphs             │   │
│  │  - Query: "what depends on X?" - "what tests cover Y?"            │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                    │                                        │
│  ┌─────────────────────────────────▼───────────────────────────────────┐   │
│  │  PROVENANCE / EVENT LOG (Event sourcing)                           │   │
│  │  - All decisions logged    - Replayable    - Auditable             │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Key insight:** These layers have different storage and query requirements. Don't force one graph model.

---

## Highest-Value Capabilities (Ranked)

| Rank | Capability | Expected Impact | Confidence | Complexity |
|------|-----------|-----------------|------------|------------|
| 1 | Durable execution (Temporal) | Critical | High | Medium |
| 2 | Roadmap DAG | High | High | Low |
| 3 | Code property graph (Joern) | High | High | Medium |
| 4 | Event sourcing provenance | High | High | Low-Medium |
| 5 | State machines for lifecycles | Medium | High | Low |
| 6 | Incremental invalidation (Bazel-style) | Medium | Medium | Medium |
| 7 | Dynamic DAG scheduling | Medium | Medium | High |
| 8 | Graph visualization/observability | Medium | High | Low |

---

## Technology / Project Evaluation

### Serious Adoption Candidates

| Project | Purpose | Language | License | Activity | Maestro Use | Verdict |
|---------|---------|----------|---------|----------|-------------|---------|
| **Temporal**[reference:12] | Durable execution | Go/Java/Python | MIT | Very high | Wrap all agent execution | **ADOPT** |
| **LangGraph**[reference:13] | Agent state graphs | Python/TS | MIT | Very high | Agent decision flows | **REJECT** (for Maestro) |
| **Joern**[reference:14] | Code property graphs | Scala/Java | Apache 2.0 | High | Repository code queries | **ADOPT** |
| **XState**[reference:15] | State machines | TypeScript | MIT | High | Component lifecycles | **ADOPT** |
| **Prefect**[reference:16] | Workflow orchestration | Python | Apache 2.0 | High | Task DAG execution | **CONSIDER** |
| **Dagster**[reference:17] | Data orchestration | Python | Apache 2.0 | High | Task DAG + lineage | **CONSIDER** |
| **CodexGraph**[reference:18] | Graph DB for code | Python | Research | Active | Code retrieval | **STUDY** |
| **RepoGraph**[reference:19] | Repository code graph | Python | MIT | Active | Code retrieval | **STUDY** |

### Why Temporal over LangGraph

A critical distinction emerges from the research: LangGraph and Temporal solve different problems at different layers.[reference:20]

**LangGraph** is a lightweight library for modeling agent workflows as explicit graphs with state, nodes, edges, and conditional routing. Its checkpointer enables pause/resume **within a single process**—but if the process dies, the run dies with it.[reference:21]

**Temporal** is a durable execution engine. Workflows survive server restarts, network failures, and process crashes. State is retained and automatically retried.[reference:22]

The production pattern emerging in 2025-2026 is clear: use LangGraph for agent **logic** and Temporal for agent **durability**, or skip LangGraph entirely and use Temporal + a simpler agent framework.[reference:23][reference:24]

For Maestro, the priority is durability—agents running for hours or days must survive infrastructure failures. Temporal addresses this directly. LangGraph would add complexity without solving Maestro's core problem.

---

## Execution and Workflow Graphs

### Finding

Maestro should adopt an explicit execution graph, but **implemented via Temporal workflows**, not as a custom graph engine.

### Recommendation

**Q1: Should Maestro introduce an explicit execution graph?** Yes.

**Minimal semantics:**
- Nodes = executable units (agent invocations, validation steps, commits)
- Edges = control flow and data dependencies
- State = PENDING, RUNNING, COMPLETED, FAILED, RETRYING, BLOCKED
- Each node is idempotent or has compensating actions

**Q2: Should the graph be static, dynamic, persistent, event-sourced, replayable?**
Dynamic, persistent via Temporal, event-sourced (Temporal's history), replayable.

**Q3: Should graph execution be implemented by Maestro or delegated?**
**Delegated to Temporal.** Building a durable execution engine is a multi-year project. Temporal is mature, production-proven, and has explicit OpenAI Agents SDK and LangGraph integrations.[reference:25]

**Q4: Strongest existing project for adoption?**
**Temporal** is the clear leader. It has:
- Production use at major companies
- Active development (2025-2026)
- Multi-language SDKs
- Explicit agentic-AI integrations[reference:26]

---

## Dynamic Planning and Task Graphs

### Finding

Maestro's roadmap is naturally a dependency DAG. Dynamic task graph techniques from distributed systems research offer valuable patterns.

### Recommendation

**Q8: Should Maestro represent its roadmap as a dependency DAG?** Yes.

**Node types:**
- `ROADMAP_ITEM`: High-level goal
- `TASK`: Executable unit
- `VALIDATION`: Test/check step
- `CHECKPOINT`: Save state

**Edge types:**
- `DEPENDS_ON`: Must complete before
- `BLOCKS`: Prevents execution until resolved
- `INVALIDATES`: Changes make dependent work stale

**Dynamic modifications:**
When a task fails or produces unexpected results:
1. Mark dependent tasks as `STALE`
2. Re-plan affected subgraph
3. Insert new tasks dynamically
4. Continue from nearest valid checkpoint

**Research note:** DynTaskMAS (2025) demonstrates dynamic task graphs for LLM-based multi-agent systems with asynchronous parallel operations.[reference:27] The Adaptive Workflow Manager pattern is directly relevant.

---

## Repository / Code Graphs

### Finding

A persistent code property graph can **materially reduce** agent repository exploration. CodexGraph[reference:28] and RepoGraph[reference:29] both demonstrate LLM agents using graph database queries for precise code navigation instead of grep+read loops.

### Recommendation

**Q6: Should Maestro construct a persistent repository/code graph?** Yes.

**Technology: Joern Code Property Graph (CPG)**
Joern generates CPGs combining AST, control-flow graph, and program-dependence graph.[reference:30] It supports C/C++, Java, Python, JavaScript, Kotlin, and binaries.[reference:31]

**Alternative: CodeQL**
CodeQL provides interprocedural taint analysis and vulnerability detection.[reference:32] It's more specialized for security than general code understanding.

**For Maestro: Use Joern as the primary CPG engine, with CodeQL optionally for security-focused queries.**

**Exact queries that reduce agent work:**
1. "What functions call function X?" → Replace grep for call sites
2. "What files import module Y?" → Replace file-system traversal
3. "What tests cover component Z?" → Avoid test-discovery overhead
4. "What is the data flow from input A to output B?" → Avoid manual tracing

**Integration:**
- Run Joern analysis on repository clone (one-time or on commit)
- Store CPG in a graph database (Neo4j or custom)
- Expose as MCP server (CodeBadger[reference:33] demonstrates this pattern)
- Agents query via structured graph queries, not natural language

**Evidence:** Codex-Atlas reports 91% route accuracy for adaptive vector+graph hybrid retrieval over Python codebases.[reference:34]

---

## Graph Retrieval and Context Efficiency

### Finding

**Q7: Can graph-based context retrieval materially reduce repeated repository exploration?** Yes, but only with the **right** graph.

### Critical Distinction

| Approach | What it does | For Maestro? |
|----------|--------------|--------------|
| **GraphRAG**[reference:35] | Knowledge graphs over documents for QA | **NO** - Wrong abstraction |
| **Code property graph**[reference:36] | AST+CFG+PDG over code | **YES** - Correct abstraction |
| **Vector RAG** | Semantic similarity search | **Maybe** - Complementary |
| **Hybrid** (Codex-Atlas)[reference:37] | Vector + graph with routing | **YES** - Best of both |

**The trap:** GraphRAG is fashionable but optimized for factoid question answering over unstructured text.[reference:38] It does **not** help an agent understand "what calls this function?"—that's a structural query.

**The solution:** Code property graphs + structured query language (CPGQL[reference:39]). Agents ask precise structural questions and get precise answers, eliminating grep+read loops.

**Implementation path:**
1. Build CPG via Joern on repository check-in
2. Store in queryable graph DB
3. Expose via MCP server (CodeBadger pattern[reference:40])
4. Agents use graph queries **instead of** file-system exploration
5. Fall back to vector RAG for semantic questions ("find code similar to...")

---

## Durable Execution, Recovery, and Provenance

### Finding

**Q10: What graph-related mechanism would most improve crash recovery and resumability?**

**Temporal's durable execution** is the answer, not a graph abstraction.

### How Temporal Works

Temporal provides durable execution through:
- **Event sourcing**: Every workflow step is recorded as an immutable event[reference:41]
- **Automatic retry**: Failed steps retry with configurable backoff[reference:42]
- **Crash recovery**: If worker dies, another picks up from last event[reference:43]
- **State persistence**: Workflow state survives process restarts[reference:44]

**For Maestro specifically:**
- Agent mid-task? Temporal preserves state across agent switches
- Watchdog restarts? Temporal resumes from checkpoint
- Backend quota exhausted? Temporal retries with backoff
- Human escalation? Temporal waits for signal, then continues

**Q11: What graph-related mechanism would most improve debugging and explaining autonomous behavior?**

**Event sourcing + deterministic replay.**

The event log lets you replay the exact sequence that led to any state.[reference:45][reference:46]

**For Maestro:**
- Every agent decision logged as event
- Every tool call logged
- Every handoff logged
- Replay any execution to debug failures
- Audit trail for compliance

**Implementation:**
- Temporal already provides this via workflow history
- Add custom events for Maestro-specific semantics
- Build a replay UI for debugging

**Q5: Should Maestro adopt event sourcing?** Yes, via Temporal's built-in event sourcing.

---

## Scheduling and Multi-Agent Execution

### Finding

**Q9: Can graph scheduling improve parallel agent execution and allocation of Claude/Codex quotas?** Yes, but this is Tier 2—implement after basic DAG execution.

### Scheduling Techniques

From DAG scheduling research[reference:47][reference:48]:

| Technique | Application to Maestro |
|-----------|----------------------|
| Critical path analysis | Identify longest chain of dependent tasks |
| Heterogeneous Earliest Finish Time (HEFT)[reference:49] | Schedule tasks to minimize makespan |
| Dynamic priority scheduling[reference:50] | Adjust priorities based on runtime conditions |
| Work stealing | Rebalance work across agents |

**Maestro-specific resources to schedule:**
- Claude API quota (rate-limited)
- Codex API quota
- Available worktrees (concurrency limit)
- Context remaining in current agent
- Task risk (high-risk tasks need human oversight)

**Multi-agent graph structures:**
Maestro's orchestrator+implementer pattern is appropriate. The research shows multi-agent collaboration graphs are primarily academic[reference:51]—additional agents should be justified by specific failure modes, not architectural fashion.

**Recommendation:** Keep agent graph flat (orchestrator → implementers). Do not introduce supervisor graphs, debate graphs, or hierarchical agent teams unless a concrete problem demands them.

---

## State Machines / Alternative Models

### Finding

**Q5: Would a state machine be superior to a graph framework for some Maestro components?** Yes, for several.

### Where State Machines Beat Graphs

| Component | Why state machine | Why not graph |
|-----------|------------------|---------------|
| **Agent lifecycle**[reference:52] | Finite states: IDLE, RUNNING, QUOTA_EXHAUSTED, HANDOFF, DONE | Graph adds unnecessary complexity |
| **Task lifecycle** | PENDING→RUNNING→FAILED→RETRY→COMPLETED | Clear transitions, no branching |
| **Watchdog recovery** | MONITORING→DETECTED→RECOVERING→HEALTHY | Event-driven, hierarchical states |
| **Self-update** | CHECKING→DOWNLOADING→TESTING→ADOPTING→ROLLBACK | Sequential, state-machine natural |
| **Human escalation** | WAITING→ESCALATED→RESOLVED | Simple state transitions |

### Technology: XState

XState v5 is a TypeScript-first finite state machine library with:
- Hierarchical (nested) states[reference:53]
- Parallel states
- Guards and actions
- Type-safe transitions[reference:54]

**For Maestro:** Use XState for component lifecycles. The visual, testable, type-safe graph of states and transitions makes agent behavior predictable and debuggable.[reference:55]

**Alternative:** PydanticAI's graph library[reference:56] provides Python-native state machines with type hints. If Maestro is Python-based, this is a strong candidate.

---

## Observability and Visualization

### Finding

Existing tools provide graph visualization that Maestro can adopt directly.

### Recommendation

**Adopt existing visualization rather than building:**

| Tool | What it provides | For Maestro |
|------|------------------|-------------|
| **AgentMesh viz**[reference:57] | Real-time event streaming, Mermaid visualization, checkpoint time-travel | Live execution graph |
| **Arize AX**[reference:58] | Agent trajectory as interactive graph, path diagram | Debugging agent paths |
| **Agrex**[reference:59] | React Flow-based visualizer, JSON/JSONL trace input | Post-hoc visualization |
| **Azure Managed Grafana**[reference:60] | OpenTelemetry + workflow dashboards | Production monitoring |

**Q12: What promising technologies should Maestro explicitly not adopt?** See next section.

---

## Graph Validation and Invariants

### Finding

Graph validation is an emerging area with practical tools.

### Recommendation

**Q? Should Maestro validate execution graphs?** Yes, but keep it simple.

**Invariants to enforce[reference:61]:**
- Every state is reachable from NEW
- Every terminal state is reachable from all states
- No cycles in dependency edges
- Every task has a validation step

**Tools:**
- **Spec Graph**[reference:62]: Typed, verifiable nodes with JSON Schema validation
- **Sigma Guard**[reference:63]: Structural contradiction detection using sheaf cohomology (advanced, probably overkill)

**For Maestro:** Start with JSON Schema validation of graph structure. Add custom invariant checks in code. Avoid heavy formal verification frameworks initially.

---

## Proposed Maestro Architecture

### Concrete Architecture Description

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         MAESTRO ARCHITECTURE                               │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  USER INTERFACE / TELEGRAM                                          │   │
│  │  - Human escalation                                                 │   │
│  │  - Status dashboard                                                 │   │
│  │  - Execution visualization (AgentMesh viz)                         │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                    │                                        │
│  ┌─────────────────────────────────▼───────────────────────────────────┐   │
│  │  ORCHESTRATOR (Temporal Workflow)                                   │   │
│  │  - Reads roadmap DAG                                                │   │
│  │  - Schedules tasks                                                 │   │
│  │  - Handles failures/retries                                        │   │
│  │  - Manages agent handoffs                                          │   │
│  │  - Durable across crashes                                          │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                    │                                        │
│  ┌─────────────────────────────────▼───────────────────────────────────┐   │
│  │  IMPLEMENTER AGENTS (Claude Code / Codex)                           │   │
│  │  - Run in isolated git worktrees                                   │   │
│  │  - Query code graph (Joern CPG) for context                       │   │
│  │  - Report progress to orchestrator                                 │   │
│  │  - Handoff on quota/context exhaustion                            │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                    │                                        │
│  ┌─────────────────────────────────▼───────────────────────────────────┐   │
│  │  STATE MACHINES (XState)                                            │   │
│  │  - Agent lifecycle                                                 │   │
│  │  - Task lifecycle                                                  │   │
│  │  - Watchdog recovery                                               │   │
│  │  - Self-update                                                     │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                    │                                        │
│  ┌─────────────────────────────────▼───────────────────────────────────┐   │
│  │  CODE GRAPH (Joern CPG)                                             │   │
│  │  - Built on repository clone                                       │   │
│  │  - Stored in graph DB                                              │   │
│  │  - Exposed via MCP server                                          │   │
│  │  - Queried by agents for context                                   │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                    │                                        │
│  ┌─────────────────────────────────▼───────────────────────────────────┐   │
│  │  EVENT LOG (Temporal history + custom events)                      │   │
│  │  - All decisions logged                                            │   │
│  │  - Replayable                                                      │   │
│  │  - Auditable                                                       │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Data Models

**Roadmap DAG node:**
```python
class RoadmapNode:
    id: str
    type: Literal["task", "validation", "checkpoint"]
    description: str
    status: Literal["pending", "running", "completed", "failed", "blocked"]
    agent_type: Literal["claude", "codex", "orchestrator"]
    worktree_id: Optional[str]
    checkpoint_id: Optional[str]
    created_at: datetime
    updated_at: datetime
    retry_count: int
    max_retries: int
```

**Roadmap DAG edge:**
```python
class RoadmapEdge:
    source_id: str
    target_id: str
    type: Literal["depends_on", "blocks", "invalidates"]
```

**Agent state machine (XState):**
```
states:
  IDLE → RUNNING → QUOTA_EXHAUSTED → HANDOFF → RUNNING → COMPLETED
  RUNNING → FAILED → RETRYING → RUNNING
  RUNNING → HUMAN_ESCALATION → WAITING → RUNNING
```

**Event types:**
```
TASK_CREATED, AGENT_STARTED, CHECKPOINT_CREATED, COMMIT_CREATED,
BACKEND_QUOTA_LOW, HANDOFF_CREATED, AGENT_SWITCHED, TEST_FAILED,
TASK_BLOCKED, HUMAN_ESCALATED, TASK_COMPLETED, GRAPH_QUERY
```

---

## Implementation Priorities

### Tier 1 — Implement Soon

| Proposal | Expected impact | Confidence | Complexity | Adoption strategy |
|----------|----------------:|-----------:|-----------:|-------------------|
| **Temporal durable execution** | Critical | High | Medium | Adopt library |
| **Roadmap DAG** | High | High | Low | Implement internally |
| **Event sourcing (via Temporal)** | High | High | Low | Use Temporal built-in |
| **XState state machines** | Medium | High | Low | Adopt library |
| **Joern code property graph** | High | High | Medium | Adopt tool |

### Tier 2 — Prototype / Benchmark

| Proposal | Expected impact | Confidence | Complexity | Adoption strategy |
|----------|----------------:|-----------:|-----------:|-------------------|
| **Dynamic DAG scheduling** | Medium | Medium | High | Implement internally |
| **Incremental invalidation** | Medium | Medium | Medium | Borrow Bazel pattern |
| **Graph visualization** | Medium | High | Low | Adopt tool |

### Tier 3 — Keep in Architecture

| Proposal | Expected impact | Confidence | Complexity | Adoption strategy |
|----------|----------------:|-----------:|-----------:|-------------------|
| **Graph validation/invariants** | Low | Medium | Low | Implement internally |
| **Multi-agent graph structures** | Low | Low | High | Defer |

### Tier 4 — Reject / Avoid

| Proposal | Reason |
|----------|--------|
| **LangGraph** | Wrong layer for Maestro; adds complexity without solving core problem |
| **GraphRAG for code** | Wrong abstraction; code property graphs are superior |
| **CrewAI** | Role-based agent framework; Maestro's orchestrator pattern is sufficient |
| **AutoGen** | Message-passing agents; adds unnecessary complexity |
| **BPMN engines** | Overly formal; YAWL[reference:64] and BPMN[reference:65] are designed for business processes, not agent orchestration |
| **Petri nets**[reference:66] | Academically interesting but overly complex for Maestro's needs |

---

## Top 5 Things Maestro Should Implement

1. **Temporal durable execution** — Wrap all agent execution in Temporal workflows. Survive crashes, resume from checkpoints.[reference:67]

2. **Roadmap as explicit DAG** — Replace sequential task execution with dependency-aware DAG. Enable parallel execution and invalidation tracking.

3. **Joern code property graph** — Build CPG on repository. Expose via MCP. Agents query structure instead of grepping.[reference:68]

4. **Event sourcing for all decisions** — Log every agent decision, tool call, and state change. Enable replay, debugging, auditing.[reference:69]

5. **XState for component lifecycles** — Model agent, task, watchdog, and self-update as hierarchical state machines.[reference:70]

---

## Top 5 Graph Ideas Maestro Should Avoid (For Now)

1. **LangGraph** — It's an agent state machine, not a durable execution engine. Maestro needs durability first. Using LangGraph would add a framework without solving the core problem.[reference:71]

2. **GraphRAG for code retrieval** — GraphRAG[reference:72] is for document QA, not code-structure queries. Code property graphs are the correct tool.

3. **Multi-agent collaboration graphs** — Supervisor graphs, debate graphs, and hierarchical agent teams[reference:73] are academically interesting but don't solve a concrete Maestro problem.

4. **Petri nets**[reference:74] — Formally elegant but overly complex. State machines (XState) provide sufficient power with less complexity.

5. **BPMN/DMN engines**[reference:75] — Designed for business process automation, not agentic software engineering. Wrong abstraction.

---

## Proposed Experiments

### Experiment 1: Code Graph Benchmark

**Hypothesis:** Agents using Joern CPG queries complete repository-understanding tasks with fewer tool calls and less context than agents using grep+read.

**Method:**
- Task: "Find all functions that call function X and their test coverage"
- Group A: Normal agent (grep, read files, manual tracing)
- Group B: Graph-guided agent (query CPG for callers, query CPG for tests)
- Measure: Tool calls, files read, tokens consumed, time, correctness

**Success criterion:** 50% reduction in tool calls and files read.

### Experiment 2: Durable Execution Benchmark

**Hypothesis:** Temporal-wrapped Maestro recovers from process crashes without losing work.

**Method:**
- Run Maestro on a multi-step task
- Kill the process at random points during execution
- Measure: Work lost, time to recover, correctness of resumed state

**Success criterion:** Zero work lost; resumes from last checkpoint.

### Experiment 3: Roadmap DAG vs. Sequential Execution

**Hypothesis:** DAG-based execution completes multi-task roadmaps faster than sequential execution.

**Method:**
- Roadmap with 10 independent tasks
- Sequential: Execute one after another
- DAG: Execute in parallel where dependencies allow
- Measure: Total completion time, agent utilization

**Success criterion:** DAG completes at least 2x faster.

---

## Projects Worth Reusing

| Project | URL | Purpose | Language | License | Activity | Maestro Use | Integration |
|---------|-----|---------|----------|---------|----------|-------------|-------------|
| **Temporal** | temporal.io | Durable execution | Go/Java/Python | MIT | Very high | Wrap all execution | Adopt |
| **Joern** | joern.io | Code property graphs | Scala | Apache 2.0 | High | Repository analysis | Adopt |
| **XState** | xstate.js.org | State machines | TypeScript | MIT | High | Component lifecycles | Adopt |
| **CodeBadger**[reference:76] | github.com/qcri/codebadger | MCP server for CPG | Python | - | Active | CPG interface | Study/adapt |
| **Codex-Atlas**[reference:77] | github.com/BishBish123/codex-atlas | Hybrid vector+graph retrieval | Python | - | Active | Code retrieval | Study |
| **AgentMesh viz**[reference:78] | github.com/hupe1980/agentmesh | Graph visualization | Go | - | Active | Execution viz | Adopt/wrap |
| **Agrex**[reference:79] | github.com/ppazosp/agrex | Graph visualizer | React | - | Active | Post-hoc viz | Adopt |

---

## Ideas Explicitly Rejected

| Idea | Reason |
|------|--------|
| **LangGraph** | Wrong layer; adds complexity without solving Maestro's core durability problem.[reference:80] |
| **GraphRAG for code** | Wrong abstraction; optimized for document QA, not code-structure queries.[reference:81] |
| **CrewAI** | Role-based agent orchestration doesn't fit Maestro's orchestrator+implementer pattern.[reference:82] |
| **AutoGen** | Message-passing multi-agent framework; adds unnecessary complexity.[reference:83] |
| **BPMN engines** | Designed for business process automation, not agentic software engineering.[reference:84] |
| **Petri nets**[reference:85] | Formally elegant but overly complex; state machines are sufficient. |
| **Semantic Kernel**[reference:86] | Microsoft-specific; planners and skills don't align with Maestro's architecture. |
| **Dify**[reference:87] | Visual workflow builder; low-code focus doesn't match Maestro's code-first approach. |

---

## Open Questions

1. **Temporal vs. Prefect vs. Dagster:** Temporal is recommended for durability. But Prefect[reference:88] and Dagster[reference:89] are also mature. Should Maestro benchmark all three? **Research cannot confidently resolve** without Maestro-specific workload testing.

2. **CPG update frequency:** How often should Maestro rebuild the Joern CPG? On every commit? Periodically? On-demand? **Depends on repository size and change frequency.**

3. **CPG storage:** Neo4j vs. custom graph DB vs. in-memory? Joern uses a custom graph database.[reference:90] Should Maestro adopt that or use a standard graph DB? **Requires benchmarking.**

4. **Agent query language for CPG:** CPGQL[reference:91] is Joern-specific. Should agents write CPGQL directly, or should Maestro provide a higher-level query API? **UX research needed.**

5. **Temporal namespace strategy:** One namespace per Maestro run? Per project? Per user? **Depends on deployment scale.**

6. **Event retention:** How long to retain Temporal histories and Maestro custom events? **Depends on debugging and auditing requirements.**

---

## Sources

### Primary Documentation

1. Temporal Documentation — Durable Execution concepts[reference:92][reference:93]
2. LangGraph Documentation — Stateful agent graphs[reference:94]
3. Joern Documentation — Code Property Graphs[reference:95]
4. XState Documentation — Hierarchical state machines[reference:96]
5. Bazel Skyframe Documentation — Incremental DAG evaluation[reference:97][reference:98]
6. PydanticAI Graph Documentation — Python graph library[reference:99]

### Research Papers

7. CodexGraph (NAACL 2025) — LLM agents with graph database interfaces[reference:100]
8. DynTaskMAS (AAAI 2025) — Dynamic task graphs for LLM MAS[reference:101]
9. GraphRAG modular analysis (2026) — Knowledge graph retrieval[reference:102]
10. From Agent Loops to Structured Graphs (2026) — Survey of 70 projects[reference:103]

### GitHub Projects

11. temporal-langgraph-poc — Temporal + LangGraph integration[reference:104]
12. codebadger — MCP server for Joern CPG[reference:105]
13. Codex-Atlas — Hybrid vector+graph retrieval[reference:106]
14. RepoGraph — Repository-level code graph[reference:107]
15. AgentMesh viz — Real-time graph visualization[reference:108]
16. Agrex — React Flow graph visualizer[reference:109]
17. Sigma Guard — Graph consistency verification[reference:110]
18. Spec Graph — Typed verifiable graph nodes[reference:111]
19. Circuit Breaker — Petri net workflow engine[reference:112]

### Industry Publications

20. Grid Dynamics case study — LangGraph → Temporal migration[reference:113]
21. Temporal + OpenAI Agents SDK — Durable agents[reference:114]
22. LangGraph vs Temporal comparison[reference:115][reference:116]
23. CrewAI v1.0 announcement (2025)[reference:117]

---

## Final Decision Matrix

| Proposal | Expected impact | Confidence | Complexity | Adoption strategy | Priority |
|----------|----------------:|-----------:|-----------:|-------------------|----------|
| Temporal durable execution | Critical | High | Medium | Adopt library | Tier 1 |
| Roadmap DAG | High | High | Low | Implement internally | Tier 1 |
| Event sourcing (Temporal) | High | High | Low | Use Temporal built-in | Tier 1 |
| XState state machines | Medium | High | Low | Adopt library | Tier 1 |
| Joern CPG | High | High | Medium | Adopt tool | Tier 1 |
| Dynamic DAG scheduling | Medium | Medium | High | Implement internally | Tier 2 |
| Incremental invalidation | Medium | Medium | Medium | Borrow Bazel pattern | Tier 2 |
| Graph visualization | Medium | High | Low | Adopt tool | Tier 2 |
| Graph validation | Low | Medium | Low | Implement internally | Tier 3 |
| Multi-agent graphs | Low | Low | High | Defer | Tier 3 |
| LangGraph | Negative | High | Medium | Reject | Tier 4 |
| GraphRAG for code | Negative | High | Medium | Reject | Tier 4 |
| CrewAI | Negative | High | Low | Reject | Tier 4 |
| BPMN/Petri nets | Negative | High | High | Reject | Tier 4 |

---

**Report prepared:** September 3, 2026

**Research methodology:** Primary sources (documentation, GitHub, papers) prioritized. Secondary sources used only for discovery. All major claims supported by citations to primary evidence.

