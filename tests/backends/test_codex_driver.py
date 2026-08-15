"""The Codex driver: argv pinning, the resume handle, telemetry and exit classification.

`maestro/backends/` is new code, not an extraction, so these tests live here rather than
under `tests/characterization/`.

**Nothing in this file executes an agent CLI, spends a token of quota or reaches a
network.** An autouse fixture replaces the driver's `subprocess` module and the registry's
version probe with objects that raise, so a call that slipped past a monkeypatch fails the
test instead of costing money. The `codex app-server` fallback is exercised over a *real*
FIFO — a filesystem object, not a process — with a fake server running in a thread, which
is what lets the G4 "exit 0, zero bytes" case be tested honestly rather than asserted.

Each pinned behaviour names the research finding it encodes (F1–F3, G1–G6) so that a
future simplification has to argue with the evidence rather than with a bare assertion.
"""
from __future__ import annotations

import ast
import json
import os
import stat
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from maestro.backends import codex, registry
from maestro.backends.base import AgentBackend, Handle, LaunchSpec, Usage
from maestro.backends.codex import CodexBackend

BINARY = "/opt/tools/bin/codex"
THREAD_ID = "019ff499-deaf-77d1-acd4-e2135c856fa8"
NOW = datetime(2026, 8, 12, 10, 0, 0, tzinfo=timezone.utc)
NOW_ISO = "2026-08-12T10:00:00Z"

# The verified rate-limit record: a weekly window and nothing else (G5).
WEEKLY = {"used_percent": 98, "window_minutes": 10080, "resets_at": 1787036664}
# The same instant as WEEKLY["resets_at"], spelled the way ExitVerdict.reset_at spells it.
WEEKLY_RESET_ISO = "2026-08-18T07:04:24Z"


@pytest.fixture(autouse=True)
def _no_real_execution(monkeypatch):
    """No test may run a binary. Anything that tries fails loudly instead of quietly."""

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a test tried to execute a subprocess")

    monkeypatch.setattr(
        codex,
        "subprocess",
        SimpleNamespace(
            run=forbidden,
            Popen=forbidden,
            PIPE=subprocess.PIPE,
            STDOUT=subprocess.STDOUT,
            DEVNULL=subprocess.DEVNULL,
        ),
    )
    monkeypatch.setattr(registry, "_probe_version", forbidden)
    registry.clear_binary_cache()
    yield
    registry.clear_binary_cache()


class _Runner:
    """Stands in for `subprocess.run`; records the tmux command instead of running it."""

    def __init__(self):
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    @property
    def commands(self) -> list[str]:
        return [call[0][0] for call in self.calls]


@pytest.fixture
def runner():
    return _Runner()


@pytest.fixture
def journal():
    entries: list[tuple[str, str]] = []
    return SimpleNamespace(entries=entries, record=lambda e, d: entries.append((e, d)))


@pytest.fixture
def driver(tmp_path, runner, journal):
    return CodexBackend(
        binary=BINARY,
        python="/opt/venv/bin/python3",
        tmux_session="agents",
        codex_home=tmp_path / "codex-home",
        runner=runner,
        journal=journal.record,
        clock=lambda: NOW,
    )


@pytest.fixture
def spec(tmp_path):
    return LaunchSpec(
        task_id="T-1",
        session_id="sess-1",
        model="a-model",
        brief="do the thing",
        workspace=tmp_path / "ws",
        worktree=tmp_path / "wt",
    )


# ── helpers ──


def _launcher_value(source: str, name: str):
    """Read a constant out of a generated `launch.py` by parsing it — never by running it.

    Mirrors how the reference implementer's argv is pinned: the launcher is a program that
    starts an agent, so a test reads it, it does not execute it.
    """
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if name not in targets:
            continue
        value = node.value
        if isinstance(value, ast.Call):
            inner = ast.literal_eval(value.args[0])
            func = value.func
            if isinstance(func, ast.Attribute) and func.attr == "loads":
                return json.loads(inner)
            return inner
        return ast.literal_eval(value)
    raise AssertionError(f"{name} is not assigned in the generated launcher")


def _token_count_event(
    *,
    rate_limits=...,
    last_tokens=None,
    context_window=258400,
    total_tokens=2023580,
):
    info = {
        "total_token_usage": {"total_tokens": total_tokens},
        "model_context_window": context_window,
    }
    if last_tokens is not None:
        info["last_token_usage"] = {
            "total_tokens": last_tokens,
            "input_tokens": last_tokens,
        }
    payload = {"type": "token_count", "info": info}
    if rate_limits is not ...:
        payload["rate_limits"] = rate_limits
    return {"type": "event_msg", "payload": payload}


def _write_rollout(driver_: CodexBackend, thread_id: str, events, day=("2026", "08", "12")):
    directory = driver_.sessions_dir().joinpath(*day)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-08-12T09-00-00-{thread_id}.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


# ── capabilities (plan step 4) ──


