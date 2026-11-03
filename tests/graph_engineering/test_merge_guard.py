"""P12 finding D10: risky files and the deny-list on the graph's merge path.

The graph merge node (`handlers.merge_handler` -> `merge.integrate_candidate` ->
`validation.StagingPipeline`) skipped both of the legacy loop's pre-merge checks. Dan's
decision (2026-09-25):

- the hard deny-list (and secret files) still refuses a merge, fail closed: the run parks
  with the reason recorded and nothing lands;
- a risky-set edit is allowed for an autonomous task, gated by a proof review from a
  model strictly stronger than the one that wrote the reviewed tree, whose brief names the
  risky files the real diff touched. With no approving review of that commit, merge refuses.
"""
from __future__ import annotations

import dataclasses
import json
import re
import subprocess
from pathlib import Path

import pytest

import maestro.state
from maestro import merge
from maestro.backends import catalog as cat
from maestro.backends.base import Completion, ExitVerdict
from maestro.control.artifacts import ArtifactStore
from maestro.control.store import ControlStore
from maestro.hitl import verify
from maestro.taskgraph import normalize_spec
from maestro.workflows import dan_questions, handlers
from maestro.workflows import runner as wf_runner
from maestro.workflows import worker as wf_worker
from maestro.workflows import compiler
from maestro.workflows.compiler import compile_workflow
from maestro.workflows.handlers import TmuxDispatcher
from maestro.workflows.models import NodeResult
from maestro.workflows.policy import default_policy

from . import conftest as lane_conftest
from .conftest import fixture_cli_entry, fixture_routing
from .test_dispatch_wiring import (
    GRAPH,
    _attempts,
    _FakeTmux,
    _journal_details,
    _node_phase,
    _passing_evidence,
    _task,
)
from .test_end_to_end import (
    _SPEC_QUESTION,
    _V1,
    FEATURE,
    TARGET,
    _DanAnswers,
    _drive_every_worker_of_any_run,
    _git,
    _happy_scenario,
    _refusal,
    _run_status,
)
from .test_failure_ladder import _patch_fixture_cli_driver, _rows_for


@pytest.fixture(autouse=True)
def _fresh_control_store():
    maestro.state._reset_control_store()
    yield
    maestro.state._reset_control_store()


RISKY = "src/spotify.py"


def _project(**extra) -> dict:
    return {**GRAPH, **extra}


class _WritingAgent:
    """The implementer writes the feature plus `files` and commits; a reviewer approves.

    `files` may be a list, one dict per implementer round (the last one repeats), and
    `reviews` the reviewer's `(verdict, findings)` per round before it approves.
    """

    def __init__(self, files, reviews=()):
        self.rounds = list(files) if isinstance(files, list) else [files]
        self.reviews = list(reviews)
        self.specs: list = []

    def complete(self, spec):
        self.specs.append(spec)
        workspace = Path(re.search(r"^WORKSPACE: (\S+)$", spec.prompt, re.MULTILINE).group(1))
        if spec.prompt.startswith("You are the implementer "):
            files = self.rounds[min(len(self.briefs("implementer")), len(self.rounds)) - 1]
            for rel, text in {FEATURE: "done\n", **files}.items():
                path = spec.cwd / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            _git(spec.cwd, "add", "-A")
            _git(spec.cwd, "commit", "-q", "-m", "feat: the feature")
        elif spec.prompt.startswith("You are the reviewer "):
            verdict, findings = self.reviews.pop(0) if self.reviews else ("approved", [])
            handlers.write_review(workspace, verdict, findings)
        handlers.write_sentinel(workspace, handlers.DONE_SENTINEL, "done")
        return Completion(text="done", returncode=0, timed_out=False)

    def parse_exit(self, rc, tail):
        return ExitVerdict(kind="ok")

    def briefs(self, role: str) -> list[str]:
        return [s.prompt for s in self.specs if s.prompt.startswith(f"You are the {role} ")]


def _balanced_and_strong():
    """A cheap balanced model for the writer, a strong one above it for the reviewer."""
    return fixture_routing(entries=[fixture_cli_entry(
        available_models=("fixture-b", "fixture-s"),
        model_profiles={'fixture-b': cat.ModelProfile('fixture-b'), 'fixture-s': cat.ModelProfile('fixture-s')},
        pair_profiles=cat.migrate_pairs(('fixture-b', 'balanced', 0.5), ('fixture-s', 'strong', 1.0)),
        pairs_migrated=True,
        concurrency_limit=3,
    )])


BALANCED_POOL = "fixture-pool-b"
STRONG_POOL = "fixture-pool"  # `fixture_cli_entry`'s own


def _a_pool_each(*, balanced_paused: bool):
    """`_balanced_and_strong`, each model on a quota pool of its own, so a pause or a
    reset moves a node between them the way it does in production."""
    def entry(model, strength, cost, **more):
        return fixture_cli_entry(
            available_models=(model,), model_profiles={model: cat.ModelProfile(model)},
            pair_profiles=cat.migrate_pairs((model, strength, cost)), pairs_migrated=True,
            concurrency_limit=3, **more)

    routing = fixture_routing(entries=[
        entry("fixture-s", "strong", 1.0),
        entry("fixture-b", "balanced", 0.5, backend_id="fixture_cli_b",
              usage_pool_id=BALANCED_POOL),
    ])
    if balanced_paused:
        routing.resources.pool(BALANCED_POOL).pause(until=10**12, reason="weekly limit")
    return routing


def _models_by_node() -> dict:
    rows = maestro.state.control_store().conn.execute(
        "SELECT node_id, model_id FROM attempts WHERE model_id IS NOT NULL ORDER BY rowid"
    ).fetchall()
    return {row["node_id"]: row["model_id"] for row in rows}


def _assert_merge_refused(run, prefix: str) -> str:
    assert _run_status() == "failed", _journal_details("graph_run_settled")
    merges = [a for a in _attempts() if a["node_id"] == "merge"]
    assert merges and {a["status"] for a in merges} == {"failed"}
    [reason] = _journal_details("merge_refused")
    assert f": {prefix}" in reason, reason
    store = maestro.state.control_store()
    assert store.conn.execute("SELECT COUNT(*) FROM acceptance_decisions").fetchone()[0] == 0
    assert "feat: the feature" not in _git(run.repo, "log", "--format=%s", TARGET)
    # Parked, not landed: the branch stays for whoever picks it up.
    assert _git(run.repo, "branch", "--list", "impl/TASK-001")
    return reason


# ── the deny-list, in the graph ─────────────────────────────────────────────────────
#
# With a channel that can ask Dan, the gate screens first and waits on his ruling (D29,
# `test_deny_list_ask.py`). These lanes take that channel's deny ask away, so they keep
# proving the backstop: an unruled hit still refuses the merge.


@pytest.fixture
def no_deny_ask(monkeypatch):
    monkeypatch.delattr(verify.DanVerificationChannel, "poll_deny")


def test_a_deny_list_hit_refuses_the_merge_and_the_run_parks(run_lane, monkeypatch, no_deny_ask):
    _patch_fixture_cli_driver(monkeypatch, _WritingAgent({"db/reset.sql": "DROP TABLE users;\n"}))

    run = run_lane(_happy_scenario("guard_deny_list", project_yaml=_project()))

    assert run.returncode == 0
    reason = _assert_merge_refused(run, "deny_list: deny-list violation: pattern")
    assert "DROP" in reason


def test_a_project_deny_list_extra_pattern_refuses_the_merge(run_lane, monkeypatch, no_deny_ask):
    _patch_fixture_cli_driver(monkeypatch, _WritingAgent({"src/app.py": "TOKEN = 'sk-live-1'\n"}))

    run = run_lane(_happy_scenario(
        "guard_deny_extra", project_yaml=_project(deny_list_extra=[r"sk-live-\d"]),
    ))

    assert run.returncode == 0
    _assert_merge_refused(run, "deny_list: deny-list violation: pattern 'sk-live-")


