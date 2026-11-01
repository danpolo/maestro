"""Where a review finding came from, and whose miss it is (Dan, 2026-09-26).

Dan wants more quality work in fewer cycles, measured in subscription usage per accepted
task. Every review round after the first is a cycle someone's miss paid for; to find each
role's root causes, a round-2+ blocking finding names its `origin`, and this module's one
table maps the origin to the role at fault. The reviewer never states the fault — it
states what it observed (was this in a prior finding? in its scope? in the rework diff?),
and the table, which can be revised, does the blaming.

Each finding gets a stable id, `r<round>-f<n>` (n counts the reviewer's findings in the
order it listed them), which later rounds see in their PRIOR REVIEW ROUNDS and cite as
`prior_finding`. A finding whose round is unknown is `f<n>`.

Nothing here can fail a review: an unknown or missing origin in round 2+ is recorded as
`unclassified` and flagged, and the review stands on its verdict and findings alone.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from types import MappingProxyType

__all__ = [
    "DIAGNOSIS_FAULT",
    "FAULTS",
    "ORIGINS",
    "ORIGIN_FAULT",
    "PRIOR_LINKED",
    "UNATTRIBUTED",
    "UNCLASSIFIED",
    "count",
    "finding_id",
    "normalise",
    "summary",
]

UNCLASSIFIED = "unclassified"
#: An observed cause no evidence has established a fault for (GDT-12). Distinct from
#: `unclassified`, which is an unknown or malformed cause.
UNATTRIBUTED = "unattributed"

# Review findings retain their existing vocabulary. The table below is also the
# single revisable mapping for W2 extra-step observations; dynamic rules are resolved
# only from recorded context by telemetry.extra_step, never an agent's fault claim.
REVIEW_ORIGINS = (
    "not_fixed", "fixed_incompletely_in_scope", "fixed_incompletely_out_of_scope",
    "missed_earlier", "introduced_by_rework", "exposed_by_rework", "scope_widened",
    UNCLASSIFIED,
)

#: The one fault table. `scope_widened` is a reviewer-or-planner signal (the requirement
#: grew after round 1); it is counted as `planner` because a requirement a later round can
#: widen is one the task's criteria did not pin down — the reviewer rows already count the
#: cases where the earlier review, not the plan, is what fell short.
CAUSE_FAULT: Mapping[str, str] = MappingProxyType({
    "not_fixed": "implementer",
    "fixed_incompletely_in_scope": "implementer",
    "fixed_incompletely_out_of_scope": "reviewer",
    "missed_earlier": "reviewer",
    "introduced_by_rework": "implementer",
    "exposed_by_rework": "none",
    "scope_widened": "planner",
    "check_failed": "implementer",
    "uncommitted_tree": "implementer",
    "check_broken": "planner",
    "environment_missing": "maestro",
    "garbage_output": "$role",
    "no_sentinel": "$role",
    "worker_crashed": "harness",
    "quota": "$gate",
    "rate_limit": "$gate",
    "endpoint": "$gate",
    "poor_agent": "$rebriefed_role",
    "bad_graph": "planner",
    "spec_gap": "planner",
    "deny_approved": "maestro",
    "deny_disapproved": "implementer",
    "permission_allowed": "maestro",
    "permission_declined": "$role",
    "manual_redo": "prep",
    "brief_check_disagree": "planner",
    "accept_rejected_review": "reviewer",
    "accept_rejected_plan": "planner",
    "escaped_defect": "reviewer",
    "parallel_scope_overlap": "planner",
    # Mapping v2 (GDT-12, R58/R62): a served goal's miss is observed, never certified as a
    # planner fault; W12's attribution amends it from evidence. W11a's v1 rows keep their
    # captured `planner` as provenance (the report remaps them, no migration).
    "task_did_not_serve_goal": UNATTRIBUTED,
    "runaway_false_alarm": "maestro",
    "controller": "maestro",
    UNCLASSIFIED: UNCLASSIFIED,
})
ORIGINS = REVIEW_ORIGINS
# Compatibility view, derived from the single cause table rather than separately
# maintained. A review may not use unrelated extra-step causes as finding origins.
ORIGIN_FAULT: Mapping[str, str] = MappingProxyType({key: CAUSE_FAULT[key] for key in ORIGINS})
FAULTS = ("implementer", "reviewer", "planner", "none", UNCLASSIFIED, UNATTRIBUTED)
#: GDT-12 (R58): a reviewed diagnosis's first line `Cause: <token>` to the fault it
#: establishes. Revisable like `CAUSE_FAULT`. A harness or measurement defect is never
#: planner blame; `unknown` establishes nothing.
DIAGNOSIS_FAULT: Mapping[str, str] = MappingProxyType({
    "plan": "planner", "scope": "planner", "goal_mapping": "planner",
    "dependency": "none", "constraint": "none", "harness": "none", "measurement": "none",
    "agent": "implementer",
    "unknown": UNATTRIBUTED,
})
#: The origins that answer a prior finding, and so name it in `prior_finding`.
PRIOR_LINKED = frozenset({
    "not_fixed", "fixed_incompletely_in_scope", "fixed_incompletely_out_of_scope",
})


def finding_id(round_no: int | None, n: int) -> str:
    return f"r{round_no}-f{n}" if round_no else f"f{n}"


def _token(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def normalise(
    item, *, fid: str, severity: str, round_no: int | None,
    prior_ids: Sequence[str] = (),
) -> tuple[str | None, str | None, list[str]]:
    """`(origin, prior_finding, flags)` for one finding as the reviewer wrote it.

    Required only of a blocking finding in round 2+, where a missing or unknown origin is
    `unclassified` and flagged. Elsewhere a valid origin is kept and anything else dropped.
    A prior-linked origin whose `prior_finding` is missing or names no earlier finding keeps
    its origin (the reviewer's judgement of the class stands) and is flagged.
    """
    raw = item.get("origin") if isinstance(item, Mapping) else None
    origin = _token(raw)
    prior = item.get("prior_finding") if isinstance(item, Mapping) else None
    prior = prior.strip().lower() if isinstance(prior, str) and prior.strip() else None
    required = severity == "blocking" and bool(round_no) and round_no >= 2
    flags: list[str] = []
    if origin not in ORIGINS or origin == UNCLASSIFIED:
        if required:
            flags.append(f"{fid}: {'no origin' if origin is None else f'unknown origin {raw!r}'}")
            origin = UNCLASSIFIED
        else:
            origin = None
    if origin in PRIOR_LINKED:
        if prior is None:
            flags.append(f"{fid}: {origin} names no prior_finding")
        elif prior_ids and prior not in prior_ids:
            flags.append(f"{fid}: prior_finding {prior!r} is not an earlier finding")
    else:
        prior = None
    return origin, prior, flags


def count(records: Iterable[Mapping]) -> dict:
    """A round's (or a task's) blocking findings, counted by origin and by fault.

    Only findings that carry an origin are counted in the two maps, so a first round's
    findings (no origin asked) add to `blocking` alone.
    """
    blocking = 0
    by_origin: dict[str, int] = {}
    by_fault: dict[str, int] = {}
    for record in records:
        if record.get("severity") != "blocking":
            continue
        blocking += 1
        origin = record.get("origin")
        if origin is None:
            continue
        fault = ORIGIN_FAULT.get(origin, UNCLASSIFIED)
        by_origin[origin] = by_origin.get(origin, 0) + 1
        by_fault[fault] = by_fault.get(fault, 0) + 1
    return {"blocking": blocking, "origin": by_origin, "fault": by_fault}


def summary(rounds: Sequence[tuple[int, Sequence[Mapping]]]) -> str:
    """The origin distribution of rounds 2+, one line per round and a total, for a brief."""
    lines = []
    total: list[Mapping] = []
    for round_no, records in rounds:
        if round_no < 2:
            continue
        counts = count(records)
        if not counts["blocking"]:
            continue
        total += [r for r in records if r.get("origin")]
        origins = ", ".join(
            f"{k} {v} ({ORIGIN_FAULT.get(k, UNCLASSIFIED)})"
            for k, v in sorted(counts["origin"].items())
        )
        lines.append(f"Round {round_no}: {counts['blocking']} blocking — {origins or 'no origins'}")
    if not lines:
        return ""
    faults = count(total)["fault"]
    lines.append("By fault: " + ", ".join(
        f"{fault} {faults[fault]}" for fault in FAULTS if faults.get(fault)
    ))
    return "\n".join(lines)
