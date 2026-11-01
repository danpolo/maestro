"""Goal-driven ROADMAP planning (P13 GDT-4): the acceptance baseline turned into proposed
tasks, and the feasibility gate.

Setup runs `maestro goals baseline`, then `maestro goals tasks` prints a proposed task block
for every mandatory criterion that is not GREEN at the current revision. It never writes
the ROADMAP: setup merges the blocks into the plan's own tasks, keeping Dan's order. A
criterion whose test cannot run yet (`blocked_environment`, `unknown`) gets an `enabling`
harness task first and its `direct` task depends on it (`02` C11). A GREEN criterion needs
no task (it is regression protection), and an operator check is Dan's at the completion
gate.

The feasibility gate (`01` §4, R30) refuses a plan before autonomous execution when a
mandatory criterion that is not GREEN has no open task naming it in `criteria:` (serving its
goal is metadata, not a path: R69), or when one whose test cannot run has no open
`enabling` task. `maestro doctor` reports it as a required failure.
"""
from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import yaml

from maestro import acceptance
from maestro.docs import goals as doc_goals

_CANNOT_RUN = (acceptance.BLOCKED, acceptance.UNKNOWN)
_SLUG_CHARS = 40


def _no_baseline(doc: doc_goals.Goals) -> str:
    return (f"no acceptance baseline at contract revision {doc.revision} "
            "(run `maestro goals baseline`)")


def _slug(text: str) -> str:
    out = ""
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        longer = f"{out}-{word}" if out else word
        if len(longer) > _SLUG_CHARS:
            break
        out = longer
    return out


def _goals_of(doc: doc_goals.Goals, criterion: doc_goals.Criterion) -> list[str]:
    """The goals a criterion verifies, whichever side names the link, in contract order;
    its scenarios' goals only when neither side names one (a scenario spans goals)."""
    found = doc_goals.entry_goals(doc, criterion)
    if found:
        return found
    found = {g for s in doc.scenarios if s.id in criterion.scenarios for g in s.goals}
    return [g for g in doc.ids if g in found]


