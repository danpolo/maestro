"""The graph runner carries what the legacy loop did around a run (DESIGN §16, "Parity
with the legacy loop").

Every lane drives the real `orchestrator.main` through `run_lane`, as the end-to-end
lanes do. Telegram never leaves the process: the harness replaces every notifier with a
recording double, and a lane that needs the words sent swaps in `_Spy` on top of it (the
harness's own undo restores the real function afterwards).
"""
from __future__ import annotations

import dataclasses
import re
import sys
from pathlib import Path

import maestro.state
from maestro.backends.base import Completion

from .test_dispatch_wiring import _journal_details, _task
from .test_end_to_end import (
    _V1,
    _CommittingAgent,
    _IdleAgent,
    _git_sandbox,
    _happy_scenario,
    _run_status,
)
from .test_failure_ladder import _patch_fixture_cli_driver


class _Spy:
    """Records every call of the named maestro functions, on every module holding them.

    Installed from a lane's `before_run`, i.e. over the harness's doubles; `run_lane`'s own
    monkeypatch restores the real functions when the test ends.
    """

    def __init__(self, *names, returns=None):
        self.names = names
        self.returns = returns or {}
        self.calls: dict[str, list] = {name: [] for name in names}

    def install(self, run=None) -> None:
        for name in self.names:
            def spy(*args, _name=name, **kwargs):
                self.calls[_name].append(args)
                return self.returns.get(_name)
            for module_name, module in list(sys.modules.items()):
                if module is None or not (module_name == "maestro"
                                          or module_name.startswith("maestro.")):
                    continue
                if hasattr(module, name):
                    setattr(module, name, spy)

    def texts(self, name: str = "notify_telegram") -> list[str]:
        return [str(args[0]) for args in self.calls[name]]


def _with_spy(spy: _Spy):
    def before_run(run):
        _git_sandbox(run)
        spy.install(run)
    return before_run


def _seams(run, name: str) -> int:
    return sum(1 for event in run.trace.events if event.identity == name)


# ── G1: what Dan hears ────────────────────────────────────────────────────────────────


def test_an_accepted_run_announces_its_start_and_reports_its_graduation(run_lane, monkeypatch):
    _patch_fixture_cli_driver(monkeypatch, _CommittingAgent())
    spy = _Spy("notify_telegram", "phase_report")
    roadmap_at_report: list[str] = []

    def phase_report(task_id, task_def, smoke, follow_ups=""):
        from maestro.orchestrator import ROADMAP_FILE

        spy.calls["phase_report"].append((task_id, task_def, smoke))
        roadmap_at_report.append(ROADMAP_FILE.read_text(encoding="utf-8"))

    def before_run(run):
        _with_spy(spy)(run)
        for module_name, module in list(sys.modules.items()):
            if module_name.startswith("maestro") and hasattr(module, "phase_report"):
                setattr(module, "phase_report", phase_report)

    run = run_lane(_happy_scenario("parity_accept_report", before_run=before_run))

    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    starts = [t for t in spy.texts() if t.startswith("▶")]
    assert starts == ["▶ Starting TASK-001: TASK-001 title"]
    # One completion report, after graduation stripped the task from the ROADMAP, with
    # the task's definition as it stood before and the acceptance decision as its line.
    [(task_id, task_def, smoke)] = spy.calls["phase_report"]
    assert task_id == "TASK-001"
    assert task_def["short_desc"] == "add the feature file"
    assert "required check(s) passed" in smoke["reason"] and smoke["metrics"] == {}
    assert "TASK-001" not in roadmap_at_report[0]
    assert _journal_details("task_complete")[0].startswith("TASK-001 ")
    # The ROADMAP-change map push is checked every poll.
    assert _seams(run, "maybe_push_roadmap_map_change") >= run.polls
    assert not [t for t in spy.texts() if t.startswith("✗")]


