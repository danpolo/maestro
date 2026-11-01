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


# ───────────────────────────────────────────────────────────────────────────────
# P0 baseline instrumentation (graph-engineering migration)
#
# Everything above renders a smoke result for an operator. Everything below measures
# the *execution loop itself*, and it lives here because both halves answer the same
# question — "what did this run actually produce?" — for two different audiences.
#
# The migration baseline (docs/graph-engineering/specs/phases/P00_MIGRATION_BASELINE.md)
# needs numbers that survive being re-read a year from now, so two properties are built
# into the recorder rather than left to its callers:
#
#   * **Nothing is timed by wall clock.** A trace advances only when a caller says how
#     much time passed (`waited`/`computed`). A fixture that sleeps for real produces a
#     different number on every machine, and a baseline that moves is not a baseline.
#   * **Every fact carries how it was learned.** `observe` is for something the harness
#     watched happen at a seam; `infer` is for something concluded from surrounding
#     evidence. INV-04 and D06 both turn on that distinction, so the recorder refuses to
#     store a fact without it. `summary()` reports the two counts side by side, which is
#     how a later reader can tell a measured baseline from a plausible one.
# ───────────────────────────────────────────────────────────────────────────────

from dataclasses import dataclass, field  # noqa: E402  (section boundary, see above)

__all__ += [
    "OBSERVED",
    "INFERRED",
    "BaselineEvent",
    "BaselineTrace",
    "AGENT_CALL",
    "TEST_RUN",
    "STATE_READ",
    "STATE_WRITE",
    "ROUTE_RESOLVED",
    "ROUTE_UNAVAILABLE",
    "binding_identity",
]

#: A fact the harness watched happen at an instrumented seam.
OBSERVED = "observed"
#: A fact concluded from surrounding evidence rather than watched. Never proof (D06).
INFERRED = "inferred"

#: Event kinds the baseline metrics are computed from. A harness may record any other
#: kind it likes — they appear in the trace and in `by_kind` — but these four are the
#: ones `summary()` reduces into the headline counters, so they are named constants
#: rather than loose strings at each call site.
AGENT_CALL = "agent_call"
TEST_RUN = "test_run"
STATE_READ = "state_read"
STATE_WRITE = "state_write"

#: P6 routing decisions. Recorded on the same trace as the four kinds above, so that a
#: loop resolving the same node twice shows up beside the duplicate *agent calls* that
#: second resolution would go on to make — `duplicate_agent_invocations` counts the paid
#: symptom, and this counts the decision that caused it.
ROUTE_RESOLVED = "route_resolved"
ROUTE_UNAVAILABLE = "route_unavailable"


def binding_identity(binding: object) -> str:
    """The identity a routing decision is compared under (`BaselineEvent.identity`).

    Two resolutions are the same decision when they bind the same node to the same
    backend, model and effort against the same catalog revision — which is exactly what
    `AgentBinding.binding_id` already means, so this reads that rather than reassembling
    it from fields and inviting the two spellings to drift apart. The node id leads,
    because the wasteful repeat this is meant to expose is *one node* resolved twice; two
    nodes landing on the same backend is ordinary.

    Duck-typed rather than importing `maestro.backends.router`: this module is imported
    by the baseline harness and by display paths, and neither should pay for the routing
    package. Anything without a `binding_id` yields `""`, which `BaselineTrace` already
    treats as "not comparable" rather than as a duplicate of every other such event.
    """
    identity = getattr(binding, "binding_id", "")
    if not identity:
        return ""
    node_id = getattr(binding, "node_id", "") or "?"
    return f"{node_id}|{identity}"


@dataclass(frozen=True)
class BaselineEvent:
    """One thing that happened during a lane run.

    `identity` is what makes duplicate detection possible: two agent calls are the same
    call when their identity matches, and it is the *caller* that decides what that means
    (task + role + brief digest, say) because only the caller knows which differences are
    meaningful. An empty identity means "not comparable", and such events are never
    counted as duplicates of anything.
    """

    kind: str
    detail: str = ""
    evidence: str = OBSERVED
    identity: str = ""
    #: Sequence number within its trace. Not a timestamp — see the section note above.
    seq: int = 0


