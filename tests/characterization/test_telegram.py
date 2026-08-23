"""Pinned behaviour of the Telegram / HITL layer: notifications, the Bot API shim,
the Dan-request lifecycle and the parked-item reminder cadence.

Characterisation, not specification. **Nothing here reaches the network.** Every function
under test shells out through ``subprocess.run`` (``bash notify_telegram.sh``, ``curl``,
``send_dan_request.py``), so the tests replace ``subprocess.run`` with a recorder and
assert on the *argv and kwargs that would have been executed* — the request payload is the
contract. The handful of tests that deliberately let a real process run only ever run
``bash`` against a script the test itself wrote inside the sandbox.

Two rules the tests here follow:

* Path globals are read off the subject (``QUESTIONS_DIR``, ``NOTIFY_SH``, ``STATE_JSON``
  …) with a sandbox-relative fallback, never hardcoded, so the same body works against the
  reference module (one flat namespace) and against the extracted package (where
  ``read_state`` and friends live in a sibling module).
* Where the reference's wording is incidental the assertion is on structure (the payload
  carries the request id, the verb names the right command); where the wording *is* the
  contract — journal event names, callback-data grammar, the ``danreq:`` prefix, the
  ``--data-urlencode`` spelling — it is pinned exactly.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

pytestmark = pytest.mark.maestro_module("hitl.telegram")


# The reference's user-facing strings are emoji-prefixed; spelled out here so the test
# source stays legible while still pinning the exact bytes the operator sees.
CHECK = "\N{WHITE HEAVY CHECK MARK}"          # confirmation / "shipped" prefix
ALARM = "\N{ALARM CLOCK}"                     # parked-item reminder prefix
CHART = "\N{BAR CHART}"                       # metrics / smoke-reason line
CLIPBOARD = "\N{CLIPBOARD}"                   # decisions block
HOURGLASS = "\N{HOURGLASS WITH FLOWING SAND}"  # veto window
NEXT = "\N{BLACK RIGHT-POINTING TRIANGLE}"    # "Next:" line


# --- locating the subject's paths -----------------------------------------------------


def _path(subject, name: str, fallback: Path) -> Path:
    """A path global off the subject, or `fallback` when the subject does not own it.

    The reference module is one flat namespace and owns every path. The extracted package
    splits them up — `read_state` resolves `maestro.state.STATE_JSON`, which the telegram
    module never names. The `sandbox` fixture rebases both, and both land on the same
    place inside the temp tree, so the fallback is the same file either way.
    """
    value = getattr(subject, name, None)
    return value if isinstance(value, Path) else fallback


def _questions_dir(subject, sandbox) -> Path:
    return _path(subject, "QUESTIONS_DIR", sandbox.orch_dir / "questions")


def _state_json(subject, sandbox) -> Path:
    return _path(subject, "STATE_JSON", sandbox.orch_dir / "state.json")


def _journal(subject, sandbox) -> Path:
    return _path(subject, "JOURNAL", sandbox.orch_dir / "journal.ndjson")


def _notify_sh(subject, sandbox) -> Path:
    return _path(subject, "NOTIFY_SH", sandbox.repo / "scripts" / "notify_telegram.sh")


def _dep_map_png(subject, sandbox) -> Path:
    return _path(subject, "DEP_MAP_PNG", sandbox.repo / "docs" / "dependency_map.png")


def _last_map_sig(subject, sandbox) -> Path:
    return _path(subject, "LAST_MAP_SIG", sandbox.orch_dir / "last_map_roadmap.sha")


def _reminder_interval(subject) -> int:
    return int(getattr(subject, "REMINDER_INTERVAL_SEC", 86400))


# --- fakes ----------------------------------------------------------------------------


class _Run:
    """Stand-in for `subprocess.run`: records argv/kwargs, replays canned results."""

    def __init__(self, results=None, raises=None):
        self.calls: list[SimpleNamespace] = []
        self._results = list(results or [])
        self._raises = raises

    def __call__(self, argv, **kwargs):
        self.calls.append(SimpleNamespace(argv=list(argv), kwargs=dict(kwargs)))
        if self._raises is not None:
            raise self._raises
        if self._results:
            return self._results.pop(0)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    @property
    def argvs(self) -> list[list]:
        return [c.argv for c in self.calls]


def _stub_run(monkeypatch, results=None, raises=None) -> _Run:
    run = _Run(results, raises)
    monkeypatch.setattr(subprocess, "run", run)
    return run


def _result(stdout="", returncode=0):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


class _Calls(list):
    """Callable that appends every invocation as (args, kwargs)."""

    def __call__(self, *args, **kwargs):
        self.append((args, kwargs))
        return None

    @property
    def first_arg(self):
        return self[0][0][0]

    @property
    def first_args(self) -> list:
        return [c[0][0] for c in self]


class _Raiser:
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        raise self.exc


def _spy_notify(monkeypatch, subject) -> _Calls:
    calls = _Calls()
    monkeypatch.setattr(subject, "notify_telegram", calls)
    return calls


def _spy_tg_api(monkeypatch, subject) -> _Calls:
    calls = _Calls()
    monkeypatch.setattr(subject, "_tg_api", calls)
    return calls


# --- questions directory --------------------------------------------------------------


def _write_req(subject, sandbox, req_id: str, **fields) -> Path:
    """A Dan-request record on disk, with the shape send_dan_request.py writes."""
    qdir = _questions_dir(subject, sandbox)
    qdir.mkdir(parents=True, exist_ok=True)
    rec = {"id": req_id, "status": "pending", "ts": "2026-01-01T00:00:00Z"}
    rec.update(fields)
    p = qdir / f"{req_id}.json"
    p.write_text(json.dumps(rec), encoding="utf-8")
    return p


def _read_req(subject, sandbox, req_id: str) -> dict:
    return json.loads((_questions_dir(subject, sandbox) / f"{req_id}.json").read_text())


def _answer_file(subject, sandbox, req_id: str) -> Path:
    return _questions_dir(subject, sandbox) / f"{req_id}.answer"


# --- journal --------------------------------------------------------------------------


def _events(subject, sandbox) -> list[dict]:
    p = _journal(subject, sandbox)
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _event_names(subject, sandbox) -> list[str]:
    return [e["event"] for e in _events(subject, sandbox)]


def _detail(subject, sandbox, event: str) -> str:
    for e in _events(subject, sandbox):
        if e["event"] == event:
            return e["detail"]
    raise AssertionError(f"no {event!r} journal record in {_event_names(subject, sandbox)}")


# --- state ----------------------------------------------------------------------------


def _write_state(subject, sandbox, waiting: dict | None = None, **extra) -> Path:
    p = _state_json(subject, sandbox)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = {"waiting_on_dan": waiting if waiting is not None else {}}
    doc.update(extra)
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def _read_state_file(subject, sandbox) -> dict:
    return json.loads(_state_json(subject, sandbox).read_text())


def _ago(**kwargs) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(**kwargs)).isoformat()


# =====================================================================================
# notify_telegram
# =====================================================================================


def test_notify_telegram_does_nothing_when_the_notifier_script_is_absent(
    subject, sandbox, monkeypatch
):
    run = _stub_run(monkeypatch)
    assert subject.notify_telegram("hello") is None
    assert run.calls == []


def test_notify_telegram_returns_none(subject, sandbox, monkeypatch):
    sh = _notify_sh(subject, sandbox)
    sh.parent.mkdir(parents=True, exist_ok=True)
    sh.write_text("#!/bin/bash\n", encoding="utf-8")
    _stub_run(monkeypatch)
    assert subject.notify_telegram("hello") is None


def test_notify_telegram_ignores_a_failing_notifier(subject, sandbox, monkeypatch):
    """FOUND_BUGS: the exit status is never inspected — a broken notifier is silent."""
    sh = _notify_sh(subject, sandbox)
    sh.parent.mkdir(parents=True, exist_ok=True)
    sh.write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
    _stub_run(monkeypatch, results=[_result(returncode=1)])
    assert subject.notify_telegram("hello") is None


# =====================================================================================
# notify_telegram — TARGET (M4c/R4)
# =====================================================================================
# `docs/plans/2026-08-18-m4c-superseded-sidecars.md` R4: `NOTIFY_SH`/`scripts/notify_telegram.sh`
# is one of the ten scripts M5 deletes. Once it is gone, the shell-out above silently sends
# nothing — ~20 call sites across `orchestrator.py`, `quota.py`, `selfheal/*` and
# `hitl/commands.py` go dark with no error. The target: `notify_telegram` stops shelling out
# and sends the message directly through the already-allowed `requests` dependency, straight
# to the Telegram Bot API's `sendMessage` endpoint — reading `TELEGRAM_BOT_TOKEN` and
# `TELEGRAM_ALERT_CHAT_ID` off `os.environ` exactly like its neighbours `_tg_api` and
# `notify_telegram_with_map` already do in this module. Never raises: a missing token/chat id
# or a failed request is silently swallowed, same as today.
#
# These tests are pinned against the *target*, not the reference: the read-only reference
# project keeps `notify_telegram.sh` forever, so they are gated to the extracted `maestro`
# subject only (`_is_maestro`), mirroring `test_watchdog.py`'s `_notifies_via_script` gate.
# They are expected to be RED against today's still-shelling-out `maestro.hitl.telegram`
# until a later stage migrates it.


class _Post:
    """Stand-in for `requests.post`: records calls, replays a canned response or raises."""

    def __init__(self, raises=None):
        self.calls: list[SimpleNamespace] = []
        self._raises = raises

    def __call__(self, *args, **kwargs):
        self.calls.append(SimpleNamespace(args=args, kwargs=kwargs))
        if self._raises is not None:
            raise self._raises
        return SimpleNamespace(status_code=200, text='{"ok":true}')


def _stub_post(monkeypatch, raises=None) -> _Post:
    post = _Post(raises)
    monkeypatch.setattr(requests, "post", post)
    return post


def _post_payload(call) -> dict:
    payload = call.kwargs.get("data")
    if payload is None:
        payload = call.kwargs.get("json")
    if payload is None:
        payload = call.kwargs.get("params")
    assert payload is not None, f"no request payload found in kwargs: {call.kwargs}"
    return payload


def _post_url(call) -> str:
    return call.args[0] if call.args else call.kwargs.get("url")


def test_notify_telegram_target_does_nothing_without_a_bot_token(
    subject, sandbox, monkeypatch
):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    post = _stub_post(monkeypatch)
    assert subject.notify_telegram("hello") is None
    assert post.calls == []


def test_notify_telegram_target_treats_an_empty_bot_token_as_absent(
    subject, sandbox, monkeypatch
):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    post = _stub_post(monkeypatch)
    subject.notify_telegram("hello")
    assert post.calls == []


def test_notify_telegram_target_does_nothing_without_a_chat_id(
    subject, sandbox, monkeypatch
):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.delenv("TELEGRAM_ALERT_CHAT_ID", raising=False)
    post = _stub_post(monkeypatch)
    subject.notify_telegram("hello")
    assert post.calls == []


def test_notify_telegram_target_treats_an_empty_chat_id_as_absent(
    subject, sandbox, monkeypatch
):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "")
    post = _stub_post(monkeypatch)
    subject.notify_telegram("hello")
    assert post.calls == []


def test_notify_telegram_target_posts_to_the_send_message_endpoint(
    subject, sandbox, monkeypatch
):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    post = _stub_post(monkeypatch)
    subject.notify_telegram("hello")
    assert len(post.calls) == 1
    assert _post_url(post.calls[0]) == (
        "https://api.telegram.org/botunit-test-token/sendMessage"
    )


def test_notify_telegram_target_sends_chat_id_and_text_in_the_payload(
    subject, sandbox, monkeypatch
):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    post = _stub_post(monkeypatch)
    subject.notify_telegram("hello world")
    payload = _post_payload(post.calls[0])
    assert str(payload.get("chat_id")) == "555"
    assert payload.get("text") == "hello world"


def test_notify_telegram_target_passes_a_multiline_message_through(
    subject, sandbox, monkeypatch
):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    post = _stub_post(monkeypatch)
    subject.notify_telegram("line one\nline two")
    assert _post_payload(post.calls[0]).get("text") == "line one\nline two"


def test_notify_telegram_target_bounds_the_call_with_a_short_timeout(
    subject, sandbox, monkeypatch
):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    post = _stub_post(monkeypatch)
    subject.notify_telegram("hello")
    timeout = post.calls[0].kwargs.get("timeout")
    assert timeout is not None
    assert 0 < timeout <= 15


def test_notify_telegram_target_swallows_a_request_exception(
    subject, sandbox, monkeypatch
):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    _stub_post(monkeypatch, raises=requests.exceptions.RequestException("boom"))
    assert subject.notify_telegram("hello") is None


def test_notify_telegram_target_swallows_a_timeout(subject, sandbox, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    _stub_post(monkeypatch, raises=requests.exceptions.Timeout("timed out"))
    assert subject.notify_telegram("hello") is None


def test_notify_telegram_target_returns_none_on_success(subject, sandbox, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    _stub_post(monkeypatch)
    assert subject.notify_telegram("hello") is None


def test_notify_telegram_target_does_not_shell_out(subject, sandbox, monkeypatch):
    """The shell-out and its `.exists()` guard go away per R4 — no `subprocess.run` call,
    even though `NOTIFY_SH`/`notify_telegram.sh` is present on disk (today's `.exists()`
    guard would otherwise take that branch)."""
    sh = _notify_sh(subject, sandbox)
    sh.parent.mkdir(parents=True, exist_ok=True)
    sh.write_text("#!/bin/bash\n", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    run = _stub_run(monkeypatch)
    _stub_post(monkeypatch)
    subject.notify_telegram("hello")
    assert run.calls == []


# =====================================================================================
# notify_telegram_with_map
# =====================================================================================


def _prepare_map(subject, sandbox, monkeypatch, png: bool = True, sig: str | None = "sig"):
    """Common setup: stub the text notifier and the roadmap signature, optional PNG."""
    notify = _spy_notify(monkeypatch, subject)
    if sig is not None:
        monkeypatch.setattr(subject, "_roadmap_signature", lambda: sig)
    if png:
        p = _dep_map_png(subject, sandbox)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\x89PNG\r\n")
    return notify


def test_notify_telegram_with_map_always_sends_the_text_message_first(
    subject, sandbox, monkeypatch
):
    notify = _prepare_map(subject, sandbox, monkeypatch, png=False)
    _stub_run(monkeypatch)
    subject.notify_telegram_with_map("body")
    assert notify.first_args == ["body"]


def test_notify_telegram_with_map_skips_the_upload_when_no_png_exists(
    subject, sandbox, monkeypatch
):
    _prepare_map(subject, sandbox, monkeypatch, png=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    run = _stub_run(monkeypatch)
    subject.notify_telegram_with_map("body")
    assert run.calls == []


def test_notify_telegram_with_map_skips_the_upload_without_a_token(
    subject, sandbox, monkeypatch
):
    _prepare_map(subject, sandbox, monkeypatch)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    run = _stub_run(monkeypatch)
    subject.notify_telegram_with_map("body")
    assert run.calls == []


def test_notify_telegram_with_map_skips_the_upload_without_a_chat_id(
    subject, sandbox, monkeypatch
):
    _prepare_map(subject, sandbox, monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.delenv("TELEGRAM_ALERT_CHAT_ID", raising=False)
    run = _stub_run(monkeypatch)
    subject.notify_telegram_with_map("body")
    assert run.calls == []


def test_notify_telegram_with_map_uploads_via_send_photo(subject, sandbox, monkeypatch):
    _prepare_map(subject, sandbox, monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    run = _stub_run(monkeypatch, results=[_result('{"ok":true}')])
    subject.notify_telegram_with_map("body")
    assert len(run.calls) == 1
    argv = run.argvs[0]
    assert argv[0] == "curl"
    assert "-s" in argv
    assert f"chat_id=555" in argv
    assert f"photo=@{_dep_map_png(subject, sandbox)}" in argv
    assert argv[-1].endswith("/sendPhoto")


def test_notify_telegram_with_map_send_photo_kwargs(subject, sandbox, monkeypatch):
    _prepare_map(subject, sandbox, monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    run = _stub_run(monkeypatch, results=[_result('{"ok":true}')])
    subject.notify_telegram_with_map("body")
    assert run.calls[0].kwargs == {"capture_output": True, "text": True, "timeout": 30}


def test_notify_telegram_with_map_embeds_the_bot_token_in_the_url(
    subject, sandbox, monkeypatch
):
    """FOUND_BUGS: the token is an argv element, so it is visible in the process table."""
    _prepare_map(subject, sandbox, monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    run = _stub_run(monkeypatch, results=[_result('{"ok":true}')])
    subject.notify_telegram_with_map("body")
    assert run.argvs[0][-1] == (
        "https://api.telegram.org/botunit-test-token/sendPhoto"
    )


def test_notify_telegram_with_map_falls_back_to_send_document(subject, sandbox, monkeypatch):
    _prepare_map(subject, sandbox, monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    run = _stub_run(monkeypatch, results=[_result('{"ok":false,"error_code":400}')])
    subject.notify_telegram_with_map("body")
    assert len(run.calls) == 2
    argv = run.argvs[1]
    assert f"document=@{_dep_map_png(subject, sandbox)}" in argv
    assert argv[-1].endswith("/sendDocument")


def test_notify_telegram_with_map_falls_back_when_curl_produced_no_output(
    subject, sandbox, monkeypatch
):
    _prepare_map(subject, sandbox, monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    run = _stub_run(monkeypatch, results=[_result(None)])
    subject.notify_telegram_with_map("body")
    assert len(run.calls) == 2
    assert run.argvs[1][-1].endswith("/sendDocument")


def test_notify_telegram_with_map_success_probe_is_a_literal_substring(
    subject, sandbox, monkeypatch
):
    """FOUND_BUGS: `'"ok":true' in stdout` — a response with a space after the colon,
    which the Bot API is free to emit, is read as a failure and re-uploaded."""
    _prepare_map(subject, sandbox, monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    run = _stub_run(monkeypatch, results=[_result('{"ok": true}')])
    subject.notify_telegram_with_map("body")
    assert len(run.calls) == 2


def test_notify_telegram_with_map_ignores_the_fallback_result(subject, sandbox, monkeypatch):
    """FOUND_BUGS: sendDocument's own response is never checked — a double failure
    is completely silent."""
    _prepare_map(subject, sandbox, monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    run = _stub_run(monkeypatch, results=[_result('{"ok":false}'), _result('{"ok":false}')])
    subject.notify_telegram_with_map("body")
    assert len(run.calls) == 2


def test_notify_telegram_with_map_records_the_roadmap_signature(
    subject, sandbox, monkeypatch
):
    _prepare_map(subject, sandbox, monkeypatch, sig="deadbeef")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    _stub_run(monkeypatch, results=[_result('{"ok":true}')])
    subject.notify_telegram_with_map("body")
    assert _last_map_sig(subject, sandbox).read_text() == "deadbeef"


def test_notify_telegram_with_map_records_the_signature_even_when_nothing_was_uploaded(
    subject, sandbox, monkeypatch
):
    """FOUND_BUGS: with no PNG (or no token) the dedup stamp is written anyway, so the
    change-detector believes Dan has a map he was never sent."""
    _prepare_map(subject, sandbox, monkeypatch, png=False, sig="deadbeef")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    _stub_run(monkeypatch)
    subject.notify_telegram_with_map("body")
    assert _last_map_sig(subject, sandbox).read_text() == "deadbeef"


def test_notify_telegram_with_map_records_the_signature_even_when_the_upload_failed(
    subject, sandbox, monkeypatch
):
    _prepare_map(subject, sandbox, monkeypatch, sig="deadbeef")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    _stub_run(monkeypatch, results=[_result('{"ok":false}'), _result('{"ok":false}')])
    subject.notify_telegram_with_map("body")
    assert _last_map_sig(subject, sandbox).read_text() == "deadbeef"


def test_notify_telegram_with_map_skips_the_stamp_on_an_empty_signature(
    subject, sandbox, monkeypatch
):
    _prepare_map(subject, sandbox, monkeypatch, png=False, sig="")
    _stub_run(monkeypatch)
    subject.notify_telegram_with_map("body")
    assert not _last_map_sig(subject, sandbox).exists()


def test_notify_telegram_with_map_swallows_a_failure_to_write_the_stamp(
    subject, sandbox, monkeypatch
):
    _prepare_map(subject, sandbox, monkeypatch, png=False, sig="deadbeef")
    _stub_run(monkeypatch)
    target = _last_map_sig(subject, sandbox)
    target.mkdir(parents=True, exist_ok=True)  # a directory: write_text must fail
    assert subject.notify_telegram_with_map("body") is None


def test_notify_telegram_with_map_lets_a_text_notify_failure_propagate(
    subject, sandbox, monkeypatch
):
    monkeypatch.setattr(subject, "notify_telegram", _Raiser(RuntimeError("boom")))
    _stub_run(monkeypatch)
    with pytest.raises(RuntimeError):
        subject.notify_telegram_with_map("body")


def test_notify_telegram_with_map_returns_none(subject, sandbox, monkeypatch):
    _prepare_map(subject, sandbox, monkeypatch, png=False)
    _stub_run(monkeypatch)
    assert subject.notify_telegram_with_map("body") is None


def test_notify_telegram_with_map_propagates_a_curl_timeout(subject, sandbox, monkeypatch):
    """FOUND_BUGS: the upload is not wrapped — a curl timeout escapes the notifier."""
    _prepare_map(subject, sandbox, monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "555")
    _stub_run(monkeypatch, raises=subprocess.TimeoutExpired(cmd="curl", timeout=30))
    with pytest.raises(subprocess.TimeoutExpired):
        subject.notify_telegram_with_map("body")


# =====================================================================================
# _tg_api
# =====================================================================================


def test_tg_api_does_nothing_without_a_token(subject, sandbox, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    run = _stub_run(monkeypatch)
    assert subject._tg_api("answerCallbackQuery", {"text": "x"}) is None
    assert run.calls == []


def test_tg_api_treats_an_empty_token_as_absent(subject, sandbox, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    run = _stub_run(monkeypatch)
    subject._tg_api("answerCallbackQuery", {"text": "x"})
    assert run.calls == []


def test_tg_api_builds_the_curl_prefix(subject, sandbox, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    run = _stub_run(monkeypatch)
    subject._tg_api("sendMessage", {})
    assert run.argvs[0] == [
        "curl", "-s", "--max-time", "5",
        "https://api.telegram.org/botunit-test-token/sendMessage",
    ]


def test_tg_api_appends_each_param_as_data_urlencode(subject, sandbox, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    run = _stub_run(monkeypatch)
    subject._tg_api("answerCallbackQuery", {"callback_query_id": "cq1", "text": "done"})
    assert run.argvs[0][5:] == [
        "--data-urlencode", "callback_query_id=cq1",
        "--data-urlencode", "text=done",
    ]


def test_tg_api_preserves_param_order(subject, sandbox, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    run = _stub_run(monkeypatch)
    subject._tg_api("m", {"b": 1, "a": 2, "c": 3})
    encoded = [a for a in run.argvs[0] if "=" in a and not a.startswith("http")]
    assert encoded == ["b=1", "a=2", "c=3"]


def test_tg_api_stringifies_non_string_values(subject, sandbox, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    run = _stub_run(monkeypatch)
    subject._tg_api("m", {"message_id": 17, "flag": True, "none": None})
    assert "message_id=17" in run.argvs[0]
    assert "flag=True" in run.argvs[0]
    assert "none=None" in run.argvs[0]


def test_tg_api_passes_values_raw_and_lets_curl_do_the_encoding(
    subject, sandbox, monkeypatch
):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    run = _stub_run(monkeypatch)
    subject._tg_api("m", {"text": "a & b\nsecond"})
    assert "text=a & b\nsecond" in run.argvs[0]


def test_tg_api_kwargs(subject, sandbox, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    run = _stub_run(monkeypatch)
    subject._tg_api("m", {})
    assert run.calls[0].kwargs == {"capture_output": True, "timeout": 8}


def test_tg_api_never_raises_on_a_subprocess_failure(subject, sandbox, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    _stub_run(monkeypatch, raises=OSError("no curl"))
    assert subject._tg_api("m", {}) is None


def test_tg_api_never_raises_on_a_timeout(subject, sandbox, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    _stub_run(monkeypatch, raises=subprocess.TimeoutExpired(cmd="curl", timeout=8))
    assert subject._tg_api("m", {}) is None


def test_tg_api_discards_the_api_response(subject, sandbox, monkeypatch):
    """FOUND_BUGS: best-effort by design — a rejected Bot API call is indistinguishable
    from a successful one, so a stale keyboard is never noticed."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    _stub_run(monkeypatch, results=[_result('{"ok":false,"description":"bad request"}')])
    assert subject._tg_api("m", {}) is None


