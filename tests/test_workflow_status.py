"""`[P11]`: the operator projections over a compiled graph.

Two questions an operator asks that nothing could answer before this phase — *what is this
task's graph and where did it get to* (`maestro workflow show`) and *why does it stand
there* (`maestro explain`) — and one property that matters more than either: **the export
carries no credentials.** The HTML page is the artifact most likely to leave the machine,
because it is built to be forwarded, so the secret tests here are not decoration.

Everything runs against a real `ControlStore` on `tmp_path` with a hand-built revision. Hand
built rather than compiled because these tests are about *projection*, and a compiled graph
would make every assertion depend on the current template's stage list.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from maestro import status
from maestro.control.lifecycle import Lifecycle
from maestro.control.store import ControlStore
from maestro.workflows.compiler import store_revision
from maestro.workflows.failures import FailureRecord
from maestro.workflows.models import EdgeSpec, NodeSpec, WorkflowRevision

#: A Telegram bot token's exact shape. Planted in the places a real one could plausibly
#: reach — an acceptance reason, an event payload — to prove the export drops or scrubs it.
#:
#: Assembled rather than written out because `tests/test_purity.py::test_no_credentials_
#: anywhere` scans this repository for credential-shaped literals and is right to: a gate
#: that a test file could opt out of by needing a fixture would stop being a gate. The value
#: still has to match `status.SECRET_PATTERNS` exactly, which is the point of the test.
FAKE_TOKEN = "9876543210" + ":" + "B" * 35


# ── a graph with one of everything the projections have to render ────────────────────


def _revision() -> WorkflowRevision:
    nodes = (
        NodeSpec(node_id="plan", kind="agent", handler_ref="maestro.handlers.agent",
                 agent_definition_ref="planner", permissions_ref="read_only"),
        NodeSpec(node_id="implement_c1", kind="agent", handler_ref="maestro.handlers.agent",
                 agent_definition_ref="implementer", effect_class="agent_editing_worktree",
                 resource_request={"writer_lease": True, "resource_id": "worktree:T-1/c1"},
                 params={"candidate_id": "c1"}),
        NodeSpec(node_id="implement_c2", kind="agent", handler_ref="maestro.handlers.agent",
                 agent_definition_ref="implementer", effect_class="agent_editing_worktree",
                 resource_request={"writer_lease": True, "resource_id": "worktree:T-1/c2"},
                 params={"candidate_id": "c2"}),
        NodeSpec(node_id="review", kind="agent", handler_ref="maestro.handlers.agent",
                 agent_definition_ref="reviewer", permissions_ref="read_only",
                 params={"review_lens": "security"}),
        NodeSpec(node_id="verify", kind="function",
                 handler_ref="maestro.handlers.verification",
                 effect_class="repeatable_local_check"),
        NodeSpec(node_id="accept", kind="function", handler_ref="maestro.handlers.acceptance",
                 permissions_ref="controller_only"),
    )
    edges = (
        EdgeSpec(source_node_id="plan", target_node_id="implement_c1"),
        EdgeSpec(source_node_id="plan", target_node_id="implement_c2"),
        EdgeSpec(source_node_id="implement_c1", target_node_id="review"),
        EdgeSpec(source_node_id="implement_c2", target_node_id="review"),
        EdgeSpec(source_node_id="review", target_node_id="verify"),
        EdgeSpec(source_node_id="verify", target_node_id="accept",
                 guard_predicate="on_success"),
    )
    return WorkflowRevision(
        workflow_id="wf_T1", revision=1, task_id="T-1", task_spec_revision=1,
        compiler_version="p7.1", policy_hash="policyhash0000", code_base_sha="c0ffee00",
        execution_objective="free_quality", nodes=nodes, edges=edges,
        # `WorkflowRevision.terminal_requirements` is a list of terminal NODE ids, not of
        # engineering criteria — the criteria are the compiler's, checked against the nodes
        # (see `explain_view`). `human_receipt` is deliberately unsatisfiable by this graph:
        # an operator must be able to see a criterion nothing is bound to.
        terminal_requirements=("accept",),
    )


@pytest.fixture
def project(tmp_path) -> Path:
    """A project whose control plane holds one finished-ish run of `_revision()`."""
    repo = tmp_path / "repo"
    (repo / ".orchestrator").mkdir(parents=True)
    store = ControlStore.open_controller(repo / ".orchestrator" / "control.sqlite3")
    life = Lifecycle(store)
    revision = _revision()

    store.register_task("T-1", spec_revision=1)
    store_revision(store, revision)
    life.set_task_status("T-1", "active")
    life.start_run("T-1", "run_1", workflow_id="wf_T1", workflow_revision=1)

    for node_id in ("plan", "implement_c1", "implement_c2", "review"):
        life.set_node_phase("run_1", node_id, "ready")
        life.set_node_phase("run_1", node_id, "claimed")
        life.set_node_phase("run_1", node_id, "running")
        life.set_node_phase("run_1", node_id, "succeeded")

    life.claim_attempt("att_verify", run_id="run_1", node_id="verify", lease_epoch=3)
    life.start_attempt("att_verify", run_id="run_1", writer_pid=4242)
    life.finish_attempt("att_verify", run_id="run_1", status="succeeded")
    life.set_node_phase("run_1", "verify", "ready")
    life.set_node_phase("run_1", "verify", "claimed", attempt_id="att_verify")
    life.set_node_phase("run_1", "verify", "running")
    life.set_node_phase("run_1", "verify", "succeeded")

    store.record_evidence({
        "evidence_id": "ev_cited", "check_id": "pytest", "source_snapshot_id": "snap1",
        "attempt_id": "att_verify", "command_hash": "cmd1", "input_fingerprint": "in1",
        "environment_fingerprint": "env1", "exit_status": 0, "outcome": "pass",
        "validity": "current", "recorded_at": "2026-09-13T01:00:00Z",
        "verifier_version": "1", "started_at": "2026-09-13T00:59:00Z",
        "finished_at": "2026-09-13T01:00:00Z", "output_artifact_refs": "[]",
    })
    store.record_evidence({
        "evidence_id": "ev_uncited", "check_id": "lint", "source_snapshot_id": "snap1",
        "attempt_id": "att_verify", "command_hash": "cmd2", "input_fingerprint": "in2",
        "environment_fingerprint": "env1", "exit_status": 0, "outcome": "pass",
        "validity": "current", "recorded_at": "2026-09-13T01:00:05Z",
        "verifier_version": "1", "started_at": "2026-09-13T00:59:00Z",
        "finished_at": "2026-09-13T01:00:05Z", "output_artifact_refs": "[]",
    })
    store.record_acceptance({
        "decision_id": "dec_1", "task_id": "T-1", "run_id": "run_1",
        "task_spec_revision": 1, "policy_hash": "policyhash0000",
        "integrated_source_snapshot": "snap1",
        "required_evidence_ids": json.dumps(["ev_cited"]),
        "review_dispositions": "{}", "exemptions": "[]", "required_effect_receipts": "[]",
        "decision": "accepted",
        # A credential planted where free text really does flow from: whatever the
        # controller was told the reason was.
        "reason": f"green with TELEGRAM_BOT_TOKEN={FAKE_TOKEN}",
        "event_id": "evt_1", "decided_at": "2026-09-13T01:01:00Z",
    })
    # And one in an event payload, which is the other way a secret reaches an export.
    store.append_event("NodeAttemptFinished", "run_1", payload={"stdout": FAKE_TOKEN},
                       run_id="run_1", attempt_id="att_verify")
    store.close()
    return repo


@pytest.fixture
def view(project) -> dict:
    return status.workflow_view("T-1", repo=project)


# ── the view ─────────────────────────────────────────────────────────────────────────


def test_a_project_with_no_control_plane_says_so_rather_than_crashing(tmp_path):
    empty = tmp_path / "plain"
    (empty / ".orchestrator").mkdir(parents=True)
    with pytest.raises(status.WorkflowViewError) as exc:
        status.workflow_view("T-1", repo=empty)
    assert "control database" in str(exc.value)


def test_an_unknown_task_is_a_refusal_not_an_empty_graph(project):
    with pytest.raises(status.WorkflowViewError) as exc:
        status.workflow_view("T-404", repo=project)
    assert "T-404" in str(exc.value)


def test_the_view_lists_every_node_in_dependency_order(view):
    ids = [node["node_id"] for node in view["nodes"]]
    assert set(ids) == {"plan", "implement_c1", "implement_c2", "review", "verify", "accept"}
    assert ids.index("plan") < ids.index("implement_c1") < ids.index("review")
    assert ids.index("verify") < ids.index("accept")


def test_the_view_carries_the_phase_attempts_and_evidence_of_each_node(view):
    verify = next(n for n in view["nodes"] if n["node_id"] == "verify")
    assert verify["phase"] == "succeeded"
    assert [a["attempt_id"] for a in verify["attempts"]] == ["att_verify"]
    assert verify["attempts"][0]["lease_epoch"] == 3
    assert {e["check_id"] for e in verify["attempts"][0]["evidence"]} == {"pytest", "lint"}

    pending = next(n for n in view["nodes"] if n["node_id"] == "accept")
    assert pending["phase"] == "pending", "a node nothing has touched is pending, not missing"


def test_candidates_keep_their_identity_and_their_own_resource(view):
    candidates = {n["node_id"]: n for n in view["nodes"] if n["candidate_id"]}
    assert set(candidates) == {"implement_c1", "implement_c2"}
    resources = {n["resource_id"] for n in candidates.values()}
    assert len(resources) == 2, (
        "two candidates sharing one resource_id is the cross-pollution [INV-06] forbids — "
        "the operator view must be able to show that they do not"
    )


def test_reading_a_view_writes_nothing(project):
    """`[INV-01]`: an operator looking at a graph must not be able to move it."""
    store = ControlStore.open_client(project / ".orchestrator" / "control.sqlite3")
    before = store.head_sequence()
    store.close()

    status.explain_view(status.workflow_view("T-1", repo=project), repo=project)

    store = ControlStore.open_client(project / ".orchestrator" / "control.sqlite3")
    assert store.head_sequence() == before
    store.close()


# ── the text rendering ───────────────────────────────────────────────────────────────


def test_the_text_view_names_the_graph_its_nodes_and_the_verdict(view):
    text = status.render_workflow_text(view)
    assert "T-1" in text and "wf_T1@1" in text
    for node_id in ("plan", "implement_c1", "review", "verify", "accept"):
        assert node_id in text
    assert "accepted at 2026-09-13T01:01:00Z" in text


def test_text_views_are_bounded_and_show_only_useful_run_facts(view):
    workflow = status.render_workflow_text(view)
    explain = status.render_explain_text(status.explain_view(view))
    assert len(workflow.splitlines()) <= 17
    assert len(explain.splitlines()) <= 17
    assert "compiler " not in workflow
    assert "epoch=" not in workflow
    assert "criterion:" not in explain
    assert "NOT cited by any acceptance decision" not in explain
    assert "Checks: pytest pass, lint pass" in explain
    assert "Usage: unavailable" in workflow


def test_terminal_run_does_not_claim_a_pending_node_is_active(view):
    view["run"]["status"] = "canceled"
    view["run"]["started_at"] = "2026-09-28T18:00:00Z"
    view["run"]["finished_at"] = "2026-09-28T19:30:00Z"
    text = status.render_workflow_text(view)
    assert "run canceled" in text
    assert "Now:" not in text
    assert "1h 30m" in text


def test_waiting_view_shows_step_last_route_and_sampled_usage(project):
    store = ControlStore.open_controller(project / ".orchestrator" / "control.sqlite3")
    life = Lifecycle(store)
    life.claim_attempt("att_review", run_id="run_1", node_id="review", lease_epoch=4)
    life.record_routing_decision(
        "att_review", run_id="run_1", fields={
            "binding_id": "claude:opus:medium@rev1", "backend_id": "claude",
            "model_id": "opus", "effort": "medium",
            "usage_start_json": json.dumps({
                "updated_at": "2026-09-28T18:00:00Z",
                "windows": {"300": {"used_pct": 92}, "10080": {"used_pct": 61}},
            }),
            "usage_settle_json": json.dumps({
                "updated_at": "2026-09-28T18:10:00Z",
                "windows": {"300": {"used_pct": 0}, "10080": {"used_pct": 61}},
            }),
        },
    )
    life.record_failure("att_review", run_id="run_1", record=FailureRecord(kind="quota_exhausted", evidence="limit"))
    life.finish_attempt("att_review", run_id="run_1", status="failed")
    store.conn.execute("UPDATE node_states SET phase = 'ready' WHERE run_id = 'run_1' AND node_id = 'review'")
    store.close()
    (project / ".orchestrator" / "state.json").write_text(json.dumps({
        "graph_runs": [{"task_id": "T-1", "run_id": "run_1"}],
        "admission_waiting": {"T-1": {"reason": "route_pool_paused", "since": "2026-09-28T18:05:00Z"}},
    }))

    text = status.render_workflow_text(status.workflow_view("T-1", repo=project))
    assert "Waiting: review — route pool paused" in text
    assert "Last: claude/opus · medium · failed" in text
    assert "Usage (start sample 2026-09-28 21:00 IDT): 5h 92%, 7d 61%" in text
    assert "Usage now:" not in text


def test_progress_uses_the_control_run_when_a_graph_waits(project, monkeypatch):
    from maestro.hitl import commands

    state = {"in_flight": [], "graph_runs": [{
        "task_id": "T-1", "run_id": "run_1", "started_at": "2026-09-28T18:00:00Z",
        "nodes": [], "status": "running",
    }], "admission_waiting": {"T-1": {"reason": "route_pool_paused"}}}
    (project / ".orchestrator" / "state.json").write_text(json.dumps(state))
    monkeypatch.setattr(commands, "REPO", project)
    monkeypatch.setattr(commands, "read_state", lambda: state)
    report = commands._build_progress_report()
    assert "Waiting:" in report
    assert "route pool paused" in report
    assert "between steps" not in report
    assert "Usage: unavailable" in report


def test_expired_pool_pause_is_not_shown_at_its_reset_second(project, monkeypatch):
    store = ControlStore.open_controller(project / ".orchestrator" / "control.sqlite3")
    store.record_pool_pause({
        "pool_id": "codex-subscription", "paused_until": "2026-09-28T19:00:00Z",
        "reset_known": 1, "reason": "quota", "failure_kind": "quota_exhausted",
        "attempt_id": "att_verify", "recorded_at": "2026-09-28T18:00:00Z",
    })
    store.close()
    (project / ".orchestrator" / "state.json").write_text(json.dumps({
        "graph_runs": [{"task_id": "T-1", "run_id": "run_1"}],
        "admission_waiting": {"T-1": {"reason": "route_pool_paused"}},
    }))

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 28, 19, 0, 0, 500000, tzinfo=timezone.utc)

    monkeypatch.setattr(status, "datetime", FixedDatetime)
    view = status.workflow_view("T-1", repo=project)
    assert view["pool_pauses"] == []


def test_no_rendering_leaks_a_credential(view):
    """Every operator-facing string goes through the redactor, not just the HTML."""
    text = status.render_workflow_text(view)
    explained = status.render_explain_text(status.explain_view(view))
    for rendered in (text, explained):
        assert FAKE_TOKEN not in rendered
        assert status.REDACTION in rendered


def test_short_decision_does_not_expose_a_truncated_credential(view):
    view["acceptances"][-1]["reason"] = "x" * 114 + " " + FAKE_TOKEN
    rendered = status.render_workflow_text(view)
    assert "98765" not in rendered


# ── the causal trace ─────────────────────────────────────────────────────────────────


def test_explain_binds_each_criterion_to_the_nodes_that_satisfy_it(view):
    explained = status.explain_view(view)
    chains = {chain["criterion"]: chain for chain in explained["chains"]}

    assert {"implementation", "verification_gate", "acceptance", "independent_review",
            "human_receipt"} <= set(chains)
    implementation = {link["node_id"] for link in chains["implementation"]["links"]}
    assert implementation == {"implement_c1", "implement_c2"}
    assert [l["node_id"] for l in chains["verification_gate"]["links"]] == ["verify"]
    assert [l["node_id"] for l in chains["independent_review"]["links"]] == ["review"]


def test_a_criterion_no_node_is_bound_to_is_reported_as_unbound(view):
    chains = {c["criterion"]: c for c in status.explain_view(view)["chains"]}
    assert chains["human_receipt"]["unbound"] is True
    assert chains["human_receipt"]["links"] == []


def test_explain_says_which_evidence_the_acceptance_decision_actually_cited(view):
    chains = {c["criterion"]: c for c in status.explain_view(view)["chains"]}
    link = chains["verification_gate"]["links"][0]
    assert {e["evidence_id"] for e in link["evidence"]} == {"ev_cited", "ev_uncited"}
    assert link["evidence_cited_by_acceptance"] == ["ev_cited"], (
        "evidence that exists but was not cited is exactly what an operator asking "
        "'what was this decided on?' needs to see"
    )
    text = status.render_explain_text(status.explain_view(view))
    assert "Evidence cited: 1/2" in text


def test_explain_reads_the_recorded_decision_rather_than_recomputing_one(view):
    explained = status.explain_view(view)
    assert explained["verdict"].startswith("accepted at")
    assert explained["acceptances"][0]["decision"] == "accepted"
    assert FAKE_TOKEN not in json.dumps(explained)


def test_explain_shows_the_durable_routing_decision_recorded_on_an_attempt(project):
    """P12A W3: the binding is read back from the attempt, never re-resolved now."""
    store = ControlStore.open_controller(project / ".orchestrator" / "control.sqlite3")
    Lifecycle(store).record_routing_decision(
        "att_verify", run_id="run_1",
        fields={
            "binding_id": "claude:sonnet-5:high@rev1",
            "binding_json": json.dumps({"usage_pool_id": "anthropic-sub"}),
            "backend_id": "claude", "model_id": "sonnet-5", "effort": "high",
            "agent_version": "2.1.0", "session_id": "att_verify", "session_mode": "fresh",
        },
    )
    store.close()

    explained = status.explain_view(status.workflow_view("T-1", repo=project), repo=project)
    chains = {c["criterion"]: c for c in explained["chains"]}
    [attempt] = chains["verification_gate"]["links"][0]["attempts"]
    routing = attempt["routing"]
    assert routing["identity"] == "verify|claude:sonnet-5:high@rev1", (
        "`metrics.binding_identity`'s form, so the operator and the duplicate-decision "
        "detector are comparing the same string"
    )
    assert routing["summary"] == "claude/sonnet-5 · effort high · pool anthropic-sub"
    assert (routing["session_mode"], routing["agent_version"]) == ("fresh", "2.1.0")
    assert "binding verify|claude:sonnet-5:high@rev1" not in status.render_explain_text(explained)


def test_an_attempt_with_no_routing_decision_claims_no_binding(view):
    chains = {c["criterion"]: c for c in status.explain_view(view)["chains"]}
    [attempt] = chains["verification_gate"]["links"][0]["attempts"]
    assert attempt["routing"] is None, (
        "an attempt that predates W3, or that the graph runner never routed, has no "
        "routing decision to report — and inventing one would be a claim about history"
    )


def test_explain_shows_the_classified_failure_recorded_on_an_attempt(project):
    """P12B F3: `explain_view` reads the failure back from the attempt, never reclassifies."""
    store = ControlStore.open_controller(project / ".orchestrator" / "control.sqlite3")
    Lifecycle(store).record_failure(
        "att_verify", run_id="run_1",
        record=FailureRecord(kind="rate_limited", evidence=f"429 {FAKE_TOKEN}"),
    )
    store.close()

    explained = status.explain_view(status.workflow_view("T-1", repo=project), repo=project)
    chains = {c["criterion"]: c for c in explained["chains"]}
    [attempt] = chains["verification_gate"]["links"][0]["attempts"]
    failure = attempt["failure"]
    assert (failure["kind"], failure["class"]) == ("rate_limited", "blocked")
    assert FAKE_TOKEN not in failure["evidence"], "the redaction that covers routing covers failures too"
    text = status.render_explain_text(explained)
    assert FAKE_TOKEN not in text


def test_explain_uses_the_latest_failure_across_different_steps(view):
    explained = status.explain_view(view)
    explained["acceptances"] = []
    explained["decision_line"] = "Decision: pending"
    links = {link["node_id"]: link for chain in explained["chains"] for link in chain["links"]}
    links["review"]["attempts"] = [{"_order": 1, "failure": {"kind": "old", "class": "blocked"}}]
    links["plan"]["attempts"] = [{"_order": 2, "failure": {"kind": "new", "class": "blocked"}}]
    assert "Latest issue: new (blocked)" in status.render_explain_text(explained)


def test_an_attempt_with_no_recorded_failure_has_none(view):
    chains = {c["criterion"]: c for c in status.explain_view(view)["chains"]}
    [attempt] = chains["verification_gate"]["links"][0]["attempts"]
    assert attempt["failure"] is None


# ── `maestro workflow failures` ───────────────────────────────────────────────────────


def test_list_failures_filters_by_task_and_kind(project):
    store = ControlStore.open_controller(project / ".orchestrator" / "control.sqlite3")
    Lifecycle(store).record_failure(
        "att_verify", run_id="run_1",
        record=FailureRecord(kind="rate_limited", evidence=f"429 {FAKE_TOKEN}"),
    )
    store.close()

    rows = status.list_failures("T-1", repo=project)
    assert [r["attempt_id"] for r in rows] == ["att_verify"]
    assert rows[0]["node_id"] == "verify"
    assert rows[0]["failure_kind"] == "rate_limited"
    assert FAKE_TOKEN not in rows[0]["evidence"]

    assert status.list_failures("T-1", kind="rate_limited", repo=project) == rows
    assert status.list_failures("T-1", kind="timeout", repo=project) == []
    assert status.list_failures("T-404", repo=project) == []
    with pytest.raises(status.WorkflowViewError, match="unknown failure kind"):
        status.list_failures("T-1", kind="not_a_real_kind", repo=project)


def test_render_failures_text_lists_one_line_per_failure_and_redacts(project):
    store = ControlStore.open_controller(project / ".orchestrator" / "control.sqlite3")
    Lifecycle(store).record_failure(
        "att_verify", run_id="run_1",
        record=FailureRecord(kind="rate_limited", evidence=f"429 {FAKE_TOKEN}"),
    )
    store.close()

    text = status.render_failures_text(status.list_failures("T-1", repo=project))
    assert "verify" in text and "rate_limited" in text and "(blocked)" in text
    assert FAKE_TOKEN not in text


# ── the HTML export ──────────────────────────────────────────────────────────────────


@pytest.fixture
def html(view) -> str:
    return status.render_workflow_html(view)


def test_the_html_export_is_self_contained(html):
    """No network, ever: the page is read over SSH, off a phone, out of a forward."""
    assert "<script src" not in html and "<link " not in html
    assert "https://" not in html
    # The one `http://` allowed is the SVG namespace, which is an identifier, not a fetch.
    assert html.count("http://") == html.count('xmlns="http://www.w3.org/2000/svg"')
    assert "@import" not in html


def test_the_html_export_draws_the_graph_and_the_timeline(html):
    assert "<svg" in html and "</svg>" in html
    assert html.count('class="node') == 6, "one box per node"
    assert 'class="edge"' in html
    assert 'class="tbar' in html, "the one recorded attempt must appear on the timeline"
    assert 'id="payload"' in html, "the detail panel's data is embedded, not fetched"


def test_the_html_export_carries_no_credentials(html):
    assert FAKE_TOKEN not in html
    assert status.REDACTION in html


def test_the_html_export_carries_no_event_payloads(project, html):
    """Structural, not just redacted: an event body is handler output and never ships.

    The redactor is the second line of defence. This is the first — a secret shape nobody
    wrote a pattern for is still absent, because the field it lives in is not exported.
    """
    assert "stdout" not in html


def test_the_html_export_escapes_what_it_does_render(tmp_path, project):
    """A node id is compiler output, but the page must not be a markup injection anyway."""
    view = status.workflow_view("T-1", repo=project)
    view["nodes"][0]["node_id"] = '<img src=x onerror="alert(1)">'
    html = status.render_workflow_html(view)
    assert "<img src=x" not in html
    assert "&lt;img" in html


# ── the CLI surface ──────────────────────────────────────────────────────────────────


def test_workflow_show_and_explain_are_reachable_from_the_cli(project, capsys):
    from maestro import cli

    assert cli.main(["workflow", "--repo", str(project), "show", "T-1"]) == 0
    assert "verify" in capsys.readouterr().out

    assert cli.main(["explain", "--repo", str(project), "T-1"]) == 0
    assert "Checks: pytest pass, lint pass" in capsys.readouterr().out


def test_workflow_show_html_writes_one_file_the_operator_can_open(project, capsys):
    from maestro import cli

    out = project / "graph.html"
    assert cli.main(["workflow", "--repo", str(project), "show", "T-1", "--html",
                     "--out", str(out)]) == 0
    assert str(out) in capsys.readouterr().out
    body = out.read_text(encoding="utf-8")
    assert body.startswith("<!doctype html>")
    assert FAKE_TOKEN not in body


def test_a_missing_task_exits_non_zero_rather_than_printing_an_empty_page(project, capsys):
    from maestro import cli

    assert cli.main(["workflow", "--repo", str(project), "show", "T-404"]) == 1
    assert "T-404" in capsys.readouterr().out


# ── P12A W11: every run of a task ────────────────────────────────────────────────────


def _add_a_repaired_run(project: Path) -> None:
    """A second run of T-1 that started in the *same second* as the first, under a run id
    that sorts before it — so only insertion order can put it second."""
    store = ControlStore.open_controller(project / ".orchestrator" / "control.sqlite3")
    try:
        life = Lifecycle(store)
        started = store.conn.execute(
            "SELECT started_at FROM task_runs WHERE run_id = 'run_1'"
        ).fetchone()["started_at"]
        life.finish_run("T-1", "run_1", "failed", at=started)
        life.start_run("T-1", "run_0_repaired", workflow_id="wf_T1", workflow_revision=2,
                       at=started)
    finally:
        store.close()


def test_list_runs_lists_every_run_of_a_task_in_start_order(project):
    _add_a_repaired_run(project)

    rows = status.list_runs("T-1", repo=project)

    assert [r["run_id"] for r in rows] == ["run_1", "run_0_repaired"]
    assert set(rows[0]) == {"run_id", "workflow_id", "workflow_revision", "status",
                            "started_at", "finished_at"}
    assert (rows[0]["status"], rows[0]["workflow_revision"]) == ("failed", 1)
    assert rows[0]["finished_at"] == rows[0]["started_at"] == rows[1]["started_at"]
    assert (rows[1]["status"], rows[1]["workflow_revision"], rows[1]["finished_at"]) == (
        "running", 2, None
    )
    text = status.render_runs_text(rows)
    assert text.index("run_1") < text.index("run_0_repaired")
    assert "wf_T1@2" in text and "running" in text


def test_list_runs_refuses_an_unknown_task(project):
    with pytest.raises(status.WorkflowViewError, match="T-404"):
        status.list_runs("T-404", repo=project)


def test_workflow_runs_is_reachable_from_the_cli_as_text_and_json(project, capsys):
    from maestro import cli

    _add_a_repaired_run(project)
    assert cli.main(["workflow", "--repo", str(project), "runs", "T-1"]) == 0
    out = capsys.readouterr().out
    assert out.index("run_1") < out.index("run_0_repaired")

    assert cli.main(["workflow", "--repo", str(project), "runs", "T-1", "--json"]) == 0
    assert [r["run_id"] for r in json.loads(capsys.readouterr().out)] == [
        "run_1", "run_0_repaired"
    ]
    assert cli.main(["workflow", "--repo", str(project), "runs", "T-404"]) == 1
