"""Modified-B replanning: when the plan is not reaching a goal, propose a concrete fix (GDT-8).

Q1 resolved as modified B (`01` §7, R44): a fixed two-task counter is not the algorithm. Per
goal, from the accepted tasks' review episodes (`episodes`, one per task and goal, upserted,
so a repair re-graduation or a rework never counts twice), a replan is considered on:

- (a) `repeated_misses`: `thresholds.goal_replan_misses` (default 2) distinct accepted
  `direct` tasks serving G with a verified `no_progress_unexpected`, under G's current terms;
- (b) `contradiction`: one episode whose review proved a criterion infeasible on this route
  (`goal_progress` `infeasible` with runtime proof), or a feasibility problem this
  graduation opened at a checkpoint (`goal_milestones`);
- (c) `milestone_gap` at a checkpoint: G's automated mandatory criteria are RED with none
  blocked or unknown, and either none turned GREEN since the last checkpoint (or the
  baseline) despite an accepted direct task, or it is G's convergence and RED remains.

`enabled`, `unchanged_expected`, `advanced`, `unknown` (a follow-up instead, R42),
`no_progress_legacy` and `regressed` (GDT-6/7's) never count; nor do enabling or
maintenance work, `serves: []`, or a goal verified only by operator checks (R70, C25). A
fact Dan already settled is never asked again; G's terms changing, or a fact not among the
settled ones, is new evidence. At most one signal per goal per graduation, and none while
one of G's replans waits on Dan.

The proposal (`propose`, R45) names the root cause (unattributed, R58), the scope, up to
three alternatives plus keep-going, each with its changes, cost, expected criterion impact
and authority (`goal_authority`), and a recommendation. A recommended routine plan
adjustment is applied (`on_graduation` returns its tasks for the graduation commit; the
goal's direct work waits for them, `held`); anything reserved asks Dan once
(`dan_questions` shape, `dq-replan-<id>`) and holds only G's direct and enabling work and
its dependents while independent work continues (R46, C27). `poll` reads his answer: an
approved task change is returned for the ROADMAP; an approved contract change holds until
his revision lands (G's terms or the contract revision change); keep-going and free text
(a note, never a reserved change) settle the facts and every episode the signal saw. The
replans `poll` reads are the store ∪ the replan question records (a lost store is
rebuilt); a settled replan's tasks that never reached the ROADMAP are returned again until
seen there. Pure but for the goals store (`goal_store`) and the channel.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from maestro import acceptance, goal_authority, goal_milestones, goal_roadmap, goal_store
from maestro.docs import goals as doc_goals

#: `finish_gap` is the completion gate's (GDT-10, `goal_completion`): `triggers` never
#: raises it.
KINDS = ("repeated_misses", "contradiction", "milestone_gap", "finish_gap")
DEFAULT_MISSES = 2
KEEP_GOING = "Keep going"
_CONFIDENCE = ("low", "medium", "high")
_SETTLED = ("applied", "decided")


def miss_threshold(document=None) -> int:
    """`project.yaml` `thresholds.goal_replan_misses`; 2 when unset, unusable or below 1."""
    from maestro import config

    value = config.threshold("goal_replan_misses", DEFAULT_MISSES, cast=int, document=document)
    return value if isinstance(value, int) and value >= 1 else DEFAULT_MISSES


def _hash(*parts: Any) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def terms(doc: doc_goals.Goals) -> dict[str, str]:
    """Per goal, a hash of what Dan approved for it: target, verifying ids and each verifying
    criterion's oracle, threshold, test and mandatory flag. It changes only with his
    contract revision."""
    by_id = {c.id: c for c in doc.criteria}
    return {g.id: _hash(g.target, list(g.verified_ids), [
        [c.id, c.oracle, c.threshold, c.test, c.mandatory]
        for c in (by_id.get(i) for i in doc.verifiers(g.id)) if c])[:12] for g in doc.goals}


# --- episodes --------------------------------------------------------------------------------


def collect_progress(store, artifacts, run_id: str) -> list[dict]:
    """The `goal_progress` records of the accepted run's last approved review that has
    them (`[]` without one): the goal reviewer's verdict that accepted the task, never a
    reworked round, and never dropped by a later lens that judges no goals."""
    if artifacts is None or not run_id:
        return []
    rows = store.conn.execute(
        "SELECT outcome_ref FROM attempts WHERE run_id = ? AND outcome_ref IS NOT NULL"
        " ORDER BY rowid", (run_id,)).fetchall()
    found: list[dict] = []
    for row in rows:
        try:
            result = json.loads(artifacts.read(row["outcome_ref"]).decode("utf-8"))
        except Exception:  # noqa: BLE001 — an unreadable outcome says nothing
            continue
        review = (result.get("outputs") or {}).get("review") if isinstance(result, dict) else None
        if isinstance(review, dict) and str(review.get("verdict") or "").lower() == "approved":
            records = [dict(r) for r in review.get("goal_progress") or () if isinstance(r, dict)]
            if records:
                found = records
    return found


def episodes(task: Mapping[str, Any], records: Iterable[Mapping[str, Any]],
             doc: doc_goals.Goals, *, run_id: str, workflow_id: str, at: str) -> list[dict]:
    """One episode per goal of the accepted task's review (`goal_progress.Read.records`)."""
    from maestro.workflows import goal_progress

    tid = str(task.get("id") or "")
    kinds = doc_goals.task_contribution(task)
    term = terms(doc)
    out = []
    for r in records:
        goal = str(r.get("goal") or "")
        if goal not in doc.ids:
            continue
        episode = {
            "id": f"{tid}:{goal}", "task": tid, "goal": goal, "run_id": run_id,
            "workflow_id": workflow_id, "at": at, "revision": doc.revision,
            "terms": term.get(goal), "contribution": kinds.get(goal, "none"),
            "served": bool(r.get("served")), "impact": goal_progress.stored_impact(r),
            "claimed": str(r.get("claimed") or ""),
            "evidence": [{k: i[k] for k in ("kind", "id", "name", "result", "value",
                                            "baseline", "criterion") if k in i}
                         for i in r.get("evidence") or () if isinstance(i, Mapping)]}
        if r.get("reason"):
            episode["reason"] = str(r["reason"])
        if isinstance(r.get("contradiction"), Mapping):
            episode["contradiction"] = dict(r["contradiction"])
        out.append(episode)
    return out


