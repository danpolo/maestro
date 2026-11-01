"""Modified-B replanning: per-goal triggers, proposals, authority and holds (P13 GDT-8).

Q1 is resolved as modified B (`01` §7): a replan is considered on (a) repeated meaningful
unexpected misses (two by default, `thresholds.goal_replan_misses`), (b) one decisive
contradiction, or (c) a milestone gap. `enabled`, `unchanged_expected`, enabling and
maintenance work, `serves: []` and operator-only goals never trigger (R44, R70, C25, C26).
The proposal carries root cause, scope, alternatives, cost, impact and authority (R45). A
local recommendation is applied; a reserved one asks Dan once and holds only the goal's
dependent work while independent work continues (R46, C27). Dan's answer settles the
signal's facts: only new evidence asks again (R44). Requirements: R42, R44-R46, R70
(`docs/agent-plans/p13-goal-driven-tdd/`). The lanes are in
`tests/graph_engineering/test_goals_lanes.py`.
"""
from __future__ import annotations

from pathlib import Path

from maestro import acceptance, goal_replan as gr, goal_roadmap, goal_store
from maestro.docs import goals
from maestro.workflows import dan_questions
from maestro.workflows import goal_progress as gp

REPO_ROOT = Path(__file__).resolve().parents[1]
V2 = (REPO_ROOT / "tests" / "fixtures" / "goals" / "v2_GOALS.md").read_text(encoding="utf-8")
#: V2 whose `Ask Dan:` also reserves plan changes, so even a diagnosis task asks Dan.
ASK_PLANS = V2.replace("any goal conflict, paid services.",
                       "any goal conflict, paid services, plan changes.")
DAY = "2026-10-18T09:00:00+00:00"

#: A served direct task that did not move G2: AC-03's test still fails.
MISS = {"goal": "G2", "impact": "no_progress_unexpected", "claimed": "no_progress_unexpected",
        "served": True, "evidence": [{"kind": "test", "id": "tests/test_totals.py::test_match",
                                      "result": "failed", "criterion": "AC-03"}]}
G2_CHECK = {"mode": "served", "serves": ["G2"], "goal_ids": ["G1", "G2", "G3"],
            "contribution": {"G2": "direct"},
            "criteria": {"AC-03": {"goals": ["G2"], "layer": "integration", "evidence": "test",
                                   "test": "tests/test_totals.py::test_match",
                                   "baseline": acceptance.RED, "direction": None}}}
G3_CHECK = {"mode": "served", "serves": ["G3"], "goal_ids": ["G1", "G2", "G3"],
            "contribution": {"G3": "direct"},
            "criteria": {"AC-04": {"goals": ["G3"], "layer": "e2e", "evidence": "metric",
                                   "test": "`python bench.py`", "baseline": acceptance.RED,
                                   "direction": "below"}}}
#: The reviewer's proof that AC-04 (under 10 s) cannot be met on this route.
INFEASIBLE = {"goal": "G3", "impact": "no_progress_unexpected",
              "evidence": [{"kind": "metric", "name": "import seconds", "value": 31.0,
                            "baseline": 42.0, "criterion": "AC-04"}],
              "infeasible": "parsing alone takes 30 s on the reference machine"}


def _doc(text: str = V2) -> goals.Goals:
    return goals.parse(text)


def _task(tid: str, goal: str = "G2", contribution: str = "direct", **extra) -> dict:
    return {"id": tid, "status": "open", "mode": "autonomous",
            "serves": [goal] if goal else [], "contribution": contribution, **extra}


def _episodes(tid, records=(MISS,), *, text=V2, contribution="direct", goal="G2"):
    return gr.episodes(_task(tid, goal, contribution), list(records), _doc(text),
                       run_id=f"run-{tid}", workflow_id=f"wf_{tid}", at=DAY)


def _store(*episodes, replans=()) -> dict:
    return {"decisions": [], "episodes": [e for eps in episodes for e in eps],
            "checkpoints": [], "replans": list(replans)}


#: Open work: T3 is G2's next direct task, T5 depends on it, T4 is independent G1 work and
#: T6 is G2 maintenance.
OPEN = [_task("T3"), _task("T4", "G1"), _task("T5", "G1", deps=["T3"]),
        _task("T6", "G2", "maintenance")]


class _Dan:
    """Dan's channel: records each question and answers from `answers` by question id."""

    def __init__(self, answers=None):
        self.answers = dict(answers or {})
        self.asked: list[dict] = []
        self.recorded: list[tuple[str, dict]] = []
        self.journaled: list[tuple[str, str]] = []
        self.notified: list[str] = []

    def poll_question(self, spec):
        if spec["id"] not in [s["id"] for s in self.asked]:
            self.asked.append(dict(spec))
        return self.answers.get(spec["id"])

    def mark_question_recorded(self, req_id, **fields):
        self.recorded.append((req_id, fields))

    def journal(self, event, detail):
        self.journaled.append((event, detail))

    def notify(self, text):
        self.notified.append(text)
        return True


def _never_run(*args, **kwargs):
    raise AssertionError(f"no criterion should run here: {args}")


def _repo(tmp_path: Path, text: str = V2) -> Path:
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs" / "GOALS.md").write_text(text, encoding="utf-8")
    return tmp_path


def _graduate(repo, tid, records=(MISS,), *, dan, open_tasks=OPEN, goal="G2",
              contribution="direct"):
    return gr.on_graduation(repo, _task(tid, goal, contribution), list(records),
                            list(open_tasks), dan, run_id=f"run-{tid}",
                            workflow_id=f"wf_{tid}", at=DAY, notify=dan.notify, misses=2,
                            runner=_never_run)


def _ask_and_answer(repo, dan, answer):
    _graduate(repo, "T1", dan=dan)
    _graduate(repo, "T2", dan=dan)
    [question] = dan.asked
    dan.answers[question["id"]] = answer
    return question


# --- triggers (R44, R70, C25, C26) -----------------------------------------------------------


def test_two_unexpected_direct_misses_trigger_and_enabling_or_unrelated_do_not():
    doc = _doc()
    [signal] = gr.triggers(_store(_episodes("T1"), _episodes("T2")), doc)
    assert (signal["goal"], signal["kind"]) == ("G2", "repeated_misses")
    assert signal["facts"] == ["T1:G2", "T2:G2"] and signal["episodes"] == ["T1:G2", "T2:G2"]
    assert signal["criteria"] == ["AC-03"]
    assert "T1" in signal["evidence"][0] and "AC-03" in signal["evidence"][0]
    assert gr.triggers(_store(_episodes("T1")), doc) == []
    # C25: two enabling tasks; the rule reads their no_progress_unexpected as expected
    check = dict(G2_CHECK, contribution={"G2": "enabling"})
    demoted = gp.read({"goal_progress": [MISS]}, check).records
    assert [r["impact"] for r in demoted] == ["unchanged_expected"]
    enabling = _store(_episodes("T1", demoted, contribution="enabling"),
                      _episodes("T2", demoted, contribution="enabling"))
    assert gr.triggers(enabling, doc) == []
    # unrelated: one miss each on two goals
    g1 = dict(MISS, goal="G1", evidence=[dict(MISS["evidence"][0], criterion="AC-02")])
    assert gr.triggers(_store(_episodes("T1"), _episodes("T2", [g1], goal="G1")), doc) == []
    # legitimate impacts, GDT-6/7's own and a W11a record never count
    for impact in ("enabled", "unchanged_expected", "advanced", "unknown", "regressed",
                   gp.LEGACY_NO_PROGRESS):
        records = [dict(MISS, impact=impact)]
        assert gr.triggers(_store(_episodes("T1", records), _episodes("T2", records)),
                           doc) == [], impact


def test_threshold_comes_from_project_yaml_and_defaults_to_two():
    assert gr.miss_threshold(document={}) == 2
    assert gr.miss_threshold(document={"thresholds": {"goal_replan_misses": 3}}) == 3
    for bad in (0, -1, "many", None):
        assert gr.miss_threshold(
            document={"thresholds": {"goal_replan_misses": bad}}) == 2, bad
    doc = _doc()
    assert gr.triggers(_store(_episodes("T1"), _episodes("T2")), doc, misses=3) == []
    three = _store(_episodes("T1"), _episodes("T2"), _episodes("T3"))
    assert len(gr.triggers(three, doc, misses=3)) == 1
    template = (REPO_ROOT / "maestro" / "templates" / "project.yaml.tmpl").read_text(
        encoding="utf-8")
    assert "goal_replan_misses: 2" in template


def test_a_proven_infeasibility_triggers_on_one_episode():
    """C26: one decisive impossibility finding replans at once, not after two tasks."""
    [record] = gp.read({"goal_progress": [INFEASIBLE]}, G3_CHECK).records
    assert record["contradiction"]["criterion"] == "AC-04"
    [signal] = gr.triggers(_store(_episodes("T1", [record], goal="G3")), _doc())
    assert (signal["goal"], signal["kind"], signal["episodes"]) == (
        "G3", "contradiction", ["T1:G3"])
    assert signal["facts"] == [record["contradiction"]["facts"]]
    assert "parsing alone takes 30 s" in " ".join(signal["evidence"])