def test_an_unusable_global_config_parks_the_run_once_without_retrying_the_merge(
        run_lane, monkeypatch, no_deny_ask, tmp_path):
    broken = tmp_path / "broken-global.yaml"
    broken.write_text("thresholds: [unclosed\n")
    monkeypatch.setenv("MAESTRO_GLOBAL_CONFIG", str(broken))
    _patch_fixture_cli_driver(monkeypatch, _WritingAgent({"src/app.py": "x = 1\n"}))

    run = run_lane(_happy_scenario("guard_global_config", project_yaml=_project()))

    assert run.returncode == 0
    reason = _assert_merge_refused(run, "deny_list: the global config cannot be used")
    assert str(broken) in reason
    nodes = [a["node_id"] for a in _attempts()]
    assert nodes.count("merge") == 1
    assert "diagnose" not in nodes and "diagnoser" not in nodes


def test_a_secret_file_in_the_diff_refuses_the_merge(run_lane, monkeypatch, no_deny_ask):
    _patch_fixture_cli_driver(monkeypatch, _WritingAgent({"config/.env": "API_KEY=x\n"}))

    run = run_lane(_happy_scenario("guard_secret", project_yaml=_project()))

    assert run.returncode == 0
    reason = _assert_merge_refused(run, "deny_list: deny-list violation: secret")
    assert "config/.env" in reason


def test_a_clean_diff_under_a_deny_list_and_risky_set_merges(run_lane, monkeypatch):
    _patch_fixture_cli_driver(monkeypatch, _WritingAgent({"src/app.py": "x = 1\n"}))

    run = run_lane(_happy_scenario("guard_clean", project_yaml=_project(
        deny_list_extra=[r"sk-live-\d"], risky_set=["spotify.py"],
    )))

    assert run.returncode == 0
    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    assert _git(run.repo, "show", f"{TARGET}:src/app.py") == "x = 1"
    assert _journal_details("merge_refused") == []


# ── risky files: a stronger reviewer, told what the diff touched ────────────────────


def test_a_risky_edit_is_reviewed_by_a_stronger_model_told_the_files_and_lands(
    run_lane, monkeypatch
):
    agent = _WritingAgent({RISKY: "def refresh():\n    return 1\n", "src/app.py": "x = 1\n"})
    _patch_fixture_cli_driver(monkeypatch, agent)

    run = run_lane(_happy_scenario(
        "guard_risky_reviewed", routing=_balanced_and_strong,
        project_yaml=_project(risky_set=["spotify.py"]),
    ))

    assert run.returncode == 0
    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    assert _git(run.repo, "show", f"{TARGET}:{RISKY}").startswith("def refresh")
    [brief] = agent.briefs("reviewer")
    assert "RISKY FILES TOUCHED" in brief
    assert f"- {RISKY}" in brief
    # Only what the diff touched and the risky_set names: not the other changed file.
    assert "- src/app.py" not in brief
    assert "RISKY FILES TOUCHED" not in agent.briefs("implementer")[0]
    assert _models_by_node() == {"implementer": "fixture-b", "proof_review": "fixture-s"}


def test_risky_review_ranks_the_writer_pair_that_ran_not_its_model_maximum(run_lane, monkeypatch):
    def routing():
        entry = fixture_cli_entry(
            available_models=("fixture-b", "fixture-s"),
            model_profiles={"fixture-b": cat.ModelProfile("fixture-b"),
                            "fixture-s": cat.ModelProfile("fixture-s")},
            effort_controls=cat.EffortControls(supported=True, delivery_mechanism="flag",
                                               native_values=("medium", "high")),
            pair_profiles={(p.model_id, p.effort): p for p in [
                cat.PairProfile.from_score("fixture-b", "medium", 41, 0.5),
                cat.PairProfile.from_score("fixture-b", "high", 52, 2.0),
                cat.PairProfile.from_score("fixture-s", "medium", 48, 1.0)]},
            concurrency_limit=3)
        return fixture_routing(entries=[entry])

    _patch_fixture_cli_driver(monkeypatch, _WritingAgent({RISKY: "x = 1\n"}))
    run = run_lane(_happy_scenario("guard_pair_strength", routing=routing,
                                  project_yaml=_project(risky_set=["spotify.py"])))
    assert run.returncode == 0
    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    assert _models_by_node() == {"implementer": "fixture-b", "proof_review": "fixture-s"}


def test_no_stronger_reviewer_than_the_writer_refuses_the_review_route(run_lane, monkeypatch):
    """One strong route serves everyone: the writer is already on the top rung, so no model
    can out-rank it, and the review is refused rather than run by an equal."""
    _patch_fixture_cli_driver(monkeypatch, _WritingAgent({RISKY: "x = 1\n"}))

    run = run_lane(_happy_scenario(
        "guard_risky_no_stronger", project_yaml=_project(risky_set=["spotify.py"]),
    ))

    assert run.returncode == 0
    assert _run_status() == "failed", _journal_details("graph_run_settled")
    [review] = [a for a in _attempts() if a["node_id"] == "proof_review"]
    assert review["status"] == "failed"
    assert _refusal(review["attempt_id"]).startswith("route_no_stronger_reviewer:")
    assert "proof_review" not in _models_by_node()
    assert not [a for a in _attempts() if a["node_id"] == "merge"]
    assert _node_phase("park") == "claimed"
    assert "feat: the feature" not in _git(run.repo, "log", "--format=%s", TARGET)


def test_a_non_risky_review_keeps_its_route_when_the_writer_is_as_strong(run_lane, monkeypatch):
    """The strictly-stronger rule is the risky-set gate; an ordinary review is unchanged."""
    _patch_fixture_cli_driver(monkeypatch, _WritingAgent({"src/app.py": "x = 1\n"}))

    run = run_lane(_happy_scenario(
        "guard_not_risky_strong", project_yaml=_project(risky_set=["spotify.py"]),
    ))

    assert run.returncode == 0
    assert _run_status() == "succeeded", _journal_details("graph_run_settled")


def test_a_risky_edit_in_a_lane_with_no_reviewer_refuses_the_merge(run_lane):
    """The script lane composes no proof review, so a risky edit has no gate: merge refuses."""
    run = run_lane(_happy_scenario(
        "guard_risky_unreviewed",
        tasks=[_task(
            "TASK-001", kind="script", verifications=_V1,
            run=f"mkdir -p src && echo x > {RISKY} && echo done > {FEATURE}",
        )],
        project_yaml=_project(risky_set=["spotify.py"]),
    ))

    assert run.returncode == 0
    assert _run_status() == "failed", _journal_details("graph_run_settled")
    [reason] = _journal_details("merge_refused")
    assert "risky_unreviewed:" in reason and RISKY in reason
    assert FEATURE not in _git(run.repo, "ls-tree", "--name-only", TARGET)


