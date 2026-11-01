"""W12: improvement proposals from reliable evidence — the analyst and its proposals (GDT-12 S3).

**One run** (`run`, R57, R65): attribute the goal misses (`improvement_evidence.attribute`),
pack the window's evidence, and only when a pattern exists ask the analyst once: one bounded
`agentcall` judgment on the diagnoser route (`analyst`, strong; no new role, no graph node).
The answer is validated against the **full** patterns (`check`): a proposal names a pattern
shown to the analyst, cites only that pattern's evidence, never blames a role the pattern's
fault does not name (an unattributed pattern gets only evidence-gathering levers, R58), stays
within the pattern's spend in its pool, carries risk, cost, measurement and rollback, and a
config lever may only raise `roles.<role>.strength` (B6). An invalid answer is retried twice
with its problems appended; then the run is `failed` with no proposal. There is never a
weaker route or a deterministic stand-in.

**Ranking and decline memory** (R60, R70): candidates are ranked by expected saving (null
last), then by pattern rank; at most `MAX_ASKED` are asked, the rest are `deferred` and
candidates again at the next run. A candidate whose key (lever, target and the config change
or the pattern) matches a proposal under way is suppressed; one matching a declined, noted or
rolled-back proposal is suppressed unless its pattern holds `NEW_EVIDENCE_MIN` evidence rows
recorded after that decision. A revision of a noted proposal is sent once without new
evidence (Dan's approve-with-notes path).

**The question** (`question`, `decide`) holds nothing (`holds: False`): Approve, Decline,
or free text (a note: recorded, never applied, treated as declined for resending).

**Dan's decision applied** (S4, §7): `poll` hands an approval to `apply` / `queue`, which
the orchestrator binds to `apply_config` (a config lever: one leaf of the global config,
backed up, reversible byte for byte) and `queue_intake` (a code lever: an intake report
`I-<project>-IP-n` in Maestro's own checkout, `maestro_repo()`). A queued code lever
`landed` at the first adopted release whose history names its id. Each run measures every
applied proposal's effect on paired windows of accepted tasks (`improvement_metrics`); a
`worse` effect asks Dan to roll back (`rollback_question`, holding nothing) and never acts
by itself: only his tap reaches `rollback` (`poll`'s `undo`). One digest message per run
(`digest`) reports effects, new proposals, suppressions and the analyst call.

The store is `.orchestrator/improvement.json` (`{version, runs, proposals}`), written through
`goal_store.update_file`. Nothing here writes a run status, the completion registry, the
ROADMAP, the goals store or a review artifact.

`python -m maestro.improvement report --repo <path>` prints `report()` (read-only JSON);
`python -m maestro.improvement evidence --repo <path> --database <db>` prints the pack and
makes no call.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from maestro import goal_store, improvement_evidence as ie
from maestro.improvement_metrics import METRICS
from maestro.roles import ROLE_DIAGNOSER

#: Relative to the project root.
STORE = ".orchestrator/improvement.json"
SECTIONS = ("runs", "proposals")
VERSION = 1
#: The analyst's one bounded judgment (seconds per try).
ANALYST_TIMEOUT = 300
#: One call and two retries with the problems ("enhance prompt"); never a weaker route.
MAX_TRIES = 3
#: Questions per run (R70: alert noise).
MAX_ASKED = 3
#: Pattern rows recorded after a decline that count as new evidence.
NEW_EVIDENCE_MIN = 3
DEFAULT_INTERVAL_DAYS = 7
DEFAULT_MIN_TASKS = 5
#: The retry's appended problems, at most.
PROBLEMS_LIMIT = 3000

ROLE_LEVERS = ("role_prompt", "model_effort", "context_skill")
#: What an unattributed (or fault-less) pattern accepts: gather evidence, never blame.
EVIDENCE_LEVERS = ("gate", "human_step", "graph_order", "context_skill")
FAULT_ROLES = ("implementer", "reviewer", "planner")
#: An earlier proposal with the same key in one of these states is already under way.
UNDER_WAY = ("asked", "approved", "applied", "queued", "measured", "kept", "rollback_failed")
#: Dan said no (a note counts as no for resending): only new evidence sends it again.
DECIDED_AGAINST = ("declined", "noted", "rolled_back")
REQUIRED = ("change", "risk", "cost", "rollback", "why")
QUALIFIED_METRICS = ("cost_units_per_task", "extra_steps_per_task")
WHY_UNDEFINED = "Maestro changes its own config or code only with your approval (B14)."
DECLINE_CONSEQUENCE = "Not sent again without new evidence."
_STATE = {"approve": "approved", "decline": "declined", "notes": "noted"}
#: Where an approved code lever's intake report goes (Maestro's checkout root); the test
#: suite points it at a path that does not exist.
INTAKE_ENV = "MAESTRO_IMPROVEMENT_INTAKE"
ROLL_BACK, KEEP = "Roll back", "Keep"
WHY_ROLLBACK = "An effect verdict never acts by itself (R59): only you roll a change back."

SCHEMA = ('{"proposals": [{"pattern": "PAT-…", "lever": "<lever>", "target": "<role or empty>", '
          '"change": "...", "config": {"path": "roles.<role>.strength", "value": "strong"} | '
          'null, "evidence": ["<ledger id of that pattern>"], "expected_saving": '
          '{"cost_units": <number> | null, "pool": "<pool>" | null, "basis": "..."}, '
          '"risk": "...", "cost": "...", "measure": {"metrics": ["<metric>"]}, '
          '"rollback": "...", "why": "...", "alternatives": ["..."], "revises": "IP-…" | null}]}')


def _noop(*_args, **_kwargs) -> None:
    return None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _when(text: Any) -> Optional[datetime]:
    return ie._when(text)


def _number(value: Any) -> str:
    if value is None:
        return "unmeasured"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(int(value)) if isinstance(value, float) else str(value)


# --- the store ------------------------------------------------------------------------------


def path_for(repo: Path) -> Path:
    return Path(repo) / STORE


def read(repo: Path) -> dict:
    """`{runs, proposals}`; empty when there is no file or it does not read."""
    try:
        stored = goal_store.read_file(path_for(repo), SECTIONS)
    except (OSError, ValueError):
        stored = {s: [] for s in SECTIONS}
    return {s: stored[s] for s in SECTIONS}


def _update(repo: Path, section: str, mutate: Callable[[list[dict]], list[dict]]) -> None:
    goal_store.update_file(path_for(repo), SECTIONS, section, mutate, version=VERSION)


def _put(repo: Path, rows: Sequence[Mapping]) -> None:
    """Replace the proposals with these ids and append the new ones."""
    by_id = {str(r["id"]): dict(r) for r in rows}

    def mutate(stored: list[dict]) -> list[dict]:
        out = [by_id.pop(str(r.get("id")), r) for r in stored]
        return out + list(by_id.values())

    _update(repo, "proposals", mutate)


def slug(repo: Path) -> str:
    """The project's name as an intake id part (`I-<slug>-IP-0001`)."""
    name = re.sub(r"[^a-z0-9]+", "-", Path(repo).resolve().name.lower()).strip("-")
    return name or "project"


def project_key(repo: Path) -> str:
    """The project's part of an intake id: its slug plus the first 8 hex digits of the
    sha256 of its resolved path, so two projects of the same directory name never share
    an id (review L3). Stable for one checkout."""
    digest = hashlib.sha256(str(Path(repo).resolve()).encode("utf-8")).hexdigest()[:8]
    return f"{slug(repo)}-{digest}"


def _threshold(repo: Path, name: str, default: int) -> int:
    from maestro import config

    try:
        value = config.threshold(name, default, cast=int,
                                 document=config.effective_config(repo)[0])
    except Exception:  # noqa: BLE001 — an unreadable config keeps the default
        return default
    return value if isinstance(value, int) else default


def _since(runs: Sequence[Mapping]) -> Optional[str]:
    """The previous run's `until`; a failed run does not move the window (the next due run
    reads its tasks again), nor does an empty one (no pattern, no call): on a fresh project
    the first idle tick's weekly run can come after one task, and its rows must still count
    with the later tasks' toward a pattern."""
    for r in reversed(runs):
        if r.get("status") not in ("failed", "empty"):
            return (r.get("window") or {}).get("until")
    return None


