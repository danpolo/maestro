"""The timed per-backend exhaustion record (A6).

`maestro.quota.paused_until` is a single global timestamp naming no backend: once the
reactive net pauses the loop, it stays paused until the reset regardless of which
backend actually ran dry. `record_backend_exhausted`/`exhausted_backends` add a *named*
record beside it -- backend -> reset_at -- read on every fresh-launch resolution so a
pinned-but-still-exhausted backend is skipped and the fallback chain is walked, instead
of failing onto the same wall on every new task until the pause's own reset.

Reactive only: nothing here classifies an exit or parses a reset time -- both already
exist (`AgentBackend.parse_exit` -> `ExitVerdict(kind="quota_exhausted", reset_at=...)`).
This module only remembers what the driver already decided, and forgets it again once
`reset_at` has passed -- no operator action, no second command to clear it.

The read side (`exhausted_backends`) and the write side (`record_backend_exhausted`) are
covered here in isolation; the wiring that feeds the read side into the three
fresh-launch call sites, and the write side into `reconcile_in_flight`, is covered in
`tests/test_orchestrator_switch_hooks.py`.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from maestro import quota
from maestro import state as state_module

CLAUDE = "claude"
CODEX = "codex"


def _state_file(monkeypatch, tmp_path, **fields) -> Path:
    path = tmp_path / "state.json"
    path.write_text(json.dumps(fields), encoding="utf-8")
    monkeypatch.setattr(state_module, "STATE_JSON", path)
    return path


def _read(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ── record_backend_exhausted ──


def test_records_a_valid_reset_time(monkeypatch, tmp_path):
    path = _state_file(monkeypatch, tmp_path)
    quota.record_backend_exhausted(CLAUDE, "2999-01-01T00:00:00Z")
    assert _read(path)[quota.EXHAUSTED_KEY] == {CLAUDE: "2999-01-01T00:00:00Z"}


def test_normalises_the_backend_name(monkeypatch, tmp_path):
    path = _state_file(monkeypatch, tmp_path)
    quota.record_backend_exhausted("  Claude  ", "2999-01-01T00:00:00Z")
    assert _read(path)[quota.EXHAUSTED_KEY] == {CLAUDE: "2999-01-01T00:00:00Z"}


def test_overwrites_an_earlier_record_for_the_same_backend(monkeypatch, tmp_path):
    path = _state_file(
        monkeypatch, tmp_path, **{quota.EXHAUSTED_KEY: {CLAUDE: "2900-01-01T00:00:00Z"}}
    )
    quota.record_backend_exhausted(CLAUDE, "2999-01-01T00:00:00Z")
    assert _read(path)[quota.EXHAUSTED_KEY] == {CLAUDE: "2999-01-01T00:00:00Z"}


def test_leaves_other_backends_records_alone(monkeypatch, tmp_path):
    path = _state_file(
        monkeypatch, tmp_path, **{quota.EXHAUSTED_KEY: {CODEX: "2999-01-01T00:00:00Z"}}
    )
    quota.record_backend_exhausted(CLAUDE, "2999-06-01T00:00:00Z")
    assert _read(path)[quota.EXHAUSTED_KEY] == {
        CODEX: "2999-01-01T00:00:00Z",
        CLAUDE: "2999-06-01T00:00:00Z",
    }


@pytest.mark.parametrize(
    "reset_at",
    [None, "", "   ", "not-a-timestamp", "2026-13-40T99:99:99Z"],
    ids=["null", "empty", "blank", "junk", "impossible"],
)
def test_a_missing_or_unparseable_reset_time_records_nothing(monkeypatch, tmp_path, reset_at):
    """The binding constraint's write-time half: a value that could never expire must
    never be written, because a record that can never expire is a backend suppressed
    forever."""
    path = _state_file(monkeypatch, tmp_path)
    quota.record_backend_exhausted(CLAUDE, reset_at)
    assert quota.EXHAUSTED_KEY not in _read(path)


@pytest.mark.parametrize("name", ["", "   ", None], ids=["empty", "blank", "null"])
def test_a_blank_backend_name_records_nothing(monkeypatch, tmp_path, name):
    path = _state_file(monkeypatch, tmp_path)
    quota.record_backend_exhausted(name, "2999-01-01T00:00:00Z")
    assert quota.EXHAUSTED_KEY not in _read(path)


def test_an_already_past_reset_time_records_nothing(monkeypatch, tmp_path):
    """Recording a backend as exhausted until a time already gone would suppress it for
    zero seconds, at the cost of a state write -- not wrong, just pointless; refused the
    same way a malformed timestamp is."""
    path = _state_file(monkeypatch, tmp_path)
    quota.record_backend_exhausted(CLAUDE, "2000-01-01T00:00:00Z")
    assert quota.EXHAUSTED_KEY not in _read(path)


# ── exhausted_backends ──


def test_a_future_record_is_returned(monkeypatch, tmp_path):
    _state_file(monkeypatch, tmp_path, **{quota.EXHAUSTED_KEY: {CLAUDE: "2999-01-01T00:00:00Z"}})
    assert quota.exhausted_backends() == frozenset({CLAUDE})


def test_a_past_record_is_not_returned(monkeypatch, tmp_path):
    """Expiry is automatic: once `now >= reset_at` the record is inert, with no
    operator action and no second command to clear it."""
    _state_file(monkeypatch, tmp_path, **{quota.EXHAUSTED_KEY: {CLAUDE: "2000-01-01T00:00:00Z"}})
    assert quota.exhausted_backends() == frozenset()


def test_a_record_expires_as_the_clock_crosses_reset_at(monkeypatch, tmp_path):
    """The observable-acceptance scenario's clock half, isolated from the launch sites:
    the exact same record reads as exhausted before `reset_at` and clear after, with
    nothing rewritten in between."""
    _state_file(monkeypatch, tmp_path, **{quota.EXHAUSTED_KEY: {CLAUDE: "2026-01-01T01:00:00Z"}})

    class _Before(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    class _After(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, 2, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(quota, "datetime", _Before)
    assert quota.exhausted_backends() == frozenset({CLAUDE})

    monkeypatch.setattr(quota, "datetime", _After)
    assert quota.exhausted_backends() == frozenset()


def test_mixed_past_and_future_records_returns_only_the_future_one(monkeypatch, tmp_path):
    _state_file(
        monkeypatch,
        tmp_path,
        **{quota.EXHAUSTED_KEY: {CLAUDE: "2000-01-01T00:00:00Z", CODEX: "2999-01-01T00:00:00Z"}},
    )
    assert quota.exhausted_backends() == frozenset({CODEX})


def test_no_record_at_all_is_an_empty_set(monkeypatch, tmp_path):
    _state_file(monkeypatch, tmp_path)
    assert quota.exhausted_backends() == frozenset()


@pytest.mark.parametrize(
    "body", ["{not json", "", "[]", "null"], ids=["junk", "empty", "list", "null"]
)
def test_an_unreadable_state_document_is_not_exhausted(monkeypatch, tmp_path, body):
    """Fail-safe: a state document maestro cannot even parse must never suppress a
    backend -- the missing/unparseable direction the binding constraint requires."""
    path = tmp_path / "state.json"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setattr(state_module, "STATE_JSON", path)
    assert quota.exhausted_backends() == frozenset()


def test_a_missing_state_document_is_not_exhausted(monkeypatch, tmp_path):
    monkeypatch.setattr(state_module, "STATE_JSON", tmp_path / "never-written.json")
    assert quota.exhausted_backends() == frozenset()


def test_a_non_mapping_table_is_not_exhausted(monkeypatch, tmp_path):
    """A hand-edited state document must not be able to crash resolution."""
    _state_file(monkeypatch, tmp_path, **{quota.EXHAUSTED_KEY: ["claude"]})
    assert quota.exhausted_backends() == frozenset()


def test_a_non_string_reset_time_is_not_exhausted(monkeypatch, tmp_path):
    """An epoch-shaped (numeric) `reset_at` is a different concern from `paused_until`'s
    own epoch-shaped escape hatch (`tests/characterization/test_quota.py`); here it is
    simply not a string, so it fails safe rather than growing a second parser."""
    _state_file(monkeypatch, tmp_path, **{quota.EXHAUSTED_KEY: {CLAUDE: 9999999999}})
    assert quota.exhausted_backends() == frozenset()
