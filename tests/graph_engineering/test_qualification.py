"""P12: the qualification catalog, run through the graph loop with fixture backends.

`tests/graph_engineering/fixtures/cases.json` names six task classes. Each one runs here as a
whole task under the real `orchestrator.main` (the `test_end_to_end.py` harness: a git
sandbox, workers run in-process from the recorded tmux command, fixture drivers only). The
paid-subscription fixture mode runs all six; the free, hybrid and script modes each run the
happy path once more.

Each lane leaves a record on its test item (`item._qualification`):
`scripts/graph_qualification.py` runs this module and writes those records into the P12
report. The records hold the raw per-attempt telemetry, the bytes the run left behind (B15),
and the invariants a fixture run can check. The assertions here and the report read the same
record, so the suite and the report cannot disagree.

A pass here qualifies the wiring, never a backend: every mode is reported against the
fixture it ran on (P12 §3).
"""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import pytest

import maestro.state
from maestro.workflows import handlers
from maestro.workflows import routing as wf_routing
from maestro.workflows.policy import HybridPolicy, default_policy

from .conftest import fixture_routing
from .test_dispatch_wiring import FREE_ENDPOINT as _FREE_ENDPOINT
from .test_dispatch_wiring import _journal_details, _node_phase, _strong_routing, _task, _windows
from .test_end_to_end import (
    FEATURE,
    TARGET,
    _V1,
    _CommittingAgent,
    _WritingModel,
    _drive_every_worker,
    _free_routing,
    _git,
    _happy_scenario,
    _landed,
    _run_status,
    FREE_ENDPOINT,
)
from .test_failure_ladder import _patch_fixture_cli_driver

CATALOG = Path(__file__).parent / "fixtures" / "cases.json"

#: cases.json's class names, spelled the way `taskgraph.TASK_CLASSES` spells them.
TASK_CLASS = {
    "bug": "bug",
    "feature": "feature",
    "documentation": "documentation",
    "debugging": "difficult_debugging",
    "manual-preparation": "manual_prep",
    "recovery": "recovery",
}

#: The attempt columns that hold JSON.
_JSON_COLUMNS = ("binding_json", "shape_key_json", "usage_start_json", "usage_settle_json",
                 "failure_json")


@pytest.fixture(autouse=True)
def _fresh_control_store():
    maestro.state._reset_control_store()
    yield
    maestro.state._reset_control_store()


def _cases() -> list[dict]:
    return json.loads(CATALOG.read_text(encoding="utf-8"))["tasks"]


# ── what a finished lane leaves behind ────────────────────────────────────────────────

def _rows(sql: str, *args) -> list[dict]:
    return [dict(r) for r in maestro.state.control_store().conn.execute(sql, args).fetchall()]


def _attempt_telemetry() -> list[dict]:
    """Every attempt row, raw. A NULL means "not measurable", never zero (P12A W3, G6)."""
    out = []
    for row in _rows("SELECT * FROM attempts ORDER BY rowid"):
        for column in _JSON_COLUMNS:
            if row.get(column):
                row[column] = json.loads(row[column])
        row["verification"] = {
            e["check_id"]: e["outcome"]
            for e in _rows("SELECT check_id, outcome FROM verification_evidence"
                           " WHERE attempt_id = ? ORDER BY rowid", row["attempt_id"])
        } or None
        out.append(row)
    return out


def _bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except FileNotFoundError:
                pass
    return total


def _volume(repo: Path) -> dict:
    """Bytes under `.orchestrator`, by top-level entry (B15). Worktrees are not evidence."""
    orch = repo / ".orchestrator"
    by_entry = {child.name: _bytes(child) for child in sorted(orch.iterdir())} if orch.exists() else {}
    impl_logs = sum(_bytes(p) for p in orch.rglob("impl.log")) if orch.exists() else 0
    return {
        "by_entry": by_entry,
        "impl_log": impl_logs,
        "total": sum(v for k, v in by_entry.items() if k != "worktrees"),
    }


def _inbox(run, attempt: dict) -> handlers.AttemptInbox:
    return handlers.AttemptInbox(handlers.attempts_root(run.repo), attempt["attempt_id"])


def _check(name: str, passed: bool, detail: str) -> dict:
    return {"invariant": name, "checked": True, "passed": bool(passed), "detail": detail}