def test_a_failed_run_tells_dan_why_once_and_not_again_after_a_restart(run_lane, monkeypatch):
    _patch_fixture_cli_driver(monkeypatch, _IdleAgent())
    # G7 item 4: the notice is a Dan request (Retry / Shelve) sent on the channel's bot.
    spy = _Spy("notify_telegram", "send_via_bot", "telegram_configured",
               returns={"telegram_configured": True})

    run = run_lane(_happy_scenario("parity_failed_notice", before_run=_with_spy(spy)))

    assert _run_status() == "failed", _journal_details("graph_run_settled")
    failed = [t for t in spy.texts() + spy.texts("send_via_bot") if t.startswith("✗")]
    assert len(failed) == 1, failed
    assert failed[0].startswith("✗ TASK-001 failed at gate — parked (run ")
    # The reason is the gate's own evidence, not a generic line.
    assert "Reason: verification_failed: required check 'V1'" in failed[0], failed[0]

    # A restarted loop does not resume a settled run, so it says nothing more about it.
    run.orchestrator.HALT_FILE.unlink()
    maestro.state._reset_control_store()
    spy.calls["notify_telegram"].clear()
    spy.calls["send_via_bot"].clear()
    polls = {"n": 0}

    def halt_soon(r):
        polls["n"] += 1
        if polls["n"] >= 2:
            r.halt()

    run.scenario = dataclasses.replace(run.scenario, on_poll=halt_soon)
    run.run()
    assert not [t for t in spy.texts() + spy.texts("send_via_bot")
                if t.startswith("✗") or t.startswith("▶")]


# ── G3: publishing the merge ──────────────────────────────────────────────────────────


def _meta_spy(calls: list):
    def before_run(run):
        _git_sandbox(run)
        run.orchestrator.metabranch.reconcile_meta = lambda repo: calls.append(repo) or "ok"
    return before_run


def test_an_accepted_run_pushes_main_and_reconciles_the_meta_branch(run_lane, monkeypatch):
    _patch_fixture_cli_driver(monkeypatch, _CommittingAgent())
    meta: list = []

    run = run_lane(_happy_scenario("parity_push", before_run=_meta_spy(meta)))

    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    assert run.commands.count("git push origin main") == 1, run.commands
    # Once at startup, once after the push.
    assert len(meta) == 2


def test_a_project_without_a_remote_pushes_nothing_and_still_graduates(run_lane, monkeypatch):
    _patch_fixture_cli_driver(monkeypatch, _CommittingAgent())
    import maestro.orchestrator

    monkeypatch.setattr(maestro.orchestrator, "_graph_no_remote_journaled", False)
    meta: list = []

    run = run_lane(_happy_scenario(
        "parity_no_remote", before_run=_meta_spy(meta),
        failing_commands={"git remote get-url origin": 2},
    ))

    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    assert not [c for c in run.commands if c.startswith("git push")]
    assert len(_journal_details("push_skipped_no_remote")) == 1
    assert "TASK-001" in _journal_details("roadmap_task_removed")[0]
    assert len(meta) == 1  # startup only


# ── G4: live runs are visible in state.json, /status, /progress and the status docs ───


def test_a_live_run_is_listed_in_state_and_phase_then_cleared_when_it_settles(
    run_lane, monkeypatch
):
    from .test_end_to_end import _drive_every_worker

    _patch_fixture_cli_driver(monkeypatch, _CommittingAgent())
    seen: list[dict] = []

    def watch(run):
        state = maestro.state.read_state()
        seen.append({"graph_runs": state.get("graph_runs"), "phase": state.get("phase"),
                     "in_flight": state.get("in_flight")})
        _drive_every_worker(run)

    run = run_lane(_happy_scenario("parity_live_state", on_poll=watch))

    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    live = [s for s in seen if s["graph_runs"]]
    assert live, seen
    [row] = live[0]["graph_runs"]
    assert (row["task_id"], row["title"], row["status"]) == ("TASK-001", "TASK-001 title", "running")
    assert row["run_id"].startswith("run_") and row["nodes"]
    assert live[0]["phase"] == {"id": "TASK-001", "title": "TASK-001 title",
                                "status": "in_progress"}
    # Graph runs never enter legacy's `in_flight`, whose rows are tmux sessions.
    assert all(not s["in_flight"] for s in seen)
    final = maestro.state.read_state()
    assert final["graph_runs"] == [] and final["phase"]["status"] == "idle"
    # Map and status at startup, after the open and after the settle (status also at exit).
    assert _seams(run, "run_dep_map") >= 3
    assert _seams(run, "run_status") >= 4