def _reopened(run_lane, monkeypatch, agent, name, *, kept=True, ended=True):
    """Run TASK-001 on `agent` until its first writer succeeds on the strong model, cancel
    that run, and `ctl retry` the task once the balanced model's pool is back.

    `kept=False` deletes `impl/TASK-001` in between; `ended=False` clears the first
    writer's `end_sha`. Returns the lane, the two runs' statuses and that `end_sha`."""
    from maestro import cli

    _patch_fixture_cli_driver(monkeypatch, agent)
    held: dict = {}
    asked: dict = {}

    def routing():      # the first run opens while the balanced model's pool is paused
        if "routing" not in held:
            held["routing"] = _a_pool_each(balanced_paused=True)
        return held["routing"]

    def runs() -> list:
        return maestro.state.control_store().conn.execute(
            "SELECT run_id, status FROM task_runs ORDER BY started_at, rowid").fetchall()

    def on_poll(run) -> None:
        if "cancel" not in asked:
            rows = _rows_for("implementer")
            if rows and rows[0]["status"] == "succeeded":
                store = maestro.state.control_store()
                held["end_sha"] = store.conn.execute(
                    "SELECT end_sha FROM attempts WHERE attempt_id = ?", (rows[0]["attempt_id"],)
                ).fetchone()["end_sha"]
                if not ended:
                    with store.conn:
                        store.conn.execute(
                            "UPDATE attempts SET end_sha = NULL WHERE attempt_id = ?",
                            (rows[0]["attempt_id"],))
                asked["cancel"] = cli.cmd_ctl(run.repo, "cancel", ["TASK-001"])
                return
        elif "retry" not in asked:
            if runs()[0]["status"] != "running":
                if not kept:
                    _git(run.repo, "branch", "-D", "impl/TASK-001")
                held["routing"].resources.pool(BALANCED_POOL).resume()
                asked["retry"] = cli.cmd_ctl(run.repo, "retry", ["TASK-001"])
            return
        elif len(runs()) == 2 and runs()[1]["status"] != "running":
            run.halt()
            return
        _drive_every_worker_of_any_run(run)

    run = run_lane(_happy_scenario(
        name, routing=routing, on_poll=on_poll,
        project_yaml=_project(risky_set=["spotify.py"]),
    ))

    assert run.returncode == 0 and asked == {"cancel": 0, "retry": 0}
    first, second = _rows_for("implementer")
    assert (first["model_id"], second["model_id"]) == ("fixture-s", "fixture-b")
    return run, [r["status"] for r in runs()], held["end_sha"]


def _assert_refused_for_an_earlier_run(run, statuses) -> None:
    assert statuses == ["canceled", "failed"], _models_by_node()
    review = _rows_for("proof_review")[-1]
    assert (review["route_refusal"] or "").startswith("route_no_stronger_reviewer:")
    assert "an earlier run" in review["route_refusal"]
    assert RISKY not in _git(run.repo, "ls-tree", "-r", "--name-only", TARGET)


@pytest.mark.parametrize("kept", [True, False], ids=["branch_kept", "branch_reset"])
@pytest.mark.parametrize("ended", [True, False], ids=["end_recorded", "end_unrecorded"])
def test_a_reopened_task_is_reviewed_above_the_earlier_run_whose_commits_it_kept(
    run_lane, monkeypatch, kept, ended
):
    """P13 W8 review F4 (D10 edge 2): a canceled or failed run keeps `impl/<task>`, and a
    fresh run of the task checks that branch out. The risky commit an earlier run's strong
    writer left there is still what the review covers, so no `strong` reviewer may gate it.
    A fresh run on a reset branch reviews only its own writer's commits.

    `end_unrecorded` is an attempt row from before `end_sha` was written, which only
    where it started and git ancestry can place."""
    agent = _WritingAgent([{RISKY: "x = 1\n"}, {RISKY: "x = 1\n", "src/app.py": "x = 1\n"}])
    run, statuses, end_sha = _reopened(
        run_lane, monkeypatch, agent, "guard_risky_reopened", kept=kept, ended=ended)

    assert end_sha, "the first run's writer recorded no end of its commits"
    if not kept:
        assert statuses == ["canceled", "succeeded"]
        assert _rows_for("proof_review")[-1]["model_id"] == "fixture-s"
        return
    # The fresh run built on the kept branch: the reviewed diff still holds the risky file.
    _assert_refused_for_an_earlier_run(run, statuses)


class _AmendingWriter(_WritingAgent):
    """`_WritingAgent` whose later implementer rounds amend the commit already there."""

    def complete(self, spec):
        if not spec.prompt.startswith("You are the implementer ") or not self.briefs("implementer"):
            return super().complete(spec)
        self.specs.append(spec)
        workspace = Path(re.search(r"^WORKSPACE: (\S+)$", spec.prompt, re.MULTILINE).group(1))
        path = spec.cwd / "src/app.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n", encoding="utf-8")
        _git(spec.cwd, "add", "-A")
        _git(spec.cwd, "commit", "--amend", "-q", "-m", "feat: the feature, amended")
        handlers.write_sentinel(workspace, handlers.DONE_SENTINEL, "done")
        return Completion(text="done", returncode=0, timed_out=False)


@pytest.mark.parametrize("ended", [True, False], ids=["end_recorded", "end_unrecorded"])
def test_an_earlier_run_writer_whose_commit_a_weaker_model_amended_is_counted(
    run_lane, monkeypatch, ended
):
    """P13 W8 review R1 (D10): `git commit --amend` is allowed to a writer, and it replaces
    the earlier run's commit with one under the amending attempt. The tree no longer holds
    a commit the strong writer made, and it holds everything that writer wrote."""
    run, statuses, end_sha = _reopened(
        run_lane, monkeypatch, _AmendingWriter({RISKY: "x = 1\n"}), "guard_risky_amended",
        ended=ended)

    _assert_refused_for_an_earlier_run(run, statuses)
    assert end_sha and end_sha not in _git(run.repo, "rev-list", "impl/TASK-001").split()


def test_an_earlier_run_writer_whose_uncommitted_edit_the_next_run_commits_is_counted(
    run_lane, monkeypatch
):
    """P13 W8 review R2 (D10): a settled run keeps a tree with uncommitted changes, and
    the task's next run works in it. What the earlier run's strong writer left there is
    committed by the next run's writer, on a branch that was ahead of nothing."""
    agent = _WriterThatForgetsToCommit([{RISKY: "x = 1\n"}, {"src/app.py": "x = 1\n"}])
    run, statuses, _ = _reopened(run_lane, monkeypatch, agent, "guard_risky_left_dirty")

    _assert_refused_for_an_earlier_run(run, statuses)
    first, second = maestro.state.control_store().conn.execute(
        "SELECT start_sha, end_sha FROM attempts WHERE node_id = 'implementer' ORDER BY rowid"
    ).fetchall()
    assert first["start_sha"] == second["start_sha"] and first["end_sha"] is None


def test_a_launch_that_failed_records_that_it_committed_nothing(run_lane, monkeypatch):
    """P13 W8 review (D10): a worker that never started wrote nothing, and its attempt says
    so, or the floor would take a `failed` row with no end for a writer of all that came
    after."""
    def no_window(self, window, argv, *, cwd=None):
        return handlers.Dispatched(window=window, argv=tuple(argv), launched=False, detail="no")

    monkeypatch.setattr(handlers.TmuxDispatcher, "dispatch", no_window)

    run = run_lane(_happy_scenario("guard_launch_failed", project_yaml=_project()))

    assert run.returncode == 0
    [row] = maestro.state.control_store().conn.execute(
        "SELECT status, start_sha, end_sha FROM attempts WHERE node_id = 'implementer'"
    ).fetchall()
    assert row["status"] == "failed" and row["start_sha"]
    assert row["end_sha"] == row["start_sha"]


class _WriterThatForgetsToCommit(_WritingAgent):
    """`_WritingAgent` whose first implementer writes its files and reports done, uncommitted."""

    def complete(self, spec):
        if not spec.prompt.startswith("You are the implementer ") or self.briefs("implementer"):
            return super().complete(spec)
        self.specs.append(spec)
        workspace = Path(re.search(r"^WORKSPACE: (\S+)$", spec.prompt, re.MULTILINE).group(1))
        for rel, text in {FEATURE: "done\n", **self.rounds[0]}.items():
            path = spec.cwd / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        handlers.write_sentinel(workspace, handlers.DONE_SENTINEL, "done")
        return Completion(text="done", returncode=0, timed_out=False)


