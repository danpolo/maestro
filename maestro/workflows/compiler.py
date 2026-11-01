"""Template specialisation: one `TaskSpec` -> one compiled DAG (`[P3]`, `[P5]`, `D03`).

The five execution kinds the roadmap already speaks (`ordinary`, `script`, `manual`,
`async`, `resumable`) are five *lanes* of the existing event loop — branches of one long
`for task in runnable:` body, distinguished by `if` statements scattered across 300 lines.
This module turns each of them into an explicit graph, and that is the whole of P3's
compilation story: **no lane gains or loses a step here**. `ordinary` still runs
implementer -> gate -> proof review -> merge -> accept, because that is what
`tests/graph_engineering/test_baseline_lanes.py` measured it doing. What changes is that
the sequence is now data the controller can reason about — which node is ready, which
failed, which was skipped and why — instead of a program counter inside a poll.

`D03` forbids a static monolithic project graph, so nothing here builds one: a workflow is
compiled per task, pinned to the task spec revision and the policy hash it was compiled
against, and stored immutably. Recompiling the same spec under the same policy is
deterministic — `test_compatibility.py` asserts the `dag_hash` is stable, because a
compiler whose output drifts run to run cannot support `§5.3`'s carry-forward.

Plan repair is `repair()`: revision `N + 1` with `parent_revision = N`. Retries do not
come through here at all; they re-arm a node in the existing revision, which is what keeps
`[INV-08]`'s counters attached to the task rather than to a graph that gets rebuilt.

**What P5 added.** The lanes above are no longer written out longhand: they are ordered
lists of `name@version` refs into `FRAGMENTS`, composed by `compose()`, and a project's
own `workflows/project.yaml` cites the same catalogue (`[INV-A06]`). One composer, so
there is no second path for a lane to drift along — `compile_workflow` with no policy
still emits the baseline graphs node for node, which `test_compatibility.py` pins.

With a `ProjectPolicy`, conditional stages resolve against the task's risk and the active
objective (`D05`: a low-risk task in paid mode buys no planner and no reviewer), and the
result is then checked against the policy's quality gates by `check_criteria` — against
the nodes actually built, never against what the template claimed it would build. A graph
that comes up short does not compile (`[INV-04]`).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from maestro.control.models import EventType, nanoid, new_run_id, now_iso
from maestro.taskgraph import TaskSpec
from maestro.workflows.models import (
    EdgeSpec,
    NodeSpec,
    RetryPolicy,
    TimeoutPolicy,
    WorkflowError,
    WorkflowRevision,
    WorkflowValidationError,
)
from maestro.workflows.policy import (
    FragmentRef,
    ProjectPolicy,
    RiskContext,
    StageSpec,
    TemplateSpec,
)

#: Bumped whenever a template changes shape. Stored on every revision so a graph compiled
#: by an older compiler is identifiable rather than silently assumed current.
COMPILER_VERSION = "p12b.1"

#: What an unescalated compile is stamped with. P12B F7's `diagnose` stage is the only
#: shape change, and it is opt-in, so a graph compiled without `escalation=` is the same
#: graph P7 produced and carries P7's version — which is what keeps `test_compatibility`
#: honest rather than merely passing.
BASELINE_COMPILER_VERSION = "p7.1"

#: The eight-hour ceiling the legacy loop applied to an implementer (`TASK_TIMEOUT`), and
#: the half-hour one it applied to a script lane.
IMPLEMENTER_TIMEOUT_SEC = 8 * 3600
SCRIPT_TIMEOUT_SEC = 1800
CHECK_TIMEOUT_SEC = 3600


def new_workflow_id() -> str:
    return f"wf_{nanoid(16)}"


# ── node constructors ────────────────────────────────────────────────────────────────
#
# One function per node shape rather than a table of dicts: the defaults that matter
# (which nodes take a writer lease, which are safe to retry blind) are policy, and policy
# that lives in a literal gets copy-pasted wrong.


def _implementer_node(spec: TaskSpec, *, node_id: str = "implementer") -> NodeSpec:
    return NodeSpec(
        node_id=node_id,
        kind="agent",
        handler_ref="maestro.handlers.agent",
        agent_definition_ref="implementer",
        permissions_ref="task_worktree_writer",
        resource_request={"writer_lease": True, "inference_slots": 1},
        retry_policy=RetryPolicy(max_attempts=1),
        timeout_policy=TimeoutPolicy(
            execution_timeout_sec=IMPLEMENTER_TIMEOUT_SEC,
            idle_heartbeat_timeout_sec=1800,
        ),
        # An agent that has been editing a worktree cannot be re-run blind: the edits it
        # already made are the uncertain side effect (`A05`).
        effect_class="agent_editing_worktree",
        reconciliation_handler="maestro.reconcile.worktree_probe",
        cache_policy="never",
        params={"task_id": spec.task_id, "title": spec.title},
    )


def _script_node(spec: TaskSpec) -> NodeSpec:
    command = str(spec.legacy_fields.get("run") or "").strip()
    return NodeSpec(
        node_id="script",
        kind="tool",
        handler_ref="maestro.handlers.script",
        permissions_ref="task_worktree_writer",
        resource_request={"writer_lease": True},
        retry_policy=RetryPolicy(max_attempts=1),
        timeout_policy=TimeoutPolicy(
            execution_timeout_sec=SCRIPT_TIMEOUT_SEC, idle_heartbeat_timeout_sec=SCRIPT_TIMEOUT_SEC
        ),
        # The task's own shell command. Nothing here knows whether it is idempotent, so it
        # is classified as a worktree edit and never retried without a probe.
        effect_class="agent_editing_worktree",
        cache_policy="never",
        params={"command": command, "timeout_sec": SCRIPT_TIMEOUT_SEC, "task_id": spec.task_id},
    )


def _gate_node(spec: TaskSpec, *, node_id: str = "gate") -> NodeSpec:
    """The verification gate. A repeatable local check on a frozen worktree (`[INV-04]`)."""
    return NodeSpec(
        node_id=node_id,
        kind="function",
        handler_ref="maestro.handlers.verification",
        permissions_ref="frozen_snapshot_reader",
        resource_request={"writer_lease": False},
        retry_policy=RetryPolicy(max_attempts=1),
        timeout_policy=TimeoutPolicy(execution_timeout_sec=CHECK_TIMEOUT_SEC),
        effect_class="repeatable_local_check",
        cache_policy="recheck_required",
        params={"task_id": spec.task_id},
    )


def _proof_review_node(spec: TaskSpec, *, node_id: str = "proof_review") -> NodeSpec:
    return NodeSpec(
        node_id=node_id,
        kind="agent",
        handler_ref="maestro.handlers.agent",
        agent_definition_ref="reviewer",
        permissions_ref="frozen_snapshot_reader",
        resource_request={"writer_lease": False, "inference_slots": 1},
        retry_policy=RetryPolicy(max_attempts=1),
        timeout_policy=TimeoutPolicy(execution_timeout_sec=CHECK_TIMEOUT_SEC),
        effect_class="pure_derivation",
        cache_policy="recheck_required",
        params={"task_id": spec.task_id},
    )


def _merge_node(spec: TaskSpec) -> NodeSpec:
    """Integration onto the target branch. The only node that touches it (`[INV-05]`)."""
    return NodeSpec(
        node_id="merge",
        kind="function",
        handler_ref="maestro.handlers.merge",
        permissions_ref="controller_target_branch",
        resource_request={"writer_lease": True, "resource_id": "target_branch"},
        retry_policy=RetryPolicy(max_attempts=1),
        timeout_policy=TimeoutPolicy(execution_timeout_sec=CHECK_TIMEOUT_SEC),
        effect_class="external_side_effect",
        reconciliation_handler="maestro.reconcile.merge_probe",
        cache_policy="never",
        params={"task_id": spec.task_id},
    )


def _accept_node(spec: TaskSpec) -> NodeSpec:
    return NodeSpec(
        node_id="accept",
        kind="decision",
        handler_ref="maestro.handlers.acceptance",
        permissions_ref="controller_only",
        resource_request={"writer_lease": False},
        effect_class="pure_derivation",
        cache_policy="never",
        params={"task_id": spec.task_id},
    )


def _human_node(spec: TaskSpec, *, node_id: str = "human_action") -> NodeSpec:
    """The operator's single step. `max_attempts=1` and no retry: a person is not a retry."""
    return NodeSpec(
        node_id=node_id,
        kind="human",
        handler_ref="maestro.handlers.human",
        permissions_ref="operator",
        resource_request={"writer_lease": False},
        retry_policy=RetryPolicy(max_attempts=1),
        timeout_policy=TimeoutPolicy(
            execution_timeout_sec=30 * 24 * 3600, idle_heartbeat_timeout_sec=30 * 24 * 3600
        ),
        effect_class="external_side_effect",
        reconciliation_handler="maestro.reconcile.human_receipt_probe",
        cache_policy="never",
        params={
            "task_id": spec.task_id,
            "action": spec.legacy_fields.get("manual_action")
            or f"perform the prepared step for {spec.task_id}",
        },
    )


