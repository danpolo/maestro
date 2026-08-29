"""Pinned behaviour of the parking layer: every park / finalise / graduate transition.

Characterisation, not specification. These functions are the orchestrator's whole
"stop and hand this to a human" surface, so what is pinned here is (a) the exact
``state.json`` document each transition leaves behind, (b) the journal events it
appends, and (c) the payload handed to the outbound edges — ``notify_telegram``,
``_danreq`` and ``subprocess.run``.

Nothing here reaches the network and nothing shells out. Every test that can reach a
subprocess installs the ``runner`` fixture, which swaps the subject's ``subprocess``
reference for a recorder that runs nothing and answers rc=0; assertions are made on
the argv that *would* have run.

Three conventions borrowed from the sibling characterisation modules:

* path globals are read off the subject through ``_path`` with a sandbox-relative
  fallback, so the same body works against the reference (one flat namespace) and the
  extracted package (where ``STATE_JSON`` and friends live in a sibling module);
* ``prep_actions`` is a *separate* module in the reference, so the ``sandbox`` fixture
  cannot see its ``PREPARED_ACTIONS_FILE`` global — every test that can reach the
  sidecar installs the ``prep`` fixture, which swaps in an in-memory store;
* tunables (``AWAIT_VERIFY_TIMEOUT_SEC``, ``MAX_RESUME_ATTEMPTS``,
  ``RESUME_COOLDOWN_H_DEFAULT``, ``SELF_FIX_SKEPTIC_MIN_CONF``) are read off the
  subject rather than hardcoded, so a retune moves the test with the code.

``park_failed`` is the reason this file exists (cyclomatic complexity 37); its section
walks every branch it distinguishes, including the two early returns that make it
idempotent, the usage-limit bail-out, and the four terminal shapes of the diagnosis
pass.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.maestro_module("parking")


# The reference's operator-facing strings are emoji-prefixed. Spelled out by name so the
# test source stays legible while still pinning the exact bytes Dan sees.
CHECK = "\N{WHITE HEAVY CHECK MARK}"           # approvals / graduation
CROSS = "\N{CROSS MARK}"                       # the /reject affordance
WARN = "\N{WARNING SIGN}"                      # merge / gate / park failures
PAUSE = "\N{DOUBLE VERTICAL BAR}"              # HITL park
TARGET = "\N{DIRECT HIT}"                      # the single prepared Dan action
QUESTION = "\N{BLACK QUESTION MARK ORNAMENT}"  # the /ask affordance
WRENCH = "\N{WRENCH}"                          # the /redo affordance
MAGNIFY = "\N{RIGHT-POINTING MAGNIFYING GLASS}"  # failure diagnosis
INFO = "\N{INFORMATION SOURCE}"                # ORCH_DIAGNOSE-off fallback
ALARM = "\N{ALARM CLOCK}"                      # awaiting-verification timeout
GEAR = "\N{GEAR}"                              # the post-action finalize command
HOURGLASS = "\N{HOURGLASS WITH FLOWING SAND}"  # resumable task incomplete
ARROW = "\N{RIGHTWARDS ARROW}"                 # self-fix offers


# --- locating the subject's paths ------------------------------------------------------


def _path(subject, name: str, fallback: Path) -> Path:
    """A path global off the subject, or `fallback` when the subject does not own it."""
    value = getattr(subject, name, None)
    return value if isinstance(value, Path) else fallback


def _state_json(subject, sandbox) -> Path:
    return _path(subject, "STATE_JSON", sandbox.orch_dir / "state.json")


def _journal(subject, sandbox) -> Path:
    return _path(subject, "JOURNAL", sandbox.orch_dir / "journal.ndjson")


def _workspaces(subject, sandbox) -> Path:
    return _path(subject, "WORKSPACES", sandbox.workspaces)


def _questions_dir(subject, sandbox) -> Path:
    return _path(subject, "QUESTIONS_DIR", sandbox.orch_dir / "questions")


def _diagnoses_dir(subject, sandbox) -> Path:
    return _path(subject, "DIAGNOSES_DIR", sandbox.orch_dir / "diagnoses")


# --- fixture state on disk -------------------------------------------------------------


def _put_state(subject, sandbox, **fields) -> Path:
    path = _state_json(subject, sandbox)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


def _get_state(subject, sandbox) -> dict:
    return json.loads(_state_json(subject, sandbox).read_text(encoding="utf-8"))


def _waiting(subject, sandbox) -> dict:
    return _get_state(subject, sandbox).get("waiting_on_dan", {})


def _events(subject, sandbox) -> list[dict]:
    path = _journal(subject, sandbox)
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _event_names(subject, sandbox) -> list[str]:
    return [e["event"] for e in _events(subject, sandbox)]


def _detail(subject, sandbox, event: str) -> str:
    for e in _events(subject, sandbox):
        if e["event"] == event:
            return e["detail"]
    raise AssertionError(f"no {event!r} journal record in {_event_names(subject, sandbox)}")


def _record(subject, sandbox, event: str) -> dict:
    for e in _events(subject, sandbox):
        if e["event"] == event:
            return e
    raise AssertionError(f"no {event!r} journal record in {_event_names(subject, sandbox)}")


def _put_journal(subject, sandbox, *lines: str) -> Path:
    path = _journal(subject, sandbox)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(ln + "\n" for ln in lines), encoding="utf-8")
    return path


def _put_answer(subject, sandbox, req_id: str, choice) -> Path:
    qdir = _questions_dir(subject, sandbox)
    qdir.mkdir(parents=True, exist_ok=True)
    path = qdir / f"{req_id}.answer"
    body = choice if isinstance(choice, str) else json.dumps(choice)
    path.write_text(body, encoding="utf-8")
    return path


def _put_question(subject, sandbox, req_id: str) -> Path:
    qdir = _questions_dir(subject, sandbox)
    qdir.mkdir(parents=True, exist_ok=True)
    path = qdir / f"{req_id}.json"
    path.write_text(json.dumps({"id": req_id}), encoding="utf-8")
    return path


def _make_workspace(subject, sandbox, session_id: str, uuid: str | None = None) -> Path:
    ws = _workspaces(subject, sandbox) / session_id
    ws.mkdir(parents=True, exist_ok=True)
    if uuid is not None:
        (ws / "session_uuid.txt").write_text(uuid, encoding="utf-8")
    return ws


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- fakes ------------------------------------------------------------------------------


class _Runner:
    """Stand-in for the `subprocess` module: records every call, runs nothing."""

    TimeoutExpired = subprocess.TimeoutExpired
    CalledProcessError = subprocess.CalledProcessError
    PIPE = subprocess.PIPE
    DEVNULL = subprocess.DEVNULL

    def __init__(self):
        self.calls: list[SimpleNamespace] = []
        self.result_for = lambda call: (0, "", "")

    def run(self, cmd=None, *args, **kwargs):
        call = SimpleNamespace(
            cmd=cmd,
            argv=list(cmd) if isinstance(cmd, (list, tuple)) else [],
            kwargs=dict(kwargs),
        )
        self.calls.append(call)
        rc, out, err = self.result_for(call)
        return subprocess.CompletedProcess(cmd, rc, out, err)

    @property
    def argvs(self) -> list[list[str]]:
        return [c.argv for c in self.calls if c.argv]

    @property
    def shell_cmds(self) -> list[str]:
        return [c.cmd for c in self.calls if isinstance(c.cmd, str)]

    def find(self, *needles: str) -> list[list[str]]:
        return [a for a in self.argvs if all(n in a for n in needles)]


@pytest.fixture
def runner(subject, monkeypatch) -> _Runner:
    r = _Runner()
    monkeypatch.setattr(subject, "subprocess", r)
    return r


@pytest.fixture
def notifier(subject, monkeypatch) -> list[str]:
    sent: list[str] = []
    monkeypatch.setattr(subject, "notify_telegram", lambda msg: sent.append(msg))
    return sent


@pytest.fixture
def danreq(subject, monkeypatch) -> list[SimpleNamespace]:
    """Records `_danreq` instead of shelling out to the request sender."""
    calls: list[SimpleNamespace] = []

    def fake(question, options, req_type="decision", req_id=None):
        calls.append(SimpleNamespace(question=question, options=list(options),
                                     req_type=req_type, req_id=req_id))

    monkeypatch.setattr(subject, "_danreq", fake)
    return calls


@pytest.fixture
def prep(subject, monkeypatch):
    """An in-memory stand-in for the `prepared_actions` sidecar module.

    The reference imports it as a sibling module whose `PREPARED_ACTIONS_FILE` global the
    `sandbox` fixture cannot see, so an unstubbed call would read — and write — a live
    repo's sidecar.
    """
    store: dict[str, dict] = {}
    calls: list[SimpleNamespace] = []

    def set_action(task_id, action, post_action_cmd=None, dan_id=None, path=None):
        calls.append(SimpleNamespace(op="set", task_id=task_id, action=action,
                                     post_action_cmd=post_action_cmd, dan_id=dan_id))
        entry = {"action": action, "post_action_cmd": post_action_cmd, "dan_id": dan_id}
        store[task_id] = entry
        return entry

    def remove_action(task_id, path=None):
        calls.append(SimpleNamespace(op="remove", task_id=task_id))
        return store.pop(task_id, None)

    namespace = SimpleNamespace(
        store=store,
        calls=calls,
        get_action=lambda task_id, path=None: store.get(task_id),
        has_action=lambda task_id, path=None: task_id in store,
        load_actions=lambda path=None: dict(store),
        set_action=set_action,
        remove_action=remove_action,
    )
    monkeypatch.setattr(subject, "prep_actions", namespace)
    return namespace


# =======================================================================================
# _retry_phrase — how the escalation words the retry budget that was burned
# =======================================================================================


def test_retry_phrase_no_recorded_retries_reads_as_zero(subject, sandbox):
    _put_state(subject, sandbox)
    assert subject._retry_phrase("T1", "boom") == "after 0 retries"


def test_retry_phrase_one_retry_is_singular(subject, sandbox):
    _put_state(subject, sandbox, retry_counts={"T1": 1})
    assert subject._retry_phrase("T1", "boom") == "after 1 retry"


def test_retry_phrase_two_retries_is_plural(subject, sandbox):
    _put_state(subject, sandbox, retry_counts={"T1": 2})
    assert subject._retry_phrase("T1", "boom") == "after 2 retries"


def test_retry_phrase_counts_only_this_task(subject, sandbox):
    _put_state(subject, sandbox, retry_counts={"T2": 5})
    assert subject._retry_phrase("T1", "boom") == "after 0 retries"


def test_retry_phrase_tags_a_timeout_death(subject, sandbox):
    _put_state(subject, sandbox)
    assert subject._retry_phrase("T1", "timeout after 10800s") == "after 0 retries (timeout)"


def test_retry_phrase_timeout_tag_composes_with_the_count(subject, sandbox):
    _put_state(subject, sandbox, retry_counts={"T1": 1})
    assert subject._retry_phrase("T1", "timeout after 10800s") == "after 1 retry (timeout)"


def test_retry_phrase_only_tags_a_timeout_at_the_start_of_the_reason(subject, sandbox):
    """The marker is a prefix test, so the same words mid-sentence are not tagged."""
    _put_state(subject, sandbox)
    assert subject._retry_phrase("T1", "gate failed: timeout after 5s") == "after 0 retries"


def test_retry_phrase_needs_state_json_to_exist(subject, sandbox):
    with pytest.raises(Exception):
        subject._retry_phrase("T1", "boom")


# =======================================================================================
# _escalation_req_id — the idempotence key park_failed is built on
# =======================================================================================


def test_escalation_req_id_is_stable_for_the_same_task_and_reason(subject):
    assert subject._escalation_req_id("T1", "boom") == subject._escalation_req_id("T1", "boom")


def test_escalation_req_id_is_prefixed_with_the_task(subject):
    req = subject._escalation_req_id("T1", "boom")
    assert req.startswith("esc-T1-")
    assert len(req.rsplit("-", 1)[1]) == 8


def test_escalation_req_id_ignores_the_case_of_the_reason(subject):
    assert subject._escalation_req_id("T1", "BOOM") == subject._escalation_req_id("T1", "boom")


def test_escalation_req_id_collapses_session_ids(subject):
    a = subject._escalation_req_id("T1", "window gone for impl-t1-20260619-115951")
    b = subject._escalation_req_id("T1", "window gone for impl-t1-20260620-093012")
    assert a == b


def test_escalation_req_id_collapses_whitespace_runs(subject):
    a = subject._escalation_req_id("T1", "gate   failed")
    b = subject._escalation_req_id("T1", " gate failed ")
    assert a == b


def test_escalation_req_id_collapses_every_digit(subject):
    """FOUND_BUGS: two genuinely different failures that differ only in numbers collide."""
    a = subject._escalation_req_id("T1", "gate failed 3/10")
    b = subject._escalation_req_id("T1", "gate failed 9/10")
    assert a == b


def test_escalation_req_id_separates_different_tasks(subject):
    assert subject._escalation_req_id("T1", "boom") != subject._escalation_req_id("T2", "boom")


def test_escalation_req_id_separates_different_failure_classes(subject):
    assert (subject._escalation_req_id("T1", "gate failed")
            != subject._escalation_req_id("T1", "worktree creation failed"))


# =======================================================================================
# park_failed — the highest-complexity function in this module
# =======================================================================================


@pytest.fixture
def failing(subject, sandbox, monkeypatch, notifier, danreq, runner):
    """`park_failed` with every outbound edge stubbed and the diagnosis pass on.

    The skeptic is off by default (the tests that want it turn it on) so the common
    case is a straight eligible/not-eligible split.
    """
    _put_state(subject, sandbox)
    box = SimpleNamespace(
        sent=notifier,
        danreqs=danreq,
        runner=runner,
        diagnosis={},
        diagnose_raises=None,
        diagnose_calls=[],
        eligible=(False, "class 'x' not in self-fix whitelist"),
        skeptic={},
        fixes=[],
    )

    def diagnose(*args):
        box.diagnose_calls.append(args)
        if box.diagnose_raises is not None:
            raise box.diagnose_raises
        return box.diagnosis

    monkeypatch.setattr(subject, "ORCH_DIAGNOSE", True)
    monkeypatch.setattr(subject, "ORCH_SELF_FIX_SKEPTIC", False)
    monkeypatch.setattr(subject, "_diagnose_failure", diagnose)
    monkeypatch.setattr(subject, "_self_fix_eligible", lambda diag: box.eligible)
    monkeypatch.setattr(subject, "_failure_looks_normal", lambda *a: box.skeptic)
    monkeypatch.setattr(
        subject, "attempt_self_fix",
        lambda task_id, reason, diag: box.fixes.append((task_id, reason, diag)),
    )
    return box


DIAG = {
    "root_cause": "the poller never reconciles the window",
    "failure_class": "orchestrator-logic",
    "suggested_fix": "reconcile in_flight on every cycle",
    "target_files": ["scripts/orchestrator_run.py", "scripts/watchdog.py"],
}


# --- the parked_tasks ledger -----------------------------------------------------------


def test_park_failed_adds_the_task_to_the_parked_ledger(subject, sandbox, failing):
    subject.park_failed("T1", "boom")
    assert _get_state(subject, sandbox)["parked_tasks"] == ["T1"]


def test_park_failed_appends_without_disturbing_other_parked_tasks(subject, sandbox, failing):
    _put_state(subject, sandbox, parked_tasks=["T0"])
    subject.park_failed("T1", "boom")
    assert _get_state(subject, sandbox)["parked_tasks"] == ["T0", "T1"]


def test_park_failed_does_not_duplicate_an_already_parked_task(subject, sandbox, failing):
    _put_state(subject, sandbox, parked_tasks=["T1"])
    subject.park_failed("T1", "boom")
    assert _get_state(subject, sandbox)["parked_tasks"] == ["T1"]


def test_park_failed_parks_before_the_idempotence_check(subject, sandbox, failing):
    """Even the "already asked" path leaves the task parked — the ledger write is first."""
    req = subject._escalation_req_id("T1", "boom")
    _put_answer(subject, sandbox, req, {"choice": "Shelve task"})
    subject.park_failed("T1", "boom")
    assert _get_state(subject, sandbox)["parked_tasks"] == ["T1"]


def test_park_failed_needs_state_json_to_exist(subject, sandbox, notifier, danreq, runner):
    with pytest.raises(Exception):
        subject.park_failed("T1", "boom")


# --- idempotence: a previously answered escalation --------------------------------------


def test_park_failed_reuses_a_prior_answer_instead_of_re_asking(subject, sandbox, failing):
    req = subject._escalation_req_id("T1", "boom")
    _put_answer(subject, sandbox, req, {"choice": "Shelve task"})
    subject.park_failed("T1", "boom")
    assert failing.danreqs == []
    assert _event_names(subject, sandbox) == ["failed_escalation_reused"]


def test_park_failed_reuse_records_the_request_id_and_the_answer(subject, sandbox, failing):
    req = subject._escalation_req_id("T1", "boom")
    _put_answer(subject, sandbox, req, {"choice": "Shelve task"})
    subject.park_failed("T1", "boom")
    assert _detail(subject, sandbox, "failed_escalation_reused") == \
        f"T1 req={req} prior=Shelve task"


def test_park_failed_reuse_truncates_the_recorded_answer_at_sixty_chars(subject, sandbox, failing):
    req = subject._escalation_req_id("T1", "boom")
    _put_answer(subject, sandbox, req, {"choice": "A" * 200})
    subject.park_failed("T1", "boom")
    assert _detail(subject, sandbox, "failed_escalation_reused").endswith("prior=" + "A" * 60)


def test_park_failed_reuse_accepts_a_bare_string_answer(subject, sandbox, failing):
    req = subject._escalation_req_id("T1", "boom")
    _put_answer(subject, sandbox, req, "just retry it")
    subject.park_failed("T1", "boom")
    assert _event_names(subject, sandbox) == ["failed_escalation_reused"]


def test_park_failed_reuse_is_silent_on_telegram(subject, sandbox, failing):
    """FOUND_BUGS: Dan's answer is only ever used to suppress the re-ask, never acted on."""
    req = subject._escalation_req_id("T1", "boom")
    _put_answer(subject, sandbox, req, {"choice": "Retry with new approach"})
    subject.park_failed("T1", "boom")
    assert failing.sent == []
    assert failing.fixes == []


