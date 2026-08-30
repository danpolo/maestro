"""Mid-work backend switching (D2, D3) — `maestro.switch`.

`maestro/switch.py` is new M2 behaviour, not an extraction, so these tests live here
rather than under `tests/characterization/`.

**Nothing here launches an agent, spends a token of quota or reaches a network.** An
autouse fixture replaces the registry's driver factory, tmux liveness, the tmux kill and
Telegram with objects that raise, so a call that slipped past a substitution fails the
test instead of costing money or waking the operator. The same fixture rebases every
path this module writes through — its workspaces root, the state document and the
journal — into `tmp_path`, so no test can write into the live repository.

The load-bearing test is `test_uncommitted_work_survives_a_switch`. It builds a **real**
git repository, dirties it in both ways an implementer can (a modified tracked file and
an untracked one), performs a real switch through the real code path, and then asserts
both files are byte-for-byte intact. That is the entire point of D3: `create_worktree`
force-removes and re-adds a worktree, so a switch that took the obvious route would
destroy exactly the work it exists to preserve. `create_worktree` and `remove_worktree`
are replaced with explosives for the duration.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from maestro import state, switch, worktree as worktree_module
from maestro.backends import registry
from maestro.backends.base import Capabilities, Handle, LaunchSpec, Usage, WindowUsage

TASK_ID = "T7"
OLD_SESSION = "impl-T7-20260812-090000"
NEW_SESSION = "impl-T7-20260812-100000"
WINDOW = f"impl-{TASK_ID}"

DIRTY_TRACKED = "def solve():\n    return 'half-finished, uncommitted'\n"
DIRTY_UNTRACKED = "scratch notes the previous agent never committed\n"

FROM = "claude"
TO = "codex"

# Every driver capability combination these tests need, spelled by capability only.
RESUMING = Capabilities(
    native_resume=True, system_prompt_file=False, usage_telemetry=True, sandbox=True
)
REBRIEFING = Capabilities(
    native_resume=False, system_prompt_file=False, usage_telemetry=True, sandbox=True
)
PROFILED = Capabilities(
    native_resume=False, system_prompt_file=True, usage_telemetry=True, sandbox=False
)


def _forbidden(*_args, **_kwargs):
    raise AssertionError("a test reached a seam it was supposed to substitute")


@pytest.fixture(autouse=True)
def _sealed(monkeypatch, tmp_path):
    """No driver, no tmux, no Telegram, no writes outside `tmp_path`."""
    repo = tmp_path / "repo"
    workspaces = repo / ".orchestrator" / "workspaces"
    workspaces.mkdir(parents=True)

    monkeypatch.setattr(switch, "REPO", repo)
    monkeypatch.setattr(switch, "WORKSPACES", workspaces)
    monkeypatch.setattr(state, "STATE_JSON", repo / ".orchestrator" / "state.json")
    monkeypatch.setattr(state, "JOURNAL", repo / ".orchestrator" / "journal.ndjson")

    monkeypatch.setattr(registry, "get_backend", _forbidden)
    monkeypatch.setattr(switch, "tmux_window_exists", _forbidden)
    monkeypatch.setattr(switch, "_kill_tmux_window", _forbidden)
    monkeypatch.setattr(switch, "notify_telegram", _forbidden)
    yield


@pytest.fixture(autouse=True)
def _worktree_lifecycle_is_off_limits(monkeypatch):
    """A switch that recreates the worktree deletes the work it exists to preserve."""

    def explode(*_args, **_kwargs):
        raise AssertionError("a switch touched the worktree lifecycle")

    monkeypatch.setattr(worktree_module, "create_worktree", explode)
    monkeypatch.setattr(worktree_module, "remove_worktree", explode)
    yield


class FakeDriver:
    """A driver that records instead of launching. Declares capabilities like a real one."""

    def __init__(self, name=TO, capabilities=REBRIEFING, native_id="thread-1"):
        self.name = name
        self._capabilities = capabilities
        self._native_id = native_id
        self.launched: list[LaunchSpec] = []
        self.resumed: list[tuple[Handle, str]] = []
        self.capability_calls = 0

    def capabilities(self) -> Capabilities:
        self.capability_calls += 1
        return self._capabilities

    def launch(self, spec: LaunchSpec) -> Handle:
        self.launched.append(spec)
        workspace = Path(spec.workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "brief.txt").write_text(spec.brief, encoding="utf-8")
        return Handle(
            backend=self.name,
            session_id=spec.session_id,
            native_id=self._native_id,
            workspace=workspace,
            window=f"agents:impl-{spec.task_id}",
        )

    def resume(self, handle: Handle, prompt: str) -> Handle:
        self.resumed.append((handle, prompt))
        return Handle(
            backend=self.name,
            session_id=handle.session_id,
            native_id=handle.native_id,
            workspace=Path(handle.workspace),
            window=handle.window,
        )

    def parse_exit(self, rc: int, log_tail: str):  # pragma: no cover - unused here
        raise NotImplementedError

    def usage(self):  # pragma: no cover - unused here
        return None


class Recorder:
    """Collects calls; stands in for the journal, Telegram and the state document."""

    def __init__(self):
        self.journal: list[tuple] = []
        self.notices: list[str] = []
        self.state: dict = {"in_flight": []}

    def record(self, event, detail, session_id=""):
        self.journal.append((event, detail, session_id))

    def notify(self, message):
        self.notices.append(message)

    def load_state(self):
        return json.loads(json.dumps(self.state))

    def save_state(self, document):
        self.state = document

    def events(self, name):
        return [item for item in self.journal if item[0] == name]


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True
    )
    return proc.stdout


@pytest.fixture
def dirty_worktree(tmp_path) -> Path:
    """A real git worktree with one commit and both flavours of uncommitted work."""
    repo = tmp_path / "maestro-impl-T7"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "maestro@example.invalid")
    _git(repo, "config", "user.name", "Maestro Tests")
    (repo / "module.py").write_text("def solve():\n    return None\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat(T7): the committed half")
    (repo / "module.py").write_text(DIRTY_TRACKED, encoding="utf-8")
    (repo / "scratch.txt").write_text(DIRTY_UNTRACKED, encoding="utf-8")
    return repo


@pytest.fixture
def previous_workspace(tmp_path) -> Path:
    """The outgoing implementer's workspace, as a stopped agent would leave it."""
    workspace = switch.WORKSPACES / OLD_SESSION
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "brief.txt").write_text(
        "You are a Sonnet implementer. Your task is T7: teach the widget to fly.",
        encoding="utf-8",
    )
    (workspace / "checkpoint.md").write_text(
        "Wired the parser; the encoder is half-done and untested.", encoding="utf-8"
    )
    (workspace / "result.json").write_text(
        json.dumps(
            {
                "task_id": TASK_ID,
                "status": "IN-PROGRESS",
                "NEEDS": "a decision on the wire format",
                "verifications": {
                    "V1": {"done": True, "proof": "pytest tests/test_parser.py: 12 passed"},
                    "V2": {"done": False, "proof": ""},
                },
            }
        ),
        encoding="utf-8",
    )
    return workspace


