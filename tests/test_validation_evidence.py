"""`[P4]`: evidence is a receipt, not a report.

Every test here is one shape of the same refusal. An agent says it passed; a cached
result describes a tree that has since changed; a reviewer answers with an empty object;
a deployment adapter cannot say whether it already deployed. In each case the question is
whether the system treats the missing proof as a proof, and in each case it must not.
"""
from __future__ import annotations

import subprocess

import pytest

from maestro import adapters, validation
from maestro.control.store import ControlStore


# ── fixtures ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".orchestrator").mkdir(parents=True)
    return root


@pytest.fixture
def store(repo):
    store = ControlStore.open_controller(repo / ".orchestrator" / "control.sqlite3")
    yield store
    store.close()


@pytest.fixture
def registry(store):
    return validation.EvidenceRegistry(store=store)


def passing_spec(check_id="unit", **kwargs):
    defaults = dict(
        check_id=check_id,
        argv_or_adapter_ref=("true",),
        acceptance_criterion_ids=(check_id,),
        cache_policy="never",
    )
    defaults.update(kwargs)
    return validation.VerificationSpec(**defaults)


def evidence(check_id="unit", outcome="passed", **kwargs):
    defaults = dict(
        evidence_id=f"evi_{check_id}_{outcome}",
        check_id=check_id,
        source_snapshot_id="snap",
        attempt_id="att",
        command_hash="cmd",
        input_fingerprint="inp",
        environment_fingerprint="env",
        exit_status=0 if outcome == "passed" else 1,
        outcome=outcome,
    )
    defaults.update(kwargs)
    return validation.VerificationEvidence(**defaults)


# ── the specs themselves ─────────────────────────────────────────────────────────────


def test_a_spec_declares_which_criteria_it_discharges():
    spec = validation.VerificationSpec.from_roadmap(
        {"id": "V1", "kind": "auto", "cmd": "echo ok", "expect": "ok"}
    )
    assert spec.acceptance_criterion_ids == ("V1",)
    assert spec.expected_output_schema == "last_stdout_line"
    assert spec.shell is True


@pytest.mark.parametrize('case,status,cause', [
    ('missing', 127, 'environment_missing'),
    ('empty_tests', 5, 'check_broken'),
    ('arbitrary_five', 5, 'check_failed'),
    ('arbitrary_127', 127, 'check_failed'),
    ('compound', 127, None),
    ('masked_pytest', 5, None),
    ('expansion', 127, None),
    ('nested_shell', 5, None),
])
def test_roadmap_shell_evidence_uses_command_identity_not_output(
        registry, repo, tmp_path, case, status, cause):
    import shlex
    import sys
    empty = tmp_path / 'empty tests'
    empty.mkdir()
    python = shlex.quote(sys.executable)
    commands = {
        'missing': shlex.quote(str(tmp_path / 'missing tool')),
        'empty_tests': f'{python} -m pytest -q {shlex.quote(str(empty))}',
        'arbitrary_five': f'{python} -c "import sys; print(\'no tests collected\'); sys.exit(5)"',
        'arbitrary_127': f'{python} -c "import sys; print(\'command not found\'); sys.exit(127)"',
        'compound': 'true && /w2-fixture/no-such-tool',
        'masked_pytest': f'{python} -m pytest -q {shlex.quote(str(empty))}; exit 5',
        'expansion': '${W2_MISSING_EXECUTABLE:-/w2-fixture/no-such-tool}',
        'nested_shell': '/bin/sh -c "exit 5"',
    }
    spec = validation.VerificationSpec.from_roadmap(
        {'id': 'V1', 'kind': 'auto', 'cmd': commands[case]})
    receipt = validation.CheckRunner(registry).run(spec, repo, attempt_id='att')
    assert receipt.outcome == 'failed'
    assert receipt.exit_status == status
    assert receipt.extra_step_cause == cause