def _upsert(rows: list[dict], new: Sequence[Mapping]) -> list[dict]:
    ids = {str(n.get("id")) for n in new}
    return [r for r in rows if str(r.get("id")) not in ids] + [dict(n) for n in new]


def _summary(e: Mapping[str, Any]) -> str:
    items = []
    for i in e.get("evidence") or ():
        where = f" ({i['criterion']})" if i.get("criterion") else ""
        if i.get("kind") == "test":
            items.append(f"test {i.get('id')} {i.get('result')}{where}")
        elif i.get("kind") == "metric":
            items.append(f"{i.get('name') or 'metric'} {i.get('value')} vs baseline "
                         f"{i.get('baseline')}{where}")
        else:
            items.append(str(i.get("kind")) + where)
    return f"{e.get('task')}: {e.get('impact')}" + (f" ({'; '.join(items)})" if items else "")


# --- triggers --------------------------------------------------------------------------------


def settled(replans: Iterable[Mapping], goal: str, term: Optional[str]) -> set[str]:
    """The facts Dan's decisions (and applied replans) on `goal` settled at its terms: the
    signal's facts and every episode of the goal it was raised on (counted or only seen),
    so keep-going on a contradiction never lets the same misses raise a new replan."""
    out: set[str] = set()
    for r in replans:
        if r.get("goal") == goal and r.get("status") in _SETTLED and r.get("terms") == term:
            trigger = r.get("trigger") or {}
            for key in ("facts", "episodes", "seen"):
                out |= {str(f) for f in trigger.get(key) or ()}
    return out


def _operator_only(doc: doc_goals.Goals, goal: str) -> bool:
    verifiers = doc.verifiers(goal)
    return bool(verifiers) and not any(
        c.mandatory for c in doc.criteria if c.id in verifiers)


def _signal(goal: str, kind: str, facts: Sequence[str], episodes_: Sequence[str],
            evidence: Sequence[str], criteria: Sequence[str], **extra) -> dict:
    facts = sorted(dict.fromkeys(facts))
    return {"goal": goal, "kind": kind, "facts": facts, "episodes": list(episodes_),
            "evidence": list(evidence), "criteria": list(criteria),
            "fingerprint": _hash(goal, kind, facts), **extra}


def _named(eps: Sequence[Mapping], doc: doc_goals.Goals, goal: str) -> list[str]:
    named = {str(i.get("criterion")) for e in eps for i in e.get("evidence") or ()
             if i.get("criterion")}
    mine = [c.id for c in doc.criteria if c.mandatory and goal in doc_goals.entry_goals(doc, c)]
    return [c for c in mine if c in named] or mine


def _contradiction(goal, eps, done, checkpoint) -> Optional[dict]:
    facts, ids, evidence, criteria = [], [], [], []
    for e in eps:
        c = e.get("contradiction")
        if isinstance(c, Mapping) and c.get("facts") and c["facts"] not in done:
            facts.append(str(c["facts"]))
            ids.append(e["id"])
            criteria.append(str(c.get("criterion")))
            evidence.append(f"{_summary(e)}: {c.get('criterion')} infeasible: {c.get('why')}")
    if facts:
        return _signal(goal, "contradiction", facts, ids, evidence,
                       list(dict.fromkeys(criteria)))
    problems = [p for p in ((checkpoint or {}).get("feasibility") or {}).get(goal) or ()
                if _hash(p)[:12] not in done]
    if problems:
        crits = [i for i in doc_goals._ids(" ".join(problems)) if i.startswith("AC-")]
        return _signal(goal, "contradiction", [_hash(p)[:12] for p in problems], [],
                       [f"checkpoint {checkpoint['id']}: {p}" for p in problems],
                       list(dict.fromkeys(crits)), feasibility=problems)
    return None


def _gap(goal, mine, current, done, checkpoint) -> Optional[dict]:
    """`mine` (unsettled episodes) decides whether accepted work failed to move G; the
    fact names every accepted direct task (`current`, settled or not), so a settled gap is
    asked again only when a task joins it or the REDs change."""
    states = ((checkpoint or {}).get("goals") or {}).get(goal) or {}
    reds = [c for c, s in states.items() if s == acceptance.RED]
    if (checkpoint or {}).get("skipped", {}).get(goal):     # criteria not rerun: no verdict
        return None
    if not reds or any(s in (acceptance.BLOCKED, acceptance.UNKNOWN) for s in states.values()):
        return None
    before = (checkpoint.get("before") or {}).get(goal) or {}
    turned = [c for c, s in states.items() if s == acceptance.GREEN
              and before.get(c) != acceptance.GREEN]
    direct = [e for e in mine if e.get("contribution") == "direct"]
    converged = goal in ((checkpoint.get("milestones") or {}).get("convergence") or ())
    if not (converged or (direct and not turned)):
        return None
    tasks = sorted({str(e["task"]) for e in current if e.get("contribution") == "direct"})
    fact = _hash(sorted(reds), tasks)[:12]
    if fact in done:
        return None
    kinds = ", ".join(checkpoint.get("milestones") or ())
    evidence = [f"checkpoint {checkpoint['id']} ({kinds} after {checkpoint.get('task')}): "
                f"{', '.join(reds)} RED" + (f" despite accepted {', '.join(tasks)}"
                                            if tasks else "")]
    return _signal(goal, "milestone_gap", [fact], [e["id"] for e in direct],
                   evidence + [_summary(e) for e in direct], reds)