def _wait_node(spec: TaskSpec, *, node_id: str, reason: str, **params) -> NodeSpec:
    return NodeSpec(
        node_id=node_id,
        kind="wait",
        handler_ref="maestro.handlers.wait",
        permissions_ref="controller_only",
        resource_request={"writer_lease": False},
        timeout_policy=TimeoutPolicy(
            execution_timeout_sec=7 * 24 * 3600, idle_heartbeat_timeout_sec=7 * 24 * 3600
        ),
        effect_class="pure_derivation",
        cache_policy="never",
        params={"task_id": spec.task_id, "reason": reason, **params},
    )


def _async_launch_node(spec: TaskSpec) -> NodeSpec:
    return NodeSpec(
        node_id="async_launch",
        kind="tool",
        handler_ref="maestro.handlers.script",
        permissions_ref="task_worktree_writer",
        # A `dispatch: async-job` holds a resource label but no implementer slot and no
        # worktree writer lease — the legacy loop's whole reason for the lane.
        resource_request={"writer_lease": False},
        retry_policy=RetryPolicy(max_attempts=1),
        timeout_policy=TimeoutPolicy(execution_timeout_sec=300, idle_heartbeat_timeout_sec=300),
        effect_class="external_side_effect",
        reconciliation_handler="maestro.reconcile.async_job_probe",
        cache_policy="never",
        params={
            "command": str(spec.legacy_fields.get("launch_cmd") or "").strip(),
            "timeout_sec": 300,
            "task_id": spec.task_id,
        },
    )


# ── new role nodes (`[P5]`) ──────────────────────────────────────────────────────────
#
# Neither exists in a baseline lane: they are the two nodes free-quality mode and
# high-risk work buy. Both are read-or-scratch only — the planner because a plan its
# author can implement is not a plan anyone else can review, the test designer because
# tests written into the implementation worktree are not independent evidence (`D06`).


def _planner_node(spec: TaskSpec, *, node_id: str = "planner") -> NodeSpec:
    return NodeSpec(
        node_id=node_id,
        kind="agent",
        handler_ref="maestro.handlers.agent",
        agent_definition_ref="planner",
        permissions_ref="frozen_snapshot_reader",
        resource_request={"writer_lease": False, "inference_slots": 1},
        retry_policy=RetryPolicy(max_attempts=1),
        timeout_policy=TimeoutPolicy(execution_timeout_sec=CHECK_TIMEOUT_SEC),
        effect_class="pure_derivation",
        cache_policy="recheck_required",
        output_schema_ref="PlanProposal.v1",
        params={"task_id": spec.task_id, "title": spec.title},
    )


def _diagnose_node(spec: TaskSpec, *, node_id: str = "diagnose") -> NodeSpec:
    """The last rung of P12B's ladder: read the wreckage, propose one repair.

    Shaped like the planner — it reads a frozen snapshot, holds no writer lease, and emits
    a `PlanRepairProposal` — with two deliberate differences. `cache_policy="never"`,
    because a diagnosis is about one particular run's failures and may never be served
    from a prior one; and its routing demand is raised a rung above the model that failed
    (`runner._advance_agent`), because asking the model that just produced three bad
    outputs to explain them is not an escalation.
    """
    return NodeSpec(
        node_id=node_id,
        kind="agent",
        handler_ref="maestro.handlers.agent",
        agent_definition_ref="diagnoser",
        permissions_ref="frozen_snapshot_reader",
        resource_request={"writer_lease": False, "inference_slots": 1},
        retry_policy=RetryPolicy(max_attempts=1),
        timeout_policy=TimeoutPolicy(execution_timeout_sec=CHECK_TIMEOUT_SEC),
        effect_class="pure_derivation",
        cache_policy="never",
        output_schema_ref="PlanRepairProposal.v1",
        params={"task_id": spec.task_id, "title": spec.title},
    )


def _test_designer_node(spec: TaskSpec, *, node_id: str = "test_designer") -> NodeSpec:
    return NodeSpec(
        node_id=node_id,
        kind="agent",
        handler_ref="maestro.handlers.agent",
        agent_definition_ref="test_designer",
        # A scratch workspace, never the implementer's worktree — so no writer lease on
        # the task's tree, and the two can be dispatched without contending for it.
        permissions_ref="scratch_workspace_writer",
        resource_request={"writer_lease": False, "inference_slots": 1},
        retry_policy=RetryPolicy(max_attempts=1),
        timeout_policy=TimeoutPolicy(
            execution_timeout_sec=CHECK_TIMEOUT_SEC, idle_heartbeat_timeout_sec=1800
        ),
        effect_class="agent_editing_worktree",
        reconciliation_handler="maestro.reconcile.worktree_probe",
        cache_policy="never",
        params={"task_id": spec.task_id, "title": spec.title},
    )


def _candidate_select_node(spec: TaskSpec, *, node_id: str = "candidate_select") -> NodeSpec:
    """`[INV-06]`'s join: N candidate branches in, exactly one winner out.

    A `decision` node the controller runs in process, for the same reason `accept` is one:
    the choice is made from stored evidence, and a worker that chose its own winner would
    be a candidate grading itself. `controller_only` therefore, with no writer lease — the
    join reads candidate evidence and names one branch; it does not merge it, because
    merging the target branch is `merge`'s and `[INV-05]`'s.
    """
    return NodeSpec(
        node_id=node_id,
        kind="decision",
        handler_ref="maestro.handlers.candidate_select",
        permissions_ref="controller_only",
        resource_request={"writer_lease": False},
        retry_policy=RetryPolicy(max_attempts=1),
        timeout_policy=TimeoutPolicy(execution_timeout_sec=CHECK_TIMEOUT_SEC),
        effect_class="pure_derivation",
        cache_policy="never",
        params={"task_id": spec.task_id},
    )


def _park_node(spec: TaskSpec, *, node_id: str = "park") -> NodeSpec:
    return _wait_node(spec, node_id=node_id, reason="failed upstream — parked for Dan")


def _cooldown_node(spec: TaskSpec, *, node_id: str = "cooldown") -> NodeSpec:
    return _wait_node(
        spec,
        node_id=node_id,
        reason="resumable sitting incomplete — re-queued after cooldown",
        cooldown_hours=int(spec.legacy_fields.get("resume_cooldown_h") or 20),
    )


# ── the fragment catalogue (`[INV-A06]`) ─────────────────────────────────────────────
#
# A template is an ordered list of `name@version` refs into this table, never a graph
# written out longhand. Two things follow, and both are the point:
#
# * A project's `workflows/project.yaml` cites shapes rather than defining them, so a
#   fragment whose contract changes bumps its version here and every template still
#   asking for the old one refuses to compile — instead of quietly acquiring a different
#   node.
# * The five baseline lanes and every policy-composed lane run through one composer, so
#   there is no second code path where a lane could drift.


@dataclass(frozen=True)
class Fragment:
    """One reusable, versioned node shape."""

    name: str
    version: str
    build: Callable[..., NodeSpec]

    def node(self, spec: TaskSpec, node_id: str) -> NodeSpec:
        return self.build(spec, node_id=node_id)


def _fragment(name: str, version: str, build) -> Fragment:
    return Fragment(name=name, version=version, build=build)


FRAGMENTS: dict[str, Fragment] = {
    f.name: f
    for f in (
        _fragment("implementer", "1.0.0", _implementer_node),
        _fragment("script", "1.0.0", lambda spec, *, node_id="script": _script_node(spec)),
        _fragment("planner", "1.0.0", _planner_node),
        _fragment("diagnose", "1.0.0", _diagnose_node),
        _fragment("test_designer", "1.0.0", _test_designer_node),
        _fragment("gate", "1.0.0", _gate_node),
        _fragment("proof_review", "1.0.0", _proof_review_node),
        _fragment("merge", "1.0.0", lambda spec, *, node_id="merge": _merge_node(spec)),
        _fragment("accept", "1.0.0", lambda spec, *, node_id="accept": _accept_node(spec)),
        _fragment("human_action", "1.0.0", _human_node),
        _fragment(
            "async_launch", "1.0.0",
            lambda spec, *, node_id="async_launch": _async_launch_node(spec),
        ),
        _fragment(
            "await_job", "1.0.0",
            lambda spec, *, node_id="await_job": _wait_node(
                spec, node_id=node_id,
                reason="async job running — polled by awaiting-verification",
                kind="awaiting-verification",
            ),
        ),
        _fragment("candidate_select", "1.0.0", _candidate_select_node),
        _fragment("park", "1.0.0", _park_node),
        _fragment("cooldown", "1.0.0", _cooldown_node),
    )
}


