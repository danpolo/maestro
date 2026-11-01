"""P12A: the built graph components, proven under the real `orchestrator.main`.

Every test here drives the event loop through `run_lane` rather than calling a component
directly — the phase's definition of "wired" (P12A §1) is that one poll of the loop calls
the component and that its result changes what was dispatched.

W0 — runner selection and the graph poll loop:
`project.yaml`'s `engineering.runner` picks exactly one controller loop per process (D08).
`legacy`, the default, never touches `WorkflowRunner`; `graph` never touches the legacy
launch path. A switch while the other loop still owns live work is refused with a reason.

W1/W2 — binding resolution at dispatch, fed by the stored ContextManifest:
an agent node is resolved exactly once per attempt, its binding decides the worker argv and
claim deadline, a `wait` leaves it unclaimed, a `block` or a stale snapshot fails it.

W4 — candidate reap: the join is reached however a candidate ended, reads one document per
candidate with a committed result, and reports a candidate without one as `abandoned`.

W5/W6 — affected checks and persisted shadow comparisons: a verification node is handed the
selection the controller computed at claim; in shadow mode that is the full suite, and the
controller stores one comparison per verification result. `authoritative` is accepted only
when the stored comparisons qualify.

W7 — plan repair in the loop: a planner or diagnoser result carrying a `PlanRepairProposal`
moves the loop to revision N + 1 under a new run id with its counters carried; a proposal
past `engineering.max_plan_repairs` is refused and fails the node.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shlex
import sys
from pathlib import Path

import pytest

import maestro.state
from maestro import bootstrap, config, orchestrator, scheduling
from maestro.backends import catalog as cat
from maestro.backends import registry, router
from maestro.backends.base import CompletionSpec
from maestro.backends.model_api import ModelTurn
from maestro.control.artifacts import ArtifactStore
from maestro.control.resources import COUNTED_POOL, HostReservationLedger
from maestro.control.store import ControlStore
from maestro.repository.context import ContextManifest
from maestro.repository.index import StaleSnapshotError
from maestro.taskgraph import normalize_spec
from maestro.workflows import failures as wf_failures
from maestro.workflows import handlers
from maestro.workflows import routing as wf_routing
from maestro.workflows import runner as wf_runner
from maestro.workflows import worker as wf_worker
from maestro.workflows.compiler import compile_workflow
from maestro.workflows.policy import default_policy

from .conftest import FixtureQueries, LaneScenario, fixture_cli_entry, fixture_routing

GRAPH = {"engineering": {"runner": "graph"}}


def _task(task_id: str, **overrides) -> dict:
    task = {"id": task_id, "title": f"{task_id} title", "status": "pending",
            "mode": "autonomous", "deps": [],
            "verifications": [{"id": "V-fixture", "kind": "auto", "cmd": "true"}]}
    task.update(overrides)
    return task


def _halt(run) -> None:
    run.halt()


def _create_control_db(run) -> None:
    ControlStore.open_controller(maestro.state.CONTROL_DB).close()


def _journal_details(event: str) -> list[str]:
    path = maestro.state.JOURNAL
    if not path.exists():
        return []
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [record["detail"] for record in records if record.get("event") == event]


def _task_runs() -> list:
    store = maestro.state.control_store()
    return store.conn.execute(
        "SELECT run_id, task_id, status FROM task_runs ORDER BY started_at, run_id"
    ).fetchall()


@pytest.fixture(autouse=True)
def _fresh_control_store():
    """`maestro.state` caches one controller store per process; each test is its own."""
    maestro.state._reset_control_store()
    yield
    maestro.state._reset_control_store()


@pytest.fixture
def spies(monkeypatch):
    """Record every `start_run` and `WorkflowRunner.tick`, calling through to the real ones."""
    calls: dict = {"start_run": [], "tick": []}
    real_start_run, real_tick = wf_runner.start_run, wf_runner.WorkflowRunner.tick

    def start_run(*args, **kwargs):
        opened = real_start_run(*args, **kwargs)
        calls["start_run"].append(opened.run_id)
        return opened

    def tick(self, *args, **kwargs):
        calls["tick"].append(self.run_id)
        return real_tick(self, *args, **kwargs)

    monkeypatch.setattr(wf_runner, "start_run", start_run)
    monkeypatch.setattr(wf_runner.WorkflowRunner, "tick", tick)
    return calls


def _graph_scenario(lane: str, **overrides) -> LaneScenario:
    fields = dict(lane=lane, tasks=[_task("TASK-001")], project_yaml=GRAPH,
                  before_run=_create_control_db, on_poll=_halt)
    fields.update(overrides)
    return LaneScenario(**fields)


# ── W0: the selector ─────────────────────────────────────────────────────────────────


def test_runner_selector_defaults_to_legacy_and_refuses_what_it_cannot_read():
    assert config.engineering_runner({}) == "legacy"
    assert config.engineering_runner({"engineering": None}) == "legacy"
    assert config.engineering_runner({"engineering": {}}) == "legacy"
    assert config.engineering_runner({"engineering": {"runner": "legacy"}}) == "legacy"
    assert config.engineering_runner({"engineering": {"runner": "graph"}}) == "graph"
    with pytest.raises(config.ConfigError, match=r"'grpah' is not one of legacy, graph"):
        config.engineering_runner({"engineering": {"runner": "grpah"}})
    with pytest.raises(config.ConfigError, match="must be a mapping"):
        config.engineering_runner({"engineering": "graph"})


def test_affected_checks_mode_defaults_to_shadow_and_refuses_what_it_cannot_read():
    assert config.affected_checks_mode({}) == "shadow"
    assert config.affected_checks_mode({"engineering": None}) == "shadow"
    assert config.affected_checks_mode({"engineering": {"runner": "graph"}}) == "shadow"
    assert config.affected_checks_mode(
        {"engineering": {"affected_checks": "shadow"}}) == "shadow"
    assert config.affected_checks_mode(
        {"engineering": {"affected_checks": "authoritative"}}) == "authoritative"
    with pytest.raises(config.ConfigError, match=r"'optimistic' is not one of shadow, authoritative"):
        config.affected_checks_mode({"engineering": {"affected_checks": "optimistic"}})
    with pytest.raises(config.ConfigError, match="must be a mapping"):
        config.affected_checks_mode({"engineering": "authoritative"})
    assert wf_routing.DispatchRouting(
        catalog=None, policy=None, queries=None, task_spec=None
    ).affected_mode == "shadow"


def test_an_unknown_runner_value_stops_the_loop_before_any_work(run_lane, spies):
    run = run_lane(_graph_scenario("w0_bad_selector",
                                   project_yaml={"engineering": {"runner": "grpah"}}))

    assert run.returncode == 1
    assert _journal_details("runner_config_error")
    assert run.launched == [] and spies == {"start_run": [], "tick": []}


def test_a_shared_policy_newer_than_this_reader_does_not_stop_the_loop(
        run_lane, spies, tmp_path, monkeypatch):
    """P13 W8: a publication that ran before this project's release moved (W1 slice 3)."""
    from maestro.model_policy import POLICY_SCHEMAS, atomic_write, canonical, digest, shipped_policy

    home = tmp_path / "models-home"
    newer = dict(shipped_policy(), schema=max(POLICY_SCHEMAS) + 1)
    atomic_write(home / "revisions" / (digest(newer) + ".json"), canonical(newer))
    atomic_write(home / "current", (digest(newer) + "\n").encode())
    monkeypatch.setenv("MAESTRO_MODELS_HOME", str(home))
    monkeypatch.setattr(orchestrator, "_unreadable_policies_journaled", set())

    run = run_lane(_graph_scenario("w8_newer_policy"))

    assert run.returncode == 0
    assert _journal_details("runner_config_error") == []
    assert [(row["task_id"], row["status"]) for row in _task_runs()] == [("TASK-001", "running")]
    assert len(spies["start_run"]) == 1 and spies["tick"] == spies["start_run"]
    deferred = _journal_details("model_policy_unreadable")
    assert len(deferred) == 1 and digest(newer) in deferred[0]


# ── W0: one poll of each loop ────────────────────────────────────────────────────────


def test_legacy_poll_never_touches_the_graph_runner(run_lane, spies):
    run = run_lane(LaneScenario(lane="w0_legacy", tasks=[_task("TASK-001")], on_poll=_halt))

    assert run.launched, "the legacy loop launched its implementer as it always has"
    assert spies == {"start_run": [], "tick": []}
    assert not maestro.state.CONTROL_DB.exists()


def test_graph_poll_opens_a_run_and_dispatches_its_first_node(run_lane, spies):
    run = run_lane(_graph_scenario("w0_graph"))

    assert run.returncode == 0
    rows = _task_runs()
    assert [(row["task_id"], row["status"]) for row in rows] == [("TASK-001", "running")]
    run_id = rows[0]["run_id"]
    assert spies["start_run"] == [run_id]
    assert spies["tick"] == [run_id]

    store = maestro.state.control_store()
    started = store.conn.execute(
        "SELECT node_id FROM node_states WHERE run_id = ? AND phase != 'pending'", (run_id,)
    ).fetchall()
    assert len(started) == 1
    first_node = started[0]["node_id"]
    windows = [command for command in run.commands if command.startswith("tmux new-window")]
    assert len(windows) == 1
    assert f"-n {handlers.window_name('TASK-001', first_node)} " in windows[0]
    assert "maestro.workflows.worker" in windows[0]

    assert run.launched == [], "launch_implementer was never called"
    assert run.trace.of_kind("async_job_launch") == [], "launch_async_job was never called"
    assert _journal_details("graph_run_opened") == [
        f"TASK-001 run={run_id} workflow=wf_TASK-001@1"
    ]