def _misses(goal, mine, doc, misses) -> Optional[dict]:
    by_task: dict[str, Mapping] = {}
    for e in mine:
        if (e.get("impact") == "no_progress_unexpected" and e.get("served")
                and e.get("contribution") == "direct"):
            by_task[str(e["task"])] = e
    if len(by_task) < misses:
        return None
    eps = list(by_task.values())
    return _signal(goal, "repeated_misses", [e["id"] for e in eps], [e["id"] for e in eps],
                   [_summary(e) for e in eps], _named(eps, doc, goal))


def triggers(store: Mapping[str, Any], doc: doc_goals.Goals,
             checkpoint: Optional[Mapping[str, Any]] = None, *,
             misses: int = DEFAULT_MISSES) -> list[dict]:
    """The replan signals the store's history raises now, at most one per goal."""
    term = terms(doc)
    replans = list(store.get("replans") or ())
    waiting = {r.get("goal") for r in replans if r.get("status") == "asked"}
    known = {r.get("id") for r in replans}
    out = []
    for goal in doc.ids:
        if goal in waiting or _operator_only(doc, goal):
            continue
        done = settled(replans, goal, term.get(goal))
        current = [e for e in store.get("episodes") or ()
                   if e.get("goal") == goal and e.get("terms") == term.get(goal)]
        mine = [e for e in current if e.get("id") not in done]
        found = (_contradiction(goal, current, done, checkpoint)
                 or _gap(goal, mine, current, done, checkpoint)
                 or _misses(goal, mine, doc, misses))
        if found and "RP-" + found["fingerprint"][:8] not in known:
            found["seen"] = [str(e["id"]) for e in current]
            out.append(found)
    return out


# --- the proposal ----------------------------------------------------------------------------


def _open(tasks: Sequence[Mapping]) -> dict[str, Mapping]:
    return {str(t["id"]): t for t in tasks if t.get("id") and t.get("status") != "complete"}


def _holds(task: Mapping, goal: str) -> bool:
    """A replan on `goal` holds the task's work on it unless it is only maintenance."""
    return goal in (doc_goals.task_serves(task) or ()) and \
        doc_goals.task_contribution(task).get(goal) != "maintenance"


def _with_dependents(tasks: Mapping[str, Mapping], roots: Iterable[str]) -> list[str]:
    held = list(dict.fromkeys(r for r in roots if r in tasks))
    grew = True
    while grew:
        grew = False
        for tid, task in tasks.items():
            if tid in held:
                continue
            deps = task.get("deps") or []
            if any(str(d) in held for d in ([deps] if isinstance(deps, str) else deps)):
                held.append(tid)
                grew = True
    return [t for t in tasks if t in held]


def cause_file(task_id: str) -> str:
    """Where a diagnosis task writes the cause it found (its gate check, GDT-10 review H3)."""
    return f"docs/diagnosis/{task_id}.md"


#: The tokens a diagnosis cause file's first line names (GDT-12, R58): W12's attribution
#: reads the reviewed line by `review_origins.DIAGNOSIS_FAULT`.
CAUSE_TOKENS = ("plan", "scope", "goal_mapping", "harness", "measurement", "dependency",
                "constraint", "agent", "unknown")


def cause_check(cause: str) -> str:
    """The diagnosis task's gate check: its cause file's first line is a valid `Cause:`
    line, the one line W12's attribution reads (`improvement_evidence._cause_line`;
    review L1: a `Cause:` line further down no longer passes the gate)."""
    return f"head -n 1 {cause} | grep -qE '^Cause: ({'|'.join(CAUSE_TOKENS)})$'"


def _diagnosis(goal: str, rp: str, criteria: Sequence[str], doc: doc_goals.Goals,
               hypothesis: str) -> dict:
    tests = [c.test.strip().strip("`").strip() for c in doc.criteria
             if c.id in criteria and c.test]
    names = ", ".join(criteria) or goal
    tid = f"{goal.lower()}-diagnose-{rp[3:].lower()}"
    cause = cause_file(tid)
    return {
        "id": tid, "status": "open",
        "mode": "autonomous", "title": f"{goal}: find why {names} do not move",
        "short_desc": (f"{hypothesis} Run {names}'s tests on the current tree and find the "
                       "cause (the route, the harness or the measurement); write it down "
                       f"with the evidence in {cause} and commit it. Start the file with one "
                       f"line `Cause: <{'|'.join(CAUSE_TOKENS)}>`; `unknown` when the "
                       "evidence does not establish one. Change no acceptance test or "
                       "threshold."),
        "serves": [goal], "contribution": "enabling", "criteria": list(criteria),
        "tests": tests, "evidence": f"a written cause for {names} in {cause}, with the test "
                                    "or measurement that shows it",
        "deps": [], "replan_of": rp,
        # It writes the cause file and nothing else: declared, it runs beside independent
        # work instead of alone (D20; R66 limit).
        "scope": [cause],
        "verifications": [{"id": "V1", "kind": "auto", "cmd": cause_check(cause)}],
    }


def _missing_tasks(signal: Mapping, doc, results, tasks, rp: str) -> list[dict]:
    """GDT-4's proposed tasks for the criteria a feasibility problem named, renamed so an
    id graduated before is never reused."""
    try:
        proposed = goal_roadmap.propose_tasks(doc, results, tasks)
    except ValueError:
        return []
    keep = [t for t in proposed if set(t.get("criteria") or ()) & set(signal["criteria"])]
    rename = {t["id"]: f"{t['id']}-{rp[3:].lower()}" for t in keep}
    return [{**t, "id": rename[t["id"]], "status": "open", "mode": "autonomous",
             "deps": [rename.get(d, d) for d in t.get("deps") or ()], "replan_of": rp}
            for t in keep]


