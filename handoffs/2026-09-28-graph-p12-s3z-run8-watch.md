# Handoff: Graph P12, session 3z. Watch DuetFlow 06 run 8 to an accept or a park

This file is the whole prompt. Session s3y finished and adopted D29 (deny-list hits caught in the gate; Dan rules with
a button). It watched 06 run 7, which lost to a classifier outage, archived it as run 16, and started **run 8 at
15:18:21 IDT**. **Your job:** watch run 8 to an accept or a park, guide Dan through V-DAN, then update STATE.

## 0. Read first
- `docs/graph-engineering/STATE.yaml`:
  - `Pilot finding D29`: FIXED c40a89d, with run 7's live evidence and the known limits.
  - `Pilot finding D28`: rounds for runs 5, 6 and 7.
  - `Pilot finding D4`: it recurred in run 7, plus a proposal for Dan's question about escalating classifier denials.
- `handoffs/2026-09-28-graph-p12-s3x-deny-list-ask-dan.md` §3 steps 5–6, §5 and §6. They still apply, except that the
  run is now 8 and the archive would be run 17.
- Memory: `talk-in-local-time` (IDT = UTC+3), `handoffs-are-the-whole-prompt`, `no-idle-subagent-polling`,
  `do-cleanup-yourself`, `deny-list-hits-ask-dan-with-recommendation`.

## 1. State (2026-09-28 ~15:20 IDT, end of s3y)
- **maestro** `feat/graph-engineering-foundation`:
  - c40a89d is D29.
  - 862b466 is the manifest amendment 85d1cb7 → c40a89d.
  - edd681d archives the run-16 export.
  - The suite is **5127 passed + 1 xfailed**.
  - `artifacts/graph-engineering/p12-pilots/duetflow.json` is untracked; leave it.
- **DuetFlow** is pinned to c40a89d (doctor OK + 2 known WARNs). Main is b35d98b and clean. It is **running run 8**,
  `run_TtdRSe58jchh2Jud`; the implementer was claimed at 15:18:41 IDT.
- **Deny-list ruling carries over.** Dan's Approve of `duetflow/db.py` `conn.execute("DELETE FROM duet_state")`
  (key b6be9f3c423fab18, request `dl-06-writer-first-light-7f5381f4`, in `.orchestrator/questions/`) **carries into
  run 8**, because `workflow_id` is per task (`wf_06-writer-first-light`).
  - If the implementer writes that exact line again, the gate passes it without asking.
  - A different deny-listed line asks Dan again, with a recommendation.
- **Run 7 = archive run 16** (`.orchestrator/archive/2026-09-28-run16`, branch `archive/06-writer-first-light-run7` at
  d7c362f). The failed final run's uncommitted fix is saved as `uncommitted-final-run.patch` there. Don't seed run 8
  from it; no seeding mechanism exists.
- **Watch script:** `/home/dan/projects/maestro-wt/watch-run7.sh`. It is persistent, and its DB path is
  `.orchestrator/control.sqlite3`. Arm it with Monitor (30-min max, re-arm). Before a run opens it prints one
  harmless "unable to open database file" traceback.

## 2. In scope
1. **Watch run 8** with the Monitor.
   - Expected: implementer → gate (the carried-over approval, or a new deny ask) → review rounds → V-DAN.
   - If a new deny ask appears (`deny_list_hit_asked` in the journal, request file `questions/dl-*.json`), tell Dan
     the file, the line and the recommendation, then stop re-arming until he taps.
2. **V-DAN.** Tell Dan his steps: **collect, then refresh**, then check in Spotify.
   - The re-consent is done.
   - The Refresh must **reuse** `duet.playlist_id` 1kOGVDMgPBM3fjqzwxS9NV, with 0 create POSTs.
   - Stop re-arming while waiting on him.
3. **After an accept:**
   - DuetFlow main is clean, with no `stale_checkouts`.
   - Graduation touches only docs.
   - `push_skipped_no_remote` appears once.
   - `06-writer-first-light-followups` is created.
   - The merge passes the deny-list re-check with the approved key, with no `merge_refused`.
   - Report to Dan in IDT.
4. **On a park or `uncertain`:**
   - Write `.orchestrator/HALT`.
   - Export with `scripts/graph_pilot_export.py --project /home/dan/projects/duetflow --since <run-8 open minus 1 min, UTC>
     --out artifacts/graph-engineering/p12-pilots/duetflow-run17-archived.json`.
   - Archive as run 17: save any uncommitted worktree diff as a patch, `git worktree remove --force` + prune,
     `git branch -m impl/06-writer-first-light archive/06-writer-first-light-run8`, and move `control.sqlite3*`,
     `attempts` and `artifacts` plus a HALT copy into `.orchestrator/archive/2026-09-28-run17`.
   - Diagnose from evidence. For a classifier outage ("gave no verdict" in `attempts/<id>/impl.log` or in the
     result detail), probe first:
     `claude -p --model claude-sonnet-5 --permission-mode auto "Use the Bash tool to run: echo probe-ok"`.
     Then, per Dan's standing D4 choice, archive and restart.
5. **STATE:** add run 8's rounds, finding origins, V-DAN outcome, Refresh reuse, and accept or park to D28 (and to D29
   if a deny ask occurs).

## 3. Out of scope
- The Context Gate: never build it (AGENTS.md).
- The D4 fix and the classifier-escalation proposal: record only; they belong to the failure-handling phase.
- D23 (reviewer depth).
- The export's `human_interventions` not counting deny taps: recorded in D29; fix it later.

## 4. Traps
- Auto mode's classifier failed repeatedly in s3y, around 14:30–15:15 IDT. Bash calls then fail with "gave no
  verdict". File reads (Read) still work. Write `.orchestrator/HALT` with the Write tool if Bash is down.
- Foreground `sleep` is blocked.
- Auto mode refuses journal writes and token reads; hand those to Dan as one `! <command>` line.
- Hand off again at about 180K.

## 5. Report back
Run 8's id and open time (IDT), whether the gate reused the approval or asked again, review rounds with blocking
counts and origins, the V-DAN outcome, the Refresh reuse (0 create POSTs), and the accept or park.
