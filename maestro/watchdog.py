#!/usr/bin/env python3
"""B3 watchdog — liveness supervisor for the autonomous orchestrator.

Designed to run as a forking systemd user service (ExecStart wraps a tmux session).
Polls every POLL_INTERVAL seconds.

Responsibilities:
  - Respect HALT sentinel (.orchestrator/HALT) — stop immediately; with a Telegram bot
    configured, stay up instead to answer /help and /status (and queue every other command)
    for the stopped loop, and supervise again once HALT is removed.
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
   out to the reference's own notifier script; the `NOTIFY_SCRIPT` global is therefore gone.
   The alert `print()` is unchanged; the notifier itself now sends natively via
   `requests.post` (R4), not `subprocess.run(..., timeout=15)` — see
   `maestro/hitl/telegram.py`'s module docstring.
3. `launch_orchestrator()` and `ensure_resume_job()` spawn `maestro run` instead of
   `{VENV_PYTHON} scripts/launch_orchestrator.py`, so the `LAUNCHER` and `VENV_PYTHON`
   globals are gone too. `MAESTRO_REPO` is set explicitly in the spawned command because
   `tmux new-window` inherits the *tmux server's* environment, not this process's. The
   unquoted `shell=True` interpolation is deliberately kept (bug #158).
   `orchestrator_alive()`/the stall `pkill` match `controller_pattern()` — this project's
   loop argv, anchored — rather than the reference's two script names.
4. `TMUX_WINDOW` is `orchestrator-<project>` (the reference's bare `"orchestrator"` let one
   project's window block another's launch) and `TMUX_SESSION` is imported from
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

import fcntl
import json
import re
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from maestro.config import load_project_yaml, threshold
from maestro.control.store import CONTROLLER_RESOURCE, enqueue_external_command, pid_alive
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
CONTROL_DB    = _PATHS.control_db
#: Held (`flock -n`) by the `maestro run` this watchdog launches, for its whole life.
ORCHESTRATOR_LOCK = REPO / ".orchestrator" / "orchestrator.lock"

#: Absolute path to the `maestro` console script inside this project's own venv. A bare
#: `maestro` in the spawned tmux command depends on that project's `.venv/bin` being on
#: PATH inside whatever environment the tmux *server* was started with — not this
#: process's — which is not guaranteed (confirmed empirically during a live M5 cutover: a
#: fresh tmux window in an existing server had no `.venv/bin` on PATH and `which maestro`
#: failed). Every project installs maestro into `<repo>/.venv` (DESIGN.md
#: §10), so the absolute path is always resolvable without depending on inherited PATH —
#: same reasoning as `MAESTRO_REPO` being passed explicitly rather than relied on.
VENV_MAESTRO  = REPO / ".venv" / "bin" / "maestro"

#: The name that tags an operator-facing alert. `project.name` from `project.yaml`, with
#: the repository directory name as the fallback — the same derivation `cli.py`'s `doctor`
#: uses. The reference hardcodes its own project's name in the one alert below.
PROJECT_NAME  = (_PROJECT_YAML.get("project") or {}).get("name") or REPO.name
#: Per project: the window name is the watchdog's "already launched" check, and the
#: `agents` session is shared by every project on the host. A fixed `"orchestrator"` let
#: DuetFlow's controller window block the itv watchdog from ever launching (P12 itv pilot).
TMUX_WINDOW   = f"orchestrator-{PROJECT_NAME}"

def controller_pattern() -> str:
    """`pgrep -f` / `pkill -f` pattern for "THIS project's orchestrator loop is running".

    The reference matched its entrypoint names anywhere in any cmdline. P12 finding
    (2026-10-08): `maestro run|maestro\\.cli run|...` matched an agent's
    `until pgrep -f "maestro.cli run$"` wait loop, so the watchdog read the loop as alive
    and held the relaunch for ~6 min; it also matched every other project's controller,
    and the stall `pkill` would have killed them too. The pattern is now anchored on the
    whole argv of the processes `launch_orchestrator` and the cron resume actually start
    — the `flock` wrapper and the loop itself, run by this project's venv — so a shell,
    an editor or another project's loop cannot match. Read at call time, from the
    module's paths. Never matches `maestro watchdog`, i.e. this process itself.
    """
    venv_bin = re.escape(str(VENV_MAESTRO.parent))
    lock = re.escape(str(ORCHESTRATOR_LOCK))
    maestro = re.escape(str(VENV_MAESTRO))
    loop = rf"{venv_bin}/python[0-9.]*( -m maestro\.cli| {maestro}) run"
    return rf"^(flock -n {lock} {maestro} run|{loop})$"

POLL_INTERVAL     = 30   # seconds between watchdog ticks
# Read through `config.threshold` rather than off `_THRESHOLDS` directly: a `thresholds:`
# block that is null, a scalar or a list makes `.get` raise, and this module reads these
# at import — so a typo in a config file became a package that would not import.
STALL_WINDOW_MIN  = threshold("stall_window_min", 20, cast=int)    # minutes of journal silence before a stall restart
MAX_STALL_RESTARTS = threshold("max_stall_restarts", 3, cast=int)  # stall restarts before HALT


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


def request_state_change(command_type: str, payload: dict, key: str) -> bool:
    """Ask the controller to change state, instead of writing `state.json` here.

    `[INV-01]`: the watchdog is an external actor. Once a project has a control
    database it must reach the state document through the idempotent command inbox and
    never by writing the file, or the two writers race and the projection stops meaning
    anything. `True` means the command was enqueued and the caller must NOT fall back to
    the legacy write; `False` means this project has no control plane yet and the legacy
    write below is still the only path there is.

    The key is derived from what the command is about, not from the clock, so a watchdog
    that re-observes the same condition on its next poll enqueues nothing new.
    """
    return enqueue_external_command(command_type, payload, idempotency_key=key) is not None


def notify(msg: str) -> None:
    print(f"[watchdog] ALERT: {msg}")
    notify_telegram(msg)


def orchestrator_alive() -> bool:
    r = subprocess.run(
        ["pgrep", "-f", controller_pattern()],
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
    # The window name is the normal ownership boundary.  Do this check here (rather
    # than only in main) because callers reach this helper after a false liveness
    # result; tmux would otherwise silently create ``orchestrator-1``.
    if tmux_window_exists(TMUX_WINDOW):
        print(f"[watchdog] Orchestrator window {TMUX_SESSION}:{TMUX_WINDOW} already exists — skipping duplicate launch.")
        return

    # The window check is not atomic with new-window and a watchdog can be running
    # against a different tmux socket after recovery.  The child holds this advisory
    # lock for its entire lifetime, so those failure modes cannot run two loops.
    lock = REPO / ".orchestrator" / "orchestrator.lock"
    cmd = (
        f"tmux new-window -t {TMUX_SESSION} -n {TMUX_WINDOW} "
        f"'cd {REPO} && MAESTRO_REPO={REPO} flock -n {lock} {VENV_MAESTRO} run'"
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
        f"'cd {REPO} && MAESTRO_REPO={REPO} {VENV_MAESTRO} run'"
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


def graph_run_live() -> bool:
    """True when the control DB holds a `task_runs` row with status `running` (D9).

    The graph runner (`engineering.runner: graph`) never fills `state.json`'s `in_flight`
    and journals only when a run opens and settles, so an attempt longer than the stall
    window is journal silence that is not a stall. A running row is its `in_flight`.
    Read-only (`mode=ro`), and anything short of a clean answer — no DB (a legacy
    project), no table, a locked or corrupt file — is False: the watchdog then keeps its
    stall behaviour rather than suppressing it on a guess.
    """
    if not CONTROL_DB.exists():
        return False
    try:
        conn = sqlite3.connect(f"{CONTROL_DB.as_uri()}?mode=ro", uri=True, timeout=1.0)
        try:
            row = conn.execute(
                "SELECT 1 FROM task_runs WHERE status = 'running' LIMIT 1").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return row is not None


def loop_running() -> bool:
    """Whether this project's loop is up: its launch lock is held, or a live process holds
    the controller lease. Project-specific, unlike `orchestrator_alive`'s pgrep, which sees
    every project's loop. Anything short of a clean answer is True (fail closed)."""
    try:
        with open(ORCHESTRATOR_LOCK, "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(lock, fcntl.LOCK_UN)
    except OSError:
        return True
    if not CONTROL_DB.exists():
        return False
    try:
        conn = sqlite3.connect(f"{CONTROL_DB.as_uri()}?mode=ro", uri=True, timeout=1.0)
        try:
            row = conn.execute("SELECT writer_pid FROM writer_leases WHERE resource_id = ?",
                               (CONTROLLER_RESOURCE,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return True
    return row is not None and pid_alive(int(row[0]))


def halted_telegram_enabled() -> bool:
    """A bot is configured and the listener switch is not off (the suite turns it off)."""
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_ALERT_CHAT_ID")
                and os.environ.get("MAESTRO_TELEGRAM_LISTENER", "1").strip() != "0")


def serve_while_halted(still_halted) -> None:
    """Answer Telegram for the loop while `still_halted()` — Dan's `/help` to a halted loop
    went unanswered because nothing read Telegram. Never while the loop is up: only
    one project process may consume its mailbox. Local reads return immediately, so
    wait between passes to keep replies responsive without spinning while halted."""
    from maestro.hitl import commands

    while still_halted():
        try:
            if loop_running():
                time.sleep(POLL_INTERVAL)
                continue
            commands.answer_while_halted()
            time.sleep(0.5)
        except Exception as e:  # noqa: BLE001 — a dead network must not end the watchdog
            print(f"[watchdog] halted Telegram error: {e}")
            time.sleep(commands.LISTENER_RETRY_SEC)


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


def repinned_root(repo: Path = REPO) -> Path | None:
    """The checkout this project is now pinned to, when it is not the one running here.

    The controller restarts onto a new pin after adopting (`orchestrator._self_update_maybe`),
    but nothing restarts the watchdog: it runs in a bare tmux pane. Without this it keeps the
    code it started with indefinitely — a DuetFlow watchdog on a stale build once took another
    project's controller for its own and never relaunched the real one.
    """
    from maestro import bootstrap

    pin = bootstrap.project_pin_file(repo)
    if pin is None or not pin.exists():
        return None  # only a project pin moves on release; the host default is not followed
    adopted = bootstrap.adopted_root(repo=repo)
    return adopted if adopted is not None and adopted != bootstrap.running_root() else None


def reexec_onto(root: Path) -> None:
    """Replace this watchdog with the same command run from the project's pin.

    The bootstrap sentinel is dropped so the child's `maestro` re-resolves the pin; holding
    it would pin the child to this process's checkout, the stale one.
    """
    from maestro import bootstrap

    append_journal("watchdog_reexec", f"pin moved to {root}")
    print(f"[watchdog] Project pin moved to {root} — re-executing on it.", flush=True)
    env = {k: v for k, v in os.environ.items() if k != bootstrap.SENTINEL}
    env[bootstrap.PROJECT_ENV_VAR] = str(REPO)
    os.execve(str(VENV_MAESTRO), [str(VENV_MAESTRO), "watchdog"], env)


def main() -> int:
    print(f"[watchdog] Starting — poll={POLL_INTERVAL}s stall={STALL_WINDOW_MIN}min max_restarts={MAX_STALL_RESTARTS}")
    append_journal("watchdog_start", f"poll={POLL_INTERVAL}s stall={STALL_WINDOW_MIN}min")

    # `[P11]` / `[INV-10]`: a watchdog start is, in production, a *service* start — the
    # systemd unit's `ExecStart` after a reboot, a `systemctl restart`, or a relaunch after
    # the controller died. Every one of those may leave writer leases held by attempts whose
    # process is gone, and a loop relaunched onto a still-held lease is the duplicate-worker
    # failure this phase has to make impossible. So the first thing a starting watchdog does
    # is *ask* for the recovery scan — enqueued, never performed here; see
    # `request_lease_reconciliation` for why the watchdog must not run it itself.
    if request_lease_reconciliation("watchdog_start"):
        print("[watchdog] Requested lease reconciliation for this start.")

    stall_restarts   = 0
    last_launch_mono = 0.0   # time.monotonic() of last orchestrator launch by this watchdog
    halted_logged    = False  # one-shot journal marker for soft-halt idling
    follow_failed    = False  # a failed re-exec onto a moved pin is journalled once, not per poll
    # Clock-jump-immune stall tracking: we measure journal silence with the
    # monotonic clock and observed journal advancement, never wall-clock
    # timestamps. A one-time system clock correction (e.g. NTP stepping forward
    # several hours) must not be mistaken for a stalled loop.
    last_journal_seen   = journal_last_line()
    last_journal_change = time.monotonic()

    while True:
        try:
            # 0. Follow the project pin, so a release reaches the watchdog as well as the loop.
            moved = None if follow_failed else repinned_root()
            if moved is not None:
                follow_failed = True  # reexec_onto returns only if the exec failed
                reexec_onto(moved)

            # 1. HALT
            if HALT_FILE.exists():
                if not halted_telegram_enabled():
                    print("[watchdog] HALT sentinel present — stopping.")
                    append_journal("watchdog_halt_respected", "HALT file present")
                    return 0
                # With a bot, stay up to answer Telegram for the stopped loop; nothing is
                # relaunched while HALT exists. Once it is removed, supervise again — so a
                # `systemctl start` that found this unit still active is not lost.
                print("[watchdog] HALT sentinel present — answering Telegram until it is removed.")
                append_journal("watchdog_halt_respected", "HALT file present")
                serve_while_halted(HALT_FILE.exists)
                append_journal("watchdog_halt_cleared", "HALT removed — supervising again")
                print("[watchdog] HALT removed — supervising again.")
                last_journal_seen   = journal_last_line()
                last_journal_change = time.monotonic()
                continue

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
                if halted_telegram_enabled():
                    serve_while_halted(lambda: bool(read_json(STATE_JSON).get("halted"))
                                       and not HALT_FILE.exists())
                    continue
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
                if not request_state_change(
                    "clear_pause", {"paused_until": state.get("paused_until")},
                    f"clear_pause:{state.get('paused_until')}",
                ):
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
                    if not request_state_change(
                        "clear_blocked_on", {"blocked_on": list(blocked_on)},
                        f"clear_blocked_on:{','.join(sorted(blocked_on))}",
                    ):
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

            # 2c'. The graph runner's in-flight guard (D9): its runs live in the control
            # DB, not `in_flight`, and it journals only at run open/settle — so a long
            # attempt would otherwise be stall-killed mid-run. A live run with the
            # orchestrator alive skips the stall check, and the silence timer is kept
            # fresh so the run settling does not trip a window that ran during it.
            if graph_run_live() and orchestrator_alive():
                last_journal_change = time.monotonic()
                reap_dead_implementers()
                time.sleep(POLL_INTERVAL)
                continue

            # 2d. HITL-parked guard: when the orchestrator is parked waiting for Dan's
            # /approve or /reject, it loops silently (no journal writes) by design. That
            # silence is NOT a stall — keep the stall timer fresh so we never kill a
            # parked loop, nor fire the instant the park clears (before the merge events
            # land in the journal). Liveness relaunch below still applies if it died.
            # The graph runner's `kind: dan` wait (`maestro.hitl.verify`) is the same kind
            # of silence: a run holding a task before merge until Dan confirms its checks.
            # So is a run paused on an agent's question to Dan (D26).
            if (state.get("waiting_on_dan") or state.get("awaiting_dan_verification")
                    or state.get("awaiting_dan_questions")):
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
                    ["pkill", "-f", controller_pattern()],
                    capture_output=True,
                )
                time.sleep(2)
                # The loop we just killed had no chance to release anything it held. Ask for
                # the scan before the relaunch below, for the same reason the start does.
                request_lease_reconciliation(f"stall_restart:{stall_restarts}")
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


# ── `[P11]` / `[INV-10]`: the service definition this module is designed to run under ──


#: The tmux session a generated unit owns. `cli.py` spells the same convention when it
#: renders `systemd/<project>-watchdog.service`; it is bound here because *this* is the
#: process that runs inside it, and because `service_unit_facts` has to answer questions
#: about a unit without importing the CLI.
SERVICE_SESSION_SUFFIX = "-watchdog"

#: The three directives `[INV-10]` fixes, and why each one is not a style choice:
#:
#: `Type=forking` — `ExecStart` is `tmux new-session -d`, which returns immediately after
#: the server forks. Under `Type=simple` systemd would treat that return as the service
#: exiting and (with any `Restart=`) start a *second* session against the same project,
#: which is the duplicate-worker failure stated as an acceptance condition.
#:
#: `ExecStartPre=-tmux kill-session` — a restart must not leave the previous session's
#: orchestrator running beside the new one. The leading `-` makes "there was no session"
#: a success, so a cold start is not a failed start.
#:
#: One named session — an unnamed or index-named session is one nobody can attach to,
#: reap, or check for, so `tmux_window_exists`-style ownership checks would have nothing
#: to ask about.
SERVICE_REQUIRED_DIRECTIVES = (
    "Type=forking",
    "ExecStartPre=-/usr/bin/tmux kill-session",
    "ExecStart=/usr/bin/tmux new-session -d -s",
)


def service_session_name(project_name: str) -> str:
    """The tmux session name a generated unit owns for `project_name`."""
    return f"{project_name}{SERVICE_SESSION_SUFFIX}"


def service_unit_conforms(unit_text: str) -> list[str]:
    """The `[INV-10]` directives `unit_text` is missing — empty means it conforms.

    A predicate over the rendered unit rather than over the template, so it holds for a unit
    a project scaffolded months ago and has since edited by hand, which is the only copy that
    actually runs.
    """
    lines = [line.strip() for line in unit_text.splitlines()]
    missing = []
    for directive in SERVICE_REQUIRED_DIRECTIVES:
        if not any(line.startswith(directive) for line in lines):
            missing.append(directive)
    return missing


# ── `[P3]`: the recovery scan the watchdog may ask for, but never performs ──


def request_lease_reconciliation(reason: str) -> bool:
    """Ask the controller to reconcile its writer leases (`02_…` §6).

    The watchdog is the process that notices a dead orchestrator, so it is the obvious
    place to want a recovery scan — and exactly the wrong place to run one. `[INV-01]`
    makes `control.sqlite3` the controller's alone, and `[INV-02]` means reconciliation
    decides whether a worktree may be handed to a new writer. A watchdog that made that
    call from outside the single-writer boundary could hand a live agent's worktree away
    while the orchestrator it just relaunched was reaching the same conclusion.

    So this enqueues a command and returns. The controller performs the scan on its next
    tick, through `maestro.control.reconcile`, which is the same code path a clean restart
    takes — a recovery path that only runs after a crash is a recovery path that is never
    tested.

    Returns True when the command was enqueued (this project has a control plane), False
    when it has none and the legacy reaper is still the whole story.

    P13 W8: the controller now consumes these, and a settled command is never re-opened by
    its key. So a request whose predecessor was already applied takes a key chained to that
    predecessor: while one is still waiting nothing new is enqueued (a restart loop cannot
    flood the inbox), and a start after the scan ran asks for a fresh one.
    """
    key = f"reconcile:{reason}"
    while True:
        command = enqueue_external_command(
            "reconcile_leases", {"reason": reason}, idempotency_key=key
        )
        if command is None:
            return False
        if getattr(command, "status", "received") in ("received", "processing"):
            return True
        key = f"reconcile:{reason}:after:{command.command_id}"
