"""The orchestrator's two switch hooks, and the `backend` key on an `in_flight` entry.

M2 gives the event loop somewhere to send work when a backend runs out, instead of only
somewhere to wait. Two paths change (plan `docs/plans/2026-08-12-m2-backends.md`, Task 6
steps 3–5):

* **proactive** — `main`'s usage-threshold block. Above the switch threshold, in-flight
  tasks move to a fallback backend rather than being throttled and then paused;
* **reactive** — `reconcile_in_flight`'s usage-limit net. A limit-shaped exit hands the
  task to a fallback instead of pausing the whole loop until the reset.

A third path is pinned here because it decides what a launch *records*: `_launch_backend`
now honours the operator's `/backend <name>` (step 6) — the state key
`maestro.hitl.commands.BACKEND_KEY` — ahead of the `maestro.roles` resolution, and so does
`implementer._implementer_backend`, which resolves the launch itself. The tests below hold
those two together: if only one read the key, the `backend` on the `in_flight` entry would
name an agent the task is not running on, and every switch decision taken from that entry
would be about the wrong one.

The load-bearing property in both is the *negative* one: with no fallback available —
nothing installed to move to, no worktree left to hand over, or a switch that failed —
the pre-M2 throttle/pause behaviour must run exactly as it always did. Every test here
that asserts a switch has a sibling asserting that untouched legacy path.

Nothing in this module launches an agent: `switch_task` is stubbed everywhere it is
reachable, backend discovery is stubbed, and no `subprocess` call is left unpatched.
"""
from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from maestro import config as config_module
from maestro import implementer
from maestro import orchestrator
from maestro import state as state_module
from maestro.backends.base import Capabilities, ExitVerdict, Usage, WindowUsage
from maestro.backends.registry import get_backend as real_get_backend
from maestro.hitl import commands
from maestro.limits import ModelLimits
from maestro.switch import (
    REASON_CONTEXT,
    REASON_QUOTA,
    REASON_THRESHOLD,
    SwitchOutcome,
)

#: Repo root. Named `REPO` to match the house convention for `Path`-valued globals.
REPO = Path(__file__).resolve().parents[1]

CLAUDE = "claude"
CODEX = "codex"


# ── helpers ──


def _entry(tmp_path: Path, *, sid="impl-T1-1", task_id="T1", backend=CLAUDE,
           role="implementer", worktree=None, **extra) -> dict:
    """An `in_flight` entry whose worktree really exists, as a live one's would."""
    if worktree is None:
        worktree = tmp_path / f"wt-{task_id}"
        worktree.mkdir(parents=True, exist_ok=True)
    entry = {
        "session_id": sid,
        "task_id": task_id,
        "role": role,
        "worktree": str(worktree),
        "window": f"impl-{task_id}",
        "branch": f"impl-{task_id.lower()}-1",
        "started_at": "2026-08-12T00:00:00Z",
        "status": "running",
        "backend": backend,
    }
    entry.update(extra)
    return entry


def _switched_entry(old: dict, to_backend=CODEX, sid="impl-T1-2") -> dict:
    new = dict(old)
    new.update({"session_id": sid, "backend": to_backend})
    return new


class _FakeDriver:
    """A driver that reports a usage sample and/or an exit verdict without touching a
    binary.

    `exit_verdict` defaults to a plain crash — never `quota_exhausted` — so tests that
    only care about the threshold path are not accidentally exercising the reconcile
    switch/pause branch through `parse_exit`.
    """

    def __init__(self, used_pct: float | None, telemetry: bool = True,
                 exit_verdict: ExitVerdict | None = None,
                 context_tokens: int = 0, model: str | None = None,
                 updated_at: str = "", session_id: str = "", record=None):
        self._used_pct = used_pct
        self._telemetry = telemetry
        self._exit_verdict = exit_verdict or ExitVerdict(kind="crashed")
        # A4: the context half of the same sample. Zero tokens and no model by default,
        # so every test written before D4 keeps reporting a quota-only reading.
        self._context_tokens = context_tokens
        self._model = model
        self._updated_at = updated_at
        self._session_id = session_id
        #: Optional call recorder, so a test can pin *which* question was asked.
        self._record = record

    def capabilities(self) -> Capabilities:
        return Capabilities(usage_telemetry=self._telemetry)

    def usage(self, handle=None):
        """A5: the two questions `AgentBackend.usage` distinguishes.

        Without a handle this is the account-level answer — quota windows, attributable
        to nobody, so no context reading and no `session_id`. With one it is the
        per-session answer, and `session_id` is whatever the test told this driver to
        claim: the real drivers stamp `handle.session_id`, and a test that stamps
        something else is modelling exactly the misattribution `_attributed_to` exists to
        refuse.
        """
        if self._record is not None:
            self._record(handle)
        if self._used_pct is None:
            return None
        if handle is None:
            return Usage(windows={300: WindowUsage(used_pct=self._used_pct)})
        return Usage(
            windows={300: WindowUsage(used_pct=self._used_pct)},
            context_total_input_tokens=self._context_tokens,
            model=self._model,
            updated_at=self._updated_at,
            session_id=self._session_id,
        )

    def parse_exit(self, rc: int, log_tail: str) -> ExitVerdict:
        return self._exit_verdict


