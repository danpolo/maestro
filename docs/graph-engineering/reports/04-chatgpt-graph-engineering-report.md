# Report 4: ChatGPT graph-engineering report

Source: https://chatgpt.com/s/t_6a98aadf73648191aa14be3c761c78d1
Extracted: 2026-09-03

Yes. Below is the full text of the 4th report as extracted from the attached file. I’ve only removed the internal line-number markers added by the file reader; the substance is unchanged. State-of-the-Art Graph Engineer…

State-of-the-Art Graph Engineering for Maestro

Research date: September 3, 2026. Scope and deliverables follow the attached Maestro research brief.

Strategic conclusion

Executive Summary

The central conclusion is that Maestro should become graph-aware without becoming a “graph framework application.” The highest-value architecture is a deliberately layered combination:

Own a small, typed, persistent roadmap/task DAG. It should express real dependencies, permit transactional graph mutation, and drive readiness, invalidation, and scheduling. Build-system practice strongly supports this pattern: Bazel/Skyframe records fine-grained dependencies for incremental recomputation; Buck2’s DICE invalidates recorded computations when inputs change; Nx combines a project graph, task graph, affected-set computation, and caching; Pants can include direct or transitive dependents of changed targets.

Event-source Maestro’s orchestration control plane, not everything Maestro knows. Every state-changing operation—task creation, agent start, checkpoint, commit, handoff, quota transition, test result, escalation, completion—should produce an append-only domain event. Current state, the live execution graph, provenance views, and operator timelines should be projections of that history. Durable workflow systems demonstrate why recorded execution history is so powerful for recovery; Temporal reconstructs workflow state by replaying durable history and requires nondeterministic I/O to live in Activities.

Use state machines for lifecycle semantics instead of drawing retry loops as graph edges. Task lifecycle, agent lifecycle, backend handoff, watchdog recovery, quota states, and self-update are finite, bounded control problems. XState demonstrates mature statechart/actor semantics and persistence, while BPMN engines demonstrate compensation/retry semantics; neither implies Maestro needs those particular products.

Build an incremental validation/dependency graph before building a grand knowledge graph. This is likely one of the cheapest ways to reduce expensive model work: map changed files/symbols to affected build targets, tests, validation artifacts, and roadmap assumptions; then reuse previous results whenever their dependency fingerprints remain valid. Skyframe, Salsa, Buck2/DICE, Nx, and Pants all provide mature precedents for dependency-driven reuse and invalidation.

Construct a deterministic repository/code graph, but start much smaller than Joern or GraphRAG. Tree-sitter can incrementally parse source, and tree-sitter-graph can derive arbitrary graph structures from parsed trees. Aider’s RepoMap already demonstrates a particularly relevant pattern: extract definitions/references and use graph ranking to fit structurally important repository context into a token budget. LocAgent and RepoGraph provide research evidence that structural repository graphs can improve localization and downstream software-engineering performance.

Graph retrieval is promising only if it replaces exploration. The successful target is not “graph query, then grep, then vector search, then read everything.” The target is: task query → seed symbols/files → structural traversal → ranked minimal neighborhood → targeted reads. LocAgent reports up to 92.7% file localization and large cost reductions in its evaluated setting; RIG reported a 12.2% mean accuracy increase together with 53.9% lower completion time and 57.8% lower seconds-per-correct-answer across its eight-repository experiment. Those numbers are encouraging but not sufficiently general to skip a Maestro-specific benchmark.

Temporal is the strongest existing project if Maestro delegates durable execution, but adoption should follow a controlled bake-off rather than architectural surrender. Temporal’s deterministic Workflow/nondeterministic Activity split, recorded replay, heartbeats, retries, long-running workflows, and Worker Versioning map unusually well to Maestro’s watchdog, backend switching, interruption, self-update, and recovery requirements. Its server was actively maintained in 2026, with the official repository showing v1.31.2 on July 8, 2026. DBOS is the most interesting lighter-weight challenger, especially for a Python/Postgres-centric Maestro.

Do not adopt LangGraph as Maestro’s architecture. LangGraph has useful semantics—persistent checkpoints, interrupts, subgraphs, pending writes, and durable execution—but its abstraction is optimized for agent-state computation. Its documentation also makes clear that resumed nodes can re-execute and that side effects therefore need idempotency. That is useful technology, but Maestro’s hardest requirements are durable external-process orchestration, git/worktree effects, dependency invalidation, provenance, scheduling, and long-lived roadmap evolution. Maestro should borrow LangGraph ideas where useful rather than make LangGraph the system of record.

Avoid one giant graph. The roadmap DAG, execution history, repository graph, validation dependency graph, and possibly later a knowledge/evidence graph have different consistency, mutation, retention, and query requirements. Conflating them would create schema coupling and operational complexity with little benefit.

Explicit assumptions caused by unspecified items
Unspecified in the brief	Assumption used for this report
Maestro implementation language	Unspecified. Architecture is language-neutral; Python examples are illustrative because several candidate systems have strong Python SDKs. If Maestro is TypeScript, corresponding TypeScript choices are noted.
Existing database	Unspecified. Assume SQLite is acceptable for local development and PostgreSQL is acceptable for durable production control-plane state.
Deployment topology	Unspecified. Assume initially one controller host with multiple local/remote workers, with an eventual need to survive controller-process restarts.
Expected concurrency	Unspecified. Assume roughly 1–10 simultaneous implementer attempts initially, rather than hundreds.
Repository size/language mix	Unspecified. Assume polyglot repositories ranging from small projects to large monorepos.
Existing roadmap schema	Unspecified. Assume it can be versioned and extended with IDs, dependency edges, state, evidence, and estimates.
Claude Code/Codex invocation API	Unspecified. Assume each can be wrapped as an external process or API-backed activity and can operate in an assigned worktree.
Quota API granularity	Unspecified. Assume Maestro can at minimum observe coarse backend availability/quota pressure even if exact remaining tokens are unavailable.
Risk policy for human approval	Unspecified. Assume Maestro will define an explicit policy table rather than let agents decide autonomously which destructive actions require approval.
Team size/timeline	Unspecified. Effort estimates below are engineering estimates assuming two experienced engineers; they are not empirical source claims.
Maestro Graph Architecture Recommendation

The recommended principle is:

Own domain graphs; rent durability; borrow graph algorithms; use state machines for bounded control.

Mermaid
flowchart TB
    R[Machine-readable roadmap DAG]
    S[Dependency-aware scheduler]
    T[Task lifecycle state machine]
    D[Durable workflow adapter]
    A[Claude Code / Codex activity]
    W[Isolated git worktree]
    E[(Append-only Maestro event journal)]
    P[(Materialized state & provenance)]
    C[(Repository code graph)]
    V[(Validation / affected graph)]
    O[Operator UI + watchdog reconciler]
    H[Telegram escalation]

    R --> S
    S --> T
    T --> D
    D --> A
    A --> W

    C --> S
    C --> A
    W --> V
    V --> T

    T --> E
    D --> E
    A --> E
    W --> E
    V --> E
    E --> P

    P --> O
    O --> H
    H --> T

    S -. resource pressure .-> D
    D -. optional substrate .-> TEMP[Temporal / DBOS benchmark winner]

The important architectural separation is that Temporal, DBOS, Restate, LangGraph, or any future runtime must not own Maestro’s roadmap schema or repository intelligence. A durable executor may own execution mechanics; Maestro should own domain meaning.

The required architecture questions resolve as follows:

