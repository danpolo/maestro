"""Goal-vs-goal conflicts, escalated to Dan with a recommendation (P13 GDT-7).

When a change advances one approved goal and regresses another, only Dan decides which one
gives (`01` §8, R48): the goal priority informs the recommendation but never decides (R08).
`judge` sorts a change's regressions into blocking findings (a hard invariant is never
traded, R51), questions for Dan (one per regressed goal, at most
`dan_questions.MAX_QUESTIONS_PER_ROUND` a round, R66), regressions Dan already accepted, and
plain regressions the caller handles as before (GDT-6). `escalation` builds the question
(ids, revision, stage, task, evidence, options with consequences, recommendation, R49);
`decide` turns Dan's answer into a decision; `record`/`load` keep decisions in
`.orchestrator/goal_decisions.json` (locked, a corrupt file moved aside), and `merge` joins
the answers the store lost. A stored decision covers each (regressed goal, criterion) it
names, whatever advanced in a later change, until the evidence exceeds what Dan approved,
the facts change or the contract changes for its ids (R50, C22, C24); a free-text (`other`)
answer never accepts (Dan, 2026-10-18). `contract_problems` finds conflicts written into
the contract itself, for setup (R47).

Pure: nothing here reads or writes anything but `record`/`load`. The controller freezes
`context(...)` into the gate's and the reviewer's policy; workers read nothing else.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from maestro import goal_store
from maestro.docs import goals as doc_goals
from maestro.workflows.dan_questions import MAX_QUESTIONS_PER_ROUND

#: `01` §8.1 (a)-(e). (a) "incompatible by definition" is the grill's to find (GDT-13).
KINDS = ("incompatible", "tradeoff", "degradation", "threshold", "invariant")
#: Where a conflict can surface (R47).
STAGES = ("grill", "setup", "review", "replan", "finish")
#: Dan's decisions, relative to the project root: the goals store's `decisions` (GDT-8).
STORE = goal_store.STORE
RESOLUTIONS = ("preserve", "accept", "other")

_EPSILON = 1e-6


@dataclass(frozen=True)
class Regression:
    """A goal that got worse: a criterion GREEN→RED, or a metric worse than its baseline."""
    goal: str
    criterion: str = ""
    value: Optional[float] = None
    baseline: Optional[float] = None
    #: Shown to Dan, never part of the facts: a reason can hold temp paths or addresses.
    evidence: str = ""
    #: The criterion's state now (`acceptance` states); part of the facts.
    state: str = ""


def _facts(r: Regression) -> str:
    """One regression's facts: criterion, state, value and baseline, no reason text."""
    return _hash(r.criterion, r.state, r.value, r.baseline)


@dataclass(frozen=True)
class Conflict:
    kind: str
    stage: str
    #: The goals that advanced in the same change.
    advanced: tuple[str, ...]
    #: The one goal that regressed (one conflict per regressed goal).
    regressed: str
    #: The regressed goal's criteria that got worse.
    criteria: tuple[str, ...]
    revision: Optional[int]
    evidence: tuple[str, ...]
    #: Per regression: criterion, value, baseline, direction, worse_pct (None: unmeasured).
    measurements: tuple[Mapping[str, Any], ...]
    tasks: tuple[str, ...]
    summary: str
    #: Which criteria advanced each advanced goal: ((goal, (criteria...)), ...); not in `id`.
    progress: tuple[tuple[str, tuple[str, ...]], ...] = ()
    #: Per criterion, the hash of its facts (`_facts`): ((criterion, hash), ...); not in `id`.
    facts: tuple[tuple[str, str], ...] = ()

    @property
    def id(self) -> str:
        key = f"{self.kind}|{','.join(self.advanced)}|{self.regressed}|{','.join(self.criteria)}"
        return "GC-" + hashlib.sha1(key.encode()).hexdigest()[:8]

    @property
    def fingerprint(self) -> str:
        """The facts behind the conflict (criterion, state, value, baseline per regression;
        never reason text): an unmeasured accept covers only these (C24)."""
        return _hash([list(f) for f in self.facts])

    @property
    def measured(self) -> bool:
        return bool(self.measurements) and all(
            m.get("worse_pct") is not None for m in self.measurements)

    @property
    def worst(self) -> Optional[float]:
        """The largest measured worsening in percent, rounded up to a tenth."""
        if not self.measured:
            return None
        return _ceil(max(float(m["worse_pct"]) for m in self.measurements))


