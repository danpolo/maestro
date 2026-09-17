# P12B S2 — design contract (F4, F5: the blocked branch)

**This file is the complete brief.** Dan starts the session with nothing but
`continue from handoffs/2026-09-17-graph-p12b-s2-design.md`. No other prompt follows.

Working directory `/home/dan/projects/maestro`, branch `feat/graph-engineering-foundation`
(**no upstream**; `git pull` fails, expected). S1 (F0–F3) is committed — see `git log -5`.

Binding sources, read in this order before coding:
1. `docs/graph-engineering/specs/phases/P12B_MODEL_FAILURE_HANDLING.md` — §1 ladder, F4/F5 in
   §2, their §3 acceptance bullets, §4 invariants (git-ignored, on disk only).
2. `handoffs/2026-09-17-graph-p12b-implementation.md` — Dan's decisions and invariants.
3. `handoffs/2026-09-17-graph-p12b-s1-design.md` — what F0–F3 built (`FailureRecord`,
   `classify_node_result`, `attempts.failure_*`, `Lifecycle.record_failure`).
4. Memories: `llm-failure-handling-policy`, `context-management-not-built-independent`,
   `harness-auto-updates-must-not-break-gates`.

S2 only builds the **blocked** branch: pause and re-route (F4), wait or step down (F5).
`bad_output` retries, the diagnoser and dead writers are S3 (F6–F8) — do not start them.
With `routing is None` every behaviour stays byte-identical.

---

## Code facts this contract rests on (verified 2026-09-17, re-check line numbers)

- `catalog.PoolState` (`catalog.py:~1047`) **already has** `paused_until` (epoch int),
  `pause_reason`, `pause(*, until, reason)`, `resume()`; `admit()` returns `pool_paused` with
  `retry_after_sec = paused_until - now` and self-resumes when expired. `ResourcePoolState.pool
  (pool_id, *, concurrency_limit=1)` creates on first use. Nothing outside `catalog.py` calls
  `pause` today.
- `DispatchRouting.resources` is created **once per process** in `orchestrator._graph_routing`
  (`~:1944`) and never refreshed. `routing.resolve(routing, node, manifest)` (`routing.py:~127`)
  calls `router.resolve_agent(...)` **without `now_epoch`** and there is **no `exclude=`**.
- `router.REASON_CODES` (`router.py:~69`) is closed; `route_` prefix is added in
  `runner._advance_agent` (`~:491`). A `wait` is `RouteUnavailable(disposition="wait",
  retry_after_sec=min(known) or None)`. `report.waiting[node_id]` carries only the text; the
  orchestrator (`~:2131`) splits it into `admission_waiting` — `retry_after_sec` is dropped.
- `MODEL_STRENGTHS = ("light", "balanced", "strong")`. `TaskDemand.min_strength` defaults to
  `"standard"`, which is **not** a rung (latent `KeyError`; `estimate_demand` always sets a real
  rung, so it never fires). Don't depend on the dataclass default; fixing it to `"balanced"` is
  in scope only if a test needs it.
- `OBJECTIVE_BILLING` (`router.py:~92`); `_permitted_billing` (`~:457`) adds the hybrid grant
  for free objectives.
- `AgentBinding.to_dict()` does not include `binding_id` (it is derived). Existing
  `binding_json` must stay byte-identical for unstepped bindings.
- `runner._commit_result` (`~:645-717`) is a **series of transactions** (each lifecycle call
  opens its own `store.transaction()`); it sets the node phase *before* the F3 classification.
  `RetryPolicy.permits(node_states.attempt_index)` is checked in `pre_validate` (`~:930`), and
  every compiled node has `max_attempts=1` — so re-claiming a re-readied node is refused today.
  `NODE_TRANSITIONS["failed"] == {"ready"}` already exists (`control/models.py:~122`).
- `quota.record_backend_exhausted(backend_name, reset_at_iso)` / `exhausted_backends()` live in
  state.json, keyed by **driver name** (`registry.driver_name_for_backend_id`), format
  `%Y-%m-%dT%H:%M:%SZ`; a blank/past reset is ignored.
- Config pattern: `config.max_plan_repairs` (`config.py:~151`) + `DEFAULT_MAX_PLAN_REPAIRS`.
  Task field pattern: `taskgraph.parse_resources` + `_CONSUMED_KEYS` + `ValidationIssue`.
  Compiler has **no** task-level reason field; `COMPILER_VERSION = "p7.1"`; the graph loop
  compiles in `orchestrator` (`~:2096`) and records `graph_compile_refused`.
