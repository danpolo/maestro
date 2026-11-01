"""Milestone checkpoints: residual RED found before the final gate (P13 GDT-8).

A checkpoint runs at meaningful milestones only (`01` §7): a completed phase, an enabling
dependency unblocked, a risky integration, a strategic pivot, convergence; never after an
ordinary graduation (R43). It reruns only the touched goals' mandatory bound criteria on
the merged tree and never writes the acceptance baseline (R65). Every remaining criterion
RED despite accepted tasks is a milestone gap (C26); a criterion that no longer gives a
credible result is a targeted follow-up, not a forced verdict (R42). Requirements: R42,
R43, R65 (`docs/agent-plans/p13-goal-driven-tdd/`).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from maestro import acceptance
from maestro import goal_milestones as gm
from maestro import goal_replan as gr
from maestro.docs import goals

REPO_ROOT = Path(__file__).resolve().parents[1]
V2 = (REPO_ROOT / "tests" / "fixtures" / "goals" / "v2_GOALS.md").read_text(encoding="utf-8")
#: V2 with G1's AC-02 and G3's metric AC-04 bound too (AC-01 already is; AC-03 is not).
BOUND = V2.replace(
    "- **Layer:** e2e\n- **Evidence:** test\n\n### AC-03",
    "- **Layer:** e2e\n- **Evidence:** test\n"
    "- **Test:** `pytest tests/acceptance/test_import.py::test_refused`\n\n### AC-03").replace(
    "- **Layer:** e2e\n- **Evidence:** metric\n",
    "- **Layer:** e2e\n- **Evidence:** metric\n- **Test:** `python bench/year.py`\n")
DAY = "2026-10-18T09:00:00+00:00"
EMPTY = {"decisions": [], "episodes": [], "checkpoints": [], "replans": []}


def _task(tid: str, goal: str = "G1", contribution: str = "direct", **extra) -> dict:
    serves = goal if isinstance(goal, list) else ([goal] if goal else [])
    return {"id": tid, "status": "open", "serves": serves, "contribution": contribution,
            **extra}


def _repo(tmp_path: Path, baseline: dict) -> tuple[Path, goals.Goals]:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "GOALS.md").write_text(BOUND, encoding="utf-8")
    doc = goals.load(tmp_path)
    acceptance.store(tmp_path, doc, [
        acceptance.Result(id=cid, state=state, reason="baseline", revision=doc.revision)
        for cid, state in baseline.items()])
    return tmp_path, doc


def _runner(outcomes: dict):
    """A fake `subprocess.run`: the first `outcomes` key found in the command decides —
    `pass`/`fail`/`blocked` for a pytest binding, the printed text for any other."""
    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        line = " ".join(cmd)
        out = next((v for k, v in outcomes.items() if k in line), "pass")
        junit = next((a.split("=", 1)[1] for a in cmd if a.startswith("--junitxml=")), None)
        if junit is None:
            return subprocess.CompletedProcess(cmd, 0, out, "")
        if out == "blocked":
            return subprocess.CompletedProcess(cmd, 4, "", "ERROR: file not found")
        case = ('<testcase name="t"/>' if out == "pass" else
                '<testcase name="t"><failure message="assert False"/></testcase>')
        Path(junit).write_text(f"<testsuite>{case}</testsuite>", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0 if out == "pass" else 1, "", "")

    return run, calls


def test_each_milestone_kind_is_detected_and_an_ordinary_graduation_is_not():
    doc = goals.parse(V2)
    other = _task("T2")
    assert gm.milestones(_task("T1"), [other], doc, EMPTY) == {}
    assert gm.milestones(_task("T1", group_id="P1"), [_task("T2", group_id="P2")], doc,
                         EMPTY) == {"phase_complete": ["G1"]}
    assert gm.milestones(_task("T1", group="P1"), [_task("T2", group_id="P1")], doc,
                         EMPTY) == {}
    assert gm.milestones(_task("T1", contribution="enabling"), [_task("T2", deps=["T1"])],
                         doc, EMPTY) == {"enabling_unblocked": ["G1"]}
    assert gm.milestones(_task("T1", risk="high"), [other], doc, EMPTY) == {
        "risky_integration": ["G1"]}
    older = dict(EMPTY, checkpoints=[{"id": "CP-1", "revision": 1, "at": DAY}])
    assert gm.milestones(_task("T1"), [other], doc, older) == {"strategic_pivot": ["G1"]}
    replanned = dict(EMPTY, checkpoints=[{"id": "CP-1", "revision": 2, "at": DAY}],
                     replans=[{"id": "RP-1", "goal": "G2", "status": "applied",
                               "settled_at": "2026-10-18T10:00:00+00:00"}])
    assert gm.milestones(_task("T1"), [other], doc, replanned) == {"strategic_pivot": ["G1"]}
    same = dict(replanned, replans=[dict(replanned["replans"][0], settled_at=DAY)])
    assert gm.milestones(_task("T1"), [other], doc, same) == {}
    assert gm.milestones(_task("T1"), [], doc, EMPTY) == {"convergence": ["G1"]}
    assert gm.milestones(_task("T1"), [_task("T2", status="complete")], doc, EMPTY) == {
        "convergence": ["G1"]}
    assert gm.milestones(_task("T1", "", "maintenance"), [], doc, EMPTY) == {}
    assert set(gm.MILESTONES) == {"phase_complete", "enabling_unblocked", "risky_integration",
                                  "strategic_pivot", "convergence"}


def test_a_checkpoint_reruns_only_the_touched_goals_and_writes_no_baseline(tmp_path):
    repo, doc = _repo(tmp_path, {"AC-01": acceptance.GREEN, "AC-02": acceptance.RED,
                                 "AC-04": acceptance.GREEN})
    before = acceptance.path_for(repo).read_bytes()
    run, calls = _runner({})
    record, follow_ups = gm.checkpoint(repo, doc, {"convergence": ["G1"]}, _task("T1"), [],
                                       store=EMPTY, at=DAY, runner=run)
    assert acceptance.path_for(repo).read_bytes() == before
    assert len(calls) == 2 and not any("bench" in " ".join(c) for c in calls)
    assert record["goals"] == {"G1": {"AC-01": acceptance.GREEN, "AC-02": acceptance.GREEN}}
    assert record["before"] == {"G1": {"AC-01": acceptance.GREEN, "AC-02": acceptance.RED}}
    assert (record["task"], record["revision"], record["at"]) == ("T1", 2, DAY)
    assert record["milestones"] == {"convergence": ["G1"]} and record["id"].startswith("CP-")
    assert record["unknown"] == [] and record["feasibility"] == {} and follow_ups == []
    # the next checkpoint compares with this one, not with the baseline
    later, _ = gm.checkpoint(repo, doc, {"phase_complete": ["G1"]}, _task("T9"), [],
                             store=dict(EMPTY, checkpoints=[record]), at=DAY, runner=run)
    assert later["before"] == {"G1": {"AC-01": acceptance.GREEN, "AC-02": acceptance.GREEN}}


def test_all_remaining_red_despite_accepted_tasks_is_a_gap(tmp_path):
    repo, doc = _repo(tmp_path, {"AC-01": acceptance.RED, "AC-02": acceptance.RED})
    progress = [{"goal": "G1", "impact": "advanced", "served": True, "evidence": [
        {"kind": "test", "id": "tests/acceptance/test_import.py::test_refused",
         "result": "passed", "criterion": "AC-02"}]}]
    episodes = gr.episodes(_task("T1"), progress, doc, run_id="r1", workflow_id="wf_T1",
                           at=DAY)
    still_red, _ = _runner({"test_import.py": "fail"})
    record, _ = gm.checkpoint(repo, doc, {"phase_complete": ["G1"]}, _task("T1"),
                              [_task("T2")], store=EMPTY, at=DAY, runner=still_red)
    store = dict(EMPTY, episodes=episodes, checkpoints=[record])
    [signal] = gr.triggers(store, doc, record)
    assert (signal["goal"], signal["kind"]) == ("G1", "milestone_gap")
    assert signal["criteria"] == ["AC-01", "AC-02"] and signal["episodes"] == ["T1:G1"]
    # one criterion turned GREEN since the baseline: the route converges, no gap ...
    moving, _ = _runner({"test_refused": "pass", "test_import.py": "fail"})
    record, _ = gm.checkpoint(repo, doc, {"phase_complete": ["G1"]}, _task("T1"),
                              [_task("T2")], store=EMPTY, at=DAY, runner=moving)
    assert gr.triggers(dict(store, checkpoints=[record]), doc, record) == []
    # ... unless nothing is left to move it: at convergence any RED is a gap
    record, _ = gm.checkpoint(repo, doc, {"convergence": ["G1"]}, _task("T1"), [],
                              store=EMPTY, at=DAY, runner=moving)
    [signal] = gr.triggers(dict(store, checkpoints=[record]), doc, record)
    assert (signal["kind"], signal["criteria"]) == ("milestone_gap", ["AC-01"])


def test_unknown_at_a_checkpoint_is_a_targeted_follow_up(tmp_path):
    repo, doc = _repo(tmp_path, {"AC-01": acceptance.BLOCKED, "AC-02": acceptance.RED,
                                 "AC-04": acceptance.GREEN})
    run, _ = _runner({"bench/year.py": "fast", "test_valid": "blocked",
                      "test_refused": "fail"})
    record, follow_ups = gm.checkpoint(repo, doc, {"risky_integration": ["G1", "G3"]},
                                       _task("T1", ["G1", "G3"]), [_task("T2")],
                                       store=EMPTY, at=DAY, runner=run)
    assert [u["criterion"] for u in record["unknown"]] == ["AC-01", "AC-04"]
    assert record["goals"]["G3"] == {"AC-04": acceptance.UNKNOWN}
    # AC-01 was already blocked at the baseline (GDT-4 plans its harness task): no noise
    [follow] = follow_ups
    assert follow.startswith("Goal G3 checkpoint (risky_integration after T1): AC-04 is "
                             f"{acceptance.UNKNOWN}")
    assert "python bench/year.py" in follow
    store = dict(EMPTY, checkpoints=[record])
    assert gr.triggers(store, doc, record) == []    # an unknown never forces a verdict


# --- GDT-8 review ----------------------------------------------------------------------------


def test_a_checkpoint_journals_each_criterion_and_stops_at_its_time_cap(tmp_path):
    """Review H1: graduation runs the checkpoint with no other journal write, and the
    watchdog kills a loop silent for 20 minutes. Each criterion is journaled before it runs;
    once the checkpoint's cap is spent the rest are not rerun: UNKNOWN, each a targeted
    follow-up, never a verdict."""
    repo, doc = _repo(tmp_path, {"AC-01": acceptance.GREEN, "AC-02": acceptance.GREEN,
                                 "AC-04": acceptance.GREEN})
    run, calls = _runner({"bench/year.py": "8"})
    now = [0.0]

    def slow(cmd, **kwargs):
        now[0] += 400.0
        return run(cmd, **kwargs)

    beats: list[str] = []
    record, follow_ups = gm.checkpoint(
        repo, doc, {"risky_integration": ["G1", "G3"]}, _task("T1", ["G1", "G3"]),
        [_task("T2")], store=EMPTY, at=DAY, runner=slow, heartbeat=beats.append,
        clock=lambda: now[0], budget=600)
    assert len(calls) == 2 and len(beats) == 2
    assert "AC-01" in beats[0] and "AC-02" in beats[1] and "T1" in beats[0]
    assert "G3" not in record["goals"]          # not rerun is no verdict (verification L2)
    assert record["skipped"] == {"G3": ["AC-04"]}
    [skipped] = record["unknown"]
    assert skipped["criterion"] == "AC-04" and "600" in skipped["reason"]
    [follow] = follow_ups
    assert "AC-04" in follow and "cap" in follow and "python bench/year.py" in follow
    assert record["follow_ups"] == follow_ups
    assert 0 < gm.CHECKPOINT_BUDGET <= 600


def test_before_is_the_last_checkpoint_that_judged_the_goal(tmp_path):
    """Review L4: a later checkpoint that touched only G3 is not G1's "before"."""
    repo, doc = _repo(tmp_path, {"AC-01": acceptance.RED, "AC-02": acceptance.RED,
                                 "AC-04": acceptance.GREEN})
    run, _ = _runner({})
    g1 = {"id": "CP-1", "at": DAY, "task": "T0",
          "goals": {"G1": {"AC-01": acceptance.GREEN, "AC-02": acceptance.GREEN}}}
    g3 = {"id": "CP-2", "at": DAY, "task": "T5", "goals": {"G3": {"AC-04": acceptance.GREEN}}}
    record, _ = gm.checkpoint(repo, doc, {"phase_complete": ["G1"]}, _task("T9"), [],
                              store=dict(EMPTY, checkpoints=[g1, g3]), at=DAY, runner=run)
    assert record["before"] == {"G1": {"AC-01": acceptance.GREEN, "AC-02": acceptance.GREEN}}


