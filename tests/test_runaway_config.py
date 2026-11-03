"""P13 W13: the `runaway` setting, its layers, and the Telegram switch."""
from __future__ import annotations

import pytest

from maestro import config


def test_detection_is_on_by_default_b11():
    assert config.runaway_mode({}) == "on"
    assert config.runaway_mode({"runaway": {}}) == "on"
    assert config.runaway_factor({}) == 2.0


def test_project_then_task_override():
    assert config.runaway_mode({"runaway": {"mode": "shadow"}}) == "shadow"
    assert config.runaway_mode({"runaway": {"mode": "shadow"}}, task={"runaway": {"mode": "off"}}) == "off"
    assert config.runaway_factor({"runaway": {"factor": 3}}) == 3.0
    assert config.runaway_factor({"runaway": {"factor": 3}}, task={"runaway": {"factor": 1.5}}) == 1.5


@pytest.mark.parametrize("block, message", [
    ("on", "mapping"),
    ({"mode": "maybe"}, "runaway.mode"),
    ({"factor": 0}, "runaway.factor"),
    ({"factor": -1}, "runaway.factor"),
    ({"factor": True}, "runaway.factor"),
    ({"factor": "2"}, "runaway.factor"),
    ({"factor": float("inf")}, "runaway.factor"),
    ({"factors": 2}, "unknown"),
])
def test_bad_settings_are_refused(block, message):
    with pytest.raises(config.ConfigError, match=message):
        config.runaway_factor({"runaway": block})
        config.runaway_mode({"runaway": block})


def test_doctor_reports_a_bad_block():
    from maestro.model_policy import role_shape_issues

    assert any("runaway" in issue for issue in role_shape_issues({"runaway": {"mode": "maybe"}}))
    assert not any("runaway" in issue for issue in role_shape_issues({"runaway": {"mode": "off"}}))


# --- Telegram ------------------------------------------------------------------------

@pytest.fixture
def telegram(monkeypatch):
    from maestro.hitl import commands

    state: dict = {}
    sent: list[str] = []
    monkeypatch.setattr(commands, "read_state", lambda: state)
    monkeypatch.setattr(commands, "write_state", lambda document: state.update(document))
    monkeypatch.setattr(commands, "notify_telegram", sent.append)
    monkeypatch.setattr(config, "load_project_yaml", lambda: {"runaway": {"mode": "shadow"}})
    return commands, state, sent


def test_the_command_is_registered(telegram):
    commands, _, _ = telegram
    assert "runaway" in commands.COMMANDS
    assert commands.COMMANDS["runaway"].handler is commands._control_runaway


@pytest.mark.parametrize("mode", ["on", "shadow", "off"])
def test_the_command_sets_the_durable_control(telegram, mode):
    commands, state, sent = telegram
    commands._control_runaway(mode, {})
    assert state["runaway_control"] == mode
    assert f"Runaway detection: {mode}" in sent[-1]


@pytest.mark.parametrize("arg", ["", "status"])
def test_status_reports_the_effective_mode_and_changes_nothing(telegram, arg):
    commands, state, sent = telegram
    commands._control_runaway(arg, {})
    assert "runaway_control" not in state
    assert "Runaway detection: shadow" in sent[-1]


def test_status_with_an_invalid_config_still_answers(telegram, monkeypatch):
    commands, _, sent = telegram
    monkeypatch.setattr(config, "load_project_yaml", lambda: {"runaway": {"mode": "maybe"}})
    commands._control_runaway("status", {})
    assert "config is invalid" in sent[-1]


def test_an_unknown_argument_changes_nothing_and_shows_usage(telegram):
    commands, state, sent = telegram
    commands._control_runaway("maybe", {})
    assert "runaway_control" not in state
    assert "/runaway on|shadow|off" in sent[-1]
