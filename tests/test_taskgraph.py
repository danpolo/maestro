"""The `[P2]` roadmap dependency model.

Specification, not characterisation: unlike `tests/characterization/test_roadmap.py`,
which pins what the loose reader *does*, everything here asserts what the strict model
must *refuse*. The three acceptance conditions of the phase get their own sections —
cycles report exact paths, a split preserves every incoming and outgoing obligation, and
a legacy completion unblocks dependents without ever standing in for evidence.
"""
from __future__ import annotations

import json

import pytest

from maestro import taskgraph
from maestro.control.store import ControlStore


# --- document builders --------------------------------------------------------------


def block(body: str) -> str:
    return "```yaml\n" + body.strip("\n") + "\n```\n"


def section(task_id: str, *, deps: list[str] | None = None, title: str = "", **extra) -> str:
    lines = [f"id: {task_id}"]
    if title:
        lines.append(f"title: {title}")
    lines.append("deps: [" + ", ".join(deps or []) + "]")
    for key, value in extra.items():
        lines.append(f"{key}: {json.dumps(value)}")
    return f"\n### {task_id}\n\nProse a human wrote and nothing may touch.\n\n" + block("\n".join(lines))


def document(*sections: str) -> str:
    return "# Roadmap\n\nIntro prose.\n" + "".join(sections)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / ".orchestrator").mkdir(parents=True)
    return root


def write_roadmap(repo, text: str):
    taskgraph.roadmap_path(repo).write_text(text, encoding="utf-8")
    return taskgraph.load_document(taskgraph.roadmap_path(repo))


def write_registry(repo, ids: list[str]) -> None:
    taskgraph.registry_path(repo).write_text(
        json.dumps([{"id": i, "title": i} for i in ids]), encoding="utf-8"
    )


# ══════════════════════════════════════════════════════════════════════════
# normalization & schema
# ══════════════════════════════════════════════════════════════════════════


def test_normalizes_a_legacy_block_into_a_taskspec():
    spec = taskgraph.normalize_spec({
        "id": "T-1", "title": "Do it", "short_desc": "The concrete objective.",
        "deps": ["T-0"], "risk": "high", "hold": True,
    })
    assert spec.task_id == "T-1"
    assert spec.title == "Do it"
    assert spec.goal == "The concrete objective."
    assert spec.depends_on == ("T-0",)
    assert spec.risk == "high"
    assert spec.hold is True
    assert spec.spec_revision == 1


def test_unknown_fields_survive_in_legacy_fields():
    spec = taskgraph.normalize_spec({
        "id": "T-1", "est_time": "~2h", "eval_relevance": "retrieval",
        "post_action_cmd": "python3 scripts/x.py",
    })
    assert spec.legacy_fields == {
        "est_time": "~2h", "eval_relevance": "retrieval",
        "post_action_cmd": "python3 scripts/x.py",
    }


def test_a_mapped_field_is_not_duplicated_into_legacy_fields():
    spec = taskgraph.normalize_spec({"id": "T-1", "title": "x", "deps": ["A"], "hold": True})
    assert spec.legacy_fields == {}


def test_round_trips_through_the_document_vocabulary():
    original = {
        "id": "T-1", "title": "Do it", "short_desc": "Objective.", "est_time": "~2h",
        "mode": "autonomous", "eval_relevance": "retrieval", "deps": ["T-0"],
        "verifications": [{"id": "V1", "kind": "auto", "check": "the table is populated"}],
    }
    spec = taskgraph.normalize_spec(original)
    assert taskgraph.normalize_spec(spec.to_yaml_dict()) == spec


def test_dan_decisions_round_trip_through_the_document_vocabulary():
    # P12 pilot finding D26: Dan's answers to agents' spec questions live in the block.
    original = {
        "id": "T-1", "title": "Do it", "deps": [],
        "decisions": [{"at": "2026-09-28T10:00:00Z", "asked_by": "reviewer/review#1",
                       "question": "Which half owns a shared track?", "answer": "B"}],
    }
    spec = taskgraph.normalize_spec(original)
    assert spec.legacy_fields["decisions"] == original["decisions"]
    assert spec.to_yaml_dict()["decisions"] == original["decisions"]
    assert taskgraph.normalize_spec(spec.to_yaml_dict()) == spec