# --- when it runs (R57) -------------------------------------------------------------------


def due(repo: Path, kind: str, *, at: str, head: Optional[str] = None) -> Optional[str]:
    """The run key when a `kind` run is due, else `None`. `project_end` is due once per
    HEAD; `weekly` when no run of this project (of any kind) is younger than
    `thresholds.improvement_interval_days` (7; 0 turns weekly off)."""
    runs = read(repo)["runs"]
    if kind == "project_end":
        key = f"project_end:{head}"
        return None if not head or any(r.get("key") == key for r in runs) else key
    if kind != "weekly":
        return None
    days = _threshold(repo, "improvement_interval_days", DEFAULT_INTERVAL_DAYS)
    now = _when(at)
    if days <= 0 or now is None:
        return None
    times = [t for t in (_when(r.get("at")) for r in runs) if t is not None]
    if times and now - max(times) < timedelta(days=days):
        return None
    return f"weekly:{now.date().isoformat()}"


# --- the evidence and the brief -------------------------------------------------------------


def _evidence(repo: Path, db, *, since, until, earlier=()) -> tuple[dict, list[dict], dict]:
    """`(pack, the full ranked patterns, the usage report)`: the pack shows at most five
    evidence ids per pattern, `check` reads the full pattern."""
    from maestro import config, goal_replan
    from maestro.docs import goals as doc_goals
    from maestro.usage_report import build_usage_report

    pack = ie.pack(repo, db, since=since, until=until, earlier=earlier)
    tasks, _ = ie.window_tasks(db, since, until)
    report = build_usage_report(db)
    doc = doc_goals.load(repo)
    section = ie.goal_section(repo, doc, since, until, report=report) if doc else {}
    replans = [r for r in goal_store.read(repo)["replans"]
               if ie._in(r.get("settled_at"), since, until)] if doc else []
    try:
        misses = goal_replan.miss_threshold(config.effective_config(repo)[0])
    except Exception:  # noqa: BLE001
        misses = goal_replan.DEFAULT_MISSES
    return pack, ie.patterns(report, section, replans, tasks, misses=misses), report


def evidence(repo: Path, db, *, since: Optional[str], until: Optional[str],
             earlier: Iterable[Mapping] = ()) -> tuple[dict, list[dict]]:
    """`(pack, full patterns)` for the window; read-only, no call."""
    pack, found, _ = _evidence(repo, db, since=since, until=until, earlier=list(earlier))
    return pack, found


def brief(pack: Mapping[str, Any]) -> tuple[str, str]:
    """`(system, user)`: the schema, the levers and the rules; the pack's compact JSON."""
    metrics = ", ".join(METRICS)
    system = (
        "You are Maestro's improvement analyst. You read one project's evidence pack (the "
        "JSON below) and propose changes to how Maestro works. Answer with JSON only, in "
        f"exactly this schema: {SCHEMA}. Levers: {', '.join(ie.LEVERS)}; role_prompt, "
        "model_effort and context_skill target a role, planner_rule targets planner. "
        "Rules: propose only for a listed pattern; cite only its evidence ids; never blame "
        "a role the pattern's fault does not name; for an unattributed pattern (fault "
        "unattributed, unclassified, none, not a role, or no fault) propose only how to get "
        "evidence (gate, human_step, graph_order or context_skill); a model lever only "
        "raises a role's strength (config path roles.<role>.strength from the config rows, "
        "never one project.yaml masks) and needs lever model_effort; expected_saving."
        "cost_units is at most the pattern's spend in that pool, or null with basis "
        "\"unmeasured\" when the pattern has no measured cost; measure.metrics are from: "
        f"{metrics} (cost_units_per_task:<pool>, extra_steps_per_task[:<fault>[/<cause>]] "
        "may be qualified); risk, cost, rollback and why are required; say why this beats "
        "the alternatives. revises names a noted earlier proposal you revise with Dan's "
        "note. An empty list is a valid answer.")
    user = json.dumps(pack, separators=(",", ":"), sort_keys=True)
    return system, user


def _with_problems(user: str, problems: Sequence[str]) -> str:
    text = "\n".join(f"- {p}" for p in problems)[:PROBLEMS_LIMIT]
    return f"{user}\n\nYour previous answer was refused; fix every problem:\n{text}"


def _parse(raw: str) -> tuple[Optional[dict], Optional[str]]:
    text = (raw or "").strip()
    if not text:
        return None, "the analyst gave no answer"
    try:
        found = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except ValueError:
        return None, "the answer is not a JSON object"
    return (found, None) if isinstance(found, dict) else (None, "the answer is not an object")


def analyst(system: str, user: str) -> str:
    """The analyst call: one bounded judgment on the diagnoser route (strong), the seam
    the orchestrator wires (`_improvement_analyst`)."""
    from maestro.selfheal import diagnose

    return diagnose._judge_complete(system, user, timeout=ANALYST_TIMEOUT)


# --- validation (R60, R58) ------------------------------------------------------------------


def _fault(pattern: Mapping[str, Any]) -> Optional[str]:
    key = list(pattern.get("key") or ())
    if pattern.get("kind") == "extra_step" and key:
        return key[0]
    if pattern.get("kind") == "goal_miss" and len(key) > 1:
        return key[1]
    return None


def key_of(proposal: Mapping[str, Any], pattern: Mapping[str, Any]) -> str:
    """Decline memory's key: lever, target and the config change, or the pattern."""
    config = proposal.get("config")
    lever, target = proposal.get("lever"), _text(proposal.get("target"))
    if isinstance(config, Mapping):
        parts = [lever, target, config.get("path"), config.get("value")]
    else:
        parts = [lever, target, pattern.get("kind"), list(pattern.get("key") or ())]
    return hashlib.sha1(json.dumps(parts).encode()).hexdigest()[:12]


def _metric_ok(metric: Any) -> bool:
    if not isinstance(metric, str):
        return False
    base, _, qualifier = metric.partition(":")
    return base in METRICS and (not qualifier or base in QUALIFIED_METRICS)


def _roles(view: Sequence[Mapping]) -> set[str]:
    return {str(r.get("path", "")).split(".")[1] for r in view
            if str(r.get("path", "")).count(".") == 2}


def _fault_side(p: Mapping, pattern: Mapping, lever: str, view) -> list[str]:
    fault, target, pid = _fault(pattern), _text(p.get("target")), pattern["id"]
    if fault not in FAULT_ROLES:
        if lever not in EVIDENCE_LEVERS:
            return [f"{pid} is {fault or 'without a fault'} ({pattern.get('kind')}), "
                    f"unattributed to any role: {lever} would blame one; it takes only "
                    f"{', '.join(EVIDENCE_LEVERS)}"]
        if lever == "context_skill" and target not in _roles(view):
            return [f"context_skill must target a shipped role, not {target!r}"]
        return []
    if lever == "planner_rule":
        if fault != "planner":
            return [f"planner_rule needs a planner pattern; {pid}'s fault is {fault}"]
        return [] if target in ("", "planner") else [f"planner_rule targets planner, "
                                                     f"not {target!r}"]
    if lever in ROLE_LEVERS and target != fault:
        return [f"{lever} must target {fault}, the role {pid}'s fault names, not {target!r}"]
    return []


def _saving(p: Mapping, pattern: Mapping) -> list[str]:
    pid, es = pattern["id"], p.get("expected_saving")
    if not isinstance(es, Mapping):
        return ["expected_saving is missing"]
    measured = {pool: v.get("cost_units") for pool, v in (pattern.get("spend") or {}).items()
                if isinstance(v, Mapping) and v.get("cost_units") is not None}
    cu = es.get("cost_units")
    if cu is None:
        if _text(es.get("basis")) != "unmeasured" or measured:
            return [f"a null saving needs the basis \"unmeasured\" and a pattern without "
                    f"measured cost; {pid} spent {measured}"]
        return []
    if isinstance(cu, bool) or not isinstance(cu, (int, float)) or cu < 0:
        return [f"expected saving {cu!r} must be a number of cost units >= 0"]
    pool = es.get("pool")
    if pool not in measured:
        return [f"pool {pool!r} has no measured spend in {pid} (pools: "
                f"{', '.join(sorted(measured)) or 'none'})"]
    if cu > measured[pool]:
        return [f"expected saving {cu} exceeds {pid}'s spend of {measured[pool]} in {pool}"]
    if not _text(es.get("basis")):
        return ["expected_saving.basis is empty"]
    return []


