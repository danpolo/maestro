# P12B S3 — F7 (escalation to a stronger diagnoser), then F8 and the close-out

**This file is the complete brief.** Dan starts the session with nothing but
`continue from handoffs/2026-09-18-graph-p12b-s3-f7-diagnoser.md`. No other prompt follows.

Working directory `/home/dan/projects/maestro`, branch `feat/graph-engineering-foundation`
(**no upstream**; `git pull` fails, expected). No parallel session is active on this branch.

## Where the previous session stopped

F6 is **complete**: behaviour and units landed as `782ea77`, and its nine `run_lane` proofs
landed as `b8ef073` (`test(graph): P12B F6 — the retry ladder proven under a real lane`).
Working tree clean. Nothing from F6 is outstanding.

**F7 has not been started.** That is this session's first and probably only job.

## Read first (binding, in this order)

1. `handoffs/2026-09-17-graph-p12b-s3-design.md` — the S3 design contract. **Binding.**
   §F7 is complete and unchanged; §F8 and §"Close-out after F8" follow it.
2. `docs/graph-engineering/specs/phases/P12B_MODEL_FAILURE_HANDLING.md` — §2 F7, §3's F7 bullets.
3. `git show 782ea77` and `git show b8ef073` — the ladder F7 escalates out of.

Do **not** re-derive the design. §F7 of the contract is the specification; implement it.

## Job 1 — F7, from §F7 of the design contract

All of it, in one commit: the `diagnose` stage and `escalation=True` compile,
`BASELINE_COMPILER_VERSION` / `p12b.1`, the settle deferral, the `failure_context` artifact,
`strength_floor` + `billing_only`, the repair-budget refusal, the verdicts, the apply-refusal
override, the final run, and workflow-scoped step-down history. Seven lane tests plus the
units listed there. **Commit F7 alone** when green.

### What F6 leaves you to build on (verified at `b8ef073`)

- `maestro/workflows/failures.py`: `QUALITY_ATTEMPTS = 3`, `ROLE_DIAGNOSER = "diagnoser"`,
  `retry_cone(revision, node_id)` — a diagnoser's cone is `()`, so **the diagnoser is already
  never retried**; F7 does not need to add that.
- `maestro/control/lifecycle.py`: `override_phase(...)` already permits `succeeded → failed`,
  which is exactly the move F7's apply-refusal override needs. `requeue_cone`, `fail_quality`,
  and `node_counters` (now including `retry_resets`).
- `maestro/workflows/runner.py`: `_commit_quality_failure` is where the third `bad_output`
  becomes `fail_quality`, which is what opens the node's `on_failure` edge — in F7 that edge
  points at `diagnose` instead of `park`. `pre_validate` keys the budget off `retry_resets`,
  which `apply_repair` deliberately does **not** carry, so F7's final run dispatches once.
- `maestro/control/store.py` `_ADDED_COLUMNS`: `node_states.retry_resets`, `attempts.start_sha`.

### Test harness facts, learned the hard way — read before writing a lane test

These cost two sessions between them. `tests/graph_engineering/test_failure_ladder.py` now
carries helpers for all of it at the end of the file; reuse them rather than rebuilding.

- **`MAX_POLLS` is 12** (`tests/graph_engineering/conftest.py`). A ladder lane burns polls
  fast. F7's lanes are longer than F6's — three bad outputs, then `diagnose`, then possibly a
  repaired run — so budget the polls before writing the lane, and halt on a **durable**
  condition (an attempt count, a row that exists), never on "the node reached a terminal
  phase": F6 resets a failed writer's cone to `pending`, and a lane watching the phase spins
  to the limit.
- **`_ScriptedCliDriver(*answers)`** — a fake `agent_cli` driver with one
  `(Completion, ExitVerdict)` per call, the last repeating. This is how one node answers badly
  twice and then well. `_EMPTY` classifies `empty_output` (needs `--worktree` pointing at a
  real git repo — use `_committed_repo(tmp_path / "wt")`); `_GOOD` plus a `DONE` sentinel in
  the attempt workspace is how an adopted agent answer becomes `succeeded` rather than
  `uncertain`.
