---
name: grill-me
description: >-
  Interrogate project plans, architecture, and tradeoffs to resolve design decisions, updating CONTEXT.md and docs/PLAN.md. Use for rigorous project-level grilling.
---

<what-to-do>

Interview me relentlessly about every aspect of this project plan or design until we reach a shared understanding. Walk down each branch of the design tree, resolving dependencies between decisions one-by-one. For each question, provide your recommended answer.

Ask the questions one at a time, waiting for feedback on each question before continuing.

If a question can be answered by exploring the codebase or existing project files, explore them instead of asking me.

</what-to-do>

<supporting-info>

## Determine project state first

Infer the state from the repository; do not ask which case applies when the files make it clear.

### New project / bootstrap

Use this mode when there is little or no meaningful implementation yet.

- Read `PROJECT-BRIEF.md` first if it exists.
- Treat `PROJECT-BRIEF.md` as seed context, not as an authoritative design. Challenge its assumptions during the grill.
- Inspect any existing code or files before asking questions they can answer.
- Create permanent project documentation lazily as decisions become real.

### Existing project / new direction

Use this mode when meaningful implementation or project documentation already exists.

- Inspect the codebase before grilling the proposed change.
- Read the existing project documentation that is relevant to the topic, including `docs/GOALS.md`, `CONTEXT.md` or `CONTEXT-MAP.md`, ADRs, and `docs/PLAN.md` when present.
- Treat the implementation and existing docs as evidence. Surface contradictions instead of asking me to restate facts the repository can answer.
- Preserve established terminology and historical decisions unless the grilling explicitly changes them.

## Domain awareness

During codebase exploration, also look for existing documentation.

### File structure

Most repos have a single context:

```
/
├── PROJECT-BRIEF.md                 # optional bootstrap input
├── CONTEXT.md
├── docs/
│   ├── GOALS.md
│   ├── PLAN.md
│   └── adr/
│       ├── 0001-event-sourced-orders.md
│       └── 0002-postgres-for-write-model.md
└── src/
```

If a `CONTEXT-MAP.md` exists at the root, the repo has multiple contexts. The map points to where each one lives:

```
/
├── PROJECT-BRIEF.md                 # optional bootstrap input
├── CONTEXT-MAP.md
├── docs/
│   ├── GOALS.md                      # one set of goals, spanning contexts
│   ├── PLAN.md                       # one build plan, spanning contexts
│   └── adr/                          # system-wide decisions
├── src/
│   ├── ordering/
│   │   ├── CONTEXT.md
│   │   └── docs/adr/                 # context-specific decisions
│   └── billing/
│       ├── CONTEXT.md
│       └── docs/adr/
```