def test_acceptance_criteria_are_read_off_the_verification_checks():
    spec = taskgraph.normalize_spec({
        "id": "T-1",
        "verifications": [
            {"id": "V1", "kind": "auto", "check": "exit code is 0"},
            {"id": "V2", "kind": "manual", "check": "a human agrees"},
        ],
    })
    assert spec.acceptance_criteria == ("exit code is 0", "a human agrees")
    # a view of the verifications, not a move: they stay where the legacy readers look
    assert len(spec.legacy_fields["verifications"]) == 2


def test_a_dan_check_becomes_a_criterion_the_agent_is_told_not_to_attempt():
    # P12 pilot finding D25: DuetFlow 06's implementer tried to create a real Duet.
    spec = taskgraph.normalize_spec({
        "id": "T-1",
        "verifications": [
            {"id": "V1", "kind": "auto", "check": "exit code is 0"},
            {"id": "V-DAN", "kind": "dan", "check": "A real Duet exists in Spotify."},
        ],
    })
    assert spec.acceptance_criteria == (
        "exit code is 0",
        "A real Duet exists in Spotify." + taskgraph.DAN_CRITERION_NOTE,
    )
    assert "do not attempt it" in taskgraph.DAN_CRITERION_NOTE
    # the note survives the round-trip once, not twice
    assert taskgraph.normalize_spec(spec.to_yaml_dict()) == spec


@pytest.mark.parametrize("data,expected", [
    ({"id": "T", "mode": "autonomous"}, "ordinary"),
    ({"id": "T", "mode": "needs-dan"}, "manual"),
    ({"id": "T", "dispatch": "manual"}, "manual"),
    ({"id": "T", "kind": "script"}, "script"),
    ({"id": "T", "kind": "resumable"}, "resumable"),
])
def test_execution_kind_is_derived_from_the_legacy_mode_vocabulary(data, expected):
    assert taskgraph.normalize_spec(data).execution_kind == expected


def test_the_derivation_does_not_consume_mode_or_dispatch():
    spec = taskgraph.normalize_spec({"id": "T", "mode": "needs-dan", "dispatch": "manual"})
    assert spec.legacy_fields["mode"] == "needs-dan"
    assert spec.legacy_fields["dispatch"] == "manual"


def test_a_bare_string_dep_is_one_dependency_not_five_characters():
    assert taskgraph.normalize_spec({"id": "T", "deps": "T-0"}).depends_on == ("T-0",)


def test_a_block_without_an_id_is_not_a_spec():
    with pytest.raises(taskgraph.MutationError):
        taskgraph.normalize_spec({"title": "nameless"})


def test_prose_between_blocks_is_preserved_by_the_document_model(repo):
    text = document(section("A"), section("B", deps=["A"]))
    doc = write_roadmap(repo, text)
    assert doc.text == text
    assert "Prose a human wrote" in doc.text
    assert len(doc.blocks) == 2


# ══════════════════════════════════════════════════════════════════════════
# strict validation — every issue carries a location
# ══════════════════════════════════════════════════════════════════════════


def test_malformed_yaml_reports_the_exact_line(repo):
    text = document(section("A")) + "\n### B\n\n" + block("id: B\ndeps: [\n  unclosed")
    doc = write_roadmap(repo, text)
    issues = taskgraph.validate(doc)
    assert [i.code for i in issues] == ["malformed_yaml"]
    line = issues[0].line
    assert text.splitlines()[line - 1].strip().startswith(("unclosed", "deps", "```"))
    assert issues[0].block_index == 1


def test_a_duplicate_id_names_both_declarations(repo):
    doc = write_roadmap(repo, document(section("A"), section("A")))
    issues = taskgraph.validate(doc)
    assert [i.code for i in issues] == ["duplicate_id"]
    assert issues[0].task_id == "A"
    assert issues[0].block_index == 1
    assert "first declaration is at line" in issues[0].message