def resolve_fragment(ref: FragmentRef) -> Fragment:
    """Look a `name@version` ref up, refusing an unknown name or a stale version."""
    fragment = FRAGMENTS.get(ref.name)
    if fragment is None:
        raise WorkflowError(f"unknown workflow fragment {ref.name!r}")
    if fragment.version != ref.version:
        raise WorkflowError(
            f"fragment {ref.name!r} is at version {fragment.version}, "
            f"but the template asks for {ref.version}"
        )
    return fragment


# ── the baseline lanes, expressed as templates ───────────────────────────────────────
#
# What `compile_workflow` uses when no `ProjectPolicy` is supplied: the five lanes exactly
# as `tests/graph_engineering/test_baseline_lanes.py` measured them, with review
# unconditional. A project that has adopted a policy gets its own conditional shapes; one
# that has not gets the behaviour it had before P5 existed, node for node.


def _stage(fragment: str, *, node_id: str = "", on_failure: str | None = None) -> StageSpec:
    return StageSpec(
        fragment=FragmentRef.parse(f"{fragment}@{FRAGMENTS[fragment].version}"),
        node_id=node_id,
        on_failure=on_failure,
    )


BASELINE_TEMPLATES: dict[str, TemplateSpec] = {
    "ordinary": TemplateSpec(
        execution_kind="ordinary",
        version="1.0.0",
        stages=(
            _stage("implementer", on_failure="park"),
            _stage("gate", on_failure="park"),
            _stage("proof_review", on_failure="park"),
            _stage("merge"),
            _stage("accept"),
        ),
        sinks=(_stage("park"),),
        terminals=("accept",),
        required_criteria=("implementation", "verification_gate", "controller_merge", "acceptance"),
    ),
    "script": TemplateSpec(
        execution_kind="script",
        version="1.0.0",
        stages=(
            _stage("script", on_failure="park"),
            _stage("gate", on_failure="park"),
            _stage("merge"),
            _stage("accept"),
        ),
        sinks=(_stage("park"),),
        terminals=("accept",),
        required_criteria=("verification_gate", "controller_merge", "acceptance"),
    ),
    # 1.1.0 (G7): a merge after the gate. Legacy merged the prep branch before Dan acted
    # (`parking.handle_prep_done`); without any merge the prep commits never reached the
    # target, yet accept graduated the task. Here nothing lands before the gate has
    # passed on the tree Dan acted on (DESIGN §16).
    "manual": TemplateSpec(
        execution_kind="manual",
        version="1.1.0",
        stages=(
            _stage("implementer", node_id="prep", on_failure="park"),
            _stage("human_action"),
            _stage("gate", on_failure="park"),
            _stage("merge"),
            _stage("accept"),
        ),
        sinks=(_stage("park"),),
        terminals=("human_action", "accept"),
        required_criteria=(
            "human_receipt", "verification_gate", "controller_merge", "acceptance",
        ),
    ),
    "async": TemplateSpec(
        execution_kind="async",
        version="1.0.0",
        stages=(
            _stage("async_launch", on_failure="park"),
            _stage("await_job"),
            _stage("gate", on_failure="park"),
            _stage("accept"),
        ),
        sinks=(_stage("park"),),
        terminals=("accept",),
        required_criteria=("verification_gate", "acceptance"),
    ),
    "resumable": TemplateSpec(
        execution_kind="resumable",
        version="1.0.0",
        stages=(
            _stage("implementer", on_failure="park"),
            _stage("gate", on_failure="cooldown"),
            _stage("merge"),
            _stage("accept"),
        ),
        sinks=(_stage("cooldown"), _stage("park")),
        terminals=("accept",),
        required_criteria=("implementation", "verification_gate", "controller_merge", "acceptance"),
    ),
}


# ── composition ──────────────────────────────────────────────────────────────────────


#: `required criterion -> a property of the composed node set`. Each is checked against
#: the nodes actually built, never against what the template claimed it would build:
#: `[INV-04]` fails closed on a graph that promised a reviewer and composed none.
CRITERION_CHECKS: dict[str, Callable[[Sequence[NodeSpec]], bool]] = {
    "implementation": lambda nodes: any(
        n.effect_class == "agent_editing_worktree" and n.wants_writer_lease for n in nodes
    ),
    "verification_gate": lambda nodes: any(
        n.handler_ref == "maestro.handlers.verification" for n in nodes
    ),
    "controller_merge": lambda nodes: any(
        n.handler_ref == "maestro.handlers.merge" for n in nodes
    ),
    "acceptance": lambda nodes: any(
        n.handler_ref == "maestro.handlers.acceptance" for n in nodes
    ),
    "independent_review": lambda nodes: any(
        n.kind == "agent" and n.agent_definition_ref == "reviewer" and not n.wants_writer_lease
        for n in nodes
    ),
    "human_receipt": lambda nodes: any(n.kind == "human" for n in nodes),
    "planning": lambda nodes: any(n.agent_definition_ref == "planner" for n in nodes),
    "independent_tests": lambda nodes: any(
        n.agent_definition_ref == "test_designer" for n in nodes
    ),
    # `[P7]`. All three read the composed nodes, so a fan-out that emitted one node, or a
    # widened lane whose join stage was edited out, does not compile.
    "isolated_candidates": lambda nodes: len(_candidate_writers(nodes)) >= 2,
    "candidate_selection": lambda nodes: any(
        n.handler_ref == "maestro.handlers.candidate_select" for n in nodes
    ),
    "diverse_review": lambda nodes: len(_review_lenses(nodes)) >= 2,
}


def _candidate_writers(nodes: Sequence[NodeSpec]) -> set[str]:
    """The distinct candidate ids that hold their own writer lease (`[INV-06]`).

    Two conditions, both load-bearing: a distinct `candidate_id` is what makes a candidate
    a candidate, and a distinct exclusive `resource_id` is what keeps it isolated. Two
    implementer nodes labelled `c1` and `c2` that contend for one `worktree:<task>` lease
    are not two candidates — they are one worktree written twice.
    """
    return {
        f"{node.params.get('candidate_id')}@{node.resource_request.get('resource_id')}"
        for node in nodes
        if node.params.get("candidate_id") and node.wants_writer_lease
        and node.resource_request.get("resource_id")
    }


def _review_lenses(nodes: Sequence[NodeSpec]) -> set[str]:
    return {
        str(node.params.get("review_lens"))
        for node in nodes
        if node.agent_definition_ref == "reviewer" and node.params.get("review_lens")
    }


@dataclass(frozen=True)
class _Group:
    """One template stage and the nodes it composed into — one, or one per fan-out label.

    `labels` is parallel to `node_ids` and empty-string-padded at width 1, so the single
    node of an un-widened stage keeps the stage's own id and acquires no candidate label.
    That is what makes every pre-P7 graph compile byte-identically.
    """

    stage: StageSpec
    labels: tuple[str, ...]

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(
            f"{self.stage.node_id}_{label}" if label else self.stage.node_id
            for label in self.labels
        )

    @property
    def reaped_by_join(self) -> bool:
        """A widened candidate stage, whose failures the selection join reaps (P12A W4).

        Its nodes join the next stage on `always`, so one crashed candidate cannot skip
        the join and fire the sink before the join has marked it `abandoned`; and they get
        no `on_failure` edge of their own, because the join's `on_failure -> park` is what
        "no conclusive candidate" means. Width 1 is untouched.
        """
        return self.stage.fan_out == "candidates" and len(self.labels) >= 2