@dataclass(frozen=True)
class Judgement:
    #: Findings that block: hard invariants, Dan's "keep", conflicts past the round cap.
    blocking: tuple[str, ...]
    #: `dan_questions`-shaped escalations, at most `MAX_QUESTIONS_PER_ROUND`.
    questions: tuple[dict, ...]
    #: Regressions a stored decision of Dan's covers: {conflict, regressed, criteria, ...}.
    accepted: tuple[dict, ...]
    #: No conflict (nothing advanced elsewhere): the caller keeps GDT-6's finding.
    plain: tuple[Regression, ...]


# --- helpers ---------------------------------------------------------------------------------


def _ceil(pct: float) -> float:
    return math.ceil(pct * 10 - _EPSILON) / 10


def _pct(value: float) -> str:
    return f"{value:g}"


def _hash(*parts: Any) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:12]


def _gorder(gid: str) -> tuple[int, str]:
    return (int(gid[1:]) if gid[1:].isdigit() else 0, gid)


def _corder(cid: str) -> tuple[int, int, str]:
    return doc_goals._id_key(cid)


def _direction(criterion: doc_goals.Criterion) -> Optional[tuple[str, bool]]:
    from maestro import acceptance  # lazy: acceptance runs things; only its parser is used

    return acceptance._direction(criterion)


def _number(text: str) -> Optional[float]:
    from maestro import acceptance

    found = acceptance._NUMBER.search(text or "")
    return float(found.group()) if found else None


# --- the context the controller freezes ------------------------------------------------------


def context(doc: doc_goals.Goals, task_id: str, decisions: Optional[Iterable[Mapping]] = None,
            *, judged: Iterable[str] = ()) -> dict:
    """What `judge` reads, JSON-able, frozen into the gate's `goals_guard` policy and the
    reviewer's `goals_check`. `judged`: the criteria an earlier stage of the same change
    already judged on this tree (the review's: the ones the gate reran)."""
    invariants: dict[str, list[str]] = {}
    for inv in doc.invariants:
        for cid in inv.checked_by:
            if cid.startswith("AC-"):
                invariants.setdefault(cid, []).append(inv.id)
    directions = {}
    for c in doc.criteria:
        found = _direction(c) if c.evidence == "metric" else None
        if found:
            directions[c.id] = found[0]
    terms = {g.id: _hash(g.target, list(g.verified_ids)) for g in doc.goals}
    terms.update({c.id: _hash(c.oracle, c.threshold, c.test) for c in doc.criteria})
    return {
        "revision": doc.revision, "priority": list(doc.priority), "invariants": invariants,
        "directions": directions, "terms": terms,
        "decisions": [dict(d) for d in decisions or ()], "task": str(task_id or ""),
        "goals": {g.id: g.name for g in doc.goals}, "judged": list(judged),
    }


def _measurement(r: Regression, ctx: Mapping) -> dict:
    direction = (ctx.get("directions") or {}).get(r.criterion)
    worse = None
    if (direction in ("below", "above") and r.value is not None and r.baseline is not None
            and r.baseline != 0):
        delta = r.value - r.baseline if direction == "below" else r.baseline - r.value
        worse = round(delta / abs(r.baseline) * 100, 4)
    return {"criterion": r.criterion, "value": r.value, "baseline": r.baseline,
            "direction": direction, "worse_pct": worse}