def _entry(worktree: Path, **overrides) -> dict:
    entry = {
        "session_id": OLD_SESSION,
        "task_id": TASK_ID,
        "role": "implementer",
        "worktree": str(worktree),
        "window": WINDOW,
        "branch": "impl-t7-20260812-090000",
        "started_at": "2026-08-12T09:00:00Z",
        "status": "running",
    }
    entry.update(overrides)
    return entry


def _deps(recorder: Recorder, driver: FakeDriver, **overrides) -> switch.SwitchDeps:
    """Deps for a task whose window is already gone — the quota trigger's shape."""
    fields = {
        "driver_factory": lambda name: driver,
        "journal": recorder.record,
        "notify": recorder.notify,
        "load_state": recorder.load_state,
        "save_state": recorder.save_state,
        "window_exists": lambda window: False,
        "sleep": lambda seconds: None,
        "clock": lambda: "2026-08-12T10:00:00Z",
        "session_id_factory": lambda task_id: NEW_SESSION,
    }
    fields.update(overrides)
    return switch.SwitchDeps(**fields)


# ── the sentinel and the grace period (triggers 2 and 3) ──


def test_sentinel_names_the_target_and_the_reason(tmp_path):
    workspace = tmp_path / "ws"
    path = switch.request_checkpoint(
        workspace,
        to_backend=TO,
        reason=switch.REASON_THRESHOLD,
        requested_at="2026-08-12T10:00:00Z",
    )

    assert path == workspace / switch.SWITCH_SENTINEL
    body = path.read_text(encoding="utf-8")
    assert body.splitlines()[0] == switch.SWITCH_SENTINEL
    assert f"to={TO}" in body
    assert f"reason={switch.REASON_THRESHOLD}" in body
    assert "requested_at=2026-08-12T10:00:00Z" in body
    # The implementer must not mistake a handover for an outcome.
    assert "Do NOT write DONE or FAILED" in body
    assert switch.CHECKPOINT_FILE in body
    assert switch.checkpoint_requested(workspace) is True

    switch.clear_checkpoint_request(workspace)
    assert switch.checkpoint_requested(workspace) is False


