"""The orchestrator's two switch hooks, and the `backend` key on an `in_flight` entry.

M2 gives the event loop somewhere to send work when a backend runs out, instead of only
somewhere to wait. Two paths change (plan `docs/plans/2026-08-12-m2-backends.md`, Task 6
steps 3–5):

* **proactive** — `main`'s usage-threshold block. Above the switch threshold, in-flight
  tasks move to a fallback backend rather than being throttled and then paused;
* **reactive** — `reconcile_in_flight`'s usage-limit net. A limit-shaped exit hands the
  task to a fallback instead of pausing the whole loop until the reset.

The load-bearing property in both is the *negative* one: with no fallback available —
nothing installed to move to, no worktree left to hand over, or a switch that failed —
the pre-M2 throttle/pause behaviour must run exactly as it always did. Every test here
that asserts a switch has a sibling asserting that untouched legacy path.

Nothing in this module launches an agent: `switch_task` is stubbed everywhere it is
reachable, backend discovery is stubbed, and no `subprocess` call is left unpatched.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from maestro import orchestrator
from maestro.backends.base import Capabilities, Usage, WindowUsage
from maestro.switch import REASON_QUOTA, REASON_THRESHOLD, SwitchOutcome

#: Repo root. Named `REPO` to match the house convention for `Path`-valued globals.
REPO = Path(__file__).resolve().parents[1]

CLAUDE = "claude"
CODEX = "codex"


# ── helpers ──


def _entry(tmp_path: Path, *, sid="impl-T1-1", task_id="T1", backend=CLAUDE,
           role="implementer", worktree=None, **extra) -> dict:
    """An `in_flight` entry whose worktree really exists, as a live one's would."""
    if worktree is None:
        worktree = tmp_path / f"wt-{task_id}"
        worktree.mkdir(parents=True, exist_ok=True)
    entry = {
        "session_id": sid,
        "task_id": task_id,
        "role": role,
        "worktree": str(worktree),
        "window": f"impl-{task_id}",
        "branch": f"impl-{task_id.lower()}-1",
        "started_at": "2026-08-12T00:00:00Z",
        "status": "running",
        "backend": backend,
    }
    entry.update(extra)
    return entry


def _switched_entry(old: dict, to_backend=CODEX, sid="impl-T1-2") -> dict:
    new = dict(old)
    new.update({"session_id": sid, "backend": to_backend})
    return new


class _FakeDriver:
    """A driver that reports a usage sample without touching a binary."""

    def __init__(self, used_pct: float | None, telemetry: bool = True):
        self._used_pct = used_pct
        self._telemetry = telemetry

    def capabilities(self) -> Capabilities:
        return Capabilities(usage_telemetry=self._telemetry)

    def usage(self):
        if self._used_pct is None:
            return None
        return Usage(windows={300: WindowUsage(used_pct=self._used_pct)})


