# Maestro + Grill — Complete Goal/Final-State/Outside-In TDD Behavioral Contract

**Version:** User-approved target behavior, 2026-10-17. **Applies to the complete development plan, not just W11a.**

This is a behavioral contract, not a prescribed code architecture. Implementation must fit the live code, safeguards, workstream plan, and accepted decisions. All requirements in this file are mandatory; only timing and engineering detail are delegated. See `02-REQUIREMENT-TRACEABILITY-AND-VERIFICATION.md` for the detailed checklist. See `03-ORIGINAL-W11A-BASELINE.md` for pre-audit behavior, including what was already built vs later. Existing decisions are retained unless this document explicitly supersedes them.

## 1. Product model and invariants

### 1.1 Purpose and conceptual model

The goal is to prevent autonomous execution from successfully completing a task list yet delivering an incorrect, incomplete, or misaligned end product. The system must align initial product intentions, design, implementation, verification, future planning, and measured improvement. A plan is a hypothesis for satisfying an approved **product completion contract**, not the definition of completion.

Canonical flow:

1. **Grill first:** elicit purpose/users/goals/final product before detailed design; draft and approve outcome/verification/constraints/decision authority.
2. **Acceptance design:** derive concrete user-visible acceptance scenarios, including broad behavioral test specifications for all automatable outcomes; explicitly classify operator-only verification.
3. **Setup:** validate/read GOALS and PLAN; materialize runnable outer acceptance tests in the actual repository; establish an honest initial baseline (expected RED for missing functionality, GREEN for existing behavior, blocked/unrunnable for setup errors); plan ROADMAP work tracing every goal/scenario/test to eventual closure.
4. **Execution:** role-aware briefs, D23 scope, local inner-loop TDD, ongoing evidence and progress artifacts.
5. **Review:** accept task correctness separately from goal effects; classify direct/enabling/maintenance impact using appropriate evidence; protect global invariants.
6. **Adaptive planning:** checkpoint at meaningful milestones, recognize failed strategies early, update authorized plans or request user decisions for substantive changes and goal conflicts.
7. **Finish:** W7 aggregated polish; W11b authoritative repeated final verification of all mandatory outcomes including end-to-end journeys/operator checks. Empty queue != done.
8. **Improve:** W12 learns from reliable fault telemetry and measures whether suggestions actually help.

### 1.2 Standing principles (preserve)

- **D23 no expansion:** Seeing a goal never independently authorizes an implementer/reviewer/diagnoser to perform new feature work beyond the assigned task. It provides why/context. Additional work is added by proper planner/ROADMAP authority within the approved contract, or escalated when material.
- **Reviewer observes; deterministic rules decide.** No self-issued review verdict or untrusted classification may secretly override policy.
- **Accepted task ≠ achieved goal; completed tasks ≠ completed project.** A correctly implemented task is not reworked solely due to an incorrectly defined task/plan.
- **Telemetry never silently changes verdicts.** It informs later planning/improvement rather than retroactively rewriting acceptance.
- **Small authorized gaps handled autonomously, larger out-of-authority decisions escalated.** Avoid question spam; resume safe independent work.
- **Stable, never-reused goal IDs** throughout revisions. Referenced documents can be authored via canonical Drive grill skill or ChatGPT paste-ready version; contract format is the interoperability interface, not the skill implementation.
- **Cost and context discipline:** concise role-specific briefs; deterministic validations; no extra model role/gate without justifiable benefit; incremental tests; preserve ongoing parallel work.

## 2. Grill and `docs/GOALS.md`: user-intent-to-verifiable-contract

### 2.1 Conversation sequence and usability

