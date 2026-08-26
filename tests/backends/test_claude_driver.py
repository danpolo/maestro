"""The Claude driver: argv, generated launcher, exit verdict and usage.

`maestro/backends/` is new code, not an extraction, so these tests live here rather than
under `tests/characterization/` — there is no legacy counterpart to characterise. What
*is* characterised is the shape the driver has to reproduce: the reference's generated
`launch.py` is rebuilt literally in `_reference_launcher` below, straight out of
`maestro/implementer.py`, and the driver's output is compared against it byte for byte.

**No test here spends a token or touches a network.** An autouse fixture replaces
`subprocess.run` for the whole module with a recorder that *refuses* anything that is not
a `tmux new-window` line, so a driver that tried to exec an agent CLI would fail the
suite rather than quietly bill an account. `usage.json` and the workspace both live under
`tmp_path`, and `maestro.quota`'s own `USAGE_JSON` global is repointed there too so the
reset-time delegation cannot read the live repo.
"""
from __future__ import annotations

import ast
import json
import subprocess
import uuid
from pathlib import Path

import pytest

from maestro import quota
from maestro.backends import registry
from maestro.backends.base import (
    FIVE_HOUR_MINUTES,
    SEVEN_DAY_MINUTES,
    AgentBackend,
    Capabilities,
    Completion,
    CompletionSpec,
    Handle,
    LaunchSpec,
)
from maestro.backends import claude as claude_mod
from maestro.backends.claude import ClaudeBackend

MODEL = "a-model-id"
SESSION_UUID = "11111111-2222-3333-4444-555555555555"


# ── the no-quota firewall ──


