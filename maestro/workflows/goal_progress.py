"""The goals contract in the graph runner: briefs and the reviewer's `goal_progress` (P13 W11a).

Every agent role's manifest carries a bounded excerpt of `docs/GOALS.md`
(`maestro.docs.goals.excerpt`), so planner, implementer, reviewer, diagnoser and any later
role (triage, W7) see why the project exists. Since GDT-5 the excerpt is the role's own:
served criteria with their tests and baseline states, invariants and trade-off bounds,
and one rule per role (D23 for writers, gaps for the planner, policy for the diagnoser).
A reviewer's manifest also carries the check it owes (GDT-6), with the task's
contribution per goal and each criterion's goals, layer, evidence kind, test and
current-revision baseline state:

- `served` (the task's `serves:` names goals): one `goal_progress` entry per served goal,
  and one for any other goal the change makes worse.
- `regressions` (no `serves:`) and `skip` (`serves: []`): entries only for a goal the
  change makes worse; `serves: []` skips expected advancement, never regression (R28).

Each entry states an impact (`IMPACTS`, R38) and evidence items (`EVIDENCE_KINDS`); the
outcome is decided here, by rule (R39). A diff shows an edit, not an outcome: `advanced`,
`regressed` and `no_progress_unexpected` need a test result, a complete measurement, a
trace or an operator observation, else they read as `unknown` with a targeted follow-up
(R40, R42); a mocked test counts only for a unit criterion (R24). A failing test of a
criterion GREEN at the baseline is a verified regression whatever was claimed, except one
the task's gate found RED on the base too (`red_on_base`, GDT-9 review M4: a note, as at
the gate). A verified
`regressed` on any goal is a synthesized blocking finding; `no_progress_unexpected` on a
served goal is a shortfall that merges (R37), an unattributed observation, never a
planner fault (R58). Goal work the task did not ask for is a W9 out-of-scope finding that
never widens the task's scope (R36, D23). A stored W11a `no_progress` reads as
`LEGACY_NO_PROGRESS` (R62). Unknown ids, bad values and missing entries are flagged,
never failed. GDT-8: an entry's `infeasible` with proof for a criterion is a
`contradiction` the replan reads (`maestro.goal_replan`), else a follow-up; every role's
brief carries Dan's replan decisions on the goals its task serves.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from maestro.docs import goals as doc_goals

IMPACTS = ("advanced", "enabled", "unchanged_expected", "no_progress_unexpected",
           "regressed", "unknown")
#: A W11a `progress: no_progress` record, read back; never an unexpected miss (C32).
LEGACY_NO_PROGRESS = "no_progress_legacy"
EVIDENCE_KINDS = ("test", "metric", "trace", "operator", "diff")
#: Impacts that claim an outcome and so need runtime evidence.
_NEEDS_PROOF = ("advanced", "regressed", "no_progress_unexpected")
#: `goals_check.mode` values.
SERVED, REGRESSIONS, SKIP = "served", "regressions", "skip"
_REVIEW_FILE = "review.json"
_PROOF = ("a test result, a measurement against its baseline, a trace or an operator "
          "observation")
#: `acceptance.GREEN` (`maestro.acceptance` is imported lazily, as in `manifest_fields`).
_GREEN = "green_already_satisfied"


def stored_impact(record: Mapping[str, Any]) -> str:
    """The impact of a stored record: `impact`, else W11a's `progress`, whose
    `no_progress` is `LEGACY_NO_PROGRESS`."""
    if "impact" in record:
        return str(record["impact"])
    progress = str(record.get("progress") or "")
    return LEGACY_NO_PROGRESS if progress == "no_progress" else progress


def _criteria(repo: Path, doc: doc_goals.Goals, red_on_base=()) -> dict[str, dict]:
    from maestro import acceptance, goal_state

    # GDT-9 (C12): a criterion observed GREEN since the baseline is protected like a
    # baseline GREEN one, so a failing test of it is a verified regression, unless this
    # task's gate found it RED on the base too (`red_on_base`, review M4).
    states = goal_state.protected_states(repo, doc)

    def direction(c: doc_goals.Criterion) -> Optional[str]:
        found = acceptance._direction(c) if c.evidence == "metric" else None
        return found[0] if found else None

    return {c.id: {"goals": doc_goals.entry_goals(doc, c), "layer": c.layer,
                   "evidence": c.evidence, "test": c.test, "baseline": states.get(c.id),
                   "direction": direction(c),
                   **({"red_on_base": True} if c.id in red_on_base else {})}
            for c in doc.criteria}


def manifest_fields(repo: Path, task: Mapping[str, Any], *, role: str,
                    decisions: Optional[list] = None, red_on_base=()) -> tuple[dict, str]:
    """`role`'s `goals_excerpt` (GDT-5, with the stored acceptance baseline) plus
    `goals_check` for a reviewer, and the excerpt's cut (`""` when none). Nothing when the
    project has no `docs/GOALS.md`. `red_on_base`: the criteria this task's gate noted
    `goal_protected_red_on_base` (`WorkflowRunner.gate_red_on_base`)."""
    from maestro import goal_state

    doc = doc_goals.load(repo)
    if doc is None:
        return {}, ""
    serves = doc_goals.task_serves(task)
    # GDT-9: the baseline overlaid by later observations, each labelled with its source.
    text, cut = doc_goals.excerpt(doc, serves, role=role, task=task,
                                  results=goal_state.results(repo, doc),
                                  sources=goal_state.sources(repo, doc))
    fields: dict[str, Any] = {"goals_excerpt": text}
    from maestro import goal_replan

    # GDT-8 (R46): Dan's answers to a replan of a goal this task serves, for every role.
    replans = goal_replan.decisions_for(repo, serves or ())
    if replans:
        fields["replan_decisions"] = replans
    if role == "reviewer":
        from maestro import goal_conflicts
        from maestro.workflows import goal_guard

        mode = REGRESSIONS if serves is None else (SERVED if serves else SKIP)
        fields["goals_check"] = {"mode": mode, "serves": list(serves or ()),
                                 "goal_ids": list(doc.ids),
                                 "contribution": doc_goals.task_contribution(task),
                                 "criteria": _criteria(repo, doc, set(red_on_base)),
                                 # GDT-7: what `goal_conflicts.judge` reads, Dan's decisions
                                 # in, and the criteria the gate already judged.
                                 "conflicts": goal_conflicts.context(
                                     doc, str(task.get("id") or ""),
                                     goal_conflicts.load(repo) if decisions is None
                                     else decisions,
                                     judged=goal_guard.reruns(repo, doc, task))}
    return fields, cut


def brief_section(repo: Path, task: Mapping[str, Any], *, role: str) -> str:
    """The GOALS section for a brief built outside the graph manifest (the legacy
    implementer); `""` when the project has no `docs/GOALS.md`."""
    fields, cut = manifest_fields(repo, task, role=role)
    return render({**fields, "omitted_material": [cut] if cut else []}, role=role)


def _reviewer_rule(check: Mapping[str, Any]) -> str:
    mode = check.get("mode")
    if mode not in (SERVED, REGRESSIONS, SKIP):
        return ""
    impacts = " | ".join(f'"{i}"' for i in IMPACTS)
    shape = (f'"goal_progress": [{{"goal": "<id>", "impact": {impacts}, "evidence": '
             '[<items>]}, ...]')
    if mode == SERVED:
        ask = (f"GOAL PROGRESS: this task serves {', '.join(check.get('serves') or ())}. "
               f"Add {shape} to {_REVIEW_FILE}: one entry for each goal it serves, and one "
               "for any other goal above that the change makes worse.")
    else:
        why = ("serves no goal (serves: [])" if mode == SKIP
               else "does not name the goals it serves")
        ask = (f"GOAL PROGRESS: this task {why}. Add {shape} to {_REVIEW_FILE} only for a "
               "goal the change makes worse (regressed).")
    return (
        ask + " Evidence items: "
        '{"kind": "test", "id": "<test id>", "result": "passed" | "failed", '
        '"criterion": "<AC id>", "mocked": true when it runs against mocks}; '
        '{"kind": "metric", "name": "<what>", "value": <number>, "baseline": <number>, '
        '"criterion": "<AC id>"}; {"kind": "trace", "path": "<log or trace file>"}; '
        '{"kind": "operator", "observation": "<what a person saw>", "by": "<who>"}; '
        '{"kind": "diff", "text": "<what the change does>"}. '
        "advanced, regressed and no_progress_unexpected need a test, metric, trace or "
        "operator item: a diff shows an edit, not an outcome, and a mocked test is no "
        "evidence for an e2e or integration criterion. enabled (a prerequisite now "
        "exists), unchanged_expected (no movement was promised) and unknown (no evidence "
        "either way) need none. State what you observed; the outcome is decided by rule: "
        "a verified regressed on any goal is blocking; no_progress_unexpected on a served "
        "goal is recorded and does not send the task back. When the evidence shows a "
        "criterion cannot be met on this route at all, add \"infeasible\": \"<why>\" to "
        "that goal's no_progress_unexpected entry, with the test or metric item that "
        "shows it: it asks for a replan at once; without such an item it is only a "
        "follow-up. Judge the task's correctness "
        "against its own acceptance criteria and notes first, and its goal impact "
        "separately. A goal never widens this task's scope: never ask for work beyond the "
        "task because a goal would benefit; report goal work the change does that the task "
        'does not ask for as "unasked_goal_work": [{"path": "<file>", "goal": "<id>", '
        '"what": "<one line>"}], which is a scope finding, not progress.'
    )


def _replan_line(d: Mapping[str, Any]) -> str:
    """One replan decision of Dan's (`goal_replan.decisions_for`), as a brief line."""
    head = f"{d.get('id')} on {d.get('goal')} ({str(d.get('at') or '')[:10]}): "
    if d.get("resolution") == "approve":
        return head + f"approved '{d.get('alternative')}'"
    said = f": '{d['answer']}'" if d.get("answer") else ""
    if d.get("resolution") == "keep_going":
        return head + "keep going" + said
    return head + f"Dan wrote{said or ' nothing'} (a note; it changes no plan or contract)"