def test_the_sentinel_is_not_a_terminal_sentinel():
    """`reconcile_in_flight` reads DONE/FAILED/PAUSED; a handover is none of them."""
    assert switch.SWITCH_SENTINEL not in {"DONE", "FAILED", "PAUSED"}


def test_stop_agent_writes_no_sentinel_when_the_process_is_already_gone(tmp_path):
    """Trigger 1: quota exhausted — the process has exited, there is nothing to interrupt."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    deps = switch.SwitchDeps(
        window_exists=lambda window: False, kill_window=_forbidden, sleep=_forbidden
    )

    stopped = switch.stop_agent(
        WINDOW, workspace, to_backend=TO, reason=switch.REASON_QUOTA, deps=deps
    )

    assert stopped == switch.STOPPED_GONE
    assert not switch.checkpoint_requested(workspace)


def test_stop_agent_prefers_a_checkpoint_over_a_kill(tmp_path):
    """Trigger 2: the window disappears during the grace period — no kill is needed."""
    workspace = tmp_path / "ws"
    liveness = iter([True, False])
    deps = switch.SwitchDeps(
        window_exists=lambda window: next(liveness),
        kill_window=_forbidden,
        sleep=lambda seconds: None,
    )

    stopped = switch.stop_agent(
        WINDOW,
        workspace,
        to_backend=TO,
        reason=switch.REASON_THRESHOLD,
        grace_sec=60,
        deps=deps,
    )

    assert stopped == switch.STOPPED_CHECKPOINTED
    assert switch.checkpoint_requested(workspace)


def test_stop_agent_kills_only_after_the_grace_period(tmp_path):
    """The hard kill is the fallback for an unresponsive implementer, never the first move."""
    workspace = tmp_path / "ws"
    order: list[str] = []
    killed: list[str] = []
    slept: list[float] = []

    def alive(window):
        order.append("check")
        return True

    def kill(window):
        order.append("kill")
        killed.append(window)

    deps = switch.SwitchDeps(
        window_exists=alive, kill_window=kill, sleep=lambda seconds: slept.append(seconds)
    )

    stopped = switch.stop_agent(
        WINDOW,
        workspace,
        to_backend=TO,
        reason=switch.REASON_THRESHOLD,
        grace_sec=12,
        deps=deps,
    )

    assert stopped == switch.STOPPED_KILLED
    assert killed == [WINDOW]
    assert order[-1] == "kill"
    assert sum(slept) == pytest.approx(12)
    assert switch.checkpoint_requested(workspace)
    # The sentinel is written before any waiting begins, not after.
    assert order.count("check") >= 2


def test_stop_agent_accepts_a_qualified_window_target(tmp_path):
    """A handle carries `agents:impl-x`; tmux liveness compares bare window names."""
    seen: list[str] = []
    deps = switch.SwitchDeps(
        window_exists=lambda window: seen.append(window) or False, sleep=_forbidden
    )

    switch.stop_agent(f"agents:{WINDOW}", None, to_backend=TO, reason="x", deps=deps)

    assert seen == [WINDOW]


# ── the handoff brief, built from the worktree ──


def test_handoff_brief_carries_every_designed_ingredient(
    dirty_worktree, previous_workspace, tmp_path
):
    new_workspace = tmp_path / "ws-new"
    brief = switch.handoff_brief(
        TASK_ID,
        dirty_worktree,
        new_workspace,
        previous_workspace=previous_workspace,
        from_backend=FROM,
        to_backend=TO,
        reason=switch.REASON_THRESHOLD,
    )

    # 1. the original brief, verbatim
    assert "teach the widget to fly" in brief
    # 2. the commits made
    assert "feat(T7): the committed half" in brief
    # 3. git diff --stat
    assert "git diff --stat" in brief
    assert "module.py" in brief
    # 3b. and the untracked file a diff --stat cannot see
    assert "?? scratch.txt" in brief
    # 4. checkpoint notes
    assert "the encoder is half-done and untested" in brief
    # 5. verification status
    assert "V1: done" in brief
    assert "V2: NOT done" in brief
    # 6. remaining steps
    assert "a decision on the wire format" in brief
    assert "V2" in brief
    assert str(new_workspace / "result.json") in brief
    assert str(new_workspace / "DONE") in brief
    # and the handover guardrail that protects the uncommitted work
    assert "Do NOT reset, clean, stash, re-clone or re-create the worktree" in brief
    assert str(dirty_worktree) in brief


def test_handoff_brief_degrades_when_the_worktree_is_not_a_repository(tmp_path):
    """Every ingredient is optional: a missing one costs a section, never the switch."""
    brief = switch.handoff_brief(TASK_ID, tmp_path / "nowhere", tmp_path / "ws")

    assert "(none — nothing has been committed yet)" in brief
    assert "(not recorded" in brief
    assert "(none left" in brief
    assert "(nothing verified yet" in brief


def test_handoff_brief_reports_the_uncommitted_files_by_name(dirty_worktree, tmp_path):
    assert "M module.py" in switch.dirty_files(dirty_worktree)
    assert "?? scratch.txt" in switch.dirty_files(dirty_worktree)
    assert "module.py" in switch.diff_stat(dirty_worktree)
    assert "the committed half" in switch.commits_made(dirty_worktree)


# ── the load-bearing test ──


def test_uncommitted_work_survives_a_switch(dirty_worktree, previous_workspace):
    """D3, the whole point of Task 5: a switch must not cost the work on disk.

    A real repository, dirty in both ways an implementer can leave one, taken through the
    real switch path. `create_worktree` and `remove_worktree` are explosives (autouse
    fixture), so the "obvious" implementation — recreate the worktree and relaunch —
    fails here rather than in production.
    """
    recorder = Recorder()
    driver = FakeDriver()
    tracked = dirty_worktree / "module.py"
    untracked = dirty_worktree / "scratch.txt"
    inode_before = tracked.stat().st_ino

    outcome = switch.switch_task(
        TASK_ID,
        reason=switch.REASON_QUOTA,
        entry=_entry(dirty_worktree),
        to_backend=TO,
        config={},
        deps=_deps(recorder, driver),
    )

    assert outcome.switched is True
    # The uncommitted work is byte-for-byte where the previous agent left it.
    assert tracked.read_text(encoding="utf-8") == DIRTY_TRACKED
    assert untracked.read_text(encoding="utf-8") == DIRTY_UNTRACKED
    assert tracked.stat().st_ino == inode_before
    # …the committed half is still there too, on the same worktree…
    assert "feat(T7): the committed half" in _git(dirty_worktree, "log", "--oneline")
    assert (dirty_worktree / ".git").exists()
    # …and the incoming agent was launched into that very worktree.
    assert driver.launched[0].worktree == dirty_worktree
    assert outcome.worktree == dirty_worktree
    # The new agent is told, in its brief, that the dirty files are its inheritance.
    assert "?? scratch.txt" in driver.launched[0].brief


def test_the_switch_reuses_the_deterministic_worktree_path(previous_workspace):
    """`worktree_path_for` is a pure function of the task id — reuse is not recreating."""
    recorder = Recorder()
    driver = FakeDriver()

    outcome = switch.switch_task(
        TASK_ID,
        reason=switch.REASON_QUOTA,
        entry=_entry(worktree=""),
        to_backend=TO,
        config={},
        deps=_deps(recorder, driver),
    )

    assert outcome.worktree == worktree_module.worktree_path_for(TASK_ID)
    assert driver.launched[0].worktree == worktree_module.worktree_path_for(TASK_ID)


def test_the_incoming_agent_gets_a_fresh_workspace(dirty_worktree, previous_workspace):
    """A new workspace, so no terminal sentinel and no old brief is inherited."""
    recorder = Recorder()
    driver = FakeDriver()

    outcome = switch.switch_task(
        TASK_ID,
        reason=switch.REASON_THRESHOLD,
        entry=_entry(dirty_worktree),
        to_backend=TO,
        config={},
        deps=_deps(recorder, driver),
    )

    assert outcome.new_session_id == NEW_SESSION
    assert driver.launched[0].workspace == switch.WORKSPACES / NEW_SESSION
    assert driver.launched[0].workspace != previous_workspace
    # The outgoing workspace is left readable — it is the evidence trail.
    assert (previous_workspace / "brief.txt").exists()
    assert (previous_workspace / "result.json").exists()


# ── journal, notification and state ──


def test_journal_record_keeps_its_five_fields(dirty_worktree, previous_workspace):
    """F5: `backend_switch` is flattened into `detail`. No sixth field."""
    recorder = Recorder()
    driver = FakeDriver()
    state.JOURNAL.parent.mkdir(parents=True, exist_ok=True)

    switch.switch_task(
        TASK_ID,
        reason=switch.REASON_THRESHOLD,
        entry=_entry(dirty_worktree),
        from_backend=FROM,
        to_backend=TO,
        config={},
        deps=_deps(recorder, driver, journal=None),  # the real append_journal
    )

    records = [
        json.loads(line)
        for line in state.JOURNAL.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    switches = [r for r in records if r["event"] == switch.SWITCH_EVENT]
    assert len(switches) == 1
    assert set(switches[0]) == {"ts", "event", "agent", "session_id", "detail"}
    assert switches[0]["detail"] == f"{TASK_ID} from={FROM} to={TO} reason=usage_threshold"
    assert switches[0]["session_id"] == OLD_SESSION


def test_the_operator_is_told_which_way_the_task_moved(dirty_worktree):
    recorder = Recorder()
    driver = FakeDriver()

    switch.switch_task(
        TASK_ID,
        reason=switch.REASON_MANUAL,
        entry=_entry(dirty_worktree, backend=FROM),
        to_backend=TO,
        config={},
        deps=_deps(recorder, driver),
    )

    assert len(recorder.notices) == 1
    message = recorder.notices[0]
    assert TASK_ID in message and FROM in message and TO in message
    assert str(dirty_worktree) in message


def test_a_broken_notifier_does_not_abort_a_switch(dirty_worktree):
    """Telegram being down is not a reason to leave a task on a spent backend."""
    recorder = Recorder()
    driver = FakeDriver()

    outcome = switch.switch_task(
        TASK_ID,
        reason=switch.REASON_QUOTA,
        entry=_entry(dirty_worktree),
        to_backend=TO,
        config={},
        deps=_deps(recorder, driver, notify=_forbidden),
    )

    assert outcome.switched is True
    assert driver.launched


def test_in_flight_entry_gains_the_backend_key_when_it_lacks_one(dirty_worktree):
    """The launch sites do not write `backend` yet; a switch must tolerate that."""
    recorder = Recorder()
    driver = FakeDriver()
    entry = _entry(dirty_worktree)
    assert "backend" not in entry
    recorder.state = {"in_flight": [dict(entry)]}

    outcome = switch.switch_task(
        TASK_ID,
        reason=switch.REASON_QUOTA,
        entry=entry,
        to_backend=TO,
        config={},
        deps=_deps(recorder, driver),
    )

    assert outcome.entry["backend"] == TO
    survivors = recorder.state["in_flight"]
    assert [e["session_id"] for e in survivors] == [NEW_SESSION]
    assert survivors[0]["backend"] == TO
    # Everything the entry already carried about the task survives untouched.
    assert survivors[0]["branch"] == entry["branch"]
    assert survivors[0]["task_id"] == TASK_ID
    assert survivors[0]["role"] == "implementer"
    assert survivors[0]["worktree"] == str(dirty_worktree)
    assert survivors[0]["window"] == WINDOW
    assert survivors[0]["status"] == "running"
    assert survivors[0]["started_at"] == "2026-08-12T10:00:00Z"


def test_an_existing_backend_key_is_read_as_the_source(dirty_worktree):
    recorder = Recorder()
    driver = FakeDriver(name=FROM)

    outcome = switch.switch_task(
        TASK_ID,
        reason=switch.REASON_QUOTA,
        entry=_entry(dirty_worktree, backend=TO),
        to_backend=FROM,
        config={},
        deps=_deps(recorder, driver),
    )

    assert outcome.from_backend == TO
    assert outcome.to_backend == FROM
    assert recorder.events(switch.SWITCH_EVENT)[0][1] == (
        f"{TASK_ID} from={TO} to={FROM} reason=quota_exhausted"
    )


def test_unrelated_in_flight_entries_are_left_alone(dirty_worktree):
    recorder = Recorder()
    driver = FakeDriver()
    other = {"session_id": "impl-T9-x", "task_id": "T9"}
    recorder.state = {"in_flight": [_entry(dirty_worktree), other]}

    switch.switch_task(
        TASK_ID,
        reason=switch.REASON_QUOTA,
        entry=_entry(dirty_worktree),
        to_backend=TO,
        config={},
        deps=_deps(recorder, driver),
    )

    assert other in recorder.state["in_flight"]
    assert OLD_SESSION not in [e["session_id"] for e in recorder.state["in_flight"]]


def test_a_state_document_that_cannot_be_read_costs_bookkeeping_not_the_switch(
    dirty_worktree,
):
    recorder = Recorder()
    driver = FakeDriver()

    outcome = switch.switch_task(
        TASK_ID,
        reason=switch.REASON_QUOTA,
        entry=_entry(dirty_worktree),
        to_backend=TO,
        config={},
        deps=_deps(recorder, driver, load_state=_forbidden),
    )

    assert outcome.switched is True
    assert "in_flight not updated" in outcome.note
    assert recorder.events(switch.SWITCH_EVENT)


# ── choosing the target: capabilities and the fallback chain ──


def test_no_target_leaves_the_implementer_running(dirty_worktree):
    """No fallback means the existing throttle-and-wait behaviour must stand unchanged."""
    recorder = Recorder()

    outcome = switch.switch_task(
        TASK_ID,
        reason=switch.REASON_THRESHOLD,
        entry=_entry(dirty_worktree, backend=FROM),
        config={},
        available=[FROM],  # nothing else is installed
        exhausted=[FROM],
        deps=switch.SwitchDeps(
            driver_factory=_forbidden,
            journal=recorder.record,
            notify=_forbidden,
            load_state=_forbidden,
            window_exists=_forbidden,
            kill_window=_forbidden,
            sleep=_forbidden,
        ),
    )

    assert outcome.switched is False
    assert outcome.to_backend is None
    assert "no target backend" in outcome.note
    assert recorder.journal == []
    # Nothing was stopped: no sentinel reached the running implementer.
    assert not switch.checkpoint_requested(switch.WORKSPACES / OLD_SESSION)


def test_a_manual_switch_to_the_current_backend_is_a_no_op(dirty_worktree):
    outcome = switch.switch_task(
        TASK_ID,
        reason=switch.REASON_MANUAL,
        entry=_entry(dirty_worktree, backend=FROM),
        to_backend=FROM,
        config={},
        deps=switch.SwitchDeps(driver_factory=_forbidden, window_exists=_forbidden),
    )

    assert outcome.switched is False


def test_an_unknown_manual_target_is_refused(dirty_worktree):
    outcome = switch.switch_task(
        TASK_ID,
        reason=switch.REASON_MANUAL,
        entry=_entry(dirty_worktree, backend=FROM),
        to_backend="a-backend-that-does-not-exist",
        config={},
        deps=switch.SwitchDeps(driver_factory=_forbidden, window_exists=_forbidden),
    )

    assert outcome.switched is False


def test_the_fallback_chain_picks_the_target_when_none_is_named():
    """With no explicit target the decision belongs to `maestro.roles`, not to this module."""
    target = switch.target_backend("implementer", FROM, config={})

    assert target in registry.known_backends()
    assert target != FROM


def test_target_backend_reads_the_configured_chain_in_both_directions():
    assert switch.target_backend("implementer", TO, config={}) == FROM
    assert switch.target_backend("implementer", FROM, config={}) == TO


# ── resume vs re-brief: decided by capability ──


def test_a_driver_that_declares_native_resume_is_resumed(dirty_worktree, previous_workspace):
    recorder = Recorder()
    driver = FakeDriver(capabilities=RESUMING)
    handle = Handle(
        backend=TO,
        session_id=OLD_SESSION,
        native_id="thread-abc",
        workspace=previous_workspace,
        window=WINDOW,
    )

    outcome = switch.switch_task(
        TASK_ID,
        reason=switch.REASON_MANUAL,
        entry=_entry(dirty_worktree, backend=FROM),
        to_backend=TO,
        handle=handle,
        config={},
        deps=_deps(recorder, driver),
    )

    assert outcome.resumed is True
    assert driver.launched == []
    assert len(driver.resumed) == 1
    resumed_handle, prompt = driver.resumed[0]
    assert resumed_handle is handle
    assert "You are taking over task T7" in prompt


def test_a_driver_without_native_resume_is_re_briefed(dirty_worktree, previous_workspace):
    recorder = Recorder()
    driver = FakeDriver(capabilities=REBRIEFING)
    handle = Handle(
        backend=TO,
        session_id=OLD_SESSION,
        native_id="thread-abc",
        workspace=previous_workspace,
        window=WINDOW,
    )

    outcome = switch.switch_task(
        TASK_ID,
        reason=switch.REASON_MANUAL,
        entry=_entry(dirty_worktree, backend=FROM),
        to_backend=TO,
        handle=handle,
        config={},
        deps=_deps(recorder, driver),
    )

    assert outcome.resumed is False
    assert driver.resumed == []
    assert len(driver.launched) == 1
    assert "You are taking over task T7" in driver.launched[0].brief


def test_a_handle_belonging_to_another_backend_is_not_resumed(
    dirty_worktree, previous_workspace
):
    """A resume token is the *other* driver's; only its owner can spend it."""
    recorder = Recorder()
    driver = FakeDriver(capabilities=RESUMING)
    handle = Handle(
        backend=FROM,
        session_id=OLD_SESSION,
        native_id="uuid-of-the-outgoing-session",
        workspace=previous_workspace,
        window=WINDOW,
    )

    outcome = switch.switch_task(
        TASK_ID,
        reason=switch.REASON_QUOTA,
        entry=_entry(dirty_worktree, backend=FROM),
        to_backend=TO,
        handle=handle,
        config={},
        deps=_deps(recorder, driver),
    )

    assert outcome.resumed is False
    assert driver.resumed == []
    assert driver.launched


