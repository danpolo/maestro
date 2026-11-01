"""`kind: dan` verifications: checks only the operator can confirm, over Telegram.

A task's ROADMAP `verifications:` may declare a check that no command can discharge —
"the dashboard renders", "the phone gets the push". Under the graph runner such a check is
a required criterion discharged only by a durable Dan approval bound to the snapshot the
gate checked. These tests pin each layer on its own; the whole flow runs end to end in
`test_end_to_end.py`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from maestro import gates, validation

DAN_ENTRY = {
    "id": "V2",
    "kind": "dan",
    "check": "the page renders the new banner",
    "instructions": "cd {worktree} && make serve, then open http://localhost:8000",
}
AUTO_ENTRY = {"id": "V1", "kind": "auto", "cmd": "true"}


# ── the spec ────────────────────────────────────────────────────────────────────────


def test_a_dan_entry_is_a_required_judgment_with_no_command():
    spec = validation.VerificationSpec.from_roadmap(DAN_ENTRY)

    assert spec.handler_ref == validation.DAN_CHECK_HANDLER
    assert spec.required
    assert spec.argv_or_adapter_ref == ()
    assert spec.acceptance_criterion_ids == ("V2",)
    assert validation.is_dan_check(spec)
    assert not validation.is_dan_check(validation.VerificationSpec.from_roadmap(AUTO_ENTRY))


def test_a_manual_entry_is_unchanged():
    spec = validation.VerificationSpec.from_roadmap({"id": "M", "kind": "manual"})

    assert spec.handler_ref == "maestro.validation.reviewer_check"
    assert not validation.is_dan_check(spec)


def test_the_check_runner_never_executes_a_dan_check(tmp_path):
    ran = []

    def runner(*args, **kwargs):  # pragma: no cover - must not be reached
        ran.append(args)
        raise AssertionError("a dan check was executed")

    checker = validation.CheckRunner(registry=validation.EvidenceRegistry(), runner=runner)
    specs = [validation.VerificationSpec.from_roadmap(DAN_ENTRY)]

    assert checker.evaluate(specs, tmp_path, attempt_id="att_1", snapshot="s") == []
    assert ran == []


def _passing(check_id: str, snapshot: str) -> validation.VerificationEvidence:
    return validation.VerificationEvidence(
        evidence_id=f"ev_{check_id}",
        check_id=check_id,
        attempt_id="att_1",
        outcome="passed",
        source_snapshot_id=snapshot,
        command_hash="c",
        input_fingerprint="i",
        environment_fingerprint="e",
        exit_status=0,
    )


def _decide(dispositions=(), snapshot="abc"):
    specs = [
        validation.VerificationSpec.from_roadmap(AUTO_ENTRY),
        validation.VerificationSpec.from_roadmap(DAN_ENTRY),
    ]
    return validation.decide(
        task_id="T", run_id="r", specs=specs, evidence=[_passing("V1", snapshot)],
        required_criterion_ids=[s.check_id for s in specs],
        review_dispositions=dispositions,
        integrated_source_snapshot=snapshot, expected_snapshot=snapshot,
    )


def test_decide_refuses_a_dan_criterion_with_no_approval():
    decision = _decide()

    assert not decision.accepted
    assert "'V2'" in decision.reason


def test_decide_accepts_a_dan_criterion_discharged_by_dans_approval():
    approval = validation.review_disposition(
        "V2", "approved", reviewer="dan", subject_sha="abc"
    )

    assert _decide([approval]).accepted


def test_decide_refuses_a_dan_rejection():
    rejection = validation.review_disposition(
        "V2", "rejected", findings=["banner missing"], reviewer="dan", subject_sha="abc"
    )

    decision = _decide([rejection])

    assert not decision.accepted
    assert "'V2'" in decision.reason


# ── the gate decides on the automatic checks only ───────────────────────────────────


def _roadmap(tmp_path: Path, verifications: list) -> Path:
    import yaml

    path = tmp_path / "ROADMAP.md"
    task = {"id": "T", "title": "t", "verifications": verifications}
    path.write_text("```yaml\n" + yaml.safe_dump(task) + "```\n", encoding="utf-8")
    return path


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "tree"
    repo.mkdir()
    for args in (
        ["init", "-q"], ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "t"], ["commit", "-q", "--allow-empty", "-m", "seed"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    return repo


def test_the_gate_passes_on_its_automatic_checks_and_leaves_dan_checks_to_dan(tmp_path):
    roadmap = _roadmap(tmp_path, [AUTO_ENTRY, DAN_ENTRY])

    decision, evidence = gates.run_authoritative_gate(
        "T", _repo(tmp_path), attempt_id="att_1", roadmap=roadmap
    )

    assert decision.accepted, decision.reason
    assert [record.check_id for record in evidence] == ["V1"]


def test_a_gate_with_only_dan_checks_passes_and_says_they_are_deferred(tmp_path):
    roadmap = _roadmap(tmp_path, [DAN_ENTRY])

    decision, evidence = gates.run_authoritative_gate(
        "T", _repo(tmp_path), attempt_id="att_1", roadmap=roadmap
    )

    assert decision.accepted, decision.reason
    assert evidence == []
    assert "deferred" in decision.reason


def test_a_manual_check_still_fails_the_gate(tmp_path):
    roadmap = _roadmap(tmp_path, [AUTO_ENTRY, {"id": "M", "kind": "manual"}])

    decision, _ = gates.run_authoritative_gate(
        "T", _repo(tmp_path), attempt_id="att_1", roadmap=roadmap
    )

    assert not decision.accepted


# ── the compiled graph gains a Dan-confirmation step between gate and merge ─────────

from maestro.taskgraph import normalize_spec  # noqa: E402
from maestro.workflows import compiler, failures  # noqa: E402


def _spec(verifications, **extra):
    return normalize_spec({"id": "T", "title": "the task", "verifications": verifications, **extra})


def _edges(revision):
    return {(e.source_node_id, e.target_node_id, e.guard_predicate) for e in revision.edges}


@pytest.mark.parametrize("escalation", [False, True])
def test_an_ordinary_task_with_a_dan_check_waits_after_merge_before_acceptance(escalation):
    revision = compiler.compile_workflow(
        _spec([AUTO_ENTRY, DAN_ENTRY]), workflow_id="wf_T", escalation=escalation
    )

    node = revision.node('operator_check')
    assert node.handler_ref == 'maestro.handlers.operator_check'
    assert node.permissions_ref == "controller_only"
    assert node.params["dan_checks"] == [
        {"id": "V2", "check": DAN_ENTRY["check"], "instructions": DAN_ENTRY["instructions"]}
    ]
    edges = _edges(revision)
    assert ("proof_review", "merge", "on_success") in edges
    assert ("merge", "operator_check", "on_success") in edges
    assert ("operator_check", "accept", "on_success") in edges
    # A deployed failure queues a separate repair; the original merge is not replayed.
    assert failures.retry_cone(revision, "operator_check") == ()


def test_a_task_without_a_dan_check_compiles_exactly_as_before():
    with_auto = compiler.compile_workflow(_spec([AUTO_ENTRY]), workflow_id="wf_T")
    without = compiler.compile_workflow(_spec([]), workflow_id="wf_T")

    assert "dan_confirm" not in with_auto.node_ids
    assert with_auto.dag_hash == without.dag_hash


def test_a_lane_without_merge_confirms_before_accept():
    revision = compiler.compile_workflow(
        _spec([DAN_ENTRY], dispatch="async-job", launch_cmd="true"), workflow_id="wf_T"
    )

    edges = _edges(revision)
    assert ("gate", "operator_check", "on_success") in edges
    assert ("operator_check", "accept", "on_success") in edges


# ── the Telegram channel: one request per task per snapshot, answered by button or text ──

import json  # noqa: E402

from maestro.hitl import telegram, verify  # noqa: E402

CHECKS = [{"id": "V2", "check": DAN_ENTRY["check"], "instructions": DAN_ENTRY["instructions"]}]
SHA = "0123456789abcdef0123456789abcdef01234567"
SHA2 = "fedcba9876543210fedcba9876543210fedcba98"


class _Bot:
    """A fake Telegram transport: records what would have been sent, never the network."""

    def __init__(self):
        self.sent: list[tuple[str, dict | None]] = []

    def __call__(self, text, markup=None):
        self.sent.append((text, markup))
        return 1000 + len(self.sent)


@pytest.fixture
def channel(tmp_path, monkeypatch):
    monkeypatch.setattr(telegram, "QUESTIONS_DIR", tmp_path / "questions")
    monkeypatch.setattr(telegram, "append_journal", lambda *a, **k: None)
    monkeypatch.setattr(telegram, "notify_telegram", lambda *a, **k: None)
    monkeypatch.setattr(telegram, "_tg_api", lambda *a, **k: None)
    state: dict = {}
    journal: list = []
    bot = _Bot()
    chan = verify.DanVerificationChannel(
        send=bot,
        configured=lambda: True,
        journal=lambda event, detail: journal.append((event, detail)),
        read_state=lambda: json.loads(json.dumps(state)),
        write_state=lambda new: (state.clear(), state.update(new)),
    )
    chan.bot, chan.state, chan.journaled = bot, state, journal
    return chan


def _poll(chan, snapshot=SHA, worktree="/work/T"):
    return chan.poll(
        task_id="T", title="the task", run_id="run_1", snapshot=snapshot,
        worktree=worktree, checks=CHECKS,
    )


def _tap(req_id: str, index: int) -> None:
    telegram._handle_danreq_callback(
        {"id": "cq", "data": f"danreq:{req_id}:{index}", "message": {}}
    )


def test_the_first_poll_sends_one_request_with_the_checks_and_two_buttons(channel, monkeypatch):
    monkeypatch.setattr(telegram, "_tg_api", lambda *a, **k: None)

    assert _poll(channel) is None
    assert _poll(channel) is None  # waiting: never sent twice

    [(text, markup)] = channel.bot.sent
    assert "T" in text and "the task" in text
    # Dan's pilot feedback: no snapshot/worktree lines; the worktree is in the commands.
    assert SHA[:12] not in text
    assert DAN_ENTRY["check"] in text
    assert "cd /work/T &amp;&amp; make serve" in text  # {worktree} substituted, HTML-escaped
    assert "/verify T approve" in text and "/verify T reject" in text
    labels = [button["text"] for row in markup["inline_keyboard"] for button in row]
    assert labels == ["Approve", "Reject"]
    entry = channel.state[verify.STATE_KEY]["T"]
    assert entry["snapshot"] == SHA and entry["run_id"] == "run_1"


def test_approve_by_button_is_a_verdict_bound_to_the_snapshot(channel, monkeypatch):
    monkeypatch.setattr(telegram, "_tg_api", lambda *a, **k: None)
    _poll(channel)

    _tap(verify.request_id("T", SHA), 0)
    verdict = _poll(channel)

    assert verdict == verify.DanVerdict("approved", "", verify.request_id("T", SHA))
    assert "T" not in channel.state.get(verify.STATE_KEY, {})
    # A different tree is a different request: the approval does not carry over.
    assert _poll(channel, snapshot=SHA2) is None
    assert len(channel.bot.sent) == 2


def test_reject_by_button_asks_for_a_reason_and_the_reply_is_the_rework_note(
    channel, monkeypatch
):
    monkeypatch.setattr(telegram, "_tg_api", lambda *a, **k: None)
    _poll(channel)
    _tap(verify.request_id("T", SHA), 1)

    assert _poll(channel) is None
    assert "reason" in channel.bot.sent[-1][0].lower()
    assert _poll(channel) is None and len(channel.bot.sent) == 2  # asked once

    telegram._route_freetext_answer("the banner is blue, it must be red", None)
    verdict = _poll(channel)

    assert verdict.verdict == "rejected"
    assert verdict.reason == "the banner is blue, it must be red"


def test_verify_text_commands_answer_the_waiting_request(channel):
    _poll(channel)

    reply = verify.handle_verify_command("T reject the banner is missing", state=channel.state)
    verdict = _poll(channel)

    assert "Recorded" in reply
    assert verdict.verdict == "rejected" and verdict.reason == "the banner is missing"

    _poll(channel, snapshot=SHA2)
    verify.handle_verify_command("T approve", state=channel.state)
    assert _poll(channel, snapshot=SHA2).verdict == "approved"


def test_verify_reject_without_a_reason_is_refused(channel):
    _poll(channel)

    reply = verify.handle_verify_command("T reject", state=channel.state)

    assert "reason" in reply.lower()
    assert _poll(channel) is None


def test_verify_for_a_task_nothing_waits_on_says_so(channel):
    assert "No Dan verification is waiting" in verify.handle_verify_command(
        "NOPE approve", state=channel.state
    )


def test_an_unconfigured_bot_journals_once_and_keeps_waiting(channel):
    channel.configured = lambda: False

    assert _poll(channel) is None
    assert _poll(channel) is None

    assert channel.bot.sent == []
    assert [e for e, _ in channel.journaled].count("dan_verify_unconfigured") == 1
    assert channel.state[verify.STATE_KEY]["T"]["sent"] is False

    channel.configured = lambda: True
    assert _poll(channel) is None
    assert len(channel.bot.sent) == 1  # sent once the bot is configured


def test_waiting_lines_list_what_awaits_dan(channel):
    _poll(channel)

    [line] = verify.waiting_lines(channel.state)

    assert "T" in line and SHA[:12] in line and "/verify T approve" in line


def test_operator_waiting_help_and_legacy_aliases_use_pass_fail(channel):
    assert channel.poll(task_id='T', title='t', run_id='r', snapshot=SHA,
                        worktree='/work/T', checks=CHECKS, operator=True) is None
    [line] = verify.waiting_lines(channel.state)
    assert '/verify T pass' in line and '/verify T fail <reason>' in line
    assert 'pass' in verify.VERIFY_USAGE and 'fail' in verify.VERIFY_USAGE
    assert 'Recorded' in verify.answer_waiting('T', 'reject', 'no message', state=channel.state)
    verdict = channel.poll(task_id='T', title='t', run_id='r', snapshot=SHA,
                           worktree='/work/T', checks=CHECKS, operator=True)
    assert verdict.verdict == 'rejected' and verdict.reason == 'no message'


def test_prune_drops_entries_of_runs_that_are_no_longer_live(channel):
    _poll(channel)

    verify.prune_awaiting({"run_other"}, read_state=channel.read_state,
                          write_state=channel.write_state)

    assert channel.state[verify.STATE_KEY] == {}


# ── the request Dan reads: an action he must take is numbered imperative steps ──────
# Dan's feedback on the DuetFlow pilot (task 03, 2026-09-26): the V-DAN request read as a
# third-person report with the snapshot and worktree on top. A check Dan must *do*
# something for is now headed "Dan action needed" and, when the ROADMAP entry carries
# `steps`, rendered as numbered imperative steps with each command in its own block.

STEPS_ENTRY = {
    "id": "V-DAN",
    "kind": "dan",
    "check": "Dan picked his playlist from the list and the app registered it.",
    "steps": [
        {"do": "List your playlists", "run": "cd {worktree} && app playlists"},
        {"do": "Register the one you want by its number", "run": "cd {worktree} && app use <number>"},
        "Check the output names the playlist you chose",
    ],
}


def test_compiler_carries_steps_normalised_and_only_when_declared():
    revision = compiler.compile_workflow(_spec([AUTO_ENTRY, STEPS_ENTRY]), workflow_id="wf_T")

    [check] = revision.node('operator_check').params["dan_checks"]
    assert check["steps"] == [
        {"do": "List your playlists", "run": "cd {worktree} && app playlists"},
        {"do": "Register the one you want by its number", "run": "cd {worktree} && app use <number>"},
        {"do": "Check the output names the playlist you chose", "run": ""},
    ]
    # An entry without `steps` keeps its old params, so its dag_hash is unchanged.
    plain = compiler.compile_workflow(_spec([AUTO_ENTRY, DAN_ENTRY]), workflow_id="wf_T")
    assert "steps" not in plain.node('operator_check').params["dan_checks"][0]


def test_a_request_with_steps_is_an_action_with_numbered_imperative_steps():
    text, markup = verify.render_request(
        "03-reader", "the reader", SHA, "/work/03", compiler.dan_checks(_spec([STEPS_ENTRY])),
    )

    assert text.startswith("🛠 <b>Dan action needed — 03-reader</b>")
    assert "Snapshot" not in text and SHA[:12] not in text and "Worktree" not in text
    assert "1) List your playlists\n<pre>cd /work/03 &amp;&amp; app playlists</pre>" in text
    assert "2) Register the one you want by its number\n<pre>cd /work/03 &amp;&amp; app use &lt;number&gt;</pre>" in text
    assert "3) Check the output names the playlist you chose" in text
    # The third-person check is the record of what was verified, not Dan's instructions.
    assert STEPS_ENTRY["check"] not in text
    assert "Then tap <b>Approve</b>" in text and "/verify 03-reader approve" in text
    assert [b["text"] for b in markup["inline_keyboard"][0]] == ["Approve", "Reject"]


def test_a_request_with_free_form_instructions_is_an_action_too():
    text, _ = verify.render_request("T", "the task", SHA, "/work/T", CHECKS)

    assert text.startswith("🛠 <b>Dan action needed — T</b>")
    assert SHA[:12] not in text and "Worktree" not in text
    assert "<pre>cd /work/T &amp;&amp; make serve, then open http://localhost:8000</pre>" in text
    assert DAN_ENTRY["check"] in text  # what must hold, since there are no steps to say it


def test_a_pure_verification_asks_dan_to_confirm_without_an_action():
    checks = [{"id": "V2", "check": "the phone got the push", "instructions": ""}]
    text, _ = verify.render_request("T", "", SHA, "/work/T", checks)

    assert text.startswith("🔎 <b>Dan check needed — T</b>")
    assert "action" not in text.lower()
    assert "• the phone got the push" in text
