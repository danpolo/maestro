# Handoff: Graph P12, session 3n. Integrate s3m's five branches, recover 04 via `ctl remerge`, relaunch the pilot

Continue Phase P12 on `feat/graph-engineering-foundation` (maestro). This file is the whole prompt. It
**supersedes** `handoffs/2026-09-26-graph-p12-s3m-parallel-merge.md`. That file's §2 (D20 evidence + Dan's decision),
§3.3 (contamination design), §3.4 (then-steps), §4 (traps) and §5 (report format) still apply; read them. Also
still apply: s3j §2, §4, §5 and s3l §1, §3, §4 (s3m §0 lists exactly which). **Talk to Dan in local time (IDT = UTC+3).**

## 1. What s3m did (2026-09-26, 22:40–23:xx local)
- DuetFlow **HALTED** at 19:25Z (22:25 local; `.orchestrator/HALT` present, loop exited, watchdog respects it).
  It stays halted until you relaunch. The checkout has the loop's own uncommitted `docs/UPCOMING.md` regen and
  untracked `docs/dependency_map.{md,png}`. Those are loop output; leave them.
- Removed the four merged s3k worktrees. Built s3m's work in five parallel worktrees on persistent storage,
  `/home/dan/projects/maestro-wt/<name>`, branch `s3m/<name>`, all based on 92a604d. **Status per branch is in §2.**
  All five agents were killed once by the 5-h usage cap (22:50 reset), then resumed. If a branch has no commit,
  inspect its worktree: work may be partial and uncommitted. Finish it yourself, or with one agent.
- **D20 findings from reading the code** (so you don't re-derive them):
  - The graph launch loop is `maestro/orchestrator.py` `_graph_main`, ~l.2600–2735. Today, parallel eligibility =
    deps + the `resource:` label mutex (`_resource_set`/`_resources_conflict` l.398–430). `scope:` is parsed
    (`taskgraph.TaskSpec.scope`) but unused for scheduling. No DuetFlow task declares `scope`.
  - The merge node is `workflows/handlers.py` `merge_handler` → `merge.integrate_candidate` →
    `validation.StagingPipeline.integrate`. `prepare_staging` (~l.1270) aborts on any conflict. Stage 3
    `verify_integration` ALREADY re-runs the task's checks on the merged snapshot. So a deterministic resolution in
    stage 2 is automatically re-verified, with no review.
  - A merge failure takes the `failed` path: no retry cone and no diagnose (`failures.retry_cone`: a merge "has no model to blame").
  - `_graph_graduate` collects review follow-ups across **every run of the workflow**
    (`followups.collect(store, artifacts, workflow_id)`), so a repair successor run keeps 04's follow-ups.
  - 04's failed run `run_hN6LPQuShbwkOIEn`: implementer / gate / proof_review succeeded; merge failed; diagnose,
    park and accept skipped. 04 has only auto checks (V1, V2, V-SUITE), and its files (app.py, config.py, scoring.py,
    3 tests) are not in DuetFlow's `risky_set` (spotify.py, playlist.py, auth.py). So `integration_guard` needs no
    proof review of a new commit.
- **04 recovery, prepared by hand (NOT finished):**
  - `git branch archive/04-scoring-run1 impl/04-scoring` (the original approved tip 5f63b5c).
  - Worktree `/home/dan/projects/duetflow-wt/04-remerge` on `impl/04-scoring` holds an **in-progress
    `git merge --no-ff main`**, conflicted in `duetflow/app.py`: docstring sentence, import line, and the
    inserted blocks (04's `cmd_explain` vs 03's `_owner_playlists`/`cmd_playlists`/`cmd_use_source`; there may be a
    4th hunk for subcommand registration below the first 120 diff lines).
  - Plan: run D20's resolver (`maestro/mergeresolve.py` on `s3m/d20`) over the 3 index stages of `app.py` and
    record the per-hunk result. Hunk 1 (docstring) needs the hand-combined sentence, main's items first:
    "Implemented so far: `auth`, `status`, `playlists`, `use-source`, `collect` and `explain` (one Collection; the
    hourly timer that runs it arrives in phase 08). `refresh` arrives with the phase that owns it and is declared
    here so `--help` tells the truth about what does not exist yet." Then run 04's gate cmds (V1, V2) + the full
    DuetFlow suite in that worktree and commit the merge. Remove the worktree; the branch keeps the merge.
  - Then `maestro ctl remerge 04-scoring` (branch `s3m/remerge`, once adopted): a plan-repair successor run in which
    implementer/gate/proof_review carry forward and only merge + accept run. Then relaunch; the loop resumes the
    `running` successor at startup. Expect merge (trivial now) → integration checks → accept → graduation
    (docs-only commit, `04-scoring-followups` queued if 04 had follow-ups) → map push. **No review or model usage
    should be spent; verify that in the journal/attempts.**
