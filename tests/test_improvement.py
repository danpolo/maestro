"""GDT-12 S3 (W12; R57, R60, R65): the analyst and its improvement proposals.

One analysis run (`improvement.run`) attributes, packs the evidence, and only when a
pattern exists makes one bounded call on the diagnoser route (no new role). The answer is
validated against the full patterns (`check`): a proposal must cite a pattern shown to the
analyst and only that pattern's evidence, never blame a role the pattern's fault does not
name, stay within the pattern's spend, and a config lever may only raise a role's
strength. An invalid answer is retried twice with its problems, then the run fails with no
proposal: there is no weaker fallback. Proposals are ranked by expected saving, at most
three are asked (questions that hold nothing), and decline memory keeps a declined or
noted proposal from returning without new evidence. The analyst is always a scripted
double here; the store is `.orchestrator/improvement.json`, the ledger a real
`ControlStore` (`tests/improvement_fixture.py`).

S4 (§7): an approved config lever edits only its leaf of the global config (a tmp
`MAESTRO_GLOBAL_CONFIG`) and rolls back byte for byte; a code lever becomes an intake report
in Maestro's checkout (a tmp git repo through `MAESTRO_IMPROVEMENT_INTAKE`) and is measured
from the first adopted release naming it; a worse effect asks Dan, never acts; the digest
reports effects; and nothing changes an accepted verdict (R68).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from maestro import improvement as imp, improvement_evidence as ie, model_policy
from maestro.roles import ROLE_DIAGNOSER
from maestro.workflows import telemetry_capture

from tests import improvement_fixture as fx
from tests.hang_bound import HANG_SECONDS

ROOT = Path(__file__).resolve().parents[1]
POOL = "codex-subscription"
REVIEWER_KEY = ["reviewer", "missed_earlier", "reviewer"]


@pytest.fixture
def ledger(tmp_path):
    db, store = fx.ledger(tmp_path)
    arts = fx.artifacts(tmp_path, store)
    yield tmp_path, db, store, arts
    store.close()


def _task(store, task, finished, *, cause="missed_earlier", role="reviewer", recorded=None):
    """An accepted task whose reviewer attempts 2 and 3 carry `cause` rows, recorded at
    `recorded` (default: when it finished)."""
    run = fx.add_task(store, task, finished_at=finished, attempts=fx.work(reviews=3))
    for i in (2, 3):
        assert telemetry_capture.record_extra_step(
            store, run_id=run, cause=cause, role=role, attempt_id=f"{run}-a{i}",
            node_id="reviewer", at=recorded or finished)
    return run


def _reviewed(store, n=4, start=1, **kw):
    for k in range(n):
        _task(store, f"t{start + k}", fx.iso(start + k), **kw)


class Analyst:
    """A scripted analyst: each answer is a function of the pack it was shown (or a raw
    string). The last answer repeats. Records every (system, user) it received."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, system, user):
        self.calls.append((system, user))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if callable(answer):
            answer = answer(self.pack(user))
        return answer if isinstance(answer, str) else json.dumps(answer)

    @staticmethod
    def pack(user):
        return json.JSONDecoder().raw_decode(user)[0]


class Channel:
    def __init__(self):
        self.sent = {}
        self.answers = {}
        self.recorded = {}

    def poll_question(self, spec):
        self.sent.setdefault(spec["id"], spec)
        return self.answers.get(spec["id"])

    def mark_question_recorded(self, qid, **kw):
        self.recorded[qid] = kw

    def supersede_question(self, qid):
        self.superseded = [*getattr(self, "superseded", []), qid]
        return True


def _prop(pack, *, lever="role_prompt", target="reviewer", pattern=0, saving=100, pool=POOL,
          evidence=2, **over):
    p = pack["patterns"][pattern]
    out = {"pattern": p["id"], "lever": lever, "target": target,
           "change": f"{lever} for {target}", "config": None,
           "evidence": p["evidence"][:evidence],
           "expected_saving": {"cost_units": saving, "pool": pool,
                               "basis": "half of the reviewer rows"},
           "risk": "a longer review", "cost": "one prompt edit",
           "measure": {"metrics": ["extra_steps_per_task:reviewer/missed_earlier",
                                   "cost_units_per_task:codex-subscription"]},
           "rollback": "revert the prompt", "why": "the reviewer misses what it sees later",
           "alternatives": ["raise the reviewer's strength"], "revises": None}
    out.update(over)
    return out


def _answer(*make):
    return lambda pack: {"proposals": [m(pack) for m in make]}


def _noop(proposal):
    return None


def _run(repo, db, store, arts, analyse, *, at, kind="weekly", channel=None, notify=None):
    seen = []
    out = imp.run(repo, db, store, arts, kind=kind, key=f"{kind}:{at}", at=at,
                  analyse=analyse, channel=channel, notify=notify or (lambda text: None),
                  journal=lambda event, detail: seen.append((event, detail)),
                  apply=_noop, queue=_noop)
    return out, seen


def _poll(repo, channel, at):
    seen = []
    decided = imp.poll(repo, channel, at, apply=_noop, queue=_noop,
                       journal=lambda event, detail: seen.append((event, detail)))
    return decided, seen


def _events(seen):
    return [e for e, _ in seen]


# --- 1-2. no call without a pattern; the diagnoser route and the bound (R65) ------------------


def test_no_pattern_means_no_analyst_call_and_an_empty_run(ledger):
    repo, db, store, arts = ledger
    # Two rows in one task, one in another: below every pattern minimum.
    _task(store, "a", fx.iso(1))
    fx.add_task(store, "b", finished_at=fx.iso(2), attempts=fx.work())
    analyse = Analyst({"proposals": []})
    channel = Channel()
    out, seen = _run(repo, db, store, arts, analyse, at=fx.iso(5), channel=channel)
    assert analyse.calls == []
    assert out["status"] == "empty" and out["call"] is None and out["proposals"] == []
    assert "improvement_run_empty" in _events(seen)
    assert channel.sent == {}
    [stored] = imp.read(repo)["runs"]
    assert stored["id"] == "IR-0001" and stored["status"] == "empty"
    assert stored["window"] == {"since": None, "until": fx.iso(5)}
    assert imp.read(repo)["proposals"] == []


def test_the_analyst_is_the_diagnoser_route_and_its_brief_fits_the_bound(ledger, monkeypatch):
    repo, db, store, arts = ledger
    causes = [("not_fixed", "implementer"), ("fixed_incompletely_in_scope", "implementer"),
              ("missed_earlier", "reviewer"), ("introduced_by_rework", "implementer"),
              ("scope_widened", "planner"), ("check_failed", "implementer"),
              ("spec_gap", "planner"), ("bad_graph", "planner"),
              ("fixed_incompletely_out_of_scope", "reviewer"), ("check_broken", "planner")]
    for n in range(12):
        fx.add_task(store, f"big{n}", finished_at=fx.iso(n + 1), attempts=fx.work(reviews=2),
                    causes=[(c, r, 1) for c, r in causes])
    analyse = Analyst({"proposals": []})
    out, _ = _run(repo, db, store, arts, analyse, at=fx.iso(20))
    [(system, user)] = analyse.calls
    assert len(user) <= ie.BRIEF_LIMIT
    pack = json.loads(user)
    assert pack["patterns"] and pack["levers"] == list(ie.LEVERS)
    for rule in ("JSON", "cite only", "never blame", "only raises", "alternatives"):
        assert rule in system
    assert out["status"] == "done"
    assert out["call"]["role"] == ROLE_DIAGNOSER and out["call"]["tries"] == 1
    assert out["call"]["chars_in"] >= len(user)

    # The real seam: one bounded `agentcall.ask` on the diagnoser role.
    from maestro.selfheal import diagnose

    asked = []

    def ask(role, prompt, **kw):
        asked.append((role, prompt, kw))
        return SimpleNamespace(text='{"proposals": []}', timed_out=False, returncode=0)

    monkeypatch.setattr(diagnose.agentcall, "ask", ask)
    assert imp.analyst("SYSTEM", "USER") == '{"proposals": []}'
    [(role, prompt, kw)] = asked
    assert role == ROLE_DIAGNOSER and kw["timeout"] == imp.ANALYST_TIMEOUT == 300
    assert "SYSTEM" in prompt and "USER" in prompt
    # No new role: the model policy's roles are the shipped five.
    assert sorted(model_policy.shipped_role_strengths()) == [
        "diagnoser", "implementer", "planner", "reviewer", "test_designer"]