def test_parse_fallback_accepts_only_step_down():
    """P12B F5: `fallback:` is absent or `step_down`; anything else is refused."""
    assert taskgraph.parse_fallback(None) is None
    assert taskgraph.parse_fallback("step_down") == "step_down"
    for bad in ("", "step_up", True, ["step_down"]):
        with pytest.raises(taskgraph.FallbackSpecError, match="T-1: fallback must be one of"):
            taskgraph.parse_fallback(bad, task_id="T-1")


def test_the_fallback_flag_round_trips_and_is_written_only_when_set():
    spec = taskgraph.normalize_spec({"id": "A", "fallback": "step_down"})
    assert spec.fallback == "step_down" and "fallback" not in spec.legacy_fields
    assert spec.to_yaml_dict()["fallback"] == "step_down"
    assert "fallback" not in taskgraph.normalize_spec({"id": "A"}).to_yaml_dict()


def test_an_invalid_fallback_is_an_issue_and_the_task_is_kept_without_it(repo):
    doc = write_roadmap(repo, document(section("A", fallback="sideways", resources={"gpu": 1})))
    issues = taskgraph.validate(doc)
    assert sorted(i.code for i in issues) == ["invalid_fallback", "invalid_resources"]
    assert all(i.task_id == "A" for i in issues)


def test_a_missing_prerequisite_is_an_error_with_a_location(repo):
    doc = write_roadmap(repo, document(section("A", deps=["GHOST"])))
    issues = taskgraph.validate(doc)
    assert [i.code for i in issues] == ["missing_prerequisite"]
    assert issues[0].task_id == "A"
    assert issues[0].line is not None


def test_a_graduated_task_is_a_legal_prerequisite(repo):
    doc = write_roadmap(repo, document(section("A", deps=["OLD-1"])))
    assert taskgraph.validate(doc, known_ids={"OLD-1"}) == []


def test_a_cycle_reports_the_exact_path(repo):
    doc = write_roadmap(repo, document(
        section("A", deps=["C"]), section("B", deps=["A"]), section("C", deps=["B"]),
    ))
    issues = taskgraph.validate(doc)
    cycles = [i for i in issues if i.code == "cycle"]
    assert len(cycles) == 1
    assert cycles[0].cycle == ("A", "C", "B")
    assert cycles[0].message == "dependency cycle: A -> C -> B -> A"


def test_every_independent_cycle_is_reported(repo):
    doc = write_roadmap(repo, document(
        section("A", deps=["B"]), section("B", deps=["A"]),
        section("C", deps=["D"]), section("D", deps=["C"]),
    ))
    cycles = {i.cycle for i in taskgraph.validate(doc) if i.code == "cycle"}
    assert cycles == {("A", "B"), ("C", "D")}


def test_a_self_dependency_is_a_cycle(repo):
    doc = write_roadmap(repo, document(section("A", deps=["A"])))
    assert [i.cycle for i in taskgraph.validate(doc) if i.code == "cycle"] == [("A",)]


def test_a_cycle_is_reported_once_however_it_is_spelled():
    cycles = taskgraph.find_cycles({"A": ["C"], "B": ["A"], "C": ["B"]})
    assert cycles == [("A", "C", "B")]


def test_build_graph_refuses_an_invalid_document(repo):
    doc = write_roadmap(repo, document(section("A", deps=["GHOST"])))
    with pytest.raises(taskgraph.RoadmapValidationError) as excinfo:
        taskgraph.build_graph(doc)
    assert [i.code for i in excinfo.value.issues] == ["missing_prerequisite"]


def test_an_empty_and_a_non_mapping_block_are_both_located(repo):
    text = document(section("A")) + block("") + block("- just\n- a list")
    issues = taskgraph.validate(write_roadmap(repo, text))
    assert [i.code for i in issues] == ["empty_block", "not_a_mapping"]
    assert all(i.line is not None for i in issues)