def render(manifest: Mapping[str, Any], *, role: str) -> str:
    """The brief's GOALS section (`""` without an excerpt), with a reviewer's rule."""
    excerpt = str(manifest.get("goals_excerpt") or "")
    if not excerpt:
        return ""
    cut = [m for m in manifest.get("omitted_material") or ()
           if str(m).startswith("goals_excerpt:")]
    text = (f"GOALS ({doc_goals.DOC} excerpt; why this project exists and what the "
            "finished product must do):\n" + excerpt + "".join(f"\n[{m}]" for m in cut))
    replans = [d for d in manifest.get("replan_decisions") or () if isinstance(d, Mapping)]
    if replans:
        text += ("\nDAN'S REPLAN DECISIONS (binding; never ask them again):"
                 + "".join(f"\n- {_replan_line(d)}" for d in replans))
    check = manifest.get("goals_check")
    if role == "reviewer" and isinstance(check, Mapping):
        rule = _reviewer_rule(check)
        if rule:
            text += "\n" + rule
            held = [cid for cid, c in (check.get("criteria") or {}).items()
                    if isinstance(c, Mapping) and c.get("red_on_base")]
            if held:
                text += ("\nPRE-EXISTING RED: " + ", ".join(
                    f"{cid} was RED on the base too" for cid in held)
                    + " (the gate compared them): a failing test of those criteria is not "
                    "caused by this task; do not ask for rework because of it.")
    return text


