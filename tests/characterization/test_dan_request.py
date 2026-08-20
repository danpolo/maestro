"""Pinned behaviour of the Dan-request sender (B4): `_count_pending`,
`build_message_and_markup`, `_load_env`, `_now_iso` and the `main()` CLI contract.

This is a standalone script in the reference (`scripts/send_dan_request.py`), not a
function living inside `orchestrator_run.py`, so the shared `subject` fixture in
`conftest.py` (which only loads `orchestrator_run`) does not cover it. This file defines
its own `subject` fixture, parametrised `["legacy", "maestro"]`, mirroring `conftest.py`'s
own `_load_legacy` (module-import parametrisation — see the M4c batch-3 task prompt,
"Established conventions" §5, pattern (a)). It overrides the module-scoped `subject`
fixture for this file only; the `sandbox` fixture from `conftest.py` (which depends on
`subject` by name) picks up this file's version automatically.

Nothing here reaches the network: the outbound Telegram call is stubbed at whichever
transport the subject uses — `urllib.request.urlopen` for the legacy reference (forever;
it keeps this shape), `requests.post` for the extracted `maestro.hitl.dan_request` (the
R4-style native-transport substitution — mirrors `tests/characterization/
test_telegram.py`'s `_Post`/`_stub_post` pattern for `notify_telegram`).
"""
from __future__ import annotations

import importlib
import json
import os
import re
import sys
import urllib.request
from types import SimpleNamespace

import pytest
import requests

from tests.reference_repo import LEGACY_REPO

pytestmark = pytest.mark.maestro_module("hitl.dan_request")


# =====================================================================================
# subject / sandbox wiring
# =====================================================================================


def _load_legacy_dan_request():
    if LEGACY_REPO is None:
        pytest.skip(
            "reference repo unknown: set $MAESTRO_LEGACY_REPO or write its path to .legacy_repo"
        )
    scripts = LEGACY_REPO / "scripts"
    if not (scripts / "send_dan_request.py").is_file():
        pytest.skip(f"reference repo not found at {LEGACY_REPO}")
    env_before = dict(os.environ)
    sys.path.insert(0, str(scripts))
    try:
        mod = importlib.import_module("send_dan_request")
    finally:
        sys.path.remove(str(scripts))
        os.environ.clear()
        os.environ.update(env_before)
    return mod


@pytest.fixture(params=["legacy", "maestro"])
def subject(request):
    """The implementation under test — overrides `conftest.py`'s `subject` for this file."""
    if request.param == "legacy":
        return _load_legacy_dan_request()
    return importlib.import_module("maestro.hitl.dan_request")


def _is_maestro(subject) -> bool:
    """True for the extracted `maestro.hitl.dan_request` subject, false for the legacy
    reference. Mirrors `test_commands.py`'s `_is_maestro`: only used to pick the transport
    a network stub has to patch and which command-line prefix an argv assertion expects —
    never to branch on it inside a shared behavioural assertion."""
    return getattr(subject, "__name__", "").split(".")[0] == "maestro"


# =====================================================================================
# network stub — patches whichever transport this subject actually uses
# =====================================================================================


class _FakeHTTPResponse:
    """Stand-in for the context manager `urllib.request.urlopen` returns."""

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _stub_send(subject, monkeypatch, message_id=12345, raises=None):
    """Stub the outbound Telegram call. Returns a list of recorded call dicts
    (`url`, `data`, `timeout`). `message_id=None` simulates an unparseable response body
    (mirrors the reference's `except Exception: return None`)."""
    calls: list[dict] = []
    if _is_maestro(subject):
        body = (
            json.dumps({"result": {"message_id": message_id}})
            if message_id is not None
            else "not-json"
        )

        def fake_post(url, data=None, timeout=None, **kwargs):
            calls.append({"url": url, "data": data, "timeout": timeout})
            if raises is not None:
                raise raises
            return SimpleNamespace(status_code=200, text=body)

        monkeypatch.setattr(requests, "post", fake_post)
    else:
        body = (
            json.dumps({"result": {"message_id": message_id}}).encode()
            if message_id is not None
            else b"not-json"
        )

        def fake_urlopen(req, timeout=None):
            calls.append({"url": req.full_url, "data": req.data, "timeout": timeout})
            if raises is not None:
                raise raises
            return _FakeHTTPResponse(body)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


# =====================================================================================
# _count_pending
# =====================================================================================


def test_count_pending_missing_directory_is_zero(subject, sandbox):
    assert not subject.QUESTIONS.exists()
    assert subject._count_pending() == 0


def test_count_pending_empty_directory_is_zero(subject, sandbox):
    subject.QUESTIONS.mkdir(parents=True)
    assert subject._count_pending() == 0