@pytest.fixture
def hooks(monkeypatch, tmp_path):
    """Every seam the switch hooks reach, stubbed and recorded.

    `switch_task` is replaced by default, so no test in this module can start an agent.
    """
    box = SimpleNamespace(
        switches=[],
        journal=[],
        launch_times_persisted=[],
        pauses=[],
        outcome=None,
        raise_on_switch=None,
        available=(CLAUDE, CODEX),
        usage_pct=95.0,
    )

    def _switch_task(task_id, **kwargs):
        box.switches.append(SimpleNamespace(task_id=task_id, **kwargs))
        if box.raise_on_switch is not None:
            raise box.raise_on_switch
        entry = kwargs.get("entry") or {}
        if box.outcome is not None:
            return box.outcome
        new_entry = _switched_entry(dict(entry))
        # The real `switch_task` persists the replacement entry through
        # `_replace_in_flight`, so the next poll's `reconcile_in_flight` reads the
        # *switched* task, not the original. Mirror that here — a fixture that keeps
        # handing back the pre-switch entry would hide a switch loop rather than expose
        # one.
        live = getattr(box, "in_flight", None)
        if live is not None:
            box.in_flight = [new_entry if e.get("session_id") == entry.get("session_id")
                             else e for e in live]
        return SwitchOutcome(
            task_id=task_id,
            reason=kwargs.get("reason", ""),
            from_backend=kwargs.get("from_backend", CLAUDE),
            to_backend=CODEX,
            switched=True,
            entry=new_entry,
            new_session_id=new_entry["session_id"],
        )

    monkeypatch.setattr(orchestrator, "switch_task", _switch_task)
    monkeypatch.setattr(orchestrator, "_available_backends", lambda: box.available)
    monkeypatch.setattr(
        orchestrator, "get_backend", lambda name: _FakeDriver(box.usage_pct)
    )
    monkeypatch.setattr(
        orchestrator,
        "append_journal",
        lambda event, detail="", session_id="": box.journal.append(
            (event, detail, session_id)
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_persist_launch_time",
        lambda sid, ts: box.launch_times_persisted.append(sid),
    )
    monkeypatch.setattr(orchestrator, "WORKSPACES", tmp_path / "workspaces")
    (tmp_path / "workspaces").mkdir(parents=True, exist_ok=True)
    return box


# =======================================================================================
# Step 3 — the `backend` key on an in_flight entry
# =======================================================================================


def _launch_site_entries() -> list[ast.Dict]:
    """Every implementer `in_flight` entry literal in the orchestrator's source.

    Read out of the source rather than out of a live launch because the second site sits
    inside `main`'s 600-line body, which cannot be reached without launching an agent.
    """
    tree = ast.parse((REPO / "maestro" / "orchestrator.py").read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        value = getattr(node, "value", None)
        if not isinstance(value, ast.Dict):
            continue
        keys = [k.value for k in value.keys if isinstance(k, ast.Constant)]
        roles = [
            v.value
            for k, v in zip(value.keys, value.values)
            if isinstance(k, ast.Constant) and k.value == "role" and isinstance(v, ast.Constant)
        ]
        if "session_id" in keys and roles == ["implementer"]:
            found.append(value)
    return found


def test_both_implementer_launch_sites_record_a_backend_and_a_model():
    """Re-baselined from `test_both_implementer_launch_sites_record_a_backend` when the
    `model` key was added: one more required key, nothing relaxed.

    The `model` key is what D4 measures a task's context ceiling against. Since C1 the
    role table is not that answer — a task's own `model:` overrides it — so the launch
    has to record what it resolved, exactly as it already records the backend.
    """
    sites = _launch_site_entries()
    assert len(sites) == 2, "expected exactly the retry and the main-loop launch sites"
    for site in sites:
        keys = [k.value for k in site.keys if isinstance(k, ast.Constant)]
        assert keys == [
            "session_id", "task_id", "role", "worktree", "window",
            "branch", "started_at", "status", "backend", "model",
        ]


def _retry_box(monkeypatch, tmp_path, *, default_backend=CODEX):
    """`_do_retry` with everything it reaches stubbed. Records the launch's backend."""
    state: dict = {"in_flight": []}
    seen: list = []
    monkeypatch.setattr(orchestrator, "WORKSPACES", tmp_path / "workspaces")
    monkeypatch.setattr(orchestrator, "worktree_path_for", lambda tid: tmp_path / f"wt-{tid}")
    monkeypatch.setattr(orchestrator, "create_worktree", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "launch_implementer",
                        lambda *a, **k: seen.append(k.get("backend", "")))
    monkeypatch.setattr(orchestrator, "parse_runnable_tasks", lambda: [])
    monkeypatch.setattr(orchestrator, "append_journal", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "read_state", lambda: state)
    monkeypatch.setattr(orchestrator, "write_state", lambda new: state.update(new))
    monkeypatch.setattr(orchestrator, "_launch_backend", lambda: default_backend)
    # The retry's entry now records a model too, and that resolution reads the role
    # table. Pinned here rather than left to the repository's own `project.yaml`, so no
    # test in this helper depends on a file outside its sandbox.
    monkeypatch.setattr(
        config_module,
        "load_project_yaml",
        lambda: {"roles": {"implementer": {"backend": default_backend,
                                           "models": {CLAUDE: "claude-sonnet-5",
                                                      CODEX: "gpt-5-codex"}}}},
    )
    return seen


def test_do_retry_records_the_backend_the_retry_runs_on(monkeypatch, tmp_path):
    """The retry's entry carries a backend, and every pre-existing key keeps its
    spelling, its value and its place."""
    _retry_box(monkeypatch, tmp_path)

    old = _entry(tmp_path)
    in_flight = [old]
    orchestrator._do_retry("T1", old, "boom", {}, in_flight)

    new = in_flight[-1]
    assert new["role"] == "implementer"
    assert new["status"] == "running"
    assert new["window"] == "impl-T1"
    # Re-baselined from `list(new)[-1] == "backend"` when `model` joined it: both new
    # keys are appended, in order, and nothing before them moved.
    assert list(new)[-2:] == ["backend", "model"]


def test_do_retry_stays_on_the_backend_the_dying_attempt_was_running_on(monkeypatch, tmp_path):
    """A task only reaches a non-default backend because something moved it there.

    Re-resolving the role default on retry hands it straight back to the backend that just
    ran out of room — the wall the switch existed to avoid. Observed live 2026-08-20:
    M6SCRATCH1 had switched to codex on a usage threshold, its window was reaped, and the
    retry relaunched on claude.
    """
    seen = _retry_box(monkeypatch, tmp_path, default_backend=CLAUDE)

    old = _entry(tmp_path, backend=CODEX)
    in_flight = [old]
    orchestrator._do_retry("T1", old, "boom", {}, in_flight)

    assert in_flight[-1]["backend"] == CODEX, "retry walked back the switch"
    assert seen == [CODEX], "the launch must be pinned to the same backend it is recorded as"


def test_do_retry_records_the_model_it_resolves_for_the_backend_it_carries(
    monkeypatch, tmp_path
):
    """The `model` key exists for D4, which measures a context ceiling per model. A retry
    that stays on `codex` must record `codex`'s model — recording the default backend's
    would key the ceiling on a model the task is not running."""
    _retry_box(monkeypatch, tmp_path, default_backend=CLAUDE)

    old = _entry(tmp_path, backend=CODEX)
    in_flight = [old]
    orchestrator._do_retry("T1", old, "boom", {}, in_flight)

    assert in_flight[-1]["backend"] == CODEX
    assert in_flight[-1]["model"] == "gpt-5-codex"


def test_do_retry_records_the_tasks_own_model_override(monkeypatch, tmp_path):
    """C1: a task's `model:` beats the role table at the launch site, so it has to beat
    it in the record too — otherwise D4 measures the retry against the role table's
    model while the agent runs on the task's."""
    _retry_box(monkeypatch, tmp_path, default_backend=CODEX)
    monkeypatch.setattr(
        orchestrator, "parse_runnable_tasks",
        lambda: [{"id": "T1", "title": "T1", "short_desc": "", "model": "gpt-5.6-sol"}],
    )

    old = _entry(tmp_path, backend=CODEX)
    in_flight = [old]
    orchestrator._do_retry("T1", old, "boom", {}, in_flight)

    assert in_flight[-1]["model"] == "gpt-5.6-sol"


def test_launch_model_degrades_to_empty_when_resolution_breaks(monkeypatch):
    """G6: a missing reading is "not measurable", never a stand-in value. A resolution
    that blows up records nothing, and `_context_rotations` then falls back to the role
    table exactly as a pre-`model` entry does — the same shape `_launch_backend` has.
    """
    def boom(*_args, **_kwargs):
        raise RuntimeError("project.yaml is unreadable")

    monkeypatch.setattr(config_module, "load_project_yaml", boom)
    assert orchestrator._launch_model({"id": "T1"}, CLAUDE) == ""


def test_launch_model_degrades_to_empty_on_a_malformed_task_model(monkeypatch):
    """A non-string `model:` raises out of `resolve_implementer_model` by design (R2's
    documented edge). The *record* must not take the process down with it — the launch
    that would have raised for real ran first."""
    monkeypatch.setattr(
        config_module,
        "load_project_yaml",
        lambda: {"roles": {"implementer": {"backend": CLAUDE, "models": {}}}},
    )
    assert orchestrator._launch_model({"id": "T1", "model": 7}, CLAUDE) == ""


def test_do_retry_falls_back_to_the_role_default_when_the_entry_names_no_backend(
    monkeypatch, tmp_path
):
    """Pre-M2 entries (and hand-edited ones) carry no `backend` key."""
    seen = _retry_box(monkeypatch, tmp_path, default_backend=CODEX)

    old = _entry(tmp_path)
    old.pop("backend", None)
    in_flight = [old]
    orchestrator._do_retry("T1", old, "boom", {}, in_flight)

    assert in_flight[-1]["backend"] == CODEX
    assert seen == [CODEX]


def test_do_retry_carries_backends_tried_across_the_relaunch(monkeypatch, tmp_path):
    """`backends_tried` is the switch machinery's anti-ping-pong memory. Dropping it on a
    retry would let the task be handed back to a backend it has already exhausted."""
    _retry_box(monkeypatch, tmp_path)

    old = _entry(tmp_path, backend=CODEX)
    old["backends_tried"] = [CLAUDE]
    in_flight = [old]
    orchestrator._do_retry("T1", old, "boom", {}, in_flight)

    assert in_flight[-1]["backends_tried"] == [CLAUDE]


def test_do_retry_omits_backends_tried_when_the_dying_entry_had_none(monkeypatch, tmp_path):
    """Absent, not an empty list — the key's absence is what `_backends_tried` reads as
    'never switched', and an empty list would be a new spelling of the same thing."""
    _retry_box(monkeypatch, tmp_path)

    old = _entry(tmp_path)
    in_flight = [old]
    orchestrator._do_retry("T1", old, "boom", {}, in_flight)

    assert "backends_tried" not in in_flight[-1]


def _state_file(monkeypatch, tmp_path, **fields) -> Path:
    """A real state document, at the path `maestro.state.read_state` reads.

    Written and re-read as JSON rather than stubbed, so these tests pin the key's
    *spelling* on disk — the only thing `/backend <name>` and the launch path share.
    """
    path = tmp_path / "state.json"
    path.write_text(json.dumps(fields), encoding="utf-8")
    monkeypatch.setattr(state_module, "STATE_JSON", path)
    return path


@pytest.fixture
def no_probing(monkeypatch):
    """Fail the test if resolving a launch backend probes a binary or spawns anything."""
    monkeypatch.setattr(
        orchestrator,
        "_available_backends",
        lambda: pytest.fail("recording the backend must not probe binaries"),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: pytest.fail("recording the backend must not spawn a process"),
    )


def test_launch_backend_prefers_the_operators_recorded_choice(monkeypatch, tmp_path, no_probing):
    """Step 6's promise — `/backend <name>` "sets future launches" — is kept here.

    The recorded key wins over the configured role, and resolving it stays a
    configuration read: one small state document, no binary probe, no subprocess.
    """
    _state_file(monkeypatch, tmp_path, **{commands.BACKEND_KEY: CODEX})
    monkeypatch.setattr(orchestrator, "backend_for", lambda role: CLAUDE)
    assert orchestrator._launch_backend() == CODEX


def test_launch_backend_falls_back_to_pure_configuration_with_no_choice_recorded(
    monkeypatch, tmp_path, no_probing
):
    """With nothing recorded the pre-M2 answer stands: `maestro.roles`, unvalidated and
    unprobed, once per launch."""
    _state_file(monkeypatch, tmp_path)
    monkeypatch.setattr(orchestrator, "backend_for", lambda role: f"resolved-{role}")
    assert orchestrator._launch_backend() == "resolved-implementer"


@pytest.mark.parametrize(
    "recorded",
    ["gpt9000", "", "   ", None, 7, True, ["codex"], {"name": "codex"}],
    ids=["unknown", "empty", "blank", "null", "number", "bool", "list", "mapping"],
)
def test_a_recorded_value_that_names_no_known_backend_is_ignored_silently(
    monkeypatch, tmp_path, no_probing, recorded
):
    """A hand-edited state document must not be able to stop the loop launching work, and
    must not be able to send a launch at a driver that does not exist: anything the
    registry does not know reads as "no choice", with no exception on the way out."""
    _state_file(monkeypatch, tmp_path, **{commands.BACKEND_KEY: recorded})
    monkeypatch.setattr(orchestrator, "backend_for", lambda role: f"resolved-{role}")
    assert orchestrator._launch_backend() == "resolved-implementer"


@pytest.mark.parametrize("body", ["{not json", "", "[]", "null"], ids=["junk", "empty", "list", "null"])
def test_a_state_document_that_cannot_be_read_is_ignored_silently(
    monkeypatch, tmp_path, no_probing, body
):
    path = tmp_path / "state.json"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setattr(state_module, "STATE_JSON", path)
    monkeypatch.setattr(orchestrator, "backend_for", lambda role: f"resolved-{role}")
    assert orchestrator._launch_backend() == "resolved-implementer"


def test_a_missing_state_document_is_ignored_silently(monkeypatch, tmp_path, no_probing):
    monkeypatch.setattr(state_module, "STATE_JSON", tmp_path / "never-written.json")
    monkeypatch.setattr(orchestrator, "backend_for", lambda role: f"resolved-{role}")
    assert orchestrator._launch_backend() == "resolved-implementer"


def test_launch_backend_degrades_to_empty_when_resolution_breaks(monkeypatch, tmp_path):
    def _boom(role):
        raise RuntimeError("no config")

    _state_file(monkeypatch, tmp_path)
    monkeypatch.setattr(orchestrator, "backend_for", _boom)
    assert orchestrator._launch_backend() == ""


def test_the_launch_path_reads_the_key_the_backend_command_writes():
    """One spelling, two modules. `commands` cannot be imported from the launch path
    without closing an import cycle, so the constant is duplicated — and pinned here."""
    assert implementer.OPERATOR_BACKEND_KEY == commands.BACKEND_KEY


def test_the_backend_recorded_and_the_backend_launched_cannot_drift(monkeypatch, tmp_path):
    """The reason both readers exist: `_launch_backend` stamps the `in_flight` entry and
    `_implementer_backend` resolves the agent that actually starts. If only one honoured
    the operator's choice, the entry would name a backend the task is not running on —
    and `_entry_backend`, the switch triggers and `/progress` all read that entry."""
    monkeypatch.setattr(
        config_module,
        "load_project_yaml",
        lambda: {"roles": {"implementer": {"backend": CLAUDE, "models": {CODEX: "gpt-5-codex"}}}},
    )

    _state_file(monkeypatch, tmp_path, **{commands.BACKEND_KEY: CODEX})
    assert orchestrator._launch_backend() == CODEX
    backend, models = implementer._implementer_backend()
    assert backend == CODEX
    assert models == {CODEX: "gpt-5-codex"}     # the role's own table, one column of it

    _state_file(monkeypatch, tmp_path)          # the operator has chosen nothing
    assert orchestrator._launch_backend() == CLAUDE
    assert implementer._implementer_backend()[0] == CLAUDE


class _SpecRecorder:
    """A driver that records the `LaunchSpec` instead of starting anything."""

    def __init__(self):
        self.specs: list = []

    def capabilities(self) -> Capabilities:
        return Capabilities(system_prompt_file=False)

    def launch(self, spec):
        self.specs.append(spec)
        return SimpleNamespace(window=f"impl-{spec.task_id}")


@pytest.mark.parametrize("task_model", [None, "opus", "haiku", "gpt-5.6-sol", ""])
def test_the_model_recorded_and_the_model_launched_cannot_drift(
    monkeypatch, tmp_path, task_model
):
    """The `backend` key's story, one commit later, for the `model` key.

    `launch_implementer` resolves the model that reaches `--model`; the orchestrator
    stamps a model on the `in_flight` entry, and D4 measures that task's context ceiling
    against it. If the two resolutions were written twice they would drift — which is
    precisely what happened when C1 changed one of them and left `roles.model_for`
    standing as the other. They are one function, and this asserts it against the value
    a real launch actually hands the driver.
    """
    monkeypatch.setattr(
        config_module,
        "load_project_yaml",
        lambda: {"roles": {"implementer": {"backend": CLAUDE,
                                           "models": {CLAUDE: "claude-sonnet-5"}}}},
    )
    monkeypatch.setattr(implementer, "append_journal", lambda *a, **k: None)
    driver = _SpecRecorder()
    monkeypatch.setattr(implementer, "_implementer_driver", lambda backend, uuid_: driver)

    task = {"id": "T1", "title": "T1", "short_desc": "d"}
    if task_model is not None:
        task["model"] = task_model
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)

    implementer.launch_implementer(task, "impl-T1-1", workspace, tmp_path / "wt")

    assert driver.specs[0].model == orchestrator._launch_model(task, CLAUDE)
    # …and it is a real answer, not two empties agreeing with each other.
    assert driver.specs[0].model


# =======================================================================================
# The shared guards
# =======================================================================================


def test_a_switch_needs_a_worktree_that_still_exists(tmp_path):
    """D3 exists to preserve the worktree's uncommitted work; with the worktree gone
    there is nothing to hand over and a "switch" would be a fresh start in disguise."""
    assert orchestrator._handover_ready(_entry(tmp_path)) is True
    gone = _entry(tmp_path, worktree=tmp_path / "nonexistent")
    assert orchestrator._handover_ready(gone) is False


def test_a_script_task_is_never_switched(tmp_path):
    assert orchestrator._handover_ready(_entry(tmp_path, role="script")) is False


def test_an_entry_without_a_backend_key_falls_back_to_the_configured_one(monkeypatch, tmp_path):
    monkeypatch.setattr(orchestrator, "_launch_backend", lambda: CLAUDE)
    entry = _entry(tmp_path)
    entry.pop("backend")
    assert orchestrator._entry_backend(entry) == CLAUDE
    assert orchestrator._entry_backend(_entry(tmp_path, backend=CODEX)) == CODEX


def test_usage_pressure_reads_the_backends_own_sample(monkeypatch):
    """A backend with quota to spare is not under pressure just because the loop's own
    reading is high — otherwise a task would be moved *onto* the exhausted backend."""
    monkeypatch.setattr(orchestrator, "get_backend", lambda name: _FakeDriver(10.0))
    assert orchestrator._under_usage_pressure(CODEX, 99.0) is False
    monkeypatch.setattr(orchestrator, "get_backend", lambda name: _FakeDriver(99.0))
    assert orchestrator._under_usage_pressure(CODEX, 0.0) is True


def test_usage_pressure_falls_back_to_the_loops_reading_without_a_sample(monkeypatch):
    """No sample is never read as "no pressure" (G6): the loop's own number decides."""
    monkeypatch.setattr(orchestrator, "get_backend", lambda name: _FakeDriver(None))
    assert orchestrator._under_usage_pressure(CLAUDE, 95.0) is True
    assert orchestrator._under_usage_pressure(CLAUDE, 10.0) is False

    monkeypatch.setattr(
        orchestrator, "get_backend", lambda name: _FakeDriver(1.0, telemetry=False)
    )
    assert orchestrator._under_usage_pressure(CLAUDE, 95.0) is True


def test_usage_pressure_survives_a_driver_that_cannot_be_built(monkeypatch):
    def _boom(name):
        raise RuntimeError("no driver")

    monkeypatch.setattr(orchestrator, "get_backend", _boom)
    assert orchestrator._under_usage_pressure(CLAUDE, 95.0) is True


def test_no_fallback_means_no_switch(hooks, tmp_path):
    """The real `roles.fallback_backend` runs here: with only the current backend
    installed there is nowhere to go, and nothing is stopped."""
    hooks.available = (CLAUDE,)
    assert orchestrator._switch_instead_of_waiting(_entry(tmp_path), REASON_THRESHOLD) is None
    assert hooks.switches == []


def test_a_failed_switch_is_journalled_and_reported_as_no_switch(hooks, tmp_path):
    hooks.raise_on_switch = RuntimeError("tmux is gone")
    assert orchestrator._switch_instead_of_waiting(_entry(tmp_path), REASON_THRESHOLD) is None
    assert [event for event, _, _ in hooks.journal] == ["backend_switch_failed"]
    detail = hooks.journal[0][1]
    assert "T1" in detail and "tmux is gone" in detail


def test_a_refused_switch_is_reported_as_no_switch(hooks, tmp_path):
    """`switch_task` answering "nowhere to go" is a normal result, not an exception."""
    hooks.outcome = SwitchOutcome(
        task_id="T1", reason=REASON_THRESHOLD, from_backend=CLAUDE, switched=False
    )
    assert orchestrator._switch_instead_of_waiting(_entry(tmp_path), REASON_THRESHOLD) is None
    assert hooks.journal == []


def test_a_switch_re_dates_the_launch_clock(hooks, tmp_path):
    """The incoming session starts its timeout window now — inheriting the outgoing
    session's clock would time the new agent out early."""
    old = _entry(tmp_path)
    launch_times = {old["session_id"]: 1.0}
    new = orchestrator._switch_instead_of_waiting(old, REASON_THRESHOLD, launch_times)
    assert new is not None
    assert old["session_id"] not in launch_times
    assert launch_times[new["session_id"]] > 1.0
    assert hooks.launch_times_persisted == [new["session_id"]]


# =======================================================================================
# Step 4 — the proactive threshold hook
# =======================================================================================


def test_threshold_switch_moves_an_in_flight_task_when_a_fallback_exists(hooks, tmp_path):
    old = _entry(tmp_path)
    in_flight = [old]
    launch_times: dict = {}

    moved = orchestrator._threshold_switches(in_flight, launch_times, 95.0)

    assert moved == 1
    assert len(hooks.switches) == 1
    call = hooks.switches[0]
    assert call.task_id == "T1"
    assert call.reason == REASON_THRESHOLD
    assert call.from_backend == CLAUDE
    assert call.available == (CLAUDE, CODEX)
    assert [e["session_id"] for e in in_flight] == ["impl-T1-2"]
    assert in_flight[0]["backend"] == CODEX
    assert in_flight[0]["worktree"] == old["worktree"]      # same worktree, D3


def test_threshold_switch_is_inert_below_the_threshold(hooks, tmp_path, monkeypatch):
    """Under the threshold the hook reaches no backend at all — the ordinary poll must
    not pay for a usage sample or a binary probe."""
    monkeypatch.setattr(
        orchestrator, "get_backend", lambda name: pytest.fail("sampled below threshold")
    )
    monkeypatch.setattr(
        orchestrator, "_available_backends", lambda: pytest.fail("probed below threshold")
    )
    in_flight = [_entry(tmp_path)]
    assert orchestrator._threshold_switches(in_flight, {}, orchestrator.SWITCH_THRESHOLD_PCT - 0.1) == 0
    assert hooks.switches == []
    assert [e["session_id"] for e in in_flight] == ["impl-T1-1"]


def test_threshold_switch_is_inert_with_nothing_in_flight(hooks):
    assert orchestrator._threshold_switches([], {}, 99.0) == 0
    assert hooks.switches == []


def test_threshold_switch_reports_zero_when_no_fallback_exists(hooks, tmp_path):
    """The answer `main` depends on: nothing moved, so the throttle/pause stands."""
    hooks.available = (CLAUDE,)
    in_flight = [_entry(tmp_path)]
    assert orchestrator._threshold_switches(in_flight, {}, 99.0) == 0
    assert hooks.switches == []
    assert [e["session_id"] for e in in_flight] == ["impl-T1-1"]


def test_threshold_switch_leaves_a_task_on_an_unpressured_backend_alone(hooks, tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "get_backend", lambda name: _FakeDriver(5.0))
    in_flight = [_entry(tmp_path, backend=CODEX)]
    assert orchestrator._threshold_switches(in_flight, {}, 99.0) == 0
    assert hooks.switches == []


def test_threshold_switch_skips_a_script_task_and_a_lost_worktree(hooks, tmp_path):
    script = _entry(tmp_path, sid="script-T2-1", task_id="T2", role="script")
    lost = _entry(tmp_path, sid="impl-T3-1", task_id="T3",
                  worktree=tmp_path / "gone-T3")
    in_flight = [script, lost]
    assert orchestrator._threshold_switches(in_flight, {}, 99.0) == 0
    assert hooks.switches == []
    assert in_flight == [script, lost]


# =======================================================================================
# Step 5 — the reactive usage-limit hook in reconcile_in_flight
# =======================================================================================


@pytest.fixture
def reconcile_env(hooks, monkeypatch, tmp_path):
    """A window-gone entry whose driver classifies the exit as quota-exhausted.

    Re-baselined for A1 (readiness queue, 2026-08-30): this used to monkeypatch
    `orchestrator._scan_impl_log_for_limit` directly, pinning the pre-fix shape where
    core code scanned Claude's own wording regardless of which backend the entry ran
    on. `reconcile_in_flight` now resolves the entry's driver (`_entry_backend`) and
    calls its `parse_exit`, so the seam to drive here is `get_backend` returning a
    fake whose `parse_exit` answers the same verdict the old stub answered.
    """
    monkeypatch.setattr(orchestrator, "tmux_window_exists", lambda window: False)
    verdict = ExitVerdict(kind="quota_exhausted", reset_at="2026-08-12T18:00:00Z",
                          evidence="You've hit your usage limit")
    monkeypatch.setattr(
        orchestrator, "get_backend",
        lambda name: _FakeDriver(hooks.usage_pct, exit_verdict=verdict),
    )
    monkeypatch.setattr(
        orchestrator,
        "_pause_for_usage_limit",
        lambda task_id, reset_iso, evidence, workspace: hooks.pauses.append(
            (task_id, reset_iso, evidence, Path(workspace).name)
        ),
    )
    monkeypatch.setattr(orchestrator, "_tail_text", lambda path, n=15: "")
    hooks.entry = _entry(tmp_path)
    (orchestrator.WORKSPACES / hooks.entry["session_id"]).mkdir(parents=True, exist_ok=True)
    return hooks


def test_reconcile_switches_instead_of_pausing_on_a_usage_limit(reconcile_env):
    launch_times: dict = {}
    surviving = orchestrator.reconcile_in_flight(
        {"in_flight": [reconcile_env.entry]}, launch_times
    )

    assert reconcile_env.pauses == []                       # the loop is not paused
    assert [e["session_id"] for e in surviving] == ["impl-T1-2"]
    assert surviving[0]["backend"] == "codex"
    call = reconcile_env.switches[0]
    assert call.reason == REASON_QUOTA
    assert call.from_backend == "claude"
    assert launch_times == {"impl-T1-2": pytest.approx(launch_times["impl-T1-2"])}


def test_reconcile_still_pauses_when_no_fallback_exists(reconcile_env):
    """The pre-M2 path, byte for byte: pause with the scan's reset and evidence, and
    drop the entry from in_flight so nothing is marked FAILED."""
    reconcile_env.available = (CLAUDE,)
    surviving = orchestrator.reconcile_in_flight({"in_flight": [reconcile_env.entry]}, {})

    assert surviving == []
    assert reconcile_env.pauses == [
        ("T1", "2026-08-12T18:00:00Z", "You've hit your usage limit", "impl-T1-1")
    ]
    workspace = orchestrator.WORKSPACES / "impl-T1-1"
    assert not (workspace / "FAILED").exists()


def test_reconcile_still_pauses_when_the_worktree_is_gone(reconcile_env, tmp_path):
    reconcile_env.entry["worktree"] = str(tmp_path / "removed")
    surviving = orchestrator.reconcile_in_flight({"in_flight": [reconcile_env.entry]}, {})
    assert surviving == []
    assert [p[0] for p in reconcile_env.pauses] == ["T1"]
    assert reconcile_env.switches == []


def test_reconcile_still_pauses_when_the_switch_fails(reconcile_env):
    reconcile_env.raise_on_switch = RuntimeError("relaunch failed")
    surviving = orchestrator.reconcile_in_flight({"in_flight": [reconcile_env.entry]}, {})
    assert surviving == []
    assert [p[0] for p in reconcile_env.pauses] == ["T1"]
    assert "backend_switch_failed" in [event for event, _, _ in reconcile_env.journal]


def test_reconcile_classifies_a_codex_quota_exit_through_its_own_driver(
    hooks, monkeypatch, tmp_path
):
    """A1: `reconcile_in_flight` must resolve the entry's own driver and read *its*
    `parse_exit`, not scan the log for Claude's wording (`quota._LIMIT_RE`) regardless of
    backend. A codex implementer's own exhaustion phrasing — unrelated to Claude's — must
    be classified as `quota_exhausted` rather than falling through to "no window, no
    sentinel — stale entry; marking FAILED".

    `get_backend` is restored to the real registry function so this exercises the actual
    `CodexBackend.parse_exit`, not a stand-in — construction and `parse_exit` are both
    inert (no binary, no subprocess; see `CodexBackend`'s docstring), so this never risks
    running a real agent CLI.
    """
    monkeypatch.setattr(orchestrator, "tmux_window_exists", lambda window: False)
    monkeypatch.setattr(orchestrator, "get_backend", real_get_backend)
    monkeypatch.setattr(
        orchestrator, "_tail_text",
        lambda path, n=15: "stream error: rate_limit_reached; giving up",
    )
    monkeypatch.setattr(
        orchestrator, "_pause_for_usage_limit",
        lambda task_id, reset_iso, evidence, workspace: hooks.pauses.append(
            (task_id, reset_iso, evidence)
        ),
    )
    hooks.available = (CODEX,)                 # only the entry's own backend — no fallback
    entry = _entry(tmp_path, backend=CODEX)
    (orchestrator.WORKSPACES / entry["session_id"]).mkdir(parents=True, exist_ok=True)

    surviving = orchestrator.reconcile_in_flight({"in_flight": [entry]}, {})

    assert surviving == []                     # dropped from in_flight, not marked FAILED
    assert hooks.pauses and hooks.pauses[0][0] == "T1"
    workspace = orchestrator.WORKSPACES / "impl-T1-1"
    assert not (workspace / "FAILED").exists()


# =======================================================================================
# The whole loop: main's threshold block
# =======================================================================================


@pytest.fixture
def loop_env(hooks, monkeypatch, tmp_path):
    """One poll cycle of `main`, with every seam stubbed. Nothing here reaches tmux,
    Telegram, the roadmap or a backend binary."""
    box = hooks
    box.state = {"in_flight": [], "retry_counts": {}}
    box.cap = (0, 95.0)
    box.polls = 0

    def _read_state():
        return dict(box.state)

    def _write_state(new):
        box.state.update(new)

    def _cap():
        box.polls += 1
        if box.polls > 1:                      # one cycle, then stop the loop
            box.state["halted"] = True
        return box.cap

    for name in ("run_dep_map", "run_status", "poll_control_commands", "_send_hitl_reminders",
                 "poll_proposal_answer", "apply_ready_self_fixes", "apply_ready_redo",
                 "poll_awaiting_verifications", "maybe_push_roadmap_map_change",
                 "notify_telegram"):
        monkeypatch.setattr(orchestrator, name, lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "parse_runnable_tasks", lambda: [])
    monkeypatch.setattr(orchestrator, "parse_prep_tasks", lambda: [])
    monkeypatch.setattr(orchestrator, "get_completed_task_ids", lambda: set())
    monkeypatch.setattr(orchestrator, "get_task_by_id", lambda tid: None)
    monkeypatch.setattr(orchestrator, "read_state", _read_state)
    monkeypatch.setattr(orchestrator, "write_state", _write_state)
    monkeypatch.setattr(orchestrator, "get_effective_cap", _cap)
    monkeypatch.setattr(orchestrator, "HALT_FILE", tmp_path / "no-halt")
    monkeypatch.setattr(orchestrator, "ROADMAP_FILE", tmp_path / "no-roadmap.md")
    monkeypatch.setattr(orchestrator, "AUTO_PROPOSE", False)
    monkeypatch.setattr(
        orchestrator, "subprocess",
        SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr="")),
    )
    monkeypatch.setattr(
        orchestrator, "time", SimpleNamespace(time=lambda: 1000.0, sleep=lambda s: None)
    )
    box.in_flight = [_entry(tmp_path)]
    monkeypatch.setattr(
        orchestrator, "reconcile_in_flight", lambda state, launch_times: list(box.in_flight)
    )
    return box