def _detail(m: Mapping, evidence: str) -> str:
    if m.get("worse_pct") is not None:
        text = (f"{m['value']:g} vs baseline {m['baseline']:g}, "
                f"{_pct(_ceil(m['worse_pct']))}% worse")
        return f"{text}, {evidence}" if evidence else text
    return evidence or "GREEN→RED"


def _named(gid: str, ctx: Mapping) -> str:
    name = (ctx.get("goals") or {}).get(gid)
    return f"{gid} ({name})" if name else gid


def _rank(gid: str, ctx: Mapping) -> float:
    priority = list(ctx.get("priority") or ())
    return priority.index(gid) if gid in priority else math.inf


# --- judge -----------------------------------------------------------------------------------


def judge(regressions: Sequence[Regression], advanced: Mapping[str, Sequence[str]],
          ctx: Mapping, *, stage: str, place: str = "") -> Judgement:
    """Sort one change's regressions; `advanced` is `{goal: [criteria that advanced]}`.

    A stored decision covers each (regressed goal, criterion) it names, whatever advanced
    this time, nothing included: the stored baseline keeps an accepted criterion GREEN, so
    every later change regresses it again. A criterion no decision names is a conflict when
    another goal advanced, else `plain`. `place` names where the question is asked when the
    stage alone does not say it (the gate asks within the review stage)."""
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; one of {', '.join(STAGES)}")
    invariants = ctx.get("invariants") or {}
    blocking: list[str] = []
    plain: list[Regression] = []
    by_goal: dict[str, list[Regression]] = {}
    hits: dict[str, list[Regression]] = {}
    for r in regressions:
        if r.criterion in invariants:
            # Never a trade-off for progress; with nothing advanced, GDT-6's finding says it.
            if advanced:
                hits.setdefault(r.criterion, []).append(r)
            else:
                plain.append(r)
        else:
            by_goal.setdefault(r.goal, []).append(r)
    for cid, regs in hits.items():   # one finding per criterion, whatever its goals
        blocking.append(_invariant_finding(regs, invariants[cid], ctx))

    decisions = [d for d in ctx.get("decisions") or () if isinstance(d, Mapping)]
    questions: list[dict] = []
    accepted: list[dict] = []
    for goal in sorted(by_goal, key=_gorder):
        others = {g: list(c or ()) for g, c in advanced.items() if g != goal}
        groups: dict[str, tuple[Mapping, list[Regression]]] = {}
        uncovered: list[Regression] = []
        for r in by_goal[goal]:
            d = _covering(decisions, goal, r.criterion)
            if d is None:
                uncovered.append(r)
            else:
                groups.setdefault(str(d.get("id")), (d, []))[1].append(r)
        asked: list[Regression] = []
        reasons: list[str] = []
        prior: Optional[Mapping] = None
        for d, regs in groups.values():
            verdict, text = _coverage(
                d, _conflict(goal, regs, others or _advanced_by(d), ctx, stage), ctx)
            if verdict == "blocking":
                blocking.append(text)
            elif verdict == "covered":
                accepted.append(text)
            else:
                asked += regs
                reasons.append(text)
                prior = prior or d
        if others:
            asked += uncovered
        else:
            plain += uncovered
        if not asked:
            continue
        conflict = _conflict(goal, asked, others or _advanced_by(prior), ctx, stage)
        if len(questions) < MAX_QUESTIONS_PER_ROUND:
            questions.append(escalation(conflict, ctx, reason="; ".join(reasons),
                                        place=place))
        else:
            blocking.append(f"goal conflict {conflict.id} ({conflict.summary}) waits for the "
                            f"next question round (at most {MAX_QUESTIONS_PER_ROUND} at once)")
    return Judgement(tuple(blocking), tuple(questions), tuple(accepted), tuple(plain))


