"""Pinned behaviour of the `/ask` handler (R8): the exact `claude -p` argv shape for the
`--resume <uuid>` path and the fresh-session path, the RESUME_MISS-triggers-fallback
detection (case-insensitive), the task-brief read/truncate-to-6000-chars behaviour, the
3500-char answer truncation, the two notify message templates, `main()`'s always-0 return
code and the log-file write (including that a failed write is silently swallowed).

Standalone reference script (`scripts/maestro_ask.py`), not a function living inside
`orchestrator_run.py` — this file defines its own `subject` fixture, parametrised
`["legacy", "maestro"]`, mirroring `conftest.py`'s own `_load_legacy` (module-import
parametrisation — see the M4c batch-3 task prompt, "Established conventions" §5,
pattern (a)). It overrides the module-scoped `subject` fixture for this file only; the
`sandbox` fixture from `conftest.py` (which depends on `subject` by name) picks up this
file's version automatically.

Nothing here spawns a real agent process: the subject's own `agentcall` reference is
swapped (mirrors `_fake_agentcall` in `tests/characterization/test_selfheal.py`). Nothing
reaches Telegram: the notify path (`notify()` for legacy, `notify_telegram` for maestro,
R4's substitution) is stubbed directly by name rather than at the transport layer, since
only the message text/format matters here — the transport itself is pinned separately by
`tests/characterization/test_telegram.py`.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from maestro.backends.base import Completion

pytestmark = pytest.mark.maestro_module("hitl.ask")


# =====================================================================================
# subject wiring
# =====================================================================================


@pytest.fixture
def subject():
    """The implementation under test — overrides `conftest.py`'s `subject` for this file."""
    return importlib.import_module("maestro.hitl.ask")


# =====================================================================================
# agent stub — no real agent process ever runs
# =====================================================================================


def _agent_result(printed="", returncode=0, timed_out=False):
    """What one bounded call did, in the terms a driver reports it.

    The direct analogue of `_completed` above, at the seam that replaced it. `printed` is
    the run's combined stdout+stderr — the thing that lands in the log file, and the only
    place the resume-miss notice and the usage-limit wording ever appear.
    """
    return SimpleNamespace(printed=printed, returncode=returncode, timed_out=timed_out)


def _fake_agentcall(monkeypatch, subject, handler=None):
    """Swap the subject's own `agentcall` module reference for a recording stand-in.

    Deliberately models a *driver*, not just a return value, because `/ask` depends on two
    driver behaviours that a naive stub would paper over:

    * the run's combined output is written to `log_file`, and `_run` reads it back on a
      failed call — which is how the limit hint survives a non-zero exit;
    * `Completion.text` is empty whenever the exit was non-zero, since a failed run's
      output is not an answer. Reproducing that here is what keeps the fallback tests
      honest about *why* they fall back.

    A log that cannot be written is swallowed, matching the drivers' best-effort
    `_write_log`: a log is a record of a call, never a reason to fail it.
    """
    calls = []

    def ask(role, prompt, **kwargs):
        calls.append(SimpleNamespace(role=role, prompt=prompt, kwargs=kwargs))
        result = _agent_result() if handler is None else handler(role, prompt, **kwargs)
        log_file = kwargs.get("log_file")
        if log_file is not None:
            try:
                Path(log_file).write_text(result.printed, encoding="utf-8")
            except OSError:
                pass
        text = result.printed.strip() if result.returncode == 0 else ""
        return Completion(text=text, returncode=result.returncode,
                          timed_out=result.timed_out)

    fake = SimpleNamespace(
        ask=ask,
        calls=calls,
        resolve_call=lambda role, **_kw: ("test-backend", "test-model"),
    )
    monkeypatch.setattr(subject, "agentcall", fake)
    return fake


# =====================================================================================
# notify stub — records the message text without reaching Telegram
# =====================================================================================