def test_a_handle_without_a_native_id_is_not_resumed(dirty_worktree, previous_workspace):
    recorder = Recorder()
    driver = FakeDriver(capabilities=RESUMING)
    handle = Handle(
        backend=TO,
        session_id=OLD_SESSION,
        native_id=None,
        workspace=previous_workspace,
        window=WINDOW,
    )

    outcome = switch.switch_task(
        TASK_ID,
        reason=switch.REASON_QUOTA,
        entry=_entry(dirty_worktree, backend=FROM),
        to_backend=TO,
        handle=handle,
        config={},
        deps=_deps(recorder, driver),
    )

    assert outcome.resumed is False
    assert driver.launched


def test_the_system_prompt_file_is_gated_on_the_capability(dirty_worktree, tmp_path):
    """Capability, never name — the same body, both answers."""
    profile = tmp_path / "implementer_sys.md"
    profile.write_text("lean profile", encoding="utf-8")

    specs = {}
    for label, capabilities in (("with", PROFILED), ("without", REBRIEFING)):
        recorder = Recorder()
        driver = FakeDriver(capabilities=capabilities)
        import maestro.switch as module

        original = module.SYS_PROMPT
        module.SYS_PROMPT = profile
        try:
            switch.switch_task(
                TASK_ID,
                reason=switch.REASON_QUOTA,
                entry=_entry(dirty_worktree, backend=FROM),
                to_backend=TO,
                config={},
                deps=_deps(recorder, driver),
            )
        finally:
            module.SYS_PROMPT = original
        specs[label] = driver.launched[0]

    assert specs["with"].system_prompt_file == profile
    assert specs["without"].system_prompt_file is None


