# Handoff: Graph P12, session 3d. Make the proof review able to reject (D6), finish graduation (D7), re-run 02, continue the pilot

Continue Phase P12 on `feat/graph-engineering-foundation` (maestro). This file is the whole prompt. It
**supersedes** `handoffs/2026-09-24-graph-p12-s3b-restart-pilot.md`. That file's §3 steps 6–9 and §4 still apply.
So do `handoffs/2026-09-18-graph-p12-s3-pilots.md` §1 (Dan's decisions), §3 steps 3–9, §4, §5 and §6. Read them
there; they are not repeated here. `docs/graph-engineering/STATE.yaml` and `handoffs/` are gitignored (edits live
on disk only).

## 0. Read first (and only these)
1. `handoffs/2026-09-18-graph-p12-s3-pilots.md` §1, §3 (steps 3–9), §4, §5, §6.
2. `handoffs/2026-09-24-graph-p12-s3b-restart-pilot.md` §3 steps 6–9 (relaunch, what to watch, the V-DANs, and
   the queued model-catalog thread).
3. `docs/graph-engineering/STATE.yaml`: search `Pilot finding D6` and `Pilot finding D7` (and D5 and the dead-letter
   watchdog thread just above/below).
4. `artifacts/graph-engineering/p12-pilots/manifest.json`: `.amendments` (2 entries so far).

## 1. What session 3c did (2026-09-24)
- Full suite at `3d4bd09`: junit tests=4786 failures=0 errors=0 skipped=1, rc 0.
- **D5 fixed, `3d4bd09`**: while the loop held the controller lease, every `maestro ctl` state verb crashed with
  SingleWriterViolation, including `halt`, the documented clean stop. Now `halt` writes HALT plus a text-only journal line
  (`append_journal(..., shadow=False)`), and pause/resume/hitl/unpark refuse with a pointer to Telegram. Tests are in
  `tests/test_cli.py`. **Verified live** on DuetFlow: `ctl halt` stopped the running loop, rc 0.
- Run 1 is archived: `duetflow/.orchestrator/archive/2026-09-24-run1/` (control DB and WAL/SHM, attempts, artifacts, HALT,
  and the 02 worktree via `git worktree move`, since the classifier refused `git worktree remove --force`). The branch was
  renamed `archive/02-run1`. Its export was committed as `p12-pilots/duetflow-run1-archived.json`.
- Adopted `3d4bd09` (`duetflow/.orchestrator/current`, state `maestro_version`; doctor `adopted_code` OK at that sha,
  plus the two known WARNs). The manifest amendment was committed in `33a5eba` (code under test dacb69d→3d4bd09, 02 re-run,
  criteria unchanged, plus a note that graph routing ignores `project.yaml roles:`; DESIGN §16 doesn't bind them).
- **Run 2 of 02 (`run_H7HzeJWRxx6kagb4`)** on a fresh control DB: implementer claude-sonnet-5, agent_version
  `2.1.281 (Claude Code)` (**D2 verified live**), usage_start/settle populated with the account windows
  (**D1 verified**: 5h 30%, 7d 4%). One caveat: `usage_*_json.model` / `context_*` come from whichever Claude session
  last wrote the statusline snapshot, not the attempt, so use only `windows`. Implementer took 61 s. proof_review ran on Codex
  gpt-5.6-sol for 304 s and **wrote DONE into its workspace (D3 verified live)**; Codex reported `tokens used 84,406` at the
  tail of impl.log. Gate, merge and accept all succeeded, and DuetFlow `main` = `416e970` (merge of `d28ce0e`).
- **But this is D6:** the reviewer's DONE says the work has **4 P1 defects** (collect CLI still a stub, sub-second Plays
  collapse (collector.py:32), no 401/429/5xx recovery (spotify.py:148), silent overlong collection gaps). The
  graph accepted it anyway. The reviewer's brief (`workflows/worker.py` `task_header`) offers only DONE = finished /
  FAILED = cannot complete, and nothing checks a verdict. So **02 on DuetFlow main is defective work accepted by a maestro bug.**
- **Then D7:** after the accept, 02 stayed `status: open` in DuetFlow's ROADMAP (the graph loop never calls
  `mark_roadmap_complete`), so 03 could never open a run (`idle_gated: all gated: 02-collector-schema`).
- The loop was halted with `ctl halt` (watchdog session and orchestrator window gone). `duetflow/.orchestrator/HALT` is
  present. The control DB holds run 2 as `succeeded`.

## 2. Uncommitted work in maestro (D7, in progress)
- `maestro/orchestrator.py` `_graph_main`: every poll, any task in `runnable` with a `succeeded` task_run and not in
  `complete_ids` gets `mark_roadmap_complete(task_id)`. That covers a crash between settle and graduation too.
- `tests/graph_engineering/test_end_to_end.py`: `_PerTaskAgent`, `_drive_until_runs_settle(n)`,
  `_drive_every_worker_of_any_run`, and the lane `test_an_accepted_task_graduates_so_its_dependent_runs_next` (TASK-002 deps
  TASK-001). **Still red, for a fixture reason:** graduation now fires (`roadmap_task_removed`), but
  `maestro/docs/complete.py::remove_task_section` bounds a task by its `### <id>` heading, and the lane ROADMAP
  (`tests/graph_engineering/conftest.py::_roadmap_document`) has no headings, so the strip removes everything to EOF,
  TASK-002 included. DuetFlow's real ROADMAP does use `### ` headings. Fix: give this lane a ROADMAP with
  `### <id>` headings. Prefer a lane-local writer, or add headings in `_roadmap_document` only if the lane traces stay
  reproducible (`test_every_lane_is_reproducible`). Then commit D7.
