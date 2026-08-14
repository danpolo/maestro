"""`/backend` — the manual switch trigger in the Telegram control plane (M2, D2).

New M2 behaviour rather than an extraction, so it lives here rather than under
`tests/characterization/`; the two assertions that pin how the *reference* renders the
same input (the `/help` sheet and a `/progress` header) stay next to their siblings in
`tests/characterization/test_commands.py`.

**Nothing here launches an agent, spends a token of quota or reaches a network.** The
router fetches updates by running `curl` through the module's `subprocess` reference,
which is replaced with a recorder; `maestro.switch.switch_task` — the one thing on this
path that would start a real agent — is replaced by a spy that records its arguments, and
an autouse fixture makes the *real* one explosive so a substitution that slipped would
fail the test rather than cost money. The state document and the journal are rebased into
`tmp_path`.

The two properties worth stating outright, because both are easy to lose in a refactor:

* **Case.** Command text arrives lowercased; the argument is read off `text_raw` because
  task ids are case-sensitive. `test_router_hands_the_task_id_over_with_its_case_intact`
  is the one that fails if that regresses.
* **Silence is a bug.** `poll_control_commands` wraps the whole batch in
  `except Exception: pass`, so a handler that raises does not just lose its own command,
  it drops every later update in the same poll. Every bad input below therefore asserts
  an actual reply, not merely the absence of a crash.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from maestro import config as config_module
from maestro import implementer
from maestro import orchestrator
from maestro import state as state_module
from maestro import switch as switch_module
from maestro.hitl import commands
from maestro.backends.registry import known_backends

CHAT = "4242"
TASK_ID = "P8B2"

# Two registered backends, taken from the registry rather than written down: this suite is
# about the command, not about which drivers happen to exist.
NAMES = known_backends()
CURRENT, TARGET = NAMES[0], NAMES[1]


def _update(text: str, uid: int = 100, chat: str = CHAT) -> dict:
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


def _outcome(**fields):
    """A `SwitchOutcome` with the successful-switch defaults filled in."""
    base = dict(
        task_id=TASK_ID,
        reason=switch_module.REASON_MANUAL,
        from_backend=CURRENT,
        to_backend=TARGET,
        switched=True,
        new_session_id="impl-P8B2-20260812-100000",
    )
    base.update(fields)
    return switch_module.SwitchOutcome(**base)


@pytest.fixture(autouse=True)
def sealed(monkeypatch, tmp_path):
    """No agent, no tmux, no Telegram, no writes outside `tmp_path`."""
    orch = tmp_path / "repo" / ".orchestrator"
    orch.mkdir(parents=True)
    monkeypatch.setattr(state_module, "STATE_JSON", orch / "state.json")
    monkeypatch.setattr(state_module, "JOURNAL", orch / "journal.ndjson")

    def explode(*_args, **_kwargs):
        raise AssertionError("a test reached the real switch path")

    # Both spellings: the module's own function, and the name `commands` bound at import.
    # The `switcher` fixture replaces the second one; a test that reaches a switch without
    # it fails here instead of stopping an agent that is not there and starting one that
    # costs money.
    monkeypatch.setattr(switch_module, "switch_task", explode)
    monkeypatch.setattr(commands, "switch_task", explode)
    yield


@pytest.fixture
def notifier(monkeypatch):
    """Capture the replies the handler sends."""
    sent: list[str] = []
    monkeypatch.setattr(commands, "notify_telegram", lambda msg: sent.append(msg))
    return sent


@pytest.fixture
def switcher(monkeypatch):
    """A spy in place of `switch_task`, recording every call and answering with `result`."""
    box = SimpleNamespace(calls=[], result=_outcome(), error=None)

    def spy(task_id, **kwargs):
        box.calls.append((task_id, kwargs))
        if box.error is not None:
            raise box.error
        return box.result

    monkeypatch.setattr(commands, "switch_task", spy)
    return box


@pytest.fixture
def store(monkeypatch):
    """The state document on disk, plus a reader for the journal beside it."""

    def put(**fields):
        state_module.STATE_JSON.write_text(json.dumps(fields), encoding="utf-8")

    def get() -> dict:
        return json.loads(state_module.STATE_JSON.read_text(encoding="utf-8"))

    def events() -> list[dict]:
        path = state_module.JOURNAL
        if not path.exists():
            return []
        return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    put()
    return SimpleNamespace(put=put, get=get, events=events)


@pytest.fixture
def router(monkeypatch, notifier, store):
    """Deliver a Telegram update through the real `poll_control_commands`."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T0KEN")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", CHAT)
    monkeypatch.setattr(commands, "_getUpdates_offset", 0)

    box = SimpleNamespace(sent=notifier, calls=[], stdout='{"ok": true, "result": []}')

    def run(*args, **kwargs):
        box.calls.append((args, kwargs))
        cmd = args[0] if args else kwargs.get("args")
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "curl":
            return subprocess.CompletedProcess(list(cmd), 0, box.stdout, "")
        raise AssertionError(f"the control plane shelled out to {cmd!r}")

    monkeypatch.setattr(
        commands,
        "subprocess",
        SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired),
    )

    def deliver(text: str):
        box.stdout = json.dumps({"ok": True, "result": [_update(text)]})
        commands.poll_control_commands([])

    box.deliver = deliver
    return box


