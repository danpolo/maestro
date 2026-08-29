"""`project.yaml`'s `thresholds:` actually reaches `maestro.quota`.

`concurrency_cap` and `five_h_pause_pct` were declared in the template from the first
version and read by nothing — a project could set `concurrency_cap: 1` to throttle itself
and get three implementers anyway. `tests/test_templates.py` recorded both as known-dead;
these are what struck them off, and what fails if a refactor quietly re-hardcodes either.

`quota` reads them at import (the shape `maestro.watchdog` already uses for its own two),
so each case reloads the module against a temporary `project.yaml` rather than trying to
change a value that was resolved before the test started.
"""
from __future__ import annotations

import importlib

import pytest
import yaml

from maestro import config as _config
from maestro import quota as _quota


def _quota_with(tmp_path, monkeypatch, thresholds):
    """Reload `maestro.quota` against a `project.yaml` holding `thresholds`."""
    path = tmp_path / "project.yaml"
    path.write_text(yaml.safe_dump({"thresholds": thresholds} if thresholds is not None else {}),
                    encoding="utf-8")
    monkeypatch.setattr(_config, "PROJECT_YAML", path)
    return importlib.reload(_quota)


@pytest.fixture(autouse=True)
def _restore_quota():
    """Leave the real module in place for every other test in the session."""
    yield
    importlib.reload(_quota)


def test_the_defaults_are_what_the_constants_used_to_be(tmp_path, monkeypatch):
    """Wiring a knob must not change the behaviour of a project that never set it."""
    quota = _quota_with(tmp_path, monkeypatch, None)
    assert quota.CONCURRENCY_CAP == 3
    assert quota.PAUSE_PCT == 92.0


def test_a_configured_concurrency_cap_is_the_one_the_loop_uses(tmp_path, monkeypatch):
    quota = _quota_with(tmp_path, monkeypatch, {"concurrency_cap": 1})
    assert quota.CONCURRENCY_CAP == 1


def test_a_configured_pause_threshold_is_the_one_the_loop_uses(tmp_path, monkeypatch):
    quota = _quota_with(tmp_path, monkeypatch, {"five_h_pause_pct": 80})
    assert quota.PAUSE_PCT == 80.0


def test_the_pause_threshold_is_a_float_even_when_yaml_gives_an_int(tmp_path, monkeypatch):
    """The template writes `five_h_pause_pct: 92`, and it is compared against a float
    percentage — an int would still compare correctly, but the type is pinned so the
    comparison never depends on Python's numeric coercion rules."""
    quota = _quota_with(tmp_path, monkeypatch, {"five_h_pause_pct": 92})
    assert isinstance(quota.PAUSE_PCT, float)


def test_the_effective_cap_honours_the_configured_pause_threshold(tmp_path, monkeypatch):
    """The end-to-end claim: the knob changes what the loop actually does, not just what
    a constant says. At 85% used, a project that set the threshold to 80 is paused
    (cap 0) while the default project is merely throttled."""
    quota = _quota_with(tmp_path, monkeypatch, {"five_h_pause_pct": 80})
    monkeypatch.setattr(quota, "read_json", lambda _p: {"five_hour": {"used_pct": 85}})
    assert quota.get_effective_cap() == (0, 85.0)

    quota = _quota_with(tmp_path, monkeypatch, None)
    monkeypatch.setattr(quota, "read_json", lambda _p: {"five_hour": {"used_pct": 85}})
    cap, pct = quota.get_effective_cap()
    assert cap == quota.THROTTLE_75_CAP and pct == 85.0


def test_the_constant_no_longer_names_a_value_it_may_not_have(tmp_path, monkeypatch):
    """`PAUSE_92_PCT` was renamed to `PAUSE_PCT` when it became configurable: a constant
    whose name states its value is a name that lies the moment a project sets another."""
    quota = _quota_with(tmp_path, monkeypatch, {"five_h_pause_pct": 80})
    assert not hasattr(quota, "PAUSE_92_PCT")


@pytest.mark.parametrize("thresholds", [
    {"concurrency_cap": None}, "not-a-mapping", {"five_h_pause_pct": "abc"},
], ids=["null-value", "not-a-mapping", "unparseable"])
def test_a_malformed_thresholds_block_does_not_stop_the_loop(tmp_path, monkeypatch, thresholds):
    """`maestro.config` degrades rather than raising, and a module that reads it at import
    must not turn a typo in a config file into a package that will not import."""
    try:
        quota = _quota_with(tmp_path, monkeypatch, thresholds)
    except Exception as exc:            # pragma: no cover — the failure this guards
        pytest.fail(f"a malformed thresholds block broke the import: {exc!r}")
    assert quota.CONCURRENCY_CAP is not None
