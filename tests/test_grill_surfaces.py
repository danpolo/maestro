"""The three places a `docs/GOALS.md` contract is written (GDT-13).

Drive `ChatGPT/grill-me-framework` (ChatGPT's grill), `~/agent-skills/skills/grill-me`
(Claude/Codex) and `~/agent-skills/skills/maestro-setup` (the fallback) must write the same
contract: the same steps before any design question, the same fields and defaults, the same
rules, and worked examples that parse to the same contract (C1.1, C1.2; `01` §12 "parity of
required fields and defaults is mandatory").

The tests read only vendored copies (`tests/fixtures/grill_surfaces/`, `maestro/skills/`),
never Drive or `~/agent-skills`: this suite is every project's release gate.
`scripts/grill_surfaces.py check` proves the copies are the live text; `refresh` rewrites them.
Text checks pin structure only (bold step names, table headers, fenced examples, a few anchor
phrases), so rewording the prose does not break them.

Requirements: R01, R03, R10, R12, R13, R15, R20, R26, R47
(`docs/agent-plans/p13-goal-driven-tdd/`).
"""
from __future__ import annotations

import re
from dataclasses import fields as dc_fields, replace
from pathlib import Path

import pytest
import yaml

from maestro import goal_conflicts as gc
from maestro.docs import goals
from maestro.workflows import dan_questions

REPO_ROOT = Path(__file__).resolve().parents[1]
PARITY = REPO_ROOT / "tests" / "fixtures" / "goals" / "parity"
SURFACES = REPO_ROOT / "tests" / "fixtures" / "grill_surfaces"
SETUP = REPO_ROOT / "maestro" / "skills" / "claude" / "SKILL.md"
SKILL_PARITY = (PARITY / "skill_GOALS.md").read_text(encoding="utf-8")
DRIVE_PARITY = (PARITY / "drive_GOALS.md").read_text(encoding="utf-8")
#: The parity contract as each grill writes it.
STYLE = {"drive": DRIVE_PARITY, "grill-me": SKILL_PARITY}
GRILLS = ("drive", "grill-me")
ALL = ("drive", "grill-me", "setup")

#: The contract steps every grill walks, in order, before any design question (R01, R10).
CONTRACT_STEPS = ("Purpose", "Users", "Goals", "Final product", "Scenarios",
                  "Acceptance criteria", "Operator checks", "Non-goals and invariants",
                  "Trade-offs", "Priority", "Authority", "Approval")
#: The parsed parts compared for parity (as `test_goals_contract.test_render_round_trips`).
PARTS = ("format", "revision", "approval", "purpose", "users", "goals", "scenarios",
         "criteria", "operator_checks", "non_goals", "invariants", "trade_offs",
         "priority", "authority", "history", "retired", "final_product")


def _file(surface: str, name: str) -> str:
    if surface == "setup":
        return SETUP.read_text(encoding="utf-8")
    path = SURFACES / surface / name
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _texts(surface: str) -> str:
    if surface == "setup":
        return _file("setup", "SKILL.md")
    return "\n".join(_file(surface, n) for n in ("SKILL.md", "GOALS-FORMAT.md",
                                                 "PLAN-FORMAT.md"))


def _flat(text: str) -> str:
    """Lower case, no backticks or bold, one kind of dash and arrow: for anchor phrases."""
    text = text.replace("`", "").replace("**", "").lower()
    text = text.replace("–", "-").replace("—", "-").replace("→", "->")
    return " ".join(text.split())


def _section(text: str, title: str, *, own_only: bool = False) -> str:
    """The section whose heading starts with `title`: up to the next heading of the same or
    a higher level (`own_only`: up to the next heading of any level)."""
    match = re.search(rf"^(#{{2,4}})\s+{re.escape(title)}[^\n]*$", text, re.M)
    if not match:
        return ""
    level = len(match.group(1))
    pattern = re.compile(r"^#{1,6}\s" if own_only else rf"^#{{1,{level}}}\s")
    out, fenced = [], False
    for line in text[match.end():].split("\n"):
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and pattern.match(line):          # a heading inside a fence is text
            break
        out.append(line)
    return "\n".join(out)