def test_park_failed_reuse_skips_the_diagnosis_pass_entirely(subject, sandbox, failing):
    failing.diagnosis = dict(DIAG)
    req = subject._escalation_req_id("T1", "boom")
    _put_answer(subject, sandbox, req, {"choice": "Shelve task"})
    subject.park_failed("T1", "boom")
    assert failing.diagnose_calls == []


def test_park_failed_ignores_an_empty_answer_file(subject, sandbox, failing):
    req = subject._escalation_req_id("T1", "boom")
    _put_answer(subject, sandbox, req, "   ")
    subject.park_failed("T1", "boom")
    assert [c.req_id for c in failing.danreqs] == [req]


def test_park_failed_ignores_an_answer_for_a_different_failure_class(subject, sandbox, failing):
    _put_answer(subject, sandbox, subject._escalation_req_id("T1", "boom"), {"choice": "Shelve task"})
    subject.park_failed("T1", "a completely different explosion")
    assert len(failing.danreqs) == 1


def test_park_failed_reuses_an_answer_across_a_volatile_session_id(subject, sandbox, failing):
    first = "window gone — stale in_flight impl-t1-20260619-115951"
    _put_answer(subject, sandbox, subject._escalation_req_id("T1", first), {"choice": "Shelve task"})
    subject.park_failed("T1", "window gone — stale in_flight impl-t1-20260701-010203")
    assert failing.danreqs == []
    assert _event_names(subject, sandbox) == ["failed_escalation_reused"]


# --- idempotence: an asked-but-unanswered escalation -------------------------------------


def test_park_failed_skips_an_escalation_that_is_still_pending(subject, sandbox, failing):
    req = subject._escalation_req_id("T1", "boom")
    _put_question(subject, sandbox, req)
    subject.park_failed("T1", "boom")
    assert failing.danreqs == []
    assert _detail(subject, sandbox, "failed_escalation_pending") == f"T1 req={req}"


def test_park_failed_pending_path_sends_nothing_and_diagnoses_nothing(subject, sandbox, failing):
    failing.diagnosis = dict(DIAG)
    _put_question(subject, sandbox, subject._escalation_req_id("T1", "boom"))
    subject.park_failed("T1", "boom")
    assert failing.sent == []
    assert failing.diagnose_calls == []
    assert _event_names(subject, sandbox) == ["failed_escalation_pending"]


def test_park_failed_answer_wins_over_a_still_present_question_file(subject, sandbox, failing):
    req = subject._escalation_req_id("T1", "boom")
    _put_question(subject, sandbox, req)
    _put_answer(subject, sandbox, req, {"choice": "Shelve task"})
    subject.park_failed("T1", "boom")
    assert _event_names(subject, sandbox) == ["failed_escalation_reused"]


# --- the escalation itself ---------------------------------------------------------------


def test_park_failed_asks_dan_with_the_three_recovery_options(subject, sandbox, failing):
    subject.park_failed("T1", "gate failed")
    assert failing.danreqs[0].options == \
        ["Retry with new approach", "Shelve task", "Manual intervention"]


def test_park_failed_question_names_the_task_the_retry_count_and_the_reason(subject, sandbox, failing):
    _put_state(subject, sandbox, retry_counts={"T1": 1})
    subject.park_failed("T1", "gate failed")
    assert failing.danreqs[0].question == "T1 failed after 1 retry: gate failed. What to do?"


def test_park_failed_question_tags_a_timeout_death(subject, sandbox, failing):
    subject.park_failed("T1", "timeout after 10800s")
    assert failing.danreqs[0].question == \
        "T1 failed after 0 retries (timeout): timeout after 10800s. What to do?"


def test_park_failed_question_truncates_the_reason_at_two_hundred_chars(subject, sandbox, failing):
    subject.park_failed("T1", "z" * 500)
    assert failing.danreqs[0].question == f"T1 failed after 0 retries: {'z' * 200}. What to do?"


def test_park_failed_passes_the_deterministic_request_id(subject, sandbox, failing):
    subject.park_failed("T1", "gate failed")
    assert failing.danreqs[0].req_id == subject._escalation_req_id("T1", "gate failed")


def test_park_failed_uses_the_default_decision_request_type(subject, sandbox, failing):
    subject.park_failed("T1", "gate failed")
    assert failing.danreqs[0].req_type == "decision"


def test_park_failed_journals_the_escalation_with_a_truncated_reason(subject, sandbox, failing):
    subject.park_failed("T1", "y" * 200)
    assert _detail(subject, sandbox, "failed_escalated") == f"T1 reason={'y' * 80}"