- Notification: `hitl.telegram.notify_telegram(msg)` (never raises);
  `state.append_journal(event, detail)`. Copy `_pause_for_usage_limit`'s once-per-pause dedup.
- Append-only table pattern: `shadow_comparisons` DDL + `record_shadow_comparison` /
  `shadow_comparisons()` in `store.py`.

---

## F4 — `blocked` → pause, route again, no retry used

### Store (`control/store.py`, `control/lifecycle.py`)
- New table `pool_pauses(pool_id TEXT NOT NULL, paused_until TEXT NOT NULL, reset_known
  INTEGER NOT NULL, reason TEXT NOT NULL, failure_kind TEXT NOT NULL, attempt_id TEXT NOT NULL,
  recorded_at TEXT NOT NULL, UNIQUE(attempt_id, pool_id))`. `record_pool_pause(row, *,
  conn=None)` (controller only, `INSERT OR IGNORE`); `active_pool_pauses(now_iso)` → one row per
  pool, the latest `paused_until` still in the future.
- `node_states` gains (via `_ADDED_COLUMNS`) `blocked_attempts INTEGER NOT NULL DEFAULT 0`,
  `quality_failures INTEGER NOT NULL DEFAULT 0`, `excluded_routes_json TEXT`,
  `first_blocked_at TEXT`. Counters are cumulative across repairs (`[INV-08]`: carried by W7's
  counter carry — extend that carry to these columns).
- `Lifecycle.requeue_blocked(attempt_id, *, run_id, node_id, record, pause_row | None,
  exclude_route | None, at)` — **one** `store.transaction()`: `finish_attempt(failed)`,
  node `running → failed → ready` (or the direct path the transition table allows), the three
  F3 failure columns, `blocked_attempts += 1`, `first_blocked_at = coalesce(first_blocked_at,
  at)`, the pause row or the exclusion append. Lifecycle helpers used inside must accept the
  open connection (add `conn=` like `record_routing_outcome`), not open nested transactions.

### Failure → pause (`workflows/failures.py`, pure)
- `pause_for(record, *, now_epoch, prior_blocks, auth_pause_sec) -> (until_epoch, reset_known)
  | None`: `context_overflow` → `None`; `auth_failed` → `now + auth_pause_sec`, known; else
  `reset_at` (parsed Zulu) → known; else `retry_after_sec` → known; else backoff
  `now + min(60 * 2**prior_blocks, 1800)`, **not** known. `prior_blocks` = the node's
  `blocked_attempts` before this failure.

### Ingest (`workflows/runner.py`)
- In `_commit_result`, under `routing is not None` and `phase == "failed"`: classify **first**.
  If `record.is_blocked`, take the `requeue_blocked` path instead of the normal
  finish/set-phase path (and skip the separate F3 `record_failure` call — it is inside the
  transaction). The pool is the attempt's `binding_json["usage_pool_id"]`; the route is
  `(backend_id, model_id)` from the attempt row. No tick can observe `failed`, so no
  `on_failure` edge opens. Lease release still happens after. `bad_output` keeps today's path
  (F6 changes it in S3).
- After the transaction: `self.routing.on_blocked(self, attempt_id, record, pause)` — a new
  `DispatchRouting` callable, no-op default. Also apply the pause to the in-memory
  `routing.resources.pool(pool_id).pause(until=..., reason=kind)` immediately.
- `pre_validate`: for `node.kind == "agent"` under routing, `permits(quality_failures)`
  instead of `permits(attempt_index)`. `attempt_index` keeps incrementing.

### Routing (`workflows/routing.py`, `backends/router.py`)
- `resolve(routing, node, manifest, *, exclude=frozenset(), now_epoch=None)`; the runner passes
  the node's `excluded_routes` and `time.time()`. Still **one** call site in `_advance_agent`.
- `resolve_agent(..., exclude=frozenset())`: a route whose `(backend_id, model_id)` is in
  `exclude` is rejected with new reason code `route_excluded` (add to `REASON_CODES`). If that
  leaves no route, it is `block`/`no_eligible_route` as for any hard constraint.
- `DispatchRouting.refresh_pauses(store, now_epoch)` — called by the orchestrator at the start
  of every poll (next to `host_admission.begin`): for every `active_pool_pauses` row,
  `resources.pool(pool_id).pause(...)`; also for every `quota.exhausted_backends()` name, pause
  each catalog entry's pool whose driver name matches until that reset (legacy → graph).
  Restart safety comes from the table, not memory.

### Orchestrator (`orchestrator.py`, `config.py`)
- `config.auth_failure_pause_sec()` (default 3600) and `config.max_blocked_wait_sec()`
  (default 86400), strict like `max_plan_repairs`; both read once in `_graph_main`, errors →
  `runner_config_error`, exit 1.
