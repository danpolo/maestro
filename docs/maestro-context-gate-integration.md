# Maestro Context Gate Integration

> **Audience:** agent implementing the Context Gate inside the existing Maestro orchestrator.
>
> **Dependency:** read `context-gate-common-spec.md` in full before implementing this file.
>
> **Scope:** Maestro-specific orchestration, execution, and logging. Do not duplicate or fork the shared policy. The shared learner is specified in `interactive-context-gate-and-learning.md`.

---

## 0. Goal

Maestro already has the component that this design needs: an orchestrator that owns task/subtask dispatch.

Integrate the Context Gate at the **Work Unit dispatch boundary**.

Do **not** create a second wrapper/orchestrator process.

Target flow:

```text
goal
  -> rolling Work Unit planner
  -> next WU ready
  -> Context Gate
       -> CONTINUE
       -> COMPACT
       -> HANDOFF
  -> dispatch WU
  -> verify WU outcome
  -> emit canonical log
  -> choose/register next WU
  -> Context Gate again
```

---

## 1. First implementation step: inspect Maestro

Before changing code:

1. locate the orchestrator / scheduler;
2. identify the canonical task/subtask object;
3. identify where an agent/session is selected or resumed;
4. identify where prompts are dispatched;
5. identify where task completion/verification is recorded;
6. identify current telemetry and persistence;
7. identify session creation/resume/termination abstractions;
8. identify current compaction/handoff capabilities if any;
9. identify any existing task decomposition/planning component;
10. identify whether agent platform adapters already exist for Claude Code, Codex, or others.

Produce an internal mapping from existing Maestro concepts to:

- `Goal`;
- `WorkUnit`;
- `Session`;
- `ContextSnapshot`;
- `GateDecision`;
- `OutcomeRecord`.

Prefer adapting existing structures over introducing parallel abstractions.

---

## 2. Work Unit ownership

Maestro MUST own the authoritative WU lifecycle.

### 2.1 Rolling-horizon planning

Do not decompose an entire large project into dozens of fixed units at project start.

Prefer:

- current active WU;
- next one or a small horizon (roughly 1–3);
- re-plan after verified completion or newly discovered prerequisite.

This reduces stale plans and gives the gate meaningful boundaries.

### 2.2 Internally discovered prerequisites

An agent executing WU-A may discover an independently verifiable prerequisite WU-B.

The worker MUST NOT silently absorb WU-B if it materially changes scope.

Protocol:

```text
worker -> ORCHESTRATOR_EVENT: NEW_PREREQUISITE
Maestro -> register WU-B
Maestro -> pause/mark WU-A blocked if required
Maestro -> run Context Gate for WU-B
Maestro -> execute selected A/B/C transition
Maestro -> dispatch WU-B
```

---

## 3. Context Gate insertion point

Run the gate **immediately before dispatching a new meaningful WU into an existing logical worker/session**.

Do not run it:

- before every tool call;
- before every model turn;
- for minor retry commands;
- for simple within-WU clarification;
- when the orchestrator is merely polling status.

Run it when:

- a new planned WU starts;
- a newly discovered prerequisite starts;
- a completed diagnostic WU transitions to fix WU;
- a fix transitions to verification WU;
- a user changes the objective enough to create a new WU.

---

## 4. Sensor collection

Before each gate decision, construct the canonical shared feature record.

### 4.1 Platform telemetry

Collect available:

- active input/context tokens;
- fresh input/cache read/cache write tokens;
- model ID;
- context-window size;
- effective auto-compaction boundary if known;
- subscription/rate-limit state if exposed;
- compactions since fresh session;
- units since fresh session.

### 4.2 Repository telemetry

Cheap deterministic sensors only:

- git dirty state;
- changed file count;
- current branch/worktree;
- failing-test count if already known;
- age of latest successful verification;
- presence/version of durable plan/task state.

Do not run an expensive full test suite merely to make a gate decision unless that verification is already required by WU completion.

### 4.3 Context ledger

Maintain a compact controller-side ledger, outside normal LLM context where possible.

Example:

```json
{
  "session_id": "...",
  "work_units_seen": [
    {
      "id": "WU-12",
      "type": "debugging",
      "status": "done",
      "relevance_to_next": "medium",
      "obsolete_hypotheses": 3,
      "key_artifacts": ["src/auth/token.ts", "tests/auth.test.ts"]
    }
  ],
  "compactions": 1,
  "last_grounded_at": "..."
}
```

Do not store full tool output.

### 4.4 Semantic feature evaluator