def test_status_and_progress_and_the_docs_count_a_live_graph_run(lane_sandbox):
    import json

    from maestro import status
    from maestro.docs import depmap, upcoming
    from maestro.hitl import commands

    state = {
        "in_flight": [], "phase": {"id": "TASK-001", "title": "t", "status": "in_progress"},
        "graph_runs": [{"task_id": "TASK-002", "run_id": "run_x", "title": "two",
                        "started_at": "2026-09-25T10:00:00Z", "status": "running",
                        "nodes": ["implementer"]}],
    }
    (lane_sandbox / "docs" / "ROADMAP.md").write_text("# Roadmap\n", encoding="utf-8")
    maestro.state.STATE_JSON.write_text(json.dumps(state), encoding="utf-8")

    assert [r["task_id"] for r in status.graph_runs(state)] == ["TASK-002"]
    assert "TASK-002" in depmap._active_ids()
    report = commands._build_progress_report()
    assert "TASK-002 (graph) running" in report and "at implementer" in report
    assert upcoming.task_status({"id": "TASK-002"}, state, set())["label"] == "🟢 Running now"


def test_the_status_command_lists_a_live_graph_run(lane_sandbox, monkeypatch):
    import json
    import subprocess
    import types

    from maestro.hitl import commands

    state = {"in_flight": [], "phase": {"id": "TASK-002", "status": "in_progress"},
             "graph_runs": [{"task_id": "TASK-002", "run_id": "run_x", "nodes": []}]}
    maestro.state.STATE_JSON.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "42")
    monkeypatch.setattr(commands, "_getUpdates_offset", 0)
    updates = {"ok": True, "result": [
        {"update_id": 1, "message": {"chat": {"id": 42}, "text": "/status"}}]}
    monkeypatch.setattr(commands, "subprocess", types.SimpleNamespace(
        run=lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(updates), ""),
    ))
    sent: list = []
    monkeypatch.setattr(commands, "notify_telegram", sent.append)
    monkeypatch.setattr(commands, '_fetch_updates', lambda *a, **k: updates['result'])

    commands.poll_control_commands([])

    [text] = sent
    assert "In-flight: 1" in text and "Tasks: TASK-002" in text


# ── G5: human-in-the-loop ─────────────────────────────────────────────────────────────


def test_hitl_mode_holds_the_merge_for_dans_review_and_his_approval_lands_it(
    run_lane, monkeypatch
):
    from maestro.hitl import verify as _verify
    from maestro.workflows.compiler import MERGE_REVIEW_CHECK_ID

    from .test_end_to_end import TARGET, _Dan, _git

    _patch_fixture_cli_driver(monkeypatch, _CommittingAgent())
    dan = _Dan(["approve-button"])
    monkeypatch.setattr(_verify, "send_via_bot", dan.send)
    monkeypatch.setattr(_verify, "telegram_configured", lambda: True)

    run = run_lane(_happy_scenario("parity_hitl_mode", state={"hitl_mode": True},
                                   on_poll=dan.on_poll))

    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    seed = _git(run.repo, "rev-list", "--max-parents=0", TARGET)
    assert dan.main_when_asked == [seed], "main moved before Dan reviewed the change"
    [(text, _markup)] = dan.sent
    assert MERGE_REVIEW_CHECK_ID in text and "hitl_mode is on" in text
    assert "log --stat -p main..HEAD" in text