def test_a_manual_verification_becomes_a_reviewer_check_with_no_command():
    """A `kind: manual` item used to be discharged by the implementer's own prose.

    Lifting it to a check with an empty argv is what makes that impossible: there is
    nothing to run, so it cannot produce evidence, so it can only be discharged through a
    reviewer disposition that `decide()` scrutinises separately.
    """
    spec = validation.VerificationSpec.from_roadmap({"id": "V2", "kind": "manual"})
    assert spec.handler_ref == "maestro.validation.reviewer_check"
    assert spec.argv_or_adapter_ref == ()
    assert spec.cacheable is False


def test_an_auto_verification_with_no_command_is_refused_at_construction():
    with pytest.raises(validation.ValidationError):
        validation.VerificationSpec.from_roadmap({"id": "V3", "kind": "auto"})


def test_an_unknown_cache_policy_is_refused():
    with pytest.raises(validation.ValidationError):
        passing_spec(cache_policy="sometimes")


# ── fingerprints (`§2.1`) ────────────────────────────────────────────────────────────


def test_the_command_hash_covers_the_adapter_source_not_just_the_argv():
    """An adapter edited between runs is a different check with the same argv."""
    spec = passing_spec()
    before = validation.command_hash(spec, adapter_source=b"#!/bin/sh\nexit 0\n")
    after = validation.command_hash(spec, adapter_source=b"#!/bin/sh\nexit 1\n")
    assert before != after


def test_the_input_fingerprint_moves_when_a_declared_input_byte_changes(tmp_path):
    (tmp_path / "src").mkdir()
    target = tmp_path / "src" / "thing.py"
    target.write_text("def f():\n    return 1\n")
    before = validation.input_fingerprint(tmp_path, ["src/**/*.py"])
    # Same public signature, different behaviour — `§2.2` is explicit that this must
    # invalidate evidence, which is why the fingerprint hashes content, not interfaces.
    target.write_text("def f():\n    return 2\n")
    after = validation.input_fingerprint(tmp_path, ["src/**/*.py"])
    assert before != after


def test_the_input_fingerprint_ignores_the_git_directory(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "a.py").write_text("x = 1\n")
    before = validation.input_fingerprint(tmp_path, ["**/*"])
    (tmp_path / ".git" / "index").write_bytes(b"\x00\x01")
    assert validation.input_fingerprint(tmp_path, ["**/*"]) == before


def test_the_environment_fingerprint_records_declared_names_not_secret_values(monkeypatch):
    monkeypatch.setenv("MAESTRO_TEST_TOKEN", "s3cret")
    printed = validation.environment_fingerprint({"env": {"MAESTRO_TEST_TOKEN": None}})
    monkeypatch.setenv("MAESTRO_TEST_TOKEN", "different")
    assert validation.environment_fingerprint({"env": {"MAESTRO_TEST_TOKEN": None}}) != printed
    assert "s3cret" not in printed


def test_fingerprints_match_only_when_all_four_agree():
    base = validation.Fingerprints("snap", "cmd", "inp", "env")
    assert base.matches(validation.Fingerprints("snap", "cmd", "inp", "env"))
    assert not base.matches(validation.Fingerprints("snap", "cmd", "inp", "other"))


# ── evidence reuse (`§2.1`, `§2.2`) ──────────────────────────────────────────────────


def test_evidence_is_reused_only_on_an_exact_four_hash_match(registry, store):
    spec = passing_spec(
        cache_policy="exact_snapshot_only", dependency_completeness="exact",
        input_dependencies=("**/*.py",),
    )
    original = evidence()
    registry.record(original)
    exact = validation.Fingerprints("snap", "cmd", "inp", "env")
    assert registry.reusable(spec, exact) is not None
    for drifted in (
        validation.Fingerprints("other", "cmd", "inp", "env"),
        validation.Fingerprints("snap", "other", "inp", "env"),
        validation.Fingerprints("snap", "cmd", "other", "env"),
        validation.Fingerprints("snap", "cmd", "inp", "other"),
    ):
        assert registry.reusable(spec, drifted) is None