def test_count_pending_counts_only_pending_status(subject, sandbox):
    subject.QUESTIONS.mkdir(parents=True)
    (subject.QUESTIONS / "a.json").write_text(json.dumps({"status": "pending"}))
    (subject.QUESTIONS / "b.json").write_text(json.dumps({"status": "answered"}))
    (subject.QUESTIONS / "c.json").write_text(json.dumps({"status": "pending"}))
    assert subject._count_pending() == 2


def test_count_pending_skips_unparseable_json(subject, sandbox):
    subject.QUESTIONS.mkdir(parents=True)
    (subject.QUESTIONS / "bad.json").write_text("{not json")
    (subject.QUESTIONS / "good.json").write_text(json.dumps({"status": "pending"}))
    assert subject._count_pending() == 1


def test_count_pending_ignores_non_json_files(subject, sandbox):
    subject.QUESTIONS.mkdir(parents=True)
    (subject.QUESTIONS / "note.txt").write_text("pending")
    assert subject._count_pending() == 0


def test_count_pending_missing_status_field_is_not_counted(subject, sandbox):
    subject.QUESTIONS.mkdir(parents=True)
    (subject.QUESTIONS / "a.json").write_text(json.dumps({"id": "a"}))
    assert subject._count_pending() == 0


# =====================================================================================
# build_message_and_markup
# =====================================================================================


def test_build_message_and_markup_without_options(subject):
    msg, markup = subject.build_message_and_markup("decision", "r1", "Ship it?", None)
    assert msg == (
        "\U0001F914 <b>Dan-request [decision]</b>\n\n"
        "Ship it?\n\n"
        "<i>Reply with any text to answer.</i>"
    )
    assert markup is None


def test_build_message_and_markup_with_options(subject):
    msg, markup = subject.build_message_and_markup(
        "decision", "r1", "Ship it?", ["yes", "no"]
    )
    assert msg == (
        "\U0001F914 <b>Dan-request [decision]</b>\n\n"
        "Ship it?\n\n"
        "<b>1.</b> yes\n"
        "<b>2.</b> no\n\n"
        "<i>Tap a number below — or just reply with your own text "
        "(notes, or an answer that isn't listed).</i>"
    )
    assert markup == {
        "inline_keyboard": [
            [{"text": "1", "callback_data": "danreq:r1:0"}],
            [{"text": "2", "callback_data": "danreq:r1:1"}],
        ]
    }


def test_build_message_and_markup_empty_options_list_takes_the_free_text_branch(subject):
    """`[]` is falsy, so an empty options list is treated the same as `None`."""
    msg, markup = subject.build_message_and_markup("decision", "r1", "q", [])
    assert markup is None
    assert "Reply with any text to answer." in msg


@pytest.mark.parametrize(
    "req_type, emoji",
    [
        ("decision", "\U0001F914"),
        ("manual-task", "\U0001F6E0️"),
        ("phase-approval", "✅"),
        ("some-unknown-type", "❓"),
    ],
)
def test_build_message_and_markup_emoji_per_type(subject, req_type, emoji):
    msg, _ = subject.build_message_and_markup(req_type, "r1", "q", None)
    assert msg.startswith(f"{emoji} <b>Dan-request [{req_type}]</b>")


def test_build_message_and_markup_callback_data_is_zero_based(subject):
    _, markup = subject.build_message_and_markup(
        "decision", "req-42", "q", ["a", "b", "c"]
    )
    callback_data = [row[0]["callback_data"] for row in markup["inline_keyboard"]]
    assert callback_data == ["danreq:req-42:0", "danreq:req-42:1", "danreq:req-42:2"]


def test_build_message_and_markup_buttons_are_one_per_row(subject):
    _, markup = subject.build_message_and_markup("decision", "r1", "q", ["a", "b", "c"])
    assert all(len(row) == 1 for row in markup["inline_keyboard"])


def test_build_message_and_markup_option_text_is_never_truncated(subject):
    long_opt = "x" * 500
    msg, _ = subject.build_message_and_markup("decision", "r1", "q", [long_opt])
    assert long_opt in msg


def test_build_message_and_markup_question_appears_in_the_body(subject):
    msg, _ = subject.build_message_and_markup("decision", "r1", "Approve P12B run?", None)
    assert "Approve P12B run?" in msg


# =====================================================================================
# _now_iso
# =====================================================================================


def test_now_iso_matches_the_expected_format(subject):
    ts = subject._now_iso()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", ts)


# =====================================================================================
# _load_env
# =====================================================================================


def test_load_env_missing_file_is_a_noop(subject, sandbox):
    assert not (subject.REPO / ".env").exists()
    assert subject._load_env() is None


