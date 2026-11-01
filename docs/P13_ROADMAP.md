# Phase P13: post-P12 features

A public, readable roadmap for Maestro's P13 phase. It is a cleaned copy of the working spec: the goal, the binding decisions, the stages, every workstream with its scope and proof, and the cross-cutting rules. Each workstream has one plain status line, checked against the checkboxes in the working spec and against git history (status as of 2026-11-01).

- **Depends on:** P12 (closed 2026-10-09, tag `p12-qualified`), P12B (failure ladder), P07B (agy driver), and modelctl (a separate project that tracks model classes and limits).
- **Output:** a global config with strength-only roles; pace routing over three harnesses under a cost budget; a polish stage; a manual-step contract; a goals contract; blame telemetry; every open thread closed.
- **Verification:** `.venv/bin/python -m pytest -q` (the full suite) per workstream, before every commit.
- **Not in this phase:** the Context Gate (see section 4).

## Status at a glance

| Workstream | Status |
|---|---|
| W1 Global config, routes, one bot | Done and released (slices 1-6, 2026-10-10 to 2026-10-16) |
| W2 Telemetry, complexity, blame ledger | Done; merged to root 2026-10-15 |
| W3 Usage plane | Done; qualified and merged to root 2026-10-26 |
| W4 agy as a full peer | **Not built.** Only its test-reliability work is merged (2026-10-26); the agy routes, canonical model identity, and confined writes remain |
| W5 Pace routing, budget, sprint | Done; integrated and released at `9014b10` on 2026-11-01 |
| W6 Manual-step contract | Done; merged to root as `70d5077` on 2026-10-21 |
| W7 Polish stage | Done (built under the goal-driven plan, 2026-10-24) |
| W8 Reliability carry-overs | Done; merged to root 2026-10-14 |
| W9 Safety and gate checks | Done; merged to root 2026-10-14 |
| W10 Visibility | **Not built** |
| W11a Goals docs and briefs | Done (2026-10-22) |
| W11b Goal check stage | Done (2026-10-22); runs after the polish stage |
| W12 Improvement proposals | Built (2026-10-25) under the goal-driven plan; the original W12 boxes in the working spec are not ticked, and the itv pilot has not run |
| W13 Runaway detection | **Not built.** Only the `runaway.mode` config key and the routing plumbing that reads it exist |
| Context Gate | **Not built.** Per `AGENTS.md` it will be built independently (`docs/maestro-context-gate-integration.md`) |

Not yet done either: the qualification pilot on itv and the phase close-out (section 3).

---

## 1. Objective

P12 qualified the graph runner on two real projects. P13 builds the four features queued afterwards (agy as a full peer, model choice by usage left with the `project.yaml` redesign, follow-ups at an end-of-project polish stage, the manual-step contract). It also closes every open thread the post-P12 audit found, and adds: a goals contract from grill to final check, blame telemetry for every extra step, runaway detection, a use-it-or-lose-it sprint, and evidence-based improvement proposals.

The measure of success is the pilot goal: **lower subscription usage per accepted task than P12, with no rate-limit pauses and no drop in quality.**

### 1.1 Binding decisions

