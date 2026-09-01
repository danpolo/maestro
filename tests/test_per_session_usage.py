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


# =======================================================================================
# End to end: the real claude driver, a real transcript, and D4 actually firing
# =======================================================================================
#
# Everything above stubs the driver. These do not — they build the files a launched
# implementer really leaves behind (`session_uuid.txt` in the workspace, a transcript
# under the CLI's own home) and let `_context_rotations` reach the real `ClaudeBackend`.
# That is the check A4 could not make: its suite was green with the trigger inert, because
# every sample it rotated on was one it had constructed itself.

from maestro.backends.claude import SESSION_UUID_FILE, ClaudeBackend   # noqa: E402
from maestro.limits import ModelLimits                                 # noqa: E402
from maestro.switch import REASON_CONTEXT, SwitchOutcome               # noqa: E402

#: A stand-in limits row. No test here reads a real model-limits table.
ROW = ModelLimits("a-model", 100_000, 120_000, 150_000, 180_000, 240_000)

LAUNCHED_AT = "2026-08-12T00:00:00Z"
#: The transcript's mtime, which is what dates the reading — after the launch above.
WROTE_AT = 1786496400.0                     # 2026-08-12T01:00:00Z

UUID_ONE = "11111111-2222-3333-4444-555555555555"
UUID_TWO = "99999999-8888-7777-6666-555555555555"


def _assistant(tokens: int) -> str:
    """One assistant turn carrying `tokens` of live context, as Claude Code writes it."""
    import json
    return json.dumps({
        "type": "assistant",
        "message": {
            "model": "a-model",
            "usage": {"input_tokens": 2,
                      "cache_creation_input_tokens": 600,
                      "cache_read_input_tokens": tokens - 602,
                      "output_tokens": 1_500},
        },
    })


@pytest.fixture
def live(monkeypatch, tmp_path):
    """A loop whose claude driver is the real one, reading files a real launch leaves."""
    import os

    box = SimpleNamespace(switches=[], home=tmp_path / "claude-home",
                          workspaces=tmp_path / "workspaces")
    box.workspaces.mkdir(parents=True, exist_ok=True)
    (box.home / "projects" / "-a-worktree").mkdir(parents=True, exist_ok=True)
    # The operator's own statusline document, sitting right there with a full context on
    # it — the number A4 refused to act on, and the one A5 must still refuse to act on.
    (tmp_path / "usage.json").write_text(
        '{"context_used_pct":23,"context_total_input_tokens":228972,'
        '"five_hour":{"used_pct":9,"resets_at":1786373400},'
        '"model":"A Display Name","updated_at":"2026-08-12T01:30:00Z"}',
        encoding="utf-8",
    )

    def _launched(session_id: str, uuid_: str, tokens: int | None):
        """The workspace + transcript a launched implementer leaves behind."""
        workspace = box.workspaces / session_id
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / SESSION_UUID_FILE).write_text(uuid_, encoding="utf-8")
        if tokens is not None:
            path = box.home / "projects" / "-a-worktree" / f"{uuid_}.jsonl"
            path.write_text(_assistant(tokens) + "\n", encoding="utf-8")
            os.utime(path, (WROTE_AT, WROTE_AT))
        return workspace

    box.launched = _launched

    def _rotate(task_id, **kwargs):
        box.switches.append(SimpleNamespace(task_id=task_id, **kwargs))
        entry = dict(kwargs.get("entry") or {})
        entry.update({"session_id": f"{task_id}-next", "started_at": "2026-08-12T02:00:00Z"})
        return SwitchOutcome(task_id=task_id, reason=kwargs.get("reason", ""),
                             from_backend=kwargs.get("from_backend", CLAUDE),
                             to_backend=kwargs.get("from_backend", CLAUDE),
                             switched=True, entry=entry,
                             new_session_id=entry["session_id"])

    monkeypatch.setattr(orchestrator, "WORKSPACES", box.workspaces)
    monkeypatch.setattr(orchestrator, "USAGE_JSON", tmp_path / "usage.json")
    monkeypatch.setattr(orchestrator, "switch_task", _rotate)
    monkeypatch.setattr(orchestrator, "resolve_model_limits", lambda name: ROW)
    monkeypatch.setattr(orchestrator, "model_for", lambda role, backend=None, **kw: "a-model")
    monkeypatch.setattr(
        orchestrator, "get_backend",
        lambda name: ClaudeBackend(usage_path=tmp_path / "usage.json", claude_home=box.home),
    )
    orchestrator._rotation_notices.clear()
    yield box
    orchestrator._rotation_notices.clear()


def test_a_full_session_really_rotates(live, tmp_path):
    """D4, end to end, on the real driver. This is what A4 shipped unable to do."""
    entry = _entry(tmp_path)
    live.launched(entry["session_id"], UUID_ONE, tokens=130_000)

    assert orchestrator._context_rotations([entry], None, {}) == 1
    assert live.switches[0].reason == REASON_CONTEXT


def test_the_operators_own_context_still_rotates_nobody(live, tmp_path):
    """The defect, in the shape it actually had. `usage.json` says 228 972 tokens, well
    over the mark; the implementer has written no transcript. Nothing may move."""
    entry = _entry(tmp_path)
    live.launched(entry["session_id"], UUID_ONE, tokens=None)     # launched, no turn yet

    assert orchestrator._context_rotations([entry], None, {}) == 0
    assert live.switches == []


def test_only_the_session_that_is_full_rotates(live, tmp_path):
    """Two implementers on one backend, one full and one fresh. Per-backend sampling
    could only ever have moved both or neither."""
    full = _entry(tmp_path, sid="impl-T1-1", task_id="T1")
    fresh = _entry(tmp_path, sid="impl-T2-1", task_id="T2")
    live.launched(full["session_id"], UUID_ONE, tokens=130_000)
    live.launched(fresh["session_id"], UUID_TWO, tokens=20_000)

    in_flight = [full, fresh]
    assert orchestrator._context_rotations(in_flight, None, {}) == 1
    assert [switch.task_id for switch in live.switches] == ["T1"]


def test_a_reading_from_before_the_launch_still_rotates_nobody(live, tmp_path):
    """A4's freshness guard, now fed by a real transcript: the mtime is the moment the
    agent last wrote a turn, so a transcript that has not moved since the rotation cannot
    justify the next one."""
    entry = _entry(tmp_path)
    entry["started_at"] = "2026-08-12T02:00:00Z"      # relaunched after the last turn
    live.launched(entry["session_id"], UUID_ONE, tokens=130_000)

    assert orchestrator._context_rotations([entry], None, {}) == 0
    assert live.switches == []


def test_the_rotation_cap_still_bounds_a_real_reading(live, tmp_path):
    """`MAX_CONTEXT_ROTATIONS` is defence-in-depth against exactly this situation — a
    feed that is live, is attributed, and keeps reporting a full context. Turning the
    feed on is not a reason to relax it."""
    entry = _entry(tmp_path)
    entry["context_rotations"] = orchestrator.MAX_CONTEXT_ROTATIONS
    live.launched(entry["session_id"], UUID_ONE, tokens=130_000)

    assert orchestrator._context_rotations([entry], None, {}) == 0
    assert live.switches == []


def test_a_real_rotation_counts_itself_towards_the_cap(live, tmp_path):
    entry = _entry(tmp_path)
    live.launched(entry["session_id"], UUID_ONE, tokens=130_000)

    orchestrator._context_rotations([entry], None, {})

    assert live.switches[0].entry["context_rotations"] == 1