def test_capabilities_are_the_verified_four(driver):
    """F1 answered the M2 open question: Codex has a documented resume handle."""
    caps = driver.capabilities()
    assert caps.native_resume is True
    assert caps.system_prompt_file is False   # `codex exec` has no full-replace prompt
    assert caps.usage_telemetry is True
    assert caps.sandbox is True


def test_capabilities_resolve_no_binary_and_run_nothing(monkeypatch, runner):
    def forbidden(*_a, **_k):
        raise AssertionError("capabilities() resolved a binary")

    monkeypatch.setattr(registry, "resolve_binary", forbidden)
    plain = CodexBackend(runner=runner)
    assert plain.capabilities().native_resume is True
    assert runner.calls == []


def test_driver_satisfies_the_agent_backend_protocol(driver):
    assert isinstance(driver, AgentBackend)


def test_module_declares_no_path_globals():
    """A `Path` global would bind at import time and could not be redirected by a test.

    House rule: a module with `Path`-valued globals must define a `Path`-valued `REPO` to
    be sandboxable. This module sidesteps that by having none — `codex_home()` is a
    method, so a test can point it anywhere and importing the module can write nothing.
    """
    offenders = {
        name
        for name, value in vars(codex).items()
        if not name.startswith("__") and isinstance(value, Path)
    }
    assert offenders == set()


# ── binary resolution (plan step 1) ──


def test_binary_path_is_the_pinned_absolute_path(monkeypatch, runner, tmp_path):
    resolved = registry.BinaryInfo(tool="codex", path=tmp_path / "codex", version="codex-cli 9.9.9")
    monkeypatch.setattr(registry, "resolve_binary", lambda *a, **k: resolved)
    plain = CodexBackend(runner=runner)
    assert plain.binary_path() == str(tmp_path / "codex")
    assert plain.version() == "codex-cli 9.9.9"
    assert plain.binary_warning() is None


def test_binary_resolution_happens_once(monkeypatch, runner, tmp_path):
    calls = []

    def resolve(*_a, **_k):
        calls.append(1)
        return registry.BinaryInfo(tool="codex", path=tmp_path / "codex", version="v")

    monkeypatch.setattr(registry, "resolve_binary", resolve)
    plain = CodexBackend(runner=runner)
    plain.binary_path()
    plain.binary_path()
    assert len(calls) == 1


def test_binary_path_falls_back_to_the_bare_name(monkeypatch, runner):
    monkeypatch.setattr(registry, "resolve_binary", lambda *a, **k: None)
    assert CodexBackend(runner=runner).binary_path() == "codex"


def test_a_second_codex_on_path_is_a_journalled_warning_not_a_failure(
    monkeypatch, runner, journal, spec, tmp_path
):
    """Two installs at two versions is live on real machines; the resolved one still works."""
    info = registry.BinaryInfo(
        tool="codex",
        path=tmp_path / "local" / "codex",
        version="codex-cli 9.9.9",
        shadowed=((tmp_path / "usr" / "codex", "codex-cli 8.8.8"),),
    )
    monkeypatch.setattr(registry, "resolve_binary", lambda *a, **k: info)
    plain = CodexBackend(
        runner=runner, journal=journal.record, codex_home=tmp_path / "home", clock=lambda: NOW
    )

    plain.launch(spec)

    assert [event for event, _ in journal.entries] == ["codex_binary_warning"]
    detail = journal.entries[0][1]
    assert "9.9.9" in detail and "8.8.8" in detail


# ── launch argv (plan step 2, findings F2/G2) ──


def test_launch_argv_is_the_verified_exec_invocation(driver, spec):
    assert driver.launch_argv(spec) == [
        BINARY,
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-c",
        'approval_policy="never"',
        "-m",
        "a-model",
        "-s",
        "workspace-write",
        "-C",
        str(spec.worktree),
        "do the thing",
    ]


def test_launch_argv_never_emits_a_flag_exec_rejects(driver, spec):
    """F2: `codex exec` exits 2 on `-a`/`--ask-for-approval`/`--full-auto`. G2: `--last`
    hangs for minutes scanning every rollout on disk."""
    argv = driver.launch_argv(spec)
    for rejected in ("-a", "--ask-for-approval", "--full-auto", "--last"):
        assert rejected not in argv


def test_approval_policy_is_a_config_override(driver, spec):
    argv = driver.launch_argv(spec)
    assert argv[argv.index("-c") + 1] == 'approval_policy="never"'


def test_the_brief_is_positional_and_last(driver, spec):
    assert driver.launch_argv(spec)[-1] == spec.brief


@pytest.mark.parametrize("mode", ["read-only", "workspace-write", "danger-full-access"])
def test_the_three_sandbox_spellings_are_accepted_verbatim(driver, spec, mode):
    argv = driver.launch_argv(
        LaunchSpec(**{**spec.__dict__, "sandbox": mode})
    )
    assert argv[argv.index("-s") + 1] == mode