def _invariants(run, *, accepted: bool) -> list[dict]:
    """The invariants a fixture run can check from what it left behind."""
    attempts = _rows("SELECT * FROM attempts")
    agent = [a for a in attempts if a["backend_id"]]
    decisions = _rows("SELECT * FROM acceptance_decisions")
    evidence = _rows("SELECT outcome FROM verification_evidence")
    intents = [i["status"] for i in _rows("SELECT status FROM operation_intents")]
    receipts = _rows("SELECT operation_id FROM operation_receipts")
    branches = [b.strip().lstrip("*+ ").strip()
                for b in _git(run.repo, "branch", "--list").splitlines() if b.strip()]
    windows = _windows(run)
    checks = [
        # In-process workers finish inside one poll, so the controller settles them before
        # it would bind the lease; the PID the worker reported is in its inbox envelope.
        _check("INV-02", all(_inbox(run, a).writer_pid() for a in agent),
               f"{len(agent)} agent attempts; each inbox names the PID that wrote"),
        # `tests/conftest.py::_no_agent_cli_ever` raises out of the lane on any real CLI.
        _check("INV-03", True, "no real agent CLI started (guarded by tests/conftest.py)"),
        _check("INV-05", all(b == TARGET or b.startswith("impl/") for b in branches)
               and (not accepted or {d["integrated_source_snapshot"] for d in decisions}
                    == {_landed(run.repo)}),
               f"branches {branches}; accepted decisions bound to {TARGET}'s head"
               " below its graduation commit"),
        _check("INV-07", "pending" not in intents and len(receipts) <= len(intents),
               f"{len(intents)} operation intents {sorted(set(intents))}, {len(receipts)} receipts;"
               " none left pending"),
        _check("INV-10", all(w.startswith("tmux new-window -t agents") for w in windows),
               f"{len(windows)} worker launches, each a window in the agents session"),
    ]
    if accepted:
        checks.append(_check(
            "INV-04", evidence and all(e["outcome"] == "passed" for e in evidence),
            f"{len(evidence)} verification evidence rows, all pass, run by the gate"))
    else:
        checks.append(_check(
            "INV-04", not decisions, "no acceptance without a receipt and a passing gate"))
    return checks


def _record(request, run, *, mode: str, case: dict | None, expected: str) -> dict:
    accepted = expected == "accepted"
    record = {
        "lane": run.lane,
        "mode": mode,
        "case_id": case["id"] if case else None,
        "task_class": case["class"] if case else None,
        "expected": expected,
        "run_status": _run_status(),
        "acceptance_decisions": len(_rows("SELECT 1 FROM acceptance_decisions")),
        "workflow_revisions": len(_rows("SELECT 1 FROM workflow_revisions")),
        "attempts": _attempt_telemetry(),
        "target_moved": FEATURE in _git(run.repo, "ls-tree", "--name-only", TARGET),
        "volume": _volume(run.repo),
        "invariants": _invariants(run, accepted=accepted),
    }
    request.node._qualification = record
    return record


def _assert_qualified(record: dict) -> None:
    failed = [c for c in record["invariants"] if not c["passed"]]
    assert not failed, failed
    if record["expected"] == "accepted":
        assert record["run_status"] == "succeeded", _journal_details("graph_run_settled")
        # Merge records a decision on the integrated tree and accept re-derives it.
        assert record["acceptance_decisions"] == 2
        assert record["target_moved"]
    else:
        assert record["acceptance_decisions"] == 0
        assert not record["target_moved"]


# ── the six classes under the paid-subscription fixture mode ──────────────────────────

def _halt_at_the_human_step(run) -> None:
    if _node_phase("human_action") not in (None, "pending", "ready"):
        run.halt()
        return
    _drive_every_worker(run)


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["class"])
def test_each_catalog_class_runs_through_the_graph_loop(run_lane, monkeypatch, request, case):
    from .test_g7_parity import _PrepAgent

    task_class = TASK_CLASS[case["class"]]
    fields = dict(short_desc=case["prompt"], verifications=_V1, task_class=task_class)
    manual = task_class == "manual_prep"
    _patch_fixture_cli_driver(monkeypatch, _PrepAgent() if manual else _CommittingAgent())
    if manual:
        fields.update(dispatch="manual", manual_action="provision the credential")

    run = run_lane(_happy_scenario(
        f"qualify_subscription_{case['class']}",
        tasks=[_task("TASK-001", **fields)],
        on_poll=_halt_at_the_human_step if manual else _drive_every_worker,
    ))

    assert run.returncode == 0
    record = _record(request, run, mode="subscription", case=case,
                     expected="awaiting_human" if manual else "accepted")
    _assert_qualified(record)
    if manual:
        assert record["run_status"] == "running"
        # Manual prep cannot graduate without an operator receipt: prep ran (it was never
        # opened at all before the graph loop read `parse_prep_tasks`), and the human step
        # holds its claim with no outcome, the way `park` does, and the run stays open.
        assert _node_phase("prep") == "succeeded"
        assert _node_phase("human_action") == "claimed"
        [human] = _rows("SELECT outcome_ref FROM attempts WHERE node_id = 'human_action'")
        assert human["outcome_ref"] is None
        [descriptor] = (run.repo / '.orchestrator/manual').glob('*.json')
        contract = json.loads(descriptor.read_text())
        assert contract['command'] == "printf 'migration ran\\n'"
        assert contract['step_check_cmd'] == 'git cat-file -e HEAD^{commit}'
        assert contract['expected_result'] == 'migration ran'
        assert not Path(contract['receipt']).exists(), 'no execution evidence before the human acts'


