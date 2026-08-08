"""Pinned behaviour of the runtime-state layer.

Characterisation, not specification: where the reference implementation does something
surprising, the test pins the surprise and `docs/FOUND_BUGS.md` records it. Nothing here
asserts what the code *should* do.
"""
import json
import re

import pytest

pytestmark = pytest.mark.maestro_module("state")


def test_now_iso_is_utc_zulu(subject):
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", subject.now_iso())


def test_now_iso_has_second_resolution_only(subject):
    assert "." not in subject.now_iso()


# --- read_json: swallows everything ------------------------------------------------


def test_read_json_missing_returns_empty_dict(subject, sandbox):
    assert subject.read_json(sandbox.repo / "nope.json") == {}


def test_read_json_malformed_returns_empty_dict(subject, sandbox):
    bad = sandbox.repo / "bad.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{not json", encoding="utf-8")
    assert subject.read_json(bad) == {}


def test_read_json_directory_returns_empty_dict(subject, sandbox):
    """FOUND_BUGS: a missing file, a corrupt file and a directory are indistinguishable."""
    assert subject.read_json(sandbox.orch_dir) == {}


def test_read_json_returns_parsed_content(subject, sandbox):
    good = sandbox.repo / "good.json"
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_text('{"a": 1}', encoding="utf-8")
    assert subject.read_json(good) == {"a": 1}


def test_read_json_returns_non_dict_json_unchanged(subject, sandbox):
    """FOUND_BUGS: annotated `-> dict` but a JSON list is returned as a list."""
    listy = sandbox.repo / "list.json"
    listy.parent.mkdir(parents=True, exist_ok=True)
    listy.write_text("[1, 2]", encoding="utf-8")
    assert subject.read_json(listy) == [1, 2]


# --- write_state / read_state -------------------------------------------------------


def test_write_state_then_read_state_roundtrips(subject, sandbox):
    subject.write_state({"version": "1", "in_flight": [{"task_id": "T1"}]})
    got = subject.read_state()
    assert got["in_flight"] == [{"task_id": "T1"}]
    assert got["version"] == "1"


def test_write_state_stamps_updated_at(subject, sandbox):
    subject.write_state({"version": "1"})
    assert "updated_at" in subject.read_state()


def test_write_state_mutates_the_callers_dict(subject, sandbox):
    """FOUND_BUGS: a `write_*` function is not a pure sink — it stamps the argument."""
    payload = {"version": "1"}
    subject.write_state(payload)
    assert "updated_at" in payload


def test_write_state_leaves_no_temp_file_behind(subject, sandbox):
    subject.write_state({"version": "1"})
    assert not list(sandbox.orch_dir.glob("*.tmp"))
    assert (sandbox.orch_dir / "state.json").exists()


def test_write_state_writes_indented_json_with_trailing_newline(subject, sandbox):
    subject.write_state({"version": "1"})
    text = (sandbox.orch_dir / "state.json").read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert "\n  " in text


def test_write_state_requires_an_existing_parent_directory(subject, sandbox, monkeypatch):
    """FOUND_BUGS: no mkdir — a fresh checkout raises rather than initialising itself."""
    monkeypatch.setattr(subject, "STATE_JSON", sandbox.repo / "missing" / "state.json")
    with pytest.raises(FileNotFoundError):
        subject.write_state({"version": "1"})


def test_read_state_raises_when_the_file_is_missing(subject, sandbox):
    """FOUND_BUGS: read_json swallows everything, read_state swallows nothing."""
    with pytest.raises(FileNotFoundError):
        subject.read_state()


