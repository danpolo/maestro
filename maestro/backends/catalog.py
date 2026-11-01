"""The backend capability catalog, quota accounting and empirical outcome statistics (P6).

`03_BACKENDS_CATALOG_AND_ROUTING.md` §1 asks for one place that answers "what can this
backend actually do?", so that role definitions stop naming vendors and `router.py` can
resolve a node against capabilities instead of against a configured string. Three things
in here are load-bearing and none of them is a restatement of the spec's YAML:

**Nothing is claimed that was not observed.** An entry may be built by hand, but the
supported path is `catalog_from_register()`, which reads `maestro/thirdparty.json` —
the version-pinned verification register `maestro doctor --thirdparty` already gates
(`[INV-12]`). `effort_controls_from_findings` is the sharp end of `[INV-A07]`'s "never
invent a flag": a row that records `--effort` in `controls_observed` but never exercised
its values yields `supported=False, delivery_mechanism='unsupported'`, because the flag's
*existence* is not the same fact as its *vocabulary*, and a router that guessed
`--effort=high` from the former would be inventing the latter. The claude row on this
host is exactly that case, and B06's own open question says so.

**A window is keyed by its duration, never by a name.** `QuotaObservation` inherits G5
from `maestro.backends.base`: one sampled account has no five-hour window at all, so
`windows` is `{window_minutes: QuotaWindow}` and `five_hour` is a lookup that may return
`None`. `max_used_pct()` is what a pause threshold should read.

**A reading carries when it was taken, when it was written, and whether it was carried
forward.** `[INV-A07]` again: a usage sample that a backend *pushes* (a statusline
payload maestro does not control) can be arbitrarily older than the moment maestro
recorded it, and a pool paused on a stale reading is a pool paused on a guess.
`observed_at`/`written_at`/`staleness_bound`/`carried_forward` make the difference
legible, and `is_stale()` answers it rather than leaving each caller to subtract
timestamps its own way. `carried_forward=True` means "this is the previous reading,
re-dated" — never a fresh measurement, and `fresh_enough()` refuses it once the bound
has passed rather than aging it silently.

Everything here is inert: no subprocess, no network, no filesystem write at import.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

__all__ = [
    "CatalogError",
    "RUNTIME_KINDS",
    "BILLING_CATEGORIES",
    "EFFORT_DELIVERY",
    "EFFORT_SUPPORT",
    "MODALITIES",
    "TOOL_DELIVERY",
    "INSTRUCTION_DELIVERY",
    "STRUCTURED_OUTPUT_SUPPORT",
    "PERMISSION_ENFORCEMENT",
    "PERMISSION_STRENGTH",
    "TELEMETRY_QUALITY",
    "MODEL_STRENGTHS",
    "CLASS_FLOORS",
    "class_of",
    "CANONICAL_EFFORTS",
    "ACQUISITION_PATHS",
    "ModelProfile",
    "PairProfile",
    "migrate_pairs",
    "MIGRATION_EFFORTS",
    "MIGRATION_ROLE_EFFORTS",
    "MIGRATION_DEFAULT_EFFORT",
    "MIGRATION_ESCALATED_EFFORT",
    "EffortControls",
    "ContextLimits",
    "BackendCapabilityEntry",
    "CapabilityCatalog",
    "effort_controls_from_findings",
    "catalog_from_register",
    "ANTIGRAVITY_CATALOG_DEFAULTS",
    "CLAUDE_CATALOG_DEFAULTS",
    "CODEX_CATALOG_DEFAULTS",
    "SHIPPED_CATALOG_DEFAULTS",
    "PIN_ONLY_MODELS",
    "QuotaWindow",
    "RESET_JITTER_SEC",
    "QuotaObservation",
    "PoolState",
    "ResourcePoolState",
    "Admission",
    "ShapeKey",
    "OutcomeRecord",
    "OutcomeStatistics",
    "utc_now_iso",
]


class CatalogError(ValueError):
    """A catalog entry that could not be believed. Always raised at construction."""


# ── the vocabularies (`§1`) ──────────────────────────────────────────────────────────

RUNTIME_KINDS = frozenset({"agent_cli", "model_endpoint_harness"})
BILLING_CATEGORIES = frozenset({"subscription", "free_tier_api", "pay_per_token"})
EFFORT_DELIVERY = frozenset({"flag", "config_key", "unsupported"})
#: How the *binding's* effort value relates to the backend's own vocabulary (`§2.6`).
EFFORT_SUPPORT = frozenset({"native", "mapped", "unsupported"})
MODALITIES = frozenset({"text", "image"})
TOOL_DELIVERY = frozenset({"native_cli_tools", "json_schema_dispatch", "prompt_convention"})
INSTRUCTION_DELIVERY = frozenset({"system_flag", "profile_file", "appended_prompt"})
STRUCTURED_OUTPUT_SUPPORT = frozenset({"json_mode", "tool_call", "regex_bounded"})
PERMISSION_ENFORCEMENT = frozenset({"physical_sandbox", "directory_isolation", "conventional"})

#: Enforcement is ordered: a role that requires directory isolation is satisfied by a
#: physical sandbox and never by convention. Ranked here once so `router.py` compares
#: numbers instead of re-deriving the order at each comparison.
PERMISSION_STRENGTH: dict[str, int] = {
    "conventional": 0,
    "directory_isolation": 1,
    "physical_sandbox": 2,
}

TELEMETRY_QUALITY = frozenset({"token_exact", "window_percentage", "unobserved"})

#: The reasoning-strength ladder a role's `min_reasoning_strength` is stated in, weakest
#: first, spelled exactly as the shipped role definitions spell it
#: (`maestro/templates/roles/*.yaml` say `balanced` and `strong`). Deliberately coarse:
#: three rungs are what a policy can state honestly about a model it has not benchmarked,
#: and `OutcomeStatistics` is where finer judgement comes from once there is evidence.
MODEL_STRENGTHS: tuple[str, ...] = ("light", "balanced", "strong")

#: The lowest pair intelligence (0 to 100) of each class (DESIGN.md §17.3). A schema 2
#: policy row states the intelligence; the class is always derived from it, never stored.
CLASS_FLOORS: dict[str, int] = {"light": 0, "balanced": 40, "strong": 48}

#: Maestro's own effort vocabulary, weakest first. A backend's native vocabulary is its own
#: (`EffortControls.native_values`) and the two are only ever connected by a mapping a
#: backend row actually declares — never by string equality across vendors (B06).
CANONICAL_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

#: How a quota reading reached maestro (`[INV-A07]`). `push`: the backend writes a
#: payload maestro reads (a statusline sample) and maestro does not control its age.
#: `pull`: maestro asked, so `observed_at` is the moment it asked.
ACQUISITION_PATHS = frozenset({"push", "pull"})


def utc_now_iso() -> str:
    """Now, as the Zulu ISO-8601 string every timestamp in this module is written in."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(text: str) -> Optional[datetime]:
    """Parse a Zulu ISO-8601 timestamp, or `None` for anything unusable.

    Tolerant on purpose: these strings come from backend payloads written by processes
    maestro does not own, and a malformed one must degrade to "unknown age" — which
    `is_stale` treats as stale — never raise on a poll cycle.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _tuple_of_str(values: Any, *, field_name: str, subject: str) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str) or not isinstance(values, Iterable):
        raise CatalogError(f"{subject}: {field_name} must be a list of strings")
    return tuple(str(value) for value in values)


# ── capability entries (`§1`) ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelProfile:
    """Verified native facts for a concrete model id: capacity and quota pool.

    Routing judgments belong to `PairProfile`, including historical schema 1 grades.
    A native profile alone never approves a route.
    """

    model_id: str
    #: Per-model context limits, when they differ from the backend's own. `None` means
    #: "the entry's limits apply", which is the common case for a single-model CLI.
    context_limits: Optional["ContextLimits"] = None
    #: The quota pool this model bills against, when it differs from the backend's own.
    #: `None` means "the entry's pool applies" — see `BackendCapabilityEntry.pool_for`.
    #: A backend that resells another vendor's models can bill them separately, and
    #: attributing those calls to the host backend's pool would both understate the
    #: reseller pool and overstate the host's.
    usage_pool_id: Optional[str] = None
    #: False means neither this profile nor the backend default establishes native
    #: capacity for this model. Context-limits None otherwise retains its usual
    #: documented meaning of verified backend-default inheritance.
    native_context_verified: bool = True
    canonical_model_id: Optional[str] = None
    model_family: Optional[str] = None
    #: None uses the harness dial; a mapping permits only its exact observed variants.
    effort_variants: Optional[Mapping[str, str]] = None

    def __post_init__(self) -> None:
        if not self.model_id:
            raise CatalogError("model profile has no model_id")
        if type(self.native_context_verified) is not bool:
            raise CatalogError(f"{self.model_id}: native_context_verified must be boolean")
        for value in (self.canonical_model_id, self.model_family):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise CatalogError(f'{self.model_id}: invalid canonical identity')
        if self.effort_variants is not None and (
                not isinstance(self.effort_variants, Mapping)
                or any(k not in CANONICAL_EFFORTS or not isinstance(v, str) or not v.strip()
                       for k, v in self.effort_variants.items())):
            raise CatalogError(f'{self.model_id}: invalid effort variants')

    def to_dict(self) -> dict:
        result = {
            "model_id": self.model_id,
            "context_limits": None if self.context_limits is None else self.context_limits.to_dict(),
            "usage_pool_id": self.usage_pool_id,
            "native_context_verified": self.native_context_verified,
        }
        for key in ('canonical_model_id', 'model_family', 'effort_variants'):
            value = getattr(self, key)
            if value is not None:
                result[key] = dict(value) if key == 'effort_variants' else value
        return result


#: The efforts a model graded without one is read at (DESIGN.md §17.5): the two the roles
#: used before a route named its effort. A harness with no dial reads it at `None`.
MIGRATION_EFFORTS: tuple[str, ...] = ("medium", "high")

#: Which of those a role used, so a migrated model keeps the effort each role ran it at.
MIGRATION_ROLE_EFFORTS: dict[str, str] = {"planner": "high", "diagnoser": "high"}
MIGRATION_DEFAULT_EFFORT = "medium"
#: What any role ran at when risk or failure history raised the task's demand.
MIGRATION_ESCALATED_EFFORT = "high"


def _effort_rank(effort: Optional[str]) -> int:
    """Where an effort sorts: no effort first, then the canonical ladder, then the rest."""
    if effort is None:
        return -1
    if effort in CANONICAL_EFFORTS:
        return CANONICAL_EFFORTS.index(effort)
    return len(CANONICAL_EFFORTS)


def class_of(intelligence: int) -> str:
    """The class a pair intelligence falls in: the highest `CLASS_FLOORS` floor it reaches."""
    if type(intelligence) is not int or not 0 <= intelligence <= 100:
        raise CatalogError(f"intelligence must be a whole number from 0 to 100, got {intelligence!r}")
    return max((name for name, floor in CLASS_FLOORS.items() if intelligence >= floor),
               key=CLASS_FLOORS.__getitem__)


@dataclass(frozen=True)
class PairProfile:
    """The routing judgement for one `(model, effort)` pair on one harness (DESIGN.md §17.3).

    The pair, not the model, carries the class and the cost: one model at two efforts is
    two routes. `effort=None` is the pair of a harness with no effort dial, where the model
    id is the whole choice and nothing is sent as an effort.
    """

    model_id: str
    effort: Optional[str]
    strength: str = "balanced"
    relative_cost: float = 1.0
    #: The pair's score, 0 to 100, when an approved table gave one (`from_score`). `None`
    #: for a pair migrated from a schema 1 row, which states only a class.
    intelligence: Optional[int] = None

    @classmethod
    def from_score(cls, model_id: str, effort: Optional[str], intelligence: int,
                   relative_cost: float) -> "PairProfile":
        """The pair of an approved `(intelligence, cost)` row; its class is derived."""
        return cls(model_id, effort, class_of(intelligence), relative_cost, intelligence)

    def __post_init__(self) -> None:
        subject = f"{self.model_id or '<no model>'}@{self.effort}"
        if not self.model_id:
            raise CatalogError("pair profile has no model_id")
        if self.effort is not None and not (isinstance(self.effort, str) and self.effort.strip()):
            raise CatalogError(f"{subject}: effort must be a word or None")
        if self.strength not in MODEL_STRENGTHS:
            raise CatalogError(
                f"{subject}: unknown strength {self.strength!r} "
                f"(expected one of {list(MODEL_STRENGTHS)})"
            )
        if self.relative_cost <= 0:
            raise CatalogError(f"{subject}: relative_cost must be positive")
        if self.intelligence is not None and class_of(self.intelligence) != self.strength:
            raise CatalogError(
                f"{subject}: intelligence {self.intelligence} is {class_of(self.intelligence)}, "
                f"not {self.strength}"
            )

    @property
    def strength_rank(self) -> int:
        return MODEL_STRENGTHS.index(self.strength)

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "effort": self.effort,
            "strength": self.strength,
            "relative_cost": self.relative_cost,
            "intelligence": self.intelligence,
        }


def migrate_pairs(*rows: tuple[str, str, float]) -> dict[tuple[str, Optional[str]], PairProfile]:
    """Read schema 1 `(model, class, cost)` rows at their historical efforts.

    The caller marks its entry `pairs_migrated=True` so demand and effort selection
    retain the schema 1 rules. No model-wide grade is reconstructed.
    """
    return {(model, effort): PairProfile(model, effort, strength, cost)
            for model, strength, cost in rows for effort in (*MIGRATION_EFFORTS, None)}


@dataclass(frozen=True)
class EffortControls:
    """Whether reasoning effort can be asked for, and how it is delivered (`[INV-A07]`).

    The invariant this type exists to make unstateable: *a delivery mechanism without an
    observed vocabulary*. `supported=True` requires both a mechanism and at least one
    native value, because a router holding only the first would have to invent the
    second, and "`--effort` exists" is what a `--help` inspection proves — never
    "`--effort=high` is accepted".

    `mapping` is the only bridge between maestro's `CANONICAL_EFFORTS` and a backend's
    own words, and it is per-backend data rather than a shared table: mapping an effort
    label across vendors without a measured mapping is exactly what B06 forbids.
    """

    supported: bool = False
    delivery_mechanism: str = "unsupported"
    native_values: tuple[str, ...] = ()
    #: `canonical effort -> native value`. Empty when the native vocabulary already *is*
    #: the canonical one (then the match is `native`, not `mapped`).
    mapping: Mapping[str, str] = field(default_factory=dict)
    #: Free text: why effort is unsupported, when it is. Carried into the binding's
    #: `selection_reason` so an operator reading a low-effort run can see it was a
    #: refusal to guess rather than a choice.
    unsupported_reason: str = ""
    #: The config key a `config_key` mechanism writes (codex: `model_reasoning_effort`).
    #: Observed data from the register row, never guessed; empty for a flag.
    config_key: str = ""

    def __post_init__(self) -> None:
        if self.delivery_mechanism not in EFFORT_DELIVERY:
            raise CatalogError(
                f"unknown effort delivery_mechanism {self.delivery_mechanism!r} "
                f"(expected one of {sorted(EFFORT_DELIVERY)})"
            )
        if self.supported:
            if self.delivery_mechanism == "unsupported":
                raise CatalogError(
                    "effort_controls claims support but declares no delivery mechanism"
                )
            if not self.native_values:
                raise CatalogError(
                    "effort_controls claims support but lists no observed native values "
                    "— a flag whose vocabulary was never observed cannot be used "
                    "([INV-A07]: never invent a flag's values)"
                )
        else:
            if self.delivery_mechanism != "unsupported":
                raise CatalogError(
                    f"effort_controls is unsupported but declares delivery "
                    f"{self.delivery_mechanism!r}"
                )
            if self.native_values:
                raise CatalogError("effort_controls is unsupported but lists native values")
        unknown = sorted(set(self.mapping) - set(CANONICAL_EFFORTS))
        if unknown:
            raise CatalogError(f"effort mapping keys are not canonical efforts: {unknown}")
        stray = sorted(set(self.mapping.values()) - set(self.native_values))
        if stray:
            raise CatalogError(f"effort mapping targets values the backend never showed: {stray}")

    def resolve(self, canonical: str) -> tuple[Optional[str], str]:
        """`(native value, effort_support)` for a canonical effort level.

        Three honest outcomes, and no fourth:

        * `('high', 'native')` — the backend's own vocabulary contains that exact word.
        * `('3', 'mapped')` — this backend declared a measured mapping for it.
        * `(None, 'unsupported')` — either effort is not controllable here at all, or it
          is but nothing maps this level. The caller must then pick the best base model
          rather than send a value the backend never showed (`§2.1` step 4).
        """
        if not self.supported:
            return None, "unsupported"
        if canonical in self.native_values:
            return canonical, "native"
        mapped = self.mapping.get(canonical)
        if mapped:
            return mapped, "mapped"
        return None, "unsupported"

    def to_dict(self) -> dict:
        return {
            "supported": self.supported,
            "delivery_mechanism": self.delivery_mechanism,
            "native_values": list(self.native_values),
            "mapping": dict(self.mapping),
            "unsupported_reason": self.unsupported_reason,
            "config_key": self.config_key,
        }


@dataclass(frozen=True)
class ContextLimits:
    """Raw window, generation limit, and the empirical ceiling routing actually uses.

    `effective_context_ceiling` is the number the router filters on. The raw window is
    what the vendor sells; the ceiling is where this host has seen attention degrade,
    which is the same distinction `maestro.limits` already draws for the operator's own
    sessions. Routing a task that needs 300K of context into a model whose quality
    collapses past 200K is not a cheaper route, it is a failed one.
    """

    max_input_tokens: int
    max_output_tokens: int
    effective_context_ceiling: int

    def __post_init__(self) -> None:
        for name in ("max_input_tokens", "max_output_tokens", "effective_context_ceiling"):
            if int(getattr(self, name)) <= 0:
                raise CatalogError(f"context_limits.{name} must be positive")
        if self.effective_context_ceiling > self.max_input_tokens:
            raise CatalogError(
                "context_limits.effective_context_ceiling exceeds max_input_tokens — a "
                "quality ceiling above the window it applies to is not a measurement"
            )

    def admits(self, input_tokens: int, output_tokens: int = 0) -> bool:
        """Does a task of this shape fit *within the quality ceiling*, not the window?"""
        return (
            int(input_tokens) <= self.effective_context_ceiling
            and int(output_tokens) <= self.max_output_tokens
        )

    def to_dict(self) -> dict:
        return {
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "effective_context_ceiling": self.effective_context_ceiling,
        }


@dataclass(frozen=True)
class BackendCapabilityEntry:
    """One backend as a capability provider, across `§1`'s six dimensions."""

    backend_id: str
    runtime_kind: str
    installed_version: str
    billing_category: str
    usage_pool_id: str
    available_models: tuple[str, ...]
    effort_controls: EffortControls = field(default_factory=EffortControls)
    context_limits: ContextLimits = field(
        default_factory=lambda: ContextLimits(200_000, 32_000, 150_000)
    )
    modalities: tuple[str, ...] = ("text",)
    tool_delivery: str = "native_cli_tools"
    instruction_delivery: str = "appended_prompt"
    structured_output_support: str = "regex_bounded"
    permission_enforcement: str = "conventional"
    concurrency_limit: int = 1
    telemetry_quality: str = "unobserved"
    #: Which concrete instance this entry describes — an absolute binary path, a process
    #: handle, an endpoint URL (`§2.6`'s `runtime_instance_id`). Empty falls back to
    #: `backend_id`, which is honest for a catalog built without binary discovery.
    runtime_instance_id: str = ""
    #: Per-model annotations. A model in `available_models` with no profile here is
    #: treated as `ModelProfile(model_id)` — see `profile()`.
    model_profiles: Mapping[str, ModelProfile] = field(default_factory=dict)
    #: The verification register row this entry was derived from, when it was. Empty for
    #: a hand-built entry, which `catalog_from_register` never produces.
    verified_by: str = ""
    #: The approved `(model_id, effort)` pairs and their class and cost (DESIGN.md §17.3).
    #: An empty mapping means no pair is approved, so the entry has no route.
    pair_profiles: Mapping[tuple[str, Optional[str]], PairProfile] = field(default_factory=dict)
    #: True when the pairs were read from schema 1 rows. The router then binds a role
    #: at the effort it used (`MIGRATION_ROLE_EFFORTS`), not at whichever sorts first.
    pairs_migrated: bool = False

    def __post_init__(self) -> None:
        issues: list[str] = []
        subject = self.backend_id or "<anonymous backend>"
        if not self.backend_id:
            issues.append("entry has no backend_id")
        if self.runtime_kind not in RUNTIME_KINDS:
            issues.append(f"unknown runtime_kind {self.runtime_kind!r}")
        if self.billing_category not in BILLING_CATEGORIES:
            issues.append(f"unknown billing_category {self.billing_category!r}")
        if not self.usage_pool_id:
            issues.append("entry declares no usage_pool_id — quota cannot be attributed")
        if not self.available_models:
            issues.append("entry lists no available_models")
        if not self.modalities or set(self.modalities) - MODALITIES:
            issues.append(f"unknown modalities {sorted(set(self.modalities) - MODALITIES)}")
        if self.tool_delivery not in TOOL_DELIVERY:
            issues.append(f"unknown tool_delivery {self.tool_delivery!r}")
        if self.instruction_delivery not in INSTRUCTION_DELIVERY:
            issues.append(f"unknown instruction_delivery {self.instruction_delivery!r}")
        if self.structured_output_support not in STRUCTURED_OUTPUT_SUPPORT:
            issues.append(
                f"unknown structured_output_support {self.structured_output_support!r}"
            )
        if self.permission_enforcement not in PERMISSION_ENFORCEMENT:
            issues.append(f"unknown permission_enforcement {self.permission_enforcement!r}")
        if self.telemetry_quality not in TELEMETRY_QUALITY:
            issues.append(f"unknown telemetry_quality {self.telemetry_quality!r}")
        if int(self.concurrency_limit) < 1:
            issues.append("concurrency_limit must be at least 1")
        stray = sorted(set(self.model_profiles) - set(self.available_models))
        if stray:
            issues.append(f"model_profiles describe models not in available_models: {stray}")
        for model_id, profile in self.model_profiles.items():
            if profile.model_id != model_id:
                issues.append(
                    f"model_profiles[{model_id!r}] carries model_id {profile.model_id!r}"
                )
        stray = sorted({key[0] for key in self.pair_profiles} - set(self.available_models))
        if stray:
            issues.append(f"pair_profiles describe models not in available_models: {stray}")
        for key, pair in self.pair_profiles.items():
            if (pair.model_id, pair.effort) != tuple(key):
                issues.append(f"pair_profiles[{key!r}] carries {pair.model_id}@{pair.effort}")
        if issues:
            raise CatalogError(f"{subject}: " + "; ".join(issues))

    # --- lookups --------------------------------------------------------------------

    def profile(self, model_id: str) -> ModelProfile:
        """This model's profile, defaulted for a model the catalog only *lists*.

        Defaulting rather than raising is deliberate: `available_models` comes from a
        version-pinned observation of what the CLI accepts, so the per-model facts (limits,
        pool) can be asked of a model nobody graded yet. That does not make it routable: a
        model with no approved pair has no route (`pairs`, DESIGN.md §17.3).
        """
        if model_id not in self.available_models:
            raise CatalogError(f"{self.backend_id}: unknown model {model_id!r}")
        known = self.model_profiles.get(model_id)
        return known if known is not None else ModelProfile(model_id=model_id)

    def limits_for(self, model_id: str) -> ContextLimits:
        profile = self.profile(model_id)
        return profile.context_limits or self.context_limits

    def pool_for(self, model_id: str) -> str:
        """The quota pool *this model* bills against.

        One backend can serve models drawn from different quotas — agy serves
        third-party `claude-*` / `gpt-oss-*` out of its own `3p-*` pools, not the Gemini
        ones — so the pool is a property of the model first and of the entry second.
        Mirrors `profile()`'s semantics: unknown ids raise, and a listed-but-ungraded id
        falls back to the entry's pool.
        """
        profile = self.profile(model_id)
        return profile.usage_pool_id or self.usage_pool_id

    def resolve_effort(self, model_id: str, effort: Optional[str]) -> tuple[Optional[str], str]:
        variants = self.profile(model_id).effort_variants
        if variants is not None:
            value = variants.get(effort)
            return (value, 'native') if value is not None else (None, 'unsupported')
        return self.effort_controls.resolve(effort) if effort is not None else (None, 'unsupported')

    def effort_delivery_for(self, model_id: str) -> str:
        return ('model_variant' if self.profile(model_id).effort_variants is not None
                else self.effort_controls.delivery_mechanism)

    def delivers(self, effort: Optional[str], model_id: Optional[str] = None) -> bool:
        """Whether this harness can run a pair at `effort` (`[INV-A07]`).

        A named effort needs a native or mapped value. No effort is deliverable only where
        there is no dial: on a harness with one, a pair must say which setting it means.
        """
        if model_id is not None and self.profile(model_id).effort_variants is not None:
            return effort in self.profile(model_id).effort_variants
        if effort is None:
            return not self.effort_controls.supported
        return self.effort_controls.resolve(effort)[0] is not None

    def pairs(self, model_id: str) -> tuple[PairProfile, ...]:
        """This model's routes: its approved pairs that the harness can deliver."""
        found = [
            pair for pair in self.pair_profiles.values()
            if pair.model_id == model_id and self.delivers(pair.effort, model_id)
        ]
        return tuple(sorted(found, key=lambda pair: (_effort_rank(pair.effort), pair.effort or "")))

    def pair(self, model_id: str, effort: Optional[str]) -> PairProfile:
        for found in self.pairs(model_id):
            if found.effort == effort:
                return found
        raise CatalogError(f"{self.backend_id}: {model_id}@{effort} is not a route")

    def undeliverable_pairs(self) -> tuple[PairProfile, ...]:
        """Approved pairs that are not routes because the harness cannot deliver the effort."""
        refused = [pair for pair in self.pair_profiles.values() if not self.delivers(pair.effort, pair.model_id)]
        return tuple(sorted(
            refused, key=lambda pair: (pair.model_id, _effort_rank(pair.effort), pair.effort or "")))

    @property
    def instance_id(self) -> str:
        return self.runtime_instance_id or self.backend_id

    @property
    def permission_rank(self) -> int:
        return PERMISSION_STRENGTH[self.permission_enforcement]

    def to_dict(self) -> dict:
        return {
            "backend_id": self.backend_id,
            "runtime_kind": self.runtime_kind,
            "installed_version": self.installed_version,
            "billing_category": self.billing_category,
            "usage_pool_id": self.usage_pool_id,
            "available_models": list(self.available_models),
            "effort_controls": self.effort_controls.to_dict(),
            "context_limits": self.context_limits.to_dict(),
            "modalities": list(self.modalities),
            "tool_delivery": self.tool_delivery,
            "instruction_delivery": self.instruction_delivery,
            "structured_output_support": self.structured_output_support,
            "permission_enforcement": self.permission_enforcement,
            "concurrency_limit": int(self.concurrency_limit),
            "telemetry_quality": self.telemetry_quality,
            "runtime_instance_id": self.runtime_instance_id,
            "model_profiles": {
                key: value.to_dict() for key, value in sorted(self.model_profiles.items())
            },
            "verified_by": self.verified_by,
            "pair_profiles": [
                pair.to_dict() for pair in sorted(
                    self.pair_profiles.values(),
                    key=lambda pair: (pair.model_id, _effort_rank(pair.effort), pair.effort or ""),
                )
            ],
        }

    @property
    def revision(self) -> str:
        """A content hash of this entry, for `AgentBinding.catalog_revision` (`§2.6`).

        Every field participates, `installed_version` included: a CLI upgrade that
        changes what a flag means must not leave old bindings looking like they were
        resolved against the new reality.
        """
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class CapabilityCatalog:
    """Every backend maestro may route to, keyed by `backend_id`."""

    entries: Mapping[str, BackendCapabilityEntry] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for backend_id, entry in self.entries.items():
            if entry.backend_id != backend_id:
                raise CatalogError(
                    f"catalog key {backend_id!r} does not match entry {entry.backend_id!r}"
                )

    @classmethod
    def of(cls, entries: Sequence[BackendCapabilityEntry]) -> "CapabilityCatalog":
        return cls(entries={entry.backend_id: entry for entry in entries})

    def entry(self, backend_id: str) -> BackendCapabilityEntry:
        try:
            return self.entries[backend_id]
        except KeyError:
            raise CatalogError(f"no catalog entry for backend {backend_id!r}") from None

    def backend_ids(self) -> tuple[str, ...]:
        """Every backend id, in a deterministic order — routing must not depend on dict
        insertion order, or the same inputs would resolve differently between runs."""
        return tuple(sorted(self.entries))

    def pool_ids(self) -> tuple[str, ...]:
        return tuple(sorted({entry.usage_pool_id for entry in self.entries.values()}))

    def models(self) -> tuple[tuple[BackendCapabilityEntry, str], ...]:
        """Every `(entry, model_id)`, deterministically ordered, routed or not. For what is
        true of a model at any effort: its pool, its limits."""
        listed: list[tuple[BackendCapabilityEntry, str]] = []
        for backend_id in self.backend_ids():
            entry = self.entries[backend_id]
            for model_id in sorted(entry.available_models):
                listed.append((entry, model_id))
        return tuple(listed)

    def routes(self) -> tuple[tuple[BackendCapabilityEntry, str, Optional[str]], ...]:
        """Every `(entry, model_id, effort)` route, deterministically ordered. The router's
        candidate space before any filtering: approved pairs the harness can deliver."""
        return tuple(
            (entry, model_id, pair.effort)
            for entry, model_id in self.models()
            for pair in entry.pairs(model_id)
        )

    @property
    def revision(self) -> str:
        """A hash over every entry revision — the catalog's own version."""
        joined = "|".join(f"{key}:{self.entries[key].revision}" for key in self.backend_ids())
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {
            "revision": self.revision,
            "entries": {key: self.entries[key].to_dict() for key in self.backend_ids()},
        }


