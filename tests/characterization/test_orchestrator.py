"""Pinned behaviour of the orchestrator event loop and the helpers it drives each poll.

Characterisation, not specification. Nothing here launches an agent, opens a tmux window,
creates a worktree or touches the network:

* every test that can reach a shell replaces the *subject's* ``subprocess`` reference with
  a recorder (``_fake_subprocess``) that returns rc=0 and records argv/shell strings; the
  recorder proxies the real module for ``TimeoutExpired`` / ``CalledProcessError`` so the
  reference's own ``except`` clauses still match;
* ``time`` is replaced the same way so ``time.sleep`` is a no-op and the launch clock is
  controllable, while ``time.time`` / ``time.monotonic`` keep working;
* ``tmux_window_exists`` / ``_kill_tmux_window`` / ``notify_telegram`` / ``create_worktree``
  / ``launch_implementer`` / ``park_failed`` are stubbed per test.

The module's real outputs are ``state.json`` and ``journal.ndjson``, so that is what almost
every assertion reads. Two module-level *mutable* globals leak between tests —
``_reconcile_zombie_polls`` (the zombie grace counter) and ``_last_idle_heartbeat`` (the
heartbeat throttle) — and the ``fresh_globals`` autouse fixture resets both; without it the
order tests run in would change their results.

``main`` has a cyclomatic complexity of 249 and is deliberately *not* branch-covered here.
It is pinned only at its guards (no tmux session, ``halted``, HALT sentinel) and by driving
exactly one poll cycle over an empty queue, asserting the journal that cycle emits.
"""
from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.maestro_module("orchestrator")


# --- locating the subject's paths ------------------------------------------------------


def _path(subject, name: str, fallback: Path) -> Path:
    """A path global off the subject, or `fallback` when the subject does not own it."""
    value = getattr(subject, name, None)
    return value if isinstance(value, Path) else fallback


def _state_json(subject, sandbox) -> Path:
    return _path(subject, "STATE_JSON", sandbox.orch_dir / "state.json")


def _journal_path(subject, sandbox) -> Path:
    return _path(subject, "JOURNAL", sandbox.orch_dir / "journal.ndjson")


def _workspaces(subject, sandbox) -> Path:
    return _path(subject, "WORKSPACES", sandbox.workspaces)


def _questions_dir(subject, sandbox) -> Path:
    return _path(subject, "QUESTIONS_DIR", sandbox.orch_dir / "questions")


def _roadmap(subject, sandbox) -> Path:
    return _path(subject, "ROADMAP_FILE", sandbox.repo / "docs" / "ROADMAP.md")


def _halt_file(subject, sandbox) -> Path:
    return _path(subject, "HALT_FILE", sandbox.orch_dir / "HALT")


# --- state / journal helpers -----------------------------------------------------------


def _put_state(subject, sandbox, **fields) -> Path:
    path = _state_json(subject, sandbox)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


def _get_state(subject, sandbox) -> dict:
    return json.loads(_state_json(subject, sandbox).read_text(encoding="utf-8"))


def _records(subject, sandbox) -> list[dict]:
    path = _journal_path(subject, sandbox)
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _events(subject, sandbox) -> list[str]:
    return [r["event"] for r in _records(subject, sandbox)]


def _detail(subject, sandbox, event: str) -> str:
    for record in _records(subject, sandbox):
        if record["event"] == event:
            return record["detail"]
    raise AssertionError(f"no {event!r} record in {_events(subject, sandbox)}")


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago_iso(hours: float) -> str:
    moment = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# --- fakes -----------------------------------------------------------------------------


class _Proxy:
    """A stand-in module: named attributes are overridden, everything else proxies `real`."""

    def __init__(self, real, **overrides):
        self._real = real
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _Runs:
    """Records every call; answers from `handler` or with rc=0."""

    def __init__(self, handler=None):
        self.calls: list[SimpleNamespace] = []
        self._handler = handler

    def __call__(self, *args, **kwargs):
        self.calls.append(SimpleNamespace(args=args, kwargs=dict(kwargs)))
        if self._handler is not None:
            return self._handler(args, kwargs)
        return subprocess.CompletedProcess(args[0] if args else [], 0, "", "")

    @property
    def argvs(self) -> list:
        return [c.args[0] for c in self.calls]


def _fake_subprocess(monkeypatch, subject, handler=None) -> _Runs:
    """Swap the *subject's* `subprocess` reference. Real exception classes still resolve."""
    runs = _Runs(handler)
    monkeypatch.setattr(subject, "subprocess", _Proxy(subprocess, run=runs))
    return runs


def _fake_time(monkeypatch, subject, now: float | None = None) -> SimpleNamespace:
    """Swap the subject's `time` so sleep is a no-op and `time.time()` is pinnable."""
    clock = SimpleNamespace(value=now if now is not None else time.time(), sleeps=[])
    monkeypatch.setattr(
        subject,
        "time",
        _Proxy(time, sleep=clock.sleeps.append, time=lambda: clock.value),
    )
    return clock


def _telegram(monkeypatch, subject) -> list[str]:
    sent: list[str] = []
    monkeypatch.setattr(subject, "notify_telegram", sent.append)
    return sent


@pytest.fixture(autouse=True)
def fresh_globals(subject, monkeypatch):
    """Reset the two module-level mutable globals so test order cannot change a result."""
    if hasattr(subject, "_reconcile_zombie_polls"):
        monkeypatch.setattr(subject, "_reconcile_zombie_polls", {})
    if hasattr(subject, "_last_idle_heartbeat"):
        monkeypatch.setattr(subject, "_last_idle_heartbeat", 0.0)


@pytest.fixture
def tmux(subject, monkeypatch):
    """Controllable tmux: `tmux.alive` is the set of window names that exist."""
    box = SimpleNamespace(alive=set(), killed=[])
    monkeypatch.setattr(subject, "tmux_window_exists", lambda w: w in box.alive)
    monkeypatch.setattr(subject, "_kill_tmux_window", box.killed.append)
    return box


def _entry(sid="impl-T1-1", task_id="T1", window="impl-T1", **extra) -> dict:
    entry = {
        "session_id": sid,
        "task_id": task_id,
        "window": window,
        "branch": f"impl-{task_id.lower()}-1",
        "worktree": f"/nonexistent/wt-{task_id}",
        "role": "implementer",
    }
    entry.update(extra)
    return entry


def _ws(subject, sandbox, sid="impl-T1-1", **files) -> Path:
    """A workspace directory, optionally seeded with named files."""
    ws = _workspaces(subject, sandbox) / sid
    ws.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (ws / name).write_text(body, encoding="utf-8")
    return ws


# =======================================================================================
# _resource_set  — pure
# =======================================================================================


@pytest.mark.parametrize(
    "task, expected",
    [
        ({}, set()),
        ({"resource": None}, set()),
        ({"resource": ""}, set()),
        ({"resource": []}, set()),
        ({"resource": 0}, set()),
        ({"resource": "eval-cpu"}, {"eval-cpu"}),
        ({"resource": ["eval-cpu"]}, {"eval-cpu"}),
        ({"resource": ["a", "b"]}, {"a", "b"}),
        ({"resource": ["a", "a"]}, {"a"}),
        ({"resource": ["a", "", None]}, {"a"}),
        ({"resource": ["a", 3]}, {"a", "3"}),
        ({"resource": ("a", "b")}, {"a", "b"}),
    ],
)
def test_resource_set_normalisation(subject, task, expected):
    assert subject._resource_set(task) == expected


def test_resource_set_of_a_dict_yields_its_keys(subject):
    """A mapping is truthy and non-str, so it is iterated — keys become the labels."""
    assert subject._resource_set({"resource": {"gpu": 1, "cpu": 2}}) == {"gpu", "cpu"}


def test_resource_set_of_a_bare_int_raises_type_error(subject):
    """A non-zero int `resource:` is truthy and non-str, so the set comprehension iterates it."""
    with pytest.raises(TypeError):
        subject._resource_set({"resource": 7})


# =======================================================================================
# _resources_conflict  — pure
# =======================================================================================