def test_a_succeeded_writer_whose_uncommitted_edit_a_weaker_rework_commits_is_counted(
    run_lane, monkeypatch
):
    """P13 W8 review R1 (D10): a writer that reports done with its risky edit uncommitted
    succeeds having made no commit. The gate refuses the uncommitted tree, and the rework,
    on a weaker route by then, commits that edit as its own (`git add -A`). A succeeded
    writer counts for the tree it left, whoever committed it."""
    agent = _WriterThatForgetsToCommit([{RISKY: "x = 1\n"}, {"src/app.py": "x = 1\n"}])
    _patch_fixture_cli_driver(monkeypatch, agent)
    held: dict = {}

    def routing():
        if "routing" not in held:
            held["routing"] = _a_pool_each(balanced_paused=True)
        return held["routing"]

    def on_poll(run) -> None:
        rows = _rows_for("implementer")
        if rows and rows[0]["status"] == "succeeded":
            held["routing"].resources.pool(BALANCED_POOL).resume()
        # Refused and parked, or a review let the tree through: the asserts say which.
        if _journal_details("graph_run_settled") or any(
            a["node_id"] == "merge" for a in _attempts()
        ):
            run.halt()
            return
        _drive_every_worker_of_any_run(run)

    run = run_lane(_happy_scenario(
        "guard_risky_uncommitted", routing=routing, on_poll=on_poll,
        project_yaml=_project(risky_set=["spotify.py"]),
    ))

    assert run.returncode == 0
    forgot, redone = _rows_for("implementer")
    assert (forgot["model_id"], forgot["status"]) == ("fixture-s", "succeeded")
    assert (redone["model_id"], redone["status"]) == ("fixture-b", "succeeded")
    assert RISKY in _git(run.repo, "ls-tree", "-r", "--name-only", "impl/TASK-001")
    assert _run_status() == "failed", _models_by_node()
    review = _rows_for("proof_review")[-1]
    assert (review["route_refusal"] or "").startswith("route_no_stronger_reviewer:")
    assert RISKY not in _git(run.repo, "ls-tree", "-r", "--name-only", TARGET)


class _AskingWriter(_WritingAgent):
    """`_WritingAgent` whose implementer first writes `started` (and commits it, unless
    `commit` is false) and stops to ask Dan."""

    def __init__(self, started, files, commit=True):
        super().__init__(files)
        self.started = started
        self.commit = commit

    def complete(self, spec):
        if self.started is None or not spec.prompt.startswith("You are the implementer "):
            return super().complete(spec)
        self.specs.append(spec)
        workspace = Path(re.search(r"^WORKSPACE: (\S+)$", spec.prompt, re.MULTILINE).group(1))
        for rel, text in self.started.items():
            path = spec.cwd / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        if self.commit:
            _git(spec.cwd, "add", "-A")
            _git(spec.cwd, "commit", "-q", "-m", "feat: a start, then a question")
        (workspace / dan_questions.QUESTION_FILE).write_text(
            json.dumps(_SPEC_QUESTION), encoding="utf-8")
        self.started = None
        return Completion(text="asked", returncode=0, timed_out=False)


@pytest.mark.parametrize("commit", [True, False], ids=["committed", "uncommitted"])
def test_a_writer_that_stopped_to_ask_dan_is_counted_when_a_weaker_model_finishes(
    run_lane, monkeypatch, commit
):
    """P13 W8 (D10): a writer that stops to ask Dan keeps what it committed (D26), and its
    attempt is `failed`. When the route changes before the attempt that finishes, the
    reviewed tree still holds the strong model's risky commit, so no `strong` reviewer may
    gate it.

    `uncommitted` (review R2): it keeps what it left uncommitted too, and the attempt that
    finishes commits that as its own."""
    from maestro.docs import decisions

    agent = _AskingWriter({RISKY: "x = 1\n"}, {"src/app.py": "x = 1\n"}, commit=commit)
    _patch_fixture_cli_driver(monkeypatch, agent)
    dan = _DanAnswers([("tap", 0)])
    monkeypatch.setattr(verify, "send_via_bot", dan.send)
    monkeypatch.setattr(verify, "telegram_configured", lambda: True)
    monkeypatch.setattr(decisions, "_adr_agent", lambda prompt, cwd: "")
    held: dict = {}

    def routing():
        if "routing" not in held:
            held["routing"] = _a_pool_each(balanced_paused=True)
        return held["routing"]

    def on_poll(run) -> None:
        rows = _rows_for("implementer")
        if rows and rows[0]["status"] == "failed":
            # The cheaper model's pool resets while Dan answers.
            held["routing"].resources.pool(BALANCED_POOL).resume()
        dan.on_poll(run)

    run = run_lane(_happy_scenario(
        "guard_risky_asked_dan", routing=routing, on_poll=on_poll,
        project_yaml=_project(risky_set=["spotify.py"]),
    ))

    assert run.returncode == 0
    asked, finished = _rows_for("implementer")
    assert (asked["model_id"], asked["failure_kind"]) == ("fixture-s", "dan_question")
    assert (finished["model_id"], finished["status"]) == ("fixture-b", "succeeded")
    assert _run_status() == "failed", _models_by_node()
    review = _rows_for("proof_review")[-1]
    assert (review["route_refusal"] or "").startswith("route_no_stronger_reviewer:")
    assert RISKY not in _git(run.repo, "ls-tree", "-r", "--name-only", TARGET)


class _WriterWithABadFirstOutput(_WritingAgent):
    """`_WritingAgent` whose first implementer commits its files and reports nothing."""

    def complete(self, spec):
        first = spec.prompt.startswith("You are the implementer ") and not self.briefs("implementer")
        completion = super().complete(spec)
        if first:
            workspace = Path(re.search(r"^WORKSPACE: (\S+)$", spec.prompt, re.MULTILINE).group(1))
            (workspace / handlers.DONE_SENTINEL).unlink()
        return completion


def test_an_earlier_run_writer_whose_retry_restored_the_tree_is_not_counted(run_lane, monkeypatch):
    """P13 W8 (D10): a bad output is retried from the attempt's `start_sha`, so the strong
    model's risky commit is gone from the branch a later run of the task builds on. Only
    the balanced model's commits are reviewed, and a `strong` reviewer may gate them."""
    from maestro import cli

    # The retry writes other text: the same tree on the same parent in the same second
    # would be the very commit the first attempt made.
    redo = {RISKY: "x = 2\n"}
    agent = _WriterWithABadFirstOutput([{RISKY: "x = 1\n"}, redo, {**redo, "src/app.py": "x = 1\n"}])
    _patch_fixture_cli_driver(monkeypatch, agent)
    held: dict = {}
    asked: dict = {}

    def routing():
        if "routing" not in held:
            held["routing"] = _a_pool_each(balanced_paused=True)
        return held["routing"]

    def runs() -> list:
        return maestro.state.control_store().conn.execute(
            "SELECT run_id, status FROM task_runs ORDER BY started_at, rowid").fetchall()

    def on_poll(run) -> None:
        rows = _rows_for("implementer")
        if rows:
            # The cheaper model's pool resets while the strong one works: a bad output is
            # retried in the tick that settles it.
            held["routing"].resources.pool(BALANCED_POOL).resume()
        if "cancel" not in asked:
            if len(rows) == 2 and rows[1]["status"] == "succeeded":
                asked["cancel"] = cli.cmd_ctl(run.repo, "cancel", ["TASK-001"])
                return
        elif "retry" not in asked:
            if runs()[0]["status"] != "running":
                held["kept"] = _git(run.repo, "rev-list", "impl/TASK-001").split()
                asked["retry"] = cli.cmd_ctl(run.repo, "retry", ["TASK-001"])
            return
        elif len(runs()) == 2 and runs()[1]["status"] != "running":
            run.halt()
            return
        _drive_every_worker_of_any_run(run)

    run = run_lane(_happy_scenario(
        "guard_risky_restored", routing=routing, on_poll=on_poll,
        project_yaml=_project(risky_set=["spotify.py"]),
    ))

    assert run.returncode == 0 and asked == {"cancel": 0, "retry": 0}
    restored, redone, reopened = _rows_for("implementer")
    assert (restored["model_id"], restored["failure_kind"]) == ("fixture-s", "no_sentinel")
    assert [r["model_id"] for r in (redone, reopened)] == ["fixture-b", "fixture-b"]
    assert [r["status"] for r in runs()] == ["canceled", "succeeded"], _models_by_node()
    assert _rows_for("proof_review")[-1]["model_id"] == "fixture-s"
    assert RISKY in _git(run.repo, "ls-tree", "-r", "--name-only", TARGET)
    # The strong attempt committed, the retry's restore took that off the branch the fresh
    # run kept, and the attempt's row says it left nothing.
    start_sha, end_sha = maestro.state.control_store().conn.execute(
        "SELECT start_sha, end_sha FROM attempts WHERE attempt_id = ?", (restored["attempt_id"],)
    ).fetchone()
    assert end_sha == start_sha and held["kept"][0] != start_sha
    assert RISKY not in _git(run.repo, "ls-tree", "-r", "--name-only", start_sha)


