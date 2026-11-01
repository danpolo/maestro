"""`[P7]`, `[INV-06]`: candidates stay apart, and exactly one of them wins.

The acceptance condition this file carries is "candidates never share writer worktrees", and
it is pinned at both levels — in the compiled graph (distinct node ids, distinct
`candidate_id`s, distinct exclusive writer leases) and on disk (distinct branches under the
task namespace, distinct worktree directories, a commit boundary that refuses the other
candidate's paths).

The selection half pins the other clause of `[INV-06]`: exactly one winner, never a mixture,
and a fail-closed refusal when nothing has conclusive evidence. A join that returned "the
best of the unproven" would satisfy every test about isolation and still be the bug.
"""
from __future__ import annotations

import pytest

from maestro import confinement
from maestro.workflows import candidates as cand
from maestro.workflows import compiler
from maestro.workflows.policy import default_policy

from .conftest import task_spec


@pytest.fixture
def policy():
    return default_policy().with_objective("free_quality")


@pytest.fixture
def revision(policy):
    """A medium-risk ordinary task in free mode — the shape that fans out."""
    return compiler.compile_workflow(task_spec(risk="medium"), policy=policy)


@pytest.fixture
def specs(repo, revision):
    return cand.specs_from_nodes(repo, "T-001", revision.nodes)


def outcome(spec, *, status="succeeded", passed=1, failed=0, commit="abc123", ids=None):
    """A candidate outcome with `passed`/`failed` pieces of evidence.

    Stand-ins rather than real `VerificationEvidence` rows: the join reads `evidence_id` and
    `passed` and nothing else, and building signed evidence here would test P4 instead.
    """
    evidence = []
    for index in range(passed + failed):
        evidence.append(
            type(
                "Evidence", (), {
                    "evidence_id": (ids or [])[index]
                    if ids and index < len(ids)
                    else f"ev-{spec.candidate_id}-{index}",
                    "passed": index < passed,
                }
            )()
        )
    return cand.CandidateOutcome(
        spec=spec, status=status, evidence=tuple(evidence), head_commit=commit
    )


# ── isolation in the compiled graph ──────────────────────────────────────────────────


def test_free_mode_composes_two_candidate_implementers(revision):
    ids = [n.node_id for n in revision.nodes if n.params.get("candidate_id")]
    assert ids == ["implementer_c1", "implementer_c2"]


def test_candidates_never_share_a_writer_lease(revision):
    """The compiled form of "candidates never share writer worktrees".

    Without a distinct `resource_id` the runner falls back to `worktree:<task>`, and two
    candidates would then queue for one directory — one worktree written twice, which is
    cross-pollution with extra steps rather than isolation.
    """
    leases = [
        node.resource_request.get("resource_id")
        for node in revision.nodes
        if node.params.get("candidate_id")
    ]
    assert all(leases), "a candidate implementer with no exclusive resource_id"
    assert len(set(leases)) == len(leases)
    assert leases == ["worktree:T-001/c1", "worktree:T-001/c2"]


def test_every_candidate_still_holds_its_own_writer_lease(revision):
    for node in revision.nodes:
        if node.params.get("candidate_id"):
            assert node.wants_writer_lease is True


def test_the_candidate_fan_out_is_joined_by_exactly_one_selection_node(revision):
    joins = [
        n for n in revision.nodes
        if n.handler_ref == "maestro.handlers.candidate_select"
    ]
    assert len(joins) == 1
    incoming = {e.source_node_id for e in revision.edges if e.target_node_id == "candidate_select"}
    assert incoming == {"implementer_c1", "implementer_c2"}