@pytest.mark.parametrize(
    "task, held, claimed, expected",
    [
        ({}, set(), set(), False),
        ({}, {"eval-cpu"}, {"gpu"}, False),                      # untagged never conflicts
        ({"resource": "eval-cpu"}, set(), set(), False),
        ({"resource": "eval-cpu"}, {"eval-cpu"}, set(), True),   # held by an in-flight task
        ({"resource": "eval-cpu"}, set(), {"eval-cpu"}, True),   # claimed earlier this cycle
        ({"resource": "eval-cpu"}, {"gpu"}, {"disk"}, False),
        ({"resource": ["a", "b"]}, {"b"}, set(), True),          # any overlap is enough
        ({"resource": ["a", "b"]}, {"c"}, {"a"}, True),
        ({"resource": []}, {"a"}, {"a"}, False),
    ],
)
def test_resources_conflict(subject, task, held, claimed, expected):
    assert subject._resources_conflict(task, held, claimed) is expected


def test_resources_conflict_does_not_mutate_the_caller_sets(subject):
    held, claimed = {"a"}, {"b"}
    subject._resources_conflict({"resource": ["a", "z"]}, held, claimed)
    assert (held, claimed) == ({"a"}, {"b"})


# =======================================================================================
# _timeout_expired
# =======================================================================================


def test_timeout_expired_is_false_before_the_ceiling(subject, sandbox, tmux):
    _ws(subject, sandbox)
    entry = _entry()
    launch = {"impl-T1-1": time.time() - 10}
    assert subject._timeout_expired(entry, launch) is False
    assert not (_ws(subject, sandbox) / "FAILED").exists()
    assert tmux.killed == []


def test_timeout_expired_treats_an_unknown_session_as_just_launched(subject, sandbox, tmux):
    """`launch_times.get(sid, time.time())` defaults to *now*, so a session with no
    recorded launch epoch can never be reaped."""
    _ws(subject, sandbox)
    assert subject._timeout_expired(_entry(), {}) is False
    assert not (_ws(subject, sandbox) / "FAILED").exists()


def test_timeout_expired_is_false_when_done_is_present(subject, sandbox, tmux):
    _ws(subject, sandbox, DONE="done")
    launch = {"impl-T1-1": time.time() - subject.TASK_TIMEOUT - 60}
    assert subject._timeout_expired(_entry(), launch) is False
    assert not (_ws(subject, sandbox) / "FAILED").exists()
    assert tmux.killed == []


def test_timeout_expired_writes_failed_and_kills_the_window_on_first_detection(
    subject, sandbox, tmux
):
    ws = _ws(subject, sandbox)
    launch = {"impl-T1-1": time.time() - subject.TASK_TIMEOUT - 1}
    assert subject._timeout_expired(_entry(), launch) is True
    assert (ws / "FAILED").read_text() == f"timeout after {subject.TASK_TIMEOUT // 3600}h"
    assert tmux.killed == ["impl-T1"]
    assert _events(subject, sandbox) == ["timeout_window_killed"]
    record = _records(subject, sandbox)[0]
    assert record["detail"] == "T1 window=impl-T1"
    assert record["session_id"] == "impl-T1-1"


def test_timeout_expired_is_idempotent_once_failed_exists(subject, sandbox, tmux):
    """The `not FAILED.exists()` guard makes the kill + journal happen exactly once."""
    ws = _ws(subject, sandbox, FAILED="already failed for another reason")
    launch = {"impl-T1-1": time.time() - subject.TASK_TIMEOUT - 1}
    assert subject._timeout_expired(_entry(), launch) is True
    assert (ws / "FAILED").read_text() == "already failed for another reason"
    assert tmux.killed == []
    assert _events(subject, sandbox) == []


def test_timeout_expired_with_no_window_name_skips_the_kill_and_the_journal(
    subject, sandbox, tmux
):
    ws = _ws(subject, sandbox)
    entry = _entry(window="")
    launch = {"impl-T1-1": time.time() - subject.TASK_TIMEOUT - 1}
    assert subject._timeout_expired(entry, launch) is True
    assert (ws / "FAILED").exists()
    assert tmux.killed == []
    assert _events(subject, sandbox) == []


def test_timeout_expired_raises_when_the_workspace_directory_is_gone(subject, sandbox, tmux):
    """No mkdir before the sentinel write, so a reaped workspace makes the reaper explode."""
    _workspaces(subject, sandbox).mkdir(parents=True, exist_ok=True)
    launch = {"impl-T1-1": time.time() - subject.TASK_TIMEOUT - 1}
    with pytest.raises(FileNotFoundError):
        subject._timeout_expired(_entry(), launch)


def test_timeout_expired_uses_the_entrys_session_not_its_task(subject, sandbox, tmux):
    """The clock is keyed by session_id, so a retry's fresh session starts a fresh clock."""
    _ws(subject, sandbox, sid="impl-T1-2")
    entry = _entry(sid="impl-T1-2")
    old = {"impl-T1-1": time.time() - subject.TASK_TIMEOUT - 1}
    assert subject._timeout_expired(entry, old) is False


# =======================================================================================
# reconcile_in_flight
# =======================================================================================


def test_reconcile_of_an_empty_state_returns_an_empty_list(subject, sandbox, tmux):
    launch: dict = {}
    assert subject.reconcile_in_flight({}, launch) == []
    assert launch == {}
    assert _events(subject, sandbox) == []


def test_reconcile_readopts_a_live_window_with_a_live_workspace(subject, sandbox, tmux):
    _ws(subject, sandbox)
    tmux.alive.add("impl-T1")
    launch: dict = {}
    survivors = subject.reconcile_in_flight({"in_flight": [_entry()]}, launch)
    assert [e["task_id"] for e in survivors] == ["T1"]
    assert _events(subject, sandbox) == ["re_adopted"]
    assert _detail(subject, sandbox, "re_adopted") == "T1 window=impl-T1"
    assert "impl-T1-1" in launch


def test_reconcile_restores_the_persisted_launch_epoch(subject, sandbox, tmux):
    """Bug #2's fix: a survivor keeps its original clock instead of restarting at now."""
    _ws(subject, sandbox)
    tmux.alive.add("impl-T1")
    launch: dict = {}
    state = {"in_flight": [_entry()], "launch_times": {"impl-T1-1": 1234.5}}
    subject.reconcile_in_flight(state, launch)
    assert launch["impl-T1-1"] == 1234.5


def test_reconcile_prefers_an_already_known_launch_epoch_over_the_persisted_one(
    subject, sandbox, tmux
):
    _ws(subject, sandbox)
    tmux.alive.add("impl-T1")
    launch = {"impl-T1-1": 999.0}
    state = {"in_flight": [_entry()], "launch_times": {"impl-T1-1": 1234.5}}
    subject.reconcile_in_flight(state, launch)
    assert launch["impl-T1-1"] == 999.0


def test_reconcile_zombie_grace_readopts_before_the_threshold(subject, sandbox, tmux):
    """result.json but no sentinel, window alive: tolerated for RECONCILE_DONE_GRACE_POLLS."""
    ws = _ws(subject, sandbox, **{"result.json": json.dumps({"status": "DONE"})})
    tmux.alive.add("impl-T1")
    launch: dict = {}
    survivors = subject.reconcile_in_flight({"in_flight": [_entry()]}, launch)
    assert len(survivors) == 1
    assert _events(subject, sandbox) == ["re_adopted"]
    assert not (ws / "DONE").exists()
    assert subject._reconcile_zombie_polls["impl-T1-1"] == 1


def test_reconcile_zombie_synthesizes_done_at_the_grace_threshold(subject, sandbox, tmux):
    ws = _ws(subject, sandbox, **{"result.json": json.dumps({"status": "DONE"})})
    tmux.alive.add("impl-T1")
    launch: dict = {}
    for _ in range(subject.RECONCILE_DONE_GRACE_POLLS):
        survivors = subject.reconcile_in_flight({"in_flight": [_entry()]}, launch)
    assert len(survivors) == 1
    assert (ws / "DONE").read_text().startswith("DONE (synthesized")
    assert tmux.killed == ["impl-T1"]
    assert _events(subject, sandbox)[-1] == "reconcile_synth_sentinel"
    detail = _detail(subject, sandbox, "reconcile_synth_sentinel")
    assert detail == f"T1 window=impl-T1 synth=DONE polls={subject.RECONCILE_DONE_GRACE_POLLS}"
    # the counter is cleared so a later zombie starts a fresh grace window
    assert "impl-T1-1" not in subject._reconcile_zombie_polls


