#!/usr/bin/env python3
"""Export a P12 pilot project's raw per-attempt outcomes (P12 §2) to JSON.

    scripts/graph_pilot_export.py --project /home/dan/projects/duetflow \
        --out artifacts/graph-engineering/p12-pilots/duetflow.json

Read-only: the control DB is opened `mode=ro`, and nothing under `.orchestrator` is
written. A value that cannot be measured is `null`, never estimated or zeroed (G6). The
shape is this report's own; it is deliberately not the Context Gate's JSONL schema.

Per attempt: the `attempts` row (model, agent version, session, fresh/resumed, quota
windows at start and settle, wall time, reset crossing, failure kind/class), its
`impl.log` path, and the Claude transcript the attempt ran in with the token split summed
from it (one count per assistant message id). Per task: runs, rework (implementer
attempts beyond the first, summed over runs), reopens (spec revisions beyond the first),
verification evidence, acceptance decisions, and the journal's human interventions, and
(Dan, 2026-09-26) each review round's blocking findings counted by origin and by the role
at fault (`maestro.workflows.review_origins`), `null` for a review adopted before findings
carried origins.
"""
from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# The fault table lives in one place, in maestro; this script runs from a checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from maestro.workflows import review_origins  # noqa: E402

#: Journal events that are a human acting on the loop.
HUMAN_EVENTS = (
    "dan_verify_decided",
    "control_resume",
    "control_pause",
    "control_halt",
    "control_approve",
    "hitl_",
    "telegram_command",
)
_TOKEN_KEYS = {
    "fresh_input": "input_tokens",
    "cache_read": "cache_read_input_tokens",
    "cache_write": "cache_creation_input_tokens",
    "output": "output_tokens",
}


def _json(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _transcript_dirs(worktree: Path) -> list[Path]:
    """Claude Code keeps a session's transcript under its cwd, `/` and `.` mapped to `-`."""
    root = Path.home() / ".claude" / "projects"
    name = str(worktree).replace("/", "-").replace(".", "-")
    return [p for p in (root / name,) if p.is_dir()]


def _find_transcript(attempt_id: str, worktrees: list[Path]) -> Path | None:
    """The transcript whose opening prompt names this attempt's workspace."""
    needle = f"/attempts/{attempt_id}"
    for worktree in worktrees:
        for directory in _transcript_dirs(worktree):
            for path in sorted(directory.glob("*.jsonl")):
                try:
                    with path.open(encoding="utf-8") as handle:
                        head = "".join(handle.readline() for _ in range(5))
                except OSError:
                    continue
                if needle in head:
                    return path
    return None


def _transcript_summary(path: Path | None) -> dict | None:
    if path is None:
        return None
    split = {key: 0 for key in _TOKEN_KEYS}
    seen: set = set()
    models: set = set()
    stamps: list[str] = []
    turns = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("timestamp"):
                stamps.append(row["timestamp"])
            message = row.get("message")
            if row.get("type") != "assistant" or not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not usage or message.get("id") in seen:
                continue
            seen.add(message.get("id"))
            turns += 1
            if message.get("model"):
                models.add(message["model"])
            for ours, theirs in _TOKEN_KEYS.items():
                split[ours] += int(usage.get(theirs) or 0)
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "models": sorted(models),
        "assistant_turns": turns,
        "first_at": stamps[0] if stamps else None,
        "last_at": stamps[-1] if stamps else None,
        "token_split": split if turns else None,
    }


def _review(result_path: Path) -> dict | None:
    """A reviewer attempt's verdict, blocking findings and follow-ups, from its result."""
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    review = (result.get("outputs") or {}).get("review") if isinstance(result, dict) else None
    if not isinstance(review, dict):
        return None
    return {k: review.get(k) for k in ("verdict", "reviewer_verdict", "findings", "follow_ups",
                                       "round", "finding_records", "origin_flags")
            if review.get(k) is not None}


