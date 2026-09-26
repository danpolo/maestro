# Handoff: Graph P12, session 3i. Fix D17 (a live diagnoser can never repair), rerun 02, continue the pilot, close P12

Continue Phase P12 on `feat/graph-engineering-foundation` (maestro). This file is the whole prompt. It
**supersedes** `handoffs/2026-09-26-graph-p12-s3h-review-convergence.md`. `docs/graph-engineering/STATE.yaml` and
`handoffs/` are gitignored (edits live on disk only).

## 0. Read first (and only these)
1. `handoffs/2026-09-18-graph-p12-s3-pilots.md` §1, §3 (steps 5, 7, 8, 9), §4, §5, §6.
2. `docs/graph-engineering/STATE.yaml`: search `Pilot finding D17`, `Pilot finding D14`, `Pilot finding D16`,
   `D13 widening NARROWED`.
3. `maestro/workflows/runner.py`: `_plan_repair_proposal`, `_verdict_refusal`, `_plan_repair` (~1470–1600);
   `maestro/workflows/compiler.py` `PlanRepairProposal`; `maestro/workflows/handlers.py` `_adopt_review` (the
   pattern to copy); `maestro/templates/roles/diagnoser.yaml`; `worker.task_header`.
4. Run 8's diagnosis: `/home/dan/projects/duetflow/.orchestrator/archive/2026-09-26-run8/attempts/att_run_LisIoqe6p1xbHQj2_diagnose_01/DONE`.
5. Memories `llm-failure-handling-policy.md`, `gaps-are-not-decisions.md`, `risky-files-gated-by-stronger-reviewer.md`.

## 1. What session 3h did (2026-09-26)
- **Run 7 archived** (`.orchestrator/archive/2026-09-26-run7`, branch `archive/02-run7` = 6a9fadb, export
  `duetflow-run7-archived.json`: 12 attempts, rework 2). Commit b782552.