def _covering(decisions: Sequence[Mapping], goal: str, criterion: str) -> Optional[Mapping]:
    """Dan's latest decision on `goal` regressing `criterion` (on a criterion-less
    regression of `goal` when `criterion` is empty), or `None`."""
    found: Optional[Mapping] = None
    for d in decisions:   # the latest `at` wins; a tie keeps the later entry
        if d.get("regressed") != goal:
            continue
        criteria = list(d.get("criteria") or ())
        if not ((criterion in criteria) if criterion else not criteria):
            continue
        if found is None or str(d.get("at") or "") >= str(found.get("at") or ""):
            found = d
    return found


def _advanced_by(decision: Optional[Mapping]) -> dict[str, list[str]]:
    """The goals a decision traded for, when the change at hand advanced none."""
    return {str(g): [] for g in (decision or {}).get("advanced") or ()}


def _invariant_finding(regs: Sequence[Regression], invs: Sequence[str], ctx: Mapping) -> str:
    r = regs[0]
    goals_ = ", ".join(dict.fromkeys(x.goal for x in regs))
    detail = _detail(_measurement(r, ctx), r.evidence)
    return (f"hard_invariant_regression: {goals_} regressed on {r.criterion}, which checks "
            f"{', '.join(invs)} ({detail}); a hard invariant is never traded for progress on "
            "another goal. Only Dan can grant an exception, by revising the contract.")


def _conflict(goal: str, regs: Sequence[Regression], advanced: Mapping[str, Sequence[str]],
              ctx: Mapping, stage: str) -> Conflict:
    regs = sorted(regs, key=lambda r: _corder(r.criterion) if r.criterion else (-1, 0, ""))
    others = tuple(sorted((g for g in advanced if g != goal), key=_gorder))
    criteria = tuple(dict.fromkeys(r.criterion for r in regs if r.criterion))
    measurements = tuple(_measurement(r, ctx) for r in regs)
    evidence = tuple(f"{r.criterion}: {_detail(m, r.evidence)}" if r.criterion
                     else _detail(m, r.evidence) for r, m in zip(regs, measurements))
    task = str(ctx.get("task") or "")
    summary = (f"{', '.join(others)} advanced while {goal} regressed"
               + (f" ({', '.join(criteria)})" if criteria else ""))
    progress = tuple((g, tuple(advanced[g] or ())) for g in others)
    facts = tuple((r.criterion, _facts(r)) for r in regs)
    return Conflict("tradeoff", stage, others, goal, criteria, ctx.get("revision"), evidence,
                    measurements, (task,) if task else (), summary, progress, facts)


def _ids(conflict: Conflict) -> list[str]:
    return [*conflict.advanced, conflict.regressed, *conflict.criteria]


def _terms(conflict: Conflict, ctx: Mapping) -> dict[str, Optional[str]]:
    terms = ctx.get("terms") or {}
    return {i: terms.get(i) for i in _ids(conflict)}