- Dan's side ask (22:52 local): "add a /help option to the telegram bot, I tried sending it and it didn't respond".
  Cause: `/help` exists (`hitl/commands.py` `poll_control_commands`), but only the loop polls Telegram, and the
  loop was halted, so no reply. The fix is branch `s3m/tghelp`: the watchdog answers `/help` + `/status` while
  halted, and other commands are queued or left unconsumed. **Tell Dan the cause and the fix.**

## 2. Branch status at hand-off
When s3m closed, the **d20, remerge and tghelp agents were still running** in their worktrees; they commit to
their branches when done. Before you start: if a branch has no commit, check whether its worktree's files are still
changing (`git status`, file mtimes) and give it time. If the worktree has gone quiet with no commit, the agent died
(check the usage cap): finish the remaining work yourself. (filled in by s3m; verified by s3n 2026-09-26 ~23:50 local: each branch is one commit on 92a604d, all five merged, combined suite 5063 passed / 0 failed; worktrees removed, branches kept)
- `s3m/d20` — **DONE 7b77d14** — verified by s3n: `git log 92a604d..s3m/d20` = 7b77d14 only; merged into `feat/graph-engineering-foundation` as **6c0169a** (suite 4996 passed / the same 5 worktree-only failures; 31 new tests).
  - **New modules:** `maestro/mergeresolve.py` and `maestro/writeset.py`.
  - **Journal and output:** `merge_handler` journals `merge_resolved_deterministic` / `merge_conflict_planner_fault`;
    outputs carry `resolved`, `fault` and `conflicts`.
  - **Scheduling:** the graph loop only; the legacy loop is untouched.
  - **Existing tests changed:** 3 tests now declare separate scopes, and the fake `FixtureQueries.index.list_files`
    was added.
  - **CONSEQUENCE:** DuetFlow declares no `scope:`, so after adoption it runs ONE task at a time; a waiting-on-Dan
    run also blocks everything. Fix it as a small gap: add `scope:` to DuetFlow's open tasks (04-followups, 02/03
    followups, 05–09) from each task's notes/PLAN, in one docs-only commit, and tell Dan.
  - **Installed-skill text proposed by the agent:**
    - after `deps:` in the task-block yaml, add
      `scope: [src/x.py, tests/]   # every file/dir/glob the task will write, tests included`;
    - after the schema block, add "Fill `scope` for every task: tasks whose scopes overlap run one after another,
      never in parallel, because parallel tasks must merge trivially. A task without `scope` is serialised against
      everything; `scope: []` says it writes nothing.";
    - in Step 3 item 4, change "`mode`, and `deps`" to "`mode`, `deps` and `scope`".
  - Original brief: deterministic merge resolution (`maestro/mergeresolve.py`, `prepare_staging`) + planner-fault
  failure (`merge_conflict_planner_fault`) + write-set scheduling (`launch_skipped_write_overlap`; no `scope` key =
  serialize against everything; `scope: []` = writes nothing) + planner/format docs to fill `scope`.
