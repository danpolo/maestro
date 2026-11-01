"""P9: ContextManifest is pinned, role-bounded, budgeted — and never cuts the contract."""
from __future__ import annotations

from dataclasses import replace

import pytest

from maestro.repository.context import (
    MANIFEST_FIELDS,
    ContextError,
    build_context_manifest,
    role_definition_hash,
)
from maestro.repository.index import StaleSnapshotError
from maestro.repository.queries import RepositoryQueries
from maestro.taskgraph import TaskSpec
from maestro.workflows.policy import ROLES_TEMPLATE_DIR, default_policy, load_agent_definitions

NARRATIVE = "I am confident this is correct because I tested it myself."
PATCH = "diff --git a/pkg/core.py b/pkg/core.py\n+    return x + 2\n"


@pytest.fixture(scope="module")
def roles():
    return load_agent_definitions(ROLES_TEMPLATE_DIR)


@pytest.fixture(scope="module")
def policy(roles):
    return default_policy(agent_definitions=roles)


@pytest.fixture
def task():
    return TaskSpec(
        task_id="T-P9",
        goal="make helper saturate at 10",
        acceptance_criteria=("helper(9) == 10", "helper(10) == 10"),
        scope=("pkg/core.py",),
        constraints=("do not change Engine's public signature",),
    )


def _build(repo, selection, task, role, policy, **material):
    root, sha = repo
    q = RepositoryQueries(root, selection=selection)
    return build_context_manifest(task, role, sha, policy, queries=q, **material), q


def _everything():
    return dict(
        diff=PATCH, candidate_patch=PATCH, implementer_narrative=NARRATIVE,
        decisions=[{"id": "ADR-7", "title": "helpers are pure"}],
        prior_failures=[{"ref": "art_deadbeef_1", "summary": "helper(10) returned 11"}],
        test_results=[{"check": "pytest", "status": "failed"}],
    )


def _kinds(manifest):
    return {ref["kind"] for ref in manifest.source_refs}


def test_manifest_carries_exactly_the_spec_fields(repo, selection, task, roles, policy):
    manifest, _ = _build(repo, selection, task, roles["implementer"], policy)
    assert list(manifest.to_dict()) == list(MANIFEST_FIELDS)
    assert manifest.snapshot_id == repo[1]
    assert manifest.policy_hash == policy.policy_hash
    assert manifest.role_definition_hash == role_definition_hash(roles["implementer"])
    assert manifest.freshness == "exact_match"


def test_implementer_gets_target_files_tests_and_failures_but_no_architecture(repo, selection, task, roles, policy):
    manifest, _ = _build(repo, selection, task, roles["implementer"], policy, **_everything())
    assert manifest.entrypoints == ("pkg/core.py",)
    (file_ref,) = [r for r in manifest.source_refs if r["kind"] == "file"]
    assert (file_ref["path"], file_ref["start_line"], file_ref["end_line"]) == ("pkg/core.py", 1, 10)
    assert len(file_ref["content_hash"]) == 64
    assert [t["path"] for t in manifest.related_tests_and_contracts] == ["tests/test_core.py"]
    assert manifest.prior_failure_refs == ("art_deadbeef_1",)
    assert manifest.relevant_decisions == ()
    assert any(o.startswith("decisions: withheld") for o in manifest.omitted_material)


def test_mandatory_instructions_hold_the_role_contract_and_the_task_constraints(repo, selection, task, roles, policy):
    role = roles["implementer"]
    manifest, _ = _build(repo, selection, task, role, policy)
    for exclusion in role.exclusions:
        assert exclusion in manifest.mandatory_instructions
    assert "constraint: do not change Engine's public signature" in manifest.mandatory_instructions
    assert manifest.acceptance_criteria == task.acceptance_criteria


def test_implementer_contract_allows_maestro_result_files_in_its_workspace(roles):
    exclusions = "\n".join(roles["implementer"].exclusions)
    assert "MUST NOT write outside the assigned worktree" in exclusions
    assert "WORKSPACE" in exclusions and "DONE" in exclusions


def test_budget_never_truncates_instructions_or_criteria(repo, selection, task, roles, policy):
    huge = tuple(f"criterion {i}: " + "x" * 400 for i in range(50))
    big_task = replace(task, acceptance_criteria=huge)
    tiny = replace(roles["implementer"], context_assembly_rules={"max_context_tokens": 10})
    manifest, _ = _build(repo, selection, big_task, tiny, policy, **_everything())
    assert manifest.acceptance_criteria == huge
    assert all(e in manifest.mandatory_instructions for e in tiny.exclusions)
    assert manifest.source_refs == () and manifest.prior_failure_refs == ()
    assert any("over budget" in o for o in manifest.omitted_material)
    assert manifest.context_estimate_tokens > 10
    assert any("alone need" in u for u in manifest.unresolved_queries)


def test_budget_drops_optional_material_in_priority_order(repo, selection, task, roles, policy):
    role = roles["implementer"]
    unbounded, _ = _build(repo, selection, task, role, policy, **_everything())
    fixed = sum(len(t) for t in (*unbounded.mandatory_instructions, *unbounded.acceptance_criteria)) // 4 + 20
    squeezed = replace(role, context_assembly_rules={**role.context_assembly_rules, "max_context_tokens": fixed + 12})
    manifest, _ = _build(repo, selection, task, squeezed, policy, **_everything())
    assert manifest.prior_failure_refs == ("art_deadbeef_1",), "highest priority survives"
    assert "file" not in _kinds(manifest)
    assert manifest.context_estimate_tokens <= fixed + 12


