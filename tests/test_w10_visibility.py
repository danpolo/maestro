"""P13 W10: the progress line of a running attempt, the `/workflow` picture, the weekly digest.

The digest's proof (`docs/P13_ROADMAP.md`): its numbers equal `maestro report usage`. The
fixture is a real control database shape from `test_usage_report`, read by the real report.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from maestro import digest, status
from maestro.usage_report import build_usage_report
from maestro.workflows import progress
from maestro.workflows.handlers import attempts_root

from tests.test_usage_report import add_attempt, add_run, db, split, w2_event  # noqa: F401


T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


class _Sent(list):
    """A digest `send` that keeps the text and confirms delivery."""

    def append(self, text) -> bool:
        super().append(text)
        return True


def _inbox(repo: Path, attempt_id: str = "att_1") -> Path:
    inbox = attempts_root(repo) / attempt_id
    inbox.mkdir(parents=True)
    return inbox


def _attempt(attempt_id: str = "att_1", backend_id: str = "claude") -> dict:
    return {"attempt_id": attempt_id, "status": "running", "backend_id": backend_id,
            "model_id": "m", "effort": "low", "_order": 1}


def _tokens(fresh=100, read=200, write=0, output=50) -> dict:
    return {"fresh_input": fresh, "cache_read": read, "cache_write": write, "output": output}


# ── the progress line ────────────────────────────────────────────────────────────────


def test_the_first_view_shows_tokens_without_a_growth_figure(tmp_path):
    _inbox(tmp_path)
    line = progress.line(tmp_path, _attempt(), now=T0, reader=lambda *_: _tokens())
    assert line == "Progress: 350 tokens"


def test_a_later_view_shows_the_growth_since_the_last_view(tmp_path):
    _inbox(tmp_path)
    progress.line(tmp_path, _attempt(), now=T0, reader=lambda *_: _tokens())
    later = T0 + timedelta(minutes=4)
    line = progress.line(tmp_path, _attempt(), now=later, reader=lambda *_: _tokens(output=1_250))
    assert line == "Progress: 1,550 tokens (+1,200 in 4m)"


def test_nothing_known_means_no_line(tmp_path):
    _inbox(tmp_path)
    assert progress.line(tmp_path, _attempt(), now=T0, reader=lambda *_: None) is None


def test_a_missing_inbox_or_a_broken_reader_means_no_line_and_no_exception(tmp_path):
    def broken(*_):
        raise RuntimeError("transcript moved")

    assert progress.line(tmp_path, _attempt(), now=T0, reader=lambda *_: None) is None
    _inbox(tmp_path)
    assert progress.line(tmp_path, _attempt(), now=T0, reader=broken) is None


@pytest.mark.parametrize("backend_id", ["claude", "codex", "antigravity_cli", "not-a-backend"])
def test_every_driver_is_read_cleanly_with_no_session_record(tmp_path, backend_id):
    _inbox(tmp_path)
    assert progress.line(tmp_path, _attempt(backend_id=backend_id), now=T0) is None


def _running_view(repo: Path, attempt: dict | None) -> dict:
    return {"task_id": "T-1", "task_status": "active", "repo": str(repo),
            "run": {"run_id": "r1", "status": "running", "started_at": T0.isoformat()},
            "workflow": {"workflow_id": "wf", "revision": 1},
            "nodes": [{"node_id": "implement", "phase": "running", "attempt_index": 1,
                       "attempts": [attempt] if attempt else []}]}


def test_the_compact_lines_carry_the_progress_of_the_running_attempt(tmp_path, monkeypatch):
    _inbox(tmp_path)
    _resume_state(tmp_path, last_count=2, total=5)
    monkeypatch.setattr(progress, "_driver_tokens", lambda *_: _tokens())
    text = status.render_graph_progress_text(_running_view(tmp_path, _attempt()))
    assert "Progress: 350 tokens · last gate PROGRESS 2/5" in text.splitlines()


def test_a_settled_attempt_or_a_finished_run_shows_no_progress_line(tmp_path, monkeypatch):
    _inbox(tmp_path)
    monkeypatch.setattr(progress, "_driver_tokens", lambda *_: _tokens())
    settled = status.render_graph_progress_text(
        _running_view(tmp_path, {**_attempt(), "status": "succeeded"}))
    assert "Progress:" not in settled
    done = _running_view(tmp_path, _attempt())
    done["run"].update(status="succeeded", finished_at=T0.isoformat())
    assert "Progress:" not in status.render_graph_progress_text(done)


# ── `/workflow` ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def workflow_command(tmp_path, monkeypatch):
    from maestro.hitl import commands

    view = _running_view(tmp_path, None)
    view["run"].update(status="succeeded", finished_at=T0.isoformat())
    sent = SimpleNamespace(documents=[], reports=[], repo=tmp_path)
    monkeypatch.setattr(commands, "REPO", tmp_path)
    monkeypatch.setattr(status, "workflow_view", lambda task, **kw: view)
    monkeypatch.setattr(status, "render_workflow_html", lambda v: "<html>graph</html>")
    monkeypatch.setattr(status, "render_workflow_text", lambda v: "the text")
    monkeypatch.setattr(commands, "send_report",
                        lambda text, header="": sent.reports.append(text) or 1)
    def document(path, *, caption=""):
        sent.documents.append((Path(path), Path(path).read_text(), caption))
        return sent.document_ok
    monkeypatch.setattr(commands, "send_document", document)
    sent.run = commands._show_workflow
    return sent


def test_workflow_sends_the_picture_and_not_the_text(workflow_command):
    sent = workflow_command
    sent.document_ok = True
    sent.run("T-1")
    (path, body, caption), = sent.documents
    assert path == sent.repo / ".orchestrator" / "artifacts" / "workflow-T-1.html"
    assert body == "<html>graph</html>"
    assert caption.startswith("T-1 · active")
    assert sent.reports == []


def test_workflow_falls_back_to_the_text_when_the_send_fails(workflow_command):
    sent = workflow_command
    sent.document_ok = False
    sent.run("T-1")
    assert len(sent.documents) == 1
    assert sent.reports == ["the text"]


def test_workflow_falls_back_to_the_text_when_the_picture_cannot_be_drawn(
        workflow_command, monkeypatch):
    def broken(_view):
        raise ValueError("no")

    monkeypatch.setattr(status, "render_workflow_html", broken)
    sent = workflow_command
    sent.document_ok = True
    sent.run("T-1")
    assert sent.documents == []
    assert sent.reports == ["the text"]


def test_send_document_posts_the_file_and_reports_telegram_s_answer(tmp_path, monkeypatch):
    from maestro.hitl import shared_bot, telegram

    target = tmp_path / "w.html"
    target.write_text("<html/>")
    calls = []

    def post(url, data=None, files=None, timeout=None):
        calls.append((url, data, files["document"].read()))
        return SimpleNamespace(ok=True, json=lambda: {"ok": True})

    monkeypatch.setattr(shared_bot, "credentials", lambda: ("tok", "55"))
    monkeypatch.setattr(telegram.requests, "post", post)
    assert telegram.send_document(target, caption="hello") is True
    (url, data, body), = calls
    assert url.endswith("/sendDocument")
    assert data["chat_id"] == "55" and "hello" in data["caption"]
    assert body == b"<html/>"


def test_send_document_is_false_without_credentials_a_file_or_an_ok(tmp_path, monkeypatch):
    from maestro.hitl import shared_bot, telegram

    target = tmp_path / "w.html"
    target.write_text("<html/>")
    monkeypatch.setattr(shared_bot, "credentials", lambda: (None, None))
    assert telegram.send_document(target) is False
    monkeypatch.setattr(shared_bot, "credentials", lambda: ("tok", "55"))
    monkeypatch.setattr(telegram.requests, "post", lambda *a, **k: SimpleNamespace(
        ok=True, json=lambda: {"ok": False}))
    assert telegram.send_document(target) is False
    assert telegram.send_document(tmp_path / "gone.html") is False


def test_send_document_cuts_the_caption_to_telegram_s_limit(tmp_path, monkeypatch):
    from maestro.hitl import shared_bot, telegram

    target = tmp_path / "w.html"
    target.write_text("<html/>")
    seen = {}

    def post(url, data=None, files=None, timeout=None):
        seen.update(data)
        return SimpleNamespace(ok=True, json=lambda: {"ok": True})

    monkeypatch.setattr(shared_bot, "credentials", lambda: ("tok", "55"))
    monkeypatch.setattr(telegram.requests, "post", post)
    assert telegram.send_document(target, caption="x" * 5000) is True
    assert len(seen["caption"]) <= 1024


# ── the digest ───────────────────────────────────────────────────────────────────────


@pytest.fixture
def filled(db):  # noqa: F811
    """Two accepted tasks and one failed, two routes in one pool, a fault, reviews."""
    add_run(db, run="r1", task="t1", status="succeeded")
    add_attempt(db, aid="a1", run="r1", node="looks_like_gate", tokens=split(), wall=10, cost=2)
    add_attempt(db, aid="a2", run="r1", node="writer_name", tokens=split(fresh=20), wall=5, cost=2)
    add_run(db, run="r2", task="t2", status="succeeded")
    add_attempt(db, aid="a3", run="r2", node="looks_like_gate", tokens=split(fresh=40), wall=20,
                cost=2)
    add_attempt(db, aid="a4", run="r2", node="looks_like_gate", tokens=split(output=15), wall=20,
                cost=2)
    add_run(db, run="r3", task="t3", status="failed")
    add_attempt(db, aid="a5", run="r3", node="looks_like_gate", tokens=split(), wall=1, cost=2)
    w2_event(db, "ExtraStepObserved", {"ledger_id": "attempt:a4", "observed_cause": "review_rework",
                                       "context": {"role": "reviewer"}, "fault": "reviewer",
                                       "mapping_version": 1}, aid="a4", run="r2")
    w2_event(db, "ReviewComplexityObserved", {"ledger_id": "review:a2", "complexity": 2,
                                              "rubric_version": 1}, aid="a2", run="r1")
    w2_event(db, "ReviewComplexityObserved", {"ledger_id": "review:a3", "complexity": 4,
                                              "rubric_version": 1}, aid="a3", run="r2")
    return db


def test_digest_numbers_equal_the_usage_report(filled):
    report = build_usage_report(filled)
    built = digest.build(report)
    tasks = {t["task_id"]: t for t in report["tasks"]}

    assert built["accepted_tasks"] == 2
    assert built["all_tasks"] == 3
    per_task = {t["task_id"]: t for t in built["accepted"]}
    assert set(per_task) == {"t1", "t2"}
    for tid, row in per_task.items():
        for name in ("tokens", "wall_time_sec"):
            assert row[name] == tasks[tid]["measurements"][name]
        assert row["cost_by_pool"]["codex-subscription"] == tasks[tid]["measurements"]["cost_units"]
        assert row["implementer_cycles"] == tasks[tid]["counts"]["implementer_cycles"]
        assert row["review_rounds"] == tasks[tid]["counts"]["review_rounds"]

    tokens = sum(t["measurements"]["tokens"]["known_sum"] for t in tasks.values())
    assert built["total"]["tokens"]["known_sum"] == tokens
    assert built["per_accepted"]["tokens"] == tokens / 2
    assert built["total"]["implementer_cycles"] == sum(
        t["counts"]["implementer_cycles"] for t in tasks.values())
    assert built["total"]["review_rounds"] == sum(
        t["counts"]["review_rounds"] for t in tasks.values())
    pool_cost = sum(t["measurements"]["cost_units"]["known_sum"] for t in tasks.values())
    assert built["cost_by_pool"]["codex-subscription"]["known_sum"] == pool_cost
    assert built["per_accepted"]["cost_by_pool"]["codex-subscription"] == pool_cost / 2

    fault = tasks["t2"]["faults"][0]
    row, = built["faults"]
    assert row["fault"] == fault["fault"]
    assert row["ledger_rows"] == fault["ledger_rows"]
    assert row["tokens"] == fault["measurements"]["tokens"]
    assert row["cost_by_pool"]["codex-subscription"] == fault["measurements"]["cost_units"]


def test_the_rendered_digest_prints_the_report_s_numbers(filled):
    report = build_usage_report(filled)
    text = digest.render(digest.build(report))
    t2 = next(t for t in report["tasks"] if t["task_id"] == "t2")
    assert f"t2: {t2['measurements']['tokens']['known_sum']:,} tokens" in text
    assert "2 accepted of 3 tasks" in text
    assert f"{t2['faults'][0]['fault']}: 1 rows" in text


def test_routes_compare_by_cost_per_complexity_point(filled):
    built = digest.build(build_usage_report(filled))
    rows = {(r["role"], r["model"]): r for r in built["routes"]}
    report = build_usage_report(filled)
    tasks = {t["task_id"]: t for t in report["tasks"]}
    implementer_cost = sum(
        r["measurements"]["cost_units"]["known_sum"]
        for tid in ("t1", "t2") for r in tasks[tid]["routes"] if r["role"] == "implementer")
    row = rows[("implementer", "sol")]
    assert row["complexity_points"] == 6
    assert row["tasks"] == 2
    assert row["unscored_tasks"] == 1
    assert row["cost_per_point"] == implementer_cost / 6


def test_the_last_reviewed_complexity_of_a_task_counts(db):  # noqa: F811
    add_run(db, run="r1", task="t1")
    add_attempt(db, aid="a1", run="r1", node="looks_like_gate", tokens=split(), cost=2)
    add_attempt(db, aid="a2", run="r1", node="writer_name", tokens=split(), cost=2)
    add_attempt(db, aid="a3", run="r1", node="writer_name", tokens=split(), cost=2)
    w2_event(db, "ReviewComplexityObserved", {"ledger_id": "review:a2", "complexity": 2,
                                              "rubric_version": 1}, aid="a2", run="r1")
    w2_event(db, "ReviewComplexityObserved", {"ledger_id": "review:a3", "complexity": 5,
                                              "rubric_version": 1}, aid="a3", run="r1")
    built = digest.build(build_usage_report(db))
    assert {r["complexity_points"] for r in built["routes"]} == {5}


def test_unknown_values_stay_unknown(db):  # noqa: F811
    add_run(db, run="r1", task="t1", status="succeeded")
    add_attempt(db, aid="a1", run="r1", node="looks_like_gate", tokens=None, cost=2)
    add_attempt(db, aid="a2", run="r1", node="looks_like_gate", tokens=split(), cost=2)
    built = digest.build(build_usage_report(db))
    assert built["total"]["tokens"]["measured_attempts"] == 1
    assert built["total"]["tokens"]["total_attempts"] == 2
    assert built["routes"][0]["cost_per_point"] is None
    text = digest.render(built)
    assert "1/2 attempts measured" in text
    assert "complexity unknown" in text


def test_nothing_measured_stays_unknown(db):  # noqa: F811
    add_run(db, run="r1", task="t1", status="succeeded")
    add_attempt(db, aid="a1", run="r1", tokens=None, cost=2)
    built = digest.build(build_usage_report(db))
    assert built["per_accepted"]["tokens"] is None
    assert "Tokens: unknown; per accepted task: unknown" in digest.render(built)


def test_no_accepted_task_means_no_average(db):  # noqa: F811
    add_run(db, run="r1", task="t1", status="failed")
    add_attempt(db, aid="a1", run="r1", node="looks_like_gate", tokens=split(), cost=2)
    built = digest.build(build_usage_report(db))
    assert built["accepted_tasks"] == 0
    assert built["per_accepted"]["tokens"] is None


def test_the_improvement_report_is_embedded_as_it_stands(filled):
    improvement = {"runs": [{"key": "weekly:x"}],
                   "proposals": [{"id": "IP-1", "state": "applied"},
                                 {"id": "IP-2", "state": "declined"}],
                   "effects": [{"id": "IP-1", "effect": {"verdict": "better"}}]}
    built = digest.build(build_usage_report(filled), improvement)
    assert built["improvement"] == improvement
    text = digest.render(built)
    assert "Improvement: 1 runs, 2 proposals" in text
    assert next(l for l in text.splitlines() if "IP-1" in l).endswith("better.")


def test_a_long_list_of_accepted_tasks_is_capped_and_says_so(db):  # noqa: F811
    for index in range(digest.TASK_LINES + 3):
        run = f"r{index}"
        add_run(db, run=run, task=f"t{index:03}", status="succeeded")
        add_attempt(db, aid=f"a{index}", run=run, node="looks_like_gate", tokens=split(), cost=2)
    text = digest.render(digest.build(build_usage_report(db)))
    assert "… 3 more accepted tasks" in text


# ── when it is sent ──────────────────────────────────────────────────────────────────


def test_the_digest_goes_once_per_window(filled, tmp_path):
    sent = _Sent()
    first = T0.isoformat()
    assert digest.send_if_due(tmp_path, filled, at=first, send=sent.append) is True
    assert len(sent) == 1
    day = (T0 + timedelta(days=6)).isoformat()
    assert digest.send_if_due(tmp_path, filled, at=day, send=sent.append) is False
    week = (T0 + timedelta(days=7)).isoformat()
    assert digest.send_if_due(tmp_path, filled, at=week, send=sent.append) is True
    assert len(sent) == 2


def test_the_interval_threshold_is_honoured_and_zero_turns_it_off(filled, tmp_path, monkeypatch):
    from maestro import improvement

    monkeypatch.setattr(improvement, "_threshold", lambda repo, name, default: 0)
    sent = _Sent()
    assert digest.send_if_due(tmp_path, filled, at=T0.isoformat(), send=sent.append) is False
    monkeypatch.setattr(improvement, "_threshold", lambda repo, name, default: 2)
    assert digest.send_if_due(tmp_path, filled, at=T0.isoformat(), send=sent.append) is True
    later = (T0 + timedelta(days=2)).isoformat()
    assert digest.send_if_due(tmp_path, filled, at=later, send=sent.append) is True


def test_no_database_or_no_tasks_sends_nothing_and_spends_no_window(db, tmp_path):  # noqa: F811
    sent = _Sent()
    at = T0.isoformat()
    assert digest.send_if_due(tmp_path, tmp_path / "absent.sqlite3", at=at, send=sent.append) is False
    assert digest.send_if_due(tmp_path, db, at=at, send=sent.append) is False
    assert sent == []
    assert not (tmp_path / digest.STORE).exists()


def test_a_failed_send_spends_no_window(filled, tmp_path):
    def broken(_text):
        raise OSError("network")

    with pytest.raises(OSError):
        digest.send_if_due(tmp_path, filled, at=T0.isoformat(), send=broken)
    sent = _Sent()
    assert digest.send_if_due(tmp_path, filled, at=T0.isoformat(), send=sent.append) is True


def test_an_unreadable_record_does_not_silence_the_digest(filled, tmp_path):
    store = tmp_path / digest.STORE
    store.parent.mkdir(parents=True)
    store.write_text("{not json")
    sent = _Sent()
    assert digest.send_if_due(tmp_path, filled, at=T0.isoformat(), send=sent.append) is True
    assert json.loads(store.read_text())["sent_at"] == T0.isoformat()


def test_the_loop_never_sees_a_digest_failure(monkeypatch):
    from maestro import orchestrator

    def broken(*_a, **_k):
        raise RuntimeError("bad database")

    journal = []
    monkeypatch.setattr(digest, "send_if_due", broken)
    monkeypatch.setattr(orchestrator, "append_journal", lambda *a: journal.append(a))
    assert orchestrator._graph_digest() is False
    assert journal[0][0] == "digest_failed"


def test_the_weekly_tick_sends_the_digest_only_at_a_real_idle_moment(monkeypatch):
    from maestro import orchestrator

    calls = []
    monkeypatch.setattr(orchestrator, "_graph_improvement", lambda *a: calls.append("analysis"))
    monkeypatch.setattr(orchestrator, "_graph_digest", lambda: calls.append("digest"))
    monkeypatch.setattr(orchestrator, "_last_improvement_check", -1e12)
    orchestrator._graph_improvement_weekly(None, None, None, runnable=("T-1",))
    assert calls == []
    orchestrator._graph_improvement_weekly(None, None, None, runnable=())
    assert calls == ["analysis", "digest"]


# ── review findings: real attempt shapes, delivery, agent-writable inboxes ───────────


@pytest.fixture
def real_shape(db):  # noqa: F811
    """Gate attempts with no route or tokens beside model attempts, in two pools."""
    add_run(db, run="r1", task="t1", status="succeeded")
    add_attempt(db, aid="a1", run="r1", node="writer_name", tokens=split(fresh=10, cache=0, output=0),
                wall=10, cost=2, pool="pool-a")
    add_attempt(db, aid="a2", run="r1", node="looks_like_gate", tokens=split(fresh=20, cache=0, output=0),
                wall=30, cost=3, pool="pool-b")
    add_attempt(db, aid="a3", run="r1", node="gate_node", tokens=None, wall=None, backend_id=None,
                model_id=None, effort=None, binding_json="{}")
    add_run(db, run="r2", task="t2", status="succeeded")
    add_attempt(db, aid="a4", run="r2", node="writer_name", tokens=split(fresh=30, cache=0, output=0),
                wall=20, cost=2, pool="pool-a")
    add_attempt(db, aid="a5", run="r2", node="gate_node", tokens=None, wall=None, backend_id=None,
                model_id=None, effort=None, binding_json="{}")
    return db


def test_gate_attempts_do_not_make_usage_per_accepted_task_unknown(real_shape):
    built = digest.build(build_usage_report(real_shape))
    assert built["total"]["tokens"]["known_sum"] == 60
    assert built["total"]["tokens"]["complete"] is True
    assert built["per_accepted"]["tokens"] == 30
    assert built["per_accepted"]["wall_time_sec"] == 30
    assert set(built["cost_by_pool"]) == {"pool-a", "pool-b"}
    assert built["cost_by_pool"]["pool-a"]["known_sum"] == 80
    assert built["cost_by_pool"]["pool-b"]["known_sum"] == 60
    assert built["per_accepted"]["cost_by_pool"] == {"pool-a": 40, "pool-b": 30}
    assert all(row["harness"] for row in built["routes"])


def test_the_rendered_digest_equals_the_report_json_on_a_real_shape(real_shape):
    """The proof, through the JSON `maestro report usage` prints."""
    from maestro.usage_report import render_usage_report

    report = json.loads(render_usage_report(build_usage_report(real_shape)))
    routes = [r for t in report["tasks"] for r in t["routes"] if r["harness"]]
    tokens = sum(r["measurements"]["tokens"]["known_sum"] for r in routes)
    text = digest.render(digest.build(report))
    assert f"Tokens: {tokens:,}; per accepted task: {tokens // 2:,}" in text
    assert "per accepted task, pool-a: 40" in text
    assert "per accepted task, pool-b: 30" in text
    assert "Wall time: 60 s; per accepted task: 30" in text
    assert "unknown pool" not in text and "unknown/unknown" not in text
    assert "missing_route" not in text and "mixed_cost_pools" not in text
    assert "t1: 30 tokens, cost pool-a 20; pool-b 60" in text


def test_a_partly_measured_total_is_a_lower_bound_not_unknown(db):  # noqa: F811
    add_run(db, run="r1", task="t1", status="succeeded")
    add_attempt(db, aid="a1", run="r1", tokens=None, cost=2)
    add_attempt(db, aid="a2", run="r1", tokens=split(), cost=2)
    built = digest.build(build_usage_report(db))
    assert built["per_accepted"]["tokens"] == 35
    assert built["total"]["tokens"]["complete"] is False
    text = digest.render(built)
    assert "Tokens: at least 35 (1/2 attempts measured); per accepted task: at least 35" in text


def test_routes_are_grouped_by_role_and_pool_and_ranked_cheapest_first(db):  # noqa: F811
    add_run(db, run="r1", task="t1", status="succeeded")
    add_attempt(db, aid="a1", run="r1", tokens=split(), cost=9, model_id="dear")
    add_attempt(db, aid="a2", run="r1", tokens=split(), cost=1, model_id="cheap")
    add_attempt(db, aid="a3", run="r1", tokens=split(), cost=1, model_id="other", pool="pool-b")
    w2_event(db, "ReviewComplexityObserved", {"ledger_id": "review:a1", "complexity": 5,
                                              "rubric_version": 1}, aid="a1", run="r1")
    lines = digest.render(digest.build(build_usage_report(db))).splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("Routes"))
    routes = lines[start:]
    assert "every route that worked on it" in routes[0]
    assert routes[1] == "  implementer, codex-subscription:"
    assert routes[2] == "    codex/cheap/high: 7 per point over 5 points, 1 tasks, 1 attempts per task"
    assert routes[3] == "    codex/dear/high: 63 per point over 5 points, 1 tasks, 1 attempts per task"
    assert routes[4] == "  implementer, pool-b:"
    assert routes[5] == "    codex/other/high: 7 per point over 5 points, 1 tasks, 1 attempts per task"


def test_an_applied_effect_shows_its_measured_numbers(filled):
    improvement = {"runs": [], "proposals": [{"id": "IP-1", "change": "raise the reviewer"}],
                   "effects": [{"id": "IP-1", "effect": {
                       "verdict": "better", "targets": ["cost_units_per_task"],
                       "metrics": {"cost_units_per_task": {"before": 10, "after": 5}}}}]}
    text = digest.render(digest.build(build_usage_report(filled), improvement))
    line = next(l for l in text.splitlines() if "IP-1" in l)
    assert "raise the reviewer" in line and "10" in line and "5" in line and "better" in line


def test_a_later_digest_says_what_changed_since_the_last_one(filled, tmp_path):
    sent = _Sent()
    deliver = lambda text: sent.append(text) or True  # noqa: E731
    assert digest.send_if_due(tmp_path, filled, at=T0.isoformat(), send=deliver)
    assert "Since the last digest" not in sent[0]
    add_run(filled, run="r9", task="t9", status="succeeded")
    add_attempt(filled, aid="a9", run="r9", tokens=split(fresh=65), cost=2)
    week = (T0 + timedelta(days=7)).isoformat()
    assert digest.send_if_due(tmp_path, filled, at=week, send=deliver)
    assert ("Since the last digest (2026-11-02): +1 accepted tasks, +90 tokens "
            "(90 per new accepted task), +1 implementer, +0 review") in sent[1]


def test_an_undelivered_digest_spends_no_window_and_waits_an_hour_to_retry(filled, tmp_path):
    sent = _Sent()
    tries = []
    def fail(text):
        tries.append(text)
        return False
    assert digest.send_if_due(tmp_path, filled, at=T0.isoformat(), send=fail) is False
    assert "sent_at" not in json.loads((tmp_path / digest.STORE).read_text())
    soon = (T0 + timedelta(minutes=10)).isoformat()
    assert digest.send_if_due(tmp_path, filled, at=soon, send=fail) is False
    assert len(tries) == 1
    hour = (T0 + timedelta(hours=1)).isoformat()
    assert digest.send_if_due(tmp_path, filled, at=hour, send=sent.append) is True
    assert "tried_at" not in json.loads((tmp_path / digest.STORE).read_text())


def test_a_failed_retry_keeps_the_totals_of_the_last_delivered_digest(filled, tmp_path):
    sent = _Sent()
    assert digest.send_if_due(tmp_path, filled, at=T0.isoformat(), send=sent.append) is True
    week = (T0 + timedelta(days=7)).isoformat()
    assert digest.send_if_due(tmp_path, filled, at=week, send=lambda _text: False) is False
    record = json.loads((tmp_path / digest.STORE).read_text())
    assert record["sent_at"] == T0.isoformat() and record["tried_at"] == week and record["totals"]


@pytest.mark.parametrize("totals", [
    "junk", {"accepted_tasks": 1}, {"accepted_tasks": 0, "tokens": 0, "implementer_cycles": 0,
                                    "review_rounds": 0, "cost_by_pool": ["not", "a", "map"]}])
def test_a_malformed_record_cannot_silence_the_digest(filled, tmp_path, totals):
    store = tmp_path / digest.STORE
    store.parent.mkdir(parents=True)
    store.write_text(json.dumps({"totals": totals}))
    sent = _Sent()
    assert digest.send_if_due(tmp_path, filled, at=T0.isoformat(), send=sent.append) is True
    assert "Since the last digest" not in sent[0]


def test_deliver_report_is_true_only_when_every_part_arrived(monkeypatch):
    from maestro.hitl import telegram

    answers = [True, False, True]
    seen = []
    def intake(text, **_kw):
        seen.append(text)
        return answers[len(seen) - 1]
    monkeypatch.setattr(telegram, "notify_intake", intake)
    body = "\n".join(["x" * 3000] * 3)
    assert telegram.deliver_report(body, header="h") is False
    assert len(seen) == 2 and seen[0].startswith("h (1/3)")
    answers[:] = [True, True, True]
    seen.clear()
    assert telegram.deliver_report(body, header="h") is True
    assert len(seen) == 3


def test_the_real_digest_send_uses_the_receipt_and_journals_a_failure(filled, tmp_path, monkeypatch):
    from maestro import orchestrator, state as state_module
    from maestro.hitl import telegram

    journal, delivered = [], []
    monkeypatch.setattr(orchestrator, "REPO", tmp_path)
    monkeypatch.setattr(state_module, "CONTROL_DB", filled)
    monkeypatch.setattr(orchestrator, "_improvement_now", lambda: T0.isoformat())
    monkeypatch.setattr(orchestrator, "append_journal", lambda *a: journal.append(a))
    monkeypatch.setattr(telegram, "deliver_report", lambda text, header="": False)
    assert orchestrator._graph_digest() is False
    assert journal[0][0] == "digest_undelivered"
    assert "sent_at" not in json.loads((tmp_path / digest.STORE).read_text())
    monkeypatch.setattr(orchestrator, "_improvement_now",
                        lambda: (T0 + timedelta(hours=1)).isoformat())
    monkeypatch.setattr(telegram, "deliver_report",
                        lambda text, header="": delivered.append((header, text)) or True)
    assert orchestrator._graph_digest() is True
    (header, text), = delivered
    assert "digest" in header and "2 accepted of 3 tasks" in text


def test_the_sample_is_not_kept_in_the_agent_writable_inbox_and_a_planted_link_is_left_alone(tmp_path):
    inbox = _inbox(tmp_path)
    victim = tmp_path / "victim.toml"
    victim.write_text("keep me")
    (inbox / "progress_sample.json").symlink_to(victim)
    (inbox / "impl.log").symlink_to(victim)
    assert progress.line(tmp_path, _attempt(), now=T0, reader=lambda *_: _tokens()) == "Progress: 350 tokens"
    assert victim.read_text() == "keep me"
    assert (tmp_path / progress.SAMPLES).is_file()


def test_a_blocking_token_read_cannot_stall_a_status_read(tmp_path, monkeypatch):
    import os
    import time

    from maestro.backends import registry
    from maestro.backends.claude import ClaudeBackend  # noqa: F401

    inbox = _inbox(tmp_path)
    os.mkfifo(inbox / "session_uuid.txt")
    monkeypatch.setattr(progress, "READ_SECONDS", 0.2)
    started = time.monotonic()
    assert progress.line(tmp_path, _attempt(), now=T0) is None
    assert time.monotonic() - started < 5
    assert registry.driver_name_for_backend_id("claude")


def test_the_real_driver_path_is_read(tmp_path, monkeypatch):
    from maestro.backends import registry

    _inbox(tmp_path)
    seen = []
    monkeypatch.setattr(registry, "get_backend", lambda name: SimpleNamespace(
        attempt_token_usage=lambda workspace: seen.append((name, workspace)) or _tokens()))
    assert progress.line(tmp_path, _attempt(backend_id="claude"), now=T0) == "Progress: 350 tokens"
    assert seen == [("claude", attempts_root(tmp_path) / "att_1")]


def test_agy_has_no_token_reader_so_no_line(tmp_path):
    _inbox(tmp_path)
    assert progress.line(tmp_path, _attempt(backend_id="antigravity_cli"), now=T0) is None


def _resume_state(repo: Path, **row) -> None:
    state = repo / ".orchestrator" / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"resume_state": {"T-1": row}}))


def test_progress_comes_from_the_last_gate_not_from_text_quoted_in_the_log(tmp_path):
    inbox = _inbox(tmp_path)
    (inbox / "impl.log").write_text('{"text": "the check prints PROGRESS=10/10"}\n')
    _resume_state(tmp_path, last_count=3, total=7)
    attempt = {**_attempt(), "task_id": "T-1"}
    assert progress.line(tmp_path, attempt, now=T0, reader=lambda *_: _tokens()) == (
        "Progress: 350 tokens · last gate PROGRESS 3/7")
    assert progress.line(tmp_path, attempt, now=T0, reader=lambda *_: None) == (
        "Progress: tokens unknown · last gate PROGRESS 3/7")


def test_a_broken_token_reader_keeps_the_gate_progress(tmp_path):
    _inbox(tmp_path)
    _resume_state(tmp_path, last_count=3, total=7)
    def broken(*_):
        raise RuntimeError("transcript moved")
    assert progress.line(tmp_path, {**_attempt(), "task_id": "T-1"}, now=T0, reader=broken) == (
        "Progress: tokens unknown · last gate PROGRESS 3/7")


def test_two_looks_within_a_minute_show_no_growth_and_keep_the_older_sample(tmp_path):
    _inbox(tmp_path)
    progress.line(tmp_path, _attempt(), now=T0, reader=lambda *_: _tokens())
    soon = T0 + timedelta(seconds=1)
    assert progress.line(tmp_path, _attempt(), now=soon, reader=lambda *_: _tokens(output=60)) == (
        "Progress: 360 tokens")
    later = T0 + timedelta(minutes=2)
    assert progress.line(tmp_path, _attempt(), now=later, reader=lambda *_: _tokens(output=70)) == (
        "Progress: 370 tokens (+20 in 2m)")


def test_old_samples_are_pruned(tmp_path):
    _inbox(tmp_path)
    _inbox(tmp_path, "att_2")
    progress.line(tmp_path, _attempt(), now=T0, reader=lambda *_: _tokens())
    later = T0 + timedelta(days=3)
    progress.line(tmp_path, _attempt("att_2"), now=later, reader=lambda *_: _tokens())
    assert set(json.loads((tmp_path / progress.SAMPLES).read_text())) == {"att_2"}


def test_the_latest_running_attempt_of_a_node_is_the_one_shown(tmp_path, monkeypatch):
    _inbox(tmp_path, "older")
    _inbox(tmp_path, "newer")
    def tokens(_binding, workspace):
        return _tokens(output=1) if workspace.name == "older" else _tokens(output=2)
    monkeypatch.setattr(progress, "_driver_tokens", tokens)
    view = _running_view(tmp_path, _attempt("older"))
    view["nodes"][0]["attempts"].append(_attempt("newer"))
    assert "Progress: 302 tokens" in status.render_graph_progress_text(view).splitlines()


def test_workflow_falls_back_on_any_failure_and_the_caption_is_redacted(
        workflow_command, tmp_path, monkeypatch):
    from maestro.hitl import shared_bot, telegram

    def broken(_view):
        raise AttributeError("no")

    monkeypatch.setattr(status, "render_workflow_html", broken)
    sent = workflow_command
    sent.document_ok = True
    sent.run("T-1")
    assert sent.reports == ["the text"]

    target = tmp_path / "w.html"
    target.write_text("<html/>")
    seen = {}
    def post(url, data=None, files=None, timeout=None):
        seen.update(data)
        return SimpleNamespace(ok=True, json=lambda: {"ok": True})
    secret = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"
    monkeypatch.setattr(shared_bot, "credentials", lambda: ("tok", "55"))
    monkeypatch.setattr(telegram.requests, "post", post)
    assert telegram.send_document(target, caption=f"key {secret}") is True
    assert secret not in seen["caption"]