def test_restart_resumes_the_open_run_instead_of_opening_another(run_lane, spies):
    run = run_lane(_graph_scenario("w0_restart"))
    [run_id] = spies["start_run"]

    run.orchestrator.HALT_FILE.unlink()
    maestro.state._reset_control_store()  # a new controller process holds no cached store
    run.run()

    assert run.returncode == 0
    assert [row["run_id"] for row in _task_runs()] == [run_id]
    assert spies["start_run"] == [run_id], "no second run was opened"
    assert spies["tick"] == [run_id, run_id]
    assert _journal_details("graph_run_resumed") == [f"TASK-001 run={run_id}"]


# ── W0: refused switches ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("legacy_state", [
    {"in_flight": [{"task_id": "TASK-001", "session_id": "s-1"}]},
    {"waiting_on_dan": {"1": {"task_id": "TASK-001", "kind": "awaiting-verification"}}},
], ids=["in_flight", "awaiting_verification"])
def test_switching_to_graph_is_refused_while_legacy_work_is_active(run_lane, spies, legacy_state):
    run = run_lane(_graph_scenario("w0_refuse_graph", state=legacy_state, before_run=None))

    assert run.returncode == 1
    [reason] = _journal_details("runner_switch_refused")
    assert reason.startswith(
        "engineering.runner=graph refused: the legacy runner still has active work"
    )
    assert "TASK-001" in reason
    assert spies == {"start_run": [], "tick": []}
    assert run.launched == []
    assert not maestro.state.CONTROL_DB.exists(), "a refused switch migrates nothing"


def test_graph_mode_creates_the_control_database_it_needs(run_lane, spies):
    run = run_lane(_graph_scenario("w0_creates_control_db", before_run=None))

    assert run.returncode == 0
    assert maestro.state.CONTROL_DB.exists()
    assert _journal_details("control_db_created") == [
        f"{maestro.state.CONTROL_DB} (engineering.runner=graph)"
    ]
    assert [row["task_id"] for row in _task_runs()] == ["TASK-001"]
    assert len(spies["start_run"]) == 1
    assert run.launched == []


def test_switching_back_to_legacy_is_refused_while_a_graph_run_is_open(run_lane):
    opened: list = []

    def open_graph_run(run):
        _create_control_db(run)
        store = maestro.state.control_store()
        revision = compile_workflow(normalize_spec(_task("TASK-001")), workflow_id="wf_TASK-001")
        graph_runner = wf_runner.start_run(store, revision, repo=run.repo)
        opened.append(graph_runner.run_id)

    run = run_lane(LaneScenario(lane="w0_refuse_legacy", tasks=[_task("TASK-001")],
                                before_run=open_graph_run, on_poll=_halt))

    assert run.returncode == 1
    [reason] = _journal_details("runner_switch_refused")
    assert reason.startswith(
        f"engineering.runner=legacy refused: graph runs are still open (TASK-001:{opened[0]}(running))"
    )
    assert run.launched == []


# ── P12A S2 step A: W1 binding at dispatch, W2 manifest-fed routing ──────────────────

FREE_ENDPOINT = {
    "endpoint_id": "free_api", "base_url": "https://api.example.test/v1", "model": "free-1",
    "api_key_env": "MAESTRO_FREE_KEY", "verified_version": "v1",
}


@pytest.fixture
def resolutions(monkeypatch):
    """Every `router.resolve_agent` call a poll makes, calling through to the real one."""
    calls: list = []
    real = router.resolve_agent

    def resolve_agent(node, context_manifest, *args, **kwargs):
        result = real(node, context_manifest, *args, **kwargs)
        calls.append({"node_id": node.node_id, "manifest": context_manifest, "result": result})
        return result

    monkeypatch.setattr(router, "resolve_agent", resolve_agent)
    return calls


def _windows(run) -> list:
    return [command for command in run.commands if command.startswith("tmux new-window")]


def _worker_argv(command: str) -> list:
    """The argv `worker.main` receives, recovered from the recorded tmux command."""
    parts = shlex.split(command)
    return parts[parts.index("maestro.workflows.worker") + 1:]


def _attempts() -> list:
    return maestro.state.control_store().conn.execute(
        "SELECT attempt_id, node_id, status FROM attempts ORDER BY attempt_id"
    ).fetchall()


def _node_phase(node_id: str) -> str | None:
    row = maestro.state.control_store().conn.execute(
        "SELECT phase FROM node_states WHERE node_id = ?", (node_id,)
    ).fetchone()
    return row["phase"] if row else None


def _events(event_type: str) -> list:
    return maestro.state.control_store().conn.execute(
        "SELECT aggregate_id, attempt_id, payload FROM events WHERE type = ? ORDER BY rowid",
        (event_type,),
    ).fetchall()


def test_a_graph_poll_resolves_each_agent_attempt_exactly_once(run_lane, resolutions):
    run = run_lane(_graph_scenario("w1_resolve_once"))

    assert run.returncode == 0
    assert [(a["node_id"], a["status"]) for a in _attempts()] == [("implementer", "claimed")]
    assert [call["node_id"] for call in resolutions] == ["implementer"]
    assert resolutions[0]["result"].is_bound
    assert len(_windows(run)) == 1


def test_the_worker_argv_carries_the_dispatch_plan_of_the_resolved_binding(run_lane, resolutions):
    run = run_lane(_graph_scenario("w1_argv"))

    [call] = resolutions
    binding = call["result"]
    plan = router.dispatch_plan(binding)
    assert plan.effort_args == ("--effort", "medium"), "the fixture backend delivers effort by flag"
    [window] = _windows(run)
    argv = _worker_argv(window)
    tail = argv[argv.index("--revision") + 2:]
    [attempt] = _attempts()
    manifest_id = tail[tail.index("--manifest-artifact-id") + 1]
    assert tail == [
        "--backend-id", binding.backend_id,
        "--runtime-kind", "agent_cli",
        "--model", plan.model,
        *[f"--effort-arg={arg}" for arg in plan.effort_args],
        *[f"--effort-config={key}={value}" for key, value in plan.effort_config],
        "--manifest-artifact-id", manifest_id,
        "--session-id", attempt["attempt_id"],
        "--session-mode", "fresh",
    ]
    parsed = wf_worker.parse_args(argv)
    assert parsed.effort_arg == list(plan.effort_args)
    assert parsed.model == "fixture-1" and parsed.backend_id == "fixture_cli"


def test_a_free_tier_binding_runs_the_model_agent_runtime_in_the_worker(
    run_lane, resolutions, monkeypatch
):
    free_policy = dataclasses.replace(default_policy(), objective="free_quality")
    [entry] = wf_routing.endpoints_for_catalog([FREE_ENDPOINT])
    run = run_lane(_graph_scenario(
        "w1_free_tier",
        project_yaml={"engineering": {"runner": "graph", "free_tier_endpoints": [FREE_ENDPOINT]}},
        routing=lambda: fixture_routing(entries=[entry], policy=free_policy),
    ))

    [call] = resolutions
    assert call["result"].billing_category == "free_tier_api"
    [window] = _windows(run)
    argv = _worker_argv(window)
    assert argv[argv.index("--runtime-kind") + 1] == "model_endpoint_harness"

    seen: dict = {"endpoints": [], "briefs": []}

    class FakeModelAPI:
        def complete(self, messages, *, tools=(), max_output_tokens=None):
            seen["briefs"].append(messages[-1].content)
            return ModelTurn(text="implemented", finish_reason="stop")

    def model_api(endpoint):
        seen["endpoints"].append(endpoint)
        return FakeModelAPI()

    monkeypatch.setattr(wf_worker, "_model_api", model_api)
    assert wf_worker.main(argv) == 0

    attempt_id = argv[argv.index("--attempt-id") + 1]
    inbox = handlers.AttemptInbox(handlers.attempts_root(run.repo), attempt_id)
    result = inbox.result()
    assert result is not None and result.status == "succeeded"
    assert result.outputs["runtime"]["stop_reason"] == "completed"
    assert [endpoint.endpoint_id for endpoint in seen["endpoints"]] == ["free_api"]
    stored = json.loads(ArtifactStore(run.repo / ".orchestrator" / "artifacts",
                                      maestro.state.control_store()).read(
        argv[argv.index("--manifest-artifact-id") + 1]))
    [brief] = seen["briefs"]
    header, _, body = brief.partition("\n\n")
    assert header == "You are the implementer for task TASK-001: TASK-001 title."
    assert body == wf_worker.agent_brief(stored)
    assert seen["briefs"][0]


def test_a_saturated_pool_waits_without_claiming_and_records_the_reason(run_lane, resolutions):
    resources = cat.ResourcePoolState()
    resources.pool("fixture-pool", concurrency_limit=1).reserve()
    run = run_lane(_graph_scenario("w1_wait", routing=lambda: fixture_routing(resources=resources)))

    assert run.returncode == 0
    assert [call["result"].disposition for call in resolutions] == ["wait"]
    assert _attempts() == [] and _windows(run) == []
    assert _node_phase("implementer") in (None, "pending", "ready")
    waiting = json.loads(maestro.state.STATE_JSON.read_text(encoding="utf-8"))["admission_waiting"]
    assert waiting["TASK-001"]["reason"] == "route_pool_saturated"
    assert "fixture-pool" in waiting["TASK-001"]["detail"]
    [journal] = _journal_details("launch_waiting_admission")
    assert journal.startswith("TASK-001 waiting — route_pool_saturated: pool fixture-pool")


def test_no_eligible_route_blocks_and_fails_the_node_with_that_reason(run_lane, resolutions):
    [free_entry] = wf_routing.endpoints_for_catalog([FREE_ENDPOINT])  # paid policy forbids it
    run = run_lane(_graph_scenario("w1_block", routing=lambda: fixture_routing(entries=[free_entry])))

    [call] = resolutions
    assert (call["result"].disposition, call["result"].reason) == ("block", "no_eligible_route")
    assert _windows(run) == []
    assert _node_phase("implementer") == "failed"
    [attempt] = _attempts()
    assert attempt["status"] == "failed"
    [event] = _events(wf_runner.ROUTE_REFUSED_EVENT)
    assert event["attempt_id"] == attempt["attempt_id"]
    assert json.loads(event["payload"])["route_refusal"] == (
        f"route_no_eligible_route: {call['result'].detail}"
    )


