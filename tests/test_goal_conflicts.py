"""Goal-vs-goal conflicts: detection, escalation to Dan, persisted decisions (GDT-7).

When a change advances one approved goal and regresses another, only Dan decides; priority
informs the recommendation but never decides (R08, R48). The escalation names the ids,
revision, stage, task and evidence and recommends with a rationale (R49). Dan's decision is
stored and covers the same conflict until the evidence exceeds what he approved, the facts
change, or the contract changes for those ids (R50, C22, C24). A hard invariant is never
traded (R51, C23). At most `MAX_QUESTIONS_PER_ROUND` questions go out at once (R66).
Contract-level conflicts are setup gate problems on the affected ids (R47).
Requirements: R08, R47-R51, R66 (`docs/agent-plans/p13-goal-driven-tdd/`).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from maestro import goal_conflicts as gc
from maestro.docs import goals
from maestro.workflows import dan_questions

REPO_ROOT = Path(__file__).resolve().parents[1]
V2 = (REPO_ROOT / "tests" / "fixtures" / "goals" / "v2_GOALS.md").read_text(encoding="utf-8")

#: G3's AC-04 (a metric, lower is better) measured 10% worse than its baseline.
SLOWER = gc.Regression("G3", "AC-04", 10.45, 9.5, "median of three runs")
ADVANCED = {"G1": ["AC-02"]}
G3_FIRST = V2.replace("1. G2\n2. G1\n3. G3", "1. G3\n2. G2\n3. G1")
DAY = "2026-10-18"


def _ctx(text: str = V2, decisions=(), task: str = "T-7") -> dict:
    return gc.context(goals.parse(text), task, list(decisions))


def _ask(regressions=(SLOWER,), advanced=None, ctx=None, stage="review") -> gc.Judgement:
    return gc.judge(list(regressions), ADVANCED if advanced is None else advanced,
                    _ctx() if ctx is None else ctx, stage=stage)


def _decided(answer: str, text: str = V2, regressions=(SLOWER,), advanced=None) -> dict:
    (question,) = _ask(regressions, advanced, ctx=_ctx(text)).questions
    return gc.decide(question, answer, DAY)


def _cid(kind="tradeoff", advanced="G1", regressed="G3", criteria="AC-04") -> str:
    key = f"{kind}|{advanced}|{regressed}|{criteria}".encode()
    return "GC-" + hashlib.sha1(key).hexdigest()[:8]


# --- detection and escalation (R08, R48, R49, C21) ------------------------------------------


def test_a_regression_while_another_goal_advances_escalates_in_both_priority_orders():
    g1_first = _ask()
    g3_first = _ask(ctx=_ctx(G3_FIRST))
    for judgement in (g1_first, g3_first):
        assert judgement.blocking == () and judgement.accepted == () and judgement.plain == ()
        (question,) = judgement.questions
        assert "never decides" in question["why_undefined"]
    # Priority informs the recommendation only: G1 above G3 recommends the bounded accept,
    # G3 above G1 recommends keeping G3; both still go to Dan.
    first, second = g1_first.questions[0], g3_first.questions[0]
    assert first["options"][first["recommended"]]["resolution"] == "accept"
    assert second["options"][second["recommended"]]["resolution"] == "preserve"


def test_the_escalation_payload_snapshot():
    (question,) = _ask().questions
    cid = _cid()
    assert {k: question[k] for k in ("question", "why_undefined", "options", "recommended")} == {
        "question": (
            f"{cid}: G1 (Imports every export) advanced (AC-02) while G3 (Fast enough) "
            "regressed (AC-04: 10.45 vs baseline 9.5, 10% worse, median of three runs) in "
            "task T-7 at review, contract revision 2. Keep G3, or accept the regression?"),
        "why_undefined": (
            "Only Dan decides a trade-off between approved goals (01 §8). Priority ranks G1 "
            "above G3: it informs the recommendation, never decides (R08). Held: task T-7 "
            "only; independent work continues. Recommended: accept the bounded regression, "
            "because G1 ranks above G3 and its cost is measured (up to +10%)."),
        "options": [
            {"label": "Keep G3",
             "consequence": ("Rework task T-7 so AC-04 holds its baseline; G1's progress may "
                             "shrink. No contract change."),
             "resolution": "preserve", "keep": "G3", "bound": None},
            {"label": "Accept G3 worse by up to +10%",
             "consequence": ("AC-04 may be up to 10% worse than its baseline; your contract "
                             "revision needs a TO- line for it and AC-04's test tolerance "
                             "corrected (until then this decision covers AC-04 in later "
                             "tasks too). Anything worse asks again."),
             "resolution": "accept", "keep": "G1", "bound": 10.0},
        ],
        "recommended": 1,
    }
    conflict = question["conflict"]
    assert {k: conflict[k] for k in ("id", "kind", "stage", "advanced", "regressed",
                                     "criteria", "revision", "tasks")} == {
        "id": cid, "kind": "tradeoff", "stage": "review", "advanced": ["G1"],
        "regressed": "G3", "criteria": ["AC-04"], "revision": 2, "tasks": ["T-7"]}
    assert conflict["measurements"] == [{"criterion": "AC-04", "value": 10.45,
                                         "baseline": 9.5, "direction": "below",
                                         "worse_pct": 10.0}]
    assert set(conflict["terms"]) == {"G1", "G3", "AC-04"} and conflict["fingerprint"]
    # The payload is an ordinary Dan question (Telegram's "Other" reply is free text) and
    # survives JSON, as the question record stores it.
    parsed, error = dan_questions.parse({"questions": [question]})
    assert error == "" and parsed[0]["options"][1]["label"] == "Accept G3 worse by up to +10%"
    assert json.loads(json.dumps(question)) == question


def test_an_unmeasured_regression_offers_no_bound_and_recommends_keeping():
    failing = gc.Regression("G3", "AC-04", evidence="assertion failed: 14 s")
    (question,) = _ask([failing]).questions
    labels = [o["label"] for o in question["options"]]
    assert labels == ["Keep G3", "Accept G3 failing AC-04"]
    assert question["options"][1]["bound"] is None
    assert question["recommended"] == 0
    assert "not measured" in question["why_undefined"]


def test_conflict_ids_are_deterministic():
    conflict = gc.Conflict("tradeoff", "review", ("G1",), "G3", ("AC-04",), 2, (), (),
                           ("T-7",), "")
    assert conflict.id == _cid()
    assert gc.Conflict("tradeoff", "finish", ("G1",), "G3", ("AC-04",), 3, ("other",), (),
                       (), "x").id == conflict.id


@pytest.mark.parametrize("stage", gc.STAGES)
def test_every_stage_builds_the_payload(stage):
    (question,) = _ask(stage=stage).questions
    assert question["conflict"]["stage"] == stage
    assert f"at {stage}," in question["question"]
    assert dan_questions.parse({"questions": [question]})[1] == ""


def test_an_unknown_stage_is_refused():
    with pytest.raises(ValueError):
        _ask(stage="lunch")


def test_no_advancement_elsewhere_or_the_same_goal_is_a_plain_regression():
    assert _ask(advanced={}).plain == (SLOWER,)
    same = _ask(advanced={"G3": ["AC-04"]})
    assert same.plain == (SLOWER,) and same.questions == () and same.blocking == ()


def test_conflicts_coalesce_per_goal_and_to_the_round_cap():
    regressions = [gc.Regression(g, f"AC-{n:02d}", evidence="assertion failed")
                   for n, g in enumerate(("G2", "G3", "G7", "G8", "G9"), start=11)]
    regressions.append(gc.Regression("G3", "AC-04", evidence="assertion failed"))
    judgement = _ask(regressions, advanced={"G1": ["AC-01"]})
    assert len(judgement.questions) == dan_questions.MAX_QUESTIONS_PER_ROUND
    asked = [q["conflict"]["regressed"] for q in judgement.questions]
    assert asked == ["G2", "G3", "G7"]
    g3 = judgement.questions[1]["conflict"]
    assert g3["criteria"] == ["AC-04", "AC-12"]
    assert len(judgement.blocking) == 2
    assert all("waits for the next question round" in b for b in judgement.blocking)


# --- hard invariants (R51, C23) --------------------------------------------------------------


def test_a_hard_invariant_regression_blocks_despite_progress_and_an_accept_decision():
    duplicate = gc.Regression("G1", "AC-01", evidence="an entry stored twice")
    unguarded = V2.replace("(checked by AC-01)", "")
    decision = _decided("Accept G1 failing AC-01", unguarded, [duplicate], {"G3": ["AC-04"]})
    assert decision["resolution"] == "accept" and decision["id"] == _cid(
        advanced="G3", regressed="G1", criteria="AC-01")
    judgement = gc.judge([duplicate], {"G3": ["AC-04"]},
                         _ctx(decisions=[decision]), stage="review")
    assert judgement.questions == () and judgement.accepted == () and judgement.plain == ()
    (finding,) = judgement.blocking
    assert finding.startswith("hard_invariant_regression:")
    assert "INV-01" in finding and "AC-01" in finding


# --- decisions: decide, coverage, re-asks (R50, C22, C24) ------------------------------------


def test_tapping_accept_records_a_bounded_decision():
    decision = _decided("accept g3 worse by up to +10%")
    assert decision["id"] == _cid()
    assert (decision["resolution"], decision["keep"], decision["bound"]) == ("accept", "G1",
                                                                             10.0)
    assert decision["at"] == DAY
    assert decision["regressed"] == "G3" and decision["criteria"] == ["AC-04"]
    assert decision["terms"] and decision["fingerprint"]


def test_a_free_text_answer_is_other():
    decision = _decided("Ship it, but add a cache next week")
    assert decision["resolution"] == "other" and decision["bound"] is None
    assert decision["answer"] == "Ship it, but add a cache next week"


@pytest.mark.parametrize("value,covered", [(10.26, True), (10.45, True), (11.4, False)])
def test_an_accepted_bound_covers_up_to_it_and_beyond_reasks(value, covered):
    decision = _decided("Accept G3 worse by up to +10%")
    later = gc.Regression("G3", "AC-04", value, 9.5, "median of three runs")
    judgement = _ask([later], ctx=_ctx(decisions=[decision]))
    assert judgement.blocking == ()
    if covered:
        assert judgement.questions == ()
        (accepted,) = judgement.accepted
        assert accepted["conflict"] == _cid() and accepted["resolution"] == "accept"
    else:
        assert judgement.accepted == ()
        (question,) = judgement.questions
        assert f"beyond the +10% Dan approved on {DAY}" in question["question"]
        assert question["options"][1]["label"] == "Accept G3 worse by up to +20%"


def test_an_accept_with_no_measurement_now_reasks():
    decision = _decided("Accept G3 worse by up to +10%")
    unmeasured = gc.Regression("G3", "AC-04", evidence="timed out")
    (question,) = _ask([unmeasured], ctx=_ctx(decisions=[decision])).questions
    assert "no measurement" in question["question"]


def test_keep_blocks_citing_dans_decision():
    decision = _decided("Keep G3")
    judgement = _ask(ctx=_ctx(decisions=[decision]))
    assert judgement.questions == () and judgement.accepted == ()
    (finding,) = judgement.blocking
    assert finding.startswith(f"Dan decided {_cid()} ({DAY}): keep G3")


def test_a_free_text_answer_never_accepts_and_new_evidence_asks_again():
    """Dan, 2026-10-18 (GDT-7 review L5): an `other` answer never covers a regression as
    accepted. The same facts still regressing block, citing his words, so the task reworks
    (and parks when its budget runs out); new evidence asks again."""
    decision = _decided("Ship it, but add a cache next week")
    same = _ask(ctx=_ctx(decisions=[decision]))
    assert same.questions == () and same.accepted == () and same.plain == ()
    (finding,) = same.blocking
    assert finding == (f"Dan answered {_cid()} on {DAY}: 'Ship it, but add a cache next "
                       "week'; the change still regresses G3 (AC-04)")
    # ... whatever advanced this time
    assert gc.judge([SLOWER], {}, _ctx(decisions=[decision]), stage="review").blocking == (
        finding,)
    newer = gc.Regression("G3", "AC-04", 10.2, 9.5, "median of three runs")
    (question,) = _ask([newer], ctx=_ctx(decisions=[decision])).questions
    assert "new evidence" in question["question"]


def test_a_contract_change_to_those_ids_reasks_and_an_unrelated_one_does_not():
    decision = _decided("Accept G3 worse by up to +10%")
    revised = V2.replace("- **Threshold:** 10 seconds", "- **Threshold:** 12 seconds")
    (question,) = _ask(ctx=_ctx(revised, decisions=[decision])).questions
    assert "contract changed" in question["question"] and "AC-04" in question["question"]
    unrelated = V2.replace("to the shekel", "to the agora")
    assert _ask(ctx=_ctx(unrelated, decisions=[decision])).questions == ()


# --- GDT-7 review: coverage by (regressed goal, criterion), one ask per conflict -----------


def test_a_stored_accept_covers_the_same_regression_whatever_advanced():
    """Review H1: the stored baseline keeps AC-04 GREEN after Dan accepts, so a later task
    regresses it again. His accept on (G3, AC-04) covers it within the bound whether the
    change advanced nothing or another goal set; beyond the bound he is asked again, under
    the same conflict id."""
    decision = _decided("Accept G3 worse by up to +10%")
    ctx = _ctx(decisions=[decision], task="T-8")
    for advanced in ({}, {"G2": ["AC-03"]}, {"G1": ["AC-02"], "G2": ["AC-03"]}):
        judgement = gc.judge([SLOWER], advanced, ctx, stage="review")
        assert judgement.plain == () and judgement.questions == () and judgement.blocking == ()
        (accepted,) = judgement.accepted
        assert accepted["conflict"] == _cid() and accepted["criteria"] == ["AC-04"]
    worse = gc.Regression("G3", "AC-04", 11.4, 9.5)
    (question,) = gc.judge([worse], {}, ctx, stage="review").questions
    assert f"beyond the +10% Dan approved on {DAY}" in question["question"]
    assert question["conflict"]["id"] == _cid()
    assert question["conflict"]["advanced"] == ["G1"]


def test_dan_s_keep_blocks_a_later_change_that_advances_nothing():
    decision = _decided("Keep G3")
    judgement = gc.judge([SLOWER], {}, _ctx(decisions=[decision]), stage="review")
    assert judgement.plain == () and judgement.questions == () and judgement.accepted == ()
    (finding,) = judgement.blocking
    assert finding.startswith(f"Dan decided {_cid()} ({DAY}): keep G3")


def test_a_criterion_no_decision_covers_is_still_plain_or_asked():
    decision = _decided("Accept G3 worse by up to +10%")
    failing = gc.Regression("G3", "AC-05", evidence="assertion failed")
    ctx = _ctx(decisions=[decision])
    alone = gc.judge([SLOWER, failing], {}, ctx, stage="review")
    assert alone.plain == (failing,) and len(alone.accepted) == 1 and alone.questions == ()
    traded = gc.judge([SLOWER, failing], ADVANCED, ctx, stage="review")
    assert len(traded.accepted) == 1
    (question,) = traded.questions
    assert question["conflict"]["criteria"] == ["AC-05"]


def test_the_fingerprint_holds_structured_facts_never_reason_text():
    """Review H2: a reason with a temp path or an address changes on every run; the same
    criterion, state and values are the same facts."""
    first = gc.Regression("G3", "AC-04", evidence="failed in /tmp/pytest-of-dan/pytest-1/t 0x7f01")
    later = gc.Regression("G3", "AC-04", evidence="failed in /tmp/pytest-of-dan/pytest-2/t 0x7f99")
    decision = _decided("Accept G3 failing AC-04", regressions=[first])
    judgement = _ask([later], ctx=_ctx(decisions=[decision]))
    assert judgement.questions == () and judgement.blocking == ()
    (accepted,) = judgement.accepted
    assert accepted["conflict"] == _cid()


def test_a_criterion_the_gate_judged_is_covered_at_review():
    """Review H2: Dan accepted the gate's measured +10%; the reviewer then reports AC-04 as
    a failing test (no measurement). The gate already judged AC-04 on this tree, so the
    review does not ask him again."""
    decision = _decided("Accept G3 worse by up to +10%")
    failing = gc.Regression("G3", "AC-04", evidence="test tests/bench.py failed (AC-04)")
    ctx = gc.context(goals.parse(V2), "T-7", [decision], judged=["AC-04"])
    assert ctx["judged"] == ["AC-04"]
    judgement = gc.judge([failing], ADVANCED, ctx, stage="review")
    assert judgement.questions == () and judgement.blocking == ()
    (accepted,) = judgement.accepted
    assert accepted["conflict"] == _cid()


def test_one_hard_invariant_finding_per_criterion():
    """Review L7: a criterion of two goals regressing is one finding, not one per goal."""
    regressions = [gc.Regression("G1", "AC-01", evidence="stored twice"),
                   gc.Regression("G2", "AC-01", evidence="stored twice")]
    judgement = gc.judge(regressions, {"G3": ["AC-04"]}, _ctx(), stage="review")
    (finding,) = judgement.blocking
    assert finding.startswith("hard_invariant_regression: G1, G2 regressed on AC-01")


def test_a_question_names_where_it_was_asked():
    """Review L7: the gate asks within the review stage, and says it is the gate."""
    (question,) = gc.judge([SLOWER], ADVANCED, _ctx(), stage="review",
                           place="the gate (review stage)").questions
    assert "at the gate (review stage), contract revision 2" in question["question"]
    assert question["conflict"]["stage"] == "review"


# --- the context the controller freezes ------------------------------------------------------


def test_the_context_is_json_and_carries_what_workers_read():
    ctx = _ctx(decisions=[{"id": "GC-00000000"}])
    assert json.loads(json.dumps(ctx)) == ctx
    assert ctx["revision"] == 2 and ctx["task"] == "T-7"
    assert ctx["priority"] == ["G2", "G1", "G3"]
    assert ctx["invariants"] == {"AC-01": ["INV-01"]}
    assert ctx["directions"] == {"AC-04": "below"}
    assert ctx["goals"]["G3"] == "Fast enough"
    assert {"G1", "G2", "G3", "AC-01", "AC-04"} <= set(ctx["terms"])
    assert ctx["decisions"] == [{"id": "GC-00000000"}]


# --- the store -------------------------------------------------------------------------------


def test_the_store_round_trips_and_replaces_by_id(tmp_path):
    assert gc.load(tmp_path) == []
    first = _decided("Keep G3")
    other = dict(first, id="GC-11111111")
    gc.record(tmp_path, first)
    gc.record(tmp_path, other)
    path = gc.record(tmp_path, dict(first, resolution="accept", bound=10.0))
    assert path == tmp_path / ".orchestrator" / "goal_decisions.json"
    loaded = gc.load(tmp_path)
    assert [d["id"] for d in loaded] == ["GC-11111111", first["id"]]
    assert loaded[1]["resolution"] == "accept"
    assert sorted(p.name for p in path.parent.iterdir()) == ["goal_decisions.json"]
    path.write_text("{not json", encoding="utf-8")
    assert gc.load(tmp_path) == []


def test_an_unreadable_store_is_moved_aside_never_overwritten(tmp_path):
    """Review M3: a corrupt file read as `[]` and overwritten would lose every decision."""
    path = tmp_path / gc.STORE
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    decision = _decided("Keep G3")
    gc.record(tmp_path, decision)
    assert gc.load(tmp_path) == [decision]
    [aside] = [p for p in path.parent.iterdir()
               if p.name.startswith("goal_decisions.json.corrupt-")]
    assert aside.read_text(encoding="utf-8") == "{not json"


def test_recording_a_decision_keeps_the_other_store_sections(tmp_path):
    """GDT-8: the one goals store also keeps episodes, checkpoints and replans; recording a
    decision rewrote the file as `{"decisions": ...}` and would have lost them."""
    from maestro import goal_store

    goal_store.update(tmp_path, "episodes", lambda rows: [*rows, {"id": "T1:G2"}])
    goal_store.update(tmp_path, "replans", lambda rows: [*rows, {"id": "RP-1"}])
    decision = _decided("Keep G3")
    gc.record(tmp_path, decision)
    store = goal_store.read(tmp_path)
    assert store["episodes"] == [{"id": "T1:G2"}] and store["replans"] == [{"id": "RP-1"}]
    assert store["decisions"] == [decision] and store["checkpoints"] == []
    goal_store.update(tmp_path, "checkpoints", lambda rows: [*rows, {"id": "CP-1"}])
    assert gc.load(tmp_path) == [decision]
    assert goal_store.path_for(tmp_path) == tmp_path / gc.STORE == gc.path_for(tmp_path)


def test_concurrent_records_keep_every_decision(tmp_path, monkeypatch):
    """Review M3: two answers recorded at once each read, modify and write the file; the
    store is locked, so neither is lost."""
    import threading
    import time

    real = gc.load

    def slow(repo):
        found = real(repo)
        time.sleep(0.05)
        return found

    monkeypatch.setattr(gc, "load", slow)
    first = _decided("Keep G3")
    ids = [f"GC-{n:08d}" for n in range(6)]
    threads = [threading.Thread(target=gc.record, args=(tmp_path, dict(first, id=i)))
               for i in ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(d["id"] for d in real(tmp_path)) == ids


# --- contract conflicts at setup (R47) -------------------------------------------------------

AC05 = """### AC-05: A year imports with progress shown