def test_main_switches_instead_of_pausing_when_a_fallback_exists(loop_env):
    """The switching poll does not pause, and the task moves exactly once.

    The second poll *does* pause: the task is on the fallback by then and
    `_backends_tried` has ruled out going back, so there is nothing left to move and the
    pre-M2 path takes over. That is the intended shape — a switch buys the work a new
    backend, not the loop an exemption from a quota it really has hit.
    """
    assert orchestrator.main() == 0
    events = [event for event, _, _ in loop_env.journal]
    assert [call.reason for call in loop_env.switches] == [REASON_THRESHOLD]
    assert loop_env.polls == 2                              # it survived to a second poll
    assert "concurrency_throttled" not in events
    assert events == ["rate_limit_pause"]                   # from the second poll, not the first


def test_a_task_already_switched_once_is_not_switched_back(loop_env, tmp_path):
    """No ping-pong: the outgoing backend is exhausted for the rest of this task's life.

    The loop's `five_pct` reading is about the backend it launched on, and a driver that
    cannot sample its own usage reports nothing (G6), so without the exhausted set the
    same task would be swapped back and forth once per poll — losing its in-progress work
    every time.
    """
    loop_env.in_flight = [_entry(tmp_path, sid="impl-T1-2", backend=CODEX,
                                 backends_tried=[CLAUDE])]
    assert orchestrator.main() == 0
    assert loop_env.switches == []
    assert [event for event, _, _ in loop_env.journal] == ["rate_limit_pause"]


