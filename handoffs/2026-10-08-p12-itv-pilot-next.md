# P12 stays open: onboard and pilot instagram-to-value (after Dan's grill-me)

## Goal

On 2026-10-08 Dan chose **"Keep P12 open for itv"** rather than closing P12 as a qualified pass on DuetFlow alone. P12's spec requires a second scratch pilot on `/home/dan/projects/instagram-to-value` (itv). That pilot never ran, so B01 and B18 have no evidence. This session has four jobs:

1. **Precondition: Dan has run `grill-me` on itv himself.** Check for `CONTEXT.md` and ADRs in itv. If they are missing, stop: tell Dan the grill is the next step and do nothing else (memory: grill-with-docs-precedes-maestro-init).
2. Onboard itv with `maestro-setup`, starting from CONTEXT.md and the ADRs, and with the spec's safeguards:
   - `bot_files`: `scripts/bot.py` and `scripts/worker.py`;
   - `deny_list_extra`: `jobs/ staging/ media/ out/ logs/ config/`;
   - `gate.chain: [test]`;
   - tmux isolation from `itv-bot` and `itv-worker`.
3. Run representative pilot tasks. Supervise them the way DuetFlow was supervised.
4. Then close the remaining P12 gaps listed in the audit, write the P12 verdict as a `visual-explainer` artifact sent with `send-to-me`, and ask Dan about the post-P12 feature queue (spec "On closing P12").

## Read first

- Root `AGENTS.md` and the auto-memory index, especially:
  - context-management-not-built
  - grill-with-docs-precedes-maestro-init
  - no-idle-subagent-polling
  - talk-in-local-time
  - do-cleanup-yourself
  - handoffs-are-the-whole-prompt
  - post-p12-feature-queue
- `docs/graph-engineering/specs/phases/P12_QUALIFICATION_AND_RELEASE.md`: §2 has the itv safeguards, §3 the acceptance criteria.
- **`handoffs/2026-10-08-p12-closeout/p12-verdict-evidence.md`**: the full audit of criteria, thread classification and DuetFlow tally. It recommends a QUALIFIED PASS and lists what is needed for a full PASS. Use it; don't redo it.
- `docs/graph-engineering/STATE.yaml` `_p12_open_threads`:
  - the two new top entries (the itv pilot thread and the watchdog pgrep finding);
  - the closed D31 and D33 entries;
  - the RESUMED thread note, which has the 2026-10-08 run outcomes.
- `tasks/lessons.md`, the 2026-10-08 part-2 entries. They cover:
  - induced-fault matching;
  - the env of tmux windows;
  - script-for-Dan relaunches;
  - **never wait on a `pgrep -f "maestro.cli run"` loop**.

## Done on 2026-10-08, session 2 (11:30–12:15 IDT)

- **D33 is CLOSED.** The genuine `/ask` exposed a delivery bug: `hitl.ask` runs in a tmux window that has no Telegram env, so the reply never went out. The fix is maestro `4b97273` (`ask` loads `<repo>/.env`, with a regression test). The second `/ask` was delivered. One gap is carried over, not fixed: the ask window has no PYTHONPATH, so it runs the live maestro checkout, not the pin.
- **D31 is CLOSED, with live automatic recovery observed.** It used a labelled induced limit (option b). Timeline:
  - the pool paused at 08:57:40Z;
  - it recovered at 08:58:16Z from a fresh `claude_oauth_usage_api` sample;
  - the fallback ran on codex gpt-6-luna;
  - the run was accepted.

  The scripts are in `handoffs/2026-10-08-p12-closeout/d31-induced/`. The wrapper is disarmed, and the DuetFlow watchdog is back on its normal launch (controller PID 2027072, normal PATH).
- **DuetFlow runs:** `10-followups-sweep` and the auto-queued `10-followups-sweep-followups` were both accepted, with 2 acceptance_decisions rows each. Details are in the RESUMED thread.
- **STATE format fixes:** the three unstructured entries are fixed, and `tests/test_next_graph_prompt.py` passes.
- **Full suite after `4b97273`:** everything passed except that STATE-format test, which was fixed afterwards. The suite has not been re-run since; the STATE fix touched only the gitignored STATE.

## State at handoff

- Maestro branch `feat/graph-engineering-foundation`, HEAD `4b97273` or later. A parallel modelctl session commits its own files on the same branch; commit only your own files, never `git add -A`.
- STATE and lessons are gitignored, with their changes on disk.
- DuetFlow is idle:
  - the queue holds `01-auth` (needs-dan) and `09-observe-tune` (blocked);
  - HALT is absent;
  - the pin is 0c7eb92.
- DuetFlow's main checkout has re-rendered `docs/dependency_map.md`, `UPCOMING.md` and the `.png`. Leave them.
- No monitors, background shells or agents are left running.

## In scope

- Checking the grill precondition.
- itv onboarding through `maestro-setup` with the safeguards.
- Supervising the itv pilot.
- Folding itv evidence into B01/B18.
- Re-running the suite and `scripts/graph_qualification.py`, regenerating the report with the pilot data.
- Updating PROGRESS/EXECUTION.
- Giving every `_p12_open_threads` entry a disposition. The audit counts 25 FIXED-BUT-NOT-CLOSED.
- The verdict artifact.
- Asking Dan about the post-P12 queue.

## Out of scope

- The Context Gate.
- Building the post-P12 features.
- Pushing or merging the feature branch.
- `~/.maestro/current`.
- modelctl-line files.
- Editing itv's live services, queues or media.
- Fixing F1 (`worker.py` writable=True for reviewers) or the watchdog pgrep pattern without asking Dan whether they belong in P12. **Ask him once, bundled with any other material decision.**
- A release tag. Ask Dan first, because it is outward-facing.

## Verification to report back

- The grill precondition: the CONTEXT.md and ADR paths, or "missing → stopped".
- The itv `project.yaml` safeguards, shown as a diff.
- The itv suite baseline: the spec says 235 passed in 1.44 s. Report the current numbers.
- For each pilot task:
  - the run id;
  - node models;
  - quality failures;
  - accepted or not;
  - acceptance_decisions rows (expect 2).
- Confirmation that no live service was touched: the `itv-bot` and `itv-worker` uptime is unchanged.
- B01 and B18, closed or not, with evidence.
- The number of threads still undisposed (target 0).
- Full suite counts and the `graph_qualification.py` exit code.
- The verdict artifact path and the `send-to-me` result.

## Suggested skills

- `maestro-setup`
- `monitor-long-running-tasks`
- `verification-before-completion`
- `visual-explainer`, for the verdict
- `close-session`