def _fenced(text: str, after: str = "") -> list[str]:
    start = text.find(after) if after else 0
    if start < 0:
        return []
    return re.findall(r"^```[a-z]*\n(.*?)\n```", text[start:], re.M | re.S)


def _worked_example(surface: str) -> str:
    blocks = _fenced(_section(_file(surface, "GOALS-FORMAT.md"), "Worked example"))
    return blocks[0] + "\n" if blocks else ""


def _table(text: str, header: str) -> list[list[str]]:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == header:
            rows = []
            for row in lines[i + 2:]:
                if not row.strip().startswith("|"):
                    break
                rows.append([c.strip() for c in row.strip().strip("|").split("|")])
            return rows
    return []


def _same(a: goals.Goals, b: goals.Goals) -> None:
    for part in PARTS:
        assert getattr(a, part) == getattr(b, part), part


# --- deleting one field from the parity contract ---------------------------------------------


_ENTRY_SECTIONS = {"Goal": "Goals", "Scenario": "Final product",
                   "Acceptance criterion": "Acceptance criteria",
                   "Operator check": "Operator checks"}


def _drop_field(line: str, label: str) -> str | None:
    """`line` without its `label` field (a whole line, or one `·`-joined segment); None when
    the line does not carry it."""
    prefix, rest = re.match(r"^(\s*(?:[-*+]\s+)?)(.*)$", line).groups()
    segments = [s.strip() for s in rest.split("·")]
    keep = [s for s in segments
            if not re.match(rf"^{re.escape(label)}\s*(?::|—|–)", goals._plain(s), re.I)]
    if len(keep) == len(segments):
        return None
    return prefix + " · ".join(keep) if keep else ""


def _delete(text: str, field: goals.Field) -> tuple[str, str]:
    """The contract without `field` (at its first place), and the id or the text the problem
    about it names. The text is unchanged when the contract does not carry the field."""
    lines = text.split("\n")
    section, entry = None, None
    header = True
    for i, line in enumerate(lines):
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            section, entry, header = goals._section_key(heading.group(1)), None, False
            if field.part == "Section" and section == field.label:
                end = next((j for j in range(i + 1, len(lines))
                            if re.match(r"^##\s", lines[j])), len(lines))
                return "\n".join(lines[:i] + lines[end:]), f"missing section: {field.label}"
            continue
        if field.part == "Contract" and header and field.label.lower() in line.lower():
            about = "no contract revision" if field.label == "Contract revision" else ""
            return "\n".join(lines[:i] + lines[i + 1:]), about
        sub = re.match(r"^###\s+(\S+)", line)
        if sub:
            entry = goals._ids(sub.group(1))[0] if goals._ids(sub.group(1)) else None
            continue
        if field.part in _ENTRY_SECTIONS and section == _ENTRY_SECTIONS[field.part] and entry:
            dropped = _drop_field(line, field.label)
            if dropped is not None:
                return "\n".join(lines[:i] + ([dropped] if dropped else []) + lines[i + 1:]), entry
        if field.part == "Authority" and section == "Authority":
            dropped = _drop_field(line, field.label)
            if dropped is not None:
                return ("\n".join(lines[:i] + ([dropped] if dropped else []) + lines[i + 1:]),
                        f"authority does not say: {field.label.lower()}")
        if field.part == "Hard invariant" and section == "Hard invariants":
            new = re.sub(r"\s*\((?:checked|verified) by [^)]*\)", "", line, flags=re.I)
            if new != line:
                return "\n".join(lines[:i] + [new] + lines[i + 1:]), goals._ids(line)[0]
        if field.part == "Trade-off" and section == "Trade-offs":
            new = re.sub(rf";\s*{field.label.lower()}\b[^;]*", "", line, flags=re.I)
            if new != line:
                return "\n".join(lines[:i] + [new] + lines[i + 1:]), goals._ids(line)[0]
        if field.part == "Change history" and section == "Change history" \
                and line.strip().startswith("| 2 |"):
            cells = line.strip().strip("|").split("|")
            cells[5] = " "
            return ("\n".join(lines[:i] + ["|" + "|".join(cells) + "|"] + lines[i + 1:]),
                    "is not approved")
    return text, ""