- `artifacts/graph-engineering/p12-pilots/duetflow.json` is untracked. It is the stale s3b export; overwrite it with the
  final run's export later.

## 3. In scope, in order
1. **Finish D7** (§2), then commit.
2. **Fix D6 with TDD and a lane.** The machinery exists: `validation.review_disposition` and `review_is_conclusive`
   (INV-04: empty, unknown or no-findings rejections fail closed). Suggested shape:
   - Reviewer brief (`worker.task_header` when `agent_definition_ref == "reviewer"`): report a verdict,
     e.g. `review.json` in the WORKSPACE `{"verdict": "approved"|"changes_requested"|"rejected", "findings": [...]}`,
     and then DONE. Keep it CLI-agnostic, since Codex and Claude both write files.
   - The agent handler/worker, for a reviewer, adopts the verdict. `approved` → succeeded with a disposition in
     outputs. Anything else, or none → a **quality failure (bad_output / verification_failed)** carrying the findings.
   - **Trap:** `failures.retry_cone` gives an agent node a cone of *itself*, so a failed review would just re-review the
     same tree. A rejecting review must behave like a verification verdict: its cone reaches back to the implementer
     (as the gate's does, and as `dan_confirm`'s rejection does via `failures.retry_cone`). The implementer's rework
     brief carries the findings: extend `runner.rework_note` (today it covers only Dan's rejection) to include the
     latest reviewer findings as `NOTE FROM ORCHESTRATOR`. The usual budget of 3 quality attempts applies, then park.
   - Every fixture agent that writes a bare DONE for the reviewer must now approve (update `_CommittingAgent` and friends).
     Add lanes: a rejecting review reworks and then lands; an inconclusive review fails closed.
   - DESIGN §16 gets a short paragraph on the verdict; EXECUTION.md too if it describes the reviewer.
3. Full suite (`--junitxml`, rc) must be green. Then adopt the new HEAD into DuetFlow:
   `MAESTRO_REPO=/home/dan/projects/duetflow .venv/bin/python -c "...selfupdate.materialize_worktree(Path('/home/dan/projects/maestro'), sha); selfupdate.adopt(sha, project_repo=Path('/home/dan/projects/duetflow'))"`.
   **Set MAESTRO_REPO**, or adopt's state write targets the maestro repo and raises. Check `.orchestrator/current` and doctor.
4. **Undo run 2 on DuetFlow and re-run 02** (same rule as run 1; not a decision for Dan, since a maestro defect
   accepted rejected work). Stop first: `git -C duetflow revert -m 1 416e970`, a new commit and nothing destroyed.
   Before doing so, confirm `git log main` still has 416e970 at the tip and nothing on top. Then re-export run 2 with
   `scripts/graph_pilot_export.py --since 2026-09-24T12:57:00Z --out p12-pilots/duetflow-run2-archived.json`, and archive
   `.orchestrator/{control.sqlite3*,attempts,artifacts,worktrees(if any)}` plus `HALT` into
   `.orchestrator/archive/2026-09-24-run2/` by `mv`. The run 2 task worktree was released at accept, so only the branch
   may remain: rename `impl/02-collector-schema` to `archive/02-run2` if it exists. Leave journal.ndjson and state.json
   (check `in_flight: []`). If 02 was graduated by then, it wasn't (D7 was never deployed), but check that the ROADMAP
   still has 02 `status: open`.
5. **Manifest amendment** (append, commit): code under test → new sha for D6/D7. 02 is re-run again because run 2 was
   accepted despite a rejecting review (D6). Criteria and weights are unchanged.
6. Relaunch (`setsid nohup bash launch.sh`; `ctl resume` only if paused; check that HALT is gone), then continue per
   s3b §3 steps 6–8. Wait with a background `until` loop on the control DB (run status, an `uncertain` attempt,
   `dan_verify` in the journal, or the watchdog session disappearing) and **never** with repeated sleeps. On each run
   settling, check the proof_review verdict, not just `succeeded`.
7. Then s3 §3 steps 5, 7, 8 and 9 (retention re-measure, qualification re-run, verdict, close P12). At the close, name the
   model-catalog-config thread and the failure-handling phase as next-task candidates.

## 4. Coordination and traps
- No other session writes maestro or DuetFlow. The `agents` tmux session hosts other work: kill only pilot windows.
- `pkill -f <pattern>` matches its own shell when the pattern is in the command: it killed the commit in s3c. Kill by PID.
- The auto-mode classifier refuses `git worktree remove --force`. Use `git worktree move` into the archive.
- `ctl halt` now works while the loop runs. `ctl pause/resume` refuse while it runs (use Telegram or halt).
- The watchdog's `clear_pause`/`clear_blocked_on`/`reconcile_leases` commands are dead letters (STATE thread, control-plane).
  An expired `paused_until` is inert, so this doesn't block the pilot, but watch `blocked_on` if a Dan request appears.
- Budget: s3c crossed 160K while diagnosing D6/D7. Keep reads narrow (sed -n ranges, jq), and use the export script.

## 5. Report back (with numbers)
As in s3 §6, plus: D5/D6/D7 one line each with commit; for each 02 run, the review verdict and the rework count.