def test_without_hitl_mode_an_ordinary_task_compiles_no_dan_step():
    from maestro.taskgraph import normalize_spec
    from maestro.workflows.compiler import compile_workflow

    spec = normalize_spec(_task("TASK-001", verifications=_V1))
    plain = compile_workflow(spec, workflow_id="wf")
    reviewed = compile_workflow(spec, workflow_id="wf", merge_review="hitl_mode is on")
    assert "dan_confirm" not in {n.node_id for n in plain.nodes}
    [confirm] = [n for n in reviewed.nodes if n.node_id == "dan_confirm"]
    assert [c["id"] for c in confirm.params["dan_checks"]] == ["MERGE-REVIEW"]
    assert any(e.source_node_id == "dan_confirm" and e.target_node_id == "merge"
               for e in reviewed.edges)


def test_a_task_with_questions_opens_only_once_dan_answered_and_its_brief_has_them(
    run_lane, monkeypatch
):
    from maestro import implementer

    agent = _CommittingAgent()
    _patch_fixture_cli_driver(monkeypatch, agent)
    asked: list = []
    monkeypatch.setattr(implementer, "_ask_question", lambda task_id, q: asked.append(q["id"]))
    polls = {"n": 0}

    def answer_on_third_poll(run):
        from .test_end_to_end import _drive_every_worker

        polls["n"] += 1
        if polls["n"] == 3:
            assert maestro.state.control_store().conn.execute(
                "SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0
            implementer.QUESTIONS_DIR.mkdir(parents=True, exist_ok=True)
            (implementer.QUESTIONS_DIR / "q-TASK-001-Q1.answer").write_text(
                "use the blue one\n", encoding="utf-8")
        _drive_every_worker(run)

    task = _task("TASK-001", verifications=_V1,
                 questions=[{"id": "Q1", "prompt": "Which colour?"}])
    run = run_lane(_happy_scenario("parity_questions", tasks=[task],
                                   on_poll=answer_on_third_poll))

    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    assert set(asked) == {"Q1"}
    assert len(_journal_details("launch_skipped_questions")) == 1
    [brief] = [s.prompt for s in agent.specs if s.prompt.startswith("You are the implementer ")]
    assert "ANSWERS FROM DAN" in brief and "use the blue one" in brief


def test_a_self_modifying_task_never_opens_a_run(run_lane):
    task = _task("TASK-001", verifications=_V1, self_modifying=True)

    run = run_lane(_happy_scenario("parity_self_modifying", tasks=[task],
                                   on_poll=lambda r: r.halt()))

    assert run.returncode == 0
    assert maestro.state.control_store().conn.execute(
        "SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0


def test_the_graph_loop_polls_reminders_and_proposal_answers(run_lane):
    run = run_lane(_happy_scenario("parity_hitl_polls", on_poll=lambda r: r.halt()))

    assert _seams(run, "_send_hitl_reminders") >= 1
    assert _seams(run, "poll_proposal_answer") >= 1


def test_a_paused_loop_reads_dans_proposal_answer_while_it_waits(run_lane):
    def halt(run):
        state = maestro.state.read_state()
        state["halted"] = True
        maestro.state.write_state(state)

    run = run_lane(_happy_scenario("parity_paused_proposals", state={"paused_by_user": True},
                                   on_poll=halt))

    assert run.returncode == 0
    assert _seams(run, "poll_proposal_answer") == 1


def test_a_long_wait_on_dans_verification_is_reminded_once_a_day(lane_sandbox, monkeypatch):
    import json
    from datetime import datetime, timedelta, timezone

    from maestro.hitl import telegram

    old = (datetime.now(timezone.utc) - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = {"awaiting_dan_verification": {
        "TASK-001": {"run_id": "r1", "request_id": "verify-TASK-001-abc", "requested_at": old,
                     "sent": True},
        "TASK-002": {"run_id": "r2", "request_id": "verify-TASK-002-def", "requested_at": fresh,
                     "sent": True},
        "TASK-003": {"run_id": "r3", "request_id": "verify-TASK-003-ghi", "requested_at": old,
                     "sent": False},
    }}
    maestro.state.STATE_JSON.write_text(json.dumps(state), encoding="utf-8")
    sent: list = []
    monkeypatch.setattr(telegram, "notify_telegram", sent.append)

    telegram._send_hitl_reminders()
    telegram._send_hitl_reminders()

    assert len(sent) == 1 and "TASK-001" in sent[0] and "/verify TASK-001 approve" in sent[0]
    stamps = maestro.state.read_state()[telegram.VERIFY_REMINDED_KEY]
    assert list(stamps) == ["verify-TASK-001-abc"]


def test_a_long_wait_on_dans_decision_is_reminded_once_a_day(lane_sandbox, monkeypatch):
    """D26: a task paused on an agent's spec question gets the same daily reminder."""
    import json
    from datetime import datetime, timedelta, timezone

    from maestro.hitl import telegram

    old = (datetime.now(timezone.utc) - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = {"awaiting_dan_questions": {
        "TASK-001": {"run_id": "r1", "request_ids": ["dq-TASK-001-a"], "requested_at": old,
                     "sent": True},
        "TASK-002": {"run_id": "r2", "request_ids": ["dq-TASK-002-b"], "requested_at": fresh,
                     "sent": True},
        "TASK-003": {"run_id": "r3", "request_ids": ["dq-TASK-003-c"], "requested_at": old,
                     "sent": False},
    }}
    maestro.state.STATE_JSON.write_text(json.dumps(state), encoding="utf-8")
    sent: list = []
    monkeypatch.setattr(telegram, "notify_telegram", sent.append)

    telegram._send_hitl_reminders()
    telegram._send_hitl_reminders()

    assert len(sent) == 1 and "TASK-001" in sent[0] and "1 decision question;" in sent[0]
    stamps = maestro.state.read_state()[telegram.QUESTIONS_REMINDED_KEY]
    assert list(stamps) == ["TASK-001"]


# ── G6: ordering, cooldown, canary, self-update, self-fix/redo landing ────────────────


def test_runs_open_in_priority_order(run_lane):
    tasks = [_task("TASK-001", verifications=_V1, scope=["a.txt"]),
             _task("TASK-002", verifications=_V1, priority=5, scope=["b.txt"])]

    run_lane(_happy_scenario("parity_order", tasks=tasks, on_poll=lambda r: r.halt()))

    opened = [d.split()[0] for d in _journal_details("graph_run_opened")]
    assert opened == ["TASK-002", "TASK-001"]


def test_a_resumable_cooldown_left_by_legacy_holds_the_task_until_it_expires(run_lane):
    from datetime import datetime, timedelta, timezone

    later = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
    earlier = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    tasks = [_task("TASK-001", verifications=_V1), _task("TASK-002", verifications=_V1)]
    state = {"resume_state": {"TASK-001": {"resume_after": later},
                              "TASK-002": {"resume_after": earlier}}}

    run_lane(_happy_scenario("parity_cooldown", tasks=tasks, state=state,
                             on_poll=lambda r: r.halt()))

    opened = [d.split()[0] for d in _journal_details("graph_run_opened")]
    assert opened == ["TASK-002"]


def test_the_idle_graph_loop_checks_for_a_self_update_and_lands_ready_fixes(run_lane):
    # Idle, not exhausted: a task is ready but parked, so nothing opens.
    run = run_lane(_happy_scenario("parity_idle", state={"parked_tasks": ["TASK-001"]},
                                   on_poll=lambda r: r.halt()))

    assert _seams(run, "_self_update_maybe") >= 1
    assert _seams(run, "apply_ready_self_fixes") >= 1
    assert _seams(run, "apply_ready_redo") >= 1


def _bot_scenario(lane, spy, *, canary_ok):
    def before_run(run):
        _git_sandbox(run)
        spy.install(run)
        run.orchestrator._graph_changed_files = lambda before, after: ["bot/main_bot.py"]
    spy.returns["run_canary_deploy"] = canary_ok
    return _happy_scenario(
        lane, before_run=before_run,
        project_yaml={"engineering": {"runner": "graph"}, "bot_files": ["main_bot.py"]},
    )


def test_an_accepted_merge_of_bot_files_is_canary_deployed_before_the_report(
    run_lane, monkeypatch
):
    _patch_fixture_cli_driver(monkeypatch, _CommittingAgent())
    spy = _Spy("run_canary_deploy", "phase_report")

    run_lane(_bot_scenario("parity_canary_ok", spy, canary_ok=True))

    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    assert spy.calls["run_canary_deploy"] == [("TASK-001",)]
    assert len(spy.calls["phase_report"]) == 1


def test_a_failed_canary_skips_the_report(run_lane, monkeypatch):
    _patch_fixture_cli_driver(monkeypatch, _CommittingAgent())
    spy = _Spy("run_canary_deploy", "phase_report")

    run_lane(_bot_scenario("parity_canary_failed", spy, canary_ok=False))

    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    assert spy.calls["run_canary_deploy"] == [("TASK-001",)]
    assert spy.calls["phase_report"] == []
    assert _journal_details("canary_reverted") == ["TASK-001 canary deploy failed"]


def test_the_merge_range_is_read_from_the_accepted_runs_merge_result(run_lane, monkeypatch):
    from maestro import orchestrator
    from maestro.control.artifacts import ArtifactStore

    from .test_end_to_end import TARGET, _git

    _patch_fixture_cli_driver(monkeypatch, _CommittingAgent())
    run = run_lane(_happy_scenario("parity_merge_range"))

    store = maestro.state.control_store()
    [row] = store.conn.execute("SELECT run_id FROM task_runs").fetchall()
    artifacts = ArtifactStore(run.repo / ".orchestrator" / "artifacts", store)
    before, after = orchestrator._graph_merge_range(store, artifacts, row["run_id"])
    assert before == _git(run.repo, "rev-list", "--max-parents=0", TARGET)
    assert after and _git(run.repo, "merge-base", "--is-ancestor", after, TARGET) == ""
    changed = _diff_names(run.repo, before, after)
    assert "feature.txt" in changed


def _diff_names(repo, before, after) -> list[str]:
    import subprocess

    return subprocess.run(["git", "diff", "--name-only", before, after], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.split()


def test_a_resource_label_is_held_by_one_run_at_a_time(run_lane, monkeypatch):
    from .test_end_to_end import _PerTaskAgent, _drive_until_runs_settle

    _patch_fixture_cli_driver(monkeypatch, _PerTaskAgent())
    tasks = [
        _task("TASK-001", resource="eval-cpu",
              verifications=[{"id": "V1", "kind": "auto", "cmd": "test -f TASK-001.txt"}]),
        _task("TASK-002", resource=["eval-cpu"],
              verifications=[{"id": "V1", "kind": "auto", "cmd": "test -f TASK-002.txt"}]),
    ]

    run_lane(_happy_scenario("parity_resource_mutex", tasks=tasks,
                             on_poll=_drive_until_runs_settle(2)))

    runs = maestro.state.control_store().conn.execute(
        "SELECT task_id, status, started_at, finished_at FROM task_runs ORDER BY rowid"
    ).fetchall()
    assert [(r["task_id"], r["status"]) for r in runs] == [
        ("TASK-001", "succeeded"), ("TASK-002", "succeeded")]
    assert runs[1]["started_at"] >= runs[0]["finished_at"], "the label was held twice"
    assert len(_journal_details("launch_skipped_resource")) == 1