def test_routing_receives_the_stored_manifest_of_the_attempt(run_lane, resolutions):
    run = run_lane(_graph_scenario("w2_manifest"))

    [call] = resolutions
    assert isinstance(call["manifest"], ContextManifest), "the manifest itself, not a hand-built context"
    [window] = _windows(run)
    argv = _worker_argv(window)
    manifest_id = argv[argv.index("--manifest-artifact-id") + 1]
    store = maestro.state.control_store()
    stored = json.loads(ArtifactStore(run.repo / ".orchestrator" / "artifacts", store).read(manifest_id))
    assert router.RoutingContext.from_manifest(call["manifest"]) == \
        router.RoutingContext.from_manifest(stored)
    assert stored == call["manifest"].to_dict()
    assert stored["snapshot_id"] == "snap_fixture" and stored["task_id"] == "TASK-001"
    [attempt] = _attempts()
    [row] = store.conn.execute(
        "SELECT type FROM artifacts WHERE artifact_id = ?", (manifest_id,)
    ).fetchall()
    assert row["type"] == "context_manifest"
    [committed] = [e for e in _events("ArtifactCommitted") if e["aggregate_id"] == manifest_id]
    assert committed["attempt_id"] == attempt["attempt_id"], "producer_attempt_id is the attempt"


def test_a_stale_snapshot_fails_the_node_before_routing(run_lane, resolutions):
    stale = StaleSnapshotError("snapshot snap_fixture is stale: drifted maestro/x.py",
                               drifted=("maestro/x.py",))
    run = run_lane(_graph_scenario(
        "w2_stale", routing=lambda: fixture_routing(queries=FixtureQueries(error=stale))
    ))

    assert resolutions == [], "a stale snapshot never reaches resolve_agent"
    assert _windows(run) == []
    assert _node_phase("implementer") == "failed"
    [event] = _events(wf_runner.ROUTE_REFUSED_EVENT)
    assert json.loads(event["payload"])["route_refusal"] == (
        "context_manifest_refused: snapshot snap_fixture is stale: drifted maestro/x.py"
    )


# ── P12A S2 step B: W3 the durable routing decision ──────────────────────────────────


def _attempt_rows() -> list:
    return maestro.state.control_store().conn.execute(
        "SELECT * FROM attempts ORDER BY rowid"
    ).fetchall()


def _usage(*, resets_at, used_pct=12.5, window=300):
    from maestro.backends.base import Usage, WindowUsage

    return Usage(
        context_used_pct=None,  # the fixture driver cannot read a context percentage
        windows={window: WindowUsage(used_pct=used_pct, resets_at=resets_at)},
        updated_at="2026-09-16T00:00:00Z",
    )


def test_a_graph_poll_writes_the_binding_onto_the_attempt_it_claimed(run_lane, resolutions):
    """W3: what was resolved in this poll is readable from the attempt afterwards."""
    run = run_lane(_graph_scenario("w3_binding"))

    [call] = resolutions
    binding = call["result"]
    [attempt] = _attempt_rows()
    assert attempt["binding_id"] == binding.binding_id
    assert (attempt["backend_id"], attempt["model_id"]) == ("fixture_cli", "fixture-1")
    # The column holds the *native* effort, so it and `ShapeKey.effort` are one value.
    assert attempt["effort"] == binding.effort_native_value == "medium"
    assert json.loads(attempt["shape_key_json"])["effort"] == binding.shape_key.effort
    assert attempt["agent_version"] == "1.0.0", "the catalog entry's installed_version"
    assert attempt["session_id"] == attempt["attempt_id"]
    assert attempt["session_mode"] == "fresh", "the one fresh-vs-resume decision"
    assert attempt["manifest_artifact_id"], "W2's manifest artifact travels with the binding"
    assert json.loads(attempt["binding_json"])["requested_effort"] == binding.requested_effort


def test_telemetry_the_fixture_driver_cannot_measure_stays_null(run_lane, resolutions):
    """G6: a value no driver reported is `NULL`, never an estimate standing in for one."""
    run = run_lane(_graph_scenario("w3_nulls"))

    [attempt] = _attempt_rows()
    assert attempt["usage_start_json"] is None, "the fixture routing reads no usage"
    assert attempt["usage_settle_json"] is None
    assert attempt["wall_time_sec"] is None, "nothing has settled yet"
    assert attempt["reset_crossing"] is None, "unknown, which is not the same as 'no'"
    assert attempt["route_refusal"] is None


def test_a_refusal_is_recorded_in_the_route_refusal_column(run_lane, resolutions):
    [free_entry] = wf_routing.endpoints_for_catalog([FREE_ENDPOINT])  # paid policy forbids it
    run = run_lane(_graph_scenario("w3_refusal",
                                   routing=lambda: fixture_routing(entries=[free_entry])))

    [attempt] = _attempt_rows()
    [call] = resolutions
    assert attempt["route_refusal"] == f"route_no_eligible_route: {call['result'].detail}"
    assert attempt["binding_id"] is None, "a refused route binds nothing"
    # The event is kept as well: it is what gives the refusal a place in the ordered history.
    [event] = [
        e for e in _events(wf_runner.ROUTE_REFUSED_EVENT)
        if json.loads(e["payload"])["node_id"] == "implementer"
    ]
    assert json.loads(event["payload"])["route_refusal"] == attempt["route_refusal"]


def test_maestro_explain_shows_the_binding_identity_chosen_in_that_poll(run_lane, resolutions):
    from maestro import status

    run = run_lane(_graph_scenario("w3_explain"))

    [call] = resolutions
    [attempt] = _attempt_rows()
    view = status.workflow_view("TASK-001", repo=run.repo)
    explained = status.explain_view(view, repo=run.repo)
    text = status.render_explain_text(explained)

    identity = f"{attempt['node_id']}|{call['result'].binding_id}"
    assert "fixture_cli/fixture-1 · medium" in text
    assert identity not in text, "the concise text keeps the route; JSON retains the binding id"
    routings = [
        a["routing"] for chain in explained["chains"] for link in chain["links"]
        for a in link["attempts"] if a["routing"]
    ]
    assert routings and routings[0]["identity"] == identity
    assert routings[0]["session_mode"] == "fresh"


def test_every_graph_launch_passes_through_the_single_choke_point(run_lane, monkeypatch):
    """P12A §4: one dispatch function sees each launch exactly once."""
    seen: list = []
    real = wf_runner.WorkflowRunner.launch_worker

    def launch_worker(self, node, claim, *, at):
        seen.append(claim.attempt_id)
        return real(self, node, claim, at=at)

    monkeypatch.setattr(wf_runner.WorkflowRunner, "launch_worker", launch_worker)
    run = run_lane(_graph_scenario("w3_choke"))

    [attempt] = _attempt_rows()
    assert seen == [attempt["attempt_id"]], "exactly one launch, through the choke point"
    assert len(_windows(run)) == 1


# ── W3 settle: outcomes, amendments, usage across a quota reset ──────────────────────
#
# The claim half above is driven through the real loop. The settle half needs a run that
# reaches a terminal outcome, so these drive a real `WorkflowRunner` over a real store
# with the same fixture routing — the loop calls exactly these methods (`tick`,
# `ingest_results`, `settle`) and nothing here reaches a driver, a repo or the host.


class _FakeTmux:
    def __init__(self):
        self.windows: list = []

    def __call__(self, cmd, **kwargs):
        import subprocess

        if cmd[:2] == ["tmux", "new-window"]:
            self.windows.append(cmd[cmd.index("-n") + 1])
        return subprocess.CompletedProcess(cmd, 0, "", "")


def _one_agent_revision(task_id: str):
    from maestro.workflows.models import NodeSpec, WorkflowRevision

    node = NodeSpec(
        node_id="implement", kind="agent", handler_ref="maestro.handlers.noop",
        agent_definition_ref="implementer", effect_class="agent_editing_worktree",
    )
    return WorkflowRevision(
        workflow_id=f"wf_{task_id}", revision=1, task_id=task_id, task_spec_revision=1,
        compiler_version="test", policy_hash="", code_base_sha="deadbeef",
        execution_objective="paid_efficiency", nodes=(node,), edges=(),
        terminal_requirements=("implement",),
    )


def _settle_routing(task_id: str, *, usage_samples=(), token_reader=None):
    """Fixture routing whose `usage_reader` answers with each scripted sample in turn."""
    routing = fixture_routing()
    routing.task_spec = lambda _task_id: normalize_spec(_task(task_id))
    queue = list(usage_samples)

    def usage_reader(binding):
        return queue.pop(0) if queue else None

    routing.usage_reader = usage_reader
    if token_reader is not None:
        routing.token_reader = token_reader
    return routing


def _settled_run(tmp_path, *, task_id="T-settle", usage_samples=(), status="succeeded",
                 token_reader=None):
    """Open a one-node run, drive it to a terminal outcome, return (runner, routing)."""
    from maestro.workflows.handlers import AttemptInbox, TmuxDispatcher, attempts_root
    from maestro.workflows.models import NodeResult

    repo = tmp_path / "repo"
    (repo / ".orchestrator").mkdir(parents=True)
    store = ControlStore.open_controller(repo / ".orchestrator" / "control.sqlite3")
    routing = _settle_routing(task_id, usage_samples=usage_samples,
                              token_reader=token_reader)
    graph_runner = wf_runner.start_run(
        store, _one_agent_revision(task_id), repo=repo,
        dispatcher=TmuxDispatcher(runner=_FakeTmux()),
        artifacts=ArtifactStore(repo / ".orchestrator" / "artifacts", store),
        routing=routing,
    )
    # P12B F6: a `failed` agent result is a bad output, so it is retried until the
    # quality budget is spent; only then does the run settle on the controller's verdict.
    for _ in range(wf_failures.QUALITY_ATTEMPTS if status == "failed" else 1):
        graph_runner.tick()
        row = store.conn.execute("SELECT * FROM attempts ORDER BY rowid").fetchall()[-1]
        inbox = AttemptInbox(attempts_root(repo), row["attempt_id"])
        result = NodeResult(
            run_id=graph_runner.run_id, node_id="implement", attempt_id=row["attempt_id"],
            status=status,
        )
        inbox.write_result(result)
        inbox.write_receipt(operation_id="", exit_status=result.exit_status)
    graph_runner.tick()
    return graph_runner, routing, store