def test_the_profile_is_folded_into_the_brief_when_the_capability_is_false(
    dirty_worktree, tmp_path
):
    """A3: a driver that can't take `--system-prompt-file` must not lose the profile
    outright — the same text rides along inside the brief instead."""
    profile = tmp_path / "implementer.md"
    profile.write_text("PROFILE-TEXT-MARKER", encoding="utf-8")

    import maestro.switch as module

    original = module.SYS_PROMPT
    module.SYS_PROMPT = profile
    try:
        recorder = Recorder()
        driver = FakeDriver(capabilities=REBRIEFING)  # system_prompt_file=False
        switch.switch_task(
            TASK_ID,
            reason=switch.REASON_QUOTA,
            entry=_entry(dirty_worktree, backend=FROM),
            to_backend=TO,
            config={},
            deps=_deps(recorder, driver),
        )
    finally:
        module.SYS_PROMPT = original

    spec = driver.launched[0]
    assert spec.system_prompt_file is None
    assert spec.brief.startswith("PROFILE-TEXT-MARKER")
    assert "You are taking over task T7" in spec.brief


def test_the_profile_is_not_duplicated_into_the_brief_when_the_capability_is_true(
    dirty_worktree, tmp_path
):
    """A driver that *can* take the file gets it only that way — not both places."""
    profile = tmp_path / "implementer.md"
    profile.write_text("PROFILE-TEXT-MARKER", encoding="utf-8")

    import maestro.switch as module

    original = module.SYS_PROMPT
    module.SYS_PROMPT = profile
    try:
        recorder = Recorder()
        driver = FakeDriver(capabilities=PROFILED)  # system_prompt_file=True
        switch.switch_task(
            TASK_ID,
            reason=switch.REASON_QUOTA,
            entry=_entry(dirty_worktree, backend=FROM),
            to_backend=TO,
            config={},
            deps=_deps(recorder, driver),
        )
    finally:
        module.SYS_PROMPT = original

    spec = driver.launched[0]
    assert spec.system_prompt_file == profile
    assert "PROFILE-TEXT-MARKER" not in spec.brief