def test_an_unproven_infeasibility_is_a_follow_up():
    """R42: a claim without runtime evidence is a targeted follow-up, never a replan."""
    entry = dict(INFEASIBLE, evidence=[{"kind": "diff", "text": "rewrote the parser"}])
    read = gp.read({"goal_progress": [entry]}, G3_CHECK)
    [record] = read.records
    assert record["impact"] == "unknown" and "contradiction" not in record
    [follow] = [f for f in read.follow_ups if "infeasib" in f]
    assert follow.startswith("Goal G3 infeasibility unproven: gather evidence: ")
    assert "AC-04" in follow
    assert any("infeasible" in flag for flag in read.flags)
    assert gr.triggers(_store(_episodes("T1", read.records, goal="G3")), _doc()) == []


def test_operator_only_maintenance_and_serves_empty_never_trigger():
    optional = V2.replace("- **Layer:** integration\n- **Evidence:** test\n",
                          "- **Layer:** integration\n- **Evidence:** test\n"
                          "- **Mandatory:** no\n")
    doc = _doc(optional)    # G2's only mandatory verifier is now OP-01
    assert [c.mandatory for c in doc.criteria if c.id == "AC-03"] == [False]
    assert gr.triggers(_store(_episodes("T1", text=optional), _episodes("T2", text=optional)),
                       doc) == []
    maintenance = _store(_episodes("T1", contribution="maintenance"),
                         _episodes("T2", contribution="maintenance"))
    assert gr.triggers(maintenance, _doc()) == []
    unserved = [dict(MISS, served=False)]
    assert gr.triggers(_store(_episodes("T1", unserved, goal="", contribution="maintenance"),
                              _episodes("T2", unserved, goal="", contribution="maintenance")),
                       _doc()) == []


# --- the proposal (R45) ----------------------------------------------------------------------


def test_proposal_has_root_cause_scope_alternatives_cost_impact_and_authority():
    doc = _doc()
    [signal] = gr.triggers(_store(_episodes("T1"), _episodes("T2")), doc)
    p = gr.propose(signal, doc, OPEN, task="T2", at=DAY)
    assert p["id"] == "RP-" + signal["fingerprint"][:8] and p["stage"] == "replan"
    assert (p["goal"], p["revision"], p["task"]) == ("G2", 2, "T2")
    assert p["trigger"] == signal
    cause = p["root_cause"]
    assert cause["attribution"] == "unattributed"      # R58: no planner fault
    assert cause["confidence"] in ("low", "medium", "high")
    assert "T1" in cause["hypothesis"] and "T2" in cause["hypothesis"]
    assert "AC-03" in cause["hypothesis"] and len(cause["evidence"]) == 2
    assert p["scope"] == {"criteria_red": ["AC-03"], "open_tasks": ["T3", "T6"],
                          "held_tasks": ["T3", "T5"], "independent": "continues"}
    alts = p["alternatives"]
    assert 2 <= len(alts) <= dan_questions.MAX_OPTIONS
    assert alts[-1]["label"] == "Keep going" and alts[-1]["action"] == "keep_going"
    assert alts[-1]["requires_dan"] is False
    for alt in alts:
        assert set(alt) >= {"label", "action", "changes", "cost", "impact", "requires_dan",
                            "authority_reason"}, alt["label"]
        assert set(alt["changes"]) == {"add_tasks", "deps_rewire", "contract_text"}
        assert set(alt["cost"]) == {"tasks", "attempts_spent", "paid"}
        assert set(alt["impact"]) == {"criteria", "other_goals"}
        assert set(alt["impact"]["criteria"]) == {"AC-03"}
    diagnose = alts[p["recommended"]]
    assert diagnose["action"] == "routine_plan_adjustment" and diagnose["requires_dan"] is False
    [task] = diagnose["changes"]["add_tasks"]
    assert (task["serves"], task["contribution"], task["criteria"]) == (
        ["G2"], "enabling", ["AC-03"])
    assert task["id"].endswith(p["id"][3:].lower())
    assert diagnose["changes"]["deps_rewire"] == ["T3"]
    assert diagnose["cost"] == {"tasks": 1, "attempts_spent": 2, "paid": False}
    contract = next(a for a in alts if a["changes"]["contract_text"])
    assert contract["label"] == "Revise G2's contract"
    assert contract["action"] == "goal_change" and contract["requires_dan"] is True
    assert "only Dan" in p["why"] or "routine" in p["why"]
    # one Dan question carries all of it, in `dan_questions`' shape
    q = gr.question(p)
    assert (q["id"], q["run_id"], q["task_id"]) == (
        f"dq-replan-{p['id']}", f"replan-{p['id']}", "T2")
    parsed, error = dan_questions.parse(q["spec"])
    assert error == ""
    assert [o["label"] for o in parsed[0]["options"]] == [a["label"] for a in alts]
    text = q["spec"]["question"]
    assert p["id"] in text and "G2" in text and "AC-03" in text and "T3, T5" in text
    assert "independent work continues" in text
    assert q["spec"]["replan"]["id"] == p["id"]


# --- authority, holds and Dan's answers (R46, C27) ------------------------------------------


def test_a_local_recommendation_is_applied_without_asking(tmp_path):
    repo, dan = _repo(tmp_path), _Dan()
    assert _graduate(repo, "T1", dan=dan) == ([], [])
    extra, follow_ups = _graduate(repo, "T2", dan=dan)
    [task] = extra
    assert task["contribution"] == "enabling" and follow_ups == []
    assert dan.asked == []
    [replan] = goal_store.read(repo)["replans"]
    assert replan["status"] == "applied"
    assert (replan["applied"], replan["waits"]) == ([task["id"]], ["T3"])
    assert [event for event, _ in dan.journaled] == ["goal_replan_applied"]
    [fyi] = dan.notified
    assert replan["id"] in fyi and task["id"] in fyi
    # the diagnosis runs first: G2's direct task and its dependent wait, the rest runs
    assert gr.held(repo, [*OPEN, task]) == {"T3": replan["id"], "T5": replan["id"]}
    assert gr.held(repo, OPEN) == {}        # the diagnosis graduated
    # applied facts are settled: a repair re-graduation of T2 adds nothing
    assert _graduate(repo, "T2", dan=dan) == ([], [])


def test_a_threshold_change_asks_dan_and_holds_only_dependents(tmp_path):
    repo, dan = _repo(tmp_path), _Dan()
    [record] = gp.read({"goal_progress": [INFEASIBLE]}, G3_CHECK).records
    tasks = [_task("T2", "G3"), _task("T3", "G3", "maintenance"), _task("T4", "G1"),
             _task("T5", "G1", deps=["T2"]), _task("T6", "", "maintenance"),
             _task("T7", "G3", "enabling")]
    extra, _ = _graduate(repo, "T1", [record], dan=dan, open_tasks=tasks, goal="G3")
    assert extra == []
    [q] = dan.asked
    spec = q["spec"]
    assert spec["options"][spec["recommended"]]["label"] == "Revise G3's contract"
    recommended = spec["replan"]["alternatives"][spec["recommended"]]
    assert recommended["action"] == "acceptance_threshold" and recommended["requires_dan"]
    assert "acceptance_threshold is Dan's" in spec["why_undefined"]
    [replan] = goal_store.read(repo)["replans"]
    assert (replan["status"], replan["question_id"]) == ("asked", q["id"])
    held = gr.held(repo, tasks)
    assert sorted(held) == ["T2", "T5", "T7"]     # maintenance, G1 and serves: [] run
    assert [event for event, _ in dan.journaled] == ["goal_replan_asked"]


def test_keep_going_resets_the_count_and_the_same_facts_are_not_re_asked(tmp_path):
    repo, dan = _repo(tmp_path, ASK_PLANS), _Dan()
    q = _ask_and_answer(repo, dan, "Keep going")
    assert gr.poll(repo, dan, DAY) == []
    [replan] = goal_store.read(repo)["replans"]
    assert replan["status"] == "decided"
    decision = replan["decision"]
    assert decision["resolution"] == "keep_going" and decision["facts"] == ["T1:G2", "T2:G2"]
    assert decision["rationale"]
    assert [req for req, _ in dan.recorded] == [q["id"]]
    assert gr.held(repo, OPEN) == {}
    assert gr.poll(repo, dan, DAY) == []           # nothing is decided twice
    _graduate(repo, "T2", dan=dan)                 # the same facts again
    _graduate(repo, "T3", dan=dan)                 # one new miss: the count starts over
    assert len(dan.asked) == 1
    _graduate(repo, "T4", dan=dan)
    assert len(dan.asked) == 2
    assert dan.asked[1]["spec"]["replan"]["trigger"]["facts"] == ["T3:G2", "T4:G2"]


