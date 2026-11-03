"""The Antigravity (`agy`) driver: argv, launcher, exit verdict, usage (P07B).

**No test here spends a token.** The autouse `_Runs` firewall (copied from
`test_claude_driver.py`) rejects any `subprocess.run` that is not a tmux window open, and
`complete()` runs through an injected runner. Every stream-json line below is synthetic,
shaped after `artifacts/graph-engineering/p07b-agy-1.2.2-probe.json`; the status-line
fixture is synthetic too and carries no operator identity.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from maestro import usage_report
from maestro.backends import antigravity as agy_mod
from maestro.backends import registry
from maestro.backends.antigravity import AntigravityBackend
from maestro.backends.base import (
    FIVE_HOUR_MINUTES,
    SEVEN_DAY_MINUTES,
    AgentBackend,
    Completion,
    CompletionSpec,
    Handle,
    LaunchSpec,
    TokenSplit,
    to_usage_json,
)

MODEL = "gemini-3.8-flash-high"
CID = "dfd9c8ca-ed0f-441a-b1dc-8444b8e9d9f9"
LIVE_NOTICE = (
    '\n\nDirect AGENTS.md and GEMINI.md are LIVE: native directory scope and normal task edit permissions '
    'apply to those task files. They are read from the current task tree, not frozen snapshots. '
    'Global and discovery-alias instruction snapshots, including rewritten references, remain frozen '
    'for this invocation.'
)


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
def isolated_native_runtime(tmp_path, monkeypatch):
    from maestro.backends import agy_runtime
    home = tmp_path / 'native-home'
    (home / '.gemini/antigravity-cli/bin').mkdir(parents=True)
    (home / '.gemini/config').mkdir()
    cwd = tmp_path / 'default-cwd'
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    original = agy_runtime.prepare
    def prepare(**kwargs):
        kwargs['home'] = home
        return original(**kwargs)
    monkeypatch.setattr(agy_runtime, 'prepare', prepare)


def _job(workspace, launcher='launch.py'):
    import re
    path = re.search(r"main\((.+)\)", (workspace / launcher).read_text()).group(1)
    return json.loads(Path(ast.literal_eval(path)).read_text())


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
def driver(tmp_path):
    return AntigravityBackend(gemini_home=tmp_path / "gemini", python=tmp_path / "py")


def _spec(workspace, worktree, **overrides) -> LaunchSpec:
    fields = dict(
        task_id="T1", session_id="impl-T1-1", model=MODEL, brief="do the thing",
        workspace=workspace, worktree=worktree,
    )
    fields.update(overrides)
    return LaunchSpec(**fields)


def _event(kind: str, **payload) -> str:
    return json.dumps({"event": kind, kind: payload})


def _init(model=MODEL, cid=CID) -> str:
    return json.dumps({"event": "init", "conversation_id": cid, "init": {"model": model}})


def _turn(input_tokens, cache_read=0, output=19, step=4) -> str:
    return _event(
        "step_update", conversation_id=CID, step_index=step, state="DONE",
        step_type="agent_response", text_delta="ok\n",
        usage={"input_tokens": input_tokens, "output_tokens": output, "thinking_tokens": 18,
               "cache_read_tokens": cache_read, "total_tokens": input_tokens + output},
    )


def _result(status="SUCCESS", error=None, total=41288) -> str:
    body = dict(conversation_id=CID, status=status, response="ok\n", num_turns=2,
                usage={"input_tokens": total, "output_tokens": 38, "cache_read_tokens": 0})
    if error is not None:
        body["error"] = error
    return _event("result", **body)


def _handle(workspace, native_id=None) -> Handle:
    return Handle(backend="antigravity", session_id="impl-T1-1", native_id=native_id,
                  workspace=workspace, window="agents:impl-T1")


# ── protocol + registry ──


def test_driver_satisfies_the_agent_backend_protocol(driver):
    assert isinstance(driver, AgentBackend)


def test_registry_resolves_this_driver_by_name_and_binary():
    assert registry.driver_class("antigravity") is AntigravityBackend
    assert registry.driver_ref("antigravity").binary == agy_mod.BINARY == "agy"
    assert isinstance(registry.get_backend("antigravity"), AntigravityBackend)


def test_capabilities_claim_only_what_was_exercised(driver):
    caps = driver.capabilities()
    assert caps.native_resume is True
    assert caps.usage_telemetry is True
    assert caps.system_prompt_file is False
    assert caps.sandbox is False


# ── argv ──


def test_launch_argv_ends_in_the_prompt_flag_and_the_brief(workspace, worktree):
    assert agy_mod.launch_argv(_spec(workspace, worktree)) == [
        "agy", "--output-format", "stream-json", "--model", MODEL,
        "--add-dir", str(worktree), "-p", "do the thing",
    ]


def test_every_argv_names_its_directory_as_the_active_workspace(tmp_path):
    # Without --add-dir agy tells the model it has no workspace, and the model then
    # guesses a directory ($HOME in 2 of 6 probes on 2026-09-17).
    assert agy_mod.resume_argv(CID, "go on", workspace=tmp_path)[-4:] == [
        "--add-dir", str(tmp_path), "-p", "go on",
    ]
    assert agy_mod.completion_argv(CompletionSpec(prompt="x", cwd=tmp_path))[-4:] == [
        "--add-dir", str(tmp_path), "-p", "x",
    ]
    bare = agy_mod.completion_argv(CompletionSpec(prompt="x"))
    assert "--add-dir" not in bare


def test_every_argv_retains_the_native_permission_gate(workspace, worktree):
    argvs = [
        agy_mod.launch_argv(_spec(workspace, worktree)),
        agy_mod.resume_argv(CID, "x"),
        agy_mod.completion_argv(CompletionSpec(prompt="x")),
    ]
    for argv in argvs:
        assert "--dangerously-skip-permissions" not in argv


def test_resume_argv_continues_by_conversation_id():
    assert agy_mod.resume_argv(CID, "go on", model=MODEL) == [
        "agy", "--output-format", "stream-json", "--conversation", CID, "--model", MODEL,
        "-p", "go on",
    ]
    assert "--model" not in agy_mod.resume_argv(CID, "go on")


def test_no_argv_ever_carries_an_effort_flag(workspace, worktree):
    argvs = [
        agy_mod.launch_argv(_spec(workspace, worktree)),
        agy_mod.resume_argv(CID, "x", model=MODEL),
        agy_mod.completion_argv(CompletionSpec(prompt="x", model=MODEL, resume_id=CID)),
    ]
    for argv in argvs:
        assert not any(element.startswith("--effort") for element in argv)
        assert argv[-2] == "-p"


# ── launch / resume ──


def test_launch_writes_the_brief_and_a_launcher_and_no_session_sidecar(driver, workspace, worktree):
    handle = driver.launch(_spec(workspace, worktree))
    assert (workspace / '.agy-runtime').is_dir()
    assert (workspace / 'launch_spec.json').is_file()
    assert (workspace / 'impl.log').read_text() == ''
    assert (workspace / "brief.txt").read_text() == "do the thing"
    assert os.access(workspace / "launch.py", os.X_OK)
    assert handle.native_id is None
    assert handle.backend == "antigravity" and handle.session_id == "impl-T1-1"


def test_generated_launcher_parses_back_to_the_launch_argv(driver, workspace, worktree):
    driver.launch(_spec(workspace, worktree))
    source = (workspace / "launch.py").read_text()
    job = _job(workspace)
    argv = job['argv']
    expected = agy_mod.launch_argv(_spec(workspace, worktree))
    assert argv[argv.index('--') + 1:] == [*expected[:-1], expected[-1] + LIVE_NOTICE]
    assert "do the thing" not in source
    assert job['log_file'] == str(workspace / 'impl.log')
    assert job['mode'] == 'w'


def test_launch_opens_one_tmux_window_named_after_the_task(driver, workspace, worktree, runs):
    driver.launch(_spec(workspace, worktree))
    assert runs.only.startswith("tmux new-window -t agents -n impl-T1 ")
    assert str(worktree) in runs.only and "launch.py" in runs.only


def test_resume_recovers_the_conversation_id_from_impl_log(driver, workspace, worktree, runs):
    (workspace / "impl.log").write_text("\n".join([_init(), _turn(20_000), _result()]) + "\n")
    resumed = driver.resume(_handle(workspace), "carry on", model=MODEL, worktree=worktree)
    assert resumed.native_id == CID
    job = _job(workspace, 'resume.py')
    assert CID in job['argv'] and job['mode'] == 'a'
    assert (workspace / "resume_prompt.txt").read_text() == "carry on"
    assert str(worktree) in runs.only


def test_resume_without_a_conversation_id_anywhere_is_an_error(driver, workspace, worktree):
    with pytest.raises(ValueError):
        driver.resume(_handle(workspace), "carry on", worktree=worktree)


# ── complete ──


class _CompletionRunner:
    def __init__(self, *, stdout="", stderr="", returncode=0, raises=None):
        self.calls = []
        self._stdout, self._stderr, self._rc, self._raises = stdout, stderr, returncode, raises

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._raises is not None:
            raise self._raises
        return subprocess.CompletedProcess(args=args[0], returncode=self._rc,
                                           stdout=self._stdout, stderr=self._stderr)


def _json_doc(status="SUCCESS", response="OK\n"):
    return json.dumps({"conversation_id": CID, "status": status, "response": response})


def test_completion_argv_is_the_json_shape_with_the_prompt_last():
    assert agy_mod.completion_argv(CompletionSpec(prompt="why?", model=MODEL, resume_id=CID)) == [
        "agy", "--output-format", "json", "--model", MODEL, "--conversation", CID,
        "-p", "why?",
    ]
    assert agy_mod.completion_argv(CompletionSpec(prompt="why?")) == [
        "agy", "--output-format", "json", "-p", "why?",
    ]


def test_complete_returns_the_response_on_success(tmp_path):
    runner = _CompletionRunner(stdout=_json_doc())
    log = tmp_path / "c.log"
    result = AntigravityBackend(runner=runner).complete(CompletionSpec(prompt="hi", log_file=log))
    assert result == Completion(text="OK", returncode=0)
    assert runner.calls[0][0][0][-1] == "hi" + LIVE_NOTICE
    assert CID in log.read_text()


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(stdout=_json_doc(status="ERROR"), returncode=0),
        dict(stdout=_json_doc(), returncode=1),
        dict(stdout="not json", returncode=0),
    ],
)
def test_complete_drops_the_text_unless_rc_zero_and_success(kwargs):
    result = AntigravityBackend(runner=_CompletionRunner(**kwargs)).complete(CompletionSpec(prompt="hi"))
    assert result.text == ""


def test_complete_reports_a_timeout_without_raising():
    runner = _CompletionRunner(raises=subprocess.TimeoutExpired(cmd="agy", timeout=1))
    assert AntigravityBackend(runner=runner).complete(CompletionSpec(prompt="hi", timeout=1)) == Completion(
        text="", returncode=124, timed_out=True
    )


def test_complete_timeout_retains_raw_partial_output_and_failure(tmp_path):
    output, errors = b'partial stdout\xe2', b'partial stderr\xff'
    runner = _CompletionRunner(raises=subprocess.TimeoutExpired(
        cmd='agy', timeout=1, output=output, stderr=errors))
    log = tmp_path / 'timeout.log'
    result = AntigravityBackend(runner=runner).complete(
        CompletionSpec(prompt='hi', timeout=1, log_file=log))
    assert result == Completion(text='', returncode=124, timed_out=True)
    assert log.read_bytes() == output + errors + b'\n(timed out after 1s)'


@pytest.mark.parametrize('preparation_seconds', [3, 10, 11])
def test_completion_preparation_consumes_its_native_time_budget(tmp_path, monkeypatch, preparation_seconds):
    from types import SimpleNamespace

    clock = [100.0]
    monkeypatch.setattr(agy_mod, 'time', SimpleNamespace(monotonic=lambda: clock[0]), raising=False)
    prepare = agy_mod.agy_runtime.prepare
    def slow_prepare(**kwargs):
        run = prepare(**kwargs)
        clock[0] += preparation_seconds
        return run
    monkeypatch.setattr(agy_mod.agy_runtime, 'prepare', slow_prepare)
    runner = _CompletionRunner(stdout=_json_doc())
    result = AntigravityBackend(runner=runner).complete(
        CompletionSpec(prompt='hi', timeout=10, log_file=tmp_path/'budget.log'))
    if preparation_seconds < 10:
        assert result == Completion(text='OK', returncode=0)
        assert runner.calls[0][1]['timeout'] == 10 - preparation_seconds
    else:
        assert result == Completion(text='', returncode=124, timed_out=True)
        assert runner.calls == []
        assert (tmp_path/'budget.log').read_text() == '(timed out after 10s)'


def test_completion_refuses_late_native_success_and_keeps_its_output(tmp_path, monkeypatch):
    from types import SimpleNamespace

    clock = [100.0]
    monkeypatch.setattr(agy_mod, 'time', SimpleNamespace(monotonic=lambda: clock[0]), raising=False)
    document = _json_doc()
    def late_native(argv, **kwargs):
        clock[0] += 11
        return subprocess.CompletedProcess(argv, 0, document, '')
    log = tmp_path/'late.log'
    result = AntigravityBackend(runner=late_native).complete(
        CompletionSpec(prompt='hi', timeout=10, log_file=log))
    assert result == Completion(text='', returncode=124, timed_out=True)
    assert log.read_bytes() == document.encode() + b'\n(timed out after 10s)'


# ── parse_exit ──


def test_parse_exit_is_ok_for_rc_zero_and_a_success_result(driver):
    assert driver.parse_exit(0, "\n".join([_init(), _turn(20_000), _result()])).kind == "ok"


def test_parse_exit_is_crashed_with_the_result_error_as_evidence(driver):
    tail = "\n".join([_init(), _result(status="ERROR", error="Eligibility check failed: UNAVAILABLE (code 503)")])
    verdict = driver.parse_exit(1, tail)
    assert verdict.kind == "crashed"
    assert "UNAVAILABLE" in verdict.evidence


def test_parse_exit_without_a_result_is_crashed_even_at_rc_zero(driver):
    assert driver.parse_exit(0, _init()).kind == "crashed"


def test_parse_exit_reads_the_single_json_document_of_a_completion(driver):
    # P12 pilot, DuetFlow 05-selector implementer_05: the graph worker runs agy through
    # `complete()` (`--output-format json`), whose log is ONE document, not stream-json
    # events. A successful run was classified `worker_crashed` with empty evidence.
    document = json.dumps({"conversation_id": "c1", "status": "SUCCESS", "response": "done", "num_turns": 1})
    assert driver.parse_exit(0, document).kind == "ok"


def test_parse_exit_reports_a_failed_json_document_error_as_evidence(driver):
    document = json.dumps({"conversation_id": "c1", "status": "ERROR", "error": "Eligibility check failed"})
    verdict = driver.parse_exit(1, document)
    assert verdict.kind == "crashed"
    assert "Eligibility check failed" in verdict.evidence


def test_parse_exit_classifies_limit_wording_in_the_result_error(driver, tmp_path):
    fixed = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    d = AntigravityBackend(gemini_home=tmp_path / "none", now=lambda: fixed)
    verdict = d.parse_exit(1, _result(status="ERROR", error="429 Too Many Requests"))
    assert verdict.kind == "quota_exhausted"
    assert verdict.reset_at == "2026-09-15T13:00:00Z"
    assert "Too Many Requests" in verdict.evidence


def test_parse_exit_classifies_limit_wording_on_a_stderr_line(driver):
    assert driver.parse_exit(1, "Error: usage limit reached\n").kind == "quota_exhausted"


def test_agent_output_that_mentions_a_rate_limit_is_not_quota_exhaustion(driver):
    chatty = _event("step_update", step_type="agent_response", text_delta="added a rate limit to the API")
    assert driver.parse_exit(0, "\n".join([_init(), chatty, _result()])).kind == "ok"


def test_quota_reset_comes_from_the_exhausted_pools_own_reset(tmp_path):
    home = tmp_path / "gemini"
    _write_dump(home, gemini_5h=(0.0, "2026-09-15T14:30:00Z"))
    fixed = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    verdict = AntigravityBackend(gemini_home=home, now=lambda: fixed).parse_exit(1, "rate limit exceeded")
    assert verdict.reset_at == "2026-09-15T14:30:00Z"


# ── usage: the account, from the status-line push file ──


def _write_dump(home: Path, *, gemini_5h=(0.9, "2026-09-15T14:30:00Z"), weekly=(0.25, "2026-09-17T22:00:00Z")):
    home.mkdir(parents=True, exist_ok=True)
    document = {
        "model": {"id": "Gemini 3.8 Flash (High)", "display_name": "Gemini 3.8 Flash (High)", "effort": "high"},
        "version": "1.2.3",
        "quota": {
            "gemini-5h": {"remaining_fraction": gemini_5h[0], "reset_time": gemini_5h[1]},
            "gemini-weekly": {"remaining_fraction": weekly[0], "reset_time": weekly[1]},
            "3p-5h": {"remaining_fraction": 0.0, "reset_time": "2026-09-15T13:00:00Z"},
            "3p-weekly": {"remaining_fraction": 0.0, "reset_time": "2026-09-21T00:00:00Z"},
        },
    }
    (home / "statusline_dump.json").write_text(json.dumps(document))


def test_account_usage_maps_the_gemini_pools_onto_their_durations(driver, tmp_path):
    _write_dump(tmp_path / "gemini")
    sample = driver.usage()
    assert set(sample.windows) == {FIVE_HOUR_MINUTES, SEVEN_DAY_MINUTES}
    assert sample.five_hour.used_pct == pytest.approx(10.0)
    assert sample.seven_day.used_pct == pytest.approx(75.0)
    assert sample.five_hour.resets_at == int(datetime(2026, 9, 15, 14, 30, tzinfo=timezone.utc).timestamp())
    assert sample.model == "Gemini 3.8 Flash (High)"
    assert sample.session_id == ""
    assert sample.updated_at.endswith("Z")


def test_the_3p_pools_are_not_attributed_to_a_gemini_window(driver, tmp_path):
    _write_dump(tmp_path / "gemini")
    assert driver.usage().max_used_pct() == pytest.approx(75.0)


def test_the_3p_pools_are_reported_under_their_own_usage_pool(driver, tmp_path):
    """Both quotas are measured; each is attributed to the pool the catalog routes on."""
    _write_dump(tmp_path / "gemini")
    sample = driver.usage()

    assert set(sample.pools) == {"antigravity-gemini", "antigravity-3p"}
    gemini = sample.pools["antigravity-gemini"]
    assert gemini[FIVE_HOUR_MINUTES].used_pct == pytest.approx(10.0)
    assert gemini[SEVEN_DAY_MINUTES].used_pct == pytest.approx(75.0)

    third_party = sample.pools["antigravity-3p"]
    assert third_party[FIVE_HOUR_MINUTES].used_pct == pytest.approx(100.0)
    assert third_party[SEVEN_DAY_MINUTES].used_pct == pytest.approx(100.0)
    assert third_party[FIVE_HOUR_MINUTES].resets_at == int(
        datetime(2026, 9, 15, 13, 0, tzinfo=timezone.utc).timestamp()
    )

    # `windows` stays the Gemini reading exactly, which is what the pin above asserts.
    assert sample.windows == gemini


def test_a_dump_missing_the_3p_pools_reports_no_3p_pool_at_all(driver, tmp_path):
    """Not measured is never "not used" (G6): the pool is absent, not zero."""
    home = tmp_path / "gemini"
    home.mkdir(parents=True, exist_ok=True)
    (home / "statusline_dump.json").write_text(
        json.dumps(
            {
                "model": {"id": "Gemini 3.8 Flash (High)"},
                "quota": {"gemini-5h": {"remaining_fraction": 0.5, "reset_time": None}},
            }
        )
    )
    sample = driver.usage()
    assert set(sample.pools) == {"antigravity-gemini"}
    assert set(sample.pools["antigravity-gemini"]) == {FIVE_HOUR_MINUTES}


@pytest.mark.parametrize("content", [None, "{half", "[1, 2]"])
def test_account_usage_is_none_without_a_usable_dump(driver, tmp_path, content):
    if content is not None:
        (tmp_path / "gemini").mkdir()
        (tmp_path / "gemini" / "statusline_dump.json").write_text(content)
    assert driver.usage() is None


def test_gemini_home_defaults_under_the_users_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert AntigravityBackend().gemini_home() == tmp_path / ".gemini"


# ── usage: one session, from its own impl.log ──


def test_a_session_reading_is_the_last_turns_input_plus_cache_read(driver, workspace):
    (workspace / "impl.log").write_text("\n".join([
        _init(), _turn(20_532, step=2), _turn(3_000, cache_read=17_756, output=40, step=4), _result(total=41_288),
    ]) + "\n")
    sample = driver.usage(_handle(workspace))
    assert sample.context_total_input_tokens == 20_756
    assert sample.token_split == TokenSplit(fresh_input=3_000, cache_read=17_756, cache_write=None, output=40)
    assert sample.session_id == "impl-T1-1"
    assert sample.model == MODEL
    assert sample.windows == {}
    assert sample.cli_version is None


def test_the_split_stays_out_of_the_usage_json_document(driver, workspace):
    (workspace / "impl.log").write_text(_init() + "\n" + _turn(10_000) + "\n")
    document = to_usage_json(driver.usage(_handle(workspace)))
    assert "token_split" not in document and "cli_version" not in document


@pytest.mark.parametrize("lines", [[], [_init()], [_init(), _result()], [_init(), _turn(0)]])
def test_a_session_with_no_usable_turn_is_not_measurable(driver, workspace, lines):
    (workspace / "impl.log").write_text("\n".join(lines))
    assert driver.usage(_handle(workspace)) is None


def test_a_session_with_no_log_is_not_measurable(driver, workspace):
    assert driver.usage(_handle(workspace)) is None


def test_a_half_written_last_line_does_not_lose_the_reading(driver, workspace):
    (workspace / "impl.log").write_text(_init() + "\n" + _turn(12_000) + '\n{"event":"step_up')
    assert driver.usage(_handle(workspace)).context_total_input_tokens == 12_000


def test_the_operators_status_line_is_never_a_sessions_reading(driver, workspace, tmp_path):
    _write_dump(tmp_path / "gemini")
    assert driver.usage(_handle(workspace)) is None


def test_usage_and_parse_exit_never_run_anything(driver, workspace, runs, tmp_path):
    _write_dump(tmp_path / "gemini")
    (workspace / "impl.log").write_text(_init() + "\n" + _turn(1_000) + "\n")
    driver.usage()
    driver.usage(_handle(workspace))
    driver.parse_exit(1, "rate limit")
    assert runs.calls == []


# ── attempt_token_usage: the finished attempt's cumulative tokens ──

#: Shape of a real `complete()` log (itv P12 attempt, agy 1.4.12 reports the same keys).
_COMPLETION_LOG = json.dumps({
    "conversation_id": CID, "status": "SUCCESS", "response": "done\n", "num_turns": 1,
    "usage": {"input_tokens": 228844, "output_tokens": 18705, "thinking_tokens": 12637,
              "cache_read_tokens": 2398313, "total_tokens": 247549},
})


def test_attempt_tokens_are_read_from_a_completion_document(tmp_path, driver):
    (tmp_path / "impl.log").write_text(_COMPLETION_LOG + "\n")

    assert driver.attempt_token_usage(tmp_path) == {
        "fresh_input": 228844, "cache_read": 2398313, "cache_write": 0, "output": 18705,
        "reasoning_output": 12637, "cost_usd": None, "models": [], "source": "agy_log",
    }


def test_the_usage_report_totals_an_agy_attempt(tmp_path, driver):
    """The report totals an attempt only when all four counts are numbers. A `None` here
    left every agy attempt out of the P12 sums: 2.65 M tokens on itv, 7.35 M on DuetFlow."""
    (tmp_path / "impl.log").write_text(_COMPLETION_LOG + "\n")

    tokens = driver.attempt_token_usage(tmp_path)

    assert all(isinstance(tokens[field], int) for field in usage_report.TOKEN_FIELDS)


def test_attempt_tokens_take_the_stream_result_not_the_last_turn(tmp_path, driver):
    """A step's usage is one turn; `result` is the running total of the conversation."""
    (tmp_path / "impl.log").write_text("\n".join(
        [_init(), _turn(900, cache_read=50, output=7), _result(total=41288)]) + "\n")

    tokens = driver.attempt_token_usage(tmp_path)

    assert (tokens["fresh_input"], tokens["output"], tokens["models"]) == (41288, 38, [MODEL])