Question	Decision
Q1 — explicit execution graph?	Yes, minimally. Represent planned/actionable execution as typed actions and dependencies, but represent retries/lifecycle as state transitions and preserve actual execution as events.
Q2 — static/dynamic/persistent/event-sourced/replayable?	Roadmap: dynamic + persistent DAG. Control plane: event-sourced. Execution state: persistent. Replay: deterministic control decisions and previously recorded activity outputs are replayable; model calls and external effects are not blindly reissued. Temporal formalizes exactly this deterministic/nondeterministic separation.
Q3 — build or existing engine?	Build Maestro graph semantics; benchmark an existing durable engine for execution. Do not build a distributed workflow engine unless Temporal/DBOS integration proves worse.
Q4 — strongest adoption candidate?	Temporal, with DBOS as the strongest lightweight alternative. Temporal’s replay model, worker versioning, heartbeats, retries, and long-running execution are especially aligned with Maestro.
Q5 — where are state machines superior?	Task lifecycle, agent lifecycle, quota/backend state, watchdog recovery, human approval, and self-update. Statecharts are explicitly designed to give finite lifecycle semantics and hierarchical/actor behavior.
Q6 — persistent code graph?	Yes, after an MVP benchmark. Start with Tree-sitter-derived files/symbols/imports/references/calls plus test/build edges in SQLite or relational tables. Use Joern on demand for deep CPG/data-flow questions.
Q7 — reduce repository exploration?	Probably materially, but benchmark it. Aider, RepoGraph, LocAgent, and RIG all provide positive structural-retrieval evidence.
Q8 — roadmap DAG?	Yes. Hard dependencies remain acyclic; discoveries mutate it transactionally through split/add/supersede/invalidate operations rather than wholesale replanning.
Q9 — scheduling?	Yes when concurrency begins. Readiness comes from DAG dependencies; ranking incorporates critical path, backend quota, worktrees, conflict risk, CPU/RAM, and human attention. NetworkX supplies basic DAG algorithms; OR-Tools is a later option for resource-constrained scheduling.
Q10 — biggest recovery improvement?	Append-only event history + idempotent durable activities + leases/checkpoints. If adopted, Temporal provides the strongest ready-made replay mechanism.
Q11 — biggest debugging improvement?	Causally linked provenance events projected into task/attempt/agent/commit/test timelines, correlated with OpenTelemetry traces/logs.
Q12 — what not to adopt?	A universal graph DB/ontology, LangGraph as the whole control plane, LLM-generated GraphRAG for deterministic code structure, default multi-agent debate, general graph-rewriting DSLs, and BPMN/Petri-net infrastructure for ordinary Maestro lifecycles.
Highest-Value Capabilities
Rank	Capability	Capability gained	Current Maestro problem	Expected practical impact	Cost	Confidence
1	Durable event journal + lifecycle state machines	Crash reconstruction, explicit transitions, auditable handoffs	Loops contain implicit state and recovery logic	Very high reliability/recovery/debuggability	Medium	High
2	Dynamic roadmap dependency DAG	True readiness, blocking, local replanning, parallelism	Roadmap is mainly sequential/delegated	Very high autonomy/concurrency	Medium	High
3	Incremental validation graph	Re-test only what may be affected; reuse valid evidence	Reverification can repeat expensive work	High task throughput and compute/model efficiency	Medium	High
4	Repository structural graph + targeted retrieval	Persistent architectural orientation	Fresh agents repeatedly rediscover repository structure	High context efficiency if benchmark validates	Medium	Medium-high
5	Dependency/resource-aware scheduler	Allocate independent work to Claude/Codex intelligently	Parallel work risks quota/resource/conflict waste	High once concurrency rises	Medium	High
6	Causal provenance projections	Explain “why this happened”	Logs do not naturally reconstruct causal execution	High operational leverage	Low-medium	High
7	Durable workflow substrate	Automatic replay, timers, cancellation, worker recovery	Watchdog must implement failure semantics manually	Potentially very high	Medium-high ops/integration cost	Medium-high
8	Temporal decision/knowledge graph	Historical decision retrieval	Re-reading decisions/requirements may become costly	Medium, later	High	Low-medium
Runtime and planning
Technology / Project Evaluation

Methodology and literature survey. The research prioritized official documentation, creator-maintained repositories, and original papers, with emphasis on technologies active through 2026. The strongest transferable ideas came not from agent frameworks but from durable workflow engines, build systems, incremental-computation frameworks, source-code analysis, constraint scheduling, and observability. Current agent frameworks were evaluated primarily for semantics Maestro could reuse, not as replacement products.

A notable 2026 architectural signal is Pydantic AI’s separation of agent abstractions from durable execution: its official documentation supports Temporal, DBOS, Prefect, and Restate as independent durability backends. That validates the architectural idea that agent execution semantics and durable orchestration need not be the same layer.

Candidate	What it is useful for	Durability/recovery	Dynamic control	Maestro fit	Key concern	Verdict
Temporal	Durable application workflows	Excellent replay/history model; Activities isolate nondeterministic I/O; worker versioning available.	Strong: normal code, child workflows, signals/timers	Excellent durability substrate	Operational service + deterministic workflow constraints	Tier 2 bake-off; likely adoption if benchmark wins
DBOS	Database-backed durable workflows	Postgres-backed durable execution; active Python implementation and releases through July/August 2026.	Strong code-first model	Very strong if Maestro is Python/Postgres-first	Younger ecosystem than Temporal	Tier 2 head-to-head with Temporal
Restate	Durable event-driven/stateful execution	Designed around durable execution; official project remained active in 2026.	Strong	Potentially strong	Smaller ecosystem; retrieved material did not establish a simple license conclusion, so legal review is needed	Tier 2/3
LangGraph	Stateful LLM/agent graphs	Checkpoints, threads, pending writes, interrupts and resumability.	Excellent agent-level branching/cycles/subgraphs	Good for local agent workflows	Too close to agent-state abstraction for Maestro’s whole control plane; side effects must be made replay/idempotency-safe	Study / selectively wrap; do not make core
Pydantic Graph	Typed Python execution graphs	Persistence/resumability plus integrations with external durable systems.	Decisions, parallel spread/broadcast/join	Good if Maestro is Python	Runtime and graph abstractions remain a moving part of rapidly evolving Pydantic AI ecosystem	Study / possible internal-workflow helper
Prefect	Python workflow/task orchestration	Persistent flow/task state and events	Flexible dynamic Python workflows	Adequate	Less compelling than Temporal/DBOS for Maestro’s exact durable-process problem	Not shortlisted as core; official repository remained actively releasing 3.8.x.
Dagster	Data-asset orchestration	Mature run orchestration	Strong for asset/data dependencies	Weak domain fit	Maestro is not primarily an asset/materialization system	Reject as core; Dagster was actively shipping 1.13.x in July 2026 and remains Apache-2.0.
AutoGen GraphFlow	Explicit directed multi-agent interaction	Not the main differentiator	Sequential, parallel, conditional, looping agent graphs.	Useful design reference	Agent interaction abstraction, not crash-resilient control-plane substrate	Study
Semantic Kernel orchestration	Standard multi-agent patterns	Not the main differentiator	Concurrent/sequential/handoff/group/Magentic	Low near-term	Microsoft documentation still describes agent orchestration as experimental.	Avoid core dependency now
OpenAI Agents SDK	Agent runs, handoffs, sessions, traces	Sessions can resume interrupted approval flows, but it is not a general durable workflow engine.	Manager/handoff patterns	Useful backend integration ideas	Handoffs remain inside an agent run and are not Maestro’s durable task graph.	Borrow typed handoff/tracing patterns

Temporal's specific fit deserves emphasis. Temporal Workflow code is replayed and therefore constrained to deterministic behavior, while Activities may perform arbitrary network, filesystem, database, model, or subprocess I/O. Pydantic AI’s Temporal integration goes so far as to route model requests, tools, and MCP communication through Activities while keeping coordination logic inside the Workflow. This is almost exactly the separation Maestro needs between control decisions and Claude Code/Codex/worktree effects.

The main warning is equally important: a Temporal Activity can be retried from its beginning. Therefore an activity that launches an implementer into a worktree must be designed around checkpoints, idempotency keys, attempt IDs, and inspection of existing git state rather than assuming exactly-once process execution.

DBOS deserves a serious comparison instead of being dismissed as “new.” Its Python repository describes database-backed durable workflows, uses an MIT license, and continued releasing rapidly in 2026; recent releases include workflow locking, recovery, cancellation, queues, and transient-database-failure handling. If Maestro already depends on PostgreSQL and is mostly one language, its smaller operational footprint could outweigh Temporal’s larger ecosystem.

Execution and Workflow Graphs

The recommended Maestro execution abstraction is much smaller than a general agent graph.

A durable action node needs:

ActionNode
  id
  task_id
  action_type
  input_artifact_refs[]
  expected_output_types[]
  precondition_ids[]
  handler
  resource_request
  idempotency_key
  retry_policy
  timeout_policy
  risk_class
  current_attempt_id

The minimal edge vocabulary should remain deliberately tiny:

REQUIRES       # hard prerequisite
PRODUCES       # action -> artifact
VERIFIES       # verification -> artifact/task
COMPENSATES    # recovery action -> prior action
DERIVED_FROM   # replanned/split execution relationship

Do not encode retry, quota_low, backend_changed, or waiting_for_human as arbitrary graph edges. Those are lifecycle transitions.

Suggested action states:

PENDING
READY
LEASED
RUNNING
WAITING_EXTERNAL
VERIFYING
SUCCEEDED
FAILED_RETRYABLE
FAILED_TERMINAL
CANCELED
STALE

A useful distinction is:

planned execution graph: what Maestro expects to do;

state machine: what state each unit currently occupies;

event/provenance graph: what actually happened.

This eliminates one of the common weaknesses of agent graph implementations: the diagram does not need to serve simultaneously as scheduler, event log, lifecycle state machine, memory store, and audit representation.

LangGraph remains worth studying because its checkpoint model stores state after graph supersteps, its thread mechanism supports resumption, and successful parallel writes can survive sibling failures. However, its interrupt documentation explicitly warns that a resumed node executes again from the beginning, so side effects preceding an interrupt need to be idempotent. That is acceptable for an LLM application graph; it is a significant architectural constraint around git/worktree/subprocess effects.

Dynamic Planning and Task Graphs

Maestro should formalize its roadmap as a versioned, mutable dependency DAG.

The DAG is not merely a visualization. It should determine:

ready(task) =
    every HARD predecessor is SUCCEEDED
    AND no blocking assumption is invalid
    AND required resources are currently satisfiable
    AND policy permits execution