def _in_flight(**fields) -> dict:
    entry = {
        "session_id": "impl-P8B2-20260812-090000",
        "task_id": TASK_ID,
        "role": "implementer",
        "worktree": "/tmp/maestro-impl-P8B2",
        "window": f"impl-{TASK_ID}",
        "started_at": "2026-08-12T09:00:00Z",
        "status": "running",
    }
    entry.update(fields)
    return entry


# =======================================================================================
# routing
# =======================================================================================


def test_the_router_dispatches_bare_backend_to_the_handler(router, store, switcher):
    router.deliver("/backend")
    assert len(router.sent) == 1
    assert router.sent[0].startswith("Usage: /backend <name> [task_id]")
    assert switcher.calls == []


def test_the_router_advances_the_offset_past_a_backend_command(router, store):
    router.deliver("/backend")
    assert commands._getUpdates_offset == 101


def test_a_command_after_a_bad_backend_command_in_the_same_batch_still_runs(
    router, store, monkeypatch, notifier
):
    """The router's blanket `except Exception: pass` swallows the *rest of the batch*, so
    a handler that raises on bad input costs more than its own reply."""
    monkeypatch.setattr(commands, "_getUpdates_offset", 0)
    router.stdout = json.dumps({
        "ok": True,
        "result": [_update("/backend nosuchbackend ZZ9", uid=100), _update("/pause", uid=101)],
    })
    commands.poll_control_commands([])
    assert store.get()["paused_by_user"] is True
    assert len(notifier) == 2


def test_router_hands_the_task_id_over_with_its_case_intact(router, store, switcher):
    """`text` is lowercased before dispatch; task ids are case-sensitive, so the argument
    has to come off `text_raw`. An id that matches nothing is the cheapest proof: the
    reply quotes what was typed."""
    store.put(in_flight=[])
    router.deliver(f"/backend {TARGET} Zz9")
    assert router.sent == [f"ℹ No task `Zz9` is in flight. In flight: none."]
    assert switcher.calls == []


# =======================================================================================
# /backend <name> — the choice for future launches
# =======================================================================================


def test_a_bare_name_records_the_choice_journals_it_and_confirms(store, notifier, switcher):
    commands._process_backend(TARGET)
    assert store.get()[commands.BACKEND_KEY] == TARGET
    assert [(e["event"], e["detail"]) for e in store.events()] == [
        (commands.BACKEND_EVENT, f"{TARGET} selected via Telegram /backend")
    ]
    assert len(notifier) == 1
    assert notifier[0].startswith(f"✅ Backend set to {TARGET}")
    assert switcher.calls == []


def test_a_bare_name_is_normalised_before_it_is_recorded(store, notifier):
    commands._process_backend(f"  {TARGET.upper()}  ")
    assert store.get()[commands.BACKEND_KEY] == TARGET


def test_a_bare_name_leaves_every_other_state_key_alone(store, notifier):
    store.put(phase={"id": "P8"}, in_flight=[_in_flight()], paused_by_user=True)
    commands._process_backend(TARGET)
    after = store.get()
    assert after["phase"] == {"id": "P8"}
    assert after["paused_by_user"] is True
    assert [e["task_id"] for e in after["in_flight"]] == [TASK_ID]