def test_a_new_contradiction_after_keep_going_still_fires(tmp_path):
    repo, dan = _repo(tmp_path, ASK_PLANS), _Dan()
    _ask_and_answer(repo, dan, "Keep going")
    gr.poll(repo, dan, DAY)
    entry = dict(MISS, infeasible="the statement rounds each line, the ledger the total")
    [record] = gp.read({"goal_progress": [entry]}, G2_CHECK).records
    _graduate(repo, "T3", [record], dan=dan)
    assert len(dan.asked) == 2
    assert dan.asked[1]["spec"]["replan"]["trigger"]["kind"] == "contradiction"


def test_regrading_a_task_does_not_double_count(tmp_path):
    repo, dan = _repo(tmp_path), _Dan()
    for _ in range(3):      # a repair re-graduation, or a rework's second accepted review
        assert _graduate(repo, "T1", dan=dan) == ([], [])
    [episode] = goal_store.read(repo)["episodes"]
    assert (episode["id"], episode["task"], episode["goal"]) == ("T1:G2", "T1", "G2")
    assert episode["contribution"] == "direct" and episode["terms"] == gr.terms(_doc())["G2"]
    assert dan.asked == [] and goal_store.read(repo)["replans"] == []


def test_free_text_is_a_note_that_applies_nothing(tmp_path):
    """Like GDT-7's `other`: Dan's own words are recorded and reach the briefs; they never
    apply a reserved change, and they settle the facts as keep-going does."""
    repo, dan = _repo(tmp_path, ASK_PLANS), _Dan()
    _ask_and_answer(repo, dan, "try the streaming parser first")
    assert gr.poll(repo, dan, DAY) == []
    [replan] = goal_store.read(repo)["replans"]
    assert replan["decision"]["resolution"] == "notes"
    assert replan["decision"]["rationale"] == "try the streaming parser first"
    assert replan["applied"] == [] and gr.held(repo, OPEN) == {}
    assert gr.triggers(goal_store.read(repo), _doc(ASK_PLANS)) == []


def test_approving_a_task_change_returns_it_and_a_contract_change_holds_until_dan_revises(
        tmp_path):
    repo, dan = _repo(tmp_path, ASK_PLANS), _Dan()
    _ask_and_answer(repo, dan, "Add a diagnosis task")
    [task] = gr.poll(repo, dan, DAY)
    [replan] = goal_store.read(repo)["replans"]
    assert replan["decision"]["resolution"] == "approve" and replan["applied"] == [task["id"]]
    assert sorted(gr.held(repo, [*OPEN, task])) == ["T3", "T5"]
    # a contract revision is Dan's to write: G3's work waits until his revision lands
    [record] = gp.read({"goal_progress": [INFEASIBLE]}, G3_CHECK).records
    g3 = [_task("T8", "G3")]
    _graduate(repo, "T7", [record], dan=dan, open_tasks=g3, goal="G3")
    question = dan.asked[-1]
    dan.answers[question["id"]] = "Revise G3's contract"
    assert gr.poll(repo, dan, DAY) == []
    assert list(gr.held(repo, g3)) == ["T8"]
    goals_md = repo / "docs" / "GOALS.md"
    goals_md.write_text(goals_md.read_text(encoding="utf-8").replace(
        "- **Threshold:** 10 seconds", "- **Threshold:** 32 seconds"), encoding="utf-8")
    assert gr.held(repo, g3) == {}


# --- the graph loop's wiring -----------------------------------------------------------------


def test_a_replan_wait_survives_pruning():
    """A replan question belongs to no live run: pruning dead runs' waits keeps it."""
    from maestro.hitl import verify

    key = verify.QUESTIONS_STATE_KEY
    state = {key: {"T2": {"run_id": "replan-RP-1", "request_ids": ["dq-replan-RP-1"]},
                   "T9": {"run_id": "run-9", "request_ids": ["dq-9"]}}}
    verify.prune_awaiting([], read_state=lambda: dict(state), write_state=state.update)
    assert list(state[key]) == ["T2"]


def test_graduation_hands_replan_tasks_and_checkpoint_follow_ups_to_the_commit(monkeypatch):
    from maestro import orchestrator
    from maestro.docs import followups

    added = _task("g2-diagnose-1234abcd", contribution="enabling")
    seen: dict = {}
    channel = object()
    monkeypatch.setattr(orchestrator, "get_task_by_id", lambda tid: _task(tid))
    monkeypatch.setattr(orchestrator, "_graph_accepted_run",
                        lambda store, tid: {"run_id": "r2", "workflow_id": "wf_T2"})
    monkeypatch.setattr(followups, "collect", lambda store, artifacts, wf: [])
    monkeypatch.setattr(orchestrator, "_load_roadmap_tasks", lambda: [_task("T2"), *OPEN])
    monkeypatch.setattr(gr, "collect_progress", lambda store, artifacts, run_id: [MISS])

    def on_graduation(repo, task, records, open_tasks, dan, **kwargs):
        seen.update(task=task["id"], records=records, open=[t["id"] for t in open_tasks],
                    dan=dan, **kwargs)
        return [added], ["Goal G1 checkpoint (convergence after T2): AC-01 is unknown"]

    monkeypatch.setattr(gr, "on_graduation", on_graduation)

    def mark(task_id, follow_ups=(), replan_tasks=()):
        seen.update(follow_ups=list(follow_ups), replan_tasks=list(replan_tasks))

    monkeypatch.setattr(orchestrator, "mark_roadmap_complete", mark)
    for name in ("_graph_push_target", "phase_report", "_process_intake", "append_journal"):
        monkeypatch.setattr(orchestrator, name, lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "read_state", lambda: {})
    monkeypatch.setattr(orchestrator, "_graph_acceptance_smoke",
                        lambda store, run_id: {"pass": True, "metrics": {}, "reason": "ok"})
    monkeypatch.setattr(orchestrator, "_graph_touches_bot_files", lambda *a: False)
    monkeypatch.setattr(orchestrator, "_graph_goals_contract", lambda: True)

    orchestrator._graph_graduate(object(), "T2", object(), dan_channel=channel)

    assert (seen["task"], seen["records"], seen["dan"]) == ("T2", [MISS], channel)
    assert seen["open"] == ["T3", "T4", "T5", "T6"]
    assert (seen["run_id"], seen["workflow_id"]) == ("r2", "wf_T2")
    assert seen["replan_tasks"] == [added]
    [follow] = seen["follow_ups"]
    assert follow.node == "goal checkpoint" and follow.text.startswith("Goal G1 checkpoint")
    assert follow.line() == "(goal checkpoint) " + follow.text


def test_graduation_applies_triage_and_hands_polish_tasks_to_the_commit(monkeypatch, tmp_path):
    """P13 GDT-11 / W7: a triage task's graduation applies its decisions and its polish
    tasks join the graduation commit (`mark_roadmap_complete(polish_tasks=...)`); a polish
    task's graduation settles its items from its report."""
    import json

    from maestro import backlog, orchestrator
    from maestro.docs import followups

    at = "2026-10-24T00:00:00+00:00"
    backlog.add(tmp_path, {"id": "T1", "scope": ["src/a.py"]},
                [followups.FollowUp("a", 1, "proof_review")], at=at)
    backlog.add(tmp_path, {"id": "T2", "scope": ["src/a.py"]},
                [followups.FollowUp("a again", 1, "proof_review")], at=at)
    [triage] = backlog.stage(tmp_path, [], {"T1", "T2"}, at=at)
    (tmp_path / "docs" / "polish").mkdir(parents=True)
    (tmp_path / "docs" / "polish" / "triage-1.json").write_text(json.dumps({"items": [
        {"id": "BL-0001", "decision": "keep"},
        {"id": "BL-0002", "decision": "duplicate", "of": "BL-0001", "reason": "same"}]}))
    task = {"triage": triage}
    seen: list = []
    sent: list = []
    journal: list = []
    reports: list = []
    monkeypatch.setattr(orchestrator, "REPO", tmp_path)
    monkeypatch.setattr(orchestrator, "get_task_by_id", lambda tid: task["triage"])
    monkeypatch.setattr(orchestrator, "_graph_accepted_run",
                        lambda store, tid: {"run_id": "r1", "workflow_id": f"wf_{tid}"})
    monkeypatch.setattr(followups, "collect", lambda store, artifacts, wf: [])
    monkeypatch.setattr(orchestrator, "_graph_goals_contract", lambda: False)

    def mark(task_id, follow_ups=(), replan_tasks=(), polish_tasks=()):
        seen.append((task_id, list(replan_tasks), list(polish_tasks)))

    monkeypatch.setattr(orchestrator, "mark_roadmap_complete", mark)
    for name in ("_graph_push_target", "_process_intake"):
        monkeypatch.setattr(orchestrator, name, lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "phase_report",
                        lambda tid, task_def, smoke, follow_ups="": reports.append(follow_ups))
    monkeypatch.setattr(orchestrator, "notify_telegram", lambda text, *a, **k: sent.append(text))
    monkeypatch.setattr(orchestrator, "append_journal",
                        lambda event, detail="", session_id="": journal.append((event, detail)))
    monkeypatch.setattr(orchestrator, "read_state", lambda: {})
    monkeypatch.setattr(orchestrator, "_graph_acceptance_smoke",
                        lambda store, run_id: {"pass": True, "metrics": {}, "reason": "ok"})
    monkeypatch.setattr(orchestrator, "_graph_touches_bot_files", lambda *a: False)

    orchestrator._graph_graduate(object(), "polish-triage-1", object())

    [(tid, replan, [polish])] = seen
    assert (tid, replan, polish["id"]) == ("polish-triage-1", [], "polish-1-a")
    assert polish["polish_items"] == ["BL-0001"]
    assert sent == ["🧹 Triage (polish stage 1): 2 follow-ups → 1 merged as duplicates, "
                    "1 polish task: polish-1-a (src/a.py)."]
    assert [e for e, _ in journal if e.startswith("polish_")] == ["polish_triage_applied"]

    # The polish task graduates: its report settles its items, and the accept report says so.
    (tmp_path / "docs" / "polish" / "polish-1-a.json").write_text(json.dumps({"items": [
        {"id": "BL-0001", "outcome": "done", "reason": "fixed"}]}))
    task["triage"] = polish
    seen.clear()
    orchestrator._graph_graduate(object(), "polish-1-a", object())

    assert seen == [("polish-1-a", [], [])]          # a polish task adds no task
    assert [i["state"] for i in backlog.items(tmp_path)] == ["done", "merged"]
    assert ("polish_items_settled", "polish-1-a: 1 done, 0 declined, 0 deferred") in journal
    assert reports[-1] == "polish-1-a: 1 done, 0 declined, 0 deferred"


