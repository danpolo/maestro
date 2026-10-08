# Keep the resumed DuetFlow P12 pilot moving: 08-timers-notify manual step, then collect D31/D33 evidence

## Goal

The modelctl D-A–D-E release is **done and adopted for DuetFlow**. The P12 pilot is **running**.
Continue supervising it. Help Dan through `08-timers-notify`'s manual step, which is a privileged
install of systemd timers that he runs himself. Record genuine D30/D31/D33 evidence.
Report back the pilot items still open from `handoffs/2026-10-07-duetflow-pilot-hold-next.md`.

## Read first

- Root `AGENTS.md` and the auto-memory index. These memories apply especially:
  - no-idle-subagent-polling
  - agent-permission-lists-not-classifier (D30)
  - deny-list-hits-ask-dan-with-recommendation
  - agents-ask-dan-on-spec-gaps (D33)
  - context-management-not-built
  - talk-in-local-time
  - do-cleanup-yourself
- `handoffs/2026-10-07-duetflow-pilot-hold-next.md`: steps 5–6 and its "Verification to report back" list.
- `handoffs/2026-10-08-modelctl-release-continue-next.md`: the release line, now complete. Its results are summarized below.
- `docs/graph-engineering/STATE.yaml` `_p12_open_threads` (gitignored):
  - the first thread, `P12-pilots (RESUMED 2026-10-08 on 0c7eb92 …)`, holds all evidence so far, including the two findings;
  - the D33 thread;
  - the modelctl-integration-review UPDATE.
- Release evidence dir: `handoffs/2026-10-08-modelctl-release/`. It contains:
  - `owner-suite-root-cause.md`;
  - `installation.json`;
  - `pinned-doctor.log`;
  - `monitor-pilot.sh`, the event monitor for this pilot.

## Done (2026-10-08, previous session)

**Release.**
- The owner suite went from 195 passed / 2 failed to **198 passed / 0 failed** against both the main checkout and the released path.
  - Root cause: tests coupled to the host Maestro checkout.
  - One real modelctl defect is fixed in modelctl **ccc0b2f**.
- The self-test of `~/.maestro/versions/0c7eb92…` passed: 5538 passed, 1 xfailed.
- Adopted for DuetFlow only. The pin moved f13b2c7 → 0c7eb92. The global pointer is unchanged at 2f555c28.
- The pinned doctor exited 0, with only 2 warnings: systemd unit missing, and meta origin.
- Manifest amendment: Maestro **322dc7e**.
- The plan header `docs/plans/2026-09-30-modelctl-integration.md` is updated (gitignored).

**Commits.**
- Maestro: 072d399, c4fc6b7, c0f484b, 0c7eb92, 322dc7e. These are on `feat/graph-engineering-foundation` and not pushed.
- modelctl: f39bbc8, 61c9de0, ccc0b2f.
- Dan's light-tier answer: Codex light uses gpt-6-luna; gpt-5.6-luna is pin-only.

**Pilot.**
- HALT was cleared at **02:52:15 IDT**.
- `launch.sh` started `duetflow-watchdog`, which runs PID 1344377 (`maestro watchdog`).
- There is **exactly one controller**: PID **1344437** (`maestro.cli run`), in tmux `agents:orchestrator`. Its flock wrapper is 1344435.
- The controller runs with `PYTHONPATH=~/.maestro/versions/0c7eb92…`.
- **First run:** `run_UfEWmL3Y5cb3VLHN`, task `07-reconciliation-followups`. It **succeeded** at 02:57 IDT.
  - The implementer ran on claude-sonnet-5-5.
  - The gate passed 3 of 3 checks.
  - **proof_review was the first strong node and ran on claude-opus-5-5.**
  - It merged at 9a5db1a and graduated as DuetFlow 225ad82, with 2 follow-ups.
- **Second run:** `run_oyOUdfP3xtxsV571`, task `08-timers-notify`, a manual-prep task with needs-dan.
  - **D30 genuine, first case:** prep_01 asked for `Bash(chmod *)` (`questions/pr-08-timers-notify-f8e68e7b.json`, Telegram msg 144).
    Dan chose Allow at 03:13 IDT, and the family landed in `~/.maestro/permissions.json`. prep_02 then succeeded.
  - **Finding, brief/gate contract.** The prep brief carries only the verification check *prose*, not the `cmd`.
    - As a result, the agent named the script `deploy/install.sh`.
    - But ROADMAP V3 runs `test -x deploy/install_timers.sh`, and V-DAN runs `install_timers.sh`.
    - Dan answered the first manual step with **Redo** and gave the rename as the reason (03:21 IDT).
  - **D30 genuine, second case:** prep_03 asked for `Bash(git mv *)` (`pr-08-timers-notify-c55cd9f1`, msg 149).
    Dan chose Allow at 03:23 IDT. **prep_04 was claimed at ~03:23 IDT.**
  - **Finding, D30 wording.** The dontAsk denial text "Permission to use Bash has been denied" made the agent claim all Bash was denied.
    The transcript shows only `git mv` was denied. Maestro still proposed the narrow family.
  - Both findings are recorded in STATE.yaml.

## Next steps

> **Update 03:26 IDT:** prep_04 succeeded and committed fe3e861, which renames the script to
> `install_timers.sh`, mode 100755. The README and tests were updated, and the script body is
> unchanged (already verified). Manual step `action-08-timers-notify-f5575ee5` is pending as
> Telegram msg 150. It was handed to Dan. Start at step 2's "Afterwards, verify".


