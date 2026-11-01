---
name: grill-me
description: Grill a project plan or design until its key decisions are resolved, updating project docs as decisions crystallize. use when the user asks to be grilled on a project
---

<what-to-do>

Interview the user relentlessly about every important aspect of the project until the design is sufficiently resolved to hand to an implementation agent. Start with the product contract (`docs/GOALS.md`): settle and approve it before any design question. Then walk the decision tree in dependency order: resolve foundational choices before downstream ones. Ask one question at a time and, for every question, give a recommended answer with concise reasoning.

Do not reduce the depth of the grill because this session is running in ChatGPT rather than on the development machine. Use available conversation context, artifacts, connected sources, and GitHub evidence. When required evidence cannot be obtained from ChatGPT, record an explicit evidence request instead of guessing, weakening the question, or silently skipping the branch.

Do not implement the project during the grill. The outputs are an approved product contract, resolved decisions, durable project documentation, and an implementation-ready plan.

</what-to-do>

<supporting-info>

## Choose the mode

Infer the mode from the conversation and available sources. Do not ask which mode applies when it is evident.

### Mode A — initial project setup

Use when meaningful implementation does not yet exist.

The starting context may be either:

- an attached idea/brief/artifact, or
- the conversation itself when the user has already been discussing and refining the idea in ChatGPT.

Do not make the user restate information already present in the current conversation.

At the start of the grill, create `PROJECT-BRIEF.md` from the available idea context if one does not already exist. Use [PROJECT-BRIEF-FORMAT.md](./PROJECT-BRIEF-FORMAT.md). Treat it as a faithful seed snapshot of the project intent, constraints, and already-made choices — not as authoritative design. Challenge its assumptions during the grill.

There is no requirement to inspect or run local code in this mode.

### Mode B — existing project on GitHub

Use only when the whole project needed for the design discussion is available through GitHub.

Before asking design questions, inspect the GitHub repository using the GitHub connection. Read enough of the repository to understand the current state, including the relevant README/docs, `PROJECT-BRIEF.md` if present, `docs/GOALS.md`, `CONTEXT.md` or `CONTEXT-MAP.md`, ADRs, `docs/PLAN.md`, configuration, tests, workflows, and the implementation relevant to the proposed direction.

Use the repository's default branch unless the user identifies another branch. Record the branch and commit/revision used for the grill so returned runtime evidence can be matched to the same code state.

If a question can be answered from GitHub, answer it from GitHub instead of asking the user. Treat code and existing documentation as evidence. Surface contradictions between the proposed direction, documentation, and implementation explicitly.

Do not modify repository code during the grill. Repository writes are outside this skill unless the user separately asks for them after the grill.

## Durable working documentation

Important decisions must be persisted as they crystallize, not reconstructed only at the end.

### Preferred persistence: Google Drive / Google Docs

When a writable Google Drive connection is available, create or reuse one working folder for the grilling session and maintain native Google Docs whose names mirror the final Markdown artifacts.

Persist updates immediately when they become durable:

- `docs/GOALS.md` — the first doc created: write it as the contract steps settle, in [GOALS-FORMAT.md](./GOALS-FORMAT.md). After approval, change it only through a new contract revision.
- `PROJECT-BRIEF.md` — create once near the start; update only to correct a factual mistake or explicitly revised project intent. Do not use it as a scratchpad.
- `CONTEXT.md` / context docs — update as soon as canonical terminology is resolved.
- ADR documents — create as soon as a decision qualifies for an ADR.
- `docs/PLAN.md` — create/update only once enough design has settled that real remaining work is visible; refine the current block as later decisions affect it.
- `AGENT-DATA-REQUEST.md` — create/update as soon as an unresolved branch requires evidence ChatGPT cannot obtain itself.

Use Google Docs as durable working state, not as a replacement for the final Markdown handoff. At completion, produce the final project files in the exact Markdown structure described below.

