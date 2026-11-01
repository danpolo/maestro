# Maestro + Grill — Exhaustive Requirements / Verification Register

**Approved target; all rows REQUIRED.** The `P0/P1/P2` labels are **sequencing only**: do not skip P1/P2 as "noncritical" or "optimization". Where a feature exists, mark `existing-but-modify` or `verified-existing`, with proof, rather than rewriting. Update this matrix in the development plan or an equivalent tracked repo file.

Cross-reference: `01-FULL-BEHAVIOR-CONTRACT.md` is the normative detailed behavior. `03-ORIGINAL-W11A-BASELINE.md` is the original status snapshot; the agent must inspect the live code and not mistake it for current implementation truth.

## A. Requirement inventory (must account for EVERY row)

| ID | Priority | Domain / feature | Minimal acceptance evidence |
|---|---|---|---|
| R01 | P0 | Grill elicits purpose/users before detailed design; goals-first branch | Skill flow example and tests for missing goals |
| R02 | P0 | Six original `GOALS.md` sections maintained/extended | Parse/render round trip and backward compatibility |
| R03 | P0 | 3–7 goals guidance, NOT rigid cap; complex criteria supported | Grills with 2, 5 and 9 legitimate goals |
| R04 | P0 | Stable never-reused goal IDs and traceable criterion/scenario IDs | Rename/retire/duplicate tests |
| R05 | P0 | Outcomes/baselines/targets/thresholds/verification, no invented metrics | Example contracts; negative validation tests |
| R06 | P0 | Concrete final-product walkthrough and user journey acceptance scenarios | Scenario map plus end-to-end sample |
| R07 | P0 | Hard invariants, other constraints, non-goals and negotiable trade-offs distinguished | Hard-vs-soft tests and prompt examples |
| R08 | P0 | Priority guides advice; never silently resolves a goal conflict | Conflict tests for both priority orders |
| R09 | P0 | Explicit autonomy/iteration/retry/escalation/stop policy | Permission-boundary and blocked-stop tests |
| R10 | P0 | User approves outcome/contract before architectural choices; minimal grilling burden | Interactive/skill fixture evidence |
| R11 | P1 | Versioned approval/change-history; targeted invalidation of tasks/tests/evidence | Revise an accepted criterion and trace stale results |
| R12 | P0 | Drive canonical grill skill and ChatGPT paste-ready version produce same contract; setup fallback asks only missing | Cross-path parity fixture |
| R13 | P0 | For every automatable goal and final-product scenario, derive broad behavior acceptance-test specs at grill | Coverage checker links goal/scenario/criterion to spec |
| R14 | P0 | Non-automatable outcomes create explicit operator checks and sign-off | Operator-only goal never considered automated pass |
| R15 | P0 | Specifications independent of architecture and test implementation; valid alternate approaches allowed | Independent black-box scenario example |
| R16 | P0 | Setup materializes REAL executable tests or maps credible existing tests in repo | Temporary example project test runner |
| R17 | P0 | Initial test baseline is actually RUN and status is truthful: expected RED, existing GREEN, blocked environment, operator-only, unknown | Five baseline-state fixtures |
| R18 | P0 | RED failures reflect unmet behavior, not broken imports/config/dependencies/runner | Broken harness distinguished from honest RED |
| R19 | P1 | Safe fixtures, deterministic oracles, bounded E2E cost; no paid/production mutations | Mock integration-safe tests; configuration safety |
| R20 | P0 | Tests drive gap-to-ROADMAP decomposition with G/S/AC/test traceability | Start with RED; planner creates legitimate tasks |
| R21 | P0 | Dual loops: inner unit/integration TDD and outer product acceptance TDD | RED→implementation→GREEN replay |
| R22 | P0 | Pending future RED acceptance tests allowed during interim merges; preserve already GREEN | Partial-project merge not blocked by future RED |
| R23 | P0 | No agent weakening/removing/bypassing/scoping-away product acceptance tests for green; reviewed corrections only | Tamper attempts, test edits and xfail checks |
| R24 | P1 | Valid green requires trustworthy behavior/oracle, not superficial mocking or green-count gaming | Mock-only result rejected as final evidence |
| R25 | P1 | Performance baseline, multi-service E2E, security negative tests, uncertainty where not automatable | Representative fixtures/qualification |
| R26 | P0 | PLAN phase goal mappings retained but task-level mappings independently accurate | Phase direct/child enabling/maintenance mapping |
| R27 | P0 | Contribution `direct`, `enabling`, `maintenance`, including goal-specific type when needed | Benchmark harness task classified enabled |
| R28 | P0 | `serves: []` skips direct advancement only, NOT invariant/global regression protection | Maintenance task causes unrelated goal regression |
| R29 | P0 | Missing `serves` != intentionally empty; known valid reference required for serving | Task mapping diagnostics tests |
| R30 | P1 | Before execution check path/dependencies/coverage to achieve every goal/scenario/criterion | Plan gap/contradiction detection |
| R31 | P0 | Draft validation warns; execution gates block material malformed/unknown/missing references in affected work | Same invalid contract warning vs gate failure |
| R32 | P1 | Task acceptance defines evidence, dependency, impacted tests and intended contribution | ROADMAP fixture check |
| R33 | P0 | Brief includes purpose, served goals, other goals, non-goals, priorities and hard constraints | Planner/implementer/reviewer/diagnoser/triage fixtures |
| R34 | P1 | Context bounded and role-specific; never silently truncate critical constraints; link source | Prompt snapshot and truncation guards |
| R35 | P1 | Durable session compaction/handoff: revision, known pass/fail/blocked, evidence, next tasks, decisions | Resume from fresh session without false done |
| R36 | P0 | D23 preserved: goal awareness does not authorize implementer scope expansion | Attempted unplanned goal feature prevented |
| R37 | P0 | Separate task correctness from goal advancement, never rework a correct task just for plan error | Task pass/no expected metric change accepted |
| R38 | P0 | Six impact statuses: advanced, enabled, unchanged_expected, no_progress_unexpected, regressed, unknown | Classification fixtures for each |
| R39 | P0 | Reviewer observations evidence-based; deterministic rules decide verdict; telemetry not override | Incorrect review claim no silent pass/fail |
| R40 | P0 | Outcome-appropriate evidence beyond one diff line incl tests, metrics, UI trace, operator | Diff without runtime proof remains unknown |
| R41 | P0 | Regression outside served goals blocks/escapes safe work according to invariants, proof and conflict rules | Regress unrelated goal on `serves: []` task |
| R42 | P1 | Unknown evidence tracked and targeted, not forced advanced/regressed | Indeterminate benchmark handling |
| R43 | P1 | Meaningful milestone checkpoints, not costly full E2E on each task | Phase checkpoint catches residual RED |
| R44 | P0 | Modified Q1 B evidence-based: repeated unexpected misses, single decisive contradiction, or milestone gap | Trigger/no-trigger per-goal history cases |
| R45 | P1 | Proposed corrective plan has root cause, scope, alternatives, evidence, cost and impact | Alert contains an actionable plan not a bare question |
| R46 | P0 | Autonomous routine within-bound plan changes; user approval for material change; holds isolate dependent tasks | Independent task keeps executing while conflict held |
| R47 | P0 | Conflict detected at grill, setup, implementation/review, replan, completion | Incompatibility fixture at each stage |
| R48 | P0 | Conflict or advancement-vs-regression **always** requires USER resolution; priority never grants automatic sacrifice | G1 prioritized over G2 still escalates |
| R49 | P0 | Conflict escalation includes IDs, revision, evidence/estimates, alternatives/consequences, recommended option/rationale | Telegram/UI payload snapshot and simulated decision |
| R50 | P0 | Approved conflict decision records scope, limits, rationale; updates goals/tests/plan as needed; prevents reasking without new evidence | Approve-bounded tradeoff/then exceed bound |
| R51 | P0 | Hard invariant violations remain blocked, not balanced away; explicit exception authority | Hard regression fails despite high priority progress |
| R52 | P1 | W7 backlog groups/triages non-blocking findings; declines moot with reason; protects global tests | Repeated polish findings form one grouped task |
| R53 | P0 | W11b verifies each mandatory goal, behavioral scenario/acceptance test, E2E, operator check & metrics | Gate refuses done on any unmet mandatory criterion |
| R54 | P1 | Completion states complete/incomplete/blocked, not based on empty queue or textual claim | Queue empty with failing test => incomplete |
| R55 | P1 | W11b unmet outcomes propose scoped tasks; corrective work loops back through relevant test/polish/check, bounded by stop policy | Failed final gate→work→retest, no infinite loop |
| R56 | P1 | Final product outcome report and accepted trade-offs/limitations with evidence links | Report example for passing & blocked project |
| R57 | P2 | W12 strong-model analyst, weekly + project-end, ranked alternatives (planner, prompts, model/effort, graph, gate, human, skills) | Proposal set and schedule coverage |
| R58 | P0 | No automatic `fault: planner` on unexpected no-progress; rule-based attribution only when evidence establishes fault; unknown possible | Dependency/test-harness/no-progress misattribution prevented |
| R59 | P2 | W12 evaluates true effects, false alarms/holds, cost, time-to-outcome, quality; not raw green counts | Before/after test and digest |
| R60 | P2 | W12 proposal evidence, predicted saving, risks, measurement and rollback; approval/decline on Telegram, no resubmit without new evidence | Decline/retry/measurement cases |
| R61 | P0 | Human authority matrix: agent local actions vs user-goal/conflict/material/paid/destructive | Approval boundary test matrix |
| R62 | P1 | Preserve preexisting W11a work, legacy data, graph + legacy agents, migration of old statuses | Regression suite and migration tests |
| R63 | P1 | Reconcile approved changes with active W11a/W7/W11b/W12 plan; ALL P0/P1/P2 staged and tracked | Existing plan links every R-ID |
| R64 | P1 | Reproducible multi-case end-to-end feature proof using sample project; independent evidence and test provenance | Sample project demonstrator artifact |
| R65 | P2 | Efficiency: bounded role excerpts, retrieval on demand, incremental test selection, no unnecessary role/gates or costly recurring E2E | Prompt-size/runtime/call-count before/after |
| R66 | P1 | Spec gaps only prompt user for genuine choices; record answers and continue independent work | Multiple questions coalesced, no needless stop |
| R67 | P1 | Change-controlled acceptance criteria; contract revision invalidates stale goal proof and updates test mappings | Test passes old revision but cannot close new one |
| R68 | P1 | Ledger never retroactively changes acceptance verdict; fault classification provenance persisted | W12 ledger action leaves past verdict unchanged |
| R69 | P1 | Goals / scenario coverage diagnostics including orphaned goal, missing path or untested promise; avoid false coverage from mere metadata | One goal without task/test surfaced before execution |
| R70 | P2 | Polish, enabling, operator-only and project-specific special cases handled without alert noise | No bogus Q1 from enabling or maintenance |

