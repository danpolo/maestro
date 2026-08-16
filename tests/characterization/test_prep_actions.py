"""Pinned behaviour of the prepared-action sidecar: the one human action held per task.

Characterisation, not specification: where the reference implementation does something
surprising, the test pins the surprise and `docs/FOUND_BUGS.md` records it. Nothing here
asserts what the code *should* do.

Two conventions are specific to this module:

* the sidecar is a **separate module** in the reference (`scripts/prepared_actions.py`,
  imported by the orchestrator as `prep_actions`), so the dual `subject` fixture hands
  back the flat orchestrator namespace for the legacy subject and the sidecar itself for
  the maestro one. The `actions` fixture normalises that to "the module that owns
  `load_actions`" for both;
* the `sandbox` fixture cannot help here — it rebases path globals off a module's `REPO`,
  and the reference sidecar has none, so an unstubbed call would read and write a live
  repo's `.orchestrator/prepared_actions.json`. Every test therefore takes the `store`
  fixture, which repoints `PREPARED_ACTIONS_FILE` into a temp tree before anything runs.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.maestro_module("prep_actions")


@pytest.fixture
def actions(subject):
    """The module that owns the sidecar functions, whichever subject is under test.

    The reference orchestrator is one flat namespace that does
    `import prepared_actions as prep_actions`; in maestro the sidecar *is* the subject.
    """
    return getattr(subject, "prep_actions", subject)


@pytest.fixture
def store(actions, tmp_path, monkeypatch) -> Path:
    """Repoint the subject's one path global into a temp tree. Never touches a real repo."""
    path = tmp_path / "repo" / ".orchestrator" / "prepared_actions.json"
    monkeypatch.setattr(actions, "PREPARED_ACTIONS_FILE", path)
    assert not path.exists()
    return path


def _seed(path: Path, mapping) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mapping), encoding="utf-8")
    return path


def _entry(action: str = "open the notebook") -> dict:
    return {"action": action, "post_action_cmd": None, "prepared_at": "x", "dan_id": None}


# --- the default path and the explicit override ----------------------------------------


def test_default_path_is_the_module_global(actions, store):
    actions.set_action("T1", "run the script")
    assert store.exists()
    assert list(json.loads(store.read_text(encoding="utf-8"))) == ["T1"]


def test_explicit_path_overrides_the_global(actions, store, tmp_path):
    other = tmp_path / "elsewhere" / "prepared_actions.json"
    actions.set_action("T1", "run the script", path=other)
    assert other.exists()
    assert not store.exists()
    assert actions.get_action("T1", other) == actions.load_actions(other)["T1"]
    assert actions.get_action("T1") is None


# --- load_actions: swallows everything --------------------------------------------------


def test_load_actions_missing_file_returns_empty_dict(actions, store):
    assert actions.load_actions() == {}


def test_load_actions_corrupt_json_returns_empty_dict(actions, store):
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("{not json", encoding="utf-8")
    assert actions.load_actions() == {}


def test_load_actions_directory_returns_empty_dict(actions, store):
    """FOUND_BUGS: a missing file, a corrupt file and a directory are indistinguishable."""
    store.mkdir(parents=True)
    assert actions.load_actions() == {}


def test_load_actions_returns_the_stored_map(actions, store):
    _seed(store, {"T1": _entry(), "T2": _entry("call the API")})
    got = actions.load_actions()
    assert sorted(got) == ["T1", "T2"]
    assert got["T2"]["action"] == "call the API"


def test_load_actions_returns_non_dict_json_unchanged(actions, store):
    """FOUND_BUGS: annotated `-> dict[str, dict]` but a JSON list is returned as a list."""
    _seed(store, ["T1", "T2"])
    assert actions.load_actions() == ["T1", "T2"]


# --- get_action -------------------------------------------------------------------------


def test_get_action_returns_the_entry(actions, store):
    _seed(store, {"T1": _entry(), "T2": _entry("call the API")})
    assert actions.get_action("T1")["action"] == "open the notebook"


def test_get_action_absent_returns_none(actions, store):
    _seed(store, {"T1": _entry()})
    assert actions.get_action("T2") is None


def test_get_action_missing_file_returns_none(actions, store):
    assert actions.get_action("T1") is None


def test_get_action_raises_attribute_error_on_a_json_list_file(actions, store):
    """FOUND_BUGS: the "never raises" contract holds only while the JSON is an object."""
    _seed(store, ["T1"])
    with pytest.raises(AttributeError):
        actions.get_action("T1")


# --- has_action -------------------------------------------------------------------------


