"""Mid-work backend switching and session rotation: four triggers, one path (D2, D3, D4).

Design authority is `docs/DESIGN.md` §7. An implementer that is running under one backend
can be handed to another *while it is working*, and the thing that makes that worth doing
is that **its uncommitted work survives**: the work lives in a git worktree on disk, and a
switch never touches that worktree. Conversation context does not survive — it is
re-summarised into a handoff brief built from the worktree itself, which is the same trade
already accepted for context-ceiling handoffs.

**Four triggers, one code path.** `switch_task` is the whole path; the trigger only
decides what it takes to stop the outgoing agent, and that decision is made from *window
liveness*, never from the reason string:

1. **Quota exhausted.** The process has already exited, so there is nothing to interrupt —
   `stop_agent` sees no live window and returns immediately.
2. **Usage threshold crossed.** A `SWITCH` sentinel is written into the implementer's
   workspace, which the operating preamble tells implementers to honour by checkpointing
   and exiting at the next tool boundary. Only after a grace period, and only if the
   window is still alive, is it killed. Never mid-tool-call: the hard kill is the fallback
   for an unresponsive implementer, and the work it can lose is bounded by the last
   checkpoint.
3. **Manual.** `/backend <name> [task-id]` — same path, with the target named explicitly
   instead of resolved from the fallback chain.
4. **Context ceiling reached** (D4). The implementer's own context has crossed its
   model's `prepare_handoff_high` (`maestro.limits`), so it is *rotated onto a fresh
   session of the backend it is already on* rather than handed to a different one:
   nothing is wrong with the backend, the conversation is simply full. This is the one
   trigger whose target is not chosen — `target_backend` is bypassed, because it answers
   `None` for a target equal to the current backend and a rotation expressed through it
   could never fire. It is also the one trigger that must never take the native-resume
   path: resuming restores the very conversation the rotation exists to drop.

The context trigger is *carried by the sentinel*. Without a checkpoint the rotation
degrades to a 120-second wait and a hard kill, which loses exactly the uncommitted work
it exists to preserve — so the sentinel/checkpoint contract in the implementer brief is
load-bearing for D4 in a way it never was for a backend switch.

Three rules this module exists to hold:

* **The worktree is reused, never recreated.** `maestro.worktree.create_worktree`
  force-removes and re-adds the worktree; calling it here would delete exactly the
  uncommitted work D3 exists to preserve. `worktree_path_for(task_id)` is a pure function
  of the task id, so "reuse" is simply *not recreating* — this module never calls
  `create_worktree`, and `tests/test_switch.py` makes that explosive rather than implicit.
* **Capabilities, never names.** Whether the target is resumed natively or re-briefed is
  decided by `capabilities().native_resume`; whether it is handed a system-prompt file is
  decided by `capabilities().system_prompt_file`. No backend name is compared against a
  literal anywhere in this module.
* **The journal record keeps its five fields.** `append_journal` writes
  `{ts, event, agent, session_id, detail}` and that shape is pinned by
  `tests/characterization/test_state.py`. A switch is therefore journalled by *flattening*
  into `detail`: `"<task_id> from=<a> to=<b> reason=<r>"`. No sixth field is added — a
  rotation, whose `from` and `to` are necessarily the same backend, is distinguished by
  its own event name (`session_rotation`) rather than by a sixth field, so that
  `from=X to=X` under `backend_switch` never reads as a bug in the switch.

Every seam that touches the world — the driver factory, the journal, Telegram, the state
document, tmux, git, the clock — is injectable through `SwitchDeps`, whose fields all
default to `None` and resolve to this module's globals **at call time**. That is what
lets the tests drive a real switch against a real throwaway git repository without
launching an agent, spending a token of quota or touching the operator's tmux server.
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from maestro import limits, roles
from maestro.backends import registry
from maestro.backends.base import Handle, LaunchSpec, Usage
from maestro.config import threshold
from maestro.hitl.telegram import notify_telegram
from maestro.implementer import SYS_PROMPT, brief_with_profile
from maestro.paths import Paths
from maestro.state import append_journal, now_iso, read_state, write_state
from maestro.worktree import _kill_tmux_window, tmux_window_exists, worktree_path_for

__all__ = [
    "REPO",
    "WORKSPACES",
    "SWITCH_SENTINEL",
    "CHECKPOINT_FILE",
    "BRIEF_FILE",
    "RESULT_FILE",
    "DEFAULT_GRACE_SEC",
    "GRACE_POLL_SEC",
    "SWITCH_THRESHOLD_PCT",
    "REASON_QUOTA",
    "REASON_THRESHOLD",
    "REASON_MANUAL",
    "REASON_CONTEXT",
    "STOPPED_GONE",
    "STOPPED_CHECKPOINTED",
    "STOPPED_KILLED",
    "SWITCH_EVENT",
    "SWITCH_FAILED_EVENT",
    "ROTATE_EVENT",
    "ROTATE_FAILED_EVENT",
    "SwitchDeps",
    "SwitchOutcome",
    "switch_sentinel_path",
    "sentinel_text",
    "request_checkpoint",
    "checkpoint_requested",
    "clear_checkpoint_request",
    "stop_agent",
    "commits_made",
    "diff_stat",
    "dirty_files",
    "original_brief",
    "checkpoint_notes",
    "verification_status",
    "remaining_steps",
    "handoff_brief",
    "target_backend",
    "threshold_crossed",
    "context_crossed",
    "switch_task",
]

_PATHS = Paths.from_env()

#: Path-valued module globals, so the characterisation sandbox can rebase them by
#: reflection. A module with `Path` globals and no `Path`-valued `REPO` is silently left
#: pointing at the live repository.
REPO = _PATHS.repo
WORKSPACES = _PATHS.workspaces

#: The sentinel a threshold or manual switch writes into the *outgoing* implementer's
#: workspace. It is deliberately not one of the terminal sentinels the orchestrator's
#: reconcile loop reads (`DONE` / `FAILED` / `PAUSED`), so writing it cannot be mistaken
#: for a task outcome.
SWITCH_SENTINEL = "SWITCH"

#: Where an implementer is asked to leave its handover notes, and where the handoff brief
#: reads them back from.
CHECKPOINT_FILE = "checkpoint.md"

#: Workspace files written by the launch path, read here.
BRIEF_FILE = "brief.txt"
RESULT_FILE = "result.json"

#: How long an implementer is given to honour the sentinel before the window is killed.
DEFAULT_GRACE_SEC = 120.0
#: How often liveness is re-checked while waiting out the grace period.
GRACE_POLL_SEC = 5.0

#: Deliberately *below* `maestro.quota.THROTTLE_75_PCT` so that a task switches before
#: the existing throttle would start starving the queue, and deliberately a separate
#: constant: the quota constants govern concurrency, not backend choice, and repurposing
#: one would couple two unrelated policies. Calibration waits until switching has run in
#: anger; now configurable via `thresholds: { switch_threshold_pct: ... }` in `project.yaml`,
#: falling back to 70.0.
#:
#: This is the *only* usage threshold anything enforces. `project.yaml` used to declare
#: a `switch.on_usage_threshold: {five_hour_pct, weekly_pct}` block that no code ever read;
#: it was removed on 2026-08-21 rather than wired, because its per-window shape contradicts
#: how the check actually works — `threshold_crossed()` compares one limit against
#: `Usage.max_used_pct()`, the most-consumed window, precisely so an account whose only
#: window is the unnamed one still trips it (finding G5). Making this configurable means
#: adding a single window-agnostic knob, not restoring that block.
SWITCH_THRESHOLD_PCT = threshold("switch_threshold_pct", 70.0, cast=float)

#: Reasons, journalled verbatim as `reason=<...>`.
REASON_QUOTA = "quota_exhausted"
REASON_THRESHOLD = "usage_threshold"
REASON_MANUAL = "manual"
#: D4. The one reason that means "stay on this backend, start a new session on it" — the
#: only reason `switch_task` reads to change what it does rather than only to record why.
REASON_CONTEXT = "context_ceiling"

#: What it took to stop the outgoing agent.
STOPPED_GONE = "gone"                    # the process had already exited
STOPPED_CHECKPOINTED = "checkpointed"    # it honoured the sentinel within the grace period
STOPPED_KILLED = "killed"                # the window had to be killed

SWITCH_EVENT = "backend_switch"
SWITCH_FAILED_EVENT = "backend_switch_failed"

#: D4's rotation, journalled under its own name. Same five fields, same flattened
#: `detail`; a distinct event only because a rotation's `from` and `to` are the same
#: backend by construction and would otherwise read as a switch that failed to switch.
ROTATE_EVENT = "session_rotation"
ROTATE_FAILED_EVENT = "session_rotation_failed"

#: How many commits the handoff brief lists, and how long a git probe may take.
COMMIT_LINES = 40
GIT_TIMEOUT_SEC = 30

#: Caps on the excerpts folded into the brief, so a runaway log cannot produce a brief
#: bigger than the context it is meant to fit into.
MAX_SECTION_CHARS = 4000
MAX_BRIEF_CHARS = 60000


# ── small helpers ──


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _bare_window(window: object) -> str:
    """The unqualified tmux window name.

    Handles carry a window in either spelling — `impl-<task>` or `agents:impl-<task>` —
    because a handle travels between modules and a bare name is ambiguous without its
    session. `tmux_window_exists` compares against bare `#{window_name}` output and the
    `in_flight` entry stores the bare form, so everything here is normalised to it.
    """
    return str(window or "").rsplit(":", 1)[-1].strip()


def _read_text(path: Optional[Path]) -> str:
    """A file's contents, or `""` for anything that cannot be read.

    Everything this module reads is written by an agent that may have been killed
    mid-write. A missing or truncated file means "that ingredient is unavailable"; it
    must never abort a switch.
    """
    if path is None:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _clip(text: str, limit: int = MAX_SECTION_CHARS) -> str:
    body = (text or "").strip()
    if len(body) <= limit:
        return body
    return body[:limit].rstrip() + f"\n… (truncated at {limit} characters)"


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line if line else line for line in text.splitlines())


def _section(title: str, body: str, empty: str) -> str:
    return f"{title}:\n{_indent(_clip(body) or empty)}"


def _timestamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")


def _default_session_id(task_id: str) -> str:
    """A new session id in the reference's shape, `impl-<task>-<YYYYmmdd-HHMMSS>`."""
    return f"impl-{task_id}-{_timestamp()}"