def _coverage(d: Mapping, conflict: Conflict, ctx: Mapping) -> tuple[str, Any]:
    """Whether Dan's decision `d` covers `conflict` (the regressions on the criteria it
    names): ("covered", accepted entry) | ("blocking", finding) | ("ask", why asked again).

    `accept` with a bound covers every measured worsening within it; an unmeasured
    accept covers the same facts. Either covers a criterion an earlier stage of this change
    already judged on this tree (`ctx["judged"]`): the review never re-asks what the gate
    measured. `preserve` blocks citing Dan; `other` (free text) never accepts (Dan,
    2026-10-18): the same facts block citing his words, new facts ask again."""
    did = str(d.get("id") or conflict.id)
    at = d.get("at") or "an earlier date"
    stored = d.get("terms") or {}
    terms = ctx.get("terms") or {}
    ids = list(dict.fromkeys([*(d.get("advanced") or ()), conflict.regressed,
                              *conflict.criteria]))
    changed = [i for i in ids if stored.get(i) != terms.get(i)]
    if changed:
        return "ask", (f"the contract changed for {', '.join(changed)} since Dan decided "
                       f"{did} on {at}")
    resolution = d.get("resolution")
    criteria = ", ".join(conflict.criteria)
    if resolution == "preserve":
        keep = d.get("keep") or conflict.regressed
        return "blocking", (f"Dan decided {did} ({at}): keep {keep}. Rework without "
                            f"regressing {criteria or keep} ({'; '.join(conflict.evidence)})")
    known = {str(c): f for c, f in (d.get("facts") or ())}
    same = bool(conflict.facts) and all(known.get(c) == f for c, f in conflict.facts)
    judged = bool(conflict.criteria) and set(conflict.criteria) <= set(ctx.get("judged") or ())
    bound = d.get("bound")
    if resolution == "accept":
        if judged:
            pass
        elif bound is not None:
            approved = f"+{_pct(float(bound))}%"
            if not conflict.measured:
                return "ask", (f"no measurement to compare with the {approved} Dan approved "
                               f"on {at}")
            worst = max(float(m["worse_pct"]) for m in conflict.measurements)
            if worst > float(bound) + _EPSILON:
                return "ask", (f"{conflict.regressed} is {_pct(_ceil(worst))}% worse, beyond "
                               f"the {approved} Dan approved on {at}")
        elif not same:
            return "ask", f"new evidence since Dan answered {did} on {at}"
        return "covered", {"conflict": did, "regressed": conflict.regressed,
                           "criteria": list(conflict.criteria), "resolution": resolution,
                           "bound": bound, "answer": d.get("answer", ""),
                           "at": d.get("at", "")}
    if not same:
        return "ask", f"new evidence since Dan answered {did} on {at}"
    return "blocking", (f"Dan answered {did} on {at}: '{d.get('answer', '')}'; the change "
                        f"still regresses {conflict.regressed}"
                        + (f" ({criteria})" if criteria else ""))


# --- escalation ------------------------------------------------------------------------------