def test_reconcile_zombie_synthesizes_failed_for_a_non_done_status(subject, sandbox, tmux):
    ws = _ws(subject, sandbox, **{"result.json": json.dumps({"status": "BLOCKED"})})
    tmux.alive.add("impl-T1")
    launch: dict = {}
    for _ in range(subject.RECONCILE_DONE_GRACE_POLLS):
        subject.reconcile_in_flight({"in_flight": [_entry()]}, launch)
    assert "BLOCKED" in (ws / "FAILED").read_text()
    assert _detail(subject, sandbox, "reconcile_synth_sentinel").endswith(
        f"synth=FAILED polls={subject.RECONCILE_DONE_GRACE_POLLS}"
    )


def test_reconcile_zombie_counter_resets_when_a_sentinel_appears(subject, sandbox, tmux):
    ws = _ws(subject, sandbox, **{"result.json": json.dumps({"status": "DONE"})})
    tmux.alive.add("impl-T1")
    launch: dict = {}
    subject.reconcile_in_flight({"in_flight": [_entry()]}, launch)
    assert subject._reconcile_zombie_polls["impl-T1-1"] == 1
    (ws / "PAUSED").write_text("hitl")
    subject.reconcile_in_flight({"in_flight": [_entry()]}, launch)
    assert "impl-T1-1" not in subject._reconcile_zombie_polls
    assert _events(subject, sandbox) == ["re_adopted", "re_adopted"]


def test_reconcile_marks_failed_when_the_window_is_alive_but_the_workspace_is_gone(
    subject, sandbox, tmux
):
    _workspaces(subject, sandbox).mkdir(parents=True, exist_ok=True)
    tmux.alive.add("impl-T1")
    launch: dict = {}
    survivors = subject.reconcile_in_flight({"in_flight": [_entry()]}, launch)
    assert len(survivors) == 1
    ws = _workspaces(subject, sandbox) / "impl-T1-1"
    assert (ws / "FAILED").read_text() == "workspace missing at orchestrator restart"
    assert _events(subject, sandbox) == ["reconcile_stale"]
    assert _detail(subject, sandbox, "reconcile_stale") == "T1 window=impl-T1 no_workspace"
    assert tmux.killed == []


def test_reconcile_keeps_a_dead_window_with_done_present(subject, sandbox, tmux):
    _ws(subject, sandbox, DONE="done")
    launch: dict = {}
    survivors = subject.reconcile_in_flight({"in_flight": [_entry()]}, launch)
    assert len(survivors) == 1
    assert _events(subject, sandbox) == ["reconcile_done_present"]
    assert "impl-T1-1" in launch


def test_reconcile_keeps_a_dead_window_with_failed_present(subject, sandbox, tmux):
    _ws(subject, sandbox, FAILED="boom")
    launch: dict = {}
    survivors = subject.reconcile_in_flight({"in_flight": [_entry()]}, launch)
    assert len(survivors) == 1
    assert _events(subject, sandbox) == ["reconcile_failed_present"]


def test_reconcile_done_wins_over_failed_when_both_sentinels_exist(subject, sandbox, tmux):
    _ws(subject, sandbox, DONE="done", FAILED="boom")
    subject.reconcile_in_flight({"in_flight": [_entry()]}, {})
    assert _events(subject, sandbox) == ["reconcile_done_present"]


def test_reconcile_a_paused_workspace_with_a_dead_window_is_treated_as_stale(
    subject, sandbox, tmux
):
    """PAUSED is a sentinel for the zombie guard but NOT for the dead-window ladder, so a
    HITL-parked task whose window died is failed rather than left parked."""
    ws = _ws(subject, sandbox, PAUSED="waiting for Dan")
    subject.reconcile_in_flight({"in_flight": [_entry()]}, {})
    assert _events(subject, sandbox) == ["reconcile_stale"]
    assert (ws / "FAILED").read_text().startswith("window gone")


def test_reconcile_stale_entry_records_the_impl_log_tail_in_the_sentinel(
    subject, sandbox, tmux
):
    ws = _ws(subject, sandbox, **{"impl.log": "line-a\nline-b\nsegmentation fault\n"})
    launch: dict = {}
    survivors = subject.reconcile_in_flight({"in_flight": [_entry()]}, launch)
    assert len(survivors) == 1
    text = (ws / "FAILED").read_text()
    assert text.startswith("window gone — stale in_flight entry cleared")
    assert "--- impl.log tail ---" in text
    assert "segmentation fault" in text
    assert _events(subject, sandbox) == ["reconcile_stale"]
    assert _detail(subject, sandbox, "reconcile_stale") == (
        "T1 window=impl-T1 no_sentinel (cleared stale in_flight)"
    )


def test_reconcile_stale_entry_without_an_impl_log_has_no_tail(subject, sandbox, tmux):
    _workspaces(subject, sandbox).mkdir(parents=True, exist_ok=True)
    subject.reconcile_in_flight({"in_flight": [_entry()]}, {})
    ws = _workspaces(subject, sandbox) / "impl-T1-1"
    assert (ws / "FAILED").read_text() == "window gone — stale in_flight entry cleared"


def test_reconcile_creates_the_missing_workspace_for_a_stale_entry(subject, sandbox, tmux):
    _workspaces(subject, sandbox).mkdir(parents=True, exist_ok=True)
    subject.reconcile_in_flight({"in_flight": [_entry()]}, {})
    assert (_workspaces(subject, sandbox) / "impl-T1-1").is_dir()


def test_reconcile_drops_a_usage_limit_exit_and_pauses_instead_of_failing(
    subject, sandbox, tmux, monkeypatch
):
    sent = _telegram(monkeypatch, subject)
    _put_state(subject, sandbox)
    ws = _ws(
        subject,
        sandbox,
        **{"impl.log": "working…\nYou've hit your limit · resets 4:30pm (UTC)\n"},
    )
    launch: dict = {}
    survivors = subject.reconcile_in_flight({"in_flight": [_entry()]}, launch)
    assert survivors == []                      # dropped from in_flight, not failed
    assert not (ws / "FAILED").exists()
    assert (ws / "USAGE_LIMIT_PARKED").exists()
    assert _events(subject, sandbox) == ["usage_limit_backoff"]
    assert _get_state(subject, sandbox)["paused_until"]
    assert len(sent) == 1 and "usage limit" in sent[0]


def test_reconcile_usage_limit_notifies_only_once_per_workspace(
    subject, sandbox, tmux, monkeypatch
):
    sent = _telegram(monkeypatch, subject)
    _put_state(subject, sandbox)
    _ws(subject, sandbox, **{"impl.log": "rate limit reached\n"})
    subject.reconcile_in_flight({"in_flight": [_entry()]}, {})
    subject.reconcile_in_flight({"in_flight": [_entry()]}, {})
    assert len(sent) == 1
    assert _events(subject, sandbox) == ["usage_limit_backoff", "usage_limit_backoff"]


def test_reconcile_never_shortens_an_existing_later_pause(subject, sandbox, tmux, monkeypatch):
    _telegram(monkeypatch, subject)
    far = "2099-01-01T00:00:00Z"
    _put_state(subject, sandbox, paused_until=far)
    _ws(subject, sandbox, **{"impl.log": "usage limit reached\n"})
    subject.reconcile_in_flight({"in_flight": [_entry()]}, {})
    assert _get_state(subject, sandbox)["paused_until"] == far


def test_reconcile_processes_every_entry_independently(subject, sandbox, tmux):
    _ws(subject, sandbox, sid="impl-A-1", DONE="done")
    _ws(subject, sandbox, sid="impl-B-1")
    tmux.alive.add("impl-B")
    entries = [
        _entry(sid="impl-A-1", task_id="A", window="impl-A"),
        _entry(sid="impl-B-1", task_id="B", window="impl-B"),
    ]
    survivors = subject.reconcile_in_flight({"in_flight": entries}, {})
    assert [e["task_id"] for e in survivors] == ["A", "B"]
    assert _events(subject, sandbox) == ["reconcile_done_present", "re_adopted"]


def test_reconcile_raises_on_an_in_flight_entry_missing_session_id(subject, sandbox, tmux):
    with pytest.raises(KeyError):
        subject.reconcile_in_flight({"in_flight": [{"task_id": "T1"}]}, {})


# =======================================================================================
# _do_retry
# =======================================================================================