| # | Topic | Decision |
|---|---|---|
| B1 | Scope | Every open Maestro item. The Context Gate stays independent. |
| B2 | Config layering | `~/.maestro/config.yaml` holds defaults; `project.yaml` overrides any key (deep-merge); the ROADMAP task overrides last. Auto-slim migration at idle. |
| B3 | Role shape | A role states `strength` and `independent_of`. **A role has no effort.** |
| B4 | Model choice = model + effort | A route is `(harness, model, effort)`. Each pair has its own strength class and `relative_cost` (e.g. opus at medium and a second model at high may both be `strong`). The owner approves each pair's class at first, over Telegram. Later, telemetry may **suggest** a regrade with its evidence; it never regrades by itself. |
| B5 | Usage source | QuotaPulse bridge first, native readers as fallback. |
| B6 | Routing order | Strength, then the 5h gate, then the evidence guard, then weekly pace. Per node. A resumed session keeps its harness. Pace never buys a weaker model. |
| B7 | Budget | Global, priority-weighted, in `relative_cost` units. **The cap falls as the used share of the 5h window rises. It never falls because the reset is near; a near reset with headroom left admits more.** |
| B8 | agy | Full peer. A third-party model served through agy (for example claude-opus-5.5 or claude-sonnet-5.5) is the same model as on its own harness: same class, same family for independence, same evidence prior. Its quota pool (antigravity-3p, low limit) and its telemetry are separate. |
| B9 | Telegram | One Maestro bot. |
| B10 | Complexity | The reviewer scores 1-5 after the fact. The planner forecasts 1-5 before the run **only while runaway detection is on**. |
| B11 | Runaway detection | Built, on by default, one switch turns it and the planner forecast off. The pilot measures whether it is overused. |
| B12 | Sprint | Use-it-or-lose-it is on by default. The owner can skip one week in advance or stop it live, from Telegram. Codex planned and banked resets are inputs. |
| B13 | Per-class model preference | Approved as planned. |
| B14 | Improvement proposals | Not only prompt fixes: any lever the evidence supports (stronger model or effort, graph order, a gate, a human step, a prompt). Each is approved on Telegram. |
| B15 | Project priority | Asked by the `maestro-setup` skill (default if skipped) and changeable by a Telegram command. |
| B16 | Goals contract | Full, in P13: grill-me writes the goals, maestro-setup reads them, every role sees them, a goal check runs before a project is done. Extended 2026-10-17 to goal-driven execution with project-level outside-in TDD (requirements R01-R70, plan in `docs/agent-plans/p13-goal-driven-tdd/`). It supersedes parts of W11 and adds goal-related items to W7 and W12. |
| B17 | Pilot | instagram-to-value (itv). DuetFlow is finished: it is used only for the config-migration equivalence check and as P12 baseline data. |
| B18 | QuotaPulse widget row for Maestro | Future idea. Not built in P13 (section 5). |
| B19 | Soft independence, bubblewrap for agy, equal default priorities | As the plan's "I'll decide these" list. |
| B20 | Pair scores and role requirements | Every `(harness, model, effort)` pair has a numeric intelligence (light below 40, balanced 40-47, strong 48+) and a relative cost per quota pool. A role states a minimum intelligence and an effort rule: implementer and test designer 40, reviewer 48, all three with medium as the normal preference; planner and diagnoser 50 with high as a minimum. `xhigh` and `max` are ordinary efforts where the harness has them. **No fixed harness priority**: selection is over the whole enabled pool, costs are compared only inside a quota pool, and QuotaPulse-style weekly pacing compares across pools. agy stays inactive until W4. Medium is a near-cost preference: inside a pool, pairs within about 20% of the cheapest prefer medium unless measured evidence favours another pair. A risk-escalated task raises the intelligence floor to 48 only; its effort follows the role rule. The shipped policy is built from the same pair table. |
| B21 | Effort of an escalated task on a schema-1 policy | A task raised to `strong` by risk, class or failure rate still binds at `high` on a schema 1 policy, as before P13. Under schema 2, B20 governs. |
| B22 | `routing.harness_order` | Replaced by B20 (integrated and released 2026-10-11). Pools rank globally by fresh weekly pace and a deterministic node/pool hash. `doctor` reports the retired key. |

Future only, not built: subscription-cost calibration; cost per successful task; role-specific intelligence; adaptive effort; better cross-pool optimization; latency/context-aware routing; benchmark calibration against real outcomes.

### 1.2 Stages

Workstreams in one stage run as parallel tasks and must merge trivially (different files).

| Stage | Workstreams |
|---|---|
| 0 | W0 housekeeping (done 2026-10-10: 45 STATE threads triaged, the working spec) |
| 1 | W1 global config · W2 telemetry + blame ledger · W8 reliability · W9 safety and gate checks |
| 2 | W3 usage plane · W4 agy · W6 manual-step contract · W7 polish stage · W11a goals docs and briefs · W13 runaway detection |
| 3 | W5 pace routing, budget, sprint · W11b goal check stage |
| 4 | W10 visibility · W12 improvement proposals · qualification pilot on itv · close-out |

---

## 2. Workstreams

### W1 · Global config, routes and one bot (stage 1, L)
**Status: done and released.** Slices 1-2 released in `9f770ed` (2026-10-10); slice 3 (scored pairs, B20) in `15e0f01` (2026-10-11); slice 4 (one bot) on 2026-10-12; slice 5 (auto-slim) on 2026-10-12; slice 6 closed the carried follow-ups.