def test_the_default_sandbox_lets_the_implementer_write_its_worktree(driver, spec):
    argv = driver.launch_argv(spec)
    assert argv[argv.index("-s") + 1] == "workspace-write"
    assert codex.DEFAULT_SANDBOX == "workspace-write"


def test_an_unknown_sandbox_mode_is_refused_before_launch(driver, spec):
    with pytest.raises(ValueError) as excinfo:
        driver.launch_argv(LaunchSpec(**{**spec.__dict__, "sandbox": "full-auto"}))
    assert "workspace-write" in str(excinfo.value)


def test_add_dirs_are_emitted_before_the_brief(tmp_path, runner, spec):
    plain = CodexBackend(binary=BINARY, runner=runner, add_dirs=[tmp_path / "shared"])
    argv = plain.launch_argv(spec)
    assert argv[-3:] == ["--add-dir", str(tmp_path / "shared"), spec.brief]


# ── launch (plan step 2) ──


def test_launch_writes_the_workspace_files_and_opens_a_window(driver, spec, runner):
    handle = driver.launch(spec)

    workspace = Path(spec.workspace)
    assert (workspace / "brief.txt").read_text() == "do the thing"
    assert (workspace / "launch.py").exists()
    assert json.loads((workspace / "codex_launch.json").read_text())["model"] == "a-model"
    assert handle == Handle(
        backend="codex",
        session_id="sess-1",
        native_id=None,          # the thread id does not exist until the CLI announces it
        workspace=workspace,
        window="impl-T-1",
    )
    assert len(runner.commands) == 1


def test_launch_runs_the_reference_tmux_invocation(driver, spec, runner):
    driver.launch(spec)
    command = runner.commands[0]
    assert command.startswith("tmux new-window -t agents -n impl-T-1 ")
    assert f"cd {spec.worktree} && /opt/venv/bin/python3 " in command
    assert str(Path(spec.workspace) / "launch.py") in command
    assert runner.calls[0][1] == {"shell": True, "check": True}


def test_the_generated_launcher_is_executable(driver, spec):
    driver.launch(spec)
    mode = (Path(spec.workspace) / "launch.py").stat().st_mode
    assert mode & stat.S_IXUSR


def test_launch_clears_a_thread_id_left_by_an_earlier_run(driver, spec):
    workspace = Path(spec.workspace)
    workspace.mkdir(parents=True)
    (workspace / "codex_thread_id.txt").write_text("stale-thread")

    driver.launch(spec)

    assert not (workspace / "codex_thread_id.txt").exists()
    assert driver.thread_id_for(workspace) is None


# ── the generated launcher ──


def test_the_launcher_compiles_and_carries_the_argv(driver, spec):
    driver.launch(spec)
    source = (Path(spec.workspace) / "launch.py").read_text()
    compile(source, "launch.py", "exec")   # parsed and compiled, never executed

    assert _launcher_value(source, "ARGV") == driver.launch_argv(spec)
    assert _launcher_value(source, "FALLBACK_ARGV") is None
    assert _launcher_value(source, "LOG") == str(Path(spec.workspace) / "impl.log")
    assert _launcher_value(source, "THREAD_FILE") == str(
        Path(spec.workspace) / "codex_thread_id.txt"
    )


def test_the_launcher_merges_stderr_into_one_log(driver, spec):
    """The reference merges the streams, so every captured sample is stream-agnostic."""
    driver.launch(spec)
    source = (Path(spec.workspace) / "launch.py").read_text()
    assert "stderr=subprocess.STDOUT" in source


def test_the_launcher_captures_the_thread_id_from_the_first_event(driver, spec):
    driver.launch(spec)
    source = (Path(spec.workspace) / "launch.py").read_text()
    assert "thread.started" in source
    assert "THREAD_FILE.write_text" in source


def test_a_fresh_launch_opens_impl_log_in_write_mode(driver, spec):
    """A fresh launch has no prior `impl.log` to protect, so it truncates as before."""
    driver.launch(spec)
    source = (Path(spec.workspace) / "launch.py").read_text()
    assert _launcher_value(source, "LOG_MODE") == "w"


def test_a_brief_full_of_quotes_and_newlines_survives_into_the_launcher(driver, spec):
    nasty = "line one\n'single' \"double\" \\backslash\\ {braces} — dash"
    driver.launch(LaunchSpec(**{**spec.__dict__, "brief": nasty}))
    source = (Path(spec.workspace) / "launch.py").read_text()
    compile(source, "launch.py", "exec")
    assert _launcher_value(source, "ARGV")[-1] == nasty


# ── resume (plan step 3, findings G1/G2/G3) ──


def test_resume_argv_puts_the_flags_before_the_resume_subcommand(driver, spec):
    """G1: `resume` accepts none of -s/-C/--add-dir/-p, but they work ahead of it."""
    argv = driver.resume_argv(spec, THREAD_ID, "carry on")
    assert argv == [
        BINARY,
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-c",
        'approval_policy="never"',
        "-m",
        "a-model",
        "-s",
        "workspace-write",
        "-C",
        str(spec.worktree),
        "resume",
        THREAD_ID,
        "carry on",
    ]
    resume_at = argv.index("resume")
    for flag in ("-s", "-C", "-m", "--json", "--skip-git-repo-check", "-c"):
        assert argv.index(flag) < resume_at