# --- 3-7. the proposal format and its validation (R60, R58) ---------------------------------


def test_a_ledger_dominated_by_reviewer_missed_earlier_yields_a_reviewer_side_proposal(ledger):
    repo, db, store, arts = ledger
    _reviewed(store, 4)
    fx.add_task(store, "impl", finished_at=fx.iso(6), attempts=fx.work(),
                causes=[("check_failed", "implementer", 0)])
    analyse = Analyst(
        _answer(lambda p: _prop(p, lever="planner_rule", target="planner"),
                lambda p: _prop(p)),
        _answer(lambda p: _prop(p)))
    channel = Channel()
    out, seen = _run(repo, db, store, arts, analyse, at=fx.iso(10), channel=channel)
    pack = Analyst.pack(analyse.calls[0][1])
    assert pack["patterns"][0]["key"] == REVIEWER_KEY
    # The planner rule is refused, and the refusal goes back to the analyst.
    assert len(analyse.calls) == 2
    retry = analyse.calls[1][1]
    assert "planner_rule" in retry and "planner" in retry
    assert out["status"] == "done" and out["call"]["tries"] == 2
    [row] = imp.read(repo)["proposals"]
    assert (row["id"], row["lever"], row["target"], row["state"]) == (
        "IP-0001", "role_prompt", "reviewer", "asked")
    assert row["pattern"]["key"] == REVIEWER_KEY and row["pattern"]["rows"] == 8
    assert row["question_id"] == "dq-improve-IP-0001"
    [spec] = channel.sent.values()
    text = spec["spec"]["question"]
    assert row["pattern"]["id"] in text and "missed_earlier" in text and "reviewer" in text
    assert all(e in text for e in row["evidence"][:2])
    assert "improvement_run_done" in _events(seen)
    assert "improvement_proposal_asked" in _events(seen)


def _checked(repo, db, make, rows=(), view=None):
    pack, found = imp.evidence(repo, db, since=None, until=None, earlier=rows)
    document = {"proposals": [make(pack, found)]}
    return imp.check(document, pack, list(rows),
                     ie.config_view(repo) if view is None else view, patterns=found)


@pytest.mark.parametrize("mutate, word", [
    (lambda p, f: {**_prop(p), "evidence": ["attempt:invented-a9"]}, "evidence"),
    (lambda p, f: {**_prop(p), "evidence": []}, "evidence"),
    (lambda p, f: {**_prop(p), "pattern": "PAT-00000000"}, "pattern"),
    (lambda p, f: _prop(p, lever="rewrite_everything"), "lever"),
    (lambda p, f: _prop(p, saving=10_000), "saving"),
    (lambda p, f: _prop(p, saving=-1), "saving"),
    (lambda p, f: _prop(p, pool="claude-subscription"), "pool"),
    (lambda p, f: {**_prop(p), "expected_saving": {"cost_units": None, "pool": None,
                                                   "basis": "a guess"}}, "unmeasured"),
    (lambda p, f: {**_prop(p), "rollback": ""}, "rollback"),
    (lambda p, f: {**_prop(p), "risk": " "}, "risk"),
    (lambda p, f: {**_prop(p), "measure": {"metrics": []}}, "metric"),
    (lambda p, f: {**_prop(p), "measure": {"metrics": ["green_criteria"]}}, "metric"),
    (lambda p, f: {**_prop(p), "revises": "IP-0042"}, "revises"),
])
def test_check_refuses_invented_evidence_unknown_levers_savings_above_the_pattern_spend_and_missing_rollback(
        ledger, mutate, word):
    repo, db, store, arts = ledger
    _reviewed(store, 4)
    assert _checked(repo, db, lambda p, f: _prop(p)) == []
    problems = _checked(repo, db, mutate)
    assert len(problems) == 1 and word in problems[0], problems


def test_check_reads_the_full_pattern_not_the_five_ids_the_pack_shows(ledger):
    repo, db, store, arts = ledger
    _reviewed(store, 4)          # 8 evidence ids; the pack shows 5
    pack, found = imp.evidence(repo, db, since=None, until=None)
    assert len(pack["patterns"][0]["evidence"]) == ie.SHOWN_EVIDENCE
    hidden = found[0]["evidence"][-1]
    assert hidden not in pack["patterns"][0]["evidence"]
    proposal = {**_prop(pack), "evidence": [hidden]}
    assert imp.check({"proposals": [proposal]}, pack, [], ie.config_view(repo),
                     patterns=found) == []
    # The spend bound is the full pattern's: 8 reviewer attempts of 100 units.
    assert found[0]["spend"][POOL]["cost_units"] == 800
    assert _checked(repo, db, lambda p, f: _prop(p, saving=800)) == []


def test_an_unattributed_pattern_gets_only_evidence_gathering_levers(ledger):
    repo, db, store, arts = ledger
    _reviewed(store, 3, cause="task_did_not_serve_goal", role="planner")
    pack, found = imp.evidence(repo, db, since=None, until=None)
    # The row's role is its attempt's (the reviewer node); the fault is unattributed.
    assert pack["patterns"][0]["key"] == ["unattributed", "task_did_not_serve_goal", "reviewer"]
    view = ie.config_view(repo)

    def problems(**kw):
        doc = {"proposals": [_prop(pack, **kw)]}
        return imp.check(doc, pack, [], view, patterns=found)

    for lever, target in (("planner_rule", "planner"), ("role_prompt", "planner"),
                          ("model_effort", "planner")):
        [problem] = problems(lever=lever, target=target)
        assert "unattributed" in problem and lever in problem
    for lever, target in (("gate", ""), ("human_step", ""), ("graph_order", ""),
                          ("context_skill", "planner")):
        assert problems(lever=lever, target=target) == []
    # On an attributed pattern a role lever names the pattern's own fault role only.
    _reviewed(store, 4, start=10)
    pack, found = imp.evidence(repo, db, since=fx.iso(9), until=None)
    assert pack["patterns"][0]["key"] == REVIEWER_KEY
    [problem] = imp.check({"proposals": [_prop(pack, target="implementer")]}, pack, [], view,
                          patterns=found)
    assert "reviewer" in problem and "implementer" in problem


def _strength(lever="model_effort", target="reviewer", path="roles.reviewer.strength",
              value="strong"):
    return lambda p, f: _prop(p, lever=lever, target=target,
                              config={"path": path, "value": value})


