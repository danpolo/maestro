# Handoff: Graph P12, session 3e. Verify the 02 revert, amend the manifest, relaunch run 3 of 02, continue the pilot

Continue Phase P12 on `feat/graph-engineering-foundation` (maestro). This file is the whole prompt. It
**supersedes** `handoffs/2026-09-24-graph-p12-s3d-review-verdict-and-graduation.md`. Its §3 steps 5–7 and §4 still
apply and are restated below where they changed. `docs/graph-engineering/STATE.yaml` and `handoffs/` are gitignored
(edits live on disk only).

## 0. Read first (and only these)
1. `handoffs/2026-09-18-graph-p12-s3-pilots.md` §1, §3 (steps 3–9), §4, §5, §6.
2. `handoffs/2026-09-24-graph-p12-s3b-restart-pilot.md` §3 steps 6–9 (relaunch, what to watch, the V-DANs, and
   the queued model-catalog thread).
3. `docs/graph-engineering/STATE.yaml`: search `Pilot finding D6`, `D7`, `D8`.
4. `artifacts/graph-engineering/p12-pilots/manifest.json`: `.amendments` (2 entries so far).
5. `docs/DESIGN.md` §16: "The proof review's verdict" and "The target checkout follows the merge".

## 1. What session 3d did (2026-09-25)
- **D7 fixed, `96570a0`**: `_graph_main` graduates every runnable task that has a `succeeded` run, on every poll. The lane
  ROADMAPs now have `### <id>` headings.
- **D6 fixed, `3898496`**: the reviewer brief asks for `review.json` `{verdict, findings}` in the WORKSPACE before
  DONE. `agent_handler` adopts it:
  - approved → succeeded;
  - changes_requested or rejected with findings → `verification_failed`. `retry_cone(..., kind=)` then reaches back to
    the implementer (implementer, gate, review), and `rework_note` hands the findings to the implementer;
  - no verdict, an unreadable one, or no findings → `garbage_output`, and the reviewer is asked again.

  The budget is 3, then park. Lanes: `e2e_review_rejects_then_approves`, `e2e_review_inconclusive`.
  **Open gap (recorded, not blocking):** a model-endpoint harness bound to the reviewer is not judged by verdict.
- **D8 found and fixed, `9c61367`**: merge moves the target with `update-ref` and never updated a checkout of that
  branch, so DuetFlow's main checkout stayed on the pre-merge tree with an index that read as a *staged revert* of the
  merge. D7's graduation then ran a whole-index `git commit`, which would have landed that revert on main (reproduced in
  the D7 lane). Now `StagingPipeline._follow_checkouts` runs `read-tree -m -u old new` in every checkout of the target;
  a refusal is recorded as `stale_checkouts` on the `TargetBranchFastForwarded` event and in merge's detail. Graduation
  also commits only its own staged paths.
- Full suite at `9c61367`: junit tests=4800 failures=0 errors=0 skipped=1, rc 0.
- **Adopted `9c61367` into DuetFlow** (`.orchestrator/current`; doctor `adopted_code` OK, plus the two known WARNs).
- `638963f`: the run 2 export was committed as `p12-pilots/duetflow-run2-archived.json` (1 task, accepted, 5 attempts,
  rework 0).
- Run 2 is archived by `mv` into `duetflow/.orchestrator/archive/2026-09-24-run2/` (control DB, attempts, artifacts,
  HALT). **HALT is therefore gone**, but nothing launches on its own: no systemd unit is installed and no watchdog is
  running. `impl/02-collector-schema` had already been deleted at accept, so there was nothing to rename. `state.json`
  has `in_flight: []`. The ROADMAP (`docs/ROADMAP.md`) still has 02 `status: open`.
- **The revert of 416e970 is NOT done by Claude.** The auto-mode classifier refused it. Dan chose to run it himself:
  `cd /home/dan/projects/duetflow && git restore --staged --worktree --source=HEAD -- . && git revert -m 1 --no-edit 416e970`
  (the checkout's index was exactly `416e970^1` because of D8, so the restore is needed before a revert).

## 2. In scope, in order
1. **Verify Dan's revert** (read-only): `git -C duetflow log --oneline -3 main` shows a `Revert "integrate(impl/02-…"`
   commit on top of 416e970; `git diff 98033e5 main --stat` is empty; `git status --short` is clean. If the revert is
   not there, stop and ask Dan. Do not do it yourself.
2. **Manifest amendment** (append to `.amendments`, then commit). kind `code_under_test`: maestro_from 3d4bd09 →
   maestro_to `9c6136785db3cdbf63070868e409181319b910cc`, adopted path as in entry 0. Reason: D6/D7/D8 as in §1.
   rerun `["02-collector-schema"]`, with rerun_reason: run 2 was accepted despite a rejecting review (D6); reverted on
   DuetFlow main; archived as above. `criteria_or_weights_changed: false`.
3. Overwrite the untracked, stale `p12-pilots/duetflow.json` only with the final run's export (at the end).
4. **Relaunch**: `cd /home/dan/projects/duetflow && setsid nohup bash launch.sh` (check HALT is absent; use
   `ctl resume` only if paused). Then continue per s3b §3 steps 6–8. Wait with a background `until` loop on the
   control DB (run status, an `uncertain` attempt, `dan_verify` in the journal, or the watchdog session disappearing),
   and **never** with repeated sleeps.
5. On each run settling, check **the proof_review verdict** (`outputs.review` in the reviewer attempt's result) and the
   rework count, not just `succeeded`. After an accept, check that DuetFlow's checkout is clean and at main
   (`git status --short` empty; no `stale_checkouts` in the event) and that the graduation commit touches only docs.
6. Then s3 §3 steps 5, 7, 8 and 9 (retention re-measure, qualification re-run, verdict, close P12). At the close, name
   as next-task candidates: the model-catalog-config thread, the failure-handling phase (D4), and the reviewer-verdict
   gap for model-endpoint harnesses.

## 3. Coordination and traps
- No other session writes maestro or DuetFlow. The `agents` tmux session hosts other work: kill only pilot windows.
- `pkill -f <pattern>` matches its own shell. Kill by PID.
- The classifier refuses `git worktree remove --force` and refused `git restore --worktree` + `git revert` on
  DuetFlow main. Hand such steps to Dan as one `! <command>` line.
- `ctl halt` works while the loop runs. `ctl pause/resume` refuse while it runs.
- The watchdog's `clear_pause`/`clear_blocked_on`/`reconcile_leases` commands are dead letters (STATE thread).
- Adopt needs `MAESTRO_REPO=/home/dan/projects/duetflow` set.
- The repo's pytest addopts has `-q`, so passing `-q` makes runs silent. Use junitxml for counts.
- Keep reads narrow (sed -n, jq) and use `scripts/graph_pilot_export.py`.

## 4. Report back (with numbers)
As in s3 §6, plus: D5/D6/D7/D8 one line each with commit (3d4bd09 / 3898496 / 96570a0 / 9c61367), and for each 02
run its review verdict and rework count (run 1: hung on D4; run 2: reviewer found 4 P1s, accepted anyway by D6, reverted).
