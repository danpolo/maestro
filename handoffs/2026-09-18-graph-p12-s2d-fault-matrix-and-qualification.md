# Handoff — Graph P12, session 2d: the rest of the fault matrix, then the qualification script

Continue Phase P12 on `feat/graph-engineering-foundation`. The working tree is clean. This file is the whole prompt.

## 0. Read first (and only these)
1. `handoffs/2026-09-18-graph-p12-s2-worktree-and-e2e.md`: its §1 (Dan's decisions, do not re-ask), §4 (rest of
   P12: the fault-matrix list with the mechanisms to reuse, the qualification-script spec, B15, docs), §5 (out of
   scope), §6 (coordination) and §7 (report back) all still apply. Its §3 is DONE.
2. `docs/graph-engineering/specs/phases/P12_QUALIFICATION_AND_RELEASE.md` (short).
3. `docs/graph-engineering/STATE.yaml`: `in_progress_details` and `_p12_open_threads`. Search for them. Do not read
   the whole file.
4. `tests/graph_engineering/test_end_to_end.py`, from `# ── the happy path` to the end. It is the template for every
   lane you add. The last section, `# ── the fault matrix`, has the first fault lane.

## 1. Done in sessions 2b and 2c (commits)
- 2b: `387e902` task worktree, `55dcd15` merge/accept specs, `55f6d5e` parked run settles. Suite then: 4722/0/0.
- 2c `08e3a02`: **free mode could never land a task.** The model-endpoint harness has four tools (read_file,
  write_file, run_test, grep) and no shell, so it wrote the tree but nothing committed it, and merge's freeze
  refused. The worker now commits a writer's tree on a clean finish, authored `maestro harness`
  (`task_tree.commit_all`, `task_tree.HARNESS_AUTHOR`). The ToolBox boundary now also takes the candidate id.
- 2c `afd677e`: **the script lane could never land a task that changes a file.** The graph `script_handler` never
  committed; the legacy loop did. It now commits through `task_tree.commit_all`. Both commit sites skip a
  `--worktree` that is not a git tree (the `test_failure_ladder.py` seam).
- 2c: the hybrid lane needed no code change. Hybrid is not an objective: it is `free_quality` plus
  `policy.hybrid = HybridPolicy(paid_roles=...)`, and paid_roles takes **role ids** (`reviewer`, `diagnoser`,
  `implementer`, `planner`; see `policy.ROLE_FOR_FRAGMENT`), not node ids. Without the grant, free mode spends
  nothing paid and a `strong`-floored role has no route.
- 2c `e087520`: fault lane 1/5, **lost controller** after the implementer claim. A fresh `main()` resumes the run,
  adopts the same attempt and lands. No bug. The mode lanes: `..._free_route_...`, `..._hybrid_catalog_...`,
  `..._script_task_...`. e2e file: 10 tests, all green. `tests/graph_engineering tests/workflows tests/control`
  were green after afd677e. **The full suite was NOT run in 2c.** Run it first.

## 2. Traps (in addition to s2c's)
- The `attempts` table has `backend_id`, `binding_json`, `route_refusal`. It has no `billing_category` column.
- `CapabilityCatalog.entries` is a mapping, so use `.entries.values()`.
- To change a lane's `on_poll` between two `main()` runs: `run.scenario = dataclasses.replace(run.scenario, ...)`,
  then `HALT_FILE.unlink()`, `maestro.state._reset_control_store()`, `run.run()` (see the lost-controller lane).
- For a quick per-node view while debugging, print
  `SELECT node_id, status, backend_id, route_refusal FROM attempts ORDER BY rowid`. Do not commit the debug hook.
- There is no `python` on PATH. Use `python3` or `.venv/bin/python`.

## 3. In scope, in order
1. The remaining four fault lanes, one each, reached through a full task (s2 §4(c) names the mechanism to reuse):
   **writer crash** (F8 in test_failure_ladder), **quota switch** (F4
   `test_a_quota_exhausted_route_pauses_its_pool_and_the_next_poll_routes_elsewhere`), **interrupted integration**
   (merge crashes after staging, before the fast-forward: target untouched, rerun converges;
   `maestro/merge.py::integrate_candidate`), **transaction rollback** (a store transaction raises mid-commit: no
   partial state; see tests/control). Each bug found gets its own commit.
2. `scripts/graph_qualification.py` as specified in s2 §4. Count `acceptance_decisions` per run; there are two for
   each accepted run (see `_p12_open_threads`). The four fixture modes now all reach accept. Label every mode
   except paid subscription *unqualified* (s2 §1).
3. B15 retention: measure bytes during the fixture run, then set `maestro init`'s default from it.
4. Docs (DESIGN, EXECUTION, PROGRESS), including the docs thread in `_p12_open_threads`, which now also names the
   harness/script commit and the paid_roles role ids.
5. Decide two open threads. Fix each if it is small; otherwise record the owner and set `disposition: needs-dan`:
   (a) the candidate winner is never merged; (b) new in 2c: the gate verifies the working tree, not a commit.
6. Write the s3 pilot handoff (s2 §5). Report per s2 §7. Close with `close-session`.

## 4. Out of scope
Real pilots (DuetFlow, instagram-to-value). The Context Gate. Converting the `test_failure_ladder.py` lanes that pass
`--worktree` into non-git sandboxes: that flag is a documented test seam.

## 5. Verification to report
- `.venv/bin/python -m pytest -q -p no:randomly --junitxml=<scratch>/junit.xml`: tests/failures/errors.
- `.venv/bin/python scripts/graph_qualification.py --repo /tmp/maestro-graph-qualification --fixture-backends`:
  the exit code, tasks per class, invariants checked and passed, total artifact bytes, and the retention default.
- One line per integration bug, with its commit. Include 2c's: `08e3a02` (free), `afd677e` (script).

## 6. Coordination
No other session is known to be writing maestro. Edit STATE.yaml with targeted replacements only. Keep work on
persistent storage and commit early. Budget: the context-governor thresholds (handoff at 160K). The session starts
at about 50K and 2c reached 125K after 3 mode lanes and 1 fault lane, so measure after each lane with
`python3 ~/.claude/skills/close-session/scripts/context_usage.py`. If the fault lanes run long, hand off before
the qualification script instead of starting it.