def test_source_drift_invalidates_cached_evidence_immediately(repo, registry, store, tmp_path):
    """The P4 acceptance condition, exercised end to end through `CheckRunner`.

    The first run executes and records. The second, with the declared input edited by one
    byte, must execute again — a reuse here would be the system accepting a proof about a
    tree that no longer exists.
    """
    worktree = tmp_path / "wt"
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "a.py").write_text("x = 1\n")
    spec = passing_spec(
        cache_policy="exact_snapshot_only", dependency_completeness="exact",
        input_dependencies=("src/**/*.py",),
    )
    checker = validation.CheckRunner(registry=registry)
    first = checker.run(spec, worktree, attempt_id="att-1", snapshot="snap")
    assert first.reused_from is None

    second = checker.run(spec, worktree, attempt_id="att-2", snapshot="snap")
    assert second.reused_from == first.evidence_id, "identical inputs should reuse"

    (worktree / "src" / "a.py").write_text("x = 2\n")
    third = checker.run(spec, worktree, attempt_id="att-3", snapshot="snap")
    assert third.reused_from is None, "a source edit must force re-execution"
    assert third.input_fingerprint != first.input_fingerprint


def test_a_never_cacheable_check_is_never_served_from_cache(registry):
    """Network calls, browser tests and staging smokes (`§2.2`)."""
    spec = passing_spec(cache_policy="never")
    registry.record(evidence())
    assert registry.reusable(spec, validation.Fingerprints("snap", "cmd", "inp", "env")) is None


def test_a_check_with_unknown_dependencies_is_not_cacheable_whatever_it_declares():
    spec = passing_spec(cache_policy="exact_snapshot_only", dependency_completeness="unknown")
    assert spec.cacheable is False


def test_a_reused_record_names_what_it_reused(registry):
    original = registry.record(evidence())
    reused = registry.reuse(original, attempt_id="att-2")
    assert reused.reused_from == original.evidence_id
    assert reused.evidence_id != original.evidence_id
    assert "input_fingerprint" in (reused.reuse_reason or "")


def test_a_failing_check_is_never_reused(repo, registry, tmp_path):
    """Only a passing cached record short-circuits; a failure re-runs and can recover."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    spec = validation.VerificationSpec(
        check_id="unit", argv_or_adapter_ref=("false",),
        cache_policy="exact_snapshot_only", dependency_completeness="exact",
        input_dependencies=("*.py",), acceptance_criterion_ids=("unit",),
    )
    checker = validation.CheckRunner(registry=registry)
    first = checker.run(spec, worktree, attempt_id="a1", snapshot="snap")
    assert first.outcome == "failed"
    second = checker.run(spec, worktree, attempt_id="a2", snapshot="snap")
    assert second.reused_from is None


# ── running checks ───────────────────────────────────────────────────────────────────


def test_a_check_that_times_out_is_timed_out_not_failed(registry, tmp_path):
    """Distinct outcomes because the recoveries differ: a timeout says nothing about
    the code, and recording it as a failure would blame the change for the machine."""
    spec = validation.VerificationSpec(
        check_id="slow", argv_or_adapter_ref=("sleep 5",), shell=True, timeout=1,
        acceptance_criterion_ids=("slow",),
    )
    record = validation.CheckRunner(registry=registry).run(
        spec, tmp_path, attempt_id="a", snapshot="snap"
    )
    assert record.outcome == "timed_out"
    assert record.passed is False


def test_an_expected_output_mismatch_fails_even_on_a_zero_exit(registry, tmp_path):
    spec = validation.VerificationSpec(
        check_id="echo", argv_or_adapter_ref=("echo NOPE",), shell=True,
        expected_output="OK", expected_output_schema="last_stdout_line",
        acceptance_criterion_ids=("echo",),
    )
    record = validation.CheckRunner(registry=registry).run(
        spec, tmp_path, attempt_id="a", snapshot="snap"
    )
    assert record.outcome == "failed" and record.exit_status == 0


def test_a_reviewer_check_produces_no_evidence_when_the_suite_is_evaluated(registry, tmp_path):
    specs = [passing_spec("unit"), validation.VerificationSpec.from_roadmap(
        {"id": "V2", "kind": "manual"}
    )]
    produced = validation.CheckRunner(registry=registry).evaluate(
        specs, tmp_path, attempt_id="a", snapshot="snap"
    )
    assert [record.check_id for record in produced] == ["unit"]


def test_evidence_and_its_output_artifact_are_durable(repo, store, tmp_path):
    from maestro.control.artifacts import ArtifactStore

    artifacts = ArtifactStore(repo / ".orchestrator" / "artifacts", store)
    registry = validation.EvidenceRegistry(store=store, artifacts=artifacts)
    spec = validation.VerificationSpec(
        check_id="unit", argv_or_adapter_ref=("echo hello",), shell=True,
        acceptance_criterion_ids=("unit",),
    )
    record = validation.CheckRunner(registry=registry).run(
        spec, tmp_path, attempt_id="a", snapshot="snap"
    )
    assert store.evidence(record.evidence_id) is not None
    assert record.output_artifact_refs
    assert b"hello" in artifacts.read(record.output_artifact_refs[0])


# ── the closed-world decision (`§1.3`, `[INV-04]`) ───────────────────────────────────


def test_an_agent_authored_done_and_tests_pass_cannot_accept_a_task(tmp_path):
    """The P4 acceptance condition, stated as bluntly as it can be.

    The workspace below is exactly what a finished implementer leaves: a `DONE` sentinel
    and a `result.json` claiming every verification passed. `decide()` is given the same
    task and no evidence, and it rejects — because there is no parameter through which
    any of those files could have reached it.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "DONE").write_text("all green")
    (workspace / "result.json").write_text('{"tests": {"pass": true}, "verifications": {}}')

    decision = validation.decide(
        task_id="T-001", run_id="run", specs=[passing_spec("unit")], evidence=[],
    )
    assert decision.decision == "rejected"
    assert "no evidence" in decision.reason


