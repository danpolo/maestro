#!/usr/bin/env python3
"""B3 watchdog — liveness supervisor for the autonomous orchestrator.

Designed to run as a forking systemd user service (ExecStart wraps a tmux session).
Polls every POLL_INTERVAL seconds.

Responsibilities:
  - Respect HALT sentinel (.orchestrator/HALT) — stop immediately.
  - Respect paused_until in state.json — idle and ensure resume job is registered.
  - Stall detection: no new journal.ndjson entry for STALL_WINDOW_MIN → kill + relaunch;
    after MAX_STALL_RESTARTS repeated stalls → set HALT + Telegram alert.
  - Ensure exactly one orchestrator alive when not paused/halted; relaunch on crash.
  - Reap dead implementer tmux windows listed in state.json.in_flight.

STALL_WINDOW_MIN = 20:  implementers take ~15 min; 20 min gives one full poll cycle headroom.

Extracted from the reference project's standalone `scripts/watchdog.py` (322 lines).
Function bodies are copied verbatim; only the import block, the derivation of the
module-level path globals and the five project-specific constants that plan
`docs/plans/2026-08-17-m4b-watchdog.md` §5 names are changed:

1. `REPO` and every `.orchestrator/*` path come from `maestro.paths.Paths.from_env()`
   instead of `Path(__file__).resolve().parent.parent`, exactly as `maestro/prep_actions.py`
   does. The module still exposes the same module-level `Path` globals the reference code
   (and the characterisation harness's reflection-based sandbox) expects.
2. `notify()` delivers through `maestro.hitl.telegram.notify_telegram` instead of shelling
   out to `REPO/scripts/notify_telegram.sh`; the `NOTIFY_SCRIPT` global is therefore gone.
   The alert `print()` and the notifier's `subprocess.run(..., timeout=15)` shape (including
   its not-exception-safe behaviour, bug #154) are unchanged.
3. `launch_orchestrator()` and `ensure_resume_job()` spawn `maestro run` instead of
   `{VENV_PYTHON} scripts/launch_orchestrator.py`, so the `LAUNCHER` and `VENV_PYTHON`
   globals are gone too. `MAESTRO_REPO` is set explicitly in the spawned command because
   `tmux new-window` inherits the *tmux server's* environment, not this process's. The
   unquoted `shell=True` interpolation is deliberately kept (bug #158).
   `orchestrator_alive()`/the stall `pkill` match `ORCHESTRATOR_PATTERN` — the maestro
   entrypoints — rather than the reference's two script names.
4. `TMUX_WINDOW` is unchanged (`"orchestrator"`) and `TMUX_SESSION` is imported from
   `maestro.worktree` rather than re-spelled here. Neither names any project: the reference
   watchdog's own tmux constants are `"agents"`/`"orchestrator"`, and `"agents"` is the
   *shared* session `maestro.worktree`/`maestro.implementer` launch implementer windows into
   — deriving it from `project.yaml` instead would mean `reap_dead_implementers()` listed a
   session no implementer ever runs in. The project name the plan asks for is bound to
   `PROJECT_NAME` and used where the reference actually hardcodes one: the stall-HALT alert
   (bug #176). `{project_name}-watchdog`, `cli.py`'s convention, is the name of the session
   this process *runs in*, which `launch.sh` owns — not a name this module ever spells.
5. `STALL_WINDOW_MIN` (20) and `MAX_STALL_RESTARTS` (3) keep the reference's values as
   defaults and can be overridden under `project.yaml`'s `thresholds:`.

Behavioural surprises are catalogued in `docs/FOUND_BUGS.md` (#151–#179) and pinned by
`tests/characterization/test_watchdog.py`; none of them is fixed here.
"""
from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from maestro.config import load_project_yaml
from maestro.hitl.telegram import notify_telegram
from maestro.paths import Paths
from maestro.worktree import TMUX_SESSION

_PATHS = Paths.from_env()
_PROJECT_YAML = load_project_yaml()
_THRESHOLDS = (_PROJECT_YAML.get("thresholds") or {})

