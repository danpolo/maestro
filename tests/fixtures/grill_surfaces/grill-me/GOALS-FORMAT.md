# GOALS.md Format

`docs/GOALS.md` is the product contract: why the project exists, what the finished product must do, and how anyone will know it does. `CONTEXT.md` says what things are called, `docs/adr/` says why the design is shaped this way, and `docs/PLAN.md` says what to build next. None of them says what "done" means for the user. This file does.

Downstream tools depend on it: an orchestrator shows an excerpt to every agent that plans, builds, reviews or diagnoses a task, every plan phase and task names the goals and criteria it serves, setup turns each acceptance criterion into a runnable test before any code is written, and a final gate verifies every mandatory criterion, scenario and operator check before "project done". So the format is a contract. It is written by this skill, by ChatGPT from the Drive grill (`ChatGPT/grill-me-framework`) or by Maestro's setup; the three carry the same steps, fields and rules, and either style below parses the same.

## Structure

```md
# Goals: {Project}

Format: 2
Contract revision: 1 (approved {date} by Dan)

## Purpose

{At most two sentences: what the project is, and why it exists.}

## Users

- **{Who}:** {what they do with it}

## Goals

### G1: {Short name}

- **Outcome:** {what the user gets, in plain words}
- **Rationale:** {why it matters}
- **Baseline:** {the value measured today, or not_applicable}
- **Target:** {a measurable result; never invented: an Open question instead}
- **Verified by:** AC-1, OP-1
- **Constraints:** INV-1
- **Depends on:** G2

## Final product

{A walk-through: what the user sees and does once the project is done.}

### S-1: {Short name}

- **Goals:** G1
- **Kind:** normal | error | boundary
- **Given:** {the situation}
- **When:** {what the user does or what happens}
- **Then:** {what the user observes}
- **Criteria:** AC-1
- **Operator checks:** OP-1

## Acceptance criteria

### AC-1: {Short name}

- **Goals:** G1
- **Scenarios:** S-1
- **Given:** {inputs and preconditions}
- **When:** {the action}
- **Then:** {the observable result}
- **Oracle:** {the success condition anyone can check}
- **Threshold:** {the number, when there is one}
- **Layer:** e2e | integration | unit
- **Evidence:** test | metric | trace
- **Fixtures:** {safe, deterministic test data}
- **Environment:** {what the test needs to run}
- **Data:** {where the data comes from}
- **Test:** `{command}` (setup binds it; leave it out while grilling)
- **Mandatory:** no (only for an optional criterion)

## Operator checks

### OP-1: {Short name}

- **Goals:** G1
- **Scenarios:** S-1
- **Instructions:** {what Dan does}
- **Expected:** {what he should observe}
- **Sign-off:** Dan

## Non-goals

- {Something the project deliberately will not do, and why when it is not obvious.}

## Hard invariants

- INV-1: {a property that must never regress} (checked by AC-2)

## Trade-offs

- TO-1: {an approved, bounded degradation}; decision {ref}; bound {limit}; expires {condition}

## Priority

1. G1
2. G2

## Authority

- **Autonomous:** {what agents decide alone}
- **Ask Dan:** {what they must ask}
- **Retries:** {how many, then what}
- **Stop a failed approach when:** {the condition}
- **Blocker:** {what counts as a true blocker}
- **Channel:** telegram

## Change history

| Rev | Date | Change | Rationale | Impact | Approved by | Supersedes |
|---|---|---|---|---|---|---|
| 1 | {date} | first contract | grill | all | Dan | — |

## Retired ids

- G4 (rev 2): {why it was dropped}
```

`Operator checks`, `Hard invariants`, `Trade-offs` and `Retired ids` may say `None.`. Every other section has content.

## Fields

What each field's absence does at worst. `gate`: a gate problem that holds the work it names until it is fixed. `draft`: a draft problem, asked while drafting. `no`: nothing (at most a note). A section counts with its entries, so a section whose ids other entries name gates through those links. This table is generated from Maestro's `goals.CONTRACT_FIELDS`, and every grill surface carries the same one.

