"""The controller's side of the command inbox (P13 W8).

`[INV-01]`: an external actor (the watchdog, the CLI, Telegram) may not write the control
database or `state.json`; it enqueues a command (`control.store.enqueue_external_command`)
and the controller applies it. `drain` is that application. It runs once per poll in both
loops, so a command enqueued while the loop was down is applied when it next starts.

A handler takes the `Command` and returns one line saying what it did; it raises
`CommandRejected` to refuse. Every command a drain claims is settled in that drain
(`committed` or `rejected`, with the reason) and journaled. One left `processing` by a
controller that died between the two is settled by the next drain.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from maestro.control.reconcile import reconcile
from maestro.quota import _paused_until_epoch
from maestro.state import append_journal, read_state, write_state

__all__ = [
    "CommandRejected", "FOREIGN_COMMAND_TYPES", "GRAPH_COMMAND_TYPES",
    "REPEATABLE_COMMAND_TYPES", "check_graph_request", "drain", "state_handlers",
]

#: Command types another consumer owns: `hitl.commands` claims a Telegram verb by its
#: update id when Telegram redelivers it, so the drain must leave the row `received`.
FOREIGN_COMMAND_TYPES = frozenset({"telegram_control"})
#: The state commands (`state_handlers`): each reads what is true now and applies only
#: that, so one interrupted part-way is applied again rather than lost.
REPEATABLE_COMMAND_TYPES = frozenset({"clear_pause", "clear_blocked_on", "reconcile_leases"})
#: `ctl cancel` / `ctl retry`: only the graph loop has the live runners that apply them.
GRAPH_COMMAND_TYPES = ("graph_cancel", "graph_retry")

_ERROR_LIMIT = 400


class CommandRejected(Exception):
    """A command the current state does not allow. Its text is the recorded reason."""


def drain(store, handlers: Mapping[str, Callable]) -> list[tuple]:
    """Apply every `received` command, oldest first. Returns `(command, status, detail)`.

    A type with no handler is rejected by name rather than left to be re-read every poll.
    A handler that raises rejects its own command only; the rest of the inbox still runs.
    An operator verb waits, `received`, for a drain that has its handler: the
    paused-by-user wait has none, and the graph loop that follows it does.

    Claim and settle are two transactions, and this drain settles what it claims before
    it returns, so a command found `processing` on entry was claimed by a controller that
    died. A state command is applied again (its handler is safe to repeat); anything else
    is rejected, never replayed.
    """
    settled = []
    for command in store.claimed_commands():
        if command.command_type not in FOREIGN_COMMAND_TYPES:
            settled.append(_apply(store, command, handlers, interrupted=True))
    for pending in store.pending_commands():
        if pending.command_type in FOREIGN_COMMAND_TYPES:
            continue
        if pending.command_type in GRAPH_COMMAND_TYPES and pending.command_type not in handlers:
            continue
        command = store.claim_command(pending.command_id)
        if command is not None:
            settled.append(_apply(store, command, handlers))
    return settled


def _apply(store, command, handlers: Mapping[str, Callable], *, interrupted: bool = False) -> tuple:
    handler = handlers.get(command.command_type)
    try:
        if interrupted and command.command_type not in REPEATABLE_COMMAND_TYPES:
            raise CommandRejected("its controller stopped while applying it; not replayed")
        if handler is None:
            raise CommandRejected(f"no consumer for command type {command.command_type!r}")
        detail, status = str(handler(command) or ""), "committed"
    except CommandRejected as exc:
        detail, status = str(exc), "rejected"
    except Exception as exc:  # noqa: BLE001 — recorded on the command, never swallowed
        detail, status = f"{type(exc).__name__}: {exc}", "rejected"
    detail = detail[:_ERROR_LIMIT]
    store.settle_command(
        command.command_id, status, detail if status == "rejected" else None
    )
    append_journal(
        "command_applied" if status == "committed" else "command_rejected",
        f"{command.command_type} {command.command_id}: {detail}",
    )
    return command, status, detail


def check_graph_request(store, command_type: str, task_id: str, node_id: str | None = None):
    """Whether `ctl cancel` / `ctl retry` may be asked of this task now (P13 W8).

    Read-only, so it also runs on a client store: the CLI calls it before it enqueues, so
    the operator is refused at the terminal, and the controller calls it again when it
    applies the command, since the run may have moved in between. Raises `CommandRejected`
    with the reason; returns the task's latest `task_runs` row.
    """
    from maestro.workflows import compiler
    from maestro.workflows.runner import OPERATOR_STOPPABLE_PHASES

    if command_type not in GRAPH_COMMAND_TYPES:
        raise CommandRejected(f"{command_type!r} is not a graph operator command")
    task_id = str(task_id or "").strip()
    run = store.conn.execute(
        "SELECT run_id, task_id, workflow_id, workflow_revision, status FROM task_runs"
        " WHERE task_id = ? ORDER BY started_at DESC, rowid DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if run is None:
        raise CommandRejected(f"{task_id or '(no task)'} has no graph run")
    run_id, status = run["run_id"], run["status"]
    phases = {row["node_id"]: row["phase"] for row in store.conn.execute(
        "SELECT node_id, phase FROM node_states WHERE run_id = ? ORDER BY rowid", (run_id,)
    )}
    live = ", ".join(n for n, p in phases.items() if p in OPERATOR_STOPPABLE_PHASES) or "none"
    if node_id:
        if status != "running":
            raise CommandRejected(
                f"{task_id}'s latest run {run_id} is {status}; a node is stopped only in a "
                f"running run"
            )
        revision = compiler.load_revision(store, run["workflow_id"], run["workflow_revision"])
        if revision is not None and node_id not in revision.node_ids:
            raise CommandRejected(
                f"{node_id!r} is not a node of {task_id} ({', '.join(revision.node_ids)})"
            )
        phase = phases.get(node_id, "pending")
        if phase not in OPERATOR_STOPPABLE_PHASES:
            raise CommandRejected(
                f"{node_id} is {phase}; only a claimed, running or uncertain node can be "
                f"stopped (now: {live})"
            )
    elif command_type == "graph_cancel":
        if status != "running":
            raise CommandRejected(
                f"{task_id}'s latest run {run_id} is {status}; there is nothing to cancel"
            )
    elif status == "running":
        raise CommandRejected(
            f"{task_id} is running (run {run_id}); name the node to retry (now: {live})"
        )
    elif status == "succeeded":
        raise CommandRejected(
            f"{task_id}'s latest run {run_id} succeeded; there is nothing to retry"
        )
    return run


def state_handlers(repo: Path) -> dict[str, Callable]:
    """The handlers both loops share: the three requests the watchdog makes."""
    repo = Path(repo)

    def clear_pause(command) -> str:
        # The request names the pause the watchdog saw expire. A different value is a
        # pause set since, and the controller's own clock has the last word on expiry.
        state = read_state()
        current = state.get("paused_until")
        if current != command.payload.get("paused_until"):
            raise CommandRejected(f"paused_until is now {current!r}, not the pause requested")
        if _paused_until_epoch(state) > datetime.now(tz=timezone.utc).timestamp():
            raise CommandRejected(f"paused_until {current!r} has not expired")
        state["paused_until"] = None
        write_state(state)
        return f"cleared paused_until {current!r}"

    def clear_blocked_on(command) -> str:
        questions = repo / ".orchestrator" / "questions"
        asked = {str(item) for item in command.payload.get("blocked_on") or []}
        state = read_state()
        blocked = list(state.get("blocked_on") or [])
        answered = [r for r in blocked if r in asked and (questions / f"{r}.answer").exists()]
        if not answered:
            raise CommandRejected("none of the named requests is both blocking and answered")
        state["blocked_on"] = [r for r in blocked if r not in answered]
        write_state(state)
        return f"cleared {', '.join(answered)}; still blocked on {state['blocked_on'] or 'nothing'}"

    def reconcile_leases(command) -> str:
        from maestro.workflows.handlers import attempts_root

        store = _controller_store()
        verdicts = list(reconcile(store, attempts_root=attempts_root(repo)))
        return (f"reason={command.payload.get('reason')}; {len(verdicts)} verdict(s): "
                + (", ".join(f"{v.attempt_id}={v.disposition}" for v in verdicts) or "none"))

    return {
        "clear_pause": clear_pause,
        "clear_blocked_on": clear_blocked_on,
        "reconcile_leases": reconcile_leases,
    }


def _controller_store():
    from maestro.state import control_store

    return control_store()