- [x] `~/.maestro/config.yaml`: roles, budget, priorities default, runaway, sprint, telegram, defaults. `project.yaml` deep-merges over it; a ROADMAP task overrides last. `maestro config show --effective` prints every key with its source. `doctor` flags unknown keys.
- [x] Role = `{strength, independent_of}`. Remove `effort`, `backend`, the mixed model map and `fallback_chain` from roles; the router owns fallback.
- [x] Route = `(harness, model, effort)` with its own intelligence, derived class and `relative_cost`, read from modelctl policy. The model profile holds native facts only; the pair profile owns routing judgments. Effort controls deliver only verified values.
- [x] Pair approval (B4): the modelctl Telegram flow asks the owner to approve or edit intelligence and cost for each useful effort of a new class; declined or unapproved pairs never enter policy.
- [x] `priority:` is a project key (default from global `defaults.priority`, 1 = normal). `maestro-setup` asks it once. Telegram `/priority [project] <weight>` rewrites it and commits.
- [x] One bot: token in `~/.maestro/.env`; every message tagged `[project]`; every command takes an optional project.
- [x] One Telegram command registry in code (name, arguments, one-line description). `maestro telegram commands --botfather` prints the BotFather list from it.
- [x] Auto-slim at the idle adoption: drop project keys equal to global, commit, send a Telegram notice.
- [x] Carried: the open items in `docs/MODEL_TRANSITIONS.md` "Open follow-ups from the live rollout".
- **Proof:** DuetFlow and itv effective configs are equal before and after the slim; a role with `effort` fails `doctor`; one Telegram smoke per project.

### W2 · Telemetry, complexity and the blame ledger (stage 1, M)
**Status: done.** Merged to root 2026-10-15 (`dc1e560`). Report: `docs/W2_USAGE_REPORT.md`.

- [x] Per-task roll-up over attempts and node states: implementer cycles, review rounds, gate failures, tokens (split), quota delta per window, wall time, cost units (`relative_cost` x tokens), keyed by harness x model x effort. Quota deltas are per-window account observations, never attributed or summed consumption.
- [x] The reviewer adds `complexity: 1-5` to `review.json` against a fixed rubric (files, cross-module reach, new behaviour, risk). Diff stats are recorded as a cross-check.
- [x] **Blame ledger.** The seven round-2+ review causes extend to every step beyond the happy path (one implementer, one gate, one review, merge, accept). Each extra attempt gets an `extra_step_cause`; one revisable table maps cause to the role at fault; agents state what they observed and never assign blame; nothing in the ledger can fail a run.

  | Extra step | Cause | Fault |
  |---|---|---|
  | Review round 2+ | the 7 existing origins | unchanged |
  | Gate failed, implementer reruns | `check_failed` / `uncommitted_tree` | implementer |
  | Gate failed | `check_broken` (selects nothing, wrong path, cannot pass as written) | planner |
  | Gate failed | `environment_missing` (tool, venv) | maestro |
  | Bad-output retry | `garbage_output`, `no_sentinel` | the node's own role |
  | Worker died | `worker_crashed` | harness |
  | Blocked, rerouted or waited | `quota`, `rate_limit`, `endpoint` | router if its 5h gate admitted the attempt, else none |
  | Diagnoser run, final run | diagnoser verdict | `poor_agent` -> the re-briefed role; `bad_graph` -> planner |
  | Question to the owner | `spec_gap` | planner |
  | Deny-list ask | approved / disapproved | maestro (list too broad) / implementer |
  | Permission ask | allowed / declined | maestro (list gap) / the asking role |
  | Manual step Redo | the owner's reason | prep, or planner when the brief and the check disagree |
  | Owner rejects after an approving review | the owner's reason | reviewer or planner |
  | Post-merge check failed | `escaped_defect` | reviewer |
  | Merge conflict | `parallel_scope_overlap` | planner |
  | Accepted task made no progress on its goal | `task_did_not_serve_goal` | planner; **superseded 2026-10-17 (R58):** no automatic planner fault, a rule attributes only when evidence establishes the cause, else `unattributed` |
  | Runaway early diagnosis | as diagnosed; `runaway_false_alarm` | as diagnosed; maestro |
  | Controller error, uncertain node | `controller` | maestro |

