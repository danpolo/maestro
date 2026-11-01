"""The GDT-14 demonstrator through the real graph loop (P13 goal-driven TDD, R64; `02` §C).

Lane A takes "Recipe Box" (`tests/fixtures/goals/demo/`, helpers in `tests/goals_demo.py`)
from its RED baseline to `complete`; lane B runs a maintenance task that breaks a protected
criterion beside a direct task whose goal conflict goes to Dan, and independent work goes
on while he is asked. Each plan is what `maestro goals tasks` proposes (lane B adds the
hand-written TIDY and the planner's `scope:`), the scripted implementer commits the
fixture's overlays (one per task round, keyed by task id), and everything between is the
product: the gate's checks and goals guard on the real
project, the reviewer handler's rules, `goal_store`/`goal_state`, the completion gate and
its report. Fakes sit only at the agent CLI (`_patch_fixture_cli_driver`), Telegram
(`_DanAnswers`) and ADR-agent boundaries. The provenance bundle is assembled from Maestro's
own durable outputs and git (`goals_demo.provenance`).
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

from maestro import acceptance, goal_completion, orchestrator, taskgraph
from maestro.backends.base import Completion
from maestro.workflows import handlers

from .. import goals_demo as demo
from .test_dispatch_wiring import _create_control_db
from .test_end_to_end import (
    TARGET,
    _briefs,
    _drive_every_worker_of_any_run,
    _git,
    _happy_scenario,
    _journal_details,
    _task,
)
from .test_goals_lanes import (
    _dan_lane,
    _events,
    _GoalReviewingAgent,
    _planner_rows,
    _runs,
    _store,
    _tell,
)

RED, GREEN = acceptance.RED, acceptance.GREEN
_ROLE = re.compile(r"You are the (implementer|reviewer) for task (\S+?)[:.]")
_AC3_TEST = "tests/acceptance/test_search.py"
_AC3_NODE = f"{_AC3_TEST}::test_the_right_recipe_is_in_the_top_3_for_9_of_10_searches"


class _DemoAgent(_GoalReviewingAgent):
    """The scripted agent, keyed by task id and round (never by dispatch order): an
    implementer round commits the next overlay named for its task; a review carries the
    next `(verdict, goal_progress[, {"findings": [...]}])` named for its task. A task the
    loop adds is keyed by the planned task its id extends (a completion-gate round
    `<id>-<rp>`); a GDT-8 diagnosis task writes its cause file and is approved."""

    def __init__(self, overlays: dict[str, list[str]], reviews: dict[str, list[tuple]]):
        super().__init__([])
        self.overlays = {k: list(v) for k, v in overlays.items()}
        self.reviews = {k: list(v) for k, v in reviews.items()}
        self.rounds: Counter = Counter()

    @staticmethod
    def _key(task: str, table: dict) -> str:
        if task in table:
            return task
        return next((k for k in table if task.startswith(f"{k}-")), task)

    def complete(self, spec):
        found = _ROLE.match(spec.prompt)
        if found and found.group(1) == "implementer":
            self.specs.append(spec)
            task = found.group(2)
            self.rounds[task] += 1
            cause = re.search(r"docs/diagnosis/[\w.-]+\.md", spec.prompt)
            if "-diagnose-" in task and cause:
                path = Path(spec.cwd) / cause.group(0)
                path.parent.mkdir(parents=True, exist_ok=True)
                # GDT-12: the first line states the cause its gate check requires.
                path.write_text("Cause: unknown\n\nparse_recipe is still a stub: it reads "
                                "no caption.\n", encoding="utf-8")
                _git(spec.cwd, "add", cause.group(0))
                _git(spec.cwd, "commit", "-q", "-m", "docs: the written cause")
            else:
                demo.apply_overlay(Path(spec.cwd),
                                   self.overlays[self._key(task, self.overlays)].pop(0))
            workspace = Path(re.search(r"^WORKSPACE: (\S+)$", spec.prompt,
                                       re.MULTILINE).group(1))
            handlers.write_sentinel(workspace, handlers.DONE_SENTINEL, "done")
            return Completion(text="done", returncode=0, timed_out=False)
        if found:
            queue = self.reviews.get(self._key(found.group(2), self.reviews))
            self.verdicts = [queue.pop(0) if queue else ("approved", [])]
        return super().complete(spec)

    def briefs(self, task: str) -> list[str]:
        return [p for p in _briefs(self, "implementer")
                if p.startswith(f"You are the implementer for task {task}")]


@pytest.fixture(scope="module")
def plan(tmp_path_factory):
    """Lane A's ROADMAP: what `goals tasks` proposes on the seed's baseline, from a probe
    materialization (the lane writes its ROADMAP before its sandbox exists)."""
    probe = demo.materialize(tmp_path_factory.mktemp("demo-probe") / "recipebox")
    demo.baseline(probe)
    return [_task(t["id"], **t) for t in demo.roadmap(probe, tidy=False)]


def _demo_sandbox(run) -> None:
    """The seed as setup leaves it (plus the lane's ROADMAP), its control DB, and one real
    `goals baseline`: AC-1 RED, AC-2 GREEN, AC-3 BLOCKED, AC-4 unknown, OP-1 Dan's."""
    demo.materialize(run.repo)
    _create_control_db(run)
    states = {cid: r.state for cid, r in demo.baseline(run.repo).items()}
    assert states == {"AC-1": RED, "AC-2": GREEN, "AC-3": acceptance.BLOCKED,
                      "AC-4": acceptance.UNKNOWN, "OP-1": acceptance.OPERATOR}


def _gates(task_id: str) -> list[str]:
    import maestro.state

    rows = maestro.state.control_store().conn.execute(
        "SELECT a.status FROM attempts a JOIN task_runs t ON a.run_id = t.run_id "
        "WHERE a.node_id = 'gate' AND t.task_id = ? ORDER BY a.rowid", (task_id,)).fetchall()
    return [r["status"] for r in rows]


#: G1 "advanced" on a diff alone: the rule stores it `unknown` (C19). (A diff record needs
#: its text; a bare `{"kind": "diff"}` is no evidence item at all.)
_DIFF = {"kind": "diff", "text": "recipebox/parse.py now reads the recipe out of the caption"}
_DIFF_ONLY = [{"goal": "G1", "impact": "advanced", "evidence": [_DIFF]}]
#: The harness moved no criterion, as enabling work is expected not to (C16).
_HARNESS_SAID = [{"goal": "G3", "impact": "no_progress_unexpected", "evidence": [
    {"kind": "test", "criterion": "AC-3", "result": "failed", "id": _AC3_NODE}]}]
_SEARCH_SAID = [{"goal": "G3", "impact": "advanced", "evidence": [
    {"kind": "test", "criterion": "AC-3", "result": "passed", "id": _AC3_NODE}]}]


def _follow_ups(*texts: str) -> dict:
    """A review's extra: its follow-up findings (GDT-11: they wait for the polish stage)."""
    return {"findings": [{"finding": t, "severity": "follow_up"} for t in texts]}


#: Lane A's 4 explicit follow-ups (AC-1's reviewer also gets the rule's "G1 impact unknown"
#: one): two restate the parse_recipe docstring item in other words.
_AC1_FOLLOW_UP = "parse_recipe's docstring does not say that ingredients are lowercased"
_HARNESS_FOLLOW_UP = ("the sample searches rely on lowercased ingredients, which nothing in "
                      "recipebox documents")
_AC3_FOLLOW_UPS = ("rank's docstring does not say that matching ignores case",
                   "parse_recipe lowercases ingredients but does not say so, and rank relies "
                   "on it")
#: Triage's reason for declining the G1 follow-up (overlay `triage-1`).
_MOOT = ("the completion gate reruns AC-1 on HEAD (bench/import_ratio.py): that is G1's "
         "evidence")
#: Where lane A's follow-ups end: (id, raising task, state, of, the stage task holding it).
_W7_ITEMS = [
    ("BL-0001", demo.HARNESS, "done", None, "polish-1-a"),
    ("BL-0002", demo.AC1_TASK, "merged", "BL-0001", "polish-triage-1"),
    ("BL-0003", demo.AC1_TASK, "moot", None, "polish-triage-1"),
    ("BL-0004", demo.AC3_TASK, "done", None, "polish-1-a"),
    ("BL-0005", demo.AC3_TASK, "merged", "BL-0001", "polish-triage-1")]


def _gate_acceptance(repo: Path, task_id: str) -> list[dict]:
    """The criteria each gate attempt of `task_id` reran, with their states."""
    import maestro.state

    rows = maestro.state.control_store().conn.execute(
        "SELECT a.attempt_id FROM attempts a JOIN task_runs t ON a.run_id = t.run_id "
        "WHERE a.node_id = 'gate' AND t.task_id = ? ORDER BY a.rowid", (task_id,)).fetchall()
    inbox = handlers.attempts_root(repo)
    out = []
    for row in rows:
        result = handlers.AttemptInbox(inbox, row["attempt_id"]).result()
        out.append(dict(((result.outputs or {}) if result else {}).get("acceptance") or {}))
    return out


def test_the_demo_goes_from_a_red_baseline_to_complete_with_provenance(
        run_lane, monkeypatch, plan):
    """C6, C8, C9, C11-C16, C19, C25, C30, R56, R64 on Recipe Box. AC-3's enabling harness
    lands with no replan and no planner fault; AC-3's direct task tampers with its
    acceptance test in round 1, the gate's goals guard sends it back, and round 2 restores
    the test and does inner TDD (the unit test committed before the code); AC-1's reviewer
    claims G1 advanced on a diff alone, which the rule stores `unknown`, and the task still
    merges, as each task does while other criteria are RED. At exhaustion the completion
    gate reruns the contract on HEAD (AC-1's metric three times) and asks Dan for OP-1 on
    the final tree; nothing is complete before his Pass, which is recorded at (revision 2,
    HEAD), and then he is told complete once. The provenance bundle, built only from
    Maestro's outputs and git, shows the proof independent of the reviewer and the bound
    tests untouched since the baseline.

    W7 and C29 (GDT-11): the three reviews raise 5 follow-ups (4 explicit, plus the G1
    "impact unknown" one). They wait in the backlog; once the main tasks are done, and
    before the completion gate, triage merges the 2 duplicates and declines the G1 item as
    moot, and exactly one polish task runs. Its round 1 also drops the not-a-recipe check:
    the gate reruns the protected AC-2 (and its area's AC-1 and AC-3), finds it GREEN → RED
    and sends it back; round 2 does both items and merges.

    W12 (GDT-12): the project-end improvement analysis runs once, right after exhaustion,
    on HEAD; with nothing to propose it asks and changes nothing, and the bundle names it."""
    from maestro import backlog

    from .test_end_to_end import _this_maestro_for_gate_checks

    _this_maestro_for_gate_checks(monkeypatch)
    from . import conftest as lane_conftest

    monkeypatch.setattr(lane_conftest, "MAX_POLLS", 40)
    agent = _DemoAgent(
        overlays={demo.HARNESS: ["harness"], demo.AC3_TASK: ["search-tamper", "search"],
                  demo.AC1_TASK: ["import"], "polish-triage-1": ["triage-1"],
                  "polish-1-a": ["polish-break", "polish-1"]},
        reviews={demo.HARNESS: [("approved", _HARNESS_SAID,
                                 _follow_ups(_HARNESS_FOLLOW_UP))],
                 demo.AC3_TASK: [("approved", _SEARCH_SAID, _follow_ups(*_AC3_FOLLOW_UPS))],
                 demo.AC1_TASK: [("approved", _DIFF_ONLY, _follow_ups(_AC1_FOLLOW_UP))]})
    dan = _dan_lane(monkeypatch, agent, [("tap", 0)])          # OP-1: Pass
    told = _tell(monkeypatch)
    before_pass: list[list[str]] = []
    # W12 (GDT-12): the improvement analyses run for real; only the analyst's answer is
    # scripted (no proposal). Each analysis is wrapped to read the outcome report around it.
    monkeypatch.setattr(orchestrator, "_improvement_analyst",
                        lambda system, user: '{"proposals": []}')
    analyses: list[tuple] = []
    real_improvement = orchestrator._graph_improvement

    def improvement(store, artifacts, channel, kind):
        outcome = Path(orchestrator.REPO) / goal_completion.REPORT_JSON

        def read():
            return outcome.read_bytes() if outcome.exists() else None

        before = read()
        record = real_improvement(store, artifacts, channel, kind)
        analyses.append((kind, record, before, read()))
        return record

    monkeypatch.setattr(orchestrator, "_graph_improvement", improvement)

    def on_poll(run) -> None:
        if dan.sent and not before_pass:
            before_pass.append(_journal_details("goal_finish_verdict"))
        dan.answer()
        if "queue_exhausted" in run.journal_events():
            run.halt()
            return
        _drive_every_worker_of_any_run(run)

    run = run_lane(_happy_scenario("e2e_gdt14_demo_lane_a", tasks=plan,
                                   before_run=_demo_sandbox, on_poll=on_poll))

    assert run.returncode == 0
    assert sorted(_runs()) == sorted([(demo.AC1_TASK, "succeeded"),
                                      (demo.HARNESS, "succeeded"),
                                      (demo.AC3_TASK, "succeeded"),
                                      ("polish-triage-1", "succeeded"),
                                      ("polish-1-a", "succeeded")]), \
        _journal_details("graph_run_settled")
    store = _store(run)
    episodes = {e["id"]: e for e in store["episodes"]}

    # C8, C16, C25: the enabling harness makes AC-3 credible (RED on its assertion), its
    # G3 episode is expected, and nothing replans or blames the planner.
    harness = episodes[f"{demo.HARNESS}:G3"]
    assert (harness["contribution"], harness["claimed"], harness["impact"]) == (
        "enabling", "no_progress_unexpected", "unchanged_expected")
    assert _gates(demo.HARNESS) == ["succeeded"]
    assert store["replans"] == []
    assert _planner_rows() == []
    assert not [e for e in _events("ExtraStepObserved") if "planner" in json.dumps(e)]
    [unblocked] = [c for c in store["checkpoints"] if c["task"] == demo.HARNESS]
    assert unblocked["milestones"] == {"enabling_unblocked": ["G3"]}
    assert (unblocked["before"], unblocked["goals"]) == (
        {"G3": {"AC-3": acceptance.BLOCKED}}, {"G3": {"AC-3": RED}})

    # C13: round 1 edits the bound acceptance test; the goals guard fails the gate, the
    # rework brief names the finding, and the test lands exactly as it was at the seed.
    assert _gates(demo.AC3_TASK) == ["failed", "succeeded"]
    first, rework = agent.briefs(demo.AC3_TASK)
    assert "acceptance_test_changed" not in first
    assert f"acceptance_test_changed: {_AC3_TEST} binds AC-3" in rework
    assert _git(run.repo, "show", f"{TARGET}:{_AC3_TEST}") == \
        (demo.PROJECT / _AC3_TEST).read_text(encoding="utf-8").rstrip("\n")

    # C11: inner TDD on the target: the unit test commit before the code commit.
    log = _git(run.repo, "log", "--format=%s", TARGET).splitlines()
    assert log.index("feat: search") < log.index("test: search")
    assert episodes[f"{demo.AC3_TASK}:G3"]["impact"] == "advanced"

    # C19: G1 "advanced" on a diff alone is stored `unknown`, and the task still merges.
    ac1 = episodes[f"{demo.AC1_TASK}:G1"]
    assert (ac1["claimed"], ac1["impact"], ac1["evidence"]) == (
        "advanced", "unknown", [{"kind": "diff"}])
    assert ac1["reason"].endswith("a diff shows an edit, not an outcome")
    assert _gates(demo.AC1_TASK) == ["succeeded"]
    assert len(agent.briefs(demo.AC1_TASK)) == 1

    # C12: tasks merge while mandatory criteria are still RED: the harness graduated with
    # AC-3 RED (above), and AC-1's task converged G1 from a RED AC-1.
    [converged] = [c for c in store["checkpoints"] if c["task"] == demo.AC1_TASK]
    assert converged["milestones"] == {"convergence": ["G1"]}
    assert (converged["before"]["G1"]["AC-1"], converged["goals"]["G1"]["AC-1"]) == (
        RED, GREEN)

    # C30, C9, C15: no verdict is complete before Dan's Pass; his Pass is recorded at
    # (revision 2, HEAD), the verdict is complete and he is told so once.
    head = _git(run.repo, "rev-parse", TARGET)
    assert before_pass and before_pass[0] and all(
        not v.startswith("complete") for v in before_pass[0])
    assert _journal_details("goal_finish_verdict")[-1].startswith("complete")
    [(asked, _)] = dan.sent
    assert "Operator check OP-1" in asked
    [op] = store["operator"]
    assert (op["id"], op["result"], op["revision"], op["commit"]) == ("OP-1", "pass", 2, head)
    [fin] = store["finish"]
    assert (fin["revision"], fin["commit"], fin["done"]) == (2, head, True)
    assert fin["results"]["AC-1"]["values"] == [1.0] * goal_completion.METRIC_RUNS
    assert len([t for t in told if t.startswith("✅ Project complete")]) == 1
    events = run.journal_events()
    assert events.index("goal_finish_signoff") < events.index("queue_exhausted")

    # R56: the report shows each criterion from its baseline to the finish run: AC-1's
    # metric from the measured 0 to GREEN three times over, AC-3 from BLOCKED to GREEN.
    report = (run.repo / goal_completion.REPORT_MD).read_text(encoding="utf-8")
    assert "Verdict: complete" in report
    lines = {x.strip("- ").split(" ", 1)[0]: x.strip("- ") for x in report.splitlines()
             if x.strip("- ").startswith(("AC-1 ", "AC-3 "))}
    assert lines["AC-1"].startswith("AC-1 GREEN — measured 1, 1, 1 (median 1)")
    assert "baseline 0 (`python bench/import_ratio.py`)" in lines["AC-1"]
    assert lines["AC-3"].startswith("AC-3 GREEN") and "baseline BLOCKED" in lines["AC-3"]
    assert f"{demo.AC1_TASK}-followups" not in report

    # W7 (GDT-11, R52): 3 tasks, 5 follow-ups (2 duplicates, 1 moot) end in 1 polish task.
    # No `-followups` task is queued; the stage opens once the main tasks are done.
    # The tasks run one at a time (none declares `scope:`): the harness, AC-1's, AC-3's.
    items = {i["id"]: i for i in backlog.items(run.repo)}
    assert [(i["id"], i["source"]["task"], i["state"], i.get("of"), i["task"])
            for i in items.values()] == _W7_ITEMS
    assert items["BL-0003"]["text"].startswith("Goal G1 impact unknown: gather evidence")
    assert items["BL-0003"]["reason"] == _MOOT
    assert _journal_details("polish_stage_opened") == [
        "1: BL-0001, BL-0002, BL-0003, BL-0004, BL-0005 → polish-triage-1"]
    assert _journal_details("polish_triage_applied") == [
        "Triage (polish stage 1): 5 follow-ups → 2 merged as duplicates, 1 declined as moot "
        f"(BL-0003: \"{_MOOT}\"), 1 polish task: polish-1-a (undeclared scope)."]
    assert _journal_details("polish_items_settled") == [
        "polish-1-a: 2 done, 0 declined, 0 deferred"]
    assert not [d for d in _journal_details("graph_run_settled") if "followups" in d]
    # The triage writer reads the `triage` goals excerpt (every non-goal kept) under its
    # bar; the polish writer the D23 scope rule under its own.
    [triage_brief] = agent.briefs("polish-triage-1")
    assert "TRIAGE TASK (polish stage 1)" in triage_brief
    assert "TRIAGE: polish keeps every hard invariant" in triage_brief
    polish_first, polish_rework = agent.briefs("polish-1-a")
    assert "POLISH TASK (stage 1, area undeclared scope)" in polish_first
    assert "SCOPE (D23)" in polish_first and "TRIAGE:" not in polish_first
    # C29, R28, R41: polish never escapes the invariants. Round 1 breaks AC-2 (INV-1's
    # check); the gate reruns it with every protected criterion and the area's AC-1 and
    # AC-3, and sends it back as a plain regression (a task serving no goal advances none,
    # so nothing is traded and Dan is never asked); round 2 does both items and merges.
    assert _gates("polish-1-a") == ["failed", "succeeded"]
    broke, fixed = _gate_acceptance(run.repo, "polish-1-a")
    assert set(broke) >= {"AC-1", "AC-2", "AC-3"} and broke["AC-2"] == RED
    assert {broke[c] for c in ("AC-1", "AC-3")} == {GREEN}
    assert set(fixed) >= {"AC-1", "AC-2", "AC-3"} and set(fixed.values()) == {GREEN}
    assert "acceptance_regression: AC-2 (G1) was green_already_satisfied" in polish_rework
    assert "hard_invariant_regression" not in polish_rework
    assert all("polish" not in q for q, _ in dan.sent)
    # The finish ran only after polish: the stage, the triage and the settle come first.
    events = run.journal_events()
    order = [e for e in events if e in ("polish_stage_opened", "polish_triage_applied",
                                        "polish_items_settled", "goal_finish_verdict",
                                        "queue_exhausted")]
    assert order[:3] == ["polish_stage_opened", "polish_triage_applied",
                         "polish_items_settled"], order
    assert order[-1] == "queue_exhausted" and "goal_finish_verdict" in order[3:]

    # R64: the provenance bundle, from Maestro's outputs and git only (design §4).
    bundle = demo.provenance(run.repo, lane="A")
    path = demo.export(run.repo, bundle)
    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(json.dumps(bundle))
    assert path == run.repo / demo.PROVENANCE
    assert bundle["schema"] == demo.SCHEMA and bundle["lane"] == "A"
    # 1. The finish ran on the target's HEAD at the contract's revision.
    assert bundle["finish"]["commit"] == head
    assert bundle["finish"]["revision"] == bundle["contract"]["revision"] == 2
    assert bundle["finish"]["verdict"] == "complete"
    assert bundle["contract"]["source"] == demo.CONTRACT_SOURCE
    # 2. Every mandatory criterion met by the finish run; OP-1 passed at (2, HEAD).
    items = bundle["finish"]["items"]
    for cid in ("AC-1", "AC-2", "AC-3"):
        assert (items[cid]["status"], items[cid]["state"], items[cid]["source"]) == (
            "met", GREEN, "finish"), items[cid]
    assert items["AC-1"]["runs"] == goal_completion.METRIC_RUNS
    assert (items["OP-1"]["status"], items["OP-1"]["source"]) == ("met", "operator")
    assert {k: items["OP-1"]["signoff"][k] for k in ("result", "revision", "commit")} == {
        "result": "pass", "revision": 2, "commit": head}
    assert all(i["status"] == "met" for i in items.values())
    # 3. Every bound test predates the baseline and is unchanged since, though round 1 of
    # the search task edited one; the harness's new module beside it is an added file.
    tests = {t["path"]: t for t in bundle["tests"]}
    bound = [t for t in tests.values() if t["kind"] == "binding"]
    assert sorted(t["path"] for t in bound) == [
        "bench/import_ratio.py", "tests/acceptance/test_not_a_recipe.py", _AC3_TEST]
    assert all(t["before_baseline"] and not t["changed_after_baseline"] for t in bound), bound
    added = tests["tests/acceptance/cookbook500.py"]
    assert (added["kind"], added["criteria"], added["before_baseline"]) == (
        "imported", ["AC-3"], False)
    assert bundle["baseline"]["commit"] == _git(run.repo, "rev-list", "--max-parents=0",
                                                TARGET)
    assert {k: v["state"] for k, v in bundle["baseline"]["results"].items()} == {
        "AC-1": RED, "AC-2": GREEN, "AC-3": acceptance.BLOCKED,
        "AC-4": acceptance.UNKNOWN, "OP-1": acceptance.OPERATOR}
    # 4. Which Maestro ran: this checkout in-process, and the one the gate's checks import
    # (the interpreter every acceptance check in the plan names; the root checkout's
    # editable install even in a worktree run, DESIGN §19.12).
    assert bundle["maestro"]["commit"] == demo.git(demo.REPO_ROOT, "rev-parse", "HEAD")
    gate = bundle["maestro"]["gate"]
    plan = taskgraph.scan_tasks(_git(run.repo, "show",
                                     f"{bundle['baseline']['commit']}:docs/ROADMAP.md"))
    checks = [v["cmd"] for t in plan for v in t.get("verifications") or ()
              if "maestro.acceptance" in v["cmd"]]
    assert checks and all(f" {gate['interpreter']} -m maestro.acceptance" in c for c in checks)
    assert (Path(gate["package"]) / "acceptance.py").is_file()
    if Path(gate["root"]) == demo.REPO_ROOT:
        assert gate["commit"] == bundle["maestro"]["commit"]
    # 5. The fixture: its hash is the committed tree's whenever the fixture is clean.
    fixture = bundle["fixture"]
    assert (fixture["sha256"] == demo.tree_sha256(fixture["tree"])) == fixture["clean"]
    # Dan's goal decisions travel with it: lane A trades nothing.
    assert bundle["decisions"] == []
    # W7: where each review follow-up went (the backlog, GDT-11).
    assert [(b["id"], b["source"], b["state"], b["of"], b["task"])
            for b in bundle["backlog"]] == _W7_ITEMS
    assert bundle["backlog"][2]["reason"] == _MOOT
    # 6. The scenario map travels with the evidence.
    assert bundle["scenarios"] == json.loads(json.dumps(demo.C_MAP))
    # 7. The verdict rests on the finish run and Dan's sign-off only; each reviewer claim
    # the rule overrode is recorded: the diff-only G1 claim, and the harness's miss, which
    # enabling work was never expected to move.
    independence = bundle["independence"]
    assert independence["sources"] == ["finish", "operator"]
    assert independence["independent"] is True
    assert [(o["task"], o["goal"], o["claimed"], o["stored"])
            for o in independence["overridden"]] == [
        (demo.AC1_TASK, "G1", "advanced", "unknown"),
        (demo.HARNESS, "G3", "no_progress_unexpected", "unchanged_expected")]
    tasks = {t["id"]: t for t in bundle["tasks"]}
    assert list(tasks) == [demo.AC1_TASK, demo.HARNESS, demo.AC3_TASK]
    assert tasks[demo.AC3_TASK]["gates"] == ["failed", "succeeded"]
    assert [f.split(":", 1)[0] for f in tasks[demo.AC3_TASK]["guard_findings"]] == [
        "acceptance_test_changed"]
    assert tasks[demo.HARNESS]["contribution"] == "enabling"
    assert all(t["runs"] == ["succeeded"] and t["merge_commit"] for t in tasks.values())

    # W12 (GDT-12, R57, R68): right after the queue is exhausted, one project-end analysis
    # on HEAD (a weekly one may have run at an earlier idle tick: runs are counted by kind).
    # The analyst proposes nothing, so Dan is asked nothing and told nothing new, and the
    # outcome report is byte-identical around every analysis.
    from maestro import improvement

    stored = improvement.read(run.repo)["runs"]
    kinds = Counter(r["kind"] for r in stored)
    assert kinds["project_end"] == 1 and kinds["weekly"] <= 1 and set(kinds) <= {
        "project_end", "weekly"}, stored
    [end] = [r for r in stored if r["kind"] == "project_end"]
    assert end["key"] == f"project_end:{_git(run.repo, 'rev-parse', 'HEAD')}"
    assert end["status"] in ("empty", "done") and end["proposals"] == [], end
    assert all(r["status"] in ("empty", "done") for r in stored), stored
    assert improvement.read(run.repo)["proposals"] == []
    assert not [q for q, _ in dan.sent if "IP-0" in q]
    assert len([t for t in told if t.startswith("✅ Project complete")]) == 1
    [(_, end_record, report_before, report_after)] = [
        a for a in analyses if a[0] == "project_end"]
    assert end_record["id"] == end["id"] and report_before is not None
    assert all(before == after for _, _, before, after in analyses)
    events = run.journal_events()
    assert events.index("goal_finish_verdict") < events.index("queue_exhausted")
    # The bundle carries the analyses: each run's kind, status, tasks and proposals.
    assert bundle["improvement"] == [
        {"kind": r["kind"], "status": r["status"], "tasks": r["tasks"],
         "proposals": r["proposals"]} for r in stored]


# --- Lane B: a maintenance regression reworks; a goal conflict goes to Dan ------------------

#: Lane B's contract: the demo's, plus AC-4 bound to its cost model (same revision 2).
_B_CONTRACT = demo.bind(demo.contract_text(), "AC-4", demo.AC4_TEST)
#: The earlier work lane B starts from, committed on `main` before its baseline.
_B_EARLIER = ("harness", "search")
#: The write sets the planner declares (D20). `goals tasks` proposes no `scope:` (it cannot
#: know the files), and a task without one overlaps every live run, so it runs alone: two
#: tasks run side by side only when both declare disjoint scopes.
_B_SCOPES = {demo.AC1_TASK: ["recipebox/parse.py"], demo.TIDY_ID: ["recipebox/classify.py"]}


@pytest.fixture(scope="module")
def plan_b(tmp_path_factory):
    """Lane B's ROADMAP: on the seed with AC-3's work landed and AC-4 bound, `goals tasks`
    proposes only AC-1's direct task; the hand-written TIDY rides beside it. The planner
    declares each one's write set, so they may run at once."""
    probe = demo.materialize(tmp_path_factory.mktemp("demo-probe-b") / "recipebox",
                             contract=_B_CONTRACT, overlays=_B_EARLIER)
    demo.baseline(probe)
    tasks = demo.roadmap(probe)
    assert [t["id"] for t in tasks] == [demo.AC1_TASK, demo.TIDY_ID]
    assert all("scope" not in t for t in tasks)
    return [_task(t["id"], **t, scope=_B_SCOPES[t["id"]]) for t in tasks]


def _lane_b_sandbox(run) -> None:
    """AC-3's harness and search committed, AC-4 bound: AC-1 RED, AC-2/AC-3/AC-4 GREEN
    (AC-4 measures 0.6 s), OP-1 Dan's."""
    demo.materialize(run.repo, contract=_B_CONTRACT, overlays=_B_EARLIER)
    _create_control_db(run)
    found = demo.baseline(run.repo)
    assert {cid: r.state for cid, r in found.items()} == {
        "AC-1": RED, "AC-2": GREEN, "AC-3": GREEN, "AC-4": GREEN,
        "OP-1": acceptance.OPERATOR}
    assert found["AC-4"].value == pytest.approx(0.6)


def _journal() -> list[tuple[str, str]]:
    import maestro.state

    path = maestro.state.JOURNAL
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
               if line.strip()]
    return [(r.get("event", ""), str(r.get("detail", ""))) for r in records]


