"""`scripts/graph_pilot_export.py`'s per-step quota section (Dan: usage per accepted task).

Per attempt: the 5h/weekly `used_pct` at claim and settle, whether each reading is a
fresh account read or a stale snapshot, the attempt's tokens, the raw window delta and
an *attributed* delta. Two attempts of one backend running at the same time share one
account window, so the raw delta of each double-counts the other; the attributed delta
splits every sampled segment of the window between the attempts active in it, in
proportion to their consumption (cost where the CLI reports one, else tokens) spread
evenly over each attempt's life. Anything that cannot be attributed is `null` with a
reason — never a number standing in for one (G6).
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

T0 = datetime(2026, 9, 26, 10, 0, 0, tzinfo=timezone.utc)
CLAUDE_SOURCE = "claude_oauth_usage_api"


def _module(tmp_path):
    """The export, with the harness session roots pointed at empty dirs (hermetic)."""
    path = Path(__file__).resolve().parents[2] / "scripts" / "graph_pilot_export.py"
    spec = importlib.util.spec_from_file_location("graph_pilot_export_quota", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.CLAUDE_PROJECTS_ROOT = tmp_path / "claude-projects"
    module.CODEX_SESSIONS_ROOT = tmp_path / "codex-sessions"
    module.CLAUDE_PROJECTS_ROOT.mkdir(exist_ok=True)
    module.CODEX_SESSIONS_ROOT.mkdir(exist_ok=True)
    return module


def _z(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _reading(at, five, week=40.0, *, source=CLAUDE_SOURCE, five_reset=2_000_000_000,
             tokens=None, error=None):
    windows = {} if five is None else {
        "300": {"used_pct": five, "resets_at": five_reset},
        "10080": {"used_pct": week, "resets_at": 2_000_500_000},
    }
    body = {"windows": windows, "updated_at": _z(at), "read_at": _z(at + 5),
            "source": source, "error": error, "token_split": tokens, "model": None}
    if source is None:  # a pre-telemetry row: no provenance keys at all
        for key in ("source", "error", "read_at"):
            body.pop(key)
        body["model"] = "Opus 5.5"
    return body


def _attempt(attempt_id, start, end, *, node="implementer", backend="claude",
             model="claude-sonnet-5", start_reading=None, settle_reading=None):
    return {
        "attempt_id": attempt_id, "node_id": node, "status": "succeeded",
        "started_at": _z(start), "backend_id": backend, "model_id": model,
        "effort": "medium", "wall_time_sec": float(end - start),
        "usage_start": start_reading, "usage_settle": settle_reading,
        "impl_log": None, "transcript": None,
    }


def _tasks(*attempts, status="accepted"):
    return {"T1": {"status": status, "runs": [{"run_id": "r1", "attempts": list(attempts)}]}}


def _tok(cost=None, output=100):
    return {"fresh_input": 10, "cache_read": 1000, "cache_write": 50, "output": output,
            "cost_usd": cost, "models": ["m"], "source": "claude_cost_state"}


def test_a_lone_fresh_attempt_is_charged_its_whole_window_delta(tmp_path):
    export = _module(tmp_path)
    attempt = _attempt("a", 0, 600, start_reading=_reading(0, 10.0, 40.0),
                       settle_reading=_reading(600, 14.0, 41.0, tokens=_tok(cost=1.0)))
    gate = _attempt("g", 600, 620, node="gate", backend=None, model=None)
    report = export.quota_report(_tasks(attempt, gate), tmp_path)

    assert "quota" not in gate, "a controller step spends no subscription"
    assert set(report["by_role"]) == {"implementer"}
    quota = attempt["quota"]
    assert quota["readings"]["start"]["freshness"] == "fresh"
    assert quota["raw_delta"] == {"five_hour": 4.0, "seven_day": 1.0, "reasons": {}}
    assert quota["attributed_delta"]["five_hour"] == pytest.approx(4.0)
    assert quota["attributed_delta"]["seven_day"] == pytest.approx(1.0)
    assert quota["tokens"]["output"] == 100
    assert quota["tokens_source"] == "settle_telemetry"
    claude = report["per_task"]["T1"]["claude"]
    assert claude["attributed_five_hour_pct"] == pytest.approx(4.0)
    assert report["by_role"]["implementer"]["claude"]["attempts"] == 1


def test_overlapping_attempts_split_each_segment_by_their_consumption(tmp_path):
    """A [0,600] costs 1, B [300,900] costs 3. Samples 10 → 12 → 16 → 18.
    [0,300] is A's alone (2); [300,600] splits 4 at A 0.5 : B 1.5 (1 and 3);
    [600,900] is B's alone (2). A = 3, B = 5, and together they are the whole 8."""
    export = _module(tmp_path)
    a = _attempt("a", 0, 600, start_reading=_reading(0, 10.0),
                 settle_reading=_reading(600, 16.0, tokens=_tok(cost=1.0)))
    b = _attempt("b", 300, 900, node="proof_review", model="claude-opus-5",
                 start_reading=_reading(300, 12.0),
                 settle_reading=_reading(900, 18.0, tokens=_tok(cost=3.0)))
    report = export.quota_report(_tasks(a, b), tmp_path)

    assert a["quota"]["raw_delta"]["five_hour"] == 6.0
    assert a["quota"]["attributed_delta"]["five_hour"] == pytest.approx(3.0)
    assert b["quota"]["attributed_delta"]["five_hour"] == pytest.approx(5.0)
    assert a["quota"]["attributed_delta"]["weight_basis"] == ["cost_usd", "sole"]
    assert a["quota"]["attributed_delta"]["overlapping_attempts"] == ["b"]
    assert report["per_task"]["T1"]["claude"]["attributed_five_hour_pct"] == pytest.approx(8.0)
    assert report["by_route"]["proof_review|claude|claude-opus-5|medium"]["attempts"] == 1


