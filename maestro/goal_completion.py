"""The W11b completion gate: done only when the contract is met at this revision and commit
(P13 GDT-10).

When nothing is left to run (the graph and legacy loops' queue exhaustion) and on
`maestro goals finish`, `evaluate` judges a format-2 project against its goals contract
(`docs/GOALS.md`) instead of calling it finished because the queue is empty (R54):

- **The verification run** (`run`, R53, R67, R24): every mandatory acceptance criterion, and
  every criterion a hard invariant names in `checked by`, rerun on the controller's checkout
  at (contract revision, HEAD), in budgeted slices (`goal_milestones.budget_for`) that
  resume across polls and restarts; nothing runs twice at one key. A metric runs
  `METRIC_RUNS` times and only a uniform result counts (a mix is UNKNOWN, C15). The run is
  stored in the goals store's `finish` section and, once complete, as `finish`
  observations (`goal_state.record_finish`). Only this run is final evidence: gate
  observations, checkpoints, review episodes and older revisions close nothing.
- **Trust** (R24, R25): a bound test changed since the commit the baseline ran on without a
  graduated task naming the criterion in `test_corrections:` is untrusted, and so is every
  bound test when that diff cannot be read; tracked uncommitted changes (other than the
  ROADMAP, `.orchestrator/` and the views the loop regenerates) block `complete`, and a
  run any part of which saw them is rerun once the tree is clean; a scenario proved only
  by unit-level criteria and an invariant with no check are Dan's contract changes.
- **The verdict** (`verdict`, pure, R53): every item is `met`, `work` (the loop can act) or
  `dan` (Dan, or an outside change). `complete` needs every mandatory item met;
  `incomplete` when any item is work; otherwise `blocked`.
- **Operator checks** (R14, C9): asked through the existing question path only once every
  other mandatory item is met, on the final tree; a sign-off counts only at the same
  revision and commit (`poll` records Dan's Pass, Fail or his own words).
- **The corrective loop** (R55): one `finish_gap` replan per goal with work, in priority
  order, through GDT-8's store, apply/ask and landing path (`goal_replan`), capped by the
  contract's stop policy (`round_cap`); at the cap Dan is asked once per facts, and the
  project stays blocked, never done.
- **Finish-stage conflicts** (R47): a protected criterion RED while another goal advanced
  goes through GDT-7's `judge`/`escalation`/`decide` with stage `finish`.
- **The outcome report** (R56): `render` writes `REPORT_MD` and `REPORT_JSON` (gitignored,
  rewritten on each evaluation).

Without `docs/GOALS.md` nothing is read or written; a format-1 contract is not judged.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import subprocess
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from maestro import acceptance, backlog as _backlog, goal_store
from maestro.docs import goals as doc_goals

VERDICTS = ("complete", "incomplete", "blocked")
#: The corrective rounds per goal when neither the contract's stop policy nor
#: `thresholds.goal_finish_rounds` names a number (the failure ladder's final run).
DEFAULT_ROUNDS = 2
#: How often a metric criterion is measured; only a uniform result counts (C15).
METRIC_RUNS = 3
REPORT_MD = ".orchestrator/goal_outcome.md"
REPORT_JSON = ".orchestrator/goal_outcome.json"
#: The completion gate's question ids (operator checks: `dq-finish-op-`).
QUESTION_PREFIX = "dq-finish-"
OP_PREFIX = "dq-finish-op-"
#: Finish rows kept in the store (each new HEAD or revision adds one).
_KEEP_ROWS = 20
_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
            "eight": 8, "nine": 9}
_FIRST_NUMBER = re.compile(r"\b(\d+|" + "|".join(_NUMBERS) + r")\b", re.I)
_SHORT = {acceptance.GREEN: "GREEN", acceptance.RED: "RED", acceptance.BLOCKED: "BLOCKED",
          acceptance.UNKNOWN: "UNKNOWN", acceptance.OPERATOR: "OPERATOR"}
#: Internal failures already told to Dan by this process, by exception type and where it
#: was raised (its text can name a temp file that changes every poll; the store may be
#: what failed).
_FAILED_TOLD: set = set()
#: Verdicts kept on a FIN row (what was journaled and reported last).
_KEEP_VERDICTS = 10


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _short(state: Any) -> str:
    return _SHORT.get(str(state), str(state).upper())


def _clip(text: Any, n: int = 160) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _hash(*parts: Any) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def finish_id(revision: Any, commit: str) -> str:
    """The verification run's key: one per (contract revision, commit)."""
    return "FIN-" + hashlib.sha1(f"{revision}|{commit}".encode()).hexdigest()[:8]


def round_cap(doc: doc_goals.Goals, repo: Path) -> int:
    """The corrective rounds per goal: the first number (digits, or one to nine) in the
    contract's `Stop a failed approach when:`, else `project.yaml`
    `thresholds.goal_finish_rounds`, else `DEFAULT_ROUNDS`; below 1 means the default."""
    text = str((doc.authority or {}).get("stop a failed approach when") or "")
    found = _FIRST_NUMBER.search(text)
    if found:
        word = found.group(1).lower()
        value: Any = int(word) if word.isdigit() else _NUMBERS[word]
    else:
        from maestro import config

        value = config.threshold("goal_finish_rounds", None, cast=int,
                                 document=config.effective_config(repo)[0])
    return value if isinstance(value, int) and value >= 1 else DEFAULT_ROUNDS


def targets(doc: doc_goals.Goals) -> list[doc_goals.Criterion]:
    """What the run reruns: every mandatory criterion and every criterion a hard invariant
    names, in contract order."""
    checkers = {cid for inv in doc.invariants for cid in inv.checked_by}
    return [c for c in doc.criteria if c.mandatory or c.id in checkers]


def _goals(doc: doc_goals.Goals, entry: Any) -> list[str]:
    from maestro import goal_roadmap

    if isinstance(entry, doc_goals.Criterion):
        return goal_roadmap._goals_of(doc, entry)
    return doc_goals.entry_goals(doc, entry)


def _ranked(doc: doc_goals.Goals) -> list[str]:
    """The goals in Priority order, then contract order."""
    first = [g for g in doc.priority if g in doc.ids]
    return list(dict.fromkeys([*first, *doc.ids]))


# --- the verification run ----------------------------------------------------------------


def _row(repo: Path, fid: str) -> Optional[dict]:
    return next((dict(r) for r in goal_store.read(repo)["finish"] if r.get("id") == fid), None)


def _put(repo: Path, row: Mapping[str, Any]) -> None:
    def mutate(rows: list[dict]) -> list[dict]:
        kept = [r for r in rows if r.get("id") != row.get("id")] + [dict(row)]
        return kept[-_KEEP_ROWS:]

    goal_store.update(repo, "finish", mutate)


def _patch(repo: Path, fid: str, **fields) -> dict:
    """Merge `fields` into the stored row `fid` (atomically, keeping what others wrote to
    it); returns the row."""
    found: dict = {}

    def mutate(rows: list[dict]) -> list[dict]:
        current = next((dict(r) for r in rows if r.get("id") == fid), None)
        if current is None:
            return rows
        current.update(fields)
        found.update(current)
        return ([r for r in rows if r.get("id") != fid] + [current])[-_KEEP_ROWS:]

    goal_store.update(repo, "finish", mutate)
    return found


def _put_result(repo: Path, row: dict, cid: str, entry: Mapping[str, Any]) -> dict:
    """Write one criterion's result into the stored row as soon as it exists."""
    found: dict = {}

    def mutate(rows: list[dict]) -> list[dict]:
        current = next((dict(r) for r in rows if r.get("id") == row["id"]), dict(row))
        current["results"] = {**(current.get("results") or {}), cid: dict(entry)}
        found.update(current)
        return ([r for r in rows if r.get("id") != row["id"]] + [current])[-_KEEP_ROWS:]

    goal_store.update(repo, "finish", mutate)
    return found


def _entry(result: acceptance.Result, **extra) -> dict:
    return {"state": result.state, "reason": result.reason, "value": result.value,
            "values": [result.value] if result.value is not None else [],
            "seconds": result.seconds, "exit_code": result.exit_code,
            "command": " ".join(result.command), "at": _now(), **extra}