REPO          = _PATHS.repo
STATE_JSON    = _PATHS.state
USAGE_JSON    = _PATHS.usage
JOURNAL       = _PATHS.journal
HALT_FILE     = REPO / ".orchestrator" / "HALT"
QUESTIONS     = REPO / ".orchestrator" / "questions"
TMUX_WINDOW   = "orchestrator"

#: The name that tags an operator-facing alert. `project.name` from `project.yaml`, with
#: the repository directory name as the fallback — the same derivation `cli.py`'s `doctor`
#: uses. The reference hardcodes its own project's name in the one alert below.
PROJECT_NAME  = (_PROJECT_YAML.get("project") or {}).get("name") or REPO.name

#: `pgrep -f` / `pkill -f` pattern for "the orchestrator loop is running". The reference
#: matches its two entrypoint scripts (`launch_orchestrator\.py|orchestrator_run\.py`);
#: maestro's equivalent entrypoints are the `maestro run` console script and the module it
#: calls. Deliberately does not match `maestro watchdog`, i.e. this process itself.
ORCHESTRATOR_PATTERN = r"maestro run|maestro\.orchestrator"

POLL_INTERVAL     = 30   # seconds between watchdog ticks
STALL_WINDOW_MIN  = _THRESHOLDS.get("stall_window_min", 20)    # minutes of journal silence before a stall restart
MAX_STALL_RESTARTS = _THRESHOLDS.get("max_stall_restarts", 3)  # stall restarts before HALT


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_epoch() -> float:
    return datetime.now(tz=timezone.utc).timestamp()


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def append_journal(event: str, detail: str) -> None:
    record = {"ts": now_iso(), "event": event, "agent": "B3-watchdog", "detail": detail}
    try:
        with open(JOURNAL, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def notify(msg: str) -> None:
    print(f"[watchdog] ALERT: {msg}")
    notify_telegram(msg)


def orchestrator_alive() -> bool:
    r = subprocess.run(
        ["pgrep", "-f", ORCHESTRATOR_PATTERN],
        capture_output=True,
    )
    return r.returncode == 0


def tmux_window_exists(window: str) -> bool:
    r = subprocess.run(
        ["tmux", "list-windows", "-t", TMUX_SESSION, "-F", "#{window_name}"],
        capture_output=True, text=True,
    )
    return window in r.stdout.splitlines()


def launch_orchestrator() -> None:
    """Spawn `maestro run` in a dedicated tmux window."""
    cmd = (
        f"tmux new-window -t {TMUX_SESSION} -n {TMUX_WINDOW} "
        f"'cd {REPO} && MAESTRO_REPO={REPO} maestro run'"
    )
    subprocess.run(cmd, shell=True)
    append_journal("orchestrator_relaunched", f"window={TMUX_WINDOW}")
    print(f"[watchdog] Launched orchestrator in {TMUX_SESSION}:{TMUX_WINDOW}")


def ensure_resume_job(resets_at: float) -> None:
    """Register resume cron entry if not already present (mirrors launch_orchestrator logic)."""
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    existing = r.stdout if r.returncode == 0 else ""
    if "cron-orch-resume" in existing:
        return
    resume_cmd = (
        f"/usr/bin/tmux new-window -t {TMUX_SESSION} -n 'cron-orch-resume' "
        f"'cd {REPO} && MAESTRO_REPO={REPO} maestro run'"
    )
    dt_local  = datetime.fromtimestamp(resets_at)
    cron_line = f"{dt_local.minute} {dt_local.hour} {dt_local.day} {dt_local.month} * {resume_cmd}\n"
    subprocess.run(["crontab", "-"], input=existing.rstrip("\n") + "\n" + cron_line,
                   text=True, capture_output=True)
    print(f"[watchdog] Resume job scheduled: {dt_local.strftime('%H:%M %d/%m')} local")
    append_journal("resume_scheduled_watchdog", f"cron {dt_local.isoformat()}")


def _parse_epoch(value) -> float | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        pass
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def journal_last_line() -> str | None:
    """Return the raw last line of the journal, or None if empty/missing.

    Used for clock-jump-immune stall detection: we detect that the orchestrator
    is alive by the journal *content* advancing, measured against the monotonic
    clock — never against wall-clock timestamps, which a system clock correction
    can jump by hours and make a healthy loop look stalled.
    """
    if not JOURNAL.exists():
        return None
    try:
        text = JOURNAL.read_text(encoding="utf-8").strip()
        if not text:
            return None
        return text.splitlines()[-1]
    except Exception:
        return None


def reap_dead_implementers() -> None:
    """Kill any tmux windows for in-flight tasks that have no live pane."""
    state     = read_json(STATE_JSON)
    in_flight = state.get("in_flight") or []
    for entry in in_flight:
        window = entry.get("window")
        if not window or not tmux_window_exists(window):
            continue
        r = subprocess.run(
            ["tmux", "list-panes", "-t", f"{TMUX_SESSION}:{window}", "-F", "#{pane_dead}"],
            capture_output=True, text=True,
        )
        if r.returncode != 0 or "1" in r.stdout.splitlines():
            subprocess.run(
                ["tmux", "kill-window", "-t", f"{TMUX_SESSION}:{window}"],
                capture_output=True,
            )
            append_journal("implementer_reaped", f"dead window {window}")
            print(f"[watchdog] Reaped dead implementer window: {window}")


def main() -> int:
    print(f"[watchdog] Starting — poll={POLL_INTERVAL}s stall={STALL_WINDOW_MIN}min max_restarts={MAX_STALL_RESTARTS}")
    append_journal("watchdog_start", f"poll={POLL_INTERVAL}s stall={STALL_WINDOW_MIN}min")

    stall_restarts   = 0
    last_launch_mono = 0.0   # time.monotonic() of last orchestrator launch by this watchdog
    halted_logged    = False  # one-shot journal marker for soft-halt idling
    # Clock-jump-immune stall tracking: we measure journal silence with the
    # monotonic clock and observed journal advancement, never wall-clock
    # timestamps. A one-time system clock correction (e.g. NTP stepping forward
    # several hours) must not be mistaken for a stalled loop.
    last_journal_seen   = journal_last_line()
    last_journal_change = time.monotonic()

    while True:
        try:
            # 1. HALT
            if HALT_FILE.exists():
                print("[watchdog] HALT sentinel present — stopping.")
                append_journal("watchdog_halt_respected", "HALT file present")
                return 0

            state = read_json(STATE_JSON)

            # Track journal advancement on the monotonic clock. Reset the silence
            # timer whenever the journal's last line changes — this is what makes
            # stall detection immune to wall-clock jumps.
            cur_journal = journal_last_line()
            if cur_journal != last_journal_seen:
                last_journal_seen   = cur_journal
                last_journal_change = time.monotonic()

            # 1b. Soft halt (orchestrator_ctl halt → state.halted) — idle without
            # relaunching. Relaunching here just makes the launcher exit with
            # launch_skipped_halted every poll, spamming the journal and tmux.
            if state.get("halted"):
                if not halted_logged:
                    append_journal("watchdog_idle_halted",
                                   "state.halted=True — suppressing relaunch until resume")
                    print("[watchdog] state.halted — idling (no relaunch).")
                    halted_logged = True
                time.sleep(POLL_INTERVAL)
                continue
            halted_logged = False

            # 2. Paused
            paused_epoch = _parse_epoch(state.get("paused_until"))
            if paused_epoch and now_epoch() < paused_epoch:
                usage     = read_json(USAGE_JSON)
                resets_at = (usage.get("five_hour") or {}).get("resets_at")
                if resets_at:
                    ensure_resume_job(float(resets_at))
                time.sleep(POLL_INTERVAL)
                continue
            elif paused_epoch and now_epoch() >= paused_epoch:
                state["paused_until"] = None
                state["updated_at"]   = now_iso()
                tmp = STATE_JSON.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(state, indent=2) + "\n")
                tmp.rename(STATE_JSON)
                print("[watchdog] Pause expired — cleared.")

            # 2b. Blocked-on: orchestrator parked waiting for Dan-request answers.
            blocked_on = state.get("blocked_on") or []
            if blocked_on:
                all_answered = all(
                    (QUESTIONS / f"{req_id}.answer").exists() for req_id in blocked_on
                )
                if all_answered:
                    state["blocked_on"] = []
                    state["updated_at"] = now_iso()
                    tmp = STATE_JSON.with_suffix(".json.tmp")
                    tmp.write_text(json.dumps(state, indent=2) + "\n")
                    tmp.rename(STATE_JSON)
                    append_journal("blocked_on_cleared", f"answered={blocked_on}")
                    print(f"[watchdog] All blocked_on answers received — clearing; will relaunch.")
                    # Fall through: liveness path below will relaunch the orchestrator.
                else:
                    # Still waiting for Dan's answers — do not relaunch yet.
                    time.sleep(POLL_INTERVAL)
                    continue

            # 2c. In-flight guard: if an implementer is running AND the orchestrator is
            # already alive, nothing to do — skip liveness relaunch. If the orchestrator
            # is dead while in_flight, fall through so liveness check below relaunches it;
            # orchestrator_run.py handles in_flight gracefully via reconcile_in_flight.
            if state.get("in_flight") and orchestrator_alive():
                reap_dead_implementers()
                time.sleep(POLL_INTERVAL)
                continue

            # 2d. HITL-parked guard: when the orchestrator is parked waiting for Dan's
            # /approve or /reject, it loops silently (no journal writes) by design. That
            # silence is NOT a stall — keep the stall timer fresh so we never kill a
            # parked loop, nor fire the instant the park clears (before the merge events
            # land in the journal). Liveness relaunch below still applies if it died.
            if state.get("waiting_on_dan"):
                last_journal_change = time.monotonic()

            alive = orchestrator_alive()

            # 3. Stall detection — monotonic-based so a wall-clock jump can't
            # manufacture a fake stall. `stall_secs` is real elapsed time since
            # the journal last advanced; `time_since_launch` guards a just-launched
            # loop from being killed before it can write its first entry.
            stall_secs        = time.monotonic() - last_journal_change
            time_since_launch = time.monotonic() - last_launch_mono
            if (alive
                    and stall_secs > STALL_WINDOW_MIN * 60
                    and time_since_launch > STALL_WINDOW_MIN * 60):
                stall_min = stall_secs / 60
                stall_restarts += 1
                if stall_restarts >= MAX_STALL_RESTARTS:
                    msg = (
                        f"[{PROJECT_NAME}] Orchestrator stalled {MAX_STALL_RESTARTS}x "
                        f"({stall_min:.0f} min silence). Setting HALT."
                    )
                    append_journal("watchdog_halt_stall",
                                   f"stall_restarts={stall_restarts} silence={stall_min:.0f}min")
                    HALT_FILE.touch()
                    notify(msg)
                    return 1
                print(f"[watchdog] Stall #{stall_restarts} ({stall_min:.0f} min) — killing orchestrator.")
                append_journal("stall_restart", f"restart={stall_restarts} silence={stall_min:.0f}min")
                subprocess.run(
                    ["pkill", "-f", ORCHESTRATOR_PATTERN],
                    capture_output=True,
                )
                time.sleep(2)
                alive = False  # force relaunch path below
                last_journal_change = time.monotonic()  # fresh window for the relaunched loop

            # 4. Liveness — relaunch if not running
            if not alive:
                last_launch_mono = time.monotonic()
                launch_orchestrator()

            # 5. Reap dead implementer windows
            reap_dead_implementers()

        except Exception as e:
            print(f"[watchdog] poll error: {e}")
            append_journal("watchdog_error", str(e))

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    raise SystemExit(main())
