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

Nothing here spawns a real `claude` process: the subject's own `subprocess` reference is
swapped (mirrors `_fake_subprocess` in `tests/characterization/test_selfheal.py`). Nothing
reaches Telegram: the notify path (`notify()` for legacy, `notify_telegram` for maestro,
R4's substitution) is stubbed directly by name rather than at the transport layer, since
only the message text/format matters here — the transport itself is pinned separately by
`tests/characterization/test_telegram.py`.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.reference_repo import LEGACY_REPO

pytestmark = pytest.mark.maestro_module("hitl.ask")


# =====================================================================================
# subject wiring
# =====================================================================================


def _load_legacy_ask():
    if LEGACY_REPO is None:
        pytest.skip(
            "reference repo unknown: set $MAESTRO_LEGACY_REPO or write its path to .legacy_repo"
        )
    scripts = LEGACY_REPO / "scripts"
    if not (scripts / "maestro_ask.py").is_file():
        pytest.skip(f"reference repo not found at {LEGACY_REPO}")
    env_before = dict(os.environ)
    sys.path.insert(0, str(scripts))
    try:
        mod = importlib.import_module("maestro_ask")
    finally:
        sys.path.remove(str(scripts))
        os.environ.clear()
        os.environ.update(env_before)
    return mod


@pytest.fixture(params=["legacy", "maestro"])
def subject(request):
    """The implementation under test — overrides `conftest.py`'s `subject` for this file."""
    if request.param == "legacy":
        return _load_legacy_ask()
    return importlib.import_module("maestro.hitl.ask")


def _is_maestro(subject) -> bool:
    return getattr(subject, "__name__", "").split(".")[0] == "maestro"


# =====================================================================================
# subprocess stub — no real `claude` process ever runs
# =====================================================================================


def _completed(returncode=0, stdout="", stderr="", args=()):
    return subprocess.CompletedProcess(args=list(args), returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def _fake_subprocess(monkeypatch, subject, handler=None):
    """Swap the subject's `subprocess` module reference. Scoped to the subject module."""
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        if handler is None:
            return _completed()
        return handler(*args, **kwargs)

    fake = SimpleNamespace(
        run=run,
        calls=calls,
        TimeoutExpired=subprocess.TimeoutExpired,
        CalledProcessError=subprocess.CalledProcessError,
        PIPE=subprocess.PIPE,
        STDOUT=subprocess.STDOUT,
        DEVNULL=subprocess.DEVNULL,
    )
    monkeypatch.setattr(subject, "subprocess", fake)
    return fake


# =====================================================================================
# notify stub — records the message text without reaching Telegram
# =====================================================================================


def _stub_notify(subject, monkeypatch) -> list:
    calls: list = []
    if _is_maestro(subject):
        monkeypatch.setattr(subject, "notify_telegram", lambda msg: calls.append(msg))
    else:
        monkeypatch.setattr(subject, "notify", lambda msg: calls.append(msg))
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
    fake = _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout="ok"))
    _stub_notify(subject, monkeypatch)
    req_path = _write_request(tmp_path, uuid="", brief="")
    _set_argv(monkeypatch, req_path)

    rc = subject.main()

    assert rc == 0
    assert len(fake.calls) == 1
    (cmd,), kwargs = fake.calls[0]
    assert cmd == ["claude", "-p", "--model", subject.ASK_MODEL, _framed()]
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["stdout"] == subprocess.PIPE
    assert kwargs["stderr"] == subprocess.STDOUT
    assert kwargs["text"] is True
    assert kwargs["timeout"] == 300


def test_resume_argv_shape_when_uuid_present(subject, sandbox, monkeypatch, tmp_path):
    fake = _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout="ok"))
    _stub_notify(subject, monkeypatch)
    req_path = _write_request(tmp_path, uuid="uuid-123")
    _set_argv(monkeypatch, req_path)

    rc = subject.main()

    assert rc == 0
    assert len(fake.calls) == 1
    (cmd,), kwargs = fake.calls[0]
    assert cmd == ["claude", "-p", "--model", subject.ASK_MODEL,
                   "--resume", "uuid-123", _framed()]
    assert kwargs["cwd"] == str(tmp_path)


# =====================================================================================
# RESUME_MISS-triggers-fallback
# =====================================================================================


def test_resume_miss_triggers_fallback_case_insensitive(subject, sandbox, monkeypatch, tmp_path):
    calls_seen = []

    def handler(*args, **kwargs):
        calls_seen.append(args[0])
        if len(calls_seen) == 1:
            return _completed(stdout="No Conversation Found With Session ID abc")
        return _completed(stdout="fresh answer")

    fake = _fake_subprocess(monkeypatch, subject, handler)
    _stub_notify(subject, monkeypatch)
    req_path = _write_request(tmp_path, uuid="uuid-123", brief="")
    _set_argv(monkeypatch, req_path)

    rc = subject.main()

    assert rc == 0
    assert len(fake.calls) == 2
    # first call resumed; second call is the fresh-session fallback
    assert "--resume" in fake.calls[0][0][0]
    assert "--resume" not in fake.calls[1][0][0]
    assert fake.calls[1][0][0] == ["claude", "-p", "--model", subject.ASK_MODEL, _framed()]