# ── injectable seams ──


@dataclass(frozen=True)
class SwitchDeps:
    """Every seam a switch reaches the world through.

    Each field defaults to `None` and resolves to this module's global at *call* time, so
    monkeypatching a module global still takes effect and a test can replace one seam
    without stubbing the rest.
    """

    driver_factory: Optional[Callable[[str], Any]] = None
    journal: Optional[Callable[..., None]] = None
    notify: Optional[Callable[[str], None]] = None
    load_state: Optional[Callable[[], dict]] = None
    save_state: Optional[Callable[[dict], None]] = None
    runner: Optional[Callable[..., Any]] = None
    window_exists: Optional[Callable[[str], bool]] = None
    kill_window: Optional[Callable[[str], None]] = None
    sleep: Optional[Callable[[float], None]] = None
    clock: Optional[Callable[[], str]] = None
    session_id_factory: Optional[Callable[[str], str]] = None

    def make_driver(self, name: str):
        factory = self.driver_factory or registry.get_backend
        return factory(name)

    def record(self, event: str, detail: str, session_id: str = "") -> None:
        """Journal, best effort: a failed write must not abandon a half-done switch."""
        try:
            (self.journal or append_journal)(event, detail, session_id)
        except Exception:
            pass

    def announce(self, message: str) -> None:
        """Notify, best effort. Telegram being down is not a reason to keep an
        implementer on an exhausted backend."""
        try:
            (self.notify or notify_telegram)(message)
        except Exception:
            pass

    def state(self) -> dict:
        return (self.load_state or read_state)()

    def store(self, state: dict) -> None:
        (self.save_state or write_state)(state)

    def run(self, argv: list[str], cwd: Path):
        return (self.runner or subprocess.run)(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SEC,
        )

    def alive(self, window: str) -> bool:
        return bool((self.window_exists or tmux_window_exists)(window))

    def kill(self, window: str) -> None:
        (self.kill_window or _kill_tmux_window)(window)

    def pause(self, seconds: float) -> None:
        (self.sleep or time.sleep)(seconds)

    def now(self) -> str:
        return (self.clock or now_iso)()

    def new_session_id(self, task_id: str) -> str:
        return (self.session_id_factory or _default_session_id)(task_id)


