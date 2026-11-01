"""Legacy shadowing and export: `state.json` and the journal become projections (`[P1]`).

The migration has to hold in both directions at once, and each direction has its own
failure mode:

* A project with no control database must behave *exactly* as it did before P01. The
  characterisation suite owns that claim in detail; the tests here pin the seam itself —
  that the shadow code does not run and nothing new appears on disk.
* A project with one must treat SQLite as authority: every legacy write goes through the
  event store first, the exported file is stamped with the sequence it projects, and an
  edit made behind the controller's back does not survive the next read.
"""
from __future__ import annotations

import json

import pytest

from maestro import state as state_module
from maestro.control.models import EventType
from maestro.control.store import ControlStore, enqueue_external_command


@pytest.fixture
def legacy(monkeypatch, repo):
    """`maestro.state` rebased onto a temporary repo with no control database."""
    orch = repo / ".orchestrator"
    monkeypatch.setattr(state_module, "STATE_JSON", orch / "state.json")
    monkeypatch.setattr(state_module, "JOURNAL", orch / "journal.ndjson")
    monkeypatch.setattr(state_module, "CONTROL_DB", orch / "control.sqlite3")
    state_module._reset_control_store()
    yield state_module
    state_module._reset_control_store()


@pytest.fixture
def migrated(legacy, db_path):
    """The same module once the project has been migrated onto the control store."""
    ControlStore.open_controller(db_path).close()
    legacy._reset_control_store()
    return legacy


# --- an un-migrated project is untouched --------------------------------------------------


def test_without_a_control_database_nothing_shadows(legacy):
    assert legacy.control_store() is None
    legacy.write_state({"version": "1"})
    legacy.append_journal("started", "first")

    assert json.loads(legacy.STATE_JSON.read_text()) == {
        "version": "1", "updated_at": legacy.read_state()["updated_at"],
    }
    assert not (legacy.STATE_JSON.parent / "control.sqlite3").exists()


def test_without_a_control_database_state_json_carries_no_sequence(legacy):
    legacy.write_state({"version": "1"})
    assert legacy.CONTROL_SEQUENCE_KEY not in json.loads(legacy.STATE_JSON.read_text())


# --- with a control database, SQLite is the authority ----------------------------------------


def test_a_state_write_is_committed_as_an_event_before_it_is_exported(migrated):
    migrated.write_state({"version": "1", "in_flight": []})
    events = migrated.control_store().events(aggregate_id="legacy_state")
    assert [e.type for e in events] == [EventType.LEGACY_STATE_WRITTEN]
    assert events[0].payload["document"]["version"] == "1"


def test_the_exported_state_file_is_stamped_with_its_sequence(migrated):
    migrated.write_state({"version": "1"})
    store = migrated.control_store()
    on_disk = json.loads(migrated.STATE_JSON.read_text())
    assert on_disk[migrated.CONTROL_SEQUENCE_KEY] == store.head_sequence()
    assert store.exported_sequence("state.json") == store.head_sequence()


def test_the_stamp_advances_with_every_write(migrated):
    migrated.write_state({"version": "1"})
    first = json.loads(migrated.STATE_JSON.read_text())[migrated.CONTROL_SEQUENCE_KEY]
    migrated.write_state({"version": "2"})
    second = json.loads(migrated.STATE_JSON.read_text())[migrated.CONTROL_SEQUENCE_KEY]
    assert second > first


def test_reading_back_a_written_document_still_roundtrips(migrated):
    migrated.write_state({"version": "1", "in_flight": [{"task_id": "T1"}]})
    got = migrated.read_state()
    assert got["in_flight"] == [{"task_id": "T1"}]
    assert got["version"] == "1"


def test_the_sequence_stamp_is_not_written_back_into_the_authority(migrated):
    """A round trip must not smuggle the projection's stamp into the next document."""
    migrated.write_state({"version": "1"})
    migrated.write_state(migrated.read_state())
    document = migrated.control_store().events(aggregate_id="legacy_state")[-1].payload["document"]
    assert migrated.CONTROL_SEQUENCE_KEY not in document


# --- legacy files cannot independently overwrite SQLite authority -------------------------------


def test_an_outside_edit_to_state_json_does_not_survive_a_read(migrated):
    migrated.write_state({"version": "1", "halted": False})
    migrated.STATE_JSON.write_text(json.dumps({"version": "1", "halted": True}))

    assert migrated.read_state()["halted"] is False


def test_a_deleted_state_file_is_re_exported_from_the_event_store(migrated):
    migrated.write_state({"version": "1", "phase": {"id": "P3"}})
    migrated.STATE_JSON.unlink()

    assert migrated.read_state()["phase"] == {"id": "P3"}


def test_a_corrupt_state_file_is_replaced_rather_than_raising(migrated):
    migrated.write_state({"version": "1"})
    migrated.STATE_JSON.write_text("{not json")
    assert migrated.read_state()["version"] == "1"


