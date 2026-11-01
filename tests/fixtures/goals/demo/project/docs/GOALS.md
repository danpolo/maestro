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
- **Test:** `pytest tests/acceptance/test_not_a_recipe.py`

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
