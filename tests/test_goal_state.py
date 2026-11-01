"""Durable goal-progress state: what the goals have reached, read back by a fresh process
(P13 GDT-9, R35, C28).

The goals store (`.orchestrator/goal_decisions.json`, version 2) also keeps `criteria`
(one observation per criterion and contract revision: the gate's reruns of an accepted
task and the milestone checkpoints) and `contract` (each legal contract revision seen).
`goal_state.results` is the acceptance baseline overlaid by newer observations at the
current revision only (R67); `protected` is every criterion GREEN at the baseline or
observed GREEN since, and never shrinks; `progress` is what `maestro goals status` prints:
revision, baseline, each criterion's state with its source, what is unmet, Dan's decisions
and replans, what is held and why, the next work, what turned stale (R11), contract
problems (an id seen earlier and silently removed, GDT-1-FORMAT) and how to reproduce
it, and never a done verdict (GDT-10's). Requirements: R11, R35, R67, C28
(`docs/agent-plans/p13-goal-driven-tdd/`).
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from maestro import acceptance, goal_state as gs, goal_store
from maestro.docs import goals

from .test_goals_contract import V2

GREEN, RED = acceptance.GREEN, acceptance.RED
BEFORE = "2026-10-18T08:00:00+0000"
DAY = "2026-10-18T09:00:00+00:00"
LATER = "2026-10-18T11:00:00+00:00"
#: V2 with AC-02 bound too (AC-01 already is).
BOUND = V2.replace(
    "- **Layer:** e2e\n- **Evidence:** test\n\n### AC-03",
    "- **Layer:** e2e\n- **Evidence:** test\n"
    "- **Test:** `pytest tests/acceptance/test_import.py::test_refused`\n\n### AC-03")
#: A legal revision 3 of BOUND: AC-03's oracle changed, with its history row.
REV3 = BOUND.replace(
    "Contract revision: 2 (approved 2026-10-17 by Dan)",
    "Contract revision: 3 (approved 2026-10-18 by Dan)").replace(
    "- **Oracle:** exact equality per category",
    "- **Oracle:** equal per category to the shekel").replace(
    "| 2 | 2026-10-17 | G3 target 15 s → 10 s | 15 s felt slow | AC-04 | Dan | rev 1 G3 target |",
    "| 2 | 2026-10-17 | G3 target 15 s → 10 s | 15 s felt slow | AC-04 | Dan | rev 1 G3 target |\n"
    "| 3 | 2026-10-18 | AC-03 oracle to the shekel | statements round | AC-03 | Dan | "
    "rev 2 AC-03 oracle |")
BASELINE = {"AC-01": GREEN, "AC-02": RED, "AC-03": acceptance.BLOCKED,
            "AC-04": acceptance.UNKNOWN, "OP-01": acceptance.OPERATOR}


def _repo(tmp_path: Path, text: str = BOUND) -> tuple[Path, goals.Goals]:
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs" / "GOALS.md").write_text(text, encoding="utf-8")
    return tmp_path, goals.load(tmp_path)


def _baseline(repo: Path, states=None, *, revision: int = 2, ran_at: str = BEFORE) -> None:
    """The stored baseline, as `maestro goals baseline` writes it, at a fixed time."""
    path = acceptance.path_for(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    results = [acceptance.Result(id=cid, state=state, reason="baseline", revision=revision)
               for cid, state in (BASELINE if states is None else states).items()]
    path.write_text(json.dumps({"revision": revision, "format": 2, "ran_at": ran_at,
                                "results": [asdict(r) for r in results]}), encoding="utf-8")


def _gate(repo, doc, states, *, task="T1", at=DAY, snapshot="abc1234def"):
    return gs.record_gate(repo, doc, {"acceptance": dict(states), "snapshot": snapshot},
                          task=task, run_id=f"run-{task}", at=at)


def _states(repo, doc=None) -> dict[str, str]:
    return {r.id: r.state for r in gs.results(repo, doc)}


def _keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


# --- the store (R35, R62) ----------------------------------------------------------------------


def test_a_store_without_version_reads_and_every_section_is_kept(tmp_path):
    """A GDT-8 file (no `version`) reads as before, with the new sections empty; the first
    write stamps version 2 and keeps every section and any key a newer maestro wrote."""
    path = goal_store.path_for(tmp_path)
    path.parent.mkdir(parents=True)
    old = {"decisions": [{"id": "GC-1"}], "episodes": [{"id": "T1:G2"}],
           "checkpoints": [{"id": "CP-1"}], "replans": [{"id": "RP-1"}],
           "later": {"kept": True}}
    path.write_text(json.dumps(old), encoding="utf-8")
    store = goal_store.read(tmp_path)
    assert store["criteria"] == [] and store["contract"] == []
    assert {k: store[k] for k in old} == old
    goal_store.update(tmp_path, "criteria", lambda rows: [*rows, {"id": "AC-01"}])
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["version"] == goal_store.VERSION == 2
    assert {k: saved[k] for k in old} == old
    assert saved["criteria"] == [{"id": "AC-01"}] and saved["contract"] == []


def test_recording_criteria_keeps_decisions_episodes_checkpoints_replans(tmp_path):
    repo, doc = _repo(tmp_path)
    _baseline(repo)
    for section in ("decisions", "episodes", "checkpoints", "replans"):
        goal_store.update(repo, section, lambda rows, s=section: [*rows, {"id": s}])
    _gate(repo, doc, {"AC-01": GREEN, "AC-02": GREEN})
    _gate(repo, doc, {"AC-02": GREEN}, task="T2", at=LATER, snapshot="fed4321")
    store = goal_store.read(repo)
    for section in ("decisions", "episodes", "checkpoints", "replans"):
        assert store[section] == [{"id": section}]
    rows = {r["id"]: r for r in store["criteria"]}
    assert sorted(rows) == ["AC-01", "AC-02"]             # one row per id and revision
    assert {k: rows["AC-02"][k] for k in ("id", "revision", "state", "source", "at", "task",
                                           "run_id", "commit", "command")} == {
        "id": "AC-02", "revision": 2, "state": GREEN, "source": "gate", "at": LATER,
        "task": "T2", "run_id": "run-T2", "commit": "fed4321",
        "command": "pytest tests/acceptance/test_import.py::test_refused"}


# --- results: the baseline overlaid by newer observations (R35, R67, C28) -----------------------


def test_the_latest_observation_at_the_current_revision_wins_over_the_baseline(tmp_path):
    repo, doc = _repo(tmp_path)
    _baseline(repo)
    assert _states(repo, doc)["AC-02"] == RED and gs.sources(repo, doc) == {}
    _gate(repo, doc, {"AC-01": GREEN, "AC-02": GREEN})
    states = _states(repo, doc)
    assert (states["AC-01"], states["AC-02"], states["AC-03"]) == (
        GREEN, GREEN, acceptance.BLOCKED)
    label = gs.sources(repo, doc)["AC-02"]
    assert "gate" in label and "T1" in label and "abc1234" in label
    gs.record_checkpoint(repo, doc, {"id": "CP-1", "at": LATER, "task": "T2",
                                     "run_id": "run-T2", "revision": 2,
                                     "goals": {"G1": {"AC-02": RED}}, "skipped": {},
                                     "unknown": []}, commit="0123456789")
    assert _states(repo, doc)["AC-02"] == RED
    assert "checkpoint" in gs.sources(repo, doc)["AC-02"]


def test_a_baseline_rerun_after_an_observation_wins(tmp_path):
    repo, doc = _repo(tmp_path)
    _baseline(repo)
    _gate(repo, doc, {"AC-02": GREEN})
    _baseline(repo, ran_at=LATER)                     # `maestro goals baseline` again
    assert _states(repo, doc)["AC-02"] == RED
    assert "AC-02" not in gs.sources(repo, doc)


def test_an_observation_at_an_older_revision_closes_nothing_and_is_listed_stale(tmp_path):
    repo, doc = _repo(tmp_path)
    _baseline(repo)
    goal_store.update(repo, "criteria", lambda rows: [*rows, {
        "id": "AC-02", "revision": 1, "state": GREEN, "source": "gate", "at": LATER,
        "task": "T0", "run_id": "run-T0", "commit": "old", "command": "", "reason": ""}])
    goal_store.update(repo, "episodes", lambda rows: [*rows, {"id": "T0:G1", "revision": 1}])
    assert _states(repo, doc)["AC-02"] == RED
    assert "AC-02" not in gs.protected(repo, doc)
    stale = gs.progress(repo, doc, [])["stale"]
    assert {"id": "AC-02", "revision": 1} in [
        {k: o[k] for k in ("id", "revision")} for o in stale["observations"]]
    assert stale["episodes"] == ["T0:G1"]


def test_a_skipped_checkpoint_criterion_is_no_observation(tmp_path):
    repo, doc = _repo(tmp_path)
    _baseline(repo)
    rows = gs.record_checkpoint(repo, doc, {
        "id": "CP-1", "at": DAY, "task": "T1", "run_id": "run-T1", "revision": 2,
        "goals": {"G1": {"AC-01": GREEN}}, "skipped": {"G1": ["AC-02"]},
        "unknown": [{"criterion": "AC-02", "goal": "G1", "state": acceptance.UNKNOWN,
                     "reason": "not rerun: the checkpoint's 600 s cap was spent"}]})
    assert [(r["id"], r["source"]) for r in rows] == [("AC-01", "checkpoint")]
    assert [r["id"] for r in goal_store.read(repo)["criteria"]] == ["AC-01"]
    assert _states(repo, doc)["AC-02"] == RED


def test_no_current_baseline_gives_no_results_but_status_shows_the_observations(tmp_path):
    repo, doc = _repo(tmp_path)
    _baseline(repo, revision=1)
    _gate(repo, doc, {"AC-02": GREEN})
    assert gs.results(repo, doc) == [] and gs.protected(repo, doc) == set()
    shown = gs.progress(repo, doc, [])
    assert shown["revision"] == 2
    assert (shown["baseline"]["revision"], shown["baseline"]["stale"]) == (1, True)
    by_id = {c["id"]: c for c in shown["criteria"]}
    assert (by_id["AC-02"]["state"], by_id["AC-02"]["source"]) == (GREEN, "gate")
    assert by_id["AC-01"]["state"] is None
    assert "AC-01" in shown["unmet"] and "AC-02" not in shown["unmet"]


def test_protected_adds_observed_green_and_never_shrinks_on_an_accepted_regression(tmp_path):
    """C12, C22: a criterion an earlier task turned GREEN is protected like a baseline GREEN
    one; a regression Dan accepted is observed RED but stays protected (GDT-7's bound)."""
    repo, doc = _repo(tmp_path)
    _baseline(repo)
    assert gs.protected(repo, doc) == {"AC-01"}
    _gate(repo, doc, {"AC-01": GREEN, "AC-02": GREEN})
    assert gs.protected(repo, doc) == {"AC-01", "AC-02"}
    _gate(repo, doc, {"AC-01": RED, "AC-02": RED}, task="T2", at=LATER)
    assert gs.protected(repo, doc) == {"AC-01", "AC-02"}
    assert (_states(repo, doc)["AC-01"], _states(repo, doc)["AC-02"]) == (RED, RED)


# --- progress (R35, C28) ------------------------------------------------------------------------


def test_progress_lists_unmet_decisions_replans_holds_and_next_work(tmp_path):
    repo, doc = _repo(tmp_path)
    _baseline(repo)
    goal_store.update(repo, "decisions", lambda rows: [*rows, {
        "id": "GC-1", "resolution": "accept", "criteria": ["AC-04"], "regressed": "G3",
        "at": DAY}])
    goal_store.update(repo, "replans", lambda rows: [*rows, {
        "id": "RP-1", "goal": "G2", "status": "asked", "question_id": "dq-replan-RP-1",
        "trigger": {"kind": "repeated_misses"}}])
    tasks = [
        {"id": "T2", "status": "open", "serves": ["G1"], "contribution": "direct",
         "criteria": ["AC-02"]},
        {"id": "T3", "status": "open", "serves": ["G2"], "contribution": "direct",
         "criteria": ["AC-03"]},
        {"id": "T4", "status": "open", "serves": ["G3"], "contribution": "enabling",
         "criteria": ["AC-04"]},
        {"id": "T9", "status": "open", "serves": ["G9"], "contribution": "direct",
         "criteria": ["AC-02"]},
    ]
    shown = gs.progress(repo, doc, tasks)
    assert shown["unmet"] == ["AC-02", "AC-03", "AC-04", "OP-01"]
    by_id = {c["id"]: c for c in shown["criteria"]}
    assert by_id["AC-04"]["decisions"] == [{"id": "GC-1", "resolution": "accept"}]
    assert (by_id["AC-01"]["source"], by_id["AC-01"]["protected"]) == ("baseline", True)
    assert by_id["OP-01"]["automated"] is False
    assert [d["id"] for d in shown["decisions"]] == ["GC-1"]
    [replan] = shown["replans"]
    assert (replan["id"], replan["goal"], replan["status"]) == ("RP-1", "G2", "asked")
    holds = {h["task"]: h["reason"] for h in shown["holds"]}
    assert sorted(holds) == ["T3", "T9"]
    assert "RP-1" in holds["T3"] and "G9" in holds["T9"]
    assert shown["next_work"] == [{"task": "T2", "criteria": ["AC-02"]},
                                  {"task": "T4", "criteria": ["AC-04"]}]
    assert shown["reproduce"][0] == "maestro goals baseline"
    assert "AC-01: pytest tests/acceptance/test_import.py::test_valid" in shown["reproduce"]
    assert shown["store_problem"] == "" and shown["contract_problems"] == []


def test_progress_never_reports_done(tmp_path):
    """C28: every automated criterion GREEN is not "done": the completion verdict is
    GDT-10's, and an operator check is never an automated pass (R14)."""
    repo, doc = _repo(tmp_path)
    _baseline(repo)
    _gate(repo, doc, {"AC-01": GREEN, "AC-02": GREEN, "AC-03": GREEN, "AC-04": GREEN})
    shown = gs.progress(repo, doc, [])
    assert shown["unmet"] == ["OP-01"]
    assert not _keys(shown) & {"done", "complete", "completed", "finished", "verdict"}
    assert "done" not in gs.render(shown).lower()


# --- the contract (R04, R11) --------------------------------------------------------------------


def test_an_id_seen_at_an_earlier_revision_and_removed_silently_is_a_gate_problem(tmp_path):
    """GDT-1-FORMAT: ids seen at earlier revisions are kept, so one that vanishes without
    being retired is caught even after the revision that retired it moved on."""
    from maestro import cli

    from .test_goals import _project

    repo = _project(tmp_path, goals_md=BOUND, tasks=[])
    doc = goals.load(repo)
    older = BOUND.replace("Contract revision: 2", "Contract revision: 1").replace(
        "- INV-01: no entry is ever stored twice (checked by AC-01)",
        "- INV-01: no entry is ever stored twice (checked by AC-01)\n"
        "- INV-02: an import never deletes an entry")
    goal_store.update(repo, "contract", lambda rows: [
        {"revision": 1, "sha256": "x", "text": older, "at": DAY},
        {"revision": 2, "sha256": "y", "text": BOUND, "at": LATER}])
    problems = gs.contract_problems(repo, doc)
    assert any("INV-02" in p and "retired" in p for p in problems), problems
    assert gs.progress(repo, doc, [])["contract_problems"] == problems
    check = cli._check_goals(repo)
    assert check.required is True and "contract: " in check.detail and "INV-02" in check.detail


def test_an_illegal_edit_keeps_failing_until_it_is_fixed(tmp_path):
    repo, doc = _repo(tmp_path)
    assert gs.observe_contract(repo, doc) is True
    edited = BOUND.replace("- **Oracle:** exact equality per category",
                           "- **Oracle:** equal per category to the shekel")
    (repo / "docs" / "GOALS.md").write_text(edited, encoding="utf-8")
    for _ in range(2):
        assert gs.observe_contract(repo, goals.load(repo)) is False
        assert [r["revision"] for r in goal_store.read(repo)["contract"]] == [2]
        assert "AC-03 changed without a new contract revision" in gs.contract_problems(
            repo, goals.load(repo))
    (repo / "docs" / "GOALS.md").write_text(BOUND, encoding="utf-8")
    assert gs.contract_problems(repo, goals.load(repo)) == []


def test_a_legal_revision_lists_stale_evidence_and_tasks(tmp_path):
    """R11: Dan's revision 3 changes AC-03; what it touches is stale, and so is every
    observation, episode and open task resting on it."""
    repo, doc = _repo(tmp_path)
    _baseline(repo)
    assert gs.observe_contract(repo, doc) is True
    _gate(repo, doc, {"AC-01": GREEN})
    goal_store.update(repo, "episodes", lambda rows: [*rows, {"id": "T1:G2", "revision": 2}])
    (repo / "docs" / "GOALS.md").write_text(REV3, encoding="utf-8")
    rev3 = goals.load(repo)
    assert gs.observe_contract(repo, rev3) is True
    assert [r["revision"] for r in goal_store.read(repo)["contract"]] == [2, 3]
    tasks = [{"id": "T2", "status": "open", "serves": ["G1"], "contribution": "direct",
              "criteria": ["AC-02"]},
             {"id": "T3", "status": "open", "serves": ["G2"], "contribution": "direct",
              "criteria": ["AC-03"]}]
    shown = gs.progress(repo, rev3, tasks)
    assert shown["revision"] == 3 and shown["baseline"]["stale"] is True
    stale = shown["stale"]
    assert {"AC-03", "G2", "S-01"} <= set(stale["ids"])
    assert stale["tasks"] == ["T3"]
    assert [o["id"] for o in stale["observations"]] == ["AC-01"]
    assert stale["episodes"] == ["T1:G2"]
    assert shown["contract_problems"] == []
    assert gs.results(repo, rev3) == []           # nothing proven at revision 3 yet


# --- corruption and release safety --------------------------------------------------------------


def test_a_corrupt_store_is_reported_by_progress_and_moved_aside_only_by_a_writer(tmp_path):
    repo, doc = _repo(tmp_path)
    _baseline(repo)
    path = goal_store.path_for(repo)
    path.write_text("{not json", encoding="utf-8")
    shown = gs.progress(repo, doc, [])
    assert shown["store_problem"] and str(goal_store.STORE) in shown["store_problem"]
    assert _states(repo, doc)["AC-02"] == RED                  # the baseline still reads
    assert path.read_text(encoding="utf-8") == "{not json"     # a reader never moves it
    _gate(repo, doc, {"AC-02": GREEN})
    [aside] = [p for p in path.parent.iterdir()
               if p.name.startswith("goal_decisions.json.corrupt-")]
    assert aside.read_text(encoding="utf-8") == "{not json"
    assert gs.progress(repo, doc, [])["store_problem"] == ""


def test_without_goals_md_nothing_is_read_or_written(tmp_path):
    assert gs.progress(tmp_path) is None
    assert gs.results(tmp_path) == [] and gs.protected(tmp_path) == set()
    assert gs.record_gate(tmp_path, None, {"acceptance": {"AC-01": GREEN}, "snapshot": "a"},
                          task="T1", run_id="r", at=DAY) == []
    assert gs.record_checkpoint(tmp_path, None, {"goals": {"G1": {"AC-01": GREEN}}}) == []
    assert gs.observe_contract(tmp_path) is False
    assert gs.contract_problems(tmp_path) == []
    assert not (tmp_path / ".orchestrator").exists()


# --- GDT-9 review ---------------------------------------------------------------------------------

NOON = "2026-10-18T10:00:00+00:00"
#: BOUND with a scoped open question on AC-04 (R05/R66: it holds only what it names).
ASKED = BOUND.replace("- **Threshold:** 10 seconds", "- **Open question:** median or p95?")


def _roadmap_project(tmp_path, text=BOUND):
    """A doctor-ready project: BOUND, its baseline, and the tasks setup proposes from it."""
    from maestro import goal_roadmap

    from .test_goals import _project

    repo, doc = _repo(tmp_path, text)
    _baseline(repo)
    _project(tmp_path, goals_md=text, tasks=goal_roadmap.propose_tasks(doc, gs.results(repo, doc)))
    return repo, doc


def test_a_gate_that_cannot_run_a_criterion_keeps_its_credible_state(tmp_path):
    """Review M1: the gate's sandbox is another environment than the controller's. A
    BLOCKED/UNKNOWN there (the guard already made it a note) is no observation: it never
    replaces the baseline's GREEN, so the doctor gives no false FAIL and briefs keep GREEN."""
    from maestro import cli

    repo, doc = _roadmap_project(tmp_path)
    assert "AC-01" not in cli._check_goals(repo).detail
    rows = _gate(repo, doc, {"AC-01": acceptance.UNKNOWN, "AC-02": acceptance.BLOCKED})
    assert rows == [] and goal_store.read(repo)["criteria"] == []
    assert _states(repo, doc)["AC-01"] == GREEN and gs.sources(repo, doc) == {}
    assert "AC-01" not in cli._check_goals(repo).detail
    _gate(repo, doc, {"AC-02": GREEN}, task="T2")
    _gate(repo, doc, {"AC-02": acceptance.UNKNOWN}, task="T3", at=LATER)
    assert _states(repo, doc)["AC-02"] == GREEN                 # the credible row stays
    assert goal_store.read(repo)["criteria"][0]["task"] == "T2"


def test_a_contract_with_a_scoped_open_question_is_snapshotted(tmp_path):
    """Review M2: an open question blocks only what it names (R05, R66), so it does not keep
    the contract from its snapshot: a later silent removal fails, and a legal revision made
    while the question is still open is recorded."""
    repo, doc = _repo(tmp_path, ASKED)
    assert any("open question" in p.text for p in doc.gate)
    assert gs.observe_contract(repo, doc) is True
    removed = ASKED.replace(
        "- TO-01: import may take up to 12 seconds while G1's new bank is added; decision D-7; "
        "bound 12 seconds; expires when the bank adapter lands\n", "")
    (repo / "docs" / "GOALS.md").write_text(removed, encoding="utf-8")
    assert "TO-01 removed without being retired" in gs.contract_problems(repo, goals.load(repo))
    rev3 = REV3.replace("- **Threshold:** 10 seconds", "- **Open question:** median or p95?")
    (repo / "docs" / "GOALS.md").write_text(rev3, encoding="utf-8")
    assert gs.observe_contract(repo, goals.load(repo)) is True
    assert [r["revision"] for r in goal_store.read(repo)["contract"]] == [2, 3]


def test_a_contract_with_a_section_level_open_question_is_snapshotted(tmp_path):
    """GDT-9 verification L1: an open question under Authority (no ids) blocks nothing it
    names, like a scoped one (R05, R66), so it does not keep the contract from its snapshot
    either; a missing revision still does."""
    text = BOUND.replace("## Authority\n\n",
                         "## Authority\n\n- **Open question:** who may waive a trade-off?\n")
    repo, doc = _repo(tmp_path, text)
    assert [p.text for p in doc.gate if not p.ids] == [
        "Authority: open question for Dan: who may waive a trade-off?"]
    assert gs.observe_contract(repo, doc) is True
    assert [r["revision"] for r in goal_store.read(repo)["contract"]] == [2]


def test_observe_contract_skips_format_one_and_a_contract_with_a_whole_contract_problem(tmp_path):
    """Review L2 (deviation 6): no snapshot of a format-1 contract or of one with a gate
    problem on the whole contract (its revision or history), so every snapshot is legal."""
    from .test_goals import SKILL_STYLE

    repo, _ = _repo(tmp_path, SKILL_STYLE)
    assert gs.observe_contract(repo) is False
    unhistoried = BOUND.replace("Contract revision: 2", "Contract revision: 3")
    assert "contract revision 3 has no change-history row" in [
        p.text for p in goals.parse(unhistoried).gate]
    (repo / "docs" / "GOALS.md").write_text(unhistoried, encoding="utf-8")
    assert gs.observe_contract(repo) is False
    assert not goal_store.path_for(repo).exists()


def test_a_gate_tree_at_an_older_revision_files_its_result_under_that_revision(tmp_path):
    """Review L2 (deviation 2): the gate ran on a commit whose `docs/GOALS.md` is revision 1;
    Dan's revision 2 landed before the graduation. The result is revision 1's: it closes
    nothing at revision 2 (R67) and is listed stale."""
    import subprocess

    older = BOUND.replace("Contract revision: 2", "Contract revision: 1")
    repo, _ = _repo(tmp_path, older)

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                              text=True).stdout.strip()

    git("init", "-q", "-b", "main")
    git("-c", "user.email=t@example.com", "-c", "user.name=t", "add", "-A")
    git("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "rev 1")
    commit = git("rev-parse", "HEAD")
    (repo / "docs" / "GOALS.md").write_text(BOUND, encoding="utf-8")
    doc = goals.load(repo)
    _baseline(repo)
    [row] = _gate(repo, doc, {"AC-02": GREEN}, snapshot=commit)
    assert row["revision"] == 1
    assert _states(repo, doc)["AC-02"] == RED and "AC-02" not in gs.protected(repo, doc)
    assert [o["id"] for o in gs.progress(repo, doc, [])["stale"]["observations"]] == ["AC-02"]


def test_an_id_retired_and_then_dropped_from_retired_ids_is_a_gate_problem(tmp_path):
    """Review L3: the reachable silent removal. INV-02 is in revision 2, retired at
    revision 3, and revision 4 deletes its Retired-ids entry: `changes(3, 4)` cannot see
    it, the snapshots can."""
    from maestro import cli

    from .test_goals import _project

    row2 = "| 2 | 2026-10-17 | G3 target 15 s → 10 s | 15 s felt slow | AC-04 | Dan | rev 1 G3 target |"
    row3 = "| 3 | 2026-10-18 | retire INV-02 | imports are append-only | INV-02 | Dan | rev 2 INV-02 |"
    row4 = "| 4 | 2026-10-18 | tidy the retired ids | housekeeping | none | Dan | rev 3 |"
    inv2 = "\n- INV-02: an import never deletes an entry"
    retired = "\n- INV-02 (rev 3): imports are append-only now."
    g4 = "- G4 (rev 2): CSV export of the ledger is a non-goal now."
    rev2 = BOUND.replace("(checked by AC-01)", "(checked by AC-01)" + inv2)
    rev3 = rev2.replace(inv2, "").replace(
        "Contract revision: 2 (approved 2026-10-17 by Dan)",
        "Contract revision: 3 (approved 2026-10-18 by Dan)").replace(
        row2, f"{row2}\n{row3}").replace(g4, g4 + retired)
    rev4 = rev3.replace(retired, "").replace(
        "Contract revision: 3 (approved", "Contract revision: 4 (approved").replace(
        row3, f"{row3}\n{row4}")
    repo = _project(tmp_path, goals_md=rev2, tasks=[])
    for text in (rev2, rev3, rev4):
        (repo / "docs" / "GOALS.md").write_text(text, encoding="utf-8")
        doc = goals.load(repo)
        assert [p.text for p in doc.gate] == [], (doc.revision, doc.gate)
        assert gs.observe_contract(repo, doc) is True
    assert [r["revision"] for r in goal_store.read(repo)["contract"]] == [2, 3, 4]
    problems = gs.contract_problems(repo, goals.load(repo))
    assert problems == ["INV-02 was in the contract at revision 2 and is gone without being "
                        "retired"]
    check = cli._check_goals(repo)
    assert check.required is True and "contract: INV-02" in check.detail


def test_an_accepted_regression_reads_red_and_the_doctor_fails_until_dan_s_revision(tmp_path):
    """Review L4: Dan accepted AC-01's regression (GC-1). The gate's RED is the truth now
    (R30), so the doctor's required FAIL names AC-01 until his revision lands; AC-01 stays
    protected (GDT-7's bound)."""
    from maestro import cli

    repo, doc = _roadmap_project(tmp_path)
    goal_store.update(repo, "decisions", lambda rows: [*rows, {
        "id": "GC-1", "resolution": "accept", "criteria": ["AC-01"], "regressed": "G1",
        "at": DAY}])
    _gate(repo, doc, {"AC-01": RED})
    assert _states(repo, doc)["AC-01"] == RED and "AC-01" in gs.protected(repo, doc)
    check = cli._check_goals(repo)
    assert check.required is True
    assert "AC-01 is red_expected_missing_behavior and no open task names it" in check.detail
    by_id = {c["id"]: c for c in gs.progress(repo, doc)["criteria"]}
    assert by_id["AC-01"]["decisions"] == [{"id": "GC-1", "resolution": "accept"}]


def test_goals_baseline_survives_a_failed_contract_snapshot(tmp_path, monkeypatch, capsys):
    """Review L5: the baseline is stored first; a store that cannot be written for the
    snapshot is a warning, not a traceback."""
    from maestro import cli

    repo, doc = _repo(tmp_path)
    monkeypatch.setattr(acceptance, "run_baseline", lambda repo, doc, timeout: [
        acceptance.Result(id=c.id, state=GREEN, reason="passed", revision=2)
        for c in doc.criteria])

    def boom(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(gs, "observe_contract", boom)
    cli._goals_baseline(repo / "docs" / "GOALS.md", as_json=False, timeout=5)
    assert acceptance.path_for(repo).is_file()
    err = capsys.readouterr().err
    assert "contract snapshot" in err and "read-only file system" in err


def test_a_re_graduation_keeps_the_gate_row_s_first_time(tmp_path):
    """Review L6: the repair re-graduation of the same accepted run records the same gate
    observation again; it keeps its first `at`, so a baseline rerun since still wins."""
    repo, doc = _repo(tmp_path)
    _baseline(repo)
    _gate(repo, doc, {"AC-02": GREEN}, at=DAY)
    _baseline(repo, ran_at=NOON)
    assert _states(repo, doc)["AC-02"] == RED
    _gate(repo, doc, {"AC-02": GREEN}, at=LATER)                # same task, run and commit
    assert goal_store.read(repo)["criteria"][0]["at"] == DAY
    assert _states(repo, doc)["AC-02"] == RED
    _gate(repo, doc, {"AC-02": GREEN}, task="T2", at=LATER, snapshot="fed4321")
    assert _states(repo, doc)["AC-02"] == GREEN                 # another run is new evidence


def _state_file(repo, parked):
    path = repo / ".orchestrator" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"parked_tasks": list(parked)}), encoding="utf-8")