### Relation to the earlier 13 audit recommendations

All 13 substantive fixes are covered: (1) fuller execution contract R01–R12/R61/R66; (2) testable final product R06/R13–R25; (3) enabling classification R26–R29/R38; (4) evidence beyond diff R37–R42; (5) stricter validation R30–R32/R69; (6) cross-goal and global regression R07/R08/R28/R41/R47–R51; (7) early detection R43; (8) Q1 smarter R44–R46; (9) attribution R58; (10) revision lifecycle R04/R11/R67; (11) approval boundaries R09/R49/R61/R66; (12) bounded completion loop R52–R56; (13) W12/measurement R57–R60/R68. Additional role-context/efficiency optimizations are R33–R35/R65/R70. User-added outside-in TDD and user-resolved conflicts are explicit throughout.

## B. Workstream allocation (proposed, adjust to live repo with evidence)

| Existing stream | Logical responsibilities / dependencies | Ranges requiring explicit placement |
|---|---|---|
| W11a / grill-me / setup | Schema, new contract, early scenarios/spec generation, legacy goals/check parsing, accurate `serves`/contribution, evidence status, review rules and conflict decision capture | R01–R18, R26–R42, R47–R51, R61–R62, R66–R67, R69 |
| Outside-in acceptance-test/setup work (reuse current W11a or dependent workstream) | Compile specs to runnable acceptance tests, credible RED baseline, persistent mapping, test safety and anti-cheating | R13–R25, R30–R32, R64 |
| Running plan/progress checks (current W11a or near W11b) | Milestones, Q1 replacement, replanning, authority, holds | R35, R43–R46, R61, R66, R70 |
| W7 | Triaged late polish and global invariant protection | R28, R41, R52, R55 |
| W11b | Product completion gate, operator validation, repeatable bounded work loop, result report | R14, R17, R22–R25, R53–R56, R67 |
| W12 | Fault provenance, feedback, independent calibration, measured optimization | R39, R57–R60, R65, R68, R70 |
| Cross-cutting | Graph/legacy parity, contract changes, anti-regression, prompt cost | R04, R11–R12, R33–R35, R62–R65 |

