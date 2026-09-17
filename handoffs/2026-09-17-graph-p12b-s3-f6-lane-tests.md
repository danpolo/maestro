# P12B S3 — finish F6 (the lane tests), then F7

**This file is the complete brief.** Dan starts the session with nothing but
`continue from handoffs/2026-09-17-graph-p12b-s3-f6-lane-tests.md`. No other prompt follows.

Working directory `/home/dan/projects/maestro`, branch `feat/graph-engineering-foundation`
(**no upstream**; `git pull` fails, expected). No parallel session is active on this branch.

## Where the previous session stopped

It built **all of F6's behaviour** and its **unit tests**, and committed them as
`782ea77` (see `git log -3 --oneline`; the commit is
`feat(graph): P12B F6 — bad output retries twice, fresh then on another model`).
It stopped at 182K tokens, past the 160K handoff line, **before writing F6's
lane-level (`run_lane`) tests** — items 1–8 of the contract's §"F6 tests".

So: F6 is implemented and every existing test passes, but the end-to-end proof that a real
lane retries twice and then parks has not been written yet. **That is the first job here.**

## Read first (binding, in this order)

1. `handoffs/2026-09-17-graph-p12b-s3-design.md` — the S3 design contract. **Binding.**
   §F6 describes what was built; §F7 and §F8 are still ahead.
2. `docs/graph-engineering/specs/phases/P12B_MODEL_FAILURE_HANDLING.md` — §2 F6/F7,
   §3's F6/F7 bullets.
3. `git show 782ea77` — what actually landed.

## What F6 built (verified, at `782ea77`)

- `maestro/workflows/failures.py`: `QUALITY_ATTEMPTS = 3`, `ROLE_DIAGNOSER`,
  `retry_cone(revision, node_id)` (agent → itself; verification handler → nearest upstream
  agent writer + everything on a path between, gate last; everything else → `()`).
