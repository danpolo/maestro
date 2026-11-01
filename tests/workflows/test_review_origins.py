"""Where each review finding came from (Dan, 2026-09-26): ids, origins, the fault table.

Dan wants more quality work in fewer cycles, measured in subscription usage per accepted
task. A round-2+ blocking finding says whose miss it is — the implementer's, the
reviewer's, the planner's, or nobody's — so each role's root causes can be counted.
"""
from __future__ import annotations

import json

import pytest

from maestro.workflows import handlers, review_origins
from maestro.workflows.models import NodeSpec


def test_every_origin_maps_to_exactly_one_fault_in_one_table():
    assert set(review_origins.ORIGIN_FAULT) == set(review_origins.ORIGINS)
    assert review_origins.ORIGIN_FAULT[review_origins.UNCLASSIFIED] == "unclassified"
    assert set(review_origins.ORIGIN_FAULT.values()) <= set(review_origins.FAULTS)
    assert review_origins.ORIGIN_FAULT["not_fixed"] == "implementer"
    assert review_origins.ORIGIN_FAULT["missed_earlier"] == "reviewer"
    assert review_origins.ORIGIN_FAULT["exposed_by_rework"] == "none"
    assert review_origins.PRIOR_LINKED == {
        "not_fixed", "fixed_incompletely_in_scope", "fixed_incompletely_out_of_scope",
    }


def test_counts_are_by_origin_and_by_fault_over_blocking_findings_only():
    records = [
        {"id": "r2-f1", "severity": "blocking", "origin": "not_fixed"},
        {"id": "r2-f2", "severity": "blocking", "origin": "missed_earlier"},
        {"id": "r2-f3", "severity": "blocking", "origin": "unclassified"},
        {"id": "r2-f4", "severity": "follow_up", "origin": "missed_earlier"},
    ]
    counts = review_origins.count(records)
    assert counts == {
        "blocking": 3,
        "origin": {"not_fixed": 1, "missed_earlier": 1, "unclassified": 1},
        "fault": {"implementer": 1, "reviewer": 1, "unclassified": 1},
    }