**Don't assume these allocations override existing repo dependencies.** They are a coverage checklist, not a command to split tasks a particular way. An already-delivered workstream must be updated if its present behavior conflicts with the approved spec.

## C. Mandatory system-level test scenarios

The agent must design automated tests/fixtures representing these behaviors. Use actual stack/framework; examples describe *expected outcome*, not test implementation.

### C1. Contract/grill/validation

1. User expresses product goal + final walkthrough. Grill produces stable IDs, testable expected scenarios, verification, constraints and operator checks; no detailed architecture imposed prematurely.
2. Two distinct grill implementations (canonical Drive skill, ChatGPT portable) yield equivalent contract semantics. `maestro-setup` accepts either and only asks truly missing choices.
3. Existing six-section GOALS loads and migrates without losing old IDs. Retired G2 never assigned to a new goal. A modified G1 target causes tests/proof to be marked stale.
4. Empty `serves`, omitted `serves`, unknown served goal, orphan goal, malformed contract and orphan scenario produce correct warnings during draft and correct gate failures at execution. Valid `serves: []` maintenance remains legal.
5. Inheriting a phase serving G1 must not falsely claim that *every* child directly advances G1; test an enabling and a maintenance child.

### C2. Goal-driven RED/GREEN acceptance tests

6. A project begins with a missing, but clearly specified, valid-input happy path: test runner works and **assertion** fails because output missing; baseline records legitimate expected RED.
7. Same project contains an already working error-handling behavior: existing baseline GREEN and future changes cannot break it unnoticed.
8. An import error, missing test dependency, absent credentials or missing service must yield `blocked_environment/unrunnable`, never be counted as a legitimate product RED.
9. Operator-only experience (visual quality, subjective interpretation) has an explicit request+verification record; neither no test nor an unconditional success means passing.
10. Contradictory or unknown numeric threshold triggers a user clarification rather than fabricated test oracle.
11. ROADMAP maps missing behavior to goals, scenarios, acceptance IDs, test commands; agent builds enabling harness before direct behavior where needed; inner unit TDD and outer acceptance TDD remain consistent.
12. A legitimate partially implemented project can merge a task while other product tests are still expected RED. An earlier GREEN test regressing requires applicable investigation/decision.
13. Implementer changes an acceptance-test oracle or disables/xfails a failing test to fake completion: guard flags or rejects it; reviewed test correction allowed only if not disguising a contract change.
14. A mocked service returning success without exercising the true workflow does not satisfy a real end-to-end contract. Final gate requires trustworthy evidence.
15. A performance contract provides a reproducible baseline and tolerance; no claimed advancement purely from diff; indeterminate measurement -> `unknown`.