def _run_one(repo: Path, c: doc_goals.Criterion, revision: Any, timeout: int,
             runner, beat: Optional[Callable[[str], Any]] = None) -> dict:
    if c.evidence != "metric" or not c.test.strip():
        return _entry(acceptance.run_criterion(repo, c, revision=revision, timeout=timeout,
                                               runner=runner))
    runs = []
    for k in range(METRIC_RUNS):
        if k and beat is not None:
            # Each repeat can take its whole timeout: a heartbeat before each keeps the
            # watchdog's stall window from seeing three timeouts of silence (review L1).
            beat(f"repeat {k + 1} of {METRIC_RUNS}")
        runs.append(acceptance.run_criterion(repo, c, revision=revision, timeout=timeout,
                                             runner=runner))
    states = {r.state for r in runs}
    values = [r.value for r in runs if r.value is not None]
    shown = ", ".join(f"{v:g}" for v in values) or "no number"
    median = statistics.median(values) if values else None
    against = f"against {c.threshold}" if c.threshold else "against no threshold"
    if states in ({acceptance.GREEN}, {acceptance.RED}):
        state = runs[0].state
        reason = (f"measured {shown} (median {median:g}) {against}" if median is not None
                  else runs[0].reason)
    else:
        state = acceptance.UNKNOWN
        odd = next((r.reason for r in runs if r.state not in (acceptance.GREEN,
                                                              acceptance.RED)), "")
        reason = f"unstable: {shown} {against}" + (f" ({odd})" if odd else "")
    last = runs[-1]
    return {"state": state, "reason": _clip(reason, 500), "value": median, "values": values,
            "seconds": round(sum(r.seconds for r in runs), 3), "exit_code": last.exit_code,
            "command": " ".join(last.command), "at": _now()}


def run(repo: Path, doc: doc_goals.Goals, *, commit: str, at: str = "", runner=None,
        timeout: int = acceptance.DEFAULT_TIMEOUT, budget: Optional[float] = None,
        clock: Optional[Callable[[], float]] = None,
        heartbeat: Optional[Callable[[str], Any]] = None,
        journal: Optional[Callable[[str, str], Any]] = None) -> dict:
    """One budgeted slice of the verification run at (`doc.revision`, `commit`); returns
    the stored row. Each result is stored as soon as it exists; the next slice (or a fresh
    process) continues where this one stopped and never reruns a criterion with a result.
    At least one criterion runs per slice. Once every target has a result the row is
    `done` and stored as `finish` observations. A row any part of which ran on a tree
    with uncommitted changes (`dirty`) is not final evidence: once the tree is clean at
    the same HEAD its results are dropped and the run starts again (review M2)."""
    from maestro import goal_state

    runner = runner or subprocess.run
    clock = clock or time.monotonic
    fid = finish_id(doc.revision, commit)
    row = _row(repo, fid)
    if row is None or row.get("revision") != doc.revision or row.get("commit") != commit:
        row = {"id": fid, "revision": doc.revision, "commit": commit,
               "started_at": at or _now(), "done": False, "results": {}, "verdicts": [],
               "told": [], "dirty": False}
        _put(repo, row)
    elif row.get("dirty") and not dirty(repo):
        if journal is not None:
            journal("goal_finish_running", f"{fid}: the tree is clean again; rerunning what "
                                           "ran on uncommitted changes")
        row = _patch(repo, fid, done=False, results={}, dirty=False,
                     started_at=at or _now())
    if row.get("done"):
        return row

    def seen_dirty(current: dict) -> dict:
        return current if current.get("dirty") or not dirty(repo) else \
            _patch(repo, fid, dirty=True)

    todo = targets(doc)
    start, ran = clock(), False
    for i, c in enumerate(todo, 1):
        if c.id in (row.get("results") or {}):
            continue
        if ran and budget is not None and clock() - start >= budget:
            break
        where = f"{fid} {c.id} ({i}/{len(todo)})"
        if heartbeat is not None:
            heartbeat(where)
        row = seen_dirty(row)
        entry = _run_one(repo, c, doc.revision, timeout, runner,
                         None if heartbeat is None else
                         (lambda text, where=where: heartbeat(f"{where} {text}")))
        ran = True
        row = _put_result(repo, row, c.id, entry)
    if all(c.id in (row.get("results") or {}) for c in todo):
        row = {**seen_dirty(row), "done": True}
        _put(repo, row)
        try:
            goal_state.record_finish(repo, doc, row)
        except Exception as exc:  # noqa: BLE001 — the observation copy is best-effort
            if journal is not None:
                journal("goal_state_failed", f"{fid}: {type(exc).__name__}: {exc}"[:300])
    return row


# --- trust -------------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True,
                              timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None


def dirty(repo: Path) -> Optional[list[str]]:
    """Tracked uncommitted changes outside `docs/ROADMAP.md`, `.orchestrator/` and the views
    the loop regenerates after every launch (`merge._MERGE_DISCARDABLE`: the dependency
    map and UPCOMING, review H1): the tests did not run on HEAD. `None` when this is not a
    git checkout; raises when git does not answer (timed out or could not start), which
    says nothing about the checkout (review L9)."""
    from maestro.merge import _MERGE_DISCARDABLE

    proc = _git(repo, "status", "--porcelain", "--untracked-files=no")
    if proc is None:
        raise RuntimeError("git status did not answer (timed out or could not start)")
    if proc.returncode != 0:
        return None
    out = []
    for line in proc.stdout.splitlines():
        path = line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path and path != "docs/ROADMAP.md" and not path.startswith(".orchestrator/") \
                and path not in _MERGE_DISCARDABLE:
            out.append(path)
    return out


def _graduated(tasks: Sequence[Mapping[str, Any]], completed: Iterable[str]) -> set[str]:
    return {str(i) for i in completed} | {str(t.get("id")) for t in tasks
                                          if t.get("id") and t.get("status") == "complete"}


def _corrections(repo: Path, base: str, tasks: Sequence[Mapping[str, Any]],
                 completed: Iterable[str] = ()) -> list[str]:
    """Every criterion a graduated task named in `test_corrections:`, read from its own
    entry: the current ROADMAP, else the newest ROADMAP revision since the baseline's
    commit that has it (a graduated task leaves the ROADMAP). An open task's declaration
    has not run, so it excuses nothing (review L3)."""
    from maestro import taskgraph

    graduated = _graduated(tasks, completed)
    entries = {str(t.get("id")): t for t in tasks if str(t.get("id")) in graduated}
    missing = graduated - set(entries)
    if missing:
        log = _git(repo, "log", "--format=%H", f"{base}..HEAD", "--", "docs/ROADMAP.md")
        for sha in [*((log.stdout.split() if log and log.returncode == 0 else [])), base]:
            if not missing:
                break
            shown = _git(repo, "show", f"{sha}:docs/ROADMAP.md")
            if shown is None or shown.returncode != 0:
                continue
            try:
                found = taskgraph.scan_tasks(shown.stdout)
            except Exception:  # noqa: BLE001 — an unreadable revision names nothing
                continue
            for task in found:
                tid = str(task.get("id"))
                if tid in missing:
                    entries[tid] = task
                    missing.discard(tid)
    out: list[str] = []
    for task in entries.values():
        raw = task.get("test_corrections") or []
        for item in raw if isinstance(raw, list) else [raw]:
            out.append(str(item).strip().upper())
    return list(dict.fromkeys(out))


def trust(repo: Path, doc: doc_goals.Goals, tasks: Sequence[Mapping[str, Any]],
          completed: Iterable[str] = ()) -> tuple[dict[str, str], list[str]]:
    """`({criterion: finding}, limitations)`: the bound tests changed since the commit the
    baseline ran on without a graduated task's correction naming them (R24, C13); every
    bound test when that diff cannot be read (never permission to proceed, review L4)."""
    from maestro import goal_state
    from maestro.workflows import goal_guard

    base = str(goal_state._baseline_meta(repo).get("commit") or "")
    if not base:
        return {}, ["bound tests not compared: the baseline has no commit (rerun "
                    "`maestro goals baseline` to record one)"]
    bound: dict[str, list[str]] = {}
    for c in doc.criteria:
        for path in goal_guard._bound_paths(c, repo):
            bound.setdefault(path, []).append(c.id)
    findings = goal_guard.tamper_findings(repo, {
        "base": base, "bound_files": bound,
        "corrections": _corrections(repo, base, tasks, completed)})
    known = {c.id for c in doc.criteria}
    out: dict[str, str] = {}
    limits: list[str] = []
    for finding in findings:
        if not finding.startswith("acceptance_test_"):
            if finding.startswith("goals_guard_diff_unavailable"):
                for cid in (i for ids in bound.values() for i in ids):
                    out.setdefault(cid, f"bound tests not compared against {base[:12]}: "
                                        f"{finding}")
            continue
        for cid in doc_goals._ids(finding):
            if cid in known:
                out.setdefault(cid, finding)
    return out, limits


# --- the verdict -------------------------------------------------------------------------


def _conflicts(judgement) -> dict[str, tuple[str, str]]:
    """Per criterion: how the finish-stage conflict judgement treats its regression."""
    out: dict[str, tuple[str, str]] = {}
    if judgement is None:
        return out
    for q in judgement.questions:
        conflict = q.get("conflict") or {}
        for cid in conflict.get("criteria") or ():
            out.setdefault(str(cid), ("ask", str(conflict.get("id") or "")))
    for a in judgement.accepted:
        for cid in a.get("criteria") or ():
            out.setdefault(str(cid), ("accepted", str(a.get("conflict") or "")))
    for text in judgement.blocking:
        kind = "defer" if text.startswith("goal conflict ") else "blocking"
        for cid in doc_goals._ids(text):
            if cid.startswith("AC-"):
                out.setdefault(cid, (kind, text))
    return out