# --- 1-3: the parity contract and the fields every surface lists (R12, C1.2) ------------------


def test_the_drive_and_skill_parity_contracts_parse_equal():
    """The same contract as grill-me writes it and as ChatGPT writes it from the Drive doc
    and a Google-Docs export delivers it (BOM, tripled blank lines, numbered headings, `*`,
    `—`, `AC1`, `·`): every part parses equal, with no problem on either."""
    skill, drive = goals.parse(SKILL_PARITY), goals.parse(DRIVE_PARITY)
    for doc in (skill, drive):
        assert [p.text for p in doc.issues] == []
    _same(drive, skill)
    assert drive.title == skill.title == "Recipe Box"       # a BOM does not hide the title
    assert drive.by_id["G3"].depends_on == ("G1",)
    assert [c.mandatory for c in drive.criteria] == [True, True, True, False]
    ac1 = drive.criteria[0]
    assert ac1.test == "python bench/import_ratio.py"
    assert ac1.fixtures and ac1.environment and ac1.data
    assert drive.trade_offs[0].bound == "2 hours" and drive.trade_offs[0].decision == "D-3"
    assert set(drive.retired) == {"G4"} and [h.rev for h in drive.history] == [1, 2]
    assert set(drive.authority) == set(goals._AUTHORITY_LABELS)
    assert drive.open_questions == () and drive.invariants[0].checked_by == ("AC-2",)


def test_the_parity_contract_names_every_contract_field():
    labels = {"Goal": goals._GOAL_LABELS, "Scenario": goals._SCENARIO_LABELS,
              "Acceptance criterion": goals._CRITERION_LABELS,
              "Operator check": goals._OPERATOR_LABELS}
    fields = goals.CONTRACT_FIELDS
    assert {f.required for f in fields} <= {"gate", "draft", "no"}
    for part, known in labels.items():
        named = {known[f.label.lower()] for f in fields if f.part == part}
        assert named == set(known.values()), part
    assert {f.label for f in fields if f.part == "Section"} == {
        *goals.SECTIONS, *goals.SECTIONS_V2, "Operator checks", "Trade-offs", "Retired ids"}
    assert tuple(f.label.lower() for f in fields if f.part == "Authority") == \
        goals._AUTHORITY_LABELS
    for field in fields:
        for name, text in (("skill", SKILL_PARITY), ("drive", DRIVE_PARITY)):
            assert _delete(text, field)[0] != text, f"{name} parity lacks {field}"


@pytest.mark.parametrize("field", getattr(goals, "CONTRACT_FIELDS", ("absent",)),
                         ids=lambda f: f"{getattr(f, 'part', f)}:{getattr(f, 'label', '')}")
def test_contract_fields_match_what_validation_requires(field):
    """The table says what the field's absence does, neither more nor less: without the
    field, the worst new problem is exactly its `required` severity (`no`: none, or only a
    note). A section counts with its entries, so a section whose ids other entries name
    gates through those links (GDT-13 review M1)."""
    rank = {"no": 0, "note": 0, "draft": 1, "gate": 2}
    before = {(p.severity, p.text) for p in goals.parse(SKILL_PARITY).issues}
    text, about = _delete(SKILL_PARITY, field)
    new = [p for p in goals.parse(text).issues if (p.severity, p.text) not in before]
    worst = max((p.severity for p in new), key=rank.__getitem__, default="no")
    assert rank[worst] == rank[field.required], new
    if field.part != "Section" and field.required != "no":
        assert any(about in p.ids or about in p.text for p in new
                   if p.severity == field.required), new


def _fields_rows() -> list[list[str]]:
    return [[f.part, f.label, f.required, f.default] for f in goals.CONTRACT_FIELDS]


