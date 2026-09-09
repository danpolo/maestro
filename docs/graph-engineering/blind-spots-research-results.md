# Maestro Graph Engineering Targeted Research

The completed research deliverable is available here:

[Download `GRAPH_ENGINEERING_TARGETED_RESEARCH.md`](sandbox:/mnt/data/GRAPH_ENGINEERING_TARGETED_RESEARCH.md)

The attached `MAESTRO_GRAPH_ENGINEERING_ARCHITECTURE.html` was treated as the baseline architecture rather than an external factual authority. In particular, I evaluated its native SQLite durability decision, model/effort routing seam, separate subscription-window accounting, and conservative repository-intelligence design rather than reopening the broader graph-engineering work. fileciteturn0file0

## Executive findings

The central architecture survives this targeted pass, but several parts can now be made considerably more concrete.

**Paid routing:** Maestro can obtain a usable 5-hour and weekly allowance-efficiency metric for all three paid backends. Claude Code is substantially better instrumented than the architecture assumed: for Claude.ai Pro/Max accounts, its documented status-line JSON can directly expose `five_hour.used_percentage`, `seven_day.used_percentage`, and the reset timestamps. That makes isolated before/after task attribution feasible without equating tokens with subscription usage. citeturn24view0

Codex also exposes 5-hour and weekly allowance state through `/status` and its web usage surface, but automation is less clean. A stable noninteractive `codex status --json` was still an open feature request in the official Codex repository; current session files can contain rate-limit snapshots but are not dependable enough to be the sole source. Reports in OpenAI’s own issue tracker also show stale or internally inconsistent quota displays, so Maestro should preserve raw samples and support conflicting/censored observations rather than pretending every reading is exact. citeturn23search0turn23search2turn23search10turn23search11

Antigravity’s `/usage`/`/quota` command is documented to refresh model-quota information from Google’s backend and display the active limits. This gives Maestro a direct observable signal, although the current documentation does not expose as clean a structured machine-readable quota feed as Claude does. A version-qualified PTY/TUI collector is therefore the best present implementation. citeturn23search7

**Durability:** do not replace Maestro-native durability now. Temporal, DBOS and Restate can all remove generic durable-workflow machinery, but all leave Maestro responsible for the difficult part: proving whether an opaque coding-agent CLI or subprocess is still editing a Git worktree, reconciling ambiguous external effects, fencing writers, preserving checkpoints and safely switching backends. Temporal Activities themselves require application-level side-effect discipline; DBOS explicitly documents at-least-once steps and possible “zombie” concurrent executions; Restate’s durable journal likewise cannot physically fence a child process from the filesystem. citeturn18search1turn16search5turn16search14turn19search0

**Repository intelligence:** choose Tree-sitter as the smallest optional syntax layer, shortlist SCIP for deeper precise definition/reference navigation, and retain Joern only as an on-demand specialist for control/data-flow and security-oriented analysis. Tree-sitter is explicitly incremental and tolerant of syntactically broken intermediate code; SCIP now has broad mature indexer coverage and an open community-governed protocol; Joern supplies materially deeper program-analysis semantics at substantially higher cost. citeturn20search4turn20search9turn20search11turn21search0 Stack Graphs should leave the active shortlist because its official repository was archived in September 2025. citeturn20search7

Critically, no current provider has established that structural repository intelligence can **replace** ordinary coding-agent exploration without a quality loss. A 2026 Tree-sitter knowledge-graph evaluation reported roughly tenfold lower tokens and 2.1-times fewer tool calls, but 83% answer quality versus 92% for a file-exploration agent. That argues for structural context plus fallback search/read, not structural context instead of search/read. citeturn20academia47

## Paid backend, model and effort routing

The approximately-$20 subscriptions currently correspond to **Claude Pro at $20/month**, **ChatGPT Plus at $20/month**, and **Google AI Pro at $19.99/month** in the US. citeturn5search8turn2search3turn12search2 These values were verified on September 8, 2026 and should remain runtime/catalog facts with verification timestamps rather than constants in Maestro source.