Build-system experience strongly supports maintaining a distinct dependency graph and deriving only affected work from changes. Nx, for example, maintains a project graph and separate task graph; its affected calculation uses changed files plus dependency relationships to select the minimum relevant project subset. Pants similarly exposes direct or transitive dependents of changed targets. The key Maestro innovation should be bounded graph repair rather than roadmap regeneration.

Allowed graph mutations should be explicit operations:

Mutation	Semantics
ADD_TASK	Add newly discovered required work.
ADD_DEPENDENCY	Add hard/soft dependency if graph invariants remain valid.
SPLIT_TASK	Convert one coarse task into a sub-DAG; original becomes EXPANDED, never silently disappears.
SUPERSEDE_TASK	Preserve old node for provenance while replacing its execution relevance.
INVALIDATE	Mark completed/ready downstream nodes stale when a dependency assumption or artifact changes.
CHOOSE_ALTERNATIVE	Select one branch of an explicit OR/alternative group.
CANCEL_SPECULATION	Cancel a speculative branch after another branch becomes preferred.
MERGE_DISCOVERY	Convert newly discovered facts into changed prerequisites or new nodes.

Graph mutation should be optimistic and transactional:

BEGIN
  lock roadmap_version
  require expected_version == current_version
  apply proposed mutations
  validate node schemas
  validate edge types
  validate hard-dependency acyclicity
  validate policy invariants
  append ROADMAP_GRAPH_MUTATED event
  increment roadmap_version
COMMIT

Do not physically delete historical work after replanning. A task that became unnecessary should be SUPERSEDED or CANCELED with a reason and causation event. Otherwise debugging a week-long autonomous run becomes unnecessarily difficult.

Incremental-computation lessons. Skyframe treats computations as keys/values and records dependencies dynamically; accurate dependency recording permits correct incremental reuse. Salsa likewise memoizes queries and uses dependency validation to avoid recomputing unchanged results. Buck2’s DICE invalidates computations based on recorded inputs. For Maestro, the corresponding rule should be:

A previous engineering result is reusable exactly when Maestro can establish that every declared input and dependency relevant to that result is unchanged.

That gives concrete machinery for:

test-result reuse;

build-result reuse;

static-analysis reuse;

architecture-summary reuse;

repository-context cache validity;

task invalidation;

requirement/evidence invalidation.

The danger is hidden dependencies. A cache is only correct if dependencies are complete; Bazel’s Skyframe design explicitly makes dependency declaration/recording central to incrementality. Consequently, Maestro should begin conservatively: false-positive invalidation wastes compute, while false-negative invalidation can cause incorrect code to be accepted.

Planning research supports hierarchical decomposition as a pattern, but the strongest recent LLM evidence is still domain-specific. ReAcTree, for example, dynamically constructs a hierarchical agent tree and reported gains on embodied-agent benchmarks, not autonomous software engineering. Maestro should borrow hierarchical decomposition and local repair, not adopt a tree-search research stack based on that evidence.

Useful planning ideas are therefore:

HTN-like decomposition: roadmap task → implementation/test/review subtasks.

Partial-order planning: encode only real ordering constraints; maximize independent readiness.

AND/OR semantics: AND for all-required subtasks; OR for alternative strategy/backend branches.

Plan repair: replace the smallest invalid subgraph.

A*/best-first: potentially useful later for choosing among existing alternatives when reliable cost estimates exist.

MCTS / Graph-of-Thought: not justified for normal software execution because each expansion can imply expensive additional model inference.

Repository intelligence
Repository / Code Graphs

A persistent repository graph is one of the most promising Maestro-specific opportunities because fresh-agent handoffs deliberately discard polluted conversational context. Without another persistent structural representation, every fresh agent may have to rediscover imports, symbols, callers, tests, module boundaries, and architectural relationships.

The first version should be deterministic and local-first.

Recommended node types:

Repository
Package / Module
File
Symbol
Function
Method
Class / Interface
Test
BuildTarget
APIEndpoint
ConfigurationUnit

Recommended edge types:

CONTAINS
IMPORTS
DEFINES
REFERENCES
CALLS
INHERITS
IMPLEMENTS
TESTS
BUILDS
DEPENDS_ON
GENERATES
CONFIGURES

Later provenance edges can connect code to Maestro’s control plane:

Task --MODIFIED--> File
Attempt --TOUCHED--> Symbol
Commit --CHANGED--> File
Verification --VERIFIED--> BuildTarget
Requirement --IMPLEMENTED_BY--> Symbol
Failure --OBSERVED_IN--> Test

Tree-sitter is a particularly strong primitive because it is an incremental parsing system designed to update syntax trees efficiently after edits and is widely usable across languages. tree-sitter-graph adds a DSL specifically for constructing graphs from Tree-sitter parses.

Aider offers the most directly relevant mature design pattern. Its RepoMap extracts repository definitions/references, ranks structural importance using a graph algorithm, and constructs a concise map within a token budget. An active Aider issue in 2026 concerning PageRank edge direction is also a useful reminder: generic graph ranking does not automatically understand the semantics of “caller,” “callee,” “dependency,” and “affected by.” Maestro should use typed/directed traversal, not one universal PageRank score.

The research literature is directionally supportive:

RepoGraph reported improvements when repository-level structural graphs were plugged into several software-engineering methods.

LocAgent models files, classes, functions and relations such as import, invocation, and inheritance; it reported up to 92.7% file localization and substantial cost reduction on its evaluated benchmarks.

SWE-Explore found that exploration quality correlates with downstream repair and that line-level coverage/ranking remain important even when file-level localization is good.

RIG reported improvements in answer accuracy and large completion-time reductions when exposing a deterministic build/test architecture graph to commercial coding agents, including Claude Code and Codex, although its experiment covered only eight repositories.

Those papers are strong enough to justify a benchmark, but not strong enough to justify a heavyweight graph platform before testing.

Recommended storage: SQLite initially, optionally PostgreSQL for shared indexing. Do not add Neo4j/Memgraph solely because the data is graph-shaped. The first required queries are bounded traversals, reverse dependencies, neighborhood expansion, and ranking, all of which can be represented with ordinary adjacency tables and evaluated in memory or with recursive SQL.

Joern should be an on-demand specialist, not the default index. Joern’s Code Property Graph combines syntax, control-flow, and data-flow relationships and supports multiple languages; its current repository is Apache-2.0 and remained very actively released in 2026. It is valuable where Maestro needs vulnerability analysis, detailed call/data flow, or language-specific deep analysis. It would be unnecessarily heavy as the first mechanism for answering “what imports this file?” or “which tests are likely affected?”

Two very recent code-graph projects are particularly worth benchmarking rather than blindly adopting:

RepoSkein uses Tree-sitter to construct a deterministic local code graph and exposes it to agents through MCP, with graph data stored locally in git-friendly form.

CodeWiki uses Tree-sitter plus SQLite and exposes calls, impact, and context queries; its repository reports its own benchmark of substantially fewer agent tool calls/tokens. Because those figures are project-authored and the project dates from 2026, they should be treated as a lead for reproduction, not independent evidence.

The persistent code graph should answer exact queries such as:

Query	Why Maestro needs it
definitions("AuthenticationService")	Jump directly to implementation entry points.
callers(symbol, depth=2)	Find downstream behavioral impact.
callees(symbol, depth=2)	Collect dependencies required to understand implementation.
importers(file)	Identify reverse module impact.
tests_for(changed_symbols)	Select targeted validation.
build_targets_for(changed_files)	Select required builds.
context(task_text, token_budget=N)	Build graph-guided agent context.
blast_radius(commit)	Identify potentially invalidated tasks/tests/components.
architectural_path(A, B)	Explain how subsystems connect.
changed_since(commit)	Incrementally refresh only affected graph nodes.

An important distinction: git remains the truth for code. The graph is a reproducible index keyed to a commit/tree state. If graph state and repository state disagree, rebuild the affected index.

Graph Retrieval and Context Efficiency

The recommended context pipeline is hybrid, but graph traversal must perform real pruning:

Mermaid
flowchart LR
    Q[Task / failure / requirement]
    L[Lexical/BM25 symbol seeds]
    V[Optional semantic seeds]
    G[Typed code-graph traversal]
    I[Impact + test/build expansion]
    R[Structural ranking]
    B[Token-budget selector]
    C[Minimal context bundle]
    A[Claude Code / Codex]

    Q --> L
    Q --> V
    L --> G
    V --> G
    G --> I
    I --> R
    R --> B
    B --> C
    C --> A

    A -. low retrieval confidence only .-> F[Targeted fallback search]
    F --> G

Microsoft GraphRAG is useful evidence for combining semantic entry points with graph-neighborhood expansion and budgeted context. Its local-search architecture starts from entities related to the query and expands through connected graph information, while its global search uses community summaries for broader corpus questions. But GraphRAG itself is not the recommended code index, because source structure can usually be extracted deterministically rather than inferred by an LLM.

A reasonable retrieval score is:

score(entity) =
    lexical_relevance
  + semantic_relevance
  + edge_type_weight
  + proximity_to_seed
  + reverse_dependency_importance
  + recent_change_bonus
  + task_history_relevance
  - context_cost