- Elicit purpose and who uses the product; establish goals **before detailed design/architecture questions**. Brief purpose/user questions may precede final goal drafting as necessary.
- Guide the user through *decisions they must make*, not implementation details an agent can choose. Avoid excessive grilling or mechanically insisting on a fixed number of answers.
- Preserve the existing six named areas while strengthening them: Purpose (normally <=2 sentences), Users/use cases, Goals, Final Product, Non-goals, Priority. Integrate additional fields/sections or structured subfields as needed for constraints, verification, autonomy boundaries, iteration policy, blockers, change control, and acceptance scenarios. `PROJECT.md` points to `GOALS.md`.
- Use 3–7 as a preferred **top-level goal** range, never a rigid validity limit. Complex top-level goals may have independently verifiable acceptance criteria; avoid both fragmented micro-goals and untestable omnibus goals.
- For every goal G*: human-understandable outcome, stable ID, context/rationale, baseline if measurable and applicable, success threshold/target, validation method, scope/priority, constraints/invariants, dependencies, and user/operator observation when needed. Do not invent metrics or thresholds; explicitly escalate a truly undefined acceptance choice.
- Explicit final-product walkthrough expressed as what the user sees/does when the product works, not just a feature list. Derive one or more concrete user journeys/scenarios from it (normal flow, meaningful error/failure flow, and boundary flows where material). Goals and scenarios must link bidirectionally to prevent orphaned promises.
- Explicit **non-goals**, out-of-scope work, critical properties that must not regress, and system/resource/security constraints. Separate **hard invariants** from **negotiable, explicitly approvable trade-offs**.
- Priorities express desired order when optimizing goals, but do **not** pre-authorize weakening any approved goal. All real conflicts require a user decision, with a recommended option; see §8.
- Define what may be autonomously decided; iteration/retry policy; when to stop a failed approach; what counts as a true blocker; escalation medium, options, recommended course.
- User approves the *intended outcome/contract* prior to detailed design; don't require their approval of ordinary code implementation. Capture decisions, timestamps/revisions, and a concise digest of what will be considered "done".

### 2.2 Goal and scenario traceability

- Goals are stable `G1..Gn` or compatible schema; IDs immutable, never recycled after retirement. Scenario and acceptance-criterion IDs stable too (`S...`, `AC...` or compatible); reference the goals they test.
- Have a single authoritative version/revision of the approved product contract, with change history: proposed change, rationale, impact, user approval, superseded interpretation, effective revision. Never silently rewrite goals, thresholds, or non-goals through planning or implementation.
- Approved changes trigger targeted revalidation of mapped scenarios/tests, evidence, planned tasks, dependencies and completed conclusions. Retired/changed criteria remain auditable instead of losing history.
- Keep format versioning, structural validation, helpful migration from existing `GOALS.md`, and compatibility with both grill paths. Setup may generate a missing contract by asking the minimum necessary goal questions; do not invent user decisions.

### 2.3 Example semantics (illustrative, not a schema prescription)

```yaml
contract_revision: 4
purpose: "..."
users: ["..."]
goals:
  - id: G1
    outcome: "..."
    baseline: "... (or not_applicable)"
    target: "..."
    verification: ["AC-01", "operator: OP-01"]
    constraints: ["INV-01"]
final_product_scenarios:
  - id: S-01
    goals: [G1]
    given: "preconditions"
    when: "user action"
    then: "observable result"
    automated_criteria: [AC-01]
    operator_checks: [OP-01]
non_goals: ["..."]
hard_invariants: ["INV-01"]
authority_policy: "..."
```

Markdown remains an acceptable source format; don't force YAML or any specific encoding absent repository reason. The example is about semantics, not executable syntax.

## 3. Outside-in project TDD: goal/final-product tests from day zero

This is a **mandatory extra feature**, on top of all audit fixes. Two TDD loops coexist:

- **Outer/product-level TDD:** approval-grade high-level acceptance tests/specifications derived during grill and executed as early as setup. They define the final product's externally visible behavior, independent of architecture.
- **Inner/task-level TDD:** implementer follows existing red/green/refactor for units, integrations and narrow tasks, progressing toward outer acceptance test closure.

### 3.1 Grill responsibilities