def test_commented_example_is_preserved_without_becoming_a_task(repo):
    example = "## Example (commented out — not a real task)\n\n" + block(
        "# id: example-001\n\n  # deps: [MISSING]\n# title: Example")
    doc = write_roadmap(repo, document(example, section("A")))
    graph = taskgraph.build_graph(doc)
    assert set(graph.specs) == {"A"}
    txn = taskgraph.RoadmapTransaction(doc)
    txn.add_task({"id": "B", "deps": ["A"]})
    committed = txn.commit()
    assert example in committed.text
    assert set(taskgraph.build_graph(committed).specs) == {"A", "B"}


@pytest.mark.parametrize("body", ["  \n", "null", "# explanation\nnull", "# explanation\n---"])
def test_non_comment_empty_yaml_is_still_refused(body):
    issues = taskgraph.validate(taskgraph.parse_document(block(body)))
    assert [issue.code for issue in issues] == ["empty_block"]


# ══════════════════════════════════════════════════════════════════════════
# the graph
# ══════════════════════════════════════════════════════════════════════════


def test_order_is_topological(repo):
    doc = write_roadmap(repo, document(
        section("C", deps=["B"]), section("A"), section("B", deps=["A"]),
    ))
    graph = taskgraph.build_graph(doc)
    assert graph.order.index("A") < graph.order.index("B") < graph.order.index("C")


def test_ready_is_the_tasks_whose_prerequisites_are_discharged(repo):
    write_registry(repo, ["A"])
    doc = write_roadmap(repo, document(
        section("B", deps=["A"]), section("C", deps=["B"]), section("D", deps=["A"], hold=True),
    ))
    graph = taskgraph.build_graph(doc, registry_ids=taskgraph.read_registry_ids(repo))
    assert graph.ready() == ("B",)


def test_transitive_dependents_is_the_whole_downstream_cone(repo):
    doc = write_roadmap(repo, document(
        section("A"), section("B", deps=["A"]), section("C", deps=["B"]), section("D"),
    ))
    graph = taskgraph.build_graph(doc)
    assert graph.transitive_dependents("A") == ("B", "C")
    assert graph.transitive_dependents("D") == ()


# ══════════════════════════════════════════════════════════════════════════
# legacy completions: they unblock, they never prove
# ══════════════════════════════════════════════════════════════════════════


def test_a_graduated_task_is_imported_as_legacy_accepted(repo):
    write_registry(repo, ["OLD-1"])
    doc = write_roadmap(repo, document(section("A", deps=["OLD-1"])))
    graph = taskgraph.build_graph(doc, registry_ids=taskgraph.read_registry_ids(repo))
    assert graph.acceptances["OLD-1"].kind == "legacy_accepted"


def test_a_legacy_acceptance_satisfies_a_dependency(repo):
    write_registry(repo, ["OLD-1"])
    doc = write_roadmap(repo, document(section("A", deps=["OLD-1"])))
    graph = taskgraph.build_graph(doc, registry_ids=taskgraph.read_registry_ids(repo))
    assert graph.unmet("A") == ()
    assert graph.ready() == ("A",)


def test_a_legacy_acceptance_can_never_supply_evidence_or_a_cache_hit(repo):
    write_registry(repo, ["OLD-1"])
    doc = write_roadmap(repo, document(section("A", deps=["OLD-1"])))
    graph = taskgraph.build_graph(doc, registry_ids=taskgraph.read_registry_ids(repo))
    assert graph.supplies_evidence("OLD-1") is False
    assert graph.cache_eligible("OLD-1") is False


def test_an_in_document_completion_is_also_evidence_free(repo):
    doc = write_roadmap(repo, document(
        section("A", status="complete"), section("B", deps=["A"]),
    ))
    graph = taskgraph.build_graph(doc)
    assert graph.acceptances["A"].kind == "in_document_complete"
    assert graph.acceptances["A"].satisfies_dependencies is True
    assert graph.supplies_evidence("A") is False
    assert graph.ready() == ("B",)


# ══════════════════════════════════════════════════════════════════════════
# mutation — transactional, and it never reformats what it did not touch
# ══════════════════════════════════════════════════════════════════════════


def txn(repo, text, registry=()):
    return taskgraph.RoadmapTransaction(write_roadmap(repo, text), registry_ids=registry)


