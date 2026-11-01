#!/usr/bin/env python3
"""P12 qualification: the task catalog through the graph loop, written out as a report.

    .venv/bin/python scripts/graph_qualification.py --repo /tmp/maestro-graph-qualification --fixture-backends

`tests/graph_engineering/test_qualification.py` runs each class of the fixed catalog
(`tests/graph_engineering/fixtures/cases.json`) as a whole task under the real
`orchestrator.main`, in a git sandbox, with fixture backends only. This script runs that module
with its lane sandboxes under `--repo`, collects the record each lane leaves, and writes
`artifacts/graph-engineering/p12-qualification.json`:

* per-attempt raw telemetry (the P12A W3 attempt columns, unmeasurable values left null);
* the bytes each run left under `.orchestrator` (B15) and the retention default they set;
* the twelve invariants (INV-01..12): checked where a fixture run can check them, and
  otherwise named with the tests that cover them;
* the E1-E5 enabled/disabled table with its evidence;
* the Context Gate inspection mapping (P12 §4). The gate itself is not built here.

A fixture pass qualifies wiring, not a backend (P12 §3). Only paid subscription is to be
qualified for real use, by the real pilots (session s3); every other mode is reported as
unqualified whatever its fixture lane did.

Exit 0 only if every lane passed and every checked invariant held.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LANES = REPO / "tests" / "graph_engineering" / "test_qualification.py"
END_TO_END = REPO / "tests" / "graph_engineering" / "test_end_to_end.py"
DEFAULT_OUT = REPO / "artifacts" / "graph-engineering" / "p12-qualification.json"
#: Written into a target the script created, so a re-run may reuse it.
MARKER = ".maestro-graph-qualification"

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

INVARIANTS = {
    "INV-01": "SINGLE_WRITER_AUTHORITY", "INV-02": "PROCESS_LEASE_BINDING",
    "INV-03": "CREDENTIAL_BOUNDARY", "INV-04": "CLOSED_WORLD_VERIFICATION",
    "INV-05": "TARGET_BRANCH_PROTECTION", "INV-06": "NO_CROSS_CANDIDATE_POLLUTION",
    "INV-07": "TRANSACTIONAL_EFFECT_INTENT", "INV-08": "RETRY_COUNTER_IMMUTABILITY",
    "INV-09": "PER_PROJECT_BOOTSTRAP_PIN", "INV-10": "TMUX_WINDOW_ISOLATION",
    "INV-11": "SEPARATE_FILESYSTEM_BACKUP", "INV-12": "THIRDPARTY_VERIFICATION_GATE",
}

#: Why a fixture run cannot check an invariant, and what does.
NOT_CHECKABLE = {
    "INV-01": ("workers run in-process in a lane, so a second writer is not observable here",
               ["tests/test_integration_acceptance.py", "tests/ops/test_graph_service_lifecycle.py"]),
    "INV-06": ("no catalog task widens into candidates (the winner is never merged: "
               "_p12_open_threads)", ["tests/workflows/test_candidates.py", "tests/test_confinement.py"]),
    "INV-08": ("a clean run has no retry; the fault matrix reaches one",
               ["tests/graph_engineering/test_end_to_end.py", "tests/workflows/test_plan_repair.py"]),
    "INV-09": ("bootstrap argv pinning happens before maestro is imported, outside a lane",
               ["tests/test_graph_update_compatibility.py"]),
    "INV-11": ("the fixture run takes no backup",
               ["tests/graph_engineering/test_storage_recovery.py"]),
    "INV-12": ("fixture backends are not registered third parties; doctor --thirdparty is "
               "environment-dependent", ["tests/backends/test_catalog.py", "tests/backends/test_model_runtime.py"]),
}

#: The fault matrix (P12 §2), each reached through a full task in the e2e file.
FAULT_MATRIX = {
    "transaction rollback": "test_an_acceptance_commit_that_fails_part_way_leaves_nothing_behind",
    "lost controller": "test_a_controller_lost_after_the_claim_is_resumed_and_the_task_still_lands",
    "writer crash": "test_a_writer_that_dies_mid_task_is_retried_from_its_start_and_only_the_retry_lands",
    "quota switch": "test_a_route_whose_quota_runs_out_pauses_and_the_task_lands_on_the_other",
    "interrupted integration (before the swap)":
        "test_a_controller_lost_inside_merge_leaves_the_target_untouched_and_a_rerun_lands",
    "interrupted integration (after the swap)":
        "test_a_controller_lost_after_the_swap_is_escalated_not_replayed",
}

MODES = {
    "subscription": "fixture-passed; unqualified until the real pilots (s3) qualify paid subscription",
    "free": "unqualified (Dan, 2026-09-18: real pilots qualify paid subscription only)",
    "hybrid": "unqualified (Dan, 2026-09-18: real pilots qualify paid subscription only)",
    "script": "unqualified (Dan, 2026-09-18: real pilots qualify paid subscription only)",
}

#: E1-E5 (specs/07 §1). Fixture evidence never promotes an optimisation.
EXPERIMENTS = [
    {"id": "E1", "name": "route & graph policy",
     "state": "baseline graph (implementer, gate, proof_review, merge, accept); candidates and extra nodes off",
     "decision": "stays off / unqualified",
     "evidence": "fixture lanes only; no token or pass-rate measurement across the six classes"},
    {"id": "E2", "name": "repository context value",
     "state": "Layer 1 (Python tree-sitter facts) on; Layer 2 (SCIP) not built",
     "decision": "stays off / unqualified",
     "evidence": "no measurement of search loops or context volume (D07 gates Layer 2)"},
    {"id": "E3", "name": "validation cache & affected invalidation",
     "state": "engineering.affected_checks defaults to shadow; authoritative needs qualify_affected",
     "decision": "stays off (shadow) / unqualified",
     "evidence": "promotion needs zero false cache hits over 50+ real commits; none recorded"},
    {"id": "E4", "name": "plan repair vs fresh retry",
     "state": "plan repair on, bounded by engineering.max_plan_repairs (default 1)",
     "decision": "stays as shipped, unqualified",
     "evidence": "the escalation ladder uses it; no token or fix-rate comparison with a fresh retry"},
    {"id": "E5", "name": "scheduling heuristics",
     "state": "priority -> aging -> downstream leverage (P08, accepted launch-order policy)",
     "decision": "stays as shipped, unqualified",
     "evidence": "no starvation or median-latency comparison against FIFO on a real backlog"},
]

CONTEXT_GATE_MAPPING = {
    "Goal": "task (ROADMAP task spec, control-store tasks/task_revisions)",
    "WorkUnit": "run (task_runs: one compiled workflow revision of a task)",
    "Session": "node (node_states) and the agent session on its attempt (attempts.session_id, session_mode)",
    "ContextSnapshot": "attempt (attempts.manifest_artifact_id, the context manifest artifact on the claim)",
    "GateDecision": "no maestro structure yet; the gate is not built (P12 §4). The routing decision "
                    "on the attempt (binding_json) is where one would sit",
    "OutcomeRecord": "attempt outcome (outcome_ref, failure_kind/class) with Usage "
                     "(usage_start_json/usage_settle_json), plus acceptance_decisions",
    "seams": "none found that force a second wrapper process or a gate bypass: every agent "
             "launch goes through WorkflowRunner.launch_worker",
}


# ── the target ───────────────────────────────────────────────────────────────────────

def refuse_target(repo: Path, *, deployment: bool) -> str | None:
    """Why `repo` may not be used as a scratch target, or None if it may."""
    if deployment:
        return None
    resolved = repo.resolve()
    projects = (Path.home() / "projects").resolve()
    if resolved == projects or projects in resolved.parents:
        return f"{resolved} is under {projects}; pass --deployment to use a real project"
    if resolved.exists() and not resolved.is_dir():
        return f"{resolved} exists and is not a directory"
    if resolved.exists() and any(resolved.iterdir()) and not (resolved / MARKER).exists():
        return (f"{resolved} is not empty and was not created by this script; "
                "pass --deployment to use it anyway")
    return None


# ── the run ──────────────────────────────────────────────────────────────────────────

def run_lanes(target: Path) -> tuple[int, list[dict]]:
    """Run the qualification module with its lane sandboxes under `target`."""
    import pytest

    lanes: list[dict] = []

    class _Collector:
        def pytest_runtest_makereport(self, item, call):
            if call.when != "call" and call.excinfo is None:
                return
            record = getattr(item, "_qualification", None)
            lanes.append({
                "test": item.nodeid.split("::", 1)[-1],
                "passed": call.excinfo is None,
                "error": None if call.excinfo is None else str(call.excinfo.value)[:2000],
                "record": record,
            })

    code = pytest.main(
        [str(LANES), "--no-header", "-p", "no:cacheprovider", "-p", "no:randomly",
         f"--basetemp={target / 'lanes'}"],
        plugins=[_Collector()],
    )
    return int(code), lanes


def fault_matrix() -> dict:
    tree = ast.parse(END_TO_END.read_text(encoding="utf-8"))
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    rel = END_TO_END.relative_to(REPO)
    return {
        fault: {"test": f"{rel}::{name}", "present": name in names,
                "note": f"exercised by {rel}; not re-run by this script"}
        for fault, name in FAULT_MATRIX.items()
    }


def invariants(lanes: list[dict]) -> list[dict]:
    by_name: dict[str, list[dict]] = {}
    for lane in lanes:
        for check in (lane["record"] or {}).get("invariants", []):
            by_name.setdefault(check["invariant"], []).append({**check, "lane": lane["record"]["lane"]})
    out = []
    for inv, title in INVARIANTS.items():
        checks = by_name.get(inv)
        if checks:
            out.append({"invariant": inv, "title": title, "checked": True,
                        "passed": all(c["passed"] for c in checks),
                        "lanes": [{k: c[k] for k in ("lane", "passed", "detail")} for c in checks]})
        else:
            why, tests = NOT_CHECKABLE[inv]
            out.append({"invariant": inv, "title": title, "checked": False, "passed": None,
                        "why_not_checked": why, "covered_by": tests})
    return out


def retention(lanes: list[dict]) -> dict:
    from maestro import config

    records = [lane["record"] for lane in lanes if lane["record"]]
    totals = [r["volume"]["total"] for r in records]
    per_entry: dict[str, int] = {}
    for r in records:
        for name, size in r["volume"]["by_entry"].items():
            per_entry[name] = per_entry.get(name, 0) + size
    return {
        "measured": {
            "runs": len(totals),
            "total_bytes": sum(totals),
            "max_bytes_per_run": max(totals, default=0),
            "mean_bytes_per_run": round(sum(totals) / len(totals)) if totals else 0,
            "by_entry_bytes": per_entry,
            "impl_log_bytes": sum(r["volume"]["impl_log"] for r in records),
            "excludes": "worktrees (released on accept/park; not evidence)",
        },
        "default": config.RETENTION_DEFAULTS,
        "basis": config.RETENTION_BASIS,
        "caveat": "fixture runs write tiny transcripts; the real-pilot measurement (s3) may "
                  "revise the default (_p12_open_threads)",
    }


def build_report(target: Path, code: int, lanes: list[dict]) -> dict:
    records = [lane["record"] for lane in lanes if lane["record"]]
    per_class: dict[str, int] = {}
    per_mode: dict[str, dict] = {}
    for r in records:
        if r["task_class"]:
            per_class[r["task_class"]] = per_class.get(r["task_class"], 0) + 1
        mode = per_mode.setdefault(r["mode"], {"lanes": 0, "outcomes": {}})
        mode["lanes"] += 1
        mode["outcomes"][r["run_status"]] = mode["outcomes"].get(r["run_status"], 0) + 1
    for mode, row in per_mode.items():
        row["label"] = MODES[mode]
    invs = invariants(lanes)
    checked = [i for i in invs if i["checked"]]
    faults = fault_matrix()
    ok = (code == 0 and lanes and all(lane["passed"] for lane in lanes)
          and all(i["passed"] for i in checked) and all(f["present"] for f in faults.values()))
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target": str(target),
        "backends": "fixture",
        "passed": bool(ok),
        "pytest_exit": code,
        "summary": {
            "lanes": len(lanes),
            "lanes_passed": sum(1 for lane in lanes if lane["passed"]),
            "tasks_per_class": per_class,
            "invariants_checked": len(checked),
            "invariants_passed": sum(1 for i in checked if i["passed"]),
            "invariants_not_checkable": len(invs) - len(checked),
            "acceptance_decisions_per_accepted_run": sorted(
                {r["acceptance_decisions"] for r in records if r["expected"] == "accepted"}),
        },
        "modes": per_mode,
        "invariants": invs,
        "fault_matrix": faults,
        "retention": retention(lanes),
        "experiments": EXPERIMENTS,
        "context_gate_mapping": CONTEXT_GATE_MAPPING,
        "lanes": lanes,
    }


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", required=True, type=Path,
                        help="scratch directory the lane repositories are built under")
    parser.add_argument("--fixture-backends", action="store_true",
                        help="run with fixture backends (the only mode this script runs)")
    parser.add_argument("--deployment", action="store_true",
                        help="allow a non-scratch --repo")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    if not args.fixture_backends:
        print("only --fixture-backends is implemented; real backends are qualified by the "
              "pilots (session s3), not by this script", file=sys.stderr)
        return 2
    why = refuse_target(args.repo, deployment=args.deployment)
    if why:
        print(f"refusing --repo: {why}", file=sys.stderr)
        return 2
    target = args.repo.resolve()
    target.mkdir(parents=True, exist_ok=True)
    (target / MARKER).write_text("created by scripts/graph_qualification.py\n", encoding="utf-8")

    code, lanes = run_lanes(target)
    report = build_report(target, code, lanes)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")

    s, r = report["summary"], report["retention"]
    print(f"qualification {'PASSED' if report['passed'] else 'FAILED'}: "
          f"{s['lanes_passed']}/{s['lanes']} lanes; tasks per class {s['tasks_per_class']}; "
          f"invariants {s['invariants_passed']}/{s['invariants_checked']} checked passed "
          f"({s['invariants_not_checkable']} not checkable here); "
          f"artifact bytes {r['measured']['total_bytes']}; retention default {r['default']}")
    print(f"report: {args.out}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