def _config(p: Mapping, config: Any, lever: str, view: Sequence[Mapping]) -> list[str]:
    from maestro.backends.catalog import MODEL_STRENGTHS

    if lever != "model_effort":
        return [f"only a model_effort lever may carry config, not {lever}"]
    if not isinstance(config, Mapping):
        return ["config must be {path, value} or null"]
    rows = {str(r.get("path")): r for r in view}
    path, target = str(config.get("path")), _text(p.get("target"))
    found = re.fullmatch(r"roles\.([a-z_]+)\.strength", path)
    if not found or path not in rows:
        return [f"config path {path!r} is not roles.<role>.strength of a shipped role"]
    if found.group(1) != target:
        return [f"config path {path} must be the target's own, roles.{target}.strength"]
    value, current = config.get("value"), rows[path].get("value")
    if value not in MODEL_STRENGTHS:
        return [f"{value!r} is not a strength ({', '.join(MODEL_STRENGTHS)})"]
    if current in MODEL_STRENGTHS and \
            MODEL_STRENGTHS.index(value) <= MODEL_STRENGTHS.index(current):
        return [f"{path} {value} is not stronger than the effective {current}: a lever never "
                "lowers quality"]
    if rows[path].get("masked"):
        return [f"project.yaml sets {path}, so a global change would be masked"]
    return []


def _problems(p: Any, shown: set, full: Mapping[str, Mapping], rows: Sequence[Mapping],
              view: Sequence[Mapping]) -> list[str]:
    if not isinstance(p, Mapping):
        return ["is not an object"]
    pid = p.get("pattern")
    if pid not in shown or pid not in full:
        return [f"pattern {pid!r} is not a listed pattern"]
    pattern, lever = full[pid], p.get("lever")
    if lever not in ie.LEVERS:
        return [f"lever {lever!r} is not one of {', '.join(ie.LEVERS)}"]
    out = []
    cited = p.get("evidence")
    if not isinstance(cited, list) or not cited or not all(isinstance(e, str) for e in cited):
        out.append(f"evidence must list ids of {pid}'s evidence")
    else:
        invented = sorted(set(cited) - set(pattern.get("evidence") or ()))
        if invented:
            out.append(f"evidence {', '.join(invented[:3])} is not {pid}'s evidence")
    out += _fault_side(p, pattern, lever, view)
    out += _saving(p, pattern)
    out += [f"{field} is empty" for field in REQUIRED if not _text(p.get(field))]
    measure = p.get("measure")
    metrics = measure.get("metrics") if isinstance(measure, Mapping) else None
    if not isinstance(metrics, list) or not metrics:
        out.append("measure.metrics is empty")
    else:
        bad = [m for m in metrics if not _metric_ok(m)]
        if bad:
            out.append(f"metric {bad[0]!r} is not in the vocabulary ({', '.join(METRICS)})")
    if p.get("config") is not None:
        out += _config(p, p.get("config"), lever, view)
    revises = p.get("revises")
    if revises is not None:
        earlier = next((r for r in rows if r.get("id") == revises), None)
        if earlier is None or earlier.get("state") != "noted" \
                or earlier.get("key") != key_of(p, pattern):
            out.append(f"revises {revises!r} must name a noted proposal with the same lever, "
                       "target and change")
    return out


def check(document: Any, pack: Mapping[str, Any], rows: Sequence[Mapping],
          view: Sequence[Mapping], *, patterns: Optional[Sequence[Mapping]] = None) -> list[str]:
    """The problems with the analyst's answer (`[]` when it is valid). A proposal's
    pattern must be one the pack showed; its evidence and spend are checked against the
    full pattern in `patterns` (the pack shows at most five evidence ids)."""
    if not isinstance(document, Mapping) or not isinstance(document.get("proposals"), list):
        return ['the answer must be {"proposals": [...]}']
    shown = {p.get("id") for p in pack.get("patterns") or ()}
    full = {p["id"]: p for p in (patterns if patterns is not None
                                 else pack.get("patterns") or ())}
    out = []
    for n, p in enumerate(document["proposals"], 1):
        out += [f"proposal {n}: {problem}" for problem in _problems(p, shown, full, rows, view)]
    return out


# --- decline memory (R60, C32) --------------------------------------------------------------


def _times(store, report: Mapping[str, Any], repo: Path) -> dict[str, str]:
    """When each evidence id was recorded: W2 rows, goal-impact observations (their first
    row), episodes and replans."""
    from maestro.workflows import telemetry_capture

    times: dict[str, str] = {}
    for task in report.get("tasks") or ():
        for row in task.get("blame_ledger") or ():
            if row.get("recorded_at"):
                times[str(row["ledger_id"])] = row["recorded_at"]
    if store is not None:
        for row in store.conn.execute(
                "SELECT payload, recorded_at FROM events WHERE type = ? ORDER BY sequence",
                (telemetry_capture.GOAL_IMPACT_EVENT,)):
            try:
                lid = json.loads(row["payload"]).get("ledger_id")
            except (TypeError, ValueError, AttributeError):
                continue
            if lid:
                times.setdefault(str(lid), row["recorded_at"])
    stored = goal_store.read(repo)
    for ep in stored["episodes"]:
        times.setdefault(f"episode:{ep.get('id')}", ep.get("at"))
    for r in stored["replans"]:
        times.setdefault(f"replan:{r.get('id')}", r.get("settled_at"))
    return times


def _memory(cand: Mapping, rows: Sequence[Mapping], times: Mapping[str, str]):
    """`(reason, of, new_evidence)`: why the candidate is suppressed (`None` when it is
    sent), and the new evidence it is sent on."""
    same = sorted((r for r in rows if r.get("key") == cand["key"]), key=lambda r: r["id"])
    for r in same:
        if r.get("state") in UNDER_WAY:
            return f"{r['id']} is already {r['state']}", r["id"], None
    newest = None
    for r in same:
        state = r.get("state")
        if state not in DECIDED_AGAINST:
            continue
        if state == "noted" and cand.get("revises") == r["id"] and not any(
                o.get("revises") == r["id"] for o in rows):
            continue                                   # the one revision with his note
        decided = (r.get("decision") or {}).get("at") or r.get("at")
        after = _when(decided)
        n = sum(1 for e in cand["pattern"].get("evidence") or ()
                if after is not None and (_when(times.get(e)) or after) > after)
        if n < NEW_EVIDENCE_MIN:
            return (f"{state} {r['id']} on {str(decided)[:10]}: {n} new row(s) since, "
                    f"{NEW_EVIDENCE_MIN} needed"), r["id"], None
        newest = {"since": r["id"], "at": decided, "rows": n}
    return None, None, newest


# --- the question (R60) ---------------------------------------------------------------------


def _pattern_text(pattern: Mapping[str, Any]) -> str:
    kind, key = pattern.get("kind"), list(pattern.get("key") or ())
    if kind == "extra_step" and len(key) == 3:
        return f"extra_step pattern (fault {key[0]}, cause {key[1]}, role {key[2]})"
    if kind == "goal_miss" and len(key) == 3:
        return f"goal_miss pattern ({key[0]}, attribution {key[1]}, cause {key[2]})"
    if kind == "evidence_gap" and key:
        return f"evidence_gap pattern ({key[0]}: unknown impacts)"
    if kind == "false_alarm":
        return "false_alarm pattern (replans Dan settled keep_going)"
    return f"{kind} pattern {key}"


def _home(path: Path) -> str:
    try:
        return "~/" + str(Path(path).resolve().relative_to(Path.home().resolve()))
    except ValueError:
        return str(path)