def test_capabilities_are_consulted_exactly_once_per_switch(dirty_worktree):
    recorder = Recorder()
    driver = FakeDriver()

    switch.switch_task(
        TASK_ID,
        reason=switch.REASON_QUOTA,
        entry=_entry(dirty_worktree),
        to_backend=TO,
        config={},
        deps=_deps(recorder, driver),
    )

    assert driver.capability_calls == 1


# ── the threshold trigger ──


def test_threshold_is_window_agnostic(dirty_worktree):
    """G5: an account whose only window is weekly must still be able to cross a threshold."""
    weekly_only = Usage(windows={10080: WindowUsage(used_pct=80.0, resets_at=1787036664)})

    assert weekly_only.five_hour is None  # a name-keyed threshold could never fire
    assert switch.threshold_crossed(weekly_only) is True
    assert switch.threshold_crossed(weekly_only, threshold=95.0) is False


def test_threshold_fires_on_the_most_consumed_window():
    both = Usage(
        windows={300: WindowUsage(used_pct=10.0), 10080: WindowUsage(used_pct=99.0)}
    )

    assert switch.threshold_crossed(both) is True


def test_an_unmeasurable_sample_does_not_fire_the_threshold():
    """G6: `None` means "not measurable here", never "no pressure" — and never a switch."""
    assert switch.threshold_crossed(None) is False
    assert switch.threshold_crossed(Usage()) is False
    assert switch.threshold_crossed(Usage(context_used_pct=None, windows={})) is False
    assert switch.threshold_crossed(Usage(windows={10080: WindowUsage()})) is False