### C3. Review, regression, user decisions

16. A benchmark-fixture `enabling` task passes acceptance; no metric improvement is expected; no `no_progress_unexpected` or planner fault is emitted.
17. A `direct` task fulfills its requested change but fails to improve the expected goal outcome. If otherwise valid/no regression, merge task, record the unexpected shortfall, propose plan correction at right boundary.
18. Housekeeping task with `serves: []` breaks an unrelated goal's previously passing acceptance test. Regression protection still applies.
19. A diff suggests speed improved but no benchmark was run: reviewer must report `unknown`, not `advanced`.
20. A reviewer explicitly claims success despite failing verification; deterministic verdict overrides unsupported assertion. Telemetry does not rewrite earlier accepted verdicts.
21. Competing G1=accuracy and G2=latency: improvement on G1 measurably worsens G2. Even if G1 is higher priority, autonomous agent cannot accept tradeoff; send user concrete alternatives, recommendation, and consequences; pause dependent tasks, let independent work continue.
22. User authorizes bounded +10% latency in pursuit of G1; later +20% appears. Original bounded approval is insufficient; re-ask. Record decision in contract and update test tolerances only after approval.
23. A hard data-integrity invariant fails while a goal improves: hard block; do not quietly trade it away.
24. Same goal conflict was previously fully resolved with same facts and scope: don't ask again. New material evidence does justify fresh request.

### C4. Adaptive planning, completion and learning

25. Two unrelated/expected enabling tasks must NOT trigger Q1; two informative unexpected no-progress results should trigger corrective analysis (not automatic blame).
26. One decisive impossibility finding triggers replanning immediately, not waiting for two tasks. Milestone reveals all remaining goal tests RED despite accepted tasks -> address before final gate.
27. Routine within-goal task reorder may be autonomous; a material goal scope/threshold change escalates. Dependencies held while unrelated task continues.
28. Context compaction/restart retains real test baseline, goal revision, approvals, and unfinished obligations. No hallucinated green.
29. W7 groups duplicate nonblocking findings; polish itself does not escape invariant checks and may cause repeat targeted verification.
30. Task queue empty but S1 acceptance test still RED or operator check unapproved: result is `incomplete`/`blocked`, never `complete`.
31. Failed W11b gate proposes scoped work; work and relevant checks/polish repeat; after bounded attempts an evidenced blocker is reported rather than looping forever.
32. Existing historical `no_progress` event migrates without being retroactively certified planner-fault; W12 respects provenance/unknown; a past declined W12 suggestion is only resurfaced on **new** evidence.
33. W12 reports actual before/after effect and resource cost, avoids gaming acceptance green count or increasing false positive alerts.