def test_main_still_pauses_when_one_task_switches_but_another_is_stranded(loop_env, tmp_path):
    """A partial switch must not exempt the loop from protecting what didn't move.

    One entry (a normal implementer) switches to the fallback; a second entry (a
    `kind: script` task, which `_handover_ready` excludes on role alone) has nowhere to
    go and stays on the exhausted backend. The safety valve must still fire this same
    poll — moving *some* work off the exhausted backend is not the same as moving all of
    it, and the script task (or any other Claude call this cycle, e.g. a merge judge)
    would otherwise run unprotected at critical usage.
    """
    switchable = _entry(tmp_path, sid="impl-T1-1", task_id="T1")
    stranded = _entry(tmp_path, sid="script-T2-1", task_id="T2", role="script")
    loop_env.in_flight = [switchable, stranded]
    assert orchestrator.main() == 0
    events = [event for event, _, _ in loop_env.journal]
    assert [call.reason for call in loop_env.switches] == [REASON_THRESHOLD]
    assert loop_env.polls == 1                # did not survive to a second poll
    assert events == ["rate_limit_pause"]      # the stranded script task blocked the exemption


def test_main_pauses_exactly_as_before_when_no_fallback_exists(loop_env):
    """The pre-M2 behaviour on the same reading, unchanged: pause and break.

    No `concurrency_throttled` here, and that is the reference behaviour rather than a
    gap: `prev_cap` starts at the `-1` sentinel, whose whole purpose is to suppress the
    throttle notice on the first reading of a fresh process.
    """
    loop_env.available = (CLAUDE,)
    assert orchestrator.main() == 0
    events = [event for event, _, _ in loop_env.journal]
    assert events == ["rate_limit_pause"]
    assert loop_env.journal[0][1] == "five_h=95%"
    assert loop_env.switches == []
    assert loop_env.polls == 1                              # broke out of the loop