- [x] The roll-up sums tokens, cost units and wall time **per fault** per task, role and route. `maestro report usage --database <absolute-db-path> [--task <task-id>]` shows it read-only.
- **Proof:** an unchanged saved P12 accepted DuetFlow run gives 14 attempts, 4,718,781 known tokens (5/14), 2,031 known wall seconds (6/14); every extra-step family maps to real graph/runtime emission; unknown or malformed causes are flagged unclassified.

### W3 · Usage plane (stage 2, M)
**Status: done.** Qualified and merged to root 2026-10-26. Details: `docs/W3_USAGE_SOURCES.md`.

- [x] `UsageSource` reads the QuotaPulse bridge first (used %, reset, pace per pool: claude including the Opus-scoped weekly, codex, agy gemini and agy third-party). Bridge down or stale -> native readers; the switch is journalled. Every sample keeps `observed_at` and `staleness_bound`.
- [x] `ResetSignalSource` reads the Codex reset monitor read-only through its local HTTP API. Three signals, kept apart: `active_signal` (an announced forced reset with its deadline or window), `banked_signal` (a banked credit and its expiry), and `forecast` (`p24`, `p48`, `confidence`; informational, never an announcement). Monitor absent, unhealthy or stale -> no signal, never an error.
- **Proof:** bridge killed -> routing continues on native readers; values match the widget within 1%.

### W4 · agy as a full peer (stage 2, L)
**Status: not built.** The W4 test-reliability work (hang bounds, load-proof tests) was integrated 2026-10-26; none of the items below is done. The agy driver itself dates from P07B. Plan: `docs/agent-plans/p13-w4.md`.

- [ ] **First step:** confirm modelctl emits agy routes. modelctl has an `antigravity` provider (one class per model version, `--model` variant ids as aliases, the measured compaction boundary as the enforced window), but it writes no antigravity events until Maestro's `registry.py` declares `modelctl_provider="antigravity"`. If the provider is missing, stop and tell the owner; do not hardcode rows.
- [ ] Canonical model identity (B8): a model served by two harnesses shares class, independence family and evidence prior; pool and telemetry stay per harness. The evidence guard uses the harness's own statistics once it has the minimum sample count, else the shared prior.
- [ ] Confined writes: a bubblewrap wrapper on writers when the host has it, plus the post-run tree audit. The global permission lists apply to agy and Codex (from W9), replacing `--dangerously-skip-permissions`. Session-resume facts verified.
- **Proof:** agy completes a real implementer node and a real review node in the pilot; a write outside the tree is refused; a claude-via-agy reviewer is not treated as independent of a claude implementer.

### W5 · Pace routing, budget and sprint (stage 3, L)
**Status: done.** Integrated to root and released at `9014b10` on 2026-11-01. Known limits: `docs/W5_PACE_ROUTING.md`. Design plan: `docs/agent-plans/p13-w5-pace-budget-sprint.md`.

- [x] `objective: pace` runs: eligible routes by strength and hard limits -> soft independence -> 5h gate -> evidence guard -> rank by weekly pace delta -> budget admission. Every decision is journalled with its inputs.
- [x] **Budget (B7).** `B_pool = (target_5h - used_5h) / burn_pool`, where burn is the learned % of the 5h window per cost unit (starting from `relative_cost`). An attempt's charge against the current window is `relative_cost x burn x the share of its expected runtime that falls before the reset`. So the cap follows the used share, and a reset that is close while headroom remains admits more. Projects share B by `priority`.
- [x] Per-class preference (B13): success per cost unit by task class x complexity band x route feeds the evidence guard. The band is the planner forecast while runaway detection is on, else the task class alone.
- [x] **Sprint (B12).** Within about 24h of a pool's weekly reset, when the pool is behind pace by a margin, raise that pool's target and pull forward polish tasks and tasks waiting only on budget (never a task the owner holds). Codex reset signals (W3) feed the same sprint:
  - An **announced** forced reset: treat its deadline as a reset of the Codex pool and sprint before it, because unused quota is wiped.
  - A **forecast** forced reset: sprint the Codex pool when the forecast passes a configurable bar (default: `p24` at or above 70 with `confidence` high). A forecast is a likelihood, so it only raises the Codex pace target; it never lets the 5h gate or the budget be exceeded, and the sprint notice says it is forecast-based. The pilot reports how many forecast sprints were followed by a real reset.
  - A **banked** credit near expiry: sprint the Codex pool, then tell the owner to activate the credit (Maestro cannot).
