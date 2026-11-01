"""Project engineering policy: `workflows/project.yaml` (`[P5]`, `D03`, `D05`).

`project.yaml` at the repository root is the *operational* config — which backend runs
which role, what the quota thresholds are, where the deny list lives. This module reads a
different file for a different question: `workflows/project.yaml`, the **engineering
policy**, which says what shape a workflow for this project is allowed to have.

Three things live here and nowhere else:

* **Template composition rules** (`[INV-A06]`). A template is not a hardcoded lane; it is
  an ordered list of named, versioned fragment refs, some of them conditional. The
  compiler resolves the refs; this module decides which conditional stages survive for a
  given task.
* **Quality gates.** The criteria a composed graph must satisfy before it may run —
  "a high-risk change gets an independent review", "every lane ends at controller-executed
  acceptance". The compiler checks the composition against these and refuses to emit a
  graph that omits one (`[INV-04]` fails closed; a missing reviewer is not a cheaper
  workflow, it is an invalid one).
* **Agent role definitions** (`§2.5`). Five distinct cognitive contracts — implementer,
  planner, reviewer, test designer, diagnoser — as data, versioned, with explicit negative
  boundaries. `D04` separates these from runtime bindings: nothing here names a backend, a
  model or an effort value, because a role that knew its backend could not be re-bound
  when quota moved.

**Fail-closed, everywhere.** A policy file that is present but malformed raises; it never
degrades into the defaults. A silently-defaulted policy is the failure mode that makes a
review requirement disappear without anybody noticing, which is precisely the thing this
file exists to prevent. Absence is different from corruption: a project that has not
written a policy yet gets `default_policy()`, which is the baseline P3 lane shape.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from maestro.workflows.models import EXECUTION_OBJECTIVES, WorkflowError

#: Where a project keeps its engineering policy, relative to the repository root.
POLICY_RELPATH = Path("workflows") / "project.yaml"

#: Where the packaged defaults live: the policy template `init` scaffolds, and the five
#: role definitions the compiler resolves `agent_definition_ref` against.
TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
POLICY_TEMPLATE = TEMPLATES_DIR / "workflows" / "project.yaml"
ROLES_TEMPLATE_DIR = TEMPLATES_DIR / "roles"

#: A project may override a packaged role definition by dropping a file of the same name
#: here. Overriding, not extending — a half-overridden role contract is a role contract
#: nobody can read off one page.
PROJECT_ROLES_RELPATH = Path("workflows") / "roles"

POLICY_SCHEMA_VERSION = 1


class PolicyError(WorkflowError):
    """A policy document that cannot be trusted. Carries every issue, not just the first."""

    def __init__(self, issues: Sequence[str] | str):
        self.issues = [issues] if isinstance(issues, str) else list(issues)
        super().__init__("; ".join(self.issues))


# ── conditions ───────────────────────────────────────────────────────────────────────
#
# A closed set on purpose. The alternative — an expression string evaluated at compile
# time — would make the policy file executable, and a config file that can run code is a
# config file that has to be trusted the way source is.

#: `condition name -> predicate over a RiskContext`.
CONDITIONS = {
    "always": lambda ctx: True,
    "never": lambda ctx: False,
    "high_risk": lambda ctx: ctx.risk in ("high", "critical"),
    "low_risk": lambda ctx: ctx.risk == "low",
    "touches_tests": lambda ctx: ctx.touches_tests,
    "touches_risky_paths": lambda ctx: ctx.touches_risky_paths,
    "self_modifying": lambda ctx: ctx.self_modifying,
    "free_quality_mode": lambda ctx: ctx.objective == "free_quality",
    "paid_efficiency_mode": lambda ctx: ctx.objective == "paid_efficiency",
    # `include_when` OR-s its conditions, so a stage that needs "free mode AND not
    # trivial" cannot say so in two entries — it would then also fire on a low-risk
    # task. The conjunction is named here, once, and `[INV-06]`'s candidate fan-out is
    # gated on it: buying two implementers for a typo fix is not quality, it is spend.
    "free_quality_nontrivial": lambda ctx: (
        ctx.objective == "free_quality" and ctx.risk != "low"
    ),
}

#: Criteria a composed graph can be required to satisfy. Each is a property of the node
#: set, checked by the compiler — never a claim the policy makes about itself.
REQUIRED_CRITERIA = frozenset(
    {
        "implementation",
        "verification_gate",
        "controller_merge",
        "acceptance",
        "independent_review",
        "human_receipt",
        "planning",
        "independent_tests",
        # `[P7]`, all three checked against the nodes actually composed: a template
        # that declared a fan-out and emitted one node is the failure these catch.
        "isolated_candidates",
        "candidate_selection",
        "diverse_review",
    }
)

#: The two shapes a stage may widen into (`D05`, `§3`). A closed vocabulary for the
#: same reason `CONDITIONS` is one: the width of a compiled graph is a quality gate
#: input, and a template that could name an arbitrary fan-out kind could ask for a
#: width nothing knows how to check.
FAN_OUT_KINDS = frozenset({"candidates", "review_lenses"})

#: Default globs for "this change touches the test suite". Overridable per project.
DEFAULT_TEST_PATHS = ("tests/*", "test/*", "*_test.py", "test_*.py", "*.test.*", "*.spec.*")


@dataclass(frozen=True)
class RiskContext:
    """Everything a condition may ask about a task. Derived once, per compilation.

    A value, not a lookup into the task: the same context is handed to the stage filter
    and to the criteria check, so a graph can never be composed under one reading of the
    task's risk and validated under another.
    """

    task_id: str
    risk: str = "medium"
    execution_kind: str = "ordinary"
    objective: str = "paid_efficiency"
    touches_tests: bool = False
    touches_risky_paths: bool = False
    self_modifying: bool = False

    def holds(self, condition: str) -> bool:
        predicate = CONDITIONS.get(condition)
        if predicate is None:
            raise PolicyError(f"{self.task_id}: unknown condition {condition!r}")
        return bool(predicate(self))

    def any_holds(self, conditions: Iterable[str]) -> bool:
        return any(self.holds(c) for c in conditions)


# ── fragments & templates ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FragmentRef:
    """`name@version`, the citation form `[INV-A06]` asks templates to be written in.

    The version is carried, compared and hashed rather than merely documented: a fragment
    whose shape changed under a template that still asks for `@1.0.0` is a compile-time
    refusal, not a graph that quietly grew a node.
    """

    name: str
    version: str

    @classmethod
    def parse(cls, text: str) -> "FragmentRef":
        raw = str(text or "").strip()
        name, sep, version = raw.partition("@")
        if not name or not sep or not version:
            raise PolicyError(f"fragment ref {text!r} is not 'name@version'")
        return cls(name=name, version=version)

    def __str__(self) -> str:
        return f"{self.name}@{self.version}"


@dataclass(frozen=True)
class StageSpec:
    """One position in a template: a fragment, when to include it, where failure goes."""

    fragment: FragmentRef
    #: Node id in the compiled graph. Defaults to the fragment name — overridden where a
    #: lane reuses a fragment under a different name (`manual`'s `prep` is an implementer).
    node_id: str = ""
    #: Conditions under which this stage is included. Empty means unconditional.
    include_when: tuple[str, ...] = ()
    #: Sink node this stage's failure edge points at, if any.
    on_failure: str | None = None
    #: Widen this position into N parallel nodes when the policy calls for it — either
    #: `'candidates'` (`[INV-06]`'s isolated implementers) or `'review_lenses'` (`D05`'s
    #: diverse reviewers). Empty means exactly one node, which is every paid-mode stage
    #: and every baseline lane; the width itself is the policy's to compute, never the
    #: template's to state, so a project cannot hardcode "three candidates" into a shape.
    fan_out: str = ""

    def __post_init__(self) -> None:
        if not self.node_id:
            object.__setattr__(self, "node_id", self.fragment.name)
        for condition in self.include_when:
            if condition not in CONDITIONS:
                raise PolicyError(f"stage {self.node_id}: unknown condition {condition!r}")
        if self.fan_out and self.fan_out not in FAN_OUT_KINDS:
            raise PolicyError(
                f"stage {self.node_id}: unknown fan_out {self.fan_out!r} "
                f"(expected one of {sorted(FAN_OUT_KINDS)})"
            )

    @property
    def optional(self) -> bool:
        return bool(self.include_when)

    def included(self, ctx: RiskContext) -> bool:
        return True if not self.include_when else ctx.any_holds(self.include_when)


@dataclass(frozen=True)
class TemplateSpec:
    """A composable lane: ordered stages, failure sinks, terminals, required criteria."""

    execution_kind: str
    version: str
    stages: tuple[StageSpec, ...]
    sinks: tuple[StageSpec, ...] = ()
    terminals: tuple[str, ...] = ()
    required_criteria: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        issues = []
        if not self.stages:
            issues.append(f"template {self.execution_kind}: no stages")
        seen: set[str] = set()
        for stage in (*self.stages, *self.sinks):
            if stage.node_id in seen:
                issues.append(f"template {self.execution_kind}: duplicate stage {stage.node_id}")
            seen.add(stage.node_id)
        sink_ids = {s.node_id for s in self.sinks}
        for stage in self.stages:
            if stage.on_failure and stage.on_failure not in sink_ids:
                issues.append(
                    f"template {self.execution_kind}: stage {stage.node_id} fails to "
                    f"{stage.on_failure!r}, which is not a declared sink"
                )
        for criterion in self.required_criteria:
            if criterion not in REQUIRED_CRITERIA:
                issues.append(
                    f"template {self.execution_kind}: unknown required criterion {criterion!r}"
                )
        if issues:
            raise PolicyError(issues)

    def selected_stages(self, ctx: RiskContext) -> tuple[StageSpec, ...]:
        """The stages that survive `ctx` — the minimal shape, before criteria checking."""
        return tuple(stage for stage in self.stages if stage.included(ctx))


@dataclass(frozen=True)
class FreeQualityPolicy:
    """What free mode buys with inference that costs nothing (`D05`, `§3`).

    Two dials and nothing else, because the topology they widen is checked afterwards:
    `candidates` is how many isolated implementers `[INV-06]` will have to keep apart, and
    `review_lenses` names the reviews `D05` calls diverse. The lens names are *data* — they
    reach the reviewer node as a parameter — so adding a fourth lens is a policy edit, not
    a compiler change.

    Both are floors of 1 read the other way round: a width of 1 emits exactly the node a
    paid-mode graph emits, which is what keeps free mode's topology a superset of paid
    mode's rather than a separate lane.
    """

    candidates: int = 2
    review_lenses: tuple[str, ...] = ("logic", "security", "architecture")

    def __post_init__(self) -> None:
        issues = []
        if self.candidates < 1:
            issues.append(f"free_quality.candidates must be at least 1, not {self.candidates}")
        if self.candidates > 8:
            issues.append(
                f"free_quality.candidates is {self.candidates}; more than 8 isolated "
                "worktrees per task is a host problem, not a quality one"
            )
        seen = set()
        for lens in self.review_lenses:
            if not str(lens).strip():
                issues.append("free_quality.review_lenses contains an empty lens")
            if lens in seen:
                issues.append(f"free_quality.review_lenses repeats {lens!r}")
            seen.add(lens)
        if issues:
            raise PolicyError(issues)

    def width(self, fan_out: str) -> int:
        """How wide a stage of this kind gets, at full free-mode width."""
        if fan_out == "candidates":
            return int(self.candidates)
        if fan_out == "review_lenses":
            return len(self.review_lenses)
        return 1

    def labels(self, fan_out: str) -> tuple[str, ...]:
        """The per-node suffixes for a widened stage — `('c1', 'c2')`, or the lens names."""
        if fan_out == "candidates":
            return tuple(f"c{index}" for index in range(1, int(self.candidates) + 1))
        if fan_out == "review_lenses":
            return tuple(str(lens) for lens in self.review_lenses)
        return ()

    def to_dict(self) -> dict:
        return {"candidates": int(self.candidates), "review_lenses": list(self.review_lenses)}


@dataclass(frozen=True)
class HybridPolicy:
    """`§3.1`'s eligibility overlay. Not a third objective — an exception list.

    Under the free objective the router permits only `free_tier_api` capacity. This names
    the roles that may nonetheless spend paid capacity, because `§3.1` reserves the
    subscription for high-leverage nodes (architectural planning, critical debugging) and
    leaves routine implementation, candidate generation and test design on the free tier.

    Empty by default, and deliberately so: hybrid is something an operator turns on for
    named roles, never a default that quietly reintroduces spend into a free-mode project.
    """

    paid_roles: tuple[str, ...] = ()
    billing_categories: tuple[str, ...] = ("subscription",)

    def __post_init__(self) -> None:
        if self.paid_roles and not self.billing_categories:
            raise PolicyError(
                "hybrid.paid_roles names roles but hybrid.billing_categories is empty — "
                "a grant of nothing is not a grant"
            )

    @property
    def active(self) -> bool:
        return bool(self.paid_roles)

    def grants(self, role_id: str) -> tuple[str, ...]:
        """The extra billing categories `role_id` may spend, `()` for every other role."""
        return tuple(self.billing_categories) if role_id in self.paid_roles else ()

    def to_dict(self) -> dict:
        return {
            "paid_roles": list(self.paid_roles),
            "billing_categories": list(self.billing_categories),
        }


@dataclass(frozen=True)
class QualityGates:
    """What the composed graph must satisfy, independent of how it was composed.

    Deliberately separate from the stage conditions. `include_when` is an optimisation —
    "do not pay for a reviewer on a typo fix"; this is a floor — "whatever you composed,
    a high-risk change without an independent reviewer does not compile". Two mechanisms
    because a single one gets tuned for cost and silently lowers the floor with it.
    """

    review_required_when: tuple[str, ...] = ("high_risk", "touches_risky_paths")
    planning_required_when: tuple[str, ...] = ()
    independent_tests_required_when: tuple[str, ...] = ()
    #: A reviewer that can write to the worktree it is reviewing is not independent.
    independent_review: bool = True
    risky_paths: tuple[str, ...] = ()
    test_paths: tuple[str, ...] = DEFAULT_TEST_PATHS

    def __post_init__(self) -> None:
        for label, conditions in (
            ("review_required_when", self.review_required_when),
            ("planning_required_when", self.planning_required_when),
            ("independent_tests_required_when", self.independent_tests_required_when),
        ):
            for condition in conditions:
                if condition not in CONDITIONS:
                    raise PolicyError(f"{label}: unknown condition {condition!r}")

    def criteria_for(self, ctx: RiskContext) -> tuple[str, ...]:
        """The criteria this task's composed graph must satisfy, on top of the template's."""
        required = []
        if ctx.any_holds(self.review_required_when):
            required.append("independent_review")
        if ctx.any_holds(self.planning_required_when):
            required.append("planning")
        if ctx.any_holds(self.independent_tests_required_when):
            required.append("independent_tests")
        return tuple(required)


# ── agent role definitions (`§2.5`) ──────────────────────────────────────────────────


@dataclass(frozen=True)
class AgentDefinition:
    """A versioned role contract (`§2.5`, `D04`).

    What a role *is*, never what runs it. `permitted_backend_capabilities` states a
    requirement (`min_reasoning_strength`) that P6's router resolves against the catalog;
    it does not name a backend, and a definition that did would make the role unroutable
    the moment that backend ran out of quota.
    """

    role_id: str
    version: str
    responsibility: str
    exclusions: tuple[str, ...] = ()
    instruction_layers: Mapping[str, str] = field(default_factory=dict)
    context_assembly_rules: Mapping[str, Any] = field(default_factory=dict)
    skill_references: tuple[str, ...] = ()
    logical_tools: tuple[str, ...] = ()
    permission_requirements: tuple[str, ...] = ()
    permitted_backend_capabilities: Mapping[str, Any] = field(default_factory=dict)
    success_criteria: tuple[str, ...] = ()
    memory_handoff_policy: str = "stateless"

    #: The write-capable logical tools. A role that holds none of these cannot mutate a
    #: worktree whatever its permissions say, which is what makes `writes` checkable.
    WRITE_TOOLS = frozenset({"write_file", "edit_file", "apply_patch", "run_command", "git_commit"})

    MEMORY_POLICIES = frozenset({"stateless", "read_prior_attempt", "full_causal"})

    def __post_init__(self) -> None:
        issues = []
        if not self.role_id:
            issues.append("role definition has no role_id")
        if not self.version:
            issues.append(f"{self.role_id}: role definition has no version")
        if not self.responsibility:
            issues.append(f"{self.role_id}: role definition states no responsibility")
        if self.memory_handoff_policy not in self.MEMORY_POLICIES:
            issues.append(
                f"{self.role_id}: unknown memory_handoff_policy "
                f"{self.memory_handoff_policy!r}"
            )
        if issues:
            raise PolicyError(issues)

    @property
    def writes(self) -> bool:
        """Does this role hold any tool that can change the repository?"""
        return bool(self.WRITE_TOOLS & set(self.logical_tools))

    @classmethod
    def from_document(cls, document: Mapping) -> "AgentDefinition":
        if not isinstance(document, Mapping):
            raise PolicyError("role definition is not a mapping")
        body = dict(document)
        if "effort_policy" in body:
            raise PolicyError(
                f"{body.get('role_id', '?')}: effort_policy is retired, remove it: a role "
                "states no effort, the route carries it (DESIGN.md §17.2)"
            )
        known = {f for f in cls.__dataclass_fields__}
        unknown = sorted(set(body) - known)
        if unknown:
            raise PolicyError(
                f"{body.get('role_id', '?')}: unknown role definition keys {unknown}"
            )
        for key in (
            "exclusions", "skill_references", "logical_tools",
            "permission_requirements", "success_criteria",
        ):
            body[key] = tuple(body.get(key) or ())
        for key in (
            "instruction_layers", "context_assembly_rules",
            "permitted_backend_capabilities",
        ):
            body[key] = dict(body.get(key) or {})
        return cls(**body)

    def to_dict(self) -> dict:
        return {
            "role_id": self.role_id,
            "version": self.version,
            "responsibility": self.responsibility,
            "exclusions": list(self.exclusions),
            "instruction_layers": dict(self.instruction_layers),
            "context_assembly_rules": dict(self.context_assembly_rules),
            "skill_references": list(self.skill_references),
            "logical_tools": list(self.logical_tools),
            "permission_requirements": list(self.permission_requirements),
            "permitted_backend_capabilities": dict(self.permitted_backend_capabilities),
            "success_criteria": list(self.success_criteria),
            "memory_handoff_policy": self.memory_handoff_policy,
        }


def load_agent_definitions(*directories: Path) -> dict[str, AgentDefinition]:
    """Read `*.yaml` role definitions, later directories overriding earlier ones by id."""
    definitions: dict[str, AgentDefinition] = {}
    for directory in directories:
        if not directory or not Path(directory).is_dir():
            continue
        for path in sorted(Path(directory).glob("*.yaml")):
            try:
                document = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError) as exc:
                raise PolicyError(f"{path}: unreadable role definition ({exc})") from exc
            definition = AgentDefinition.from_document(document or {})
            if definition.role_id != path.stem:
                raise PolicyError(
                    f"{path}: role_id {definition.role_id!r} does not match filename"
                )
            definitions[definition.role_id] = definition
    return definitions


# ── the policy ───────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProjectPolicy:
    """One project's engineering policy: objective, templates, gates, roles, permissions."""

    objective: str = "paid_efficiency"
    templates: Mapping[str, TemplateSpec] = field(default_factory=dict)
    quality_gates: QualityGates = field(default_factory=QualityGates)
    #: `D05`'s free-mode dials, and `§3.1`'s paid-capacity exception list. Both are read
    #: under *either* objective — a project does not rewrite its policy to switch mode,
    #: it changes `objective:` and the same blocks resolve to a different width.
    free_quality: FreeQualityPolicy = field(default_factory=FreeQualityPolicy)
    hybrid: HybridPolicy = field(default_factory=HybridPolicy)
    permissions: Mapping[str, str] = field(default_factory=dict)
    agent_definitions: Mapping[str, AgentDefinition] = field(default_factory=dict)
    source_path: str = ""
    schema_version: int = POLICY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        issues = []
        if self.objective not in EXECUTION_OBJECTIVES:
            issues.append(
                f"unknown objective {self.objective!r} "
                f"(expected one of {sorted(EXECUTION_OBJECTIVES)})"
            )
        if not self.templates:
            issues.append("policy declares no workflow templates")
        # A role a template asks for but no definition describes is a node the compiler
        # could still emit and no dispatcher could bind. Caught here, once.
        for template in self.templates.values():
            for stage in (*template.stages, *template.sinks):
                role = ROLE_FOR_FRAGMENT.get(stage.fragment.name)
                if role and role not in self.agent_definitions:
                    issues.append(
                        f"template {template.execution_kind}: stage {stage.node_id} needs "
                        f"role {role!r}, which has no AgentDefinition"
                    )
        # `[INV-05]`/`D04`: a reviewing role that can write is not a reviewer.
        if self.quality_gates.independent_review:
            for role_id in ("reviewer", "planner"):
                definition = self.agent_definitions.get(role_id)
                if definition is not None and definition.writes:
                    issues.append(
                        f"role {role_id!r} holds write tools "
                        f"{sorted(AgentDefinition.WRITE_TOOLS & set(definition.logical_tools))} "
                        f"but independent_review is required"
                    )
        if issues:
            raise PolicyError(issues)

    # --- lookups --------------------------------------------------------------------

    def template(self, execution_kind: str) -> TemplateSpec:
        try:
            return self.templates[execution_kind]
        except KeyError:
            raise PolicyError(
                f"policy declares no template for execution_kind {execution_kind!r}"
            ) from None

    def definition(self, role_id: str) -> AgentDefinition:
        try:
            return self.agent_definitions[role_id]
        except KeyError:
            raise PolicyError(f"policy declares no AgentDefinition for role {role_id!r}") from None

    def permission_for(self, role_id: str, default: str) -> str:
        value = self.permissions.get(role_id)
        return str(value) if value else default

    def risk_context(self, spec) -> RiskContext:
        """Derive the compile-time context for one `TaskSpec` (`D05`'s topology input)."""
        scope = tuple(str(p) for p in getattr(spec, "scope", ()) or ())
        return RiskContext(
            task_id=spec.task_id,
            risk=getattr(spec, "risk", "medium") or "medium",
            execution_kind=getattr(spec, "execution_kind", "ordinary"),
            objective=self.objective,
            touches_tests=_matches_any(scope, self.quality_gates.test_paths),
            touches_risky_paths=_matches_any(scope, self.quality_gates.risky_paths),
            self_modifying=bool(getattr(spec, "self_modifying", False)),
        )

    # --- fan-out (`D05`, `[INV-06]`) -------------------------------------------------

    def fan_out_labels(self, stage: StageSpec, ctx: RiskContext) -> tuple[str, ...]:
        """The per-node suffixes this stage widens into, `()` when it stays one node.

        The width is computed here rather than declared in the template because it is a
        *mode* question, not a shape question: the same `fan_out: candidates` stage is one
        node in paid mode, one node on a low-risk free task, and `candidates` nodes on a
        free-mode task worth exploring. A template that stated its own width would make
        paid mode a different template, and then `D05`'s "both share one runtime" would be
        two lanes wearing one name.
        """
        if not stage.fan_out or not ctx.holds("free_quality_nontrivial"):
            return ()
        labels = self.free_quality.labels(stage.fan_out)
        return labels if len(labels) > 1 else ()

    def fan_out_width(self, stage: StageSpec, ctx: RiskContext) -> int:
        return len(self.fan_out_labels(stage, ctx)) or 1

    def fan_out_criteria(self, execution_kind: str, ctx: RiskContext) -> tuple[str, ...]:
        """The floor a *widened* graph must clear, derived from the widening itself.

        Deliberately not a condition an operator sets. These criteria are required exactly
        where this compilation widened a stage — so a lane that never fans out (a `script`
        task under the free objective) is never asked for candidates it has no business
        having, and a lane that does fan out cannot drop the selection join that `[INV-06]`
        makes the whole point of fanning out.
        """
        required: list[str] = []
        for stage in self.template(execution_kind).selected_stages(ctx):
            if self.fan_out_width(stage, ctx) < 2:
                continue
            if stage.fan_out == "candidates":
                for criterion in ("isolated_candidates", "candidate_selection"):
                    if criterion not in required:
                        required.append(criterion)
            elif stage.fan_out == "review_lenses" and "diverse_review" not in required:
                required.append("diverse_review")
        return tuple(required)

    def required_criteria(self, execution_kind: str, ctx: RiskContext) -> tuple[str, ...]:
        """Template floor, plus risk-derived floor, plus fan-out floor. Deduplicated."""
        combined = [*self.template(execution_kind).required_criteria]
        for criterion in (
            *self.quality_gates.criteria_for(ctx),
            *self.fan_out_criteria(execution_kind, ctx),
        ):
            if criterion not in combined:
                combined.append(criterion)
        return tuple(combined)

    def with_objective(self, objective: str) -> "ProjectPolicy":
        """The same policy read under the other optimisation objective (`D05`)."""
        return self if objective == self.objective else replace(self, objective=objective)

    # --- identity -------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Canonical form. `source_path` is excluded: where the file sits is not policy."""
        return {
            "schema_version": self.schema_version,
            "objective": self.objective,
            "templates": {
                kind: {
                    "version": t.version,
                    "stages": [_stage_dict(s) for s in t.stages],
                    "sinks": [_stage_dict(s) for s in t.sinks],
                    "terminals": list(t.terminals),
                    "required_criteria": list(t.required_criteria),
                }
                for kind, t in sorted(self.templates.items())
            },
            "quality_gates": {
                "review_required_when": list(self.quality_gates.review_required_when),
                "planning_required_when": list(self.quality_gates.planning_required_when),
                "independent_tests_required_when": list(
                    self.quality_gates.independent_tests_required_when
                ),
                "independent_review": self.quality_gates.independent_review,
                "risky_paths": list(self.quality_gates.risky_paths),
                "test_paths": list(self.quality_gates.test_paths),
            },
            "free_quality": self.free_quality.to_dict(),
            "hybrid": self.hybrid.to_dict(),
            "permissions": dict(sorted(self.permissions.items())),
            "roles": {
                role_id: definition.to_dict()
                for role_id, definition in sorted(self.agent_definitions.items())
            },
        }

    @property
    def policy_hash(self) -> str:
        """SHA256 of the canonical policy, stamped onto every revision compiled under it.

        Every input the compiler consults is inside this hash — including the role
        definitions, because a workflow whose reviewer contract changed is not the same
        workflow even when the graph is node-for-node identical.
        """
        body = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    # --- construction ---------------------------------------------------------------

    @classmethod
    def from_document(
        cls,
        document: Mapping,
        *,
        agent_definitions: Mapping[str, AgentDefinition] | None = None,
        source_path: str = "",
    ) -> "ProjectPolicy":
        if not isinstance(document, Mapping):
            raise PolicyError(f"{source_path or 'policy'}: document is not a mapping")
        schema_version = int(document.get("schema_version") or POLICY_SCHEMA_VERSION)
        if schema_version != POLICY_SCHEMA_VERSION:
            raise PolicyError(
                f"{source_path or 'policy'}: schema_version {schema_version} is not "
                f"{POLICY_SCHEMA_VERSION}"
            )
        templates = {}
        raw_templates = document.get("templates") or {}
        if not isinstance(raw_templates, Mapping):
            raise PolicyError(f"{source_path or 'policy'}: templates: is not a mapping")
        for kind, body in raw_templates.items():
            templates[str(kind)] = _template_from_document(str(kind), body)
        gates_body = document.get("quality_gates") or {}
        if not isinstance(gates_body, Mapping):
            raise PolicyError(f"{source_path or 'policy'}: quality_gates: is not a mapping")
        gates = QualityGates(
            review_required_when=tuple(gates_body.get("review_required_when") or ()),
            planning_required_when=tuple(gates_body.get("planning_required_when") or ()),
            independent_tests_required_when=tuple(
                gates_body.get("independent_tests_required_when") or ()
            ),
            independent_review=bool(gates_body.get("independent_review", True)),
            risky_paths=tuple(str(p) for p in gates_body.get("risky_paths") or ()),
            test_paths=tuple(str(p) for p in gates_body.get("test_paths") or DEFAULT_TEST_PATHS),
        )
        permissions = document.get("permissions") or {}
        if not isinstance(permissions, Mapping):
            raise PolicyError(f"{source_path or 'policy'}: permissions: is not a mapping")
        return cls(
            objective=str(document.get("objective") or "paid_efficiency"),
            templates=templates,
            quality_gates=gates,
            free_quality=_free_quality_from_document(
                document.get("free_quality"), source_path=source_path
            ),
            hybrid=_hybrid_from_document(document.get("hybrid"), source_path=source_path),
            permissions={str(k): str(v) for k, v in permissions.items()},
            agent_definitions=dict(
                agent_definitions if agent_definitions is not None
                else load_agent_definitions(ROLES_TEMPLATE_DIR)
            ),
            source_path=source_path,
            schema_version=schema_version,
        )

    @classmethod
    def load(cls, repo: Path | str) -> "ProjectPolicy":
        """Read `<repo>/workflows/project.yaml`, falling back to the packaged default.

        Absence falls back; corruption does not. See the module docstring — the whole
        point of a quality gate is that it cannot be removed by a typo.
        """
        repo = Path(repo)
        path = repo / POLICY_RELPATH
        definitions = load_agent_definitions(ROLES_TEMPLATE_DIR, repo / PROJECT_ROLES_RELPATH)
        if not path.is_file():
            return default_policy(agent_definitions=definitions)
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise PolicyError(f"{path}: unreadable engineering policy ({exc})") from exc
        if document is None:
            raise PolicyError(f"{path}: engineering policy is empty")
        return cls.from_document(
            document, agent_definitions=definitions, source_path=str(path)
        )