def test_add_task_appends_a_section(repo):
    t = txn(repo, document(section("A")))
    t.add_task(taskgraph.TaskSpec(task_id="B", title="New", depends_on=("A",)))
    out = t.commit()
    assert "### B — New" in out.text
    assert taskgraph.build_graph(out).prerequisites("B") == ("A",)


def test_add_task_refuses_a_duplicate(repo):
    t = txn(repo, document(section("A")))
    with pytest.raises(taskgraph.MutationError):
        t.add_task(taskgraph.TaskSpec(task_id="A"))


def test_add_dependency_refuses_an_unknown_parent(repo):
    t = txn(repo, document(section("A")))
    with pytest.raises(taskgraph.MutationError):
        t.add_dependency("A", "GHOST")


def test_add_dependency_that_closes_a_cycle_writes_nothing(repo):
    text = document(section("A"), section("B", deps=["A"]))
    t = txn(repo, text)
    t.add_dependency("A", "B")
    with pytest.raises(taskgraph.RoadmapValidationError) as excinfo:
        t.commit()
    assert any(i.code == "cycle" for i in excinfo.value.issues)
    assert taskgraph.roadmap_path(repo).read_text() == text


def test_a_mutation_leaves_every_untouched_block_byte_for_byte(repo):
    text = document(section("A"), section("B", deps=["A"]))
    t = txn(repo, text)
    t.add_dependency("B", "A")  # already there — a no-op
    t.add_task(taskgraph.TaskSpec(task_id="C", depends_on=("B",)))
    out = t.commit()
    assert out.text.startswith(text)


def test_a_mutation_bumps_only_the_touched_task_revision(repo):
    t = txn(repo, document(section("A"), section("B")))
    t.add_dependency("B", "A")
    assert t.specs["B"].spec_revision == 2
    assert t.specs["A"].spec_revision == 1


# --- split ---------------------------------------------------------------------------


def test_split_preserves_incoming_obligations(repo):
    t = txn(repo, document(section("P0"), section("P", deps=["P0"]), section("D", deps=["P"])))
    t.split_task("P", [
        taskgraph.TaskSpec(task_id="P-a"),
        taskgraph.TaskSpec(task_id="P-b", depends_on=("P-a",)),
    ])
    graph = taskgraph.build_graph(t.commit())
    assert graph.prerequisites("P-a") == ("P0",)      # entry child inherited them
    assert graph.prerequisites("P-b") == ("P-a",)


def test_split_preserves_outgoing_obligations(repo):
    t = txn(repo, document(section("P"), section("D", deps=["P"])))
    t.split_task("P", [
        taskgraph.TaskSpec(task_id="P-a"),
        taskgraph.TaskSpec(task_id="P-b", depends_on=("P-a",)),
    ])
    graph = taskgraph.build_graph(t.commit())
    assert graph.prerequisites("D") == ("P-b",)       # rewired onto the exit child
    assert "P" not in graph.prerequisites("D")


def test_split_turns_the_parent_into_a_group(repo):
    t = txn(repo, document(section("P"), section("D", deps=["P"])))
    t.split_task("P", [taskgraph.TaskSpec(task_id="P-a")])
    graph = taskgraph.build_graph(t.commit())
    assert graph.spec("P").is_group is True
    assert graph.spec("P").dispatchable is False
    assert graph.spec("P-a").group_id == "P"


def test_split_with_several_exit_children_makes_downstream_wait_for_all(repo):
    t = txn(repo, document(section("P"), section("D", deps=["P"])))
    t.split_task("P", [
        taskgraph.TaskSpec(task_id="P-a"),
        taskgraph.TaskSpec(task_id="P-b"),
    ])
    graph = taskgraph.build_graph(t.commit())
    assert set(graph.prerequisites("D")) == {"P-a", "P-b"}


def test_split_refuses_children_that_have_no_entry(repo):
    t = txn(repo, document(section("P")))
    with pytest.raises(taskgraph.MutationError, match="no entry child"):
        t.split_task("P", [
            taskgraph.TaskSpec(task_id="P-a", depends_on=("P-b",)),
            taskgraph.TaskSpec(task_id="P-b", depends_on=("P-a",)),
        ])


