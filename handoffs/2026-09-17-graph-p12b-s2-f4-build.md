# P12B S2 (resumed) — build F4, then F5

**This file is the complete brief.** Dan starts the session with nothing but
`continue from handoffs/2026-09-17-graph-p12b-s2-f4-build.md`. No other prompt follows.

Working directory `/home/dan/projects/maestro`, branch `feat/graph-engineering-foundation`
(**no upstream**; `git pull` fails, expected). HEAD should still be `d356b7f` (S1 F3) with a
clean tree. Check with `git log -1 --oneline && git status --short`.

## Why this file exists

The previous session read the S2 contract and every source file it depends on. Reading took
177K tokens before any code was written, which is past the 160K handoff threshold. **No code
was changed.** This file keeps what that reading found, so this session can start coding
without re-reading whole files.

**Binding contract:** `handoffs/2026-09-17-graph-p12b-s2-design.md`. Read it in full; it is
about 250 lines. It has the F4/F5 design, the tests, the invariants, what is out of scope and
the verification commands. Everything below adds to it. Where the two differ, the contract
wins, except for the decisions listed under "Decisions taken".
**Do not** re-read the spec, the S1 contract or the implementation handoff unless something
is ambiguous. The contract already includes what matters from them.

**Context discipline:** read only the line ranges listed below. Don't `Read` whole files over
300 lines. Measure with `python3 ~/.claude/skills/close-session/scripts/context_usage.py`
after the F4 commit. If you're over 150K at that point, write the F5 handoff instead of
starting F5.

---

## Verified code map (2026-09-17, HEAD d356b7f)

### `maestro/control/lifecycle.py` (499 lines)
- `set_node_phase` at :274–306. It reads the row through `self.store.conn`, then opens
  `with self.store.transaction() as conn`. The INSERT…ON CONFLICT only sets
  phase/attempt_index/last_attempt_id/updated_at.
- `finish_attempt` at :361–378, `record_routing_decision` at :380, `record_failure` at :398,
  `_set_attempt_fields` at :408–428 (the `allowed` set, plus its own transaction).
- `store.transaction()` (`store.py:514`) runs `BEGIN IMMEDIATE`, and **nesting it fails**. To
  add `conn=None` to `set_node_phase`, `finish_attempt` and `_set_attempt_fields`, use a small
  helper: `contextlib.nullcontext(conn) if conn is not None else self.store.transaction()`.
  Reads that go through `store.conn` inside the open transaction see the uncommitted writes,
  because it is the same connection.
- The replay fold (`apply`, :120–130) ignores extra node_states columns, and
  `stored_projections` (:468) doesn't read them. The new columns can't cause replay drift, as
  long as every phase move still appends its own `NODE_PHASE_CHANGED` event.

### `maestro/control/store.py` (1327 lines)
- The `node_states` DDL is at :275–283. `attempts` is at :285–320. `_ADDED_COLUMNS` is at
  :393–422, with the P12B F3 entries last. `_migrate_columns` is at :424.
- The append-only pattern to copy is `record_routing_outcome` at :1167–1188. It takes
  `conn=None`, calls `_require_controller()` and runs `INSERT OR IGNORE`. The shadow
  comparisons follow the same pattern at :1206–1223.
- Put the new `pool_pauses` DDL after `shadow_comparisons` (:250–261), using
  `CREATE TABLE IF NOT EXISTS`.

### `maestro/control/models.py`
- `NODE_TRANSITIONS` is at :116: `running→failed`, `failed→ready`, `claimed→running|failed`,
  `ready→claimed`. A requeue therefore runs `[claimed→]running→failed→ready`, one event per
  step. `claim()` then moves `ready→ready`, which is a no-op, and then `→claimed`.

### `maestro/workflows/runner.py` (1379 lines)
- `TickReport` is at :281 (`waiting: dict`; F5 adds `wait_retry`). `Claim` is at :260.
- `advance` is at :406. `_advance_agent` is at :476–530, and its single `resolve` call is at
  :489. The wait branch is at :491–494.
- `_record_routing_decision` is at :532–572. `_refuse` is at :574. `_commit_result` is at
  :645–717. Its current order is: receipt → evidence → shadow comparison → outcome artifact →
  plan repair (only when succeeded) → `finish_attempt` → `[running]` → `set_node_phase(phase)`
  → settle telemetry → `on_attempt_finished` → F3 classify plus `record_failure` (:695–706) →
  lease release (:708–714) → `_apply_repair`.
- `_select_affected_checks` is at :437–474. It calls
  `validation.select_affected_checks(specs, changed, mode=self.routing.affected_mode)`, and
  F5 forces `mode="shadow"` there.
- `pre_validate` is at :922–944. The `permits(index)` check is at :930–935. `claim` is at
  :946–1022. `_fail_claim` is at :1177–1210.
- `time` is not imported in `runner.py`, so add `import time` for `now_epoch`.