def _alternative(label, action, doc, *, add=(), waits=(), contract=None, criteria=(),
                 expected="", spent=0) -> dict:
    if action == "keep_going":
        needs, why = False, "keeps the plan as it is; a new miss counts from zero"
    else:
        needs, why = goal_authority.requires_dan(action, doc)
    return {"label": label, "action": action,
            "changes": {"add_tasks": list(add), "deps_rewire": list(waits),
                        "contract_text": contract},
            "cost": {"tasks": len(add), "attempts_spent": spent, "paid": False},
            "impact": {"criteria": {c: expected for c in criteria}, "other_goals": []},
            "requires_dan": needs, "authority_reason": why}


def propose(signal: Mapping[str, Any], doc: doc_goals.Goals, tasks: Sequence[Mapping],
            *, task: str = "", results: Sequence[acceptance.Result] = (), at: str = "",
            analyse: Optional[Callable[[str, str], str]] = None,
            repo: Optional[Path] = None) -> dict:
    """The concrete replanning proposal for one signal (R45). Every task it adds carries a
    required gate check (`goal_roadmap.verifications`, run in `repo`): the graph refuses an
    agent task without one (INV-04)."""
    goal, kind = signal["goal"], signal["kind"]
    rp = "RP-" + signal["fingerprint"][:8]
    criteria = list(signal.get("criteria") or ())
    names = ", ".join(criteria) or goal
    open_ = _open(tasks)
    serving = [tid for tid, t in open_.items() if goal in (doc_goals.task_serves(t) or ())]
    direct = [tid for tid in serving
              if doc_goals.task_contribution(open_[tid]).get(goal) == "direct"]
    held = _with_dependents(open_, [tid for tid in serving if _holds(open_[tid], goal)])
    spent = len(signal.get("episodes") or ())
    tasks_named = sorted({e.split(":", 1)[0] for e in signal.get("episodes") or ()})
    if kind == "contradiction" and signal.get("feasibility"):
        hypothesis = (f"The remaining plan cannot reach {names}: "
                      + "; ".join(signal["feasibility"]))
        confidence = "high"
    elif kind == "contradiction":
        hypothesis = (f"The review evidence shows {names} cannot be met on the current "
                      f"route ({', '.join(tasks_named)}).")
        confidence = "high"
    elif kind == "milestone_gap":
        hypothesis = (f"At the checkpoint {names} stay RED"
                      + (f" despite accepted {', '.join(tasks_named)}" if tasks_named else "")
                      + ": the route is not converging on them.")
        confidence = "medium"
    elif kind == "finish_gap":
        commit = str(signal.get("commit") or "")[:12]
        done = int(signal.get("rounds") or 0)
        hypothesis = (f"The completion gate found {names} unmet on the final tree"
                      + (f" ({commit})" if commit else "")
                      + (f" after {done} corrective round{'s' if done != 1 else ''}"
                         if done else "") + ": the queue is empty but the contract is not met.")
        confidence = "medium"
    else:
        hypothesis = (f"{len(tasks_named)} accepted direct tasks for {goal} "
                      f"({', '.join(tasks_named)}) did not move {names}: the route may not "
                      "reach them, or their tests may not measure what the tasks change.")
        confidence = "low"
    cause = {"hypothesis": hypothesis, "evidence": list(signal.get("evidence") or ()),
             "confidence": confidence, "attribution": "unattributed"}
    approach = _analyse(analyse, signal, cause, doc) if analyse else ""
    at_cap = kind == "finish_gap" and bool(signal.get("at_cap"))
    if kind == "finish_gap":
        # GDT-10: the completion gate names its own corrective tasks; at its cap another
        # round is Dan's call, never routine.
        first = _alternative("One more round" if at_cap else "Add the corrective tasks",
                             "routine_plan_adjustment", doc,
                             add=[dict(t) for t in signal.get("tasks") or ()],
                             criteria=criteria, expected="named by a corrective task",
                             spent=spent)
        if at_cap:
            first.update(requires_dan=True, authority_reason=(
                f"the completion gate's cap ({_rounds(signal.get('cap'))}) is reached; "
                "another round is Dan's call"))
    elif signal.get("feasibility"):
        first = _alternative("Add the missing tasks", "routine_plan_adjustment", doc,
                             add=_missing_tasks(signal, doc, results, tasks, rp),
                             criteria=criteria, expected="named by a new task", spent=spent)
    else:
        diag = _diagnosis(goal, rp, criteria, doc, cause["hypothesis"])
        if approach:
            diag["notes"] = f"Suggested approach: {approach}"
        first = _alternative("Add a diagnosis task", "routine_plan_adjustment", doc,
                             add=[diag], waits=direct, criteria=criteria,
                             expected="unchanged until the diagnosis names the cause",
                             spent=spent)
    by_id = {c.id: c for c in doc.criteria}
    threshold = any(by_id[c].threshold for c in criteria if c in by_id)
    contract = _alternative(
        f"Revise {goal}'s contract", "acceptance_threshold" if threshold else "goal_change",
        doc, contract=(f"Dan revises {doc_goals.DOC}: {names}'s threshold or {goal}'s target, "
                       f"as a new contract revision; until it lands, {goal}'s direct and "
                       "enabling work stays held."),
        criteria=criteria, expected="judged again at the new revision", spent=spent)
    keep = _alternative(KEEP_GOING, "keep_going", doc, criteria=criteria,
                        expected="stays as it is for now", spent=spent)
    alternatives = [first, contract, keep] if first["changes"]["add_tasks"] else \
        [contract, keep]
    for alt in alternatives:
        alt["changes"]["add_tasks"] = [
            t if t.get("verifications") else
            {**t, "verifications": goal_roadmap.verifications(t, doc, repo)}
            for t in alt["changes"]["add_tasks"]]
    if at_cap:
        recommended = alternatives.index(contract)
        why = (f"the completion gate's cap ({_rounds(signal.get('cap'))}) is reached: "
               f"{names} still unmet after {int(signal.get('rounds') or 0)} corrective "
               "round(s); another round or a contract revision is Dan's decision")
    elif kind == "contradiction" and not signal.get("feasibility") or len(alternatives) == 2:
        recommended = alternatives.index(contract)
        why = (f"{contract['label']}: the evidence says {names} cannot be met as approved, "
               "and only Dan changes the contract")
    else:
        recommended = 0
        why = (f"{first['label']}: a routine plan adjustment within {goal}'s approved scope; "
               "the cause is not proven, so no contract change is proposed yet")
    return {"id": rp, "goal": goal, "trigger": dict(signal),
            "stage": "finish" if kind == "finish_gap" else "replan",
            "revision": doc.revision, "terms": terms(doc).get(goal), "task": task, "at": at,
            "status": "", "root_cause": cause,
            "scope": {"criteria_red": criteria, "open_tasks": serving, "held_tasks": held,
                      "independent": "continues"},
            "alternatives": alternatives, "recommended": recommended, "why": why}


