"""P13 W13: runaway detection, under the real runner.

An attempt that fails after spending more than `factor` x the p90 of its role, task class
and planner complexity band stops retrying and enters the P12B ladder at the diagnoser.
"""
from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import pytest

from maestro.backends import catalog as cat
from maestro.control.artifacts import ArtifactStore
from maestro.control.store import ControlStore
from maestro.taskgraph import normalize_spec
from maestro.workflows import failures as wf_failures
from maestro.workflows import runaway as wf_runaway
from maestro.workflows import runner as wf_runner
from maestro.workflows.handlers import AttemptInbox, TmuxDispatcher, attempts_root
from maestro.workflows.models import EdgeSpec, NodeResult, NodeSpec, WorkflowRevision

from .conftest import fixture_cli_entry, fixture_routing
from .test_dispatch_wiring import _FakeTmux, _task

TASK = "T-runaway"
BAND = 3


def _revision() -> WorkflowRevision:
    implement = NodeSpec(
        node_id="implement", kind="agent", handler_ref="maestro.handlers.noop",
        agent_definition_ref="implementer", effect_class="agent_editing_worktree",
    )
    diagnose = NodeSpec(
        node_id="diagnose", kind="agent", handler_ref="maestro.handlers.noop",
        agent_definition_ref="diagnoser", effect_class="pure_derivation",
    )
    return WorkflowRevision(
        workflow_id=f"wf_{TASK}", revision=1, task_id=TASK, task_spec_revision=1,
        compiler_version="test", policy_hash="", code_base_sha="deadbeef",
        execution_objective="paid_efficiency", nodes=(implement, diagnose),
        edges=(EdgeSpec("implement", "diagnose", guard_predicate="on_failure", required=False),),
        terminal_requirements=("implement",),
    )


def _tokens(total):
    return {"fresh_input": total, "cache_read": 0, "cache_write": 0, "output": 0,
            "cost_usd": None, "models": [], "source": "fixture"}


def _seed_history(store, run_id, *, samples, spend=1000, band=BAND, role="implementer",
                  task_class="feature"):
    """Earlier attempts in this band, each with a frozen cost of `spend` units."""
    for index in range(samples):
        binding = {
            "relative_cost": 1.0,
            wf_runaway.BINDING_KEY: {"role": role, "task_class": task_class, "complexity": band},
        }
        store.conn.execute(
            "INSERT INTO attempts (attempt_id, run_id, node_id, status, binding_json, usage_settle_json)"
            " VALUES (?, ?, ?, 'succeeded', ?, ?)",
            (f"seed-{band}-{role}-{index}", run_id, f"seed-{index}", json.dumps(binding),
             json.dumps({"token_split": _tokens(spend)})),
        )
    store.conn.commit()


def _reviewed_revision() -> WorkflowRevision:
    """Writer, then a reviewer whose rejection is a verdict on the writer's work."""
    implement = NodeSpec(
        node_id="implement", kind="agent", handler_ref="maestro.handlers.noop",
        agent_definition_ref="implementer", effect_class="agent_editing_worktree",
        resource_request={"writer_lease": True},
    )
    review = NodeSpec(
        node_id="review", kind="agent", handler_ref="maestro.handlers.noop",
        agent_definition_ref="reviewer", effect_class="pure_derivation",
    )
    diagnose = NodeSpec(
        node_id="diagnose", kind="agent", handler_ref="maestro.handlers.noop",
        agent_definition_ref="diagnoser", effect_class="pure_derivation",
    )
    return WorkflowRevision(
        workflow_id=f"wf_{TASK}", revision=1, task_id=TASK, task_spec_revision=1,
        compiler_version="test", policy_hash="", code_base_sha="deadbeef",
        execution_objective="paid_efficiency", nodes=(implement, review, diagnose),
        edges=(EdgeSpec("implement", "review", guard_predicate="on_success"),
               EdgeSpec("review", "diagnose", guard_predicate="on_failure", required=False),),
        terminal_requirements=("review",),
    )