def test_a_crashed_candidate_cannot_route_around_the_join(policy):
    """P12A W4: the join is reached whatever a candidate's phase, so it can reap it.

    Edges into the join are `always` and required, so a failed candidate still lets the
    join run and report it `abandoned`; no candidate has its own `on_failure` edge to the
    sink, because the join's `on_failure -> park` is what "no conclusive candidate" means.
    """
    import dataclasses

    wide = dataclasses.replace(
        policy, free_quality=dataclasses.replace(policy.free_quality, candidates=3)
    )
    revision = compiler.compile_workflow(task_spec(risk="medium"), policy=wide)
    candidates = {n.node_id for n in revision.nodes if n.params.get("candidate_id")}
    assert candidates == {"implementer_c1", "implementer_c2", "implementer_c3"}
    into_join = [e for e in revision.edges if e.target_node_id == "candidate_select"]
    assert {e.source_node_id for e in into_join} == candidates
    assert all(e.guard_predicate == "always" and e.required for e in into_join)
    assert not [e for e in revision.edges if e.source_node_id in candidates
                and e.target_node_id != "candidate_select"]
    assert [(e.target_node_id, e.guard_predicate) for e in revision.edges
            if e.source_node_id == "candidate_select"] == [("gate", "on_success"),
                                                          ("park", "on_failure")]


def test_an_unwidened_implementer_keeps_its_success_and_failure_edges(policy):
    """Width 1 is byte-identical: `on_success` onward, optional `on_failure` to park."""
    revision = compiler.compile_workflow(task_spec(risk="low"), policy=policy)
    assert [(e.target_node_id, e.guard_predicate, e.required) for e in revision.edges
            if e.source_node_id == "implementer"] == [
        ("gate", "on_success", True), ("park", "on_failure", False),
    ]


def test_a_widened_lane_that_loses_its_join_does_not_compile(policy):
    """`[INV-06]` as a compile-time floor, not a convention.

    Editing the join out of the template is exactly the change that would leave N patches
    and no winner, so the criteria check refuses the graph rather than emitting it.
    """
    import dataclasses

    template = policy.template("ordinary")
    without_join = dataclasses.replace(
        template,
        stages=tuple(s for s in template.stages if s.node_id != "candidate_select"),
    )
    broken = dataclasses.replace(policy, templates={**policy.templates, "ordinary": without_join})
    with pytest.raises(Exception, match="candidate_selection"):
        compiler.compile_workflow(task_spec(risk="medium"), policy=broken)


def test_a_low_risk_free_task_buys_no_candidates(policy):
    revision = compiler.compile_workflow(task_spec(risk="low"), policy=policy)
    assert "implementer" in revision.node_ids
    assert "candidate_select" not in revision.node_ids
    assert not any(n.params.get("candidate_id") for n in revision.nodes)


# ── isolation on disk ────────────────────────────────────────────────────────────────


def test_candidate_branches_sit_under_the_task_namespace(specs):
    assert [s.branch for s in specs] == ["impl/T-001/c1", "impl/T-001/c2"]
    for spec in specs:
        assert confinement.is_namespaced(spec.branch)


def test_candidate_worktrees_are_distinct_and_under_the_task_directory(repo, specs):
    task_root = confinement.worktree_for(repo, "T-001")
    paths = [s.worktree for s in specs]
    assert len(set(paths)) == len(paths)
    for path in paths:
        assert path.parent == task_root


def test_a_candidates_commit_boundary_refuses_its_siblings_paths(specs):
    first, second = specs
    boundary = first.boundary()
    permitted, _ = boundary.permits(first.worktree / "file.py")
    assert permitted is True
    refused, reason = boundary.permits(second.worktree / "file.py")
    assert refused is False
    assert reason


def test_a_candidate_may_not_write_the_target_branch(specs):
    boundary = specs[0].boundary()
    permitted, reason = boundary.permits(specs[0].repo / ".git" / "refs" / "heads" / "main")
    assert permitted is False
    assert reason


def test_the_specs_read_off_the_graph_match_the_graphs_own_leases(revision, specs):
    by_node = {n.node_id: n for n in revision.nodes}
    for spec in specs:
        assert spec.node_id in by_node
        assert by_node[spec.node_id].resource_request["resource_id"] == spec.resource_id


# ── the selection join ───────────────────────────────────────────────────────────────


def test_selection_returns_exactly_one_winner(specs):
    selection = cand.select_winner([outcome(specs[0], passed=2), outcome(specs[1], passed=1)])
    assert selection.winner.candidate_id == "c1"
    assert selection.losers == ("c2",)
    assert selection.branch == "impl/T-001/c1"