class _Runs:
    """Records every `subprocess.run` and rejects anything but a tmux window open."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        command = args[0] if args else kwargs.get("args")
        text = command if isinstance(command, str) else " ".join(map(str, command))
        assert text.startswith("tmux new-window "), (
            f"a driver test tried to run something other than tmux: {text[:120]!r}"
        )
        self.calls.append((args, kwargs))
        return subprocess.CompletedProcess(args=command, returncode=0)

    @property
    def only(self) -> str:
        assert len(self.calls) == 1, f"expected exactly one run, got {len(self.calls)}"
        return self.calls[0][0][0]


@pytest.fixture(autouse=True)
def runs(monkeypatch):
    recorder = _Runs()
    monkeypatch.setattr(subprocess, "run", recorder)
    return recorder


@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path, monkeypatch):
    """Repoint every path global away from the live repo, for this module and `quota`.

    `parse_exit` delegates the reset time to `maestro.quota._resolve_limit_reset`, which
    consults `quota.USAGE_JSON`. Left alone it would read the developer's real usage
    sample and make the reset assertions depend on the machine.
    """
    monkeypatch.setattr(claude_mod, "REPO", tmp_path / "repo")
    monkeypatch.setattr(claude_mod, "USAGE_JSON", tmp_path / "repo" / "usage.json")
    monkeypatch.setattr(claude_mod, "VENV_PYTHON", tmp_path / "repo" / "venv-python3")
    monkeypatch.setattr(quota, "USAGE_JSON", tmp_path / "repo" / "quota-usage.json")


@pytest.fixture
def workspace(tmp_path):
    path = tmp_path / "workspaces" / "impl-T1-1"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def worktree(tmp_path):
    path = tmp_path / "wt-T1"
    path.mkdir()
    return path


@pytest.fixture
def driver():
    return ClaudeBackend(new_session_id=lambda: SESSION_UUID)


def _spec(workspace, worktree, **overrides) -> LaunchSpec:
    fields = dict(
        task_id="T1",
        session_id="impl-T1-1",
        model=MODEL,
        brief="do the thing",
        workspace=workspace,
        worktree=worktree,
    )
    fields.update(overrides)
    return LaunchSpec(**fields)


def _agent_argv(source: str) -> list[str]:
    """The argv a generated launcher would hand the agent CLI.

    Parsed rather than string-matched, exactly as the existing characterisation suite
    parses it: constants come through as themselves and the prompt, a variable in the
    generated source, comes through as ``<brief>``.
    """
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and node.args
            and isinstance(node.args[0], ast.List)
        ):
            out = []
            for element in node.args[0].elts:
                if isinstance(element, ast.Constant):
                    out.append(element.value)
                elif isinstance(element, ast.Name):
                    out.append(f"<{element.id}>")
                else:  # pragma: no cover - defensive
                    out.append(f"<{type(element).__name__}>")
            return out
    raise AssertionError("generated launcher contains no subprocess.run([...]) call")


def _reference_launcher(brief_file, impl_log, model_id, sess_uuid, sys_prompt_line=""):
    """The reference's generated `launch.py`, transcribed from `maestro/implementer.py`.

    Kept as one literal expression rather than assembled, so a divergence shows up as a
    diff against the reference source instead of against a paraphrase of it.
    """
    return (
        "import subprocess\nfrom pathlib import Path\n"
        f"brief = Path(r'{brief_file}').read_text()\n"
        f"log = open(r'{impl_log}', 'w', encoding='utf-8')\n"
        f"subprocess.run([\n    'claude', '-p',\n    '--model', '{model_id}',\n"
        f"    '--session-id', r'{sess_uuid}',\n"
        f"{sys_prompt_line}    brief,\n], stdout=log, stderr=subprocess.STDOUT)\n"
        "log.flush()\nlog.close()\n"
    )


# ── protocol conformance ──


def test_driver_satisfies_the_agent_backend_protocol(driver):
    assert isinstance(driver, AgentBackend)
    assert driver.name == "claude"


def test_registry_resolves_this_driver_by_name():
    assert registry.driver_class("claude") is ClaudeBackend


def test_registry_can_construct_it_with_no_arguments():
    assert isinstance(registry.get_backend("claude"), ClaudeBackend)


def test_capabilities_are_the_four_f4_values(driver):
    assert driver.capabilities() == Capabilities(
        native_resume=True,
        system_prompt_file=True,
        usage_telemetry=True,
        sandbox=False,
    )


def test_capabilities_has_no_side_effects(driver, workspace, runs):
    driver.capabilities()
    driver.capabilities()
    assert runs.calls == []
    assert list(workspace.iterdir()) == []


def test_module_defines_a_path_valued_repo_for_the_sandbox():
    """House rule: a module with `Path` globals needs `REPO` to be rebasable."""
    assert isinstance(claude_mod.REPO, Path)
    for name, value in vars(claude_mod).items():
        if name.startswith("_") or not isinstance(value, Path):
            continue
        assert claude_mod.REPO == value or claude_mod.REPO in value.parents, name


# ── argv (F4) ──


def test_launch_argv_is_exactly_the_f4_invocation(workspace, worktree):
    assert claude_mod.launch_argv(_spec(workspace, worktree), SESSION_UUID) == [
        "claude", "-p",
        "--model", MODEL,
        "--session-id", SESSION_UUID,
        "do the thing",
    ]


def test_launch_argv_puts_the_brief_positional_and_last(workspace, worktree):
    argv = claude_mod.launch_argv(_spec(workspace, worktree, brief="B"), SESSION_UUID)
    assert argv[-1] == "B"
    assert not argv[-1].startswith("-")


def test_launch_argv_carries_no_other_flags(workspace, worktree, tmp_path):
    """The four flags DESIGN.md §6 claimed appear nowhere in the reference (C1)."""
    prompt = tmp_path / "sys.md"
    prompt.write_text("profile", encoding="utf-8")
    argv = claude_mod.launch_argv(
        _spec(workspace, worktree, system_prompt_file=prompt), SESSION_UUID
    )
    flags = {token for token in argv if token.startswith("--")}
    assert flags == {"--model", "--session-id", "--system-prompt-file"}
    for banned in (
        "--strict-mcp-config",
        "--mcp-config",
        "--add-dir",
        "--dangerously-skip-permissions",
        "--output-format",
    ):
        assert banned not in argv


def test_system_prompt_file_is_emitted_only_when_the_file_exists(
    workspace, worktree, tmp_path
):
    missing = tmp_path / "absent.md"
    argv = claude_mod.launch_argv(
        _spec(workspace, worktree, system_prompt_file=missing), SESSION_UUID
    )
    assert "--system-prompt-file" not in argv

    missing.write_text("now it exists", encoding="utf-8")
    argv = claude_mod.launch_argv(
        _spec(workspace, worktree, system_prompt_file=missing), SESSION_UUID
    )
    assert argv[argv.index("--system-prompt-file") + 1] == str(missing)


def test_system_prompt_file_precedes_the_brief(workspace, worktree, tmp_path):
    prompt = tmp_path / "sys.md"
    prompt.write_text("profile", encoding="utf-8")
    argv = claude_mod.launch_argv(
        _spec(workspace, worktree, system_prompt_file=prompt), SESSION_UUID
    )
    assert argv.index("--system-prompt-file") < len(argv) - 1
    assert argv[-1] == "do the thing"


def test_resume_argv_replaces_session_id_and_never_pairs_with_it():
    argv = claude_mod.resume_argv(SESSION_UUID, "carry on", model=MODEL)
    assert argv == ["claude", "-p", "--model", MODEL, "--resume", SESSION_UUID, "carry on"]
    assert "--session-id" not in argv


def test_resume_argv_omits_the_model_when_the_caller_does_not_know_it():
    argv = claude_mod.resume_argv(SESSION_UUID, "carry on")
    assert argv == ["claude", "-p", "--resume", SESSION_UUID, "carry on"]


def test_resume_argv_puts_the_prompt_positional_and_last():
    assert claude_mod.resume_argv(SESSION_UUID, "carry on")[-1] == "carry on"


# ── the generated launcher ──


def test_generated_launcher_is_byte_identical_to_the_reference(
    driver, workspace, worktree
):
    driver.launch(_spec(workspace, worktree))
    assert (workspace / "launch.py").read_text(encoding="utf-8") == _reference_launcher(
        workspace / "brief.txt", workspace / "impl.log", MODEL, SESSION_UUID
    )


def test_generated_launcher_matches_the_reference_with_a_system_prompt_file(
    driver, workspace, worktree, tmp_path
):
    prompt = tmp_path / "sys.md"
    prompt.write_text("profile", encoding="utf-8")
    driver.launch(_spec(workspace, worktree, system_prompt_file=prompt))
    assert (workspace / "launch.py").read_text(encoding="utf-8") == _reference_launcher(
        workspace / "brief.txt",
        workspace / "impl.log",
        MODEL,
        SESSION_UUID,
        sys_prompt_line=f"    '--system-prompt-file', r'{prompt}',\n",
    )


def test_generated_launcher_is_valid_python(driver, workspace, worktree):
    driver.launch(_spec(workspace, worktree))
    compile((workspace / "launch.py").read_text(encoding="utf-8"), "launch.py", "exec")


def test_generated_launcher_argv_parses_back_to_the_f4_shape(driver, workspace, worktree):
    driver.launch(_spec(workspace, worktree))
    assert _agent_argv((workspace / "launch.py").read_text(encoding="utf-8")) == [
        "claude", "-p",
        "--model", MODEL,
        "--session-id", SESSION_UUID,
        "<brief>",
    ]


def test_generated_launcher_merges_stderr_into_stdout(driver, workspace, worktree):
    driver.launch(_spec(workspace, worktree))
    source = (workspace / "launch.py").read_text(encoding="utf-8")
    assert "stdout=log, stderr=subprocess.STDOUT" in source
    assert f"open(r'{workspace / 'impl.log'}', 'w'" in source


def test_generated_launcher_reads_the_brief_from_disk_not_from_source(
    driver, workspace, worktree
):
    """A multi-kilobyte brief stays out of the generated source, as in the reference."""
    driver.launch(_spec(workspace, worktree, brief="X" * 5000))
    source = (workspace / "launch.py").read_text(encoding="utf-8")
    assert "X" * 5000 not in source
    assert f"Path(r'{workspace / 'brief.txt'}').read_text()" in source


def test_generated_launcher_is_executable(driver, workspace, worktree):
    driver.launch(_spec(workspace, worktree))
    assert (workspace / "launch.py").stat().st_mode & 0o777 == 0o755


# ── launch() ──


def test_launch_writes_exactly_the_three_reference_artifacts(driver, workspace, worktree):
    driver.launch(_spec(workspace, worktree))
    assert sorted(p.name for p in workspace.iterdir()) == [
        "brief.txt", "launch.py", "session_uuid.txt"
    ]


def test_launch_writes_the_brief_verbatim(driver, workspace, worktree):
    driver.launch(_spec(workspace, worktree, brief="line one\nline two\n"))
    assert (workspace / "brief.txt").read_text(encoding="utf-8") == "line one\nline two\n"


def test_launch_persists_a_bare_uuid_sidecar(workspace, worktree):
    ClaudeBackend().launch(_spec(workspace, worktree))
    text = (workspace / "session_uuid.txt").read_text(encoding="utf-8")
    assert uuid.UUID(text)
    assert text == text.strip()


def test_launch_uuid_is_fresh_per_launch(tmp_path, worktree):
    driver = ClaudeBackend()
    seen = set()
    for index in range(3):
        workspace = tmp_path / f"ws-{index}"
        workspace.mkdir()
        seen.add(driver.launch(_spec(workspace, worktree)).native_id)
    assert len(seen) == 3


def test_launch_returns_a_handle_carrying_the_resume_uuid(driver, workspace, worktree):
    handle = driver.launch(_spec(workspace, worktree))
    assert handle == Handle(
        backend="claude",
        session_id="impl-T1-1",
        native_id=SESSION_UUID,
        workspace=workspace,
        window="agents:impl-T1",
    )
    assert handle.native_id == (workspace / "session_uuid.txt").read_text(encoding="utf-8")


def test_launch_names_the_tmux_window_after_the_task(driver, workspace, worktree, runs):
    driver.launch(_spec(workspace, worktree))
    assert claude_mod.window_name("T1") == "impl-T1"
    assert claude_mod.window_target("T1") == "agents:impl-T1"
    assert " -n impl-T1 " in runs.only


def test_launch_tmux_command_matches_the_reference_shape(driver, workspace, worktree, runs):
    driver.launch(_spec(workspace, worktree))
    args, kwargs = runs.calls[0]
    assert args[0] == (
        f"tmux new-window -t agents -n impl-T1 "
        f"'cd {worktree} && {claude_mod.VENV_PYTHON} {workspace / 'launch.py'}'"
    )
    assert kwargs == {"shell": True, "check": True}


def test_launch_runs_the_launcher_not_the_agent(driver, workspace, worktree, runs):
    """The launch is indirect: tmux runs `launch.py`, never `claude` directly."""
    driver.launch(_spec(workspace, worktree))
    assert "claude" not in runs.only


def test_launch_creates_a_missing_workspace(driver, tmp_path, worktree):
    workspace = tmp_path / "fresh" / "impl-T9-1"
    driver.launch(_spec(workspace, worktree, task_id="T9"))
    assert (workspace / "launch.py").is_file()


def test_launch_propagates_a_tmux_failure(driver, workspace, worktree, monkeypatch):
    def boom(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(subprocess.CalledProcessError):
        driver.launch(_spec(workspace, worktree))


# ── resume() ──


def test_resume_generates_a_resume_launcher_with_the_resume_flag(
    driver, workspace, worktree
):
    handle = driver.launch(_spec(workspace, worktree))
    driver.resume(handle, "keep going", model=MODEL, worktree=worktree)
    argv = _agent_argv((workspace / "resume.py").read_text(encoding="utf-8"))
    assert argv == ["claude", "-p", "--model", MODEL, "--resume", SESSION_UUID, "<brief>"]
    assert "--session-id" not in argv


def test_resume_leaves_the_original_launcher_untouched(driver, workspace, worktree):
    handle = driver.launch(_spec(workspace, worktree))
    before = (workspace / "launch.py").read_text(encoding="utf-8")
    driver.resume(handle, "keep going", worktree=worktree)
    assert (workspace / "launch.py").read_text(encoding="utf-8") == before


def test_resume_appends_to_impl_log_instead_of_truncating_it(driver, workspace, worktree):
    """Truncating impl.log would destroy the evidence the reactive quota net reads."""
    handle = driver.launch(_spec(workspace, worktree))
    driver.resume(handle, "keep going", worktree=worktree)
    source = (workspace / "resume.py").read_text(encoding="utf-8")
    assert f"open(r'{workspace / 'impl.log'}', 'a'" in source


def test_resume_writes_the_prompt_to_its_own_file(driver, workspace, worktree):
    handle = driver.launch(_spec(workspace, worktree))
    driver.resume(handle, "keep going", worktree=worktree)
    assert (workspace / "resume_prompt.txt").read_text(encoding="utf-8") == "keep going"
    assert (workspace / "brief.txt").read_text(encoding="utf-8") == "do the thing"


def test_resume_runs_in_the_worktree_it_is_given(driver, workspace, worktree, runs):
    handle = driver.launch(_spec(workspace, worktree))
    driver.resume(handle, "keep going", worktree=worktree)
    assert f"'cd {worktree} &&" in runs.calls[1][0][0]


def test_resume_derives_the_worktree_from_the_task_id_when_not_given(
    driver, workspace, worktree, runs
):
    from maestro.worktree import worktree_path_for

    handle = driver.launch(_spec(workspace, worktree))
    driver.resume(handle, "keep going")
    assert f"'cd {worktree_path_for('T1')} &&" in runs.calls[1][0][0]


def test_resume_reuses_the_window_name(driver, workspace, worktree, runs):
    handle = driver.launch(_spec(workspace, worktree))
    resumed = driver.resume(handle, "keep going", worktree=worktree)
    assert resumed.window == handle.window == "agents:impl-T1"
    assert " -n impl-T1 " in runs.calls[1][0][0]


def test_resume_keeps_the_session_and_native_ids(driver, workspace, worktree):
    handle = driver.launch(_spec(workspace, worktree))
    resumed = driver.resume(handle, "keep going", worktree=worktree)
    assert resumed.session_id == handle.session_id
    assert resumed.native_id == SESSION_UUID
    assert resumed.backend == "claude"


def test_resume_falls_back_to_the_uuid_sidecar(driver, workspace, worktree):
    """A handle rebuilt from an `in_flight` entry carries no native id."""
    driver.launch(_spec(workspace, worktree))
    rebuilt = Handle(
        backend="claude",
        session_id="impl-T1-1",
        native_id=None,
        workspace=workspace,
        window="agents:impl-T1",
    )
    assert driver.resume(rebuilt, "keep going", worktree=worktree).native_id == SESSION_UUID


def test_resume_without_any_session_uuid_is_an_error(driver, workspace, worktree):
    orphan = Handle(
        backend="claude",
        session_id="impl-T1-1",
        native_id=None,
        workspace=workspace,
        window="agents:impl-T1",
    )
    with pytest.raises(ValueError) as excinfo:
        driver.resume(orphan, "keep going", worktree=worktree)
    assert "session_uuid.txt" in str(excinfo.value)


@pytest.mark.parametrize(
    "window,expected",
    [
        ("agents:impl-T1", "T1"),
        ("impl-T1", "T1"),
        ("agents:impl-P12C-2", "P12C-2"),
        ("", ""),
    ],
)
def test_task_id_from_window(window, expected):
    assert claude_mod.task_id_from_window(window) == expected


# ── parse_exit() ──


def test_parse_exit_maps_a_clean_return_code_to_ok(driver):
    verdict = driver.parse_exit(0, "all good\ndone\n")
    assert verdict.kind == "ok"
    assert verdict.reset_at is None


def test_parse_exit_maps_a_nonzero_return_code_to_crashed(driver):
    verdict = driver.parse_exit(1, "Traceback...\nRuntimeError: boom\n")
    assert verdict.kind == "crashed"
    assert verdict.evidence == "RuntimeError: boom"
    assert verdict.reset_at is None


def test_parse_exit_crashed_with_no_log_still_classifies(driver):
    assert driver.parse_exit(2, "").kind == "crashed"


@pytest.mark.parametrize(
    "line",
    [
        "You've hit your usage limit",
        "hit the limit",
        "usage limit reached",
        "rate-limit exceeded",
        "rate limit exceeded",
        "429 too many requests",
        "API Error: overloaded_error",
    ],
)
def test_parse_exit_detects_every_wording_quota_owns(driver, line):
    """The wordings come from `quota._LIMIT_RE`; this driver re-derives nothing."""
    assert driver.parse_exit(1, f"working\n{line}\n").kind == "quota_exhausted"


def test_parse_exit_quota_wins_over_a_clean_return_code(driver):
    """The reference's reactive net never sees a return code — it scans impl.log alone."""
    assert driver.parse_exit(0, "You've hit your usage limit").kind == "quota_exhausted"


