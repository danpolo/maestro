# Maestro build watchdog — design

Status: approved for planning
Date: 2026-08-14
Author: Claude (with danpolo)

## Problem

`scripts/run_overnight.sh` (driven by `maestro-build.service`) is a chain of fresh
`claude -p` sessions that hands off through `docs/PROGRESS.md`. On 2026-08-12 19:17
IDT a chain session fired an async multi-agent `Workflow` tool call, signed off with
"I'll report when it lands" without waiting for it, and was force-terminated by the
harness's ~600s background-task grace period. That session never returned to update
`docs/PROGRESS.md` or commit its work, which (most likely) tripped the driver's own
2-consecutive-stale-session guard and stopped the chain — a *correct*, intentional
stop given the guard's job.

The actual bug is structural, not in the guard: `maestro-build-launcher.sh`'s wait
loop always exits `0` when the tmux session ends, whether that's the stale guard
stopping on purpose or a genuine crash. `Restart=on-failure` can therefore never fire,
and nothing else watches the driver. The chain sat dead for ~22 hours with
`PROGRAMME-STATUS: IN-PROGRESS` and real uncommitted M2 work in the working tree,
discovered only because the operator happened to check.

Fixed already, out of scope here: `scripts/run_overnight.sh`'s `PROMPT` now tells
chain sessions not to end their turn on unresolved background work (see that file's
inline comment, commit pending in the working tree as of this design).

## Goal

Detect a stalled/dead driver automatically, attempt an unattended fix the same way an
operator would (diagnose root cause → apply minimal fix → verify green → recover),
and report the outcome over Telegram — including a resumable session handle for
anything the fixer couldn't finish, and honest reporting (not silent retry) when the
blocker is a Claude usage limit.

## Non-goals

- **Not a generic multi-service watchdog.** Scoped to `maestro-build.service` /
  `run_overnight.sh` only. AbuAliArchive's own `scripts/watchdog.py` and
  `maestro_selffix.py` are separate, pre-existing systems for that project and are
  not touched by this design.