1. **Re-arm the monitor.** It must be event-driven, not polling:
   `Monitor(command="bash /home/dan/projects/maestro/handoffs/2026-10-08-modelctl-release/monitor-pilot.sh", timeout_ms=1800000)`.
   Re-arm it on expiry. It emits journal events (idle noise filtered), attempt/run transitions, HALT and controller loss.
   Re-snapshot first:
   - run `pgrep -af "maestro.cli (run|watchdog)"`; expect exactly those two PIDs, or a watchdog relaunch;
   - check the attempts of `run_oyOUdfP3xtxsV571`.
2. **When prep_04 succeeds,** a new manual-step message arrives with Done / Redo / Abandon.
   - Before Dan runs anything, check the branch `impl/08-timers-notify`:
     - `deploy/install_timers.sh` exists with mode 100755;
     - `deploy/README.md` and the tests reference it;
     - the script is still only: root check → `install -m 0644` of the 4 units → `daemon-reload` → `enable --now` of the 2 timers.
   - The previous session reviewed `install.sh` and the units and found them safe:
     - both services are oneshot, run as `User=dan` from `/home/dan/projects/duetflow`, and use `.env`;
     - collect runs hourly; refresh runs daily at 05:00;
     - both have `Persistent=true`, so collect fires immediately after install.
   - Then give Dan the exact command from the Telegram message: `sudo bash /home/dan/projects/duetflow/.orchestrator/worktrees/08-timers-notify/deploy/install_timers.sh`.
     He then taps **Done**. Never run it yourself; the hook blocks it anyway.
   - Afterwards, verify with `systemctl list-timers 'duetflow-*'` (unprivileged).
   - Then the gate runs, and V-DAN live verification follows: an induced failure must reach Telegram, and stopping a timer must trip the heartbeat alert.
     Walk Dan through it per `deploy/README.md`, one step at a time.
   - Bare Redo/Reject buttons open a follow-up message. Dan *replies to it* with the reason. Alternatively, `/verify <task> redo <reason>`.
3. **If a prep attempt fails a third time without a permission ask,** read `failure_json` and `impl.log`.
   The ladder: 2 retries → stronger diagnoser → replan. A failure that isn't Dan's to fix is a pilot finding, not something to patch in DuetFlow.
4. **D33 /ask.** If an agent raises a genuine waiting `/ask`, give Dan three things:
   - the bot: @MaestroGenericTestingBot;
   - the exact message;
   - the expected reply.
   Record the evidence paths. D31: record only genuine events.
5. **After 08 settles,** the queue is:
   - `03-source-playlist-reader-followups` has `hold: true` (end-of-project polish);
   - another needs-dan task further down has `hold: true`.
   So the controller will probably idle-gate. Report that, and leave the controller running unless Dan says otherwise.
6. **Commits and records.**
   - Commit any Maestro-tracked file change alone.
   - Pilot evidence goes in STATE.yaml (gitignored) and, if criteria are touched, in the manifest. No criteria change is expected.
   - Consider adding the brief/gate-contract finding as its own `_p12_open_threads` entry, owner P12, with a recommendation: include each verification's `cmd` in the brief, or at least the paths it references.

## In scope
- Supervising the pilot.
- Guiding Dan through Telegram asks and manual steps.
- Evidence and findings.
- STATE updates.

## Out of scope
- Fixing the findings in code. They are recorded for a later phase; ask Dan before any code change.
- The live modelctl rollout (scan, consume, timer).
- `~/.maestro/current`.
- Other projects.
- Push or merge.
- The Context Gate.
- The Telegram approval-flow design. That belongs to the parallel session `handoffs/2026-10-08-modelctl-telegram-approval-flow-next.md`, which never touches DuetFlow.
- Editing DuetFlow code by hand.

## Parallel-session notes
- This line owns `/home/dan/projects/duetflow`, its `.orchestrator/` and the manifest.
- The approval-flow session works on `feat/graph-engineering-foundation` too. Commit small; everything is local.
- Leave `docs/dependency_map.md` (DuetFlow, pre-existing modification) and the main checkout's untracked files alone.
- DuetFlow `docs/UPCOMING.md` and `docs/dependency_map.png` were modified by the controller itself at 02:58 IDT when it rendered the map after 07 graduated. They aren't ours; leave them to Maestro.

## Verification to report back
- The 08 outcome:
  - prep_04 result;
  - the `install_timers.sh` check;
  - the time Dan ran the install, in IDT;
  - `systemctl list-timers` output;
  - gate result and V-DAN result;
  - run status and the merge/graduation commits.
- Any further D30 asks: request id, family, Dan's answer, and the `permissions.json` diff.
- D31/D33 evidence with file paths, or "none genuine yet".
- The controller is still single (PID), and whether HALT was touched.
- What the first strong node used: already claude-opus-5-5. Note it if any Codex strong route (gpt-6.1-sol) is exercised.

## Suggested skills
`monitor-long-running-tasks`, `systematic-debugging` (only for an unexplained failure),
`verification-before-completion`, `close-session` at the end.

## Note from the model-asks build session (2026-10-08, ~04:10 IDT)

- `feat/graph-engineering-foundation` fast-forwarded 322dc7e → 45bcbc9: Telegram model asks
  (`maestro/model_asks.py`, `maestro models asks process`, `docs/MODEL_TRANSITIONS.md` "Telegram
  approval"). modelctl master → 225780f (defers its "Run: modelctl add" notice to Maestro).
- **DuetFlow may adopt this release, but only with a manifest amendment that you (the pilot
  session) record** (Dan's decision 5, `handoffs/2026-10-08-modelctl-telegram-approval-build-next.md`).
  Nothing in DuetFlow was touched; adopting is your call within the pilot's rules.
- Live effect before Dan's rollout go-ahead: none sent. Asks send to Telegram only once
  `~/.maestro/models/asks/telegram.enabled` exists; until then the live `modelctl-scan` timer's
  consume creates and holds asks (`send_pending`). Rollout is pending Dan's go-ahead.