def _why_not(tasks: Sequence[Mapping[str, Any]], holds: Mapping[str, str],
             parked: Iterable[str], drift: Iterable[str], completed: Iterable[str]
             ) -> Callable[[Mapping[str, Any]], str]:
    """Why the loop cannot run a task: `""` when it can, now or once its open dependencies
    run (review H2). The loop's own predicate (`roadmap.unrunnable`), with the reasons it
    folds into one held set named (parked, a goals-gate or replan hold, a drift conflict),
    and each dependency followed to its own reason."""
    from maestro.docs import roadmap

    by_id = {str(t.get("id")): t for t in tasks if t.get("id")}
    done = _graduated(tasks, completed)
    parked, drift = set(parked), set(drift)
    memo: dict[str, str] = {}

    def why(tid: str, trail: tuple = ()) -> str:
        if tid in memo:
            return memo[tid]
        if tid in parked:
            out = f"is parked: /unpark {tid}"
        elif tid in holds:
            out = f"is held: {holds[tid]}"
        elif tid in drift:
            out = "is held by a ROADMAP drift conflict (a hand edit under the live loop)"
        else:
            reason = roadmap.unrunnable(by_id[tid], done)
            out = "" if reason in ("", roadmap.WAITS) else f"cannot be dispatched: {reason}"
            for dep in roadmap.pending_deps(by_id[tid], done) if not out else ():
                if dep not in by_id:
                    out = f"waits for {dep}, which is not in the ROADMAP"
                elif dep == tid or dep in trail:
                    out = f"waits for {dep}, which is in a dependency cycle"
                elif sub := why(dep, (*trail, tid)):
                    out = f"waits for {dep}, which {sub}"
                if out:
                    break
        memo[tid] = out
        return out

    return lambda task: why(str(task.get("id"))) if str(task.get("id")) in by_id else \
        "is not in the ROADMAP"


def _naming(cid: str, tasks: Sequence[Mapping[str, Any]],
            why: Callable[[Mapping[str, Any]], str]) -> tuple[list[str], list[str]]:
    """`(workable task ids, why each other naming task cannot run)` of the open tasks
    naming `cid` in their `criteria:`."""
    workable, stuck = [], []
    for t in tasks:
        tid = str(t.get("id") or "")
        if not tid or t.get("status") == "complete" or \
                cid not in doc_goals.normalise_criteria(t.get("criteria"))[0]:
            continue
        reason = why(t)
        if reason:
            stuck.append(f"{tid} (names {cid}) {reason}")
        else:
            workable.append(tid)
    return workable, stuck


def _cap_text(goal: str, cap: int) -> str:
    return (f"the completion gate's cap ({cap} round{'s' if cap != 1 else ''}) is reached "
            f"for {goal}")


def _item(nid: str, kind: str, status: str, why: str, **extra) -> dict:
    return {"id": nid, "kind": kind, "status": status, "why": why, **extra}


