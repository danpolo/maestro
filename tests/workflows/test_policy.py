"""`maestro.workflows.policy` — the engineering policy and the five role contracts (`[P5]`).

Three properties are what this module is for, and each has a section below:

* **It fails closed.** A policy present but malformed raises; it never degrades into the
  defaults. Every test that asserts a refusal here is asserting that a typo cannot delete
  a quality gate.
* **Conditions are a closed set.** A stage's `include_when` names a predicate this package
  owns. Nothing in a policy file is evaluated as an expression, so a config file cannot
  become a code path.
* **Roles are contracts, not bindings** (`D04`). No definition names a backend, a model or
  an effort value, and the two review roles hold no tool that can write.

Nothing here touches a database, a worktree or a subprocess.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
import yaml

from maestro.workflows import policy as policy_mod
from maestro.workflows.policy import (
    AgentDefinition,
    FragmentRef,
    PolicyError,
    ProjectPolicy,
    QualityGates,
    RiskContext,
    StageSpec,
    TemplateSpec,
    default_policy,
    load_agent_definitions,
)

ROLE_IDS = ("implementer", "planner", "reviewer", "test_designer", "diagnoser")


@pytest.fixture
def packaged() -> ProjectPolicy:
    return default_policy()


def _document() -> dict:
    return yaml.safe_load(policy_mod.POLICY_TEMPLATE.read_text(encoding="utf-8"))


def _write(repo: Path, document) -> Path:
    path = repo / policy_mod.POLICY_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        document if isinstance(document, str) else yaml.safe_dump(document),
        encoding="utf-8",
    )
    return path


# ── the packaged policy ──────────────────────────────────────────────────────────────


def test_the_packaged_policy_loads_and_covers_every_execution_kind(packaged):
    assert set(packaged.templates) == {"ordinary", "script", "manual", "async", "resumable"}
    assert packaged.objective == "paid_efficiency"


def test_every_packaged_template_declares_a_fragment_version(packaged):
    # `[INV-A06]`: a template cites `name@version`, never a bare name. A stage that lost
    # its version would be a template that silently follows whatever the fragment becomes.
    for template in packaged.templates.values():
        assert template.version
        for stage in (*template.stages, *template.sinks):
            assert stage.fragment.version, f"{stage.node_id} cites no fragment version"


def test_a_project_without_a_policy_file_gets_the_packaged_one(tmp_path):
    assert ProjectPolicy.load(tmp_path).policy_hash == default_policy().policy_hash


def test_the_policy_hash_is_stable_across_loads_and_moves(tmp_path):
    # Two projects that adopted the same policy compile to the same `policy_hash`, so a
    # revision's provenance is comparable across checkouts.
    _write(tmp_path, _document())
    other = tmp_path / "elsewhere"
    other.mkdir()
    _write(other, _document())
    assert ProjectPolicy.load(tmp_path).policy_hash == ProjectPolicy.load(other).policy_hash


def test_the_policy_hash_changes_when_a_gate_changes(tmp_path):
    document = _document()
    before = ProjectPolicy.from_document(document).policy_hash
    document["quality_gates"]["review_required_when"] = []
    assert ProjectPolicy.from_document(document).policy_hash != before


def test_the_policy_hash_changes_when_a_role_contract_changes(packaged):
    # A workflow whose reviewer contract changed is not the same workflow, even when the
    # graph is node for node identical — so the role definitions are inside the hash.
    reviewer = packaged.agent_definitions["reviewer"]
    changed = dict(packaged.agent_definitions)
    changed["reviewer"] = dataclasses.replace(reviewer, version="9.9.9")
    assert dataclasses.replace(packaged, agent_definitions=changed).policy_hash != packaged.policy_hash


def test_the_source_path_is_not_part_of_the_policys_identity(packaged):
    moved = dataclasses.replace(packaged, source_path="/somewhere/else/project.yaml")
    assert moved.policy_hash == packaged.policy_hash


# ── fail-closed loading ──────────────────────────────────────────────────────────────


def test_a_malformed_policy_raises_rather_than_falling_back(tmp_path):
    _write(tmp_path, "templates: [this is a list, not a mapping\n")
    with pytest.raises(PolicyError):
        ProjectPolicy.load(tmp_path)


def test_an_empty_policy_file_is_a_refusal_not_a_default(tmp_path):
    _write(tmp_path, "\n")
    with pytest.raises(PolicyError, match="empty"):
        ProjectPolicy.load(tmp_path)


def test_an_unknown_stage_key_is_refused(tmp_path):
    document = _document()
    document["templates"]["ordinary"]["stages"][0]["exclude_when"] = ["high_risk"]
    with pytest.raises(PolicyError, match="unknown stage keys"):
        ProjectPolicy.from_document(document)


def test_an_unknown_condition_is_refused():
    with pytest.raises(PolicyError, match="unknown condition"):
        StageSpec(fragment=FragmentRef.parse("gate@1.0.0"), include_when=("on_tuesdays",))


def test_an_unknown_required_criterion_is_refused():
    with pytest.raises(PolicyError, match="unknown required criterion"):
        TemplateSpec(
            execution_kind="ordinary", version="1.0.0",
            stages=(StageSpec(fragment=FragmentRef.parse("gate@1.0.0")),),
            required_criteria=("looks_fine",),
        )


def test_a_template_without_a_version_is_refused():
    with pytest.raises(PolicyError, match="no version"):
        policy_mod._template_from_document("ordinary", {"stages": [{"fragment": "gate@1.0.0"}]})


def test_a_fragment_ref_without_a_version_is_refused():
    with pytest.raises(PolicyError, match="not 'name@version'"):
        FragmentRef.parse("gate")


def test_a_failure_sink_that_is_not_declared_is_refused():
    with pytest.raises(PolicyError, match="not a declared sink"):
        TemplateSpec(
            execution_kind="ordinary", version="1.0.0",
            stages=(StageSpec(fragment=FragmentRef.parse("gate@1.0.0"), on_failure="park"),),
        )


def test_an_unknown_objective_is_refused(tmp_path):
    document = _document()
    document["objective"] = "cheap_and_fast"
    with pytest.raises(PolicyError, match="unknown objective"):
        ProjectPolicy.from_document(document)


def test_a_future_schema_version_is_refused():
    document = _document()
    document["schema_version"] = 99
    with pytest.raises(PolicyError, match="schema_version"):
        ProjectPolicy.from_document(document)


def test_a_template_asking_for_a_role_with_no_definition_is_refused():
    document = _document()
    with pytest.raises(PolicyError, match="has no AgentDefinition"):
        ProjectPolicy.from_document(document, agent_definitions={})


# ── conditions and risk ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "risk,expected", [("low", False), ("medium", False), ("high", True), ("critical", True)]
)
def test_high_risk_covers_critical_too(risk, expected):
    assert RiskContext(task_id="T", risk=risk).holds("high_risk") is expected


def test_an_unknown_condition_raises_at_evaluation_too():
    with pytest.raises(PolicyError, match="unknown condition"):
        RiskContext(task_id="T").holds("vibes")


def test_scope_paths_decide_whether_a_task_touches_tests(packaged):
    spec = _spec(scope=["tests/workflows/test_policy.py"])
    assert packaged.risk_context(spec).touches_tests is True
    assert packaged.risk_context(_spec(scope=["maestro/cli.py"])).touches_tests is False


def test_risky_paths_are_matched_as_globs():
    gates = QualityGates(risky_paths=("migrations/*",))
    policy = dataclasses.replace(default_policy(), quality_gates=gates)
    assert policy.risk_context(_spec(scope=["migrations/0003_add.sql"])).touches_risky_paths
    assert not policy.risk_context(_spec(scope=["docs/x.md"])).touches_risky_paths


def test_the_objective_is_read_off_the_policy_not_the_task(packaged):
    assert packaged.risk_context(_spec()).objective == "paid_efficiency"
    assert packaged.with_objective("free_quality").risk_context(_spec()).objective == "free_quality"


# ── quality gates: the floor ─────────────────────────────────────────────────────────


def test_a_high_risk_task_requires_an_independent_review(packaged):
    ctx = packaged.risk_context(_spec(risk="high"))
    assert "independent_review" in packaged.required_criteria("ordinary", ctx)


def test_a_low_risk_task_requires_no_review(packaged):
    ctx = packaged.risk_context(_spec(risk="low"))
    assert "independent_review" not in packaged.required_criteria("ordinary", ctx)


def test_a_self_modifying_task_requires_an_independent_review(packaged):
    ctx = packaged.risk_context(_spec(self_modifying=True))
    assert "independent_review" in packaged.required_criteria("ordinary", ctx)


def test_the_template_floor_and_the_risk_floor_are_combined_without_duplicates(packaged):
    ctx = packaged.risk_context(_spec(risk="critical"))
    criteria = packaged.required_criteria("ordinary", ctx)
    assert len(criteria) == len(set(criteria))
    assert {"verification_gate", "controller_merge", "acceptance", "independent_review"} <= set(criteria)


def test_stage_selection_is_the_optimisation_and_the_gate_is_the_floor(packaged):
    # The two mechanisms are separate on purpose: a project that tunes `include_when`
    # down for cost still cannot compile a high-risk graph without a reviewer, because
    # the criteria check is not the same switch.
    template = packaged.template("ordinary")
    low = packaged.risk_context(_spec(risk="low"))
    assert "proof_review" not in [s.node_id for s in template.selected_stages(low)]
    high = packaged.risk_context(_spec(risk="high"))
    assert "proof_review" in [s.node_id for s in template.selected_stages(high)]


def test_an_unknown_condition_in_a_gate_is_refused():
    with pytest.raises(PolicyError, match="review_required_when"):
        QualityGates(review_required_when=("if_dan_is_asleep",))


# ── the five role definitions (`§2.5`) ───────────────────────────────────────────────


def test_the_five_specialised_roles_all_ship(packaged):
    assert set(packaged.agent_definitions) == set(ROLE_IDS)


@pytest.mark.parametrize("role_id", ROLE_IDS)
def test_every_role_states_a_responsibility_and_negative_boundaries(packaged, role_id):
    definition = packaged.definition(role_id)
    assert definition.responsibility.strip()
    assert definition.exclusions, f"{role_id} declares no MUST NOT boundaries"
    assert all(e.strip() for e in definition.exclusions)


@pytest.mark.parametrize("role_id", ROLE_IDS)
def test_every_role_declares_capability_requirements_not_a_backend(packaged, role_id):
    # `D04`: a definition that named its backend would be unroutable the moment that
    # backend ran out of quota. The role states what it needs; P6's router picks.
    definition = packaged.definition(role_id)
    assert definition.permitted_backend_capabilities.get("min_reasoning_strength") in (
        "small", "balanced", "strong"
    )
    # DESIGN.md §17.2: nor does it state an effort. The route carries it.
    assert "effort_policy" not in definition.to_dict()
    body = yaml.safe_dump(definition.to_dict()).lower()
    for backend_name in ("claude", "codex", "antigravity", "gpt-", "sonnet", "opus"):
        assert backend_name not in body, f"{role_id} names a backend or model"


@pytest.mark.parametrize("role_id", ROLE_IDS)
def test_every_role_declares_success_criteria_and_a_memory_policy(packaged, role_id):
    definition = packaged.definition(role_id)
    assert definition.success_criteria
    assert definition.memory_handoff_policy in AgentDefinition.MEMORY_POLICIES


def test_only_the_implementer_and_the_test_designer_hold_write_tools(packaged):
    writers = {r for r in ROLE_IDS if packaged.definition(r).writes}
    assert writers == {"implementer", "test_designer"}


def test_the_reviewer_and_planner_are_read_only_by_construction(packaged):
    for role_id in ("reviewer", "planner"):
        definition = packaged.definition(role_id)
        assert definition.permission_requirements == ("frozen_snapshot_reader",)
        assert not definition.writes


def test_the_test_designer_writes_to_a_scratch_workspace_not_the_worktree(packaged):
    definition = packaged.definition("test_designer")
    assert "scratch_workspace" in definition.permission_requirements
    assert "isolated_worktree" not in definition.permission_requirements


def test_a_reviewer_that_can_write_is_refused(packaged):
    # The independence a reviewer claims has to be structural. A definition holding
    # `write_file` while `independent_review` is required does not load at all.
    compromised = dict(packaged.agent_definitions)
    compromised["reviewer"] = dataclasses.replace(
        packaged.definition("reviewer"), logical_tools=("read_file", "write_file")
    )
    with pytest.raises(PolicyError, match="independent_review"):
        dataclasses.replace(packaged, agent_definitions=compromised)


def test_an_unknown_role_key_is_refused():
    with pytest.raises(PolicyError, match="unknown role definition keys"):
        AgentDefinition.from_document(
            {"role_id": "x", "version": "1", "responsibility": "y", "temperature": 0.7}
        )


def test_a_role_template_that_still_states_effort_policy_is_told_to_remove_it():
    # DESIGN.md §17.2: a role states no effort. A project's own template that still
    # carries the retired key gets that sentence, not "unknown role definition keys".
    with pytest.raises(PolicyError, match="effort_policy is retired"):
        AgentDefinition.from_document(
            {"role_id": "x", "version": "1", "responsibility": "y",
             "effort_policy": {"paid_default": "medium"}}
        )


def test_an_unknown_memory_handoff_policy_is_refused():
    with pytest.raises(PolicyError, match="memory_handoff_policy"):
        AgentDefinition.from_document(
            {"role_id": "x", "version": "1", "responsibility": "y",
             "memory_handoff_policy": "remembers_everything"}
        )


def test_a_project_may_shadow_a_packaged_role_definition(tmp_path):
    # Overriding, not extending: the file replaces the packaged contract wholesale, so
    # the role stays readable off one page.
    roles = tmp_path / policy_mod.PROJECT_ROLES_RELPATH
    roles.mkdir(parents=True)
    packaged = default_policy().definition("diagnoser").to_dict()
    packaged["version"] = "2.0.0"
    (roles / "diagnoser.yaml").write_text(yaml.safe_dump(packaged), encoding="utf-8")
    assert ProjectPolicy.load(tmp_path).definition("diagnoser").version == "2.0.0"


def test_a_role_file_whose_id_disagrees_with_its_name_is_refused(tmp_path):
    roles = tmp_path / "roles"
    roles.mkdir()
    body = default_policy().definition("reviewer").to_dict()
    (roles / "diagnoser.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
    with pytest.raises(PolicyError, match="does not match filename"):
        load_agent_definitions(roles)


# ── helpers ──────────────────────────────────────────────────────────────────────────


def _spec(**fields):
    from maestro.taskgraph import normalize_spec

    block = {"id": "T-001", "title": "a task", "status": "pending"}
    self_modifying = fields.pop("self_modifying", False)
    block.update(fields)
    spec = normalize_spec(block)
    return dataclasses.replace(spec, self_modifying=self_modifying) if self_modifying else spec