# ── deriving entries from the verification register (`[INV-12]`, B06) ────────────────


def effort_controls_from_findings(findings: Mapping) -> EffortControls:
    """`EffortControls` for one verification row's `findings` block.

    The whole of `[INV-A07]`'s "never invent a flag" is this function's contract, so it
    is stated as three rules rather than left in the branches:

    1. **A mechanism needs an observation.** `--effort` in `controls_observed` gives
       `delivery_mechanism='flag'`; an `effort_control.kind == 'config-key'` block gives
       `'config_key'`. Nothing else invents one.
    2. **A mechanism without a vocabulary is not support.** Values come only from
       `effort_control.native_values`. Absent, the result is `supported=False` with a
       reason naming the row's own gap — which is the claude case on this host: the flag
       is present at 2.1.269, its values were never exercised, and B06 says so in its own
       open questions.
    3. **No cross-vendor mapping is inferred.** `effort_control.mapping` is used when the
       row declares it and is never synthesised from the canonical vocabulary.
    """
    if not isinstance(findings, Mapping):
        return EffortControls(unsupported_reason="verification row carries no findings")

    controls = findings.get("controls_observed")
    observed_flags = set(controls) if isinstance(controls, (list, tuple)) else set()
    block = findings.get("effort_control")
    block = block if isinstance(block, Mapping) else {}

    kind = str(block.get("kind") or "").strip().replace("-", "_")
    if kind in ("flag", "config_key"):
        mechanism = kind
    elif "--effort" in observed_flags:
        mechanism = "flag"
    else:
        return EffortControls(
            unsupported_reason=(
                "no effort control was observed on the verified version "
                "(no --effort in controls_observed, no effort_control block)"
            )
        )

    native_values = tuple(
        str(value) for value in (block.get("native_values") or ()) if str(value).strip()
    )
    if not native_values:
        return EffortControls(
            unsupported_reason=(
                f"an effort control is present ({mechanism}) but the register records no "
                "native values for it — the flag's existence is not its vocabulary "
                "([INV-A07])"
            )
        )

    raw_mapping = block.get("mapping")
    mapping = (
        {
            str(key): str(value)
            for key, value in raw_mapping.items()
            if str(key) in CANONICAL_EFFORTS and str(value) in native_values
        }
        if isinstance(raw_mapping, Mapping)
        else {}
    )
    return EffortControls(
        supported=True,
        delivery_mechanism=mechanism,
        native_values=native_values,
        mapping=mapping,
        config_key=str(block.get("key") or "") if mechanism == "config_key" else "",
    )