def _named(tasks: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    """The open tasks naming each criterion id in their `criteria:`."""
    out: dict[str, list[Mapping[str, Any]]] = {}
    for task in tasks:
        if task.get("status") == "complete":
            continue
        for cid in doc_goals.normalise_criteria(task.get("criteria"))[0]:
            out.setdefault(cid, []).append(task)
    return out


def _unmet(doc: doc_goals.Goals, results: Sequence[acceptance.Result]):
    """Every mandatory criterion that is not GREEN at the current revision, with its state
    and result; `ValueError` when there is no baseline at this revision."""
    current = acceptance.current(results, doc)
    if not current:
        raise ValueError(_no_baseline(doc))
    for criterion in doc.criteria:
        result = current.get(criterion.id)
        state = result.state if result else acceptance.UNKNOWN
        if criterion.mandatory and state != acceptance.GREEN:
            yield criterion, state, result


def propose_tasks(doc: doc_goals.Goals, results: Sequence[acceptance.Result],
                  tasks: Sequence[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    """ROADMAP task blocks for the unmet mandatory criteria no open task names yet."""
    named = _named(tasks)
    out: list[dict[str, Any]] = []
    for c, state, result in _unmet(doc, results):
        if c.id in named:
            continue
        serves = _goals_of(doc, c)
        test = c.test.strip().strip("`").strip()
        tests = [test] if test else []
        deps: list[str] = []
        if state in _CANNOT_RUN:
            reason = result.reason if result else "not in the baseline"
            harness = {
                "id": f"{c.id.lower()}-harness",
                "title": f"{c.id}: make its acceptance test runnable",
                "short_desc": (f"{c.id} cannot give a credible result yet ({state}: {reason}). "
                               "Bind or repair its test with safe fixtures inside the project "
                               "until it fails on the assertion or passes, never on the "
                               "harness; do not change its oracle."),
                "serves": serves, "contribution": "enabling", "criteria": [c.id],
                "tests": tests,
                "evidence": (f"`maestro goals baseline` reports {c.id} as "
                             f"{acceptance.RED} or {acceptance.GREEN}"),
                "deps": [],
            }
            out.append(harness)
            deps = [harness["id"]]
        out.append({
            "id": f"{c.id.lower()}-{_slug(c.name)}",
            "title": f"{c.id}: {c.name}",
            "short_desc": (f"Given {c.given}, when {c.when}, then {c.then}. "
                           f"Oracle: {c.oracle}." + (f" Threshold: {c.threshold}."
                                                    if c.threshold else "")),
            "serves": serves, "contribution": "direct", "criteria": [c.id, *c.scenarios],
            "tests": tests,
            "evidence": (f"`maestro goals baseline` reports {c.id} {acceptance.GREEN} at "
                         f"contract revision {doc.revision}"),
            "deps": deps,
        })
    return out


def _pytest(argv: Sequence[str]) -> bool:
    """Whether the command runs pytest, directly or behind a wrapper (`timeout 60 pytest`)."""
    names = [Path(a).name for a in argv]
    return any(n in ("pytest", "py.test") for n in names) or any(
        argv[i:i + 2] == ["-m", "pytest"] for i in range(len(argv)))


def checks(criteria: Sequence[doc_goals.Criterion], repo: Path) -> list[dict]:
    """The gate checks that a task turned each criterion GREEN, without bytecode or a
    pytest cache (the gate refuses an uncommitted tree): a test's bound command (pytest,
    also behind a wrapper, with `-p no:cacheprovider`); a metric's state against its
    threshold (`python -m maestro.acceptance AC`), never the measurement's exit status.
    A criterion with no runnable command adds none."""
    out = []
    for c in criteria:
        argv, _ = acceptance.command_for(c, repo)
        if not argv:
            continue
        if c.evidence == "metric":
            cmd = f"{shlex.quote(sys.executable)} -m maestro.acceptance {c.id}"
        else:
            argv = list(argv)
            if _pytest(argv):
                argv += ["-p", "no:cacheprovider"]
            cmd = shlex.join(argv)
        out.append({"id": f"V{len(out) + 1}", "kind": "auto",
                    "cmd": "env PYTHONDONTWRITEBYTECODE=1 " + cmd})
    return out


def credible(criteria: Sequence[doc_goals.Criterion], repo: Path) -> list[dict]:
    """The gate checks that each criterion's test gives a credible result (RED or GREEN,
    no longer BLOCKED or UNKNOWN): an enabling harness task's proof."""
    return [{"id": f"V{i}", "kind": "auto",
             "cmd": f"env PYTHONDONTWRITEBYTECODE=1 {shlex.quote(sys.executable)} -m "
                    f"maestro.acceptance {c.id} --credible"}
            for i, c in enumerate([c for c in criteria
                                   if acceptance.command_for(c, repo)[0]], 1)]


def dan_check(task: Mapping[str, Any]) -> dict:
    """Dan confirms the task's evidence (`kind: dan`): required, never executed, discharged
    by his approval of the gate's snapshot. The one check that needs no command and still
    settles (a `kind: manual` check never discharges in the graph)."""
    names = ", ".join(str(c) for c in task.get("criteria") or ()) or str(task.get("id"))
    return {"id": "V1", "kind": "dan",
            "check": str(task.get("evidence") or f"{names} is met"),
            "instructions": f"Check {names} on the gate's tree: {task.get('title') or ''}"
                            .rstrip(": ")}


def verifications(task: Mapping[str, Any], doc: doc_goals.Goals,
                  repo: Optional[Path] = None) -> list[dict]:
    """A proposed task's required gate checks: the graph refuses an agent task without one
    (INV-04). A direct task's are its criteria's (`checks`), an enabling task's that its
    criteria's tests run credibly (`credible`; its criterion is still RED by design); a
    task none of whose criteria has a runnable command is Dan's to confirm (`dan_check`)."""
    repo = Path(repo) if repo is not None else Path.cwd()
    by_id = {c.id: c for c in doc.criteria}
    named = [by_id[c] for c in task.get("criteria") or () if c in by_id]
    contribution = task.get("contribution")
    if isinstance(contribution, Mapping):
        contribution = "direct" if "direct" in contribution.values() else "enabling"
    found = checks(named, repo) if contribution == "direct" else credible(named, repo)
    return found or [dan_check(task)]


#: A proposal cannot know the files its task writes (its bound test is the one file it must
#: not edit), and a task without `scope:` runs alone (D20). `goals tasks` says so on each
#: block it shows Dan (GDT-14 review M2a); a YAML comment, so the block still parses.
SCOPE_HINT = ("# scope: add every file/dir this task writes, tests included; without "
              "`scope:` it runs alone, never beside other tasks (D20)")


def render_tasks(tasks: Sequence[Mapping[str, Any]], *, scope_hint: bool = False) -> str:
    """Proposed tasks as ROADMAP ```` ```yaml ```` blocks; with `scope_hint`, a block with
    no `scope:` ends with `SCOPE_HINT`."""
    return "".join("```yaml\n" + yaml.safe_dump(dict(task), sort_keys=False, width=1000,
                                                 allow_unicode=True,
                                                 default_flow_style=None)
                   + (SCOPE_HINT + "\n" if scope_hint and "scope" not in task else "")
                   + "```\n\n"
                   for task in tasks)


def feasibility(doc: doc_goals.Goals, tasks: Sequence[Mapping[str, Any]],
                results: Sequence[acceptance.Result]) -> list[doc_goals.Problem]:
    """Why the plan cannot reach the contract yet (R30); `[]` for a format-1 contract."""
    if doc.format != 2:
        return []
    try:
        unmet = list(_unmet(doc, results))
    except ValueError as exc:
        return [doc_goals.Problem("gate", str(exc))]
    named = _named(tasks)
    out: list[doc_goals.Problem] = []
    for c, state, _ in unmet:
        ids = (c.id, *_goals_of(doc, c))
        naming = named.get(c.id, [])
        if not naming:
            out.append(doc_goals.Problem(
                "gate", f"{c.id} is {state} and no open task names it in `criteria:`", ids))
        elif state in _CANNOT_RUN and not any(
                "enabling" in doc_goals.task_contribution(t).values() for t in naming):
            out.append(doc_goals.Problem(
                "gate", f"{c.id} is {state}: no open enabling task makes its test runnable "
                "before the direct work", ids))
    return out