@dataclass
class Read:
    """What a review's `goal_progress` and `unasked_goal_work` say, after the rules."""

    #: `{goal, impact, claimed, evidence, served[, reason][, origin][, contradiction]}` per
    #: kept entry.
    records: list[dict] = field(default_factory=list)
    #: Synthesized blocking findings, one per verified regressed goal.
    blocking: list[str] = field(default_factory=list)
    #: Served goals with `no_progress_unexpected` (recorded, never blocking).
    shortfalls: list[str] = field(default_factory=list)
    #: Targeted evidence to gather for each `unknown` goal.
    follow_ups: list[str] = field(default_factory=list)
    #: W9 out-of-scope findings for goal work the task does not ask for (R36).
    scope_findings: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    #: GDT-7 (`goal_conflicts.judge`, with `goals_check["conflicts"]`): blocking findings
    #: (a hard invariant, Dan's "keep", a conflict past the round cap), questions for Dan,
    #: and conflicts a stored decision of his covers. A regressed record in a conflict
    #: carries `conflict` and has no `blocking` text.
    conflict_findings: list[str] = field(default_factory=list)
    questions: list[dict] = field(default_factory=list)
    accepted: list[dict] = field(default_factory=list)


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _item(raw: Any) -> Optional[dict]:
    """One evidence item, normalised; `None` when it is not one of `EVIDENCE_KINDS`."""
    if not isinstance(raw, Mapping):
        return None
    kind = raw.get("kind")
    keys = {"test": ("kind", "id", "result", "criterion", "mocked"),
            "metric": ("kind", "name", "value", "baseline", "criterion"),
            "trace": ("kind", "path"), "operator": ("kind", "observation", "by"),
            "diff": ("kind", "text")}.get(kind)
    if keys is None:
        return None
    if kind == "test" and not (_text(raw.get("id")) and raw.get("result") in ("passed",
                                                                              "failed")):
        return None
    if kind == "trace" and not _text(raw.get("path")):
        return None
    if kind == "operator" and not _text(raw.get("observation")):
        return None
    if kind == "diff" and not _text(raw.get("text")):
        return None
    return {k: raw[k] for k in keys if k in raw}


