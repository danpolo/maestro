# Handoff — Graph P12, session 2: the graph-loop worktree gap, then the rest of P12

Continue Phase P12 on `feat/graph-engineering-foundation` (HEAD at hand-off: `ac92f2c`, working tree clean
apart from this handoff and STATE.yaml). This file is the whole prompt: everything you need is here or linked.

## 0. Read first (and only these)
1. `docs/graph-engineering/specs/phases/P12_QUALIFICATION_AND_RELEASE.md` (the phase spec, short).
2. `docs/graph-engineering/specs/phases/MIGRATION_GUARDRAILS.md`.
3. `docs/graph-engineering/STATE.yaml`: `in_progress_details` and `_p12_open_threads` (search for them; do
   not read the whole 1300-line file).
4. `.venv/bin/python scripts/next_graph_prompt.py`: the phase directive (§4 checklist, §6 close-out rules).
Secondary, only if needed: `docs/graph-engineering/specs/07_EXPERIMENTS_AMENDMENTS_AND_DISPOSITION.md`
(E1–E7 table: every uncertain optimisation needs an enabled/disabled decision backed by evidence).

## 1. Decisions Dan made in session 1 (2026-09-18). Do not re-ask
- **Pilots run later, in their own session.** This session (s2) does the maestro-side code only. Do NOT dispatch
  any real claude/codex/agy task against DuetFlow or instagram-to-value here.
- **instagram-to-value:** Dan runs `grill-me` himself (the skill was renamed from `grill-with-docs`) and will
  report when it's done. Do not onboard it and do not run maestro-setup on it here.
- **ITV daemons** (`itv-bot`, `itv-worker` tmux sessions) keep running during the later pilot, protected by config
  guards: `bot_files: ["scripts/bot.py", "scripts/worker.py"]`, `deny_list_extra` adds
  `jobs/ staging/ media/ out/ logs/ config/`, `gate.chain: [test]`.
- **Modes the real pilots qualify: paid subscription only.** Free, hybrid and the script lane stay labeled
  *unqualified* for real use. Fixture lanes may still exercise them, but a fixture pass never claims production
  readiness (P12 §3).

## 2. Done in session 1
- `ac92f2c` closes `_p12b_open_threads` #4: a controller error decides its node and leaves a durable record.
  `WorkflowRunner.tick` wraps each `advance` call. Before any launch, the claim is failed with
  `controller_error: <Type>: <msg>` as the route refusal, so the `on_failure` edge runs. After a launch, the node
  is left to its worker. Either way a `ControllerError` store event (`runner.CONTROLLER_ERROR_EVENT`) keeps the
  traceback tail. Errors outside `advance` (ingest/reconcile/settle) become one `GraphTickFailed` run event per
  distinct error (`orchestrator._record_graph_tick_failure`), plus the existing `graph_tick_failed` journal line.
  Lanes: `tests/graph_engineering/test_end_to_end.py`, 4 tests, all green. Also green:
  `tests/graph_engineering tests/workflows tests/control`.
  - Note for the thread's close note (already written in STATE): a controller-error failure takes the same
    `on_failure` → `diagnose` edge as every other route refusal (W9 oversized, F5 blocked-wait). That is consistent
    with existing behaviour, but it means a controller bug can launch a diagnoser. This is recorded as an accepted
    design note, not changed.

## 3. The blocker found in session 1: the graph loop has no task worktree (`_p12_open_threads`, owner P12)
**Evidence (read from code, not executed):**
- `WorkflowRunner.worktree_for(node)` (`maestro/workflows/runner.py`, search `def worktree_for`) returns
  `node.params["worktree"]`, and the compiler never sets that param. Compiled params for an ordinary task are only
  `task_id`/`title`.
- `_default_worker_argv` never passes `--worktree`. Nothing in the graph path calls `git worktree add` (grep it).
- So the implementer's CLI runs in `.orchestrator/attempts/<attempt>/` (its inbox, not a git checkout). The gate
  runs checks in that inbox. `merge_handler` integrates branch `impl/<task>`, which never exists.
  **No code-changing task can reach `accept` under `engineering.runner: graph`.**