def test_more_passing_evidence_wins(specs):
    selection = cand.select_winner([outcome(specs[0], passed=1), outcome(specs[1], passed=3)])
    assert selection.winner.candidate_id == "c2"


def test_a_tie_breaks_on_index_so_two_controllers_agree(specs):
    first = cand.select_winner([outcome(specs[0], passed=2), outcome(specs[1], passed=2)])
    reversed_order = cand.select_winner(
        [outcome(specs[1], passed=2), outcome(specs[0], passed=2)]
    )
    assert first.winner.candidate_id == reversed_order.winner.candidate_id == "c1"


def test_a_candidate_with_a_failed_check_cannot_win(specs):
    selection = cand.select_winner(
        [outcome(specs[0], passed=5, failed=1), outcome(specs[1], passed=1)]
    )
    assert selection.winner.candidate_id == "c2"


def test_no_conclusive_candidate_fails_closed(specs):
    with pytest.raises(cand.NoWinnerError):
        cand.select_winner([outcome(specs[0], failed=1, passed=0), outcome(specs[1], status="failed")])


def test_a_candidate_with_no_evidence_is_not_a_clean_candidate(specs):
    with pytest.raises(cand.NoWinnerError):
        cand.select_winner([outcome(specs[0], passed=0), outcome(specs[1], passed=0)])


def test_a_candidate_with_no_commit_is_not_conclusive(specs):
    assert outcome(specs[0], commit="").conclusive is False


def test_selection_with_no_candidates_fails_closed():
    with pytest.raises(cand.NoWinnerError):
        cand.select_winner([])


# ── cross-pollution ──────────────────────────────────────────────────────────────────


def test_two_candidates_sharing_a_worktree_are_refused(repo, specs):
    clone = cand.CandidateSpec(
        repo=repo, task_id="T-001", candidate_id="c1", index=2, node_id="implementer_c2"
    )
    with pytest.raises(cand.CrossPollutionError, match="share"):
        cand.assert_no_cross_pollution([outcome(specs[0]), outcome(clone)])


def test_two_candidates_graded_by_one_piece_of_evidence_are_refused(specs):
    shared = ["ev-shared"]
    with pytest.raises(cand.CrossPollutionError, match="share evidence"):
        cand.assert_no_cross_pollution(
            [outcome(specs[0], ids=shared), outcome(specs[1], ids=shared)]
        )


def test_a_nested_candidate_worktree_is_refused(repo, specs):
    nested = cand.CandidateSpec(
        repo=repo / ".orchestrator" / "worktrees" / "T-001" / "c1",
        task_id="T-001", candidate_id="c2", index=2,
    )
    with pytest.raises(cand.CrossPollutionError):
        cand.assert_no_cross_pollution([outcome(specs[0]), outcome(nested)])


def test_selection_checks_isolation_before_choosing(specs):
    """A winner chosen out of a polluted set is not a winner. The check runs first, so the
    refusal names the pollution rather than reporting a selection that should not exist."""
    shared = ["ev-shared"]
    with pytest.raises(cand.CrossPollutionError):
        cand.select_winner([outcome(specs[0], ids=shared), outcome(specs[1], ids=shared)])


def test_a_missing_candidate_document_is_abandoned_not_dropped(specs):
    """Shrinking the set to one would make it trivially unpolluted — the wrong way to pass."""
    outcomes = cand.outcomes_from_documents(
        specs, {"c1": {"status": "succeeded", "head_commit": "abc", "evidence": []}}
    )
    assert [o.candidate_id for o in outcomes] == ["c1", "c2"]
    assert outcomes[1].status == "abandoned"
    assert outcomes[1].conclusive is False


def test_the_join_exposes_no_way_to_combine_candidates(specs):
    """`[INV-06]`: mixing is prevented by an API in which it does not exist. `losers` is a
    tuple of ids, so a caller cannot reach a losing patch through the decision."""
    selection = cand.select_winner([outcome(specs[0]), outcome(specs[1])])
    assert all(isinstance(loser, str) for loser in selection.losers)
    assert not hasattr(selection, "merge")