def _evidence(raw: Any, goal: str, flags: list[str]) -> list[dict]:
    if isinstance(raw, str):
        return [{"kind": "diff", "text": raw.strip()}] if raw.strip() else []
    items = []
    for n, value in enumerate(raw if isinstance(raw, list) else [], 1):
        item = _item(value)
        if item is None:
            flags.append(f"goal_progress {goal}: evidence item {n} is not a test, metric, "
                         "trace, operator or diff record")
        else:
            items.append(item)
    return items


def _binding(test: str) -> str:
    return test.strip().strip("`").strip().removeprefix("pytest ").strip()


def _criterion(item: Mapping[str, Any], criteria: Mapping[str, Mapping]) -> Optional[str]:
    """The criterion an evidence item speaks for: named, or a test id equal to a binding."""
    named = item.get("criterion")
    if isinstance(named, str) and named.strip().upper() in criteria:
        return named.strip().upper()
    if item.get("kind") == "test":
        for cid, crit in criteria.items():
            if _binding(str(crit.get("test") or "")) == str(item["id"]).strip():
                return cid
    return None


def _complete_metric(item: Mapping[str, Any]) -> bool:
    return _number(item.get("value")) and _number(item.get("baseline"))


def _summary(item: Mapping[str, Any], cid: Optional[str]) -> str:
    where = f" ({cid})" if cid else ""
    if item["kind"] == "test":
        return f"test {item['id']} {item['result']}{where}"
    if item["kind"] == "metric":
        return (f"metric {item.get('name') or cid or 'value'} {item.get('value')} against "
                f"baseline {item.get('baseline')}{where}")
    if item["kind"] == "trace":
        return f"trace {item['path']}"
    if item["kind"] == "operator":
        return f"operator: {item['observation']}"
    return str(item.get("text") or "")


