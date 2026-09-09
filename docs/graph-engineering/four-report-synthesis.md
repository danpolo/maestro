# Four-report synthesis

Source: https://chatgpt.com/share/6a98a47c-aeb0-83eb-9905-2d1385518a9b?ogimg=plain
Extracted: 2026-09-03

Yes. I treated the four reports as independent architecture reviews, weighted their arguments rather than simply voting, and resolved the contradictions against Maestro’s actual constraints: autonomous coding agents, local/server execution, git worktrees, backend handoffs, subscription limits, and the need to avoid unnecessary infrastructure.

# Final synthesis

The biggest conclusion is that **Maestro should become graph-aware, but should not become a graph-framework application**.

All four reports substantially converge on this. The right architecture is several small specialized models: a roadmap DAG, lifecycle state machines, an append-only execution history, an incremental validation/dependency graph, and a deterministic repository graph. Trying to put execution, memory, source code, agents, roadmap and provenance into one universal graph is consistently identified as an anti-pattern. fileciteturn1file0L10-L28 fileciteturn1file1L16-L25 fileciteturn1file3L9-L12

The architecture I would actually build is:

```text
                    MACHINE-READABLE ROADMAP
                              │
                              ▼
                  Versioned Mutable Task DAG
                  dependencies / invalidation
                              │
                              ▼
                  Ready-Frontier Scheduler
             quota / worktree / conflict / priority
                              │
                              ▼
                   Task Lifecycle Controller
                       deterministic FSM
                              │
                              ▼
                Durable Execution / Reconciler
               checkpoint / lease / heartbeat
                              │
                  ┌───────────┴───────────┐
                  ▼                       ▼
             Claude Code                 Codex
                  │                       │
                  └───────────┬───────────┘
                              ▼
                         Git Worktree
                              │
                ┌─────────────┴─────────────┐
                ▼                           ▼
         Repository Graph             Validation Graph
       context / impact              tests / build / cache
                │                           │
                └─────────────┬─────────────┘
                              ▼
                     Append-only Events
                              │
                  state / provenance / UI
```

That separation is extremely close to the strongest of the four reports' recommendation: **planned graph ≠ lifecycle state ≠ actual execution history**. fileciteturn3file2L519-L576

## The decisions I would make

| Capability | Decision | Priority |
|---|---|---|
| Append-only Maestro event journal | **Build** | Tier 1 |
| Agent/task/handoff/watchdog FSMs | **Build internally** | Tier 1 |
| Mutable roadmap dependency DAG | **Build internally** | Tier 1 |
| Incremental validation/invalidation | **Build** | Tier 1 |
| Basic resource-aware scheduler | **Build simple version** | Tier 1 |
| Repository/code graph | **Tree-sitter + SQLite first** | Tier 1 |
| Graph-guided agent context | **Build + benchmark** | Tier 1 |
| Temporal | **Benchmark, don't adopt yet** | Tier 2 |
| DBOS | **Benchmark against Temporal/native** | Tier 2 |
| Stack Graphs | **Prototype where language support fits** | Tier 2 |
| Joern CPG | **On-demand specialist** | Tier 2/3 |
| LangGraph | **Study concepts only** | Tier 4 as core |
| Neo4j/Memgraph | **Don't add yet** | Tier 4 |
| GraphRAG for source code | **Reject as default** | Tier 4 |
| MCTS / Graph-of-Thought | **Reject for normal execution** | Tier 4 |
| Multi-agent debate graphs | **Reject unless benchmarks prove ROI** | Tier 4 |

The fourth report reaches almost exactly this conservative architecture, including Tree-sitter + SQLite, conservative invalidation, a simple critical-path scheduler, and a pluggable `internal | temporal | dbos` durability boundary. fileciteturn3file3L812-L855

# What should actually be implemented

1. **Start with the control plane: event journal + explicit lifecycle state machines.** Every meaningful mutation should create a typed event such as `TASK_CREATED`, `AGENT_STARTED`, `CHECKPOINT_CREATED`, `HANDOFF_CREATED`, `BACKEND_SWITCHED`, `TEST_FAILED`, `TASK_INVALIDATED`, `HUMAN_ESCALATED`, `TASK_COMPLETED`. Include `event_id`, timestamp, task/attempt IDs, correlation ID, causation ID and artifact references. Current state becomes a projection of those events rather than state scattered through orchestration loops. Then encode task, agent, backend/handoff, watchdog and self-update lifecycles as explicit legal transitions. This is one of the strongest points of agreement across the reports. fileciteturn3file4L1002-L1018