- `s3m/d21` — **DONE 077f2f8** — verified by s3n: `git log 92a604d..s3m/d21` = 077f2f8 only; merged into `feat/graph-engineering-foundation` as **80d88fe** (suite 4986 passed / the same 5 worktree-only failures). Changes:
  - `taskgraph.transitive_reduction` + `reduce_document_deps`. They use registry deps as extra reachability, but a
    graduated task never vouches for an open one, and cycles are left untouched.
  - `docs/roadmap.reduce_roadmap_deps()` runs once at the top of `_graph_main`. It edits deps lines minimally,
    journals `roadmap_deps_reduced`, updates the CAS, and makes a docs-only commit. It skips when the ROADMAP is dirty
    or a block has drifted from the CAS.
  - Graduation now records `deps` in `completed_tasks.json` (old entries have none, so they give fewer removals,
    never a wrong one).
  - `RoadmapTransaction.render`, proposals and `docs/depmap.render` reduce too.
  - The in-repo skill copies (`maestro/skills/claude/SKILL.md`, `maestro/skills/codex/maestro-setup.md`) have a new
    step 5.

  On DuetFlow it will remove `02-…-followups`→02, 07→03 and 08→06. Not done: the legacy loop and doctor don't
  reduce. The installed skill has diverged from the in-repo copy; the line to apply there is: "List only direct
  deps. A dep another dep already implies (A needs B and C, and B needs C) is transitively redundant: maestro
  removes it automatically (the transitive reduction, at loop start and in the dependency map), so do not ask the
  operator about it."
