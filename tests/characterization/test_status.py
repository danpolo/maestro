"""Pinned field values of the `/status` report (M4c batch 1, R13).

This is *not* a byte-for-byte text-layout test — `docs/plans/2026-08-18-m4c-superseded-
sidecars.md` section 5 requires semantic field parity: phase id/title, `in_flight` task
ids, `parked_tasks`, `waiting_on_dan` ids, and the usage/quota numbers. Those are exactly
the fields pinned below, driven through `maestro/status.py` (see `_maestro_report`).

The retired legacy subject ran the reference `orchestrator_status.py` as a read-only
subprocess out of a fixture tree, since it was a standalone self-locating script rather
than an importable module.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

@pytest.fixture
def status_repo(tmp_path):
    """A fixture repo with empty `.orchestrator/` and `docs/` directories for the tests
    to seed. Previously it also held a copy of the reference `orchestrator_status.py`,
    for the retired legacy subject to run as a subprocess.
    """
    repo = tmp_path / "repo"
    (repo / ".orchestrator").mkdir(parents=True)
    (repo / "docs").mkdir()
    return repo


# --- fixture data --------------------------------------------------------------------
#
# Shapes transcribed from the real project's `.orchestrator/state.json` /
# `usage.json` (field names and nesting only — no live values, no credentials).

def _write_state(repo: Path, **overrides) -> dict:
    state = {
        "phase": {"id": "M4C-B1", "title": "Pin the status report's fields",
                   "status": "in_progress"},
        "halted": False,
        "paused_until": None,
        "paused_by_user": False,
        "blocked_on": [],
        "queue_ref": "docs/ROADMAP.md",
        "updated_at": "2026-08-19T12:00:00Z",
        "in_flight": [
            {"status": "running", "task_id": "T7", "role": "implementer",
             "started_at": "2026-08-19T11:00:00Z", "window": "impl-T7"},
        ],
        "parked_tasks": ["T3"],
        "waiting_on_dan": {
            "7": {"task_id": "T5", "session_id": "seed-b14-T5", "branch": "",
                  "worktree": "", "parked_at": "2026-08-19T09:00:00Z",
                  "kind": "manual-action", "summary": "do the thing"},
        },
    }
    state.update(overrides)
    (repo / ".orchestrator" / "state.json").write_text(json.dumps(state))
    return state


def _write_usage(repo: Path, **overrides) -> dict:
    usage = {
        "updated_at": "2026-08-19T12:05:00Z",
        "model": "claude-sonnet-4-5",
        "context_used_pct": 42.3,
        "five_hour": {"used_pct": 61.0, "resets_at": 1755640800},
        "seven_day": {"used_pct": 18.4, "resets_at": 1756177200},
    }
    usage.update(overrides)
    (repo / ".orchestrator" / "usage.json").write_text(json.dumps(usage))
    return usage


def _write_journal(repo: Path) -> None:
    lines = [
        {"ts": "2026-08-19T12:04:00Z", "event": "task_launched", "agent": "orchestrator",
         "detail": "T7 dispatched"},
        {"ts": "2026-08-19T12:06:00Z", "event": "usage_polled", "agent": "orchestrator",
         "detail": "5h=61.0%"},
    ]
    (repo / ".orchestrator" / "journal.ndjson").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n"
    )


def _write_roadmap(repo: Path) -> None:
    (repo / "docs" / "ROADMAP.md").write_text(
        "# ROADMAP\n\n"
        "```yaml\n"
        "id: T7\n"
        "title: In-flight task\n"
        "mode: autonomous\n"
        "deps: []\n"
        "```\n\n"
        "```yaml\n"
        "id: T8\n"
        "title: Ready task\n"
        "mode: autonomous\n"
        "deps: []\n"
        "```\n\n"
        "```yaml\n"
        "id: T9\n"
        "title: Needs Dan\n"
        "mode: needs-dan\n"
        "deps: [T8]\n"
        "```\n"
    )


def _seed(repo: Path, **state_overrides) -> dict:
    """Seed a realistic fixture (state + usage + journal + roadmap) and return the
    state dict written, so tests can assert against it without re-deriving it."""
    state = _write_state(repo, **state_overrides)
    _write_usage(repo)
    _write_journal(repo)
    _write_roadmap(repo)
    return state


def _maestro_report(repo: Path, monkeypatch, as_json: bool = True):
    """The `maestro.status` equivalent (R13). `maestro.status` binds its path globals
    once, at import time, off `$MAESTRO_REPO` — the same "one project per process"
    convention every extracted module follows (see `maestro/cli.py`'s module docstring).
    This file does not use the shared `tests/characterization/conftest.py` `subject`/
    `sandbox` fixtures (see the module docstring), so there is no reflection-based
    rebasing here — instead we monkeypatch the module's four path globals directly onto
    `repo`, the same targets `status_repo` already seeded, then drive the real
    `run_cli`/`get_status_report` seam (`maestro/status.py`) so the in-process wiring
    itself is exercised, not just its constituent functions."""
    from maestro import status as maestro_status

    monkeypatch.setattr(maestro_status, "STATE_JSON", repo / ".orchestrator" / "state.json")
    monkeypatch.setattr(maestro_status, "USAGE_JSON", repo / ".orchestrator" / "usage.json")
    monkeypatch.setattr(maestro_status, "JOURNAL", repo / ".orchestrator" / "journal.ndjson")
    monkeypatch.setattr(maestro_status, "ROADMAP", repo / "docs" / "ROADMAP.md")

    if as_json:
        return maestro_status.get_status_report()
    _rc, output = maestro_status.run_cli([])
    return output


@pytest.fixture
def status_source(status_repo, monkeypatch):
    """Callable `report(as_json=True) -> dict` driving the real `maestro.status` module (R13)."""
    return lambda as_json=True: _maestro_report(status_repo, monkeypatch, as_json=as_json)


# --- phase id / title -----------------------------------------------------------------

def test_phase_id_and_title(status_repo, status_source):
    _seed(status_repo)

    payload = status_source(as_json=True)

    phase = payload["state"]["phase"]
    assert phase["id"] == "M4C-B1"
    assert phase["title"] == "Pin the status report's fields"
    assert phase["status"] == "in_progress"


def test_phase_missing_reads_as_empty_dict_not_a_crash(status_repo, status_source):
    _seed(status_repo, phase={})

    payload = status_source(as_json=True)

    assert payload["state"]["phase"] == {}


# --- in_flight -------------------------------------------------------------------------

def test_in_flight_task_ids(status_repo, status_source):
    _seed(status_repo)

    payload = status_source(as_json=True)

    ids = [row["task_id"] for row in payload["state"]["in_flight"]]
    assert ids == ["T7"]


def test_in_flight_empty_list_when_nothing_running(status_repo, status_source):
    _seed(status_repo, in_flight=[])

    payload = status_source(as_json=True)

    assert payload["state"]["in_flight"] == []


def test_in_flight_multiple_entries_preserve_order(status_repo, status_source):
    _seed(status_repo, in_flight=[
        {"status": "running", "task_id": "T1", "role": "implementer",
         "started_at": "2026-08-19T10:00:00Z", "window": "impl-T1"},
        {"status": "running", "task_id": "T2", "role": "eval", "started_at": "2026-08-19T10:30:00Z",
         "window": "impl-T2"},
    ])

    payload = status_source(as_json=True)

    ids = [row["task_id"] for row in payload["state"]["in_flight"]]
    assert ids == ["T1", "T2"]


# --- parked_tasks -----------------------------------------------------------------------

def test_parked_tasks_ids(status_repo, status_source):
    _seed(status_repo)

    payload = status_source(as_json=True)

    assert payload["state"]["parked_tasks"] == ["T3"]


def test_parked_tasks_empty_when_none_parked(status_repo, status_source):
    _seed(status_repo, parked_tasks=[])

    payload = status_source(as_json=True)

    assert payload["state"]["parked_tasks"] == []


# --- waiting_on_dan ----------------------------------------------------------------------

def test_waiting_on_dan_task_ids(status_repo, status_source):
    _seed(status_repo)

    payload = status_source(as_json=True)

    ids = {entry["task_id"] for entry in payload["state"]["waiting_on_dan"].values()}
    assert ids == {"T5"}


def test_waiting_on_dan_empty_when_nothing_parked_for_dan(status_repo, status_source):
    _seed(status_repo, waiting_on_dan={})

    payload = status_source(as_json=True)

    assert payload["state"]["waiting_on_dan"] == {}


def test_waiting_on_dan_multiple_entries(status_repo, status_source):
    _seed(status_repo, waiting_on_dan={
        "7": {"task_id": "T5", "session_id": "seed-b14-T5", "branch": "", "worktree": "",
              "parked_at": "2026-08-19T09:00:00Z", "kind": "manual-action", "summary": "a"},
        "8": {"task_id": "T6", "session_id": "seed-b14-T6", "branch": "", "worktree": "",
              "parked_at": "2026-08-19T09:05:00Z", "kind": "manual-action", "summary": "b"},
    })

    payload = status_source(as_json=True)

    ids = {entry["task_id"] for entry in payload["state"]["waiting_on_dan"].values()}
    assert ids == {"T5", "T6"}


# --- usage / quota numbers -----------------------------------------------------------------

def test_usage_model_and_context_pct(status_repo, status_source):
    _seed(status_repo)

    payload = status_source(as_json=True)

    usage = payload["usage"]
    assert usage["model"] == "claude-sonnet-4-5"
    assert usage["context_used_pct"] == 42.3


def test_usage_five_hour_limit_pct_and_reset(status_repo, status_source):
    _seed(status_repo)

    payload = status_source(as_json=True)

    five_hour = payload["usage"]["five_hour"]
    assert five_hour["used_pct"] == 61.0
    assert five_hour["resets_at"] == 1755640800


def test_usage_seven_day_limit_pct_and_reset(status_repo, status_source):
    _seed(status_repo)

    payload = status_source(as_json=True)

    seven_day = payload["usage"]["seven_day"]
    assert seven_day["used_pct"] == 18.4
    assert seven_day["resets_at"] == 1756177200


def test_usage_missing_file_reads_as_none_not_a_crash(status_repo, status_source):
    _seed(status_repo)
    (status_repo / ".orchestrator" / "usage.json").unlink()

    payload = status_source(as_json=True)

    assert payload["usage"] is None


# --- state.json missing entirely -------------------------------------------------------

def test_state_missing_reads_as_none_not_a_crash(status_repo, status_source):
    # No _seed() call: state.json is never written. usage/roadmap are, so only the
    # state half of the report is exercised under absence.
    _write_usage(status_repo)
    _write_journal(status_repo)
    _write_roadmap(status_repo)

    payload = status_source(as_json=True)

    assert payload["state"] is None