def test_park_failed_shells_out_to_the_request_sender(subject, sandbox, notifier, runner, monkeypatch):
    """The one test that leaves `_danreq` real, to pin the argv it builds."""
    _put_state(subject, sandbox)
    monkeypatch.setattr(subject, "ORCH_DIAGNOSE", False)
    subject.park_failed("T1", "gate failed")
    argv = runner.find("--question")[0]
    assert argv[argv.index("--type") + 1] == "decision"
    assert argv[argv.index("--options") + 1] == \
        "Retry with new approach,Shelve task,Manual intervention"
    assert argv[argv.index("--id") + 1] == subject._escalation_req_id("T1", "gate failed")
    assert runner.calls[0].kwargs["capture_output"] is True


# --- the usage-limit bail-out -------------------------------------------------------------


@pytest.mark.parametrize("reason", [
    "hit your usage limit",
    "usage limit reached",
    "rate-limit from the API",
    "too many requests",
    "overloaded_error",
])
def test_park_failed_skips_the_llm_when_the_reason_is_a_usage_limit(subject, sandbox, failing, reason):
    failing.diagnosis = dict(DIAG)
    subject.park_failed("T1", reason)
    assert failing.diagnose_calls == []
    assert _detail(subject, sandbox, "diagnose_skipped_usage_limit") == "T1"


def test_park_failed_still_escalates_before_the_usage_limit_bail_out(subject, sandbox, failing):
    subject.park_failed("T1", "hit your usage limit")
    assert len(failing.danreqs) == 1
    assert _event_names(subject, sandbox) == ["failed_escalated", "diagnose_skipped_usage_limit"]


def test_park_failed_skips_the_llm_when_the_impl_log_shows_a_usage_limit(subject, sandbox, failing):
    ws = _make_workspace(subject, sandbox, "impl-T1-20260101-000000")
    (ws / "impl.log").write_text("working…\nClaude usage limit reached\n", encoding="utf-8")
    failing.diagnosis = dict(DIAG)
    subject.park_failed("T1", "gate failed")
    assert failing.diagnose_calls == []
    assert "diagnose_skipped_usage_limit" in _event_names(subject, sandbox)


def test_park_failed_usage_limit_bail_out_sends_nothing_to_telegram(subject, sandbox, failing):
    subject.park_failed("T1", "hit the usage limit")
    assert failing.sent == []


# --- ORCH_DIAGNOSE off ---------------------------------------------------------------------


def test_park_failed_with_diagnosis_off_reports_the_journal_tail(subject, sandbox, failing, monkeypatch):
    monkeypatch.setattr(subject, "ORCH_DIAGNOSE", False)
    subject.park_failed("T1", "gate failed")
    assert len(failing.sent) == 1
    msg = failing.sent[0]
    assert msg.startswith(f"{INFO} *T1* failure (ORCH_DIAGNOSE off):\nReason: gate failed")
    assert "Last journal lines:" in msg
    assert '"event": "failed_escalated"' in msg


def test_park_failed_with_diagnosis_off_never_calls_the_llm(subject, sandbox, failing, monkeypatch):
    monkeypatch.setattr(subject, "ORCH_DIAGNOSE", False)
    failing.diagnosis = dict(DIAG)
    subject.park_failed("T1", "gate failed")
    assert failing.diagnose_calls == []
    assert failing.fixes == []


def test_park_failed_with_diagnosis_off_truncates_the_reason_at_three_hundred(subject, sandbox, failing, monkeypatch):
    monkeypatch.setattr(subject, "ORCH_DIAGNOSE", False)
    subject.park_failed("T1", "q" * 400)
    assert f"Reason: {'q' * 300}\n" in failing.sent[0]


def test_park_failed_journal_tail_only_keeps_lines_naming_the_task(subject, sandbox, failing, monkeypatch):
    monkeypatch.setattr(subject, "ORCH_DIAGNOSE", False)
    _put_journal(subject, sandbox, json.dumps({"event": "other", "detail": "T9 unrelated"}))
    subject.park_failed("T1", "gate failed")
    assert "T9 unrelated" not in failing.sent[0]


def test_park_failed_journal_tail_leaks_lines_of_prefix_sibling_tasks(subject, sandbox, failing, monkeypatch):
    """FOUND_BUGS: the filter is a bare substring test, so `T1` matches a `T10` line."""
    monkeypatch.setattr(subject, "ORCH_DIAGNOSE", False)
    _put_journal(subject, sandbox, json.dumps({"event": "other", "detail": "T10 sibling"}))
    subject.park_failed("T1", "gate failed")
    assert "T10 sibling" in failing.sent[0]


# --- the diagnosis pass ---------------------------------------------------------------------


def test_park_failed_feeds_the_diagnosis_the_task_reason_tail_and_journal(subject, sandbox, failing):
    ws = _make_workspace(subject, sandbox, "impl-T1-20260101-000000")
    (ws / "impl.log").write_text("line one\nline two\n", encoding="utf-8")
    subject.park_failed("T1", "gate failed")
    task_id, reason, impl_tail, journal_ctx = failing.diagnose_calls[0]
    assert (task_id, reason) == ("T1", "gate failed")
    assert impl_tail == "line one\nline two"
    assert '"event": "failed_escalated"' in journal_ctx


def test_park_failed_reports_an_empty_diagnosis_as_such(subject, sandbox, failing):
    failing.diagnosis = {}
    subject.park_failed("T1", "gate failed")
    assert failing.sent == [
        f"{MAGNIFY} *T1* failed; diagnosis pass returned nothing.\nReason: gate failed"
    ]
    assert "failure_diagnosis" not in _event_names(subject, sandbox)


def test_park_failed_publishes_the_diagnosis_to_telegram(subject, sandbox, failing):
    failing.diagnosis = dict(DIAG)
    subject.park_failed("T1", "gate failed")
    assert failing.sent[0] == (
        f"{MAGNIFY} *T1* diagnosis (class: orchestrator-logic)\n"
        "Root cause: the poller never reconciles the window\n"
        "Suggested fix: reconcile in_flight on every cycle\n"
        "Likely files: scripts/orchestrator_run.py, scripts/watchdog.py"
    )


def test_park_failed_diagnosis_renders_an_em_dash_for_no_target_files(subject, sandbox, failing):
    failing.diagnosis = dict(DIAG, target_files=[])
    subject.park_failed("T1", "gate failed")
    assert failing.sent[0].endswith("Likely files: \N{EM DASH}")


def test_park_failed_diagnosis_defaults_a_missing_class_to_a_question_mark(subject, sandbox, failing):
    failing.diagnosis = {"root_cause": "r", "suggested_fix": "f"}
    subject.park_failed("T1", "gate failed")
    assert failing.sent[0].startswith(f"{MAGNIFY} *T1* diagnosis (class: ?)")


def test_park_failed_persists_the_diagnosis_for_a_later_slash_fix(subject, sandbox, failing):
    failing.diagnosis = dict(DIAG)
    subject.park_failed("T1", "gate failed")
    saved = json.loads((_diagnoses_dir(subject, sandbox) / "T1.json").read_text(encoding="utf-8"))
    assert saved["task_id"] == "T1"
    assert saved["reason"] == "gate failed"
    assert saved["failure_class"] == "orchestrator-logic"
    assert saved["target_files"] == DIAG["target_files"]


def test_park_failed_journals_the_diagnosis_class_and_root_cause(subject, sandbox, failing):
    failing.diagnosis = dict(DIAG)
    subject.park_failed("T1", "gate failed")
    assert _detail(subject, sandbox, "failure_diagnosis") == \
        "T1 class=orchestrator-logic: the poller never reconciles the window"


def test_park_failed_swallows_a_diagnosis_exception(subject, sandbox, failing):
    failing.diagnose_raises = RuntimeError("agent CLI died")
    subject.park_failed("T1", "gate failed")
    assert failing.sent == []
    assert _event_names(subject, sandbox) == ["failed_escalated"]


def test_park_failed_diagnosis_exception_leaves_the_task_parked_and_escalated(subject, sandbox, failing):
    failing.diagnose_raises = RuntimeError("agent CLI died")
    subject.park_failed("T1", "gate failed")
    assert _get_state(subject, sandbox)["parked_tasks"] == ["T1"]
    assert len(failing.danreqs) == 1


def test_park_failed_swallows_an_exception_from_the_self_fix_runner(subject, sandbox, failing, monkeypatch):
    """FOUND_BUGS: Dan is told the fix is being implemented, then the failure is printed only."""
    failing.diagnosis = dict(DIAG)
    failing.eligible = (True, "eligible")

    def boom(task_id, reason, diag):
        raise RuntimeError("self-fix exploded")

    monkeypatch.setattr(subject, "attempt_self_fix", boom)
    subject.park_failed("T1", "gate failed")
    assert failing.sent[-1] == f"{ARROW} Auto-eligible \N{EM DASH} implementing the fix for `T1` now."
    assert "self_fix_skipped" not in _event_names(subject, sandbox)


# --- the self-fix decision ---------------------------------------------------------------------


def test_park_failed_offers_slash_fix_when_the_diagnosis_is_not_auto_eligible(subject, sandbox, failing):
    failing.diagnosis = dict(DIAG)
    failing.eligible = (False, "class 'task-spec' not in self-fix whitelist")
    subject.park_failed("T1", "gate failed")
    assert failing.sent[-1] == (
        f"{ARROW} Reply `/fix T1` to have me implement this fix, or "
        "`/unpark T1` to retry as a task / `/reject` to skip."
    )
    assert failing.fixes == []


def test_park_failed_journals_why_the_self_fix_was_skipped(subject, sandbox, failing):
    failing.diagnosis = dict(DIAG)
    failing.eligible = (False, "rate-limited")
    subject.park_failed("T1", "gate failed")
    assert _detail(subject, sandbox, "self_fix_skipped") == "T1 rate-limited"


def test_park_failed_runs_the_self_fix_when_eligible(subject, sandbox, failing):
    failing.diagnosis = dict(DIAG)
    failing.eligible = (True, "eligible")
    subject.park_failed("T1", "gate failed")
    assert failing.fixes == [("T1", "gate failed", DIAG)]
    assert failing.sent[-1] == f"{ARROW} Auto-eligible \N{EM DASH} implementing the fix for `T1` now."


def test_park_failed_eligible_path_journals_no_skip(subject, sandbox, failing):
    failing.diagnosis = dict(DIAG)
    failing.eligible = (True, "eligible")
    subject.park_failed("T1", "gate failed")
    assert _event_names(subject, sandbox) == ["failed_escalated", "failure_diagnosis"]


def test_park_failed_does_not_consult_the_skeptic_when_not_eligible(subject, sandbox, failing, monkeypatch):
    monkeypatch.setattr(subject, "ORCH_SELF_FIX_SKEPTIC", True)
    seen = []
    monkeypatch.setattr(subject, "_failure_looks_normal",
                        lambda *a: (seen.append(a), {"verdict": "normal", "confidence": 1.0})[1])
    failing.diagnosis = dict(DIAG)
    failing.eligible = (False, "not whitelisted")
    subject.park_failed("T1", "gate failed")
    assert seen == []


def test_park_failed_skeptic_veto_holds_the_auto_fix(subject, sandbox, failing, monkeypatch):
    monkeypatch.setattr(subject, "ORCH_SELF_FIX_SKEPTIC", True)
    failing.diagnosis = dict(DIAG)
    failing.eligible = (True, "eligible")
    failing.skeptic = {"verdict": "normal", "confidence": 0.9, "rationale": "flaky network"}
    subject.park_failed("T1", "gate failed")
    assert failing.fixes == []
    assert failing.sent[-1] == (
        f"{ARROW} Auto-fix held for `T1`: a second, independent check thinks this is likely "
        "*normal/transient*, not a Maestro bug (90%): flaky network\n"
        "Reply `/fix T1` to override and fix anyway, or `/unpark T1` to retry / "
        "`/reject` to skip."
    )


