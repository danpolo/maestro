"""An idle controller that adopts a new maestro exits, so the watchdog relaunches it.

`adopt` only moves the pin; the running process keeps the code it imported. Exiting 0 at
the idle moment hands the restart to the watchdog's liveness relaunch, whose `maestro run`
re-execs into the new pin (`maestro.bootstrap`).
"""
from __future__ import annotations

import pytest

from maestro import orchestrator


@pytest.fixture
def journal(monkeypatch):
    events = []
    monkeypatch.setattr(orchestrator, "append_journal", lambda e, d: events.append((e, d)))
    monkeypatch.setattr(orchestrator, "_last_self_update_check", 0.0)
    return events


def _given(monkeypatch, *, recorded, status):
    state = {"maestro_version": recorded} if recorded else {}
    monkeypatch.setattr(orchestrator, "read_state", lambda: dict(state))
    monkeypatch.setattr(orchestrator, "maybe_self_update", lambda: status)


def test_adopting_a_new_version_asks_the_loop_to_exit(monkeypatch, journal):
    _given(monkeypatch, recorded="old", status="adopted:new")

    assert orchestrator._self_update_maybe() is True
    assert [e for e, _ in journal] == ["self_update_restart"]


@pytest.mark.parametrize("recorded, status", [
    (None, "adopted:running"),   # first run records the code already running
    ("old", "up_to_date"),
    ("old", "held_red:bad"),
    ("old", "error:boom"),
])
def test_anything_else_keeps_the_loop_running(monkeypatch, journal, recorded, status):
    _given(monkeypatch, recorded=recorded, status=status)

    assert orchestrator._self_update_maybe() is False
    assert journal == []


def test_the_check_is_throttled(monkeypatch, journal):
    _given(monkeypatch, recorded="old", status="adopted:new")
    assert orchestrator._self_update_maybe() is True

    assert orchestrator._self_update_maybe() is False, "a second check inside the window"