| Part | Field | Required | Default |
|---|---|---|---|
| Contract | Format | draft | none: read as format 1, so nothing verifies it at the end |
| Contract | Contract revision | gate | none: Dan approves the contract |
| Section | Purpose | draft | none |
| Section | Users | draft | none |
| Section | Goals | gate | none: every link to a goal gates |
| Section | Final product | gate | none: a contract with no scenario gates |
| Section | Acceptance criteria | gate | none: every link to a criterion gates |
| Section | Operator checks | gate | None. is a complete answer, so write it when there are none; absent, it gates only when an entry names an OP- |
| Section | Non-goals | draft | none |
| Section | Hard invariants | gate | None. is a complete answer, so write it when there are none; absent, it is draft, or gate when an entry names an INV- |
| Section | Trade-offs | no | None. |
| Section | Priority | draft | none |
| Section | Authority | draft | none |
| Section | Change history | gate | none: the revision has no approval row |
| Section | Retired ids | no | None. |
| Goal | Outcome | no | the goal's name |
| Goal | Rationale | no | none |
| Goal | Baseline | draft | none (not_applicable when nothing is measured) |
| Goal | Target | gate | none: an open question for Dan |
| Goal | Verified by | draft | the criteria and checks naming the goal |
| Goal | Constraints | no | none |
| Goal | Depends on | no | none |
| Scenario | Goals | gate | none |
| Scenario | Kind | draft | none (normal, error or boundary) |
| Scenario | Given | draft | none |
| Scenario | When | draft | none |
| Scenario | Then | draft | none |
| Scenario | Criteria | draft | the criteria naming the scenario |
| Scenario | Operator checks | draft | the checks naming the scenario |
| Acceptance criterion | Goals | draft | the goals listing it |
| Acceptance criterion | Scenarios | draft | the scenarios listing it |
| Acceptance criterion | Given | draft | none |
| Acceptance criterion | When | draft | none |
| Acceptance criterion | Then | draft | none |
| Acceptance criterion | Oracle | gate | none: an open question for Dan |
| Acceptance criterion | Threshold | no | none |
| Acceptance criterion | Layer | draft | none (e2e, integration or unit) |
| Acceptance criterion | Evidence | draft | none (test, metric or trace) |
| Acceptance criterion | Fixtures | no | none |
| Acceptance criterion | Environment | no | none |
| Acceptance criterion | Data | no | none |
| Acceptance criterion | Test | no | none: setup binds it |
| Acceptance criterion | Mandatory | no | yes |
| Operator check | Goals | draft | the goals listing it |
| Operator check | Scenarios | draft | the scenarios listing it |
| Operator check | Instructions | gate | none |
| Operator check | Expected | gate | none |
| Operator check | Sign-off | draft | none |
| Operator check | Mandatory | no | yes |
| Hard invariant | Checked by | draft | none |
| Trade-off | Decision | draft | none |
| Trade-off | Bound | gate | none |
| Trade-off | Expires | no | none |
| Authority | Autonomous | draft | none |
| Authority | Ask Dan | draft | none |
| Authority | Retries | draft | none |
| Authority | Stop a failed approach when | draft | none |
| Authority | Blocker | draft | none |
| Authority | Channel | draft | none |
| Change history | Approved by | gate | none: Dan approves the revision |

Beyond single fields: every goal has at least one acceptance criterion or operator check, every scenario names its goals and has at least one criterion or check, every criterion names a goal or a scenario, and every id the file names exists. A missing one of these is a gate problem.

## Rules