@pytest.fixture
def retry_env(subject, sandbox, monkeypatch, tmp_path):
    """Everything `_do_retry` reaches, stubbed and recorded."""
    box = SimpleNamespace(worktrees=[], launches=[], parked=[], raise_on_worktree=None)

    def _create(task_id, worktree, branch):
        box.worktrees.append((task_id, str(worktree), branch))
        if box.raise_on_worktree is not None:
            raise box.raise_on_worktree

    monkeypatch.setattr(subject, "worktree_path_for", lambda tid: tmp_path / f"wt-{tid}")
    monkeypatch.setattr(subject, "create_worktree", _create)
    monkeypatch.setattr(
        subject,
        "launch_implementer",
        lambda task, sid, ws, wt, retry_note="": box.launches.append(
            SimpleNamespace(task=task, sid=sid, workspace=ws, worktree=wt, note=retry_note)
        ),
    )
    monkeypatch.setattr(subject, "parse_runnable_tasks", lambda: box.tasks)
    monkeypatch.setattr(
        subject, "park_failed", lambda tid, reason: box.parked.append((tid, reason))
    )
    box.tasks = []
    return box


def test_do_retry_launches_a_fresh_session_and_rewrites_state(subject, sandbox, retry_env):
    old = _entry()
    _put_state(subject, sandbox, in_flight=[old])
    in_flight = [old]
    subject._do_retry("T1", old, "gate failed", {}, in_flight)

    assert len(in_flight) == 2
    new = in_flight[-1]
    assert new["task_id"] == "T1"
    assert new["role"] == "implementer"
    assert new["status"] == "running"
    assert new["session_id"].startswith("impl-T1-")
    assert new["session_id"] != old["session_id"]
    assert new["branch"].startswith("impl-t1-")           # branch is lower-cased
    assert new["window"] == "impl-T1"

    persisted = _get_state(subject, sandbox)["in_flight"]
    assert [e["session_id"] for e in persisted] == [new["session_id"]]


def test_do_retry_reuses_the_task_id_derived_tmux_window_name(subject, sandbox, retry_env):
    """The retry's window name is derived from the task, not the session — so it collides
    with the window of the attempt it replaces."""
    old = _entry()
    _put_state(subject, sandbox, in_flight=[old])
    subject._do_retry("T1", old, "boom", {}, [old])
    assert _get_state(subject, sandbox)["in_flight"][0]["window"] == old["window"]


def test_do_retry_creates_the_new_workspace_directory(subject, sandbox, retry_env):
    old = _entry()
    _put_state(subject, sandbox, in_flight=[old])
    in_flight = [old]
    subject._do_retry("T1", old, "boom", {}, in_flight)
    assert (_workspaces(subject, sandbox) / in_flight[-1]["session_id"]).is_dir()


def test_do_retry_journals_against_the_old_session_id(subject, sandbox, retry_env):
    old = _entry()
    _put_state(subject, sandbox, in_flight=[old])
    subject._do_retry("T1", old, "x" * 200, {}, [old])
    record = _records(subject, sandbox)[-1]
    assert record["event"] == "implementer_retry"
    assert record["session_id"] == old["session_id"]
    assert record["detail"] == "T1 reason=" + "x" * 100      # reason truncated to 100


def test_do_retry_briefs_the_implementer_with_the_previous_failure(subject, sandbox, retry_env):
    old = _entry()
    _put_state(subject, sandbox, in_flight=[old])
    subject._do_retry("T1", old, "the gate rejected it", {}, [old])
    note = retry_env.launches[0].note
    assert "the gate rejected it" in note
    assert "different approach" in note


def test_do_retry_uses_the_roadmap_task_when_it_is_still_runnable(subject, sandbox, retry_env):
    retry_env.tasks = [{"id": "T1", "title": "Real title", "mode": "autonomous"}]
    old = _entry()
    _put_state(subject, sandbox, in_flight=[old])
    subject._do_retry("T1", old, "boom", {}, [old])
    assert retry_env.launches[0].task["title"] == "Real title"


def test_do_retry_synthesizes_a_placeholder_task_when_the_roadmap_has_none(
    subject, sandbox, retry_env
):
    old = _entry()
    _put_state(subject, sandbox, in_flight=[old])
    subject._do_retry("T1", old, "boom", {}, [old])
    assert retry_env.launches[0].task == {
        "id": "T1", "title": "T1", "short_desc": "", "mode": "autonomous",
    }


def test_do_retry_parks_and_clears_state_when_the_worktree_cannot_be_made(
    subject, sandbox, retry_env
):
    retry_env.raise_on_worktree = subprocess.CalledProcessError(128, ["git", "worktree"])
    old = _entry()
    _put_state(subject, sandbox, in_flight=[old])
    in_flight = [old]
    subject._do_retry("T1", old, "boom", {}, in_flight)

    assert in_flight == [old]                       # nothing appended
    assert _get_state(subject, sandbox)["in_flight"] == []
    assert retry_env.parked and retry_env.parked[0][0] == "T1"
    assert "retry launch failed" in retry_env.parked[0][1]
    assert _events(subject, sandbox) == []          # no implementer_retry on the failure path


def test_do_retry_never_reads_or_persists_the_retry_counts_argument(
    subject, sandbox, retry_env
):
    """`retry_counts` is a declared parameter the body never reads or writes.

    The caller's dict comes back unmutated, and the counts on disk keep whatever was
    already persisted: `_do_retry` re-reads state itself, so a caller that bumped the
    count in memory only (see docs/found_bugs_inbox/orchestrator.md) is not written
    through. The key is always present in the written state because `read_state()`
    defaults it to `{}`.
    """
    old = _entry()
    _put_state(subject, sandbox, in_flight=[old], retry_counts={"T1": 0})
    counts = {"T1": 5}
    subject._do_retry("T1", old, "boom", counts, [old])
    assert counts == {"T1": 5}
    assert _get_state(subject, sandbox)["retry_counts"] == {"T1": 0}


def test_do_retry_drops_the_old_entry_but_keeps_unrelated_ones(subject, sandbox, retry_env):
    old = _entry()
    other = _entry(sid="impl-T2-1", task_id="T2", window="impl-T2")
    _put_state(subject, sandbox, in_flight=[old, other])
    subject._do_retry("T1", old, "boom", {}, [old])
    sids = [e["session_id"] for e in _get_state(subject, sandbox)["in_flight"]]
    assert "impl-T2-1" in sids
    assert old["session_id"] not in sids


# =======================================================================================
# _remove_from_state
# =======================================================================================


def test_remove_from_state_drops_the_entry_and_its_launch_time(subject, sandbox):
    _put_state(
        subject,
        sandbox,
        in_flight=[_entry(), _entry(sid="impl-T2-1", task_id="T2")],
        launch_times={"impl-T1-1": 1.0, "impl-T2-1": 2.0},
    )
    subject._remove_from_state(_entry())
    state = _get_state(subject, sandbox)
    assert [e["session_id"] for e in state["in_flight"]] == ["impl-T2-1"]
    assert state["launch_times"] == {"impl-T2-1": 2.0}


def test_remove_from_state_is_a_no_op_for_an_unknown_session(subject, sandbox):
    _put_state(subject, sandbox, in_flight=[_entry()], launch_times={"impl-T1-1": 1.0})
    subject._remove_from_state(_entry(sid="impl-ZZ-9", task_id="ZZ"))
    state = _get_state(subject, sandbox)
    assert [e["session_id"] for e in state["in_flight"]] == ["impl-T1-1"]
    assert state["launch_times"] == {"impl-T1-1": 1.0}


def test_remove_from_state_still_rewrites_the_document(subject, sandbox):
    """Even a no-op removal persists state — `updated_at` and the read_state defaults land."""
    _put_state(subject, sandbox)
    subject._remove_from_state(_entry())
    state = _get_state(subject, sandbox)
    assert state["in_flight"] == []
    assert state["launch_times"] == {}
    assert "updated_at" in state
    assert state["waiting_on_dan"] == {}          # read_state defaults are materialised


def test_remove_from_state_raises_on_a_sibling_entry_with_no_session_id(subject, sandbox):
    _put_state(subject, sandbox, in_flight=[{"task_id": "T9"}])
    with pytest.raises(KeyError):
        subject._remove_from_state(_entry())


# =======================================================================================
# _idle_heartbeat_maybe
# =======================================================================================


def test_idle_heartbeat_fires_on_the_first_call(subject, sandbox):
    subject._idle_heartbeat_maybe([{"id": "T1"}, {"id": "T2"}])
    assert _events(subject, sandbox) == ["idle_gated"]
    assert _detail(subject, sandbox, "idle_gated") == (
        "no launchable task (all gated: T1,T2); polling"
    )