def escalation(conflict: Conflict, ctx: Mapping, *, reason: str = "", place: str = "") -> dict:
    """The question for Dan: a `dan_questions` question plus each option's `resolution`,
    `keep` and `bound`, and the `conflict` itself (what `decide` stores). `place` names
    where it is asked (default: the stage)."""
    b = conflict.regressed
    a = min(conflict.advanced, key=lambda g: (_rank(g, ctx), _gorder(g))) \
        if conflict.advanced else ""
    task = conflict.tasks[0] if conflict.tasks else ""
    crits = ", ".join(conflict.criteria)
    criteria_text = crits or f"{b}'s outcome"
    rev = (f"contract revision {conflict.revision}" if conflict.revision is not None
           else "no contract revision")
    where = (f" in task {task}" if task else "") + f" at {place or conflict.stage}, {rev}"
    progress = dict(conflict.progress)
    advanced_by = []
    for g in conflict.advanced:
        named = ", ".join(progress.get(g, ()))
        advanced_by.append(_named(g, ctx) + " advanced" + (f" ({named})" if named else ""))
    question = (f"{conflict.id}: {' and '.join(advanced_by)} while {_named(b, ctx)} regressed "
                f"({'; '.join(conflict.evidence)}){where}. Keep {b}, or accept the "
                "regression?")
    if reason:
        question += f" Asked again: {reason}."

    a_rank, b_rank = _rank(a, ctx), _rank(b, ctx)
    if a_rank < b_rank:
        priority = f"Priority ranks {a} above {b}"
    elif b_rank < a_rank:
        priority = f"Priority ranks {b} above {a}"
    else:
        priority = f"Priority does not rank {a} against {b}"
    worst = conflict.worst
    if worst is not None and a_rank < b_rank:
        recommended, why = 1, (f"accept the bounded regression, because {a} ranks above {b} "
                               f"and its cost is measured (up to +{_pct(worst)}%)")
    elif worst is None:
        recommended, why = 0, (f"keep {b}, because the regression is not measured, so no "
                               "bound can be approved")
    elif b_rank < a_rank:
        recommended, why = 0, f"keep {b}, because {b} ranks above {a}"
    else:
        recommended, why = 0, f"keep {b}, because the priority does not rank {a} above {b}"
    held = f"task {task} only" if task else f"work on {b} only"
    why_undefined = (f"Only Dan decides a trade-off between approved goals (01 §8). "
                     f"{priority}: it informs the recommendation, never decides (R08). "
                     f"Held: {held}; independent work continues. Recommended: {why}.")

    rework = f"Rework task {task}" if task else "Rework"
    gains = " and ".join(f"{g}'s" for g in conflict.advanced) or "the other goals'"
    keep = {"label": f"Keep {b}",
            "consequence": (f"{rework} so {criteria_text} holds its baseline; {gains} "
                            "progress may shrink. No contract change."),
            "resolution": "preserve", "keep": b, "bound": None}
    kept = ", ".join(conflict.advanced)
    if worst is not None:
        accept = {"label": f"Accept {b} worse by up to +{_pct(worst)}%",
                  "consequence": (f"{criteria_text} may be up to {_pct(worst)}% worse than its "
                                  "baseline; your contract revision needs a TO- line for it "
                                  f"and {crits or b}'s test tolerance corrected (until then "
                                  f"this decision covers {criteria_text} in later tasks too). "
                                  "Anything worse asks again."),
                  "resolution": "accept", "keep": kept, "bound": worst}
    else:
        accept = {"label": f"Accept {b} failing {crits}" if crits else f"Accept {b}'s "
                  "regression",
                  "consequence": (f"{criteria_text} may stay failing, for this evidence only; "
                                  "your contract revision needs a TO- line for it and "
                                  f"{crits or b}'s test corrected (until then this decision "
                                  f"covers {criteria_text} in later tasks too). Any new "
                                  "evidence asks again."),
                  "resolution": "accept", "keep": kept, "bound": None}
    payload = {**asdict(conflict), "id": conflict.id, "terms": _terms(conflict, ctx),
               "fingerprint": conflict.fingerprint}
    payload = json.loads(json.dumps(payload))
    return {"question": question, "why_undefined": why_undefined, "options": [keep, accept],
            "recommended": recommended, "conflict": payload}


# --- Dan's decision --------------------------------------------------------------------------


def decide(spec: Mapping, answer: str, at: str) -> dict:
    """Dan's answer to an escalation as a stored decision: a tapped option is its
    resolution; anything else is his own words (`other`)."""
    conflict = spec.get("conflict")
    if not isinstance(conflict, Mapping) or not conflict.get("id"):
        raise ValueError("not a goal-conflict question: no conflict")
    answer = str(answer or "").strip()
    chosen: Mapping = {"resolution": "other", "keep": "", "bound": None}
    for option in spec.get("options") or ():
        if str(option.get("label") or "").strip().lower() == answer.lower():
            chosen = option
            break
    return {
        "id": conflict["id"], "resolution": chosen.get("resolution", "other"),
        "keep": chosen.get("keep", ""), "bound": chosen.get("bound"), "answer": answer,
        "at": at, "question": str(spec.get("question") or ""),
        **{k: conflict.get(k) for k in ("kind", "stage", "tasks", "revision", "advanced",
                                         "regressed", "criteria", "terms", "fingerprint",
                                         "facts")},
    }


def path_for(repo: Path) -> Path:
    return goal_store.path_for(repo)


def load(repo: Path) -> list[dict]:
    """Every stored decision, `[]` when there is none or the file does not read."""
    return goal_store.read(repo)["decisions"]


def record(repo: Path, decision: Mapping) -> Path:
    """Store `decision`, replacing an earlier one for the same conflict.

    Through `goal_store.update`: locked, so two answers recorded at once both land, atomic,
    a corrupt file moved aside and never overwritten, and the store's other sections
    (GDT-8's episodes, checkpoints and replans) kept."""
    def replace(stored: list[dict]) -> list[dict]:
        return [d for d in stored if d.get("id") != decision.get("id")] + [dict(decision)]

    return goal_store.update(repo, "decisions", replace)