def test_the_switch_threshold_is_below_the_existing_throttle():
    """Provisional, but it must fire before the throttle starts starving the queue."""
    from maestro import quota

    assert switch.SWITCH_THRESHOLD_PCT < quota.THROTTLE_75_PCT
    assert switch.SWITCH_THRESHOLD_PCT is not quota.THROTTLE_75_PCT


# ── failure ──


def test_a_failed_relaunch_reports_an_unswitched_outcome(dirty_worktree):
    recorder = Recorder()

    class Broken(FakeDriver):
        def launch(self, spec):
            raise RuntimeError("tmux refused the window")

    outcome = switch.switch_task(
        TASK_ID,
        reason=switch.REASON_QUOTA,
        entry=_entry(dirty_worktree),
        to_backend=TO,
        config={},
        deps=_deps(recorder, Broken()),
    )

    assert outcome.switched is False
    assert "tmux refused the window" in outcome.note
    assert recorder.events(switch.SWITCH_EVENT) == []
    failures = recorder.events(switch.SWITCH_FAILED_EVENT)
    assert len(failures) == 1
    assert failures[0][1].startswith(f"{TASK_ID} from=")
    assert recorder.notices == []
    # The work is still on disk, whatever happened to the relaunch.
    assert (dirty_worktree / "module.py").read_text(encoding="utf-8") == DIRTY_TRACKED