- The P12B lanes papered over this: `tests/graph_engineering/test_failure_ladder.py` passes
  `argv += ["--worktree", str(worktree)]` in its `on_poll`.
- This is a wiring gap, not design drift. A08 and INV-05 already fix the branch name (`impl/<task>`) and
  `maestro/confinement.py::worktree_for(repo, task_id, candidate=)` already fixes the path
  (`<repo>/.orchestrator/worktrees/<task>[/<candidate>]`). Fix it; do not escalate unless the fix needs a
  D01–D08 change.

**Fix to build (TDD, lanes under the real `orchestrator.main` via `run_lane`, whose sandbox you `git init` in
`before_run`):**
1. When a `task_worktree_writer` node is claimed (implementer; candidates use `candidate=`), ensure the worktree
   exists at `confinement.worktree_for(repo, task_id[, candidate])` on branch `impl/<task>` (candidates:
   `CandidateSpec` branch in `maestro/workflows/candidates.py`), created from the run's `code_base_sha`. Reuse it on
   retry. P12B F6 `_restore_start_tree` / `_commit_boundary` already assume a tree, so read them first.
   `maestro/worktree.py::create_worktree` is the legacy helper, but it deletes an existing tree and uses `REPO` and
   `/tmp` prefixes. Do not delete a retry's tree. Write a graph-side helper, or parameterise the existing one.
2. Make `worktree_for(node)` return that path for every node that reads the task's tree: implementer, gate
   (`frozen_snapshot_reader`), proof_review, diagnose, and merge (inline, `candidate_worktree=ctx.worktree`). Pass
   `--worktree` in `_default_worker_argv`.
3. The worktree must hold a **commit**, or merge's freeze refuses uncommitted changes
   (`test_a_candidate_worktree_with_uncommitted_changes_cannot_be_frozen`). Check who commits in the legacy path
   (the agent is told to commit; see `maestro/implementer.py` brief) and that the graph `agent_brief`
   (`maestro/workflows/worker.py::agent_brief`) tells it the same, including the task's title and notes. If the
   brief lacks the task text, that is the same gap: fix it.
4. Cleanup after accept/park: remove the worktree (never on failure mid-run; F6 retries reuse it).
5. Remove the `--worktree` workaround from `test_failure_ladder.py` once the runner supplies it.

## 4. Then the rest of P12 (the directive's §4 checklist)
- **`tests/graph_engineering/test_end_to_end.py`** (file exists, extend it). Needed:
  (a) a **happy path to `task_runs.status='succeeded'` + an acceptance decision + target branch fast-forwarded**,
  driven through `orchestrator.main`. No test does this today. Workers are run in-process from the recorded tmux
  command, as `test_failure_ladder._run_the_implementer_worker` does, with a fake `agent_cli` driver
  (`_patch_fixture_cli_driver`) whose `complete()` edits and commits in `spec.cwd`. The ROADMAP task carries
  `verifications: [{id: V1, kind: auto, cmd: ...}]` (format: `tests/test_integration_acceptance.py::test_the_verification_node_reports_evidence_the_controller_can_ingest`).
  Expect to find more integration bugs on this path; each one gets a lane.
  (b) the same happy path under each **fixture** mode: subscription `agent_cli` (fixture_cli_entry), free
  (`FREE_ENDPOINT` in test_dispatch_wiring + the model runtime), hybrid (both routes in one catalog, e.g. the
  implementer paid and the reviewer free under a free-quality objective), script lane.
  (c) the **fault matrix**, one lane each, reusing existing mechanisms where they exist:
  transaction rollback (store transaction raises mid-commit → no partial state; see tests/control),
  lost controller (kill after claim, new `main()` resumes via `graph_run_resumed`; see
  `test_restart_resumes_the_open_run_instead_of_opening_another`), writer crash (F8 lanes in test_failure_ladder),
  quota switch (F4 `test_a_quota_exhausted_route_pauses_its_pool_and_the_next_poll_routes_elsewhere`),
  interrupted integration (merge node crashes after staging, before fast-forward → target untouched, rerun
  converges; see `maestro/merge.py::integrate_candidate` stages).
  The e2e file should *reach* each fault through a full task, not duplicate component tests.