- **maestro 0e80da0** (TDD, lanes in `tests/graph_engineering/test_end_to_end.py`):
  - D14: `_restore_start_tree` restores only when the writer's own previous attempt did not succeed; a rework
    after any downstream verdict continues from its commit. Lane `test_a_rework_after_a_rejecting_review_continues_from_the_previous_commit`.
  - D16 (Dan's decision): `handlers._split_findings` — findings `{"finding", "severity": blocking|follow_up}`;
    only blocking fail; unmarked = blocking under a rejection, follow-up under an approval; unknown = blocking;
    approved + blocking → fails. Follow-ups in the disposition and in the export (`review_follow_ups`).
  - D15: `runner.review_history` → manifest `prior_reviews` → brief section "PRIOR REVIEW ROUNDS".
    Lane `test_a_review_converges_on_blocking_findings_and_keeps_its_follow_ups` (D15+D16).
  - D13 narrowed: `worker._git_dirs` grants own gitdir, `objects/`, `refs/heads/impl`, `logs/refs/heads/impl`
    only. Verified live (codex 0.157.0 committed in a real DuetFlow worktree). The negative control (codex
    touching another ref) was refused by auto mode, not run.
  - Full suite: junit tests=4888 failures=2 (both `test_purity` on the untracked
    `docs/ABUALI_INTEGRATION_BOUNDARIES.md`, not ours) → 0 real.
- Adopted 0e80da0 into DuetFlow (doctor OK + 2 known WARNs). DuetFlow PLAN §5 Retry-After sentence: DuetFlow
  main 048c526. Manifest amendments (code_under_test 9d91b4c→0e80da0, and a `note`): b782552.
- **Run 8** (`run_LisIoqe6p1xbHQj2`, 07:22Z → parked 07:46Z, 24 min): sonnet ×2 + codex terra writer, gate green
  every round, 3 × gpt-5.6-sol `changes_requested`; findings **4 → 3 → 1**, converging (not disjoint as run 7).
  Confirmed live: D14 (fe23083 → e9a9b34 → 38ebd0b, no reset), D13 narrowing (terra committed), D15 (R2/R3
  briefs had PRIOR REVIEW ROUNDS; R3 said "only partially fixes round 1's blocking finding"), severity used.
  No follow-ups recorded: the reviewer marked every finding blocking. R3's last finding: `_play_iso` forces
  `timespec="milliseconds"` so sub-ms Plays collide (defensible; the diagnoser agreed and blamed example-specific
  remediation). Telegram `graph_run_failed_notified` journaled without `telegram=unconfigured`.
  Then diagnose (gpt-5.6-sol) wrote a good prose DONE, but its attempt row settled `failed` → park: **D17**.
- Run 8 archived (`2026-09-26-run8`, branch `archive/02-run8` = 38ebd0b, export `duetflow-run8-archived.json`:
  11 attempts, rework 2). Commit 22daf01. DuetFlow is **HALTED** (ctl halt, 07:47Z), loop not running,
  DuetFlow runs maestro 0e80da0.

## 2. Dan's decisions (do not re-ask)
- Retry-After: cap below the hourly interval, fail fast beyond (in PLAN §5 now).
- Review bar: blocking vs follow_up, reviewer sees prior rounds (done).
- Failure policy (memory): 2 retries → stronger diagnoser → replan or enhance prompt → one final run → park.

## 3. In scope, in order
1. **D17, TDD with a lane** (DuetFlow halted): the diagnoser brief (`worker.task_header` for
   `ROLE_DIAGNOSER`, and `templates/roles/diagnoser.yaml`) names a `plan_repair.json` in the WORKSPACE with the
   `PlanRepairProposal` schema and the verdicts (read `compiler.PlanRepairProposal.from_dict` for the exact
   fields; for a converging review loop the sensible verdict is the one that grants one final writer run with
   an enhanced prompt — check which verdict does that; if none does, record it and ask Dan only if it is a
   genuine policy choice). `agent_handler` adopts it into `outputs.plan_repair` (like `_adopt_review`); an
   unparseable file stays the refusal it is. Lane: a diagnoser that writes a valid proposal → one final run →
   accepted; one that writes none → park (existing behaviour). Full suite, commit, adopt into DuetFlow
   (`selfupdate.materialize_worktree` + `adopt(sha, project_repo=...)`, `MAESTRO_REPO=/home/dan/projects/duetflow`),
   manifest `code_under_test` amendment (use `json.dumps(..., indent=2, ensure_ascii=False)` — plain dump
   re-escapes existing criteria).
2. **Make each review round aim to be the last (Dan, 2026-09-26; both sides, TDD with a lane).** Run 8's
   round-2 findings were all side effects of round-1 fixes, and round 3 was round 1's timestamp fix done only for
   the reviewer's example. Dan decided both sides change:
   - **Reviewer** (`worker.task_header` reviewer text + `templates/roles/reviewer.yaml`): each blocking finding
     states (1) the general rule/property violated, the example only as a demonstration; (2) its scope — every
     place the rule applies; (3) how to verify the fix — a test of the property, checkable next round. Report
     every blocking finding in this one pass; aim for this review to be the last; a defect visible now but held
     back is a wasted round. Optional structured fields on the finding object (e.g. `rule`, `scope`, `verify`)
     are fine if the render stays one string per finding in the implementer's note.
   - **Implementer** (`runner.rework_note`): fix the class of defect each finding names, not only its example;
     add a general test per finding; check each fix does not break adjacent behaviour (run 8: the `collect`
     wiring polled role A; the 401 callback dropped the rotated refresh token).
   Caveat to keep in mind: a reviewer cannot foresee defects a future fix introduces — the implementer side and
   the retry budget/diagnoser remain the backstop. Commit together with D17 or right after it, before run 9.
3. **Run 9 of 02**: remove HALT, `setsid nohup bash launch.sh` in DuetFlow (the `agents` tmux session must
   exist). Watch with a background until-loop on the control DB (`task_runs.status` not `running`, or any
   `uncertain` attempt, or HALT). Check: verdicts, follow-ups, rework count, diagnose → final run. After an accept:
   checkout clean at main, no `stale_checkouts`, graduation touched only docs, `push_skipped_no_remote` once.
4. Continue the pilot per s3 §3 and s3b §3 steps 7–8 (03's V-DAN: Dan picks his playlist on Telegram; 06 needs
   `duetflow collect`; 08-timers-notify is the manual lane with merge). Archive before any rerun (export first,
   `git branch -m impl/<task> archive/<task>-runN`, move control.sqlite3*, attempts, artifacts, HALT copy into
   `.orchestrator/archive/<date>-runN`, keep a HALT in place). Overwrite the untracked `p12-pilots/duetflow.json`
   only with the final export.
5. Then s3 §3 steps 5, 7, 8, 9: retention re-measure, qualification re-run, verdict, close P12. ITV only if Dan
   reports grill-me done. Next-task candidates: model-catalog-config; the failure-handling phase (D4 uncertain
   nodes hang forever; D10 edge); model-endpoint reviewer verdict gap; G7 item 7 (idle self-update during
   pilots); `/ask` under the graph.

## 4. Coordination and traps
- No other session writes maestro or DuetFlow. In the `agents` tmux session, kill only pilot windows; by PID.
- Halt DuetFlow before committing maestro code (idle self-update, G7 item 7). Adopt only between pilot tasks.
- Auto mode refuses: writing to the journal, reading bot token values, `git worktree remove --force`,
  `git restore --worktree` + `revert` on DuetFlow main, and having codex write outside its grant (negative
  sandbox probes). Hand such steps to Dan as one `! <command>` line.
- `ctl` is `/home/dan/projects/duetflow/.venv/bin/maestro ctl …` with `MAESTRO_REPO` set.
- pytest: `.venv/bin/python -m pytest --junitxml=<file in repo root> -n auto` (no -q). Delete stray `.junit-*.xml`.
- STATE.yaml threads must parse; quote any `owner:` containing `: `.
- A 02 run takes ~25 min to park (3 rounds + diagnose). Foreground `sleep` is blocked; use a background loop.
- Hand off again at ~120K context.

## 5. Report back (with numbers)
As in s3 §6, plus one line each with commit for D5 (3d4bd09), D6 (3898496), D7 (96570a0), D8 (9c61367), D9 (a86542b),
D10 (d0452f8/da16113), D11 (2b80705, live run 7), D12 (ad742b7, confirmed by Dan), D13 (dd9a777, 9d91b4c, narrowed
0e80da0, live run 8), D14 (0e80da0, live run 8), D15 (0e80da0, live run 8), D16 (0e80da0; severity used live, no
follow-ups yet), D17, and G1–G7 status (G7 be3b0c6..508a9a7; item 7 open). Per 02 run, verdicts and rework:
- run 1: hung on D4, after D9 stall kills;
- run 2: accepted despite 4 P1s (D6), reverted;
- run 3: 2 rejections, then parked on D10;
- run 4: 1 valid rejection, then parked in ~3 min on D11 (9 attempts, rework 2);
- run 5: 2 valid rejections, codex rework could not commit → uncertain, hung (D13) (7 attempts, rework 2);
- run 6: same as run 5 (first D13 fix incomplete) (7 attempts, rework 2);
- run 7: 3 rejections (sol, sol, opus after codex quota pause), diagnose → parked; found D14–D16 (12 attempts, rework 2);
- run 8: 3 rejections, findings 4 → 3 → 1 converging, diagnose had no plan_repair → parked; found D17 (11 attempts, rework 2);
- run 9: fill in.