def test_backends_are_separate_accounts_and_never_share_a_window(tmp_path):
    export = _module(tmp_path)
    a = _attempt("a", 0, 600, start_reading=_reading(0, 10.0),
                 settle_reading=_reading(600, 16.0, tokens=_tok(cost=1.0)))
    c = _attempt("c", 0, 600, backend="codex", model="gpt-5.6-sol",
                 start_reading=_reading(0, 50.0, source="codex_app_server"),
                 settle_reading=_reading(600, 70.0, source="codex_app_server"))
    report = export.quota_report(_tasks(a, c), tmp_path)

    assert a["quota"]["attributed_delta"]["five_hour"] == pytest.approx(6.0)
    assert c["quota"]["attributed_delta"]["five_hour"] == pytest.approx(20.0)
    assert set(report["per_task"]["T1"]) == {"claude", "codex"}


def test_a_statusline_snapshot_is_marked_stale_and_yields_no_numbers(tmp_path):
    """DuetFlow 02 run 9: the interactive session's snapshot, identical at both ends."""
    export = _module(tmp_path)
    attempt = _attempt("a", 0, 600, start_reading=_reading(0, 75.0, source=None),
                       settle_reading=_reading(0, 75.0, source=None))
    report = export.quota_report(_tasks(attempt), tmp_path)

    quota = attempt["quota"]
    assert quota["readings"]["start"]["freshness"] == "stale"
    assert "statusline" in quota["readings"]["start"]["reason"]
    assert quota["raw_delta"]["five_hour"] is None
    assert quota["attributed_delta"]["five_hour"] is None
    assert quota["attributed_delta"]["reasons"]["five_hour"]
    claude = report["per_task"]["T1"]["claude"]
    assert claude["attributed_five_hour_pct"] is None
    assert claude["attempts_unattributed_five_hour"] == 1


def test_a_failed_read_is_unavailable_with_its_reason(tmp_path):
    export = _module(tmp_path)
    attempt = _attempt("a", 0, 600,
                       start_reading=_reading(0, None, error="fetch failed: URLError"),
                       settle_reading=_reading(600, 14.0))
    export.quota_report(_tasks(attempt), tmp_path)

    start = attempt["quota"]["readings"]["start"]
    assert start["freshness"] == "unavailable"
    assert start["reason"] == "fetch failed: URLError"
    assert attempt["quota"]["raw_delta"]["five_hour"] is None


