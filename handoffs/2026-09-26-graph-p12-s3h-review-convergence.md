# Handoff: Graph P12, session 3h. Fix D14–D16 (review convergence), narrow D13, rerun 02, continue the pilot, close P12

Continue Phase P12 on `feat/graph-engineering-foundation` (maestro). This file is the whole prompt. It
**supersedes** `handoffs/2026-09-26-graph-p12-s3g-run5.md`. `docs/graph-engineering/STATE.yaml` and `handoffs/`
are gitignored (edits live on disk only).

## 0. Read first (and only these)
1. `handoffs/2026-09-18-graph-p12-s3-pilots.md` §1, §3 (steps 5, 7, 8, 9), §4, §5, §6.
2. `docs/graph-engineering/STATE.yaml`: search `Pilot finding D13`, `D13 widening`, `Pilot finding D14`,
   `Pilot finding D15`, `Pilot finding D16`, `Pilot finding D11`.
3. Run 7's diagnosis (excellent, read all of it):
   `/home/dan/projects/duetflow/.orchestrator/attempts/att_run_zc15NaSQOmPW1RAf_diagnose_01/DONE`.
4. `artifacts/graph-engineering/p12-pilots/manifest.json` `.amendments` (last 2 are s3g's).
5. Memories `risky-files-gated-by-stronger-reviewer.md`, `gaps-are-not-decisions.md`.

## 1. What session 3g did (2026-09-26)
- **D12 confirmed by Dan**: he received run 5's start notice from `@MaestroGenericTestingBot`.
- **D11 confirmed live** (run 7): codex "usage limit … try again at 8:26 AM" → `pool_paused` codex-subscription until
  05:26Z, no retry spent, proof review moved to claude-opus-5.
- **D13 FIXED `dd9a777` + `9d91b4c`**: after two valid rejections the writer steps up to codex gpt-5.6-terra, whose
  `workspace-write` sandbox could not `git commit` (`.git/worktrees/<name>/index.lock`: Read-only file system); it
  wrote no DONE, the node settled `uncertain`, the run hung (D4). Runs 5 and 6 both hung this way. Codex keeps the
  workdir's own resolved gitdir read-only even under a granted `.git`, so writers now get `--absolute-git-dir` AND
  `--git-common-dir` as add_dirs (`worker._git_dirs`). Lane `test_a_sandboxed_implementer_can_commit_in_its_worktree`.
  Verified live in run 7 (codex writer committed). **Open: the grant is too wide** (see §2 step 2).
  Lesson: s3g's first scratch repro (fresh repo under the job dir) did not show codex's gitdir protection;
  probe in a real DuetFlow worktree (`codex exec ... "touch <path> && echo OK"` works; the agent's hooks reject `rm -f`).
- Runs 5 and 6 archived (`.orchestrator/archive/2026-09-26-run5|run6`, branches `archive/02-run5` = c94029c and
  `archive/02-run6` = 839268a hold the codex writer's uncommitted rework as a commit), exports
  `duetflow-run5-archived.json` / `duetflow-run6-archived.json` (7 attempts, rework 2 each). Commits 82593ef, 857d07d.
- **Run 7** (`run_zc15NaSQOmPW1RAf`, 01:37Z → parked 02:09Z): sonnet writer ×2 + codex terra writer, gate green
  every round (V-SUITE 77), reviews: gpt-5.6-sol changes_requested ×2, gpt-5.6-sol quota_exhausted, claude-opus-5
  changes_requested (unbounded Retry-After sleep; SQL outside db.py; `runs.started_at` bound to finish time; no
  `runs` row on failure), diagnose (claude-opus-5 high, 426 s) → park. Failed notice journaled without
  `telegram=unconfigured`. Its diagnosis found D14/D15/D16. **Not yet archived or exported.**
- DuetFlow loop is **HALTED** (06:56Z). DuetFlow runs maestro `9d91b4c` (adopted, doctor OK + 2 known WARNs).
  Journal line count 124.
- Full suite at 9d91b4c: junit tests=4884 failures=2 (both `test_purity` on the untracked
  `docs/ABUALI_INTEGRATION_BOUNDARIES.md`, not ours) → 0 real. DuetFlow also has an untracked `docs/UPCOMING.md`,
  not ours; leave both.

## 2. Dan's decisions (2026-09-26, do not re-ask)
- **Retry-After (DuetFlow product):** cap and fail fast. Honour `Retry-After` up to a bound below the hourly
  Collection interval; beyond it, fail that Collection with a clear error so the next hourly run retries.
- **Review bar (maestro, D16):** the reviewer marks each finding `blocking` or `follow_up`; only blocking findings
  fail the node; follow-ups are recorded, not lost. The reviewer also sees prior rounds' findings (D15).

## 3. In scope, in order
1. **Archive run 7** exactly as runs 5/6: export first (`scripts/graph_pilot_export.py --project
   /home/dan/projects/duetflow --out artifacts/graph-engineering/p12-pilots/duetflow-run7-archived.json --since
   2026-09-26T01:37:00Z`), then in the task worktree commit any uncommitted work, `git worktree remove` (no
   --force), `git branch -m impl/02-collector-schema archive/02-run7`, move control.sqlite3(+wal/shm), attempts,
   artifacts, HALT into `.orchestrator/archive/2026-09-26-run7`. Keep the diagnose DONE (it moves with attempts).
2. **Maestro fixes, TDD, each with a lane in `tests/graph_engineering/test_end_to_end.py`** (DuetFlow halted):
   - **D14**: a rework after a reviewer rejection continues from the previous attempt's commit; only a retry
     (bad output / crash) restores `start_sha`. (`runner._restore_start_tree`, `confinement.CommitBoundary.restore`.)
   - **D15 + D16**: reviewer brief gets the prior rounds' findings and what was done about them; `review.json`
     findings carry `severity: blocking|follow_up`; `agent_handler` (handlers.py ~272) fails only on blocking; follow-ups
     go into the run's record (artifact + the export) so they are not lost. Update the reviewer brief text in
     `worker.task_header` and `maestro/templates/roles/reviewer.yaml`. Missing severity → treat as blocking.
   - **D13 narrowing**: grant the own gitdir + `objects/` + the `refs/heads/impl/` and `logs/refs/heads/impl/`
     dirs instead of the whole common `.git`; confirm with a real codex commit in a real DuetFlow worktree; if codex
     cannot commit with the narrow set, keep the wide grant and record why in STATE.
   - Full suite, commit, adopt into DuetFlow (`selfupdate.materialize_worktree` + `adopt(sha, project_repo=...)`,
     `MAESTRO_REPO=/home/dan/projects/duetflow`), manifest `code_under_test` amendment.
3. **DuetFlow PLAN §5**: add one sentence stating Dan's Retry-After decision (cap below the hourly interval, fail
   fast beyond it). Commit on DuetFlow main (docs only). Record a manifest `note` amendment (spec clarification, not a
   criterion or weight change).
4. **Run 8 of 02**: remove HALT, `setsid nohup bash launch.sh` in DuetFlow (the `agents` tmux session must exist).
   Watch with a background `until` loop that exits on run settle OR any `uncertain` attempt (s3g's loop). Check: the
   review verdicts and rework count, follow-ups recorded, rework builds on the previous commit, D9 no stall kills,
   Telegram lines without `telegram=unconfigured`. After an accept: checkout clean at main, no `stale_checkouts`,
   graduation touched only docs, `push_skipped_no_remote` once.
5. Continue the pilot per s3 §3 and s3b §3 steps 7–8 (03's V-DAN: Dan picks his playlist on Telegram; 06 needs
   `duetflow collect`; 08-timers-notify is the manual lane with merge). Archive before any rerun. Overwrite the
   untracked `p12-pilots/duetflow.json` only with the final export.
6. Then s3 §3 steps 5, 7, 8, 9: retention re-measure, qualification re-run, verdict, close P12. ITV only if Dan
   reports grill-me done. Next-task candidates: model-catalog-config; the failure-handling phase (D4 uncertain
   nodes hang forever — runs 5/6 would have hung indefinitely; D10 edge); model-endpoint reviewer verdict gap; G7
   item 7 (idle self-update during pilots); `/ask` under the graph.

## 4. Coordination and traps
- No other session writes maestro or DuetFlow. In the `agents` tmux session, kill only pilot windows; by PID.
- **Halt DuetFlow before committing maestro code** (idle self-update, G7 item 7). Adopt only between pilot tasks.
- Auto mode refuses: writing to the journal, reading bot token values, `git worktree remove --force`,
  `git restore --worktree` + `revert` on DuetFlow main. Hand such steps to Dan as one `! <command>` line.
- `ctl` is `/home/dan/projects/duetflow/.venv/bin/maestro ctl …` with `MAESTRO_REPO` set. Dan can watch a run with
  `MAESTRO_REPO=$PWD .venv/bin/maestro workflow show 02-collector-schema` (`--html` for a page) and
  `tail -f .orchestrator/attempts/<latest>/impl.log`.
- pytest: `.venv/bin/python -m pytest --junitxml=<file in repo root>` (no -q). Delete stray `.junit-*.xml`.
- STATE.yaml threads must parse; quote any `owner:` containing `: `.
- A 02 round takes ~6 min (writer 1–5 min, review ~4 min); three rejections → diagnose → park in ~30 min.
- Hand off again at ~120K context.

## 5. Report back (with numbers)
As in s3 §6, plus one line each with commit for D5 (3d4bd09), D6 (3898496), D7 (96570a0), D8 (9c61367), D9 (a86542b),
D10 (d0452f8/da16113), D11 (2b80705, confirmed live run 7), D12 (ad742b7, confirmed by Dan), D13 (dd9a777, 9d91b4c,
+ narrowing), D14, D15, D16, and G1–G7 status (G7 be3b0c6..508a9a7; item 7 open). Per 02 run, review verdict and
rework count:
- run 1: hung on D4, after D9 stall kills;
- run 2: accepted despite 4 P1s (D6), reverted;
- run 3: 2 rejections, then parked on D10;
- run 4: 1 valid rejection, then parked in ~3 min on D11 (9 attempts, rework 2);
- run 5: 2 valid rejections, codex rework could not commit → uncertain, hung (D13) (7 attempts, rework 2);
- run 6: same as run 5 (first D13 fix incomplete) (7 attempts, rework 2);
- run 7: 3 rejections (sol, sol, opus after codex quota pause), diagnose → parked; found D14–D16;
- run 8: fill in.