#: Routing judgement for the `agy` register row (P07B) — what a verification row cannot
#: know. The model ids are exactly `agy models` at 1.2.2
#: (`artifacts/graph-engineering/p07b-agy-1.2.2-probe.json`), and every one carries an
#: explicit profile: `profile()` would otherwise grade a newly listed id `balanced`,
#: and on this backend only `gemini-3.8-flash-high` has earned that. `relative_cost`
#: stays at 1.0 throughout because no within-backend cost ratio has been measured.
#: The entry limits describe the Flash family (the status line's `context_window_size`
#: and the `agy/` table's prepare-handoff ceiling); the models whose AGY profile
#: differs carry their own.
_AGY_PRO_LIMITS = ContextLimits(128_000, 32_000, 108_000)
_AGY_OSS_LIMITS = ContextLimits(80_000, 32_000, 60_000)
_AGY_3P_CLAUDE_LIMITS = ContextLimits(160_000, 32_000, 120_000)

#: agy bills the third-party models it serves (`claude-*`, `gpt-oss-*`) against its
#: `3p-5h` / `3p-weekly` quota, separately from the Gemini pools — observed in
#: `~/.gemini/statusline_dump.json` (P07B). Those models therefore carry their own pool.
AGY_THIRD_PARTY_POOL = "antigravity-3p"


