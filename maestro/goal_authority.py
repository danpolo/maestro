"""The human authority matrix: what an agent decides and what only Dan decides (P13 GDT-7).

Agents decide ordinary implementation details, retries, test selection, local debugging and
within-scope fixes, and Maestro makes routine plan adjustments within approved objectives
(`LOCAL`). New or changed goals, acceptance thresholds and final-state definitions, any goal
conflict, scope expansion, relaxing a hard constraint, security exceptions, paid operations,
destructive actions and true product questions are Dan's (`RESERVED`, `01` §11, R61).

A project's `Ask Dan:` (GOALS.md Authority) can reserve a local action as well, by keyword;
its `Autonomous:` can never grant a reserved one, and naming one there is a `draft` problem
of the contract (R09). Pure: reads only the parsed contract.
"""
from __future__ import annotations

import re
from typing import Optional

from maestro.docs import goals as doc_goals

#: Agent-decided actions -> the keywords an `Ask Dan:` line reserves them by.
LOCAL: dict[str, tuple[str, ...]] = {
    "implementation_detail": ("implementation detail", "implementation details",
                              "implementation"),
    "retry": ("retry", "retries"),
    "test_selection": ("test selection", "which tests", "selecting tests"),
    "local_debugging": ("local debugging", "debugging", "debug"),
    "within_scope_fix": ("within-scope fix", "within-scope fixes", "within scope fix",
                         "within scope fixes", "fix", "fixes"),
    "routine_plan_adjustment": ("plan adjustment", "plan adjustments", "plan change",
                                "plan changes", "replan", "replanning", "task order"),
}

#: Dan-only actions -> the phrases that find one in an `Autonomous:` line. Phrases, not bare
#: words: "resolve merge conflicts" or "delete temp files" is ordinary work (GDT-7 review).
RESERVED: dict[str, tuple[str, ...]] = {
    "goal_change": ("new goal", "new goals", "changed goal", "changed goals", "goal change",
                    "goal changes", "change goals", "changing goals"),
    "acceptance_threshold": ("acceptance threshold", "acceptance thresholds",
                             "change thresholds", "changing thresholds", "threshold change",
                             "threshold changes", "acceptance criteria",
                             "acceptance criterion", "acceptance test", "acceptance tests",
                             "test tolerance", "test tolerances",
                             *(f"{verb} {the}{noun}"
                               for verb in ("relax", "lower", "raise", "edit", "loosen",
                                            "drop", "change")
                               for the in ("", "the ")
                               for noun in ("threshold", "thresholds", "tolerance",
                                            "tolerances"))),
    "final_state_definition": ("final state", "final product", "definition of done",
                               "done criteria"),
    "goal_conflict": ("goal conflict", "goal conflicts", "conflicting goals",
                      "goal trade-off", "goal trade-offs", "goal tradeoff", "goal tradeoffs",
                      "trade-offs between goals", "tradeoffs between goals"),
    "scope_expansion": ("scope expansion", "expand scope", "expanding scope", "scope change",
                        "new feature", "new features"),
    "hard_constraint_relaxation": ("hard constraint", "hard constraints", "hard invariant",
                                   "hard invariants", "relax an invariant",
                                   "relax invariants", "relaxing invariants"),
    "security_exception": ("security exception", "security exceptions",
                           "security exemption", "security exemptions", "bypass security",
                           "disable security", "elevated privileges", "privilege escalation",
                           "credentials", "secrets"),
    "paid_operation": ("paid operation", "paid operations", "paid service", "paid services",
                       "paid api", "paid apis", "spend money", "spending money", "payment",
                       "payments", "billing", "purchase", "purchases"),
    "destructive_action": ("destructive", "delete production", "deleting production",
                           "production data", "delete data", "drop database", "drop table",
                           "force-push", "force push", "high-impact", "high impact",
                           "irreversible"),
    "product_question": ("product question", "product questions", "product decision",
                         "product decisions"),
}


def _found(text: str, keywords: tuple[str, ...]) -> str:
    """The first keyword `text` names as a whole word, or ""."""
    for keyword in keywords:
        if re.search(r"(?<![\w-])" + re.escape(keyword) + r"(?![\w-])", text or "", re.I):
            return keyword
    return ""


def requires_dan(action: str, doc: Optional[doc_goals.Goals] = None) -> tuple[bool, str]:
    """Whether `action` needs Dan's approval, and why. Unknown actions raise `ValueError`."""
    authority = dict(getattr(doc, "authority", None) or {})
    if action in RESERVED:
        granted = _found(authority.get("autonomous", ""), RESERVED[action])
        if granted:
            return True, (f"{action} is Dan's (01 §11); the contract's Autonomous names "
                          f"{granted!r}, but Autonomous can never grant a reserved action")
        return True, f"{action} is Dan's to decide (01 §11)"
    if action in LOCAL:
        reserved = _found(authority.get("ask dan", ""), LOCAL[action])
        if reserved:
            return True, f"the contract's Ask Dan reserves {action} ({reserved!r})"
        return False, f"{action} is the agent's to decide (01 §11)"
    raise ValueError(f"unknown action {action!r}; one of "
                     f"{', '.join((*LOCAL, *RESERVED))}")


def contract_problems(doc: doc_goals.Goals) -> tuple[doc_goals.Problem, ...]:
    """A reserved action the `Autonomous:` line tries to grant: a draft problem each."""
    text = (doc.authority or {}).get("autonomous", "")
    out = []
    for action, keywords in RESERVED.items():
        granted = _found(text, keywords)
        if granted:
            out.append(doc_goals.Problem(
                "draft", f"authority: Autonomous names {granted!r} ({action}), which only "
                         "Dan decides; it stays Ask Dan"))
    return tuple(out)