- **These section names,** each a `##` heading: Purpose, Users, Goals, Final product, Acceptance criteria, Operator checks, Non-goals, Hard invariants, Trade-offs, Priority, Authority, Change history, Retired ids. Numbered headings such as `## 3. Goals` are fine.
- **Purpose is at most two sentences.** If it needs more, the project is not yet clear. Keep grilling.
- **3–7 top-level goals is the usual range, not a limit; split an omnibus goal into criteria, merge micro-goals.** A goal too big to verify in one place becomes one goal with several criteria; goals that only make sense together become one.
- **Ids are stable.** `G1`, `S-1`, `AC-1`, `OP-1`, `INV-1`, `TO-1`. Plans, tasks, tests and review records cite them, so never renumber one and never reuse a dropped one: it goes under Retired ids. A new entry takes the next unused number.
- **Every goal has a measurable target.** "Fast" is not a target. "A year of exports imports in under 10 seconds" is. A target nobody has given is an `Open question:` for Dan, never a default you chose.
- **Links go both ways.** A goal lists its `Verified by` criteria and checks, and each of those names the goal; a scenario lists its criteria and each names the scenario. Many-to-many is fine.
- **Scenarios come from the walk-through:** the normal flow, plus an error flow and a boundary flow where they matter.
- **Acceptance criteria are automatable checks** with Given/When/Then, an oracle, a threshold when there is a number, a layer, an evidence kind, and the fixtures, environment and data the test needs. Make them safe and deterministic: no live production data, no paid calls, no secrets.
- **Operator checks are for what only a person can verify:** instructions, the expected observation, and who signs it off. Never write a vacuous test that passes to claim coverage.
- **An oracle you cannot derive is an `Open question:`** naming the one decision Dan must make. Open questions hold only the ids they name.
- **Hard invariants are never traded.** Trade-offs are bounded, approved degradations with a decision, a bound and an expiry. Priority alone never approves one.
- **Priority ranks every goal.** It is an input to a recommendation when goals conflict, never a resolution: only Dan resolves a conflict.
- **Authority says all six things:** what agents decide alone, what they ask Dan, retries, when to stop a failed approach, what counts as a blocker, and the channel.
- **Every revision is approved.** `Contract revision: N` has a change-history row with Dan in Approved by. Of an entry's fields, only these may change at the same revision: its name, `Rationale`, `Kind`, `Layer`, `Evidence`, `Fixtures`, `Environment`, `Data`, `Test`, `Sign-off` and `Decision`. Any other change to a goal, scenario, criterion, operator check, invariant or trade-off is a new revision, never a silent edit: a target, threshold or oracle, and also a baseline, a Given/When/Then, a link between ids, `Mandatory`, or an invariant's or trade-off's text. Once Maestro has recorded the contract (at `maestro goals baseline`), a change at the same revision fails `maestro doctor`.
- **Goals are outcomes, not tasks.** "Add a search index" is a task. "A search by ingredient lists the right recipe in the top 3" is a goal. A goal never widens a task's scope. It explains why the task exists.

### Behaviour, not design

Criteria state what the user observes, never how it is built: no internal files, algorithms, database tables, routes or mocks unless the contract itself says so. A behavioural test survives any valid implementation; a design test locks one in and passes on code the user never sees.

**Good**: observable behaviour

```md
### AC-1: Saved posts are listed within an hour

- **Goals:** G1
- **Scenarios:** S-1
- **Given:** a saved post in the weekly sample of 40
- **When:** an hour passes
- **Then:** the recipe is listed in the cookbook
- **Oracle:** the share of the sample listed as recipes is at or above the threshold
- **Threshold:** 0.95
- **Layer:** e2e
- **Evidence:** metric
- **Test:** `python bench/import_ratio.py`
```

**Bad**: names the design

```md
### AC-1: Saved posts are listed within an hour

- **Goals:** G1
- **Scenarios:** S-1
- **Given:** a row in the posts table
- **When:** src/importer.py runs
- **Then:** insert_row() is called once per post
- **Oracle:** the mock of insert_row() saw 40 calls
- **Layer:** unit
- **Evidence:** test
```