def question(proposal: Mapping[str, Any]) -> dict:
    """The `poll_question` record (the `dan_questions` shape) for one proposal; it holds
    nothing."""
    from maestro import config as config_mod

    ip, pattern = proposal["id"], proposal["pattern"]
    spend = "; ".join(f"{pool}: {_number(v.get('cost_units'))} cost units, "
                      f"{_number(v.get('tokens'))} tokens"
                      for pool, v in sorted((pattern.get("spend") or {}).items())) or "none"
    es = proposal.get("expected_saving") or {}
    saving = (f"{_number(es.get('cost_units'))} cost units ({es.get('pool')})"
              if es.get("cost_units") is not None else "unmeasured")
    measure = proposal.get("measure") or {}
    n = measure.get("min_tasks") or DEFAULT_MIN_TASKS
    lever, target = proposal.get("lever"), proposal.get("target")
    cfg = proposal.get("config")
    parts = [
        f"{ip}: {_pattern_text(pattern)} {pattern.get('id')}: {pattern.get('rows')} rows in "
        f"{pattern.get('tasks')} tasks, e.g. {', '.join(proposal.get('evidence', [])[:3])}.",
        f"Spend: {spend}.",
        f"Change ({lever}{' for ' + target if target else ''}): {proposal.get('change')}.",
    ]
    if isinstance(cfg, Mapping):
        parts.append(f"Config: {cfg.get('path')} {cfg.get('previous')} → {cfg.get('value')}.")
    parts += [
        f"Expected saving: {saving}; basis: {es.get('basis')}.",
        f"Risk: {proposal.get('risk')}. Cost: {proposal.get('cost')}.",
        f"Measure: {', '.join(measure.get('metrics') or ())}; the next {n} accepted tasks vs "
        f"the {n} before.",
        f"Rollback: {proposal.get('rollback')}.",
        f"Why: {proposal.get('why')}. Alternatives: "
        f"{'; '.join(proposal.get('alternatives') or ()) or 'none given'}.",
    ]
    new = proposal.get("new_evidence")
    if new:
        parts.append(f"This is new since you declined {new['since']} on "
                     f"{str(new['at'])[:10]}: {new['rows']} rows.")
    if proposal.get("revises"):
        parts.append(f"Revises {proposal['revises']} with your note: "
                     f"{proposal.get('revised_note') or ''}".rstrip(": ") + ".")
    if isinstance(cfg, Mapping):
        approve = (f"Applies `{cfg.get('path')}: {cfg.get('value')}` to "
                   f"{_home(config_mod.global_config_path())} now; one tap rolls it back.")
    else:
        approve = f"Queues it in Maestro's intake as {intake_id(proposal)}."
    qid = proposal.get("question_id") or f"dq-improve-{ip}"
    return {"id": qid, "task_id": "maestro", "run_id": f"improve-{ip}",
            "title": f"Improvement {ip}", "holds": False,
            "spec": {"question": " ".join(parts), "why_undefined": WHY_UNDEFINED,
                     "options": [{"label": "Approve", "consequence": approve,
                                  "resolution": "approve"},
                                 {"label": "Decline", "consequence": DECLINE_CONSEQUENCE,
                                  "resolution": "decline"}],
                     "recommended": 0,
                     "improvement": {"id": ip, "key": proposal.get("key")}}}


def decide(proposal: Mapping[str, Any], answer: str, at: str) -> dict:
    """A tap on Approve approves, on Decline declines; any other text is Dan's note:
    recorded, never applied, treated as declined for resending."""
    text = str(answer or "").strip()
    resolution = {"approve": "approve", "decline": "decline"}.get(text.lower(), "notes")
    return {"resolution": resolution, "answer": text, "at": at}


# --- Dan's decision applied (S4, §7) ----------------------------------------------------------


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _sha(data: Any) -> str:
    return hashlib.sha256(data if isinstance(data, bytes) else str(data).encode()).hexdigest()


def _stamp(at: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", str(at)) or "now"


def maestro_repo() -> Path:
    """Maestro's own checkout, whose `.orchestrator/intake/` is Maestro's queue: the root
    `$MAESTRO_IMPROVEMENT_INTAKE` names, else the main repository of the running checkout
    (the parent of its git common dir, so a `~/.maestro/versions/<sha>` worktree resolves to
    the checkout it was made from)."""
    raw = os.environ.get(INTAKE_ENV)
    if raw:
        return Path(raw).expanduser()
    here = Path(__file__).resolve().parent.parent
    proc = subprocess.run(["git", "-C", str(here), "rev-parse", "--path-format=absolute",
                           "--git-common-dir"], capture_output=True, text=True)
    common = proc.stdout.strip()
    return Path(common).parent if proc.returncode == 0 and common else here


def head(repo: Path) -> Optional[str]:
    """`repo`'s HEAD sha (the project-end run's key), `None` when it cannot be read."""
    proc = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                          text=True)
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and sha else None


def _intake_part(row: Mapping[str, Any]) -> str:
    return str(row.get("project_key") or row.get("project") or "project")


def intake_id(row: Mapping[str, Any]) -> str:
    """`I-<project_key>-<IP>`: the intake report an approved code lever is queued as."""
    return f"I-{_intake_part(row)}-{row['id']}"


def revert_id(row: Mapping[str, Any]) -> str:
    """`I-<project_key>-revert-<IP>`: the revert report a rolled-back code lever queues."""
    return f"I-{_intake_part(row)}-revert-{row['id']}"


def _names(rid: str) -> "re.Pattern[str]":
    """A commit message names `rid` only as a whole id (not inside a longer one)."""
    return re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(rid) + r"(?![A-Za-z0-9_-])")


class _Refused(Exception):
    """A config edit refused under the lock: `now` is the value found there."""

    def __init__(self, now: Any):
        super().__init__(f"now {now}")
        self.now = now


def _write_atomic(path: Path, data: bytes) -> None:
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw)
    try:
        if path.exists():
            os.fchmod(fd, path.stat().st_mode & 0o777)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _failed_apply(repo: Path, row: Mapping, exc: Exception, journal, notify) -> dict:
    problem = f"{type(exc).__name__}: {exc}"[:300]
    fields = {"state": "apply_failed", "apply_error": problem}
    _put(repo, [{**row, **fields}])
    journal("improvement_apply_failed", f"{row['id']}: {problem}")
    notify(f"⚠️ {row['id']} was approved but could not be applied: {problem}")
    return fields


def apply_config(repo: Path, row: Mapping[str, Any], *, at: Optional[str] = None,
                 journal: Optional[Callable] = None,
                 notify: Optional[Callable] = None) -> dict:
    """An approved config lever: its one leaf of the global config is set
    (`config.set_global_leaf`), after the file's bytes are backed up beside it
    (`backups/config.yaml.<IP>.<at>`). Returns (and stores) the fields: `state: applied`,
    `applied = {at, kind: config, path, leaf, value, previous, existed, before_text_sha,
    after_text_sha, backup}`. Only an `approved` row is applied; a failure is
    `apply_failed`, journaled and told. Never lowers quality (review M3): under the config
    lock the leaf's effective value is read again, and unless it is still strictly weaker
    than the approved value (a stale approval: the leaf was raised since it was asked) the
    approval is refused, `apply_refused = {at, leaf, value, now}`, journaled and told, and
    nothing is written."""
    from maestro import config as config_mod
    from maestro.backends.catalog import MODEL_STRENGTHS

    journal, notify = journal or _noop, notify or _noop
    if row.get("state") != "approved":
        return {}
    at = at or (row.get("decision") or {}).get("at") or _now()
    cfg = row.get("config") or {}
    leaf, value, previous = str(cfg.get("path")), cfg.get("value"), cfg.get("previous")

    def weaker_still(_original: str) -> None:
        now = next((r.get("value") for r in ie.config_view(repo)
                    if r.get("path") == leaf), None)
        if value not in MODEL_STRENGTHS or now not in MODEL_STRENGTHS \
                or MODEL_STRENGTHS.index(now) >= MODEL_STRENGTHS.index(value):
            raise _Refused(now)

    backup = None
    try:
        path = config_mod.global_config_path()
        existed = path.exists()
        before_bytes = path.read_bytes() if existed else b""
        backup = path.parent / "backups" / f"{path.name}.{row['id']}.{_stamp(at)}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(before_bytes)
        before, after = config_mod.set_global_leaf(tuple(leaf.split(".")), value,
                                                   check=weaker_still)
        if before.encode() != before_bytes:            # changed between the read and the lock
            backup.write_bytes(before.encode())
    except _Refused as refused:
        if backup is not None:
            backup.unlink(missing_ok=True)
        fields = {"state": "apply_refused", "apply_refused": {
            "at": at, "leaf": leaf, "value": value, "now": refused.now}}
        _put(repo, [{**row, **fields}])
        journal("improvement_apply_refused",
                f"{row['id']}: {leaf} is {refused.now}, not weaker than {value}")
        notify(f"⚠️ {row['id']} not applied: {leaf} is now {refused.now}, not weaker than "
               f"the approved {value}, and a lever never lowers quality. Nothing changed.")
        return fields
    except Exception as exc:  # noqa: BLE001 — told, never raised into the loop
        return _failed_apply(repo, row, exc, journal, notify)
    fields = {"state": "applied", "applied": {
        "at": at, "kind": "config", "path": str(path), "leaf": leaf, "value": value,
        "previous": previous, "existed": existed, "before_text_sha": _sha(before),
        "after_text_sha": _sha(after), "backup": str(backup)}}
    _put(repo, [{**row, **fields}])
    journal("improvement_applied", f"{row['id']}: {leaf} {previous} → {value}")
    notify(f"✅ {row['id']} applied: {leaf} {previous} → {value}")
    return fields


