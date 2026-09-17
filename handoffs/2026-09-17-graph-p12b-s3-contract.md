# P12B S3 — write the S3 design contract (F6–F8), then start F6 if context allows

**This file is the complete brief.** Dan starts the session with nothing but
`continue from handoffs/2026-09-17-graph-p12b-s3-contract.md`. No other prompt follows.

Working directory `/home/dan/projects/maestro`, branch `feat/graph-engineering-foundation`
(**no upstream**; `git pull` fails, expected). HEAD should be the **F5 commit** `929248a`
(`feat(graph): P12B F5 — …`) on top of F4 `1e78300`, with a clean tree.
Check with `git log -3 --oneline && git status --short`.

## Why this file exists

S2 is finished: F4 (`1e78300`) and F5 (`929248a`) are committed, gate and full suite green
(numbers below), STATE `in_progress_details` updated. The previous session reached 167K
tokens after F5, past the 160K handoff line, so it did **not** write the S3 contract. Writing
it needs a fresh read of the spec's F6–F8 sections; that is this session's first job.

No parallel session is active on this branch. Leave `P12A_*`, `AGENTS.md`, `GEMINI.md` alone.

## Step 1 — write `handoffs/2026-09-17-graph-p12b-s3-design.md`

Same shape as `handoffs/2026-09-17-graph-p12b-s2-design.md` (read its headings and its
§Invariants / §Out of scope / §Verification, lines ~1–64 and ~218–251, as the template):
a "complete brief" header, binding sources, "Code facts this contract rests on" (verified by
grep, with approximate line numbers), one section per F (store / pure module / runner /
routing / orchestrator / tests under `run_lane`, plus units), Invariants, Out of scope,
Verification with the gate command and the numbers to report.

Binding sources to read, in order:
1. `docs/graph-engineering/specs/phases/P12B_MODEL_FAILURE_HANDLING.md` — §1 ladder, F6/F7/F8
   in §2, their §3 acceptance bullets, §4 invariants (git-ignored, on disk only).
2. `handoffs/2026-09-17-graph-p12b-implementation.md` — Dan's decisions and invariants.
3. Memories `llm-failure-handling-policy` (2 retries → stronger diagnoser → replan or enhance
   prompt → one final run → park; fallback must not lower quality),
   `context-management-not-built-independent`, `harness-auto-updates-must-not-break-gates`,
   `gaps-are-not-decisions`.
4. STATE `_p12a…` open thread on dead candidate writers (the `uncertain` attempt whose node
   stays `running`, D-FAIL-1) — F8's starting point. `grep -n "D-FAIL-1" docs/graph-engineering/STATE.yaml`.

**Carry-over notes that must appear in the S3 contract:**
- Under routing, `pre_validate` checks `RetryPolicy.permits(quality_failures)` for routed
  agent nodes, and **nothing increments `quality_failures` yet**. F6 must increment it on a
  `bad_output` failure (inside the same transaction as the requeue). Until then a routed
  agent node re-readied by any path is not retry-limited.
- A `blocked` result from a non-agent node keeps the `failed` path (no pool to pause).
- A stepped-down failure is classified like any other (F5); F6's retries must not step down
  again — `routing.resolve(stepped_down_before=…)` already refuses a second step-down in the
  same run.
- Step-down history is per run: a W7 plan repair starts a new run with none. Decide in the
  contract whether F7's replan should carry it (the F4 counters are carried by
  `compiler.apply_repair`; `attempts.stepped_down` is not a counter).

## What S2 built (so S3 can use it without re-reading)

F4 — see `handoffs/2026-09-17-graph-p12b-s2-f5-build.md` §"What F4 built" (store
`pool_pauses`, node_states counters `blocked_attempts / quality_failures /
excluded_routes_json / first_blocked_at`, `Lifecycle.requeue_blocked / node_counters`,
`failures.pause_for`, `resolve_agent(exclude=)`, `DispatchRouting.refresh_pauses / on_blocked`,
`orchestrator._graph_on_blocked`).

F5:
- `router.resolve_agent(..., exclude=, strength_offset=0, billing=None)`. The offset lowers the
  estimated `min_strength` (floor `light`) and appends `stepped from <x>` to the demand reason;
  `billing` replaces the objective set with no hybrid grant, intersected with a role's
  declared `billing_categories`. `RouteUnavailable.reset_known: Optional[bool] = None`.
  `AgentBinding.stepped_down: bool = False`, emitted by `to_dict` only when True.