def test_a_window_that_reset_mid_attempt_is_not_attributed(tmp_path):
    export = _module(tmp_path)
    attempt = _attempt("a", 0, 600, start_reading=_reading(0, 90.0),
                       settle_reading=_reading(600, 3.0, five_reset=2_000_018_000))
    export.quota_report(_tasks(attempt), tmp_path)

    delta = attempt["quota"]["attributed_delta"]
    assert delta["five_hour"] is None and "reset" in delta["reasons"]["five_hour"]
    assert delta["seven_day"] == pytest.approx(0.0), "the weekly window did not reset"


def test_an_overlapping_attempt_without_a_count_makes_the_split_unknowable(tmp_path):
    export = _module(tmp_path)
    a = _attempt("a", 0, 600, start_reading=_reading(0, 10.0),
                 settle_reading=_reading(600, 16.0, tokens=_tok(cost=1.0)))
    b = _attempt("b", 300, 900)  # no readings and no tokens: still a consumer
    export.quota_report(_tasks(a, b), tmp_path)

    delta = a["quota"]["attributed_delta"]
    assert delta["five_hour"] is None
    assert "b" in delta["reasons"]["five_hour"]
    assert b["quota"]["tokens"] is None


def test_an_attempt_inside_one_cached_sample_is_unmeasured_not_zero(tmp_path):
    """Claim and settle within the 60 s Claude read throttle read the same sample: the
    attempt's own movement was never observed, which is not the same as 0."""
    export = _module(tmp_path)
    attempt = _attempt("a", 0, 30, start_reading=_reading(0, 10.0),
                       settle_reading=_reading(0, 10.0, tokens=_tok(cost=1.0)))
    export.quota_report(_tasks(attempt), tmp_path)

    quota = attempt["quota"]
    assert quota["raw_delta"]["five_hour"] is None
    assert quota["attributed_delta"]["five_hour"] is None
    assert "one cached sample" in quota["attributed_delta"]["reasons"]["five_hour"]


# ── contamination: another session of the same harness ran during the attempt ─────────
#
# The account window moves for every session on the account. When a session outside this
# project's `.orchestrator` (Dan's interactive work, maestro development, another project)
# was active during an attempt of the same harness, that attempt's delta includes it.