2. **Turn the roadmap into a versioned mutable DAG.** Hard prerequisites are actual dependency edges; the ready frontier comes from successful predecessors. Do not let the orchestrator arbitrarily rewrite the roadmap text. Give it explicit transactional mutations such as `ADD_TASK`, `ADD_DEPENDENCY`, `SPLIT_TASK`, `SUPERSEDE_TASK`, `INVALIDATE`, `CHOOSE_ALTERNATIVE`, and later speculative operations. Never delete old nodes—supersede them so provenance survives. Every mutation validates DAG acyclicity and increments `roadmap_version`. fileciteturn3file2L580-L623

3. **Add incremental validation, borrowing the important idea from Bazel/Skyframe/Salsa/Buck rather than adopting those products.** A successful test/build/static-analysis result should record the dependency fingerprint that made it valid. After a code change, recompute the affected reverse-dependency closure and invalidate only what may actually have changed. Be conservative at first: unnecessary re-testing wastes resources; incorrectly reusing stale validation can ship broken code. fileciteturn3file2L625-L650 This may ultimately save more actual coding capacity than fancy agent orchestration because it prevents already-proven engineering work from being repeated.

4. **Build the repository graph lightweight-first.** Start with Tree-sitter and ordinary relational/adjacency storage in SQLite. Model files, modules, symbols, functions, classes, tests, build targets and relationships such as `DEFINES`, `REFERENCES`, `IMPORTS`, `CALLS`, `INHERITS`, `TESTS`, and `DEPENDS_ON`. Do **not** start with Neo4j. The required queries are bounded graph traversals and reverse dependencies, which SQLite/in-memory structures can handle. fileciteturn3file2L652-L712

5. **Make the code graph replace exploration, not supplement it.** This requirement from our previous research remains crucial. A fresh Claude/Codex agent should receive a compact structural manifest: likely files/symbols, definitions, callers/callees, related tests and a bounded neighborhood. Only fall back to `rg`/file exploration when graph confidence is insufficient. One report proposes an Aider-like PageRank repo map plus Stack Graphs for precise cross-file resolution; another independently recommends Tree-sitter/SCIP-style structural retrieval. fileciteturn3file1L463-L481 fileciteturn2file8L954-L964

6. **Add a very simple DAG scheduler—not HEFT yet.** First compute `ready_tasks()`, then rank them using things Maestro actually knows: downstream criticality, backend availability/quota pressure, worktree availability, estimated risk/cost and likely code overlap. NetworkX-level algorithms are sufficient initially; the research explicitly shows the useful graph logic can remain tiny. fileciteturn3file3L739-L810 Once you collect real duration/quota/conflict data, you can test HEFT/OR-Tools or more sophisticated scheduling.

7. **Only after that, run the durability bake-off.** Implement a clean `DurableRuntime` interface so the current/native implementation, Temporal and DBOS can all satisfy the same Maestro contract. Inject process deaths, quota exhaustion, backend switches, long human waits and crashes after worktree mutations. Choose the substrate that provides the best reliability-to-complexity ratio. The best report explicitly recommends selecting Temporal/DBOS only if they beat the native implementation on net complexity and reliability. fileciteturn3file3L878-L889 fileciteturn3file3L938-L947

# Temporal: my resolution of the biggest disagreement

DeepSeek makes Temporal the number-one recommendation and argues all long-running execution should be wrapped in it. fileciteturn2file12L1184-L1194

The DOCX report reaches essentially the opposite conclusion: for Maestro's embedded/local nature, Temporal introduces a server/runtime dependency and deterministic replay constraints that may be disproportionate. fileciteturn3file0L27-L54

Grok puts Temporal in Tier 2 rather than Tier 1. fileciteturn3file4L1012-L1018

The fourth report gives the most defensible answer: **Temporal is probably the strongest external durability candidate, but benchmark it against DBOS and a small native implementation.** It also correctly observes that model calls, bash, git and filesystem operations should be external side-effecting activities rather than deterministically replayed control code. fileciteturn3file2L502-L515