- [x] Sprint control on Telegram: the start notice carries a "Skip this week" button; `/sprint off` skips the coming or current week once and re-arms after the reset; `/sprint stop` ends a running sprint now; `/sprint on`; `/sprint status`. A config key turns it off for good.
- **Proof:** fixture lanes for a pool behind pace, a full 5h window, a worse-evidence route, two-project contention. Property tests: at a fixed time to reset, B never rises as used % rises; at a fixed used %, admitted cost never falls as the reset nears. Sprint lanes: a skipped week re-arms; a held task is never pulled.

### W6 · Manual-step contract (stage 2, M)
**Status: done.** Merged to root as `70d5077` on 2026-10-21.

- [x] The prep brief carries each verification's `cmd` and paths.
- [x] Maestro writes the step script (output and exit code to a file it owns); the message states the command and the expected result; Done means "I ran it"; the gate checks the log and a step check.
- [x] Post-merge checks are split. Checks a command can decide run automatically after the merge and never reach the owner; a failure opens a fix task. **Only checks the owner alone can observe** (`kind: dan`: a message on a phone, an external console) go to them as Pass / Fail. Prerequisites are asked at plan time through the question channel.
- **Proof:** a replay of DuetFlow 08 passes with no Redo; the gate rejects a log with a non-zero exit; a machine post-merge check failure opens a fix task without a Telegram ask.

### W7 · Follow-ups at an end-of-project polish stage (stage 2, M)
**Status: done.** Built 2026-10-24 under the goal-driven plan (GDT-11): backlog store `maestro/backlog.py`, triage task, polish tasks grouped by write-set area, and loop-back within the stop policy.

- [x] A non-blocking finding goes to the project backlog unless the reviewer marks it `before_dependents`. When the main tasks are accepted, a triage node merges duplicates and declines moot items with a reason, then polish tasks run grouped by area. Existing queued `-followups` tasks finish as they are.
- [x] The D23 bound is part of the polish task's bar: do or decline each item, no new scope, and the reviewer may not widen the edge-case set.
- [x] Goal-driven additions: a polish task (`serves: []`) keeps the global regression and hard-invariant checks and reruns the product acceptance tests of the areas it touches; a W11b failure loops back through polish within the stop policy.
- **Proof:** a lane with 3 tasks and 5 follow-ups (2 duplicates, 1 moot) ends in 1 polish task.

### W8 · Reliability carry-overs (stage 1, M)
**Status: done.** Merged to root 2026-10-14.

One lane per item: a node with no sentinel is retried or failed, never left uncertain (a writer restarts from `start_sha`); an inline handler that raises settles and notifies; `worktree`, `human_receipt` and `async_job` probes; `ctl retry` / `ctl cancel` for a graph node; a consumer for the command inbox; `/ask`, `/redo` and selffix run the pinned version; the release self-test gets a private `MAESTRO_HOME` and one retry before holding; a pilot records idle adoptions in its manifest automatically; D10 edges 2-4. Nine W8 threads are closed.

### W9 · Safety and gate checks (stage 1, S-M)
**Status: done.** Merged to root 2026-10-14. Contract: `docs/W9_SAFETY.md`.

One lane per item: an out-of-scope path is a gate finding for the implementer, with an escape for files the task needs; `pre_validate` refuses a task with no required checks; family derivation never proposes a leading wildcard; the denial text names the denied family; permission lists for Codex and agy; the interpreter secret-read limit is documented and tested.

### W10 · Visibility (stage 4, S)
**Status: not built.**

- [ ] Progress signal for long CLI runs (token growth and a PROGRESS line). Telegram `/workflow` sends the picture `workflow show --html` already draws.
- [ ] Weekly digest: usage per accepted task, cycles, cost per fault (W2), complexity-normalised route comparison.
- **Proof:** digest numbers equal `maestro report usage`.

### W11 · Goals contract (a: stage 2, b: stage 3, M)
**Status: W11a done (2026-10-22); W11b done (2026-10-22), running after the polish stage.** One item remains: itv still needs its own `GOALS.md` from a short grill by the owner before the pilot.

