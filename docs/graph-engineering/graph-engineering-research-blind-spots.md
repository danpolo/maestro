# Graph engineering research blind spots

Companion to [Maestro’s graph architecture and implementation plan](docs/graph-engineering/MAESTRO_GRAPH_ENGINEERING_ARCHITECTURE.html), 2026-09-07. This file records unresolved evidence, not additional implementation commitments. No new external research was conducted. The supplied reports’ numerical claims, product versions, and license assertions have not been independently verified.

The blind spots are grouped by the **next action required to resolve them**:

1. **Deep research needed now** — questions that require fresh external evidence before near-term architectural or implementation decisions.
2. **Actions / experiments in Maestro** — questions that public research cannot settle and should be answered through repository work, instrumentation, fixtures, fault injection, or real Maestro runs.
3. **Implementation-time external verification** — narrow facts that should be checked against the exact version/backend being integrated rather than researched broadly in advance.
4. **Deferred future work: free/open models** — intentionally documented but not part of the current Maestro graph-engineering implementation or research scope.

Some items ultimately require both outside research and a Maestro benchmark. They are listed under the step that should happen first.

---

## 1. Deep research needed now

| ID | Missing evidence and why it matters | Deep-research question / follow-up |
|---|---|---|
| B02 | **Subscription accounting and model/effort economics.** The packet frequently substitutes tokens-per-minute for subscription windows. Actual usage attribution, model weighting, shared account pools, concurrent human use, and opaque effort costs are unresolved. | Research what each relevant subscription backend actually exposes or documents about usage windows, model weighting, effort/reasoning settings, shared pools, reset behavior, and concurrency. Identify what is unknowable from public information. This can change routing scores, account admission, reserves, and paid-mode model choices. After the documented limits are understood, Maestro should compare concrete backend/model/effort tuples on matched tasks and keep session-context accounting separate from account-quota accounting. |
| B09 | **Native durability versus an external engine.** Reports contradict each other about LangGraph persistence, Temporal’s deterministic boundary, DBOS deployment requirements, and the operational cost of the alternatives. Neither blanket rejection nor automatic adoption is justified by the current packet. | Research the current, self-hostable, genuinely free durability options and reconcile the reports against primary documentation: recovery semantics, replay/idempotency model, deployment footprint, licensing, persistence requirements, worker/versioning behavior, and fit for Maestro’s external-process/worktree effects. This can change a later executor replacement decision. It is not a prerequisite for native implementation; any shortlisted engine should later be run against the same Maestro fault suite and compared on recovery, operator burden, and maintained integration code. |
| B10 | **Repository index provider quality and total cost.** Tree-sitter tags, SCIP, Stack Graphs, Joern, Aider-like ranking, RepoSkein, CodeWiki, and SymDex have not been compared rigorously, and several reported gains lack reproducible supporting material in the packet. | Research the current state of the candidate repository-index/code-graph approaches: exact supported languages/edges, incremental behavior, dirty-worktree handling, licensing, resource requirements, integration surface, and quality of independent/reproducible evidence. This can narrow the provider set, supported edge claims, and incremental strategy. Shortlisted approaches should then be benchmarked on Maestro’s actual repositories against bounded lexical/file/AST baselines, measuring indexing resources and downstream task success. Any SymDex experiment must use the operator’s contained launcher and documented resource/evidence procedure; direct indexing is prohibited. |

---

## 2. Actions / experiments in Maestro