def test_resume_argv_never_uses_last(driver, spec):
    """G2: `--last` hung for 150s+ scanning ~17k rollout files. Never in automation."""
    assert "--last" not in driver.resume_argv(spec, THREAD_ID, "carry on")


def test_resume_argv_requires_an_explicit_thread_id(driver, spec):
    with pytest.raises(ValueError):
        driver.resume_argv(spec, "", "carry on")


def test_resume_uses_the_thread_id_recorded_by_the_launcher(driver, spec):
    handle = driver.launch(spec)
    (Path(spec.workspace) / "codex_thread_id.txt").write_text(THREAD_ID)

    resumed = driver.resume(handle, "carry on")

    assert resumed.native_id == THREAD_ID
    source = (Path(spec.workspace) / "launch.py").read_text()
    argv = _launcher_value(source, "ARGV")
    assert argv[argv.index("resume") + 1 : argv.index("resume") + 3] == [
        THREAD_ID,
        "carry on",
    ]


def test_resume_falls_back_to_a_fresh_brief_when_the_handle_is_dead(driver, spec):
    """G3: a bogus thread id fails in milliseconds with no model call, so attempting the
    resume *is* the probe — the launcher relaunches with a brief when it sees that."""
    handle = driver.launch(spec)
    (Path(spec.workspace) / "codex_thread_id.txt").write_text(THREAD_ID)

    driver.resume(handle, "carry on with the handoff")

    source = (Path(spec.workspace) / "launch.py").read_text()
    compile(source, "launch.py", "exec")
    fallback = _launcher_value(source, "FALLBACK_ARGV")
    assert "resume" not in fallback
    assert fallback[-1] == "carry on with the handoff"
    assert codex.DEAD_THREAD_MARKER in source
    assert codex.DEAD_THREAD_MARKER == "no rollout found for thread id"


def test_resume_without_any_thread_id_just_launches_a_fresh_brief(driver, spec):
    handle = driver.launch(spec)

    resumed = driver.resume(handle, "start again")

    assert resumed.native_id is None
    source = (Path(spec.workspace) / "launch.py").read_text()
    assert "resume" not in _launcher_value(source, "ARGV")
    assert _launcher_value(source, "FALLBACK_ARGV") is None


def test_resume_reads_the_launch_parameters_a_handle_does_not_carry(driver, spec, runner):
    """A restarted orchestrator holds a handle but not the spec that produced it."""
    handle = driver.launch(spec)
    (Path(spec.workspace) / "codex_thread_id.txt").write_text(THREAD_ID)

    fresh_driver = CodexBackend(binary=BINARY, runner=runner, clock=lambda: NOW)
    fresh_driver.resume(handle, "carry on")

    argv = _launcher_value((Path(spec.workspace) / "launch.py").read_text(), "ARGV")
    assert argv[argv.index("-m") + 1] == "a-model"
    assert argv[argv.index("-C") + 1] == str(spec.worktree)


def test_resume_says_so_when_it_cannot_reconstruct_a_spec(driver, tmp_path):
    handle = Handle(
        backend="codex",
        session_id="sess-1",
        native_id=THREAD_ID,
        workspace=tmp_path / "empty-ws",
        window="impl-T-1",
    )
    (tmp_path / "empty-ws").mkdir()
    with pytest.raises(ValueError) as excinfo:
        driver.resume(handle, "carry on")
    assert "codex_launch.json" in str(excinfo.value)


def test_resume_opens_impl_log_in_append_mode(driver, spec):
    """A resume must never truncate `impl.log` — that is the reactive quota net's evidence.

    Mirrors the Claude driver's `launcher_source(..., mode="a")` on resume: truncating
    would destroy everything a prior run (or runs) already wrote to the log.
    """
    handle = driver.launch(spec)
    (Path(spec.workspace) / "codex_thread_id.txt").write_text(THREAD_ID)

    driver.resume(handle, "carry on")

    source = (Path(spec.workspace) / "launch.py").read_text()
    assert _launcher_value(source, "LOG_MODE") == "a"
    assert _launcher_value(source, "LOG_MODE") != "w"


def test_resume_records_the_prompt_it_handed_over(driver, spec):
    handle = driver.launch(spec)
    driver.resume(handle, "the handoff brief")
    assert (Path(spec.workspace) / "resume_prompt.txt").read_text() == "the handoff brief"


# ── the resume handle itself (F1) ──


def test_thread_id_comes_from_the_first_jsonl_event():
    line = json.dumps({"type": "thread.started", "thread_id": THREAD_ID})
    assert codex.thread_id_from_line(line) == THREAD_ID