def _agy_profiles() -> dict[str, ModelProfile]:
    profiles = [
        # Current base ids: enforced AGY windows and normally-fresh low ends from
        # modelctl's 2026-10-10 table. Old ids below are native pin-only compatibility.
        *[ModelProfile(model, context_limits=ContextLimits(176_000, 32_000, 125_000))
          for model in ('gemini-3.8-flash', 'gemini-3.7-flash', 'gemini-3.6-flash')],
        ModelProfile('gemini-3.1-pro', context_limits=ContextLimits(88_000, 32_000, 65_000)),
        *[ModelProfile(model, context_limits=ContextLimits(316_000, 32_000, 215_000),
                       usage_pool_id=AGY_THIRD_PARTY_POOL)
          for model in ('claude-sonnet-5-5', 'claude-opus-5-5')],
        *[ModelProfile(f'{model}-{effort}')
          for model in ('gemini-3.8-flash', 'gemini-3.7-flash', 'gemini-3.6-flash')
          for effort in ('high', 'medium', 'low')],
        ModelProfile("gemini-3.1-pro-high", context_limits=_AGY_PRO_LIMITS),
        ModelProfile("gemini-3.1-pro-low", context_limits=_AGY_PRO_LIMITS),
        ModelProfile(
            "claude-sonnet-4-6",
            context_limits=_AGY_3P_CLAUDE_LIMITS,
            usage_pool_id=AGY_THIRD_PARTY_POOL,
        ),
        ModelProfile(
            "claude-opus-4-6-thinking",
            context_limits=_AGY_3P_CLAUDE_LIMITS,
            usage_pool_id=AGY_THIRD_PARTY_POOL,
        ),
        ModelProfile(
            "gpt-oss-120b-medium",
            context_limits=_AGY_OSS_LIMITS,
            usage_pool_id=AGY_THIRD_PARTY_POOL,
        ),
    ]
    return {profile.model_id: profile for profile in profiles}