def test_main_leaves_an_unthrottled_poll_untouched(loop_env):
    """Below the switch threshold nothing changes at all: no switch, no throttle."""
    loop_env.cap = (orchestrator.CONCURRENCY_CAP, 10.0)
    assert orchestrator.main() == 0
    assert loop_env.switches == []
    assert [event for event, _, _ in loop_env.journal] == ["halt_respected"]


# =======================================================================================
# A4 / D4 — the context ceiling as a same-backend session rotation
# =======================================================================================
#
# The fourth trigger is not a fallback: nothing is wrong with the backend, the
# conversation is simply full. It reuses A2's per-poll usage sample rather than taking a
# third reading, and it must not mark the backend it stays on as exhausted.

#: A stand-in limits row, injected everywhere below — no test here reads the operator's
#: real `~/.claude/model_context_limits.md`.
ROW = ModelLimits("Claude Opus 5", 100_000, 120_000, 150_000, 180_000, 240_000)

#: The entry `_entry()` builds is launched at 00:00Z; a sample from 01:00Z therefore
#: postdates it, and the replacement entry a rotation writes is stamped 02:00Z.
SAMPLED_AT = "2026-08-12T01:00:00Z"
ROTATED_AT = "2026-08-12T02:00:00Z"


@pytest.fixture
def rotation(hooks, monkeypatch):
    """The rotation hook's seams: a limits row, a context-carrying sample, and a
    `switch_task` that behaves the way a real rotation does (same backend, new session,
    a fresh `started_at`)."""
    box = hooks
    box.rows = {"claude-opus-5": ROW}
    box.resolved = []
    box.context_tokens = 130_000
    box.sampled_at = SAMPLED_AT
    # The spelling production actually produces: `usage.json` carries the statusline's
    # display name, which is in neither limits table. The launch model below is what the
    # lookup must key on.
    box.model = "Opus 5"
    box.launch_model = "claude-opus-5"
    # Attributed to the entry `_entry()` builds — the shape both real drivers now produce
    # (A5): `usage(handle)` stamps the handle's own session id. Every rotation test below
    # states its attribution precondition rather than relying on this default.
    box.attributed_to = "impl-T1-1"
    orchestrator._rotation_notices.clear()

    def _resolve(name):
        box.resolved.append(name)
        return box.rows.get(name)

    def _rotate(task_id, **kwargs):
        box.switches.append(SimpleNamespace(task_id=task_id, **kwargs))
        if box.raise_on_switch is not None:
            raise box.raise_on_switch
        entry = dict(kwargs.get("entry") or {})
        entry.update({"session_id": "impl-T1-2", "started_at": ROTATED_AT})
        live = getattr(box, "in_flight", None)
        if live is not None:
            box.in_flight = [
                entry if e.get("session_id") == (kwargs.get("entry") or {}).get("session_id")
                else e for e in live
            ]
        return SwitchOutcome(
            task_id=task_id,
            reason=kwargs.get("reason", ""),
            from_backend=kwargs.get("from_backend", CLAUDE),
            to_backend=kwargs.get("from_backend", CLAUDE),
            switched=True,
            entry=entry,
            new_session_id=entry["session_id"],
        )

    monkeypatch.setattr(orchestrator, "resolve_model_limits", _resolve)
    monkeypatch.setattr(orchestrator, "switch_task", _rotate)
    monkeypatch.setattr(
        orchestrator, "model_for", lambda role, backend=None, **kw: box.launch_model
    )
    monkeypatch.setattr(
        orchestrator,
        "get_backend",
        lambda name: _FakeDriver(
            10.0,                                  # no quota pressure at all
            context_tokens=box.context_tokens,
            model=box.model,
            updated_at=box.sampled_at,
            session_id=box.attributed_to,
        ),
    )
    yield box
    orchestrator._rotation_notices.clear()