def _not_proof(item: Mapping[str, Any], cid: Optional[str],
               criteria: Mapping[str, Mapping], claimed: str) -> Optional[str]:
    """Why an item proves no `claimed` outcome; `None` when it does. Evidence must point
    the claimed way: `regressed` is a verified degradation (`01` §6.2)."""
    kind = item["kind"]
    crit = (criteria.get(cid) or {}) if cid else {}
    if kind == "diff":
        return "a diff shows an edit, not an outcome"
    name = item.get("name") or cid or "value"
    if kind == "metric" and not _complete_metric(item):
        return f"metric {name} has no numeric value or no baseline"
    if kind == "test" and item.get("mocked") is True:
        layer = str(crit.get("layer") or "")
        if layer != "unit":
            return (f"a mocked test is no evidence for {cid} ({layer or 'unknown layer'})"
                    if cid else "a mocked test names no unit criterion")
    if kind == "test" and claimed == "regressed":
        if item["result"] != "failed":
            return f"a {item['result']} test shows no degradation"
        if cid and crit.get("red_on_base"):
            return _red_on_base_note(cid)
        if cid and crit.get("baseline") != _GREEN:
            return (f"{cid} was {crit.get('baseline') or 'unmeasured'} at the baseline: still "
                    "failing is not a regression")
    if kind == "metric" and claimed in ("advanced", "regressed"):
        direction = crit.get("direction")
        if direction not in ("below", "above"):
            return f"metric {name} names no criterion whose threshold says which way is better"
        value, baseline = float(item["value"]), float(item["baseline"])
        better = value < baseline if direction == "below" else value > baseline
        if claimed == "regressed" and (better or value == baseline):
            return f"metric {name} {item['value']} is not worse than its baseline {item['baseline']}"
        if claimed == "advanced" and not better:
            return (f"metric {name} {item['value']} is not better than its baseline "
                    f"{item['baseline']}")
    return None


def _red_on_base_note(cid: str) -> str:
    return (f"goal_protected_red_on_base: {cid} is GREEN only by an observation since the "
            "baseline, and this task's gate found it RED on the task's tree and on the base: "
            "the target is already RED, so it is no regression of this task")


def _decide(claimed: str, goal: str, evidence: list[dict], check: Mapping[str, Any]
            ) -> tuple[str, str, list[tuple[dict, Optional[str]]]]:
    """The rule's impact, its reason and the evidence items (with their criterion) it
    rests on."""
    criteria = check.get("criteria") or {}
    linked = [(item, _criterion(item, criteria)) for item in evidence]
    failing = [(i, c) for i, c in linked if i["kind"] == "test" and i["result"] == "failed"]
    # GDT-9 review M4: RED on the base too (the gate's note) is no regression of the task.
    held = [c for _, c in failing if c and (criteria.get(c) or {}).get("red_on_base")]
    regressing = [(i, c) for i, c in failing
                  if c and c not in held and (criteria.get(c) or {}).get("baseline") == _GREEN]
    if regressing:
        return "regressed", "", regressing
    if claimed == "advanced" and failing:
        item, cid = failing[0]
        return "unknown", f"advanced contradicts a failing check: {_summary(item, cid)}", []
    kind = (check.get("contribution") or {}).get(goal)
    if claimed == "no_progress_unexpected" and kind in ("enabling", "maintenance"):
        return "unchanged_expected", f"no direct progress was promised ({kind})", []
    if claimed in _NEEDS_PROOF:
        reasons = [(i, c, _not_proof(i, c, criteria, claimed)) for i, c in linked]
        proof = [(i, c) for i, c, why in reasons if why is None]
        if not proof and claimed == "regressed" and held:
            return "unchanged_expected", _red_on_base_note(held[0]), []
        if not proof:
            why = [w for _, _, w in reasons if w] or ["no evidence was given"]
            detail = "; ".join(dict.fromkeys(why))
            return "unknown", f"{claimed} needs {_PROOF}; {detail}", []
        return claimed, "", proof
    return claimed, "", []


def _follow_up(goal: str, evidence: list[dict], check: Mapping[str, Any]) -> str:
    return f"Goal {goal} impact unknown: gather evidence: {_gather(goal, evidence, check)}"