def compose(
    spec: TaskSpec,
    template: TemplateSpec,
    *,
    context: RiskContext | None = None,
    policy: ProjectPolicy | None = None,
) -> tuple[list[NodeSpec], list[EdgeSpec], list[str]]:
    """Resolve a template's fragments into the minimal graph `context` calls for.

    Conditional stages that do not apply are dropped, and a sink nothing routes to is
    dropped with them — a `park` node no edge reaches is not a safety net, it is a node
    the runner has to explain. Wiring is mechanical: every node of one surviving stage is
    joined `on_success` to every node of the next, and each stage that declares one gets an
    optional `on_failure` edge to its sink from each of its nodes. Nothing here consults
    the task beyond what the fragments read, so two compilers handed the same spec and
    template emit byte-identical graphs.

    **Fan-out is that "every to every" and nothing more** (`[P7]`). A stage the policy
    widens becomes `implementer_c1 … _cN` or `proof_review_<lens>`; the stage before it
    fans out to all of them and the stage after fans them back in. At width 1 — every
    paid-mode stage, every baseline lane — the widened node keeps the stage's own id and
    carries no candidate label, so a graph compiled before this existed is emitted
    byte-for-byte unchanged. The one exception to "on_success, plus on_failure to the
    sink" is a widened candidate stage (`_Group.reaped_by_join`, P12A W4).
    """
    context = context or RiskContext(task_id=spec.task_id)
    stages = template.selected_stages(context)
    if not stages:
        raise WorkflowError(
            f"{spec.task_id}: template {template.execution_kind} composed no stages"
        )
    used_sinks = {stage.on_failure for stage in stages if stage.on_failure}
    sinks = [sink for sink in template.sinks if sink.node_id in used_sinks]

    # Widths first, then nodes, then edges — so the edge loop can see the stage after the
    # one it is wiring and keep the ordering a single-width lane always had.
    groups: list[_Group] = []
    for stage in stages:
        labels = policy.fan_out_labels(stage, context) if policy is not None else ()
        groups.append(_Group(stage, labels or ("",)))

    nodes: list[NodeSpec] = []
    for group in groups:
        for node_id, label in zip(group.node_ids, group.labels):
            node = resolve_fragment(group.stage.fragment).node(spec, node_id)
            if node.node_id != node_id:
                # A fragment builder that discards the id it was handed would emit N
                # identically-named nodes for a widened stage. Caught here, naming the
                # fragment, rather than downstream as "duplicate node_id".
                raise WorkflowError(
                    f"{spec.task_id}: fragment {group.stage.fragment.name!r} ignored the "
                    f"node id it was given ({node_id!r} -> {node.node_id!r})"
                )
            node = _with_fan_out_identity(node, spec, group.stage.fan_out, label)
            nodes.append(_with_policy_permissions(node, policy))
    for sink in sinks:
        node = resolve_fragment(sink.fragment).node(spec, sink.node_id)
        nodes.append(_with_policy_permissions(node, policy))

    live_sinks = {sink.node_id for sink in sinks}
    edges: list[EdgeSpec] = []
    for index, group in enumerate(groups):
        reaped = group.reaped_by_join
        if index + 1 < len(groups):
            guard = "always" if reaped else "on_success"
            for source in group.node_ids:
                for target in groups[index + 1].node_ids:
                    edges.append(EdgeSpec(source, target, guard_predicate=guard))
        if group.stage.on_failure in live_sinks and not reaped:
            for source in group.node_ids:
                edges.append(
                    EdgeSpec(
                        source, group.stage.on_failure,
                        guard_predicate="on_failure", required=False,
                    )
                )

    built = {node.node_id for node in nodes}
    terminals = [t for t in template.terminals if t in built] or list(groups[-1].node_ids)
    return nodes, edges, terminals


#: The node ids `escalate` inserts, and the handler that marks a verification node.
DIAGNOSE_NODE_ID = "diagnose"
_VERIFICATION_HANDLER = "maestro.handlers.verification"


def _escalates(node: NodeSpec) -> bool:
    """Whether a failure of `node` is worth a diagnosis (P12B F7).

    An agent produced the bad output, and a verification node is where a bad output is
    usually *caught* — both are quality verdicts a stronger model can speak to. A script,
    an async launch or a controller join that fails has failed for a mechanical reason,
    and keeps its direct route to `park`.
    """
    return node.kind == "agent" or node.handler_ref == _VERIFICATION_HANDLER


def escalate(
    spec: TaskSpec,
    nodes: Sequence[NodeSpec],
    edges: Sequence[EdgeSpec],
    *,
    policy: "ProjectPolicy | None" = None,
) -> tuple[list[NodeSpec], list[EdgeSpec]]:
    """Re-point quality failures at a `diagnose` stage instead of straight at `park`.

    Runs after `compose`, on the composed graph, so a project's own lanes escalate on
    exactly the terms the baseline ones do and neither has to know about the other. Only
    `on_failure → park` edges out of escalating sources move; a `cooldown` target is a
    schedule, not a verdict, and stays. `diagnose` itself fails to `park`, which is what
    stops the ladder rather than looping it.
    """
    sources = {node.node_id: node for node in nodes}
    if DIAGNOSE_NODE_ID in sources:
        return list(nodes), list(edges)
    moved = False
    out: list[EdgeSpec] = []
    for edge in edges:
        source = sources.get(edge.source_node_id)
        if (
            edge.guard_predicate == "on_failure"
            and edge.target_node_id == "park"
            and source is not None
            and _escalates(source)
        ):
            out.append(replace(edge, target_node_id=DIAGNOSE_NODE_ID))
            moved = True
        else:
            out.append(edge)
    if not moved:
        return list(nodes), list(edges)
    diagnose = _with_policy_permissions(
        resolve_fragment(FragmentRef.parse("diagnose@1.0.0")).node(spec, DIAGNOSE_NODE_ID),
        policy,
    )
    out.append(
        EdgeSpec(DIAGNOSE_NODE_ID, "park", guard_predicate="on_failure", required=False)
    )
    return [*nodes, diagnose], out


#: The Dan-confirmation step a task with `kind: dan` verifications gains (see
#: `insert_dan_confirmation`), and the handler that runs it in the controller.
DAN_CONFIRM_NODE_ID = "dan_confirm"
DAN_CONFIRM_HANDLER = "maestro.handlers.dan_confirm"
_MERGE_HANDLER = "maestro.handlers.merge"
_ACCEPTANCE_HANDLER = "maestro.handlers.acceptance"


def dan_checks(spec: TaskSpec) -> list[dict]:
    """The task's `kind: dan` verifications as `{id, check, instructions[, steps]}`, in order.

    `steps` (optional) is what Dan must *do*, as imperative `{do, run}` steps the request
    numbers (`verify.render_request`). It is carried only when the entry declares it, so
    an entry without it compiles to the same params, and the same `dag_hash`, as before.
    """
    out = []
    for entry in spec.legacy_fields.get("verifications") or ():
        if isinstance(entry, Mapping) and str(entry.get("kind") or "") == "dan":
            check = {
                "id": str(entry.get("id") or ""),
                "check": str(entry.get("check") or ""),
                "instructions": str(entry.get("instructions") or ""),
            }
            steps = _dan_steps(entry.get("steps"))
            if steps:
                check["steps"] = steps
            out.append(check)
    return out


def _dan_steps(raw) -> list[dict]:
    """A `kind: dan` entry's `steps` as `[{do, run}]`: a plain string is an instruction
    with no command, a mapping is `{do: <imperative>, run: <command>}`. Anything else, and
    a step with neither, is dropped rather than refused: a malformed optional field must
    not stop the task, which then falls back to its `instructions`."""
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for step in raw:
        if isinstance(step, Mapping):
            do, run = str(step.get("do") or "").strip(), str(step.get("run") or "").strip()
        elif isinstance(step, str):
            do, run = step.strip(), ""
        else:
            continue
        if do or run:
            out.append({"do": do, "run": run})
    return out


def _dan_confirm_node(spec: TaskSpec, checks: Sequence[Mapping]) -> NodeSpec:
    """Dan confirms the checks only he can, on the snapshot the gate passed.

    A `human` step the controller runs in process: it asks once per snapshot over
    Telegram and pauses until Dan answers (`handlers.dan_confirm_handler`). Deciding from a
    stored answer is a pure derivation, and the request is idempotent per snapshot, so the
    node is safe to re-evaluate every tick. No writer lease — it only reads the tree.
    """
    return NodeSpec(
        node_id=DAN_CONFIRM_NODE_ID,
        kind="human",
        handler_ref=DAN_CONFIRM_HANDLER,
        permissions_ref="controller_only",
        resource_request={"writer_lease": False},
        retry_policy=RetryPolicy(max_attempts=1),
        timeout_policy=TimeoutPolicy(
            execution_timeout_sec=30 * 24 * 3600, idle_heartbeat_timeout_sec=30 * 24 * 3600
        ),
        effect_class="pure_derivation",
        cache_policy="never",
        params={
            "task_id": spec.task_id,
            "title": spec.title,
            "dan_checks": [dict(check) for check in checks],
        },
    )


#: The check a merge review adds to `dan_confirm` (`merge_review_check`).
MERGE_REVIEW_CHECK_ID = "MERGE-REVIEW"


def merge_review_check(reason: str) -> dict:
    """Dan reviews the whole change before it merges — legacy's `park_for_dan`.

    The legacy loop paused before `merge_and_eval` when `hitl_mode` was on. The graph asks the same question through the `dan_confirm` step a
    `kind: dan` check already gets: same request, same Approve/Reject, same rework on a
    rejection. Its disposition is bound to the snapshot like any other and discharges no
    acceptance criterion; it only holds the merge until Dan approves.
    """
    return {
        "id": MERGE_REVIEW_CHECK_ID,
        "check": f"review the change before it merges ({reason})",
        "instructions": "git -C {worktree} log --stat -p main..HEAD",
    }


