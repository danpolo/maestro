"""The compiled execution graph: `WorkflowRevision`, `NodeSpec`, `EdgeSpec` (`[P3]`).

Pure data and pure functions, the same discipline `maestro.control.models` keeps: nothing
here opens a database, launches a process or reads a clock the caller did not hand it.
That is what lets the runner's frontier computation be a total function of
`(revision, phases)` and therefore testable without a worktree, a tmux server or an agent.

Three things this module is deliberately strict about:

* **A revision is immutable.** `revision` is an integer snapshot; retries do not increment
  it, plan repair does (`02_EXECUTION_ENGINE_AND_DURABILITY.md` §5.3). `evolve()` is the
  only constructor for a successor and it always sets `parent_revision`.
* **Structure is validated at construction, not at dispatch.** An edge naming a node that
  does not exist, a duplicate `node_id`, a cycle, a terminal requirement pointing at
  nothing — each raises `WorkflowValidationError` while the DAG is still a value. A graph
  that reaches the runner has already been proved well-formed.
* **Serialisation round-trips.** `to_dict`/`from_dict` are what `workflow_revisions.dag_json`
  stores, so a controller restart reconstructs the identical graph rather than recompiling
  one and hoping it matches.

Schemas are `01_DATA_CONTRACTS_AND_SCHEMAS.md` §2.2–§2.4 verbatim. Where that document
leaves a default unstated it is stated here once, as a dataclass default, rather than being
re-guessed at every call site.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from graphlib import CycleError, TopologicalSorter
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = 1

#: Node kinds (`§2.3`). `join` is structural — it has no handler of its own; the runner
#: resolves it from its incoming edges.
NODE_KINDS = frozenset(
    {"agent", "function", "tool", "human", "wait", "decision", "join"}
)

#: Guard predicates an edge may carry (`§2.4`, `§5.1`). `None` means unguarded: the edge is
#: satisfied as soon as the source reaches any terminal phase other than `skipped`.
GUARD_PREDICATES = frozenset({"on_success", "on_failure", "is_valid", "always"})

EDGE_KINDS = frozenset({"dependency", "data_flow"})

#: `§2.3`. The class decides whether an uncertain outcome may be retried without a probe
#: (`A05`): only `pure_derivation` and `repeatable_local_check` are safe to repeat blind.
EFFECT_CLASSES = frozenset(
    {
        "pure_derivation",
        "repeatable_local_check",
        "agent_editing_worktree",
        "external_side_effect",
    }
)

IDEMPOTENT_EFFECT_CLASSES = frozenset({"pure_derivation", "repeatable_local_check"})

CACHE_POLICIES = frozenset({"immutable_memoized", "recheck_required", "never"})

EXECUTION_OBJECTIVES = frozenset({"paid_efficiency", "free_quality", "pace"})

#: Terminal phases of a node, as the runner sees them (`02_…` §2.2).
TERMINAL_PHASES = frozenset({"succeeded", "failed", "skipped"})

DEFAULT_SOURCE_PORT = "outcome"
DEFAULT_TARGET_PORT = "input"


class WorkflowError(RuntimeError):
    """Base class for every refusal this module makes."""


class WorkflowValidationError(WorkflowError):
    """A DAG that does not hold together. Carries every issue, not just the first."""

    def __init__(self, issues: Sequence[str]):
        self.issues = list(issues)
        super().__init__("; ".join(self.issues))


# ── policies ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RetryPolicy:
    """Local retry parameters (`§2.3`).

    `max_attempts` counts attempts, not retries: 1 means "run it once, never again".
    `[INV-08]` lives above this — a repair or a backend switch must not reset the
    *cumulative* counters the task carries, whatever a node's local policy says.
    """

    max_attempts: int = 1
    backoff_seconds: float = 0.0
    retryable_errors: tuple[str, ...] = ()

    def permits(self, attempt_index: int) -> bool:
        return attempt_index < self.max_attempts


@dataclass(frozen=True)
class TimeoutPolicy:
    """Wall-clock and heartbeat ceilings (`§2.3`)."""

    execution_timeout_sec: int = 4 * 3600
    idle_heartbeat_timeout_sec: int = 900


# ── the graph ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NodeSpec:
    """One executable unit inside a workflow revision (`§2.3`)."""

    node_id: str
    kind: str
    handler_ref: str
    handler_version: str = "1.0.0"
    agent_definition_ref: str | None = None
    input_bindings: Mapping[str, str] = field(default_factory=dict)
    output_schema_ref: str = "NodeResult.v1"
    activation_predicate: str | None = None
    permissions_ref: str = "task_worktree_writer"
    resource_request: Mapping[str, Any] = field(default_factory=dict)
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    timeout_policy: TimeoutPolicy = field(default_factory=TimeoutPolicy)
    effect_class: str = "pure_derivation"
    reconciliation_handler: str | None = None
    cache_policy: str = "recheck_required"
    candidate_id: str | None = None
    #: Handler parameters the compiler specialised for this task. Not in `§2.3`, which
    #: assumes a handler reads its inputs from bound artifacts; the legacy lanes carry
    #: their parameters (a `run:` command, a workspace) in the task block, and losing
    #: them at compile time would mean re-reading the roadmap at dispatch.
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        issues = []
        if not self.node_id:
            issues.append("node_id is empty")
        if self.kind not in NODE_KINDS:
            issues.append(f"{self.node_id}: unknown node kind {self.kind!r}")
        if self.effect_class not in EFFECT_CLASSES:
            issues.append(f"{self.node_id}: unknown effect_class {self.effect_class!r}")
        if self.cache_policy not in CACHE_POLICIES:
            issues.append(f"{self.node_id}: unknown cache_policy {self.cache_policy!r}")
        if self.kind == "agent" and not self.agent_definition_ref:
            issues.append(f"{self.node_id}: kind 'agent' requires agent_definition_ref")
        if issues:
            raise WorkflowValidationError(issues)

    @property
    def wants_writer_lease(self) -> bool:
        """Does dispatching this node need exclusive ownership of a writable resource?

        Declared, never inferred from the kind: a `function` node that commits to a
        worktree needs the lease exactly as much as an `agent` node does.
        """
        return bool(self.resource_request.get("writer_lease", False))

    @property
    def retry_without_probe(self) -> bool:
        """`A05`: may an uncertain outcome be re-run without a postcondition probe?"""
        if self.effect_class in IDEMPOTENT_EFFECT_CLASSES:
            return True
        return self.reconciliation_handler is not None

    def to_dict(self) -> dict:
        out = asdict(self)
        out["input_bindings"] = dict(self.input_bindings)
        out["resource_request"] = dict(self.resource_request)
        out["params"] = dict(self.params)
        out["retry_policy"]["retryable_errors"] = list(self.retry_policy.retryable_errors)
        return out

    @classmethod
    def from_dict(cls, data: Mapping) -> "NodeSpec":
        body = dict(data)
        retry = dict(body.pop("retry_policy", None) or {})
        timeout = dict(body.pop("timeout_policy", None) or {})
        retry["retryable_errors"] = tuple(retry.get("retryable_errors") or ())
        return cls(
            retry_policy=RetryPolicy(**retry),
            timeout_policy=TimeoutPolicy(**timeout),
            **body,
        )


@dataclass(frozen=True)
class EdgeSpec:
    """A guarded directed relation between two nodes (`§2.4`)."""

    source_node_id: str
    target_node_id: str
    source_port: str = DEFAULT_SOURCE_PORT
    target_port: str = DEFAULT_TARGET_PORT
    guard_predicate: str | None = None
    edge_kind: str = "dependency"
    #: `§5.1` distinguishes edges that must be satisfied from edges that merely may be.
    #: An optional edge that resolves false does not by itself skip its target.
    required: bool = True

    def __post_init__(self) -> None:
        issues = []
        if self.guard_predicate is not None and self.guard_predicate not in GUARD_PREDICATES:
            issues.append(
                f"{self.source_node_id}->{self.target_node_id}: "
                f"unknown guard {self.guard_predicate!r}"
            )
        if self.edge_kind not in EDGE_KINDS:
            issues.append(
                f"{self.source_node_id}->{self.target_node_id}: "
                f"unknown edge_kind {self.edge_kind!r}"
            )
        if self.source_node_id == self.target_node_id:
            issues.append(f"{self.source_node_id}: edge to itself")
        if issues:
            raise WorkflowValidationError(issues)

    def satisfied_by(self, phase: str) -> bool:
        """Does the source node's terminal `phase` open this edge? (`§5.1`)

        A `skipped` source never opens an edge: a node that did not run cannot stand in
        for one that did, and `[INV-04]` fails closed on exactly that substitution.
        """
        if phase not in TERMINAL_PHASES:
            return False
        if phase == "skipped":
            return False
        guard = self.guard_predicate
        if guard in (None, "always"):
            return True
        if guard in ("on_success", "is_valid"):
            return phase == "succeeded"
        if guard == "on_failure":
            return phase == "failed"
        return False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping) -> "EdgeSpec":
        return cls(**dict(data))


@dataclass(frozen=True)
class WorkflowRevision:
    """The immutable compiled execution graph for one task run (`§2.2`)."""

    workflow_id: str
    revision: int
    task_id: str
    task_spec_revision: int
    compiler_version: str
    policy_hash: str
    code_base_sha: str
    execution_objective: str
    nodes: tuple[NodeSpec, ...]
    edges: tuple[EdgeSpec, ...]
    terminal_requirements: tuple[str, ...] = ()
    parent_revision: int | None = None
    project_profile_hash: str = ""
    backend_eligibility: tuple[str, ...] = ()
    catalog_snapshot_hash: str = ""
    mutation_reason: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        issues = list(_structural_issues(self.nodes, self.edges, self.terminal_requirements))
        if self.execution_objective not in EXECUTION_OBJECTIVES:
            issues.append(f"unknown execution_objective {self.execution_objective!r}")
        if self.revision < 1:
            issues.append(f"revision {self.revision} is not a positive integer")
        if self.parent_revision is not None and self.parent_revision >= self.revision:
            issues.append(
                f"parent_revision {self.parent_revision} does not precede revision "
                f"{self.revision}"
            )
        if issues:
            raise WorkflowValidationError(issues)

    # --- lookups -------------------------------------------------------------------

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(node.node_id for node in self.nodes)

    def node(self, node_id: str) -> NodeSpec:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise KeyError(node_id)

    def incoming(self, node_id: str) -> tuple[EdgeSpec, ...]:
        return tuple(e for e in self.edges if e.target_node_id == node_id)

    def outgoing(self, node_id: str) -> tuple[EdgeSpec, ...]:
        return tuple(e for e in self.edges if e.source_node_id == node_id)

    def roots(self) -> tuple[str, ...]:
        """Nodes with no incoming edge — the initial frontier."""
        return tuple(n.node_id for n in self.nodes if not self.incoming(n.node_id))

    def topological_order(self) -> tuple[str, ...]:
        sorter = TopologicalSorter(
            {n.node_id: {e.source_node_id for e in self.incoming(n.node_id)} for n in self.nodes}
        )
        return tuple(sorter.static_order())

    def candidate_groups(self) -> dict[str, tuple[str, ...]]:
        """Nodes grouped by `candidate_id` (`[INV-06]`)."""
        groups: dict[str, list[str]] = {}
        for node in self.nodes:
            if node.candidate_id:
                groups.setdefault(node.candidate_id, []).append(node.node_id)
        return {k: tuple(v) for k, v in sorted(groups.items())}

    # --- successor revisions ---------------------------------------------------------

    def evolve(
        self,
        *,
        nodes: Iterable[NodeSpec] | None = None,
        edges: Iterable[EdgeSpec] | None = None,
        terminal_requirements: Iterable[str] | None = None,
        mutation_reason: str,
    ) -> "WorkflowRevision":
        """Revision `N` -> `N + 1` for local plan repair (`§5.3`).

        `parent_revision` is set here and nowhere else, so a revision that claims a
        parent always had one. What carries forward from `N` — completed nodes and their
        verified artifacts — is the runner's decision, not this one's; §5.3's invariant 3
        is enforced in `runner.carry_forward()`, which can see the phases.
        """
        return replace(
            self,
            revision=self.revision + 1,
            parent_revision=self.revision,
            nodes=tuple(nodes) if nodes is not None else self.nodes,
            edges=tuple(edges) if edges is not None else self.edges,
            terminal_requirements=(
                tuple(terminal_requirements)
                if terminal_requirements is not None
                else self.terminal_requirements
            ),
            mutation_reason=mutation_reason,
        )

    # --- serialisation ----------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "workflow_id": self.workflow_id,
            "revision": self.revision,
            "task_id": self.task_id,
            "task_spec_revision": self.task_spec_revision,
            "parent_revision": self.parent_revision,
            "compiler_version": self.compiler_version,
            "policy_hash": self.policy_hash,
            "project_profile_hash": self.project_profile_hash,
            "code_base_sha": self.code_base_sha,
            "execution_objective": self.execution_objective,
            "backend_eligibility": list(self.backend_eligibility),
            "catalog_snapshot_hash": self.catalog_snapshot_hash,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "terminal_requirements": list(self.terminal_requirements),
            "mutation_reason": self.mutation_reason,
            "schema_version": self.schema_version,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, data: Mapping) -> "WorkflowRevision":
        body = dict(data)
        return cls(
            nodes=tuple(NodeSpec.from_dict(n) for n in body.pop("nodes", ())),
            edges=tuple(EdgeSpec.from_dict(e) for e in body.pop("edges", ())),
            terminal_requirements=tuple(body.pop("terminal_requirements", ()) or ()),
            backend_eligibility=tuple(body.pop("backend_eligibility", ()) or ()),
            **body,
        )

    @classmethod
    def from_json(cls, raw: str) -> "WorkflowRevision":
        return cls.from_dict(json.loads(raw))

    @property
    def dag_hash(self) -> str:
        """Content hash of the graph itself — identity that survives re-serialisation."""
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()


def _structural_issues(
    nodes: Sequence[NodeSpec], edges: Sequence[EdgeSpec], terminals: Sequence[str]
) -> list[str]:
    issues: list[str] = []
    ids = [n.node_id for n in nodes]
    if not ids:
        issues.append("workflow has no nodes")
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    for dup in duplicates:
        issues.append(f"duplicate node_id {dup!r}")
    known = set(ids)
    for edge in edges:
        if edge.source_node_id not in known:
            issues.append(f"edge source {edge.source_node_id!r} is not a node")
        if edge.target_node_id not in known:
            issues.append(f"edge target {edge.target_node_id!r} is not a node")
    for terminal in terminals:
        if terminal not in known:
            issues.append(f"terminal requirement {terminal!r} is not a node")
    if not issues:
        graph = {
            n.node_id: {e.source_node_id for e in edges if e.target_node_id == n.node_id}
            for n in nodes
        }
        try:
            TopologicalSorter(graph).prepare()
        except CycleError as exc:
            cycle = " -> ".join(exc.args[1])
            issues.append(f"workflow graph has a cycle: {cycle}")
    return issues


# ── the result envelope ──────────────────────────────────────────────────────────────

#: Envelope statuses a worker may report (`§2.2` terminal node phases, plus the two
#: dispositions the legacy lanes need: a human pause and a resumable sitting).
RESULT_STATUSES = frozenset(
    {"succeeded", "failed", "uncertain", "paused", "incomplete", "handover"}
)

#: How each envelope status moves the node, and what it means for the enclosing task
#: (`02_…` §2.1/§2.2). `None` means "no node transition": the node stays where it is
#: because the work has not finished, it is waiting on something outside the process.
RESULT_DISPOSITION: dict[str, tuple[str | None, str | None]] = {
    "succeeded": ("succeeded", None),
    "failed": ("failed", None),
    "uncertain": ("uncertain", None),
    "paused": (None, "waiting"),
    "incomplete": ("failed", "waiting"),
    "handover": ("uncertain", None),
}


@dataclass(frozen=True)
class NodeResult:
    """What a worker reports back about one attempt (`NodeResult.v1`).

    This is the only thing the controller believes about a worker, and it believes it as
    a *claim about the process*, never as evidence of engineering quality: `[INV-04]` and
    `D06` mean a `succeeded` envelope moves the node, it does not accept the task.
    """

    run_id: str
    node_id: str
    attempt_id: str
    status: str
    detail: str = ""
    outputs: Mapping[str, Any] = field(default_factory=dict)
    artifact_refs: tuple[str, ...] = ()
    exit_status: int = 0
    writer_pid: int | None = None
    source: str = "worker"
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.status not in RESULT_STATUSES:
            raise WorkflowValidationError([f"unknown result status {self.status!r}"])

    @property
    def node_phase(self) -> str | None:
        return RESULT_DISPOSITION[self.status][0]

    @property
    def task_status(self) -> str | None:
        return RESULT_DISPOSITION[self.status][1]

    @property
    def is_terminal(self) -> bool:
        return self.node_phase in TERMINAL_PHASES

    def to_dict(self) -> dict:
        out = asdict(self)
        out["outputs"] = dict(self.outputs)
        out["artifact_refs"] = list(self.artifact_refs)
        return out

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, data: Mapping) -> "NodeResult":
        body = dict(data)
        body["artifact_refs"] = tuple(body.get("artifact_refs") or ())
        body.pop("node_phase", None)
        body.pop("task_status", None)
        return cls(**body)

    @classmethod
    def from_json(cls, raw: str) -> "NodeResult":
        return cls.from_dict(json.loads(raw))