| Backend | Current basic plan | Current coding-agent model situation | Allowance structure | Measurement quality |
|---|---|---|---|---|
| Claude Code | Claude Pro, $20 | Sonnet 4.6 is the current Pro Claude Code default; Opus is not the normal Pro entitlement. citeturn24view4turn24view5 | 5-hour plus 7-day rate-limit windows | **High**: documented structured percentages and reset timestamps |
| Codex CLI | ChatGPT Plus, $20 | Current GPT-5.6 Codex tiers include Sol, Terra and Luna for Plus-class access; Sol is positioned for difficult work, Terra as balanced, Luna as cost-sensitive/high-volume. citeturn4view2turn14search1turn14search0turn14search2 | Shared 5-hour window plus additional weekly limit | **Medium**: direct UI/dashboard signal, weaker automation and known display inconsistencies |
| Antigravity CLI | Google AI Pro, $19.99 | Current docs expose Gemini Pro/Flash choices and paid-plan non-Gemini choices including Claude Thinking models and GPT-OSS. citeturn24view3 | 5-hour refreshes until weekly cap; Gemini and non-Gemini pool behavior differs | **Medium**: refreshed `/usage` signal, currently TUI-oriented |

Anthropic’s older Pro guidance gives rough figures such as approximately 10–40 Claude Code prompts per five-hour period, but those are planning ranges rather than a measurement unit: project size, context and workload alter usage materially. citeturn8search0 The architecture was therefore right not to model “one prompt” or “one token” as a fixed subscription charge. fileciteturn0file0

Claude now gives Maestro almost exactly the signal it needs. Its status-line JSON can contain:

```text
rate_limits.five_hour.used_percentage
rate_limits.five_hour.resets_at

rate_limits.seven_day.used_percentage
rate_limits.seven_day.resets_at
```

and separately exposes model identity, effort, context-window size, context/token information, API duration and session identity. The documentation explicitly distinguishes these fields; context-window percentage is not the subscription rate-limit percentage. citeturn24view0

For a task inside one unchanged quota epoch:

```text
delta_5h_pp =
    used_5h_after - used_5h_before

delta_weekly_pp =
    used_weekly_after - used_weekly_before
```

A changed reset timestamp, negative delta, missing rate-limit field, or known unrelated account activity should produce a `censored` or `contaminated` observation rather than zero usage.

Codex should use the same formula over its directly displayed 5-hour and weekly state. The difficulty is acquisition, not the metric itself. OpenAI’s official-repository request for `codex status --json` shows the desired internal representation as primary 300-minute and secondary 10080-minute windows, but that issue was requesting a stable command rather than documenting one already available. citeturn23search0 Maestro should first detect whether the installed version has since gained such a command; otherwise it should drive `/status` in a PTY and version its parser.

That collector needs unusual care. OpenAI’s issue tracker contains reports of the first `/status` showing stale values, session files with `rate_limits = null`, and `usage_limit_exceeded` errors disagreeing with displayed quota. These are unofficial observations rather than plan rules, but they are credible evidence that the measurement layer must retain raw artifacts and signal disagreement. citeturn23search2turn23search10turn23search11turn23search13 A zero-work control run should therefore measure the apparent overhead/drift of Maestro’s own status-probe sequence.

Antigravity should similarly be probed immediately before and after a task through `/usage`. Google says this command refreshes quota status from the backend rather than merely displaying an old local counter. citeturn23search7 Google also documents an important pool distinction: Gemini models consume a shared Gemini quota normalized according to model pricing ratios, while non-Gemini models are subject to a separate fixed rate limit because of model capacity. citeturn10view1 `account_pool_id` therefore cannot simply mean “Google account”; Maestro needs separate pool identities.

For a `/usage` panel that provides remaining percentage:

```text
delta_window_pp =
    remaining_before - remaining_after
```

For a panel exposing raw units and known total capacity:

```text
delta_window_pp =
    100 * (remaining_units_before - remaining_units_after)
        / capacity_units
```

When Antigravity exposes units without a denominator, Maestro should store the raw unit delta and estimate percentage consumption from a locally calibrated `(pool, model, effort, task-class)` mapping. It should not manufacture a percentage from tokens alone.

The route-level metrics should include failed attempts and rework. For route `r` within one matched task class/difficulty stratum, let `V_i` equal one only when Maestro eventually verifies task `i`; let `D5_i` and `DW_i` be attributable allowance consumed by all attempts/rework:

```text
five_hour_pp_per_verified_task
    = ΣD5_i / ΣV_i

weekly_pp_per_verified_task
    = ΣDW_i / ΣV_i

verified_tasks_per_100pct_5h
    = 100 * ΣV_i / ΣD5_i

verified_tasks_per_100pct_weekly
    = 100 * ΣV_i / ΣDW_i
```