#: The verdicts that make a review a round (`validation.REVIEW_VERDICTS`); a review that
#: reached none failed closed and was re-asked, as `WorkflowRunner.review_rounds` counts.
_ROUND_VERDICTS = ("approved", "rejected", "changes_requested")


def _review_origins(reviews: list[dict]) -> dict:
    """A task's review rounds (oldest first), each counted by origin and by fault."""
    by_round: dict[str, dict | None] = {}
    total: list[dict] = []
    measured = False
    number = 0
    for review in reviews:
        if str(review.get("verdict") or "").strip().lower() not in _ROUND_VERDICTS:
            continue
        number += 1
        key = str(review.get("round") or number)
        records = review.get("finding_records")
        if not isinstance(records, list):
            by_round[key] = None        # adopted before findings carried origins (G6)
            continue
        measured = True
        by_round[key] = review_origins.count(records)
        total += records
    return {
        "by_round": by_round,
        "total": review_origins.count(total) if measured else None,
    }


def _journal(repo: Path, since: str | None) -> list[dict]:
    path = repo / ".orchestrator" / "journal.ndjson"
    rows = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if since and str(row.get("ts", "")) < since:
            continue
        rows.append(row)
    return rows


# ── per-step subscription usage (5h / weekly quota per attempt) ──────────────────────────
#
# Kept apart from the rest of the export on purpose (it is post-processing over the
# finished `tasks` map), so the review-finding columns and this section merge cleanly.
#
# **Attribution is computed here, at export time, not at settle.** A settle cannot know
# which attempts will still overlap it; the export sees every attempt of the project.
# Per backend (one subscription account each, never mixed) and per window:
#   1. every *fresh* reading of every attempt of that backend is a sample point
#      (its own `updated_at` — the moment the account was actually read);
#   2. between consecutive points the window moved by `delta`; that delta is split
#      between the attempts whose activity interval overlaps the segment, each in
#      proportion to `weight / duration × overlap` — its consumption spread evenly over
#      its life. The weight is the CLI's cost estimate when every overlapping attempt has
#      one (Claude's `cost-state`: the best relative weight across models), else total
#      tokens, and needs none when the attempt ran alone;
#   3. an attempt's attributed delta is the sum over the segments inside its own
#      start..settle samples. A segment across a window reset, or shared with an attempt
#      that has no count, makes that window `null` with a reason.
# Caveats that stay caveats: the account also moves for work outside this project (Dan's
# interactive sessions, other projects), which lands on whichever attempts were running —
# flagged per attempt as `quota.contamination` when another session of the same harness
# left activity in the local session stores during the attempt (`_foreign_activity`;
# `contaminated: null` when that store is missing), counted in `attempts_contaminated`;
# percentages are whole numbers, so a short step reads 0 or 1; the Claude read is cached
# for up to 60 s, which shifts a sample by at most that much.

#: A reading older than this when the controller took it is stale.
FRESH_MAX_AGE_SEC = 180
#: Sources that read the account at claim/settle time. The statusline's `usage.json`
#: is not one: only the interactive session writes it, on its own renders.
FRESH_SOURCES = ("claude_oauth_usage_api", "codex_app_server", "codex_rollout")
STATUSLINE_REASON = (
    "statusline snapshot (.orchestrator/usage.json) written by the interactive session, "
    "not re-read per attempt"
)
#: Two `resets_at` further apart than this are two different windows.
RESET_TOLERANCE_SEC = 600
QUOTA_WINDOWS = {"five_hour": "300", "seven_day": "10080"}
_QUOTA_TOKEN_KEYS = ("fresh_input", "cache_read", "cache_write", "output")


#: The harnesses' local session stores, read to flag contamination (injectable for tests).
CLAUDE_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
CODEX_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"


def _iso_epoch(stamp) -> float | None:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _jsonl(path: Path):
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            yield from handle
    except OSError:
        return