Invoke the common semantic rubric from the shared specification.

Use a cheaper model/effort than the primary coding agent when possible.

Input should be compact:

- upcoming WU;
- context ledger;
- latest durable plan;
- selected repo metadata;
- recent transcript tail/summary only when needed.

Do not feed the whole session to a second evaluator merely to decide whether the first session is too large.

---

## 5. Action execution

### 5.1 CONTINUE

Requirements:

1. record decision and policy probabilities;
2. preserve session;
3. dispatch WU;
4. do not inject unnecessary gate explanation into worker context.

### 5.2 COMPACT

Requirements:

1. hold WU dispatch;
2. invoke the platform-specific intentional compaction mechanism;
3. use the shared continuation-grade compaction requirements;
4. verify compaction completed;
5. update session/context telemetry;
6. increment `compactions_since_fresh`;
7. dispatch the held WU.

For Claude Code, `/compact [instructions]` is explicitly supported. Use persistent `CLAUDE.md` compaction instructions for invariant preservation requirements and dynamic focus only for WU-specific emphasis.

For Codex, inspect the installed/current platform capabilities. Do not assume Claude syntax. Current open-source Codex uses a continuation/handoff-oriented built-in compact prompt and exposes Pre/PostCompact hooks, but exact control surfaces may change.

#### Minimum compaction preservation requirements

Preserve:

- active goal and upcoming WU;
- current progress;
- verified decisions and rationale that are still relevant;
- exact user constraints not durable elsewhere;
- current changed-file list;
- current test/build state;
- unresolved blockers;
- rejected approaches only when forgetting them risks repetition;
- pointers to authoritative artifacts;
- explicit next step.

Discard:

- redundant raw tool output;
- completed low-value exploration;
- superseded file bodies;
- conversational repetition.

### 5.3 HANDOFF

Requirements:

1. hold WU dispatch;
2. run a **verified handoff** workflow;
3. write one-time handoff artifact `X`;
4. create a genuinely fresh target session/worker;
5. pass `X` plus the upcoming WU identity;
6. verify the new worker ingested it;
7. mark the old session retired;
8. delete/archive `X` according to local policy; it is not required as durable memory;
9. dispatch WU.

#### Verified handoff generation

Before writing X, inspect authoritative state:

- `git status`;
- relevant `git diff`;
- changed files;
- current task/plan records;
- test/build/lint results already available;
- relevant authoritative docs/specs;
- current blockers.

X should be concise and state-transfer oriented:

```markdown
# Continuation checkpoint
## Goal
## Upcoming Work Unit
## Verified current repository state
## Relevant decisions + rationale
## Constraints
## Modified/relevant files
## Verification status
## Rejected approaches worth remembering
## Unresolved risks/blockers
## Exact next action
```

Do not copy large files into X. Point to them and instruct the fresh agent to re-read selectively.

---

## 6. Maestro bounded exploration for learning

Maestro is the preferred place to collect counterfactual-learning signal because it can implement known action probabilities.

### 6.1 Never explore across unsafe actions

First compute the normal feasible action set using:

- hard capacity constraints;
- irreversible-information-risk constraints;
- quality lower bounds;
- action support/confidence.

Only explore when at least two actions are already predicted to be **near-equivalent in quality**.

Recommended initial exploration eligibility:

```text
abs(Q_lcb(a) - Q_lcb(best)) <= epsilon_explore_quality
```

with `epsilon_explore_quality <= epsilon_quality`.

### 6.2 Exploration rate

Initial recommendation:

```text
epsilon_explore = 0.05
```

Meaning roughly 5% of **eligible** boundaries, not 5% of all WUs.

Never force HANDOFF exploration when:

- exact-detail value is high;
- groundability is low;
- prediction support is weak;
- WU is high-risk/critical;
- capacity transition is currently unstable.

Prefer exploration between CONTINUE and COMPACT first. HANDOFF should enter the exploration set only after enough observational support exists.

### 6.3 Probability logging

When randomized, log the exact probability of the action that was actually selected:

```json
"policy_probabilities": {
  "CONTINUE": 0.475,
  "COMPACT": 0.525,
  "HANDOFF": 0.0
},
"actual_action_source": "bounded_exploration",
"exploration": {
  "eligible": true,
  "randomized": true,
  "logging_propensity": 0.525,
  "candidate_actions": ["CONTINUE", "COMPACT"]
}
```

This is mandatory for inverse-propensity / doubly-robust offline policy evaluation.