A failed task contributes consumed allowance to the numerator while contributing no verified success. If a route consumes allowance but produces no verified successes, record `verified_tasks_per_100pct = 0` and the inverse cost as effectively infinite/no verified success observed rather than leaving the metric blank.

The two windows remain completely separate. **No weighted sum of 5-hour and weekly percentages should be introduced.** When routes trade off—one cheaper on five-hour usage and another cheaper on weekly usage—admission should consider whichever pool currently has tighter usable headroom after Maestro’s completion/recovery reserve, while preserving both predicted costs independently.

Reasoning/effort belongs in that calibration. Claude Code currently exposes native effort states on supported models and reports the live effort in its structured status data. citeturn24view4turn24view0 Current GPT-5.6 model documentation exposes effort controls across the Sol/Terra/Luna family, while OpenAI explicitly says Codex usage depends on factors including model, context and reasoning. citeturn14search1turn14search0turn14search2turn4view1 Antigravity headless operation currently exposes `--effort low|medium|high`. citeturn13search4turn13search15 None of the three providers publishes a reliable formula such as “high effort costs 1.7× the subscription percentage.” Maestro must learn that locally.

A sensible bootstrap is therefore:

| Task | Initial route | Initial effort |
|---|---|---|
| Clear, localized, low-risk implementation | Codex Luna or Antigravity Flash | low/medium |
| Ordinary multi-file implementation | Codex Terra as calibration anchor; Claude Sonnet 4.6 as principal alternate | medium |
| Difficult debugging/diagnosis | Codex Sol, Claude Sonnet 4.6, or Antigravity Gemini Pro | high |
| Simple repository orientation/planning | Luna/Flash/Sonnet | low/medium |
| Cross-module or ambiguous planning | Terra/Sonnet/Gemini Pro | medium/high |
| Ordinary low-risk review | no separate paid reviewer unless policy requires one | n/a |
| Security/API/migration/high-risk review | strong independent route, preferably different from implementer | high |

This is intentionally a bootstrap rather than a benchmark-derived ranking. Luna is positioned by OpenAI as the high-volume/cost-sensitive tier, Terra as balanced and Sol for the hardest work; Sonnet 4.6 is Claude Pro’s current coding model; Antigravity gives an additional high-capability pool and model family. citeturn14search2turn14search0turn14search1turn24view4turn24view3 Actual Maestro outcomes should rapidly supersede those priors.

The full telemetry schema in the deliverable records backend/version, opaque account-pool ID, plan and verification date, concrete model, native effort, task class/difficulty, repository/change features, timestamps, before/after quota observations, reset epochs, proxies, verified outcome, rework, failure category, tokens/context separately, contamination/censoring and provenance. [The downloadable research file](sandbox:/mnt/data/GRAPH_ENGINEERING_TARGETED_RESEARCH.md) contains the complete schema and measurement procedures.

## Native durability versus external engine

Temporal is the most mature general orchestration substrate considered. Its server is MIT-licensed and self-hostable; production deployments require running the Temporal service and a supported persistence setup, with PostgreSQL usable for persistence/visibility. citeturn16search9turn18search8 Temporal gives Maestro durable workflow history, timers, signals, workflow rescheduling and mature deployment versioning; Worker Versioning reached GA in March 2026. citeturn18search6

But Temporal’s determinism boundary is precisely where Maestro’s hardest effects begin. Temporal requires deterministic Workflow code and puts API/database/other nondeterministic operations in Activities. citeturn18search4 Activities must heartbeat to receive cancellation, and an activity’s interaction with an external side effect still needs application-level idempotency/reconciliation. citeturn18search1 A Temporal server knowing that an Activity worker disappeared does not tell Maestro whether a detached Claude Code process is still editing `/worktrees/task-123`. Consequently the architecture’s process identity, fencing, worktree reconciliation and effect receipts survive a Temporal migration.

DBOS is the most interesting later comparison because it is lighter operationally. Its open-source library checkpoints workflows and steps into Postgres without requiring a separate orchestration server. citeturn16search1 DBOS can make database transactions exactly once when application mutation and the durability record commit atomically; ordinary workflow steps, however, retain at-least-once crash semantics. citeturn16search14turn16search16 DBOS also explicitly documents cases where recovery can temporarily produce a second “zombie” executor and detects conflicts at checkpoint/outcome boundaries. citeturn16search5 For Maestro, that means the Git/filesystem ownership problem remains.