- **Goals:** G3
- **Scenarios:** S-03
- **Given:** a year of sample exports on the reference machine
- **When:** the import runs three times
- **Then:** the median wall time is measured
- **Oracle:** median above the threshold
- **Threshold:** {threshold}
- **Layer:** e2e
- **Evidence:** metric
- **Test:** `python bench/year.py`
{extra}
## Operator checks
"""


def _two_metrics(threshold: str = "12 seconds", extra: str = "") -> str:
    text = V2.replace("- **Evidence:** metric\n",
                      "- **Evidence:** metric\n- **Test:** `python bench/year.py`\n", 1)
    text = text.replace("- **Verified by:** AC-04", "- **Verified by:** AC-04, AC-05")
    text = text.replace("- **Criteria:** AC-04", "- **Criteria:** AC-04, AC-05")
    return text.replace("## Operator checks\n", AC05.format(threshold=threshold, extra=extra),
                        1)


def _kind(problems, word: str) -> list[goals.Problem]:
    return [p for p in problems if p.text.startswith(f"{word} conflict")]


def test_thresholds_on_one_binding_that_cannot_both_pass_are_a_gate_problem():
    doc = goals.parse(_two_metrics())
    (problem,) = _kind(gc.contract_problems(doc), "threshold")
    assert problem.severity == "gate"
    assert {"AC-04", "AC-05", "G3"} <= set(problem.ids)
    assert "python bench/year.py" in problem.text
    # goals.py validation reports it (goals check, doctor, runner holds) ...
    assert _kind(doc.gate, "threshold") == [problem]
    # ... and it holds only the affected work.
    held = goals.task_gate(doc, {"id": "T-1", "serves": ["G3"], "tests": []})
    free = goals.task_gate(doc, {"id": "T-2", "serves": ["G2"], "tests": []})
    assert _kind(held, "threshold") and not _kind(free, "threshold")


@pytest.mark.parametrize("threshold,extra", [
    ("2 seconds", ""),                      # below 10 and above 2 can both pass
    ("12 seconds", "- **Mandatory:** no\n"),  # an optional criterion never conflicts
])
def test_thresholds_that_can_both_pass_are_no_problem(threshold, extra):
    doc = goals.parse(_two_metrics(threshold, extra))
    assert _kind(gc.contract_problems(doc), "threshold") == []


@pytest.mark.parametrize("named", ["INV-01", "AC-01"])
def test_a_trade_off_naming_a_hard_invariant_is_a_gate_problem(named):
    text = V2.replace(
        "## Priority",
        f"- TO-02: {named} may slip during the bulk import; decision D-8; bound 5 entries\n\n"
        "## Priority", 1)
    doc = goals.parse(text)
    (problem,) = _kind(doc.gate, "invariant")
    assert {"TO-02", "INV-01", "AC-01"} <= set(problem.ids)
    assert _kind(gc.contract_problems(doc), "invariant") == [problem]


def test_the_fixture_contract_has_no_conflict_problems():
    assert gc.contract_problems(goals.parse(V2)) == ()


# --- wiring: the runner (GDT-7) ----------------------------------------------------------


def _runner_answers(tmp_path, monkeypatch, spec, answer, marked=None):
    """`WorkflowRunner._record_dan_answer` on a stub runner; returns its journal lines
    (`marked` gets the fields it stores on the question record)."""
    from types import SimpleNamespace

    from maestro.docs import decisions
    from maestro.workflows.runner import WorkflowRunner

    monkeypatch.setattr(decisions, "record", lambda *a, **k: "f" * 40)
    journal: list = []
    marked = {} if marked is None else marked
    runner = SimpleNamespace(
        dan_channel=SimpleNamespace(journal=lambda *a: journal.append(a),
                                    mark_question_recorded=lambda _id, **k: marked.update(k)),
        revision=SimpleNamespace(task_id="TASK-001"), run_id="run-1", repo=tmp_path,
        store=SimpleNamespace(append_event=lambda *a, **k: None))
    WorkflowRunner._record_dan_answer(
        runner, {"id": "q-1", "asked_by": "function/gate#1", "spec": spec}, answer,
        at="2026-10-18T10:00:00+03:00")
    return journal


def test_dan_s_answer_to_a_conflict_question_is_stored_as_a_goal_decision(
        tmp_path, monkeypatch):
    [question] = _ask().questions
    label = question["options"][1]["label"]
    _runner_answers(tmp_path, monkeypatch, question, label)
    assert gc.load(tmp_path) == [gc.decide(question, label, "2026-10-18T10:00:00+03:00")]
    assert (tmp_path / gc.STORE).is_file()


AT = "2026-10-18T10:00:00+03:00"


def test_a_failed_store_keeps_dan_s_answer_on_the_question_record(tmp_path, monkeypatch):
    """Review M3: `record` failing (I/O, a lost race) must not waste Dan's answer."""
    [question] = _ask().questions
    label = question["options"][1]["label"]

    def broken(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(gc, "record", broken)
    marked: dict = {}
    journal = _runner_answers(tmp_path, monkeypatch, question, label, marked)
    assert [j for j in journal if j[0] == "goal_decision_record_failed"]
    assert marked["goal_decision"] == gc.decide(question, label, AT)


def _decisions_runner(tmp_path, monkeypatch, records, answers=None):
    from types import SimpleNamespace

    from maestro.hitl import verify

    monkeypatch.setattr(verify, "question_records",
                        lambda **k: [r for r in records
                                     if k.get("task_id") in (None, r.get("task_id"))])
    monkeypatch.setattr(verify, "_answer", lambda req: (answers or {}).get(req))
    journal: list = []
    return SimpleNamespace(repo=tmp_path, revision=SimpleNamespace(task_id="TASK-001"),
                           dan_channel=SimpleNamespace(journal=lambda *a: journal.append(a)))


def test_answered_conflict_questions_join_the_stored_decisions_and_are_written_back(
        tmp_path, monkeypatch):
    """Review M3: the decisions the gate and review read are the store plus every answered
    conflict question of the task; one the store lost is written back."""
    from maestro.workflows.runner import WorkflowRunner

    [question] = _ask().questions
    lost = gc.decide(question, question["options"][1]["label"], AT)
    kept = dict(lost, id="GC-11111111", resolution="preserve")
    gc.record(tmp_path, kept)
    records = [
        {"id": "dq-1", "task_id": "TASK-001", "spec": question, "recorded": True,
         "status": "answered", "goal_decision": lost},
        {"id": "dq-2", "task_id": "TASK-001", "recorded": True, "status": "answered",
         "spec": {"question": "Does a shared track count?", "options": []}},
        {"id": "dq-3", "task_id": "TASK-009", "spec": question, "recorded": True,
         "status": "answered", "goal_decision": dict(lost, id="GC-99999999")},
    ]
    runner = _decisions_runner(tmp_path, monkeypatch, records)
    got = WorkflowRunner.goal_decisions(runner)
    assert [d["id"] for d in got] == ["GC-11111111", lost["id"]]
    assert gc.load(tmp_path) == got


def test_an_answered_conflict_question_without_its_decision_is_decided_from_the_answer(
        tmp_path, monkeypatch):
    from maestro.workflows.runner import WorkflowRunner

    [question] = _ask().questions
    label = question["options"][1]["label"]
    records = [{"id": "dq-1", "task_id": "TASK-001", "spec": question, "status": "answered",
                "ts": "2026-10-18T07:00:00Z"}]
    runner = _decisions_runner(tmp_path, monkeypatch, records, {"dq-1": label})
    [decision] = WorkflowRunner.goal_decisions(runner)
    assert decision == gc.decide(question, label, "2026-10-18T07:00:00Z")
    assert gc.load(tmp_path) == [decision]


def test_the_gate_policy_reads_the_runner_s_decisions(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from maestro.workflows import goal_guard
    from maestro.workflows import runner as runner_mod

    seen: dict = {}
    monkeypatch.setattr(goal_guard, "policy",
                        lambda repo, task, base, decisions=None: seen.update(d=decisions))
    monkeypatch.setattr(runner_mod, "_roadmap_task", lambda task_id: {"id": task_id})
    stub = SimpleNamespace(repo=tmp_path, _merge_target=lambda: "main",
                           revision=SimpleNamespace(task_id="TASK-001", code_base_sha="sha"),
                           goal_decisions=lambda: [{"id": "GC-1"}])
    runner_mod.WorkflowRunner.goals_guard_policy(stub)
    assert seen["d"] == [{"id": "GC-1"}]


def test_an_ordinary_question_stores_no_goal_decision(tmp_path, monkeypatch):
    spec = {"question": "Does a shared track count for B?", "why_undefined": "silent",
            "options": [{"label": "Yes", "consequence": ""},
                        {"label": "No", "consequence": ""}], "recommended": 0}
    _runner_answers(tmp_path, monkeypatch, spec, "Yes")
    assert gc.load(tmp_path) == [] and not (tmp_path / gc.STORE).exists()


def _gate_revision():
    from maestro.workflows.models import EdgeSpec, NodeSpec, WorkflowRevision

    nodes = (
        NodeSpec(node_id="implementer", kind="agent", handler_ref="maestro.handlers.agent",
                 agent_definition_ref="implementer", resource_request={"writer_lease": True}),
        NodeSpec(node_id="gate", kind="function", handler_ref="maestro.handlers.verification"),
        NodeSpec(node_id="proof_review", kind="agent", handler_ref="maestro.handlers.agent",
                 agent_definition_ref="reviewer"),
    )
    edges = (EdgeSpec("implementer", "gate", guard_predicate="on_success", required=True),
             EdgeSpec("gate", "proof_review", guard_predicate="on_success", required=True))
    return WorkflowRevision(
        workflow_id="wf", revision=1, task_id="TASK-001", task_spec_revision=1,
        compiler_version="test", policy_hash="", code_base_sha="sha",
        execution_objective="paid_efficiency", nodes=nodes, edges=edges)


def test_a_gate_asking_dan_holds_the_implementer_like_a_rejecting_reviewer():
    from types import SimpleNamespace

    from maestro.workflows.runner import WorkflowRunner

    revision = _gate_revision()
    runner = SimpleNamespace(revision=revision)
    assert WorkflowRunner._question_cone(runner, revision.node("gate")) == (
        ("implementer", "gate"), "")


def test_the_rework_note_carries_dan_s_conflict_decision_and_the_gate_s_findings():
    from maestro.workflows.runner import WorkflowRunner

    [question] = _ask().questions
    label = question["options"][1]["label"]
    outputs = {"dan_questions": [question],
               "goals_guard_findings": ["hard_invariant_regression: G1 regressed on AC-01"]}
    decided = {dan_questions.normalise(question["question"]): label}
    note = WorkflowRunner._question_note(None, _gate_revision().node("gate"), outputs, decided)
    assert f"Dan decided: {label}" in note
    assert "- hard_invariant_regression: G1 regressed on AC-01" in note


def test_the_newest_decision_covers_whatever_its_place_in_the_list():
    """GDT-7 verification low: a rebuilt older answer appended last must not shadow a newer
    stored decision with another id; ties keep the later entry."""
    newer = {"id": "GC-aaaaaaaa", "regressed": "G3", "criteria": ["AC-04"],
             "at": "2026-10-18T12:00:00+03:00"}
    older = {"id": "GC-bbbbbbbb", "regressed": "G3", "criteria": ["AC-04"],
             "at": "2026-10-18T09:00:00+03:00"}
    assert gc._covering([newer, older], "G3", "AC-04") is newer
    assert gc._covering([older, newer], "G3", "AC-04") is newer
    tie = dict(older, id="GC-cccccccc", at=newer["at"])
    assert gc._covering([newer, tie], "G3", "AC-04") is tie


def test_a_rebuilt_decision_takes_the_time_dan_answered(tmp_path, monkeypatch):
    """GDT-7 verification low: the answer's time, not the question record's, is `at`."""
    from maestro.hitl import verify
    from maestro.workflows.runner import WorkflowRunner

    [question] = _ask().questions
    label = question["options"][1]["label"]
    records = [{"id": "dq-1", "task_id": "TASK-001", "spec": question, "status": "answered",
                "ts": "2026-10-18T07:00:00Z"}]
    runner = _decisions_runner(tmp_path, monkeypatch, records, {"dq-1": label})
    monkeypatch.setattr(verify, "_answered_at", lambda req: "2026-10-18T09:30:00Z")
    [decision] = WorkflowRunner.goal_decisions(runner)
    assert decision["at"] == "2026-10-18T09:30:00Z"
    records[0]["answered_at"] = "2026-10-18T08:15:00Z"
    gc.STORE and (tmp_path / gc.STORE).unlink()
    [decision] = WorkflowRunner.goal_decisions(runner)
    assert decision["at"] == "2026-10-18T08:15:00Z"
