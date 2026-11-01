"""`maestro ctl remerge <task>`: re-run only merge and accept after a hand-fixed conflict.

A graph run whose `merge` node failed — typically a conflict with a task that merged
first — has already paid for its implementer, gate and proof review, and the review
approved. The operator fixes `impl/<task>` by hand so it merges trivially (main merged in,
deterministic tests green). Dan's rule for that recovery: no new review and no model usage.
Only the merge node, which re-runs the task's deterministic checks on the merged tree
(`StagingPipeline.integrate` stage 3), and acceptance may run again.

The reopen paths (`/unpark`, Retry on the failed-run notice) start a whole new run from
the implementer. This instead drafts a `partial_failure` plan repair with
`rerun_nodes=(merge,)` and commits it through `compiler.apply_repair`, so revision
`N + 1` carries implementer, gate and proof review forward as `succeeded` and leaves
merge and accept pending. The loop resumes the successor, like any `running` run, on
its next start.

Three deliberate differences from a diagnoser's repair, each a parameter of
`apply_repair` rather than a second mechanism:

* the old run stays `failed` (`keep_failed`) — it did fail, and history should say so;
* merge's attempt budget restarts in the successor (`fresh_attempts`) — its
  `max_attempts=1` would otherwise refuse the re-run, and a conflict the operator
  resolved is not a retry of the same failure;
* the revision's reason starts `OPERATOR_REMERGE_REASON`, which `repair_counters`
  leaves out of the diagnoser's `max_repairs` budget.
"""
from __future__ import annotations

from dataclasses import dataclass

from maestro.workflows import compiler
from maestro.workflows.models import WorkflowError

MERGE_HANDLER = "maestro.handlers.merge"


class RemergeRefused(WorkflowError):
    """The task's latest run is not a merge failure a remerge may recover. Nothing written."""


@dataclass(frozen=True)
class Remerge:
    task_id: str
    old_run_id: str
    new_run_id: str
    revision: int
    carried: tuple[str, ...]
    rerun: tuple[str, ...]


def _success_ancestors(revision, node_id: str) -> set[str]:
    """Every node `node_id` depends on through non-failure edges."""
    seen: set[str] = set()
    frontier = [node_id]
    while frontier:
        for edge in revision.incoming(frontier.pop()):
            if edge.guard_predicate == "on_failure" or edge.source_node_id in seen:
                continue
            seen.add(edge.source_node_id)
            frontier.append(edge.source_node_id)
    return seen


def remerge(
    store, task_id: str, *, lifecycle=None, run_id: str | None = None, at: str | None = None,
) -> Remerge:
    """Open the successor run of `task_id`'s failed merge. Raises `RemergeRefused`.

    Refused unless the task's latest run is `failed`, its merge node is `failed`, every
    node merge depends on succeeded, and no other node failed, is live or is `uncertain`.
    `canceled` is refused too: a canceled run was superseded (a repair's old run) or
    stopped on purpose, and neither is a merge failure to recover.
    """
    latest = store.conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? ORDER BY started_at DESC, rowid DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if latest is None:
        raise RemergeRefused(f"{task_id} has no graph run")
    old_run = latest["run_id"]
    if latest["status"] != "failed":
        raise RemergeRefused(
            f"{task_id}'s latest run {old_run} is {latest['status']}; only a failed run's "
            "merge can be re-run"
        )
    revision = compiler.load_revision(store, latest["workflow_id"], latest["workflow_revision"])
    if revision is None:
        raise RemergeRefused(
            f"run {old_run}'s revision {latest['workflow_id']}@{latest['workflow_revision']}"
            " is not stored"
        )
    merges = [n.node_id for n in revision.nodes if n.handler_ref == MERGE_HANDLER]
    if len(merges) != 1:
        raise RemergeRefused(f"run {old_run} has {len(merges)} merge nodes, not one")
    (merge,) = merges
    rows = {
        r["node_id"]: r
        for r in store.conn.execute("SELECT * FROM node_states WHERE run_id = ?", (old_run,))
    }
    phases = {n: (rows[n]["phase"] if n in rows else "pending") for n in revision.node_ids}

    live = sorted(n for n, p in phases.items() if p in compiler.ACTIVE_PHASES or p == "uncertain")
    if live:
        raise RemergeRefused(
            f"run {old_run} has live or uncertain node(s) "
            + ", ".join(f"{n}={phases[n]}" for n in live) + "; reconcile them first"
        )
    if phases[merge] != "failed":
        raise RemergeRefused(f"run {old_run}'s merge did not fail (it is {phases[merge]})")
    failed = sorted(n for n, p in phases.items() if p == "failed" and n != merge)
    if failed:
        raise RemergeRefused(
            f"run {old_run} also failed at {', '.join(failed)}; a remerge re-runs only merge"
        )
    unmet = sorted(n for n in _success_ancestors(revision, merge) if phases[n] != "succeeded")
    if unmet:
        raise RemergeRefused(f"merge's upstream {', '.join(unmet)} did not succeed in {old_run}")

    proposal = compiler.PlanRepairProposal(
        workflow_id=revision.workflow_id, base_revision=revision.revision,
        kind="partial_failure", rerun_nodes=(merge,),
        reason=f"{compiler.OPERATOR_REMERGE_REASON}: {old_run}'s {merge} failed; "
               f"branch fixed by the operator",
    )
    # Provenance for the carried nodes' events: the committed result each one carries.
    outcomes = {
        row["node_id"]: [row["outcome_ref"]]
        for row in store.conn.execute(
            "SELECT s.node_id, a.outcome_ref FROM node_states s"
            " JOIN attempts a ON a.attempt_id = s.last_attempt_id"
            " WHERE s.run_id = ? AND a.outcome_ref IS NOT NULL",
            (old_run,),
        )
    }
    try:
        plan = compiler.plan_repair(
            revision, proposal, phases,
            counters=compiler.repair_counters(store, revision.workflow_id, run_id=old_run),
            artifacts=outcomes,
        )
        new_run = compiler.apply_repair(
            store, plan, old_run_id=old_run, lifecycle=lifecycle, run_id=run_id, at=at,
            keep_failed=True, fresh_attempts=(merge,),
        )
    except compiler.PlanRepairRejected as exc:
        raise RemergeRefused(str(exc)) from exc

    from maestro.state import append_journal

    # Text line only: the durable record is `apply_repair`'s `WorkflowRepaired` event.
    append_journal(
        "graph_run_remerge",
        f"{task_id} {old_run}→{new_run} revision {revision.revision}→{plan.revision.revision}"
        f" (carried {', '.join(sorted(plan.carried))}; re-run {', '.join(plan.invalidated)})",
        shadow=False,
    )
    return Remerge(
        task_id=task_id, old_run_id=old_run, new_run_id=new_run,
        revision=plan.revision.revision, carried=tuple(sorted(plan.carried)),
        rerun=tuple(plan.invalidated),
    )