This is preferable to blindly applying PageRank. Personalized PageRank may still be one useful component, especially for identifying structurally central nodes relative to task seeds, but the edge types matter: a call from A→B does not mean the same thing for impact analysis as an import dependency or inheritance edge.

The agent should receive a context manifest:

JSON
{
  "repo_revision": "git-sha",
  "task_id": "TASK-417",
  "retrieval_confidence": 0.87,
  "entrypoints": [
    "src/auth/service.py:AuthenticationService",
    "src/api/login.py:login"
  ],
  "required_files": [
    "src/auth/service.py",
    "src/api/login.py"
  ],
  "supporting_symbols": [
    "TokenStore",
    "validate_session"
  ],
  "likely_tests": [
    "tests/auth/test_login.py"
  ],
  "graph_reasoning": [
    "login CALLS AuthenticationService.authenticate",
    "test_login TESTS login",
    "AuthenticationService IMPORTS TokenStore"
  ]
}

Agents remain free to request additional context, but Maestro should instrument whether they actually needed it. A graph feature that returns ten files only for Claude Code to subsequently read 150 files has failed its purpose.

The central evaluation should therefore be engineering work per exploration cost, not token savings in isolation:

context_efficiency =
    correctly_completed_engineering_tasks
    / total_repository_exploration_cost

Exploration cost should count agent tool calls, files/lines read, input context, wall-clock time, and fallback searches.

Graph memory. Maestro should not introduce an LLM-generated persistent knowledge graph for source code in Tier 1. Graphiti is genuinely interesting for temporal/episodic knowledge: its current repository remained active through July 2026 and is Apache-2.0, and its authors have published benchmark results for temporal agent memory. But decisions, requirements, previous attempts, and evidence should first be stored as typed events/artifacts. Add a temporal knowledge-graph layer only after demonstrating recurring queries that are difficult or expensive to answer from those structures.

Durability, scheduling, and control models
Durable Execution, Recovery, and Provenance

The watchdog should evolve from “keep the loop alive” into a durable reconciler.

Every running external operation receives a lease:

AttemptLease
  attempt_id
  task_id
  worker_id
  backend
  worktree_id
  acquired_at
  heartbeat_at
  expires_at
  checkpoint_ref
  idempotency_key

If heartbeats stop, the reconciler does not simply launch another agent. It determines:

What was the last durable event?

Does the worktree contain unrecorded changes?

Was a checkpoint commit created?

Did the external action produce an artifact whose completion event was lost?

Is the previous worker definitely dead?

Is the operation safe to retry?

Should Maestro resume the same backend, switch backend, or escalate?

That dramatically reduces duplicate work and ambiguous recovery.

The event record should be stable and engine-independent:

JSON
{
  "event_id": "01J...",
  "sequence": 1842,
  "timestamp": "2026-09-03T08:14:31Z",
  "event_type": "HANDOFF_CREATED",
  "aggregate_type": "task",
  "aggregate_id": "TASK-417",
  "run_id": "RUN-91",
  "attempt_id": "ATT-3",
  "correlation_id": "TASK-417",
  "causation_id": "EVT-1841",
  "schema_version": 1,
  "payload": {
    "from_backend": "claude-code",
    "to_backend": "codex",
    "worktree": "...",
    "head_commit": "...",
    "reason": "quota_pressure",
    "brief_artifact_id": "ART-992"
  }
}

Minimum event families should include:

ROADMAP_GRAPH_MUTATED
TASK_CREATED
TASK_READY
TASK_LEASED
TASK_STARTED
AGENT_STARTED
CHECKPOINT_CREATED
COMMIT_CREATED
BACKEND_QUOTA_LOW
HANDOFF_REQUESTED
HANDOFF_CREATED
AGENT_SWITCHED
VERIFICATION_STARTED
TEST_FAILED
TEST_PASSED
TASK_BLOCKED
TASK_INVALIDATED
HUMAN_ESCALATED
HUMAN_RESPONSE_RECEIVED
TASK_COMPLETED
ATTEMPT_LOST
ATTEMPT_RECOVERED
SELF_UPDATE_STAGED
SELF_UPDATE_VALIDATED
SELF_UPDATE_ADOPTED
SELF_UPDATE_ROLLED_BACK

Event sourcing scope should be narrow: orchestration decisions, lifecycle changes, graph mutations, artifact references, and provenance. Do not event-source every source-code edit line or every telemetry datum.

Temporal is the best-known implementation substrate for the hardest part. Its event history supports recovery by replaying workflow code while replaying recorded activity results instead of blindly repeating every external operation. Temporal Worker Versioning also supports controlled deployment of workflow code and rollback, which is relevant to a self-updating Maestro even though it does not replace Maestro’s own last-known-good update protocol.

LangGraph’s semantics provide a useful contrasting lesson. It also provides persistence/checkpoints and fault tolerance, but its interrupt mechanism resumes by executing the node again from its beginning. This reinforces the general rule that durability never eliminates the need to reason about side-effect idempotency.

For git worktrees, idempotency should be achieved with structure rather than hope:

(activity, task_id, attempt_id, checkpoint_sha) -> idempotency key

Before rerunning an activity, Maestro should inspect the worktree and event/artifact records:

if completion artifact already recorded:
    return recorded result

if checkpoint exists and worktree is consistent:
    resume from checkpoint

if partial uncheckpointed changes exist:
    snapshot or classify them before retrying

if effects cannot be reconciled:
    mark attempt UNCERTAIN and escalate/review

A model call itself should not be presumed deterministic. Maestro should persist important request/result artifacts when replay correctness depends on them and replay the stored result when appropriate.

Provenance graph

The most useful provenance query is not “show all events.” It is:

Requirement
  -> RoadmapTask
  -> Attempt
  -> Agent/backend
  -> Handoff
  -> Commit
  -> ChangedSymbols
  -> Verification
  -> TestResult
  -> CompletionDecision

This immediately answers:

Why was this task considered complete?

Which agent generated this commit?

Why did Maestro switch from Claude Code to Codex?

Which failed test triggered replanning?

Which checkpoint survived a crash?

Which evidence justified skipping a test?

Which roadmap mutation made a task obsolete?

OpenLineage demonstrates a standardized event-oriented lineage model built around jobs, runs, datasets, and run events. Its data-centric schema is not a direct fit, but its separation of lineage events from visualization is worth copying conceptually.

Scheduling and Multi-Agent Execution

A roadmap DAG immediately creates a ready frontier. NetworkX already provides topological ordering, topological generations, descendants/ancestors, transitive reduction, and longest-path functions that are sufficient for Maestro’s initial scheduler. The first scheduler should be greedy and explainable, not a research optimizer.

Example task score:

priority =
    2.0 * critical_path_pressure
  + 1.5 * downstream_blocked_count
  + 1.2 * age
  + 1.0 * expected_uncertainty_reduction
  + 0.8 * backend_affinity
  - 1.5 * predicted_conflict_risk
  - 1.2 * quota_pressure
  - 1.0 * expected_context_cost
  - 0.8 * resource_cost

Each task requests a resource vector:

YAML
resources:
  worktrees: 1
  cpu: 2
  memory_mb: 4096
  backend:
    allowed: [claude-code, codex]
    preferred: claude-code
  context_budget: 90000
  human_attention: 0

Backend quota itself becomes a schedulable resource. A task that is Claude-preferred but can run adequately under Codex does not need to block if Claude quota is under pressure. Conversely, a task with evidence that one backend performs markedly better can wait if it is not on the critical path.

The code graph can supply conflict risk:

conflict(task_a, task_b) ≈
    overlap(
        predicted_affected_neighborhood(task_a),
        predicted_affected_neighborhood(task_b)
    )

This is especially useful with git worktrees. Two tasks can be logically independent in the roadmap but likely to modify the same foundational file; the scheduler should not parallelize them merely because no explicit task edge exists.

OR-Tools’ CP-SAT scheduling examples model precedence and mutually exclusive resource use, making it a natural later candidate for multi-resource scheduling. Do not introduce CP-SAT in the first scheduler: until Maestro has reasonably calibrated duration, quota-cost, and conflict estimates, sophisticated optimization would optimize noisy inputs.

Nx provides another practical precedent: it separates project dependencies from task execution and combines affected-set computation, caching, and distribution. Maestro should copy the architecture, not the JS-monorepo product.

Multi-agent execution

The recommended default remains:

one responsible implementer per independently executable task

Additional agents are justified in three circumstances:

genuinely independent DAG nodes;

a specialist capability the current agent lacks;

independent verification for a sufficiently risky change.

Do not create a standing “debate graph” around every implementation. AutoGen GraphFlow provides controlled sequential/parallel/conditional/looped multi-agent flows, and Semantic Kernel offers group and Magentic patterns, but these primarily solve communication topology, not the underlying question of whether the extra model calls improve engineering outcomes.

State Machines / Alternative Models

Different Maestro subsystems should use different formal models.