def test_a_task_declaring_no_verification_at_all_fails_closed():
    """The legacy gate printed "no verifications defined — pass (no gate)"."""
    decision = validation.decide(task_id="T", run_id="r", specs=[], evidence=[])
    assert decision.decision == "rejected"
    assert "empty closed world" in decision.reason


def test_a_missing_proof_key_fails_closed():
    """Two criteria declared, one discharged. The undischarged one decides."""
    specs = [passing_spec("unit"), passing_spec("lint")]
    decision = validation.decide(
        task_id="T", run_id="r", specs=[specs[0]], evidence=[evidence("unit")],
        required_criterion_ids=["unit", "lint"],
    )
    assert decision.decision == "rejected"
    assert "'lint'" in decision.reason


def test_every_required_check_passing_accepts():
    specs = [passing_spec("unit"), passing_spec("lint")]
    decision = validation.decide(
        task_id="T", run_id="r", specs=specs,
        evidence=[evidence("unit"), evidence("lint", evidence_id="evi_lint")],
        integrated_source_snapshot="snap",
    )
    assert decision.accepted
    assert len(decision.required_evidence_ids) == 2


def test_evidence_from_a_different_snapshot_cannot_accept_this_one():
    decision = validation.decide(
        task_id="T", run_id="r", specs=[passing_spec("unit")],
        evidence=[evidence("unit", source_snapshot_id="old")],
        integrated_source_snapshot="new", expected_snapshot="new",
    )
    assert decision.decision == "rejected"
    assert "not on 'new'" in decision.reason


def test_invalidated_evidence_does_not_discharge_anything():
    decision = validation.decide(
        task_id="T", run_id="r", specs=[passing_spec("unit")],
        evidence=[evidence("unit", validity="invalidated")],
    )
    assert decision.decision == "rejected"


def test_an_unreceipted_external_effect_blocks_acceptance():
    decision = validation.decide(
        task_id="T", run_id="r", specs=[passing_spec("unit")], evidence=[evidence("unit")],
        required_effects=["push"], effect_receipts={},
    )
    assert decision.decision == "rejected"
    assert "push" in decision.reason