def test_parse_exit_delegates_the_reset_time_to_quota(driver, monkeypatch):
    seen = {}

    def fake_resolve(tail):
        seen["tail"] = tail
        return "2099-01-01T00:00:00Z"

    monkeypatch.setattr(quota, "_resolve_limit_reset", fake_resolve)
    verdict = driver.parse_exit(1, "You've hit your usage limit, resets 4:30pm")
    assert verdict.reset_at == "2099-01-01T00:00:00Z"
    assert "resets 4:30pm" in seen["tail"]


def test_parse_exit_reset_time_is_zulu_iso(driver):
    verdict = driver.parse_exit(1, "You've hit your usage limit")
    assert verdict.reset_at is not None
    assert verdict.reset_at.endswith("Z") and len(verdict.reset_at) == 20


def test_parse_exit_uses_the_same_25_line_tail_as_quota(driver):
    older = "\n".join(f"line {n}" for n in range(40))
    assert driver.parse_exit(1, f"You've hit your usage limit\n{older}").kind == "crashed"
    assert driver.parse_exit(1, f"{older}\nYou've hit your usage limit").kind == \
        "quota_exhausted"


def test_parse_exit_evidence_is_the_matching_line_capped(driver):
    verdict = driver.parse_exit(1, "noise\n" + "You've hit your usage limit " + "x" * 500)
    assert verdict.evidence.startswith("You've hit your usage limit")
    assert len(verdict.evidence) <= 200