def test_a_config_lever_may_only_raise_a_role_strength_and_never_one_project_yaml_masks(
        ledger, monkeypatch):
    repo, db, store, arts = ledger
    _reviewed(store, 4)
    glob = repo / "global.yaml"
    glob.write_text("roles:\n  reviewer:\n    strength: balanced\n", encoding="utf-8")
    monkeypatch.setenv("MAESTRO_GLOBAL_CONFIG", str(glob))
    assert _checked(repo, db, _strength()) == []
    for make, word in ((_strength(value="balanced"), "stronger"),
                       (_strength(value="light"), "stronger"),
                       (_strength(value="ultra"), "strength"),
                       (_strength(path="roles.reviewer.effort"), "path"),
                       (_strength(path="roles.implementer.strength"), "path"),
                       (_strength(lever="role_prompt"), "model_effort")):
        [problem] = _checked(repo, db, make)
        assert word in problem, problem
    # The shipped reviewer is already strong: nothing to raise.
    monkeypatch.setenv("MAESTRO_GLOBAL_CONFIG", str(repo / "no-global.yaml"))
    [problem] = _checked(repo, db, _strength())
    assert "stronger" in problem
    # A project.yaml that sets the leaf would mask the global change.
    monkeypatch.setenv("MAESTRO_GLOBAL_CONFIG", str(glob))
    (repo / "project.yaml").write_text("roles:\n  reviewer:\n    strength: balanced\n",
                                       encoding="utf-8")
    [problem] = _checked(repo, db, _strength())
    assert "project.yaml" in problem


@pytest.mark.parametrize("bad", [
    "not json at all",
    {"proposals": "none"},
    lambda p: {"proposals": [_prop(p, lever="planner_rule", target="planner")]},
])
def test_an_invalid_answer_is_retried_twice_with_its_problems_then_the_run_fails_without_proposals(
        ledger, bad):
    repo, db, store, arts = ledger
    _reviewed(store, 4)
    analyse = Analyst(bad)
    channel = Channel()
    out, seen = _run(repo, db, store, arts, analyse, at=fx.iso(10), channel=channel)
    assert len(analyse.calls) == 3          # one call and two retries, never a fourth
    first = analyse.calls[0][1]
    for _, user in analyse.calls[1:]:
        assert user.startswith(first) and len(user) > len(first)   # the problems appended
    assert out["status"] == "failed" and out["problems"] and out["proposals"] == []
    assert out["call"]["tries"] == 3
    assert imp.read(repo)["proposals"] == [] and channel.sent == {}
    assert "improvement_analysis_failed" in _events(seen)
    # The next due run tries again.
    analyse = Analyst(_answer(lambda p: _prop(p)))
    _reviewed(store, 2, start=20)
    out, _ = _run(repo, db, store, arts, analyse, at=fx.iso(30))
    assert out["status"] == "done" and len(analyse.calls) == 1
    # A failed run does not move the window: the second run read the first one's tasks.
    assert out["window"]["since"] is None and out["tasks"] == 6


# --- 8. ranking ---------------------------------------------------------------------------------


def test_proposals_are_ranked_by_expected_saving_and_at_most_three_are_asked(ledger):
    repo, db, store, arts = ledger
    _reviewed(store, 4)
    levers = [("role_prompt", "reviewer", 10), ("context_skill", "reviewer", 50),
              ("gate", "", 30), ("human_step", "", 20), ("graph_order", "", 40)]
    analyse = Analyst(_answer(*[lambda p, l=l, t=t, s=s: _prop(p, lever=l, target=t, saving=s)
                                for l, t, s in levers]))
    channel = Channel()
    out, _ = _run(repo, db, store, arts, analyse, at=fx.iso(10), channel=channel)
    rows = {r["lever"]: r for r in imp.read(repo)["proposals"]}
    assert [rows[l]["state"] for l, _, _ in levers] == [
        "deferred", "asked", "asked", "deferred", "asked"]
    asked = [r["id"] for r in imp.read(repo)["proposals"] if r["state"] == "asked"]
    assert sorted(channel.sent) == sorted(f"dq-improve-{i}" for i in asked)
    assert len(channel.sent) == imp.MAX_ASKED == 3
    assert out["proposals"] == [rows[l]["id"] for l in
                                ("context_skill", "graph_order", "gate", "human_step",
                                 "role_prompt")]
    # The deferred ones are candidates again at the next run. While the three asked stay
    # unanswered they outrank them, and at most MAX_ASKED questions are open (review M4a).
    _reviewed(store, 2, start=20)
    _run(repo, db, store, arts, Analyst({"proposals": []}), at=fx.iso(30), channel=channel)
    rows = {r["lever"]: r for r in imp.read(repo)["proposals"]}
    assert rows["human_step"]["state"] == rows["role_prompt"]["state"] == "deferred"
    assert len(channel.sent) == 3
    # Once Dan has answered those, the next run asks the deferred ones.
    for ip in asked:
        channel.answers[f"dq-improve-{ip}"] = "Decline"
    _poll(repo, channel, fx.iso(31))
    _reviewed(store, 2, start=40)
    _run(repo, db, store, arts, Analyst({"proposals": []}), at=fx.iso(50), channel=channel)
    rows = {r["lever"]: r for r in imp.read(repo)["proposals"]}
    assert rows["human_step"]["state"] == rows["role_prompt"]["state"] == "asked"
    assert len(channel.sent) == 5


# --- 9-11. decline memory (R60, C32; W12 proof 2) -----------------------------------------------


def _asked_and_answered(repo, db, store, arts, answer):
    _reviewed(store, 4)
    channel = Channel()
    _run(repo, db, store, arts, Analyst(_answer(lambda p: _prop(p))), at=fx.iso(5),
         channel=channel)
    channel.answers["dq-improve-IP-0001"] = answer
    decided, seen = _poll(repo, channel, fx.iso(20))
    return channel, decided, seen


def test_a_declined_proposal_is_not_resent_without_new_evidence(ledger):
    repo, db, store, arts = ledger
    channel, decided, seen = _asked_and_answered(repo, db, store, arts, "Decline")
    [row] = decided
    assert row["state"] == "declined" and row["decision"]["resolution"] == "decline"
    assert "improvement_decided" in _events(seen)
    assert channel.recorded["dq-improve-IP-0001"]
    # The same pattern again: 4 rows, 2 recorded before the decline, 2 after.
    _task(store, "t10", fx.iso(6))
    _task(store, "t11", fx.iso(21))
    analyse = Analyst(_answer(lambda p: _prop(p, change="a different wording")))
    out, seen = _run(repo, db, store, arts, analyse, at=fx.iso(30), channel=channel)
    assert len(analyse.calls) == 1 and out["status"] == "done"
    [suppressed] = out["suppressed"]
    assert suppressed["of"] == "IP-0001" and "declined" in suppressed["reason"]
    assert "improvement_proposal_suppressed" in _events(seen)
    assert list(channel.sent) == ["dq-improve-IP-0001"]
    assert [r["id"] for r in imp.read(repo)["proposals"]] == ["IP-0001"]
    # Dan's answer is in the next analyst's brief.
    earlier = Analyst.pack(analyse.calls[0][1])["earlier"]
    assert [(e["id"], e["state"]) for e in earlier] == [("IP-0001", "declined")]


def test_three_new_rows_after_the_decline_are_new_evidence_and_it_is_asked_again(ledger):
    repo, db, store, arts = ledger
    channel, _, _ = _asked_and_answered(repo, db, store, arts, "Decline")
    _task(store, "t10", fx.iso(21))
    _task(store, "t11", fx.iso(22))
    out, _ = _run(repo, db, store, arts, Analyst(_answer(lambda p: _prop(p))), at=fx.iso(30),
                  channel=channel)
    assert out["suppressed"] == []
    rows = imp.read(repo)["proposals"]
    assert [(r["id"], r["state"]) for r in rows] == [("IP-0001", "declined"),
                                                    ("IP-0002", "asked")]
    assert rows[1]["new_evidence"] == {"since": "IP-0001", "at": fx.iso(20), "rows": 4}
    text = channel.sent["dq-improve-IP-0002"]["spec"]["question"]
    assert "new since you declined IP-0001" in text and "4 rows" in text