def _intake_text(row: Mapping[str, Any], rid: str) -> str:
    pattern = row.get("pattern") or {}
    es = row.get("expected_saving") or {}
    measure = row.get("measure") or {}
    n = measure.get("min_tasks") or DEFAULT_MIN_TASKS
    target = row.get("target")
    saving = (f"{_number(es.get('cost_units'))} cost units ({es.get('pool')})"
              if es.get("cost_units") is not None else "unmeasured")
    evidence = list(row.get("evidence") or ())
    return "\n".join([
        f"Improvement {row['id']} for {row.get('project')}, approved by Dan: {row.get('change')}",
        f"Lever: {row.get('lever')}{' for ' + target if target else ''}.",
        f"Pattern {pattern.get('id')}: {_pattern_text(pattern)}, {pattern.get('rows')} rows in "
        f"{pattern.get('tasks')} tasks; evidence: {', '.join(evidence[:20])}"
        f"{' …' if len(evidence) > 20 else ''}.",
        f"Expected saving: {saving}; basis: {es.get('basis')}.",
        f"Risk: {row.get('risk')}. Cost: {row.get('cost')}.",
        f"Why: {row.get('why')}. Alternatives: "
        f"{'; '.join(row.get('alternatives') or ()) or 'none given'}.",
        f"Measure: {', '.join(measure.get('metrics') or ())}; the next {n} accepted tasks of "
        f"{row.get('project')} vs the {n} before, from the first adopted release naming {rid}.",
        f"Rollback: {row.get('rollback')}.",
        f"Name {rid} in the commit message that ships it.",
    ])


def queue_intake(repo: Path, row: Mapping[str, Any], *, at: Optional[str] = None,
                 journal: Optional[Callable] = None, notify: Optional[Callable] = None,
                 maestro: Optional[Path] = None) -> dict:
    """An approved code lever enters Maestro's own queue: intake report
    `I-<project>-<IP>` (kind change) in `maestro_repo()` (`intake.capture`; a mailbox file,
    not a commit). Returns (and stores) `state: queued`, `intake = {id, at, repo}`; it is
    applied when it `landed`."""
    from maestro import intake

    journal, notify = journal or _noop, notify or _noop
    if row.get("state") != "approved":
        return {}
    at = at or (row.get("decision") or {}).get("at") or _now()
    root = Path(maestro) if maestro is not None else maestro_repo()
    rid = intake_id(row)
    try:
        intake.capture(root, "change", _intake_text(row, rid), report_id=rid)
    except Exception as exc:  # noqa: BLE001
        return _failed_apply(repo, row, exc, journal, notify)
    fields = {"state": "queued", "intake": {"id": rid, "at": at, "repo": str(root)}}
    _put(repo, [{**row, **fields}])
    journal("improvement_queued", f"{row['id']} → {rid} in {root}")
    notify(f"📮 {row['id']} queued in Maestro's intake ({rid})")
    return fields


def _leaf_value(data: bytes, leaf: str) -> Any:
    import yaml

    try:
        node = yaml.safe_load(data.decode("utf-8")) or {}
    except (yaml.YAMLError, UnicodeDecodeError):
        return None
    for part in leaf.split("."):
        node = node.get(part) if isinstance(node, Mapping) else None
    return node


def rollback(repo: Path, row: Mapping[str, Any], *, at: Optional[str] = None,
             journal: Optional[Callable] = None, notify: Optional[Callable] = None,
             maestro: Optional[Path] = None) -> dict:
    """Undo an applied proposal, only ever on Dan's tap. A config lever: the backup is
    restored byte for byte when the file is still exactly as applied; else, when its leaf
    still holds the applied value, only the leaf is put back; else it is refused ("changed
    since applied") and Dan is told the leaf and both values. A code lever: a revert report
    `I-<project_key>-revert-<IP>` is queued in Maestro's intake. Returns (and stores) the
    fields: `state: rolled_back`, `rolled_back = {at, how}`, or `rollback_refused`. The sha
    check and the byte restore hold `config.global_config_lock` (review L2); the leaf path
    re-checks the leaf under the same lock. Never raises (review M2): a failure is
    `state: rollback_failed`, `rollback_failed = {at, error}`, journaled and told."""
    journal, notify = journal or _noop, notify or _noop
    at = at or (row.get("rollback_decision") or {}).get("at") or _now()
    try:
        return _rollback(repo, row, at=at, journal=journal, notify=notify, maestro=maestro)
    except Exception as exc:  # noqa: BLE001 — told, never raised into the loop
        return _failed_rollback(repo, row, exc, at, journal, notify)


def _failed_rollback(repo: Path, row: Mapping, exc: Exception, at: str, journal,
                     notify) -> dict:
    problem = f"{type(exc).__name__}: {exc}"[:300]
    applied = row.get("applied") or {}
    fields = {"state": "rollback_failed", "rollback_failed": {"at": at, "error": problem}}
    _put(repo, [{**row, **fields}])
    journal("improvement_rollback_failed", f"{row['id']}: {problem}")
    if applied.get("kind") == "code":
        still = (f"{str(applied.get('landed'))[:12]} is still in; queue its revert as "
                 f"{revert_id(row)} by hand")
    else:
        still = (f"{applied.get('leaf')} is still {applied.get('value')} in "
                 f"{_home(Path(applied.get('path') or '.'))}; set it back to "
                 f"{applied.get('previous')} by hand (backup: {applied.get('backup')})")
    notify(f"⚠️ {row['id']} could not be rolled back: {problem}. {still}.")
    return fields


def _rollback(repo: Path, row: Mapping[str, Any], *, at: str, journal, notify,
              maestro: Optional[Path]) -> dict:
    from maestro import config as config_mod
    from maestro import intake

    ip, applied = row["id"], row.get("applied") or {}
    if applied.get("kind") == "code":
        root = Path(maestro) if maestro is not None else Path(
            (row.get("intake") or {}).get("repo") or maestro_repo())
        rid = revert_id(row)
        text = (f"Revert {ip} ({applied.get('landed')}), which {row.get('project')}'s "
                f"measurement found worse: {row.get('change')}.\nRollback: "
                f"{row.get('rollback')}.\nName {rid} in the commit message that ships it.")
        intake.capture(root, "change", text, report_id=rid)
        fields = {"state": "rolled_back", "rolled_back": {"at": at, "how": "revert_queued",
                                                          "intake": rid}}
        _put(repo, [{**row, **fields}])
        journal("improvement_rolled_back", f"{ip}: revert queued as {rid}")
        notify(f"↩️ {ip}: a revert of {str(applied.get('landed'))[:12]} is queued in "
               f"Maestro's intake ({rid})")
        return fields
    path = Path(applied["path"])
    leaf, value, previous = applied.get("leaf"), applied.get("value"), applied.get("previous")
    with config_mod.global_config_lock(path):
        current = path.read_bytes() if path.exists() else None
        restore = current is not None and _sha(current) == applied.get("after_text_sha")
        if restore:
            if applied.get("existed", True):
                _write_atomic(path, Path(applied["backup"]).read_bytes())
            else:
                path.unlink()
    how = "restored" if restore else None
    if how is None:
        now = _leaf_value(current, leaf) if current is not None else None
        if now == value and previous is not None \
                and config_mod.global_config_path() == path:

            def holds_applied(original: str) -> None:
                found = _leaf_value(original.encode("utf-8"), str(leaf))
                if found != value:
                    raise _Refused(found)

            try:
                config_mod.set_global_leaf(tuple(str(leaf).split(".")), previous,
                                           check=holds_applied)
                how = "leaf"
            except _Refused as refused:       # changed between the read and the lock
                now = refused.now
        if how is None:
            fields = {"rollback_refused": {"at": at, "leaf": leaf, "applied": value,
                                           "now": now}}
            _put(repo, [{**row, **fields}])
            journal("improvement_rollback_refused",
                    f"{ip}: {leaf} changed since applied ({value} → {now})")
            notify(f"⚠️ {ip} not rolled back: {leaf} changed since applied (applied {value}, "
                   f"now {now}). Set it to {previous} by hand if you still want that.")
            return fields
    fields = {"state": "rolled_back", "rolled_back": {"at": at, "how": how}}
    _put(repo, [{**row, **fields}])
    journal("improvement_rolled_back", f"{ip}: {leaf} {value} → {previous} ({how})")
    notify(f"↩️ {ip} rolled back: {leaf} {value} → {previous}")
    return fields