def _stub_notify(subject, monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr(subject, "notify_telegram", lambda msg: calls.append(msg))
    return calls


# =====================================================================================
# request-file / argv helpers
# =====================================================================================


def _write_request(tmp_path, **overrides) -> "SimpleNamespace":
    req = {
        "uuid": "",
        "task": "P12C",
        "dan_id": "7",
        "question": "cell 5 fails",
        "action": "Run all cells",
        "brief": "",
        "worktree": str(tmp_path),
        "log": str(tmp_path / "answer.log"),
    }
    req.update(overrides)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(req), encoding="utf-8")
    return path


def _set_argv(monkeypatch, path) -> None:
    monkeypatch.setattr(sys, "argv", ["maestro_ask", str(path)])


def _framed(task="P12C", action="Run all cells", question="cell 5 fails") -> str:
    return (
        f"You are Maestro's implementer that prepared task {task}. Dan is performing the "
        f"manual action you handed him and has a question or hit a problem.\n\n"
        f"MANUAL ACTION HE WAS GIVEN:\n{action}\n\n"
        f"DAN'S MESSAGE:\n{question}\n\n"
        f"Help him resolve it. If the problem is a bug in what you prepared (e.g. the "
        f"notebook or script you wrote), state exactly what is wrong and the precise fix. "
        f"Be concrete and concise — this is a Telegram message. Advice only: do NOT modify "
        f"files or push anything."
    )


# =====================================================================================
# claude -p argv shape
# =====================================================================================


def test_fresh_session_argv_shape_when_no_uuid(subject, sandbox, monkeypatch, tmp_path):
    fake = _fake_agentcall(monkeypatch, subject, lambda *a, **k: _agent_result(printed="ok"))
    _stub_notify(subject, monkeypatch)
    req_path = _write_request(tmp_path, uuid="", brief="")
    _set_argv(monkeypatch, req_path)

    rc = subject.main()

    assert rc == 0
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call.role == subject.ROLE_IMPLEMENTER
    assert call.prompt == _framed()
    assert call.kwargs["cwd"] == tmp_path
    assert call.kwargs["resume_id"] == ""
    assert call.kwargs["timeout"] == subject.ASK_TIMEOUT
    # `/ask` is advice only — it must never be handed a writable workspace.
    assert call.kwargs.get("writable", False) is False
    # No model is passed: `roles.implementer`'s per-backend table chooses it.
    assert "model" not in call.kwargs


def test_resume_argv_shape_when_uuid_present(subject, sandbox, monkeypatch, tmp_path):
    fake = _fake_agentcall(monkeypatch, subject, lambda *a, **k: _agent_result(printed="ok"))
    _stub_notify(subject, monkeypatch)
    req_path = _write_request(tmp_path, uuid="uuid-123")
    _set_argv(monkeypatch, req_path)

    rc = subject.main()

    assert rc == 0
    assert len(fake.calls) == 1
    call = fake.calls[0]
    # The resume token goes to the driver as `resume_id`, not as a `--resume` flag: a
    # driver without native resume can then seed a fresh session itself rather than being
    # handed a flag it cannot honour.
    assert call.kwargs["resume_id"] == "uuid-123"
    assert call.prompt == _framed()
    assert call.kwargs["cwd"] == tmp_path


# =====================================================================================
# RESUME_MISS-triggers-fallback
# =====================================================================================


def test_resume_miss_triggers_fallback_case_insensitive(subject, sandbox, monkeypatch, tmp_path):
    calls_seen = []

    def handler(*args, **kwargs):
        calls_seen.append(args[0])
        if len(calls_seen) == 1:
            return _agent_result(printed="No Conversation Found With Session ID abc")
        return _agent_result(printed="fresh answer")

    fake = _fake_agentcall(monkeypatch, subject, handler)
    _stub_notify(subject, monkeypatch)
    req_path = _write_request(tmp_path, uuid="uuid-123", brief="")
    _set_argv(monkeypatch, req_path)

    rc = subject.main()

    assert rc == 0
    assert len(fake.calls) == 2
    # first call resumed; second call is the fresh-session fallback
    assert fake.calls[0].kwargs["resume_id"] == "uuid-123"
    assert fake.calls[1].kwargs["resume_id"] == ""
    assert fake.calls[1].prompt == _framed()