@pytest.mark.parametrize("surface", ALL)
def test_every_surface_lists_the_contract_fields_and_defaults(surface):
    """`01` §12: the same required fields and defaults on every path; the table is generated
    from `goals.CONTRACT_FIELDS` (`scripts/grill_surfaces.py fields`)."""
    text = _file(surface, "GOALS-FORMAT.md")
    rows = _table(text, "| Part | Field | Required | Default |")
    assert rows == _fields_rows()


# --- 5, 10: worked examples (R12, R02, R13) ------------------------------------------------


@pytest.mark.parametrize("surface", GRILLS)
def test_each_grill_s_worked_example_is_the_parity_contract(surface):
    example = _worked_example(surface)
    assert example, f"{surface} has no worked example"
    doc, parity = goals.parse(example), goals.parse(STYLE[surface])
    _same(doc, parity)
    assert doc.title == parity.title


@pytest.mark.parametrize("surface", GRILLS)
def test_each_example_covers_every_automatable_goal_and_scenario(surface):
    """R13: every goal and scenario has an AC or, when not automatable, an OP; G2 is
    operator-only; a goal with neither is a gate problem."""
    doc = goals.parse(_worked_example(surface))
    assert doc.goals, f"{surface} has no worked example"
    assert not [p for p in doc.issues if "has no acceptance criterion or operator check"
                in p.text]
    assert doc.verifiers("G2") and all(v.startswith("OP-") for v in doc.verifiers("G2"))
    assert all(doc.verifiers(g.id) for g in doc.goals)
    unlinked = replace(
        doc, goals=tuple(replace(g, verified_ids=()) if g.id == "G1" else g for g in doc.goals),
        criteria=tuple(replace(c, goals=tuple(x for x in c.goals if x != "G1"))
                       for c in doc.criteria))
    again = goals.parse(goals.render(unlinked))
    assert any(p.text == "G1 has no acceptance criterion or operator check"
               and p.ids == ("G1",) for p in again.gate)


# --- 6, 7, 8: contract first, missing or old goals, no goal-count rule (R01, R10, R03) -------


@pytest.mark.parametrize("surface", GRILLS)
def test_every_grill_settles_the_contract_before_design(surface):
    text = _file(surface, "SKILL.md")
    first = re.search(r"^#{2,3}\s+Contract first\s*$", text, re.M)
    assert first, f"{surface} has no Contract first section"
    steps = re.findall(r"^\d+\.\s+\*\*(.+?)\*\*", _section(text, "Contract first",
                                                             own_only=True), re.M)
    assert tuple(s.rstrip(":.").strip() for s in steps) == CONTRACT_STEPS
    design = re.search(r"^#{2,4}\s+Challenge the starting", text, re.M)
    assert design and first.start() < design.start()


@pytest.mark.parametrize("surface", ALL)
def test_every_surface_handles_a_missing_or_format_one_goals_file(surface):
    text = _flat(_texts(surface))
    assert "no docs/goals.md" in text
    assert "format 1" in text
    if surface == "drive":
        assert "v1 -> v2" in text                  # ChatGPT has no shell: the rules instead
    else:
        assert "maestro goals migrate" in text
    if surface == "setup":
        assert "maestro goals check docs/goals.md --json" in text and '"ask": true' in text


@pytest.mark.parametrize("surface", ALL)
def test_no_surface_makes_three_to_seven_a_rule(surface):
    text = _flat(_texts(surface))
    assert "3 to 7 goals" not in text and "more than seven means" not in text
    if surface != "setup":
        assert "3-7 top-level goals is the usual range, not a limit" in _flat(
            _file(surface, "GOALS-FORMAT.md"))


# --- the revision rule as `goals.changes` enforces it (GDT-13 review M2) ---------------------


_ENTRY_TYPES = {"Goal": goals.Goal, "Scenario": goals.Scenario,
                "Acceptance criterion": goals.Criterion, "Operator check": goals.OperatorCheck,
                "Hard invariant": goals.Invariant, "Trade-off": goals.TradeOff}


def _attr(field: goals.Field) -> str:
    labels = {"Goal": goals._GOAL_LABELS, "Scenario": goals._SCENARIO_LABELS,
              "Acceptance criterion": goals._CRITERION_LABELS,
              "Operator check": goals._OPERATOR_LABELS}.get(field.part, {})
    attr = labels.get(field.label.lower(), field.label.lower().replace(" ", "_"))
    return "verified_ids" if attr == "verified" else attr