def _claude_sessions(root: Path, orch: Path, since: float) -> dict:
    """Foreign Claude sessions -> activity epochs. Activity is an assistant message; a
    session is ours when its project dir encodes a path under `orch` (Claude names a
    project dir after its cwd with every non-alphanumeric turned into `-`)."""
    ours = "".join(c if c.isalnum() else "-" for c in str(orch))
    found: dict[tuple, list] = {}
    for path in root.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime < since:
                continue
        except OSError:
            continue
        rel = path.relative_to(root).parts
        if len(rel) < 2 or rel[0] == ours or rel[0].startswith(ours + "-"):
            continue
        key = (rel[1].removesuffix(".jsonl"), rel[0])  # a subagent counts as its session
        for line in _jsonl(path):
            if '"assistant"' not in line:
                continue
            row = _json(line)
            if isinstance(row, dict) and row.get("type") == "assistant":
                t = _iso_epoch(row.get("timestamp"))
                if t is not None:
                    found.setdefault(key, []).append(t)
    return found


def _codex_sessions(root: Path, orch: Path, since: float) -> dict:
    """Foreign Codex sessions -> activity epochs. Activity is a `token_count` event; a
    session is ours when its `session_meta` cwd is under `orch`."""
    found: dict[tuple, list] = {}
    for path in root.rglob("rollout-*.jsonl"):
        try:
            if path.stat().st_mtime < since:
                continue
        except OSError:
            continue
        key, times = None, []
        for number, line in enumerate(_jsonl(path)):
            if number == 0:
                meta = _json(line)
                payload = meta.get("payload") if isinstance(meta, dict) else None
                cwd = str((payload or {}).get("cwd") or "")
                if cwd and (Path(cwd) == orch or orch in Path(cwd).parents):
                    break
                key = (str((payload or {}).get("id") or path.stem), cwd or "?")
                continue
            if '"token_count"' not in line:
                continue
            row = _json(line)
            if isinstance(row, dict) and (row.get("payload") or {}).get("type") == "token_count":
                t = _iso_epoch(row.get("timestamp"))
                if t is not None:
                    times.append(t)
        if key and times:
            found.setdefault(key, []).extend(times)
    return found


def _foreign_activity(backend: str, orch: Path, since: float) -> tuple[dict | None, str]:
    """Every session of `backend` outside this project, with its sorted activity epochs,
    or None and the reason it cannot be read."""
    readers = {"claude": (CLAUDE_PROJECTS_ROOT, _claude_sessions),
               "codex": (CODEX_SESSIONS_ROOT, _codex_sessions)}
    if backend not in readers:
        return None, f"no session store is read for backend {backend!r}"
    root, reader = readers[backend]
    if not Path(root).is_dir():
        return None, f"{backend} session store {root} not found"
    sessions = reader(Path(root), orch, since)
    return {key: sorted(times) for key, times in sessions.items()}, ""


def _contamination(entry: dict, foreign: tuple[dict | None, str]) -> dict:
    sessions, reason = foreign
    out = {"contaminated": None, "foreign_sessions": [], "reason": reason}
    if sessions is None:
        return out
    if entry["interval"] is None:
        out["reason"] = "the attempt's interval is unknown"
        return out
    t0, t1 = entry["interval"]
    for (sid, where), times in sorted(sessions.items()):
        count = bisect.bisect_right(times, t1) - bisect.bisect_left(times, t0)
        if count:
            out["foreign_sessions"].append(
                {"id": sid, "where": where, "activity_count": count})
    backend = entry["backend"]
    out["contaminated"] = bool(out["foreign_sessions"])
    out["reason"] = (
        f"{len(out['foreign_sessions'])} other {backend} session(s) active during the attempt"
        if out["contaminated"] else f"no other {backend} session was active during the attempt")
    return out