@dataclass(frozen=True)
class SwitchOutcome:
    """What a switch did — or, when `switched` is false, why it did nothing.

    A no-op outcome is a normal result, not an error: "there is nowhere to switch to" is
    the answer that tells the orchestrator to keep its existing throttle-and-wait
    behaviour, and raising there would force every call site to handle an exception on a
    path where there is nothing useful to do.

    `switched` means "the outgoing agent was stopped and a new one is running in its
    worktree", not "the backend name changed". For D4's rotation `to_backend` equals
    `from_backend` and `switched` is still true, because a fresh session on the same
    backend is exactly as much of a relaunch as a move to a different one.
    """

    task_id: str
    reason: str
    from_backend: str
    to_backend: Optional[str] = None
    switched: bool = False
    stopped: str = ""
    session_id: str = ""
    new_session_id: str = ""
    worktree: Optional[Path] = None
    workspace: Optional[Path] = None
    handle: Optional[Handle] = None
    resumed: bool = False
    entry: Optional[dict] = None
    note: str = ""


# ── the sentinel (trigger 2 and 3) ──


def switch_sentinel_path(workspace: Path | str) -> Path:
    return Path(workspace) / SWITCH_SENTINEL


def sentinel_text(to_backend: str, reason: str, requested_at: str) -> str:
    """The sentinel's contents: machine-readable header, then the instruction.

    The header is `key=value` lines so a reader (or `/progress`) can parse it without a
    JSON document; the prose below it is what the implementer actually acts on.

    Only the opening sentence varies by reason, and only for D4's rotation: telling an
    implementer it is being "moved to another agent backend" when it is being restarted
    on the one it is already running on is a lie it could act on. Everything that makes
    the sentinel work — commit, checkpoint, exit, do not write a terminal sentinel — is
    identical for all four triggers, because it is the same handover either way.
    """
    if reason == REASON_CONTEXT:
        opening = (
            "This session's context is nearly full, so the orchestrator is rotating the\n"
            "task onto a FRESH session of the same agent backend.\n"
        )
    else:
        opening = "The orchestrator is moving this task to another agent backend.\n"
    return (
        f"{SWITCH_SENTINEL}\n"
        f"to={to_backend}\n"
        f"reason={reason}\n"
        f"requested_at={requested_at}\n"
        "\n"
        f"{opening}"
        "At the NEXT tool boundary — never in the middle of a tool call:\n"
        f"  1. Commit whatever is worth keeping inside your worktree.\n"
        f"  2. Write {CHECKPOINT_FILE} in this workspace: what you did, what you were\n"
        "     part-way through, what is left, and anything the next agent would waste\n"
        "     time rediscovering.\n"
        "  3. Exit. Do NOT write DONE or FAILED — the task is not finished, it is\n"
        "     being handed over, and your uncommitted files are kept as they are.\n"
    )