def _same_revision_labels() -> set[str]:
    """The labels of the entry fields that may change at the same revision: the ones
    `goals.changes` does not compare. Each field is also changed on the parity contract, to
    prove the derivation is what `changes` does."""
    doc = goals.parse(SKILL_PARITY)
    entries = {kind: next(e for e in goals._entities(doc).values() if type(e) is kind)
               for kind in _ENTRY_TYPES.values()}
    out = set()
    for field in goals.CONTRACT_FIELDS:
        kind = _ENTRY_TYPES.get(field.part)
        if kind is None:
            continue
        attr = _attr(field)
        assert attr in {f.name for f in dc_fields(kind)}, (field, attr)
        entry = entries[kind]
        value = getattr(entry, attr)
        other = (not value if isinstance(value, bool) else
                 (*value, "G9") if isinstance(value, tuple) else f"{value} changed")
        edited = replace(doc, **{
            name: tuple(replace(e, **{attr: other}) if e is entry else e
                        for e in getattr(doc, name))
            for name in ("goals", "scenarios", "criteria", "operator_checks", "invariants",
                         "trade_offs")})
        illegal = [p for p in goals.changes(doc, edited).problems
                   if "without a new contract revision" in p.text]
        exempt = attr not in goals._REVISION_FIELDS[kind]
        assert (illegal == []) == exempt, (field, illegal)
        if exempt:
            out.add(field.label)
    return out


def _name_changes_freely() -> bool:
    """Whether an entry's name may change at the same revision, proved on the parity contract
    for every entry kind that has a name (the rule says "its name" without backticks, so the
    label check above cannot see it; GDT-13 verify Low 3)."""
    doc = goals.parse(SKILL_PARITY)
    free = set()
    for kind in _ENTRY_TYPES.values():
        if "name" not in {f.name for f in dc_fields(kind)}:
            continue
        entry = next(e for e in goals._entities(doc).values() if type(e) is kind)
        edited = replace(doc, **{
            name: tuple(replace(e, name=f"{e.name} renamed") if e is entry else e
                        for e in getattr(doc, name))
            for name in ("goals", "scenarios", "criteria", "operator_checks")})
        illegal = [p for p in goals.changes(doc, edited).problems
                   if "without a new contract revision" in p.text]
        assert (illegal == []) == ("name" not in goals._REVISION_FIELDS[kind]), kind
        free.add(illegal == [])
    assert len(free) == 1, "an entry's name is free for some kinds and compared for others"
    return free.pop()


def _revision_rules(text: str) -> list[str]:
    """Each statement of what may change at the same revision, to the end of its sentence."""
    return re.findall(r"at the same revision:(.*?)\.(?:\s|$)", " ".join(text.split()))


@pytest.mark.parametrize("surface", ALL)
def test_every_surface_states_the_revision_rule_maestro_enforces(surface):
    """Once a baseline recorded the contract, a change to a compared entry field at the same
    revision fails `maestro doctor` (`goal_state.contract_problems`). So every surface says
    which fields may change at the same revision, and they are the ones `goals.changes`
    does not compare; every other entry change is a new revision."""
    text = _texts(surface)
    rules = _revision_rules(text)
    assert rules, f"{surface} does not say what may change at the same revision"
    exempt = _same_revision_labels()
    name_is_free = _name_changes_freely()
    for rule in rules:
        assert set(re.findall(r"`([^`]+)`", rule)) == exempt, rule
        assert ("its name," in _flat(rule)) == name_is_free, rule
    flat = _flat(text)
    assert ("any other change to a goal, scenario, criterion, operator check, invariant or "
            "trade-off is a new revision") in flat
    for narrow in ("a change to a target, threshold or oracle is a new revision",
                   "changing a target, a threshold or an oracle is a new revision"):
        assert narrow not in flat
    if surface == "setup":
        step = _flat(_section(text, "2c."))
        for anchor in ("before the first maestro goals baseline",
                       "after the first maestro goals baseline",
                       "into one new revision", "ask dan to approve it",
                       "the one exception"):
            assert anchor in step, anchor