@dataclass
class BaselineTrace:
    """An append-only record of one lane run, plus the clock the run reports to it.

    Deliberately not a context manager and not global: a lane harness builds one, hands
    it to the doubles it installs at each seam, and reads it back when the run ends. Two
    runs of the same lane build two traces and the test compares them, which is the
    reproducibility check P0's acceptance condition asks for.
    """

    lane: str = ""
    events: list = field(default_factory=list)
    idle_wait_seconds: float = 0.0
    active_compute_seconds: float = 0.0

    # ── recording ──────────────────────────────────────────────────────────────
    def record(self, kind: str, detail: str = "", *, evidence: str,
               identity: str = "") -> BaselineEvent:
        """Append one event. Prefer `observe`/`infer`, which fix `evidence` for you."""
        if evidence not in (OBSERVED, INFERRED):
            raise ValueError(
                f"evidence must be {OBSERVED!r} or {INFERRED!r}, got {evidence!r}. "
                "A baseline fact with no stated provenance cannot be used as proof (D06)."
            )
        event = BaselineEvent(kind=kind, detail=detail, evidence=evidence,
                              identity=identity, seq=len(self.events))
        self.events.append(event)
        return event

    def observe(self, kind: str, detail: str = "", *, identity: str = "") -> BaselineEvent:
        """Record something the harness watched happen at a seam."""
        return self.record(kind, detail, evidence=OBSERVED, identity=identity)

    def infer(self, kind: str, detail: str = "", *, identity: str = "") -> BaselineEvent:
        """Record something concluded rather than watched. Reported separately, always."""
        return self.record(kind, detail, evidence=INFERRED, identity=identity)

    def waited(self, seconds: float) -> None:
        """Time the loop spent doing nothing but waiting (a poll sleep, an async wait)."""
        self.idle_wait_seconds += float(seconds)

    def computed(self, seconds: float) -> None:
        """Time the loop spent doing work (an agent turn, a test run, a merge)."""
        self.active_compute_seconds += float(seconds)

    # ── reduction ──────────────────────────────────────────────────────────────
    def of_kind(self, kind: str) -> list:
        return [event for event in self.events if event.kind == kind]

    def _repeats(self, kind: str) -> int:
        """How many events of `kind` repeat an identity already seen in this trace.

        The first occurrence of an identity is the work; every later one is the waste.
        Events with an empty identity are skipped rather than lumped together — see
        `BaselineEvent.identity`.
        """
        seen: set = set()
        repeats = 0
        for event in self.of_kind(kind):
            if not event.identity:
                continue
            if event.identity in seen:
                repeats += 1
            else:
                seen.add(event.identity)
        return repeats

    @property
    def duplicate_agent_invocations(self) -> int:
        """Agent calls the loop made twice for the same work. Each one is paid quota."""
        return self._repeats(AGENT_CALL)

    @property
    def unnecessary_test_reruns(self) -> int:
        """Test runs repeating a command against an unchanged tree."""
        return self._repeats(TEST_RUN)

    def summary(self) -> dict:
        """The five P0 baseline measures, plus the provenance split behind them.

        Returned as a plain dict so `scripts/graph_baseline.py` can write it to JSON and a
        later phase can diff two baselines without importing anything from here.
        """
        observed = sum(1 for event in self.events if event.evidence == OBSERVED)
        by_kind: dict = {}
        for event in self.events:
            by_kind[event.kind] = by_kind.get(event.kind, 0) + 1
        return {
            "lane": self.lane,
            "events": len(self.events),
            "by_kind": by_kind,
            "duplicate_agent_invocations": self.duplicate_agent_invocations,
            "unnecessary_test_reruns": self.unnecessary_test_reruns,
            "idle_wait_seconds": round(self.idle_wait_seconds, 6),
            "active_compute_seconds": round(self.active_compute_seconds, 6),
            "state_reads": len(self.of_kind(STATE_READ)),
            "state_writes": len(self.of_kind(STATE_WRITE)),
            "observed_facts": observed,
            "inferred_facts": len(self.events) - observed,
        }

    def signature(self) -> list:
        """The trace reduced to what a reproducibility check may compare.

        Kind, evidence and identity only: `detail` carries session ids and timestamps
        that differ between two runs of the same lane *by design*, so comparing it would
        report every lane as non-reproducible and prove nothing.
        """
        return [(event.kind, event.evidence, event.identity) for event in self.events]