# ── a widened run: the floor reads the winner's tree ────────────────────────────────
#
# The lanes above have one writer and one tree. A widened run has a tree and a branch per
# candidate, so these drive a real `WorkflowRunner` on the compiled widened graph, over a
# real store and repository with the lanes' routing: every attempt row is the runner's own.


def _reports_done(run, node_id: str, *, checks: int = 0) -> None:
    """`node_id`'s claimed worker reports done: a candidate, with `checks` passing checks
    on the commit its tree stands at."""
    attempt_id = run.store.conn.execute(
        "SELECT attempt_id FROM attempts WHERE run_id = ? AND node_id = ?"
        " ORDER BY rowid DESC LIMIT 1", (run.run_id, node_id),
    ).fetchone()["attempt_id"]
    outputs: dict = {}
    if checks:
        tree = run.worktree_for(run.revision.node(node_id))
        outputs = {
            "head_commit": _git(tree, "rev-parse", "HEAD"),
            "evidence": [_passing_evidence(attempt_id, f"check-{n}") for n in range(checks)],
        }
    inbox = run.inbox(attempt_id)
    inbox.write_result(NodeResult(
        run_id=run.run_id, node_id=node_id, attempt_id=attempt_id, status="succeeded",
        outputs=outputs,
    ))
    inbox.write_receipt(operation_id="", exit_status=0)


def _widened_to_its_writers(repo, store, revision, routing, before_writers=lambda: None):
    """Open a run of the widened `revision` and take it to its two claimed candidate
    writers; `before_writers` runs ahead of the tick that routes them. Returns the runner
    and `c2`'s tree."""
    run = wf_runner.start_run(
        store, revision, repo=repo, routing=routing,
        dispatcher=TmuxDispatcher(runner=_FakeTmux()),
        artifacts=ArtifactStore(repo / ".orchestrator" / "artifacts", store),
    )
    for upstream in ("planner", "test_designer"):
        run.tick()
        _reports_done(run, upstream)
    before_writers()
    run.tick()
    return run, run.worktree_for(revision.node("implementer_c2"))


def test_an_earlier_run_on_the_winning_candidates_branch_is_counted_past_a_fresh_loser(tmp_path):
    """P13 W8 review (D10): each candidate of a widened run has a branch of its own, and a
    fresh run of the task builds on whichever of them an earlier run left. Whether the
    reviewed tree was kept is read where the winner's writer started: a losing candidate
    that started first, on a branch still at the base, says nothing about it.

    The canceled run's strong `c2` committed the risky edit and its `c1` committed nothing.
    The fresh run's balanced `c2` amends that commit into its own and wins the join, so
    the tree holds all the strong model wrote and no commit it made."""
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "main")
    (repo / ".orchestrator").mkdir()
    store = ControlStore.open_controller(repo / ".orchestrator" / "control.sqlite3")
    routing = _a_pool_each(balanced_paused=True)
    routing.task_spec = lambda _task_id: normalize_spec(_task("TASK-001"))
    policy = default_policy().with_objective("free_quality")
    revision = compile_workflow(
        normalize_spec(_task("TASK-001")), workflow_id="wf_widened", code_base_sha=base,
        policy=dataclasses.replace(
            policy, free_quality=dataclasses.replace(policy.free_quality, candidates=2)),
    )

    earlier, tree = _widened_to_its_writers(repo, store, revision, routing)
    (tree / RISKY).parent.mkdir()
    (tree / RISKY).write_text("x = 1\n", encoding="utf-8")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-q", "-m", "feat: the risky edit")
    strong_commit = _git(tree, "rev-parse", "HEAD")
    _reports_done(earlier, "implementer_c2", checks=1)
    earlier.ingest_results()
    assert earlier.operator_cancel() == ["implementer_c1"]

    def the_strong_pool_runs_out() -> None:
        routing.resources.pool(BALANCED_POOL).resume()
        routing.resources.pool(STRONG_POOL).pause(until=10**12, reason="weekly limit")

    fresh, tree = _widened_to_its_writers(repo, store, revision, routing, the_strong_pool_runs_out)
    (tree / "src/app.py").write_text("x = 1\n", encoding="utf-8")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-q", "--amend", "-m", "feat: the risky edit, amended")
    _reports_done(fresh, "implementer_c1", checks=1)
    _reports_done(fresh, "implementer_c2", checks=2)
    fresh.tick()  # both candidates are ingested and the join runs
    fresh.ingest_results()

    assert fresh.candidate_winner() == "c2", fresh.phases()
    idle, strong, loser, winner = store.conn.execute(
        "SELECT node_id, model_id, start_sha, end_sha FROM attempts"
        " WHERE node_id LIKE 'implementer_%' ORDER BY rowid").fetchall()
    assert tuple(idle) == ("implementer_c1", "fixture-s", base, base)
    assert tuple(strong) == ("implementer_c2", "fixture-s", base, strong_commit)
    # The loser started first, on a branch still at the base; the winner, on the weaker
    # model, on the branch the task kept.
    assert tuple(loser)[:3] == ("implementer_c1", "fixture-b", base)
    assert tuple(winner)[:3] == ("implementer_c2", "fixture-b", strong_commit)
    assert strong_commit not in _git(tree, "rev-list", "HEAD").split()
    assert RISKY in _git(tree, "ls-tree", "-r", "--name-only", "HEAD")

    # W4: the retained/amended author and the finisher both determine independence;
    # neither candidate tree inherits the losing candidate's independent family.
    with store.conn:
        store.conn.execute("UPDATE attempts SET binding_json=json_set(binding_json, '$.model_family',"
            " CASE WHEN node_id='implementer_c1' THEN 'loser'"
            " WHEN model_id='fixture-s' THEN 'claude' ELSE 'gemini' END)"
            " WHERE node_id LIKE 'implementer_%'")
    assert fresh._independent_families(revision.node('proof_review_logic')) == {'claude', 'gemini'}

    floor, refusal = fresh._review_strength_floor(revision.node("proof_review_logic"))
    assert floor is None and refusal.startswith("route_no_stronger_reviewer:"), (floor, refusal)
    assert "an earlier run" in refusal
    store.close()