def _rounds(cap: Any) -> str:
    """`N round(s)`, the completion gate's cap as text."""
    n = int(cap or 0)
    return f"{n} round{'s' if n != 1 else ''}"


def _analyse(analyse: Callable[[str, str], str], signal: Mapping, cause: dict,
             doc: doc_goals.Goals) -> str:
    """The optional judgment call: a better hypothesis and an approach for the diagnosis.
    Schema-checked; anything else keeps the deterministic proposal. Returns the approach."""
    system = ("You analyse why a project's plan is not reaching an approved goal. Answer "
              'with JSON only: {"hypothesis": "<one or two sentences>", "confidence": '
              '"low" | "medium" | "high", "approach": "<one sentence for the next task>"}. '
              "Never blame a role; never propose changing the goal or its tests.")
    user = json.dumps({"goal": signal["goal"], "target": doc.by_id[signal["goal"]].target,
                       "signal": signal["kind"], "criteria": signal.get("criteria"),
                       "evidence": signal.get("evidence"), "hypothesis": cause["hypothesis"]})
    try:
        raw = (analyse(system, user) or "").strip()
        found = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    except Exception:  # noqa: BLE001 — a failed judgment keeps the deterministic proposal
        return ""
    if not isinstance(found, dict):
        return ""
    hypothesis, confidence = found.get("hypothesis"), found.get("confidence")
    if isinstance(hypothesis, str) and hypothesis.strip() and confidence in _CONFIDENCE:
        cause.update(hypothesis=hypothesis.strip()[:600], confidence=confidence)
    approach = found.get("approach")
    return approach.strip()[:400] if isinstance(approach, str) else ""


# --- Dan's question and decision -------------------------------------------------------------


def _consequence(alt: Mapping[str, Any], *, finish: bool = False) -> str:
    changes = alt["changes"]
    if alt["action"] == "keep_going":
        what = "No change; these facts are settled and only new evidence asks again"
        if finish:
            what += "; the project stays blocked, never done"
    elif changes["contract_text"]:
        what = changes["contract_text"]
    else:
        added = ", ".join(t["id"] for t in changes["add_tasks"])
        what = f"Adds {added}" + (f"; {', '.join(changes['deps_rewire'])} wait for it"
                                  if changes["deps_rewire"] else "")
    return (f"{what}. Cost: {alt['cost']['tasks']} task(s), no paid service. "
            + ("Needs your approval." if alt["requires_dan"] else "Routine."))


def question(proposal: Mapping[str, Any]) -> dict:
    """The `poll_question` record for a reserved recommendation (GDT-7's shape)."""
    rp, goal = proposal["id"], proposal["goal"]
    alts = proposal["alternatives"]
    best = alts[proposal["recommended"]]
    held = proposal["scope"]["held_tasks"]
    trigger = proposal["trigger"]
    finish = trigger["kind"] == "finish_gap"
    if finish:
        done = int(trigger.get("rounds") or 0)
        head = "the completion gate found it unmet" + (
            f" {done} round{'s' if done != 1 else ''} running (cap {trigger.get('cap')})"
            if trigger.get("at_cap") else "")
    else:
        head = {"repeated_misses": "repeated unexpected misses",
                "contradiction": "a decisive contradiction",
                "milestone_gap": "a milestone gap"}[trigger["kind"]]
    state = "is not done" if finish else "is not converging"
    text = (f"{rp}: {goal} {state} ({head}). {proposal['root_cause']['hypothesis']} "
            f"Evidence: {'; '.join(proposal['root_cause']['evidence'])}. Remaining: "
            f"{', '.join(proposal['scope']['criteria_red']) or goal}; contract revision "
            f"{proposal['revision']}. Held: {', '.join(held) or 'nothing'}; independent work "
            "continues. Which change?")
    why = (f"{best['authority_reason']}. Recommended: {proposal['why']}. Root cause is "
           f"{proposal['root_cause']['confidence']}-confidence and unattributed.")
    options = [{"label": a["label"], "consequence": _consequence(a, finish=finish),
                "resolution": "keep_going" if a["action"] == "keep_going" else "approve",
                "alternative": i} for i, a in enumerate(alts)]
    # The wait is keyed by the replan, not the graduated task: two replans asked at one
    # graduation keep a wait each (`verify._mark_question`).
    return {"id": f"dq-replan-{rp}", "task_id": proposal.get("task") or goal,
            "run_id": f"replan-{rp}", "wait_key": f"replan-{rp}", "title": f"Replan {goal}",
            "spec": {"question": text, "why_undefined": why, "options": options,
                     "recommended": proposal["recommended"], "replan": dict(proposal)}}


