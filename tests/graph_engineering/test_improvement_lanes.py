"""P13 GDT-12 / W12 lanes: improvement proposals through the real graph loop.

A project without `docs/GOALS.md` whose ledger already holds three accepted task runs with
reviewer `missed_earlier` rows (a pattern). One real task runs and is accepted; at queue
exhaustion the loop runs the project-end analysis (one patched analyst call, no new role,
no new dispatch path), asks Dan one question that holds nothing, and applies his answer:
an approved config lever raises `roles.reviewer.strength` in the (tmp) global config; a
declined one is not sent again at the next weekly run without new evidence (§W12 proof 2).
"""
from __future__ import annotations

import json

import maestro.orchestrator as orchestrator
import maestro.state
from maestro import goal_store
from maestro import improvement as imp
from maestro.hitl import telegram as _telegram
from maestro.hitl import verify as _verify
from maestro.workflows import telemetry_capture

from tests import improvement_fixture as fx

from ..test_improvement import _prop
from .test_end_to_end import (
    _CommittingAgent,
    _drive_every_worker_of_any_run,
    _git,
    _git_sandbox,
    _happy_scenario,
    _journal_details,
)
from .test_failure_ladder import _patch_fixture_cli_driver

#: The project-end analysis's clock (`orchestrator._improvement_now`), after the seeded
#: runs and before the real task's real finish time.
T = fx.iso(100)
REVIEWER_KEY = ["reviewer", "missed_earlier", "reviewer"]
QID = "dq-improve-IP-0001"


def _seed(store, *, n=3, start=1, recorded=None) -> None:
    """`n` earlier accepted task runs, each with two reviewer `missed_earlier` rows."""
    for k in range(start, start + n):
        run = fx.add_task(store, f"old-{k}", finished_at=fx.iso(k), attempts=fx.work(reviews=3))
        for i in (2, 3):
            assert telemetry_capture.record_extra_step(
                store, run_id=run, cause="missed_earlier", role="reviewer",
                attempt_id=f"{run}-a{i}", node_id="reviewer", at=recorded or fx.iso(k))


def _sandbox(run) -> None:
    """The project, the seeded ledger, and an (empty) weekly run the day before, so the
    weekly analysis is not due at the idle tick after the task settles and the project-end
    run is the one that reads the seeded rows."""
    _git_sandbox(run)
    _seed(maestro.state.control_store())
    weekly = {"id": "IR-0001", "kind": "weekly", "key": "weekly:2026-10-04",
              "at": fx.iso(76), "window": {"since": None, "until": fx.iso(0)},
              "status": "empty", "tasks": 0, "patterns": [], "proposals": [], "asked": [],
              "suppressed": [], "measured": [], "call": None, "problems": []}
    goal_store.update_file(imp.path_for(run.repo), imp.SECTIONS, "runs",
                           lambda rows: rows + [weekly], version=imp.VERSION)


class _Dan:
    """Dan on Telegram: records what was sent and taps an option of one question."""

    def __init__(self):
        self.sent: list[str] = []
        self.tapped: set[str] = set()

    def send(self, text, markup=None):
        self.sent.append(text)
        return 8000 + len(self.sent)

    def tap(self, req: str, index: int) -> None:
        if req in self.tapped or not (_telegram.QUESTIONS_DIR / f"{req}.json").exists():
            return
        self.tapped.add(req)
        _telegram._handle_danreq_callback(
            {"id": "cq", "data": f"danreq:{req}:{index}", "message": {}})


def _lane(monkeypatch, tmp_path):
    glob = tmp_path / "global-config.yaml"
    glob.write_text("roles:\n  reviewer:\n    strength: balanced\n", encoding="utf-8")
    monkeypatch.setenv("MAESTRO_GLOBAL_CONFIG", str(glob))
    monkeypatch.setenv(imp.INTAKE_ENV, str(tmp_path / "maestro-checkout"))
    _patch_fixture_cli_driver(monkeypatch, _CommittingAgent())
    dan = _Dan()
    monkeypatch.setattr(_verify, "send_via_bot", dan.send)
    monkeypatch.setattr(_verify, "telegram_configured", lambda: True)
    clock = {"now": T}
    monkeypatch.setattr(orchestrator, "_improvement_now", lambda: clock["now"])
    packs: list[dict] = []

    def analyst(system, user):
        """The analyst (one bounded strong-route call in production): a reviewer
        `model_effort` config proposal built from the pack it was shown."""
        pack = json.JSONDecoder().raw_decode(user)[0]
        packs.append(pack)
        return json.dumps({"proposals": [_prop(
            pack, lever="model_effort", change="reviewer strength → strong",
            config={"path": "roles.reviewer.strength", "value": "strong"})]})

    monkeypatch.setattr(orchestrator, "_improvement_analyst", analyst)
    return glob, dan, clock, packs


