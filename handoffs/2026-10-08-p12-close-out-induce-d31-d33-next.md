# P12 close-out: close D30 and B15, induce live D31 and D33 evidence, then the P12 verdict

## Goal

The DuetFlow pilot has run out of runnable work. 07-followups, 08-timers-notify and
03-followups all succeeded on 2026-10-08. The only task left is `09-observe-tune`, which has
`hold: true` because it needs two weeks of real collection first.

P12 now needs a few things closed and two live proofs before its verdict can be written.

**Dan's decision (2026-10-08 ~05:10 IDT): induce D31 and D33 on purpose. Do not wait for them to happen organically.**

The vehicle is one small, controlled DuetFlow task: a sweep of the open follow-ups in DuetFlow
`docs/FOLLOW_UPS.md`, which holds 3 items. That task also yields:
- **D33:** one genuine waiting `/ask` that reaches Dan on Telegram and is answered by him;
- **D31:** one quota pause that recovers automatically, with the waiting node routed again and no quality failure.

After that, write the P12 qualification verdict. Then tell Dan the post-P12 feature queue is next
and ask him to elaborate on each item before any planning.

## Read first

- Root `AGENTS.md` and the auto-memory index. These memories apply especially:
  - context-management-not-built
  - no-idle-subagent-polling
  - talk-in-local-time
  - agents-ask-dan-on-spec-gaps (D33)
  - agent-permission-lists-not-classifier (D30)
  - post-p12-feature-queue (now 4 items)
  - manual-step-done-means-ran-it
  - review-follow-ups-become-a-task
  - do-cleanup-yourself
- `docs/graph-engineering/STATE.yaml` `_p12_open_threads` (gitignored).
  - **The first entry is new:** the queue item (4) manual-step contract, which holds three findings.
  - The `P12-pilots (RESUMED 2026-10-08 …)` thread has all of tonight's evidence.
  - The D30, D31, D33 and B15 threads carry the open conditions, quoted below.
- D33 background: `handoffs/2026-09-30-graph-ask-installed-next-live-smoke.md`. This defines what a "genuine" `/ask` smoke must show.
- D31 background: `handoffs/2026-09-29-graph-p12-d31-run18-archive-codex.md`, plus the D31 thread. Relevant commits:
  - ac72b9a: attempt-settle trust boundary;
  - da26cd6: controller-owned durable pause reconciliation, cleared only on newer fresh telemetry;
  - 5c94c9f: crash between recovery marker and mirror clear.
- Manifest and pilot artifacts: `artifacts/graph-engineering/p12-pilots/manifest.json`, `duetflow.json` (untracked; leave it untracked unless the repo already tracks its siblings — check with `git ls-files`).
- Specs: `docs/graph-engineering/specs/README.md` §8–§9 and the P12 phase spec under `specs/phases/` for the verdict's exit criteria.
- Pilot event monitor: `handoffs/2026-10-08-modelctl-release/monitor-pilot.sh`. It is event-driven. Arm it with `Monitor(timeout_ms=1800000)` and re-arm it on expiry.

## Current state (2026-10-08 ~05:10 IDT)

**Controller and pin.**
- There is exactly one controller: PID 1344437 (`maestro.cli run`), in tmux `agents:orchestrator`.
- The watchdog is PID 1344377.
- DuetFlow is pinned to `~/.maestro/versions/0c7eb92…`. The global pointer `~/.maestro/current` is unchanged at 2f555c28.
- The controller is idle (`queue_exhausted`). HALT has not been touched.

**DuetFlow main.**
- 30c2612 graduated 03-followups.
- 8ff6f44 is Dan's hold release, which also narrows the `config.yaml` criterion.
- 24040ce graduated 08.
- Leave alone: `docs/dependency_map.md` (pre-existing modification) and the `docs/UPCOMING.md` / `dependency_map.png` that the controller re-renders.

**Done tonight (evidence is in STATE).**
- 08-timers-notify:
  - systemd timers are installed;
  - a gate-time deny-list ask happened (D29 genuine, msg 151);
  - V-DAN was approved, and the live checks passed post-merge;
  - the first real timed collect at 04:02 IDT collected 28 plays.
- 03-followups: approved by the claude-opus-5-5 proof review, with 1 follow-up.
- A local heartbeat checker was built. This was Dan's choice instead of an external monitor:
  - the script is `~/.local/bin/duetflow-heartbeat-check`;
  - it runs from user units `duetflow-heartbeat.{service,timer}` every 15 minutes, with a 90-minute bound;
  - it alerts through the `.env` bot and chat, sending one alert per outage and one recovery message.
- `config.yaml` now has `notify.telegram_chat_id`. `config.yaml` is gitignored; a backup is in `handoffs/2026-10-08-modelctl-release/duetflow-config.yaml.bak`.

**D30 genuine x2 tonight.**
- `Bash(chmod *)` and `Bash(git mv *)`. Both were Allowed by Dan on Telegram, and the families landed in `~/.maestro/permissions.json`.
- The D30 thread's open condition was "pending a quota-available live Claude run". That condition is now met.