| ID | Missing evidence and why it matters | Repository / operational action |
|---|---|---|
| B01 | **Representative Maestro outcomes.** The progress record reports a real cutover and switching exercises, but no fresh second-project installation or complete real LLM implementation since cutover. Synthetic success is insufficient evidence of engineering quality or generality. | Run ordinary bug, feature, documentation, manual-preparation, and recovery tasks on two materially different scratch projects; retain full outcomes and human interventions. This affects P0/P12 release gates and the priority of later optimizations. |
| B05 | **Project-policy construction accuracy.** `init` synthesis is largely a hypothesis in the packet. Detection coverage, monorepo conventions, missing tests, generated assets, service dependencies, and policy drift have not been evaluated. | Compare generated project facts and proposed checks with the actual CI/project process across representative repositories. Count missed requirements, false requirements, policy drift, and unnecessary human questions. Use the results to decide the template set, automatic fact refresh, when an adviser is useful, and activation rules. |
| B07 | **Filesystem enforcement.** Maestro’s confinement plan is prepared, not built. A visible worktree, a sandbox boolean, or bind mounts alone do not demonstrate isolation, especially with shared Git metadata, credentials, spawned children, and agent-owned configuration. | Implement/test an actual enforcement mechanism against read/write escape fixtures, child processes, shared Git metadata, credentials/configuration, and backend switching. Only execution profiles that pass may claim enforced permissions. Keep the standing network-access decision separate. Privileged setup, if necessary, must be packaged for Dan in one script. |
| B08 | **Process recovery fidelity.** No complete matrix establishes ownership of detached grandchildren, session termination, launcher acknowledgment loss, asynchronous jobs, or external effects for every backend. | Build a recovery/fault-injection matrix. Kill execution at each persisted dispatch/effect boundary and prove that a second writer never starts while the first may still write. Use the results to define cancellation protocol, reconciliation adapters, trusted-runtime eligibility, and uncertain-result policy. |
| B11 | **Validation dependency completeness.** An unchanged function signature cannot establish unchanged behavior. Dynamic imports, data files, service state, environment, generated code, fixtures, and test infrastructure can escape a code graph. | Use injected regressions and held-out changes against the complete required suite to measure affected-check recall. Default to invalidation whenever dependency completeness is unknown. Only measured evidence should justify affected-check skipping or cross-snapshot cache reuse. |
| B12 | **Calibration and trustworthy quality labels.** Model self-confidence, agreement, and passing self-authored tests are weak measures. The packet lacks task-stratified data linking those signals to escaped defects or operator acceptance. | Build local held-out acceptance tasks and seeded-defect fixtures. Measure which observable signals predict actual correctness/operator acceptance, with sample size and uncertainty. Use this to set routing quality floors, reviewer independence, candidate selection, and escalation thresholds rather than inventing confidence percentages. |
| B13 | **Concurrency and resource estimates.** There is no calibrated workload for durations, source overlap, memory/VRAM, human attention, or multiple projects sharing one subscription account. SQLite and host resource contention at higher concurrency are unmeasured. | Instrument a simple scheduler first and measure queue delay, conflicts, writer contention, memory/VRAM peaks, human waits, account contention, and useful accepted work. Use those results to decide whether host-wide account admission, richer scheduling, HEFT/CP-SAT, distributed workers, or a different storage/deployment topology are justified. |
| B14 | **External-action reconciliation.** Project adapters do not yet standardize deployment IDs, remote job identity, idempotency, cancellation, or postcondition probes. An acknowledgment may be lost after an action succeeds. | Add per-adapter action receipts, durable external IDs, postcondition probes, cancellation behavior, and failure/acknowledgment-loss fixtures. Determine which actions are safely retryable. Unresolved effects remain uncertain and cannot be blindly replayed. |
| B15 | **Retention, disk loss, and operational scale.** The packet does not establish transcript/artifact volumes, retention periods, backup restoration time, SQLite filesystem suitability, or disk-full behavior under long autonomous runs. | Measure real artifact/event/transcript growth and test online backup/restore, missing artifacts, full disk, schema upgrades, and restoration after host/disk loss. Define storage limits, snapshot cadence, retention defaults, and backup location before evidence is pruned. Same-disk durability is not protection against host/disk loss. |
| B16 | **Self-update and service ownership across projects.** A shared adopted-version pointer can affect future launches in other projects. Current `Type=simple` templates conflict with Dan’s current `Type=forking`/TMUX generation rule. The packet does not prove the replacement lifecycle or rollback semantics. | Test two projects with different active versions, interrupted migrations, HALT, service stop/restart, rollback, and TMUX ownership. Use the results to define project version pinning, upgrade quiescence, and generated service integration. Produce the required privileged install script only after the candidate workflow is concrete. |
| B18 | **Evaluation breadth and stopping limits.** There is no evidence that a small pilot proves non-inferiority, that a particular retry count is optimal, or that one quality score transfers across coding, debugging, planning, UI, and security work. | Build a stratified matched-task evaluation set across the major Maestro task classes. Retain raw paired results and uncertainty, and use operator-defined practical margins. Derive paid-mode stopping/retry defaults from observed results. A small fixture suite is a safety gate, not a statistical proof. |