def test_an_uncommitted_edit_of_a_run_a_plan_repair_replaced_is_counted(tmp_path):
    """P13 W8 second review (D10): a run a plan repair replaced never settles, so nothing
    it recorded says what it left in the tree. Its strong `c2` leaves the risky edit
    uncommitted; the run that replaced it is canceled and the tree is kept as it stands.
    The fresh run's balanced `c2` commits what it found there (`git add -A`). What says so
    is the tree the fresh run's writer was handed, not a fact about any one earlier run."""
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "main")
    (repo / ".orchestrator").mkdir()
    store = ControlStore.open_controller(repo / ".orchestrator" / "control.sqlite3")
    routing = _a_pool_each(balanced_paused=True)
    routing.task_spec = lambda _task_id: normalize_spec(_task("TASK-001"))
    policy = default_policy().with_objective("free_quality")
    revision = compile_workflow(
        normalize_spec(_task("TASK-001")), workflow_id="wf_widened", code_base_sha=base,
        policy=dataclasses.replace(
            policy, free_quality=dataclasses.replace(policy.free_quality, candidates=2)),
    )

    replaced, tree = _widened_to_its_writers(repo, store, revision, routing)
    (tree / RISKY).parent.mkdir()
    (tree / RISKY).write_text("x = 1\n", encoding="utf-8")       # never committed
    _reports_done(replaced, "implementer_c1", checks=1)
    _reports_done(replaced, "implementer_c2", checks=1)
    replaced.ingest_results()
    proposal = compiler.PlanRepairProposal(
        workflow_id=revision.workflow_id, base_revision=revision.revision,
        kind="partial_failure", rerun_nodes=("implementer_c2",), reason="a repair",
    )
    plan = compiler.plan_repair(
        revision, proposal, replaced.phases(),
        counters=compiler.repair_counters(store, revision.workflow_id, run_id=replaced.run_id),
    )
    successor = compiler.apply_repair(
        store, plan, old_run_id=replaced.run_id, lifecycle=replaced.lifecycle)
    replaced.spawn(successor, plan.revision).operator_cancel()

    def the_strong_pool_runs_out() -> None:
        routing.resources.pool(BALANCED_POOL).resume()
        routing.resources.pool(STRONG_POOL).pause(until=10**12, reason="weekly limit")

    fresh, tree = _widened_to_its_writers(repo, store, revision, routing, the_strong_pool_runs_out)
    assert (tree / RISKY).exists(), "the kept tree lost the uncommitted edit"
    _git(tree, "add", "-A")
    _git(tree, "commit", "-q", "-m", "feat: what was there, committed")
    _reports_done(fresh, "implementer_c1", checks=1)
    _reports_done(fresh, "implementer_c2", checks=2)
    fresh.tick()
    fresh.ingest_results()

    assert fresh.candidate_winner() == "c2", fresh.phases()
    strong = store.conn.execute(
        "SELECT model_id, status, start_sha, end_sha FROM attempts"
        " WHERE run_id = ? AND node_id = 'implementer_c2'", (replaced.run_id,)).fetchone()
    assert tuple(strong) == ("fixture-s", "succeeded", base, None)
    with store.conn:
        store.conn.execute("UPDATE attempts SET binding_json=json_set(binding_json, '$.model_family',"
            " CASE WHEN node_id='implementer_c1' THEN 'loser'"
            " WHEN model_id='fixture-s' THEN 'claude' ELSE 'gemini' END)"
            " WHERE node_id LIKE 'implementer_%'")
    assert fresh._independent_families(revision.node('proof_review_logic')) == {'claude', 'gemini'}
    floor, refusal = fresh._review_strength_floor(revision.node("proof_review_logic"))
    assert floor is None and refusal.startswith("route_no_stronger_reviewer:"), (floor, refusal)
    store.close()


# ── a later writer rewrites an earlier one's commit ─────────────────────────────────
#
# `git commit --amend` is allowed to a writer. It leaves the tree holding everything the
# earlier attempt wrote and none of the commits it made, whatever that attempt's status.


class _AskThenAmend(_WritingAgent):
    """The first implementer commits the risky file and asks Dan; each later one amends
    the commit it finds on the branch and reports done."""

    def __init__(self):
        super().__init__({})
        self.calls = 0

    def complete(self, spec):
        if not spec.prompt.startswith("You are the implementer "):
            return super().complete(spec)
        self.specs.append(spec)
        self.calls += 1
        workspace = Path(re.search(r"^WORKSPACE: (\S+)$", spec.prompt, re.MULTILINE).group(1))
        files = {RISKY: "x = 1\n"} if self.calls == 1 else {
            FEATURE: "done\n", "src/app.py": f"x = {self.calls}\n"}
        for rel, text in files.items():
            path = spec.cwd / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        _git(spec.cwd, "add", "-A")
        if self.calls == 1:
            _git(spec.cwd, "commit", "-q", "-m", "feat: a start, then a question")
            (workspace / dan_questions.QUESTION_FILE).write_text(
                json.dumps(_SPEC_QUESTION), encoding="utf-8")
            return Completion(text="asked", returncode=0, timed_out=False)
        _git(spec.cwd, "commit", "--amend", "-q", "-m", f"feat: amended by round {self.calls}")
        handlers.write_sentinel(workspace, handlers.DONE_SENTINEL, "done")
        return Completion(text="done", returncode=0, timed_out=False)


def _dan_taps(monkeypatch) -> _DanAnswers:
    from maestro.docs import decisions

    dan = _DanAnswers([("tap", 0)])
    monkeypatch.setattr(verify, "send_via_bot", dan.send)
    monkeypatch.setattr(verify, "telegram_configured", lambda: True)
    monkeypatch.setattr(decisions, "_adr_agent", lambda prompt, cwd: "")
    return dan


def _runs() -> list:
    return maestro.state.control_store().conn.execute(
        "SELECT run_id, status FROM task_runs ORDER BY started_at, rowid").fetchall()


def _risky_lane(run_lane, name, held, on_poll, *, balanced_paused=True):
    def routing():
        if "routing" not in held:
            held["routing"] = _a_pool_each(balanced_paused=balanced_paused)
        return held["routing"]

    return run_lane(_happy_scenario(
        name, routing=routing, on_poll=on_poll, project_yaml=_project(risky_set=["spotify.py"]),
    ))


def test_a_writer_that_asked_dan_is_counted_when_a_weaker_finisher_amends_its_commit(
    run_lane, monkeypatch
):
    """P13 W8 second review (D10): the strong writer commits the risky file and asks Dan
    (D26 keeps the commit; the attempt is `failed` with its end recorded). The balanced
    attempt that finishes amends that commit into its own."""
    _patch_fixture_cli_driver(monkeypatch, _AskThenAmend())
    dan = _dan_taps(monkeypatch)
    held: dict = {}

    def on_poll(run) -> None:
        rows = _rows_for("implementer")
        if rows and rows[0]["status"] == "failed":
            held["routing"].resources.pool(BALANCED_POOL).resume()
        dan.on_poll(run)

    run = _risky_lane(run_lane, "guard_risky_asked_amended", held, on_poll)

    assert run.returncode == 0
    asked, finished = _rows_for("implementer")
    assert (asked["model_id"], asked["failure_kind"]) == ("fixture-s", "dan_question")
    assert (finished["model_id"], finished["status"]) == ("fixture-b", "succeeded")
    assert _run_status() == "failed", _models_by_node()
    review = _rows_for("proof_review")[-1]
    assert (review["route_refusal"] or "").startswith("route_no_stronger_reviewer:")
    assert RISKY not in _git(run.repo, "ls-tree", "-r", "--name-only", TARGET)


def test_an_earlier_run_writer_that_asked_dan_is_counted_when_the_next_run_amends_its_commit(
    run_lane, monkeypatch
):
    """P13 W8 second review (D10): the same across runs. The run is canceled while the
    strong writer waits for Dan (a clean tree, the branch kept), and the fresh run's
    balanced writer amends the commit it finds."""
    from maestro import cli

    monkeypatch.setattr(lane_conftest, "MAX_POLLS", 40)
    _patch_fixture_cli_driver(monkeypatch, _AskThenAmend())
    _dan_taps(monkeypatch).replies.clear()       # Dan never answers
    held: dict = {}
    asked: dict = {}

    def on_poll(run) -> None:
        if "cancel" not in asked:
            rows = _rows_for("implementer")
            if rows and rows[0]["status"] == "failed":
                asked["cancel"] = cli.cmd_ctl(run.repo, "cancel", ["TASK-001"])
                return
        elif "retry" not in asked:
            if _runs()[0]["status"] != "running":
                held["routing"].resources.pool(BALANCED_POOL).resume()
                asked["retry"] = cli.cmd_ctl(run.repo, "retry", ["TASK-001"])
            return
        elif len(_runs()) == 2 and _runs()[1]["status"] != "running":
            run.halt()
            return
        _drive_every_worker_of_any_run(run)

    run = _risky_lane(run_lane, "guard_risky_asked_canceled_amended", held, on_poll)

    assert run.returncode == 0 and asked == {"cancel": 0, "retry": 0}
    first, second = _rows_for("implementer")
    assert (first["model_id"], first["failure_kind"]) == ("fixture-s", "dan_question")
    assert (second["model_id"], second["status"]) == ("fixture-b", "succeeded")
    _assert_refused_for_an_earlier_run(run, [r["status"] for r in _runs()])