def test_read_state_raises_on_malformed_json(subject, sandbox):
    (sandbox.orch_dir / "state.json").write_text("{nope", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        subject.read_state()


def test_read_state_injects_six_defaults(subject, sandbox):
    (sandbox.orch_dir / "state.json").write_text('{"version": "1"}', encoding="utf-8")
    got = subject.read_state()
    assert got["hitl_mode"] is False
    assert got["waiting_on_dan"] == {}
    assert got["dan_id_counter"] == 0
    assert got["retry_counts"] == {}
    assert got["parked_tasks"] == []
    assert got["launch_times"] == {}


def test_read_state_does_not_default_version_or_in_flight(subject, sandbox):
    """FOUND_BUGS: the two load-bearing keys are the two with no defaults."""
    (sandbox.orch_dir / "state.json").write_text("{}", encoding="utf-8")
    got = subject.read_state()
    assert "version" not in got
    assert "in_flight" not in got


def test_read_state_defaults_are_not_persisted(subject, sandbox):
    (sandbox.orch_dir / "state.json").write_text('{"version": "1"}', encoding="utf-8")
    subject.read_state()
    on_disk = json.loads((sandbox.orch_dir / "state.json").read_text(encoding="utf-8"))
    assert on_disk == {"version": "1"}


# --- _persist_launch_time -----------------------------------------------------------


def test_persist_launch_time_records_the_epoch(subject, sandbox):
    subject.write_state({"version": "1", "in_flight": []})
    subject._persist_launch_time("impl-T1-1", 1234.5)
    assert subject.read_state()["launch_times"] == {"impl-T1-1": 1234.5}


def test_persist_launch_time_keeps_existing_entries(subject, sandbox):
    subject.write_state({"version": "1", "launch_times": {"old": 1.0}})
    subject._persist_launch_time("new", 2.0)
    assert subject.read_state()["launch_times"] == {"old": 1.0, "new": 2.0}


def test_persist_launch_time_rereads_from_disk_and_discards_unsaved_edits(subject, sandbox):
    """FOUND_BUGS: read-modify-write with no locking silently drops concurrent changes."""
    subject.write_state({"version": "1", "in_flight": []})
    held = subject.read_state()
    held["in_flight"] = [{"task_id": "T1"}]  # never written
    subject._persist_launch_time("sid", 1.0)
    assert subject.read_state()["in_flight"] == []


# --- append_journal -----------------------------------------------------------------


def test_append_journal_writes_one_ndjson_line_per_call(subject, sandbox):
    subject.append_journal("started", "first")
    subject.append_journal("finished", "second", session_id="s1")
    lines = (sandbox.orch_dir / "journal.ndjson").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    first, second = json.loads(lines[0]), json.loads(lines[1])
    assert first["event"] == "started" and first["detail"] == "first"
    assert second["session_id"] == "s1"
    assert "ts" in first


def test_append_journal_record_has_exactly_five_fields(subject, sandbox):
    subject.append_journal("e", "d")
    record = json.loads((sandbox.orch_dir / "journal.ndjson").read_text(encoding="utf-8"))
    assert set(record) == {"ts", "event", "agent", "session_id", "detail"}


def test_append_journal_stamps_a_constant_agent(subject, sandbox):
    """The agent name is a hardcoded literal, identical on every record.

    Asserted as an invariant rather than as its value: the literal is vocabulary from the
    consuming project, and `tests/test_purity.py` exists to keep that out of maestro.
    """
    subject.append_journal("a", "1")
    subject.append_journal("b", "2")
    lines = (sandbox.orch_dir / "journal.ndjson").read_text(encoding="utf-8").strip().split("\n")
    agents = {json.loads(line)["agent"] for line in lines}
    assert len(agents) == 1
    assert agents.pop().strip()


def test_append_journal_defaults_session_id_to_empty_string(subject, sandbox):
    subject.append_journal("e", "d")
    record = json.loads((sandbox.orch_dir / "journal.ndjson").read_text(encoding="utf-8"))
    assert record["session_id"] == ""


def test_append_journal_creates_the_file_but_not_the_directory(subject, sandbox, monkeypatch):
    assert not (sandbox.orch_dir / "journal.ndjson").exists()
    subject.append_journal("e", "d")
    assert (sandbox.orch_dir / "journal.ndjson").exists()

    monkeypatch.setattr(subject, "JOURNAL", sandbox.repo / "missing" / "journal.ndjson")
    with pytest.raises(FileNotFoundError):
        subject.append_journal("e", "d")


def test_append_journal_writes_non_ascii_literally(subject, sandbox):
    subject.append_journal("e", "café")
    raw = (sandbox.orch_dir / "journal.ndjson").read_text(encoding="utf-8")
    assert "café" in raw
    assert "\\u" not in raw