def test_parse_exit_never_runs_anything(driver, runs):
    driver.parse_exit(1, "You've hit your usage limit")
    driver.parse_exit(0, "fine")
    assert runs.calls == []


# ── usage() ──


F4_USAGE_JSON = {
    "context_used_pct": None,
    "context_total_input_tokens": 0,
    "five_hour": {"used_pct": 95, "resets_at": 1786373400},
    "seven_day": {"used_pct": 91, "resets_at": 1786453200},
    "model": "a-model",
    "updated_at": "2026-08-10T10:36:59Z",
}


def _usage_driver(tmp_path, document=None) -> tuple[ClaudeBackend, Path]:
    path = tmp_path / "usage.json"
    if document is not None:
        path.write_text(json.dumps(document), encoding="utf-8")
    return ClaudeBackend(usage_path=path), path


def test_usage_maps_the_named_windows_onto_their_durations(tmp_path):
    driver, _ = _usage_driver(tmp_path, F4_USAGE_JSON)
    sample = driver.usage()
    assert sample is not None
    assert sample.windows[FIVE_HOUR_MINUTES].used_pct == 95
    assert sample.windows[FIVE_HOUR_MINUTES].resets_at == 1786373400
    assert sample.windows[SEVEN_DAY_MINUTES].used_pct == 91
    assert sample.five_hour == sample.window(300)
    assert sample.seven_day == sample.window(10080)