def _attempt_count(task_id: str) -> int:
    import maestro.state

    return maestro.state.control_store().conn.execute(
        "SELECT COUNT(*) FROM attempts a JOIN task_runs t ON a.run_id = t.run_id "
        "WHERE t.task_id = ?", (task_id,)).fetchone()[0]


def _settled(task_id: str) -> bool:
    return any(t == task_id and s != "running" for t, s in _runs())


#: AC-1's reviewer: G1 advanced on AC-1's measurement, and G3 regressed on AC-4's, the
#: trade Dan already answered at the gate (C24: not asked again at review).
_IMPORT_SAID = [
    {"goal": "G1", "impact": "advanced", "evidence": [
        {"kind": "metric", "name": "import_ratio", "value": 1.0, "baseline": 0.0,
         "criterion": "AC-1"}]},
    {"goal": "G3", "impact": "regressed", "evidence": [
        {"kind": "metric", "name": "search_latency", "value": 1.4, "baseline": 0.6,
         "criterion": "AC-4"}]}]


def test_the_demo_s_maintenance_regression_reworks_and_its_goal_conflict_goes_to_dan(
        run_lane, monkeypatch, plan_b):
    """C7, C12, C18, C21, C22, C24, C28 on Recipe Box. Two independent tasks run together: AC-1's
    direct task (the caption parser) and TIDY, a `serves: []` refactor of the classifier.
    TIDY's round 1 drops the not-a-recipe check: the gate reruns the protected AC-2, finds
    it GREEN → RED and INV-1 (checked by AC-2) broken, and sends it back with no question;
    round 2 is a pure refactor and merges. AC-1's parser turns AC-1 GREEN but grows AC-4's
    cost model 0.6 → 1.4 s: G1 advanced, G3 regressed, so the gate asks Dan once (G1 ranks
    above G3: accepting is recommended). While that question waits, TIDY runs to its merge;
    only then does Dan tap Accept. The decision stores the measured bound, the rework's gate
    is covered by it, the reviewer's G3 claim is not asked again, and both tasks merge. A
    fresh `maestro goals status` process then reads it all back from disk."""
    from ..test_cli import _run_cli

    # The lane's subprocess double leaves each run's `code_base_sha` empty, so the gates
    # take `main` as the base, and TIDY's merge moves it under the AC-1 run: the scope
    # check and the goals guard count only the task's own commits from the fork point
    # (GDT-14 review M2b; this lane needed a real recorded base before that fix).
    agent = _DemoAgent(
        overlays={demo.AC1_TASK: ["import", "import-2"],
                  demo.TIDY_ID: ["tidy-break", "tidy"]},
        reviews={demo.AC1_TASK: [("approved", _IMPORT_SAID)],
                 demo.TIDY_ID: [("approved", [])]})
    dan = _dan_lane(monkeypatch, agent, [("tap", 1)])          # the recommended Accept
    asked: list[int] = []        # TIDY's attempts when Dan's question went out

    def on_poll(run) -> None:
        if dan.sent and not asked:
            asked.append(_attempt_count(demo.TIDY_ID))
        if _settled(demo.TIDY_ID):          # Dan answers only once TIDY has merged
            dan.answer()
        if _settled(demo.AC1_TASK) and _settled(demo.TIDY_ID):
            run.halt()
            return
        _drive_every_worker_of_any_run(run)

    run = run_lane(_happy_scenario("e2e_gdt14_demo_lane_b", tasks=plan_b,
                                   before_run=_lane_b_sandbox, on_poll=on_poll))

    assert run.returncode == 0
    assert sorted(_runs()) == sorted([(demo.AC1_TASK, "succeeded"),
                                      (demo.TIDY_ID, "succeeded")]), \
        _journal_details("graph_run_settled")

    # C21: the gate asks Dan once: G3's AC-4 for G1, accepting recommended by priority.
    [(text, markup)] = dan.sent
    assert "Decision needed" in text and "G3" in text and "AC-4" in text
    assert "Accept G3 worse by up to +" in text
    assert [b["text"] for row in markup["inline_keyboard"] for b in row] == ["1", "2 ⭐"]
    # ... and independent work continues while it waits: TIDY's later attempts start after
    # the question went out, and TIDY settles before Dan's answer is read.
    journal = _journal()
    order = [e for e, d in journal if e in ("dan_question_asked", "dan_question_answered")
             or (e == "graph_run_settled" and d.startswith(f"{demo.TIDY_ID} "))]
    assert order == ["dan_question_asked", "graph_run_settled", "dan_question_answered"], \
        journal
    assert asked and asked[0] < _attempt_count(demo.TIDY_ID)

    # C22, C24: the decision keeps the measured bound; the rework's gate is covered and the
    # reviewer's G3 claim is not asked again: one question in all.
    [decision] = _store(run)["decisions"]
    assert (decision["resolution"], decision["regressed"], decision["criteria"]) == (
        "accept", "G3", ["AC-4"])
    assert decision["advanced"] == ["G1"]
    assert decision["bound"] == pytest.approx(100 * (1.4 - 0.6) / 0.6, abs=0.5)
    assert len(_journal_details("dan_question_asked")) == 1
    assert len(_journal_details("dan_question_answered")) == 1
    assert _gates(demo.AC1_TASK) == ["failed", "succeeded"]
    first, rework = agent.briefs(demo.AC1_TASK)
    assert "Accept G3 worse by up to +" in rework

    # C18, C12: TIDY serves no goal, yet its break of AC-2, the check of hard invariant
    # INV-1, reworks it, and is never a question. With no goal advanced there is no trade
    # to refuse, so GDT-6's regression finding is the block (`goal_conflicts.judge`); the
    # `hard_invariant_regression` wording is for a change that advanced another goal (C23:
    # `tests/test_goals_demo.py::test_a_change_that_advances_g1_but_breaks_inv_1_...`).
    assert _gates(demo.TIDY_ID) == ["failed", "succeeded"]
    tidy_first, tidy_rework = agent.briefs(demo.TIDY_ID)
    assert "acceptance_regression" not in tidy_first
    assert "acceptance_regression: AC-2 (G1) was green_already_satisfied" in tidy_rework
    assert "hard_invariant_regression" not in tidy_rework
    assert [(i.id, i.checked_by) for i in demo.goals.load(run.repo).invariants] == [
        ("INV-1", ("AC-2",))]
    assert all(demo.TIDY_ID not in q for q, _ in dan.sent)
    log = _git(run.repo, "log", "--format=%s", TARGET).splitlines()
    assert {"feat: tidy", "feat: import-2"} <= set(log)
    assert _git(run.repo, "show", f"{TARGET}:recipebox/classify.py") == (
        demo.OVERLAYS / "tidy" / "recipebox" / "classify.py").read_text(
        encoding="utf-8").rstrip("\n")

    # C28: a fresh process reads the goals' progress back from disk: revision 2; AC-1 GREEN
    # from the AC-1 task's checkpoint on the merged tree (a commit on main); AC-4 RED from
    # its gate under Dan's accept; AC-2 still protected; the decision listed; and no done
    # verdict (that is only the completion gate's).
    shown = _run_cli("goals", "status", str(run.repo), "--json")
    assert shown.returncode == 0, shown.stderr
    status = json.loads(shown.stdout)
    assert status["revision"] == status["baseline"]["revision"] == 2
    assert status["baseline"]["stale"] is False
    by_id = {c["id"]: c for c in status["criteria"]}
    ac1 = by_id["AC-1"]
    assert (ac1["state"], ac1["source"], ac1["task"]) == (GREEN, "checkpoint", demo.AC1_TASK)
    assert ac1["commit"] and demo._ok(run.repo, "merge-base", "--is-ancestor", ac1["commit"],
                                      TARGET) == 0
    ac4 = by_id["AC-4"]
    assert (ac4["state"], ac4["source"]) == (RED, "gate")
    assert ac4["decisions"] == [{"id": decision["id"], "resolution": "accept"}]
    assert (by_id["AC-2"]["state"], by_id["AC-2"]["protected"]) == (GREEN, True)
    assert [d["id"] for d in status["decisions"]] == [decision["id"]]
    assert "AC-1" not in status["unmet"] and "OP-1" in status["unmet"]
    assert "done" not in status


