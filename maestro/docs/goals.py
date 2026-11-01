"""`docs/GOALS.md`, the product completion contract (P13 W11, B16).

Why the project exists and what the finished product must do. grill-me writes it in its
`GOALS-FORMAT.md`; Dan sometimes writes it in ChatGPT web instead, so the contract is the
format, not the skill: this parser accepts either style (numbered `##` headings, `—` for
`:`, `*` lists, bold labels, `·` joining fields, `AC1` for `AC-1`) and lists what is missing
rather than refusing. maestro-setup runs `maestro goals check` and asks Dan only for those
items.

- Format 1 (W11a): six sections. Purpose, Users, Goals (`### G<n>: name` with a `Target:`
  and a `Verified by:` naming a command, an observation by Dan or a metric), Final product,
  Non-goals, Priority. It still parses and still passes; a note points at
  `maestro goals migrate`, which prints a format-2 skeleton and invents nothing.
- Format 2 (`Format: 2`, GDT-1): a contract revision and change history, goals with
  outcome/baseline/target and the criteria that verify them, final-product scenarios
  (`S-`), acceptance criteria (`AC-`), operator checks (`OP-`), hard invariants (`INV-`),
  approved bounded trade-offs (`TO-`), an authority policy and retired ids (never reused).
  Links are read both ways; a link named on one side only counts and is a draft problem.
- Every problem has a severity: `note` (guidance, e.g. 3-7 goals), `draft` (asked while
  drafting) or `gate` (blocks the work on the ids it names at execution, R31). A value the
  contract does not give (a target, an oracle) is an open question for Dan, never a default.
- A ROADMAP task names the goals it serves with `serves: [G1]`; `serves: []` (polish,
  housekeeping) serves none, and a task without the key has not said (unclassified). It
  may name the `S-`/`AC-`/`OP-` ids it works on with `criteria: [AC-01]`.
- Two validation modes (GDT-2, R31): `diagnose()` lists every problem while drafting;
  `task_gate()` is the execution gate, holding a task on its own problems and on the gate
  problems whose ids it touches. A whole-contract gate problem holds goal work but never a
  `serves: []` task with no criteria; a draft problem never holds anything.
- A problem only Dan can answer (a target, an oracle, a behaviour, an approval) has `ask`;
  setup asks him those and fixes the rest itself (GDT-13). `CONTRACT_FIELDS` is every field
  with what its absence does, the Fields table each grill surface carries.
- Every agent role's brief carries `excerpt()` (GDT-5: role-specific, R33/R34), bounded by
  `MAX_GOALS_TOKENS`; critical lines are never cut, and the cut names each omitted part
  with its section, like the task notes' cut (`repository.context._bounded_notes`).

Requirements: `docs/agent-plans/p13-goal-driven-tdd/` (R01-R70).
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

#: The goals file, relative to the project root.
DOC = "docs/GOALS.md"
#: The goals excerpt in a brief is cut past this bound (4 characters a token).
MAX_GOALS_TOKENS = 1_000
#: How a format-1 goal's target is verified (W11b runs commands; observations go to Dan).
VERIFY_KINDS = ("command", "observation", "metric")
#: The usual number of top-level goals: guidance, never a validity limit (R03).
MIN_GOALS, MAX_GOALS = 3, 7
#: Problem severities, weakest first.
SEVERITIES = ("note", "draft", "gate")
FORMATS = (1, 2)

SECTIONS = ("Purpose", "Users", "Goals", "Final product", "Non-goals", "Priority")
#: Format 2's further sections that must be present (Operator checks, Trade-offs and
#: Retired ids may be absent).
SECTIONS_V2 = ("Acceptance criteria", "Hard invariants", "Authority", "Change history")

_GOAL_ID = re.compile(r"^G\d+$")
_ID = re.compile(r"\b(G|S|AC|OP|INV|TO)-?(\d+)\b")
_ID_ORDER = ("G", "S", "AC", "OP", "INV", "TO")
_SECTION = re.compile(r"^##\s+(.+?)\s*$")
_GOAL_HEADING = re.compile(r"^#{3,6}\s*(G\d+)\b[\s:.)\-\u2013\u2014]*(.*?)\s*$", re.I)
_ENTRY_HEADING = re.compile(
    r"^#{3,6}\s*(G|S|AC|OP)-?(\d+)\b[\s:.)\-\u2013\u2014]*(.*?)\s*$", re.I)
_FIELD = re.compile(
    r"^\s*(?:[-*+]|\d+[.)])?\s*(target|verified by|verification|verify|how (?:it is )?verified)"
    r"\s*:\s*(.*)$", re.I)
_KIND = re.compile(
    r"^(?:by\s+)?(?:dan(?:'s)?\s+)?(command|observation|metric)s?\b(?:\s+by\s+dan\b)?"
    r"[\s:\-\u2013\u2014]*(.*)$", re.I | re.S)
_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_LABEL_SEP = r"\s*(?::|\u2014|\u2013|\s-\s)\s*"
_OPEN = re.compile(r"^open questions?" + _LABEL_SEP + r"(.*)$", re.I)
_NONE = re.compile(r"^(none|n/?a|-|—)\.?$", re.I)

#: Field labels per entry kind: label as written (lower case) -> attribute.
_GOAL_LABELS = {
    "outcome": "outcome", "rationale": "rationale", "why": "rationale", "context": "rationale",
    "baseline": "baseline", "target": "target", "threshold": "target",
    "verified by": "verified", "verification": "verified", "verify": "verified",
    "how it is verified": "verified", "how verified": "verified",
    "constraints": "constraints", "invariants": "constraints",
    "depends on": "depends_on", "dependencies": "depends_on",
}
_SCENARIO_LABELS = {
    "goals": "goals", "kind": "kind", "given": "given", "when": "when", "then": "then",
    "criteria": "criteria", "acceptance criteria": "criteria",
    "operator checks": "operator_checks", "operator check": "operator_checks",
}
_CRITERION_LABELS = {
    "goals": "goals", "scenarios": "scenarios", "given": "given", "when": "when",
    "then": "then", "oracle": "oracle", "threshold": "threshold", "layer": "layer",
    "evidence": "evidence", "fixtures": "fixtures", "environment": "environment",
    "data": "data", "test": "test", "mandatory": "mandatory",
}
_OPERATOR_LABELS = {
    "goals": "goals", "scenarios": "scenarios", "instructions": "instructions",
    "expected": "expected", "expected observation": "expected", "sign-off": "sign_off",
    "sign off": "sign_off", "signoff": "sign_off", "mandatory": "mandatory",
}
_AUTHORITY_LABELS = ("autonomous", "ask dan", "retries", "stop a failed approach when",
                     "blocker", "channel")


@dataclass(frozen=True)
class Field:
    """One field of the contract, as every grill surface lists it (GDT-13, R12)."""
    #: Contract, Section, Goal, Scenario, Acceptance criterion, Operator check, Hard
    #: invariant, Trade-off, Authority or Change history.
    part: str
    #: The label as written (`render` writes it so).
    label: str
    #: Without it: `gate` (a gate problem: it blocks the work it names), `draft` (a draft
    #: problem, asked while drafting) or `no` (nothing, or a note).
    required: str
    #: What the parser takes when it is absent; `none` is nothing, never an invented value.
    default: str


_ASK = "none: an open question for Dan"
#: Every field a format-2 contract has, with the worst its absence does. A section counts
#: with its entries, so one whose ids other entries name gates through those links. The
#: Fields table on each grill surface is generated from this (`scripts/grill_surfaces.py
#: fields`), and `tests/test_grill_surfaces.py` deletes each one from the parity contract to
#: prove the table says what validation does (`01` §12: parity of required fields and
#: defaults).
CONTRACT_FIELDS: tuple[Field, ...] = (
    Field("Contract", "Format", "draft",
          "none: read as format 1, so nothing verifies it at the end"),
    Field("Contract", "Contract revision", "gate", "none: Dan approves the contract"),
    *(Field("Section", name, "draft", "none") for name in ("Purpose", "Users")),
    Field("Section", "Goals", "gate", "none: every link to a goal gates"),
    Field("Section", "Final product", "gate", "none: a contract with no scenario gates"),
    Field("Section", "Acceptance criteria", "gate", "none: every link to a criterion gates"),
    Field("Section", "Operator checks", "gate",
          "None. is a complete answer, so write it when there are none; absent, it gates "
          "only when an entry names an OP-"),
    Field("Section", "Non-goals", "draft", "none"),
    Field("Section", "Hard invariants", "gate",
          "None. is a complete answer, so write it when there are none; absent, it is draft, "
          "or gate when an entry names an INV-"),
    Field("Section", "Trade-offs", "no", "None."),
    *(Field("Section", name, "draft", "none") for name in ("Priority", "Authority")),
    Field("Section", "Change history", "gate", "none: the revision has no approval row"),
    Field("Section", "Retired ids", "no", "None."),
    Field("Goal", "Outcome", "no", "the goal's name"),
    Field("Goal", "Rationale", "no", "none"),
    Field("Goal", "Baseline", "draft", "none (not_applicable when nothing is measured)"),
    Field("Goal", "Target", "gate", _ASK),
    Field("Goal", "Verified by", "draft", "the criteria and checks naming the goal"),
    Field("Goal", "Constraints", "no", "none"),
    Field("Goal", "Depends on", "no", "none"),
    Field("Scenario", "Goals", "gate", "none"),
    Field("Scenario", "Kind", "draft", "none (normal, error or boundary)"),
    *(Field("Scenario", name, "draft", "none") for name in ("Given", "When", "Then")),
    Field("Scenario", "Criteria", "draft", "the criteria naming the scenario"),
    Field("Scenario", "Operator checks", "draft", "the checks naming the scenario"),
    Field("Acceptance criterion", "Goals", "draft", "the goals listing it"),
    Field("Acceptance criterion", "Scenarios", "draft", "the scenarios listing it"),
    *(Field("Acceptance criterion", name, "draft", "none") for name in ("Given", "When", "Then")),
    Field("Acceptance criterion", "Oracle", "gate", _ASK),
    Field("Acceptance criterion", "Threshold", "no", "none"),
    Field("Acceptance criterion", "Layer", "draft", "none (e2e, integration or unit)"),
    Field("Acceptance criterion", "Evidence", "draft", "none (test, metric or trace)"),
    *(Field("Acceptance criterion", name, "no", "none") for name in (
        "Fixtures", "Environment", "Data")),
    Field("Acceptance criterion", "Test", "no", "none: setup binds it"),
    Field("Acceptance criterion", "Mandatory", "no", "yes"),
    Field("Operator check", "Goals", "draft", "the goals listing it"),
    Field("Operator check", "Scenarios", "draft", "the scenarios listing it"),
    Field("Operator check", "Instructions", "gate", "none"),
    Field("Operator check", "Expected", "gate", "none"),
    Field("Operator check", "Sign-off", "draft", "none"),
    Field("Operator check", "Mandatory", "no", "yes"),
    Field("Hard invariant", "Checked by", "draft", "none"),
    Field("Trade-off", "Decision", "draft", "none"),
    Field("Trade-off", "Bound", "gate", "none"),
    Field("Trade-off", "Expires", "no", "none"),
    *(Field("Authority", label, "draft", "none") for label in (
        "Autonomous", "Ask Dan", "Retries", "Stop a failed approach when", "Blocker",
        "Channel")),
    Field("Change history", "Approved by", "gate", "none: Dan approves the revision"),
)


@dataclass(frozen=True)
class Problem:
    #: One of `SEVERITIES`.
    severity: str
    text: str
    #: The contract ids whose work this problem affects (empty: the whole contract).
    ids: tuple[str, ...] = ()
    #: The ROADMAP task this problem is about or holds (GDT-2); `""` for the contract.
    task: str = ""
    #: Only Dan can answer it (a value, behaviour, a link's meaning, an approval): setup asks
    #: him. `False`: an agent fixes it from what the contract already says (syntax, spelling,
    #: the other side of a named link, a typo) and says what it changed (GDT-13, R12).
    #: Advice about the problem, not part of it: problems compare and hash without it.
    ask: bool = field(default=False, compare=False)


@dataclass(frozen=True)
class Goal:
    id: str
    name: str
    target: str = ""
    #: Format 1: one of `VERIFY_KINDS`, or `""` when the file does not say which.
    verified_kind: str = ""
    #: Format 1: the command/observation/metric; format 2: the verifying ids, joined.
    verified_by: str = ""
    outcome: str = ""
    rationale: str = ""
    baseline: str = ""
    #: Format 2: the `AC-`/`OP-` ids that verify this goal, as the goal lists them.
    verified_ids: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()


@dataclass(frozen=True)
class Scenario:
    id: str
    name: str
    goals: tuple[str, ...] = ()
    #: normal | error | boundary
    kind: str = ""
    given: str = ""
    when: str = ""
    then: str = ""
    criteria: tuple[str, ...] = ()
    operator_checks: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()


@dataclass(frozen=True)
class Criterion:
    """An automatable acceptance criterion (`AC-`); `test` is filled by setup."""
    id: str
    name: str
    goals: tuple[str, ...] = ()
    scenarios: tuple[str, ...] = ()
    given: str = ""
    when: str = ""
    then: str = ""
    oracle: str = ""
    threshold: str = ""
    #: e2e | integration | unit
    layer: str = ""
    #: test | metric | trace
    evidence: str = ""
    fixtures: str = ""
    environment: str = ""
    data: str = ""
    test: str = ""
    mandatory: bool = True
    open_questions: tuple[str, ...] = ()


@dataclass(frozen=True)
class OperatorCheck:
    """A criterion only a person can verify (`OP-`); never an automated pass (R14)."""
    id: str
    name: str
    goals: tuple[str, ...] = ()
    scenarios: tuple[str, ...] = ()
    instructions: str = ""
    expected: str = ""
    sign_off: str = ""
    mandatory: bool = True
    open_questions: tuple[str, ...] = ()


@dataclass(frozen=True)
class Invariant:
    id: str
    text: str
    checked_by: tuple[str, ...] = ()


@dataclass(frozen=True)
class TradeOff:
    """An approved, bounded degradation (`TO-`); never granted by priority alone (R08)."""
    id: str
    text: str
    decision: str = ""
    bound: str = ""
    expires: str = ""


@dataclass(frozen=True)
class HistoryRow:
    rev: int
    date: str = ""
    change: str = ""
    rationale: str = ""
    impact: str = ""
    approved_by: str = ""
    supersedes: str = ""


@dataclass(frozen=True)
class Retired:
    id: str
    rev: Optional[int]
    reason: str


@dataclass(frozen=True)
class Goals:
    purpose: str
    users: tuple[str, ...]
    goals: tuple[Goal, ...]
    final_product: str
    non_goals: tuple[str, ...]
    priority: tuple[str, ...]
    #: Every problem with its severity; `problems` and `notes` are views of it.
    issues: tuple[Problem, ...]
    format: int = 1
    #: Format 2: `Contract revision: N`; `None` when the file does not say.
    revision: Optional[int] = None
    approval: str = ""
    title: str = ""
    scenarios: tuple[Scenario, ...] = ()
    criteria: tuple[Criterion, ...] = ()
    operator_checks: tuple[OperatorCheck, ...] = ()
    invariants: tuple[Invariant, ...] = ()
    trade_offs: tuple[TradeOff, ...] = ()
    #: Authority policy: lower-case label -> text (autonomous, ask dan, retries, ...).
    authority: Mapping[str, str] = field(default_factory=dict)
    history: tuple[HistoryRow, ...] = ()
    retired: Mapping[str, Retired] = field(default_factory=dict)
    #: Section-level open questions: (section, question).
    open_questions: tuple[tuple[str, str], ...] = ()

    @property
    def by_id(self) -> dict[str, Goal]:
        out: dict[str, Goal] = {}
        for goal in self.goals:
            out.setdefault(goal.id, goal)
        return out

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(self.by_id)

    @property
    def problems(self) -> tuple[str, ...]:
        """What is missing or malformed (`draft` and `gate`), one line each."""
        return tuple(p.text for p in self.issues if p.severity != "note")

    @property
    def notes(self) -> tuple[str, ...]:
        return tuple(p.text for p in self.issues if p.severity == "note")

    @property
    def gate(self) -> tuple[Problem, ...]:
        return tuple(p for p in self.issues if p.severity == "gate")

    def verifiers(self, gid: str) -> tuple[str, ...]:
        """The `AC-`/`OP-` ids verifying goal `gid`, from either side of the link."""
        known = {c.id for c in self.criteria} | {o.id for o in self.operator_checks}
        goal = self.by_id.get(gid)
        out = [i for i in (goal.verified_ids if goal else ()) if i in known]
        for entry in (*self.criteria, *self.operator_checks):
            if gid in entry.goals and entry.id not in out:
                out.append(entry.id)
        return tuple(out)


# --- small text helpers ----------------------------------------------------------------------


def _section_key(heading: str) -> Optional[str]:
    name = re.sub(r"^\d+[.)]?\s*", "", heading.strip()).lower()
    name = re.sub(r"[^a-z ]+", " ", name).strip()
    if name.startswith(("non goal", "nongoal")):
        return "Non-goals"
    if name.startswith("purpose"):
        return "Purpose"
    if name.startswith(("users", "user", "who")):
        return "Users"
    if name.startswith("goals"):
        return "Goals"
    if name.startswith("final"):
        return "Final product"
    if name.startswith("priorit"):
        return "Priority"
    if name.startswith("acceptance"):
        return "Acceptance criteria"
    if name.startswith("operator"):
        return "Operator checks"
    if name.startswith(("hard invariant", "invariant")):
        return "Hard invariants"
    if name.startswith("trade"):
        return "Trade-offs"
    if name.startswith("authority"):
        return "Authority"
    if name.startswith(("change history", "history")):
        return "Change history"
    if name.startswith("retired"):
        return "Retired ids"
    return None


def _plain(text: str) -> str:
    return text.replace("**", "").replace("__", "").strip()


def _norm_id(prefix: str, digits: str) -> str:
    prefix = prefix.upper()
    return f"{prefix}{digits}" if prefix == "G" else f"{prefix}-{digits}"


def _ids(text: str) -> tuple[str, ...]:
    out: list[str] = []
    for prefix, digits in _ID.findall(text or ""):
        nid = _norm_id(prefix, digits)
        if nid not in out:
            out.append(nid)
    return tuple(out)


def _id_key(nid: str) -> tuple[int, int, str]:
    match = re.match(r"([A-Z]+)-?(\d+)$", nid)
    if not match:
        return (len(_ID_ORDER), 0, nid)
    return (_ID_ORDER.index(match.group(1)), int(match.group(2)), nid)


def _items(body: str) -> tuple[str, ...]:
    items: list[str] = []
    for line in body.splitlines():
        match = _ITEM.match(line)
        if match:
            items.append(re.sub(r"\s+", " ", _plain(match.group(1))))
        elif line.strip() and items and line.startswith((" ", "\t")):
            items[-1] = f"{items[-1]} {_plain(line)}"
        elif line.strip():
            items.append(_plain(line))
    return tuple(i for i in items if i and not _NONE.match(i))


def _split_open(items: Iterable[str]) -> tuple[list[str], list[str]]:
    """Items, and the `Open question:` items among them."""
    kept, questions = [], []
    for item in items:
        match = _OPEN.match(item)
        (questions.append(match.group(1).strip()) if match else kept.append(item))
    return kept, questions


def _sentences(text: str) -> int:
    flat = " ".join(text.split())
    return len([s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])", flat) if s.strip()])


def _fields(body: str, labels: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
    """`- Label: value` lines (bold, `—` or `·`-joined) -> {attribute: value}, and the
    open questions; an unlabelled indented or plain line continues the previous field."""
    pattern = re.compile(
        r"^(" + "|".join(sorted((re.escape(k) for k in labels), key=len, reverse=True))
        + r")" + _LABEL_SEP + r"(.*)$", re.I)
    values: dict[str, str] = {}
    questions: list[str] = []
    current = None
    for line in body.splitlines():
        if not line.strip():
            current = None
            continue
        item = _ITEM.match(line)
        text = _plain(item.group(1) if item else line)
        matched = False
        for segment in (s.strip() for s in text.split("\u00b7")):
            label = pattern.match(segment)
            question = _OPEN.match(segment)
            if question:
                questions.append(question.group(1).strip())
                current, matched = None, True
            elif label:
                current = labels[label.group(1).lower()]
                values[current] = label.group(2).strip()
                matched = True
        if not matched and current and not item:
            values[current] = f"{values[current]} {text}".strip()
    return values, questions


def _entries(body: str, heading: re.Pattern) -> tuple[str, list[tuple[Optional[str], str, str, str]]]:
    """A section's prose before its first `###`, and its `###` entries as
    (id or None, name, heading line, body)."""
    prose: list[str] = []
    entries: list[tuple[Optional[str], str, str, str]] = []
    current: Optional[list] = None
    for line in body.splitlines():
        if line.startswith("###"):
            match = heading.match(line)
            nid = _norm_id(match.group(1), match.group(2)) if match else None
            name = _plain(match.group(3)).rstrip("*").strip() if match else ""
            current = [nid, name, line.strip(), []]
            entries.append(current)  # type: ignore[arg-type]
        elif current is not None:
            current[3].append(line)
        else:
            prose.append(line)
    return "\n".join(prose).strip(), [(e[0], e[1], e[2], "\n".join(e[3])) for e in entries]


def _goal_v1(gid: str, name: str, body: str) -> Goal:
    fields_: dict[str, str] = {}
    current = None
    for line in body.splitlines():
        match = _FIELD.match(_plain(line)) if line.strip() else None
        if match:
            current = "target" if match.group(1).lower() == "target" else "verified"
            fields_[current] = _plain(match.group(2))
        elif line.strip() and current and not _ITEM.match(line):
            fields_[current] = f"{fields_[current]} {_plain(line)}".strip()
        elif not line.strip():
            current = None
    kind, by = "", fields_.get("verified", "")
    match = _KIND.match(by)
    if match:
        kind, by = match.group(1).lower(), match.group(2).strip()
    return Goal(gid, _plain(name).rstrip("*").strip(), fields_.get("target", ""), kind, by)


def _goal_v2(gid: str, name: str, body: str) -> Goal:
    values, questions = _fields(body, _GOAL_LABELS)
    verified = _ids(values.get("verified", ""))
    return Goal(
        gid, _plain(name).rstrip("*").strip(), values.get("target", ""), "",
        ", ".join(verified), outcome=values.get("outcome", ""),
        rationale=values.get("rationale", ""), baseline=values.get("baseline", ""),
        verified_ids=verified, constraints=_ids(values.get("constraints", "")),
        depends_on=_ids(values.get("depends_on", "")), open_questions=tuple(questions))


def _mandatory(value: str) -> bool:
    return value.strip().lower().rstrip(".") not in ("no", "false", "optional")


# --- parse -----------------------------------------------------------------------------------


def parse(text: str) -> Goals:
    """Parse a GOALS.md (format 1 or 2) in the skill's or a free-hand (ChatGPT) style."""
    text = text.lstrip("\N{BYTE ORDER MARK}")  # a Google-Docs export starts with one
    sections: dict[str, str] = {}
    header: list[str] = []
    current = None
    started = False
    lines: list[str] = []
    for line in text.splitlines() + ["## (end)"]:
        match = _SECTION.match(line)
        if match:
            if current and current not in sections:
                sections[current] = "\n".join(lines).strip()
            current, lines, started = _section_key(match.group(1)), [], True
        elif current:
            lines.append(line)
        elif not started:
            header.append(line)

    title, fmt, revision, approval = "", 1, None, ""
    for line in header:
        plain = _plain(line)
        if re.match(r"^#\s", plain):
            title = re.sub(r"^#\s*goals?\b\s*[:\-\u2013\u2014]?\s*", "", plain, flags=re.I)
            title = title.lstrip("# ").strip() if title != plain else ""
        elif m := re.match(r"^format(?:\s+version)?" + _LABEL_SEP + r"(\d+)", plain, re.I):
            fmt = int(m.group(1))
        elif m := re.match(r"^contract revision" + _LABEL_SEP + r"(\d+)\s*(?:\((.*)\))?",
                           plain, re.I):
            revision, approval = int(m.group(1)), (m.group(2) or "").strip()

    issues: list[Problem] = []

    def add(severity: str, text_: str, *ids: str, ask: bool = False) -> None:
        issues.append(Problem(severity, text_, tuple(dict.fromkeys(ids)), ask=ask))

    if fmt not in FORMATS:
        add("gate", f"unknown format {fmt}; this Maestro reads formats 1 and 2")
    v2 = fmt == 2
    required = SECTIONS + (SECTIONS_V2 if v2 else ())
    for name in required:
        if name not in sections:
            add("draft", f"missing section: {name}", ask=True)
        elif not sections[name]:
            add("draft", f"empty section: {name}", ask=True)

    purpose = " ".join(_plain(sections.get("Purpose", "")).split())
    count = _sentences(purpose)
    if purpose and count > 2:
        add("note", f"purpose has {count} sentences; the format asks for two")

    section_questions: list[tuple[str, str]] = []

    def section_items(name: str) -> tuple[str, ...]:
        kept, questions = _split_open(_items(sections.get(name, "")))
        section_questions.extend((name, q) for q in questions)
        return tuple(kept)

    users = section_items("Users")
    non_goals = section_items("Non-goals")

    goal_list: list[Goal] = []
    heading = None
    body: list[str] = []
    for line in sections.get("Goals", "").splitlines() + ["### (end)"]:
        match = _GOAL_HEADING.match(line)
        if line.startswith("###"):
            if heading:
                build = _goal_v2 if v2 else _goal_v1
                goal_list.append(build(heading[0], heading[1], "\n".join(body)))
            heading = (match.group(1).upper(), match.group(2)) if match else None
            body = []
            if not match and line != "### (end)":
                add("gate", f"goal heading {line.strip()!r} has no id like G1")
        elif heading:
            body.append(line)

    seen: set[str] = set()
    for goal in goal_list:
        if goal.id in seen:
            add("gate", f"duplicate goal id {goal.id}", goal.id)
            continue
        seen.add(goal.id)
        if not goal.name:
            add("draft", f"{goal.id}: no name", goal.id)
        if v2:
            continue
        if not goal.target:
            add("gate", f"{goal.id}: no measurable target", goal.id, ask=True)
        if not goal.verified_kind and not goal.verified_by:
            add("gate", f"{goal.id}: no verification (command, observation or metric)", goal.id,
                ask=True)
        elif not goal.verified_kind:
            add("draft", f"{goal.id}: verification must say command, observation or metric",
                goal.id)
        elif goal.verified_kind == "command" and not goal.verified_by:
            add("gate", f"{goal.id}: verified by command, but names no command", goal.id)
    if "Goals" in sections and not seen:
        add("gate", "no goals", ask=True)
    elif seen and not MIN_GOALS <= len(seen) <= MAX_GOALS:
        add("note", f"{len(seen)} goals; {MIN_GOALS}-{MAX_GOALS} is the usual range "
                    "(guidance, not a limit)")

    priority: list[str] = []
    for gid in re.findall(r"\bG\d+\b", sections.get("Priority", ""), re.I):
        if gid.upper() not in priority:
            priority.append(gid.upper())
    if "Priority" in sections and sections["Priority"]:
        for g in priority:
            if g not in seen:
                add("gate", f"priority names unknown goal {g}", g)
        for g in sorted(seen, key=_order):
            if g not in priority:
                add("draft", f"priority does not rank {g}", g, ask=True)

    doc = Goals(
        purpose=purpose, users=users, goals=tuple(goal_list),
        final_product=sections.get("Final product", ""), non_goals=non_goals,
        priority=tuple(priority), issues=(), format=fmt, revision=revision,
        approval=approval, title=title)
    if not v2:
        add("note", "format 1 (six sections): no scenarios, acceptance criteria or operator "
                    "checks yet; `maestro goals migrate` prints a format-2 skeleton")
        return replace(doc, issues=tuple(issues))
    return _parse_v2(doc, sections, issues, add, section_items, section_questions)


