from __future__ import annotations

import json
from pathlib import Path

import pytest

from maestro.graph_evidence import (
    BackendTuple,
    load_case_catalog,
    paired_summary,
    validate_record,
    weighted_score,
    wilson_interval,
)


CATALOG = Path(__file__).parent / "fixtures" / "cases.json"
BACKEND_RESULTS = Path(__file__).parents[2] / "artifacts" / "graph-engineering" / "backend-review-results.json"


def test_backend_tuple_keeps_cli_model_and_effort_separate():
    binding = BackendTuple(
        backend="claude-code",
        cli_version="2.1.263",
        model="claude-sonnet-4-6",
        effort="high",
    )

    assert binding.as_dict() == {
        "backend": "claude-code",
        "cli_version": "2.1.263",
        "model": "claude-sonnet-4-6",
        "effort": "high",
    }


def test_backend_tuple_requires_an_explicit_effort_value():
    with pytest.raises(ValueError, match="effort"):
        BackendTuple("codex-cli", "0.153.4", "gpt-6", "")


def test_record_rejects_a_claim_that_does_not_name_its_evidence_kind():
    record = {
        "blind_spot_id": "B08",
        "status": "progress",
        "claims": [{"statement": "a second writer was refused", "evidence": ["pytest:x"]}],
    }

    with pytest.raises(ValueError, match="claim kind"):
        validate_record(record)


def test_unknown_claims_need_no_invented_evidence_reference():
    record = {
        "blind_spot_id": "B02",
        "status": "unresolved",
        "claims": [
            {
                "kind": "unknown",
                "statement": "weekly usage attribution is unavailable",
                "evidence": [],
            }
        ],
    }

    validate_record(record)


def test_observed_claims_require_an_evidence_reference():
    record = {
        "blind_spot_id": "B13",
        "status": "progress",
        "claims": [
            {"kind": "observed", "statement": "peak RSS was 12 MiB", "evidence": []}
        ],
    }

    with pytest.raises(ValueError, match="observed claim"):
        validate_record(record)


def test_weighted_score_uses_weights_fixed_in_the_fixture():
    outcomes = [
        {"criterion_id": "correct", "weight": 5, "passed": True},
        {"criterion_id": "held-out", "weight": 3, "passed": False},
        {"criterion_id": "docs", "weight": 2, "passed": True},
    ]

    assert weighted_score(outcomes) == {"earned": 7, "possible": 10, "ratio": 0.7}


def test_paired_summary_preserves_failures_and_ties_in_the_denominator():
    rows = [
        {"left": 1, "right": 0},
        {"left": 0, "right": 1},
        {"left": 0, "right": 0},
        {"left": 1, "right": 1},
    ]

    assert paired_summary(rows) == {"n": 4, "left_wins": 1, "ties": 2, "right_wins": 1}


def test_wilson_interval_states_small_sample_uncertainty():
    low, high = wilson_interval(3, 4)

    assert round(low, 3) == 0.301
    assert round(high, 3) == 0.954
    assert wilson_interval(0, 0) is None


def test_case_catalog_has_unique_stratified_tasks_and_dependency_seeds():
    catalog = load_case_catalog(CATALOG)

    assert [task["class"] for task in catalog["tasks"]] == [
        "bug",
        "feature",
        "documentation",
        "debugging",
        "manual-preparation",
        "recovery",
    ]
    assert {case["dependency_kind"] for case in catalog["dependency_cases"]} == {
        "behavior-with-unchanged-signature",
        "dynamic-import",
        "data-file",
        "environment",
        "generated-code",
        "test-infrastructure",
    }
    assert len({task["id"] for task in catalog["tasks"]}) == len(catalog["tasks"])
    assert json.loads(CATALOG.read_text(encoding="utf-8"))["schema_version"] == 1


@pytest.mark.parametrize("bad_id", [None, "", "   "])
def test_case_catalog_rejects_missing_or_empty_ids(tmp_path, bad_id):
    catalog = tmp_path / "cases.json"
    catalog.write_text(
        json.dumps({"schema_version": 1, "tasks": [{"id": bad_id}], "dependency_cases": []}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="ids"):
        load_case_catalog(catalog)


def test_paid_backend_results_preserve_exact_tuples_provisional_scores_and_unknown_attribution():
    report = json.loads(BACKEND_RESULTS.read_text(encoding="utf-8"))
    runs = {run["backend"]: run for run in report["runs"]}

    assert {
        name: (run["cli_version"], run["model"], run["effort"])
        for name, run in runs.items()
    } == {
        "Claude Code": ("2.1.263", "claude-sonnet-4-6", "low"),
        "Antigravity CLI": ("1.1.27", "gemini-3.8-flash-low", "low"),
        "Codex CLI": ("0.153.4", "gpt-5.6-sol", "high (configured default; no task launched)"),
    }
    assert runs["Claude Code"]["provisional_weighted_score"] == {"earned": 17, "possible": 17, "ratio": 1.0}
    assert runs["Antigravity CLI"]["provisional_weighted_score"] == {"earned": 17, "possible": 17, "ratio": 1.0}
    assert runs["Codex CLI"]["status"] == "not-run-quota-conservation"
    assert report["verification"] == {
        "criteria_fixed_before_runs": True,
        "classification_method": "manual mapping from the structured CLI responses observed during the run",
        "raw_model_outputs_retained": False,
        "exact_invocations_retained": False,
        "status": "provisional-not-independently-reproducible",
    }
    assert report["subscription_window_samples"]["attribution"]["kind"] == "unknown"
