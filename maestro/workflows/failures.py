"""The failure vocabulary every graph attempt is classified into (P12B F0).

Dan's policy (D-FAIL-1/2) turns on one distinction: did the model **fail to work**
(`blocked`: a rate limit, an outage, a credential), or did it **work badly** (`bad_output`)?
The first is routed around and never spends a retry; the second is retried, then escalated.
Everything downstream — pausing a pool, choosing the next binding, counting retries — reads
the class, so the vocabulary is closed: a kind string not listed here is refused rather than
accepted and silently treated as one or the other.

A failure nobody anticipated is `unknown`, which is `bad_output`. Its raw evidence is kept,
so Dan's free-model pilots can name it later (`maestro workflow failures --kind unknown`).
A harness update that rewords an error degrades to `unknown`; it never fails a gate.

This module also holds the one CLI table the phase allows (`classify_exit`): it reads only
what the drivers' own `parse_exit` already decided, and every miss is `unknown`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping, Optional

__all__ = [
    "BLOCKED",
    "BAD_OUTPUT",
    "QUALITY_ATTEMPTS",
    "ROLE_DIAGNOSER",
    "ROLE_REVIEWER",
    "retry_cone",
    "FAILURE_CLASSES",
    "FAILURE_KINDS",
    "VERIFICATION_HANDLERS",
    "FailureRecord",
    "classify_node_result",
    "classify_exit",
    "record_from_runtime",
    "pause_for",
    "BACKOFF_CAP_SEC",
]

BLOCKED = "blocked"
BAD_OUTPUT = "bad_output"
FAILURE_CLASSES: tuple[str, ...] = (BLOCKED, BAD_OUTPUT)

FAILURE_KINDS: Mapping[str, str] = MappingProxyType({
    "rate_limited": BLOCKED,
    "quota_exhausted": BLOCKED,
    "provider_unavailable": BLOCKED,
    "timeout": BLOCKED,
    "auth_failed": BLOCKED,
    "context_overflow": BLOCKED,
    # The controller died inside a non-idempotent node and its postcondition probe proved
    # the effect absent (A05): nothing was judged, so the re-run spends no budget.
    "interrupted": BLOCKED,
    # P12 pilot finding D26: an agent asked Dan about a case the spec never defined. The
    # node waits on his answer and spends no retry (`WorkflowRunner._ask_dan`).
    "dan_question": BLOCKED,
    # P13 W8: the operator stopped the node (`ctl cancel` / `ctl retry`). Nothing it
    # produced was judged, so no model is blamed and no retry is spent.
    "operator_canceled": BLOCKED,
    "operator_retry": BLOCKED,
    "empty_output": BAD_OUTPUT,
    "malformed_tool_calls": BAD_OUTPUT,
    "garbage_output": BAD_OUTPUT,
    # P13 W8: the agent answered and exited cleanly but reported nothing (no sentinel, no
    # result document). Its work cannot be read, so it is run again like any bad output.
    "no_sentinel": BAD_OUTPUT,
    "verification_failed": BAD_OUTPUT,
    "worker_crashed": BAD_OUTPUT,
    # P13 W8: a handler raised, so nobody judged the node's work. Never retried
    # (`retry_cone`): the same code would raise again, and no model is at fault.
    "controller_error": BAD_OUTPUT,
    "unknown": BAD_OUTPUT,
})

#: Handlers whose failure *is* a failed check rather than a failed model.
VERIFICATION_HANDLERS = frozenset({
    "maestro.handlers.verification",
    "maestro.handlers.acceptance",
    # Dan rejecting a `kind: dan` check is a failed check on the implementer's work.
    "maestro.handlers.dan_confirm",
})

#: G7: a manual lane's human step. Dan's Redo is a verdict on the prep agent's work (a
#: cone back to it); his Abandon, or anything else, is not retried.
HUMAN_HANDLER = "maestro.handlers.human"

EVIDENCE_LIMIT = 2000

#: P12B F6: how many times a node may produce a `bad_output` before it is written
#: `failed` — the first run plus two retries.
QUALITY_ATTEMPTS = 3

#: The role whose own bad output is never retried: retrying a diagnosis with the same
#: inputs is the loop the escalation exists to break.
ROLE_DIAGNOSER = "diagnoser"

#: The role whose rejection is a verdict on the implementer's work (P12 pilot finding D6).
ROLE_REVIEWER = "reviewer"


def _fails_to_cooldown(revision: Any, node_id: str) -> bool:
    """Whether `node_id`'s failure edge leads to a resumable lane's `cooldown` step."""
    for edge in revision.outgoing(node_id):
        if edge.guard_predicate != "on_failure":
            continue
        if revision.node(edge.target_node_id).params.get("cooldown_hours") is not None:
            return True
    return False


def retry_cone(revision: Any, node_id: str, *, kind: str = "") -> tuple[str, ...]:
    """The nodes a `bad_output` on `node_id` re-runs, in topological order (P12B F6).

    Three shapes, and the empty tuple means "not retried — write `failed` as today":

    * an agent node other than a diagnoser answers for its own output, so its cone is
      itself — except a reviewer's rejection (`kind="verification_failed"`, P12 pilot
      finding D6), which is a verdict on the implementer's work and takes the next shape;
    * a verification node's output is a *verdict on someone else's* work, so retrying the
      check alone would re-run the same failing tree. Its cone reaches back to the nearest
      upstream agent writer and includes every node on a path between them, the gate
      itself last, so the work is redone and then re-checked;
    * a manual lane's human step whose failure is Dan's Redo (`kind="verification_failed"`,
      G7) is a verdict on the prep, and takes the verification shape too;
    * everything else — a script, a merge, a join, an abandoned human step, a diagnoser,
      and any node whose handler raised (`kind="controller_error"`) — has no model to
      blame and is left to the `failed` path.

    Failure edges are not followed: they describe where a failure *goes*, never where the
    work came from, and following one would pull a sink into the cone.

    The cone's first element is its root — the node whose counters the ladder spends.
    """
    node = revision.node(node_id)
    if kind == "controller_error":
        return ()
    if node.kind == "agent":
        if node.agent_definition_ref == ROLE_DIAGNOSER:
            return ()
        if node.agent_definition_ref != ROLE_REVIEWER or kind != "verification_failed":
            return (node_id,)
    elif node.handler_ref == HUMAN_HANDLER:
        if kind != "verification_failed":
            return ()
    elif node.handler_ref not in VERIFICATION_HANDLERS:
        return ()
    elif _fails_to_cooldown(revision, node_id):
        # G7 1c: a resumable task's failing gate is an incomplete sitting, not bad work.
        # Legacy ran one sitting per cooldown; re-running the writer at once would spend
        # the quota the cooldown waits out and skew the stall count.
        return ()

    order = revision.topological_order()
    rank = {name: index for index, name in enumerate(order)}

    def sources(target: str) -> list[str]:
        return [
            edge.source_node_id for edge in revision.incoming(target)
            if edge.guard_predicate != "on_failure"
        ]

    # Breadth-first backwards: the first writer reached is the nearest one, and among
    # equals the earliest in topological order wins, so the cone is deterministic.
    seen = {node_id}
    frontier = sorted(sources(node_id), key=lambda name: rank.get(name, 0))
    root = ""
    while frontier and not root:
        for name in frontier:
            candidate = revision.node(name)
            if candidate.kind == "agent" and candidate.wants_writer_lease:
                root = name
                break
        seen.update(frontier)
        if root:
            break
        frontier = sorted(
            {s for name in frontier for s in sources(name) if s not in seen},
            key=lambda name: rank.get(name, 0),
        )
    if not root:
        return ()

    # Everything on a path from the root to the gate: reachable forwards from the root
    # and backwards from the gate.
    forward = {root}
    for name in order:
        if name in forward:
            continue
        if any(source in forward for source in sources(name)):
            forward.add(name)
    backward = {node_id}
    pending = [node_id]
    while pending:
        for source in sources(pending.pop()):
            if source not in backward:
                backward.add(source)
                pending.append(source)
    cone = (forward & backward) | {root, node_id}
    return tuple(name for name in order if name in cone)


@dataclass(frozen=True)
class FailureRecord:
    """One classified failure: what kind, which class, and the evidence it was read from.

    `retry_after_sec` and `reset_at` (Zulu ISO) are what the failure itself reported about
    when the model can be tried again; `None` means it said nothing, never "now".
    """

    kind: str
    failure_class: str = ""
    evidence: str = ""
    retry_after_sec: Optional[int] = None
    reset_at: Optional[str] = None

    def __post_init__(self) -> None:
        if self.kind not in FAILURE_KINDS:
            raise ValueError(
                f"unknown failure kind {self.kind!r} (expected one of {sorted(FAILURE_KINDS)})"
            )
        expected = FAILURE_KINDS[self.kind]
        if not self.failure_class:
            object.__setattr__(self, "failure_class", expected)
        elif self.failure_class != expected:
            raise ValueError(
                f"failure kind {self.kind!r} is {expected!r}, not {self.failure_class!r}"
            )
        object.__setattr__(self, "evidence", str(self.evidence or "")[:EVIDENCE_LIMIT])
        if self.retry_after_sec is not None:
            object.__setattr__(self, "retry_after_sec", max(0, int(self.retry_after_sec)))
        if self.reset_at is not None:
            object.__setattr__(self, "reset_at", str(self.reset_at) or None)

    @property
    def is_blocked(self) -> bool:
        return self.failure_class == BLOCKED

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "failure_class": self.failure_class,
            "evidence": self.evidence,
            "retry_after_sec": self.retry_after_sec,
            "reset_at": self.reset_at,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "FailureRecord":
        if isinstance(raw, FailureRecord):
            return raw
        if not isinstance(raw, Mapping):
            raise ValueError(f"a failure record is an object, not {type(raw).__name__}")
        return cls(
            kind=str(raw.get("kind") or ""),
            failure_class=str(raw.get("failure_class") or ""),
            evidence=str(raw.get("evidence") or ""),
            retry_after_sec=raw.get("retry_after_sec"),
            reset_at=raw.get("reset_at"),
        )


def classify_node_result(result: Any, *, handler_ref: str = "") -> Optional[FailureRecord]:
    """The failure a `NodeResult` reports, or `None` when it is not a failure.

    `uncertain` is not a verdict — it is reconciliation's to decide — so only `failed` is
    classified. A worker's own `outputs["failure"]` wins; an unknown kind there raises
    (`ValueError`) so the caller can decide how to keep the raw value.
    """
    if getattr(result, "status", None) != "failed":
        return None
    outputs = result.outputs if isinstance(result.outputs, Mapping) else {}
    if outputs.get("failure") is not None:
        return FailureRecord.from_dict(outputs["failure"])
    kind = "verification_failed" if handler_ref in VERIFICATION_HANDLERS else "unknown"
    return FailureRecord(kind=kind, evidence=str(result.detail or ""))


# ── the CLI table ────────────────────────────────────────────────────────────────────

#: Within a driver's `quota_exhausted` verdict: wording that means a short-lived rate limit
#: rather than a spent quota. The claude CLI's "monthly spend limit" notice arrives as a
#: 429 and resets, so it is a rate limit with a reset.
_RATE_LIMIT_RE = re.compile(
    r"rate[\s-]?limit|too many requests|\b429\b|spend limit|overloaded", re.I
)

#: `ExitVerdict.kind` → failure kind. `ok` is not a failure; anything else is `unknown`.
_EXIT_KINDS: Mapping[str, str] = MappingProxyType({
    "quota_exhausted": "quota_exhausted",
    "crashed": "worker_crashed",
})


def classify_exit(verdict: Any) -> Optional[FailureRecord]:
    """A driver's `ExitVerdict` as a failure, or `None` for a clean exit."""
    verdict_kind = str(getattr(verdict, "kind", "") or "")
    evidence = str(getattr(verdict, "evidence", "") or "")
    reset_at = getattr(verdict, "reset_at", None) or None
    if verdict_kind == "ok":
        return None
    kind = _EXIT_KINDS.get(verdict_kind)
    if kind is None:
        return FailureRecord(kind="unknown", evidence=f"exit verdict {verdict_kind!r}: {evidence}")
    if kind == "quota_exhausted" and _RATE_LIMIT_RE.search(evidence):
        kind = "rate_limited"
    return FailureRecord(kind=kind, evidence=evidence, reset_at=reset_at)