def _parse_v2(doc: Goals, sections: Mapping[str, str], issues: list[Problem], add,
              section_items, section_questions: list[tuple[str, str]]) -> Goals:
    defined: dict[str, str] = {g.id: "goal" for g in doc.goals}

    def define(nid: Optional[str], line: str, expected: str) -> bool:
        if nid is None or not nid.startswith(expected):
            add("gate", f"heading {line!r} has no id like {expected}1")
            return False
        if nid in defined:
            add("gate", f"duplicate id {nid}", nid)
            return False
        defined[nid] = expected
        return True

    prose, entries = _entries(sections.get("Final product", ""), _ENTRY_HEADING)
    prose_lines = []
    for line in prose.splitlines():
        item = _ITEM.match(line)
        question = _OPEN.match(_plain(item.group(1) if item else line))
        if question:
            section_questions.append(("Final product", question.group(1).strip()))
        else:
            prose_lines.append(line)
    final_product = "\n".join(prose_lines).strip()

    scenarios: list[Scenario] = []
    for nid, name, line, body in entries:
        if not define(nid, line, "S"):
            continue
        v, q = _fields(body, _SCENARIO_LABELS)
        scenarios.append(Scenario(
            nid, name, _ids(v.get("goals", "")), v.get("kind", "").lower(), v.get("given", ""),
            v.get("when", ""), v.get("then", ""), _ids(v.get("criteria", "")),
            _ids(v.get("operator_checks", "")), tuple(q)))

    criteria: list[Criterion] = []
    for nid, name, line, body in _entries(sections.get("Acceptance criteria", ""),
                                          _ENTRY_HEADING)[1]:
        if not define(nid, line, "AC"):
            continue
        v, q = _fields(body, _CRITERION_LABELS)
        criteria.append(Criterion(
            nid, name, _ids(v.get("goals", "")), _ids(v.get("scenarios", "")),
            v.get("given", ""), v.get("when", ""), v.get("then", ""), v.get("oracle", ""),
            v.get("threshold", ""), v.get("layer", "").lower(), v.get("evidence", "").lower(),
            v.get("fixtures", ""), v.get("environment", ""), v.get("data", ""),
            v.get("test", "").strip("` "), _mandatory(v.get("mandatory", "")), tuple(q)))

    operators: list[OperatorCheck] = []
    for nid, name, line, body in _entries(sections.get("Operator checks", ""),
                                          _ENTRY_HEADING)[1]:
        if not define(nid, line, "OP"):
            continue
        v, q = _fields(body, _OPERATOR_LABELS)
        operators.append(OperatorCheck(
            nid, name, _ids(v.get("goals", "")), _ids(v.get("scenarios", "")),
            v.get("instructions", ""), v.get("expected", ""), v.get("sign_off", ""),
            _mandatory(v.get("mandatory", "")), tuple(q)))

    invariants: list[Invariant] = []
    for item in section_items("Hard invariants"):
        m = re.match(r"^(INV)-?(\d+)" + _LABEL_SEP + r"(.*)$", item, re.I)
        if not m:
            add("gate", f"hard invariant {item!r} has no id like INV-1")
            continue
        nid, rest = _norm_id(m.group(1), m.group(2)), m.group(3).strip()
        checked = re.search(r"\s*\((?:checked|verified) by ([^)]*)\)\s*$", rest, re.I)
        if checked:
            rest = rest[:checked.start()].strip()
        if define(nid, item, "INV"):
            invariants.append(Invariant(nid, rest, _ids(checked.group(1)) if checked else ()))

    trade_offs: list[TradeOff] = []
    for item in section_items("Trade-offs"):
        m = re.match(r"^(TO)-?(\d+)" + _LABEL_SEP + r"(.*)$", item, re.I)
        if not m:
            add("gate", f"trade-off {item!r} has no id like TO-1")
            continue
        nid, text_parts, extra = _norm_id(m.group(1), m.group(2)), [], {}
        for part in (p.strip() for p in m.group(3).split(";")):
            key = re.match(r"^(decision|bound|expires)\b[\s:]*(.*)$", part, re.I)
            if key and text_parts:
                extra[key.group(1).lower()] = key.group(2).strip()
            else:
                text_parts.append(part)
        if define(nid, item, "TO"):
            trade_offs.append(TradeOff(nid, "; ".join(text_parts), extra.get("decision", ""),
                                       extra.get("bound", ""), extra.get("expires", "")))

    authority_labels = {k: k for k in _AUTHORITY_LABELS}
    authority, questions = _fields(sections.get("Authority", ""), authority_labels)
    section_questions.extend(("Authority", q) for q in questions)

    history: list[HistoryRow] = []
    for line in sections.get("Change history", "").splitlines():
        cells = [_plain(c) for c in line.strip().strip("|").split("|")]
        if not line.strip().startswith("|") or not cells or not cells[0].isdigit():
            continue
        cells += [""] * (7 - len(cells))
        history.append(HistoryRow(int(cells[0]), *cells[1:7]))

    retired: dict[str, Retired] = {}
    for item in section_items("Retired ids"):
        m = re.match(r"^((?:G|S|AC|OP|INV|TO)-?\d+)\s*(?:\(\s*rev(?:ision)?\s*(\d+)\s*\))?"
                     + _LABEL_SEP + r"?(.*)$", item, re.I)
        if m:
            nid = _ids(m.group(1))[0]
            retired[nid] = Retired(nid, int(m.group(2)) if m.group(2) else None,
                                   m.group(3).strip())

    doc = replace(
        doc, final_product=final_product, scenarios=tuple(scenarios),
        criteria=tuple(criteria), operator_checks=tuple(operators),
        invariants=tuple(invariants), trade_offs=tuple(trade_offs), authority=authority,
        history=tuple(history), retired=retired, open_questions=tuple(section_questions))
    _validate_v2(doc, defined, add)
    return replace(doc, issues=tuple(issues))