ANTIGRAVITY_CATALOG_DEFAULTS: dict[str, dict[str, Any]] = {
    "agy": {
        "backend_id": "antigravity_cli",
        "billing_category": "subscription",
        # The entry's pool is the Gemini quota; the 3p models carry `antigravity-3p`
        # on their own profiles, which `pool_for` prefers.
        "usage_pool_id": "antigravity-gemini",
        "available_models": list(_agy_profiles()),
        "context_limits": ContextLimits(1_048_576, 32_000, 120_000),
        "model_profiles": _agy_profiles(),
        "pair_profiles": migrate_pairs(*[
            (model, 'balanced' if model == 'gemini-3.8-flash-high' else
             'light' if model in {
                 *[f'{base}-{effort}' for base in ('gemini-3.8-flash', 'gemini-3.7-flash', 'gemini-3.6-flash')
                   for effort in ('low', 'medium', 'high')],
                 'gemini-3.1-pro-low', 'gemini-3.1-pro-high', 'claude-sonnet-4-6',
                 'claude-opus-4-6-thinking', 'gpt-oss-120b-medium'} else 'balanced', 1.0)
            for model in _agy_profiles()]),
        "pairs_migrated": True,
    },
}

#: Native facts and historical schema 1 pair grades (P12A S2, Dan 2026-09-15).
#: Current routing judgments are supplied by model_policy's approved schema 2 table.
#: Effective ceilings follow each model's "normally start fresh" low end in
#: `~/.claude/model_context_limits.md`; Context Gate is unbuilt.
CLAUDE_CATALOG_DEFAULTS: dict[str, dict[str, Any]] = {
    "claude": {
        "backend_id": "claude",
        "billing_category": "subscription",
        "usage_pool_id": "claude-subscription",
        "concurrency_limit": 2,
        "available_models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-haiku-4-5-20251001"],
        "context_limits": ContextLimits(1_000_000, 32_000, 240_000),
        "model_profiles": {
            "claude-opus-5-5": ModelProfile("claude-opus-5-5"),
            "claude-sonnet-5-5": ModelProfile(
                "claude-sonnet-5-5",
                context_limits=ContextLimits(1_000_000, 32_000, 220_000),
            ),
            "claude-haiku-5-5": ModelProfile(
                "claude-haiku-5-5",
                context_limits=ContextLimits(1_000_000, 32_000, 190_000),
            ),
            "claude-haiku-4-5-20251001": ModelProfile(
                "claude-haiku-4-5-20251001",
                context_limits=ContextLimits(200_000, 32_000, 120_000),
            ),
        },
        "pair_profiles": migrate_pairs(
            ('claude-opus-5-5', 'strong', 1.25), ('claude-sonnet-5-5', 'balanced', 0.5),
            ('claude-haiku-5-5', 'light', 0.25), ('claude-haiku-4-5-20251001', 'light', 0.25)),
        "pairs_migrated": True,
    },
}

