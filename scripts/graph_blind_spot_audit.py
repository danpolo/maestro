#!/usr/bin/env python3
"""Run bounded, local-only graph-engineering probes and emit machine-readable evidence."""
from __future__ import annotations

import argparse
import copy
import errno
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from maestro import bootstrap, cli, confinement, selfupdate
from maestro.backends import registry
from maestro.backends.base import to_usage_json
from maestro.graph_evidence import load_case_catalog, validate_record
from maestro.graph_experiments import (
    apply_file_action,
    bwrap_confinement_probe,
    detached_child_probe,
    exclusive_writer_probe,
    json_enospc_probe,
    run_process_batch,
    sqlite_contention_probe,
    sqlite_schema_rollback_probe,
    sqlite_storage_probe,
)


REPO = Path(__file__).resolve().parents[1]
CATALOG = REPO / "tests" / "graph_engineering" / "fixtures" / "cases.json"
INIT_REHEARSAL = REPO / "artifacts" / "graph-engineering" / "init-rehearsal.json"
BACKEND_REVIEW = REPO / "artifacts" / "graph-engineering" / "backend-review-results.json"
DEPENDENCY_FIXTURE = REPO / "tests" / "graph_engineering" / "fixtures" / "dependency_project"


def _run(argv: list[str], *, cwd: Path = REPO, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def _version(binary: str) -> str:
    try:
        result = _run([binary, "--version"], timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    lines = ((result.stdout or "") + (result.stderr or "")).strip().splitlines()
    return lines[-1].strip() if lines else ""


def _help(binary: str) -> str:
    try:
        result = _run([binary, "--help"], timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (result.stdout or "") + (result.stderr or "")


def _usage(backend: str) -> dict[str, Any] | None:
    try:
        sample = registry.get_backend(backend).usage()
    except Exception:
        return None
    return None if sample is None else to_usage_json(sample)


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _init_rehearsal_ok(document: dict[str, Any] | None) -> bool:
    if not document or document.get("backend_calls_started") != 0:
        return False
    projects = document.get("projects")
    if not isinstance(projects, list) or {row.get("kind") for row in projects} != {"python", "node"}:
        return False
    return all(
        row.get("init_exit_code") == row.get("test_adapter_exit_code") == 0
        and row.get("queue_exhausted") is True
        and row.get("halt_respected") is True
        and row.get("service_type_forking") is True
        and row.get("service_direct_tmux") is True
        for row in projects
    )


def _backend_inventory() -> dict[str, Any]:
    inventory: dict[str, Any] = {}
    controls = {
        "claude": ("--model", "--effort", "--restricted", "--resume"),
        "codex": ("--model", "--sandbox", "resume", "--config"),
        "agy": ("--model", "--effort", "--sandbox", "--conversation"),
    }
    for name in ("claude", "codex", "agy"):
        path = shutil.which(name)
        help_text = _help(path) if path else ""
        inventory[name] = {
            "path": path,
            "version": _version(path) if path else "",
            "help_controls": {flag: flag in help_text for flag in controls[name]},
            "maestro_driver": name in registry.known_backends(),
        }
    for name in registry.known_backends():
        try:
            capabilities = registry.get_backend(name).capabilities()
            inventory[name]["maestro_capabilities"] = capabilities._asdict()
        except Exception as exc:
            inventory[name]["maestro_capabilities_error"] = type(exc).__name__
        inventory[name]["usage"] = _usage(name)

    config_path = Path.home() / ".codex" / "config.toml"
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        config = {}
    inventory["codex"]["configured_tuple"] = {
        "model": config.get("model") or "unknown",
        "effort": config.get("model_reasoning_effort") or "unknown",
    }
    inventory["agy"]["usage"] = None
    inventory["agy"]["usage_note"] = "no usage surface observed in installed CLI help or Maestro"
    return inventory


def _policy_probe(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    cases = [
        ("node-root", {"test"}, {"package.json": '{"scripts":{"test":"node --test"}}'}),
        ("node-no-test", set(), {"package.json": '{"scripts":{"build":"vite build"}}'}),
        ("python-root", {"test"}, {"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths=["tests"]'}),
        ("python-no-test", set(), {"pyproject.toml": '[project]\nname="x"\nversion="1"'}),
        ("go-root", {"test"}, {"go.mod": "module fixture.test/x\n\ngo 1.23"}),
        (
            "mixed-monorepo",
            {"subproject-test"},
            {
                "package.json": '{"private":true,"workspaces":["packages/*"]}',
                "packages/api/pyproject.toml": '[tool.pytest.ini_options]\ntestpaths=["tests"]',
            },
        ),
        ("generated-assets", {"generated-assets"}, {"schema/api.yaml": "openapi: 3.1.0"}),
        ("service-dependency", {"service-dependency"}, {"compose.yaml": "services: {}"}),
    ]
    raw = []
    missed = 0
    false = 0
    for name, expected, files in cases:
        project = root / name
        project.mkdir()
        for relative, content in files.items():
            path = project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content + "\n", encoding="utf-8")
        facts = cli._derive_repo_facts(project)
        observed = {"test"} if facts["test_command_argv"] else set()
        missed_items = sorted(expected - observed)
        false_items = sorted(observed - expected)
        missed += len(missed_items)
        false += len(false_items)
        raw.append(
            {
                "case": name,
                "expected": sorted(expected),
                "observed": sorted(observed),
                "missed": missed_items,
                "false": false_items,
            }
        )
    return {"cases": raw, "case_count": len(raw), "missed": missed, "false": false}


def _declaration_signatures(text: str) -> list[str]:
    return re.findall(r"^(?:async\s+)?(?:def|class)\s+[A-Za-z_]\w*", text, flags=re.MULTILINE)


#: Bounds a hung nested fixture pytest only; its exit code is the observation.
NESTED_PYTEST_TIMEOUT_SEC = 120


def _dependency_probe(root: Path, catalog: dict[str, Any]) -> dict[str, Any]:
    pristine = root / "pristine"
    shutil.copytree(DEPENDENCY_FIXTURE, pristine)
    pristine_run = _run(
        [sys.executable, "-m", "pytest", "-q", "-o", "python_files=verify_*.py", "acceptance"],
        cwd=pristine,
        timeout=NESTED_PYTEST_TIMEOUT_SEC,
    )
    results = []
    for case in catalog["dependency_cases"]:
        project = root / case["id"]
        shutil.copytree(DEPENDENCY_FIXTURE, project)
        changed = project / case["changed"][0]
        before = _declaration_signatures(changed.read_text(encoding="utf-8"))
        changed.write_text(case["replacement"], encoding="utf-8")
        after = _declaration_signatures(changed.read_text(encoding="utf-8"))
        run = _run(
            [sys.executable, "-m", "pytest", "-q", "-o", "python_files=verify_*.py", "acceptance"],
            cwd=project,
            timeout=NESTED_PYTEST_TIMEOUT_SEC,
        )
        results.append(
            {
                "id": case["id"],
                "dependency_kind": case["dependency_kind"],
                "changed": case["changed"],
                "must_run": case["must_run"],
                "must_run_present": all((project / path).is_file() for path in case["must_run"]),
                "declaration_signatures_before": before,
                "declaration_signatures_after": after,
                "signature_only_selected": before != after,
                "full_suite_exit_code": run.returncode,
                "full_suite_caught_mutation": run.returncode == 1,
                "output_tail": (run.stdout + run.stderr)[-800:],
            }
        )
    return {
        "pristine_exit_code": pristine_run.returncode,
        "case_count": len(results),
        "caught_count": sum(item["full_suite_caught_mutation"] for item in results),
        "signature_only_selected": sum(item["signature_only_selected"] for item in results),
        "cases": results,
    }


def _claim(kind: str, statement: str, *evidence: str) -> dict[str, Any]:
    return {"kind": kind, "statement": statement, "evidence": list(evidence)}


def _record(blind_spot_id: str, status: str, *claims: dict[str, Any]) -> dict[str, Any]:
    record = {"blind_spot_id": blind_spot_id, "status": status, "claims": list(claims)}
    validate_record(record)
    return record


def build_report(*, skip_backends: bool) -> dict[str, Any]:
    catalog = load_case_catalog(CATALOG)
    init_rehearsal = _load_json(INIT_REHEARSAL)
    init_rehearsal_valid = _init_rehearsal_ok(init_rehearsal)
    backend_review = _load_json(BACKEND_REVIEW)
    with tempfile.TemporaryDirectory(prefix="maestro-graph-audit-") as raw_root:
        root = Path(raw_root)
        policy = _policy_probe(root / "policy")
        validation = _dependency_probe(root / "validation", catalog)
        bwrap = bwrap_confinement_probe(root / "bwrap")
        writer = exclusive_writer_probe(root / "writer")
        process = detached_child_probe(root / "process")
        receipt_first = apply_file_action(root / "receipt", "deploy-42", use_receipt=True)
        receipt_retry = apply_file_action(root / "receipt", "deploy-42", use_receipt=True)
        apply_file_action(root / "blind", "deploy-42", use_receipt=False)
        blind_retry = apply_file_action(root / "blind", "deploy-42", use_receipt=False)
        storage = {
            "online_backup": sqlite_storage_probe(root / "storage"),
            "json_enospc": json_enospc_probe(root / "enospc"),
            "schema_rollback": sqlite_schema_rollback_probe(root / "migration"),
        }
        sleep_command = [
            sys.executable,
            "-c",
            "import time; payload=bytearray(1024*1024); time.sleep(0.12)",
        ]
        serial = run_process_batch(sleep_command, task_count=4, concurrency=1, cwd=root)
        parallel = run_process_batch(sleep_command, task_count=4, concurrency=2, cwd=root)
        sqlite_contention = sqlite_contention_probe(root / "contention")

    logical_confinement = confinement.path_ok(
        ["docs/result.md"], kind=confinement.REDO, config={"confinement": {"redo_allow": ["docs/"]}}
    )
    backends = None if skip_backends else _backend_inventory()
    observations = {
        "project_policy": policy,
        "validation_dependency_fixture": validation,
        "logical_confinement": {"accepted": logical_confinement[0], "reason": logical_confinement[1]},
        "bwrap_confinement": bwrap,
        "exclusive_writer": writer,
        "detached_process": process,
        "external_action": {
            "receipt_first": receipt_first,
            "receipt_retry": receipt_retry,
            "blind_retry": blind_retry,
        },
        "storage": storage,
        "concurrency": {"serial": serial, "parallel": parallel, "sqlite": sqlite_contention},
        "service_ownership": {
            "current_pointer": str(bootstrap.current_file()),
            "pointer_scope": "machine-global",
            "generated_service": "Type=forking/direct named TMUX session",
        },
        "init_rehearsal": init_rehearsal,
        "init_rehearsal_valid": init_rehearsal_valid,
        "backend_review": backend_review,
        "backends": backends,
    }

    records = [
        _record(
            "B01", "progress",
            _claim("observed", f"source-controlled catalog contains {len(catalog['tasks'])} task classes", "file:tests/graph_engineering/fixtures/cases.json"),
            *(
                [_claim("observed", "backend-free init and real test adapters passed in disposable Python and Node projects", "artifact:artifacts/graph-engineering/init-rehearsal.json")]
                if init_rehearsal_valid
                else [_claim("unknown", "the two-project init rehearsal is absent or does not pass every required check")]
            ),
            _claim("unknown", "no complete real LLM implementation baseline was run"),
        ),
        _record(
            "B02", "progress" if backends else "unresolved",
            _claim("observed", "installed-backend usage surfaces were sampled without inference" if backends else "backend sampling was explicitly skipped", "artifact:observations.backends" if backends else "flag:--skip-backends"),
            *(
                [_claim("observed", "pre/post account samples were not attributable to the two review calls", "artifact:artifacts/graph-engineering/backend-review-results.json")]
                if backend_review
                else []
            ),
            _claim("unknown", "subscription attribution to this experiment remains unavailable unless a backend reports it"),
        ),
        _record(
            "B05", "progress",
            _claim("observed", f"fixture policy detector recorded {policy['missed']} misses and {policy['false']} false requirements across {policy['case_count']} cases", "artifact:observations.project_policy"),
        ),
        _record(
            "B06", "progress" if backends else "unresolved",
            _claim("observed", "installed CLI and Maestro capability surfaces were recorded" if backends else "backend introspection was explicitly skipped", "artifact:observations.backends" if backends else "flag:--skip-backends"),
        ),
        _record(
            "B07", "progress",
            _claim("observed", "logical path confinement accepted an allowed scratch-relative path", "artifact:observations.logical_confinement"),
            (
                _claim(
                    "observed",
                    "the bounded bwrap profile confined its parent and child and masked one credential directory",
                    "artifact:observations.bwrap_confinement",
                )
                if bwrap["available"]
                else _claim("unknown", "the bwrap enforcement probe could not run in this environment")
            ),
            _claim("unknown", "the profile does not establish complete confidential-read isolation across the host"),
        ),
        _record(
            "B08", "progress",
            _claim("observed", "exclusive lock blocked a replacement while its owner lived", "artifact:observations.exclusive_writer"),
            _claim("observed", "a detached child outlived a successful launcher exit", "artifact:observations.detached_process"),
        ),
        _record(
            "B11", "progress",
            _claim("observed", f"the full fixture suite caught {validation['caught_count']}/{validation['case_count']} seeded dependency changes", "artifact:observations.validation_dependency_fixture"),
            _claim("inferred", "signature-only affected selection is unsafe for these dependency classes", "artifact:observations.validation_dependency_fixture"),
        ),
        _record(
            "B12", "progress",
            _claim("observed", "fixed criteria, paired-result summaries, and uncertainty functions are executable", "pytest:tests/graph_engineering/test_evidence.py"),
            *(
                [_claim("unknown", "two stored 17/17 manual classifications lack raw outputs and exact invocations, so they are provisional and not independently reproducible")]
                if backend_review
                else []
            ),
            _claim("unknown", "two reviews of one task do not calibrate a transferable quality floor"),
        ),
        _record(
            "B13", "progress",
            _claim("observed", "bounded serial/parallel process and SQLite writer measurements were captured", "artifact:observations.concurrency"),
        ),
        _record(
            "B14", "progress",
            _claim("observed", "the receipt fixture reconciled one effect while blind replay produced two", "artifact:observations.external_action"),
        ),
        _record(
            "B15", "progress",
            _claim("observed", "raw probes captured pre-rename ENOSPC preservation, online backup/missing-artifact recovery, and schema rollback", "artifact:observations.storage"),
            _claim("unknown", "same-disk fixture backup does not establish host/disk-loss recovery"),
        ),
        _record(
            "B16", "progress",
            _claim("observed", "the generated unit uses a direct named TMUX session with Type=forking", "pytest:tests/graph_engineering/test_service_ownership.py"),
            _claim("observed", "the adopted-version pointer is machine-global", "artifact:observations.service_ownership"),
            _claim("unknown", "per-project version pinning and migration rollback require a design decision"),
        ),
        _record(
            "B18", "progress",
            _claim("observed", f"the fixed catalog spans {len(catalog['tasks'])} task classes but contains no statistical claim", "file:tests/graph_engineering/fixtures/cases.json"),
            _claim("unknown", "only one catalog class has two backend outcomes; retry and stopping limits remain uncalibrated"),
        ),
    ]
    report = {
        "schema_version": 1,
        "scope": "local-machine-only",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repository": str(REPO),
        "backend_calls_started": 0,
        "observations": observations,
        "records": records,
    }
    report["audit_ok"] = (
        validation["pristine_exit_code"] == 0
        and init_rehearsal_valid
        and validation["caught_count"] == validation["case_count"]
        and validation["signature_only_selected"] == 0
        and all(item["must_run_present"] for item in validation["cases"])
        and writer == {
            "owner_acquired": True,
            "concurrent_replacement_acquired": False,
            "post_exit_replacement_acquired": True,
        }
        and process["launcher_exit_code"] == 0
        and process["child_alive_after_launcher_exit"] is True
        and process["child_cleaned_up"] is True
        and receipt_retry["effect_count"] == 1
        and receipt_retry["reconciled"] is True
        and blind_retry["effect_count"] == 2
        and storage["online_backup"]["quick_check"] == "ok"
        and storage["json_enospc"]["injected_errno"] == errno.ENOSPC
        and storage["json_enospc"]["previous_document"] == {"generation": 1}
        and storage["schema_rollback"] == {
            "columns": ["id", "status"],
            "user_version": 1,
            "rows": [[1, "waiting"]],
        }
        and serial["exit_codes"] == parallel["exit_codes"] == [0, 0, 0, 0]
        and sqlite_contention["claimed_jobs"] == sqlite_contention["jobs"]
        and sqlite_contention["duplicate_claims"] == 0
    )
    if bwrap["available"]:
        report["audit_ok"] = report["audit_ok"] and all(
            [
                bwrap["inside_write"],
                not bwrap["outside_write"],
                not bwrap["outside_read"],
                not bwrap["child_outside_write"],
                bwrap["linked_worktree_git_status"],
                not bwrap["linked_worktree_git_commit"],
            ]
        )
    return report


def _stable_snapshot(report: dict[str, Any]) -> dict[str, Any]:
    """Remove intentionally volatile observations before comparing repeated runs."""
    snapshot = copy.deepcopy(report)
    snapshot.pop("generated_at", None)
    snapshot.pop("repeatability", None)
    observations = snapshot["observations"]
    for case in observations["validation_dependency_fixture"]["cases"]:
        case.pop("output_tail", None)
    for field in (
        "detail",
        "linked_worktree_git_status_detail",
        "linked_worktree_git_commit_detail",
    ):
        observations["bwrap_confinement"].pop(field, None)
    for mode in ("serial", "parallel"):
        for field in ("elapsed_ms", "max_queue_delay_ms", "peak_rss_kib"):
            observations["concurrency"][mode].pop(field, None)
    # Lock retries depend on scheduling and storage latency. Keep the raw
    # measurements in the report, but compare queue correctness across runs.
    for field in ("max_writer_wait_ms", "writer_attempts", "busy_errors"):
        observations["concurrency"]["sqlite"].pop(field, None)
    if observations["backends"]:
        for backend in observations["backends"].values():
            backend.pop("usage", None)
    return snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=REPO / "artifacts" / "graph-engineering" / "local-audit.json")
    parser.add_argument("--skip-backends", action="store_true", help="do not inspect installed backend CLIs or usage surfaces")
    parser.add_argument("--repeat", type=int, default=2, help="number of bounded runs used for stable-field comparison")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    reports = [build_report(skip_backends=args.skip_backends) for _ in range(args.repeat)]
    report = reports[0]
    baseline = _stable_snapshot(report)
    stable = all(_stable_snapshot(candidate) == baseline for candidate in reports[1:])
    report["repeatability"] = {
        "runs": args.repeat,
        "stable_fields_equal": stable,
        "excluded_dynamic_fields": [
            "timestamps and live usage",
            "temporary paths and command timing/output",
            "elapsed/queue/RSS/writer-wait/retry measurements",
        ],
    }
    report["audit_ok"] = report["audit_ok"] and stable
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    return 0 if report["audit_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