def test_resume_nonzero_returncode_triggers_fallback(subject, sandbox, monkeypatch, tmp_path):
    calls_seen = []

    def handler(*args, **kwargs):
        calls_seen.append(1)
        if len(calls_seen) == 1:
            return _agent_result(printed="some error", returncode=1)
        return _agent_result(printed="fresh answer")

    fake = _fake_agentcall(monkeypatch, subject, handler)
    _stub_notify(subject, monkeypatch)
    req_path = _write_request(tmp_path, uuid="uuid-123")
    _set_argv(monkeypatch, req_path)

    subject.main()

    assert len(fake.calls) == 2


def test_resume_empty_output_triggers_fallback(subject, sandbox, monkeypatch, tmp_path):
    calls_seen = []

    def handler(*args, **kwargs):
        calls_seen.append(1)
        if len(calls_seen) == 1:
            return _agent_result(printed="")
        return _agent_result(printed="fresh answer")

    fake = _fake_agentcall(monkeypatch, subject, handler)
    _stub_notify(subject, monkeypatch)
    req_path = _write_request(tmp_path, uuid="uuid-123")
    _set_argv(monkeypatch, req_path)

    subject.main()

    assert len(fake.calls) == 2


def test_successful_resume_does_not_fall_back(subject, sandbox, monkeypatch, tmp_path):
    fake = _fake_agentcall(monkeypatch, subject,
                            lambda *a, **k: _agent_result(printed="a real answer"))
    notified = _stub_notify(subject, monkeypatch)
    req_path = _write_request(tmp_path, uuid="uuid-123")
    _set_argv(monkeypatch, req_path)

    subject.main()

    assert len(fake.calls) == 1
    assert "a real answer" in notified[0]


# =====================================================================================
# brief-file read + 6000-char truncation
# =====================================================================================


def test_brief_missing_file_uses_framed_prompt_only(subject, sandbox, monkeypatch, tmp_path):
    fake = _fake_agentcall(monkeypatch, subject, lambda *a, **k: _agent_result(printed="ok"))
    _stub_notify(subject, monkeypatch)
    missing = tmp_path / "no-such-brief.txt"
    req_path = _write_request(tmp_path, uuid="", brief=str(missing))
    _set_argv(monkeypatch, req_path)

    subject.main()

    prompt = fake.calls[0].prompt
    assert prompt == _framed()


def test_brief_unreadable_file_falls_back_to_framed_prompt_only(
    subject, sandbox, monkeypatch, tmp_path
):
    fake = _fake_agentcall(monkeypatch, subject, lambda *a, **k: _agent_result(printed="ok"))
    _stub_notify(subject, monkeypatch)
    # A directory at the brief path exists() == True but read_text() raises — exercises
    # the bare `except Exception: brief_txt = ""` branch.
    brief_dir = tmp_path / "brief_is_a_dir"
    brief_dir.mkdir()
    req_path = _write_request(tmp_path, uuid="", brief=str(brief_dir))
    _set_argv(monkeypatch, req_path)

    subject.main()

    prompt = fake.calls[0].prompt
    assert prompt == _framed()


def test_brief_present_file_is_prepended_as_context(subject, sandbox, monkeypatch, tmp_path):
    fake = _fake_agentcall(monkeypatch, subject, lambda *a, **k: _agent_result(printed="ok"))
    _stub_notify(subject, monkeypatch)
    brief_file = tmp_path / "brief.txt"
    brief_file.write_text("the task brief content", encoding="utf-8")
    req_path = _write_request(tmp_path, uuid="", brief=str(brief_file))
    _set_argv(monkeypatch, req_path)

    subject.main()

    prompt = fake.calls[0].prompt
    assert prompt == (
        "CONTEXT — the task brief you worked from:\nthe task brief content\n\n" + _framed()
    )