def test_a_verified_outcome_is_appended_and_moves_the_statistics(tmp_path):
    graph_runner, routing, store = _settled_run(tmp_path)

    [row] = store.conn.execute("SELECT * FROM attempts ORDER BY rowid").fetchall()
    shape = wf_routing.shape_key_from_json(row["shape_key_json"])
    assert routing.statistics.observations(shape) == 1, "one verified outcome, one observation"

    [outcome] = store.routing_outcomes()
    assert outcome["attempt_id"] == row["attempt_id"]
    assert (bool(outcome["success"]), outcome["verified_by"]) == (True, "run_outcome")
    assert outcome["amends"] is None
    # Folding the table back yields the same statistics a fresh process would start with.
    assert wf_routing.load_statistics(store).observations(shape) == 1
    store.close()


def test_a_failed_run_records_the_controllers_verdict_not_the_agents(tmp_path):
    graph_runner, routing, store = _settled_run(tmp_path, status="failed")

    [outcome] = store.routing_outcomes()
    assert bool(outcome["success"]) is False
    assert outcome["verified_by"] == "run_outcome", "D06: never the agent's self-report"
    store.close()


def test_a_differing_later_verdict_appends_an_amendment_rather_than_overwriting(tmp_path):
    graph_runner, routing, store = _settled_run(tmp_path)
    [first] = store.routing_outcomes()

    # Later than the settle that recorded `first`: this is a verdict being reopened.
    graph_runner._record_routing_outcomes("failed", at="2099-01-01T00:00:00Z")

    rows = store.routing_outcomes()
    assert len(rows) == 2, "the verdict was appended, not updated"
    assert rows[0]["outcome_id"] == first["outcome_id"]
    assert bool(rows[0]["success"]) is True, "the original row is untouched"
    assert rows[1]["amends"] == first["outcome_id"]
    assert bool(rows[1]["success"]) is False
    # The fold counts one verdict per attempt: the latest one.
    shape = wf_routing.shape_key_from_json(rows[1]["shape_key_json"])
    folded = wf_routing.load_statistics(store)
    assert folded.observations(shape) == 1
    assert folded.records[shape.as_tuple()].successes == 0
    store.close()


def test_a_second_settle_with_the_same_verdict_adds_nothing(tmp_path):
    graph_runner, routing, store = _settled_run(tmp_path)

    graph_runner.settle(at="2026-09-16T03:00:00Z")

    assert len(store.routing_outcomes()) == 1, "an unchanged verdict is not re-recorded"
    store.close()


def test_usage_is_snapshotted_at_claim_and_at_settle_with_wall_time(tmp_path):
    graph_runner, routing, store = _settled_run(
        tmp_path,
        usage_samples=[_usage(resets_at=1000), _usage(resets_at=1000, used_pct=40.0)],
    )

    [row] = store.conn.execute("SELECT * FROM attempts ORDER BY rowid").fetchall()
    start = json.loads(row["usage_start_json"])
    settle = json.loads(row["usage_settle_json"])
    assert start["windows"]["300"]["used_pct"] == 12.5
    assert settle["windows"]["300"]["used_pct"] == 40.0
    assert start["context_used_pct"] is None, "unmeasured stays null through serialization"
    assert row["wall_time_sec"] is not None and row["wall_time_sec"] >= 0
    assert row["reset_crossing"] == 0, "both ends reported windows and none rolled over"
    store.close()


def test_each_usage_sample_says_where_it_came_from_and_when_it_was_read(tmp_path):
    """Per-step quota telemetry: a fresh account read and a carried-forward snapshot
    must be told apart, so the sample keeps its source, its sample time and the moment
    the controller read it."""
    from dataclasses import replace

    graph_runner, routing, store = _settled_run(
        tmp_path,
        usage_samples=[replace(_usage(resets_at=1000), source="claude_oauth_usage_api"),
                       replace(_usage(resets_at=1000), source="claude_oauth_usage_api")],
    )

    [row] = store.conn.execute("SELECT * FROM attempts ORDER BY rowid").fetchall()
    start = json.loads(row["usage_start_json"])
    assert start["source"] == "claude_oauth_usage_api"
    assert start["updated_at"] == "2026-09-16T00:00:00Z", "the sample's own time"
    assert start["read_at"], "when the controller took it"
    assert start["error"] is None
    store.close()


def test_a_failed_usage_read_is_stored_as_a_reason_not_a_number(tmp_path):
    from maestro.backends.base import Usage

    failed = Usage(source="claude_oauth_usage_api", error="fetch failed: URLError")
    graph_runner, routing, store = _settled_run(tmp_path, usage_samples=[failed, failed])

    [row] = store.conn.execute("SELECT * FROM attempts ORDER BY rowid").fetchall()
    start = json.loads(row["usage_start_json"])
    assert start["windows"] == {} and start["error"] == "fetch failed: URLError"
    assert row["reset_crossing"] is None, "an unread quota is not evidence of continuity"
    store.close()


def test_the_attempts_own_token_totals_are_filled_at_settle(tmp_path):
    """`token_split` was null on every pilot attempt. At settle the controller asks the
    driver for the attempt's cumulative tokens, from the attempt's own inbox."""
    seen = []

    def token_reader(binding, workspace):
        seen.append(Path(workspace))
        return {"fresh_input": 5, "cache_read": 50, "cache_write": 7, "output": 9,
                "cost_usd": 0.1, "models": ["fixture-1"], "source": "fixture"}

    graph_runner, routing, store = _settled_run(
        tmp_path, usage_samples=[_usage(resets_at=1000), _usage(resets_at=1000)],
        token_reader=token_reader,
    )

    [row] = store.conn.execute("SELECT * FROM attempts ORDER BY rowid").fetchall()
    settle = json.loads(row["usage_settle_json"])
    assert settle["token_split"]["output"] == 9
    assert settle["token_split"]["source"] == "fixture"
    assert seen == [tmp_path / "repo" / ".orchestrator" / "attempts" / row["attempt_id"]]
    assert json.loads(row["usage_start_json"])["token_split"] is None
    store.close()


def test_a_token_reader_that_raises_leaves_the_split_null(tmp_path):
    def token_reader(binding, workspace):
        raise RuntimeError("transcript vanished")

    graph_runner, routing, store = _settled_run(
        tmp_path, usage_samples=[_usage(resets_at=1000), _usage(resets_at=1000)],
        token_reader=token_reader,
    )

    [row] = store.conn.execute("SELECT * FROM attempts ORDER BY rowid").fetchall()
    assert json.loads(row["usage_settle_json"])["token_split"] is None
    store.close()


def test_a_window_that_resets_mid_attempt_is_flagged_as_crossing(tmp_path):
    graph_runner, routing, store = _settled_run(
        tmp_path,
        usage_samples=[_usage(resets_at=1000), _usage(resets_at=6000, used_pct=1.0)],
    )

    [row] = store.conn.execute("SELECT * FROM attempts ORDER BY rowid").fetchall()
    assert row["reset_crossing"] == 1
    store.close()


def test_a_reset_moment_read_a_second_apart_is_not_a_crossing():
    """Claude's usage endpoint reports one window's `resets_at` with sub-second jitter
    (…199 on one read, …200 on the next); only a real rollover counts."""
    start = {"windows": {"300": {"used_pct": 60.0, "resets_at": 1790452199}}}
    settle = {"windows": {"300": {"used_pct": 62.0, "resets_at": 1790452200}}}
    assert wf_routing.reset_crossing(start, settle) == 0


def test_reset_crossing_is_unknown_rather_than_no_when_nothing_was_measured():
    """`0` is a measurement; `None` is the absence of one. They must not collapse."""
    assert wf_routing.reset_crossing(None, None) is None
    assert wf_routing.reset_crossing({"windows": {}}, {"windows": {}}) is None
    measured = {"windows": {"300": {"used_pct": 1.0, "resets_at": 10}}}
    assert wf_routing.reset_crossing(measured, measured) == 0
    assert wf_routing.reset_crossing(
        measured, {"windows": {"300": {"used_pct": 1.0, "resets_at": 10 + 5 * 3600}}}
    ) == 1, "a rollover moves the reset moment by the window's length"
    # A window the driver reports without a reset moment answers nothing either way.
    unknown = {"windows": {"300": {"used_pct": 1.0, "resets_at": None}}}
    assert wf_routing.reset_crossing(unknown, unknown) is None


# ── unit: configuration, driver mapping, effort delivery ─────────────────────────────