def _epoch(stamp) -> float | None:
    try:
        return datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _reading_view(reading, backend: str, reference: float | None) -> dict:
    """One claim/settle reading, with its freshness judged and the reason when not fresh."""
    view = {"five_hour_pct": None, "seven_day_pct": None, "five_hour_resets_at": None,
            "seven_day_resets_at": None, "sampled_at": None, "read_at": None,
            "source": None, "freshness": "unavailable", "reason": None}
    if not isinstance(reading, dict):
        view["reason"] = "no usage sample recorded"
        return view
    windows = reading.get("windows") or {}
    for name, key in QUOTA_WINDOWS.items():
        window = windows.get(key) or {}
        view[f"{name}_pct"] = window.get("used_pct")
        view[f"{name}_resets_at"] = window.get("resets_at")
    view["sampled_at"] = reading.get("updated_at") or None
    view["read_at"] = reading.get("read_at")
    legacy = "source" not in reading
    source = reading.get("source")
    if legacy and backend == "claude":
        source = "statusline_usage_json"
    view["source"] = source or ("unrecorded (pre-provenance row)" if legacy else None)
    if source == "statusline_usage_json":
        view.update(freshness="stale", reason=STATUSLINE_REASON)
        return view
    if reading.get("error") or not windows:
        view["reason"] = reading.get("error") or "the sample has no windows"
        return view
    if not legacy and source not in FRESH_SOURCES:
        view.update(freshness="stale", reason=f"source {source!r} is not a fresh account read")
        return view
    sampled = _epoch(view["sampled_at"])
    read = _epoch(view["read_at"]) if view["read_at"] else reference
    if sampled is None or read is None:
        view.update(freshness="stale", reason="the sample time is unknown")
        return view
    age = read - sampled
    if age > FRESH_MAX_AGE_SEC:
        view.update(freshness="stale", reason=f"the sample was {int(age)} s old when read")
        return view
    view["freshness"] = "fresh"
    return view


def _quota_token_reader(backend: str):
    """The driver's own per-attempt token reader, for an attempt settled before settle
    recorded tokens. Imported lazily: the rest of the export needs no maestro import."""
    repo = Path(__file__).resolve().parents[1]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    try:
        if backend == "claude":
            from maestro.backends.claude import ClaudeBackend
            return ClaudeBackend().attempt_token_usage
        if backend == "codex":
            from maestro.backends.codex import CodexBackend
            return CodexBackend().attempt_token_usage
    except Exception:  # noqa: BLE001 — no reader is "not measurable", not an export failure
        return None
    return None


def _quota_tokens(att: dict, orch: Path) -> tuple[dict | None, str | None]:
    settle = att.get("usage_settle") if isinstance(att.get("usage_settle"), dict) else {}
    split = settle.get("token_split")
    if isinstance(split, dict) and split.get("source"):
        return split, "settle_telemetry"
    reader = _quota_token_reader(att.get("backend_id") or "")
    try:
        found = reader(orch / "attempts" / att["attempt_id"]) if reader else None
        if found is None and att.get("backend_id") == "claude" and att.get("transcript"):
            from maestro.backends.claude import session_token_totals
            found = session_token_totals(Path(att["transcript"]["path"]))
    except Exception:  # noqa: BLE001
        found = None
    return (found, "session_files") if found else (None, None)


def _quota_weight(tokens: dict | None, basis: str) -> float | None:
    if not tokens:
        return None
    if basis == "cost_usd":
        cost = tokens.get("cost_usd")
        return float(cost) if isinstance(cost, (int, float)) else None
    return float(sum(tokens.get(key) or 0 for key in _QUOTA_TOKEN_KEYS))


def _quota_interval(att: dict, start: dict, settle: dict) -> tuple[float, float] | None:
    """When the attempt was consuming: its own samples when both are fresh (so it always
    covers its own segments), else claim .. claim + wall time."""
    if start["freshness"] == settle["freshness"] == "fresh":
        a, b = _epoch(start["sampled_at"]), _epoch(settle["sampled_at"])
        if a is not None and b is not None and b >= a:
            return a, b
    begun = _epoch(att.get("started_at"))
    wall = att.get("wall_time_sec")
    if begun is None or wall is None:
        return None
    return begun, begun + float(wall)


#: An attempt shorter than the Claude read throttle claims and settles on one sample.
SHARED_SAMPLE_REASON = "start and settle share one cached sample (shorter than the read throttle)"