def _gather(goal: str, evidence: list[dict], check: Mapping[str, Any]) -> str:
    """The targeted evidence to get for `goal`: the incomplete measurements, else its
    criteria's tests."""
    criteria = check.get("criteria") or {}
    metrics = []
    for item in evidence:
        if item["kind"] == "metric" and not _complete_metric(item):
            cid = _criterion(item, criteria)
            test = str((criteria.get(cid) or {}).get("test") or "") if cid else ""
            where = f" ({cid}: {test})" if cid and test else (f" ({cid})" if cid else "")
            metrics.append(f"measure {item.get('name') or cid} against its baseline{where}")
    if metrics:
        what = ", ".join(metrics)
    else:
        runs, measures = [], []
        for cid, crit in criteria.items():
            test = str(crit.get("test") or "")
            if goal not in (crit.get("goals") or ()) or not test:
                continue
            if crit.get("evidence") == "metric":
                measures.append(f"measure {cid} against its baseline ({test})")
            else:
                runs.append(f"{cid} ({test})")
        parts = (["run " + ", ".join(runs)] if runs else []) + measures
        what = ", ".join(parts) or f"name {_PROOF} for it"
    return what


def _infeasible(out: Read, record: dict, why: str, cited: list[tuple[dict, Optional[str]]],
                evidence: list[dict], check: Mapping[str, Any]) -> None:
    """GDT-8 (R44, C26): the reviewer's `infeasible` on a decided `no_progress_unexpected`
    whose proof names a criterion is a contradiction (a replan at once); its facts are the
    criterion and the evidence's kind/result/value/baseline, never the text. Otherwise it is
    flagged and becomes a targeted follow-up (R42)."""
    from maestro import goal_conflicts

    goal = record["goal"]
    proven = [(i, c) for i, c in cited if c] if record["impact"] == "no_progress_unexpected" \
        else []
    if proven:
        record["contradiction"] = {
            "criterion": proven[0][1], "why": why,
            "facts": goal_conflicts._hash(proven[0][1], [
                [i["kind"], i.get("result"), i.get("value"), i.get("baseline")]
                for i, _ in proven])}
        return
    out.flags.append(f"goal_progress {goal}: infeasible needs {_PROOF} for a criterion of a "
                     "direct no_progress_unexpected; recorded as a follow-up")
    out.follow_ups.append(f"Goal {goal} infeasibility unproven: gather evidence: "
                          f"{_gather(goal, evidence, check)} ({why})")


def _judge_conflicts(out: Read, regressed: list[tuple[dict, list[tuple[dict, Optional[str]]]]],
                     advanced: Mapping[str, list[str]], check: Mapping[str, Any]) -> None:
    """GDT-7 (R48, R51): decided `regressed` records against decided `advanced` ones. A
    regression on a goal other than one that advanced is Dan's question instead of
    GDT-6's finding; one his stored decision names is covered (or blocks, or is asked
    again) whatever advanced, and one the gate already judged on this tree is not asked
    again (`conflicts["judged"]`); a hard invariant blocks beside it; with nothing advanced
    elsewhere and no decision GDT-6's finding stands."""
    from maestro import goal_conflicts

    from maestro import acceptance

    ctx = check.get("conflicts")
    if not isinstance(ctx, Mapping) or not regressed:
        return
    per_record: list[list[goal_conflicts.Regression]] = []
    for record, cited in regressed:
        per_record.append([
            goal_conflicts.Regression(
                record["goal"], cid or "",
                value=float(item["value"]) if item["kind"] == "metric" else None,
                baseline=float(item["baseline"]) if item["kind"] == "metric" else None,
                evidence=_summary(item, cid), state=acceptance.RED)
            for item, cid in cited])
    judgement = goal_conflicts.judge([r for regs in per_record for r in regs], advanced, ctx,
                                     stage="review")
    invariants = ctx.get("invariants") or {}
    ids = {q["conflict"]["regressed"]: q["conflict"]["id"] for q in judgement.questions}
    ids.update({a["regressed"]: a["conflict"] for a in judgement.accepted})
    texts, out.blocking = list(out.blocking), []    # one per regressed record, in order
    for (record, _), regs, text in zip(regressed, per_record, texts):
        if any(r in judgement.plain or r.criterion in invariants for r in regs):
            out.blocking.append(text)
        else:
            record["conflict"] = ids.get(record["goal"], True)
    out.conflict_findings.extend(judgement.blocking)
    out.questions.extend(judgement.questions)
    out.accepted.extend(judgement.accepted)