def test_usage_carries_the_model_and_timestamp(tmp_path):
    driver, _ = _usage_driver(tmp_path, F4_USAGE_JSON)
    sample = driver.usage()
    assert sample.model == "a-model"
    assert sample.updated_at == "2026-08-10T10:36:59Z"


def test_usage_tolerates_a_null_context_percentage(tmp_path):
    """G6: `None` means "not measurable", never "no pressure"."""
    driver, _ = _usage_driver(tmp_path, F4_USAGE_JSON)
    sample = driver.usage()
    assert sample.context_used_pct is None
    assert sample.max_used_pct() == 95


def test_usage_is_none_when_no_sample_exists(tmp_path):
    driver, _ = _usage_driver(tmp_path)
    assert driver.usage() is None


def test_usage_is_none_for_a_truncated_mid_write_document(tmp_path):
    driver, path = _usage_driver(tmp_path)
    path.write_text('{"five_hour": {"used', encoding="utf-8")
    assert driver.usage() is None


def test_usage_is_none_for_a_non_object_document(tmp_path):
    driver, path = _usage_driver(tmp_path)
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert driver.usage() is None


def test_usage_survives_a_window_the_document_does_not_name(tmp_path):
    """G5: an account can have a window with no conventional name at all."""
    driver, _ = _usage_driver(
        tmp_path,
        {"windows": {"10080": {"used_pct": 98, "resets_at": 1787036664}}},
    )
    sample = driver.usage()
    assert sample.five_hour is None
    assert sample.max_used_pct() == 98