def verdict(doc: doc_goals.Goals, row: Mapping[str, Any], shown: Mapping[str, Any], *,
            signoffs: Mapping[str, Mapping[str, Any]], judgement=None,
            tasks: Sequence[Mapping[str, Any]] = (), parked: Iterable[str] = (),
            untrusted: Optional[Mapping[str, str]] = None, limitations: Sequence[str] = (),
            dirty: Optional[Sequence[str]] = (), rounds: Optional[Mapping[str, int]] = None,
            cap: int = DEFAULT_ROUNDS, asked: Optional[Mapping[str, str]] = None,
            proposals: Sequence[Mapping[str, Any]] = (), baseline=None, act: bool = True,
            asking: bool = False, at: str = "", completed: Iterable[str] = (),
            drift: Iterable[str] = (), backlog: Sequence[Mapping[str, Any]] = ()) -> dict:
    """The completion verdict (pure): every item `met`, `work` or `dan`, and `complete`
    only when the run is done and every item is met; `incomplete` when any item is work;
    otherwise `blocked`. Reads states from `row["results"]` alone (R24, R67). A task
    naming a criterion "can run" only by the loop's own predicate (`_why_not`: `completed`
    ids, `drift`-held ids, `parked`, the holds in `shown`)."""
    results = row.get("results") or {}
    asked = dict(asked or {})
    rounds = dict(rounds or {})
    untrusted = dict(untrusted or {})
    parked = set(parked)
    holds = {str(h.get("task")): str(h.get("reason") or "") for h in shown.get("holds") or ()}
    why_not = _why_not(tasks, holds, parked, drift, completed)
    rank = _ranked(doc)
    conflicts = _conflicts(judgement)
    commit = str(row.get("commit") or "")
    items: dict[str, dict] = {}

    def first_goal(goals: Sequence[str]) -> str:
        return next((g for g in rank if g in goals), "")

    def gate_problems(nid: str) -> list[str]:
        return [p.text for p in doc.gate if nid in p.ids]

    def with_asked(nid: str, why: str) -> str:
        return f"{why}; {asked[nid]}" if nid in asked else why

    def capped(item: dict) -> dict:
        goal = item.get("goal") or ""
        if item["status"] == "work" and not item.get("named") and goal \
                and rounds.get(goal, 0) >= cap:
            item.update(status="dan", capped=goal,
                        why=with_asked(item["id"], f"{_cap_text(goal, cap)}: {item['why']}"))
        return item

    for c in targets(doc):
        e = results.get(c.id) or {"state": acceptance.UNKNOWN, "reason": "not run"}
        state, reason = str(e.get("state")), str(e.get("reason") or "")
        goals = _goals(doc, c)
        item = _item(c.id, "criterion", "work", f"{c.id} {_short(state)}: {_clip(reason)}",
                     state=state, goals=goals, goal=first_goal(goals),
                     mandatory=c.mandatory, evidence=e)
        mode, text = conflicts.get(c.id, ("", ""))
        if c.id in untrusted:
            item.update(status="dan", why=f"{c.id} is untrusted: {untrusted[c.id]}")
        elif problems := gate_problems(c.id):
            item.update(status="dan", why=f"{c.id}: {'; '.join(problems)}")
        elif state == acceptance.GREEN:
            item.update(status="met", why=f"{c.id} GREEN on the finish run")
        elif not c.test.strip():
            item.update(status="dan", why=(f"{c.id} has no test bound yet: only your contract "
                                           "revision binds one (`Test:`)"))
        elif mode == "ask":
            item.update(status="dan", why=with_asked(c.id, (
                f"goal conflict {text} at the completion gate waits on your answer"
                + ("" if asking else "; only the graph loop asks"))))
        elif mode == "accepted":
            item.update(status="dan", why=(f"Dan's accept {text} waits on your contract "
                                           f"revision: {c.id} is still {_short(state)}"))
        elif mode == "defer":
            item.update(status="dan", why=text)
        elif mode == "blocking":
            item.update(why=text)
            capped(item)
        else:
            workable, stuck = _naming(c.id, tasks, why_not)
            if workable:
                item.update(named=True, why=f"{item['why']}; {', '.join(workable)} can run")
            elif stuck:
                item.update(status="dan", why=f"{item['why']}; " + "; ".join(stuck))
            else:
                capped(item)
        items[c.id] = item

    for op in doc.operator_checks:
        if not op.mandatory:
            continue
        goals = doc_goals.entry_goals(doc, op)
        s = signoffs.get(op.id)
        item = _item(op.id, "operator_check", "dan", "", state=acceptance.OPERATOR,
                     goals=goals, goal=first_goal(goals), mandatory=True, signoff=s)
        if s and s.get("result") == "pass":
            item.update(status="met", state="pass",
                        why=f"passed by Dan {s.get('at')} ({s.get('question_id')})")
        elif s:
            seen = s.get("observation") or s.get("answer") or "Fail"
            item.update(status="work", state="fail",
                        why=f"Dan's operator check failed at {commit[:12]}: '{seen}'")
            capped(item)
        elif not act:
            item["why"] = f"{op.id} needs your sign-off; only the graph loop asks"
        elif asking:
            item["why"] = with_asked(op.id, f"{op.id} needs your sign-off, asked once every "
                                            "other mandatory item is met")
        else:
            item["why"] = f"{op.id} needs your sign-off at the completion gate"
        items[op.id] = item

    def derived(nid: str, kind: str, members: Sequence[str], **extra) -> dict:
        found = [items[m] for m in members if m in items]
        if not found:
            return _item(nid, kind, "met", "nothing mandatory proves it", derived=True,
                         **extra)
        unmet = [i["id"] for i in found if i["status"] != "met"]
        status = ("met" if not unmet else
                  "work" if any(i["status"] == "work" for i in found) else "dan")
        return _item(nid, kind, status, f"unmet: {', '.join(unmet)}" if unmet else
                     f"{', '.join(i['id'] for i in found)} met", derived=True, **extra)

    for inv in doc.invariants:
        checks = [cid for cid in inv.checked_by if cid in items]
        if not checks:
            items[inv.id] = _item(inv.id, "invariant", "dan", (
                f"{inv.id} has no negative check: no criterion checks it; only your contract "
                "revision adds one"), text=inv.text)
        else:
            items[inv.id] = derived(inv.id, "invariant", checks, text=inv.text,
                                    checks=checks)

    unit_only = set(doc_goals.unit_only_scenarios(doc))
    for s in doc.scenarios:
        crits, ops = doc_goals.scenario_entries(doc, s)
        members = [c.id for c in crits if c.mandatory] + [o.id for o in ops if o.mandatory]
        if s.id in unit_only:
            items[s.id] = _item(s.id, "scenario", "dan", (
                f"{s.id} has only unit-level proof: no e2e/integration criterion or operator "
                "check; only your contract revision adds one"), name=s.name, members=members)
        else:
            items[s.id] = derived(s.id, "scenario", members, name=s.name)

    optional_only: list[str] = []
    for g in doc.goals:
        verifiers = doc.verifiers(g.id)
        mandatory = [i for i in verifiers if i in items
                     and items[i]["kind"] in ("criterion", "operator_check")
                     and items[i].get("mandatory")]
        if not verifiers:
            items[g.id] = _item(g.id, "goal", "dan", f"{g.id}: nothing verifies it",
                                name=g.name, target=g.target)
        elif not mandatory:
            items[g.id] = _item(g.id, "goal", "met", "only optional verifiers",
                                derived=True, name=g.name, target=g.target)
            optional_only.append(g.id)
        else:
            items[g.id] = derived(g.id, "goal", mandatory, name=g.name, target=g.target)

    problems = list(shown.get("contract_problems") or ()) + [
        p.text for p in doc.gate if not p.ids]
    items["contract"] = _item("contract", "contract", "dan" if problems else "met",
                              "; ".join(problems) or "no contract problem")
    if dirty is None:
        items["checkout"] = _item("checkout", "checkout", "dan", (
            "not a git checkout: the run is tied to no commit"))
    elif dirty:
        items["checkout"] = _item("checkout", "checkout", "dan",
                                  f"uncommitted changes: {', '.join(dirty)}")
    else:
        items["checkout"] = _item("checkout", "checkout", "met", "clean")

    listed = list(items.values())
    roots = [i for i in listed if not i.get("derived")]
    if row.get("done") and all(i["status"] == "met" for i in listed):
        outcome = "complete"
    elif any(i["status"] == "work" for i in listed):
        outcome = "incomplete"
    else:
        outcome = "blocked"
    states = {e["id"]: e for e in shown.get("criteria") or ()}
    deferred = [{"id": c.id, "state": (states.get(c.id) or {}).get("state"),
                 "why": "optional criterion, not rerun"}
                for c in doc.criteria if not c.mandatory and c.id not in items
                and (states.get(c.id) or {}).get("state") != acceptance.GREEN]
    deferred += [{"id": o.id, "state": acceptance.OPERATOR, "why": "optional operator check"}
                 for o in doc.operator_checks if not o.mandatory]
    unmet_text = " ".join(i["why"] for i in listed if i["status"] != "met")
    for t in tasks:
        tid = str(t.get("id") or "")
        named = tid and re.search(rf"(?<![\w-]){re.escape(tid)}(?![\w-])", unmet_text)
        if tid and t.get("status") != "complete" and not named:
            how = ("parked" if tid in parked else "held" if tid in holds else "open")
            deferred.append({"id": tid, "state": how,
                             "why": f"{how}; no unmet mandatory item depends on it"})
    # GDT-11 (R56, `01` §9): review follow-ups the polish stage left are nonblocking.
    left = {str(b.get("id")) for b in backlog if b.get("state") != "merged"}
    for b in backlog:
        if b.get("state") == "merged":
            # Review L2: a duplicate of an item left here keeps its text visible to Dan;
            # one whose kept item was settled is not listed.
            if str(b.get("of")) not in left:
                continue
            why = f'merged into {b.get("of")}: "{b.get("text")}"'
        else:
            why = b.get("reason") or (f"held by {b['task']}" if b.get("task") else
                                      f"raised by {(b.get('source') or {}).get('task')}")
        deferred.append({"id": str(b.get("id")), "state": b.get("state"),
                         "why": f"review follow-up, nonblocking ({why})"})
    trade = [f"{to.id}: {to.text}" + (f"; decision {to.decision}" if to.decision else "")
             + (f"; bound {to.bound}" if to.bound else "")
             + (f"; expires {to.expires}" if to.expires else "") for to in doc.trade_offs]
    trade += [f"{a.get('conflict')}: Dan's {a.get('resolution')} of {a.get('regressed')} "
              f"({', '.join(a.get('criteria') or ())})"
              + (f" up to +{a.get('bound')}%" if a.get("bound") is not None else "")
              + f" ({a.get('at')}), waits on your revision"
              for a in (judgement.accepted if judgement is not None else ())]
    limits = list(limitations) + [
        f"{sid} has only unit-level proof (no e2e/integration criterion or operator check)"
        for sid in sorted(unit_only)] + [
        f"{gid} is proved by nothing mandatory (only optional verifiers)"
        for gid in optional_only]
    if not act:
        limits.append("verdict only: operator checks are asked and corrective rounds run by "
                      "the graph loop")
    fingerprint = _hash(outcome, sorted((i["id"], i["status"], i["why"]) for i in listed))
    return {
        "id": row.get("id") or finish_id(doc.revision, commit), "verdict": outcome,
        "revision": doc.revision, "commit": commit, "at": at, "format": doc.format,
        "title": doc.title, "items": listed,
        "work": [{"id": i["id"], "why": i["why"], "goal": i.get("goal", ""),
                  "state": i.get("state", "")} for i in roots if i["status"] == "work"],
        "dan": [{"id": i["id"], "why": i["why"], "goal": i.get("goal", "")}
                for i in roots if i["status"] == "dan"],
        "proposals": [dict(p) for p in proposals], "accepted_trade_offs": trade,
        "limitations": limits, "deferred": deferred, "rounds": rounds, "cap": cap,
        "report": REPORT_MD, "fingerprint": fingerprint,
        "baseline": {k: {"state": r.state, "value": r.value}
                     for k, r in (baseline or {}).items()},
    }


# --- what Dan is told ---------------------------------------------------------------------


def _done_text(v: Mapping[str, Any], doc: doc_goals.Goals) -> str:
    criteria = sum(1 for i in v["items"] if i["kind"] == "criterion")
    ops = sum(1 for i in v["items"] if i["kind"] == "operator_check")
    return (f"✅ Project complete at contract revision {v['revision']} ({v['commit'][:12]}): "
            f"{len(doc.goals)} goals, {criteria} criteria, {ops} operator checks verified. "
            f"Report: {REPORT_MD} (`maestro goals finish` prints it).")


def message(v: Mapping[str, Any], doc: doc_goals.Goals) -> str:
    """The verdict as Dan reads it (one existing notify)."""
    where = f"revision {v['revision']} ({v['commit'][:12]})"
    if v["verdict"] == "complete":
        return _done_text(v, doc)
    if v["verdict"] == "incomplete":
        unmet = "; ".join(_clip(w["why"], 90) for w in v["work"][:3])
        applied = [p for p in v["proposals"] if p.get("status") == "applied" and p.get("new")]
        if applied:
            r = max(int(p.get("round") or 1) for p in applied)
            ids = ", ".join(t for p in applied for t in p.get("applied") or ())
            rps = ", ".join(p["id"] for p in applied)
            return (f"🔁 Not done yet — incomplete (round {r} of {v['cap']}) at {where}: "
                    f"{unmet}. Added: {ids} ({rps}). Report: {REPORT_MD}")
        return f"🔁 Not done yet — incomplete at {where}: {unmet}. Report: {REPORT_MD}"
    lines = "; ".join(f"{d['id']}: {_clip(d['why'], 200)}" for d in v["dan"][:5])
    return (f"⏸ Not done — blocked at {where}. Waiting on you: {lines}. Nothing else can "
            f"run. Report: {REPORT_MD}")


def _tell(repo: Path, v: Mapping[str, Any], text: str, notify, journal) -> None:
    """Tell Dan once per verdict fingerprint: stored on the run's row before sending, so a
    restart does not resend."""
    fp = v["fingerprint"]
    row = _row(repo, v["id"]) or {}
    if fp in (row.get("told") or ()):
        return
    if row:
        _put(repo, {**row, "told": [*(row.get("told") or ()), fp]})
    try:
        notify(text)
    except Exception as exc:  # noqa: BLE001 — a failed send is journaled, never raised
        journal("goal_finish_failed", f"{v['id']}: notify failed: {type(exc).__name__}: "
                                      f"{exc}"[:300])