- **`_drive_implementer(predicate, *, worktree=, succeed_from=)`** — runs the newest live
  implementer worker each poll until `predicate()` holds. For a diagnoser lane you will want
  a sibling that drives whichever node is live, or `_run_the_gate_worker` (in
  `test_dispatch_wiring.py`), which runs the gate for real and succeeds everything else.
- **`_halt_when(predicate, each)`**, `_retry_rows()`, `_settled_implementer()`,
  `_counters(node_id="implementer")` (now selects `retry_resets`), `_attempts_by_node`,
  `resolve_spy` / `ladder_spies` (note: `ladder_spies["resolve"]` counts **`router.resolve_agent`**,
  which fires twice in one `routing.resolve` when a soft-exclude pass misses — use
  `resolve_spy["routing"]` for "once per attempt").
- **Prove the tests aren't vacuous.** F6's lane tests were checked by mutating
  `QUALITY_ATTEMPTS` to 1 and confirming all nine failed. Do the equivalent for F7 (e.g.
  compile with `escalation=False` and confirm the escalation lanes fail) and say so in the
  commit message.
- `worktree_for` is overridable per test by monkeypatching `wf_runner.WorkflowRunner.worktree_for`;
  `_restore_start_tree` only acts when it resolves to the node's own
  `CommitBoundary.for_task(repo, task_id, candidate=...)` worktree under
  `.orchestrator/worktrees/`.

## Job 2 — F8, from §F8 of the design contract

Only after F7 is green and committed. §F8 is small and unchanged: `_decide_dead_writers(at)`
in `tick` right after `reconcile`, under routing only, reading durable state rather than the
reconcile report; candidates go to `failed` + `record_failure` with no `outcome_ref`, other
writers take F6's path. Three lane tests. **Commit F8 alone.**

## Job 3 — the close-out, from §"Close-out after F8"

Close the three `_p12b_open_threads` (disposition `closed` + a note naming the commit), check
`owned_threads(state, "P12B") == 0`, record P12B in STATE `completed_phases` / `phase_records`,
reset `in_progress_details` to null per its comment, and move `current_phase` on to P12 —
`scripts/next_graph_prompt.py` should then print P12.

## Invariants (binding)

One dispatch choke point (`advance` → `resolve` once → `claim` → `launch_worker`) and one
fresh-vs-resume decision, with no Context Gate. New behaviour only when `routing is not None`.
`tests/workflows/test_compatibility.py` stays unchanged and an unescalated compile stays
byte-identical. Don't edit `NODE_TRANSITIONS`. The diagnoser never uses the hybrid overlay
(Dan, 2026-09-17). Leave `P12A_*`, `AGENTS.md` and `GEMINI.md` alone. Scratch files go in
`.scratch/`. A PreToolUse hook rejects Bash text containing the privileged word. pytest's
summary line is suppressed, so read the counts off the junit root `testsuite`. Commits end
with `Co-Authored-By: <model actually running> <noreply@anthropic.com>`. STATE: targeted
replacements only. If a step can only be built by bypassing the choke point or adding a
second resume decision, stop and ask Dan (spec §4).

## Verification (report the numbers)

```bash
.venv/bin/python -m pytest -q tests/workflows/test_failures.py tests/graph_engineering tests/backends tests/workflows tests/test_workflow_status.py tests/test_taskgraph*.py tests/control tests/test_confinement.py --junit-xml=.scratch/p12b/gate.xml
.venv/bin/python -m pytest -q --junit-xml=.scratch/p12b/full.xml   # exit 0; run in background, check xml mtime
```

Baseline at `b8ef073`: **gate tests=1207 failures=0 skipped=0; full tests=4697 failures=0
skipped=1.** Report per commit: hash, gate and full junit counts.

## Budget

F7 is the largest of the three Fs. Measure context with
`python3 ~/.claude/skills/close-session/scripts/context_usage.py` after each commit; for
Opus 5, write a handoff at 160K and start fresh by 200K. One F per session is fine — if F7
lands and the budget is spent, hand off F8 + the close-out rather than starting them.