def decide(proposal: Mapping[str, Any], answer: str, at: str) -> dict:
    """Dan's answer: a tapped option approves its alternative (keep-going settles the
    facts); anything else is his note, recorded and never applied."""
    answer = str(answer or "").strip()
    chosen = next((a for a in proposal["alternatives"]
                   if a["label"].strip().lower() == answer.lower()), None)
    facts = list(proposal["trigger"].get("facts") or ())
    if chosen is None:
        return {"resolution": "notes", "alternative": "", "action": "", "answer": answer,
                "rationale": answer, "at": at, "facts": facts, "hold_until_terms": False}
    if chosen["action"] == "keep_going":
        return {"resolution": "keep_going", "alternative": chosen["label"],
                "action": "keep_going", "answer": answer,
                "rationale": "Dan chose to keep the plan; a new miss counts from zero",
                "at": at, "facts": facts, "hold_until_terms": False}
    return {"resolution": "approve", "alternative": chosen["label"], "action": chosen["action"],
            "answer": answer, "rationale": chosen["label"], "at": at, "facts": facts,
            "hold_until_terms": bool(chosen["changes"]["contract_text"])}


def decisions_for(repo: Path, goals: Iterable[str]) -> list[dict]:
    """Dan's replan decisions on `goals`, for briefs (`[]` without a store)."""
    goals = set(goals)
    if not goals or not goal_store.path_for(repo).exists():
        return []
    out = []
    for r in goal_store.read(repo)["replans"]:
        d = r.get("decision")
        if r.get("goal") in goals and r.get("status") == "decided" and isinstance(d, Mapping):
            out.append({"id": r.get("id"), "goal": r.get("goal"),
                        "resolution": d.get("resolution"), "alternative": d.get("alternative"),
                        "answer": d.get("answer") if d.get("resolution") == "notes" else "",
                        "at": d.get("at")})
    return out


# --- holds -----------------------------------------------------------------------------------


def _awaits_revision(r: Mapping[str, Any], doc: Optional[doc_goals.Goals],
                     term: Mapping[str, str]) -> bool:
    """Dan approved a contract change and his revision has not landed: neither the goal's
    terms nor the contract revision moved since the proposal."""
    return bool(r.get("status") == "decided"
                and (r.get("decision") or {}).get("hold_until_terms")
                and term.get(r.get("goal")) == r.get("terms")
                and doc is not None and doc.revision == r.get("revision"))


def parked_added(repo: Path, tasks: Sequence[Mapping[str, Any]],
                 parked: Iterable[str]) -> dict[str, str]:
    """`{task id: replan id}` of the tasks a replan added that are open on the ROADMAP and
    parked (failed for good): Dan is told, and the work they hold is blocked, not waiting
    on a replan (`waiting_holds`). `{}` without a store."""
    if not goal_store.path_for(repo).exists():
        return {}
    parked, open_ = set(parked), _open(tasks)
    return {tid: str(r.get("id")) for r in goal_store.read(repo)["replans"]
            for tid in r.get("applied") or () if tid in open_ and tid in parked}


def waiting_holds(repo: Path, tasks: Sequence[Mapping[str, Any]],
                  parked: Iterable[str]) -> dict[str, str]:
    """`held(...)` without the holds of a replan whose added task is parked (failed for
    good): that work waits on Dan's `/unpark` as the dependents of any parked task do, so it
    is a blocked queue, not one waiting on a replan (`parked_added` reports it)."""
    stuck = set(parked_added(repo, tasks, parked).values())
    return {tid: rp for tid, rp in held(repo, tasks).items() if rp not in stuck}