@pytest.mark.parametrize(
    "line",
    [
        "",
        "not json at all",
        "{ broken",
        json.dumps({"type": "turn.completed", "thread_id": "other"}),
        json.dumps({"type": "thread.started"}),
        json.dumps({"type": "thread.started", "thread_id": ""}),
    ],
)
def test_a_line_that_is_not_the_started_event_yields_no_thread_id(line):
    assert codex.thread_id_from_line(line) is None


def test_thread_id_from_text_takes_the_first_one():
    text = "\n".join(
        [
            "some preamble",
            json.dumps({"type": "thread.started", "thread_id": THREAD_ID}),
            json.dumps({"type": "thread.started", "thread_id": "a-later-run"}),
        ]
    )
    assert codex.thread_id_from_text(text) == THREAD_ID


def test_thread_id_for_prefers_the_recorded_file(driver, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "codex_thread_id.txt").write_text(f"  {THREAD_ID}\n")
    (workspace / "impl.log").write_text(
        json.dumps({"type": "thread.started", "thread_id": "from-the-log"})
    )
    assert driver.thread_id_for(workspace) == THREAD_ID


def test_thread_id_for_falls_back_to_the_log(driver, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "impl.log").write_text(
        json.dumps({"type": "thread.started", "thread_id": THREAD_ID}) + "\n"
    )
    assert driver.thread_id_for(workspace) == THREAD_ID


def test_thread_id_for_is_none_on_an_empty_workspace(driver, tmp_path):
    assert driver.thread_id_for(tmp_path) is None


def test_is_dead_thread_only_fires_on_a_failure(driver):
    message = "ERROR: no rollout found for thread id 00000000 (code -32600)"
    assert codex.is_dead_thread(1, message) is True
    assert codex.is_dead_thread(0, message) is False
    assert codex.is_dead_thread(1, "something else went wrong") is False


# ── usage from the rollout (plan step 5, findings G5/G6) ──


def test_usage_keys_windows_by_duration_not_by_field_name(driver):
    """G5: the sampled account's `primary` *is* the weekly window and `secondary` is null.
    A driver that read the names would publish a five-hour window that can never fire."""
    _write_rollout(
        driver, THREAD_ID, [_token_count_event(rate_limits={"limit_id": "codex", "primary": WEEKLY, "secondary": None})]
    )

    usage = driver.usage_from_rollout(THREAD_ID)

    assert set(usage.windows) == {10080}
    assert usage.seven_day.used_pct == 98.0
    assert usage.seven_day.resets_at == 1787036664
    assert usage.five_hour is None
    assert usage.max_used_pct() == 98.0
    assert usage.updated_at == NOW_ISO


def test_usage_reads_the_last_snapshot_in_the_rollout(driver):
    early = dict(WEEKLY, used_percent=10)
    late = dict(WEEKLY, used_percent=71)
    _write_rollout(
        driver,
        THREAD_ID,
        [
            _token_count_event(rate_limits={"primary": early}),
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "hi"}},
            _token_count_event(rate_limits={"primary": late}),
        ],
    )
    assert driver.usage_from_rollout(THREAD_ID).seven_day.used_pct == 71.0


def test_usage_keeps_the_last_snapshot_that_actually_carried_limits(driver):
    """A later turn can report token counts without a fresh limit snapshot; dropping to
    no reading at all would be worse than using the newest reading there is."""
    _write_rollout(
        driver,
        THREAD_ID,
        [
            _token_count_event(rate_limits={"primary": WEEKLY}),
            _token_count_event(last_tokens=20000),   # no rate_limits on this one
        ],
    )
    usage = driver.usage_from_rollout(THREAD_ID)
    assert usage.seven_day.used_pct == 98.0


def test_usage_finds_the_rollout_by_thread_id_under_the_dated_directories(driver):
    path = _write_rollout(driver, THREAD_ID, [_token_count_event(rate_limits={"primary": WEEKLY})])
    _write_rollout(driver, "another-thread", [_token_count_event(rate_limits={"primary": dict(WEEKLY, used_percent=1)})])
    assert driver.rollout_path(THREAD_ID) == path


def test_usage_is_none_when_the_thread_has_no_rollout(driver):
    assert driver.usage_from_rollout("no-such-thread") is None
    assert "no rollout file" in driver.last_telemetry_error


def test_usage_is_none_when_the_rollout_holds_no_token_count(driver):
    _write_rollout(driver, THREAD_ID, [{"type": "event_msg", "payload": {"type": "agent_message"}}])
    assert driver.usage_from_rollout(THREAD_ID) is None
    assert "no token_count" in driver.last_telemetry_error


def test_a_truncated_rollout_line_is_skipped_not_fatal(driver):
    path = _write_rollout(driver, THREAD_ID, [_token_count_event(rate_limits={"primary": WEEKLY})])
    path.write_text(path.read_text() + '{"type": "event_msg", "payl', encoding="utf-8")
    assert driver.usage_from_rollout(THREAD_ID).seven_day.used_pct == 98.0