def _gated_revision() -> WorkflowRevision:
    """The same shape with a verification gate in place of the reviewer."""
    revision = _reviewed_revision()
    gate = NodeSpec(node_id="review", kind="function", handler_ref="maestro.handlers.verification",
                    effect_class="repeatable_local_check")
    return dataclasses.replace(
        revision, nodes=(revision.node("implement"), gate, revision.node("diagnose")))


class _Lane:
    def __init__(self, tmp_path, *, mode="on", band=BAND, spend=2500, factor=2.0, control=None,
                 revision=None):
        self.repo = tmp_path / "repo"
        (self.repo / ".orchestrator").mkdir(parents=True)
        self.store = ControlStore.open_controller(self.repo / ".orchestrator" / "control.sqlite3")
        # A strong route, because a reviewer and a diagnoser refuse anything less.
        routing = fixture_routing(entries=[fixture_cli_entry(
            available_models=("fixture-strong",),
            model_profiles={"fixture-strong": cat.ModelProfile("fixture-strong")},
            pair_profiles=cat.migrate_pairs(("fixture-strong", "strong", 1.0)),
            pairs_migrated=True,
        )])
        routing.task_spec = lambda _id: normalize_spec(_task(TASK))
        routing.token_reader = lambda binding, workspace: _tokens(spend)
        routing.runaway_mode = mode
        routing.runaway_factor = factor
        routing.planner_complexity_reader = lambda _id: band
        routing.runaway_control_reader = lambda: control
        self.routing = routing
        self.runner = wf_runner.start_run(
            self.store, revision or _revision(), repo=self.repo,
            dispatcher=TmuxDispatcher(runner=_FakeTmux()),
            artifacts=ArtifactStore(self.repo / ".orchestrator" / "artifacts", self.store),
            routing=routing,
        )

    def seed(self, **kwargs):
        _seed_history(self.store, self.runner.run_id, **kwargs)

    def fail_first_attempt(self):
        self.runner.tick()
        self.answer("implement", "failed")

    def answer(self, node_id, status, outputs=None):
        """Settle the live attempt of `node_id` and let the runner decide."""
        row = self.attempts(node_id)[-1]
        inbox = AttemptInbox(attempts_root(self.repo), row["attempt_id"])
        result = NodeResult(run_id=row["run_id"], node_id=node_id,
                            attempt_id=row["attempt_id"], status=status, outputs=outputs or {})
        inbox.write_result(result)
        inbox.write_receipt(operation_id="", exit_status=result.exit_status)
        self.runner.tick()

    def latest_manifest(self):
        """The context manifest the runner stored for the newest dispatch."""
        rows = self.store.conn.execute(
            "SELECT payload FROM events WHERE type = 'ArtifactCommitted' ORDER BY sequence DESC"
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            if payload["type"] == "context_manifest":
                artifacts = ArtifactStore(self.repo / ".orchestrator" / "artifacts", self.store)
                return json.loads(artifacts.read(payload["artifact_refs"][0]))
        raise AssertionError("no manifest was stored")

    def attempts(self, node_id):
        return self.store.conn.execute(
            "SELECT * FROM attempts WHERE node_id = ? ORDER BY rowid", (node_id,)).fetchall()

    def phase(self, node_id):
        return self.runner.phases().get(node_id)

    def quality_failures(self):
        return self.store.conn.execute(
            "SELECT quality_failures FROM node_states WHERE node_id = 'implement'"
        ).fetchone()["quality_failures"]

    def triggers(self):
        return [json.loads(row["payload"]) for row in self.store.conn.execute(
            "SELECT payload FROM events WHERE type = ? ORDER BY sequence",
            (wf_runaway.TRIGGER_EVENT,)).fetchall()]


@pytest.fixture
def lane(tmp_path):
    made = []

    def make(**kwargs):
        made.append(_Lane(tmp_path, **kwargs))
        return made[-1]

    yield make
    for item in made:
        item.store.close()


def test_a_failed_attempt_past_the_threshold_stops_retrying_and_opens_the_diagnoser(lane):
    run = lane()
    run.seed(samples=10)
    run.fail_first_attempt()

    assert run.phase("implement") == "failed"
    # One final run after the repair, as on the ordinary ladder: no retries are left.
    assert run.quality_failures() == wf_failures.QUALITY_ATTEMPTS
    assert [a["node_id"] for a in run.attempts("implement")] == ["implement"]
    [trigger] = run.triggers()
    assert trigger["acted"] is True and trigger["mode"] == "on"
    assert trigger["spend"] == 2500 and trigger["p90"] == 1000 and trigger["threshold"] == 2000
    assert trigger["samples"] == 10 and trigger["complexity"] == BAND
    assert trigger["role"] == "implementer" and trigger["task_class"] == "feature"


def test_spend_at_or_below_the_threshold_retries_as_usual(lane):
    run = lane(spend=2000)
    run.seed(samples=10)
    run.fail_first_attempt()
    assert run.phase("implement") != "failed"
    assert run.quality_failures() == 1
    assert run.triggers() == []


def test_the_configured_factor_sets_the_threshold(lane):
    run = lane(spend=2500, factor=3.0)
    run.seed(samples=10)
    run.fail_first_attempt()
    assert run.triggers() == [] and run.quality_failures() == 1


def test_a_band_with_fewer_than_ten_samples_never_triggers(lane):
    run = lane(spend=10_000_000)
    run.seed(samples=9)
    run.fail_first_attempt()
    assert run.triggers() == [] and run.quality_failures() == 1
    assert run.phase("implement") != "failed"


@pytest.mark.parametrize("other", [
    dict(band=2), dict(role="reviewer"), dict(task_class="bugfix"),
])
def test_only_the_same_role_class_and_band_count_as_samples(lane, other):
    run = lane()
    run.seed(samples=10, **other)
    run.fail_first_attempt()
    assert run.triggers() == [] and run.quality_failures() == 1


def test_a_task_with_no_forecast_never_triggers(lane):
    run = lane(band=None)
    run.seed(samples=10)
    run.fail_first_attempt()
    assert run.triggers() == [] and run.quality_failures() == 1


def test_shadow_records_the_trigger_and_retries_as_usual(lane):
    run = lane(mode="shadow")
    run.seed(samples=10)
    run.fail_first_attempt()
    [trigger] = run.triggers()
    assert trigger["acted"] is False and trigger["mode"] == "shadow"
    assert run.quality_failures() == 1
    assert run.phase("implement") != "failed"


def test_off_neither_stamps_the_band_nor_records_a_trigger(lane):
    run = lane(mode="off")
    run.seed(samples=10)
    run.fail_first_attempt()
    assert run.triggers() == [] and run.quality_failures() == 1
    binding = json.loads(run.attempts("implement")[0]["binding_json"])
    assert wf_runaway.BINDING_KEY not in binding


def test_the_band_is_frozen_on_the_attempt_under_the_default_objective(lane):
    """`paid_efficiency` records no pace decision, so the stamp is W13's own."""
    run = lane()
    run.runner.tick()
    binding = json.loads(run.attempts("implement")[0]["binding_json"])
    assert binding[wf_runaway.BINDING_KEY] == {
        "role": "implementer", "task_class": "feature", "complexity": BAND}


def test_the_operators_control_overrides_the_configured_mode(lane):
    run = lane(mode="on", control="off")
    run.seed(samples=10)
    run.fail_first_attempt()
    assert run.triggers() == [] and run.quality_failures() == 1


def test_a_broken_control_reader_keeps_the_configured_mode(lane):
    run = lane(mode="shadow")

    def broken():
        raise OSError("state unreadable")

    run.routing.runaway_control_reader = broken
    run.seed(samples=10)
    run.fail_first_attempt()
    assert [t["mode"] for t in run.triggers()] == ["shadow"]


def test_assessment_failure_leaves_the_ordinary_ladder(lane, monkeypatch):
    run = lane()
    run.seed(samples=10)

    def broken(*args, **kwargs):
        raise ValueError("bad history")

    monkeypatch.setattr(wf_runaway, "assess", broken)
    run.fail_first_attempt()
    assert run.quality_failures() == 1 and run.phase("implement") != "failed"


# --- the pure rule ------------------------------------------------------------------

def _row(attempt_id, spend, *, cost=1.0, stamp=None, tokens=True):
    stamp = stamp or {"role": "implementer", "task_class": "feature", "complexity": BAND}
    return {
        "attempt_id": attempt_id,
        "binding_json": json.dumps({"relative_cost": cost, wf_runaway.BINDING_KEY: stamp}),
        "usage_settle_json": json.dumps({"token_split": _tokens(spend)} if tokens else {}),
    }


def test_p90_is_nearest_rank_and_excludes_the_attempt_itself():
    rows = [_row(f"a{i}", (i + 1) * 100) for i in range(10)] + [_row("me", 10**9)]
    found = wf_runaway.assess(rows, attempt=rows[-1], factor=2.0)
    assert found["p90"] == 900 and found["samples"] == 10 and found["threshold"] == 1800


def test_spend_is_relative_cost_times_tokens():
    rows = [_row(f"a{i}", 100, cost=2.0) for i in range(10)] + [_row("me", 500, cost=2.0)]
    found = wf_runaway.assess(rows, attempt=rows[-1], factor=2.0)
    assert found["spend"] == 1000 and found["p90"] == 200


def test_an_attempt_with_no_measurable_spend_is_not_assessed():
    rows = [_row(f"a{i}", 100) for i in range(10)] + [_row("me", 0, tokens=False)]
    assert wf_runaway.assess(rows, attempt=rows[-1], factor=2.0) is None


def test_samples_without_a_measurable_spend_do_not_count():
    rows = [_row(f"a{i}", 100) for i in range(9)] + [_row("x", 0, tokens=False)]
    rows.append(_row("me", 10**6))
    assert wf_runaway.assess(rows, attempt=rows[-1], factor=2.0) is None


@pytest.mark.parametrize("value, band", [
    (3, 3), (1, 1), (5, 5), (0, None), (6, None), (True, None), (3.0, None), ("3", None), (None, None),
])
def test_only_an_integer_one_to_five_is_a_band(value, band):
    assert wf_runaway.valid_band(value) == band


# --- the planner's forecast ----------------------------------------------------------

@pytest.mark.parametrize("mode, present", [("on", True), ("shadow", True), ("off", False)])
def test_the_forecast_contract_reaches_only_a_planner_while_detection_is_on(lane, mode, present):
    run = lane(mode=mode)
    planner = SimpleNamespace(agent_definition_ref="planner")
    implementer = SimpleNamespace(agent_definition_ref="implementer")
    contract = wf_runaway.forecast_contract(run.routing, planner)
    assert bool(contract) is present
    if present:
        assert wf_runaway.FORECAST_FILE in contract and "complexity" in contract
    assert not wf_runaway.forecast_contract(run.routing, implementer)


def _planner_revision() -> WorkflowRevision:
    planner = NodeSpec(
        node_id="plan", kind="agent", handler_ref="maestro.handlers.noop",
        agent_definition_ref="planner", effect_class="pure_derivation",
    )
    return WorkflowRevision(
        workflow_id=f"wf_{TASK}", revision=1, task_id=TASK, task_spec_revision=1,
        compiler_version="test", policy_hash="", code_base_sha="deadbeef",
        execution_objective="paid_efficiency", nodes=(planner,), edges=(),
        terminal_requirements=("plan",),
    )


@pytest.mark.parametrize("mode, control, present", [
    ("on", None, True), ("shadow", None, True), ("off", None, False), ("on", "off", False),
])
def test_a_real_planner_dispatch_carries_the_forecast_only_while_detection_is_on(
        lane, mode, control, present):
    from maestro.workflows.worker import agent_brief

    run = lane(mode=mode, control=control, revision=_planner_revision())
    run.runner.tick()
    manifest = run.latest_manifest()
    assert ("forecast_contract" in manifest) is present
    brief = agent_brief(manifest, role="planner")
    assert (wf_runaway.FORECAST_FILE in brief) is present


@pytest.mark.parametrize("mode, recorded", [("on", 4), ("off", None)])
def test_a_planner_result_records_its_forecast_only_while_detection_is_on(lane, mode, recorded):
    run = lane(mode=mode, revision=_planner_revision())
    run.runner.tick()
    run.answer("plan", "succeeded", {"complexity_forecast": 4})
    assert wf_runaway.latest_forecast(run.store, TASK) == recorded


@pytest.mark.parametrize("text, band", [
    ('{"complexity": 4}', 4), ('{"complexity": 9}', None), ('not json', None), (None, None),
])
def test_the_planners_forecast_file_is_adopted_only_when_valid(tmp_path, text, band):
    from maestro.workflows.handlers import _adopt_forecast

    if text is not None:
        (tmp_path / wf_runaway.FORECAST_FILE).write_text(text)
    result = NodeResult(run_id="r", node_id="plan", attempt_id="a", status="succeeded")
    adopted = _adopt_forecast(SimpleNamespace(workspace=tmp_path), result)
    assert (adopted.outputs or {}).get("complexity_forecast") == band


def test_the_controllers_forecast_prefers_a_planner_over_the_roadmap(tmp_path, monkeypatch):
    from maestro import orchestrator, state

    store = ControlStore.open_controller(tmp_path / "control.sqlite3")
    try:
        monkeypatch.setattr(state, "control_store", lambda: store)
        monkeypatch.setattr(orchestrator, "_graph_task_overrides", lambda _id: {"complexity": 2})
        assert orchestrator._graph_planner_complexity(TASK) == 2
        wf_runaway.record_forecast(store, TASK, 4, run_id="r", attempt_id="a",
                                   at="2026-11-02T00:00:00Z")
        assert orchestrator._graph_planner_complexity(TASK) == 4
    finally:
        store.close()


def test_the_real_diagnoser_dispatch_carries_the_runaway_contract(lane):
    run = lane()
    run.seed(samples=10)
    run.fail_first_attempt()
    assert run.phase("diagnose") == "claimed"
    assert wf_runaway.FALSE_ALARM in run.latest_manifest()["runaway_contract"]


def test_a_trigger_keeps_the_retries_when_no_failure_edge_reaches_a_diagnoser(lane):
    revision = dataclasses.replace(_revision(), edges=())
    run = lane(revision=revision)
    run.seed(samples=10)
    run.fail_first_attempt()
    [trigger] = run.triggers()
    assert trigger["acted"] is False and trigger["mode"] == "on"
    assert run.quality_failures() == 1 and run.phase("implement") != "failed"


def test_a_trigger_keeps_the_retries_when_the_repair_budget_is_spent(lane):
    run = lane()
    run.routing.max_plan_repairs = 0
    run.seed(samples=10)
    run.fail_first_attempt()
    [trigger] = run.triggers()
    assert trigger["acted"] is False
    assert run.quality_failures() == 1 and run.phase("implement") != "failed"


def test_a_crashed_gate_says_nothing_about_the_writers_spend(lane):
    run = lane(revision=_gated_revision())
    run.seed(samples=10)
    run.runner.tick()
    run.answer("implement", "succeeded")
    run.answer("review", "failed", {
        "failure": {"kind": "worker_crashed", "evidence": "the host was killed"}})
    assert run.triggers() == []
    assert run.quality_failures() == 1


def test_the_planner_brief_is_unchanged_when_detection_is_off():
    from maestro.workflows.worker import agent_brief

    plain = agent_brief({"acceptance_criteria": ["x"]}, role="planner")
    assert "omplexity" not in plain and "forecast" not in plain.lower()
    assert "FORECAST" in agent_brief(
        {"acceptance_criteria": ["x"], "forecast_contract": "FORECAST"}, role="planner")


def test_a_planner_forecast_is_read_back_and_wins_over_the_roadmap(tmp_path):
    store = ControlStore.open_controller(tmp_path / "control.sqlite3")
    try:
        assert wf_runaway.latest_forecast(store, TASK) is None
        wf_runaway.record_forecast(store, TASK, 4, run_id="r1", attempt_id="a1", at="2026-11-02T00:00:00Z")
        wf_runaway.record_forecast(store, "other", 1, run_id="r2", attempt_id="a2", at="2026-11-02T00:00:00Z")
        assert wf_runaway.latest_forecast(store, TASK) == 4
    finally:
        store.close()


# --- the false alarm -----------------------------------------------------------------

def test_the_false_alarm_verdict_needs_a_real_trigger(lane):
    from maestro.workflows.compiler import PlanRepairProposal

    run = lane()
    node = run.runner.revision.node("diagnose")
    proposal = PlanRepairProposal.from_dict({
        "workflow_id": f"wf_{TASK}", "base_revision": 1, "reason": "nothing wrong",
        "verdict": "runaway_false_alarm", "rerun_nodes": ["implement"]})
    assert "runaway" in run.runner._verdict_refusal(node, proposal)

    run.seed(samples=10)
    run.fail_first_attempt()
    assert run.runner._verdict_refusal(node, proposal) is None


def test_the_false_alarm_defaults_to_re_running_the_failed_writer(lane):
    run = lane()
    run.seed(samples=10)
    run.fail_first_attempt()
    completed = run.runner._completed_diagnosis(
        {"verdict": "runaway_false_alarm", "reason": "large but legitimate"})
    assert completed["rerun_nodes"] == ["implement"]


def test_a_review_rejection_is_assessed_on_the_writers_spend(lane):
    """The rework path: a reviewer's verdict stops the writer's retries, not the reviewer's."""
    run = lane(revision=_reviewed_revision())
    run.seed(samples=10)
    run.runner.tick()
    run.answer("implement", "succeeded")
    run.answer("review", "failed", {
        "failure": {"kind": "verification_failed", "evidence": "review rejected: wrong"}})

    [trigger] = run.triggers()
    assert trigger["node_id"] == "implement" and trigger["role"] == "implementer"
    assert trigger["acted"] is True
    assert run.quality_failures() == wf_failures.QUALITY_ATTEMPTS
    assert [a["node_id"] for a in run.attempts("implement")] == ["implement"]
    assert run.phase("review") == "failed"


def test_a_failed_gate_is_assessed_on_the_writers_spend(lane):
    """A gate costs nothing itself: its failure stops the writer it judged."""
    run = lane(revision=_gated_revision())
    run.seed(samples=10)
    run.runner.tick()
    run.answer("implement", "succeeded")
    run.answer("review", "failed", {
        "failure": {"kind": "verification_failed", "evidence": "the check failed"}})

    [trigger] = run.triggers()
    assert trigger["node_id"] == "implement" and trigger["spend"] == 2500
    assert run.quality_failures() == wf_failures.QUALITY_ATTEMPTS
    assert [a["node_id"] for a in run.attempts("implement")] == ["implement"]


def test_an_expensive_success_is_left_alone(lane):
    run = lane()
    run.seed(samples=10)
    run.runner.tick()
    run.answer("implement", "succeeded")
    assert run.triggers() == [] and run.quality_failures() == 0


def test_a_false_alarm_is_accepted_by_plan_repair_and_reruns_with_no_retries_left(lane):
    from maestro.workflows.compiler import latest_revision

    run = lane()
    run.seed(samples=10)
    run.fail_first_attempt()
    assert run.phase("implement") == "failed"
    run.runner.tick()

    run.answer("diagnose", "succeeded", {"plan_repair": {
        "verdict": "runaway_false_alarm", "reason": "large but legitimate"}})

    assert latest_revision(run.store, f"wf_{TASK}").revision == 2, "plan_repair accepted the rerun"
    runs = run.store.conn.execute(
        "SELECT run_id, status, workflow_revision FROM task_runs ORDER BY workflow_revision"
    ).fetchall()
    assert [(r["status"], r["workflow_revision"]) for r in runs][0] == ("canceled", 1)
    assert runs[-1]["workflow_revision"] == 2
    # The repaired run has no retries left: this is the one final run, as on the ladder.
    state = run.store.conn.execute(
        "SELECT quality_failures FROM node_states WHERE run_id = ? AND node_id = 'implement'",
        (runs[-1]["run_id"],)).fetchone()
    assert state["quality_failures"] == wf_failures.QUALITY_ATTEMPTS


def test_a_false_alarm_is_charged_to_maestro():
    from maestro.workflows.telemetry_observation import cause_observation

    assert cause_observation("runaway_false_alarm", role="diagnoser")["fault"] == "maestro"


def test_the_diagnoser_is_told_it_was_opened_early(lane):
    run = lane()
    assert run.runner._runaway_contract() is None
    run.seed(samples=10)
    run.fail_first_attempt()
    text = run.runner._runaway_contract()
    assert "runaway_false_alarm" in text and "2500" in text and "2000" in text