def request_checkpoint(
    workspace: Path | str,
    *,
    to_backend: str,
    reason: str,
    requested_at: str = "",
) -> Optional[Path]:
    """Ask the running implementer to checkpoint and exit. Returns the sentinel path.

    Best effort: a workspace that has been cleaned up under us is not a reason to abandon
    a switch, because the grace period and the fallback kill still apply.
    """
    path = switch_sentinel_path(workspace)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            sentinel_text(to_backend, reason, requested_at or now_iso()), encoding="utf-8"
        )
    except OSError:
        return None
    return path


def checkpoint_requested(workspace: Path | str) -> bool:
    """True when a switch has been requested of the implementer in this workspace."""
    try:
        return switch_sentinel_path(workspace).exists()
    except OSError:
        return False


def clear_checkpoint_request(workspace: Path | str) -> None:
    """Remove the sentinel. Used when a switch is abandoned before it relaunches."""
    try:
        switch_sentinel_path(workspace).unlink()
    except OSError:
        pass


def stop_agent(
    window: object,
    workspace: Optional[Path | str] = None,
    *,
    to_backend: str = "",
    reason: str = "",
    grace_sec: Optional[float] = None,
    deps: Optional[SwitchDeps] = None,
) -> str:
    """Stop the outgoing implementer, and report what it took.

    The trigger is *not* consulted here. Liveness is: a quota-exhausted implementer has
    already exited, so the same code that would write a sentinel simply finds no window
    and returns `STOPPED_GONE`. That is what keeps all four triggers on one path instead
    of four that drift apart — D4's rotation stops its outgoing agent through exactly
    this function, and depends on the checkpoint half of it more than any of the other
    three, since the work it is preserving is the only thing the fresh session inherits.

    The kill is a fallback, never the first move (`docs/DESIGN.md` §7): the sentinel goes
    in first, the window is re-checked throughout the grace period, and a window that
    disappears on its own reports `STOPPED_CHECKPOINTED` — the implementer stopped at a
    tool boundary of its own choosing.
    """
    deps = deps or SwitchDeps()
    name = _bare_window(window)
    if not name or not deps.alive(name):
        return STOPPED_GONE

    if workspace is not None:
        request_checkpoint(
            workspace, to_backend=to_backend, reason=reason, requested_at=deps.now()
        )

    grace = DEFAULT_GRACE_SEC if grace_sec is None else max(0.0, float(grace_sec))
    waited = 0.0
    while waited < grace:
        step = min(GRACE_POLL_SEC, grace - waited)
        deps.pause(step)
        waited += step
        if not deps.alive(name):
            return STOPPED_CHECKPOINTED

    if not deps.alive(name):
        return STOPPED_CHECKPOINTED
    deps.kill(name)
    return STOPPED_KILLED


# ── the handoff brief, built from the worktree ──


def _git(args: list[str], worktree: Path, deps: SwitchDeps) -> str:
    """One read-only git command in the worktree. `""` on any failure.

    Every ingredient of the brief is optional: a worktree that is not a git repository
    yet, a repository with no commits, or a git that times out must each cost the brief
    one section, not the switch.
    """
    try:
        proc = deps.run(["git", *args], Path(worktree))
    except Exception:
        return ""
    if getattr(proc, "returncode", 1) != 0:
        return ""
    return (getattr(proc, "stdout", "") or "").strip()


def commits_made(worktree: Path | str, *, limit: int = COMMIT_LINES, deps=None) -> str:
    """The worktree's most recent commits, newest first.

    Honest about what it is: the last `limit` commits reachable from `HEAD`, not
    "the commits this implementer made". A linked worktree is created on a fresh branch
    but the fork point is not recorded anywhere this module can read, and inventing one
    (guessing a default branch name) would be worse than showing a few commits too many.
    """
    return _git(
        ["log", "--oneline", "-n", str(int(limit))], Path(worktree), deps or SwitchDeps()
    )