def test_next_work_is_only_work_that_can_start(tmp_path):
    """Review L8 (R35 "next safe work"): a parked task, or one whose dependency is open or
    parked, is not next."""
    repo, doc = _repo(tmp_path)
    _baseline(repo)
    _state_file(repo, ["T2"])
    tasks = [
        {"id": "T2", "status": "open", "serves": ["G1"], "contribution": "direct",
         "criteria": ["AC-02"]},
        {"id": "T4", "status": "open", "serves": ["G3"], "contribution": "direct",
         "criteria": ["AC-04"], "deps": ["T8"]},
        {"id": "T5", "status": "open", "serves": ["G3"], "contribution": "direct",
         "criteria": ["AC-04"], "deps": ["T2"]},
        {"id": "T6", "status": "open", "serves": ["G3"], "contribution": "enabling",
         "criteria": ["AC-04"]},
        {"id": "T8", "status": "open", "serves": ["G1"], "contribution": "direct",
         "criteria": ["AC-02"]},
        {"id": "T1", "status": "complete", "serves": ["G2"], "contribution": "direct",
         "criteria": ["AC-03"]},
        {"id": "T9", "status": "open", "serves": ["G2"], "contribution": "direct",
         "criteria": ["AC-03"], "deps": ["T1"]},
    ]
    assert gs.progress(repo, doc, tasks)["next_work"] == [
        {"task": "T6", "criteria": ["AC-04"]}, {"task": "T8", "criteria": ["AC-02"]},
        {"task": "T9", "criteria": ["AC-03"]}]