def test_park_failed_skeptic_veto_journals_the_confidence(subject, sandbox, failing, monkeypatch):
    monkeypatch.setattr(subject, "ORCH_SELF_FIX_SKEPTIC", True)
    failing.diagnosis = dict(DIAG)
    failing.eligible = (True, "eligible")
    failing.skeptic = {"verdict": "normal", "confidence": 0.9, "rationale": "flaky network"}
    subject.park_failed("T1", "gate failed")
    assert _detail(subject, sandbox, "self_fix_vetoed_skeptic") == "T1 conf=0.90: flaky network"


def test_park_failed_skeptic_veto_falls_back_to_a_default_rationale(subject, sandbox, failing, monkeypatch):
    monkeypatch.setattr(subject, "ORCH_SELF_FIX_SKEPTIC", True)
    failing.diagnosis = dict(DIAG)
    failing.eligible = (True, "eligible")
    failing.skeptic = {"verdict": "normal", "confidence": 1.0}
    subject.park_failed("T1", "gate failed")
    assert "looks transient/normal" in failing.sent[-1]


def test_park_failed_skeptic_below_the_threshold_does_not_veto(subject, sandbox, failing, monkeypatch):
    monkeypatch.setattr(subject, "ORCH_SELF_FIX_SKEPTIC", True)
    threshold = subject.SELF_FIX_SKEPTIC_MIN_CONF
    failing.diagnosis = dict(DIAG)
    failing.eligible = (True, "eligible")
    failing.skeptic = {"verdict": "normal", "confidence": threshold - 0.01, "rationale": "meh"}
    subject.park_failed("T1", "gate failed")
    assert failing.fixes == [("T1", "gate failed", DIAG)]


def test_park_failed_skeptic_exactly_at_the_threshold_vetoes(subject, sandbox, failing, monkeypatch):
    monkeypatch.setattr(subject, "ORCH_SELF_FIX_SKEPTIC", True)
    failing.diagnosis = dict(DIAG)
    failing.eligible = (True, "eligible")
    failing.skeptic = {"verdict": "normal",
                       "confidence": subject.SELF_FIX_SKEPTIC_MIN_CONF, "rationale": "meh"}
    subject.park_failed("T1", "gate failed")
    assert failing.fixes == []


def test_park_failed_skeptic_that_agrees_it_is_a_bug_does_not_veto(subject, sandbox, failing, monkeypatch):
    monkeypatch.setattr(subject, "ORCH_SELF_FIX_SKEPTIC", True)
    failing.diagnosis = dict(DIAG)
    failing.eligible = (True, "eligible")
    failing.skeptic = {"verdict": "bug", "confidence": 1.0, "rationale": "real defect"}
    subject.park_failed("T1", "gate failed")
    assert failing.fixes == [("T1", "gate failed", DIAG)]


def test_park_failed_skeptic_with_a_missing_confidence_reads_as_zero(subject, sandbox, failing, monkeypatch):
    monkeypatch.setattr(subject, "ORCH_SELF_FIX_SKEPTIC", True)
    failing.diagnosis = dict(DIAG)
    failing.eligible = (True, "eligible")
    failing.skeptic = {"verdict": "normal"}
    subject.park_failed("T1", "gate failed")
    assert failing.fixes == [("T1", "gate failed", DIAG)]


# =======================================================================================
# park_regression
# =======================================================================================


def test_park_regression_asks_dan_with_the_three_revert_options(subject, sandbox, danreq):
    _put_state(subject, sandbox)
    subject.park_regression("T1", {"metrics": {"recall_at_5": 0.42}, "reason": "worse"})
    assert danreq[0].options == ["Revert and shelve", "Revert and retry", "Keep despite regression"]


def test_park_regression_question_carries_the_metric_and_reason(subject, sandbox, danreq):
    """The metric is named by the adapter that reported it. This question used to read
    `Recall@5=0.42` whatever the project measured, so on anything but the reference it
    asked Dan to weigh a regression in a number that project has never computed."""
    subject.park_regression("T1", {"metrics": {"recall_at_5": 0.42}, "reason": "worse"})
    assert danreq[0].question == "Regression on T1: recall_at_5=0.42. worse. What to do?"


def test_park_regression_says_so_plainly_when_there_are_no_metrics(subject, sandbox, danreq):
    """Was `Recall@5=?` — a named metric reported as missing. A project with no eval
    harness is not missing a number, it has none, and the question now says that."""
    subject.park_regression("T1", {})
    assert danreq[0].question == "Regression on T1: no metrics. . What to do?"


def test_park_regression_truncates_the_reason_at_two_hundred_chars(subject, sandbox, danreq):
    subject.park_regression("T1", {"reason": "w" * 400})
    assert f". {'w' * 200}. What to do?" in danreq[0].question


def test_park_regression_journals_the_escalation(subject, sandbox, danreq):
    subject.park_regression("T1", {"metrics": {"recall_at_5": 0.42}})
    assert _detail(subject, sandbox, "regression_escalated") == "T1 recall_at_5=0.42"


def test_park_regression_sends_no_request_id_so_the_answer_is_unaddressable(subject, sandbox, danreq):
    """FOUND_BUGS: no `req_id`, so nothing can ever correlate Dan's choice back to it."""
    subject.park_regression("T1", {})
    assert danreq[0].req_id is None


def test_park_regression_does_not_park_anything(subject, sandbox, danreq):
    """FOUND_BUGS: despite the name it touches neither `parked_tasks` nor `waiting_on_dan`."""
    _put_state(subject, sandbox, parked_tasks=[], waiting_on_dan={})
    subject.park_regression("T1", {})
    state = _get_state(subject, sandbox)
    assert state["parked_tasks"] == []
    assert state["waiting_on_dan"] == {}


def test_park_regression_needs_no_state_json(subject, sandbox, danreq):
    subject.park_regression("T1", {})
    assert not _state_json(subject, sandbox).exists()


def test_park_regression_shells_out_to_the_request_sender(subject, sandbox, runner):
    subject.park_regression("T1", {"metrics": {"recall_at_5": 0.5}})
    argv = runner.find("--question")[0]
    assert "--id" not in argv
    assert argv[argv.index("--options") + 1] == \
        "Revert and shelve,Revert and retry,Keep despite regression"


# =======================================================================================
# _session_uuid_for
# =======================================================================================


def test_session_uuid_for_empty_session_id_is_empty(subject, sandbox):
    assert subject._session_uuid_for("") == ""


def test_session_uuid_for_missing_workspace_is_empty(subject, sandbox):
    assert subject._session_uuid_for("impl-T1-1") == ""


def test_session_uuid_for_reads_the_sidecar_file(subject, sandbox):
    _make_workspace(subject, sandbox, "impl-T1-1", uuid="abc-123")
    assert subject._session_uuid_for("impl-T1-1") == "abc-123"


def test_session_uuid_for_strips_surrounding_whitespace(subject, sandbox):
    _make_workspace(subject, sandbox, "impl-T1-1", uuid="  abc-123\n\n")
    assert subject._session_uuid_for("impl-T1-1") == "abc-123"


def test_session_uuid_for_swallows_a_read_error(subject, sandbox):
    ws = _make_workspace(subject, sandbox, "impl-T1-1")
    (ws / "session_uuid.txt").mkdir()
    assert subject._session_uuid_for("impl-T1-1") == ""


def test_session_uuid_for_empty_file_is_empty(subject, sandbox):
    _make_workspace(subject, sandbox, "impl-T1-1", uuid="")
    assert subject._session_uuid_for("impl-T1-1") == ""


# =======================================================================================
# park_for_dan — the HITL park of a fully gated task
# =======================================================================================


def _entry(task_id="T1", session_id="impl-T1-1", **extra) -> dict:
    entry = {"task_id": task_id, "session_id": session_id,
             "branch": f"{task_id.lower()}-work", "worktree": f"/wt/{task_id}"}
    entry.update(extra)
    return entry


def test_park_for_dan_allocates_the_next_dan_id(subject, sandbox, notifier):
    _put_state(subject, sandbox, dan_id_counter=7)
    _make_workspace(subject, sandbox, "impl-T1-1")
    assert subject.park_for_dan(_entry(), "did the thing") == 8
    assert _get_state(subject, sandbox)["dan_id_counter"] == 8


def test_park_for_dan_starts_the_counter_at_one(subject, sandbox, notifier):
    _put_state(subject, sandbox)
    _make_workspace(subject, sandbox, "impl-T1-1")
    assert subject.park_for_dan(_entry(), "s") == 1


def test_park_for_dan_writes_the_waiting_entry(subject, sandbox, notifier):
    _put_state(subject, sandbox)
    _make_workspace(subject, sandbox, "impl-T1-1", uuid="uuid-1")
    subject.park_for_dan(_entry(), "did the thing")
    entry = _waiting(subject, sandbox)["1"]
    assert set(entry) == {"task_id", "session_id", "branch", "worktree",
                          "parked_at", "summary", "session_uuid"}
    assert entry["task_id"] == "T1"
    assert entry["branch"] == "t1-work"
    assert entry["worktree"] == "/wt/T1"
    assert entry["summary"] == "did the thing"
    assert entry["session_uuid"] == "uuid-1"


def test_park_for_dan_does_not_tag_the_entry_with_a_kind(subject, sandbox, notifier):
    """The absence of `kind` is what tells /approve to take the merge path."""
    _put_state(subject, sandbox)
    _make_workspace(subject, sandbox, "impl-T1-1")
    subject.park_for_dan(_entry(), "s")
    assert "kind" not in _waiting(subject, sandbox)["1"]


def test_park_for_dan_truncates_the_summary_at_two_hundred_chars(subject, sandbox, notifier):
    _put_state(subject, sandbox)
    _make_workspace(subject, sandbox, "impl-T1-1")
    subject.park_for_dan(_entry(), "s" * 500)
    assert _waiting(subject, sandbox)["1"]["summary"] == "s" * 200


def test_park_for_dan_defaults_a_missing_branch_and_worktree_to_empty(subject, sandbox, notifier):
    _put_state(subject, sandbox)
    _make_workspace(subject, sandbox, "impl-T1-1")
    subject.park_for_dan({"task_id": "T1", "session_id": "impl-T1-1"}, "s")
    entry = _waiting(subject, sandbox)["1"]
    assert entry["branch"] == "" and entry["worktree"] == ""


def test_park_for_dan_keeps_earlier_waiting_entries(subject, sandbox, notifier):
    _put_state(subject, sandbox, dan_id_counter=1, waiting_on_dan={"1": {"task_id": "T0"}})
    _make_workspace(subject, sandbox, "impl-T1-1")
    subject.park_for_dan(_entry(), "s")
    assert sorted(_waiting(subject, sandbox)) == ["1", "2"]


def test_park_for_dan_drops_the_paused_sentinel_in_the_workspace(subject, sandbox, notifier):
    _put_state(subject, sandbox, dan_id_counter=4)
    ws = _make_workspace(subject, sandbox, "impl-T1-1")
    subject.park_for_dan(_entry(), "s")
    assert (ws / "PAUSED").read_text(encoding="utf-8") == "5"


def test_park_for_dan_pings_dan_with_the_review_affordances(subject, sandbox, notifier):
    _put_state(subject, sandbox)
    _make_workspace(subject, sandbox, "impl-T1-1")
    subject.park_for_dan(_entry(), "did the thing")
    assert notifier == [
        f"{PAUSE} *T1* passed all gates \N{EM DASH} waiting for your review (ID: 1)\n"
        "Branch: `t1-work`\n"
        "did the thing\n\n"
        "Reply /approve 1 or /reject 1"
    ]