# --- GDT-8 verification lows ------------------------------------------------------------------


def test_a_skipped_criterion_is_no_verdict_for_the_next_before_or_the_gap(tmp_path):
    """Verification L2: a criterion the cap skipped is stored apart (`skipped`), never as an
    UNKNOWN state: the next checkpoint's `before` ignores it and a gap is not judged on the
    criteria that did run."""
    repo, doc = _repo(tmp_path, {"AC-01": acceptance.RED, "AC-02": acceptance.RED,
                                 "AC-04": acceptance.GREEN})
    run, _ = _runner({"test_import.py": "fail"})
    now = [0.0]

    def slow(cmd, **kwargs):
        now[0] += 400.0
        return run(cmd, **kwargs)

    capped, _ = gm.checkpoint(repo, doc, {"convergence": ["G1"]}, _task("T1"), [],
                              store=EMPTY, at=DAY, runner=slow, clock=lambda: now[0],
                              budget=300)
    assert capped["goals"] == {"G1": {"AC-01": acceptance.RED}}
    assert capped["skipped"] == {"G1": ["AC-02"]} and capped["before"]["G1"] == {
        "AC-01": acceptance.RED}
    assert gr.triggers(dict(EMPTY, checkpoints=[capped]), doc, capped) == []
    # a checkpoint that skipped the whole goal leaves the earlier verdicts as "before"
    earlier = {"id": "CP-0", "at": DAY, "task": "T0",
               "goals": {"G1": {"AC-01": acceptance.GREEN, "AC-02": acceptance.GREEN}}}
    all_skipped, _ = gm.checkpoint(repo, doc, {"convergence": ["G1"]}, _task("T1"), [],
                                   store=dict(EMPTY, checkpoints=[earlier]), at=DAY,
                                   runner=run, budget=0)
    assert "G1" not in all_skipped["goals"] and all_skipped["skipped"] == {
        "G1": ["AC-01", "AC-02"]}
    later, _ = gm.checkpoint(repo, doc, {"phase_complete": ["G1"]}, _task("T9"), [],
                             store=dict(EMPTY, checkpoints=[earlier, all_skipped]), at=DAY,
                             runner=run)
    assert later["before"] == {"G1": {"AC-01": acceptance.GREEN, "AC-02": acceptance.GREEN}}


