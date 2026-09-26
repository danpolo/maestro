# Handoff: Graph P12, session 3l. s3k telemetry is live in DuetFlow; watch 04 onward, verify the new features live, close P12

Continue Phase P12 on `feat/graph-engineering-foundation` (maestro). This file is the whole prompt. It
**supersedes** `handoffs/2026-09-26-graph-p12-s3k-telemetry.md` (its §4 steps 2–4 and §5 still apply; the
s3j handoff §2 decisions, §4 traps and §5 report format still apply). `docs/graph-engineering/STATE.yaml` and
`handoffs/` are gitignored.

## 0. Read first (and only these)
1. `handoffs/2026-09-26-graph-p12-s3j-run9.md` §2, §4, §5; `handoffs/2026-09-26-graph-p12-s3k-telemetry.md` §4–§5.
2. STATE.yaml: search `Pilot finding D19`.
3. Memories `pilot-goal-subscription-efficiency.md` (Dan's 7 finding origins), `review-follow-ups-become-a-task.md`.
4. `git log --oneline 2df7cb1^..92a604d` (what s3k landed).

## 1. What session 3k did (2026-09-26)
- 03 ACCEPTED earlier (V-DAN approved 11:29Z, merged 798b677, graduated c77a0dd: docs only, checkout clean — D18 fix
  confirmed live). 03 had 5 follow-ups, 02 had 3 — both graduated BEFORE the follow-up feature, so no
  `-followups` task exists for them (not queued retroactively; ask Dan only if it matters).
- **D19 found + fixed 00ff57d, LIVE-CONFIRMED**: 04 hung 4.5 h (implementer succeeded 10:04Z, node stuck
  `running`, nothing journaled). Each runner's Reconciler scans every lease; 03's tick finished 04's attempt
  (INGESTED) and 04's `ingest_results` only read claimed/running attempts. Now it also commits finished attempts
  whose node is still claimed/running. After the relaunch 04 went implementer→gate→proof_review.
- Dan's asks built in 4 parallel worktrees (`/home/dan/projects/maestro-wt/{origin,usage,followups,telegram}`,
  branches `s3k/*`, all merged; the worktrees can be removed with `git worktree remove` once you're done with them):
  - **Finding origin** (ac9ba47): ids `r<round>-f<n>`; round≥2 blocking findings carry one of Dan's 7 origins
    (+ `prior_finding` for not_fixed / fixed_incompletely_*); fault table `review_origins.ORIGIN_FAULT`
    (**scope_widened → planner** — Dan said "reviewer/planner"; the agent picked planner, Dan has NOT confirmed:
    mention it); reviewer gets `prior_reviewed_commit`; unknown origin → `unclassified` + `ReviewOriginFlagged`
    event; diagnoser sees the distribution; export `review_origins` per task/round. Rounds = reviews with a verdict.
  - **Per-step usage** (dc749ab, a068c13, 76784c1): fresh Claude 5h/7d via the OAuth usage endpoint (cached ≤1
    fetch/60 s in `.orchestrator/claude_subscription_usage.json`), per-attempt tokens (claude session uuid →
    transcript; codex rollout), attribution across overlapping attempts at export time (`quota` report:
    per attempt/task/role/route/accepted task). Limits: deltas include Dan's other usage; integer %.
  - **Follow-up tasks** (6737163): graduation queues `<id>-followups` (criteria = the follow-ups; checks copied from
    the parent minus dan/await; bar: done or declined with reason, no new scope); its own follow-ups → 
    `docs/FOLLOW_UPS.md`; Telegram phase_report line. Open: a parent with only `kind: dan` checks gives a
    follow-up task with no checks (can't accept) — small gap, fix if it bites; consistency-guard graduation-marker
    regex misses hyphenated ids (result ignored; low).
  - **Telegram** (1e55c83, b09d5a4, 0211182, 4058606): the dependency map was NEVER rendered for DuetFlow (depmap
    required old-format fields) → fixed; each map push pins silently and unpins the previous
    (`.orchestrator/pinned_map.json`). V-DAN: "🛠 Dan action needed" + numbered `steps:` (new optional ROADMAP field
    `{do, run}`), no snapshot/worktree lines. A background listener answers taps instantly
    (`MAESTRO_TELEGRAM_LISTENER=0` disables); the request message is edited to end "✅ Approved" / "❌ Rejected — …".
    Only tested with fakes → **verify live** on the next Dan request (08/09 are needs-dan). DuetFlow's ROADMAP
    still uses `instructions:` (not `steps:`) for its dan checks — converting 08/09 is a project edit; propose to Dan.
- Merged tree 710ce78: full suite 4964 passed, 2 failed (known test_purity on untracked ABUALI doc). Adopted into
  DuetFlow (doctor OK + known meta_branch WARN), manifest amendment 92a604d. Loop relaunched 15:40Z (setsid
  launch.sh; watchdog running).

## 2. Live state at hand-off (~15:43Z)
- 04-scoring run_hN6LPQuShbwkOIEn: implementer ✔, gate ✔, proof_review running (first review ever with origins +
  fresh usage). Codex pool pause ended 12:15Z.
- Next by deps: 05-selector (needs 03 ✔, 04), then 06 (needs `duetflow collect`), 07, 08 (manual lane), 09.
- No background watcher is running — start your own (until-loop on `task_runs.status`, uncertain attempts, HALT).

## 3. In scope, in order
1. Watch 04 to accept/park. On accept check: graduation commit docs-only, clean checkout, a `04-scoring-followups`
   task queued in the SAME commit if there were follow-ups, dependency map pushed + pinned (journal), export shows
   `review_origins` and fresh `quota` rows for 04's claude/codex attempts (stale flags only for pre-s3k rows).
2. Continue the pilot per s3k §4 (05+, 06 collect, 08 manual lane); on Dan's next V-DAN verify the new message,
   instant tap, ✅ edit.
3. Archive before any rerun; final export overwrites untracked `p12-pilots/duetflow.json`.
4. s3 §3 steps 5, 7, 8, 9: retention re-measure, qualification re-run, verdict, close P12.

## 4. Report back
As s3k §5, adding D19 (00ff57d; LIVE-CONFIRMED 04), and per task 03+: verdict, rework rounds, blocking findings by
origin/fault, follow-ups and where they went, per-role attributed 5h/weekly usage.
