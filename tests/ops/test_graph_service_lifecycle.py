"""`[P11]` / `[INV-10]`: the generated service, and what a restart must not duplicate.

Two halves of one acceptance condition — *service restart or controller crash never
duplicates active workers*:

1. **The unit.** `Type=forking` with `tmux new-session -d` as `ExecStart`, and a
   `kill-session` before it. Under `Type=simple` systemd reads tmux's immediate return as
   the service exiting and starts a second session against the same project; without the
   `ExecStartPre` a restart leaves the old session's orchestrator running beside the new one.
   Both produce two loops driving one repository.
2. **The restart.** A watchdog start is, in production, a service start: after a reboot, a
   `systemctl restart`, or a controller crash. Leases held by attempts whose process is gone
   must be reconciled before the relaunched loop can claim them — and the watchdog must *ask*
   for that scan rather than perform it (`[INV-01]`).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from maestro import cli, watchdog

TEMPLATE = (Path(cli.__file__).resolve().parent / "templates" / "systemd"
            / "maestro-watchdog.service.tmpl")


@pytest.fixture
def unit_text(tmp_path) -> str:
    """The unit `maestro init` would write for a project at `tmp_path`."""
    facts = {"project_name": "demo", "repo_path": str(tmp_path / "demo")}
    mapping = {"project_name": facts["project_name"], **cli._systemd_service_mapping(facts)}
    return cli._render(TEMPLATE.read_text(encoding="utf-8"), mapping)


# ── the unit ─────────────────────────────────────────────────────────────────────────


def test_the_generated_unit_satisfies_every_required_directive(unit_text):
    assert watchdog.service_unit_conforms(unit_text) == []


def test_the_generated_unit_forks_into_one_named_tmux_session(unit_text):
    assert "Type=forking" in unit_text
    exec_start = next(l for l in unit_text.splitlines() if l.startswith("ExecStart="))
    assert exec_start.startswith("ExecStart=/usr/bin/tmux new-session -d -s ")
    assert " -n main " in exec_start, (
        "a named window is what makes the running loop attachable and reapable"
    )
    assert watchdog.service_session_name("demo") in exec_start


def test_the_generated_unit_kills_the_previous_session_before_starting(unit_text):
    pre = [l for l in unit_text.splitlines() if l.startswith("ExecStartPre=")]
    assert any(l.startswith("ExecStartPre=-/usr/bin/tmux kill-session") for l in pre), (
        "without this, `systemctl restart` leaves the old orchestrator running beside the new"
    )
    assert any(l.startswith("ExecStop=-/usr/bin/tmux kill-session")
               for l in unit_text.splitlines())


@pytest.mark.parametrize("directive, broken", [
    ("Type=forking", "Type=simple"),
    ("ExecStartPre=-/usr/bin/tmux kill-session", "ExecStartPre=-/bin/true"),
    ("ExecStart=/usr/bin/tmux new-session -d -s", "ExecStart=/srv/demo/.venv/bin/maestro watchdog"),
])
def test_a_hand_edited_unit_that_lost_a_directive_is_reported(unit_text, directive, broken):
    """The copy that runs is `/etc/systemd/system`'s, which an operator may have edited."""
    lines = [broken if l.startswith(directive) else l for l in unit_text.splitlines()]
    assert directive in watchdog.service_unit_conforms("\n".join(lines))


def test_the_unit_runs_the_watchdog_not_the_orchestrator_directly(unit_text):
    """The watchdog is the supervisor; `maestro run` is what *it* launches, into the
    shared `agents` session. A unit that started `run` itself would give the loop no
    supervisor and no stall detection."""
    directives = [l for l in unit_text.splitlines() if l and not l.startswith("#")]
    assert any("maestro watchdog" in l for l in directives)
    assert not any("maestro run" in l for l in directives)


# ── the restart ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def sealed_watchdog(monkeypatch, tmp_path):
    """`watchdog`'s module globals pointed at a scratch project, with no real subprocesses."""
    orch = tmp_path / ".orchestrator"
    orch.mkdir(parents=True)
    monkeypatch.setattr(watchdog, "REPO", tmp_path)
    monkeypatch.setattr(watchdog, "STATE_JSON", orch / "state.json")
    monkeypatch.setattr(watchdog, "JOURNAL", orch / "journal.ndjson")
    monkeypatch.setattr(watchdog, "HALT_FILE", orch / "HALT")
    monkeypatch.setattr(watchdog, "notify_telegram", lambda *a, **k: None)
    enqueued = []
    monkeypatch.setattr(
        watchdog, "enqueue_external_command",
        lambda command_type, payload, *, idempotency_key, **kw: enqueued.append(
            (command_type, payload, idempotency_key)) or object(),
    )
    return enqueued


def test_a_watchdog_start_asks_the_controller_to_reconcile_its_leases(sealed_watchdog):
    """A service start may follow a crash that left leases held by dead attempts."""
    watchdog.HALT_FILE.touch()   # stop after the first poll: this is about the start, not the loop

    assert watchdog.main() == 0

    kinds = [command for command, _payload, _key in sealed_watchdog]
    assert "reconcile_leases" in kinds
    reasons = [payload["reason"] for command, payload, _ in sealed_watchdog
               if command == "reconcile_leases"]
    assert reasons == ["watchdog_start"]


def test_the_reconciliation_request_is_a_command_never_a_scan(sealed_watchdog, monkeypatch):
    """`[INV-01]`: the watchdog notices, the controller acts. It must not touch leases."""
    from maestro.control import reconcile

    monkeypatch.setattr(
        reconcile, "reconcile_leases",
        lambda *a, **k: pytest.fail("the watchdog ran the recovery scan itself"),
        raising=False,
    )
    watchdog.HALT_FILE.touch()
    assert watchdog.main() == 0


def test_the_request_is_idempotent_across_repeated_starts(sealed_watchdog):
    """The key is derived from what the command is about, not from the clock, so a
    restart loop cannot flood the inbox with distinct commands."""
    watchdog.HALT_FILE.touch()
    watchdog.main()
    watchdog.main()

    keys = {key for _command, _payload, key in sealed_watchdog}
    assert keys == {"reconcile:watchdog_start"}


def test_a_second_orchestrator_is_never_launched_into_the_same_window(monkeypatch, sealed_watchdog):
    """The window name is the ownership boundary: tmux would otherwise make `orchestrator-1`."""
    monkeypatch.setattr(watchdog, "tmux_window_exists", lambda window: True)
    launched = []
    monkeypatch.setattr(watchdog.subprocess, "run",
                        lambda *a, **k: launched.append(a) or pytest.fail(
                            "a duplicate orchestrator was launched into an owned window"))

    watchdog.launch_orchestrator()

    assert launched == []


def test_a_launch_into_a_free_window_holds_an_exclusive_lock(monkeypatch, sealed_watchdog):
    """The window check is not atomic with `new-window`; `flock -n` is what makes two
    watchdogs (or two tmux sockets after recovery) unable to run two loops."""
    monkeypatch.setattr(watchdog, "tmux_window_exists", lambda window: False)
    commands = []

    class _Done:
        returncode = 0

    monkeypatch.setattr(watchdog.subprocess, "run",
                        lambda cmd, **kwargs: commands.append(cmd) or _Done())

    watchdog.launch_orchestrator()

    assert len(commands) == 1
    assert "flock -n" in commands[0]
    assert f"new-window -t {watchdog.TMUX_SESSION} -n {watchdog.TMUX_WINDOW}" in commands[0]