def test_a_noted_proposal_may_return_once_revised_with_dan_s_note(ledger):
    repo, db, store, arts = ledger
    note = "Only for tasks that touch the parser"
    channel, [row], _ = _asked_and_answered(repo, db, store, arts, note)
    assert row["state"] == "noted"
    assert row["decision"] == {"resolution": "notes", "answer": note, "at": fx.iso(20)}
    # Recorded before the note: no new evidence.
    _task(store, "t10", fx.iso(6))
    _task(store, "t11", fx.iso(7))
    revised = lambda p: _prop(p, change="role prompt, parser tasks only", revises="IP-0001")
    analyse = Analyst(_answer(revised))
    out, _ = _run(repo, db, store, arts, analyse, at=fx.iso(30), channel=channel)
    assert note in analyse.calls[0][1]           # his note is in the brief
    rows = imp.read(repo)["proposals"]
    assert [(r["id"], r["state"], r["revises"]) for r in rows] == [
        ("IP-0001", "noted", None), ("IP-0002", "asked", "IP-0001")]
    assert "IP-0001" in channel.sent["dq-improve-IP-0002"]["spec"]["question"]
    # Noted again; a second revision of IP-0001 is not sent without new evidence.
    channel.answers["dq-improve-IP-0002"] = "Still too broad"
    _poll(repo, channel, fx.iso(31))
    _task(store, "t12", fx.iso(32), recorded=fx.iso(8))
    _task(store, "t13", fx.iso(33), recorded=fx.iso(9))
    out, _ = _run(repo, db, store, arts, Analyst(_answer(revised)), at=fx.iso(40),
                  channel=channel)
    assert [s["of"] for s in out["suppressed"]] == ["IP-0001"]
    assert len(imp.read(repo)["proposals"]) == 2


# --- 12. when it runs (R57) ---------------------------------------------------------------------


def test_weekly_is_due_after_the_interval_and_project_end_once_per_head(ledger):
    repo, db, store, arts = ledger
    day = lambda d: fx.iso(24 * d)
    assert imp.due(repo, "weekly", at=day(0)) == "weekly:2026-10-01"
    _run(repo, db, store, arts, Analyst({"proposals": []}), at=day(0))
    assert imp.due(repo, "weekly", at=day(6)) is None
    assert imp.due(repo, "weekly", at=day(7)) == "weekly:2026-10-08"
    assert imp.due(repo, "project_end", at=day(1), head="abc") == "project_end:abc"
    imp.run(repo, db, store, arts, kind="project_end", key="project_end:abc", at=day(8),
            analyse=Analyst({"proposals": []}), apply=_noop, queue=_noop)
    assert imp.due(repo, "project_end", at=day(9), head="abc") is None
    assert imp.due(repo, "project_end", at=day(9), head="def") == "project_end:def"
    # Any run of the project counts for the interval.
    assert imp.due(repo, "weekly", at=day(14)) is None
    assert imp.due(repo, "weekly", at=day(15)) == "weekly:2026-10-16"
    (repo / "project.yaml").write_text("thresholds:\n  improvement_interval_days: 0\n",
                                       encoding="utf-8")
    assert imp.due(repo, "weekly", at=day(60)) is None
    assert imp.due(repo, "project_end", at=day(60), head="xyz") == "project_end:xyz"


def test_an_empty_run_does_not_move_the_window_so_a_pattern_split_across_runs_still_forms(
        ledger):
    """S4b, the first idle tick: on a fresh project the weekly run can come after the first
    task, find no pattern and make no call. Its window is read again by the next run, so
    that task's rows and a later task's form the pattern together; a run that called the
    analyst (or found a pattern) moves the window, a failed one never does."""
    repo, db, store, arts = ledger
    _task(store, "t1", fx.iso(1))
    first, _ = _run(repo, db, store, arts, Analyst({"proposals": []}), at=fx.iso(2))
    assert (first["status"], first["tasks"], first["call"]) == ("empty", 1, None)
    _task(store, "t2", fx.iso(3))
    analyse = Analyst({"proposals": []})
    second, _ = _run(repo, db, store, arts, analyse, at=fx.iso(4), kind="project_end")
    assert second["window"] == {"since": None, "until": fx.iso(4)}
    assert (second["status"], second["tasks"], len(analyse.calls)) == ("done", 2, 1)
    assert second["patterns"]
    third, _ = _run(repo, db, store, arts, Analyst({"proposals": []}), at=fx.iso(6))
    assert third["window"] == {"since": fx.iso(4), "until": fx.iso(6)}


# --- 13. the question (R60) ---------------------------------------------------------------------


def test_the_question_offers_approve_and_decline_names_every_field_and_holds_nothing(
        ledger, monkeypatch):
    repo, db, store, arts = ledger
    _reviewed(store, 4)
    glob = repo / "global.yaml"
    glob.write_text("roles:\n  reviewer:\n    strength: balanced\n", encoding="utf-8")
    monkeypatch.setenv("MAESTRO_GLOBAL_CONFIG", str(glob))
    analyse = Analyst(_answer(
        lambda p: _prop(p, saving=300),
        lambda p: _prop(p, lever="model_effort", saving=200,
                        config={"path": "roles.reviewer.strength", "value": "strong"})))
    channel = Channel()
    _run(repo, db, store, arts, analyse, at=fx.iso(10), channel=channel)
    code, conf = imp.read(repo)["proposals"]
    assert conf["config"] == {"path": "roles.reviewer.strength", "value": "strong",
                              "previous": "balanced"}
    spec = channel.sent["dq-improve-IP-0001"]
    assert spec == imp.question(code)
    assert (spec["id"], spec["run_id"], spec["task_id"], spec["title"], spec["holds"]) == (
        "dq-improve-IP-0001", "improve-IP-0001", "maestro", "Improvement IP-0001", False)
    body = spec["spec"]
    text = body["question"]
    for field in ("extra_step", "8 rows", "4 tasks", "reviewer", "missed_earlier",
                  *code["evidence"][:2], POOL, "800", code["change"], "300",
                  "half of the reviewer rows", code["risk"], code["cost"],
                  "extra_steps_per_task:reviewer/missed_earlier",
                  "the next 5 accepted tasks vs the 5 before", code["rollback"], code["why"],
                  code["alternatives"][0]):
        assert str(field) in text, field
    assert body["why_undefined"] == ("Maestro changes its own config or code only with your "
                                     "approval (B14).")
    assert [o["label"] for o in body["options"]] == ["Approve", "Decline"]
    assert body["recommended"] == 0
    key = imp.project_key(repo)
    assert f"I-{key}-IP-0001" in body["options"][0]["consequence"]
    assert body["options"][1]["consequence"] == "Not sent again without new evidence."
    conf_option = channel.sent["dq-improve-IP-0002"]["spec"]["options"][0]["consequence"]
    assert "roles.reviewer.strength: strong" in conf_option and "rolls it back" in conf_option
    # Dan's answer: a tap approves or declines; anything else is a note.
    assert imp.decide(code, "Approve", "T")["resolution"] == "approve"
    assert imp.decide(code, "decline", "T")["resolution"] == "decline"
    assert imp.decide(code, "only on Mondays", "T") == {
        "resolution": "notes", "answer": "only on Mondays", "at": "T"}


# --- 14. read-only report ---------------------------------------------------------------------


def test_python_m_maestro_improvement_report_is_read_only(ledger):
    repo, db, store, arts = ledger
    _reviewed(store, 4)
    _run(repo, db, store, arts, Analyst(_answer(lambda p: _prop(p))), at=fx.iso(10))
    path = imp.path_for(repo)
    before = path.read_bytes()
    events = store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = subprocess.run([sys.executable, "-m", "maestro.improvement", "report",
                           "--repo", str(repo)], cwd=str(repo), env=env,
                          capture_output=True, text=True, timeout=HANG_SECONDS)
    assert proc.returncode == 0, proc.stderr
    shown = json.loads(proc.stdout)
    assert [r["id"] for r in shown["runs"]] == ["IR-0001"]
    assert [p["id"] for p in shown["proposals"]] == ["IP-0001"]
    assert shown["effects"] == []
    proc = subprocess.run([sys.executable, "-m", "maestro.improvement", "evidence",
                           "--repo", str(repo), "--database", str(db)], cwd=str(repo),
                          env=env, capture_output=True, text=True, timeout=HANG_SECONDS)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["levers"] == list(ie.LEVERS)
    assert path.read_bytes() == before
    assert store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events