def _unmeasured(start: dict, settle: dict) -> str | None:
    if start["freshness"] != "fresh" or settle["freshness"] != "fresh":
        return "start or settle reading is not fresh"
    if start["sampled_at"] == settle["sampled_at"]:
        return SHARED_SAMPLE_REASON
    return None


def _raw_delta(start: dict, settle: dict) -> dict:
    out = {"five_hour": None, "seven_day": None, "reasons": {}}
    for name in QUOTA_WINDOWS:
        if _unmeasured(start, settle):
            out["reasons"][name] = _unmeasured(start, settle)
            continue
        a, b = start[f"{name}_pct"], settle[f"{name}_pct"]
        if a is None or b is None:
            out["reasons"][name] = "window not reported"
            continue
        ra, rb = start[f"{name}_resets_at"], settle[f"{name}_resets_at"]
        if ra is not None and rb is not None and abs(int(rb) - int(ra)) > RESET_TOLERANCE_SEC:
            out["reasons"][name] = "the window reset during the attempt"
            continue
        out[name] = float(b) - float(a)
    return out


def _attribute(entry: dict, peers: list[dict], points: dict) -> dict:
    """The segment split described at the top of this section, for one attempt."""
    out = {"five_hour": None, "seven_day": None, "reasons": {}, "weight_basis": [],
           "overlapping_attempts": []}
    reason = _unmeasured(entry["start"], entry["settle"])
    if reason:
        for name in QUOTA_WINDOWS:
            out["reasons"][name] = reason
        return out
    t0, t1 = entry["interval"]
    bases: set = set()
    overlapping: set = set()
    for name in QUOTA_WINDOWS:
        series = [p for p in points[name] if t0 <= p[0] <= t1]
        total, reason = 0.0, None
        for (a_t, a_pct, a_reset), (b_t, b_pct, b_reset) in zip(series, series[1:]):
            if b_t <= a_t:
                continue
            if a_reset is not None and b_reset is not None and \
                    abs(int(b_reset) - int(a_reset)) > RESET_TOLERANCE_SEC:
                reason = "the window reset during the attempt"
                break
            active = []
            for peer in peers:
                if peer["interval"] is None:
                    continue
                lap = min(b_t, peer["interval"][1]) - max(a_t, peer["interval"][0])
                if lap > 0:
                    active.append((peer, lap))
            if not any(peer is entry for peer, _ in active):
                continue  # a gap in this attempt's own activity: not its consumption
            overlapping.update(p["id"] for p, _ in active if p is not entry)
            if len(active) == 1:
                bases.add("sole")
                total += b_pct - a_pct
                continue
            basis = next((b for b in ("cost_usd", "tokens") if all(
                _quota_weight(p["tokens"], b) is not None for p, _ in active)), None)
            if basis is None:
                missing = sorted(p["id"] for p, _ in active if not p["tokens"])
                reason = f"overlapping attempt(s) without a token count: {', '.join(missing)}"
                break
            bases.add(basis)
            shares = {}
            for peer, lap in active:
                span = max(peer["interval"][1] - peer["interval"][0], 1.0)
                shares[peer["id"]] = _quota_weight(peer["tokens"], basis) / span * lap
            whole = sum(shares.values())
            if whole <= 0:
                reason = "overlapping attempts report zero consumption"
                break
            total += (b_pct - a_pct) * shares[entry["id"]] / whole
        if reason:
            out["reasons"][name] = reason
        elif len(series) < 2:
            out["reasons"][name] = "fewer than two samples inside the attempt"
        else:
            out[name] = total
    out["weight_basis"] = sorted(bases)
    out["overlapping_attempts"] = sorted(overlapping)
    return out


def _quota_totals() -> dict:
    return {"attempts": 0, "attempts_with_tokens": 0, "tokens": dict.fromkeys(
        _QUOTA_TOKEN_KEYS, 0), "cost_usd": None, "wall_time_sec": 0.0,
        "attributed_five_hour_pct": None, "attributed_seven_day_pct": None,
        "attempts_unattributed_five_hour": 0, "attempts_unattributed_seven_day": 0,
        "attempts_contaminated": 0, "attempts_contamination_unmeasurable": 0}