- `maestro/control/store.py` `_ADDED_COLUMNS`: `node_states.retry_resets`
  (`INTEGER NOT NULL DEFAULT 0`, deliberately **not** in `apply_repair`'s carry list) and
  `attempts.start_sha` (`TEXT`, added to `_set_attempt_fields`'s allow-list).
- `maestro/control/lifecycle.py`: `override_phase(...)` (closed set: any → `pending`,
  `succeeded` → `failed`; appends `NODE_PHASE_CHANGED` with `"override": reason`;
  `NODE_TRANSITIONS` untouched), `requeue_cone(...)` and `fail_quality(...)`, both one
  transaction. `node_counters` now also returns `retry_resets`.
- `maestro/workflows/runner.py`: `_commit_result` takes a new `cone` branch before the
  ordinary `failed` path → `_commit_quality_failure` (root = `cone[0]`, spends
  `quality_failures`, requeues under budget else `fail_quality`); `_skipped_below` re-opens
  the cone's skipped descendants; `pre_validate` switches agent nodes (and any node with
  `retry_resets > 0`) to `RetryPolicy(max_attempts=QUALITY_ATTEMPTS).permits(retry_resets)`;
  `_soft_exclude` (retry 2 only), `_commit_boundary`, `_restore_start_tree`,
  `_record_routing_decision(..., start_sha=)`, module-level `_git_head`.
- `maestro/workflows/routing.py`: `resolve(..., soft_exclude=frozenset())` — first pass
  `exclude | soft_exclude`, else the plain pass with
  `selection_reason += "; retry 2: no other equal-or-better route, same model reused"`.
  `soft_exclude` is never persisted.
- `maestro/confinement.py`: new `ConfinementError`, and
  `CommitBoundary.restore(sha, *, runner=subprocess.run)` → `git reset --hard` + `git clean -fdq`,
  refusing anything that is not this boundary's own worktree under `.orchestrator/worktrees/`
  and never the repo root.

### Units already written (green)
- `tests/workflows/test_failures.py` — `QUALITY_ATTEMPTS`, six `retry_cone` shapes.
- `tests/control/test_store.py` — `override_phase` allowed/refused/replay-equal,
  `requeue_cone`, `fail_quality`, `start_sha` as attempt telemetry.
- `tests/test_confinement.py` — `restore` happy path, three refusals, a git failure raised.

### Existing tests the new behaviour changed (already updated in `782ea77`)
- `tests/graph_engineering/test_dispatch_wiring.py` `_settled_run` now drives the failure
  `QUALITY_ATTEMPTS` times, because a `failed` agent result is a bad output and is retried.
- `tests/graph_engineering/test_failure_ladder.py` F5 step-down test: its `cmd: "false"`
  gate now resets the cone, so it halts via the new `_halt_once_the_gate_has_failed(...)`
  helper and asserts `gate` attempt `failed` / node phase `pending`, plus "nothing steps
  down twice" across the retry.

## Job 1 — F6's lane tests (`tests/graph_engineering/test_failure_ladder.py`, under `run_lane`)

Write them TDD-style against the already-built behaviour (they are the proof, not the design).
Reuse the helpers at the end of that file: `resolve_spy`, `ladder_spies`, `_implementer_rows`,
`_attempts_by_node`, `_attempts_by_rowid`, `_counters`, `_halt_after`,
`_halt_once_the_gate_has_failed`, `_FakeCliDriver`, `_patch_fixture_cli_driver`,
`_patch_two_drivers`, `_run_implementer_workers`, `_committed_repo`, `_two_route_routing`,
`_strong_and_balanced`. Note `_counters()` selects named columns — add `retry_resets` to it.

The eight the contract asks for:
1. Attempts 1 and 2 end `bad_output` (an empty completion is `empty_output`), attempt 3
   succeeds → the run proceeds to the gate; `quality_failures == 2`; attempt 2's
   `(backend_id, model_id)` equals attempt 1's and its `session_mode == "fresh"`.
2. Two equal routes → attempt 3's model differs from attempt 1's. One route only → attempt 3
   equals attempt 1's and `binding_json.selection_reason` contains
   `"retry 2: no other equal-or-better route"`.
3. A failing gate (`cmd: "false"`) → the next polls re-run the implementer **and** the gate
   (two new attempts each, the gate not alone); `quality_failures` counts on the implementer.
4. Three `bad_output` → implementer `failed`, `park` reached, `quality_failures == 3`.
5. Start SHA: a real temp git worktree wired through a `worktree_for` override; attempt 1
   commits junk; the retry's claim restores `start_sha` (HEAD equals it, junk gone) and the
   repo root is never touched.
6. A blocked failure between two bad outputs spends no retry (`retry_resets` unchanged).
7. Spies: `launch_worker` once per launch, `routing.resolve` once per agent attempt.
8. routing-None: a bad output is written `failed` exactly as today (extend
   `test_an_unrouted_controller_writes_no_failure_columns` or add a sibling).

**Watch out for two harness facts learned the hard way:**
- `MAX_POLLS` is 12 in `tests/graph_engineering/conftest.py`. A retry lane burns polls fast —
  halt on a durable condition (a settled attempt count), never on "the node reached a
  terminal phase", because F6 resets it to `pending`.
- `_run_the_gate_worker` and `_run_the_implementer_worker` both halt on a terminal node
  phase and therefore spin under F6. Use or extend `_halt_once_the_gate_has_failed`.

For test 5, `_restore_start_tree` only acts when `runner.worktree_for(node)` resolves to the
node's **own** `CommitBoundary.for_task(repo, task_id, candidate=...)` worktree — i.e. the
override must return `<repo>/.orchestrator/worktrees/<task_id>`, initialised as a git repo.
Anything else means no restore and `start_sha` stays NULL, by design.

Commit the lane tests as a follow-up to F6 (a `test(graph):` commit is fine — F6's behaviour
is already committed), or amend if that reads better. Then continue.

## Job 2 — F7, from §F7 of the design contract

Only after F6's lane tests are green. §F7 is unchanged and complete: the `diagnose` stage and
`escalation=True` compile, `BASELINE_COMPILER_VERSION`/`p12b.1`, the settle deferral, the
`failure_context` artifact, `strength_floor` + `billing_only`, the repair budget refusal, the
verdicts, the apply-refusal override, the final run, and workflow-scoped step-down history.
Commit F7 alone. Then F8, then the close-out in §"Close-out after F8".

## Invariants (binding)
One dispatch choke point (`advance` → `resolve` once → `claim` → `launch_worker`) and one
fresh-vs-resume decision, with no Context Gate. New behaviour only when `routing is not None`.
`tests/workflows/test_compatibility.py` stays unchanged. Don't edit `NODE_TRANSITIONS`. Leave
`P12A_*`, `AGENTS.md` and `GEMINI.md` alone. Scratch files go in `.scratch/`. A PreToolUse hook
rejects Bash text containing the privileged word. pytest's summary line is suppressed, so read
the counts off the junit root `testsuite`. Commits end with
`Co-Authored-By: <model actually running> <noreply@anthropic.com>`. STATE: targeted
replacements only.

## Verification (report the numbers)
```bash
.venv/bin/python -m pytest -q tests/workflows/test_failures.py tests/graph_engineering tests/backends tests/workflows tests/test_workflow_status.py tests/test_taskgraph*.py tests/control tests/test_confinement.py --junit-xml=.scratch/p12b/gate.xml
.venv/bin/python -m pytest -q --junit-xml=.scratch/p12b/full.xml   # exit 0; run in background, check xml mtime
```
Baseline at `782ea77`: gate tests=1198 failures=0 skipped=0; full tests=4688
failures=0 skipped=1. (The gate command above is wider than S2's — it now
includes `tests/control` and `tests/test_confinement.py`, which F6 touches.)

Also still open: STATE's `in_progress_details` was updated for F6 but the three
`_p12b_open_threads` stay open until F8.