def test_a_gate_section_that_may_say_none_says_so_plainly():
    """`Operator checks` and `Hard invariants` are `gate` and also accept `None.`; their
    Default cell says outright that `None.` is a complete answer and when its absence gates,
    so the Required column is not read as "must have entries" (GDT-13 verify Low 1). Every
    surface carries these cells (`test_every_surface_lists_the_contract_fields_and_defaults`)."""
    sections = [f for f in goals.CONTRACT_FIELDS
                if f.part == "Section" and f.required == "gate"
                and f.default.startswith("None.")]
    assert {f.label for f in sections} == {"Operator checks", "Hard invariants"}
    for f in sections:
        assert f.default.startswith("None. is a complete answer, so write it when there "
                                    "are none; absent, it "), f
        assert "names an " in f.default, f


# --- 9: behaviour, not design (R15) ----------------------------------------------------------


def _swap_ac1(text: str, block: str) -> str:
    lines = text.split("\n")
    start = next(i for i, line in enumerate(lines) if re.match(r"^###\s+AC-?1\b", line))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith(("### ", "## ")))
    return "\n".join(lines[:start] + block.strip("\n").split("\n") + [""] + lines[end:])


@pytest.mark.parametrize("surface", GRILLS)
def test_the_black_box_example_passes_the_lint_and_the_bad_one_fails(surface):
    section = _section(_file(surface, "GOALS-FORMAT.md"), "Behaviour, not design")
    good = re.search(r"\*\*Good\*\*[^\n]*\n+```[a-z]*\n(.*?)\n```", section, re.S)
    bad = re.search(r"\*\*Bad\*\*[^\n]*\n+```[a-z]*\n(.*?)\n```", section, re.S)
    assert good and bad, f"{surface} has no Good/Bad pair under 'Behaviour, not design'"

    def internal(block: str) -> list[goals.Problem]:
        doc = goals.parse(_swap_ac1(STYLE[surface], block))
        assert [p.text for p in doc.issues if p.severity != "note"] == []
        return [p for p in goals.diagnose(doc, []) if "names an internal detail" in p.text]

    assert internal(good.group(1)) == []
    flagged = internal(bad.group(1))
    assert flagged and flagged[0].ids[0] == "AC-1"
    shown = goals.parse(_swap_ac1(STYLE[surface], good.group(1))).criteria[0]
    assert shown.given.startswith("a saved post") and shown.when == "an hour passes"
    assert shown.then.startswith("the recipe is listed")


# --- 11, 12: conflicts while grilling (R47, grill stage) --------------------------------------


@pytest.mark.parametrize("surface", GRILLS)
def test_every_grill_asks_dan_about_a_definitional_conflict(surface):
    body = _section(_file(surface, "SKILL.md"), "Conflicts while grilling")
    assert body, f"{surface} has no Conflicts while grilling section"
    text = _flat(body)
    for anchor in ("revision", "evidence", "estimate", "consequences", "recommend",
                   "approve", "decline", "approve-with-notes", "choose", "to-",
                   "change-history row", "priority never decides", "hard invariant"):
        assert anchor in text, anchor
    assert len(re.findall(r"^\d+\.\s", body, re.M)) >= 5          # the five §8.2 parts


_AC5_SKILL = """### AC-5: Most saved posts are listed

- **Goals:** G1
- **Scenarios:** S-1
- **Given:** the weekly sample of 40 saved posts
- **When:** an hour passes after they are saved
- **Then:** the share of them listed as recipes is reported
- **Oracle:** the share is below the threshold
- **Threshold:** 0.9
- **Layer:** e2e
- **Evidence:** metric
- **Test:** `python bench/import_ratio.py`

"""
_AC5_DRIVE = """### AC5 — Most saved posts are listed
* **Goals** — G1 · **Scenarios** — S1
* **Given** — the weekly sample of 40 saved posts
* **When** — an hour passes after they are saved
* **Then** — the share of them listed as recipes is reported
* **Oracle** — the share is below the threshold · **Threshold** — 0.9
* **Layer** — e2e · **Evidence** — metric
* **Test** — `python bench/import_ratio.py`

"""