def test_park_for_dan_journals_the_park_against_the_session(subject, sandbox, notifier):
    _put_state(subject, sandbox)
    _make_workspace(subject, sandbox, "impl-T1-1")
    subject.park_for_dan(_entry(), "s")
    record = _record(subject, sandbox, "hitl_parked")
    assert record["detail"] == "T1 dan_id=1"
    assert record["session_id"] == "impl-T1-1"


def test_park_for_dan_without_a_workspace_dies_after_mutating_state(subject, sandbox, notifier):
    """FOUND_BUGS: the state write happens first, so the dan_id is burned and Dan is never told."""
    _put_state(subject, sandbox)
    with pytest.raises(FileNotFoundError):
        subject.park_for_dan(_entry(), "s")
    assert _waiting(subject, sandbox)["1"]["task_id"] == "T1"
    assert notifier == []
    assert _event_names(subject, sandbox) == []


# =======================================================================================
# park_manual_action — the dispatch:manual park
# =======================================================================================


def test_park_manual_action_allocates_the_next_dan_id(subject, sandbox, notifier, prep):
    _put_state(subject, sandbox, dan_id_counter=3)
    assert subject.park_manual_action("T1", "upload the notebook", None) == 4


def test_park_manual_action_writes_a_manual_action_waiting_entry(subject, sandbox, notifier, prep):
    _put_state(subject, sandbox)
    _make_workspace(subject, sandbox, "impl-T1-1", uuid="uuid-9")
    subject.park_manual_action("T1", "upload the notebook", "python3 import.py",
                               branch="b", worktree="/wt", session_id="impl-T1-1")
    entry = _waiting(subject, sandbox)["1"]
    assert set(entry) == {"task_id", "session_id", "branch", "worktree",
                          "parked_at", "kind", "summary", "session_uuid"}
    assert entry["kind"] == "manual-action"
    assert entry["summary"] == "upload the notebook"
    assert entry["session_uuid"] == "uuid-9"


def test_park_manual_action_defaults_every_optional_field_to_empty(subject, sandbox, notifier, prep):
    _put_state(subject, sandbox)
    subject.park_manual_action("T1", "do it", None)
    entry = _waiting(subject, sandbox)["1"]
    assert entry["branch"] == "" and entry["worktree"] == "" and entry["session_id"] == ""
    assert entry["session_uuid"] == ""


def test_park_manual_action_truncates_the_stored_summary_but_not_the_ping(subject, sandbox, notifier, prep):
    """FOUND_BUGS: the stored summary is cut to 200 chars, the ping and sidecar keep it all."""
    _put_state(subject, sandbox)
    subject.park_manual_action("T1", "a" * 400, None)
    assert _waiting(subject, sandbox)["1"]["summary"] == "a" * 200
    assert "a" * 400 in notifier[0]
    assert prep.calls[0].action == "a" * 400


def test_park_manual_action_records_the_action_in_the_sidecar(subject, sandbox, notifier, prep):
    _put_state(subject, sandbox, dan_id_counter=6)
    subject.park_manual_action("T1", "upload it", "python3 import.py")
    call = prep.calls[0]
    assert (call.op, call.task_id, call.action) == ("set", "T1", "upload it")
    assert call.post_action_cmd == "python3 import.py"
    assert call.dan_id == 7


def test_park_manual_action_pings_dan_with_all_four_affordances(subject, sandbox, notifier, prep):
    _put_state(subject, sandbox)
    subject.park_manual_action("T1", "upload the notebook", None)
    msg = notifier[0]
    assert msg.startswith(
        f"{TARGET} *T1* is prepped \N{EM DASH} your ONE action (ID 1):\n\nupload the notebook\n\n"
    )
    assert f"{CHECK} It worked {ARROW} /approve 1" in msg
    assert f"{QUESTION} Problem / question {ARROW} /ask 1 <your message>" in msg
    assert f"{WRENCH} Needs a rewrite {ARROW} /redo 1 <what's wrong>" in msg
    assert msg.endswith(f"{CROSS} Abandon {ARROW} /reject 1")


def test_park_manual_action_journals_against_the_session(subject, sandbox, notifier, prep):
    _put_state(subject, sandbox)
    subject.park_manual_action("T1", "do it", None, session_id="impl-T1-1")
    record = _record(subject, sandbox, "manual_action_parked")
    assert record["detail"] == "T1 dan_id=1"
    assert record["session_id"] == "impl-T1-1"


def test_park_manual_action_needs_no_workspace_directory(subject, sandbox, notifier, prep):
    _put_state(subject, sandbox)
    subject.park_manual_action("T1", "do it", None, session_id="never-created")
    assert _waiting(subject, sandbox)["1"]["session_uuid"] == ""


def test_park_manual_action_keeps_earlier_waiting_entries(subject, sandbox, notifier, prep):
    _put_state(subject, sandbox, dan_id_counter=1, waiting_on_dan={"1": {"task_id": "T0"}})
    subject.park_manual_action("T1", "do it", None)
    assert sorted(_waiting(subject, sandbox)) == ["1", "2"]


# =======================================================================================
# handle_prep_done — end to end over a sandbox repo
# =======================================================================================


@pytest.fixture
def prep_done(subject, sandbox, monkeypatch, notifier, runner, prep):
    """`handle_prep_done` with the prep merge stubbed; everything else is real."""
    _put_state(subject, sandbox, in_flight=[{"session_id": "impl-T1-1", "task_id": "T1"}])
    box = SimpleNamespace(sent=notifier, runner=runner, prep=prep,
                          merge=(True, "merged t1-prep at abc1234"), removed=[])
    monkeypatch.setattr(subject, "_merge_prep_branch", lambda entry: box.merge)
    monkeypatch.setattr(subject, "remove_worktree", lambda wt: box.removed.append(Path(wt)))
    return box


def test_handle_prep_done_parks_the_action_from_the_implementer_result(subject, sandbox, prep_done):
    subject.handle_prep_done("T1", {}, _entry(), {"dan_action": "run the notebook"})
    assert _waiting(subject, sandbox)["1"]["summary"] == "run the notebook"


def test_handle_prep_done_falls_back_to_the_task_definition(subject, sandbox, prep_done):
    subject.handle_prep_done("T1", {"dan_action": "from roadmap"}, _entry(), {})
    assert _waiting(subject, sandbox)["1"]["summary"] == "from roadmap"


def test_handle_prep_done_falls_back_to_a_placeholder_action(subject, sandbox, prep_done):
    subject.handle_prep_done("T1", {}, _entry(), {})
    assert _waiting(subject, sandbox)["1"]["summary"] == \
        "(prep done \N{EM DASH} see the ROADMAP task for the action)"


def test_handle_prep_done_tolerates_a_non_dict_implementer_result(subject, sandbox, prep_done):
    subject.handle_prep_done("T1", {"dan_action": "from roadmap"}, _entry(), None)
    assert _waiting(subject, sandbox)["1"]["summary"] == "from roadmap"


def test_handle_prep_done_prefers_the_implementer_post_action_cmd(subject, sandbox, prep_done):
    subject.handle_prep_done("T1", {"post_action_cmd": "roadmap.py"}, _entry(),
                             {"dan_action": "a", "post_action_cmd": "impl.py"})
    assert prep_done.prep.calls[0].post_action_cmd == "impl.py"


def test_handle_prep_done_falls_back_to_the_task_post_action_cmd(subject, sandbox, prep_done):
    subject.handle_prep_done("T1", {"post_action_cmd": "roadmap.py"}, _entry(), {"dan_action": "a"})
    assert prep_done.prep.calls[0].post_action_cmd == "roadmap.py"


def test_handle_prep_done_journals_the_successful_prep_merge(subject, sandbox, prep_done):
    subject.handle_prep_done("T1", {}, _entry(), {"dan_action": "a"})
    record = _record(subject, sandbox, "prep_merged")
    assert record["detail"] == "T1 merged t1-prep at abc1234"
    assert record["session_id"] == "impl-T1-1"


def test_handle_prep_done_pushes_main_after_a_successful_merge(subject, sandbox, prep_done):
    subject.handle_prep_done("T1", {}, _entry(), {"dan_action": "a"})
    assert prep_done.runner.find("push", "origin", "main")


def test_handle_prep_done_parks_the_action_even_when_the_merge_is_blocked(subject, sandbox, prep_done):
    prep_done.merge = (False, "dirty worktree blocks prep merge: main_bot.py")
    subject.handle_prep_done("T1", {}, _entry(), {"dan_action": "a"})
    assert _waiting(subject, sandbox)["1"]["kind"] == "manual-action"
    assert _detail(subject, sandbox, "prep_merge_failed").startswith("T1 dirty worktree")


def test_handle_prep_done_warns_dan_when_the_merge_is_blocked(subject, sandbox, prep_done):
    prep_done.merge = (False, "z" * 300)
    subject.handle_prep_done("T1", {}, _entry(), {"dan_action": "a"})
    assert prep_done.sent[0] == (
        f"{WARN} T1: prep done but merge blocked \N{EM DASH} {'z' * 160}. "
        "Parking the action anyway; resolve the tree before /approve."
    )


def test_handle_prep_done_does_not_push_when_the_merge_failed(subject, sandbox, prep_done):
    prep_done.merge = (False, "conflict")
    subject.handle_prep_done("T1", {}, _entry(), {"dan_action": "a"})
    assert prep_done.runner.find("push", "origin", "main") == []


def test_handle_prep_done_removes_an_existing_worktree(subject, sandbox, prep_done):
    wt = sandbox.repo / "wt-T1"
    wt.mkdir()
    subject.handle_prep_done("T1", {}, _entry(worktree=str(wt)), {"dan_action": "a"})
    assert prep_done.removed == [wt]


def test_handle_prep_done_skips_a_worktree_that_is_already_gone(subject, sandbox, prep_done):
    subject.handle_prep_done("T1", {}, _entry(worktree=str(sandbox.repo / "absent")),
                             {"dan_action": "a"})
    assert prep_done.removed == []


def test_handle_prep_done_drops_the_session_from_in_flight(subject, sandbox, prep_done):
    subject.handle_prep_done("T1", {}, _entry(), {"dan_action": "a"})
    assert _get_state(subject, sandbox)["in_flight"] == []


def test_handle_prep_done_refreshes_the_dep_map_and_status(subject, sandbox, prep_done, monkeypatch):
    """R5–R7 rewired `run_dep_map`/`run_status` off subprocessing out to
    `gen_dependency_map.py`/`gen_upcoming.py` onto in-process calls into
    `maestro.docs.depmap`/`.upcoming` — the maestro subject leaves no extra subprocess
    argv behind for the refresh, so it is pinned by stubbing the two functions
    `handle_prep_done` calls and asserting on that instead. The legacy subject still
    shells out for both, so its assertion is untouched."""
    calls: list[str] = []
    monkeypatch.setattr(subject, "run_dep_map", lambda: calls.append("dep_map"))
    monkeypatch.setattr(subject, "run_status", lambda: calls.append("status"))
    subject.handle_prep_done("T1", {}, _entry(), {"dan_action": "a"})
    assert calls == ["dep_map", "status"]
    return
    subject.handle_prep_done("T1", {}, _entry(), {"dan_action": "a"})
    assert len(prep_done.runner.calls) >= 2


def test_handle_prep_done_journal_order_is_merge_then_park(subject, sandbox, prep_done):
    subject.handle_prep_done("T1", {}, _entry(), {"dan_action": "a"})
    assert _event_names(subject, sandbox) == ["prep_merged", "manual_action_parked"]


