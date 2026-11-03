"""The non-blocking controller tick and the 6-step controlled-operation protocol (`[P3]`).

`02_EXECUTION_ENGINE_AND_DURABILITY.md` §1.1 gives the canonical `tick(project)`; this is
it, with the frontier computation split out as a pure function so the interesting
decisions can be tested without a database.

The shape of the change from the legacy loop is worth stating plainly, because everything
else follows from it. The old loop *was* the worker: it called `subprocess.run(...)` for a
script lane and blocked for up to half an hour, it ran gates inline, it slept. A crash
anywhere in that window lost the fact that work had been started at all. Here the
controller never blocks on work. It claims, dispatches into a supervised TMUX window, and
returns; the next tick reads what the worker left behind. Every tick is therefore
restartable, and the recovery path is the ordinary path (`control.reconcile`).

**The 6-step protocol** (`§3`) is `dispatch_node`, and its step boundaries are load-bearing:

    1. pre-validate      — inputs, handler, permissions, resource availability
    2. claim + intent    — ONE transaction: epoch++, lease row, intent row, claim event
    3. dispatch          — OUT of transaction: tmux new-window; intent -> 'dispatched'
    4. PID bind          — the worker names its own writing PID; the lease rebinds to it
    5. execute           — the worker works, heartbeating
    6. receipt           — the worker commits a receipt; the controller ingests it

Step 2 being one transaction is what makes "crash after launch but before receipt" safe: a
launch that is not preceded by a committed claim cannot happen, so a crash anywhere after
it finds a lease naming a PID, and the reconciler adjudicates that PID rather than
dispatching a second writer. Step 3 being *outside* the transaction is equally deliberate —
holding a SQLite write transaction across a process launch would serialise the whole
control plane behind tmux.

**Guards, joins and skips** (`§5.1`, `§5.2`) live in `ready_frontier` / `propagate_skips`.
A node is ready when every *required* incoming edge is satisfied by its source's terminal
phase; it is skipped when a required edge is closed for good. The rule that makes this
safe is in `EdgeSpec.satisfied_by`: a `skipped` source never satisfies an edge, so a
skipped branch cannot discharge a terminal verification requirement (`[INV-04]`).
"""
from __future__ import annotations

import json
import os
import re
import signal
import time
from collections.abc import Callable, Mapping, Sequence
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path

from maestro.control.artifacts import ArtifactStore
from maestro.control.lifecycle import Lifecycle
from maestro.control.models import (
    attempt_id as make_attempt_id,
    new_operation_id,
    new_run_id,
    now_iso,
)
from maestro.control.reconcile import ReconcileReport, Reconciler, resource_is_free
from maestro.control.store import ControlStore, SingleWriterViolation, pid_alive
from maestro.scheduling import EXCEEDS_CAPACITY
from maestro.workflows import failures as wf_failures
from maestro.workflows import goal_progress, review_origins
from maestro.workflows.compiler import load_revision, store_revision
from maestro.workflows.handlers import (
    AttemptInbox,
    Dispatched,
    TmuxDispatcher,
    attempts_root,
    window_name,
)
from maestro.workflows.models import (
    TERMINAL_PHASES,
    EdgeSpec,
    NodeResult,
    NodeSpec,
    RetryPolicy,
    WorkflowError,
    WorkflowRevision,
)

#: Phases a node can be in before anything has happened to it.
UNSTARTED = frozenset({"pending", "ready"})

#: Permission scopes that mean "the controller performs this itself" (`[P4]`). The
#: compiler already marks the merge node `controller_target_branch` and the accept node
#: `controller_only`; this is where that declaration starts having teeth.
#: `operator` (G7): a manual lane's `human_action` asks Dan through the controller's
#: channel and waits on his answer; no worker process has anything to do for it.
CONTROLLER_PERMISSIONS = frozenset({"controller_only", "controller_target_branch", "operator"})

#: P13 W8: the phases `ctl retry` / `ctl cancel` may stop a node in.
OPERATOR_STOPPABLE_PHASES = ("claimed", "running", "uncertain")


#: The store event a routing refusal leaves on its attempt, until W3's `route_refusal`
#: column exists to hold it.
ROUTE_REFUSED_EVENT = "RouteRefused"
#: The store event an unexpected exception inside `advance` leaves on its run (payload:
#: node_id, attempt_id, error, traceback, decision). `decision` is `failed` when no worker
#: had launched, `left_to_worker` when one had, `undecided` when deciding raised too.
CONTROLLER_ERROR_EVENT = "ControllerError"
#: How much of a controller error's traceback the event keeps: the frames nearest the raise.
_TRACEBACK_TAIL_CHARS = 4000


#: Permission scopes whose nodes run in the task's worktree (`_p12_open_threads` #1): the
#: writer that edits it, the readers that check a frozen view of it, and the merge node that
#: freezes and integrates it. A writer's tree is created at claim (`task_tree.ensure`).
TASK_TREE_PERMISSIONS = frozenset(
    {"task_worktree_writer", "frozen_snapshot_reader", "controller_target_branch"}
)
#: The store event a settled run leaves when its task worktree is released (or kept).
TASK_TREE_RELEASED_EVENT = "TaskTreeReleased"
#: The store event a run leaves when its first writer is handed a task worktree: whether
#: one was already there, and with uncommitted changes.
TASK_TREE_PREPARED_EVENT = "TaskTreePrepared"


#: The candidate join's handler; `node_inputs` hands it the reaped candidate documents.
CANDIDATE_SELECT_HANDLER = "maestro.handlers.candidate_select"
#: The verification gate's handler: under routing it is handed an affected-check
#: selection at claim, and its shadow-mode results are recorded as comparisons (W5/W6).
VERIFICATION_HANDLER = "maestro.handlers.verification"
#: The controller nodes that judge the task against its declared checks: merge re-runs
#: them on the integrated tree, accept decides on the evidence (`node_inputs`).
ACCEPTANCE_HANDLER = "maestro.handlers.acceptance"
#: The `kind: dan` confirmation step (`compiler.insert_dan_confirmation`): paused until Dan
#: answers, and re-evaluated every tick rather than once (`_repoll_operator_nodes`).
DAN_CONFIRM_HANDLER = "maestro.handlers.dan_confirm"
#: G7: the manual lane's human step and the async/cooldown waits. Like `dan_confirm` they
#: report `paused` until something outside the run resolves them, so they are re-polled.
HUMAN_HANDLER = "maestro.handlers.human"
OPERATOR_CHECK_HANDLER = 'maestro.handlers.operator_check'
WAIT_HANDLER = "maestro.handlers.wait"
REPOLLED_HANDLERS = frozenset({DAN_CONFIRM_HANDLER, HUMAN_HANDLER, WAIT_HANDLER, OPERATOR_CHECK_HANDLER})
#: D26: store events for an agent's spec question to Dan and his answer.
DAN_QUESTION_ASKED_EVENT = "DanQuestionAsked"
DAN_QUESTION_ANSWERED_EVENT = "DanQuestionAnswered"
#: P12 pilot finding D29: a deny-list hit put to Dan, and his ruling on it.
DENY_LIST_HIT_ASKED_EVENT = "DenyListHitAsked"
DENY_LIST_HIT_RULED_EVENT = "DenyListHitRuled"
#: P12 pilot finding D30: an agent's request to allow a command family, and its outcome.
PERMISSION_ASKED_EVENT = "PermissionAsked"
PERMISSION_RULED_EVENT = "PermissionRuled"
PERMISSION_REFUSED_EVENT = "PermissionRefused"
#: The failure sink stays a sink: nothing re-polls it (the operator reopens the task).
PARK_NODE_ID = "park"
MERGE_HANDLER = "maestro.handlers.merge"
SPEC_JUDGING_HANDLERS = frozenset({MERGE_HANDLER, ACCEPTANCE_HANDLER})
#: W7: the only agent roles whose `outputs["plan_repair"]` the controller listens to.
PLAN_REPAIR_ROLES = frozenset({"planner", "diagnoser"})
#: W7: the store event a refused plan repair leaves (payload: node_id, reason).
WORKFLOW_REPAIR_REFUSED = "WorkflowRepairRefused"
#: Dan, 2026-09-26: a round-2+ review whose findings' origins were missing or unusable
#: (payload: node_id, round, flags). The review itself stands; this is for the counts.
REVIEW_ORIGIN_FLAGGED_EVENT = "ReviewOriginFlagged"
#: Dan, 2026-09-28: a rework's manifest took in files a blocking finding named outside
#: the task's `scope:` (payload: node_id, paths). The ROADMAP is not rewritten.
SCOPE_WIDENED_EVENT = "ScopeWidenedForRework"


class DispatchRefused(WorkflowError):
    """Pre-validation or resource admission said no. Not an error — a `None` claim."""


class OperatorRefused(WorkflowError):
    """P13 W8: `ctl retry` / `ctl cancel` asked for something the run's state forbids."""


class PaceJournalUnavailable(WorkflowError):
    """Mandatory preclaim evidence could not be stored; retry without an attempt."""


def _finding_records(review: Mapping, round_no: int) -> list[dict]:
    """A disposition's findings as records with ids (Dan, 2026-09-26).

    A review adopted since findings carried ids stores its own `finding_records`; an
    older one is numbered here, blocking findings first, the way it would read.
    """
    stored = review.get("finding_records")
    if isinstance(stored, list) and all(isinstance(r, Mapping) for r in stored):
        if review.get("round"):
            return [dict(r) for r in stored]
        # Adopted without a known round (`f<n>`): the history gives it one.
        return [
            {**dict(r), "id": review_origins.finding_id(round_no, n)}
            for n, r in enumerate(stored, 1)
        ]
    texts = [("blocking", f) for f in review.get("findings") or ()]
    texts += [("follow_up", f) for f in review.get("follow_ups") or ()]
    return [
        {"id": review_origins.finding_id(round_no, n), "severity": severity,
         "finding": str(text), "origin": None, "prior_finding": None}
        for n, (severity, text) in enumerate(texts, 1)
    ]


#: A repository path as a finding spells it (`duetflow/auth.py`, `auth.py:42`): a
#: match against the snapshot's files (`context._widened_for_rework`) decides if it is one.
_PATH_TOKEN = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_./-]*\.[A-Za-z0-9]+")


def _path_tokens(text: str, known_paths=()) -> list[str]:
    out = list(dict.fromkeys(m.group(0) for m in _PATH_TOKEN.finditer(text)
                             if m.start() == 0 or text[m.start() - 1] != "/"))
    known = set(known_paths)
    names = {path.rsplit("/", 1)[-1] for path in known}
    # Extensionless existing files and explicit new paths are legitimate rework too.
    tokens = [m.group(0).rstrip(".") for m in re.finditer(r"/?[A-Za-z0-9_.][A-Za-z0-9_./-]*", text)]
    quoted = re.findall(r"`([A-Za-z0-9_.][A-Za-z0-9_./-]*)`", text)
    for token in (*tokens, *quoted):
        path = token.removeprefix("./")
        if ("/" in path or path in known or path in names or token in quoted) and token not in out:
            out.append(token)
    return out


# ── the pure frontier ────────────────────────────────────────────────────────────────


def edge_state(revision: WorkflowRevision, edge: EdgeSpec, phases: Mapping[str, str]) -> str:
    """`'open'`, `'closed'` or `'pending'` for one edge, given its source's phase.

    Three states rather than a boolean because a required edge whose source has not
    finished is not the same as one whose source finished the wrong way: the first makes
    the target wait, the second makes it skip.
    """
    source_phase = phases.get(edge.source_node_id, "pending")
    if source_phase not in TERMINAL_PHASES:
        return "pending"
    return "open" if edge.satisfied_by(source_phase) else "closed"


def node_is_ready(
    revision: WorkflowRevision, node_id: str, phases: Mapping[str, str]
) -> bool:
    """AND over required edges, OR over optional ones (`§5.2`).

    A node with no incoming edges is a root and is ready immediately. Required edges are
    AND-joined. Optional edges are the park/cooldown branches: they exist to *receive* a
    failure, so they never hold back a node that also has required edges — but a node whose
    edges are *all* optional is exactly a receiver, and it must not fire until one of them
    opens. Treating "no required edges" as "is a root" dispatches every park node on the
    first tick, which is the bug this distinction exists to prevent.
    """
    incoming = revision.incoming(node_id)
    if not incoming:
        return True
    required = [e for e in incoming if e.required]
    if required:
        return all(edge_state(revision, e, phases) == "open" for e in required)
    return any(edge_state(revision, e, phases) == "open" for e in incoming)


def node_is_skipped(
    revision: WorkflowRevision, node_id: str, phases: Mapping[str, str]
) -> bool:
    """`§5.1`: the node can no longer run, so it never will.

    The quantifier differs by edge kind, and both readings are forced by a real case:

    * **Required edges: ANY closed skips.** An AND-join needs all of them, so one closed
      required edge decides the node however well its siblings do. A diamond whose left
      branch failed is skipped at the join even once the right branch succeeds.
    * **Optional-only edges: ALL closed skips.** A receiver is still live while any branch
      that could route to it is undecided.

    A `pending` edge means undecided, and an undecided node is never skipped — that is what
    keeps a sibling branch running rather than being cancelled by its neighbour's failure.
    """
    incoming = revision.incoming(node_id)
    if not incoming:
        return False
    required = [e for e in incoming if e.required]
    if required:
        states = [edge_state(revision, e, phases) for e in required]
        return any(state == "closed" for state in states)
    states = [edge_state(revision, e, phases) for e in incoming]
    if any(state == "pending" for state in states):
        return False
    return all(state == "closed" for state in states)


def propagate_skips(
    revision: WorkflowRevision, phases: Mapping[str, str]
) -> dict[str, str]:
    """Close out every node that can no longer run, transitively.

    Returns the *new* phases only. Iterates to a fixed point because a skip closes the
    edges out of the skipped node, which can skip its successors in turn — a failed
    implementer skips the gate, which skips the review, which skips the merge.
    """
    resolved = dict(phases)
    changed: dict[str, str] = {}
    while True:
        progressed = False
        for node in revision.nodes:
            if resolved.get(node.node_id, "pending") not in UNSTARTED:
                continue
            if node_is_skipped(revision, node.node_id, resolved):
                resolved[node.node_id] = "skipped"
                changed[node.node_id] = "skipped"
                progressed = True
        if not progressed:
            return changed


def ready_frontier(
    revision: WorkflowRevision, phases: Mapping[str, str]
) -> tuple[str, ...]:
    """Nodes that may be dispatched now, in topological order.

    Topological rather than declaration order so a tick that can dispatch several nodes
    dispatches them in an order a reader can predict; the graph makes them independent, so
    the order is cosmetic, but a deterministic tick is a debuggable one.
    """
    order = revision.topological_order()
    return tuple(
        node_id
        for node_id in order
        if phases.get(node_id, "pending") in UNSTARTED
        and not node_is_skipped(revision, node_id, phases)
        and node_is_ready(revision, node_id, phases)
    )


def run_outcome(
    revision: WorkflowRevision, phases: Mapping[str, str]
) -> str | None:
    """`'succeeded'`, `'failed'` or `None` (still running) for the whole revision.

    Terminal requirements decide, and they decide strictly: a requirement that was
    *skipped* fails the run. `§5.1`'s "a skipped node MUST NOT satisfy a required terminal
    verification check" is this line, and it is the difference between a workflow that
    routes around its own gate and one that cannot.
    """
    required = revision.terminal_requirements or revision.node_ids
    outcomes = [phases.get(node_id, "pending") for node_id in required]
    if all(outcome == "succeeded" for outcome in outcomes):
        return "succeeded"
    if any(outcome in ("failed", "skipped") for outcome in outcomes):
        return "failed"
    if all(outcome in TERMINAL_PHASES for outcome in outcomes):
        return "failed"
    return None


def carry_forward(
    previous: WorkflowRevision,
    repaired: WorkflowRevision,
    phases: Mapping[str, str],
) -> dict[str, str]:
    """`§5.3` invariant 3: which completed nodes survive a plan repair.

    A node carries forward when it exists in both revisions *unchanged* and succeeded.
    Identity is the `NodeSpec`, not the id: a repair that rewrites what `gate` runs has
    produced a different node that happens to share a name, and reusing the old node's
    proof would be exactly the stale-evidence substitution `D06` rejects.
    """
    carried: dict[str, str] = {}
    for node in repaired.nodes:
        if phases.get(node.node_id) != "succeeded":
            continue
        try:
            if previous.node(node.node_id) == node:
                carried[node.node_id] = "succeeded"
        except KeyError:
            continue
    return carried


# ── the controller ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Claim:
    """A committed intent to do one thing, before that thing has been done (`§3` step 2)."""

    attempt_id: str
    run_id: str
    node_id: str
    operation_id: str
    resource_id: str | None
    lease_epoch: int
    attempt_index: int
    inbox: AttemptInbox
    #: P12A W1/W2 — set only for an agent node advanced under routing.
    binding: object | None = None
    plan: object | None = None
    manifest_artifact_id: str | None = None
    session: object | None = None
    #: P12A W5 — the stored affected-check selection of a verification node.
    selection_artifact_id: str | None = None
    #: P12B F7 — the stored failure context a diagnoser node reads.
    failure_context_artifact_id: str | None = None


@dataclass
class TickReport:
    """What one tick did. The controller's own account of itself."""

    run_id: str
    reconciled: ReconcileReport | None = None
    ingested: list[str] = field(default_factory=list)
    dispatched: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    refused: dict[str, str] = field(default_factory=dict)
    #: Nodes left unclaimed this tick, with why (`route_<reason>: <detail>`, W8 refusals).
    waiting: dict[str, str] = field(default_factory=dict)
    #: P12B F5: for a routed wait, the seconds until the soonest known reset (or `None`).
    wait_retry: dict[str, float | None] = field(default_factory=dict)
    #: Actual selected pool for each atomic B7 refusal, never inferred from text.
    budget_wait_pools: dict[str, str] = field(default_factory=dict)
    outcome: str | None = None
    #: W7: the run id this tick's plan repair moved the loop to; the old run is canceled.
    repaired_to: str | None = None

    @property
    def idle(self) -> bool:
        return not (self.ingested or self.dispatched or self.skipped)