def test_split_refuses_an_empty_child_list(repo):
    t = txn(repo, document(section("P")))
    with pytest.raises(taskgraph.MutationError):
        t.split_task("P", [])


# --- supersede -----------------------------------------------------------------------


def test_supersede_rewires_dependents_and_inherits_prerequisites(repo):
    t = txn(repo, document(
        section("P0"), section("OLD", deps=["P0"]), section("NEW"), section("D", deps=["OLD"]),
    ))
    t.supersede_task("OLD", "NEW")
    graph = taskgraph.build_graph(t.commit())
    assert graph.prerequisites("NEW") == ("P0",)
    assert graph.prerequisites("D") == ("NEW",)


def test_a_superseded_task_is_a_tombstone(repo):
    t = txn(repo, document(section("OLD"), section("NEW")))
    t.supersede_task("OLD", "NEW")
    graph = taskgraph.build_graph(t.commit())
    assert graph.spec("OLD").is_superseded is True
    assert graph.spec("OLD").dispatchable is False
    assert graph.spec("OLD").legacy_fields["superseded_by"] == "NEW"
    assert "OLD" not in graph.ready()


def test_supersede_refuses_an_unknown_successor(repo):
    t = txn(repo, document(section("OLD")))
    with pytest.raises(taskgraph.MutationError):
        t.supersede_task("OLD", "NEW")


# --- invalidate ----------------------------------------------------------------------


def test_invalidate_inputs_sweeps_the_whole_downstream_cone(repo):
    t = txn(repo, document(
        section("A"), section("B", deps=["A"]), section("C", deps=["B"]), section("D"),
    ))
    assert t.invalidate_inputs("A") == ("A", "B", "C")
    graph = taskgraph.build_graph(t.commit())
    assert "inputs_invalidated_at" in graph.spec("C").legacy_fields
    assert "inputs_invalidated_at" not in graph.spec("D").legacy_fields


def test_invalidate_inputs_bumps_every_affected_revision(repo):
    t = txn(repo, document(section("A"), section("B", deps=["A"])))
    t.invalidate_inputs("A")
    assert t.specs["A"].spec_revision == 2
    assert t.specs["B"].spec_revision == 2


def test_invalidate_inputs_refuses_an_unknown_task(repo):
    t = txn(repo, document(section("A")))
    with pytest.raises(taskgraph.MutationError):
        t.invalidate_inputs("GHOST")


# ══════════════════════════════════════════════════════════════════════════
# compare-and-swap & document drift
# ══════════════════════════════════════════════════════════════════════════


def test_apply_records_the_document_as_the_applied_revision(repo):
    write_roadmap(repo, document(section("A")))
    result = taskgraph.apply(repo)
    record = taskgraph.read_cas(repo)
    assert record.document_hash == result.document.document_hash
    assert record.revision == 1
    assert set(record.tasks) == {"A"}


def test_a_second_apply_of_an_unchanged_document_advances_the_revision(repo):
    write_roadmap(repo, document(section("A")))
    taskgraph.apply(repo)
    assert taskgraph.apply(repo).record.revision == 2


def test_apply_refuses_a_stale_expected_hash(repo):
    write_roadmap(repo, document(section("A")))
    with pytest.raises(taskgraph.DocumentDriftError):
        taskgraph.apply(repo, expected_hash="0" * 64)


def test_apply_accepts_the_hash_it_was_handed(repo):
    doc = write_roadmap(repo, document(section("A")))
    assert taskgraph.apply(repo, expected_hash=doc.document_hash).record.revision == 1


def test_a_hand_edit_after_apply_is_a_drift_conflict(repo):
    write_roadmap(repo, document(section("A"), section("B", deps=["A"])))
    taskgraph.apply(repo)
    write_roadmap(repo, document(section("A", note="Dan edited this"), section("B", deps=["A"])))
    conflict = taskgraph.drift(repo)
    assert conflict is not None
    assert conflict.changed == ("A",)
    assert conflict.blocked == frozenset({"A", "B"})   # the edit, and everything downstream