def test_a_context_crossing_rotates_the_task_onto_its_own_backend(rotation, tmp_path):
    entry = _entry(tmp_path)
    in_flight = [entry]
    rotation.in_flight = in_flight

    assert orchestrator._context_rotations(in_flight, {}, {}) == 1

    call = rotation.switches[0]
    assert call.reason == REASON_CONTEXT
    assert call.from_backend == CLAUDE
    assert call.task_id == "T1"
    # The entry was replaced in place, not appended alongside the one it replaced.
    assert [e["session_id"] for e in in_flight] == ["impl-T1-2"]


def test_a_rotation_does_not_mark_the_backend_it_stays_on_as_exhausted(rotation, tmp_path):
    """`backends_tried` is the quota path's ping-pong guard. A rotation is not a reason
    to rule out the backend the task is deliberately staying on — doing so would spend
    the fallback chain on a problem the fallback chain cannot fix."""
    entry = _entry(tmp_path)
    orchestrator._context_rotations([entry], {}, {})

    handed = rotation.switches[0].entry
    assert "backends_tried" not in handed
    # …and no target is named at all: a rotation's target is not chosen, it is where the
    # task already is.
    assert not hasattr(rotation.switches[0], "to_backend")


def test_a_rotation_re_dates_the_launch_clock(rotation, tmp_path):
    entry = _entry(tmp_path)
    launch_times = {entry["session_id"]: 1.0}

    orchestrator._context_rotations([entry], launch_times, {})

    assert entry["session_id"] not in launch_times
    assert "impl-T1-2" in launch_times
    assert rotation.launch_times_persisted == ["impl-T1-2"]


def test_a_sample_older_than_the_launch_never_rotates(rotation, tmp_path):
    """The anti-loop guard. A rotation's whole effect is on the session, and the only
    evidence it worked is a sample taken *after* it — acting on a pre-rotation reading
    would rotate the same task once per poll forever, losing its progress each time."""
    rotation.sampled_at = "2026-08-11T23:00:00Z"      # before the entry's started_at
    assert orchestrator._context_rotations([_entry(tmp_path)], {}, {}) == 0
    assert rotation.switches == []

    rotation.sampled_at = "2026-08-12T00:00:00Z"      # exactly the launch: not newer
    assert orchestrator._context_rotations([_entry(tmp_path)], {}, {}) == 0
    assert rotation.switches == []

    rotation.sampled_at = ""                          # undated: not evidence of anything
    assert orchestrator._context_rotations([_entry(tmp_path)], {}, {}) == 0
    assert rotation.switches == []


def test_a_rotated_task_does_not_rotate_again_on_the_same_sample(rotation, tmp_path):
    """The guard, exercised end to end: the same stale sample, two consecutive polls."""
    in_flight = [_entry(tmp_path)]
    rotation.in_flight = in_flight

    assert orchestrator._context_rotations(in_flight, {}, {}) == 1
    assert orchestrator._context_rotations(in_flight, {}, {}) == 0
    assert len(rotation.switches) == 1


def test_a_model_with_no_limits_row_never_rotates(rotation, tmp_path):
    """C7 owns the normaliser defect that does this to a dated model slug; A4 only has
    to degrade safely."""
    rotation.rows = {}
    rotation.context_tokens = 10_000_000

    assert orchestrator._context_rotations([_entry(tmp_path)], {}, {}) == 0
    assert rotation.switches == []
    assert rotation.resolved == ["claude-opus-5"]   # the launch model, not "Opus 5"


def test_a_context_below_the_handoff_mark_never_rotates(rotation, tmp_path):
    rotation.context_tokens = ROW.prepare_handoff_high - 1
    assert orchestrator._context_rotations([_entry(tmp_path)], {}, {}) == 0
    assert rotation.switches == []


