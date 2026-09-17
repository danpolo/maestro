# P12B S2 (resumed again) — build F5, then close S2

**This file is the complete brief.** Dan starts the session with nothing but
`continue from handoffs/2026-09-17-graph-p12b-s2-f5-build.md`. No other prompt follows.

Working directory `/home/dan/projects/maestro`, branch `feat/graph-engineering-foundation`
(**no upstream**; `git pull` fails, expected). HEAD should be the **F4 commit**
(`feat(graph): P12B F4 — …`) on top of `d356b7f`, with a clean tree. Check with
`git log -2 --oneline && git status --short`.

## Why this file exists

The previous session built and committed F4. It ended at 151K tokens, past the 150K limit
for starting F5. **F5 has not been started.**

**Binding contract:** `handoffs/2026-09-17-graph-p12b-s2-design.md`. Read §F5, §Invariants,
§Out of scope and §Verification in full (lines ~151–251). Skip §F4, which is done.
`handoffs/2026-09-17-graph-p12b-s2-f4-build.md` holds the verified code map and decisions 1–6.
Read its "Verified code map" only for the files F5 touches: `runner.py`, `routing.py`,
`router.py`, `compiler.py`, `orchestrator.py` and `taskgraph`. **Line numbers there have
shifted** because F4 added code. Re-grep before editing. Read line ranges, never whole files
over 300 lines. Measure with `python3 ~/.claude/skills/close-session/scripts/context_usage.py`.

---

## What F4 built (so F5 can use it without re-reading)

- **Store.** `pool_pauses` table; `store.record_pool_pause(row, conn=)`;
  `store.active_pool_pauses(now_iso)` returns the latest row per pool.
  `paused_until` is Zulu `%Y-%m-%dT%H:%M:%SZ`, and `reset_known` is 0 or 1. `INSERT OR
  IGNORE` also silently skips NOT NULL violations.
- **node_states columns.** `blocked_attempts`, `quality_failures`, `excluded_routes_json`
  and `first_blocked_at`. `compiler.apply_repair` carries all four.
- **Lifecycle.**
  - `requeue_blocked(...)` runs as one transaction.
  - `node_counters(run_id, node_id)` returns
    `{blocked_attempts, quality_failures, excluded_routes: frozenset[(backend, model)], first_blocked_at}`.
  - `set_node_phase`, `finish_attempt`, `record_failure` and `_set_attempt_fields` take
    `conn=`, through `Lifecycle._transaction(conn)`.
  - F5's `stepped_down` goes into `_set_attempt_fields`'s `allowed` set, and into
    `store._ADDED_COLUMNS` as `("attempts", "stepped_down", "INTEGER")`.
- **`failures.py`.** `pause_for(record, *, now_epoch, prior_blocks, auth_pause_sec)` and
  `BACKOFF_CAP_SEC = 1800`.
- **`router.resolve_agent`.**
  - It takes a new `exclude=frozenset()` kw-only argument, placed after `trace`.
  - `route_excluded` has been added to `REASON_CODES`.
  - The exclusion check runs before `_hard_constraints` in the route loop.
  - F5 still needs to add `strength_offset=0` and `billing=None` (the override), and
    `RouteUnavailable.reset_known`.
- **`routing.py`.**
  - `resolve(routing, node, manifest, *, exclude=, now_epoch=)` forwards both arguments.
  - `DispatchRouting` gained `on_blocked`, `auth_failure_pause_sec` (3600),
    `max_blocked_wait_sec` (86400, **already set from config by `_graph_main`**) and
    `refresh_pauses(store, now_epoch)`.
  - `refresh_pauses` pauses from the table and from `quota.exhausted_backend_resets()`.
  - Module helpers: `extend_pause(pool, *, until, reason)`, which never shortens a pause,
    and `_zulu_epoch`.
  - F5 still needs to add `step_down_tasks: set`.
- **`runner.py`.**
  - `import json` and `import time` are at module top.
  - `_advance_agent` reads `self.lifecycle.node_counters(...)` just before the single
    `resolve` call. The counters are therefore already there for the F5 park check
    (`first_blocked_at`).
  - `_commit_result` classifies first. A blocked failure on an `agent` node goes through
    `_requeue_blocked`, then `routing.on_blocked`.
  - `pre_validate` uses `quality_failures` for routed agent nodes.
- **`orchestrator.py`.**
  - `_graph_on_blocked(catalog)` is a closure factory placed before `_graph_main`. It
    journals `pool_paused` and `auth_failed_pause`, sends one Telegram notification per
    `attempt_id`, and mirrors a known `agent_cli` reset into `record_backend_exhausted`.
  - `routing.refresh_pauses(store, time.time())` runs right after `host_admission.begin`.
- **`config.py`.** `auth_failure_pause_sec()` and `max_blocked_wait_sec()` are strict
  positive integers.
- **`quota.py`.** `exhausted_backend_resets()` was added, and `exhausted_backends()` is
  now built on it.
- **Tests.**
  - `tests/graph_engineering/test_failure_ladder.py` gained reusable helpers:
    `_two_route_routing`, `_patch_two_drivers(monkeypatch, first, second)` (driver names
    `fake_a` and `fake_b`), `_patch_verdict_kinds(monkeypatch, *kinds)`,
    `_attempts_by_rowid`, `_run_implementer_workers(settled_limit)`, `_counters`,
    `_pool_pauses`, the `ladder_spies` fixture (implementer `resolve` exclude arguments,
    launches, and the phase at each tick), `_reset_in(hours)` and `_DONE`.
  - `before_run` hooks must chain `_create_control_db(run)`. Patch `notify_telegram` on
    `run.orchestrator` inside `before_run`.
  - Unit tests are in `tests/control/test_store.py`, `tests/workflows/test_failures.py` and
    `tests/backends/test_router.py`.
- **Lane fact.** Without a sentinel, an adopted clean fixture answer settles as
  `uncertain`, not `succeeded`. Assert on "not failed / no failure_kind" instead.

## Carry-over notes

- **S3 must know:** under routing, `pre_validate` now checks `permits(quality_failures)`, and
  **nothing increments `quality_failures` yet**. F6 must increment it on a `bad_output`.
  Until then, a routed agent node that is re-readied by any path is not retry-limited. Put
  this in the S3 handoff.
- A `blocked` result from a non-agent node keeps today's `failed` path (a deliberate
  choice, because such a node has no pool to pause).

## Order of work

1. F5 exactly as contract §F5 says. Tests 1–6 plus the unit tests. Gate. Commit F5 on its
   own, with a message ending `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
   (use the model actually running).
2. Full suite in the background. Read the counts from the junit root `testsuite`.
3. Update STATE `in_progress_details` with targeted edits only: F4 and F5 are done, and S3
   (F6–F8) is next.
4. Write `handoffs/2026-09-17-graph-p12b-s3-design.md` in the shape of the S2 contract, and
   include the carry-over notes above.
5. Report to Dan: the F4 and F5 hashes and both junit counts.

## Numbers so far

- F4 gate command (contract §Verification): **tests=900, failures=0**. The S1 baseline was
  814.
- F4 full suite: see the F4 commit message, or re-run it.
