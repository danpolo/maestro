"""The W11b completion gate: done only when the contract is met at this revision and commit
(P13 GDT-10).

At queue exhaustion (and `maestro goals finish`) `goal_completion.evaluate` reruns every
mandatory acceptance criterion, and every criterion a hard invariant names, on the
controller's checkout at (contract revision, HEAD), in budgeted slices that resume across
polls and restarts. Only that run is final evidence (R24, R67): gate observations,
checkpoints, review episodes and older revisions close nothing. The verdict is
`complete` (every mandatory criterion, operator check, scenario, goal and invariant met),
`incomplete` (the loop can still act: one `finish_gap` replan per goal through GDT-8's
path, capped) or `blocked` (Dan, or an outside change). Operator checks are Dan's Pass at
this revision and commit (R14), asked only on the final tree; finish-stage goal conflicts
go through GDT-7's API (R47); the outcome report says what was proved and how (R56).
Requirements: R14, R21, R24, R25, R47, R53-R56, R67 (`docs/agent-plans/p13-goal-driven-tdd/`).
The lanes are in `tests/graph_engineering/test_goals_lanes.py`.
"""
from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest

from maestro import acceptance, goal_completion as gc, goal_conflicts, goal_replan as gr
from maestro import goal_state, goal_store
from maestro.docs import goals

from .test_goals import SKILL_STYLE
from .hang_bound import HANG_SECONDS
from .test_goals_contract import V2

GREEN, RED = acceptance.GREEN, acceptance.RED
BLOCKED, UNKNOWN, OPERATOR = acceptance.BLOCKED, acceptance.UNKNOWN, acceptance.OPERATOR
AT = "2026-10-18T12:00:00+00:00"

VALID = "pytest tests/acceptance/test_import.py::test_valid"
REFUSED = "pytest tests/acceptance/test_import.py::test_refused"
TOTALS = "pytest tests/acceptance/test_totals.py::test_match"
BENCH = "python tests/acceptance/bench.py"

#: V2 with every criterion bound: AC-02 and AC-03 to pytest nodes, AC-04 (a metric below
#: 10 seconds) to a measurement script.
FULL = V2.replace(
    "- **Layer:** e2e\n- **Evidence:** test\n\n### AC-03",
    f"- **Layer:** e2e\n- **Evidence:** test\n- **Test:** `{REFUSED}`\n\n### AC-03").replace(
    "- **Layer:** integration\n- **Evidence:** test\n",
    f"- **Layer:** integration\n- **Evidence:** test\n- **Test:** `{TOTALS}`\n").replace(
    "- **Layer:** e2e\n- **Evidence:** metric\n",
    f"- **Layer:** e2e\n- **Evidence:** metric\n- **Test:** `{BENCH}`\n")
#: FULL with OP-01 optional: complete needs no sign-off.
NO_OP = FULL.replace("- **Sign-off:** Dan\n", "- **Sign-off:** Dan\n- **Mandatory:** no\n")
#: NO_OP with AC-03 optional.
AC03_OPTIONAL = NO_OP.replace(f"- **Test:** `{TOTALS}`\n",
                              f"- **Test:** `{TOTALS}`\n- **Mandatory:** no\n")

#: The test each criterion's binding names, to tell the fake runs apart.
NODES = {"AC-01": "test_import.py::test_valid", "AC-02": "test_import.py::test_refused",
         "AC-03": "test_totals.py::test_match", "AC-04": "bench.py"}
FILES = {
    "tests/acceptance/test_import.py": "def test_valid():\n    pass\n\n\ndef test_refused():\n"
                                       "    pass\n",
    "tests/acceptance/test_totals.py": "def test_match():\n    pass\n",
    "tests/acceptance/bench.py": "print(8)\n",
    "app.py": "print('ledger')\n",
}
BASELINE = {"AC-01": GREEN, "AC-02": RED, "AC-03": RED, "AC-04": RED, "OP-01": OPERATOR}


class _Runner:
    """Plays every bound test: `plan` maps a criterion to pass, fail (an assertion: RED),
    error (an environment error: BLOCKED) or timeout (UNKNOWN), and a metric to the values
    it prints in turn. Each run is recorded; `on_call` sees the criterion first."""

    def __init__(self, plan=None, on_call=None):
        self.plan = {"AC-01": "pass", "AC-02": "pass", "AC-03": "pass",
                     "AC-04": ["8.2", "8.4", "8.3"], **(plan or {})}
        self.calls: list[str] = []
        self.on_call = on_call

    def __call__(self, command, **kwargs):
        text = " ".join(map(str, command))
        cid = next(c for c, node in NODES.items() if node in text)
        self.calls.append(cid)
        if self.on_call is not None:
            self.on_call(cid)
        outcome = self.plan[cid]
        if isinstance(outcome, list):
            value = outcome[(self.calls.count(cid) - 1) % len(outcome)]
            return subprocess.CompletedProcess(command, 0, f"{value}\n", "")
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, 1)
        junit = next(str(a).split("=", 1)[1] for a in command
                     if str(a).startswith("--junitxml="))
        name = NODES[cid].split("::")[-1]
        body = {"pass": "", "fail": '<failure message="assert 1 == 2"/>',
                "error": '<error message="ImportError: no bank adapter"/>'}[outcome]
        Path(junit).write_text(f'<testsuite><testcase name="{name}">{body}</testcase>'
                               "</testsuite>", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0 if outcome == "pass" else 1, "", "")


class _Dan:
    """Dan's channel: records each question once and answers from `answers` by id; its
    question records (what the real channel keeps on disk) are what `records` returns."""

    def __init__(self, answers=None):
        self.answers = dict(answers or {})
        self.asked: list[dict] = []
        self.fields: dict[str, dict] = {}

    def poll_question(self, spec):
        if spec["id"] not in [s["id"] for s in self.asked]:
            self.asked.append(json.loads(json.dumps(dict(spec))))
        return self.answers.get(spec["id"])

    def mark_question_recorded(self, req_id, **fields):
        self.fields[req_id] = {**json.loads(json.dumps(fields)), "recorded": True}

    def records(self):
        return [{**s, "type": "spec-question", **self.fields.get(s["id"], {})}
                for s in self.asked]

    def ids(self, prefix: str) -> list[str]:
        return [s["id"] for s in self.asked if s["id"].startswith(prefix)]

    def question_answer(self, req_id):
        """Dan's answer, read without asking again (the real channel's answer file)."""
        return self.answers.get(req_id)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


def _roadmap(repo: Path, tasks) -> None:
    blocks = "".join("```yaml\n" + "".join(f"{k}: {json.dumps(v)}\n" for k, v in t.items())
                     + "```\n\n" for t in tasks)
    (repo / "docs" / "ROADMAP.md").write_text("# ROADMAP\n\n" + blocks, encoding="utf-8")


def _baseline(repo: Path, states=None, *, values=None, commit=True, revision=None) -> None:
    """The stored baseline, as `maestro goals baseline` writes it (with the commit it ran
    on, unless `commit` is false)."""
    doc = goals.load(repo)
    results = [acceptance.Result(id=cid, state=state, reason="baseline",
                                 revision=doc.revision if revision is None else revision,
                                 value=(values or {}).get(cid))
               for cid, state in (BASELINE if states is None else states).items()]
    path = acceptance.store(repo, doc, results)
    if not commit:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.pop("commit", None)
        path.write_text(json.dumps(payload), encoding="utf-8")


def _project(tmp_path: Path, text: str = NO_OP, *, tasks=(), baseline=None, values=None,
             commit_baseline=True) -> Path:
    """A committed project with the contract `text`, the bound tests, a ROADMAP of `tasks`
    and (format 2) the stored baseline."""
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "GOALS.md").write_text(text, encoding="utf-8")
    (repo / ".gitignore").write_text(".orchestrator/\n", encoding="utf-8")
    for path, body in FILES.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(body, encoding="utf-8")
    _roadmap(repo, tasks)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    if text is not None and goals.load(repo).format == 2:
        _baseline(repo, baseline, values=values, commit=commit_baseline)
    return repo


def _commit(repo: Path, path: str = "app.py", text: str = "", message: str = "change") -> str:
    """Commit `text` at `path` (by default one more line in the file)."""
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if not text:
        text = (target.read_text(encoding="utf-8") if target.exists() else "") + "# more\n"
    target.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _ran(repo: Path) -> None:
    """The corrective tasks the finish replans added ran and graduated (listed completed)
    without fixing anything, and the merged tree moved HEAD."""
    ids = [t for r in goal_store.read(repo)["replans"] for t in r.get("applied") or ()]
    done = repo / ".orchestrator" / "completed_tasks.json"
    known = json.loads(done.read_text(encoding="utf-8")) if done.exists() else []
    done.write_text(json.dumps(known + [{"id": t} for t in ids]), encoding="utf-8")
    _commit(repo)