def test_an_earlier_run_writer_is_counted_when_the_commit_it_started_from_is_amended_away(
    run_lane, monkeypatch
):
    """P13 W8 second review (D10): an earlier attempt is placed by where it started, and an
    amend can remove that commit too. Run 1: the balanced writer commits and asks Dan; the
    strong attempt that finishes amends that commit, and the review is refused. Run 2: the
    balanced writer amends again. Neither the strong attempt's commit nor the one it
    started from is on the branch."""
    from maestro import cli

    monkeypatch.setattr(lane_conftest, "MAX_POLLS", 40)
    _patch_fixture_cli_driver(monkeypatch, _AskThenAmend())
    dan = _dan_taps(monkeypatch)
    held: dict = {}
    asked: dict = {}

    def on_poll(run) -> None:
        rows = _rows_for("implementer")
        if len(_runs()) == 1 and _runs()[0]["status"] == "running":
            if len(rows) == 1 and rows[0]["status"] == "failed":
                # The balanced pool runs out while Dan answers: the strong model finishes.
                held["routing"].resources.pool(BALANCED_POOL).pause(
                    until=10**12, reason="weekly limit")
            dan.answer()
        elif "retry" not in asked:
            held["routing"].resources.pool(BALANCED_POOL).resume()
            asked["retry"] = cli.cmd_ctl(run.repo, "retry", ["TASK-001"])
            return
        elif len(_runs()) == 2 and _runs()[1]["status"] != "running":
            run.halt()
            return
        _drive_every_worker_of_any_run(run)

    run = _risky_lane(
        run_lane, "guard_risky_start_amended_away", held, on_poll, balanced_paused=False)

    assert run.returncode == 0 and asked == {"retry": 0}
    one, two, three = _rows_for("implementer")
    assert (one["model_id"], one["failure_kind"]) == ("fixture-b", "dan_question")
    assert (two["model_id"], two["status"]) == ("fixture-s", "succeeded")
    assert (three["model_id"], three["status"]) == ("fixture-b", "succeeded")
    assert [r["status"] for r in _runs()] == ["failed", "failed"], _models_by_node()
    review = _rows_for("proof_review")[-1]
    assert (review["route_refusal"] or "").startswith("route_no_stronger_reviewer:")
    assert RISKY not in _git(run.repo, "ls-tree", "-r", "--name-only", TARGET)
    started = maestro.state.control_store().conn.execute(
        "SELECT start_sha FROM attempts WHERE attempt_id = ?", (two["attempt_id"],)
    ).fetchone()["start_sha"]
    assert started not in _git(run.repo, "rev-list", "impl/TASK-001").split()


class _AskThenIntegrateTarget(_WritingAgent):
    """The first implementer writes the risky file (committed, unless `commit` is false)
    and asks Dan; the second brings the moved target into the branch (`how`: rebase or
    merge) and commits the feature; later ones add a commit."""

    def __init__(self, how: str, commit: bool = True):
        super().__init__({})
        self.calls = 0
        self.how = how
        self.commit = commit

    def complete(self, spec):
        if not spec.prompt.startswith("You are the implementer "):
            return super().complete(spec)
        self.specs.append(spec)
        self.calls += 1
        workspace = Path(re.search(r"^WORKSPACE: (\S+)$", spec.prompt, re.MULTILINE).group(1))
        if self.calls == 1:
            path = spec.cwd / RISKY
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x = 1\n", encoding="utf-8")
            if self.commit:
                _git(spec.cwd, "add", "-A")
                _git(spec.cwd, "commit", "-q", "-m", "feat: a start, then a question")
            (workspace / dan_questions.QUESTION_FILE).write_text(
                json.dumps(_SPEC_QUESTION), encoding="utf-8")
            return Completion(text="asked", returncode=0, timed_out=False)
        if self.calls == 2:
            if not self.commit:
                _git(spec.cwd, "add", "-A")
                _git(spec.cwd, "commit", "-q", "-m", "feat: what was there")
            if self.how == "rebase":
                _git(spec.cwd, "rebase", "-q", TARGET)
            else:
                _git(spec.cwd, "merge", "-q", "--no-edit", TARGET)
        for rel, text in {FEATURE: "done\n", f"src/app{self.calls}.py": "x = 1\n"}.items():
            path = spec.cwd / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        _git(spec.cwd, "add", "-A")
        _git(spec.cwd, "commit", "-q", "-m", f"feat: round {self.calls}")
        handlers.write_sentinel(workspace, handlers.DONE_SENTINEL, "done")
        return Completion(text="done", returncode=0, timed_out=False)


@pytest.mark.parametrize(
    "how, commit", [("rebase", True), ("merge", False)], ids=["rebased", "uncommitted_merged"])
def test_an_earlier_run_writer_is_counted_after_the_moved_target_is_brought_into_the_branch(
    run_lane, monkeypatch, how, commit
):
    """P13 W8 third review (D10): the target moves while the strong writer waits for Dan
    (another task lands), and the balanced attempt that finishes rebases the kept branch
    onto it, or merges it in. The review is refused, and the task is retried. The fork
    point is now above where the strong attempt started, on the base's own history, which
    is also where a reset branch leaves an earlier run's start: that alone cannot say the
    attempt's work is gone."""
    from maestro import cli

    monkeypatch.setattr(lane_conftest, "MAX_POLLS", 40)
    _patch_fixture_cli_driver(monkeypatch, _AskThenIntegrateTarget(how, commit))
    dan = _dan_taps(monkeypatch)
    held: dict = {}
    asked: dict = {}

    def on_poll(run) -> None:
        rows = _rows_for("implementer")
        if len(_runs()) == 1 and _runs()[0]["status"] == "running":
            if len(rows) == 1 and rows[0]["status"] == "failed":
                # The cheaper pool resets while Dan answers, and another task lands.
                held["routing"].resources.pool(BALANCED_POOL).resume()
                if "landed" not in asked:
                    _git(run.repo, "commit", "--allow-empty", "-q", "-m", "another task landed")
                    asked["landed"] = True
            dan.answer()
        elif "retry" not in asked:
            asked["retry"] = cli.cmd_ctl(run.repo, "retry", ["TASK-001"])
            return
        elif len(_runs()) == 2 and _runs()[1]["status"] != "running":
            run.halt()
            return
        _drive_every_worker_of_any_run(run)

    run = _risky_lane(run_lane, f"guard_risky_target_{how}", held, on_poll)

    assert run.returncode == 0 and asked == {"landed": True, "retry": 0}
    one, two, three = _rows_for("implementer")
    assert (one["model_id"], one["failure_kind"]) == ("fixture-s", "dan_question")
    assert (two["model_id"], two["status"]) == ("fixture-b", "succeeded")
    assert (three["model_id"], three["status"]) == ("fixture-b", "succeeded")
    assert [r["status"] for r in _runs()] == ["failed", "failed"], _models_by_node()
    review = _rows_for("proof_review")[-1]
    assert (review["route_refusal"] or "").startswith("route_no_stronger_reviewer:")
    assert RISKY not in _git(run.repo, "ls-tree", "-r", "--name-only", TARGET)


