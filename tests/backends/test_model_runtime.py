"""P7 §5: the bounded model/tool supervisor, and the HTTP adapter underneath it.

Nothing here opens a socket. `ScriptedTransport` answers a fixed sequence of turns, which
is what makes the interesting assertions possible at all: a real endpoint cannot be asked
to reach for a forbidden tool on turn three, and a wall-clock ceiling cannot be tested by
waiting for it.

The file is in two halves. The first pins `model_api`'s three wire formats — the property
that matters is that all three produce the *same* `ModelTurn`, because the loop in the
second half never branches on the provider.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from maestro.backends import model_api as api
from maestro.backends import model_runtime as rt
from maestro.confinement import CommitBoundary


# ── fixtures ─────────────────────────────────────────────────────────────────────────


class ScriptedTransport:
    """Answers a queued list of `(status, payload)` and records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def post(self, url, *, headers, body, timeout):
        self.requests.append({"url": url, "headers": dict(headers), "body": dict(body)})
        if not self.responses:
            raise AssertionError("the loop asked for more turns than the script provides")
        return self.responses.pop(0)


def endpoint(**overrides) -> api.ModelEndpoint:
    body = dict(
        endpoint_id="free_api",
        base_url="https://api.example.test/v1",
        model="free-1",
        api_key_env="MAESTRO_FREE_KEY",
    )
    body.update(overrides)
    return api.ModelEndpoint(**body)


def model(transport, **overrides) -> api.ModelAPI:
    return api.ModelAPI(
        endpoint(**overrides), transport=transport, environ={"MAESTRO_FREE_KEY": "k-123"}
    )