- `on_blocked` implementation: journal `pool_paused` (`<pool> until <iso> (<kind>)`); for an
  `agent_cli` binding with `reset_known`, `quota.record_backend_exhausted(driver_name, iso)`
  (graph → legacy); for `auth_failed` also journal `auth_failed_pause` and send one
  `notify_telegram` per pause row (dedup by `attempt_id`).
- Legacy loop: unchanged (it already reads/writes `backend_exhausted`).

### F4 tests (`tests/graph_engineering/test_failure_ladder.py`, under `run_lane`)
Reuse the S1 harness in that file (`_FakeCliDriver`, `_patch_fixture_cli_driver`,
`_run_the_implementer_worker`). Two equal-strength entries: `fixture_cli_entry()` and
`fixture_cli_entry(backend_id="fixture_cli_b", usage_pool_id="fixture-pool-b")` (patch
`driver_name_for_backend_id` to map each to a distinct fake). Assert:
1. first route → `quota_exhausted` with reset: the next poll claims the implementer on
   `fixture_cli_b`; `quality_failures == 0`, `blocked_attempts == 1`; a `pool_pauses` row for
   `fixture-pool`; the implementer's phase never read `failed` (spy `set_node_phase` or
   check no `on_failure` target became ready); `resolve_agent` called once per attempt;
   `launch_worker` once per launch.
2. pause survives a restart: `maestro.state._reset_control_store()` + a new routing object →
   `refresh_pauses` pauses `fixture-pool` again.
3. `quota.exhausted_backends()` contains the fake driver name.
4. `context_overflow` → `excluded_routes_json` holds `[fixture_cli, fixture-1]` on that node
   only; no `pool_pauses` row; next claim goes to `fixture_cli_b`.
5. `auth_failed` → pause `paused_until - recorded_at == auth_failure_pause_sec`; journal
   `auth_failed_pause`; `notify_telegram` spy called once.
6. routing-None: a blocked result is written `failed` as today (extend the S1 unrouted test).
Unit: `pause_for` ordering and the 30-min cap; `record_pool_pause`/`active_pool_pauses`.

**Commit F4 alone** when green.

---

## F5 — no equal-or-better route → wait, or step down with checks

