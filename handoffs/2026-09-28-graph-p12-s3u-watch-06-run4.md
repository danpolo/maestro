# Handoff: Graph P12, session 3u. Watch DuetFlow 06 run 4, the first live test of D26 (agents ask Dan)

This file is the whole prompt. Session s3t built, tested and integrated D26. It stopped at the 180K handoff line while
06 run 4 was running. **Your job: watch run 4 to a V-DAN, a settle or a park. Report any D26 question it asks.
Finish the live Telegram check if s3t could not.**

## 0. Read first
- `docs/graph-engineering/STATE.yaml`: the D26 thread (search `Pilot finding D26 FIXED`) and D27 (the lost-write
  case run 3 parked on).
- `handoffs/2026-09-28-graph-p12-s3r-dan-spec-questions.md`: §4 (report-back) and §5 (the pilot session's note:
  watch run 4 to a V-DAN or a settle; if you stop first, leave HALT in place and say so).
- Memory: `talk-in-local-time` (IDT = UTC+3), `handoffs-are-the-whole-prompt`, `gaps-are-not-decisions`,
  `llm-failure-handling-policy`, `no-idle-subagent-polling`.

## 1. State (2026-09-28 ~02:30 IDT)
- maestro `feat/graph-engineering-foundation`:
  - 294922d: feat(graph), D26;
  - 8be7937: manifest `code_under_test` amendment a4ea0a9 → 294922d.
  - Suite: 5106 passed, 1 xfailed (baseline 5068).
- DuetFlow is pinned to `/home/dan/.maestro/versions/294922d…`. Doctor: OK plus the 2 known WARNs.
- HALT was removed at 02:07:46 IDT and the watchdog relaunched.
- 06 run 4 = `run_ORxN6bPzdwJhdXDy`, opened 02:07:53 IDT (fresh `control.sqlite3`; run 3 is archived as run 12).
- The worktree `/home/dan/projects/maestro-wt/dan-questions` (branch `s3r/dan-questions`, merged) can be removed.
  Its `docs/graph-engineering` symlink makes `git worktree remove` need `--force`, so hand that to Dan as a `!` line.
- The pilot session (maestro-36) has stepped back; it won't touch DuetFlow.

## 2. Live Telegram check: DONE by s3t
- msg 71 tap recorded 23:28:08Z, msg 72 free-text reply recorded 23:29:08Z (answer file, status answered,
  danreq_answered). The dummy records are deleted. Dan confirmed the ✅ edits. Nothing is left here.

## 3. Watch run 4
- Use a Monitor on `.orchestrator/journal.ndjson`: `dan_*`, `graph_run_*`, `watchdog_*`, `orchestrator_*`, and any
  event naming park/halt/error/crash/fail/refus. Also check the control DB for a `park` node that is `claimed`.
- Expected D26 behaviour, if the reviewer hits the lost-write case:
  - `dan_question_asked`; a "❓ Decision needed — 06…" message goes to Dan;
  - the cone waits, and `awaiting_dan_questions` is in `state.json`;
  - after Dan answers: `dan_question_answered`, and a `roadmap(06-writer-first-light): record Dan's decision …`
    commit on DuetFlow `main` touching only `docs/ROADMAP.md` plus one `docs/adr/*.md`;
  - the rework brief carries DAN'S DECISIONS.
- Record in the STATE D26 thread: whether the ask was on target, and how long the ADR agent call held the tick (the
  known limit; `dan_question_answered` minus the tap time).
- On a park: archive per s3j §3 item 3 (export first, rename the branch, move control.sqlite3*, attempts and
  artifacts, keep HALT). Diagnose from evidence before any fix.
- On an accept or a V-DAN: report to Dan.

## 4. Traps
- Auto mode refuses journal writes, token reads and `git worktree remove --force`; hand those to Dan as one
  `! <command>` line.
- `sqlite3.connect` on a missing DB path creates an empty file. Check that it exists first, or open it read-only
  with `file:...?mode=ro`.
- Foreground `sleep` is blocked; use background until-loops or Monitor.
- Hand off again at ~180K context.
