# Goals — Recipe Box

**Format:** 2
**Contract revision:** 1 (approved 2026-10-17 by Dan in ChatGPT)

## 1. Purpose

Recipe Box turns the recipes Dan saves from Instagram into a searchable cookbook on his phone. It exists because saved posts are impossible to find again when he is actually cooking.

## 2. Who uses it

* **Dan** — saves posts during the week and cooks from them at the weekend.

## 3. Goals

### G1 — Every saved post lands in the cookbook
* **Outcome** — saved posts become recipes without Dan doing anything.
* **Baseline** — not_applicable
* **Target** — 95% of posts saved in the last 7 days appear as recipes within 1 hour.
* **Verified by** — AC1
* **Constraints** — INV1

### G2 — Usable in the kitchen
* **Outcome** — a recipe page is readable on a phone from arm's length.
* **Baseline** — not_applicable
* **Target** — Dan reads three recipes without zooming.
* **Verified by** — OP1

## 4. Final product walk-through

Dan saves a post on Tuesday. On Saturday he opens Recipe Box, finds the dish and cooks from the page.

### S1 — Saved post becomes a recipe
* **Goals** — G1, G2
* **Kind** — normal
* **Given** — a post saved on Instagram
* **When** — an hour passes
* **Then** — the recipe is in the cookbook and readable on the phone
* **Criteria** — AC1 · **Operator checks** — OP1

## 5. Acceptance criteria

### AC1 — Import ratio
* **Goals** — G1 · **Scenarios** — S1
* **Given** — the weekly sample of 40 saved posts
* **When** — the importer runs
* **Then** — the import ratio is reported
* **Oracle** — ratio at or above the threshold
* **Threshold** — 0.95
* **Layer** — e2e · **Evidence** — metric

## 6. Operator checks

### OP1 — Readable from arm's length
* **Goals** — G2 · **Scenarios** — S1
* **Instructions** — open three recipes on the phone propped on the counter.
* **Expected** — each reads without zooming.
* **Sign-off** — Dan

## 7. Non-goals

* No meal planning or shopping lists.

## 8. Hard invariants

* INV1 — no recipe is shown that Dan did not save (checked by AC1)

## 9. Trade-offs

None.

## 10. Priority order (when goals conflict)

G2 > G1

## 11. Authority

* **Autonomous** — everything inside the approved goals.
* **Ask Dan** — goal changes and conflicts.
* **Retries** — two.
* **Stop a failed approach when** — the same criterion fails twice for one cause.
* **Blocker** — Dan's decision or a missing Instagram login.
* **Channel** — telegram

## 12. Change history

| Rev | Date | Change | Rationale | Impact | Approved by | Supersedes |
|---|---|---|---|---|---|---|
| 1 | 2026-10-17 | first contract | ChatGPT grill | all | Dan | — |