Restate has particularly attractive primitives for durable human and quota waits. Durable promises and timers can suspend and later resume across restarts without keeping compute occupied. citeturn16search12 Its self-hosted server is simpler than a large Temporal deployment, and the current BSL 1.1 additional-use grant explicitly permits internal and ordinary production deployment by the licensee; however, the server is source-available rather than OSI-open-source. citeturn19search4 Multi-node operation also introduces snapshot/storage considerations Maestro does not currently need. citeturn19search6

The conclusion is therefore:

**Keep native-first unchanged. Keep the later E6 bake-off. Do not start one now.**

Should native durability later prove materially burdensome, **DBOS should be the first bake-off candidate** because it has the lowest architectural distance from a local Python application while still providing durable workflow recovery. Restate is the next candidate if durable waiting/communication becomes the main pain point. Temporal becomes much more compelling if Maestro actually acquires a multi-host/distributed-worker requirement or needs Temporal’s mature workflow deployment/versioning ecosystem.

The bake-off trigger should be demonstrated defects or complexity—not the mere fact that Maestro contains retries. This directly confirms the attached plan’s “truthful side-effect boundary” reasoning. fileciteturn0file0

## Repository intelligence provider

Tree-sitter now has enough evidence to stop being merely one undifferentiated candidate. Its core project describes itself as incremental enough to parse on every keystroke and robust enough to provide useful results despite syntax errors. citeturn20search4 That behavior is unusually well aligned with agent worktrees, where code routinely passes through temporarily invalid intermediate states.

Accordingly, the optional MVP should be:

```text
Exact Git/project facts
        ↓
bounded lexical queries
        ↓
Tree-sitter parsing/query packs
        ↓
definitions / declarations / imports /
syntactic references / exact ranges /
parse-error + completeness metadata
```

`tree-sitter-graph` should not become required simply because Maestro wants graph-shaped data. It is a general DSL for constructing arbitrary graphs from Tree-sitter parses, meaning language-specific semantic rules remain an implementation responsibility. citeturn20search0 The ordinary Tree-sitter parser/query API is enough for the MVP.

SCIP should be the first deeper provider benchmark. SCIP is a language-neutral code-intelligence protocol centered on symbols and occurrences used for precise definition/reference navigation, and the current ecosystem covers major languages including Go, TypeScript/JavaScript, C/C++, JVM languages, Rust, Python and Ruby. citeturn20search9turn20search11 It provides a materially stronger answer to “which exact symbol does this occurrence reference?” than syntax-only Tree-sitter logic, while staying much narrower than a full CPG. Its downside for Maestro is dirty-worktree integration: index generation is language/toolchain dependent, so per-worktree overlays and freshness still need deliberate handling.

Joern belongs at the opposite end. Its Code Property Graph combines syntax with control-flow and data-flow information, and its current documentation describes strongest support for C/C++, Java, JavaScript and Python, with additional JVM/Kotlin/binary front ends at varying maturity. citeturn21search0turn21search5 This is valuable for high-risk security/data-flow investigations, but excessive as the always-on navigation layer.

Aider RepoMap is valuable primarily as a **design concept**. Aider uses Tree-sitter definitions/references plus graph ranking to select the repository symbols most relevant to the current task and fit them into a bounded context map; importantly, the agent can still request the underlying files. citeturn23search1turn23search14 Maestro should borrow that ranked-context behavior in `ContextManifest` rather than depend on Aider itself.

RepoSkein, CodeWiki and SymDex are notable 2026 agent-oriented implementations. RepoSkein advertises a local Tree-sitter graph and MCP interface; CodeWiki advertises a local SQLite graph with 18 languages and incremental synchronization; SymDex supplies a local SQLite symbolic index with symbols, routes, callers/callees and context packs. citeturn22search3turn22search2turn22search0 These are credible benchmark candidates, particularly because they could save Maestro implementation effort, but most available performance claims are currently authored by the projects themselves. They should not become mandatory infrastructure before matched Maestro tasks show real exploration displacement.

Stack Graphs is no longer an attractive active candidate because GitHub archived the official project in September 2025. citeturn20search7

The provider benchmark should therefore compare:

```text
A: exact facts + lexical baseline
B: baseline + Tree-sitter manifest
C: Tree-sitter + SCIP on supported repositories
D: one agent-native provider (CodeWiki/SymDex/RepoSkein)
E: Joern only for data-flow/security task classes
```