### `maestro/workflows/routing.py` (368 lines)
- `DispatchRouting` is a dataclass at :74–96. Add `on_blocked` (no-op default, like
  `_no_release`), `step_down_tasks: set` (F5) and a `refresh_pauses` method.
- `resolve(routing, node, manifest)` is at :127–132. `binding_view` is at :270 and already
  exposes `usage_pool_id`, `backend_id` and `model_id` from `binding_json`.

### `maestro/backends/router.py` (872 lines)
- `REASON_CODES` is at :69–85. Add `route_excluded`. The runner then prefixes it to
  `route_route_excluded` in the refusal text, which is harmless. The contract names the code
  `route_excluded`; keep that name.
- `OBJECTIVE_BILLING` is at :92. `AgentBinding` is at :130–201, with `to_dict` at :182; F5
  adds `stepped_down` only when it is True. `RouteUnavailable` is at :205–240; F5 adds
  `reset_known: Optional[bool] = None`, left out of `to_dict` when `None`.
- `estimate_demand` is at :348–421. `_permitted_billing` is at :457. `_hard_constraints` is at
  :466.
- `resolve_agent` is at :605–787. Its signature already has `now_epoch`. The route loop is at
  :667: put the `exclude` check before `_hard_constraints`, as
  `RejectedRoute(entry.backend_id, model_id, "route_excluded", …)`. Admission is at :704–715
  and calls `resources.pool(pool_id, concurrency_limit=entry.concurrency_limit)`, then
  `resources.admit`. The wait result is built at :771–780.
- Nothing calls `PoolState.reserve()`, so `active` stays 0. Creating a pool early with
  `concurrency_limit=1` is harmless, but still pass the entry's limit where you know it.

### `maestro/backends/catalog.py`
- `PoolState` is at :1047–1123 and `ResourcePoolState.pool` is at :1141. `pause()`
  **overwrites** the current pause. Write a helper that only ever extends a pause
  (`until > paused_until`), and use it in both `refresh_pauses` and the in-memory apply.

### `maestro/workflows/compiler.py`
- `apply_repair` is at :1233. The old rows come from `SELECT * FROM node_states` (:1284). The
  new-run seed INSERT is at :1346–1369, and **this is where the W7 counter carry lives**.
  Extend the INSERT column list with `blocked_attempts, quality_failures,
  excluded_routes_json, first_blocked_at`, copied from `prior` (defaults 0/0/None/None). The
  seed runs only when `carried or index > 0`, so a node with counters always has
  `index > 0`.

### `maestro/orchestrator.py` (2940 lines)
- `_graph_routing` is at :1944–1980. `_graph_main` is at :1983. The config reads are at
  :2021–2042; copy the `max_plan_repairs` block for the two new settings. `host_admission` is
  at :2047–2049. Compilation is at :2095–2103 (F5 adds `fallback_refusal` right after
  it). `host_admission.begin(store, time.time())` is at :2114, and `refresh_pauses` goes
  next to it. The waits are split at :2132–2136; F5 adds `retry_after_sec` there.
  `_record_admission_waits` is at about :789.
- `_pause_for_usage_limit` is at :190–218 and is the model for the once-per-pause
  notification.
- The imports already include `notify_telegram` (:62) and `record_backend_exhausted` (:92).
  Refer to `registry` through the module (`from maestro.backends import registry`, then
  `registry.driver_name_for_backend_id`) so the tests' monkeypatch applies. Catch its
  `UnknownBackend`.

### `maestro/quota.py`
- `record_backend_exhausted(backend, reset_at)` is at :198. It silently ignores a blank reset,
  a past reset and a blank name. `exhausted_backends()` is at :230. Add
  `exhausted_backend_resets() -> dict[name, reset_iso]`, using the same filtering, and build
  `exhausted_backends` on top of it. `refresh_pauses` needs the reset times, not just the
  names.

### `maestro/config.py`
- `DEFAULT_MAX_PLAN_REPAIRS` and `max_plan_repairs()` are at :146–175. Copy them for
  `auth_failure_pause_sec` (default 3600) and `max_blocked_wait_sec` (default 86400). Both
  must be strict positive integers; a bool is not accepted.

### Tests: `tests/graph_engineering/`
- `conftest.py`:
  - The fake clock (`_Clock`, :82) replaces **only** `orchestrator.time`. It starts at epoch
    `1_000_000.0` and adds 30 s per poll. `MAX_POLLS = 12`.
  - `fixture_cli_entry(**overrides)` is at :313 (backend `fixture_cli`, pool `fixture-pool`,
    model `fixture-1`, strength `balanced`, subscription). `fixture_routing(entries=,
    policy=, resources=)` is at :334.
  - `LaneScenario` fields are at :168: `project_yaml`, `routing` (a factory),
    `failing_commands`, `on_poll`, `before_run`.
  - `notify_telegram` is already patched everywhere to a no-op. To count its calls, patch
    `maestro.orchestrator.notify_telegram` again inside the test, after `run_lane` installs
    its doubles. Because `run_lane` installs the doubles inside `_run`, do it with an
    `on_poll` hook or a `before_run` hook.