def test_all_three_triggers_take_the_same_path(dirty_worktree, previous_workspace):
    """One path, three reasons: only the stopping differs, and only by liveness."""
    outcomes = {}
    for reason, alive in (
        (switch.REASON_QUOTA, False),
        (switch.REASON_THRESHOLD, True),
        (switch.REASON_MANUAL, True),
    ):
        recorder = Recorder()
        driver = FakeDriver()
        liveness = iter([alive, False])
        outcomes[reason] = switch.switch_task(
            TASK_ID,
            reason=reason,
            entry=_entry(dirty_worktree, backend=FROM),
            to_backend=TO,
            grace_sec=30,
            config={},
            deps=_deps(
                recorder, driver, window_exists=lambda window: next(liveness, False)
            ),
        )
        assert driver.launched[0].worktree == dirty_worktree
        assert recorder.events(switch.SWITCH_EVENT)[0][1].endswith(f"reason={reason}")

    assert all(outcome.switched for outcome in outcomes.values())
    assert outcomes[switch.REASON_QUOTA].stopped == switch.STOPPED_GONE
    assert outcomes[switch.REASON_THRESHOLD].stopped == switch.STOPPED_CHECKPOINTED
    assert outcomes[switch.REASON_MANUAL].stopped == switch.STOPPED_CHECKPOINTED