def test_free_tier_endpoints_are_empty_when_absent_and_strict_when_malformed():
    assert config.free_tier_endpoints({}) == []
    assert config.free_tier_endpoints({"engineering": {"runner": "graph"}}) == []
    assert config.free_tier_endpoints({"engineering": {"free_tier_endpoints": None}}) == []
    row = dict(FREE_ENDPOINT, strength="strong", usage_pool_id="free-pool")
    assert config.free_tier_endpoints({"engineering": {"free_tier_endpoints": [row]}}) == [row]
    for bad, match in [
        ({"engineering": "x"}, "must be a mapping"),
        ({"engineering": {"free_tier_endpoints": {"a": 1}}}, "must be a list"),
        ({"engineering": {"free_tier_endpoints": ["free_api"]}}, r"\[0\] must be a mapping"),
        ({"engineering": {"free_tier_endpoints": [dict(FREE_ENDPOINT, cost=0)]}}, "unknown keys"),
        ({"engineering": {"free_tier_endpoints": [
            {k: v for k, v in FREE_ENDPOINT.items() if k != "verified_version"}]}},
         r"missing \['verified_version'\]"),
        ({"engineering": {"free_tier_endpoints": [dict(FREE_ENDPOINT, model="")]}},
         "model must be a non-empty string"),
    ]:
        with pytest.raises(config.ConfigError, match=match):
            config.free_tier_endpoints(bad)


@pytest.mark.parametrize("backend_id, driver", [
    ("claude", "claude"), ("codex", "codex"), ("antigravity_cli", "antigravity"),
])
def test_a_catalog_backend_id_maps_to_its_driver(backend_id, driver):
    assert registry.driver_name_for_backend_id(backend_id) == driver


def test_an_unknown_backend_id_has_no_driver():
    with pytest.raises(registry.UnknownBackend, match="fixture_cli"):
        registry.driver_name_for_backend_id("fixture_cli")


def test_drivers_append_binding_effort_to_their_completion_argv(tmp_path):
    from maestro.backends import antigravity, claude, codex

    spec = CompletionSpec(prompt="go", model="m", effort_args=("--effort", "high"),
                          effort_config=(("model_reasoning_effort", "high"),))
    assert claude.completion_argv(spec)[-3:] == ["--effort", "high", "go"]
    agy = antigravity.completion_argv(spec)
    assert agy[-2:] == ["-p", "go"]
    assert agy[agy.index("--effort"):agy.index("--effort") + 2] == ["--effort", "high"]
    argv = codex.completion_argv("codex", spec, last_message_file=tmp_path / "last")
    assert ["-c", "model_reasoning_effort=high"] == argv[argv.index("-o") + 2:argv.index("-o") + 4]
    assert argv[-4:] == ["--effort", "high", "--ephemeral", "go"]


# ── P12A S2 step C: W8 host admission for graph nodes ────────────────────────────────
#
# The graph loop admits a ready node against the same detected capacity, the same
# `_admission_round` and the same shared ledger as a legacy task, so two projects — or
# the two loops — can never reserve the same RAM twice. Driven through `run_lane`, per §3.
#
# No shipped compiler fragment asks for cpu / ram_mb / vram_mb: every `resource_request`
# names only a writer lease and inference slots. So a heavy node cannot come out of a
# packaged template, and `_heavy_implementers` wraps the real compiler to add `ram_mb` to
# the implementer node of each compiled revision. That absence is itself an assertion —
# `test_a_stock_graph_node_never_touches_the_host_ledger` is the stock path, and it
# reserves nothing.

RAM = 8000.0
HEAVY_MB = 5000.0


@pytest.fixture
def host_capacity(monkeypatch):
    """8 cores, 8 GB, no GPU — pinned, so no assertion depends on the test machine."""
    capacity = scheduling.HostCapacity(limits={"cpu": 8.0, "ram_mb": RAM, "vram_mb": 0.0})
    monkeypatch.setattr(orchestrator, "_host_capacity", lambda: capacity)
    monkeypatch.setattr(orchestrator, "_ELIGIBILITY", scheduling.EligibilityClock())
    return capacity


def _heavy_implementers(monkeypatch, ram_mb) -> None:
    """Compile for real, then give each revision's implementer node counted RAM.

    `ram_mb` is an amount, or a `task_id -> amount` mapping when the tasks differ.
    """
    from maestro.workflows import compiler as wf_compiler

    real = wf_compiler.compile_workflow

    def compile_workflow(spec, **kwargs):
        revision = real(spec, **kwargs)
        amount = ram_mb[spec.task_id] if isinstance(ram_mb, dict) else ram_mb
        nodes = tuple(
            dataclasses.replace(
                node, resource_request={**dict(node.resource_request), "ram_mb": amount}
            )
            if node.node_id == "implementer" else node
            for node in revision.nodes
        )
        return dataclasses.replace(revision, nodes=nodes)

    monkeypatch.setattr(wf_compiler, "compile_workflow", compile_workflow)


def _counted_rows() -> list:
    return HostReservationLedger().reservations(COUNTED_POOL)


def _admission_waiting() -> dict:
    state = json.loads(maestro.state.STATE_JSON.read_text(encoding="utf-8"))
    return state.get("admission_waiting") or {}


def _two_heavy_tasks(lane: str, **overrides) -> LaneScenario:
    # Disjoint scopes, so write-set overlap (D20) lets both open in one poll.
    return _graph_scenario(lane, tasks=[_task("TASK-001", scope=["a.txt"]),
                                        _task("TASK-002", scope=["b.txt"])], **overrides)


def test_with_ram_for_one_of_two_heavy_graph_nodes_one_is_claimed_and_one_waits(
    run_lane, host_capacity, monkeypatch
):
    _heavy_implementers(monkeypatch, HEAVY_MB)

    run = run_lane(_two_heavy_tasks("w8_one_fits"))

    assert run.returncode == 0
    [window] = _windows(run)
    assert f"-n {handlers.window_name('TASK-001', 'implementer')} " in window
    assert [a["node_id"] for a in _attempts()] == ["implementer"], \
        "the node that could not be admitted claimed nothing"
    why = _admission_waiting()["TASK-002"]
    assert why["reason"] == "resource_exhausted" and "ram_mb" in why["detail"]
    [journal] = _journal_details("launch_waiting_admission")
    assert journal.startswith("TASK-002 waiting — resource_exhausted: ram_mb")
    [row] = _counted_rows()
    assert row.amounts == {"ram_mb": HEAVY_MB} and row.project_id == str(orchestrator.REPO)


def test_two_projects_sharing_the_ledger_never_reserve_more_ram_than_capacity(
    run_lane, host_capacity, monkeypatch
):
    HostReservationLedger().reserve_counted(
        {"ram_mb": 6000}, capacity={"ram_mb": RAM}, project_id="/other/project",
        run_id="impl-X-1", writer_pid=os.getpid(),
    )
    _heavy_implementers(monkeypatch, {"TASK-001": 4000.0, "TASK-002": 1000.0})

    run = run_lane(_two_heavy_tasks("w8_two_projects"))

    assert run.returncode == 0
    [window] = _windows(run)
    assert f"-n {handlers.window_name('TASK-002', 'implementer')} " in window
    assert "2000 of 8000 free" in _admission_waiting()["TASK-001"]["detail"]
    rows = _counted_rows()
    assert {r.project_id for r in rows} == {"/other/project", str(orchestrator.REPO)}
    assert sum(r.amounts.get("ram_mb", 0.0) for r in rows) <= RAM


def test_a_ledger_refusal_withdraws_the_admission_and_records_the_wait(
    run_lane, host_capacity, monkeypatch
):
    """The in-process round said yes; the ledger — another project got there first — says no."""
    withdrawn: list = []
    real_withdraw = scheduling.AdmissionRound.withdraw

    def withdraw(self, candidate):
        withdrawn.append(candidate.candidate_id)
        return real_withdraw(self, candidate)

    monkeypatch.setattr(scheduling.AdmissionRound, "withdraw", withdraw)
    monkeypatch.setattr(
        HostReservationLedger, "held_amounts", lambda self, exclude_project=None: {}
    )
    HostReservationLedger().reserve_counted(
        {"ram_mb": 6000}, capacity={"ram_mb": RAM}, project_id="/other/project",
        run_id="impl-X-1", writer_pid=os.getpid(),
    )
    _heavy_implementers(monkeypatch, 4000.0)

    run = run_lane(_graph_scenario("w8_withdraw"))

    assert _windows(run) == [] and _attempts() == [], "nothing was claimed"
    assert len(withdrawn) == 1, "the admission it took in-process was given back"
    why = _admission_waiting()["TASK-001"]
    assert why["reason"] == "resource_exhausted"
    assert "held by: /other/project" in why["detail"]
    assert [r.project_id for r in _counted_rows()] == ["/other/project"]


def test_an_ingested_result_gives_the_attempts_host_reservation_back(
    run_lane, host_capacity, monkeypatch
):
    _heavy_implementers(monkeypatch, HEAVY_MB)

    def finish_then_halt(run) -> None:
        from maestro.workflows.models import NodeResult

        store = maestro.state.control_store()
        row = store.conn.execute(
            "SELECT attempt_id, run_id, node_id, status FROM attempts WHERE node_id = 'implementer'"
        ).fetchone()
        if row is None:
            return
        if row["status"] in ("claimed", "running"):
            inbox = handlers.AttemptInbox(handlers.attempts_root(run.repo), row["attempt_id"])
            inbox.write_result(NodeResult(
                run_id=row["run_id"], node_id=row["node_id"],
                attempt_id=row["attempt_id"], status="succeeded",
            ))
            inbox.write_receipt(operation_id="", exit_status=0)
            return
        run.halt()

    run = run_lane(_graph_scenario("w8_release_on_ingest", on_poll=finish_then_halt))

    assert run.returncode == 0
    assert [a["status"] for a in _attempts() if a["node_id"] == "implementer"] == ["succeeded"]
    assert _counted_rows() == [], "the hold ended with the attempt that took it"


def test_a_launch_that_fails_gives_its_host_reservation_back(
    run_lane, host_capacity, monkeypatch
):
    _heavy_implementers(monkeypatch, HEAVY_MB)

    run = run_lane(_graph_scenario("w8_release_on_launch_failure",
                                   failing_commands={"tmux new-window": 1}))

    assert run.returncode == 0
    assert _node_phase("implementer") == "failed"
    assert _counted_rows() == [], "a claim that will never be redeemed holds nothing"