def _validate_v2(doc: Goals, defined: Mapping[str, str], add) -> None:
    by_ac = {c.id: c for c in doc.criteria}
    by_op = {o.id: o for o in doc.operator_checks}
    by_s = {s.id: s for s in doc.scenarios}

    for nid in defined:
        if nid in doc.retired:
            rev = doc.retired[nid].rev
            add("gate", f"{nid} is retired{f' (rev {rev})' if rev else ''} and cannot be reused",
                nid)

    def refs(owner: str, ids: Iterable[str], *affected: str) -> None:
        for ref in ids:
            if ref not in defined:
                add("gate", f"{owner} names unknown id {ref}", owner, *affected)

    for g in doc.goals:
        refs(g.id, (*g.verified_ids, *g.constraints, *g.depends_on))
    for s in doc.scenarios:
        refs(s.id, (*s.goals, *s.criteria, *s.operator_checks), *s.goals)
    for c in (*doc.criteria, *doc.operator_checks):
        refs(c.id, (*c.goals, *c.scenarios), *c.goals)
    for inv in doc.invariants:
        refs(inv.id, inv.checked_by)

    # --- the contract revision (R11, R67) ---
    if doc.revision is None:
        add("gate", "no contract revision", ask=True)
    else:
        rows = {h.rev: h for h in doc.history}
        if doc.revision not in rows:
            add("gate", f"contract revision {doc.revision} has no change-history row", ask=True)
        elif not rows[doc.revision].approved_by.strip(" —-"):
            add("gate", f"contract revision {doc.revision} is not approved "
                        "(no approver in its change-history row)", ask=True)
        for rev in sorted(r for r in rows if r > doc.revision):
            add("gate", f"change-history row for revision {rev} is newer than the contract "
                        f"revision {doc.revision}")

    # --- goals (R05) ---
    for g in doc.goals:
        if not g.target:
            add("gate", f"{g.id}: no target (ask Dan; never invented)", g.id, ask=True)
        if not g.baseline:
            add("draft", f"{g.id}: no baseline (a value, or not_applicable)", g.id, ask=True)
        if not g.outcome:
            add("note", f"{g.id}: no outcome line (the name stands in)", g.id)
        if not g.rationale:
            add("note", f"{g.id}: no rationale", g.id)
        if not doc.verifiers(g.id):
            add("gate", f"{g.id} has no acceptance criterion or operator check", g.id, ask=True)
        if not any(g.id in s.goals for s in doc.scenarios) and doc.scenarios:
            add("draft", f"{g.id} appears in no final-product scenario", g.id, ask=True)
        for ref in g.verified_ids:
            entry = by_ac.get(ref) or by_op.get(ref)
            if entry is not None and g.id not in entry.goals:
                add("draft", f"{g.id} lists {ref} but {ref} does not name {g.id}", g.id, ref)
    for entry in (*doc.criteria, *doc.operator_checks):
        for gid in entry.goals:
            goal = doc.by_id.get(gid)
            if goal is not None and entry.id not in goal.verified_ids:
                add("draft", f"{entry.id} names {gid} but {gid} does not list {entry.id}",
                    gid, entry.id)

    # --- scenarios (R06) ---
    if not doc.scenarios:
        add("gate", "final product names no scenario", ask=True)
    elif not any(s.kind == "error" for s in doc.scenarios):
        add("note", "no error scenario (what the user sees when something fails)")
    for s in doc.scenarios:
        if not s.goals:
            add("gate", f"{s.id} names no goal", s.id, ask=True)
        checks = set(s.criteria) | set(s.operator_checks) | {
            c.id for c in (*doc.criteria, *doc.operator_checks) if s.id in c.scenarios}
        if not checks:
            add("gate", f"{s.id} has no acceptance criterion or operator check", s.id, *s.goals,
                ask=True)
        for part in ("given", "when", "then"):
            if not getattr(s, part):
                add("draft", f"{s.id}: no {part.capitalize()}", s.id, ask=True)
        if s.kind not in ("normal", "error", "boundary"):
            add("draft", f"{s.id}: kind must be normal, error or boundary", s.id)
        for ref in (*s.criteria, *s.operator_checks):
            entry = by_ac.get(ref) or by_op.get(ref)
            if entry is not None and s.id not in entry.scenarios:
                add("draft", f"{s.id} lists {ref} but {ref} does not name {s.id}", s.id, ref)

    def linked_goals(entry) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            (*entry.goals, *(g.id for g in doc.goals if entry.id in g.verified_ids))))

    def linked_scenarios(entry) -> tuple[str, ...]:
        listed = (s.id for s in doc.scenarios
                  if entry.id in s.criteria or entry.id in s.operator_checks)
        return tuple(dict.fromkeys((*entry.scenarios, *listed)))

    # --- acceptance criteria and operator checks (R05, R13, R14) ---
    for c in doc.criteria:
        goals_ = linked_goals(c)
        if not goals_ and not linked_scenarios(c):
            add("gate", f"{c.id} names no goal or scenario", c.id, ask=True)
        if not c.oracle:
            add("gate", f"{c.id}: no oracle (ask Dan; never invented)", c.id, *goals_, ask=True)
        for part in ("given", "when", "then"):
            if not getattr(c, part):
                add("draft", f"{c.id}: no {part.capitalize()}", c.id, ask=True)
        if c.layer not in ("e2e", "integration", "unit"):
            add("draft", f"{c.id}: layer must be e2e, integration or unit", c.id)
        if c.evidence not in ("test", "metric", "trace"):
            add("draft", f"{c.id}: evidence must be test, metric or trace", c.id)
        for scenario in c.scenarios:
            s = by_s.get(scenario)
            if s is not None and c.id not in s.criteria:
                add("draft", f"{c.id} names {scenario} but {scenario} does not list {c.id}",
                    c.id, scenario)
    for o in doc.operator_checks:
        goals_ = linked_goals(o)
        if not goals_ and not linked_scenarios(o):
            add("gate", f"{o.id} names no goal or scenario", o.id, ask=True)
        if not o.instructions:
            add("gate", f"{o.id} has no instructions", o.id, *goals_, ask=True)
        if not o.expected:
            add("gate", f"{o.id} has no expected observation", o.id, *goals_, ask=True)
        if not o.sign_off:
            add("draft", f"{o.id}: no sign-off (who signs it off)", o.id, ask=True)
        for scenario in o.scenarios:
            s = by_s.get(scenario)
            if s is not None and o.id not in s.operator_checks:
                add("draft", f"{o.id} names {scenario} but {scenario} does not list {o.id}",
                    o.id, scenario)

    # --- invariants and trade-offs (R07, R51, R50) ---
    for inv in doc.invariants:
        if not inv.checked_by:
            add("draft", f"{inv.id}: names no acceptance criterion that checks it", inv.id,
                ask=True)
    for to in doc.trade_offs:
        if not to.bound:
            add("gate", f"{to.id}: no bound (an approved trade-off is bounded)", to.id, ask=True)
        if not to.decision:
            add("draft", f"{to.id}: names no approving decision", to.id, ask=True)

    # --- authority (R09) ---
    if doc.authority:
        for label in _AUTHORITY_LABELS:
            if not doc.authority.get(label):
                add("draft", f"authority does not say: {label}", ask=True)

    # --- open questions: block only what they name (R05, R66) ---
    for g in doc.goals:
        for q in g.open_questions:
            add("gate", f"{g.id}: open question for Dan: {q}", g.id, ask=True)
    for s in doc.scenarios:
        for q in s.open_questions:
            add("gate", f"{s.id}: open question for Dan: {q}", s.id, *s.goals, ask=True)
    for entry in (*doc.criteria, *doc.operator_checks):
        for q in entry.open_questions:
            add("gate", f"{entry.id}: open question for Dan: {q}", entry.id,
                *linked_goals(entry), ask=True)
    for section, q in doc.open_questions:
        add("gate", f"{section}: open question for Dan: {q}", ask=True)

    # --- conflicts written into the contract, and what Autonomous may grant (GDT-7) ---
    from maestro import goal_authority, goal_conflicts  # lazy: both import this module

    for problem in (*goal_conflicts.contract_problems(doc),
                    *goal_authority.contract_problems(doc)):
        add(problem.severity, problem.text, *problem.ids, ask=problem.ask)


