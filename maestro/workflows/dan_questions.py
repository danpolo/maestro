"""Questions an agent asks Dan when the task's spec is silent (P12 pilot finding D26).

DuetFlow 06 run 2 spent four review rounds, a diagnosis and a final run on a case the
ROADMAP and ADRs never defined; the reviewer and the diagnoser invented a rule, the rework
broke the selector, and the task parked. Dan, 2026-09-28: the role that hits such a gap
asks him on Telegram at once, and his answer becomes a binding decision of the task.

This module is the shared vocabulary: the question's shape, its parser, the brief text
that says when to ask, and the rendering of Dan's decisions for every later brief. The
runner pauses the node (`WorkflowRunner._ask_dan`), Telegram carries the question
(`hitl.verify.DanVerificationChannel.poll_question`), and `maestro.docs.decisions`
records the answer.
"""
from __future__ import annotations

import json
import re

#: The implementer's question, written into the WORKSPACE instead of DONE.
QUESTION_FILE = "QUESTION"

#: Questions one attempt may ask at once. More is a sign the agent is not stuck on a gap
#: but unsure of the whole task; three keeps a round readable on a phone.
MAX_QUESTIONS_PER_ROUND = 3

#: Question rounds one workflow may ask. From the next, questions are refused like a
#: re-ask (`garbage_output`), so a confused agent cannot hold a task waiting forever.
MAX_ROUNDS_PER_WORKFLOW = 4

MIN_OPTIONS = 2
MAX_OPTIONS = 4

BRIEF_RULE = (
    "ASKING DAN: if you reach a case whose correct behaviour the task does not determine, "
    "ask Dan instead of guessing. Ask only when BOTH hold: (1) the task's criteria, notes, "
    "DAN'S DECISIONS, the project's ADRs (docs/adr/) and CONTEXT.md are all silent on it, "
    "and (2) reasonable choices would change the code or the tests. Never ask what you can "
    "look up in the repository or settle by ordinary engineering convention: Dan's "
    "attention is the cost. Never re-ask anything under DAN'S DECISIONS. A question is "
    'JSON: {"question": "<one decision, answerable without reading code>", '
    '"why_undefined": "<which criteria, notes or ADRs are silent, and why the choice '
    'matters>", "options": [{"label": "<short choice>", "consequence": "<what the code and '
    'tests would then do>"}, ...2 to 4 options], "recommended": <0-based index of the '
    'option you recommend>}. At most '
    f"{MAX_QUESTIONS_PER_ROUND} questions at once."
)

REVIEWER_RULE = (
    'A review may raise questions alongside findings, as "questions": [<question>, ...] in '
    "the same JSON. Never raise a blocking finding whose fix depends on the answer to an "
    "open question: ask the question instead, and the rework will carry Dan's decision."
)


def implementer_instruction() -> str:
    """How a sentinel-reporting writer asks: the QUESTION file instead of DONE."""
    return (
        BRIEF_RULE + f" To ask, write the file {QUESTION_FILE} into the WORKSPACE instead "
        'of DONE, containing {"questions": [<question>, ...]}. Commit what you have done so '
        "far first; you will be re-run with Dan's decision and continue from there."
    )


def parse(raw) -> tuple[list[dict], str]:
    """`(questions, error)`: the valid questions of a round, or why the round is unusable.

    `raw` is the decoded JSON (or its text): `{"questions": [...]}`, a list, or one bare
    question object. The round is all or nothing: one malformed question refuses it, so an
    agent is never answered on half of what it asked.
    """
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            return [], f"not JSON: {exc}"
    if isinstance(raw, dict) and "questions" in raw:
        raw = raw["questions"]
    elif isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return [], "no questions"
    if len(raw) > MAX_QUESTIONS_PER_ROUND:
        return [], f"{len(raw)} questions; at most {MAX_QUESTIONS_PER_ROUND} at once"
    questions: list[dict] = []
    for n, item in enumerate(raw, 1):
        question, error = _one(item)
        if error:
            return [], f"question {n}: {error}"
        questions.append(question)
    return questions, ""


def _one(item) -> tuple[dict, str]:
    if not isinstance(item, dict):
        return {}, "not an object"
    text = str(item.get("question") or "").strip()
    if not text:
        return {}, "no question"
    why = str(item.get("why_undefined") or "").strip()
    if not why:
        return {}, "no why_undefined"
    options = item.get("options")
    if not isinstance(options, list) or not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
        return {}, f"needs {MIN_OPTIONS}-{MAX_OPTIONS} options"
    parsed = []
    for option in options:
        if isinstance(option, str):
            option = {"label": option, "consequence": ""}
        if not isinstance(option, dict) or not str(option.get("label") or "").strip():
            return {}, "an option has no label"
        parsed.append({
            "label": str(option["label"]).strip(),
            "consequence": str(option.get("consequence") or "").strip(),
        })
    recommended = item.get("recommended")
    if isinstance(recommended, bool) or not isinstance(recommended, int) or not (
        0 <= recommended < len(parsed)
    ):
        return {}, "recommended is not an option index"
    return {
        "question": text, "why_undefined": why, "options": parsed,
        "recommended": recommended,
    }, ""


def decision_answer(question: dict, answer: str) -> str:
    """Dan's answer as the decision reads: a tapped option is its label and consequence,
    a free-text reply ("Other") is his own words."""
    answer = str(answer or "").strip()
    for option in question.get("options") or ():
        label = str(option.get("label") or "").strip()
        if label and label.lower() == answer.lower():
            consequence = str(option.get("consequence") or "").strip()
            return f"{label} ({consequence})" if consequence else label
    return answer


def normalise(text: str) -> str:
    """A question's identity for the re-ask guard: case, spacing and punctuation folded."""
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def render_decisions(decisions) -> str:
    """The binding "DAN'S DECISIONS" brief section, or "" when there are none."""
    items = [d for d in decisions or () if isinstance(d, dict) and d.get("question")]
    if not items:
        return ""
    lines = [
        "DAN'S DECISIONS (binding; they settle cases the task's text left open, and override "
        "any rule you or an earlier round inferred. Never ask them again):"
    ]
    for d in items:
        lines.append(f"- Q: {d['question']}\n  A: {d.get('answer', '')}")
    return "\n".join(lines)
