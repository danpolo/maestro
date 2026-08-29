"""`maestro.agentcall` — the one seam every bounded agent call goes through.

The point of this module is that a caller stops naming a binary. So these tests assert
what a caller can now rely on: the role decides the backend, the operator's `/backend`
leads, the model follows the backend that actually won, and nothing here ever raises —
because each of the six sites it replaced was already best-effort, and an exception on a
poll cycle would take the orchestrator loop down.

No test executes an agent: `ask()` reaches a driver through `registry.driver_class`, which
is exactly the seam a stub is substituted at.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from maestro import agentcall
from maestro.backends.base import Completion, CompletionSpec


class _StubDriver:
    """Records the spec it was handed and answers with a fixed completion."""

    seen: list[CompletionSpec] = []
    reply = Completion(text="stub answer")
    explode = None

    def __init__(self):
        if type(self).explode is not None:
            raise type(self).explode

    def complete(self, spec: CompletionSpec) -> Completion:
        type(self).seen.append(spec)
        return type(self).reply


@pytest.fixture
def stub(monkeypatch):
    """`registry.driver_class` answers with a recording stub for every backend."""
    _StubDriver.seen = []
    _StubDriver.reply = Completion(text="stub answer")
    _StubDriver.explode = None
    chosen: list[str] = []

    def driver_class(name):
        chosen.append(name)
        return _StubDriver

    monkeypatch.setattr(agentcall.registry, "driver_class", driver_class)
    monkeypatch.setattr(agentcall, "operator_backend", lambda: "")
    return SimpleNamespace(driver=_StubDriver, chosen=chosen)


# ── resolution ──


def test_resolve_call_uses_the_role_not_a_hardcoded_backend(stub):
    backend, model = agentcall.resolve_call("judge")
    assert backend in agentcall.registry.known_backends()
    assert model


def test_resolve_call_lets_the_operator_choice_lead(monkeypatch, stub):
    """An operator who switches backends because one is rate limited means it for the
    judge and the diagnoser too, not only the implementer."""
    monkeypatch.setattr(agentcall, "operator_backend", lambda: "codex")
    assert agentcall.resolve_call("judge")[0] == "codex"


def test_resolve_call_ignores_an_unregistered_operator_choice(monkeypatch, stub):
    """A hand-edited state file must never stop work; `roles.resolve` drops the typo."""
    monkeypatch.setattr(agentcall, "operator_backend", lambda: "not-a-backend")
    assert agentcall.resolve_call("judge")[0] in agentcall.registry.known_backends()


def test_resolve_call_lets_an_explicit_model_win(stub):
    """`/redo` and self-fix pin a model per task."""
    assert agentcall.resolve_call("judge", model="pinned-x")[1] == "pinned-x"


def test_resolve_call_returns_the_model_for_the_backend_that_won(monkeypatch, stub):
    """Not the configured one — a call that falls through the chain must not carry the
    wrong CLI's model id with it."""
    monkeypatch.setattr(agentcall, "operator_backend", lambda: "codex")
    backend, model = agentcall.resolve_call("implementer")
    assert backend == "codex"
    assert "claude" not in (model or "")


def test_resolve_call_starts_no_process(stub):
    agentcall.resolve_call("judge")
    assert stub.driver.seen == []


# ── ask ──


def test_ask_routes_to_the_resolved_backends_driver(stub):
    agentcall.ask("judge", "why?")
    assert stub.chosen == [agentcall.resolve_call("judge")[0]]


def test_ask_returns_the_drivers_completion(stub):
    assert agentcall.ask("judge", "why?").text == "stub answer"


def test_ask_passes_the_prompt_through_untouched(stub):
    agentcall.ask("judge", "  keep   my whitespace\n")
    assert stub.driver.seen[0].prompt == "  keep   my whitespace\n"


def test_ask_defaults_to_a_bounded_timeout_and_no_writing(stub):
    """A judgment call must not be able to edit the tree it runs in."""
    agentcall.ask("judge", "q")
    spec = stub.driver.seen[0]
    assert spec.timeout == agentcall.DEFAULT_TIMEOUT
    assert spec.writable is False
    assert spec.cwd is None
    assert spec.resume_id == ""
    assert spec.log_file is None


def test_ask_forwards_every_call_shaping_field(stub, tmp_path):
    agentcall.ask(
        "implementer", "go",
        model="m", timeout=1800, cwd=tmp_path, resume_id="sess-1",
        log_file=tmp_path / "run.log", writable=True, add_dirs=[tmp_path / "ws"],
    )
    spec = stub.driver.seen[0]
    assert (spec.model, spec.timeout, spec.cwd) == ("m", 1800, tmp_path)
    assert (spec.resume_id, spec.writable) == ("sess-1", True)
    assert spec.log_file == tmp_path / "run.log"
    assert spec.add_dirs == (tmp_path / "ws",)


def test_ask_normalises_add_dirs_to_a_tuple(stub, tmp_path):
    """`CompletionSpec` is frozen and gets compared in tests; a list would make two
    equivalent specs unequal and is mutable under a frozen dataclass besides."""
    agentcall.ask("judge", "q", add_dirs=[tmp_path])
    assert isinstance(stub.driver.seen[0].add_dirs, tuple)


# ── failure is always the same shape ──


def test_ask_never_raises_when_the_driver_cannot_be_built(stub):
    stub.driver.explode = RuntimeError("no driver for you")
    result = agentcall.ask("judge", "q")
    assert (result.text, result.returncode) == ("", 1)


def test_ask_never_raises_when_resolution_itself_fails(monkeypatch, stub):
    def boom(*_a, **_k):
        raise ValueError("project.yaml is a banana")

    monkeypatch.setattr(agentcall.roles, "resolve", boom)
    assert agentcall.ask("judge", "q").text == ""


def test_ask_passes_a_driver_failure_through_unchanged(stub):
    """The drivers already answer a failed run with an empty completion; this must not
    wrap or re-interpret it, so callers have exactly one failure shape."""
    stub.driver.reply = Completion(text="", returncode=124, timed_out=True)
    result = agentcall.ask("judge", "q")
    assert (result.text, result.returncode, result.timed_out) == ("", 124, True)


# ── the session-wide guard, asserted from the caller's side ──


def test_an_unstubbed_call_fails_the_test_instead_of_returning_an_empty_answer():
    """`tests/conftest.py`'s guard must survive the driver's own `except Exception`.

    `complete()` is contractually forbidden from raising, so it catches `Exception` and
    turns any failure into an empty `Completion`. A guard that raised an `AssertionError`
    would therefore be caught *by the code under test*, and a test that forgot to stub the
    seam would quietly assert against "the agent said nothing" while, on a machine where
    the binary exists, having spent real quota to get there. Raising a `BaseException`
    subclass is what makes that impossible — this test is the proof, and it fails the
    moment someone re-bases `RealAgentCLIStarted` on `Exception`.

    No stub fixture here on purpose: this is the unstubbed path.
    """
    from tests.conftest import RealAgentCLIStarted

    with pytest.raises(RealAgentCLIStarted):
        agentcall.ask("judge", "this must never reach a real binary")