def merge(stored: Iterable[Mapping], answered: Iterable[Mapping]
          ) -> tuple[list[dict], list[dict]]:
    """`(decisions, missing)`: the stored decisions plus each answered one the store lacks
    (no decision with its id, or only an older one by `at`), and those it lacked."""
    out = [dict(d) for d in stored]
    missing: list[dict] = []
    for d in answered:
        found = [i for i, s in enumerate(out) if s.get("id") == d.get("id")]
        if found and str(out[found[-1]].get("at") or "") >= str(d.get("at") or ""):
            continue
        out = [s for s in out if s.get("id") != d.get("id")] + [dict(d)]
        missing.append(dict(d))
    return out, missing


# --- conflicts in the contract (setup) -------------------------------------------------------


def contract_problems(doc: doc_goals.Goals) -> tuple[doc_goals.Problem, ...]:
    """Conflicts written into the contract, as gate problems on the ids they affect:
    two mandatory metric criteria on one `Test:` binding whose thresholds cannot both pass
    (`threshold`), and a `TO-` trading a hard invariant (`invariant`)."""
    out: list[doc_goals.Problem] = []
    rules: dict[str, list[tuple[doc_goals.Criterion, str, bool, float]]] = {}
    for c in doc.criteria:
        if not (c.mandatory and c.test and c.evidence == "metric"):
            continue
        found, limit = _direction(c), _number(c.threshold)
        if found and limit is not None:
            rules.setdefault(c.test, []).append((c, found[0], found[1], limit))
    for test, entries in rules.items():
        for i, (a, da, ia, la) in enumerate(entries):
            for b, db, ib, lb in entries[i + 1:]:
                if da == db:
                    continue
                (low, li), (high, hi) = ((la, ia), (lb, ib)) if da == "above" else \
                    ((lb, ib), (la, ia))
                if low < high or (low == high and li and hi):
                    continue
                goals_ = (*doc_goals.entry_goals(doc, a), *doc_goals.entry_goals(doc, b))
                out.append(doc_goals.Problem(
                    "gate", f"threshold conflict: {a.id} ({_rule(da, la, ia)}) and {b.id} "
                            f"({_rule(db, lb, ib)}) share the binding `{test}` and cannot "
                            "both pass; ask Dan which threshold stands",
                    tuple(dict.fromkeys((a.id, b.id, *goals_))), ask=True))
    checks: dict[str, list[str]] = {}
    for inv in doc.invariants:
        for cid in inv.checked_by:
            checks.setdefault(cid, []).append(inv.id)
    by_ac = {c.id: c for c in doc.criteria}
    for to in doc.trade_offs:
        named = doc_goals._ids(f"{to.text} {to.bound}")
        invs = [i for i in named if i.startswith("INV-")]
        crits = [i for i in named if i in checks]
        for cid in crits:
            invs += [i for i in checks[cid] if i not in invs]
        if not invs:
            continue
        for inv in doc.invariants:
            if inv.id in invs:
                crits += [c for c in inv.checked_by if c not in crits]
        goals_ = [g for c in crits if c in by_ac for g in doc_goals.entry_goals(doc, by_ac[c])]
        out.append(doc_goals.Problem(
            "gate", f"invariant conflict: {to.id} trades {', '.join(invs)} (a hard invariant"
                    + (f", checked by {', '.join(crits)}" if crits else "") + "); a hard "
                    "invariant is never traded: only Dan can change it, by revising the "
                    "invariant itself",
            tuple(dict.fromkeys((to.id, *invs, *crits, *goals_))), ask=True))
    return tuple(out)


def _rule(direction: str, limit: float, inclusive: bool) -> str:
    return f"{direction} {limit:g}" + (" or equal" if inclusive else "")