def test_usage_labels_the_sample_with_the_model_the_rollout_names(driver):
    _write_rollout(
        driver,
        THREAD_ID,
        [
            {"type": "turn_context", "payload": {"model": "a-codex-model"}},
            _token_count_event(rate_limits={"primary": WEEKLY}),
        ],
    )
    assert driver.usage_from_rollout(THREAD_ID).model == "a-codex-model"


# ── context_used_pct is nullable (G6) ──


def test_context_used_pct_is_none_when_only_a_cumulative_total_exists(driver):
    """G6: `total_token_usage.total_tokens` is cumulative and routinely exceeds the
    window (2,023,580 against 258,400 in the sampled record). Reading it as a percentage
    is the exact mistake this refuses to make."""
    _write_rollout(driver, THREAD_ID, [_token_count_event(rate_limits={"primary": WEEKLY})])
    usage = driver.usage_from_rollout(THREAD_ID)
    assert usage.context_used_pct is None


def test_a_missing_context_percentage_never_hides_quota_pressure(driver):
    """The load-bearing consequence of G6: `None` must not disable switching."""
    _write_rollout(driver, THREAD_ID, [_token_count_event(rate_limits={"primary": WEEKLY})])
    usage = driver.usage_from_rollout(THREAD_ID)
    assert usage.context_used_pct is None
    assert usage.max_used_pct() == 98.0


def test_context_used_pct_is_computed_from_the_per_turn_usage(driver):
    info = {
        "model_context_window": 112000,
        "last_token_usage": {"total_tokens": 62000},
    }
    # (62000 - 12000) / (112000 - 12000) = 50%
    assert codex.context_used_pct(info) == pytest.approx(50.0)


def test_context_used_pct_is_none_when_the_formula_lands_out_of_range(driver):
    """The TUI's exact formula was never traced, so a number outside 0–100 means the
    assumption is wrong for that record — say "unknown", not something untrue."""
    assert codex.context_used_pct(
        {"model_context_window": 100000, "last_token_usage": {"total_tokens": 999999}}
    ) is None


@pytest.mark.parametrize(
    "info",
    [
        {},
        None,
        {"model_context_window": 0, "last_token_usage": {"total_tokens": 10}},
        {"model_context_window": 100000},
        {"last_token_usage": {"total_tokens": 10}},
        {"model_context_window": 5000, "last_token_usage": {"total_tokens": 10}},
    ],
)
def test_context_used_pct_declines_to_guess(info):
    assert codex.context_used_pct(info) is None


# ── window normalisation across both wire shapes ──


def test_windows_read_the_rollouts_snake_case(driver):
    assert codex.windows_from_rate_limits({"primary": WEEKLY}) == {
        10080: codex.WindowUsage(used_pct=98.0, resets_at=1787036664)
    }


def test_windows_read_the_app_servers_camel_case(driver):
    camel = {"usedPercent": 98, "windowDurationMins": 10080, "resetsAt": 1787036664}
    assert codex.windows_from_rate_limits({"primary": camel}) == {
        10080: codex.WindowUsage(used_pct=98.0, resets_at=1787036664)
    }


def test_a_window_with_no_duration_is_dropped(driver):
    """An unlabelled window cannot be compared against a threshold, so it is not kept."""
    assert codex.windows_from_rate_limits({"primary": {"used_percent": 98}}) == {}


def test_a_null_secondary_window_is_simply_absent(driver):
    windows = codex.windows_from_rate_limits(
        {"limit_id": "codex", "primary": WEEKLY, "secondary": None}
    )
    assert set(windows) == {10080}


def test_two_records_of_one_duration_keep_the_higher_reading(driver):
    windows = codex.windows_from_rate_limits(
        {"primary": dict(WEEKLY, used_percent=40), "secondary": dict(WEEKLY, used_percent=90)}
    )
    assert windows[10080].used_pct == 90.0


def test_a_relative_reset_is_resolved_against_the_clock(driver):
    windows = codex.windows_from_rate_limits(
        {"primary": {"window_minutes": 300, "used_percent": 5, "resets_in_seconds": 600}},
        now=NOW,
    )
    assert windows[300].resets_at == int(NOW.timestamp()) + 600


# ── the app-server fallback (plan step 5, finding G4) ──