def insert_dan_confirmation(
    spec: TaskSpec, nodes: Sequence[NodeSpec], edges: Sequence[EdgeSpec],
    *, merge_review: str = "",
) -> tuple[list[NodeSpec], list[EdgeSpec]]:
    """Keep merge approval before merge; observations of deployed behavior follow it."""
    checks = dan_checks(spec)
    by_handler = {node.handler_ref: node.node_id for node in nodes}
    merged, accept = by_handler.get(_MERGE_HANDLER), by_handler.get(_ACCEPTANCE_HANDLER)
    result, out = list(nodes), list(edges)

    def insert(anchor, node):
        nonlocal out
        if anchor is None:
            raise WorkflowError(f'{spec.task_id}: verification has no merge or accept anchor')
        out = [replace(edge, target_node_id=node.node_id)
               if edge.target_node_id == anchor and edge.guard_predicate != 'on_failure' else edge
               for edge in out]
        out.append(EdgeSpec(node.node_id, anchor, guard_predicate='on_success'))
        if node.handler_ref not in ('maestro.handlers.post_merge_checks', 'maestro.handlers.operator_check') and any(n.node_id == 'park' for n in result):
            out.append(EdgeSpec(node.node_id, 'park', guard_predicate='on_failure', required=False))
        result.append(node)

    if merge_review:
        insert(merged or accept, _dan_confirm_node(spec, [merge_review_check(merge_review)]))
    machine_checks = [v for v in spec.legacy_fields.get('verifications') or ()
                      if isinstance(v, Mapping) and v.get('kind') == 'auto' and v.get('stage') == 'post_merge']
    if machine_checks and merged is None:
        raise WorkflowError(f'{spec.task_id}: stage post_merge requires a lane with a merge')
    if machine_checks:
        insert(accept, NodeSpec(
            node_id='post_merge_checks', kind='function',
            handler_ref='maestro.handlers.post_merge_checks', permissions_ref='controller_only',
            retry_policy=RetryPolicy(max_attempts=1), cache_policy='never',
            params={'task_id': spec.task_id},
        ))
    if checks:
        operator = replace(_dan_confirm_node(spec, checks), node_id='operator_check',
                           handler_ref='maestro.handlers.operator_check')
        insert(accept, operator)
    return result, out


def check_criteria(
    spec: TaskSpec, nodes: Sequence[NodeSpec], criteria: Iterable[str]
) -> None:
    """Fail closed on a composed graph that omits a required criterion (`[INV-04]`)."""
    missing = []
    for criterion in criteria:
        check = CRITERION_CHECKS.get(criterion)
        if check is None:
            raise WorkflowError(f"{spec.task_id}: unknown required criterion {criterion!r}")
        if not check(nodes):
            missing.append(criterion)
    if missing:
        raise WorkflowError(
            f"{spec.task_id}: composed workflow does not satisfy required criteria "
            f"{missing} — refusing to compile"
        )


def _with_fan_out_identity(
    node: NodeSpec, spec: TaskSpec, fan_out: str, label: str
) -> NodeSpec:
    """Give one node of a widened stage the identity that makes it distinguishable.

    A candidate gets both halves of `[INV-06]`: the `candidate_id` a handler reads to
    derive its branch and worktree (`confinement.branch_name`, `confinement.worktree_for`),
    and an exclusive `resource_id` naming *that* worktree — without which the runner's
    default `worktree:<task>` lease would make N candidates queue for one directory, which
    is cross-pollution with extra steps. A reviewer gets its lens, which is the only thing
    that makes three reviews of one diff diverse rather than three of the same review.
    """
    if not label:
        return node
    if fan_out == "candidates":
        return replace(
            node,
            params={**dict(node.params), "candidate_id": label},
            resource_request={
                **dict(node.resource_request),
                "resource_id": f"worktree:{spec.task_id}/{label}",
            },
        )
    if fan_out == "review_lenses":
        return replace(node, params={**dict(node.params), "review_lens": label})
    return node


def _with_policy_permissions(node: NodeSpec, policy: ProjectPolicy | None) -> NodeSpec:
    """Stamp the project's permission boundary onto a role's node, if it declared one."""
    if policy is None or not node.agent_definition_ref:
        return node
    permission = policy.permission_for(node.agent_definition_ref, node.permissions_ref)
    return node if permission == node.permissions_ref else replace(node, permissions_ref=permission)


# ── compilation ──────────────────────────────────────────────────────────────────────


def policy_hash(policy: "ProjectPolicy | Mapping | None") -> str:
    """SHA256 of the engineering policy a revision was compiled under (`§2.2`).

    A `ProjectPolicy` hashes its own canonical form — role definitions included, because
    a workflow whose reviewer contract changed is not the same workflow even when the
    graph is node for node identical. A bare mapping (or `None`) keeps the pre-P5
    behaviour so a caller that never adopted a policy stamps the same hash it always did.
    """
    if isinstance(policy, ProjectPolicy):
        return policy.policy_hash
    body = json.dumps(policy or {}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def head_sha(repo: Path, *, runner=subprocess.run) -> str:
    """The target repository's HEAD at compile time. `''` when it cannot be read.

    An empty string rather than a raise: a project mid-`git init` has no HEAD, and
    refusing to compile there would make the failure look like a workflow defect.
    """
    try:
        proc = runner(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
        )
    except OSError:
        return ""
    return (proc.stdout or "").strip() if getattr(proc, "returncode", 1) == 0 else ""


def compile_workflow(
    spec: TaskSpec,
    *,
    workflow_id: str | None = None,
    revision: int = 1,
    policy: "ProjectPolicy | Mapping | None" = None,
    code_base_sha: str = "",
    execution_objective: str | None = None,
    backend_eligibility: Sequence[str] = (),
    project_profile_hash: str = "",
    catalog_snapshot_hash: str = "",
    escalation: bool = False,
    merge_review: str = "",
) -> WorkflowRevision:
    """Specialise the template for `spec.execution_kind` into an immutable revision.

    Two sources of templates, one composer. With a `ProjectPolicy` the lane is the
    project's own — conditional stages resolved against the task's risk and the active
    objective (`D05`), then checked against the policy's quality gates and refused if it
    comes up short. Without one, the five baseline lanes, unchanged from P3.

    Deterministic in everything but `workflow_id`: hand the same spec and policy to two
    compilers and the graphs are byte-identical, which is what `§5.3`'s carry-forward
    needs in order to recognise a node across a repair.

    `merge_review`, when set, is why Dan reviews the change before it merges (the graph
    loop passes it under `hitl_mode`); see `merge_review_check`.
    """
    project_policy = policy if isinstance(policy, ProjectPolicy) else None
    objective = execution_objective or (
        project_policy.objective if project_policy else "paid_efficiency"
    )
    if project_policy is not None:
        project_policy = project_policy.with_objective(objective)
        template = project_policy.template(spec.execution_kind)
        context = project_policy.risk_context(spec)
        criteria = project_policy.required_criteria(spec.execution_kind, context)
    else:
        template = BASELINE_TEMPLATES.get(spec.execution_kind)
        if template is None:
            raise WorkflowError(
                f"{spec.task_id}: no workflow template for execution_kind "
                f"{spec.execution_kind!r}"
            )
        context = RiskContext(
            task_id=spec.task_id,
            risk=spec.risk,
            execution_kind=spec.execution_kind,
            objective=objective,
        )
        criteria = template.required_criteria

    nodes, edges, terminals = compose(
        spec, template, context=context, policy=project_policy
    )
    nodes, edges = insert_dan_confirmation(spec, nodes, edges, merge_review=merge_review)
    if escalation:
        nodes, edges = escalate(spec, nodes, edges, policy=project_policy)
    if spec.resources:
        # Task minima apply to every attempt, including inserted/recovery nodes.
        # Keep them in the immutable revision consumed by the existing host gate;
        # a fragment may require more, and its leases/pool keys remain unchanged.
        nodes = [replace(node, resource_request={
            **node.resource_request,
            **{name: max(amount, node.resource_request.get(name, 0))
               for name, amount in spec.resources.items()},
        }) for node in nodes]
    check_criteria(spec, nodes, criteria)
    return WorkflowRevision(
        workflow_id=workflow_id or new_workflow_id(),
        revision=revision,
        task_id=spec.task_id,
        task_spec_revision=spec.spec_revision,
        compiler_version=COMPILER_VERSION if escalation else BASELINE_COMPILER_VERSION,
        policy_hash=policy_hash(policy),
        project_profile_hash=project_profile_hash,
        code_base_sha=code_base_sha,
        execution_objective=objective,
        backend_eligibility=tuple(backend_eligibility),
        catalog_snapshot_hash=catalog_snapshot_hash,
        nodes=tuple(nodes),
        edges=tuple(edges),
        terminal_requirements=tuple(terminals),
    )


def fallback_refusal(
    spec: TaskSpec, revision: WorkflowRevision, *, has_authoritative_specs: bool
) -> str | None:
    """Why `spec`'s `fallback: step_down` may not be honoured, or `None` (P12B F5).

    A weaker model is only acceptable when its work is checked: the revision must carry a
    verification gate and the task must have authoritative verification specs for it to
    run. Pure; the compiled revision is not changed.
    """
    if spec.fallback != "step_down":
        return None
    if not any(node.handler_ref == "maestro.handlers.verification" for node in revision.nodes):
        return "step_down needs a verification gate node in the compiled workflow"
    if not has_authoritative_specs:
        return "step_down needs authoritative verifications for the task"
    return None


def repair(
    previous: WorkflowRevision,
    *,
    reason: str,
    nodes: Iterable[NodeSpec] | None = None,
    edges: Iterable[EdgeSpec] | None = None,
    terminal_requirements: Iterable[str] | None = None,
) -> WorkflowRevision:
    """Local plan repair: revision `N` -> `N + 1` (`§5.3`).

    A thin wrapper over `WorkflowRevision.evolve` on purpose — the interesting invariants
    (carry completed nodes forward, cancel stale downstream work, never reset a counter)
    are the runner's, because only the runner can see the phases.
    """
    if not reason:
        raise WorkflowError("plan repair requires a mutation reason")
    return previous.evolve(
        nodes=nodes, edges=edges, terminal_requirements=terminal_requirements,
        mutation_reason=reason,
    )


# ── persistence ──────────────────────────────────────────────────────────────────────


def store_revision(store, revision: WorkflowRevision, *, at: str | None = None) -> None:
    """Persist a revision into `workflow_revisions`. Immutable: re-storing one raises.

    `INSERT` rather than `INSERT OR REPLACE`, so a compiler that produced a different graph
    under the same `(workflow_id, revision)` is a loud integrity error rather than a silent
    rewrite of a graph some run is already executing against.
    """
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO workflow_revisions"
            " (workflow_id, revision, task_id, task_spec_revision, parent_revision,"
            "  compiler_version, policy_hash, code_base_sha, execution_objective,"
            "  dag_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                revision.workflow_id,
                revision.revision,
                revision.task_id,
                revision.task_spec_revision,
                revision.parent_revision,
                revision.compiler_version,
                revision.policy_hash,
                revision.code_base_sha,
                revision.execution_objective,
                revision.to_json(),
                at or now_iso(),
            ),
        )