def test_reviewer_gets_the_frozen_diff_and_results_but_never_the_narrative(repo, selection, task, roles, policy):
    manifest, _ = _build(repo, selection, task, roles["reviewer"], policy, **_everything())
    assert {"diff", "test_results"} <= _kinds(manifest)
    assert "implementer_narrative" not in _kinds(manifest)
    assert NARRATIVE not in repr(manifest.to_dict())
    assert any(o.startswith("implementer_narrative: withheld") for o in manifest.omitted_material)


def test_test_designer_gets_public_interfaces_and_never_the_patch(repo, selection, task, roles, policy):
    manifest, _ = _build(repo, selection, task, roles["test_designer"], policy, **_everything())
    assert "diff" not in _kinds(manifest) and "file" not in _kinds(manifest)
    signatures = {r["symbol"]: (r["start_line"], r["end_line"]) for r in manifest.source_refs if r["kind"] == "signature"}
    assert signatures == {"helper": (1, 1), "Engine": (5, 5), "Engine.run": (6, 6)}
    assert any(o.startswith("candidate_patch: withheld") for o in manifest.omitted_material)


def test_planner_gets_decisions_and_dependencies_but_no_source_bodies(repo, selection, task, roles, policy):
    manifest, _ = _build(repo, selection, task, roles["planner"], policy, **_everything())
    assert "file" not in _kinds(manifest)
    assert {r["symbol"] for r in manifest.source_refs if r["kind"] == "signature"} == {"helper", "Engine"}
    assert {r["path"] for r in manifest.source_refs if r["kind"] == "import_edge"} == {"pkg/app.py", "tests/test_core.py"}
    assert manifest.relevant_decisions == ({"id": "ADR-7", "title": "helpers are pure"},)


def test_an_unknown_role_fails_closed(repo, selection, task, roles, policy):
    stranger = replace(roles["implementer"], role_id="setup_adviser")
    with pytest.raises(ContextError):
        _build(repo, selection, task, stranger, policy)


def test_the_role_hash_moves_when_the_contract_does(roles):
    role = roles["reviewer"]
    assert role_definition_hash(role) != role_definition_hash(replace(role, exclusions=role.exclusions + ("new",)))


def test_a_scope_entry_that_matches_nothing_is_reported(repo, selection, task, roles, policy):
    manifest, _ = _build(repo, selection, replace(task, scope=("pkg/core.py", "pkg/gone.py")), roles["implementer"], policy)
    assert manifest.entrypoints == ("pkg/core.py",)
    assert any("pkg/gone.py" in u for u in manifest.unresolved_queries)


def test_a_stale_overlay_is_rejected_or_flagged(repo, selection, task, roles, policy, tmp_path):
    from .conftest import run_git

    root, sha = repo
    wt = tmp_path / "wt"
    run_git(root, "worktree", "add", "-q", "-b", "impl/T-P9", str(wt), sha)
    (wt / "pkg/core.py").write_text("def helper(x):\n    return min(x + 1, 10)\n")
    q = RepositoryQueries(root, selection=selection)
    overlay = q.overlay(wt, sha)
    fresh = build_context_manifest(task, roles["implementer"], overlay.snapshot_id, policy, queries=q)
    assert fresh.freshness == "exact_match" and fresh.snapshot_id == overlay.snapshot_id
    (wt / "pkg/core.py").write_text("def helper(x):\n    return 10\n")
    with pytest.raises(StaleSnapshotError):
        build_context_manifest(task, roles["implementer"], overlay.snapshot_id, policy, queries=q)
    flagged = build_context_manifest(task, roles["implementer"], overlay.snapshot_id, policy, queries=q, on_stale="flag")
    assert flagged.freshness == "partial"
    assert flagged.acceptance_criteria == task.acceptance_criteria


def test_queries_context_delegates_to_the_builder(repo, selection, task, roles, policy):
    root, sha = repo
    q = RepositoryQueries(root, selection=selection)
    manifest = q.context(task, roles["reviewer"], sha, policy, diff=PATCH)
    assert manifest.task_id == "T-P9" and "diff" in _kinds(manifest)


def test_over_long_notes_are_bounded_and_reported_as_omitted(repo, selection, task, roles, policy):
    """P12 pilot finding D28: a task's notes are carried verbatim and counted with the
    contract; past `MAX_NOTES_TOKENS` they are cut at a line, never silently."""
    from maestro.repository.context import MAX_NOTES_TOKENS, estimate_tokens
    from maestro.workflows.worker import agent_brief

    short = "Retry the whole call with backoff.\nNever repeat the create POST."
    manifest, _ = _build(repo, selection, replace(task, legacy_fields={"notes": short}),
                         roles["implementer"], policy)
    bare, _ = _build(repo, selection, task, roles["implementer"], policy)
    assert manifest.task_notes == short and bare.task_notes == ""
    assert manifest.context_estimate_tokens - bare.context_estimate_tokens == estimate_tokens(short)
    assert not any(m.startswith("task_notes:") for m in manifest.omitted_material)

    line = "Requirement: every write is one PUT of the full ordered list, no partial state.\n"
    long = (line * (MAX_NOTES_TOKENS * 4 // len(line) + 20)).strip()
    manifest, _ = _build(repo, selection, replace(task, legacy_fields={"notes": long}),
                         roles["reviewer"], policy)
    kept = manifest.task_notes
    assert long.startswith(kept) and kept.endswith("no partial state.")
    assert len(kept) <= MAX_NOTES_TOKENS * 4
    [cut] = [m for m in manifest.omitted_material if m.startswith("task_notes:")]
    assert f"the last {len(long) - len(kept)} of {len(long)} characters cut" in cut
    assert "T-P9's `notes:` in docs/ROADMAP.md" in cut
    brief = agent_brief(manifest.to_dict(), role="reviewer")
    assert kept in brief and f"[{cut}]" in brief