def test_an_exemption_without_a_reason_and_scope_discharges_nothing():
    decision = validation.decide(
        task_id="T", run_id="r", specs=[passing_spec("unit")], evidence=[],
        exemptions=[{"criterion_id": "unit"}],
    )
    assert decision.decision == "rejected"
    assert "exemption" in decision.reason


def test_a_fully_formed_exemption_discharges_its_criterion():
    decision = validation.decide(
        task_id="T", run_id="r", specs=[passing_spec("unit", required=False)], evidence=[],
        required_criterion_ids=["unit"],
        exemptions=[{
            "criterion_id": "unit", "reason": "the harness is down, verified by hand",
            "scope": "T only", "approved_by": "dan",
        }],
    )
    assert decision.accepted


# ── reviewer records fail closed (`[INV-04]`) ────────────────────────────────────────


def test_an_empty_reviewer_verdict_is_not_an_approval():
    ok, why = validation.review_is_conclusive({"check_id": "V2", "verdict": ""})
    assert not ok and "fails closed" in why


def test_a_rejection_with_no_findings_is_not_a_review():
    ok, why = validation.review_is_conclusive(
        {"check_id": "V2", "verdict": "rejected", "findings": []}
    )
    assert not ok and "without a single finding" in why


def test_an_inconclusive_review_blocks_acceptance_even_with_green_checks():
    decision = validation.decide(
        task_id="T", run_id="r", specs=[passing_spec("unit")], evidence=[evidence("unit")],
        review_dispositions=[{"check_id": "V2", "verdict": ""}],
    )
    assert decision.decision == "rejected"


def test_the_proof_review_gate_refuses_a_judge_that_answered_about_nothing(monkeypatch):
    """`gates.sonnet_review_proofs` used to read `{}` as "no failures", i.e. as a pass."""
    from maestro import gates
    from maestro.backends.base import Completion

    monkeypatch.setattr(
        gates.agentcall, "ask",
        lambda *a, **k: Completion(text="{}", returncode=0, timed_out=False),
    )
    passed, feedback = gates.sonnet_review_proofs(
        "T-001",
        [{"id": "V1", "kind": "manual", "check": "the token flow"}],
        {"V1": {"done": True, "proof": "walked both consent flows and read the stored row"}},
    )
    assert passed is False
    assert "no verdict" in feedback


# ── human approvals (`§5`) ───────────────────────────────────────────────────────────


def test_an_approval_stops_binding_when_the_patch_changes():
    approval = validation.ApprovalBinding(
        task_id="T", run_id="r", subject_sha="aaa", policy_hash="p",
        expires_at="2099-01-01T00:00:00Z",
    )
    ok, _ = approval.validate(task_id="T", run_id="r", subject_sha="aaa", policy_hash="p")
    assert ok
    stale, why = approval.validate(
        task_id="T", run_id="r", subject_sha="bbb", policy_hash="p"
    )
    assert not stale and "changed after it was granted" in why


def test_an_approval_stops_binding_when_the_policy_changes():
    approval = validation.ApprovalBinding(
        task_id="T", run_id="r", subject_sha="aaa", policy_hash="p1",
        expires_at="2099-01-01T00:00:00Z",
    )
    ok, _ = approval.validate(task_id="T", run_id="r", subject_sha="aaa", policy_hash="p2")
    assert not ok


def test_an_expired_approval_is_not_an_approval():
    approval = validation.ApprovalBinding(
        task_id="T", run_id="r", subject_sha="aaa", policy_hash="p",
        expires_at="2000-01-01T00:00:00Z",
    )
    ok, why = approval.validate(
        task_id="T", run_id="r", subject_sha="aaa", policy_hash="p",
        now="2026-09-12T00:00:00Z",
    )
    assert not ok and "expired" in why


# ── effect classes and postcondition probes (`A05`) ──────────────────────────────────


def test_a_repeatable_local_check_may_be_retried():
    allowed, _ = validation.retry_allowed("repeatable_local_check")
    assert allowed


def test_an_unprobed_external_effect_is_never_retried_automatically():
    allowed, why = validation.retry_allowed("external_side_effect")
    assert not allowed and "A05" in why