## Steps

1. **Close D30.**
   - Mark the D30 thread closed in STATE, citing tonight's two asks:
     - `questions/pr-08-timers-notify-f8e68e7b` (msg 144);
     - `pr-08-timers-notify-c55cd9f1` (msg 149).
   - Note that the D30 wording finding is still open inside the RESUMED thread: the denial text made the agent claim that all of Bash was denied.
   - Update the manifest only if a criterion's status is recorded there.
2. **B15 retention.**
   - Measure `.orchestrator` bytes per accepted task across the real DuetFlow runs: attempts, the control DB with its WAL, and questions.
   - Compare against `config.RETENTION_DEFAULTS` / `RETENTION_BASIS`, which are 1 GiB warn with prune off.
   - Revise them only if the numbers say so. That would be a Maestro code change: commit it alone with its test.
3. **Design the induced-evidence task.** Bundle everything Dan must approve into ONE question.
   - **Vehicle:** one ROADMAP task in DuetFlow, for example `10-followups-sweep`. Its bar is: do or decline each of the 3 `docs/FOLLOW_UPS.md` items with a reason, and tick them off. No new scope.
     Each criterion needs a `cmd` (pytest `-k`).
     Its scope must cover the follow-ups' files (`duetflow/db.py`, `duetflow/config.py`, `tests/test_playlist.py`, `tests/test_config.py`) and `docs/FOLLOW_UPS.md`.
   - **D33.** Find a way to get a *genuine* waiting `/ask`. It has to come from a real spec gap, not a scripted echo.
     Candidate: the first 07 follow-up, the pre-upgrade DB, is a real judgement call. The question is whether to insert a missing history row or to decline because no such DB exists in production. The task notes can say: "this is Dan's call; ask him via /ask".
     Check against the D33 handoff that this still counts as genuine. If it doesn't, propose the closest acceptable alternative.
     Give Dan three things: the bot (@MaestroGenericTestingBot), the exact message, and the expected reply.
   - **D31.** Work out how to induce a real quota pause safely and observe automatic recovery, without hand-clearing state.
     Read the D31 thread to see which pause source the controller honours (account telemetry vs `usage.json` / the pause mirror).
     Prefer a mechanism the code supports as an operator or test seam. Never fabricate telemetry in a way that would mislead the evidence: the evidence must record exactly what was induced.
     If no honest induction path exists, stop and tell Dan, with options.
4. **Run it.** Arm the monitor, guide Dan through any Telegram ask, and record the evidence paths.
   - Record D31 and D33 only for what genuinely happened.
5. **P12 verdict.** Write the qualification report and verdict against the P12 exit criteria.
   - Count acceptance decisions per run (two rows per accepted run, which is by design).
   - Per the collaboration policy, deliver it as a `visual-explainer` artifact, sent with `send-to-me`.
6. **Close the P12 thread for the post-P12 queue:** tell Dan the queue is next and ask him to elaborate on each of the 4 items. Then mark that thread `needs-dan`.

## In scope
- STATE and manifest records.
- The B15 measurement, and its retention change if warranted.
- One DuetFlow ROADMAP task, added through Maestro's normal ROADMAP conventions: commit it alone, matching the earlier `roadmap(...)` commits.
- Supervising the run.
- The P12 report and verdict.

## Out of scope
- Building the queued features: the manual-step contract, the brief/gate contract, and moving follow-ups to a polish stage.
- The Context Gate.
- Push or merge of `feat/graph-engineering-foundation`.
- `~/.maestro/current`.
- Other projects.
- Editing DuetFlow code by hand.
- The modelctl Telegram approval-flow session's files: the uncommitted `maestro/model_asks.py`, `model_detection.py`, `docs/MODEL_TRANSITIONS.md` and `tests/test_model_asks.py` in the main checkout. They are not ours.

## Parallel-session notes
- The modelctl approval-flow line also works on `feat/graph-engineering-foundation` in the main checkout, and it has uncommitted changes there right now.
- Commit only your own files, and commit them alone. Never `git add -A`.
- This line owns `/home/dan/projects/duetflow`, its `.orchestrator/`, and the P12 manifest.

## Verification to report back
- D30: closed, with the STATE line.
- B15: bytes per accepted task (number and method); change made or "no change".
- The induced task:
  - its run id and node models;
  - the `/ask` request id, the Telegram message id, Dan's answer, and where it landed (ROADMAP decisions or brief);
  - the pause start, the recovery time (IDT) and the telemetry that cleared it;
  - the run outcome, and the merge/graduation commits;
  - whether `FOLLOW_UPS.md` items were ticked or declined.
- The controller is still single (PID), and whether HALT was touched.
- The P12 verdict artifact link and its delivery status.

## Suggested skills
- `monitor-long-running-tasks`
- `verification-before-completion`
- `systematic-debugging` (only for an unexplained failure)
- `visual-explainer` for the verdict
- `close-session` at the end