def _order(gid: str) -> int:
    return int(gid[1:]) if gid[1:].isdigit() else 0


def path_for(target: Path) -> Path:
    """`target` itself when it is a file, else the project's `docs/GOALS.md`."""
    target = Path(target)
    return target if target.is_file() else target / DOC


def load(repo: Path) -> Optional[Goals]:
    """The project's parsed goals, or `None` when it has no `docs/GOALS.md`."""
    path = Path(repo) / DOC
    if not path.is_file():
        return None
    return parse(path.read_text(encoding="utf-8"))


# --- render and migrate ----------------------------------------------------------------------


def _bullets(pairs: Iterable[tuple[str, Any]]) -> list[str]:
    out = []
    for label, value in pairs:
        if isinstance(value, tuple):
            value = ", ".join(value)
        if value:
            out.append(f"- {label}: {value}")
    return out


def _questions(questions: Iterable[str]) -> list[str]:
    return [f"- Open question: {q}" for q in questions]


def render(doc: Goals) -> str:
    """`doc` as a format-2 GOALS.md; `parse(render(doc))` gives `doc` back."""
    def section_questions(name: str) -> list[str]:
        return _questions(q for s, q in doc.open_questions if s == name)

    out = [f"# Goals: {doc.title}" if doc.title else "# Goals", "", "Format: 2"]
    if doc.revision is not None:
        out.append(f"Contract revision: {doc.revision}"
                   + (f" ({doc.approval})" if doc.approval else ""))
    out += ["", "## Purpose", "", doc.purpose, "", "## Users", ""]
    out += [f"- {u}" for u in doc.users] + section_questions("Users")
    out += ["", "## Goals", ""]
    for g in doc.goals:
        out += [f"### {g.id}: {g.name}", ""]
        out += _bullets((("Outcome", g.outcome), ("Rationale", g.rationale),
                         ("Baseline", g.baseline), ("Target", g.target),
                         ("Verified by", g.verified_ids), ("Constraints", g.constraints),
                         ("Depends on", g.depends_on)))
        out += _questions(g.open_questions) + [""]
    out += ["## Final product", "", doc.final_product, ""]
    if section_questions("Final product"):
        out += section_questions("Final product") + [""]
    for s in doc.scenarios:
        out += [f"### {s.id}: {s.name}", ""]
        out += _bullets((("Goals", s.goals), ("Kind", s.kind), ("Given", s.given),
                         ("When", s.when), ("Then", s.then), ("Criteria", s.criteria),
                         ("Operator checks", s.operator_checks)))
        out += _questions(s.open_questions) + [""]
    out += ["## Acceptance criteria", ""]
    for c in doc.criteria:
        out += [f"### {c.id}: {c.name}", ""]
        out += _bullets((("Goals", c.goals), ("Scenarios", c.scenarios), ("Given", c.given),
                         ("When", c.when), ("Then", c.then), ("Oracle", c.oracle),
                         ("Threshold", c.threshold), ("Layer", c.layer),
                         ("Evidence", c.evidence), ("Fixtures", c.fixtures),
                         ("Environment", c.environment), ("Data", c.data),
                         ("Test", f"`{c.test}`" if c.test else ""),
                         ("Mandatory", "" if c.mandatory else "no")))
        out += _questions(c.open_questions) + [""]
    out += ["## Operator checks", ""]
    for o in doc.operator_checks:
        out += [f"### {o.id}: {o.name}", ""]
        out += _bullets((("Goals", o.goals), ("Scenarios", o.scenarios),
                         ("Instructions", o.instructions), ("Expected", o.expected),
                         ("Sign-off", o.sign_off), ("Mandatory", "" if o.mandatory else "no")))
        out += _questions(o.open_questions) + [""]
    if not doc.operator_checks:
        out += ["None.", ""]
    out += ["## Non-goals", ""] + [f"- {n}" for n in doc.non_goals]
    out += section_questions("Non-goals") + ["", "## Hard invariants", ""]
    out += [f"- {i.id}: {i.text}" + (f" (checked by {', '.join(i.checked_by)})"
                                     if i.checked_by else "") for i in doc.invariants]
    out += section_questions("Hard invariants")
    if not doc.invariants and not section_questions("Hard invariants"):
        out.append("None.")
    out += ["", "## Trade-offs", ""]
    for t in doc.trade_offs:
        extra = "".join(f"; {k} {v}" for k, v in (("decision", t.decision), ("bound", t.bound),
                                                  ("expires", t.expires)) if v)
        out.append(f"- {t.id}: {t.text}{extra}")
    out += section_questions("Trade-offs")
    if not doc.trade_offs and not section_questions("Trade-offs"):
        out.append("None.")
    out += ["", "## Priority", ""] + [f"{n}. {g}" for n, g in enumerate(doc.priority, 1)]
    out += ["", "## Authority", ""]
    out += [f"- {_AUTHORITY_TITLES.get(k, k.capitalize())}: {v}"
            for k, v in doc.authority.items()]
    out += section_questions("Authority")
    out += ["", "## Change history", "",
            "| Rev | Date | Change | Rationale | Impact | Approved by | Supersedes |",
            "|---|---|---|---|---|---|---|"]
    out += ["| " + " | ".join([str(h.rev), h.date, h.change, h.rationale, h.impact,
                                h.approved_by, h.supersedes]) + " |" for h in doc.history]
    if doc.retired:
        out += ["", "## Retired ids", ""]
        out += [f"- {r.id}" + (f" (rev {r.rev})" if r.rev is not None else "") + f": {r.reason}"
                for r in doc.retired.values()]
    return "\n".join(out).rstrip() + "\n"