def test_a_stock_graph_node_never_touches_the_host_ledger(run_lane, host_capacity):
    """No packaged fragment requests cpu / ram_mb / vram_mb, so nothing is reserved."""
    run = run_lane(_graph_scenario("w8_stock"))

    assert run.returncode == 0
    assert len(_windows(run)) == 1, "the stock path dispatches exactly as it did before W8"
    assert _counted_rows() == [] and _admission_waiting() == {}


def test_a_node_larger_than_the_whole_host_fails_to_park_instead_of_waiting(
    run_lane, host_capacity, monkeypatch
):
    """P12A W9: an oversized request is a refusal on the one `_fail_claim` path, not a wait."""
    _heavy_implementers(monkeypatch, RAM * 2)
    polls: list = []

    def halt_after_the_second_poll(run) -> None:
        polls.append(len(_windows(run)))
        if len(polls) == 2:
            run.halt()

    run = run_lane(_graph_scenario("w9_oversized", on_poll=halt_after_the_second_poll))

    assert run.returncode == 0
    assert polls[0] == 0, "the first poll launched nothing"
    assert _node_phase("implementer") == "failed"
    [attempt] = [a for a in _attempt_rows() if a["node_id"] == "implementer"]
    assert attempt["status"] == "failed"
    assert attempt["route_refusal"] == (
        f"exceeds_host_capacity: ram_mb requested {RAM * 2:g}, host capacity {RAM:g}"
    )
    [event] = [
        e for e in _events(wf_runner.ROUTE_REFUSED_EVENT)
        if json.loads(e["payload"])["node_id"] == "implementer"
    ]
    assert json.loads(event["payload"])["route_refusal"] == attempt["route_refusal"]
    assert _admission_waiting() == {}, "an oversized node is not a wait"
    assert _counted_rows() == [], "and it reserved nothing"
    # P12B F7: the graph loop compiles with escalation, so an agent's `on_failure` edge
    # reaches `park` by way of `diagnose` rather than directly.
    assert _node_phase("diagnose") is not None, "its on_failure edge reaches diagnose"


# ── W4: candidate reap ───────────────────────────────────────────────────────────────
#
# A free-quality lane widened to three candidates. c1 and c2 finish with passing,
# controller-ingested evidence and a head commit; c3 crashes (its launch fails, so it
# leaves a failed attempt with no committed outcome). The join must still run, pick
# exactly one winner, and report c3 as `abandoned` rather than dropping it.

CANDIDATES = ("implementer_c1", "implementer_c2", "implementer_c3")


def _three_candidates(monkeypatch) -> None:
    """Compile every revision under the free objective, widened to three candidates."""
    from maestro.workflows import compiler as wf_compiler

    policy = default_policy().with_objective("free_quality")
    wide = dataclasses.replace(
        policy, free_quality=dataclasses.replace(policy.free_quality, candidates=3)
    )
    real = wf_compiler.compile_workflow

    def compile_workflow(spec, **kwargs):
        return real(spec, **{**kwargs, "policy": wide})

    monkeypatch.setattr(wf_compiler, "compile_workflow", compile_workflow)


def _passing_evidence(attempt_id: str, check_id: str) -> dict:
    from maestro.validation import VerificationEvidence

    return VerificationEvidence(
        evidence_id=f"ev-{attempt_id}-{check_id}", check_id=check_id,
        source_snapshot_id="snap-1", attempt_id=attempt_id, command_hash="cmd",
        input_fingerprint="in", environment_fingerprint="env", exit_status=0,
        outcome="passed",
    ).to_row()


def _finish_workers_until_the_join(run) -> None:
    """Every launched worker succeeds; candidates report evidence and a head commit."""
    from maestro.workflows.models import NodeResult

    if _node_phase("candidate_select") in ("succeeded", "failed", "skipped"):
        run.halt()
        return
    store = maestro.state.control_store()
    for row in store.conn.execute(
        "SELECT attempt_id, run_id, node_id FROM attempts WHERE status IN ('claimed', 'running')"
    ).fetchall():
        inbox = handlers.AttemptInbox(handlers.attempts_root(run.repo), row["attempt_id"])
        if inbox.result() is not None:
            continue
        outputs: dict = {}
        if row["node_id"] in CANDIDATES:
            outputs = {
                "head_commit": f"commit-{row['node_id']}",
                "evidence": [_passing_evidence(row["attempt_id"], check)
                             for check in ("unit", "lint")],
            }
        inbox.write_result(NodeResult(
            run_id=row["run_id"], node_id=row["node_id"], attempt_id=row["attempt_id"],
            status="succeeded", outputs=outputs,
        ))
        inbox.write_receipt(operation_id="", exit_status=0)


def test_a_crashed_candidate_is_reaped_as_abandoned_and_one_winner_is_selected(
    run_lane, monkeypatch
):
    from maestro.workflows import candidates as candidate_join

    _three_candidates(monkeypatch)
    launched: list = []
    real_launch = wf_runner.WorkflowRunner.launch_worker

    def launch_worker(self, node, claim, *, at):
        launched.append(node.node_id)
        return real_launch(self, node, claim, at=at)

    selections: list = []
    real_select = candidate_join.select_winner

    def select_winner(outcomes):
        selections.append({o.candidate_id: o for o in outcomes})
        return real_select(outcomes)

    monkeypatch.setattr(wf_runner.WorkflowRunner, "launch_worker", launch_worker)
    monkeypatch.setattr(candidate_join, "select_winner", select_winner)
    crashed = handlers.window_name("TASK-001", "implementer_c3")

    strong = fixture_cli_entry(
        model_profiles={'fixture-1': cat.ModelProfile('fixture-1')},
        pair_profiles=cat.migrate_pairs(('fixture-1', 'strong', 1.0)),
        pairs_migrated=True,
        concurrency_limit=3,
    )
    run = run_lane(_graph_scenario(
        "w4_candidate_reap",
        routing=lambda: fixture_routing(entries=[strong]),
        on_poll=_finish_workers_until_the_join,
        failing_commands={f"-n {crashed} ": 1},
    ))

    assert run.returncode == 0
    assert [n for n in launched if n in CANDIDATES] == list(CANDIDATES), \
        "every candidate launched exactly once, through the choke point"
    assert _node_phase("implementer_c3") == "failed"
    assert _node_phase("candidate_select") == "succeeded"
    assert _node_phase("park") in (None, "pending")

    [outcomes] = selections
    assert set(outcomes) == {"c1", "c2", "c3"}, "the crashed candidate is present, not dropped"
    assert outcomes["c3"].status == "abandoned"
    assert [outcomes[c].status for c in ("c1", "c2")] == ["succeeded", "succeeded"]
    assert outcomes["c1"].passed_checks == 2 and outcomes["c1"].head_commit == \
        "commit-implementer_c1"

    [join] = [a for a in _attempts() if a["node_id"] == "candidate_select"]
    result = handlers.AttemptInbox(handlers.attempts_root(run.repo), join["attempt_id"]).result()
    assert result.outputs["winner"] == "c1", "most passed checks, then lowest index"
    assert sorted(result.outputs["losers"]) == ["c2", "c3"]
    assert result.outputs["winner_commit"] == "commit-implementer_c1"


DEAD_PID = 4_000_000


def _candidate_revision(task_id: str):
    """Three candidate nodes joined by the selection node on `always` edges."""
    from maestro.workflows.models import EdgeSpec, NodeSpec, WorkflowRevision

    candidates = tuple(
        NodeSpec(
            node_id=f"implementer_{label}", kind="agent", handler_ref="maestro.handlers.noop",
            agent_definition_ref="implementer", effect_class="agent_editing_worktree",
            params={"candidate_id": label},
            resource_request={"writer_lease": True,
                              "resource_id": f"worktree:{task_id}/{label}"},
        )
        for label in ("c1", "c2", "c3")
    )
    join = NodeSpec(
        node_id="candidate_select", kind="decision",
        handler_ref=wf_runner.CANDIDATE_SELECT_HANDLER, permissions_ref="controller_only",
        params={"task_id": task_id}, resource_request={"writer_lease": False},
    )
    return WorkflowRevision(
        workflow_id=f"wf_{task_id}", revision=1, task_id=task_id, task_spec_revision=1,
        compiler_version="test", policy_hash="", code_base_sha="deadbeef",
        execution_objective="free_quality", nodes=(*candidates, join),
        edges=tuple(EdgeSpec(c.node_id, "candidate_select", guard_predicate="always")
                    for c in candidates),
        terminal_requirements=("candidate_select",),
    )


