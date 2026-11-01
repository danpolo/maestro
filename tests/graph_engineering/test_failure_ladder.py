"""P12B F2/F3: the failure ladder, proven under the real `orchestrator.main`.

The implementer's worker really runs in-process against a scripted `agent_cli` driver, so a
failure travels the whole way: the driver's completion and exit verdict are classified by the
worker (F2), committed as a `failed` result that skips handler adoption, and read back by the
controller onto the attempt row (F3) — where `explain`, `list_failures` and
`maestro workflow failures` find it.

A controller without routing writes no failure columns: classification rides on the P12A
dispatch wiring, never ahead of it.
"""
from __future__ import annotations

import dataclasses
import json
import subprocess

import pytest

import maestro.state
from maestro import cli, status
from maestro.backends.base import Completion, ExitVerdict, Usage, WindowUsage
from maestro.control.artifacts import ArtifactStore
from maestro.control.store import ControlStore
from maestro.taskgraph import normalize_spec
from maestro.workflows import handlers
from maestro.workflows import routing as wf_routing
from maestro.workflows import runner as wf_runner
from maestro.workflows import worker as wf_worker
from maestro.workflows.compiler import compile_workflow
from maestro.workflows.models import NodeResult
from maestro.workflows.policy import default_policy

from .conftest import fixture_cli_entry, fixture_routing
from .test_dispatch_wiring import (
    FREE_ENDPOINT,
    _create_control_db,
    _events,
    _FakeTmux,
    _journal_details,
    _graph_scenario,
    _node_phase,
    _runs_with_revisions,
    _task,
    _window_for,
    _worker_argv,
)

FAILURE_COLUMNS = "failure_kind, failure_class, failure_json"


@pytest.fixture(autouse=True)
def _fresh_control_store():
    maestro.state._reset_control_store()
    yield
    maestro.state._reset_control_store()


class _FakeCliDriver:
    """`reports_done`: write DONE where the brief says, as an agent that finished does.
    Without it the answer carries no sentinel, which is a `no_sentinel` bad output."""

    def __init__(self, completion, verdict=None, parse_exit_raises=None, reports_done=False):
        self._completion = completion
        self._verdict = verdict
        self._raises = parse_exit_raises
        self._reports_done = reports_done

    def complete(self, spec):
        if self._reports_done:
            import re
            from pathlib import Path

            workspace = re.search(r"^WORKSPACE: (\S+)$", spec.prompt, re.MULTILINE)
            handlers.write_sentinel(Path(workspace.group(1)), handlers.DONE_SENTINEL, "done")
        return self._completion

    def parse_exit(self, rc, tail):
        if self._raises is not None:
            raise self._raises
        return self._verdict


def _patch_fixture_cli_driver(monkeypatch, driver) -> None:
    from maestro.backends import registry

    monkeypatch.setattr(registry, "driver_name_for_backend_id", lambda backend_id: "fixture_cli")
    monkeypatch.setattr(registry, "get_backend", lambda name, *a, **kw: driver)


@pytest.fixture
def adoptions(monkeypatch):
    """Every handler the *worker* adopts through (the controller resolves its own nodes too)."""
    from types import SimpleNamespace

    calls: list = []
    real = handlers.resolve

    def resolve(handler_ref):
        calls.append(handler_ref)
        return real(handler_ref)

    monkeypatch.setattr(wf_worker, "handlers", SimpleNamespace(resolve=resolve))
    return calls


def _implementer_attempts() -> list:
    return maestro.state.control_store().conn.execute(
        f"SELECT attempt_id, status, {FAILURE_COLUMNS} FROM attempts "
        "WHERE node_id = 'implementer' ORDER BY started_at, rowid"
    ).fetchall()


def _run_the_implementer_worker(worktree=None):
    """`on_poll`: run the implementer's worker once, halt once its first attempt settled.

    `worktree` is a test seam, not a stand-in for the runner: these sandboxes are not git
    repositories, so the runner hands the writer no tree of its own (`task_tree`), and a
    lane that needs one supplies it here. Git-backed lanes get the runner's tree instead
    (`test_a_retry_starts_from_the_sha_its_predecessor_claimed_at`, `test_end_to_end`).
    """
    def on_poll(run) -> None:
        attempts = _implementer_attempts()
        if not attempts or attempts[0]["status"] not in ("claimed", "running") \
                or _node_phase("implementer") in ("succeeded", "failed"):
            run.halt()
            return
        attempt_id = attempts[0]["attempt_id"]
        inbox = handlers.AttemptInbox(handlers.attempts_root(run.repo), attempt_id)
        if inbox.result() is None:
            argv = _worker_argv(_window_for(run, attempt_id))
            if worktree is not None:
                argv += ["--worktree", str(worktree)]
            wf_worker.main(argv)

    return on_poll


def _settled_attempt(run_lane, lane, **kwargs):
    run = run_lane(_graph_scenario(lane, on_poll=_run_the_implementer_worker(**kwargs)))
    assert run.returncode == 0
    first = _implementer_attempts()[0]
    assert first["status"] not in ("claimed", "running"), "the controller ingested the result"
    result = handlers.AttemptInbox(handlers.attempts_root(run.repo), first["attempt_id"]).result()
    return run, first, result


def _assert_failure(first, result, kind: str) -> dict:
    assert result.status == "failed"
    assert result.outputs["failure"]["kind"] == kind
    assert first["failure_kind"] == kind
    assert first["failure_class"]
    stored = json.loads(first["failure_json"])
    assert stored == result.outputs["failure"]
    return stored


@pytest.mark.parametrize("evidence, kind", [
    ("429 rate limit exceeded", "rate_limited"),
    ("weekly quota spent", "quota_exhausted"),
])
def test_a_quota_verdict_is_classified_and_written_onto_the_attempt(
    run_lane, monkeypatch, adoptions, evidence, kind
):
    _patch_fixture_cli_driver(monkeypatch, _FakeCliDriver(
        Completion(text="", returncode=1, timed_out=False),
        ExitVerdict(kind="quota_exhausted", reset_at="2026-09-18T00:00:00Z", evidence=evidence),
    ))
    _, first, result = _settled_attempt(run_lane, f"f3_quota_{kind}")

    stored = _assert_failure(first, result, kind)
    assert stored["evidence"] == evidence
    assert stored["reset_at"] == "2026-09-18T00:00:00Z"
    assert adoptions == [], "a classified failure is never adopted"


@pytest.mark.parametrize("verdict_kind, kind", [("crashed", "worker_crashed"), ("weird", "unknown")])
def test_a_crash_or_unrecognised_verdict_is_classified(
    run_lane, monkeypatch, adoptions, verdict_kind, kind
):
    _patch_fixture_cli_driver(monkeypatch, _FakeCliDriver(
        Completion(text="partial", returncode=3, timed_out=False),
        ExitVerdict(kind=verdict_kind, evidence="boom"),
    ))
    _, first, result = _settled_attempt(run_lane, f"f3_verdict_{verdict_kind}")

    _assert_failure(first, result, kind)
    assert adoptions == []


def test_a_timeout_is_classified_before_the_exit_verdict_is_read(run_lane, monkeypatch, adoptions):
    _patch_fixture_cli_driver(monkeypatch, _FakeCliDriver(
        Completion(text="", returncode=0, timed_out=True),
        parse_exit_raises=AssertionError("parse_exit must not be called on a timeout"),
    ))
    _, first, result = _settled_attempt(run_lane, "f3_timeout")

    _assert_failure(first, result, "timeout")
    assert adoptions == []


class _Answer429:
    def post(self, url, *, headers, body, timeout):
        return 429, {"error": {"message": "slow down"}}, {"Retry-After": "120"}


def test_a_free_tier_429_is_rate_limited_with_its_retry_hint(run_lane, monkeypatch, adoptions):
    from maestro.backends.model_api import ModelAPI

    free_policy = dataclasses.replace(default_policy(), objective="free_quality")
    [entry] = wf_routing.endpoints_for_catalog([FREE_ENDPOINT])
    monkeypatch.setattr(wf_worker, "_model_api", lambda endpoint: ModelAPI(
        endpoint, transport=_Answer429(), environ={endpoint.api_key_env: "test-key"}))
    run = run_lane(_graph_scenario(
        "f1_free_tier_429",
        project_yaml={"engineering": {"runner": "graph", "free_tier_endpoints": [FREE_ENDPOINT]}},
        routing=lambda: fixture_routing(entries=[entry], policy=free_policy),
        on_poll=_run_the_implementer_worker(),
    ))
    assert run.returncode == 0
    first = _implementer_attempts()[0]
    result = handlers.AttemptInbox(handlers.attempts_root(run.repo), first["attempt_id"]).result()

    stored = _assert_failure(first, result, "rate_limited")
    assert stored["retry_after_sec"] == 120
    assert result.outputs["runtime"]["stop_reason"] == "rate_limited"
    assert adoptions == []


def _committed_repo(path):
    path.mkdir(parents=True)
    for args in (["init", "-q"], ["config", "user.email", "t@example.invalid"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=path, check=True)
    (path / "README").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=path, check=True)
    return path


