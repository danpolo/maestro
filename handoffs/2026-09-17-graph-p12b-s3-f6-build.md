# P12B S3 — build F6 (`bad_output` → two retries)

**This file is the complete brief.** Dan starts the session with nothing but
`continue from handoffs/2026-09-17-graph-p12b-s3-f6-build.md`. No other prompt follows.

Working directory `/home/dan/projects/maestro`, branch `feat/graph-engineering-foundation`
(**no upstream**; `git pull` fails, expected). HEAD should be F5 `929248a` with a clean tree
(`git log -3 --oneline && git status --short`). No parallel session is active on this branch.

## Why this file exists
The previous session wrote the S3 design contract,
`handoffs/2026-09-17-graph-p12b-s3-design.md`. By then it had used 165K tokens, past the 160K
handoff line (most of that was the system prompt and the code reads), so it did not start F6.

## What to do
1. Read `handoffs/2026-09-17-graph-p12b-s3-design.md` **in full**. It is binding. It has the
   verified code facts with line numbers, F6/F7/F8 sections, invariants and verification.
   Also read spec §2 F6 and §3's F6 bullet:
   `docs/graph-engineering/specs/phases/P12B_MODEL_FAILURE_HANDLING.md` (~:97–103, ~:136–140).
2. Build **F6 only**, as the contract's §F6 describes: `failures.QUALITY_ATTEMPTS` and
   `retry_cone`; `node_states.retry_resets` and `attempts.start_sha`; `Lifecycle.override_phase`,
   `requeue_cone` and `fail_quality`; the `_commit_result` branch; the `pre_validate` switch to
   `retry_resets`; retry-2 `soft_exclude` inside `routing.resolve`; `CommitBoundary.restore`
   and the restore at claim time. Write the tests first (TDD), under `run_lane`, reusing the
   helpers at the end of `tests/graph_engineering/test_failure_ladder.py` (`resolve_spy`,
   `_implementer_rows`, `_attempts_by_node`, `_halt_after`, `_FakeCliDriver`,
   `_patch_fixture_cli_driver`).
3. Run the gate and the full suite, then commit F6 alone:
   `feat(graph): P12B F6 — bad output retries twice, fresh then on another model`.
4. Update STATE `in_progress_details` with a targeted replacement: F6 done with its hash and
   numbers, F7 next.
5. Measure context. Under 120K: start F7 from the contract. Otherwise write
   `handoffs/<date>-graph-p12b-s3-f7-build.md` (a complete brief like this one) and stop.

## Lane facts learned in S2 (still true)
- `node_states` has a row only after a node moves. To seed a never-moved node, use
  `INSERT OR IGNORE … phase 'ready'` inside `store.transaction()`.
- The manifest carries no task `risk`. To force `strong` demand, use the role's
  `permitted_backend_capabilities["min_reasoning_strength"]`.
- A failed node reaches `park` on a later poll, so don't halt the lane on the failure itself.
- The graph gate runs the real `gates.run_authoritative_gate`, so a verification with
  `cmd: "false"` fails it (use this for the gate-cone test).

## Invariants
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
.venv/bin/python -m pytest -q tests/workflows/test_failures.py tests/graph_engineering/test_failure_ladder.py tests/graph_engineering/test_dispatch_wiring.py tests/backends tests/workflows tests/test_workflow_status.py tests/test_taskgraph*.py --junit-xml=.scratch/p12b/gate.xml
.venv/bin/python -m pytest -q --junit-xml=.scratch/p12b/full.xml   # exit 0; run in background, check xml mtime
```
Baseline (F5, `929248a`): gate tests=915 failures=0; full tests=4672 failures=0 skipped=1.
Report the F6 hash, both junit counts (gate count must grow by the new tests), and the
measured context.