def read(document: Mapping[str, Any], check: Optional[Mapping[str, Any]]) -> Read:
    """Read `review.json`'s `goal_progress` and `unasked_goal_work` under the reviewer's
    `goals_check`."""
    from maestro.workflows import scope_gate

    out = Read()
    if not isinstance(check, Mapping):
        return out
    raw = document.get("goal_progress")
    entries = raw if isinstance(raw, list) else []
    mode = check.get("mode")
    serves = [str(g) for g in check.get("serves") or ()]
    known = {str(g) for g in check.get("goal_ids") or ()}
    seen: set[str] = set()
    advanced: dict[str, list[str]] = {}
    regressed: list[tuple[dict, list[tuple[dict, Optional[str]]]]] = []
    for n, entry in enumerate(entries, 1):
        if not isinstance(entry, Mapping) or not isinstance(entry.get("goal"), str):
            out.flags.append(f"goal_progress entry {n} is not an object with goal and impact")
            continue
        goal = entry["goal"].strip().upper()
        given = entry.get("impact", entry.get("progress"))
        claimed = str(given or "").strip().lower().replace("-", "_")
        seen.add(goal)
        if goal not in known:
            out.flags.append(f"goal_progress {goal}: not a goal in {doc_goals.DOC}")
            continue
        legacy = claimed == "no_progress"
        if legacy:
            out.flags.append(f"goal_progress {goal}: 'no_progress' is no longer an impact "
                             "(it cannot tell enabling work from an unexpected miss); "
                             "recorded as unknown")
            claimed = "unknown"
        elif claimed not in IMPACTS:
            out.flags.append(f"goal_progress {goal}: impact {given!r} is not one of "
                             + ", ".join(IMPACTS))
            continue
        evidence = _evidence(entry.get("evidence"), goal, out.flags)
        impact, reason, cited = _decide(claimed, goal, evidence, check)
        if impact == "advanced":
            # GDT-7: progress counts against another goal's regression in any mode.
            advanced.setdefault(goal, []).extend(
                c for _, c in cited if c and c not in advanced.get(goal, ()))
        if mode != SERVED and "regressed" not in (impact, claimed):
            continue
        served = goal in serves
        record = {"goal": goal, "impact": impact, "claimed": "no_progress" if legacy
                  else claimed, "evidence": evidence, "served": served}
        if reason:
            record["reason"] = reason
        if isinstance(entry.get("origin"), str):
            record["origin"] = entry["origin"]
        out.records.append(record)
        if impact == "regressed":
            regressed.append((record, cited))
            out.blocking.append(
                f"Goal {goal} regressed: {'; '.join(_summary(i, c) for i, c in cited)}. Rule: "
                f"no change may make a project goal worse ({doc_goals.DOC}). Verify: "
                f"{goal}'s target still holds.")
        elif reason.startswith("goal_protected_red_on_base:"):
            pass    # GDT-9 review M4: a note in the record's `reason`, never blocking
        elif impact == "no_progress_unexpected" and served:
            out.shortfalls.append(goal)
        elif impact == "unknown":
            out.follow_ups.append(_follow_up(goal, evidence, check))
        if _text(entry.get("infeasible")):
            _infeasible(out, record, entry["infeasible"].strip(), cited, evidence, check)
    _judge_conflicts(out, regressed, advanced, check)
    if mode == SERVED:
        out.flags.extend(f"goal_progress: no entry for served goal {g}"
                         for g in serves if g not in seen)
    unasked = document.get("unasked_goal_work")
    for n, entry in enumerate(unasked if isinstance(unasked, list) else [], 1):
        path = entry.get("path") if isinstance(entry, Mapping) else None
        if not _text(path):
            out.flags.append(f"unasked_goal_work entry {n} names no path")
            continue
        goal = str(entry.get("goal") or "").strip().upper()
        what = str(entry.get("what") or "").strip()
        label = ": ".join(p for p in (goal, what) if p)
        out.scope_findings.append(scope_gate.out_of_scope_finding(
            path.strip(), "goal work the task does not ask for"
            + (f" ({label})" if label else "")))
    return out