@pytest.fixture
def configured(monkeypatch):
    """`roles:` says the implementer runs on `CURRENT`, whatever the real repo says.

    The launch path resolves through `maestro.roles`, which reads `project.yaml` at call
    time; pinning it here is what makes "the recorded choice won" mean something rather
    than "the default happened to match".
    """
    monkeypatch.setattr(
        config_module,
        "load_project_yaml",
        lambda: {"roles": {"implementer": {"backend": CURRENT}}},
    )


def test_the_recorded_choice_is_what_future_launches_resolve_to(store, notifier, configured):
    """The command is worth no more than its reader. Both readers are asserted: the one
    that resolves the agent `launch_implementer` starts, and the one that stamps the
    `backend` on the `in_flight` entry — they have to move together, or the entry names a
    backend the task is not running on."""
    assert implementer._implementer_backend()[0] == CURRENT
    assert orchestrator._launch_backend() == CURRENT

    commands._process_backend(TARGET)

    assert implementer._implementer_backend()[0] == TARGET
    assert orchestrator._launch_backend() == TARGET


def test_a_choice_made_over_telegram_reaches_the_launch_path(router, store, configured):
    """End to end through the real router: an operator types it, a launch honours it."""
    router.deliver(f"/backend {TARGET}")
    assert router.sent[0].startswith(f"✅ Backend set to {TARGET}")
    assert implementer._implementer_backend()[0] == TARGET
    assert orchestrator._launch_backend() == TARGET


def test_an_unknown_name_never_reaches_the_launch_path(store, notifier, configured):
    """Declined at the door, so nothing downstream has to defend itself — and nothing
    downstream is left resolving a driver that does not exist."""
    commands._process_backend("gpt9000")
    assert implementer._implementer_backend()[0] == CURRENT
    assert orchestrator._launch_backend() == CURRENT


def test_an_unreadable_state_document_is_reported_not_raised(store, notifier):
    state_module.STATE_JSON.write_text("{not json", encoding="utf-8")
    commands._process_backend(TARGET)
    assert len(notifier) == 1
    assert notifier[0].startswith("⚠ Could not record the backend choice:")


# =======================================================================================
# bad input — every one of these answers, and none of them raises
# =======================================================================================


@pytest.mark.parametrize("arg", ["", "   "])
def test_a_missing_argument_answers_with_the_usage_line_and_the_known_backends(
    store, notifier, switcher, arg
):
    commands._process_backend(arg)
    (msg,) = notifier
    assert msg.startswith("Usage: /backend <name> [task_id]")
    for name in known_backends():
        assert name in msg
    assert switcher.calls == []


def test_an_unknown_backend_name_is_declined_with_the_list_of_real_ones(
    store, notifier, switcher
):
    commands._process_backend("gpt9000")
    (msg,) = notifier
    assert msg.startswith("ℹ Unknown backend `gpt9000`.")
    for name in known_backends():
        assert name in msg
    assert switcher.calls == []
    assert commands.BACKEND_KEY not in store.get()


def test_an_unknown_backend_name_is_declined_before_any_task_is_touched(
    store, notifier, switcher
):
    store.put(in_flight=[_in_flight(backend=CURRENT)])
    commands._process_backend(f"gpt9000 {TASK_ID}")
    assert switcher.calls == []
    assert len(notifier) == 1


def test_an_unknown_task_id_lists_what_is_actually_in_flight(store, notifier, switcher):
    store.put(in_flight=[_in_flight(), _in_flight(task_id="P9A", session_id="s2")])
    commands._process_backend(f"{TARGET} P1Z")
    assert notifier == [f"ℹ No task `P1Z` is in flight. In flight: {TASK_ID}, P9A."]
    assert switcher.calls == []


def test_a_task_already_on_the_named_backend_is_left_alone(store, notifier, switcher):
    store.put(in_flight=[_in_flight(backend=TARGET)])
    commands._process_backend(f"{TARGET} {TASK_ID}")
    assert notifier == [f"ℹ {TASK_ID} is already running on {TARGET}. Nothing to do."]
    assert switcher.calls == []


def test_a_switch_that_raises_is_reported_rather_than_propagated(store, notifier, switcher):
    store.put(in_flight=[_in_flight(backend=CURRENT)])
    switcher.error = RuntimeError("tmux is not running")
    commands._process_backend(f"{TARGET} {TASK_ID}")
    (msg,) = notifier
    assert msg == f"⚠ Could not move {TASK_ID} to {TARGET}: tmux is not running"