#: Routing judgement for the `codex` register row (P12A S2, chosen by Dan 2026-09-15). Same
#: provenance as the Claude row; ceilings from `~/.codex/model_context_limits.md`.
CODEX_CATALOG_DEFAULTS: dict[str, dict[str, Any]] = {
    "codex": {
        "backend_id": "codex",
        "billing_category": "subscription",
        "usage_pool_id": "codex-subscription",
        "concurrency_limit": 2,
        "available_models": ["gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-luna"],
        "context_limits": ContextLimits(400_000, 32_000, 230_000),
        "model_profiles": {
            "gpt-6-astra": ModelProfile("gpt-6-astra", native_context_verified=False),
            "gpt-6.1-sol": ModelProfile(
                "gpt-6.1-sol",
                context_limits=ContextLimits(420_000, 32_000, 240_000),
            ),
            "gpt-6-sol": ModelProfile(
                "gpt-6-sol",
                context_limits=ContextLimits(420_000, 32_000, 240_000),
            ),
            "gpt-6-luna": ModelProfile(
                "gpt-6-luna",
                context_limits=ContextLimits(340_000, 32_000, 180_000),
            ),
            "gpt-5.6-luna": ModelProfile(
                "gpt-5.6-luna",
                context_limits=ContextLimits(400_000, 32_000, 100_000),
            ),
        },
        "pair_profiles": migrate_pairs(
            ('gpt-6-astra', 'strong', 2.5), ('gpt-6.1-sol', 'strong', 1.0),
            ('gpt-6-sol', 'strong', 1.0), ('gpt-6-luna', 'balanced', 0.5),
            ('gpt-5.6-luna', 'light', 0.05)),
        "pairs_migrated": True,
    },
}

#: backend_id -> models still verified here but superseded (Dan, 2026-10-01: gpt-6.1-sol
#: replaces gpt-6-sol; 2026-10-08: gpt-6-luna replaces gpt-5.6-luna as the light route).
#: Their native facts remain available for historical policy rows and literal task-pin
#: validation. A project role pin cannot add them to shared routing eligibility.
PIN_ONLY_MODELS: dict[str, frozenset[str]] = {
    "codex": frozenset({"gpt-6-sol", "gpt-5.6-luna"}),
    "claude": frozenset({"claude-haiku-4-5-20251001"}),
    "antigravity_cli": frozenset({
        *[f'{model}-{effort}' for model in ('gemini-3.8-flash', 'gemini-3.7-flash',
                                          'gemini-3.6-flash')
          for effort in ('low', 'medium', 'high')],
        'gemini-3.1-pro-low', 'gemini-3.1-pro-high', 'claude-sonnet-4-6',
        'claude-opus-4-6-thinking', 'gpt-oss-120b-medium',
    }),
}

#: Every shipped routing default, keyed by register subject — what the live loop's catalog reads.
SHIPPED_CATALOG_DEFAULTS: dict[str, dict[str, Any]] = {
    **ANTIGRAVITY_CATALOG_DEFAULTS,
    **CLAUDE_CATALOG_DEFAULTS,
    **CODEX_CATALOG_DEFAULTS,
}


def catalog_from_register(
    register: Mapping[str, Any],
    *,
    defaults: Optional[Mapping[str, Mapping[str, Any]]] = None,
    require_driver: bool = True,
) -> CapabilityCatalog:
    """Build a catalog from `maestro.thirdparty`'s parsed register.

    Fail-closed in two places, both of them `[INV-12]`:

    * a row with no `verified_version` is **not** catalogued. An unverified backend is
      not a cheaper route, it is an unproven one, and `maestro doctor --thirdparty`
      exists precisely so that "we never checked" cannot reach production as a default.
    * with `require_driver=True` (the default), a row whose `findings.maestro_driver` is
      explicitly `False` is skipped: the agy row on this host is an inspection of an
      installed CLI that no driver can currently launch, and a catalog that offered it
      as a route would produce bindings nothing could dispatch.

    `defaults` supplies the judgement the register cannot: models, limits, pool ids,
    profiles — keyed by subject. Capability *facts* (effort, version, sandboxing) always
    come from the row, never from `defaults`, so a stale default can never overstate what
    a verified CLI does.
    """
    defaults = defaults or {}
    entries: list[BackendCapabilityEntry] = []
    for subject in sorted(register):
        row = register[subject]
        kind = getattr(row, "kind", None) or (row.get("kind") if isinstance(row, Mapping) else None)
        if kind != "backend":
            continue
        version = getattr(row, "verified_version", "") or ""
        if not str(version).strip():
            continue
        findings = getattr(row, "findings", None) or {}
        if require_driver and findings.get("maestro_driver") is False:
            continue

        base = dict(defaults.get(subject) or {})
        models = _tuple_of_str(
            base.get("available_models"), field_name="available_models", subject=subject
        )
        if not models:
            continue

        declared = findings.get("declared_capabilities")
        declared = declared if isinstance(declared, Mapping) else {}
        permission = base.get("permission_enforcement") or (
            "directory_isolation" if declared.get("sandbox") else "conventional"
        )
        telemetry = base.get("telemetry_quality") or (
            "window_percentage" if declared.get("usage_telemetry") else "unobserved"
        )
        instruction = base.get("instruction_delivery") or (
            "profile_file" if declared.get("system_prompt_file") else "appended_prompt"
        )

        limits = base.get("context_limits")
        if isinstance(limits, ContextLimits):
            context_limits = limits
        elif isinstance(limits, Mapping):
            context_limits = ContextLimits(**limits)
        else:
            context_limits = ContextLimits(200_000, 32_000, 150_000)

        profiles = {
            model_id: profile
            for model_id, profile in (base.get("model_profiles") or {}).items()
        }

        entries.append(
            BackendCapabilityEntry(
                backend_id=str(base.get("backend_id") or subject),
                runtime_kind=str(base.get("runtime_kind") or "agent_cli"),
                installed_version=str(version),
                billing_category=str(base.get("billing_category") or "subscription"),
                usage_pool_id=str(base.get("usage_pool_id") or subject),
                available_models=models,
                effort_controls=effort_controls_from_findings(findings),
                context_limits=context_limits,
                modalities=_tuple_of_str(
                    base.get("modalities") or ("text",), field_name="modalities", subject=subject
                ),
                tool_delivery=str(base.get("tool_delivery") or "native_cli_tools"),
                instruction_delivery=str(instruction),
                structured_output_support=str(
                    base.get("structured_output_support") or "regex_bounded"
                ),
                permission_enforcement=str(permission),
                concurrency_limit=int(base.get("concurrency_limit") or 1),
                telemetry_quality=str(telemetry),
                runtime_instance_id=str(base.get("runtime_instance_id") or ""),
                model_profiles=profiles,
                verified_by=str(getattr(row, "verified_by", "") or ""),
                pair_profiles=base.get("pair_profiles", {}),
                pairs_migrated=base.get("pairs_migrated", False),
            )
        )
    return CapabilityCatalog.of(entries)


# ── multi-window quota accounting (`[INV-A07]`) ──────────────────────────────────────


#: Reset deadlines within this tolerance identify the same observed quota window.
RESET_JITTER_SEC = 600


@dataclass(frozen=True)
class QuotaWindow:
    """One rate-limit window's reading. `resets_at` is unix epoch seconds.

    Mirrors `maestro.backends.base.WindowUsage` rather than importing it: that type is
    the *driver protocol's* value and carries no window duration, because it is stored in
    a map keyed by one. Here a window travels alone (a pool's most-pressured window, say)
    and must be able to say which duration it is.
    """

    window_minutes: int
    used_pct: Optional[float] = None
    resets_at: Optional[int] = None

    def __post_init__(self) -> None:
        if int(self.window_minutes) <= 0:
            raise CatalogError("quota window_minutes must be positive")

    def to_dict(self) -> dict:
        return {
            "window_minutes": int(self.window_minutes),
            "used_pct": self.used_pct,
            "resets_at": self.resets_at,
        }