def test_usage_defaults_to_the_module_global(tmp_path, monkeypatch):
    path = tmp_path / "global-usage.json"
    path.write_text(json.dumps(F4_USAGE_JSON), encoding="utf-8")
    monkeypatch.setattr(claude_mod, "USAGE_JSON", path)
    assert ClaudeBackend().usage().windows[FIVE_HOUR_MINUTES].used_pct == 95


def test_usage_never_runs_anything(tmp_path, runs):
    driver, _ = _usage_driver(tmp_path, F4_USAGE_JSON)
    driver.usage()
    assert runs.calls == []


# ── the capabilities-not-names rule ──


def test_driver_source_branches_on_no_backend_name():
    source = Path(claude_mod.__file__).read_text(encoding="utf-8")
    for forbidden in ('name == "codex"', "name == 'codex'", 'backend.name =='):
        assert forbidden not in source


# ── complete: one bounded call ──────────────────────────────────────────────────────


class _CompletionRunner:
    """Stands in for `subprocess.run` on the `complete()` path."""

    def __init__(self, *, stdout="", stderr="", returncode=0, raises=None):
        self.calls: list[tuple[tuple, dict]] = []
        self._stdout, self._stderr = stdout, stderr
        self._rc, self._raises = returncode, raises

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._raises is not None:
            raise self._raises
        return subprocess.CompletedProcess(
            args=args[0], returncode=self._rc, stdout=self._stdout, stderr=self._stderr
        )

    @property
    def argv(self) -> list[str]:
        return list(self.calls[0][0][0])


