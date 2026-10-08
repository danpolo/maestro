# P12 close-out, part 2: supervise 10-followups-sweep (D33), decide D31 with Dan, then the P12 verdict

## Goal

Finish the P12 close-out that `handoffs/2026-10-08-p12-close-out-induce-d31-d33-next.md` started:
1. Supervise DuetFlow `10-followups-sweep` (run `run_TmlHz98N6pniAwcd`) to acceptance. Use its V-DAN wait for the **D33** genuine `/ask` smoke.
2. Get Dan's decision on **D31** (the planned induced pause did not fire; options below), and carry it out.
3. Write the **P12 qualification verdict**, a `visual-explainer` artifact delivered with `send-to-me`.
4. Tell Dan the post-P12 feature queue (4 items) is next and ask him to elaborate on each one. Then mark that thread `needs-dan`.

## Read first

- Root `AGENTS.md` and the auto-memory index. These memories apply especially:
  - context-management-not-built
  - no-idle-subagent-polling
  - talk-in-local-time
  - post-p12-feature-queue
  - manual-step-done-means-ran-it
  - do-cleanup-yourself
  - handoffs-are-the-whole-prompt
- The previous handoff, `handoffs/2026-10-08-p12-close-out-induce-d31-d33-next.md`. It has the original steps 4–6 and the verification list. This file supersedes its steps 1–3.
- `docs/graph-engineering/STATE.yaml` `_p12_open_threads`. Look at:
  - D30 (now closed);
  - B15 (now closed);
  - D31 (new 2026-10-08 note at the top of the entry);
  - D33;
  - the `P12-pilots (RESUMED 2026-10-08 …)` thread;
  - the "two acceptance_decisions rows" thread.
- D33 definition: `handoffs/2026-09-30-graph-ask-installed-next-live-smoke.md`, section "Genuine support delivery verification".
- `tasks/lessons.md`, last entry (2026-10-08): a ROADMAP edit is live before it is committed.
- Pilot event monitor: `handoffs/2026-10-08-modelctl-release/monitor-pilot.sh`. Arm it with `Monitor(timeout_ms=1800000)` and re-arm it on expiry. Its query already covers this run.

## Done in session 1 (2026-10-08, 05:10–11:08 IDT)

- **D30 CLOSED** in STATE. Evidence:
  - two genuine dontAsk asks: `pr-08-timers-notify-f8e68e7b` (msg 144) and `pr-08-timers-notify-c55cd9f1` (msg 149);
  - no classifier hits in any of tonight's Claude attempt logs (runs UfEW, oyOU, buS3).
  - The D4 retry-on-classifier-outage PROPOSAL is marked superseded. The D4 no-sentinel fix stays open.
  - The D30 wording finding stays open in the RESUMED thread.
  - The manifest records no per-finding status, so it was not changed.
- **B15 CLOSED**, with no value change. Maestro commit `6e1c10e` touches only the `RETENTION_BASIS` text and the template comment; template/cli tests passed. The measurement (`du -sb`):
  - DuetFlow `.orchestrator` is 84.07 MB over 11 accepted tasks, which is **7.6 MB per accepted task**;
  - of that, `archive/` (18 operator-archived failed runs) is 75.9 MB;
  - without the archive it is **2.0 MB per task**, with the control DB at 0.88 MB plus a 4.14 MB WAL;
  - at the archive-heavy rate, 1 GiB covers about 140 tasks.
- **Task added.** DuetFlow `10-followups-sweep` was committed alone as `88d5c79 roadmap(10-followups-sweep): …`.
  - Dan approved the design.
  - Checks:
    - V1 `pytest tests/test_playlist.py -k vetoed_on_a_clean_refresh`;
    - V2 `pytest tests/test_config.py -k comment_only_person_a`;
    - V3: FOLLOW_UPS has no `- [ ]` item and at least 3 `Done:`/`Declined:` lines;
    - V-SUITE;
    - V-DAN: "Dan confirms the three dispositions".
  - The notes record that the live DB has `duet_state_meta.prior_state` NULL, so item 1's pre-upgrade state doesn't exist. The agent may decline item 1, or ask Dan through the D26 question channel. If it asks, that is a genuine D26 live ask: record it in the D26 thread.
- **Run `run_TmlHz98N6pniAwcd` opened at 11:01 IDT** (08:01:42Z), before the commit. The controller reads the working-tree ROADMAP; see the lesson.
  - At 11:07 IDT the attempts were:
    - implementer_01: claude-sonnet-5-5, succeeded;
    - gate_01: failed, `verification_failed`, V1 failed;
    - implementer_02: sonnet, succeeded;
    - gate_02: succeeded;
    - proof_review_01: claude-opus-5-5, running.
  - That makes 1 quality failure so far (gate_01).
- **Leave alone in the DuetFlow main checkout:** the controller re-rendered `docs/UPCOMING.md`, `docs/dependency_map.md` and `.png`, and these are modified. `dependency_map.md` was already modified before this session.

## D31: NOT done. Dan must choose (ask in ONE question)

