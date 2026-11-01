"""Milestone checkpoints: the goal tests rerun where the plan's route can be judged (P13 GDT-8).

Not after every graduation (`01` §7, R43): only at a meaningful milestone, found when a task
graduates (`milestones`):

- `phase_complete`: the task's `group_id`/`group` has no other open task;
- `enabling_unblocked`: an enabling task for G that an open task serving G depends on;
- `risky_integration`: a `risk: high`/`critical` task that serves a goal;
- `strategic_pivot`: the contract revision changed since the last checkpoint, or a replan
  was applied or decided since;
- `convergence`: no open task serves G any more.

`checkpoint` reruns only the touched goals' mandatory criteria that have a `Test:` (R65),
with `acceptance.run_criterion` on the merged tree, and never writes the acceptance
baseline; each criterion is journaled before it runs and none starts once
`CHECKPOINT_BUDGET` is spent (the rest are "not rerun", `skipped`). It records each state
beside the state before (the last checkpoint that judged the goal, else the baseline's)
and the feasibility problems (`goal_roadmap.feasibility`) this graduation
opened: a criterion the plan named only in the graduated task. A criterion that stopped
giving a credible result (`blocked_environment`/`unknown`, and not already so before) is a
targeted follow-up in the graduation commit (R42). `goal_replan.triggers` reads the record.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from maestro import acceptance, goal_roadmap
from maestro.docs import goals as doc_goals

MILESTONES = ("phase_complete", "enabling_unblocked", "risky_integration", "strategic_pivot",
              "convergence")
#: Seconds a checkpoint may spend starting criteria (GDT-8 review H1). With one criterion's
#: `acceptance.DEFAULT_TIMEOUT` (300 s) on top, a checkpoint stays well inside the
#: watchdog's 20-minute stall window even before its per-criterion heartbeats.
CHECKPOINT_BUDGET = 600
_RISKY = ("high", "critical")
_CANNOT_RUN = (acceptance.BLOCKED, acceptance.UNKNOWN)


def _group(task: Mapping[str, Any]) -> str:
    group = task.get("group_id") or task.get("group")
    return group if isinstance(group, str) else ""


def _deps(task: Mapping[str, Any]) -> list[str]:
    deps = task.get("deps") or []
    return [str(d) for d in ([deps] if isinstance(deps, str) else deps)]


def _serves(task: Mapping[str, Any]) -> tuple[str, ...]:
    return doc_goals.task_serves(task) or ()


def _settled_at(replan: Mapping[str, Any]) -> str:
    return str(replan.get("settled_at") or "") if replan.get("status") in (
        "applied", "decided") else ""


def milestones(task: Mapping[str, Any], open_tasks: Sequence[Mapping[str, Any]],
               doc: doc_goals.Goals, store: Mapping[str, Any]) -> dict[str, list[str]]:
    """The milestones `task`'s graduation reaches, each with the goals it touches; `{}` for
    an ordinary graduation or a task that serves no goal."""
    served = [g for g in _serves(task) if g in doc.ids]
    if not served:
        return {}
    tid = str(task.get("id") or "")
    others = [t for t in open_tasks
              if str(t.get("id") or "") != tid and t.get("status") != "complete"]
    out: dict[str, list[str]] = {}
    group = _group(task)
    if group and not any(_group(t) == group for t in others):
        out["phase_complete"] = served
    kinds = doc_goals.task_contribution(task)
    unblocked = [g for g in served if kinds.get(g) == "enabling" and any(
        g in _serves(t) and tid in _deps(t) for t in others)]
    if unblocked:
        out["enabling_unblocked"] = unblocked
    if str(task.get("risk") or "").lower() in _RISKY:
        out["risky_integration"] = served
    checkpoints = list(store.get("checkpoints") or ())
    if checkpoints:
        last = checkpoints[-1]
        since = str(last.get("at") or "")
        if last.get("revision") != doc.revision or any(
                _settled_at(r) > since for r in store.get("replans") or ()):
            out["strategic_pivot"] = served
    converged = [g for g in served if not any(g in _serves(t) for t in others)]
    if converged:
        out["convergence"] = converged
    return out


def _id(*parts: Any) -> str:
    return "CP-" + hashlib.sha1(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:8]


def _feasibility(doc: doc_goals.Goals, task: Mapping[str, Any],
                 open_tasks: Sequence[Mapping[str, Any]], results: Sequence[acceptance.Result],
                 goals: Sequence[str]) -> dict[str, list[str]]:
    """Per touched goal, the feasibility problems this graduation opened (absent while the
    task was still open), so a plan gap known at setup is not raised at every checkpoint."""
    after = goal_roadmap.feasibility(doc, open_tasks, results)
    before = {p.text for p in goal_roadmap.feasibility(doc, [*open_tasks, task], results)}
    out: dict[str, list[str]] = {}
    for problem in after:
        if problem.text in before:
            continue
        for g in goals:
            if g in problem.ids:
                out.setdefault(g, []).append(problem.text)
    return out


def budget_for(stall_window_min: Optional[float] = None) -> float:
    """The checkpoint's time cap for the watchdog's stall window: `CHECKPOINT_BUDGET`, or
    less when the configured `stall_window_min` leaves no room for the cap, one criterion's
    timeout and a minute's margin; never negative."""
    if stall_window_min is None:
        from maestro import watchdog

        stall_window_min = watchdog.STALL_WINDOW_MIN
    room = stall_window_min * 60 - acceptance.DEFAULT_TIMEOUT - 60
    return float(max(0, min(CHECKPOINT_BUDGET, room)))