def _add_quota(totals: dict, quota: dict) -> None:
    totals["attempts"] += 1
    totals["wall_time_sec"] += float(quota["wall_time_sec"] or 0)
    if quota["tokens"]:
        totals["attempts_with_tokens"] += 1
        for key in _QUOTA_TOKEN_KEYS:
            totals["tokens"][key] += int(quota["tokens"].get(key) or 0)
        cost = quota["tokens"].get("cost_usd")
        if isinstance(cost, (int, float)):
            totals["cost_usd"] = (totals["cost_usd"] or 0.0) + float(cost)
    flag = quota["contamination"]["contaminated"]
    if flag is None:
        totals["attempts_contamination_unmeasurable"] += 1
    elif flag:
        totals["attempts_contaminated"] += 1
    for name in QUOTA_WINDOWS:
        value = quota["attributed_delta"][name]
        if value is None:
            totals[f"attempts_unattributed_{name}"] += 1
        else:
            key = f"attributed_{name}_pct"
            totals[key] = (totals[key] or 0.0) + value


def quota_report(tasks: dict, orch: Path) -> dict:
    """Adds `quota` to every attempt in `tasks`; returns the per-task/role/route totals.

    Totals are per backend: a Claude percent and a Codex percent are of different
    accounts and are never added together. `attributed_*_pct` sums only attributed
    attempts; `attempts_unattributed_*` counts the rest, so a partial sum is visible.
    """
    entries = []
    for tid, task in tasks.items():
        for run in task.get("runs") or ():
            for att in run.get("attempts") or ():
                backend = att.get("backend_id")
                if not backend:
                    continue  # a controller step (gate, merge, accept): no subscription
                begun = _epoch(att.get("started_at"))
                wall = att.get("wall_time_sec")
                start = _reading_view(att.get("usage_start"), backend, begun)
                settle = _reading_view(att.get("usage_settle"), backend,
                                       None if begun is None or wall is None
                                       else begun + float(wall))
                tokens, source = _quota_tokens(att, orch)
                entries.append({"id": att["attempt_id"], "task": tid, "att": att,
                                "backend": backend, "start": start, "settle": settle,
                                "tokens": tokens, "tokens_source": source,
                                "interval": _quota_interval(att, start, settle)})
    by_backend: dict[str, list] = {}
    for entry in entries:
        by_backend.setdefault(entry["backend"], []).append(entry)
    report = {"method": "per-backend segment split; see scripts/graph_pilot_export.py",
              "per_task": {}, "by_role": {}, "by_route": {}, "per_accepted_task": {},
              "contamination": {}}
    for backend, peers in by_backend.items():
        starts = [e["interval"][0] for e in peers if e["interval"] is not None]
        foreign = _foreign_activity(backend, orch, min(starts)) if starts else ({}, "")
        summary = report["contamination"][backend] = {
            "contaminated": 0, "clean": 0, "not_measurable": 0, "foreign_sessions": {}}
        points = {}
        for name, key in QUOTA_WINDOWS.items():
            seen: dict[float, tuple] = {}
            for entry in peers:
                for side in ("start", "settle"):
                    view = entry[side]
                    t = _epoch(view["sampled_at"]) if view["freshness"] == "fresh" else None
                    if t is not None and view[f"{name}_pct"] is not None and t not in seen:
                        seen[t] = (t, float(view[f"{name}_pct"]), view[f"{name}_resets_at"])
            points[name] = sorted(seen.values())
        for entry in peers:
            att = entry["att"]
            att["quota"] = {
                "role": att.get("node_id"), "backend": backend, "model": att.get("model_id"),
                "effort": att.get("effort"), "wall_time_sec": att.get("wall_time_sec"),
                "tokens": entry["tokens"], "tokens_source": entry["tokens_source"],
                "readings": {"start": entry["start"], "settle": entry["settle"]},
                "raw_delta": _raw_delta(entry["start"], entry["settle"]),
                "attributed_delta": _attribute(entry, peers, points),
                "contamination": _contamination(entry, foreign),
            }
            flag = att["quota"]["contamination"]
            summary[{None: "not_measurable", True: "contaminated", False: "clean"}[
                flag["contaminated"]]] += 1
            for seen in flag["foreign_sessions"]:
                row = summary["foreign_sessions"].setdefault(
                    seen["id"], {"where": seen["where"], "attempts": 0, "activity_count": 0})
                row["attempts"] += 1
                row["activity_count"] += seen["activity_count"]
    accepted = [tid for tid, task in tasks.items() if task.get("status") == "accepted"]
    for entry in entries:
        quota, att = entry["att"]["quota"], entry["att"]
        route = f"{att.get('node_id')}|{entry['backend']}|{att.get('model_id')}|{att.get('effort')}"
        for bucket in (
            report["per_task"].setdefault(entry["task"], {}),
            report["by_role"].setdefault(att.get("node_id") or "?", {}),
        ):
            _add_quota(bucket.setdefault(entry["backend"], _quota_totals()), quota)
        _add_quota(report["by_route"].setdefault(route, _quota_totals()), quota)
        if entry["task"] in accepted:
            _add_quota(report["per_accepted_task"].setdefault(
                entry["backend"], _quota_totals()), quota)
    for backend, totals in report["per_accepted_task"].items():
        n = len(accepted)
        totals["accepted_tasks"] = n
        for name in QUOTA_WINDOWS:
            value = totals[f"attributed_{name}_pct"]
            totals[f"attributed_{name}_pct_per_accepted_task"] = (
                None if value is None or not n else value / n)
    for tid, task in tasks.items():
        task["quota_totals"] = report["per_task"].get(tid, {})
    return report