def test_drift_blocks_only_the_affected_cone(repo):
    write_roadmap(repo, document(section("A"), section("B", deps=["A"]), section("C")))
    taskgraph.apply(repo)
    write_roadmap(repo, document(
        section("A", note="edited"), section("B", deps=["A"]), section("C"),
    ))
    assert "C" not in taskgraph.drift(repo).blocked


def test_a_new_hand_added_task_is_drift_too(repo):
    write_roadmap(repo, document(section("A")))
    taskgraph.apply(repo)
    write_roadmap(repo, document(section("A"), section("Z")))
    conflict = taskgraph.drift(repo)
    assert conflict.added == ("Z",)


def test_a_removed_task_is_drift_too(repo):
    write_roadmap(repo, document(section("A"), section("Z")))
    taskgraph.apply(repo)
    write_roadmap(repo, document(section("A")))
    assert taskgraph.drift(repo).removed == ("Z",)


def test_drift_never_rewrites_the_document(repo):
    write_roadmap(repo, document(section("A")))
    taskgraph.apply(repo)
    edited = document(section("A", note="Dan edited this"))
    write_roadmap(repo, edited)
    with pytest.raises(taskgraph.DocumentDriftError):
        taskgraph.apply(repo)
    assert taskgraph.roadmap_path(repo).read_text() == edited


def test_a_project_that_never_applied_has_no_drift(repo):
    write_roadmap(repo, document(section("A")))
    assert taskgraph.drift(repo) is None
    assert taskgraph.blocked_task_ids(repo) == frozenset()


def test_blocked_task_ids_never_raises(tmp_path):
    assert taskgraph.blocked_task_ids(tmp_path / "nowhere") == frozenset()


def test_an_apply_after_an_acknowledged_edit_adopts_it(repo):
    write_roadmap(repo, document(section("A")))
    taskgraph.apply(repo)
    doc = write_roadmap(repo, document(section("A", note="edited")))
    result = taskgraph.apply(repo, expected_hash=doc.document_hash)
    assert taskgraph.drift(repo) is None
    assert result.record.revision == 2


def test_apply_with_a_mutation_writes_the_new_document(repo):
    write_roadmap(repo, document(section("A")))
    taskgraph.apply(repo)
    result = taskgraph.apply(
        repo,
        lambda t: t.add_task(taskgraph.TaskSpec(task_id="B", depends_on=("A",))),
        expected_hash=taskgraph.load_document(taskgraph.roadmap_path(repo)).document_hash,
    )
    assert result.wrote_document is True
    assert "B" in taskgraph.load_document(taskgraph.roadmap_path(repo)).block_hashes()
    assert taskgraph.drift(repo) is None    # the write re-baselined the CAS record


def test_a_failing_mutation_leaves_the_file_untouched(repo):
    text = document(section("A"), section("B", deps=["A"]))
    write_roadmap(repo, text)
    taskgraph.apply(repo)
    with pytest.raises(taskgraph.RoadmapValidationError):
        taskgraph.apply(repo, lambda t: t.add_dependency("A", "B"))
    assert taskgraph.roadmap_path(repo).read_text() == text


# ══════════════════════════════════════════════════════════════════════════
# persistence into the control store
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def store(repo):
    store = ControlStore.open_controller(repo / ".orchestrator" / "control.sqlite3")
    yield store
    store.close()


def test_sync_registers_every_task_with_a_revision(repo, store):
    doc = write_roadmap(repo, document(section("A"), section("B", deps=["A"])))
    graph = taskgraph.build_graph(doc)
    assert taskgraph.sync_to_store(store, graph) == {"A": 1, "B": 1}
    assert store.task("B")["current_spec_revision"] == 1