def test_idle_heartbeat_says_none_when_nothing_is_runnable(subject, sandbox):
    subject._idle_heartbeat_maybe([])
    assert _detail(subject, sandbox, "idle_gated") == (
        "no launchable task (all gated: none); polling"
    )


def test_idle_heartbeat_is_throttled_after_it_fires(subject, sandbox):
    subject._idle_heartbeat_maybe([])
    subject._idle_heartbeat_maybe([])
    subject._idle_heartbeat_maybe([])
    assert _events(subject, sandbox) == ["idle_gated"]


def test_idle_heartbeat_fires_again_once_the_interval_elapses(subject, sandbox, monkeypatch):
    subject._idle_heartbeat_maybe([])
    monkeypatch.setattr(
        subject, "_last_idle_heartbeat", time.monotonic() - subject.IDLE_HEARTBEAT_S - 1
    )
    subject._idle_heartbeat_maybe([])
    assert _events(subject, sandbox) == ["idle_gated", "idle_gated"]


def test_idle_heartbeat_interval_stays_under_the_watchdog_stall_window(subject):
    assert subject.IDLE_HEARTBEAT_S <= 20 * 60


def test_idle_heartbeat_raises_on_a_task_without_an_id(subject, sandbox):
    with pytest.raises(KeyError):
        subject._idle_heartbeat_maybe([{"title": "no id here"}])


# =======================================================================================
# launch_async_job
# =======================================================================================


@pytest.fixture
def async_env(subject, sandbox, monkeypatch):
    box = SimpleNamespace(parked=[], sent=_telegram(monkeypatch, subject))
    monkeypatch.setattr(
        subject, "park_failed", lambda tid, reason: box.parked.append((tid, reason))
    )
    _put_state(subject, sandbox)
    return box


ASYNC_TASK = {
    "id": "J1",
    "title": "long read-only job",
    "dispatch": "async-job",
    "launch_cmd": "bash run_job.sh",
    "verifications": [
        {"id": "V1", "kind": "auto", "await": True},
        {"id": "V2", "kind": "auto"},
        {"id": "V3", "kind": "manual", "await": True},
    ],
}


def test_launch_async_job_without_a_launch_cmd_parks_and_returns_false(
    subject, sandbox, async_env, monkeypatch
):
    _fake_subprocess(monkeypatch, subject)
    task = dict(ASYNC_TASK, launch_cmd="   ")
    assert subject.launch_async_job(task) is False
    assert _events(subject, sandbox) == ["async_job_no_cmd"]
    assert _detail(subject, sandbox, "async_job_no_cmd") == "J1 missing launch_cmd"
    assert async_env.parked[0][0] == "J1"
    assert _get_state(subject, sandbox).get("waiting_on_dan", {}) == {}


def test_launch_async_job_treats_a_missing_launch_cmd_key_the_same(
    subject, sandbox, async_env, monkeypatch
):
    _fake_subprocess(monkeypatch, subject)
    task = {k: v for k, v in ASYNC_TASK.items() if k != "launch_cmd"}
    assert subject.launch_async_job(task) is False
    assert _events(subject, sandbox) == ["async_job_no_cmd"]


def test_launch_async_job_runs_the_command_detached_and_parks(
    subject, sandbox, async_env, monkeypatch
):
    runs = _fake_subprocess(monkeypatch, subject)
    assert subject.launch_async_job(ASYNC_TASK) is True

    assert runs.argvs == ["bash run_job.sh"]
    kwargs = runs.calls[0].kwargs
    assert kwargs["shell"] is True
    assert kwargs["cwd"] == str(subject.REPO)
    assert kwargs["timeout"] == subject.SMOKE_TIMEOUT

    state = _get_state(subject, sandbox)
    assert state["dan_id_counter"] == 1
    parked = state["waiting_on_dan"]["1"]
    assert parked["task_id"] == "J1"
    assert parked["kind"] == "awaiting-verification"
    assert parked["session_id"] == ""
    assert parked["branch"] == ""
    assert parked["worktree"] == ""
    assert parked["await_checks"] == ["V1"]        # kind:auto AND await:true only
    assert parked["summary"].startswith("async job launched: bash run_job.sh")
    assert parked["await_started_at"] and parked["parked_at"]

    assert _events(subject, sandbox) == ["async_job_launched"]
    assert _detail(subject, sandbox, "async_job_launched") == (
        "J1 dan_id=1 cmd=bash run_job.sh"
    )
    assert len(async_env.sent) == 1 and "bash run_job.sh" in async_env.sent[0]


def test_launch_async_job_rewrites_a_leading_python_to_the_project_interpreter(
    subject, sandbox, async_env, monkeypatch
):
    runs = _fake_subprocess(monkeypatch, subject)
    subject.launch_async_job(dict(ASYNC_TASK, launch_cmd="python3 scripts/eval.py --tag x"))
    assert runs.argvs == [f"{subject.VENV_PYTHON} scripts/eval.py --tag x"]


def test_launch_async_job_records_the_raw_command_not_the_rewritten_one(
    subject, sandbox, async_env, monkeypatch
):
    _fake_subprocess(monkeypatch, subject)
    subject.launch_async_job(dict(ASYNC_TASK, launch_cmd="python3 scripts/eval.py"))
    parked = _get_state(subject, sandbox)["waiting_on_dan"]["1"]
    assert parked["summary"] == "async job launched: python3 scripts/eval.py"


def test_launch_async_job_increments_an_existing_dan_id_counter(
    subject, sandbox, async_env, monkeypatch
):
    _fake_subprocess(monkeypatch, subject)
    _put_state(subject, sandbox, dan_id_counter=7, waiting_on_dan={"7": {"task_id": "old"}})
    subject.launch_async_job(ASYNC_TASK)
    state = _get_state(subject, sandbox)
    assert state["dan_id_counter"] == 8
    assert set(state["waiting_on_dan"]) == {"7", "8"}