def test_an_unmeasurable_context_never_rotates(rotation, tmp_path):
    rotation.context_tokens = 0
    assert orchestrator._context_rotations([_entry(tmp_path)], {}, {}) == 0

    # No model id from either source — neither the role table nor the sample — leaves
    # nothing to key the ceiling lookup on, however many tokens are reported.
    rotation.context_tokens = 10_000_000
    rotation.model = None
    rotation.launch_model = ""
    assert orchestrator._context_rotations([_entry(tmp_path)], {}, {}) == 0
    assert rotation.switches == []


def test_a_script_task_and_a_lost_worktree_are_never_rotated(rotation, tmp_path):
    """`_handover_ready` again: a script task runs no agent, and a vanished worktree has
    no uncommitted work left to preserve."""
    script = _entry(tmp_path, sid="script-T2-1", task_id="T2", role="script")
    lost = _entry(tmp_path, sid="impl-T3-1", task_id="T3",
                  worktree=tmp_path / "gone-for-good")

    assert orchestrator._context_rotations([script, lost], {}, {}) == 0
    assert rotation.switches == []


def test_a_failed_rotation_leaves_the_task_exactly_where_it_is(rotation, tmp_path):
    """A rotation is an optimisation over letting a session fill up; failing at it must
    cost the loop nothing it was not already going to have."""
    rotation.raise_on_switch = RuntimeError("tmux refused the window")
    entry = _entry(tmp_path)
    in_flight = [entry]

    assert orchestrator._context_rotations(in_flight, {}, {}) == 0
    assert in_flight == [entry]
    assert [event for event, _, _ in rotation.journal] == ["session_rotation_failed"]


def test_the_rotation_reuses_this_polls_usage_sample(rotation, tmp_path):
    """The rotation takes no reading the poll has already taken.

    **Re-baselined by A5.** Before it, "the reading" was one thing — one account-level
    sample per backend — and this test asserted the rotation reached no driver at all
    once A2 had primed the memo. A5 splits the reading in two: the account-level sample
    the memo already holds, and one per-session context reading per in-flight task, which
    is a different question and cannot be served out of the account slot. So what is
    pinned here now is the cost that actually matters — the account-level sample, the one
    that may spawn a `codex app-server`, is never re-taken — plus the per-session slots
    being one per session per poll however many times the rotation runs.
    """
    asked: list = []

    def _get_backend(name):
        return _FakeDriver(10.0, context_tokens=130_000, model="Opus 5",
                           updated_at=SAMPLED_AT, session_id="impl-T1-1",
                           record=lambda handle: asked.append((name, handle)))

    cache: dict = {}
    entries = [_entry(tmp_path), _entry(tmp_path, sid="impl-T4-1", task_id="T4")]
    originals = list(entries)
    orchestrator._sampled_usage(CLAUDE, cache)                # A2's own sampling

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(orchestrator, "get_backend", _get_backend)
        # One rotation, not two: the sample names T1's session, so T4 — which gets its
        # own reading, and one that does not name it — is correctly left alone.
        assert orchestrator._context_rotations(entries, {}, cache) == 1
        # Asking about the same two sessions again inside the same poll costs nothing.
        for entry in originals:
            orchestrator._rotation_sample(entry, cache)

    # The account slot was primed before the driver was watched, and never asked again.
    assert [handle for _, handle in asked if handle is None] == []
    # Two sessions, two readings — not four, and not one shared between them.
    assert sorted(handle.session_id for _, handle in asked) == ["impl-T1-1", "impl-T4-1"]


def test_the_limits_tables_are_parsed_once_per_poll(rotation, tmp_path):
    """`limits.resolve` re-reads and re-merges both markdown tables on every call, and
    rewrites `.orchestrator/model_limits.json` while it is there. Two entries on the same
    model must not cost two parses."""
    entries = [_entry(tmp_path), _entry(tmp_path, sid="impl-T4-1", task_id="T4")]

    orchestrator._context_rotations(entries, {}, {})

    assert rotation.resolved == ["claude-opus-5"]   # the launch model, not "Opus 5"


def test_main_rotates_a_context_bound_task(loop_env, monkeypatch, tmp_path):
    """The trigger is wired into the loop, not merely available to it — the exact way D4
    was 'measured, reported and ignored' before A4."""
    loop_env.cap = (orchestrator.CONCURRENCY_CAP, 10.0)   # no quota pressure anywhere
    rotations = []

    def _rotate(task_id, **kwargs):
        rotations.append(kwargs.get("reason"))
        entry = dict(kwargs.get("entry") or {})
        entry.update({"session_id": "impl-T1-2", "started_at": ROTATED_AT})
        loop_env.in_flight = [entry]
        return SwitchOutcome(
            task_id=task_id, reason=kwargs.get("reason", ""), from_backend=CLAUDE,
            to_backend=CLAUDE, switched=True, entry=entry,
            new_session_id=entry["session_id"],
        )

    monkeypatch.setattr(orchestrator, "switch_task", _rotate)
    monkeypatch.setattr(orchestrator, "resolve_model_limits", lambda name: ROW)
    monkeypatch.setattr(orchestrator, "model_for",
                        lambda role, backend=None, **kw: "claude-opus-5")
    monkeypatch.setattr(
        orchestrator, "get_backend",
        lambda name: _FakeDriver(10.0, context_tokens=130_000, model="Opus 5",
                                 updated_at=SAMPLED_AT, session_id="impl-T1-1"),
    )

    assert orchestrator.main() == 0
    # Once, on the first poll; the second poll reads the rotated entry, whose launch now
    # postdates the sample.
    assert rotations == [REASON_CONTEXT]


def test_main_leaves_a_poll_with_room_to_spare_untouched(loop_env, monkeypatch):
    """Below both marks the loop is byte-for-byte what it was: no switch, no rotation."""
    loop_env.cap = (orchestrator.CONCURRENCY_CAP, 10.0)
    monkeypatch.setattr(orchestrator, "resolve_model_limits", lambda name: ROW)
    monkeypatch.setattr(orchestrator, "model_for",
                        lambda role, backend=None, **kw: "claude-opus-5")
    monkeypatch.setattr(
        orchestrator, "get_backend",
        lambda name: _FakeDriver(10.0, context_tokens=10_000, model="Opus 5",
                                 updated_at=SAMPLED_AT, session_id="impl-T1-1"),
    )

    assert orchestrator.main() == 0
    assert loop_env.switches == []
    assert [event for event, _, _ in loop_env.journal] == ["halt_respected"]


# ── A4 fix round 1: what makes the trigger correct rather than merely wired up ──


def test_the_lookup_keys_on_the_launch_model_not_the_samples_display_name(rotation, tmp_path):
    """The defect that made D4 dead on arrival.

    `usage.json` carries the statusline's display name — `"Opus 5"` in this repo's live
    file — which is in neither limits table, and `limits`' normaliser folds case and
    whitespace, not a missing `"Claude "` prefix. Keying on it meant `context_crossed`
    answered `False` on every claude task no matter how full the context was, and every
    test that injected a slug agreed with it. The lookup must use the model the task was
    *launched* with, which is the slug `--model` was given.
    """
    rotation.rows = {"claude-opus-5": ROW}       # keyed the way the tables key
    entry = _entry(tmp_path)

    assert orchestrator._context_rotations([entry], {}, {}) == 1
    assert rotation.resolved == ["claude-opus-5"]
    assert "Opus 5" not in rotation.resolved


def test_the_display_name_alone_would_not_have_resolved(rotation, tmp_path):
    """The other half of the same statement: with the launch model unavailable, the
    sample's own name is all that is left and it resolves to nothing."""
    rotation.launch_model = ""                   # no model configured for the role
    rotation.rows = {"claude-opus-5": ROW}

    assert orchestrator._context_rotations([_entry(tmp_path)], {}, {}) == 0
    assert rotation.resolved == ["Opus 5"]
    assert rotation.switches == []


# ── A4 fix round 2: C1 made the role table stop being the answer ──
#
# C1 landed after A4 and made a task's `model:` key override the role table at the launch
# site (`implementer.launch_implementer`). The ceiling this trigger measures against is
# per *model*, so from that commit on it was measuring a task with an override against a
# model the task is not running — rotating early on a bigger model (discarding a healthy
# conversation) and late on a smaller one (letting it fill past its handoff mark).

#: A second row, smaller than `ROW`, so "which model was the ceiling taken from" is
#: visible in the *decision* and not only in what got looked up.
SMALL_ROW = ModelLimits("Claude Haiku 4.5", 45_000, 65_000, 90_000, 110_000, 140_000)