# --- Lane C: a failed finish works again and polishes again, then asks at the cap -----------

#: Lane C's contract: the demo's (AC-4 unbound, so the parser's growth trades nothing),
#: with a stop policy that names no number, so `thresholds.goal_finish_rounds` is the cap
#: (the demo's says "two attempts").
_C_CONTRACT = demo.contract_text().replace("two attempts miss the same criterion",
                                           "an approach keeps missing the same criterion")
#: The parser's local cache: gitignored, so absent from every task worktree and left behind
#: on the controller's checkout, where the completion gate reruns AC-1.
_CACHE = ".recipe-cache"


#: Lane C's ROADMAP: only the hand-written TIDY (with its declared write set); no task
#: names AC-1, so the completion gate is what adds AC-1's work. (A planned direct AC-1 task
#: whose criterion stays RED on the merged tree is GDT-8's milestone gap at its own
#: graduation, before any exhaustion.)
_C_PLAN = [_task(demo.TIDY_ID, **{**demo.TIDY, "scope": _B_SCOPES[demo.TIDY_ID]})]


def _lane_c_sandbox(run) -> None:
    """The seed with AC-3's harness and search committed (AC-1 RED, AC-2 and AC-3 GREEN)
    under `_C_CONTRACT`, and a stale parser cache on the controller: AC-1 turns GREEN in
    each task worktree (its gate check passes), yet stays RED on the final tree (as
    `test_goals_lanes._stale_sandbox`)."""
    assert _C_CONTRACT != demo.contract_text()
    demo.materialize(run.repo, contract=_C_CONTRACT, overlays=_B_EARLIER)
    with (run.repo / ".gitignore").open("a", encoding="utf-8") as handle:
        handle.write(f"{_CACHE}\n")
    demo.git(run.repo, "commit", "-qam", "ignore the parser's local cache")
    (run.repo / _CACHE).write_text("stale\n", encoding="utf-8")
    _create_control_db(run)
    states = {cid: r.state for cid, r in demo.baseline(run.repo).items()}
    assert states["AC-1"] == RED and states["AC-2"] == GREEN