def test_load_env_sets_variables_from_the_repo_env_file(subject, sandbox, monkeypatch):
    monkeypatch.delenv("UNIT_TEST_DANREQ_VAR", raising=False)
    subject.REPO.mkdir(parents=True, exist_ok=True)
    (subject.REPO / ".env").write_text("UNIT_TEST_DANREQ_VAR=hello\n")
    try:
        subject._load_env()
        assert os.environ["UNIT_TEST_DANREQ_VAR"] == "hello"
    finally:
        os.environ.pop("UNIT_TEST_DANREQ_VAR", None)


def test_load_env_skips_comments_and_blank_lines(subject, sandbox, monkeypatch):
    monkeypatch.delenv("UNIT_TEST_DANREQ_VAR", raising=False)
    subject.REPO.mkdir(parents=True, exist_ok=True)
    (subject.REPO / ".env").write_text("# a comment\n\nUNIT_TEST_DANREQ_VAR=hello\n")
    try:
        subject._load_env()
        assert os.environ["UNIT_TEST_DANREQ_VAR"] == "hello"
    finally:
        os.environ.pop("UNIT_TEST_DANREQ_VAR", None)


def test_load_env_strips_surrounding_quotes(subject, sandbox, monkeypatch):
    monkeypatch.delenv("UNIT_TEST_DANREQ_VAR", raising=False)
    subject.REPO.mkdir(parents=True, exist_ok=True)
    (subject.REPO / ".env").write_text('UNIT_TEST_DANREQ_VAR="hello world"\n')
    try:
        subject._load_env()
        assert os.environ["UNIT_TEST_DANREQ_VAR"] == "hello world"
    finally:
        os.environ.pop("UNIT_TEST_DANREQ_VAR", None)


def test_load_env_ignores_a_line_without_an_equals_sign(subject, sandbox, monkeypatch):
    monkeypatch.delenv("UNIT_TEST_DANREQ_VAR", raising=False)
    subject.REPO.mkdir(parents=True, exist_ok=True)
    (subject.REPO / ".env").write_text("this is not a var\n")
    subject._load_env()
    assert "UNIT_TEST_DANREQ_VAR" not in os.environ


# =====================================================================================
# main() — CLI contract
# =====================================================================================