def test_only_a_committed_result_becomes_a_candidate_document(tmp_path):
    """A settled c1 has a document; c2 reconciled with no receipt and c3 still silent do not.

    Driven without routing: the join's input is the same either way. A provider that
    tries to substitute its own documents is overridden (D06).
    """
    from maestro.workflows.handlers import AttemptInbox, TmuxDispatcher, attempts_root
    from maestro.workflows.models import NodeResult

    repo = tmp_path / "repo"
    (repo / ".orchestrator").mkdir(parents=True)
    store = ControlStore.open_controller(repo / ".orchestrator" / "control.sqlite3")
    graph_runner = wf_runner.start_run(
        store, _candidate_revision("T-reap"), repo=repo,
        dispatcher=TmuxDispatcher(runner=_FakeTmux()),
        artifacts=ArtifactStore(repo / ".orchestrator" / "artifacts", store),
        is_alive=lambda pid: pid != DEAD_PID,
    )
    graph_runner.inputs_provider = lambda node: {"candidates": {"c3": {"status": "succeeded"}}}
    graph_runner.tick()
    attempts = {row["node_id"]: row["attempt_id"] for row in store.conn.execute(
        "SELECT node_id, attempt_id FROM attempts ORDER BY rowid")}
    assert set(attempts) == {"implementer_c1", "implementer_c2", "implementer_c3"}

    c1 = AttemptInbox(attempts_root(repo), attempts["implementer_c1"])
    c1.write_result(NodeResult(
        run_id=graph_runner.run_id, node_id="implementer_c1",
        attempt_id=attempts["implementer_c1"], status="succeeded", detail="done",
        outputs={"head_commit": "abc123",
                 "evidence": [_passing_evidence(attempts["implementer_c1"], "unit")]},
    ))
    c1.write_receipt(operation_id="", exit_status=0)
    AttemptInbox(attempts_root(repo), attempts["implementer_c2"]).bind_pid(DEAD_PID)
    graph_runner.tick()  # ingest c1, bind c2's pid
    graph_runner.tick()  # reconcile c2's dead writer: no receipt, no outcome

    c2 = store.conn.execute(
        "SELECT status, outcome_ref FROM attempts WHERE attempt_id = ?",
        (attempts["implementer_c2"],),
    ).fetchone()
    assert c2["status"] == "uncertain" and c2["outcome_ref"] is None

    documents = graph_runner.node_inputs(graph_runner.revision.node("candidate_select"))
    documents = documents["candidates"]
    assert set(documents) == {"c1"}, "only a committed result is a document"
    assert documents["c1"]["status"] == "succeeded"
    assert documents["c1"]["head_commit"] == "abc123"
    assert documents["c1"]["detail"] == "done"
    assert [row["check_id"] for row in documents["c1"]["evidence"]] == ["unit"]
    assert documents["c1"]["evidence"][0]["attempt_id"] == attempts["implementer_c1"]

    graph_runner.artifacts = None
    assert graph_runner.candidate_documents(
        graph_runner.revision.node("candidate_select")
    ) == {}, "no artifact store, no trustworthy outcome, no documents"


# ── W5/W6: affected checks at verification, persisted shadow comparisons ──────────────
#
# The stock graph's `gate` node is a real verification worker here: the lane runs the
# recorded worker argv in-process, so the selection travels the whole way — computed at
# claim, stored as an artifact, parsed by the worker, honoured by the gate — and the
# comparison is written by the controller when it ingests the result.

AFFECTED = {"engineering": {"runner": "graph", "affected_checks": "authoritative"}}
TWO_CHECKS = [{"id": "unit", "kind": "auto", "cmd": "true"},
              {"id": "lint", "kind": "auto", "cmd": "true"}]


def _shadow_rows() -> list:
    return maestro.state.control_store().shadow_comparisons()


def _window_for(run, attempt_id: str) -> str:
    [window] = [w for w in _windows(run) if f"--attempt-id {attempt_id} " in w]
    return window


def _run_the_gate_worker(run) -> None:
    """The gate's worker really runs; every other worker simply succeeds."""
    from maestro.workflows.models import NodeResult

    if _node_phase("gate") in ("succeeded", "failed"):
        run.halt()
        return
    store = maestro.state.control_store()
    for row in store.conn.execute(
        "SELECT attempt_id, run_id, node_id FROM attempts WHERE status IN ('claimed', 'running')"
    ).fetchall():
        inbox = handlers.AttemptInbox(handlers.attempts_root(run.repo), row["attempt_id"])
        if inbox.result() is not None:
            continue
        if row["node_id"] == "gate":
            wf_worker.main(_worker_argv(_window_for(run, row["attempt_id"])))
            continue
        inbox.write_result(NodeResult(
            run_id=row["run_id"], node_id=row["node_id"], attempt_id=row["attempt_id"],
            status="succeeded",
        ))
        inbox.write_receipt(operation_id="", exit_status=0)


def _seed_comparisons(count: int):
    """`before_run`: the control database with `count` clean stored comparisons."""
    from maestro.validation import ShadowComparison, VerificationSpec, select_affected_checks

    def seed(run) -> None:
        spec = VerificationSpec(
            check_id="unit", argv_or_adapter_ref=("true",), shell=True,
            acceptance_criterion_ids=("unit",), input_dependencies=("src/**",),
            dependency_completeness="exact", cache_policy="exact_snapshot_only",
        )
        store = ControlStore.open_controller(maestro.state.CONTROL_DB)
        try:
            for index in range(count):
                comparison = ShadowComparison(
                    select_affected_checks([spec], ["src/a.py"], mode="shadow"),
                    {"unit": "passed"},
                )
                store.record_shadow_comparison(comparison.to_row(
                    run_id=f"run-{index}", node_id="gate", snapshot_id=f"snap-{index}",
                    attempt_id=f"att-{index}", recorded_at="2026-09-16T00:00:00Z",
                ))
        finally:
            store.close()

    return seed


def test_a_shadow_verification_runs_the_full_suite_and_its_comparison_survives_a_restart(
    run_lane, monkeypatch
):
    from maestro import gates, validation

    gate_calls: list = []
    real_gate = gates.run_authoritative_gate

    def run_authoritative_gate(task_id, worktree, **kwargs):
        gate_calls.append(kwargs.get("check_ids"))
        return real_gate(task_id, worktree, **kwargs)

    monkeypatch.setattr(gates, "run_authoritative_gate", run_authoritative_gate)
    run = run_lane(_graph_scenario(
        "w5_shadow_selection",
        tasks=[_task("TASK-001", verifications=TWO_CHECKS)],
        on_poll=_run_the_gate_worker,
    ))

    assert run.returncode == 0
    assert _node_phase("gate") == "succeeded"
    [gate] = [a for a in _attempts() if a["node_id"] == "gate"]
    argv = _worker_argv(_window_for(run, gate["attempt_id"]))
    selection_id = argv[argv.index("--affected-selection-artifact-id") + 1]
    stored = json.loads(ArtifactStore(run.repo / ".orchestrator" / "artifacts",
                                      maestro.state.control_store()).read(selection_id))
    assert stored["mode"] == "shadow"
    assert stored["full_suite"] is True
    assert stored["expansion_reason"] == "change set unknown", "the lane repo has no git diff"
    assert gate_calls == [("unit", "lint")], "the worker ran the full suite"

    result = handlers.AttemptInbox(handlers.attempts_root(run.repo), gate["attempt_id"]).result()
    assert result.outputs["affected_selection"] == stored
    assert sorted(e["check_id"] for e in result.outputs["evidence"]) == ["lint", "unit"]

    [row] = _shadow_rows()
    assert row["node_id"] == "gate" and row["attempt_id"] == gate["attempt_id"]
    assert row["snapshot_id"] == result.outputs["snapshot"]
    assert json.loads(row["outcomes_json"]) == {"unit": "passed", "lint": "passed"}
    assert json.loads(row["discrepancies_json"]) == []

    # the controller restarts: the row is in the database, not in the process
    maestro.state._reset_control_store()
    reopened = ControlStore.open_client(maestro.state.CONTROL_DB)
    try:
        [comparison] = validation.load_shadow_comparisons(reopened)
    finally:
        reopened.close()
    assert comparison.selection.selected == ("unit", "lint")
    assert comparison.outcomes == {"unit": "passed", "lint": "passed"}


def test_an_unrouted_verification_node_gets_no_selection(tmp_path):
    """`routing is None`: the gate is claimed and launched exactly as before W5."""
    from maestro.workflows.handlers import TmuxDispatcher

    repo = tmp_path / "repo"
    (repo / ".orchestrator").mkdir(parents=True)
    store = ControlStore.open_controller(repo / ".orchestrator" / "control.sqlite3")
    try:
        revision = compile_workflow(
            normalize_spec(_task("T-unrouted", verifications=TWO_CHECKS)),
            workflow_id="wf_T-unrouted", code_base_sha="deadbeef",
        )
        tmux = _FakeTmux()
        graph_runner = wf_runner.start_run(
            store, revision, repo=repo, dispatcher=TmuxDispatcher(runner=tmux),
            artifacts=ArtifactStore(repo / ".orchestrator" / "artifacts", store),
        )
        gate = revision.node("gate")
        claim = graph_runner.claim(gate)
        assert claim.selection_artifact_id is None
        argv = wf_runner._default_worker_argv(graph_runner, gate, claim)
        assert "--affected-selection-artifact-id" not in argv
        # W8 lane 6: a tmux window gets the tmux server's environment, so the worker is
        # started through the pinned launcher of the checkout that dispatched it.
        assert argv[:3] == [
            sys.executable,
            str(bootstrap.running_root() / "maestro" / "pinned.py"),
            "maestro.workflows.worker",
        ]
        assert "affected_selection" not in graph_runner.node_inputs(gate)
        assert store.shadow_comparisons() == []
    finally:
        store.close()


def test_authoritative_below_minimum_runs_is_refused_with_the_count_missing(run_lane):
    routing = fixture_routing()
    run = run_lane(_graph_scenario(
        "w6_switch_refused",
        project_yaml=AFFECTED,
        routing=lambda: routing,
        before_run=_seed_comparisons(3),
    ))

    assert run.returncode == 0
    assert routing.affected_mode == "shadow"
    [refusal] = _journal_details("affected_mode_refused")
    assert "3/20 clean shadow runs recorded" in refusal
    assert [a["node_id"] for a in _attempts()] == ["implementer"], "the loop keeps running"


def test_authoritative_is_accepted_on_twenty_clean_stored_comparisons(run_lane):
    routing = fixture_routing()
    run = run_lane(_graph_scenario(
        "w6_switch_accepted",
        project_yaml=AFFECTED,
        routing=lambda: routing,
        before_run=_seed_comparisons(20),
    ))

    assert run.returncode == 0
    assert routing.affected_mode == "authoritative"
    assert _journal_details("affected_mode_refused") == []


def test_an_unreadable_affected_checks_value_stops_the_graph_loop(run_lane, spies):
    run = run_lane(_graph_scenario(
        "w6_switch_invalid",
        project_yaml={"engineering": {"runner": "graph", "affected_checks": "sometimes"}},
    ))

    assert run.returncode == 1
    assert any("affected_checks" in d for d in _journal_details("runner_config_error"))
    assert spies["tick"] == []