Maestro subsystem	Recommended representation	Why
Roadmap dependencies	Mutable DAG	Real precedence and affected-set semantics
Action execution dependencies	Small typed DAG/subgraph	Parallel/conditional execution
Task lifecycle	State machine/statechart	Finite states and explicit legal transitions
Agent/backend lifecycle	State machine	Switching, loss, quota, handoff are lifecycle events
Watchdog recovery	State machine + reconciliation loop	Recovery depends on durable state, not graph traversal
Self-update	State machine + compensation/saga	Stage/validate/adopt/rollback
Repository structure	Directed property-like graph	Many typed relationships and traversal queries
Validation/cache dependencies	Incremental dependency graph	Invalidation and reuse
Provenance	Event log projected as graph	History is append-only; graph is a query/view
Scheduler	DAG frontier + resource model	Ready tasks constrained by finite resources
Human escalation	Waiting state / durable signal	Not an “agent node”
Model context	Ordinary bounded data structure	Context itself need not be a graph

Example task lifecycle:

PLANNED
  -> READY
  -> LEASED
  -> RUNNING
      -> HANDOFF_PENDING
      -> WAITING_HUMAN
      -> VERIFYING
  -> SUCCEEDED

RUNNING / VERIFYING
  -> RETRYABLE_FAILURE
  -> READY

any nonterminal
  -> BLOCKED
  -> CANCELED
  -> STALE

Example backend/agent lifecycle:

STARTING -> RUNNING -> CHECKPOINTING -> STOPPED
                      |
                      v
                HANDOFF_READY
                      |
                      v
                  SWITCHED

XState is attractive if Maestro is TypeScript because it supplies statecharts, actors, persistence, and inspection tooling under an MIT license and remained actively maintained through 2026. If Maestro is Python, the lifecycle model is small enough that a typed reducer plus Pydantic/dataclass validation may be preferable to adding a large state-machine dependency.

Petri nets are not recommended now. Their token-flow semantics are elegant for formally analyzing concurrency, synchronization, and resource consumption, but Maestro’s practical concurrency can be represented more understandably through state machines plus DAG/resource scheduling. Adopt Petri-net analysis only if deadlock/concurrency verification becomes a demonstrated operational problem.

BPMN/Camunda should likewise not become Maestro’s internal programming model. Camunda supports compensation and incident/retry behavior, demonstrating the maturity of those patterns, but it carries business-process semantics, a major platform footprint, and licensing considerations that are disproportionate to Maestro’s needs. Behavior trees could model watchdog fallback logic, but a simple state machine plus policy function is easier to audit. Process algebra is even less justified unless Maestro later requires formal concurrency verification.

Graph validation and invariants

The real benefit of formalizing graphs is not prettier diagrams; it is making invalid autonomous behavior unrepresentable or immediately rejectable.

Tier-1 invariants should include:

hard roadmap dependency graph MUST be acyclic

only READY tasks MAY acquire execution leases

a worktree MAY have at most one concurrent writer lease

a task MAY NOT become SUCCEEDED without verification evidence
unless an explicit policy exemption is recorded

verification evidence MUST identify the git revision it verified

a handoff MUST contain:
  task_id
  attempt_id
  worktree identity
  base revision
  current revision/checkpoint
  changed-files/diff summary
  completed work
  unresolved work
  latest verification state
  switch reason

a merge/adoption decision MUST satisfy the configured verification policy

a cache result MAY be reused only while every dependency fingerprint matches

roadmap mutation MUST NOT erase historical completed/failed attempts

high-risk actions MUST enter a durable approval state

self-update MAY become active only after candidate validation succeeds

last-known-good version MUST remain addressable until post-adoption health passes

These should be checked both when proposals are produced and again transactionally when state changes are committed.

Operator experience and target design
Observability and Visualization

The operator needs a live causal model, not a wall of agent transcripts.

Recommended default views:

View	Important content
Roadmap	Completed/ready/running/blocked/stale tasks and dependencies
Active execution	Running attempt, agent/backend, worktree, duration, last heartbeat
Critical path	Tasks currently determining projected completion
Resource view	Claude/Codex pressure, worktree availability, CPU/RAM, waiting jobs
Recovery	Lost leases, retries, uncertain side effects, last checkpoint
Provenance	Task → attempt → commits → tests → completion decision
Timeline	Backend switches, checkpoints, failures, escalations
Context efficiency	Files/tool calls/context used by each agent
Validation	Cached vs rerun tests and reason for each
Graph mutations	Who/what changed roadmap structure and why

Use OpenTelemetry for runtime telemetry, correlated by stable Maestro IDs. OpenTelemetry defines traces, metrics, and logs; spans provide parent/child structure and links appropriate for distributed causal relationships. Current OpenTelemetry guidance is moving event-style telemetry toward log-based events rather than relying on new span-event APIs, so Maestro should not make span events its permanent domain-event format.

In other words:

Maestro event journal = durable domain truth
OpenTelemetry         = operational telemetry
UI graph              = projection
agent transcript      = artifact

Do not confuse those layers.

The OpenAI Agents SDK offers a useful trace hierarchy to study: it records run/task/turn/agent/generation/tool/handoff/guardrail spans and supports external trace processors. Maestro can reuse that conceptual hierarchy even when agents are Claude Code or Codex rather than SDK-native agents.

Proposed Maestro Architecture

The target design has six related graph/state layers.

Layer	Source of truth	Mutation rate	Primary queries
Roadmap/task DAG	Postgres/SQLite domain tables + events	Medium	ready, blocked, descendants, critical path
Execution graph	Current action definitions + lifecycle projections	High	what can run next, what is waiting
Repository/code graph	Reproducible index keyed to git revision	High on edits	callers, imports, affected tests, context
Validation dependency graph	Artifact/dependency fingerprints	High	what must be revalidated
Provenance graph	Append-only event journal	Append-only	why/when/by whom
Knowledge/evidence graph	Deferred; domain records first	Low/medium	historical decisions and requirements

The complete data flow should look like this:

Mermaid
flowchart TD
    RM[Roadmap DAG]
    MUT[Validated graph mutation]
    SCH[Ready-frontier scheduler]
    SM[Task state machine]
    DUR[Durable executor]
    IMP[Implementer]
    WT[Git worktree]
    IDX[Incremental code graph]
    AFFECT[Affected-set engine]
    TEST[Test / build / verification]
    EV[(Event journal)]
    ART[(Artifact store)]
    PROJ[(State projections)]
    WD[Watchdog reconciler]
    UI[Operator UI]
    TG[Telegram]
    CACHE[(Validation cache)]

    RM --> SCH
    MUT --> RM
    SCH --> SM
    SM --> DUR
    DUR --> IMP
    IMP --> WT

    IDX --> IMP
    IDX --> AFFECT
    WT --> IDX
    WT --> AFFECT
    AFFECT --> CACHE
    CACHE --> TEST
    AFFECT --> TEST
    TEST --> SM

    IMP --> ART
    TEST --> ART

    RM --> EV
    MUT --> EV
    SM --> EV
    DUR --> EV
    IMP --> EV
    TEST --> EV

    EV --> PROJ
    PROJ --> WD
    PROJ --> UI

    WD --> SM
    UI --> TG
    TG --> SM

    TEST -. new discovery .-> MUT
    IMP -. discovered prerequisite .-> MUT
Control-plane relational model

A minimal schema could be:

SQL
CREATE TABLE roadmap_node (
    node_id            TEXT PRIMARY KEY,
    node_type          TEXT NOT NULL,
    title              TEXT NOT NULL,
    lifecycle_state    TEXT NOT NULL,
    risk_class         TEXT NOT NULL DEFAULT 'normal',
    version            INTEGER NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL,
    updated_at         TIMESTAMPTZ NOT NULL
);

CREATE TABLE roadmap_edge (
    from_node_id       TEXT NOT NULL REFERENCES roadmap_node(node_id),
    to_node_id         TEXT NOT NULL REFERENCES roadmap_node(node_id),
    edge_type          TEXT NOT NULL,
    reason             TEXT NOT NULL,
    created_event_id   TEXT NOT NULL,
    PRIMARY KEY (from_node_id, to_node_id, edge_type),
    CHECK (from_node_id <> to_node_id)
);

CREATE TABLE maestro_event (
    sequence           BIGSERIAL PRIMARY KEY,
    event_id           TEXT UNIQUE NOT NULL,
    occurred_at        TIMESTAMPTZ NOT NULL,
    event_type         TEXT NOT NULL,
    aggregate_type     TEXT NOT NULL,
    aggregate_id       TEXT NOT NULL,
    run_id             TEXT,
    attempt_id         TEXT,
    correlation_id     TEXT,
    causation_id       TEXT,
    schema_version     INTEGER NOT NULL,
    payload            JSONB NOT NULL
);

CREATE INDEX maestro_event_aggregate_idx
ON maestro_event (aggregate_type, aggregate_id, sequence);

CREATE TABLE artifact (
    artifact_id        TEXT PRIMARY KEY,
    artifact_type      TEXT NOT NULL,
    content_hash       TEXT NOT NULL,
    git_revision       TEXT,
    uri                TEXT NOT NULL,
    created_event_id   TEXT NOT NULL
);