def test_a_forged_sequence_stamp_does_not_make_an_edit_authoritative(migrated):
    migrated.write_state({"version": "1", "halted": False})
    store = migrated.control_store()
    forged = {"version": "1", "halted": True, migrated.CONTROL_SEQUENCE_KEY: 999}
    migrated.STATE_JSON.write_text(json.dumps(forged))

    assert migrated.read_state()["halted"] is False
    assert json.loads(migrated.STATE_JSON.read_text())[migrated.CONTROL_SEQUENCE_KEY] == (
        store.exported_sequence("state.json")
    )


def test_a_projection_is_restored_before_any_default_is_injected(migrated):
    """`read_state`'s defaults must not paper over a lost projection."""
    migrated.write_state({"version": "1", "parked_tasks": ["T9"]})
    migrated.STATE_JSON.unlink()
    assert migrated.read_state()["parked_tasks"] == ["T9"]


# --- the journal shadows without changing its own shape -------------------------------------------


def test_a_journal_line_shadows_into_the_event_store(migrated):
    migrated.append_journal("task_complete", "T1 done", session_id="impl-T1-1")
    event = migrated.control_store().events(aggregate_id="legacy_journal")[-1]
    assert event.type == EventType.LEGACY_JOURNAL_APPENDED
    assert event.payload["event"] == "task_complete"
    assert event.run_id == "impl-T1-1"


def test_the_journal_line_itself_keeps_exactly_five_fields(migrated):
    migrated.append_journal("e", "d")
    record = json.loads(migrated.JOURNAL.read_text())
    assert set(record) == {"ts", "event", "agent", "session_id", "detail"}


# --- external actors reach the control plane only through the inbox ---------------------------------


def test_the_watchdog_enqueues_instead_of_writing_state(monkeypatch, repo, db_path):
    from maestro import watchdog

    ControlStore.open_controller(db_path).close()
    monkeypatch.setattr(watchdog, "STATE_JSON", repo / ".orchestrator" / "state.json")
    monkeypatch.setenv("MAESTRO_REPO", str(repo))

    assert watchdog.request_state_change("clear_pause", {"paused_until": 12.0}, "clear_pause:12.0")

    store = ControlStore.open_client(db_path)
    try:
        pending = store.pending_commands()
        assert [c.command_type for c in pending] == ["clear_pause"]
        assert pending[0].payload == {"paused_until": 12.0}
    finally:
        store.close()


def test_the_watchdog_keeps_its_legacy_write_when_there_is_no_control_plane(monkeypatch, repo):
    from maestro import watchdog

    monkeypatch.setenv("MAESTRO_REPO", str(repo))
    assert not watchdog.request_state_change("clear_pause", {}, "clear_pause:none")


def test_the_watchdog_re_observing_a_condition_enqueues_it_once(monkeypatch, repo, db_path):
    from maestro import watchdog

    ControlStore.open_controller(db_path).close()
    monkeypatch.setenv("MAESTRO_REPO", str(repo))
    for _ in range(3):
        watchdog.request_state_change("clear_blocked_on", {"blocked_on": ["R1"]}, "clear_blocked_on:R1")

    store = ControlStore.open_client(db_path)
    try:
        assert len(store.pending_commands()) == 1
    finally:
        store.close()


def test_a_telegram_update_is_recorded_then_settled(migrated, db_path):
    from maestro.hitl import commands

    command_id = commands._ingest_control_command(4242, "/pause")
    store = migrated.control_store()
    assert store.command(command_id).status == "processing"

    commands._settle_control_command(command_id)
    assert store.command(command_id).status == "committed"
    assert store.command(command_id).payload == {"update_id": 4242, "text": "/pause"}


def test_a_redelivered_telegram_update_is_skipped(migrated):
    from maestro.hitl import commands

    commands._settle_control_command(commands._ingest_control_command(4242, "/approve T1"))
    assert commands._ingest_control_command(4242, "/approve T1") is commands._ALREADY_PROCESSED


def test_a_telegram_verb_that_raised_is_never_re_executed(migrated):
    """It stays `processing`: an unprobed side effect must not retry itself (`[A05]`)."""
    from maestro.hitl import commands

    commands._ingest_control_command(99, "/redo T1 rewrite")      # never settled
    assert commands._ingest_control_command(99, "/redo T1 rewrite") is commands._ALREADY_PROCESSED


def test_without_a_control_plane_telegram_records_nothing(legacy):
    from maestro.hitl import commands

    assert commands._ingest_control_command(1, "/pause") is None
    commands._settle_control_command(None)       # must not raise


def test_an_external_enqueue_is_a_no_op_when_the_project_has_no_control_database(monkeypatch, repo):
    monkeypatch.setenv("MAESTRO_REPO", str(repo))
    assert enqueue_external_command("clear_pause", {}, idempotency_key="k") is None