def test_brief_content_is_truncated_to_6000_chars(subject, sandbox, monkeypatch, tmp_path):
    fake = _fake_agentcall(monkeypatch, subject, lambda *a, **k: _agent_result(printed="ok"))
    _stub_notify(subject, monkeypatch)
    brief_file = tmp_path / "brief.txt"
    brief_file.write_text("x" * 7000, encoding="utf-8")
    req_path = _write_request(tmp_path, uuid="", brief=str(brief_file))
    _set_argv(monkeypatch, req_path)

    subject.main()

    prompt = fake.calls[0].prompt
    expected_brief = "x" * 6000
    assert prompt == (
        f"CONTEXT — the task brief you worked from:\n{expected_brief}\n\n" + _framed()
    )


# =====================================================================================
# answer truncation to 3500 chars, and the two notify templates
# =====================================================================================


def test_answer_is_truncated_to_3500_chars_in_the_notify_message(
    subject, sandbox, monkeypatch, tmp_path
):
    long_answer = "y" * 4000
    _fake_agentcall(monkeypatch, subject, lambda *a, **k: _agent_result(printed=long_answer))
    notified = _stub_notify(subject, monkeypatch)
    req_path = _write_request(tmp_path, uuid="", dan_id="7")
    _set_argv(monkeypatch, req_path)

    rc = subject.main()

    assert rc == 0
    assert len(notified) == 1
    expected_answer = "y" * 3500
    assert notified[0] == (
        f"💬 *Maestro* — re: `P12C` (your question)\n\n{expected_answer}\n\n"
        f"✅ Worked → /approve 7   ·   ❓ Ask again → /ask 7 <message>"
    )


def test_notify_message_uses_task_id_when_dan_id_is_empty(
    subject, sandbox, monkeypatch, tmp_path
):
    _fake_agentcall(monkeypatch, subject, lambda *a, **k: _agent_result(printed="short answer"))
    notified = _stub_notify(subject, monkeypatch)
    req_path = _write_request(tmp_path, uuid="", dan_id="", task="P99")
    _set_argv(monkeypatch, req_path)

    subject.main()

    assert notified[0] == (
        "💬 *Maestro* — re: `P99` (your question)\n\nshort answer\n\n"
        "✅ Worked → /approve    ·   ❓ Ask again → /ask P99 <message>"
    )


def test_no_answer_produced_falls_back_to_placeholder_text(
    subject, sandbox, monkeypatch, tmp_path
):
    """`out` empty after a *successful* fresh-session attempt (rc=0, blank stdout, no
    uuid so no fallback branch is even reachable) still notifies with the placeholder."""
    _fake_agentcall(monkeypatch, subject, lambda *a, **k: _agent_result(printed=""))
    notified = _stub_notify(subject, monkeypatch)
    req_path = _write_request(tmp_path, uuid="")
    _set_argv(monkeypatch, req_path)

    subject.main()

    assert "(no answer produced)" in notified[0]


@pytest.mark.parametrize("hint", [
    "You hit your limit for today.",
    "Usage limit reached, try later.",
    "Rate limit exceeded, slow down.",
    "Too many requests right now.",
])
def test_limit_hint_short_output_sends_the_limit_notify_message(
    subject, sandbox, monkeypatch, tmp_path, hint
):
    _fake_agentcall(monkeypatch, subject, lambda *a, **k: _agent_result(printed=hint))
    notified = _stub_notify(subject, monkeypatch)
    req_path = _write_request(tmp_path, uuid="", dan_id="7", task="P12C")
    _set_argv(monkeypatch, req_path)

    rc = subject.main()

    assert rc == 0
    assert notified == [
        "⏳ Maestro couldn't answer about P12C — Claude usage limit. "
        "Try /ask 7 again after it resets."
    ]


def test_limit_hint_uses_task_when_dan_id_is_empty(subject, sandbox, monkeypatch, tmp_path):
    _fake_agentcall(monkeypatch, subject, lambda *a, **k: _agent_result(printed="usage limit hit"))
    notified = _stub_notify(subject, monkeypatch)
    req_path = _write_request(tmp_path, uuid="", dan_id="", task="P12C")
    _set_argv(monkeypatch, req_path)

    subject.main()

    assert notified == [
        "⏳ Maestro couldn't answer about P12C — Claude usage limit. "
        "Try /ask P12C again after it resets."
    ]