def _previous(checkpoints: Sequence[Mapping[str, Any]], goal: str) -> Mapping[str, str]:
    """The goal's states at the last checkpoint that judged it (`{}` without one)."""
    for point in reversed(checkpoints):
        states = (point.get("goals") or {}).get(goal)
        if isinstance(states, Mapping):
            return states
    return {}


def checkpoint(repo: Path, doc: doc_goals.Goals, found: Mapping[str, Sequence[str]],
               task: Mapping[str, Any], open_tasks: Sequence[Mapping[str, Any]], *,
               store: Mapping[str, Any], at: str, run_id: str = "", runner=subprocess.run,
               timeout: int = acceptance.DEFAULT_TIMEOUT,
               heartbeat: Optional[Callable[[str], None]] = None,
               clock: Callable[[], float] = time.monotonic,
               budget: Optional[float] = None) -> tuple[dict, list[str]]:
    """Rerun the touched goals' mandatory bound criteria: `(record, follow_ups)`.

    `found` is `milestones(...)`. Synchronous, inside a graduation that journals nothing
    else, so `heartbeat` is told before each criterion runs (the watchdog reads journal
    silence as a stall). Once `budget` seconds are spent no further criterion starts: the
    rest are "not rerun" (`skipped`, kept out of `goals` and `before`), each a targeted
    follow-up, never a verdict. `budget` defaults to `budget_for()` (the stall window). At
    most `budget` plus one criterion's `timeout`. The follow-ups are kept in the record, so a
    repair re-graduation reuses it instead of running again (`goal_replan.on_graduation`)."""
    tid = str(task.get("id") or "")
    if budget is None:
        budget = budget_for()
    touched =list(dict.fromkeys(g for goals in found.values() for g in goals))
    results = acceptance.load(repo)
    baseline = acceptance.current(results, doc)
    checkpoints = list(store.get("checkpoints") or ())
    bound = [c for c in doc.criteria if c.mandatory and c.test]
    order = list(dict.fromkeys(c.id for g in touched for c in bound
                               if g in doc_goals.entry_goals(doc, c)))
    ran: dict[str, acceptance.Result] = {}
    skipped: set[str] = set()
    states: dict[str, dict[str, str]] = {}
    not_rerun: dict[str, list[str]] = {}
    before: dict[str, dict[str, Optional[str]]] = {}
    unknown: list[dict] = []
    follow_ups: list[str] = []
    kinds = ", ".join(found)
    start = clock()
    for g in touched:
        previous = _previous(checkpoints, g)
        for c in bound:
            if g not in doc_goals.entry_goals(doc, c):
                continue
            if c.id not in ran:
                if clock() - start >= budget:
                    skipped.add(c.id)
                    ran[c.id] = acceptance.Result(
                        id=c.id, state=acceptance.UNKNOWN, revision=doc.revision,
                        reason=f"not rerun: the checkpoint's {budget:g} s cap was spent")
                else:
                    if heartbeat is not None:
                        heartbeat(f"{tid}: {c.id} ({order.index(c.id) + 1}/{len(order)})")
                    ran[c.id] = acceptance.run_criterion(repo, c, revision=doc.revision,
                                                         timeout=timeout, runner=runner)
            result = ran[c.id]
            was = previous.get(c.id) or (baseline[c.id].state if c.id in baseline else None)
            if c.id in skipped:
                not_rerun.setdefault(g, []).append(c.id)
            else:
                states.setdefault(g, {})[c.id] = result.state
                before.setdefault(g, {})[c.id] = was
            if result.state not in _CANNOT_RUN:
                continue
            if c.id not in [u["criterion"] for u in unknown]:
                unknown.append({"criterion": c.id, "goal": g, "state": result.state,
                                "reason": result.reason})
            if c.id in skipped:
                follow_ups.append(
                    f"Goal {g} checkpoint ({kinds} after {tid}): {c.id} was not rerun, the "
                    f"checkpoint's {budget:g} s cap was spent; run {c.id} ({c.test.strip()}) "
                    "and make the touched goals' tests finish within the cap, never its "
                    "oracle")
            elif was not in _CANNOT_RUN:
                follow_ups.append(
                    f"Goal {g} checkpoint ({kinds} after {tid}): {c.id} is {result.state} "
                    f"({result.reason}); make it give a credible result: run {c.id} "
                    f"({c.test.strip()}) and fix what stops it, never its oracle")
    feasibility: dict[str, list[str]] = {}
    if baseline:
        from maestro import goal_state

        # GDT-9: the plan is judged on the baseline overlaid by what was observed since (a
        # criterion an earlier task turned GREEN needs no task); `before`/`was` stay the
        # baseline's and the checkpoints', so the milestone gap counts GREEN changes truly.
        merged = [r for r in goal_state.results(repo, doc) if r.id not in ran
                  or r.id in skipped] + [r for cid, r in ran.items() if cid not in skipped]
        feasibility = _feasibility(doc, task, open_tasks, merged, touched)
    record = {"id": _id(tid, at, touched), "at": at, "task": tid, "run_id": run_id,
              "milestones": {k: list(v) for k, v in found.items()},
              "revision": doc.revision, "goals": states, "before": before,
              "skipped": not_rerun, "unknown": unknown, "feasibility": feasibility, "follow_ups": follow_ups}
    return record, follow_ups