def test_resume_nonzero_returncode_triggers_fallback(subject, sandbox, monkeypatch, tmp_path):
    calls_seen = []

    def handler(*args, **kwargs):
        calls_seen.append(1)
        if len(calls_seen) == 1:
            return _completed(returncode=1, stdout="some error")
        return _completed(stdout="fresh answer")

    fake = _fake_subprocess(monkeypatch, subject, handler)
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
            return _completed(returncode=0, stdout="")
        return _completed(stdout="fresh answer")

    fake = _fake_subprocess(monkeypatch, subject, handler)
    _stub_notify(subject, monkeypatch)
    req_path = _write_request(tmp_path, uuid="uuid-123")
    _set_argv(monkeypatch, req_path)

    subject.main()

    assert len(fake.calls) == 2


def test_successful_resume_does_not_fall_back(subject, sandbox, monkeypatch, tmp_path):
    fake = _fake_subprocess(monkeypatch, subject,
                            lambda *a, **k: _completed(stdout="a real answer"))
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
    fake = _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout="ok"))
    _stub_notify(subject, monkeypatch)
    missing = tmp_path / "no-such-brief.txt"
    req_path = _write_request(tmp_path, uuid="", brief=str(missing))
    _set_argv(monkeypatch, req_path)

    subject.main()

    prompt = fake.calls[0][0][0][-1]
    assert prompt == _framed()


def test_brief_unreadable_file_falls_back_to_framed_prompt_only(
    subject, sandbox, monkeypatch, tmp_path
):
    fake = _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout="ok"))
    _stub_notify(subject, monkeypatch)
    # A directory at the brief path exists() == True but read_text() raises — exercises
    # the bare `except Exception: brief_txt = ""` branch.
    brief_dir = tmp_path / "brief_is_a_dir"
    brief_dir.mkdir()
    req_path = _write_request(tmp_path, uuid="", brief=str(brief_dir))
    _set_argv(monkeypatch, req_path)

    subject.main()

    prompt = fake.calls[0][0][0][-1]
    assert prompt == _framed()


def test_brief_present_file_is_prepended_as_context(subject, sandbox, monkeypatch, tmp_path):
    fake = _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout="ok"))
    _stub_notify(subject, monkeypatch)
    brief_file = tmp_path / "brief.txt"
    brief_file.write_text("the task brief content", encoding="utf-8")
    req_path = _write_request(tmp_path, uuid="", brief=str(brief_file))
    _set_argv(monkeypatch, req_path)

    subject.main()

    prompt = fake.calls[0][0][0][-1]
    assert prompt == (
        "CONTEXT — the task brief you worked from:\nthe task brief content\n\n" + _framed()
    )


def test_brief_content_is_truncated_to_6000_chars(subject, sandbox, monkeypatch, tmp_path):
    fake = _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout="ok"))
    _stub_notify(subject, monkeypatch)
    brief_file = tmp_path / "brief.txt"
    brief_file.write_text("x" * 7000, encoding="utf-8")
    req_path = _write_request(tmp_path, uuid="", brief=str(brief_file))
    _set_argv(monkeypatch, req_path)

    subject.main()

    prompt = fake.calls[0][0][0][-1]
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
    _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout=long_answer))
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
    _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout="short answer"))
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
    _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(returncode=0, stdout=""))
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
    _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout=hint))
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
    _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout="usage limit hit"))
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
    _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout=long_answer))
    notified = _stub_notify(subject, monkeypatch)
    req_path = _write_request(tmp_path, uuid="", dan_id="7", task="P12C")
    _set_argv(monkeypatch, req_path)

    subject.main()

    assert notified[0].startswith("💬 *Maestro* — re: `P12C`")
    assert long_answer[:3500] in notified[0]


# =====================================================================================
# main()'s return code
# =====================================================================================


def test_main_returns_0_even_when_the_run_itself_raised(subject, sandbox, monkeypatch, tmp_path):
    def handler(*args, **kwargs):
        raise OSError("boom")

    _fake_subprocess(monkeypatch, subject, handler)
    notified = _stub_notify(subject, monkeypatch)
    req_path = _write_request(tmp_path, uuid="")
    _set_argv(monkeypatch, req_path)

    rc = subject.main()

    assert rc == 0
    assert "(Maestro failed to answer: boom)" in notified[0]


def test_main_returns_0_on_a_timeout(subject, sandbox, monkeypatch, tmp_path):
    def handler(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["claude"], timeout=300)

    _fake_subprocess(monkeypatch, subject, handler)
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
    _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout=long_answer))
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
    _fake_subprocess(monkeypatch, subject, lambda *a, **k: _completed(stdout="ok answer"))
    notified = _stub_notify(subject, monkeypatch)
    req_path = _write_request(tmp_path, uuid="", log=str(log_dir))
    _set_argv(monkeypatch, req_path)

    rc = subject.main()

    assert rc == 0
    assert log_dir.is_dir()  # untouched, still a directory
    assert "ok answer" in notified[0]