# --- the outcome report (R56) ------------------------------------------------------------


def _delta(value: Optional[float], base: Optional[float]) -> str:
    if value is None or base in (None, 0):
        return ""
    pct = (value - base) / abs(base) * 100
    sign = "−" if pct < 0 else "+"
    return f" → {sign}{abs(pct):.0f}%"


def _evidence_line(item: Mapping[str, Any], doc: doc_goals.Goals,
                   baseline: Mapping[str, Mapping[str, Any]]) -> str:
    nid = item["id"]
    if item["kind"] == "operator_check":
        s = item.get("signoff") or {}
        if item["status"] == "met":
            return f"{nid} passed by Dan {s.get('at')} ({s.get('question_id')})"
        if s:
            return f"{nid} failed by Dan: '{s.get('observation') or s.get('answer')}'"
        return f"{nid} not signed off: {item['why']}"
    e = item.get("evidence") or {}
    c = next((x for x in doc.criteria if x.id == nid), None)
    base = baseline.get(nid) or {}
    based = f"baseline {_short(base['state'])}" if base.get("state") else "no baseline"
    command = str(getattr(c, "test", "") or "").strip().strip("`").strip()
    if e.get("values"):
        values = ", ".join(f"{x:g}" for x in e["values"])
        median = f" (median {e['value']:g})" if e.get("value") is not None else ""
        bv = base.get("value")
        shown = f"baseline {bv:g}{_delta(e.get('value'), bv)}" if bv is not None else based
        return (f"{nid} {_short(e.get('state'))} — measured {values}{median} "
                f"{getattr(c, 'threshold', '')}; {shown} (`{command}`)")
    secs = e.get("seconds")
    took = f", {secs:g} s" if isinstance(secs, (int, float)) else ""
    return (f"{nid} {_short(e.get('state'))} — `{command or 'no test bound'}` "
            f"(finish run{took}; {based})"
            + (f": {_clip(e.get('reason'), 120)}" if item["status"] != "met" else ""))


def render(v: Mapping[str, Any], doc: Optional[doc_goals.Goals] = None) -> str:
    """The outcome report (R56): what was proved, how, what is unmet and next, what waits
    on Dan, the limits, and how to reproduce it."""
    items = {i["id"]: i for i in v["items"]}
    baseline = v.get("baseline") or {}
    unmet_goals = [i for i in v["items"] if i["kind"] == "goal" and i["status"] != "met"]
    goals_n = sum(1 for i in v["items"] if i["kind"] == "goal")
    if v["verdict"] == "complete":
        summary = "every mandatory criterion, operator check, scenario, goal and invariant met"
    else:
        summary = f"{len(unmet_goals)} of {goals_n} goals unmet"
        if v["dan"]:
            summary += f"; waiting on you for {', '.join(d['id'] for d in v['dan'][:5])}"
    lines = [f"# Outcome — {v.get('title') or 'project'}  (contract revision {v['revision']}, "
             f"commit {str(v['commit'])[:12]}, {v.get('at')})",
             f"Verdict: {v['verdict']} — {summary}.", "", "## Goals → scenarios → evidence"]
    shown: set[str] = set()
    for g in [i for i in v["items"] if i["kind"] == "goal"]:
        lines.append(f"### {g['id']} {g.get('name', '')} — {g['status']}"
                     + (f"  (target: {g.get('target')})" if g.get("target") else ""))
        verifiers = set(doc.verifiers(g["id"])) if doc is not None else set()
        scenarios = [s for s in v["items"] if s["kind"] == "scenario"
                     and doc is not None and g["id"] in next(
                         (x.goals for x in doc.scenarios if x.id == s["id"]), ())]
        for s in scenarios:
            lines.append(f"- {s['id']} {s.get('name', '')} — {s['status']}"
                         + ("" if s["status"] == "met" else f": {s['why']}"))
            crits, ops = doc_goals.scenario_entries(doc, next(
                x for x in doc.scenarios if x.id == s["id"]))
            for e in [*crits, *ops]:
                if e.id in items and e.id in verifiers:
                    lines.append(f"  - {_evidence_line(items[e.id], doc, baseline)}")
                    shown.add(e.id)
        for nid in sorted(verifiers - shown):
            if nid in items:
                lines.append(f"- {_evidence_line(items[nid], doc, baseline)}")
                shown.add(nid)
    lines += ["", "## Hard invariants"]
    invs = [i for i in v["items"] if i["kind"] == "invariant"]
    for i in invs:
        checks = ", ".join(f"{c} {_short(items[c].get('state'))}" for c in i.get("checks") or ())
        lines.append(f"- {i['id']} held: {checks} (negative check)" if i["status"] == "met"
                     else f"- {i['id']} {i['status']}: {i['why']}")
    if not invs:
        lines.append("- none in the contract")
    lines += ["", "## Accepted trade-offs"]
    lines += [f"- {t}" for t in v["accepted_trade_offs"]] or ["- none"]
    if v["verdict"] != "complete":
        lines += ["", "## Unmet and next"]
        props = v.get("proposals") or ()
        n = 0
        for i in [*v["work"], *v["dan"]]:
            item = items.get(i["id"]) or {}
            n += 1
            nxt = "; ".join(
                f"{p['id']} {p.get('status')}"
                + (f": {', '.join(p.get('applied') or ())}" if p.get("applied") else "")
                + (f" (round {p['round']} of {v['cap']})" if p.get("round") else "")
                + (f", {p['question_id']}" if p.get("question_id") else "")
                for p in props if p.get("goal") and p.get("goal") == i.get("goal"))
            state = item.get("state")
            head = f"{i['id']} {_short(state)}" if state else i["id"]
            lines.append(f"{n}. {head}: {_clip(i['why'], 300)}" + (f" → {nxt}" if nxt else ""))
    lines += ["", "## Waiting on Dan"]
    lines += [f"- {d['id']}: {d['why']}" for d in v["dan"]] or ["- nothing"]
    lines += ["", "## Limitations"]
    lines += [f"- {t}" for t in v["limitations"]] or ["- none"]
    lines += ["", "## Deferred (nonblocking)"]
    lines += [f"- {d['id']} {d.get('state') or ''}: {d['why']}" for d in v["deferred"]] or \
        ["- nothing"]
    lines += ["", "## Reproduce", "- maestro goals baseline"]
    if doc is not None:
        lines += [f"- {c.id}: {c.test.strip().strip('`').strip()}" for c in targets(doc)
                  if c.test.strip()]
    lines += ["- maestro goals finish", "", "## Evidence",
              f"- goals store finish row {v['id']} (.orchestrator/goal_decisions.json); "
              f"{acceptance.BASELINE}"]
    questions = sorted(set(re.findall(r"dq-[\w-]+", json.dumps(v["items"]))))
    if questions:
        lines.append(f"- questions: {', '.join(questions)}")
    return "\n".join(lines) + "\n"


def _write(repo: Path, rel: str, text: str) -> None:
    path = Path(repo) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_report(repo: Path, v: Mapping[str, Any], doc: doc_goals.Goals) -> None:
    _write(repo, REPORT_MD, render(v, doc))
    _write(repo, REPORT_JSON, json.dumps(v, indent=2, default=str) + "\n")


# --- questions ----------------------------------------------------------------------------


def _op_question(op: doc_goals.OperatorCheck, doc: doc_goals.Goals, fid: str,
                 commit: str) -> dict:
    goals = ", ".join(doc_goals.entry_goals(doc, op)) or "no goal"
    rev = doc.revision
    return {
        "id": f"{OP_PREFIX}{op.id}-{fid[4:]}", "task_id": "finish", "run_id": f"finish-{fid}",
        "wait_key": f"finish-{fid}-{op.id}", "title": f"Operator check {op.id}",
        "spec": {
            "question": (f"{op.id} {op.name} ({goals}). Do this: {op.instructions} Expected: "
                         f"{op.expected} Contract revision {rev}, commit {commit[:12]}; every "
                         "automated check passed."),
            "why_undefined": ("Only you can observe this; an operator check is never an "
                              "automated pass. Pass closes it for this revision and commit. "
                              "Fail, or a reply saying what you saw, adds a fix task."),
            "options": [{"label": "Pass",
                         "consequence": f"{op.id} is verified at revision {rev}, commit "
                                        f"{commit[:12]}"},
                        {"label": "Fail", "consequence": f"a fix task is added for {goals}"}],
            "recommended": None,
            "operator_check": {"id": op.id, "revision": rev, "commit": commit, "finish": fid,
                               "goals": doc_goals.entry_goals(doc, op),
                               "expected": op.expected},
        },
    }


def _conflict_question(q: Mapping[str, Any], fid: str) -> dict:
    gc = str((q.get("conflict") or {}).get("id") or "GC-?")
    return {"id": f"{QUESTION_PREFIX}{gc}-{fid[4:]}", "task_id": "finish",
            "run_id": f"finish-{fid}", "wait_key": f"finish-{fid}-{gc}",
            "title": "Goal conflict at the completion gate", "spec": dict(q)}