def _completion_driver(**kwargs):
    runner = _CompletionRunner(**kwargs)
    return ClaudeBackend(runner=runner), runner


def test_completion_argv_is_the_bounded_call_shape():
    argv = claude_mod.completion_argv(CompletionSpec(prompt="why?", model=MODEL))
    assert argv == ["claude", "-p", "--model", MODEL, "why?"]


def test_completion_argv_omits_the_model_when_the_caller_has_none():
    """The driver does not own the model allowlist, so it guesses nothing."""
    assert claude_mod.completion_argv(CompletionSpec(prompt="why?")) == ["claude", "-p", "why?"]


def test_completion_argv_resumes_by_id_and_never_emits_a_session_id():
    """`--resume` and `--session-id` are mutually exclusive (F4), and a completion never
    creates a session it would have to name."""
    argv = claude_mod.completion_argv(
        CompletionSpec(prompt="again", model=MODEL, resume_id=SESSION_UUID)
    )
    assert argv == ["claude", "-p", "--model", MODEL, "--resume", SESSION_UUID, "again"]
    assert "--session-id" not in argv


def test_completion_argv_keeps_the_prompt_positional_and_last():
    argv = claude_mod.completion_argv(
        CompletionSpec(prompt="--not-a-flag", model=MODEL, resume_id=SESSION_UUID)
    )
    assert argv[-1] == "--not-a-flag"


