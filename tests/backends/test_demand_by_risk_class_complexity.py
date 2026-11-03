"""Dan, 2026-11-02: high risk means real outside cost. Below it, only class and complexity move
the demand. Low risk and a small file count never pick a weaker model."""
from types import SimpleNamespace

import pytest

from maestro import taskgraph
from maestro.backends import router as rt
from tests.backends.test_router import Policy, agent_node


def demand(**context):
    return rt.estimate_demand(agent_node(), rt.RoutingContext(**context), Policy())


def test_high_risk_demands_a_strong_model():
    assert demand(risk="high").min_strength == "strong"


def test_low_risk_does_not_lower_the_model():
    assert demand(risk="low").min_strength == "balanced"


@pytest.mark.parametrize("task_class", ["bug", "feature", "documentation", "manual_prep", "recovery"])
def test_an_ordinary_class_keeps_the_ordinary_demand(task_class):
    assert demand(task_class=task_class).min_strength == "balanced"


def test_difficult_debugging_demands_a_strong_model():
    picked = demand(task_class="difficult_debugging")
    assert picked.min_strength == "strong" and "difficult_debugging" in picked.reason


@pytest.mark.parametrize("complexity,expected", [
    (None, "balanced"), (1, "balanced"), (3, "balanced"), (4, "strong"), (5, "strong"),
])
def test_only_high_complexity_raises_the_demand(complexity, expected):
    assert demand(complexity=complexity).min_strength == expected


def test_a_role_that_declares_light_still_gets_light():
    light = rt.estimate_demand(agent_node(), rt.RoutingContext(), Policy(), role_strength="light")
    assert light.min_strength == "light"


def test_a_real_class_reaches_the_statistics_key():
    """The W5 gap: every route used to be recorded under `ordinary`."""
    assert rt.RoutingContext.from_manifest({"task_class": "bug"}).task_class == "bug"


@pytest.mark.parametrize("written,read", [
    ("low", "low"), ("high", "high"), ("medium", "low"), ("critical", "high"), (None, "low"), ("odd", "low"),
])
def test_a_roadmap_risk_reads_as_low_or_high(written, read):
    data = {"id": "T-1"} if written is None else {"id": "T-1", "risk": written}
    assert taskgraph.normalize_spec(data).risk == read


def test_the_risk_options_are_low_and_high():
    assert taskgraph.RISKS == ("low", "high")


def _free_quality_context(**fields):
    from maestro.workflows.policy import CONDITIONS, RiskContext

    context = RiskContext(task_id="T-1", objective="free_quality", **fields)
    return CONDITIONS["free_quality_nontrivial"](context)


def test_free_candidates_are_kept_for_ordinary_work_and_skipped_for_docs():
    """With `medium` gone, risk can no longer say what is trivial, so the class says it."""
    assert _free_quality_context() is True
    assert _free_quality_context(task_class="documentation") is False


@pytest.mark.parametrize("complexity", ["5", 4.5, True])
def test_a_malformed_complexity_never_raises_the_demand(complexity):
    assert demand(complexity=complexity).min_strength == "balanced"


@pytest.mark.parametrize("mode,reader,expected", [
    ("on", lambda _task: 4, 4), ("shadow", lambda _task: 4, 4), ("off", lambda _task: 4, None),
    ("on", lambda _task: None, None),
])
def test_the_manifest_gets_the_forecast_unless_runaway_detection_is_off(
        monkeypatch, mode, reader, expected):
    from maestro.workflows import routing as wf_routing

    seen = {}
    monkeypatch.setattr(wf_routing, "build_context_manifest",
                        lambda *_a, **fields: seen.update(fields) or "manifest")
    routing = SimpleNamespace(
        policy=SimpleNamespace(agent_definitions={"implementer": object()}),
        task_spec=lambda _task: object(), queries=None, runaway_mode=mode,
        runaway_control_reader=lambda: None, planner_complexity_reader=reader)
    runner = SimpleNamespace(revision=SimpleNamespace(task_id="T-1", code_base_sha="abc"))
    node = SimpleNamespace(node_id="n", agent_definition_ref="implementer")

    assert wf_routing.build_manifest(routing, runner, node) == "manifest"
    assert seen["complexity"] == expected


@pytest.mark.parametrize("written", ["risk: low\n", "risk: critical\n"])
def test_a_stored_spec_in_the_old_form_is_not_a_new_revision(written):
    """`to_yaml` omits `low`, not `medium`, so the old YAML of a task with `risk: low` differs
    in text while saying the same thing. Neither a re-apply nor a recovery may treat it as an edit."""
    current = taskgraph.normalize_spec({"id": "T-1", "risk": written.split()[1]}).to_yaml()
    old = current if "risk" in current else current + written
    if written == "risk: critical\n":
        old = current.replace("risk: high", "risk: critical")
    assert old != current
    assert taskgraph.same_spec_yaml(old, current)


def test_a_real_edit_is_still_a_new_revision():
    one = taskgraph.normalize_spec({"id": "T-1", "risk": "low"}).to_yaml()
    other = taskgraph.normalize_spec({"id": "T-1", "risk": "high"}).to_yaml()
    assert not taskgraph.same_spec_yaml(one, other)
    assert not taskgraph.same_spec_yaml(None, one)


@pytest.mark.parametrize("risk,valid", [
    ("low", True), ("high", True), ("medium", True), ("critical", True), ("sometimes", False),
])
def test_retired_risks_stay_readable_in_a_pending_draft(risk, valid):
    assert (risk in taskgraph.KNOWN_RISKS) is valid


def test_doctor_warns_about_a_missing_or_unknown_class_on_open_tasks(tmp_path):
    from maestro.cli import _check_task_labels

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "ROADMAP.md").write_text(
        "```yaml\nid: good\ntask_class: bug\n```\n"
        "```yaml\nid: bare\n```\n"
        "```yaml\nid: odd\ntask_class: mystery\nrisk: sometimes\n```\n"
        "```yaml\nid: finished\nstatus: complete\n```\n")
    check = _check_task_labels(tmp_path)
    assert check.ok is False and check.required is False
    assert "bare" in check.detail and "odd" in check.detail and "sometimes" in check.detail
    assert "good" not in check.detail and "finished" not in check.detail