def _records() -> list[dict]:
    from maestro.hitl import verify

    return verify.question_records(prefix=QUESTION_PREFIX)


def signoffs(store: Mapping[str, Any], doc: doc_goals.Goals, commit: str) -> dict[str, dict]:
    """Dan's operator-check sign-offs valid now: the same revision and commit (R67); the
    latest per check."""
    out: dict[str, dict] = {}
    for r in store.get("operator") or ():
        if r.get("revision") == doc.revision and r.get("commit") == commit and r.get("id"):
            out[str(r["id"])] = dict(r)
    return out


def _signoff(check: Mapping[str, Any], answer: str, at: str, qid: str) -> dict:
    text = str(answer or "").strip()
    if text.lower() == "pass":
        result, observation = "pass", ""
    elif text.lower() == "fail":
        result, observation = "fail", ""
    else:
        result, observation = "fail", text
    return {"id": check.get("id"), "revision": check.get("revision"),
            "commit": check.get("commit"), "finish": check.get("finish"), "result": result,
            "observation": observation, "answer": text, "at": at, "question_id": qid}


def poll(repo: Path, channel, at: str, *,
         records: Optional[Callable[[], Iterable[Mapping]]] = None,
         journal: Optional[Callable[[str, str], Any]] = None) -> Optional[str]:
    """Each poll: Dan's answers to the completion gate's questions (an operator check's
    Pass, Fail or his own words; a finish conflict's decision through GDT-7's
    `decide`/`record`), also an answer to an older run's question not yet recorded (HEAD
    moved after it was asked: a sign-off counts only at its own commit, a decision is
    kept, review L9), read through the channel's `question_answer` without asking it
    again (follow-up L-b). A sign-off or decision the store lost is restored from the question
    record; one stored but never marked recorded (a crash in between) is marked now
    (review M1). Returns the current FIN id (`None` without a format-2 contract), so the
    caller can drop the waits of older runs."""
    from maestro import goal_conflicts, goal_state
    from maestro.hitl import verify

    doc = doc_goals.load(repo)
    if doc is None or doc.format != 2 or channel is None:
        return None
    fid = finish_id(doc.revision, goal_state.head(repo))
    journal = journal or (lambda event, detail: None)
    store = goal_store.read(repo)
    stored = {str(r.get("question_id")): r for r in store["operator"]}
    decided = {str(d.get("id")): d for d in store["decisions"]}
    def answer_of(record: Mapping, older: bool) -> Optional[str]:
        # An older run's question is only read: polling it would re-send it and put back
        # the wait `prune_runless_waits` dropped, on every poll (follow-up L-b).
        read = getattr(channel, "question_answer", None)
        if older and read is not None:
            return read(str(record.get("id")))
        return channel.poll_question(record)

    for record in (records or _records)():
        rid = str(record.get("id") or "")
        spec = record.get("spec") if isinstance(record.get("spec"), Mapping) else {}
        older = not rid.endswith(fid[4:])
        if not rid.startswith(QUESTION_PREFIX) or (older and record.get("recorded")):
            continue
        try:
            if isinstance(spec.get("operator_check"), Mapping):
                if rid in stored:
                    if not record.get("recorded"):
                        channel.mark_question_recorded(rid, op_signoff=dict(stored[rid]))
                    continue
                kept = record.get("op_signoff")
                if record.get("recorded") and isinstance(kept, Mapping):
                    goal_store.update(repo, "operator", lambda rows: [*rows, dict(kept)])
                    continue
                answer = answer_of(record, older)
                if answer is None:
                    continue
                row = _signoff(spec["operator_check"], answer,
                               verify._answered_at(rid) or at, rid)
                goal_store.update(repo, "operator", lambda rows: [*rows, row])
                channel.mark_question_recorded(rid, op_signoff=row)
                journal("goal_finish_signoff", f"{row['id']} {row['result']} at {fid} "
                                               f"({rid})" + (f": {row['observation'][:120]}"
                                                             if row["observation"] else ""))
            elif isinstance(spec.get("conflict"), Mapping):
                gc = str(spec["conflict"].get("id") or "")
                kept = record.get("goal_decision")
                if record.get("recorded"):
                    if isinstance(kept, Mapping) and gc not in decided:
                        goal_conflicts.record(repo, kept)
                    continue
                answer = answer_of(record, older)
                if answer is None:
                    continue
                decision = goal_conflicts.decide(spec, answer, verify._answered_at(rid) or at)
                # Replaces any decision with its id: a crash before the mark below re-records
                # the same decision on the next poll, never a second one.
                goal_conflicts.record(repo, decision)
                channel.mark_question_recorded(rid, goal_decision=decision)
                journal("goal_finish_decided", f"{gc} {decision['resolution']} ({rid})")
        except Exception as exc:  # noqa: BLE001 — one bad record never stops the others
            journal("goal_finish_failed", f"poll {rid}: {type(exc).__name__}: {exc}"[:300])
    return fid


# --- the corrective loop (R55) -----------------------------------------------------------


def rounds(replans: Iterable[Mapping[str, Any]], doc: doc_goals.Goals) -> dict[str, int]:
    """Per goal, the `finish_gap` replans at its current terms that added work: applied,
    or decided with Dan's approval. A contract revision of the goal's terms resets it."""
    from maestro import goal_replan

    term = goal_replan.terms(doc)
    out = {g: 0 for g in doc.ids}
    for r in replans:
        goal = r.get("goal")
        if (r.get("trigger") or {}).get("kind") != "finish_gap" or goal not in out \
                or r.get("terms") != term.get(goal):
            continue
        if r.get("status") == "applied" or (
                r.get("status") == "decided"
                and (r.get("decision") or {}).get("resolution") == "approve"):
            out[goal] += 1
    return out


def _corrective_tasks(repo: Path, doc: doc_goals.Goals, goal: str, mine: Sequence[dict],
                      results: Sequence[acceptance.Result], tasks: Sequence[Mapping],
                      rp: str, done: int, cap: int, commit: str,
                      replans: Sequence[Mapping]) -> list[dict]:
    from maestro import goal_replan, goal_roadmap

    sfx = rp[3:].lower()
    by_id = {c.id: c for c in doc.criteria}
    term = goal_replan.terms(doc).get(goal)
    earlier = [t for r in replans if r.get("goal") == goal and r.get("terms") == term
               and (r.get("trigger") or {}).get("kind") == "finish_gap"
               for t in r.get("applied") or ()]
    diagnosis = [t for r in replans if r.get("goal") == goal
                 for t in r.get("applied") or () if "-diagnose-" in str(t)]

    def notes(nid: str, item: Mapping[str, Any]) -> str:
        text = (f"Completion gate round {done + 1} of {cap} at contract revision "
                f"{doc.revision}, commit {commit[:12]}: {nid} {_short(item.get('state'))} — "
                f"{_clip(item.get('why'), 300)}.")
        if item.get("state") in (acceptance.BLOCKED, acceptance.UNKNOWN):
            text += (" Its test cannot give a credible result yet: make it runnable with safe "
                     "fixtures inside the project, never by changing its oracle or threshold.")
        if earlier:
            text += f" Earlier rounds added {', '.join(earlier)} and it is still unmet."
        if diagnosis:
            text += (" Read the written cause in "
                     f"{', '.join(goal_replan.cause_file(t) for t in diagnosis)} first.")
        return text

    out: list[dict] = []
    acs = {i["id"]: i for i in mine if i["kind"] == "criterion"}
    if acs:
        open_ = [t for t in tasks if t.get("status") != "complete"]
        try:
            proposed = goal_roadmap.propose_tasks(doc, results, open_)
        except ValueError:
            proposed = []
        for t in proposed:
            named = [c for c in t.get("criteria") or () if c in acs]
            if t.get("contribution") != "direct" or not named:
                continue
            cid = named[0]
            out.append({**t, "id": f"{t['id']}-{sfx}", "status": "open", "mode": "autonomous",
                        "evidence": f"`maestro goals finish` reruns {cid} {acceptance.GREEN} "
                                    f"at contract revision {doc.revision}",
                        "deps": [], "replan_of": rp,
                        "verifications": goal_roadmap.checks([by_id[cid]], repo),
                        "notes": notes(cid, acs[cid])})
    for item in mine:
        if item["kind"] != "operator_check":
            continue
        op = next(o for o in doc.operator_checks if o.id == item["id"])
        s = item.get("signoff") or {}
        seen = s.get("observation") or s.get("answer") or "Fail"
        guard = [c for c in doc.criteria if c.mandatory and goal in _goals(doc, c)] or \
            [c for c in doc.criteria if c.mandatory]
        fix = {
            "id": f"{op.id.lower()}-fix-{sfx}", "status": "open", "mode": "autonomous",
            "title": f"{op.id}: fix what Dan saw",
            "short_desc": (f"Dan's operator check failed at {commit[:12]}: '{seen}'. Expected: "
                           f"{op.expected} Change the product, never the check."),
            "serves": doc_goals.entry_goals(doc, op) or [goal], "contribution": "direct",
            "criteria": [op.id, *op.scenarios], "tests": [],
            "evidence": f"Dan passes {op.id} at the completion gate", "deps": [],
            "replan_of": rp, "notes": notes(op.id, item)}
        # With no bound mandatory criterion to guard the fix, Dan confirms it (an empty
        # list is refused by INV-04, review L2).
        fix["verifications"] = goal_roadmap.checks(guard, repo) or \
            [goal_roadmap.dan_check(fix)]
        out.append(fix)
    return out