def _committed_root(repo):
    """The sandbox itself as a committed repository the runner can hang task trees off."""
    for args in (["init", "-q"], ["config", "user.email", "t@example.invalid"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / ".gitignore").write_text(".orchestrator/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
    return repo


def test_an_empty_answer_in_an_unchanged_worktree_is_empty_output(
    run_lane, monkeypatch, adoptions, tmp_path
):
    _patch_fixture_cli_driver(monkeypatch, _FakeCliDriver(
        Completion(text="", returncode=0, timed_out=False), ExitVerdict(kind="ok"),
    ))
    worktree = _committed_repo(tmp_path / "wt")
    _, first, result = _settled_attempt(run_lane, "f3_empty", worktree=worktree)

    _assert_failure(first, result, "empty_output")
    assert adoptions == []


def test_a_clean_answer_is_adopted_and_records_no_failure(run_lane, monkeypatch, adoptions):
    _patch_fixture_cli_driver(monkeypatch, _FakeCliDriver(
        Completion(text="done", returncode=0, timed_out=False), ExitVerdict(kind="ok"),
        reports_done=True,
    ))
    _, first, result = _settled_attempt(run_lane, "f3_clean")

    assert result.outputs.get("failure") is None
    assert len(adoptions) == 1, "the workspace is adopted through the node handler"
    assert (first["failure_kind"], first["failure_class"], first["failure_json"]) == (
        None, None, None)


def test_the_recorded_failure_reaches_explain_and_the_failures_listing(
    run_lane, monkeypatch, capsys
):
    _patch_fixture_cli_driver(monkeypatch, _FakeCliDriver(
        Completion(text="", returncode=1, timed_out=False),
        ExitVerdict(kind="weird", evidence="odd exit"),
    ))
    run, first, _ = _settled_attempt(run_lane, "f3_read_path")

    explained = status.explain_view(status.workflow_view("TASK-001", repo=run.repo), repo=run.repo)
    failures = {
        attempt["attempt_id"]: attempt["failure"]
        for chain in explained["chains"] for link in chain["links"]
        for attempt in link["attempts"]
    }
    shown = failures[first["attempt_id"]]
    assert (shown["kind"], shown["class"]) == ("unknown", first["failure_class"])
    assert "odd exit" in shown["evidence"]

    maestro.state._reset_control_store()
    capsys.readouterr()
    assert cli.cmd_workflow(run.repo, "failures", None, as_json=True) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [(r["attempt_id"], r["failure_kind"]) for r in listed] == [
        (first["attempt_id"], "unknown")]
    assert [r["attempt_id"] for r in status.list_failures(None, kind="unknown", repo=run.repo)] == [
        first["attempt_id"]]
    assert status.list_failures(None, kind="timeout", repo=run.repo) == []


def test_an_unrouted_controller_writes_no_failure_columns(tmp_path):
    from maestro.workflows.handlers import TmuxDispatcher

    repo = tmp_path / "repo"
    (repo / ".orchestrator").mkdir(parents=True)
    store = ControlStore.open_controller(repo / ".orchestrator" / "control.sqlite3")
    try:
        revision = compile_workflow(
            normalize_spec(_task("T-unrouted")),
            workflow_id="wf_T-unrouted", code_base_sha="deadbeef",
        )
        graph_runner = wf_runner.start_run(
            store, revision, repo=repo, dispatcher=TmuxDispatcher(runner=_FakeTmux()),
            artifacts=ArtifactStore(repo / ".orchestrator" / "artifacts", store),
        )
        assert graph_runner.routing is None
        claim = graph_runner.claim(revision.node("implementer"))
        inbox = handlers.AttemptInbox(handlers.attempts_root(repo), claim.attempt_id)
        inbox.write_result(NodeResult(
            run_id=graph_runner.run_id, node_id="implementer", attempt_id=claim.attempt_id,
            status="failed", exit_status=1, detail="timeout: slow",
            outputs={"failure": {"kind": "timeout", "evidence": "slow"}},
        ))
        inbox.write_receipt(operation_id=claim.operation_id, exit_status=1)
        graph_runner.ingest_results()

        row = store.conn.execute(
            f"SELECT status, {FAILURE_COLUMNS} FROM attempts WHERE attempt_id = ?",
            (claim.attempt_id,),
        ).fetchone()
        assert row["status"] not in ("claimed", "running"), "the result was ingested"
        assert (row["failure_kind"], row["failure_class"], row["failure_json"]) == (
            None, None, None)
        # P12B F4: without routing a blocked result is not requeued — it fails as today.
        assert row["status"] == "failed"
        state = store.conn.execute(
            "SELECT phase, blocked_attempts FROM node_states WHERE node_id = 'implementer'"
        ).fetchone()
        assert (state["phase"], state["blocked_attempts"]) == ("failed", 0)
        assert store.conn.execute("SELECT COUNT(*) FROM pool_pauses").fetchone()[0] == 0
    finally:
        store.close()


# ── P12B F4: a blocked attempt pauses its pool and routes again ──────────────────────

ROUTE_A = ("fixture_cli", "fixture-1")
ROUTE_B = ("fixture_cli_b", "fixture-1")


def _two_route_routing():
    return fixture_routing(preferred_pool="fixture-pool", entries=[
        fixture_cli_entry(),
        fixture_cli_entry(backend_id="fixture_cli_b", usage_pool_id="fixture-pool-b"),
    ])


def _patch_two_drivers(monkeypatch, first, second) -> None:
    """`fixture_cli` → driver `fake_a` (`first`); `fixture_cli_b` → `fake_b` (`second`)."""
    from maestro.backends import registry

    names = {"fixture_cli": "fake_a", "fixture_cli_b": "fake_b"}
    drivers = {"fake_a": first, "fake_b": second}
    original_driver_name = registry.driver_name_for_backend_id

    def driver_name_for_backend_id(backend_id):
        try:
            return names[backend_id]
        except KeyError:
            # Keep real registry identities valid when the controller validates a frozen
            # shipped policy; only the two synthetic worker routes use fake drivers.
            return original_driver_name(backend_id)

    monkeypatch.setattr(registry, "driver_name_for_backend_id", driver_name_for_backend_id)
    monkeypatch.setattr(registry, "get_backend", lambda name, *a, **kw: drivers[name])


def _patch_verdict_kinds(monkeypatch, *kinds) -> None:
    """Let a fake driver's verdict name a failure kind no real CLI verdict maps to."""
    from maestro.workflows import failures as wf_failures

    real = wf_failures.classify_exit

    def classify_exit(verdict):
        if verdict.kind in kinds:
            return wf_failures.FailureRecord(kind=verdict.kind, evidence=verdict.evidence)
        return real(verdict)

    monkeypatch.setattr(wf_failures, "classify_exit", classify_exit)


def _attempts_by_rowid() -> list:
    return maestro.state.control_store().conn.execute(
        "SELECT attempt_id, status, backend_id, model_id, failure_kind FROM attempts "
        "WHERE node_id = 'implementer' ORDER BY rowid"
    ).fetchall()


def _run_implementer_workers(settled_limit: int = 2):
    """`on_poll`: run the newest live implementer attempt; halt once `settled_limit` settled."""
    def on_poll(run) -> None:
        attempts = _attempts_by_rowid()
        settled = [a for a in attempts if a["status"] not in ("claimed", "running")]
        if len(settled) >= settled_limit or _node_phase("implementer") in ("succeeded", "failed"):
            run.halt()
            return
        live = [a for a in attempts if a["status"] in ("claimed", "running")]
        if not live:
            return
        attempt_id = live[-1]["attempt_id"]
        inbox = handlers.AttemptInbox(handlers.attempts_root(run.repo), attempt_id)
        if inbox.result() is None:
            wf_worker.main(_worker_argv(_window_for(run, attempt_id)))

    return on_poll


def _counters(node_id: str = "implementer") -> dict:
    return dict(maestro.state.control_store().conn.execute(
        "SELECT blocked_attempts, quality_failures, retry_resets, excluded_routes_json,"
        " first_blocked_at FROM node_states WHERE node_id = ?", (node_id,)
    ).fetchone())


def _pool_pauses() -> list:
    return [dict(r) for r in maestro.state.control_store().conn.execute(
        "SELECT * FROM pool_pauses ORDER BY rowid")]


@pytest.fixture
def ladder_spies(monkeypatch):
    """Implementer resolutions, launches, and the implementer phase each tick starts from."""
    from maestro.backends import router

    calls = {"resolve": [], "launch": [], "phases": []}
    real_resolve = router.resolve_agent
    real_launch = wf_runner.WorkflowRunner.launch_worker
    real_tick = wf_runner.WorkflowRunner.tick

    def resolve_agent(node, *args, **kwargs):
        if node.node_id == "implementer":
            calls["resolve"].append(kwargs.get("exclude"))
        return real_resolve(node, *args, **kwargs)

    def launch_worker(self, node, claim, *, at):
        calls["launch"].append((node.node_id, claim.attempt_id))
        return real_launch(self, node, claim, at=at)

    def tick(self, *args, **kwargs):
        calls["phases"].append(_node_phase("implementer"))
        return real_tick(self, *args, **kwargs)

    monkeypatch.setattr(router, "resolve_agent", resolve_agent)
    monkeypatch.setattr(wf_runner.WorkflowRunner, "launch_worker", launch_worker)
    monkeypatch.setattr(wf_runner.WorkflowRunner, "tick", tick)
    return calls


def _reset_in(hours: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


_DONE = _FakeCliDriver(
    Completion(text="done", returncode=0, timed_out=False), ExitVerdict(kind="ok"),
    reports_done=True,
)


def test_a_quota_exhausted_route_pauses_its_pool_and_the_next_poll_routes_elsewhere(
    run_lane, monkeypatch, ladder_spies
):
    from maestro import quota

    reset = _reset_in(5)
    _patch_two_drivers(monkeypatch, _FakeCliDriver(
        Completion(text="", returncode=1, timed_out=False),
        ExitVerdict(kind="quota_exhausted", reset_at=reset, evidence="weekly quota spent"),
    ), _DONE)
    run = run_lane(_graph_scenario(
        "f4_quota_reroute", routing=_two_route_routing, on_poll=_run_implementer_workers(),
    ))
    assert run.returncode == 0

    first, second = _attempts_by_rowid()
    assert (first["backend_id"], first["model_id"]) == ROUTE_A
    assert (first["status"], first["failure_kind"]) == ("failed", "quota_exhausted")
    assert (second["backend_id"], second["model_id"]) == ROUTE_B
    # What matters is that the rerouted attempt ran and recorded no failure.
    assert second["status"] == "succeeded" and second["failure_kind"] is None

    counters = _counters()
    assert (counters["blocked_attempts"], counters["quality_failures"]) == (1, 0)
    assert counters["first_blocked_at"] and counters["excluded_routes_json"] is None
    [pause] = _pool_pauses()
    assert (pause["pool_id"], pause["paused_until"], pause["reset_known"]) == (
        "fixture-pool", reset, 1)
    assert pause["attempt_id"] == first["attempt_id"]
    assert "failed" not in ladder_spies["phases"], "no tick ever saw the implementer failed"
    assert len(ladder_spies["resolve"]) == 2, "one resolution per implementer attempt"
    implementer_launches = [a for n, a in ladder_spies["launch"] if n == "implementer"]
    assert implementer_launches == [first["attempt_id"], second["attempt_id"]]
    assert len(ladder_spies["launch"]) == len(set(ladder_spies["launch"]))
    assert _journal_details("pool_paused") == [f"fixture-pool until {reset} (quota_exhausted)"]

    # graph → legacy: the legacy loop now skips the driver too.
    assert "fake_a" in quota.exhausted_backends()

    # Restart: a fresh store and a fresh routing re-learn the pause from the table.
    maestro.state._reset_control_store()
    fresh = _two_route_routing()
    assert all(pool.paused_until is None for pool in fresh.resources.pools.values())
    fresh.refresh_pauses(maestro.state.control_store(), 1_000_000.0)
    from datetime import datetime, timezone

    until = int(datetime.strptime(reset, "%Y-%m-%dT%H:%M:%SZ")
                .replace(tzinfo=timezone.utc).timestamp())
    assert fresh.resources.pools["fixture-pool"].paused_until == until
    assert "fixture-pool-b" not in fresh.resources.pools


def test_a_reset_crossing_overrides_a_stale_cli_reset_time(run_lane, monkeypatch):
    """07 pilot: account telemetry rolled over while the CLI still said yesterday's time."""
    from datetime import datetime, timezone
    from maestro import quota

    now = int(datetime.now(timezone.utc).timestamp())
    opening = Usage(
        windows={300: WindowUsage(used_pct=100, resets_at=now - 10),
                 10080: WindowUsage(used_pct=52, resets_at=now + 86400)},
        updated_at=_zulu(now - 20), source="codex_app_server",
    )
    closing = Usage(
        windows={300: WindowUsage(used_pct=0, resets_at=now + 18000),
                 10080: WindowUsage(used_pct=52, resets_at=now + 86400)},
        updated_at=_zulu(now), source="codex_app_server",
    )
    # This lane exercises a reset between attempt start and settle. The separate
    # pace poll supplies only the weekly ranking window, without an exhausted 5h
    # observation preventing the attempt from starting in the first place.
    pace = Usage(windows={10080: opening.windows[10080]}, updated_at=_zulu(now))
    samples = iter((pace, opening, closing))

    def routing():
        dispatch = fixture_routing()
        dispatch.usage_reader = lambda binding: next(samples, closing)
        return dispatch

    _patch_fixture_cli_driver(monkeypatch, _FakeCliDriver(
        Completion(text="", returncode=1, timed_out=False),
        ExitVerdict(kind="quota_exhausted", reset_at=_zulu(now + 86400),
                    evidence="try again at 9:38 PM"),
    ))
    run = run_lane(_graph_scenario(
        "f4_reset_crossing", routing=routing,
        on_poll=_run_implementer_workers(settled_limit=1),
    ))
    assert run.returncode == 0
    [attempt] = _attempts_by_rowid()
    row = maestro.state.control_store().conn.execute(
        "SELECT reset_crossing FROM attempts WHERE attempt_id = ?", (attempt["attempt_id"],)
    ).fetchone()
    assert row["reset_crossing"] == 1
    [pause] = _pool_pauses()
    assert pause["reset_known"] == 0
    assert pause["paused_until"] < _zulu(now + 300)
    assert quota.exhausted_backend_resets().get("fixture_cli") is None


def test_a_restarted_controller_recovers_only_the_measured_available_pool(
    run_lane, monkeypatch
):
    """A stale durable pause must release its ready node without a quality retry."""
    from datetime import datetime, timedelta, timezone
    from maestro import quota

    now = datetime.now(timezone.utc)
    stamp = lambda value: value.strftime("%Y-%m-%dT%H:%M:%SZ")
    future = stamp(now + timedelta(hours=12))
    recorded = stamp(now - timedelta(minutes=2))
    sample_time = stamp(now)
    available = Usage(
        windows={300: WindowUsage(used_pct=39, resets_at=int(now.timestamp()) + 18000),
                 10080: WindowUsage(used_pct=58, resets_at=int(now.timestamp()) + 86400)},
        updated_at=sample_time, source="codex_app_server",
    )
    exhausted = Usage(
        windows={300: WindowUsage(used_pct=20, resets_at=int(now.timestamp()) + 18000),
                 10080: WindowUsage(used_pct=100, resets_at=int(now.timestamp()) + 86400)},
        updated_at=sample_time, source="claude_oauth_usage_api",
    )
    _patch_two_drivers(monkeypatch, _DONE, _DONE)

    def before_run(run):
        _create_control_db(run)
        run.orchestrator.time._now = now.timestamp()
        store = maestro.state.control_store()
        for pool, attempt in (("fixture-pool", "old-a"),
                              ("fixture-pool-b", "old-b")):
            store.record_pool_pause({
                "pool_id": pool, "paused_until": future, "reset_known": 1,
                "reason": "quota_exhausted", "failure_kind": "quota_exhausted",
                "attempt_id": attempt, "recorded_at": recorded,
            })

    def routing():
        dispatch = _two_route_routing()
        dispatch.pause_usage_reader = lambda driver: {
            "fake_a": available, "fake_b": exhausted,
        }[driver]
        return dispatch

    run = run_lane(_graph_scenario(
        "f4_durable_pause_recovery", before_run=before_run, routing=routing,
        state={"backend_exhausted": {"fake_a": future, "fake_b": future}},
        on_poll=_run_implementer_workers(settled_limit=1),
    ))
    assert run.returncode == 0
    [attempt] = _attempts_by_rowid()
    assert attempt["backend_id"] == ROUTE_A[0]
    assert _counters()["quality_failures"] == 0
    store = maestro.state.control_store()
    assert [row["pool_id"] for row in store.active_pool_pauses(sample_time)] == [
        "fixture-pool-b"]
    assert store.conn.execute(
        "SELECT COUNT(*) FROM pool_pause_recoveries WHERE pool_id = 'fixture-pool'"
    ).fetchone()[0] == 1
    assert quota.exhausted_backend_resets() == {"fake_b": future}

    maestro.state._reset_control_store()
    fresh = routing()
    fresh.refresh_pauses(maestro.state.control_store(), now.timestamp())
    assert fresh.resources.pools["fixture-pool"].paused_until is None
    assert fresh.resources.pools["fixture-pool-b"].paused_until is not None


def test_a_recovery_marker_finishes_clearing_the_legacy_mirror_after_restart(
    run_lane, monkeypatch
):
    """A crash after recording recovery cannot rehydrate the stale mirror forever."""
    from datetime import datetime, timedelta, timezone
    from maestro import quota

    _patch_two_drivers(monkeypatch, _DONE, _DONE)
    now = datetime.now(timezone.utc)
    stamp = lambda value: value.strftime("%Y-%m-%dT%H:%M:%SZ")
    reset = stamp(now + timedelta(hours=10))

    def before_run(run):
        _create_control_db(run)
        run.orchestrator.time._now = now.timestamp()
        store = maestro.state.control_store()
        pause = {
            "pool_id": "fixture-pool", "paused_until": reset, "reset_known": 1,
            "reason": "quota_exhausted", "failure_kind": "quota_exhausted",
            "attempt_id": "old-a", "recorded_at": stamp(now - timedelta(minutes=5)),
        }
        store.record_pool_pause(pause)
        store.record_pool_recovery(
            pause, recovered_at=stamp(now - timedelta(minutes=1)),
            usage_source="codex_app_server", usage_json="{}",
        )

    run = run_lane(_graph_scenario(
        "f4_crash_after_recovery_marker", before_run=before_run,
        routing=_two_route_routing,
        state={"backend_exhausted": {"fake_a": reset}},
        on_poll=_run_implementer_workers(settled_limit=1),
    ))
    assert run.returncode == 0
    [attempt] = _attempts_by_rowid()
    assert attempt["backend_id"] == ROUTE_A[0]
    assert quota.exhausted_backend_resets() == {}


def test_a_context_overflow_excludes_the_route_not_the_pool(run_lane, monkeypatch, ladder_spies):
    _patch_verdict_kinds(monkeypatch, "context_overflow")
    _patch_two_drivers(monkeypatch, _FakeCliDriver(
        Completion(text="", returncode=1, timed_out=False),
        ExitVerdict(kind="context_overflow", evidence="prompt is too long"),
    ), _DONE)
    run = run_lane(_graph_scenario(
        "f4_context_overflow", routing=_two_route_routing, on_poll=_run_implementer_workers(),
    ))
    assert run.returncode == 0

    first, second = _attempts_by_rowid()
    assert first["failure_kind"] == "context_overflow"
    assert (second["backend_id"], second["model_id"]) == ROUTE_B
    counters = _counters()
    assert json.loads(counters["excluded_routes_json"]) == [list(ROUTE_A)]
    assert counters["blocked_attempts"] == 1
    assert _pool_pauses() == []
    assert ladder_spies["resolve"] == [frozenset(), frozenset({ROUTE_A})]
    others = maestro.state.control_store().conn.execute(
        "SELECT COUNT(*) FROM node_states WHERE node_id != 'implementer'"
        " AND excluded_routes_json IS NOT NULL"
    ).fetchone()[0]
    assert others == 0, "the exclusion is the implementer's alone"


def test_an_auth_failure_pauses_for_the_configured_time_and_notifies_once(
    run_lane, monkeypatch, ladder_spies
):
    from datetime import datetime

    _patch_verdict_kinds(monkeypatch, "auth_failed")
    _patch_two_drivers(monkeypatch, _FakeCliDriver(
        Completion(text="", returncode=1, timed_out=False),
        ExitVerdict(kind="auth_failed", evidence="401 invalid credentials"),
    ), _DONE)
    notes: list = []

    def before_run(run) -> None:
        _create_control_db(run)
        monkeypatch.setattr(run.orchestrator, "notify_telegram", notes.append)

    run = run_lane(_graph_scenario(
        "f4_auth_failed", routing=_two_route_routing, before_run=before_run,
        project_yaml={"engineering": {"runner": "graph", "auth_failure_pause_sec": 7200}},
        on_poll=_run_implementer_workers(),
    ))
    assert run.returncode == 0

    [pause] = _pool_pauses()
    assert (pause["failure_kind"], pause["reset_known"]) == ("auth_failed", 1)
    parse = lambda iso: datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()  # noqa: E731
    assert abs(parse(pause["paused_until"]) - parse(pause["recorded_at"]) - 7200) <= 2
    assert len(_journal_details("auth_failed_pause")) == 1
    # The run's own "▶ Starting" line is not about the pool.
    pauses = [n for n in notes if "paused pool" in n]
    assert len(pauses) == 1 and "fixture-pool" in pauses[0]
    _, second = _attempts_by_rowid()
    assert second["backend_id"] == "fixture_cli_b"


def test_blocked_timing_config_is_strict():
    from maestro import config

    assert config.auth_failure_pause_sec({}) == 3600
    assert config.max_blocked_wait_sec({"engineering": {}}) == 86400
    assert config.auth_failure_pause_sec({"engineering": {"auth_failure_pause_sec": 60}}) == 60
    for bad in (0, -1, True, "3600", 1.5):
        with pytest.raises(config.ConfigError, match="positive integer"):
            config.max_blocked_wait_sec({"engineering": {"max_blocked_wait_sec": bad}})


def test_a_bad_blocked_timing_value_stops_the_graph_loop(run_lane):
    run = run_lane(_graph_scenario(
        "f4_bad_config",
        project_yaml={"engineering": {"runner": "graph", "auth_failure_pause_sec": 0}},
    ))
    assert run.returncode == 1
    assert any("auth_failure_pause_sec" in d for d in _journal_details("runner_config_error"))



def test_a_legacy_backend_exhaustion_pauses_every_pool_its_driver_serves(monkeypatch):
    from maestro import quota

    _patch_two_drivers(monkeypatch, _DONE, _DONE)
    reset = _reset_in(3)
    monkeypatch.setattr(quota, "exhausted_backend_resets", lambda: {"fake_b": reset, "gone": reset})
    routing = _two_route_routing()
    routing.resources.pool("fixture-pool-b").pause(until=10**12, reason="longer")

    class _NoPauses:
        def active_pool_pauses(self, now_iso):
            return []

    routing.refresh_pauses(_NoPauses(), 1_000_000.0)
    assert routing.resources.pools["fixture-pool"].paused_until is None
    assert routing.resources.pools["fixture-pool-b"].paused_until == 10**12, "never shortened"

    routing = _two_route_routing()
    routing.refresh_pauses(_NoPauses(), 1_000_000.0)
    pool = routing.resources.pools["fixture-pool-b"]
    assert pool.admit(now_epoch=pool.paused_until - 1).reason == "pool_paused"


# ── P12B F5: no equal-or-better route → wait, or step down with checks ───────────────

STRONG = ("fixture_cli", "fixture-1")
BALANCED = ("fixture_cli_b", "fixture-1")
_DAY = 86400


def _strong_floor(policy):
    """`policy` with the implementer role demanding `strong` reasoning."""
    definitions = dict(policy.agent_definitions)
    role = definitions["implementer"]
    definitions["implementer"] = dataclasses.replace(
        role, permitted_backend_capabilities={
            **role.permitted_backend_capabilities, "min_reasoning_strength": "strong",
        },
    )
    return dataclasses.replace(policy, agent_definitions=definitions)


def _strong_and_balanced(policy=None, *, billing="subscription"):
    """One `strong` route on `fixture-pool`, one `balanced` route on `fixture-pool-b`;
    the implementer demands `strong`."""
    from maestro.backends import catalog as cat

    policy = _strong_floor(policy if policy is not None else default_policy())
    return fixture_routing(policy=policy, entries=[
        fixture_cli_entry(
            billing_category=billing,
            model_profiles={'fixture-1': cat.ModelProfile('fixture-1')},
            pair_profiles=cat.migrate_pairs(('fixture-1', 'strong', 1.0)),
            pairs_migrated=True,
        ),
        fixture_cli_entry(
            backend_id="fixture_cli_b", usage_pool_id="fixture-pool-b", billing_category=billing,
        ),
    ])


def _zulu(epoch: float) -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _seed_strong_pause(*, until_epoch: float, reset_known: int, then=None):
    """`before_run`: the control database with `fixture-pool` paused until `until_epoch`."""
    def seed(run) -> None:
        store = ControlStore.open_controller(maestro.state.CONTROL_DB)
        try:
            store.record_pool_pause({
                "pool_id": "fixture-pool", "paused_until": _zulu(until_epoch),
                "reset_known": reset_known, "reason": "quota_exhausted: seeded",
                "failure_kind": "quota_exhausted", "attempt_id": "att-seeded",
                "recorded_at": "2026-09-17T00:00:00Z",
            })
        finally:
            store.close()
        if then is not None:
            then(run)

    return seed


def _halt_after(polls: int, each=None):
    seen = []

    def on_poll(run) -> None:
        seen.append(1)
        if each is not None:
            each(run)
        if len(seen) >= polls:
            run.halt()

    return on_poll


def _high_risk(**fields):
    # `risk` does not reach the manifest; the role floor in `_strong_and_balanced` is what
    # forces `strong`. Kept high so the task reads as what it models.
    return [_task("TASK-001", risk="high", **fields)]


def _implementer_rows() -> list:
    return maestro.state.control_store().conn.execute(
        "SELECT attempt_id, backend_id, model_id, stepped_down, binding_json FROM attempts"
        " WHERE node_id = 'implementer' ORDER BY rowid"
    ).fetchall()


@pytest.fixture
def resolve_spy(monkeypatch):
    """Implementer calls through `routing.resolve` (the runner's one call) and the router."""
    from maestro.backends import router

    calls = {"routing": [], "router": [], "launch": []}
    real_resolve, real_agent = wf_routing.resolve, router.resolve_agent
    real_launch = wf_runner.WorkflowRunner.launch_worker

    def resolve(routing, node, manifest, **kwargs):
        result = real_resolve(routing, node, manifest, **kwargs)
        if node.node_id == "implementer":
            calls["routing"].append(result)
        return result

    def resolve_agent(node, *args, **kwargs):
        if node.node_id == "implementer":
            calls["router"].append(kwargs.get("strength_offset", 0))
        return real_agent(node, *args, **kwargs)

    def launch_worker(self, node, claim, *, at):
        calls["launch"].append(node.node_id)
        return real_launch(self, node, claim, at=at)

    monkeypatch.setattr(wf_routing, "resolve", resolve)
    monkeypatch.setattr(router, "resolve_agent", resolve_agent)
    monkeypatch.setattr(wf_runner.WorkflowRunner, "launch_worker", launch_worker)
    return calls


def test_without_the_flag_a_known_reset_is_waited_for_however_far_away(
    run_lane, monkeypatch, resolve_spy
):
    import time
    from types import SimpleNamespace

    started = time.time()
    reset = started + 3 * _DAY
    shifted = SimpleNamespace(
        time=lambda: time.time() + 2 * _DAY, strftime=time.strftime, gmtime=time.gmtime,
    )
    monkeypatch.setattr(wf_runner, "time", shifted)
    run = run_lane(_graph_scenario(
        "f5_wait_known_reset", tasks=_high_risk(), routing=_strong_and_balanced,
        before_run=_seed_strong_pause(until_epoch=reset, reset_known=1),
        on_poll=_halt_after(3),
    ))
    assert run.returncode == 0

    assert _implementer_rows() == [], "two days in, a reset three days out is still waited for"
    why = maestro.state.read_state()["admission_waiting"]["TASK-001"]
    assert why["reason"] == "route_pool_paused"
    assert f"resets at {_zulu(reset)}" in why["detail"]
    # One day left, less only the real time the run took (a fixed 5 s missed under load)
    # and two whole-second truncations: `_zulu`'s paused_until and the stored int(retry).
    elapsed = time.time() - started
    assert _DAY - elapsed - 2 < why["retry_after_sec"] <= _DAY
    assert all(r.reset_known is True for r in resolve_spy["routing"])
    assert set(resolve_spy["router"]) == {0}, "no flag, no step-down"
    assert _journal_details("fallback_refused") == []


def test_an_unknown_reset_parks_the_node_once_the_blocked_wait_is_exceeded(
    run_lane, resolve_spy
):
    import time

    def age_the_first_block(run) -> None:
        # What an earlier blocked requeue leaves behind, two days ago: the node ready again.
        store = maestro.state.control_store()
        [run_id] = [r["run_id"] for r in store.conn.execute("SELECT run_id FROM task_runs")]
        with store.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO node_states (run_id, node_id, phase, updated_at,"
                " blocked_attempts, first_blocked_at) VALUES (?, 'implementer', 'ready', ?, 1, ?)",
                (run_id, _zulu(time.time()), _zulu(time.time() - 2 * _DAY)),
            )

    run = run_lane(_graph_scenario(
        "f5_unknown_reset_parks", tasks=_high_risk(), routing=_strong_and_balanced,
        before_run=_seed_strong_pause(until_epoch=time.time() + 600, reset_known=0),
        on_poll=_halt_after(4, each=age_the_first_block),
    ))
    assert run.returncode == 0

    assert resolve_spy["routing"][0].reset_known is False
    assert _node_phase("implementer") == "failed"
    [attempt] = maestro.state.control_store().conn.execute(
        "SELECT status, route_refusal FROM attempts WHERE node_id = 'implementer'"
    ).fetchall()
    assert attempt["status"] == "failed"
    assert "route_blocked_wait_exceeded" in (attempt["route_refusal"] or "")
    assert resolve_spy["launch"].count("implementer") == 0
    # P12B F7: the graph loop compiles escalated, so the implementer's `on_failure` edge
    # opens onto `diagnose` rather than straight onto `park`. Here `diagnose` is left
    # waiting on the same paused pool, which is why neither sink has a phase yet.
    assert _node_phase("park") is None
    [row] = maestro.state.control_store().conn.execute(
        "SELECT dag_json FROM workflow_revisions"
    ).fetchall()
    failure_edges = {
        (e["source_node_id"], e["target_node_id"])
        for e in json.loads(row["dag_json"])["edges"]
        if e.get("guard_predicate") == "on_failure"
    }
    assert ("implementer", "diagnose") in failure_edges
    assert ("diagnose", "park") in failure_edges


def test_step_down_binds_the_weaker_route_and_the_gate_still_runs_the_full_suite(
    run_lane, monkeypatch, resolve_spy
):
    import time

    from maestro import gates

    from .test_dispatch_wiring import AFFECTED, _run_the_gate_worker, _seed_comparisons

    gate_calls: list = []
    real_gate = gates.run_authoritative_gate

    def run_authoritative_gate(task_id, worktree, **kwargs):
        gate_calls.append(kwargs.get("check_ids"))
        return real_gate(task_id, worktree, **kwargs)

    monkeypatch.setattr(gates, "run_authoritative_gate", run_authoritative_gate)
    routing = _strong_and_balanced()
    checks = [{"id": "unit", "kind": "auto", "cmd": "true"},
              {"id": "lint", "kind": "auto", "cmd": "false"}]
    run = run_lane(_graph_scenario(
        "f5_step_down", routing=lambda: routing,
        tasks=_high_risk(fallback="step_down", verifications=checks),
        project_yaml=AFFECTED,
        before_run=_seed_strong_pause(
            until_epoch=time.time() + 3 * _DAY, reset_known=1, then=_seed_comparisons(20),
        ),
        on_poll=_halt_once_the_gate_has_failed(_run_the_gate_worker),
    ))
    assert run.returncode == 0
    assert routing.affected_mode == "authoritative"
    assert routing.step_down_tasks == {"TASK-001"}
    assert _journal_details("fallback_refused") == []

    [row] = _implementer_rows()
    assert (row["backend_id"], row["model_id"]) == BALANCED
    assert row["stepped_down"] == 1
    assert json.loads(row["binding_json"])["stepped_down"] is True
    # F5 test 6: still one resolution per attempt through the choke point, one launch.
    # The F6 retry of the cone resolves once more and waits — nothing ever steps down twice.
    assert resolve_spy["routing"][0].stepped_down
    assert not any(getattr(r, "stepped_down", False) for r in resolve_spy["routing"][1:])
    assert resolve_spy["router"][:2] == [0, -1], "the step-down is inside that one call"
    assert resolve_spy["launch"].count("implementer") == 1

    [gate] = [a for a in _attempts_by_node("gate")]
    argv = _worker_argv(_window_for(run, gate["attempt_id"]))
    selection_id = argv[argv.index("--affected-selection-artifact-id") + 1]
    stored = json.loads(ArtifactStore(run.repo / ".orchestrator" / "artifacts",
                                      maestro.state.control_store()).read(selection_id))
    assert (stored["mode"], stored["full_suite"], stored["expansion_reason"]) == (
        "shadow", True, "stepped_down")
    assert gate_calls == [("unit", "lint")]
    [gate_attempt] = _attempts_by_node("gate")
    assert gate_attempt["status"] == "failed"
    # P12B F6: the gate's verdict is on the implementer's work, so the failure re-opens
    # the cone rather than ending the run. What F5 asserts is the binding, not the ending.
    assert _node_phase("gate") == "pending"
    assert _node_phase("accept") != "succeeded", "a failing gate is never accepted"


def _halt_once_the_gate_has_failed(each):
    """`on_poll`: `each`, then halt as soon as a gate attempt has settled `failed`.

    P12B F6 resets a failed gate's cone to `pending`, so "the gate reached a terminal
    phase" is no longer a stopping condition for a lane that only cares about the first
    verdict.
    """
    def on_poll(run) -> None:
        settled = [a for a in _attempts_by_node("gate") if a["status"] == "failed"]
        if settled:
            run.halt()
            return
        each(run)

    return on_poll


def _attempts_by_node(node_id: str) -> list:
    return maestro.state.control_store().conn.execute(
        "SELECT attempt_id, status FROM attempts WHERE node_id = ? ORDER BY rowid", (node_id,)
    ).fetchall()


def test_missing_verifications_refuses_before_step_down_or_admission(
    run_lane, resolve_spy
):
    import time

    routing = _strong_and_balanced()
    run = run_lane(_graph_scenario(
        "f5_fallback_refused", routing=lambda: routing,
        tasks=_high_risk(fallback="step_down", verifications=[]),
        before_run=_seed_strong_pause(until_epoch=time.time() + _DAY, reset_known=1),
        on_poll=_halt_after(2),
    ))
    assert run.returncode == 0

    [refusal] = _journal_details("pre_validation_refused")
    assert "TASK-001" in refusal and "no required verification" in refusal
    state = maestro.state.read_state()
    [fallback] = _journal_details("fallback_refused")
    assert "verifications" in fallback
    assert routing.step_down_tasks == set()
    assert _implementer_rows() == []
    assert "TASK-001" not in state.get("admission_waiting", {})
    assert resolve_spy["router"] == []


def test_a_free_step_down_never_binds_a_paid_route(run_lane, resolve_spy):
    import dataclasses
    import time

    from maestro.workflows.policy import HybridPolicy

    policy = dataclasses.replace(
        default_policy(), objective="free_quality",
        hybrid=HybridPolicy(paid_roles=("implementer",), billing_categories=("subscription",)),
    )
    routing = _strong_and_balanced(policy)
    run = run_lane(_graph_scenario(
        "f5_free_step_down", routing=lambda: routing,
        tasks=_high_risk(fallback="step_down", verifications=[
            {"id": "unit", "kind": "auto", "cmd": "true"}]),
        before_run=_seed_strong_pause(until_epoch=time.time() + _DAY, reset_known=1),
        on_poll=_halt_after(2),
    ))
    assert run.returncode == 0

    assert routing.step_down_tasks == {"TASK-001"}
    assert resolve_spy["router"][:2] == [0, -1], "the step-down was tried"
    assert _implementer_rows() == [], "the hybrid grant never reaches the step-down"
    assert all(not r.is_bound and r.disposition == "wait" for r in resolve_spy["routing"])


# ── P12B F6: a bad output is retried twice, fresh and then on another model ──────────
#
# These are the lane-level proofs of the behaviour committed in `782ea77`: a real
# controller, a real worker, a scripted `agent_cli` driver, and the ladder read back off
# the attempt rows and the node counters.
#
# Every halt condition below is a *durable* one — an attempt count, a row that exists —
# and never "the implementer reached a terminal phase". F6 resets a failed writer's whole
# cone to `pending`, so a lane watching the phase would spin to `MAX_POLLS` (12) instead
# of stopping.

#: An answer that classifies `empty_output`: nothing said, clean exit, worktree untouched.
#: `_classify_cli` needs the worktree to be readable, so these lanes pass `--worktree`.
_EMPTY = (Completion(text="", returncode=0, timed_out=False), ExitVerdict(kind="ok"))
_GOOD = (Completion(text="done", returncode=0, timed_out=False), ExitVerdict(kind="ok"))
_ONE_CHECK = [{"id": "unit", "kind": "auto", "cmd": "true"}]


class _ScriptedCliDriver:
    """A fake `agent_cli` driver with one `(completion, verdict)` per call, last repeating.

    The ladder is a sequence, so the double has to be one too: the same node dispatched
    three times must be able to answer badly, badly, then well.
    """

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0
        self._current = self.answers[0]

    def complete(self, spec):
        self._current = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        return self._current[0]

    def parse_exit(self, rc, tail):
        return self._current[1]


def _quota(reset_at: str):
    return (Completion(text="", returncode=1, timed_out=False),
            ExitVerdict(kind="quota_exhausted", reset_at=reset_at, evidence="weekly quota spent"))


def _retry_rows() -> list:
    return maestro.state.control_store().conn.execute(
        "SELECT attempt_id, status, backend_id, model_id, session_mode, start_sha,"
        " failure_kind, binding_json FROM attempts WHERE node_id = 'implementer' ORDER BY rowid"
    ).fetchall()


def _settled_implementer() -> list:
    return [r for r in _retry_rows() if r["status"] not in ("claimed", "running")]


def _halt_when(predicate, each):
    """`on_poll`: halt as soon as `predicate()` holds, otherwise do `each(run)`."""
    def on_poll(run) -> None:
        if predicate():
            run.halt()
            return
        each(run)

    return on_poll


def _drive_implementer(predicate, *, worktree=None, succeed_from=None):
    """`on_poll`: run the newest live implementer worker until `predicate()` holds.

    `succeed_from` is the 1-based attempt from which the workspace is given a `DONE`
    sentinel before the worker runs, which is how an adopted agent answer becomes
    `succeeded` rather than `uncertain`.
    """
    def each(run) -> None:
        attempts = _retry_rows()
        live = [a for a in attempts if a["status"] in ("claimed", "running")]
        if not live:
            return
        attempt_id = live[-1]["attempt_id"]
        inbox = handlers.AttemptInbox(handlers.attempts_root(run.repo), attempt_id)
        if inbox.result() is not None:
            return
        if succeed_from is not None and len(attempts) >= succeed_from:
            handlers.write_sentinel(inbox.ensure(), handlers.DONE_SENTINEL, "done")
        argv = _worker_argv(_window_for(run, attempt_id))
        if worktree is not None:
            argv += ["--worktree", str(worktree)]
        wf_worker.main(argv)

    return _halt_when(predicate, each)


def _two_bad_then_good(run_lane, monkeypatch, tmp_path, lane: str, **overrides):
    """The canonical F6 lane: one route, two `empty_output` answers, then a good one."""
    _patch_fixture_cli_driver(monkeypatch, _ScriptedCliDriver(_EMPTY, _EMPTY, _GOOD))
    worktree = _committed_repo(tmp_path / "wt")
    run = run_lane(_graph_scenario(
        lane, tasks=[_task("TASK-001", verifications=_ONE_CHECK)],
        on_poll=_drive_implementer(
            lambda: bool(_attempts_by_node("gate")), worktree=worktree, succeed_from=3,
        ),
        **overrides,
    ))
    assert run.returncode == 0
    return run


def test_two_bad_outputs_are_retried_and_the_third_answer_reaches_the_gate(
    run_lane, monkeypatch, tmp_path
):
    _two_bad_then_good(run_lane, monkeypatch, tmp_path, "f6_two_retries")

    rows = _retry_rows()
    assert [r["failure_kind"] for r in rows] == ["empty_output", "empty_output", None]
    assert [r["status"] for r in rows] == ["failed", "failed", "succeeded"]
    assert _node_phase("implementer") == "succeeded"
    assert _attempts_by_node("gate"), "the third answer is what the gate is asked about"

    counters = _counters()
    assert (counters["quality_failures"], counters["retry_resets"]) == (2, 2)
    # Retry 1 is the same route run fresh: a single bad answer is as often a bad sample.
    assert (rows[1]["backend_id"], rows[1]["model_id"]) == (
        rows[0]["backend_id"], rows[0]["model_id"])
    assert rows[1]["session_mode"] == "fresh"


def test_with_one_route_the_second_retry_reuses_it_and_the_binding_says_so(
    run_lane, monkeypatch, tmp_path
):
    _two_bad_then_good(run_lane, monkeypatch, tmp_path, "f6_retry_two_reuse")

    rows = _retry_rows()
    assert (rows[2]["backend_id"], rows[2]["model_id"]) == (
        rows[0]["backend_id"], rows[0]["model_id"])
    assert "retry 2: no other equal-or-better route" in json.loads(
        rows[2]["binding_json"])["selection_reason"]
    assert _counters()["excluded_routes_json"] is None, "a preference is never persisted"


def test_the_second_retry_prefers_another_equal_route_when_there_is_one(
    run_lane, monkeypatch, tmp_path
):
    _patch_two_drivers(monkeypatch, _ScriptedCliDriver(_EMPTY), _ScriptedCliDriver(_GOOD))
    worktree = _committed_repo(tmp_path / "wt")
    run = run_lane(_graph_scenario(
        "f6_retry_two_other_route", routing=_two_route_routing,
        on_poll=_drive_implementer(
            lambda: len(_settled_implementer()) >= 3, worktree=worktree, succeed_from=3,
        ),
    ))
    assert run.returncode == 0

    rows = _retry_rows()
    assert [(r["backend_id"], r["model_id"]) for r in rows] == [ROUTE_A, ROUTE_A, ROUTE_B]
    assert "retry 2: no other equal-or-better route" not in json.loads(
        rows[2]["binding_json"])["selection_reason"]
    assert _counters()["quality_failures"] == 2


def test_a_failing_gate_re_runs_the_writer_and_then_the_check(run_lane):
    from .test_dispatch_wiring import _run_the_gate_worker

    run = run_lane(_graph_scenario(
        "f6_gate_cone",
        tasks=[_task("TASK-001", verifications=[{"id": "lint", "kind": "auto", "cmd": "false"}])],
        on_poll=_halt_when(
            lambda: len(_attempts_by_node("gate")) >= 2, _run_the_gate_worker,
        ),
    ))
    assert run.returncode == 0

    # The gate's verdict is on the implementer's work, so the cone re-runs both — and the
    # gate is never re-run alone against the same tree.
    assert len(_attempts_by_node("implementer")) == 2
    assert len(_attempts_by_node("gate")) == 2
    assert [a["status"] for a in _attempts_by_node("gate")][0] == "failed"
    # One retry for the pair, counted on the writer that the ladder will escalate.
    writer, gate = _counters(), _counters("gate")
    assert (writer["quality_failures"], writer["retry_resets"]) == (1, 1)
    assert (gate["quality_failures"], gate["retry_resets"]) == (0, 1)


def test_a_third_bad_output_fails_the_writer_and_takes_the_park_edge(
    run_lane, monkeypatch, tmp_path
):
    _patch_fixture_cli_driver(monkeypatch, _ScriptedCliDriver(_EMPTY))
    worktree = _committed_repo(tmp_path / "wt")
    run = run_lane(_graph_scenario(
        "f6_park", on_poll=_drive_implementer(
            lambda: _node_phase("park") is not None, worktree=worktree,
        ),
    ))
    assert run.returncode == 0

    rows = _retry_rows()
    assert len(rows) == 3, "the first run plus two retries, and no more"
    assert [r["failure_kind"] for r in rows] == ["empty_output"] * 3
    assert _node_phase("implementer") == "failed"
    assert _node_phase("park") is not None
    # The third failure is counted too, so F7's repaired run inherits an exhausted node.
    assert (_counters()["quality_failures"], _counters()["retry_resets"]) == (3, 2)


def _head(tree) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tree, capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_a_retry_starts_from_the_sha_its_predecessor_claimed_at(run_lane, monkeypatch):
    """The writer's own worktree is reset to the retry's start SHA; nothing else is."""
    _patch_fixture_cli_driver(monkeypatch, _ScriptedCliDriver(_EMPTY))

    def writer_tree(repo):
        return repo / ".orchestrator" / "worktrees" / "TASK-001"

    def before_run(run) -> None:
        _create_control_db(run)
        # A real repository at the root, with a file `git clean -fdq` would remove: if the
        # restore ever pointed here instead of at the worktree, it would be gone. The
        # runner hangs the task's worktree off it at claim (`task_tree.ensure`).
        _committed_root(run.repo)
        (run.repo / "root-junk").write_text("keep me\n", encoding="utf-8")

    observed: dict = {}

    def on_poll(run) -> None:
        tree = writer_tree(run.repo)
        rows = _retry_rows()
        if len(rows) >= 2 and rows[1]["start_sha"]:
            observed["head"] = _head(tree)
            observed["junk"] = (tree / "junk.txt").exists()
            run.halt()
            return
        live = [a for a in rows if a["status"] in ("claimed", "running")]
        if not live:
            return
        attempt_id = live[-1]["attempt_id"]
        inbox = handlers.AttemptInbox(handlers.attempts_root(run.repo), attempt_id)
        if inbox.result() is not None:
            return
        wf_worker.main(_worker_argv(_window_for(run, attempt_id)))
        # What a bad attempt leaves behind: half-finished work the retry must not inherit.
        (tree / "junk.txt").write_text("half-done\n", encoding="utf-8")
        subprocess.run(["git", "add", "junk.txt"], cwd=tree, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "junk"], cwd=tree, check=True)

    run = run_lane(_graph_scenario("f6_start_sha", before_run=before_run, on_poll=on_poll))
    assert run.returncode == 0

    rows = _retry_rows()
    assert rows[0]["start_sha"], "a writer's claim records where its tree stood"
    assert rows[1]["start_sha"] == rows[0]["start_sha"]
    assert observed == {"head": rows[0]["start_sha"], "junk": False}
    assert (run.repo / "root-junk").exists(), "the restore never reaches the repository root"


def test_a_blocked_failure_between_two_bad_outputs_spends_no_retry(
    run_lane, monkeypatch, tmp_path
):
    _patch_two_drivers(
        monkeypatch,
        _ScriptedCliDriver(_EMPTY, _quota(_reset_in(5))),
        _ScriptedCliDriver(_EMPTY, _GOOD),
    )
    worktree = _committed_repo(tmp_path / "wt")
    run = run_lane(_graph_scenario(
        "f6_blocked_between", routing=_two_route_routing,
        on_poll=_drive_implementer(
            lambda: len(_settled_implementer()) >= 3, worktree=worktree,
        ),
    ))
    assert run.returncode == 0

    rows = _retry_rows()
    assert [r["failure_kind"] for r in rows[:3]] == [
        "empty_output", "quota_exhausted", "empty_output"]
    counters = _counters()
    # Three attempts, two of them bad output: the blocked one bought a reroute, not a retry.
    assert (counters["retry_resets"], counters["quality_failures"]) == (2, 2)
    assert counters["blocked_attempts"] == 1


def test_the_retry_ladder_resolves_once_and_launches_once_per_attempt(
    run_lane, monkeypatch, tmp_path, resolve_spy
):
    _two_bad_then_good(run_lane, monkeypatch, tmp_path, "f6_choke_point")

    rows = _retry_rows()
    assert len({r["attempt_id"] for r in rows}) == 3, "three distinct attempts"
    assert resolve_spy["launch"].count("implementer") == 3, "one launch per attempt"
    assert len(resolve_spy["routing"]) == 3, "one `routing.resolve` per implementer attempt"
    assert all(r.is_bound for r in resolve_spy["routing"]), "every retry bound a route"


def test_an_unrouted_controller_fails_a_bad_output_without_retrying_it(tmp_path):
    """Test 8: new behaviour only where routing is. Without it, `failed` as today."""
    from maestro.workflows.handlers import TmuxDispatcher

    repo = tmp_path / "repo"
    (repo / ".orchestrator").mkdir(parents=True)
    store = ControlStore.open_controller(repo / ".orchestrator" / "control.sqlite3")
    try:
        revision = compile_workflow(
            normalize_spec(_task("T-unrouted-bad")),
            workflow_id="wf_T-unrouted-bad", code_base_sha="deadbeef",
        )
        graph_runner = wf_runner.start_run(
            store, revision, repo=repo, dispatcher=TmuxDispatcher(runner=_FakeTmux()),
            artifacts=ArtifactStore(repo / ".orchestrator" / "artifacts", store),
        )
        assert graph_runner.routing is None
        claim = graph_runner.claim(revision.node("implementer"))
        inbox = handlers.AttemptInbox(handlers.attempts_root(repo), claim.attempt_id)
        inbox.write_result(NodeResult(
            run_id=graph_runner.run_id, node_id="implementer", attempt_id=claim.attempt_id,
            status="failed", exit_status=1, detail="empty_output: nothing said",
            outputs={"failure": {"kind": "empty_output", "evidence": ""}},
        ))
        inbox.write_receipt(operation_id=claim.operation_id, exit_status=1)
        graph_runner.ingest_results()

        row = store.conn.execute(
            "SELECT status FROM attempts WHERE attempt_id = ?", (claim.attempt_id,)
        ).fetchone()
        assert row["status"] == "failed"
        state = store.conn.execute(
            "SELECT phase, quality_failures, retry_resets FROM node_states"
            " WHERE node_id = 'implementer'"
        ).fetchone()
        assert (state["phase"], state["quality_failures"], state["retry_resets"]) == (
            "failed", 0, 0)
    finally:
        store.close()


# ── P12B F7: the third bad output escalates to a stronger diagnoser ───────────────────
#
# The lane is longer than F6's — three bad answers, then `diagnose`, then possibly a
# repaired run — so every halt below is on a durable condition (an attempt row exists, a
# second run exists) and the polls are counted against `MAX_POLLS` (12) before the
# scenario is written. Where the full three-answer ladder does not fit, the lane shortens
# it with `_one_bad_output`: how many retries a bad output buys is F6's subject and F6's
# lanes prove it, and these lanes are about what happens once the retries are spent.

#: The strong route and the balanced one `_escalation_routing` offers. They differ by model
#: id as well as by backend, so a role can be held to one of them by name.
STRONG_MODEL = ("fixture_cli", "fixture-strong")
BALANCED_MODEL = ("fixture_cli_b", "fixture-1")


def _relaxed_diagnoser(policy):
    """`policy` with the diagnoser's own `strong` floor lowered out of the way.

    The shipped role already demands `strong`, so a lane asserting "the diagnoser bound a
    strong model" would pass with F7's floor deleted. Lowering the declared floor here is
    what leaves the escalation as the only thing that can raise it.
    """
    return _with_capabilities(policy, "diagnoser", min_reasoning_strength="light")


def _balanced_implementer(policy):
    """`policy` with the implementer permitted only the balanced route's model.

    F6's second retry prefers another equal-or-better route, which would move the writer
    onto the strong route and leave nothing for F7 to escalate above. Naming the model the
    role may use pins the writer to `balanced` without touching the ladder.
    """
    return _with_capabilities(policy, "implementer", models=("fixture-1",))


def _with_capabilities(policy, role_name: str, **capabilities):
    definitions = dict(policy.agent_definitions)
    role = definitions[role_name]
    definitions[role_name] = dataclasses.replace(
        role,
        permitted_backend_capabilities={
            **role.permitted_backend_capabilities, **capabilities,
        },
    )
    return dataclasses.replace(policy, agent_definitions=definitions)


def _escalation_routing(policy=None, *, billing="subscription", entries=None):
    """A `strong` route and a `balanced` one, with only the writer held to the balanced."""
    from maestro.backends import catalog as cat

    policy = _balanced_implementer(
        _relaxed_diagnoser(policy if policy is not None else default_policy()))
    return fixture_routing(policy=policy, preferred_pool="fixture-pool",
                           entries=entries if entries is not None else [
        fixture_cli_entry(
            billing_category=billing,
            available_models=("fixture-strong",),
            model_profiles={'fixture-strong': cat.ModelProfile('fixture-strong')},
            pair_profiles=cat.migrate_pairs(('fixture-strong', 'strong', 1.0)),
            pairs_migrated=True,
        ),
        fixture_cli_entry(
            backend_id="fixture_cli_b", usage_pool_id="fixture-pool-b",
            billing_category=billing,
        ),
    ])


def _rows_for(node_id: str) -> list:
    return maestro.state.control_store().conn.execute(
        "SELECT attempt_id, status, backend_id, model_id, route_refusal, failure_kind,"
        " binding_json FROM attempts WHERE node_id = ? ORDER BY rowid", (node_id,)
    ).fetchall()


def _bound(row) -> tuple:
    return (row["backend_id"], row["model_id"])


def test_the_third_bad_output_escalates_to_a_model_one_rung_stronger(
    run_lane, monkeypatch, tmp_path
):
    _patch_two_drivers(monkeypatch, _ScriptedCliDriver(_GOOD), _ScriptedCliDriver(_EMPTY))
    worktree = _committed_repo(tmp_path / "wt")
    run = run_lane(_graph_scenario(
        "f7_escalates", routing=_escalation_routing,
        on_poll=_drive_implementer(
            lambda: any(r["backend_id"] for r in _rows_for("diagnose")), worktree=worktree,
        ),
    ))
    assert run.returncode == 0

    implementer = _rows_for("implementer")
    assert len(implementer) == 3, "the first run plus F6's two retries"
    assert {_bound(r) for r in implementer} == {BALANCED_MODEL}
    assert _node_phase("implementer") == "failed"

    [diagnose] = _rows_for("diagnose")
    assert _bound(diagnose) == STRONG_MODEL, \
        "the diagnoser is escalated a rung above the model that failed"
    assert "raised to strong" in json.loads(diagnose["binding_json"])["selection_reason"]

    # And it is handed the controller's own account of the failure it has to explain.
    context = _failure_context(run, diagnose["attempt_id"])
    assert context["failing_node"]["node_id"] == "implementer"
    assert [a["attempt_index"] for a in context["attempts"]] == [1, 2, 3]
    assert [a["failure"]["kind"] for a in context["attempts"]] == ["empty_output"] * 3
    assert {a["binding"]["model_id"] for a in context["attempts"]} == {BALANCED_MODEL[1]}


def _failure_context(run, attempt_id: str) -> dict:
    """The `failure_context` artifact a diagnoser's claim was given."""
    store = maestro.state.control_store()
    [row] = store.conn.execute(
        "SELECT a.artifact_id FROM artifacts a JOIN events e"
        " ON e.aggregate_id = a.artifact_id AND e.type = 'ArtifactCommitted'"
        " WHERE a.type = 'failure_context' AND e.attempt_id = ?", (attempt_id,)
    ).fetchall()
    return json.loads(
        ArtifactStore(run.repo / ".orchestrator" / "artifacts", store).read(row["artifact_id"])
    )


def _free_and_paid(policy=None):
    """A free `balanced` route and a paid `strong` one, under the free objective.

    The only escalation the diagnoser could make is onto paid capacity, which `§3.1`'s
    hybrid overlay grants to its role — and which F7 forbids it to take.
    """
    import dataclasses as _dc

    from maestro.backends import catalog as cat
    from maestro.workflows.policy import HybridPolicy

    policy = _dc.replace(
        policy if policy is not None else default_policy(),
        objective="free_quality",
        hybrid=HybridPolicy(
            paid_roles=("implementer", "diagnoser"), billing_categories=("subscription",)),
    )
    return _escalation_routing(policy, entries=[
        fixture_cli_entry(
            billing_category="subscription",
            available_models=("fixture-strong",),
            model_profiles={'fixture-strong': cat.ModelProfile('fixture-strong')},
            pair_profiles=cat.migrate_pairs(('fixture-strong', 'strong', 1.0)),
            pairs_migrated=True,
        ),
        fixture_cli_entry(
            backend_id="fixture_cli_b", usage_pool_id="fixture-pool-b",
            billing_category="free_tier_api",
        ),
    ])


def test_a_diagnoser_with_no_stronger_free_route_parks_rather_than_spend(
    run_lane, monkeypatch, tmp_path
):
    """The hybrid overlay grants the diagnoser paid capacity; F7 never takes it."""
    _patch_two_drivers(monkeypatch, _ScriptedCliDriver(_GOOD), _ScriptedCliDriver(_EMPTY))
    worktree = _committed_repo(tmp_path / "wt")
    run = run_lane(_graph_scenario(
        "f7_free_no_escalation", routing=_free_and_paid,
        on_poll=_drive_implementer(
            lambda: _node_phase("park") is not None, worktree=worktree,
        ),
    ))
    assert run.returncode == 0

    assert {_bound(r) for r in _rows_for("implementer")} == {BALANCED_MODEL}
    [diagnose] = _rows_for("diagnose")
    assert diagnose["status"] == "failed"
    assert diagnose["route_refusal"].startswith("route_"), diagnose["route_refusal"]
    assert _bound(diagnose) == (None, None), "a refused diagnoser binds nothing"
    assert _node_phase("diagnose") == "failed"
    assert _node_phase("park") is not None
    # The grant is real and still unused: nothing in the run ever bound paid capacity.
    assert all(r["backend_id"] != "fixture_cli" for r in _rows_for("diagnose"))


def test_the_run_does_not_settle_while_the_diagnoser_is_live(
    run_lane, monkeypatch, tmp_path
):
    """Every other node is terminal — `failed` or `skipped` — and the run stays open."""
    _patch_two_drivers(monkeypatch, _ScriptedCliDriver(_GOOD), _ScriptedCliDriver(_EMPTY))
    worktree = _committed_repo(tmp_path / "wt")
    run = run_lane(_graph_scenario(
        "f7_no_settle", routing=_escalation_routing,
        on_poll=_drive_implementer(
            lambda: any(r["backend_id"] for r in _rows_for("diagnose")), worktree=worktree,
        ),
    ))
    assert run.returncode == 0

    [diagnose] = _rows_for("diagnose")
    assert diagnose["status"] in ("claimed", "running"), "the diagnosis is still out"
    assert _node_phase("implementer") == "failed"
    [task_run] = maestro.state.control_store().conn.execute(
        "SELECT status FROM task_runs"
    ).fetchall()
    assert task_run["status"] == "running", "a live diagnoser holds the run open"


def _one_bad_output(monkeypatch) -> None:
    """Spend the whole quality budget on the first bad answer.

    How many retries a bad output buys is F6's subject and F6's nine lanes prove it; the
    lanes below are about what happens once the budget is gone, and a three-answer ladder
    plus a diagnosis plus a repaired run does not fit in `MAX_POLLS`. The carried-failure
    arithmetic is unchanged by the smaller number: the repaired run still inherits a node
    whose budget is spent, which is the thing under test.
    """
    from maestro.workflows import failures as wf_failures

    monkeypatch.setattr(wf_failures, "QUALITY_ATTEMPTS", 1)


def _live_diagnose_rows() -> list:
    return maestro.state.control_store().conn.execute(
        "SELECT a.attempt_id, a.run_id, r.workflow_id FROM attempts a"
        " JOIN task_runs r ON a.run_id = r.run_id"
        " WHERE a.status IN ('claimed', 'running') AND a.node_id = 'diagnose'"
        " ORDER BY a.rowid"
    ).fetchall()


def _answer_the_diagnoser(build, halt_when, *, worktree):
    """`on_poll`: drive the writer, then answer whatever diagnosis the loop asks for.

    `build(revision)` returns the `plan_repair` payload for the live `diagnose` attempt, or
    `None` for a diagnoser that answers with nothing at all. No diagnoser worker is run —
    the result is written into the live attempt's inbox exactly as P12A's W7 planner lane
    writes a planner's, which is what keeps the lane about the controller's decision.
    """
    from maestro.workflows.compiler import latest_revision

    answered: list = []

    def on_poll(run) -> None:
        if halt_when():
            run.halt()
            return
        store = maestro.state.control_store()
        for row in _live_diagnose_rows():
            if row["attempt_id"] in answered:
                continue
            inbox = handlers.AttemptInbox(handlers.attempts_root(run.repo), row["attempt_id"])
            if inbox.result() is not None:
                continue
            proposal = build(latest_revision(store, row["workflow_id"]))
            inbox.write_result(NodeResult(
                run_id=row["run_id"], node_id="diagnose", attempt_id=row["attempt_id"],
                status="succeeded",
                outputs={} if proposal is None else {"plan_repair": proposal},
            ))
            inbox.write_receipt(operation_id="", exit_status=0)
            answered.append(row["attempt_id"])
            return
        _run_newest_implementer(run, worktree)

    return on_poll


def _run_newest_implementer(run, worktree) -> None:
    """Run the newest live implementer worker of whichever run owns it."""
    live = [a for a in _retry_rows() if a["status"] in ("claimed", "running")]
    if not live:
        return
    attempt_id = live[-1]["attempt_id"]
    inbox = handlers.AttemptInbox(handlers.attempts_root(run.repo), attempt_id)
    if inbox.result() is not None:
        return
    wf_worker.main(
        _worker_argv(_window_for(run, attempt_id)) + ["--worktree", str(worktree)]
    )


def _enhanced_implementer(revision) -> dict:
    """A `poor_agent` proposal: the same node, the same plan, a fuller brief."""
    from maestro.workflows.compiler import PlanRepairProposal

    node = revision.node("implementer")
    return PlanRepairProposal(
        workflow_id=revision.workflow_id, base_revision=revision.revision,
        kind="partial_failure", verdict="poor_agent",
        reason="the plan is sound; the brief did not say what 'done' looks like",
        replace_nodes=(dataclasses.replace(
            node, params={**node.params, "brief_addendum": ENHANCEMENT},
        ),),
        rerun_nodes=("implementer",),
    ).to_dict()


def _graph_patch(revision) -> dict:
    """A `bad_graph` proposal: a prerequisite the plan should have had."""
    from maestro.workflows.compiler import PlanRepairProposal
    from maestro.workflows.models import EdgeSpec, NodeSpec

    prereq = NodeSpec(
        node_id=PREREQ, kind="agent", handler_ref="maestro.handlers.agent",
        agent_definition_ref="implementer", permissions_ref="frozen_snapshot_reader",
        resource_request={"writer_lease": False, "inference_slots": 1},
        params={"task_id": "TASK-001", "title": "build the missing fixture"},
    )
    return PlanRepairProposal(
        workflow_id=revision.workflow_id, base_revision=revision.revision,
        kind="discovered_prerequisite", verdict="bad_graph",
        reason="the checks cannot pass until the fixture exists",
        add_nodes=(prereq,),
        add_edges=(
            EdgeSpec(source_node_id="implementer", target_node_id=PREREQ,
                     guard_predicate="on_success"),
            EdgeSpec(source_node_id=PREREQ, target_node_id="gate",
                     guard_predicate="on_success"),
        ),
        remove_edges=((("implementer", "gate")),),
        rerun_nodes=("implementer",),
    ).to_dict()


#: What a `poor_agent` diagnosis adds to the brief of the node that failed.
ENHANCEMENT = "state the acceptance criteria before writing code"
PREREQ = "build_fixture"


def _repair_lane(run_lane, monkeypatch, tmp_path, lane: str, build):
    """The F7 tail: one bad answer, a diagnosis, the repaired run, its one bad answer."""
    _one_bad_output(monkeypatch)
    _patch_two_drivers(monkeypatch, _ScriptedCliDriver(_GOOD), _ScriptedCliDriver(_EMPTY))
    worktree = _committed_repo(tmp_path / "wt")
    launched: list = []
    real_launch = wf_runner.WorkflowRunner.launch_worker

    def launch_worker(self, node, claim, *, at):
        launched.append(node.node_id)
        return real_launch(self, node, claim, at=at)

    monkeypatch.setattr(wf_runner.WorkflowRunner, "launch_worker", launch_worker)
    run = run_lane(_graph_scenario(
        lane, routing=_escalation_routing,
        on_poll=_answer_the_diagnoser(
            build, lambda: _node_phase("park") is not None, worktree=worktree,
        ),
    ))
    assert run.returncode == 0
    return run, launched


def _repaired_revision():
    from maestro.workflows.compiler import latest_revision

    return latest_revision(maestro.state.control_store(), "wf_TASK-001")


def test_a_poor_agent_diagnosis_re_briefs_the_node_and_its_next_answer_parks(
    run_lane, monkeypatch, tmp_path
):
    _, launched = _repair_lane(
        run_lane, monkeypatch, tmp_path, "f7_poor_agent", _enhanced_implementer)

    old, new = _runs_with_revisions()
    assert (old["status"], old["workflow_revision"]) == ("canceled", 1)
    assert new["workflow_revision"] == 2
    revision = _repaired_revision()
    assert revision.node("implementer").params["brief_addendum"] == ENHANCEMENT, \
        "revision N+1's writer carries the enhanced brief"

    # The repaired run's one answer is bad too: no retry, and no second diagnosis.
    assert launched.count("diagnose") == 1, "the escalation is spent, not repeated"
    assert launched.count("implementer") == 2, "one writer launch per revision"
    assert _node_phase("implementer") == "failed"
    assert _node_phase("park") is not None
    [refused] = [r for r in _rows_for("diagnose") if r["route_refusal"]]
    assert "repair_budget_exhausted" in refused["route_refusal"]


def test_a_bad_graph_diagnosis_patches_the_plan_and_its_next_answer_parks(
    run_lane, monkeypatch, tmp_path
):
    _, launched = _repair_lane(
        run_lane, monkeypatch, tmp_path, "f7_bad_graph", _graph_patch)

    old, new = _runs_with_revisions()
    assert (old["workflow_revision"], new["workflow_revision"]) == (1, 2)
    revision = _repaired_revision()
    assert PREREQ in revision.node_ids, "revision N+1 carries the added prerequisite"
    assert ("implementer", "gate") not in [
        (e.source_node_id, e.target_node_id) for e in revision.edges
    ], "and the edge it was spliced into is gone"

    assert launched.count("diagnose") == 1
    assert _node_phase("park") is not None


def test_a_diagnoser_that_proposes_nothing_parks_the_task(run_lane, monkeypatch, tmp_path):
    """The escalation has already been paid for; an answer nobody can act on ends it."""
    _, launched = _repair_lane(
        run_lane, monkeypatch, tmp_path, "f7_no_proposal", lambda revision: None)

    assert len(_runs_with_revisions()) == 1, "nothing was repaired"
    assert launched.count("diagnose") == 1
    [diagnose] = _rows_for("diagnose")
    # The worker exited cleanly and the node is still `failed`: what a diagnoser owes is a
    # reading the controller can act on, and an empty answer is not one.
    assert diagnose["status"] == "failed"
    assert _node_phase("diagnose") == "failed"
    assert _counters("diagnose")["retry_resets"] == 0, "and a diagnoser is never retried"
    assert _node_phase("park") is not None
    [refused] = _events("WorkflowRepairRefused")
    assert json.loads(refused["payload"])["reason"] == "diagnoser returned no proposal"


def test_a_repair_refused_after_the_fact_still_fails_the_diagnoser_and_parks(
    run_lane, monkeypatch, tmp_path
):
    """P12A's `apply_repair` limit, closed: the node moves `succeeded → failed`.

    `apply_repair` refuses for reasons the controller cannot stage in one poll — a writer
    that outlived its revocation, phases that moved between drawing the plan and committing
    it. The refusal itself is the compiler's own unit territory; what this lane proves is
    that the run reaches a decision afterwards instead of standing on a repair that was
    never applied.
    """
    from maestro.workflows import compiler as wf_compiler

    def apply_repair(*args, **kwargs):
        raise wf_compiler.StaleRepairProposal("node phase(s) moved since the plan was drawn")

    monkeypatch.setattr(wf_compiler, "apply_repair", apply_repair)
    _, launched = _repair_lane(
        run_lane, monkeypatch, tmp_path, "f7_repair_refused", _enhanced_implementer)

    assert len(_runs_with_revisions()) == 1, "the repair was refused, so no revision N+1"
    assert launched.count("diagnose") == 1
    assert _node_phase("diagnose") == "failed", "succeeded, then overridden to failed"
    assert _node_phase("park") is not None
    [refused] = _events("WorkflowRepairRefused")
    assert "moved since the plan was drawn" in json.loads(refused["payload"])["reason"]
    [diagnose] = _rows_for("diagnose")
    assert diagnose["failure_kind"] == "garbage_output"


# ── P12B F7 (job 0): the failure context reaches the model ────────────────────────────
#
# F7's lanes above answer the diagnoser from outside its worker, which is right for the
# controller's decision and blind to what the model is told. These two look at the text
# itself: the lane runs a real diagnoser worker against a real gate verdict and reads the
# prompt its driver was handed; the unit checks the bound on verification output.


class _RecordingCliDriver(_ScriptedCliDriver):
    """A scripted driver that keeps every `CompletionSpec` it was handed."""

    def __init__(self, *answers):
        super().__init__(*answers)
        self.specs: list = []

    def complete(self, spec):
        self.specs.append(spec)
        return super().complete(spec)


def test_the_diagnoser_is_told_what_failed_and_what_the_checks_saw(
    run_lane, monkeypatch, tmp_path
):
    from maestro.workflows import failures as wf_failures

    # Two answers buy one retry: the gate's failure spends it, the writer's empty answer
    # then fails it, and `diagnose` is dispatched with a real gate verdict in the run.
    monkeypatch.setattr(wf_failures, "QUALITY_ATTEMPTS", 2)
    diagnoser = _RecordingCliDriver(_GOOD)
    _patch_two_drivers(monkeypatch, diagnoser, _ScriptedCliDriver(_EMPTY))
    worktree = _committed_repo(tmp_path / "wt")

    def each(run) -> None:
        store = maestro.state.control_store()
        for row in store.conn.execute(
            "SELECT attempt_id, run_id, node_id FROM attempts"
            " WHERE status IN ('claimed', 'running') ORDER BY rowid"
        ).fetchall():
            inbox = handlers.AttemptInbox(handlers.attempts_root(run.repo), row["attempt_id"])
            if inbox.result() is not None:
                continue
            argv = _worker_argv(_window_for(run, row["attempt_id"]))
            if row["node_id"] == "implementer" and len(_rows_for("implementer")) == 1:
                # The first answer is good, so the gate has something to judge.
                inbox.write_result(NodeResult(
                    run_id=row["run_id"], node_id="implementer",
                    attempt_id=row["attempt_id"], status="succeeded",
                ))
                inbox.write_receipt(operation_id="", exit_status=0)
            elif row["node_id"] == "implementer":
                wf_worker.main(argv + ["--worktree", str(worktree)])
            else:
                wf_worker.main(argv)            # the gate, and then the diagnoser

    run = run_lane(_graph_scenario(
        "f7_diagnoser_brief", routing=_escalation_routing,
        tasks=[_task("TASK-001", verifications=[{
            "id": "lint", "kind": "auto", "cmd": "echo 'E501 src/app.py:7 line too long'; false",
        }])],
        on_poll=_halt_when(lambda: bool(diagnoser.specs), each),
    ))
    assert run.returncode == 0

    assert [r["status"] for r in _rows_for("gate")] == ["failed"]
    assert [r["failure_kind"] for r in _rows_for("implementer")] == [None, "empty_output"]
    [diagnose] = _rows_for("diagnose")
    assert _bound(diagnose) == STRONG_MODEL

    [spec] = diagnoser.specs
    prompt = spec.prompt
    assert "Failing node: implementer" in prompt
    assert '"kind": "empty_output"' in prompt, "every attempt's failure record"
    assert "check_id=lint, outcome=failed" in prompt, "the verification evidence"
    assert "E501 src/app.py:7 line too long" in prompt, "what the check actually printed"

    # Full vs delivered, durable per diagnosis, so the cap can be tuned against usage.
    sizes = _failure_context(run, diagnose["attempt_id"])["sizes"]
    assert sizes["full_chars"] == sizes["delivered_chars"] > 0, "well under the cap: uncut"
    assert sizes["evidence_cap_tokens"] == 16_000


def test_verification_output_over_the_cap_is_cut_failed_checks_first_head_and_tail():
    from maestro.repository.context import estimate_tokens
    from maestro.workflows.failure_context import render_failure_context

    def row(check_id, outcome, text):
        return {"check_id": check_id, "outcome": outcome, "exit_status": 0,
                "output": [{"check_id": check_id, "stdout": text, "stderr": ""}]}

    long = "HEAD-OF-FAILURE\n" + "x" * 40_000 + "\nTAIL-OF-FAILURE"
    document = {
        "failing_node": {"node_id": "implementer", "params": {"brief": "B" * 5_000}},
        "brief": {"mandatory_instructions": ["M" * 5_000]},
        "attempts": [{"attempt_index": i, "attempt_id": f"a{i}", "status": "failed",
                      "binding": {"model_id": "fixture-1"},
                      "failure": {"kind": "empty_output", "evidence": "E" * 3_000}}
                     for i in (1, 2, 3)],
        # The passing check comes first in the stored order, and is the one that loses.
        "evidence": [row("unit", "passed", "PASSED-OUTPUT " * 2_000),
                     row("lint", "failed", long)],
    }
    cap = 5_000
    text = render_failure_context(document, cap_tokens=cap)
    head, _, evidence = text.partition("## Verification evidence")

    # Everything but the evidence is delivered in full.
    assert "B" * 5_000 in head and "M" * 5_000 in head and head.count("E" * 3_000) == 3
    # The failed check is spent on first, cut in the middle with both ends kept.
    assert evidence.index("check_id=lint") < evidence.index("check_id=unit")
    assert "HEAD-OF-FAILURE" in evidence and "TAIL-OF-FAILURE" in evidence
    assert "[EXCERPT: head and tail only" in evidence
    assert "characters omitted from the middle" in evidence
    # Nothing is left for the passing one, which is named as omitted, not silently lost.
    assert "PASSED-OUTPUT" not in evidence
    assert "check_id=unit, outcome=passed" in evidence and "[OMITTED" in evidence
    assert estimate_tokens(evidence.split("\n", 1)[1]) <= cap
    # And uncut, it all comes back.
    assert "x" * 40_000 in render_failure_context(document, cap_tokens=None)


# ── P12B F8: a writer that died with no receipt still reaches a decision ──────────────
#
# The kill is the real thing the reconciler sees: the worker reports a writing PID that no
# process holds, and never writes a receipt. Nothing launches it again by failing, so no
# failed launch can stand in for the crash.

_DEAD_PID = 4_000_000


def _kill(run, attempt_id: str) -> None:
    handlers.AttemptInbox(handlers.attempts_root(run.repo), attempt_id).bind_pid(_DEAD_PID)


def _kill_first_attempt_of(node_id: str, *, halt_when):
    """`on_poll`: the first attempt of `node_id` dies; every other worker succeeds."""
    from .test_dispatch_wiring import CANDIDATES, _passing_evidence

    killed: list = []

    def on_poll(run) -> None:
        if halt_when():
            run.halt()
            return
        store = maestro.state.control_store()
        for row in store.conn.execute(
            "SELECT attempt_id, run_id, node_id FROM attempts"
            " WHERE status IN ('claimed', 'running') ORDER BY rowid"
        ).fetchall():
            inbox = handlers.AttemptInbox(handlers.attempts_root(run.repo), row["attempt_id"])
            if inbox.result() is not None or row["attempt_id"] in killed:
                continue
            if row["node_id"] == node_id and not killed:
                _kill(run, row["attempt_id"])
                killed.append(row["attempt_id"])
                continue
            outputs: dict = {}
            if row["node_id"] in CANDIDATES:
                outputs = {
                    "head_commit": f"commit-{row['node_id']}",
                    "evidence": [_passing_evidence(row["attempt_id"], c) for c in ("unit", "lint")],
                }
            inbox.write_result(NodeResult(
                run_id=row["run_id"], node_id=row["node_id"], attempt_id=row["attempt_id"],
                status="succeeded", outputs=outputs,
            ))
            inbox.write_receipt(operation_id="", exit_status=0)

    return on_poll


def _launch_spy(monkeypatch) -> list:
    launched: list = []
    real_launch = wf_runner.WorkflowRunner.launch_worker

    def launch_worker(self, node, claim, *, at):
        launched.append(node.node_id)
        return real_launch(self, node, claim, at=at)

    monkeypatch.setattr(wf_runner.WorkflowRunner, "launch_worker", launch_worker)
    return launched


def test_a_killed_candidate_is_abandoned_and_the_join_still_picks_one_winner(
    run_lane, monkeypatch
):
    from maestro.backends import catalog as cat
    from maestro.workflows import candidates as candidate_join

    from .test_dispatch_wiring import CANDIDATES, _three_candidates

    _three_candidates(monkeypatch)
    launched = _launch_spy(monkeypatch)
    selections: list = []
    real_select = candidate_join.select_winner

    def select_winner(outcomes):
        selections.append({o.candidate_id: o for o in outcomes})
        return real_select(outcomes)

    monkeypatch.setattr(candidate_join, "select_winner", select_winner)
    strong = fixture_cli_entry(
        model_profiles={'fixture-1': cat.ModelProfile('fixture-1')},
        pair_profiles=cat.migrate_pairs(('fixture-1', 'strong', 1.0)),
        pairs_migrated=True,
        concurrency_limit=3,
    )
    run = run_lane(_graph_scenario(
        "f8_candidate_killed",
        routing=lambda: fixture_routing(entries=[strong]),
        on_poll=_kill_first_attempt_of(
            "implementer_c3",
            halt_when=lambda: _node_phase("candidate_select") in ("succeeded", "failed"),
        ),
    ))
    assert run.returncode == 0

    assert [n for n in launched if n in CANDIDATES] == list(CANDIDATES), \
        "each candidate launched once: the kill is not a failed launch, and is not retried"
    [c3] = _rows_for("implementer_c3")
    assert c3["status"] == "uncertain", "A05: the attempt's own record stays truthful"
    assert c3["failure_kind"] == "worker_crashed"
    assert "no receipt" in json.loads(
        maestro.state.control_store().conn.execute(
            "SELECT failure_json FROM attempts WHERE attempt_id = ?", (c3["attempt_id"],)
        ).fetchone()["failure_json"])["evidence"]
    assert _node_phase("implementer_c3") == "failed"
    assert _node_phase("candidate_select") == "succeeded"

    [outcomes] = selections
    assert set(outcomes) == {"c1", "c2", "c3"}
    assert outcomes["c3"].status == "abandoned"
    assert sum(outcomes[c].status == "succeeded" for c in ("c1", "c2")) == 2
    [join] = _rows_for("candidate_select")
    result = handlers.AttemptInbox(
        handlers.attempts_root(run.repo), join["attempt_id"]).result()
    assert result.outputs["winner"] in ("c1", "c2"), "exactly one winner"
    assert "c3" in result.outputs["losers"]


def test_a_killed_implementer_is_retried_once_from_its_start_sha(run_lane, monkeypatch):
    launched = _launch_spy(monkeypatch)
    run = run_lane(_graph_scenario(
        "f8_implementer_killed",
        on_poll=_kill_first_attempt_of(
            "implementer", halt_when=lambda: len(_settled_implementer()) >= 2,
        ),
    ))
    assert run.returncode == 0

    first, second = _retry_rows()[:2]
    assert (first["status"], first["failure_kind"]) == ("failed", "worker_crashed")
    assert second["status"] == "succeeded"
    assert second["start_sha"] == first["start_sha"], "the retry starts where the first did"
    assert _counters()["quality_failures"] == 1
    assert launched.count("implementer") == 2


def test_without_routing_a_killed_writer_is_left_uncertain_as_before(tmp_path):
    """The graph loop always routes, so an unrouted run is a directly driven runner."""
    from maestro.workflows.handlers import TmuxDispatcher

    repo = tmp_path / "repo"
    (repo / ".orchestrator").mkdir(parents=True)
    store = ControlStore.open_controller(repo / ".orchestrator" / "control.sqlite3")
    try:
        revision = compile_workflow(
            normalize_spec(_task("T-unrouted-kill")),
            workflow_id="wf_T-unrouted-kill", code_base_sha="deadbeef",
        )
        graph_runner = wf_runner.start_run(
            store, revision, repo=repo, dispatcher=TmuxDispatcher(runner=_FakeTmux()),
            artifacts=ArtifactStore(repo / ".orchestrator" / "artifacts", store),
            is_alive=lambda pid: pid != _DEAD_PID,
        )
        assert graph_runner.routing is None
        graph_runner.tick()             # claims and launches the implementer
        [row] = store.conn.execute("SELECT attempt_id FROM attempts").fetchall()
        handlers.AttemptInbox(
            handlers.attempts_root(repo), row["attempt_id"]).bind_pid(_DEAD_PID)
        graph_runner.tick()             # binds the dead pid
        graph_runner.tick()             # reconciles it: no receipt
        graph_runner.tick()             # and nothing more happens to it

        attempt = store.conn.execute(
            "SELECT status, failure_kind FROM attempts WHERE attempt_id = ?",
            (row["attempt_id"],),
        ).fetchone()
        assert (attempt["status"], attempt["failure_kind"]) == ("uncertain", None)
        state = store.conn.execute(
            "SELECT phase, quality_failures FROM node_states WHERE node_id = 'implementer'"
        ).fetchone()
        assert (state["phase"], state["quality_failures"]) == ("running", 0)
        assert store.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1
    finally:
        store.close()