class _FakeAppServer:
    """A stand-in for `codex app-server`, in a thread — no process, no binary, no quota.

    It reads the *real* FIFO the driver created, which is what makes the G4 behaviour
    testable: `saw_eof_before_reply` records whether stdin was closed before the answer
    was written, i.e. whether the driver forgot to hold it open.
    """

    def __init__(self, *, reply=None, answer=True, hang=False, exit_code=0):
        self.reply = reply
        self.answer = answer
        self.hang = hang
        self.exit_code = exit_code
        self.argv = None
        self.requests: list[str] = []
        self.saw_eof_before_reply = False
        self.stdout = None
        self.returncode = None
        self._thread = None

    def __call__(self, argv, stdin=None, stdout=None, stderr=None, **kwargs):
        self.argv = list(argv)
        read_fd, write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "rb", 0)
        child_stdin = os.dup(stdin)   # as a real child would: its own copy
        self._thread = threading.Thread(target=self._serve, args=(child_stdin, write_fd))
        self._thread.daemon = True
        self._thread.start()
        return self

    def _serve(self, stdin_fd, write_fd):
        buffered = b""
        try:
            while codex.APP_SERVER_METHOD.encode() not in buffered:
                chunk = os.read(stdin_fd, 4096)
                if not chunk:
                    self.saw_eof_before_reply = True
                    break
                buffered += chunk
            self.requests = [line for line in buffered.decode().splitlines() if line.strip()]
            if self.hang:
                while os.read(stdin_fd, 4096):   # answer nothing; wait for our EOF
                    pass
            elif self.answer and not self.saw_eof_before_reply:
                os.write(write_fd, (json.dumps(self.reply) + "\n").encode())
        finally:
            os.close(stdin_fd)
            os.close(write_fd)
            self.returncode = self.exit_code

    # the parts of a Popen the driver actually uses
    def poll(self):
        return self.returncode if self._thread is not None and not self._thread.is_alive() else None

    def wait(self, timeout=None):
        self._thread.join(timeout)
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


_APP_SERVER_REPLY = {
    "jsonrpc": "2.0",
    "id": 2,
    "result": {
        "rateLimits": {
            "primary": {"usedPercent": 98, "windowDurationMins": 10080, "resetsAt": 1787036664},
            "secondary": None,
            "planType": "a-plan",
        }
    },
}


def _with_server(tmp_path, server, **kwargs):
    return CodexBackend(
        binary=BINARY,
        codex_home=tmp_path / "codex-home",
        popen=server,
        clock=lambda: NOW,
        **kwargs,
    )


def test_app_server_answers_a_rate_limit_sample(tmp_path):
    server = _FakeAppServer(reply=_APP_SERVER_REPLY)
    usage = _with_server(tmp_path, server).usage_from_app_server(timeout_sec=5)

    assert usage == Usage(
        context_used_pct=None,
        windows={10080: codex.WindowUsage(used_pct=98.0, resets_at=1787036664)},
        model=None,
        updated_at=NOW_ISO,
    )
    assert server.argv == [BINARY, "app-server"]


def test_app_server_asks_the_documented_method(tmp_path):
    server = _FakeAppServer(reply=_APP_SERVER_REPLY)
    _with_server(tmp_path, server).usage_from_app_server(timeout_sec=5)

    methods = [json.loads(line)["method"] for line in server.requests]
    assert methods[0] == "initialize"
    assert methods[-1] == "account/rateLimits/read"
    assert codex.APP_SERVER_METHOD == "account/rateLimits/read"


def test_app_server_stdin_stays_open_until_the_reply_arrives(tmp_path):
    """G4: on EOF the server tears down its stdout writer before flushing. Closing stdin
    after writing the request would lose the answer every time."""
    server = _FakeAppServer(reply=_APP_SERVER_REPLY)
    usage = _with_server(tmp_path, server).usage_from_app_server(timeout_sec=5)

    assert server.saw_eof_before_reply is False
    assert usage is not None


def test_exit_zero_with_no_output_is_a_failed_sample_not_an_absence_of_limits(tmp_path):
    """G4's trap: the silent no-answer looks exactly like "this feature does not exist"."""
    driver_ = _with_server(tmp_path, _FakeAppServer(answer=False, exit_code=0))

    assert driver_.usage_from_app_server(timeout_sec=5) is None
    assert driver_.usage() is None            # and never Usage(windows={})
    assert "silent" in driver_.last_telemetry_error


def test_a_server_that_never_answers_times_out_into_a_failed_sample(tmp_path):
    driver_ = _with_server(tmp_path, _FakeAppServer(hang=True))
    assert driver_.usage_from_app_server(timeout_sec=0.3) is None
    assert "did not answer" in driver_.last_telemetry_error


def test_an_answer_without_rate_limits_is_a_failed_sample(tmp_path):
    reply = {"jsonrpc": "2.0", "id": 2, "error": {"code": -32601, "message": "unknown"}}
    driver_ = _with_server(tmp_path, _FakeAppServer(reply=reply))
    assert driver_.usage_from_app_server(timeout_sec=5) is None
    assert driver_.last_telemetry_error


def test_a_snapshot_reporting_no_windows_is_a_reading_not_a_failure(tmp_path):
    """The distinction G4 turns on: an answered sample with nothing in it is a `Usage`."""
    reply = {"jsonrpc": "2.0", "id": 2, "result": {"rateLimits": {"secondary": None}}}
    usage = _with_server(tmp_path, _FakeAppServer(reply=reply)).usage_from_app_server(timeout_sec=5)
    assert usage is not None
    assert usage.windows == {}
    assert usage.max_used_pct() is None


# ── usage() source selection ──


