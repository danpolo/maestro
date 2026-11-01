"""The watchdog follows its project's pin: a release reaches it, not only the controller."""
from __future__ import annotations

import pytest

from maestro import bootstrap, watchdog


@pytest.fixture
def pinned(tmp_path):
    pin = bootstrap.project_pin_file(tmp_path)
    pin.parent.mkdir(parents=True)
    pin.write_text("whatever adopted_root resolves\n")
    return tmp_path


def test_no_reexec_while_running_the_pinned_checkout(monkeypatch, pinned):
    monkeypatch.setattr(bootstrap, "adopted_root", lambda **k: bootstrap.running_root())
    assert watchdog.repinned_root(pinned) is None


def test_no_reexec_without_a_usable_pin(monkeypatch, pinned):
    monkeypatch.setattr(bootstrap, "adopted_root", lambda **k: None)
    assert watchdog.repinned_root(pinned) is None


def test_the_host_default_is_not_followed(monkeypatch, tmp_path):
    """No project pin: a stray `~/.maestro/current` must not move this watchdog."""
    monkeypatch.setattr(bootstrap, "adopted_root", lambda **k: tmp_path / "elsewhere")
    assert watchdog.repinned_root(tmp_path) is None


def test_a_moved_pin_is_reported(monkeypatch, pinned):
    tmp_path = pinned
    seen = {}

    def adopted(**kwargs):
        seen.update(kwargs)
        return tmp_path / "versions" / "new"
    monkeypatch.setattr(bootstrap, "adopted_root", adopted)

    assert watchdog.repinned_root(tmp_path) == tmp_path / "versions" / "new"
    assert seen == {"repo": tmp_path}


def test_reexec_drops_the_sentinel_so_the_child_resolves_the_pin(monkeypatch, tmp_path):
    calls, events = [], []
    monkeypatch.setattr(watchdog, "append_journal", lambda e, d: events.append(e))
    monkeypatch.setattr(watchdog.os, "execve", lambda *a: calls.append(a))
    monkeypatch.setenv(bootstrap.SENTINEL, "1")

    watchdog.reexec_onto(tmp_path / "versions" / "new")

    (path, argv, env), = calls
    assert path == str(watchdog.VENV_MAESTRO) and argv[1:] == ["watchdog"]
    assert bootstrap.SENTINEL not in env
    assert env[bootstrap.PROJECT_ENV_VAR] == str(watchdog.REPO)
    assert events == ["watchdog_reexec"]