@dataclass(frozen=True)
class QuotaObservation:
    """What one account pool's allowances looked like, and how well that is known.

    The four `[INV-A07]` fields are the point of this type:

    * `observed_at` — when the *backend* measured it.
    * `written_at` — when maestro recorded it. For a `push` surface these differ by an
      amount maestro does not control.
    * `staleness_bound` — how many seconds old this reading may be before it stops
      counting as evidence. Not a guess at when it expires: a declared bound, so that a
      pause decision can say which side of it the reading fell.
    * `carried_forward` — `True` when this is an earlier reading re-dated because no new
      one arrived. A carried reading is never proof of headroom (`fresh_enough`), only of
      pressure already seen.
    """

    pool_id: str
    windows: Mapping[int, QuotaWindow] = field(default_factory=dict)
    observed_at: str = ""
    written_at: str = ""
    staleness_bound: float = 900.0
    carried_forward: bool = False
    acquisition: str = "push"
    #: Independent model constraints in this account, never merged with its windows.
    scoped_windows: Mapping[str, Mapping[int, QuotaWindow]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.pool_id:
            raise CatalogError("quota observation has no pool_id")
        if self.acquisition not in ACQUISITION_PATHS:
            raise CatalogError(
                f"{self.pool_id}: unknown acquisition path {self.acquisition!r} "
                f"(expected one of {sorted(ACQUISITION_PATHS)})"
            )
        if float(self.staleness_bound) <= 0:
            raise CatalogError(f"{self.pool_id}: staleness_bound must be positive")
        for minutes, window in self.windows.items():
            if window.window_minutes != minutes:
                raise CatalogError(
                    f"{self.pool_id}: window keyed {minutes} carries "
                    f"window_minutes {window.window_minutes}"
                )

    # --- readings -------------------------------------------------------------------

    def window(self, window_minutes: int) -> Optional[QuotaWindow]:
        return self.windows.get(int(window_minutes))

    @property
    def five_hour(self) -> Optional[QuotaWindow]:
        """The 300-minute window, or `None` — an account may genuinely have none (G5)."""
        return self.window(300)

    @property
    def weekly(self) -> Optional[QuotaWindow]:
        return self.window(10080)

    def max_used_pct(self) -> Optional[float]:
        """The most-consumed window, whatever its duration. `None` when nothing is known
        — which never means "no pressure", only "not measurable" (G6)."""
        values = [w.used_pct for w in self.windows.values() if w.used_pct is not None]
        return max(values) if values else None

    def earliest_reset(self) -> Optional[int]:
        """The soonest epoch any window resets — a wait's `retry_after` comes from here."""
        resets = [w.resets_at for w in self.windows.values() if w.resets_at]
        return min(resets) if resets else None

    # --- age ------------------------------------------------------------------------

    def age_seconds(self, now: Optional[datetime] = None) -> Optional[float]:
        """Seconds since the backend measured this, or `None` when unparseable."""
        observed = _parse_iso(self.observed_at)
        if observed is None:
            return None
        moment = now or datetime.now(timezone.utc)
        return (moment - observed).total_seconds()

    def is_stale(self, now: Optional[datetime] = None) -> bool:
        """Has this reading aged past its declared bound? An unknown age is stale."""
        age = self.age_seconds(now)
        return True if age is None else age > float(self.staleness_bound)

    def fresh_enough(self, now: Optional[datetime] = None) -> bool:
        """May this reading be used as evidence of *headroom*?

        A carried-forward reading may not: it is the previous sample wearing a new
        timestamp, and admitting work on it would be admitting on a sample maestro
        already knows it failed to refresh.
        """
        return not self.carried_forward and not self.is_stale(now)

    def pace_delta(self, now_epoch: float) -> Optional[float]:
        """QuotaPulse weekly pace: positive means usage is behind elapsed time."""
        import math
        moment = datetime.fromtimestamp(now_epoch, timezone.utc)
        window = self.weekly
        if (not self.fresh_enough(moment) or window is None or window.used_pct is None
                or window.resets_at is None or not math.isfinite(window.used_pct)):
            return None
        elapsed = max(0.0, min(100.0,
                      (now_epoch - (window.resets_at - 7 * 86400)) / (7 * 86400) * 100))
        return round(round(elapsed, 1) - window.used_pct, 1)

    def carry_forward(self, *, at: Optional[str] = None) -> "QuotaObservation":
        """This reading, re-recorded now because no fresh one arrived.

        `observed_at` is deliberately untouched — carrying a reading forward must not
        make it look younger; only `written_at` moves.
        """
        return replace(self, written_at=at or utc_now_iso(), carried_forward=True)

    def exhausted(self, pause_pct: float) -> bool:
        """Is any window at or past the pause threshold? Unknown usage is not exhaustion
        (that is `is_stale`'s question, answered separately so a missing sample and a
        full pool never collapse into one state)."""
        used = self.max_used_pct()
        return used is not None and used >= float(pause_pct)

    @classmethod
    def from_usage(
        cls,
        usage: Any,
        *,
        pool_id: str,
        acquisition: str = "push",
        staleness_bound: float = 900.0,
        written_at: Optional[str] = None,
    ) -> "QuotaObservation":
        """Bridge a driver's `maestro.backends.base.Usage` into an observation.

        `usage.updated_at` is the driver's own "when this was measured", so it becomes
        `observed_at`; `written_at` is now. That gap is the whole reason both fields
        exist, and collapsing them here would erase the only evidence of it.
        """
        import math

        windows = {
            int(minutes): QuotaWindow(
                window_minutes=int(minutes),
                used_pct=getattr(window, "used_pct", None),
                resets_at=getattr(window, "resets_at", None),
            )
            for minutes, window in (getattr(usage, "windows", None) or {}).items()
        }
        scoped = {}
        raw_scoped = getattr(usage, 'scoped_windows', None)
        if isinstance(raw_scoped, Mapping):
            for scope, constraints in raw_scoped.items():
                if not isinstance(scope, str) or not scope or not isinstance(constraints, Mapping):
                    continue
                converted = {}
                for minutes, window in constraints.items():
                    if (isinstance(minutes, bool) or not isinstance(minutes, (int, str))
                            or not str(minutes).isdecimal() or int(minutes) <= 0):
                        continue
                    duration = int(minutes)
                    reset = getattr(window, 'resets_at', None)
                    if (isinstance(reset, bool) or not isinstance(reset, (int, float))
                            or not math.isfinite(reset) or reset < 0):
                        reset = None
                    converted[duration] = QuotaWindow(
                        duration, getattr(window, 'used_pct', None),
                        reset)
                if converted:
                    scoped[scope] = converted
        return cls(
            pool_id=pool_id,
            windows=windows,
            observed_at=str(getattr(usage, "updated_at", "") or ""),
            written_at=written_at or utc_now_iso(),
            staleness_bound=staleness_bound,
            carried_forward=False,
            acquisition=acquisition,
            scoped_windows=scoped,
        )

    def to_dict(self) -> dict:
        return {
            "pool_id": self.pool_id,
            "windows": {
                str(minutes): self.windows[minutes].to_dict()
                for minutes in sorted(self.windows)
            },
            "observed_at": self.observed_at,
            "written_at": self.written_at,
            "staleness_bound": float(self.staleness_bound),
            "carried_forward": self.carried_forward,
            "acquisition": self.acquisition,
            "scoped_windows": {
                scope: {str(minutes): windows[minutes].to_dict() for minutes in sorted(windows)}
                for scope, windows in sorted(self.scoped_windows.items())
            },
        }


@dataclass(frozen=True)
class Admission:
    """The answer to "may this pool take one more call right now?".

    `admitted=False` is never a silent no: `reason` is one of the router's own reason
    codes and `retry_after_sec` is present whenever the refusal is temporal, so the
    caller can emit a typed wait instead of falling back to another billing tier
    (`§3`'s acceptance condition).
    """

    admitted: bool
    reason: str = ""
    detail: str = ""
    retry_after_sec: Optional[float] = None


@dataclass
class PoolState:
    """One account pool's live admission state.

    Mutable, unlike everything above it: this is the ledger the scheduler updates, and
    P8 replaces it with the SQLite-backed reservation ledger without changing the
    question it answers. `active` counts *running* workers, and `paused_until` gates only
    *new* admissions — pausing a pool must never kill a healthy worker mid-task
    (`§3`'s acceptance condition), so nothing here can express "stop the running ones".
    """

    pool_id: str
    concurrency_limit: int = 1
    active: int = 0
    observation: Optional[QuotaObservation] = None
    paused_until: Optional[int] = None
    pause_reason: str = ""

    def has_slot(self) -> bool:
        return self.active < int(self.concurrency_limit)

    def reserve(self) -> None:
        """Take a concurrency slot. Callers must have checked `admit()` first."""
        self.active += 1

    def release(self) -> None:
        self.active = max(0, self.active - 1)

    def pause(self, *, until: Optional[int], reason: str) -> None:
        """Stop admitting new work. Running workers are untouched, by construction."""
        self.paused_until = until
        self.pause_reason = reason

    def resume(self) -> None:
        self.paused_until = None
        self.pause_reason = ""

    def admit(self, *, pause_pct: float = 92.0, now_epoch: Optional[float] = None) -> Admission:
        """May this pool start one more call?

        Ordered so the *temporal* refusals come first and carry a `retry_after_sec`: an
        operator (and the router) can tell "come back in 40 minutes" from "this pool will
        never take this work".
        """
        moment = now_epoch if now_epoch is not None else datetime.now(timezone.utc).timestamp()
        if self.paused_until is not None:
            if moment < float(self.paused_until):
                return Admission(
                    False,
                    reason="pool_paused",
                    detail=self.pause_reason or f"pool {self.pool_id} is paused",
                    retry_after_sec=float(self.paused_until) - moment,
                )
            self.resume()

        observation = self.observation
        if observation is not None and observation.exhausted(pause_pct):
            reset = observation.earliest_reset()
            return Admission(
                False,
                reason="quota_exhausted",
                detail=(
                    f"pool {self.pool_id} at {observation.max_used_pct()}% of its most "
                    f"pressured window (threshold {pause_pct}%)"
                ),
                retry_after_sec=max(0.0, float(reset) - moment) if reset else None,
            )
        if not self.has_slot():
            return Admission(
                False,
                reason="pool_saturated",
                detail=(
                    f"pool {self.pool_id} has {self.active} of {self.concurrency_limit} "
                    "concurrency slots in use"
                ),
            )
        return Admission(True)


@dataclass
class ResourcePoolState:
    """Every pool's admission state, keyed by `usage_pool_id`.

    The `resources` argument of `resolve_agent`. P8 owns the real host-wide ledger
    (`~/.maestro/reservations.json`, `§3` of the scheduling spec); this is the contract
    that ledger has to satisfy, kept small enough that P6 can be tested without it.
    """

    pools: dict = field(default_factory=dict)
    #: Usage percentage at or above which a pool stops admitting. Defaults to the value
    #: `maestro.quota` already pauses the legacy loop on, so the graph runner and the
    #: lane runner do not disagree about what "full" means.
    pause_pct: float = 92.0

    def pool(self, pool_id: str, *, concurrency_limit: int = 1) -> PoolState:
        """The pool's state, created on first mention with `concurrency_limit`."""
        state = self.pools.get(pool_id)
        if state is None:
            state = PoolState(pool_id=pool_id, concurrency_limit=concurrency_limit)
            self.pools[pool_id] = state
        return state

    def observe(self, observation: QuotaObservation) -> None:
        """Record a fresh reading for its pool."""
        self.pool(observation.pool_id).observation = observation

    def admit(self, pool_id: str, *, now_epoch: Optional[float] = None) -> Admission:
        state = self.pools.get(pool_id)
        if state is None:
            return Admission(True)
        return state.admit(pause_pct=self.pause_pct, now_epoch=now_epoch)

    def pause_exhausted_pools(self, *, now_epoch: Optional[float] = None) -> tuple[str, ...]:
        """Pause every pool whose reading is at or past the threshold; return their ids.

        The acceptance condition in two lines: pausing sets `paused_until` and touches
        `active` nowhere, so a pool can be paused with three healthy workers still
        running and all three finish.
        """
        paused: list[str] = []
        for pool_id in sorted(self.pools):
            state = self.pools[pool_id]
            observation = state.observation
            if observation is not None and observation.exhausted(self.pause_pct):
                state.pause(
                    until=observation.earliest_reset(),
                    reason=f"quota at {observation.max_used_pct()}%",
                )
                paused.append(pool_id)
        return tuple(paused)


# ── empirical outcome statistics (`[INV-A07]`) ───────────────────────────────────────


@dataclass(frozen=True)
class ShapeKey:
    """What a success rate is a success rate *of* (`[INV-A07]`).

    Backend and model alone are not a shape. The same model at the same effort behaves
    differently under a different role version, a different template revision, or a
    different set of composed fragments — so a statistic keyed only on the model would
    silently attribute a template regression to the model and reroute away from a model
    that was never the problem. The eight fields are exactly A07's list, and the fragment
    set is normalised (sorted, deduplicated) so two identical compositions written in
    different orders are one shape and not two.
    """

    backend_id: str
    model_id: str
    effort: str
    role_version: str
    task_class: str
    template_id: str
    template_version: str
    active_fragment_set: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "active_fragment_set", tuple(sorted(set(self.active_fragment_set)))
        )

    def as_tuple(self) -> tuple:
        return (
            self.backend_id,
            self.model_id,
            self.effort,
            self.role_version,
            self.task_class,
            self.template_id,
            self.template_version,
            self.active_fragment_set,
        )

    def to_dict(self) -> dict:
        return {
            "backend_id": self.backend_id,
            "model_id": self.model_id,
            "effort": self.effort,
            "role_version": self.role_version,
            "task_class": self.task_class,
            "template_id": self.template_id,
            "template_version": self.template_version,
            "active_fragment_set": list(self.active_fragment_set),
        }