# ── the other fixture modes, one happy path each ──────────────────────────────────────

def test_the_free_mode_qualifies_on_its_fixture(run_lane, monkeypatch, request):
    monkeypatch.setattr("maestro.workflows.worker._model_api", lambda endpoint: _WritingModel())
    run = run_lane(_happy_scenario(
        "qualify_free",
        project_yaml={"engineering": {"runner": "graph", "free_tier_endpoints": [FREE_ENDPOINT]}},
        routing=_free_routing,
    ))
    _assert_qualified(_record(request, run, mode="free", case=None, expected="accepted"))


def test_the_hybrid_mode_qualifies_on_its_fixture(run_lane, monkeypatch, request):
    monkeypatch.setattr("maestro.workflows.worker._model_api", lambda endpoint: _WritingModel())
    _patch_fixture_cli_driver(monkeypatch, _CommittingAgent())
    balanced_free = dict(_FREE_ENDPOINT, strength="balanced")

    def hybrid_routing():
        [free] = wf_routing.endpoints_for_catalog([balanced_free])
        return fixture_routing(
            entries=[free, *_strong_routing().catalog.entries.values()],
            policy=dataclasses.replace(
                default_policy().with_objective("free_quality"),
                hybrid=HybridPolicy(paid_roles=("reviewer", "diagnoser")),
            ),
        )

    run = run_lane(_happy_scenario(
        "qualify_hybrid",
        project_yaml={"engineering": {"runner": "graph", "free_tier_endpoints": [balanced_free]}},
        routing=hybrid_routing,
    ))
    record = _record(request, run, mode="hybrid", case=None, expected="accepted")
    _assert_qualified(record)
    assert {a["backend_id"] for a in record["attempts"] if a["backend_id"]} == {
        "free_api", "fixture_cli"}


def test_the_script_mode_qualifies_on_its_fixture(run_lane, request):
    run = run_lane(_happy_scenario(
        "qualify_script",
        tasks=[_task("TASK-001", kind="script", run=f"echo done > {FEATURE}", verifications=_V1)],
    ))
    _assert_qualified(_record(request, run, mode="script", case=None, expected="accepted"))


# ── scripts/graph_qualification.py: the target it will and will not use ───────────────

def _script():
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "scripts" / "graph_qualification.py"
    spec = importlib.util.spec_from_file_location("graph_qualification", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_script_refuses_a_non_scratch_target_unless_told_it_is_a_deployment(
    tmp_path, monkeypatch
):
    script = _script()
    assert script.refuse_target(tmp_path / "fresh", deployment=False) is None
    assert script.refuse_target(tmp_path / "fresh", deployment=False) is None
    lived_in = tmp_path / "lived_in"
    lived_in.mkdir()
    (lived_in / "notes.md").write_text("mine", encoding="utf-8")
    assert "not empty" in script.refuse_target(lived_in, deployment=False)
    assert script.refuse_target(lived_in, deployment=True) is None
    (lived_in / script.MARKER).write_text("", encoding="utf-8")
    assert script.refuse_target(lived_in, deployment=False) is None
    monkeypatch.setenv("HOME", str(tmp_path))
    assert "under" in script.refuse_target(tmp_path / "projects" / "app", deployment=False)
    assert script.main(["--repo", str(tmp_path / "projects" / "app"), "--fixture-backends"]) == 2
    assert script.main(["--repo", str(tmp_path / "fresh")]) == 2
    assert not (tmp_path / "fresh").exists()
    # Every fault the P12 matrix names is still a lane in the e2e file.
    assert all(row["present"] for row in script.fault_matrix().values())
