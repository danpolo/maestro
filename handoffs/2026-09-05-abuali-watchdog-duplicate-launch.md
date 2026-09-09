# Handoff — AbuAliArchive watchdog duplicate-TMUX-launch failure

## Goal

Fix Maestro's watchdog liveness/launch path so an AbuAliArchive watchdog owns exactly one
`maestro run` child, does not accumulate `agents:orchestrator` TMUX windows, and can be
restarted safely after a full poll.

## Read first

- `maestro/watchdog.py` — liveness matcher, TMUX helpers, launch path, and main loop.
- `tests/characterization/test_watchdog.py` — existing characterization harness; liveness
  tests begin around line 505 and launch tests around line 577.
- `maestro/cli.py:1222` — `maestro watchdog` entrypoint.
- `/home/dan/projects/AbuAliArchive/launch.sh` — production launch topology.
- `/home/dan/projects/AbuAliArchive/systemd/abuali-watchdog.service` — service definition.
- `/home/dan/projects/AbuAliArchive/.orchestrator/journal.ndjson` — production incident record.

## Incident facts (verified)

1. A stale AbuAliArchive systemd drop-in overrode `ExecStop` with lowercase
   `abuali-watchdog`, while `launch.sh` creates `AbuAliArchive-watchdog`. On stop, systemd
   missed the live TMUX session, waited 90 seconds, then SIGKILLed the TMUX server. The stale
   drop-in was removed; the installed base unit now has the correct `ExecStop` target.
2. After that repair, the watchdog launched a new `maestro run` child every 30 seconds. This
   created 38 `agents:orchestrator` windows and concurrent CPU work, the likely source of the
   broader TMUX degradation.
3. The live child argv is:
   `/home/dan/projects/AbuAliArchive/.venv/bin/python3 -m maestro.cli run`.
   The original `ORCHESTRATOR_PATTERN` did not cover that form. The working tree currently
   contains an uncommitted attempted extension plus a TMUX-window launch guard in
   `maestro/watchdog.py`; neither prevented the controlled duplicate launch at runtime.
4. Controlled 33–36 second `maestro watchdog` runs, with the production systemd service
   stopped, consistently launched one child initially and a second child at the next poll.
   The journal recorded `orchestrator_relaunched` at both timestamps. The test harness then
   terminated the children.
5. Direct interactive checks in the same AbuAliArchive venv returned `True` for
   `maestro.watchdog.orchestrator_alive()` when matching child processes existed, and direct
   `tmux_window_exists('orchestrator')` returned `True` when the window existed. The watchdog
   runtime nevertheless launched a duplicate. Determine why before keeping any source patch.

## Current safe state

- `abuali-watchdog.service` is **inactive**.
- No process matching the AbuAliArchive `maestro.cli run` argv remains.
- No `AbuAliArchive-watchdog` TMUX session remains.
- The shared `agents` session remains available; do not kill the entire session.
- The Maestro worktree is dirty: `maestro/watchdog.py` contains diagnostic attempted changes.
  Review, replace with a tested fix, or revert them deliberately. Do not treat them as a solution.

## In scope

- Root-cause the divergence between the watchdog's runtime liveness/launch behaviour and the
  direct `pgrep` / TMUX helper checks.
- Make duplicate launch structurally impossible.
- Add regression tests in the existing watchdog characterization suite.
- Verify the installed AbuAliArchive venv executes the patched Maestro code.
- Safely restart and observe production watchdog behaviour.

## Out of scope

- Archive retrieval, database, or Telegram bot changes.
- Killing unrelated TMUX sessions (`workspace`, `claude-bridge`, `hive-chef`) or recreating the
  shared `agents` session.
- Broad systemd/resource-limit redesign beyond the watchdog's service and its exact TMUX
  ownership contract.

## Suggested investigation

1. Start with a new test that simulates: liveness says false, but the canonical
   `agents:orchestrator` window is live. Assert `launch_orchestrator()` does not invoke
   `tmux new-window` or write `orchestrator_relaunched`.
2. Add a bounded integration harness that starts `maestro watchdog` for more than one
   `POLL_INTERVAL` using a disposable TMUX socket/session and asserts exactly **one** child
   and one canonical orchestrator window. Do not use production `agents` for this test.
3. Trace the actual subprocess/environment used by `cmd_watchdog`; establish why the live
   watchdog did not respect direct helper results. Treat the source/runtime mismatch as a
   finding to prove, not an assumption.
4. Only after tests are green, update the deployed package/venv in the correct way for this
   editable/install layout, then use the production launch path.

## Verification and restart gate

Report all of the following before declaring fixed:

1. Targeted watchdog tests pass, including the new false-liveness/live-window regression test.
2. A bounded test lasting at least **35 seconds** (one 30-second poll) reports exactly:
   - **1** live `maestro.cli run` child;
   - **1** canonical orchestrator window;
   - **0** duplicate-launch journal events after the initial launch.
3. Production preflight: service inactive and **0** AbuAliArchive Maestro run children.
4. After restart and at least **35 seconds**: `abuali-watchdog.service` is `active`,
   `NRestarts=0`, `ExecStop` targets `AbuAliArchive-watchdog`, the named watchdog TMUX session
   exists, and the run-child count is **1**.
5. Verify a normal stop completes without the former 90-second timeout or SIGKILL.

## Suggested skills

- `diagnose` — preserve a controlled reproduction and trace the actual runtime path.
- `superpowers:test-driven-development` — write the watchdog regression before retaining a fix.
- `superpowers:systematic-debugging` — keep the investigation evidence-led.
- `superpowers:verification-before-completion` — require fresh production evidence before a
  restart is called safe.