def diff_stat(worktree: Path | str, *, deps=None) -> str:
    """`git diff --stat` — the uncommitted, tracked changes that must survive."""
    return _git(["diff", "--stat"], Path(worktree), deps or SwitchDeps())


def dirty_files(worktree: Path | str, *, deps=None) -> str:
    """`git status --porcelain` — everything uncommitted, *including untracked files*.

    `git diff --stat` alone cannot see an untracked file, and an untracked file is
    exactly the kind of work a switch most easily appears to have destroyed. Both are
    reported.
    """
    return _git(["status", "--porcelain"], Path(worktree), deps or SwitchDeps())


def original_brief(workspace: Optional[Path | str]) -> str:
    """The brief the outgoing implementer was launched with, if it is still readable.

    On a second switch this is the *previous handoff* brief, which itself opens with the
    original brief — the task description therefore survives any number of switches,
    getting one layer of summary older each time.
    """
    if workspace is None:
        return ""
    return _read_text(Path(workspace) / BRIEF_FILE)


def checkpoint_notes(workspace: Optional[Path | str]) -> str:
    """Whatever the outgoing implementer left in `checkpoint.md`."""
    if workspace is None:
        return ""
    return _read_text(Path(workspace) / CHECKPOINT_FILE)


def _result(workspace: Optional[Path | str]) -> dict:
    if workspace is None:
        return {}
    try:
        document = json.loads(_read_text(Path(workspace) / RESULT_FILE) or "null")
    except Exception:
        return {}
    return document if isinstance(document, dict) else {}


def verification_status(workspace: Optional[Path | str]) -> str:
    """What the outgoing implementer had proved, from `result.json`.

    A task's verifications are the gate that decides whether its work counts, so the
    incoming agent needs to know which of them are already evidenced and which are still
    open — otherwise a switch quietly costs a re-run of everything.
    """
    result = _result(workspace)
    if not result:
        return ""
    lines = []
    status = _text(result.get("status"))
    if status:
        lines.append(f"reported status: {status}")
    verifications = result.get("verifications")
    if isinstance(verifications, Mapping) and verifications:
        for key, value in verifications.items():
            done = bool(value.get("done")) if isinstance(value, Mapping) else bool(value)
            proof = _text(value.get("proof")) if isinstance(value, Mapping) else ""
            mark = "done" if done else "NOT done"
            lines.append(f"{key}: {mark}" + (f" — {proof}" if proof else ""))
    return "\n".join(lines)


def remaining_steps(
    task_id: str,
    worktree: Path | str,
    workspace: Path | str,
    previous_workspace: Optional[Path | str] = None,
) -> str:
    """What is left: whatever the outgoing agent recorded, then the standing deliverables.

    The standing steps are the ones every implementer brief ends with, restated because
    the incoming agent is not carrying the original conversation — the handoff brief is
    the only instruction it ever sees.
    """
    result = _result(previous_workspace)
    lines: list[str] = []

    needs = _text(result.get("NEEDS")) if result else ""
    if needs:
        lines.append(f"- Unblock what the previous agent flagged as NEEDS: {needs}")

    verifications = result.get("verifications") if result else None
    if isinstance(verifications, Mapping):
        open_ids = [
            str(key)
            for key, value in verifications.items()
            if not (bool(value.get("done")) if isinstance(value, Mapping) else bool(value))
        ]
        if open_ids:
            lines.append(
                "- Finish and evidence the verifications still open: "
                + ", ".join(open_ids)
            )

    lines += [
        "- Continue the task from where it was left; read the worktree before changing it.",
        f"- Commit inside {worktree}:",
        f"    git add -A && git commit -m 'feat({task_id}): <one-line summary>'",
        f"- Write {Path(workspace) / RESULT_FILE} in the schema the original brief gives.",
        f"- Write {Path(workspace) / 'DONE'} (content: DONE) as the final step, or "
        f"{Path(workspace) / 'FAILED'} (reason text) if it failed.",
    ]
    return "\n".join(lines)


