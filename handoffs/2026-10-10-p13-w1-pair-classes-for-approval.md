# P13 W1: pair classes for approval (decisions 3 and 4)

Dan, two decisions are in this file. Answer by row number, for example: "3: approve all, except row 7 -> light. 4: A".

Nothing here is live. The classes take effect only when the first schema 2 policy is published (slice 3). Until then both projects keep routing as they do today.

Correction (2026-10-10, after the spec review): that last sentence is not exact. The slice 2 release itself changes two things before any schema 2 policy: the DuetFlow planner moves from Codex 6.1 Sol to Opus 5.5, and, unless decision 6 is answered **A**, high-risk tasks run at medium effort where today they run at high. See `handoffs/2026-10-10-p13-w1-slice2-part2f-next.md`, "Finding that became decision 6".

## Decision 3: the class and cost of each (model, effort) pair

A route is now one model at one effort on one harness. Each row gets its own class and its own cost. A role asks only for a class (`light` < `balanced` < `strong`), and the router picks the cheapest row at or above that class.

| # | Harness | Model | Effort | Class | Cost | Basis |
|---|---|---|---|---|---|---|
| 1 | claude | claude-opus-5-5 | medium | strong | 1.0 | live policy |
| 2 | claude | claude-opus-5-5 | high | strong | 1.5 | estimate |
| 3 | claude | claude-sonnet-5-5 | low | light | 0.4 | estimate |
| 4 | claude | claude-sonnet-5-5 | medium | balanced | 0.7 | live policy |
| 5 | claude | claude-sonnet-5-5 | high | balanced | 1.0 | estimate |
| 6 | claude | claude-haiku-5-5 | medium | light | 0.18 | live policy |
| 7 | codex | gpt-6.1-sol | low | balanced | 0.6 | estimate |
| 8 | codex | gpt-6.1-sol | medium | strong | 1.0 | live policy |
| 9 | codex | gpt-6.1-sol | high | strong | 1.5 | estimate |
| 10 | codex | gpt-6-luna | medium | light | 0.06 | live policy |
| 11 | codex | gpt-6-luna | high | balanced | 0.1 | estimate |
| 12 | codex | gpt-6-astra | medium | strong | 4.0 | live policy |

Notes:

- "live policy" rows carry the class and cost the live policy gives that model today. The live policy has no effort, so those values are pinned to `medium`.
- "estimate" rows are not measured. Low is about x0.6 of the medium cost and high about x1.5. W2 telemetry replaces the estimates.
- Rows 7 and 11 decide whether the implementer can run on Codex. The implementer asks for `balanced`. If neither row is `balanced`, the implementer has no Codex route.
- `xhigh` and `max` are not proposed as routes. All agy pairs wait for W4, so agy has no route from the first schema 2 policy until then.
- Not verified yet: that Haiku accepts `--effort medium`. If it does not, row 6 is dropped.

### What each role would run under this table

Claude is first in the harness order, so Codex is used only when Claude cannot run.

| Role | Asks for | On Claude | On Codex (fallback) |
|---|---|---|---|
| implementer, test_designer | balanced | Sonnet at medium (row 4) | Luna at high (row 11) |
| reviewer | strong | Opus at medium (row 1) | 6.1 Sol at medium (row 8) |
| planner, diagnoser | strong | Opus at medium (row 1) | 6.1 Sol at medium (row 8) |

Two things change from today:

- Planner and diagnoser drop from Opus at `high` to Opus at `medium`. This is decision 4.
- The Codex implementer becomes Luna at `high` (row 11, cost 0.1), because it is cheaper than Sol at `low` (row 7, cost 0.6). To keep Sol for the implementer, make row 11 `light`.

## Decision 4: planner and diagnoser effort

Today both run Opus at `high`. A role no longer states an effort, so under the table above they get the cheapest `strong` row, which is Opus at `medium`.

- **A**: accept `medium` for planner and diagnoser. Nothing else changes.
- **B**: keep them at `high`. This needs one of two edits to the table:
  - **B1**: add a fourth class above `strong` (for example `max`), give it to rows 2 and 9, and set planner and diagnoser to it.
  - **B2**: lower row 1 (and row 8) to `balanced`. Then every `strong` role, the reviewer included, runs at `high`, and Sonnet no longer serves the implementer alone at that class.
- **C**: something else you state.

My recommendation is **A** now, and revisit with W2 telemetry: it is the cheaper choice, it needs no new class, and a regrade later is one row edit. If the planner's or diagnoser's quality matters more to you than the Opus quota, choose **B1**.