def load_revision(store, workflow_id: str, revision: int) -> WorkflowRevision | None:
    row = store.conn.execute(
        "SELECT dag_json FROM workflow_revisions WHERE workflow_id = ? AND revision = ?",
        (workflow_id, revision),
    ).fetchone()
    return WorkflowRevision.from_json(row["dag_json"]) if row else None


def latest_revision(store, workflow_id: str) -> WorkflowRevision | None:
    row = store.conn.execute(
        "SELECT dag_json FROM workflow_revisions WHERE workflow_id = ?"
        " ORDER BY revision DESC LIMIT 1",
        (workflow_id,),
    ).fetchone()
    return WorkflowRevision.from_json(row["dag_json"]) if row else None


# ── local plan repair protocol (`§5.3`, `[P10]`) ─────────────────────────────────────
#
# `repair()` above builds a successor graph. What follows is the protocol around it:
#
#   plan_repair()  pure — proposal + revision N + current phases -> RepairPlan, or refusal
#   apply_repair() durable — revision-checked; cancels obsolete consumers *first*, then
#                  stores N + 1, opens its run and seeds carried nodes and counters
#
# The split is the same one `models.py` keeps: every decision is made on values, so the
# awkward cases (a cycle, a stale proposal, an uncertain node in the cone) are refused
# before anything touches the store.

#: Phases in which a node is consuming its inputs right now.
ACTIVE_PHASES = frozenset({"ready", "claimed", "running"})

#: Phases with a live (or possibly live) writer behind them.
LIVE_PHASES = frozenset({"claimed", "running"})

REPAIR_KINDS = frozenset({"discovered_prerequisite", "partial_failure", "scope_change"})

#: A diagnoser's two readings (P12B F7). `bad_graph` must come with a graph patch and
#: `poor_agent` with a replacement for the node that failed; the runner checks that the
#: verdict and the patch agree, because a verdict no patch supports is not a diagnosis.
VERDICTS = frozenset({"bad_graph", "poor_agent", "spec_gap"})

#: Event type for a committed repair. A string, like every other `EventType` value.
WORKFLOW_REPAIRED = "WorkflowRepaired"

#: The reason prefix of an operator's remerge revision, which `repair_counters` does not
#: count against the repair quota (see `maestro.workflows.remerge`).
OPERATOR_REMERGE_REASON = "operator remerge"


class PlanRepairRejected(WorkflowError):
    """A proposal that cannot become revision `N + 1`. Nothing was written."""


class StaleRepairProposal(PlanRepairRejected):
    """The proposal, or the plan, was drawn against state that has since moved."""


