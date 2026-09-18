# Handoff — Graph P12, session 2c: fixture-mode happy paths, fault matrix, qualification script

Continue Phase P12 on `feat/graph-engineering-foundation`. The working tree is clean. This file is the whole prompt.

## 0. Read first (and only these)
1. `handoffs/2026-09-18-graph-p12-s2-worktree-and-e2e.md`: its §1 (Dan's decisions, do not re-ask), §4 (rest of
   P12), §5 (out of scope), §6 (coordination) and §7 (report back) all still apply. Its §3 (worktree fix) is DONE.
2. `docs/graph-engineering/specs/phases/P12_QUALIFICATION_AND_RELEASE.md` (short).
3. `docs/graph-engineering/STATE.yaml`: `in_progress_details` and `_p12_open_threads`. Search for them. Do not read
   the whole file.
4. `tests/graph_engineering/test_end_to_end.py`: the bottom half, from `# ── the happy path`. It is the template
   for every lane you add.

## 1. Done in session 2b (commits)
- `387e902` The graph loop gives each task its own worktree. `maestro/workflows/task_tree.py` has `ensure` and
  `release`. `worktree_for` covers the writer, the readers and merge. The worker argv gets `--worktree`. The brief
  header (`worker.task_header`) now carries the task id and title, WORKTREE, the commit instruction, and, for
  agent_cli only, the DONE/FAILED sentinel line.
- `55dcd15` Merge and accept got no `specs`, so no task could be accepted. They now default to
  `gates.authoritative_specs`. Accept is also bound to merge's `merged_commit` (`expected_snapshot`).
- `55f6d5e` A parked run never settled: `park`, an inline wait node, stays `claimed`. `_still_working` now ignores
  controller-executed nodes.
- `scripts/next_graph_prompt.py`: a string `relevant_context` is no longer printed one character per bullet.
- Full suite after these commits: `tests=4722 failures=0 errors=0`, exit 0.

## 2. How the happy lane works (reuse it)
`_happy_scenario(lane, **overrides)` builds a git sandbox on `main` (`_git_sandbox`), one task with
`V1: test -f feature.txt`, `routing=_strong_routing` (proof_review and diagnose floor at `strong`), and
`on_poll=_drive_every_worker`, which runs each live worker in-process from its recorded tmux command. The fixture
agent `_CommittingAgent` reads `WORKSPACE:` from the brief and commits `feature.txt` when the prompt starts
`You are the implementer `. Traps that already cost time:
- A verification `expect` field means "the last stdout line equals". It is not an exit-code spec. Omit it for
  exit-status checks.
- `-k happy` matches nothing. Test names are `..._accepted_run_...` and `..._parked_run_...`.
- Every accepted run has TWO `acceptance_decisions` rows (merge's, then accept's). See `_p12_open_threads`.
- The model-endpoint harness brief has no WORKSPACE or sentinel lines (`reports_by_sentinel=False`). A free-mode
  lane must make the harness itself commit in the worktree: check how `_run_model_endpoint` gives the model tools
  and a cwd before you assume it can.

## 3. In scope, in order (s2 §4, unchanged apart from what is done)
1. The happy path under the **free**, **hybrid** and **script-lane** fixture modes. Subscription is done. Each
   mode's first run will probably find another integration bug. Each bug gets its own lane and its own commit.
2. The **fault matrix**, one lane each, reached through a full task: transaction rollback, lost controller, writer
   crash, quota switch, interrupted integration (s2 §4(c) lists the existing mechanisms to reuse).
3. `scripts/graph_qualification.py`, as specified in s2 §4 (the six-class catalog, invariants, raw telemetry,
   the E1–E5 table, the Context Gate mapping, and refusal of a non-scratch repo).
4. B15 retention: measure bytes during the fixture run, then set `maestro init`'s default from it.
5. Docs (DESIGN, EXECUTION, PROGRESS), including the docs thread in `_p12_open_threads`.
6. Decide the "candidate winner is never merged" thread. Fix it if it is small. If not, record the owner and leave
   `disposition: needs-dan`.
7. Write the s3 pilot handoff (s2 §5). Report per s2 §7. Close with `close-session`.

## 4. Out of scope
Real pilots (DuetFlow, instagram-to-value). The Context Gate. Converting the 11 `test_failure_ladder.py` lanes that
pass `--worktree` into a non-git sandbox: that flag is now a documented test seam there, not a workaround.

## 5. Verification to report
- `.venv/bin/python -m pytest -q -p no:randomly --junitxml=<scratch>/junit.xml`: tests/failures/errors.
- `.venv/bin/python scripts/graph_qualification.py --repo /tmp/maestro-graph-qualification --fixture-backends`:
  the exit code, tasks per class, invariants checked and passed, total artifact bytes, and the retention default.
- One line per integration bug, with its commit.

## 6. Coordination
No other session is known to be writing maestro. Edit STATE.yaml with targeted replacements only. Keep work on
persistent storage and commit early. Budget: the context-governor thresholds (handoff at 160K). This session's
system prompt alone costs about 130K, so measure early with
`python3 ~/.claude/skills/close-session/scripts/context_usage.py`.