If writable Drive/Docs is unavailable, continue the grill without reducing quality. Maintain an explicit decision/evidence ledger in the conversation and regenerate the current artifacts at meaningful checkpoints. Never claim an external document was updated when it was not.

## Required evidence that ChatGPT cannot collect

ChatGPT may lack evidence that requires local execution, private runtime state, secrets, hardware, a development database, a local-only file, or another capability unavailable to the current session.

Do not guess around such gaps.

When a design branch depends on unavailable evidence:

1. State the dependency clearly.
2. Add a uniquely identified item to `AGENT-DATA-REQUEST.md` using [AGENT-DATA-REQUEST-FORMAT.md](./AGENT-DATA-REQUEST-FORMAT.md).
3. Specify exactly what evidence is needed and, when useful, the commands/checks an agent should run to collect it.
4. Do not ask the collection agent to make the design decision. The agent's job is evidence collection only.
5. Continue grilling every independent branch that does not depend on that evidence.
6. When all useful remaining branches are blocked by missing evidence, pause the unresolved branches and hand the user `AGENT-DATA-REQUEST.md`.
7. When the user returns with the collected report, verify that it answers the request IDs and resume the unresolved branches from that evidence rather than restarting the grill.

A goal baseline that only the machine can measure (a run time, a count in a database, a current pass rate) is such a gap: make it an `AGENT-DATA-REQUEST.md` item, never a guess, and leave the baseline open until the report returns.

A missing-data handoff is not a lower-quality conclusion. The grilling session is incomplete until evidence-dependent decisions are resolved.

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
12. **Approval:** resolve every conflict first (see Conflicts while grilling). Show a short "done means" digest: each goal's target and the criteria and checks that prove it. Dan approves the contract and its scenarios together; write `Contract revision: 1` and a change-history row with Dan in `Approved by`. Only then go on to the design.

Write `docs/GOALS.md` in [GOALS-FORMAT.md](./GOALS-FORMAT.md). ChatGPT has no `maestro goals check`, so check the file against the Fields table in GOALS-FORMAT.md yourself before Approval: every `gate` and `draft` field present, or an `Open question:` in its place.

### When `docs/GOALS.md` already exists, or does not

- **no `docs/GOALS.md`:** walk the steps above.
- **format 1** (no `Format: 2` line; the six old sections): apply the v1 → v2 rules in GOALS-FORMAT.md. They keep every id and text; fill only the `Open question:` lines they leave with Dan, then take the contract through Approval.
- **format 2:** read it and challenge it rather than re-asking it: is each target still measurable, still wanted, still ranked right? Of an entry's fields, only these may change at the same revision: its name, `Rationale`, `Kind`, `Layer`, `Evidence`, `Fixtures`, `Environment`, `Data`, `Test`, `Sign-off` and `Decision`. Any other change to a goal, scenario, criterion, operator check, invariant or trade-off is a new revision (a Given/When/Then or a link as much as a target): bump `Contract revision` and add a change-history row Dan approves. Keep ids stable: a new entry takes the next unused id, and a dropped id goes under `Retired ids`, never reused.

When a later design question conflicts with a goal, a non-goal or an invariant, call it out immediately, as you would a glossary conflict.

### Conflicts while grilling

Before Approval, check every pair of goals, criteria, budgets and invariants for a conflict that is incompatible by definition: two thresholds on one measurement that cannot both pass, or a trade-off that names a hard invariant. Raise only credible conflicts, never conjectural ones.

Ask Dan in five parts:

1. The conflicting ids and the contract revision.
2. The evidence, or your estimate and how sure you are of it.
3. The consequences of keeping one goal, of keeping the other, and of a bounded compromise.
4. Your recommendation, with its rationale.
5. What he can answer: approve, decline, approve-with-notes, or choose an option.

Record his answer as a bounded `TO-` line with its decision plus a change-history row, or as a revised goal or criterion. Priority never decides, and a hard invariant is never traded: only Dan changes one, by revising the invariant itself.

## Domain awareness and project documentation

The final repository documentation uses this structure when applicable:

```text
/
├── PROJECT-BRIEF.md
├── CONTEXT.md                         # or CONTEXT-MAP.md + context-specific CONTEXT.md files
├── docs/
│   ├── GOALS.md                       # the product contract, written first
│   ├── PLAN.md
│   └── adr/
│       ├── 0001-example-decision.md
│       └── ...
└── AGENT-DATA-REQUEST.md              # only while unresolved external evidence is required
```

Create documentation lazily. Do not create empty ADR directories, context maps, or filler documents merely to satisfy the tree.

### Challenge the starting idea

Treat the conversation, `PROJECT-BRIEF.md`, and proposed direction as hypotheses to test rather than established truth. Resolve foundational decisions before choices that depend on them.

### Ask one question at a time

Each question should resolve one meaningful branch. Include the recommended answer in the same turn. Do not dump a questionnaire or ask several independent questions at once. The exception is the contract's drafted parts, which you confirm in one batch.

If the user answers one question in a way that resolves several dependent questions, accept that and move forward rather than mechanically asking them anyway.

### Sharpen terminology

When the user uses vague or overloaded domain language, propose a precise canonical term. When an existing `CONTEXT.md` defines the term differently, surface the conflict immediately.

When a canonical domain term is resolved, update the relevant context document immediately. Use [CONTEXT-FORMAT.md](./CONTEXT-FORMAT.md).

`CONTEXT.md` is a glossary, not a specification or scratchpad. Keep implementation details out of it.

### Stress-test with concrete scenarios

Probe boundaries, failure modes, state transitions, ownership, invariants, data lifecycle, concurrency, recovery, security assumptions, and operational behavior where relevant. Use concrete scenarios rather than abstract wording when they expose ambiguity.

### Offer ADRs sparingly

Create an ADR only when all three are true:

1. **Hard to reverse** — changing the decision later has meaningful cost.
2. **Surprising without context** — a future reader could reasonably wonder why the system was built this way.
3. **Real trade-off** — genuine alternatives existed and this option was chosen for specific reasons.

Use [ADR-FORMAT.md](./ADR-FORMAT.md).

For an existing project, do not rewrite history. If a durable decision changes, create a new ADR and supersede the old one when appropriate.

## Planning during the grill

`docs/PLAN.md` is the project-level execution contract, not a transcript of the discussion.

Use [PLAN-FORMAT.md](./PLAN-FORMAT.md).

- If no `docs/PLAN.md` exists, create it with only remaining/current work.
- If one already exists, preserve everything already there and prepend the newly agreed current-work block above it.
- Never renumber historical phase IDs.
- Every new phase must have a stable ID, explicit dependencies, the goals it serves, the criteria it moves and how it contributes, observable completion criteria, and `agent` or `operator` ownership.

Do not create a detailed low-level coding plan merely to fill space. The downstream implementation agent may expand a phase when implementation detail is actually needed.

## Closing the session

The grill is complete only when `docs/GOALS.md` is an approved contract with no gate problem except the named, deferred open questions (each holds only its ids), checked against its Fields table, and the important design branches are resolved or explicitly blocked on evidence that has been handed off. Name every deferred `Open question:` and the ids it holds instead of inventing an answer.

Before finalizing a buildable plan, ask the user as the last design question:

> Which of these phases can only you do?

Mark those phases `operator` and the rest `agent`.

At the final handoff, provide the current repository-ready Markdown artifacts:

- `docs/GOALS.md`
- `PROJECT-BRIEF.md`
- the applicable `CONTEXT.md` / `CONTEXT-MAP.md` files
- all ADRs created by the session
- `docs/PLAN.md`
- `AGENT-DATA-REQUEST.md` only if unresolved evidence remains

Do not put working notes, abandoned alternatives, or conversation transcript into the final files unless they are intentionally preserved by one of those document types.

If `AGENT-DATA-REQUEST.md` is non-empty, explicitly state that the grill is awaiting evidence and which decision branches remain open. When the evidence returns, continue the same session and regenerate the affected final artifacts.

</supporting-info>