def test_a_switch_that_could_not_happen_reports_the_reason_it_gave(store, notifier, switcher):
    store.put(in_flight=[_in_flight(backend=CURRENT)])
    switcher.result = _outcome(
        switched=False, to_backend=None, new_session_id="",
        note="no target backend available; leaving the task where it is",
    )
    commands._process_backend(f"{TARGET} {TASK_ID}")
    (msg,) = notifier
    assert msg == (
        f"⚠ {TASK_ID} stayed on {CURRENT}: no target backend available; "
        f"leaving the task where it is."
    )


def test_a_malformed_in_flight_list_does_not_break_the_lookup(store, notifier, switcher):
    store.put(in_flight=["junk", None, _in_flight(backend=CURRENT)])
    commands._process_backend(f"{TARGET} {TASK_ID}")
    (task_id, _kwargs), = switcher.calls
    assert task_id == TASK_ID


# =======================================================================================
# /backend <name> <task-id> — the switch itself
# =======================================================================================


def test_a_named_task_is_switched_through_the_one_switch_path(store, notifier, switcher):
    entry = _in_flight(backend=CURRENT)
    store.put(in_flight=[entry])
    commands._process_backend(f"{TARGET} {TASK_ID}")
    (task_id, kwargs), = switcher.calls
    assert task_id == TASK_ID
    assert kwargs["to_backend"] == TARGET
    assert kwargs["reason"] == switch_module.REASON_MANUAL
    assert kwargs["entry"] == entry


def test_a_successful_switch_confirms_the_new_backend_and_session(store, notifier, switcher):
    store.put(in_flight=[_in_flight(backend=CURRENT)])
    commands._process_backend(f"{TARGET} {TASK_ID}")
    (msg,) = notifier
    assert msg == (
        f"✅ {TASK_ID} is now on {TARGET} — same worktree, uncommitted work kept. "
        f"New session impl-P8B2-20260812-100000."
    )


def test_the_manual_switch_never_recreates_the_worktree(store, notifier, switcher):
    """D3: the uncommitted work lives in the worktree, so the command hands `switch_task`
    the entry and nothing else — it does not go near the worktree lifecycle itself."""
    store.put(in_flight=[_in_flight(backend=CURRENT)])
    commands._process_backend(f"{TARGET} {TASK_ID}")
    (_task_id, kwargs), = switcher.calls
    assert kwargs["entry"]["worktree"] == "/tmp/maestro-impl-P8B2"
    assert not Path("/tmp/maestro-impl-P8B2").exists()


def test_an_entry_with_no_backend_key_is_still_switchable(store, notifier, switcher):
    """Every task launched before M2 has no `backend` key; `switch_task` resolves the
    outgoing backend from configuration, so the command must not refuse the handover."""
    store.put(in_flight=[_in_flight()])
    commands._process_backend(f"{TARGET} {TASK_ID}")
    assert len(switcher.calls) == 1


def test_a_task_id_matches_exactly_before_it_matches_case_insensitively(
    store, notifier, switcher
):
    store.put(in_flight=[
        _in_flight(task_id="p8b2", session_id="s-lower", backend=CURRENT),
        _in_flight(task_id=TASK_ID, session_id="s-upper", backend=CURRENT),
    ])
    commands._process_backend(f"{TARGET} {TASK_ID}")
    (task_id, kwargs), = switcher.calls
    assert task_id == TASK_ID
    assert kwargs["entry"]["session_id"] == "s-upper"


def test_a_task_id_typed_in_the_wrong_case_still_finds_its_task(store, notifier, switcher):
    store.put(in_flight=[_in_flight(backend=CURRENT)])
    commands._process_backend(f"{TARGET} {TASK_ID.lower()}")
    (task_id, _kwargs), = switcher.calls
    assert task_id == TASK_ID


def test_trailing_words_after_the_task_id_are_ignored(store, notifier, switcher):
    store.put(in_flight=[_in_flight(backend=CURRENT)])
    commands._process_backend(f"{TARGET} {TASK_ID} please and thank you")
    (task_id, _kwargs), = switcher.calls
    assert task_id == TASK_ID


def test_switching_one_task_does_not_change_the_launch_default(store, notifier, switcher):
    """`/backend <name> <task_id>` moves one task; the backend future launches use is a
    separate decision the operator makes with the one-argument form."""
    store.put(in_flight=[_in_flight(backend=CURRENT)])
    commands._process_backend(f"{TARGET} {TASK_ID}")
    assert commands.BACKEND_KEY not in store.get()