def test_the_checkpoint_budget_follows_the_watchdog_stall_window(monkeypatch, tmp_path):
    """Verification L3: the budget plus one criterion's timeout (and a margin) fits the
    configured `stall_window_min`; never above the default cap, never negative."""
    assert gm.budget_for(20) == gm.CHECKPOINT_BUDGET == 600
    assert gm.budget_for(120) == gm.CHECKPOINT_BUDGET
    small = gm.budget_for(10)
    assert 0 < small < 600 and small + acceptance.DEFAULT_TIMEOUT < 10 * 60
    assert gm.budget_for(1) == 0
    from maestro import watchdog

    monkeypatch.setattr(watchdog, "STALL_WINDOW_MIN", 7)
    repo, doc = _repo(tmp_path, {"AC-01": acceptance.GREEN, "AC-02": acceptance.GREEN})
    run, calls = _runner({})
    now = [0.0]

    def slow(cmd, **kwargs):
        now[0] += 400.0
        return run(cmd, **kwargs)

    record, _ = gm.checkpoint(repo, doc, {"convergence": ["G1"]}, _task("T1"), [],
                              store=EMPTY, at=DAY, runner=slow, clock=lambda: now[0])
    assert len(calls) == 1 and record["skipped"] == {"G1": ["AC-02"]}