The key outcome is not graph recall. Maestro should record verified task success, 5-hour and weekly allowance consumption, number and volume of fallback source reads, time to correct target symbol, full/incremental indexing CPU/RAM/time, stale resolutions, dirty-worktree correctness, cross-worktree contamination and escaped defects.

A useful explicit metric to add to E2 is:

```text
exploration_displacement
    = 1 - exploration_with_provider / exploration_baseline
```

and compute it separately for source-read calls, source bytes/tokens, and paid allowance. A provider that answers ten graph queries and then causes the agent to perform exactly the same grep/read sequence has not accomplished Maestro’s goal.

## Evidence quality and implementation rechecks

Official provider documentation/pricing was used for plan names, prices, model availability, documented quota structure and CLI controls. Official repositories and original project repositories/papers supplied technical details. Unofficial/community evidence was used deliberately where official documentation does not describe operational defects—most importantly Codex quota-display inconsistencies and Antigravity reset/lockout observations—and is labeled as observation rather than provider policy. citeturn23search2turn23search10turn9search17

The highest-confidence quota measurement is therefore Claude’s documented structured rate-limit feed. citeturn24view0 Codex has a high-confidence conceptual 5-hour/weekly allowance structure but only medium-confidence automated sampling until its installed CLI exposes a documented machine-readable status surface. citeturn23search0 Antigravity has a documented backend-refreshed quota panel but similarly needs a version-qualified parser unless a structured interface appears in the installed version. citeturn23search7

Model/catalog facts should be revalidated on **every relevant backend upgrade**, not only on a calendar schedule. That includes model IDs, effort values, context behavior, pool topology, quota output schema and reset semantics. This is especially important for Codex because the official pricing/model surfaces changed during the research window, and for Antigravity because its CLI was actively adding multi-agent functionality in September 2026. citeturn24view1turn13search0

There is also one authentication-boundary fact worth making architectural rather than leaving to implementation convention. Anthropic restricts repurposing Claude.ai subscription authentication through third-party applications, and Google similarly warns against using Antigravity login credentials through unrelated third-party software. citeturn7search15turn11search7 Maestro should therefore launch the **installed official CLI** under its supported user login and treat provider credentials as opaque. It should not extract OAuth/session credentials and turn them into its own API transport.

## Architecture delta

Only a small set of findings actually warrants edits to `MAESTRO_GRAPH_ENGINEERING_ARCHITECTURE.html`.

**Add a first-class `QuotaObservation` contract to P6.** It should include 5-hour and weekly observations independently, reset timestamp/epoch, used/remaining percentage or raw units, raw artifact reference, measurement source, contamination, censoring, confidence and provenance. Claude should immediately use its structured status-line rate-limit fields; Codex and Antigravity should use version-qualified status/quota collectors with calibrated proxies. citeturn24view0turn23search0turn23search7

**Make allowance-pool topology explicit.** An `account_pool_id` is insufficient if one backend/account has multiple independent pools. Antigravity in particular distinguishes a Gemini pool from non-Gemini limits. citeturn10view1

**Add the four explicit allowance-efficiency statistics to P6/E1:** 5-hour percentage points per verified task, weekly percentage points per verified task, verified tasks per 100% of a 5-hour allowance, and verified tasks per 100% of a weekly allowance. Failures/rework must contribute their consumption. The windows must never be summed.

**Add a credential-boundary invariant:** Maestro invokes official subscription CLIs but never reuses their authentication credentials as generic API credentials. citeturn7search15turn11search7

**Keep D02/native durability unchanged.** E6 remains deferred; DBOS becomes the first candidate to test if a demonstrated native recovery-complexity gap appears. No current engine removes Maestro’s worktree/process/effect reconciliation requirement. citeturn16search1turn16search5turn18search1

**Strengthen D07/P9:** Tree-sitter becomes the selected smallest optional syntax provider; SCIP becomes the principal precise-navigation benchmark; Joern remains on-demand. Remove Stack Graphs from the active shortlist. Keep search/read fallback because present independent evidence does not establish that graph retrieval can fully replace exploration at equal quality. citeturn20search4turn20search9turn20search7turn20academia47

The remaining architectural decisions are confirmed rather than changed: the attached plan was correct to separate session/context state from subscription allowance, bind every invocation to an explicit model and effort, preserve separate five-hour and weekly limits, keep deterministic acceptance independent of agent prose, reconcile opaque side effects explicitly, and require repository intelligence to prove end-to-end value before it becomes necessary infrastructure. fileciteturn0file0