## D. Required plan-integration and delivery checklist

Use the following as **exit criteria** for the full requested integration:

- [ ] Every R01–R70 in this file mapped to an item in the existing development plan, with current implementation evidence, dependencies, target workstream and tests. No untracked `later` bucket.
- [ ] Original B16, W11a, D23, W7, W11b, W12, Q1 decisions explicitly reconciled; superseded rules are not accidentally reintroduced by legacy paths.
- [ ] `docs/GOALS.md` format, `docs/PLAN.md` mapping, ROADMAP and role briefs support goal/criterion/scenario/test links without bloating prompts.
- [ ] Canonical Drive grill-me skill and its relevant format documents updated; portable ChatGPT grill and `maestro-setup` are semantically compatible.
- [ ] Outer-level tests are generated/mapped and actual RED/GREEN/blocked baseline is verifiable; coverage includes goals and *final product* scenarios; no false RED.
- [ ] Inner task-level TDD remains effective; correct tasks aren't reworked just due to planner error; evidence-backed goal impact available.
- [ ] Cross-goal conflicts ALWAYS go to user with recommendation, and decisions persist through compactions; independent work doesn't stop unnecessarily.
- [ ] Regression checks cover `serves: []`, non-served goals, changed goals and hard invariants; no unapproved test weakening.
- [ ] Q1 modified-B logic and W11b bounded final repeat gate work on demo project; empty task queue cannot masquerade as complete.
- [ ] W12 correct attribution plus all approved analytics/optimization improvements, with measurable effects.
- [ ] Graph/legacy and migration tested as applicable; relevant end-to-end demo and code tests pass, with actual outputs retained.
- [ ] Any uncompleted prerequisite has a named owner, dependency, planned implementation step and honest blocked/not-started status, never called complete.

## E. Expected agent completion report

Include:

1. **Reconciliation:** baseline observed in live repo, active branch/concurrent changes, what original behavior was preserved vs superseded.
2. **Plan:** link/path to modified single current Maestro development plan and traceability matrix, all 70 R-IDs assigned, W11a/W7/W11b/W12 stage mapping.
3. **Code/docs:** actual commits/diffs, canonical Drive grill path updates, setup/ChatGPT grill parity.
4. **Verification:** test commands/outcomes, RED→GREEN demonstration, regression and conflict-decision cases, resource/prompt effects where measured.
5. **Remaining:** explicit undone requirement IDs, dependencies, truthful status, any decisions awaiting the user. Do not declare finished while required items are merely planned.
6. **Handoff hygiene:** temporary Drive files deleted after durable integration, or precise cleanup list if deletion unavailable.

## F. Full previous-audit inventory check (human-readable cross-check)

The initial audit's problems and proposed solutions were:

1. Insufficient operational contract (outcome, verification, constraints, boundaries, retry, blocked stop) → R01–R12, R61/R66.
2. Descriptive rather than executable final product → R06/R13–R25/R53.
3. Misclassification of essential enabling work → R26–R29/R37–R38/R70.
4. One-line diff evidence insufficient → R38–R42, R17/R25.
5. Warnings only for malformed goals/unknown IDs → R29–R31/R69.
6. Global no-regression inconsistency with `serves: []` and real tradeoffs → R07–R08, R28/R41/R47–R51.
7. Plan failure discovered at project end → R30/R43–R46.
8. Fragile consecutive-two no-progress heuristic → R44–R46.
9. Incorrect automatic blame assignment → R58/R68.
10. No goal revision/invalidated evidence policy → R04/R11/R67.
11. Unbounded ambiguity over what requires user involvement → R09/R46–R51/R61/R66.
12. Final polish/goal verification can loop without contract-governed stop → R52–R56.
13. Long-running handoff doesn't persist true progress and verification → R33–R35/R65.
14. W12 optimization telemetry might optimize the wrong thing → R57–R60/R68.
15. Excessive prompt/testing overhead and needless interruptions → R19/R33–R35/R43/R65–R66/R70.

The two additional user requests (added after the audit) are explicitly: (a) initially RED **goal-/final-scenario-derived product acceptance tests** guiding autonomous gap filling, R13–R25/R53/R64, and (b) **mandatory human adjudication of any goal-goal conflict**, with recommendation, R08/R47–R51.