CREATE TABLE artifact_dependency (
    artifact_id        TEXT NOT NULL REFERENCES artifact(artifact_id),
    dependency_type    TEXT NOT NULL,
    dependency_key     TEXT NOT NULL,
    dependency_hash    TEXT NOT NULL,
    PRIMARY KEY (artifact_id, dependency_type, dependency_key)
);

Acyclicity is intentionally enforced in the graph-mutation transaction rather than by pretending a simple SQL CHECK constraint can validate arbitrary reachability.

Repository graph model
SQL
CREATE TABLE code_node (
    id            INTEGER PRIMARY KEY,
    revision      TEXT NOT NULL,
    kind          TEXT NOT NULL,
    qualified_name TEXT,
    file_path     TEXT NOT NULL,
    start_line    INTEGER,
    end_line      INTEGER,
    fingerprint   TEXT NOT NULL
);

CREATE TABLE code_edge (
    revision      TEXT NOT NULL,
    src_id        INTEGER NOT NULL,
    dst_id        INTEGER NOT NULL,
    edge_type     TEXT NOT NULL,
    confidence    REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (revision, src_id, dst_id, edge_type)
);

CREATE INDEX code_edge_reverse_idx
ON code_edge (revision, dst_id, edge_type);

The indexer should update only changed files and their incident cross-file relationships. Tree-sitter’s incremental parsing model and Nx’s cached/recomputed graph model both provide useful precedent for avoiding complete re-analysis on every edit.

Runnable readiness/scheduling skeleton

For a Python Maestro, the first DAG logic genuinely can be this small:

Python
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import networkx as nx


@dataclass(frozen=True)
class Task:
    task_id: str
    state: str
    estimated_cost: float = 1.0
    quota_pressure: float = 0.0


SUCCESS = "SUCCEEDED"
WAITING = {"PLANNED", "READY"}


def validate_dag(graph: nx.DiGraph) -> None:
    if not nx.is_directed_acyclic_graph(graph):
        cycle = nx.find_cycle(graph)
        raise ValueError(f"Hard dependency cycle rejected: {cycle}")


def ready_tasks(
    graph: nx.DiGraph,
    tasks: dict[str, Task],
) -> list[str]:
    validate_dag(graph)

    ready: list[str] = []
    for node in graph.nodes:
        task = tasks[node]
        if task.state not in WAITING:
            continue

        predecessors: Iterable[str] = graph.predecessors(node)
        if all(tasks[p].state == SUCCESS for p in predecessors):
            ready.append(node)

    return ready


def criticality(graph: nx.DiGraph, node: str) -> int:
    """Simple first-version proxy: number of downstream tasks."""
    return len(nx.descendants(graph, node))


def choose_next(
    graph: nx.DiGraph,
    tasks: dict[str, Task],
) -> str | None:
    candidates = ready_tasks(graph, tasks)
    if not candidates:
        return None

    return max(
        candidates,
        key=lambda node: (
            2.0 * criticality(graph, node)
            - tasks[node].estimated_cost
            - 1.5 * tasks[node].quota_pressure
        ),
    )

NetworkX already includes the necessary DAG primitives and is BSD-licensed. The point is not that this exact scoring formula is optimal; the point is that Maestro does not need a distributed graph database or AI framework to obtain useful graph semantics.

Suggested configuration boundary
YAML
maestro:
  control_plane:
    event_store: postgres
    projection_store: postgres
    graph_schema_version: 1

  roadmap:
    hard_dependencies_must_be_acyclic: true
    preserve_superseded_nodes: true
    mutation_requires_version_match: true

  execution:
    durable_backend: internal   # internal | temporal | dbos
    activity_idempotency: required
    lease_timeout_seconds: 120
    heartbeat_seconds: 20

  code_graph:
    enabled: true
    parser: tree-sitter
    store: sqlite
    max_traversal_depth: 3
    context_budget_tokens: 24000
    fallback_search_on_low_confidence: true

  validation:
    affected_mode: conservative
    cache_results: true
    invalidate_on_unknown_dependency: true

  scheduling:
    strategy: greedy-critical-path
    prevent_overlapping_worktree_writers: true
    quota_aware: true
    code_conflict_aware: true

  observability:
    domain_events: postgres
    telemetry: opentelemetry
    include_model_payloads: false

Model/tool payloads warrant special caution. The OpenAI Agents SDK documentation notes that traces may contain sensitive model and function inputs/outputs and provides options not to capture them. Maestro should default toward hashes/artifact references and selectively retained content rather than replicating entire potentially secret-bearing coding sessions into every telemetry backend.

Delivery, experiments, and reuse
Implementation Priorities

Tier 1 — Implement soon

Proposal	Minimal first version	Estimated effort	Primary risk
Domain event journal + projections	Append-only events, causation/correlation IDs, task projection	2–3 engineer-weeks	Schema churn
Typed lifecycle state machines + invariants	Task, agent/handoff, self-update; transition tests	2–3 ew	Too many states
Versioned roadmap DAG + mutation transactions	hard dependencies, ready set, split/supersede/invalidate	2–3 ew	Incorrect dynamic mutation
Incremental affected/validation graph	changed files → components/tests; dependency fingerprints; cache	2–4 ew	Missing dependency produces unsafe reuse
Basic dependency/resource scheduler	ready frontier + priority/resource constraints	1–2 ew	Poor duration/quota estimates
Repository graph MVP	Tree-sitter files/symbols/imports/refs/calls + SQLite	4–6 ew	Language-resolution quality
Graph-guided context manifest	seed → traverse → rank → token budget	2–3 ew after graph MVP	Poor recall causing missed context
Provenance/operator view	task/attempt/backend/commit/test timeline	2–3 ew	UI scope creep

Several work streams overlap; the sum is not the calendar duration.

Tier 2 — Prototype / benchmark

Candidate	Experiment
Temporal	Crash/restart/backend-switch benchmark against internal event-loop implementation
DBOS	Same benchmark; emphasize operational footprint and Postgres integration
RepoSkein / CodeWiki	Compare against Maestro’s own Tree-sitter MVP on representative repositories
Joern	Benchmark on tasks requiring data/control-flow reasoning
Personalized PageRank / graph ranking	Compare against typed breadth-first neighborhood + BM25
OR-Tools scheduler	Introduce after workload estimates become measurable
Reviewer/verifier agent	Measure net success gain after accounting for extra model consumption
Graphiti / temporal KG	Test only on decision/requirement-history questions

Tier 3 — Keep in architecture

A richer requirement/evidence graph, distributed code-graph service, deeper CPG extraction, learned scheduling, cross-repository knowledge, explicit AND/OR strategy branches, and graph-based experience learning all belong here. They are plausible future capabilities, but none should block the simpler architecture.

Tier 4 — Reject / avoid

A universal graph database, GraphRAG over raw source as the primary code index, default LangGraph adoption, routine multi-agent debate, general graph rewriting languages, BPMN as Maestro’s programming model, Petri nets for normal task execution, and MCTS/Graph-of-Thought planning by default should be rejected for now.

Proposed roadmap with milestones and estimated effort

The following timeline assumes two engineers and an illustrative start date of September 7, 2026; the actual start date was unspecified.

Mermaid
gantt
    title Illustrative Maestro graph-engineering delivery timeline
    dateFormat  YYYY-MM-DD
    axisFormat  %b %d

    section Durable control plane
    Event journal and projections       :a1, 2026-09-07, 10d
    Lifecycle state machines/invariants :a2, 2026-09-07, 15d
    Roadmap DAG and graph mutations     :a3, 2026-09-21, 15d

    section Efficiency
    Incremental validation graph        :b1, 2026-09-28, 20d
    Repository graph MVP                :b2, 2026-09-28, 25d
    Graph-guided retrieval              :b3, after b2, 15d

    section Execution
    Resource-aware scheduler            :c1, 2026-10-12, 15d
    Provenance/operator views           :c2, 2026-10-12, 20d

    section Architecture bake-off
    Temporal vs DBOS benchmark          :d1, 2026-10-05, 15d
    Durable-runtime decision            :milestone, d2, after d1, 0d

    section Hardening
    Failure-injection and integration   :e1, 2026-11-09, 15d
    Tier-1 acceptance decision          :milestone, e2, after e1, 0d

A realistic Tier-1 total is approximately 18–28 engineer-weeks, with an elapsed duration around 10–13 weeks for two experienced engineers if work is parallelized. Repository-graph language coverage is the largest uncertainty.

Proposed Experiments

The report’s most important uncertain claims should be decided experimentally.

