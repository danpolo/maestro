"""Small, dependency-free records for graph-engineering experiments.

The experiment harness keeps raw observations separate from interpretation.  These
helpers deliberately do not decide routing or promotion policy; they only make it hard
for an evidence file to omit failed cases, blur a backend tuple, or present an inference
as a measured fact.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


CLAIM_KINDS = frozenset({"observed", "inferred", "unknown"})
RECORD_STATUSES = frozenset({"resolved", "progress", "unresolved", "blocked"})


@dataclass(frozen=True)
class BackendTuple:
    """The exact paid-agent selection used for one observation."""

    backend: str
    cli_version: str
    model: str
    effort: str

    def __post_init__(self) -> None:
        for field_name, value in asdict(self).items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"backend tuple requires a non-empty {field_name}")

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def validate_record(record: Mapping[str, Any]) -> None:
    """Raise ``ValueError`` when a blind-spot record blurs evidence provenance."""
    blind_spot_id = record.get("blind_spot_id")
    if not isinstance(blind_spot_id, str) or not blind_spot_id.startswith("B"):
        raise ValueError("record needs a blind_spot_id")
    if record.get("status") not in RECORD_STATUSES:
        raise ValueError(f"record status must be one of {sorted(RECORD_STATUSES)}")
    claims = record.get("claims")
    if not isinstance(claims, list) or not claims:
        raise ValueError("record needs at least one claim")
    for claim in claims:
        if not isinstance(claim, Mapping) or claim.get("kind") not in CLAIM_KINDS:
            raise ValueError("claim kind must be observed, inferred, or unknown")
        if not isinstance(claim.get("statement"), str) or not claim["statement"].strip():
            raise ValueError("claim needs a statement")
        evidence = claim.get("evidence")
        if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
            raise ValueError("claim evidence must be a list of references")
        if claim["kind"] == "observed" and not evidence:
            raise ValueError("an observed claim needs an evidence reference")


def weighted_score(outcomes: Sequence[Mapping[str, Any]]) -> dict[str, float | int | None]:
    """Return the fixed-weight score while retaining every failed criterion."""
    possible = 0
    earned = 0
    for outcome in outcomes:
        weight = outcome.get("weight")
        if not isinstance(weight, int) or isinstance(weight, bool) or weight <= 0:
            raise ValueError("criterion weight must be a positive integer")
        if not isinstance(outcome.get("passed"), bool):
            raise ValueError("criterion passed must be boolean")
        possible += weight
        if outcome["passed"]:
            earned += weight
    ratio = (earned / possible) if possible else None
    return {"earned": earned, "possible": possible, "ratio": ratio}


def paired_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Count raw paired wins, ties, and losses without dropping double failures."""
    summary = {"n": 0, "left_wins": 0, "ties": 0, "right_wins": 0}
    for row in rows:
        left = row.get("left")
        right = row.get("right")
        if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
            raise ValueError("paired rows need numeric left and right values")
        summary["n"] += 1
        if left > right:
            summary["left_wins"] += 1
        elif right > left:
            summary["right_wins"] += 1
        else:
            summary["ties"] += 1
    return summary


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float] | None:
    """Wilson score interval for a binary rate, or ``None`` with no observations."""
    if isinstance(successes, bool) or isinstance(total, bool):
        raise ValueError("successes and total must be integers")
    if not isinstance(successes, int) or not isinstance(total, int):
        raise ValueError("successes and total must be integers")
    if total < 0 or successes < 0 or successes > total:
        raise ValueError("require 0 <= successes <= total")
    if total == 0:
        return None
    proportion = successes / total
    denominator = 1 + (z * z / total)
    centre = (proportion + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt((proportion * (1 - proportion) / total) + z * z / (4 * total * total))
        / denominator
    )
    return max(0.0, centre - margin), min(1.0, centre + margin)


def load_case_catalog(path: Path) -> dict[str, Any]:
    """Load and minimally validate the source-controlled evaluation catalog."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("schema_version") != 1:
        raise ValueError("unsupported case catalog schema")
    tasks = document.get("tasks")
    dependency_cases = document.get("dependency_cases")
    if not isinstance(tasks, list) or not isinstance(dependency_cases, list):
        raise ValueError("catalog needs task and dependency case lists")
    task_ids = [item.get("id") for item in tasks if isinstance(item, Mapping)]
    dependency_ids = [item.get("id") for item in dependency_cases if isinstance(item, Mapping)]
    if (
        len(task_ids) != len(tasks)
        or any(not isinstance(item, str) or not item.strip() for item in task_ids)
        or len(set(task_ids)) != len(task_ids)
    ):
        raise ValueError("task ids must be present and unique")
    if (
        len(dependency_ids) != len(dependency_cases)
        or any(not isinstance(item, str) or not item.strip() for item in dependency_ids)
        or len(set(dependency_ids)) != len(dependency_ids)
    ):
        raise ValueError("dependency case ids must be present and unique")
    return document