def _with_ac5(style: str) -> str:
    if style == "skill":
        return SKILL_PARITY.replace("- **Verified by:** AC-1, AC-2",
                                    "- **Verified by:** AC-1, AC-2, AC-5").replace(
            "- **Criteria:** AC-1\n", "- **Criteria:** AC-1, AC-5\n").replace(
            "## Operator checks\n", _AC5_SKILL + "## Operator checks\n")
    return DRIVE_PARITY.replace("**Verified by** — AC1, AC2 ·",
                                "**Verified by** — AC1, AC2, AC5 ·").replace(
        "* **Criteria** — AC1 ·", "* **Criteria** — AC1, AC5 ·").replace(
        "## 6. Operator checks", _AC5_DRIVE + "## 6. Operator checks")


@pytest.mark.parametrize("style", ["drive", "skill"])
def test_a_definitional_conflict_from_either_grill_holds_only_its_goal(style):
    """Characterisation (GDT-7 built it): two thresholds on one binding that cannot both pass,
    written by either grill, are a gate problem holding only G1's work, and the grill stage
    builds Dan's question through the conflict API."""
    doc = goals.parse(_with_ac5(style))
    (problem,) = [p for p in doc.gate if p.text.startswith("threshold conflict")]
    assert {"AC-1", "AC-5", "G1"} <= set(problem.ids)
    assert goals.task_gate(doc, {"id": "T-1", "serves": ["G1"], "tests": []})
    assert goals.task_gate(doc, {"id": "T-2", "serves": ["G3"], "tests": []}) == ()
    conflict = gc.Conflict("threshold", "grill", (), "G1", ("AC-1", "AC-5"), doc.revision,
                           (problem.text,), (), (), problem.text)
    question = gc.escalation(conflict, gc.context(doc, ""))
    assert question["conflict"]["stage"] == "grill" and "at grill," in question["question"]
    assert dan_questions.parse({"questions": [question]})[1] == ""


# --- 13-15: PLAN-FORMAT and setup (R26, R20, R12) ---------------------------------------------


@pytest.mark.parametrize("surface", GRILLS)
def test_every_plan_format_maps_phases_to_goals_and_criteria(surface):
    text = _file(surface, "PLAN-FORMAT.md")
    header = next((line for line in text.splitlines() if line.startswith("| id | Phase |")), "")
    cells = {c.strip() for c in header.strip("|").split("|")}
    assert {"Serves", "Criteria", "Contribution"} <= cells, header
    assert ("a phase's serves is a suggestion for its tasks, not proof that each task moves "
            "the goal") in _flat(text)


def _setup_examples() -> list[dict]:
    section = _section(_file("setup", "SKILL.md"), "5a.")
    return [yaml.safe_load(block) for block in _fenced(section, "Worked example")
            if block.lstrip().startswith("id:")]


def test_the_setup_examples_are_tasks_the_goals_gate_accepts():
    doc = goals.parse(SKILL_PARITY)
    tasks = _setup_examples()
    assert len(tasks) >= 2, "5a's worked example is one direct task and an enabling child"
    collector = next(t for t in tasks if t["id"] == "02-collector")
    assert {"serves", "contribution", "criteria", "tests", "evidence"} <= set(collector)
    for task in tasks:
        assert goals.task_problems(doc, task) == (), task["id"]
        assert [p.text for p in goals.diagnose(doc, tasks) if p.task == str(task["id"])] == []
    assert any("enabling" in goals.task_contribution(t).values() for t in tasks)


def test_setup_routes_project_md_to_goals_first():
    blocks = _fenced(_file("setup", "SKILL.md"), "### 2b.")
    routed = next((b for b in blocks if "## Source documents" in b), "")
    first = re.search(r"^1\.\s+(.*)$", routed, re.M)
    assert first and "docs/GOALS.md" in first.group(1)
