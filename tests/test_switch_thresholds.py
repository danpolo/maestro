"""`project.yaml`'s `thresholds:` actually reaches `maestro.switch`.

`switch_threshold_pct` was hardcoded at 70.0 pending calibration in anger; now it is
configurable via the `thresholds:` block, and its value affects `threshold_crossed()`.

Like `maestro.quota`, `maestro.switch` reads `switch_threshold_pct` at import time,
so each test reloads the module against a temporary `project.yaml` rather than trying
to change a value that was resolved before the test started.
"""
from __future__ import annotations

import importlib

import pytest
import yaml

from maestro import config as _config
from maestro import switch as _switch
from maestro.backends.base import Usage, WindowUsage


def _switch_with(tmp_path, monkeypatch, thresholds):
    """Reload `maestro.switch` against a `project.yaml` holding `thresholds`."""
    path = tmp_path / "project.yaml"
    path.write_text(yaml.safe_dump({"thresholds": thresholds} if thresholds is not None else {}),
                    encoding="utf-8")
    monkeypatch.setattr(_config, "PROJECT_YAML", path)
    return importlib.reload(_switch)


@pytest.fixture(autouse=True)
def _restore_switch():
    """Leave the real module in place for every other test in the session."""
    yield
    importlib.reload(_switch)


def test_the_switch_threshold_default_is_70(tmp_path, monkeypatch):
    """Wiring a knob must not change the behaviour of a project that never set it."""
    switch = _switch_with(tmp_path, monkeypatch, None)
    assert switch.SWITCH_THRESHOLD_PCT == 70.0


def test_a_configured_switch_threshold_is_the_one_used(tmp_path, monkeypatch):
    """When switch_threshold_pct is configured, that value is used."""
    switch = _switch_with(tmp_path, monkeypatch, {"switch_threshold_pct": 80})
    assert switch.SWITCH_THRESHOLD_PCT == 80.0


def test_a_configured_switch_threshold_changes_behavior(tmp_path, monkeypatch):
    """The observable effect: at 75% used, a project set to 80% does not cross,
    but one set to 70% does. This proves the knob has an observable effect, not
    merely that it parses."""
    # Load with 80% threshold - should NOT cross at 75%
    switch = _switch_with(tmp_path, monkeypatch, {"switch_threshold_pct": 80})
    usage_75 = Usage(windows={300: WindowUsage(used_pct=75.0)})
    assert switch.threshold_crossed(usage_75) is False

    # Load with default 70% threshold - should cross at 75%
    switch = _switch_with(tmp_path, monkeypatch, None)
    assert switch.threshold_crossed(usage_75) is True


def test_the_switch_threshold_is_a_float_even_when_yaml_gives_an_int(tmp_path, monkeypatch):
    """Type consistency: the value is cast to float."""
    switch = _switch_with(tmp_path, monkeypatch, {"switch_threshold_pct": 75})
    assert isinstance(switch.SWITCH_THRESHOLD_PCT, float)
    assert switch.SWITCH_THRESHOLD_PCT == 75.0


def test_a_malformed_switch_threshold_degrades_to_default(tmp_path, monkeypatch):
    """A malformed value (unparseable as float) returns the default 70.0, following
    the `maestro.config` pattern: degrade gracefully rather than raising, because a
    module that dies on a typo'd config is worse than one that ignores it."""
    switch = _switch_with(tmp_path, monkeypatch, {"switch_threshold_pct": "abc"})
    assert switch.SWITCH_THRESHOLD_PCT == 70.0