- `test_failure_ladder.py` (310 lines) holds the S1 harness: `_FakeCliDriver` (:55),
  `_patch_fixture_cli_driver` (:70, maps every backend to one driver; F4 needs a per-backend
  version), `_implementer_attempts` (:93), `_run_the_implementer_worker` (:100, only ever
  runs `attempts[0]`; F4 needs a version that runs the newest claimed attempt), and the
  unrouted test (:276).
- `test_dispatch_wiring.py` has `_graph_scenario` (:119), `_task` (:61), `FREE_ENDPOINT`
  (:278), `_worker_argv` (:303), `_node_phase` (:315), `_FakeTmux` (:593) and `_window_for`
  (:1251).
- The worker picks its driver with
  `registry.get_backend(registry.driver_name_for_backend_id(args.backend_id))`
  (`worker.py:140`).
- The real clock reads 2026-09-17. Build `reset_at` values in tests relative to the real
  `datetime.now(timezone.utc)`, because `record_backend_exhausted` drops past resets.

---

## Decisions taken (in addition to the contract)

1. **Time bases.** The runner passes `time.time()`, the real clock, to `resolve` and to
   `pause_for`. The orchestrator passes its own `time.time()` to `refresh_pauses`, which is
   the fake clock in lane tests. `active_pool_pauses` takes `now_iso`. Convert the fake
   epoch with `datetime.fromtimestamp(..., timezone.utc)`. Under the fake clock (1970) every
   stored row counts as active, which is the right answer for the restart test.
2. **`requeue_blocked`** runs as one transaction with these steps:
   1. `finish_attempt(failed, outcome_ref)`;
   2. `running` (only if the row was `claimed`);
   3. `failed`;
   4. `ready`, with `attempt_id` and `attempt_index` unchanged and one event per move;
   5. the three F3 columns;
   6. `UPDATE node_states SET blocked_attempts = blocked_attempts + 1,
      first_blocked_at = COALESCE(first_blocked_at, ?)[, excluded_routes_json = ?]`;
   7. `record_pool_pause(row, conn=conn)` when there is a pause.

   Store `excluded_routes_json` as a sorted JSON list of `[backend_id, model_id]` pairs,
   without duplicates. Add a `node_counters(run_id, node_id)` helper, either in `Lifecycle`
   or in the runner, that returns the four new columns, with the exclusions as a
   `frozenset` of tuples.
3. **Timestamps.** `pool_pauses.paused_until` is Zulu ISO (`%Y-%m-%dT%H:%M:%SZ`), so it can
   be handed straight to `record_backend_exhausted`. `recorded_at` is the ingest `at`. The
   F4 test 5 check (`paused_until - recorded_at == auth_failure_pause_sec`) needs both values
   on the same clock. `at` is `now_iso()`, which is real time, so compute the pause from
   `now_epoch = time.time()` taken at ingest and allow ±2 s in the assertion.
4. **Carry** all four new node_states columns in `apply_repair`.
5. **`on_blocked` wiring.** The orchestrator builds a closure and sets it after
   `_graph_routing()`, the same way it wires `host_admission`. The closure closes over the
   `auth_failure_pause_sec` value that was read once. The runner passes `pause_for` its
   `auth_pause_sec` through `routing.auth_failure_pause_sec`, a new `DispatchRouting` field
   (default 3600) that `_graph_main` sets. Contract §Orchestrator then needs no second read.
6. **Telegram dedup.** Keep a set of `attempt_id`s in the closure. The `UNIQUE(attempt_id,
   pool_id)` constraint on the table already stops duplicate rows.

## Order of work

1. F4: store and lifecycle → `failures.pause_for` → router `exclude` plus `route_excluded` →
   `routing.resolve(exclude=, now_epoch=)`, `on_blocked` and `refresh_pauses` → runner ingest
   and `pre_validate` → config → orchestrator → quota helper → tests (contract §F4 tests 1–6
   plus the unit tests).
2. Run the gate command. Commit F4 on its own. The message ends with
   `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
3. Measure context. If there is room, do F5 exactly as the contract says. Otherwise write
   `handoffs/2026-09-17-graph-p12b-s2-f5-build.md`, in this file's shape.

## Invariants, verification and the close-out

These are all in the contract; don't skip them.
- There is one `resolve` per attempt.
- `routing is None` must behave byte-identically, so `tests/workflows/test_compatibility.py`
  stays unchanged.
- The Context Gate is not built.
- A hook rejects any Bash text containing the privileged word.
- Read test counts from the root `testsuite` element of the junit XML.
- STATE `in_progress_details` takes targeted edits only.
- S1 baseline: gate tests=814 failures=0.
- The last step is `handoffs/<date>-graph-p12b-s3-design.md`.