I agree with that fourth position.

So: **do not install Temporal as part of this graph upgrade. Architect Maestro so Temporal can later be plugged in without changing Maestro's domain model.**

That distinction matters enormously.

# Joern: same story

DeepSeek recommends Joern as the main repository graph. fileciteturn2file12L1224-L1234

The other reports are more cautious, and I think correctly so. Maestro's repositories are frequently in dirty/intermediate states while agents edit them. The DOCX research argues strongly for Tree-sitter/Stack-Graph-style syntax-driven indexing because it continues functioning when the repository isn't buildable. fileciteturn3file1L281-L311 fileciteturn3file1L378-L406

The fourth report has the right compromise: **Tree-sitter graph permanently; Joern on-demand for questions requiring serious control/data-flow analysis.** fileciteturn3file2L704-L716

That is what I'd implement.

# LangGraph

This one is easier.

Do **not** migrate Maestro onto LangGraph.

There are useful ideas worth stealing: typed state, checkpoint boundaries, conditional transitions, interruptions and resumable subgraphs. But Maestro has much broader semantics—git effects, worktree ownership, roadmap dependencies, validation evidence, process execution, backend quotas and potentially days-long execution.

Grok explicitly recommends a thin graph/FSM layer rather than making the system a LangGraph app. fileciteturn3file4L1078-L1087 The fourth report similarly says LangGraph's node-reexecution/idempotency model is an important constraint around worktree/subprocess side effects. fileciteturn3file2L568-L576

**Borrow semantics, not architecture.**

# Probably the most important new capability from this research

I expected the answer to be “graph orchestration.”

I don't think it is.

The most interesting addition is the combination:

**repository graph → affected graph → validation cache → roadmap invalidation**

Example:

```text
Agent changes AuthenticationService.login()
              │
              ▼
Repository graph finds:
  callers + importers + related API + tests
              │
              ▼
Validation graph determines:
  auth_unit_test = stale
  login_integration_test = stale
  unrelated_payment_tests = still valid
              │
              ▼
Roadmap graph determines:
  task B depends on old Auth API → STALE
  task C uses unchanged public interface → still READY
              │
              ▼
Only necessary tests and replanning occur.
```

That is far more powerful than simply drawing the current orchestration loop as nodes and edges. It allows **previous engineering reasoning and verification to survive safely across changes**, which directly increases how much useful engineering Maestro gets out of each Claude/Codex quota.

The fourth report ranks incremental validation among the highest-impact capabilities, immediately alongside the roadmap DAG and durability. fileciteturn2file2L295-L323

# What I would explicitly tell the Maestro agent NOT to do

No universal graph database. No Neo4j just because the word “graph” appears everywhere. No GraphRAG over source code. No LangGraph rewrite. No MCTS planning. No automatic debate agents. No BPMN/Petri-net control plane. No full Joern index on every edit. No sophisticated HEFT scheduler before there are actual scheduling metrics.

And crucially: **do not build a “generic graph engine.”**

Maestro needs a handful of explicit domain operations and a handful of bounded traversals. One report phrases this particularly well: Maestro needs six or seven transactional graph mutations, not a general graph-rewriting language. fileciteturn2file1L202-L208

## The implementation target

After this work, Maestro should conceptually own:

```text
maestro/
  control/
    events
    projections
    lifecycle
    reconciler

  roadmap/
    graph
    mutations
    invalidation
    readiness

  repository/
    parser
    graph
    queries
    context_manifest

  validation/
    dependencies
    fingerprints
    affected
    cache

  scheduling/
    ready_frontier
    priority
    resources

  durability/
    interface
    native
    temporal?   # later
    dbos?       # later
```

That is the practical architecture I would now move forward with.

The most important benchmark remains the same philosophy as our previous tooling research: **graph capabilities only stay if they reduce existing expensive work without reducing task success**. The strongest report proposes measuring verified task success, repository tool calls, files/lines read, input/output tokens, peak context, elapsed time and fallback-search use; that is exactly the evaluation I would use. fileciteturn3file3L934-L980

If you want, the next step should be to turn **this synthesis into one implementation brief `.md` file specifically written for the Maestro coding agent**, with architecture decisions, phases, acceptance criteria, and what it must *not* implement.