def handoff_brief(
    task_id: str,
    worktree: Path | str,
    workspace: Path | str,
    *,
    previous_workspace: Optional[Path | str] = None,
    from_backend: str = "",
    to_backend: str = "",
    reason: str = "",
    deps: Optional[SwitchDeps] = None,
) -> str:
    """The brief the incoming agent is launched with, built **from the worktree**.

    `docs/DESIGN.md` §7 fixes the ingredients: the original task brief, the commits made,
    `git diff --stat`, the checkpoint notes, the verification status and the remaining
    steps. They are assembled from the worktree and the outgoing workspace rather than
    from orchestrator state, because the worktree is the only place that knows what the
    outgoing agent actually did — state knows what it was asked to do.
    """
    deps = deps or SwitchDeps()
    worktree = Path(worktree)
    workspace = Path(workspace)

    # D4: a rotation's `to_backend` equals its `from_backend`, so "You are the claude
    # backend" would tell the incoming agent nothing and imply the move was a switch. The
    # fact it actually needs is that it is a new session with none of the old context.
    taking_over = (
        "You are a FRESH session on the SAME backend"
        if reason == REASON_CONTEXT
        else f"You are the {to_backend or 'new'} backend"
    )
    header = (
        f"You are taking over task {task_id} MID-FLIGHT.\n\n"
        f"The previous implementer ran on the {from_backend or 'previous'} backend and was "
        f"stopped (reason: {reason or 'unspecified'}). {taking_over}, "
        "picking the same work up in the SAME worktree. Its files are exactly as "
        "it left them, including uncommitted and untracked changes. That work is yours: "
        "build on it.\n\n"
        "GUARDRAILS FOR A HANDOVER:\n"
        "- Do NOT reset, clean, stash, re-clone or re-create the worktree. Uncommitted\n"
        "  work there is the whole point of this handover and is not recoverable.\n"
        "- Do NOT redo work that is already committed or already verified below.\n"
        "- You do not have the previous agent's conversation. Everything it managed to\n"
        "  pass on is in this brief and in the worktree."
    )

    parts = [
        header,
        f"WORKTREE (your git sandbox — work only within this directory):\n  {worktree}",
        f"WORKSPACE (write result.json + DONE/FAILED sentinel here):\n  {workspace}",
        _section(
            "ORIGINAL BRIEF (verbatim, as the previous implementer received it)",
            original_brief(previous_workspace),
            "(not recorded — reconstruct the task from the worktree and the roadmap)",
        ),
        _section(
            "COMMITS IN THIS WORKTREE (newest first)",
            commits_made(worktree, deps=deps),
            "(none — nothing has been committed yet)",
        ),
        _section(
            "UNCOMMITTED WORK — git diff --stat",
            diff_stat(worktree, deps=deps),
            "(no tracked file is modified)",
        ),
        _section(
            "UNCOMMITTED WORK — git status --porcelain (includes untracked files)",
            dirty_files(worktree, deps=deps),
            "(the worktree is clean)",
        ),
        _section(
            "CHECKPOINT NOTES FROM THE PREVIOUS IMPLEMENTER",
            checkpoint_notes(previous_workspace),
            "(none left — it did not get the chance to write any)",
        ),
        _section(
            "VERIFICATION STATUS",
            verification_status(previous_workspace),
            "(nothing verified yet — no result.json was written)",
        ),
        _section(
            "REMAINING STEPS",
            remaining_steps(task_id, worktree, workspace, previous_workspace),
            "(none recorded)",
        ),
    ]
    return _clip("\n\n".join(parts), MAX_BRIEF_CHARS)


# ── choosing the target ──


def target_backend(
    role: object,
    current: object,
    *,
    to: object = None,
    config: Optional[Mapping] = None,
    available: Optional[Iterable[object]] = None,
    exhausted: Iterable[object] = (),
) -> Optional[str]:
    """Where this task should go instead, or `None` when it should stay put.

    An explicitly named target (the manual trigger) is honoured as given, short of being
    unregistered or being the backend the task is already on — an operator naming a
    backend has made a decision that availability heuristics should not overrule. With no
    explicit target, `maestro.roles` walks the fallback chain, which is the only place
    that policy lives.
    """
    now = registry.normalise_name(current)
    wanted = registry.normalise_name(to) if to is not None else ""
    if wanted:
        if wanted not in registry.known_backends() or wanted == now:
            return None
        return wanted
    return roles.fallback_backend(
        role, now, config=config, available=available, exhausted=exhausted
    )


def threshold_crossed(
    usage: Optional[Usage], threshold: Optional[float] = None
) -> bool:
    """True when a usage sample says it is time to move this task.

    Window-agnostic on purpose (finding G5): it reads `Usage.max_used_pct`, the most
    consumed window *whatever its duration*, because an account whose only window is
    weekly has no five-hour window at all and a threshold keyed to that name could never
    fire — silently disabling switching on the very account that needs it.

    An absent sample, or one whose percentages are all unmeasurable (G6), returns
    `False`: "not measurable" is not evidence of pressure, and the existing
    throttle-and-wait behaviour stays in charge rather than being replaced on a guess.
    """
    if usage is None:
        return False
    used = usage.max_used_pct()
    if used is None:
        return False
    limit = SWITCH_THRESHOLD_PCT if threshold is None else float(threshold)
    return used >= limit