# ── P12A S3 step C: W7 plan repair in the loop ───────────────────────────────────────
#
# A free-quality lane opens with a planner. The planner's result carries a
# `PlanRepairProposal` (a discovered prerequisite between the planner and the test
# designer). The poll that ingests it cancels the old run and opens revision N + 1; the
# next poll launches the new node through the ordinary `advance` path.

PREREQ = "prereq_fixture"


def _free_quality(monkeypatch) -> None:
    """Compile every revision under the free objective, which opens with a planner."""
    from maestro.workflows import compiler as wf_compiler

    policy = default_policy().with_objective("free_quality")
    real = wf_compiler.compile_workflow

    def compile_workflow(spec, **kwargs):
        return real(spec, **{**kwargs, "policy": policy})

    monkeypatch.setattr(wf_compiler, "compile_workflow", compile_workflow)


def _strong_routing():
    strong = fixture_cli_entry(
        model_profiles={'fixture-1': cat.ModelProfile('fixture-1')},
        pair_profiles=cat.migrate_pairs(('fixture-1', 'strong', 1.0)),
        pairs_migrated=True,
        concurrency_limit=3,
    )
    return fixture_routing(entries=[strong])


def _prerequisite_proposal(workflow_id: str, base_revision: int) -> dict:
    from maestro.workflows.compiler import PlanRepairProposal
    from maestro.workflows.models import EdgeSpec, NodeSpec

    prereq = NodeSpec(
        node_id=PREREQ, kind="agent", handler_ref="maestro.handlers.agent",
        agent_definition_ref="implementer", permissions_ref="frozen_snapshot_reader",
        resource_request={"writer_lease": False, "inference_slots": 1},
        params={"task_id": "TASK-001", "title": "build the missing fixture"},
    )
    return PlanRepairProposal(
        workflow_id=workflow_id, base_revision=base_revision,
        kind="discovered_prerequisite", reason="the suite needs a fixture first",
        add_nodes=(prereq,),
        add_edges=(
            EdgeSpec(source_node_id="planner", target_node_id=PREREQ,
                     guard_predicate="on_success"),
            EdgeSpec(source_node_id=PREREQ, target_node_id="test_designer",
                     guard_predicate="on_success"),
        ),
    ).to_dict()


def _answer_with_a_proposal(node_id: str, after_answer):
    """`on_poll`: the first live `node_id` attempt succeeds carrying a proposal."""
    from maestro.workflows.compiler import latest_revision
    from maestro.workflows.models import NodeResult

    answered: list = []

    def on_poll(run) -> None:
        store = maestro.state.control_store()
        if answered:
            after_answer(run)
            return
        for row in store.conn.execute(
            "SELECT a.attempt_id, a.run_id, a.node_id, r.workflow_id FROM attempts a"
            " JOIN task_runs r ON a.run_id = r.run_id"
            " WHERE a.status IN ('claimed', 'running') AND a.node_id = ?", (node_id,)
        ).fetchall():
            revision = latest_revision(store, row["workflow_id"])
            inbox = handlers.AttemptInbox(handlers.attempts_root(run.repo), row["attempt_id"])
            inbox.write_result(NodeResult(
                run_id=row["run_id"], node_id=node_id, attempt_id=row["attempt_id"],
                status="succeeded",
                outputs={"plan_repair": _prerequisite_proposal(
                    revision.workflow_id, revision.revision)},
            ))
            inbox.write_receipt(operation_id="", exit_status=0)
            answered.append(row["attempt_id"])

    return on_poll


def _runs_with_revisions() -> list:
    return maestro.state.control_store().conn.execute(
        "SELECT run_id, status, workflow_revision FROM task_runs"
        " ORDER BY workflow_revision, started_at, run_id"
    ).fetchall()


def test_a_planner_proposal_moves_the_loop_to_revision_n_plus_one(run_lane, monkeypatch):
    from maestro.workflows.compiler import repair_counters

    _free_quality(monkeypatch)
    launched: list = []
    real_launch = wf_runner.WorkflowRunner.launch_worker

    def launch_worker(self, node, claim, *, at):
        launched.append((node.node_id, self.run_id))
        return real_launch(self, node, claim, at=at)

    monkeypatch.setattr(wf_runner.WorkflowRunner, "launch_worker", launch_worker)
    seen_after_repair: list = []

    def after_answer(run) -> None:
        runs = _runs_with_revisions()
        if len(runs) < 2:
            return
        if not seen_after_repair:
            # The repairing poll itself launched nothing on the new run.
            seen_after_repair.append([n for n, r in launched if r == runs[1]["run_id"]])
            return
        run.halt()

    run = run_lane(_graph_scenario(
        "w7_plan_repair", routing=_strong_routing,
        on_poll=_answer_with_a_proposal("planner", after_answer),
    ))

    assert run.returncode == 0
    old, new = _runs_with_revisions()
    assert (old["status"], old["workflow_revision"]) == ("canceled", 1)
    assert (new["status"], new["workflow_revision"]) == ("running", 2)
    assert seen_after_repair == [[]], "the new revision dispatches on the next poll"
    assert launched == [("planner", old["run_id"]), (PREREQ, new["run_id"])], \
        "each launch exactly once, through the choke point"

    store = maestro.state.control_store()
    counters = repair_counters(store, "wf_TASK-001", run_id=new["run_id"])
    assert counters.repairs_used == 1
    assert counters.task_attempts == 2, "the old run's planner attempt still counts"
    assert counters.node_attempts["planner"] == 1, "carried node keeps its attempt_index"
    assert store.conn.execute(
        "SELECT phase FROM node_states WHERE run_id = ? AND node_id = 'planner'",
        (new["run_id"],),
    ).fetchone()["phase"] == "succeeded"
    [repaired] = _events("WorkflowRepaired")
    assert json.loads(repaired["payload"])["carried"] == ["planner"]
    assert _journal_details("graph_run_repaired") == [
        f"TASK-001 {old['run_id']}→{new['run_id']} revision 1→2"
    ]
    assert not _events("WorkflowRepairRefused")


def test_a_proposal_past_the_repair_quota_is_refused_and_fails_the_node(run_lane, monkeypatch):
    _free_quality(monkeypatch)

    def after_answer(run) -> None:
        run.halt()

    run = run_lane(_graph_scenario(
        "w7_repair_quota", routing=_strong_routing,
        project_yaml={"engineering": {"runner": "graph", "max_plan_repairs": 0}},
        on_poll=_answer_with_a_proposal("planner", after_answer),
    ))

    assert run.returncode == 0
    [only] = _runs_with_revisions()
    assert only["workflow_revision"] == 1, "no new run exists"
    assert _node_phase("planner") == "failed"
    assert [a["status"] for a in _attempts() if a["node_id"] == "planner"] == ["failed"]
    [refused] = _events("WorkflowRepairRefused")
    payload = json.loads(refused["payload"])
    assert payload["node_id"] == "planner"
    assert "repair quota exhausted: 0/0" in payload["reason"]
    assert not _events("WorkflowRepaired")


def test_a_proposal_from_a_node_that_is_not_a_planner_or_diagnoser_is_ignored(run_lane):
    def after_answer(run) -> None:
        run.halt()

    run = run_lane(_graph_scenario(
        "w7_not_a_planner", on_poll=_answer_with_a_proposal("implementer", after_answer),
    ))

    assert run.returncode == 0
    assert len(_runs_with_revisions()) == 1
    assert _node_phase("implementer") == "succeeded"
    assert not _events("WorkflowRepairRefused")
    assert not _events("WorkflowRepaired")


def test_max_plan_repairs_defaults_to_one_and_refuses_what_it_cannot_read():
    assert config.max_plan_repairs({}) == 1
    assert config.max_plan_repairs({"engineering": None}) == 1
    assert config.max_plan_repairs({"engineering": {"runner": "graph"}}) == 1
    assert config.max_plan_repairs({"engineering": {"max_plan_repairs": 0}}) == 0
    assert config.max_plan_repairs({"engineering": {"max_plan_repairs": 3}}) == 3
    for bad in (-1, "2", 1.5, True):
        with pytest.raises(config.ConfigError, match="max_plan_repairs"):
            config.max_plan_repairs({"engineering": {"max_plan_repairs": bad}})
    with pytest.raises(config.ConfigError, match="must be a mapping"):
        config.max_plan_repairs({"engineering": 1})
    assert wf_routing.DispatchRouting(
        catalog=None, policy=None, queries=None, task_spec=None
    ).max_plan_repairs is None


def test_the_production_routing_observes_usage_and_the_cli_version(tmp_path, monkeypatch):
    """P12 pilots: `_graph_routing` left `usage_reader` inert and read `agent_version` from
    the register, so real attempts kept NULL windows and a stale CLI version."""
    from maestro import orchestrator

    monkeypatch.setattr(orchestrator, "REPO", tmp_path)
    routing = orchestrator._graph_routing()
    assert routing.usage_reader is wf_routing.driver_usage
    assert routing.version_reader is wf_routing.observed_agent_version
    assert routing.token_reader is wf_routing.driver_attempt_tokens


def test_driver_usage_prefers_a_fresh_subscription_read(monkeypatch):
    """The statusline snapshot is the interactive session's, not re-read per attempt."""
    from types import SimpleNamespace

    from maestro.backends import registry
    from maestro.backends.base import Usage

    class _Driver:
        def usage(self, handle=None):
            return Usage(source="statusline_usage_json")

        def subscription_usage(self):
            return Usage(source="claude_oauth_usage_api")

    monkeypatch.setattr(registry, "get_backend", lambda name: _Driver())
    sample = wf_routing.driver_usage(SimpleNamespace(backend_id="claude"))
    assert sample.source == "claude_oauth_usage_api"