def test_attempt_tokens_after_a_resume_are_the_last_running_total(tmp_path, driver):
    """Seen on agy 1.4.12: a resumed conversation's `result` already includes earlier turns."""
    (tmp_path / "impl.log").write_text("\n".join(
        [_init(), _result(total=12595), _init(), _result(total=25477)]) + "\n")

    assert driver.attempt_token_usage(tmp_path)["fresh_input"] == 25477


def test_attempt_tokens_read_cache_read_from_a_stream_result(tmp_path, driver):
    result = _event("result", status="SUCCESS", usage={
        "input_tokens": 10, "output_tokens": 2, "cache_read_tokens": 500})
    (tmp_path / "impl.log").write_text(_init() + "\n" + result + "\n")

    assert driver.attempt_token_usage(tmp_path)["cache_read"] == 500


def test_a_resume_that_has_not_finished_has_no_count(tmp_path, driver):
    """`resume` appends to the log. Its predecessor's total is not this call's count."""
    (tmp_path / "impl.log").write_text("\n".join(
        [_init(), _result(total=12595), _init(), _turn(900)]) + "\n")

    assert driver.attempt_token_usage(tmp_path) is None


def test_a_result_with_no_usage_keeps_the_last_real_count(tmp_path, driver):
    (tmp_path / "impl.log").write_text("\n".join(
        [_COMPLETION_LOG, _event("result", status="FAILED")]) + "\n")

    assert driver.attempt_token_usage(tmp_path)["fresh_input"] == 228844


def test_the_later_final_record_wins_whatever_its_form(tmp_path, driver):
    (tmp_path / "impl.log").write_text("\n".join(
        [_init(), _result(total=5), _COMPLETION_LOG]) + "\n")

    assert driver.attempt_token_usage(tmp_path)["fresh_input"] == 228844


@pytest.mark.parametrize("log", [
    None,                                               # the record is gone
    "",
    "not json\n",
    _init() + "\n" + _turn(900) + "\n",                 # killed before its result
    json.dumps({"status": "SUCCESS", "response": "no usage"}) + "\n",
    json.dumps({"status": "SUCCESS", "usage": {"input_tokens": "many"}}) + "\n",
])
def test_attempt_tokens_stay_absent_when_the_log_cannot_say(tmp_path, driver, log):
    if log is not None:
        (tmp_path / "impl.log").write_text(log)

    assert driver.attempt_token_usage(tmp_path) is None