@pytest.fixture
def hooks(monkeypatch, tmp_path):
    """Every seam the switch hooks reach, stubbed and recorded.

    `switch_task` is replaced by default, so no test in this module can start an agent.
    """
    box = SimpleNamespace(
        switches=[],
        journal=[],
        launch_times_persisted=[],
        pauses=[],
        outcome=None,
        raise_on_switch=None,
        available=(CLAUDE, CODEX),
        usage_pct=95.0,
    )

    def _switch_task(task_id, **kwargs):
        box.switches.append(SimpleNamespace(task_id=task_id, **kwargs))
        if box.raise_on_switch is not None:
            raise box.raise_on_switch
        entry = kwargs.get("entry") or {}
        if box.outcome is not None:
            return box.outcome
        new_entry = _switched_entry(dict(entry))
        # The real `switch_task` persists the replacement entry through
        # `_replace_in_flight`, so the next poll's `reconcile_in_flight` reads the
        # *switched* task, not the original. Mirror that here — a fixture that keeps
        # handing back the pre-switch entry would hide a switch loop rather than expose
        # one.
        live = getattr(box, "in_flight", None)
        if live is not None:
            box.in_flight = [new_entry if e.get("session_id") == entry.get("session_id")
                             else e for e in live]
        return SwitchOutcome(
            task_id=task_id,
            reason=kwargs.get("reason", ""),
            from_backend=kwargs.get("from_backend", CLAUDE),
            to_backend=CODEX,
            switched=True,
            entry=new_entry,
            new_session_id=new_entry["session_id"],
        )

    monkeypatch.setattr(orchestrator, "switch_task", _switch_task)
    monkeypatch.setattr(orchestrator, "_available_backends", lambda: box.available)
    monkeypatch.setattr(
        orchestrator, "get_backend", lambda name: _FakeDriver(box.usage_pct)
    )
    monkeypatch.setattr(
        orchestrator,
        "append_journal",
        lambda event, detail="", session_id="": box.journal.append(
            (event, detail, session_id)
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_persist_launch_time",
        lambda sid, ts: box.launch_times_persisted.append(sid),
    )
    monkeypatch.setattr(orchestrator, "WORKSPACES", tmp_path / "workspaces")
    (tmp_path / "workspaces").mkdir(parents=True, exist_ok=True)
    return box


# =======================================================================================
# Step 3 — the `backend` key on an in_flight entry
# =======================================================================================


def _launch_site_entries() -> list[ast.Dict]:
    """Every implementer `in_flight` entry literal in the orchestrator's source.

    Read out of the source rather than out of a live launch because the second site sits
    inside `main`'s 600-line body, which cannot be reached without launching an agent.
    """
    tree = ast.parse((REPO / "maestro" / "orchestrator.py").read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        value = getattr(node, "value", None)
        if not isinstance(value, ast.Dict):
            continue
        keys = [k.value for k in value.keys if isinstance(k, ast.Constant)]
        roles = [
            v.value
            for k, v in zip(value.keys, value.values)
            if isinstance(k, ast.Constant) and k.value == "role" and isinstance(v, ast.Constant)
        ]
        if "session_id" in keys and roles == ["implementer"]:
            found.append(value)
    return found


def test_both_implementer_launch_sites_record_a_backend():
    sites = _launch_site_entries()
    assert len(sites) == 2, "expected exactly the retry and the main-loop launch sites"
    for site in sites:
        keys = [k.value for k in site.keys if isinstance(k, ast.Constant)]
        assert keys == [
            "session_id", "task_id", "role", "worktree", "window",
            "branch", "started_at", "status", "backend",
        ]


def test_do_retry_records_the_backend_the_retry_runs_on(monkeypatch, tmp_path):
    """The retry's entry carries the backend `launch_implementer` resolves, and every
    pre-existing key keeps its spelling, its value and its place."""
    state: dict = {"in_flight": []}
    monkeypatch.setattr(orchestrator, "WORKSPACES", tmp_path / "workspaces")
    monkeypatch.setattr(orchestrator, "worktree_path_for", lambda tid: tmp_path / f"wt-{tid}")
    monkeypatch.setattr(orchestrator, "create_worktree", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "launch_implementer", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "parse_runnable_tasks", lambda: [])
    monkeypatch.setattr(orchestrator, "append_journal", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "read_state", lambda: state)
    monkeypatch.setattr(orchestrator, "write_state", lambda new: state.update(new))
    monkeypatch.setattr(orchestrator, "_launch_backend", lambda: CODEX)

    old = _entry(tmp_path)
    in_flight = [old]
    orchestrator._do_retry("T1", old, "boom", {}, in_flight)

    new = in_flight[-1]
    assert new["backend"] == CODEX
    assert new["role"] == "implementer"
    assert new["status"] == "running"
    assert new["window"] == "impl-T1"
    assert list(new)[-1] == "backend"          # appended, nothing reordered


def test_launch_backend_is_pure_configuration(monkeypatch):
    """Recording the backend must not probe a binary or spawn anything: it is read from
    `maestro.roles`, on the launch path, once per launch."""
    monkeypatch.setattr(
        orchestrator, "backend_for", lambda role: f"resolved-{role}"
    )
    monkeypatch.setattr(
        orchestrator,
        "_available_backends",
        lambda: pytest.fail("recording the backend must not probe binaries"),
    )
    assert orchestrator._launch_backend() == "resolved-implementer"


def test_launch_backend_degrades_to_empty_when_resolution_breaks(monkeypatch):
    def _boom(role):
        raise RuntimeError("no config")

    monkeypatch.setattr(orchestrator, "backend_for", _boom)
    assert orchestrator._launch_backend() == ""


# =======================================================================================
# The shared guards
# =======================================================================================


def test_a_switch_needs_a_worktree_that_still_exists(tmp_path):
    """D3 exists to preserve the worktree's uncommitted work; with the worktree gone
    there is nothing to hand over and a "switch" would be a fresh start in disguise."""
    assert orchestrator._handover_ready(_entry(tmp_path)) is True
    gone = _entry(tmp_path, worktree=tmp_path / "nonexistent")
    assert orchestrator._handover_ready(gone) is False


def test_a_script_task_is_never_switched(tmp_path):
    assert orchestrator._handover_ready(_entry(tmp_path, role="script")) is False


def test_an_entry_without_a_backend_key_falls_back_to_the_configured_one(monkeypatch, tmp_path):
    monkeypatch.setattr(orchestrator, "_launch_backend", lambda: CLAUDE)
    entry = _entry(tmp_path)
    entry.pop("backend")
    assert orchestrator._entry_backend(entry) == CLAUDE
    assert orchestrator._entry_backend(_entry(tmp_path, backend=CODEX)) == CODEX


def test_usage_pressure_reads_the_backends_own_sample(monkeypatch):
    """A backend with quota to spare is not under pressure just because the loop's own
    reading is high — otherwise a task would be moved *onto* the exhausted backend."""
    monkeypatch.setattr(orchestrator, "get_backend", lambda name: _FakeDriver(10.0))
    assert orchestrator._under_usage_pressure(CODEX, 99.0) is False
    monkeypatch.setattr(orchestrator, "get_backend", lambda name: _FakeDriver(99.0))
    assert orchestrator._under_usage_pressure(CODEX, 0.0) is True


def test_usage_pressure_falls_back_to_the_loops_reading_without_a_sample(monkeypatch):
    """No sample is never read as "no pressure" (G6): the loop's own number decides."""
    monkeypatch.setattr(orchestrator, "get_backend", lambda name: _FakeDriver(None))
    assert orchestrator._under_usage_pressure(CLAUDE, 95.0) is True
    assert orchestrator._under_usage_pressure(CLAUDE, 10.0) is False

    monkeypatch.setattr(
        orchestrator, "get_backend", lambda name: _FakeDriver(1.0, telemetry=False)
    )
    assert orchestrator._under_usage_pressure(CLAUDE, 95.0) is True


def test_usage_pressure_survives_a_driver_that_cannot_be_built(monkeypatch):
    def _boom(name):
        raise RuntimeError("no driver")

    monkeypatch.setattr(orchestrator, "get_backend", _boom)
    assert orchestrator._under_usage_pressure(CLAUDE, 95.0) is True


def test_no_fallback_means_no_switch(hooks, tmp_path):
    """The real `roles.fallback_backend` runs here: with only the current backend
    installed there is nowhere to go, and nothing is stopped."""
    hooks.available = (CLAUDE,)
    assert orchestrator._switch_instead_of_waiting(_entry(tmp_path), REASON_THRESHOLD) is None
    assert hooks.switches == []


def test_a_failed_switch_is_journalled_and_reported_as_no_switch(hooks, tmp_path):
    hooks.raise_on_switch = RuntimeError("tmux is gone")
    assert orchestrator._switch_instead_of_waiting(_entry(tmp_path), REASON_THRESHOLD) is None
    assert [event for event, _, _ in hooks.journal] == ["backend_switch_failed"]
    detail = hooks.journal[0][1]
    assert "T1" in detail and "tmux is gone" in detail


def test_a_refused_switch_is_reported_as_no_switch(hooks, tmp_path):
    """`switch_task` answering "nowhere to go" is a normal result, not an exception."""
    hooks.outcome = SwitchOutcome(
        task_id="T1", reason=REASON_THRESHOLD, from_backend=CLAUDE, switched=False
    )
    assert orchestrator._switch_instead_of_waiting(_entry(tmp_path), REASON_THRESHOLD) is None
    assert hooks.journal == []


def test_a_switch_re_dates_the_launch_clock(hooks, tmp_path):
    """The incoming session starts its timeout window now — inheriting the outgoing
    session's clock would time the new agent out early."""
    old = _entry(tmp_path)
    launch_times = {old["session_id"]: 1.0}
    new = orchestrator._switch_instead_of_waiting(old, REASON_THRESHOLD, launch_times)
    assert new is not None
    assert old["session_id"] not in launch_times
    assert launch_times[new["session_id"]] > 1.0
    assert hooks.launch_times_persisted == [new["session_id"]]


# =======================================================================================
# Step 4 — the proactive threshold hook
# =======================================================================================


def test_threshold_switch_moves_an_in_flight_task_when_a_fallback_exists(hooks, tmp_path):
    old = _entry(tmp_path)
    in_flight = [old]
    launch_times: dict = {}

    moved = orchestrator._threshold_switches(in_flight, launch_times, 95.0)

    assert moved == 1
    assert len(hooks.switches) == 1
    call = hooks.switches[0]
    assert call.task_id == "T1"
    assert call.reason == REASON_THRESHOLD
    assert call.from_backend == CLAUDE
    assert call.available == (CLAUDE, CODEX)
    assert [e["session_id"] for e in in_flight] == ["impl-T1-2"]
    assert in_flight[0]["backend"] == CODEX
    assert in_flight[0]["worktree"] == old["worktree"]      # same worktree, D3


def test_threshold_switch_is_inert_below_the_threshold(hooks, tmp_path, monkeypatch):
    """Under the threshold the hook reaches no backend at all — the ordinary poll must
    not pay for a usage sample or a binary probe."""
    monkeypatch.setattr(
        orchestrator, "get_backend", lambda name: pytest.fail("sampled below threshold")
    )
    monkeypatch.setattr(
        orchestrator, "_available_backends", lambda: pytest.fail("probed below threshold")
    )
    in_flight = [_entry(tmp_path)]
    assert orchestrator._threshold_switches(in_flight, {}, orchestrator.SWITCH_THRESHOLD_PCT - 0.1) == 0
    assert hooks.switches == []
    assert [e["session_id"] for e in in_flight] == ["impl-T1-1"]


def test_threshold_switch_is_inert_with_nothing_in_flight(hooks):
    assert orchestrator._threshold_switches([], {}, 99.0) == 0
    assert hooks.switches == []


def test_threshold_switch_reports_zero_when_no_fallback_exists(hooks, tmp_path):
    """The answer `main` depends on: nothing moved, so the throttle/pause stands."""
    hooks.available = (CLAUDE,)
    in_flight = [_entry(tmp_path)]
    assert orchestrator._threshold_switches(in_flight, {}, 99.0) == 0
    assert hooks.switches == []
    assert [e["session_id"] for e in in_flight] == ["impl-T1-1"]


def test_threshold_switch_leaves_a_task_on_an_unpressured_backend_alone(hooks, tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "get_backend", lambda name: _FakeDriver(5.0))
    in_flight = [_entry(tmp_path, backend=CODEX)]
    assert orchestrator._threshold_switches(in_flight, {}, 99.0) == 0
    assert hooks.switches == []


def test_threshold_switch_skips_a_script_task_and_a_lost_worktree(hooks, tmp_path):
    script = _entry(tmp_path, sid="script-T2-1", task_id="T2", role="script")
    lost = _entry(tmp_path, sid="impl-T3-1", task_id="T3",
                  worktree=tmp_path / "gone-T3")
    in_flight = [script, lost]
    assert orchestrator._threshold_switches(in_flight, {}, 99.0) == 0
    assert hooks.switches == []
    assert in_flight == [script, lost]


# =======================================================================================
# Step 5 — the reactive usage-limit hook in reconcile_in_flight
# =======================================================================================


@pytest.fixture
def reconcile_env(hooks, monkeypatch, tmp_path):
    """A window-gone entry whose impl.log looks like a usage-limit exit."""
    monkeypatch.setattr(orchestrator, "tmux_window_exists", lambda window: False)
    monkeypatch.setattr(
        orchestrator,
        "_scan_impl_log_for_limit",
        lambda workspace: {
            "limit": True,
            "reset_iso": "2026-08-12T18:00:00Z",
            "evidence": "You've hit your usage limit",
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "_pause_for_usage_limit",
        lambda task_id, reset_iso, evidence, workspace: hooks.pauses.append(
            (task_id, reset_iso, evidence, Path(workspace).name)
        ),
    )
    monkeypatch.setattr(orchestrator, "_tail_text", lambda path, n=15: "")
    hooks.entry = _entry(tmp_path)
    (orchestrator.WORKSPACES / hooks.entry["session_id"]).mkdir(parents=True, exist_ok=True)
    return hooks


def test_reconcile_switches_instead_of_pausing_on_a_usage_limit(reconcile_env):
    launch_times: dict = {}
    surviving = orchestrator.reconcile_in_flight(
        {"in_flight": [reconcile_env.entry]}, launch_times
    )

    assert reconcile_env.pauses == []                       # the loop is not paused
    assert [e["session_id"] for e in surviving] == ["impl-T1-2"]
    assert surviving[0]["backend"] == "codex"
    call = reconcile_env.switches[0]
    assert call.reason == REASON_QUOTA
    assert call.from_backend == "claude"
    assert launch_times == {"impl-T1-2": pytest.approx(launch_times["impl-T1-2"])}


def test_reconcile_still_pauses_when_no_fallback_exists(reconcile_env):
    """The pre-M2 path, byte for byte: pause with the scan's reset and evidence, and
    drop the entry from in_flight so nothing is marked FAILED."""
    reconcile_env.available = (CLAUDE,)
    surviving = orchestrator.reconcile_in_flight({"in_flight": [reconcile_env.entry]}, {})

    assert surviving == []
    assert reconcile_env.pauses == [
        ("T1", "2026-08-12T18:00:00Z", "You've hit your usage limit", "impl-T1-1")
    ]
    workspace = orchestrator.WORKSPACES / "impl-T1-1"
    assert not (workspace / "FAILED").exists()


def test_reconcile_still_pauses_when_the_worktree_is_gone(reconcile_env, tmp_path):
    reconcile_env.entry["worktree"] = str(tmp_path / "removed")
    surviving = orchestrator.reconcile_in_flight({"in_flight": [reconcile_env.entry]}, {})
    assert surviving == []
    assert [p[0] for p in reconcile_env.pauses] == ["T1"]
    assert reconcile_env.switches == []


def test_reconcile_still_pauses_when_the_switch_fails(reconcile_env):
    reconcile_env.raise_on_switch = RuntimeError("relaunch failed")
    surviving = orchestrator.reconcile_in_flight({"in_flight": [reconcile_env.entry]}, {})
    assert surviving == []
    assert [p[0] for p in reconcile_env.pauses] == ["T1"]
    assert "backend_switch_failed" in [event for event, _, _ in reconcile_env.journal]


# =======================================================================================
# The whole loop: main's threshold block
# =======================================================================================


@pytest.fixture
def loop_env(hooks, monkeypatch, tmp_path):
    """One poll cycle of `main`, with every seam stubbed. Nothing here reaches tmux,
    Telegram, the roadmap or a backend binary."""
    box = hooks
    box.state = {"in_flight": [], "retry_counts": {}}
    box.cap = (0, 95.0)
    box.polls = 0

    def _read_state():
        return dict(box.state)

    def _write_state(new):
        box.state.update(new)

    def _cap():
        box.polls += 1
        if box.polls > 1:                      # one cycle, then stop the loop
            box.state["halted"] = True
        return box.cap

    for name in ("run_dep_map", "run_status", "poll_control_commands", "_send_hitl_reminders",
                 "poll_proposal_answer", "apply_ready_self_fixes", "apply_ready_redo",
                 "poll_awaiting_verifications", "maybe_push_roadmap_map_change",
                 "notify_telegram"):
        monkeypatch.setattr(orchestrator, name, lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "parse_runnable_tasks", lambda: [])
    monkeypatch.setattr(orchestrator, "parse_prep_tasks", lambda: [])
    monkeypatch.setattr(orchestrator, "get_completed_task_ids", lambda: set())
    monkeypatch.setattr(orchestrator, "get_task_by_id", lambda tid: None)
    monkeypatch.setattr(orchestrator, "read_state", _read_state)
    monkeypatch.setattr(orchestrator, "write_state", _write_state)
    monkeypatch.setattr(orchestrator, "get_effective_cap", _cap)
    monkeypatch.setattr(orchestrator, "HALT_FILE", tmp_path / "no-halt")
    monkeypatch.setattr(orchestrator, "ROADMAP_FILE", tmp_path / "no-roadmap.md")
    monkeypatch.setattr(orchestrator, "AUTO_PROPOSE", False)
    monkeypatch.setattr(
        orchestrator, "subprocess",
        SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr="")),
    )
    monkeypatch.setattr(
        orchestrator, "time", SimpleNamespace(time=lambda: 1000.0, sleep=lambda s: None)
    )
    box.in_flight = [_entry(tmp_path)]
    monkeypatch.setattr(
        orchestrator, "reconcile_in_flight", lambda state, launch_times: list(box.in_flight)
    )
    return box


def test_main_switches_instead_of_pausing_when_a_fallback_exists(loop_env):
    """The switching poll does not pause, and the task moves exactly once.

    The second poll *does* pause: the task is on the fallback by then and
    `_backends_tried` has ruled out going back, so there is nothing left to move and the
    pre-M2 path takes over. That is the intended shape — a switch buys the work a new
    backend, not the loop an exemption from a quota it really has hit.
    """
    assert orchestrator.main() == 0
    events = [event for event, _, _ in loop_env.journal]
    assert [call.reason for call in loop_env.switches] == [REASON_THRESHOLD]
    assert loop_env.polls == 2                              # it survived to a second poll
    assert "concurrency_throttled" not in events
    assert events == ["rate_limit_pause"]                   # from the second poll, not the first


def test_a_task_already_switched_once_is_not_switched_back(loop_env, tmp_path):
    """No ping-pong: the outgoing backend is exhausted for the rest of this task's life.

    The loop's `five_pct` reading is about the backend it launched on, and a driver that
    cannot sample its own usage reports nothing (G6), so without the exhausted set the
    same task would be swapped back and forth once per poll — losing its in-progress work
    every time.
    """
    loop_env.in_flight = [_entry(tmp_path, sid="impl-T1-2", backend=CODEX,
                                 backends_tried=[CLAUDE])]
    assert orchestrator.main() == 0
    assert loop_env.switches == []
    assert [event for event, _, _ in loop_env.journal] == ["rate_limit_pause"]


def test_main_pauses_exactly_as_before_when_no_fallback_exists(loop_env):
    """The pre-M2 behaviour on the same reading, unchanged: pause and break.

    No `concurrency_throttled` here, and that is the reference behaviour rather than a
    gap: `prev_cap` starts at the `-1` sentinel, whose whole purpose is to suppress the
    throttle notice on the first reading of a fresh process.
    """
    loop_env.available = (CLAUDE,)
    assert orchestrator.main() == 0
    events = [event for event, _, _ in loop_env.journal]
    assert events == ["rate_limit_pause"]
    assert loop_env.journal[0][1] == "five_h=95%"
    assert loop_env.switches == []
    assert loop_env.polls == 1                              # broke out of the loop


def test_main_leaves_an_unthrottled_poll_untouched(loop_env):
    """Below the switch threshold nothing changes at all: no switch, no throttle."""
    loop_env.cap = (orchestrator.CONCURRENCY_CAP, 10.0)
    assert orchestrator.main() == 0
    assert loop_env.switches == []
    assert [event for event, _, _ in loop_env.journal] == ["halt_respected"]