def test_usage_prefers_the_rollout_and_starts_no_app_server(driver, spec, tmp_path):
    def forbidden(*_a, **_k):
        raise AssertionError("the rollout was available; no app-server should start")

    driver._popen = forbidden
    _write_rollout(driver, THREAD_ID, [_token_count_event(rate_limits={"primary": WEEKLY})])
    handle = Handle(
        backend="codex",
        session_id="sess-1",
        native_id=THREAD_ID,
        workspace=tmp_path / "ws",
        window="impl-T-1",
    )
    assert driver.usage(handle).seven_day.used_pct == 98.0


def test_usage_falls_back_to_the_app_server_when_no_run_is_active(tmp_path):
    server = _FakeAppServer(reply=_APP_SERVER_REPLY)
    usage = _with_server(tmp_path, server).usage()
    assert usage.seven_day.used_pct == 98.0


def test_usage_uses_the_thread_id_recorded_in_the_workspace(driver, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "codex_thread_id.txt").write_text(THREAD_ID)
    _write_rollout(driver, THREAD_ID, [_token_count_event(rate_limits={"primary": WEEKLY})])

    handle = Handle(
        backend="codex",
        session_id="sess-1",
        native_id=None,
        workspace=workspace,
        window="impl-T-1",
    )
    assert driver.usage(handle).seven_day.used_pct == 98.0


# ── parse_exit (plan step 6) ──


def test_a_clean_run_is_ok(driver):
    assert driver.parse_exit(0, "") == codex.ExitVerdict(kind="ok")


def test_a_failed_run_is_crashed_with_its_last_line(driver):
    verdict = driver.parse_exit(1, "starting\nsomething went wrong\n")
    assert verdict.kind == "crashed"
    assert verdict.evidence == "something went wrong"


def test_the_argv_rejection_exit_code_is_a_crash_not_a_quota_stop(driver):
    """F2: exit 2 is `unexpected argument` — a bug in the invocation, not exhaustion."""
    assert driver.parse_exit(2, "error: unexpected argument '--full-auto' found").kind == "crashed"


def test_a_dead_resume_handle_is_a_crash_that_says_so(driver):
    verdict = driver.parse_exit(1, "ERROR: no rollout found for thread id x (code -32600)")
    assert verdict.kind == "crashed"
    assert codex.DEAD_THREAD_MARKER in verdict.evidence


def test_rate_limit_reached_is_quota_exhausted(driver):
    line = json.dumps(
        {
            "type": "error",
            "message": "rate limit",
            "rate_limit_reached_type": "weekly",
            "resets_at": 1787036664,
        }
    )
    verdict = driver.parse_exit(1, line)
    assert verdict.kind == "quota_exhausted"
    assert verdict.reset_at == WEEKLY_RESET_ISO
    assert verdict.evidence


def test_a_plain_text_rate_limit_reached_notice_also_counts(driver):
    verdict = driver.parse_exit(1, "stream error: rate_limit_reached; giving up")
    assert verdict.kind == "quota_exhausted"
    assert "rate_limit_reached" in verdict.evidence


def test_a_window_at_a_hundred_percent_on_a_failed_run_is_quota_exhausted(driver):
    tail = json.dumps(
        _token_count_event(rate_limits={"primary": dict(WEEKLY, used_percent=100)})
    )
    verdict = driver.parse_exit(1, tail)
    assert verdict.kind == "quota_exhausted"
    assert verdict.reset_at == WEEKLY_RESET_ISO


def test_a_run_that_finished_on_its_last_percent_still_succeeded(driver):
    """Exit 0 means the work is done; calling it exhausted would throw a finished task away."""
    tail = json.dumps(
        _token_count_event(rate_limits={"primary": dict(WEEKLY, used_percent=100)})
    )
    assert driver.parse_exit(0, tail).kind == "ok"


def test_the_reset_falls_back_to_a_conservative_hour(driver):
    verdict = driver.parse_exit(1, json.dumps({"rate_limit_reached_type": "weekly"}))
    assert verdict.kind == "quota_exhausted"
    assert verdict.reset_at == "2026-08-12T11:00:00Z"


def test_a_reset_already_in_the_past_is_not_trusted(driver):
    tail = json.dumps({"rate_limit_reached_type": "weekly", "resets_at": 1000})
    assert driver.parse_exit(1, tail).reset_at == "2026-08-12T11:00:00Z"


def test_the_other_clis_exhaustion_wording_is_not_borrowed(driver):
    """`quota._LIMIT_RE` encodes the *other* backend's phrasing. Reusing it here would
    classify unrelated Codex output as exhaustion — capabilities, not shared regexes."""
    from maestro import quota

    tail = "Claude usage limit reached — you have hit your usage limit"
    assert quota._LIMIT_RE.search(tail)          # it matches, for the other driver
    assert driver.parse_exit(1, tail).kind == "crashed"


def test_parse_exit_survives_garbage(driver):
    assert driver.parse_exit(0, "{not json\n\x00\n").kind == "ok"
    assert driver.parse_exit(1, "").kind == "crashed"


def test_parse_exit_runs_nothing(driver, runner):
    driver.parse_exit(1, json.dumps({"rate_limit_reached_type": "weekly"}))
    assert runner.calls == []