def _summary(r: Mapping[str, Any], *, new: bool = False) -> dict:
    trigger = r.get("trigger") or {}
    return {"id": r.get("id"), "goal": r.get("goal"), "status": r.get("status"),
            "applied": list(r.get("applied") or ()), "question_id": r.get("question_id"),
            "resolution": (r.get("decision") or {}).get("resolution"),
            "round": (int(trigger.get("rounds") or 0) + 1) if r.get("status") == "applied"
            else None, "at_cap": bool(trigger.get("at_cap")), "new": new}


def _correct(repo: Path, doc: doc_goals.Goals, v: Mapping[str, Any], row: Mapping[str, Any],
             tasks: Sequence[Mapping], store: Mapping[str, Any], cap: int,
             done: Mapping[str, int], at: str, channel, journal) -> list[dict]:
    """One `finish_gap` replan per goal with work, in priority order: applied when routine,
    asked when the contract reserves it or the goal is at the cap (once per facts)."""
    from maestro import goal_replan

    fid, commit = str(v["id"]), str(v["commit"])
    by_goal: dict[str, list[dict]] = {}
    for i in v["items"]:
        if i["kind"] in ("criterion", "operator_check") and i.get("goal") and (
                (i["status"] == "work" and not i.get("named")) or i.get("capped")):
            by_goal.setdefault(i["goal"], []).append(i)
    known = {str(r.get("id")): r for r in store.get("replans") or ()}
    results = [acceptance.Result(id=cid, state=str(e.get("state")),
                                 reason=str(e.get("reason") or ""), revision=doc.revision,
                                 value=e.get("value"))
               for cid, e in (row.get("results") or {}).items()]
    out: list[dict] = []
    for goal in _ranked(doc):
        mine = by_goal.get(goal)
        if not mine:
            continue
        r = int(done.get(goal, 0))
        facts = [fid, *sorted(f"{i['id']}={i.get('state')}" for i in mine)]
        evidence = [f"{i['id']} {_short(i.get('state'))} on the finish run at {commit[:12]}: "
                    f"{_clip(i['why'], 200)}" for i in mine]
        signal = goal_replan._signal(goal, "finish_gap", facts, [], evidence,
                                     [i["id"] for i in mine], rounds=r, cap=cap,
                                     at_cap=r >= cap, commit=commit, finish=fid)
        signal["facts"] = facts
        rp = "RP-" + signal["fingerprint"][:8]
        if rp in known:
            out.append(_summary(known[rp]))
            continue
        add = _corrective_tasks(repo, doc, goal, mine, results, tasks, rp, r, cap, commit,
                                list(store.get("replans") or ()))
        if not add:
            continue
        signal["tasks"] = add
        proposal = goal_replan.propose(signal, doc, tasks, task="finish", results=results,
                                       at=at, repo=repo)
        best = proposal["alternatives"][proposal["recommended"]]
        if not best["requires_dan"]:
            added = list(best["changes"]["add_tasks"])
            proposal.update(status="applied", settled_at=at, applied=[t["id"] for t in added],
                            waits=list(best["changes"]["deps_rewire"]))
            goal_replan._store_replan(repo, proposal)
            journal("goal_finish_round", f"{rp} {goal}: round {r + 1} of {cap} "
                                         f"({', '.join(proposal['applied'])}) at {fid}")
        else:
            spec = goal_replan.question(proposal)
            proposal.update(status="asked", question_id=spec["id"])
            goal_replan._store_replan(repo, proposal)
            journal("goal_finish_asked", f"{spec['id']} {goal}: {best['label']} at {fid}"
                                         + (" (cap reached)" if r >= cap else ""))
            if channel is not None:
                channel.poll_question(spec)
        out.append(_summary(proposal, new=True))
    return out


# --- evaluate ----------------------------------------------------------------------------


def _step(kind: str, v: Optional[dict], text: str) -> dict:
    return {"kind": kind, "verdict": v, "text": text}


def _completed(repo: Path) -> set[str]:
    try:
        data = json.loads((Path(repo) / ".orchestrator" / "completed_tasks.json").read_text(
            encoding="utf-8"))
        return {str(item["id"]) for item in data}
    except Exception:  # noqa: BLE001 — none recorded
        return set()


def _landing(repo: Path, doc: doc_goals.Goals, store: Mapping[str, Any],
             tasks: Sequence[Mapping], parked: set[str],
             completed: Optional[Iterable[str]] = None) -> list[str]:
    """Corrective work still on its way: a settled replan's tasks not yet in the ROADMAP,
    or a finish replan's task open there that the loop can run (its own predicate,
    `_why_not`, review H2). The finish waits for them instead of rerunning on a tree they
    are about to change; one the loop cannot run is named by the verdict instead."""
    from maestro import goal_replan, taskgraph

    done = _completed(repo) if completed is None else {str(i) for i in completed}
    present = {str(t.get("id")) for t in tasks} | done
    replans = list(store.get("replans") or ())
    found = [str(t.get("id")) for t in goal_replan.unlanded(replans, present)]
    if found:
        return found
    added = {str(i) for r in replans if (r.get("trigger") or {}).get("kind") == "finish_gap"
             for i in r.get("applied") or ()}
    held = {**{tid: "" for tid in doc_goals.execution_holds(doc, tasks)},
            **goal_replan.held(repo, tasks)}
    why = _why_not(tasks, held, parked, taskgraph.blocked_task_ids(repo), done)
    return [str(t["id"]) for t in tasks if str(t.get("id")) in added
            and t.get("status") != "complete" and not why(t)]


def _judge(repo: Path, doc: doc_goals.Goals, row: Mapping[str, Any], baseline, decisions):
    """GDT-7's judgement at the finish stage: a protected criterion RED on the finish run
    while another goal advanced (a criterion not GREEN at the baseline is GREEN now)."""
    from maestro import goal_conflicts, goal_state

    results = row.get("results") or {}
    protected = goal_state.protected(repo, doc)
    regressions, advanced = [], {}
    for c in doc.criteria:
        e = results.get(c.id)
        if e is None:
            continue
        base = baseline.get(c.id)
        for g in doc_goals.entry_goals(doc, c):
            if c.id in protected and e.get("state") == acceptance.RED:
                regressions.append(goal_conflicts.Regression(
                    goal=g, criterion=c.id, value=e.get("value"),
                    baseline=base.value if base else None, evidence=str(e.get("reason") or ""),
                    state=acceptance.RED))
            if e.get("state") == acceptance.GREEN and (base is None
                                                       or base.state != acceptance.GREEN):
                advanced.setdefault(g, []).append(c.id)
    if not regressions:
        return None
    ctx = goal_conflicts.context(doc, "", decisions)
    return goal_conflicts.judge(regressions, advanced, ctx, stage="finish",
                                place="the completion gate")


def _trusted(repo: Path, doc: doc_goals.Goals, row: Mapping[str, Any],
             tasks: Sequence[Mapping[str, Any]], completed: Iterable[str]
             ) -> tuple[dict[str, str], list[str]]:
    """`trust`, cached on the FIN row while (the baseline's commit, the ROADMAP's tasks,
    the graduated ids) hold: the sweep reads git history and parses ROADMAP revisions, and a
    held state is polled every 30 s (review M3)."""
    from maestro import goal_state

    key = _hash(str(goal_state._baseline_meta(repo).get("commit") or ""),
                row.get("commit"), list(tasks), sorted(_graduated(tasks, completed)))
    cached = row.get("trust") or {}
    if cached.get("key") == key:
        return dict(cached.get("untrusted") or {}), list(cached.get("limitations") or ())
    untrusted, limitations = trust(repo, doc, tasks, completed)
    _patch(repo, str(row.get("id")), trust={"key": key, "untrusted": untrusted,
                                            "limitations": limitations})
    return untrusted, limitations