- For **every** goal and final-state scenario, ask whether objective behavior can be expressed as a runnable test; generate at least one test specification for every automatable criterion/scenario. One test may cover several goals/scenarios, and one goal may require several tests; preserve the explicit many-to-many mapping.
- Express in behavior terms: inputs/preconditions, user action, observable outputs, error/boundary behavior, success oracle, necessary fixtures/environment, performance measurement/threshold, data provenance, responsible test layer, and evidence type. Use Given/When/Then or equivalent when it improves clarity.
- Avoid test pollution by architectural assumptions: no forcing internal files, algorithms, database, routes, or mocks unless explicitly part of the user-approved contract. Broad tests should tolerate valid alternative implementations.
- For non-automatable criteria, create an **explicit operator acceptance check**: instructions, expected observation, success/failure recording, and user sign-off when necessary. Never generate a vacuous/pass-through/fake test to claim coverage.
- For testable criteria whose oracle cannot be unambiguously derived, mark a genuine **unresolved user choice** and ask only the necessary decision. No fabricated thresholds.
- Approve contract and its acceptance scenarios together; goals dictate tests, not vice versa.

### 3.2 Setup responsibilities

- Read/validate the approved contract and approved plan, work in the target project repository/environment; generate **real executable** high-level behavioral tests with stable IDs where feasible, or bind acceptance criteria to existing tests when they already validate the requirement. Use the repo's actual test framework, avoid redundant test systems.
- Establish the **baseline by running** generated/identified acceptance tests. Result categories MUST distinguish:
  - `red_expected_missing_behavior`: harness works, assertion fails because product isn't implemented yet.
  - `green_already_satisfied`: behavior already works; keep regression protection.
  - `blocked_environment` / `unrunnable`: dependencies, credentials, test env, fixture, non-determinism or harness failures; NOT acceptable as credible RED.
  - `operator_only` / `cannot_automate`: explicit human verification, no fictional executable check.
  - `unknown`: insufficient evidence; require more targeted work rather than claiming RED/GREEN.
- Where a full product isn't runnable yet, prioritize building the **minimal safe test harness/fixtures/adapters** as an enabling task, then prove the intended behavior fails rather than due to a missing import, nonexistent command, crashed runner, unprovisioned service, or wrong configuration. This need not prohibit test stubs that intentionally fail an approved semantic assertion when honest and identifiable; don't manufacture false RED for optics.
- Existing functionality may be GREEN from day one; do not artificially force it RED. Where a criterion cannot yet be made runnable, preserve the spec and an explicitly tracked setup/enabling task; don't silently mark it passing.
- Store baseline results, test commands, environment, revision, observed failure reasons and mapping to goal/scenario/criterion. Distinguish goal correctness from harness correctness.
- Enforce repository safety: safe test data, isolation, deterministic fixtures, stable oracles, bounded runtime; contract tests avoid leaking secrets, external paid calls, unnecessary cloud resources, live production mutations. For nondeterministic/model-centric quality, use reproducible sample sets with defined grading/threshold, or operator verification where sound automated judgment is unavailable.
- Agent-generated acceptance tests must be independently scrutinized against the **approved product contract**; the implementing agent cannot redefine the oracle to match the implementation.

### 3.3 Execution as RED-to-GREEN navigation

- Every unsatisfied or blocked acceptance criterion with an approved final-state obligation must have an accountable path in ROADMAP (task or justified existing dependency), projected evidence, and completion gate ownership.
- Planner decomposes missing behaviors into minimal coherent tasks and dependencies, tracking `G...`, `S...`, `AC...`/`OP...`, and ideally test location/command. Progress can be **measured by resolved acceptance criteria**, but raw green test count is not the goal; prevent metric gaming and superficial mock-success.
- Task agent creates the smallest legitimate implementation that satisfies task acceptance criteria; inner TDD continues. Re-run impacted product tests when beneficial, not the entire expensive suite at every micro-step. Milestones and final gate run broader checks.
- A product-level RED from an unmet *future* requirement is **expected**; it must not block every intermediate task's merge. A previously GREEN behavioral test turning RED is a **potential regression** and must be investigated/protected under §7–8.
- A passing test is valid only if it exercises the relevant real observable behavior with a trustworthy oracle and correct environment; flag skipped/xfail/mock-only/disabled/edited tests distinctly. A test can be updated only via legitimate independently reviewed correction, or user-approved change if it alters the acceptance contract.
- Never weaken, delete, change thresholds, bypass, indiscriminately skip, or adjust acceptance tests simply to make the implementation look finished. Preserve scope/revision; record reviewed test fixes. Anti-Goodhart: don't optimize green count at expense of user outcomes.
- Final operator checks and cross-scenario E2E remain necessary; automated test success isn't by itself universal proof of user experience.

