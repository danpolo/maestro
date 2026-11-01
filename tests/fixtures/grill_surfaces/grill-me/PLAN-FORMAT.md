# PLAN.md Format

`docs/PLAN.md` is the agent-facing build plan: what remains to build, in what order, and how you know a piece is done. `CONTEXT.md` says what things are called and `docs/adr/` says why durable design decisions were made — this file is the execution surface.

Write or update it lazily, only once the grilling has settled enough of the design that real remaining work is visible. A plan written before the decisions are made is fiction.

## Creating vs updating

### No existing `docs/PLAN.md`

Create the file with only the remaining/current work established by the grilling session.

Do not reconstruct already-completed work merely to manufacture project history.

### Existing `docs/PLAN.md`

Preserve the existing file contents and prepend the new plan above them.

The old content is historical context and should remain intact unless the user explicitly asks to clean it up or correct it.

Rules:

- Do not delete, rewrite, reorder, or summarize existing plan history merely for neatness.
- Do not renumber existing phase IDs.
- Scan the entire existing file before assigning new phase IDs; new IDs must not collide with historical IDs.
- The newest plan block is always at the top of the file; older plan blocks remain below it.
- Only the newly prepended block needs to describe the remaining/current work from this grilling session.

## Structure of each new plan block

```md
# {Project} — Implementation Plan (agent-facing)

**Read first, in this order:** `docs/GOALS.md` for why the project exists and what done means,
`CONTEXT.md` for the canonical vocabulary (use these terms in code and commits), then
`docs/adr/` for the durable decisions and their reasoning. This plan block says
*what to build now*; the ADRs say *why the design is shaped this way*. If this block and an ADR
disagree, the ADR wins and this block is stale.

## 1. Invariants — do not break these

| # | Invariant | Enforced where |
|---|---|---|
| I1 | {A property that must hold after every operation.} | {module} |

## 2. Module map

{One line per module involved in this work: its single responsibility, and what it is forbidden to do.}

## 3. Build phases

{The table below. This is the section downstream tooling reads.}

## 4. Out of scope

{Deliberately excluded from this work, with the reason. "Do not add these without a new ADR" when appropriate.}
```

Sections 1–2 and 4 are the spine. Add data-model, algorithm, interface, migration, rollout, or operational sections when the design contains detail that would otherwise be re-invented differently by each implementer.

For a narrow change to an existing project, keep these sections proportional to the work. Do not restate the entire architecture when only one subsystem is changing.

## Build phases — the contracted part

Phases are the handoff surface. Anything that picks this plan up — a person, coding agent, or orchestrator that turns phases into a task queue — should be able to execute from this table without re-deriving ordering or completion criteria.

```md
| id | Phase | Depends on | Serves | Criteria | Contribution | Done when | Who |
|---|---|---|---|---|---|---|---|
| `11-harness` | Acceptance harness + sample posts | — | G1 | AC-1 | enabling | `python bench/import_ratio.py` runs and reports a ratio (RED is fine) | agent |
| `12-collector` | Collector + schema | `11-harness` | G1 | AC-1, AC-2 | direct | AC-1 and AC-2 pass; running it twice adds zero rows (I8) | agent |
| `18-timers` | systemd timers + notify | `12-collector` | G1, G3 | AC-1 | G1: direct, G3: enabling | Alert fires on an induced failure | operator |
| `19-tidy` | Remove the old importer | `12-collector` | — | — | maintenance | `old_importer/` is gone and the suite passes | agent |
```

- **`id` is stable and never renumbered.** Use `NN-slug` when possible. Other documents, commits, and task queues may reference the id, so changing it silently breaks those references. If the natural numeric slot is already taken, use a non-colliding suffix such as `12b-…` rather than renumbering historical phases.
- **When updating an existing plan, IDs are globally unique across the whole file.** Scan historical plan blocks before choosing new IDs.
- **`Depends on` is explicit and lists ids.** Do not write ordering only as prose and leave readers to infer dependency edges. Write `—` for a phase with no predecessor and name every predecessor even when it is simply the previous phase. Where true execution order differs from numeric order, the dependency column is authoritative; explain unusual ordering in a note under the table.
- **`Serves` names goal ids from `docs/GOALS.md`** that the phase is meant to move. Write `—` for polish or housekeeping that serves no goal. An orchestrator carries this into each task (`serves: [G1]`, or `serves: []` for `—`) and checks at review whether the goal moved. Every goal should be served by at least one phase.
- **A phase's `Serves` is a suggestion for its tasks, not proof that each task moves the goal.** Each task states its own `serves` and contribution when the plan becomes tasks; a fixture or a harness inside a `direct` phase is still `enabling`.
- **`Criteria` names the `AC-`/`OP-` ids from `docs/GOALS.md` the phase should turn from RED to GREEN** (or that it prepares, for an enabling phase). Write `—` when it moves none.
- **`Contribution` is `direct`, `enabling` or `maintenance`.** `direct` moves the goal's criteria; `enabling` is a verifiable prerequisite (a harness, fixtures, a benchmark) that moves none by itself; `maintenance` keeps reliability or quality and serves no goal. When a phase serves several goals differently, say it per goal (`G1: direct, G3: enabling`). Never assume `direct`.
- **`Done when` is observable.** Use a command that passes, a file that exists, a number that changes, or behavior someone can check. "Scoring is implemented" is not a completion criterion; "the four cases in §4.1 pass exactly" is. Name the invariant IDs a phase is responsible for when applicable.
- **`Who` is `agent` or `operator`.** See below.

Prefer phases that each end in something observable. If the plan deliberately begins somewhere other than the numerically earliest phase, state that plainly and explain why.

## Existing completed work

When creating a new `docs/PLAN.md`, omit work that is already complete.

When `docs/PLAN.md` already exists, do not duplicate completed historical phases into the new top block. They remain available in the preserved older blocks below. Reference a historical phase by its stable id when the new work genuinely depends on it.

## Closing question: who has to do it

Before the new plan block is finished, ask the user one question directly:

> Which of these phases can only you do?

Mark those `operator` in the `Who` column and say why in a note when useful — an interactive login or OAuth consent, a credential only they hold, a privileged install (`sudo`, a system service), a purchase, physical access, or a decision in an external console.

This is worth asking explicitly because nothing in the implementation plan reliably reveals it. A coding agent may otherwise attempt an impossible operator-only phase and fail unnecessarily.
