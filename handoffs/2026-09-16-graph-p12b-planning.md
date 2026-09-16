# Handoff — P12B planning: model-failure handling (after P12A, before P12)

**This file is the complete brief.** Dan starts the session with nothing but
`continue from handoffs/2026-09-16-graph-p12b-planning.md` — no other prompt follows and
there is no chat context. Read this file in full, follow the read-order, and discover
anything not written here from the repo.

Working directory: `/home/dan/projects/maestro`. Branch `feat/graph-engineering-foundation`
(**no upstream** — `git pull` fails; that is expected).

---

## Where things stand

**P12A is complete** (2026-09-16). Commits: S2 `b738faa` `4ec3a69` `0bc0e97` `ea82b01`;
S3 `67ed72b` (W4) `4616f2d` (W5/W6) `8e0de58` (W7) `2ee754c` (W9/W10/W11).
**Full suite at `2ee754c`: junit tests=4560 failures=0 errors=0 skipped=1, exit 0.**
Every `owner: P12A` thread is closed (`owned_threads(state, "P12A") == []`,
`unstructured_threads == []`). The P12A record is in STATE `completed_phases` and
`phase_records.P12A`; `in_progress_details` is `null`.

STATE now says `current_phase: "P12B"`, `status: "not_started"`. **"P12B" is a provisional
name** Dan hasn't confirmed; rename it (STATE + generator + spec filename) if Dan prefers another.

**Known trap:** `scripts/next_graph_prompt.py` doesn't know P12B yet
(`PHASE_SEQUENCE`, `ALL_PHASES`, `CONTEXT_ROUTING` at lines ~38-43 and ~224). Running it
with no `--phase` raises `ValueError: Unknown phase ID: P12B`. That's deliberate, not a
bug to hide: registering the phase is part of this planning session (see below).

## Goal of this session

Plan the phase Dan decided on 2026-09-16. **It is a planning session, not an implementation
session.** Deliverables:
1. `docs/graph-engineering/specs/phases/P12B_<NAME>.md`, in the same shape as
   `P12A_GRAPH_DISPATCH_WIRING.md` (§1 goal/"wired" definition, §2 W-items, §3 acceptance
   evidence, §4 invariants, target files, checklist, test command).
2. Register the phase in `scripts/next_graph_prompt.py`: `PHASE_SEQUENCE` becomes
   `... "P12A", "P12B", "P12"`, plus `ALL_PHASES` and a `CONTEXT_ROUTING["P12B"]` entry.
   Add a test next to `test_next_phase_after_p07b_is_the_wiring_phase` in
   `tests/test_next_graph_prompt.py` asserting P12A's prompt says `Set current_phase: P12B`.
   Then the generator must run cleanly on live STATE.
3. Move the W4 known limit (below) and any thread the spec absorbs to `owner: P12B`, or
   add new `_p12b_open_threads`, using the thread format in STATE's header.
4. Per Dan's global rules, a plan is a **visual deliverable**: build a diagram-led HTML
   artifact with the `visual-explainer` skill and send it with `send-to-me`.

Use `grill-with-docs` / `grill-me` with Dan for the decisions below. Per Dan's collaboration
policy, bundle every material decision into one request, not a series of approval gates.

## Dan's binding decisions (2026-09-16) — the phase's scope

Full text: end of `handoffs/2026-09-16-graph-p12a-s3-design.md`. Also memory
`llm-failure-handling-policy`.

**D-FAIL-1.** Handle crashing/failing LLM calls in **every mode and every graph run**.
Principle: **a failing model must not lower the quality of the work**; if one model is
blocked, use another.
- Paid mode: failures are mostly rate limits; fall back to another model.
- Free mode: handle what is anticipable now: 429/rate limit, quota exhausted, provider
  5xx/outage, timeout, auth failure, context overflow, malformed tool calls,
  empty/garbage output. The unknown ones come out of Dan's free-model pilots.
- No equal-or-better model available: if the task is **fully checkable** (tests/evals),
  step down a tier, verify properly, and accept only on success. Otherwise wait for the reset.

**D-FAIL-2.** Escalation ladder: after **2 failed retries** of a node, escalate to a
**more expensive model** acting as diagnoser, which decides *bad graph* or *poor agent*.
- bad graph → plan repair plus **one final run**, then park;
- poor agent → enhance that node's prompt/context and relaunch **one last time**, then park.
Both are one `PlanRepairProposal` and count against `engineering.max_plan_repairs`
(default **1**, already built in W7).

## What P12A already gives this phase (build on it)