---

## 3. Implementation-time external verification

These are important, but a broad deep-research pass is unlikely to age well. Verify them against the **exact backend, version, grammar, dependency, or service Maestro is about to enable**.

| ID | Missing evidence and why it matters | Verification action |
|---|---|---|
| B06 | **Actual backend capability and effort controls.** Current core specs lack effort fields. The packet does not establish exact current controls for all subscription agents or future runtimes; prompt replacement, native resume, cancellation, and model/effort controls differ. | When implementing or updating each backend adapter, verify the installed CLI/API contract and supported models/effort controls, then run the common adapter conformance suite. Never invent flags, assume prompt replacement semantics, or silently map effort labels across vendors. For the current implementation phase, prioritize the subscription backends actually in use. |
| B17 | **Free-use guarantees for optional dependencies.** The reports disagree on some licenses and include commercial telemetry, hosted products, and paid embedding examples. Language grammars, data fixtures, and optional components may differ from the parent project’s license. | Before enabling any optional dependency, verify the exact distributed version, component licenses, and normal-operation terms. Record the result with the dependency version. Paid hosted infrastructure and paid embeddings remain excluded. Model-weight and free-model-runtime verification is deferred with the future free-model track below. |

---

## 4. Deferred future work: free/open models

This work is **intentionally out of scope for the current graph-engineering implementation**.

Maestro’s architecture should remain extensible enough to support a future free/open-model mode, but no current phase should spend engineering or research effort selecting, benchmarking, serving, or optimizing those models.

These blind spots should remain documented until that work becomes a deliberate project.

| ID | Future blind spot | Future work |
|---|---|---|
| B03 | **Free-model runtime and model selection.** The packet does not compare genuinely free serving runtimes, hardware requirements, model licenses, context behavior, structured outputs, tool use, or node-specific performance. “Open source” alone does not establish a zero-charge hosted service. | In a future dedicated research pass, compare then-current genuinely free/open-source models and runtimes for Maestro: serving options, hardware requirements, licensing/terms, context, structured/tool use, coding/research/review strengths, and integration interfaces. Then shortlist models by agent-node role and run Maestro’s common conformance and quality fixtures. No paid API should be used as a fallback for this mode. |
| B04 | **Value of extra inference in free mode.** The reports do not establish how much extra inference best compensates for weaker zero-marginal-cost models: multiple candidates, independent reviewers, critique rounds, deeper reasoning, retries, or larger execution graphs. | After a free-model backend exists, benchmark progressively richer free-mode workflows against identical acceptance suites and task snapshots. Optimize final correctness and quality rather than token usage, and derive stopping rules from quality plateaus rather than arbitrary token/retry limits. |

### Deferred-mode architecture rule

Current implementation may define **interfaces and abstractions that make future free-model support possible**, but should not build free-model-specific orchestration yet.

In particular:

- do not select or deploy an open model now;
- do not build a local model-serving stack now;
- do not benchmark free-model agent topologies now;
- do not spend current implementation effort on free-mode stopping policies;
- do not let hypothetical future free models complicate the current paid-agent execution path unnecessarily.

The immediate architecture should focus on paid subscription agents while keeping backend, model, effort, and execution-policy interfaces sufficiently generic that the free-model mode can be added later without redesigning the core.

---

The main architecture plan should remain usable while these questions are open.

- **Section 1** feeds targeted Deep Research needed for current decisions.
- **Section 2** becomes near-term Maestro experiments, fixtures, or implementation-validation work.
- **Section 3** becomes adoption/conformance gates when relevant components are actually integrated.
- **Section 4** is a future backlog and should not generate current implementation work.

Experiments E0–E6 in the main plan resolve only the questions needed for the current paid-agent architecture; they do not authorize work on the deferred free/open-model track.