### 3.4 Special-case outcomes

- Benchmark/performance criteria need environment-stable baseline comparisons and explicit thresholds. Avoid pretending a code diff or synthetic benchmark alone proves production improvement.
- Multi-service flows need integration/E2E coverage or honest, scoped alternatives; isolated unit tests alone aren't final scenario proof.
- Privacy/security/integrity invariants need independent negative tests and should be checked even on housekeeping tasks.
- Projects with no testable final-state scenarios still generate operator-check contracts and an explicit explanation—never pretend every goal is automatable.

## 4. Setup, PLAN, ROADMAP and task contribution

- Preserve plan phase `serves` attribution and setup's ask-for-only-missing behavior; phase-to-task inheritance is a **suggestion**, not proof that every task directly advances the goal. Individual ROADMAP tasks must express accurate goal/scenario associations.
- Every task includes `serves: [G...]` where relevant and contribution type: `direct` (measurable product advancement), `enabling` (verifiable prerequisite), or `maintenance` (preserves reliability/quality/operations without direct advancement). If multiple served goals have different contribution modes, represent mapping per goal or equivalent; avoid a misleading single flag.
- `serves: []` explicitly means no expected goal advancement; it does **not** opt out of invariant/regression checks. A missing `serves` is unclassified, NOT necessarily a deliberate none; take appropriate action at validation boundary.
- Each task declares intended outcome, acceptance criteria, dependencies, evidence and expected affected test/goal associations. An enabling task can be successful without moving a final product metric (e.g. build benchmark harness, locate bottleneck, establish data fixtures). Do NOT label legitimate enablement as planner failure.
- Plan feasibility gate before autonomous execution verifies: every required goal and approved scenario has a credible path from RED/unknown to verified GREEN/operator evidence; dependencies and critical journeys are covered; invariants and non-goals held; every required test can actually produce evidence in time; risks/uncertainties explicit. A plan can be approved with tracked honest unknowns/enabling tasks, but not silent uncovered requirements.
- Validate references and contract integrity: draft-time warnings with actionable details; when beginning execution and at affected task boundaries, reject malformed required contract, unknown IDs, missing required links/untraceable obligations, illegal revisions, and other conditions that would make verification unsound. Fail **the affected work/gate** rather than necessarily stopping unrelated safe work. Do not silently proceed based on bad metadata. Preserve clear diagnostics and migration for existing projects.
- ROADMAP should prioritize meaningful milestones/test gaps (and available critical path), not generate gratuitous follow-ups for each non-blocking polish item.
- `maestro doctor` may warn at drafting; it must provide actionable blocking diagnostics for material execution-invalid state. Do not create false blockers for legitimately `serves: []` maintenance or operator-only criteria.

## 5. Efficient, durable context for long-running agents

