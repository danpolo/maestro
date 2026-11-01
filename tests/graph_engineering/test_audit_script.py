from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from maestro.graph_evidence import validate_record


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "graph_blind_spot_audit.py"


def test_local_audit_emits_valid_records_without_starting_backends(tmp_path):
    output = tmp_path / "audit.json"

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--skip-backends", "--output", str(output)],
        cwd=REPO,
        capture_output=True,
        text=True,
        # Bounds a hung audit only. It runs every local probe twice, including nine
        # nested pytest runs, so its duration measures host load, not the audit.
        timeout=600,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["scope"] == "local-machine-only"
    assert report["backend_calls_started"] == 0
    assert report["audit_ok"] is True
    assert report["repeatability"] == {
        "runs": 2,
        "stable_fields_equal": True,
        "excluded_dynamic_fields": [
            "timestamps and live usage",
            "temporary paths and command timing/output",
            "elapsed/queue/RSS/writer-wait/retry measurements",
        ],
    }
    assert {record["blind_spot_id"] for record in report["records"]} == {
        "B01", "B02", "B05", "B06", "B07", "B08", "B11", "B12", "B13",
        "B14", "B15", "B16", "B18",
    }
    for record in report["records"]:
        validate_record(record)


@pytest.fixture
def audit_module():
    spec = importlib.util.spec_from_file_location("graph_blind_spot_audit", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def successful_probe_report():
    return {
        "audit_ok": True,
        "observations": {
            "validation_dependency_fixture": {"cases": []},
            "bwrap_confinement": {},
            "backends": None,
            "concurrency": {
                "serial": {},
                "parallel": {},
                "sqlite": {
                    "jobs": 12, "workers": 4, "claimed_jobs": 12,
                    "duplicate_claims": 0, "busy_errors": 0, "writer_attempts": 16,
                    "max_writer_wait_ms": 1000.0,
                },
            },
        },
    }


def test_repeatability_preserves_raw_sqlite_contention_without_requiring_identical_scheduling(
    tmp_path, monkeypatch, audit_module, successful_probe_report,
):
    # Real concurrent report capture observed 0/16 versus 3/19, with all jobs
    # claimed once in both runs. Busy retries are measurements, not outcomes.
    first = successful_probe_report
    second = copy.deepcopy(first)
    second["observations"]["concurrency"]["sqlite"].update(
        busy_errors=3, writer_attempts=19, max_writer_wait_ms=1900.0,
    )
    originals = copy.deepcopy([first, second])
    reports = iter([first, second])
    monkeypatch.setattr(audit_module, "build_report", lambda **kwargs: next(reports))
    output = tmp_path / "audit.json"

    assert audit_module.main(["--skip-backends", "--output", str(output)]) == 0
    saved = json.loads(output.read_text())
    assert saved["repeatability"]["stable_fields_equal"] is True
    assert saved["observations"] == originals[0]["observations"]
    assert second == originals[1]


@pytest.mark.parametrize("field,value", [("claimed_jobs", 11), ("duplicate_claims", 1)])
def test_repeatability_still_rejects_sqlite_outcome_changes(
    tmp_path, monkeypatch, audit_module, successful_probe_report, field, value,
):
    first = successful_probe_report
    second = copy.deepcopy(first)
    second["observations"]["concurrency"]["sqlite"][field] = value
    reports = iter([first, second])
    monkeypatch.setattr(audit_module, "build_report", lambda **kwargs: next(reports))
    output = tmp_path / "audit.json"

    assert audit_module.main(["--skip-backends", "--output", str(output)]) == 1
    saved = json.loads(output.read_text())
    assert saved["repeatability"]["stable_fields_equal"] is False
    assert saved["audit_ok"] is False