def default_policy(
    *, agent_definitions: Mapping[str, AgentDefinition] | None = None
) -> ProjectPolicy:
    """The packaged policy — what `init` scaffolds and what an un-policied project gets."""
    try:
        document = yaml.safe_load(POLICY_TEMPLATE.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:  # pragma: no cover - packaging failure
        raise PolicyError(f"{POLICY_TEMPLATE}: packaged policy is unreadable ({exc})") from exc
    return ProjectPolicy.from_document(
        document,
        agent_definitions=(
            agent_definitions if agent_definitions is not None
            else load_agent_definitions(ROLES_TEMPLATE_DIR)
        ),
        source_path=str(POLICY_TEMPLATE),
    )


#: Which fragments are agent nodes, and which role definition each one needs. Read by the
#: policy's own validation and by the compiler; declared once so the two cannot disagree.
ROLE_FOR_FRAGMENT = {
    "implementer": "implementer",
    "planner": "planner",
    "proof_review": "reviewer",
    "test_designer": "test_designer",
    "diagnoser": "diagnoser",
}


# ── helpers ──────────────────────────────────────────────────────────────────────────


def _matches_any(paths: Sequence[str], patterns: Sequence[str]) -> bool:
    return any(fnmatch(path, pattern) for path in paths for pattern in patterns)


def _free_quality_from_document(body: object, *, source_path: str = "") -> FreeQualityPolicy:
    if body is None:
        return FreeQualityPolicy()
    if not isinstance(body, Mapping):
        raise PolicyError(f"{source_path or 'policy'}: free_quality: is not a mapping")
    unknown = sorted(set(body) - {"candidates", "review_lenses"})
    if unknown:
        raise PolicyError(f"{source_path or 'policy'}: unknown free_quality keys {unknown}")
    try:
        candidates = int(body.get("candidates", 2))
    except (TypeError, ValueError):
        raise PolicyError(
            f"{source_path or 'policy'}: free_quality.candidates "
            f"{body.get('candidates')!r} is not a number"
        ) from None
    lenses = body.get("review_lenses")
    return FreeQualityPolicy(
        candidates=candidates,
        review_lenses=(
            tuple(str(lens) for lens in lenses)
            if lenses is not None
            else FreeQualityPolicy.review_lenses
        ),
    )


def _hybrid_from_document(body: object, *, source_path: str = "") -> HybridPolicy:
    if body is None:
        return HybridPolicy()
    if not isinstance(body, Mapping):
        raise PolicyError(f"{source_path or 'policy'}: hybrid: is not a mapping")
    unknown = sorted(set(body) - {"paid_roles", "billing_categories"})
    if unknown:
        raise PolicyError(f"{source_path or 'policy'}: unknown hybrid keys {unknown}")
    categories = body.get("billing_categories")
    return HybridPolicy(
        paid_roles=tuple(str(role) for role in body.get("paid_roles") or ()),
        billing_categories=(
            tuple(str(c) for c in categories)
            if categories is not None
            else HybridPolicy.billing_categories
        ),
    )


def _stage_dict(stage: StageSpec) -> dict:
    return {
        "fragment": str(stage.fragment),
        "node_id": stage.node_id,
        "include_when": list(stage.include_when),
        "on_failure": stage.on_failure,
        "fan_out": stage.fan_out,
    }


def _stage_from_document(kind: str, body: Mapping) -> StageSpec:
    if not isinstance(body, Mapping):
        raise PolicyError(f"template {kind}: stage entry {body!r} is not a mapping")
    unknown = sorted(
        set(body) - {"fragment", "node_id", "include_when", "on_failure", "fan_out"}
    )
    if unknown:
        raise PolicyError(f"template {kind}: unknown stage keys {unknown}")
    return StageSpec(
        fragment=FragmentRef.parse(body.get("fragment", "")),
        node_id=str(body.get("node_id") or ""),
        include_when=tuple(str(c) for c in body.get("include_when") or ()),
        on_failure=(str(body["on_failure"]) if body.get("on_failure") else None),
        fan_out=str(body.get("fan_out") or ""),
    )


def _template_from_document(kind: str, body: Mapping) -> TemplateSpec:
    if not isinstance(body, Mapping):
        raise PolicyError(f"template {kind}: is not a mapping")
    unknown = sorted(set(body) - {"version", "stages", "sinks", "terminals", "required_criteria"})
    if unknown:
        raise PolicyError(f"template {kind}: unknown keys {unknown}")
    version = str(body.get("version") or "")
    if not version:
        raise PolicyError(f"template {kind}: no version (`[INV-A06]` requires one)")
    return TemplateSpec(
        execution_kind=kind,
        version=version,
        stages=tuple(_stage_from_document(kind, s) for s in body.get("stages") or ()),
        sinks=tuple(_stage_from_document(kind, s) for s in body.get("sinks") or ()),
        terminals=tuple(str(t) for t in body.get("terminals") or ()),
        required_criteria=tuple(str(c) for c in body.get("required_criteria") or ()),
    )
