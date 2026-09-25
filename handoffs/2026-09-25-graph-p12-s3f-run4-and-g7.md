# Handoff: Graph P12, session 3f. Watch run 4 of 02, land G7, continue the pilot, close P12

Continue Phase P12 on `feat/graph-engineering-foundation` (maestro). This file is the whole prompt. It
**supersedes** `handoffs/2026-09-25-graph-p12-s3e-relaunch-after-d6-d8.md` (its §2 steps 5–6, §3 and §4 still apply
and are restated below where they changed). `docs/graph-engineering/STATE.yaml` and `handoffs/` are gitignored (edits
live on disk only).

## 0. Read first (and only these)
1. `handoffs/2026-09-18-graph-p12-s3-pilots.md` §1, §3 (steps 5, 7, 8, 9), §4, §5, §6.
2. `docs/graph-engineering/STATE.yaml`: search `Pilot finding D9`, `Pilot finding D10`, `D10 fix edges`,
   `parity batch G7`, `Legacy-loop behaviours absent`.
3. `docs/DESIGN.md` §16: "The watchdog sees a live graph run", "Risky files and the deny-list", "Parity with the
   legacy loop".
4. `artifacts/graph-engineering/p12-pilots/manifest.json` `.amendments` (5 entries).
5. Memory `risky-files-gated-by-stronger-reviewer.md` (Dan's decision, 2026-09-25).

## 1. What session 3e did (2026-09-25)
- Verified Dan's revert of 416e970 (DuetFlow main 49f9979 == 98033e5 tree). Manifest amendment `00b1e1a`.
- **Run 3 of 02** (`run_vSNPmKFwtFb65fq5`) parked at 07:02Z: the reviewer (codex gpt-5.6-sol) rejected twice with real
  defects (D6 fix worked live). Then implementer_03 (codex terra) refused, because 02 must edit `spotify.py` (risky_set)
  and DuetFlow's preamble forbade it. **D10.** Dan decided: risky edits are allowed; the gate is a proof review by a
  strictly stronger model told which risky files changed; no Dan approval at merge. DuetFlow preamble `97f6244`.
  Archived: `.orchestrator/archive/2026-09-25-run3` (DB with WAL/shm, attempts, artifacts, HALT), branch
  `archive/02-run3`, export `p12-pilots/duetflow-run3-archived.json` (9 attempts, rework 2).
- **Parity audit** (Dan: the graph runner must carry every legacy behaviour unless irrelevant AND replaced by something
  better; no gap may be lost). Fixed and merged, full suite at dd9a2ff: 4842 tests, 0 real failures (3 test_purity
  failures came only from a live agent worktree under `.claude/worktrees/`; they clear once it is removed):
  - D9 `a86542b` watchdog: a running graph run counts like `in_flight` (run 1's 2 stall kills preceded its D4 hang).
  - D10 `d0452f8` `da16113` `f98c778`: graph merge runs deny-list + secrets (added lines, globs) and refuses an
    unreviewed risky edit (`risky_unreviewed`); the reviewer brief lists `RISKY FILES TOUCHED`; the reviewer strength
    rung must be strictly above the strongest writer's, else `route_no_stronger_reviewer` → park.
  - G1 `75e546b` Telegram (start, failure notice, completion report + map, map push on ROADMAP change); G3 `cf82dfa`
    push + meta reconcile; G4 `48d62b0` `graph_runs` state key, live `/status`, `phase`; G5 `c454ab5` hitl_mode,
    questions, reminders, proposals; G6 `047e4b1` priority, legacy cooldowns, canary, self-update, /fix /redo landing;
    B8 `5fe3c88` resource labels; `dd9a2ff` DESIGN §16.
- Adopted dd9a2ff into DuetFlow (doctor `adopted_code` OK + the two known WARNs). Manifest amendment + note `6d15694`.
- **Run 4 of 02** (`run_8HKdj2Aq2ZHEgb2i`) opened 07:55:59Z on dd9a2ff. `roadmap_map_pushed` journaled;
  `state.json` `graph_runs`/`phase` live. Unconfirmed: whether Dan received the map, and on which bot. Auto mode
  refuses reading the bot tokens, so ask Dan and don't try again.
- **G7** (still open, see STATE): pause nodes never released (human_action / await_job / cooldown), `/unpark` never
  reopens, `/approve /reject /redo /fix /backend` have no graph meaning, no Retry/Shelve buttons, **the manual lane has
  no merge node (DuetFlow 08-timers-notify would never land)**, and the graph prep brief lacks the legacy prep rules.
  Plan: `handoffs/2026-09-25-graph-g7-legacy-parity-open-items.md`. A worktree agent of session 3e was implementing it
  (in `/home/dan/projects/maestro/.claude/worktrees/agent-*`, branch `worktree-agent-*`).

## 1b. Late in session 3e (after the section above was written)
- **Run 4 parked at 08:02Z because of D11** (STATE `Pilot finding D11`). Both subscriptions ran out at about 08:00Z.
  The limit texts were classified `worker_crashed/bad_output` instead of quota, so every retry and the diagnoser
  failed in about 30s each. Before that, the one real review (claude-opus-5 over a sonnet writer) rejected validly:
  `collect` is still `cmd_not_yet`. Telegram `graph_run_failed_notified` was journaled (G1 live). Run 4 is **not
  archived yet**. DuetFlow is **halted** (`ctl halt`, 11:0xZ): the idle self-update (G6) would otherwise adopt
  whatever maestro HEAD becomes, outside the manifest. Remove HALT only when you relaunch.
- **The G7 agent died on the usage limit.** Its partial work is saved as WIP commit `626ce4e` (manual lane merge:
  `compiler.py`, `templates/workflows/project.yaml`; unverified) in `.claude/worktrees/agent-a3f85f2a38e40c66e`,
  branch `worktree-agent-a3f85f2a38e40c66e`. Nothing else from G7 is done. The plan file is unchanged.

- **DuetFlow is halted.** `maestro ctl halt` ran at 2026-09-25T11:01:36Z, the watchdog exited, and HALT is
  present. Nothing runs until you relaunch. Move HALT into the run-4 archive together with the DB, as for run 3.
- `docs/ABUALI_INTEGRATION_BOUNDARIES.md` in the maestro checkout is untracked and not from this work (created
  2026-09-25 12:32 local). Leave it alone; ask Dan if it gets in the way.

## 2. In scope, in order
0. **Fix D11 first** (classifier recognises both limit texts as quota, parses the reset, and pauses the pool). Add
   tests with the exact strings, run the full suite, and commit. Then do G7 (step 2) before adopting, so there is
   one adoption and one amendment. Next, archive run 4 as runs 2 and 3 were archived (DB + WAL/shm, attempts,
   artifacts, the `impl/02-collector-schema` branch renamed to `archive/02-run4`, export
   `duetflow-run4-archived.json`), write the amendment, and relaunch run 5 of 02. Step 1 below then applies to
   run 5.
1. **Run 4:** check the state. Control DB `/home/dan/projects/duetflow/.orchestrator/control.sqlite3` (task_runs,
   attempts). Wait with a background `until` loop, never repeated sleeps. **Only match journal lines written after
   the loop starts**: `base=$(wc -l < journal.ndjson)` then `tail -n +$((base+1))`, because the old run-1
   `stall_restart` lines would match straight away. When it settles, check:
   - the proof_review verdict and the rework count;
   - that a reviewer of a risky diff sits on a higher strength rung than the writer;
   - that no `stall_restart` appears during long attempts (D9 live);
   - that Telegram notices were journaled.

   After an accept, also check that the checkout is clean and at main, that there are no `stale_checkouts`, that
   graduation touched only docs, and that `push_skipped_no_remote` was journaled once.
2. **G7:** `git worktree list`. If a `.claude/worktrees/agent-*` exists, read its `git log` and its copy of
   `handoffs/2026-09-25-graph-g7-legacy-parity-open-items.md` (the agent was told to keep it current). Cherry-pick
   its finished commits onto the branch, resolving overlaps by keeping both sides where they are independent. Finish
   what remains yourself, or with one fresh worktree agent. Then remove the worktree and delete its branch; don't use
   `--force` (the classifier refuses it). Run the full suite (junit counts). **Adopt between pilot tasks, never under a
   live run**: halt, adopt (`selfupdate.materialize_worktree` + `adopt(sha, project_repo=...)` with
   `MAESTRO_REPO=/home/dan/projects/duetflow`), then append a manifest `code_under_test` amendment and relaunch. G7
   must land before DuetFlow reaches 08-timers-notify (manual lane) or any task that needs /unpark.
3. Continue the pilot per s3 §3 and s3b §3 steps 7–8 (03's V-DAN: Dan picks his playlist on Telegram; 06 needs
   `duetflow collect`). To rerun a task, archive its run first, as for runs 2 and 3: the DB **with WAL/shm**, attempts,
   artifacts, and rename `impl/<task>` to `archive/<task>-runN`, since a new run reuses a kept `impl/<task>` branch.
   Overwrite the untracked `p12-pilots/duetflow.json` only with the final export.
4. Then s3 §3 steps 5, 7, 8 and 9: retention re-measure, qualification re-run, verdict, close P12. At the close, name as
   next-task candidates:
   - the model-catalog-config thread;
   - the failure-handling phase (D4, plus the D10 edge where a strong writer means no stronger reviewer, so the run parks);
   - the model-endpoint reviewer verdict gap;
   - any G7 leftovers.

## 3. Coordination and traps
- No other session writes maestro or DuetFlow. In the `agents` tmux session, kill only pilot windows. Kill by PID
  (`pkill -f` matches its own shell).
- Auto mode refuses: writing to the journal (keepalives), reading bot tokens, `git worktree remove --force`, and
  `git restore --worktree` + `revert` on DuetFlow main. Hand such steps to Dan as one `! <command>` line.
- `ctl` is `/home/dan/projects/duetflow/.venv/bin/maestro ctl …` with `MAESTRO_REPO` set. `ctl halt` works while
  the loop runs; `pause`/`resume` refuse while it runs.
- pytest: use `.venv/bin/python -m pytest --junitxml=<file in repo root>` (addopts has -q; don't pass -q). The
  scratchpad may vanish. Delete stray `.junit-*.xml` files after use.
- STATE.yaml threads must parse: quote any `owner:` value that contains `: `
  (`tests/test_next_graph_prompt.py::test_live_state_threads_are_all_structured`).
- Session 3e's context passed 178K; a fresh session should hand off again at 160K.

## 4. Report back (with numbers)
As in s3 §6, plus one line each with commit for D5 (3d4bd09), D6 (3898496), D7 (96570a0), D8 (9c61367), D9 (a86542b)
and D10 (d0452f8/da16113), and the G1–G7 parity status. For each 02 run give the review verdict and rework count:
- run 1: hung on D4, after D9 stall kills;
- run 2: accepted despite 4 P1s (D6), reverted;
- run 3: 2 rejections, then parked on D10;
- run 4: 1 valid rejection, then parked in ~3 min on D11 (quota misclassified);
- run 5: fill in.
