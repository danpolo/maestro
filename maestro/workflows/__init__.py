"""Workflow compilation and execution: lanes as explicit, durable DAGs (`[P3]`).

`models.py` is the graph as a value, `policy.py` is the project's engineering policy and
the five role contracts a graph is composed under (`[P5]`), `discovery.py` derives the
project profile `init` compiles against, `compiler.py` builds one graph per task,
`runner.py` is the
controller's non-blocking tick, `handlers.py` is what a supervised worker actually runs,
and `worker.py` is the process it runs in. Crash recovery for all of it lives one package
over, in `maestro.control.reconcile`, because reconciliation is an authority concern.
"""
from __future__ import annotations

from maestro.workflows.compiler import (
    BASELINE_COMPILER_VERSION,
    BASELINE_TEMPLATES,
    COMPILER_VERSION,
    FRAGMENTS,
    Fragment,
    check_criteria,
    compile_workflow,
    compose,
    load_revision,
    repair,
    resolve_fragment,
    store_revision,
)
from maestro.workflows.discovery import (
    Fact,
    ProjectProfile,
    Unresolved,
    discover,
)
from maestro.workflows.handlers import (
    AttemptInbox,
    HandlerContext,
    SentinelReading,
    TmuxDispatcher,
    read_sentinel,
    result_from_workspace,
)
from maestro.workflows.models import (
    EdgeSpec,
    NodeResult,
    NodeSpec,
    RetryPolicy,
    TimeoutPolicy,
    WorkflowError,
    WorkflowRevision,
    WorkflowValidationError,
)
from maestro.workflows.policy import (
    AgentDefinition,
    FragmentRef,
    PolicyError,
    ProjectPolicy,
    QualityGates,
    RiskContext,
    StageSpec,
    TemplateSpec,
    default_policy,
)
from maestro.workflows.runner import (
    Claim,
    TickReport,
    WorkflowRunner,
    carry_forward,
    node_is_ready,
    node_is_skipped,
    propagate_skips,
    ready_frontier,
    run_outcome,
    start_run,
)

__all__ = [
    "AgentDefinition",
    "AttemptInbox",
    "BASELINE_COMPILER_VERSION",
    "BASELINE_TEMPLATES",
    "COMPILER_VERSION",
    "Claim",
    "EdgeSpec",
    "FRAGMENTS",
    "Fact",
    "Fragment",
    "FragmentRef",
    "HandlerContext",
    "NodeResult",
    "NodeSpec",
    "PolicyError",
    "ProjectPolicy",
    "ProjectProfile",
    "QualityGates",
    "RetryPolicy",
    "RiskContext",
    "SentinelReading",
    "StageSpec",
    "TemplateSpec",
    "TickReport",
    "TimeoutPolicy",
    "TmuxDispatcher",
    "Unresolved",
    "WorkflowError",
    "WorkflowRevision",
    "WorkflowRunner",
    "WorkflowValidationError",
    "carry_forward",
    "check_criteria",
    "compile_workflow",
    "compose",
    "default_policy",
    "discover",
    "load_revision",
    "node_is_ready",
    "node_is_skipped",
    "propagate_skips",
    "read_sentinel",
    "ready_frontier",
    "repair",
    "resolve_fragment",
    "result_from_workspace",
    "run_outcome",
    "start_run",
    "store_revision",
]