# =======================================================================================
# _graduate_manual_action — the shared graduation tail
# =======================================================================================


@pytest.fixture
def graduating(subject, sandbox, monkeypatch, notifier, runner, prep):
    _put_state(subject, sandbox, waiting_on_dan={
        "1": {"task_id": "T1", "session_id": "impl-T1-1", "kind": "manual-action"},
        "2": {"task_id": "T2", "session_id": "impl-T2-1", "kind": "manual-action"},
    })
    box = SimpleNamespace(sent=notifier, runner=runner, prep=prep,
                          marked=[], reports=[], task_def={"short_desc": "d"})
    monkeypatch.setattr(subject, "mark_roadmap_complete", lambda tid: box.marked.append(tid))
    monkeypatch.setattr(subject, "phase_report",
                        lambda *a: box.reports.append(a))
    monkeypatch.setattr(subject, "get_task_by_id", lambda tid: box.task_def)
    monkeypatch.setattr(subject, "run_dep_map", lambda: None)
    monkeypatch.setattr(subject, "run_status", lambda: None)
    return box


def test_graduate_manual_action_pops_only_its_own_waiting_entry(subject, sandbox, graduating):
    subject._graduate_manual_action("T1", "1", "impl-T1-1", [])
    assert sorted(_waiting(subject, sandbox)) == ["2"]


def test_graduate_manual_action_removes_the_sidecar(subject, sandbox, graduating):
    graduating.prep.set_action("T1", "do it")
    subject._graduate_manual_action("T1", "1", "impl-T1-1", [])
    assert graduating.prep.store == {}


def test_graduate_manual_action_drops_the_task_from_in_flight_in_place(subject, sandbox, graduating):
    in_flight = [{"task_id": "T1"}, {"task_id": "T2"}]
    original = in_flight
    subject._graduate_manual_action("T1", "1", "impl-T1-1", in_flight)
    assert in_flight is original
    assert in_flight == [{"task_id": "T2"}]


def test_graduate_manual_action_clears_the_paused_sentinel(subject, sandbox, graduating):
    ws = _make_workspace(subject, sandbox, "impl-T1-1")
    (ws / "PAUSED").write_text("1", encoding="utf-8")
    subject._graduate_manual_action("T1", "1", "impl-T1-1", [])
    assert not (ws / "PAUSED").exists()


def test_graduate_manual_action_tolerates_a_missing_paused_sentinel(subject, sandbox, graduating):
    subject._graduate_manual_action("T1", "1", "impl-T1-1", [])
    assert _detail(subject, sandbox, "task_complete") == "T1 (manual-action graduated)"


def test_graduate_manual_action_tolerates_an_empty_session_id(subject, sandbox, graduating):
    subject._graduate_manual_action("T1", "1", "", [])
    assert "task_complete" in _event_names(subject, sandbox)


def test_graduate_manual_action_marks_the_roadmap_complete_and_pushes(subject, sandbox, graduating):
    subject._graduate_manual_action("T1", "1", "impl-T1-1", [])
    assert graduating.marked == ["T1"]
    assert graduating.runner.find("push", "origin", "main")


def test_graduate_manual_action_congratulates_dan(subject, sandbox, graduating):
    subject._graduate_manual_action("T1", "1", "impl-T1-1", [])
    assert graduating.sent == [
        f"{CHECK} T1 verified and graduated. Nice \N{EM DASH} that's done."
    ]


def test_graduate_manual_action_reports_the_phase_with_an_empty_smoke(subject, sandbox, graduating):
    subject._graduate_manual_action("T1", "1", "impl-T1-1", [])
    assert graduating.reports == [("T1", graduating.task_def, {})]


def test_graduate_manual_action_graduates_an_unknown_dan_id_anyway(subject, sandbox, graduating):
    """FOUND_BUGS: the pop is unguarded, so a stale id still marks the task complete."""
    subject._graduate_manual_action("T1", "99", "impl-T1-1", [])
    assert sorted(_waiting(subject, sandbox)) == ["1", "2"]
    assert graduating.marked == ["T1"]
    assert "task_complete" in _event_names(subject, sandbox)


# =======================================================================================
# _park_awaiting_verification
# =======================================================================================


@pytest.fixture
def awaiting(subject, sandbox, notifier):
    _put_state(subject, sandbox, waiting_on_dan={
        "1": {"task_id": "T1", "session_id": "impl-T1-1", "kind": "manual-action",
              "summary": "do it", "reminded_at": "2026-01-01T00:00:00Z"},
    })
    return SimpleNamespace(sent=notifier)


def test_park_awaiting_verification_flips_the_kind(subject, sandbox, awaiting):
    subject._park_awaiting_verification("1", "T1", ["eval-row"])
    assert _waiting(subject, sandbox)["1"]["kind"] == "awaiting-verification"


def test_park_awaiting_verification_stamps_the_start_and_the_checks(subject, sandbox, awaiting):
    subject._park_awaiting_verification("1", "T1", ["eval-row", "latency"])
    entry = _waiting(subject, sandbox)["1"]
    assert entry["await_checks"] == ["eval-row", "latency"]
    assert entry["await_started_at"].endswith("Z")
    assert not subject._await_timed_out(entry["await_started_at"])


def test_park_awaiting_verification_clears_the_reminder_clock(subject, sandbox, awaiting):
    subject._park_awaiting_verification("1", "T1", ["eval-row"])
    assert "reminded_at" not in _waiting(subject, sandbox)["1"]


def test_park_awaiting_verification_keeps_the_rest_of_the_entry(subject, sandbox, awaiting):
    subject._park_awaiting_verification("1", "T1", ["eval-row"])
    entry = _waiting(subject, sandbox)["1"]
    assert entry["task_id"] == "T1" and entry["summary"] == "do it"


def test_park_awaiting_verification_journals_the_pending_checks(subject, sandbox, awaiting):
    subject._park_awaiting_verification("1", "T1", ["eval-row", "latency"])
    assert _detail(subject, sandbox, "await_verify_parked") == "T1 dan_id=1 eval-row,latency"


def test_park_awaiting_verification_tells_dan_no_action_is_needed(subject, sandbox, awaiting):
    subject._park_awaiting_verification("1", "T1", ["eval-row"])
    hours = subject.AWAIT_VERIFY_TIMEOUT_SEC // 3600
    assert awaiting.sent == [
        f"{CHECK} Approved *T1* \N{EM DASH} your action's done. Verification eval-row "
        "depends on an async job (e.g. the eval your action kicked off) that hasn't "
        "landed yet. I'll re-check every poll and finalize automatically when it passes "
        f"\N{EM DASH} no further action needed unless it fails or times out (after {hours} h)."
    ]


def test_park_awaiting_verification_of_an_unknown_id_is_a_silent_no_op(subject, sandbox, awaiting):
    """FOUND_BUGS: no journal, no ping — a lost entry loses the transition entirely."""
    subject._park_awaiting_verification("99", "T1", ["eval-row"])
    assert awaiting.sent == []
    assert _event_names(subject, sandbox) == []
    assert _waiting(subject, sandbox)["1"]["kind"] == "manual-action"


# =======================================================================================
# _surface_await_failure
# =======================================================================================


@pytest.fixture
def surfacing(subject, sandbox, notifier):
    _put_state(subject, sandbox, waiting_on_dan={
        "1": {"task_id": "T1", "session_id": "impl-T1-1", "kind": "awaiting-verification",
              "await_started_at": "2026-01-01T00:00:00+00:00",
              "await_checks": ["eval-row"], "reminded_at": "2026-01-01T00:00:00Z"},
    })
    return SimpleNamespace(sent=notifier,
                           parked={"task_id": "T1", "session_id": "impl-T1-1"})


def test_surface_await_failure_reverts_the_entry_to_a_manual_action(subject, sandbox, surfacing):
    subject._surface_await_failure("1", surfacing.parked, ["eval-row"], reason="gate")
    assert _waiting(subject, sandbox)["1"]["kind"] == "manual-action"


def test_surface_await_failure_records_why(subject, sandbox, surfacing):
    subject._surface_await_failure("1", surfacing.parked, ["eval-row"], reason="timeout")
    assert _waiting(subject, sandbox)["1"]["await_failed_reason"] == "timeout"


def test_surface_await_failure_clears_the_reminder_clock(subject, sandbox, surfacing):
    subject._surface_await_failure("1", surfacing.parked, ["eval-row"], reason="gate")
    assert "reminded_at" not in _waiting(subject, sandbox)["1"]


def test_surface_await_failure_journals_the_reason_and_the_checks(subject, sandbox, surfacing):
    subject._surface_await_failure("1", surfacing.parked, ["eval-row", "lat"], reason="gate")
    assert _detail(subject, sandbox, "await_verify_surfaced") == "T1 reason=gate eval-row,lat"


def test_surface_await_failure_gate_wording_names_the_threshold(subject, sandbox, surfacing):
    subject._surface_await_failure("1", surfacing.parked, ["eval-row"], reason="gate")
    assert surfacing.sent == [
        f"{WARN} *T1* (ID 1): awaited verification landed but FAILED (eval-row) "
        "\N{EM DASH} present, below threshold, not still-running. Investigate, then "
        "/approve 1 to retry or /reject 1."
    ]


def test_surface_await_failure_timeout_wording_names_the_bound(subject, sandbox, surfacing):
    subject._surface_await_failure("1", surfacing.parked, ["eval-row"], reason="timeout")
    hours = subject.AWAIT_VERIFY_TIMEOUT_SEC // 3600
    assert surfacing.sent == [
        f"{ALARM} *T1* (ID 1): awaited verification (eval-row) never landed within "
        f"{hours} h. Check the eval/job, then /approve 1 to retry or /reject 1."
    ]


def test_surface_await_failure_treats_any_unknown_reason_as_a_gate_failure(subject, sandbox, surfacing):
    subject._surface_await_failure("1", surfacing.parked, ["eval-row"], reason="whatever")
    assert surfacing.sent[0].startswith(f"{WARN} *T1*")
    assert _waiting(subject, sandbox)["1"]["await_failed_reason"] == "whatever"


def test_surface_await_failure_of_an_unknown_id_is_a_silent_no_op(subject, sandbox, surfacing):
    subject._surface_await_failure("99", surfacing.parked, ["eval-row"], reason="gate")
    assert surfacing.sent == []
    assert _event_names(subject, sandbox) == []


def test_surface_await_failure_names_the_task_from_the_snapshot_not_the_state(subject, sandbox, surfacing):
    """The journal/ping task id comes from the caller's `parked` dict, not from state."""
    subject._surface_await_failure("1", {"task_id": "SNAPSHOT"}, ["eval-row"], reason="gate")
    assert _detail(subject, sandbox, "await_verify_surfaced").startswith("SNAPSHOT ")
    assert _waiting(subject, sandbox)["1"]["task_id"] == "T1"


def test_surface_await_failure_is_idempotent_by_kind(subject, sandbox, surfacing):
    subject._surface_await_failure("1", surfacing.parked, ["eval-row"], reason="gate")
    subject._surface_await_failure("1", surfacing.parked, ["eval-row"], reason="gate")
    assert _event_names(subject, sandbox).count("await_verify_surfaced") == 2
    assert _waiting(subject, sandbox)["1"]["kind"] == "manual-action"


# =======================================================================================
# _finalize_manual_action — /approve of a manual-action item
# =======================================================================================