@dataclass(frozen=True)
class PlanRepairProposal:
    """What a planner or diagnoser node emits (`§5.3` step 1).

    A patch, not a replacement graph: it names the revision it was drafted against, and
    everything it does not mention is kept. `rerun_nodes` is for a partial failure whose
    fix is outside the graph (a flaky fixture repaired, a dependency pinned) — the node's
    spec is unchanged but its prior success no longer counts.
    """

    workflow_id: str
    base_revision: int
    reason: str
    kind: str = "partial_failure"
    add_nodes: tuple[NodeSpec, ...] = ()
    replace_nodes: tuple[NodeSpec, ...] = ()
    remove_nodes: tuple[str, ...] = ()
    add_edges: tuple[EdgeSpec, ...] = ()
    remove_edges: tuple[tuple[str, str], ...] = ()
    rerun_nodes: tuple[str, ...] = ()
    terminal_requirements: tuple[str, ...] | None = None
    #: P12B F7: a diagnoser's reading of *why* the node failed — `bad_graph` (the plan was
    #: wrong) or `poor_agent` (the plan was fine, the brief or the model was not). A
    #: planner's W7 proposal leaves it unset, which is why `to_dict` omits it when `None`
    #: and the wire form P12A shipped is unchanged.
    verdict: str | None = None

    def __post_init__(self) -> None:
        if not self.reason:
            raise PlanRepairRejected("plan repair requires a mutation reason")
        if self.kind not in REPAIR_KINDS:
            raise PlanRepairRejected(f"unknown repair kind {self.kind!r}")
        if self.verdict is not None and self.verdict not in VERDICTS:
            raise PlanRepairRejected(f"unknown repair verdict {self.verdict!r}")
        for name in ("add_nodes", "replace_nodes", "remove_nodes", "add_edges", "rerun_nodes"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(
            self, "remove_edges", tuple((str(s), str(t)) for s, t in self.remove_edges)
        )
        if self.terminal_requirements is not None:
            object.__setattr__(self, "terminal_requirements", tuple(self.terminal_requirements))

    def to_dict(self) -> dict:
        """The JSON wire form a node result carries at `outputs["plan_repair"]` (P12A W7)."""
        data = {
            "workflow_id": self.workflow_id,
            "base_revision": self.base_revision,
            "reason": self.reason,
            "kind": self.kind,
            "add_nodes": [n.to_dict() for n in self.add_nodes],
            "replace_nodes": [n.to_dict() for n in self.replace_nodes],
            "remove_nodes": list(self.remove_nodes),
            "add_edges": [e.to_dict() for e in self.add_edges],
            "remove_edges": [[s, t] for s, t in self.remove_edges],
            "rerun_nodes": list(self.rerun_nodes),
            "terminal_requirements": (
                None if self.terminal_requirements is None
                else list(self.terminal_requirements)
            ),
        }
        if self.verdict is not None:
            data["verdict"] = self.verdict
        return data

    @classmethod
    def from_dict(cls, data: Mapping) -> "PlanRepairProposal":
        """Parse the wire form. Anything malformed is a `PlanRepairRejected`, never a crash."""
        if not isinstance(data, Mapping):
            raise PlanRepairRejected(
                f"plan repair proposal must be a mapping, got {type(data).__name__}"
            )
        try:
            terminals = data.get("terminal_requirements")
            return cls(
                workflow_id=str(data["workflow_id"]),
                base_revision=int(data["base_revision"]),
                reason=str(data.get("reason") or ""),
                kind=str(data.get("kind") or "partial_failure"),
                add_nodes=tuple(NodeSpec.from_dict(n) for n in data.get("add_nodes") or ()),
                replace_nodes=tuple(
                    NodeSpec.from_dict(n) for n in data.get("replace_nodes") or ()
                ),
                remove_nodes=tuple(str(n) for n in data.get("remove_nodes") or ()),
                add_edges=tuple(EdgeSpec.from_dict(e) for e in data.get("add_edges") or ()),
                remove_edges=tuple(
                    (str(s), str(t)) for s, t in data.get("remove_edges") or ()
                ),
                rerun_nodes=tuple(str(n) for n in data.get("rerun_nodes") or ()),
                verdict=(None if data.get("verdict") is None else str(data["verdict"])),
                terminal_requirements=(
                    None if terminals is None else tuple(str(t) for t in terminals)
                ),
            )
        except PlanRepairRejected:
            raise
        except Exception as exc:  # noqa: BLE001 — any malformed field refuses the proposal
            raise PlanRepairRejected(f"malformed plan repair proposal: {exc}") from exc


@dataclass(frozen=True)
class RepairCounters:
    """`[INV-08]`'s cumulative counters. A repair increments; nothing here ever resets.

    `node_attempts` is keyed by node id and survives a node being replaced: a rewritten
    `gate` is a different node for carry-forward, but not a fresh retry budget.
    """

    repairs_used: int = 0
    max_repairs: int | None = None
    task_attempts: int = 0
    node_attempts: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class RepairPlan:
    previous: WorkflowRevision
    revision: WorkflowRevision
    phases: Mapping[str, str]
    carried: Mapping[str, str]
    carried_artifacts: Mapping[str, tuple[str, ...]]
    invalidated: tuple[str, ...]
    cancel: tuple[str, ...]
    counters: RepairCounters


def _descendants(revision: WorkflowRevision, roots: Iterable[str]) -> set[str]:
    ids = set(revision.node_ids)
    seen: set[str] = set()
    frontier = [r for r in roots if r in ids]
    while frontier:
        node = frontier.pop()
        for edge in revision.outgoing(node):
            if edge.target_node_id not in seen:
                seen.add(edge.target_node_id)
                frontier.append(edge.target_node_id)
    return seen


def plan_repair(
    previous: WorkflowRevision,
    proposal: PlanRepairProposal,
    phases: Mapping[str, str],
    *,
    counters: RepairCounters | None = None,
    artifacts: Mapping[str, Sequence[str]] | None = None,
) -> RepairPlan:
    """Compile a proposal against revision `N` and decide what survives (`§5.3` 2–5).

    Refused, with nothing built, when: the proposal names another workflow or an older
    revision; the repair quota is spent; the patch names nodes or edges that do not
    exist; it would drop a terminal requirement (`[INV-04]` — a repair may not route
    around a gate); the result has a cycle (a discovered prerequisite that depends on its
    own dependent); or an `uncertain` node sits in the invalidated cone, since its effect
    must be reconciled before anything can decide whether it is obsolete (`A05`).

    Invalidated = every node the patch changed — added, rewritten, re-wired on its inputs,
    or marked for re-run — plus everything downstream of it, plus everything downstream
    of a removed node. Carried = the succeeded nodes outside that cone whose `NodeSpec` is
    byte-identical in both revisions.
    """
    if proposal.workflow_id != previous.workflow_id:
        raise PlanRepairRejected(
            f"proposal is for workflow {proposal.workflow_id!r}, not {previous.workflow_id!r}"
        )
    if proposal.base_revision != previous.revision:
        raise StaleRepairProposal(
            f"proposal drafted against revision {proposal.base_revision}; "
            f"current revision is {previous.revision}"
        )
    counters = counters or RepairCounters()
    if counters.max_repairs is not None and counters.repairs_used >= counters.max_repairs:
        raise PlanRepairRejected(
            f"repair quota exhausted: {counters.repairs_used}/{counters.max_repairs} used"
        )

    nodes = {n.node_id: n for n in previous.nodes}
    issues: list[str] = []
    removed = set(proposal.remove_nodes)
    for node_id in sorted(removed - set(nodes)):
        issues.append(f"cannot remove unknown node {node_id!r}")
    for node in proposal.replace_nodes:
        if node.node_id not in nodes:
            issues.append(f"cannot replace unknown node {node.node_id!r}")
    for node in proposal.add_nodes:
        if node.node_id in nodes:
            issues.append(f"cannot add {node.node_id!r}: node already exists")
    for node_id in proposal.rerun_nodes:
        if node_id not in nodes or node_id in removed:
            issues.append(f"cannot re-run unknown node {node_id!r}")
    existing_edges = {(e.source_node_id, e.target_node_id) for e in previous.edges}
    for pair in proposal.remove_edges:
        if pair not in existing_edges:
            issues.append(f"cannot remove unknown edge {pair[0]} -> {pair[1]}")
    if issues:
        raise PlanRepairRejected("; ".join(issues))

    replaced = {n.node_id: n for n in proposal.replace_nodes}
    new_nodes = [replaced.get(n.node_id, n) for n in previous.nodes if n.node_id not in removed]
    new_nodes.extend(proposal.add_nodes)
    dropped = set(proposal.remove_edges)
    new_edges = [
        e for e in previous.edges
        if (e.source_node_id, e.target_node_id) not in dropped
        and e.source_node_id not in removed and e.target_node_id not in removed
    ]
    new_edges.extend(proposal.add_edges)

    surviving_terminals = [t for t in previous.terminal_requirements if t not in removed]
    if proposal.terminal_requirements is None:
        lost = [t for t in previous.terminal_requirements if t in removed]
        if lost:
            raise PlanRepairRejected(
                f"repair removes terminal requirement(s) {', '.join(lost)} without "
                "declaring replacements"
            )
        terminals = surviving_terminals
    else:
        terminals = list(proposal.terminal_requirements)
        lost = [t for t in surviving_terminals if t not in terminals]
        if lost:
            raise PlanRepairRejected(
                f"repair drops terminal requirement(s) {', '.join(lost)}"
            )

    try:
        revision = repair(
            previous, reason=f"{proposal.kind}: {proposal.reason}",
            nodes=new_nodes, edges=new_edges, terminal_requirements=terminals,
        )
    except WorkflowValidationError as exc:
        raise PlanRepairRejected(f"repaired graph is invalid: {exc}") from exc

    def incoming(rev: WorkflowRevision, node_id: str) -> list[str]:
        return sorted(json.dumps(e.to_dict(), sort_keys=True) for e in rev.incoming(node_id))

    changed: set[str] = {n.node_id for n in proposal.add_nodes}
    changed.update(proposal.rerun_nodes)
    for node in revision.nodes:
        if node.node_id in changed or node.node_id not in nodes:
            continue
        if nodes[node.node_id] != node or incoming(previous, node.node_id) != incoming(revision, node.node_id):
            changed.add(node.node_id)
    invalidated = (
        changed
        | _descendants(revision, changed)
        | removed
        | _descendants(previous, removed)
    )

    uncertain = sorted(n for n in invalidated if phases.get(n) == "uncertain")
    if uncertain:
        raise PlanRepairRejected(
            f"uncertain node(s) {', '.join(uncertain)} must be reconciled before repair"
        )

    carried = {
        n.node_id: "succeeded"
        for n in revision.nodes
        if phases.get(n.node_id) == "succeeded"
        and n.node_id not in invalidated
        and nodes.get(n.node_id) == n
    }
    order = previous.topological_order()
    cancel = tuple(
        n for n in order if n in invalidated and phases.get(n, "pending") in ACTIVE_PHASES
    )
    return RepairPlan(
        previous=previous,
        revision=revision,
        phases=dict(phases),
        carried=carried,
        carried_artifacts={
            n: tuple(artifacts[n]) for n in carried if artifacts and n in artifacts
        },
        invalidated=tuple(
            n for n in dict.fromkeys([*order, *revision.topological_order()]) if n in invalidated
        ),
        cancel=cancel,
        counters=replace(
            counters,
            repairs_used=counters.repairs_used + 1,
            node_attempts=dict(counters.node_attempts),
        ),
    )


def repair_counters(
    store, workflow_id: str, *, run_id: str | None = None, max_repairs: int | None = None
) -> RepairCounters:
    """Read `[INV-08]`'s counters back out of history rather than out of a mutable field.

    Repairs are the count of revisions with a parent; task attempts are every attempt any
    run of the task ever claimed. Both are derived from append-only rows, which is what
    makes them impossible to reset by accident.
    """
    # An operator's remerge (`maestro.workflows.remerge`) is a revision with a parent too,
    # but not a repair the diagnoser spent: it does not draw on `max_repairs`.
    repairs = store.conn.execute(
        "SELECT COUNT(*) AS n FROM workflow_revisions"
        " WHERE workflow_id = ? AND parent_revision IS NOT NULL"
        " AND COALESCE(json_extract(dag_json, '$.mutation_reason'), '') NOT LIKE ?",
        (workflow_id, f"partial_failure: {OPERATOR_REMERGE_REASON}%"),
    ).fetchone()["n"]
    task_attempts = store.conn.execute(
        "SELECT COUNT(*) AS n FROM attempts a JOIN task_runs r ON a.run_id = r.run_id"
        " WHERE r.workflow_id = ?",
        (workflow_id,),
    ).fetchone()["n"]
    node_attempts: dict[str, int] = {}
    if run_id is not None:
        for row in store.conn.execute(
            "SELECT node_id, attempt_index FROM node_states WHERE run_id = ?", (run_id,)
        ):
            node_attempts[row["node_id"]] = int(row["attempt_index"])
    return RepairCounters(
        repairs_used=int(repairs), max_repairs=max_repairs,
        task_attempts=int(task_attempts), node_attempts=node_attempts,
    )


def apply_repair(
    store,
    plan: RepairPlan,
    *,
    old_run_id: str,
    lifecycle=None,
    run_id: str | None = None,
    is_alive: Callable[[int], bool] | None = None,
    terminate: Callable[[int, int], None] = os.kill,
    grace_seconds: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    at: str | None = None,
    keep_failed: bool = False,
    fresh_attempts: Iterable[str] = (),
    trigger_attempt_id: str | None = None,
) -> str:
    """Commit a `RepairPlan`: cancel, then store `N + 1`, then open its run. Returns the run id.

    Revision-checked twice: the stored latest revision must still be `N`, and every node
    phase the plan was drawn against must still hold — a node that finished, failed or
    started since would make the plan's carry/cancel sets wrong. Either mismatch raises
    `StaleRepairProposal` and nothing is written.

    Cancellation comes before the new revision exists (`§5.3` step 4). A live writer is
    stopped through `ControlStore.revoke_lease` (SIGTERM, grace, SIGKILL, verify); one that
    survives, or a claim with no bound PID to prove stopped, refuses the repair. A live
    writer *outside* the cone refuses it too: its attempt belongs to the old run, and
    P10 does not move a running process between runs.

    The new run's `node_states` are seeded with the old attempt indices for every node id
    that survives, carried or not, so `pre_validate`'s retry ceiling keeps counting —
    except the `fresh_attempts` nodes, whose budget restarts in the new run (an operator's
    remerge after fixing the branch by hand is not a retry of the conflict that failed).

    `keep_failed` leaves an old run that already settled `failed` as `failed` instead of
    rewriting it `canceled`: the operator's remerge follows a run that really failed, and
    its history should say so.
    """
    from maestro.control.lifecycle import Lifecycle
    from maestro.control.store import pid_alive

    life = lifecycle or Lifecycle(store)
    alive = is_alive or pid_alive
    stamp = at or now_iso()
    previous, revision = plan.previous, plan.revision

    latest = latest_revision(store, previous.workflow_id)
    if latest is None or latest.revision != previous.revision:
        raise StaleRepairProposal(
            f"workflow {previous.workflow_id} is at revision "
            f"{latest.revision if latest else 'none'}, plan is against {previous.revision}"
        )
    run = store.conn.execute("SELECT * FROM task_runs WHERE run_id = ?", (old_run_id,)).fetchone()
    if run is None or run["workflow_id"] != previous.workflow_id or int(
        run["workflow_revision"]
    ) != previous.revision:
        raise PlanRepairRejected(f"run {old_run_id} is not a run of revision {previous.revision}")
    if run["status"] not in ("running", "paused", "failed"):
        raise PlanRepairRejected(f"run {old_run_id} is {run['status']}; nothing to repair")

    rows = {
        r["node_id"]: r
        for r in store.conn.execute("SELECT * FROM node_states WHERE run_id = ?", (old_run_id,))
    }
    current = {n: (rows[n]["phase"] if n in rows else "pending") for n in previous.node_ids}
    moved = sorted(n for n in previous.node_ids if current[n] != plan.phases.get(n, "pending"))
    if moved:
        raise StaleRepairProposal(f"node phase(s) moved since the plan was drawn: {', '.join(moved)}")
    outside = sorted(
        n for n in previous.node_ids if current[n] in LIVE_PHASES and n not in plan.cancel
    )
    if outside:
        raise PlanRepairRejected(
            f"live attempt(s) outside the repair cone ({', '.join(outside)}) must settle first"
        )

    # --- step 4: stop obsolete consumers before N + 1 can exist ---------------------
    for node_id in plan.cancel:
        row = rows.get(node_id)
        phase = current[node_id]
        if phase == "ready":
            life.set_node_phase(old_run_id, node_id, "skipped", at=stamp)
            continue
        attempt_id = row["last_attempt_id"] if row else None
        lease = store.conn.execute(
            "SELECT * FROM writer_leases WHERE owner_attempt_id = ?", (attempt_id,)
        ).fetchone()
        attempt = store.conn.execute(
            "SELECT writer_pid FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if lease is not None:
            # The lease row's writer_pid is NOT NULL and holds a placeholder until the
            # worker binds; only the attempt's own writer_pid says a writer was bound.
            # Revoking on the placeholder would signal the wrong process.
            if attempt is None or attempt["writer_pid"] is None:
                raise PlanRepairRejected(
                    f"{node_id}: claimed attempt {attempt_id} has no bound writer PID; "
                    "its termination cannot be proved"
                )
            if not store.revoke_lease(
                lease["resource_id"], at=stamp, is_alive=alive, terminate=terminate,
                grace_seconds=grace_seconds, sleep=sleep,
            ):
                raise PlanRepairRejected(
                    f"{node_id}: writer {lease['writer_pid']} survived SIGKILL; repair refused"
                )
        if attempt_id:
            for intent in store.intents_for_attempt(attempt_id):
                if intent["status"] in ("pending", "dispatched"):
                    store.set_intent_status(intent["operation_id"], "failed")
            life.finish_attempt(attempt_id, run_id=old_run_id, status="failed", at=stamp)
        life.set_node_phase(old_run_id, node_id, "failed", attempt_id=attempt_id, at=stamp)

    if not (keep_failed and run["status"] == "failed"):
        life.finish_run(previous.task_id, old_run_id, "canceled", at=stamp)
    fresh = set(fresh_attempts)

    # --- steps 2–3: revision N + 1 and its run, carrying what survived -----------------
    store_revision(store, revision, at=stamp)
    new_run = run_id or new_run_id()
    life.start_run(
        revision.task_id, new_run, workflow_id=revision.workflow_id,
        workflow_revision=revision.revision, at=stamp,
    )
    with store.transaction() as conn:
        for node_id in revision.node_ids:
            carried = node_id in plan.carried
            prior = None if node_id in fresh and not carried else rows.get(node_id)
            index = int(prior["attempt_index"]) if prior else 0
            if not carried and index == 0:
                continue
            phase = "succeeded" if carried else "pending"
            last = prior["last_attempt_id"] if prior else None
            # P12B F4: the ladder counters are cumulative across repairs (`[INV-08]`).
            conn.execute(
                "INSERT INTO node_states"
                " (run_id, node_id, phase, attempt_index, last_attempt_id, updated_at,"
                "  blocked_attempts, quality_failures, excluded_routes_json, first_blocked_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_run, node_id, phase, index, last, stamp,
                    int(prior["blocked_attempts"] or 0) if prior else 0,
                    int(prior["quality_failures"] or 0) if prior else 0,
                    prior["excluded_routes_json"] if prior else None,
                    prior["first_blocked_at"] if prior else None,
                ),
            )
            store.append_event(
                EventType.NODE_PHASE_CHANGED, new_run,
                payload={
                    "run_id": new_run, "node_id": node_id, "phase": phase,
                    "attempt_index": index, "carried_forward_from": old_run_id,
                    "artifacts": list(plan.carried_artifacts.get(node_id, ())),
                },
                run_id=new_run, attempt_id=last, recorded_at=stamp, conn=conn,
            )
        store.append_event(
            WORKFLOW_REPAIRED, revision.task_id,
            payload={
                "workflow_id": revision.workflow_id,
                "from_revision": previous.revision,
                "to_revision": revision.revision,
                "reason": revision.mutation_reason,
                "old_run_id": old_run_id,
                "new_run_id": new_run,
                "carried": sorted(plan.carried),
                "invalidated": list(plan.invalidated),
                "canceled": list(plan.cancel),
                "repairs_used": plan.counters.repairs_used,
                **({"trigger_attempt_id": trigger_attempt_id} if trigger_attempt_id else {}),
            },
            run_id=new_run, recorded_at=stamp, conn=conn,
        )
    return new_run