- **Does not act on `PROGRAMME-STATUS: ABORTED`.** An abort is a deliberate safety
  stop per `docs/EXECUTION.md` ("STOP the programme and report it — not repair it,
  not reason past it"). The watchdog reports an ABORTED status once, the same way it
  reports COMPLETE, but never attempts to fix or resume it.
- **Does not detect "driver active but silently wedged."** A running session that
  makes zero progress for hours is legitimately ambiguous with a long usage-limit
  sleep (which the driver already handles by design, sometimes for hours). Only "the
  driver process is simply not running while the programme claims IN-PROGRESS" is
  unambiguous enough to automate. Flagged here as a known gap, not solved.
- **No push, no PR flow.** The fixer commits locally only, same as every chain
  session to date.

## Architecture

A new, independent systemd timer — not embedded in `run_overnight.sh` or the
launcher — polls every 20 minutes. A separate timer (rather than a check inside the
launcher) is required to catch the case that actually happened: the driver process
not running at all, which a launcher-embedded check can never observe.

```
maestro-watchdog.timer (every 20 min)
  -> maestro-watchdog.service (oneshot)
       -> scripts/watchdog_selffix.sh
            reads: docs/PROGRESS.md, systemctl state, .run/watchdog_state.json
            IN-PROGRESS + service inactive, not in cooldown:
              -> spawns fixer: claude -p --model claude-sonnet-5
                   --dangerously-skip-permissions --session-id <uuid>
              -> classifies fixer outcome (fixed / limit-hit / crashed / unclear)
              -> scripts/notify_telegram_bridge.sh with the report
            ABORTED or COMPLETE, not yet reported:
              -> one-time notice via notify_telegram_bridge.sh
            otherwise: exit quietly, no state change
```

### New files

| File | Purpose |
|---|---|
| `scripts/watchdog_selffix.sh` | Detection + dispatch; entry point for the timer |
| `scripts/notify_telegram_bridge.sh` | Sends a message via the existing Claude Code Telegram bot |
| `scripts/lib_maestro_ops.sh` | Shared helpers: `status()`, `hash_p()`, `seconds_until_reset()` — extracted out of `run_overnight.sh` and sourced by both it and the watchdog, so the two never drift |
| `systemd/maestro-watchdog.timer` | `OnUnitActiveSec=20min`, tracked in git like `maestro-build.service` |
| `systemd/maestro-watchdog.service` | `Type=oneshot`, `ExecStart=scripts/watchdog_selffix.sh` |
| `.run/watchdog_state.json` | `{last_incident_fingerprint, last_notified_at, retry_not_before, reported_terminal_status}` |

`run_overnight.sh` is refactored (not rewritten) to source `lib_maestro_ops.sh`
instead of defining `status()`/`hash_p()`/`seconds_until_reset()` inline — pure
extraction, same behavior.

## Detection logic

On each tick, `watchdog_selffix.sh`:

1. Read `PROGRAMME-STATUS` via the shared `status()` helper.
2. **`COMPLETE` or `ABORTED`**: if `watchdog_state.json.reported_terminal_status`
   doesn't already match this status, send a one-time Telegram notice and record it.
   Otherwise no-op. Never fires the fixer for these.
3. **`IN-PROGRESS`**:
   - If `systemctl is-active maestro-build.service` reports active → healthy, no-op
     (this is the deliberate non-goal above: an active-but-slow driver is left alone).
   - If inactive/failed → a stall. Check `watchdog_state.json.retry_not_before`; if
     set and still in the future, no-op (rate-limit cooldown from a prior tick).
     Otherwise proceed to the fixer.

A lock file (`.run/watchdog.lock`, pid + timestamp, checked for a live pid before
trusting it stale) prevents two ticks from overlapping if a fixer run takes longer
than 20 minutes.

## Fixer session

```
claude -p --model claude-sonnet-5 --dangerously-skip-permissions \
  --session-id <fresh-uuid> "<brief>" > .run/watchdog-fix-<timestamp>.log 2>&1
```
capped with `timeout 1800` (30 min, matching AbuAliArchive's `maestro_selffix.py`
precedent).

**Trust level: same as a normal chain session.** It inherits the operator's global
`~/.claude/CLAUDE.md`, so the standing privileged-commands rule already applies
without any extra plumbing — it can only run the two `NOPASSWD` commands granted
below (restart/status of `maestro-build.service`), and falls back to "write a
`/tmp` script, tell the operator the exact command" for anything else privileged,
exactly like an interactive session would. It may commit locally (never push).

**Brief** (final form written during implementation, but must include):
- Systematic-debugging framing: read `loop_status.sh`, `journalctl -u
  maestro-build.service`, `.run/session-*.log`, `git log`, `docs/PROGRESS.md` before
  proposing a fix; find root cause, not the first plausible guess.
- Explicit instruction not to spawn unresolved async background work itself (the
  same fix just applied to `run_overnight.sh`'s own prompt — the fixer is just as
  headless as a chain session and subject to the same 600s grace-period trap).
- Verify the test suite is green before committing anything.
- If the fix is "restart the driver," do so (passwordless now) and confirm via
  `loop_status.sh`-equivalent checks that it's actually iterating before reporting
  success — don't just assume the restart worked.
- Must end its final message in this exact structure, so the watchdog can forward it
  without re-summarizing:
  ```
  ## What happened
  ## What I fixed
  ## Open questions
  ## Action needed from you
  ```

## Outcome classification

`watchdog_selffix.sh` inspects the fixer's log after it exits:

- **Usage limit hit** (matches the same broadened regex `run_overnight.sh` already
  uses: `usage limit|hit (your|the) .*limit|rate.?limit|resets at|spend limit`) and
  no commit was made → call `seconds_until_reset()`, notify once with the real reset
  time, set `retry_not_before` in state. No further attempts until then.
- **Timed out** (the `timeout` wrapper killed it) → notify with the log path and the
  `--resume <uuid>` hint; the session may be resumable even though the wrapping
  process was killed.
- **Crashed / empty output** → notify with the raw error and log path. Never fails
  silently.
- **Normal completion** → forward the fixer's structured report as-is.

In every case except the cooldown no-op, state records `last_notified_at` and clears
`retry_not_before` on next success.

## Reporting format

Sent via `notify_telegram_bridge.sh`, which reads `TELEGRAM_BOT_TOKEN` from
`~/.claude/channels/telegram/.env` and the chat id from
`~/.claude/channels/telegram/access.json`'s `allowFrom[0]` — the same bot/chat the
operator already uses for Claude Code, independent of the live bridge process (which
is guarded by `patch-telegram-plugin.sh` against any non-bridge session using the
plugin channel directly, so a direct Bot-API push is the only viable route). Skips
silently (exit 0, stderr warning) if the config is missing, matching
`AbuAliArchive/scripts/notify_telegram.sh`'s existing safe-skip behavior.

```
🔧 maestro-build watchdog — driver was down, PROGRAMME-STATUS still IN-PROGRESS
Fixer session: <uuid>

## What happened
...
## What I fixed
...
## Open questions
...
## Action needed from you
...

Resume if needed: claude --resume <uuid>   (cwd: /home/dan/projects/maestro)
Full log: .run/watchdog-fix-<timestamp>.log
```

Truncated with a "full log at <path>" note if it would exceed Telegram's 4096-char
message limit.

## Deployment

One `/tmp/setup-maestro-watchdog.sh`, per the standing privileged-commands rule,
containing every privileged step:

1. Write `/etc/sudoers.d/maestro-watchdog`:
   ```
   dan ALL=(root) NOPASSWD: /bin/systemctl restart maestro-build.service
   dan ALL=(root) NOPASSWD: /bin/systemctl status maestro-build.service
   ```
   validated with `visudo -cf` before install, `chmod 440` after — mirroring
   `services/claude-bridge/setup-sudoers.sh`. Granted to `dan` generally (sudoers is
   per-user, not per-session) — the fixer, running as the same user, gets the same
   passwordless restart/status, which is what lets it close the loop without paging
   the operator for the common case.
2. Copy `systemd/maestro-watchdog.{timer,service}` into `/etc/systemd/system/`.
3. `systemctl daemon-reload && systemctl enable --now maestro-watchdog.timer`.

The operator runs this once (`sudo bash /tmp/setup-maestro-watchdog.sh`); everything
after that — including future restarts of `maestro-build.service` itself, by the
operator or the fixer — needs no further sudo prompts.

## Testing

- `watchdog_selffix.sh`'s detection logic (state transitions: healthy / stalled /
  cooldown / terminal-status-reported) gets a bash test harness with stubbed
  `docs/PROGRESS.md` fixtures and a stubbed `systemctl` — this is new coverage,
  since `loop_status.sh` itself has none today.
- `notify_telegram_bridge.sh` tested against a mock HTTP endpoint; no real sends in
  CI.
- `lib_maestro_ops.sh`'s extracted helpers get characterization tests pinning
  current behavior before/after the extraction from `run_overnight.sh`.
- The fixer's prompt/brief is not unit-testable; validated by observing its first
  real trigger.

## Open questions carried into implementation

- Exact wording of the fixer brief (drafted above in outline; full text is an
  implementation detail).
- Exact cooldown duration between repeated stall notifications when the fixer's own
  fix doesn't fully resolve things (proposed default: 90 minutes; not load-bearing
  enough to block approval on).