@pytest.fixture
def finalizing(subject, sandbox, monkeypatch, notifier, runner, prep):
    _put_state(subject, sandbox, waiting_on_dan={
        "1": {"task_id": "T1", "session_id": "impl-T1-1", "kind": "manual-action"},
    })
    box = SimpleNamespace(
        sent=notifier, runner=runner, prep=prep,
        gate=(True, "all checks pass"),
        classified=([], []),
        task_def={},
        graduated=[],
        parked=None,
    )
    monkeypatch.setattr(subject, "run_verification_gate",
                        lambda *a, **kw: box.gate)
    monkeypatch.setattr(subject, "_classify_verifications", lambda verifs, cwd: box.classified)
    monkeypatch.setattr(subject, "get_task_by_id", lambda tid: box.task_def)
    monkeypatch.setattr(subject, "_graduate_manual_action",
                        lambda *a: box.graduated.append(a))
    box.parked = {"task_id": "T1", "session_id": "impl-T1-1", "kind": "manual-action"}
    return box


def test_finalize_manual_action_announces_the_finalize(subject, sandbox, finalizing):
    subject._finalize_manual_action("1", finalizing.parked, [])
    assert finalizing.sent[0] == \
        f"{CHECK} Finalizing T1 \N{EM DASH} running post-action + verification gate\N{HORIZONTAL ELLIPSIS}"


def test_finalize_manual_action_journals_the_approval_first(subject, sandbox, finalizing):
    subject._finalize_manual_action("1", finalizing.parked, [])
    assert _detail(subject, sandbox, "manual_action_approved") == "T1 dan_id=1"


def test_finalize_manual_action_graduates_when_the_gate_passes(subject, sandbox, finalizing):
    in_flight = []
    subject._finalize_manual_action("1", finalizing.parked, in_flight)
    assert finalizing.graduated == [("T1", "1", "impl-T1-1", in_flight)]


def test_finalize_manual_action_runs_the_sidecar_post_action_cmd(subject, sandbox, finalizing):
    finalizing.prep.set_action("T1", "do it", post_action_cmd="python3 import.py")
    subject._finalize_manual_action("1", finalizing.parked, [])
    assert finalizing.runner.shell_cmds
    cmd = finalizing.runner.shell_cmds[0]
    assert cmd.endswith(" import.py") and cmd != "python3 import.py"
    assert finalizing.runner.calls[0].kwargs["shell"] is True
    assert finalizing.runner.calls[0].kwargs["timeout"] == 1800


def test_finalize_manual_action_falls_back_to_the_task_post_action_cmd(subject, sandbox, finalizing):
    finalizing.task_def = {"post_action_cmd": "bash import.sh"}
    subject._finalize_manual_action("1", finalizing.parked, [])
    assert finalizing.runner.shell_cmds == ["bash import.sh"]


def test_finalize_manual_action_announces_the_post_action_cmd(subject, sandbox, finalizing):
    finalizing.task_def = {"post_action_cmd": "bash import.sh"}
    subject._finalize_manual_action("1", finalizing.parked, [])
    assert finalizing.sent[1] == f"{GEAR} T1: running finalize \N{EM DASH} `bash import.sh`"


def test_finalize_manual_action_skips_the_gate_when_the_post_action_cmd_fails(subject, sandbox, finalizing):
    finalizing.task_def = {"post_action_cmd": "bash import.sh"}
    finalizing.runner.result_for = lambda call: (1, "", "no such file")
    subject._finalize_manual_action("1", finalizing.parked, [])
    assert finalizing.graduated == []
    assert _detail(subject, sandbox, "manual_action_postcmd_failed") == "T1 no such file"
    assert finalizing.sent[-1] == (
        f"{WARN} T1: finalize cmd failed \N{EM DASH} no such file\n"
        "Item stays parked (ID 1); fix and /approve again."
    )


def test_finalize_manual_action_leaves_the_item_parked_when_the_post_cmd_fails(subject, sandbox, finalizing):
    finalizing.task_def = {"post_action_cmd": "bash import.sh"}
    finalizing.runner.result_for = lambda call: (2, "stdout noise", "")
    subject._finalize_manual_action("1", finalizing.parked, [])
    assert _waiting(subject, sandbox)["1"]["kind"] == "manual-action"
    assert "stdout noise" in finalizing.sent[-1]


def test_finalize_manual_action_surfaces_a_hard_gate_failure(subject, sandbox, finalizing):
    finalizing.gate = (False, "check `eval-row` returned 0.31, expected 0.40")
    finalizing.classified = ([], ["eval-row"])
    subject._finalize_manual_action("1", finalizing.parked, [])
    assert finalizing.graduated == []
    assert _detail(subject, sandbox, "manual_action_gate_failed").startswith("T1 check `eval-row`")
    assert finalizing.sent[-1] == (
        f"{WARN} T1: verification gate FAILED after your action \N{EM DASH}\n"
        "check `eval-row` returned 0.31, expected 0.40\n"
        "Item stays parked (ID 1). Fix and /approve again."
    )


def test_finalize_manual_action_parks_as_awaiting_when_only_awaits_are_pending(subject, sandbox, finalizing):
    finalizing.gate = (False, "eval-row absent")
    finalizing.classified = (["eval-row"], [])
    subject._finalize_manual_action("1", finalizing.parked, [])
    assert finalizing.graduated == []
    assert _waiting(subject, sandbox)["1"]["kind"] == "awaiting-verification"
    assert _detail(subject, sandbox, "await_verify_parked") == "T1 dan_id=1 eval-row"


def test_finalize_manual_action_treats_a_mixed_classification_as_a_hard_failure(subject, sandbox, finalizing):
    finalizing.gate = (False, "mixed")
    finalizing.classified = (["eval-row"], ["syntax"])
    subject._finalize_manual_action("1", finalizing.parked, [])
    assert _waiting(subject, sandbox)["1"]["kind"] == "manual-action"
    assert "manual_action_gate_failed" in _event_names(subject, sandbox)


def test_finalize_manual_action_treats_an_empty_classification_as_a_hard_failure(subject, sandbox, finalizing):
    finalizing.gate = (False, "gate said no but nothing classified")
    finalizing.classified = ([], [])
    subject._finalize_manual_action("1", finalizing.parked, [])
    assert "manual_action_gate_failed" in _event_names(subject, sandbox)


def test_finalize_manual_action_truncates_the_gate_message(subject, sandbox, finalizing):
    finalizing.gate = (False, "g" * 500)
    finalizing.classified = ([], ["x"])
    subject._finalize_manual_action("1", finalizing.parked, [])
    assert _detail(subject, sandbox, "manual_action_gate_failed") == f"T1 {'g' * 160}"
    assert "g" * 300 in finalizing.sent[-1]
    assert "g" * 301 not in finalizing.sent[-1]


def test_finalize_manual_action_gate_runs_auto_only_against_the_repo(subject, sandbox, finalizing, monkeypatch):
    seen = []
    monkeypatch.setattr(subject, "run_verification_gate",
                        lambda *a, **kw: (seen.append((a, kw)), finalizing.gate)[1])
    subject._finalize_manual_action("1", finalizing.parked, [])
    args, kwargs = seen[0]
    assert args == ("T1", subject.REPO, subject.REPO)
    assert kwargs == {"auto_only": True}


def test_finalize_manual_action_does_not_catch_a_post_cmd_timeout(subject, sandbox, finalizing, monkeypatch):
    """FOUND_BUGS: a hung finalize command escapes after Dan was told finalizing had begun."""
    finalizing.task_def = {"post_action_cmd": "bash slow.sh"}

    def boom(*a, **kw):
        raise subprocess.TimeoutExpired("bash slow.sh", 1800)

    monkeypatch.setattr(finalizing.runner, "run", boom)
    with pytest.raises(subprocess.TimeoutExpired):
        subject._finalize_manual_action("1", finalizing.parked, [])
    assert finalizing.sent[0].startswith(f"{CHECK} Finalizing T1")
    assert _waiting(subject, sandbox)["1"]["kind"] == "manual-action"


def test_finalize_manual_action_needs_a_task_id_on_the_parked_entry(subject, sandbox, finalizing):
    with pytest.raises(KeyError):
        subject._finalize_manual_action("1", {"session_id": "impl-T1-1"}, [])


# =======================================================================================
# handle_incomplete — the third loop outcome, alongside DONE and FAILED
# =======================================================================================


@pytest.fixture
def incomplete(subject, sandbox, monkeypatch, notifier, runner):
    """`handle_incomplete` with the merge, the worktree removal and `park_failed` stubbed."""
    _put_state(subject, sandbox, in_flight=[{"session_id": "impl-T1-1", "task_id": "T1"}])
    box = SimpleNamespace(
        sent=notifier, runner=runner,
        merge="ok", escalation=(True, "approved", "ok"),
        parked=[], removed=[],
    )
    monkeypatch.setattr(subject, "_merge_data_only", lambda entry: box.merge)
    monkeypatch.setattr(subject, "_resumable_code_change_escalation",
                        lambda entry, task_id: box.escalation)
    monkeypatch.setattr(subject, "remove_worktree", lambda wt: box.removed.append(Path(wt)))
    monkeypatch.setattr(subject, "park_failed",
                        lambda task_id, reason: box.parked.append((task_id, reason)))
    return box


def _wt_entry(sandbox, task_id="T1", session_id="impl-T1-1", branch="t1-work") -> dict:
    wt = sandbox.repo / f"wt-{task_id}"
    wt.mkdir(exist_ok=True)
    return {"task_id": task_id, "session_id": session_id, "branch": branch, "worktree": str(wt)}


def _resume_state(subject, sandbox, task_id="T1") -> dict:
    return _get_state(subject, sandbox).get("resume_state", {}).get(task_id, {})


# --- the happy path: forward progress ------------------------------------------------------


def test_handle_incomplete_records_the_new_progress(subject, sandbox, incomplete):
    subject.handle_incomplete("T1", {}, _wt_entry(sandbox), "PROGRESS=40/100")
    rs = _resume_state(subject, sandbox)
    assert rs["last_count"] == 40 and rs["total"] == 100
    assert rs["attempts"] == 1 and rs["stalls"] == 0


def test_handle_incomplete_merges_the_partial_data_only_work(subject, sandbox, incomplete, monkeypatch):
    """The in-flight entry itself is handed to `_merge_data_only`, and its status is journalled."""
    seen = []

    def _merge(entry):
        seen.append(entry)
        return incomplete.merge

    monkeypatch.setattr(subject, "_merge_data_only", _merge)
    entry = _wt_entry(sandbox)
    subject.handle_incomplete("T1", {}, entry, "PROGRESS=40/100")
    assert seen == [entry]
    cooldown_h = subject.RESUME_COOLDOWN_H_DEFAULT
    assert _detail(subject, sandbox, "task_incomplete") == \
        f"T1 40/100 (40%) ok attempt=1 cooldown={cooldown_h}h"


def test_handle_incomplete_uses_the_explicit_progress_marker_over_a_bare_ratio(subject, sandbox, incomplete):
    subject.handle_incomplete("T1", {}, _wt_entry(sandbox), "saw 3/9 files; PROGRESS=40/100")
    assert _resume_state(subject, sandbox)["last_count"] == 40


def test_handle_incomplete_falls_back_to_the_first_bare_ratio(subject, sandbox, incomplete):
    subject.handle_incomplete("T1", {}, _wt_entry(sandbox), "processed 7 / 20 posts")
    rs = _resume_state(subject, sandbox)
    assert (rs["last_count"], rs["total"]) == (7, 20)


def test_handle_incomplete_sets_the_default_cooldown(subject, sandbox, incomplete):
    subject.handle_incomplete("T1", {}, _wt_entry(sandbox), "PROGRESS=40/100")
    default_h = subject.RESUME_COOLDOWN_H_DEFAULT
    resume_after = datetime.fromisoformat(_resume_state(subject, sandbox)["resume_after"])
    expected = datetime.now(tz=timezone.utc) + timedelta(hours=default_h)
    assert abs((resume_after - expected).total_seconds()) < 120