# === S4: Dan's decision applied, rolled back and measured (§7; R59, R68) =======================

GLOBAL = ("# Dan's global defaults\n"
          "roles:\n"
          "  # the reviewer\n"
          "  reviewer:\n"
          "    strength: balanced  # for now\n"
          "  planner:\n"
          "    strength: strong\n"
          "thresholds:\n"
          "  five_h_pause_pct: 90\n")
STRONG = GLOBAL.replace("strength: balanced  # for now", "strength: strong  # for now")


def _sha(data) -> str:
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def _global(repo, monkeypatch, text=GLOBAL):
    path = repo / "maestro-home" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("MAESTRO_GLOBAL_CONFIG", str(path))
    return path


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _maestro(tmp, monkeypatch):
    """A Maestro checkout (one seed commit) the improvement intake points at."""
    root = tmp / "maestro-checkout"
    root.mkdir()
    for args in (("init", "-q", "-b", "master"), ("config", "user.email", "t@example.invalid"),
                 ("config", "user.name", "t"), ("commit", "-q", "--allow-empty", "-m", "seed")):
        _git(root, *args)
    monkeypatch.setenv(imp.INTAKE_ENV, str(root))
    return root


def _stronger(p):
    return _prop(p, lever="model_effort", change="reviewer strength → strong",
                 config={"path": "roles.reviewer.strength", "value": "strong"})


def _stored(repo, ip="IP-0001"):
    return next(r for r in imp.read(repo)["proposals"] if r["id"] == ip)


class Told:
    """The `notify` and `journal` doubles."""

    def __init__(self):
        self.sent, self.seen = [], []

    def notify(self, text):
        self.sent.append(text)

    def journal(self, event, detail):
        self.seen.append((event, detail))

    def events(self):
        return [e for e, _ in self.seen]


def _decide(repo, channel, answer, at, told, ip="IP-0001", **actions):
    """Dan answers `ip`; the poll applies a config lever or queues a code lever for real."""
    channel.answers[f"dq-improve-{ip}"] = answer
    actions.setdefault("apply", lambda row: imp.apply_config(
        repo, row, journal=told.journal, notify=told.notify))
    actions.setdefault("queue", lambda row: imp.queue_intake(
        repo, row, journal=told.journal, notify=told.notify))
    return imp.poll(repo, channel, at, journal=told.journal, **actions)


def _approved_config(repo, db, store, arts, monkeypatch):
    path = _global(repo, monkeypatch)
    _reviewed(store, 4)
    channel, told = Channel(), Told()
    _run(repo, db, store, arts, Analyst(_answer(_stronger)), at=fx.iso(10), channel=channel)
    _decide(repo, channel, "Approve", fx.iso(11), told)
    return path, channel, told


def test_approve_applies_a_config_lever_to_the_global_config_keeping_every_other_byte(
        ledger, monkeypatch):
    repo, db, store, arts = ledger
    path, channel, told = _approved_config(repo, db, store, arts, monkeypatch)
    assert path.read_text(encoding="utf-8") == STRONG
    row = _stored(repo)
    assert row["state"] == "applied"
    applied = row["applied"]
    assert (applied["kind"], applied["at"], applied["path"]) == ("config", fx.iso(11), str(path))
    assert applied["before_text_sha"] == _sha(GLOBAL)
    assert applied["after_text_sha"] == _sha(STRONG)
    backup = Path(applied["backup"])
    assert backup.parent == path.parent / "backups"
    assert backup.name.startswith("config.yaml.IP-0001.")
    assert backup.read_text(encoding="utf-8") == GLOBAL
    assert told.sent == ["✅ IP-0001 applied: roles.reviewer.strength balanced → strong"]
    events = told.events()
    assert events.index("improvement_decided") < events.index("improvement_applied")
    view = {r["path"]: r["value"] for r in ie.config_view(repo)}
    assert view["roles.reviewer.strength"] == "strong"
    # Polling again applies nothing twice.
    _decide(repo, channel, "Approve", fx.iso(12), told)
    assert path.read_text(encoding="utf-8") == STRONG and len(told.sent) == 1


def test_rollback_restores_the_exact_previous_file_and_refuses_when_the_leaf_changed_since(
        ledger, monkeypatch):
    repo, db, store, arts = ledger
    path, channel, told = _approved_config(repo, db, store, arts, monkeypatch)
    imp.rollback(repo, _stored(repo), at=fx.iso(12), journal=told.journal, notify=told.notify)
    assert path.read_bytes() == GLOBAL.encode()
    row = _stored(repo)
    assert row["state"] == "rolled_back" and row["rolled_back"]["how"] == "restored"
    assert told.sent[-1] == "↩️ IP-0001 rolled back: roles.reviewer.strength strong → balanced"
    assert "improvement_rolled_back" in told.events()

    def again(at):
        imp.apply_config(repo, {**_stored(repo), "state": "approved"}, at=at,
                         journal=told.journal, notify=told.notify)
        assert path.read_text(encoding="utf-8").count("strength: strong") == 2

    # Applied again, then another line changed: only the leaf is put back.
    again(fx.iso(13))
    path.write_text(path.read_text(encoding="utf-8").replace("pct: 90", "pct: 80"),
                    encoding="utf-8")
    imp.rollback(repo, _stored(repo), at=fx.iso(14), journal=told.journal)
    assert path.read_text(encoding="utf-8") == GLOBAL.replace("pct: 90", "pct: 80")
    assert _stored(repo)["rolled_back"]["how"] == "leaf"

    # Applied again, then the leaf itself changed: refused, nothing written, Dan told.
    again(fx.iso(15))
    path.write_text(path.read_text(encoding="utf-8").replace(
        "strong  # for now", "light  # for now"), encoding="utf-8")
    before = path.read_bytes()
    out = imp.rollback(repo, _stored(repo), at=fx.iso(16), journal=told.journal,
                       notify=told.notify)
    assert path.read_bytes() == before
    assert _stored(repo)["state"] == "applied"
    assert out["rollback_refused"]["at"] == fx.iso(16)
    for word in ("changed since applied", "IP-0001", "roles.reviewer.strength", "strong",
                 "light"):
        assert word in told.sent[-1], word
    assert "improvement_rollback_refused" in told.events()


def _queued(repo, db, store, arts, monkeypatch):
    maestro = _maestro(repo, monkeypatch)
    _reviewed(store, 4)
    channel, told = Channel(), Told()
    _run(repo, db, store, arts, Analyst(_answer(lambda p: _prop(p))), at=fx.iso(10),
         channel=channel)
    _decide(repo, channel, "Approve", fx.iso(11), told)
    return maestro, told


def test_approve_queues_a_code_lever_as_an_intake_report_in_maestro_s_checkout(
        ledger, monkeypatch):
    from maestro import intake

    repo, db, store, arts = ledger
    maestro, told = _queued(repo, db, store, arts, monkeypatch)
    assert imp.maestro_repo() == maestro
    rid = f"I-{imp.project_key(repo)}-IP-0001"
    row = _stored(repo)
    assert row["state"] == "queued" and row["applied"] is None
    assert row["intake"] == {"id": rid, "at": fx.iso(11), "repo": str(maestro)}
    report = intake.load(maestro, rid)
    assert (report["kind"], report["status"]) == ("change", "captured")
    for part in ("IP-0001", row["change"], row["pattern"]["id"], "missed_earlier",
                 *row["evidence"], "Measure:", "extra_steps_per_task:reviewer/missed_earlier",
                 "Rollback:", row["rollback"], f"Name {rid} in the commit message that ships it"):
        assert part in report["text"], part
    assert told.sent == [f"📮 IP-0001 queued in Maestro's intake ({rid})"]
    assert "improvement_queued" in told.events()
    # A mailbox file, not a commit and so not a release.
    assert _git(maestro, "rev-list", "--count", "HEAD") == "1"
    # Without the variable it is the running checkout's main repository.
    monkeypatch.delenv(imp.INTAKE_ENV)
    common = _git(ROOT, "rev-parse", "--path-format=absolute", "--git-common-dir")
    assert imp.maestro_repo() == Path(common).parent