class WorkflowRunner:
    """Executes one compiled revision against the control store.

    One runner per run, not per project: the run id is the thing every event, lease and
    inbox directory is keyed by, and threading it through every method as an argument was
    the first version of this class. It read worse and made it possible to pass the wrong one.
    """

    def __init__(
        self,
        store: ControlStore,
        revision: WorkflowRevision,
        *,
        run_id: str,
        repo: Path,
        lifecycle: Lifecycle | None = None,
        dispatcher: TmuxDispatcher | None = None,
        artifacts: ArtifactStore | None = None,
        is_alive: Callable[[int], bool] = pid_alive,
        worker_argv: Callable[["WorkflowRunner", NodeSpec, Claim], list[str]] | None = None,
        routing=None,
        dan_channel=None,
    ):
        self.store = store
        #: The operator channel a `dan_confirm` node asks through
        #: (`maestro.hitl.verify.DanVerificationChannel`), wired by the graph loop. `None`
        #: leaves such a node paused, visibly, with nothing sent.
        self.dan_channel = dan_channel
        #: `maestro.workflows.routing.DispatchRouting`, or `None` for unrouted dispatch.
        self.routing = routing
        self._attempt_inputs: dict[str, dict] = {}
        self.revision = revision
        self.run_id = run_id
        self.repo = Path(repo)
        self.lifecycle = lifecycle or Lifecycle(store)
        self.dispatcher = dispatcher or TmuxDispatcher()
        self.artifacts = artifacts
        self.is_alive = is_alive
        self.attempts_root = attempts_root(self.repo)
        self._worker_argv = worker_argv or _default_worker_argv
        #: W7: the runner of the repaired revision, once a plan repair has committed.
        self.successor: WorkflowRunner | None = None
        self.reconciler = Reconciler(
            store, self.lifecycle, attempts_root=self.attempts_root, is_alive=is_alive
        )

    # --- state ---------------------------------------------------------------------

    def phases(self) -> dict[str, str]:
        """Current phase of every node in the revision, defaulting to `pending`."""
        rows = self.store.conn.execute(
            "SELECT node_id, phase FROM node_states WHERE run_id = ?", (self.run_id,)
        ).fetchall()
        known = {row["node_id"]: row["phase"] for row in rows}
        return {node_id: known.get(node_id, "pending") for node_id in self.revision.node_ids}

    def attempt_index(self, node_id: str) -> int:
        row = self.store.conn.execute(
            "SELECT attempt_index FROM node_states WHERE run_id = ? AND node_id = ?",
            (self.run_id, node_id),
        ).fetchone()
        return int(row["attempt_index"]) if row else 0

    def inbox(self, attempt_id: str) -> AttemptInbox:
        return AttemptInbox(self.attempts_root, attempt_id)

    def resource_id_for(self, node: NodeSpec) -> str | None:
        """Which resource this node needs exclusively, if any.

        Defaults to the task's worktree, which is the resource the lanes actually contend
        for. A node may name another (`merge` names `target_branch`, `[INV-05]`).
        """
        if not node.wants_writer_lease:
            return None
        declared = node.resource_request.get("resource_id")
        if declared:
            return str(declared)
        return f"worktree:{self.revision.task_id}"

    # --- the tick ------------------------------------------------------------------

    def tick(self, *, at: str | None = None) -> TickReport:
        """One non-blocking cycle (`§1.1`). Never launches work it did not first record."""
        stamp = at or now_iso()
        report = TickReport(run_id=self.run_id)

        # 1. ingest worker completion envelopes — after re-asking any paused operator
        #    step, so an answer Dan gave since the last tick is committed in this one
        self._repoll_operator_nodes(at=stamp)
        # D26: record Dan's answers to agents' spec questions, which release their nodes
        self._poll_dan_questions(at=stamp)
        # D29: record Dan's rulings on deny-list hits, which release the gate
        self._poll_deny_rulings(at=stamp)
        # D30: record Dan's rulings on agents' permission requests, which release them
        self._poll_permission_rulings(at=stamp)
        report.ingested = self.ingest_results(at=stamp)
        if self.successor is not None:
            # W7: a plan repair canceled this run; the successor's nodes dispatch on the
            # next poll through its own `advance`. Nothing more happens to this run.
            report.repaired_to = self.successor.run_id
            return report

        # 2. reconcile leases, dead processes, uncertain operations
        report.reconciled = self.reconciler.reconcile(at=stamp)
        if self.routing is not None:
            # P12B F8: a writer that died with no receipt still owes its node a decision.
            self._decide_dead_writers(stamp, report=report.reconciled)
            self.routing.reconcile_budget(self)

        # 3. close out branches that can no longer run, before deciding what is ready —
        #    a skip is a state change, and dispatching against stale phases would launch
        #    a node whose parent has already failed.
        report.skipped = self.apply_skips(at=stamp)

        # 4. advance the frontier
        for node_id in ready_frontier(self.revision, self.phases()):
            node = self.revision.node(node_id)
            try:
                self.advance(node, at=stamp, report=report)
            except PaceJournalUnavailable as exc:
                report.waiting[node.node_id] = f'pace_journal_unavailable: {exc}'
            except Exception as exc:  # noqa: BLE001 — decided and recorded, never swallowed
                self._decide_controller_error(node, exc, at=stamp, report=report)
            finally:
                if self.routing is not None:
                    # A preclaim exception has no attempt for terminal hooks to
                    # release; a postclaim controller error may just have ended it.
                    self.routing.reconcile_budget(self)

        # 5. export: the run's own terminal state
        report.outcome = self.settle(at=stamp)
        return report

    def _decide_dead_writers(self, at: str, *, report: ReconcileReport | None = None) -> None:
        """P12B F8: decide the nodes whose writer died with no receipt.

        The reconciler settles such an attempt (`uncertain`, A05) and stops there, which
        left the node `claimed`/`running` forever. This reads durable state rather than
        `report`, so a second controller process decides the same thing; `report` only
        lends its wording to the evidence when this tick produced it.

        - A candidate reaped by the join (no `on_failure` edge of its own) is written
          `failed` with no `outcome_ref`, so the join reports it `abandoned`.
        - Any other writer is a `worker_crashed` bad output through F6, retried from its
          `start_sha` while the budget lasts.
        """
        rows = self.store.conn.execute(
            "SELECT a.attempt_id, a.node_id, a.status FROM node_states n"
            " JOIN attempts a ON a.attempt_id = n.last_attempt_id"
            " WHERE n.run_id = ? AND n.phase IN ('claimed', 'running')"
            " AND a.status IN ('uncertain', 'failed')",
            (self.run_id,),
        ).fetchall()
        details = {v.attempt_id: v.detail for v in report or ()}
        for row in rows:
            attempt_id, node_id = row["attempt_id"], row["node_id"]
            if self.inbox(attempt_id).receipt() is not None:
                continue            # the work finished; ingest owns it, not this
            node = self.revision.node(node_id)
            self._record_end_sha(attempt_id, node_id, at=at)
            record = wf_failures.FailureRecord(
                kind="worker_crashed",
                evidence=details.get(attempt_id) or "writer died with no receipt",
            )
            reaped_by_join = node.params.get("candidate_id") and not any(
                edge.guard_predicate == "on_failure" for edge in self.revision.outgoing(node_id)
            )
            if reaped_by_join:
                self.lifecycle.fail_dead_writer_node(
                    attempt_id, run_id=self.run_id, node_id=node_id, record=record, at=at,
                )
                continue
            probe = _POSTCONDITION_PROBES.get(node.reconciliation_handler or "")
            cone = wf_failures.retry_cone(self.revision, node_id, kind=record.kind)
            if cone:
                # An agent's work is confined to its tree and redone from `start_sha` (F6);
                # its probe only says what the dead writer left there.
                if probe is not None:
                    record = dataclasses.replace(
                        record, evidence=f"{record.evidence}; probe: {_ask(probe, self, node)[1]}",
                    )
                self._commit_quality_failure(row, record, cone, outcome_ref=None, at=at)
            elif probe is not None:
                self._decide_by_probe(probe, node, attempt_id, record, at=at)
            else:
                # P13 W8: no probe to ask and no writer to redo the work. Failing the node
                # is the escalation; leaving it `running` held the run open forever.
                self.lifecycle.fail_dead_writer_node(
                    attempt_id, run_id=self.run_id, node_id=node_id, record=record, at=at,
                )

    def _decide_by_probe(self, probe, node: NodeSpec, attempt_id: str, record, *, at: str) -> None:
        """A05 / spec §4.1: a dead non-idempotent node is decided by its postcondition probe.

        `False` (the effect provably did not happen) re-runs the node. Anything else — the
        effect landed but its result was never recorded, or the probe cannot tell — is
        escalated by failing the node; it is never replayed blind.
        """
        landed, evidence = _ask(probe, self, node)
        evidence = f"{record.evidence}; probe: {evidence}"
        if landed is False:
            record = wf_failures.FailureRecord(kind="interrupted", evidence=evidence)
            self.lifecycle.requeue_probed(
                attempt_id, run_id=self.run_id, node_id=node.node_id, record=record, at=at,
            )
            return
        self.lifecycle.fail_dead_writer_node(
            attempt_id, run_id=self.run_id, node_id=node.node_id,
            record=dataclasses.replace(record, evidence=evidence), at=at,
        )

    def _decide_controller_error(
        self, node: NodeSpec, exc: Exception, *, at: str, report: TickReport
    ) -> None:
        """P12: an exception `advance` did not expect still leaves a decided node.

        Before a worker launched, the claim is failed (made first, when the error came
        before one existed) with `controller_error: <type>: <message>` as its refusal, so
        the node's `on_failure` edge runs. After the launch the node belongs to its worker
        and the ingest path; failing it would orphan a live writer. Either way the error is
        a `ControllerError` store event carrying the traceback's tail.
        """
        import traceback

        reason = f"controller_error: {type(exc).__name__}: {exc}"
        tail = "".join(traceback.format_exception(exc))[-_TRACEBACK_TAIL_CHARS:]
        attempt_id, decision = None, "undecided"
        try:
            attempt_id, decision = self._fail_after_controller_error(node, reason, at, report)
        except Exception as second:  # noqa: BLE001 — the event below still records both
            tail += f"\ndeciding the node raised too: {type(second).__name__}: {second}"
        self.store.append_event(
            CONTROLLER_ERROR_EVENT, self.run_id,
            payload={"node_id": node.node_id, "attempt_id": attempt_id, "error": reason,
                     "traceback": tail, "decision": decision},
            run_id=self.run_id, attempt_id=attempt_id, recorded_at=at,
        )

    def _fail_after_controller_error(
        self, node: NodeSpec, reason: str, at: str, report: TickReport
    ) -> tuple[str | None, str]:
        """`(attempt_id, decision)` for `_decide_controller_error`, read from durable state."""
        row = self.store.conn.execute(
            "SELECT n.phase, n.last_attempt_id, o.operation_id, o.status, o.intent_payload"
            " FROM node_states n LEFT JOIN operation_intents o ON o.attempt_id = n.last_attempt_id"
            " WHERE n.run_id = ? AND n.node_id = ? ORDER BY o.rowid DESC LIMIT 1",
            (self.run_id, node.node_id),
        ).fetchone()
        if row is None or row["phase"] not in ("claimed", "running"):
            self._refuse(node, reason, at=at, report=report)
            failed = self.phases().get(node.node_id) == "failed"
            last = self.store.conn.execute(
                "SELECT last_attempt_id FROM node_states WHERE run_id = ? AND node_id = ?",
                (self.run_id, node.node_id),
            ).fetchone()
            return (last["last_attempt_id"] if last else None), ("failed" if failed else "undecided")
        attempt_id = row["last_attempt_id"]
        if row["status"] != "pending":
            return attempt_id, "left_to_worker"
        payload = json.loads(row["intent_payload"])
        claim = Claim(
            attempt_id=attempt_id,
            run_id=self.run_id,
            node_id=node.node_id,
            operation_id=row["operation_id"],
            resource_id=payload.get("resource_id"),
            lease_epoch=int(payload.get("lease_epoch") or 0),
            attempt_index=self.attempt_index(node.node_id),
            inbox=self.inbox(attempt_id),
        )
        self._fail_claim(node, claim, reason, at)
        report.refused[node.node_id] = reason
        return attempt_id, "failed"

    def advance(self, node: NodeSpec, *, at: str, report: TickReport) -> None:
        """Admit, route, claim and launch one ready node — `tick`'s only per-node call.

        The one function that owns "admit -> manifest -> resolve -> claim -> launch", so the
        binding can decide the claim deadline without a second path to `launch_worker`.
        Without routing (or for a non-agent node) it is exactly the old claim + dispatch.
        """
        waiting_on = self._awaiting_dan(node.node_id)
        if waiting_on:
            # D26: an agent asked Dan about a case the spec never defined (D29: or the gate
            # asked him about a deny-list hit); this node re-runs only once every question
            # of that round is answered. Nothing is spent here.
            report.waiting[node.node_id] = "dan_question: " + ", ".join(waiting_on)
            return
        routing = self.routing
        if node.kind == "agent":
            try:
                self.pre_validate(node)
            except DispatchRefused as refusal:
                reason = str(refusal)
                report.refused[node.node_id] = reason
                previous = self.store.conn.execute(
                    "SELECT payload FROM events WHERE type = ? AND run_id = ? AND aggregate_id = ?",
                    ("PreValidationRefused", self.run_id, node.node_id),
                ).fetchall()
                if not any(json.loads(row["payload"]).get("reason") == reason for row in previous):
                    self.store.append_event(
                        "PreValidationRefused", node.node_id,
                        payload={"node_id": node.node_id, "reason": reason},
                        run_id=self.run_id, recorded_at=at,
                    )
                    if self.dan_channel is not None and hasattr(self.dan_channel, "journal"):
                        self.dan_channel.journal(
                            "pre_validation_refused", f"{self.revision.task_id} {node.node_id}: {reason}",
                        )
                return
        if routing is not None:
            answer = routing.admit(self, node)
            if answer is not None:
                text = f"{answer.reason}: {answer.detail}"
                if answer.reason == EXCEEDS_CAPACITY:
                    # W9: larger than the whole host — waiting would never end.
                    self._refuse(node, text, at=at, report=report)
                    return
                report.waiting[node.node_id] = text
                return
        if routing is not None and node.kind == "agent":
            self._advance_agent(node, at=at, report=report)
            return
        try:
            claim = self.claim(node, at=at)
        except DispatchRefused as refusal:
            report.refused[node.node_id] = str(refusal)
            return
        try:
            self._prepare_task_tree(node, at=at)
        except Exception as exc:  # noqa: BLE001 — no tree, nothing to run the node in
            self._fail_claim(node, claim, f"worktree_unavailable: {exc}", at)
            report.refused[node.node_id] = f"worktree_unavailable: {exc}"
            return
        if routing is not None and node.handler_ref == VERIFICATION_HANDLER:
            claim = self._select_affected_checks(node, claim, at=at)
        self.dispatch(node, claim, at=at)
        report.dispatched.append(node.node_id)

    def _select_affected_checks(self, node: NodeSpec, claim: Claim, *, at: str) -> Claim:
        """W5: which of the task's checks this verification attempt runs.

        The change set is the diff between the node's tree and the run's `code_base_sha`;
        when it cannot be computed it is `None`, which selects the full suite. The
        selection is stored as an artifact the worker reads, and handed to an in-process
        execution through `node_inputs`. In `shadow` mode it is always the full suite.
        """
        import dataclasses
        import json

        from maestro import gates, validation

        try:
            specs = gates.authoritative_specs(self.revision.task_id)
        except Exception:  # noqa: BLE001 — the gate reports unreadable specs itself
            return claim
        tree = self.worktree_for(node) or self.repo
        base = self.revision.code_base_sha
        try:
            changed = validation.changed_paths(tree, base) if base else None
        except Exception:  # noqa: BLE001 — an unknown change set is a full-suite change
            changed = None
        if self.lifecycle.stepped_down_attempts(
            self.run_id, workflow_id=self.revision.workflow_id
        ):
            # P12B F5: work from a stepped-down model is checked by the whole suite —
            # and F7's repaired run inherits that, because the weaker model's work is
            # still in the tree the repaired run builds on.
            selection = dataclasses.replace(
                validation.select_affected_checks(specs, changed, mode="shadow"),
                expansion_reason="stepped_down",
            )
        else:
            selection = validation.select_affected_checks(
                specs, changed, mode=self.routing.affected_mode
            )
        document = selection.to_dict()
        artifact_id = None
        if self.artifacts is not None:
            ref = self.artifacts.put_bytes(
                json.dumps(document, sort_keys=True).encode("utf-8"),
                type="affected_selection",
                producer_attempt_id=claim.attempt_id,
                at=at,
            )
            artifact_id = ref.artifact_id
        self._attempt_inputs[node.node_id] = {"affected_selection": document}
        return dataclasses.replace(claim, selection_artifact_id=artifact_id)

    def _advance_agent(self, node: NodeSpec, *, at: str, report: TickReport) -> None:
        """W2 manifest, W1 binding (resolved exactly once), claim, record, launch."""
        import dataclasses

        from maestro.workflows import routing as wf_routing

        routing = self.routing
        diagnoser = node.agent_definition_ref == wf_failures.ROLE_DIAGNOSER
        if diagnoser:
            spent = self._repair_budget_exhausted()
            if spent is not None:
                # P12B F7: the run has already spent its one repair, so a diagnosis could
                # not be acted on even if it were right. Refused before routing, because
                # the cheapest escalation is the one not launched.
                self._refuse(node, f"repair_budget_exhausted: {spent}", at=at, report=report)
                return
        try:
            manifest = wf_routing.build_manifest(routing, self, node)
        except wf_routing.MANIFEST_REFUSALS as exc:
            # A stale or unknown snapshot never reaches the router.
            self._refuse(node, f"context_manifest_refused: {exc}", at=at, report=report)
            return
        self._note_scope_widened(node, manifest, at=at)
        reviewer = node.agent_definition_ref == wf_failures.ROLE_REVIEWER
        reviewed_commit, risky_touched, review_floor = None, [], None
        if reviewer:
            # P12 finding D10: what the diff actually touched, and — when that includes a
            # risky file — a reviewer strictly stronger than the tree's writer, or none.
            reviewed_commit, risky_touched = self.risky_files_touched(self.worktree_for(node))
            if risky_touched is None:
                self._refuse(
                    node,
                    "review_diff_unavailable: could not diff the reviewed tree against "
                    f"the run's base {self.revision.code_base_sha[:12] or 'target branch'}",
                    at=at, report=report,
                )
                return
            if risky_touched:
                review_floor, refusal = self._review_strength_floor(node)
                if refusal:
                    self._refuse(node, refusal, at=at, report=report)
                    return
        counters = self.lifecycle.node_counters(self.run_id, node.node_id)
        now_epoch = time.time()
        resolved = wf_routing.resolve(
            routing, node, manifest,
            exclude=counters["excluded_routes"], now_epoch=now_epoch,
            task_id=self.revision.task_id,
            stepped_down_before=bool(
                self.lifecycle.stepped_down_attempts(
                    self.run_id, node.node_id, workflow_id=self.revision.workflow_id
                )
            ),
            # A diagnoser is not retried and never steps down, so neither preference
            # applies to it; what it gets instead is a floor above the model that failed.
            soft_exclude=frozenset() if diagnoser else self._soft_exclude(node, counters),
            strength_floor=(
                self._escalated_strength(node) if diagnoser else review_floor
            ),
            billing_only=diagnoser,
            independent_families=self._independent_families(node),
            history_store=self.store,
        )
        if not resolved.is_bound:
            self._record_pace_decision(node, resolved, manifest, at=at,
                                       outcome=resolved.disposition)
            text = f"route_{resolved.reason}: {resolved.detail}"
            if resolved.disposition == "wait":
                try:
                    from maestro.workflows.telemetry_runtime import observe_route_wait
                    observe_route_wait(self, node, resolved, at=at)
                except Exception:
                    pass  # nullable diagnostics cannot alter admission
                if self._blocked_wait_exceeded(counters, resolved, now_epoch):
                    self._refuse(
                        node,
                        f"route_blocked_wait_exceeded: waited since "
                        f"{counters['first_blocked_at']} on a pool with no known reset "
                        f"(limit {routing.max_blocked_wait_sec}s); {resolved.detail}",
                        at=at, report=report,
                    )
                    return
                retry = resolved.retry_after_sec
                if retry is not None:
                    # P12B F5: a known reset is waited for, however far away.
                    reset_at = time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_epoch + retry)
                    )
                    text = f"{text} (resets at {reset_at})"
                report.waiting[node.node_id] = text
                report.wait_retry[node.node_id] = retry
                return
            self._refuse(node, text, at=at, report=report)
            return
        binding = resolved
        budget = routing.reserve_selected(self, node, binding, now_epoch)
        if budget is not None:
            binding = dataclasses.replace(binding, pace_decision=binding.pace_decision.with_budget(
                budget.budget_decision['inputs'], budget.budget_decision['result']))
            if budget.admission.admitted:
                binding = dataclasses.replace(binding, pace_budget={
                    'pool_id': binding.usage_pool_id, 'amounts': dict(budget.reservation.amounts),
                    **budget.reservation.budget})
            self._record_pace_decision(node, binding, manifest, at=at,
                outcome='admitted' if budget.admission.admitted else 'wait')
            if not budget.admission.admitted:
                report.waiting[node.node_id] = (f'{budget.admission.reason}: '
                                               f'{budget.admission.detail}')
                report.budget_wait_pools[node.node_id] = binding.usage_pool_id
                return
        try:
            claim = self.claim(
                node, at=at, deadline_sec=wf_routing.deadline_sec(binding, node)
            )
        except DispatchRefused as refusal:
            from maestro.control.models import attempt_id as make_attempt_id
            routing.attempt_finished(self, make_attempt_id(
                self.run_id, node.node_id, self.attempt_index(node.node_id) + 1))
            report.refused[node.node_id] = str(refusal)
            return
        manifest_doc = manifest.to_dict()
        from maestro.workflows import runaway as wf_runaway

        forecast_contract = wf_runaway.forecast_contract(routing, node)
        if forecast_contract:
            manifest_doc["forecast_contract"] = forecast_contract
        if reviewer and reviewed_commit:
            # The commit this review speaks for: merge accepts its approval for no other.
            manifest_doc["reviewed_commit"] = reviewed_commit
        if risky_touched:
            # Rendered into the reviewer's brief by `worker.agent_brief`.
            manifest_doc["risky_files_touched"] = list(risky_touched)
        if reviewer:
            rounds = self.review_rounds()
            if rounds:
                # P12 pilot finding D15: rendered into the reviewer's brief too.
                manifest_doc["prior_reviews"] = self.review_history(rounds)
                # Dan, 2026-09-26: the ids a round-2+ finding may cite as its
                # prior_finding, and the commit the last round reviewed, which the
                # reviewer diffs and blames to tell missed_earlier from a rework's defect.
                manifest_doc["prior_finding_ids"] = [
                    r["id"] for entry in rounds for r in entry["records"]
                ]
                commits = [entry["commit"] for entry in rounds if entry["commit"]]
                if commits:
                    manifest_doc["prior_reviewed_commit"] = commits[-1]
            manifest_doc["review_round"] = len(rounds) + 1
        if reviewer or node.permissions_ref == "task_worktree_writer":
            from maestro.docs import followups as doc_followups

            # A follow-up task's bar: each item done or declined, no new scope. Rendered
            # into the writer's and the reviewer's brief by `worker.agent_brief`.
            bar = doc_followups.contract(
                _roadmap_task(self.revision.task_id), reviewer=reviewer)
            if bar:
                manifest_doc["followup_contract"] = bar
        if diagnoser:
            # P12 pilot finding D17: what its plan_repair.json may name.
            manifest_doc["repair_contract"] = self.repair_contract(node)
            runaway_contract = self._runaway_contract()
            if runaway_contract:
                manifest_doc["runaway_contract"] = runaway_contract
            origins = review_origins.summary(
                [(entry["round"], entry["records"]) for entry in self.review_rounds()]
            )
            if origins:
                # Dan, 2026-09-26: whose misses the review rounds were, for its verdict.
                manifest_doc["review_origins"] = origins
        role = node.agent_definition_ref or ""
        from maestro import backlog

        roadmap_task = _roadmap_task(self.revision.task_id)
        goal_fields, goals_cut = goal_progress.manifest_fields(
            self.repo, roadmap_task,
            # GDT-11: a triage writer reads the `triage` excerpt (every non-goal kept).
            role="triage" if role == "implementer" and backlog.is_triage(roadmap_task)
            else role,
            # GDT-7: the reviewer judges by the same decisions as the gate.
            decisions=self.goal_decisions() if role == "reviewer" else None,
            # GDT-9 review M4: and by its red-on-base notes on this tree.
            red_on_base=self.gate_red_on_base() if role == "reviewer" else ())
        if goal_fields:
            # P13 W11a: the bounded GOALS.md excerpt for every role (`worker.agent_brief`),
            # and a reviewer's goal_progress check.
            manifest_doc.update(goal_fields)
            if goals_cut:
                manifest_doc["omitted_material"] = [
                    *manifest_doc.get("omitted_material", ()), goals_cut]
        decided = self.dan_decisions()
        if decided:
            # D26: Dan's answers to earlier spec questions, binding for every role.
            manifest_doc["dan_decisions"] = decided
        permission_rulings = self.permission_rulings()
        if permission_rulings:
            # D30: Dan's rulings on this workflow's permission requests, for every role.
            manifest_doc["permission_rulings"] = permission_rulings
        if node.params.get("brief_addendum"):
            # P12 pilot finding D17: a `poor_agent` diagnosis, for this node's final run.
            manifest_doc["brief_addendum"] = str(node.params["brief_addendum"])
        if node.permissions_ref == "task_worktree_writer":
            note = self.rework_note()
            if note:
                # Rendered into the brief by `worker.agent_brief`.
                manifest_doc["rework_note"] = note
            answers = _dan_answers(self.revision.task_id)
            if answers:
                # B12: what Dan answered to the task's `questions:` before it opened.
                manifest_doc["dan_answers"] = answers
            if self.has_manual_step():
                # G7: the manual lane's prep agent gets legacy's B14 contract.
                manifest_doc["prep_contract"] = prep_contract(self.revision.task_id)
        manifest_artifact_id = None
        if self.artifacts is not None:
            import json

            ref = self.artifacts.put_bytes(
                json.dumps(manifest_doc, sort_keys=True).encode("utf-8"),
                type="context_manifest",
                producer_attempt_id=claim.attempt_id,
                source_snapshot_id=manifest.snapshot_id,
                at=at,
            )
            manifest_artifact_id = ref.artifact_id
        self._attempt_inputs[node.node_id] = {"context_manifest": manifest_doc}
        claim = dataclasses.replace(
            claim,
            binding=binding,
            plan=wf_routing.plan_for(binding),
            manifest_artifact_id=manifest_artifact_id,
        )
        claim = dataclasses.replace(
            claim, session=wf_routing.session_decision(self, node, claim)
        )
        if diagnoser:
            claim = self._attach_failure_context(node, claim, at=at)
        try:
            self._prepare_task_tree(node, at=at)
        except Exception as exc:  # noqa: BLE001 — no tree, nothing to run the writer in
            self._fail_claim(node, claim, f"worktree_unavailable: {exc}", at)
            report.refused[node.node_id] = f"worktree_unavailable: {exc}"
            return
        try:
            start_sha = self._restore_start_tree(node, counters)
        except Exception as exc:  # noqa: BLE001 — a tree we cannot restore is not runnable
            self._fail_claim(node, claim, f"retry_restore_failed: {exc}", at)
            report.refused[node.node_id] = f"retry_restore_failed: {exc}"
            return
        self._record_routing_decision(claim, at=at, start_sha=start_sha)
        self.dispatch(node, claim, at=at)
        report.dispatched.append(node.node_id)

    # --- P12B F7: the diagnoser's inputs and its escalated demand -------------------

    def _failing_node(self, node: NodeSpec) -> NodeSpec | None:
        """The node whose failure opened `node`'s (the diagnoser's) `on_failure` edge.

        Read from durable phases rather than from the tick that got here, so a diagnosis
        resumed by a second controller process diagnoses the same failure the first one
        would have. With more than one failed source — a widened stage, say — the first in
        topological order is taken: the earliest failure is the one the rest descend from.
        """
        phases = self.phases()
        sources = {
            edge.source_node_id
            for edge in self.revision.edges
            if edge.target_node_id == node.node_id and edge.guard_predicate == "on_failure"
        }
        for node_id in self.revision.topological_order():
            if node_id in sources and phases.get(node_id) == "failed":
                return self.revision.node(node_id)
        return None

    def _last_binding(self, node_id: str):
        """The newest bound attempt, including its requested pair effort."""
        row = self.store.conn.execute(
            "SELECT backend_id, model_id, binding_json FROM attempts WHERE run_id = ? AND node_id = ?"
            " AND backend_id IS NOT NULL ORDER BY rowid DESC LIMIT 1",
            (self.run_id, node_id),
        ).fetchone()
        if row is None or not (row["backend_id"] and row["model_id"]):
            return None
        return row

    def _independent_families(self, node: NodeSpec) -> frozenset[str]:
        """Families of actual upstream authors, including retained work across repairs.

        A retry can keep or amend a failed writer's draft. Use the same retained-tree
        evidence as the reviewer strength gate, and each repair's own revision, rather
        than treating its last binding as the tree's only author. Unknown provenance
        cannot prove independence; it triggers the router's logged soft fallback.
        """
        import json
        roles = self.routing.role_independence.get(node.agent_definition_ref, ())
        if not roles:
            return frozenset()
        winner = self.candidate_winner()
        lineage = self._repair_lineage()
        sources, writers = {}, {}
        for run_id, revision in lineage:
            ancestors, pending = set(), [node.node_id]
            while pending:
                target = pending.pop()
                for edge in revision.edges:
                    if edge.target_node_id == target and edge.source_node_id not in ancestors:
                        ancestors.add(edge.source_node_id)
                        pending.append(edge.source_node_id)
            sources[run_id] = [
                n.node_id for n in revision.nodes
                if n.kind == 'agent' and n.agent_definition_ref in roles
                and (n.node_id in ancestors or n.permissions_ref == 'task_worktree_writer')
                and (not winner or not n.params.get('candidate_id')
                     or str(n.params.get('candidate_id')) == winner)
            ]
            writers[run_id] = [n.node_id for n in revision.nodes
                              if n.kind == 'agent' and n.permissions_ref == 'task_worktree_writer'
                              and (not winner or not n.params.get('candidate_id')
                                   or str(n.params.get('candidate_id')) == winner)]
        git, reviewed = self._reviewed_commits(node)
        if git is not None and reviewed is None:
            return frozenset({'*'})
        kept = self._kept_earlier_writers(writers, git, reviewed, winner)
        if kept is None:
            return frozenset({'*'})
        rows = []
        for row in kept:
            run = self.store.conn.execute(
                'SELECT workflow_id, workflow_revision FROM task_runs WHERE run_id=?',
                (row['run_id'],),
            ).fetchone()
            revision = load_revision(self.store, run['workflow_id'], run['workflow_revision']) if run else None
            source = next((n for n in revision.nodes if n.node_id == row['node_id']), None) if revision else None
            if source is None:
                return frozenset({'*'})
            if source.agent_definition_ref in roles and source.permissions_ref == 'task_worktree_writer':
                rows.append(row)
        for run_id, nodes in sources.items():
            if nodes:
                rows.extend(self.store.conn.execute(
                    'SELECT backend_id, model_id, binding_json, status, start_sha, end_sha'
                    f" FROM attempts WHERE run_id=? AND node_id IN ({','.join('?' * len(nodes))})"
                    ' AND backend_id IS NOT NULL', (run_id, *nodes),
                ).fetchall())
        families = set()
        for row in rows:
            if row['status'] != 'succeeded' and (not row['start_sha'] or row['start_sha'] == row['end_sha']):
                continue
            try:
                document = json.loads(row['binding_json'] or '{}')
                family = document.get('model_family') if isinstance(document, dict) else None
                if not family:
                    family = self.routing.catalog.entry(row['backend_id']).profile(row['model_id']).model_family
            except (ValueError, KeyError, TypeError):
                family = None
            families.add(family if isinstance(family, str) and family.strip() else '*')
        return frozenset(families)

    def _binding_strength_rank(self, row) -> int:
        """Rank the pair that ran. Old migrated bindings have a model-wide grade."""
        import json
        entry = self.routing.catalog.entry(row["backend_id"])
        document = json.loads(row["binding_json"] or "{}")
        if "requested_effort" in document:
            return entry.pair(row["model_id"], document["requested_effort"]).strength_rank
        if entry.pairs_migrated:
            return entry.pairs(row["model_id"])[0].strength_rank
        raise ValueError("the stored binding has no requested pair effort")

    def _escalated_strength(self, node: NodeSpec) -> str | None:
        """One rung above the model that failed, or `None` when there is nothing to read.

        The top rung stays the top: there is no model stronger than the strongest, and a
        floor that could not be met would turn "the best we have already failed" into an
        unroutable node, which is a worse answer than trying the best we have again.
        """
        from maestro.backends import catalog as bk_catalog

        failing = self._failing_node(node)
        if failing is None:
            return None
        binding = self._last_binding(failing.node_id)
        if binding is None:
            return None
        routing = self.routing
        try:
            failed_rank = self._binding_strength_rank(binding)
        except Exception:  # noqa: BLE001 — an unknown route escalates on its own merits
            return None
        rank = min(failed_rank + 1, len(bk_catalog.MODEL_STRENGTHS) - 1)
        return bk_catalog.MODEL_STRENGTHS[rank]

    def _failure_context(self, node: NodeSpec) -> dict | None:
        """Everything the controller knows about the failure `node` is asked to explain.

        The failing node's spec and its latest brief say what was asked; every attempt's
        binding and failure record say what was tried and how each try ended; the latest
        verification evidence says what the checks saw. All of it controller-recorded —
        the diagnoser reads the run's own account, never an agent's account of itself.
        """
        import json

        failing = self._failing_node(node)
        if failing is None:
            return None
        rows = self.store.conn.execute(
            "SELECT attempt_id, status, binding_json, failure_json"
            " FROM attempts WHERE run_id = ? AND node_id = ? ORDER BY rowid",
            (self.run_id, failing.node_id),
        ).fetchall()
        attempts = [
            {
                "attempt_id": row["attempt_id"],
                # `attempts` carries no index column: the try number is the row's place in
                # this node's own history, which is what `ORDER BY rowid` gives.
                "attempt_index": index,
                "status": row["status"],
                "binding": _loads(row["binding_json"]),
                "failure": _loads(row["failure_json"]),
            }
            for index, row in enumerate(rows, start=1)
        ]
        brief = None
        manifest_id = self.store.conn.execute(
            "SELECT manifest_artifact_id FROM attempts WHERE run_id = ? AND node_id = ?"
            " AND manifest_artifact_id IS NOT NULL ORDER BY rowid DESC LIMIT 1",
            (self.run_id, failing.node_id),
        ).fetchone()
        if manifest_id is not None and self.artifacts is not None:
            try:
                brief = json.loads(self.artifacts.read(manifest_id["manifest_artifact_id"]))
            except Exception:  # noqa: BLE001 — an unreadable brief is context, not a crash
                brief = None
        return {
            "failing_node": failing.to_dict(),
            "brief": brief,
            "attempts": attempts,
            "evidence": self._latest_verification_evidence(),
        }

    def _latest_verification_evidence(self) -> list[dict]:
        """The evidence rows of the newest verification attempt of this run.

        What the checks actually saw, as the controller recorded it — the half of the
        story the failing agent's own attempts cannot tell.
        """
        nodes = tuple(
            n.node_id for n in self.revision.nodes if n.handler_ref == VERIFICATION_HANDLER
        )
        if not nodes:
            return []
        row = self.store.conn.execute(
            "SELECT attempt_id FROM attempts WHERE run_id = ?"
            f" AND node_id IN ({','.join('?' * len(nodes))}) ORDER BY rowid DESC LIMIT 1",
            (self.run_id, *nodes),
        ).fetchone()
        if row is None:
            return []
        rows = self.store.conn.execute(
            "SELECT * FROM verification_evidence WHERE attempt_id = ? ORDER BY rowid",
            (row["attempt_id"],),
        ).fetchall()
        evidence = []
        for item in rows:
            entry = {key: item[key] for key in item.keys()}
            # The check's own stdout/stderr, which the row only references: without it the
            # diagnoser would learn that a check failed but never what it printed.
            entry["output"] = [
                self._verification_output(ref) for ref in _loads(entry["output_artifact_refs"]) or ()
            ]
            evidence.append(entry)
        return evidence

    def _verification_output(self, artifact_id: str) -> dict:
        import json

        if self.artifacts is None:
            return {"artifact_id": artifact_id, "unreadable": "no artifact store"}
        try:
            return json.loads(self.artifacts.read(artifact_id))
        except Exception as exc:  # noqa: BLE001 — unreadable output is context, not a crash
            return {"artifact_id": artifact_id, "unreadable": f"{type(exc).__name__}: {exc}"}

    def _attach_failure_context(self, node: NodeSpec, claim: Claim, *, at: str) -> Claim:
        """Store the failure context as an artifact and put it on the claim."""
        import dataclasses
        import json

        from maestro.workflows.failure_context import failure_context_sizes

        document = self._failure_context(node)
        if document is None:
            return claim
        # Full vs delivered characters, durable per diagnosis, so the evidence cap can be
        # re-evaluated against real diagnoses rather than argued about (Dan, 2026-09-18).
        document["sizes"] = failure_context_sizes(document)
        artifact_id = None
        if self.artifacts is not None:
            ref = self.artifacts.put_bytes(
                json.dumps(document, sort_keys=True, default=str).encode("utf-8"),
                type="failure_context",
                producer_attempt_id=claim.attempt_id,
                at=at,
            )
            artifact_id = ref.artifact_id
        self._attempt_inputs.setdefault(node.node_id, {})["failure_context"] = document
        return dataclasses.replace(claim, failure_context_artifact_id=artifact_id)

    def _repair_budget_exhausted(self) -> str | None:
        """Why no repair is left for this workflow, or `None` while one is (P12B F7)."""
        from maestro.workflows import compiler

        routing = self.routing
        counters = compiler.repair_counters(
            self.store, self.revision.workflow_id, run_id=self.run_id,
            max_repairs=routing.max_plan_repairs if routing is not None else None,
        )
        if counters.max_repairs is not None and counters.repairs_used >= counters.max_repairs:
            return (
                f"{counters.repairs_used} of {counters.max_repairs} plan repairs already "
                f"spent on this workflow"
            )
        return None

    def _soft_exclude(self, node: NodeSpec, counters: dict) -> frozenset:
        """P12B F6 retry 2: the route whose output was bad, as a preference to avoid.

        Only the second retry asks for another model. Retry 1 is the same route run fresh,
        because a single bad answer is as often a bad sample as a bad model — and a route
        swapped on the first failure would never let the statistics see the difference.

        Never persisted to `excluded_routes_json`: the preference belongs to this one
        resolution and must not follow the node into F7's repaired run.
        """
        if counters["quality_failures"] != wf_failures.QUALITY_ATTEMPTS - 1:
            return frozenset()
        row = self.store.conn.execute(
            "SELECT backend_id, model_id FROM attempts WHERE run_id = ? AND node_id = ?"
            " AND backend_id IS NOT NULL ORDER BY rowid DESC LIMIT 1",
            (self.run_id, node.node_id),
        ).fetchone()
        if row is None or not (row["backend_id"] and row["model_id"]):
            return frozenset()
        return frozenset({(row["backend_id"], row["model_id"])})

    def _prepare_task_tree(self, node: NodeSpec, *, at: str) -> None:
        """Create the task's worktree for a claimed writer, or reuse the one it left.

        Called after the claim (the writer lease is held) and before the launch, once per
        launched attempt. A node that declares its own `worktree`, and a project that is
        not a git repository, get nothing here: `worktree_for` answers for them.

        The first time in a run for each candidate's tree, what was found there is
        recorded once the tree is handed over (`TaskTreePrepared`): a tree left with
        uncommitted changes holds an earlier run's work that no commit names, and this
        run's writer commits it as its own (D10, `_kept_earlier_writers`).
        """
        import json

        from maestro.workflows import task_tree

        if not self._owns_task_tree(node) or node.permissions_ref != "task_worktree_writer":
            return
        candidate = str(node.params.get("candidate_id") or "")
        tree = task_tree.path_for(self.repo, self.revision.task_id, candidate=candidate)
        recorded = any(
            json.loads(event["payload"]).get("candidate") == candidate
            for event in self.store.conn.execute(
                "SELECT payload FROM events WHERE type = ? AND aggregate_id = ?",
                (TASK_TREE_PREPARED_EVENT, self.run_id),
            )
        )
        kept = tree.exists()
        uncommitted = not recorded and kept and task_tree.uncommitted(tree) != []
        task_tree.ensure(
            self.repo, self.revision.task_id, candidate=candidate,
            base=self.revision.code_base_sha,
        )
        if not recorded:
            self.store.append_event(
                TASK_TREE_PREPARED_EVENT, self.run_id,
                payload={"candidate": candidate, "kept": kept, "uncommitted": uncommitted},
                run_id=self.run_id, recorded_at=at,
            )

    def _owns_task_tree(self, node: NodeSpec) -> bool:
        from maestro.workflows import task_tree

        if node.params.get("worktree") or node.permissions_ref not in TASK_TREE_PERMISSIONS:
            return False
        if node.permissions_ref == "task_worktree_writer" and not node.wants_writer_lease:
            # `async_launch`: a resource label, no worktree lease, no tree (the legacy lane).
            return False
        return task_tree.is_repository(self.repo)

    def _release_task_trees(self, outcome: str, *, at: str) -> None:
        """A settled run's worktrees go; a dirty one stays (`task_tree.release`).

        Only on settle: a failure mid-run keeps its tree for the F6 retry that restores it,
        and a run canceled by a plan repair (W7) never settles, so its successor inherits
        the tree it left.
        """
        from maestro.workflows import task_tree

        candidates = sorted({
            str(node.params.get("candidate_id") or "")
            for node in self.revision.nodes
            if node.permissions_ref == "task_worktree_writer" and self._owns_task_tree(node)
        })
        for candidate in candidates:
            try:
                outcome_text = task_tree.release(
                    self.repo, self.revision.task_id, candidate=candidate,
                    merged=outcome == "succeeded",
                )
            except Exception as exc:  # noqa: BLE001 — a kept tree is not a failed run
                outcome_text = f"release failed: {type(exc).__name__}: {exc}"
            self.store.append_event(
                TASK_TREE_RELEASED_EVENT, self.run_id,
                payload={"candidate": candidate, "outcome": outcome, "detail": outcome_text},
                run_id=self.run_id, recorded_at=at,
            )

    def _commit_boundary(self, node: NodeSpec):
        """This node's `CommitBoundary`, when it owns the worktree the runner hands it.

        `worktree_for` is overridable, so the boundary is only trusted when the path it
        derives is the path the node will actually be run in; anything else is somebody
        else's tree and is left alone.
        """
        from maestro import confinement

        tree = self.worktree_for(node)
        if tree is None:
            return None, None
        try:
            boundary = confinement.CommitBoundary.for_task(
                self.repo, self.revision.task_id, candidate=self._tree_candidate(node),
            )
        except Exception:  # noqa: BLE001 — an unnameable branch has no boundary
            return None, tree
        same = Path(os.path.normpath(str(boundary.worktree))) == Path(
            os.path.normpath(str(tree))
        )
        return (boundary if same else None), tree

    def _restore_start_tree(self, node: NodeSpec, counters: dict) -> str | None:
        """P12B F6: put a writer's retry back where its predecessor started, and say where.

        Returns the SHA to record as this attempt's `start_sha`, or `None` when the node
        writes no git worktree the controller can name.

        Restoring here — at claim time, once per launched retry — rather than at ingest is
        what makes it restart-safe: a controller that dies after the failure and before the
        relaunch has not yet discarded anything, so the retry it eventually runs still
        restores exactly once.

        P12 pilot finding D14: only a *retry* restores. When the writer's previous attempt
        succeeded, the reset came from a verdict on its finished work downstream (a review
        rejection, a failing gate, Dan's reject), so the rework continues from that work.
        """
        if not node.wants_writer_lease:
            return None
        boundary, tree = self._commit_boundary(node)
        if boundary is None or not (Path(tree) / ".git").exists():
            return None
        previous = self.store.conn.execute(
            "SELECT attempt_id, start_sha, status, failure_kind FROM attempts WHERE run_id = ?"
            " AND node_id = ? AND start_sha IS NOT NULL ORDER BY rowid DESC LIMIT 1",
            (self.run_id, node.node_id),
        ).fetchone()
        # P13 W8: `ctl retry` resets no counter, so its attempt is recognised by kind.
        retried = counters["retry_resets"] or (
            previous is not None and previous["failure_kind"] == "operator_retry"
        )
        # D26: a writer that stopped to ask Dan keeps what it committed before asking.
        if retried and previous is not None and previous["status"] != "succeeded" \
                and previous["failure_kind"] != "dan_question":
            boundary.restore(previous["start_sha"])
            # D10: nothing that attempt wrote is left in the tree, committed or not, and
            # its row says so. Nothing else the runner does discards a writer's work
            # while the tree stays.
            self.lifecycle.record_routing_settle(
                previous["attempt_id"], run_id=self.run_id,
                fields={"end_sha": previous["start_sha"]},
            )
        return _git_head(tree)

    def _record_end_sha(self, attempt_id: str, node_id: str, *, at: str) -> None:
        """P13 W8: where a writer's tree stands as its attempt ends, onto the attempt.

        With `start_sha` it names the commits the attempt made. An end equal to the start
        says the attempt left nothing: it committed nothing and left a clean tree, or its
        retry restored the tree (`_restore_start_tree` writes that). `_reviewed_tree_strength`
        does not count such an attempt unless it succeeded. Left `NULL` for a node
        `_restore_start_tree` records no start for, and for a tree left with uncommitted
        changes: whoever commits those commits this attempt's work, so no range of commits
        says what it wrote.
        """
        from maestro.workflows import task_tree

        node = self.revision.node(node_id)
        if not node.wants_writer_lease:
            return
        boundary, tree = self._commit_boundary(node)
        if boundary is None or not (Path(tree) / ".git").exists():
            return
        if task_tree.uncommitted(tree) != []:
            return
        self.lifecycle.record_routing_settle(
            attempt_id, run_id=self.run_id, at=at, fields={"end_sha": _git_head(tree)},
        )

    def _blocked_wait_exceeded(self, counters: dict, resolved, now_epoch: float) -> bool:
        """P12B F5: a node blocked long ago, now waiting on pools with no known reset.

        A known reset is waited for however far away; only an open-ended wait parks.
        """
        from maestro.workflows import routing as wf_routing

        since = counters.get("first_blocked_at")
        if not since or resolved.reason != "pool_paused" or resolved.reset_known is not False:
            return False
        started = wf_routing._zulu_epoch(since)
        if started is None:
            return False
        return now_epoch - started >= self.routing.max_blocked_wait_sec

    def _record_pace_decision(self, node, resolved, manifest, *, at, outcome):
        """Mandatory no-attempt decision evidence in the existing ordered store.

        Recorded before the sole claim. A selected route with a refused budget
        has no attempt; the proposed identity is only an input, never a claim.
        Failure propagates and prevents dispatch, unlike nullable diagnostics.
        """
        decision = resolved.pace_decision
        if decision is None:
            return
        resolution = resolved.to_dict()
        resolution.pop('pace_decision', None)  # the full snapshot is stored once below
        try:
            self.store.append_event('PaceDecisionRecorded', self.run_id, run_id=self.run_id,
                recorded_at=at, payload={
                    'run_id': self.run_id, 'node_id': node.node_id,
                    'task_id': self.revision.task_id, 'workflow_id': self.revision.workflow_id,
                    'workflow_revision': self.revision.revision,
                    'next_attempt_index': self.attempt_index(node.node_id) + 1,
                    'policy_hash': self.routing.policy.policy_hash,
                    'model_policy_revision': self.routing.model_policy_revision,
                    'context': manifest.to_dict(), 'outcome': outcome,
                    'decision': decision.to_dict(),
                    'resolution': resolution,
                })
        except Exception as exc:
            raise PaceJournalUnavailable(str(exc)) from exc

    def _record_routing_decision(
        self, claim: Claim, *, at: str, start_sha: str | None = None
    ) -> None:
        """W3: the binding, the manifest and the start telemetry, onto the claimed attempt.

        Written after the claim and before the launch, so a crash in between still leaves
        a durable account of what the controller had decided to run. Everything the driver
        cannot measure stays `NULL` rather than being estimated (G6).
        """
        import json

        from maestro.workflows import routing as wf_routing

        binding = claim.binding
        if binding is None or self.routing is None:
            return
        shape_key = getattr(binding, "shape_key", None)
        session = claim.session
        binding_document = binding.to_dict()
        from maestro.workflows import runaway as wf_runaway

        band_stamp = wf_runaway.binding_stamp(
            self.routing, self.revision.node(claim.node_id), self.revision.task_id)
        if band_stamp is not None:
            binding_document[wf_runaway.BINDING_KEY] = band_stamp
        self.lifecycle.record_routing_decision(
            claim.attempt_id,
            run_id=self.run_id,
            at=at,
            fields={
                "binding_id": binding.binding_id,
                "binding_json": json.dumps(binding_document, sort_keys=True),
                "backend_id": binding.backend_id,
                "model_id": binding.model_id,
                # The *native* effort, so this column and `ShapeKey.effort` are the same
                # value; the effort that was asked for stays in `binding_json`.
                "effort": binding.effort_native_value or "unsupported",
                "shape_key_json": (
                    None if shape_key is None
                    else json.dumps(shape_key.to_dict(), sort_keys=True)
                ),
                "manifest_artifact_id": claim.manifest_artifact_id,
                "agent_version": wf_routing.agent_version(self.routing, binding),
                "session_id": None if session is None else session.session_id,
                "session_mode": None if session is None else session.mode,
                "usage_start_json": _usage_json(
                    wf_routing.read_usage(self.routing, binding)
                ),
                "stepped_down": 1 if getattr(binding, "stepped_down", False) else None,
                # P12B F6: where this attempt's worktree stood when it was claimed, so a
                # retry can start from the same place instead of inheriting the mess.
                "start_sha": start_sha,
            },
        )

    def _refuse(self, node: NodeSpec, reason: str, *, at: str, report: TickReport) -> None:
        """Claim at the node's default deadline and fail it with `reason` (S-3 block)."""
        try:
            claim = self.claim(node, at=at)
        except DispatchRefused as refusal:
            report.refused[node.node_id] = str(refusal)
            return
        self._fail_claim(node, claim, reason, at)
        report.refused[node.node_id] = reason

    # --- step 1: ingest ------------------------------------------------------------

    def ingest_results(self, *, at: str | None = None) -> list[str]:
        """Read finished workers' envelopes and move their nodes (`§3` step 6).

        Only attempts the store believes are `claimed` or `running` are considered, so an
        envelope re-read after the node already moved is a no-op rather than a second
        transition — the ingest path has to be idempotent, because a crash between reading
        a receipt and committing its effect is ordinary.
        """
        stamp = at or now_iso()
        moved: list[str] = []
        # P12 D19: also an attempt the reconciler already finished (a dead writer with its
        # receipt on disk) while its node is still `claimed`/`running`. Every runner's
        # reconciler scans every lease, so with two runs live the other run's tick can
        # settle this run's attempt first; the node has to move here all the same.
        rows = self.store.conn.execute(
            "SELECT a.* FROM attempts a"
            " LEFT JOIN node_states n ON n.run_id = a.run_id AND n.node_id = a.node_id"
            " WHERE a.run_id = ? AND (a.status IN ('claimed', 'running')"
            "  OR (a.status IN ('succeeded', 'failed')"
            "      AND n.phase IN ('claimed', 'running')"
            "      AND n.last_attempt_id = a.attempt_id))",
            (self.run_id,),
        ).fetchall()
        for row in rows:
            inbox = self.inbox(row["attempt_id"])
            result = inbox.result()
            if result is None:
                self._bind_pid_if_reported(row, inbox, at=stamp)
                continue
            receipt = inbox.receipt()
            if receipt is None:
                # Result without receipt: the worker is still inside its own protocol.
                # Waiting costs one tick; reading it early would race the receipt commit.
                continue
            self._commit_result(row, result, receipt, at=stamp)
            moved.append(row["node_id"])
            if self.successor is not None:
                # The repair canceled this run and settled its cone; the rows read above
                # are stale, so nothing else of the old run is committed.
                break
        return moved

    def _bind_pid_if_reported(self, row, inbox: AttemptInbox, *, at: str) -> None:
        """Step 4: rebind the lease to the PID the worker says it writes from (`[INV-02]`).

        Until this happens the lease holds the launcher's PID, which is a placeholder and
        is treated as one: it is only ever used to keep the resource unallocatable.
        """
        reported = inbox.writer_pid()
        if reported is None or reported == row["writer_pid"]:
            return
        node = self.revision.node(row["node_id"])
        resource_id = self.resource_id_for(node)
        if resource_id is not None:
            lease = self.store.lease(resource_id)
            if lease is not None and lease.owner_attempt_id == row["attempt_id"]:
                self.store.bind_writer_pid(
                    resource_id, lease_epoch=lease.lease_epoch, writer_pid=reported, at=at
                )
        if row["status"] == "claimed":
            self.lifecycle.start_attempt(
                row["attempt_id"], run_id=self.run_id, writer_pid=reported, at=at
            )
            self.lifecycle.set_node_phase(
                self.run_id, row["node_id"], "running",
                attempt_id=row["attempt_id"], at=at,
            )

    def _commit_result(self, row, result: NodeResult, receipt: Mapping, *, at: str) -> None:
        attempt_id = row["attempt_id"]
        node_id = row["node_id"]
        node = self.revision.node(node_id)

        operation_id = str(receipt.get("operation_id") or "")
        if operation_id and self.store.intent(operation_id) is not None:
            self.store.record_receipt(
                operation_id=operation_id,
                attempt_id=attempt_id,
                exit_status=int(receipt.get("exit_status", result.exit_status)),
                payload=dict(receipt.get("payload") or {}),
                at=at,
            )

        asking = None
        if isinstance(result.outputs, Mapping) and result.outputs.get("dan_questions"):
            # D26: screened before the outcome is committed, so a refused round is stored
            # as the failure it becomes, not as a question.
            asking, result = self._screen_questions(node, result)
        elif isinstance(result.outputs, Mapping) and result.outputs.get("deny_list_asks"):
            # D29: the gate found deny-list hits Dan has not ruled on.
            asking, result = self._screen_deny_asks(node, result)
        elif isinstance(result.outputs, Mapping) and result.outputs.get("permission_asks"):
            # D30: an agent asked Dan to allow a command family its call was denied.
            asking, result = self._screen_permission_asks(node, result)
        self._ingest_evidence(result, attempt_id=attempt_id)
        if (node.handler_ref in ('maestro.handlers.post_merge_checks', OPERATOR_CHECK_HANDLER)
                and result.node_phase == 'failed' and result.outputs.get('post_merge_failure')
                and self.merged_snapshot()):
            # Publish before finishing the attempt. A crash anywhere here leaves the
            # same result to reap and the durable CAS intent to recover, never remerge.
            from maestro.workflows.post_merge import publish_fix

            fix_task = publish_fix(self, result)
            result = dataclasses.replace(result, outputs={**result.outputs, 'fix_task': fix_task})
        self._record_shadow_comparison(node, result, attempt_id=attempt_id, at=at)
        self._record_planner_forecast(node, result, attempt_id=attempt_id, at=at)
        outcome_ref = self._commit_outcome_artifact(attempt_id, result)
        self._record_origin_flags(node, result, attempt_id=attempt_id, at=at)
        # W2 observes the screened result; diagnostics cannot interrupt core ingest.
        try:
            from maestro.workflows.telemetry_runtime import observe_result
            observe_result(self, row, node, result, at=at)
        except Exception:
            pass
        if asking is not None and "permission_asks" in asking:
            self._ask_permission(row, node, asking, outcome_ref=outcome_ref, at=at)
            return
        if asking is not None and asking.get("deny_asks"):
            self._ask_deny(row, node, asking, outcome_ref=outcome_ref, at=at)
            return
        if asking is not None:
            self._ask_dan(row, node, result, asking, outcome_ref=outcome_ref, at=at)
            return
        phase = result.node_phase
        repair_plan = None
        if phase == "succeeded" and self.successor is None:
            proposal = self._plan_repair_proposal(node, result)
            refusal = self._verdict_refusal(node, proposal)
            if refusal is not None:
                phase = "failed"
                self._record_repair_refused(node_id, refusal, attempt_id=attempt_id, at=at)
            elif proposal is not None:
                repair_plan, refusal = self._plan_repair(node, proposal)
                if refusal is not None:
                    phase = "failed"
                    self._record_repair_refused(node_id, refusal, attempt_id=attempt_id, at=at)
            try:
                from maestro.workflows.telemetry_runtime import observe_diagnosis
                observe_diagnosis(self, row, node, proposal, repair_plan, refusal, at=at)
            except Exception:
                pass
        record = None
        if self.routing is not None and phase == "uncertain":
            # P13 W8: `uncertain` is not terminal, so a node left there held its run open
            # forever and nothing told Dan. It is decided here instead.
            record = self._decide_undecided_result(node, result, attempt_id=attempt_id, at=at)
            phase = "failed"
        elif self.routing is not None and phase == "failed":
            try:
                record = wf_failures.classify_node_result(result, handler_ref=node.handler_ref)
            except ValueError:
                record = wf_failures.FailureRecord(
                    kind="unknown",
                    evidence=str((result.outputs or {}).get("failure")),
                )
        attempt_status = phase if phase in ("succeeded", "failed", "uncertain") else "running"
        blocked = record is not None and record.is_blocked and node.kind == "agent"
        if phase is not None:
            # Settle telemetry must be durable before deciding whether a CLI's reported
            # reset still applies. A window may roll over while the worker is exiting.
            self._record_settle_telemetry(row, at=at)
        cone = ()
        if record is not None and not blocked and phase == "failed":
            cone = wf_failures.retry_cone(self.revision, node_id, kind=record.kind)
        if blocked:
            # P12B F4: a blocked attempt spends no retry. The node goes back to `ready` in
            # the same transaction that fails the attempt, so no tick sees it `failed`.
            pause = self._requeue_blocked(row, record, outcome_ref=outcome_ref, at=at)
        elif cone:
            # P12B F6: the model worked badly. Under the retry budget the whole cone goes
            # back to `pending` in one transaction (so no `on_failure` edge opens between
            # the failure and the retry); once it is spent, today's `failed` path runs —
            # counted, so the repaired run inherits an exhausted node.
            self._commit_quality_failure(row, record, cone, outcome_ref=outcome_ref, at=at)
        elif phase is not None:
            self.lifecycle.finish_attempt(
                attempt_id, run_id=self.run_id, status=attempt_status,
                outcome_ref=outcome_ref, at=at,
            )
            if self.phases().get(node_id) == "claimed":
                # A worker that finished before the controller ever saw it running still
                # has to pass through `running`: the FSM has no claimed -> succeeded edge,
                # and coercing one would make the state machine advisory.
                self.lifecycle.set_node_phase(
                    self.run_id, node_id, "running", attempt_id=attempt_id, at=at
                )
            self.lifecycle.set_node_phase(
                self.run_id, node_id, phase, attempt_id=attempt_id, at=at
            )

        if phase is not None:
            if self.routing is not None:
                # W8: the attempt is over, so whatever host resources it was admitted
                # with go back before the next tick admits anything against them.
                self.routing.attempt_finished(self, attempt_id)
            if blocked:
                self.routing.on_blocked(self, attempt_id, record, pause)
            elif record is not None and not cone:
                self.lifecycle.record_failure(
                    attempt_id, run_id=self.run_id, record=record, at=at
                )

        resource_id = self.resource_id_for(node)
        if resource_id is not None and phase is not None:
            lease = self.store.lease(resource_id)
            if lease is not None and lease.owner_attempt_id == attempt_id:
                self.store.release_lease(
                    resource_id, lease_epoch=lease.lease_epoch, at=at
                )

        if repair_plan is not None:
            self._apply_repair(node_id, repair_plan, attempt_id=attempt_id, at=at)

    def _record_planner_forecast(self, node, result, *, attempt_id: str, at: str) -> None:
        """P13 W13: keep a planner's forecast for this task's later routing and trigger."""
        from maestro.workflows import runaway as wf_runaway

        outputs = result.outputs if isinstance(result.outputs, Mapping) else {}
        band = wf_runaway.valid_band(outputs.get("complexity_forecast"))
        if (band is None or self.routing is None
                or node.agent_definition_ref != wf_runaway.PLANNER_ROLE
                or wf_runaway.effective_mode(self.routing) == "off"):
            return
        wf_runaway.record_forecast(self.store, self.revision.task_id, band,
                                   run_id=self.run_id, attempt_id=attempt_id, at=at)

    def _decide_undecided_result(
        self, node: NodeSpec, result: NodeResult, *, attempt_id: str, at: str
    ):
        """P13 W8: the failure an `uncertain` (or `handover`) result is settled as.

        A handler raised, in the controller (`_run_inline`) or in a worker, so nobody
        judged the node's work: `controller_error`, which is never retried, and the run
        goes down its failure edge to the notice that tells Dan (A05's escalation). A node
        with a postcondition probe has the probe's answer added to the evidence, so the
        reader knows whether the effect landed before the handler died; either way the
        node is never replayed blind. The result file keeps the handler's own account.

        A `handover` is an agent that stopped mid-work on a `SWITCH`, which the graph
        runner has no lane for: an ordinary bad output, run again.
        """
        detail = str(result.detail or "")
        if result.status == "handover":
            return wf_failures.FailureRecord(
                kind="unknown", evidence=f"agent handed over mid-work (SWITCH): {detail}",
            )
        evidence = detail or "the handler reported nothing"
        probe = _POSTCONDITION_PROBES.get(node.reconciliation_handler or "")
        if probe is not None:
            evidence = f"{evidence}; probe: {_ask(probe, self, node)[1]}"
        outputs = result.outputs if isinstance(result.outputs, Mapping) else {}
        self.store.append_event(
            CONTROLLER_ERROR_EVENT, self.run_id,
            payload={"node_id": node.node_id, "attempt_id": attempt_id,
                     "error": f"controller_error: {evidence}"[:2000],
                     "traceback": str(outputs.get("traceback") or ""), "decision": "failed"},
            run_id=self.run_id, attempt_id=attempt_id, recorded_at=at,
        )
        return wf_failures.FailureRecord(kind="controller_error", evidence=evidence)

    def _commit_quality_failure(self, row, record, cone, *, outcome_ref, at: str) -> None:
        """P12B F6: retry the cone, or spend the last of its budget and fail it.

        The root of the cone owns the counter: for an agent that is the node itself, for a
        gate it is the writer whose work the gate judged. Counting on the root rather than
        on whichever node happened to fail is what makes "a failing gate re-runs the
        implementer and the gate" cost one retry and not two.
        """
        attempt_id, node_id = row["attempt_id"], row["node_id"]
        root = cone[0]
        spent = self.lifecycle.node_counters(self.run_id, root)["quality_failures"] + 1
        if spent >= wf_failures.QUALITY_ATTEMPTS:
            self.lifecycle.fail_quality(
                attempt_id, run_id=self.run_id, node_id=node_id, root=root,
                record=record, outcome_ref=outcome_ref, at=at,
            )
            return
        if self._runaway_stops_retries(root, failing=node_id, kind=record.kind, at=at):
            self.lifecycle.fail_quality(
                attempt_id, run_id=self.run_id, node_id=node_id, root=root,
                record=record, outcome_ref=outcome_ref, at=at,
                spend_to=wf_failures.QUALITY_ATTEMPTS,
            )
            return
        self.lifecycle.requeue_cone(
            attempt_id, run_id=self.run_id, root=root, cone=cone, record=record,
            outcome_ref=outcome_ref, skipped=self._skipped_below(cone), at=at,
        )

    def _runaway_can_diagnose(self, failing: str) -> bool:
        """Would stopping now reach a diagnoser that could act? If not, retries are all
        that is left, so a trigger must not take them away."""
        if self._repair_budget_exhausted() is not None:
            return False
        return any(
            edge.guard_predicate == "on_failure"
            and self.revision.node(edge.target_node_id).agent_definition_ref
            == wf_failures.ROLE_DIAGNOSER
            for edge in self.revision.outgoing(failing)
        )

    def _runaway_stops_retries(self, root: str, *, failing: str, kind: str, at: str) -> bool:
        """P13 W13: record a trigger for the cone root's latest attempt; `on` acts on it.

        The root is the attempt that spent: a failed gate costs nothing itself and re-runs
        the writer it judged. That holds only for a verdict on the writer's work: a gate
        whose host crashed says nothing about what the writer spent. Any error here leaves
        the ordinary ladder running.
        """
        from maestro.workflows import runaway as wf_runaway

        try:
            if self.routing is None:
                return False
            if failing != root and kind != "verification_failed":
                return False
            mode = wf_runaway.effective_mode(self.routing)
            if mode == "off":
                return False
            attempt = self.store.conn.execute(
                "SELECT attempt_id, binding_json, usage_settle_json FROM attempts"
                " WHERE run_id = ? AND node_id = ? ORDER BY rowid DESC LIMIT 1",
                (self.run_id, root),
            ).fetchone()
            if attempt is None or not attempt["binding_json"] or not attempt["usage_settle_json"]:
                return False
            trigger = wf_runaway.assess(
                wf_runaway.stamped_attempts(self.store), attempt=attempt,
                factor=self.routing.runaway_factor,
            )
            if trigger is None:
                return False
            acted = mode == "on" and self._runaway_can_diagnose(failing)
            self.store.append_event(
                wf_runaway.TRIGGER_EVENT, self.run_id,
                payload={**trigger, "mode": mode, "acted": acted, "node_id": root},
                run_id=self.run_id, attempt_id=attempt["attempt_id"], recorded_at=at,
            )
            return acted
        except Exception:  # noqa: BLE001 — detection is advisory; the retry budget still holds
            return False

    def _runaway_trigger(self) -> dict | None:
        """The trigger that stopped this run's retries early, or `None`."""
        from maestro.workflows import runaway as wf_runaway

        rows = self.store.conn.execute(
            "SELECT payload FROM events WHERE type = ? AND run_id = ? ORDER BY sequence DESC",
            (wf_runaway.TRIGGER_EVENT, self.run_id),
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            if payload.get("acted"):
                return payload
        return None

    def _runaway_contract(self) -> str | None:
        from maestro.workflows import runaway as wf_runaway

        trigger = self._runaway_trigger()
        return None if trigger is None else wf_runaway.contract_text(trigger)

    def _skipped_below(self, cone) -> tuple[str, ...]:
        """The `skipped` nodes downstream of a cone, which the retry re-opens.

        A failed writer skips its gate, review, merge and accept in the same tick. Reset
        the cone without these and the run is re-run into a graph whose tail is already
        closed, so `run_outcome` still reads `failed` while the retry is in flight.
        """
        phases = self.phases()
        reachable = set(cone)
        for node_id in self.revision.topological_order():
            if any(
                edge.source_node_id in reachable
                for edge in self.revision.incoming(node_id)
            ):
                reachable.add(node_id)
        return tuple(
            node_id for node_id in self.revision.topological_order()
            if node_id in reachable and node_id not in cone
            and phases.get(node_id) == "skipped"
        )

    def _requeue_blocked(self, row, record, *, outcome_ref, at: str):
        """P12B F4: pause the attempt's pool (or exclude its route) and re-ready the node.

        Returns the stored pause row, or `None` when the failure paused nothing.
        """
        from maestro.workflows import routing as wf_routing

        attempt_id = row["attempt_id"]
        node_id = row["node_id"]
        try:
            binding = json.loads(row["binding_json"] or "{}")
        except ValueError:
            binding = {}
        pool_id = binding.get("usage_pool_id")
        route = (row["backend_id"], row["model_id"])
        now_epoch = time.time()
        counters = self.lifecycle.node_counters(self.run_id, node_id)
        settle_row = self.store.conn.execute(
            "SELECT reset_crossing, usage_settle_json FROM attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        reset_recovered = _quota_reset_recovered(settle_row, at=at)
        pause = wf_failures.pause_for(
            record, now_epoch=now_epoch, prior_blocks=counters["blocked_attempts"],
            auth_pause_sec=self.routing.auth_failure_pause_sec,
            reset_recovered=reset_recovered,
        )
        pause_row = None
        if pause is not None and pool_id:
            until, reset_known = pause
            pause_row = {
                "pool_id": pool_id,
                "paused_until": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(until)),
                "reset_known": 1 if reset_known else 0,
                "reason": f"{record.kind}: {record.evidence}"[:200],
                "failure_kind": record.kind,
                "attempt_id": attempt_id,
                "recorded_at": at,
                # Not a column: the epoch the in-memory pause is applied with.
                "until_epoch": until,
            }
        exclude_route = (
            route if record.kind == "context_overflow" and all(route) else None
        )
        self.lifecycle.requeue_blocked(
            attempt_id, run_id=self.run_id, node_id=node_id, record=record,
            pause_row=pause_row, exclude_route=exclude_route,
            outcome_ref=outcome_ref, at=at,
        )
        if pause_row is not None:
            pool = self.routing.resources.pool(pool_id)
            wf_routing.extend_pause(pool, until=pause_row["until_epoch"], reason=record.kind)
            self.routing.note_pause(pool_id, reset_known=bool(pause_row["reset_known"]))
        return pause_row

    # --- W7: plan repair ------------------------------------------------------------

    def _plan_repair_proposal(self, node: NodeSpec, result: NodeResult):
        """The proposal a planner or diagnoser result carries, or `None`.

        The key is ignored on any other node. A present but malformed proposal comes back
        as the refusal it is, so the caller fails the node exactly as for any other
        rejected proposal.
        """
        from maestro.workflows.compiler import PlanRepairProposal, PlanRepairRejected

        if node.agent_definition_ref not in PLAN_REPAIR_ROLES:
            return None
        outputs = result.outputs if isinstance(result.outputs, Mapping) else {}
        if "plan_repair" not in outputs or outputs["plan_repair"] is None:
            return None
        data = outputs["plan_repair"]
        if node.agent_definition_ref == wf_failures.ROLE_DIAGNOSER and isinstance(data, Mapping):
            data = self._completed_diagnosis(data)
        try:
            return PlanRepairProposal.from_dict(data)
        except PlanRepairRejected as exc:
            return exc

    def _completed_diagnosis(self, data: Mapping) -> dict:
        """A diagnoser's `plan_repair.json` in the full wire form (P12 pilot finding D17).

        An agent is not asked for what the controller already knows: the workflow and the
        revision it drafted against default to this run's. `brief_addenda` —
        `{node_id: text}` — is the compact `poor_agent` repair: each named node is replaced
        by its own spec with `params.brief_addendum` set, which re-runs it and everything
        after it. An unknown node id is passed through so `plan_repair` refuses it by name.
        """
        data = dict(data)
        data.setdefault("workflow_id", self.revision.workflow_id)
        data.setdefault("base_revision", self.revision.revision)
        if data.get("verdict") == "runaway_false_alarm" and not (
            data.get("rerun_nodes") or data.get("replace_nodes") or data.get("brief_addenda")
        ):
            # P13 W13: nothing to change, so the node whose retries were stopped runs again.
            trigger = self._runaway_trigger()
            if trigger is not None:
                data["rerun_nodes"] = [trigger["node_id"]]
        addenda = data.pop("brief_addenda", None)
        if not isinstance(addenda, Mapping):
            return data
        replaced = list(data.get("replace_nodes") or ())
        named = {n.get("node_id") for n in replaced if isinstance(n, Mapping)}
        known = {n.node_id: n for n in self.revision.nodes}
        for node_id, text in addenda.items():
            if node_id in named:
                continue
            node = known.get(node_id)
            if node is None:
                data.setdefault("rerun_nodes", [])
                data["rerun_nodes"] = [*data["rerun_nodes"], node_id]
                continue
            replaced.append(dataclasses.replace(
                node, params={**node.params, "brief_addendum": str(text)},
            ).to_dict())
        data["replace_nodes"] = replaced
        return data

    def _verdict_refusal(self, node: NodeSpec, proposal) -> str | None:
        """Why a diagnoser's answer is not a diagnosis, or `None` (P12B F7).

        A diagnoser that was launched has already cost the escalation; the one thing it
        owes in return is a reading the controller can act on. Silence, a verdict with no
        patch behind it, or a `poor_agent` that leaves the node that failed untouched are
        all the same refusal — the node is written `failed`, its `on_failure` edge opens,
        and the task parks for Dan rather than looping on an answer nobody can apply.

        A planner's W7 proposal carries no verdict and is judged only by `plan_repair`,
        exactly as it was before this existed.
        """
        from maestro.workflows import compiler

        if node.agent_definition_ref != wf_failures.ROLE_DIAGNOSER:
            return None
        if proposal is None:
            return "diagnoser returned no proposal"
        if isinstance(proposal, compiler.PlanRepairRejected):
            return None  # `_plan_repair` refuses it, with its own reason.
        if proposal.verdict is None:
            return "diagnoser proposal carries no verdict"
        failing = self._failing_node(node)
        if proposal.verdict == "poor_agent":
            replaced = {n.node_id for n in proposal.replace_nodes}
            if failing is None or not replaced & self._blamable(failing):
                return (
                    f"poor_agent verdict replaces neither the node that failed nor the "
                    f"writer whose work it judged"
                    f" ({'unknown' if failing is None else failing.node_id})"
                )
        elif proposal.verdict == "spec_gap":
            # D26: a usable spec_gap never gets here (`_screen_questions` asks Dan).
            return "spec_gap verdict carries no usable questions"
        elif proposal.verdict == "runaway_false_alarm" and self._runaway_trigger() is None:
            return "runaway_false_alarm verdict but no runaway trigger stopped this run"
        elif proposal.verdict == "bad_graph" and not (
            proposal.add_nodes or proposal.remove_nodes or proposal.replace_nodes
            or proposal.add_edges or proposal.remove_edges
        ):
            return "bad_graph verdict carries no graph patch"
        return None

    def _blamable(self, failing: NodeSpec) -> set[str]:
        """The failing node, and — when it judges someone else's work (a review, a gate) —
        the writer whose work it judged: the nodes a `poor_agent` verdict may re-brief.

        P12 pilot finding D17: in DuetFlow 02 the node that fails is `proof_review`, but
        its rejections are verdicts on the implementer, whose brief is the one to change.
        """
        cone = wf_failures.retry_cone(
            self.revision, failing.node_id, kind="verification_failed"
        )
        writers = {
            node_id for node_id in cone
            if self.revision.node(node_id).permissions_ref == "task_worktree_writer"
        }
        return {failing.node_id, *writers}

    def repair_contract(self, node: NodeSpec) -> str:
        """What a diagnoser's `plan_repair.json` may name, for its brief (P12 finding D17)."""
        failing = self._failing_node(node)
        lines = [f"Failing node: {'unknown' if failing is None else failing.node_id}"]
        if failing is not None:
            judged = sorted(self._blamable(failing) - {failing.node_id})
            if judged:
                lines.append(f"The writer whose work it judged: {', '.join(judged)}")
        lines.append(
            "CURRENT GRAPH (nodes and edges; use these shapes for a bad_graph patch):\n"
            + json.dumps({
                "nodes": [n.to_dict() for n in self.revision.nodes],
                "edges": [e.to_dict() for e in self.revision.edges],
            }, sort_keys=True)
        )
        return "\n".join(lines)

    def _plan_repair(self, node: NodeSpec, proposal):
        """Decide a proposal against this run: `(RepairPlan, None)` or `(None, reason)`."""
        from maestro.workflows import compiler

        if isinstance(proposal, compiler.PlanRepairRejected):
            return None, str(proposal)
        phases = self.phases() | {node.node_id: "succeeded"}
        routing = self.routing
        try:
            plan = compiler.plan_repair(
                self.revision, proposal, phases,
                counters=compiler.repair_counters(
                    self.store, self.revision.workflow_id, run_id=self.run_id,
                    max_repairs=routing.max_plan_repairs if routing is not None else None,
                ),
            )
        except compiler.PlanRepairRejected as exc:
            return None, str(exc)
        return plan, None

    def _apply_repair(self, node_id: str, plan, *, attempt_id: str, at: str) -> None:
        """Commit an accepted plan and hand the loop to the repaired revision's runner.

        When `apply_repair` itself refuses — a live writer outside the repair cone, or node
        phases that moved since the plan was drawn — the refusal is recorded as
        `WorkflowRepairRefused`. For a **diagnoser** (P12B F7, closing P12A's known limit)
        the node is then moved `succeeded → failed` after the fact, so its
        `on_failure → park` edge opens and the task reaches a decision instead of standing
        on a repair that was never applied. The phase had to be committed first, because
        the durable half cannot decide until the attempt is closed; the override is how
        that ordering is paid for rather than hidden.

        A **planner's** W7 behaviour is unchanged: its node stays `succeeded` and the run
        carries on unrepaired, because a planner's proposal is an optimisation, not the
        run's last resort.
        """
        from maestro.workflows import compiler

        try:
            new_run = compiler.apply_repair(
                self.store, plan, old_run_id=self.run_id, lifecycle=self.lifecycle,
                is_alive=self.is_alive, at=at,
                trigger_attempt_id=attempt_id if self.revision.node(node_id).agent_definition_ref
                == wf_failures.ROLE_DIAGNOSER else None,
            )
        except compiler.PlanRepairRejected as exc:
            self._record_repair_refused(node_id, str(exc), attempt_id=attempt_id, at=at)
            self._fail_refused_diagnoser(node_id, str(exc), attempt_id=attempt_id, at=at)
            return
        self.successor = self.spawn(new_run, plan.revision)

    def _fail_refused_diagnoser(
        self, node_id: str, reason: str, *, attempt_id: str, at: str
    ) -> None:
        """Move a diagnoser whose accepted plan was then refused to `failed` (P12B F7)."""
        node = self.revision.node(node_id)
        if self.routing is None or node.agent_definition_ref != wf_failures.ROLE_DIAGNOSER:
            return
        record = wf_failures.FailureRecord(kind="garbage_output", evidence=reason)
        with self.store.transaction() as conn:
            self.lifecycle.override_phase(
                self.run_id, node_id, "failed",
                reason=f"repair_refused: {reason}", attempt_id=attempt_id, at=at, conn=conn,
            )
            self.lifecycle.record_failure(
                attempt_id, run_id=self.run_id, record=record, at=at, conn=conn
            )

    def _record_repair_refused(
        self, node_id: str, reason: str, *, attempt_id: str, at: str
    ) -> None:
        self.store.append_event(
            WORKFLOW_REPAIR_REFUSED, self.revision.task_id,
            payload={"node_id": node_id, "reason": reason},
            run_id=self.run_id, attempt_id=attempt_id, recorded_at=at,
        )

    def spawn(self, run_id: str, revision: WorkflowRevision) -> "WorkflowRunner":
        """A runner for `run_id` sharing this one's store, repo, seams and routing."""
        return type(self)(
            self.store, revision, run_id=run_id, repo=self.repo, lifecycle=self.lifecycle,
            dispatcher=self.dispatcher, artifacts=self.artifacts, is_alive=self.is_alive,
            worker_argv=self._worker_argv, routing=self.routing,
            dan_channel=self.dan_channel,
        )

    def _record_settle_telemetry(self, row, *, at: str) -> None:
        """W3's settle half: closing usage, wall time, and whether a quota window reset.

        The binding is read back from the attempt row rather than from memory, so an
        attempt claimed by an earlier controller process still settles with telemetry —
        the ingest that closes a node is routinely a different process from the one that
        opened it. Anything unmeasurable stays `NULL` (G6).
        """
        import json

        from maestro.workflows import routing as wf_routing

        if self.routing is None:
            return
        keys = row.keys()
        if "binding_json" not in keys or not row["binding_json"]:
            return
        self._record_end_sha(row["attempt_id"], row["node_id"], at=at)
        binding = wf_routing.binding_view(row["binding_json"])
        settle_usage = wf_routing.read_usage(self.routing, binding)
        # The attempt's own cumulative tokens (per-step quota telemetry), read from its
        # inbox now that the call is over. Stored on the settle sample, where `token_split`
        # was always null before; the start sample keeps null (nothing had run yet).
        tokens = wf_routing.read_attempt_tokens(
            self.routing, binding, self.attempts_root / row["attempt_id"]
        )
        if tokens is not None:
            settle_usage = dict(settle_usage or {"windows": {}, "source": None,
                                                 "error": "no usage sample"})
            settle_usage["token_split"] = tokens
        start_usage = None
        if "usage_start_json" in keys and row["usage_start_json"]:
            try:
                start_usage = json.loads(row["usage_start_json"])
            except (TypeError, ValueError):
                start_usage = None
        self.lifecycle.record_routing_settle(
            row["attempt_id"],
            run_id=self.run_id,
            at=at,
            fields={
                "usage_settle_json": _usage_json(settle_usage),
                "wall_time_sec": _elapsed_sec(
                    self.lifecycle.attempt_claimed_at(row["attempt_id"]), at
                ),
                "reset_crossing": wf_routing.reset_crossing(start_usage, settle_usage),
            },
        )

    def _ingest_evidence(self, result: NodeResult, *, attempt_id: str) -> list[str]:
        """Commit the `VerificationEvidence` rows a worker reported (`[P4]`, `[INV-01]`).

        The worker ran the checks; only the controller may write them down. Rows arrive
        in `outputs["evidence"]` already shaped by `VerificationEvidence.to_row`, and the
        attempt id is overwritten with the attempt the controller believes produced them —
        a worker naming a different attempt is either confused or lying, and neither is a
        reason to store its claim about provenance.

        A row's check output (`outputs["check_output"]`, by evidence id) is stored as a
        `verification_output` artifact and referenced from the row, as the legacy gate's
        `EvidenceRegistry.store_output` does in-process (P12B F7).
        """
        import json

        outputs = result.outputs if isinstance(result.outputs, Mapping) else {}
        rows = outputs.get("evidence")
        if not isinstance(rows, (list, tuple)):
            return []
        check_output = outputs.get("check_output")
        check_output = check_output if isinstance(check_output, Mapping) else {}
        stored: list[str] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            record = dict(row)
            record["attempt_id"] = attempt_id
            printed = check_output.get(record.get("evidence_id"))
            if isinstance(printed, Mapping) and self.artifacts is not None:
                ref = self.artifacts.put_bytes(
                    json.dumps(dict(printed), ensure_ascii=False).encode("utf-8"),
                    type="verification_output",
                    producer_attempt_id=attempt_id,
                    source_snapshot_id=record.get("source_snapshot_id") or None,
                )
                record["output_artifact_refs"] = json.dumps([ref.artifact_id])
            try:
                stored.append(self.store.record_evidence(record))
            except Exception:  # noqa: BLE001 — malformed evidence is discarded, not fatal
                continue
        return stored

    def _record_shadow_comparison(
        self, node: NodeSpec, result: NodeResult, *, attempt_id: str, at: str
    ) -> None:
        """W6: one `ShadowComparison` for a verification result run in shadow mode.

        Controller-only, like evidence ingest: the worker reports the selection it ran and
        its evidence, and only the controller writes the comparison down. Outcomes are
        `passed` for a passing evidence row and the row's outcome otherwise.
        """
        from maestro import validation

        if node.handler_ref != VERIFICATION_HANDLER:
            return
        outputs = result.outputs if isinstance(result.outputs, Mapping) else {}
        document = outputs.get("affected_selection")
        if not isinstance(document, Mapping) or document.get("mode") != "shadow":
            return
        try:
            selection = validation.AffectedSelection.from_dict(document)
        except Exception:  # noqa: BLE001 — a malformed selection is not recorded
            return
        outcomes: dict[str, str] = {}
        rows = outputs.get("evidence")
        for row in rows if isinstance(rows, (list, tuple)) else ():
            if not isinstance(row, Mapping) or not row.get("check_id"):
                continue
            try:
                passed = validation.VerificationEvidence.from_row(row).passed
            except Exception:  # noqa: BLE001 — an unreadable row passes nothing
                passed = False
            outcome = str(row.get("outcome") or "errored")
            outcomes[str(row["check_id"])] = "passed" if passed else (
                outcome if outcome != "passed" else str(row.get("validity") or "errored")
            )
        comparison = validation.ShadowComparison(selection, outcomes)
        self.store.record_shadow_comparison(comparison.to_row(
            run_id=self.run_id, node_id=node.node_id,
            snapshot_id=str(outputs.get("snapshot") or ""),
            attempt_id=attempt_id, recorded_at=at,
        ))

    def _commit_outcome_artifact(self, attempt_id: str, result: NodeResult) -> str | None:
        if self.artifacts is None:
            return None
        ref = self.artifacts.put_bytes(
            result.to_json().encode("utf-8"),
            type="node_result",
            producer_attempt_id=attempt_id,
        )
        return ref.artifact_id

    # --- step 3 (frontier bookkeeping): skips --------------------------------------

    def apply_skips(self, *, at: str | None = None) -> list[str]:
        stamp = at or now_iso()
        skipped = propagate_skips(self.revision, self.phases())
        for node_id in skipped:
            self.lifecycle.set_node_phase(self.run_id, node_id, "skipped", at=stamp)
        return sorted(skipped)

    # --- steps 1-2: pre-validate, claim, intent ------------------------------------

    def pre_validate(self, node: NodeSpec) -> None:
        """Step 1. Every refusal here is a `DispatchRefused`, never a partial launch."""
        from maestro.workflows import handlers

        if not handlers.is_registered(node.handler_ref):
            raise DispatchRefused(
                f"{node.node_id}: no handler registered as {node.handler_ref!r}"
            )
        if node.kind == "agent" and self.routing is not None:
            from maestro import validation

            try:
                task = self.routing.task_spec(self.revision.task_id)
                specs = validation.specs_from_roadmap(task.legacy_fields.get("verifications") or [])
            except Exception as exc:
                raise DispatchRefused(f"{node.node_id}: required verification unreadable: {exc}") from exc
            if not any(spec.required for spec in specs):
                raise DispatchRefused(
                    f"{node.node_id}: no required verification is declared for this task (INV-04)"
                )
            try:
                self.scope_check_policy()
            except ValueError as exc:
                raise DispatchRefused(f"{node.node_id}: invalid scope contract: {exc}") from exc
        index = self.attempt_index(node.node_id)
        policy = node.retry_policy
        if self.routing is not None:
            counters = self.lifecycle.node_counters(self.run_id, node.node_id)
            if node.kind == "agent" or counters["retry_resets"] or wf_failures.retry_cone(
                self.revision, node.node_id, kind="verification_failed"
            ):
                # P12B F6: the ladder's budget is *this run's* resets, not the node's
                # cumulative quality failures. A blocked requeue spends neither, and a
                # repaired run carries the failures but not the resets — which is exactly
                # what lets F7's final run dispatch once and no more. That holds for a
                # check on the ladder as much as for an agent: the gate a repair
                # invalidated re-checks the final run once, where its carried attempt
                # index alone would refuse it forever (P12 pilot finding D17).
                index = counters["retry_resets"]
                policy = RetryPolicy(max_attempts=wf_failures.QUALITY_ATTEMPTS)
            else:
                # A probed-absent re-run (`requeue_probed`) is not an attempt the policy
                # counts, any more than a blocked requeue is on the ladder above.
                index -= counters["blocked_attempts"]
        if not policy.permits(index):
            raise DispatchRefused(
                f"{node.node_id}: retry policy permits {policy.max_attempts} "
                f"attempt(s); {index} already made"
            )
        resource_id = self.resource_id_for(node)
        if resource_id is not None and not resource_is_free(
            self.store, resource_id, is_alive=self.is_alive
        ):
            # `[INV-02]`: this is the refusal that stops a second writer entering a
            # worktree whose previous writer is still alive, expired lease or not.
            raise DispatchRefused(
                f"{node.node_id}: {resource_id} is held by a live writer"
            )

    def claim(
        self, node: NodeSpec, *, at: str | None = None, deadline_sec: int | None = None
    ) -> Claim:
        """Step 2: one transaction — epoch, lease, intent, claim event.

        `claim_lease` opens its own transaction (it has to: the epoch bump and the refusal
        check are inseparable), so the sequence here is claim-lease-then-intent-and-event
        in one further transaction. The ordering is what matters for recovery: the lease
        exists before anything is dispatched, so no launch is ever unaccounted for.
        """
        stamp = at or now_iso()
        self.pre_validate(node)
        index = self.attempt_index(node.node_id)
        attempt_id = make_attempt_id(self.run_id, node.node_id, index + 1)
        operation_id = new_operation_id()
        resource_id = self.resource_id_for(node)
        launcher_pid = os.getpid()
        budget = (
            node.timeout_policy.execution_timeout_sec if deadline_sec is None else deadline_sec
        )

        epoch = 0
        if resource_id is not None:
            try:
                lease = self.store.claim_lease(
                    resource_id,
                    attempt_id,
                    writer_pid=launcher_pid,
                    ttl_seconds=budget,
                    at=stamp,
                    is_alive=self.is_alive,
                )
            except SingleWriterViolation as exc:
                raise DispatchRefused(str(exc)) from None
            epoch = lease.lease_epoch

        deadline = _plus_seconds(stamp, budget)
        with self.store.transaction() as conn:
            self.store.record_intent(
                operation_id,
                attempt_id=attempt_id,
                effect_class=node.effect_class,
                payload={
                    "run_id": self.run_id,
                    "node_id": node.node_id,
                    "task_id": self.revision.task_id,
                    "handler_ref": node.handler_ref,
                    "resource_id": resource_id,
                    "lease_epoch": epoch,
                },
                status="pending",
                at=stamp,
                conn=conn,
            )
        self.lifecycle.claim_attempt(
            attempt_id, run_id=self.run_id, node_id=node.node_id,
            lease_epoch=epoch, deadline_at=deadline, at=stamp,
        )
        self.lifecycle.set_node_phase(
            self.run_id, node.node_id, "ready", attempt_index=index + 1, at=stamp
        )
        self.lifecycle.set_node_phase(
            self.run_id, node.node_id, "claimed",
            attempt_id=attempt_id, attempt_index=index + 1, at=stamp,
        )
        inbox = self.inbox(attempt_id)
        inbox.ensure()
        return Claim(
            attempt_id=attempt_id,
            run_id=self.run_id,
            node_id=node.node_id,
            operation_id=operation_id,
            resource_id=resource_id,
            lease_epoch=epoch,
            attempt_index=index + 1,
            inbox=inbox,
        )

    # --- step 3: dispatch ----------------------------------------------------------

    def execute_inline(self, node: NodeSpec, claim: Claim, *, at: str | None = None) -> Dispatched:
        """Step 3 for a node only the controller may perform (`[P4]`).

        `merge` writes the target branch (`[INV-05]`) and `accept` writes the acceptance
        transaction (`[INV-01]`); neither may happen in a worker, so neither is dispatched
        into one. The node runs here, in the controller process, and then writes the same
        three files a worker would — envelope, result, receipt — so the ingest path,
        the reconciler and the event stream see one shape of completion rather than two.

        A controller-executed node must therefore be *short*. Everything expensive stays
        in a worker: the merge node's cost is git plumbing and the checks it runs on the
        staging tree, which is the one place the controller has to be the actor.
        """
        stamp = at or now_iso()
        self.store.set_intent_status(claim.operation_id, "dispatched")
        inbox = claim.inbox
        inbox.bind_pid(os.getpid(), node_id=node.node_id, run_id=self.run_id)
        inbox.heartbeat(stamp)
        self._run_inline(node, claim.attempt_id, claim.operation_id, inbox)
        return Dispatched(window=f"inline:{node.node_id}", argv=(), launched=True)

    def _run_inline(self, node: NodeSpec, attempt_id: str, operation_id: str, inbox) -> NodeResult:
        """Run a controller node's handler in process and write its result and receipt."""
        from maestro.workflows import handlers as handler_module

        context = handler_module.HandlerContext(
            run_id=self.run_id,
            task_id=self.revision.task_id,
            node=node,
            attempt_id=attempt_id,
            inbox=inbox,
            workspace=inbox.ensure(),
            worktree=self.worktree_for(node),
            inputs=self.node_inputs(node),
            params=node.params,
            controller=self,
        )
        try:
            result = handler_module.resolve(node.handler_ref)(context)
        except Exception as exc:  # noqa: BLE001 — same rule as the worker's
            import traceback

            result = NodeResult(
                run_id=self.run_id, node_id=node.node_id, attempt_id=attempt_id,
                status="uncertain", detail=f"{type(exc).__name__}: {exc}"[:2000],
                exit_status=1,
                outputs={"traceback": "".join(
                    traceback.format_exception(exc))[-_TRACEBACK_TAIL_CHARS:]},
            )
        inbox.write_result(result)
        inbox.write_receipt(
            operation_id=operation_id, exit_status=result.exit_status,
            payload={"status": result.status, "node_id": node.node_id},
        )
        return result

    def _repoll_operator_nodes(self, *, at: str) -> None:
        """Re-ask a paused operator or wait step whether it is resolved.

        `dan_confirm` and a manual `human_action` wait on Dan's answer; an async
        `await_job` on its awaited artifact; a resumable `cooldown` decides once (G7).
        `park` is excluded: it is the failure sink, and only the operator reopens it.

        A paused result moves no node (`RESULT_DISPOSITION`), so without this the step
        would wait on its first answer forever. The handler is re-run in place on the same
        attempt and operation — its request is idempotent per snapshot — and the ingest
        that follows commits whatever it now reports. Durable across a restart: the paused
        attempt, its inbox and Dan's answer files all outlive the controller process.
        """
        for node in self.revision.nodes:
            if node.handler_ref not in REPOLLED_HANDLERS or node.node_id == PARK_NODE_ID:
                continue
            row = self.store.conn.execute(
                "SELECT n.phase, a.attempt_id, a.status FROM node_states n"
                " JOIN attempts a ON a.attempt_id = n.last_attempt_id"
                " WHERE n.run_id = ? AND n.node_id = ?",
                (self.run_id, node.node_id),
            ).fetchone()
            if row is None or row["phase"] not in ("claimed", "running"):
                continue
            if row["status"] not in ("claimed", "running"):
                continue
            inbox = self.inbox(row["attempt_id"])
            result, receipt = inbox.result(), inbox.receipt()
            if result is None or receipt is None:
                # P13 W8: these steps run only here, synchronously, so an envelope left
                # unfinished means the controller that was writing it died. Asking is
                # idempotent per attempt, so the step is resumed on the same attempt and
                # reads whatever Dan (or the job) answered meanwhile, never asked twice.
                intents = self.store.intents_for_attempt(row["attempt_id"])
                operation_id = str(intents[-1]["operation_id"]) if intents else ""
            elif result.status != "paused":
                continue
            else:
                operation_id = str(receipt.get("operation_id") or "")
            self._run_inline(node, row["attempt_id"], operation_id, inbox)

    # --- `kind: dan`: the snapshot Dan is asked about, and what he said -------------

    def verified_snapshot(self) -> str:
        """The snapshot this run's gate last passed — the tree Dan is asked to confirm."""
        for node in reversed(self.revision.nodes):
            if node.handler_ref == VERIFICATION_HANDLER:
                snapshot = self._committed_outputs(node.node_id).get("snapshot")
                if snapshot:
                    return str(snapshot)
        return ""

    def gate_red_on_base(self) -> list[str]:
        """GDT-9 review M4: the criteria this run's last committed gate noted
        `goal_protected_red_on_base` (RED on the task's tree and on the base), which the
        reviewer reads as no regression of the task, like the gate."""
        from maestro.workflows import goal_guard

        for node in reversed(self.revision.nodes):
            if node.handler_ref == VERIFICATION_HANDLER:
                outputs = self._committed_outputs(node.node_id)
                if outputs:
                    return goal_guard.red_on_base(outputs.get("goals_guard_notes"))
        return []

    def task_tree_path(self) -> Path | None:
        """The task tree the gate checked (the join's winner, when widened)."""
        for node in self.revision.nodes:
            if node.handler_ref == VERIFICATION_HANDLER:
                return self.worktree_for(node)
        return None

    def _upstream(self, node_id: str) -> list[NodeSpec]:
        return [
            self.revision.node(edge.source_node_id)
            for edge in self.revision.incoming(node_id)
            if edge.guard_predicate != "on_failure"
        ]

    def manual_step(self, node: NodeSpec) -> dict:
        """What Dan is asked to do at a manual lane's `human_action` (G7).

        First found: the prep agent's committed `result.json` (`dan_action`,
        `post_action_cmd`), the task's ROADMAP fields, then the node's own `action` param.
        """
        prep: Mapping = {}
        for source in self._upstream(node.node_id):
            if source.permissions_ref == "task_worktree_writer":
                result = self._committed_outputs(source.node_id).get("result")
                if isinstance(result, Mapping):
                    prep = result
        task = _roadmap_task(self.revision.task_id)
        action = (
            str(prep.get("dan_action") or "").strip()
            or str(task.get("dan_action") or "").strip()
            or str(node.params.get("action") or "")
        )
        post = str(prep.get("post_action_cmd") or task.get("post_action_cmd") or "").strip()
        if post.lower() in ("null", "none"):
            post = ""
        contract = {key: str(prep.get(key) or task.get(key) or '').strip()
                    for key in ('step_command', 'step_check_cmd', 'expected_result')}
        # Legacy shell actions must go through exactly the same capture path as
        # explicit step_command. URL/prose actions require an explicit external kind.
        external = action.startswith(('http://', 'https://')) or prep.get('step_kind') == 'external' or task.get('step_kind') == 'external'
        if not contract['step_command'] and not external:
            contract['step_command'] = action
        return {
            "dan_action": action, "post_action_cmd": post,
            **contract,
            "title": str(task.get("title") or task.get("short_desc") or ""),
        }

    def manual_contract_for(self, node: NodeSpec) -> dict:
        """Only a controller-committed human receipt can supply capture paths to a gate."""
        if node.handler_ref == VERIFICATION_HANDLER:
            for source in self._upstream(node.node_id):
                if source.handler_ref == HUMAN_HANDLER:
                    return dict(self._committed_outputs(source.node_id).get('receipt', {}).get('manual_contract') or {})
        return {}

    def post_action_cmd_for(self, node: NodeSpec) -> str:
        """The command a gate right after a completed human step runs first (G7), or `""`.

        Legacy `_finalize_manual_action` ran the prep's `post_action_cmd` after Dan acted and
        before the gate; here the gate runs it in the task tree, so a failure is the gate's.
        """
        if node.handler_ref != VERIFICATION_HANDLER:
            return ""
        for source in self._upstream(node.node_id):
            if source.handler_ref == HUMAN_HANDLER:
                receipt = self._committed_outputs(source.node_id).get("receipt")
                if isinstance(receipt, Mapping):
                    return str(receipt.get("post_action_cmd") or "")
        return ""

    def gate_failure_text(self) -> str:
        """What this run's gate last reported: its detail and every check's output (G7 1c,
        where a resumable task's `PROGRESS=n/m` is read from). `""` when there is none."""
        for node in reversed(self.revision.nodes):
            if node.handler_ref != VERIFICATION_HANDLER:
                continue
            result = self._committed_result(node.node_id)
            if not result:
                continue
            outputs = result.get("outputs") if isinstance(result.get("outputs"), Mapping) else {}
            # What the checks printed first: the detail also quotes each check's expected
            # value, and an expected `PROGRESS=10/10` must not read as progress made.
            parts = []
            check_output = outputs.get("check_output")
            printed = [v for _, v in sorted(check_output.items())] \
                if isinstance(check_output, Mapping) else []
            parts += [str(v.get("stdout") or "") for v in printed if isinstance(v, Mapping)]
            parts += [str(v.get("stderr") or "") for v in printed if isinstance(v, Mapping)]
            parts.append(str(result.get("detail") or ""))
            return "\n".join(part for part in parts if part)
        return ""

    def has_manual_step(self) -> bool:
        return any(n.handler_ref == HUMAN_HANDLER for n in self.revision.nodes)

    def dan_dispositions(self) -> list[dict]:
        """Dan's approvals for this run, only while they name the snapshot the gate passed.

        Read from the committed outcome of the `dan_confirm` node — durable in the control
        store's artifact registry — never from memory. An approval of any other tree
        discharges nothing, so it is not returned.
        """
        result = []
        for node in self.revision.nodes:
            if node.handler_ref not in (DAN_CONFIRM_HANDLER, OPERATOR_CHECK_HANDLER):
                continue
            if self.phases().get(node.node_id) != 'succeeded':
                continue
            outputs = self._committed_outputs(node.node_id)
            snapshot = self.operator_subject()[0] if node.handler_ref == OPERATOR_CHECK_HANDLER else self.verified_snapshot()
            if snapshot and outputs.get('verdict') == 'approved':
                result.extend(dict(d) for d in outputs.get('dispositions') or ()
                              if isinstance(d, Mapping) and d.get('subject_sha') == snapshot
                              and d.get('verdict') == 'approved')
        return result

    def merged_snapshot(self) -> str:
        return str(self._committed_outputs('merge').get('merged_commit') or '')

    def operator_subject(self) -> tuple[str, Path | None]:
        """Observe the integrated checkout, or the gate's result in a no-merge lane."""
        if any(n.handler_ref == MERGE_HANDLER for n in self.revision.nodes):
            return self.merged_snapshot(), self.repo
        return self.verified_snapshot(), self.task_tree_path() or self.repo

    def rework_note(self) -> str:
        """Why the previous attempt in this run was rejected, for the implementer's rework
        brief: Dan's latest reason, and each reviewer's latest findings (P12 pilot finding
        D6) — a rejecting review re-runs the implementer, which must know what to fix."""
        notes = []
        decided = None
        for node in self.revision.nodes:
            outputs = self._committed_outputs(node.node_id)
            if outputs.get("dan_questions"):
                decided = self._decision_answers() if decided is None else decided
                note = self._question_note(node, outputs, decided)
                if note:
                    notes.append(note)
                continue
            if node.handler_ref == HUMAN_HANDLER:
                if outputs.get("answer") == "redo" and outputs.get("reason"):
                    notes.append(
                        f"Dan asked for the prep to be redone before he performs the manual "
                        f"step: {outputs['reason']}. Rework the prep so it addresses this."
                    )
                continue
            if node.handler_ref == DAN_CONFIRM_HANDLER:
                if outputs.get("verdict") == "rejected" and outputs.get("reason"):
                    notes.append(
                        f"Dan rejected the previous attempt (snapshot "
                        f"{str(outputs.get('subject_sha') or '')[:12]}) when he verified it "
                        f"by hand: {outputs['reason']}. Rework the task so it addresses this."
                    )
                continue
            if node.handler_ref == VERIFICATION_HANDLER:
                if outputs.get("scope_findings"):
                    notes.append("The gate rejected the previous commit; rework these blocking findings:\n"
                                 + "\n".join(f"- {f}" for f in outputs["scope_findings"]))
                if outputs.get("goals_guard_findings"):
                    notes.append("The gate's goals guard rejected the previous commit; rework "
                                 "these blocking findings:\n"
                                 + "\n".join(f"- {f}" for f in outputs["goals_guard_findings"]))
                if outputs.get("deny_list_disapproved"):
                    from maestro.workflows import deny_list

                    findings = "\n".join(
                        f"- {deny_list.finding(hit)}"
                        for hit in outputs["deny_list_disapproved"] if isinstance(hit, Mapping))
                    notes.append(
                        "Dan disapproved deny-list hit(s) in the previous attempt. Each is a "
                        f"blocking finding; rework the task so it fixes every one:\n{findings}"
                    )
                if outputs.get("decision") == "rejected":
                    gate_note = _gate_rework_note(node.node_id, outputs, self.revision.task_id)
                    if gate_note:
                        notes.append(gate_note)
                continue
            if node.agent_definition_ref != wf_failures.ROLE_REVIEWER:
                continue
            review = outputs.get("review")
            failure = outputs.get("failure")
            if isinstance(review, Mapping) and isinstance(failure, Mapping) \
                    and failure.get("kind") == "verification_failed":
                findings = "\n".join(f"- {f}" for f in review.get("findings") or ())
                notes.append(
                    f"The reviewer ({node.node_id}) rejected the previous attempt "
                    f"({review.get('verdict')}). Rework the task so it fixes every finding:"
                    f"\n{findings}\n"
                    "For each finding, fix the class of defect it names, not only its "
                    "example: find every place the same rule applies and fix it there too. "
                    "Add a test per finding that checks the general property, not just the "
                    "reviewer's example. Then check that each fix does not break adjacent "
                    "behaviour — callers, wiring and error paths next to what you changed."
                )
        return "\n\n".join(notes)

    # --- D26: agents ask Dan when the spec is silent --------------------------------

    def _question_cone(self, node: NodeSpec) -> tuple[tuple[str, ...], str]:
        """`(cone, refusal)`: the nodes that wait on Dan's answer and re-run after it.

        The asker's own node for a writer (or any role answering for its own output); for a
        reviewer the writer through the review, as a rejection would; for a diagnoser the
        cone of the node whose failure it was diagnosing, which re-runs with the decision.
        """
        if node.agent_definition_ref == wf_failures.ROLE_DIAGNOSER:
            failing = self._failing_node(node)
            if failing is None:
                return (), "a diagnoser's spec_gap names no failed node to re-run"
            cone = wf_failures.retry_cone(
                self.revision, failing.node_id, kind="verification_failed")
            return cone or (failing.node_id,), ""
        # A gate asking Dan (GDT-7's goal conflicts) is a verdict on the writer's work too.
        kind = "verification_failed" \
            if node.agent_definition_ref == wf_failures.ROLE_REVIEWER \
            or node.handler_ref in wf_failures.VERIFICATION_HANDLERS else ""
        cone = wf_failures.retry_cone(self.revision, node.node_id, kind=kind)
        return cone or (node.node_id,), ""

    def _screen_questions(self, node: NodeSpec, result: NodeResult):
        """`(asking, result)`: the round to ask Dan, or `None` and the result it becomes.

        A question already decided is dropped; a round of nothing but re-asks, a round past
        `MAX_ROUNDS_PER_WORKFLOW`, or one with no channel to ask through is refused as
        `garbage_output`, which spends budget like any unusable answer.
        """
        from maestro.hitl import verify
        from maestro.workflows import dan_questions

        questions = list(result.outputs.get("dan_questions") or ())
        cone, refusal = self._question_cone(node)
        decided = {dan_questions.normalise(d.get("question")) for d in self.dan_decisions()}
        fresh = [q for q in questions if dan_questions.normalise(q["question"]) not in decided]
        rounds = {
            r.get("round") for r in verify.question_records(task_id=self.revision.task_id)
            if r.get("workflow_id") == self.revision.workflow_id
        }
        if refusal:
            pass
        elif self.dan_channel is None or not hasattr(self.dan_channel, "poll_question"):
            refusal = "asked Dan, but this run has no operator channel to ask through"
        elif not fresh:
            refusal = "re-asked an answered question: " + questions[0]["question"]
        elif len(rounds) >= dan_questions.MAX_ROUNDS_PER_WORKFLOW:
            refusal = (f"asked Dan a question round beyond the cap of "
                       f"{dan_questions.MAX_ROUNDS_PER_WORKFLOW} per workflow")
        if not refusal:
            return {"questions": fresh, "cone": cone}, result
        evidence = f"{refusal} (DAN'S DECISIONS in the brief settle what was decided)"
        outputs = {k: v for k, v in result.outputs.items() if k != "dan_questions"}
        outputs["dan_questions_refused"] = questions
        outputs["failure"] = {"kind": "garbage_output", "evidence": evidence[:4000]}
        return None, dataclasses.replace(
            result, status="failed", exit_status=1, detail=evidence[:2000], outputs=outputs)

    def _ask_dan(self, row, node: NodeSpec, result, asking, *, outcome_ref, at: str) -> None:
        """Pause `node`'s cone on Dan's answer: nothing runs, and no budget is spent.

        The attempt fails as `dan_question` and the cone goes back to `pending` in one
        transaction (`Lifecycle.requeue_for_question`); the question records on disk hold
        the cone's root (`_awaiting_dan`) until every question is answered, across a
        controller restart. Each question goes to Telegram at once.
        """
        from maestro.hitl import verify

        attempt_id, node_id = row["attempt_id"], row["node_id"]
        questions, cone = asking["questions"], asking["cone"]
        record = wf_failures.FailureRecord(
            kind="dan_question",
            evidence=("asked Dan: " + " | ".join(q["question"] for q in questions))[
                :wf_failures.EVIDENCE_LIMIT],
        )
        self.lifecycle.requeue_for_question(
            attempt_id, run_id=self.run_id, node_id=node_id, cone=cone, record=record,
            outcome_ref=outcome_ref, skipped=self._skipped_below(cone), at=at,
        )
        task_id = self.revision.task_id
        asked_by = (f"{node.agent_definition_ref or node.kind}/{node_id}"
                    f"#{self.attempt_index(node_id)}")
        title = str(_roadmap_task(task_id).get("title") or "")
        ids = []
        for n, question in enumerate(questions):
            req_id = verify.question_request_id(task_id, self.run_id, attempt_id, n)
            ids.append(req_id)
            self.dan_channel.poll_question({
                "id": req_id, "task_id": task_id, "run_id": self.run_id,
                "workflow_id": self.revision.workflow_id, "attempt_id": attempt_id,
                "node_id": node_id, "hold": cone[0], "asked_by": asked_by,
                "round": attempt_id, "n": n, "title": title, "spec": question,
            })
        self.dan_channel.journal(
            "dan_question_asked",
            f"{task_id} {asked_by} run={self.run_id} holds={cone[0]}: {', '.join(ids)}",
        )
        self.store.append_event(
            DAN_QUESTION_ASKED_EVENT, task_id,
            payload={"node_id": node_id, "attempt_id": attempt_id, "asked_by": asked_by,
                     "holds": cone[0], "request_ids": ids,
                     "questions": [q["question"] for q in questions]},
            run_id=self.run_id, attempt_id=attempt_id, recorded_at=at,
        )
        self._release_held_attempt(row, node, at=at)

    def _awaiting_dan(self, node_id: str) -> list[str]:
        """The ids of this run's unanswered questions (and D29 deny-list hits, D30
        permission requests) that hold `node_id`."""
        from maestro.hitl import verify

        records = [*verify.question_records(run_id=self.run_id),
                   *verify.deny_records(run_id=self.run_id),
                   *verify.permission_records(run_id=self.run_id)]
        return [
            str(r["id"]) for r in records
            if r.get("hold") == node_id and not r.get("recorded")
        ]

    # --- D29: a deny-list hit asks Dan, with an agent's recommendation ---------------

    def _deny_channel(self):
        channel = self.dan_channel
        return channel if channel is not None and hasattr(channel, "poll_deny") else None

    def _merge_target(self) -> str | None:
        """The branch this run's merge lands on, or `None` for a lane with no merge."""
        for node in self.revision.nodes:
            if node.handler_ref == MERGE_HANDLER:
                return str(node.params.get("target_branch") or "main")
        return None

    def deny_rulings(self) -> dict[str, dict]:
        """Dan's rulings on this workflow's deny-list hits, by hit key.

        Bound to the task and its workflow: a repaired run keeps them, a fresh run of the
        task (a new workflow) asks again, and a changed hit has a new key.
        """
        from maestro.hitl import verify

        out: dict[str, dict] = {}
        for record in verify.deny_records(
            task_id=self.revision.task_id, workflow_id=self.revision.workflow_id,
        ):
            ruling = record.get("ruling")
            key = str((record.get("hit") or {}).get("key") or "")
            if record.get("recorded") and key and ruling in ("approved", "disapproved"):
                out[key] = {
                    "ruling": ruling, "note": str(record.get("note") or ""),
                    "what_to_change": str(
                        (record.get("recommendation") or {}).get("what_to_change") or ""),
                    "request_id": str(record.get("id")), "by": str(record.get("by") or ""),
                }
        return out

    def deny_approved_keys(self) -> list[str]:
        return sorted(k for k, r in self.deny_rulings().items() if r["ruling"] == "approved")

    def _screen_deny_asks(self, node: NodeSpec, result: NodeResult):
        """`(asking, result)`: the hits to put to Dan, or `None` and a refusal.

        A gate that asks with no channel to ask through (the input is only handed over
        when there is one, so this is a race with a channel going away) fails as the
        verification failure it would be at merge.
        """
        asks = [a for a in result.outputs.get("deny_list_asks") or ()
                if isinstance(a, Mapping) and isinstance(a.get("hit"), Mapping)]
        if self._deny_channel() is not None and asks:
            return {"deny_asks": asks}, result
        outputs = {k: v for k, v in result.outputs.items() if k != "deny_list_asks"}
        evidence = "deny_list: no operator channel to ask Dan about " + "; ".join(
            str(a["hit"].get("file")) for a in asks)
        outputs["failure"] = {"kind": "verification_failed", "evidence": evidence[:4000]}
        return None, dataclasses.replace(
            result, status="failed", exit_status=1, detail=evidence[:2000], outputs=outputs)

    def _ask_deny(self, row, node: NodeSpec, asking, *, outcome_ref, at: str) -> None:
        """Hold the gate on Dan's rulings: one Telegram request per hit, nothing spent.

        The same hold as a spec question (`Lifecycle.requeue_for_question` on the gate
        alone): the gate re-runs once every hit is ruled on, and then passes the approved
        hits or sends a disapproved one back to the implementer.
        """
        from maestro.hitl import verify
        from maestro.workflows import deny_list

        attempt_id, node_id = row["attempt_id"], row["node_id"]
        asks = asking["deny_asks"]
        record = wf_failures.FailureRecord(
            kind="dan_question",
            evidence=("deny-list hit(s) put to Dan: " + deny_list.summary(
                [a["hit"] for a in asks]))[:wf_failures.EVIDENCE_LIMIT],
        )
        cone = (node_id,)
        self.lifecycle.requeue_for_question(
            attempt_id, run_id=self.run_id, node_id=node_id, cone=cone, record=record,
            outcome_ref=outcome_ref, skipped=self._skipped_below(cone), at=at,
        )
        task_id = self.revision.task_id
        title = str(_roadmap_task(task_id).get("title") or "")
        channel = self._deny_channel()
        ids = []
        for ask in asks:
            hit = dict(ask["hit"])
            req_id = verify.deny_request_id(task_id, self.revision.workflow_id, hit["key"])
            ids.append(req_id)
            channel.poll_deny({
                "id": req_id, "task_id": task_id, "run_id": self.run_id,
                "workflow_id": self.revision.workflow_id, "attempt_id": attempt_id,
                "node_id": node_id, "hold": node_id, "title": title, "hit": hit,
                "recommendation": dict(ask.get("recommendation") or {}),
            })
        channel.journal(
            "deny_list_hit_asked",
            f"{task_id} run={self.run_id} holds={node_id}: "
            + "; ".join(f"{i} {a['hit'].get('file')} "
                        f"rec={(a.get('recommendation') or {}).get('recommendation') or 'none'}"
                        for i, a in zip(ids, asks)),
        )
        self.store.append_event(
            DENY_LIST_HIT_ASKED_EVENT, task_id,
            payload={"node_id": node_id, "attempt_id": attempt_id, "request_ids": ids,
                     "hits": [dict(a["hit"]) for a in asks],
                     "recommendations": [dict(a.get("recommendation") or {}) for a in asks]},
            run_id=self.run_id, attempt_id=attempt_id, recorded_at=at,
        )
        self._release_held_attempt(row, node, at=at)

    def _poll_deny_rulings(self, *, at: str) -> None:
        """Record each ruling Dan gave since the last tick; the gate re-runs with it."""
        channel = self._deny_channel()
        if channel is None:
            return
        from maestro.hitl import verify

        for record in verify.deny_records(run_id=self.run_id):
            if record.get("recorded"):
                continue
            answer = channel.poll_deny(record)
            if answer is None:
                continue
            ruling, note = verify.interpret_deny(answer)
            hit = record.get("hit") or {}
            channel.mark_question_recorded(
                str(record["id"]), ruling=ruling, note=note, by="dan", ruled_at=at,
            )
            channel.journal(
                f"deny_list_hit_{ruling}",
                f"{self.revision.task_id} {record.get('id')} by=dan key={hit.get('key')} "
                f"{hit.get('file')}: {str(hit.get('match'))[:80]!r}"
                + (f" note={note[:120]}" if note else ""),
            )
            self.store.append_event(
                DENY_LIST_HIT_RULED_EVENT, self.revision.task_id,
                payload={"request_id": record.get("id"), "ruling": ruling, "by": "dan",
                         "note": note, "hit": dict(hit),
                         "workflow_id": self.revision.workflow_id},
                run_id=self.run_id, recorded_at=at,
            )

    def _release_held_attempt(self, row, node: NodeSpec, *, at: str) -> None:
        """Settle an attempt whose node now waits on Dan: telemetry, capacity, lease."""
        attempt_id = row["attempt_id"]
        self._record_settle_telemetry(row, at=at)
        if self.routing is not None:
            self.routing.attempt_finished(self, attempt_id)
        resource_id = self.resource_id_for(node)
        if resource_id is not None:
            lease = self.store.lease(resource_id)
            if lease is not None and lease.owner_attempt_id == attempt_id:
                self.store.release_lease(resource_id, lease_epoch=lease.lease_epoch, at=at)

    # --- D30: an agent asks Dan to allow a command family -----------------------------

    def _permission_channel(self):
        channel = self.dan_channel
        return channel if channel is not None and hasattr(channel, "poll_permission") else None

    def _permission_records(self) -> list[dict]:
        from maestro.hitl import verify

        return verify.permission_records(
            task_id=self.revision.task_id, workflow_id=self.revision.workflow_id)

    def permission_rulings(self) -> list[dict]:
        """This workflow's ruled permission requests, oldest first, for every agent's brief.

        Bound to the task and its workflow, like D29's rulings: a repaired run keeps them.
        """
        return [
            {"family": str((r.get("request") or {}).get("family") or ""),
             "denied": str((r.get("request") or {}).get("denied") or ""),
             "ruling": str(r.get("ruling") or ""), "note": str(r.get("note") or "")}
            for r in self._permission_records() if r.get("recorded")
        ]

    def _screen_permission_asks(self, node: NodeSpec, result: NodeResult):
        """`(asking, result)`: the round's requests, each screened, or `None` and a refusal.

        A round past `MAX_ROUNDS_PER_WORKFLOW`, one that re-asks a family declined or
        refused in this workflow, or one with no channel to ask through is refused as
        `garbage_output`, which spends budget like any unusable answer. Otherwise each
        request is screened (`permissions.screen`): a refused one is recorded and never
        sent, an already-allowed one needs no ask, and the rest go to Dan.
        """
        from maestro import permissions
        from maestro.workflows import permission_asks as asks_mod

        requests = [r for r in result.outputs.get("permission_asks") or ()
                    if isinstance(r, Mapping)]
        prior = self._permission_records()
        rounds = {r.get("attempt_id") for r in prior}
        closed = {str((r.get("request") or {}).get("family") or "") for r in prior
                  if r.get("ruling") in (asks_mod.DECLINED, asks_mod.REFUSED)}
        again = [r["family"] for r in requests if r.get("family") in closed]
        refusal = ""
        if self._permission_channel() is None:
            refusal = ("asked Dan to allow a command family, but this run has no operator "
                       "channel to ask through")
        elif len(rounds) >= asks_mod.MAX_ROUNDS_PER_WORKFLOW:
            refusal = (f"asked Dan for permissions beyond the cap of "
                       f"{asks_mod.MAX_ROUNDS_PER_WORKFLOW} rounds per workflow")
        elif again:
            refusal = (f"re-asked {again[0]}, which was declined or refused in this task "
                       "(PERMISSION RULINGS in the brief)")
        if not refusal:
            screened = [{"request": dict(r), **permissions.screen(r["family"], r["denied"])}
                        for r in requests]
            return {"permission_asks": screened}, result
        outputs = {k: v for k, v in result.outputs.items() if k != "permission_asks"}
        outputs["permission_asks_refused"] = requests
        outputs["failure"] = {"kind": "garbage_output", "evidence": refusal[:4000]}
        return None, dataclasses.replace(
            result, status="failed", exit_status=1, detail=refusal[:2000], outputs=outputs)

    def _ask_permission(self, row, node: NodeSpec, asking, *, outcome_ref, at: str) -> None:
        """Hold `node` on Dan's rulings, nothing spent (the D26/D29 hold on the node alone).

        A refused request is recorded with its reason and never sent; an already-allowed
        one is recorded as such. Either holds nothing, so a round with nothing to ask
        re-runs the node at once with the rulings in its brief.
        """
        from maestro.hitl import verify
        from maestro.workflows import permission_asks as asks_mod

        attempt_id, node_id = row["attempt_id"], row["node_id"]
        screened = asking["permission_asks"]
        record = wf_failures.FailureRecord(
            kind="dan_question",
            evidence=("permission request(s): " + asks_mod.summary(
                [s["request"] for s in screened]))[:wf_failures.EVIDENCE_LIMIT],
        )
        cone = (node_id,)
        self.lifecycle.requeue_for_question(
            attempt_id, run_id=self.run_id, node_id=node_id, cone=cone, record=record,
            outcome_ref=outcome_ref, skipped=self._skipped_below(cone), at=at,
        )
        task_id = self.revision.task_id
        title = str(_roadmap_task(task_id).get("title") or "")
        channel = self._permission_channel()
        asked = []
        for n, item in enumerate(screened):
            request = item["request"]
            spec = {
                "id": verify.permission_request_id(
                    task_id, self.revision.workflow_id, attempt_id, n),
                "task_id": task_id, "run_id": self.run_id,
                "workflow_id": self.revision.workflow_id, "attempt_id": attempt_id,
                "node_id": node_id, "hold": node_id, "title": title, "request": request,
            }
            line = (f"{task_id} {spec['id']} {node_id} family={request['family']} "
                    f"denied={request['denied'][:120]!r}")
            if item.get("refused"):
                verify.record_permission(spec, ruling=asks_mod.REFUSED, note=item["refused"])
                channel.journal("permission_refused", f"{line}: {item['refused']}")
                self.store.append_event(
                    PERMISSION_REFUSED_EVENT, task_id,
                    payload={"request_id": spec["id"], "node_id": node_id,
                             "request": request, "reason": item["refused"]},
                    run_id=self.run_id, attempt_id=attempt_id, recorded_at=at,
                )
            elif item.get("already_allowed"):
                verify.record_permission(spec, ruling=asks_mod.ALREADY_ALLOWED)
                channel.journal("permission_already_allowed", line)
            else:
                channel.poll_permission(spec)
                asked.append((spec["id"], request))
        if asked:
            channel.journal(
                "permission_asked",
                f"{task_id} run={self.run_id} holds={node_id}: "
                + "; ".join(f"{i} {r['family']}" for i, r in asked),
            )
            self.store.append_event(
                PERMISSION_ASKED_EVENT, task_id,
                payload={"node_id": node_id, "attempt_id": attempt_id,
                         "request_ids": [i for i, _ in asked],
                         "requests": [dict(r) for _, r in asked]},
                run_id=self.run_id, attempt_id=attempt_id, recorded_at=at,
            )
        self._release_held_attempt(row, node, at=at)

    def _poll_permission_rulings(self, *, at: str) -> None:
        """Record each ruling Dan gave since the last tick; Allow adds the family to the
        global list before the node is released, so its re-run already has it."""
        channel = self._permission_channel()
        if channel is None:
            return
        from maestro import permissions
        from maestro.hitl import verify
        from maestro.workflows import permission_asks as asks_mod

        for record in verify.permission_records(run_id=self.run_id):
            if record.get("recorded"):
                continue
            answer = channel.poll_permission(record)
            if answer is None:
                continue
            ruling, note = verify.interpret_permission(answer)
            request = record.get("request") or {}
            family = str(request.get("family") or "")
            if ruling == asks_mod.ALLOWED:
                try:
                    permissions.add(family, by="dan", request_id=str(record["id"]),
                                    reason=str(request.get("reason") or ""), at=at)
                except ValueError as exc:
                    ruling, note = asks_mod.REFUSED, f"Dan allowed it, but {exc}"
            channel.mark_question_recorded(
                str(record["id"]), ruling=ruling, note=note, by="dan", ruled_at=at,
            )
            channel.journal(
                f"permission_{ruling}",
                f"{self.revision.task_id} {record.get('id')} by=dan family={family}"
                + (f" note={note[:120]}" if note else ""),
            )
            self.store.append_event(
                PERMISSION_RULED_EVENT, self.revision.task_id,
                payload={"request_id": record.get("id"), "ruling": ruling, "by": "dan",
                         "note": note, "request": dict(request),
                         "workflow_id": self.revision.workflow_id},
                run_id=self.run_id, recorded_at=at,
            )

    def _poll_dan_questions(self, *, at: str) -> None:
        """Record each answer Dan gave since the last tick as a decision of the task.

        The decision is committed to the ROADMAP (and an ADR) before the question stops
        holding its node, so the re-run's brief already carries it.
        """
        channel = self.dan_channel
        if channel is None or not hasattr(channel, "poll_question"):
            return
        from maestro.hitl import verify

        for record in verify.question_records(run_id=self.run_id):
            if record.get("recorded"):
                continue
            answer = channel.poll_question(record)
            if answer is not None:
                self._record_dan_answer(record, answer, at=at)

    def _record_dan_answer(self, record: Mapping, answer: str, *, at: str) -> None:
        from maestro.docs import decisions
        from maestro.docs import roadmap as doc_roadmap
        from maestro.workflows import dan_questions

        channel = self.dan_channel
        task_id = self.revision.task_id
        spec = record.get("spec") or {}
        item = decisions.entry(
            asked_by=str(record.get("asked_by") or ""), question=str(spec.get("question")),
            answer=dan_questions.decision_answer(spec, answer),
        )
        sha = None
        try:
            sha = decisions.record(
                doc_roadmap.REPO, task_id, item, roadmap=doc_roadmap.ROADMAP_FILE,
                journal=channel.journal,
            )
        except Exception as exc:  # noqa: BLE001 — the record still carries the decision
            channel.journal("dan_decision_record_failed",
                            f"{task_id} {record.get('id')}: {type(exc).__name__}: {exc}"[:300])
        goal_decision = None
        if isinstance(spec.get("conflict"), Mapping):
            # GDT-7 (R50): a goal conflict's answer is also a decision the next gate and
            # review read (`goal_conflicts.judge`), so the same facts are not asked again.
            # The question record keeps it too: a lost store write is restored from there
            # (`goal_decisions`).
            from maestro import goal_conflicts

            try:
                goal_decision = goal_conflicts.decide(spec, answer, at)
                goal_conflicts.record(self.repo, goal_decision)
            except (OSError, ValueError) as exc:
                channel.journal("goal_decision_record_failed",
                                f"{task_id} {record.get('id')}: {type(exc).__name__}: "
                                f"{exc}"[:300])
        channel.mark_question_recorded(
            str(record["id"]), decision=item, decision_commit=sha,
            **({"goal_decision": goal_decision} if goal_decision is not None else {}))
        channel.journal(
            "dan_question_answered",
            f"{task_id} {record.get('id')} {item['asked_by']}: {item['answer'][:120]}"
            + (f" (commit {sha[:12]})" if sha else ""),
        )
        self.store.append_event(
            DAN_QUESTION_ANSWERED_EVENT, task_id,
            payload={"request_id": record.get("id"), "node_id": record.get("node_id"),
                     "asked_by": item["asked_by"], "question": item["question"],
                     "answer": item["answer"], "commit": sha},
            run_id=self.run_id, recorded_at=at,
        )

    def dan_decisions(self) -> list[dict]:
        """Dan's decisions for this task: the ROADMAP's `decisions:`, then any answered
        question whose ROADMAP write did not land (the record keeps it either way)."""
        from maestro.hitl import verify
        from maestro.workflows import dan_questions

        roadmap = [d for d in _roadmap_task(self.revision.task_id).get("decisions") or ()
                   if isinstance(d, Mapping) and d.get("question")]
        seen = {dan_questions.normalise(d["question"]) for d in roadmap}
        out = [dict(d) for d in roadmap]
        for r in verify.question_records(task_id=self.revision.task_id):
            decision = r.get("decision")
            if isinstance(decision, Mapping) and decision.get("question") and \
                    dan_questions.normalise(decision["question"]) not in seen:
                seen.add(dan_questions.normalise(decision["question"]))
                out.append(dict(decision))
        return out

    def goal_decisions(self) -> list[dict]:
        """GDT-7: Dan's goal decisions the gate and review judge by: the store plus every
        answered goal-conflict question of this task (its `goal_decision`, else decided
        from his raw answer). One the store lacks (a failed or lost write) is written back;
        a write that fails again is journalled, and the answer still counts."""
        from maestro import goal_conflicts
        from maestro.hitl import verify

        answered = []
        for r in verify.question_records(task_id=self.revision.task_id):
            spec = r.get("spec")
            if not isinstance(spec, Mapping) or not isinstance(spec.get("conflict"), Mapping):
                continue
            decision = r.get("goal_decision")
            if not isinstance(decision, Mapping):
                raw = verify._answer(str(r.get("id")))
                if raw is None:
                    continue
                try:
                    decision = goal_conflicts.decide(
                        spec, raw, str(r.get("answered_at")
                                       or verify._answered_at(str(r.get("id")))
                                       or r.get("ts") or ""))
                except ValueError:
                    continue
            answered.append(dict(decision))
        decisions, missing = goal_conflicts.merge(goal_conflicts.load(self.repo), answered)
        for decision in missing:
            try:
                goal_conflicts.record(self.repo, decision)
            except (OSError, ValueError) as exc:
                if self.dan_channel is not None:
                    self.dan_channel.journal(
                        "goal_decision_record_failed",
                        f"{self.revision.task_id} {decision.get('id')}: "
                        f"{type(exc).__name__}: {exc}"[:300])
        return decisions

    def _decision_answers(self) -> dict[str, str]:
        from maestro.workflows import dan_questions

        return {dan_questions.normalise(d["question"]): str(d.get("answer", ""))
                for d in self.dan_decisions()}

    def _question_note(self, node: NodeSpec, outputs: Mapping, decided: Mapping) -> str:
        """The rework brief's account of a question round: each question, Dan's answer,
        and, for a review, the round's findings, which the rework fixes too."""
        from maestro.workflows import dan_questions

        role = node.agent_definition_ref or node.kind
        lines = []
        for q in outputs.get("dan_questions") or ():
            answer = decided.get(dan_questions.normalise(q.get("question")))
            if answer is None:
                continue
            lines.append(
                f"The {role} ({node.node_id}) asked Dan: {q['question']} Dan decided: "
                f"{answer}. Make the code and tests follow this decision."
            )
        review = outputs.get("review")
        findings = review.get("findings") if isinstance(review, Mapping) else None
        if lines and findings:
            lines.append(
                f"The same review ({node.node_id}) also found, and the rework must fix:\n"
                + "\n".join(f"- {f}" for f in findings)
            )
        guarded = outputs.get("goals_guard_findings")
        if lines and guarded:
            # GDT-7: a gate that asked Dan about a goal conflict may also have blocked.
            lines.append(
                f"The same gate ({node.node_id}) also rejected the commit; rework these "
                "blocking findings:\n" + "\n".join(f"- {f}" for f in guarded)
            )
        return "\n".join(lines)

    def _record_origin_flags(self, node: NodeSpec, result, *, attempt_id: str, at) -> None:
        """Journal a review whose round-2+ findings' origins were missing or unusable.

        Dan, 2026-09-26: an unclassified origin never fails a review; it is recorded in the
        disposition (`origin_flags`) and here, so a reviewer that ignores the origin
        contract shows up in the counts rather than silently.
        """
        if node.agent_definition_ref != wf_failures.ROLE_REVIEWER:
            return
        review = (result.outputs or {}).get("review") if isinstance(
            result.outputs, Mapping) else None
        if not isinstance(review, Mapping) or not review.get("origin_flags"):
            return
        self.store.append_event(
            REVIEW_ORIGIN_FLAGGED_EVENT, attempt_id,
            payload={"node_id": node.node_id, "round": review.get("round"),
                     "flags": list(review["origin_flags"])},
            run_id=self.run_id, attempt_id=attempt_id, recorded_at=at,
        )

    def review_rounds(self) -> list[dict]:
        """The task's earlier review rounds, oldest first: each round's number, reviewer
        node, reviewed commit, disposition, finding records, and what the writer reported
        after it (P12 pilot findings D15, D17; ids and origins: Dan, 2026-09-26).

        A round is a review that reached a verdict; an attempt that wrote none (fails
        closed, `[INV-04]`) was re-asked and is not a round. The number is the position,
        which is what `review_round` in the next reviewer's manifest continues, so the ids
        a finding is stored under and the ids a later round cites agree.
        """
        from maestro import validation

        rounds: list[dict] = []
        # Every run of this workflow, not only this one: a diagnoser's repair opens a
        # successor run, and its final review is still a round of the same review
        # (P12 pilot finding D17).
        rows = self.store.conn.execute(
            "SELECT a.node_id, a.outcome_ref, a.manifest_artifact_id FROM attempts a"
            " JOIN task_runs r ON a.run_id = r.run_id"
            " WHERE r.workflow_id = ? AND a.outcome_ref IS NOT NULL ORDER BY a.rowid",
            (self.revision.workflow_id,),
        ).fetchall()
        nodes = {n.node_id: n for n in self.revision.nodes}
        for row in rows:
            node = nodes.get(row["node_id"])
            if node is None or self.artifacts is None:
                continue
            try:
                result = json.loads(self.artifacts.read(row["outcome_ref"]).decode("utf-8"))
            except Exception:  # noqa: BLE001 — an unreadable outcome says nothing
                continue
            if not isinstance(result, Mapping):
                continue
            if node.permissions_ref == "task_worktree_writer" and rounds:
                detail = str(result.get("detail") or "").strip()
                rounds[-1]["response"] = detail or f"(status {result.get('status')})"
                continue
            if node.agent_definition_ref != wf_failures.ROLE_REVIEWER:
                continue
            review = (result.get("outputs") or {}).get("review")
            if not isinstance(review, Mapping):
                continue
            if str(review.get("verdict") or "").strip().lower() not in \
                    validation.REVIEW_VERDICTS:
                continue
            commit = ""
            if row["manifest_artifact_id"]:
                try:
                    manifest = json.loads(self.artifacts.read(row["manifest_artifact_id"]))
                    commit = str(manifest.get("reviewed_commit") or "")
                except Exception:  # noqa: BLE001 — an unreadable manifest names no commit
                    pass
            number = len(rounds) + 1
            rounds.append({
                "round": number, "review": review, "commit": commit, "node": node.node_id,
                "records": _finding_records(review, number),
            })
        return rounds

    def rework_paths(self, node: NodeSpec) -> list[str]:
        """Path-like tokens in this task's blocking review findings, for a writer's or a
        reviewer's manifest (Dan, 2026-09-28: auto-widen scope for the rework).

        DuetFlow 06 run 4's reviewer found the defect in `duetflow/auth.py`, outside the
        task's `scope:`, and no later brief held that file. Blocking reviewer findings
        widen the manifest and the G8 gate. The manifest keeps the tokens that are files.
        """
        if not (node.permissions_ref == "task_worktree_writer"
                or node.agent_definition_ref == wf_failures.ROLE_REVIEWER
                or node.handler_ref == VERIFICATION_HANDLER):
            return []
        out: list[str] = []
        rounds = self.review_rounds()
        files = ()
        if rounds and self.routing is not None:
            snap, _, _ = self.routing.queries.resolve(self.revision.code_base_sha)
            files = self.routing.queries.index.list_files(snap)
        for entry in rounds:
            for record in entry["records"]:
                # GDT-6 (R36, D23): goal work the task does not ask for is out of scope; the
                # path it names is to be reverted, never added to the rework's scope.
                if record.get("severity") != "blocking" or record.get("kind") == "out_of_scope":
                    continue
                text = " ".join(str(record.get(k) or "") for k in ("finding", "scope"))
                out.extend(t for t in _path_tokens(text, files) if t not in out)
        return out

    def _note_scope_widened(self, node: NodeSpec, manifest, *, at: str) -> None:
        from maestro.repository.context import WIDENED_FOR_REWORK

        paths = [p[len(WIDENED_FOR_REWORK):] for p in manifest.retrieval_paths
                 if p.startswith(WIDENED_FOR_REWORK)]
        if not paths:
            return
        channel = self.dan_channel
        if channel is not None and hasattr(channel, "journal"):
            channel.journal(
                "scope_widened_for_rework",
                f"{self.revision.task_id} {node.node_id} run={self.run_id}: {', '.join(paths)}",
            )
        self.store.append_event(
            SCOPE_WIDENED_EVENT, self.revision.task_id,
            payload={"node_id": node.node_id, "paths": paths},
            run_id=self.run_id, recorded_at=at,
        )

    def review_history(self, rounds: list[dict] | None = None) -> str:
        """The task's earlier review rounds and what the writer did after each, for the
        next reviewer's brief (P12 pilot finding D15), each finding under its id and, from
        round 2, its origin (Dan, 2026-09-26).

        Without it every review is a fresh sample: DuetFlow 02 run 7's third reviewer
        demanded the Retry-After cap its predecessor had had removed.
        """
        parts = []
        for entry in self.review_rounds() if rounds is None else rounds:
            review = entry["review"]
            lines = [
                f"Round {entry['round']} ({entry['node']}"
                + (f", commit {entry['commit'][:12]}" if entry["commit"] else "")
                + f"): {review.get('reviewer_verdict') or review.get('verdict')}"
            ]
            for record in entry["records"]:
                label = "blocking" if record["severity"] == "blocking" else "follow-up"
                origin = record.get("origin")
                if origin and record.get("prior_finding"):
                    origin = f"{origin} of {record['prior_finding']}"
                lines.append(
                    f"  {label} {record['id']}" + (f" ({origin})" if origin else "")
                    + f": {record['finding']}"
                )
            if entry.get("response"):
                lines.append(f"  the implementer then reported: {entry['response'][:1500]}")
            parts.append("\n".join(lines))
        return "\n".join(parts)

    def worktree_for(self, node: NodeSpec) -> Path | None:
        """The worktree a node runs in or reads. Overridable by the caller's wiring.

        A declared `worktree` param wins. Otherwise a node in `TASK_TREE_PERMISSIONS`
        gets the task's (or its candidate's) tree at `confinement.worktree_for`: always
        for the writer, which `_prepare_task_tree` creates at claim, and for a reader only
        once that tree exists — a diagnoser launched before any writer ran reads nothing.
        """
        from maestro.workflows import task_tree

        declared = node.params.get("worktree")
        if declared:
            return Path(str(declared))
        if not self._owns_task_tree(node):
            return None
        tree = task_tree.path_for(
            self.repo, self.revision.task_id, candidate=self._tree_candidate(node),
        )
        if node.permissions_ref == "task_worktree_writer" or tree.exists():
            return tree
        return None

    def _tree_candidate(self, node: NodeSpec) -> str:
        """Which candidate's tree `node` works in: its own label, else the join's winner.

        A widened graph has no plain task tree, only one per candidate. The nodes after
        the join (gate, review, merge) carry no label, and the one tree they may read is
        the winner's (`[INV-06]`); before the join has chosen there is none.
        """
        own = str(node.params.get("candidate_id") or "")
        if own or node.permissions_ref == "task_worktree_writer":
            return own
        return self.candidate_winner()

    def candidate_winner(self) -> str:
        """The candidate id this run's join chose, or `""` (not widened, or not chosen)."""
        joins = [n for n in self.revision.nodes if n.handler_ref == CANDIDATE_SELECT_HANDLER]
        if not joins:
            return ""
        return str(self._committed_outputs(joins[0].node_id).get("winner") or "")

    def deny_list_config(self) -> dict | None:
        """D29: what the gate needs to run the merge guard's deny-list and ask Dan on a hit.

        Only with a channel to ask through; without one the merge guard refuses at merge.
        """
        target = self._merge_target()
        if target is None or self._deny_channel() is None:
            return None
        return {"target": target, "rulings": self.deny_rulings()}

    def scope_check_policy(self, node: NodeSpec | None = None) -> dict | None:
        if self.routing is None:
            return None
        from maestro.workflows import scope_gate

        task = self.routing.task_spec(self.revision.task_id)
        paths = self.rework_paths(node) if node is not None else ()
        files = ()
        if paths:
            snap, _, _ = self.routing.queries.resolve(self.revision.code_base_sha)
            files = self.routing.queries.index.list_files(snap)
        return scope_gate.policy(
            task, self.revision.code_base_sha or self._merge_target() or "",
            reviewer_paths=paths, snapshot_files=files,
        )

    def goals_guard_policy(self, node: NodeSpec | None = None) -> dict | None:
        """GDT-6: the gate's goals guard (`goal_guard.policy`) for this task against the
        run base; `None` when the project has no `docs/GOALS.md`."""
        from maestro.workflows import goal_guard

        return goal_guard.policy(self.repo, _roadmap_task(self.revision.task_id),
                                 self.revision.code_base_sha or self._merge_target() or "",
                                 decisions=self.goal_decisions())

    def node_inputs(self, node: NodeSpec) -> dict:
        """What a controller-executed node is handed besides its params.

        Deliberately empty by default: the specs a merge or accept node validates against
        are project policy, which `[P5]` owns. Until then a caller wires them in by
        subclassing or by assigning `runner.inputs_provider`.

        The candidate join is the exception (P12A W4): its `candidates` input is assembled
        here from the store, and applied last, so no provider can hand the join documents
        the controller did not reap itself (D06). With or without routing.
        """
        provider = getattr(self, "inputs_provider", None)
        inputs = dict(provider(node)) if callable(provider) else {}
        if node.handler_ref in SPEC_JUDGING_HANDLERS and "specs" not in inputs:
            # The checks merge re-runs and accept decides on are the ones the gate ran:
            # the task's ROADMAP verifications (`gates.authoritative_specs`). Without
            # them both fail closed on an empty closed world (INV-04), so no task could
            # ever be accepted under the graph loop (`_p12_open_threads` #1).
            from maestro import gates

            inputs["specs"] = gates.authoritative_specs(self.revision.task_id)
        if node.handler_ref == MERGE_HANDLER and "branch" not in inputs:
            # A widened task has no `impl/<task>`: merge integrates the one branch the
            # join chose, and no other candidate's commits (`[INV-06]`).
            winner = self.candidate_winner()
            if winner:
                from maestro import confinement

                inputs["branch"] = confinement.branch_name(
                    "impl", self.revision.task_id, candidate=winner
                )
        if node.handler_ref == MERGE_HANDLER and "proof_reviews" not in inputs:
            # P12 finding D10: merge refuses a risky-set edit no proof review approved, so it
            # is handed this run's review verdicts, each bound to the commit it reviewed.
            inputs["proof_reviews"] = self.proof_reviews()
        if node.handler_ref == MERGE_HANDLER and "risky_globs" not in inputs:
            inputs["risky_globs"] = list(self._risky_globs())
        if node.handler_ref in SPEC_JUDGING_HANDLERS and "review_dispositions" not in inputs:
            # `kind: dan` criteria are discharged by Dan's approval of the gate's snapshot.
            inputs["review_dispositions"] = self.dan_dispositions()
        if node.handler_ref == ACCEPTANCE_HANDLER and "snapshot" not in inputs:
            # Accept decides on the tree merge integrated, not on whatever snapshot some
            # earlier gate ran on: without it, pre-merge evidence would discharge the
            # task (D06's stale-evidence substitution).
            merged = self._committed_outputs("merge").get("merged_commit")
            if merged:
                inputs["snapshot"] = str(merged)
        if node.handler_ref == VERIFICATION_HANDLER and "deny_list" not in inputs:
            config = self.deny_list_config()
            if config is not None:
                inputs["deny_list"] = config
        if node.handler_ref == VERIFICATION_HANDLER:
            inputs["scope_check"] = self.scope_check_policy(node)
            guard = self.goals_guard_policy(node)
            if guard is not None:
                inputs["goals_guard"] = guard
        if node.handler_ref == MERGE_HANDLER and "deny_list_approved" not in inputs:
            inputs["deny_list_approved"] = self.deny_approved_keys()
        if node.handler_ref == VERIFICATION_HANDLER and "post_action_cmd" not in inputs:
            post = self.post_action_cmd_for(node)
            if post:
                inputs["post_action_cmd"] = post
        if node.handler_ref == VERIFICATION_HANDLER:
            inputs['manual_contract'] = self.manual_contract_for(node)
        inputs.update(self._attempt_inputs.get(node.node_id, {}))
        if node.handler_ref == CANDIDATE_SELECT_HANDLER:
            inputs["candidates"] = self.candidate_documents(node)
        return inputs

    # --- P12 finding D10: risky files and the proof review ---------------------------

    def _risky_globs(self) -> tuple[str, ...]:
        """The engineering policy's `quality_gates.risky_paths`, when routing carries one."""
        policy = getattr(self.routing, "policy", None) if self.routing is not None else None
        gates = getattr(policy, "quality_gates", None)
        return tuple(getattr(gates, "risky_paths", ()) or ())

    def risky_files_touched(self, tree: Path | None) -> tuple[str | None, list[str] | None]:
        """`(head, risky paths)` the tree's commits change relative to the run's base.

        The change set is the tree's HEAD against the merge base with `code_base_sha` (or,
        when the revision recorded none, with the target branch) — what the implementer
        actually did, never the scope the spec declared. `head` is
        `None` (and the list empty) when the tree is not a readable git worktree; the list
        is `None` when git could not diff a tree that is one (the caller refuses).
        """
        import subprocess

        from maestro import merge
        from maestro.config import _load_project_yaml

        if tree is None or not (Path(tree) / ".git").exists():
            return None, []
        head = _git_head(tree)
        if not head:
            return head, []
        # With no recorded base, the fork point from the branch merge will land on.
        base = self._review_base()
        proc = subprocess.run(
            ["git", "diff", "--name-only", "--no-renames", "-z", f"{base}...{head}"],
            cwd=str(tree), capture_output=True, text=True,
        )
        if proc.returncode != 0:
            return head, None
        changed = [p for p in proc.stdout.split("\0") if p]
        project = _load_project_yaml()
        risky_set = project.get("risky_set") if isinstance(project, dict) else None
        return head, merge.risky_files(changed, risky_set or (), self._risky_globs())

    def _reviewed_tree_strength(self, node: NodeSpec) -> tuple[int | None, str]:
        """The strongest catalog rung among the models that wrote the tree `node` reviews.

        Every succeeded attempt of an upstream writer counts (a rework builds on the tree
        the first round left), in this run and in every run a plan repair replaced to reach
        it: a carried writer has no attempt here, and a re-run one builds on what its old
        run committed. Each run is read by its own revision's writers, since a repair can
        remove or rename one. In a widened run only the join's winner counts.
        `(None, why)` when a writer's model is not in the catalog, which the caller refuses:
        a reviewer cannot be proven stronger than a model the catalog cannot rank.
        `(-1, "")` when no bound writer produced the tree (nothing to exceed).

        P13 W8: a failed or canceled run keeps `impl/<task>`, and a fresh run of the task
        builds on it, so the writers of an earlier run count too while the tree still holds
        their commits (`_kept_earlier_writers`). `(rank, note)` says so when they decide.

        P13 W8: an attempt that did not succeed counts too, unless it left nothing
        (`end_sha` equal to `start_sha`, `_record_end_sha`): one that stopped to ask Dan,
        was blocked or was killed keeps what it wrote, and a later attempt may commit or
        amend that into its own, so which commits the tree holds does not decide it. With
        no start recorded (no tree) it does not count. A succeeded attempt always counts.
        """
        winner = self.candidate_winner()
        best = -1
        # Each run's writers of the reviewed tree, by the revision that run ran.
        writers = {
            run_id: [
                n.node_id for n in revision.nodes
                if n.permissions_ref == "task_worktree_writer" and n.kind == "agent"
                and (
                    not winner or not n.params.get("candidate_id")
                    or str(n.params.get("candidate_id")) == winner
                )
            ]
            for run_id, revision in self._repair_lineage()
        }
        git, reviewed = self._reviewed_commits(node)
        if git is not None and reviewed is None:
            return None, "git could not say which commits the reviewed tree holds"
        kept = self._kept_earlier_writers(writers, git, reviewed, winner)
        if kept is None:
            return None, "git could not say which earlier commits the reviewed tree holds"
        for row in kept:
            try:
                best = max(best, self._binding_strength_rank(row))
            except Exception:  # noqa: BLE001 — an unrankable writer cannot be out-ranked
                return None, (
                    f"an earlier run's writer {row['node_id']} ran on "
                    f"{row['backend_id']}/{row['model_id']}, which the catalog cannot rank"
                )
        earlier = best
        for run_id, nodes in writers.items():
            if not nodes:
                continue
            rows = self.store.conn.execute(
                "SELECT node_id, backend_id, model_id, binding_json, status, start_sha, end_sha"
                f" FROM attempts WHERE run_id = ? AND node_id IN ({','.join('?' * len(nodes))})"
                " AND backend_id IS NOT NULL",
                (run_id, *nodes),
            ).fetchall()
            for row in rows:
                # A succeeded attempt counts for the tree it left: no retry restores past
                # it. One that ended otherwise counts unless its row says it left nothing.
                # Either way a later attempt may commit its uncommitted edits or amend
                # its commits into its own.
                if row["status"] != "succeeded" and (
                    not row["start_sha"] or row["end_sha"] == row["start_sha"]
                ):
                    continue
                try:
                    writer_rank = self._binding_strength_rank(row)
                except Exception:  # noqa: BLE001 — an unrankable writer cannot be out-ranked
                    return None, (
                        f"the writer {row['node_id']} ran on {row['backend_id']}/{row['model_id']}, "
                        "which the catalog cannot rank"
                    )
                best = max(best, writer_rank)
        if earlier == best and earlier >= 0:
            return best, "counting an earlier run of this task whose work the tree may still hold"
        return best, ""

    def _kept_earlier_writers(
        self, writers: Mapping[str, Sequence[str]], git, reviewed, winner: str = ""
    ) -> list | None:
        """Writer attempts of this task's earlier runs that the reviewed tree may still hold.

        `writers` names, for each run of this lineage, its writers of the reviewed tree;
        `winner` is the candidate whose tree that is, in a widened run.

        An attempt that did not succeed and left nothing (`end_sha` equal to `start_sha`)
        never counts. Another counts when the tree holds a commit it made
        (`_wrote_reviewed_commits`). Otherwise only where it started places it: a later
        writer may have amended its commits into its own, and a row from before `end_sha`
        was recorded has nothing else. The tree was kept when the reviewed tree's first
        writer started ahead of the run's base, and such an attempt then counts unless it
        started where that writer did (it left no commit above, and uncommitted changes
        are the next rule's). Ancestry says no more than that: an amend removes the commit
        an attempt started from, and a rebase onto a moved target puts it on the base's
        own history, exactly where a branch reset and built on again leaves it. So the
        floor may be higher than needed. A fresh tree (reset, or the earlier work merged
        into the base) is ahead of nothing, and nothing counts. `None`: git could not say.

        An attempt with no `end_sha` also counts when this lineage's first writer was
        handed a tree with uncommitted changes (`TaskTreePrepared`): the branch may be
        ahead of nothing, and that writer commits what is there as its own.
        """
        import json

        lineage = tuple(writers)
        marks = ",".join("?" * len(lineage))
        # Where the reviewed tree stood: a losing candidate's writer may have started
        # first, on a branch of its own that says nothing of the winner's.
        own = [(run_id, node_id) for run_id, nodes in writers.items() for node_id in nodes]
        first = self.store.conn.execute(
            "SELECT start_sha FROM attempts WHERE (run_id, node_id) IN"
            f" (VALUES {','.join(['(?,?)'] * len(own))})"
            " AND start_sha IS NOT NULL ORDER BY rowid LIMIT 1",
            tuple(value for pair in own for value in pair),
        ).fetchone() if own else None
        rows = self.store.conn.execute(
            "SELECT a.run_id, a.node_id, a.backend_id, a.model_id, a.binding_json, a.status,"
            " a.start_sha, a.end_sha"
            " FROM attempts a JOIN task_runs r ON r.run_id = a.run_id"
            f" WHERE r.task_id = ? AND a.run_id NOT IN ({marks})"
            " AND a.backend_id IS NOT NULL AND a.start_sha IS NOT NULL",
            (self.revision.task_id, *lineage),
        ).fetchall()
        if not rows or git is None:
            return []
        # What the lineage's first run found in the reviewed tree: a run a repair started
        # was handed its predecessor's tree, which that predecessor's own rows account for.
        found = [
            payload for payload in (
                json.loads(event["payload"]) for event in self.store.conn.execute(
                    f"SELECT payload FROM events WHERE type = ? AND aggregate_id IN ({marks})"
                    " ORDER BY rowid",
                    (TASK_TREE_PREPARED_EVENT, *lineage),
                )
            ) if not winner or payload.get("candidate") in ("", winner)
        ]
        found_uncommitted = bool(found and found[0].get("uncommitted"))
        counted, placed = [], []
        for row in rows:
            if row["status"] != "succeeded" and row["end_sha"] == row["start_sha"]:
                continue
            if row["end_sha"] and self._wrote_reviewed_commits(git, reviewed, row):
                counted.append(row)
            elif not row["end_sha"] and found_uncommitted:
                counted.append(row)
            else:
                placed.append(row)
        if not placed or first is None:
            return counted
        base = self._review_base()
        ahead = git("rev-list", "--max-count=1", f"{base}..{first['start_sha']}")
        if ahead.returncode != 0:
            return None
        if not ahead.stdout.strip():
            return counted
        return counted + [row for row in placed if row["start_sha"] != first["start_sha"]]

    def _reviewed_commits(self, node: NodeSpec):
        """`(git, commits)` for the tree `node` reviews: a runner for git inside it, and
        the commits its HEAD holds above the run's base. `(None, None)` with no git tree
        to read; `(git, None)` when git could not list them."""
        import subprocess

        tree = self.worktree_for(node)
        if tree is None or not (Path(tree) / ".git").exists():
            return None, None

        def git(*args: str):
            return subprocess.run(
                ["git", *args], cwd=str(tree), capture_output=True, text=True,
            )

        listed = git("rev-list", f"{self._review_base()}..HEAD")
        return git, (set(listed.stdout.split()) if listed.returncode == 0 else None)

    @staticmethod
    def _wrote_reviewed_commits(git, reviewed: set, row) -> bool:
        """Whether the writer attempt `row` made a commit the reviewed tree still holds.

        Its commits are `start_sha..end_sha`. A range git cannot list counts: nothing
        shows the attempt left no commit.
        """
        made = git("rev-list", f"{row['start_sha']}..{row['end_sha']}")
        return made.returncode != 0 or not reviewed.isdisjoint(made.stdout.split())

    def _review_base(self) -> str:
        """What a review's diff is taken against: the run's recorded base, or with none,
        the branch merge will land on."""
        merges = [n for n in self.revision.nodes if n.handler_ref == MERGE_HANDLER]
        target = str((merges[0].params.get("target_branch") if merges else "") or "main")
        return self.revision.code_base_sha or target

    def _repair_lineage(self) -> list[tuple[str, WorkflowRevision]]:
        """This run and the runs a plan repair replaced to reach it, each with the
        revision it ran, newest first.

        Only the repair chain: another run of the same workflow may have started from a
        reset tree, and counting it would raise the floor for commits that are not there
        (`_kept_earlier_writers` reads the tree for those).
        The link is the `WorkflowRepaired` event `apply_repair` appends.
        """
        import json

        from maestro.workflows.compiler import WORKFLOW_REPAIRED

        replaced: dict[str, str] = {}
        for row in self.store.conn.execute(
            "SELECT payload FROM events WHERE type = ? AND aggregate_id = ?",
            (WORKFLOW_REPAIRED, self.revision.task_id),
        ):
            payload = json.loads(row["payload"])
            if payload.get("old_run_id") and payload.get("new_run_id"):
                replaced[str(payload["new_run_id"])] = str(payload["old_run_id"])
        lineage = [(self.run_id, self.revision)]
        seen = {self.run_id}
        run_id = replaced.get(self.run_id)
        while run_id and run_id not in seen:
            seen.add(run_id)
            run = self.store.conn.execute(
                "SELECT workflow_id, workflow_revision FROM task_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            revision = (
                load_revision(self.store, run["workflow_id"], run["workflow_revision"])
                if run is not None else None
            )
            # A revision that cannot be read is ranked by the current graph's writers.
            lineage.append((run_id, revision or self.revision))
            run_id = replaced.get(run_id)
        return lineage

    def _review_strength_floor(self, node: NodeSpec) -> tuple[str | None, str]:
        """`(floor, refusal)` for a proof reviewer: one rung above the tree's writer.

        Dan, 2026-09-25 (P12 finding D10): the gate on an autonomous risky-set edit is a
        proof review by a model strictly stronger than the one that wrote the reviewed
        tree. The catalog's only comparable ordering is `MODEL_STRENGTHS`, so "stronger"
        is "a higher rung". With no rung above the writer's, the node is refused rather
        than bound to an equal or weaker model.
        """
        from maestro.backends import catalog as bk_catalog

        rank, why = self._reviewed_tree_strength(node)
        if rank is None:
            return None, f"route_no_stronger_reviewer: {why}"
        if rank < 0:
            return None, ""
        if rank + 1 >= len(bk_catalog.MODEL_STRENGTHS):
            return None, (
                "route_no_stronger_reviewer: the reviewed tree was written by a "
                f"{bk_catalog.MODEL_STRENGTHS[rank]!r} model and the catalog has no stronger "
                "rung; a risky-set change is reviewed only by a stronger model"
                + (f" ({why})" if why else "")
            )
        return bk_catalog.MODEL_STRENGTHS[rank + 1], ""

    def proof_reviews(self) -> list[dict]:
        """This run's latest proof-review verdicts, each bound to the commit it reviewed
        (a review carried forward by a plan repair included)."""
        import json

        reviews = []
        for reviewer in self.revision.nodes:
            if reviewer.agent_definition_ref != wf_failures.ROLE_REVIEWER:
                continue
            review = self._committed_outputs(reviewer.node_id).get("review")
            if not isinstance(review, Mapping):
                continue
            review = dict(review)
            row = self._committed_attempt(reviewer.node_id)
            if row is not None and row["manifest_artifact_id"] and self.artifacts is not None:
                try:
                    manifest = json.loads(self.artifacts.read(row["manifest_artifact_id"]))
                except Exception:  # noqa: BLE001 — an unreadable manifest names no subject
                    manifest = {}
                reviewed = manifest.get("reviewed_commit") if isinstance(manifest, Mapping) else None
                if reviewed and not review.get("subject_sha"):
                    review["subject_sha"] = str(reviewed)
            reviews.append(review)
        return reviews

    def _committed_outputs(self, node_id: str) -> Mapping:
        """The outputs of `node_id`'s latest committed result in this run, or `{}`."""
        result = self._committed_result(node_id)
        outputs = result.get("outputs") if isinstance(result, Mapping) else None
        return outputs if isinstance(outputs, Mapping) else {}

    def _committed_attempt(self, node_id: str):
        """`node_id`'s latest attempt in this run with a committed result, or `None`.

        A node a plan repair carried forward as `succeeded` ran in an earlier run; its
        node state names the attempt whose result it carries, and that result is its own.
        Only a carried `succeeded` node reads through: a node being re-run must not see
        its old run's outcome as its own (D06's stale-evidence substitution).
        """
        row = self.store.conn.execute(
            "SELECT outcome_ref, manifest_artifact_id FROM attempts"
            " WHERE run_id = ? AND node_id = ? AND outcome_ref IS NOT NULL"
            " ORDER BY rowid DESC LIMIT 1",
            (self.run_id, node_id),
        ).fetchone()
        if row is not None:
            return row
        return self.store.conn.execute(
            "SELECT a.outcome_ref, a.manifest_artifact_id FROM node_states s"
            " JOIN attempts a ON a.attempt_id = s.last_attempt_id"
            " WHERE s.run_id = ? AND s.node_id = ? AND s.phase = 'succeeded'"
            " AND a.outcome_ref IS NOT NULL",
            (self.run_id, node_id),
        ).fetchone()

    def _committed_result(self, node_id: str) -> Mapping:
        """`node_id`'s latest committed result in this run (`NodeResult` JSON), or `{}`."""
        if self.artifacts is None:
            return {}
        row = self._committed_attempt(node_id)
        if row is None:
            return {}
        try:
            result = json.loads(self.artifacts.read(row["outcome_ref"]).decode("utf-8"))
        except Exception:  # noqa: BLE001 — an unreadable outcome is no outcome
            return {}
        return result if isinstance(result, Mapping) else {}

    def candidate_documents(self, node: NodeSpec) -> dict[str, dict]:
        """One document per candidate with a *settled* attempt in this run (P12A W4).

        Settled means the result was committed by `_commit_result`, i.e. the attempt row
        carries an `outcome_ref`; the latest such attempt of each candidate node is the one
        read. A candidate that crashed, was reconciled with no receipt or never ran has no
        such attempt and gets no document, so the join marks it `abandoned` — present,
        never dropped (`[INV-06]`). Evidence is the controller-ingested rows of that
        attempt, never the agent's own account (D06). Without an artifact store there is
        nothing trustworthy to read the outcome from, so there are no documents at all.
        """
        import json

        from maestro.workflows import candidates as candidate_join

        if self.artifacts is None:
            return {}
        documents: dict[str, dict] = {}
        specs = candidate_join.specs_from_nodes(
            self.repo, self.revision.task_id, self.revision.nodes
        )
        for spec in specs:
            row = self.store.conn.execute(
                "SELECT attempt_id, status, outcome_ref FROM attempts"
                " WHERE run_id = ? AND node_id = ? AND outcome_ref IS NOT NULL"
                " ORDER BY rowid DESC LIMIT 1",
                (self.run_id, spec.node_id),
            ).fetchone()
            if row is None:
                continue
            try:
                result = json.loads(self.artifacts.read(row["outcome_ref"]).decode("utf-8"))
            except Exception:  # noqa: BLE001 — an unreadable outcome is no outcome
                continue
            outputs = result.get("outputs") if isinstance(result, Mapping) else None
            outputs = outputs if isinstance(outputs, Mapping) else {}
            documents[spec.candidate_id] = {
                "status": "succeeded" if row["status"] == "succeeded" else "failed",
                "evidence": [
                    dict(evidence)
                    for evidence in self.store.evidence_for_attempt(row["attempt_id"])
                ],
                "head_commit": str(outputs.get("head_commit") or ""),
                "detail": str(result.get("detail") or ""),
            }
        return documents

    def dispatch(self, node: NodeSpec, claim: Claim, *, at: str | None = None) -> Dispatched:
        """Step 3: launch into a named TMUX window, outside any transaction (`[INV-10]`).

        A node whose `permissions_ref` is controller-scoped never reaches tmux at all —
        see `execute_inline`, and `CONTROLLER_PERMISSIONS` for which those are. Every
        other node goes through `launch_worker`.
        """
        stamp = at or now_iso()
        if node.permissions_ref in CONTROLLER_PERMISSIONS:
            return self.execute_inline(node, claim, at=stamp)
        return self.launch_worker(node, claim, at=stamp)

    def launch_worker(self, node: NodeSpec, claim: Claim, *, at: str) -> Dispatched:
        """The single agent-launch choke point, between `claim` and the worker process.

        Every worker the graph loop starts passes through here exactly once: planned nodes,
        retries, candidates (P12A W4) and nodes of a repaired revision (W7). Binding
        resolution (W1), the durable routing decision (W3) and the future Context Gate
        (P12A §4) attach here, so nothing may launch a worker around this method.

        A failed launch is not a crash: the intent is marked failed, the lease released and
        the node moved to `failed`, so the next tick sees a decided node rather than a
        claim that will never be redeemed.
        """
        stamp = at
        window = window_name(self.revision.task_id, node.node_id)
        argv = self._worker_argv(self, node, claim)
        dispatched = self.dispatcher.dispatch(window, argv, cwd=self.repo)
        if not dispatched.launched:
            self._fail_claim(node, claim, None, stamp)
            return dispatched
        self.store.set_intent_status(claim.operation_id, "dispatched")
        return dispatched

    def _fail_claim(self, node: NodeSpec, claim: Claim, reason: str | None, at: str) -> None:
        """The one failure path for a claim that will never be redeemed.

        Used by a failed launch and by a routing refusal (a `block`, or a manifest the
        repository refused). The intent is failed, the attempt and node decided, the lease
        released. A refusal's reason is recorded against the attempt as a store event.
        """
        self.store.set_intent_status(claim.operation_id, "failed")
        self.lifecycle.finish_attempt(
            claim.attempt_id, run_id=self.run_id, status="failed", at=at
        )
        # A worker that never ran committed nothing; with no end it would count (D10), as
        # it still does when an earlier attempt left the tree with uncommitted changes.
        self._record_end_sha(claim.attempt_id, node.node_id, at=at)
        self.lifecycle.set_node_phase(
            self.run_id, node.node_id, "running", attempt_id=claim.attempt_id, at=at
        )
        self.lifecycle.set_node_phase(
            self.run_id, node.node_id, "failed", attempt_id=claim.attempt_id, at=at
        )
        if claim.resource_id is not None:
            self.store.release_lease(claim.resource_id, lease_epoch=claim.lease_epoch, at=at)
        if self.routing is not None:
            # W8: a claim that will never be redeemed holds no host resources either.
            self.routing.attempt_finished(self, claim.attempt_id)
        if reason:
            # W3's column is where the reason now lives durably; the event stays because
            # it is what gives the refusal a place in the ordered history of the run.
            self.lifecycle.record_routing_decision(
                claim.attempt_id, run_id=self.run_id, at=at,
                fields={"route_refusal": reason},
            )
            self.store.append_event(
                ROUTE_REFUSED_EVENT, claim.attempt_id,
                payload={"node_id": node.node_id, "route_refusal": reason},
                run_id=self.run_id, attempt_id=claim.attempt_id, recorded_at=at,
            )

    # --- operator verbs (P13 W8) ----------------------------------------------------

    def operator_cancel(
        self, node_id: str | None = None, *, at: str | None = None,
        terminate: Callable[[int, int], None] = os.kill,
        sleep: Callable[[float], None] = time.sleep, grace_seconds: float = 5.0,
    ) -> list[str]:
        """`ctl cancel`: stop one node and fail it, or stop every live node and the run.

        A canceled node is `failed` with kind `operator_canceled`, and the run then takes
        that node's ordinary failure path. With no node the run is finished `canceled`
        and its trees are released; the caller drops this runner, since a tick would
        still advance the failure edges of a run that no longer settles. Returns the
        nodes stopped. A refusal part-way leaves the nodes already stopped `failed`.
        """
        stamp = at or now_iso()
        targets = [node_id] if node_id is not None else [
            target for target, phase in self.phases().items()
            if phase in OPERATOR_STOPPABLE_PHASES
        ]
        for target in targets:
            attempt_id = self._operator_stop(
                target, at=stamp, terminate=terminate, sleep=sleep, grace_seconds=grace_seconds,
            )
            self.lifecycle.fail_operator_canceled(
                attempt_id, run_id=self.run_id, node_id=target, at=stamp,
                record=wf_failures.FailureRecord(
                    kind="operator_canceled", evidence="stopped by the operator (ctl cancel)",
                ),
            )
        if node_id is None:
            self.lifecycle.finish_run(self.revision.task_id, self.run_id, "canceled", at=stamp)
            self._release_task_trees("canceled", at=stamp)
        return targets

    def operator_retry(
        self, node_id: str, *, at: str | None = None,
        terminate: Callable[[int, int], None] = os.kill,
        sleep: Callable[[float], None] = time.sleep, grace_seconds: float = 5.0,
    ) -> str:
        """`ctl retry <task> <node>`: stop the node and queue a fresh attempt of it.

        No quality budget is spent (`Lifecycle.requeue_operator`), and a writer's next
        attempt restarts from the stopped one's `start_sha` (`_restore_start_tree`).
        Returns the stopped attempt.
        """
        stamp = at or now_iso()
        attempt_id = self._operator_stop(
            node_id, at=stamp, terminate=terminate, sleep=sleep, grace_seconds=grace_seconds,
        )
        self.lifecycle.requeue_operator(
            attempt_id, run_id=self.run_id, node_id=node_id, at=stamp,
            record=wf_failures.FailureRecord(
                kind="operator_retry", evidence="stopped by the operator (ctl retry)",
            ),
            skipped=self._skipped_below((node_id,)),
        )
        return attempt_id

    def _operator_stop(
        self, node_id: str, *, at: str, terminate, sleep, grace_seconds: float
    ) -> str:
        """Stop the writer of `node_id`'s current attempt; returns that attempt's id.

        The tmux window is killed first: for a worker that has not bound a pid yet it is
        the only stop there is. Then the pid the worker bound (or reported and no tick has
        bound yet) gets SIGTERM, a bounded grace, SIGKILL and a liveness check; a writer
        that survives refuses the verb before anything is recorded. The lease is released
        by its epoch and never revoked, because until the worker binds, the lease row's
        pid is the launcher's placeholder, which is this controller. For the same reason
        `os.getpid()` is never signalled: a step the controller runs itself carries it.
        """
        if node_id not in self.revision.node_ids:
            raise OperatorRefused(
                f"{node_id!r} is not a node of {self.revision.workflow_id}"
                f"@{self.revision.revision} ({', '.join(self.revision.node_ids)})"
            )
        row = self.store.conn.execute(
            "SELECT phase, last_attempt_id FROM node_states WHERE run_id = ? AND node_id = ?",
            (self.run_id, node_id),
        ).fetchone()
        phase = row["phase"] if row else "pending"
        if phase not in OPERATOR_STOPPABLE_PHASES or not row["last_attempt_id"]:
            raise OperatorRefused(
                f"{node_id} is {phase}; only a claimed, running or uncertain node can be stopped"
            )
        attempt_id = row["last_attempt_id"]
        if self.revision.node(node_id).permissions_ref not in CONTROLLER_PERMISSIONS:
            self.dispatcher.kill(window_name(self.revision.task_id, node_id))
        attempt = self.store.conn.execute(
            "SELECT writer_pid FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        pid = attempt["writer_pid"] if attempt is not None else None
        if pid is None:
            pid = self.inbox(attempt_id).writer_pid()
        if pid is not None and int(pid) != os.getpid() and self.is_alive(int(pid)):
            pid = int(pid)
            terminate(pid, signal.SIGTERM)
            remaining = grace_seconds
            while remaining > 0 and self.is_alive(pid):
                sleep(min(0.25, remaining))
                remaining -= 0.25
            if self.is_alive(pid):
                terminate(pid, signal.SIGKILL)
                sleep(0.25)
            if self.is_alive(pid):
                raise OperatorRefused(
                    f"{node_id}: writer {pid} survived SIGKILL; nothing was changed"
                )
        lease = self.store.conn.execute(
            "SELECT resource_id, lease_epoch FROM writer_leases WHERE owner_attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        if lease is not None:
            self.store.release_lease(
                lease["resource_id"], lease_epoch=int(lease["lease_epoch"]), at=at
            )
        for intent in self.store.intents_for_attempt(attempt_id):
            if intent["status"] in ("pending", "dispatched"):
                self.store.set_intent_status(intent["operation_id"], "failed")
        if self.routing is not None:
            self.routing.attempt_finished(self, attempt_id)
        self._record_end_sha(attempt_id, node_id, at=at)
        return attempt_id

    # --- step 5: the run's own settlement -------------------------------------------

    def settle(self, *, at: str | None = None) -> str | None:
        """Finish the run when its terminal requirements are decided."""
        stamp = at or now_iso()
        row = self.store.conn.execute(
            "SELECT status FROM task_runs WHERE run_id = ?", (self.run_id,)
        ).fetchone()
        if row is None or row["status"] != "running":
            return row["status"] if row else None
        phases = self.phases()
        outcome = run_outcome(self.revision, phases)
        if outcome is None:
            return None
        if self.routing is not None and self._still_working(phases):
            # P12B F7: the terminal requirements can be decided while `diagnose` (or the
            # `park` behind it) is still live, because a failure sink is not a terminal.
            # Finishing here would leave a dispatched worker's result with no run to
            # ingest it into, so the run stays open until the frontier is empty.
            return None
        self.lifecycle.finish_run(
            self.revision.task_id, self.run_id, outcome, at=stamp
        )
        self._record_routing_outcomes(outcome, at=stamp)
        self._release_task_trees(outcome, at=stamp)
        return outcome

    def _still_working(self, phases: Mapping) -> bool:
        """Whether some node of this run is live or about to be (P12B F7).

        A controller-executed node still `claimed` is not live: its handler already ran in
        process and reported `paused` (`park`, a `wait` or `human` node), so no worker
        result is coming for the run to ingest. Counting it kept a parked run open forever.
        """
        inline = {
            node.node_id for node in self.revision.nodes
            if node.permissions_ref in CONTROLLER_PERMISSIONS
        }
        if any(
            phase in ("claimed", "running") and node_id not in inline
            for node_id, phase in phases.items()
        ):
            return True
        return bool(ready_frontier(self.revision, phases))

    def _record_routing_outcomes(self, outcome: str, *, at: str) -> None:
        """One verified routing outcome per bound attempt, when the run settles (W3).

        The verdict is the *controller's* run outcome and never the agent's own account
        of how it went (D06), which is what `verified_by` records. Rows are appended: a
        verdict that differs from the one already standing for an attempt is written as
        an amendment naming its predecessor, so nothing previously believed is erased.
        """
        if self.routing is None:
            return
        from maestro.control.models import nanoid
        from maestro.workflows import routing as wf_routing

        success = outcome == "succeeded"
        rows = self.store.conn.execute(
            "SELECT attempt_id, node_id, shape_key_json FROM attempts"
            " WHERE run_id = ? AND binding_json IS NOT NULL ORDER BY rowid",
            (self.run_id,),
        ).fetchall()
        last_bound = {row["node_id"]: row for row in rows}
        for row in last_bound.values():
            previous = self.store.routing_outcomes(attempt_id=row["attempt_id"])
            if previous and bool(previous[-1]["success"]) == success:
                continue
            self.store.record_routing_outcome({
                "outcome_id": f"out_{nanoid()}",
                "attempt_id": row["attempt_id"],
                "run_id": self.run_id,
                "shape_key_json": row["shape_key_json"] or "",
                "success": int(success),
                "verified_by": "run_outcome",
                "amends": previous[-1]["outcome_id"] if previous else None,
                "recorded_at": at,
            })
            wf_routing.record_outcome(
                self.routing,
                wf_routing.shape_key_from_json(row["shape_key_json"]),
                success=success,
            )


def _default_worker_argv(runner: "WorkflowRunner", node: NodeSpec, claim: Claim) -> list[str]:
    """The command a supervised window runs.

    A module entry point rather than an inline `python -c`: the worker has to be
    re-readable in `tmux capture-pane` output by whoever is debugging at 3am, and a
    500-character one-liner is not.

    Through the pinned launcher rather than `-m`: the window gets the tmux server's
    environment, not this process's, so `-m` would import the project venv's editable
    install instead of the checkout that dispatched the node.
    """
    import sys

    from maestro import bootstrap

    argv = [
        *bootstrap.pinned_module_argv(sys.executable, "maestro.workflows.worker"),
        "--repo", str(runner.repo),
        "--run-id", runner.run_id,
        "--node-id", node.node_id,
        "--attempt-id", claim.attempt_id,
        "--operation-id", claim.operation_id,
        "--workflow-id", runner.revision.workflow_id,
        "--revision", str(runner.revision.revision),
    ]
    if claim.plan is not None:
        from maestro.workflows import routing as wf_routing

        argv += wf_routing.render_binding_argv(
            backend_id=claim.binding.backend_id,
            runtime_kind=wf_routing.runtime_kind(runner.routing, claim.binding),
            plan=claim.plan,
            manifest_artifact_id=claim.manifest_artifact_id,
            session=claim.session,
        )
    tree = runner.worktree_for(node)
    if tree is not None:
        argv += ["--worktree", str(tree)]
    if claim.selection_artifact_id:
        argv += ["--affected-selection-artifact-id", claim.selection_artifact_id]
    if claim.failure_context_artifact_id:
        argv += ["--failure-context-artifact-id", claim.failure_context_artifact_id]
    if node.handler_ref == VERIFICATION_HANDLER:
        scope_check = runner.scope_check_policy(node)
        if scope_check is not None:
            import json

            argv += ["--scope-check", json.dumps(scope_check, sort_keys=True)]
        guard = runner.goals_guard_policy(node)
        if guard is not None:
            import json

            argv += ["--goals-guard", json.dumps(guard, sort_keys=True)]
        # D29: the gate is a worker, so `node_inputs` never reaches it.
        deny = runner.deny_list_config()
        if deny is not None:
            import json

            argv += ["--deny-list-config", json.dumps(deny, sort_keys=True)]
    post = runner.post_action_cmd_for(node)
    if post:
        # G7: the gate after a manual step runs the prep's post_action_cmd first.
        argv += ["--post-action-cmd", post]
    contract = runner.manual_contract_for(node)
    if contract:
        import json
        argv += ['--manual-contract', json.dumps(contract, sort_keys=True)]
    return argv


def _loads(value) -> object | None:
    """A stored JSON column as a document, or `None` when it is absent or unreadable."""
    import json

    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _merge_probe(runner: "WorkflowRunner", node: NodeSpec) -> tuple[bool | None, str]:
    """`maestro.reconcile.merge_probe`: did an interrupted merge reach its target branch?

    Merge writes the target exactly once, by a compare-and-swap, after staging and
    verification (`StagingPipeline.fast_forward`). So the candidate being absent from the
    target's history proves nothing landed, and a fresh merge — which re-stages on the
    target's current head — cannot duplicate anything. Present, or unreadable, is `True`
    or `None`: the swap may have happened, and the caller escalates.
    """
    import subprocess

    task_id = str(node.params.get("task_id") or runner.revision.task_id)
    branch = str(node.params.get("branch") or f"impl/{task_id}")
    target = str(node.params.get("target_branch") or "main")
    try:
        proc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", branch, target],
            cwd=str(runner.repo), capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"git could not be asked: {exc}"
    if proc.returncode == 1:
        return False, f"{target} does not contain {branch}; nothing landed, merge re-runs"
    if proc.returncode == 0:
        return True, f"{target} already contains {branch}; the merge result was never recorded"
    return None, f"cannot tell whether {branch} reached {target}: {proc.stderr.strip()[:300]}"


def _last_attempt(runner: "WorkflowRunner", node: NodeSpec):
    """The node's latest attempt row in this run (`attempt_id`, `start_sha`), or `None`."""
    return runner.store.conn.execute(
        "SELECT attempt_id, start_sha FROM attempts WHERE run_id = ? AND node_id = ?"
        " ORDER BY rowid DESC LIMIT 1", (runner.run_id, node.node_id),
    ).fetchone()


def _worktree_probe(runner: "WorkflowRunner", node: NodeSpec) -> tuple[bool | None, str]:
    """`maestro.reconcile.worktree_probe`: what did a dead agent leave in the tree it edits?

    `False` when the tree still stands at the attempt's `start_sha` with nothing
    uncommitted, `True` when it holds commits or edits, `None` when there is no tree or
    start to compare. An agent is redone from its `start_sha` either way (F6), so for one
    the answer is evidence; it decides only a node with no writer to redo the work.
    """
    import subprocess

    tree, attempt = runner.worktree_for(node), _last_attempt(runner, node)
    start = attempt["start_sha"] if attempt is not None else None
    if tree is None or not start:
        return None, "no task tree and start commit to compare (the work is in its workspace)"
    try:
        commits = subprocess.run(
            ["git", "-C", str(tree), "rev-list", "--count", f"{start}..HEAD"],
            capture_output=True, text=True, timeout=60,
        )
        dirty = subprocess.run(
            ["git", "-C", str(tree), "status", "--porcelain"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"git could not be asked: {exc}"
    if commits.returncode or dirty.returncode:
        return None, f"cannot read {tree}: {(commits.stderr or dirty.stderr).strip()[:300]}"
    made, edits = int(commits.stdout.strip() or 0), len(dirty.stdout.splitlines())
    return bool(made or edits), (
        f"{made} commit(s) and {edits} uncommitted path(s) in {tree} since {start[:12]}"
    )


def _human_receipt_probe(runner: "WorkflowRunner", node: NodeSpec) -> tuple[bool | None, str]:
    """`maestro.reconcile.human_receipt_probe`: has Dan answered this attempt's request?

    The request is one durable record per attempt (`verify.action_request_id`), so the
    answer survives a controller that died before recording it. A controller lost while
    asking is resumed on the same attempt (`_repoll_operator_nodes`) and reads it there.
    """
    from maestro.hitl import verify

    attempt = _last_attempt(runner, node)
    if attempt is None:
        return False, "the manual step was never claimed"
    task_id = str(node.params.get("task_id") or runner.revision.task_id)
    request_id, asked, answer = verify.action_receipt(task_id, attempt["attempt_id"])
    if answer:
        return True, f"Dan answered {request_id}: {answer[:200]}"
    if asked:
        return False, f"{request_id} was sent and Dan has not answered it"
    return False, f"{request_id} was never asked"


def _async_job_probe(runner: "WorkflowRunner", node: NodeSpec) -> tuple[bool | None, str]:
    """`maestro.reconcile.async_job_probe`: did a detached job leave its awaited artifact?

    A launch has no identity to ask for (`launch_cmd` detaches and returns), so the only
    proof is an `await_artifact` of the task's awaited checks. One that exists proves the
    job ran; none existing proves nothing, because the job may still be running. So this
    never answers `False`, and a dead launch is never replayed.
    """
    from maestro.workflows import task_tree

    task_id = str(node.params.get("task_id") or runner.revision.task_id)
    tree = task_tree.path_for(Path(getattr(runner, "repo", None) or "."), task_id)
    cwd = tree if tree.exists() else Path(getattr(runner, "repo", None) or ".")
    awaited = [
        str(v["await_artifact"]) for v in _roadmap_task(task_id).get("verifications") or ()
        if isinstance(v, Mapping) and v.get("await") and v.get("await_artifact")
    ]
    landed = [a for a in awaited if (Path(a) if Path(a).is_absolute() else cwd / a).exists()]
    if landed:
        return True, f"awaited artifact {', '.join(landed)} exists; the job ran"
    if awaited:
        return None, f"cannot tell whether the job started: {', '.join(awaited)} not there yet"
    return None, "cannot tell whether the job started: the task awaits no artifact"


def _ask(probe: Callable, runner: "WorkflowRunner", node: NodeSpec) -> tuple[bool | None, str]:
    """A probe's answer; one that raises cannot tell (`None`), and never stops the tick."""
    try:
        return probe(runner, node)
    except Exception as exc:  # noqa: BLE001 — a probe is evidence, never a second failure
        return None, f"probe raised {type(exc).__name__}: {exc}"


#: A05: the postcondition probes the controller can run, by `reconciliation_handler`.
#: A declared handler missing here is treated as unprobed (never retried).
_POSTCONDITION_PROBES: Mapping[str, Callable] = {
    "maestro.reconcile.merge_probe": _merge_probe,
    "maestro.reconcile.worktree_probe": _worktree_probe,
    "maestro.reconcile.human_receipt_probe": _human_receipt_probe,
    "maestro.reconcile.async_job_probe": _async_job_probe,
}


def _git_head(tree) -> str | None:
    """`HEAD` inside `tree`, or `None` when git cannot answer for it (P12B F6)."""
    import subprocess

    try:
        done = subprocess.run(
            ["git", "-C", str(tree), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None


def _usage_json(document) -> str | None:
    """A usage snapshot as stored text, or `NULL` when there was nothing to measure."""
    import json

    return None if document is None else json.dumps(document, sort_keys=True)


def _quota_reset_recovered(settle_row, *, at: str) -> bool:
    """Whether fresh account telemetry disproves a CLI's still-exhausted reset time."""
    from maestro.workflows.routing import _zulu_epoch

    if settle_row is None or settle_row["reset_crossing"] != 1:
        return False
    try:
        sample = json.loads(settle_row["usage_settle_json"] or "null")
    except (TypeError, ValueError):
        return False
    if not isinstance(sample, dict) or sample.get("error") or sample.get("source") not in (
        "codex_app_server", "claude_oauth_usage_api",
    ):
        return False
    updated = _zulu_epoch(sample.get("updated_at"))
    ingested = _zulu_epoch(at)
    if updated is None or ingested is None or abs(updated - ingested) > 120:
        return False
    windows = sample.get("windows") or {}
    measured = [window.get("used_pct") for window in windows.values()
                if isinstance(window, dict) and isinstance(window.get("used_pct"), (int, float))
                and not isinstance(window.get("used_pct"), bool)]
    return bool(measured) and all(used < 100 for used in measured)


def _elapsed_sec(start: str | None, end: str | None) -> float | None:
    """Seconds between two control-plane stamps, or `None` when either cannot be read.

    Never estimated (G6): an attempt whose claim event is missing has no wall time, and
    reporting one anyway would put a measured-looking number next to genuine samples.
    """
    from datetime import datetime, timezone

    if not start or not end:
        return None
    try:
        opened = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        closed = datetime.strptime(end, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (closed - opened).total_seconds()


def _plus_seconds(stamp: str, seconds: float) -> str:
    from datetime import datetime, timedelta, timezone

    moment = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (moment + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── starting a run ───────────────────────────────────────────────────────────────────


def start_run(
    store: ControlStore,
    revision: WorkflowRevision,
    *,
    repo: Path,
    run_id: str | None = None,
    lifecycle: Lifecycle | None = None,
    persist: bool = True,
    at: str | None = None,
    model_policy_revision: str | None = None,
    **runner_kwargs,
) -> WorkflowRunner:
    """Register the task, persist the revision, open a run, and return its runner.

    Registering the task is idempotent: a project that already migrated its roadmap into
    `tasks` keeps its row and its status, because overwriting it here would reset exactly
    the counters `[INV-08]` protects.
    """
    stamp = at or now_iso()
    life = lifecycle or Lifecycle(store)
    if store.task(revision.task_id) is None:
        store.register_task(
            revision.task_id, spec_revision=revision.task_spec_revision,
            status="planned", at=stamp,
        )
    if persist and load_revision(store, revision.workflow_id, revision.revision) is None:
        store_revision(store, revision, at=stamp)
    row = store.task(revision.task_id)
    if row is not None and row["status"] == "planned":
        life.set_task_status(revision.task_id, "active", at=stamp)
    rid = run_id or new_run_id()
    life.start_run(
        revision.task_id, rid, workflow_id=revision.workflow_id,
        workflow_revision=revision.revision, at=stamp,
        model_policy_revision=model_policy_revision,
    )
    return WorkflowRunner(
        store, revision, run_id=rid, repo=repo, lifecycle=life, **runner_kwargs
    )


def _dan_answers(task_id: str) -> str:
    """Dan's answers to the task's `questions:` block, rendered as the legacy brief has
    them (`implementer._answers_section`), or `''` for a task that asks none.

    The graph loop opens such a task only once every question is answered (B12,
    `orchestrator._graph_main`); this is how those answers reach the writer's brief.
    """
    try:
        from maestro.docs.roadmap import get_task_by_id
        from maestro.implementer import _answers_section

        task = get_task_by_id(task_id)
        return _answers_section(task).strip() if task else ""
    except Exception:  # noqa: BLE001 — a ROADMAP that cannot be read asks nothing here
        return ""


def _gate_rework_note(node_id: str, outputs: Mapping, task_id: str) -> str:
    """P12 pilot finding D24: why the gate rejected the previous attempt — each failed
    check, what it checks, its command and its output (bounded like the diagnoser's)."""
    from maestro.workflows import failure_context

    output_by_evidence = outputs.get("check_output") or {}
    failed = [
        dict(row, output=[output_by_evidence[row["evidence_id"]]]
             if row.get("evidence_id") in output_by_evidence else [])
        for row in outputs.get("evidence") or ()
        if isinstance(row, Mapping) and row.get("outcome") != "passed"
    ]
    if not failed:
        return ""
    specs = {
        str(v.get("id")): v for v in _roadmap_task(task_id).get("verifications") or ()
        if isinstance(v, Mapping)
    }
    lines = []
    for row in failed:
        spec = specs.get(str(row.get("check_id")), {})
        line = f"- {row.get('check_id')} ({row.get('outcome')})"
        if spec.get("check"):
            line += f": {spec['check']}"
        if spec.get("cmd"):
            line += f"\n  command: {spec['cmd']}"
        lines.append(line)
    return (
        f"The gate ({node_id}) rejected the previous attempt: these checks failed.\n"
        + "\n".join(lines)
        + "\nRun each command yourself, read its output below, and make it pass without "
        "weakening the check.\n\n"
        + failure_context.render_evidence(failed, failure_context.EVIDENCE_TOKEN_CAP)
    )


def _roadmap_task(task_id: str) -> dict:
    """The task's ROADMAP block, or `{}` when it cannot be read."""
    try:
        from maestro.docs.roadmap import get_task_by_id

        return dict(get_task_by_id(task_id) or {})
    except Exception:  # noqa: BLE001 — a ROADMAP that cannot be read suggests nothing
        return {}


def prep_contract(task_id: str) -> str:
    """The B14 prep contract for a manual lane's writer (G7), from legacy
    `implementer._make_prep_brief`: do every automatable step, never the human one, commit,
    and name the one action in `result.json`."""
    from maestro.taskgraph import verification_brief

    task = _roadmap_task(task_id)
    hints = []
    if task.get("dan_action"):
        hints.append(f"ROADMAP already suggests dan_action: {task['dan_action']!r} — reuse/refine it.")
    if task.get("post_action_cmd"):
        hints.append(f"ROADMAP already suggests post_action_cmd: {task['post_action_cmd']!r}.")
    schema = (
        '{"task_id":"%s","status":"DONE|FAILED|BLOCKED",'
        '"dan_action":"<ONE action: a URL to open + \'Run all\', OR a single bash '
        'scripts/x.sh command — never a multi-step list>",'
        '"post_action_cmd":"<a single shell cmd to finalize AFTER Dan acts, or null>",'
        '"step_command":"<exact manual shell command for Maestro to wrap; null for a non-shell action>",'
        '"step_kind":"<shell or external; external requires an artifact/result step check>",'
        '"step_check_cmd":"<unprivileged command checking the step result>",'
        '"expected_result":"<what Dan should see when he runs the step>",'
        '"files_changed":[],"summary":"<=5 lines"}' % task_id
    )
    return "\n".join([
        "PREP CONTRACT (dispatch: manual): a human (Dan) must perform exactly ONE step that "
        "an agent cannot — a GPU/compute run, an approved prod-downtime window, or a "
        "hardware/credential decision. Your job is to do EVERYTHING ELSE so his whole "
        "involvement is that single action.",
        "- Do all automatable prep in the WORKTREE and commit it there. NEVER perform the "
        "human step yourself.",
        "- Write result.json in the WORKSPACE, before DONE: " + schema,
        "- `dan_action` is ONE line; `{worktree}` in it is replaced by the tree's path. "
        "`post_action_cmd` runs in the tree after Dan acts, before the verification gate.",
        "Every manual step requires step_check_cmd and expected_result. Shell dan_action values are always wrapped; "
        "external actions require a check of their resulting artifact or state. Done is not proof.",
        verification_brief(task),
        *hints,
    ])