def test_complete_returns_the_stripped_stdout():
    driver, _ = _completion_driver(stdout="  the answer\n")
    assert driver.complete(CompletionSpec(prompt="q")).text == "the answer"


def test_complete_runs_in_the_requested_directory_with_the_requested_timeout(tmp_path):
    driver, runner = _completion_driver(stdout="ok")
    driver.complete(CompletionSpec(prompt="q", cwd=tmp_path, timeout=17))
    assert runner.calls[0][1]["cwd"] == str(tmp_path)
    assert runner.calls[0][1]["timeout"] == 17


def test_complete_passes_no_cwd_when_the_caller_gave_none():
    driver, runner = _completion_driver(stdout="ok")
    driver.complete(CompletionSpec(prompt="q"))
    assert runner.calls[0][1]["cwd"] is None


def test_complete_drops_the_text_on_a_non_zero_exit():
    """Output printed by a failing run is not an answer. Returning it would let a crash
    message flow into a proof review or a Telegram reply as if the model had said it."""
    driver, _ = _completion_driver(stdout="half an answer", returncode=2)
    result = driver.complete(CompletionSpec(prompt="q"))
    assert result.text == ""
    assert result.returncode == 2


def test_complete_reports_a_timeout_without_raising():
    driver, _ = _completion_driver(raises=subprocess.TimeoutExpired(cmd="claude", timeout=5))
    result = driver.complete(CompletionSpec(prompt="q", timeout=5))
    assert (result.text, result.returncode, result.timed_out) == ("", 124, True)


def test_complete_swallows_an_unexpected_failure():
    """Every caller is best-effort; an exception here would take down a poll cycle."""
    driver, _ = _completion_driver(raises=FileNotFoundError("no claude on PATH"))
    result = driver.complete(CompletionSpec(prompt="q"))
    assert (result.text, result.returncode, result.timed_out) == ("", 1, False)


def test_complete_tees_stdout_and_stderr_to_the_log_file(tmp_path):
    log = tmp_path / "nested" / "run.log"
    driver, _ = _completion_driver(stdout="out", stderr="err")
    driver.complete(CompletionSpec(prompt="q", log_file=log))
    assert log.read_text(encoding="utf-8") == "outerr"


def test_complete_records_a_timeout_in_the_log_file(tmp_path):
    """The log is the only record of a run that produced nothing."""
    log = tmp_path / "run.log"
    driver, _ = _completion_driver(raises=subprocess.TimeoutExpired(cmd="claude", timeout=5))
    driver.complete(CompletionSpec(prompt="q", timeout=5, log_file=log))
    assert "timed out after 5s" in log.read_text(encoding="utf-8")


def test_complete_survives_an_unwritable_log_file(tmp_path):
    """A log that cannot be written must not fail the call it was only recording."""
    clash = tmp_path / "afile"
    clash.write_text("x")
    driver, _ = _completion_driver(stdout="the answer")
    result = driver.complete(CompletionSpec(prompt="q", log_file=clash / "nope.log"))
    assert result.text == "the answer"