def evaluate(repo: Path, *, channel=None, tasks: Optional[Sequence[Mapping]] = None,
             parked: Optional[Iterable[str]] = None, at: Optional[str] = None, runner=None,
             timeout: int = acceptance.DEFAULT_TIMEOUT, budget: Optional[float] = None,
             clock: Optional[Callable[[], float]] = None, journal=None, notify=None,
             rounds_cap: Optional[int] = None, act: bool = True,
             records: Optional[Callable[[], Iterable[Mapping]]] = None,
             completed: Optional[Iterable[str]] = None) -> dict:
    """The completion gate at queue exhaustion (and `maestro goals finish`). Returns a step
    `{kind, verdict, text}`: `none` (no format-2 contract: the caller exhausts as before;
    format 1 says it is not verified), `verifying` (the run continues next poll),
    `working` (corrective work is landing or was added), `asking`/`stopped` (blocked),
    or `complete`. With `act` (the graph loop) it asks operator checks and finish
    conflicts and runs the corrective rounds; without it (legacy, CLI) it only judges.
    Without `notify` (the CLI) Dan is told nothing and nothing is recorded as told, so
    the loop's own evaluation still tells him (review M4). An internal failure is
    `blocked`, never `complete`."""
    repo = Path(repo)
    journal = journal or (lambda event, detail: None)
    doc = doc_goals.load(repo)
    if doc is None:
        return _step("none", None, "")
    if doc.format != 2:
        return _step("none", None, f"goals not verified: {doc_goals.DOC} is format "
                                   f"{doc.format}")
    at = at or _now()
    try:
        return _evaluate(repo, doc, channel=channel, tasks=tasks, parked=parked, at=at,
                         runner=runner, timeout=timeout, budget=budget, clock=clock,
                         journal=journal, notify=notify, rounds_cap=rounds_cap, act=act,
                         records=records, completed=completed)
    except Exception as exc:  # noqa: BLE001 — never complete on an internal failure
        why = _clip(f"the completion gate failed: {type(exc).__name__}: {exc}", 400)
        journal("goal_finish_failed", why)
        v = {"id": "", "verdict": "blocked", "revision": doc.revision, "commit": "", "at": at,
             "format": doc.format, "title": doc.title, "items": [], "work": [],
             "dan": [{"id": "completion gate", "why": why, "goal": ""}], "proposals": [],
             "accepted_trade_offs": [], "limitations": [], "deferred": [], "rounds": {},
             "cap": DEFAULT_ROUNDS, "report": REPORT_MD, "fingerprint": _hash(why)}
        text = (f"⏸ Not done — blocked at revision {doc.revision}. Waiting on you: {why}. "
                "Nothing else can run.")
        frames = traceback.extract_tb(exc.__traceback__)
        key = (type(exc).__name__,
               f"{Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else "")
        if notify is not None and key not in _FAILED_TOLD:
            _FAILED_TOLD.add(key)
            try:
                notify(text)
            except Exception:  # noqa: BLE001
                pass
        return _step("stopped", v, text)


def _evaluate(repo: Path, doc: doc_goals.Goals, *, channel, tasks, parked, at, runner, timeout,
              budget, clock, journal, notify, rounds_cap, act, records, completed) -> dict:
    from maestro import goal_conflicts, goal_milestones, goal_state, taskgraph
    from maestro.workflows.dan_questions import MAX_QUESTIONS_PER_ROUND

    commit = goal_state.head(repo)
    fid = finish_id(doc.revision, commit)
    tasks = goal_state._roadmap_tasks(repo) if tasks is None else list(tasks)
    parked = goal_state._parked(repo) if parked is None else set(parked)
    completed = _completed(repo) if completed is None else {str(i) for i in completed}
    store = goal_store.read(repo)
    if act:
        landing = _landing(repo, doc, store, tasks, parked, completed)
        if landing:
            return _step("working", None, f"corrective tasks still to run before the "
                                          f"completion gate reruns: {', '.join(landing)}")
    row = run(repo, doc, commit=commit, at=at, runner=runner, timeout=timeout,
              budget=goal_milestones.budget_for() if budget is None else budget, clock=clock,
              heartbeat=lambda text: journal("goal_finish_running", text), journal=journal)
    if not row.get("done"):
        return _step("verifying", None, f"{fid}: {len(row.get('results') or {})} of "
                                        f"{len(targets(doc))} criteria verified")
    store = goal_store.read(repo)
    baseline = acceptance.current(acceptance.load(repo), doc)
    shown = goal_state.progress(repo, doc, tasks, parked) or {}
    untrusted, limitations = _trusted(repo, doc, row, tasks, completed)
    recs = list((records or _records)())
    answered = [dict(r["goal_decision"]) for r in recs
                if r.get("recorded") and isinstance(r.get("goal_decision"), Mapping)]
    decisions = goal_conflicts.merge(store.get("decisions") or (), answered)[0]
    judgement = _judge(repo, doc, row, baseline, decisions)
    cap = rounds_cap if rounds_cap is not None else round_cap(doc, repo)
    done = rounds(store.get("replans") or (), doc)
    signed = signoffs(store, doc, commit)
    asking = bool(act and channel is not None)
    common = dict(backlog=_backlog.items(repo, "open", "triage", "assigned", "deferred",
                                         "merged"),
                  signoffs=signed, judgement=judgement, tasks=tasks, parked=parked,
                  untrusted=untrusted, limitations=limitations, dirty=dirty(repo),
                  rounds=done, cap=cap, baseline=baseline, act=act, asking=asking, at=at,
                  completed=completed, drift=taskgraph.blocked_task_ids(repo))
    first = verdict(doc, row, shown, **common)
    asked: dict[str, str] = {}
    proposals: list[dict] = []
    if act:
        seen = {str(r.get("id")) for r in recs}
        # A question already recorded is not polled again: that would put back a wait
        # that no poll clears at this FIN (review L8).
        recorded = {str(r.get("id")) for r in recs if r.get("recorded")}
        if channel is not None and judgement is not None:
            for q in judgement.questions:
                spec = _conflict_question(q, fid)
                if spec["id"] not in recorded:
                    channel.poll_question(spec)
                if spec["id"] not in seen:
                    journal("goal_finish_asked", f"{spec['id']} {q['conflict'].get('regressed')}"
                                                 f" at {fid}")
                for cid in q["conflict"].get("criteria") or ():
                    asked[str(cid)] = f"asked: {spec['id']}"
        others = [i for i in first["items"] if not i.get("derived")
                  and i["kind"] != "operator_check"]
        if channel is not None and all(i["status"] == "met" for i in others):
            unsigned = [o for o in doc.operator_checks if o.mandatory and o.id not in signed]
            for op in unsigned[:MAX_QUESTIONS_PER_ROUND]:
                spec = _op_question(op, doc, fid, commit)
                if spec["id"] not in recorded:
                    channel.poll_question(spec)
                if spec["id"] not in seen:
                    journal("goal_finish_asked", f"{spec['id']} at {fid}")
                asked[op.id] = f"your sign-off was asked: {spec['id']}"
        proposals = _correct(repo, doc, first, row, tasks, store, cap, done, at, channel,
                             journal)
        # The report's "Unmet and next" names every round at the goal's current terms.
        from maestro import goal_replan

        term = goal_replan.terms(doc)
        now = {p["id"] for p in proposals}
        proposals = [_summary(r) for r in store.get("replans") or ()
                     if (r.get("trigger") or {}).get("kind") == "finish_gap"
                     and r.get("terms") == term.get(r.get("goal")) and r.get("id") not in now
                     ] + proposals
        for p in proposals:
            if not p.get("at_cap"):
                continue
            text = (f"asked: {p['question_id']}" if p.get("status") == "asked" else
                    f"Dan answered {p.get('question_id')}: {p.get('resolution')}")
            for i in first["items"]:
                if i.get("capped") == p.get("goal"):
                    asked[i["id"]] = text
    v = verdict(doc, row, shown, asked=asked, proposals=proposals, **common)
    # A held state is polled every 30 s: the report and the journal line change only with
    # what they say (review M3), its limitations included: the loop rewrites a "verdict
    # only" report `maestro goals finish` wrote at the same run (follow-up L-a).
    shown_key = _hash(v["fingerprint"], v["proposals"], v["limitations"])
    kept = list((_row(repo, fid) or {}).get("verdicts") or ())
    new = not kept or (kept[-1] or {}).get("key") != shown_key
    if new or not (repo / REPORT_MD).exists():
        write_report(repo, v, doc)
    if new:
        journal("goal_finish_verdict", f"{v['verdict']} {fid} revision {doc.revision} "
                                       f"({commit[:12]}): {len(v['work'])} work, "
                                       f"{len(v['dan'])} waiting on Dan")
        _patch(repo, fid, verdicts=[*kept, {"key": shown_key, "verdict": v["verdict"],
                                             "fingerprint": v["fingerprint"], "at": at}
                                    ][-_KEEP_VERDICTS:])
    text = message(v, doc)
    if notify is not None:
        _tell(repo, v, text, notify, journal)
    if v["verdict"] == "complete":
        kind = "complete"
    elif v["verdict"] == "incomplete":
        kind = "working"
    else:
        kind = "asking" if asked else "stopped"
    return _step(kind, v, text)