- Planner, implementer, reviewer, diagnoser, triage (when available), and other relevant execution roles receive a **brief, bounded goals excerpt**: purpose, served goals in adequate detail, other goals in concise summaries, priorities, non-goals, hard invariants and trade-off boundaries. State which lower-priority text is omitted; never silently truncate critical constraints or scenario acceptance criteria.
- Supplement task brief with precise task acceptance criteria, applicable product acceptance tests/commands, relevant baseline/milestone evidence, unresolved blockers and current approved goal-contract revision. Include references/paths to authoritative full docs rather than pasting all prose into every prompt.
- Role-specific instructions: implementer knows expected task and cannot expand scope; reviewer knows expected contribution/evidence but separately judges actual product outcomes; planner sees all remaining gaps; diagnoser sees failures and policy; triage sees aggregate polish and non-goal constraints.
- Durable machine-/human-readable state across compactions, handoffs and session changes: last known revision, which tests/scenarios already passed/failed/unrunnable, what was actually verified, outstanding requirements, decisions/approvals, next safe work, environment reproduction. Handoff cannot merely repeat agent narrative or rely on unpersisted memory.
- Use bounded excerpts, retrieval-on-demand, deterministic scripts, targeted tests; avoid enormous repeated prompts and expensive review/replan loops. Preserve graph/legacy execution paths if still supported.

## 6. Verification model: task acceptance is distinct from goal impact

### 6.1 Independent questions

1. **Task correctness:** did this task meet its own explicitly specified acceptance criteria, tests, review/security/invariant checks? Deterministic policy makes accepted/rework decisions.
2. **Goal impact:** what does the evidence actually establish about the goal(s), scenarios, and remaining work? This is an observation recorded without automatic feature creep/rework for a valid task.

### 6.2 Required goal impact classes

- `advanced`: verified direct positive movement of an approved criterion/metric/scenario.
- `enabled`: verified prerequisite that plausibly unblocks future goal movement; doesn't pretend the final outcome is already improved.
- `unchanged_expected`: no direct progress was promised, e.g. maintenance/polish/housekeeping. This is not a planning defect.
- `no_progress_unexpected`: direct improvement was promised but not observed under sufficient relevant evidence. Task may still be correct and merged; planner gets a diagnostic signal.
- `regressed`: verified material degradation against an approved mandatory goal or invariant, including **goals not listed in `serves`**. Must evaluate applicability/tolerances and conflict policy.
- `unknown`: evidence incomplete/invalid/inapplicable; gather targeted evidence when needed; do NOT force a fictitious positive/negative classification.

Keep backward compatibility/migrations for existing `advanced/no_progress/regressed` records without pretending historical `no_progress` reveals whether work was enabling or unexpected. Preserve historical telemetry provenance.

### 6.3 Evidence standards and review rules

- A line from the diff demonstrates an edit, NOT necessarily an outcome. Evidence can be failing/passing test IDs with results, baseline-delta measurements, logs, UI/E2E traces, reproducible experiments, or human observations. Include links/paths, environment/conditions, certainty, and goal/scenario/test mapping. Avoid unnecessary heavyweight runs when narrower evidence suffices.
- Reviewer records actual observations for served goals and known global/affected invariants, and signals regression evidence even outside `serves`. `serves: []` skips **expected advancement**, never global regression protection.
- Unknown results are not automatically failures or successes. Targeted validation may be an enabling task or completion blocker if evidence is indispensable.
- If a task fulfills its own acceptance criteria but unexpectedly doesn't advance a goal: **accept/merge the correct task** unless separate invalidity/regression exists; record finding, assess/replan at useful boundaries, do not automatically send implementer into endless rework.
- A verified unacceptable regression blocks appropriate acceptance/merge. If resolving the regression conflicts with progress on another approved goal, route conflict to user (see §8), do not silently choose. A confirmed hard invariant violation blocks even if the user was not trying to optimize it; approvals for exceptions must be explicit and within lawful/security boundaries.
- Distinguish *globally observable negative effect* from undetected unknown effect; design reasonable regression suites/test selection. No claim that every possible goal regression can be mechanically checked on every task.

## 7. Milestone checkpoints, plan adaptation and Q1 resolution