Experiment	Baseline	Treatment	Workload	Primary metrics	Decision threshold
Code graph retrieval	Claude/Codex normal repo exploration	Graph-provided context manifest	30–100 real tasks across ≥5 repos	success, files read, tool calls, context, elapsed time	Adopt only if success is non-inferior and exploration falls materially
Affected validation	Run complete standard verification	Graph-selected tests/checks	Historical commits + injected changes	missed failure rate, tests executed, time	Near-zero missed regressions; meaningful execution reduction
Crash recovery	Existing watchdog loop	Event-sourced state/reconciler	Kill orchestrator/worker at randomized points	completion rate, duplicate effects, MTTR	≥99% recoverable injected runs without silent corruption
Temporal/DBOS	Maestro-native durable loop	Temporal and DBOS adapters	quota failure, process death, HITL wait, backend switch	code complexity, recovery, ops burden, throughput	Choose only if net complexity/reliability beats native
Graph ranking	BM25/vector-only	typed traversal / PPR / hybrid	localization corpus	Recall@K, MRR, tokens	Use algorithm giving best downstream task success
Parallel scheduling	FIFO ready queue	critical-path/resource scheduler	synthetic + real multi-task roadmaps	makespan, quota waste, merge conflicts	Adopt sophisticated scoring only with measured gain
Reviewer agent	implementer + deterministic tests	+ independent reviewer	high-risk task subset	defect escape, cost, latency	Keep only if corrected defects justify model cost
Temporal memory graph	event/search retrieval	Graphiti-style temporal KG	long-lived project decisions	answer accuracy, latency, ingestion cost	Add only if repeated reasoning materially decreases
Code-graph benchmark protocol

For each task, start with a fresh agent so previous context does not bias results. Pin identical repository revision, model/backend, prompt, tool permissions, and time limits. Treatment A gives normal repository tools. Treatment B initially provides only the graph-selected context plus a fallback mechanism.

Record:

task_correct
tests_passed
agent_steps
repository_tool_calls
grep/search_calls
files_opened
unique_lines_read
input_tokens
output_tokens
context_peak
elapsed_seconds
fallback_search_used
graph_seed_recall
graph_context_precision

The decisive metric is not token reduction alone:

useful_work_efficiency =
    verified_tasks_completed
    / (model_cost + exploration_time + failed_rework)

LocAgent and RIG make this experiment especially worth doing, but their positive results should be treated as priors, not assumed Maestro results.

Failure-injection matrix

At minimum inject failures at:

before agent starts
during model call
after worktree edit but before checkpoint
after checkpoint but before event write
after event write but before acknowledgement
during test
during backend handoff
while waiting for human
during roadmap mutation
during self-update validation
immediately after self-update activation

Acceptance should verify there is exactly one explainable outcome: resume, compensate, retry safely, preserve an uncertain attempt for inspection, or request human action.

Evaluation metrics
Metric	Definition
Task success rate	Verified tasks completed / attempted
Useful autonomy	Verified tasks completed between human interventions
Recovery success	Correctly resumed recoverable runs / injected recoverable failures
Mean time to resume	Failure detection → useful execution resumed
Duplicate-side-effect rate	Duplicated externally visible actions / retries
Exploration calls/task	Search/read/navigation calls before successful task completion
Files read/task	Unique repository files consumed
Context efficiency	Successful tasks / input-context volume
Affected-set recall	Truly required validations selected / all truly required validations
Affected-set precision	Required validations selected / validations executed
Validation cache hit rate	Safe reused validation artifacts / eligible validations
Scheduler makespan	Roadmap start → completion under fixed resource budget
Backend utilization	Useful agent runtime / backend-available time
Parallel conflict rate	Parallel tasks requiring conflict/rework / parallel task pairs
Handoff success	Fresh-agent continuations completed without rediscovery/restart
Provenance completeness	Completion decisions linked to required commit/test/evidence records
Graph freshness	Changed relevant source entities reflected before next retrieval
Retrieval Recall@K	Ground-truth relevant entities contained in top-K context candidates
Projects Worth Reusing
Project	URL	Purpose / language	License	Activity / maturity evidence	Exact Maestro use	Difficulty	Decision
Temporal	github.com/temporalio/temporal	Durable workflow server; Go core, multi-language SDKs	MIT	Official repo showed 21k+ stars, 171 releases and v1.31.2 in July 2026.	Durable task execution, waits, retries, recovery, handoffs	Medium-high	Prototype → likely adopt
DBOS Transact	github.com/dbos-inc/dbos-transact-py	Postgres-backed durable Python workflows	MIT	Active through August 2026; releases included 2.28.0 in July.	Lightweight durable control plane	Medium	Prototype against Temporal
LangGraph	github.com/langchain-ai/langgraph	Stateful agent/workflow graphs; Python	MIT	Very active, with 1.2.9 shown in July 2026.	Study checkpoints/subgraphs/interrupt semantics; possibly isolated agent workflows	Medium	Study/wrap, not core
Pydantic AI / Graph	github.com/pydantic/pydantic-ai	Typed Python agents/graphs and durability integrations	MIT-family project; verify exact package notice in adoption review	Rapid 2026 releases; official docs support four durability platforms.	Typed schemas; possible graph helper if Maestro is Python	Low-medium	Study
XState	github.com/statelyai/xstate	Statecharts and actor model; TypeScript/JavaScript	MIT	Large, mature and actively maintained.	Task/agent/self-update lifecycle if Maestro is TS	Low	Adopt if TS; otherwise copy model
NetworkX	github.com/networkx/networkx	General graph algorithms; Python	BSD-3-Clause	17k stars and very broad downstream use; 3.6.1 released Dec. 2025.	DAG validation, descendants, critical path, traversal	Low	Adopt if Python
Tree-sitter	github.com/tree-sitter/tree-sitter	Incremental source parsing; Rust/C core	MIT	Mature, broadly used; v0.26.9 shown May 2026.	Repository graph extraction	Medium	Adopt
tree-sitter-graph	github.com/tree-sitter/tree-sitter-graph	Graph construction DSL over parsed source; Rust	Apache-2.0/MIT	Official Tree-sitter project.	Evaluate as extraction layer	Medium	Prototype / wrap
Aider RepoMap	github.com/Aider-AI/aider	Coding-agent repository mapping; Python	Apache-2.0	Mature project with continued 2026 development evidence.	Copy/study graph ranking and token-budget ideas	Low	Study/copy conceptually
Joern	github.com/joernio/joern	Code Property Graph/static analysis; Scala	Apache-2.0	Thousands of releases; active through July 2026.	Deep on-demand call/data/control analysis	High	Wrap selectively
RepoSkein	github.com/reposkein/reposkein	Deterministic local code graph/MCP; implementation details should be verified at adoption time	License not established by retrieved primary snippet	New 2026 project	Fast external test of graph-guided coding	Low	Benchmark, not depend on yet
CodeWiki	github.com/0xsyncroot/codewiki	Tree-sitter + SQLite code graph/MCP; Rust	MIT	New 2026 project; publishes reproducible benchmark methodology according to its repo.	Benchmark impact/context APIs and architecture	Low	Tier-2 benchmark
OR-Tools	github.com/google/or-tools	Constraint/optimization toolkit	Apache-2.0	Mature Google project	Later resource-constrained scheduling	Medium	Tier 2/3
OpenTelemetry	opentelemetry.io	Traces, metrics, logs	Open-source CNCF ecosystem	Mature cross-language standard	Correlated runtime telemetry	Medium	Adopt
Graphiti	github.com/getzep/graphiti	Temporal/episodic knowledge graph	Apache-2.0	Active through July 2026.	Later decision/experience memory benchmark	Medium-high	Tier 3
Microsoft GraphRAG	github.com/microsoft/graphrag	Graph-assisted RAG over text corpora	Project license should be reviewed for exact deployment	Active through 2026; v3.1.0 reported May 2026.	Study hybrid graph retrieval patterns	High	Do not use as source-code index
Ideas Explicitly Rejected

Universal “Maestro graph.” A task dependency DAG is frequently mutated and strongly consistent; a code graph is reproducible from git; provenance is append-only; execution state is transient; knowledge memory is uncertain/probabilistic. One universal graph would force unrelated consistency semantics into a single schema.

Replacing Maestro with LangGraph. LangGraph is a mature and active graph-agent runtime with useful checkpointing. It does not eliminate Maestro’s need for roadmap semantics, worktree isolation, side-effect reconciliation, validation invalidation, quota/resource scheduling, or domain provenance. Adopting it wholesale would trade explicit Maestro architecture for framework coupling without solving the hardest parts.

LLM-generated GraphRAG for code structure. Microsoft GraphRAG’s local/global retrieval architecture is useful for natural-language corpora. Parsing source deterministically with Tree-sitter is cheaper, more reproducible, and avoids hallucinated import/call edges for the basic code relationships Maestro needs.

Neo4j/Memgraph by default. A graph database can be introduced when queries or scale justify it. It should not be a prerequisite for storing a few typed adjacency tables and doing short traversals.

Always-on Joern. Joern is an excellent deep analysis platform, not the cheapest universal repository map. Run it when a task actually needs its richer CPG semantics.

Graphiti as Maestro’s primary memory. Its temporal graph approach is interesting and active, but adding LLM-backed knowledge extraction before Maestro has proven deficiencies in ordinary typed provenance would increase ingestion cost and ambiguity.

MCTS / graph-of-thought planning. Software-engineering action-state spaces and costs are difficult to model faithfully, and tree expansion can multiply expensive model calls. Keep the roadmap planner hierarchical and repair-oriented until Maestro has evidence that explicit alternative search improves verified task success.

