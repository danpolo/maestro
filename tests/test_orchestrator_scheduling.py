"""P08 wiring in `orchestrator.main`: launch ordering, agent-only slots, the host ledger.

* A ready `kind: script` task must never be delayed by full implementer slots (P08
  §1.3). The launch loop used to `break` at the first candidate that needed a slot,
  which stopped a script task queued behind an agent task.
* Every agent launch reserves a slot in the host ledger before its worktree exists, and
  the reservation goes away once the entry leaves `in_flight`.

`conftest._isolated_host_ledger` points the ledger at a temp file for every test.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from maestro import orchestrator
from maestro.backends.base import Capabilities, Usage, WindowUsage
from maestro.control.resources import HostReservationLedger

CLAUDE = "claude"


class _FakeDriver:
    def capabilities(self) -> Capabilities:
        return Capabilities(usage_telemetry=True)

    def usage(self):
        return Usage(windows={300: WindowUsage(used_pct=10.0)})


def _entry(tmp_path: Path, *, sid="impl-T1-1", task_id="T1", role="implementer") -> dict:
    worktree = tmp_path / f"wt-{sid}"
    worktree.mkdir(parents=True, exist_ok=True)
    return {
        "session_id": sid, "task_id": task_id, "role": role, "worktree": str(worktree),
        "window": f"impl-{task_id}", "started_at": "2026-08-12T00:00:00Z",
        "status": "running", "backend": CLAUDE,
    }


def _agent_task(task_id: str, **extra) -> dict:
    return {"id": task_id, "title": f"agent {task_id}", **extra}


def _script_task(task_id: str) -> dict:
    return {"id": task_id, "title": f"script {task_id}", "kind": "script", "run": "true"}


@pytest.fixture
def loop_env(monkeypatch, tmp_path):
    """One poll cycle of `main` with every seam that reaches tmux, git, Telegram or a
    backend stubbed. `box.runnable` feeds the launch loop; `box.launched` records agent
    launches."""
    box = SimpleNamespace(state={"in_flight": [], "retry_counts": {}}, polls=0,
                          cap=(1, 10.0), in_flight=[], runnable=[], launched=[], journal=[])

    def _cap():
        box.polls += 1
        return box.cap

    def _sleep(seconds):
        # End of the first poll: stop before a finished script task reaches the gate.
        box.state["halted"] = True

    for name in ("run_dep_map", "run_status", "poll_control_commands", "_send_hitl_reminders",
                 "poll_proposal_answer", "apply_ready_self_fixes", "apply_ready_redo",
                 "poll_awaiting_verifications", "maybe_push_roadmap_map_change",
                 "notify_telegram", "_persist_launch_time", "create_worktree"):
        monkeypatch.setattr(orchestrator, name, lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "parse_runnable_tasks", lambda: list(box.runnable))
    monkeypatch.setattr(orchestrator, "parse_prep_tasks", lambda: [])
    monkeypatch.setattr(orchestrator, "_load_roadmap_tasks", lambda: list(box.runnable))
    monkeypatch.setattr(orchestrator, "get_completed_task_ids", lambda: set())
    monkeypatch.setattr(orchestrator, "get_task_by_id", lambda tid: None)
    monkeypatch.setattr(orchestrator, "_questions_ready", lambda task: True)
    monkeypatch.setattr(
        orchestrator, "append_journal",
        lambda event, detail="", session_id="": box.journal.append((event, detail)),
    )
    monkeypatch.setattr(orchestrator, "read_state", lambda: dict(box.state))
    monkeypatch.setattr(orchestrator, "write_state", lambda new: box.state.update(new))
    monkeypatch.setattr(orchestrator, "get_effective_cap", _cap)
    monkeypatch.setattr(orchestrator, "HALT_FILE", tmp_path / "no-halt")
    monkeypatch.setattr(orchestrator, "ROADMAP_FILE", tmp_path / "no-roadmap.md")
    monkeypatch.setattr(orchestrator, "WORKSPACES", tmp_path / "workspaces")
    monkeypatch.setattr(orchestrator, "USAGE_JSON", tmp_path / "usage.json")
    monkeypatch.setattr(orchestrator, "worktree_path_for", lambda tid: tmp_path / f"wt-{tid}")
    monkeypatch.setattr(orchestrator, "AUTO_PROPOSE", False)
    monkeypatch.setattr(orchestrator, "_available_backends", lambda: (CLAUDE,))
    monkeypatch.setattr(orchestrator, "_launch_backend", lambda: CLAUDE)
    monkeypatch.setattr(orchestrator, "_launch_model", lambda task, backend: "")
    # Model policy binding writes under `.orchestrator/`; covered by test_model_task_freeze.
    monkeypatch.setattr(orchestrator, "_legacy_bind_model_policy",
                        lambda task_id, renew=False, task=None: None)
    monkeypatch.setattr(orchestrator, "get_backend", lambda name: _FakeDriver())
    monkeypatch.setattr(
        orchestrator, "launch_implementer",
        lambda task, sid, workspace, worktree, **kwargs: box.launched.append(task["id"]),
    )
    monkeypatch.setattr(
        orchestrator, "subprocess",
        SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr="")),
    )
    monkeypatch.setattr(
        orchestrator, "time",
        SimpleNamespace(time=lambda: 1000.0, monotonic=lambda: 1000.0, sleep=_sleep)
    )
    monkeypatch.setattr(orchestrator.metabranch, "reconcile_meta", lambda repo: "stubbed")

    def _reconcile(state, launch_times):
        # Startup hands in the whole state document: seed it from `box.in_flight`. The
        # per-poll call hands in just `{"in_flight": ...}`: pass it through, so work
        # launched by an earlier poll stays in flight.
        if set(state) == {"in_flight"}:
            return list(state["in_flight"])
        return list(box.in_flight)

    monkeypatch.setattr(orchestrator, "reconcile_in_flight", _reconcile)
    return box


def _launched_ids(box) -> set[str]:
    return {e["task_id"] for e in box.state.get("in_flight", [])}


def test_a_ready_script_task_launches_while_every_agent_slot_is_full(loop_env, tmp_path):
    """§1.3 negative invariant: the agent task ahead of it in the queue is skipped, not
    a reason to stop scanning."""
    loop_env.in_flight = [_entry(tmp_path)]
    loop_env.runnable = [_agent_task("A1"), _script_task("S1")]

    assert orchestrator.main() == 0

    assert loop_env.launched == []
    assert "S1" in _launched_ids(loop_env)
    assert "A1" not in _launched_ids(loop_env)


def test_a_running_script_task_holds_no_agent_slot(loop_env, tmp_path):
    loop_env.in_flight = [_entry(tmp_path, sid="impl-S0-1", task_id="S0", role="script")]
    loop_env.runnable = [_agent_task("A1")]

    assert orchestrator.main() == 0

    assert loop_env.launched == ["A1"]


def test_an_agent_launch_reserves_a_host_slot_for_its_backend(loop_env):
    loop_env.runnable = [_agent_task("A1")]

    assert orchestrator.main() == 0

    rows = HostReservationLedger().reservations()
    (entry,) = [e for e in loop_env.state["in_flight"] if e["task_id"] == "A1"]
    assert [(r.run_id, r.pool_id, r.project_id) for r in rows] == [
        (entry["session_id"], f"backend:{CLAUDE}", str(orchestrator.REPO))
    ]


def test_a_script_launch_reserves_nothing(loop_env):
    loop_env.runnable = [_script_task("S1")]

    assert orchestrator.main() == 0

    assert "S1" in _launched_ids(loop_env)
    assert HostReservationLedger().reservations() == []


def test_a_reservation_is_released_when_its_entry_leaves_in_flight(loop_env, tmp_path):
    loop_env.in_flight = [_entry(tmp_path)]
    assert orchestrator.main() == 0
    assert [r.run_id for r in HostReservationLedger().reservations()] == ["impl-T1-1"]

    loop_env.in_flight = []
    loop_env.polls = 0
    loop_env.state.pop("halted", None)
    assert orchestrator.main() == 0

    assert HostReservationLedger().reservations() == []


def test_a_saturated_host_pool_holds_agent_work_but_not_a_script_task(
    loop_env, monkeypatch, tmp_path
):
    """Another project holds the only slot: the agent task waits, the script task runs."""
    import os

    HostReservationLedger().reserve(
        f"backend:{CLAUDE}", project_id="/other/project", run_id="impl-X-1",
        writer_pid=os.getpid(), limit=1,
    )
    monkeypatch.setattr(orchestrator, "_host_pool_limit", lambda: 1)
    loop_env.cap = (4, 10.0)
    loop_env.runnable = [_agent_task("A1"), _script_task("S1")]

    assert orchestrator.main() == 0

    assert loop_env.launched == []
    assert _launched_ids(loop_env) == {"S1"}
    assert any(event == "launch_skipped_host_pool" for event, _ in loop_env.journal)
    assert [r.project_id for r in HostReservationLedger().reservations()] == ["/other/project"]


def test_launch_order_puts_operator_priority_first_and_keeps_roadmap_order_on_ties(monkeypatch):
    tasks = [_agent_task("B"), _agent_task("A"), _agent_task("C", priority=2)]
    monkeypatch.setattr(orchestrator, "_load_roadmap_tasks", lambda: tasks)
    monkeypatch.setattr(orchestrator, "_ELIGIBILITY", orchestrator.scheduling.EligibilityClock())

    assert [t["id"] for t in orchestrator._order_runnable(tasks, now=0.0)] == ["C", "B", "A"]


def test_launch_order_prefers_the_task_that_unblocks_more_work(monkeypatch):
    tasks = [_agent_task("A"), _agent_task("B")]
    roadmap = tasks + [{"id": "D1", "deps": ["B"]}, {"id": "D2", "deps": ["D1"]}]
    monkeypatch.setattr(orchestrator, "_load_roadmap_tasks", lambda: roadmap)
    monkeypatch.setattr(orchestrator, "_ELIGIBILITY", orchestrator.scheduling.EligibilityClock())

    assert [t["id"] for t in orchestrator._order_runnable(tasks, now=0.0)] == ["B", "A"]


def test_aging_lifts_a_long_waiting_task_over_fresh_higher_priority_work(monkeypatch):
    clock = orchestrator.scheduling.EligibilityClock()
    monkeypatch.setattr(orchestrator, "_ELIGIBILITY", clock)
    monkeypatch.setattr(orchestrator, "_load_roadmap_tasks", lambda: [])
    old = _agent_task("OLD")
    orchestrator._order_runnable([old], now=0.0)            # eligible since t=0

    fresh = _agent_task("NEW", priority=1)
    order = orchestrator._order_runnable([fresh, old], now=3 * 600.0)

    assert [t["id"] for t in order] == ["OLD", "NEW"]


def test_a_replan_hold_skips_dependents_and_dispatches_independent_work(monkeypatch, tmp_path):
    """P13 GDT-8 (R46, C27): while a replan of G3 waits on Dan, dispatch skips G3's direct
    and enabling work (and what depends on it), autonomous or prepped; G3's maintenance,
    `serves: []` and other goals' work keep running. Dan's answer lifts the hold."""
    import json

    from maestro import goal_store
    from maestro.docs import roadmap

    from .test_goals_contract import V2

    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "GOALS.md").write_text(V2, encoding="utf-8")
    blocks = [
        {"id": "G3-WORK", "mode": "autonomous", "serves": ["G3"], "contribution": "direct"},
        {"id": "G3-HARNESS", "mode": "autonomous", "serves": ["G3"], "contribution": "enabling"},
        {"id": "G3-POLISH", "mode": "autonomous", "serves": ["G3"],
         "contribution": "maintenance"},
        {"id": "G1-WORK", "mode": "autonomous", "serves": ["G1"], "contribution": "direct"},
        {"id": "TIDY", "mode": "autonomous", "serves": [], "contribution": "maintenance"},
        {"id": "G3-PREP", "mode": "needs-dan", "dispatch": "manual", "serves": ["G3"],
         "contribution": "direct"},
        {"id": "G1-PREP", "mode": "needs-dan", "dispatch": "manual", "serves": ["G1"],
         "contribution": "direct"},
    ]
    (repo / "docs" / "ROADMAP.md").write_text("# Roadmap\n\n" + "".join(
        f"```yaml\n{json.dumps(b)}\n```\n\n" for b in blocks), encoding="utf-8")
    journal: list[tuple[str, str]] = []
    monkeypatch.setattr(roadmap, "REPO", repo)
    monkeypatch.setattr(roadmap, "ROADMAP_FILE", repo / "docs" / "ROADMAP.md")
    monkeypatch.setattr(roadmap, "COMPLETED_TASKS", repo / ".orchestrator" / "completed.json")
    monkeypatch.setattr(roadmap, "read_state", lambda: {})
    monkeypatch.setattr(roadmap.prep_actions, "has_action", lambda tid: False)
    monkeypatch.setattr(roadmap, "append_journal", lambda e, d="": journal.append((e, d)))

    def ids(found):
        return [t["id"] for t in found]

    everything = ["G3-WORK", "G3-HARNESS", "G3-POLISH", "G1-WORK", "TIDY"]
    assert ids(roadmap.parse_runnable_tasks()) == everything     # no store: no hold
    goal_store.update(repo, "replans", lambda rows: [*rows, {"id": "RP-1", "goal": "G3",
                                                             "status": "asked"}])
    assert ids(roadmap.parse_runnable_tasks()) == ["G3-POLISH", "G1-WORK", "TIDY"]
    assert ids(roadmap.parse_prep_tasks()) == ["G1-PREP"]
    assert [e for e, _ in journal] == ["goal_replan_hold"]       # once per change
    assert "G3-WORK" in journal[0][1] and "RP-1" in journal[0][1]
    goal_store.update(repo, "replans", lambda rows: [
        dict(r, status="decided", decision={"resolution": "keep_going"}) for r in rows])
    assert ids(roadmap.parse_runnable_tasks()) == everything
    assert ids(roadmap.parse_prep_tasks()) == ["G3-PREP", "G1-PREP"]


def test_an_exhausted_queue_exits_for_a_relaunch_after_adopting(loop_env, monkeypatch):
    """Queue exhausted is the idle moment a finished project sits in for days; adopting a
    new maestro there must exit the loop, not sleep on the old code."""
    monkeypatch.setattr(orchestrator, "_self_update_maybe", lambda: True)

    assert orchestrator.main() == 0

    assert ("queue_exhausted", "") in loop_env.journal
    assert not loop_env.state.get("halted"), "the loop slept instead of exiting"


# ── P13 GDT-11 / W7: the polish stage at queue exhaustion ───────────────────────────────


def _polish_env(monkeypatch, tmp_path):
    from maestro.docs import roadmap as _roadmap

    box = SimpleNamespace(journal=[], sent=[], added=[], finish=[])
    monkeypatch.setattr(orchestrator, "REPO", tmp_path)
    monkeypatch.setattr(orchestrator, "read_state", lambda: {})
    monkeypatch.setattr(orchestrator, "_load_roadmap_tasks", lambda: [])
    monkeypatch.setattr(orchestrator, "get_completed_task_ids", lambda: {"T1", "T2"})
    monkeypatch.setattr(orchestrator, "notify_telegram", lambda text, *a, **k: box.sent.append(text))
    monkeypatch.setattr(orchestrator, "append_journal",
                        lambda event, detail="", session_id="": box.journal.append((event, detail)))
    monkeypatch.setattr(_roadmap, "add_tasks", lambda tasks, label, event: box.added.append(
        ([t["id"] for t in tasks], label, event)) or [t["id"] for t in tasks])

    def finish(channel):
        box.finish.append(channel)
        return {"kind": "complete", "text": "", "verdict": {"verdict": "complete"}}

    monkeypatch.setattr(orchestrator, "_graph_goal_finish", finish)
    return box


def test_at_exhaustion_the_polish_stage_runs_before_the_completion_gate(monkeypatch, tmp_path):
    from maestro import backlog
    from maestro.docs import followups

    box = _polish_env(monkeypatch, tmp_path)
    for tid, text in (("T1", "search does not fold accents"), ("T2", "magic rank weights")):
        backlog.add(tmp_path, {"id": tid}, [followups.FollowUp(text, 1, "proof_review")],
                    at="2026-10-24T00:00:00+00:00")
    channel = object()

    polishing, step = orchestrator._graph_at_exhaustion(channel)

    # A stage was added: the completion gate is not asked; the loop idles on.
    assert (polishing, step, box.finish) == (True, None, [])
    assert box.added == [(["polish-triage-1"], "polish stage 1", "polish")]
    assert box.sent == ["🧹 Main work is done. Polish stage 1: 2 review follow-ups go to "
                        "triage (polish-triage-1)."]
    assert ("polish_stage_opened", "1: BL-0001, BL-0002 → polish-triage-1") in box.journal

    # The stage's task is in the ROADMAP now: nothing opens, and the finish runs.
    monkeypatch.setattr(orchestrator, "_load_roadmap_tasks",
                        lambda: [{"id": "polish-triage-1", "status": "open"}])
    polishing, step = orchestrator._graph_at_exhaustion(channel)
    assert polishing is False and step["kind"] == "complete" and box.finish == [channel]
    assert len(box.added) == 1

    # One item goes straight to its polish task.
    other = tmp_path / "other"
    monkeypatch.setattr(orchestrator, "REPO", other)
    # Its own ROADMAP: a polish-triage-1 still listed would number this stage 2 (review M1).
    monkeypatch.setattr(orchestrator, "_load_roadmap_tasks", lambda: [])
    backlog.add(other, {"id": "T1"}, [followups.FollowUp("x", 1, "proof_review")],
                at="2026-10-24T00:00:00+00:00")
    assert orchestrator._graph_at_exhaustion(channel)[0] is True
    assert box.sent[-1] == "🧹 Main work is done. Polish stage 1: 1 follow-up → polish-1-a."


def test_a_failing_polish_stage_never_stops_the_loop(monkeypatch, tmp_path):
    from maestro import backlog

    box = _polish_env(monkeypatch, tmp_path)

    def broken(*a, **k):
        raise ValueError("the store is gone")

    monkeypatch.setattr(backlog, "stage", broken)

    polishing, step = orchestrator._graph_at_exhaustion(None)

    assert polishing is False and step["kind"] == "complete" and box.finish == [None]
    assert ("polish_stage_failed", "ValueError: the store is gone") in box.journal
    assert box.added == [] and box.sent == []


# ── P13 GDT-12 / W12: the improvement analysis at idle moments ───────────────────────────


@pytest.fixture
def improvement_env(monkeypatch, tmp_path):
    """A git project whose ledger holds a reviewer pattern (3 tasks, 2 `missed_earlier`
    rows each); the analyst is a recording double, the clock `box.now`."""
    import subprocess as sp

    from maestro.workflows import telemetry_capture
    from tests import improvement_fixture as fx

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return sp.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                      text=True).stdout.strip()

    for args in (("init", "-q"), ("config", "user.email", "t@example.invalid"),
                 ("config", "user.name", "t"), ("commit", "-q", "--allow-empty", "-m", "seed")):
        git(*args)
    db, store = fx.ledger(repo)
    for k in range(3):
        run = fx.add_task(store, f"t{k}", finished_at=fx.iso(k + 1), attempts=fx.work(reviews=3))
        for i in (2, 3):
            telemetry_capture.record_extra_step(
                store, run_id=run, cause="missed_earlier", role="reviewer",
                attempt_id=f"{run}-a{i}", node_id="reviewer", at=fx.iso(k + 1))
    box = SimpleNamespace(journal=[], calls=[], now=fx.iso(100), store=store, repo=repo,
                          arts=fx.artifacts(repo, store), git=git, fx=fx)
    monkeypatch.setattr(orchestrator, "REPO", repo)
    monkeypatch.setattr(orchestrator._state_module, "CONTROL_DB", db)
    monkeypatch.setattr(orchestrator, "append_journal",
                        lambda event, detail="", session_id="": box.journal.append((event, detail)))
    monkeypatch.setattr(orchestrator, "notify_telegram", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "_improvement_now", lambda: box.now)

    def analyst(system, user):
        box.calls.append(user)
        return '{"proposals": []}'

    monkeypatch.setattr(orchestrator, "_improvement_analyst", analyst)
    yield box
    store.close()


def _improvement_events(box) -> list[str]:
    return [e for e, _ in box.journal if e.startswith("improvement_")]


def test_project_end_runs_the_improvement_analysis_once_per_head(improvement_env):
    import inspect

    from maestro import improvement

    box = improvement_env
    head = box.git("rev-parse", "HEAD")
    out = orchestrator._graph_improvement(box.store, box.arts, None, "project_end")
    assert (out["kind"], out["key"], out["status"]) == (
        "project_end", f"project_end:{head}", "done")
    assert out["at"] == box.fx.iso(100) and len(box.calls) == 1
    # A repeated exhausted poll on the same HEAD does nothing.
    assert orchestrator._graph_improvement(box.store, box.arts, None, "project_end") is None
    assert len(box.calls) == 1 and len(improvement.read(box.repo)["runs"]) == 1
    # A new HEAD (more work merged, exhausted again) is a new project end.
    box.git("commit", "-q", "--allow-empty", "-m", "more")
    box.now = box.fx.iso(101)
    again = orchestrator._graph_improvement(box.store, box.arts, None, "project_end")
    assert again["key"] == f"project_end:{box.git('rev-parse', 'HEAD')}"
    assert again["status"] == "empty" and len(box.calls) == 1    # no new task: no call
    # The graph loop runs it right after it journals the exhausted queue.
    source = inspect.getsource(orchestrator._graph_main)
    exhausted = source.index('append_journal("queue_exhausted", "")')
    assert source.index('_graph_improvement(store, artifacts, dan_channel, "project_end")',
                        exhausted) - exhausted < 200


def test_a_failing_improvement_run_never_stops_the_loop(improvement_env, monkeypatch):
    from maestro import improvement

    box = improvement_env

    def boom(*a, **k):
        raise RuntimeError("the store is gone")

    monkeypatch.setattr(improvement, "due", boom)
    assert orchestrator._graph_improvement(box.store, box.arts, None, "project_end") is None
    assert orchestrator._graph_improvement_weekly(box.store, box.arts, None) is None
    assert ("improvement_failed", "project_end: RuntimeError: the store is gone") in box.journal
    monkeypatch.setattr(improvement, "poll", boom)
    monkeypatch.setattr(improvement, "landed", boom)
    orchestrator._graph_improvement_poll(object())
    assert ("improvement_failed", "poll: RuntimeError: the store is gone") in box.journal
    monkeypatch.undo()
    # An analyst that fails inside the run is the run's own failure: stored, never raised.
    monkeypatch.setattr(orchestrator, "REPO", box.repo)
    monkeypatch.setattr(orchestrator, "append_journal",
                        lambda event, detail="", session_id="": box.journal.append((event, detail)))
    monkeypatch.setattr(orchestrator, "_improvement_now", lambda: box.now)
    monkeypatch.setattr(orchestrator, "_improvement_analyst", boom)
    from maestro import state as state_module

    monkeypatch.setattr(state_module, "CONTROL_DB", box.repo / ".orchestrator" / "control.sqlite3")
    out = orchestrator._graph_improvement(box.store, box.arts, None, "project_end")
    assert out["status"] == "failed" and out["call"]["tries"] == improvement.MAX_TRIES
    assert "improvement_analyst_error" in _improvement_events(box)
    assert "improvement_analysis_failed" in _improvement_events(box)


def test_the_weekly_run_happens_only_at_an_idle_tick_when_due(improvement_env, monkeypatch):
    import inspect

    from maestro import improvement

    box = improvement_env
    clock = SimpleNamespace(t=1_000_000.0)
    monkeypatch.setattr(orchestrator, "time", SimpleNamespace(
        monotonic=lambda: clock.t, time=lambda: clock.t, sleep=lambda s: None))
    monkeypatch.setattr(orchestrator, "_last_improvement_check", 0.0)
    out = orchestrator._graph_improvement_weekly(box.store, box.arts, None)
    assert (out["kind"], out["key"], out["status"]) == ("weekly", "weekly:2026-10-05", "done")
    # Throttled like self-update: the next idle tick checks nothing, even when due.
    box.now = box.fx.iso(100 + 8 * 24)
    clock.t += 1
    assert orchestrator._graph_improvement_weekly(box.store, box.arts, None) is None
    clock.t += orchestrator.IMPROVEMENT_CHECK_S
    assert orchestrator._graph_improvement_weekly(box.store, box.arts, None)["key"] == (
        "weekly:2026-10-13")
    # Within the interval it is not due.
    box.now = box.fx.iso(100 + 9 * 24)
    clock.t += orchestrator.IMPROVEMENT_CHECK_S
    assert orchestrator._graph_improvement_weekly(box.store, box.arts, None) is None
    assert [r["kind"] for r in improvement.read(box.repo)["runs"]] == ["weekly", "weekly"]
    # Only the loop's idle branches call it, each beside the idle self-update check.
    source = inspect.getsource(orchestrator._graph_main)
    lines = source.splitlines()
    calls = [n for n, line in enumerate(lines) if "_graph_improvement_weekly(" in line]
    assert len(calls) == 3
    for n in calls:
        assert any("_self_update_maybe()" in line for line in lines[n + 1:n + 3]), lines[n]


def test_the_weekly_run_waits_while_work_is_runnable_but_not_launched(
        improvement_env, monkeypatch):
    """Review M1: right after the last runner settles, or while admission or a quota pause
    holds every launch, the loop has no runner but runnable work. That is not idle: an
    analysis with a pattern may take MAX_TRIES x ANALYST_TIMEOUT, so it waits, and the
    throttle is not spent on the skipped tick."""
    import inspect

    from maestro import improvement

    box = improvement_env
    clock = SimpleNamespace(t=1_000_000.0)
    monkeypatch.setattr(orchestrator, "time", SimpleNamespace(
        monotonic=lambda: clock.t, time=lambda: clock.t, sleep=lambda s: None))
    monkeypatch.setattr(orchestrator, "_last_improvement_check", 0.0)
    assert orchestrator._graph_improvement_weekly(
        box.store, box.arts, None, runnable=["t9"]) is None
    assert box.calls == [] and improvement.read(box.repo)["runs"] == []
    # The next tick with nothing runnable runs it at once: the throttle was not spent.
    clock.t += 1
    out = orchestrator._graph_improvement_weekly(box.store, box.arts, None, runnable=[])
    assert (out["kind"], out["status"]) == ("weekly", "done") and len(box.calls) == 1
    # Every call in the loop passes what is runnable.
    source = inspect.getsource(orchestrator._graph_main)
    calls = [line.strip() for line in source.splitlines()
             if "_graph_improvement_weekly(" in line]
    assert len(calls) == 3
    assert all(c == "_graph_improvement_weekly(store, artifacts, dan_channel, runnable)"
               for c in calls), calls


@pytest.mark.parametrize('barrier', ['hold', 'rejected', 'paused', 'parked', 'prerequisite',
                                    'unaccepted', 'replan', 'resource'])
def test_polish_never_promotes_items_from_an_ineligible_source(monkeypatch, tmp_path, barrier):
    from maestro import backlog
    from maestro.docs import followups, roadmap

    box = _polish_env(monkeypatch, tmp_path)
    source = {'id': 'T1', 'status': 'complete', 'mode': 'autonomous', 'deps': []}
    document = {}
    if barrier == 'hold':
        source['hold'] = True
    elif barrier in ('rejected', 'paused'):
        source['status'] = barrier
    elif barrier == 'parked':
        document['parked_tasks'] = ['T1']
    elif barrier == 'prerequisite':
        source['deps'] = ['UNFINISHED']
    elif barrier == 'unaccepted':
        source['status'] = 'open'
        monkeypatch.setattr(orchestrator, 'get_completed_task_ids', lambda: {'T2'})
    elif barrier == 'resource':
        source['resource'] = 'eval-cpu'
        document['graph_runs'] = [{'task_id': 'OTHER'}]
    monkeypatch.setattr(orchestrator, 'read_state', lambda: document)
    monkeypatch.setattr(orchestrator, '_load_roadmap_tasks', lambda: [source,
        {'id': 'OTHER', 'resource': 'eval-cpu'}])
    monkeypatch.setattr(roadmap, '_drift_blocked_ids', lambda: frozenset())
    monkeypatch.setattr(roadmap, '_goal_blocked_ids', lambda tasks: frozenset())
    monkeypatch.setattr(roadmap, '_replan_blocked_ids',
                        lambda tasks: frozenset({'T1'} if barrier == 'replan' else ()))
    backlog.add(tmp_path, source, [followups.FollowUp('Preserve the held source', 1, 'review')],
                at='2026-10-27T00:00:00+00:00')
    backlog.add(tmp_path, {'id': 'T2'}, [followups.FollowUp('Eligible follow-up', 1, 'review')],
                at='2026-10-27T00:00:00+00:00')

    assert orchestrator._graph_polish_stage() is True
    rows = backlog.items(tmp_path)
    assert [(r['source']['task'], r['state']) for r in rows] == [
        ('T1', 'open'), ('T2', 'assigned')]
    assert box.added == [(['polish-1-a'], 'polish stage 1', 'polish')]