def _review_context(tmp_path, **inputs):
    node = NodeSpec(
        node_id="proof_review", kind="agent", handler_ref="maestro.handlers.agent",
        agent_definition_ref="reviewer",
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return handlers.HandlerContext(
        run_id="r", task_id="T", node=node, attempt_id="a",
        inbox=handlers.AttemptInbox(tmp_path / "attempts", "a"), workspace=workspace,
        inputs=inputs,
    )


def _adopt(ctx, findings, verdict="changes_requested"):
    handlers.write_review(ctx.workspace, verdict, findings)
    handlers.write_sentinel(ctx.workspace, "DONE")
    return handlers.agent_handler(ctx).outputs["review"]


def test_a_first_round_gets_ids_and_needs_no_origin(tmp_path):
    ctx = _review_context(tmp_path, review_round=1)
    review = _adopt(ctx, [
        {"finding": "sleep is unbounded", "severity": "blocking"},
        {"finding": "SQL outside db.py", "severity": "follow_up"},
    ])
    assert review["round"] == 1
    assert [(r["id"], r["severity"], r["origin"]) for r in review["finding_records"]] == [
        ("r1-f1", "blocking", None), ("r1-f2", "follow_up", None),
    ]
    assert "origin_flags" not in review
    # The display strings are unchanged: what the implementer's rework note reads.
    assert review["findings"] == ["sleep is unbounded"]


def test_a_later_round_s_blocking_findings_carry_origin_and_prior_finding(tmp_path):
    ctx = _review_context(tmp_path, review_round=2, prior_finding_ids=["r1-f1", "r1-f2"])
    review = _adopt(ctx, [
        {"finding": "sleep still unbounded", "severity": "blocking",
         "origin": "not_fixed", "prior_finding": "R1-F1"},
        {"finding": "new crash in the retry", "severity": "blocking",
         "origin": "introduced-by-rework"},
        {"finding": "a nit", "severity": "follow_up"},
    ])
    records = review["finding_records"]
    assert [(r["id"], r["origin"], r["prior_finding"]) for r in records] == [
        ("r2-f1", "not_fixed", "r1-f1"),
        ("r2-f2", "introduced_by_rework", None),
        ("r2-f3", None, None),
    ]
    assert "origin_flags" not in review


@pytest.mark.parametrize("finding, flag", [
    ({"finding": "x", "severity": "blocking"}, "r2-f1: no origin"),
    ({"finding": "x", "severity": "blocking", "origin": "bad luck"}, "r2-f1: unknown origin"),
    ("an unmarked string finding", "r2-f1: no origin"),
])
def test_a_missing_or_unknown_origin_never_fails_the_review(tmp_path, finding, flag):
    ctx = _review_context(tmp_path, review_round=2, prior_finding_ids=["r1-f1"])
    handlers.write_review(ctx.workspace, "changes_requested", [finding])
    handlers.write_sentinel(ctx.workspace, "DONE")
    result = handlers.agent_handler(ctx)
    # Still a conclusive rejection that sends the work back — not garbage output.
    assert result.outputs["failure"]["kind"] == "verification_failed"
    review = result.outputs["review"]
    assert review["finding_records"][0]["origin"] == "unclassified"
    assert any(f.startswith(flag) for f in review["origin_flags"]), review["origin_flags"]


def test_a_prior_linked_origin_naming_no_prior_finding_is_flagged_but_kept(tmp_path):
    ctx = _review_context(tmp_path, review_round=3, prior_finding_ids=["r1-f1", "r2-f1"])
    review = _adopt(ctx, [
        {"finding": "x", "severity": "blocking", "origin": "fixed_incompletely_in_scope",
         "prior_finding": "r9-f9"},
        {"finding": "y", "severity": "blocking", "origin": "not_fixed"},
    ])
    records = review["finding_records"]
    assert [r["origin"] for r in records] == ["fixed_incompletely_in_scope", "not_fixed"]
    assert records[0]["prior_finding"] == "r9-f9"
    assert len(review["origin_flags"]) == 2


def test_a_review_without_a_known_round_still_numbers_its_findings(tmp_path):
    ctx = _review_context(tmp_path)
    review = _adopt(ctx, ["one", "two"])
    assert review["round"] is None
    assert [r["id"] for r in review["finding_records"]] == ["f1", "f2"]


def test_an_origin_on_an_approved_review_s_follow_up_is_kept_when_valid(tmp_path):
    ctx = _review_context(tmp_path, review_round=2, prior_finding_ids=["r1-f1"])
    review = _adopt(ctx, [
        {"finding": "a nit", "severity": "follow_up", "origin": "missed_earlier"},
        {"finding": "b nit", "severity": "follow_up", "origin": "nonsense"},
    ], verdict="approved")
    assert [r["origin"] for r in review["finding_records"]] == ["missed_earlier", None]
    assert json.dumps(review)  # plain JSON all the way down


def test_a_follow_up_marked_before_dependents_keeps_the_mark_in_its_record_and_disposition(
    tmp_path,
):
    """GDT-11 S1: the reviewer marks a follow-up a dependent would build on; the mark
    rides in its record and in the disposition (by its text, as `follow_ups`). A blocking
    finding ignores it: it sends the task back anyway."""
    ctx = _review_context(tmp_path, review_round=1)
    review = _adopt(ctx, [
        {"finding": "the result field is misnamed", "severity": "follow_up",
         "rule": "names match the schema", "before_dependents": True},
        {"finding": "sleep is unbounded", "severity": "blocking", "before_dependents": True},
        {"finding": "a nit", "severity": "follow_up"},
    ])
    flagged = "the result field is misnamed Rule: names match the schema."
    assert [r.get("before_dependents") for r in review["finding_records"]] == [
        True, None, None]
    assert review["before_dependents"] == [flagged]
    assert review["follow_ups"] == [flagged, "a nit"]
    assert review["findings"] == ["sleep is unbounded"]
    (tmp_path / "plain").mkdir()
    plain = _adopt(_review_context(tmp_path / "plain", review_round=1),
                   [{"finding": "a nit", "severity": "follow_up"}], verdict="approved")
    assert "before_dependents" not in plain