def export(repo: Path, *, since: str | None) -> dict:
    orch = repo / ".orchestrator"
    conn = sqlite3.connect(f"file:{orch / 'control.sqlite3'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    worktree_root = orch / "worktrees"
    journal = _journal(repo, since)

    tasks: dict[str, dict] = {}
    for task in conn.execute("SELECT * FROM tasks ORDER BY created_at, task_id"):
        tid = task["task_id"]
        worktrees = [worktree_root / tid]
        runs = []
        implementer_attempts = 0
        follow_ups: list[dict] = []
        reviews: list[dict] = []
        for run in conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY started_at, rowid", (tid,)
        ):
            attempts = []
            for att in conn.execute(
                "SELECT * FROM attempts WHERE run_id = ? ORDER BY started_at, rowid",
                (run["run_id"],),
            ):
                keys = att.keys()
                binding = _json(att["binding_json"]) or {}
                binding.pop("rejected_routes", None)
                log = orch / "attempts" / att["attempt_id"] / "impl.log"
                transcript = _find_transcript(att["attempt_id"], worktrees)
                if att["node_id"] == "implementer":
                    implementer_attempts += 1
                review = _review(orch / "attempts" / att["attempt_id"] / "result.json")
                if review:
                    reviews.append(review)
                    follow_ups += [
                        {"attempt_id": att["attempt_id"], "finding": f}
                        for f in review.get("follow_ups") or ()
                    ]
                attempts.append({
                    "attempt_id": att["attempt_id"],
                    "node_id": att["node_id"],
                    "status": att["status"],
                    "started_at": att["started_at"],
                    "backend_id": att["backend_id"],
                    "model_id": att["model_id"],
                    "effort": att["effort"],
                    "billing_category": binding.get("billing_category"),
                    "usage_pool_id": binding.get("usage_pool_id"),
                    "agent_version": att["agent_version"],
                    "session_id": att["session_id"],
                    "session_mode": att["session_mode"],
                    "usage_start": _json(att["usage_start_json"]),
                    "usage_settle": _json(att["usage_settle_json"]),
                    "wall_time_sec": att["wall_time_sec"],
                    "reset_crossing": att["reset_crossing"],
                    "failure_kind": att["failure_kind"],
                    "failure_class": att["failure_class"],
                    "failure": _json(att["failure_json"]),
                    "stepped_down": att["stepped_down"] if "stepped_down" in keys else None,
                    "impl_log": str(log) if log.is_file() else None,
                    "transcript": _transcript_summary(transcript),
                    "review": review,
                })
            runs.append({
                "run_id": run["run_id"],
                "workflow": f"{run['workflow_id']}@{run['workflow_revision']}",
                "status": run["status"],
                "started_at": run["started_at"],
                "finished_at": run["finished_at"],
                "attempts": attempts,
            })
        revisions = conn.execute(
            "SELECT COUNT(*) FROM task_revisions WHERE task_id = ?", (tid,)
        ).fetchone()[0]
        evidence = [
            {k: row[k] for k in ("check_id", "attempt_id", "outcome", "exit_status",
                                 "validity", "recorded_at")}
            for row in conn.execute(
                "SELECT e.* FROM verification_evidence e JOIN attempts a "
                "ON a.attempt_id = e.attempt_id JOIN task_runs r ON r.run_id = a.run_id "
                "WHERE r.task_id = ? ORDER BY e.recorded_at", (tid,)
            )
        ]
        decisions = [
            {k: row[k] for k in ("run_id", "decision", "reason", "integrated_source_snapshot",
                                 "decided_at")}
            for row in conn.execute(
                "SELECT * FROM acceptance_decisions WHERE task_id = ? ORDER BY decided_at",
                (tid,),
            )
        ]
        human = [
            row for row in journal
            if str(row.get("event", "")).startswith(HUMAN_EVENTS)
            and str(row.get("detail", "")).startswith(tid)
        ]
        wall = [
            a["wall_time_sec"] for r in runs for a in r["attempts"]
            if a["wall_time_sec"] is not None
        ]
        tokens = [
            a["transcript"]["token_split"] for r in runs for a in r["attempts"]
            if a["transcript"] and a["transcript"]["token_split"]
        ]
        tasks[tid] = {
            "status": task["status"],
            "runs": runs,
            "rework_attempts": max(implementer_attempts - len(runs), 0) if runs else 0,
            "reopens": max(revisions - 1, 0),
            # P12 pilot finding D16: what reviewers recorded for later, never lost.
            "review_follow_ups": follow_ups,
            # Dan, 2026-09-26: whose miss each later review round was.
            "review_origins": _review_origins(reviews),
            "verification_evidence": evidence,
            "acceptance_decisions": decisions,
            "human_interventions": human,
            "attempt_wall_time_sec_sum": sum(wall) if wall else None,
            "tokens_sum": (
                {k: sum(t[k] for t in tokens) for k in _TOKEN_KEYS} if tokens else None
            ),
        }

    quota = quota_report(tasks, orch)
    global_human = [
        row for row in journal
        if str(row.get("event", "")).startswith(HUMAN_EVENTS)
        and not any(str(row.get("detail", "")).startswith(t) for t in tasks)
    ]
    accepted = [t for t, v in tasks.items() if v["status"] == "accepted"]
    return {
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "project": str(repo),
        "since": since,
        "summary": {
            "tasks_attempted": sum(1 for v in tasks.values() if v["runs"]),
            "tasks_accepted": len(accepted),
            "runs": sum(len(v["runs"]) for v in tasks.values()),
            "attempts": sum(len(r["attempts"]) for v in tasks.values() for r in v["runs"]),
            "rework_attempts": sum(v["rework_attempts"] for v in tasks.values()),
            "human_interventions": sum(len(v["human_interventions"]) for v in tasks.values())
            + len(global_human),
        },
        "tasks": tasks,
        "project_human_interventions": global_human,
        # Per-step subscription usage: per role and per route, per backend account.
        "quota": quota,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--since", help="ignore journal rows before this UTC stamp")
    args = parser.parse_args(argv)
    report = export(args.project.resolve(), since=args.since)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