Default multi-agent debate or group chat. The existence of group/Magentic/GraphFlow frameworks shows these patterns are easy to implement, not that they are efficient for coding. Additional agents should earn their quota through measurable defect reduction or parallel work.

Generic graph-rewriting engine. Maestro needs six or seven well-defined transactional mutations, not a general graph-rewrite language.

BPMN/Camunda as core. Compensation and incidents are useful patterns, but the full platform is disproportionate.

Petri nets for normal execution. Reconsider only if Maestro eventually needs formal deadlock/resource-flow verification beyond what state machines and constraint scheduling can provide.

Open Questions

The following cannot be answered reliably from public research alone:

How much repository exploration is Maestro actually wasting? No existing Maestro trace baseline was provided. The code-graph business case depends heavily on how often fresh Claude/Codex agents repeat searches and reads.

How large are Maestro’s repositories? A lightweight SQLite/Tree-sitter graph is likely sufficient for a broad range of projects, but the graph-storage decision should be tested on Maestro’s actual largest targets.

What is Maestro written in? TypeScript strengthens XState; Python strengthens NetworkX/Pydantic/DBOS options.

What persistence infrastructure already exists? If PostgreSQL is already mandatory, DBOS becomes more attractive. If Maestro is intentionally zero-service/local-only, Temporal’s operational cost becomes more significant.

Can Claude Code/Codex expose stable checkpoint/resume boundaries? If their only interface is an opaque CLI subprocess, activity recovery must rely more heavily on worktree/git inspection.

How observable are subscription quotas? Exact quota-aware optimization is impossible if Maestro only sees sudden exhaustion. Coarse pressure models remain possible.

What percentage of tasks can actually run safely in parallel? The answer determines the value of sophisticated scheduling.

How complete can test/component dependencies be inferred? If repositories have extensive dynamic loading, generated code, runtime configuration, or integration behavior, affected-test selection must remain conservative.

Does Temporal’s operational footprint beat a small database-backed reconciler at Maestro’s initial scale? This is the most important control-plane bake-off.

Will graph-provided context cause agents to stop exploring, or merely add another tool call? Only a controlled agent benchmark can resolve the additive-tooling failure mode called out in the research brief.

Evidence and final decisions
Sources

The following primary/creator sources carry most of the report’s conclusions.

Area	Key primary sources
Research specification	Attached Maestro research brief.
Durable execution	Temporal, Temporal workflow/history and Worker Versioning documentation; Pydantic AI’s official Temporal integration.
Lightweight durability	DBOS Transact, Restate.
Agent graph runtime	LangGraph persistence, interrupt, subgraph and functional APIs.
Typed graph/durability separation	Pydantic AI Graph Builder and durable-execution documentation.
Incremental computation	Bazel Skyframe, Salsa, Buck2 DICE.
Monorepo affected computation	Nx project/task graph and affected documentation; Pants changed/dependents.
Parsing/code graph	Tree-sitter, tree-sitter-graph, Joern.
Agent repository mapping	Aider RepoMap documentation.
Software-engineering graph research	RepoGraph, LocAgent, SWE-Explore, RIG.
Graph retrieval	Microsoft GraphRAG local/global retrieval.
Temporal knowledge graphs	Graphiti and associated research.
Statecharts	XState persistence/actors/inspection.
Scheduling	NetworkX, Google OR-Tools.
Observability/provenance	OpenTelemetry and OpenLineage.
Multi-agent control	AutoGen GraphFlow, Semantic Kernel agent orchestration, OpenAI Agents SDK handoffs/tracing.
Final decision matrix
Proposal	Expected impact	Confidence	Complexity	Adoption strategy	Priority
Append-only orchestration event journal	Very high	High	Medium	Implement internally	Tier 1
Explicit task/agent/self-update state machines	Very high	High	Low-medium	Implement internally; XState if TS	Tier 1
Versioned dynamic roadmap DAG	Very high	High	Medium	Implement internally	Tier 1
Transactional graph invariants/mutations	High	High	Medium	Implement internally	Tier 1
Incremental affected/validation graph	High	High	Medium	Borrow Skyframe/Salsa/Nx patterns	Tier 1
Validation artifact caching	High	High	Medium	Implement internally	Tier 1
Tree-sitter repository graph	High	Medium-high	Medium	Adopt parser; build small index	Tier 1
Graph-guided context packing	High	Medium	Medium	Implement and benchmark	Tier 1
Greedy critical-path/resource scheduler	High	High	Low-medium	NetworkX/internal	Tier 1
Causal provenance/operator views	High	High	Medium	Internal + OpenTelemetry	Tier 1
Temporal durability substrate	Very high	Medium-high	Medium-high	Benchmark then adopt	Tier 2
DBOS durable substrate	High	Medium	Medium	Benchmark against Temporal	Tier 2
RepoSkein/CodeWiki reuse	Potentially high	Medium-low	Low	Benchmark before dependency	Tier 2
Joern deep CPG integration	Medium-high on special tasks	High	High	Wrap on demand	Tier 2
OR-Tools resource scheduling	Medium later	High	Medium-high	Prototype after metrics exist	Tier 2
Independent reviewer agent	Medium	Medium-low	Medium	Benchmark only on risky tasks	Tier 2
Graphiti decision/episodic memory	Medium	Medium-low	High	Benchmark later	Tier 3
Rich AND/OR strategy graph	Medium	Medium	Medium	Preserve schema option	Tier 3
Distributed graph database	Low now	High	High	Defer until scale proves need	Tier 3
LangGraph as Maestro core	Low/negative	High	High migration/coupling	Reject wholesale adoption	Tier 4
Microsoft GraphRAG as source-code index	Low/negative	High	High	Reject; borrow retrieval ideas	Tier 4
Universal Maestro graph/ontology	Negative	High	Very high	Reject	Tier 4
Always-on Joern for every repository	Low relative benefit	High	High	Reject default deployment	Tier 4
Default multi-agent group/debate graph	Likely negative efficiency	Medium-high	Medium-high	Reject by default	Tier 4
BPMN/Camunda control plane	Negative complexity	High	Very high	Reject	Tier 4
Petri-net core execution model	Low now	Medium-high	High	Reject for now	Tier 4
MCTS/Graph-of-Thought planning by default	Low/uncertain	Medium	High model cost	Reject for now	Tier 4
Top 5 things Maestro should implement

A durable, append-only domain event journal plus explicit state machines and runtime invariants. This changes recovery from guessing what the loop was doing to reconstructing known state and deciding the next legal transition.

A versioned, dynamically mutable roadmap DAG with transactional add, split, supersede, invalidate, and alternative-selection operations. This is the foundation for intelligent continuation, local replanning, dependency-aware parallelism, and explainable blocking.

An incremental validation/affected graph modeled after Skyframe/Salsa/Nx principles. Record why a build/test/analysis result was valid; after a change, invalidate only results whose dependencies changed. This directly attacks repeated engineering work rather than merely reorganizing control flow.

A deterministic Tree-sitter repository graph with graph-guided context selection. Start with definitions, references, imports, calls, inheritance, build targets, and tests; expose compact context manifests to fresh Claude Code/Codex agents. Aider, RepoGraph, LocAgent, and RIG provide enough evidence to make this a high-priority benchmark.

A simple dependency/resource-aware scheduler backed by causal provenance and a reconciler-style watchdog. Schedule the ready DAG frontier against worktrees, backend quota pressure, CPU/RAM, predicted code conflict, and critical path; make every selection explainable. Prototype Temporal and DBOS behind this abstraction instead of letting either runtime define Maestro’s domain architecture.

Top 5 graph ideas Maestro should avoid for now

Do not create one giant graph containing execution, roadmap, source code, agents, memory, and provenance.

Do not make LangGraph—or any other LLM graph framework—the architectural center of Maestro merely because Maestro has agents.

Do not build an LLM-generated GraphRAG knowledge graph of source code when deterministic parsers can provide the important structural edges.

Do not multiply agents through debate/group-chat/Graph-of-Thought/MCTS structures unless controlled experiments demonstrate higher verified engineering success after accounting for quota and latency.

Do not introduce heavyweight graph infrastructure—Neo4j/Memgraph, always-on Joern, BPMN, Petri nets, or a generic graph-rewrite engine—before a concrete query or reliability requirement exceeds what typed tables, state machines, Tree-sitter, NetworkX, and a durable event log can deliver.

The resulting architecture is intentionally less glamorous than “Maestro becomes a graph-native multi-agent platform.” It is also more defensible: the roadmap becomes a graph where dependencies matter; code becomes a graph where structure saves exploration; validation becomes a graph where dependency knowledge saves work; history becomes an event stream where causality enables recovery; and lifecycle control remains a state machine where finite semantics are clearer than arbitrary edges. Durable execution can then be delegated to Temporal or DBOS if benchmarking shows that doing so buys more reliability than operational complexity. That combination gives Maestro the graph capabilities most likely to improve long-duration autonomy without turning graph engineering itself into the new source of fragility. State-of-the-Art Graph Engineer…