def _evaluate(repo: Path, runner=None, **kwargs) -> dict:
    kwargs.setdefault("at", AT)
    kwargs.setdefault("budget", float("inf"))
    dan = kwargs.get("channel")
    kwargs.setdefault("records", dan.records if isinstance(dan, _Dan) else (lambda: []))
    return gc.evaluate(repo, runner=runner or _Runner(), **kwargs)


def _items(step: dict) -> dict[str, dict]:
    return {i["id"]: i for i in step["verdict"]["items"]}


def _finish_replans(repo: Path) -> list[dict]:
    return [r for r in goal_store.read(repo)["replans"]
            if (r.get("trigger") or {}).get("kind") == "finish_gap"]


# --- release safety -------------------------------------------------------------------------


def test_without_goals_md_nothing_runs_and_the_queue_exhausts_as_before(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = _Runner()
    step = gc.evaluate(repo, runner=runner, at=AT, records=lambda: [])
    assert step["kind"] == "none" and step["verdict"] is None
    assert runner.calls == []
    assert not goal_store.path_for(repo).exists()
    assert not (repo / gc.REPORT_MD).exists() and not (repo / gc.REPORT_JSON).exists()


def test_format_one_is_not_judged_and_says_so(tmp_path):
    repo = _project(tmp_path, SKILL_STYLE)
    runner = _Runner()
    step = _evaluate(repo, runner)
    assert step["kind"] == "none" and step["verdict"] is None
    assert "format 1" in step["text"] and "not verified" in step["text"]
    assert runner.calls == [] and not goal_store.path_for(repo).exists()
    assert not (repo / gc.REPORT_MD).exists()


# --- the verdict (R53, R54, C30) ----------------------------------------------------------


def test_an_empty_queue_with_a_red_criterion_is_incomplete_never_complete(tmp_path):
    repo = _project(tmp_path)
    told: list[str] = []
    step = _evaluate(repo, _Runner({"AC-02": "fail"}), notify=told.append)
    v = step["verdict"]
    assert v["verdict"] == "incomplete" and step["kind"] != "complete"
    assert _items(step)["AC-02"]["status"] == "work"
    assert "AC-02" in [w["id"] for w in v["work"]]
    assert v["id"] == gc.finish_id(2, _git(repo, "rev-parse", "HEAD"))
    [text] = told
    assert text.startswith("🔁 Not done yet — incomplete") and "AC-02" in text


def test_every_remaining_task_goal_held_is_blocked_with_the_hold_reasons(tmp_path):
    held = {"id": "T7", "status": "open", "mode": "autonomous", "criteria": ["AC-02"],
            "evidence": "AC-02 GREEN"}
    repo = _project(tmp_path, tasks=[held])
    told: list[str] = []
    step = _evaluate(repo, _Runner({"AC-02": "fail"}), notify=told.append)
    assert step["verdict"]["verdict"] == "blocked"
    item = _items(step)["AC-02"]
    assert item["status"] == "dan"
    assert "T7" in item["why"] and "names no goal it serves" in item["why"]
    [text] = told
    assert text.startswith("⏸ Not done — blocked") and "T7" in text
    assert "names no goal it serves" in text
    assert _finish_replans(repo) == []


def _signed(repo: Path, result: str = "pass", *, commit: str | None = None,
            revision: int = 2, observation: str = "") -> None:
    head = commit if commit is not None else _git(repo, "rev-parse", "HEAD")
    goal_store.update(repo, "operator", lambda rows: [*rows, {
        "id": "OP-01", "revision": revision, "commit": head,
        "finish": gc.finish_id(revision, head), "result": result,
        "observation": observation, "answer": "Pass" if result == "pass" else observation,
        "at": AT, "question_id": "dq-finish-op-OP-01-test"}])


def test_complete_only_when_every_mandatory_criterion_operator_check_scenario_and_goal_is_met(
        tmp_path):
    repo = _project(tmp_path, FULL)
    _signed(repo)
    told: list[str] = []
    step = _evaluate(repo, notify=told.append)
    v = step["verdict"]
    assert (step["kind"], v["verdict"]) == ("complete", "complete")
    items = _items(step)
    for nid in ("G1", "G2", "G3", "S-01", "S-02", "S-03", "AC-01", "AC-02", "AC-03",
                "AC-04", "OP-01", "INV-01"):
        assert items[nid]["status"] == "met", (nid, items[nid])
    assert v["work"] == [] and v["dan"] == []
    [text] = told
    head = _git(repo, "rev-parse", "HEAD")
    assert text.startswith(f"✅ Project complete at contract revision 2 ({head[:12]})")
    assert "3 goals, 4 criteria, 1 operator checks verified" in text and gc.REPORT_MD in text


@pytest.mark.parametrize("case", ["red", "blocked", "unknown", "unbound", "op_unsigned",
                                  "op_failed", "inv_checker_red"])
def test_any_unmet_mandatory_item_refuses_complete(tmp_path, case):
    text, plan, nid = NO_OP, {}, "AC-03"
    if case == "red":
        plan = {"AC-03": "fail"}
    elif case == "blocked":
        plan = {"AC-03": "error"}
    elif case == "unknown":
        plan = {"AC-03": "timeout"}
    elif case == "unbound":
        text = NO_OP.replace(f"- **Test:** `{TOTALS}`\n", "")
    elif case in ("op_unsigned", "op_failed"):
        text, nid = FULL, "OP-01"
    else:
        text = NO_OP.replace(f"- **Test:** `{VALID}`\n", f"- **Test:** `{VALID}`\n"
                             "- **Mandatory:** no\n")
        plan, nid = {"AC-01": "fail"}, "INV-01"
    repo = _project(tmp_path, text)
    if case == "op_failed":
        _signed(repo, "fail", observation="the food total is off")
    step = _evaluate(repo, _Runner(plan))
    assert step["verdict"]["verdict"] != "complete" and step["kind"] != "complete"
    assert _items(step)[nid]["status"] != "met", _items(step)[nid]


def test_an_optional_criterion_not_green_is_deferred_not_blocking(tmp_path):
    repo = _project(tmp_path, AC03_OPTIONAL)
    runner = _Runner({"AC-03": "fail"})
    step = _evaluate(repo, runner)
    assert step["verdict"]["verdict"] == "complete"
    assert "AC-03" not in runner.calls                  # optional: not rerun
    assert any(d["id"] == "AC-03" for d in step["verdict"]["deferred"])


def test_backlog_items_left_after_polish_are_deferred_never_blocking(tmp_path):
    """GDT-11 (R56, `01` §9 "optional quality backlog nonblocking"): review follow-ups the
    polish stage left (open, held by a stage task, or deferred) are listed as deferred in
    the outcome; they never decide the verdict, and its fingerprint is unchanged. Settled
    items (done, declined, merged, moot) are not listed."""
    from maestro import backlog
    from maestro.docs import followups

    repo = _project(tmp_path)
    before = _evaluate(repo)["verdict"]
    assert before["verdict"] == "complete"
    raised = [followups.FollowUp(t, 1, "proof_review") for t in ("rename the field",
                                                                  "log less", "tidy docs")]
    backlog.add(repo, {"id": "T1"}, raised[:2], at=AT)
    backlog.add(repo, {"id": "polish-1-a", "polish_stage": 1}, raised[2:], at=AT)

    def settle(rows):
        rows[1].update(state="done", task="polish-1-a")
        return rows

    goal_store.update_file(backlog.path_for(repo), backlog.SECTIONS, "items", settle,
                           version=backlog.VERSION)
    (repo / gc.REPORT_MD).unlink()          # the same verdict: rewritten only when missing
    v = _evaluate(repo)["verdict"]
    assert v["verdict"] == "complete" and v["fingerprint"] == before["fingerprint"]
    listed = [d for d in v["deferred"] if d["id"].startswith("BL-")]
    assert listed == [
        {"id": "BL-0001", "state": "open",
         "why": "review follow-up, nonblocking (raised by T1)"},
        {"id": "BL-0003", "state": "deferred",
         "why": f"review follow-up, nonblocking ({backlog.DEFERRED_REASON})"}]
    report = (repo / gc.REPORT_MD).read_text(encoding="utf-8")
    assert "- BL-0001 open: review follow-up, nonblocking (raised by T1)" in report


def test_a_merged_item_left_with_its_kept_item_shows_its_text_in_the_outcome(tmp_path):
    """GDT-11 review L2: a duplicate merged into an item the polish stage left is listed
    beside it with its own text, so a wrong merge stays visible to Dan; a duplicate whose
    kept item was settled is not listed. The fingerprint is unchanged."""
    from maestro import backlog
    from maestro.docs import followups

    repo = _project(tmp_path)
    before = _evaluate(repo)["verdict"]
    raised = [followups.FollowUp(t, 1, "proof_review") for t in (
        "rename the field", "the field name is wrong", "log less", "logs are noisy")]
    backlog.add(repo, {"id": "T1"}, raised, at=AT)

    def triaged(rows):
        rows[0].update(state="deferred", task="polish-1-a", reason="not reported by polish-1-a")
        rows[1].update(state="merged", of="BL-0001", reason="same", task="polish-triage-1")
        rows[2].update(state="done", task="polish-1-a")
        rows[3].update(state="merged", of="BL-0003", reason="same", task="polish-triage-1")
        return rows

    goal_store.update_file(backlog.path_for(repo), backlog.SECTIONS, "items", triaged,
                           version=backlog.VERSION)
    (repo / gc.REPORT_MD).unlink()
    v = _evaluate(repo)["verdict"]
    assert v["verdict"] == "complete" and v["fingerprint"] == before["fingerprint"]
    listed = [d for d in v["deferred"] if d["id"].startswith("BL-")]
    assert listed == [
        {"id": "BL-0001", "state": "deferred",
         "why": "review follow-up, nonblocking (not reported by polish-1-a)"},
        {"id": "BL-0002", "state": "merged",
         "why": 'review follow-up, nonblocking (merged into BL-0001: "the field name is '
                'wrong")'}]


def test_green_from_an_older_revision_commit_or_source_closes_nothing(tmp_path):
    """R67: a baseline at revision 1, an observation at revision 1, a gate row GREEN at
    this revision and a FIN row at another commit are all GREEN; the finish run at
    (revision 2, HEAD) says AC-02 is RED, and only it counts."""
    repo = _project(tmp_path)
    _baseline(repo, {c: GREEN for c in ("AC-01", "AC-02", "AC-03", "AC-04")}, revision=1)
    doc = goals.load(repo)
    goal_store.update(repo, "criteria", lambda rows: [*rows, {
        "id": "AC-02", "revision": 1, "state": GREEN, "source": "checkpoint", "at": AT}])
    goal_state.record_gate(repo, doc, {"acceptance": {"AC-02": GREEN},
                                       "snapshot": _git(repo, "rev-parse", "HEAD")},
                           task="T1", run_id="run-T1", at="2099-01-01T00:00:00+00:00")
    other = "0" * 40
    goal_store.update(repo, "finish", lambda rows: [*rows, {
        "id": gc.finish_id(2, other), "revision": 2, "commit": other, "done": True,
        "results": {c: {"state": GREEN, "reason": "passed"}
                    for c in ("AC-01", "AC-02", "AC-03", "AC-04")}}])
    runner = _Runner({"AC-02": "fail"})
    step = _evaluate(repo, runner)
    assert "AC-02" in runner.calls
    assert step["verdict"]["verdict"] == "incomplete"
    assert _items(step)["AC-02"]["status"] == "work"


# --- operator checks (R14, C9) ---------------------------------------------------------------


def test_an_operator_check_needs_dan_s_pass_at_this_revision_and_commit(tmp_path):
    repo = _project(tmp_path, FULL)                     # baseline: OP-01 operator_only
    step = _evaluate(repo)
    assert _items(step)["OP-01"]["status"] == "dan" and step["verdict"]["verdict"] == "blocked"

    _signed(repo, "pass", commit="1" * 40)              # a pass on another commit
    assert _items(_evaluate(repo))["OP-01"]["status"] == "dan"

    dan = _Dan()
    _evaluate(repo, channel=dan)
    [qid] = dan.ids("dq-finish-op-OP-01-")
    dan.answers[qid] = "the food total is 12 shekels off"
    gc.poll(repo, dan, AT, records=dan.records)
    [row] = [r for r in goal_store.read(repo)["operator"] if r.get("question_id") == qid]
    assert (row["result"], row["observation"]) == ("fail", "the food total is 12 shekels off")
    assert row["commit"] == _git(repo, "rev-parse", "HEAD") and row["revision"] == 2
    item = _items(_evaluate(repo, channel=dan))["OP-01"]
    assert item["status"] == "work" and "the food total is 12 shekels off" in item["why"]


def test_operator_checks_are_asked_only_once_every_other_mandatory_item_is_met(tmp_path):
    repo = _project(tmp_path, FULL)
    dan = _Dan()
    _evaluate(repo, _Runner({"AC-02": "fail"}), channel=dan)
    assert dan.ids("dq-finish-op-") == []
    _ran(repo)
    step = _evaluate(repo, channel=dan)
    fin = gc.finish_id(2, _git(repo, "rev-parse", "HEAD"))
    assert dan.ids("dq-finish-op-") == [f"dq-finish-op-OP-01-{fin[4:]}"]
    [spec] = [s for s in dan.asked if s["id"].startswith("dq-finish-op-")]
    assert spec["run_id"] == f"finish-{fin}" and spec["task_id"] == "finish"
    assert spec["wait_key"] == f"finish-{fin}-OP-01"
    options = [o["label"] for o in spec["spec"]["options"]]
    assert options == ["Pass", "Fail"] and spec["spec"]["recommended"] is None
    assert "every category total is the same." in spec["spec"]["question"]
    assert "never an automated pass" in spec["spec"]["why_undefined"]
    assert _items(step)["OP-01"]["status"] == "dan"
    assert qid_in(step, f"dq-finish-op-OP-01-{fin[4:]}")


def qid_in(step: dict, qid: str) -> bool:
    return any(qid in d["why"] for d in step["verdict"]["dan"])


# --- trust (R24, R25, C13-C15) -----------------------------------------------------------------


def test_review_evidence_and_gate_observations_never_close_a_criterion(tmp_path):
    repo = _project(tmp_path)
    doc = goals.load(repo)
    goal_store.update(repo, "episodes", lambda rows: [*rows, {
        "id": "T1:G1", "task": "T1", "goal": "G1", "impact": "advanced", "revision": 2,
        "evidence": [{"kind": "test", "id": REFUSED, "result": "passed",
                      "criterion": "AC-02", "mocked": True}]}])
    goal_state.record_gate(repo, doc, {"acceptance": {"AC-02": GREEN},
                                       "snapshot": _git(repo, "rev-parse", "HEAD")},
                           task="T1", run_id="run-T1", at="2099-01-01T00:00:00+00:00")
    assert {r.id: r.state for r in goal_state.results(repo, doc)}["AC-02"] == GREEN
    # The gate's GREEN row makes AC-02 protected; nothing else advances on this run, so
    # its RED is plain corrective work, not a GDT-7 finish conflict (design §5).
    step = _evaluate(repo, _Runner({"AC-02": "fail", "AC-03": "fail", "AC-04": ["12"]}))
    assert _items(step)["AC-02"]["status"] == "work"
    assert step["verdict"]["verdict"] == "incomplete"


def test_a_bound_test_changed_since_the_baseline_without_a_correction_is_untrusted(tmp_path):
    repo = _project(tmp_path)
    _commit(repo, "tests/acceptance/test_totals.py", "def test_match():\n    assert True\n")
    step = _evaluate(repo)
    item = _items(step)["AC-03"]
    assert item["status"] == "dan" and "tests/acceptance/test_totals.py" in item["why"]
    assert "test_corrections" in item["why"] and step["verdict"]["verdict"] == "blocked"

    # A task that named the correction graduated since: the ROADMAP's history has it.
    _roadmap(repo, [{"id": "T9", "status": "open", "serves": ["G2"],
                     "contribution": "direct", "criteria": ["AC-03"],
                     "test_corrections": ["AC-03"], "evidence": "AC-03 GREEN"}])
    _commit(repo, "docs/ROADMAP.md", (repo / "docs" / "ROADMAP.md").read_text())
    _roadmap(repo, [])
    _commit(repo, "docs/ROADMAP.md", (repo / "docs" / "ROADMAP.md").read_text())
    (repo / ".orchestrator" / "completed_tasks.json").write_text(
        json.dumps([{"id": "T9"}]), encoding="utf-8")
    step = _evaluate(repo)
    assert _items(step)["AC-03"]["status"] == "met"
    assert step["verdict"]["verdict"] == "complete"


def test_a_metric_is_green_only_when_every_repeat_passes_and_a_mix_is_unknown(tmp_path):
    repo = _project(tmp_path, values={"AC-04": 42.0})
    runner = _Runner()
    step = _evaluate(repo, runner)
    assert runner.calls.count("AC-04") == gc.METRIC_RUNS == 3
    assert _items(step)["AC-04"]["status"] == "met"
    report = (repo / gc.REPORT_MD).read_text(encoding="utf-8")
    assert "8.2, 8.4, 8.3" in report and "median 8.3" in report and "baseline 42" in report

    _commit(repo)
    runner = _Runner({"AC-04": ["9.1", "9.4", "10.6"]})
    step = _evaluate(repo, runner)
    assert runner.calls.count("AC-04") == 3
    item = _items(step)["AC-04"]
    assert item["status"] == "work" and item["state"] == UNKNOWN
    assert "unstable" in item["why"] and "9.1, 9.4, 10.6" in item["why"]


def test_a_scenario_proved_only_by_unit_criteria_is_not_final_proof(tmp_path):
    text = NO_OP.replace(f"- **Layer:** e2e\n- **Evidence:** test\n- **Test:** `{REFUSED}`",
                         f"- **Layer:** unit\n- **Evidence:** test\n- **Test:** `{REFUSED}`")
    repo = _project(tmp_path, text)
    step = _evaluate(repo)
    item = _items(step)["S-02"]
    assert item["status"] == "dan" and "only unit-level proof" in item["why"]
    assert any("S-02" in line for line in step["verdict"]["limitations"])
    assert step["verdict"]["verdict"] == "blocked"


def test_an_invariant_needs_a_green_check_even_when_optional_and_one_without_a_check_blocks(
        tmp_path):
    optional = NO_OP.replace(f"- **Test:** `{VALID}`\n",
                             f"- **Test:** `{VALID}`\n- **Mandatory:** no\n")
    repo = _project(tmp_path / "a", optional)
    runner = _Runner()
    step = _evaluate(repo, runner)
    assert "AC-01" in runner.calls and _items(step)["INV-01"]["status"] == "met"
    assert step["verdict"]["verdict"] == "complete"

    unchecked = NO_OP.replace("(checked by AC-01)\n",
                              "(checked by AC-01)\n- INV-02: amounts are never rounded\n")
    repo = _project(tmp_path / "b", unchecked)
    step = _evaluate(repo)
    item = _items(step)["INV-02"]
    assert item["status"] == "dan" and "no negative check" in item["why"]
    assert step["verdict"]["verdict"] == "blocked"


# --- the corrective loop (R55, C31) -----------------------------------------------------------


def test_unmet_work_becomes_one_applied_finish_replan_per_goal_in_priority_order(tmp_path):
    repo = _project(tmp_path)
    step = _evaluate(repo, _Runner({"AC-02": "fail", "AC-03": "fail"}))
    assert step["verdict"]["verdict"] == "incomplete"
    replans = _finish_replans(repo)
    assert [r["goal"] for r in replans] == ["G2", "G1"]     # Priority: G2, G1, G3
    for r in replans:
        assert (r["status"], r["stage"]) == ("applied", "finish")
        assert r["applied"] and all(t.endswith("-" + r["id"][3:].lower()) for t in r["applied"])
        assert r["trigger"]["facts"][0] == step["verdict"]["id"]
    g2, g1 = replans
    assert any(t.startswith("ac-03-") for t in g2["applied"])
    assert any(t.startswith("ac-02-") for t in g1["applied"])
    landed = gr.unlanded(goal_store.read(repo)["replans"], set())
    assert sorted(t["id"] for t in landed) == sorted(g2["applied"] + g1["applied"])
    for task in landed:
        assert task["replan_of"] in (g1["id"], g2["id"]) and task["status"] == "open"
        assert "round 1" in task["notes"] and step["verdict"]["commit"][:12] in task["notes"]
        # R66, accepted: corrective tasks run on an empty queue and may touch the same
        # code, so they declare no scope and serialize among themselves (D20).
        assert "scope" not in task
    assert {p["id"] for p in step["verdict"]["proposals"]} == {g1["id"], g2["id"]}


def test_a_failed_operator_check_gets_a_fix_task_quoting_dan(tmp_path):
    repo = _project(tmp_path, FULL)
    dan = _Dan()
    _evaluate(repo, channel=dan)
    [qid] = dan.ids("dq-finish-op-")
    dan.answers[qid] = "Fail"
    gc.poll(repo, dan, AT, records=dan.records)
    [row] = goal_store.read(repo)["operator"]
    assert (row["result"], row["observation"]) == ("fail", "")
    dan.answers[qid] = "the food total is 12 shekels off"     # his words, by reply
    goal_store.update(repo, "operator", lambda rows: [])
    dan.fields.pop(qid)
    gc.poll(repo, dan, AT, records=dan.records)
    step = _evaluate(repo, channel=dan)
    assert step["verdict"]["verdict"] == "incomplete"
    [replan] = _finish_replans(repo)
    assert replan["goal"] == "G2" and replan["status"] == "applied"
    [task] = gr.unlanded([replan], set())
    assert task["id"] == f"op-01-fix-{replan['id'][3:].lower()}"
    assert task["criteria"][0] == "OP-01" and task["serves"] == ["G2"]
    assert "the food total is 12 shekels off" in task["short_desc"]
    assert "Change the product, never the check" in task["short_desc"]
    assert "every category total is the same." in task["short_desc"]
    assert "scope" not in task      # R66, accepted: what Dan saw names no file


def test_the_cap_asks_dan_and_keep_going_stays_blocked_never_done(tmp_path):
    repo = _project(tmp_path)
    dan = _Dan()
    fail = {"AC-02": "fail"}
    _evaluate(repo, _Runner(fail), channel=dan, rounds_cap=1)
    [first] = _finish_replans(repo)
    assert first["status"] == "applied" and dan.ids("dq-replan-") == []

    _ran(repo)
    step = _evaluate(repo, _Runner(fail), channel=dan, rounds_cap=1)
    assert step["verdict"]["verdict"] == "blocked"
    [qid] = dan.ids("dq-replan-")
    [spec] = [s for s in dan.asked if s["id"] == qid]
    assert "the completion gate found it unmet" in spec["spec"]["question"]
    assert "cap (1 round) is reached" in spec["spec"]["why_undefined"]
    labels = [o["label"] for o in spec["spec"]["options"]]
    assert labels[-1] == gr.KEEP_GOING and "One more round" in labels
    keep = spec["spec"]["options"][-1]["consequence"]
    assert "the project stays blocked, never done" in keep
    assert _items(step)["AC-02"]["status"] == "dan"
    assert len(_finish_replans(repo)) == 2

    dan.answers[qid] = gr.KEEP_GOING
    gr.poll(repo, dan, AT, records=dan.records)
    step = _evaluate(repo, _Runner(fail), channel=dan, rounds_cap=1)
    assert step["verdict"]["verdict"] == "blocked" and step["kind"] != "complete"
    assert dan.ids("dq-replan-") == [qid]                 # the same facts: not asked again
    assert len(_finish_replans(repo)) == 2                # no third round

    _commit(repo)                                         # a new HEAD is new evidence
    _evaluate(repo, _Runner(fail), channel=dan, rounds_cap=1)
    assert len(dan.ids("dq-replan-")) == 2


def test_the_cap_comes_from_the_contract_stop_policy_then_project_yaml_then_two(tmp_path):
    def cap(text: str, yaml_text: str | None = None, name: str = "p") -> int:
        repo = tmp_path / name
        (repo / "docs").mkdir(parents=True)
        (repo / "docs" / "GOALS.md").write_text(text, encoding="utf-8")
        if yaml_text is not None:
            (repo / "project.yaml").write_text(yaml_text, encoding="utf-8")
        return gc.round_cap(goals.load(repo), repo)

    stop = "two attempts miss the same criterion for the same cause."
    assert cap(V2, name="words") == 2
    assert cap(V2.replace(stop, "three attempts miss the same criterion."), name="three") == 3
    assert cap(V2.replace(stop, "4 attempts miss it."), "thresholds:\n  goal_finish_rounds: 7\n",
               name="digits") == 4
    no_number = V2.replace(stop, "the same cause repeats.")
    assert cap(no_number, "thresholds:\n  goal_finish_rounds: 5\n", name="yaml") == 5
    assert cap(no_number, name="default") == gc.DEFAULT_ROUNDS == 2
    assert cap(no_number, "thresholds:\n  goal_finish_rounds: 0\n", name="zero") == 2


REV3 = NO_OP.replace(
    "Contract revision: 2 (approved 2026-10-17 by Dan)",
    "Contract revision: 3 (approved 2026-10-18 by Dan)").replace(
    "- **Target:** every CSV export from the two banks imports with no manual edits.",
    "- **Target:** every CSV export from the three banks imports with no manual edits.").replace(
    "| 2 | 2026-10-17 | G3 target 15 s → 10 s | 15 s felt slow | AC-04 | Dan | rev 1 G3 target |",
    "| 2 | 2026-10-17 | G3 target 15 s → 10 s | 15 s felt slow | AC-04 | Dan | rev 1 G3 target |\n"
    "| 3 | 2026-10-18 | G1 target: three banks | a third bank | AC-01, AC-02 | Dan | "
    "rev 2 G1 target |")


def test_rounds_reset_when_dan_revises_the_goal_s_terms(tmp_path):
    repo = _project(tmp_path)
    dan = _Dan()
    fail = {"AC-02": "fail"}
    _evaluate(repo, _Runner(fail), channel=dan, rounds_cap=1)
    _ran(repo)
    _commit(repo, "docs/GOALS.md", REV3, "contract revision 3")
    step = _evaluate(repo, _Runner(fail), channel=dan, rounds_cap=1)
    assert step["verdict"]["revision"] == 3 and step["verdict"]["rounds"]["G1"] == 0
    replans = _finish_replans(repo)
    assert [r["status"] for r in replans] == ["applied", "applied"]
    assert dan.ids("dq-replan-") == []


def test_a_criterion_whose_only_task_is_parked_is_blocked_on_unpark(tmp_path):
    task = {"id": "T5", "status": "open", "mode": "autonomous", "serves": ["G1"],
            "contribution": "direct", "criteria": ["AC-02"], "evidence": "AC-02 GREEN"}
    repo = _project(tmp_path, tasks=[task])
    step = _evaluate(repo, _Runner({"AC-02": "fail"}), parked={"T5"})
    item = _items(step)["AC-02"]
    assert item["status"] == "dan" and "/unpark T5" in item["why"]
    assert step["verdict"]["verdict"] == "blocked" and _finish_replans(repo) == []


# --- finish-stage conflicts (R47, §19.7) -----------------------------------------------------

CONFLICT = {"AC-01": GREEN, "AC-02": RED, "AC-03": GREEN, "AC-04": GREEN, "OP-01": OPERATOR}


def test_a_regression_at_finish_while_another_goal_advanced_asks_dan_through_the_conflict_api(
        tmp_path):
    repo = _project(tmp_path, baseline=CONFLICT)
    dan = _Dan()
    step = _evaluate(repo, _Runner({"AC-03": "fail"}), channel=dan)
    [qid] = dan.ids("dq-finish-GC-")
    fin = step["verdict"]["id"]
    assert qid.endswith(fin[4:])
    [spec] = [s for s in dan.asked if s["id"] == qid]
    assert spec["run_id"] == f"finish-{fin}" and spec["task_id"] == "finish"
    assert spec["spec"]["conflict"]["stage"] == "finish"
    assert "the completion gate" in spec["spec"]["question"]
    assert spec["spec"]["conflict"]["regressed"] == "G2"
    assert _items(step)["AC-03"]["status"] == "dan"
    assert step["verdict"]["verdict"] == "blocked"


def test_dan_s_accept_at_finish_still_blocks_until_his_revision(tmp_path):
    repo = _project(tmp_path, baseline=CONFLICT)
    dan = _Dan()
    _evaluate(repo, _Runner({"AC-03": "fail"}), channel=dan)
    [qid] = dan.ids("dq-finish-GC-")
    dan.answers[qid] = "Accept G2 failing AC-03"
    gc.poll(repo, dan, AT, records=dan.records)
    [decision] = goal_conflicts.load(repo)
    assert (decision["resolution"], decision["stage"]) == ("accept", "finish")
    step = _evaluate(repo, _Runner({"AC-03": "fail"}), channel=dan)
    item = _items(step)["AC-03"]
    assert item["status"] == "dan" and "waits on your contract revision" in item["why"]
    assert step["verdict"]["verdict"] == "blocked"
    assert dan.ids("dq-finish-GC-") == [qid] and _finish_replans(repo) == []


def test_a_hard_invariant_regression_at_finish_is_work_not_a_question(tmp_path):
    repo = _project(tmp_path, baseline={"AC-01": GREEN, "AC-02": RED, "AC-03": RED,
                                        "AC-04": GREEN, "OP-01": OPERATOR})
    dan = _Dan()
    step = _evaluate(repo, _Runner({"AC-01": "fail"}), channel=dan)
    assert dan.ids("dq-finish-GC-") == []
    item = _items(step)["AC-01"]
    assert item["status"] == "work" and "hard_invariant_regression" in item["why"]
    assert step["verdict"]["verdict"] == "incomplete"


# --- budget, resume, telling once, failures --------------------------------------------------


def test_verification_resumes_across_polls_within_the_budget_and_after_a_restart(tmp_path):
    repo = _project(tmp_path)
    now = [0.0]
    journal: list[tuple[str, str]] = []

    def tick(_cid):
        now[0] += 400.0

    first = _Runner(on_call=tick)
    step = _evaluate(repo, first, budget=600, clock=lambda: now[0],
                     journal=lambda e, d: journal.append((e, d)))
    assert step["kind"] == "verifying" and step["verdict"] is None
    assert first.calls == ["AC-01", "AC-02"]
    assert [d for e, d in journal if e == "goal_finish_running"][:2] == [
        f"{gc.finish_id(2, _git(repo, 'rev-parse', 'HEAD'))} AC-01 (1/4)",
        f"{gc.finish_id(2, _git(repo, 'rev-parse', 'HEAD'))} AC-02 (2/4)"]

    importlib.reload(goal_store)
    importlib.reload(goal_state)
    importlib.reload(gc)
    now[0] = 0.0
    second = _Runner(on_call=tick)
    step = _evaluate(repo, second, budget=600, clock=lambda: now[0])
    assert second.calls == ["AC-03", "AC-04", "AC-04", "AC-04"]
    assert step["verdict"]["verdict"] == "complete"
    third = _Runner()
    _evaluate(repo, third)
    assert third.calls == []                              # nothing runs twice at one key


def test_the_same_verdict_is_told_once_across_restarts(tmp_path):
    """Review M5: a blocked state (the only task naming AC-02 is parked) adds no round, so
    both evaluations reach the tell; the fingerprint stored on the FIN row keeps the
    second one quiet after a module reload."""
    task = {"id": "T5", "status": "open", "mode": "autonomous", "serves": ["G1"],
            "contribution": "direct", "criteria": ["AC-02"], "evidence": "AC-02 GREEN"}
    repo = _project(tmp_path, tasks=[task])
    told: list[str] = []
    first = _evaluate(repo, _Runner({"AC-02": "fail"}), notify=told.append, parked={"T5"})
    importlib.reload(gc)
    second = _evaluate(repo, _Runner({"AC-02": "fail"}), notify=told.append, parked={"T5"})
    assert first["kind"] == second["kind"] == "stopped"
    assert first["verdict"]["fingerprint"] == second["verdict"]["fingerprint"]
    assert len(told) == 1 and told[0].startswith("⏸ Not done — blocked")


def test_a_dirty_checkout_is_never_complete(tmp_path):
    repo = _project(tmp_path)
    (repo / "app.py").write_text("print('uncommitted')\n", encoding="utf-8")
    (repo / "docs" / "ROADMAP.md").write_text("# ROADMAP\n\nDan, mid-edit\n", encoding="utf-8")
    step = _evaluate(repo)
    item = _items(step)["checkout"]
    assert item["status"] == "dan" and "uncommitted changes: app.py" in item["why"]
    assert "ROADMAP" not in item["why"] and step["verdict"]["verdict"] == "blocked"


def test_an_internal_error_is_blocked_never_complete(tmp_path, monkeypatch):
    repo = _project(tmp_path)
    journal: list[tuple[str, str]] = []

    def boom(*args, **kwargs):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(gc, "verdict", boom)
    step = _evaluate(repo, journal=lambda e, d: journal.append((e, d)))
    assert step["verdict"]["verdict"] == "blocked" and step["kind"] != "complete"
    assert "the completion gate failed" in step["text"] and "store exploded" in step["text"]
    assert [e for e, _ in journal if e == "goal_finish_failed"]


# --- the outcome report (R56) -----------------------------------------------------------------


def test_the_outcome_report_for_a_passing_project(tmp_path):
    repo = _project(tmp_path, AC03_OPTIONAL)
    step = _evaluate(repo)
    head = _git(repo, "rev-parse", "HEAD")
    report = (repo / gc.REPORT_MD).read_text(encoding="utf-8")
    assert report.startswith("# Outcome — Ledger")
    assert f"contract revision 2, commit {head[:12]}" in report
    assert "Verdict: complete" in report
    for section in ("## Goals → scenarios → evidence", "## Hard invariants",
                    "## Accepted trade-offs", "## Deferred (nonblocking)", "## Reproduce",
                    "## Evidence"):
        assert section in report, section
    assert "AC-01 GREEN" in report and f"`{VALID}`" in report and "finish run" in report
    assert "INV-01 held" in report and "TO-01" in report
    assert "AC-03" in report.split("## Deferred (nonblocking)")[1]
    assert "maestro goals finish" in report and step["verdict"]["id"] in report
    stored = json.loads((repo / gc.REPORT_JSON).read_text(encoding="utf-8"))
    assert (stored["verdict"], stored["commit"]) == ("complete", head)
    assert "## Unmet and next" not in report


def test_the_outcome_report_for_a_blocked_project(tmp_path):
    repo = _project(tmp_path, commit_baseline=False)
    dan = _Dan()
    fail = {"AC-02": "fail"}
    _evaluate(repo, _Runner(fail), channel=dan, rounds_cap=1)
    [applied] = _finish_replans(repo)
    _ran(repo)
    step = _evaluate(repo, _Runner(fail), channel=dan, rounds_cap=1)
    [qid] = dan.ids("dq-replan-")
    report = (repo / gc.REPORT_MD).read_text(encoding="utf-8")
    assert "Verdict: blocked" in report and step["verdict"]["verdict"] == "blocked"
    waiting = report.split("## Waiting on Dan")[1].split("\n## ")[0]
    assert qid in waiting
    unmet = report.split("## Unmet and next")[1].split("\n## ")[0]
    assert "AC-02 RED" in unmet and applied["id"] in unmet
    limits = report.split("## Limitations")[1].split("\n## ")[0]
    assert "baseline has no commit" in limits


# --- GDT-10 review: H1-H3, M1-M4, L1-L9 -----------------------------------------------------


def test_maestro_s_regenerated_views_do_not_make_the_checkout_dirty(tmp_path):
    """H1: the loop rewrites the dependency map and UPCOMING after every launch and
    publish; those controller views (`merge._MERGE_DISCARDABLE`) never block `complete`."""
    from maestro import merge

    repo = _project(tmp_path)
    for path in merge._MERGE_DISCARDABLE:
        _commit(repo, path, "In-progress: 04-quota-adapter-followups\n")
    for path in merge._MERGE_DISCARDABLE:
        (repo / path).write_text("_none — orchestrator idle_\n", encoding="utf-8")
    step = _evaluate(repo)
    assert _items(step)["checkout"]["status"] == "met"
    assert step["verdict"]["verdict"] == "complete"


#: T4 names AC-02 and serves no goal: the goals gate holds it.
HELD_T4 = {"id": "T4", "status": "open", "mode": "autonomous", "criteria": ["AC-02"],
           "evidence": "AC-02 GREEN"}


def _t5(**extra) -> dict:
    return {"id": "T5", "status": "open", "mode": "autonomous", "serves": ["G1"],
            "contribution": "direct", "criteria": ["AC-02"], "evidence": "AC-02 GREEN",
            **extra}


def test_a_task_waiting_for_a_held_dependency_is_blocked_with_that_hold_reason(tmp_path):
    """H2: T5 names AC-02 but waits for T4, which the goals gate holds. The loop never
    runs T5, so it is not "can run": AC-02 is Dan's, with T4's hold reason."""
    repo = _project(tmp_path, tasks=[HELD_T4, _t5(deps=["T4"])])
    told: list[str] = []
    step = _evaluate(repo, _Runner({"AC-02": "fail"}), notify=told.append)
    item = _items(step)["AC-02"]
    assert item["status"] == "dan" and "can run" not in item["why"]
    assert "T5 (names AC-02) waits for T4, which is held" in item["why"]
    assert "names no goal it serves" in item["why"]
    assert (step["kind"], step["verdict"]["verdict"]) == ("stopped", "blocked")
    [text] = told
    assert text.startswith("⏸ Not done — blocked") and "T4" in text
    assert _finish_replans(repo) == []


def test_a_naming_task_with_no_mode_is_blocked_not_can_run(tmp_path):
    """H2: only `autonomous` and `needs-dan` tasks run (the loop's own predicate)."""
    t5 = {k: v for k, v in _t5().items() if k != "mode"}
    repo = _project(tmp_path, tasks=[t5])
    step = _evaluate(repo, _Runner({"AC-02": "fail"}))
    item = _items(step)["AC-02"]
    assert item["status"] == "dan" and "can run" not in item["why"]
    assert "T5 (names AC-02) cannot be dispatched" in item["why"] and "mode" in item["why"]
    assert step["verdict"]["verdict"] == "blocked"


def test_a_landed_corrective_task_the_loop_never_runs_does_not_keep_the_gate_working(
        tmp_path):
    """H2 (`_landing`): round 1's task landed without a `mode:`; the gate does not wait for
    it forever (`working`): AC-02 is Dan's, saying why the task cannot run."""
    repo = _project(tmp_path)
    _evaluate(repo, _Runner({"AC-02": "fail"}))
    [replan] = _finish_replans(repo)
    [task] = gr.unlanded([replan], set())
    _roadmap(repo, [{k: v for k, v in task.items() if k != "mode"}])
    _commit(repo, "docs/ROADMAP.md", (repo / "docs" / "ROADMAP.md").read_text())
    step = _evaluate(repo, _Runner({"AC-02": "fail"}))
    assert step["kind"] == "stopped"
    item = _items(step)["AC-02"]
    assert item["status"] == "dan"
    assert f"{task['id']} (names AC-02) cannot be dispatched" in item["why"]


def _accept(repo: Path, *args: str) -> subprocess.CompletedProcess:
    import os
    import sys

    source = str(Path(gc.__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": source}
    return subprocess.run([sys.executable, "-m", "maestro.acceptance", *args], cwd=repo,
                          capture_output=True, text=True, env=env,
                          timeout=HANG_SECONDS)


def test_one_criterion_s_state_is_a_gate_check_green_or_credible(tmp_path):
    """H3, L2: `python -m maestro.acceptance AC [--credible]` runs one criterion in the
    project it is run in: exit 0 when GREEN (a metric against its threshold, not its exit
    status), with `--credible` when a credible RED or GREEN (a harness task's check). It
    leaves no bytecode or pytest cache in the tree."""
    repo = _project(tmp_path, FULL)
    (repo / "tests" / "acceptance" / "test_import.py").write_text(
        "def test_valid():\n    pass\n\n\ndef test_refused():\n    assert 1 == 2\n",
        encoding="utf-8")
    green = _accept(repo, "AC-01")
    assert green.returncode == 0 and f"AC-01 {GREEN}" in green.stdout, green.stderr
    assert _accept(repo, "AC-02").returncode == 1
    assert _accept(repo, "AC-02", "--credible").returncode == 0
    (repo / "tests" / "acceptance" / "test_totals.py").write_text(
        "import no_such_bank_adapter\n\n\ndef test_match():\n    pass\n", encoding="utf-8")
    blocked = _accept(repo, "AC-03", "--credible")
    assert blocked.returncode == 1 and BLOCKED in blocked.stdout
    (repo / "tests" / "acceptance" / "bench.py").write_text("print(12)\n", encoding="utf-8")
    assert _accept(repo, "AC-04").returncode == 1          # exit 0, but over 10 seconds
    (repo / "tests" / "acceptance" / "bench.py").write_text("print(8)\n", encoding="utf-8")
    assert _accept(repo, "AC-04").returncode == 0
    assert _accept(repo, "AC-99").returncode == 2
    untracked = _git(repo, "status", "--porcelain", "--untracked-files=all")
    assert "__pycache__" not in untracked and ".pytest_cache" not in untracked


def test_a_metric_corrective_task_is_checked_against_its_threshold(tmp_path):
    """L2: a metric's corrective V1 is its criterion's state (`maestro.acceptance`), not
    the measurement's exit status; a wrapped pytest binding gets `-p no:cacheprovider`."""
    from maestro import goal_roadmap

    wrapped = NO_OP.replace(f"`{TOTALS}`", f"`timeout 60 {TOTALS}`")
    repo = _project(tmp_path, wrapped)
    doc = goals.load(repo)
    by_id = {c.id: c for c in doc.criteria}
    [metric] = goal_roadmap.checks([by_id["AC-04"]], repo)
    assert metric["kind"] == "auto" and metric["cmd"].endswith("-m maestro.acceptance AC-04")
    [timed] = goal_roadmap.checks([by_id["AC-03"]], repo)
    assert timed["cmd"].startswith("env PYTHONDONTWRITEBYTECODE=1 timeout 60 pytest")
    assert timed["cmd"].endswith("-p no:cacheprovider")


def test_an_operator_fix_task_with_no_bound_guard_is_confirmed_by_dan(tmp_path):
    """L2: Dan failed OP-01 and no mandatory criterion has a bound command, so the fix
    task's check is Dan's confirmation (never an empty list, which INV-04 refuses)."""
    unbound = FULL
    for test in (VALID, REFUSED, TOTALS, BENCH):
        unbound = unbound.replace(f"- **Test:** `{test}`\n", "")
    unbound = unbound.replace("- **Layer:** e2e\n- **Evidence:** test\n",
                              "- **Layer:** e2e\n- **Evidence:** test\n- **Mandatory:** no\n")
    unbound = unbound.replace("- **Layer:** integration\n- **Evidence:** test\n",
                              "- **Layer:** integration\n- **Evidence:** test\n"
                              "- **Mandatory:** no\n")
    unbound = unbound.replace("- **Layer:** e2e\n- **Evidence:** metric\n",
                              "- **Layer:** e2e\n- **Evidence:** metric\n- **Mandatory:** no\n")
    repo = _project(tmp_path, unbound)
    _signed(repo, "fail", observation="the food total is off")
    _evaluate(repo, channel=_Dan())
    tasks = [t for r in _finish_replans(repo) for t in gr.unlanded([r], set())]
    [fix] = [t for t in tasks if t["id"].startswith("op-01-fix-")]
    [check] = fix["verifications"]
    assert check["kind"] == "dan" and "OP-01" in check["check"]


def test_a_sign_off_stored_before_a_crash_is_still_marked_recorded(tmp_path):
    """M1: the sign-off was stored, then marking the question recorded failed; the next
    poll marks it (with the stored row), so its wait does not hold the gate forever."""
    repo = _project(tmp_path, FULL)
    dan = _Dan()
    _evaluate(repo, channel=dan)
    [qid] = dan.ids("dq-finish-op-")
    dan.answers[qid] = "Pass"
    real = dan.mark_question_recorded
    dan.mark_question_recorded = lambda *a, **k: (_ for _ in ()).throw(OSError("disk"))
    gc.poll(repo, dan, AT, records=dan.records)
    [row] = goal_store.read(repo)["operator"]
    assert qid not in dan.fields
    dan.mark_question_recorded = real
    gc.poll(repo, dan, AT, records=dan.records)
    assert dan.fields[qid]["recorded"] and dan.fields[qid]["op_signoff"] == row
    assert len(goal_store.read(repo)["operator"]) == 1


def test_results_from_a_dirty_tree_are_rerun_once_it_is_clean(tmp_path):
    """M2: the run at HEAD ran on Dan's uncommitted edit; once he discards it (HEAD is
    unchanged) the clean tree is verified again before any verdict."""
    repo = _project(tmp_path)
    (repo / "app.py").write_text("print('uncommitted')\n", encoding="utf-8")
    assert _evaluate(repo)["verdict"]["verdict"] == "blocked"
    _git(repo, "checkout", "--", "app.py")
    runner = _Runner()
    step = _evaluate(repo, runner)
    assert runner.calls[:2] == ["AC-01", "AC-02"]
    assert step["verdict"]["verdict"] == "complete"
    again = _Runner()
    _evaluate(repo, again)
    assert again.calls == []                               # the clean run is final


def test_a_held_state_is_journaled_once_and_trust_is_computed_once_per_row(
        tmp_path, monkeypatch):
    """M3: polling a blocked state re-journals nothing and does not redo the trust sweep
    (git history and a ROADMAP parse per revision) while (baseline, HEAD, ROADMAP) hold."""
    repo = _project(tmp_path, tasks=[_t5()])
    swept: list[int] = []
    real = gc.trust
    monkeypatch.setattr(gc, "trust", lambda *a, **k: swept.append(1) or real(*a, **k))
    journal: list[tuple[str, str]] = []
    for _ in range(3):
        _evaluate(repo, _Runner({"AC-02": "fail"}), parked={"T5"},
                  journal=lambda e, d: journal.append((e, d)))
    assert len([e for e, _ in journal if e == "goal_finish_verdict"]) == 1
    assert len(swept) == 1


def test_the_finish_cli_does_not_silence_the_loop_s_message(tmp_path):
    """M4: `maestro goals finish` (no notify) records nothing as told, so the loop's own
    evaluation of the same verdict still tells Dan."""
    repo = _project(tmp_path, tasks=[_t5()])
    cli = _evaluate(repo, _Runner({"AC-02": "fail"}), act=False, parked={"T5"})
    told: list[str] = []
    loop = _evaluate(repo, _Runner({"AC-02": "fail"}), parked={"T5"}, notify=told.append)
    assert cli["verdict"]["fingerprint"] == loop["verdict"]["fingerprint"]
    assert len(told) == 1 and told[0].startswith("⏸ Not done — blocked")


def test_every_metric_repeat_heartbeats(tmp_path):
    """L1: a metric runs three times; each repeat journals a heartbeat, so the watchdog's
    stall window never sees three timeouts of silence."""
    repo = _project(tmp_path)
    journal: list[tuple[str, str]] = []
    _evaluate(repo, journal=lambda e, d: journal.append((e, d)))
    beats = [d for e, d in journal if e == "goal_finish_running" and "AC-04" in d]
    assert len(beats) == gc.METRIC_RUNS


def test_only_a_graduated_task_s_correction_excuses_a_changed_bound_test(tmp_path):
    """L3: an open task's `test_corrections:` never ran; it does not excuse a change to
    AC-03's test made by someone else."""
    repo = _project(tmp_path)
    _commit(repo, "tests/acceptance/test_totals.py", "def test_match():\n    assert True\n")
    _roadmap(repo, [{"id": "T9", "status": "open", "mode": "autonomous", "serves": ["G2"],
                     "contribution": "direct", "criteria": ["AC-03"],
                     "test_corrections": ["AC-03"], "evidence": "AC-03 GREEN"}])
    _commit(repo, "docs/ROADMAP.md", (repo / "docs" / "ROADMAP.md").read_text())
    step = _evaluate(repo)
    assert _items(step)["AC-03"]["status"] == "dan"
    assert step["verdict"]["verdict"] == "blocked"


def test_an_unreadable_bound_test_diff_blocks_complete(tmp_path):
    """L4: the baseline names a commit, but its diff cannot be read: the bound tests are
    not shown untouched, which is never permission to proceed."""
    repo = _project(tmp_path)
    path = repo / acceptance.BASELINE
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["commit"] = "f" * 40
    path.write_text(json.dumps(payload), encoding="utf-8")
    step = _evaluate(repo)
    item = _items(step)["AC-01"]
    assert item["status"] == "dan" and "not compared" in item["why"]
    assert step["verdict"]["verdict"] == "blocked"


def test_a_goal_proved_by_nothing_mandatory_is_a_limitation(tmp_path):
    """L5: G2's verifiers (AC-03, OP-01) are both optional: met, and said so."""
    repo = _project(tmp_path, AC03_OPTIONAL)
    step = _evaluate(repo)
    assert _items(step)["G2"]["status"] == "met"
    assert any("G2 is proved by nothing mandatory" in t for t in step["verdict"]["limitations"])


def test_an_internal_failure_is_told_once_however_its_text_varies(tmp_path, monkeypatch):
    """L6: the failure text names a temp file that changes every poll; Dan hears it once."""
    repo = _project(tmp_path)
    count = iter(range(100))

    def boom(*args, **kwargs):
        raise OSError(f"cannot replace .goal_outcome.md.{next(count)}.tmp")

    monkeypatch.setattr(gc, "verdict", boom)
    monkeypatch.setattr(gc, "_FAILED_TOLD", set())
    told: list[str] = []
    for _ in range(2):
        assert _evaluate(repo, notify=told.append)["kind"] == "stopped"
    assert len(told) == 1


def test_a_task_id_inside_another_is_still_deferred(tmp_path):
    """L7: T10 (parked) blocks AC-02; T1 is unrelated open work and is listed as deferred,
    although its id is part of "T10"."""
    t10 = dict(_t5(), id="T10")
    t1 = {"id": "T1", "status": "open", "mode": "autonomous", "serves": ["G3"],
          "contribution": "maintenance", "evidence": "tidy"}
    repo = _project(tmp_path, tasks=[t10, t1])
    step = _evaluate(repo, _Runner({"AC-02": "fail"}), parked={"T10"})
    assert "T1" in [d["id"] for d in step["verdict"]["deferred"]]
    assert "T10" not in [d["id"] for d in step["verdict"]["deferred"]]


class _Counting(_Dan):
    """`_Dan` that counts every `poll_question` call by id."""

    def __init__(self, answers=None):
        super().__init__(answers)
        self.polled: list[str] = []

    def poll_question(self, spec):
        self.polled.append(spec["id"])
        return super().poll_question(spec)


def test_a_recorded_conflict_question_is_not_asked_again_at_the_same_run(
        tmp_path, monkeypatch):
    """L8: GDT-7 raises the same conflict again at the same FIN after Dan's decision was
    recorded; the gate does not poll it again (which would re-add a wait no poll clears)."""
    repo = _project(tmp_path, baseline=CONFLICT)
    dan = _Counting()
    first = _evaluate(repo, _Runner({"AC-03": "fail"}), channel=dan)
    [qid] = dan.ids("dq-finish-GC-")
    dan.answers[qid] = "Accept G2 failing AC-03"
    gc.poll(repo, dan, AT, records=dan.records)
    judgement = gc._judge(repo, goals.load(repo), goal_store.read(repo)["finish"][-1],
                          acceptance.current(acceptance.load(repo), goals.load(repo)), [])
    monkeypatch.setattr(gc, "_judge", lambda *a, **k: judgement)
    dan.polled.clear()
    _evaluate(repo, _Runner({"AC-03": "fail"}), channel=dan)
    assert first["verdict"]["id"] and qid not in dan.polled


def test_an_answer_given_after_head_moved_is_still_recorded(tmp_path):
    """L9: Dan answers the operator check asked at the previous HEAD; the poll records it
    (a sign-off for that commit) instead of dropping his answer."""
    repo = _project(tmp_path, FULL)
    dan = _Dan()
    _evaluate(repo, channel=dan)
    [qid] = dan.ids("dq-finish-op-")
    old = _git(repo, "rev-parse", "HEAD")
    _commit(repo)
    dan.answers[qid] = "Pass"
    gc.poll(repo, dan, AT, records=dan.records)
    [row] = goal_store.read(repo)["operator"]
    assert (row["question_id"], row["commit"], row["result"]) == (qid, old, "pass")
    assert dan.fields[qid]["recorded"]


def test_a_git_status_that_does_not_answer_is_not_called_a_non_git_checkout(
        tmp_path, monkeypatch):
    """L9: `git status` timing out is a failure of the gate, not "not a git checkout"."""
    repo = _project(tmp_path)
    real = gc._git
    monkeypatch.setattr(gc, "_git", lambda r, *a: None if a[:1] == ("status",) else real(r, *a))
    step = _evaluate(repo)
    assert step["verdict"]["verdict"] == "blocked"
    assert "not a git checkout" not in json.dumps(step["verdict"])
    assert "git status" in step["text"]


# --- GDT-10 follow-ups (verify M-A, L-a, L-b, L-d) -----------------------------------------


def test_the_loop_rewrites_a_report_the_finish_cli_wrote_at_the_same_run(tmp_path):
    """L-a: `maestro goals finish` (`act=False`) wrote the report with "verdict only"; the
    loop's first evaluation of the same verdict rewrites it and journals its verdict."""
    repo = _project(tmp_path, tasks=[_t5()])
    _evaluate(repo, _Runner({"AC-02": "fail"}), act=False, parked={"T5"})
    report = repo / gc.REPORT_MD
    assert "verdict only" in report.read_text(encoding="utf-8")
    journal: list[tuple[str, str]] = []
    _evaluate(repo, _Runner({"AC-02": "fail"}), parked={"T5"},
              journal=lambda e, d: journal.append((e, d)))
    assert "verdict only" not in report.read_text(encoding="utf-8")
    assert [e for e, _ in journal if e == "goal_finish_verdict"] == ["goal_finish_verdict"]


def test_an_older_run_s_unanswered_question_is_read_not_asked_again(tmp_path, monkeypatch):
    """L-b: HEAD moved after OP-01 was asked; its wait was dropped. Each poll reads Dan's
    answer to that question without asking it again (no wait put back, no state write),
    and his later answer is still recorded as a sign-off at the commit it was asked at."""
    from maestro.hitl import verify

    from .test_goal_replan import _real_channel

    chan = _real_channel(tmp_path, monkeypatch)
    repo = _project(tmp_path, FULL)
    _evaluate(repo, channel=chan, records=gc._records)
    [qid] = [r["id"] for r in gc._records() if str(r["id"]).startswith(gc.OP_PREFIX)]
    old = _git(repo, "rev-parse", "HEAD")
    _commit(repo)
    fid = gc.poll(repo, chan, AT)
    verify.prune_runless_waits("finish-", [fid], read_state=chan.read_state,
                               write_state=chan.write_state)
    before = len(chan.writes)
    for _ in range(2):
        gc.poll(repo, chan, AT)
    assert len(chan.writes) == before
    assert not any(str(w.get("run_id")).startswith("finish-")
                   for w in (chan.state.get(verify.QUESTIONS_STATE_KEY) or {}).values())
    (tmp_path / "questions" / f"{qid}.answer").write_text("Pass\n", encoding="utf-8")
    gc.poll(repo, chan, AT)
    [row] = goal_store.read(repo)["operator"]
    assert (row["question_id"], row["commit"], row["result"]) == (qid, old, "pass")
    assert not (chan.state.get(verify.QUESTIONS_STATE_KEY) or {})


def test_a_gate_check_in_a_task_worktree_runs_as_the_finish_run_does(tmp_path):
    """L-d: `python -m maestro.acceptance` run in a task worktree (no `.venv`) uses the
    controller's interpreter, as the finish run does, and the controller's current
    contract: Dan renamed AC-02 to AC-05 after the worktree was cut."""
    import shlex
    import stat
    import sys

    repo = _project(tmp_path, FULL)
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "task", str(worktree))
    marker = tmp_path / "interpreter-used"
    python = repo / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f"#!/bin/sh\necho \"$@\" >> {shlex.quote(str(marker))}\n"
                      f"exec {shlex.quote(sys.executable)} \"$@\"\n", encoding="utf-8")
    python.chmod(python.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    contract = repo / "docs" / "GOALS.md"
    contract.write_text(contract.read_text(encoding="utf-8").replace("AC-02", "AC-05"),
                        encoding="utf-8")
    run = _accept(worktree, "AC-05")
    assert run.returncode == 0 and f"AC-05 {GREEN}" in run.stdout, (run.stdout, run.stderr)
    assert "-m pytest" in marker.read_text(encoding="utf-8")
    assert _git(worktree, "status", "--porcelain", "--untracked-files=all") == ""
    # The controller's contract is the one judged: the worktree's old AC-02 is not a
    # criterion there, so the check refuses it rather than falling back (GDT-10fu F2).
    stale = _accept(worktree, "AC-02")
    assert stale.returncode == 2 and "AC-02: not a criterion" in stale.stderr, stale.stderr


def test_the_controller_of_a_directory_git_does_not_know_is_itself(tmp_path, monkeypatch):
    """GDT-10fu F2: outside any git checkout, `controller` returns the directory itself, so
    the check reads that directory's contract."""
    from maestro import acceptance

    plain = tmp_path / "plain"
    (plain / "docs").mkdir(parents=True)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    assert acceptance.controller(plain) == plain
    repo = _project(tmp_path)
    assert acceptance.controller(repo).resolve() == repo.resolve()       # the main worktree


def test_a_gate_check_falls_back_to_the_worktree_when_the_controller_has_no_contract(
        tmp_path):
    """GDT-10fu F2: a linked worktree whose controller has no `docs/GOALS.md` (deleted, or
    never committed there) is judged by its own contract."""
    from maestro import acceptance

    repo = _project(tmp_path, FULL)
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "task", str(worktree))
    assert acceptance.controller(worktree).resolve() == repo.resolve()
    (repo / "docs" / "GOALS.md").unlink()
    run = _accept(worktree, "AC-02")
    assert run.returncode == 0 and f"AC-02 {GREEN}" in run.stdout, (run.stdout, run.stderr)