Redesigned 2026-10-17 as goal-driven execution plus project-level outside-in TDD. The normative contract, the requirement register R01-R70 and scenarios C1-C33, and the plan items GDT-0..GDT-14 are in `docs/agent-plans/p13-goal-driven-tdd/`. It keeps D23, "reviewer observes, rules decide", and "telemetry never changes a verdict". Replanning is considered on repeated unexpected misses (default alert threshold 2), one decisive contradiction, or a milestone gap; routine in-scope changes are autonomous, material ones go to the owner.

- [x] **W11a, grill-me:** goals are the first branch of the grill. Output `docs/GOALS.md` in the `GOALS-FORMAT.md` format: purpose in two sentences; who uses it; goals with stable ids (`G1`...), each with a measurable target and how it is verified; final-product expectations as a walk-through; non-goals; the priority order used when goals conflict. The contract is the `GOALS.md` format, not the skill, so a grill run elsewhere (for example in a chat assistant) works too.
- [x] **W11a, maestro-setup and Maestro:** setup harvests `GOALS.md` (and asks the same questions when it is missing). Planner, implementer, reviewer, diagnoser and triage briefs carry a bounded goals excerpt. A ROADMAP task has `serves: [G1]`; `doctor` fails a task the execution gate would hold.
- [x] **W11b, goal check stage:** after the polish stage and before "project done", the completion gate runs the approved acceptance tests and operator checks at the current contract revision, typed `complete` / `incomplete` / `blocked`, with a bounded corrective loop and a product outcome report.
- [ ] itv gets its `GOALS.md` from a short grill before the pilot (operator step).
- **Proof:** a lane whose project has 2 goals, one unmet, ends with a proposal for the unmet goal and is not reported done; a `GOALS.md` written outside the skill passes setup's validation.

### W12 · Improvement proposals (stage 4, M)
**Status: built 2026-10-25 under the goal-driven plan (GDT-12).** The analyst, proposals, decisions and measurement code is in `maestro/improvement.py` and related modules; it has not run in the itv pilot, and the working spec's original W12 boxes are not ticked.

- An analyst node (strong route) reads the blame ledger and the per-route statistics, weekly and at project end. It proposes ranked changes, each with its evidence, the expected saving in cost units, and how the effect will be measured. Lever types: role prompt, stronger model or effort for a role x class, graph order, an added gate, an added human step, context or skill for a role, a planner rule.
- Each proposal is sent on Telegram (Approve / Decline / free text). Approved config levers are applied to the global config and are reversible; approved code levers enter Maestro's own queue through operator intake. The next digest reports the measured effect.
- Goal-driven additions: attribution only by evidence (`unattributed` allowed), provenance kept, the ledger never changes a verdict; each proposal carries pattern and evidence, expected saving, risk and cost, measurement baseline, follow-up and rollback; the digest measures effects (false holds, time-to-outcome, cost, quality), never raw green counts.
- **Proof:** a fixture ledger dominated by reviewer `missed_earlier` yields a reviewer-side proposal; a declined proposal is not re-sent without new evidence.

### W13 · Runaway detection (stage 2, M)
**Status: not built.** Only the `runaway.mode` config key and the routing code that reads it exist (added with W5).

- [ ] `runaway.mode: on | shadow | off` in the global config, project override, Telegram `/runaway on|shadow|off`. `off` also removes the complexity forecast from the planner's brief and schema, so the planner's output is what it was before P13.
- [ ] The planner forecasts `complexity: 1-5` per task with the reviewer's rubric. An attempt whose spend passes `factor` (default 2.0) x the p90 of its role x task class x complexity band stops further retries and enters the P12B ladder at the diagnoser. A band with fewer than 10 samples never triggers. `shadow` records the trigger and does nothing.
- [ ] Pilot focus: triggers per 100 attempts; the share where the diagnoser found a real fault versus `runaway_false_alarm`; estimated quota saved versus the cost of the early diagnoser.
- **Proof:** lanes for trigger, under-sampled band, shadow, and off (planner brief has no forecast).

---

## 3. Acceptance criteria