## Format 1: the v1 → v2 rules

A file without a `Format: 2` line is format 1: the six old sections, each goal with a `Target:` and a `Verified by: command | observation | metric`. It still parses, but it has no scenarios, criteria or operator checks, so nothing verifies it at the end. `maestro goals migrate docs/GOALS.md` prints a format-2 skeleton by these rules (it never writes the file). Without Maestro, apply them by hand:

1. Add `Format: 2` and `Contract revision: 1` under the title.
2. Keep every goal id, name and target, and the Purpose, Users, Final product, Non-goals and Priority text, unchanged.
3. Move each goal's verification into the criteria: `command: X` becomes an `AC-` with `Test: X`, `Evidence: test` and the goal's target as its oracle; `metric: X` becomes an `AC-` with `Evidence: metric` and X as its oracle; `observation: X` becomes an `OP-` with X as its instructions, the target as its expected observation and Dan as sign-off. The goal's `Verified by` names the new id.
4. A goal with no target gets `Open question: the measurable target`.
5. Add the `Open question:` lines format 1 cannot answer: the scenarios the walk-through implies (under Final product), the hard invariants (or `None.`), and the authority policy.
6. Add the change-history row `| 1 | {date} | migrated from format 1 | contract format 2 | all | | format 1 file |`, with Approved by empty until Dan approves.

Then fill only those open questions with Dan and take the contract through Approval as revision 1.

## Worked example

The same contract ChatGPT writes from the Drive grill; Maestro parses both to the same thing.