def context_crossed(
    usage: Optional[Usage],
    model: str = "",
    *,
    resolve: Optional[Callable[[str], Any]] = None,
) -> bool:
    """True when a sample says this session's *own context* is time to rotate (D4).

    The comparison is in **tokens**, against the running model's `prepare_handoff_high`
    from `maestro.limits` — not against `context_used_pct`, and not against
    `exception_ceiling`:

    * tokens, because the limits tables are token counts and a percentage is a fraction
      of a window the tables do not record. A session can sit at a comfortable-looking
      15 % of a million-token window and still be far past the point where a fresh one
      pays for itself, which is the whole reason D4 exists;
    * `prepare_handoff_high` rather than `exception_ceiling`, because the tables' own
      semantics (and the operator's session-triage rule they were written for) are that
      "prepare handoff" is where you act and the ceiling is where it is already too late.

    Every way of not knowing answers `False` — no sample, an unmeasurable or zero token
    count (G6: `None`/`0` is "not measurable here", never "the context is empty"), no
    model id to key the lookup on, no row for that id, or a lookup that raised. A
    rotation throws away a live session; "we could not measure" is never enough reason.
    A model id the table cannot resolve is reported by `doctor`'s `model_limits` check,
    which is where an operator should learn about it — not by silently rotating.

    `resolve` is the seam, defaulting to `limits.resolve`; callers that check several
    entries in one pass memoise through it rather than re-parsing the tables per entry.
    """
    if usage is None:
        return False
    used = int(getattr(usage, "context_total_input_tokens", 0) or 0)
    if used <= 0:
        return False
    name = _text(model) or _text(getattr(usage, "model", ""))
    if not name:
        return False
    try:
        row = (resolve or limits.resolve)(name)
    except Exception:
        return False
    if row is None:
        return False
    return used >= int(row.prepare_handoff_high)


# ── the one path ──


def _workspace_for(session_id: str) -> Optional[Path]:
    return (WORKSPACES / session_id) if session_id else None


def _replace_in_flight(old_session_id: str, entry: dict, deps: SwitchDeps) -> str:
    """Swap the outgoing `in_flight` entry for the incoming one. Returns a note.

    Written defensively because the state document is owned by the orchestrator loop and
    this may run beside it: a state that cannot be read or written costs the switch its
    bookkeeping, not its relaunch — the agent is already running by this point, and
    `reconcile_in_flight` re-adopts a live window on the next poll anyway.
    """
    try:
        state = deps.state()
        kept = [
            existing
            for existing in state.get("in_flight", [])
            if existing.get("session_id") not in {old_session_id, entry.get("session_id")}
        ]
        state["in_flight"] = kept + [entry]
        deps.store(state)
    except Exception as exc:
        return f"in_flight not updated: {exc}"
    return ""