@dataclass
class OutcomeRecord:
    """Attempts and verified successes for one shape."""

    attempts: int = 0
    successes: int = 0

    @property
    def success_rate(self) -> Optional[float]:
        """`None` when nothing has been attempted — never `0.0`, which would read as
        "this shape fails" and route away from a shape that was simply never tried."""
        return None if self.attempts == 0 else self.successes / self.attempts

    def to_dict(self) -> dict:
        return {"attempts": self.attempts, "successes": self.successes}


@dataclass
class OutcomeStatistics:
    """Verified outcomes per shape, and the evidence floor before they may steer routing.

    `success_rate` returns `None` below `minimum_observations` rather than a number. One
    success out of one attempt is 100%, and a router that believed it would pin every
    future task of that class to whichever route happened to go first — the ranking would
    be reproducible, deterministic and evidence-free. D06's "agent assertions are not
    evidence" has the same shape: a number is not a measurement just because it exists.

    Only *verified* outcomes belong here. `record()` takes the controller's acceptance
    decision, never an agent's self-reported pass.
    """

    records: dict = field(default_factory=dict)
    minimum_observations: int = 5

    def record(self, key: ShapeKey, *, success: bool) -> OutcomeRecord:
        entry = self.records.setdefault(key.as_tuple(), OutcomeRecord())
        entry.attempts += 1
        if success:
            entry.successes += 1
        return entry

    def observations(self, key: ShapeKey) -> int:
        entry = self.records.get(key.as_tuple())
        return 0 if entry is None else entry.attempts

    def success_rate(self, key: ShapeKey) -> Optional[float]:
        """The measured rate, or `None` when the evidence floor is not met."""
        entry = self.records.get(key.as_tuple())
        if entry is None or entry.attempts < int(self.minimum_observations):
            return None
        return entry.success_rate

    def to_dict(self) -> dict:
        return {
            "minimum_observations": int(self.minimum_observations),
            "records": [
                {
                    "shape": list(shape[:-1]) + [list(shape[-1])],
                    **record.to_dict(),
                }
                for shape, record in sorted(self.records.items(), key=lambda kv: str(kv[0]))
            ],
        }
