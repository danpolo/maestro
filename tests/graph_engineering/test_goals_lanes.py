"""P13 W11a lanes: the goals contract through the real graph loop.

Every role's brief carries the bounded `docs/GOALS.md` excerpt; the reviewer's
`goal_progress` decides by rule (GDT-6): a verified `regressed` reworks the implementer;
`no_progress_unexpected` merges, reruns nobody and is an unattributed observation, never a
planner fault (R58); `serves: []` still asks for regressions; unasked goal work is a W9
scope finding that never widens the scope. The gate reruns the GREEN acceptance criteria:
a `serves: []` task that turns one RED reworks (R28).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import maestro.state
from maestro.backends.base import Completion
from maestro.workflows import failures as wf_failures
from maestro.workflows import handlers
from maestro.workflows.runner import SCOPE_WIDENED_EVENT

from ..test_goals import SKILL_STYLE
from .test_end_to_end import (
    DIAGNOSIS,
    FEATURE,
    TARGET,
    _attempts,
    _briefs,
    _DiagnosingAgent,
    _drive_until_the_newest_run_settles,
    _git,
    _git_sandbox,
    _happy_scenario,
    _journal_details,
    _newest_run_status,
    _ReviewingAgent,
    _run_status,
    _task,
    _V1,
)
from .test_failure_ladder import _patch_fixture_cli_driver
from tests.hang_bound import HANG_SECONDS

_HEADING = "GOALS (docs/GOALS.md excerpt; why this project exists"


def _goals_sandbox(run) -> None:
    (run.repo / "docs").mkdir(parents=True, exist_ok=True)
    (run.repo / "docs" / "GOALS.md").write_text(SKILL_STYLE, encoding="utf-8")
    _git_sandbox(run)


_PASSED = [{"kind": "test", "id": "tests/test_totals.py::test_match", "result": "passed"}]


class _GoalReviewingAgent(_ReviewingAgent):
    """`_ReviewingAgent` whose reviews carry the next `(verdict, goal_progress[, extra])`:
    `extra` is merged into review.json (e.g. `unasked_goal_work`)."""

    def complete(self, spec):
        if not spec.prompt.startswith("You are the reviewer "):
            return super().complete(spec)
        self.specs.append(spec)
        workspace = Path(re.search(r"^WORKSPACE: (\S+)$", spec.prompt, re.MULTILINE).group(1))
        verdict, progress, *extra = self.verdicts.pop(0)
        path = handlers.write_review(workspace, verdict, [])
        import json

        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["goal_progress"] = progress
        for more in extra:
            doc.update(more)
        path.write_text(json.dumps(doc), encoding="utf-8")
        handlers.write_sentinel(workspace, handlers.DONE_SENTINEL, "done")
        return Completion(text="done", returncode=0, timed_out=False)


def test_every_role_brief_carries_the_goals_excerpt(run_lane, monkeypatch):
    """B16: planner, implementer, reviewer and diagnoser briefs carry the excerpt (the
    manifest step is shared, so a later role such as W7's triage gets it too)."""
    monkeypatch.setattr(wf_failures, "QUALITY_ATTEMPTS", 1)
    agent = _DiagnosingAgent(
        ["changes_requested", "approved"],
        {"verdict": "poor_agent", "reason": "missed it",
         "brief_addenda": {"implementer": DIAGNOSIS}},
    )
    _patch_fixture_cli_driver(monkeypatch, agent)

    run = run_lane(_happy_scenario(
        "e2e_w11a_goals_every_role",
        tasks=[_task("TASK-001", short_desc="add the feature file", verifications=_V1,
                     serves=["G2"])],
        before_run=_goals_sandbox,
        on_poll=_drive_until_the_newest_run_settles,
    ))

    assert run.returncode == 0
    assert _newest_run_status() == "succeeded", _journal_details("graph_run_settled")
    for role in ("implementer", "reviewer", "diagnoser"):
        briefs = _briefs(agent, role)
        assert briefs, role
        for brief in briefs:
            assert _HEADING in brief, role
            assert "This task serves: G2" in brief and "the monthly totals match" in brief
            assert ("GOAL PROGRESS" in brief) == (role == "reviewer"), role
            # GDT-5: the runner passes the node's role, so each GOALS section has its own
            # rule (a diagnoser's brief also quotes the review material, so look only there)
            goals = brief.split(_HEADING, 1)[1].split("\n\n", 1)[0]
            for marker, owner in (("SCOPE (D23)", "implementer"), ("REVIEW:", "reviewer"),
                                  ("DIAGNOSIS:", "diagnoser")):
                assert (marker in goals) == (role == owner), (role, marker)
    reviewer = _briefs(agent, "reviewer")[0]
    assert "never widens this task's scope" in reviewer


def test_planner_brief_carries_the_goals_excerpt(run_lane, monkeypatch):
    """The free-quality objective opens with a planner; its brief comes through the same
    shared manifest step and carries the excerpt, without the reviewer's check."""
    from .test_dispatch_wiring import _free_quality

    _free_quality(monkeypatch)
    agent = _GoalReviewingAgent([])
    _patch_fixture_cli_driver(monkeypatch, agent)
    seen: list[str] = []

    def on_poll(run) -> None:
        seen[:] = _briefs(agent, "planner")
        if seen:
            run.halt()
        else:
            _drive_until_the_newest_run_settles(run)

    run_lane(_happy_scenario(
        "e2e_w11a_goals_planner",
        tasks=[_task("TASK-001", short_desc="add the feature file", verifications=_V1,
                     serves=["G2"])],
        before_run=_goals_sandbox,
        on_poll=on_poll,
    ))

    [planner] = seen
    assert _HEADING in planner
    assert "This task serves: G2" in planner and "the monthly totals match" in planner
    assert "PLANNING: plan only inside the approved contract" in planner  # GDT-5
    assert "GOAL PROGRESS" not in planner


def test_regressed_reworks_the_implementer(run_lane, monkeypatch):
    agent = _GoalReviewingAgent([
        # G2 only enabled: had it advanced, G3's regression would be a goal conflict for
        # Dan (GDT-7, the conflict lane below), not GDT-6's finding.
        ("approved", [{"goal": "G2", "impact": "enabled", "evidence": []},
                      # A failed test of no criterion: a metric would need a criterion
                      # whose threshold says which way is better (stage-1 review), and
                      # this legacy-format contract has none.
                      {"goal": "G3", "impact": "regressed", "evidence": [
                          {"kind": "test", "id": "tests/test_speed.py::test_import_time",
                           "result": "failed"}]}]),
        ("approved", [{"goal": "G2", "impact": "advanced", "evidence": _PASSED}]),
    ])
    _patch_fixture_cli_driver(monkeypatch, agent)

    run = run_lane(_happy_scenario(
        "e2e_w11a_regressed_reworks",
        tasks=[_task("TASK-001", short_desc="add the feature file", verifications=_V1,
                     serves=["G2"])],
        before_run=_goals_sandbox,
    ))

    assert run.returncode == 0
    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    writers = _briefs(agent, "implementer")
    assert len(writers) == 2
    assert "Goal G3 regressed: test tests/test_speed.py::test_import_time failed" in writers[1]
    reviews = [a for a in _attempts() if a["node_id"] == "proof_review"]
    assert [a["status"] for a in reviews] == ["failed", "succeeded"]
    assert _planner_rows() == [] and _impact_rows() == []
    assert _git(run.repo, "show", f"{TARGET}:{FEATURE}") == "round 2"


def test_no_progress_unexpected_merges_and_is_an_unattributed_observation(
        run_lane, monkeypatch):
    """R37, R58, C17: a correct direct task that did not move its goal merges with no
    rework; the miss is recorded with its provenance and no automatic planner fault."""
    agent = _GoalReviewingAgent([
        ("approved", [{"goal": "G2", "impact": "no_progress_unexpected",
                       "evidence": [{"kind": "test", "id": "tests/test_summary.py::t",
                                     "result": "failed"}]}]),
    ])
    _patch_fixture_cli_driver(monkeypatch, agent)
    # GDT-12: the idle-tick weekly analysis would append an attribution amendment to this
    # observation (R58; covered by test_improvement_evidence); this lane reads the capture.
    import maestro.orchestrator as orchestrator
    monkeypatch.setattr(orchestrator, "_graph_improvement", lambda *a, **k: None)

    run = run_lane(_happy_scenario(
        "e2e_gdt6_no_progress_unexpected_merges",
        tasks=[_task("TASK-001", short_desc="add the feature file", verifications=_V1,
                     serves=["G2"], contribution="direct")],
        before_run=_goals_sandbox,
    ))

    assert run.returncode == 0
    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    assert len(_briefs(agent, "implementer")) == 1
    reviews = [a for a in _attempts() if a["node_id"] == "proof_review"]
    assert [a["status"] for a in reviews] == ["succeeded"]
    assert _planner_rows() == []
    [row] = _impact_rows()
    assert row["ledger_id"] == f"goal:G2:review:{reviews[0]['attempt_id']}"
    assert (row["goal"], row["impact"], row["attribution"]) == (
        "G2", "no_progress_unexpected", "unattributed")
    assert row["provenance"]["attempt_id"] == reviews[0]["attempt_id"]
    assert row["provenance"]["node_id"] == "proof_review"
    assert row["provenance"]["evidence"] == [
        {"kind": "test", "id": "tests/test_summary.py::t", "result": "failed"}]


def _events(kind: str) -> list[dict]:
    import json

    rows = maestro.state.control_store().conn.execute(
        "SELECT payload FROM events WHERE type = ? ORDER BY sequence", (kind,)
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def _planner_rows() -> list[dict]:
    return [p for p in _events("ExtraStepObserved")
            if p.get("extra_step_cause") == "task_did_not_serve_goal"]


def _impact_rows() -> list[dict]:
    return _events("GoalImpactObserved")


def test_serves_empty_still_asks_the_reviewer_for_regressions(run_lane, monkeypatch):
    agent = _GoalReviewingAgent([
        ("approved", [{"goal": "G3", "impact": "advanced", "evidence": "ignored"}]),
    ])
    _patch_fixture_cli_driver(monkeypatch, agent)

    run = run_lane(_happy_scenario(
        "e2e_w11a_serves_empty",
        tasks=[_task("TASK-001", short_desc="tidy", verifications=_V1, serves=[])],
        before_run=_goals_sandbox,
    ))

    assert run.returncode == 0
    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    [reviewer] = _briefs(agent, "reviewer")
    assert "serves no goal (serves: [])" in reviewer and "GOAL PROGRESS" in reviewer
    assert "only for a goal the change makes worse" in reviewer
    assert len(_briefs(agent, "implementer")) == 1


def test_unasked_goal_work_is_a_scope_finding_that_never_widens_the_scope(
        run_lane, monkeypatch):
    """R36: the reviewer reports goal work the task did not ask for; it reworks through
    W9's out-of-scope finding, and the path it names is not added to the rework scope."""
    agent = _GoalReviewingAgent([
        ("approved", [], {"unasked_goal_work": [
            {"path": "app.py", "goal": "G2", "what": "adds a summary page"}]}),
        ("approved", []),
    ])
    _patch_fixture_cli_driver(monkeypatch, agent)

    def sandbox(run):
        (run.repo / "app.py").write_text("print('app')\n", encoding="utf-8")
        _goals_sandbox(run)

    run = run_lane(_happy_scenario(
        "e2e_gdt6_unasked_goal_work",
        tasks=[_task("TASK-001", short_desc="add the feature file", verifications=_V1,
                     serves=["G2"], contribution="direct")],
        before_run=sandbox,
    ))

    assert run.returncode == 0
    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    writers = _briefs(agent, "implementer")
    assert len(writers) == 2
    assert "out_of_scope: app.py; " in writers[1]
    assert "goal work the task does not ask for (G2: adds a summary page)" in writers[1]
    assert _events(SCOPE_WIDENED_EVENT) == []


_GUARD_TEST = (
    "from pathlib import Path\n\n\n"
    "def test_feature_is_not_round_one():\n"
    "    feature = Path('feature.txt')\n"
    "    assert not feature.exists() or feature.read_text() != 'round 1\\n'\n"
)


def _acceptance_sandbox(run) -> None:
    """A format-2 contract whose AC-01 is bound to a real test, GREEN at the baseline:
    round 1 of the fixture's feature file turns it RED, round 2 keeps it GREEN."""
    from maestro import acceptance
    from maestro.docs import goals

    from ..test_goals_contract import V2

    (run.repo / "docs").mkdir(parents=True, exist_ok=True)
    (run.repo / "docs" / "GOALS.md").write_text(V2.replace(
        "`pytest tests/acceptance/test_import.py::test_valid`",
        "`pytest tests/acceptance/test_guard.py::test_feature_is_not_round_one`"),
        encoding="utf-8")
    (run.repo / "tests" / "acceptance").mkdir(parents=True, exist_ok=True)
    (run.repo / "tests" / "acceptance" / "test_guard.py").write_text(_GUARD_TEST)
    (run.repo / "pytest.ini").write_text("[pytest]\n")
    _git_sandbox(run)
    doc = goals.load(run.repo)
    acceptance.store(run.repo, doc, acceptance.run_baseline(run.repo, doc, timeout=HANG_SECONDS))
    assert {r.id: r.state for r in acceptance.load(run.repo)}["AC-01"] == acceptance.GREEN


def test_a_maintenance_task_that_turns_a_green_criterion_red_reworks(run_lane, monkeypatch):
    """R22, R28, C18: the gate reruns the GREEN criteria whatever the task serves; a
    `serves: []` task whose round 1 breaks AC-01 is sent back, and round 2 lands."""
    agent = _GoalReviewingAgent([("approved", [])])
    _patch_fixture_cli_driver(monkeypatch, agent)

    run = run_lane(_happy_scenario(
        "e2e_gdt6_acceptance_regression",
        tasks=[_task("TASK-001", short_desc="tidy", verifications=_V1, serves=[],
                     contribution="maintenance")],
        before_run=_acceptance_sandbox,
    ))

    assert run.returncode == 0
    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    writers = _briefs(agent, "implementer")
    assert len(writers) == 2
    assert "acceptance_regression: AC-01 (G1) was green_already_satisfied" in writers[1]
    gates = [a["status"] for a in _attempts() if a["node_id"] == "gate"]
    assert gates == ["failed", "succeeded"]
    assert _git(run.repo, "show", f"{TARGET}:{FEATURE}") == "round 2"


# --- GDT-7: a goal conflict at the gate goes to Dan once, and his answer is kept ----------

_G1_TEST = ("from pathlib import Path\n\n\n"
            "def test_refused():\n    assert Path('feature.txt').exists()\n")
#: AC-04's measurement: 8 seconds at the base, 10.4 once the feature lands (30% worse).
_MEASURE = ("from pathlib import Path\n\n"
            "print('10.4' if Path('feature.txt').exists() else '8')\n")


def _conflict_sandbox(run, measure: str = _MEASURE) -> None:
    """V2 with AC-02 (G1) bound to a test the feature turns GREEN and AC-04 (G3, metric,
    below 10 seconds) bound to a measurement the feature turns RED."""
    from maestro import acceptance
    from maestro.docs import goals

    from ..test_goals_contract import V2

    contract = V2.replace(
        "- **Layer:** e2e\n- **Evidence:** test\n\n### AC-03",
        "- **Layer:** e2e\n- **Evidence:** test\n"
        "- **Test:** `pytest tests/acceptance/test_g1.py::test_refused`\n\n### AC-03").replace(
        "- **Layer:** e2e\n- **Evidence:** metric\n",
        "- **Layer:** e2e\n- **Evidence:** metric\n"
        "- **Test:** `python tests/acceptance/measure.py`\n")
    (run.repo / "docs").mkdir(parents=True, exist_ok=True)
    (run.repo / "docs" / "GOALS.md").write_text(contract, encoding="utf-8")
    (run.repo / "tests" / "acceptance").mkdir(parents=True, exist_ok=True)
    (run.repo / "tests" / "acceptance" / "test_g1.py").write_text(_G1_TEST)
    (run.repo / "tests" / "acceptance" / "measure.py").write_text(measure)
    (run.repo / "pytest.ini").write_text("[pytest]\n")
    _git_sandbox(run)
    doc = goals.load(run.repo)
    acceptance.store(run.repo, doc, acceptance.run_baseline(run.repo, doc, timeout=HANG_SECONDS))
    states = {r.id: r.state for r in acceptance.load(run.repo)}
    assert (states["AC-02"], states["AC-04"]) == (acceptance.RED, acceptance.GREEN)


def test_a_goal_conflict_at_the_gate_asks_dan_once_and_his_accept_is_kept(
        run_lane, monkeypatch):
    """R48-R50, C22: the task turns AC-02 (G1) GREEN while metric AC-04 (G3) goes GREEN to
    RED. The gate asks Dan once (priority ranks G1 above G3, so accepting the measured
    +30% is recommended); he taps Accept, the decision lands in
    `.orchestrator/goal_decisions.json`, the rework's gate sees the same facts, is covered
    and passes without asking again, and the task merges."""
    import json

    from maestro.docs import decisions as _decisions
    from maestro.hitl import verify as _verify

    from .test_end_to_end import _DanAnswers

    agent = _GoalReviewingAgent([("approved", [])])
    _patch_fixture_cli_driver(monkeypatch, agent)
    dan = _DanAnswers([("tap", 1)])
    monkeypatch.setattr(_verify, "send_via_bot", dan.send)
    monkeypatch.setattr(_verify, "telegram_configured", lambda: True)
    monkeypatch.setattr(_decisions, "_adr_agent", lambda prompt, cwd: "")

    run = run_lane(_happy_scenario(
        "e2e_gdt7_goal_conflict",
        tasks=[_task("TASK-001", short_desc="refuse broken exports", verifications=_V1,
                     serves=["G1"], tests=["AC-02"])],
        before_run=_conflict_sandbox, on_poll=dan.on_poll,
    ))

    assert run.returncode == 0
    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    [(text, markup)] = dan.sent
    assert "Decision needed" in text and "G3" in text and "AC-04" in text
    assert "Accept G3 worse by up to +30%" in text
    assert [b["text"] for row in markup["inline_keyboard"] for b in row] == ["1", "2 ⭐"]
    assert len(_journal_details("dan_question_asked")) == 1
    assert len(_journal_details("dan_question_answered")) == 1
    [decision] = json.loads((run.repo / ".orchestrator" / "goal_decisions.json").read_text(
        encoding="utf-8"))["decisions"]
    assert (decision["resolution"], decision["bound"], decision["regressed"]) == (
        "accept", 30.0, "G3")
    assert decision["advanced"] == ["G1"] and decision["criteria"] == ["AC-04"]
    gates = [a["status"] for a in _attempts() if a["node_id"] == "gate"]
    assert gates == ["failed", "succeeded"]
    first, rework = _briefs(agent, "implementer")
    assert "Accept G3 worse by up to +30%" in rework
    assert _git(run.repo, "show", f"{TARGET}:{FEATURE}") == "round 2"


# --- GDT-7 review: one ask per conflict, and Dan's accept covers later tasks --------------

#: AC-04 at 8 s on the base, 10.4 s with the feature (+30%), 13 s once `slow.txt` lands too
#: (+62.5%, beyond the +30% Dan accepts).
_SLOW_MEASURE = ("from pathlib import Path\n\n"
                 "print('13' if Path('slow.txt').exists() else "
                 "'10.4' if Path('feature.txt').exists() else '8')\n")


class _SlowingAgent(_GoalReviewingAgent):
    """`_GoalReviewingAgent`; with `slow`, TASK-002's implementer also adds `slow.txt`."""

    def __init__(self, verdicts, slow: bool):
        super().__init__(verdicts)
        self.slow = slow

    def complete(self, spec):
        if self.slow and spec.prompt.startswith("You are the implementer for task TASK-002"):
            (spec.cwd / "slow.txt").write_text("slow\n", encoding="utf-8")
            _git(spec.cwd, "add", "slow.txt")
        return super().complete(spec)


def _dan_lane(monkeypatch, agent, replies):
    from maestro.docs import decisions as _decisions
    from maestro.hitl import verify as _verify

    from . import conftest as lane_conftest
    from .test_end_to_end import _DanAnswers

    # Questions, reworks and a second task's run: more than the default 12 polls.
    monkeypatch.setattr(lane_conftest, "MAX_POLLS", 40)
    _patch_fixture_cli_driver(monkeypatch, agent)
    dan = _DanAnswers(replies)
    monkeypatch.setattr(_verify, "send_via_bot", dan.send)
    monkeypatch.setattr(_verify, "telegram_configured", lambda: True)
    monkeypatch.setattr(_decisions, "_adr_agent", lambda prompt, cwd: "")
    return dan


def _runs() -> list[tuple[str, str]]:
    rows = maestro.state.control_store().conn.execute(
        "SELECT task_id, status FROM task_runs ORDER BY started_at, rowid").fetchall()
    return [(r["task_id"], r["status"]) for r in rows]


@pytest.mark.parametrize("slow", [False, True], ids=["within_bound", "beyond_bound"])
def test_dan_s_accept_covers_a_later_task_within_its_bound(run_lane, monkeypatch, slow):
    """GDT-7 review H1: TASK-001 trades AC-04 (+30%) for G1 and Dan accepts. The stored
    baseline still has AC-04 GREEN, so TASK-002 (advancing nothing) reruns it RED: within
    the +30% his accept covers it and TASK-002 merges without asking; beyond it, he is
    asked again (once), accepts, and TASK-002 merges."""
    from .test_end_to_end import _drive_until_runs_settle

    agent = _SlowingAgent([("approved", []), ("approved", [])], slow)
    dan = _dan_lane(monkeypatch, agent, [("tap", 1), ("tap", 1)])
    settle = _drive_until_runs_settle(2)

    def on_poll(run) -> None:
        dan.answer()
        parked = maestro.state.control_store().conn.execute(
            "SELECT 1 FROM node_states WHERE node_id = 'park' AND phase = 'claimed'"
        ).fetchone()
        if parked:
            run.halt()
        else:
            settle(run)

    first = _task("TASK-001", short_desc="refuse broken exports", verifications=_V1,
                  serves=["G1"], tests=["AC-02"])
    second = _task("TASK-002", short_desc="tidy the totals", deps=["TASK-001"],
                   verifications=_V1, serves=["G2"])
    run = run_lane(_happy_scenario(
        f"e2e_gdt7r_later_task_{'beyond' if slow else 'within'}", tasks=[first, second],
        before_run=lambda r: _conflict_sandbox(r, _SLOW_MEASURE), on_poll=on_poll))

    assert run.returncode == 0
    assert _runs() == [("TASK-001", "succeeded"), ("TASK-002", "succeeded")], \
        _journal_details("graph_run_settled")
    asked = [text for text, _ in dan.sent]
    if slow:
        assert len(asked) == 2
        assert "beyond the +30% Dan approved" in asked[1] and "TASK-002" in asked[1]
    else:
        assert len(asked) == 1
    assert _git(run.repo, "show", f"{TARGET}:{FEATURE}").startswith("round ")


def test_a_conflict_dan_answered_at_the_gate_is_not_asked_again_at_review(
        run_lane, monkeypatch):
    """GDT-7 review H2: the gate asks Dan about AC-04 and he accepts +30%; the reviewer then
    reports the same regression as a failing test (no measurement). The gate already judged
    AC-04 on this tree, so Dan is asked exactly once and the task merges."""
    progress = [
        {"goal": "G1", "impact": "advanced", "evidence": [
            {"kind": "test", "criterion": "AC-02", "result": "passed",
             "id": "tests/acceptance/test_g1.py::test_refused"}]},
        {"goal": "G3", "impact": "regressed", "evidence": [
            {"kind": "test", "criterion": "AC-04", "result": "failed",
             "id": "tests/acceptance/measure.py"}]},
    ]
    agent = _GoalReviewingAgent([("approved", progress), ("approved", progress)])
    dan = _dan_lane(monkeypatch, agent, [("tap", 1), ("tap", 1)])

    run = run_lane(_happy_scenario(
        "e2e_gdt7r_gate_then_review",
        tasks=[_task("TASK-001", short_desc="refuse broken exports", verifications=_V1,
                     serves=["G1"], tests=["AC-02"])],
        before_run=_conflict_sandbox, on_poll=dan.on_poll,
    ))

    assert run.returncode == 0
    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    assert len(dan.sent) == 1
    assert len(_journal_details("dan_question_asked")) == 1
    reviews = [a["status"] for a in _attempts() if a["node_id"] == "proof_review"]
    assert reviews == ["succeeded"]


# --- GDT-8: modified-B replanning at graduation ----------------------------------------------

#: A served direct task that did not move G2: AC-03's test still fails (and merges, C17).
_G2_MISS = [{"goal": "G2", "impact": "no_progress_unexpected", "evidence": [
    {"kind": "test", "id": "tests/test_totals.py::test_match", "result": "failed",
     "criterion": "AC-03"}]}]


def _replan_sandbox(run) -> None:
    """V2 with no criterion bound (no checkpoint runs a test) and `Ask Dan:` reserving
    plan changes, so even the recommended diagnosis task is Dan's to approve."""
    from ..test_goals_contract import V2

    contract = V2.replace("- **Test:** `pytest tests/acceptance/test_import.py::test_valid`\n",
                          "").replace("any goal conflict, paid services.",
                                      "any goal conflict, paid services, plan changes.")
    (run.repo / "docs").mkdir(parents=True, exist_ok=True)
    (run.repo / "docs" / "GOALS.md").write_text(contract, encoding="utf-8")
    _git_sandbox(run)


def _store(run) -> dict:
    from maestro import goal_store

    return goal_store.read(run.repo)


def test_two_accepted_unexpected_misses_ask_once_and_an_independent_task_still_runs(
        run_lane, monkeypatch):
    """R44-R46, C25, C27: TASK-001 and TASK-002, direct G2 work, merge though neither moved
    G2 (C17). The second miss asks Dan once, with the proposal; TASK-003 (G2's next direct
    task) is held while TASK-004 (G1) runs and merges, and the queue is not called
    exhausted while the question waits."""
    from maestro import orchestrator

    from .test_end_to_end import _drive_every_worker_of_any_run

    agent = _GoalReviewingAgent([("approved", _G2_MISS), ("approved", _G2_MISS),
                                 ("approved", [])])
    dan = _dan_lane(monkeypatch, agent, [])
    monkeypatch.setattr(orchestrator, "_judge_complete", lambda *a, **k: "")
    after: list[int] = []

    def on_poll(run) -> None:
        settled = maestro.state.control_store().conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE status != 'running'").fetchone()[0]
        if settled >= 3:
            if after:           # one more poll with nothing runnable but the held task
                run.halt()
                return
            after.append(run.polls)
        _drive_every_worker_of_any_run(run)

    g2 = dict(verifications=_V1, serves=["G2"], contribution="direct")
    run = run_lane(_happy_scenario("e2e_gdt8_replan_asks_once", tasks=[
        _task("TASK-001", short_desc="sum the totals", **g2),
        _task("TASK-002", short_desc="sum per category", deps=["TASK-001"], **g2),
        _task("TASK-003", short_desc="round per line", deps=["TASK-002"], **g2),
        _task("TASK-004", short_desc="refuse broken exports", deps=["TASK-002"],
              verifications=_V1, serves=["G1"], contribution="direct"),
    ], before_run=_replan_sandbox, on_poll=on_poll))

    assert run.returncode == 0
    assert _runs() == [("TASK-001", "succeeded"), ("TASK-002", "succeeded"),
                       ("TASK-004", "succeeded")], _journal_details("graph_run_settled")
    [(text, markup)] = dan.sent
    assert "Decision needed" in text and "RP-" in text and "G2" in text
    assert "TASK-001" in text and "TASK-002" in text and "Held: TASK-003" in text
    assert "Add a diagnosis task" in text and "Keep going" in text
    assert len(markup["inline_keyboard"][0]) == 3
    [replan] = _store(run)["replans"]
    assert (replan["status"], replan["goal"]) == ("asked", "G2")
    assert replan["trigger"]["facts"] == ["TASK-001:G2", "TASK-002:G2"]
    assert len(_journal_details("goal_replan_asked")) == 1
    assert "queue_exhausted" not in run.journal_events()


def test_an_approved_contract_change_waits_on_dan_and_is_not_an_exhausted_queue(
        run_lane, monkeypatch):
    """GDT-8 review M3: Dan approves revising G2's contract. TASK-003 stays held until his
    revision lands; with nothing else to run the loop says what waits on him instead of
    "Queue exhausted"."""
    from maestro import orchestrator

    from .test_end_to_end import _drive_every_worker_of_any_run

    agent = _GoalReviewingAgent([("approved", _G2_MISS), ("approved", _G2_MISS),
                                 ("approved", [])])
    dan = _dan_lane(monkeypatch, agent, [("tap", 1)])     # "Revise G2's contract"
    monkeypatch.setattr(orchestrator, "_judge_complete", lambda *a, **k: "")
    after: list[int] = []

    def on_poll(run) -> None:
        dan.answer()
        settled = maestro.state.control_store().conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE status != 'running'").fetchone()[0]
        replans = _store(run)["replans"]
        if settled >= 3 and replans and replans[0].get("decision"):
            if len(after) >= 2:
                run.halt()
                return
            after.append(run.polls)
        _drive_every_worker_of_any_run(run)

    g2 = dict(verifications=_V1, serves=["G2"], contribution="direct")
    run = run_lane(_happy_scenario("e2e_gdt8r_contract_waits", tasks=[
        _task("TASK-001", short_desc="sum the totals", **g2),
        _task("TASK-002", short_desc="sum per category", deps=["TASK-001"], **g2),
        _task("TASK-003", short_desc="round per line", deps=["TASK-002"], **g2),
        _task("TASK-004", short_desc="refuse broken exports", deps=["TASK-002"],
              verifications=_V1, serves=["G1"], contribution="direct"),
    ], before_run=_replan_sandbox, on_poll=on_poll))

    assert run.returncode == 0
    assert _runs() == [("TASK-001", "succeeded"), ("TASK-002", "succeeded"),
                       ("TASK-004", "succeeded")], _journal_details("graph_run_settled")
    [replan] = _store(run)["replans"]
    assert replan["status"] == "decided"
    assert replan["decision"]["alternative"] == "Revise G2's contract"
    assert "queue_exhausted" not in run.journal_events()
    [waiting] = _journal_details("goal_replan_waiting")
    assert replan["id"] in waiting and "TASK-003" in waiting and "GOALS.md" in waiting


def test_an_enabling_lane_pair_raises_no_replan(run_lane, monkeypatch):
    """R70, C25: two enabling tasks whose reviewer reports no movement merge; the rule
    reads it as expected, so no replan is raised and nothing is held. The second one is
    G2's convergence: a checkpoint is recorded (no bound criterion, so nothing runs)."""
    from .test_end_to_end import _drive_until_runs_settle

    agent = _GoalReviewingAgent([("approved", _G2_MISS), ("approved", _G2_MISS)])
    dan = _dan_lane(monkeypatch, agent, [])
    enabling = dict(verifications=_V1, serves=["G2"], contribution="enabling")
    run = run_lane(_happy_scenario("e2e_gdt8_enabling_pair", tasks=[
        _task("TASK-001", short_desc="fixture statements", **enabling),
        _task("TASK-002", short_desc="statement harness", deps=["TASK-001"], **enabling),
    ], before_run=_replan_sandbox, on_poll=_drive_until_runs_settle(2)))

    assert run.returncode == 0
    assert _runs() == [("TASK-001", "succeeded"), ("TASK-002", "succeeded")], \
        _journal_details("graph_run_settled")
    assert dan.sent == []
    store = _store(run)
    assert store["replans"] == []
    assert [(e["id"], e["impact"]) for e in store["episodes"]] == [
        ("TASK-001:G2", "unchanged_expected"), ("TASK-002:G2", "unchanged_expected")], \
        _journal_details("goal_replan_failed")
    assert [list(c["milestones"]) for c in store["checkpoints"]] == [
        ["enabling_unblocked"], ["convergence"]]


# --- GDT-9: durable goal-progress state -------------------------------------------------------

_OK_TEST = "def test_ok():\n    assert True\n"
#: AC-02's test: GREEN once the feature exists, RED at round 2 (a later task breaks it).
_PROGRESS_TEST = ("from pathlib import Path\n\n\n"
                  "def test_refused():\n"
                  "    feature = Path('feature.txt')\n"
                  "    assert feature.exists() and feature.read_text() != 'round 2\\n'\n")


def _progress_sandbox(run, g1_test: str = _PROGRESS_TEST) -> None:
    """V2 with AC-01 bound to a passing test, AC-02 (G1) to a test the feature turns GREEN,
    and AC-03/AC-04 optional: the plan is feasible with one direct task naming AC-02."""
    from maestro import acceptance
    from maestro.docs import goals

    from ..test_goals_contract import V2

    contract = V2.replace(
        "`pytest tests/acceptance/test_import.py::test_valid`",
        "`pytest tests/acceptance/test_ok.py::test_ok`").replace(
        "- **Layer:** e2e\n- **Evidence:** test\n\n### AC-03",
        "- **Layer:** e2e\n- **Evidence:** test\n"
        "- **Test:** `pytest tests/acceptance/test_g1.py::test_refused`\n\n### AC-03").replace(
        "- **Layer:** integration\n- **Evidence:** test\n",
        "- **Layer:** integration\n- **Evidence:** test\n- **Mandatory:** no\n").replace(
        "- **Layer:** e2e\n- **Evidence:** metric\n",
        "- **Layer:** e2e\n- **Evidence:** metric\n- **Mandatory:** no\n")
    (run.repo / "docs").mkdir(parents=True, exist_ok=True)
    (run.repo / "docs" / "GOALS.md").write_text(contract, encoding="utf-8")
    (run.repo / "tests" / "acceptance").mkdir(parents=True, exist_ok=True)
    (run.repo / "tests" / "acceptance" / "test_ok.py").write_text(_OK_TEST)
    (run.repo / "tests" / "acceptance" / "test_g1.py").write_text(g1_test)
    (run.repo / "pytest.ini").write_text("[pytest]\n")
    _git_sandbox(run)
    doc = goals.load(run.repo)
    acceptance.store(run.repo, doc, acceptance.run_baseline(run.repo, doc, timeout=HANG_SECONDS))
    states = {r.id: r.state for r in acceptance.load(run.repo)}
    assert (states["AC-01"], states["AC-02"]) == (acceptance.GREEN, acceptance.RED)


def test_a_direct_task_turning_its_criterion_green_clears_doctor_and_survives_a_restart(
        run_lane, monkeypatch):
    """R35, C28, DESIGN §19.4: TASK-001 (direct, names AC-02) turns AC-02 GREEN and merges.
    The baseline still says RED and no open task names AC-02 any more; the doctor reads
    the observed state, so the plan is still feasible. A fresh process (modules reloaded,
    the store read from disk) shows AC-02 GREEN and where it was observed."""
    import importlib

    from maestro import acceptance, cli, goal_state, goal_store

    from .test_end_to_end import _drive_until_runs_settle

    # GDT-9 review L1: the production gate → store path, seen before the checkpoint's row
    # for the same (criterion, revision) replaces it.
    gate_rows: list = []
    record_gate = goal_state.record_gate

    def spy(*args, **kwargs):
        rows = record_gate(*args, **kwargs)
        gate_rows.extend(dict(r) for r in rows)
        return rows

    monkeypatch.setattr(goal_state, "record_gate", spy)
    agent = _GoalReviewingAgent([("approved", [])])
    _dan_lane(monkeypatch, agent, [])
    run = run_lane(_happy_scenario("e2e_gdt9_progress_survives", tasks=[
        _task("TASK-001", short_desc="refuse broken exports", verifications=_V1,
              serves=["G1"], contribution="direct", criteria=["AC-02"], tests=["AC-02"]),
    ], before_run=_progress_sandbox, on_poll=_drive_until_runs_settle(1)))

    assert run.returncode == 0
    assert _runs() == [("TASK-001", "succeeded")], _journal_details("graph_run_settled")
    assert {r.id: r.state for r in acceptance.load(run.repo)}["AC-02"] == acceptance.RED
    gated = [r for r in gate_rows if r["id"] == "AC-02"]
    assert gated and all((r["source"], r["state"], r["task"], r["revision"]) == (
        "gate", acceptance.GREEN, "TASK-001", 2) and r["commit"] for r in gated), gate_rows
    check = cli._check_goals(run.repo)
    assert "not feasible" not in check.detail and check.required is False, check.detail

    importlib.reload(goal_store)
    importlib.reload(goal_state)
    shown = goal_state.progress(run.repo)
    by_id = {c["id"]: c for c in shown["criteria"]}
    assert (by_id["AC-02"]["state"], by_id["AC-02"]["task"]) == (acceptance.GREEN, "TASK-001")
    assert by_id["AC-02"]["source"] in ("gate", "checkpoint") and by_id["AC-02"]["commit"]
    assert by_id["AC-02"]["protected"] is True
    assert "AC-02" not in shown["unmet"] and shown["revision"] == 2


def test_a_later_task_regressing_a_criterion_an_earlier_task_turned_green_reworks(
        run_lane, monkeypatch):
    """C12: TASK-001 turns AC-02 GREEN (RED at the baseline). TASK-002 (G2 work) breaks it
    in its first round: the gate reruns it as protected, so the round reworks, and the
    second round lands."""
    import maestro.state as _state

    from .test_end_to_end import _drive_until_runs_settle

    agent = _GoalReviewingAgent([("approved", []), ("approved", [])])
    _dan_lane(monkeypatch, agent, [])
    run = run_lane(_happy_scenario("e2e_gdt9_protected_regression", tasks=[
        _task("TASK-001", short_desc="refuse broken exports", verifications=_V1,
              serves=["G1"], contribution="direct", criteria=["AC-02"], tests=["AC-02"]),
        _task("TASK-002", short_desc="sum the totals", deps=["TASK-001"],
              verifications=_V1, serves=["G2"], contribution="direct"),
    ], before_run=_progress_sandbox, on_poll=_drive_until_runs_settle(2)))

    assert run.returncode == 0
    assert _runs() == [("TASK-001", "succeeded"), ("TASK-002", "succeeded")], \
        _journal_details("graph_run_settled")
    gates = [r["status"] for r in _state.control_store().conn.execute(
        "SELECT a.status FROM attempts a JOIN task_runs t ON a.run_id = t.run_id "
        "WHERE a.node_id = 'gate' AND t.task_id = 'TASK-002' ORDER BY a.rowid").fetchall()]
    assert gates == ["failed", "succeeded"]
    writers = _briefs(agent, "implementer")
    assert len(writers) == 3
    assert "acceptance_regression: AC-02 (G1) was green_already_satisfied" in writers[2]
    assert _git(run.repo, "show", f"{TARGET}:{FEATURE}") == "round 3"


_RED_TEST = "def test_refused():\n    assert False, 'the target is already broken'\n"


def _red_on_base_sandbox(run) -> None:
    """`_progress_sandbox` with AC-02 always RED, and an earlier gate's GREEN observation
    of it stored after the baseline (a flaky pass, or a target broken since)."""
    from datetime import datetime, timedelta, timezone

    from maestro import goal_state
    from maestro.docs import goals

    _progress_sandbox(run, _RED_TEST)
    later = (datetime.now(tz=timezone.utc) + timedelta(minutes=5)).isoformat(timespec="seconds")
    goal_state.record_gate(run.repo, goals.load(run.repo), {
        "acceptance": {"AC-02": "green_already_satisfied"},
        "snapshot": _git(run.repo, "rev-parse", "HEAD")},
        task="TASK-000", run_id="run-0", at=later)


def test_a_criterion_red_on_the_base_too_is_a_note_through_gate_journal_and_reviewer(
        run_lane, monkeypatch):
    """GDT-9 verification L3/L4/L5: AC-02 is protected only by an observation, and RED on
    the task's tree and on the base. The real gate notes it (no rework), the controller
    journals the note, the reviewer's brief says it was RED on the base, and a reviewer
    who claims the regression from the failing test is read from the real committed gate
    attempt (`gate_red_on_base`): unchanged_expected, no second round."""
    from .test_end_to_end import _drive_until_runs_settle

    claim = [{"goal": "G1", "impact": "regressed", "evidence": [
        {"kind": "test", "id": "tests/acceptance/test_g1.py::test_refused",
         "result": "failed", "criterion": "AC-02"}]}]
    agent = _GoalReviewingAgent([("approved", claim)])
    _dan_lane(monkeypatch, agent, [])
    run = run_lane(_happy_scenario("e2e_gdt9_red_on_base", tasks=[
        _task("TASK-001", short_desc="sum the totals", verifications=_V1,
              serves=["G2"], contribution="direct"),
    ], before_run=_red_on_base_sandbox, on_poll=_drive_until_runs_settle(1)))

    assert run.returncode == 0
    assert _runs() == [("TASK-001", "succeeded")], _journal_details("graph_run_settled")
    assert len(_briefs(agent, "implementer")) == 1          # the gate and review reworked nothing
    [review] = _briefs(agent, "reviewer")
    assert "RED on the base" in review and "AC-02" in review
    assert any("TASK-001" in d and "AC-02 is RED on the task's tree and on the base" in d
               for d in _journal_details("goal_protected_red_on_base"))


# --- GDT-10: the completion gate at queue exhaustion ------------------------------------------

#: The inner TDD test the scripted implementer commits before its code (L1).
_UNIT_TEST = ("from ledger import total\n\n\n"
              "def test_total():\n    assert total([1, 2]) == 3\n")
_LEDGER = "def total(amounts):\n    return sum(amounts)\n"


def _finish_sandbox(run, g1_test: str = _G1_TEST, *, operator: bool = False) -> None:
    """`_progress_sandbox`'s contract (AC-01 GREEN, AC-02 bound to `g1_test`, AC-03/AC-04
    optional) with OP-01 mandatory only when `operator`; a project pytest config that
    imports from the root, and `__pycache__/` ignored (the inner unit tests run in the
    task's tree)."""
    from maestro import acceptance
    from maestro.docs import goals

    from ..test_goals_contract import V2

    contract = V2.replace(
        "`pytest tests/acceptance/test_import.py::test_valid`",
        "`pytest tests/acceptance/test_ok.py::test_ok`").replace(
        "- **Layer:** e2e\n- **Evidence:** test\n\n### AC-03",
        "- **Layer:** e2e\n- **Evidence:** test\n"
        "- **Test:** `pytest tests/acceptance/test_g1.py::test_refused`\n\n### AC-03").replace(
        "- **Layer:** integration\n- **Evidence:** test\n",
        "- **Layer:** integration\n- **Evidence:** test\n- **Mandatory:** no\n").replace(
        "- **Layer:** e2e\n- **Evidence:** metric\n",
        "- **Layer:** e2e\n- **Evidence:** metric\n- **Mandatory:** no\n")
    if not operator:
        contract = contract.replace("- **Sign-off:** Dan\n",
                                    "- **Sign-off:** Dan\n- **Mandatory:** no\n")
    (run.repo / "docs").mkdir(parents=True, exist_ok=True)
    (run.repo / "docs" / "GOALS.md").write_text(contract, encoding="utf-8")
    (run.repo / "tests" / "acceptance").mkdir(parents=True, exist_ok=True)
    (run.repo / "tests" / "acceptance" / "test_ok.py").write_text(_OK_TEST)
    (run.repo / "tests" / "acceptance" / "test_g1.py").write_text(g1_test)
    (run.repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
    _git_sandbox(run)
    with (run.repo / ".gitignore").open("a", encoding="utf-8") as handle:
        handle.write("__pycache__/\n")
    _git(run.repo, "commit", "-qam", "ignore bytecode")
    doc = goals.load(run.repo)
    acceptance.store(run.repo, doc, acceptance.run_baseline(run.repo, doc, timeout=HANG_SECONDS))


def _tell(monkeypatch):
    """What the completion gate tells Dan. The lane installs its notify doubles after the
    test body runs, so the gate's own send is spied on instead (it still calls the
    lane's double)."""
    from maestro import goal_completion

    told: list[str] = []
    real = goal_completion._tell

    def spy(repo, v, text, notify, journal):
        def send(message):
            told.append(message)
            return notify(message)

        return real(repo, v, text, send, journal)

    monkeypatch.setattr(goal_completion, "_tell", spy)
    return told


#: AC-02 for the cap lane: GREEN in every task worktree, RED on the controller's checkout,
#: where a gitignored local file stays behind. A corrective task's gate check passes, yet
#: the completion gate's rerun on the final tree does not.
_STALE_TEST = ("from pathlib import Path\n\n\n"
               "def test_refused():\n"
               "    assert not Path('stale-cache.txt').exists(), 'a stale local cache'\n")


def _stale_sandbox(run) -> None:
    from maestro import acceptance
    from maestro.docs import goals

    _finish_sandbox(run, _STALE_TEST)
    # The stop policy names no number here, so `thresholds.goal_finish_rounds` is the cap.
    path = run.repo / "docs" / "GOALS.md"
    path.write_text(path.read_text(encoding="utf-8").replace(
        "two attempts miss the same criterion", "an approach keeps missing the same criterion"),
        encoding="utf-8")
    with (run.repo / ".gitignore").open("a", encoding="utf-8") as handle:
        handle.write("stale-cache.txt\n")
    _git(run.repo, "commit", "-qam", "ignore the local cache")
    (run.repo / "stale-cache.txt").write_text("stale\n", encoding="utf-8")
    doc = goals.load(run.repo)
    acceptance.store(run.repo, doc, acceptance.run_baseline(run.repo, doc, timeout=HANG_SECONDS))


def _finish_rows() -> list[str]:
    return _journal_details("goal_finish_verdict")


class _TddAgent(_GoalReviewingAgent):
    """The implementer commits its unit test first, then the code (inner TDD)."""

    def complete(self, spec):
        if spec.prompt.startswith("You are the implementer "):
            (spec.cwd / "tests" / "unit").mkdir(parents=True, exist_ok=True)
            (spec.cwd / "tests" / "unit" / "test_total.py").write_text(_UNIT_TEST)
            _git(spec.cwd, "add", "tests/unit/test_total.py")
            _git(spec.cwd, "commit", "-q", "-m", "test: the total, before the code")
            (spec.cwd / "ledger.py").write_text(_LEDGER)
            _git(spec.cwd, "add", "ledger.py")
        return super().complete(spec)


def test_red_to_green_replay_keeps_inner_tdd_and_the_finish_says_complete(
        run_lane, monkeypatch):
    """R21, C11, R53, R56: AC-02 is RED at the baseline; TASK-001 names it and its gate
    check runs the unit tests. The implementer commits the unit test first, then the code;
    the gate passes V1 and reruns AC-02 GREEN, nothing reworks. At exhaustion the
    completion gate reruns the contract on HEAD: complete, the report shows AC-02 baseline
    RED → finish GREEN, Dan is told "✅ Project complete", then the queue is exhausted."""
    import sys

    from maestro import goal_completion

    from .test_end_to_end import _drive_every_worker_of_any_run

    agent = _TddAgent([("approved", [])])
    _dan_lane(monkeypatch, agent, [])
    told = _tell(monkeypatch)
    unit = [{"id": "V1", "kind": "auto",
             "cmd": f"{sys.executable} -m pytest tests/unit -p no:cacheprovider"}]

    def on_poll(run) -> None:
        if "queue_exhausted" in run.journal_events():
            run.halt()
            return
        _drive_every_worker_of_any_run(run)

    run = run_lane(_happy_scenario("e2e_gdt10_red_to_green", tasks=[
        _task("TASK-001", short_desc="refuse broken exports", verifications=unit,
              serves=["G1"], contribution="direct", criteria=["AC-02"], tests=["AC-02"]),
    ], before_run=_finish_sandbox, on_poll=on_poll))

    assert run.returncode == 0
    assert _runs() == [("TASK-001", "succeeded")], _journal_details("graph_run_settled")
    log = _git(run.repo, "log", "--format=%s", TARGET).splitlines()
    assert log.index("test: the total, before the code") > log.index(
        "feat: the feature, round 1")
    evidence = maestro.state.control_store().conn.execute(
        "SELECT check_id, outcome FROM verification_evidence").fetchall()
    assert ("V1", "passed") in [(e["check_id"], e["outcome"]) for e in evidence]
    assert len(_briefs(agent, "implementer")) == 1
    events = run.journal_events()
    assert events.index("goal_finish_verdict") < events.index("queue_exhausted")
    assert _finish_rows()[-1].startswith("complete")
    report = (run.repo / goal_completion.REPORT_MD).read_text(encoding="utf-8")
    assert "Verdict: complete" in report
    assert "AC-02 GREEN" in report and "baseline RED" in report
    finished = [t for t in told if t.startswith("✅ Project complete")]
    assert len(finished) == 1 and told.index(finished[0]) == 0


class _CauseWritingAgent(_GoalReviewingAgent):
    """A GDT-8 diagnosis brief names the file its cause goes in: the implementer writes and
    commits it there (any other implementer round commits the feature)."""

    def complete(self, spec):
        found = re.search(r"docs/diagnosis/[\w.-]+\.md", spec.prompt)
        if spec.prompt.startswith("You are the implementer ") and found:
            self.specs.append(spec)
            path = spec.cwd / found.group(0)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Cause: harness\n\nAC-02 reads a stale local cache on the "
                            "controller.\n")
            _git(spec.cwd, "add", found.group(0))
            _git(spec.cwd, "commit", "-q", "-m", "docs: the written cause")
            workspace = Path(re.search(r"^WORKSPACE: (\S+)$", spec.prompt,
                                       re.MULTILINE).group(1))
            handlers.write_sentinel(workspace, handlers.DONE_SENTINEL, "done")
            return Completion(text="done", returncode=0, timed_out=False)
        return super().complete(spec)


def test_an_empty_queue_with_a_red_criterion_works_again_then_asks_at_the_cap(
        run_lane, monkeypatch):
    """R54, R55, C30, C31: nothing is queued while AC-02 is RED. The completion gate adds
    round 1 (`ac-02-…-<rp>`) through GDT-8's replan path; it lands and runs without fixing
    AC-02. Its graduation is a GDT-8 milestone gap: the diagnosis task it adds carries its
    own check (review H3), dispatches and settles. At the next exhaustion the cap
    (`thresholds.goal_finish_rounds: 1`) is reached and Dan is asked once, naming the
    completion gate. He taps Keep going: the project stays blocked, no third round is
    added and the queue is never called exhausted."""
    from maestro import goal_replan, orchestrator

    from . import conftest as lane_harness
    from .test_dispatch_wiring import GRAPH
    from .test_end_to_end import _drive_every_worker_of_any_run

    # Round 1, then the diagnosis run, then the cap question do not fit in 12 polls.
    monkeypatch.setattr(lane_harness, "MAX_POLLS", 24)
    agent = _CauseWritingAgent([("approved", [])] * 6)
    dan = _dan_lane(monkeypatch, agent, [("tap", 2)])          # Keep going
    monkeypatch.setattr(orchestrator, "_judge_complete", lambda *a, **k: "")
    told = _tell(monkeypatch)
    after: list[int] = []

    def on_poll(run) -> None:
        dan.answer()
        replans = [r for r in _store(run)["replans"]
                   if (r.get("trigger") or {}).get("kind") == "finish_gap"]
        if any(r.get("status") == "decided" for r in replans):
            if len(after) >= 2:
                run.halt()
                return
            after.append(run.polls)
        _drive_every_worker_of_any_run(run)

    run = run_lane(_happy_scenario(
        "e2e_gdt10_cap", tasks=[], project_yaml={**GRAPH,
                                                 "thresholds": {"goal_finish_rounds": 1}},
        before_run=_stale_sandbox, on_poll=on_poll))

    assert run.returncode == 0
    finish = [r for r in _store(run)["replans"]
              if (r.get("trigger") or {}).get("kind") == "finish_gap"]
    assert [r["status"] for r in finish] == ["applied", "decided"], finish
    assert finish[1]["decision"]["resolution"] == "keep_going"
    added = finish[0]["applied"]
    assert added and added[0].startswith("ac-02-")
    assert (added[0], "succeeded") in _runs()
    diagnosis = [t for r in _store(run)["replans"] for t in r.get("applied") or ()
                 if "-diagnose-" in t]
    assert diagnosis and (diagnosis[0], "succeeded") in _runs(), _runs()
    assert not _journal_details("pre_validation_refused")
    [(text, _)] = dan.sent
    assert "completion gate" in text and goal_replan.KEEP_GOING in text
    assert "queue_exhausted" not in run.journal_events()
    assert _finish_rows()[-1].startswith("blocked")
    assert any(t.startswith("🔁 Not done yet — incomplete (round 1 of 1)") for t in told)
    assert any(t.startswith("⏸ Not done — blocked") for t in told)
    assert not any(t.startswith("✅") for t in told)


def test_every_remaining_task_goal_held_is_reported_blocked_not_exhausted(
        run_lane, monkeypatch):
    """R54: TASK-001 names AC-02 but serves no goal, so the goals gate holds it and nothing
    is runnable. That is not "done": the completion gate reports the project blocked and
    tells Dan which task is held and why."""
    agent = _GoalReviewingAgent([])
    _dan_lane(monkeypatch, agent, [])
    told = _tell(monkeypatch)

    def on_poll(run) -> None:
        if "goal_finish_verdict" in run.journal_events():
            run.halt()

    run = run_lane(_happy_scenario("e2e_gdt10_held", tasks=[
        _task("TASK-001", short_desc="refuse broken exports", verifications=_V1,
              criteria=["AC-02"]),
    ], before_run=lambda r: _finish_sandbox(r, _RED_TEST), on_poll=on_poll))

    assert run.returncode == 0
    assert _runs() == []
    assert "queue_exhausted" not in run.journal_events()
    assert _finish_rows()[-1].startswith("blocked")
    [text] = [t for t in told if t.startswith("⏸ Not done — blocked")]
    assert "TASK-001" in text and "names no goal it serves" in text
    assert not any("All queued tasks are done" in t for t in told)


def test_an_operator_check_is_asked_on_the_final_tree_and_dan_s_pass_completes(
        run_lane, monkeypatch):
    """R14, C9: every automated criterion is GREEN and OP-01 is mandatory. Nothing is
    queued; the completion gate asks Dan for OP-01 on the final tree (no recommendation),
    the queue waits on him, his Pass is recorded at this revision and commit, and the
    project completes."""
    from maestro import goal_store

    agent = _GoalReviewingAgent([])
    dan = _dan_lane(monkeypatch, agent, [("tap", 0)])          # Pass
    told = _tell(monkeypatch)

    def on_poll(run) -> None:
        dan.answer()
        if "queue_exhausted" in run.journal_events():
            run.halt()

    run = run_lane(_happy_scenario(
        "e2e_gdt10_operator", tasks=[],
        before_run=lambda r: _finish_sandbox(r, _OK_TEST.replace("test_ok", "test_refused"),
                                             operator=True),
        on_poll=on_poll))

    assert run.returncode == 0
    [(text, markup)] = dan.sent
    assert "Operator check OP-01" in text and "every category total is the same." in text
    assert [b["text"] for row in markup["inline_keyboard"] for b in row] == ["1", "2"]
    [row] = goal_store.read(run.repo)["operator"]
    head = _git(run.repo, "rev-parse", TARGET)
    assert (row["id"], row["result"], row["commit"], row["revision"]) == (
        "OP-01", "pass", head, 2)
    events = run.journal_events()
    assert "goal_finish_signoff" in events
    assert events.index("goal_finish_signoff") < events.index("queue_exhausted")
    assert any(t.startswith("✅ Project complete") and "1 operator checks verified" in t
               for t in told)