_AUTHORITY_TITLES = {"ask dan": "Ask Dan"}


def migrate(doc: Goals, *, today: Optional[str] = None) -> str:
    """A format-2 skeleton of a format-1 contract: every id and text kept, each goal's
    verification moved into a criterion (command, metric) or an operator check
    (observation), and what format 1 cannot say left as `Open question:` for Dan."""
    if doc.format != 1:
        raise ValueError(f"already format {doc.format}; nothing to migrate")
    goals_out: list[Goal] = []
    criteria: list[Criterion] = []
    operators: list[OperatorCheck] = []
    for g in doc.goals:
        questions = () if g.target else ("the measurable target",)
        verifier = ""
        if g.verified_kind == "observation":
            verifier = f"OP-{len(operators) + 1:02d}"
            operators.append(OperatorCheck(verifier, g.name, (g.id,), (), g.verified_by,
                                           g.target, "Dan"))
        elif g.verified_kind in ("command", "metric") or g.verified_by:
            verifier = f"AC-{len(criteria) + 1:02d}"
            command = g.verified_kind == "command"
            criteria.append(Criterion(
                verifier, g.name, (g.id,), oracle=g.target if command else g.verified_by,
                evidence="test" if command else ("metric" if g.verified_kind else ""),
                test=g.verified_by.strip("` ") if command else "",
                open_questions=() if g.verified_kind else (
                    "is this checked by a command, a metric or Dan's observation?",)))
        goals_out.append(Goal(g.id, g.name, g.target, "", verifier,
                              verified_ids=(verifier,) if verifier else (),
                              open_questions=questions))
    v2 = replace(
        doc, format=2, revision=1, approval="migrated from format 1; approve with Dan",
        goals=tuple(goals_out), criteria=tuple(criteria), operator_checks=tuple(operators),
        history=(HistoryRow(1, today or _dt.date.today().isoformat(), "migrated from format 1",
                            "W11 contract format 2", "all", "", "format 1 file"),),
        open_questions=(
            ("Final product", "ask Dan: the scenarios this walkthrough implies (normal, error, "
                              "boundary), each with its goals and criteria"),
            ("Hard invariants", "ask Dan: the properties that must never regress (or None)"),
            ("Authority", "ask Dan: what agents decide alone, retries, when to stop a failed "
                          "approach, what counts as a blocker, the channel"),
        ))
    return render(v2)


