"""How a smoke result's numbers are named and shown.

The reference project measured retrieval quality, so every display site in the loop
spelled its headline number `Recall@5` and read it from a `recall_at_5` key that only
that project's smoke adapter ever wrote. On any other project those lines printed
`Recall@5=?` next to a perfectly good result — naming a number the project does not
measure, and reporting it as *missing* rather than as never having existed. The
`p95 latency` line beside it had the same problem, keyed to `latency_p95_ms`.

Nothing here knows a metric's name. An adapter returns whatever it measured under
`metrics`, and these helpers render it:

* `primary_name` reads `project.yaml`'s `gate.primary_metric` — until now one of the
  keys the template declared and nothing read (`tests/test_templates.py`). It is the
  metric an operator is quoted first, and `null` (the scaffold default) simply means
  this project has not nominated one yet.
* `summary` renders every metric an adapter returned, primary first, and says so
  plainly when there are none — which is the honest report for a task whose smoke was
  skipped, and for the whole D8 "unevaluated" state a fresh project starts in.

`smoke` throughout is the dict `gates._smoke_for_task` returns: `{"pass": bool,
"metrics": {...}, "reason": str}`. Every reader here tolerates a malformed one, because
these are display paths on a poll cycle: a smoke result that cannot be rendered must
never be the reason a merge is not reported.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from maestro.config import load_project_yaml

__all__ = ["primary_name", "metrics_of", "primary_value", "summary", "NO_METRICS"]

#: What `summary` says when an adapter returned nothing to show. Deliberately not an
#: error: a skipped smoke and a project with no eval harness both land here, and both
#: are ordinary states rather than failures (DESIGN.md D8).
NO_METRICS = "no metrics"


def primary_name(config: Optional[Mapping] = None) -> str:
    """`gate.primary_metric` from `project.yaml`, or `""` when none is nominated.

    `config` is injectable so a caller that already loaded the document does not read it
    twice, and so tests need no file on disk.
    """
    document = load_project_yaml() if config is None else config
    gate = document.get("gate") if isinstance(document, Mapping) else None
    name = gate.get("primary_metric") if isinstance(gate, Mapping) else None
    return str(name).strip() if isinstance(name, str) and name.strip() else ""


def metrics_of(smoke: Any) -> dict:
    """The `metrics` mapping of a smoke result, `{}` for anything unusable."""
    if not isinstance(smoke, Mapping):
        return {}
    values = smoke.get("metrics")
    return dict(values) if isinstance(values, Mapping) else {}


def primary_value(smoke: Any, config: Optional[Mapping] = None) -> str:
    """The headline number as text, or `"?"`.

    `"?"` covers both "no primary metric is configured" and "the adapter did not report
    the one that is" — from a reader's point of view those are the same thing, and the
    caller that needs to tell them apart has `primary_name` and `metrics_of`.
    """
    name = primary_name(config)
    if not name:
        return "?"
    values = metrics_of(smoke)
    return "?" if name not in values else str(values[name])


def summary(smoke: Any, config: Optional[Mapping] = None) -> str:
    """One line naming every metric the adapter reported, primary first.

    Primary first because it is the number an operator scans for; the rest follow in the
    adapter's own order, so a project that cares about their ordering gets it by choosing
    the order it writes them in. With no metrics at all this is `NO_METRICS` rather than
    an empty string, so a caller can drop it into a sentence without having to check.
    """
    values = metrics_of(smoke)
    if not values:
        return NO_METRICS
    name = primary_name(config)
    keys = list(values)
    if name in values:
        keys.remove(name)
        keys.insert(0, name)
    return " · ".join(f"{key}={values[key]}" for key in keys)