def test_a_queued_code_lever_is_measured_from_the_first_adopted_release_naming_it(
        ledger, monkeypatch):
    repo, db, store, arts = ledger
    maestro, told = _queued(repo, db, store, arts, monkeypatch)
    rid = f"I-{imp.project_key(repo)}-IP-0001"
    seed = _git(maestro, "rev-parse", "HEAD")

    def land(latest, at):
        return imp.landed(repo, at=at, latest=lambda: latest, journal=told.journal)

    assert land(seed, fx.iso(12)) == [] and _stored(repo)["state"] == "queued"
    # Review L3: a commit naming another project of the same directory name, or an id
    # that only starts with ours, is not ours.
    other = repo / "elsewhere" / repo.name
    other.mkdir(parents=True)
    namesake = f"I-{imp.project_key(other)}-IP-0001"
    assert imp.slug(other) == imp.slug(repo) and namesake != rid
    _git(maestro, "commit", "-q", "--allow-empty", "-m", f"feat: ships {namesake} and {rid}0")
    assert land(_git(maestro, "rev-parse", "HEAD"), fx.iso(12)) == []
    assert _stored(repo)["state"] == "queued"
    _git(maestro, "commit", "-q", "--allow-empty", "-m", f"feat(reviewer): earlier misses ({rid})")
    shipped = _git(maestro, "rev-parse", "HEAD")
    _git(maestro, "commit", "-q", "--allow-empty", "-m", "chore: later")
    release = _git(maestro, "rev-parse", "HEAD")
    # Committed but not adopted yet (or nothing adopted): still queued.
    assert land(seed, fx.iso(13)) == [] and land(None, fx.iso(13)) == []
    [row] = land(release, fx.iso(14))
    assert row["state"] == "applied"
    assert row["applied"] == {"kind": "code", "at": fx.iso(14), "landed": shipped,
                              "release": release}
    assert "improvement_landed" in told.events()
    assert land(release, fx.iso(15)) == []
    # Measured from the landing: 2 accepted tasks since, 5 needed.
    _reviewed(store, 2, start=20)
    out, _ = _run(repo, db, store, arts, Analyst({"proposals": []}), at=fx.iso(30))
    assert out["measured"] == [{"id": "IP-0001", "verdict": "insufficient_data",
                                "tasks": {"after": 2, "needed": 5},
                                "line": "IP-0001: 2 of 5 accepted tasks since applied"}]
    assert _stored(repo)["state"] == "applied" and _stored(repo)["effect"] is None


@pytest.mark.parametrize("answer, state", [("Keep", "kept"), ("Roll back", "rolled_back")])
def test_a_measured_worse_effect_asks_dan_to_roll_back_and_never_rolls_back_itself(
        ledger, monkeypatch, answer, state):
    repo, db, store, arts = ledger
    path = _global(repo, monkeypatch)
    _reviewed(store, 5)
    channel, told = Channel(), Told()
    _run(repo, db, store, arts, Analyst(_answer(_stronger)), at=fx.iso(6), channel=channel)
    _decide(repo, channel, "Approve", fx.iso(7), told)
    # Cheaper and no reviewer misses after, but a defect escaped review.
    for h in range(8, 13):
        fx.add_task(store, f"a{h}", finished_at=fx.iso(h), attempts=fx.work(),
                    causes=[("escaped_defect", "reviewer")] if h == 10 else [])
    out, seen = _run(repo, db, store, arts, Analyst({"proposals": []}), at=fx.iso(20),
                     channel=channel, notify=told.notify)
    row = _stored(repo)
    assert row["state"] == "measured" and row["effect"]["verdict"] == "worse"
    assert [(m["id"], m["verdict"]) for m in out["measured"]] == [("IP-0001", "worse")]
    assert "improvement_measured" in _events(seen)
    qid = "dq-improve-rollback-IP-0001"
    assert row["rollback_question_id"] == qid
    spec = channel.sent[qid]
    assert spec["holds"] is False and spec["task_id"] == "maestro"
    assert [o["label"] for o in spec["spec"]["options"]] == ["Roll back", "Keep"]
    assert spec["spec"]["recommended"] == 0
    assert "worse" in spec["spec"]["question"] and "quality" in spec["spec"]["question"]
    assert path.read_text(encoding="utf-8") == STRONG        # never rolled back by itself
    undone = []

    def undo(r):
        undone.append(r["id"])
        return imp.rollback(repo, r, journal=told.journal, notify=told.notify)

    def poll(at):
        return imp.poll(repo, channel, at, apply=_noop, queue=_noop, undo=undo,
                        journal=told.journal)

    assert poll(fx.iso(21)) == [] and _stored(repo)["state"] == "measured"
    # A later run does not measure it again.
    _run(repo, db, store, arts, Analyst({"proposals": []}), at=fx.iso(22), channel=channel)
    assert _stored(repo)["effect"] == row["effect"]
    channel.answers[qid] = answer
    poll(fx.iso(23))
    assert _stored(repo)["state"] == state
    assert _stored(repo)["rollback_decision"]["answer"] == answer
    if state == "kept":
        assert undone == [] and path.read_text(encoding="utf-8") == STRONG
    else:
        assert undone == ["IP-0001"] and path.read_bytes() == GLOBAL.encode()


def test_the_digest_message_reports_effects_cost_false_holds_time_to_outcome_and_quality(
        ledger, monkeypatch):
    repo, db, store, arts = ledger
    _global(repo, monkeypatch)
    _reviewed(store, 5)
    channel, told = Channel(), Told()
    _run(repo, db, store, arts, Analyst(_answer(_stronger)), at=fx.iso(6), channel=channel,
         notify=told.notify)
    [first] = [t for t in told.sent if t.startswith("📈")]
    assert "New: IP-0001 (asked below)." in first and "Effects" not in first
    _decide(repo, channel, "Approve", fx.iso(7), told)
    told.sent.clear()
    # After: no reviewer misses, but each task's implementer failed a check once.
    for h in range(8, 13):
        fx.add_task(store, f"a{h}", finished_at=fx.iso(h), attempts=fx.work(),
                    causes=[("check_failed", "implementer", 0)])

    def implementer(p):
        return _prop(p, target="implementer", change="the implementer brief names the check",
                     saving=50)

    _run(repo, db, store, arts, Analyst(_answer(implementer, implementer)), at=fx.iso(20),
         channel=channel, notify=told.notify)
    [digest] = [t for t in told.sent if t.startswith("📈")]
    assert digest.startswith(
        f"📈 Maestro improvement — {imp.slug(repo)}, weekly, 1 Oct: 5 accepted tasks.")
    assert "Effects: IP-0001 (reviewer strength → strong): " in digest
    for part in ("cost units/task", "(codex-subscription)", "false holds", "time to outcome",
                 "quality defects/task 0 → 0", "→ improved."):
        assert part in digest, part
    assert "New: IP-0002 (asked below)." in digest
    assert "Not re-sent: role_prompt implementer (already a candidate as IP-0002)." in digest
    assert "Analyst: 1 call, " in digest and "k chars, diagnoser route." in digest
    assert "green" not in digest.lower()
    # No effect, no new proposal and no suppression: nothing is sent.
    told.sent.clear()
    _run(repo, db, store, arts, Analyst({"proposals": []}), at=fx.iso(30), channel=channel,
         notify=told.notify)
    assert told.sent == []