def openai_turn(text="", calls=(), finish="stop"):
    return 200, {
        "choices": [
            {
                "finish_reason": finish,
                "message": {
                    "content": text,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                        for call_id, name, args in calls
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


@pytest.fixture
def worktree(tmp_path):
    repo = tmp_path / "repo"
    tree = repo / ".orchestrator" / "worktrees" / "T-001"
    tree.mkdir(parents=True)
    (repo / ".git" / "objects").mkdir(parents=True)
    return repo, tree


@pytest.fixture
def boundary(worktree):
    repo, _ = worktree
    return CommitBoundary.for_task(repo, "T-001")


@pytest.fixture
def toolbox(boundary):
    return rt.ToolBox(boundary=boundary, runner=fake_runner())


def fake_runner(returncode=0, stdout="", stderr=""):
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    run.calls = calls                                 # type: ignore[attr-defined]
    return run


def runtime(api_, toolbox_, tmp_path, **overrides):
    return rt.ModelAgentRuntime(
        api_,
        toolbox_,
        rt.CheckpointWriter(tmp_path / ".orchestrator" / "artifacts", attempt_id="att-1"),
        digest=lambda _: "digest",
        **overrides,
    )


# ── §5.4: transport and the credential boundary ──────────────────────────────────────


def test_an_endpoint_must_be_https():
    with pytest.raises(api.ModelAPIError):
        endpoint(base_url="http://api.example.test/v1")


def test_the_key_is_named_as_an_environment_variable_and_never_as_a_path():
    """`[INV-03]` made unstateable: there is no spelling of "read the key from a file"."""
    for forbidden in ("~/.claude/.credentials.json", "/etc/key", "creds.env"):
        with pytest.raises(api.ModelAPIError):
            endpoint(api_key_env=forbidden)


def test_an_unset_key_is_a_refusal_not_an_anonymous_request():
    client = api.ModelAPI(endpoint(), transport=ScriptedTransport(), environ={})
    with pytest.raises(api.CredentialMissing):
        client.complete([api.Message(role="user", content="hi")])


def test_a_non_2xx_status_raises_rather_than_reading_as_an_empty_answer():
    """Unlike `AgentBackend.complete`, whose callers are all best-effort. A supervisor
    must tell "the model said nothing" from "the endpoint is refusing us"."""
    transport = ScriptedTransport((429, {"error": "rate limited"}))
    with pytest.raises(api.TransportError):
        model(transport).complete([api.Message(role="user", content="hi")])


def test_all_three_wire_formats_produce_the_same_turn():
    tool_args = {"path": "a.py"}
    shapes = {
        "openai_chat": openai_turn("thinking", [("c1", "read_file", tool_args)]),
        "anthropic_messages": (
            200,
            {
                "content": [
                    {"type": "text", "text": "thinking"},
                    {"type": "tool_use", "id": "c1", "name": "read_file", "input": tool_args},
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        ),
        "gemini_generate": (
            200,
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "thinking"},
                                {"functionCall": {"name": "read_file", "args": tool_args}},
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
            },
        ),
    }
    for wire, response in shapes.items():
        turn = model(ScriptedTransport(response), wire=wire).complete(
            [api.Message(role="user", content="go")], tools=rt.TOOL_SCHEMAS
        )
        assert turn.text == "thinking", wire
        assert [(c.name, dict(c.arguments)) for c in turn.tool_calls] == [
            ("read_file", tool_args)
        ], wire
        assert (turn.input_tokens, turn.output_tokens) == (10, 5), wire


def test_the_catalogued_entry_declares_effort_unsupported_rather_than_inventing_one():
    """`[INV-A07]`: a bare chat endpoint has no reasoning-effort control, and the honest
    catalog entry says so instead of offering a parameter nobody verified."""
    entry = api.capability_entry(endpoint(), verified_version="v1")
    assert entry.billing_category == "free_tier_api"
    assert entry.runtime_kind == "model_endpoint_harness"
    assert entry.effort_controls.supported is False
    assert entry.permission_enforcement == "directory_isolation"


def test_an_unverified_endpoint_is_not_catalogued():
    """`[INV-12]`: verification is a precondition of being a route, not a nice-to-have."""
    with pytest.raises(api.ModelAPIError):
        api.capability_entry(endpoint(), verified_version="")


# ── §5.1: the closed tool set ────────────────────────────────────────────────────────


def test_the_offered_tools_are_exactly_the_four_the_spec_names():
    assert rt.ALLOWED_TOOLS == ("read_file", "write_file", "run_test", "grep")
    assert {s.name for s in rt.TOOL_SCHEMAS} == set(rt.ALLOWED_TOOLS)


def test_a_tool_outside_the_allowlist_is_refused_and_recorded_not_executed(toolbox):
    result = toolbox.dispatch(api.ToolCall(name="bash", arguments={"cmd": "rm -rf /"}))
    assert result.ok is False
    assert "not available" in result.content
    assert toolbox.refusals == ["bash"]


def test_run_test_never_takes_a_command_from_the_model(boundary):
    """The whole point of the tool: the model chooses *which* configured command, and
    there is no branch in which it can supply one."""
    runner = fake_runner()
    box = rt.ToolBox(
        boundary=boundary, test_commands={"unit": ["pytest", "-q"]}, runner=runner
    )
    refused = box.dispatch(
        api.ToolCall(name="run_test", arguments={"name": "rm", "command": "rm -rf /"})
    )
    assert refused.ok is False and runner.calls == []

    ran = box.dispatch(api.ToolCall(name="run_test", arguments={"name": "unit"}))
    assert ran.ok is True
    assert runner.calls == [["pytest", "-q"]]


def test_read_and_write_stay_inside_the_worktree(toolbox, worktree):
    _, tree = worktree
    written = toolbox.dispatch(
        api.ToolCall(name="write_file", arguments={"path": "src/a.py", "content": "x = 1\n"})
    )
    assert written.ok and written.mutated
    assert (tree / "src" / "a.py").read_text() == "x = 1\n"
    assert toolbox.dispatch(
        api.ToolCall(name="read_file", arguments={"path": "src/a.py"})
    ).content == "x = 1\n"


@pytest.mark.parametrize("path", ["../../escape.py", "/etc/passwd"])
def test_a_write_outside_the_worktree_is_a_breach_not_a_refused_call(toolbox, path):
    """A refused tool is a conversation; a path escape is the end of the run (`§4.2`)."""
    with pytest.raises(rt.ConfinementBreach):
        toolbox.dispatch(api.ToolCall(name="write_file", arguments={"path": path, "content": ""}))


# ── §5.2 / §5.3: checkpoints and ceilings ────────────────────────────────────────────


def test_a_checkpoint_is_written_after_every_single_tool_execution(toolbox, tmp_path):
    transport = ScriptedTransport(
        openai_turn("", [("c1", "write_file", {"path": "a.py", "content": "1"})]),
        openai_turn("", [("c2", "read_file", {"path": "a.py"})]),
        openai_turn("done"),
    )
    outcome = runtime(model(transport), toolbox, tmp_path).run("implement it")

    assert outcome.stop_reason == "completed" and outcome.status == "succeeded"
    assert [c.turn for c in outcome.checkpoints] == [1, 2]
    assert [c.tool for c in outcome.checkpoints] == ["write_file", "read_file"]
    for checkpoint in outcome.checkpoints:
        document = json.loads(checkpoint.path.read_text())
        assert document["worktree_digest"] == "digest"
        assert document["tool"] == checkpoint.tool


def test_the_turn_ceiling_stops_the_loop_and_says_so(toolbox, tmp_path):
    transport = ScriptedTransport(
        *[openai_turn("", [("c", "read_file", {"path": "a.py"})]) for _ in range(5)]
    )
    outcome = runtime(
        model(transport), toolbox, tmp_path, budget=rt.Budget(max_turns=3)
    ).run("loop forever")

    assert outcome.stop_reason == "turn_budget_exhausted"
    assert outcome.status == "uncertain"
    assert outcome.turns == 3


def test_the_wall_clock_ceiling_stops_the_loop_without_waiting_for_it(toolbox, tmp_path):
    ticks = iter([0.0, 0.0, 5.0, 5.0, 900.0, 900.0, 900.0])
    transport = ScriptedTransport(
        *[openai_turn("", [("c", "read_file", {"path": "a.py"})]) for _ in range(5)]
    )
    budget = rt.Budget(max_turns=50, wall_clock_sec=600, clock=lambda: next(ticks))
    outcome = runtime(model(transport), toolbox, tmp_path, budget=budget).run("slow work")

    assert outcome.stop_reason == "deadline_exceeded"
    assert outcome.status == "uncertain"


def test_the_default_ceilings_are_the_ones_the_spec_states():
    assert rt.DEFAULT_MAX_TURNS == 25
    assert rt.Budget().max_turns == 25


def test_a_confinement_breach_fails_the_run_and_checkpoints_the_refusal(toolbox, tmp_path):
    transport = ScriptedTransport(
        openai_turn("", [("c1", "write_file", {"path": "../../out.py", "content": "x"})])
    )
    outcome = runtime(model(transport), toolbox, tmp_path).run("escape")

    assert outcome.status == "failed"
    assert outcome.stop_reason == "confinement_breach"
    assert len(outcome.checkpoints) == 1
    assert json.loads(outcome.checkpoints[0].path.read_text())["ok"] is False


def test_an_endpoint_failure_fails_the_node_rather_than_producing_empty_work(toolbox, tmp_path):
    outcome = runtime(
        api.ModelAPI(endpoint(), transport=ScriptedTransport(), environ={}), toolbox, tmp_path
    ).run("go")
    assert outcome.status == "failed"
    assert outcome.stop_reason == "endpoint_unavailable"


def test_a_turn_with_no_tool_calls_is_the_only_way_to_complete(toolbox, tmp_path):
    outcome = runtime(model(ScriptedTransport(openai_turn("all done"))), toolbox, tmp_path).run("go")
    assert (outcome.status, outcome.stop_reason, outcome.turns) == ("succeeded", "completed", 0)
    assert outcome.text == "all done"
    assert outcome.input_tokens == 10 and outcome.output_tokens == 5


def test_a_refused_tool_keeps_the_loop_running_and_is_reported_at_the_end(toolbox, tmp_path):
    transport = ScriptedTransport(
        openai_turn("", [("c1", "bash", {"cmd": "ls"})]),
        openai_turn("fine, done"),
    )
    outcome = runtime(model(transport), toolbox, tmp_path).run("go")

    assert outcome.status == "succeeded"
    assert outcome.refused_tools == ("bash",)
    # The refusal reached the model as a tool result, not as a crash.
    last_request = transport.requests[-1]["body"]["messages"]
    assert any(m.get("role") == "tool" and "not available" in m.get("content", "")
               for m in last_request)


# ── P12B F1: the stop reason is the failure kind ─────────────────────────────────────


@pytest.mark.parametrize("answer, kind", [
    ((429, {"error": "slow"}, {"retry-after": "120"}), "rate_limited"),
    ((401, {"error": "bad key"}), "auth_failed"),
    ((503, {"error": "down"}), "provider_unavailable"),
    ((400, {"error": {"code": "context_length_exceeded"}}), "context_overflow"),
])
def test_a_refusing_endpoint_stops_the_run_with_its_failure_kind(toolbox, tmp_path, answer, kind):
    outcome = runtime(model(ScriptedTransport(answer)), toolbox, tmp_path).run("go")
    assert (outcome.status, outcome.stop_reason) == ("failed", kind)
    if kind == "rate_limited":
        assert outcome.retry_after_sec == 120
        assert outcome.to_dict()["retry_after_sec"] == 120


def test_a_timed_out_request_stops_the_run_as_a_timeout(toolbox, tmp_path):
    class Slow:
        def post(self, url, *, headers, body, timeout):
            raise TimeoutError("read timed out")

    outcome = runtime(model(Slow()), toolbox, tmp_path).run("go")
    assert (outcome.status, outcome.stop_reason) == ("failed", "timeout")


def test_a_first_turn_with_nothing_in_it_is_empty_output(toolbox, tmp_path):
    outcome = runtime(model(ScriptedTransport(openai_turn(""))), toolbox, tmp_path).run("go")
    assert (outcome.status, outcome.stop_reason) == ("failed", "empty_output")


def test_an_empty_final_turn_after_real_work_still_completes(toolbox, tmp_path):
    transport = ScriptedTransport(
        openai_turn("", [("c1", "write_file", {"path": "a.py", "content": "x = 1\n"})]),
        openai_turn(""),
    )
    outcome = runtime(model(transport), toolbox, tmp_path).run("go")
    assert (outcome.status, outcome.stop_reason) == ("succeeded", "completed")


def _garbled_turn():
    return 200, {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "read_file", "arguments": "{path: a.py"}}
    ]}}]}