def test_a_tree_that_could_not_be_prepared_records_nothing_about_what_was_found(
    run_lane, monkeypatch
):
    """P13 W8 third review (D10): what a run's first writer was handed is recorded once and
    read for the whole lineage, so it is recorded only for a tree that was handed over."""
    from maestro.workflows import task_tree

    def no_tree(*args, **kwargs):
        raise RuntimeError("the worktree directory is not a registered worktree")

    monkeypatch.setattr(task_tree, "ensure", no_tree)

    run = run_lane(_happy_scenario("guard_tree_unavailable", project_yaml=_project()))

    assert run.returncode == 0
    assert _rows_for("implementer")[0]["status"] == "failed"
    assert maestro.state.control_store().conn.execute(
        "SELECT COUNT(*) FROM events WHERE type = ?", (wf_runner.TASK_TREE_PREPARED_EVENT,)
    ).fetchone()[0] == 0


# ── nothing of an earlier writer is left ────────────────────────────────────────────


class _WriterThatDiesWithUncommittedEdits(_WritingAgent):
    """`_WritingAgent` whose first implementer leaves a risky draft uncommitted and reports
    nothing."""

    def complete(self, spec):
        if not spec.prompt.startswith("You are the implementer ") or self.briefs("implementer"):
            return super().complete(spec)
        self.specs.append(spec)
        path = spec.cwd / RISKY
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 'strong draft'\n", encoding="utf-8")
        return Completion(text="", returncode=0, timed_out=False)


def test_a_writer_whose_uncommitted_edits_its_retry_discarded_is_not_counted(
    run_lane, monkeypatch
):
    """P13 W8 second review (D10): a bad output's retry restores the tree (`reset --hard`,
    `clean -fd`), so nothing of the strong draft is left for the balanced retry to commit,
    and a `strong` reviewer may gate what it writes."""
    _patch_fixture_cli_driver(monkeypatch, _WriterThatDiesWithUncommittedEdits([{RISKY: "x = 2\n"}]))
    held: dict = {}

    def on_poll(run) -> None:
        if _rows_for("implementer"):
            held["routing"].resources.pool(BALANCED_POOL).resume()
        if _journal_details("graph_run_settled"):
            run.halt()
            return
        _drive_every_worker_of_any_run(run)

    run = _risky_lane(run_lane, "guard_risky_draft_discarded", held, on_poll)

    assert run.returncode == 0
    died, redone = _rows_for("implementer")[:2]
    assert (died["model_id"], died["status"]) == ("fixture-s", "failed")
    assert (redone["model_id"], redone["status"]) == ("fixture-b", "succeeded")
    assert _run_status() == "succeeded", _models_by_node()
    assert _rows_for("proof_review")[-1]["model_id"] == "fixture-s"
    assert _git(run.repo, "show", f"{TARGET}:{RISKY}") == "x = 2"


def test_an_earlier_run_whose_uncommitted_tree_was_thrown_away_is_not_counted(
    run_lane, monkeypatch
):
    """P13 W8 second review (D10): a canceled run's strong writer left its edit uncommitted,
    and the tree and the branch were removed before the task ran again. The fresh run
    starts from the base and holds nothing of it."""
    from maestro import cli
    from maestro.workflows import task_tree

    agent = _WriterThatForgetsToCommit([{RISKY: "x = 1\n"}, {RISKY: "x = 2\n"}])
    _patch_fixture_cli_driver(monkeypatch, agent)
    held: dict = {}
    asked: dict = {}

    def on_poll(run) -> None:
        if "cancel" not in asked:
            rows = _rows_for("implementer")
            if rows and rows[0]["status"] == "succeeded":
                asked["cancel"] = cli.cmd_ctl(run.repo, "cancel", ["TASK-001"])
                return
        elif "retry" not in asked:
            if _runs()[0]["status"] != "running":
                tree = task_tree.path_for(run.repo, "TASK-001")
                assert tree.exists(), "the tree with uncommitted changes was not kept"
                _git(run.repo, "worktree", "remove", "--force", str(tree))
                _git(run.repo, "branch", "-D", "impl/TASK-001")
                held["routing"].resources.pool(BALANCED_POOL).resume()
                asked["retry"] = cli.cmd_ctl(run.repo, "retry", ["TASK-001"])
            return
        elif len(_runs()) == 2 and _runs()[1]["status"] != "running":
            run.halt()
            return
        _drive_every_worker_of_any_run(run)

    run = _risky_lane(run_lane, "guard_risky_thrown_away", held, on_poll)

    assert run.returncode == 0 and asked == {"cancel": 0, "retry": 0}
    first, second = _rows_for("implementer")
    assert (first["model_id"], second["model_id"]) == ("fixture-s", "fixture-b")
    assert [r["status"] for r in _runs()] == ["canceled", "succeeded"], _models_by_node()


# ── unit: the guard itself ──────────────────────────────────────────────────────────


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@example.invalid"),
                 ("config", "user.name", "t")):
        _git(repo, *args)
    (repo / "README").write_text("seed\nDELETE FROM legacy;\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _commit_on_branch(repo: Path, branch: str, files: dict[str, str | None]) -> str:
    _git(repo, "checkout", "-q", "-B", branch, "main")
    for rel, text in files.items():
        path = repo / rel
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", branch)
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return head


def test_the_guard_diffs_against_the_target_merge_base_and_reads_only_additions(tmp_path):
    repo = _repo(tmp_path)
    main = _git(repo, "rev-parse", "main")
    # Removing a deny-listed line is a clean-up, not a violation.
    cleanup = _commit_on_branch(repo, "cleanup", {"README": "seed\n"})
    assert merge.integration_guard(repo, cleanup, main, proj={}) == ""
    # The target moving on (with a deny-listed line of its own) is not the candidate's diff.
    other = _commit_on_branch(repo, "other", {"b.txt": "ok\n"})
    _git(repo, "checkout", "-q", "main")
    (repo / "c.sql").write_text("DROP TABLE t;\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "target moves")
    assert merge.integration_guard(repo, other, _git(repo, "rev-parse", "main"), proj={}) == ""
    bad = _commit_on_branch(repo, "bad", {"run.sh": "sudo rm -rf /var\n"})
    assert merge.integration_guard(repo, bad, main, proj={}).startswith(
        "deny_list: deny-list violation: pattern"
    )


def test_the_guard_matches_glob_secrets_as_globs(tmp_path):
    repo = _repo(tmp_path)
    main = _git(repo, "rev-parse", "main")
    pem = _commit_on_branch(repo, "pem", {"deploy/key.pem": "k\n"})
    reason = merge.integration_guard(repo, pem, main, proj={"secrets": ["**/*.pem"]})
    assert reason == "deny_list: deny-list violation: secret or production file in diff: deploy/key.pem"


def test_a_risky_edit_needs_an_approval_of_that_exact_commit(tmp_path):
    repo = _repo(tmp_path)
    main = _git(repo, "rev-parse", "main")
    head = _commit_on_branch(repo, "risky", {RISKY: "x\n"})
    proj = {"risky_set": ["spotify.py"]}

    def approval(subject=""):
        return {"check_id": "proof_review", "verdict": "approved", "findings": [],
                "reviewer": "proof_review", "subject_sha": subject}

    refused = merge.integration_guard(repo, head, main, proj=proj)
    assert refused.startswith("risky_unreviewed:") and RISKY in refused
    rejected = {**approval(head), "verdict": "changes_requested", "findings": ["no"]}
    assert merge.integration_guard(repo, head, main, proj=proj, proof_reviews=[rejected])
    assert merge.integration_guard(repo, head, main, proj=proj, proof_reviews=[approval(main)])
    assert merge.integration_guard(repo, head, main, proj=proj, proof_reviews=[approval(head)]) == ""
    # A policy glob counts as risky too.
    assert merge.integration_guard(repo, head, main, proj={}, risky_globs=["src/*"]).startswith(
        "risky_unreviewed:"
    )


def test_the_reviewer_brief_names_the_risky_files():
    brief = wf_worker.agent_brief({"risky_files_touched": [RISKY]}, header="You are the reviewer")
    assert "RISKY FILES TOUCHED" in brief and f"- {RISKY}" in brief
    assert "RISKY FILES TOUCHED" not in wf_worker.agent_brief({}, header="You are the reviewer")