def test_launch_async_job_parks_failed_when_the_launcher_does_not_detach(
    subject, sandbox, async_env, monkeypatch
):
    def _timeout(args, kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout", 1))

    _fake_subprocess(monkeypatch, subject, _timeout)
    assert subject.launch_async_job(ASYNC_TASK) is False
    assert _events(subject, sandbox) == ["async_job_launch_timeout"]
    assert _detail(subject, sandbox, "async_job_launch_timeout") == (
        f"J1 launch cmd exceeded {subject.SMOKE_TIMEOUT}s"
    )
    assert async_env.parked[0][0] == "J1"
    assert _get_state(subject, sandbox).get("waiting_on_dan", {}) == {}
    assert async_env.sent == []


def test_launch_async_job_parks_failed_on_a_non_zero_exit(
    subject, sandbox, async_env, monkeypatch
):
    def _fail(args, kwargs):
        return subprocess.CompletedProcess(args[0], 2, "", "no such file")

    _fake_subprocess(monkeypatch, subject, _fail)
    assert subject.launch_async_job(ASYNC_TASK) is False
    assert _events(subject, sandbox) == ["async_job_launch_failed"]
    assert "no such file" in _detail(subject, sandbox, "async_job_launch_failed")
    assert "exit 2" in async_env.parked[0][1]
    assert _get_state(subject, sandbox).get("waiting_on_dan", {}) == {}


def test_launch_async_job_falls_back_to_stdout_when_stderr_is_empty(
    subject, sandbox, async_env, monkeypatch
):
    def _fail(args, kwargs):
        return subprocess.CompletedProcess(args[0], 1, "printed to stdout", "")

    _fake_subprocess(monkeypatch, subject, _fail)
    subject.launch_async_job(ASYNC_TASK)
    assert "printed to stdout" in _detail(subject, sandbox, "async_job_launch_failed")


def test_launch_async_job_with_no_awaitable_checks_still_parks(
    subject, sandbox, async_env, monkeypatch
):
    _fake_subprocess(monkeypatch, subject)
    task = dict(ASYNC_TASK, verifications=[{"id": "V2", "kind": "auto"}])
    assert subject.launch_async_job(task) is True
    parked = _get_state(subject, sandbox)["waiting_on_dan"]["1"]
    assert parked["await_checks"] == []
    assert "its eval gate" in async_env.sent[0]


# =======================================================================================
# poll_awaiting_verifications
# =======================================================================================


@pytest.fixture
def await_env(subject, monkeypatch):
    """Stubs the classifier and the graduation tail so only the poller's routing is tested."""
    box = SimpleNamespace(
        classification=([], []), graduated=[], sent=_telegram(monkeypatch, subject),
        task={"id": "M1", "verifications": [{"id": "V1", "kind": "auto", "await": True}]},
    )
    monkeypatch.setattr(subject, "get_task_by_id", lambda tid: box.task)
    monkeypatch.setattr(
        subject, "_classify_verifications", lambda verifs, cwd: box.classification
    )
    monkeypatch.setattr(
        subject,
        "_graduate_manual_action",
        lambda task_id, dan_id, sid, in_flight: box.graduated.append((task_id, dan_id, sid)),
    )
    return box


def _parked(**extra) -> dict:
    entry = {
        "task_id": "M1",
        "kind": "awaiting-verification",
        "session_id": "impl-M1-1",
        "await_started_at": _now_iso(),
        "await_checks": ["V1"],
    }
    entry.update(extra)
    return entry


def test_poll_awaiting_verifications_ignores_an_empty_queue(subject, sandbox, await_env):
    _put_state(subject, sandbox, waiting_on_dan={})
    subject.poll_awaiting_verifications([])
    assert _events(subject, sandbox) == []


def test_poll_awaiting_verifications_skips_other_kinds(subject, sandbox, await_env):
    _put_state(subject, sandbox, waiting_on_dan={"1": _parked(kind="manual-action")})
    subject.poll_awaiting_verifications([])
    assert _events(subject, sandbox) == []
    assert await_env.graduated == []


def test_poll_awaiting_verifications_graduates_when_everything_passes(
    subject, sandbox, await_env
):
    _put_state(subject, sandbox, waiting_on_dan={"3": _parked()})
    await_env.classification = ([], [])
    subject.poll_awaiting_verifications([])
    assert _events(subject, sandbox) == ["await_verify_passed"]
    assert _detail(subject, sandbox, "await_verify_passed") == "M1 dan_id=3"
    assert await_env.graduated == [("M1", "3", "impl-M1-1")]
    assert len(await_env.sent) == 1 and "finalizing" in await_env.sent[0]


def test_poll_awaiting_verifications_keeps_waiting_while_the_artifact_is_absent(
    subject, sandbox, await_env
):
    _put_state(subject, sandbox, waiting_on_dan={"1": _parked()})
    await_env.classification = (["V1"], [])
    subject.poll_awaiting_verifications([])
    assert _events(subject, sandbox) == []
    assert await_env.sent == []
    assert _get_state(subject, sandbox)["waiting_on_dan"]["1"]["kind"] == "awaiting-verification"


def test_poll_awaiting_verifications_surfaces_a_hard_failure_as_a_gate_failure(
    subject, sandbox, await_env
):
    _put_state(subject, sandbox, waiting_on_dan={"1": _parked(reminded_at=_now_iso())})
    await_env.classification = ([], ["V1"])
    subject.poll_awaiting_verifications([])

    assert _events(subject, sandbox) == ["await_verify_surfaced"]
    assert _detail(subject, sandbox, "await_verify_surfaced") == "M1 reason=gate V1"
    info = _get_state(subject, sandbox)["waiting_on_dan"]["1"]
    assert info["kind"] == "manual-action"
    assert info["await_failed_reason"] == "gate"
    assert "reminded_at" not in info               # the nag clock is reset
    assert "below threshold" in await_env.sent[0]


def test_poll_awaiting_verifications_surfaces_a_hard_failure_even_with_pending_ones(
    subject, sandbox, await_env
):
    _put_state(subject, sandbox, waiting_on_dan={"1": _parked()})
    await_env.classification = (["V2"], ["V1"])
    subject.poll_awaiting_verifications([])
    assert _detail(subject, sandbox, "await_verify_surfaced") == "M1 reason=gate V1"


def test_poll_awaiting_verifications_surfaces_a_timeout_once_the_bound_elapses(
    subject, sandbox, await_env
):
    started = _ago_iso(subject.AWAIT_VERIFY_TIMEOUT_SEC / 3600 + 1)
    _put_state(subject, sandbox, waiting_on_dan={"1": _parked(await_started_at=started)})
    await_env.classification = (["V1"], [])
    subject.poll_awaiting_verifications([])

    assert _detail(subject, sandbox, "await_verify_surfaced") == "M1 reason=timeout V1"
    info = _get_state(subject, sandbox)["waiting_on_dan"]["1"]
    assert info["kind"] == "manual-action"
    assert info["await_failed_reason"] == "timeout"
    assert "never landed" in await_env.sent[0]


def test_poll_awaiting_verifications_never_times_out_an_unparseable_start_stamp(
    subject, sandbox, await_env
):
    _put_state(subject, sandbox, waiting_on_dan={"1": _parked(await_started_at="not-a-date")})
    await_env.classification = (["V1"], [])
    subject.poll_awaiting_verifications([])
    assert _events(subject, sandbox) == []


def test_poll_awaiting_verifications_surfacing_is_idempotent(subject, sandbox, await_env):
    """Reverting the kind takes the entry out of the poller's scope, so no per-poll re-ping."""
    _put_state(subject, sandbox, waiting_on_dan={"1": _parked()})
    await_env.classification = ([], ["V1"])
    subject.poll_awaiting_verifications([])
    subject.poll_awaiting_verifications([])
    assert _events(subject, sandbox) == ["await_verify_surfaced"]
    assert len(await_env.sent) == 1


def test_poll_awaiting_verifications_graduates_a_task_that_vanished_from_the_roadmap(
    subject, sandbox, monkeypatch
):
    """`get_task_by_id` → None yields an empty verification list, which classifies as
    "nothing pending, nothing failing" — i.e. a pass. The gate is never actually run."""
    graduated: list = []
    _telegram(monkeypatch, subject)
    monkeypatch.setattr(subject, "get_task_by_id", lambda tid: None)
    monkeypatch.setattr(
        subject,
        "_graduate_manual_action",
        lambda task_id, dan_id, sid, in_flight: graduated.append(task_id),
    )
    runs = _fake_subprocess(monkeypatch, subject)
    _put_state(subject, sandbox, waiting_on_dan={"1": _parked()})
    subject.poll_awaiting_verifications([])
    assert graduated == ["M1"]
    assert runs.calls == []
    assert _events(subject, sandbox) == ["await_verify_passed"]


def test_poll_awaiting_verifications_handles_several_entries_in_one_pass(
    subject, sandbox, await_env
):
    _put_state(
        subject,
        sandbox,
        waiting_on_dan={
            "1": _parked(task_id="M1"),
            "2": _parked(task_id="M2", kind="manual-action"),
        },
    )
    await_env.classification = ([], [])
    subject.poll_awaiting_verifications([])
    assert [g[0] for g in await_env.graduated] == ["M1"]


# =======================================================================================
# _notify_proposal_test_due
# =======================================================================================


def test_notify_proposal_test_due_pings_once_and_parks_the_loop(
    subject, sandbox, monkeypatch
):
    sent = _telegram(monkeypatch, subject)
    _put_state(subject, sandbox)
    subject._notify_proposal_test_due()

    state = _get_state(subject, sandbox)
    assert state["paused_by_user"] is True
    assert state["proposal_test_notified"] is True
    assert _events(subject, sandbox) == ["proposal_test_due"]
    assert _detail(subject, sandbox, "proposal_test_due") == subject.PROPOSAL_TEST_HANDOFF
    assert len(sent) == 1
    assert subject.PROPOSAL_TEST_HANDOFF in sent[0]


def test_notify_proposal_test_due_is_a_no_op_once_notified(subject, sandbox, monkeypatch):
    sent = _telegram(monkeypatch, subject)
    _put_state(subject, sandbox, proposal_test_notified=True, paused_by_user=False)
    subject._notify_proposal_test_due()
    assert sent == []
    assert _events(subject, sandbox) == []
    assert _get_state(subject, sandbox)["paused_by_user"] is False


# =======================================================================================
# generate_proposals
# =======================================================================================


@pytest.fixture
def proposal_env(subject, sandbox, monkeypatch):
    box = SimpleNamespace(sent=_telegram(monkeypatch, subject), raw="[]", error=None)

    def _judge(system, user, **kwargs):
        if box.error is not None:
            raise box.error
        return box.raw

    monkeypatch.setattr(subject, "_judge_complete", _judge)
    _put_state(subject, sandbox)
    roadmap = _roadmap(subject, sandbox)
    roadmap.parent.mkdir(parents=True, exist_ok=True)
    roadmap.write_text("# Roadmap\n", encoding="utf-8")
    (subject.REPO / "docs" / "PROJECT.md").write_text("# Project\n", encoding="utf-8")
    return box


PROPOSAL = {
    "title": "Speed up retrieval",
    "why_and_expected_impact": "latency hotspot",
    "est_hours": 3,
    "mode": "autonomous",
    "deps": [],
}


def test_generate_proposals_bails_out_when_the_docs_are_unreadable(
    subject, sandbox, proposal_env
):
    _roadmap(subject, sandbox).unlink()
    subject.generate_proposals()
    assert _events(subject, sandbox) == []
    assert _get_state(subject, sandbox).get("paused_by_user") is None
    assert any("Could not read docs" in m for m in proposal_env.sent)


def test_generate_proposals_reports_a_judge_error_and_returns(subject, sandbox, proposal_env):
    proposal_env.error = RuntimeError("judge unavailable")
    subject.generate_proposals()
    assert _events(subject, sandbox) == []
    assert any("Proposal generation error" in m for m in proposal_env.sent)
    assert _get_state(subject, sandbox).get("pending_proposal_qid") is None


def test_generate_proposals_says_parking_but_does_not_park_when_none_come_back(
    subject, sandbox, proposal_env
):
    """The 'Parking' message is sent, yet no state is written — the loop is not parked."""
    proposal_env.raw = "no json here at all"
    subject.generate_proposals()
    assert proposal_env.sent[-1] == "No proposals generated. Parking — ping Dan."
    state = _get_state(subject, sandbox)
    assert state.get("paused_by_user") is None
    assert state.get("pending_proposal_qid") is None
    assert _events(subject, sandbox) == []


def test_generate_proposals_saves_the_question_parks_and_journals(
    subject, sandbox, proposal_env
):
    proposal_env.raw = "prose before " + json.dumps([PROPOSAL]) + " prose after"
    subject.generate_proposals()

    state = _get_state(subject, sandbox)
    q_id = state["pending_proposal_qid"]
    assert state["paused_by_user"] is True
    assert q_id.startswith("proposals-")

    payload = json.loads((_questions_dir(subject, sandbox) / f"{q_id}.json").read_text())
    assert payload["id"] == q_id
    assert payload["type"] == "phase-approval"
    assert payload["proposals"] == [PROPOSAL]
    assert payload["created_at"]

    assert _events(subject, sandbox) == ["proposals_sent"]
    assert _detail(subject, sandbox, "proposals_sent") == f"1 proposals sent to Dan, q_id={q_id}"


def test_generate_proposals_renders_the_multi_select_message(subject, sandbox, proposal_env):
    proposal_env.raw = json.dumps([PROPOSAL, dict(PROPOSAL, title="Ask Dan", mode="needs-dan")])
    subject.generate_proposals()
    body = proposal_env.sent[-1]
    assert "Proposed next tasks" in body
    assert "1." in body and "2." in body
    assert "Speed up retrieval" in body
    assert "~3h" in body
    assert "comma-separated numbers" in body


def test_generate_proposals_renders_at_most_ten_but_saves_them_all(
    subject, sandbox, proposal_env
):
    many = [dict(PROPOSAL, title=f"P{i}") for i in range(12)]
    proposal_env.raw = json.dumps(many)
    subject.generate_proposals()
    body = proposal_env.sent[-1]
    assert "P9" in body
    assert "P10" not in body and "P11" not in body
    q_id = _get_state(subject, sandbox)["pending_proposal_qid"]
    payload = json.loads((_questions_dir(subject, sandbox) / f"{q_id}.json").read_text())
    assert len(payload["proposals"]) == 12
    assert _detail(subject, sandbox, "proposals_sent").startswith("12 proposals")


def test_generate_proposals_raises_on_a_proposal_without_a_title(
    subject, sandbox, proposal_env
):
    """The rendering loop indexes `p['title']` outside the try/except that guards parsing."""
    proposal_env.raw = json.dumps([{"est_hours": 1}])
    with pytest.raises(KeyError):
        subject.generate_proposals()
    assert _get_state(subject, sandbox).get("pending_proposal_qid") is None


def test_generate_proposals_tolerates_a_missing_est_hours(subject, sandbox, proposal_env):
    proposal_env.raw = json.dumps([{"title": "No estimate"}])
    subject.generate_proposals()
    assert "~?h" in proposal_env.sent[-1]


# =======================================================================================
# poll_proposal_answer
# =======================================================================================


def _put_question(subject, sandbox, q_id: str, proposals: list) -> Path:
    qdir = _questions_dir(subject, sandbox)
    qdir.mkdir(parents=True, exist_ok=True)
    path = qdir / f"{q_id}.json"
    path.write_text(json.dumps({"id": q_id, "proposals": proposals}), encoding="utf-8")
    return path


def _put_answer(subject, sandbox, q_id: str, text: str) -> Path:
    qdir = _questions_dir(subject, sandbox)
    qdir.mkdir(parents=True, exist_ok=True)
    path = qdir / f"{q_id}.answer"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def answer_env(subject, sandbox, monkeypatch):
    box = SimpleNamespace(sent=_telegram(monkeypatch, subject))
    roadmap = _roadmap(subject, sandbox)
    roadmap.parent.mkdir(parents=True, exist_ok=True)
    roadmap.write_text("# Roadmap\n\nexisting text\n", encoding="utf-8")
    return box


def test_poll_proposal_answer_does_nothing_without_a_pending_question(
    subject, sandbox, answer_env
):
    _put_state(subject, sandbox)
    subject.poll_proposal_answer()
    assert _events(subject, sandbox) == []
    assert answer_env.sent == []


def test_poll_proposal_answer_waits_while_the_answer_file_is_absent(
    subject, sandbox, answer_env
):
    _put_state(subject, sandbox, pending_proposal_qid="q1", paused_by_user=True)
    _put_question(subject, sandbox, "q1", [PROPOSAL])
    subject.poll_proposal_answer()
    assert _get_state(subject, sandbox)["paused_by_user"] is True
    assert _events(subject, sandbox) == []


def test_poll_proposal_answer_appends_the_chosen_proposals_to_the_roadmap(
    subject, sandbox, answer_env
):
    proposals = [PROPOSAL, dict(PROPOSAL, title="Second one", mode="needs-dan", deps=["T1"])]
    _put_state(subject, sandbox, pending_proposal_qid="q1", paused_by_user=True)
    _put_question(subject, sandbox, "q1", proposals)
    answer = _put_answer(subject, sandbox, "q1", " 1,2 ")

    subject.poll_proposal_answer()

    text = _roadmap(subject, sandbox).read_text(encoding="utf-8")
    assert text.startswith("# Roadmap\n\nexisting text")
    assert "id: P-Speed_up_retrieval" in text
    assert 'short_desc: "Speed up retrieval"' in text
    assert "id: P-Second_one" in text
    assert "mode: needs-dan" in text
    assert "status: pending" in text
    assert 'deps: ["T1"]' in text

    state = _get_state(subject, sandbox)
    assert state["paused_by_user"] is False
    assert "pending_proposal_qid" not in state
    assert not answer.exists()
    assert _events(subject, sandbox) == ["proposals_approved"]
    assert _detail(subject, sandbox, "proposals_approved") == (
        "['Speed up retrieval', 'Second one']"
    )
    assert answer_env.sent == ["✅ Added 2 approved proposals to ROADMAP."]


def test_poll_proposal_answer_truncates_the_generated_id_to_twenty_characters(
    subject, sandbox, answer_env
):
    long_title = "A very long proposal title that will not fit"
    _put_state(subject, sandbox, pending_proposal_qid="q1")
    _put_question(subject, sandbox, "q1", [dict(PROPOSAL, title=long_title)])
    _put_answer(subject, sandbox, "q1", "1")
    subject.poll_proposal_answer()
    assert f"id: P-{long_title[:20].replace(' ', '_')}" in _roadmap(subject, sandbox).read_text()


@pytest.mark.parametrize("answer", ["", "0", "9", "-1", "abc", "1.5"])
def test_poll_proposal_answer_resumes_without_changes_on_an_unusable_answer(
    subject, sandbox, answer_env, answer
):
    _put_state(subject, sandbox, pending_proposal_qid="q1", paused_by_user=True)
    _put_question(subject, sandbox, "q1", [PROPOSAL])
    _put_answer(subject, sandbox, "q1", answer)
    before = _roadmap(subject, sandbox).read_text(encoding="utf-8")

    subject.poll_proposal_answer()

    assert _roadmap(subject, sandbox).read_text(encoding="utf-8") == before
    state = _get_state(subject, sandbox)
    assert state["paused_by_user"] is False
    assert "pending_proposal_qid" not in state
    assert _events(subject, sandbox) == []
    assert answer_env.sent == []


def test_poll_proposal_answer_ignores_out_of_range_indices_but_keeps_valid_ones(
    subject, sandbox, answer_env
):
    _put_state(subject, sandbox, pending_proposal_qid="q1")
    _put_question(subject, sandbox, "q1", [PROPOSAL])
    _put_answer(subject, sandbox, "q1", "1,4,7")
    subject.poll_proposal_answer()
    assert _detail(subject, sandbox, "proposals_approved") == "['Speed up retrieval']"


def test_poll_proposal_answer_stays_parked_forever_when_the_question_file_is_gone(
    subject, sandbox, answer_env
):
    """The unreadable-question path returns *before* clearing `pending_proposal_qid`, so
    the loop stays paused and re-reads the same undecodable question every poll."""
    _put_state(subject, sandbox, pending_proposal_qid="q1", paused_by_user=True)
    _put_answer(subject, sandbox, "q1", "1")
    subject.poll_proposal_answer()
    state = _get_state(subject, sandbox)
    assert state["paused_by_user"] is True
    assert state["pending_proposal_qid"] == "q1"
    assert (_questions_dir(subject, sandbox) / "q1.answer").exists()


def test_poll_proposal_answer_leaves_the_question_file_behind(subject, sandbox, answer_env):
    """Only the `.answer` is unlinked; the `.json` accumulates in the questions directory."""
    _put_state(subject, sandbox, pending_proposal_qid="q1")
    q_path = _put_question(subject, sandbox, "q1", [PROPOSAL])
    _put_answer(subject, sandbox, "q1", "1")
    subject.poll_proposal_answer()
    assert q_path.exists()


# =======================================================================================
# main — guards and exactly one poll cycle (cc=249; deliberately not branch-covered)
# =======================================================================================


def test_main_is_importable_and_callable(subject):
    assert callable(subject.main)


def test_main_refuses_to_start_without_its_tmux_session(subject, sandbox, monkeypatch):
    def _no_session(args, kwargs):
        return subprocess.CompletedProcess(args[0], 1, "", "")

    runs = _fake_subprocess(monkeypatch, subject, _no_session)
    assert subject.main() == 1
    assert runs.argvs == [["tmux", "has-session", "-t", subject.TMUX_SESSION]]
    assert _events(subject, sandbox) == []


def test_main_respects_a_halted_state_before_doing_any_work(subject, sandbox, monkeypatch):
    _fake_subprocess(monkeypatch, subject)
    _put_state(subject, sandbox, halted=True)
    assert subject.main() == 0
    assert _events(subject, sandbox) == ["halt_respected"]
    assert _detail(subject, sandbox, "halt_respected") == "halted=True"


@pytest.fixture
def loop_env(subject, sandbox, monkeypatch):
    """Everything one poll cycle reaches, stubbed. The queue is empty by construction."""
    box = SimpleNamespace(sent=_telegram(monkeypatch, subject), calls=[])

    def _record(name, result=None):
        def _fn(*args, **kwargs):
            box.calls.append(name)
            return result
        return _fn

    for name, result in [
        ("run_dep_map", None),
        ("run_status", None),
        ("poll_control_commands", None),
        ("_send_hitl_reminders", None),
        ("apply_ready_self_fixes", None),
        ("apply_ready_redo", None),
        ("maybe_push_roadmap_map_change", None),
        ("parse_runnable_tasks", []),
        ("parse_prep_tasks", []),
    ]:
        monkeypatch.setattr(subject, name, _record(name, result))
    monkeypatch.setattr(subject, "get_effective_cap", lambda: (subject.CONCURRENCY_CAP, 0.0))
    # B7 default: defer to the proposal-test ping rather than auto-proposing.
    monkeypatch.setattr(subject, "AUTO_PROPOSE", False)
    box.runs = _fake_subprocess(monkeypatch, subject)
    box.clock = _fake_time(monkeypatch, subject)
    return box


def test_main_stops_on_the_halt_sentinel_at_the_top_of_the_loop(
    subject, sandbox, loop_env
):
    _put_state(subject, sandbox)
    _halt_file(subject, sandbox).parent.mkdir(parents=True, exist_ok=True)
    _halt_file(subject, sandbox).write_text("stop", encoding="utf-8")
    assert subject.main() == 0
    assert _events(subject, sandbox) == ["halt_detected"]
    assert loop_env.clock.sleeps == []


def test_main_runs_one_poll_cycle_over_an_empty_queue_then_parks(
    subject, sandbox, loop_env
):
    """The whole contract of an idle cycle: queue_exhausted → phase idle → the
    proposal-test ping parks the loop → the next iteration's halt guard breaks out."""
    _put_state(subject, sandbox)
    assert subject.main() == 0

    assert _events(subject, sandbox) == [
        "queue_exhausted", "proposal_test_due", "halt_respected",
    ]
    assert _detail(subject, sandbox, "queue_exhausted") == ""
    assert _detail(subject, sandbox, "halt_respected") == "halted=None paused_by_user=True"

    state = _get_state(subject, sandbox)
    assert state["phase"] == {"id": None, "title": None, "status": "idle"}
    assert state["in_flight"] == []
    assert state["skeleton_version"] == "B7"
    assert state["paused_by_user"] is True
    assert state["proposal_test_notified"] is True

    # exactly one throttled sleep — the idle cycle's, not the heartbeat's
    assert loop_env.clock.sleeps == [subject.POLL_INTERVAL]
    assert loop_env.calls.count("poll_control_commands") == 1
    assert loop_env.calls.count("parse_runnable_tasks") == 1
    assert loop_env.calls[-1] == "run_status"           # the post-loop status refresh
    assert loop_env.runs.argvs == [["tmux", "has-session", "-t", subject.TMUX_SESSION]]


def test_main_breaks_out_while_a_usage_limit_pause_is_active(subject, sandbox, loop_env):
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _put_state(subject, sandbox, paused_until=future)
    assert subject.main() == 0
    assert _events(subject, sandbox) == ["paused_until_break"]
    assert _detail(subject, sandbox, "paused_until_break") == f"paused_until={future}"
    assert loop_env.calls.count("poll_control_commands") == 0


# =====================================================================================
# _noop_merge_action — a branch that merged nothing must not relaunch forever
# =====================================================================================


def test_noop_merge_parks_a_script_task_on_the_first_attempt(subject):
    """No implementer to re-brief and a deterministic `run:` — retrying reproduces the
    same nothing, so the first no-op parks."""
    assert subject._noop_merge_action({"kind": "script"}, "T1", {}) == "park"


def test_noop_merge_parks_a_script_task_even_with_retries_available(subject):
    assert subject._noop_merge_action({"kind": "script"}, "T1", {"T1": 0}) == "park"


def test_noop_merge_re_briefs_an_implementer_task_once(subject):
    assert subject._noop_merge_action({"kind": "code"}, "T1", {}) == "retry"


def test_noop_merge_parks_an_implementer_task_after_its_one_retry(subject):
    """Mirrors the verification-gate and proof-review paths: one re-brief, then park."""
    assert subject._noop_merge_action({"kind": "code"}, "T1", {"T1": 1}) == "park"


def test_noop_merge_treats_a_missing_task_def_as_an_implementer_task(subject):
    """A task that fell out of ROADMAP mid-flight still had an implementer, so it keeps
    the re-brief. Guards against `None.get` too."""
    assert subject._noop_merge_action(None, "T1", {}) == "retry"


def test_noop_merge_never_escalates(subject):
    """`park_regression` is the wrong destination for a no-op: it registers the task in
    neither `parked_tasks` nor `waiting_on_dan`, leaving it runnable — the relaunch storm
    this function exists to stop."""
    seen = {
        subject._noop_merge_action(td, "T1", rc)
        for td in ({"kind": "script"}, {"kind": "code"}, None, {})
        for rc in ({}, {"T1": 0}, {"T1": 1}, {"T1": 9})
    }
    assert seen <= {"retry", "park"}