# --- revisions -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Changes:
    """What changed between two parses of one contract (R11, R67)."""
    changed: tuple[str, ...]
    added: tuple[str, ...]
    removed: tuple[str, ...]
    #: Ids whose earlier proof no longer counts: each changed or removed id and what it
    #: links to one step away (a criterion's goals and scenarios, a goal's verifiers ...).
    stale: tuple[str, ...]
    problems: tuple[Problem, ...]


#: The fields whose change changes the contract, so needs a new revision (a criterion's
#: `test` command is filled by setup and guarded by review, not by revision). Every grill
#: surface states the rest as what may change at the same revision
#: (`tests/test_grill_surfaces.py`). Not `CONTRACT_FIELDS`, the Fields table.
_REVISION_FIELDS = {
    Goal: ("outcome", "baseline", "target", "verified_ids", "constraints", "depends_on"),
    Scenario: ("goals", "given", "when", "then", "criteria", "operator_checks"),
    Criterion: ("goals", "scenarios", "given", "when", "then", "oracle", "threshold",
                "mandatory"),
    OperatorCheck: ("goals", "scenarios", "instructions", "expected", "mandatory"),
    Invariant: ("text", "checked_by"),
    TradeOff: ("text", "bound", "expires"),
}


def _entities(doc: Goals) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for entry in (*doc.goals, *doc.scenarios, *doc.criteria, *doc.operator_checks,
                  *doc.invariants, *doc.trade_offs):
        out.setdefault(entry.id, entry)
    return out


def _neighbours(doc: Goals, nid: str) -> set[str]:
    near: set[str] = set()
    for entry in _entities(doc).values():
        own = {f.name: getattr(entry, f.name) for f in fields(entry)}
        linked = {i for v in own.values() if isinstance(v, tuple) for i in v
                  if isinstance(i, str)}
        if entry.id == nid:
            near |= linked
        elif nid in linked:
            near.add(entry.id)
    return near


def changes(old: Goals, new: Goals) -> Changes:
    """The contract changes from `old` to `new`, what they make stale, and the illegal
    ones: a change without a new revision, a removal without retiring, a revision going
    back."""
    before, after = _entities(old), _entities(new)
    changed = []
    for nid, entry in after.items():
        prior = before.get(nid)
        if prior is not None and type(prior) is type(entry):
            names = _REVISION_FIELDS[type(entry)]
            if any(getattr(prior, n) != getattr(entry, n) for n in names):
                changed.append(nid)
        elif prior is not None:
            changed.append(nid)
    added = [n for n in after if n not in before]
    removed = [n for n in before if n not in after]
    stale: set[str] = set()
    for nid in (*changed, *removed):
        stale.add(nid)
        stale |= _neighbours(old, nid) | _neighbours(new, nid)
    problems: list[Problem] = []
    if old.revision is not None and new.revision is not None and new.revision < old.revision:
        problems.append(Problem("gate", f"contract revision went back from {old.revision} "
                                        f"to {new.revision}"))
    if old.revision == new.revision:
        problems += [Problem("gate", f"{n} changed without a new contract revision", (n,))
                     for n in sorted(changed, key=_id_key)]
    problems += [Problem("gate", f"{n} removed without being retired", (n,))
                 for n in sorted(removed, key=_id_key) if n not in new.retired]
    key = _id_key
    return Changes(tuple(sorted(changed, key=key)), tuple(sorted(added, key=key)),
                   tuple(sorted(removed, key=key)), tuple(sorted(stale, key=key)),
                   tuple(problems))


# --- serves ---------------------------------------------------------------------------------


def normalise_serves(raw: Any) -> tuple[tuple[str, ...], list[str]]:
    """A ROADMAP `serves:` value as goal ids, with what in it is not a goal id."""
    return _normalise_ids(raw, "serves", lambda e: e.upper() if _GOAL_ID.match(e.upper())
                          else None, "a goal id")


_LINK_ID = re.compile(r"^(S|AC|OP)-?(\d+)$", re.I)


def normalise_criteria(raw: Any) -> tuple[tuple[str, ...], list[str]]:
    """A ROADMAP `criteria:` value (GDT-2; GDT-4 documents the key) as `S-`/`AC-`/`OP-`
    ids, with what in it is not one."""
    def norm(entry: str) -> Optional[str]:
        match = _LINK_ID.match(entry)
        return _norm_id(match.group(1), match.group(2)) if match else None
    return _normalise_ids(raw, "criteria", norm, "an S-, AC- or OP- id")


def _normalise_ids(raw: Any, key: str, norm, what: str) -> tuple[tuple[str, ...], list[str]]:
    if raw is None:
        return (), []
    if isinstance(raw, str):
        entries: list[Any] = re.split(r"[,\s]+", raw)
    else:
        entries = list(raw) if isinstance(raw, (list, tuple)) else [raw]
    ids: list[str] = []
    problems: list[str] = []
    for entry in entries:
        if isinstance(entry, str) and not entry.strip():
            continue
        nid = norm(entry.strip()) if isinstance(entry, str) else None
        if nid is None:
            problems.append(f"{key} entry {entry!r} is not {what}")
        elif nid not in ids:
            ids.append(nid)
    return tuple(ids), problems


def task_serves(task: Mapping[str, Any]) -> Optional[tuple[str, ...]]:
    """The goals a task serves; `()` for `serves: []`, `None` when the task does not say."""
    if "serves" not in task:
        return None
    return normalise_serves(task.get("serves"))[0]


# --- contribution and task evidence (GDT-4) -------------------------------------------------

#: How a task contributes to a goal it serves (`01` §4): `direct` should move the goal's
#: criteria, `enabling` is a verifiable prerequisite (a harness, fixtures, a benchmark) and
#: `maintenance` keeps reliability or quality without advancing the goal.
CONTRIBUTIONS = ("direct", "enabling", "maintenance")


def _contribution(task: Mapping[str, Any]) -> tuple[dict[str, str], list[str]]:
    """A task's `contribution:` per served goal, and what is wrong with how it says it.
    One value applies to every served goal; a mapping names goals one by one."""
    served = task_serves(task) or ()
    raw = task.get("contribution")
    if raw is None:
        return {}, []

    def kind(value: Any) -> Optional[str]:
        value = value.strip().lower() if isinstance(value, str) else None
        return value if value in CONTRIBUTIONS else None

    if isinstance(raw, Mapping):
        out: dict[str, str] = {}
        problems: list[str] = []
        for key, value in raw.items():
            gid = str(key).strip().upper()
            if kind(value) is None:
                problems.append(f"contribution {value!r} for {gid} is not direct, enabling "
                                "or maintenance")
            elif gid not in served:
                problems.append(f"contribution names {gid}, which it does not serve")
            else:
                out[gid] = kind(value)
        return out, problems
    if kind(raw) is None:
        return {}, [f"contribution {raw!r} is not direct, enabling or maintenance"]
    if "serves" in task and not served and kind(raw) != "maintenance":
        return {}, ["serves no goal, so its contribution can only be maintenance"]
    return {g: kind(raw) for g in served}, []


def task_contribution(task: Mapping[str, Any]) -> dict[str, str]:
    """How a task contributes to each goal it serves; `{}` when it does not say."""
    return _contribution(task)[0]


def task_tests(task: Mapping[str, Any]) -> Optional[tuple[str, ...]]:
    """A task's `tests:` (the test ids or commands it expects to affect); `None` when
    malformed. One string is one test."""
    raw = task.get("tests")
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)) or not all(isinstance(t, str) for t in raw):
        return None
    return tuple(t.strip() for t in raw if t.strip())


def contribution_map(doc: Goals, tasks: Sequence[Mapping[str, Any]]
                     ) -> dict[str, dict[str, list[str]]]:
    """Per goal, the open tasks serving it by contribution (R27); a task serving a goal
    without saying how is `unclassified`, never assumed `direct` (C5)."""
    out: dict[str, dict[str, list[str]]] = {g: {} for g in doc.ids}
    for task in tasks:
        if task.get("status") == "complete":
            continue
        kinds = task_contribution(task)
        for gid in task_serves(task) or ():
            if gid in out:
                out[gid].setdefault(kinds.get(gid, "unclassified"), []).append(
                    str(task.get("id", "?")))
    return out


def _task_drafts(task: Mapping[str, Any]) -> list[Problem]:
    """What a format-2 task does not yet say (R32): drafts, never holds."""
    tid = str(task.get("id", "?"))

    def draft(text: str) -> Problem:
        return Problem("draft", f"task {tid} {text}", task=tid)

    served = task_serves(task) or ()
    kinds = task_contribution(task)
    out: list[Problem] = []
    if served and "contribution" not in task:
        out.append(draft(f"serves {', '.join(served)} but names no contribution (direct, "
                         "enabling or maintenance)"))
    elif isinstance(task.get("contribution"), Mapping):
        out += [draft(f"names no contribution for {g}") for g in served if g not in kinds]
    direct = [g for g in served if kinds.get(g) == "direct"]
    if direct and not (normalise_criteria(task.get("criteria"))[0] or task_tests(task)):
        out.append(draft(f"is direct for {', '.join(direct)} but names no criterion or test "
                         "it should turn GREEN"))
    if not task.get("evidence") and not task.get("acceptance_criteria"):
        out.append(draft("names no evidence of done (`evidence:` or `acceptance_criteria:`)"))
    return out