def landed(repo: Path, *, at: Optional[str] = None, journal: Optional[Callable] = None,
           maestro: Optional[Path] = None,
           latest: Optional[Callable[[], Optional[str]]] = None) -> list[dict]:
    """Each queued code lever whose intake id a commit in the adopted release's history
    names (`git log --grep`, the release `selfupdate.read_latest()` announced) is applied
    from now: `applied = {kind: code, at, landed: the first such commit, release}`. Returns
    the rows that landed; measurement starts at `at`."""
    journal = journal or _noop
    queued = [r for r in read(repo)["proposals"] if r.get("state") == "queued"]
    if not queued:
        return []
    if latest is None:
        from maestro.selfupdate import read_latest as latest
    release = latest()
    if not release:
        return []
    at = at or _now()
    out = []
    for row in queued:
        rid = (row.get("intake") or {}).get("id") or intake_id(row)
        root = Path(maestro) if maestro is not None else Path(
            (row.get("intake") or {}).get("repo") or maestro_repo())
        proc = subprocess.run(["git", "-C", str(root), "log", "--fixed-strings",
                               f"--grep={rid}", "--format=%H%x1f%B%x1e", release],
                              capture_output=True, text=True)
        whole = _names(rid)                  # the full id, not one that only starts so (L3)
        shas = []
        for entry in (proc.stdout.split("\x1e") if proc.returncode == 0 else []):
            sha, _, body = entry.strip().partition("\x1f")
            if sha and whole.search(body):
                shas.append(sha)
        if not shas:
            continue
        updated = {**row, "state": "applied",
                   "applied": {"kind": "code", "at": at, "landed": shas[-1], "release": release}}
        _put(repo, [updated])
        journal("improvement_landed", f"{row['id']}: {shas[-1][:12]} in release {release[:12]}")
        out.append(updated)
    return out


def rollback_question(row: Mapping[str, Any]) -> dict:
    """The rollback question a `worse` effect asks (it holds nothing): Roll back
    (recommended) or Keep. Only Dan's tap rolls back."""
    from maestro.improvement_metrics import line

    ip, effect = row["id"], row.get("effect") or {}
    applied = row.get("applied") or {}
    notes = "; ".join(effect.get("notes") or ())
    if applied.get("kind") == "code":
        undo = (f"Queues a revert of {str(applied.get('landed'))[:12]} in Maestro's intake "
                f"({revert_id(row)}).")
    else:
        undo = (f"Restores {_home(Path(applied.get('path') or '.'))} as it was before {ip} "
                f"({applied.get('leaf')} back to {applied.get('previous')}).")
    text = (f"{ip} was measured worse: {line(row, effect)}"
            + (f" Notes: {notes}." if notes else "")
            + " Roll it back? Maestro never rolls back by itself.")
    return {"id": row.get("rollback_question_id") or f"dq-improve-rollback-{ip}",
            "task_id": "maestro", "run_id": f"improve-{ip}", "title": f"Roll back {ip}?",
            "holds": False,
            "spec": {"question": text, "why_undefined": WHY_ROLLBACK,
                     "options": [{"label": ROLL_BACK, "consequence": undo,
                                  "resolution": "roll_back"},
                                 {"label": KEEP, "consequence": f"Keeps {ip}; not asked again.",
                                  "resolution": "keep"}],
                     "recommended": 0, "improvement": {"id": ip, "rollback": True}}}


# --- effects and the digest (S4, §7.3, §7.4) ---------------------------------------------------


def _effects(repo: Path, db, record: dict, rows: Sequence[Mapping], *, at: str,
             journal) -> list[dict]:
    """Measure each applied proposal without an effect: paired windows of accepted tasks
    from its `applied.at`. Too few tasks after is a line, no state change; otherwise the
    effect is stored and the state is `measured`. Returns the `worse` rows, whose rollback
    question the run asks."""
    from maestro import improvement_metrics as im

    min_tasks = max(1, _threshold(repo, "improvement_min_tasks", DEFAULT_MIN_TASKS))
    worse, changed = [], []
    for row in rows:
        start = (row.get("applied") or {}).get("at")
        if row.get("state") != "applied" or row.get("effect") or not start:
            continue
        w = im.windows(db, start, min_tasks=min_tasks)
        if w is None:
            effect = {"verdict": "insufficient_data",
                      "tasks": {"after": len(im.accepted_after(db, start)),
                                "needed": min_tasks}}
            record["measured"].append({"id": row["id"], "verdict": effect["verdict"],
                                       "tasks": effect["tasks"], "line": im.line(row, effect)})
            continue
        before, after = (im.measure(db, repo, w[k]["tasks"], since=w[k]["since"],
                                    until=w[k]["until"]) for k in ("before", "after"))
        effect = im.effect(before, after, list((row.get("measure") or {}).get("metrics") or ()))
        record["measured"].append({"id": row["id"], "verdict": effect["verdict"],
                                   "line": im.line(row, effect)})
        if effect["verdict"] == "insufficient_data":
            continue
        updated = {**row, "state": "measured", "measured_at": at,
                   "effect": {**effect, "windows": w}}
        if effect["verdict"] == "worse":
            updated["rollback_question_id"] = f"dq-improve-rollback-{row['id']}"
            worse.append(updated)
        changed.append(updated)
        journal("improvement_measured", f"{row['id']}: {effect['verdict']}")
    if changed:
        _put(repo, changed)
    return worse


def _day(when: datetime) -> str:
    return f"{when.day} {when.strftime('%b')}"


def _span(since: Optional[str], until: Optional[str]) -> str:
    a, b = _when(since), _when(until)
    if b is None:
        return "all history"
    if a is None:
        return f"to {_day(b)}"
    if a.date() == b.date():
        return _day(b)
    if (a.year, a.month) == (b.year, b.month):
        return f"{a.day}–{_day(b)}"
    return f"{_day(a)}–{_day(b)}"


def digest(repo: Path, record: Mapping[str, Any]) -> Optional[str]:
    """The run's one message (§7.4), or `None` when it has no effect line, no new
    proposal, no withdrawn question and no suppression."""
    measured, asked = record.get("measured") or [], record.get("asked") or []
    suppressed = record.get("suppressed") or []
    withdrawn = record.get("superseded") or []
    if not (measured or asked or suppressed or withdrawn):
        return None
    window = record.get("window") or {}
    lines = [f"📈 Maestro improvement — {slug(repo)}, {record.get('kind')}, "
             f"{_span(window.get('since'), window.get('until'))}: "
             f"{record.get('tasks', 0)} accepted tasks."]
    if measured:
        lines.append("Effects: " + "; ".join(m["line"] for m in measured))
    if asked:
        lines.append(f"New: {', '.join(asked)} (asked below).")
    deferred = [p for p in record.get("proposals") or () if p not in asked]
    if deferred:
        lines.append(f"Deferred to the next run: {', '.join(deferred)}.")
    if withdrawn:
        lines.append(f"Withdrawn unanswered: {', '.join(withdrawn)} (outranked; asked again "
                     "when it ranks).")
    if suppressed:
        lines.append("Not re-sent: " + "; ".join(
            f"{s.get('lever')} {s.get('target') or '-'} ({s.get('reason')})"
            for s in suppressed) + ".")
    call = record.get("call")
    if call:
        tries = call.get("tries", 0)
        lines.append(f"Analyst: {tries} call{'' if tries == 1 else 's'}, "
                     f"{call.get('chars_in', 0) / 1000:.1f}k chars, {call.get('role')} route.")
    else:
        lines.append("Analyst: no call (no pattern).")
    return "\n".join(lines)


