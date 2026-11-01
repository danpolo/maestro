"""P12 pilot finding D29: a deny-list hit is caught in the gate and asks Dan.

DuetFlow 06 run 6 passed three review rounds and Dan's real-world test, then the merge
guard refused the app's own `DELETE FROM duet_state`. Dan's decision (2026-09-28): the
gate runs the same deny-list check before any review; on a hit an agent recommends allow
or send back, and Dan approves or disapproves with a button. An approval lets that exact
hit merge; a disapproval goes back to the implementer as a blocking finding.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import maestro.config
import maestro.state
from maestro import merge
from maestro.hitl import verify
from maestro.workflows import deny_list

from .test_dispatch_wiring import _attempts, _events, _journal_details
from .test_end_to_end import (
    FEATURE,
    TARGET,
    _DanAnswers,
    _git,
    _git_sandbox,
    _happy_scenario,
    _quality_failures,
    _run_status,
)
from .test_failure_ladder import _patch_fixture_cli_driver
from .test_merge_guard import _project, _WritingAgent


@pytest.fixture(autouse=True)
def _fresh_control_store():
    maestro.state._reset_control_store()
    yield
    maestro.state._reset_control_store()


STATE_FILE = "src/state.py"
DELETE = 'conn.execute("DELETE FROM duet_state")\n'
DELETE_2 = 'conn.execute("DELETE FROM duet_state WHERE id = 1")\n'
CLEAN = 'conn.execute("UPDATE duet_state SET seeds = NULL")\n'
YAML_COMMENT = "# DELETE FROM guards production tables; local app state is the app's own."
ALLOW = {"recommendation": "allow", "reason": "it resets the app-owned local state table",
         "what_to_change": ""}
SEND_BACK = {"recommendation": "send_back", "reason": "a bulk delete is avoidable here",
             "what_to_change": "clear the row with an UPDATE instead of DELETE FROM"}


class _Dan(_DanAnswers):
    """Dan, who also notes how many reviewer briefs existed when each ask arrived."""

    def __init__(self, replies, agent):
        super().__init__(replies)
        self.agent = agent
        self.reviews_at_ask: list[int] = []

    def send(self, text, markup=None):
        self.reviews_at_ask.append(len(self.agent.briefs("reviewer")))
        return super().send(text, markup)


def _lane(run_lane, monkeypatch, lane, agent, dan, recommendation=ALLOW):
    """A task whose implementer adds deny-listed lines; the recommender always answers
    `recommendation` for every hit it is shown, and its prompts are recorded."""
    _patch_fixture_cli_driver(monkeypatch, agent)
    monkeypatch.setattr(verify, "send_via_bot", dan.send)
    monkeypatch.setattr(verify, "telegram_configured", lambda: True)
    prompts: list[str] = []

    def recommender(prompt, cwd):
        prompts.append(prompt)
        keys = [line.split()[2] for line in prompt.splitlines() if line.startswith("--- hit ")]
        return json.dumps({"hits": [{"key": k, **recommendation} for k in keys]})

    monkeypatch.setattr(deny_list, "_agent", recommender)

    def sandbox(run):
        _git_sandbox(run)
        path = Path(maestro.config.PROJECT_YAML)
        path.write_text(YAML_COMMENT + "\n" + path.read_text(encoding="utf-8"), encoding="utf-8")

    run = run_lane(_happy_scenario(
        lane, before_run=sandbox, on_poll=dan.on_poll, project_yaml=_project(),
    ))
    assert run.returncode == 0
    return run, prompts


def _gates() -> list[str]:
    return [a["status"] for a in _attempts() if a["node_id"] == "gate"]


def _buttons(markup) -> list[str]:
    return [b["text"] for row in markup["inline_keyboard"] for b in row]


# ── lanes ────────────────────────────────────────────────────────────────────────────


def test_a_deny_list_hit_is_caught_in_the_gate_before_review(run_lane, monkeypatch):
    agent = _WritingAgent({STATE_FILE: DELETE})
    dan = _Dan([("tap", 0)], agent)

    _lane(run_lane, monkeypatch, "d29_caught_in_gate", agent, dan)

    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    assert dan.reviews_at_ask == [0]
    assert _gates() == ["failed", "succeeded"]
    assert _quality_failures("gate") == 0 and _quality_failures("implementer") == 0
    assert len(agent.briefs("implementer")) == 1
    assert len(_journal_details("deny_list_hit_asked")) == 1
    assert _events("DenyListHitAsked")


def test_a_deny_list_hit_asks_dan_with_the_agents_recommendation_and_buttons(
    run_lane, monkeypatch
):
    agent = _WritingAgent({STATE_FILE: DELETE})
    dan = _Dan([("tap", 0)], agent)

    _, prompts = _lane(run_lane, monkeypatch, "d29_ask_text", agent, dan)

    [(text, markup)] = dan.sent
    assert text.startswith("⛔ <b>Deny-list hit — TASK-001 title</b>")
    assert STATE_FILE in text and "DELETE FROM duet_state" in text
    assert "Recommendation: Allow" in text and ALLOW["reason"] in text
    assert _buttons(markup) == ["✅ Approve", "❌ Disapprove"]
    [prompt] = prompts
    assert "TASK: TASK-001: TASK-001 title" in prompt
    assert YAML_COMMENT in prompt
    assert '+conn.execute("DELETE FROM duet_state")' in prompt


def test_an_approved_hit_lets_the_same_change_merge(run_lane, monkeypatch):
    agent = _WritingAgent({STATE_FILE: DELETE})
    dan = _Dan([("tap", 0)], agent)

    run, _ = _lane(run_lane, monkeypatch, "d29_approved_merges", agent, dan)

    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    assert "DELETE FROM duet_state" in _git(run.repo, "show", f"{TARGET}:{STATE_FILE}")
    [approved] = _journal_details("deny_list_hit_approved")
    assert "by=dan" in approved and STATE_FILE in approved
    [ruled] = _events("DenyListHitRuled")
    assert json.loads(ruled["payload"])["ruling"] == "approved"
    assert _journal_details("merge_refused") == []


def test_a_changed_hit_after_approval_asks_again(run_lane, monkeypatch):
    agent = _WritingAgent(
        [{STATE_FILE: DELETE}, {STATE_FILE: DELETE_2}],
        reviews=[("changes_requested", ["only reset the seed row"])],
    )
    dan = _Dan([("tap", 0), ("tap", 0)], agent)

    run, _ = _lane(run_lane, monkeypatch, "d29_changed_asks_again", agent, dan)

    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    assert len(dan.sent) == 2
    keys = [json.loads(e["payload"])["hit"]["key"] for e in _events("DenyListHitRuled")]
    assert len(keys) == 2 and len(set(keys)) == 2
    assert dan.reviews_at_ask == [0, 1]
    assert _git(run.repo, "show", f"{TARGET}:{STATE_FILE}") == DELETE_2.strip()


def test_a_disapproved_hit_returns_to_the_implementer_as_a_blocking_finding(
    run_lane, monkeypatch
):
    agent = _WritingAgent([{STATE_FILE: DELETE}, {STATE_FILE: CLEAN}])
    dan = _Dan([("tap", 1)], agent)

    run, _ = _lane(run_lane, monkeypatch, "d29_disapproved", agent, dan, recommendation=SEND_BACK)

    assert _run_status() == "succeeded", _journal_details("graph_run_settled")
    [(text, _)] = dan.sent
    assert "Recommendation: Send back" in text and SEND_BACK["what_to_change"] in text
    first, rework = agent.briefs("implementer")
    assert "Deny-list hit disapproved by Dan" not in first
    assert "Deny-list hit disapproved by Dan" in rework
    assert SEND_BACK["what_to_change"] in rework
    assert len(_journal_details("deny_list_hit_disapproved")) == 1
    assert len(agent.briefs("reviewer")) == 1
    assert _git(run.repo, "show", f"{TARGET}:{STATE_FILE}") == CLEAN.strip()
    assert _journal_details("merge_refused") == []


# ── unit: hits, keys, the guard ──────────────────────────────────────────────────────


def _diff(*files: tuple[str, str]) -> str:
    out = []
    for name, added in files:
        out += [f"diff --git a/{name} b/{name}", "--- /dev/null", f"+++ b/{name}",
                "@@ -0,0 +1 @@", *[f"+{line}" for line in added.splitlines()]]
    return "\n".join(out) + "\n"


def test_deny_list_hits_are_reported_per_file_with_the_line():
    text = _diff(("a.py", "x = 1\nDELETE FROM t\n"), ("b.py", "ok\n"))
    hits, refusal = merge.deny_list_hits(text, ["a.py", "b.py"], {})
    assert refusal == ""
    [hit] = hits
    assert (hit["kind"], hit["file"], hit["line"]) == ("pattern", "a.py", "DELETE FROM t")
    assert hit["pattern"] == r"DELETE\s+FROM"


def test_command_patterns_skip_prose_files():
    """Dan 2026-10-08 (itv 01): `sudo` in a report's prose asked him three times. Docs are
    not executed, so command-shaped patterns do not apply to them."""
    text = _diff(("REPORT.md", "installed by `sudo bash deploy.sh`\nAgents never run `sudo`\n"),
                 ("notes.txt", "DELETE FROM t\n"), ("docs/x.rst", "git push --force\n"))
    hits, refusal = merge.deny_list_hits(text, ["REPORT.md", "notes.txt", "docs/x.rst"], {})
    assert (hits, refusal) == ([], "")


def test_command_patterns_still_apply_to_code_beside_prose():
    text = _diff(("REPORT.md", "run sudo\n"), ("deploy.sh", "sudo rm -rf /srv\n"))
    [hit], _ = merge.deny_list_hits(text, ["REPORT.md", "deploy.sh"], {})
    assert hit["file"] == "deploy.sh"


def test_a_command_split_across_prose_files_is_not_reassembled():
    text = _diff(("a.md", "DELETE\n"), ("b.md", "FROM t\n"))
    assert merge.deny_list_hits(text, ["a.md", "b.md"], {})[0] == []


def test_secret_and_project_patterns_still_apply_to_prose():
    text = _diff(("README.md", "TELEGRAM_BOT_TOKEN=abc\nuse --cookies-from-browser\n"))
    hits, _ = merge.deny_list_hits(text, ["README.md"], {"deny_list_extra": [r"--cookies-from-browser"]})
    assert sorted(h["pattern"] for h in hits) == sorted([r"TELEGRAM_BOT_TOKEN\s*=", r"--cookies-from-browser"])


def test_a_match_spanning_two_files_is_reported_across_files():
    text = _diff(("a.sql", "DELETE\n"), ("b.sql", "FROM t\n"))
    hits, _ = merge.deny_list_hits(text, ["a.sql", "b.sql"], {})
    assert [h["file"] for h in hits] == ["(across files)"]


def test_a_secret_path_is_a_path_hit():
    hits, _ = merge.deny_list_hits(_diff(("config/.env", "K=1\n")), ["config/.env"], {})
    assert [(h["kind"], h["file"]) for h in hits] == [("path", "config/.env")]


def test_a_hit_key_ignores_whitespace_and_changes_with_the_line():
    key = merge.deny_hit_key("a.py", r"DELETE\s+FROM", "DELETE FROM t")
    assert merge.deny_hit_key("a.py", r"DELETE\s+FROM", "DELETE   FROM  t") == key
    assert merge.deny_hit_key("a.py", r"DELETE\s+FROM", "DELETE FROM u") != key
    assert merge.deny_hit_key("b.py", r"DELETE\s+FROM", "DELETE FROM t") != key
    # The key is the whole line: the same match on a changed line is a new hit.
    [one], _ = merge.deny_list_hits(_diff(("a.py", "DELETE FROM t\n")), ["a.py"], {})
    [two], _ = merge.deny_list_hits(_diff(("a.py", "DELETE FROM t WHERE x\n")), ["a.py"], {})
    assert one["match"] == two["match"] and one["key"] != two["key"]


def test_an_added_line_that_looks_like_a_file_header_stays_in_its_file():
    text = _diff(("a.py", "++ b/other.py\nDELETE FROM t\n"))
    hits, _ = merge.deny_list_hits(text, ["a.py"], {})
    assert [h["file"] for h in hits] == ["a.py"]


def test_an_invalid_extra_pattern_refuses():
    _, refusal = merge.deny_list_hits(_diff(("a.py", "x\n")), ["a.py"], {"deny_list_extra": ["("]})
    assert refusal


def test_the_guard_passes_only_the_approved_key(tmp_path):
    from .test_merge_guard import _commit_on_branch, _repo

    repo = _repo(tmp_path)
    main = _git(repo, "rev-parse", "main")
    head = _commit_on_branch(repo, "hit", {STATE_FILE: DELETE})
    [hit], _ = merge.deny_list_hits(
        _git(repo, "diff", f"{main}...{head}") + "\n", [STATE_FILE], {})
    assert merge.integration_guard(repo, head, main, proj={}).startswith("deny_list:")
    assert merge.integration_guard(repo, head, main, proj={}, approved_hits=[hit["key"]]) == ""
    changed = _commit_on_branch(repo, "hit2", {STATE_FILE: DELETE_2})
    assert merge.integration_guard(
        repo, changed, main, proj={}, approved_hits=[hit["key"]]).startswith("deny_list:")


# ── unit: Dan's request and the recommender ─────────────────────────────────────────


def test_interpret_deny_reads_taps_and_replies():
    assert verify.interpret_deny("Approve") == ("approved", "")
    assert verify.interpret_deny("approved, it is fine") == ("approved", "")
    assert verify.interpret_deny("Disapprove") == ("disapproved", "")
    assert verify.interpret_deny("Disapprove: use UPDATE") == ("disapproved", "use UPDATE")
    assert verify.interpret_deny("use UPDATE instead") == ("disapproved", "use UPDATE instead")


_HIT = {"key": "k1", "kind": "pattern", "file": STATE_FILE, "pattern": r"DELETE\s+FROM",
        "match": "DELETE FROM", "line": 'conn.execute("DELETE FROM duet_state")'}


def test_render_deny_shows_the_hit_the_recommendation_and_two_buttons():
    text, markup = verify.render_deny(
        "T-1", "Title <x>", "dl-T-1-abc", {"hit": _HIT, "recommendation": SEND_BACK})
    assert text.startswith("⛔ <b>Deny-list hit — Title &lt;x&gt;</b>")
    assert STATE_FILE in text and "DELETE FROM duet_state" in text
    assert "Recommendation: Send back" in text and SEND_BACK["what_to_change"] in text
    assert [b["callback_data"] for row in markup["inline_keyboard"] for b in row] == [
        "danreq:dl-T-1-abc:0", "danreq:dl-T-1-abc:1"]
    text, _ = verify.render_deny("T-1", "", "r", {"hit": _HIT, "recommendation": {}})
    assert "No recommendation" in text


def test_recommend_retries_once_then_asks_without_a_recommendation(monkeypatch, tmp_path):
    replies = ["not json", json.dumps({"hits": [{"key": "k1", **ALLOW}]})]
    monkeypatch.setattr(deny_list, "_agent", lambda prompt, cwd: replies.pop(0))
    assert deny_list.recommend(tmp_path, {"id": "T"}, [_HIT], "")["k1"] == ALLOW

    calls = []

    def broken(prompt, cwd):
        calls.append(prompt)
        raise TimeoutError("300s")

    monkeypatch.setattr(deny_list, "_agent", broken)
    [(key, rec)] = deny_list.recommend(tmp_path, {"id": "T"}, [_HIT], "").items()
    assert len(calls) == 2
    assert key == "k1" and rec["recommendation"] == "" and "TimeoutError" in rec["reason"]


def test_a_reply_missing_a_hit_is_unusable(monkeypatch, tmp_path):
    other = {**_HIT, "key": "k2"}
    reply = json.dumps({"hits": [{"key": "k1", **ALLOW}]})
    monkeypatch.setattr(deny_list, "_agent", lambda prompt, cwd: reply)
    recs = deny_list.recommend(tmp_path, {"id": "T"}, [_HIT, other], "")
    assert {r["recommendation"] for r in recs.values()} == {""}
