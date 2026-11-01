# How planning works in Maestro, from goals to "done"

Behaviour and decisions only, no implementation detail. Status as of 2026-10-17. **Decided** = your decision or the spec. **Open** = still needs your answer. **Later** = specced but not built yet.

---

## 1. The chain at a glance

```
grill (skill or ChatGPT)  →  docs/GOALS.md + docs/PLAN.md
        ↓
maestro-setup             →  checks GOALS.md, asks only for what is missing
                             turns PLAN phases into ROADMAP tasks, each with serves: [G…]
        ↓
each task runs            →  every agent sees the goals excerpt
        ↓
proof review              →  per goal: advanced / no_progress / regressed
        ↓
rules decide              →  rework, ledger row, or (open) ask you
        ↓
end of project (later)    →  polish stage (W7) → goal check (W11b) → done or new proposals
        ↓
improvement (later, W12)  →  analyst reads the ledger and proposes fixes for you to approve
```

---

## 2. Goals are written first

**Decided (B16, W11a).**

- Goals are the **first branch of every grill**, before any design question. The design is judged against them.
- The grill writes `docs/GOALS.md` with six parts:
  1. **Purpose**: two sentences at most.
  2. **Users**: who uses the project and what each does with it.
  3. **Goals**: 3 to 7, each with a stable id (`G1`, `G2`, …), a **measurable target**, and **how it is verified**: a command that passes, something only you can observe, or a metric with a threshold.
  4. **Final product**: a walk-through of what you see and do once the project is done.
  5. **Non-goals**: what it deliberately won't do.
  6. **Priority**: which goal wins when two conflict.
- Goal ids never change and are never reused, because tasks and review records cite them.
- **The contract is the file format, not the skill.** You can run the grill in ChatGPT web from a paste-ready version. Setup accepts the file either way, lists exactly what is missing, and asks you only for that.
- Every PLAN phase names the goals it serves. A phase that serves none (polish, housekeeping) says so explicitly.

## 3. Setup turns the plan into tasks

**Decided.**

- maestro-setup reads `GOALS.md`. If the file is missing, setup asks the same goal questions itself. `PROJECT.md` points to it.
- Each ROADMAP task carries `serves: [G1, G3]`, inherited from its phase:
  - `serves: [G…]`: the task is meant to move those goals.
  - `serves: []`: it deliberately serves none (polish, housekeeping), and the goal check is skipped.
  - no `serves` key: it hasn't said. That gets a warning, but nothing blocks.
- `maestro doctor` **warns, never fails**, on a task with no `serves`, a goal that no open or finished task serves, an unknown goal id, or a malformed GOALS.md.

## 4. Every agent sees the goals

**Decided.**

- The planner, implementer, reviewer, diagnoser and triage (later) all get a **short goals excerpt** in their brief: the purpose, the served goals in full, the other goals as one line each, the priority order and the non-goals. The excerpt has a size cap, and anything cut is named.
- **The D23 bound: a goal never widens a task's scope.** The goals explain *why* the task exists. They are never a reason to build more than the task asks.

## 5. The review judges goal progress; rules decide the outcome

**Decided (your 2026-10-10 rules).** For each goal the task serves, the reviewer records what it observed, with one line of evidence from the diff. **The reviewer reports; the rules decide:**

| Reviewer says | What happens | Why |
|---|---|---|
| **advanced** | Nothing extra. | The task did its job. |
| **regressed** (any goal, served or not) | A blocking finding. The implementer reworks it like any other failed review. | No change may make a goal worse. |
| **no_progress** on a served goal, criteria pass | **Merged as is.** No rework, no hold. One ledger row: `task_did_not_serve_goal`, **fault: planner**. | The implementer built what was asked. Reworking would spiral. The miss is in how the task was defined. |
| task has `serves: []` | No check at all. | Polish and housekeeping serve no goal on purpose. |

Unknown goal ids or missing entries are flagged for later analysis, never failed.

## 6. Repeated no_progress on one goal (open: Q1)

**Specced:** "Two such tasks in a row for one goal send Dan one question: re-plan that goal's tasks, or keep going."

**How the count works** (what I'll build unless you change it): one count per goal. Only tasks that serve that goal affect it. `advanced` resets it to zero, `no_progress` adds one, and tasks for other goals leave it alone. At 2 you get one question for that goal, once. A goal that no task serves is a doctor warning, never this question.

**Still open, and we're discussing it now:**

- **(a) Drop the ask.** Only the ledger rows are recorded. The end-of-project goal check (W11b) and the analyst (W12) surface the pattern later. Simplest, but the planner can keep missing all the way to the end.
- **(b) Keep the ask, framed as "the plan for G1 isn't landing".**
  - **Re-plan:** that goal's not-started tasks are put on hold and listed to you. Other goals keep running. You re-plan them and release the holds.
  - **Keep going:** nothing changes, and the count restarts.
- (c) Re-plan queues a planner task. This overlaps W12, and I don't recommend it now.

My recommendation is (b). Your call.

## 7. End of project (later: W7, W11b)

- **Polish stage (W7):** non-blocking review findings collect in a backlog instead of spawning a follow-up task each. When the main tasks are accepted, a triage step merges duplicates, declines moot items with a reason, and runs polish tasks grouped by area. Polish follows the same rule: do or decline each item, no new scope.
- **Goal check (W11b):** after polish and **before the project counts as done**, each goal's target is verified. Commands are run by the gate, and observations come to you as an operator check. **An unmet goal blocks "done"** and becomes proposed tasks. When the queue runs out, proposals are ranked by unmet goals.

## 8. Planning gets better over time (later: W12)

- An analyst (strong model), weekly and at project end, reads the fault ledger, including the planner's `task_did_not_serve_goal` rows, and the per-route statistics.
- It proposes ranked fixes, each with evidence, the expected saving, and how the effect will be measured. A fix can be a planner rule, a role prompt, a stronger model or effort, a graph-order change, an added gate or human step, or extra context or skills for a role.
- You approve or decline each one on Telegram. The next digest reports the measured effect. A declined proposal is not re-sent without new evidence.

## 9. Standing rules that apply throughout

- **Spec gaps go to you.** When a role hits an undefined case, it asks you on Telegram with options and a recommendation. The answer is recorded as a decision and fed into later briefs.
- **Who is at fault is decided by rule, never taken from the agent.** Ledger faults come from fixed cause-to-fault mappings.
- **Telemetry never changes a verdict.** The ledger records; it doesn't decide.
- **Small gaps get fixed, big ones get recorded, only real uncertainty comes to you.**

---

### What is built right now (W11a, in progress)

Built and tested:
- GOALS.md parsing and `maestro goals check`
- `serves` on tasks and the doctor warnings
- the goals excerpt in every brief (graph and legacy)
- the review rules: regressed reworks, no_progress merges and writes the ledger row, `serves: []` is skipped

In progress: the grill-me and maestro-setup skill changes, and the ChatGPT paste-ready version.

Waiting on Q1: the repeated-no_progress question (section 6).

Not in W11a: W7, W11b, W12.