# --- the two validation modes (GDT-2) -------------------------------------------------------


def _linkable(doc: Goals) -> dict[str, Any]:
    return {e.id: e for e in (*doc.scenarios, *doc.criteria, *doc.operator_checks)}


def task_links(doc: Goals, task: Mapping[str, Any]) -> set[str]:
    """The contract ids a task touches: the goals it serves, the ids in its `criteria`, and
    the goals those belong to (a criterion of G3 is G3's work, whichever side links it)."""
    links = set(task_serves(task) or ())
    named = normalise_criteria(task.get("criteria"))[0]
    links.update(named)
    entries = _linkable(doc)
    for nid in named:
        if nid in entries:
            links.update(entries[nid].goals)
        links.update(g.id for g in doc.goals if nid in g.verified_ids)
    return links


def task_problems(doc: Goals, task: Mapping[str, Any]) -> tuple[Problem, ...]:
    """What is wrong with how a task names its goals and criteria; each holds it (R29).

    No `serves` key is unclassified, not a deliberate none; `serves: []` is an answer.
    """
    tid = str(task.get("id", "?"))

    def gate(text: str) -> Problem:
        return Problem("gate", f"task {tid} {text}", task=tid)

    if "serves" not in task:
        out = [gate("names no goal it serves (add `serves:`; `serves: []` for polish)")]
    else:
        ids, bad = normalise_serves(task.get("serves"))
        out = [gate(p) for p in bad]
        out += [gate(f"serves unknown goal {g}") for g in ids if g not in doc.by_id]
    named, bad = normalise_criteria(task.get("criteria"))
    out += [gate(p) for p in bad]
    out += [gate(f"names unknown criterion {c}") for c in named if c not in _linkable(doc)]
    out += [gate(p) for p in _contribution(task)[1]]
    if task_tests(task) is None:
        out.append(gate("tests must be a list of test ids or commands"))
    return tuple(out)


def task_gate(doc: Goals, task: Mapping[str, Any]) -> tuple[Problem, ...]:
    """The execution gate for one task: its own problems, then the contract's gate problems
    on ids it touches. A whole-contract one (`ids == ()`) holds any task touching an id, so
    a `serves: []` task with no `criteria` is never held by the contract (`01` §4); it stays
    regression-checked at review (R28)."""
    tid = str(task.get("id", "?"))
    links = task_links(doc, task)
    held = list(task_problems(doc, task))
    held += [replace(p, task=tid) for p in doc.gate
             if links and (not p.ids or links.intersection(p.ids))]
    return tuple(held)


def execution_holds(doc: Goals,
                    tasks: Sequence[Mapping[str, Any]]) -> dict[str, tuple[Problem, ...]]:
    """Every open task the gate holds, in document order, with why."""
    holds: dict[str, tuple[Problem, ...]] = {}
    for task in tasks:
        if task.get("status") == "complete":
            continue
        held = task_gate(doc, task)
        if held:
            holds[str(task.get("id", "?"))] = held
    return holds


def describe_holds(holds: Mapping[str, Sequence[Problem]]) -> str:
    return "; ".join(f"{tid} ({'; '.join(p.text for p in held)})"
                     for tid, held in holds.items())


def _unserved(doc: Goals, tasks: Sequence[Mapping[str, Any]],
              completed: Sequence[Mapping[str, Any]]) -> list[Problem]:
    served: set[str] = set()
    for entry in (*tasks, *completed):
        served.update(task_serves(entry) or ())
    return [Problem("draft", f"goal {g} is served by no open or completed task", (g,))
            for g in doc.ids if g not in served]


def diagnose(doc: Goals, tasks: Sequence[Mapping[str, Any]],
             completed: Sequence[Mapping[str, Any]] = ()) -> list[Problem]:
    """The drafting view (R31): the contract's problems, every open task's, and coverage
    (R69) — a goal no open or completed task serves (`completed`:
    `.orchestrator/completed_tasks.json` entries), and in format 2 a criterion with no
    bound test and a goal nothing runnable verifies, even when a task serves it
    (metadata-only coverage), plus what each open task does not yet say (contribution,
    evidence, a criterion for direct work; GDT-4). Coverage items stay drafts here; the
    feasibility gate (`maestro.goal_roadmap.feasibility`) is the execution-time check."""
    out = [p for p in doc.issues if p.severity != "note"]
    for task in tasks:
        if task.get("status") != "complete":
            out.extend(task_problems(doc, task))
            if doc.format == 2:
                out.extend(_task_drafts(task))
    out += _unserved(doc, tasks, completed)
    if doc.format == 2:
        out += [Problem("draft", f"{c.id} has no test bound yet (setup binds one, GDT-3)",
                        (c.id, *c.goals)) for c in doc.criteria if not c.test]
        runnable = {c.id for c in doc.criteria if c.test} | {o.id for o in doc.operator_checks}
        out += [Problem("draft", f"{g} has no bound test or operator check", (g,))
                for g in doc.ids if not runnable.intersection(doc.verifiers(g))]
        out += _internal_details(doc)
        by_id = {s.id: s for s in doc.scenarios}
        out += [Problem("draft", f"{sid} is proved only by unit-level criteria (no "
                        "e2e/integration criterion or operator check)",
                        (sid, *by_id[sid].goals)) for sid in unit_only_scenarios(doc)]
    return out


def scenario_entries(doc: Goals, scenario: Scenario) -> tuple[list[Criterion],
                                                               list[OperatorCheck]]:
    """The criteria and operator checks proving a scenario, whichever side names the link,
    in contract order."""
    crits = [c for c in doc.criteria if c.id in scenario.criteria or scenario.id in c.scenarios]
    ops = [o for o in doc.operator_checks
           if o.id in scenario.operator_checks or scenario.id in o.scenarios]
    return crits, ops


def unit_only_scenarios(doc: Goals) -> list[str]:
    """The scenarios whose mandatory proof is only unit-level criteria: every mandatory
    criterion's layer is `unit` and no mandatory operator check proves it (GDT-10, R25:
    the completion gate never takes that as final proof)."""
    out = []
    for s in doc.scenarios:
        crits, ops = scenario_entries(doc, s)
        mandatory = [c for c in crits if c.mandatory]
        if mandatory and all(c.layer.strip().lower() == "unit" for c in mandatory) \
                and not any(o.mandatory for o in ops):
            out.append(s.id)
    return out


#: A source path or a `name()` call in a behaviour field: the spec says how, not what (R15).
_INTERNAL = re.compile(r"\b[\w.-]+/[\w./-]+\.(?:py|js|ts|go|rs|java|rb|sql)\b|\b[A-Za-z_]\w*\(\)")


def _internal_details(doc: Goals) -> list[Problem]:
    """A criterion whose Given/When/Then/Oracle names an internal detail. Fixtures and the
    bound test are implementation by nature, so they are never checked."""
    out: list[Problem] = []
    for c in doc.criteria:
        found = next((m.group() for text in (c.given, c.when, c.then, c.oracle)
                      for m in [_INTERNAL.search(text)] if m), None)
        if found:
            out.append(Problem("draft", f"{c.id} names an internal detail ({found}): state "
                               "the observable behaviour, unless Dan approved that detail in "
                               "the contract", (c.id, *c.goals)))
    return out


def coverage(doc: Goals, tasks: Sequence[Mapping[str, Any]],
             completed: Sequence[Mapping[str, Any]] = ()) -> list[str]:
    """W11a's goal warnings as text: each task's `serves` problems and a goal no open or
    completed task serves. `diagnose` is the full listing."""
    return [p.text for task in tasks for p in task_problems(doc, task)] + [
        p.text for p in _unserved(doc, tasks, completed)]


# --- the brief excerpt ------------------------------------------------------------------------


# --- the role excerpt (GDT-5) ---------------------------------------------------------------

#: Roles that change the task's worktree; D23 is their rule (`01` §1, R36).
WRITER_ROLES = ("implementer", "test_designer")
#: The authority labels a diagnoser's excerpt carries (`01` §5: failures and policy).
_DIAGNOSER_AUTHORITY = ("retries", "stop a failed approach when", "ask dan", "blocker")
_ROLE_RULES = {
    "planner": "PLANNING: plan only inside the approved contract. Every remaining gap needs "
               "an open task naming it in `criteria:`; a new or changed goal, criterion or "
               "threshold is Dan's.",
    "diagnoser": "DIAGNOSIS: a repair stays inside this task and the contract; changing a "
                 "goal, a criterion, its bound test or an invariant is Dan's decision.",
    "triage": "TRIAGE: polish keeps every hard invariant and never adds goal work; the "
              "non-goals are out of scope.",
}
_D23 = ("SCOPE (D23): the goals explain why this task exists; they never widen it. Build "
        "only what this task's acceptance criteria and notes ask; if a goal needs more, say "
        "so in your result for the planner instead of building it.")


def entry_goals(doc: Goals, entry: Any) -> list[str]:
    """The goals an `AC-`/`OP-` entry verifies, whichever side names the link, in contract
    order."""
    found = set(entry.goals) | {g.id for g in doc.goals if entry.id in g.verified_ids}
    return [g for g in doc.ids if g in found]


def _full(goal: Goal) -> str:
    how = f"{goal.verified_kind}: {goal.verified_by}" if goal.verified_kind else goal.verified_by
    return f"{goal.id} (served) {goal.name} — target: {goal.target}; verified by {how}"