def test_a_hold_on_a_parked_replan_task_says_it_needs_unpark(tmp_path):
    """Review L8 (GDT-8 L1's wording): the task a replan added is parked, so the work
    waiting for it waits on Dan's `/unpark`, not on the task."""
    repo, doc = _repo(tmp_path)
    _baseline(repo)
    _state_file(repo, ["T7"])
    goal_store.update(repo, "replans", lambda rows: [*rows, {
        "id": "RP-1", "goal": "G2", "status": "applied", "applied": ["T7"], "waits": ["T3"],
        "trigger": {"kind": "repeated_misses"}}])
    tasks = [{"id": "T3", "status": "open", "serves": ["G2"], "contribution": "direct",
              "criteria": ["AC-03"]},
             {"id": "T7", "status": "open", "serves": ["G2"], "contribution": "enabling",
              "criteria": ["AC-03"]}]
    shown = gs.progress(repo, doc, tasks)
    [hold] = shown["holds"]
    assert hold["task"] == "T3"
    assert "T7 is parked" in hold["reason"] and "/unpark T7" in hold["reason"]
    assert shown["next_work"] == []


# --- the completion gate's sections (GDT-10) --------------------------------------------------


def test_the_finish_and_operator_sections_read_empty_and_are_kept(tmp_path):
    """GDT-10 adds `finish` (each verification run at a contract revision and commit) and
    `operator` (Dan's operator-check sign-offs) to the version-2 store: a store without
    them reads `[]`, and a writer of another section keeps them."""
    repo, _ = _repo(tmp_path)
    path = goal_store.path_for(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 2, "decisions": [], "criteria": []}),
                    encoding="utf-8")
    stored = goal_store.read(repo)
    assert stored["finish"] == [] and stored["operator"] == []
    goal_store.update(repo, "finish", lambda rows: [*rows, {"id": "FIN-1", "done": True}])
    goal_store.update(repo, "operator", lambda rows: [*rows, {"id": "OP-01", "result": "pass"}])
    goal_store.update(repo, "decisions", lambda rows: [*rows, {"id": "GC-1"}])
    stored = goal_store.read(repo)
    assert stored["finish"] == [{"id": "FIN-1", "done": True}]
    assert stored["operator"] == [{"id": "OP-01", "result": "pass"}]
    assert stored["decisions"] == [{"id": "GC-1"}]