- `routing.DispatchRouting`: `step_down_tasks: set`, `unknown_reset_pools: set`,
  `note_pause(pool_id, *, reset_known)`; `refresh_pauses` rebuilds `unknown_reset_pools`.
  `routing.resolve(routing, node, manifest, *, exclude, now_epoch, task_id="",
  stepped_down_before=False)` — the step-down re-resolve lives inside it; a `pool_paused` wait
  gets `reset_known` from `_any_reset_known`.
- `runner`: `TickReport.wait_retry`; the wait text ends `(resets at <iso>)` when known;
  `_blocked_wait_exceeded(counters, resolved, now_epoch)` → `_refuse(...,
  "route_blocked_wait_exceeded: …")`; `_record_routing_decision` writes `stepped_down` (1 or
  NULL); `_select_affected_checks` forces `mode="shadow"`, `expansion_reason="stepped_down"`
  when `lifecycle.stepped_down_attempts(run_id)` is non-zero. `_requeue_blocked` calls
  `routing.note_pause`.
- `Lifecycle.stepped_down_attempts(run_id, node_id=None)`; `attempts.stepped_down INTEGER`.
- `orchestrator._graph_fallback(routing, spec, revision)` — called after compile and for each
  resumed run; journals `fallback_refused`, keeps `state["fallback_refused"]`, which
  `maestro status` prints. `admission_waiting[task]` carries `retry_after_sec`.
- `taskgraph`: `TaskSpec.fallback`, `FALLBACK_VALUES`, `parse_fallback`, `FallbackSpecError`,
  `_normalize_dropping_invalid` (drops invalid `resources` / `fallback` with an issue each).
- `compiler.fallback_refusal(spec, revision, *, has_authoritative_specs)` (pure; no
  `COMPILER_VERSION` bump).
- Test helpers in `tests/graph_engineering/test_failure_ladder.py` (F5 block at the end):
  `_strong_floor(policy)`, `_strong_and_balanced(policy=None)`, `_seed_strong_pause(...,
  then=)`, `_halt_after(polls, each=)`, `resolve_spy` fixture (routing results, router
  `strength_offset`s, launches), `_implementer_rows`, `_attempts_by_node`, `_zulu`.

**Lane facts learned in F5:**
- `node_states` has a row only after a node moves; a never-moved ready node has none (seed
  with `INSERT OR IGNORE … phase 'ready'` inside `store.transaction()`).
- The manifest carries no task `risk`; force `strong` demand through the role's
  `permitted_backend_capabilities["min_reasoning_strength"]`.
- A failed node reaches `park` on a later poll; don't halt the lane on the failure itself.
- The graph gate runs real `gates.run_authoritative_gate`; a verification with `cmd: "false"`
  fails it.

## Step 2 — F6 if context allows

Measure with `python3 ~/.claude/skills/close-session/scripts/context_usage.py` after the
contract is written. Under 120K: start F6 from the contract and commit it alone. Otherwise
write `handoffs/<date>-graph-p12b-s3-f6-build.md` (complete brief, like this one) and stop.

## Invariants (unchanged from S2)
One dispatch choke point (`advance` → `resolve` once → `claim` → `launch_worker`); one
fresh-vs-resume decision; no Context Gate. New behaviour only when `routing is not None`;
`tests/workflows/test_compatibility.py` unchanged. Failure columns never change routing
weights (D06). Scratch in `.scratch/`. A PreToolUse hook rejects Bash text containing the
privileged word. pytest's summary line is suppressed: read counts off the junit root
`testsuite`. Commits end with `Co-Authored-By: <model actually running> <noreply@anthropic.com>`.
STATE: targeted replacements only.

## Numbers so far
- F4: gate tests=900 failures=0; full tests=4657 failures=0 skipped=1 (`1e78300`).
- F5: gate tests=915 failures=0; full FULL_F5 (`929248a`).
- Gate command (S2 contract §Verification):
  `.venv/bin/python -m pytest -q tests/workflows/test_failures.py tests/graph_engineering/test_failure_ladder.py tests/graph_engineering/test_dispatch_wiring.py tests/backends tests/workflows tests/test_workflow_status.py tests/test_taskgraph*.py --junit-xml=.scratch/p12b/gate.xml`