def _set_argv(monkeypatch, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", ["send_dan_request", *args])


def test_main_missing_token_returns_1_and_prints_only_to_stderr(
    subject, sandbox, monkeypatch, capsys
):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    _set_argv(monkeypatch, "--type", "decision", "--question", "q")
    rc = subject.main()
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "TELEGRAM_BOT_TOKEN" in captured.err


def test_main_missing_chat_id_returns_1(subject, sandbox, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.delenv("TELEGRAM_ALERT_CHAT_ID", raising=False)
    _set_argv(monkeypatch, "--type", "decision", "--question", "q")
    rc = subject.main()
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "TELEGRAM_ALERT_CHAT_ID" in captured.err


def test_main_missing_token_does_not_write_a_question_file(subject, sandbox, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    _set_argv(monkeypatch, "--type", "decision", "--question", "q")
    subject.main()
    assert not subject.QUESTIONS.exists() or not list(subject.QUESTIONS.glob("*.json"))


def test_main_pending_cap_reached_returns_1(subject, sandbox, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    subject.QUESTIONS.mkdir(parents=True)
    for i in range(3):
        (subject.QUESTIONS / f"p{i}.json").write_text(json.dumps({"status": "pending"}))
    calls = _stub_send(subject, monkeypatch)
    _set_argv(monkeypatch, "--type", "decision", "--question", "q")
    rc = subject.main()
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "3 outstanding requests" in captured.err
    assert "cap=3" in captured.err
    assert calls == []  # never even attempted the send


def test_main_below_the_pending_cap_still_sends(subject, sandbox, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    subject.QUESTIONS.mkdir(parents=True)
    for i in range(2):
        (subject.QUESTIONS / f"p{i}.json").write_text(json.dumps({"status": "pending"}))
    _stub_send(subject, monkeypatch)
    _set_argv(monkeypatch, "--type", "decision", "--question", "q", "--id", "danreq-below-cap")
    rc = subject.main()
    assert rc == 0


def test_main_successful_send_prints_only_the_id_to_stdout(
    subject, sandbox, monkeypatch, capsys
):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    _stub_send(subject, monkeypatch, message_id=999)
    _set_argv(
        monkeypatch,
        "--type", "decision", "--question", "Ship it?",
        "--options", "yes,no", "--id", "danreq-test-1",
    )
    rc = subject.main()
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == "danreq-test-1\n"
    assert "Parked:" in captured.err
    assert "Telegram message sent" in captured.err
    assert "danreq-test-1" in captured.err


def test_main_persists_the_parked_record_with_the_message_id(subject, sandbox, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    _stub_send(subject, monkeypatch, message_id=999)
    _set_argv(
        monkeypatch,
        "--type", "decision", "--question", "Ship it?",
        "--options", "yes,no", "--id", "danreq-test-2",
    )
    subject.main()
    rec = json.loads((subject.QUESTIONS / "danreq-test-2.json").read_text())
    assert rec["id"] == "danreq-test-2"
    assert rec["type"] == "decision"
    assert rec["question"] == "Ship it?"
    assert rec["options"] == ["yes", "no"]
    assert rec["status"] == "pending"
    assert rec["message_id"] == 999


def test_main_omitted_options_are_recorded_as_none(subject, sandbox, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    _stub_send(subject, monkeypatch, message_id=1)
    _set_argv(
        monkeypatch, "--type", "manual-task", "--question", "q", "--id", "danreq-free"
    )
    subject.main()
    rec = json.loads((subject.QUESTIONS / "danreq-free.json").read_text())
    assert rec["options"] is None


def test_main_generates_an_id_when_none_is_given(subject, sandbox, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    _stub_send(subject, monkeypatch, message_id=1)
    _set_argv(monkeypatch, "--type", "decision", "--question", "q")
    rc = subject.main()
    captured = capsys.readouterr()
    assert rc == 0
    req_id = captured.out.strip()
    assert req_id.startswith("danreq-")
    assert (subject.QUESTIONS / f"{req_id}.json").exists()


def test_main_treats_an_unparseable_response_as_no_message_id(subject, sandbox, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    _stub_send(subject, monkeypatch, message_id=None)
    _set_argv(monkeypatch, "--type", "decision", "--question", "q", "--id", "danreq-bad-resp")
    rc = subject.main()
    assert rc == 0
    rec = json.loads((subject.QUESTIONS / "danreq-bad-resp.json").read_text())
    assert "message_id" not in rec


def test_main_returns_1_when_the_send_raises(subject, sandbox, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    _stub_send(subject, monkeypatch, raises=OSError("boom"))
    _set_argv(monkeypatch, "--type", "decision", "--question", "q", "--id", "danreq-err")
    rc = subject.main()
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "Telegram error" in captured.err
    # The request was parked to disk before the send was attempted.
    assert (subject.QUESTIONS / "danreq-err.json").exists()


def test_main_rejects_an_invalid_type(subject, sandbox, monkeypatch):
    _set_argv(monkeypatch, "--type", "bogus", "--question", "q")
    with pytest.raises(SystemExit):
        subject.main()


def test_main_requires_a_question(subject, sandbox, monkeypatch):
    _set_argv(monkeypatch, "--type", "decision")
    with pytest.raises(SystemExit):
        subject.main()


# =====================================================================================
# main() — TARGET: the transport used to send the message
# =====================================================================================
# The reference sends via `urllib.request`/`urllib.parse` (`send_dan_request.py`'s own
# `_send_telegram`); `maestro.hitl.dan_request` sends via `requests.post`, mirroring R4's
# `notify_telegram` migration (`docs/plans/2026-08-18-m4c-superseded-sidecars.md`). Both
# sides are pinned here — this is not a skip, it's a genuine two-subject characterisation
# of two different, correct transports for the same contract.


def test_main_posts_to_the_send_message_endpoint(subject, sandbox, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    calls = _stub_send(subject, monkeypatch, message_id=1)
    _set_argv(monkeypatch, "--type", "decision", "--question", "q", "--id", "danreq-url")
    subject.main()
    assert len(calls) == 1
    assert calls[0]["url"] == "https://api.telegram.org/botunit-test-token/sendMessage"


def test_main_bounds_the_send_with_a_ten_second_timeout(subject, sandbox, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    calls = _stub_send(subject, monkeypatch, message_id=1)
    _set_argv(monkeypatch, "--type", "decision", "--question", "q", "--id", "danreq-timeout")
    subject.main()
    assert calls[0]["timeout"] == 10


def test_main_uses_requests_only_for_the_maestro_subject(subject, sandbox, monkeypatch):
    """Pins the R4-style transport split explicitly: patching `requests.post` alone must
    be a no-op for the legacy reference (it never imports `requests`), and patching
    `urllib.request.urlopen` alone must be a no-op for maestro."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    other_calls: list = []

    if _is_maestro(subject):
        def fake_urlopen(req, timeout=None):
            other_calls.append(req)
            raise AssertionError("maestro should not touch urllib.request.urlopen")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        calls = _stub_send(subject, monkeypatch, message_id=1)
    else:
        def fake_post(*args, **kwargs):
            other_calls.append(args)
            raise AssertionError("legacy should not touch requests.post")

        monkeypatch.setattr(requests, "post", fake_post)
        calls = _stub_send(subject, monkeypatch, message_id=1)

    _set_argv(monkeypatch, "--type", "decision", "--question", "q", "--id", "danreq-transport")
    rc = subject.main()
    assert rc == 0
    assert other_calls == []
    assert len(calls) == 1