def test_a_finish_run_is_an_observation_labelled_finish(tmp_path):
    """The completion gate's run is stored like a checkpoint's (`record_finish`): one
    observation per criterion at the run's revision, source `finish`, on its commit; a
    BLOCKED result is an observation too (the run is the controller's own)."""
    repo, doc = _repo(tmp_path)
    _baseline(repo)
    row = {"id": "FIN-abcd1234", "revision": 2, "commit": "c0ffee1234567890", "done": True,
           "results": {"AC-02": {"state": GREEN, "reason": "passed", "at": LATER},
                       "AC-03": {"state": acceptance.BLOCKED, "reason": "ImportError",
                                 "at": LATER}}}
    written = gs.record_finish(repo, doc, row)
    assert {r["id"] for r in written} == {"AC-02", "AC-03"}
    assert "finish" in gs.SOURCES
    assert all(r["source"] == "finish" and r["commit"] == "c0ffee1234567890"
               and r["run_id"] == "FIN-abcd1234" and r["revision"] == 2 for r in written)
    assert _states(repo, doc)["AC-02"] == GREEN
    by_id = {c["id"]: c for c in gs.progress(repo, doc, [])["criteria"]}
    assert (by_id["AC-02"]["source"], by_id["AC-02"]["commit"]) == ("finish",
                                                                    "c0ffee1234567890")
    assert "AC-02" in gs.protected(repo, doc)
    # It reads as the finish run it was, not as a task named "finish" (GDT-14 S2 caveat b).
    assert by_id["AC-02"]["reason"].startswith(
        "observed by the finish run FIN-abcd1234 (c0ffee123456)"), by_id["AC-02"]["reason"]
    assert "after finish" not in by_id["AC-02"]["reason"]