def test_replan_tasks_land_in_the_roadmap_and_are_committed(monkeypatch, tmp_path):
    import json
    import subprocess

    from maestro.docs import roadmap

    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    path = repo / "docs" / "ROADMAP.md"
    path.write_text("# Roadmap\n\n```yaml\n" + json.dumps(_task("T2")) + "\n```\n",
                    encoding="utf-8")
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.invalid"],
                 ["config", "user.name", "t"], ["add", "-A"], ["commit", "-q", "-m", "seed"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    monkeypatch.setattr(roadmap, "REPO", repo)
    monkeypatch.setattr(roadmap, "ROADMAP_FILE", path)
    monkeypatch.setattr(roadmap, "append_journal", lambda *a, **k: None)
    diag = _task("g2-diagnose-1234abcd", contribution="enabling", replan_of="RP-1234abcd")
    assert roadmap.add_replan_tasks([diag], "RP-1234abcd") == ["g2-diagnose-1234abcd"]
    assert [t["id"] for t in roadmap._load_roadmap_tasks()] == ["T2", "g2-diagnose-1234abcd"]
    log = subprocess.run(["git", "log", "--format=%s", "-1"], cwd=repo, capture_output=True,
                         text=True).stdout
    assert log.startswith("roadmap: replan RP-1234abcd adds g2-diagnose-1234abcd")
    assert roadmap.add_replan_tasks([diag], "RP-1234abcd") == []     # never twice
    # at graduation the blocks join the graduation commit instead
    monkeypatch.setattr(roadmap, "_call_main", lambda *a, **k: None)
    monkeypatch.setattr(roadmap.doc_depmap, "render_png", lambda: None)
    monkeypatch.setattr(roadmap, "_refresh_upcoming", lambda: None)
    second = _task("ac-03-totals-match-1234abcd", replan_of="RP-1234abcd")
    roadmap.mark_roadmap_complete("T2", replan_tasks=[second])
    assert [t["id"] for t in roadmap._load_roadmap_tasks()][-1] == second["id"]
    log = subprocess.run(["git", "log", "--format=%s", "-1"], cwd=repo, capture_output=True,
                         text=True).stdout
    assert log.startswith("roadmap(T2): graduate")


# --- GDT-8 review ----------------------------------------------------------------------------


def test_a_repair_re_graduation_reuses_the_task_s_checkpoint(tmp_path):
    """Review H1: a re-graduation of the same accepted run never reruns its checkpoint (a
    new `at`, a new CP id, the goal tests again); its follow-ups come back from the record."""
    from .test_goal_milestones import BOUND, _runner

    repo, dan = _repo(tmp_path, BOUND), _Dan()
    run, calls = _runner({"test_valid": "blocked"})

    def graduate(at, runner):
        return gr.on_graduation(repo, _task("T1", "G1"), [], [], dan, run_id="run-T1",
                                workflow_id="wf_T1", at=at, notify=dan.notify, misses=2,
                                runner=runner)

    first = graduate(DAY, run)
    [point] = goal_store.read(repo)["checkpoints"]
    assert len(calls) == 2 and first[1] and "AC-01" in first[1][0]
    assert graduate("2026-10-18T10:00:00+00:00", _never_run) == first
    assert goal_store.read(repo)["checkpoints"] == [point]


def test_an_applied_replan_s_tasks_lost_before_the_commit_come_back(tmp_path):
    """Review M1: the replan is stored `applied` before its tasks reach the ROADMAP; a loop
    lost in between must not lose them. Re-graduation and each poll return them while they
    are neither in the ROADMAP nor completed; once landed, Dan's own deletion stands."""
    repo, dan = _repo(tmp_path), _Dan()
    _graduate(repo, "T1", dan=dan)
    [task], _ = _graduate(repo, "T2", dan=dan)
    roadmap = {"T1", "T2", "T3", "T4", "T5", "T6"}
    assert gr.on_graduation(repo, _task("T2"), [MISS], OPEN, dan, run_id="run-T2",
                            workflow_id="wf_T2", at=DAY, notify=dan.notify, misses=2,
                            runner=_never_run, completed={"T1"}) == ([task], [])
    assert gr.poll(repo, dan, DAY, present=roadmap) == [task]
    assert gr.poll(repo, dan, DAY, present=roadmap | {task["id"]}) == []
    assert gr.poll(repo, dan, DAY, present=roadmap) == []      # landed: never re-added


def test_an_approved_task_change_lost_before_the_commit_comes_back(tmp_path):
    repo, dan = _repo(tmp_path, ASK_PLANS), _Dan()
    _ask_and_answer(repo, dan, "Add a diagnosis task")
    present = {"T1", "T2", "T3", "T4", "T5", "T6"}
    [task] = gr.poll(repo, dan, DAY, present=present)
    assert gr.poll(repo, dan, DAY, present=present) == [task]     # the commit never landed
    assert len(dan.recorded) == 1                                 # decided once


def test_keep_going_on_a_contradiction_settles_the_misses_it_saw(tmp_path):
    """Review M2: T1 misses, T2 misses with proof: the contradiction asks and Dan keeps
    going. The next graduation of any task must not count T1 and T2 as two new misses."""
    repo, dan = _repo(tmp_path, ASK_PLANS), _Dan()
    entry = dict(MISS, infeasible="the statement rounds each line, the ledger the total")
    [proof] = gp.read({"goal_progress": [entry]}, G2_CHECK).records
    _graduate(repo, "T1", dan=dan)
    _graduate(repo, "T2", [proof], dan=dan)
    [q] = dan.asked
    assert q["spec"]["replan"]["trigger"]["kind"] == "contradiction"
    dan.answers[q["id"]] = "Keep going"
    gr.poll(repo, dan, DAY)
    _graduate(repo, "T3", [], dan=dan)          # no new evidence
    assert len(dan.asked) == 1
    assert gr.triggers(goal_store.read(repo), _doc(ASK_PLANS)) == []
    _graduate(repo, "T4", dan=dan)              # one new miss: the count starts over
    assert len(dan.asked) == 1
    _graduate(repo, "T5", dan=dan)
    assert len(dan.asked) == 2
    assert dan.asked[1]["spec"]["replan"]["trigger"]["facts"] == ["T4:G2", "T5:G2"]


def test_an_approved_contract_change_lifts_on_a_new_revision_and_says_what_waits(tmp_path):
    """Review M3: the hold waits for Dan's revision; a new revision lifts it even when the
    terms read the same, and what waits names his revision."""
    repo, dan = _repo(tmp_path), _Dan()
    [record] = gp.read({"goal_progress": [INFEASIBLE]}, G3_CHECK).records
    g3 = [_task("T8", "G3")]
    _graduate(repo, "T7", [record], dan=dan, open_tasks=g3, goal="G3")
    dan.answers[dan.asked[-1]["id"]] = "Revise G3's contract"
    gr.poll(repo, dan, DAY)
    holds = gr.held(repo, g3)
    assert list(holds) == ["T8"]
    text = gr.describe_holds(repo, holds)
    assert holds["T8"] in text and "G3" in text and "T8" in text
    assert "revision" in text and "docs/GOALS.md" in text
    goals_md = repo / "docs" / "GOALS.md"
    goals_md.write_text(goals_md.read_text(encoding="utf-8").replace(
        "Contract revision: 2", "Contract revision: 3"), encoding="utf-8")
    assert gr.held(repo, g3) == {}


def test_one_malformed_replan_row_does_not_stop_the_others(tmp_path):
    """Review L1: a row poll cannot read is journaled; every other row is still decided."""
    repo, dan = _repo(tmp_path, ASK_PLANS), _Dan()
    _ask_and_answer(repo, dan, "Add a diagnosis task")
    goal_store.update(repo, "replans", lambda rows: [
        {"id": "RP-bad", "goal": "G2", "status": "asked"}, *rows])
    [task] = gr.poll(repo, dan, DAY)
    assert task["contribution"] == "enabling"
    assert [d for e, d in dan.journaled if e == "goal_replan_poll_failed" and "RP-bad" in d]


def _attempts_store(outcomes: list[dict]):
    import json
    import sqlite3
    from types import SimpleNamespace

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE attempts (run_id TEXT, outcome_ref TEXT)")
    blobs = {}
    for n, outcome in enumerate(outcomes):
        blobs[f"ref-{n}"] = json.dumps(outcome).encode("utf-8")
        conn.execute("INSERT INTO attempts VALUES (?, ?)", ("r1", f"ref-{n}"))
    return SimpleNamespace(conn=conn), SimpleNamespace(read=blobs.__getitem__)


def test_progress_is_the_last_approved_review_that_judged_the_goals():
    """Review L5: with several review lenses, a later approved lens without goal_progress
    does not drop the goal reviewer's records."""
    judged = {"outputs": {"review": {"verdict": "approved", "goal_progress": [MISS]}}}
    other = {"outputs": {"review": {"verdict": "approved"}}}
    store, artifacts = _attempts_store([judged, other])
    assert gr.collect_progress(store, artifacts, "r1") == [MISS]
    reworked = {"outputs": {"review": {"verdict": "changes_requested",
                                       "goal_progress": [dict(MISS, impact="regressed")]}}}
    store, artifacts = _attempts_store([judged, other, reworked])
    assert gr.collect_progress(store, artifacts, "r1") == [MISS]


def test_collect_observed_reads_the_last_accepted_gate_only():
    """GDT-9 (R35): the observations a graduation records are the accepted run's last
    succeeded gate: a rejected gate, one that asked Dan, or a review say nothing."""
    from maestro import goal_state

    gate = {"status": "succeeded", "outputs": {
        "decision": "accepted", "snapshot": "old", "evidence": [],
        "acceptance": {"AC-01": acceptance.GREEN, "AC-02": acceptance.RED}}}
    rejected = {"status": "failed", "outputs": {
        "decision": "rejected", "snapshot": "bad", "acceptance": {"AC-01": acceptance.RED}}}
    asked = {"status": "failed", "outputs": {
        "decision": "rejected", "acceptance": {"AC-04": acceptance.RED},
        "dan_questions": [{"question": "?"}]}}
    accepted = {"status": "succeeded", "outputs": {
        "decision": "accepted", "snapshot": "abc1234", "evidence": [],
        "acceptance": {"AC-01": acceptance.GREEN, "AC-02": acceptance.GREEN}}}
    review = {"status": "succeeded", "outputs": {"review": {"verdict": "approved"}}}
    store, artifacts = _attempts_store([gate, rejected, accepted, asked, review])
    assert goal_state.collect_observed(store, artifacts, "r1") == {
        "acceptance": {"AC-01": acceptance.GREEN, "AC-02": acceptance.GREEN},
        "snapshot": "abc1234"}
    store, artifacts = _attempts_store([rejected, asked, review])
    assert goal_state.collect_observed(store, artifacts, "r1") == {}
    assert goal_state.collect_observed(store, None, "r1") == {}
    assert goal_state.collect_observed(object(), artifacts, "r1") == {}   # unreadable


def test_graduation_records_the_gate_and_checkpoint_observations(tmp_path):
    """GDT-9 (R35): at graduation the gate's observations of the accepted run and the
    checkpoint's reruns are stored in the goals store's `criteria`, beside the episodes, and
    the contract revision seen is kept; the replan reads the overlaid results."""
    from maestro import goal_state

    from .test_goal_milestones import _repo as _bound_repo
    from .test_goal_milestones import _runner

    repo, doc = _bound_repo(tmp_path, {"AC-01": acceptance.RED, "AC-02": acceptance.RED,
                                       "AC-04": acceptance.RED})
    run, calls = _runner({"bench/year.py": "8"})
    dan = _Dan()
    gr.on_graduation(repo, _task("T1", "G3"), [], [], dan, run_id="run-T1",
                     workflow_id="wf_T1", at="2099-01-01T00:00:00+00:00",
                     notify=dan.notify, misses=2, runner=run,
                     observed={"acceptance": {"AC-01": acceptance.GREEN},
                               "snapshot": "abc1234"})
    store = goal_store.read(repo)
    rows = {r["id"]: r for r in store["criteria"]}
    assert (rows["AC-01"]["source"], rows["AC-01"]["state"], rows["AC-01"]["task"],
            rows["AC-01"]["commit"]) == ("gate", acceptance.GREEN, "T1", "abc1234")
    assert (rows["AC-04"]["source"], rows["AC-04"]["state"]) == ("checkpoint",
                                                                 acceptance.GREEN)
    assert [r["revision"] for r in store["contract"]] == [2]
    assert [c["id"] for c in store["checkpoints"]] and len(calls) == 1
    states = {r.id: r.state for r in goal_state.results(repo, doc)}
    assert (states["AC-01"], states["AC-02"], states["AC-04"]) == (
        acceptance.GREEN, acceptance.RED, acceptance.GREEN)


def test_a_failing_progress_write_is_journaled_and_keeps_the_replan_tasks(tmp_path,
                                                                           monkeypatch):
    """GDT-9 review L2 (deviation 5): a goal-state write that raises is journaled
    `goal_state_failed`; the graduation still records its episodes and hands back the
    replan's tasks."""
    from maestro import goal_state

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(goal_state, "record_gate", boom)
    monkeypatch.setattr(goal_state, "observe_contract", boom)
    repo, dan = _repo(tmp_path), _Dan()
    assert _graduate(repo, "T1", dan=dan) == ([], [])
    [task] = _graduate(repo, "T2", dan=dan)[0]
    assert task["contribution"] == "enabling"
    failed = [d for e, d in dan.journaled if e == "goal_state_failed"]
    assert len(failed) == 4 and all(d.endswith("OSError: disk full") for d in failed)
    assert "goal_replan_applied" in [e for e, _ in dan.journaled]
    assert [e["id"] for e in goal_store.read(repo)["episodes"]] == ["T1:G2", "T2:G2"]


def _real_channel(tmp_path, monkeypatch):
    import json

    from maestro.hitl import telegram, verify

    monkeypatch.setattr(telegram, "QUESTIONS_DIR", tmp_path / "questions")
    monkeypatch.setattr(telegram, "append_journal", lambda *a, **k: None)
    monkeypatch.setattr(telegram, "notify_telegram", lambda *a, **k: None)
    monkeypatch.setattr(telegram, "_tg_api", lambda *a, **k: None)
    state: dict = {}
    writes: list[int] = []
    journal: list = []

    def write(new):
        writes.append(1)
        state.clear()
        state.update(new)

    chan = verify.DanVerificationChannel(
        send=lambda text, markup=None: 1000, configured=lambda: True,
        journal=lambda event, detail: journal.append((event, detail)),
        read_state=lambda: json.loads(json.dumps(state)), write_state=write)
    chan.state, chan.writes, chan.journaled = state, writes, journal
    chan.notify = lambda text: True
    return chan


def test_a_question_that_holds_nothing_marks_no_wait(tmp_path, monkeypatch):
    """GDT-12 S4 (§8): an improvement question (`holds: False`) is saved, sent and read
    like any question, but marks no wait, so it never keeps the queue from being exhausted
    and holds no task; an ordinary question still waits."""
    import json

    from maestro import improvement as imp
    from maestro.hitl import telegram, verify

    chan = _real_channel(tmp_path, monkeypatch)
    row = {"id": "IP-0001", "key": "k", "project": "itv", "lever": "role_prompt",
           "target": "reviewer", "change": "name earlier misses", "config": None,
           "evidence": ["attempt:a1"], "risk": "longer reviews", "cost": "one prompt edit",
           "rollback": "revert the prompt", "why": "the reviewer misses them",
           "alternatives": [], "revises": None, "new_evidence": None,
           "expected_saving": {"cost_units": None, "pool": None, "basis": "unmeasured"},
           "measure": {"metrics": ["review_rounds_per_task"], "min_tasks": 5},
           "pattern": {"id": "PAT-1", "kind": "extra_step",
                       "key": ["reviewer", "missed_earlier", "reviewer"], "rows": 6,
                       "tasks": 3, "evidence": ["attempt:a1"], "spend": {}}}
    spec = imp.question(row)
    assert spec["holds"] is False
    assert chan.poll_question(spec) is None
    assert not chan.state.get(verify.QUESTIONS_STATE_KEY)
    record = json.loads((telegram.QUESTIONS_DIR / "dq-improve-IP-0001.json").read_text())
    assert record["holds"] is False and record["sent"] is True
    (telegram.QUESTIONS_DIR / "dq-improve-IP-0001.answer").write_text("Approve\n")
    assert chan.poll_question(spec) == "Approve"
    chan.mark_question_recorded("dq-improve-IP-0001", improvement_decision={"r": 1})
    assert not chan.state.get(verify.QUESTIONS_STATE_KEY)
    # A question without `holds: False` still waits under its task.
    other = {k: v for k, v in spec.items() if k != "holds"}
    other["id"] = "dq-other"
    chan.poll_question(other)
    assert chan.state[verify.QUESTIONS_STATE_KEY]["maestro"]["request_ids"] == ["dq-other"]


def test_a_superseded_improvement_question_is_closed_and_its_tap_refused(tmp_path, monkeypatch):
    """GDT-12 review M4a: `supersede_question` closes a still-pending question (an outranked
    improvement proposal); it is no longer pending, so it never takes a plain-text answer,
    and a tap on its old message is refused. An answered question is left as it is."""
    import json

    from maestro.hitl import telegram

    chan = _real_channel(tmp_path, monkeypatch)
    spec = {"id": "dq-improve-IP-0001", "task_id": "maestro", "run_id": "improve-IP-0001",
            "title": "Improvement IP-0001", "holds": False,
            "spec": {"question": "Q?", "why_undefined": "W",
                     "options": [{"label": "Approve", "consequence": "A"},
                                 {"label": "Decline", "consequence": "D"}],
                     "recommended": 0}}
    assert chan.poll_question(spec) is None
    assert chan.supersede_question("dq-improve-IP-0001") is True
    path = telegram.QUESTIONS_DIR / "dq-improve-IP-0001.json"
    assert json.loads(path.read_text())["status"] == "superseded"
    assert telegram._pending_danreqs() == []
    assert telegram._danreq_tap("danreq:dq-improve-IP-0001:0") == ("", "", "No longer open")
    assert not (telegram.QUESTIONS_DIR / "dq-improve-IP-0001.answer").exists()
    assert chan.supersede_question("dq-improve-IP-0001") is False      # already closed
    assert chan.supersede_question("dq-missing") is False
    # A tap the update listener acknowledged writes only the answer file; the record stays
    # pending until the controller records it. It is answered: never superseded, kept.
    tapped = {**spec, "id": "dq-improve-IP-0003"}
    chan.poll_question(tapped)
    assert telegram.ack_danreq_callback(
        {"id": "cq1", "data": "danreq:dq-improve-IP-0003:1"}) is True
    assert json.loads((telegram.QUESTIONS_DIR / "dq-improve-IP-0003.json").read_text())[
        "status"] == "pending"
    assert chan.supersede_question("dq-improve-IP-0003") is False
    assert json.loads((telegram.QUESTIONS_DIR / "dq-improve-IP-0003.json").read_text())[
        "status"] == "pending"
    assert chan.poll_question(tapped) == "Decline"
    other = {**spec, "id": "dq-improve-IP-0002"}
    chan.poll_question(other)
    (telegram.QUESTIONS_DIR / "dq-improve-IP-0002.answer").write_text("Approve\n")
    chan.mark_question_recorded("dq-improve-IP-0002", improvement_decision={"r": 1})
    assert chan.supersede_question("dq-improve-IP-0002") is False
    assert json.loads((telegram.QUESTIONS_DIR / "dq-improve-IP-0002.json").read_text())[
        "status"] == "answered"


def test_two_replans_asked_at_one_graduation_wait_apart(tmp_path, monkeypatch):
    """Review L2: two questions from one graduation each keep their own wait; polling them
    does not rewrite state.json every time."""
    from maestro.hitl import verify

    chan = _real_channel(tmp_path, monkeypatch)
    [record] = gp.read({"goal_progress": [INFEASIBLE]}, G3_CHECK).records
    store = _store(_episodes("T1"), _episodes("T2"), _episodes("T2", [record], goal="G3"))
    proposals = [gr.propose(s, _doc(), OPEN, task="T2", at=DAY)
                 for s in gr.triggers(store, _doc())]
    assert len(proposals) == 2
    for p in proposals:
        chan.poll_question(gr.question(p))
    before = len(chan.writes)
    for _ in range(2):
        for p in proposals:
            chan.poll_question(gr.question(p))
    assert len(chan.writes) == before
    waits = chan.state[verify.QUESTIONS_STATE_KEY]
    assert sorted(waits) == sorted(f"replan-{p['id']}" for p in proposals)
    for p in proposals:
        assert waits[f"replan-{p['id']}"]["request_ids"] == [f"dq-replan-{p['id']}"]
    chan.mark_question_recorded(f"dq-replan-{proposals[0]['id']}")
    assert list(chan.state[verify.QUESTIONS_STATE_KEY]) == [f"replan-{proposals[1]['id']}"]


def test_a_lost_store_is_rebuilt_from_the_questions_and_stale_waits_are_pruned(
        tmp_path, monkeypatch):
    """Review L3: a corrupt store loses the asked replan; its question record brings it
    back (store ∪ question records, as GDT-7's decisions), Dan's answer still decides it,
    and a replan wait nothing asks any more is pruned, so the queue can be exhausted."""
    from maestro.hitl import telegram, verify

    chan = _real_channel(tmp_path, monkeypatch)
    repo = _repo(tmp_path, ASK_PLANS)
    for tid in ("T1", "T2"):
        gr.on_graduation(repo, _task(tid), [MISS], OPEN, chan, run_id=f"run-{tid}",
                         workflow_id=f"wf_{tid}", at=DAY, notify=chan.notify, misses=2,
                         runner=_never_run)
    [asked] = goal_store.read(repo)["replans"]
    goal_store.path_for(repo).write_text("{not json", encoding="utf-8")
    assert goal_store.read(repo)["replans"] == []
    assert gr.poll(repo, chan, DAY) == []
    [restored] = goal_store.read(repo)["replans"]
    assert (restored["id"], restored["status"]) == (asked["id"], "asked")
    assert gr.asked_ids(repo) == {asked["id"]}
    (telegram.QUESTIONS_DIR / f"{asked['question_id']}.answer").write_text(
        "Keep going", encoding="utf-8")
    gr.poll(repo, chan, DAY)
    [decided] = goal_store.read(repo)["replans"]
    assert decided["status"] == "decided" and gr.asked_ids(repo) == set()
    key = verify.QUESTIONS_STATE_KEY
    chan.state.setdefault(key, {})["replan-RP-gone"] = {
        "run_id": "replan-RP-gone", "request_ids": ["dq-replan-RP-gone"]}
    chan.state[key]["T9"] = {"run_id": "run-9", "request_ids": ["dq-9"]}
    verify.prune_replan_waits(gr.asked_ids(repo), read_state=lambda: dict(chan.state),
                              write_state=lambda new: chan.state.update(new))
    assert list(chan.state[key]) == ["T9"]


def test_an_accepted_task_held_by_a_replan_still_graduates(monkeypatch):
    """Review L6: the repair-graduation pass also covers replan-held tasks; without
    `docs/GOALS.md` it is the runnable list, as before."""
    from maestro import orchestrator

    monkeypatch.setattr(orchestrator, "_load_roadmap_tasks",
                        lambda: [_task("T3"), _task("T4", "G1")])
    monkeypatch.setattr(gr, "held", lambda repo, tasks, parked=(): {"T3": "RP-1"})
    monkeypatch.setattr(orchestrator, "_graph_goals_contract", lambda: True)
    monkeypatch.setattr(orchestrator, "read_state", lambda: {})
    runnable = [_task("T4", "G1")]
    assert [t["id"] for t in orchestrator._graph_repair_candidates(runnable)] == ["T4", "T3"]
    monkeypatch.setattr(orchestrator, "_graph_goals_contract", lambda: False)
    assert orchestrator._graph_repair_candidates(runnable) == runnable


def test_replan_held_work_is_not_an_exhausted_queue_and_is_reported_once(monkeypatch):
    """Review M3: nothing runnable only because replans hold work is a wait on Dan, not an
    exhausted queue: journaled and told once per change."""
    from maestro import orchestrator

    journal: list = []
    told: list = []
    monkeypatch.setattr(orchestrator, "_REPLAN_WAIT_SEEN", frozenset(), raising=False)
    monkeypatch.setattr(orchestrator, "append_journal",
                        lambda event, detail="", **k: journal.append((event, detail)))
    monkeypatch.setattr(orchestrator, "notify_telegram", lambda text, *a, **k: told.append(text))
    monkeypatch.setattr(gr, "describe_holds",
                        lambda repo, holds: "RP-1 (G3): waits on Dan's revision; holds T8")
    assert orchestrator._graph_replan_waiting({}) is False
    assert orchestrator._graph_replan_waiting({"T8": "RP-1"}) is True
    assert orchestrator._graph_replan_waiting({"T8": "RP-1"}) is True
    assert journal == [("goal_replan_waiting", "RP-1 (G3): waits on Dan's revision; holds T8")]
    assert len(told) == 1 and "RP-1" in told[0]
    assert orchestrator._graph_replan_waiting({}) is False
    assert orchestrator._graph_replan_waiting({"T8": "RP-1"}) is True
    assert len(journal) == 2


def _roadmap_repo(monkeypatch, tmp_path, journal):
    import json
    import subprocess

    from maestro.docs import roadmap

    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    path = repo / "docs" / "ROADMAP.md"
    path.write_text("# Roadmap\n\n```yaml\n" + json.dumps(_task("T2")) + "\n```\n",
                    encoding="utf-8")
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.invalid"],
                 ["config", "user.name", "t"], ["add", "-A"], ["commit", "-q", "-m", "seed"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    monkeypatch.setattr(roadmap, "REPO", repo)
    monkeypatch.setattr(roadmap, "ROADMAP_FILE", path)
    monkeypatch.setattr(roadmap, "append_journal", lambda e, d="", **k: journal.append((e, d)))
    return repo, path


def _head(repo) -> str:
    import subprocess

    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                          text=True).stdout.strip()


def test_replan_tasks_never_sweep_dan_s_uncommitted_roadmap_edit(monkeypatch, tmp_path):
    """Review L7: an uncommitted ROADMAP is Dan's edit in progress: the tasks join the
    working tree (dispatch reads it) and are committed later, never with his edit."""
    from maestro.docs import roadmap

    journal: list = []
    repo, path = _roadmap_repo(monkeypatch, tmp_path, journal)
    path.write_text(path.read_text(encoding="utf-8") + "\n<!-- Dan, mid-edit -->\n",
                    encoding="utf-8")
    head = _head(repo)
    diag = _task("g2-diagnose-1234abcd", contribution="enabling", replan_of="RP-1234abcd")
    assert roadmap.add_replan_tasks([diag], "RP-1234abcd") == [diag["id"]]
    assert _head(repo) == head
    text = path.read_text(encoding="utf-8")
    assert "Dan, mid-edit" in text and diag["id"] in text
    assert [e for e, _ in journal] == ["goal_replan_tasks_uncommitted"]


def test_a_failed_replan_commit_is_journaled(monkeypatch, tmp_path):
    import subprocess
    from types import SimpleNamespace

    from maestro.docs import roadmap

    journal: list = []
    repo, _ = _roadmap_repo(monkeypatch, tmp_path, journal)
    real = subprocess.run

    def run(cmd, **kwargs):
        if cmd[:2] == ["git", "commit"]:
            return subprocess.CompletedProcess(cmd, 1, "", "fatal: boom")
        return real(cmd, **kwargs)

    monkeypatch.setattr(roadmap, "subprocess", SimpleNamespace(run=run))
    diag = _task("g2-diagnose-1234abcd", contribution="enabling", replan_of="RP-1234abcd")
    assert roadmap.add_replan_tasks([diag], "RP-1234abcd") == [diag["id"]]
    [(event, detail)] = journal
    assert event == "goal_replan_commit_failed" and "boom" in detail and "RP-1234abcd" in detail


# --- GDT-8 verification lows ------------------------------------------------------------------


def test_a_parked_added_task_releases_the_hold_like_any_parked_task(tmp_path):
    """Verification L1: the replan's added task is parked (it failed): the tasks waiting
    for it stay held (blocked, as the dependents of any parked task are) but the queue is
    not 'waiting on a replan' forever. It is reported."""
    repo, dan = _repo(tmp_path), _Dan()
    _graduate(repo, "T1", dan=dan)
    [task] = _graduate(repo, "T2", dan=dan)[0]
    tasks = [*OPEN, task]
    assert sorted(gr.waiting_holds(repo, tasks, {"T4"})) == ["T3", "T5"]   # unrelated parked
    assert sorted(gr.held(repo, tasks)) == ["T3", "T5"]       # still held for dispatch
    assert gr.waiting_holds(repo, tasks, {task["id"]}) == {}
    assert gr.parked_added(repo, tasks, {task["id"]}) == {task["id"]: task["replan_of"]}
    assert gr.parked_added(repo, tasks, set()) == {}
    assert gr.parked_added(repo, OPEN, {task["id"]}) == {}      # not on the ROADMAP now


def test_a_parked_replan_task_is_told_to_dan_once(monkeypatch):
    from maestro import orchestrator

    told: list = []
    journal: list = []
    monkeypatch.setattr(orchestrator, "_REPLAN_PARKED_SEEN", set(), raising=False)
    monkeypatch.setattr(orchestrator, "_graph_goals_contract", lambda: True)
    monkeypatch.setattr(orchestrator, "_load_roadmap_tasks", lambda: [_task("D1")])
    monkeypatch.setattr(orchestrator, "read_state", lambda: {"parked_tasks": ["D1"]})
    monkeypatch.setattr(orchestrator, "append_journal",
                        lambda event, detail="", **k: journal.append((event, detail)))
    monkeypatch.setattr(orchestrator, "notify_telegram", lambda text, *a, **k: told.append(text))
    seen: list = []
    monkeypatch.setattr(gr, "parked_added",
                        lambda repo, tasks, parked: seen.append(set(parked)) or {"D1": "RP-1"})
    orchestrator._graph_replan_parked_report()
    orchestrator._graph_replan_parked_report()
    assert seen[0] == {"D1"} and len(told) == 1 and "D1" in told[0] and "RP-1" in told[0]
    assert [e for e, _ in journal] == ["goal_replan_task_parked"]


def test_the_exhausted_check_counts_waiting_holds_with_the_parked_tasks(monkeypatch):
    from maestro import orchestrator

    got: list = []
    monkeypatch.setattr(orchestrator, "_graph_goals_contract", lambda: True)
    monkeypatch.setattr(orchestrator, "_load_roadmap_tasks", lambda: [_task("D1")])
    monkeypatch.setattr(orchestrator, "read_state", lambda: {"parked_tasks": ["D1"]})
    monkeypatch.setattr(gr, "waiting_holds",
                        lambda repo, tasks, parked: got.append(set(parked)) or {})
    assert orchestrator._graph_replan_holds(waiting=True) == {} and got == [{"D1"}]


# --- the completion gate's replans (GDT-10) ---------------------------------------------------


def test_a_finish_gap_proposal_uses_its_given_tasks_and_names_the_completion_gate():
    """GDT-10 (R55): the completion gate's `finish_gap` signal carries its own tasks; the
    proposal is stage `finish` and its question names the completion gate. `triggers`
    never raises the kind (only the completion gate does)."""
    doc = _doc()
    assert "finish_gap" in gr.KINDS
    task = {"id": "ac-03-fix-abcd1234", "status": "open", "mode": "autonomous",
            "serves": ["G2"], "contribution": "direct", "criteria": ["AC-03"]}
    signal = gr._signal("G2", "finish_gap", ["FIN-1", "AC-03=red"], [],
                        ["AC-03 RED at abc123: assertion failed"], ["AC-03"], tasks=[task],
                        round=1, cap=2, at_cap=False)
    proposal = gr.propose(signal, doc, OPEN, at=DAY)
    assert proposal["stage"] == "finish"
    first = proposal["alternatives"][proposal["recommended"]]
    assert [t["id"] for t in first["changes"]["add_tasks"]] == ["ac-03-fix-abcd1234"]
    assert not first["requires_dan"]
    assert "completion gate" in proposal["root_cause"]["hypothesis"]
    assert "the completion gate found it unmet" in gr.question(proposal)["spec"]["question"]
    for store in (_store(_episodes("T1"), _episodes("T2")), _store()):
        assert all(s["kind"] != "finish_gap" for s in gr.triggers(store, doc, misses=2))


def test_finish_waits_survive_prune_awaiting_and_stale_ones_are_dropped():
    """GDT-10: an operator check or finish conflict question waits on `finish-<FIN>`,
    which no run owns: `prune_awaiting` keeps it, and `prune_runless_waits` drops the waits
    of a FIN that is no longer the current one (`prune_replan_waits` is its replan case)."""
    from maestro.hitl import verify

    key = verify.QUESTIONS_STATE_KEY
    state = {key: {"finish-FIN-1-OP-01": {"run_id": "finish-FIN-1", "request_ids": ["q1"]},
                   "finish-FIN-0-OP-01": {"run_id": "finish-FIN-0", "request_ids": ["q0"]},
                   "replan-RP-1": {"run_id": "replan-RP-1", "request_ids": ["dq-replan-RP-1"]},
                   "T9": {"run_id": "run-9", "request_ids": ["dq-9"]}}}
    verify.prune_awaiting([], read_state=lambda: dict(state), write_state=state.update)
    assert sorted(state[key]) == ["finish-FIN-0-OP-01", "finish-FIN-1-OP-01", "replan-RP-1"]
    verify.prune_runless_waits("finish-", {"FIN-1"}, read_state=lambda: dict(state),
                               write_state=state.update)
    assert sorted(state[key]) == ["finish-FIN-1-OP-01", "replan-RP-1"]
    verify.prune_replan_waits(set(), read_state=lambda: dict(state), write_state=state.update)
    assert sorted(state[key]) == ["finish-FIN-1-OP-01"]


# --- GDT-10 review H3: every proposed task carries a check the graph runs ---------------------


def _pre_validate(tmp_path: Path, task: dict) -> None:
    """The graph's step-1 refusal for an agent node of `task` (`WorkflowRunner.pre_validate`,
    INV-04), with the live loop's routing set."""
    import subprocess
    from types import SimpleNamespace

    from maestro.control.artifacts import ArtifactStore
    from maestro.control.store import ControlStore
    from maestro.taskgraph import normalize_spec
    from maestro.workflows import handlers, runner
    from maestro.workflows.models import NodeSpec, RetryPolicy, WorkflowRevision

    graph = tmp_path / f"graph-{task['id']}"
    graph.mkdir()
    store = ControlStore.open_controller(graph / ".orchestrator" / "control.sqlite3")
    node = NodeSpec(node_id="implementer", kind="agent", handler_ref="maestro.handlers.agent",
                    agent_definition_ref="implementer", permissions_ref="frozen_snapshot_reader",
                    retry_policy=RetryPolicy(max_attempts=3))
    revision = WorkflowRevision(
        workflow_id=f"wf_{task['id']}", revision=1, task_id=str(task["id"]),
        task_spec_revision=1, compiler_version="fixture", policy_hash="", code_base_sha="",
        execution_objective="paid_efficiency", nodes=(node,), edges=(),
        terminal_requirements=("implementer",))
    run = runner.start_run(
        store, revision, repo=graph,
        dispatcher=handlers.TmuxDispatcher(
            runner=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", "")),
        artifacts=ArtifactStore(graph / ".orchestrator" / "artifacts", store),
        is_alive=lambda pid: True,
        routing=SimpleNamespace(task_spec=lambda tid: normalize_spec(dict(task))))
    try:
        run.pre_validate(node)
    finally:
        store.close()


#: V2 with AC-03 bound to a pytest node.
BOUND = V2.replace("- **Layer:** integration\n- **Evidence:** test\n",
                   "- **Layer:** integration\n- **Evidence:** test\n"
                   "- **Test:** `pytest tests/test_totals.py::test_match`\n")


def test_every_task_a_replan_proposes_carries_a_check_the_graph_runs(tmp_path):
    """GDT-10 review H3 (GDT-8): the graph refuses an agent task with no required
    verification (INV-04), and a `kind: manual` check never discharges there (only Dan's
    dispositions reach `decide`), so a proposed task needs a command the gate runs: a
    diagnosis task's written cause, GDT-4's harness task's credible run of its criterion, a
    direct task's bound command. Each passes `pre_validate`."""
    from maestro import validation

    repo = _repo(tmp_path, BOUND)
    doc = _doc(BOUND)
    results = [acceptance.Result(id="AC-03", state=acceptance.BLOCKED,
                                 reason="ImportError: no bank adapter", revision=doc.revision)]
    feasible = gr._signal("G2", "contradiction", ["f1"], [], ["checkpoint CP-1: AC-03"],
                          ["AC-03"], feasibility=["AC-03 is blocked_environment: no open "
                                                  "enabling task makes its test runnable"])
    gap = gr._signal("G2", "milestone_gap", ["g1"], ["T1:G2"], ["checkpoint CP-2: AC-03 RED"],
                     ["AC-03"])
    tasks = []
    for signal in (feasible, gap):
        proposal = gr.propose(signal, doc, OPEN, task="T2", results=results, at=DAY, repo=repo)
        tasks += [t for alt in proposal["alternatives"] for t in alt["changes"]["add_tasks"]]
    assert len(tasks) == 3
    for task in tasks:
        specs = validation.specs_from_roadmap(task.get("verifications") or [])
        assert any(s.required and s.handler_ref not in validation.JUDGMENT_HANDLERS
                   for s in specs), task
        _pre_validate(tmp_path, task)
    diagnosis = next(t for t in tasks if "-diagnose-" in t["id"])
    cause = gr.cause_file(diagnosis["id"])
    assert diagnosis["verifications"] == [{"id": "V1", "kind": "auto", "cmd": (
        f"head -n 1 {cause} | grep -qE '^Cause: (plan|scope|goal_mapping|harness|measurement|"
        "dependency|constraint|agent|unknown)$'")}]
    assert cause in diagnosis["short_desc"] and cause in diagnosis["evidence"]
    # R66 limit: a diagnosis writes its cause file and nothing else, so it declares that
    # file as its scope (D20) and runs beside independent work instead of alone.
    assert diagnosis["scope"] == [cause]
    assert "scope: [" + cause + "]" in goal_roadmap.render_tasks([diagnosis])
    [harness] = [t for t in tasks if t["id"].startswith("ac-03-harness")]
    assert "-m maestro.acceptance AC-03 --credible" in harness["verifications"][0]["cmd"]
    [direct] = [t for t in tasks if t.get("contribution") == "direct"]
    assert "tests/test_totals.py::test_match" in direct["verifications"][0]["cmd"]
    assert "-p no:cacheprovider" in direct["verifications"][0]["cmd"]
    # R66, accepted: a feasibility replan's missing tasks write files nothing names yet, and a
    # declared scope is enforced (out-of-scope work is refused), so they declare none and run
    # alone (D20) rather than guess a scope that would block their own work.
    assert "scope" not in harness and "scope" not in direct


def test_a_proposed_task_with_no_runnable_command_is_confirmed_by_dan(tmp_path):
    """H3: AC-03 has no `Test:` in V2, so its direct task has no command to run; Dan
    confirms its evidence (`kind: dan`, required, never a reviewer's prose)."""
    doc = _doc()
    task = {"id": "ac-03-fix-abcd1234", "status": "open", "mode": "autonomous",
            "serves": ["G2"], "contribution": "direct", "criteria": ["AC-03"],
            "evidence": "AC-03 GREEN"}
    signal = gr._signal("G2", "finish_gap", ["FIN-1", "AC-03=red"], [], ["AC-03 RED"],
                        ["AC-03"], tasks=[task], rounds=0, cap=2, at_cap=False)
    proposal = gr.propose(signal, doc, OPEN, at=DAY, repo=_repo(tmp_path))
    [added] = proposal["alternatives"][proposal["recommended"]]["changes"]["add_tasks"]
    [check] = added["verifications"]
    assert (check["kind"], check["id"]) == ("dan", "V1") and "AC-03 GREEN" in check["check"]
    _pre_validate(tmp_path, added)


def test_a_diagnosis_task_states_its_cause_on_a_first_line_its_check_requires(tmp_path):
    """GDT-12 (R58): the diagnosis brief asks for one `Cause: <token>` first line, and its
    gate check refuses a cause file without a valid one; the reviewed line is the only text
    W12's attribution reads."""
    import subprocess

    task = gr._diagnosis("G2", "RP-1234abcd", ["AC-03"], _doc(), "AC-03 does not move.")
    assert ("Start the file with one line `Cause: <plan|scope|goal_mapping|harness|"
            "measurement|dependency|constraint|agent|unknown>`") in task["short_desc"]
    assert "`unknown` when the evidence does not establish one" in task["short_desc"]
    [check] = task["verifications"]
    cause = tmp_path / gr.cause_file(task["id"])
    cause.parent.mkdir(parents=True)

    def passes(text: str) -> bool:
        cause.write_text(text, encoding="utf-8")
        return subprocess.run(check["cmd"], shell=True, cwd=tmp_path).returncode == 0

    assert passes("Cause: harness\n\nThe stub reads no caption.\n")
    assert passes("Cause: unknown\n")
    assert not passes("The stub reads no caption.\n")
    assert not passes("Cause: the planner\n")
    assert not passes("cause: harness\n")
    # Review L1: the first line, as W12's attribution reads it (`_cause_line`).
    assert not passes("The stub reads no caption.\nCause: harness\n")
    assert not passes("\nCause: harness\n")
    cause.unlink()
    assert subprocess.run(check["cmd"], shell=True, cwd=tmp_path,
                          stderr=subprocess.DEVNULL).returncode != 0
