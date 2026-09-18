# Handoff — Graph P12, session 2b: finish the worktree fix (happy path is red at the gate), then the rest of P12

Continue Phase P12 on `feat/graph-engineering-foundation` (HEAD `ac92f2c`). **The working tree has UNCOMMITTED
work from session 2**, listed in §2. It lives in the repo (persistent storage), not in /tmp. This file is the whole
prompt. Everything you need is here or linked.

## 0. Read first (and only these)
1. `handoffs/2026-09-18-graph-p12-s2-worktree-and-e2e.md`: the full P12 s2 plan. Its §1 (Dan's decisions,
   do not re-ask), §4 (rest of P12), §5 (out of scope), §6 (coordination) and §7 (report back) all still apply
   verbatim. Its §3 is the worktree fix, which is now **half done** (see below).
2. `docs/graph-engineering/specs/phases/P12_QUALIFICATION_AND_RELEASE.md` (short).
3. `git diff` and `git status`: the uncommitted session-2 work.

## 1. Session 2 went over its context budget
It stopped at ~165K tokens in the middle of debugging the first happy-path lane. Nothing was committed.

## 2. Uncommitted work in the tree (review it, then keep it)
- **New `maestro/workflows/task_tree.py`**: `ensure(repo, task_id, candidate=, base=)` creates the worktree at
  `confinement.worktree_for` on `impl/<task>[/<cand>]`. It creates the branch from the run's `code_base_sha`
  (HEAD when empty), reuses a registered tree, checks out an existing branch instead of recreating it, and refuses
  an unregistered directory. `release(..., merged=)` removes a clean tree, keeps a dirty one, and runs
  `branch -d` only when the branch was merged.
- **`maestro/workflows/runner.py`** changes:
  - `TASK_TREE_PERMISSIONS`, `TASK_TREE_RELEASED_EVENT`.
  - `worktree_for` resolves the task tree for writer, reader and merge nodes when the repo is git. A reader gets
    it only once it exists. A declared `params["worktree"]` still wins.
  - `_prepare_task_tree`: writer + git repo only; `async_launch` is excluded because it has no writer lease. It is
    called after the claim and before the launch, in both `_advance_agent` (before `_restore_start_tree`) and the
    generic `advance` path. On failure the claim fails with `worktree_unavailable: …`.
  - `_release_task_trees` runs in `settle()` after `finish_run`. It emits one `TaskTreeReleased` event per tree.
  - `_default_worker_argv` passes `--worktree`.
- **`maestro/workflows/worker.py`**: `task_header(context)` is prepended to the brief. It covers role, task id and
  title, `WORKTREE:`, `WORKSPACE:`, a commit instruction with the branch name (writers only), and "write DONE/FAILED
  into the WORKSPACE". Integration bug found: a real agent_cli agent was never told where its workspace was or how
  to report, so a clean run could only ever be adopted as `uncertain`. The brief also had no task id, title or
  commit instruction.
- **`tests/graph_engineering/test_end_to_end.py`**: new lane
  `test_a_task_runs_to_an_accepted_run_and_the_target_branch_moves`. It uses a git sandbox on `main` (`_git_sandbox`)
  and a fixture agent (`_CommittingAgent`) that reads `WORKSPACE:` from the brief, edits and commits
  `feature.txt` when the prompt starts `You are the implementer `, then writes DONE. `_drive_every_worker` runs
  each live worker from its recorded tmux command. **It still has a temporary `print("POLL", …)` debug trace in
  `_drive_every_worker`. Remove it once the lane passes.**
- The full suite has NOT been run since these edits. Run it first:
  `.venv/bin/python -m pytest -q tests/graph_engineering tests/workflows tests/control`. Then the whole suite.
  Watch `test_failure_ladder.py`: its lanes still append their own `--worktree`. Argparse takes the last
  occurrence, so they should pass, but step 5 of s2 §3 removes that workaround.

## 3. Where the happy lane stands (red)
Per-poll trace: implementer `succeeded` → gate `failed` (`required check 'V1' is failed/current`, cmd
`test -f feature.txt`) → the implementer is retried twice (F6), with the same result each time → `diagnose` refused
`route_no_eligible_route` (the fixture catalog has only a `balanced` model and the diagnoser floor is `strong`) →
`park` stays `claimed` forever, and the lane hits `MAX_POLLS` (12).

What is proven: `.orchestrator/worktrees/TASK-001` exists as a registered worktree. So `ensure` works.
Last inspected state: that tree held only the `seed` commit. The test's agent-detection string was wrong at that
point. It was fixed, but the gate still fails on the rerun, and the cause was not inspected. **Next steps, in order:**
1. After a rerun, check whether `feature.txt` is committed in the tree
   (`/tmp/pytest-of-dan/pytest-current/test_a_task_runs_to_an_accepte0/repo`). If it is not committed, check
   `_CommittingAgent.complete` (the prompt prefix; `spec.cwd` may be the inbox if `--worktree` is missing from the
   argv). If it is committed, look at where the gate ran its check: the verification handler's cwd for
   `ctx.worktree`, and `_select_affected_checks`. Note that `code_base_sha` is `""` in lanes, because the
   orchestrator's `subprocess` is faked, so `head_sha` returns empty.
2. Then the next hops: proof_review (an agent node; the fixture agent writes DONE for it too), merge (inline,
   `candidate_worktree=worktree_for(merge)`, target `main`, `specs=[]` because there is no inputs_provider; check
   that this is acceptable), accept, `settle` → `release`.
3. **Suspected second bug (verify):** a run whose `park` node is `claimed` never settles. `settle` →
   `_still_working` sees `claimed`. If a parked run can never finish, that is a lane of its own under s2 §4(c).
4. `diagnose` being unroutable in the fixture catalog is expected. For lanes that need it, add a `strong` model
   profile to the fixture entry.

## 4. Other findings to record in STATE `_p12_open_threads` (not yet written)
- **Candidate winner is never merged:** `merge_handler` integrates `params/inputs["branch"]` or `impl/<task>`, and
  nothing feeds it the `candidate_select` winner's branch or tree. A widened (candidates) task cannot land.
  Owner: P12 or later. Decide whether it is in P12 scope; it is not on the s2 checklist.
- **`scripts/next_graph_prompt.py` prints `relevant_context` one character per bullet**, because it iterates a
  string. This is a small fix; do it in session.
- New `worktree_unavailable:` refusal and `TaskTreeReleased` event need a line in docs/EXECUTION.md (s2 §4 docs).

## 5. Then
Continue with s2 §3 steps 4–5 (cleanup is implemented; remove the ladder's `--worktree` workaround), then s2 §4
(the fixture-mode happy paths, the fault matrix, `scripts/graph_qualification.py`, B15 retention, docs), then write
the s3 pilot handoff (s2 §5), and report per s2 §7. Commit in small steps: worktree fix + lane first, one commit
per integration bug the happy path finds.

## 6. Coordination
No other session is known to be writing maestro. Edit STATE.yaml with targeted replacements only. Budget: prepare a
handoff at 160K (context-governor).