- Schedule efficient **goal/acceptance progress checkpoints at meaningful milestones** (completed phase, enabling dependency unblocked, risky integration, strategic pivot, convergence checkpoint), NOT full suite after each small commit. Check remaining RED/blocked/unknown criteria and evidence-based expected-to-actual progress.
- Detect infeasible approaches before final gate; replanning should occur when evidence says the remaining route cannot achieve the contract, even if only one high-confidence result establishes it.
- **Modified original Q1 choice (B):** retain the useful repeated unexpected-no-progress signal, but a *fixed two-task counter is not the decision algorithm*. Trigger consideration on (a) repeated meaningful unexpected failures (two can be the default alert threshold), (b) one decisive feasibility contradiction, or (c) milestone gap indicating material failure to converge. Legitimate `enabled`/`unchanged_expected` statuses do not trigger. Track per-goal evidence and episodes; don't repeatedly ask an already settled question unless material new evidence.
- Planner analyzes root cause, remaining tasks, dependencies, time/resources, alternate approaches, expected acceptance-test impact and decision authority; generate a **concrete replanning proposal**, not simply "the plan isn't landing". Avoid premature blame.
- Within scope, non-conflicting routine plan adjustments may be autonomous per approved authority (including adding scoped tasks through planning), preserving D23 for individual implementers. Hold dependent or conflicting work when needed; independent work continues. Changes that materially alter goals, final product, verification standards, scope, resource/cost limits, security, or competing goal outcomes require your approval.
- The user's explicit approval/decline/approval-with-notes is persisted and propagated; keep-going can reset/snooze signal with rationale, without suppressing genuinely new failures.

## 8. Goal-vs-goal conflicts and controlled trade-offs — USER DECISION REQUIRED

**Hard new user requirement:** When two approved goals conflict, or progress on one requires regression in another, it **must** be raised to the user for resolution with a recommendation. Neither ranking priorities, implicit tolerances nor local agent judgment may autonomously sacrifice the other approved goal. Apply during grilling, setup/plan, execution, regression review, replanning, and final gate.

### 8.1 Detection and scoping

- Identify real plausible material conflicts **proactively** from goals, criteria, constraints, budgets and acceptance tests, and **reactively** from evidence. Avoid spamming conjectural theoretical conflicts; ask when a credible conflict affects a proposed decision or work, and document uncertainty.
- Distinguish (a) incompatible goals by definition, (b) resource/time trade-off, (c) implementation-induced degradation, (d) outdated/contradictory acceptance thresholds, (e) hard invariant violation. Hard invariants remain blocked, not casually balanced as soft goals.
- Priorities are inputs for the recommended option, never delegated decision authority. Do not assume "higher priority wins" means automatic approval.

### 8.2 Escalation format

When a user choice is necessary, send a concise Telegram decision request (or currently approved user-interaction channel) with:

1. Exact conflicting goal/criterion IDs, current approved contract revision and decision deadline/impact if any.
2. Evidence or uncertainty, including measured trade-off/estimates (performance, quality, resources) when available.
3. Consequences of preserving Gx, preserving Gy, or a bounded compromise/alternative, including blocked tasks.
4. A recommended option **with rationale**, grounded in goal priorities, constraints, objectives, reversibility and evidence.
5. Clear **approve / decline / approve-with-notes / choose option** semantics and whether changes affect acceptance tests/contract.

Wait for the user's decision for conflicting work, leaving safe independent tasks active. If the conflict is unresolvable under authorized choices, mark blocked rather than invent a compromise.

### 8.3 Persist resolution and prevent re-asking

- Record user's decision, allowed degradation/threshold changes (if any), rationale, scope, expiry/conditions, and approvals in durable decisions + revised GOALS/acceptance tests/planning as applicable.
- Recheck and re-request only if new evidence materially exceeds the explicitly approved trade-off, a new conflict appears, or contract changes invalidate the prior decision.
- Reject an automated change to acceptance criteria that would disguise an unapproved regression.

## 9. W7 polish, W11b authoritative finish, completion lifecycle

