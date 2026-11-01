"""P12 pilot finding D30: an agent asks Dan to allow a command family.

Maestro's Claude agents run `dontAsk` on a global allow list; a denied call the agent
needs becomes a `PERMISSION_REQUEST` naming the call, the broadest family it proposes, and
why. The node is held without spending budget while Dan taps Allow or Decline on Telegram.
Allow adds the family to the global list and re-runs the agent; Decline tells it why. A
never-allow or over-broad family is refused and never sent.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

import maestro.state
from maestro import permissions
from maestro.hitl import verify
from maestro.workflows import permission_asks

from .test_dispatch_wiring import _attempts, _events, _journal_details
from .test_end_to_end import (
    _DanAnswers,
    _drive_until_the_newest_run_settles,
    _happy_scenario,
    _quality_failures,
    _run_status,
)
from .test_failure_ladder import _patch_fixture_cli_driver
from .test_merge_guard import _WritingAgent


@pytest.fixture(autouse=True)
def _fresh_control_store():
    maestro.state._reset_control_store()
    yield
    maestro.state._reset_control_store()


REBASE = {"denied": "git rebase main", "family": "Bash(git rebase *)",
          "reason": "keeping a task branch current with its target is routine"}
SUDO = {"denied": "sudo apt-get install jq", "family": "Bash(sudo *)",
        "reason": "the tests need jq"}
ANYTHING = {"denied": "git rebase main", "family": "Bash(*)", "reason": "saves asking"}
STATUS = {"denied": "git status --short", "family": "Bash(git status *)",
          "reason": "to see the tree"}


class _AskingAgent(_WritingAgent):
    """A Claude-like agent on the allow list: its first implementer calls (one per entry
    of `asks`) write PERMISSION_REQUEST and stop; later ones do the work and report DONE."""

    permission_lists = True

    def __init__(self, asks):
        super().__init__({})
        self.asks = list(asks)

    def complete(self, spec):
        import re

        from maestro.backends.base import Completion

        if spec.prompt.startswith("You are the implementer ") and self.asks:
            self.specs.append(spec)
            workspace = Path(re.search(r"^WORKSPACE: (\S+)$", spec.prompt, re.MULTILINE).group(1))
            (workspace / permission_asks.REQUEST_FILE).write_text(
                json.dumps(self.asks.pop(0)), encoding="utf-8")
            return Completion(text="asked", returncode=0, timed_out=False)
        return super().complete(spec)


def _lane(run_lane, monkeypatch, lane, agent, dan):
    _patch_fixture_cli_driver(monkeypatch, agent)
    monkeypatch.setattr(verify, "send_via_bot", dan.send)
    monkeypatch.setattr(verify, "telegram_configured", lambda: True)
    run = run_lane(_happy_scenario(lane, on_poll=dan.on_poll))
    assert run.returncode == 0
    return run


def _buttons(markup) -> list[str]:
    return [b["text"] for row in markup["inline_keyboard"] for b in row]


def _implementers() -> list[str]:
    return [a["status"] for a in _attempts() if a["node_id"] == "implementer"]


# ── lanes ────────────────────────────────────────────────────────────────────────────


def test_a_denied_command_asks_dan_with_the_command_the_family_the_reason_and_two_buttons(
    run_lane, monkeypatch
):
    agent = _AskingAgent([REBASE])
    dan = _DanAnswers([("tap", 0)])

    _lane(run_lane, monkeypatch, "d30_ask_text", agent, dan)

    [(text, markup)] = dan.sent
    assert text.startswith("🔐 <b>Permission request — TASK-001 title</b>")
    assert "<pre>git rebase main</pre>" in text
    assert "<code>Bash(git rebase *)</code>" in text
    assert REBASE["reason"] in text and "GLOBAL allow list" in text
    assert _buttons(markup) == ["✅ Allow", "❌ Decline"]
    first = agent.briefs("implementer")[0]
    assert "PERMISSIONS: your tools run on an allow list" in first
    assert "PERMISSION RULINGS" not in first
    assert "only the attempted command family" in first
    assert "Bash(git mv *)" in first
    assert "Bash(* --version)" in first


def test_allow_adds_the_family_to_the_global_list_and_the_implementer_finishes(
    run_lane, monkeypatch
):
    agent = _AskingAgent([REBASE])
    dan = _DanAnswers([("tap", 0)])
    assert "Bash(git rebase *)" not in permissions.allow_rules()

    _lane(run_lane, monkeypatch, "d30_allowed", agent, dan)

    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    assert "Bash(git rebase *)" in permissions.allow_rules()
    [entry] = json.loads(permissions.path().read_text(encoding="utf-8"))["history"]
    assert entry["by"] == "dan" and entry["request_id"].startswith("pr-TASK-001-")
    [allowed] = _journal_details("permission_allowed")
    assert "by=dan" in allowed and "Bash(git rebase *)" in allowed
    [ruled] = _events("PermissionRuled")
    assert json.loads(ruled["payload"])["ruling"] == "allowed"
    assert len(_events("PermissionAsked")) == 1
    # Held, not failed: no budget spent, and the re-run is told.
    assert _quality_failures("implementer") == 0
    assert _implementers() == ["failed", "succeeded"]
    rerun = agent.briefs("implementer")[1]
    assert "Dan allowed Bash(git rebase *)" in rerun


def test_decline_tells_the_agent_in_its_re_run_brief(run_lane, monkeypatch):
    agent = _AskingAgent([REBASE])
    dan = _DanAnswers([("text", "Decline: merge the target instead")])

    _lane(run_lane, monkeypatch, "d30_declined", agent, dan)

    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    assert "Bash(git rebase *)" not in permissions.allow_rules()
    [declined] = _journal_details("permission_declined")
    assert "merge the target instead" in declined
    rerun = agent.briefs("implementer")[1]
    assert "Dan declined Bash(git rebase *): merge the target instead" in rerun
    assert _quality_failures("implementer") == 0


@pytest.mark.parametrize("request_, lane, why", [
    (SUDO, "d30_never_allow", "never-allow list"),
    (ANYTHING, "d30_over_broad", "over-broad"),
])
def test_a_never_allow_or_over_broad_proposal_is_refused_and_never_sent(
    run_lane, monkeypatch, request_, lane, why
):
    agent = _AskingAgent([request_])
    dan = _DanAnswers([])

    _lane(run_lane, monkeypatch, lane, agent, dan)

    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    assert dan.sent == []
    [refused] = _journal_details("permission_refused")
    assert why in refused
    assert len(_events("PermissionRefused")) == 1
    assert request_["family"] not in permissions.allow_rules()
    rerun = agent.briefs("implementer")[1]
    assert f"{request_['family']} was refused without asking Dan" in rerun


def test_an_already_allowed_family_re_runs_without_asking(run_lane, monkeypatch):
    agent = _AskingAgent([STATUS])
    dan = _DanAnswers([])

    _lane(run_lane, monkeypatch, "d30_already_allowed", agent, dan)

    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    assert dan.sent == []
    assert len(_journal_details("permission_already_allowed")) == 1
    assert _quality_failures("implementer") == 0
    assert "Bash(git status *) is already on the allow list" in agent.briefs("implementer")[1]


def test_re_asking_a_declined_family_fails_the_node(run_lane, monkeypatch):
    agent = _AskingAgent([REBASE, REBASE])
    dan = _DanAnswers([("tap", 1)])

    _lane(run_lane, monkeypatch, "d30_re_asked", agent, dan)

    assert len(dan.sent) == 1, "a declined family was put to Dan again"
    assert _quality_failures("implementer") == 1
    [refused] = maestro.state.control_store().conn.execute(
        "SELECT failure_json FROM attempts WHERE node_id = 'implementer' "
        "AND failure_kind = 'garbage_output'").fetchall()
    assert "re-asked Bash(git rebase *)" in refused["failure_json"]


def test_a_permission_wait_survives_a_controller_restart(run_lane, monkeypatch):
    agent = _AskingAgent([REBASE])
    dan = _DanAnswers([])  # Dan does not answer before the controller dies
    asked = {"n": 0}

    def until_asked(run):
        if maestro.state.read_state().get(verify.QUESTIONS_STATE_KEY):
            asked["n"] += 1
            if asked["n"] >= 3:  # a few idle ticks: the held node must not run
                run.halt()
                return
        _drive_until_the_newest_run_settles(run)

    monkeypatch.setattr(dan, "on_poll", until_asked)
    run = _lane(run_lane, monkeypatch, "d30_restart", agent, dan)
    assert _run_status() == "running" and len(dan.sent) == 1
    assert len(agent.briefs("implementer")) == 1, "the held node ran while Dan was asked"

    run.orchestrator.HALT_FILE.unlink()
    maestro.state._reset_control_store()
    dan.replies = [("tap", 0)]
    run.scenario = dataclasses.replace(run.scenario, on_poll=_DanAnswers.on_poll.__get__(dan))
    run.run()

    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    assert len(dan.sent) == 1, "the restarted loop re-sent the request"
    assert "Bash(git rebase *)" in permissions.allow_rules()


# ── unit ─────────────────────────────────────────────────────────────────────────────


def test_parse_takes_one_request_or_a_round_and_refuses_a_malformed_one():
    assert permission_asks.parse(json.dumps(REBASE)) == ([REBASE], "")
    assert permission_asks.parse(json.dumps({"requests": [REBASE, STATUS]}))[0] == [REBASE, STATUS]
    assert "not JSON" in permission_asks.parse("nope")[1]
    assert "no reason" in permission_asks.parse(json.dumps({**REBASE, "reason": ""}))[1]
    assert "at most 3" in permission_asks.parse(json.dumps({"requests": [REBASE] * 4}))[1]


def test_interpret_permission_reads_taps_and_replies():
    assert verify.interpret_permission("Allow") == ("allowed", "")
    assert verify.interpret_permission("allow it") == ("allowed", "")
    assert verify.interpret_permission("Decline") == ("declined", "")
    assert verify.interpret_permission("Decline: use merge") == ("declined", "use merge")
    assert verify.interpret_permission("use merge instead") == ("declined", "use merge instead")


def test_render_permission_escapes_and_carries_two_buttons():
    text, markup = verify.render_permission(
        "T-1", "Title <x>", "pr-T-1-abc",
        {"node_id": "implementer", "request": {**REBASE, "denied": "cat a <b"}})
    assert text.startswith("🔐 <b>Permission request — Title &lt;x&gt;</b>")
    assert "<pre>cat a &lt;b</pre>" in text
    assert [b["callback_data"] for row in markup["inline_keyboard"] for b in row] == [
        "danreq:pr-T-1-abc:0", "danreq:pr-T-1-abc:1"]


def test_outcome_line_for_a_permission_request():
    record = {"type": verify.PERMISSION_TYPE}
    assert "Allowed" in verify.outcome_line(record, "Allow")
    assert verify.outcome_line(record, "Decline: no") == "<b>❌ Declined</b> — no"


def test_the_claude_backend_advertises_the_allow_list():
    from maestro.backends.claude import ClaudeBackend

    assert ClaudeBackend.permission_lists is True


def test_a_malformed_request_file_is_garbage_output(tmp_path):
    from types import SimpleNamespace

    from maestro.workflows import worker

    args = SimpleNamespace(run_id="r", node_id="implementer", attempt_id="a")
    assert worker._adopt_permission_request(args, tmp_path) is None
    (tmp_path / permission_asks.REQUEST_FILE).write_text("{}", encoding="utf-8")
    result = worker._adopt_permission_request(args, tmp_path)
    assert result.outputs["failure"]["kind"] == "garbage_output"
    (tmp_path / permission_asks.REQUEST_FILE).write_text(json.dumps(REBASE), encoding="utf-8")
    assert worker._adopt_permission_request(args, tmp_path).outputs == {
        "permission_asks": [REBASE]}