def held(repo: Path, tasks: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """`{task id: replan id}` the replans hold (R46): while one waits on Dan, or after he
    approved a contract change until his revision lands (the goal's terms or the contract
    revision change), every open task working on the goal (not maintenance) and its
    dependents; while an added task is open, the tasks waiting for it and their
    dependents. `{}` without a store."""
    if not goal_store.path_for(repo).exists():
        return {}
    replans = goal_store.read(repo)["replans"]
    if not replans:
        return {}
    doc = doc_goals.load(repo)
    term = terms(doc) if doc is not None else {}
    open_ = _open(tasks)
    out: dict[str, str] = {}
    for r in replans:
        goal, added = r.get("goal"), set(r.get("applied") or ())
        roots: list[str] = []
        if r.get("status") == "asked" or _awaits_revision(r, doc, term):
            roots += [tid for tid, t in open_.items() if _holds(t, goal)]
        if added & set(open_):
            roots += [w for w in r.get("waits") or () if w in open_]
        for tid in _with_dependents(open_, roots):
            if tid not in added:
                out.setdefault(tid, str(r.get("id")))
    return out


def describe_holds(repo: Path, holds: Mapping[str, str], parked: Iterable[str] = ()) -> str:
    """What each holding replan waits for, for the journal and Dan: his answer, his
    contract revision, or the task it added (with `parked`, an added task that is parked
    waits on Dan's `/unpark`). `holds` is `held(...)`."""
    parked = set(parked)
    by: dict[str, list[str]] = {}
    for tid, rp in holds.items():
        by.setdefault(rp, []).append(tid)
    rows = {str(r.get("id")): r for r in goal_store.read(repo)["replans"]}
    doc = doc_goals.load(repo)
    term = terms(doc) if doc is not None else {}
    parts = []
    for rp, ids in by.items():
        r = rows.get(rp) or {}
        goal = r.get("goal") or "?"
        if r.get("status") == "asked":
            why = f"waits on Dan's answer to {r.get('question_id') or 'dq-replan-' + rp}"
        elif _awaits_revision(r, doc, term):
            why = (f"waits on Dan's revision of {doc_goals.DOC} ({goal}'s contract, "
                   f"approved: {(r.get('decision') or {}).get('alternative') or 'revise'})")
        elif stuck := [t for t in r.get("applied") or () if t in parked]:
            why = "; ".join(f"its added task {t} is parked (it failed for good): /unpark {t} "
                            "or change the plan" for t in stuck)
        else:
            why = f"waits for {', '.join(r.get('applied') or ()) or 'its added task'}"
        parts.append(f"{rp} ({goal}): {why}; holds {', '.join(ids)}")
    return "; ".join(parts)


def asked_ids(repo: Path) -> set[str]:
    """The replans waiting on Dan's answer (`set()` without a store)."""
    if not goal_store.path_for(repo).exists():
        return set()
    return {str(r.get("id")) for r in goal_store.read(repo)["replans"]
            if r.get("status") == "asked"}


# --- wiring ----------------------------------------------------------------------------------


def _store_replan(repo: Path, proposal: Mapping) -> None:
    goal_store.update(repo, "replans", lambda rows: _upsert(rows, [proposal]))


def _settle(r: Mapping[str, Any], decision: Mapping[str, Any]) -> dict:
    """Replan `r` decided by Dan: an approved task change records the tasks it adds."""
    alt = next((a for a in r.get("alternatives") or ()
                if a["label"] == decision.get("alternative")), None)
    add = list(alt["changes"]["add_tasks"]) if alt and decision.get("resolution") == \
        "approve" else []
    return {**r, "status": "decided", "decision": dict(decision),
            "settled_at": decision.get("at"), "applied": [t["id"] for t in add],
            "waits": list(alt["changes"]["deps_rewire"]) if add else []}


def _added_tasks(r: Mapping[str, Any]) -> dict[str, dict]:
    """`{id: task}` a settled replan added (its `applied` ids, from its alternatives)."""
    if r.get("status") not in _SETTLED:
        return {}
    ids = [str(i) for i in r.get("applied") or ()]
    tasks = {str(t.get("id")): t for a in r.get("alternatives") or ()
             for t in (a.get("changes") or {}).get("add_tasks") or ()}
    return {i: dict(tasks[i]) for i in ids if i in tasks}


def unlanded(replans: Iterable[Mapping[str, Any]], present: Iterable[str]) -> list[dict]:
    """The tasks settled replans added that never reached the ROADMAP (review M1): neither
    in it nor completed (`present`), and never seen there (`landed`), so a task Dan later
    deletes by hand stays deleted. The append is idempotent, so returning one twice is
    harmless."""
    present = set(present)
    out: list[dict] = []
    for r in replans:
        landed = {str(i) for i in r.get("landed") or ()}
        out += [t for i, t in _added_tasks(r).items() if i not in present and i not in landed]
    return out


def _dedupe(tasks: Iterable[Mapping]) -> list[dict]:
    return list({str(t.get("id")): dict(t) for t in tasks}.values())


def on_graduation(repo: Path, task: Mapping[str, Any], records: Sequence[Mapping],
                  open_tasks: Sequence[Mapping[str, Any]], channel=None, *, run_id: str = "",
                  workflow_id: str = "", at: str = "", notify=None, misses=None,
                  analyse=None, runner=subprocess.run,
                  timeout: int = acceptance.DEFAULT_TIMEOUT, journal=None,
                  completed: Optional[Iterable[str]] = None,
                  observed: Optional[Mapping[str, Any]] = None) -> tuple[list[dict], list[str]]:
    """At an accepted task's graduation: record its episodes, run a milestone checkpoint
    when one is reached, and act on the replan signals. Returns `(tasks to add in the
    graduation commit, checkpoint follow-ups)`; `([], [])` without `docs/GOALS.md`.

    A re-graduation of the same accepted run reuses the checkpoint it recorded (never a
    second run of the goal tests). With `completed` (the graduated ids), the tasks of
    settled replans that never reached the ROADMAP come back too (`unlanded`). One signal
    that fails is journaled `goal_replan_failed`; the others still act.

    GDT-9: `observed` (`goal_state.collect_observed`, the accepted run's gate reruns) and
    the checkpoint's reruns are stored as the goals' durable progress, the contract
    revision seen is kept, and the replan reads the baseline overlaid by them. A failure
    there is journaled `goal_state_failed` and never stops the graduation."""
    from maestro import goal_state

    doc = doc_goals.load(repo)
    if doc is None or not task.get("id"):
        return [], []
    tid = str(task["id"])
    journal = journal or getattr(channel, "journal", None) or (lambda event, detail: None)
    notify = notify or getattr(channel, "notify", None) or (lambda text: None)
    open_tasks = [t for t in open_tasks if str(t.get("id") or "") != tid]
    found = episodes(task, records, doc, run_id=run_id, workflow_id=workflow_id, at=at)
    if found:
        goal_store.update(repo, "episodes", lambda rows: _upsert(rows, found))
    _durable(journal, tid, lambda: goal_state.record_gate(repo, doc, observed or {}, task=tid,
                                                          run_id=run_id, at=at))
    _durable(journal, tid, lambda: goal_state.observe_contract(repo, doc))
    store = goal_store.read(repo)
    prior = next((c for c in reversed(store["checkpoints"])
                  if c.get("task") == tid and c.get("run_id") == run_id), None)
    point, follow_ups = prior, list((prior or {}).get("follow_ups") or ())
    reached = {} if prior else goal_milestones.milestones(task, open_tasks, doc, store)
    if reached:
        point, follow_ups = goal_milestones.checkpoint(
            repo, doc, reached, task, open_tasks, store=store, at=at, run_id=run_id,
            runner=runner, timeout=timeout,
            heartbeat=lambda text: journal("goal_checkpoint_running", text))
        goal_store.update(repo, "checkpoints", lambda rows: _upsert(rows, [point]))
        _durable(journal, tid, lambda: goal_state.record_checkpoint(
            repo, doc, point, commit=goal_state.head(repo)))
        store = goal_store.read(repo)
    extra: list[dict] = []
    results = goal_state.results(repo, doc)
    for signal in triggers(store, doc, point,
                           misses=miss_threshold() if misses is None else misses):
        try:
            extra += _act(repo, signal, doc, open_tasks, tid, results, at, analyse,
                          channel, journal, notify)
        except Exception as exc:  # noqa: BLE001 — one signal never drops the others
            journal("goal_replan_failed",
                    f"{tid} {signal.get('goal')}: {type(exc).__name__}: {exc}"[:300])
    if completed is not None:
        present = {tid, *completed, *(str(t.get("id")) for t in open_tasks)}
        extra += unlanded(goal_store.read(repo)["replans"], present)
    return _dedupe(extra), follow_ups


def _durable(journal, tid: str, write: Callable[[], Any]) -> None:
    """One GDT-9 progress write; a failure is journaled, never raised (the graduation and
    the replan go on: a lost observation only leaves the baseline's view)."""
    try:
        write()
    except Exception as exc:  # noqa: BLE001 — progress is best-effort at graduation
        journal("goal_state_failed", f"{tid}: {type(exc).__name__}: {exc}"[:300])


def _act(repo, signal, doc, open_tasks, tid, results, at, analyse, channel, journal,
         notify) -> list[dict]:
    """One signal: the routine recommendation applied (its tasks returned), else Dan
    asked once."""
    proposal = propose(signal, doc, open_tasks, task=tid, results=results, at=at,
                       analyse=analyse, repo=repo)
    best = proposal["alternatives"][proposal["recommended"]]
    if not best["requires_dan"]:
        add = list(best["changes"]["add_tasks"])
        proposal.update(status="applied", settled_at=at, applied=[t["id"] for t in add],
                        waits=list(best["changes"]["deps_rewire"]))
        _store_replan(repo, proposal)
        journal("goal_replan_applied", f"{proposal['id']} {proposal['goal']}: "
                                       f"{best['label']} ({', '.join(proposal['applied'])})")
        notify(f"🧭 Replan {proposal['id']} for {proposal['goal']}: "
               f"{proposal['root_cause']['hypothesis']} Applied (routine): {best['label']} "
               f"({', '.join(proposal['applied']) or 'no task'}).")
        return add
    spec = question(proposal)
    proposal.update(status="asked", question_id=spec["id"])
    _store_replan(repo, proposal)
    journal("goal_replan_asked", f"{proposal['id']} {proposal['goal']}: {best['label']}")
    if channel is not None:
        channel.poll_question(spec)
    return []


def _replan_records() -> list[dict]:
    from maestro.hitl import verify

    return verify.question_records(prefix="dq-replan-")


def _rebuild(repo: Path, rows: list[dict], records: Iterable[Mapping], journal) -> list[dict]:
    """Store ∪ question records (the design's rebuild, as `runner.goal_decisions` does for
    GDT-7): a replan whose question was asked but which the store lost (a corrupt file is
    moved aside and starts empty) comes back from the question's own payload, decided when
    the record holds Dan's recorded decision, else asked. Each is written back."""
    known = {str(r.get("id")) for r in rows}
    for record in records:
        spec = record.get("spec") if isinstance(record, Mapping) else None
        payload = spec.get("replan") if isinstance(spec, Mapping) else None
        if not isinstance(payload, Mapping) or not payload.get("id") \
                or str(payload["id"]) in known:
            continue
        restored = {**dict(payload), "status": "asked", "question_id": str(record.get("id"))}
        decision = record.get("replan_decision")
        if record.get("recorded") and isinstance(decision, Mapping):
            restored = _settle(restored, decision)
        known.add(str(payload["id"]))
        rows.append(restored)       # acted on this poll even if the write-back fails
        try:
            _store_replan(repo, restored)
        except (OSError, ValueError) as exc:
            journal("goal_replan_poll_failed", f"{payload['id']}: not restored: "
                                               f"{type(exc).__name__}: {exc}"[:300])
            continue
        journal("goal_replan_restored", f"{payload['id']} {payload.get('goal')}: "
                                        f"{restored['status']} (from {record.get('id')})")
    return rows


def poll(repo: Path, channel, at: str, *, present: Optional[Iterable[str]] = None,
         records: Optional[Callable[[], Iterable[Mapping]]] = None) -> list[dict]:
    """Each poll: send what waits unsent and read Dan's answers. Returns the tasks his
    approvals add (the caller commits them to the ROADMAP). A store write that fails leaves
    the replan asked, so the next poll decides it again from his answer.

    The replans are the store ∪ the replan question records (`_rebuild`); a row that cannot
    be read is journaled `goal_replan_poll_failed` and the others are still decided. With
    `present` (the ROADMAP's and the completed ids), a settled replan's tasks that never
    reached the ROADMAP are returned again (`unlanded`); one seen there is marked `landed`."""
    if channel is None:
        return []
    from maestro.hitl import verify

    journal = getattr(channel, "journal", None) or (lambda event, detail: None)
    rows = _rebuild(repo, goal_store.read(repo)["replans"],
                    (records or _replan_records)(), journal)
    added: list[dict] = []
    for r in rows:
        if r.get("status") != "asked":
            continue
        try:
            spec = question(r)
            answer = channel.poll_question(spec)
            if answer is None:
                continue
            decision = decide(r, answer, verify._answered_at(spec["id"]) or at)
            settled_row = _settle(r, decision)
            _store_replan(repo, settled_row)
            channel.mark_question_recorded(spec["id"], replan_decision=decision)
            journal("goal_replan_decided", f"{r['id']} {r.get('goal')}: "
                                           f"{decision['resolution']} {answer[:120]}")
            added += list(_added_tasks(settled_row).values())
        except Exception as exc:  # noqa: BLE001 — one bad row never stops the others
            journal("goal_replan_poll_failed",
                    f"{r.get('id')}: {type(exc).__name__}: {exc}"[:300])
    if present is not None:
        present = set(present)
        replans = goal_store.read(repo)["replans"]
        added += unlanded(replans, present)
        if any(_newly_landed(r, present) for r in replans):
            goal_store.update(repo, "replans", lambda rows: [
                {**r, "landed": sorted({*(r.get("landed") or ()), *_newly_landed(r, present)})}
                if _newly_landed(r, present) else r for r in rows])
    return _dedupe(added)


def _newly_landed(r: Mapping[str, Any], present: set[str]) -> set[str]:
    return (set(_added_tasks(r)) & present) - {str(i) for i in r.get("landed") or ()}