# --- one run (R57, R65) ---------------------------------------------------------------------


def _row(p: Mapping, pattern: Mapping, run_id: str, at: str, view: Sequence[Mapping],
         repo: Path, min_tasks: int, rows: Sequence[Mapping]) -> dict:
    cfg = p.get("config")
    config = None
    if isinstance(cfg, Mapping):
        current = next((r.get("value") for r in view if r.get("path") == cfg.get("path")), None)
        config = {"path": cfg.get("path"), "value": cfg.get("value"), "previous": current}
    es = p.get("expected_saving") or {}
    revises = p.get("revises")
    row = {
        "id": None, "run": run_id, "at": at, "key": key_of(p, pattern), "project": slug(repo),
        "project_key": project_key(repo),
        "pattern": {k: pattern.get(k) for k in ("id", "kind", "key", "rows", "tasks",
                                                "evidence", "spend")},
        "lever": p.get("lever"), "target": _text(p.get("target")),
        "change": _text(p.get("change")), "config": config,
        "evidence": list(p.get("evidence") or ()),
        "expected_saving": {"cost_units": es.get("cost_units"), "pool": es.get("pool"),
                            "basis": _text(es.get("basis"))},
        "risk": _text(p.get("risk")), "cost": _text(p.get("cost")),
        "measure": {"metrics": list((p.get("measure") or {}).get("metrics") or ()),
                    "min_tasks": min_tasks},
        "rollback": _text(p.get("rollback")), "why": _text(p.get("why")),
        "alternatives": [str(a) for a in p.get("alternatives") or () if _text(a)],
        "revises": revises, "state": None, "question_id": None, "decision": None,
        "applied": None, "effect": None, "rollback_question_id": None, "new_evidence": None,
    }
    if revises:
        noted = next((r for r in rows if r.get("id") == revises), {})
        row["revised_note"] = (noted.get("decision") or {}).get("answer")
    return row


def _store_run(repo: Path, record: Mapping) -> None:
    _update(repo, "runs", lambda rows: rows + [dict(record)])


def _analyse(repo, db, store, artifacts, record, *, kind, at, analyse, channel, journal,
             notify):
    ie.attribute(repo, store, artifacts, at=at, journal=journal)
    stored = read(repo)
    runs = stored["runs"]
    record["id"] = f"IR-{len(runs) + 1:04d}"
    since = _since(runs)
    record["window"] = {"since": since, "until": at}
    worse = _effects(repo, db, record, stored["proposals"], at=at, journal=journal)
    rows = read(repo)["proposals"]
    pack, found, report = _evidence(repo, db, since=since, until=at, earlier=rows)
    record["tasks"] = pack.get("tasks", 0)
    record["patterns"] = [p["id"] for p in found]
    asked: list[dict] = []
    if not found:
        record["status"] = "empty"
        done = ("improvement_run_empty",
                f"{record['id']} {kind}: {record['tasks']} task(s), no pattern; no call")
    else:
        # Review M4a: an outranked, still-unanswered question is closed before the new
        # ones go (inside `_propose`, before its row is withdrawn).
        done, asked = _propose(repo, store, record, pack, found, report, rows, kind=kind, at=at,
                               analyse=analyse, journal=journal,
                               supersede=getattr(channel, "supersede_question", None))
    _store_run(repo, record)
    text = digest(repo, record)
    if text:
        notify(text)
    for row in read(repo)["proposals"]:
        if row.get("id") in record["superseded"]:
            qid = (row.get("superseded") or {}).get("question_id")
            journal("improvement_question_superseded",
                    f"{row['id']}: {qid} outranked in {record['id']}")
    for row in asked:
        if channel is not None:
            channel.poll_question(question(row))
        journal("improvement_proposal_asked",
                f"{row['id']} {row['lever']} {row['target'] or '-'} on {row['pattern']['id']}")
    for row in worse:
        if channel is not None:
            channel.poll_question(rollback_question(row))
        journal("improvement_rollback_asked", f"{row['id']}: measured worse")
    journal(*done)


def _propose(repo, store, record, pack, found, report, rows, *, kind, at, analyse, journal,
             supersede=None):
    """The analyst call, its validation and the ranked candidates: `(the run's journal
    entry, the proposals to ask)`; the run record is filled in place. `supersede` closes
    an outranked question (the channel's `supersede_question`; None without a channel)."""
    system, user = brief(pack)
    view = ie.config_view(repo)
    call = {"role": ROLE_DIAGNOSER, "tries": 0, "chars_in": 0, "chars_out": 0, "seconds": 0.0}
    record["call"] = call
    problems: list[str] = []
    document = None
    for _ in range(MAX_TRIES):
        text = _with_problems(user, problems) if problems else user
        call["tries"] += 1
        call["chars_in"] += len(system) + len(text)
        started = time.monotonic()
        try:
            raw = analyse(system, text) or ""
        except Exception as exc:  # noqa: BLE001 — a failed call is a problem, then a retry
            raw = ""
            journal("improvement_analyst_error", f"{type(exc).__name__}: {exc}"[:300])
        call["seconds"] = round(call["seconds"] + time.monotonic() - started, 3)
        call["chars_out"] += len(raw)
        document, problem = _parse(raw)
        problems = [problem] if problem else check(document, pack, rows, view, patterns=found)
        if not problems:
            break
    if problems:
        record.update(status="failed", problems=problems)
        return ("improvement_analysis_failed",
                f"{record['id']} {kind}: {call['tries']} tries; {problems[0]}"[:300]), []
    min_tasks = max(1, _threshold(repo, "improvement_min_tasks", DEFAULT_MIN_TASKS))
    full = {p["id"]: p for p in found}
    rank = {p["id"]: i for i, p in enumerate(found)}
    times = _times(store, report, repo)
    # Earlier deferred proposals whose pattern still holds are candidates again, and so are
    # the still-unanswered asked ones (review M4a): they rank with this run's candidates,
    # at most MAX_ASKED questions stay open, and one a new question outranks is withdrawn.
    still_open = [r for r in rows if r.get("state") == "asked"]
    again = [r for r in rows if r.get("state") in ("deferred", "asked")
             and (r.get("pattern") or {}).get("id") in full]
    taken = {r["key"]: r["id"] for r in again}
    fresh = [(n, _row(p, full[p["pattern"]], record["id"], at, view, repo, min_tasks, rows))
             for n, p in enumerate(document["proposals"])]

    def order(item):
        n, row = item
        cu = row["expected_saving"].get("cost_units")
        return (cu is None, -(cu or 0), rank.get(row["pattern"]["id"], len(rank)), n)

    ranked = sorted([(-1, r) for r in again] + fresh, key=order)
    number = max([int(str(r.get("id"))[3:]) for r in rows
                  if re.fullmatch(r"IP-\d+", str(r.get("id")))] or [0])
    chosen, asked, kept = [], 0, set()
    for _, row in ranked:
        if row["id"] is None:                       # a new candidate
            reason, of, new = (f"already a candidate as {taken[row['key']]}",
                               taken[row["key"]], None) if row["key"] in taken \
                else _memory(row, rows, times)
            if reason:
                record["suppressed"].append({"key": row["key"], "of": of, "reason": reason,
                                             "lever": row["lever"], "target": row["target"]})
                journal("improvement_proposal_suppressed",
                        f"{row['lever']} {row['target'] or '-'} on {row['pattern']['id']}: "
                        f"{reason}")
                continue
            number += 1
            row.update(id=f"IP-{number:04d}", new_evidence=new)
            taken[row["key"]] = row["id"]
        row = dict(row)
        if row.get("state") == "asked":             # still open from an earlier run
            if asked < MAX_ASKED:
                asked += 1
                kept.add(row["id"])
            continue                                # never re-sent; maybe withdrawn below
        if asked < MAX_ASKED:
            asked += 1
            # A row asked before (then withdrawn) gets a fresh question id, so a tap on
            # its old, superseded message is never read as this answer.
            qid = (f"dq-improve-{row['id']}" if not row.get("question_id")
                   else f"dq-improve-{row['id']}-{record['id']}")
            row.update(state="asked", question_id=qid)
        else:
            row["state"] = "deferred"
        chosen.append(row)
    new = [r for r in chosen if r["state"] == "asked"]
    withdrawn = []
    if new:
        for r in still_open:
            if r["id"] in kept:
                continue
            qid = question(r)["id"]
            # The question is closed before its row is withdrawn. One Dan answered while
            # the analyst ran (the loop was blocked, so only its record knows) cannot be
            # closed: its row stays open and the next poll records his answer.
            if supersede is not None and not supersede(qid):
                continue
            withdrawn.append({**r, "state": "deferred", "superseded": {
                "at": at, "by": record["id"], "question_id": qid}})
    record["proposals"] = [r["id"] for r in chosen]
    record["asked"] = [r["id"] for r in new]
    record["superseded"] = [r["id"] for r in withdrawn]
    _put(repo, chosen + withdrawn)
    record["status"] = "done"
    return ("improvement_run_done",
            f"{record['id']} {kind}: {record['tasks']} task(s), {len(found)} pattern(s), "
            f"{len(new)} asked, {len(chosen) - len(new)} deferred, "
            f"{len(kept)} still open, {len(withdrawn)} withdrawn, "
            f"{len(record['suppressed'])} suppressed"), new