def test_a_failed_finish_works_again_and_polishes_again_then_asks_at_the_cap(
        run_lane, monkeypatch):
    """C31, R55 on Recipe Box (GDT-11). With AC-3's work landed and AC-1 RED, TIDY alone
    lands; its review raises 1 follow-up, which goes straight to polish-1-a (nothing to
    triage) before the completion gate runs. The gate finds AC-1 RED and adds round 1 of 1
    (`thresholds.goal_finish_rounds: 1`): AC-1's corrective task. Its parser reads every
    caption in its worktree, so its gate check passes, but a stale local cache on the
    controller keeps AC-1 RED on the merged tree, so GDT-8 adds a missing task and then
    a diagnosis task, which run first. The corrective task's own follow-up opens polish
    stage 2 (polish-2-a) at the next exhaustion, again before the gate. At the cap Dan is
    asked once; he taps Keep going and the project stays blocked: no third stage, and the
    queue is never called exhausted."""
    from maestro import backlog, goal_replan

    from .test_dispatch_wiring import GRAPH
    from .test_end_to_end import _this_maestro_for_gate_checks

    _this_maestro_for_gate_checks(monkeypatch)
    agent = _DemoAgent(
        overlays={demo.TIDY_ID: ["tidy"], demo.AC1_TASK: ["import-miss", "import-miss-2"],
                  "polish-1-a": ["polish-c1"], "polish-2-a": ["polish-c2"]},
        reviews={demo.TIDY_ID: [("approved", [], _follow_ups(
                     "is_recipe's docstring does not say which caption lines it reads"))],
                 demo.AC1_TASK: [("approved", [], _follow_ups(
                     "parse_recipe's docstring does not mention the local cache"))]})
    dan = _dan_lane(monkeypatch, agent, [("tap", 2)])          # Keep going
    from . import conftest as lane_conftest

    monkeypatch.setattr(lane_conftest, "MAX_POLLS", 60)
    monkeypatch.setattr(orchestrator, "_judge_complete", lambda *a, **k: "")
    told = _tell(monkeypatch)
    after: list[int] = []

    def on_poll(run) -> None:
        dan.answer()
        if any(r.get("status") == "decided" for r in _store(run)["replans"]
               if (r.get("trigger") or {}).get("kind") == "finish_gap"):
            if len(after) >= 2:
                run.halt()
                return
            after.append(run.polls)
        _drive_every_worker_of_any_run(run)

    run = run_lane(_happy_scenario(
        "e2e_gdt11_demo_lane_c", tasks=_C_PLAN,
        project_yaml={**GRAPH, "thresholds": {"goal_finish_rounds": 1}},
        before_run=_lane_c_sandbox, on_poll=on_poll))

    assert run.returncode == 0
    finish = [r for r in _store(run)["replans"]
              if (r.get("trigger") or {}).get("kind") == "finish_gap"]
    assert [r["status"] for r in finish] == ["applied", "decided"], finish
    assert finish[1]["decision"]["resolution"] == "keep_going"
    [corrective] = [t for t in finish[0]["applied"] if t.startswith(f"{demo.AC1_TASK}-")]
    ran = _runs()
    order_ran = [t for t, _ in ran]
    for tid in (demo.TIDY_ID, "polish-1-a", corrective, "polish-2-a"):
        assert (tid, "succeeded") in ran, ran
    assert order_ran.index(demo.TIDY_ID) < order_ran.index("polish-1-a") \
        < order_ran.index(corrective) < order_ran.index("polish-2-a"), ran
    # GDT-8's own work (the missing task, the diagnosis) runs before the stage opens.
    gdt8 = [t for t in order_ran if t.startswith(f"{demo.AC1_TASK}-") and t != corrective
            or "-diagnose-" in t]
    assert gdt8 and all(order_ran.index(t) < order_ran.index("polish-2-a") for t in gdt8)
    # One follow-up per stage: no triage; each settled by its polish task.
    assert _journal_details("polish_stage_opened") == ["1: BL-0001 → polish-1-a",
                                                       "2: BL-0002 → polish-2-a"]
    assert not _journal_details("polish_triage_applied")
    assert [(i["id"], i["source"]["task"], i["state"], i["task"])
            for i in backlog.items(run.repo)] == [
        ("BL-0001", demo.TIDY_ID, "declined", "polish-1-a"),
        ("BL-0002", corrective, "declined", "polish-2-a")]
    # R55: polish, the finish round, polish again, then the cap question; nothing after.
    journal = _journal()
    order = [(e, d.split(":", 1)[0]) for e, d in journal
             if e in ("polish_stage_opened", "goal_finish_round", "goal_finish_asked",
                      "queue_exhausted")]
    assert [e for e, _ in order] == ["polish_stage_opened", "goal_finish_round",
                                     "polish_stage_opened", "goal_finish_asked"], order
    assert "queue_exhausted" not in run.journal_events()
    [(text, _)] = dan.sent
    assert "completion gate" in text and goal_replan.KEEP_GOING in text
    assert _journal_details("goal_finish_verdict")[-1].startswith("blocked")
    assert any(t.startswith("🔁 Not done yet — incomplete (round 1 of 1)") for t in told)
    assert not any(t.startswith("✅") for t in told)
