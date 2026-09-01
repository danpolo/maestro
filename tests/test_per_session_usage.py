"""A5: a context reading that is about **one named session**, not about an account.

A2 gave the loop a usage sample per backend. That is the right cardinality for a quota
window — a window belongs to the account — and the wrong one for a context reading,
which belongs to one conversation. A4 built the D4 rotation on top of the per-backend
sample and then refused to act on it, because nothing could show the reading was the
in-flight implementer's rather than (on the claude path) the operator's own interactive
statusline. This module pins the seam that closes that gap:

* `AgentBackend.usage()` takes an optional `Handle`. With one, the driver answers about
  that session or answers `None`; without one it answers about the account, exactly as
  before.
* `_sampled_usage`'s per-poll memo is keyed on **backend *and* session**, because one
  slot per backend can no longer hold the answer to two different questions.
* The account-level slot is still taken at most once per backend per poll — that is the
  call that may cost a short-lived subprocess (`CodexBackend.usage_from_app_server`),
  and A5 must not multiply it by the number of in-flight tasks.

Nothing here runs an agent CLI: `get_backend` is stubbed in every test.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from maestro import orchestrator
from maestro.backends.base import Capabilities, Handle, Usage, WindowUsage

CLAUDE = "claude"


class _RecordingDriver:
    """A driver that records the handle (or lack of one) it was asked about."""

    def __init__(self, calls: list, *, context_tokens: int = 0, session_id: str = ""):
        self.calls = calls
        self._context_tokens = context_tokens
        self._session_id = session_id

    def capabilities(self) -> Capabilities:
        return Capabilities(usage_telemetry=True)

    def usage(self, handle: Handle | None = None) -> Usage | None:
        self.calls.append(handle)
        if handle is None:
            # The account-level answer: quota windows, and no attribution at all.
            return Usage(windows={300: WindowUsage(used_pct=10.0)})
        return Usage(
            windows={},
            context_total_input_tokens=self._context_tokens,
            updated_at="2026-08-12T01:00:00Z",
            session_id=self._session_id or handle.session_id,
        )


def _entry(tmp_path: Path, *, sid="impl-T1-1", task_id="T1", backend=CLAUDE) -> dict:
    worktree = tmp_path / f"wt-{sid}"
    worktree.mkdir(parents=True, exist_ok=True)
    return {
        "session_id": sid,
        "task_id": task_id,
        "role": "implementer",
        "worktree": str(worktree),
        "window": f"impl-{task_id}",
        "started_at": "2026-08-12T00:00:00Z",
        "status": "running",
        "backend": backend,
    }


@pytest.fixture
def driver(monkeypatch, tmp_path):
    box = SimpleNamespace(calls=[])
    monkeypatch.setattr(orchestrator, "WORKSPACES", tmp_path / "workspaces")
    monkeypatch.setattr(orchestrator, "USAGE_JSON", tmp_path / "usage.json")
    monkeypatch.setattr(
        orchestrator, "get_backend",
        lambda name: _RecordingDriver(box.calls, context_tokens=130_000),
    )
    return box


# ── the handle the rotation path passes ──


def test_the_rotation_sample_asks_about_the_entrys_own_session(driver, tmp_path):
    entry = _entry(tmp_path)

    orchestrator._rotation_sample(entry, {})

    (handle,) = driver.calls
    assert handle is not None
    assert handle.session_id == "impl-T1-1"
    assert handle.backend == CLAUDE
    # The workspace is where the launch left the driver's own session sidecar; the entry
    # itself records maestro's session id and never the backend's native one.
    assert handle.workspace == orchestrator.WORKSPACES / "impl-T1-1"
    assert handle.native_id is None


def test_an_entry_naming_no_session_takes_no_reading_at_all(driver, tmp_path):
    entry = _entry(tmp_path)
    entry["session_id"] = ""

    assert orchestrator._rotation_sample(entry, {}) is None
    # Not even an account-level fallback: an unattributable reading is exactly the
    # evidence D4 must never rotate on.
    assert driver.calls == []


# ── the memo's cardinality ──


def test_the_account_level_reading_is_taken_without_a_handle(driver, tmp_path):
    orchestrator._write_usage_sample([_entry(tmp_path)], {})

    assert driver.calls == [None]


def test_two_sessions_on_one_backend_get_their_own_readings(driver, tmp_path):
    cache: dict = {}
    one = _entry(tmp_path, sid="impl-T1-1", task_id="T1")
    two = _entry(tmp_path, sid="impl-T2-1", task_id="T2")

    orchestrator._rotation_sample(one, cache)
    orchestrator._rotation_sample(two, cache)

    assert [h.session_id for h in driver.calls] == ["impl-T1-1", "impl-T2-1"]


def test_the_same_session_is_sampled_once_per_poll(driver, tmp_path):
    cache: dict = {}
    entry = _entry(tmp_path)

    orchestrator._rotation_sample(entry, cache)
    orchestrator._rotation_sample(entry, cache)

    assert len(driver.calls) == 1


def test_one_account_level_sample_per_backend_however_many_sessions(driver, tmp_path):
    """The constraint A5 is most able to break silently.

    The account-level call is the one that may spawn a short-lived subprocess. Keying the
    memo on the session as well as the backend must not turn one such call per poll into
    one per in-flight task.
    """
    cache: dict = {}
    entries = [_entry(tmp_path, sid=f"impl-T{n}-1", task_id=f"T{n}") for n in (1, 2, 3)]

    for entry in entries:
        orchestrator._under_usage_pressure(orchestrator._entry_backend(entry), 0.0, cache)
        orchestrator._rotation_sample(entry, cache)
    orchestrator._write_usage_sample(entries, cache)

    assert driver.calls.count(None) == 1
    assert len([h for h in driver.calls if h is not None]) == 3