def test_no_improvement_action_changes_an_accepted_verdict(ledger, monkeypatch):
    """R68: attribution, analysis, apply, queue, rollback and landing write no run status,
    registry, ROADMAP, goals store or review artifact."""
    from maestro import goal_store

    repo, db, store, arts = ledger
    _global(repo, monkeypatch)
    maestro = _maestro(repo, monkeypatch)
    fx.registry(repo, ["t1", "t2", "t3", "t4"])
    (repo / "docs").mkdir()
    (repo / "docs" / "ROADMAP.md").write_text("# Roadmap\n\n### next\n", encoding="utf-8")
    fx.add_rows(repo, "episodes", [{"id": "t1:G1", "task": "t1", "goal": "G1",
                                    "impact": "advanced", "at": fx.iso(1)}])
    arts.put_bytes(b'{"verdict": "approved"}', type="node_result")
    _reviewed(store, 4)
    goals = goal_store.path_for(repo)

    def snapshot():
        rows = {table: [tuple(r) for r in store.conn.execute(
            f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("tasks", "task_runs", "attempts", "acceptance_decisions")}
        files = {p.name: p.read_bytes() for p in (
            repo / ".orchestrator" / "completed_tasks.json", repo / "docs" / "ROADMAP.md",
            goals)}
        stored = {str(p): p.read_bytes() for p in
                  sorted((repo / ".orchestrator" / "artifacts").rglob("*")) if p.is_file()}
        return rows, files, stored

    before = snapshot()
    channel, told = Channel(), Told()
    _run(repo, db, store, arts, Analyst(_answer(_stronger, lambda p: _prop(p))), at=fx.iso(10),
         channel=channel)
    assert snapshot() == before
    channel.answers["dq-improve-IP-0002"] = "Approve"
    _decide(repo, channel, "Approve", fx.iso(11), told)
    assert {r["id"]: r["state"] for r in imp.read(repo)["proposals"]} == {
        "IP-0001": "applied", "IP-0002": "queued"}
    assert snapshot() == before
    imp.rollback(repo, _stored(repo), at=fx.iso(12), journal=told.journal)
    imp.landed(repo, at=fx.iso(13), latest=lambda: _git(maestro, "rev-parse", "HEAD"),
               journal=told.journal)
    assert _stored(repo)["state"] == "rolled_back"
    assert snapshot() == before


# === review fixes (M2, M3, M4a, L2, L3) =========================================================


def _measured_worse(repo, db, store, arts, monkeypatch):
    """IP-0001 (reviewer strength → strong) applied, then measured worse; its rollback
    question asked."""
    path = _global(repo, monkeypatch)
    _reviewed(store, 5)
    channel, told = Channel(), Told()
    _run(repo, db, store, arts, Analyst(_answer(_stronger)), at=fx.iso(6), channel=channel)
    _decide(repo, channel, "Approve", fx.iso(7), told)
    for h in range(8, 13):
        fx.add_task(store, f"a{h}", finished_at=fx.iso(h), attempts=fx.work(),
                    causes=[("escaped_defect", "reviewer")] if h == 10 else [])
    _run(repo, db, store, arts, Analyst({"proposals": []}), at=fx.iso(20), channel=channel)
    assert _stored(repo)["state"] == "measured"
    return path, channel, told


def test_a_rollback_that_fails_is_recorded_journaled_and_told_never_lost(ledger, monkeypatch):
    """Review M2: Dan's Roll back is stored before the undo runs; an undo that raises used to
    leave the row `measured` with a decision, so no later poll read it and Dan was never
    told. The failure is now stored (`rollback_failed`), journaled and told."""
    repo, db, store, arts = ledger
    path, channel, told = _measured_worse(repo, db, store, arts, monkeypatch)
    channel.answers["dq-improve-rollback-IP-0001"] = "Roll back"

    def undo(row):
        raise OSError("disk full")

    imp.poll(repo, channel, fx.iso(21), apply=_noop, queue=_noop, undo=undo,
             journal=told.journal, notify=told.notify)
    row = _stored(repo)
    assert row["state"] == "rollback_failed"
    assert row["rollback_failed"] == {"at": fx.iso(21), "error": "OSError: disk full"}
    assert row["rollback_decision"]["resolution"] == "roll_back"
    assert "improvement_rollback_failed" in told.events()
    for word in ("IP-0001", "could not be rolled back", "OSError: disk full",
                 "roles.reviewer.strength"):
        assert word in told.sent[-1], word
    assert path.read_text(encoding="utf-8") == STRONG
    sent = len(told.sent)
    imp.poll(repo, channel, fx.iso(22), apply=_noop, queue=_noop, undo=undo,
             journal=told.journal, notify=told.notify)
    assert len(told.sent) == sent                     # told once
    # Under the same proposal key, a failed rollback is still under way: never re-proposed.
    assert "rollback_failed" in imp.UNDER_WAY


def test_rollback_itself_never_raises_a_missing_backup_is_recorded_and_told(
        ledger, monkeypatch):
    """Review M2, the real undo: `rollback` mirrors `apply_config` — a failure (here the
    backup it would restore is gone) is stored, journaled and told, never raised."""
    repo, db, store, arts = ledger
    path, channel, told = _approved_config(repo, db, store, arts, monkeypatch)
    Path(_stored(repo)["applied"]["backup"]).unlink()
    out = imp.rollback(repo, _stored(repo), at=fx.iso(12), journal=told.journal,
                       notify=told.notify)
    assert out["state"] == "rollback_failed"
    assert out["rollback_failed"]["at"] == fx.iso(12)
    assert out["rollback_failed"]["error"].startswith("FileNotFoundError")
    assert _stored(repo)["state"] == "rollback_failed"
    assert "improvement_rollback_failed" in told.events()
    assert "IP-0001" in told.sent[-1] and "could not be rolled back" in told.sent[-1]
    assert path.read_text(encoding="utf-8") == STRONG


def test_a_stale_approval_never_lowers_a_strength_raised_since_it_was_asked(
        ledger, monkeypatch):
    """Review M3: IP-0001 asked light → balanced; before Dan's Approve the leaf was raised
    to strong. Applying it would lower quality globally: under the config lock the
    effective value is read again and only a strictly weaker one is raised; otherwise the
    approval is refused (`apply_refused`), journaled and told, nothing written."""
    repo, db, store, arts = ledger
    light = GLOBAL.replace("strength: balanced  # for now", "strength: light  # for now")
    path = _global(repo, monkeypatch, light)
    _reviewed(store, 4)
    channel, told = Channel(), Told()
    to_balanced = lambda p: _prop(p, lever="model_effort", change="reviewer strength → balanced",
                                  config={"path": "roles.reviewer.strength",
                                          "value": "balanced"})
    _run(repo, db, store, arts, Analyst(_answer(to_balanced)), at=fx.iso(10), channel=channel)
    assert _stored(repo)["config"]["previous"] == "light"
    path.write_text(STRONG, encoding="utf-8")         # raised by hand before he answered
    before = path.read_bytes()
    _decide(repo, channel, "Approve", fx.iso(11), told)
    assert path.read_bytes() == before
    row = _stored(repo)
    assert row["state"] == "apply_refused" and row["applied"] is None
    assert row["apply_refused"] == {"at": fx.iso(11), "leaf": "roles.reviewer.strength",
                                    "value": "balanced", "now": "strong"}
    assert "improvement_apply_refused" in told.events()
    assert "improvement_applied" not in told.events()
    for word in ("IP-0001", "not applied", "roles.reviewer.strength", "strong", "balanced",
                 "never lowers quality"):
        assert word in told.sent[-1], word
    assert list((path.parent / "backups").glob("*")) == []      # no backup left behind
    # Equal is not stronger either: approving the value already in effect is refused.
    path.write_text(GLOBAL, encoding="utf-8")
    out = imp.apply_config(repo, {**row, "state": "approved"}, at=fx.iso(12),
                           journal=told.journal, notify=told.notify)
    assert out["state"] == "apply_refused" and out["apply_refused"]["now"] == "balanced"
    assert path.read_text(encoding="utf-8") == GLOBAL


def test_rollback_restores_the_bytes_under_the_global_config_lock(ledger, monkeypatch):
    """Review L2: the byte restore takes the lock `set_global_leaf` takes, so a concurrent
    leaf edit cannot land between the sha check and the write."""
    import fcntl

    repo, db, store, arts = ledger
    path, channel, told = _approved_config(repo, db, store, arts, monkeypatch)
    lock = path.parent / f".{path.name}.lock"
    held, real = [], imp._write_atomic

    def write(target, data):
        with lock.open("a") as other:
            try:
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(False)
                fcntl.flock(other, fcntl.LOCK_UN)
            except BlockingIOError:
                held.append(True)
        real(target, data)

    monkeypatch.setattr(imp, "_write_atomic", write)
    imp.rollback(repo, _stored(repo), at=fx.iso(12), journal=told.journal, notify=told.notify)
    assert held == [True]
    assert path.read_bytes() == GLOBAL.encode()
    assert _stored(repo)["rolled_back"]["how"] == "restored"


def test_a_run_that_asks_withdraws_the_unanswered_questions_it_outranks(ledger):
    """Review M4a: an unanswered proposal question holds nothing but stayed pending for
    ever. A run that sends new questions now ranks the still-open ones with its candidates:
    at most MAX_ASKED stay open, an outranked one is withdrawn (its question superseded, the
    row `deferred`, a candidate again) and is later asked under a fresh question id, so a
    stale tap on the old message is never read."""
    repo, db, store, arts = ledger
    _reviewed(store, 4)
    channel, told = Channel(), Told()
    _run(repo, db, store, arts, Analyst(_answer(lambda p: _prop(p, saving=10))),
         at=fx.iso(10), channel=channel)
    assert _stored(repo)["state"] == "asked"
    # Three better proposals: IP-0001 is outranked and withdrawn.
    _reviewed(store, 2, start=20)
    better = [lambda p, l=l, t=t, s=s: _prop(p, lever=l, target=t, saving=s)
              for l, t, s in (("context_skill", "reviewer", 90), ("gate", "", 80),
                              ("graph_order", "", 70))]
    out, seen = _run(repo, db, store, arts, Analyst(_answer(*better)), at=fx.iso(30),
                     channel=channel, notify=told.notify)
    assert out["asked"] == ["IP-0002", "IP-0003", "IP-0004"]
    assert out["superseded"] == ["IP-0001"]
    row = _stored(repo)
    assert row["state"] == "deferred"
    assert row["superseded"] == {"at": fx.iso(30), "by": "IR-0002",
                                 "question_id": "dq-improve-IP-0001"}
    assert channel.superseded == ["dq-improve-IP-0001"]
    assert "improvement_question_superseded" in _events(seen)
    [digest] = [t for t in told.sent if t.startswith("📈")]
    assert "Withdrawn unanswered: IP-0001" in digest
    # A run with nothing new keeps the open three open and sends nothing.
    _reviewed(store, 2, start=40)
    out, _ = _run(repo, db, store, arts, Analyst({"proposals": []}), at=fx.iso(50),
                  channel=channel)
    assert out["asked"] == [] and out["superseded"] == []
    assert {r["id"]: r["state"] for r in imp.read(repo)["proposals"]} == {
        "IP-0001": "deferred", "IP-0002": "asked", "IP-0003": "asked", "IP-0004": "asked"}
    assert len(channel.sent) == 4 and channel.superseded == ["dq-improve-IP-0001"]
    # Dan declines IP-0002; a stale tap on IP-0001's old message is not read.
    channel.answers["dq-improve-IP-0002"] = "Decline"
    channel.answers["dq-improve-IP-0001"] = "Approve"
    _poll(repo, channel, fx.iso(55))
    assert _stored(repo)["state"] == "deferred"
    _reviewed(store, 2, start=60)
    out, _ = _run(repo, db, store, arts, Analyst({"proposals": []}), at=fx.iso(70),
                  channel=channel)
    assert out["asked"] == ["IP-0001"] and out["superseded"] == []
    row = _stored(repo)
    assert (row["state"], row["question_id"]) == ("asked", "dq-improve-IP-0001-IR-0004")
    assert channel.sent["dq-improve-IP-0001-IR-0004"] == imp.question(row)
    _poll(repo, channel, fx.iso(71))
    assert _stored(repo)["state"] == "asked"          # the old answer is not his answer
    channel.answers["dq-improve-IP-0001-IR-0004"] = "Decline"
    _poll(repo, channel, fx.iso(72))
    assert _stored(repo)["state"] == "declined"


def test_a_question_answered_while_the_analyst_runs_is_not_withdrawn_and_its_answer_counts(ledger):
    """Verification review: the loop is blocked during the analyst call, so Dan's tap lands
    only in the question record. Withdrawing the row anyway (state `deferred`) made the next
    poll skip it, and his answer was lost. A question that can no longer be superseded
    (answered) keeps its row open, and the next poll records the answer."""
    class AnsweredMeanwhile(Channel):
        def supersede_question(self, qid):
            if qid in self.answers:
                return False
            return super().supersede_question(qid)

    repo, db, store, arts = ledger
    _reviewed(store, 4)
    channel = AnsweredMeanwhile()
    _run(repo, db, store, arts, Analyst(_answer(lambda p: _prop(p, saving=10))),
         at=fx.iso(10), channel=channel)
    assert _stored(repo)["state"] == "asked"
    _reviewed(store, 2, start=20)
    better = [lambda p, l=l, t=t, s=s: _prop(p, lever=l, target=t, saving=s)
              for l, t, s in (("context_skill", "reviewer", 90), ("gate", "", 80),
                              ("graph_order", "", 70))]

    def analyse_while_dan_answers(system, user):
        channel.answers["dq-improve-IP-0001"] = "Decline"
        return Analyst(_answer(*better))(system, user)

    out, seen = _run(repo, db, store, arts, analyse_while_dan_answers, at=fx.iso(30),
                     channel=channel)
    assert out["asked"] == ["IP-0002", "IP-0003", "IP-0004"]
    assert out["superseded"] == []
    assert _stored(repo)["state"] == "asked" and "superseded" not in _stored(repo)
    assert "improvement_question_superseded" not in _events(seen)
    _poll(repo, channel, fx.iso(35))
    assert _stored(repo)["state"] == "declined"


def test_two_projects_of_the_same_directory_name_get_distinct_intake_ids(tmp_path):
    """Review L3: `I-<slug>-IP-n` collided for two projects named alike; the id now carries
    a short stable hash of the resolved repo path, and intake accepts it."""
    from maestro import intake

    a, b = tmp_path / "one" / "recipes", tmp_path / "two" / "recipes"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    assert imp.slug(a) == imp.slug(b) == "recipes"
    ka, kb = imp.project_key(a), imp.project_key(b)
    assert ka != kb and ka.startswith("recipes-") and imp.project_key(a) == ka
    assert imp.project_key(tmp_path / "one" / ".." / "one" / "recipes") == ka
    ids = [imp.intake_id({"id": "IP-0001", "project": "recipes", "project_key": k})
           for k in (ka, kb)]
    assert ids[0] != ids[1]
    for rid in ids:
        assert intake._ID.fullmatch(rid), rid
