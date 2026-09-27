# Handoff: Graph P12, session 3q. Adopt D24, archive and rerun 06 (first live V-DAN), continue the pilot

Continue Phase P12 on `feat/graph-engineering-foundation` (maestro). This file is the whole prompt. It
**supersedes** `handoffs/2026-09-27-graph-p12-s3p-archive-05-and-03f.md`.

Some older sections still apply:
- s3m: §3.4 (the then-steps), §4 (traps), §5 (report format).
- s3j: §2, §3 item 1 (adopt procedure), §3 item 3 (archive procedure), §4, §5.
- s3l: §1, §3, §4.

`docs/graph-engineering/` and `handoffs/` are gitignored. **Talk to Dan in local time (IDT = UTC+3); journals and the
DB are UTC.**

## 1. What s3p did (2026-09-27, 19:45–21:15 local)
- **03-followups on hold** (Dan's decision): DuetFlow **64a7416** sets `hold: true`, with a comment carrying Dan's
  narrowing rule. D22 (was missing from STATE.yaml) and **D23** (a follow-up reviewer has no bound on edge-case scope;
  nothing built) are recorded.
- **Run 9 archived:**
  - Export `p12-pilots/duetflow-run9-archived.json` with `--since 2026-09-26T07:48:00Z` (run 8's archive time). It
    covers 02 run 9, 03, 04, 02-followups, 05 run 1 and 03-followups. Committed as maestro **7175821**.
  - Branches renamed to `archive/05-selector-run1` (holds agy's 509ec9b) and
    `archive/03-source-playlist-reader-followups-run1`.
  - DB, attempts and artifacts moved to `.orchestrator/archive/2026-09-27-run9`. Relaunched 19:50.
- **Small gaps, maestro cf77f9c** (suite 5067 passed, 0 failed):
  - The mmdc failure was transient under load (it renders in 3 s now). `render_png` now captures mmdc's stderr, so
    `dep_map_render_failed` shows mmdc's error line.
  - The halted `/help` and `/status` replies now journal `halted_command_answered`.
  - The **project.yaml leak was real in the main checkout too**. The `redo_case`/selffix fixtures in
    `characterization/test_selfheal.py` wrote the real tracked `project.yaml` through the unrebased `subject.REPO`.
    They now redirect `maestro.config.PROJECT_YAML` into tmp.
  - The doctor test timeout is now 180 s. The test takes 57 s on a quiet host (the doctor call alone 25 s).
- Neither cf77f9c nor 1d19dce has been **adopted into DuetFlow** yet. DuetFlow still runs a9156ec.
- **04-scoring-followups ACCEPTED** (19:52–20:01, 1 round):
  - Implementer sonnet-5, review gpt-6-sol.
  - 1 follow-up done (config errors name the real path), 1 declined with a reason (`explain` via `score_log`: no
    writer yet).
  - Graduation edfde4a, docs only. No new follow-ups.
  - Ran in parallel with 05: 04's block had no `scope`, because DuetFlow's scopes were added after it was queued.
    `build_task` already inherits `scope`.
- **05-selector ACCEPTED** (19:52–20:24, 3 rounds):
  - Rounds 1–2: sonnet-5 implementer, gpt-6-sol rejected.
  - Round 3: the router moved the implementer to codex gpt-6-luna (routing outcomes, no pool pause). Approved.
  - Graduation 2ccdac5, docs only. No follow-ups, so no `-followups` task.
- **06-writer-first-light PARKED** (20:25–21:02), `run_aGcPRNvsB97sHuTG` failed at proof_review, park `claimed`.
  DuetFlow **HALTED** (HALT present), nothing in flight.
  - Revision 1:
    - Gate failed V4 twice: `pytest -k creates_duet_once` selected 0 tests, because no test had that name.
    - implementer_03 (codex luna) wrote FAILED: "cannot create a real Duet without leaving the worktree" (D25).
    - The diagnoser (gpt-6-sol) repaired it.
  - Revision 2 (final run): implementer and gate succeeded. The review found 3 real blocking findings, then it
    parked per policy:
    1. `app.py:312` reads only `item["track"]`, but current Spotify `/items` entries are `{"item": {...}}`, so it
       PUTs zero URIs.
    2. The `invalid_grant` path does not mark `reconsent_required` when the cached token is still valid.
    3. The `create_playlist` POST is retried on 5xx, which can create duplicate Duets.
- **D24 FIXED maestro 1d19dce** (lane `test_a_gate_failure_is_reworked_from_the_failed_checks_output`; suite 5068
  passed):
  - Cause: after a gate rejection, implementer_02's brief was word for word the first, because `rework_note` covered
    only reviewer and Dan rejections.
  - Fix: it now lists each failed check (id, check text, command) plus its output, bounded by
    `failure_context.render_evidence`.
- **D25 OPEN (small gap)**, see STATE.yaml. Criteria come from every check's text, V-DAN included, so the implementer
  is told to make "a real Duet exist in Spotify".

## 2. In scope, in order
1. **Adopt 1d19dce** (it includes cf77f9c) into DuetFlow per s3j §3 item 1, while DuetFlow stays halted.
   - Run doctor: expect OK plus the 2 known WARNs.
   - Add a manifest amendment for code_under_test a9156ec→1d19dce, with a `note` covering D24, the small gaps and
     s3p's accepts. Commit it.
2. **D25 (optional before the rerun; small):** fix it if cheap (see the STATE.yaml thread for the approach and the
   trap), then adopt too. Otherwise rerun without it. Revision 2 of the next run gets the diagnoser's addendum
   either way.
3. **Archive run 10 (06) and rerun 06** (s3j §3 item 3):
   - Export `duetflow-run10-archived.json` with `--since 2026-09-27T16:49:00Z`.
   - Rename `impl/06-writer-first-light` to `archive/06-writer-first-light-run1`.
   - Move the DB, attempts, artifacts and a HALT copy into `.orchestrator/archive/2026-09-27-run10`. Keep a HALT.
   - Commit the export, then remove HALT.
   - Rerun watch items:
     - After any gate failure, the rework brief carries "The gate (gate) rejected…".
     - At V-DAN (the first live one): the numbered steps (`collect` then `refresh` in `{worktree}`), the instant tap,
       and the ✅ edit. Tell Dan when it arrives: he has to run the steps and tap.
4. Continue per s3m §3.4:
   - 07, 08 (manual lane), 09.
   - The final export (with contamination) overwrites the untracked `p12-pilots/duetflow.json`.
   - Then the retention re-measure, the qualification re-run, the verdict, and close P12.
   - **When closing P12, tell Dan the specs/README.md §9 queue is next and ask him to elaborate first.**

## 3. Traps (in addition to s3m §4)
- The watcher `scratchpad/watch_accept.sh` from s3p lives in a session scratchpad and is gone. Recreate it:
  - `tail -n0 -F journal.ndjson`.
  - On `task_complete`/`graph_run_settled`: touch HALT and exit.
  - On `dan_verify_requested`/`stall_restart`/`halt_detected`/`pool_paused`: just exit.
  - It halts even while a parallel task is mid-flight. s3p removed HALT within 18 s, before the loop noticed. Remove
    HALT promptly unless you mean to stop.
- Graduation lands ~25–50 s after `graph_run_settled`. Wait for the `roadmap(<id>): graduate` commit before judging
  the checkout.
- The host was quiet in s3p: the suite takes 2–4 min, not 18.
- Hand off again at ~180K context (Opus 5.5).

## 4. Report back
As s3m §5, adding D22 (a9156ec), D23 (recorded), D24 (1d19dce, live?), D25, and per task: verdict, rounds, the
blocking findings by origin, follow-ups, and attributed usage.
