# MAESTRO EXPERIMENTS, RESEARCH DISPOSITION & AMENDMENTS (AGENT SPECIFICATION)

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