Facts:
- Only a CLI limit line in an attempt's `impl.log` writes a durable `pool_pauses` row. That path is `claude.parse_exit` → `classify_exit` → runner `pause_for` → `lifecycle.record_pool_pause`.
- Recovery is `routing.reconcile_pauses`, called each poll at `orchestrator.py:2897`. It needs:
  - `reset_known=1`;
  - a fresh `claude_oauth_usage_api` sample taken at or after `recorded_at` and within 120 s;
  - every window below 100%.
- It then journals `pool_recovered`.
- Dan picked a "labelled fake limit": a one-shot wrapper at `handoffs/2026-10-08-p12-closeout/d31-induced/bin/claude`.
  - It fires only for `claude -p … --session-id` with cwd under DuetFlow `.orchestrator/worktrees|attempts`, and only when the file `ARMED` exists.
  - It prints `You've hit your limit · resets 1:10pm (Asia/Jerusalem)` and exits 1.
  - A dry check classified that line as `quota_exhausted`, reset `2026-10-08T10:10:00Z`. Adjust the reset time to a future time before using it again.
- **It never fired. Nothing was changed:**
  - `~/.local/bin/claude` still points to `~/.local/share/claude/versions/2.1.294`;
  - there is no `ARMED` file;
  - the tmux session env is unset again.
- Why it didn't fire:
  1. tmux gives a new window the *launching client's* PATH, which is the controller's, so `tmux set-environment -t agents PATH` never reaches workers. A probe proved this.
  2. Swapping the `~/.local/bin/claude` symlink was **denied by the Claude Code auto-mode classifier** (Unauthorized Persistence). Do not work around this.

Options to put to Dan:
- **(a)** Dan allows the user-wide symlink swap for a few minutes, for example by running it himself through `!`. Arm it right before the next Claude node of some run, and restore it straight after it fires.
- **(b)** Dan restarts the DuetFlow controller with the wrapper dir first in its PATH. This needs a HALT at an idle boundary and the launch.sh/watchdog path with a modified PATH. It is the more contained option.
- **(c)** Skip it. The P12 verdict records D31 as fixed and test-proven, with automatic live recovery unobserved: a qualified pass with a carry-over.

The vehicle for (a) or (b) would be another run. 10-followups-sweep may already be past its Claude nodes. Never fabricate the recovery telemetry. Record whatever was induced exactly.

## D33: steps

- When the V-DAN request for `10-followups-sweep` arrives, Dan gets it on Telegram from **@MaestroGenericTestingBot**. It is a `verify-10-followups-sweep-*.json` in `.orchestrator/questions/`.
- Give Dan the exact message to send, for example:
  `/ask 10-followups-sweep Item 1 says Declined (or Done) — is that safe given my live DB has prior_state NULL, and which test covers item 2?`
- Expected reply: a support answer about this task's dispositions. The footer should carry `/approve <verify-id>` and `/ask` again. The reply must not say it can't find the task.
- Afterwards, check:
  - the `.answer`/verdict is untouched by the ask;
  - the request/run/worktree context in the reply is correct.
- Record:
  - the request id;
  - the Telegram msg ids;
  - the reply text location;
  - Dan's answer.
- Then Dan approves the V-DAN, and merge, accept and graduation follow.
- If no reply arrives within about 5 minutes, check the controller's telegram listener (`hitl/ask.py`; a timeout message says so) and the journal.

## Then

- Record the run outcome in the RESUMED thread:
  - node models;
  - quality failures;
  - the merge and graduation commits;
  - FOLLOW_UPS dispositions (ticked or declined).
- Write the P12 verdict (step 5 of the previous handoff):
  - count acceptance decisions per run (two rows per accepted run, by design);
  - deliver it with visual-explainer and `send-to-me`.
- Then do the post-P12 queue ask (step 6).

## Update 11:09 IDT (just before close)

- proof_review_01 (claude-opus-5-5) **succeeded**. dan_confirm_01 is claimed: the V-DAN request is
  **`verify-10-followups-sweep-6132134bdb1f`**, and Dan was told the /ask message in chat.
- Implementer commits: c594ef2, then 6132134 (which names the V1/V2 tests).
- All three items are **Done**:
  - item 1 is implemented: the missing history row is inserted before closing, with a `pre_upgrade` test;
  - item 2: `test_a_hand_removed_track_is_vetoed_on_a_clean_refresh`;
  - item 3: the by-hand message for a comment-only person_a.
- No D26 agent question was asked.
- First thing next session: check whether Dan already sent /ask and approved; read the journal and the `questions/` .answer.

## State at handoff (11:08 IDT)

- There is a single controller, PID 1344437, in tmux `agents:orchestrator`. The watchdog is PID 1344377.
- HALT was not touched (absent).
- The pin is 0c7eb92. The global `~/.maestro/current` is unchanged.
- Maestro branch `feat/graph-engineering-foundation` HEAD is `6e1c10e`. Untracked files are pre-existing; leave them.
- STATE and lessons are gitignored, and their changes are on disk.
- No monitor or background shell is left running from session 1.

## Out of scope

- Building queued features.
- The Context Gate.
- Push or merge of the feature branch.
- `~/.maestro/current`.
- Editing DuetFlow code by hand.
- The modelctl line's files.