def run(repo: Path, db, store, artifacts, *, kind: str, key: str, at: str,
        analyse: Optional[Callable[[str, str], str]] = None, channel=None,
        notify: Optional[Callable[[str], None]] = None,
        journal: Optional[Callable[[str, str], None]] = None,
        apply: Optional[Callable] = None, queue: Optional[Callable] = None) -> dict:
    """One analysis run (`kind` weekly | project_end, keyed `key`); returns the stored run.
    It first measures every applied proposal's effect (a `worse` one is asked about), then
    analyses the window, then sends one digest (`notify`) and the questions. Never raises:
    an exception journals `improvement_failed` and stores the run `failed`. `apply` and
    `queue` are accepted for symmetry with `poll` (decisions are applied there)."""
    journal = journal or getattr(channel, "journal", None) or _noop
    record = {"id": None, "kind": kind, "key": key, "at": at,
              "window": {"since": None, "until": at}, "status": "failed", "tasks": 0,
              "patterns": [], "proposals": [], "asked": [], "superseded": [],
              "suppressed": [], "measured": [], "call": None, "problems": []}
    try:
        _analyse(repo, db, store, artifacts, record, kind=kind, at=at,
                 analyse=analyse or analyst, channel=channel, journal=journal,
                 notify=notify or _noop)
    except Exception as exc:  # noqa: BLE001 — the loop never stops on an analysis
        problem = f"{type(exc).__name__}: {exc}"[:300]
        journal("improvement_failed", problem)
        record.update(status="failed", problems=[*record["problems"], problem])
        try:
            if record["id"] is None:
                record["id"] = f"IR-{len(read(repo)['runs']) + 1:04d}"
            if not any(r.get("id") == record["id"] for r in read(repo)["runs"]):
                _store_run(repo, record)
        except Exception:  # noqa: BLE001
            pass
    return record


def _poll_rollback(repo: Path, channel, row: Mapping, at: str, undo, journal,
                   notify) -> Optional[dict]:
    """Dan's answer to a rollback question: his tap on Roll back goes to `undo`; Keep (or
    any other text, kept as his note) keeps it. Nothing is rolled back without the tap. An
    undo that raises is `rollback_failed`, journaled and told (review M2): his decision is
    already stored, so no later poll would read it again."""
    spec = rollback_question(row)
    answer = channel.poll_question(spec)
    if answer is None:
        return None
    text = str(answer).strip()
    roll = text.lower() == ROLL_BACK.lower()
    decision = {"resolution": "roll_back" if roll else "keep", "answer": text, "at": at}
    updated = {**row, "rollback_decision": decision}
    if not roll:
        updated["state"] = "kept"
    _put(repo, [updated])
    mark = getattr(channel, "mark_question_recorded", None)
    if mark is not None:
        mark(spec["id"], improvement_rollback=decision)
    journal("improvement_rollback_decided", f"{row['id']}: {decision['resolution']}")
    if roll and undo is not None:
        try:
            changed = undo(updated)
        except Exception as exc:  # noqa: BLE001
            changed = _failed_rollback(repo, updated, exc, at, journal, notify)
        if isinstance(changed, Mapping):
            updated = {**updated, **changed}
            _put(repo, [updated])
    return updated


def poll(repo: Path, channel, at: str, *, apply: Optional[Callable] = None,
         queue: Optional[Callable] = None, undo: Optional[Callable] = None,
         journal: Optional[Callable[[str, str], None]] = None,
         notify: Optional[Callable[[str], None]] = None) -> list[dict]:
    """Read Dan's answers to the asked proposals and record them; an approval goes to
    `apply` (a config lever) or `queue` (a code lever), which may return fields to store.
    A measured-worse proposal's rollback question is read too: Roll back goes to `undo`.
    Returns the decided proposals. A failing row is journaled, never raised."""
    if channel is None:
        return []
    journal = journal or getattr(channel, "journal", None) or _noop
    decided = []
    for row in read(repo)["proposals"]:
        if row.get("state") == "measured" and row.get("rollback_question_id") \
                and not row.get("rollback_decision"):
            try:
                updated = _poll_rollback(repo, channel, row, at, undo, journal,
                                         notify or _noop)
                if updated is not None:
                    decided.append(updated)
            except Exception as exc:  # noqa: BLE001
                journal("improvement_poll_failed",
                        f"{row.get('id')}: {type(exc).__name__}: {exc}"[:300])
            continue
        if row.get("state") != "asked":
            continue
        try:
            spec = question(row)
            answer = channel.poll_question(spec)
            if answer is None:
                continue
            decision = decide(row, answer, at)
            updated = {**row, "state": _STATE[decision["resolution"]], "decision": decision}
            _put(repo, [updated])
            mark = getattr(channel, "mark_question_recorded", None)
            if mark is not None:
                mark(spec["id"], improvement_decision=decision)
            journal("improvement_decided",
                    f"{row['id']}: {decision['resolution']} {decision['answer'][:120]}")
            if decision["resolution"] == "approve":
                act = apply if isinstance(row.get("config"), Mapping) else queue
                changed = act(updated) if act is not None else None
                if isinstance(changed, Mapping):
                    updated = {**updated, **changed}
                    _put(repo, [updated])
            decided.append(updated)
        except Exception as exc:  # noqa: BLE001 — one bad row never stops the others
            journal("improvement_poll_failed",
                    f"{row.get('id')}: {type(exc).__name__}: {exc}"[:300])
    return decided


# --- the read-only report -------------------------------------------------------------------


def report(repo: Path) -> dict:
    """`{runs, proposals, effects}`, read-only (the JSON W10's digest can embed)."""
    stored = read(repo)
    return {"runs": stored["runs"], "proposals": stored["proposals"],
            "effects": [{"id": p.get("id"), "effect": p["effect"]}
                        for p in stored["proposals"] if p.get("effect")]}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m maestro.improvement")
    sub = parser.add_subparsers(dest="command", required=True)
    shown = sub.add_parser("report", help="the runs, proposals and effects (read-only JSON)")
    shown.add_argument("--repo", default=".")
    pack = sub.add_parser("evidence", help="the analyst's pack for the next window; no call")
    pack.add_argument("--repo", default=".")
    pack.add_argument("--database", default=None)
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    if args.command == "report":
        out = report(repo)
    else:
        db = Path(args.database) if args.database else repo / ".orchestrator" / "control.sqlite3"
        stored = read(repo)
        out, _ = evidence(repo, db, since=_since(stored["runs"]), until=None,
                          earlier=stored["proposals"])
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