def test_limit_hint_is_ignored_when_output_is_200_chars_or_longer(
    subject, sandbox, monkeypatch, tmp_path
):
    """The limit-message branch requires BOTH a hint substring AND `len(out) < 200` —
    a long answer that happens to contain "rate limit" still gets the normal reply."""
    long_answer = ("This explanation discusses a rate limit you might hit later, but "
                   "here is the full detailed fix for your notebook cell: " + "z" * 200)
    assert len(long_answer) >= 200
    _fake_agentcall(monkeypatch, subject, lambda *a, **k: _agent_result(printed=long_answer))
    notified = _stub_notify(subject, monkeypatch)
    req_path = _write_request(tmp_path, uuid="", dan_id="7", task="P12C")
    _set_argv(monkeypatch, req_path)

    subject.main()

    assert notified[0].startswith("💬 *Maestro* — re: `P12C`")
    assert long_answer[:3500] in notified[0]


# =====================================================================================
# main()'s return code
# =====================================================================================


def test_main_returns_0_when_the_run_could_not_start(subject, sandbox, monkeypatch, tmp_path):
    """This used to raise `OSError` out of `subprocess.run` and be caught here, producing
    "(Maestro failed to answer: boom)". `agentcall.ask` never raises — the driver absorbs
    a run it could not start and records why in the log — so the failure now arrives as a
    non-zero `Completion` whose detail Dan reads from that log. The guarantee under test
    is unchanged: `main()` still returns 0 and still tells him something."""
    def handler(*args, **kwargs):
        return _agent_result(printed="(failed to run: boom)", returncode=1)

    _fake_agentcall(monkeypatch, subject, handler)
    notified = _stub_notify(subject, monkeypatch)
    req_path = _write_request(tmp_path, uuid="")
    _set_argv(monkeypatch, req_path)

    rc = subject.main()

    assert rc == 0
    assert "(failed to run: boom)" in notified[0]


def test_main_returns_0_on_a_timeout(subject, sandbox, monkeypatch, tmp_path):
    def handler(*args, **kwargs):
        return _agent_result(printed="", returncode=124, timed_out=True)

    _fake_agentcall(monkeypatch, subject, handler)
    notified = _stub_notify(subject, monkeypatch)
    req_path = _write_request(tmp_path, uuid="")
    _set_argv(monkeypatch, req_path)

    rc = subject.main()

    assert rc == 0
    assert "timed out after 5 min" in notified[0]


# =====================================================================================
# log-file writing
# =====================================================================================


def test_log_file_is_written_with_the_full_untruncated_output(
    subject, sandbox, monkeypatch, tmp_path
):
    long_answer = "q" * 4000
    _fake_agentcall(monkeypatch, subject, lambda *a, **k: _agent_result(printed=long_answer))
    _stub_notify(subject, monkeypatch)
    log_path = tmp_path / "answer.log"
    req_path = _write_request(tmp_path, uuid="", log=str(log_path))
    _set_argv(monkeypatch, req_path)

    subject.main()

    assert log_path.read_text(encoding="utf-8") == long_answer


def test_log_write_failure_is_silently_swallowed(subject, sandbox, monkeypatch, tmp_path):
    # A directory at the log path makes `Path(log_path).write_text` raise; main() must
    # not propagate it, and must still send the notify message.
    log_dir = tmp_path / "log_is_a_dir"
    log_dir.mkdir()
    _fake_agentcall(monkeypatch, subject, lambda *a, **k: _agent_result(printed="ok answer"))
    notified = _stub_notify(subject, monkeypatch)
    req_path = _write_request(tmp_path, uuid="", log=str(log_dir))
    _set_argv(monkeypatch, req_path)

    rc = subject.main()

    assert rc == 0
    assert log_dir.is_dir()  # untouched, still a directory
    assert "ok answer" in notified[0]