def test_the_ceiling_is_keyed_on_the_model_the_task_is_running_not_the_role_default(
    rotation, tmp_path
):
    """A task pinned to a smaller model than the role's default fills up sooner.

    130K is under the role default's 120K handoff mark only if you ask the wrong model:
    on the model this task is actually running it is double the mark. Keyed on the role
    table this task would run on past its ceiling until it died of it.
    """
    rotation.rows = {"claude-opus-5": ROW, "claude-haiku-4-5": SMALL_ROW}
    rotation.context_tokens = 70_000            # under ROW's 120K, over SMALL_ROW's 65K
    entry = _entry(tmp_path, model="claude-haiku-4-5")

    assert orchestrator._context_rotations([entry], {}, {}) == 1
    assert rotation.resolved == ["claude-haiku-4-5"]


def test_a_bigger_model_than_the_role_default_is_not_rotated_early(rotation, tmp_path):
    """The costly half. A rotation discards a running agent's conversation, so measuring
    a task against a *smaller* model's ceiling throws away a session that had ~50K of
    headroom left."""
    rotation.launch_model = "claude-haiku-4-5"  # the role table's answer
    rotation.rows = {"claude-opus-5": ROW, "claude-haiku-4-5": SMALL_ROW}
    rotation.context_tokens = 70_000            # over SMALL_ROW's 65K, under ROW's 120K
    entry = _entry(tmp_path, model="claude-opus-5")

    assert orchestrator._context_rotations([entry], {}, {}) == 0
    assert rotation.resolved == ["claude-opus-5"]
    assert rotation.switches == []


def test_an_entry_written_before_the_model_key_still_rotates(rotation, tmp_path):
    """Entries already on disk when this ships carry no `model`. They must keep working:
    the role table is exactly what they were launched from, so it is the right stand-in.
    """
    rotation.rows = {"claude-opus-5": ROW}
    entry = _entry(tmp_path)
    assert "model" not in entry

    assert orchestrator._context_rotations([entry], {}, {}) == 1
    assert rotation.resolved == ["claude-opus-5"]


def test_a_blank_model_on_an_entry_is_not_treated_as_a_model(rotation, tmp_path):
    """`""` is "nothing recorded", not "a model called nothing" — the same reading the
    `backend` key gets. It falls through to the role table rather than resolving blank."""
    rotation.rows = {"claude-opus-5": ROW}

    assert orchestrator._context_rotations([_entry(tmp_path, model="  ")], {}, {}) == 1
    assert rotation.resolved == ["claude-opus-5"]


def test_an_unresolved_model_is_announced_once(rotation, tmp_path, capsys):
    """The suppression that hid the defect is gone: the loop names the id it could not
    place, once per process rather than once per poll."""
    rotation.rows = {}
    entries = [_entry(tmp_path), _entry(tmp_path, sid="impl-T4-1", task_id="T4")]

    orchestrator._context_rotations(entries, {}, {})
    orchestrator._context_rotations(entries, {}, {})

    printed = capsys.readouterr().out
    assert printed.count("no context-limit row for model") == 1
    assert "'claude-opus-5'" in printed
    assert rotation.switches == []


# ── the attribution precondition ──


def test_a_sample_that_names_no_session_never_rotates(rotation, tmp_path):
    """The precondition, pinned independently of whether any driver satisfies it.

    Both shipped drivers do satisfy it now (A5), so this no longer describes production —
    it pins the rule that has to hold whatever a driver reports. An unattributed reading
    is not weaker evidence about this implementer, it is evidence about somebody else, and
    what D4 spends on it is a live session's conversation. A driver that regressed to
    reporting `""` fails closed here instead of rotating on the operator's own numbers.
    """
    rotation.attributed_to = ""                  # a driver that does not, or no longer, attributes

    assert orchestrator._context_rotations([_entry(tmp_path)], {}, {}) == 0
    assert rotation.switches == []


def test_a_sample_attributed_to_another_session_never_rotates(rotation, tmp_path):
    """The failure this precondition exists to forbid: `usage.json` is written by the
    operator's own interactive Claude Code session, whose context has nothing to do with
    the implementer's. Acting on it would discard a healthy implementer's conversation on
    evidence about a different agent entirely."""
    rotation.attributed_to = "the-operators-own-session"

    assert orchestrator._context_rotations([_entry(tmp_path)], {}, {}) == 0
    assert rotation.switches == []


def test_an_attributed_sample_above_the_mark_rotates(rotation, tmp_path):
    """The other direction, pinned so the day a driver reports a per-session reading
    (item A5) the trigger is already covered and flips on with no change here."""
    entry = _entry(tmp_path)
    rotation.attributed_to = entry["session_id"]

    assert orchestrator._context_rotations([entry], {}, {}) == 1
    assert rotation.switches[0].reason == REASON_CONTEXT


def test_the_missing_attribution_is_announced_once(rotation, tmp_path, capsys):
    """Declining, but self-announcing: a crossing that could not be justified says so,
    once per backend per process. Silence is what let the first version look implemented —
    and now that D4 does fire, an unattributed crossing means a driver is misreporting,
    which is exactly the thing an operator must not have to infer from nothing."""
    rotation.attributed_to = ""
    entries = [_entry(tmp_path), _entry(tmp_path, sid="impl-T4-1", task_id="T4")]

    orchestrator._context_rotations(entries, {}, {})
    orchestrator._context_rotations(entries, {}, {})

    printed = capsys.readouterr().out
    assert printed.count("names no session") == 1
    assert "Usage.session_id" in printed


def test_nothing_is_announced_when_no_rotation_was_indicated(rotation, tmp_path, capsys):
    """The breadcrumb is about a crossing that could not be acted on, not about every
    poll of every task — a line per task per process saying "this one did not need
    rotating" would be noise, and noise is how a real signal gets ignored."""
    rotation.attributed_to = ""
    rotation.context_tokens = 10_000             # nowhere near the mark

    assert orchestrator._context_rotations([_entry(tmp_path)], {}, {}) == 0
    assert capsys.readouterr().out == ""


# ── the rotation bound ──


def test_a_task_stops_rotating_once_it_hits_the_cap(rotation, tmp_path):
    """The second half of the anti-loop guard. `_rotation_sample` certifies *when* a
    reading was taken; it cannot certify that the reading came down, so a feed that
    restamps itself every render while reporting a full context would rotate the same
    task once per poll forever. The cap bounds that absolutely."""
    entry = _entry(tmp_path, context_rotations=orchestrator.MAX_CONTEXT_ROTATIONS)

    assert orchestrator._context_rotations([entry], {}, {}) == 0
    assert rotation.switches == []


def test_a_capped_task_is_announced_once(rotation, tmp_path, capsys):
    entry = _entry(tmp_path, context_rotations=orchestrator.MAX_CONTEXT_ROTATIONS)

    orchestrator._context_rotations([entry], {}, {})
    orchestrator._context_rotations([entry], {}, {})

    printed = capsys.readouterr().out
    assert printed.count("already rotated") == 1
    assert "T1" in printed


def test_a_task_below_the_cap_still_rotates(rotation, tmp_path):
    entry = _entry(tmp_path, context_rotations=orchestrator.MAX_CONTEXT_ROTATIONS - 1)
    assert orchestrator._context_rotations([entry], {}, {}) == 1


def test_each_rotation_records_itself_on_the_entry(rotation, tmp_path):
    """Counted on the `in_flight` entry the way `backends_tried` is, so the record
    survives the relaunch that resets everything else about the session — `switch_task`
    copies keys it does not recognise onto the entry it returns."""
    orchestrator._context_rotations([_entry(tmp_path)], {}, {})
    assert rotation.switches[0].entry["context_rotations"] == 1

    orchestrator._rotation_notices.clear()
    orchestrator._context_rotations([_entry(tmp_path, context_rotations=2)], {}, {})
    assert rotation.switches[1].entry["context_rotations"] == 3


def test_a_nonsense_rotation_count_is_read_as_none(rotation, tmp_path):
    """The entry is a JSON document an operator can hand-edit; a bad value must not stop
    a rotation with a TypeError halfway through the poll."""
    for value in ("lots", None, -4, [1]):
        assert orchestrator._context_rotations_done({"context_rotations": value}) == 0
