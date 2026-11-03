"""`resolve_agent()`: the one place an agent node becomes a backend, model and effort (P6).

`03_BACKENDS_CATALOG_AND_ROUTING.md` §2 replaces "scattered model lookups across scripts"
with a single resolution. Today those lookups live in `maestro.roles` (a role's configured
backend and model) and are re-made at each launch site; this module is their graph-side
successor, and the difference that matters is not where the lookup lives but what it
produces: a whole `AgentBinding`, resolved once, *reused* by dispatch, timeouts, token
accounting and status rather than re-derived by each of them. The four small functions at
the bottom of this file (`dispatch_plan`, `execution_deadline_sec`, `token_account_key`,
`status_summary`) exist so there is exactly one way to ask each of those four questions,
and all four take the binding rather than the node — a consumer that re-resolved would
have to go out of its way to do so.

Three rules shape the pipeline, and each one is a refusal:

**Never invent a flag** (`[INV-A07]`). Effort is delivered only in a backend's own
observed vocabulary. When no level maps, `effort_support='unsupported'`, `effort_delivery`
is the catalog's own mechanism (or `'unsupported'`), `effort_native_value` is `None`, and
the selection compensates by preferring the strongest permitted base model instead —
`§2.1` step 4 exactly.

**Never fall back silently.** Every rejected route is recorded with a reason code, and a
resolution that finds nothing returns a typed `RouteUnavailable` that says *wait* (a
temporal refusal, with `retry_after_sec`) or *block* (nothing in this catalog can ever
serve this node). There is no third outcome in which a node quietly runs on an unapproved
model or a billing tier the policy did not permit.

**Never let a tie be decided by dict order.** Candidates are scored into a total order
whose last components are the backend and model ids, so the same inputs resolve to the
same binding on every host and in every process.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from hashlib import sha256
import time
from typing import Any, Mapping, Optional, Sequence

from maestro.backends.catalog import (
    MIGRATION_DEFAULT_EFFORT,
    MIGRATION_ESCALATED_EFFORT,
    MIGRATION_ROLE_EFFORTS,
    MODEL_STRENGTHS,
    CLASS_FLOORS,
    class_of,
    PERMISSION_STRENGTH,
    BackendCapabilityEntry,
    CapabilityCatalog,
    OutcomeStatistics,
    PairProfile,
    ResourcePoolState,
    ShapeKey,
    _effort_rank,
)

__all__ = [
    "REASON_CODES",
    "OBJECTIVE_BILLING",
    "AgentBinding",
    "RejectedRoute",
    "RouteUnavailable",
    "RoutingContext",
    "TaskDemand",
    "estimate_demand",
    "resolve_agent",
    "dispatch_plan",
    "DispatchPlan",
    "execution_deadline_sec",
    "token_account_key",
    "status_summary",
    "record_outcome",
]

#: Every reason a route can be rejected or a resolution refused. Closed on purpose: an
#: operator reading `maestro explain` should meet a fixed vocabulary, and a free-text
#: reason is the shape a silent fallback hides in.
REASON_CODES: frozenset[str] = frozenset(
    {
        "billing_not_permitted",
        "modality_unsupported",
        "context_overflow",
        "permission_insufficient",
        "tool_delivery_unsupported",
        "strength_below_demand",
        "intelligence_below_demand",
        "effort_below_minimum",
        "no_approved_pair",
        "model_not_permitted",
        "pool_saturated",
        "pool_paused",
        "quota_exhausted",
        "route_excluded",
        "no_eligible_route",
        "no_agent_definition",
        "empty_catalog",
        "same_or_unknown_family",
        "quota_unknown",
        "evidence_worse",
    }
)

#: Which billing categories each objective may spend by default (`§3`). A role may
#: narrow this through `permitted_backend_capabilities['billing_categories']`; nothing
#: may widen it silently, which is why the free objective does not list `subscription`:
#: `§3.1`'s hybrid overlay grants paid capacity to named high-leverage nodes explicitly,
#: and that grant is P7's, not a default here.
OBJECTIVE_BILLING: dict[str, frozenset[str]] = {
    "pace": frozenset({"subscription"}),
    "paid_efficiency": frozenset({"subscription", "pay_per_token"}),
    "free_quality": frozenset({"free_tier_api"}),
}

#: Demand rungs, weakest first — the same ladder `catalog.MODEL_STRENGTHS` grades models
#: on, so "this task needs `strong`" and "this model is `strong`" are comparable without
#: a translation table.
_STRENGTH_RANK = {name: index for index, name in enumerate(MODEL_STRENGTHS)}
MEANINGFUL_SUCCESS_ADVANTAGE = 0.10


# ── the binding (`§2.6`) ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RejectedRoute:
    """One evaluated `(backend, model)` that did not survive, and why."""

    backend_id: str
    model_id: str
    reason: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.reason not in REASON_CODES:
            raise ValueError(f"unknown rejection reason {self.reason!r}")

    def to_dict(self) -> dict:
        return {
            "backend_id": self.backend_id,
            "model_id": self.model_id,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class AgentBinding:
    """The immutable resolution of one agent node (`§2.6`).

    Resolved once and then carried: `binding_id` is what an `Attempt` stores, and the
    four consumer functions in this module all read this object rather than re-deriving
    their own answer. `rejected_routes` travels with it because `maestro explain` must be
    able to say *why not the others* long after the catalog has changed underneath.
    """

    node_id: str
    backend_id: str
    runtime_instance_id: str
    model_id: str
    effort_native_value: Optional[str]
    effort_delivery: str
    effort_support: str
    catalog_revision: str
    instruction_delivery: str
    permission_enforcement: str
    usage_pool_id: str
    billing_category: str
    selection_reason: str = ""
    rejected_routes: tuple[RejectedRoute, ...] = ()
    #: The effort of the pair that was bound (DESIGN.md §17.3), kept beside the native value
    #: it is delivered as. Empty for a pair on a harness with no effort dial.
    requested_effort: str = "medium"
    #: The shape this run's outcome is recorded against (`[INV-A07]`).
    shape_key: Optional[ShapeKey] = None
    #: The observed config key a `config_key` delivery writes; empty for a flag.
    effort_config_key: str = ""
    #: P12B F5: bound one strength rung below the demand, because every equal-or-better
    #: route was waiting and the task allows `fallback: step_down`.
    stepped_down: bool = False
    #: W2: price of the selected pair at resolution, retained by claim. Old/manual
    #: bindings remain unknown; never look up today's price to fill historical rows.
    relative_cost: Optional[float] = None
    canonical_model_id: Optional[str] = None
    model_family: Optional[str] = None
    #: W5: immutable decision inputs/results, beside rather than inside W2 measurements.
    pace_decision: Any = None
    pace_budget: Any = None

    @property
    def is_bound(self) -> bool:
        """True — the discriminator against `RouteUnavailable`, which answers False.

        A property on both types rather than an `isinstance` check at each call site:
        callers hold a union, and the union's two members should be told apart by asking
        them, not by naming their classes.
        """
        return True

    @property
    def binding_id(self) -> str:
        """A stable identity for this binding. Not a hash of the whole object — the
        rejected routes are audit, not identity, and two identical resolutions that
        evaluated a different (already-saturated) alternate are the same binding."""
        return (
            f"{self.backend_id}:{self.model_id}:"
            f"{self.effort_native_value or 'none'}@{self.catalog_revision}"
        )

    def to_dict(self) -> dict:
        document = {
            "node_id": self.node_id,
            "backend_id": self.backend_id,
            "runtime_instance_id": self.runtime_instance_id,
            "model_id": self.model_id,
            "effort_native_value": self.effort_native_value,
            "effort_delivery": self.effort_delivery,
            "effort_support": self.effort_support,
            "catalog_revision": self.catalog_revision,
            "instruction_delivery": self.instruction_delivery,
            "permission_enforcement": self.permission_enforcement,
            "usage_pool_id": self.usage_pool_id,
            "billing_category": self.billing_category,
            "selection_reason": self.selection_reason,
            "rejected_routes": [route.to_dict() for route in self.rejected_routes],
            "requested_effort": self.requested_effort,
            "shape_key": None if self.shape_key is None else self.shape_key.to_dict(),
            "effort_config_key": self.effort_config_key,
        }
        # Emitted only when set, so an unstepped binding's JSON stays byte-identical.
        if self.stepped_down:
            document["stepped_down"] = True
        if self.relative_cost is not None:
            document["relative_cost"] = self.relative_cost
        for key in ('canonical_model_id', 'model_family'):
            if getattr(self, key) is not None:
                document[key] = getattr(self, key)
        if self.pace_decision is not None:
            document['pace_decision'] = self.pace_decision.to_dict()
        if self.pace_budget is not None:
            document['pace_budget'] = self.pace_budget
        return document


@dataclass(frozen=True)
class RouteUnavailable:
    """No binding could be made — stated as a typed wait or block, never as a fallback.

    `disposition='wait'` means the refusal is temporal: a pool is saturated, paused, or
    out of quota, and `retry_after_sec` says roughly how long (`None` when the backend
    gave no reset epoch — unknown, never zero). `disposition='block'` means no route in
    this catalog can ever serve this node as specified; waiting will not help and the
    task must be parked or the policy changed.
    """

    node_id: str
    disposition: str
    reason: str
    detail: str = ""
    retry_after_sec: Optional[float] = None
    rejected_routes: tuple[RejectedRoute, ...] = ()
    #: P12B F5: for a `pool_paused` wait, whether any paused pool has a known reset.
    #: `None` when nobody asked (the router alone cannot tell; `routing.resolve` fills it).
    reset_known: Optional[bool] = None
    pace_decision: Any = None

    def __post_init__(self) -> None:
        if self.disposition not in ("wait", "block"):
            raise ValueError(f"unknown disposition {self.disposition!r}")
        if self.reason not in REASON_CODES:
            raise ValueError(f"unknown reason {self.reason!r}")

    @property
    def is_bound(self) -> bool:
        return False

    def to_dict(self) -> dict:
        document = {
            "node_id": self.node_id,
            "disposition": self.disposition,
            "reason": self.reason,
            "detail": self.detail,
            "retry_after_sec": self.retry_after_sec,
            "rejected_routes": [route.to_dict() for route in self.rejected_routes],
        }
        if self.pace_decision is not None:
            document['pace_decision'] = self.pace_decision.to_dict()
        return document


# ── what the router needs to know about the task (`§2.1` step 2) ─────────────────────


#: What raises a task to a strong model besides `risk: high` (Dan, 2026-11-02).
STRONG_TASK_CLASSES = ("difficult_debugging",)
STRONG_COMPLEXITY = 4


@dataclass(frozen=True)
class RoutingContext:
    """The slice of a `ContextManifest` routing actually consumes.

    P9 owns `ContextManifest` (`maestro/repository/context.py`) and it will carry far
    more than this. Naming the slice here, and reading a foreign manifest through
    `from_manifest`, keeps P6 testable today without either inventing P9's type or
    coupling the router to whatever shape it eventually takes: anything that exposes
    these names as attributes or mapping keys is a valid manifest for routing purposes.
    """

    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    modalities: tuple[str, ...] = ("text",)
    #: The fixed 6-class task catalog's class for this task (A03). Free text here; the
    #: router only ever uses it as part of a shape key and as a demand input.
    task_class: str = "ordinary"
    #: `high` means real outside cost (Dan, 2026-11-02). `low` never lowers the model.
    risk: str = "low"
    #: The task's 1-5 complexity forecast, `None` when nobody forecast it.
    complexity: int | None = None
    #: Verified failure rate for prior attempts at this task, `0.0` when it is new.
    historical_failure_rate: float = 0.0
    role_version: str = ""
    template_id: str = ""
    template_version: str = ""
    active_fragment_set: tuple[str, ...] = ()

    @classmethod
    def from_manifest(cls, manifest: Any) -> "RoutingContext":
        """Read a manifest that may be a mapping, a dataclass, or already one of these."""
        if isinstance(manifest, cls):
            return manifest
        if manifest is None:
            return cls()

        def read(name: str, default: Any) -> Any:
            if isinstance(manifest, Mapping):
                value = manifest.get(name, default)
            else:
                value = getattr(manifest, name, default)
            return default if value is None else value

        return cls(
            estimated_input_tokens=int(read("estimated_input_tokens", 0)),
            estimated_output_tokens=int(read("estimated_output_tokens", 0)),
            modalities=tuple(read("modalities", ("text",))),
            task_class=str(read("task_class", "ordinary")),
            risk=str(read("risk", "low")),
            complexity=read("complexity", None),
            historical_failure_rate=float(read("historical_failure_rate", 0.0)),
            role_version=str(read("role_version", "")),
            template_id=str(read("template_id", "")),
            template_version=str(read("template_version", "")),
            active_fragment_set=tuple(read("active_fragment_set", ())),
        )


@dataclass(frozen=True)
class TaskDemand:
    """What this node needs before cost is allowed an opinion (`§2.1` step 2)."""

    min_strength: str = "standard"
    min_intelligence: int = 0
    effort_floor: Optional[str] = None
    effort_preference: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    modalities: tuple[str, ...] = ("text",)
    min_permission_enforcement: str = "conventional"
    required_tool_delivery: str = ""
    permitted_models: tuple[str, ...] = ()
    reason: str = ""
    #: Risk, the task class or the failure history raised the demand to `strong`. Read
    #: only for a migrated entry's effort (DESIGN.md §17.5); a role floor does not set it.
    escalated: bool = False

    @property
    def strength_rank(self) -> int:
        return _STRENGTH_RANK[self.min_strength]


def _role_requirements(policy: Any, node: Any) -> tuple[Mapping, str]:
    """`(permitted_backend_capabilities, role_version)` for `node`'s role.

    A node with no resolvable definition yields empty requirements rather than raising:
    `resolve_agent` turns that into a `block` with `no_agent_definition`, which is the
    honest outcome — a node whose role the policy does not describe is unroutable, not
    routable-with-defaults.
    """
    ref = getattr(node, "agent_definition_ref", None)
    if not ref or policy is None:
        return {}, ""
    definitions = getattr(policy, "agent_definitions", None) or {}
    definition = definitions.get(ref)
    if definition is None:
        return {}, ""
    return (
        getattr(definition, "permitted_backend_capabilities", {}) or {},
        str(getattr(definition, "version", "") or ""),
    )


def estimate_demand(node: Any, context: RoutingContext, policy: Any = None, *,
                    role_strength: Optional[str] = None,
                    role_requirement: Optional[Mapping] = None) -> TaskDemand:
    """Task demand, before any backend is considered (`§2.1` step 2).

    The one rule this encodes that the ordering must never be allowed to violate:
    **high-risk work is never routed to a low-effort small model**. Risk, class, complexity
    and a history of failure each raise the floor, and the floor is a hard filter — the paid
    objective minimises cost *subject to* it, never through it. The role's own
    `permitted_backend_capabilities` can raise the floor further and never lower it,
    because a policy that could lower a risk-derived floor would make the floor advisory.

    The demand names a strength and no effort (DESIGN.md §17.2): effort belongs to the
    route, and a pair's class already says what the model delivers at that effort. It does
    record `escalated`, which only a migrated entry reads (§17.5).

    `role_strength` is the role's configured `strength` (§17.2). When given it is the role
    floor in place of the template's `min_reasoning_strength`, which is only its shipped
    default. Like that default it can raise the risk-derived floor and never lower it.
    """
    permitted, _ = _role_requirements(policy, node)
    reasons: list[str] = []

    floor_name = "strength" if role_strength in _STRENGTH_RANK else "min_reasoning_strength"
    declared_strength = str(role_strength if role_strength in _STRENGTH_RANK
                            else permitted.get("min_reasoning_strength") or "")
    # Only a role that says `light` gets a light model: a task's own facts never lower it.
    strength = "light" if declared_strength == "light" else "balanced"
    escalated = False
    if context.risk == "high":
        strength, escalated = "strong", True
        reasons.append("risk=high (real outside cost) demands strong reasoning")
    if context.task_class in STRONG_TASK_CLASSES:
        strength, escalated = "strong", True
        reasons.append(f"class={context.task_class} demands strong reasoning")
    if type(context.complexity) is int and context.complexity >= STRONG_COMPLEXITY:
        strength, escalated = "strong", True
        reasons.append(f"complexity={context.complexity} demands strong reasoning")
    if context.historical_failure_rate >= 0.5:
        strength, escalated = "strong", True
        reasons.append(
            f"historical failure rate {context.historical_failure_rate:.0%} escalates demand"
        )

    if declared_strength in _STRENGTH_RANK and _STRENGTH_RANK[declared_strength] > _STRENGTH_RANK[strength]:
        strength = declared_strength
        reasons.append(f"role floor {floor_name}={declared_strength}")

    declared_permission = str(permitted.get("min_permission_enforcement") or "conventional")
    if declared_permission not in PERMISSION_STRENGTH:
        declared_permission = "conventional"

    # A role's declared modalities are a requirement, not a permission: a role that
    # needs images must not be routed to a text-only backend because this task's manifest
    # happened not to mention one.
    modalities = tuple(
        sorted(set(context.modalities or ("text",)) | set(permitted.get("modalities") or ()))
    )

    requirement = role_requirement or {}
    intelligence = max(CLASS_FLOORS[strength], requirement.get("min_intelligence", 0))
    # Numerical requirements can raise the broad class too, so step-down keeps its
    # existing class semantics (50 -> balanced/40), without changing effort rules.
    strength = class_of(intelligence)
    rule = requirement.get("effort_rule")
    return TaskDemand(
        min_strength=strength,
        min_intelligence=intelligence,
        effort_floor=requirement.get("effort") if rule == "minimum" else None,
        effort_preference=requirement.get("effort") if rule == "preference" else None,
        input_tokens=context.estimated_input_tokens,
        output_tokens=context.estimated_output_tokens,
        modalities=modalities,
        min_permission_enforcement=declared_permission,
        required_tool_delivery=str(permitted.get("tool_delivery") or ""),
        permitted_models=tuple(permitted.get("models") or ()),
        reason="; ".join(reasons) or "default demand",
        escalated=escalated,
    )


# ── the pipeline (`§2.1`) ────────────────────────────────────────────────────────────


def _hybrid_grant(policy: Any, objective: str, role_id: str) -> frozenset[str]:
    """`§3.1`'s overlay: the extra billing categories one named role may spend.

    Hybrid is an eligibility overlay, not a third objective — `EXECUTION_OBJECTIVES` still
    has two members and this adds no branch to the pipeline. It applies under the free
    objective only: reserving the subscription for high-leverage nodes is a statement about
    a project that is otherwise free, and a "hybrid" reading of paid mode would just be paid
    mode with a shorter allow-list.

    Read with `getattr` throughout, because `policy` here is whatever the caller has —
    including a role-level test double that predates the block entirely. A policy with no
    `hybrid` grants nothing, which is the safe direction: the default is that free mode
    spends no paid capacity at all.
    """
    if objective != "free_quality" or not role_id:
        return frozenset()
    hybrid = getattr(policy, "hybrid", None)
    if hybrid is None:
        return frozenset()
    granted = getattr(hybrid, "grants", None)
    if callable(granted):
        return frozenset(str(category) for category in granted(role_id) or ())
    # A mapping-shaped or otherwise foreign `hybrid:` block. Same rule, read by hand.
    if role_id not in tuple(getattr(hybrid, "paid_roles", ()) or ()):
        return frozenset()
    return frozenset(
        str(category) for category in getattr(hybrid, "billing_categories", ()) or ()
    )


def _permitted_billing(policy: Any, permitted: Mapping, role_id: str = "") -> frozenset[str]:
    declared = permitted.get("billing_categories")
    if declared:
        return frozenset(str(value) for value in declared)
    objective = getattr(policy, "objective", "paid_efficiency")
    allowed = OBJECTIVE_BILLING.get(objective, OBJECTIVE_BILLING["paid_efficiency"])
    return allowed | _hybrid_grant(policy, objective, role_id)


def _hard_constraints(
    entry: BackendCapabilityEntry,
    model_id: str,
    demand: TaskDemand,
    billing: frozenset[str],
) -> Optional[RejectedRoute]:
    """`§2.1` step 1, for what is true of the model at any effort. The first violated
    constraint, or `None` when the model survives; strength is judged per pair after it."""
    def reject(reason: str, detail: str) -> RejectedRoute:
        return RejectedRoute(entry.backend_id, model_id, reason, detail)

    if entry.billing_category not in billing:
        return reject(
            "billing_not_permitted",
            f"{entry.billing_category} is not in the permitted set {sorted(billing)}",
        )
    missing = sorted(set(demand.modalities) - set(entry.modalities))
    if missing:
        return reject("modality_unsupported", f"backend cannot handle {missing}")
    limits = entry.limits_for(model_id)
    if not limits.admits(demand.input_tokens, demand.output_tokens):
        return reject(
            "context_overflow",
            f"{demand.input_tokens} in / {demand.output_tokens} out exceeds "
            f"ceiling {limits.effective_context_ceiling} / "
            f"max output {limits.max_output_tokens}",
        )
    if entry.permission_rank < PERMISSION_STRENGTH[demand.min_permission_enforcement]:
        return reject(
            "permission_insufficient",
            f"{entry.permission_enforcement} is weaker than the required "
            f"{demand.min_permission_enforcement}",
        )
    if demand.required_tool_delivery and entry.tool_delivery != demand.required_tool_delivery:
        return reject(
            "tool_delivery_unsupported",
            f"{entry.tool_delivery} is not the required {demand.required_tool_delivery}",
        )
    if demand.permitted_models and model_id not in demand.permitted_models:
        return reject(
            "model_not_permitted",
            f"role permits only {sorted(demand.permitted_models)}",
        )
    return None


@dataclass(frozen=True)
class _Candidate:
    """One surviving route, with everything the ordering reads."""

    entry: BackendCapabilityEntry
    pair: PairProfile
    effort_native: Optional[str]
    effort_support: str
    success_rate: Optional[float]

    @property
    def model_id(self) -> str:
        return self.pair.model_id

    @property
    def strength_rank(self) -> int:
        return self.pair.strength_rank

    @property
    def relative_cost(self) -> float:
        return self.pair.relative_cost

    @property
    def effort_delivered_rank(self) -> int:
        """How much effort this route delivers, `-1` on a harness with no dial. Ranked so
        such a route sorts below every other under the free objective."""
        return _effort_rank(self.pair.effort)


def _order_key(candidate: _Candidate, objective: str) -> tuple:
    """The total order candidates are ranked in — deterministic to the last component.

    Both objectives rank *measured success first among routes that satisfy the demand*,
    because a cheap route that fails is not cheap and a strong route that fails is not
    strong. `success_rate` is `None` until the evidence floor is met
    (`OutcomeStatistics.success_rate`), and an unmeasured route sorts as neutral rather
    than as bad: a new model must be tryable, or the statistics can never accumulate.
    """
    measured = candidate.success_rate if candidate.success_rate is not None else 0.5
    if objective == "free_quality":
        # Deepest capability first; cost is not a term at all (`§3`).
        return (
            -(candidate.pair.intelligence if candidate.pair.intelligence is not None
              else candidate.strength_rank),
            -candidate.effort_delivered_rank,
            -measured,
            candidate.entry.backend_id,
            candidate.model_id,
            candidate.pair.effort or "",
        )
    # Paid efficiency: cheapest route that clears the floor, then the best evidence.
    if candidate.entry.pairs_migrated:
        return (candidate.relative_cost, candidate.strength_rank, -measured,
                -candidate.effort_delivered_rank, candidate.entry.backend_id,
                candidate.model_id, candidate.pair.effort or "")
    return (
        candidate.relative_cost,
        -measured,
        -(candidate.pair.intelligence if candidate.pair.intelligence is not None
          else -candidate.strength_rank),
        candidate.entry.backend_id,
        candidate.model_id,
        candidate.pair.effort or "",
    )


def _pace_journal_builder(objective, now_epoch, node_id, demand, context,
                          independent_families, pace_runaway_mode, planner_complexity, rejected):
    """Return local journal helpers; snapshots are populated by real resolver stages."""
    pace_stages = []
    pace_pools: dict[str, dict] = {}
    pace_candidates: list[dict] = []

    def route_identity(candidate):
        return dict(backend_id=candidate.entry.backend_id, model_id=candidate.model_id,
            effort=candidate.pair.effort, effort_native=candidate.effort_native,
            pool_id=candidate.entry.pool_for(candidate.model_id),
            catalog_revision=candidate.entry.revision,
            model_family=candidate.entry.profile(candidate.model_id).model_family,
            pair=candidate.pair.to_dict())

    def pace_stage(name, status, inputs, result):
        if objective == 'pace':
            from maestro.workflows.pace import PaceStage
            pace_stages.append(PaceStage(name, status, inputs, result))

    def pace_decision(selected=None):
        if objective != 'pace':
            return None
        from maestro.workflows.pace import PaceDecision, PaceStage
        stages = list(pace_stages)
        for name in ('eligibility', 'soft_independence', 'five_hour', 'evidence', 'weekly_pace', 'budget'):
            if not any(stage.name == name for stage in stages):
                stages.append(PaceStage(name, 'pending' if name == 'budget' and selected else 'skipped',
                    result={'reason': 'atomic_admission_not_run' if name == 'budget' and selected
                            else 'no_selected_route' if name == 'budget' else 'no_upstream_survivors'}))
        return PaceDecision(now_epoch, node_id, {
            **asdict(demand), 'task_class': context.task_class,
            'independent_families': sorted(independent_families),
            'independence_relaxed': any(s.name == 'soft_independence' and s.result['relaxed']
                                       for s in pace_stages),
            'runaway_mode': pace_runaway_mode,
            'planner_complexity': (planner_complexity if not isinstance(planner_complexity, bool)
                and isinstance(planner_complexity, int) and 1 <= planner_complexity <= 5 else None),
        }, pace_pools, tuple(pace_candidates), selected, tuple(stages),
            tuple(route.to_dict() for route in rejected))

    return pace_stage, pace_decision, route_identity, pace_pools, pace_candidates


def _ordered_in_pool(candidates: Sequence[_Candidate], demand: TaskDemand,
                     objective: str, margin: float) -> list[_Candidate]:
    """Cost and preference compete only within a quota pool (DESIGN §17.5)."""
    if objective == "free_quality" or not demand.effort_preference:
        return sorted(candidates, key=lambda candidate: _order_key(candidate, objective))
    cheapest = min(candidate.relative_cost for candidate in candidates)
    band = [candidate for candidate in candidates
            if candidate.relative_cost <= cheapest * (1 + margin)]
    preferred = [candidate for candidate in band
                 if candidate.pair.effort == demand.effort_preference]
    measured_preferred = [candidate.success_rate for candidate in preferred
                          if candidate.success_rate is not None]

    def key(candidate):
        in_band = candidate in band
        preference = 1
        if in_band:
            if candidate in preferred:
                preference = 0
            elif (candidate.success_rate is not None and measured_preferred
                  and candidate.success_rate - max(measured_preferred)
                  >= MEANINGFUL_SUCCESS_ADVANTAGE - 1e-12):
                preference = -1
            return (0, preference, candidate.relative_cost,
                    -(candidate.pair.intelligence or 0), candidate.model_id,
                    candidate.pair.effort or "", candidate.entry.backend_id)
        return (1, *_order_key(candidate, objective))

    return sorted(candidates, key=key)


def pool_order_key(node_id: str, pool_id: str, pace: Optional[float] = None) -> tuple:
    """Neutral ties distribute work without using harness names as a priority."""
    return (-(pace if pace is not None else 0.0),
            sha256(f"{node_id}/{pool_id}".encode("utf-8")).hexdigest())


def _shape_key(
    entry: BackendCapabilityEntry,
    model_id: str,
    effort_native: Optional[str],
    context: RoutingContext,
    role_version: str,
) -> ShapeKey:
    """The statistics key for one route (`[INV-A07]`).

    Built from the route's parts rather than from a `_Candidate` because the candidate's
    own `success_rate` is *read* from the statistics under this key — taking the whole
    candidate would mean constructing one before its rate could be looked up.
    """
    return ShapeKey(
        backend_id=entry.backend_id,
        model_id=model_id,
        effort=effort_native or "unsupported",
        role_version=role_version,
        task_class=context.task_class,
        template_id=context.template_id,
        template_version=context.template_version,
        active_fragment_set=tuple(context.active_fragment_set),
    )


def _adjust_demand(demand: TaskDemand, offset: int, floor: Optional[str]) -> TaskDemand:
    if offset:
        rank = min(max(demand.strength_rank + offset, 0), len(MODEL_STRENGTHS) - 1)
        demand = replace(demand, min_strength=MODEL_STRENGTHS[rank],
                         min_intelligence=CLASS_FLOORS[MODEL_STRENGTHS[rank]],
                         reason=f"{demand.reason}; stepped from {demand.min_strength}")
    if floor and MODEL_STRENGTHS.index(floor) > demand.strength_rank:
        demand = replace(demand, min_strength=floor,
                         min_intelligence=max(demand.min_intelligence, CLASS_FLOORS[floor]),
                         reason=f"{demand.reason}; raised to {floor}")
    return demand


def _equivalent_shapes(catalog, entry, model_id, effort, shape):
    """Explicit canonical identity and mapped native effort, retaining A07 keys."""
    shapes = {shape.as_tuple(): shape}
    canonical = entry.profile(model_id).canonical_model_id
    if not canonical:
        return tuple(shapes.values())
    for peer, peer_model in catalog.models():
        if (peer.backend_id == entry.backend_id
                or peer.profile(peer_model).canonical_model_id != canonical):
            continue
        native, support = peer.resolve_effort(peer_model, effort)
        if support == 'unsupported' and effort is not None:
            continue
        equivalent = replace(shape, backend_id=peer.backend_id, model_id=peer_model,
                             effort=native or 'unsupported')
        shapes[equivalent.as_tuple()] = equivalent
    return tuple(shapes.values())


def _statistics_success_evidence(statistics, catalog, entry, model_id, effort, shape):
    """Aggregate evidence retains real counts/shapes; attempt identities are unknown."""
    shapes = ((shape,) if statistics.success_rate(shape) is not None else
              _equivalent_shapes(catalog, entry, model_id, effort, shape))
    inputs = []
    for key in shapes:
        record = statistics.records.get(key.as_tuple())
        if record is not None and record.attempts:
            inputs.append(dict(shape=key.to_dict(), observations=record.attempts,
                               successes=record.successes))
    attempts = sum(item['observations'] for item in inputs)
    successes = sum(item['successes'] for item in inputs)
    measured = attempts >= statistics.minimum_observations
    return dict(success_rate=successes/attempts if measured else None,
        success_source='verified_outcome_statistics', success_observations=attempts,
        success_successes=successes, success_attempt_ids=None, success_shapes=inputs,
        success_minimum_observations=statistics.minimum_observations,
        success_measured=measured)


def _success_rate(statistics, catalog, entry, model_id, effort, shape):
    """Own measured rate wins; otherwise pool only canonical equivalent A07 shapes."""
    return _statistics_success_evidence(
        statistics, catalog, entry, model_id, effort, shape)['success_rate']


def resolve_agent(
    node: Any,
    context_manifest: Any,
    resources: Optional[ResourcePoolState] = None,
    catalog: Optional[CapabilityCatalog] = None,
    policy: Any = None,
    *,
    statistics: Optional[OutcomeStatistics] = None,
    now_epoch: Optional[float] = None,
    trace: Any = None,
    exclude: frozenset = frozenset(),
    strength_offset: int = 0,
    min_strength_floor: Optional[str] = None,
    billing: Optional[frozenset[str]] = None,
    role_strength: Optional[str] = None,
    role_requirement: Optional[Mapping] = None,
    near_cost_margin: float = 0.20,
    independent_families: frozenset[str] = frozenset(),
    pace_estimates: Optional[Mapping] = None,
    pace_history: Sequence[Mapping] = (),
    pace_runaway_mode: str = 'off',
    planner_complexity: Optional[int] = None,
    pace_sprints: Optional[Mapping] = None,
) -> "AgentBinding | RouteUnavailable":
    """Resolve one agent node to a concrete backend, model and effort (`§2`).

    A candidate is one approved, deliverable `(model, effort)` pair (DESIGN.md §17.3).
    Pools rank by weekly pace, then a stable node/pool hash (§17.5). An entry whose pairs
    were migrated from model grades offers each model at two efforts with one class and
    cost; the role then binds the effort it used before (`MIGRATION_ROLE_EFFORTS`, or `high`
    when risk or failure history raised the demand), except under the free objective, which
    never rationed effort. `role_strength` is the role's
    configured floor (`estimate_demand`).

    The five steps run in the spec's order, and the order is load-bearing: demand is
    estimated before any cost comparison, so the paid objective minimises spend *within*
    the quality floor rather than negotiating it down; and admission runs last, so a pool
    being momentarily full produces a `wait` on a route that was otherwise qualified,
    never a `block` and never a quiet hop to a different billing tier.

    `trace` is an optional `maestro.metrics.BaselineTrace`. When given, the decision is
    recorded as an observed fact keyed by `metrics.binding_identity`, so a loop that
    resolves the same node twice shows up as a duplicate in the same baseline that
    counts duplicate agent invocations.

    `exclude` holds `(backend_id, model_id)` routes this node must not use again (P12B
    F4: a context overflow excludes the route, not the pool). An excluded route is a
    hard-constraint rejection, so excluding every route blocks rather than waits.

    `strength_offset` moves the estimated `min_strength` that many rungs (floor `light`)
    and `billing` replaces the objective's permitted categories, with no hybrid grant
    (P12B F5 step-down: a weaker model, never a costlier billing tier). A role that
    declares its own categories still bounds the override.
    """
    from maestro import metrics  # local: metrics imports config, this module must stay inert

    node_id = str(getattr(node, "node_id", "") or "")
    context = RoutingContext.from_manifest(context_manifest)
    resources = resources if resources is not None else ResourcePoolState()
    now_epoch = time.time() if now_epoch is None else now_epoch
    statistics = statistics if statistics is not None else OutcomeStatistics()

    if catalog is None or not catalog.entries:
        return RouteUnavailable(
            node_id=node_id,
            disposition="block",
            reason="empty_catalog",
            detail="no backend is catalogued; nothing can be resolved",
        )

    permitted, role_version = _role_requirements(policy, node)
    if getattr(node, "agent_definition_ref", None) and not role_version:
        return RouteUnavailable(
            node_id=node_id,
            disposition="block",
            reason="no_agent_definition",
            detail=(
                f"node {node_id} names role "
                f"{getattr(node, 'agent_definition_ref', None)!r}, which the policy "
                "does not define — a node whose role is undescribed is unroutable"
            ),
        )
    role_version = role_version or str(context.role_version or "")

    # 2. demand, before cost has an opinion
    demand = estimate_demand(node, context, policy, role_strength=role_strength,
                             role_requirement=role_requirement)
    migration_demand = estimate_demand(node, context, policy, role_strength=role_strength)
    demand = _adjust_demand(demand, strength_offset, min_strength_floor)
    migration_demand = _adjust_demand(migration_demand, strength_offset, min_strength_floor)
    if billing is None:
        billing = _permitted_billing(
            policy, permitted, str(getattr(node, "agent_definition_ref", "") or "")
        )
    else:
        declared = permitted.get("billing_categories")
        billing = frozenset(billing)
        if declared:
            billing &= frozenset(str(value) for value in declared)

    objective = getattr(policy, "objective", "paid_efficiency")
    if objective == "free_quality":
        migration_effort = None
    elif demand.escalated:
        migration_effort = MIGRATION_ESCALATED_EFFORT
    else:
        migration_effort = MIGRATION_ROLE_EFFORTS.get(
            str(getattr(node, "agent_definition_ref", "") or ""), MIGRATION_DEFAULT_EFFORT)

    # 1. hard constraints
    rejected: list[RejectedRoute] = []
    survivors: list[_Candidate] = []
    pace_stage, pace_decision, route_identity, pace_pools, pace_candidates = _pace_journal_builder(
        objective, now_epoch, node_id, demand, context, independent_families,
        pace_runaway_mode, planner_complexity, rejected)
    independence_relaxed = False
    for entry, model_id in catalog.models():
        if (entry.backend_id, model_id) in exclude:
            rejected.append(RejectedRoute(
                entry.backend_id, model_id, "route_excluded",
                "excluded for this node after a context overflow",
            ))
            continue
        rejection = _hard_constraints(entry, model_id, demand, billing)
        if rejection is not None:
            rejected.append(rejection)
            continue
        pairs = entry.pairs(model_id)
        if not pairs:
            rejected.append(RejectedRoute(
                entry.backend_id, model_id, "no_approved_pair",
                "no approved pair of this model can be delivered on this harness",
            ))
            continue
        # 3./4. the pair is the joint model & effort choice, and its effort is one the
        # backend actually showed — `entry.pairs` drops any it cannot deliver.
        if entry.pairs_migrated and any(pair.effort == migration_effort for pair in pairs):
            # The same migrated model, read at the effort another role used, is dropped.
            # A harness that cannot deliver this role's effort keeps what it does deliver:
            # the migration must not take a route away.
            pairs = tuple(pair for pair in pairs if pair.effort == migration_effort)
        for pair in pairs:
            if not entry.pairs_migrated and pair.intelligence is not None:
                if pair.intelligence < demand.min_intelligence:
                    rejected.append(RejectedRoute(
                        entry.backend_id, model_id, "intelligence_below_demand",
                        f"{model_id}@{pair.effort}: {pair.intelligence} < "
                        f"{demand.min_intelligence} ({demand.reason})"))
                    continue
                if demand.effort_floor and _effort_rank(pair.effort) < _effort_rank(demand.effort_floor):
                    rejected.append(RejectedRoute(
                        entry.backend_id, model_id, "effort_below_minimum",
                        f"{model_id}@{pair.effort} < minimum {demand.effort_floor}"))
                    continue
            pair_demand = migration_demand if entry.pairs_migrated else demand
            if pair.strength_rank < pair_demand.strength_rank:
                rejected.append(RejectedRoute(
                    entry.backend_id, model_id, "strength_below_demand",
                    f"{model_id} at {pair.effort or 'no'} effort is {pair.strength}, below "
                    f"the demanded {pair_demand.min_strength} ({pair_demand.reason})",
                ))
                continue
            native, support = entry.resolve_effort(model_id, pair.effort)
            shape = _shape_key(entry, model_id, native, context, role_version)
            survivors.append(
                _Candidate(
                    entry=entry,
                    pair=pair,
                    effort_native=native,
                    effort_support=support,
                    success_rate=_success_rate(statistics, catalog, entry, model_id,
                                               pair.effort, shape),
                )
            )

    if objective == 'pace':
        pace_stage('eligibility', 'passed' if survivors else 'blocked', {
            'catalog': catalog.to_dict(), 'context': asdict(context), 'demand': asdict(demand),
            'migration_demand': asdict(migration_demand), 'billing': sorted(billing),
            'exclude': sorted(exclude), 'role_version': role_version,
            'role_requirement': dict(role_requirement or {}),
        }, {'survivors': [route_identity(c) for c in survivors],
            'rejected_routes': [route.to_dict() for route in rejected]})
    if not survivors:
        return RouteUnavailable(
            node_id=node_id,
            disposition="block",
            reason="no_eligible_route",
            detail=(
                f"every catalogued route violates a hard constraint for {node_id} "
                f"({demand.reason})"
            ),
            rejected_routes=tuple(rejected),
            pace_decision=pace_decision(),
        )

    independent_inputs = [route_identity(c) for c in survivors]
    independence_rejected_at = len(rejected)
    if independent_families:
        different = [c for c in survivors if '*' not in independent_families
                     and c.entry.profile(c.model_id).model_family
                     and c.entry.profile(c.model_id).model_family not in independent_families]
        if different:
            rejected.extend(RejectedRoute(c.entry.backend_id, c.model_id,
                                          'same_or_unknown_family',
                                          'another eligible model family is available')
                            for c in survivors if c not in different)
            survivors = different
        else:
            independence_relaxed = True
    pace_stage('soft_independence', 'passed', {
        'candidates': independent_inputs, 'independent_families': sorted(independent_families),
    }, {'relaxed': independence_relaxed, 'survivors': [route_identity(c) for c in survivors],
        'rejected_routes': [r.to_dict() for r in rejected[independence_rejected_at:]]})

    # W5: the independent-family frontier is fixed before testing headroom. A
    # blocked independent route must not silently relax the quality preference.
    pace_waits: list[tuple[float | None, str, str]] = []
    if objective == 'pace':
        from maestro.workflows.pace import (PaceEstimate, five_hour_gate,
                                           estimate_from_history, class_evidence)
        from maestro.sprint import weekly_pace

        gated = []
        evidence_by_route = {}
        for candidate in survivors:
            entry = candidate.entry
            pool_id = entry.pool_for(candidate.model_id)
            estimates = pace_estimates or {}
            estimate = estimates.get(pool_id, estimates.get('*', PaceEstimate()))
            estimate = estimate_from_history(
                pace_history, shape=_shape_key(entry, candidate.model_id,
                    candidate.effort_native, context, role_version), pool_id=pool_id,
                initial=estimate, now_epoch=now_epoch,
                minimum_observations=statistics.minimum_observations)
            state = resources.pool(pool_id, concurrency_limit=entry.concurrency_limit)
            observation = state.observation
            # W3's Opus constraint belongs to the Claude account only. A reseller
            # serving the same canonical model has its own independent quota pool.
            scopes = (('opus',) if pool_id == 'claude-subscription'
                      and 'opus' in (entry.profile(candidate.model_id).canonical_model_id
                                     or candidate.model_id).split('-') else ())
            answer = five_hour_gate(observation, now_epoch=now_epoch,
                                     relative_cost=candidate.relative_cost,
                                     estimate=estimate, hard_limit_pct=resources.pause_pct,
                                     scopes=scopes)
            shape = _shape_key(entry, candidate.model_id, candidate.effort_native, context, role_version)
            evidence = class_evidence(pace_history, shape=shape,
                pool_id=pool_id, minimum_observations=statistics.minimum_observations,
                runaway_mode=pace_runaway_mode, planner_complexity=planner_complexity,
                equivalent_shapes=_equivalent_shapes(catalog, entry, candidate.model_id,
                                                      candidate.pair.effort, shape))
            band_compared = pace_runaway_mode != 'off' and type(planner_complexity) is int \
                and 1 <= planner_complexity <= 5
            if (evidence['success_rate'] is None and not band_compared
                    and candidate.success_rate is not None):
                evidence.update(_statistics_success_evidence(statistics, catalog, entry,
                    candidate.model_id, candidate.pair.effort, shape))
            evidence_by_route[(entry.backend_id, candidate.model_id, candidate.pair.effort)] = evidence
            pace_pools.setdefault(pool_id, {
                'observation': observation.to_dict() if observation else None,
                'gate': answer.reason if not answer.admitted else 'admitted',
                'weekly_pace': weekly_pace(observation, now_epoch=now_epoch),
            })
            # A cheaper pair can fit the same pool when an expensive one does not.
            if answer.admitted:
                pace_pools[pool_id]['gate'] = 'admitted'
                gated.append(candidate)
            else:
                rejected.append(RejectedRoute(entry.backend_id, candidate.model_id,
                                              answer.reason, answer.detail))
                pace_waits.append((answer.retry_after_sec, answer.reason, answer.detail))
            pace_candidates.append({
                'backend_id': entry.backend_id, 'model_id': candidate.model_id,
                'effort': candidate.pair.effort, 'pool_id': pool_id,
                'catalog_revision': entry.revision,
                'relative_cost': candidate.relative_cost, 'estimate': estimate.to_dict(),
                'success_rate': candidate.success_rate,
                'evidence': evidence,
                'applicable_scopes': scopes,
                'five_hour': 'admitted' if answer.admitted else answer.reason,
                'five_hour_detail': answer.detail,
                'five_hour_retry_after_sec': answer.retry_after_sec,
            })
        survivors = gated
        pace_stage('five_hour', 'passed' if survivors else 'wait', {
            'pools': pace_pools, 'candidates': pace_candidates,
            'hard_limit_pct': resources.pause_pct, 'now_epoch': now_epoch,
        }, {'survivors': [route_identity(c) for c in survivors],
            'route_results': [{key: c[key] for key in ('backend_id', 'model_id', 'effort',
                'five_hour', 'five_hour_detail', 'five_hour_retry_after_sec')} for c in pace_candidates]})
        evidence_input = [route_identity(c) for c in survivors]
        evidence_rejected_at = len(rejected)
        def evidence_for(candidate):
            return evidence_by_route[(candidate.entry.backend_id, candidate.model_id, candidate.pair.effort)]
        measured = [evidence_for(c)['success_rate'] for c in survivors
                    if evidence_for(c)['success_rate'] is not None]
        best = max(measured) if measured else None
        quality_frontier = []
        for candidate in survivors:
            rate = evidence_for(candidate)['success_rate']
            if (best is not None and rate is not None
                    and best - rate >= MEANINGFUL_SUCCESS_ADVANTAGE - 1e-12):
                rejected.append(RejectedRoute(
                    candidate.entry.backend_id, candidate.model_id, 'evidence_worse',
                    f'measured class success {rate:g} trails best {best:g}'))
            else:
                quality_frontier.append(candidate)
        # A route that lost on quality cannot eliminate its quality winner on
        # cost. Compare recorded costs only within the surviving pool frontier.
        best_cost = {}
        for candidate in quality_frontier:
            evidence = evidence_for(candidate)
            if evidence['success_per_cost'] is not None:
                pool = candidate.entry.pool_for(candidate.model_id)
                best_cost[pool] = max(best_cost.get(pool, 0), evidence['success_per_cost'])
        guarded = []
        for candidate in quality_frontier:
            evidence = evidence_for(candidate)
            cost_best = best_cost.get(candidate.entry.pool_for(candidate.model_id))
            worse_cost = (cost_best is not None and evidence['success_per_cost'] is not None
                          and evidence['success_per_cost'] < cost_best*(1-MEANINGFUL_SUCCESS_ADVANTAGE))
            if worse_cost:
                rejected.append(RejectedRoute(
                    candidate.entry.backend_id, candidate.model_id, 'evidence_worse',
                    'measured class success per recorded cost trails its pool'))
            else:
                guarded.append(candidate)
        survivors = guarded
        pace_stage('evidence', 'passed' if evidence_input else 'skipped', {
            'candidates': evidence_input,
            'coverage': [dict(backend_id=c['backend_id'], model_id=c['model_id'],
                              effort=c['effort'], evidence=c['evidence']) for c in pace_candidates
                         if c['five_hour'] == 'admitted'],
            'meaningful_success_advantage': MEANINGFUL_SUCCESS_ADVANTAGE,
        }, {'best_success_rate': best, 'best_cost_by_pool': best_cost,
            'quality_frontier': [route_identity(c) for c in quality_frontier],
            'survivors': [route_identity(c) for c in survivors],
            'rejected_routes': [r.to_dict() for r in rejected[evidence_rejected_at:]],
            'reason': None if evidence_input else 'no_five_hour_survivors'})

    pools: dict[str, list[_Candidate]] = {}
    for candidate in survivors:
        pools.setdefault(candidate.entry.pool_for(candidate.model_id), []).append(candidate)
    ordered = {pool: _ordered_in_pool(candidates,
                                     migration_demand if all(c.entry.pairs_migrated for c in candidates)
                                     else demand, objective, near_cost_margin)
               for pool, candidates in pools.items()}
    paces = {}
    observed_paces = {}
    sprint_targets = {}
    from copy import deepcopy
    sprints = deepcopy(dict(pace_sprints or {})) if objective == 'pace' else {}
    for pool in pools:
        state = resources.pool(pool, concurrency_limit=pools[pool][0].entry.concurrency_limit)
        paces[pool] = state.observation.pace_delta(now_epoch) if state.observation else None
        if objective == 'pace':
            paces[pool] = weekly_pace(state.observation, now_epoch=now_epoch)
        observed_paces[pool] = paces[pool]
        if objective == 'pace':
            from maestro.sprint import pace_target
            target = pace_target(pool, state.observation, sprints.get(pool), now_epoch=now_epoch)
            sprint_targets[pool] = target
            if target is not None:
                paces[pool] = round(target-state.observation.weekly.used_pct, 1)
    pool_order = sorted(ordered, key=lambda pool: pool_order_key(node_id, pool, paces[pool]))
    survivors = [candidate for pool in pool_order for candidate in ordered[pool]]
    admission_trials = []
    weekly_inputs = dict(pool_paces=paces, pool_order=pool_order,
        ordered_routes=[route_identity(c) for c in survivors], near_cost_margin=near_cost_margin,
        pool_order_keys={pool: pool_order_key(node_id, pool, paces[pool]) for pool in pool_order})
    if objective == 'pace':
        weekly_inputs.update(observed_pool_paces=observed_paces, sprints=sprints,
                             sprint_targets=sprint_targets)

    def record_weekly(selected=None):
        pace_stage('weekly_pace', 'passed' if selected else 'wait' if survivors else 'skipped',
            weekly_inputs, {'selected': selected, 'pool_admission_trials': admission_trials,
                            'reason': None if selected else 'no_admissible_route'})

    # 5. admission. A saturated pool does not disqualify a route: it sends the resolution
    # to the next equally qualified alternate, and only a fully saturated frontier waits.
    waits: list[tuple[float | None, str, str]] = list(pace_waits)
    for candidate in survivors:
        entry = candidate.entry
        pool_id = entry.pool_for(candidate.model_id)
        resources.pool(pool_id, concurrency_limit=entry.concurrency_limit)
        state = resources.pool(pool_id)
        trial = dict(route=route_identity(candidate), active=state.active,
            concurrency_limit=state.concurrency_limit, paused_until=state.paused_until,
            pause_reason=state.pause_reason)
        admission = resources.admit(pool_id, now_epoch=now_epoch)
        admission_trials.append({**trial, 'admission': asdict(admission)})
        if not admission.admitted:
            rejected.append(
                RejectedRoute(entry.backend_id, candidate.model_id, admission.reason,
                              admission.detail)
            )
            waits.append((admission.retry_after_sec, admission.reason, admission.detail))
            continue

        shape = _shape_key(entry, candidate.model_id, candidate.effort_native,
                           context, role_version)
        reason_parts = [
            f"objective={objective}",
            f"demand={demand.min_strength} ({demand.reason})",
            f"pair {candidate.model_id}@{candidate.pair.effort or 'none'} "
            f"strength={candidate.pair.strength} cost={candidate.pair.relative_cost:g}",
            f"effort_support={candidate.effort_support}",
        ]
        if entry.pairs_migrated:
            reason_parts[1] = f"demand={migration_demand.min_strength} ({migration_demand.reason})"
        if independence_relaxed:
            reason_parts.append('independence_relaxed: no eligible different model family')
        pace = paces[pool_id]
        reason_parts.append(f"pool={pool_id} pace={pace:.1f}" if pace is not None
                            else f"pool={pool_id} pace=unknown")
        if candidate.effort_support == "unsupported":
            reason_parts.append(
                entry.effort_controls.unsupported_reason
                or "backend exposes no usable effort vocabulary; best base model selected"
            )
        if candidate.success_rate is not None:
            reason_parts.append(f"measured success={candidate.success_rate:.0%}")

        selected = route_identity(candidate)
        record_weekly(selected)
        binding = AgentBinding(
            node_id=node_id,
            backend_id=entry.backend_id,
            runtime_instance_id=entry.instance_id,
            model_id=candidate.model_id,
            effort_native_value=candidate.effort_native,
            effort_delivery=(
                entry.effort_delivery_for(candidate.model_id)
                if candidate.effort_support != "unsupported"
                else "unsupported"
            ),
            effort_support=candidate.effort_support,
            catalog_revision=entry.revision,
            instruction_delivery=entry.instruction_delivery,
            permission_enforcement=entry.permission_enforcement,
            usage_pool_id=pool_id,
            billing_category=entry.billing_category,
            selection_reason="; ".join(reason_parts),
            rejected_routes=tuple(rejected),
            requested_effort=candidate.pair.effort or "",
            relative_cost=candidate.pair.relative_cost,
            canonical_model_id=entry.profile(candidate.model_id).canonical_model_id,
            model_family=entry.profile(candidate.model_id).model_family,
            pace_decision=pace_decision(selected),
            shape_key=shape,
            effort_config_key=(
                entry.effort_controls.config_key
                if candidate.effort_support != "unsupported"
                else ""
            ),
        )
        if trace is not None:
            trace.observe(
                metrics.ROUTE_RESOLVED,
                detail=binding.selection_reason,
                identity=metrics.binding_identity(binding),
            )
        return binding

    # Every qualified route was admissible-in-principle and full in practice. That is a
    # wait, with the soonest known reset — and explicitly not a downgrade to a route that
    # failed the hard constraints above.
    known = [seconds for seconds, _, _ in waits if seconds is not None]
    reason = waits[0][1] if waits else "pool_saturated"
    record_weekly()
    unavailable = RouteUnavailable(
        node_id=node_id,
        disposition="wait",
        reason=reason,
        detail="; ".join(detail for _, _, detail in waits) or "every qualified pool is full",
        retry_after_sec=min(known) if known else None,
        rejected_routes=tuple(rejected),
        pace_decision=pace_decision(),
    )
    if trace is not None:
        trace.observe(
            metrics.ROUTE_UNAVAILABLE,
            detail=f"{unavailable.disposition}:{unavailable.reason}",
            identity=f"{node_id}:{unavailable.reason}",
        )
    return unavailable


# ── the one binding, used by all four consumers (`§3` acceptance) ────────────────────


@dataclass(frozen=True)
class DispatchPlan:
    """How a resolved binding becomes an actual invocation.

    `effort_args` and `effort_config` are mutually exclusive and both empty when effort
    is unsupported — which is the whole of `[INV-A07]` at the point it matters. A driver
    appends `effort_args` to its argv or writes `effort_config` into its config
    overrides; neither is ever synthesised here from a canonical label the backend never
    showed.
    """

    binding: AgentBinding
    model: str
    effort_args: tuple[str, ...] = ()
    effort_config: tuple[tuple[str, str], ...] = ()


def dispatch_plan(binding: AgentBinding, *, effort_key: Optional[str] = None) -> DispatchPlan:
    """The invocation form of `binding`. The only place effort becomes argv or config.

    A `config_key` delivery writes the key the register observed for that backend
    (`binding.effort_config_key`); `effort_key` overrides it, and `reasoning_effort` is the
    fallback only for a hand-built entry that recorded none.
    """
    effort_key = effort_key or binding.effort_config_key or "reasoning_effort"
    if binding.effort_support == "unsupported" or binding.effort_native_value is None:
        return DispatchPlan(binding=binding, model=binding.model_id)
    if binding.effort_delivery == 'model_variant':
        return DispatchPlan(binding=binding, model=binding.effort_native_value)
    if binding.effort_delivery == "flag":
        return DispatchPlan(
            binding=binding,
            model=binding.model_id,
            effort_args=("--effort", binding.effort_native_value),
        )
    if binding.effort_delivery == "config_key":
        return DispatchPlan(
            binding=binding,
            model=binding.model_id,
            effort_config=((effort_key, binding.effort_native_value),),
        )
    return DispatchPlan(binding=binding, model=binding.model_id)


def execution_deadline_sec(binding: AgentBinding, node: Any) -> int:
    """The wall-clock budget for this bound attempt.

    Takes the binding as well as the node so that the timeout is asked of the *resolved*
    attempt and not of the node in the abstract — the acceptance condition is that one
    binding governs dispatch, timeouts, accounting and status, and a timeout helper that
    ignored the binding would quietly reintroduce a second source of truth.
    """
    policy = getattr(node, "timeout_policy", None)
    return int(getattr(policy, "execution_timeout_sec", 4 * 3600))


def token_account_key(binding: AgentBinding) -> str:
    """Where this attempt's usage is charged: the pool, then the shape within it."""
    return f"{binding.usage_pool_id}/{binding.backend_id}/{binding.model_id}"


def status_summary(binding: AgentBinding) -> str:
    """One operator-facing line naming what is running and under what effort."""
    if binding.effort_support == "unsupported":
        effort = "effort unsupported"
    else:
        effort = f"effort {binding.effort_native_value} ({binding.effort_support})"
    return f"{binding.backend_id}/{binding.model_id} · {effort} · pool {binding.usage_pool_id}"


def record_outcome(
    statistics: OutcomeStatistics, binding: AgentBinding, *, success: bool
) -> None:
    """Fold one *controller-verified* outcome into the statistics under the binding's own
    shape key (`[INV-A07]`).

    Takes the binding rather than a loose tuple so the shape recorded is provably the
    shape resolved — an outcome filed under a shape nobody ran would poison every later
    ranking, and D06 already forbids trusting an agent's own account of how it went.
    """
    if binding.shape_key is not None:
        statistics.record(binding.shape_key, success=success)