def _full_v2(doc: Goals, goal: Goal) -> list[str]:
    fields_ = (("outcome", goal.outcome), ("rationale", goal.rationale),
               ("baseline", goal.baseline), ("target", goal.target),
               ("verified by", ", ".join(doc.verifiers(goal.id)) or goal.verified_by),
               ("constraints", ", ".join(goal.constraints)),
               ("depends on", ", ".join(goal.depends_on)))
    return ([f"{goal.id} (served) {goal.name}"]
            + [f"  {label}: {value}" for label, value in fields_ if value]
            + [f"  open question: {q}" for q in goal.open_questions])


def _given_when_then(entry: Any) -> str:
    return "; ".join(f"{word} {value}" for word, value in (
        ("given", entry.given), ("when", entry.when), ("then", entry.then)) if value)


def _task_criteria(doc: Goals, task: Mapping[str, Any], served: Sequence[str],
                   states: Mapping[str, str],
                   sources: Optional[Mapping[str, str]] = None) -> list[str]:
    """The scenarios, criteria and operator checks of the served goals and of the task's
    `criteria:`, each criterion with its bound test and baseline state (or, GDT-9, the
    later observation's state labelled with its source)."""
    sources = sources or {}
    named = set(normalise_criteria(task.get("criteria"))[0])
    shown = named | {v for g in served for v in doc.verifiers(g)}
    crits = [c for c in doc.criteria if c.id in shown]
    ops = [o for o in doc.operator_checks if o.id in shown]
    scenario_ids = named | {s for e in (*crits, *ops) for s in e.scenarios}
    lines: list[str] = []
    for s in doc.scenarios:
        if s.id in scenario_ids or set(s.goals) & set(served):
            kind = f" ({s.kind})" if s.kind else ""
            lines.append(f"{s.id} {s.name}{kind}: {_given_when_then(s)}")
            lines += [f"  open question: {q}" for q in s.open_questions]

    def head(entry: Any) -> str:
        links = [*entry_goals(doc, entry), *entry.scenarios]
        if entry.id in named:
            links.append("named by this task")
        return f"{entry.id} {entry.name}" + (f" ({'; '.join(links)})" if links else "")

    for c in crits:
        lines.append(f"{head(c)} — {sources.get(c.id) or 'baseline'}: "
                     + states.get(c.id, f"none at revision {doc.revision}"))
        lines += [f"  {_given_when_then(c)}"] if _given_when_then(c) else []
        lines += [f"  {label}: {value}" for label, value in (
            ("oracle", c.oracle), ("threshold", c.threshold)) if value]
        lines.append(f"  test: `{c.test}`" if c.test else "  test: none bound yet")
        lines += [f"  open question: {q}" for q in c.open_questions]
    for o in ops:
        lines.append(f"{head(o)} — operator check, signed off by {o.sign_off or 'Dan'}; "
                     "never an automated pass")
        lines += [f"  expected: {o.expected}"] if o.expected else []
    return lines


def _gaps(doc: Goals, states: Mapping[str, str]) -> list[str]:
    """Every remaining gap at this revision, for the planner (`01` §5)."""
    from maestro import acceptance

    if not states:
        return [f"No acceptance baseline at contract revision {doc.revision} "
                "(run `maestro goals baseline`)"]
    unmet = [f"{c.id} {states.get(c.id, acceptance.UNKNOWN)} "
             f"({', '.join(entry_goals(doc, c)) or 'no goal'})"
             for c in doc.criteria if c.mandatory and states.get(c.id) != acceptance.GREEN]
    lines = [f"Remaining gaps at contract revision {doc.revision} (mandatory criteria not "
             f"GREEN): {'; '.join(unmet) or 'none'}"]
    ops = [f"{o.id} ({', '.join(entry_goals(doc, o))})"
           for o in doc.operator_checks if o.mandatory]
    if ops:
        lines.append(f"Operator checks Dan signs off at completion: {'; '.join(ops)}")
    return lines


def _role_rule(role: str, kinds: Mapping[str, str]) -> list[str]:
    if role in WRITER_ROLES:
        return [_D23]
    if role == "reviewer":
        expected = ", ".join(f"{g} {k}" for g, k in kinds.items())
        return ["REVIEW: " + (f"Expected contribution: {expected}. " if expected else "")
                + "Judge the task's correctness against its own acceptance criteria and "
                "notes first, and its goal impact separately, from evidence. Work for a goal "
                "that this task does not ask for is a scope finding, not progress (D23): "
                "report it under unasked_goal_work."]
    return [_ROLE_RULES[role]] if role in _ROLE_RULES else []


def excerpt(doc: Goals, serves: Optional[Sequence[str]], *, role: str = "",
            task: Optional[Mapping[str, Any]] = None,
            results: Optional[Sequence[Any]] = None,
            sources: Optional[Mapping[str, str]] = None) -> tuple[str, str]:
    """The goals excerpt for a `role`'s brief, and the cut it made (`""` when none).
    `sources` labels a criterion whose state an observation gave (GDT-9) instead of the
    baseline.

    Critical lines, never cut: the purpose, the contract revision, the served goals in full
    with (format 2) their scenarios, criteria, bound tests and baseline states from
    `results` (`acceptance.Result`s; only the current revision's count), the task's
    `tests:`, hard invariants, trade-off bounds, the priority order, the planner's gaps or
    the diagnoser's authority, and the role's rule. Lower-priority lines fill what is left
    of `MAX_GOALS_TOKENS`: the other goals in priority order as one line each, the
    non-goals in their order (critical for triage), then the goals no priority names.
    The cut names each omitted part with its section in `docs/GOALS.md`.
    """
    from maestro import acceptance

    task = task or {}
    served = tuple(serves) if serves is not None else ()
    kinds = task_contribution(task)
    v2 = doc.format == 2
    states = ({cid: r.state for cid, r in acceptance.current(results or (), doc).items()}
              if v2 else {})
    head = [f"Purpose: {doc.purpose}"]
    if v2:
        head.append(f"Contract revision: {doc.revision} (full contract: {DOC})")
    if serves is None:
        head.append("This task does not name the goals it serves (no `serves:` in its "
                    "ROADMAP block).")
    elif not served:
        head.append("This task serves no goal (serves: []).")
    else:
        head.append("This task serves: " + ", ".join(
            f"{g} ({kinds[g]})" if g in kinds else g for g in served))
    by_id = doc.by_id
    for gid in served:
        if gid not in by_id:
            head.append(f"{gid} (served): not in {DOC}")
        else:
            head += _full_v2(doc, by_id[gid]) if v2 else [_full(by_id[gid])]
    if v2:
        head += _task_criteria(doc, task, served, states, sources)
    tests = task_tests(task)
    if tests:
        head.append(f"Tests this task should affect: {', '.join(tests)}")
    if doc.invariants:
        head.append("Hard invariants (never traded for progress):")
        head += [f"{i.id}: {i.text}"
                 + (f" (checked by {', '.join(i.checked_by)})" if i.checked_by else "")
                 for i in doc.invariants]
    if doc.trade_offs:
        head.append("Approved trade-offs (only within their bound):")
        for t in doc.trade_offs:
            terms = "; ".join(f"{label} {value}" for label, value in (
                ("decision", t.decision), ("bound", t.bound), ("expires", t.expires)) if value)
            head.append(f"{t.id}: {t.text}" + (f" ({terms})" if terms else ""))
    if doc.priority:
        head.append(f"Priority when goals conflict: {' > '.join(doc.priority)}")
    tail: list[str] = []
    if role == "planner" and v2:
        tail += _gaps(doc, states)
    if role == "diagnoser":
        tail += [f"Authority ({label}): {doc.authority[label]}"
                 for label in _DIAGNOSER_AUTHORITY if doc.authority.get(label)]
    tail += _role_rule(role, kinds)

    limit = MAX_GOALS_TOKENS * 4
    non_goals = list(doc.non_goals)
    keep_all_non = role == "triage"
    non_line = f"Non-goals: {'; '.join(non_goals)}" if keep_all_non and non_goals else ""
    critical = len("\n".join(head + tail + ([non_line] if non_line else [])))
    budget = limit - critical
    rank = {g: n for n, g in enumerate(doc.priority)}
    others = [g for g in by_id.values() if g.id not in served]
    kept: set[str] = set()

    def take(goal: Goal) -> None:
        nonlocal budget
        cost = len(f"{goal.id} {goal.name}") + 1
        if cost <= budget:
            budget -= cost
            kept.add(goal.id)

    for goal in sorted((g for g in others if g.id in rank), key=lambda g: rank[g.id]):
        take(goal)
    kept_non = non_goals if keep_all_non else []
    if not keep_all_non:
        for item in non_goals:
            cost = len(item) + (2 if kept_non else len("Non-goals: ") + 1)
            if cost > budget:
                break
            budget -= cost
            kept_non.append(item)
    for goal in (g for g in others if g.id not in rank):
        take(goal)

    body = head + [f"{g.id} {g.name}" for g in others if g.id in kept]
    if kept_non:
        body.append(f"Non-goals: {'; '.join(kept_non)}")
    text = "\n".join(body + tail)
    omitted = []
    dropped = [g.id for g in others if g.id not in kept]
    if dropped:
        omitted.append(f"the summaries of {', '.join(dropped)} ({DOC} §Goals)")
    if len(kept_non) < len(non_goals):
        omitted.append(f"non-goals {len(kept_non) + 1}-{len(non_goals)} of {len(non_goals)} "
                       f"({DOC} §Non-goals)")
    if critical > limit:
        return text, (f"goals_excerpt: critical lines exceed the {MAX_GOALS_TOKENS}-token "
                      "bound and are kept in full"
                      + (f"; omitted {'; '.join(omitted)}" if omitted else "")
                      + f"; read the full contract in {DOC}")
    if omitted:
        return text, (f"goals_excerpt: omitted to stay within {MAX_GOALS_TOKENS} tokens: "
                      f"{'; '.join(omitted)}; read them there")
    return text, ""
