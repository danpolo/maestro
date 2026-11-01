"""P12B F0: the closed failure vocabulary and the classifiers built on it."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from datetime import datetime, timezone

from maestro.backends.base import ExitVerdict
from maestro.workflows import failures
from maestro.workflows.failures import FAILURE_KINDS, FailureRecord
from maestro.workflows.models import NodeResult


def _result(status="failed", detail="", outputs=None) -> NodeResult:
    return NodeResult(run_id="r", node_id="n", attempt_id="a", status=status,
                      detail=detail, outputs=outputs or {})


def test_the_vocabulary_is_the_sixteen_kinds_in_two_classes():
    blocked = {k for k, c in FAILURE_KINDS.items() if c == failures.BLOCKED}
    bad = {k for k, c in FAILURE_KINDS.items() if c == failures.BAD_OUTPUT}
    assert blocked == {"rate_limited", "quota_exhausted", "provider_unavailable",
                       "timeout", "auth_failed", "context_overflow", "interrupted",
                       "dan_question", "operator_canceled", "operator_retry"}
    assert bad == {"empty_output", "malformed_tool_calls", "garbage_output", "no_sentinel",
                   "verification_failed", "worker_crashed", "controller_error", "unknown"}
    with pytest.raises(TypeError):
        FAILURE_KINDS["new_kind"] = failures.BLOCKED  # closed


@pytest.mark.parametrize("kind", sorted(FAILURE_KINDS))
def test_every_kind_round_trips(kind):
    record = FailureRecord(kind=kind, evidence="e", retry_after_sec=120,
                           reset_at="2026-09-17T12:00:00Z")
    assert record.failure_class == FAILURE_KINDS[kind]
    assert FailureRecord.from_dict(record.to_dict()) == record
    assert record.is_blocked == (FAILURE_KINDS[kind] == failures.BLOCKED)


def test_an_unknown_kind_or_a_mismatched_class_is_refused():
    with pytest.raises(ValueError, match="unknown failure kind"):
        FailureRecord(kind="cosmic_rays")
    with pytest.raises(ValueError, match="unknown failure kind"):
        FailureRecord.from_dict({"kind": "cosmic_rays"})
    with pytest.raises(ValueError, match="is 'blocked'"):
        FailureRecord(kind="rate_limited", failure_class="bad_output")
    with pytest.raises(ValueError):
        FailureRecord.from_dict(["rate_limited"])


def test_classify_node_result_reads_the_workers_record_first():
    record = FailureRecord(kind="rate_limited", evidence="429", retry_after_sec=120)
    assert failures.classify_node_result(
        _result(outputs={"failure": record.to_dict()})
    ) == record


def test_a_failed_result_without_a_record_is_unknown_or_a_failed_check():
    plain = failures.classify_node_result(_result(detail="boom"))
    assert (plain.kind, plain.evidence) == ("unknown", "boom")
    for handler in failures.VERIFICATION_HANDLERS:
        gate = failures.classify_node_result(_result(detail="2 failed"), handler_ref=handler)
        assert gate.kind == "verification_failed"


@pytest.mark.parametrize("status", ["succeeded", "uncertain", "paused", "incomplete"])
def test_only_a_failed_result_is_classified(status):
    assert failures.classify_node_result(_result(status=status)) is None


@pytest.mark.parametrize("evidence, kind", [
    ("You've hit your limit · resets 3pm (UTC)", "quota_exhausted"),
    ("usage limit reached", "quota_exhausted"),
    ("API Error: 429 monthly spend limit reached", "rate_limited"),
    ("rate-limit exceeded", "rate_limited"),
    ("Too Many Requests", "rate_limited"),
])
def test_a_quota_verdict_splits_into_rate_limit_or_spent_quota(evidence, kind):
    record = failures.classify_exit(
        ExitVerdict(kind="quota_exhausted", reset_at="2026-09-17T15:00:00Z", evidence=evidence)
    )
    assert (record.kind, record.reset_at) == (kind, "2026-09-17T15:00:00Z")


def test_the_exit_table_maps_crashes_and_misses_to_their_kinds():
    assert failures.classify_exit(ExitVerdict(kind="ok")) is None
    assert failures.classify_exit(ExitVerdict(kind="crashed", evidence="x")).kind == "worker_crashed"
    odd = failures.classify_exit(SimpleNamespace(kind="mystery", evidence="new wording", reset_at=None))
    assert odd.kind == "unknown" and "mystery" in odd.evidence and "new wording" in odd.evidence


def test_a_runtime_outcome_maps_its_stop_reason():
    ok = SimpleNamespace(status="succeeded", stop_reason="completed", detail="")
    assert failures.record_from_runtime(ok) is None
    limited = SimpleNamespace(status="failed", stop_reason="rate_limited", detail="HTTP 429",
                              retry_after_sec=120, reset_at=None)
    record = failures.record_from_runtime(limited)
    assert (record.kind, record.retry_after_sec, record.evidence) == ("rate_limited", 120, "HTTP 429")
    breach = SimpleNamespace(status="failed", stop_reason="confinement_breach", detail="../x")
    assert failures.record_from_runtime(breach).kind == "unknown"
    assert "confinement_breach" in failures.record_from_runtime(breach).evidence


# ── P12B F4: how long a blocked failure pauses its pool ──────────────────────────────


NOW = 1_800_000_000


def _pause(record, prior=0):
    return failures.pause_for(record, now_epoch=NOW, prior_blocks=prior, auth_pause_sec=3600)


def test_pause_for_prefers_the_failures_own_hints_in_order():
    reset = "2027-01-15T08:00:00Z"
    reset_epoch = int(datetime(2027, 1, 15, 8, tzinfo=timezone.utc).timestamp())
    assert _pause(FailureRecord(kind="context_overflow", reset_at=reset)) is None
    assert _pause(FailureRecord(kind="auth_failed", reset_at=reset, retry_after_sec=5)) == (
        NOW + 3600, True)
    assert _pause(FailureRecord(kind="quota_exhausted", reset_at=reset, retry_after_sec=5)) == (
        reset_epoch, True)
    assert _pause(FailureRecord(kind="rate_limited", retry_after_sec=120)) == (NOW + 120, True)
    assert _pause(FailureRecord(kind="rate_limited", reset_at="not a time",
                                retry_after_sec=7)) == (NOW + 7, True)


def test_pause_for_backs_off_exponentially_up_to_thirty_minutes_when_nothing_is_known():
    record = FailureRecord(kind="provider_unavailable")
    assert [_pause(record, prior) for prior in (0, 1, 2, 4, 5, 20)] == [
        (NOW + 60, False), (NOW + 120, False), (NOW + 240, False),
        (NOW + 960, False), (NOW + 1800, False), (NOW + 1800, False),
    ]
    assert failures.BACKOFF_CAP_SEC == 1800


# ── P12B F6: the retry cone of a `bad_output` failure ────────────────────────────────


def _revision(nodes, edges):
    from maestro.workflows.models import WorkflowRevision

    return WorkflowRevision(
        workflow_id="wf", revision=1, task_id="T-1", task_spec_revision=1,
        compiler_version="test", policy_hash="", code_base_sha="sha",
        execution_objective="paid_efficiency", nodes=tuple(nodes), edges=tuple(edges),
    )


def _agent(node_id, role="implementer", *, writer=True):
    from maestro.workflows.models import NodeSpec

    return NodeSpec(
        node_id=node_id, kind="agent", handler_ref="maestro.handlers.agent",
        agent_definition_ref=role,
        resource_request={"writer_lease": True} if writer else {},
    )


def _gate(node_id="gate"):
    from maestro.workflows.models import NodeSpec

    return NodeSpec(
        node_id=node_id, kind="function", handler_ref="maestro.handlers.verification",
    )


def _plain(node_id, handler="maestro.handlers.merge", kind="function"):
    from maestro.workflows.models import NodeSpec

    return NodeSpec(node_id=node_id, kind=kind, handler_ref=handler)


def _edge(source, target, guard="on_success"):
    from maestro.workflows.models import EdgeSpec

    return EdgeSpec(source, target, guard_predicate=guard,
                    required=guard != "on_failure")


def test_the_quality_ladder_is_the_first_run_plus_two_retries():
    assert failures.QUALITY_ATTEMPTS == 3


def test_a_failed_agent_writer_is_its_own_cone():
    revision = _revision(
        [_agent("implementer"), _gate(), _plain("park", "maestro.handlers.wait", "wait")],
        [_edge("implementer", "gate"), _edge("implementer", "park", "on_failure")],
    )
    assert failures.retry_cone(revision, "implementer") == ("implementer",)


def test_a_non_writing_agent_is_still_its_own_cone():
    revision = _revision(
        [_agent("implementer"), _agent("proof_review", "reviewer", writer=False)],
        [_edge("implementer", "proof_review")],
    )
    assert failures.retry_cone(revision, "proof_review") == ("proof_review",)


def test_a_rejecting_review_pulls_back_the_writer_like_a_gate():
    """P12 pilot finding D6: a review's rejection is a verdict on the implementer's work,
    so re-running the review alone would re-review the same tree."""
    revision = _revision(
        [_agent("implementer"), _gate(), _agent("proof_review", "reviewer", writer=False)],
        [_edge("implementer", "gate"), _edge("gate", "proof_review")],
    )
    assert failures.retry_cone(revision, "proof_review", kind="verification_failed") == (
        "implementer", "gate", "proof_review",
    )
    # A review that said nothing usable is the reviewer's own bad output.
    assert failures.retry_cone(revision, "proof_review", kind="garbage_output") == (
        "proof_review",
    )


def test_a_failed_gate_pulls_back_its_nearest_writer_and_everything_between():
    revision = _revision(
        [_agent("implementer"), _plain("stage"), _gate(), _plain("merge")],
        [_edge("implementer", "stage"), _edge("stage", "gate"), _edge("gate", "merge")],
    )
    assert failures.retry_cone(revision, "gate") == ("implementer", "stage", "gate")


def test_a_gate_prefers_the_nearest_writer_and_ignores_failure_edges():
    revision = _revision(
        [_agent("first"), _agent("second"), _gate(), _plain("park", "maestro.handlers.wait", "wait")],
        [
            _edge("first", "second"), _edge("second", "gate"),
            _edge("first", "park", "on_failure"), _edge("park", "gate", "on_failure"),
        ],
    )
    assert failures.retry_cone(revision, "gate") == ("second", "gate")


def test_a_gate_with_no_writer_upstream_has_no_cone():
    revision = _revision(
        [_plain("script"), _gate()], [_edge("script", "gate")],
    )
    assert failures.retry_cone(revision, "gate") == ()


def test_a_diagnoser_a_script_and_a_merge_are_never_retried():
    revision = _revision(
        [_agent("diagnose", "diagnoser", writer=False), _plain("merge"),
         _plain("script", "maestro.handlers.script")],
        [_edge("script", "merge"), _edge("merge", "diagnose")],
    )
    assert failures.retry_cone(revision, "diagnose") == ()
    assert failures.retry_cone(revision, "merge") == ()
    assert failures.retry_cone(revision, "script") == ()
