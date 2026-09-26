# Handoff: Graph P12, session 3k. Pilot 03/04 in flight; Dan's telemetry asks; continue the pilot, close P12

Continue Phase P12 on `feat/graph-engineering-foundation` (maestro). This file is the whole prompt. It
**supersedes** `handoffs/2026-09-26-graph-p12-s3j-run9.md` (its §2 decisions, §4 traps and §5 report format still
apply; read its §2, §3 items 3–4, §4, §5). `docs/graph-engineering/STATE.yaml` and `handoffs/` are gitignored.

## 0. Read first (and only these)
1. `handoffs/2026-09-26-graph-p12-s3j-run9.md` §2, §3 (items 3–4), §4, §5.
2. `handoffs/2026-09-18-graph-p12-s3-pilots.md` §3 steps 5, 7, 8, 9 and §6.
3. STATE.yaml: search `Pilot finding D18`.
4. Memories `pilot-goal-subscription-efficiency.md`, `gaps-are-not-decisions.md`, `llm-failure-handling-policy.md`.

## 1. What session 3j did (2026-09-26)
- Adopted 3fd165c into DuetFlow; manifest amendment fe0cb12.
- **Run 9 of 02 ACCEPTED** (run_kl1TQIeU1n5hga2W → repaired run_0qaUhrJR7x75yCFG, merged b9682e6): 3 rejections
  (blocking 7 → 4 → 1, 3 follow-ups; round 3's sol review hit codex quota → redone on claude-opus-5), diagnose
  `poor_agent` with an implementer addendum, final run approved first time. 16 attempts, ~55 min. D17 LIVE-CONFIRMED:
  `graph_run_repaired` journaled, rev-2 manifest carries `brief_addendum`, final reviewer's `prior_reviews` = 4 rounds
  (19 KB), `push_skipped_no_remote` once.
- **D18 found and FIXED 99553cd**: graduation deleted 03..09 from DuetFlow ROADMAP (strip ran to EOF over bare
  consecutive yaml blocks) and never committed (git add with the missing docs/AUTONOMOUS_SYSTEM.md stages nothing;
  no graph graduation commit had ever landed; lanes' `decision == main head` only held because of it — now
  `_landed()` in test_end_to_end). Repaired by hand on DuetFlow main 7f4f71e (02-only strip). Full suite 4895 tests,
  2 failures = known test_purity on untracked `docs/ABUALI_INTEGRATION_BOUNDARIES.md`.
- `scripts/graph_progress.py` (03361f6): `python3 ~/projects/maestro/scripts/graph_progress.py ~/projects/duetflow
  [--watch 30]`. **Uncommitted edit** on disk: shows every running task (parallel runs). Commit it only while DuetFlow
  is halted/between tasks (idle self-update trap). Known cosmetic: a `dan_confirm` node (no started_at) sorts first
  and is labelled `[prep]`.
- DuetFlow adopted **03361f6**, doctor OK + 2 known WARNs; manifest amendment ca0334b. STATE D18 thread added, D17
  marked live-confirmed.

## 2. Live state at hand-off (10:50Z)
- DuetFlow loop RUNNING (tmux `duetflow-watchdog`; windows in `agents`). 03 and 04 run in parallel.
- **03-source-playlist-reader**: implementer ok, gate ok, proof_review approved (0 blocking, 5 follow-ups); now
  waiting on Dan's V-DAN (`/verify verify-03-source-playlist-reader-36e1f153fd30 approve|reject <reason>`), sent
  10:12Z. Dan was given the `playlists` / `use-source` commands (worktree
  `.orchestrator/worktrees/03-source-playlist-reader`); `playlists` verified working live (19 playlists).
- **04-scoring**: implementer succeeded ~10:07Z, but no gate attempt yet at 10:50Z. **Check first**: is it waiting on
  the codex pool pause (`pool_paused` codex until 12:15Z) or stuck (D4-like)? Use the control DB + journal; a hang is a
  finding.
- The session-3j background watcher dies with that session; start your own (pattern: until-loop on
  `task_runs.status`, uncertain attempts, HALT; see s3j §3 item 2).


**Update 11:31Z:** Dan approved 03's V-DAN (11:29:07Z); 03 run SUCCEEDED, merged 798b677 (0 blocking, 5 follow-ups,
1 review round). Its graduation was still in progress at 11:31Z: the strip is correct (only 03 removed, 7 ids left),
but `roadmap_task_removed` for 03 and the `roadmap(03-…)` commit had not landed yet (likely inside `_refresh_upcoming`,
an Opus call). **Verify first** that the commit lands and the checkout ends clean; if the strip stays uncommitted, that
is a D18 follow-up finding. 04-scoring: implementer succeeded 10:05Z, still NO gate attempt at 11:30Z (85 min) →
treat as a probable hang; diagnose from the control DB/journal before any fix.

