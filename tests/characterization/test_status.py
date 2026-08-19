"""Pinned field values of the reference `/status` report (M4c batch 1, R13).

`orchestrator_status.py` is a standalone script, not a module `orchestrator_run.py`
functions live in — it computes its own `REPO_ROOT` from `Path(__file__).resolve()
.parent.parent` and is meant to be *run*, not imported. So unlike the rest of
`tests/characterization`, the reference side here is exercised as a read-only
subprocess: a copy of the script (never the live one) is placed at
`<fixture>/scripts/orchestrator_status.py` so its self-located `REPO_ROOT` resolves to
the fixture tree, then invoked with `--json`.

This is *not* a byte-for-byte text-layout test — `docs/plans/2026-08-18-m4c-superseded-
sidecars.md` section 5 requires semantic field parity: phase id/title, `in_flight` task
ids, `parked_tasks`, `waiting_on_dan` ids, and the usage/quota numbers. Those are exactly
the fields pinned below. `maestro/status.py` does not exist yet (M4c batch 1 Wave B) —
every case's maestro side skips until it lands; this file exists to give that future
module a fixed target.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.reference_repo import LEGACY_REPO

REFERENCE_SCRIPT = "orchestrator_status.py"


def _reference_script_path() -> Path | None:
    if LEGACY_REPO is None:
        return None
    path = LEGACY_REPO / "scripts" / REFERENCE_SCRIPT
    return path if path.is_file() else None


@pytest.fixture
def status_repo(tmp_path):
    """A fixture repo laid out exactly as the reference script expects: its own
    `scripts/orchestrator_status.py` (a copy, never the live file — the reference repo
    is read-only) plus empty `.orchestrator/` and `docs/` directories for the tests to
    seed. `REPO_ROOT` inside the copied script resolves to this tree, not the real one.
    """
    script = _reference_script_path()
    if script is None:
        pytest.skip(
            "reference repo unknown or orchestrator_status.py missing: set "
            "$MAESTRO_LEGACY_REPO or write its path to .legacy_repo"
        )
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / ".orchestrator").mkdir()
    (repo / "docs").mkdir()
    shutil.copy2(script, repo / "scripts" / REFERENCE_SCRIPT)
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


# --- invoking the reference as a subprocess -------------------------------------------

def _run_reference(repo: Path, *, args: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [sys.executable, str(repo / "scripts" / REFERENCE_SCRIPT), *args],
        capture_output=True, text=True, cwd=str(repo), timeout=30,
    )
    assert result.returncode == 0, (
        f"orchestrator_status.py exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


def _run_reference_json(repo: Path) -> dict:
    return json.loads(_run_reference(repo, args=["--json"]).stdout)


def _run_reference_text(repo: Path) -> str:
    return _run_reference(repo, args=[]).stdout


def _maestro_report(repo: Path) -> dict:
    """The `maestro.status` equivalent, once M4c batch 1 Wave B extracts it. Every
    call here skips today — `maestro/status.py` does not exist — so this pins only
    the reference's values above for that future module to reproduce."""
    pytest.importorskip("maestro.status", reason="maestro.status not extracted yet")
    raise AssertionError(  # pragma: no cover
        "maestro.status now exists — wire this helper to its real entry point"
    )


@pytest.fixture(params=["legacy", "maestro"])
def status_source(request, status_repo):
    """Callable `report(as_json=True) -> dict` for either the reference script (a real
    subprocess, read-only) or `maestro.status` (skips — not extracted yet)."""
    if request.param == "maestro":
        return lambda as_json=True: _maestro_report(status_repo)

    def _report(as_json: bool = True):
        return _run_reference_json(status_repo) if as_json else _run_reference_text(status_repo)

    return _report


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