def test_handle_incomplete_honours_a_per_task_cooldown(subject, sandbox, incomplete):
    subject.handle_incomplete("T1", {"resume_cooldown_h": 3}, _wt_entry(sandbox), "PROGRESS=40/100")
    resume_after = datetime.fromisoformat(_resume_state(subject, sandbox)["resume_after"])
    expected = datetime.now(tz=timezone.utc) + timedelta(hours=3)
    assert abs((resume_after - expected).total_seconds()) < 120
    assert "cooldown=3h" in _detail(subject, sandbox, "task_incomplete")


def test_handle_incomplete_tells_dan_when_the_work_resumes(subject, sandbox, incomplete):
    subject.handle_incomplete("T1", {"resume_cooldown_h": 6}, _wt_entry(sandbox), "PROGRESS=40/100")
    assert incomplete.sent == [
        f"{HOURGLASS} T1 incomplete: 40/100 posts (40%) \N{EM DASH} "
        "progress saved, resuming in ~6h."
    ]


def test_handle_incomplete_journals_against_the_session(subject, sandbox, incomplete):
    record = None
    subject.handle_incomplete("T1", {}, _wt_entry(sandbox), "PROGRESS=1/2")
    record = _record(subject, sandbox, "task_incomplete")
    assert record["session_id"] == "impl-T1-1"


def test_handle_incomplete_removes_the_worktree_and_the_in_flight_entry(subject, sandbox, incomplete):
    entry = _wt_entry(sandbox)
    subject.handle_incomplete("T1", {}, entry, "PROGRESS=40/100")
    assert incomplete.removed == [Path(entry["worktree"])]
    assert _get_state(subject, sandbox)["in_flight"] == []


def test_handle_incomplete_resets_the_stall_counter_on_progress(subject, sandbox, incomplete):
    _put_state(subject, sandbox, in_flight=[],
               resume_state={"T1": {"last_count": 10, "total": 100, "attempts": 2, "stalls": 1}})
    subject.handle_incomplete("T1", {}, _wt_entry(sandbox), "PROGRESS=40/100")
    rs = _resume_state(subject, sandbox)
    assert rs["stalls"] == 0 and rs["attempts"] == 3


def test_handle_incomplete_counts_zero_of_zero_as_forward_progress(subject, sandbox, incomplete):
    subject.handle_incomplete("T1", {}, _wt_entry(sandbox), "PROGRESS=0/0")
    assert "task_incomplete" in _event_names(subject, sandbox)
    assert "(0%)" in _detail(subject, sandbox, "task_incomplete")


def test_handle_incomplete_leaves_other_tasks_resume_state_alone(subject, sandbox, incomplete):
    _put_state(subject, sandbox, in_flight=[], resume_state={"T2": {"last_count": 5}})
    subject.handle_incomplete("T1", {}, _wt_entry(sandbox), "PROGRESS=40/100")
    assert _get_state(subject, sandbox)["resume_state"]["T2"] == {"last_count": 5}


# --- stalls -------------------------------------------------------------------------------


def test_handle_incomplete_counts_a_stall_when_nothing_advanced(subject, sandbox, incomplete):
    _put_state(subject, sandbox, in_flight=[],
               resume_state={"T1": {"last_count": 40, "total": 100, "attempts": 1, "stalls": 0}})
    subject.handle_incomplete("T1", {}, _wt_entry(sandbox), "PROGRESS=40/100")
    assert _resume_state(subject, sandbox)["stalls"] == 1
    assert "no-op (no new progress)" in _detail(subject, sandbox, "task_incomplete")


def test_handle_incomplete_does_not_merge_on_a_stall(subject, sandbox, incomplete, monkeypatch):
    merged = []
    monkeypatch.setattr(subject, "_merge_data_only",
                        lambda entry: (merged.append(entry), "ok")[1])
    _put_state(subject, sandbox, in_flight=[],
               resume_state={"T1": {"last_count": 40, "total": 100}})
    subject.handle_incomplete("T1", {}, _wt_entry(sandbox), "PROGRESS=40/100")
    assert merged == []


def test_handle_incomplete_parks_once_the_stall_budget_is_spent(subject, sandbox, incomplete):
    stalls = subject.MAX_RESUME_STALLS - 1
    _put_state(subject, sandbox, in_flight=[],
               resume_state={"T1": {"last_count": 40, "total": 100, "stalls": stalls}})
    subject.handle_incomplete("T1", {}, _wt_entry(sandbox), "PROGRESS=40/100")
    assert incomplete.parked == [
        ("T1", f"no progress for {subject.MAX_RESUME_STALLS} sittings at 40/100")
    ]


def test_handle_incomplete_going_backwards_counts_as_a_stall(subject, sandbox, incomplete):
    _put_state(subject, sandbox, in_flight=[],
               resume_state={"T1": {"last_count": 40, "total": 100}})
    subject.handle_incomplete("T1", {}, _wt_entry(sandbox), "PROGRESS=10/100")
    assert _resume_state(subject, sandbox)["stalls"] == 1
    assert _resume_state(subject, sandbox)["last_count"] == 10


# --- the park path -------------------------------------------------------------------------


def test_handle_incomplete_parks_when_the_gate_message_is_unparseable(subject, sandbox, incomplete):
    subject.handle_incomplete("T1", {}, _wt_entry(sandbox), "the gate said nothing useful")
    assert incomplete.parked == [
        ("T1", "gate progress unparseable: the gate said nothing useful")
    ]


def test_handle_incomplete_park_truncates_the_gate_message(subject, sandbox, incomplete):
    subject.handle_incomplete("T1", {}, _wt_entry(sandbox), "x" * 400)
    assert incomplete.parked[0][1] == f"gate progress unparseable: {'x' * 160}"


def test_handle_incomplete_park_clears_the_resume_state_and_the_worktree(subject, sandbox, incomplete):
    _put_state(subject, sandbox, in_flight=[{"session_id": "impl-T1-1"}],
               resume_state={"T1": {"last_count": 3}, "T2": {"last_count": 9}})
    entry = _wt_entry(sandbox)
    subject.handle_incomplete("T1", {}, entry, "unparseable")
    state = _get_state(subject, sandbox)
    assert "T1" not in state["resume_state"] and state["resume_state"]["T2"] == {"last_count": 9}
    assert state["in_flight"] == []
    assert incomplete.removed == [Path(entry["worktree"])]


def test_handle_incomplete_park_pings_dan_and_journals(subject, sandbox, incomplete):
    subject.handle_incomplete("T1", {}, _wt_entry(sandbox), "unparseable")
    assert incomplete.sent == [
        f"{WARN} T1 (resumable): gate progress unparseable: unparseable "
        "\N{EM DASH} parked for Dan."
    ]
    record = _record(subject, sandbox, "resume_parked")
    assert record["detail"] == "T1 gate progress unparseable: unparseable"
    assert record["session_id"] == "impl-T1-1"


def test_handle_incomplete_park_truncates_the_telegram_reason_at_one_forty(subject, sandbox, incomplete):
    subject.handle_incomplete("T1", {}, _wt_entry(sandbox), "x" * 400)
    body = incomplete.sent[0].split(": ", 1)[1]
    assert body.startswith("gate progress unparseable: " + "x" * 113)
    assert "x" * 114 not in body


def test_handle_incomplete_parks_at_the_attempt_cap(subject, sandbox, incomplete):
    cap = subject.MAX_RESUME_ATTEMPTS
    _put_state(subject, sandbox, in_flight=[],
               resume_state={"T1": {"last_count": 10, "total": 100, "attempts": cap}})
    subject.handle_incomplete("T1", {}, _wt_entry(sandbox), "PROGRESS=90/100")
    assert incomplete.parked == [
        ("T1", f"resume attempt cap reached at 90/100 (attempts={cap})")
    ]


def test_handle_incomplete_attempt_cap_counts_productive_sittings_too(subject, sandbox, incomplete):
    """FOUND_BUGS: `attempts` is incremented on every sitting, so steady progress still parks."""
    cap = subject.MAX_RESUME_ATTEMPTS
    _put_state(subject, sandbox, in_flight=[],
               resume_state={"T1": {"last_count": 10, "total": 100,
                                    "attempts": cap - 1, "stalls": 0}})
    subject.handle_incomplete("T1", {}, _wt_entry(sandbox), "PROGRESS=50/100")
    assert incomplete.parked == []
    assert _resume_state(subject, sandbox)["attempts"] == cap
    subject.handle_incomplete("T1", {}, _wt_entry(sandbox), "PROGRESS=90/100")
    assert incomplete.parked and "attempt cap reached at 90/100" in incomplete.parked[0][1]


def test_handle_incomplete_parks_on_a_failed_data_merge(subject, sandbox, incomplete):
    incomplete.merge = "conflict"
    subject.handle_incomplete("T1", {}, _wt_entry(sandbox), "PROGRESS=40/100")
    assert incomplete.parked == [("T1", "data merge conflict on branch t1-work")]


def test_handle_incomplete_accepts_an_empty_data_merge(subject, sandbox, incomplete):
    incomplete.merge = "empty"
    subject.handle_incomplete("T1", {}, _wt_entry(sandbox), "PROGRESS=40/100")
    assert incomplete.parked == []
    assert "empty attempt=1" in _detail(subject, sandbox, "task_incomplete")


def test_handle_incomplete_escalates_an_out_of_allowlist_code_change(subject, sandbox, incomplete):
    incomplete.merge = "code_change"
    incomplete.escalation = (True, "docs only", "ok")
    subject.handle_incomplete("T1", {}, _wt_entry(sandbox), "PROGRESS=40/100")
    assert incomplete.parked == []
    assert "(40%) ok attempt=1" in _detail(subject, sandbox, "task_incomplete")


def test_handle_incomplete_parks_a_rejected_code_change(subject, sandbox, incomplete):
    incomplete.merge = "code_change"
    incomplete.escalation = (False, "touches the orchestrator", "blocked")
    subject.handle_incomplete("T1", {}, _wt_entry(sandbox), "PROGRESS=40/100")
    assert incomplete.parked == [("T1", "code_change not approved: touches the orchestrator")]


def test_handle_incomplete_park_calls_park_failed_before_cleaning_up(subject, sandbox, incomplete, monkeypatch):
    """`park_failed` runs first, then the worktree removal, then Dan's ping."""
    order: list[str] = []
    incomplete.merge = "conflict"
    monkeypatch.setattr(subject, "park_failed",
                        lambda task_id, reason: order.append("park_failed"))
    monkeypatch.setattr(subject, "remove_worktree", lambda wt: order.append("remove_worktree"))
    monkeypatch.setattr(subject, "notify_telegram", lambda msg: order.append("notify_telegram"))
    subject.handle_incomplete("T1", {}, _wt_entry(sandbox), "PROGRESS=40/100")
    assert order == ["park_failed", "remove_worktree", "notify_telegram"]
    assert _event_names(subject, sandbox)[-1] == "resume_parked"


def test_handle_incomplete_park_survives_a_state_without_resume_state(subject, sandbox, incomplete):
    """Parking a task whose state has no `resume_state` is a no-op: the key stays absent."""
    _put_state(subject, sandbox, in_flight=[])
    subject.handle_incomplete("T1", {}, _wt_entry(sandbox), "unparseable")
    state = _get_state(subject, sandbox)
    assert "resume_state" not in state
    assert incomplete.parked == [("T1", "gate progress unparseable: unparseable")]