```md
# Goals: Recipe Box

Format: 2
Contract revision: 2 (approved 2026-10-20 by Dan)

## Purpose

Recipe Box turns the recipes Dan saves from Instagram into a searchable cookbook on his
phone. It exists because saved posts are impossible to find again when he is cooking.

## Users

- **Dan:** saves posts during the week and cooks from them at the weekend.

## Goals

### G1: Every saved post lands in the cookbook

- **Outcome:** saved posts become recipes without Dan doing anything.
- **Rationale:** a post that never arrives is a recipe lost.
- **Baseline:** not_applicable
- **Target:** 95% of the posts saved in the last 7 days are listed as recipes within an hour.
- **Verified by:** AC-1, AC-2
- **Constraints:** INV-1

### G2: Usable in the kitchen

- **Outcome:** a recipe page reads on a phone from arm's length.
- **Rationale:** Dan cooks with flour on his hands.
- **Baseline:** not_applicable
- **Target:** Dan reads three recipes without zooming.
- **Verified by:** OP-1

### G3: Search finds the dish

- **Outcome:** a search by ingredient finds the recipe Dan means.
- **Rationale:** a cookbook nobody can search is the saved-posts list again.
- **Baseline:** 0 of 10 sample searches (no search exists, 2026-10-20).
- **Target:** 9 of 10 sample searches list the right recipe in the top 3.
- **Verified by:** AC-3, AC-4
- **Depends on:** G1

## Final product

Dan saves a post on Tuesday. On Saturday he opens Recipe Box, searches for the ingredient he has, opens the recipe and cooks from the page.

### S-1: A saved post becomes a recipe

- **Goals:** G1, G2
- **Kind:** normal
- **Given:** a post saved on Instagram
- **When:** an hour passes
- **Then:** the recipe is listed in the cookbook and reads on the phone
- **Criteria:** AC-1
- **Operator checks:** OP-1

### S-2: A post that is not a recipe

- **Goals:** G1
- **Kind:** error
- **Given:** a saved post with no recipe in it
- **When:** an hour passes
- **Then:** the post is listed under "not a recipe" and the cookbook is unchanged
- **Criteria:** AC-2

### S-3: A full cookbook

- **Goals:** G3
- **Kind:** boundary
- **Given:** a cookbook of 500 recipes
- **When:** Dan searches for an ingredient
- **Then:** the recipe he means is in the top 3
- **Criteria:** AC-3, AC-4

## Acceptance criteria

### AC-1: Saved posts are listed within an hour

- **Goals:** G1
- **Scenarios:** S-1
- **Given:** the weekly sample of 40 saved posts
- **When:** an hour passes after they are saved
- **Then:** the share of them listed as recipes is reported
- **Oracle:** the share is at or above the threshold
- **Threshold:** 0.95
- **Layer:** e2e
- **Evidence:** metric
- **Fixtures:** 40 anonymised saved posts with their expected recipes
- **Environment:** a local Instagram stub; no network
- **Data:** recorded posts only, never a live account
- **Test:** `python bench/import_ratio.py`

### AC-2: A post that is not a recipe is listed apart

- **Goals:** G1
- **Scenarios:** S-2
- **Given:** a saved post with no recipe in it
- **When:** an hour passes
- **Then:** the post is listed under "not a recipe" and no recipe is added
- **Oracle:** zero new recipes; the post is named in the "not a recipe" list
- **Layer:** e2e
- **Evidence:** test

### AC-3: Search lists the right recipe in the top 3

- **Goals:** G3
- **Scenarios:** S-3
- **Given:** a cookbook of 500 recipes and 10 sample searches with their answers
- **When:** each search runs
- **Then:** the rank of the right recipe is reported
- **Oracle:** the right recipe is in the top 3 for at least 9 of the 10 searches
- **Layer:** integration
- **Evidence:** test
- **Test:** `pytest tests/acceptance/test_search.py`

### AC-4: Search answers within a second

- **Goals:** G3
- **Scenarios:** S-3
- **Given:** a cookbook of 500 recipes on the reference phone
- **When:** Dan searches three times
- **Then:** the median time to the first result is measured
- **Oracle:** the median is below the threshold
- **Threshold:** 1 second
- **Layer:** e2e
- **Evidence:** metric
- **Mandatory:** no

## Operator checks

### OP-1: A recipe reads from arm's length

- **Goals:** G2
- **Scenarios:** S-1
- **Instructions:** open three recipes on the phone propped on the counter.
- **Expected:** each one reads without zooming.
- **Sign-off:** Dan
- **Mandatory:** yes

## Non-goals

- No meal planning or shopping lists.

## Hard invariants

- INV-1: no recipe is shown that Dan did not save (checked by AC-2)

## Trade-offs

- TO-1: a post may take up to two hours to arrive while the Instagram stub is replaced; decision D-3; bound 2 hours; expires when the new stub lands

## Priority

1. G1
2. G2
3. G3

## Authority

- **Autonomous:** implementation, tests, retries, task order within a phase.
- **Ask Dan:** new or changed goals, thresholds, any goal conflict, paid services.
- **Retries:** two, then a stronger diagnoser.
- **Stop a failed approach when:** two attempts miss the same criterion for the same cause.
- **Blocker:** a decision only Dan can make, or a missing Instagram login.
- **Channel:** telegram

## Change history

| Rev | Date | Change | Rationale | Impact | Approved by | Supersedes |
|---|---|---|---|---|---|---|
| 1 | 2026-10-18 | first contract | grill | all | Dan | — |
| 2 | 2026-10-20 | G4 meal planning retired | it is a non-goal now | G4 | Dan | rev 1 G4 |

## Retired ids

- G4 (rev 2): meal planning is a non-goal now.
```

G2 is verified only by an operator check: reading a page from arm's length is something only Dan can judge. AC-4 is optional (`Mandatory: no`), so the final gate reports it but never waits for it. AC-1 and AC-3 already have a bound `Test:`; AC-2 and AC-4 get theirs at setup.

## Validation

When the project uses Maestro, `maestro goals check docs/GOALS.md` lists every problem with its severity (`note`, `draft`, `gate`) and exits 0 when there is no draft or gate problem. `--json` also marks each problem Dan must answer (`"ask": true`).