def test_tg_api_does_not_validate_the_method_name(subject, sandbox, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    run = _stub_run(monkeypatch)
    subject._tg_api("../../etc/passwd", {})
    assert run.argvs[0][-1].endswith("/../../etc/passwd")


# =====================================================================================
# _danreq
# =====================================================================================


def test_danreq_builds_the_send_script_command(subject, sandbox, monkeypatch):
    run = _stub_run(monkeypatch)
    subject._danreq("Ship it?", ["yes", "no"])
    prefix = [str(subject.VENV_PYTHON), "-m", "maestro.hitl.dan_request"]
    assert run.argvs[0] == prefix + [
        "--type", "decision",
        "--question", "Ship it?",
        "--options", "yes,no",
    ]


def test_danreq_defaults_the_request_type_to_decision(subject, sandbox, monkeypatch):
    run = _stub_run(monkeypatch)
    subject._danreq("q", ["a"])
    argv = run.argvs[0]
    assert argv[argv.index("--type") + 1] == "decision"


def test_danreq_honours_an_explicit_request_type(subject, sandbox, monkeypatch):
    run = _stub_run(monkeypatch)
    subject._danreq("q", ["a"], req_type="approval")
    argv = run.argvs[0]
    assert argv[argv.index("--type") + 1] == "approval"


def test_danreq_appends_an_explicit_id(subject, sandbox, monkeypatch):
    run = _stub_run(monkeypatch)
    subject._danreq("q", ["a"], req_id="esc-T1-abcd1234")
    assert run.argvs[0][-2:] == ["--id", "esc-T1-abcd1234"]


def test_danreq_omits_the_id_flag_when_none(subject, sandbox, monkeypatch):
    run = _stub_run(monkeypatch)
    subject._danreq("q", ["a"])
    assert "--id" not in run.argvs[0]


def test_danreq_treats_an_empty_id_as_absent(subject, sandbox, monkeypatch):
    run = _stub_run(monkeypatch)
    subject._danreq("q", ["a"], req_id="")
    assert "--id" not in run.argvs[0]


def test_danreq_joins_options_with_commas(subject, sandbox, monkeypatch):
    run = _stub_run(monkeypatch)
    subject._danreq("q", ["one", "two", "three"])
    argv = run.argvs[0]
    assert argv[argv.index("--options") + 1] == "one,two,three"


def test_danreq_sends_an_empty_options_string_for_no_options(subject, sandbox, monkeypatch):
    run = _stub_run(monkeypatch)
    subject._danreq("q", [])
    argv = run.argvs[0]
    assert argv[argv.index("--options") + 1] == ""


def test_danreq_option_containing_a_comma_becomes_two_options(
    subject, sandbox, monkeypatch
):
    """FOUND_BUGS: the option list is comma-joined with no escaping, so a single option
    containing a comma arrives at the sender as two buttons — and the recorded index
    then no longer matches the option the code believed it offered."""
    run = _stub_run(monkeypatch)
    subject._danreq("q", ["Revert, then retry", "Keep"])
    argv = run.argvs[0]
    assert argv[argv.index("--options") + 1] == "Revert, then retry,Keep"


def test_danreq_runs_in_the_repo_and_captures_output(subject, sandbox, monkeypatch):
    run = _stub_run(monkeypatch)
    subject._danreq("q", ["a"])
    assert run.calls[0].kwargs["cwd"] == str(subject.REPO)
    assert run.calls[0].kwargs["capture_output"] is True


def test_danreq_sets_no_timeout(subject, sandbox, monkeypatch):
    """FOUND_BUGS: unlike notify_telegram (timeout=15) and _tg_api (timeout=8) the
    Dan-request sender has no bound, so a hung send blocks the whole poll loop."""
    run = _stub_run(monkeypatch)
    subject._danreq("q", ["a"])
    assert "timeout" not in run.calls[0].kwargs


def test_danreq_returns_none(subject, sandbox, monkeypatch):
    _stub_run(monkeypatch)
    assert subject._danreq("q", ["a"]) is None


def test_danreq_ignores_a_failing_sender(subject, sandbox, monkeypatch):
    """FOUND_BUGS: the return code is discarded — a request Dan never received still
    looks parked, and the caller journals it as escalated."""
    _stub_run(monkeypatch, results=[_result(returncode=2)])
    assert subject._danreq("q", ["a"]) is None


def test_danreq_does_not_check_that_the_sender_exists(subject, sandbox):
    """FOUND_BUGS: no `.exists()` guard (notify_telegram has one), so a missing venv
    interpreter raises FileNotFoundError straight out of the escalation path."""
    assert not subject.VENV_PYTHON.exists()
    with pytest.raises(FileNotFoundError):
        subject._danreq("q", ["a"])


def test_danreq_passes_a_multiline_question_as_one_argument(subject, sandbox, monkeypatch):
    run = _stub_run(monkeypatch)
    subject._danreq("line one\nline two", ["a"])
    argv = run.argvs[0]
    assert argv[argv.index("--question") + 1] == "line one\nline two"


def test_danreq_does_not_stringify_options(subject, sandbox, monkeypatch):
    """`",".join` requires strings — a numeric option raises TypeError."""
    _stub_run(monkeypatch)
    with pytest.raises(TypeError):
        subject._danreq("q", [1, 2])


# =====================================================================================
# _pending_danreqs
# =====================================================================================


def test_pending_danreqs_empty_when_the_directory_is_absent(subject, sandbox):
    assert not _questions_dir(subject, sandbox).exists()
    assert subject._pending_danreqs() == []


def test_pending_danreqs_empty_for_an_empty_directory(subject, sandbox):
    _questions_dir(subject, sandbox).mkdir(parents=True)
    assert subject._pending_danreqs() == []


def test_pending_danreqs_returns_a_pending_record(subject, sandbox):
    _write_req(subject, sandbox, "r1", question="Ship?")
    pend = subject._pending_danreqs()
    assert [r["id"] for r in pend] == ["r1"]
    assert pend[0]["question"] == "Ship?"


def test_pending_danreqs_excludes_answered_records(subject, sandbox):
    _write_req(subject, sandbox, "r1", status="answered")
    assert subject._pending_danreqs() == []


def test_pending_danreqs_excludes_records_without_a_status(subject, sandbox):
    qdir = _questions_dir(subject, sandbox)
    qdir.mkdir(parents=True)
    (qdir / "r1.json").write_text(json.dumps({"id": "r1"}), encoding="utf-8")
    assert subject._pending_danreqs() == []


def test_pending_danreqs_status_match_is_case_sensitive(subject, sandbox):
    _write_req(subject, sandbox, "r1", status="Pending")
    assert subject._pending_danreqs() == []


def test_pending_danreqs_skips_unparseable_json(subject, sandbox):
    qdir = _questions_dir(subject, sandbox)
    qdir.mkdir(parents=True)
    (qdir / "broken.json").write_text("{not json", encoding="utf-8")
    _write_req(subject, sandbox, "r1")
    assert [r["id"] for r in subject._pending_danreqs()] == ["r1"]


def test_pending_danreqs_skips_non_dict_json(subject, sandbox):
    qdir = _questions_dir(subject, sandbox)
    qdir.mkdir(parents=True)
    (qdir / "list.json").write_text(json.dumps([{"status": "pending"}]), encoding="utf-8")
    assert subject._pending_danreqs() == []


def test_pending_danreqs_ignores_answer_sidecars(subject, sandbox):
    _write_req(subject, sandbox, "r1")
    _answer_file(subject, sandbox, "r1").write_text("yes\n", encoding="utf-8")
    assert len(subject._pending_danreqs()) == 1


def test_pending_danreqs_ignores_non_json_files(subject, sandbox):
    qdir = _questions_dir(subject, sandbox)
    qdir.mkdir(parents=True)
    (qdir / "notes.txt").write_text('{"status": "pending"}', encoding="utf-8")
    assert subject._pending_danreqs() == []


def test_pending_danreqs_does_not_recurse_into_subdirectories(subject, sandbox):
    qdir = _questions_dir(subject, sandbox)
    (qdir / "archive").mkdir(parents=True)
    (qdir / "archive" / "old.json").write_text(
        json.dumps({"id": "old", "status": "pending"}), encoding="utf-8"
    )
    assert subject._pending_danreqs() == []


def test_pending_danreqs_sorts_newest_first(subject, sandbox):
    _write_req(subject, sandbox, "old", ts="2026-01-01T00:00:00Z")
    _write_req(subject, sandbox, "new", ts="2026-03-01T00:00:00Z")
    _write_req(subject, sandbox, "mid", ts="2026-02-01T00:00:00Z")
    assert [r["id"] for r in subject._pending_danreqs()] == ["new", "mid", "old"]


def test_pending_danreqs_sorts_records_without_a_timestamp_last(subject, sandbox):
    qdir = _questions_dir(subject, sandbox)
    qdir.mkdir(parents=True)
    (qdir / "nots.json").write_text(
        json.dumps({"id": "nots", "status": "pending"}), encoding="utf-8"
    )
    _write_req(subject, sandbox, "withts", ts="2026-01-01T00:00:00Z")
    assert [r["id"] for r in subject._pending_danreqs()] == ["withts", "nots"]


def test_pending_danreqs_sorts_timestamps_as_text_not_as_instants(subject, sandbox):
    """FOUND_BUGS: `ts` is compared as a raw string, so a record written with a non-UTC
    UTC-offset spelling sorts by its wall-clock text rather than by the instant it names.

    `offset` here is 2026-01-01T01:00+05:00 = 2025-12-31T20:00Z — an hour *older* than
    `zulu` (2026-01-01T00:00Z) — yet "…T01:00:00+05:00" > "…T00:00:00Z" as text, so the
    reference reports the older record as "newest first". `_route_freetext_answer` then
    hands an untargeted free-text reply to `pend[0]`, i.e. to the wrong request.
    """
    _write_req(subject, sandbox, "zulu", ts="2026-01-01T00:00:00Z")
    _write_req(subject, sandbox, "offset", ts="2026-01-01T01:00:00+05:00")
    assert [r["id"] for r in subject._pending_danreqs()] == ["offset", "zulu"]


def test_pending_danreqs_sort_misorders_unpadded_month_numbers(subject, sandbox):
    """Same root cause, second spelling: an unpadded month ("2026-1-01") sorts above
    every zero-padded month, so a January request outranks a February one."""
    _write_req(subject, sandbox, "feb", ts="2026-02-01T00:00:00Z")
    _write_req(subject, sandbox, "jan", ts="2026-1-01T00:00:00Z")
    assert [r["id"] for r in subject._pending_danreqs()] == ["jan", "feb"]


def test_pending_danreqs_raises_on_mixed_timestamp_types(subject, sandbox):
    """FOUND_BUGS: the sort key is `r.get("ts", "")` with no coercion, so one record
    holding a numeric ts takes the whole pending list down with a TypeError."""
    _write_req(subject, sandbox, "a", ts="2026-01-01T00:00:00Z")
    _write_req(subject, sandbox, "b", ts=1767225600)
    with pytest.raises(TypeError):
        subject._pending_danreqs()


def test_pending_danreqs_returns_the_records_themselves(subject, sandbox):
    """Callers mutate what they get back (`_route_freetext_answer` reads target["id"]);
    the records are fresh parses, so mutating them does not touch disk."""
    _write_req(subject, sandbox, "r1")
    rec = subject._pending_danreqs()[0]
    rec["status"] = "answered"
    assert _read_req(subject, sandbox, "r1")["status"] == "pending"


def test_pending_danreqs_keeps_every_field_of_the_record(subject, sandbox):
    _write_req(subject, sandbox, "r1", options=["a", "b"], message_id=99, extra={"k": 1})
    rec = subject._pending_danreqs()[0]
    assert rec["options"] == ["a", "b"]
    assert rec["message_id"] == 99
    assert rec["extra"] == {"k": 1}


def test_pending_danreqs_returns_all_pending_records(subject, sandbox):
    for i in range(5):
        _write_req(subject, sandbox, f"r{i}", ts=f"2026-01-0{i + 1}T00:00:00Z")
    assert len(subject._pending_danreqs()) == 5


# =====================================================================================
# _record_danreq_answer
# =====================================================================================


def test_record_danreq_answer_writes_the_answer_sidecar(subject, sandbox):
    _write_req(subject, sandbox, "r1")
    subject._record_danreq_answer("r1", "yes")
    assert _answer_file(subject, sandbox, "r1").read_text(encoding="utf-8") == "yes\n"


def test_record_danreq_answer_strips_surrounding_whitespace(subject, sandbox):
    _write_req(subject, sandbox, "r1")
    subject._record_danreq_answer("r1", "  yes \n")
    assert _answer_file(subject, sandbox, "r1").read_text(encoding="utf-8") == "yes\n"


def test_record_danreq_answer_keeps_internal_newlines(subject, sandbox):
    _write_req(subject, sandbox, "r1")
    subject._record_danreq_answer("r1", "line one\nline two")
    assert _answer_file(subject, sandbox, "r1").read_text(
        encoding="utf-8"
    ) == "line one\nline two\n"


def test_record_danreq_answer_treats_none_as_an_empty_answer(subject, sandbox):
    _write_req(subject, sandbox, "r1")
    subject._record_danreq_answer("r1", None)
    assert _answer_file(subject, sandbox, "r1").read_text(encoding="utf-8") == "\n"


def test_record_danreq_answer_records_an_empty_answer_as_answered(subject, sandbox):
    """FOUND_BUGS: an empty reply is a valid answer — the request flips to answered and
    the consumer reads a blank choice."""
    _write_req(subject, sandbox, "r1")
    subject._record_danreq_answer("r1", "   ")
    assert _read_req(subject, sandbox, "r1")["status"] == "answered"
    assert _answer_file(subject, sandbox, "r1").read_text(encoding="utf-8") == "\n"


def test_record_danreq_answer_creates_the_questions_directory(subject, sandbox):
    assert not _questions_dir(subject, sandbox).exists()
    subject._record_danreq_answer("r1", "yes")
    assert _answer_file(subject, sandbox, "r1").exists()


def test_record_danreq_answer_flips_the_record_to_answered(subject, sandbox):
    _write_req(subject, sandbox, "r1")
    subject._record_danreq_answer("r1", "yes")
    assert _read_req(subject, sandbox, "r1")["status"] == "answered"


def test_record_danreq_answer_preserves_the_other_record_fields(subject, sandbox):
    _write_req(subject, sandbox, "r1", question="Ship?", options=["a", "b"], message_id=7)
    subject._record_danreq_answer("r1", "a")
    rec = _read_req(subject, sandbox, "r1")
    assert rec["question"] == "Ship?"
    assert rec["options"] == ["a", "b"]
    assert rec["message_id"] == 7


def test_record_danreq_answer_does_not_store_the_answer_in_the_record(subject, sandbox):
    """The answer lives only in the `.answer` sidecar — that is what both readers use."""
    _write_req(subject, sandbox, "r1")
    subject._record_danreq_answer("r1", "yes")
    assert "answer" not in _read_req(subject, sandbox, "r1")


def test_record_danreq_answer_rewrites_the_record_indented_with_a_trailing_newline(
    subject, sandbox
):
    _write_req(subject, sandbox, "r1")
    subject._record_danreq_answer("r1", "yes")
    raw = (_questions_dir(subject, sandbox) / "r1.json").read_text(encoding="utf-8")
    assert raw.endswith("}\n")
    assert '\n  "id"' in raw


def test_record_danreq_answer_keeps_non_ascii_unescaped(subject, sandbox):
    _write_req(subject, sandbox, "r1", question="שלום")
    subject._record_danreq_answer("r1", "כן")
    raw = (_questions_dir(subject, sandbox) / "r1.json").read_text(encoding="utf-8")
    assert "שלום" in raw
    assert "\\u05" not in raw
    assert _answer_file(subject, sandbox, "r1").read_text(encoding="utf-8") == "כן\n"


def test_record_danreq_answer_writes_the_sidecar_even_without_a_record(subject, sandbox):
    """FOUND_BUGS: an unknown id produces an orphan `.answer` file and a journal entry
    with no request behind it, instead of being refused."""
    subject._record_danreq_answer("ghost", "yes")
    assert _answer_file(subject, sandbox, "ghost").read_text(encoding="utf-8") == "yes\n"
    assert not (_questions_dir(subject, sandbox) / "ghost.json").exists()


def test_record_danreq_answer_leaves_a_corrupt_record_untouched(subject, sandbox):
    qdir = _questions_dir(subject, sandbox)
    qdir.mkdir(parents=True)
    (qdir / "r1.json").write_text("{not json", encoding="utf-8")
    subject._record_danreq_answer("r1", "yes")
    assert (qdir / "r1.json").read_text(encoding="utf-8") == "{not json"
    assert _answer_file(subject, sandbox, "r1").read_text(encoding="utf-8") == "yes\n"


def test_record_danreq_answer_overwrites_an_earlier_answer(subject, sandbox):
    """FOUND_BUGS: no idempotence guard — an already-answered request is silently
    re-answered, and the second answer wins."""
    _write_req(subject, sandbox, "r1")
    subject._record_danreq_answer("r1", "first")
    subject._record_danreq_answer("r1", "second")
    assert _answer_file(subject, sandbox, "r1").read_text(encoding="utf-8") == "second\n"
    assert _event_names(subject, sandbox).count("danreq_answered") == 2


def test_record_danreq_answer_journals_the_event(subject, sandbox):
    _write_req(subject, sandbox, "r1")
    subject._record_danreq_answer("r1", "yes")
    assert _event_names(subject, sandbox) == ["danreq_answered"]
    assert _detail(subject, sandbox, "danreq_answered") == "r1: yes"


def test_record_danreq_answer_truncates_the_journal_detail_at_eighty_chars(
    subject, sandbox
):
    _write_req(subject, sandbox, "r1")
    subject._record_danreq_answer("r1", "x" * 200)
    detail = _detail(subject, sandbox, "danreq_answered")
    assert detail == "r1: " + "x" * 80
    assert _answer_file(subject, sandbox, "r1").read_text(encoding="utf-8") == "x" * 200 + "\n"


def test_record_danreq_answer_journals_without_a_session_id(subject, sandbox):
    _write_req(subject, sandbox, "r1")
    subject._record_danreq_answer("r1", "yes")
    assert _events(subject, sandbox)[0]["session_id"] == ""


def test_record_danreq_answer_returns_none(subject, sandbox):
    _write_req(subject, sandbox, "r1")
    assert subject._record_danreq_answer("r1", "yes") is None


def test_record_danreq_answer_rejects_a_non_string_answer(subject, sandbox):
    """FOUND_BUGS: `(answer_text or "").strip()` assumes str, so a numeric answer
    raises AttributeError rather than being coerced."""
    _write_req(subject, sandbox, "r1")
    with pytest.raises(AttributeError):
        subject._record_danreq_answer("r1", 1)


def test_record_danreq_answer_does_not_sanitise_the_request_id(subject, sandbox):
    """FOUND_BUGS: the id is interpolated straight into a path. An id containing a
    separator writes outside the questions directory — and the id reaches here from a
    Telegram callback payload."""
    qdir = _questions_dir(subject, sandbox)
    qdir.mkdir(parents=True)
    (qdir / "sub").mkdir()
    subject._record_danreq_answer("sub/evil", "yes")
    assert (qdir / "sub" / "evil.answer").read_text(encoding="utf-8") == "yes\n"


def test_record_danreq_answer_raises_when_the_id_path_is_unwritable(subject, sandbox):
    """A traversing id whose parent does not exist fails on the sidecar write, before
    the journal entry — the answer is simply lost."""
    with pytest.raises(FileNotFoundError):
        subject._record_danreq_answer("missing/dir/evil", "yes")
    assert _events(subject, sandbox) == []


# =====================================================================================
# _handle_danreq_callback
# =====================================================================================


def _cq(data, cq_id="cq1", message=None) -> dict:
    cq: dict = {"id": cq_id, "data": data}
    if message is not None:
        cq["message"] = message
    return cq


def test_handle_danreq_callback_ignores_a_callback_without_data(
    subject, sandbox, monkeypatch
):
    api = _spy_tg_api(monkeypatch, subject)
    assert subject._handle_danreq_callback({"id": "cq1"}) is None
    assert api == []


def test_handle_danreq_callback_ignores_a_null_data_field(subject, sandbox, monkeypatch):
    api = _spy_tg_api(monkeypatch, subject)
    subject._handle_danreq_callback({"id": "cq1", "data": None})
    assert api == []


def test_handle_danreq_callback_ignores_foreign_callback_data(
    subject, sandbox, monkeypatch
):
    api = _spy_tg_api(monkeypatch, subject)
    subject._handle_danreq_callback(_cq("approve:T1"))
    assert api == []


def test_handle_danreq_callback_prefix_match_is_exact(subject, sandbox, monkeypatch):
    api = _spy_tg_api(monkeypatch, subject)
    subject._handle_danreq_callback(_cq("Danreq:r1:0"))
    assert api == []


def test_handle_danreq_callback_raises_on_non_string_data(subject, sandbox, monkeypatch):
    """FOUND_BUGS: `data.startswith` sits outside the try, so a numeric `data` field
    raises AttributeError out of the update loop — even though the except clause below
    explicitly lists AttributeError, showing it was expected to be caught."""
    _spy_tg_api(monkeypatch, subject)
    with pytest.raises(AttributeError):
        subject._handle_danreq_callback({"id": "cq1", "data": 5})


def test_handle_danreq_callback_rejects_data_with_too_few_fields(
    subject, sandbox, monkeypatch
):
    api = _spy_tg_api(monkeypatch, subject)
    subject._handle_danreq_callback(_cq("danreq:r1"))
    assert [c[0][0] for c in api] == ["answerCallbackQuery"]
    assert api[0][0][1] == {"callback_query_id": "cq1", "text": "Invalid"}


def test_handle_danreq_callback_rejects_a_non_numeric_index(subject, sandbox, monkeypatch):
    api = _spy_tg_api(monkeypatch, subject)
    subject._handle_danreq_callback(_cq("danreq:r1:abc"))
    assert api[0][0][1]["text"] == "Invalid"


def test_handle_danreq_callback_rejects_a_float_index(subject, sandbox, monkeypatch):
    api = _spy_tg_api(monkeypatch, subject)
    subject._handle_danreq_callback(_cq("danreq:r1:1.5"))
    assert api[0][0][1]["text"] == "Invalid"


def test_handle_danreq_callback_invalid_data_writes_no_answer(subject, sandbox, monkeypatch):
    _spy_tg_api(monkeypatch, subject)
    _write_req(subject, sandbox, "r1", options=["a"])
    subject._handle_danreq_callback(_cq("danreq:r1:abc"))
    assert not _answer_file(subject, sandbox, "r1").exists()
    assert _read_req(subject, sandbox, "r1")["status"] == "pending"


def test_handle_danreq_callback_splits_the_id_at_the_first_colon_only(
    subject, sandbox, monkeypatch
):
    """FOUND_BUGS: `split(":", 2)` gives the *rest* of the string to the index field, so
    an id containing a colon is truncated and the tap is rejected as invalid."""
    api = _spy_tg_api(monkeypatch, subject)
    _write_req(subject, sandbox, "r1:x", options=["a"])
    subject._handle_danreq_callback(_cq("danreq:r1:x:0"))
    assert api[0][0][1]["text"] == "Invalid"


def test_handle_danreq_callback_reports_an_unknown_request(subject, sandbox, monkeypatch):
    api = _spy_tg_api(monkeypatch, subject)
    subject._handle_danreq_callback(_cq("danreq:ghost:0"))
    assert [c[0][0] for c in api] == ["answerCallbackQuery"]
    assert api[0][0][1]["text"] == "Request not found"


def test_handle_danreq_callback_unknown_request_writes_nothing(subject, sandbox, monkeypatch):
    _spy_tg_api(monkeypatch, subject)
    subject._handle_danreq_callback(_cq("danreq:ghost:0"))
    assert not _answer_file(subject, sandbox, "ghost").exists()
    assert _events(subject, sandbox) == []


def test_handle_danreq_callback_records_the_chosen_option(subject, sandbox, monkeypatch):
    _spy_tg_api(monkeypatch, subject)
    _write_req(subject, sandbox, "r1", options=["Revert", "Retry", "Keep"])
    subject._handle_danreq_callback(_cq("danreq:r1:1"))
    assert _answer_file(subject, sandbox, "r1").read_text(encoding="utf-8") == "Retry\n"
    assert _read_req(subject, sandbox, "r1")["status"] == "answered"


def test_handle_danreq_callback_records_the_first_option_at_index_zero(
    subject, sandbox, monkeypatch
):
    _spy_tg_api(monkeypatch, subject)
    _write_req(subject, sandbox, "r1", options=["Revert", "Retry"])
    subject._handle_danreq_callback(_cq("danreq:r1:0"))
    assert _answer_file(subject, sandbox, "r1").read_text(encoding="utf-8") == "Revert\n"


def test_handle_danreq_callback_journals_the_answer(subject, sandbox, monkeypatch):
    _spy_tg_api(monkeypatch, subject)
    _write_req(subject, sandbox, "r1", options=["Revert"])
    subject._handle_danreq_callback(_cq("danreq:r1:0"))
    assert _detail(subject, sandbox, "danreq_answered") == "r1: Revert"


def test_handle_danreq_callback_acknowledges_the_tap(subject, sandbox, monkeypatch):
    api = _spy_tg_api(monkeypatch, subject)
    _write_req(subject, sandbox, "r1", options=["Revert"])
    subject._handle_danreq_callback(_cq("danreq:r1:0"))
    method, params = api[0][0]
    assert method == "answerCallbackQuery"
    assert params["callback_query_id"] == "cq1"
    assert params["text"] == f"{CHECK} Revert"


def test_handle_danreq_callback_truncates_the_toast_at_fifty_chars(
    subject, sandbox, monkeypatch
):
    api = _spy_tg_api(monkeypatch, subject)
    long = "o" * 120
    _write_req(subject, sandbox, "r1", options=[long])
    subject._handle_danreq_callback(_cq("danreq:r1:0"))
    assert api[0][0][1]["text"] == f"{CHECK} " + "o" * 50
    assert _answer_file(subject, sandbox, "r1").read_text(encoding="utf-8") == long + "\n"


def test_handle_danreq_callback_out_of_range_index_records_the_index_itself(
    subject, sandbox, monkeypatch
):
    """FOUND_BUGS: a tap on a stale keyboard whose request has since been rewritten with
    fewer options records the literal index as the answer — "5" is then read downstream
    as Dan's choice."""
    _spy_tg_api(monkeypatch, subject)
    _write_req(subject, sandbox, "r1", options=["a", "b"])
    subject._handle_danreq_callback(_cq("danreq:r1:5"))
    assert _answer_file(subject, sandbox, "r1").read_text(encoding="utf-8") == "5\n"


def test_handle_danreq_callback_negative_index_records_the_index_itself(
    subject, sandbox, monkeypatch
):
    _spy_tg_api(monkeypatch, subject)
    _write_req(subject, sandbox, "r1", options=["a", "b"])
    subject._handle_danreq_callback(_cq("danreq:r1:-1"))
    assert _answer_file(subject, sandbox, "r1").read_text(encoding="utf-8") == "-1\n"


def test_handle_danreq_callback_missing_options_records_the_index_itself(
    subject, sandbox, monkeypatch
):
    _spy_tg_api(monkeypatch, subject)
    _write_req(subject, sandbox, "r1")
    subject._handle_danreq_callback(_cq("danreq:r1:0"))
    assert _answer_file(subject, sandbox, "r1").read_text(encoding="utf-8") == "0\n"


def test_handle_danreq_callback_null_options_records_the_index_itself(
    subject, sandbox, monkeypatch
):
    _spy_tg_api(monkeypatch, subject)
    _write_req(subject, sandbox, "r1", options=None)
    subject._handle_danreq_callback(_cq("danreq:r1:0"))
    assert _answer_file(subject, sandbox, "r1").read_text(encoding="utf-8") == "0\n"


def test_handle_danreq_callback_corrupt_record_still_records_the_index(
    subject, sandbox, monkeypatch
):
    """FOUND_BUGS: the file exists but does not parse, so the option text is unknown —
    the tap is nevertheless accepted and the raw index becomes the answer."""
    _spy_tg_api(monkeypatch, subject)
    qdir = _questions_dir(subject, sandbox)
    qdir.mkdir(parents=True)
    (qdir / "r1.json").write_text("{not json", encoding="utf-8")
    subject._handle_danreq_callback(_cq("danreq:r1:2"))
    assert _answer_file(subject, sandbox, "r1").read_text(encoding="utf-8") == "2\n"


def test_handle_danreq_callback_non_list_options_records_the_index(
    subject, sandbox, monkeypatch
):
    _spy_tg_api(monkeypatch, subject)
    _write_req(subject, sandbox, "r1", options={"a": 1})
    subject._handle_danreq_callback(_cq("danreq:r1:0"))
    assert _answer_file(subject, sandbox, "r1").read_text(encoding="utf-8") == "0\n"


def test_handle_danreq_callback_strips_the_keyboard(subject, sandbox, monkeypatch):
    api = _spy_tg_api(monkeypatch, subject)
    _write_req(subject, sandbox, "r1", options=["a"])
    subject._handle_danreq_callback(
        _cq("danreq:r1:0", message={"message_id": 42, "chat": {"id": 777}})
    )
    assert [c[0][0] for c in api] == ["answerCallbackQuery", "editMessageReplyMarkup"]
    params = api[1][0][1]
    assert params["chat_id"] == 777
    assert params["message_id"] == 42
    assert json.loads(params["reply_markup"]) == {"inline_keyboard": []}


def test_handle_danreq_callback_skips_the_edit_without_a_message(
    subject, sandbox, monkeypatch
):
    api = _spy_tg_api(monkeypatch, subject)
    _write_req(subject, sandbox, "r1", options=["a"])
    subject._handle_danreq_callback(_cq("danreq:r1:0"))
    assert [c[0][0] for c in api] == ["answerCallbackQuery"]


def test_handle_danreq_callback_skips_the_edit_for_a_null_message(
    subject, sandbox, monkeypatch
):
    api = _spy_tg_api(monkeypatch, subject)
    _write_req(subject, sandbox, "r1", options=["a"])
    cq = _cq("danreq:r1:0")
    cq["message"] = None
    subject._handle_danreq_callback(cq)
    assert [c[0][0] for c in api] == ["answerCallbackQuery"]


def test_handle_danreq_callback_skips_the_edit_without_a_message_id(
    subject, sandbox, monkeypatch
):
    api = _spy_tg_api(monkeypatch, subject)
    _write_req(subject, sandbox, "r1", options=["a"])
    subject._handle_danreq_callback(_cq("danreq:r1:0", message={"chat": {"id": 777}}))
    assert [c[0][0] for c in api] == ["answerCallbackQuery"]


def test_handle_danreq_callback_sends_an_empty_chat_id_when_the_chat_is_missing(
    subject, sandbox, monkeypatch
):
    """FOUND_BUGS: `msg.get("chat", {}).get("id", "")` degrades to an empty chat_id, so
    the keyboard-stripping call is issued and silently rejected by Telegram."""
    api = _spy_tg_api(monkeypatch, subject)
    _write_req(subject, sandbox, "r1", options=["a"])
    subject._handle_danreq_callback(_cq("danreq:r1:0", message={"message_id": 42}))
    assert api[1][0][1]["chat_id"] == ""


def test_handle_danreq_callback_tolerates_a_missing_callback_id(
    subject, sandbox, monkeypatch
):
    api = _spy_tg_api(monkeypatch, subject)
    _write_req(subject, sandbox, "r1", options=["a"])
    subject._handle_danreq_callback({"data": "danreq:r1:0"})
    assert api[0][0][1]["callback_query_id"] == ""


def test_handle_danreq_callback_answers_an_already_answered_request(
    subject, sandbox, monkeypatch
):
    """FOUND_BUGS: no pending check — a second tap on the same (or a re-sent) keyboard
    overwrites Dan's earlier decision, and the consumer re-reads the new one."""
    _spy_tg_api(monkeypatch, subject)
    _write_req(subject, sandbox, "r1", status="answered", options=["a", "b"])
    subject._handle_danreq_callback(_cq("danreq:r1:1"))
    assert _answer_file(subject, sandbox, "r1").read_text(encoding="utf-8") == "b\n"


def test_handle_danreq_callback_does_not_check_who_tapped(subject, sandbox, monkeypatch):
    """FOUND_BUGS: the callback's `from` field is never inspected, so any account that
    can reach the bot's inline keyboard can answer a Dan-request."""
    _spy_tg_api(monkeypatch, subject)
    _write_req(subject, sandbox, "r1", options=["a"])
    cq = _cq("danreq:r1:0")
    cq["from"] = {"id": 1, "username": "stranger"}
    subject._handle_danreq_callback(cq)
    assert _answer_file(subject, sandbox, "r1").read_text(encoding="utf-8") == "a\n"


def test_handle_danreq_callback_accepts_a_zero_padded_index(subject, sandbox, monkeypatch):
    _spy_tg_api(monkeypatch, subject)
    _write_req(subject, sandbox, "r1", options=["a", "b"])
    subject._handle_danreq_callback(_cq("danreq:r1:01"))
    assert _answer_file(subject, sandbox, "r1").read_text(encoding="utf-8") == "b\n"


def test_handle_danreq_callback_accepts_a_whitespace_padded_index(
    subject, sandbox, monkeypatch
):
    _spy_tg_api(monkeypatch, subject)
    _write_req(subject, sandbox, "r1", options=["a", "b"])
    subject._handle_danreq_callback(_cq("danreq:r1: 1 "))
    assert _answer_file(subject, sandbox, "r1").read_text(encoding="utf-8") == "b\n"


def test_handle_danreq_callback_bare_prefix_is_rejected(subject, sandbox, monkeypatch):
    api = _spy_tg_api(monkeypatch, subject)
    subject._handle_danreq_callback(_cq("danreq:"))
    assert api[0][0][1]["text"] == "Invalid"


def test_handle_danreq_callback_empty_request_id_is_reported_not_found(
    subject, sandbox, monkeypatch
):
    api = _spy_tg_api(monkeypatch, subject)
    subject._handle_danreq_callback(_cq("danreq::0"))
    assert api[0][0][1]["text"] == "Request not found"


def test_handle_danreq_callback_returns_none(subject, sandbox, monkeypatch):
    _spy_tg_api(monkeypatch, subject)
    _write_req(subject, sandbox, "r1", options=["a"])
    assert subject._handle_danreq_callback(_cq("danreq:r1:0")) is None


def test_handle_danreq_callback_end_to_end_through_the_real_tg_api(
    subject, sandbox, monkeypatch
):
    """The whole tap path with only `subprocess.run` faked: two Bot API calls go out.

    `_tg_api` builds argv as `curl -s --max-time 5 <url>` and *then* appends one
    `--data-urlencode k=v` pair per param, so the endpoint URL is argv[4] — never the
    last element once the call carries any parameters at all.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unit-test-token")
    run = _stub_run(monkeypatch)
    _write_req(subject, sandbox, "r1", options=["Keep"])
    subject._handle_danreq_callback(
        _cq("danreq:r1:0", message={"message_id": 9, "chat": {"id": 3}})
    )
    assert [argv[4].rsplit("/", 1)[-1] for argv in run.argvs] == [
        "answerCallbackQuery", "editMessageReplyMarkup"
    ]
    assert run.argvs[0][5:] == [
        "--data-urlencode", "callback_query_id=cq1",
        "--data-urlencode", f"text={CHECK} Keep",
    ]
    assert run.argvs[1][5:] == [
        "--data-urlencode", "chat_id=3",
        "--data-urlencode", "message_id=9",
        "--data-urlencode", 'reply_markup={"inline_keyboard": []}',
    ]
    assert _answer_file(subject, sandbox, "r1").read_text(encoding="utf-8") == "Keep\n"


# =====================================================================================
# _route_freetext_answer
# =====================================================================================


def test_route_freetext_answer_drops_the_text_when_nothing_is_pending(
    subject, sandbox, monkeypatch
):
    """FOUND_BUGS: any free-text message that arrives with no open request is discarded
    with no acknowledgement at all — Dan gets silence."""
    notify = _spy_notify(monkeypatch, subject)
    assert subject._route_freetext_answer("yes", None) is None
    assert notify == []
    assert _events(subject, sandbox) == []


def test_route_freetext_answer_treats_answered_requests_as_no_pending(
    subject, sandbox, monkeypatch
):
    notify = _spy_notify(monkeypatch, subject)
    _write_req(subject, sandbox, "r1", status="answered")
    subject._route_freetext_answer("yes", None)
    assert notify == []


def test_route_freetext_answer_routes_to_the_only_pending_request(
    subject, sandbox, monkeypatch
):
    _spy_notify(monkeypatch, subject)
    _write_req(subject, sandbox, "r1")
    subject._route_freetext_answer("use sonnet", None)
    assert _answer_file(subject, sandbox, "r1").read_text(encoding="utf-8") == "use sonnet\n"
    assert _read_req(subject, sandbox, "r1")["status"] == "answered"


def test_route_freetext_answer_confirms_by_telegram(subject, sandbox, monkeypatch):
    notify = _spy_notify(monkeypatch, subject)
    _write_req(subject, sandbox, "r1")
    subject._route_freetext_answer("yes", None)
    assert len(notify) == 1
    assert "r1" in notify.first_arg
    assert notify.first_arg.startswith(CHECK)


def test_route_freetext_answer_journals_the_answer(subject, sandbox, monkeypatch):
    _spy_notify(monkeypatch, subject)
    _write_req(subject, sandbox, "r1")
    subject._route_freetext_answer("yes", None)
    assert _detail(subject, sandbox, "danreq_answered") == "r1: yes"


def test_route_freetext_answer_prefers_the_replied_to_request(subject, sandbox, monkeypatch):
    _spy_notify(monkeypatch, subject)
    _write_req(subject, sandbox, "old", ts="2026-01-01T00:00:00Z", message_id=11)
    _write_req(subject, sandbox, "new", ts="2026-03-01T00:00:00Z", message_id=22)
    subject._route_freetext_answer("pick old", {"message_id": 11})
    assert _answer_file(subject, sandbox, "old").read_text(encoding="utf-8") == "pick old\n"
    assert not _answer_file(subject, sandbox, "new").exists()


def test_route_freetext_answer_falls_back_to_the_newest_pending_request(
    subject, sandbox, monkeypatch
):
    """FOUND_BUGS: with several requests open and no reply-threading the free text is
    silently attributed to the newest one — Dan answering the older question in a plain
    message answers the wrong request, with a confirmation that names the wrong id."""
    _spy_notify(monkeypatch, subject)
    _write_req(subject, sandbox, "old", ts="2026-01-01T00:00:00Z")
    _write_req(subject, sandbox, "new", ts="2026-03-01T00:00:00Z")
    subject._route_freetext_answer("revert", None)
    assert _answer_file(subject, sandbox, "new").read_text(encoding="utf-8") == "revert\n"
    assert not _answer_file(subject, sandbox, "old").exists()


def test_route_freetext_answer_falls_back_when_the_reply_target_is_unknown(
    subject, sandbox, monkeypatch
):
    """FOUND_BUGS: replying to an unrelated bot message routes the answer to the newest
    pending request rather than being refused."""
    _spy_notify(monkeypatch, subject)
    _write_req(subject, sandbox, "r1", message_id=11)
    subject._route_freetext_answer("yes", {"message_id": 999})
    assert _answer_file(subject, sandbox, "r1").read_text(encoding="utf-8") == "yes\n"


def test_route_freetext_answer_falls_back_for_a_reply_without_a_message_id(
    subject, sandbox, monkeypatch
):
    _spy_notify(monkeypatch, subject)
    _write_req(subject, sandbox, "r1")
    subject._route_freetext_answer("yes", {})
    assert _answer_file(subject, sandbox, "r1").read_text(encoding="utf-8") == "yes\n"


def test_route_freetext_answer_falls_back_for_a_null_reply_message_id(
    subject, sandbox, monkeypatch
):
    _spy_notify(monkeypatch, subject)
    _write_req(subject, sandbox, "r1")
    subject._route_freetext_answer("yes", {"message_id": None})
    assert _answer_file(subject, sandbox, "r1").read_text(encoding="utf-8") == "yes\n"


def test_route_freetext_answer_message_id_match_is_by_identity_of_type(
    subject, sandbox, monkeypatch
):
    """FOUND_BUGS: the match is `r.get("message_id") == mid` with no coercion, so a
    record that stored its message_id as a string never matches the integer Telegram
    sends and the reply falls through to the newest-pending default."""
    _spy_notify(monkeypatch, subject)
    _write_req(subject, sandbox, "old", ts="2026-01-01T00:00:00Z", message_id="11")
    _write_req(subject, sandbox, "new", ts="2026-03-01T00:00:00Z")
    subject._route_freetext_answer("yes", {"message_id": 11})
    assert _answer_file(subject, sandbox, "new").read_text(encoding="utf-8") == "yes\n"


def test_route_freetext_answer_records_a_zero_message_id_target(
    subject, sandbox, monkeypatch
):
    """`is not None`, not truthiness — message_id 0 is still treated as a reply target."""
    _spy_notify(monkeypatch, subject)
    _write_req(subject, sandbox, "old", ts="2026-01-01T00:00:00Z", message_id=0)
    _write_req(subject, sandbox, "new", ts="2026-03-01T00:00:00Z", message_id=22)
    subject._route_freetext_answer("yes", {"message_id": 0})
    assert _answer_file(subject, sandbox, "old").read_text(encoding="utf-8") == "yes\n"


def test_route_freetext_answer_coerces_a_numeric_request_id(subject, sandbox, monkeypatch):
    _spy_notify(monkeypatch, subject)
    qdir = _questions_dir(subject, sandbox)
    qdir.mkdir(parents=True)
    (qdir / "17.json").write_text(
        json.dumps({"id": 17, "status": "pending", "ts": "2026-01-01T00:00:00Z"}),
        encoding="utf-8",
    )
    subject._route_freetext_answer("yes", None)
    assert (qdir / "17.answer").read_text(encoding="utf-8") == "yes\n"


def test_route_freetext_answer_raises_on_a_record_without_an_id(
    subject, sandbox, monkeypatch
):
    """FOUND_BUGS: `target["id"]` is indexed, not `.get()` — a record written without an
    id (the only required field the loader does not check) crashes the update handler."""
    _spy_notify(monkeypatch, subject)
    qdir = _questions_dir(subject, sandbox)
    qdir.mkdir(parents=True)
    (qdir / "r1.json").write_text(
        json.dumps({"status": "pending", "ts": "2026-01-01T00:00:00Z"}), encoding="utf-8"
    )
    with pytest.raises(KeyError):
        subject._route_freetext_answer("yes", None)


def test_route_freetext_answer_records_an_empty_message_as_an_answer(
    subject, sandbox, monkeypatch
):
    """FOUND_BUGS: no minimum length — a stray blank message closes the open request."""
    notify = _spy_notify(monkeypatch, subject)
    _write_req(subject, sandbox, "r1")
    subject._route_freetext_answer("   ", None)
    assert _read_req(subject, sandbox, "r1")["status"] == "answered"
    assert len(notify) == 1


def test_route_freetext_answer_records_the_answer_before_confirming(
    subject, sandbox, monkeypatch
):
    monkeypatch.setattr(subject, "notify_telegram", _Raiser(RuntimeError("telegram down")))
    _write_req(subject, sandbox, "r1")
    with pytest.raises(RuntimeError):
        subject._route_freetext_answer("yes", None)
    assert _answer_file(subject, sandbox, "r1").read_text(encoding="utf-8") == "yes\n"


def test_route_freetext_answer_answers_only_one_request(subject, sandbox, monkeypatch):
    _spy_notify(monkeypatch, subject)
    _write_req(subject, sandbox, "a", ts="2026-01-01T00:00:00Z")
    _write_req(subject, sandbox, "b", ts="2026-02-01T00:00:00Z")
    _write_req(subject, sandbox, "c", ts="2026-03-01T00:00:00Z")
    subject._route_freetext_answer("yes", None)
    answered = [
        r for r in ("a", "b", "c") if _answer_file(subject, sandbox, r).exists()
    ]
    assert answered == ["c"]


def test_route_freetext_answer_returns_none(subject, sandbox, monkeypatch):
    _spy_notify(monkeypatch, subject)
    _write_req(subject, sandbox, "r1")
    assert subject._route_freetext_answer("yes", None) is None


def test_route_freetext_answer_does_not_authenticate_the_sender(
    subject, sandbox, monkeypatch
):
    """The routing takes only the text and the reply stub; there is nowhere for a sender
    identity to be checked, so whoever the update loop hands it wins."""
    _spy_notify(monkeypatch, subject)
    _write_req(subject, sandbox, "r1")
    subject._route_freetext_answer("from anyone", None)
    assert _answer_file(subject, sandbox, "r1").exists()


# =====================================================================================
# danreq lifecycle, end to end
# =====================================================================================


def test_lifecycle_create_pending_button_answer(subject, sandbox, monkeypatch):
    """Send → appears pending → tapped → recorded, answered, no longer pending."""
    run = _stub_run(monkeypatch)
    subject._danreq("Regression on T1. What to do?", ["Revert", "Retry"], req_id="esc-1")
    assert run.argvs[0][-2:] == ["--id", "esc-1"]

    # the sender (a separate script) writes the record; emulate its output shape
    _write_req(subject, sandbox, "esc-1", options=["Revert", "Retry"], message_id=5)
    assert [r["id"] for r in subject._pending_danreqs()] == ["esc-1"]

    _spy_tg_api(monkeypatch, subject)
    subject._handle_danreq_callback(
        _cq("danreq:esc-1:1", message={"message_id": 5, "chat": {"id": 3}})
    )
    assert _answer_file(subject, sandbox, "esc-1").read_text(encoding="utf-8") == "Retry\n"
    assert subject._pending_danreqs() == []


def test_lifecycle_create_pending_freetext_answer(subject, sandbox, monkeypatch):
    _spy_notify(monkeypatch, subject)
    _write_req(subject, sandbox, "esc-1", options=["Revert"], message_id=5)
    subject._route_freetext_answer("do something else", {"message_id": 5})
    assert _answer_file(subject, sandbox, "esc-1").read_text(
        encoding="utf-8"
    ) == "do something else\n"
    assert subject._pending_danreqs() == []
    assert _event_names(subject, sandbox) == ["danreq_answered"]


def test_lifecycle_a_button_tap_then_free_text_overwrites_the_choice(
    subject, sandbox, monkeypatch
):
    """FOUND_BUGS: nothing closes the loop — after the tap the request is `answered`, so
    the free text falls through to *whatever else* is pending, or is dropped."""
    _spy_tg_api(monkeypatch, subject)
    notify = _spy_notify(monkeypatch, subject)
    _write_req(subject, sandbox, "esc-1", options=["Revert", "Retry"])
    subject._handle_danreq_callback(_cq("danreq:esc-1:0"))
    subject._route_freetext_answer("actually retry", None)
    assert _answer_file(subject, sandbox, "esc-1").read_text(encoding="utf-8") == "Revert\n"
    assert notify == []


def test_lifecycle_two_open_requests_are_answered_in_reply_order(
    subject, sandbox, monkeypatch
):
    _spy_notify(monkeypatch, subject)
    _write_req(subject, sandbox, "a", ts="2026-01-01T00:00:00Z", message_id=1)
    _write_req(subject, sandbox, "b", ts="2026-02-01T00:00:00Z", message_id=2)
    subject._route_freetext_answer("answer to a", {"message_id": 1})
    assert [r["id"] for r in subject._pending_danreqs()] == ["b"]
    subject._route_freetext_answer("answer to b", None)
    assert subject._pending_danreqs() == []
    assert _answer_file(subject, sandbox, "b").read_text(encoding="utf-8") == "answer to b\n"


# =====================================================================================
# _escalation_req_id
# =====================================================================================


def test_escalation_req_id_shape(subject, sandbox):
    rid = subject._escalation_req_id("T1", "smoke failed")
    assert rid.startswith("esc-T1-")
    assert re.fullmatch(r"esc-T1-[0-9a-f]{8}", rid)


def test_escalation_req_id_is_deterministic(subject, sandbox):
    a = subject._escalation_req_id("T1", "smoke failed")
    b = subject._escalation_req_id("T1", "smoke failed")
    assert a == b


def test_escalation_req_id_matches_the_documented_hash(subject, sandbox):
    """Pins the algorithm itself: sha1 of `task|normalized-reason`, first 8 hex chars."""
    norm = "smoke failed"
    expected = hashlib.sha1(f"T1|{norm}".encode("utf-8")).hexdigest()[:8]
    assert subject._escalation_req_id("T1", "smoke failed") == f"esc-T1-{expected}"


def test_escalation_req_id_is_case_insensitive_in_the_reason(subject, sandbox):
    assert subject._escalation_req_id("T1", "Smoke FAILED") == subject._escalation_req_id(
        "T1", "smoke failed"
    )


def test_escalation_req_id_collapses_session_ids(subject, sandbox):
    a = subject._escalation_req_id("T1", "impl-t1-20260619-115951 crashed")
    b = subject._escalation_req_id("T1", "impl-t1-20260620-090000 crashed")
    assert a == b


def test_escalation_req_id_collapses_all_digits(subject, sandbox):
    a = subject._escalation_req_id("T1", "exit code 3")
    b = subject._escalation_req_id("T1", "exit code 9")
    assert a == b


def test_escalation_req_id_collapses_multi_digit_runs_to_one_marker(subject, sandbox):
    """FOUND_BUGS: `\\d+` collapses a whole run, so "exit code 1" and "exit code 12345"
    are the same failure class — but "code 1 1" and "code 11" are too."""
    assert subject._escalation_req_id("T1", "code 11") == subject._escalation_req_id(
        "T1", "code 7"
    )


def test_escalation_req_id_collapses_whitespace(subject, sandbox):
    a = subject._escalation_req_id("T1", "smoke   failed")
    b = subject._escalation_req_id("T1", "smoke\n\tfailed")
    c = subject._escalation_req_id("T1", "smoke failed")
    assert a == b == c


def test_escalation_req_id_strips_the_reason(subject, sandbox):
    assert subject._escalation_req_id("T1", "  smoke failed  ") == (
        subject._escalation_req_id("T1", "smoke failed")
    )


def test_escalation_req_id_differs_per_task(subject, sandbox):
    a = subject._escalation_req_id("T1", "smoke failed")
    b = subject._escalation_req_id("T2", "smoke failed")
    assert a != b


def test_escalation_req_id_differs_per_reason_class(subject, sandbox):
    a = subject._escalation_req_id("T1", "smoke failed")
    b = subject._escalation_req_id("T1", "merge conflict")
    assert a != b


def test_escalation_req_id_handles_an_empty_reason(subject, sandbox):
    assert re.fullmatch(r"esc-T1-[0-9a-f]{8}", subject._escalation_req_id("T1", ""))


def test_escalation_req_id_does_not_normalize_the_task_id(subject, sandbox):
    """FOUND_BUGS: only the *reason* is normalized. The task id is spliced into the id
    verbatim, so a task id containing a space or a slash produces a request id that is
    also used as a filename."""
    assert subject._escalation_req_id("T 1/x", "why").startswith("esc-T 1/x-")


def test_escalation_req_id_task_id_case_is_preserved_but_significant(subject, sandbox):
    a = subject._escalation_req_id("T1", "why")
    b = subject._escalation_req_id("t1", "why")
    assert a != b
    assert a.startswith("esc-T1-")
    assert b.startswith("esc-t1-")


def test_escalation_req_id_session_collapse_runs_before_digit_collapse(subject, sandbox):
    """The `impl-` regex is greedy over [a-z0-9_-], so the whole session id becomes one
    marker rather than a digit-collapsed remnant."""
    expected = hashlib.sha1("T1|impl-* crashed".encode("utf-8")).hexdigest()[:8]
    assert subject._escalation_req_id("T1", "impl-abc-123 crashed") == f"esc-T1-{expected}"


def test_escalation_req_id_impl_marker_needs_a_hyphen(subject, sandbox):
    """"implementer" is not a session id: no hyphen after `impl`, so it survives."""
    expected = hashlib.sha1("T1|implementer died".encode("utf-8")).hexdigest()[:8]
    assert subject._escalation_req_id("T1", "implementer died") == f"esc-T1-{expected}"


def test_escalation_req_id_collapses_digits_inside_words(subject, sandbox):
    """FOUND_BUGS: `\\d+` is not word-bounded, so task ids mentioned in the reason are
    collapsed too — "P8B2 failed" and "P9B3 failed" are one escalation."""
    a = subject._escalation_req_id("T1", "P8B2 failed")
    b = subject._escalation_req_id("T1", "P9B3 failed")
    assert a == b


def test_escalation_req_id_rejects_a_non_string_reason(subject, sandbox):
    with pytest.raises(AttributeError):
        subject._escalation_req_id("T1", None)


def test_escalation_req_id_non_ascii_reason(subject, sandbox):
    rid = subject._escalation_req_id("T1", "נכשל")
    assert re.fullmatch(r"esc-T1-[0-9a-f]{8}", rid)


def test_escalation_req_id_different_wording_of_the_same_failure_differs(subject, sandbox):
    """The normalization only collapses volatile tokens; prose differences still split
    the class, so a message the runner rephrases parks a fresh request."""
    a = subject._escalation_req_id("T1", "timeout after 4h")
    b = subject._escalation_req_id("T1", "timed out after 4h")
    assert a != b


# =====================================================================================
# _send_hitl_reminders
# =====================================================================================


def _waiting(parked_at: str, task_id: str = "T1", **extra) -> dict:
    item = {"task_id": task_id, "parked_at": parked_at}
    item.update(extra)
    return item


def test_send_hitl_reminders_no_waiting_items(subject, sandbox, monkeypatch):
    notify = _spy_notify(monkeypatch, subject)
    _write_state(subject, sandbox, {})
    before = _state_json(subject, sandbox).read_text()
    assert subject._send_hitl_reminders() is None
    assert notify == []
    assert _state_json(subject, sandbox).read_text() == before


def test_send_hitl_reminders_missing_waiting_key(subject, sandbox, monkeypatch):
    notify = _spy_notify(monkeypatch, subject)
    p = _state_json(subject, sandbox)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({}), encoding="utf-8")
    subject._send_hitl_reminders()
    assert notify == []


def test_send_hitl_reminders_skips_a_recently_parked_item(subject, sandbox, monkeypatch):
    notify = _spy_notify(monkeypatch, subject)
    _write_state(subject, sandbox, {"1": _waiting(_ago(hours=3))})
    subject._send_hitl_reminders()
    assert notify == []


def test_send_hitl_reminders_boundary_is_strictly_greater_than_24h(
    subject, sandbox, monkeypatch
):
    notify = _spy_notify(monkeypatch, subject)
    _write_state(subject, sandbox, {"1": _waiting(_ago(seconds=86400 - 5))})
    subject._send_hitl_reminders()
    assert notify == []


def test_send_hitl_reminders_sends_past_24h(subject, sandbox, monkeypatch):
    notify = _spy_notify(monkeypatch, subject)
    _write_state(subject, sandbox, {"7": _waiting(_ago(hours=30), task_id="T9")})
    subject._send_hitl_reminders()
    assert len(notify) == 1
    msg = notify.first_arg
    assert msg.startswith(ALARM)
    assert "T9" in msg
    assert "ID 7" in msg
    assert "24 h" in msg


def test_send_hitl_reminders_stamps_reminded_at(subject, sandbox, monkeypatch):
    _spy_notify(monkeypatch, subject)
    _write_state(subject, sandbox, {"7": _waiting(_ago(hours=30))})
    subject._send_hitl_reminders()
    stamp = _read_state_file(subject, sandbox)["waiting_on_dan"]["7"]["reminded_at"]
    assert datetime.fromisoformat(stamp).tzinfo is not None


def test_send_hitl_reminders_persists_the_state(subject, sandbox, monkeypatch):
    _spy_notify(monkeypatch, subject)
    _write_state(subject, sandbox, {"7": _waiting(_ago(hours=30))})
    subject._send_hitl_reminders()
    assert "updated_at" in _read_state_file(subject, sandbox)


def test_send_hitl_reminders_does_not_write_state_when_nothing_was_sent(
    subject, sandbox, monkeypatch
):
    _spy_notify(monkeypatch, subject)
    _write_state(subject, sandbox, {"7": _waiting(_ago(hours=3))})
    before = _state_json(subject, sandbox).read_text()
    subject._send_hitl_reminders()
    assert _state_json(subject, sandbox).read_text() == before


def test_send_hitl_reminders_second_call_is_throttled(subject, sandbox, monkeypatch):
    notify = _spy_notify(monkeypatch, subject)
    _write_state(subject, sandbox, {"7": _waiting(_ago(hours=30))})
    subject._send_hitl_reminders()
    subject._send_hitl_reminders()
    assert len(notify) == 1


def test_send_hitl_reminders_resends_after_the_cooldown(subject, sandbox, monkeypatch):
    notify = _spy_notify(monkeypatch, subject)
    interval = _reminder_interval(subject)
    _write_state(
        subject, sandbox,
        {"7": _waiting(_ago(hours=72), reminded_at=_ago(seconds=interval + 60))},
    )
    subject._send_hitl_reminders()
    assert len(notify) == 1


def test_send_hitl_reminders_cooldown_boundary_is_strict(subject, sandbox, monkeypatch):
    """`< REMINDER_INTERVAL_SEC` — a stamp exactly one interval old re-sends."""
    notify = _spy_notify(monkeypatch, subject)
    interval = _reminder_interval(subject)
    _write_state(
        subject, sandbox,
        {"7": _waiting(_ago(hours=72), reminded_at=_ago(seconds=interval + 1))},
    )
    subject._send_hitl_reminders()
    assert len(notify) == 1


def test_send_hitl_reminders_within_cooldown_is_skipped(subject, sandbox, monkeypatch):
    notify = _spy_notify(monkeypatch, subject)
    interval = _reminder_interval(subject)
    _write_state(
        subject, sandbox,
        {"7": _waiting(_ago(hours=72), reminded_at=_ago(seconds=interval - 600))},
    )
    subject._send_hitl_reminders()
    assert notify == []


def test_send_hitl_reminders_unparseable_reminded_at_sends_anyway(
    subject, sandbox, monkeypatch
):
    """FOUND_BUGS: a corrupt cooldown stamp falls through the bare `except: pass` into
    the send, so a malformed value restores the 30-second nag loop the throttle exists
    to prevent — until the stamp is rewritten at the end of the same call."""
    notify = _spy_notify(monkeypatch, subject)
    _write_state(subject, sandbox, {"7": _waiting(_ago(hours=72), reminded_at="never")})
    subject._send_hitl_reminders()
    assert len(notify) == 1


def test_send_hitl_reminders_null_reminded_at_sends(subject, sandbox, monkeypatch):
    notify = _spy_notify(monkeypatch, subject)
    _write_state(subject, sandbox, {"7": _waiting(_ago(hours=72), reminded_at=None)})
    subject._send_hitl_reminders()
    assert len(notify) == 1


def test_send_hitl_reminders_empty_reminded_at_sends(subject, sandbox, monkeypatch):
    notify = _spy_notify(monkeypatch, subject)
    _write_state(subject, sandbox, {"7": _waiting(_ago(hours=72), reminded_at="")})
    subject._send_hitl_reminders()
    assert len(notify) == 1


def test_send_hitl_reminders_skips_awaiting_verification(subject, sandbox, monkeypatch):
    notify = _spy_notify(monkeypatch, subject)
    _write_state(
        subject, sandbox,
        {"7": _waiting(_ago(hours=72), kind="awaiting-verification")},
    )
    subject._send_hitl_reminders()
    assert notify == []


def test_send_hitl_reminders_awaiting_verification_is_not_stamped(
    subject, sandbox, monkeypatch
):
    _spy_notify(monkeypatch, subject)
    _write_state(
        subject, sandbox,
        {"7": _waiting(_ago(hours=72), kind="awaiting-verification")},
    )
    subject._send_hitl_reminders()
    assert "reminded_at" not in _read_state_file(subject, sandbox)["waiting_on_dan"]["7"]


def test_send_hitl_reminders_manual_action_verb(subject, sandbox, monkeypatch):
    notify = _spy_notify(monkeypatch, subject)
    _write_state(subject, sandbox, {"7": _waiting(_ago(hours=30), kind="manual-action")})
    subject._send_hitl_reminders()
    msg = notify.first_arg
    assert "/approve 7" in msg
    assert "/reject" not in msg


def test_send_hitl_reminders_default_verb_offers_both_commands(
    subject, sandbox, monkeypatch
):
    notify = _spy_notify(monkeypatch, subject)
    _write_state(subject, sandbox, {"7": _waiting(_ago(hours=30))})
    subject._send_hitl_reminders()
    msg = notify.first_arg
    assert "/approve 7" in msg
    assert "/reject 7" in msg


def test_send_hitl_reminders_unknown_kind_offers_both_commands(
    subject, sandbox, monkeypatch
):
    notify = _spy_notify(monkeypatch, subject)
    _write_state(subject, sandbox, {"7": _waiting(_ago(hours=30), kind="something-new")})
    subject._send_hitl_reminders()
    assert "/reject 7" in notify.first_arg


def test_send_hitl_reminders_skips_an_unparseable_parked_at(subject, sandbox, monkeypatch):
    notify = _spy_notify(monkeypatch, subject)
    _write_state(subject, sandbox, {"7": _waiting("not-a-date")})
    subject._send_hitl_reminders()
    assert notify == []


def test_send_hitl_reminders_skips_an_item_without_a_parked_at(
    subject, sandbox, monkeypatch
):
    notify = _spy_notify(monkeypatch, subject)
    _write_state(subject, sandbox, {"7": {"task_id": "T1"}})
    subject._send_hitl_reminders()
    assert notify == []


def test_send_hitl_reminders_parses_the_zulu_timestamp_the_parker_writes(
    subject, sandbox, monkeypatch
):
    notify = _spy_notify(monkeypatch, subject)
    old = (datetime.now(tz=timezone.utc) - timedelta(hours=30)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    _write_state(subject, sandbox, {"7": _waiting(old)})
    subject._send_hitl_reminders()
    assert len(notify) == 1


def test_send_hitl_reminders_raises_on_a_naive_parked_at(subject, sandbox, monkeypatch):
    """FOUND_BUGS: only `fromisoformat` is inside the try. A *parseable but naive*
    timestamp — what any hand-edit or older writer produces — reaches the subtraction
    and raises TypeError out of the poll loop, killing every later reminder too."""
    _spy_notify(monkeypatch, subject)
    naive = (datetime.now() - timedelta(hours=30)).replace(tzinfo=None).isoformat()
    _write_state(subject, sandbox, {"7": _waiting(naive)})
    with pytest.raises(TypeError):
        subject._send_hitl_reminders()


def test_send_hitl_reminders_raises_on_an_item_without_a_task_id(
    subject, sandbox, monkeypatch
):
    """FOUND_BUGS: `info['task_id']` is indexed inside the message. A waiting entry
    missing it takes the whole reminder sweep down."""
    _spy_notify(monkeypatch, subject)
    _write_state(subject, sandbox, {"7": {"parked_at": _ago(hours=30)}})
    with pytest.raises(KeyError):
        subject._send_hitl_reminders()


def test_send_hitl_reminders_handles_several_items(subject, sandbox, monkeypatch):
    notify = _spy_notify(monkeypatch, subject)
    _write_state(
        subject, sandbox,
        {
            "1": _waiting(_ago(hours=30), task_id="A"),
            "2": _waiting(_ago(hours=48), task_id="B"),
            "3": _waiting(_ago(hours=1), task_id="C"),
        },
    )
    subject._send_hitl_reminders()
    assert len(notify) == 2
    assert {"A", "B"} == {m.split("`")[1] for m in notify.first_args}


def test_send_hitl_reminders_writes_state_once_for_several_items(
    subject, sandbox, monkeypatch
):
    _spy_notify(monkeypatch, subject)
    _write_state(
        subject, sandbox,
        {
            "1": _waiting(_ago(hours=30), task_id="A"),
            "2": _waiting(_ago(hours=48), task_id="B"),
        },
    )
    subject._send_hitl_reminders()
    waiting = _read_state_file(subject, sandbox)["waiting_on_dan"]
    assert "reminded_at" in waiting["1"]
    assert "reminded_at" in waiting["2"]


def test_send_hitl_reminders_a_skipped_item_does_not_block_the_others(
    subject, sandbox, monkeypatch
):
    notify = _spy_notify(monkeypatch, subject)
    _write_state(
        subject, sandbox,
        {
            "1": _waiting("garbage", task_id="A"),
            "2": _waiting(_ago(hours=48), task_id="B"),
        },
    )
    subject._send_hitl_reminders()
    assert len(notify) == 1
    assert "B" in notify.first_arg


def test_send_hitl_reminders_leaves_the_stamp_unset_when_the_notifier_fails(
    subject, sandbox, monkeypatch
):
    """FOUND_BUGS: the send is unguarded and happens *before* the stamp, so a transient
    Telegram failure aborts the sweep with no cooldown recorded — and takes the
    already-sent items' stamps with it, since the state write never happens."""
    monkeypatch.setattr(subject, "notify_telegram", _Raiser(RuntimeError("down")))
    _write_state(subject, sandbox, {"7": _waiting(_ago(hours=30))})
    with pytest.raises(RuntimeError):
        subject._send_hitl_reminders()
    assert "reminded_at" not in _read_state_file(subject, sandbox)["waiting_on_dan"]["7"]


def test_send_hitl_reminders_requires_a_state_file(subject, sandbox, monkeypatch):
    _spy_notify(monkeypatch, subject)
    assert not _state_json(subject, sandbox).exists()
    with pytest.raises(FileNotFoundError):
        subject._send_hitl_reminders()


def test_send_hitl_reminders_returns_none(subject, sandbox, monkeypatch):
    _spy_notify(monkeypatch, subject)
    _write_state(subject, sandbox, {"7": _waiting(_ago(hours=30))})
    assert subject._send_hitl_reminders() is None


def test_send_hitl_reminders_preserves_unrelated_state(subject, sandbox, monkeypatch):
    _spy_notify(monkeypatch, subject)
    _write_state(
        subject, sandbox, {"7": _waiting(_ago(hours=30))}, hitl_mode=True,
        parked_tasks=["X1"],
    )
    subject._send_hitl_reminders()
    doc = _read_state_file(subject, sandbox)
    assert doc["hitl_mode"] is True
    assert doc["parked_tasks"] == ["X1"]


def test_send_hitl_reminders_writes_no_journal_entry(subject, sandbox, monkeypatch):
    """A reminder is a pure notification — nothing is recorded in the journal."""
    _spy_notify(monkeypatch, subject)
    _write_state(subject, sandbox, {"7": _waiting(_ago(hours=30))})
    subject._send_hitl_reminders()
    assert _events(subject, sandbox) == []


def test_send_hitl_reminders_message_quotes_the_task_id(subject, sandbox, monkeypatch):
    notify = _spy_notify(monkeypatch, subject)
    _write_state(subject, sandbox, {"7": _waiting(_ago(hours=30), task_id="T9")})
    subject._send_hitl_reminders()
    assert "`T9`" in notify.first_arg


# =====================================================================================
# phase_report
# =====================================================================================


@pytest.fixture
def report(subject, sandbox, monkeypatch):
    """phase_report with every outbound edge stubbed; `.sent` collects the message."""
    sent = _Calls()
    dep = _Calls()
    monkeypatch.setattr(subject, "notify_telegram_with_map", sent)
    monkeypatch.setattr(subject, "run_dep_map", dep)
    monkeypatch.setattr(subject, "parse_runnable_tasks", lambda: [])
    monkeypatch.setattr(subject, "parse_prep_tasks", lambda: [])
    return SimpleNamespace(sent=sent, dep=dep)


def _msg(report) -> str:
    assert len(report.sent) == 1
    return report.sent.first_arg


def test_phase_report_sends_one_message_with_the_map(subject, sandbox, report):
    subject.phase_report("T1", {"short_desc": "did a thing"}, {"metrics": {}})
    assert len(report.sent) == 1


def test_phase_report_headline_names_the_task_and_description(subject, sandbox, report):
    subject.phase_report("T1", {"short_desc": "did a thing"}, {})
    head = _msg(report).splitlines()[0]
    assert head.startswith(CHECK)
    assert "*T1 shipped*" in head
    assert "did a thing" in head


def test_phase_report_falls_back_to_the_task_id_as_description(subject, sandbox, report):
    subject.phase_report("T1", {}, {})
    assert "*T1 shipped*" in _msg(report)
    assert _msg(report).splitlines()[0].endswith("T1")


def test_phase_report_tolerates_a_null_task_def(subject, sandbox, report):
    subject.phase_report("T1", None, {})
    assert _msg(report).splitlines()[0].endswith("T1")


def test_phase_report_metrics_line(subject, sandbox, report):
    subject.phase_report(
        "T1", {}, {"metrics": {"recall_at_5": 0.91, "latency_p95_ms": 120}}
    )
    line = _msg(report).splitlines()[1]
    assert line.startswith(CHART)
    assert "0.91" in line
    assert "120" in line


def test_phase_report_metrics_line_uses_placeholders_for_missing_keys(
    subject, sandbox, report
):
    subject.phase_report("T1", {}, {"metrics": {"other": 1}})
    line = _msg(report).splitlines()[1]
    assert line.count("?") == 2


def test_phase_report_no_metrics_falls_back_to_the_smoke_reason(subject, sandbox, report):
    subject.phase_report("T1", {}, {"reason": "not a retrieval task"})
    line = _msg(report).splitlines()[1]
    assert line.startswith(CHART)
    assert "not a retrieval task" in line


def test_phase_report_empty_metrics_dict_falls_back_to_the_reason(
    subject, sandbox, report
):
    subject.phase_report("T1", {}, {"metrics": {}, "reason": "skipped"})
    assert "skipped" in _msg(report).splitlines()[1]


def test_phase_report_default_reason_when_absent(subject, sandbox, report):
    subject.phase_report("T1", {}, {})
    assert "smoke" in _msg(report).splitlines()[1]


def test_phase_report_truncates_the_reason_at_ninety_chars(subject, sandbox, report):
    subject.phase_report("T1", {}, {"reason": "r" * 200})
    line = _msg(report).splitlines()[1]
    assert line == f"{CHART} " + "r" * 90


def test_phase_report_raises_on_a_null_reason(subject, sandbox, report):
    """FOUND_BUGS: `smoke.get('reason', <default>)[:90]` — an explicit null reason is not
    the same as an absent one, and slicing None raises out of the completion path after
    the merge has already landed."""
    with pytest.raises(TypeError):
        subject.phase_report("T1", {}, {"reason": None})


def test_phase_report_raises_when_metrics_is_not_a_mapping(subject, sandbox, report):
    with pytest.raises(AttributeError):
        subject.phase_report("T1", {}, {"metrics": ["recall"]})


def test_phase_report_raises_on_a_null_smoke_result(subject, sandbox, report):
    with pytest.raises(AttributeError):
        subject.phase_report("T1", {}, None)


def test_phase_report_no_decisions_leaves_a_blank_line(subject, sandbox, report):
    """FOUND_BUGS: the empty decision block still contributes its own newline, so every
    report without decisions carries a stray blank line before "Next:"."""
    subject.phase_report("T1", {}, {})
    lines = _msg(report).splitlines()
    assert len(lines) == 4
    assert lines[2] == ""


def test_phase_report_decisions_block(subject, sandbox, report):
    subject.phase_report("T1", {"decisions_made": "chose plan B"}, {})
    lines = _msg(report).splitlines()
    assert len(lines) == 6
    assert lines[3].startswith(CLIPBOARD)
    assert "chose plan B" in lines[3]
    assert lines[4].startswith(HOURGLASS)
    assert "24 h" in lines[4]


def test_phase_report_empty_decisions_is_treated_as_absent(subject, sandbox, report):
    subject.phase_report("T1", {"decisions_made": ""}, {})
    assert len(_msg(report).splitlines()) == 4


def test_phase_report_next_line_when_nothing_is_queued(subject, sandbox, report):
    subject.phase_report("T1", {}, {})
    last = _msg(report).splitlines()[-1]
    assert last.startswith(NEXT)
    assert last.endswith("none queued")


def test_phase_report_next_line_lists_runnable_tasks(subject, sandbox, report, monkeypatch):
    monkeypatch.setattr(
        subject, "parse_runnable_tasks", lambda: [{"id": "T2"}, {"id": "T3"}]
    )
    subject.phase_report("T1", {}, {})
    assert _msg(report).splitlines()[-1].endswith("T2, T3")


def test_phase_report_next_line_truncates_at_two_entries(subject, sandbox, report, monkeypatch):
    monkeypatch.setattr(
        subject, "parse_runnable_tasks",
        lambda: [{"id": "T2"}, {"id": "T3"}, {"id": "T4"}],
    )
    subject.phase_report("T1", {}, {})
    assert _msg(report).splitlines()[-1].endswith("T2, T3")


def test_phase_report_next_line_marks_prep_tasks(subject, sandbox, report, monkeypatch):
    monkeypatch.setattr(subject, "parse_prep_tasks", lambda: [{"id": "T9"}])
    subject.phase_report("T1", {}, {})
    assert _msg(report).splitlines()[-1].endswith("T9 (prep)")


def test_phase_report_next_line_puts_runnable_tasks_before_prep(
    subject, sandbox, report, monkeypatch
):
    monkeypatch.setattr(subject, "parse_runnable_tasks", lambda: [{"id": "T2"}])
    monkeypatch.setattr(subject, "parse_prep_tasks", lambda: [{"id": "T9"}])
    subject.phase_report("T1", {}, {})
    assert _msg(report).splitlines()[-1].endswith("T2, T9 (prep)")


def test_phase_report_next_line_dedups_a_task_in_both_queues(
    subject, sandbox, report, monkeypatch
):
    monkeypatch.setattr(subject, "parse_runnable_tasks", lambda: [{"id": "T2"}])
    monkeypatch.setattr(subject, "parse_prep_tasks", lambda: [{"id": "T2"}])
    subject.phase_report("T1", {}, {})
    assert _msg(report).splitlines()[-1].endswith("T2")


def test_phase_report_next_line_dedups_within_a_queue(subject, sandbox, report, monkeypatch):
    monkeypatch.setattr(
        subject, "parse_runnable_tasks", lambda: [{"id": "T2"}, {"id": "T2"}, {"id": "T3"}]
    )
    subject.phase_report("T1", {}, {})
    assert _msg(report).splitlines()[-1].endswith("T2, T3")


def test_phase_report_next_line_does_not_exclude_the_task_just_shipped(
    subject, sandbox, report, monkeypatch
):
    """The queue is reported verbatim; if the parser still lists the shipped task (it is
    marked complete separately) it appears as the next one up."""
    monkeypatch.setattr(subject, "parse_runnable_tasks", lambda: [{"id": "T1"}])
    subject.phase_report("T1", {}, {})
    assert _msg(report).splitlines()[-1].endswith("T1")


def test_phase_report_next_line_is_a_question_mark_when_the_parser_fails(
    subject, sandbox, report, monkeypatch
):
    monkeypatch.setattr(subject, "parse_runnable_tasks", _Raiser(RuntimeError("bad yaml")))
    subject.phase_report("T1", {}, {})
    assert _msg(report).splitlines()[-1].endswith("?")


def test_phase_report_next_line_is_a_question_mark_when_the_prep_parser_fails(
    subject, sandbox, report, monkeypatch
):
    """FOUND_BUGS: the whole block is one try, so a prep-parser failure discards the
    runnable list that was already computed."""
    monkeypatch.setattr(subject, "parse_runnable_tasks", lambda: [{"id": "T2"}])
    monkeypatch.setattr(subject, "parse_prep_tasks", _Raiser(RuntimeError("bad yaml")))
    subject.phase_report("T1", {}, {})
    assert _msg(report).splitlines()[-1].endswith("?")


def test_phase_report_next_line_raises_nothing_on_a_task_without_an_id(
    subject, sandbox, report, monkeypatch
):
    """`t["id"]` would raise, but the surrounding try turns it into "?" as well."""
    monkeypatch.setattr(subject, "parse_runnable_tasks", lambda: [{"name": "T2"}])
    subject.phase_report("T1", {}, {})
    assert _msg(report).splitlines()[-1].endswith("?")


def test_phase_report_regenerates_the_dependency_map(subject, sandbox, report):
    subject.phase_report("T1", {}, {})
    assert len(report.dep) == 1


def test_phase_report_swallows_a_dependency_map_failure(subject, sandbox, report, monkeypatch):
    boom = _Raiser(RuntimeError("render failed"))
    monkeypatch.setattr(subject, "run_dep_map", boom)
    subject.phase_report("T1", {}, {})
    assert boom.calls == 1
    assert len(report.sent) == 1


def test_phase_report_returns_none(subject, sandbox, report):
    assert subject.phase_report("T1", {}, {}) is None


def test_phase_report_lets_a_notification_failure_propagate(
    subject, sandbox, report, monkeypatch
):
    monkeypatch.setattr(subject, "notify_telegram_with_map", _Raiser(RuntimeError("down")))
    with pytest.raises(RuntimeError):
        subject.phase_report("T1", {}, {})


def test_phase_report_writes_no_journal_entry(subject, sandbox, report):
    subject.phase_report("T1", {}, {})
    assert _events(subject, sandbox) == []


def test_phase_report_raises_on_a_non_mapping_task_def(subject, sandbox, report):
    """`(task_def or {}).get` — a truthy non-mapping is not defended against."""
    with pytest.raises(AttributeError):
        subject.phase_report("T1", "some task", {})


def test_phase_report_full_message_shape(subject, sandbox, report, monkeypatch):
    monkeypatch.setattr(subject, "parse_runnable_tasks", lambda: [{"id": "T2"}])
    subject.phase_report(
        "T1",
        {"short_desc": "did a thing", "decisions_made": "chose B"},
        {"metrics": {"recall_at_5": 0.9, "latency_p95_ms": 42}},
    )
    lines = _msg(report).splitlines()
    assert lines[0] == f"{CHECK} *T1 shipped* — did a thing"
    assert lines[1] == f"{CHART} Recall@5: 0.9  |  p95 latency: 42 ms"
    assert lines[2] == ""
    assert lines[3] == f"{CLIPBOARD} *Decisions made:* chose B"
    assert lines[4] == f"{HOURGLASS} Veto window: 24 h"
    assert lines[5] == f"{NEXT} Next: T2"
