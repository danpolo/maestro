"""The P0 lane-capture harness: run the real event loop, record what it did.

`maestro/orchestrator.py::main` is a 670-line `while True` with cyclomatic complexity in
the hundreds, and P0 (`docs/graph-engineering/specs/phases/P00_MIGRATION_BASELINE.md`)
has to characterise its behaviour *before* P1 starts moving authority into the control
store. So this harness does not model the loop, mock it, or re-implement its branches —
it runs `main()` itself and instruments the seams where it reaches outside the process.

Three properties the phase's acceptance condition turns on, and where each is enforced:

* **Isolation.** `lane_sandbox` rebases every `Path`-valued module global of every loaded
  `maestro.*` module into a temp tree, by reflection rather than from a hand-written list,
  then asserts none is left pointing inside a real repo. Same technique and same reason as
  `tests/characterization/conftest.py`: a name missing from a hand-written list silently
  keeps its live path, and the failure mode is writing to the operator's project.

* **Determinism.** Nothing here sleeps, samples a clock, or spawns a process. `time` is a
  counter the harness advances, `subprocess` is a recorder, and every remaining seam is a
  double with a scripted answer. A trace that changes between two runs of the same lane
  would make the baseline worthless, so `test_baseline_lanes.py` compares two runs of each
  lane and fails if their signatures differ.

* **Provenance.** Every fact reaches `BaselineTrace` through `observe` (the harness watched
  it happen at a seam) or `infer` (it was concluded from context). D06 and INV-04 both rest
  on that line, so the recorder refuses a fact without it and `summary()` reports the two
  counts separately. Nothing in this file calls `infer` — the harness watches seams, it does
  not reason — which is itself the point: the baseline is measured, not estimated.

The seam table is `_SEAMS` plus `_install_doubles`. It is deliberately explicit and
deliberately long: each entry names one place the loop leaves the process, and reading it
top to bottom is the shortest honest answer to "what does a poll cycle actually touch?"
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from maestro.metrics import (
    AGENT_CALL,
    STATE_READ,
    STATE_WRITE,
    TEST_RUN,
    BaselineTrace,
)

#: Hard stop. A lane that has not finished by this many poll cycles is a harness bug or a
#: loop that does not terminate; either way the test must fail loudly rather than hang.
MAX_POLLS = 12

#: What the fake clock charges for one poll sleep. Fixed, so `idle_wait_seconds` is a
#: reproducible count of polls rather than a measurement of this machine.
POLL_SECONDS = 30.0

#: What the fake clock charges for one agent turn and one gate/merge run. Also fixed, and
#: also not a measurement — these are the units `active_compute_seconds` is denominated in,
#: chosen so idle-vs-active is legible at a glance in a lane summary.
AGENT_SECONDS = 120.0
CHECK_SECONDS = 20.0


class LoopRanTooLong(BaseException):
    """Raised when a lane exceeds `MAX_POLLS`.

    `BaseException` for the same reason `tests/conftest.py::RealAgentCLIStarted` is: the
    event loop is full of `except Exception` blocks that turn a failure into an ordinary
    "that task didn't work out", and a harness backstop swallowed by the code under test
    is not a backstop.
    """


class HaltRequested(BaseException):
    """Internal: the scenario asked the loop to stop. Caught by `LaneRun.run`."""


# ──────────────────────────────────────────────────────────────────────────────
# Fake clock
# ──────────────────────────────────────────────────────────────────────────────
class _Clock:
    """Stands in for the orchestrator's `time` module.

    `main` uses exactly two things from it — `time()` to stamp launch times and `sleep()`
    to pace the poll loop — so those are what this provides. `sleep` is where a poll cycle
    ends, which makes it the natural place to count cycles, charge idle time, and hand
    control back to the scenario for the next cycle's setup.
    """

    def __init__(self, run: "LaneRun"):
        self._run = run
        self._now = 1_000_000.0

    def time(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += POLL_SECONDS
        self._run.trace.waited(POLL_SECONDS)
        self._run.polls += 1
        if self._run.polls > MAX_POLLS:
            raise LoopRanTooLong(
                f"lane {self._run.lane!r} ran {self._run.polls} poll cycles without "
                f"finishing (limit {MAX_POLLS})"
            )
        self._run.advance()

    def monotonic(self) -> float:
        return self._now


# ──────────────────────────────────────────────────────────────────────────────
# Fake subprocess
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class _CompletedProcess:
    args: object
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""

    def check_returncode(self) -> None:
        if self.returncode:
            raise AssertionError(f"fake subprocess failed: {self.args}")


class _Subprocess:
    """Stands in for the orchestrator's `subprocess` module.

    The loop shells out for three things: the `tmux has-session` liveness probe, git
    plumbing around worktrees and pushes, and — on the script lane only — the task's own
    `run:` command. All three are recorded and none is executed. The script lane in
    particular *needs* the command not to run: its whole subject is what the loop does
    when a script produces no commits, and a real command would make that depend on
    whatever it happened to write.
    """

    #: `main` catches this around `create_worktree`, so it has to be the real class.
    from subprocess import CalledProcessError, TimeoutExpired  # noqa: F401
    DEVNULL = -3
    PIPE = -1

    def __init__(self, run: "LaneRun"):
        self._run = run

    def run(self, args, **kwargs):
        rendered = args if isinstance(args, str) else " ".join(str(part) for part in args)
        self._run.trace.observe("subprocess", rendered[:200], identity=_head(rendered))
        self._run.commands.append(rendered)
        return _CompletedProcess(args, returncode=self._run.returncode_for(rendered))

    def Popen(self, args, **kwargs):  # noqa: N802 — mirrors the stdlib name
        raise AssertionError(
            f"a lane fixture tried to spawn a background process: {args!r}. "
            "Every lane must be reproducible; nothing here may outlive the test."
        )


def _head(rendered: str) -> str:
    """The program and its first argument — enough to tell `git add` from `git commit`."""
    return " ".join(rendered.split()[:2])


# ──────────────────────────────────────────────────────────────────────────────
# Scenario description
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class LaneScenario:
    """One lane's inputs: what the roadmap says, what state says, what the seams answer.

    `on_poll` is how a lane expresses time passing — an implementer that writes `DONE` on
    the second cycle, an async job whose result lands on the third. It is called after
    each poll sleep with the `LaneRun`, so it can plant sentinels, edit state, or ask the
    loop to halt.
    """

    lane: str
    tasks: list = field(default_factory=list)
    state: dict = field(default_factory=dict)
    #: `(cap, five_h_percent)` returned by `get_effective_cap`.
    cap: tuple = (2, 10.0)
    #: `(ok, message)` returned by `run_verification_gate`.
    gate: tuple = (True, "all verifications passed")
    #: `(ok, message)` returned by `sonnet_review_proofs`.
    review: tuple = (True, "proofs accepted")
    #: `(accepted, smoke)` returned by `merge_and_eval`.
    merge: tuple = (True, {"pass": True, "metrics": {}, "reason": ""})
    #: Shell commands whose fake exit code is non-zero, keyed by the leading fragment.
    failing_commands: dict = field(default_factory=dict)
    on_poll: object = None
    #: Written to the sandbox's `project.yaml` before `main()` starts, when set.
    project_yaml: dict | None = None
    #: Called with the `LaneRun` after the sandbox is built and before `main()` starts —
    #: for setup the loop reads at startup, such as creating the control database.
    before_run: object = None
    #: Zero-argument factory for the graph loop's `DispatchRouting` (replaces
    #: `orchestrator._graph_routing`). `None` means `fixture_routing()`: an inert catalog
    #: that binds every agent node, so no lane reaches a real driver, register or repo.
    routing: object = None


# ──────────────────────────────────────────────────────────────────────────────
# One lane execution
# ──────────────────────────────────────────────────────────────────────────────
class LaneRun:
    """Holds the mutable state of a single `main()` execution under instrumentation."""

    def __init__(self, scenario: LaneScenario, orchestrator, repo: Path):
        self.scenario = scenario
        self.lane = scenario.lane
        self.orchestrator = orchestrator
        self.repo = repo
        self.trace = BaselineTrace(lane=scenario.lane)
        self.polls = 0
        self.commands: list = []
        #: Sessions the loop has launched an implementer for, in launch order.
        self.launched: list = []
        self.returncode = 0

    # ── scenario plumbing ──────────────────────────────────────────────────────
    def returncode_for(self, rendered: str) -> int:
        for fragment, code in self.scenario.failing_commands.items():
            if fragment in rendered:
                return int(code)
        return 0

    def advance(self) -> None:
        """Give the scenario the cycle boundary. Raising `HaltRequested` ends the run."""
        if callable(self.scenario.on_poll):
            self.scenario.on_poll(self)

    def halt(self) -> None:
        """Stop the loop the way an operator would — via the HALT sentinel."""
        self.orchestrator.HALT_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.orchestrator.HALT_FILE.write_text("lane fixture complete", encoding="utf-8")

    # ── sentinel helpers a scenario uses to speak for an implementer ────────────
    def workspace(self, session_id: str) -> Path:
        return self.orchestrator.WORKSPACES / session_id

    def finish(self, session_id: str, result: dict | None = None) -> None:
        """Write the `DONE` sentinel and a `result.json`, as an implementer would."""
        workspace = self.workspace(session_id)
        workspace.mkdir(parents=True, exist_ok=True)
        payload = result if result is not None else {
            "summary": "implemented", "tests": {"pass": True}, "verifications": {},
        }
        (workspace / "result.json").write_text(json.dumps(payload), encoding="utf-8")
        (workspace / "DONE").write_text("done", encoding="utf-8")

    def fail(self, session_id: str, reason: str = "implementer failed") -> None:
        workspace = self.workspace(session_id)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "FAILED").write_text(reason, encoding="utf-8")

    def in_flight(self) -> list:
        return self.orchestrator.read_state().get("in_flight", [])

    def journal_events(self) -> list:
        """The kinds recorded in the sandbox journal, in order.

        The journal is the loop's own account of what it did, written by code this harness
        does not touch. Asserting against it rather than against the doubles is what keeps
        a lane test a characterisation of the orchestrator instead of a characterisation
        of the fixture.
        """
        import maestro.state

        path = maestro.state.JOURNAL
        if not path.exists():
            return []
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line).get("event", ""))
            except json.JSONDecodeError:
                continue
        return events

    # ── execution ──────────────────────────────────────────────────────────────
    def run(self) -> "LaneRun":
        try:
            self.returncode = self.orchestrator.main()
        except HaltRequested:
            self.returncode = 0
        return self


# ──────────────────────────────────────────────────────────────────────────────
# Fixture routing (P12A W1/W2)
# ──────────────────────────────────────────────────────────────────────────────
class FixtureQueries:
    """A `RepositoryQueries` stand-in: every snapshot is an exact match, or `error` raises."""

    def __init__(self, snapshot_id: str = "snap_fixture", error: Exception | None = None):
        self.snapshot_id = snapshot_id
        self.error = error
        self.resolved: list = []

    class index:  # noqa: N801 — mirrors `RepositoryQueries.index`
        """A declared `scope:` makes the context builder list the snapshot's files (D20
        lanes declare scopes); the fixture repository has none indexed."""

        @staticmethod
        def list_files(snapshot) -> list:
            return []

    def resolve(self, snapshot, *, on_stale: str = "reject"):
        self.resolved.append(snapshot)
        if self.error is not None:
            raise self.error
        from types import SimpleNamespace

        return SimpleNamespace(snapshot_id=self.snapshot_id), "exact_match", ()


def fixture_cli_entry(**overrides):
    """A subscription CLI backend with observed flag effort (`--effort low|medium|high`)."""
    from maestro.backends import catalog as cat

    body = dict(
        backend_id="fixture_cli",
        runtime_kind="agent_cli",
        installed_version="1.0.0",
        billing_category="subscription",
        usage_pool_id="fixture-pool",
        available_models=("fixture-1",),
        effort_controls=cat.EffortControls(
            supported=True, delivery_mechanism="flag", native_values=("low", "medium", "high")
        ),
        model_profiles={'fixture-1': cat.ModelProfile('fixture-1')},
        pair_profiles=cat.migrate_pairs(('fixture-1', 'balanced', 1.0)),
        pairs_migrated=True,
        concurrency_limit=1,
    )
    body.update(overrides)
    if 'pair_profiles' in overrides and 'pairs_migrated' not in overrides:
        body['pairs_migrated'] = False
    return cat.BackendCapabilityEntry(**body)


def fixture_routing(*, entries=None, policy=None, queries=None, resources=None, preferred_pool=None):
    """An inert `DispatchRouting`: fixture catalog, packaged policy, fixture queries."""
    from maestro.backends import catalog as cat
    from maestro.docs.roadmap import get_task_by_id
    from maestro.taskgraph import normalize_spec
    from maestro.workflows.policy import default_policy
    from maestro.workflows.routing import DispatchRouting

    dispatch = DispatchRouting(
        catalog=cat.CapabilityCatalog.of(list(entries) if entries is not None else [fixture_cli_entry()]),
        policy=policy if policy is not None else default_policy(),
        queries=queries if queries is not None else FixtureQueries(),
        task_spec=lambda task_id: normalize_spec(get_task_by_id(task_id)),
        resources=resources if resources is not None else cat.ResourcePoolState(),
    )
    if preferred_pool:
        from datetime import datetime, timezone
        # Runner admission uses wall time. Pace makes scripted A-then-B failure
        # lanes independent of neutral hashes and independently scaled pool costs.
        at = datetime.now(timezone.utc).timestamp()
        dispatch.resources.pool(preferred_pool).observation = cat.QuotaObservation(
            preferred_pool, {10080: cat.QuotaWindow(10080, 40, at + 3.5 * 86400)},
            observed_at=datetime.fromtimestamp(at, timezone.utc).isoformat())
    return dispatch


# ──────────────────────────────────────────────────────────────────────────────
# Seam table
# ──────────────────────────────────────────────────────────────────────────────
#: Seams replaced by a double that records the call and returns a fixed, inert value.
#: `(attribute, trace kind, return value)`. Everything here is a notification, a
#: report-rendering side trip, or a reconciler that talks to tmux — none of it changes
#: what the loop decides, and all of it would reach outside the process for real.
_SEAMS = (
    ("notify_telegram", "notify", None),
    ("notify_telegram_with_map", "notify", None),
    ("phase_report", "notify", None),
    ("_danreq", "dan_request", 0),
    ("_send_hitl_reminders", "notify", None),
    ("poll_control_commands", "control_poll", None),
    ("poll_proposal_answer", "control_poll", None),
    ("maybe_push_roadmap_map_change", "notify", None),
    ("run_dep_map", "report", None),
    ("run_status", "report", None),
    ("apply_ready_self_fixes", "selfheal", None),
    ("apply_ready_redo", "selfheal", None),
    ("generate_proposals", "notify", None),
    ("_notify_proposal_test_due", "notify", None),
    ("_idle_heartbeat_maybe", "heartbeat", None),
    ("_self_update_maybe", "self_update", None),
    ("maybe_self_update", "self_update", None),
    ("run_canary_deploy", "deploy", True),
    ("_touches_bot_files", "repo_query", False),
    ("create_worktree", "worktree_create", None),
    ("remove_worktree", "worktree_remove", None),
    ("_merge_data_only", "merge_partial", "ok"),
    ("_write_usage_sample", "usage_sample", None),
    ("_under_usage_pressure", "usage_sample", False),
    ("_context_rotations", "context_rotation", 0),
    ("_threshold_switches", "quota_switch", 0),
    ("launch_async_job", "async_job_launch", True),
    ("poll_awaiting_verifications", "async_job_poll", None),
)


def _patch_everywhere(monkeypatch, name: str, value) -> int:
    """Replace `name` on every loaded `maestro.*` module that has it.

    `maestro.orchestrator` does `from maestro.hitl.telegram import notify_telegram`, so it
    holds its own reference — but so do `maestro.parking` and `maestro.merge`, and the
    loop calls into those on the completion and parking lanes. Patching only the
    orchestrator's copy leaves the others live, and "live" here means a real Bot API call.
    So every holder of the name is patched, and the count is returned so the caller can
    assert that at least one was found rather than silently patching nothing.
    """
    patched = 0
    for module_name, module in sorted(sys.modules.items()):
        if module is None:
            continue
        if module_name != "maestro" and not module_name.startswith("maestro."):
            continue
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value)
            patched += 1
    return patched


def _install_doubles(monkeypatch, run: LaneRun) -> None:
    """Replace every seam the event loop uses to leave this process."""
    orchestrator = run.orchestrator
    trace = run.trace
    scenario = run.scenario

    # ── the two stdlib modules the loop reaches the outside world through ──
    monkeypatch.setattr(orchestrator, "time", _Clock(run))
    monkeypatch.setattr(orchestrator, "subprocess", _Subprocess(run))

    # ── inert, recorded seams ──
    for name, kind, result in _SEAMS:
        def double(*args, _kind=kind, _name=name, _result=result, **kwargs):
            trace.observe(_kind, _name, identity=_name)
            return _result
        assert _patch_everywhere(monkeypatch, name, double), (
            f"seam {name!r} is in the table but no loaded maestro module defines it — "
            "the table has drifted from the code it is supposed to cover."
        )

    # ── every one-shot agent question, wherever it is asked from ──
    #
    # Not every agent call in a lane comes from `launch_implementer`. `park_failed`
    # routes through the self-heal diagnoser, which asks a real `claude` — the session-wide
    # guard in `tests/conftest.py` caught exactly that on the script and risky lanes. The
    # call is instrumented rather than merely blocked, because a diagnosis the loop pays
    # for on a lane nobody thinks of as an agent lane is precisely the kind of cost P0
    # exists to surface.
    from maestro.agentcall import Completion

    def ask(role, prompt, **kwargs):
        trace.observe(AGENT_CALL, f"ask {role}", identity=f"ask:{role}")
        trace.computed(AGENT_SECONDS)
        return Completion(text="", returncode=1)

    _patch_everywhere(monkeypatch, "ask", ask)
    import maestro.agentcall

    monkeypatch.setattr(maestro.agentcall, "ask", ask)

    # ── telegram, one level below the seams above, so nothing slips past ──
    for name in ("_tg_api", "notify_telegram", "notify_telegram_with_map"):
        _patch_everywhere(monkeypatch, name, lambda *a, **k: None)
    import requests

    def _refuse_post(*args, **kwargs):
        raise AssertionError(f"a lane fixture reached the network: {args[:1]}")

    monkeypatch.setattr(requests, "post", _refuse_post)
    monkeypatch.setattr(requests, "get", _refuse_post)

    # ── state authority: real reads and writes against the sandbox, counted ──
    real_read = orchestrator.read_state
    real_write = orchestrator.write_state

    def read_state():
        trace.observe(STATE_READ, "state.json")
        return real_read()

    def write_state(state):
        trace.observe(STATE_WRITE, "state.json")
        return real_write(state)

    _patch_everywhere(monkeypatch, "read_state", read_state)
    _patch_everywhere(monkeypatch, "write_state", write_state)

    # ── reconciliation: reads live tmux windows, so it is a seam, not logic ──
    def reconcile_in_flight(state, launch_times):
        trace.observe("reconcile", "in_flight", identity="reconcile")
        return list(state.get("in_flight", []))

    def startup_reconcile(state, launch_times):
        trace.observe("reconcile", "startup", identity="reconcile_startup")
        return list(state.get("in_flight", []))

    monkeypatch.setattr(orchestrator, "reconcile_in_flight", reconcile_in_flight)
    monkeypatch.setattr(orchestrator, "_reconcile_and_persist_startup_state", startup_reconcile)
    monkeypatch.setattr(orchestrator.metabranch, "reconcile_meta", lambda repo: "fixture")

    # ── graph routing: a fixture catalog, never the host's register or drivers ──
    factory = scenario.routing if callable(scenario.routing) else fixture_routing
    monkeypatch.setattr(orchestrator, "_graph_routing", factory)

    # ── scripted answers: the decisions a lane exists to vary ──
    monkeypatch.setattr(orchestrator, "get_effective_cap", lambda: scenario.cap)

    def launch_implementer(task, sid, workspace, worktree, retry_note="", backend=""):
        """The agent call. Identity is task+role, which is what makes a repeat a repeat.

        `retry_note` is accepted and ignored on purpose: a re-brief is the *same* work
        being paid for a second time, so it must share the first call's identity or the
        duplicate-invocation count would read zero on exactly the lane that has one.
        """
        trace.observe(AGENT_CALL, f"implementer {task['id']} {sid} retry={bool(retry_note)}",
                      identity=f"implementer:{task['id']}")
        trace.computed(AGENT_SECONDS)
        run.launched.append(sid)
        Path(workspace).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(orchestrator, "launch_implementer", launch_implementer)

    def run_verification_gate(task_id, workspace, worktree, *, pre_merge_only=False):
        trace.observe(TEST_RUN, f"verification gate {task_id}",
                      identity=f"gate:{task_id}")
        trace.computed(CHECK_SECONDS)
        return scenario.gate

    monkeypatch.setattr(orchestrator, "run_verification_gate", run_verification_gate)

    def sonnet_review_proofs(task_id, verifications, impl_verifs, **kwargs):
        trace.observe(AGENT_CALL, f"proof review {task_id}", identity=f"review:{task_id}")
        trace.computed(AGENT_SECONDS)
        return scenario.review

    monkeypatch.setattr(orchestrator, "sonnet_review_proofs", sonnet_review_proofs)

    def merge_and_eval(entry):
        trace.observe(TEST_RUN, f"merge+smoke {entry['task_id']}",
                      identity=f"smoke:{entry['task_id']}")
        trace.computed(CHECK_SECONDS)
        return scenario.merge

    monkeypatch.setattr(orchestrator, "merge_and_eval", merge_and_eval)

    # Marking the roadmap is a local file write inside the sandbox, not an external
    # effect, so the real one runs — stubbing it left every completed task still looking
    # pending, and the loop relaunched it on the next poll. A double that changes which
    # branch the loop takes next is not instrumentation, it is a different program.
    real_mark = orchestrator.mark_roadmap_complete

    def mark_roadmap_complete(task_id, **kwargs):
        trace.observe("roadmap_write", f"complete {task_id}", identity="mark_complete")
        return real_mark(task_id, **kwargs)

    _patch_everywhere(monkeypatch, "mark_roadmap_complete", mark_roadmap_complete)

    # ── the retry path, recorded as the agent call it is ──
    real_retry = orchestrator._do_retry

    def _do_retry(task_id, entry, reason, retry_counts, in_flight):
        trace.observe(AGENT_CALL, f"re-brief {task_id}", identity=f"implementer:{task_id}")
        trace.computed(AGENT_SECONDS)
        return real_retry(task_id, entry, reason, retry_counts, in_flight)

    monkeypatch.setattr(orchestrator, "_do_retry", _do_retry)


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────
def pytest_addoption(parser):
    """`--baseline-capture` lets `scripts/graph_baseline.py` read the traces back out.

    The flag changes nothing about how a lane runs. It only asks `run_lane` to stash each
    `LaneRun` on the test item, so a plugin collecting reports can pull the trace out of a
    normal green suite run. The baseline is therefore always captured from a *passing*
    harness — a number recorded from a failing fixture measures the fixture.
    """
    parser.addoption(
        "--baseline-capture",
        action="store_true",
        default=False,
        help="stash each LaneRun on its test item for scripts/graph_baseline.py",
    )


def _modules_to_rebase():
    return [
        module
        for name, module in sorted(sys.modules.items())
        if module is not None and (name == "maestro" or name.startswith("maestro."))
    ]


def _module_paths(module) -> dict:
    return {
        name: value
        for name, value in vars(module).items()
        if not name.startswith("__") and isinstance(value, Path)
    }


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


@pytest.fixture
def lane_sandbox(tmp_path, monkeypatch):
    """Every maestro path global rebased into a temp tree, verified empty of live paths.

    The assertion at the end is the isolation guarantee P0 asks for, and it is phrased as
    "nothing still resolves inside a real repo" rather than "the names I listed were
    redirected" on purpose: the list is what goes stale, the invariant is what matters.
    """
    import maestro.orchestrator  # noqa: F401 — ensure the module graph is loaded

    repo = tmp_path / "repo"
    (repo / ".orchestrator" / "workspaces").mkdir(parents=True)
    (repo / "docs").mkdir(parents=True)

    checked: list = []
    for module in _modules_to_rebase():
        root = getattr(module, "REPO", None)
        if not isinstance(root, Path):
            root = getattr(module, "REPO_ROOT", None)
        if not isinstance(root, Path):
            continue
        root = root.resolve()
        checked.append((module, root))
        for name, value in _module_paths(module).items():
            resolved = value if value.is_absolute() else (Path.cwd() / value)
            if not _under(resolved, root):
                continue
            monkeypatch.setattr(module, name, repo / resolved.relative_to(root))

    leaked = sorted(
        f"{module.__name__}.{name}"
        for module, root in checked
        for name, value in _module_paths(module).items()
        if _under(value if value.is_absolute() else Path.cwd() / value, root)
    )
    assert not leaked, (
        f"path globals still resolve inside a real repo: {leaked}. "
        "Running a lane now would write to a live project."
    )
    return repo


def _roadmap_document(tasks: list) -> str:
    """A ROADMAP.md holding one fenced YAML block per task — the format the parser reads."""
    import yaml

    # Each task under its own `### <id>` heading, as real ROADMAPs have it: graduation
    # strips a task's section by that heading, and without one it would take every task
    # after it too.
    blocks = [
        f"### {task['id']}\n\n```yaml\n" + yaml.safe_dump(task, sort_keys=False) + "```"
        for task in tasks
    ]
    return "# Roadmap\n\n" + "\n\n".join(blocks) + "\n"


@pytest.fixture
def run_lane(lane_sandbox, request):
    """Run one `LaneScenario` through the real event loop and hand back its `LaneRun`.

    Each call installs its doubles through a fresh `MonkeyPatch` and undoes the previous
    call's, which matters more than it looks: the state-counting doubles wrap whatever
    `read_state` they find, so a second set installed over the first would wrap *it*, and
    every read in the second run would also be recorded into the first run's trace. That
    made two runs of the same lane produce different traces and reproducibility fail for a
    reason that had nothing to do with the orchestrator.
    """
    import maestro.orchestrator as orchestrator
    import maestro.state

    installed: list = []

    def _run(scenario: LaneScenario) -> LaneRun:
        while installed:
            installed.pop().undo()
        monkeypatch = pytest.MonkeyPatch()
        installed.append(monkeypatch)
        # Each call starts from an empty tree. `test_every_lane_is_reproducible` runs the
        # same lane twice through this one fixture, and the second run must not inherit
        # the first's journal, state document or DONE sentinels — it would take different
        # branches and the comparison would be meaningless.
        import shutil

        if lane_sandbox.exists():
            shutil.rmtree(lane_sandbox)
        (lane_sandbox / ".orchestrator" / "workspaces").mkdir(parents=True)
        orchestrator.ROADMAP_FILE.parent.mkdir(parents=True, exist_ok=True)
        orchestrator.ROADMAP_FILE.write_text(
            _roadmap_document(scenario.tasks), encoding="utf-8"
        )
        state = {"in_flight": [], "retry_counts": {}, **scenario.state}
        maestro.state.STATE_JSON.parent.mkdir(parents=True, exist_ok=True)
        maestro.state.STATE_JSON.write_text(json.dumps(state), encoding="utf-8")

        if scenario.project_yaml is not None:
            import yaml
            from maestro import config as project_config

            project_config.PROJECT_YAML.write_text(
                yaml.safe_dump(scenario.project_yaml, sort_keys=False), encoding="utf-8"
            )

        run = LaneRun(scenario, orchestrator, lane_sandbox)
        _install_doubles(monkeypatch, run)
        if callable(scenario.before_run):
            scenario.before_run(run)
        run.run()
        if request.config.getoption("--baseline-capture"):
            request.node._lane_run = run
        return run

    yield _run
    while installed:
        installed.pop().undo()