Create files lazily — only when there is something durable to write. `docs/GOALS.md` is the exception in timing, not in laziness: it is written during the first branch of the grill, as soon as the contract settles (see [Contract first](#contract-first)). If no `CONTEXT.md` exists, create one when the first canonical term is resolved. If no `docs/adr/` exists, create it when the first ADR is needed. Create or update `docs/PLAN.md` only once the design has settled enough that concrete remaining work is visible — see [Closing the session](#closing-the-session).

## Contract first

The first branch of every grill is the product contract in `docs/GOALS.md`: why the project exists, what the finished product must do, and how anyone will know it does. Every later design question is judged against it, so it is settled and approved before any design question. Brief questions about the purpose and the users may come first.

Walk these steps in order. Draft every answer yourself from `PROJECT-BRIEF.md`, the conversation and the repository, and propose it with your recommendation. Ask Dan only the decisions he must make, and confirm the drafted parts in one batch instead of one question each. Never demand a count of anything. Never invent a target or an oracle: write `Open question:` and ask only that.

1. **Purpose:** at most two sentences: what the project is, and why it exists.
2. **Users:** who uses it, and what each does with it.
3. **Goals:** for each outcome: a stable id, the outcome, its rationale, the baseline (a value measured today, or `not_applicable`), the target, the `AC-`/`OP-` ids that verify it, the `INV-` ids that constrain it, and the goals it depends on. Push back on targets nobody can measure ("fast", "easy") and on goals that are really tasks; split an omnibus goal into criteria and merge micro-goals.
4. **Final product:** a walk-through of what the user sees and does once the project is done.
5. **Scenarios:** from the walk-through, `S-` journeys: the normal flow, plus an error flow and a boundary flow where they matter. Every goal appears in a scenario and every scenario names its goals.
6. **Acceptance criteria:** for every goal and every scenario:
   - automatable: at least one `AC-` with Given/When/Then, an oracle, a threshold when there is a number, a layer, an evidence kind, and its fixtures, environment and data. One criterion may cover several goals or scenarios, and a goal may need several criteria.
   - not automatable: an operator check (next step). Never a vacuous test that passes to claim coverage.
   - an oracle you cannot derive: an `Open question:` naming the one decision Dan must make.
   State behaviour, not design: no internal files, algorithms, database, routes or mocks unless the contract says so (see GOALS-FORMAT.md).
7. **Operator checks:** each `OP-` with instructions, the expected observation and who signs it off.
8. **Non-goals and invariants:** what the project will not do, and the hard invariants (`INV-`) that must never regress, each with the criterion that checks it. A hard invariant is never traded.
9. **Trade-offs:** only bounded degradations Dan approves (`TO-`), each with its decision, bound and expiry. `None.` is a fine answer.
10. **Priority:** the order of the goals. It is an input to your recommendation when goals conflict, never a resolution.
11. **Authority:** what agents decide alone, what they ask Dan, how many retries, when to stop a failed approach, what counts as a blocker, and the channel.
12. **Approval:** resolve every conflict first (see [Conflicts while grilling](#conflicts-while-grilling)). Show a short "done means" digest: each goal's target and the criteria and checks that prove it. Dan approves the contract and its scenarios together; write `Contract revision: 1` and a change-history row with Dan in `Approved by`. Only then go on to the design.

Write `docs/GOALS.md` in the format in [GOALS-FORMAT.md](./GOALS-FORMAT.md), including its Fields table, as the steps settle. If the project uses Maestro, run `maestro goals check docs/GOALS.md` and resolve everything it lists; `--json` marks the problems only Dan can answer (`"ask": true`).

### When `docs/GOALS.md` already exists, or does not

- **no `docs/GOALS.md`:** walk the steps above.
- **format 1** (no `Format: 2` line; the six old sections): when Maestro is present, run `maestro goals migrate docs/GOALS.md`. It prints a format-2 skeleton that keeps every id and text. Fill only its `Open question:` lines with Dan, then take it through Approval. Without Maestro, apply the v1 → v2 rules in GOALS-FORMAT.md by hand.
- **format 2:** read it and challenge it rather than re-asking it: is each target still measurable, still wanted, still ranked right? Of an entry's fields, only these may change at the same revision: its name, `Rationale`, `Kind`, `Layer`, `Evidence`, `Fixtures`, `Environment`, `Data`, `Test`, `Sign-off` and `Decision`. Any other change to a goal, scenario, criterion, operator check, invariant or trade-off is a new revision (a Given/When/Then or a link as much as a target): bump `Contract revision` and add a change-history row Dan approves. Keep ids stable: a new entry takes the next unused id, and a dropped id goes under `Retired ids`, never reused.

When a later design question conflicts with a goal, a non-goal or an invariant, call it out immediately, as you would a glossary conflict.

### Conflicts while grilling

Before Approval, check every pair of goals, criteria, budgets and invariants for a conflict that is incompatible by definition: two thresholds on one measurement that cannot both pass, or a trade-off that names a hard invariant. Raise only credible conflicts, never conjectural ones. With Maestro, `maestro goals check` reports both as gate problems ("threshold conflict", "invariant conflict"); resolve them here.

Ask Dan in five parts:

1. The conflicting ids and the contract revision.
2. The evidence, or your estimate and how sure you are of it.
3. The consequences of keeping one goal, of keeping the other, and of a bounded compromise.
4. Your recommendation, with its rationale.
5. What he can answer: approve, decline, approve-with-notes, or choose an option.

Record his answer as a bounded `TO-` line with its decision plus a change-history row, or as a revised goal or criterion. Priority never decides, and a hard invariant is never traded: only Dan changes one, by revising the invariant itself.

## During the session

### Challenge the starting plan

Treat the starting plan, `PROJECT-BRIEF.md`, or proposed direction as a hypothesis to test rather than established truth. Resolve foundational decisions before decisions that depend on them.

When a branch depends on an unresolved earlier choice, resolve the earlier choice first instead of discussing downstream details prematurely.

### Challenge against the glossary

When I use a term that conflicts with the existing language in `CONTEXT.md`, call it out immediately. For example: "Your glossary defines 'cancellation' as X, but you seem to mean Y — which is it?"

### Sharpen fuzzy language

When I use vague or overloaded terms, propose a precise canonical term. For example: "You're saying 'account' — do you mean the Customer or the User? Those are different things."

### Discuss concrete scenarios

When domain relationships are being discussed, stress-test them with specific scenarios. Invent scenarios that probe edge cases and force precision about boundaries, failure modes, state transitions, ownership, and invariants.

### Cross-reference with code

When I state how something works, check whether the code agrees. If you find a contradiction, surface it explicitly and resolve which behavior is authoritative before continuing.

### Update CONTEXT.md inline

When a canonical domain term is resolved, update the relevant `CONTEXT.md` immediately rather than batching glossary changes until the end. Use the format in [CONTEXT-FORMAT.md](./CONTEXT-FORMAT.md).

`CONTEXT.md` must remain devoid of implementation details. Do not use it as a specification, scratch pad, or repository for implementation decisions. It is a glossary and nothing else.

### Offer ADRs sparingly

Only offer to create an ADR when all three are true:

1. **Hard to reverse** — the cost of changing the decision later is meaningful.
2. **Surprising without context** — a future reader will wonder why the system was built this way.
3. **The result of a real trade-off** — genuine alternatives existed and this option was chosen for specific reasons.

If any of the three is missing, skip the ADR. Use the format in [ADR-FORMAT.md](./ADR-FORMAT.md).

For an existing project, do not rewrite an old ADR to make a new decision look historical. If a durable decision changes, create a new ADR and mark the old one superseded when appropriate.

## Closing the session

Before finishing, `docs/GOALS.md` must be an approved contract with no gate problem except the named, deferred open questions (each holds only its ids): a `Contract revision` with Dan's approval row, every goal and scenario with a criterion or an operator check, and every field the Fields table requires. If an open question is deferred, name each one and the ids it holds instead of inventing an answer.

`docs/GOALS.md` says what done means. `CONTEXT.md` and ADRs record what things are called and why durable decisions were made. None of them says what to build. Once the grilling has settled enough of the design that concrete remaining work is visible, create or update `docs/PLAN.md` before finishing.

Use the format in [PLAN-FORMAT.md](./PLAN-FORMAT.md).

### If `docs/PLAN.md` does not exist

Create it with only the remaining/current work established by this grilling session. Do not reconstruct already-completed work merely to create project history.

### If `docs/PLAN.md` already exists

Preserve the existing file contents and add the newly agreed plan above them.

- Do not rewrite, delete, reorder, or summarize the previous plan history merely to make the file cleaner.
- Do not renumber existing phase IDs.
- New phase IDs must not collide with any IDs already present in the file.
- The newest plan is therefore always at the top; older plans remain below it as historical context.

Four parts of every new plan matter more than the rest because they are what downstream execution depends on:

1. **Every build phase gets a stable `id` and explicit `Depends on`.** Name predecessor IDs rather than leaving ordering implicit.
2. **Every phase gets an observable `Done when`.** Use a command that passes, a file that exists, a measurable state, or a behavior that can be verified; include the invariant IDs the phase is responsible for.
3. **Every phase names the goals it serves** (`Serves`: goal ids from `docs/GOALS.md`, or `—` for polish and housekeeping that serves none), the criteria it moves (`Criteria`: `AC-`/`OP-` ids) and how it contributes (`Contribution`: `direct`, `enabling` or `maintenance`). A goal no phase serves is either not planned yet or not a goal: say which.
4. **Ask, as the last question of the session: which of these phases can only you do?** Mark those `operator`. Interactive login, credentials only the user holds, privileged installs, purchases, physical access, and external-console decisions are typical examples.

Skip creating or updating the plan only when the session genuinely did not reach buildable work. If the grill ends with unresolved foundational questions, say so rather than inventing phases.

</supporting-info>