def record_from_runtime(outcome: Any) -> Optional[FailureRecord]:
    """A `ModelAgentRuntime` outcome as a failure, or `None` when it did not fail."""
    if getattr(outcome, "status", None) != "failed":
        return None
    stop_reason = str(getattr(outcome, "stop_reason", "") or "")
    detail = str(getattr(outcome, "detail", "") or "")
    if stop_reason in FAILURE_KINDS:
        return FailureRecord(
            kind=stop_reason,
            evidence=detail,
            retry_after_sec=getattr(outcome, "retry_after_sec", None),
            reset_at=getattr(outcome, "reset_at", None),
        )
    return FailureRecord(kind="unknown", evidence=f"{stop_reason}: {detail}")


#: The longest a guessed (unknown-reset) pause may run before the pool is tried again.
BACKOFF_CAP_SEC = 1800


def _zulu_epoch(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def pause_for(
    record: FailureRecord, *, now_epoch: float, prior_blocks: int, auth_pause_sec: int,
    reset_recovered: bool = False,
) -> Optional[tuple[int, bool]]:
    """How long a `blocked` failure pauses its pool: `(until_epoch, reset_known)`.

    `None` means the pool is not paused at all — a context overflow is the route's
    problem, not the pool's. A reported reset or retry-after is known; otherwise the pause
    is an exponential backoff on the node's earlier blocks, capped, and marked unknown.
    """
    now = int(now_epoch)
    if record.kind in ("context_overflow", "dan_question"):
        return None
    if record.kind == "auth_failed":
        return now + int(auth_pause_sec), True
    if record.kind == "quota_exhausted" and reset_recovered:
        # The account sample after this attempt proves the exhausted window rolled
        # over and all measured windows are below 100%. The CLI's undated time is
        # stale; use a short unknown-reset backoff instead of tomorrow's clock time.
        return now + min(60 * 2 ** max(0, int(prior_blocks)), BACKOFF_CAP_SEC), False
    reset = _zulu_epoch(record.reset_at)
    if reset is not None:
        return reset, True
    if record.retry_after_sec is not None:
        return now + int(record.retry_after_sec), True
    return now + min(60 * 2 ** max(0, int(prior_blocks)), BACKOFF_CAP_SEC), False