def test_sync_writes_the_dependency_edges(repo, store):
    doc = write_roadmap(repo, document(section("A"), section("B", deps=["A"])))
    taskgraph.sync_to_store(store, taskgraph.build_graph(doc))
    rows = store.conn.execute(
        "SELECT task_id, depends_on_task_id FROM task_dependencies ORDER BY task_id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [("B", "A")]


def test_an_unchanged_spec_is_not_re_revisioned(repo, store):
    doc = write_roadmap(repo, document(section("A")))
    graph = taskgraph.build_graph(doc)
    taskgraph.sync_to_store(store, graph)
    assert taskgraph.sync_to_store(store, graph) == {"A": 1}


def test_a_changed_spec_advances_the_revision(repo, store):
    taskgraph.sync_to_store(store, taskgraph.build_graph(write_roadmap(repo, document(section("A")))))
    changed = write_roadmap(repo, document(section("A", title="Renamed")))
    assert taskgraph.sync_to_store(store, taskgraph.build_graph(changed)) == {"A": 2}
    row = store.conn.execute(
        "SELECT spec_yaml FROM task_revisions WHERE task_id = 'A' AND spec_revision = 2"
    ).fetchone()
    assert "Renamed" in row["spec_yaml"]


def test_a_dropped_dependency_is_removed_from_the_edge_table(repo, store):
    taskgraph.sync_to_store(store, taskgraph.build_graph(
        write_roadmap(repo, document(section("A"), section("B", deps=["A"])))))
    taskgraph.sync_to_store(store, taskgraph.build_graph(
        write_roadmap(repo, document(section("A"), section("B")))))
    assert store.conn.execute("SELECT COUNT(*) c FROM task_dependencies").fetchone()["c"] == 0


def test_a_client_store_may_not_sync(repo, store):
    from maestro.control.store import ControlStore as CS
    from maestro.control.store import SingleWriterViolation

    client = CS.open_client(repo / ".orchestrator" / "control.sqlite3")
    try:
        doc = write_roadmap(repo, document(section("A")))
        with pytest.raises(SingleWriterViolation):
            taskgraph.sync_to_store(client, taskgraph.build_graph(doc))
    finally:
        client.close()


# ══════════════════════════════════════════════════════════════════════════
# the CLI surface
# ══════════════════════════════════════════════════════════════════════════


def test_cli_validate_is_zero_on_a_valid_roadmap(repo, capsys):
    from maestro import cli

    write_roadmap(repo, document(section("A"), section("B", deps=["A"])))
    assert cli.cmd_roadmap(repo, "validate") == 0
    assert "OK — 2 tasks" in capsys.readouterr().out


def test_cli_validate_is_nonzero_on_a_cycle(repo, capsys):
    from maestro import cli

    write_roadmap(repo, document(section("A", deps=["B"]), section("B", deps=["A"])))
    assert cli.cmd_roadmap(repo, "validate") == 1
    assert "dependency cycle: A -> B -> A" in capsys.readouterr().err


def test_cli_validate_json_reports_every_issue(repo, capsys):
    from maestro import cli

    write_roadmap(repo, document(section("A", deps=["GHOST"])))
    cli.cmd_roadmap(repo, "validate", as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert [i["code"] for i in payload["issues"]] == ["missing_prerequisite"]


def test_cli_apply_records_and_persists(repo, capsys):
    from maestro import cli

    write_roadmap(repo, document(section("A")))
    ControlStore.open_controller(repo / ".orchestrator" / "control.sqlite3").close()
    assert cli.cmd_roadmap(repo, "apply") == 0
    out = capsys.readouterr().out
    assert "applied revision 1" in out
    assert "persisted 1 tasks" in out


def test_cli_apply_refuses_a_drifted_document(repo, capsys):
    from maestro import cli

    write_roadmap(repo, document(section("A")))
    cli.cmd_roadmap(repo, "apply")
    write_roadmap(repo, document(section("A", note="edited")))
    assert cli.cmd_roadmap(repo, "apply") == 3
    assert "drifted from the applied revision" in capsys.readouterr().err


def test_cli_apply_refuses_an_invalid_roadmap(repo, capsys):
    from maestro import cli

    write_roadmap(repo, document(section("A", deps=["GHOST"])))
    assert cli.cmd_roadmap(repo, "apply") == 1


def test_cli_parser_accepts_the_roadmap_verbs():
    from maestro import cli

    args = cli.build_parser().parse_args(["roadmap", "apply", "--expect-hash", "abc"])
    assert (args.command, args.verb, args.expect_hash) == ("roadmap", "apply", "abc")