If the orchestrator overrides the sampled action for safety, record the final actual action and mark the record unsuitable for propensity-based evaluation unless the resulting probability is still correctly known.

---

## 7. Outcome capture

Maestro MUST close every WU record after outcome is known.

### 7.1 Objective outcome

Capture:

- completed/not completed;
- acceptance criteria pass fraction;
- task-specific test/build/static-check result;
- regressions;
- task reopened;
- follow-up repair WUs.

### 7.2 Quota/resource outcome

Capture before/after:

- subscription window utilization where available;
- fresh input;
- cache reads;
- cache writes;
- output tokens;
- wall time.

If a quota window resets during the WU, set:

```json
"quota_delta_censored": true
```

Do not train the direct quota-delta model on a naïvely subtracted reset-crossing sample.

### 7.3 User/human corrections

If a later user correction clearly shows that a previous WU was incomplete/wrong, backfill the original outcome record if possible:

- `user_correction = true`;
- `task_reopened = true`;
- increment repair/rework fields.

Do not silently overwrite the historical record; append an outcome amendment event or version it.

---

## 8. Shared log storage

Maestro MUST write exactly the canonical schema from `context-gate-common-spec.md`.

Recommended append-only layout:

```text
<shared-global-context-dir>/
  logs/
    context-gate/
      YYYY-MM/
        maestro-YYYY-MM-DD.jsonl
        interactive-YYYY-MM-DD.jsonl
  models/
  policy/
  audits/
```

The final path should follow the existing global skills/shared-state architecture if one exists.

The learner consumes records from **both** modes.

Maestro MUST NOT maintain private coefficients that drift from the global policy without recording them as a policy version.

---

## 9. Policy artifact consumption

Maestro reads an immutable/versioned policy bundle generated by the shared learner, for example:

```text
policy/
  current.json
  versions/
    policy-0007.json
  models/
    quality-0007.*
    quota-0007.*
    growth-0007.*
```

`current.json` should include:

```json
{
  "policy_version": "0007",
  "created_at": "...",
  "epsilon_quality": 0.02,
  "exploration_rate": 0.05,
  "minimum_support": {...},
  "feature_schema_version": 1,
  "model_artifacts": {...},
  "fallback_heuristic_version": "...",
  "approved": true
}
```

Load policy atomically. Never update weights in-place while a decision is running.

If the bundle is missing/corrupt/incompatible:

1. disable learned policy;
2. fall back to conservative shared heuristics;
3. continue logging;
4. surface an implementation warning.

---

## 10. Model/version drift

Always log:

- exact model ID;
- agent/harness version;
- platform version where available;
- semantic evaluator version;
- policy version.

A model upgrade can change long-context behavior and quota economics.

The learner may pool data across related models, but Maestro must not assume old model-specific thresholds remain valid.

For a newly observed model/version with little support, use conservative fallback/pooled priors until enough data accumulates.

---

## 11. Acceptance tests for the Maestro integration

Implement automated tests for at least:

### Work Unit boundaries

- command inside a WU does not trigger gate;
- new orchestrator subtask triggers gate;
- discovered prerequisite creates a WU and triggers gate.

### CONTINUE

- same session used;
- WU dispatched;
- correct log written.

### COMPACT

- WU held until compaction completes;
- compaction event recorded;
- WU dispatched afterward;
- post-compaction telemetry refreshed.

### HANDOFF

- old session does not execute new WU;
- verified X produced;
- fresh worker/session created;
- X ingested before WU dispatch;
- one-time artifact lifecycle handled;
- log records transition.

### Logging

- schema identical to interactive records;
- recommendation, actual action, and policy probabilities present;
- outcome closes later;
- quota-reset sample marked censored.

### Exploration

- only feasible near-equivalent actions sampled;
- exact propensity recorded;
- safety veto prevents unsafe exploration.

### Failure handling

- failed compact falls back safely;
- failed handoff never loses the old session before fresh-session verification;
- learner artifact unavailable -> heuristics, not crash.

---

## 12. Definition of done

The Maestro work is complete only when:

1. every meaningful WU dispatch passes through one Context Gate;
2. A/B/C are executable through existing orchestration abstractions;
3. no additional context-manager wrapper is required;
4. discovered subtasks cannot bypass the gate;
5. canonical records are emitted and outcome-closed;
6. bounded exploration logs valid propensities;
7. shared policy artifacts can be loaded atomically;
8. fallback heuristics work with no learner;
9. tests cover action transitions and log compatibility;
10. interactive and Maestro records can be concatenated and consumed by one learner without migration.