def test_three_consecutive_undecodable_turns_are_malformed_tool_calls(toolbox, tmp_path):
    transport = ScriptedTransport(_garbled_turn(), _garbled_turn(), _garbled_turn())
    outcome = runtime(model(transport), toolbox, tmp_path).run("go")
    assert (outcome.status, outcome.stop_reason, outcome.turns) == (
        "failed", "malformed_tool_calls", 3)


def test_a_clean_turn_resets_the_malformed_count(toolbox, tmp_path):
    clean = openai_turn("", [("c2", "write_file", {"path": "a.py", "content": "x"})])
    transport = ScriptedTransport(
        _garbled_turn(), _garbled_turn(), clean, _garbled_turn(), _garbled_turn(),
        openai_turn("done"),
    )
    outcome = runtime(model(transport), toolbox, tmp_path).run("go")
    assert (outcome.status, outcome.stop_reason) == ("succeeded", "completed")


@pytest.mark.parametrize("wire,payload", [
    ("openai_chat", {"choices": [{"finish_reason": "length", "message": {"content": "Partial answer"}}]}),
    ("anthropic_messages", {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "Partial answer"}]}),
    ("gemini_generate", {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "Partial answer"}]}}]}),
])
def test_truncated_response_is_uncertain_not_completed(toolbox, tmp_path, wire, payload):
    transport = ScriptedTransport((200, payload))
    outcome = runtime(model(transport, wire=wire), toolbox, tmp_path).run("finish the task")
    assert (outcome.status, outcome.stop_reason) == ("uncertain", "output_token_limit")
    assert outcome.text == "Partial answer"
    assert len(transport.requests) == 1


def test_truncated_tool_response_does_not_execute_partial_work(toolbox, tmp_path):
    transport = ScriptedTransport(openai_turn("partial", [("c1", "write_file", {"path": "partial.txt", "content": "truncated"})], finish="length"), openai_turn("done"))
    outcome = runtime(model(transport), toolbox, tmp_path).run("finish the task")
    assert (outcome.status, outcome.stop_reason) == ("uncertain", "output_token_limit")
    assert not (toolbox.worktree / "partial.txt").exists()
    assert not outcome.checkpoints
    assert (outcome.input_tokens, outcome.output_tokens) == (10, 5)