- **One dispatch choke point:** `WorkflowRunner.advance` → `dispatch` → `launch_worker`
  (`maestro/workflows/runner.py`). There is one fresh-vs-resume decision
  (`routing.session_decision`), one `resolve` per node, and one failure path (`_refuse` →
  `_fail_claim`, shared with launch failure).
- **Durable routing decision** on every attempt (15 `attempts` columns + append-only
  `routing_outcomes`, `lifecycle.record_routing_decision/settle`, `routing.record_outcome`,
  `load_statistics`). This is the natural input for "which model failed, try another".
- **Pools and admission:** `router.resolve_agent`, `BackendCapabilityEntry.pool_for`, usage
  pools (`Usage.pools`, never in `to_usage_json`), `_GraphHostAdmission`.
- **W7 plan repair in the loop:** a succeeded `planner`/`diagnoser` node's
  `outputs["plan_repair"]` → `compiler.plan_repair` + `repair_counters(max_repairs)` →
  `apply_repair` → `runner.successor`, `TickReport.repaired_to`, journal
  `graph_run_repaired`. Refusal fails the node + `WorkflowRepairRefused` event.
  **Known limit:** an `apply_repair` refusal leaves the node succeeded and the old run
  unrepaired. **Not built:** the automatic 2-retries → stronger-diagnoser trigger (this phase).
- **W4 known limit (this phase resolves it, per D-FAIL-1):** a candidate whose writer dies
  with no receipt is reconciled to attempt `uncertain` (`agent_editing_worktree` is not
  repeatable, A05) and its node stays `running`, so `candidate_select` waits forever.
  Fixing it needs a reconcile → node-phase transition, which also changes `routing is None`
  behaviour. It is recorded on the closed `_p07_open_threads` #2 note.

## Invariants that still bind

- **Context Gate is NOT built** and is built independently (memory
  `context-management-not-built-independent`). Plan none of it here.
- §4 of P12A: no second dispatch path, no second fresh-vs-resume decision, no second
  context rotation. The failure phase must route fallbacks through `advance`/`resolve`.
- `Usage.pools` stays out of `to_usage_json` / `from_usage_json` (a test pins it).
- `Capabilities.sandbox` stays `False` for agy. agy's `--mode accept-edits` is still
  uncharacterised (open question in STATE `whats_left` history). A probe was refused by
  the permission classifier; raise that with Dan, don't route around it.
- Harness auto-updates must not break gates (memory `harness-auto-updates-must-not-break-gates`).
- Don't flip the `engineering.runner` `legacy` default. Leave `P12A_*` spec alone.
- The `claude` CLI message "monthly spend limit" also fires for ordinary 5h/weekly caps
  (memory). It's a relevant failure class for the paid-mode design: it arrives as a
  `rate_limit` 429 and is resolved by a reset.

## Coordination

- `STATE.yaml` and `handoffs/` are **git-ignored**. Edit STATE with targeted replacements
  only; never expect it in a commit.
- `AGENTS.md` / `GEMINI.md` belong to sibling agents (git-ignored); don't touch them.
- `.scratch/` is untracked; put work files there, **not `/tmp`**. A STATE backup from
  before the P12A close-out is at `.scratch/s3/STATE.before-closeout.yaml`.
- A PreToolUse hook rejects any Bash command whose text contains the privileged word
  (s-u-d-o), even inside a string. Write such text with the Write tool.
- pytest's summary line is suppressed: read `tests`/`failures` off the junit **root
  `testsuite`**.
- Commit messages end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

## In scope / Out of scope

- **In:** grilling Dan on the open decisions; the P12B spec; generator registration and its
  test; thread re-ownership; the visual plan artifact; STATE `current_phase`/`status`
  comment updated to point at the spec.
- **Out:** implementing any P12B W-item; the Context Gate; P12; flipping `legacy`.

## Verification (report the numbers)

```bash
.venv/bin/python scripts/next_graph_prompt.py > .scratch/p12b/prompt.md; echo $?   # must be 0
.venv/bin/python -m pytest -q tests/test_next_graph_prompt.py --junit-xml=.scratch/p12b/ngp.xml
.venv/bin/python -m pytest -q --junit-xml=.scratch/p12b/full.xml   # baseline 4560/0 skipped=1, exit 0
```

Report: the spec path and W-item count; the generator exit code; the ngp and full-suite
junit counts; `owned_threads(state, "P12B")` count; the artifact link and delivery status.

## When this session ends

Measure the context: `python3 ~/.claude/skills/close-session/scripts/context_usage.py`.
For Opus 5, prepare a handoff at 160K and start fresh by 200K. These are absolute token
counts. The next handoff must be a fully self-contained file in `handoffs/`, like this one.