- Preserve W7: accumulate non-blocking review findings in a backlog; triage deduplicates, closes moot items with reasons, groups remaining polish by affected area; no spontaneous new scope and no infinite microfollowup tasks.
- W7 work uses ordinary acceptance and global invariant protections. Product tests relevant to changed areas are rerun. A `serves: []` polish task cannot silently regress an unrelated goal.
- After planned work and polish, W11b runs authoritative goal validation including approved product acceptance tests, relevant independent regression/evidence checks, actual end-to-end user journeys, threshold comparisons, and explicit operator checks routed to user when required. Commands run in controlled environment, records include revision, test status and evidence. "Every task merged" is insufficient.
- Final state taxonomy: `complete` (all **mandatory** approved criteria verified, no unresolved blockers, approved operator checks done), `incomplete` (work remains), `blocked` (can't make progress without a real external/user decision). Optional quality backlog may remain if explicitly nonblocking and within approved completion semantics.
- Unmet goal **blocks done** and yields scoped ranked proposed tasks, prioritized by unmet outcomes, with cause/evidence. New work executes then **repeats affected regression/product verification and necessary W7 polish**. No unbounded infinite loop: respect iteration caps, detect repeated failure, report blocker with alternatives/recommendation; never downgrade unmet goal to pass.
- If contract changes late, invalidated old evidence/tests are revalidated. Nothing becomes done on the basis of test skips, environment-failed tests, stale revisions, unapproved trade-offs, or a narrative assertion of completion.
- At project completion provide a succinct **product outcome report** mapping goals→scenarios→tests/operator checks→evidence, limitations, accepted trade-offs and nonblocking deferred items.

## 10. W12: improvement from reliable evidence, not naive attribution

- Preserve original W12 analyst concept (strong appropriate model, weekly and project-end), subject to resource/cost rules. Read fault ledger and per-route statistics; propose ranked, evidence-backed fixes to planner rules, prompts, model/effort, graph order, gates, human check steps, or skills/context.
- **Separate observation from blame:** `no_progress_unexpected` is not automatically `fault: planner`. Attribution comes from deterministic cause-to-fault **rules only when evidence establishes the cause**; allow `unknown/unattributed` pending additional evidence. Distinguish faulty goal mapping, invalid measurements/harness, review misclassification, dependencies, unavoidable constraints, plan definition, task scope, and agent failure.
- Keep pre-existing telemetry schema/migrations, clarify `enabled` vs lack of final progress; record false positives/false holds, time-to-replan, time-to-verified-outcome, accepted vs rejected proposals, regressions, model usage/tokens, test cost, and outcome quality where measurable. Do not reward raw green count or blame frequency.
- Each improvement proposal includes observed failure pattern and evidence, expected benefit/saving, implementation/risk/cost, how to evaluate effects (baseline and follow-up), rollback path, and why it is preferable to alternatives.
- User approves or declines each proposal via Telegram. Do not re-send declined changes without new evidence. Next digest measures observed effect rather than merely reporting application; avoid feedback loops that degrade result quality for lower AI usage.
- **The ledger never changes verdicts itself.** It drives reviewable changes to system policy and plan, respecting current approval rules and coordination with concurrent development.

## 11. Human decisions and authority boundaries

- Agents decide ordinary implementation details, retries, test selection, local debugging, and within-scope fixes. Maestro/planner may make non-conflicting routine plan adjustments within approved objectives and authorized limits.
- Explicit user approval required for: new/changed approved goals, new acceptance thresholds/final-state definitions, **any real goal-to-goal conflict or one-goal-progress/other-goal-regression trade-off**, material expansion of scope, relaxation of hard constraints, privilege/data-security exceptions, paid operations, destructive or high-impact actions, and true unresolved product questions.
- Agent/role encountering an unspecified case applies safe documented fallback for small gaps; for material unknowns ask user with options+recommendation. Do not block on implementation details that should be autonomous. Ask once with context; preserve answer and don't re-ask absent new evidence.
- Paused decisions should only suspend work that depends on them. Asynchronous sessions can continue safe independent work, and all state/decisions survive handoffs.
- Respect existing Telegram interface and permissions; do not force a new notification integration if already present.

## 12. Efficiency, compatibility, implementation and acceptance discipline

- Preserve already passing W11a goals parser/check, ROADMAP goal mapping, graph/legacy role excerpts and review machinery by **refinement and migration**, not destructive replacement. Verify current repository before change; source document status is a snapshot only.
- Make contract and test generation available both from canonical Drive `ChatGPT/grill-me-framework` and ChatGPT paste-ready version, and `maestro-setup` fallback; parity of required fields and defaults is mandatory.
- W11a implementation changes now; W7/W11b/W12 remain explicitly staged in existing plan but **all** approved features, including optimizations, must appear as implementable tasks, have owners/dependencies/tests, and ultimately be completed. Do not quietly scope out P2 items.
- Migration/backwards compatibility: old `GOALS.md` structure, old ROADMAP `serves`, old `advanced/no_progress/regressed`, older decisions/fault ledger, missing operator checks in legacy projects, multi-version projects, externally edited documents, contract changes mid-run. Fail safely with actionable errors; avoid inventing past statuses.
- Provide reproducible tests for parsing, plan consistency, scenario mapping, initial RED/GREEN/blocked handling, criterion integrity, cross-goal conflicts, review statuses/decision rules, global regression even `serves: []`, Q1 trigger, approvals, final goal gate, W12 attribution, durable handoff, efficiency and manual operator verification.
- Verify end-to-end with a small fixture project: grill contract→test specs→setup runs acceptance tests RED→ROADMAP gap decomposition→inner TDD implementation→GREEN→conflict escalation simulation→end gate. Include a project with some goals already GREEN, one operator-only criterion, an enabling task, one intentional defect in test harness, an invariant regression on maintenance, and a revised goal contract; exercise non-happy paths.
- Ensure unattended runs preserve ability to stop/hold, recover, and report remaining failures without misreporting `done`.

## 13. Existing workstream and behavior reconciliation (normative)

These statuses come from the ORIGINAL source dated 2026-10-17, not from a live repo inspection; inspect and amend plan accordingly.

- **W11a built/tested according to source:** GOALS.md parsing and `maestro goals check`; `serves` mapping and doctor warnings; short goals excerpt in graph and legacy briefs; original review rules. **Revise**, don't erase, to implement the new semantics.
- **W11a in progress:** canonical grill-me framework skill, Maestro setup skill, and portable ChatGPT paste-ready version. Integrate all revised contract/test specification/authority decisions here.
- **Q1 open in original:** now decisively **resolved** to modified B: evidence-based proposed replanning; only user-reserved decisions escalate; independent work continues.
- **W7 originally later:** backlog triage/polish remains, with invariant protection and loop-integrated completion.
- **W11b originally later:** final goal verification, now expanded to acceptance-test-runner, operator evidence, iteration/reverification, typed completion state.
- **W12 originally later:** feedback and optimization, now strengthened attribution, calibration and measured improvements.

## 14. Source material lineage and precedence

1. User approval in conversation: "I agree with everything and I want everything you mentioned in my system"; "every single thing ... don't skip anything critical or extra optimization".
2. User extension: goal/final product initially failing high-level tests in grill/setup leading autonomous gap filling, plus mandatory user resolution of goal conflicts and recommendation.
3. Full earlier audit covered: six-part/extended goal contract; executable final product; enabling-vs-direct progress; diff insufficient; strict validations; global regression on maintenance; early milestone checks; smarter Q1; nuanced fault ledger; goal revisions; decision authority; completion loop; role-specific durable context; continuous measurable improvements; priority plan P0/P1/P2.
4. `03-ORIGINAL-W11A-BASELINE.md` includes the original D23, six sections, W7/W11b/W12 and original in-progress status; do not lose unaffected features.

When interpreting an ambiguity, prioritize achieving the desired final-state outcome with minimal intrusive ceremony. Do not weaken explicit user-reserved choices or approve a conflicting trade-off on the user's behalf.