1. The full suite is green at every commit. A commit in the main Maestro checkout is a release, so experiments go in linked worktrees on persistent storage, never `/tmp`.
2. Every `owner: P13` thread in the STATE registry is `closed` with evidence or `accepted` with a reason.
3. Each workstream's Proof line passes as a named lane or test.
4. **Qualification pilot on itv:** agy runs a real implementer node and a real review node; zero rate-limit pauses; quota % and cost units per accepted task are below the P12 figures; runaway and sprint behaviour is reported with the W13 and W5 metrics; one goal check runs end to end.
5. **Close-out:** send the owner the full Telegram command list in BotFather format (`maestro telegram commands --botfather`) with one line per command; update `docs/DESIGN.md`, `docs/EXECUTION.md`, `docs/PROGRESS.md`.

---

## 4. Forward compatibility: the Context Gate (do not implement)

The Context Gate (CONTINUE / COMPACT / HANDOFF at work-unit boundaries; `docs/maestro-context-gate-integration.md`) is not built and is built independently. P13 changes routing and scheduling, so it must keep these seams:

- **One dispatch choke point.** Pace routing, the budget, the sprint and runaway detection all act through `advance` -> `resolve` -> `claim` -> `launch_worker`. Runaway detection enters the existing ladder; it adds no launch path.
- **One fresh-vs-resume decision.** "A resumed session keeps its harness" is an input to the existing session decision, not a second one.
- **No context rotation.** Nothing here compacts, summarises or hands off a session. Runaway detection measures spend, not context.
- **Telemetry stays nullable and append-only.** The blame ledger and the roll-up are views and new nullable columns; the gate can later add its own without a migration fight.
- If a workstream can only be built by breaking one of these, stop and ask.

---

## 5. Open items carried, not built

- **QuotaPulse widget row for Maestro** (in-flight cost and current B on a phone): a possible future idea, not wanted now.
- The Context Gate (independent track).
- Free-model pilots and Layer 2 (SCIP) repository facts stay where earlier phases left them.
- codex-bridge `skills_approval` callback bug (`docs/MODEL_TRANSITIONS.md`): belongs to codex-bridge, not Maestro.
- **Legacy loop exits when every harness is exhausted:** `implementer.launch_implementer` raises `PolicyError('no eligible implementer route: ...')` and `orchestrator.main()` does not catch it at the fresh-launch call, so the process exits instead of waiting for quota. Not reachable under `engineering.runner: graph` (both real projects). Fix when a project next runs the legacy loop: wait for the next poll, and do not create the worktree or send "Starting" first.
- **Two Haiku ids** are in use (`claude-haiku-4-5-20251001` in the policy, `claude-haiku-4-5` in `IMPLEMENTER_MODELS`). Nothing is made wrong by it; both resolve to the same class and limits row.
- **`maestro doctor` exits 1 on a project whose controller is running:** `[FAIL] docs: docs/ROADMAP.md did not parse: controller pid ... already owns .../control.sqlite3`. Every other check runs and reports. Fix: read the ROADMAP without taking the writer's ownership, or report this case as a WARN.
- **Compatible code must precede incompatible shared-policy publication:** a project on an older reader reads the shared policy at graph startup and fails before idle self-update on a newer policy shape. Verify all running readers before publishing a policy change. Startup compatibility hardening belongs to W8.
- **A full-suite run in a linked worktree materializes that worktree's HEAD in the real `~/.maestro/versions/`** (observed 2026-10-10, cause not traced). `~/.maestro/latest` was never moved by it. Some test reaches the real `MAESTRO_HOME`.
- **W1 slice 2 spec review, deferred items:**
  - Global layer: a global config that does not parse reads as no layer at runtime (only `doctor` says so), which drops `risky_set`, `deny_list_extra` and `confinement.deny` held there. Decide whether the safety lists fail closed. Three readers still see `project.yaml` alone: the preview-to-apply hash in `model_lifecycle.py`, `config_hash` in `model_inventory.py`, and the recommendation prompt in `workflows/deny_list.py`.
  - Smaller: `doctor`'s preamble deny-list check has no global-layer test; `derived_roles` reads the packaged role templates while the graph reads a project's replaced template; `config show --effective` prints only keys a layer sets, not code defaults; `doctor` does not flag an unknown role name or a nested typo under `routing`; some tests assert nothing or touch the real `~/.maestro`.