- **`scripts/graph_qualification.py`**: `--repo <dir> --fixture-backends` builds a scratch git repo at `<dir>`, runs
  the six-class catalog (`tests/graph_engineering/fixtures/cases.json`: bug, feature, documentation, debugging,
  manual-preparation, recovery) through the graph loop with fixture backends, and asserts the 12 invariants
  (README §3 INV-01..12) where each can be checked. It writes a JSON report under
  `artifacts/graph-engineering/p12-qualification.json`: per-attempt raw telemetry (model id, agent version, session
  id, fresh/resumed, token split, quota windows at start and settle, wall time, reset-crossing, rework/reopen
  amendments, verification pass/fail; unmeasurable → null; NOT the Context Gate JSONL schema), artifact/transcript
  bytes, the E1–E5 enabled/disabled table with its evidence (fixture evidence → "stays off / unqualified" unless a
  real measurement exists), and a short **Context Gate mapping** section (Goal / WorkUnit / Session /
  ContextSnapshot / GateDecision / OutcomeRecord → task, run, node, attempt, `Usage`, outcome). **Refuse a
  non-scratch `--repo`** (exists and is not empty/not created by the script, or is under `~/projects`) unless
  `--deployment` is given. Exit 0 only if every assertion passes. Verification:
  `.venv/bin/python -m pytest -q && .venv/bin/python scripts/graph_qualification.py --repo /tmp/maestro-graph-qualification --fixture-backends`.
- **B15 retention**: measure bytes under `.orchestrator/artifacts`, `attempts/*`, `impl.log` during the fixture run;
  set `maestro init`'s retention default from it (find the current retention setting first:
  `grep -rn retention maestro/`). The real-pilot measurement later may revise it; record that as a P12 thread.
- **Docs**: `docs/DESIGN.md`, `docs/EXECUTION.md`, `docs/PROGRESS.md`: graph-loop worktree lifecycle,
  controller-error handling, qualification script, retention default, and what stays unqualified.
- **Context Gate seams** (P12 §4): if anything forces a second wrapper process or a gate bypass, record it under
  `_p12_open_threads` with `disposition: needs-dan`.

## 5. Out of scope for s2
- Real pilots (DuetFlow, instagram-to-value): session s3, after Dan reports grill-me done on ITV. s2's last act
  is to write the s3 pilot handoff. It uses the decisions in §1, pins criterion weights and source revisions
  before any run, and retains raw per-attempt outcomes. The DuetFlow roadmap has 9 open tasks in
  `/home/dan/projects/duetflow/docs/ROADMAP.md`; the project must be switched to `engineering.runner: graph`.
- The Context Gate itself (never, in any graph phase).

## 6. Coordination
- No other session is known to be writing maestro right now. `git pull` is not needed (there is no remote
  tracking for this branch). Edit STATE.yaml with targeted replacements only.
- Memory: put agent worktrees and uncommitted agent work on persistent storage, not /tmp. The qualification
  `--repo /tmp/...` is a scratch *target*, which is fine.

## 7. Report back (with numbers)
- Full suite: `junit tests=… failures=0` and the exit code.
- The qualification script's exit code, and its report's counts: tasks run per class, invariants checked/passed,
  total artifact bytes, and the retention default chosen.
- Every integration bug the happy-path lane found, one line each with its commit.
- STATE.yaml: P12 still `in_progress` (pilots remain) with `whats_left` = the pilots, unless Dan says otherwise.
- Close with the `close-session` skill.