- `s3m/contam` — **DONE 7848b3e** — verified by s3n: `git log 92a604d..s3m/contam` = 7848b3e only; merged into `feat/graph-engineering-foundation` as **d5bfabb**: `quota.contamination` per attempt + `attempts_contaminated` /
  `attempts_contamination_unmeasurable` totals + `report.quota.contamination`; roots `CLAUDE_PROJECTS_ROOT`/
  `CODEX_SESSIONS_ROOT`, existing tests hermetic; 4 new tests. Suite 4964 passed / 5 failed. All 5 failures are in
  `test_next_graph_prompt.py` and are worktree-only (no `docs/graph-engineering/specs` in a worktree); the same
  failures occur on the base. Real read-only run (`$CLAUDE_JOB_DIR/tmp/duetflow-contam.json`, 2.6 s): Claude attempts
  5 contaminated / 5 clean, Codex 0 / 6. The main offender is the maestro-dev session f0420374 (02 proof_review_04 +
  diagnose_01, 03 implementer_01 + proof_review_01, 04 implementer_01). 04 implementer_01's interval is 5.7 h wide.
  **New small finding:** some test in the full suite rewrites the worktree's `project.yaml` (`self_fix_allow:
  [scripts/]` → `redo_allow: [colab/, docs/, notebooks/]`). Suspects: `test_confinement.py`,
  `characterization/test_selfheal.py`. It is a test-isolation bug: check `git status` after every suite run and fix
  it (small gap).
- `s3m/remerge` — **DONE b294153** — verified by s3n: `git log 92a604d..s3m/remerge` = b294153 only; merged into `feat/graph-engineering-foundation` as **ef266c1** (suite 4978 passed / the same 5 worktree-only failures).
  - **What it accepts:** only the latest run, when it is `failed`, its merge node failed and everything upstream
    succeeded.
  - **How:** `plan_repair` + `apply_repair` with new parameters `keep_failed=True` (the old run stays `failed`) and
    `fresh_attempts=("merge",)` (merge's single-attempt limit restarts in the new run).
  - **Budget:** it doesn't spend the diagnoser's repair budget.
  - **Runner fix:** `_committed_attempt` lets merge see a carried proof_review in the successor run.
  - **Safety:** the CLI refuses while the loop holds the lock.
  - **Journal:** `graph_run_remerge`.
  - **Telegram:** `/remerge` is not wired.
  - **04 dry reasoning:** the checks pass; it writes rev 2 with implementer/gate/proof_review carried; graduation will
    queue `04-scoring-followups` from proof_review_02's **2 follow-ups**.
  - **Before running it:** finish the branch merge (main is at 145a5b5). The conflict fix must not touch spotify.py,
    playlist.py or auth.py, or merge will refuse and a new review is needed.
- `s3m/tghelp` — **DONE 5f231cd** — verified by s3n: `git log 92a604d..s3m/tghelp` = 5f231cd only; merged into `feat/graph-engineering-foundation` as **7223126** (suite 4972 passed / the same 5 worktree-only failures; 12 new tests).
  - **Corrects s3m's assumption:** the watchdog EXITS on HALT; it does not stay up. So nothing read Telegram.
  - **Now, with a bot configured:** the watchdog stays up through a HALT or soft halt.
    - It answers `/help` and `/status` with a "halted" line.
    - It replies "queued" to other commands and never moves Telegram's read position past them, so the restarted
      loop runs them (they are also ingested as `received` in the inbox).
    - It only reads Telegram when the project's loop lock and control-DB lock are both free.
    - Without a bot, or with `MAESTRO_TELEGRAM_LISTENER=0` (as the tests set it), it exits on HALT as before.
  - **While the loop runs:** the listener now answers `/help` as soon as it arrives. Before, it waited up to a full
    poll.
  - **MEANING CHANGE — ASK DAN:** the watchdog no longer exits on HALT, so **removing HALT alone relaunches the
    loop**. A stall-HALT still exits with 1.
  - **Merge:** `HELP_TEXT` will conflict with nothing in s3m/remerge (it did not wire `/remerge`), but check.
  - **Stale docs to fix:** the `cli.py` `cmd_watchdog` docstring and DuetFlow `launch.sh` comments ("exits on HALT").
  - **Not covered:** the `paused_until` and `blocked_on` states when the loop has exited there.
  - **Deploy:** after adopting, restart DuetFlow's watchdog (`launch.sh`) with HALT still present, then test `/help`
    live.
- The maestro-setup skill lives OUTSIDE the repo (`~/.claude/skills/maestro-setup` →
  `~/.local/state/agent-skills/worktrees/candidate-20260926-043123/...`). Agents were told not to edit it and to
  report proposed text instead (scope guidance, D21 reduction). Apply it through the `manage-skills` skill.

## 3. In scope, in order
1. Review each branch's diff (they were built blind to each other), then merge all five into
   `feat/graph-engineering-foundation`. Expected overlap: orchestrator.py (d20 launch loop vs d21 loop-start
   normalisation) and cli.py/commands.py (remerge vs tghelp). Resolve, run the full suite (expect 0 failed; the
   ABUALI test_purity failures should be gone), and commit. Update STATE.yaml D20/D21 threads to FIXED with commits.
2. Recover 04 per §1 (resolver result per hunk; gate + suite; adopt maestro; `ctl remerge`; relaunch). Adopt
   = s3j §3 item 1 procedure + a manifest amendment with a `note`. On the first relaunch also watch D21's
   normalisation remove 07→03, 08→06 and `02-collector-schema-followups`→02 in one docs-only commit.
3. Apply the skill text (§2 last bullet). Tell Dan about `/help` (cause + fix) and test it live once while halted.
4. Continue per s3m §3.4 (follow-ups of 02/03/04, 05+, 06 collect + first live V-DAN with numbered steps; archive
   before any rerun; final export with contamination; retention re-measure, qualification re-run, verdict, close P12).

## 4. Traps (in addition to s3m §4)
- The subagents died once on "monthly spend limit"; that message also fires for the regular 5-h cap. Check
  `/usage` before dispatching big agent fan-outs.
- Don't leave `/home/dan/projects/duetflow-wt/04-remerge` mid-merge across a loop relaunch: the loop's staging
  merge reads the `impl/04-scoring` ref, not that worktree, but a dirty worktree on the candidate branch fails
  `freeze_candidate` if the runner passes it as `candidate_worktree`.
- Hand off again at ~120K context. This session reached 142K because the work started at 99K; start lean.

## 5. Report back
As s3m §5.