## 3. Dan's asks this session (2026-09-26) — NOT decided yet; bring as ONE bundled AskUserQuestion
Dan's goal (memory `pilot-goal-subscription-efficiency`): fewer cycles / lower subscription % per accepted task,
per role. Analysis given to him (run 9 of 02): of 5 later-round blocking findings, 4 were **carried over, fixed
incompletely** (implementer fixed the instance, not the class — despite 3fd165c's rework note), 1 was **exposed by
rework** (01-auth's `_grant_from` leaking the token payload became reachable via cycle 2's runs logging); 0 were
missed by round 1. Codex per-step 5h/weekly: review r1 +14/+2, r2 +17/+2, impl c3 (terra) +7/+1, r3 +15 (→100%)/+3.
Proposed features (maestro, TDD, lanes in tests/graph_engineering/test_end_to_end.py):
1. **Finding origin** field for review rounds ≥2 (optional in round 1): `carried_over` | `missed_earlier` |
   `introduced_by_rework` | `exposed_by_rework` | `scope_widened`; like rule/scope/verify in
   `handlers._split_findings`; reviewer gets the prior reviewed commit; `missed_earlier` cross-checkable by git blame.
   Export it in `scripts/graph_pilot_export.py`.
2. **Per-step usage %** (5h + weekly) for every role. Today Claude attempts read `.orchestrator/usage.json`, a stale,
   account-wide statusline snapshot (5 run-9 attempts have identical start/settle; `model: Opus 5.5` is the
   interactive session) → unusable. Needs a fresh read at claim and settle, per-attempt token counts
   (`token_split` is null everywhere), and attribution across parallel attempts (e.g. token-share of the window delta).
   Codex rows are real.
3. **Follow-ups go nowhere durable** (only the review artifact + next reviewer's prior-rounds text). Propose:
   list them in `phase_report` and append them to a project doc/backlog at graduation.
Ask Dan: build now (halt between tasks, adopt) vs after the pilot; and where follow-ups should land.

## 4. Then, in order
1. Finish 03 (Dan's verify) and 04; 05+ per dependency order. Per accept check: checkout clean at main, graduation
   commit `roadmap(<id>): graduate…` touches only docs (D18 fix live), no `stale_checkouts`.
2. s3 §3 step 7–8 items: 06 needs `duetflow collect`; 08-timers-notify is the manual lane with merge.
3. Archive before any rerun (s3j §3 item 3). Final export overwrites untracked `p12-pilots/duetflow.json`.
4. s3 §3 steps 5, 7, 8, 9: retention re-measure, qualification re-run, verdict, close P12 (ITV only if Dan reports
   grill-me done).

## 5. Report back
As s3j §5, with: D17 (3fd165c; LIVE-CONFIRMED run 9), D18 (99553cd; hand repair 7f4f71e), run 9 line = "3 rejections
(7→4→1 blocking), diagnose poor_agent, final run approved, ACCEPTED (16 attempts)", plus per task 03+ verdicts,
rework, follow-up counts and (where measurable) per-step usage.