### Waiting with a reset (`runner`, `orchestrator`)
- `_advance_agent`'s `wait` branch also stores `report.wait_retry[node_id] =
  retry_after_sec`. The orchestrator writes it into `admission_waiting[task]` as
  `retry_after_sec` and a reason detail naming the reset iso. A known reset is waited for
  however far away (no park).
- **Park on an unknown reset:** in the same branch, if `first_blocked_at` is set, the wait
  reason is `pool_paused`, none of the rejected pools has `reset_known`, and
  `now - first_blocked_at >= max_blocked_wait_sec` → `_refuse(node, "route_blocked_wait_exceeded:
  …")` (fails the node → its park edge). `routing.resolve` exposes `reset_known` on the
  `RouteUnavailable` (new optional field, default `None`).

### The flag and the checkability check
- `taskgraph`: `fallback` in `_CONSUMED_KEYS`; `parse_fallback(value)`: `None` or
  `"step_down"`, anything else → `ValidationIssue(code="invalid_fallback")` and the task is
  re-normalized without it (mirror `resources`). `TaskSpec.fallback: str | None`, written
  back by `to_yaml_dict` only when set.
- `compiler.fallback_refusal(spec, revision, *, has_authoritative_specs) -> str | None` (pure):
  refused unless the revision's terminal requirements include a
  `maestro.handlers.verification` node **and** `has_authoritative_specs`. Compiler output is
  not changed (no `COMPILER_VERSION` bump in S2).
- The orchestrator calls it right after compiling (`gates.authoritative_specs(task_id)` for the
  second input). Refusal → journal `fallback_refused` (`<task>: <reason>`), state
  `fallback_refused[task] = reason` (shown by `maestro status`), and the task runs flagless.
  Accepted → `routing.step_down_tasks` (a new `DispatchRouting` set) gets the task id.

### The step-down (inside `routing.resolve`, still one call)
- If the first `resolve_agent` returns `wait`, the task is in `step_down_tasks`, and the node
  has no prior stepped-down attempt: call `resolve_agent` **once more inside `resolve`** with
  `strength_offset=-1` (new kw-only arg: lowers the estimated `min_strength` by one rung,
  floor `light`, no-op at `light` → keep the wait) and `billing=OBJECTIVE_BILLING[objective]`
  (new kw-only override: **no hybrid grant**, so free stays free). A bound result becomes
  `dataclasses.replace(binding, stepped_down=True)`; otherwise return the original wait.
- `AgentBinding.stepped_down: bool = False`; `to_dict` emits it **only when True** (keeps
  existing `binding_json` byte-identical). `attempts.stepped_down INTEGER` (nullable), written
  by `_record_routing_decision`, allowed in `_set_attempt_fields`.
- **Full suite:** `_select_affected_checks` (W5) forces `mode="shadow"` (full suite) with
  `expansion_reason="stepped_down"` when any attempt of this run has `stepped_down = 1`,
  whatever `routing.affected_mode` says.
- Acceptance already requires the gate; assert (don't add) that a stepped-down run with a
  failing gate is not accepted. A stepped-down failure is classified like any other.

### F5 tests (under `run_lane`)
Catalog: one `strong` route (paused via a seeded `pool_pauses` row) and one `balanced` route in
the same billing category; demand forced to `strong` (policy role `min_reasoning_strength` or
a high-risk task).
1. No flag: waits; `admission_waiting` names the reset; with a reset 3 days out and the clock
   advanced 2 days (monkeypatch `time.time` in router/runner), still waiting.
2. Unknown reset (`reset_known=0`), `first_blocked_at` older than `max_blocked_wait_sec` →
   node failed with `route_blocked_wait_exceeded`, task parks.
3. `fallback: step_down` + verifications: claims on the `balanced` route;
   `attempts.stepped_down == 1`; `binding_json["stepped_down"] is True`; with
   `AFFECTED` (`authoritative`, seeded comparisons) the gate still runs the full suite;
   a failing gate (`failing_commands`) means no acceptance.
4. Same flag, task without `verifications` → `fallback_refused` journaled; task waits.
5. `free_quality` objective with a hybrid overlay granting paid: the step-down never binds a
   `subscription`/`pay_per_token` route.
6. `resolve_agent` still called once per attempt from the runner (spy on `routing.resolve`),
   `launch_worker` once per launch.
Units: `parse_fallback`, `fallback_refusal`, `resolve_agent(exclude=, strength_offset=,
billing=)`, byte-identical `binding_json` for an unstepped binding.

**Commit F5 alone** when green.

---

## Invariants (binding)
- One dispatch choke point (`advance` → `resolve` once → `claim` → `launch_worker`); one
  fresh-vs-resume decision (`routing.session_decision`); a switched model is fresh. No
  Context Gate, no context rotation — `context_overflow` is only a route exclusion.
- New behaviour only when `routing is not None`. `tests/workflows/test_compatibility.py`
  unchanged. Failure columns are never read to change routing weights (D06).
- Keep `Usage.pools` out of `to_usage_json`; keep agy `Capabilities.sandbox=False` and its
  `--add-dir`/`--dangerously-skip-permissions` flags. Don't flip `engineering.runner`; don't
  touch `P12A_*`, `AGENTS.md`, `GEMINI.md`. Scratch goes in `.scratch/`.
- A PreToolUse hook rejects Bash text containing the privileged word (s-u-d-o).
- pytest's summary line is suppressed: read counts off the junit root `testsuite`.
- Commit messages end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` (use the
  model actually running the session). STATE (`docs/graph-engineering/STATE.yaml`,
  git-ignored): targeted replacements only.
- If a step can only be built by bypassing the choke point or adding a second resume
  decision, stop and ask Dan (spec §4).

## Out of scope
F6–F8, P12, the Context Gate, the legacy loop beyond the `backend_exhausted` mirror, fixing the
`TaskDemand` default unless a test needs it.

## Verification (report the numbers)
```bash
.venv/bin/python -m pytest -q tests/workflows/test_failures.py tests/graph_engineering/test_failure_ladder.py tests/graph_engineering/test_dispatch_wiring.py tests/backends tests/workflows tests/test_workflow_status.py tests/test_taskgraph*.py --junit-xml=.scratch/p12b/gate.xml
.venv/bin/python -m pytest -q --junit-xml=.scratch/p12b/full.xml   # exit 0; >120s, run in background and check the xml mtime is fresh
```
S1 baseline: gate tests=814 failures=0; full suite count in STATE `in_progress_details`.
Report the F4 and F5 commit hashes and both junit counts, update STATE
`in_progress_details` (F4/F5 done, S3 = F6–F8 next), and write
`handoffs/<date>-graph-p12b-s3-design.md` for S3 (same shape as this file).

Context: measure with `python3 ~/.claude/skills/close-session/scripts/context_usage.py` after
F4's commit and before F5. For Opus 5, write a handoff at 160K and start fresh by 200K;
F4 is the larger half, so a split between F4 and F5 is expected and fine.