def switch_task(
    task_id: str,
    *,
    reason: str,
    entry: Optional[Mapping] = None,
    to_backend: object = None,
    from_backend: object = None,
    role: object = roles.ROLE_IMPLEMENTER,
    session_id: str = "",
    worktree: Optional[Path | str] = None,
    workspace: Optional[Path | str] = None,
    handle: Optional[Handle] = None,
    grace_sec: Optional[float] = None,
    config: Optional[Mapping] = None,
    available: Optional[Iterable[object]] = None,
    exhausted: Iterable[object] = (),
    deps: Optional[SwitchDeps] = None,
) -> SwitchOutcome:
    """Move one task to another backend — or, for `REASON_CONTEXT`, onto a fresh session
    of its own backend — keeping its worktree exactly as it is.

    The order is deliberate:

    1. **Choose the target first.** If there is nowhere to go, nothing is stopped — an
       implementer must never be killed for a switch that cannot happen. `REASON_CONTEXT`
       is the one reason that skips the choice: a rotation's target is the backend the
       task is already on, so it never has nowhere to go (D4).
    2. **Stop the outgoing agent** (sentinel, grace, kill-as-fallback — or nothing at all
       when it has already exited).
    3. **Build the handoff brief afterwards**, so the commits and checkpoint notes the
       implementer wrote *during* the grace period are in it.
    4. **Relaunch in the same worktree.** `create_worktree` is never called: it
       force-removes and re-adds the worktree, which would destroy the uncommitted work
       this whole path exists to preserve. The incoming agent gets a *new* workspace, so
       the outgoing brief, log and sentinels stay readable and no terminal sentinel is
       inherited.
    5. **Journal, then notify, then update `in_flight`** — in that order, so the record of
       what happened exists even if the bookkeeping fails.

    `entry` is the orchestrator's `in_flight` entry. Its `backend` key is read if present
    and *written* either way, so this works both before and after that key exists at the
    launch sites.
    """
    deps = deps or SwitchDeps()
    task_id = str(task_id)
    reason = _text(reason) or REASON_MANUAL
    entry = dict(entry) if isinstance(entry, Mapping) else {}

    session_id = _text(session_id) or _text(entry.get("session_id"))
    role_name = roles.normalise_role(entry.get("role") or role) or roles.ROLE_IMPLEMENTER
    current = registry.normalise_name(
        from_backend if from_backend is not None else entry.get("backend")
    ) or roles.backend_for(role_name, config=config)

    # D4. A rotation is a same-backend move, and `target_backend` answers `None` for
    # exactly that, so it is bypassed rather than coaxed: expressing "stay here, start
    # fresh" as a degenerate switch to yourself would make every no-target guard in this
    # module — and `SwitchOutcome.switched`, which means "the backend changed" — read
    # backwards.
    rotation = reason == REASON_CONTEXT
    target = current if rotation else target_backend(
        role_name,
        current,
        to=to_backend,
        config=config,
        available=available,
        exhausted=exhausted,
    )
    if not target:
        return SwitchOutcome(
            task_id=task_id,
            reason=reason,
            from_backend=current,
            session_id=session_id,
            note="no target backend available; leaving the task where it is",
        )

    path = Path(worktree) if worktree else Path(
        _text(entry.get("worktree")) or worktree_path_for(task_id)
    )
    previous_workspace = (
        Path(workspace)
        if workspace is not None
        else (Path(handle.workspace) if handle is not None else _workspace_for(session_id))
    )
    window = _bare_window(
        entry.get("window") or (handle.window if handle is not None else "") or f"impl-{task_id}"
    )

    stopped = stop_agent(
        window,
        previous_workspace,
        to_backend=target,
        reason=reason,
        grace_sec=grace_sec,
        deps=deps,
    )

    new_session_id = deps.new_session_id(task_id)
    new_workspace = WORKSPACES / new_session_id
    new_workspace.mkdir(parents=True, exist_ok=True)

    brief = handoff_brief(
        task_id,
        path,
        new_workspace,
        previous_workspace=previous_workspace,
        from_backend=current,
        to_backend=target,
        reason=reason,
        deps=deps,
    )

    try:
        driver = deps.make_driver(target)
        capabilities = driver.capabilities()
        spec = LaunchSpec(
            task_id=task_id,
            session_id=new_session_id,
            model=roles.model_for(role_name, target, config=config) or "",
            brief=brief_with_profile(brief, capabilities.system_prompt_file, SYS_PROMPT),
            workspace=new_workspace,
            worktree=path,
            system_prompt_file=SYS_PROMPT if capabilities.system_prompt_file else None,
        )
        # Capability, never name: a driver that can resume a session it already owns is
        # handed the brief as a continuation; one that cannot is re-briefed from scratch.
        # A handle for a *different* backend is not resumable by this driver — switching
        # back to a backend a task ran on earlier is the case this serves.
        #
        # D4 is the one exception, and it is not a name check: a rotation exists to shed
        # the conversation, and a native resume is precisely the thing that would restore
        # it. Rotating into a resumed session would relaunch the agent at the same context
        # it just crossed the handoff mark on, and it would cross it again immediately.
        resumable = (
            not rotation
            and handle is not None
            and capabilities.native_resume
            and bool(handle.native_id)
            and registry.normalise_name(handle.backend) == target
        )
        new_handle = driver.resume(handle, brief) if resumable else driver.launch(spec)
    except Exception as exc:
        deps.record(
            ROTATE_FAILED_EVENT if rotation else SWITCH_FAILED_EVENT,
            f"{task_id} from={current} to={target} reason={reason} error={exc}"[:300],
            session_id,
        )
        return SwitchOutcome(
            task_id=task_id,
            reason=reason,
            from_backend=current,
            to_backend=target,
            stopped=stopped,
            session_id=session_id,
            worktree=path,
            workspace=new_workspace,
            note=f"relaunch failed: {exc}",
        )

    live_session_id = _text(getattr(new_handle, "session_id", "")) or new_session_id
    new_entry = dict(entry)
    new_entry.update(
        {
            "session_id": live_session_id,
            "task_id": task_id,
            "role": role_name,
            "worktree": str(path),
            "window": _bare_window(getattr(new_handle, "window", "")) or f"impl-{task_id}",
            "started_at": deps.now(),
            "status": "running",
            "backend": target,
        }
    )

    # Five fields, flattened into `detail` (F5). Do not add a sixth — a rotation is told
    # apart by its event name, not by an extra column.
    deps.record(
        ROTATE_EVENT if rotation else SWITCH_EVENT,
        f"{task_id} from={current} to={target} reason={reason}",
        session_id,
    )
    headline = (
        f"Session rotation: {task_id} restarted on {target} with a fresh session ({reason})."
        if rotation
        else f"Backend switch: {task_id} {current} -> {target} ({reason})."
    )
    deps.announce(
        f"{headline} Same worktree {path}, uncommitted work preserved. "
        f"New session {live_session_id}."
    )
    note = _replace_in_flight(session_id, new_entry, deps)

    return SwitchOutcome(
        task_id=task_id,
        reason=reason,
        from_backend=current,
        to_backend=target,
        switched=True,
        stopped=stopped,
        session_id=session_id,
        new_session_id=live_session_id,
        worktree=path,
        workspace=Path(getattr(new_handle, "workspace", new_workspace) or new_workspace),
        handle=new_handle,
        resumed=bool(resumable),
        entry=new_entry,
        note=note,
    )