def _questions(dan) -> list[str]:
    return [t for t in dan.sent if "IP-0001" in t]


def _waits() -> dict:
    return maestro.state.read_state().get(_verify.QUESTIONS_STATE_KEY) or {}


def test_a_ledger_dominated_by_reviewer_missed_earlier_yields_a_reviewer_proposal_dan_approves_and_the_config_changes(
        run_lane, monkeypatch, tmp_path):
    glob, dan, clock, packs = _lane(monkeypatch, tmp_path)
    held: list[dict] = []

    def on_poll(run) -> None:
        events = run.journal_events()
        if "improvement_applied" in events:
            run.halt()
            return
        if "improvement_proposal_asked" in events:
            held.append(_waits())
            dan.tap(QID, 0)
        _drive_every_worker_of_any_run(run)

    run = run_lane(_happy_scenario("gdt12_improvement_approved", before_run=_sandbox,
                                   on_poll=on_poll))

    assert run.returncode == 0
    events = run.journal_events()
    assert "queue_exhausted" in events, _journal_details("graph_run_settled")
    assert events.index("queue_exhausted") < events.index("improvement_run_done") \
        < events.index("improvement_decided") < events.index("improvement_applied")
    [pack] = packs
    assert pack["patterns"][0]["key"] == REVIEWER_KEY
    seeded, stored = imp.read(run.repo)["runs"]
    # The seeded run was empty (no pattern, no call), so it did not move the window (S4b).
    assert seeded["status"] == "empty"
    assert stored["window"] == {"since": None, "until": T}
    assert (stored["kind"], stored["key"], stored["status"]) == (
        "project_end", f"project_end:{_git(run.repo, 'rev-parse', 'HEAD')}", "done")
    [row] = imp.read(run.repo)["proposals"]
    assert (row["lever"], row["target"], row["state"]) == ("model_effort", "reviewer", "applied")
    assert glob.read_text(encoding="utf-8") == "roles:\n  reviewer:\n    strength: strong\n"
    # One question, which never held the loop: no wait was marked, and the queue was
    # called exhausted again after it was asked.
    assert len(_questions(dan)) == 1
    assert held and all(not w for w in held) and not _waits()
    asked = events.index("improvement_proposal_asked")
    assert "queue_exhausted" in events[asked:]


def test_a_declined_proposal_is_not_resent_at_the_next_weekly_run_without_new_evidence(
        run_lane, monkeypatch, tmp_path):
    glob, dan, clock, packs = _lane(monkeypatch, tmp_path)
    # The weekly check at every idle tick (the lane's clock is not wall time).
    monkeypatch.setattr(orchestrator, "IMPROVEMENT_CHECK_S", 0)
    later = fx.iso(100 + 8 * 24)

    def on_poll(run) -> None:
        events = run.journal_events()
        if [r["kind"] for r in imp.read(run.repo)["runs"]].count("weekly") == 2:
            run.halt()
            return
        if "improvement_decided" in events and clock["now"] != later:
            # The same pattern in two tasks finished after the decline, its rows recorded
            # before it: not new evidence. Then a week and a day pass.
            _seed(maestro.state.control_store(), n=2, start=110, recorded=fx.iso(50))
            clock["now"] = later
        elif "improvement_proposal_asked" in events:
            dan.tap(QID, 1)
        _drive_every_worker_of_any_run(run)

    run = run_lane(_happy_scenario("gdt12_improvement_declined", before_run=_sandbox,
                                   on_poll=on_poll))

    assert run.returncode == 0
    events = run.journal_events()
    assert "improvement_decided" in events, _journal_details("graph_run_settled")
    runs = imp.read(run.repo)["runs"]
    assert [(r["kind"], r["status"]) for r in runs] == [
        ("weekly", "empty"), ("project_end", "done"), ("weekly", "done")]
    assert runs[2]["key"] == "weekly:2026-10-13"
    [suppressed] = runs[2]["suppressed"]
    assert suppressed["of"] == "IP-0001" and "declined" in suppressed["reason"]
    assert events.index("improvement_decided") < events.index("improvement_proposal_suppressed")
    # The weekly analyst saw the same pattern, and Dan got no second question.
    assert len(packs) == 2 and packs[1]["patterns"][0]["key"] == REVIEWER_KEY
    assert len(_questions(dan)) == 1
    [row] = imp.read(run.repo)["proposals"]
    assert row["state"] == "declined"
    assert glob.read_text(encoding="utf-8") == "roles:\n  reviewer:\n    strength: balanced\n"