def test_has_action_true_when_present(actions, store):
    _seed(store, {"T1": _entry()})
    assert actions.has_action("T1") is True


def test_has_action_false_when_absent(actions, store):
    _seed(store, {"T1": _entry()})
    assert actions.has_action("T2") is False


def test_has_action_missing_file_is_false(actions, store):
    assert actions.has_action("T1") is False


# --- set_action -------------------------------------------------------------------------


def test_set_action_returns_an_entry_with_exactly_four_fields(actions, store):
    entry = actions.set_action("T1", "run the script", post_action_cmd="make check", dan_id=7)
    assert set(entry) == {"action", "post_action_cmd", "prepared_at", "dan_id"}
    assert entry["action"] == "run the script"
    assert entry["post_action_cmd"] == "make check"
    assert entry["dan_id"] == 7


def test_set_action_persists_the_returned_entry_verbatim(actions, store):
    entry = actions.set_action("T1", "run the script", post_action_cmd="make check", dan_id=7)
    assert json.loads(store.read_text(encoding="utf-8")) == {"T1": entry}


def test_set_action_creates_the_missing_parent_directory(actions, store):
    assert not store.parent.exists()
    actions.set_action("T1", "run the script")
    assert store.parent.is_dir()


def test_set_action_defaults_post_action_cmd_and_dan_id_to_none(actions, store):
    entry = actions.set_action("T1", "run the script")
    assert entry["post_action_cmd"] is None
    assert entry["dan_id"] is None


def test_set_action_overwrites_an_existing_entry(actions, store):
    actions.set_action("T1", "first", post_action_cmd="make check", dan_id=1)
    actions.set_action("T1", "second")
    stored = json.loads(store.read_text(encoding="utf-8"))
    assert list(stored) == ["T1"]
    assert stored["T1"]["action"] == "second"
    assert stored["T1"]["post_action_cmd"] is None
    assert stored["T1"]["dan_id"] is None


def test_set_action_keeps_the_other_entries(actions, store):
    _seed(store, {"T1": _entry()})
    actions.set_action("T2", "run the script")
    assert sorted(json.loads(store.read_text(encoding="utf-8"))) == ["T1", "T2"]


def test_set_action_leaves_no_temp_file_behind(actions, store):
    actions.set_action("T1", "run the script")
    assert not list(store.parent.glob("*.tmp"))
    assert store.exists()


def test_set_action_writes_indented_json_with_no_trailing_newline(actions, store):
    actions.set_action("T1", "run the script")
    text = store.read_text(encoding="utf-8")
    assert not text.endswith("\n")
    assert "\n  " in text


def test_set_action_prepared_at_is_offset_isoformat_not_the_zulu_stamp(actions, store):
    """FOUND_BUGS: a second `.orchestrator/` timestamp format, unlike `now_iso`'s Zulu."""
    entry = actions.set_action("T1", "run the script")
    stamp = entry["prepared_at"]
    assert stamp.endswith("+00:00")
    assert not stamp.endswith("Z")
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(None)


def test_set_action_silently_discards_a_corrupt_store(actions, store):
    """FOUND_BUGS: every other prepared action is lost, with no error and no journal line."""
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("{not json", encoding="utf-8")
    actions.set_action("T2", "run the script")
    assert list(json.loads(store.read_text(encoding="utf-8"))) == ["T2"]


# --- remove_action ----------------------------------------------------------------------


def test_remove_action_removes_the_entry_and_returns_true(actions, store):
    _seed(store, {"T1": _entry()})
    assert actions.remove_action("T1") is True
    assert json.loads(store.read_text(encoding="utf-8")) == {}


def test_remove_action_absent_entry_returns_false_and_leaves_the_file(actions, store):
    _seed(store, {"T1": _entry()})
    before = store.read_text(encoding="utf-8")
    assert actions.remove_action("T2") is False
    assert store.read_text(encoding="utf-8") == before


def test_remove_action_missing_file_returns_false_and_creates_nothing(actions, store):
    assert actions.remove_action("T1") is False
    assert not store.exists()
    assert not store.parent.exists()


def test_remove_action_keeps_the_other_entries(actions, store):
    _seed(store, {"T1": _entry(), "T2": _entry("call the API")})
    actions.remove_action("T1")
    assert list(json.loads(store.read_text(encoding="utf-8"))) == ["T2"]


def test_remove_action_leaves_no_temp_file_behind(actions, store):
    _seed(store, {"T1": _entry()})
    actions.remove_action("T1")
    assert not list(store.parent.glob("*.tmp"))