def _ms(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.123Z")


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    import json
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    stamp = (T0 + timedelta(seconds=5000)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def _claude_session(root: Path, project_dir: str, name: str, *seconds, kind="assistant"):
    rows = [{"type": "user", "timestamp": _ms(s - 1)} for s in seconds]
    rows += [{"type": kind, "timestamp": _ms(s)} for s in seconds]
    return _write_jsonl(root / project_dir / name, rows)


def _codex_session(root: Path, name: str, cwd: str, *seconds):
    rows = [{"timestamp": _ms(0), "type": "session_meta", "payload": {"id": name, "cwd": cwd}}]
    rows += [{"timestamp": _ms(s), "type": "event_msg",
              "payload": {"type": "agent_message"}} for s in seconds]
    rows += [{"timestamp": _ms(s), "type": "event_msg",
              "payload": {"type": "token_count"}} for s in seconds]
    return _write_jsonl(root / "2026" / "09" / "26" / f"rollout-{name}.jsonl", rows)


def _orch(tmp_path) -> Path:
    orch = tmp_path / "proj" / ".orchestrator"
    orch.mkdir(parents=True)
    return orch


def _encoded(path: Path) -> str:
    return "".join(c if c.isalnum() else "-" for c in str(path))


def test_a_foreign_claude_session_active_during_the_attempt_contaminates_it(tmp_path):
    export = _module(tmp_path)
    orch = _orch(tmp_path)
    root = export.CLAUDE_PROJECTS_ROOT
    ours = _encoded(orch / "worktrees" / "T1")
    _claude_session(root, ours, "own.jsonl", 100, 200)
    _claude_session(root, ours, "own/subagents/agent-1.jsonl", 150)
    _claude_session(root, "-home-dan-projects-maestro", "dev.jsonl", 300, 400, 900)
    _claude_session(root, "-home-dan-projects-maestro", "dev/subagents/agent-x.jsonl", 350)
    _claude_session(root, "-home-dan-other", "late.jsonl", 700)  # after the attempt
    _claude_session(root, "-home-dan-other", "chat.jsonl", 300, kind="user")  # no reply
    a = _attempt("a", 0, 600, start_reading=_reading(0, 10.0),
                 settle_reading=_reading(600, 14.0, tokens=_tok(cost=1.0)))
    report = export.quota_report(_tasks(a), orch)

    contamination = a["quota"]["contamination"]
    assert contamination["contaminated"] is True
    assert contamination["foreign_sessions"] == [
        {"id": "dev", "where": "-home-dan-projects-maestro", "activity_count": 3}]
    assert "claude" in contamination["reason"]
    claude = report["per_task"]["T1"]["claude"]
    assert claude["attempts_contaminated"] == 1
    assert claude["attempts_contamination_unmeasurable"] == 0
    assert report["contamination"]["claude"]["contaminated"] == 1


def test_only_this_projects_own_sessions_leave_the_attempt_clean(tmp_path):
    export = _module(tmp_path)
    orch = _orch(tmp_path)
    _claude_session(export.CLAUDE_PROJECTS_ROOT, _encoded(orch / "worktrees" / "T1"),
                    "own.jsonl", 100, 200)
    # a sibling path that merely starts with the same characters is still foreign
    _claude_session(export.CLAUDE_PROJECTS_ROOT, _encoded(orch) + "x", "near.jsonl", 900)
    a = _attempt("a", 0, 600, start_reading=_reading(0, 10.0),
                 settle_reading=_reading(600, 14.0, tokens=_tok(cost=1.0)))
    report = export.quota_report(_tasks(a), orch)

    assert a["quota"]["contamination"] == {
        "contaminated": False, "foreign_sessions": [],
        "reason": "no other claude session was active during the attempt"}
    assert report["per_task"]["T1"]["claude"]["attempts_contaminated"] == 0
    assert report["contamination"]["claude"]["clean"] == 1


def test_a_foreign_codex_session_contaminates_only_codex_attempts(tmp_path):
    export = _module(tmp_path)
    orch = _orch(tmp_path)
    root = export.CODEX_SESSIONS_ROOT
    _codex_session(root, "ours", str(orch / "worktrees" / "T1"), 100, 200)
    _codex_session(root, "dan", "/home/dan/projects/other", 250, 260)
    c = _attempt("c", 0, 600, backend="codex", model="gpt-5.6-sol",
                 start_reading=_reading(0, 50.0, source="codex_app_server"),
                 settle_reading=_reading(600, 70.0, source="codex_app_server"))
    a = _attempt("a", 0, 600, start_reading=_reading(0, 10.0),
                 settle_reading=_reading(600, 14.0, tokens=_tok(cost=1.0)))
    export.quota_report(_tasks(c, a), orch)

    assert c["quota"]["contamination"]["contaminated"] is True
    assert c["quota"]["contamination"]["foreign_sessions"] == [
        {"id": "dan", "where": "/home/dan/projects/other", "activity_count": 2}]
    assert a["quota"]["contamination"]["contaminated"] is False


def test_a_missing_session_root_is_not_measurable_not_clean(tmp_path):
    export = _module(tmp_path)
    export.CODEX_SESSIONS_ROOT = tmp_path / "no-such-dir"
    orch = _orch(tmp_path)
    c = _attempt("c", 0, 600, backend="codex", model="gpt-5.6-sol",
                 start_reading=_reading(0, 50.0, source="codex_app_server"),
                 settle_reading=_reading(600, 70.0, source="codex_app_server"))
    report = export.quota_report(_tasks(c), orch)

    contamination = c["quota"]["contamination"]
    assert contamination["contaminated"] is None
    assert contamination["foreign_sessions"] == []
    assert "no-such-dir" in contamination["reason"]
    codex = report["per_task"]["T1"]["codex"]
    assert codex["attempts_contaminated"] == 0
    assert codex["attempts_contamination_unmeasurable"] == 1
    assert report["contamination"]["codex"]["not_measurable"] == 1