def test_a_probe_that_confirms_the_effect_records_rather_than_repeats():
    allowed, why = validation.retry_allowed("external_side_effect", probe=lambda: True)
    assert not allowed
    assert "record a receipt" in why


def test_a_probe_that_denies_the_effect_leaves_the_state_uncertain():
    allowed, why = validation.retry_allowed("external_side_effect", probe=lambda: False)
    assert not allowed and "uncertain" in why


def test_a_probe_that_raises_answered_nothing():
    def broken():
        raise RuntimeError("no network")

    allowed, why = validation.retry_allowed("external_side_effect", probe=broken)
    assert not allowed and "raised" in why


def test_an_unknown_effect_class_fails_closed():
    allowed, why = validation.retry_allowed("something_new")
    assert not allowed and "fails closed" in why


def test_adapters_declare_their_effect_class_and_deploy_is_not_repeatable():
    assert adapters.effect_class("test") == "repeatable_local_check"
    assert adapters.effect_class("deploy") == "external_side_effect"
    assert adapters.effect_class("a_kind_nobody_defined") == "external_side_effect"
    assert adapters.retry_allowed("test")[0] is True
    assert adapters.retry_allowed("deploy")[0] is False


def test_a_registered_probe_decides_a_deploy_adapters_recovery(monkeypatch):
    monkeypatch.setitem(adapters._PROBES, "deploy", lambda ctx: ctx.get("live_sha") == "abc")
    confirmed, why = adapters.retry_allowed("deploy", context={"live_sha": "abc"})
    assert not confirmed and "already happened" in why
    denied, why = adapters.retry_allowed("deploy", context={"live_sha": "zzz"})
    assert not denied and "uncertain" in why


def test_the_adapter_source_is_part_of_the_command_identity(tmp_path):
    (tmp_path / "adapters").mkdir()
    path = tmp_path / "adapters" / "test"
    path.write_text("#!/bin/sh\nexit 0\n")
    first = adapters.adapter_source(tmp_path, "test")
    path.write_text("#!/bin/sh\nexit 1\n")
    assert adapters.adapter_source(tmp_path, "test") != first
    assert adapters.adapter_source(tmp_path, "absent") == b""


# ── durability ───────────────────────────────────────────────────────────────────────


def test_an_acceptance_decision_is_committed_with_its_event(repo, store):
    from maestro.control.lifecycle import Lifecycle

    store.register_task("T-001", spec_revision=1, status="planned")
    life = Lifecycle(store)
    life.set_task_status("T-001", "active")
    decision = validation.decide(
        task_id="T-001", run_id="run", specs=[passing_spec("unit")],
        evidence=[evidence("unit")], integrated_source_snapshot="snap",
    )
    committed = validation.commit_acceptance(store, life, decision)
    assert committed.event_id
    rows = store.acceptances_for("T-001")
    assert len(rows) == 1 and rows[0]["decision"] == "accepted"
    assert store.task("T-001")["status"] == "accepted"


def test_a_rejection_is_recorded_and_does_not_move_the_task(repo, store):
    from maestro.control.lifecycle import Lifecycle

    store.register_task("T-002", spec_revision=1, status="planned")
    life = Lifecycle(store)
    life.set_task_status("T-002", "active")
    decision = validation.decide(
        task_id="T-002", run_id="run", specs=[passing_spec("unit")], evidence=[],
    )
    validation.commit_acceptance(store, life, decision)
    assert store.acceptances_for("T-002")[0]["decision"] == "rejected"
    assert store.task("T-002")["status"] == "active"


def test_a_client_store_cannot_write_evidence(repo, store):
    """`[INV-01]`: a worker that could write evidence could write itself a pass."""
    from maestro.control.store import SingleWriterViolation

    client = ControlStore.open_client(repo / ".orchestrator" / "control.sqlite3")
    try:
        with pytest.raises(SingleWriterViolation):
            client.record_evidence(evidence().to_row())
    finally:
        client.close()
