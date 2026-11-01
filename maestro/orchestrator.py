"""The event loop itself: `main`, the per-poll reconcilers and the proposal cycle.

Extracted verbatim from the reference orchestrator. Bodies are unchanged — only the
import block and the derivation of the module-level path globals differ. `main` is
copied exactly as it stands (592 lines, cyclomatic complexity 249); splitting or tidying
it would be a behaviour change, so it is left alone. Behavioural surprises are catalogued
in `docs/found_bugs_inbox/orchestrator.md` and pinned by
`tests/characterization/test_orchestrator.py`; none of them is fixed here — including
`_do_retry` silently ignoring the `retry_counts` argument it is handed.

Nothing in this module reaches the network or spawns a process for real in the tests: the
tmux probe and the async-job launcher go through this module's `subprocess` reference, the
launch clock through its `time` reference, and every operator-facing message through
`notify_telegram`, all of which the characterisation tests swap.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

import yaml  # PyYAML

from maestro.backends.base import ExitVerdict, Handle, Usage, to_usage_json
from maestro import config as _config
from maestro import scheduling
from maestro.control.resources import HostReservationLedger
from maestro.taskgraph import ResourceSpecError
from maestro.backends.registry import (
    backend_binary,
    get_backend,
    known_backends,
    normalise_name,
)
from maestro.docs.roadmap import (
    _load_roadmap_tasks,
    get_completed_task_ids,
    get_task_by_id,
    mark_roadmap_complete,
    maybe_push_roadmap_map_change,
    parse_prep_tasks,
    parse_runnable_tasks,
    reduce_roadmap_deps,
    run_dep_map,
)
from maestro.gates import (
    AWAIT_VERIFY_TIMEOUT_SEC,
    SMOKE_TIMEOUT,
    _await_timed_out,
    _classify_verifications,
    run_verification_gate,
    sonnet_review_proofs,
)
from maestro.hitl import verify as _verify
from maestro.hitl.commands import poll_control_commands, run_status, start_update_listener
from maestro.hitl.telegram import (
    _danreq,
    _send_hitl_reminders,
    delivery_note,
    notify_telegram,
    notify_intake,
    phase_report,
)
from maestro.implementer import (
    _questions_ready,
    _synthesize_sentinel,
    launch_implementer,
    operator_backend,
    resolve_launch_model,
)
from maestro.limits import resolve as resolve_model_limits
from maestro import metabranch
from maestro import metrics
from maestro.merge import _touches_bot_files, merge_and_eval, run_canary_deploy
from maestro.parking import (
    _graduate_manual_action,
    defer_merged_verification,
    handle_incomplete,
    handle_prep_done,
    park_failed,
    park_for_dan,
    park_regression,
)
from maestro.paths import Paths
from maestro.quota import (
    CONCURRENCY_CAP,
    PAUSE_PCT,
    _paused_until_epoch,
    _tail_text,
    exhausted_backends,
    get_effective_cap,
    record_backend_exhausted,
)
from maestro.roles import (
    ROLE_IMPLEMENTER,
    backend_for,
    fallback_backend,
    model_for,
    normalise_role,
)
from maestro.selfheal.diagnose import _judge_complete
from maestro.selfheal.redo import apply_ready_redo
from maestro.selfheal.selffix import apply_ready_self_fixes
from maestro.selfupdate import maybe_self_update
from maestro.state import (
    _persist_launch_time,
    append_journal,
    now_iso,
    read_state,
    write_state,
)
from maestro.workflows import handlers as _facade
from maestro import state as _state_module
from maestro.switch import (
    REASON_CONTEXT,
    REASON_QUOTA,
    REASON_THRESHOLD,
    ROTATE_FAILED_EVENT,
    SWITCH_THRESHOLD_PCT,
    context_crossed,
    switch_task,
    threshold_crossed,
)
from maestro.worktree import (
    TMUX_SESSION,
    _kill_tmux_window,
    create_worktree,
    remove_worktree,
    tmux_window_exists,
    worktree_path_for,
)

_PATHS = Paths.from_env()

REPO                  = _PATHS.repo
WORKSPACES            = _PATHS.workspaces
USAGE_JSON            = _PATHS.usage
HALT_FILE             = REPO / ".orchestrator" / "HALT"
ROADMAP_FILE          = REPO / "docs" / "ROADMAP.md"
VENV_PYTHON           = REPO / ".venv" / "bin" / "python3"
QUESTIONS_DIR         = REPO / ".orchestrator" / "questions"
# `CANARY_DEPLOY` used to live here; `run_canary_deploy` (maestro.merge) now owns the
# path and the absent-is-a-skip guard, shared with `hitl/commands.py`'s identical call.

POLL_INTERVAL = 30
TASK_TIMEOUT  = 10800

# B7: by default the orchestrator does NOT auto-propose on an empty queue. Dan
# runs the proposal test by hand at the right time (richer done-task context →
# better proposals) via handoffs/proposal_system_test.md. Set ORCH_AUTO_PROPOSE=1
# to restore the B6 auto-propose-on-empty behavior.
AUTO_PROPOSE          = os.environ.get("ORCH_AUTO_PROPOSE", "0") == "1"
PROPOSAL_TEST_HANDOFF = "handoffs/proposal_system_test.md"

# ── Stale in_flight reconcile helper (B10) ──

# Zombie-implementer guard (P8B2 hang, 2026-06-20): an implementer can write result.json
# as its near-final step and then hang/idle WITHOUT dropping the DONE/FAILED sentinel and
# (with `remain-on-exit off`) without its tmux window closing. reconcile then re-adopts it
# as "running" forever. After this many consecutive polls in that state we synthesize the
# missing sentinel from result.json so the main loop's completion handlers converge.
RECONCILE_DONE_GRACE_POLLS = 2
_reconcile_zombie_polls: dict[str, int] = {}

# Idle heartbeat: when the loop has nothing in-flight and nothing launchable (every
# runnable task is gated by a resume cooldown / parked / resource mutex), it must keep
# polling so it auto-launches the instant a gate clears — but it would otherwise write no
# journal events, and the watchdog's 20-min journal-silence stall detector (3 strikes →
# HALT) would kill the healthy idle loop. A throttled heartbeat keeps the journal advancing.
IDLE_HEARTBEAT_S      = 600      # ≤ watchdog STALL_WINDOW (20min) with wide margin
_last_idle_heartbeat  = 0.0

# M3 D5: self-update runs from the same idle branch as the heartbeat above, on the same
# cadence — but with its own throttle timestamp; the two checks are independent and must
# not share one, or throttling one would silently throttle the other.
_last_self_update_check = 0.0

# P13 GDT-12 / W12: the weekly improvement check and the code-lever landing check run at
# the same idle moments on the same cadence, each with its own timestamp.
IMPROVEMENT_CHECK_S = IDLE_HEARTBEAT_S
_last_improvement_check = 0.0
_last_landing_check = 0.0


# ── Bodies this module has to own rather than import ──
#
# `_pause_for_usage_limit` (mapped to `maestro.quota`) and `_surface_await_failure`
# (`maestro.parking`) are already extracted, but both ping Dan, and the reference resolves
# `notify_telegram` in ONE flat namespace. `reconcile_in_flight` and
# `poll_awaiting_verifications` reach them on paths the characterisation tests exercise
# unstubbed, pinning the message through *this* module's `notify_telegram` reference —
# importing them would send those pings through a sibling module's reference instead, i.e.
# at a real Telegram send (or, in quota's case, at its `pending()` placeholder).
# Both bodies are copied verbatim; each is one half of a pair whose other half becomes an
# import once the modules can share a single notification seam.
def _pause_for_usage_limit(task_id: str, reset_iso: str, evidence: str,
                           workspace: Path) -> None:
    """Reactive net: pause Maestro until the quota resets instead of failing the
    task. Reuses the same `paused_until` mechanism as the proactive launcher path
    (launch_orchestrator.py); the main loop honors it and the watchdog reschedules
    a resume at the reset time. Notifies Dan once per workspace (dedup sentinel)."""
    sentinel = workspace / "USAGE_LIMIT_PARKED"
    first = not sentinel.exists()
    try:
        sentinel.write_text(reset_iso)
    except Exception:
        pass
    state = read_state()
    cur = state.get("paused_until")  # never shorten an existing later pause
    if not cur or str(cur) < reset_iso:
        state["paused_until"] = reset_iso
    write_state(state)
    append_journal("usage_limit_backoff",
                   f"{task_id} paused_until={reset_iso} ev={evidence[:80]}",
                   session_id=workspace.name)
    if first:
        try:
            local = datetime.strptime(reset_iso, "%Y-%m-%dT%H:%M:%SZ")\
                .replace(tzinfo=timezone.utc).astimezone().strftime("%H:%M")
        except Exception:
            local = reset_iso
        notify_telegram(
            f"⏳ *Maestro* paused until {local} — Claude usage limit hit during "
            f"`{task_id}`. It will resume automatically; no action needed.")


def _surface_await_failure(dan_id_str: str, parked: dict, failing_ids: list[str],
                           reason: str) -> None:
    """An awaiting-verification check resolved to a genuine failure (`reason='gate'`,
    the awaited row landed but is below threshold) or the wait timed out
    (`reason='timeout'`). Revert the entry to `manual-action` so HITL reminders resume
    and Dan can /approve-retry or /reject, and ping him once. Reverting the kind also
    makes this idempotent — the poller no longer matches it, so no per-poll re-ping."""
    task_id = parked.get("task_id", "")
    state   = read_state()
    waiting = state.get("waiting_on_dan", {})
    info    = waiting.get(dan_id_str)
    if not info:
        return
    info["kind"]                = "manual-action"
    info["await_failed_reason"] = reason
    info.pop("reminded_at", None)
    waiting[dan_id_str] = info
    state["waiting_on_dan"] = waiting
    write_state(state)
    append_journal("await_verify_surfaced", f"{task_id} reason={reason} {','.join(failing_ids)}")
    if reason == "timeout":
        notify_telegram(
            f"⏰ *{task_id}* (ID {dan_id_str}): awaited verification "
            f"({', '.join(failing_ids)}) never landed within "
            f"{AWAIT_VERIFY_TIMEOUT_SEC // 3600} h. Check the eval/job, then "
            f"/approve {dan_id_str} to retry or /reject {dan_id_str}."
        )
    else:
        notify_telegram(
            f"⚠ *{task_id}* (ID {dan_id_str}): awaited verification landed but FAILED "
            f"({', '.join(failing_ids)}) — present, below threshold, not still-running. "
            f"Investigate, then /approve {dan_id_str} to retry or /reject {dan_id_str}."
        )


def _notify_proposal_test_due(extra: str = "") -> None:
    """Queue empty + auto-propose off: ping Dan once to run the proposal test at
    the right time, then park the loop so it neither auto-proposes nor repeats
    the ping every poll. Resume by clearing paused_by_user (and
    proposal_test_notified) once new tasks are queued. `extra`: one sentence added
    (P13 GDT-10: a format-1 goals contract is not verified)."""
    state = read_state()
    if state.get("proposal_test_notified"):
        return
    notify_telegram(
        "✅ All queued tasks are done. When the timing is right, run the proposal "
        f"test for context-aware proposals — see `{PROPOSAL_TEST_HANDOFF}`."
        + (f" ({extra}.)" if extra else "")
    )
    append_journal("proposal_test_due", PROPOSAL_TEST_HANDOFF)
    state["paused_by_user"] = True
    state["proposal_test_notified"] = True
    write_state(state)


def generate_proposals() -> None:
    """When queue is empty: brainstorm proposals and send multi-select approval to Dan."""
    print("  [proposals] Queue empty — generating proposals for Dan …")
    notify_telegram("🤔 Queue is empty. Generating improvement proposals…")

    try:
        roadmap_text = ROADMAP_FILE.read_text(encoding="utf-8")[:8000]
        project_text = (REPO / "docs" / "PROJECT.md").read_text(encoding="utf-8")[:4000]
    except Exception as exc:
        notify_telegram(f"⚠ Could not read docs for proposal generation: {exc}")
        return

    try:
        # Roadmap-shaping is the highest-leverage judgment call in the loop — it
        # decides where the project goes and what future tokens get spent on — so
        # it runs on the strong judge, never a cheap model. Low volume
        # (queue-exhaustion only) keeps it cheap.
        raw = _judge_complete(
            system=(
                "You are the autonomous orchestrator for the project described below. "
                "Generate up to 10 concrete improvement proposals based on the current project state. "
                "Each proposal must be grounded in: eval weaknesses, latency hotspots, parked tasks, "
                "or clear TODOs. Output JSON array of objects, each with fields: "
                "title, why_and_expected_impact, est_hours (int), "
                "mode (autonomous|needs-dan), deps (list of task IDs or [])."
                "Sort by expected impact descending. Max 10 items."
            ),
            user=(
                f"ROADMAP (truncated):\n{roadmap_text}\n\nPROJECT STATE:\n{project_text}\n\n"
                "Output JSON array only."
            ),
        )
        m = re.search(r'\[.*\]', raw, re.DOTALL)
        proposals = json.loads(m.group(0)) if m else []
    except Exception as exc:
        notify_telegram(f"⚠ Proposal generation error: {exc}")
        return

    if not proposals:
        notify_telegram("No proposals generated. Parking — ping Dan.")
        return

    lines = ["📋 *Proposed next tasks* — reply with numbers to approve:\n"]
    for i, p in enumerate(proposals[:10], 1):
        mode_icon = "🤖" if p.get("mode") == "autonomous" else "👤"
        lines.append(
            f"{i}. {mode_icon} *{p['title']}* (~{p.get('est_hours','?')}h)\n"
            f"   {p.get('why_and_expected_impact','')[:120]}"
        )
    lines.append("\nReply with comma-separated numbers (e.g. `1,3`) to approve and add to ROADMAP.")

    # Save proposals to .orchestrator/questions/<id>.json for the answer-poller
    q_id = f"proposals-{now_iso().replace(':','').replace('-','')[:15]}"
    QUESTIONS_DIR.mkdir(parents=True, exist_ok=True)
    q_path = QUESTIONS_DIR / f"{q_id}.json"
    q_path.write_text(json.dumps({
        "id": q_id, "type": "phase-approval",
        "proposals": proposals, "created_at": now_iso(),
    }, indent=2))

    notify_telegram("\n".join(lines))
    append_journal("proposals_sent", f"{len(proposals)} proposals sent to Dan, q_id={q_id}")

    # Park: set paused_by_user=True until Dan replies
    state = read_state()
    state["paused_by_user"] = True
    state["pending_proposal_qid"] = q_id
    write_state(state)


def poll_proposal_answer() -> None:
    """Check if Dan answered a pending proposal request; if so, append approved proposals to ROADMAP."""
    state = read_state()
    q_id = state.get("pending_proposal_qid", "")
    if not q_id:
        return

    answer_path = QUESTIONS_DIR / f"{q_id}.answer"
    if not answer_path.exists():
        return

    answer_text = answer_path.read_text().strip()
    q_path = QUESTIONS_DIR / f"{q_id}.json"
    try:
        q_data = json.loads(q_path.read_text())
    except Exception:
        return

    proposals = q_data.get("proposals", [])
    try:
        chosen_indices = [int(x.strip()) - 1 for x in answer_text.split(",") if x.strip().isdigit()]
    except Exception:
        chosen_indices = []

    chosen = [proposals[i] for i in chosen_indices if 0 <= i < len(proposals)]
    if chosen:
        # Append approved proposals to ROADMAP as pending tasks
        roadmap_path = ROADMAP_FILE
        roadmap_text = roadmap_path.read_text(encoding="utf-8")
        new_blocks = []
        # P12 finding D21: an approved proposal's deps are written reduced.
        from maestro import taskgraph

        graph = {str(t["id"]): taskgraph.legacy_deps(t) for t in taskgraph.scan_tasks(roadmap_text)}
        new_ids = [f"P-{p['title'][:20].replace(' ', '_')}" for p in chosen]
        graph.update({i: taskgraph.legacy_deps(p) for i, p in zip(new_ids, chosen)})
        reduced = taskgraph.transitive_reduction(
            graph, known=taskgraph.read_registry_deps(REPO))
        for new_id, p in zip(new_ids, chosen):
            block = (
                f"\n```yaml\n"
                f"id: {new_id}\nshort_desc: \"{p['title']}\"\n"
                f"mode: {p.get('mode','autonomous')}\nstatus: pending\n"
                f"deps: {json.dumps(reduced[new_id])}\n"
                f"```\n"
            )
            new_blocks.append(block)
        roadmap_path.write_text(roadmap_text.rstrip() + "\n" + "".join(new_blocks) + "\n")
        notify_telegram(f"✅ Added {len(chosen)} approved proposals to ROADMAP.")
        append_journal("proposals_approved", f"{[p['title'] for p in chosen]}")

    # Clear the pending state and resume
    state = read_state()
    state["paused_by_user"] = False
    state.pop("pending_proposal_qid", None)
    write_state(state)
    answer_path.unlink(missing_ok=True)


def _resource_set(task: dict) -> set[str]:
    """Normalise a task's optional `resource` field to a set of strings.

    Accepts: absent/None → empty set, a bare string, or a list of strings.
    Mirrors how `deps` is normalised in parse_runnable_tasks (~L730-732).
    """
    raw = task.get("resource")
    if not raw:
        return set()
    if isinstance(raw, str):
        return {raw}
    return {str(r) for r in raw if r}


def _resources_conflict(
    candidate_task: dict,
    held_resources: set[str],
    claimed_this_cycle: set[str],
) -> bool:
    """Return True when launching *candidate_task* would violate the resource mutex.

    A candidate conflicts if any of its resource labels:
    - is already held by a currently in-flight task, OR
    - was claimed by a task launched earlier in this same poll cycle.

    Tasks with no `resource` tag never conflict (empty intersection → False).
    """
    candidate_resources = _resource_set(candidate_task)
    if not candidate_resources:
        return False
    return bool(candidate_resources & (held_resources | claimed_this_cycle))


def launch_async_job(task: dict, resources: dict | None = None) -> bool:
    """`dispatch: async-job` — a long, read-only job (e.g. P8B6's ~5 h eval) that the
    orchestrator launches and self-monitors *itself*, without a babysitting Claude
    implementer session.

    The job's `launch_cmd` is a plain shell command that fires the work in a *detached*
    tmux window and returns immediately (see scripts/p8b6_run_eval.sh). We run it
    synchronously only to detach it, then park the task as `awaiting-verification` —
    exactly the lane a manual-action enters once its async job is kicked off
    (`_park_awaiting_verification`). The poller (`poll_awaiting_verifications`) re-runs the
    task's `await: true` gate each cycle and finalizes when the eval row lands.

    Crucially this consumes NO in_flight slot / session mutex and creates NO worktree, so
    it is invisible to `_timeout_expired` (the 3 h implementer reaper). Its only clock is
    AWAIT_VERIFY_TIMEOUT_SEC (8 h), enforced by the poller. Returns True if the job
    launched and parked; False if it could not be launched (then it is park_failed'd).

    `resources` (P12A W10) are the counted amounts the job was admitted with; they are
    recorded on the parked entry, so its host hold is that snapshot for as long as it runs."""
    task_id = task["id"]
    cmd = (task.get("launch_cmd") or "").strip()
    if not cmd:
        append_journal("async_job_no_cmd", f"{task_id} missing launch_cmd")
        park_failed(task_id, "`dispatch: async-job` task has no `launch_cmd`")
        return False

    rcmd = re.sub(r"^(python3?)\b", str(VENV_PYTHON), cmd)
    try:
        # The launch_cmd must DETACH (tmux new-window) and return promptly; SMOKE_TIMEOUT
        # bounds only the detach/setup (e.g. importing FT vectors), never the eval itself.
        r = subprocess.run(rcmd, shell=True, cwd=str(REPO),
                           capture_output=True, text=True, timeout=SMOKE_TIMEOUT)
    except subprocess.TimeoutExpired:
        append_journal("async_job_launch_timeout", f"{task_id} launch cmd exceeded {SMOKE_TIMEOUT}s")
        park_failed(task_id, f"async-job launch cmd did not detach within {SMOKE_TIMEOUT}s")
        return False
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "")[-300:]
        append_journal("async_job_launch_failed", f"{task_id} {err[:160]}")
        park_failed(task_id, f"async-job launch failed (exit {r.returncode}): {err[:200]}")
        return False

    # Park `awaiting-verification` directly — no manual-action middle step (Dan performs
    # nothing for a read-only eval). The awaited checks are the task's `await: true` autos.
    verifs    = task.get("verifications") or []
    await_ids = [str(v.get("id", "")) for v in verifs
                 if v.get("kind") == "auto" and v.get("await")]
    state   = read_state()
    dan_id  = state.get("dan_id_counter", 0) + 1
    state["dan_id_counter"] = dan_id
    waiting = state.get("waiting_on_dan", {})
    waiting[str(dan_id)] = {
        "task_id":          task_id,
        "session_id":       "",            # no implementer session / workspace
        "branch":           "",
        "worktree":         "",
        "parked_at":        now_iso(),
        "kind":             "awaiting-verification",
        "await_started_at": now_iso(),
        "await_checks":     await_ids,
        "summary":          f"async job launched: {cmd[:160]}",
    }
    if resources is not None:
        waiting[str(dan_id)]["resources"] = dict(resources)
    state["waiting_on_dan"] = waiting
    write_state(state)
    append_journal("async_job_launched", f"{task_id} dan_id={dan_id} cmd={cmd[:120]}")
    notify_telegram(
        f"🚀 *{task_id}*: launched the long read-only job myself (detached) — `{cmd}`.\n"
        f"No action needed: I'll re-check {', '.join(await_ids) or 'its eval gate'} every "
        f"poll and finalize automatically when the result lands — or surface it if it "
        f"fails or runs past {AWAIT_VERIFY_TIMEOUT_SEC // 3600} h."
    )
    return True


def poll_awaiting_verifications(in_flight: list) -> None:
    """Main-loop hook: re-run the gate for tasks parked as `awaiting-verification`.
    Pass → graduate; awaited row present-but-failing → surface; still absent → keep
    waiting unless the bound elapsed (then surface as a timeout)."""
    from maestro.parking import poll_manual_observations, resume_awaited_verification

    poll_manual_observations(in_flight)
    state   = read_state()
    waiting = state.get("waiting_on_dan", {})
    if not waiting:
        return
    for dan_id_str, parked in list(waiting.items()):
        if parked.get("kind") != "awaiting-verification":
            continue
        task_id = parked.get("task_id", "")
        verifs  = (get_task_by_id(task_id) or {}).get("verifications") or []
        pending, hard = _classify_verifications(
            [v for v in verifs if v.get('kind') != 'dan' and v.get('stage') != 'post_merge'], REPO)
        if not pending and not hard:
            append_journal("await_verify_passed", f"{task_id} dan_id={dan_id_str}")
            notify_telegram(f"✅ {task_id}: awaited verification landed and passed — finalizing.")
            if parked.get('manual_contract') or any(
                v.get('kind') == 'dan' or v.get('stage') == 'post_merge' for v in verifs
            ) or (get_task_by_id(task_id) or {}).get('dispatch') == 'manual':
                resume_awaited_verification(dan_id_str, parked, in_flight)
            else:
                _graduate_manual_action(task_id, dan_id_str, parked.get("session_id", ""), in_flight)
        elif hard:
            _surface_await_failure(dan_id_str, parked, hard, reason="gate")
        elif _await_timed_out(parked.get("await_started_at", "")):
            _surface_await_failure(dan_id_str, parked, pending, reason="timeout")
        # else: still absent within the bound — keep waiting silently.


def _timeout_expired(entry: dict, launch_times: dict) -> bool:
    """True if this in_flight entry has exceeded TASK_TIMEOUT with no DONE sentinel.

    Bug #1 (2026-06-26): on FIRST detection this also writes the FAILED sentinel AND
    kills the orphaned implementer tmux window, so its `claude -p` child stops burning
    the shared Claude quota and can't later write DONE/FAILED into an unmonitored
    workspace. Legacy calls retain their one-shot sentinel guard. Native reports
    are worker-writable, so they cannot suppress a controller stop at the ceiling.
    """
    sid       = entry["session_id"]
    workspace = WORKSPACES / sid
    from maestro.backends import control_files, git_staging
    retained_timeout = git_staging.publication_timeout(workspace)
    elapsed   = time.time() - launch_times.get(sid, time.time())
    if not retained_timeout and elapsed <= TASK_TIMEOUT:
        return False
    native_pending = git_staging.publication_state(workspace) not in (None, 'promoted')
    if not retained_timeout and (workspace / "DONE").exists() and not native_pending:
        return False
    if native_pending or retained_timeout:
        reason = git_staging.publication_timeout(
            workspace, reason=f"timeout after {TASK_TIMEOUT // 3600}h")
        # Reports are untrusted. Persist the controller outcome first; refusing
        # an alias must neither suppress the stop nor let DONE win settlement.
        try:
            try:
                previous = control_files.read(workspace / 'FAILED').decode('utf-8')
            except FileNotFoundError:
                previous = None
            if previous != reason:
                control_files.write(workspace / 'FAILED', reason)
        except (OSError, ValueError) as error:
            append_journal('timeout_report_refused', str(error), session_id=sid)
        win = entry.get("window", "")
        if win and tmux_window_exists(win):
            _kill_tmux_window(win)
            append_journal("timeout_window_killed",
                           f"{entry['task_id']} window={win}", session_id=sid)
    elif not (workspace / "FAILED").exists():
        (workspace / "FAILED").write_text(f"timeout after {TASK_TIMEOUT // 3600}h")
        win = entry.get("window", "")
        if win:
            _kill_tmux_window(win)
            append_journal("timeout_window_killed",
                           f"{entry['task_id']} window={win}", session_id=sid)
    return True


# ── Backend switching (M2 — DESIGN.md §7, D2/D3) ──
#
# Two of this loop's waiting paths become switching paths, but only when another backend
# can genuinely take the work: the proactive usage threshold in `main` and the reactive
# usage-limit net in `reconcile_in_flight`. Everything below is written so that "no, it
# cannot" is an ordinary answer returned as `None`/`0` rather than an exception — a switch
# that cannot happen must leave the pre-existing throttle/pause behaviour byte-for-byte
# as it was, because that behaviour is what keeps the loop safe when there is nowhere
# else to go.


def _available_backends() -> tuple[str, ...] | None:
    """The backends whose CLI is installed, or `None` when that could not be measured.

    `None` is not "none of them": `maestro.roles` reads it as "nobody looked, assume
    installed", which is the right default for a probe that failed. Discovery is cached
    inside the registry, so this costs one `--version` probe per binary per process — and
    it is only ever reached on a path that was otherwise about to stop launching work.
    """
    try:
        return tuple(
            name for name in known_backends() if backend_binary(name) is not None
        )
    except Exception:
        return None


# ── P08: launch ordering and the host reservation ledger ──
#
# When each runnable task became continuously eligible, so the ranking can age it.
_ELIGIBILITY = scheduling.EligibilityClock()


def _is_agent_entry(entry: dict) -> bool:
    """Whether an in_flight entry occupies an agent slot. A `kind: script` task does not."""
    return entry.get("role") != "script"


def _agent_slots_free(in_flight: list, cap: int) -> int:
    return cap - sum(1 for e in in_flight if _is_agent_entry(e))


def _task_priority(task: dict) -> int:
    try:
        return int(task.get("priority") or 0)
    except (TypeError, ValueError):
        return 0


def _order_runnable(tasks: list, now: float) -> list:
    """`tasks` in `scheduling.rank` order: operator priority, aging, then leverage.

    Resource fit is decided by the launch loop's own gates, so every candidate "fits"
    here. Ties keep ROADMAP order (a stable sort on the rank key minus its id term), so
    a roadmap with no priorities and no dependents launches exactly as before.
    """
    if not tasks:
        return []
    try:
        roadmap_tasks = _load_roadmap_tasks()
    except Exception:
        roadmap_tasks = []
    since = _ELIGIBILITY.observe([t["id"] for t in tasks], now)
    candidates = [
        scheduling.Candidate(
            candidate_id=str(t["id"]),
            operator_priority=_task_priority(t),
            eligible_since=since.get(str(t["id"]), now),
            leverage=scheduling.legacy_task_leverage(t["id"], roadmap_tasks),
        )
        for t in tasks
    ]
    engine = scheduling.AdmissionEngine(scheduling.HostCapacity())
    keys = {e.candidate_id: e.sort_key()[:-1] for e in scheduling.rank(candidates, engine, now=now)}
    return sorted(tasks, key=lambda t: keys[str(t["id"])])


def _host_pool_id(backend: str) -> str:
    return f"backend:{backend}"


def _host_pool_limit() -> int | None:
    """`thresholds.host_pool_concurrency`; 0 (the default) records without bounding."""
    limit = _config.threshold("host_pool_concurrency", 0, cast=int)
    return limit if limit and limit > 0 else None


def _sync_host_ledger(in_flight: list, counted: dict | None = None) -> None:
    """Make this project's host reservations exactly its live agent entries, plus the
    counted cpu / ram_mb / vram_mb amounts of `counted` (`run_id -> amounts`, P10B).

    Coordination metadata only: a ledger failure is journalled and never stops the loop.
    Launches still fail closed on it through `_reserve_host_slot` when a limit is set.
    """
    live = {
        e["session_id"]: _host_pool_id(_entry_backend(e))
        for e in in_flight
        if _is_agent_entry(e) and e.get("session_id")
    }
    try:
        HostReservationLedger().sync_project(
            str(REPO), live, writer_pid=os.getpid(), counted=counted or {}
        )
    except Exception as exc:
        append_journal("host_ledger_error", f"sync: {str(exc)[:160]}")


def _reserve_host_slot(task_id: str, sid: str, backend: str):
    """Reserve one host pool slot for `sid` before launch. The reservation, or None.

    None means "do not launch this poll": the pool is saturated, or the ledger is broken
    and a limit is configured. With no limit a broken ledger is journalled and the launch
    proceeds, returning a falsy-but-not-None marker.
    """
    limit = _host_pool_limit()
    pool = _host_pool_id(backend)
    try:
        result = HostReservationLedger().reserve(
            pool, project_id=str(REPO), run_id=sid, writer_pid=os.getpid(), limit=limit
        )
    except Exception as exc:
        append_journal("host_ledger_error", f"reserve {task_id}: {str(exc)[:160]}")
        return None if limit is not None else False
    if not result.admission.admitted:
        append_journal("launch_skipped_host_pool", f"{task_id} waiting — {result.admission.detail}")
        return None
    return result.reservation


def _release_host_slot(reservation) -> None:
    if not reservation:
        return
    try:
        HostReservationLedger().release(reservation.reservation_id)
    except Exception as exc:
        append_journal("host_ledger_error", f"release: {str(exc)[:160]}")


# ── P10B: host resource admission (cpu / ram_mb / vram_mb) ──
#
# A task's `resources:` block is admitted against this host's capacity less every other
# project's live counted reservations, with this project's in-flight work taken first.
# The in-process check orders and explains; `reserve_counted` under the ledger lock is
# what makes two projects unable to claim the same RAM at once.

_HOST_CAPACITY: "scheduling.HostCapacity | None" = None


def _host_capacity() -> scheduling.HostCapacity:
    """This host's CPU / RAM / VRAM, detected once per process — hardware does not move
    between polls, and detection shells out to `nvidia-smi`."""
    global _HOST_CAPACITY
    if _HOST_CAPACITY is None:
        _HOST_CAPACITY = scheduling.HostCapacity.detect()
    return _HOST_CAPACITY


def _async_run_id(task_id: str) -> str:
    return f"async-{task_id}"


def _live_counted(in_flight: list, launch_state: dict) -> dict:
    """`run_id -> amounts` for everything of this project that holds host resources.

    In-flight entries carry the amounts they were admitted with. A self-monitored async
    job lives in `waiting_on_dan`, not `in_flight`, and holds its request until Dan
    resolves it — the same rule B8 applies to its `resource:` label.

    That request is the `resources` snapshot `launch_async_job` recorded on the entry
    (P12A W10), used whenever the key is present — even `{}` — so editing or removing the
    ROADMAP block mid-run leaves the hold unchanged. Only an entry written before W10
    (no `resources` key) falls back to re-deriving the request from the ROADMAP.
    """
    counted = {
        e["session_id"]: dict(e["resources"])
        for e in in_flight
        if e.get("session_id") and e.get("resources")
    }
    for pending in (launch_state.get("waiting_on_dan") or {}).values():
        if pending.get("kind") != "awaiting-verification":
            continue
        if "resources" in pending:
            amounts = dict(pending["resources"] or {})
        else:
            try:
                task = get_task_by_id(pending.get("task_id", ""))
                amounts = dict(scheduling.ResourceRequest.from_task(task).counted) if task else {}
            except Exception:
                amounts = {}
        if amounts:
            counted[_async_run_id(pending["task_id"])] = amounts
    return counted


def _admission_round(counted_live: dict, now: float) -> scheduling.AdmissionRound:
    """This poll's admission: capacity less other projects, this project's work taken."""
    try:
        others = HostReservationLedger().held_amounts(exclude_project=str(REPO))
    except Exception as exc:
        # `reserve_counted` still fails closed on a broken ledger; this only orders.
        append_journal("host_ledger_error", f"held_amounts: {str(exc)[:160]}")
        others = {}
    # Other projects' holdings are subtracted as `held` rather than off the limits, so a
    # refusal still reads against the host's real capacity ("2000 of 8000 free").
    engine = scheduling.AdmissionEngine(_host_capacity(), held=others)
    for amounts in counted_live.values():
        engine.take(scheduling.ResourceRequest(counted=amounts))
    return scheduling.AdmissionRound(engine, now=now)


def _reserve_host_resources(task_id: str, run_id: str, amounts: dict):
    """Hold `amounts` in the host ledger before launch. `(reservation, admission)`.

    Fails closed: a task that declared resources does not launch on a broken ledger,
    because launching it unrecorded is exactly how two projects oversubscribe the host.
    """
    try:
        result = HostReservationLedger().reserve_counted(
            amounts, capacity=_host_capacity().limits, project_id=str(REPO),
            run_id=run_id, writer_pid=os.getpid(),
        )
    except Exception as exc:
        append_journal("host_ledger_error", f"reserve_counted {task_id}: {str(exc)[:160]}")
        return None, scheduling.Admission(
            False, reason="host_ledger_error", detail=str(exc)[:160]
        )
    return result.reservation, result.admission


def _record_admission_waits(waits: dict) -> None:
    """Persist why tasks are waiting on admission, for `maestro status` / `explain`.

    Written only when the set changes; `since` survives while the reason is unchanged,
    and the journal hears about a task once per new reason rather than once per poll.
    """
    state = read_state()
    previous = state.get("admission_waiting") or {}
    current = {}
    for task_id, answer in waits.items():
        before = previous.get(task_id) or {}
        same = before.get("reason") == answer["reason"]
        current[task_id] = {**answer, "since": before.get("since") if same else now_iso()}
        if not same:
            append_journal(
                "launch_waiting_admission",
                f"{task_id} waiting — {answer['reason']}: {answer['detail']}",
            )
    if current != previous:
        state["admission_waiting"] = current
        write_state(state)


#: The host dimensions a graph node may hold in the shared ledger. A node's request can
#: also name an agent slot or an inference pool; those are the router's and the runner's
#: questions, and the host ledger records neither.
_HOST_DIMENSIONS = (scheduling.CPU, scheduling.RAM_MB, scheduling.VRAM_MB)


class _GraphHostAdmission:
    """P12A W8: host admission for graph nodes, over the legacy loop's own helpers.

    One `_admission_round` per poll across every live run of this project, then one
    `HostReservationLedger` row per admitted node — the same two steps `main` takes for a
    legacy task, which is what stops two projects (or the two loops) reserving the same
    RAM twice. `_admission_round`, `_reserve_host_resources` and `_release_host_slot` are
    called, never copied.

    A hold is keyed by the attempt the node is *about* to claim: `advance` consults
    admission before any claim exists, and `attempt_id(run_id, node_id, index + 1)` is
    exactly the id `claim` will mint next for that node. That key is what lets
    `on_attempt_finished` give the amounts back knowing only the attempt that ended.
    """

    def __init__(self) -> None:
        #: `attempt_id -> {"reservation", "amounts"}` for every hold this process owns.
        self.held: dict = {}
        self.round: "scheduling.AdmissionRound | None" = None
        self.now = 0.0
        self.mutex_owners: dict[str, str] = {}

    def begin(self, store, now: float, *, run_labels=None) -> None:
        """Open this poll: drop holds whose attempt is over, then build the round."""
        self.now = now
        self._sweep(store)
        # The loop already owns these frozen run labels, augmented with current
        # polish requirements. Restore selected owners from graph_runs, even
        # between an operator retry and claim. Only idle inherited allocations
        # may be reconciled when requirements change into conflicting bundles.
        labels = run_labels or {}
        previous = {label: owner for label, owner in self.mutex_owners.items()
                    if label in labels.get(owner, set())}
        contenders = {}
        runs = {}
        for row in store.conn.execute(
                "SELECT run_id, task_id, started_at FROM task_runs WHERE status='running'"
                " ORDER BY started_at, run_id"):
            run_id = row['run_id']
            task = get_task_by_id(row['task_id']) or {}
            frozen = (_resource_set(task) if task.get('polish_items')
                      else labels.get(run_id, set()))
            active = store.conn.execute(
                "SELECT 1 FROM attempts WHERE run_id=? AND status IN ('claimed', 'running')",
                (run_id,)).fetchone() is not None
            runs[run_id] = (frozen, active, row['started_at'] or '')
            for label in labels.get(run_id, set()):
                contenders.setdefault(label, []).append(
                    (label not in frozen, not active, row['started_at'] or '', run_id))
        # Active attempts and raw/frozen requirements cannot yield their labels.
        # Retain their selected owners; legacy state uses the same frozen/active
        # precedence as before. Idle inherited claims must wait for these owners.
        self.mutex_owners = {
            label: owner for label, owner in previous.items()
            if owner in runs and (runs[owner][1] or label in runs[owner][0])}
        for label, candidates in contenders.items():
            protected = [run for run in candidates if not run[0] or not run[1]]
            if protected:
                self.mutex_owners.setdefault(label, min(protected)[-1])
        # A compatible selected idle owner keeps its retry priority. Otherwise
        # assign the whole available bundle in stable age order: retaining x=A
        # and y=B after both need {x,y} would block both forever, including restart.
        selected = set(previous.values())
        compatible = {run_id for run_id in selected
                      if all(previous.get(label, run_id) == run_id
                             for label in labels.get(run_id, set()))}
        for run_id in sorted(runs, key=lambda rid: (
                rid not in compatible, rid not in selected, runs[rid][2], rid)):
            required = labels.get(run_id, set())
            if any(self.mutex_owners.get(label, run_id) != run_id for label in required):
                continue
            for label in required:
                self.mutex_owners.setdefault(label, run_id)
        self.round = _admission_round(
            {key: dict(hold["amounts"]) for key, hold in self.held.items()}, now
        )

    def _sweep(self, store) -> None:
        """Release a hold whose attempt never happened, or is no longer live.

        `on_attempt_finished` covers the two ordinary endings (an ingested result, a
        failed launch). This covers the ones a hold can outlive without one: a node
        admitted here and then left unclaimed by routing (a `wait`), a claim the lease
        refused, and an attempt a later reconcile decided.
        """
        for key in list(self.held):
            try:
                row = store.conn.execute(
                    "SELECT status FROM attempts WHERE attempt_id = ?", (key,)
                ).fetchone()
            except Exception:  # noqa: BLE001 — an unreadable store is not a reason to leak
                continue
            if row is None or row["status"] not in ("claimed", "running"):
                self._release(key)

    def admit(self, runner, node):
        """`routing.admit`: `None` when the node may claim, else the refusal to record."""
        from maestro.control.models import attempt_id as make_attempt_id

        task_id = runner.revision.task_id
        task = _graph_polish_task(get_task_by_id(task_id) or {})
        held = {label for label, owner in self.mutex_owners.items() if owner != runner.run_id}
        if ((_resource_set(task) & held and task.get('polish_items'))
                or not _graph_polish_permitted(task, held_resources=held)):
            return scheduling.Admission(False, reason='polish_source_ineligible',
                detail='a polish source is held, unaccepted or otherwise ineligible')
        request = scheduling.ResourceRequest.from_node(node, task_id=task_id)
        if task.get('polish_items'):
            # A source can acquire stricter requirements after this revision
            # was compiled. Feed them into this same host round and ledger.
            current = scheduling.ResourceRequest.from_task(task)
            request = scheduling.ResourceRequest(
                counted={**request.counted, **{name: max(amount, request.counted.get(name, 0))
                         for name, amount in current.counted.items()}},
                exclusive=request.exclusive)
        amounts = {
            name: amount
            for name, amount in request.counted.items()
            if name in _HOST_DIMENSIONS
        }
        if not amounts or self.round is None:
            # A node that asks for no counted host amounts skips the ledger entirely, so
            # a stock graph — where no compiled fragment names cpu / ram_mb / vram_mb —
            # dispatches exactly as it did before W8.
            return None
        oversized = scheduling.exceeds_capacity(
            scheduling.ResourceRequest(counted=amounts), _host_capacity()
        )
        if oversized is not None:
            # W9: no amount of waiting fits it; `advance` fails the node instead.
            return scheduling.Admission(
                False,
                reason=scheduling.EXCEEDS_CAPACITY,
                detail=oversized.split(": ", 1)[1],
            )
        key = make_attempt_id(
            runner.run_id, node.node_id, runner.attempt_index(node.node_id) + 1
        )
        if key in self.held:
            return None  # already holding for this attempt (a re-entered poll)
        candidate = scheduling.Candidate(
            candidate_id=key,
            request=request,
            eligible_since=_ELIGIBILITY.since.get(key, self.now),
        )
        answer = self.round.admit(candidate)
        reservation = None
        if answer.admitted:
            reservation, answer = _reserve_host_resources(task_id, key, amounts)
        if not answer.admitted:
            # The in-process round said yes and the ledger said no: give the amounts back
            # inside this poll, so the next node is ranked against what is really free.
            self.round.withdraw(candidate)
            return answer
        self.held[key] = {"reservation": reservation, "amounts": amounts}
        return None

    def on_attempt_finished(self, runner, attempt_id: str) -> None:
        """`routing.on_attempt_finished`: the attempt ended, so its hold ends with it."""
        self._release(attempt_id)

    def _release(self, key: str) -> None:
        hold = self.held.pop(key, None)
        if hold is not None:
            _release_host_slot(hold["reservation"])


def _launch_backend() -> str:
    """The backend a fresh `launch_implementer` call runs on.

    **A6 (2026-09-02) unified this with `implementer._implementer_backend` and
    `agentcall.resolve_call`.** Until then this hard-short-circuited on
    `operator_backend() or backend_for(ROLE_IMPLEMENTER)`, agreeing with the other two
    only because none of the three was ever fed `exhausted=`/`available=` — the moment
    one gained real data the three could observably diverge, and this function only
    *records* its answer on the `in_flight` entry, so a divergence would make every
    switch decision taken from that entry about the wrong agent (see
    `docs/PROGRESS.md`'s D3 entry for the pre-A6 history). Now all three call
    `roles.resolve` (or its `backend_for` wrapper) the same way: the operator's pin as
    `preferred=`, not a bypass of resolution, and `quota.exhausted_backends()` — the
    timed per-backend record A6 adds — as `exhausted=`, so a pinned-but-still-exhausted
    backend is skipped and the fallback chain is walked instead of failing onto it again.

    Still no binary probe and no subprocess: `exhausted_backends()` is one more small
    state-document read, exactly like `operator_backend()` already was, on a path that
    is about to start an agent. `""` when resolution itself breaks — an unknown backend
    is better than a wrong one.
    """
    try:
        return backend_for(
            ROLE_IMPLEMENTER, preferred=operator_backend() or None,
            exhausted=exhausted_backends(),
        )
    except Exception:
        return ""


def _launch_model(task: dict, backend: str) -> str:
    """The model a fresh `launch_implementer(task, ..., backend)` will run under.

    Recorded on the `in_flight` entry beside `backend`, and for the same reason: D4's
    context ceiling is per *model*, so a record that named the wrong one would measure a
    healthy conversation against a stranger's limit. Since C1 the role table alone is not
    the answer — a task's own `model:` beats it — so this goes through the very function
    the launch goes through (`implementer.resolve_launch_model`) rather than asking
    `roles.model_for` a second, differently-shaped question.

    `""` when resolution breaks, and `""` means *nothing was recorded*, never a guess:
    `_context_rotations` then falls back to the role table exactly as it does for an
    entry written before this key existed. Same degradation as `_launch_backend`.
    """
    try:
        return resolve_launch_model(task, backend)[0] or ""
    except Exception:
        return ""


def _entry_backend(entry: dict) -> str:
    """The backend an `in_flight` entry is running on.

    Entries written before M2 (and any an operator hand-edits) carry no `backend` key, so
    the configured default for the role stands in.
    """
    return normalise_name(entry.get("backend")) or _launch_backend()


def _entry_handle(entry: dict) -> Handle | None:
    """A handle naming `entry`'s live agent session, or `None` when it names none.

    This is what lets a driver be asked about *this* implementer rather than about the
    account. Two fields are deliberately not what a caller might expect:

    * `native_id` is `None`. An `in_flight` entry records maestro's own `session_id` and
      never the backend's native one, so the driver recovers its id from the sidecar its
      own launch wrote into the workspace — the same recovery `resume` already performs
      on a handle rebuilt from state.
    * `workspace` is derived, not read off the entry, because the entry has no such key.
      `WORKSPACES / session_id` is the convention every launch and every switch follows.

    A `Usage` sampled through this handle comes back stamped with the same `session_id`,
    which is the string `_attributed_to` compares against the entry.
    """
    session_id = str(entry.get("session_id") or "").strip()
    if not session_id:
        return None
    return Handle(
        backend=_entry_backend(entry),
        session_id=session_id,
        native_id=None,
        workspace=WORKSPACES / session_id,
        window=str(entry.get("window") or ""),
    )


def _reconcile_exit_verdict(entry: dict, log_tail: str) -> ExitVerdict | None:
    """`entry`'s own driver's read of a vanished window's log, or `None` when the
    driver cannot be resolved.

    A1 (readiness queue): `reconcile_in_flight` used to scan `log_tail` with
    `quota._LIMIT_RE` — the Claude CLI's own wording — no matter which backend the entry
    ran on, so a codex implementer that died on quota was never classified as such. Every
    driver already implements `parse_exit`; this resolves the one `_entry_backend` says
    the entry ran on (`get_backend` is capability-gated elsewhere in this module and
    imports no driver eagerly) and asks it instead.

    The window is already gone by the time this runs, so there is no captured return code
    to hand the driver — `launch_implementer` never captured one either (see
    `ClaudeBackend`'s "Honesty note about `parse_exit`": the reference discards the return
    code entirely). `1` stands in for "not a clean exit", the one fact this branch
    actually knows: reconcile only reaches here when no DONE/PAUSED/FAILED sentinel and no
    live window explain what happened. Every driver's quota reading is unaffected by that
    choice — `ClaudeBackend.parse_exit` checks its wording ahead of the return code, and
    `CodexBackend.parse_exit`'s explicit signal doesn't consult it either — the synthetic
    code only satisfies `CodexBackend`'s `rc != 0` gate for its non-explicit ("token
    window already at 100 %") reading, matching what this branch already assumed by being
    here at all.
    """
    try:
        driver = get_backend(_entry_backend(entry))
        return driver.parse_exit(1, log_tail)
    except Exception:
        return None


def _backends_tried(entry: dict) -> tuple[str, ...]:
    """Backends this task has already been switched away from, oldest first.

    A switch is not free — it stops an agent and starts another — so a task must not be
    handed back to a backend that already ran out of room for it. Without this the two
    triggers ping-pong: the loop's `five_pct` reading is about the backend it launched
    on, a driver that cannot sample its own usage answers "no reading" (G6), and
    `_under_usage_pressure` then reads that as pressure on *every* backend, switching the
    same task back and forth once per poll and losing its progress each time.

    The consequence is deliberate and conservative: once a task has been round the
    fallback chain it stops switching and takes the ordinary pause, even if the first
    backend's window has since reset. Waiting is always safe; relaunching in a loop is
    not.
    """
    raw = entry.get("backends_tried")
    if not isinstance(raw, (list, tuple)):
        return ()
    seen: list[str] = []
    for value in raw:
        name = normalise_name(value)
        if name and name not in seen:
            seen.append(name)
    return tuple(seen)


def _handover_ready(entry: dict) -> bool:
    """True when this entry is one a switch could actually move.

    Two preconditions, both load-bearing:

    * it must be an implementer — a script task runs no agent, so it has no session to
      hand to another backend;
    * its worktree must still be on disk. Preserving the uncommitted work in that
      worktree is the whole point of D3; with the worktree gone there is nothing to hand
      over and the "switch" would quietly become a fresh start elsewhere, which is
      strictly worse than the wait it replaced.
    """
    if normalise_role(entry.get("role")) != ROLE_IMPLEMENTER:
        return False
    worktree = str(entry.get("worktree") or "")
    return bool(worktree) and Path(worktree).is_dir()


def _sampled_usage(backend: str, cache: dict | None = None, *,
                   handle: Handle | None = None) -> Usage | None:
    """This poll's usage reading for `backend`, sampled through its driver at most once.

    `handle` selects *which reading*, and the two are different questions (see
    `AgentBackend.usage`): with no handle this is the account-level sample — quota
    windows, attributable to nobody — and with one it is that session's own context,
    carrying `Usage.session_id` so the D4 rotation can tell it is the implementer's.

    `cache`, when given, is a per-poll memo shared by every caller that needs a reading
    during the same poll — the switch-pressure check below, the `usage.json` write
    (`_write_usage_sample`) and the rotation check all go through here. **The memo is
    keyed on the backend *and* the session**, because one slot per backend can no longer
    hold the answer to both questions: serving a session's context reading out of the
    account slot is precisely how a rotation would end up acting on somebody else's
    numbers.

    That widening does not multiply the expensive call. Only the account-level slot may
    cost a short-lived subprocess — `CodexBackend.usage()` spawns a `codex app-server`
    to answer it — and that slot is still one per backend per poll, whatever the number
    of in-flight tasks. The per-session slots are reads of a record the agent is already
    writing, which is why the protocol requires the handled form never to fall back to
    the subprocess sampler.

    Capability-gated, never name-gated: a driver that declares no usage telemetry, that
    cannot be built, or that raises, answers `None` — the same "not measurable" `None`
    a driver returns deliberately (G6).
    """
    key = (backend, handle.session_id if handle is not None else "")
    if cache is not None and key in cache:
        return cache[key]
    sample = None
    try:
        driver = get_backend(backend)
        if driver.capabilities().usage_telemetry:
            sample = driver.usage(handle) if handle is not None else driver.usage()
    except Exception:
        sample = None
    if cache is not None:
        cache[key] = sample
    return sample


def _under_usage_pressure(backend: str, five_pct: float, cache: dict | None = None) -> bool:
    """True when `backend` is the one this loop's usage reading is about.

    `get_effective_cap` returns a single number that says nothing about which backend
    produced it, so a backend that can sample its own usage is asked directly: a task
    running somewhere with quota to spare must not be moved onto the backend that is
    running out. Capability-gated, never name-gated. A driver that declares no usage
    telemetry, or that has no sample to give, falls back to this loop's reading — "not
    measurable" is never evidence of no pressure (G6).
    """
    sample = _sampled_usage(backend, cache)
    if sample is None:
        return five_pct >= SWITCH_THRESHOLD_PCT
    return threshold_crossed(sample)


def _most_pressured(samples: list[Usage]) -> Usage | None:
    """The sample under the most quota pressure, or `None` when `samples` is empty.

    Window-agnostic (G5), like `switch.threshold_crossed`: compares `Usage.max_used_pct`
    rather than a named window, since an account whose only window is weekly has no
    five-hour reading at all. A sample whose percentages are all unmeasurable (G6) has
    `max_used_pct() is None` — it never wins the comparison by accident, and it never
    loses it to a phantom `0` either; it simply cannot outrank a sample that *does* carry
    a number. When every sample is unmeasurable, the first one is returned so a single
    reporting backend still gets its (empty) reading written, matching this function's
    behaviour before there was more than one backend to choose between.
    """
    best: Usage | None = None
    best_pct: float | None = None
    for sample in samples:
        pct = sample.max_used_pct()
        if pct is None:
            continue
        if best_pct is None or pct > best_pct:
            best, best_pct = sample, pct
    if best is not None:
        return best
    return samples[0] if samples else None


def _write_usage_sample(in_flight: list, cache: dict) -> None:
    """Persist this poll's usage reading to `.orchestrator/usage.json` (A2).

    Before this, the file was written only by the operator's interactive Claude Code
    statusline hook, so `quota.get_effective_cap` saw a missing file — and therefore
    `five_pct = 0` — the moment no such session was attached to this project. On an
    unattended loop that is always, so neither the concurrency throttle nor the
    `PAUSE_PCT` pause could ever fire. This makes the loop sample its own usage instead
    of depending on that hook, without disabling it — a project where both write the
    file keeps working, since either write is just the freshest one on disk.

    Every backend currently running work is sampled once — through `cache`, the same
    per-poll memo `_under_usage_pressure` already populated for the switch decision, so
    this never asks a driver twice for the same backend in one poll. A backend with
    nothing to report this poll (G6: `None` is not zero) is simply excluded from the
    choice below rather than overwriting real data with an empty document.

    `usage.json` is one document, but `get_effective_cap` treats whatever `five_hour`
    it finds there as the *global* cap and pause for every backend — genuinely
    concurrent backends racing to write it every poll means whichever one happened to
    land last could silently hide the other one's exhaustion. `_most_pressured` picks
    the more-exhausted sample instead of the most-recently-sampled one, so the number
    `quota.py` reads is always the conservative one: it can throttle early, never late.

    A render or write failure is intentionally *not* swallowed blanket-wide: an
    `OSError` (an unwritable `.orchestrator/`, a full disk) is the one failure mode this
    is expected to survive, and it prints a breadcrumb rather than vanishing silently —
    matching every other throttle/switch outcome this function's caller reports. Any
    other exception (a bug in `to_usage_json`, e.g.) is deliberately loud: swallowing it
    would hide a real programming error behind "usage.json didn't update".
    """
    seen: list[str] = []
    for entry in in_flight:
        name = _entry_backend(entry)
        if name and name not in seen:
            seen.append(name)
    samples = [s for s in (_sampled_usage(name, cache) for name in seen) if s is not None]
    sample = _most_pressured(samples)
    if sample is None:
        return
    document = to_usage_json(sample)
    try:
        tmp = USAGE_JSON.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
        tmp.rename(USAGE_JSON)
    except OSError as exc:
        print(f"  [usage] failed to write usage.json: {exc}")


def _rebase_launch_time(old_session_id: str, new_entry: dict,
                        launch_times: dict | None) -> None:
    """Move the launch clock from the session that was stopped to the one replacing it.

    Shared by both relaunching paths — a backend switch and D4's context rotation —
    because both start a *new* agent from scratch in the same worktree. `TASK_TIMEOUT`
    must therefore be measured from now; leaving the old epoch in place would time the
    fresh agent out on the stopped one's clock, and in the rotation case that clock is
    long by definition, since running out of context is what took so long.
    """
    new_sid = new_entry.get("session_id", "")
    if launch_times is None or not new_sid:
        return
    launch_times.pop(old_session_id, None)
    launch_times[new_sid] = time.time()
    _persist_launch_time(new_sid, launch_times[new_sid])


def _switch_instead_of_waiting(entry: dict, reason: str,
                               launch_times: dict | None = None) -> dict | None:
    """Hand one task to a fallback backend rather than wait for this one to recover.

    Returns the replacement `in_flight` entry, or `None` when nothing moved — no
    fallback, nothing worth handing over, or the switch itself failed. `None` is the
    caller's signal to keep doing exactly what it did before M2. The relaunch happens in
    the *same* worktree (`maestro.switch` never calls `create_worktree`), so uncommitted
    work survives the move.

    **A6 (round 1 fold-in):** the fallback is chosen against `tried` unioned with
    `quota.exhausted_backends()`, not `tried` alone — a reroute triggered by *this*
    task's own quota exit already rules out the backend it just left, but a *different*
    task's earlier failure can have recorded another backend as globally spent since, and
    a reroute must not land on it either. This never touches a `REASON_CONTEXT` rotation:
    `switch_task` bypasses `target_backend` (and therefore `exhausted=`) entirely for that
    reason, always targeting the backend the task is already on (D4) — `_rotate_session`
    does not even pass `exhausted=`.
    """
    if not _handover_ready(entry):
        return None
    available = _available_backends()
    current   = _entry_backend(entry)
    tried     = _backends_tried(entry)
    # A6 (round 1 fold-in) — a reroute must not land on a backend already known to be
    # globally spent, only on one this *task* has not yet tried. `tried` alone is a
    # per-task guard (the quota path's ping-pong prevention); `quota.exhausted_backends()`
    # is the timed record a *different* task's failure may have written since. Union,
    # not either alone: a backend this task has not tried but the record says is still
    # exhausted must be skipped exactly as a tried one would be.
    ruled_out = frozenset(tried) | exhausted_backends()
    if not fallback_backend(ROLE_IMPLEMENTER, current, available=available,
                            exhausted=ruled_out):
        return None
    task_id = entry.get("task_id", "")
    sid     = entry.get("session_id", "")
    # The outgoing backend joins the exhausted set *before* the switch, and `switch_task`
    # copies unknown keys onto the entry it returns, so the record travels with the task.
    handover = dict(entry)
    handover["backends_tried"] = [*tried, current] if current not in tried else list(tried)
    try:
        outcome = switch_task(task_id, reason=reason, entry=handover,
                              from_backend=current, available=available,
                              exhausted=ruled_out)
    except Exception as exc:
        # A switch is an optimisation over waiting; failing at it must never cost the
        # loop the pause it was going to take instead.
        print(f"  [switch] {task_id}: switch failed — {exc}")
        append_journal("backend_switch_failed",
                       f"{task_id} from={current} reason={reason} error={exc}"[:300],
                       session_id=sid)
        return None
    if not outcome.switched or not outcome.entry:
        return None

    new_entry = outcome.entry
    _rebase_launch_time(sid, new_entry, launch_times)
    print(f"  [switch] {task_id}: {current} → {outcome.to_backend} ({reason}) "
          f"in the same worktree {new_entry.get('worktree')}")
    return new_entry


def _threshold_switches(in_flight: list, launch_times: dict, five_pct: float,
                        cache: dict | None = None) -> int:
    """Move in-flight work off a backend under quota pressure. Returns how many moved.

    Zero — the common answer — means the caller throttles and pauses exactly as it did
    before M2. Below the switch threshold this costs one float comparison and reaches no
    backend at all, so the ordinary poll is unaffected. `cache` is the per-poll usage
    memo (A2's `_write_usage_sample` shares it too) so a backend already sampled this
    poll is never sampled again.
    """
    if not in_flight or five_pct < SWITCH_THRESHOLD_PCT:
        return 0
    moved = 0
    for entry in list(in_flight):
        if not _handover_ready(entry):
            continue
        if not _under_usage_pressure(_entry_backend(entry), five_pct, cache):
            continue
        new_entry = _switch_instead_of_waiting(entry, REASON_THRESHOLD, launch_times)
        if new_entry is None:
            continue
        in_flight[:] = [e for e in in_flight
                        if e.get("session_id") not in (entry.get("session_id"),
                                                       new_entry.get("session_id"))]
        in_flight.append(new_entry)
        moved += 1
    return moved


#: How many times one task may be rotated for context before the loop stops trying (D4).
#:
#: `_rotation_sample`'s "evidence must postdate the action" rule stops a task rotating on
#: a reading taken *before* its last rotation, but it cannot stop a feed that keeps
#: reporting a full context after one: the stamp advances, the number does not come down,
#: and the task is rotated once per poll forever, losing a fresh agent's conversation each
#: time. Only a count bounds that absolutely — "the token count must have moved" does not,
#: because a live-but-wrong feed reports a slightly different number every poll and so
#: satisfies it every poll.
#:
#: Reaching the cap degrades to exactly the pre-D4 behaviour: the task carries on in the
#: session it has, and `TASK_TIMEOUT` and the ordinary failure paths still apply. A task
#: that needs more than this many fresh sessions is one for Dan to look at, not one for
#: the loop to keep restarting.
MAX_CONTEXT_ROTATIONS = 3

#: Breadcrumbs already printed, so a standing condition is reported once per process
#: rather than once per poll. Keyed by "<kind>:<subject>".
_rotation_notices: set[str] = set()


def _rotation_notice(key: str, message: str) -> None:
    """Print one rotation breadcrumb, at most once per process.

    D4 is a trigger that can decline to fire for several unremarkable-looking reasons, and
    the first version of it hid every one of them — a blanket warning filter turned "this
    model id is in no table, so nothing on it can ever rotate" into silence, and the
    feature looked implemented while being dead. `doctor` cannot cover the gap either: it
    validates the `project.yaml` role slugs, which resolve fine, and never sees a name
    that only appears inside a usage sample. So the loop says it itself, in the shape of
    its existing `[usage]` / `[switch]` / `[rotate]` breadcrumbs.
    """
    if key in _rotation_notices:
        return
    _rotation_notices.add(key)
    print(message)


def _rotation_sample(entry: dict, cache: dict | None = None) -> Usage | None:
    """This poll's usage reading for `entry`, but only if it postdates the entry's launch.

    **The first half of D4's anti-loop guard.** A rotation's entire effect is on the
    session, and the reading that would justify the next one comes from the same per-poll
    sample that justified the last: a task that rotates, then re-reads the pre-rotation
    context on the very next poll, rotates again, and keeps rotating — losing whatever the
    fresh agent had done each time. Requiring evidence that *postdates the action* is the
    smallest rule that forbids that.

    It is only half, because it certifies *when* the reading was taken, not that the
    reading moved: a feed that restamps itself every render while reporting the same full
    context satisfies it every poll. `MAX_CONTEXT_ROTATIONS` is the other half.

    The sample is taken **through the entry's own handle** (`_entry_handle`), so it is a
    reading of this implementer's session and not of the account — an entry naming no
    session is not sampled at all, rather than falling back to an account-level reading
    the rotation could never justify acting on.

    Every unknown answers `None`, which the caller reads as "do not rotate": an entry with
    no session, no sample at all (G6), an undated one, or one taken at or before the
    launch. `switch_task` stamps
    the replacement entry's `started_at` from the same `now_iso()` clock the samples use,
    and both are fixed-width UTC ISO-8601 Z, so the string comparison is chronological.
    """
    handle = _entry_handle(entry)
    if handle is None:
        return None
    sample = _sampled_usage(_entry_backend(entry), cache, handle=handle)
    if sample is None:
        return None
    taken = str(getattr(sample, "updated_at", "") or "").strip()
    launched = str(entry.get("started_at") or "").strip()
    if not taken or not launched or taken <= launched:
        return None
    return sample


def _attributed_to(sample: Usage, entry: dict) -> bool:
    """True when `sample`'s context reading is about *this* entry's own session.

    **The precondition that makes D4 correct rather than merely wired up.** A quota window
    belongs to the account, so any sample may carry one; a context reading belongs to one
    conversation, and acting on someone else's is not a smaller version of acting on the
    right one — it is throwing away a healthy implementer's session on evidence about a
    different agent entirely.

    Both drivers attribute a reading now (A5), so this returns `True` for a live
    implementer and D4 fires: `usage(handle)` reads the named session's own record — a
    codex rollout, a claude transcript — and stamps `Usage.session_id` with the handle's
    id. What it still refuses is everything else. An account-level sample (`usage()` with
    no handle) names nobody by contract and can never pass, so a reading taken from
    somewhere else — the operator's own interactive statusline, say — cannot be mistaken
    for an implementer's and cost it its conversation.

    That the feature came on with no edit here is the point: it was always a precondition
    on the *data*, never a switch in `project.yaml` — a knob declared and never read is
    the defect class this whole queue exists to remove, and it would have left the
    operator able to turn on a rotation driven by the wrong session's numbers.

    It is one of three conditions a rotation must clear, and the only one about *whose*
    reading it is. `_rotation_sample` covers *when* the reading was taken — and since A5's
    fix round 1 both drivers date a handled reading by the mtime of the record it came
    from, so that guard can genuinely fail on either backend rather than only on claude.
    `MAX_CONTEXT_ROTATIONS` bounds the rest.
    """
    attributed = str(getattr(sample, "session_id", "") or "").strip()
    return bool(attributed) and attributed == str(entry.get("session_id") or "").strip()


def _context_rotations_done(entry: dict) -> int:
    """How many times this task has already been rotated for context.

    Carried on the `in_flight` entry the way `backends_tried` is, and for the same reason:
    `switch_task` copies keys it does not recognise onto the entry it returns, so the
    record travels with the task across every relaunch instead of living in loop-local
    state that a restart would forget.
    """
    try:
        return max(0, int(entry.get("context_rotations") or 0))
    except (TypeError, ValueError):
        return 0


def _rotate_session(entry: dict, launch_times: dict | None = None) -> dict | None:
    """Restart one task on a fresh session of the backend it is already running on.

    Returns the replacement `in_flight` entry, or `None` when nothing moved. Deliberately
    *not* `_switch_instead_of_waiting` with a different reason, in two ways that matter:

    * no fallback is looked for, because a rotation does not need one — the target is
      where the task already is, and a project with a single configured backend must
      still be able to rotate;
    * `backends_tried` is not touched. That set is the quota path's ping-pong guard, and
      adding the current backend to it here would rule out, for the rest of the task's
      life, the backend the task is deliberately staying on — spending the fallback chain
      on a problem the fallback chain cannot fix.

    The relaunch is in the *same* worktree (`maestro.switch` never calls
    `create_worktree`), so the uncommitted work the rotation exists to preserve survives.
    """
    task_id = entry.get("task_id", "")
    sid     = entry.get("session_id", "")
    current = _entry_backend(entry)
    # The count goes on *before* the switch, and `switch_task` copies unknown keys onto
    # the entry it returns, so the record survives the relaunch that resets everything
    # else about the session.
    handover = dict(entry)
    handover["context_rotations"] = _context_rotations_done(entry) + 1
    try:
        outcome = switch_task(task_id, reason=REASON_CONTEXT, entry=handover,
                              from_backend=current)
    except Exception as exc:
        # A rotation is an optimisation over letting a session fill up; failing at it
        # must never cost the loop anything it was not already going to have.
        print(f"  [rotate] {task_id}: rotation failed — {exc}")
        append_journal(ROTATE_FAILED_EVENT,
                       f"{task_id} from={current} to={current} "
                       f"reason={REASON_CONTEXT} error={exc}"[:300],
                       session_id=sid)
        return None
    if not outcome.switched or not outcome.entry:
        return None
    new_entry = outcome.entry
    _rebase_launch_time(sid, new_entry, launch_times)
    print(f"  [rotate] {task_id}: fresh {current} session ({REASON_CONTEXT}) "
          f"in the same worktree {new_entry.get('worktree')}")
    return new_entry


def _context_rotations(in_flight: list, launch_times: dict,
                       cache: dict | None = None) -> int:
    """Rotate every in-flight task whose own context has filled up (D4). Returns how many.

    The fourth switch trigger, and the only one that is not about quota: nothing is wrong
    with the backend, the conversation is simply too long to keep working in. `cache` is
    A2's per-poll usage memo, shared with the pressure check and the `usage.json` write,
    so this takes no third reading of any driver.

    The ceiling is looked up **by the model the task was launched with** — the `model` the
    launch stamped on the `in_flight` entry, falling back to `roles.model_for` for an entry
    written before that key existed — and not by the model name the sample reports. The
    role table is deliberately not the primary source: since C1 a task's own `model:`
    overrides it at the launch site, so asking `roles.model_for` here measured every
    overridden task against a model it was not running (`model: opus` on a sonnet role
    default rotated ~20K early, discarding a healthy conversation). A sample's `model` is
    a display name written
    for a human status line (`"Opus 5"`), which is in neither limits table and which no
    amount of normalising turns into the table's `"Claude Opus 5"`; keying on it made this
    trigger answer `False` on every claude task no matter how full the context was. The
    sample's name stays as `context_crossed`'s fallback for a driver that reports a real
    model id alongside an attributed reading.

    The order of the checks below is deliberate. Attribution is tested *after* the
    threshold even though it is the stronger precondition, because no action is taken
    either way and testing it second is what lets the loop say "a rotation was indicated
    here and could not be justified" instead of staying silent.

    Both `limits.resolve` and `roles.model_for` re-read from disk on every call —
    `resolve` re-parses and re-merges both markdown tables and rewrites
    `.orchestrator/model_limits.json` while it is there — so both are memoised for the
    length of one poll: several tasks on one model cost one parse, and a poll with nothing
    measurable to compare costs none at all.
    """
    if not in_flight:
        return 0

    rows: dict = {}
    models: dict = {}

    def resolve(name: str):
        if name not in rows:
            # `resolve` raises a `UserWarning` for an id it cannot place. Captured rather
            # than filtered, so it becomes a breadcrumb naming the id instead of the
            # silence that hid this trigger being dead.
            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                rows[name] = resolve_model_limits(name)
            if rows[name] is None:
                _rotation_notice(
                    f"model:{name}",
                    f"  [rotate] no context-limit row for model {name!r} — tasks running "
                    f"on it cannot rotate on a full context (D4). Add a row to "
                    f"model_context_limits.md, or check `maestro doctor`.",
                )
        return rows[name]

    def launch_model(entry: dict) -> str:
        """The model this entry's agent is actually running under.

        The entry's own `model` first — since C1 a task's `model:` beats the role table
        at the launch site, so the role table is no longer the whole answer and asking it
        would measure an overridden task against a model it is not running. `roles`
        stands in only for an entry that predates the key: written before this shipped,
        or hand-edited. Blank is "nothing recorded", not "a model called nothing".
        """
        recorded = str(entry.get("model") or "").strip()
        if recorded:
            return recorded
        backend = _entry_backend(entry)
        if backend not in models:
            try:
                models[backend] = model_for(ROLE_IMPLEMENTER, backend) or ""
            except Exception:
                models[backend] = ""
        return models[backend]

    moved = 0
    for entry in list(in_flight):
        if not _handover_ready(entry):
            continue
        sample = _rotation_sample(entry, cache)
        if sample is None:
            continue
        backend = _entry_backend(entry)
        if not context_crossed(sample, launch_model(entry), resolve=resolve, backend=backend):
            continue
        if not _attributed_to(sample, entry):
            _rotation_notice(
                f"unattributed:{backend}",
                f"  [rotate] a {backend} context reading crossed the handoff mark but "
                f"names no session, so it cannot be shown to be this implementer's — not "
                f"rotating. Both shipped drivers stamp Usage.session_id on a per-session "
                f"reading, so a sample arriving here unattributed means this backend "
                f"answered about something other than the session it was asked about.",
            )
            continue
        task_id = entry.get("task_id", "")
        done = _context_rotations_done(entry)
        if done >= MAX_CONTEXT_ROTATIONS:
            _rotation_notice(
                f"capped:{task_id}",
                f"  [rotate] {task_id}: already rotated {done} times "
                f"(MAX_CONTEXT_ROTATIONS={MAX_CONTEXT_ROTATIONS}) — leaving it in the "
                f"session it has.",
            )
            continue
        new_entry = _rotate_session(entry, launch_times)
        if new_entry is None:
            continue
        in_flight[:] = [e for e in in_flight
                        if e.get("session_id") not in (entry.get("session_id"),
                                                       new_entry.get("session_id"))]
        in_flight.append(new_entry)
        moved += 1
    return moved


def _native_git_reports_ready(workspace: Path, *, running: bool) -> bool:
    """A native DONE is provisional until exact-task-ref promotion has passed."""
    from maestro.backends import control_files, git_staging

    if git_staging.publication_timeout(workspace):
        if running or git_staging.publication_active(workspace):
            return False
        (workspace / 'DONE').unlink(missing_ok=True)
        return True
    state = git_staging.publication_state(workspace)
    if state is None or state == 'promoted':
        return True
    if state == 'pending' and (running or git_staging.publication_active(workspace)):
        return False
    # Preserve result/journal evidence, but make an unpromoted attempt take the
    # existing failure/retry path. Never synthesize DONE from its result.json.
    (workspace / 'DONE').unlink(missing_ok=True)
    if not (workspace / 'FAILED').exists():
        control_files.write(workspace / 'FAILED', 'native Git transaction was not promoted')
    return True


def reconcile_in_flight(state: dict, launch_times: dict) -> list:
    """Reconcile in_flight entries against live tmux windows + workspace sentinels.

    Runs at startup AND once per poll cycle so stale entries (window gone, no
    sentinel) are reaped within one poll rather than persisting for the whole
    session.  Mutates launch_times for newly-adopted entries.  Returns the
    surviving in_flight list (entries to keep tracking).
    """
    from maestro.backends import git_staging

    surviving: list[dict] = []
    # Bug #2 (2026-06-26): restore each survivor's persisted launch epoch instead of
    # resetting the clock to now. At startup `state` is the full read_state() (has
    # launch_times); the per-poll call passes only {"in_flight": ...}, where launch_times
    # is already populated in memory so the setdefault is a no-op.
    persisted = state.get("launch_times", {})
    for entry in state.get("in_flight", []):
        window    = entry.get("window", "")
        workspace = WORKSPACES / entry["session_id"]
        sid       = entry["session_id"]
        task_id   = entry["task_id"]

        launch_times.setdefault(sid, persisted.get(sid, time.time()))
        if (git_staging.publication_timeout(workspace)
                or git_staging.publication_state(workspace) not in (None, 'promoted')):
            _timeout_expired(entry, launch_times)
        running = tmux_window_exists(window)
        if not _native_git_reports_ready(workspace, running=running):
            # A timeout attempt or FAILED report is not evidence the worker has
            # stopped. Keep tracking it until its window and trusted owner retire.
            surviving.append(entry)
            continue
        if git_staging.publication_timeout(workspace):
            # Main parks from the trusted outcome even if FAILED is an alias,
            # absent, or replaced by the worker after the stop boundary.
            surviving.append(entry)
            continue
        if running:
            if workspace.exists():
                # Zombie guard: implementer wrote result.json but left no sentinel and its
                # window is still alive (hung/idle). Don't re-adopt forever — after a short
                # grace, synthesize the sentinel from result.json, kill the dead window, and
                # let the main loop's completion handlers (incl. resumable handle_incomplete)
                # run. See RECONCILE_DONE_GRACE_POLLS note above.
                # `[P3]`: the set of files that count as "the workspace has spoken" is
                # the facade's, not a local tuple. `SWITCH` is deliberately still excluded
                # here — a checkpoint-and-exit handover is precisely the case this zombie
                # guard must keep waiting through, because the task is not finished.
                has_sentinel = any(
                    (workspace / s).exists()
                    for s in (_facade.DONE_SENTINEL, _facade.FAILED_SENTINEL,
                              _facade.PAUSED_SENTINEL)
                )
                if not has_sentinel and (workspace / "result.json").exists():
                    n = _reconcile_zombie_polls.get(sid, 0) + 1
                    _reconcile_zombie_polls[sid] = n
                    if n >= RECONCILE_DONE_GRACE_POLLS:
                        synth = _synthesize_sentinel(workspace)
                        _kill_tmux_window(window)
                        _reconcile_zombie_polls.pop(sid, None)
                        print(f"  [reconcile] {task_id}: result.json but no sentinel for "
                              f"{n} polls — synthesized {synth}, killed zombie window {window}")
                        append_journal("reconcile_synth_sentinel",
                                       f"{task_id} window={window} synth={synth} polls={n}",
                                       session_id=sid)
                        surviving.append(entry)
                        launch_times.setdefault(sid, persisted.get(sid, time.time()))
                        continue
                    print(f"  [reconcile] {task_id}: result.json present, no sentinel "
                          f"(grace {n}/{RECONCILE_DONE_GRACE_POLLS})")
                else:
                    _reconcile_zombie_polls.pop(sid, None)
                print(f"  [adopt] Re-adopted running: {task_id} @ {window}")
                append_journal("re_adopted", f"{task_id} window={window}", session_id=sid)
                surviving.append(entry)
                launch_times.setdefault(sid, persisted.get(sid, time.time()))
            else:
                # Window alive but workspace missing — zombie; mark FAILED
                print(f"  [reconcile] {task_id}: window alive but workspace missing — FAILED")
                append_journal("reconcile_stale",
                               f"{task_id} window={window} no_workspace", session_id=sid)
                workspace.mkdir(parents=True, exist_ok=True)
                (workspace / "FAILED").write_text("workspace missing at orchestrator restart")
                surviving.append(entry)
                launch_times.setdefault(sid, persisted.get(sid, time.time()))
        elif (workspace / "DONE").exists():
            print(f"  [adopt] {task_id} window gone, DONE present — will merge")
            append_journal("reconcile_done_present",
                           f"{task_id} window={window}", session_id=sid)
            surviving.append(entry)
            launch_times.setdefault(sid, persisted.get(sid, time.time()))
        elif (workspace / "FAILED").exists():
            print(f"  [adopt] {task_id} window gone, FAILED present — will retry/escalate")
            append_journal("reconcile_failed_present",
                           f"{task_id} window={window}", session_id=sid)
            surviving.append(entry)
            launch_times.setdefault(sid, persisted.get(sid, time.time()))
        else:
            # Window gone, no sentinel. Before failing, read impl.log (Fix A capture).
            workspace.mkdir(parents=True, exist_ok=True)
            # B — reactive net: ask the entry's own driver whether its process died on a
            # quota wall (A1: routed through `parse_exit`, not a Claude-worded scan — see
            # `_reconcile_exit_verdict`). DON'T fail/retry/escalate on that verdict (a
            # retry just re-hits the same wall). Pause Maestro until the limit resets; the
            # task is dropped from in_flight and relaunches fresh on resume, retry_counts
            # untouched.
            verdict = _reconcile_exit_verdict(entry, _tail_text(workspace / "impl.log", 25))
            if verdict is not None and verdict.kind == "quota_exhausted":
                # A6 — remember which backend is spent and until when, straight from the
                # `ExitVerdict` the driver already returned (no new detection, no new
                # parsing). Read back by `exhausted_backends()` on every *fresh* launch
                # (`_launch_backend`, `implementer._implementer_backend`,
                # `agentcall.resolve_call`), so the *next* task skips this backend via
                # `roles.resolve`'s `exhausted=` instead of failing onto it again and
                # rediscovering the same wall. Best-effort exactly like the switch below:
                # a broken recorder must not cost the loop the switch or pause it was
                # about to do anyway.
                #
                # Deliberately `entry.get("backend")`, NOT `_entry_backend(entry)`:
                # `_entry_backend` falls back to `_launch_backend()` for a legacy/
                # hand-edited entry with no `backend` key — and `_launch_backend()` now
                # *itself* consults this same record, via `exhausted_backends()`. Feeding
                # its guess back in as the subject of a new record would close a feedback
                # loop: an already-exhausted `claude` could make `_entry_backend` guess
                # `codex` for a `backend`-less entry, and a real quota exit on that entry
                # would then record *codex* as exhausted on no evidence at all. An entry
                # with no recorded backend is not evidence about any named one, so the
                # write is skipped rather than guessed — the same fail-safe direction as
                # a missing `reset_at`.
                recorded_backend = entry.get("backend")
                if isinstance(recorded_backend, str) and recorded_backend.strip():
                    try:
                        record_backend_exhausted(recorded_backend, verdict.reset_at)
                    except Exception as exc:
                        print(f"  [reconcile] {task_id}: failed to record backend "
                              f"exhaustion — {exc}")
                else:
                    print(f"  [reconcile] {task_id}: entry carries no backend — "
                          f"skipping exhaustion record")
                # M2/D3 — a limit on one backend is only a reason to pause everything if
                # no other backend can take this task. When one can, the work moves
                # instead of waiting for the reset: same worktree, uncommitted changes
                # intact, a fresh session on the fallback. `_switch_instead_of_waiting`
                # answers `None` whenever that is not possible, and the pause below then
                # runs exactly as it always has.
                moved = _switch_instead_of_waiting(entry, REASON_QUOTA, launch_times)
                if moved is not None:
                    print(f"  [reconcile] {task_id}: usage limit on exit — switched to "
                          f"{moved.get('backend')} (no pause, no retry burned)")
                    surviving.append(moved)
                    continue
                _pause_for_usage_limit(task_id, verdict.reset_at or "", verdict.evidence,
                                       workspace)
                print(f"  [reconcile] {task_id}: usage limit on exit — paused until "
                      f"{verdict.reset_at} (no retry burned)")
                continue  # drop from in_flight; do NOT mark FAILED
            # C — carry the real error forward so non-limit crashes are diagnosable by
            # Dan and by the ORCH_DIAGNOSE pass, instead of an opaque "window gone".
            reason = "window gone — stale in_flight entry cleared"
            tail = _tail_text(workspace / "impl.log", 15)
            if tail:
                reason += f"\n--- impl.log tail ---\n{tail}"
            print(f"  [reconcile] {task_id}: no window, no sentinel — stale entry; marking FAILED")
            append_journal("reconcile_stale",
                           f"{task_id} window={window} no_sentinel (cleared stale in_flight)",
                           session_id=sid)
            (workspace / "FAILED").write_text(reason)
            # Keep in surviving so the failure handler can park/retry it this cycle.
            surviving.append(entry)
            launch_times.setdefault(sid, persisted.get(sid, time.time()))

    return surviving


def _reconcile_and_persist_startup_state(state: dict, launch_times: dict) -> list:
    """Reconcile `in_flight` against reality once at startup, then persist the result.

    Round-1 review fix (A6, IMPORTANT 2): the previous inline version wrote the caller's
    `state` back to disk *after* reconciling — but `reconcile_in_flight` can itself write
    to disk mid-pass, through two of its own read-modify-write helpers that go straight
    to `read_state()`/`write_state()` rather than through the `state` dict handed in
    here: `quota.record_backend_exhausted` (this feature — the one this bug would have
    silently defeated) and `quota._pause_for_usage_limit` (pre-existing, same class).
    Writing the caller's now-stale `state` back over the top would clobber whatever
    either of those wrote during the pass — exactly the "orchestrator restarted after a
    quota kill" case A6 exists for, since that is precisely when this startup path (as
    opposed to the per-poll one) runs. Re-reading immediately before the merge, instead
    of writing back the copy read before reconcile ran, is what the per-poll loop already
    does for every write that follows its own call to `reconcile_in_flight` (each reads
    state fresh immediately before writing); this brings the once-at-startup call in
    line with it rather than introducing a new pattern.
    """
    in_flight = reconcile_in_flight(state, launch_times)
    state = read_state()
    state["in_flight"] = in_flight
    state["skeleton_version"] = "B7"
    write_state(state)
    return in_flight


def _noop_merge_action(task_def: dict | None, task_id: str, retry_counts: dict) -> str:
    """`"retry"` or `"park"` for a branch that merged nothing. Never `"escalate"`.

    A no-op is not a regression. `park_regression` — where every failed merge used to go —
    escalates to Dan but registers the task in neither `parked_tasks` nor `waiting_on_dan`,
    so it stays runnable and relaunches every `POLL_INTERVAL`. Observed live 2026-08-20: a
    `kind: script` task looped 11 times in ~7 minutes with no backoff and no cap.

    `kind: script` parks on the first no-op: its `run:` command is authored in ROADMAP and
    deterministic, so re-running it reproduces the same nothing, and there is no implementer
    to re-brief — the same reasoning the verification gate already applies to script tasks.
    Anything with an implementer gets exactly one re-brief first, matching the gate and
    proof-review retry paths, then parks.
    """
    if (task_def or {}).get("kind") == "script":
        return "park"
    return "retry" if retry_counts.get(task_id, 0) < 1 else "park"


def _do_retry(task_id: str, entry: dict, reason: str,
              retry_counts: dict, in_flight: list) -> None:
    """Launch a retry implementer. Mutates in_flight and writes state.

    The retry stays on the backend the dying attempt was running on, rather than
    re-resolving the role default. A task reaches a non-default backend only because
    something moved it there — a quota exhaustion or a usage threshold — and re-resolving
    hands it straight back to the backend that just ran out of room, which is the wall the
    switch existed to avoid. Same reasoning `reconcile_stale` already applies when it
    refuses to blind-retry a usage-limit death.

    `backends_tried` is carried across for the same reason: it is the switch machinery's
    anti-ping-pong memory, and dropping it on a retry would let a task be handed back to a
    backend it has already exhausted.
    """
    ts_str     = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    new_sid    = f"impl-{task_id}-{ts_str}"
    new_ws     = WORKSPACES / new_sid
    new_ws.mkdir(parents=True, exist_ok=True)
    new_branch = f"impl-{task_id.lower()}-{ts_str}"
    worktree   = worktree_path_for(task_id)
    tasks_by_id = {t["id"]: t for t in parse_runnable_tasks()}
    task = tasks_by_id.get(task_id,
        {"id": task_id, "title": task_id, "short_desc": "", "mode": "autonomous"})
    try:
        model_revision = _legacy_bind_model_policy(task_id)
        from maestro.model_policy import task_model_context
        with task_model_context(REPO, task_id):
            carry_backend = _entry_backend(entry)
            # One task-scoped read for both the launch pin and the record.
            retry_backend = carry_backend or _launch_backend()
            create_worktree(task_id, worktree, new_branch)
            launch_implementer(task, new_sid, new_ws, worktree,
                retry_note=f"Previous attempt failed: {reason}. Try a different approach.",
                backend=carry_backend)
            new_entry: dict = {
                "session_id": new_sid, "task_id": task_id, "role": "implementer",
                "worktree": str(worktree), "window": f"impl-{task_id}",
                "branch": new_branch, "started_at": now_iso(), "status": "running",
                # M2: which backend this retry actually runs on. `launch_implementer` was
                # handed the same name, so the record and the launch cannot diverge.
                "backend": retry_backend,
                # D4/C1: and the model that name resolves to for *this* task — the retry may
                # be staying on a non-default backend, and the task may override the table.
                "model": _launch_model(task, retry_backend),
                "model_policy_revision": model_revision,
            }
            tried = entry.get("backends_tried")
            if isinstance(tried, (list, tuple)) and tried:
                new_entry["backends_tried"] = list(tried)
            in_flight.append(new_entry)
            state = read_state()
            state["in_flight"] = [e for e in state.get("in_flight", [])
                                   if e["session_id"] != entry["session_id"]] + [new_entry]
            write_state(state)
            append_journal("implementer_retry", f"{task_id} reason={reason[:100]}",
                           session_id=entry["session_id"])
    except Exception as exc:
        print(f"  [error] Retry launch failed for {task_id}: {exc}")
        park_failed(task_id, f"retry launch failed: {exc}")
        state = read_state()
        state["in_flight"] = [e for e in state.get("in_flight", [])
                               if e["session_id"] != entry["session_id"]]
        write_state(state)


def _remove_from_state(entry: dict) -> None:
    state = read_state()
    sid = entry["session_id"]
    state["in_flight"] = [e for e in state.get("in_flight", [])
                           if e["session_id"] != sid]
    # Bug #2: drop the persisted launch time for this finished session.
    state["launch_times"] = {k: v for k, v in state.get("launch_times", {}).items()
                             if k != sid}
    write_state(state)


def _idle_heartbeat_maybe(runnable: list) -> None:
    """Throttled journal/stdout heartbeat for an idle-but-gated loop (see note above)."""
    global _last_idle_heartbeat
    now = time.monotonic()
    if now - _last_idle_heartbeat < IDLE_HEARTBEAT_S:
        return
    _last_idle_heartbeat = now
    ids = ",".join(t["id"] for t in runnable) or "none"
    append_journal("idle_gated", f"no launchable task (all gated: {ids}); polling")
    print(f"  [idle] all runnable tasks gated ({ids}); next poll in {POLL_INTERVAL}s", flush=True)


def _self_update_maybe() -> bool:
    """Throttled idle-moment self-update check (DESIGN.md §9).

    Only ever called from the branch of `main`'s loop where `in_flight` is already
    empty — this function trusts that precondition rather than re-checking it, exactly
    like `maybe_self_update`/`adopt` themselves (see their docstrings).

    True means the loop should exit 0 now: a new version was adopted and this process still
    runs the old code, so the watchdog relaunches it on the new pin. The first-run adoption
    records the version already running and needs no restart."""
    global _last_self_update_check
    now = time.monotonic()
    if now - _last_self_update_check < IDLE_HEARTBEAT_S:
        return False
    _last_self_update_check = now
    had_version = "maestro_version" in read_state()
    status = maybe_self_update()
    if status != "up_to_date":
        print(f"  [self-update] {status}", flush=True)
    if not (had_version and status.startswith("adopted:")):
        return False
    append_journal("self_update_restart", f"{status}; exiting for the watchdog to relaunch")
    print("  [self-update] exiting so the watchdog relaunches on the new version", flush=True)
    return True


def _runner_switch_refusal(runner_kind: str, state: dict) -> str:
    """Why `runner_kind` may not start now, or `''` (D08, MIGRATION_GUARDRAILS §2).

    A switch is refused while the *other* loop still owns live work, because neither loop
    can see, finish or reconcile the other's. Checked before anything is created, so a
    refused switch leaves the project exactly as it was.
    """
    if runner_kind == "graph":
        in_flight = sorted(str(e.get("task_id")) for e in state.get("in_flight", []))
        awaiting = sorted(
            str(p.get("task_id")) for p in state.get("waiting_on_dan", {}).values()
            if p.get("kind") == "awaiting-verification"
        )
        if in_flight or awaiting:
            return (
                "engineering.runner=graph refused: the legacy runner still has active work "
                f"(in_flight={in_flight}, awaiting-verification={awaiting}) — let it finish "
                "or park it, or set engineering.runner back to legacy"
            )
        return ""
    store = _state_module.control_store()
    if store is None:
        return ""
    rows = store.conn.execute(
        "SELECT run_id, task_id, status FROM task_runs WHERE status IN ('running', 'paused')"
        " ORDER BY started_at, run_id"
    ).fetchall()
    if not rows:
        return ""
    runs = ", ".join(f"{r['task_id']}:{r['run_id']}({r['status']})" for r in rows)
    return (
        f"engineering.runner=legacy refused: graph runs are still open ({runs}) — "
        "set engineering.runner back to graph until they finish"
    )


def _graph_refresh_sprint(resources, now_epoch):
    """The graph controller's existing quota refresh also updates sprint controls."""
    global _SPRINT_REFRESH_ERROR
    from maestro import sprint, reset_signals
    if _state_module.control_store() is not None:
        try:
            settings = _config.sprint_config()
        except _config.ConfigError as exc:  # a bad sprint block must not stop dispatch every poll
            if str(exc) != _SPRINT_REFRESH_ERROR:
                append_journal('sprint_refresh_unavailable', f'ConfigError: {exc}')
            _SPRINT_REFRESH_ERROR = str(exc)
            return
        _SPRINT_REFRESH_ERROR = None
        sprint.refresh(resources, config=settings, now_epoch=now_epoch,
            signals=reset_signals.ResetSignalSource(clock=lambda: now_epoch).read())
        sprint.sync_notices(REPO, now_epoch=now_epoch)


_SPRINT_REFRESH_ERROR = None


def _graph_task_overrides(task_id):
    """Optional live overrides from this project, separate from frozen model choices."""
    if task_id is None:
        return None
    from maestro.taskgraph import scan_tasks

    try:
        content = (REPO / 'docs' / 'ROADMAP.md').read_text(encoding='utf-8')
    except FileNotFoundError:
        # Model-only recovery does not require a ROADMAP. Actual task-spec use
        # below still requires its source, and other read/config failures propagate.
        return None
    return next((task for task in scan_tasks(content, require_id=False)
                 if str(task.get('id', '')) == str(task_id)), None)


def _graph_routing(*, task_id=None):
    """The graph loop's `DispatchRouting`, built once per controller process (P12A W1/W2).

    Catalog: the verified register rows under the shipped defaults, plus a
    `model_api.capability_entry` for each `engineering.free_tier_endpoints` row. Policy
    from `workflows/project.yaml` (or the packaged default), repository queries over REPO,
    task specs from the ROADMAP. Statistics start empty until W3 persists outcomes.
    Module-level so the lane harness can substitute a fixture routing.
    """
    from maestro import thirdparty
    from maestro.backends import catalog as _catalog
    from maestro.repository.context import ContextError
    from maestro.repository.queries import RepositoryQueries
    from maestro.taskgraph import normalize_spec
    from maestro.workflows import routing as _routing
    from maestro.workflows.policy import ProjectPolicy

    from maestro.model_policy import (runtime_model_inputs, task_model_context, graph_catalog_defaults,
                                      configured_role_strengths,
                                      role_requirements, role_independence, near_cost_margin)
    project = _config.load_project_yaml()
    if task_id is None:
        model_revision, model_policy, project = runtime_model_inputs(project, REPO)
    else:
        with task_model_context(REPO, task_id):
            model_revision, model_policy, project = runtime_model_inputs(project, REPO)

    defaults = graph_catalog_defaults(model_policy, project)
    registered = _catalog.catalog_from_register(thirdparty.load_register(), defaults=defaults)
    catalog = _catalog.CapabilityCatalog.of([
        *registered.entries.values(),
        *_routing.endpoints_for_catalog(_config.free_tier_endpoints()),
    ])

    def task_spec(task_id: str):
        task = get_task_by_id(task_id)
        if task is None:
            raise ContextError(f"{task_id}: no such task in the ROADMAP")
        return normalize_spec(task)

    task = _graph_task_overrides(task_id)
    return _routing.DispatchRouting(
        catalog=catalog,
        policy=ProjectPolicy.load(REPO),
        queries=RepositoryQueries(REPO),
        task_spec=task_spec,
        role_strengths=configured_role_strengths(project),
        role_requirements=role_requirements(model_policy, project),
        role_independence=role_independence(project),
        near_cost_margin=near_cost_margin(project),
        pace_estimates=_config.pace_estimates(project, task=task),
        runaway_mode=_config.runaway_mode(project, task=task),
        project_priority=_config.project_priority(project),
        sprint_refresh=_graph_refresh_sprint,
        sprint_reader=lambda: (_state_module.read_state().get('sprint', {})
                               if _state_module.control_store() is not None else {}),
        model_policy_revision=model_revision,
        usage_reader=_routing.driver_usage,
        token_reader=_routing.driver_attempt_tokens,
        version_reader=_routing.observed_agent_version,
    )


def _graph_task_routing(routing, task_id):
    """Copy model inputs per run; quota pools, statistics and controller hooks stay shared."""
    from copy import copy
    from maestro import thirdparty
    from maestro.backends import catalog
    from maestro.model_policy import (runtime_model_inputs, task_model_context, graph_catalog_defaults,
                                      configured_role_strengths,
                                      role_requirements, role_independence, near_cost_margin, digest, shipped_policy)
    with task_model_context(REPO, task_id):
        revision, policy, project = runtime_model_inputs(_config.load_project_yaml(), REPO)
    result = copy(routing)
    result.model_policy_revision = revision
    result.role_strengths = configured_role_strengths(project)
    result.role_requirements = role_requirements(policy, project)
    result.role_independence = role_independence(project)
    result.near_cost_margin = near_cost_margin(project)
    task = _graph_task_overrides(task_id)
    result.pace_estimates = _config.pace_estimates(project, task=task)
    result.runaway_mode = _config.runaway_mode(project, task=task)
    result.project_priority = _config.project_priority(project)
    frozen_policy = project.get('_model_policy_frozen_policy')
    # An unchanged unadopted inventory already belongs to the controller catalog.
    # Rebuild only when the snapshot differs, preserving catalog extensions and
    # injected controller backends while still honoring pre-upgrade task rows.
    if policy is not None or (frozen_policy is not None and
                             digest(frozen_policy) != digest(shipped_policy())):
        defaults = graph_catalog_defaults(policy, project)
        native = catalog.catalog_from_register(thirdparty.load_register(), defaults=defaults)
        native_ids = {base['backend_id'] for base in defaults.values()}
        result.catalog = catalog.CapabilityCatalog.of([
            *native.entries.values(),
            *(entry for backend, entry in routing.catalog.entries.items() if backend not in native_ids),
        ])
    return result


def _graph_fallback(routing, spec, revision) -> None:
    """P12B F5: honour a task's `fallback: step_down` only when its work will be checked.

    Refused → journaled, shown by `maestro status`, and the task runs flagless.
    """
    from maestro import gates
    from maestro.workflows import compiler as _wf_compiler

    task_id = str(revision.task_id)
    if getattr(spec, "fallback", None) is None:
        routing.step_down_tasks.discard(task_id)
        return
    try:
        has_specs = bool(gates.authoritative_specs(task_id))
    except Exception:  # noqa: BLE001 — unreadable specs are no specs
        has_specs = False
    reason = _wf_compiler.fallback_refusal(spec, revision, has_authoritative_specs=has_specs)
    state = read_state()
    refused = dict(state.get("fallback_refused") or {})
    if reason is None:
        routing.step_down_tasks.add(task_id)
        if refused.pop(task_id, None) is not None:
            state["fallback_refused"] = refused
            write_state(state)
        return
    routing.step_down_tasks.discard(task_id)
    append_journal("fallback_refused", f"{task_id}: {reason}")
    if refused.get(task_id) != reason:
        refused[task_id] = reason
        state["fallback_refused"] = refused
        write_state(state)


def _graph_on_blocked(catalog):
    """The graph loop's reaction to a blocked requeue (P12B F4).

    Journals the pause, mirrors a known CLI reset into the legacy `backend_exhausted`
    table (graph → legacy), and for an auth failure journals it and pings Dan once per
    pause row.
    """
    from maestro.backends import registry as _registry

    notified: set[str] = set()

    def on_blocked(runner, attempt_id, record, pause) -> None:
        if pause is None:
            return
        pool_id, until = pause["pool_id"], pause["paused_until"]
        append_journal("pool_paused", f"{pool_id} until {until} ({record.kind})")
        row = runner.store.conn.execute(
            "SELECT backend_id FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        backend_id = row["backend_id"] if row is not None else None
        entry = catalog.entries.get(backend_id) if backend_id else None
        if pause["reset_known"] and entry is not None and entry.runtime_kind == "agent_cli":
            try:
                driver = _registry.driver_name_for_backend_id(backend_id)
            except _registry.UnknownBackend:
                driver = None
            if driver:
                record_backend_exhausted(driver, until)
        if record.kind == "auth_failed":
            append_journal("auth_failed_pause", f"{pool_id} until {until}: {record.evidence[:120]}")
            if attempt_id not in notified:
                notified.add(attempt_id)
                notify_telegram(
                    f"🔑 *Maestro* paused pool `{pool_id}` until {until} — authentication "
                    f"failed (`{backend_id}`). Re-authenticate to resume sooner."
                )

    return on_blocked


def _drain_command_inbox(extra=None) -> None:
    """P13 W8: apply what external actors queued in the command inbox, once per poll.

    The watchdog's `clear_pause`, `clear_blocked_on` and `reconcile_leases` requests, plus
    whatever handlers the calling loop adds in `extra`. A project with no control database
    has no inbox. A failure here is journaled and never stops the poll.
    """
    from maestro import command_inbox

    store = _state_module.control_store()
    if store is None:
        return
    try:
        command_inbox.drain(store, {**command_inbox.state_handlers(REPO), **(extra or {})})
    except Exception as exc:  # noqa: BLE001 — the inbox is retried on the next poll
        append_journal("command_inbox_failed", f"{type(exc).__name__}: {exc}"[:200])


#: P12: the control-store event a tick error outside `advance` leaves on its run.
GRAPH_TICK_FAILED_EVENT = "GraphTickFailed"


def _record_graph_tick_failure(store, run_id: str, exc: Exception, seen: set) -> None:
    """A durable `GraphTickFailed` event per distinct error per run, not one per poll.

    `advance` decides its own node (`WorkflowRunner._decide_controller_error`); what reaches
    the loop came from ingest, reconcile or settle and cannot be pinned on one node, so it
    is recorded on the run and the loop keeps polling. A store too broken to take the event
    leaves the journal line as the only record.
    """
    import traceback

    error = f"{type(exc).__name__}: {exc}"
    if (run_id, error) in seen:
        return
    try:
        store.append_event(
            GRAPH_TICK_FAILED_EVENT, run_id,
            payload={"error": error,
                     "traceback": "".join(traceback.format_exception(exc))[-4000:]},
            run_id=run_id,
        )
    except Exception:  # noqa: BLE001 — the journal line above is the fallback record
        return
    seen.add((run_id, error))


def _graph_dan_channel():
    """The graph loop's `kind: dan` operator channel. Module-level so a lane can swap it."""
    return _verify.DanVerificationChannel()


# ── graph runner: what the legacy loop told Dan, told from the control store ──────────


def _task_title(task: dict | None) -> str:
    task = task or {}
    return str(task.get("title") or task.get("short_desc") or "")


def _graph_outcome(store, artifacts, outcome_ref: str | None) -> dict:
    """A committed node result (`NodeResult` JSON) by its artifact id, or `{}`."""
    if not outcome_ref or artifacts is None:
        return {}
    try:
        result = json.loads(artifacts.read(outcome_ref).decode("utf-8"))
    except Exception:  # noqa: BLE001 — an unreadable outcome is no outcome
        return {}
    return result if isinstance(result, dict) else {}


def _graph_failure_reason(store, artifacts, run_id: str) -> tuple[str, str]:
    """Why a settled run failed: `(node_id, reason)` from its latest failed attempt.

    The classified failure (`failure_json`) first, then a refusal the controller recorded
    (a claim it failed, `worktree_unavailable`, `controller_error`), then the committed
    result's detail — which is where merge puts its refusal. The `park` and `diagnose`
    steps are how a failure is handled, not the failure, so they are skipped. A dead
    writer's attempt stays `uncertain` (A05) while its node fails, so it counts too.
    """
    rows = store.conn.execute(
        "SELECT node_id, failure_json, route_refusal, outcome_ref FROM attempts"
        " WHERE run_id = ? AND status IN ('failed', 'uncertain') ORDER BY rowid DESC",
        (run_id,),
    ).fetchall()
    for row in rows:
        if row["node_id"] in ("park", "diagnose"):
            continue
        try:
            failure = json.loads(row["failure_json"]) if row["failure_json"] else None
        except ValueError:
            failure = None
        if isinstance(failure, dict) and (failure.get("evidence") or failure.get("kind")):
            kind = str(failure.get("kind") or "failed")
            evidence = str(failure.get("evidence") or "").strip()
            return row["node_id"], f"{kind}: {evidence}" if evidence else kind
        if row["route_refusal"]:
            return row["node_id"], str(row["route_refusal"])
        detail = str(_graph_outcome(store, artifacts, row["outcome_ref"]).get("detail") or "")
        if detail.strip():
            return row["node_id"], detail.strip()
    return "", "no failure was recorded for this run"


def _graph_notify_failed(store, artifacts, task_id: str, run_id: str, channel=None) -> None:
    """Tell Dan a run settled failed (the legacy loop's `park_failed` escalation).

    Called once, on the transition this loop observed, so a restart never repeats it for
    a run that settled before the loop started (such a run is not resumed). With the
    operator channel it is a Dan request with Retry / Shelve buttons (G7 item 4), recorded
    once per run; `_graph_failed_answers` acts on the answer.
    """
    from maestro.workflows.post_merge import queued_fix

    fix = queued_fix(REPO, store, run_id)
    if fix:
        append_journal('graph_post_merge_fix_queued', f'{task_id} run={run_id} fix={fix}')
        return
    node, reason = _graph_failure_reason(store, artifacts, run_id)
    where = f" at {node}" if node else ""
    text = (
        f"✗ {task_id} failed{where} — parked (run {run_id}).\n"
        f"Reason: {reason[:700]}\n"
        f"Its branch impl/{task_id} is kept. /workflow {task_id} shows the graph; "
        f"/explain {task_id} says why; /unpark {task_id} retries."
    )
    if channel is not None and hasattr(channel, "request_failed"):
        channel.request_failed(task_id=task_id, run_id=run_id, text=text)
    else:
        notify_telegram(text)
    append_journal("graph_run_failed_notified",
                   f"{task_id} run={run_id} node={node or '?'}{delivery_note()}")


def _graph_failed_answers(channel) -> None:
    """G7 item 4: act on Dan's Retry / Shelve answers to failed-run notices, each once.

    Retry is `/unpark`: the task leaves `parked_tasks` and gets the reopen marker, so the
    next poll opens a fresh run on its kept branch. Shelve parks it until an `/unpark`.
    """
    if channel is None or not hasattr(channel, "failed_answers"):
        return
    for record, answer in channel.failed_answers():
        task_id = str(record.get("task_id") or "")
        state = read_state()
        if answer == "retry":
            reply = _graph_mark_reopen(state, task_id)
        else:
            parked = list(state.get("parked_tasks", []))
            if task_id not in parked:
                parked.append(task_id)
            state["parked_tasks"] = parked
            (state.get(GRAPH_REOPEN_KEY) or {}).pop(task_id, None)
            reply = f"🗄 {task_id} shelved — parked until you /unpark {task_id}."
        write_state(state)
        channel.mark_handled(record, answer)
        append_journal(f"graph_failed_{'retry' if answer == 'retry' else 'shelved'}",
                       f"{task_id} run={record.get('run_id')}")
        notify_telegram(reply)


def _graph_mark_reopen(state: dict, task_id: str) -> str:
    """A retry of a settled task, written into `state`: it leaves `parked_tasks`, its retry
    count is forgotten and it gets the reopen marker. Returns the reply for the operator."""
    parked = list(state.get("parked_tasks", []))
    if task_id in parked:
        parked.remove(task_id)
    state["parked_tasks"] = parked
    state.get("retry_counts", {}).pop(task_id, None)
    markers = dict(state.get(GRAPH_REOPEN_KEY) or {})
    markers[task_id] = now_iso()
    state[GRAPH_REOPEN_KEY] = markers
    return f"🔁 {task_id}: a fresh run opens on the next cycle, on impl/{task_id}."


def _graph_operator_handlers(store, runners: dict) -> dict:
    """P13 W8: the command-inbox handlers for `maestro ctl cancel` / `ctl retry`.

    Only the graph loop can apply these, since it holds the live runners. Each handler
    checks the request again (the run may have moved since the CLI checked it), acts, and
    tells the operator on Telegram: what it did, or why it refused. The same line is the
    command's journal detail.
    """
    from maestro import command_inbox
    from maestro.workflows.runner import OperatorRefused

    def live(run):
        graph_runner = runners.get(run["run_id"])
        if graph_runner is None:
            raise command_inbox.CommandRejected(
                f"{run['task_id']} run {run['run_id']} is running but this loop has no "
                f"runner for it (see graph_run_unresumable in the journal)"
            )
        return graph_runner

    def cancel(run, task_id: str, node_id) -> str:
        graph_runner = live(run)
        if node_id:
            graph_runner.operator_cancel(node_id)
            return (f"⏹ {task_id}: {node_id} stopped and failed (operator_canceled); run "
                    f"{run['run_id']} takes its failure path.")
        stopped = graph_runner.operator_cancel()
        # Dropped here, not by the loop's settle block: that block ticks the runner first,
        # and a tick would still advance the failure edges of a run that is now closed.
        del runners[run["run_id"]]
        append_journal("graph_run_settled", f"{task_id} run={run['run_id']} status=canceled")
        return (f"⏹ {task_id}: run {run['run_id']} canceled (stopped: "
                f"{', '.join(stopped) or 'no live node'}). `maestro ctl retry {task_id}` "
                f"opens a fresh run.")

    def retry(run, task_id: str, node_id) -> str:
        if node_id:
            attempt_id = live(run).operator_retry(node_id)
            return (f"🔁 {task_id}: {node_id} stopped (attempt {attempt_id}); a fresh "
                    f"attempt starts on the next cycle, from the same start.")
        state = read_state()
        reply = _graph_mark_reopen(state, task_id)
        write_state(state)
        return reply

    def handler(act):
        def apply(command) -> str:
            task_id = str(command.payload.get("task_id") or "")
            node_id = command.payload.get("node_id") or None
            try:
                run = command_inbox.check_graph_request(
                    store, command.command_type, task_id, node_id)
                line = act(run, task_id, node_id)
            except (command_inbox.CommandRejected, OperatorRefused) as exc:
                asked = " ".join(filter(None, [command.command_type, task_id, node_id]))
                notify_telegram(f"✗ {asked} refused: {exc}")
                raise command_inbox.CommandRejected(str(exc)) from exc
            notify_telegram(line)
            return line
        return apply

    return {"graph_cancel": handler(cancel), "graph_retry": handler(retry)}


def _graph_cooldown_saved(store, artifacts, task_id: str, run_id: str) -> bool:
    """G7 1c: tell Dan a resumable sitting saved its progress, if this run's `cooldown`
    step succeeded (legacy `handle_incomplete`'s ⏳ message). Returns whether it did."""
    row = store.conn.execute(
        "SELECT outcome_ref FROM attempts WHERE run_id = ? AND node_id = 'cooldown'"
        " AND status = 'succeeded' ORDER BY rowid DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    if row is None:
        return False
    saved = _graph_outcome(store, artifacts, row["outcome_ref"]).get("outputs") or {}
    done, total = saved.get("last_count", "?"), saved.get("total", "?")
    hours = saved.get("cooldown_hours", "?")
    notify_telegram(
        f"⏳ {task_id} incomplete: {done}/{total} ({saved.get('percent', '?')}%) — "
        f"progress saved on impl/{task_id}, resuming in ~{hours}h (run {run_id})."
    )
    append_journal("graph_run_cooldown_notified",
                   f"{task_id} run={run_id} {done}/{total}{delivery_note()}")
    return True


#: G7 item 2: `state.json` key of explicit reopen requests (`hitl.commands`).
GRAPH_REOPEN_KEY = "graph_reopen"


def _graph_reopenable(store, opened: set, state: dict) -> set:
    """The tasks among `opened` whose latest run has settled failed (or canceled) and that
    may get a fresh run: a reopen marker, or a resumable sitting whose `cooldown` step
    saved its progress (`resume_state`). Never by guessing (G7 item 2)."""
    markers = dict(state.get(GRAPH_REOPEN_KEY) or {})
    resume = state.get("resume_state") or {}
    out = set()
    for task_id in opened:
        latest = store.conn.execute(
            "SELECT run_id, status FROM task_runs WHERE task_id = ?"
            " ORDER BY started_at DESC, rowid DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if latest is None or latest["status"] in ("running", "succeeded"):
            continue
        if task_id in markers:
            out.add(task_id)
            continue
        cooled = store.conn.execute(
            "SELECT 1 FROM attempts WHERE run_id = ? AND node_id = 'cooldown'"
            " AND status = 'succeeded' LIMIT 1",
            (latest["run_id"],),
        ).fetchone()
        if cooled is not None and (resume.get(task_id) or {}).get("resume_after"):
            out.add(task_id)
    return out


def _graph_reopened(task_id: str, run_id: str) -> None:
    """Pop the task's reopen marker now that its fresh run is open, and journal it."""
    state = read_state()
    markers = dict(state.get(GRAPH_REOPEN_KEY) or {})
    if markers.pop(task_id, None) is not None:
        state[GRAPH_REOPEN_KEY] = markers
        write_state(state)
    append_journal("graph_run_reopened", f"{task_id} run={run_id}")


def _graph_accepted_run(store, task_id: str):
    """The task's latest succeeded run row, or None."""
    return store.conn.execute(
        "SELECT run_id, workflow_id, workflow_revision FROM task_runs"
        " WHERE task_id = ? AND status = 'succeeded'"
        " ORDER BY finished_at DESC, rowid DESC LIMIT 1",
        (task_id,),
    ).fetchone()


def _graph_acceptance_smoke(store, run_id: str) -> dict:
    """What `phase_report` needs in place of the legacy `smoke` dict.

    The graph has no smoke step; the report line is the acceptance decision's own reason
    (checks passed, criteria discharged, on which snapshot). No metrics are invented.
    """
    row = store.conn.execute(
        "SELECT reason FROM acceptance_decisions WHERE run_id = ? AND decision = 'accepted'"
        " ORDER BY rowid DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    reason = str(row["reason"]) if row else "accepted"
    return {"pass": True, "metrics": {}, "reason": reason}


#: `state.json` key listing the graph runs that are live now (G4). Deliberately not
#: `in_flight`: that list is legacy's, and the watchdog reaper, `reconcile_in_flight`, the
#: host ledger sync and `/approve`/`/backend` all read its rows as tmux-window sessions.
GRAPH_RUNS_KEY = "graph_runs"


def _graph_live_runs(store, runners: dict, titles: dict, *, mutex_owners=None) -> list[dict]:
    """One row per live graph run, oldest first: task, run, title, start, live nodes."""
    rows = []
    for run_id in runners:
        run = store.conn.execute(
            "SELECT task_id, started_at FROM task_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run is None:
            continue
        nodes = [
            r["node_id"] for r in store.conn.execute(
                "SELECT node_id FROM node_states WHERE run_id = ?"
                " AND phase IN ('claimed', 'running') ORDER BY node_id",
                (run_id,),
            )
        ]
        rows.append({
            "task_id": run["task_id"], "run_id": run_id,
            "title": titles.get(run["task_id"], ""), "started_at": run["started_at"],
            "status": "running", "nodes": nodes,
        })
        owned = sorted(label for label, owner in (mutex_owners or {}).items() if owner == run_id)
        if owned:
            rows[-1]['mutex_labels'] = owned
    rows.sort(key=lambda r: (r["started_at"] or "", r["run_id"]))
    return rows


def _graph_publish_live(live: list[dict]) -> bool:
    """Write the live runs, and `phase` as legacy sets it, into `state.json` on a change.

    `phase` names the most recently opened live run (legacy: the last launch), and is
    idle once nothing is live. Returns whether the set of live tasks changed, which is
    when legacy regenerated the dependency map and status.
    """
    state = read_state()
    if live:
        newest = live[-1]
        phase = {"id": newest["task_id"], "title": newest["title"], "status": "in_progress"}
    else:
        phase = {"id": None, "title": None, "status": "idle"}
    before = {r.get("task_id") for r in state.get(GRAPH_RUNS_KEY) or [] if isinstance(r, dict)}
    if state.get(GRAPH_RUNS_KEY) == live and state.get("phase") == phase:
        return False
    state[GRAPH_RUNS_KEY] = live
    state["phase"] = phase
    write_state(state)
    return before != {r["task_id"] for r in live}


def _graph_merge_review(task: dict, spec, state: dict) -> str:
    """Why Dan must review this task's change before it merges, or `''`.

    Legacy's `park_for_dan` under global `hitl_mode`, asked through the `dan_confirm`
    step (`compiler.merge_review_check`). Decided when the run opens: `/hitl on` holds
    every run opened after it, not one already past its compile. The manual and async
    lanes never reached legacy's merge pause. A `mode: needs-dan` task needs nothing here:
    `taskgraph` gives it the manual lane whatever its dispatch, which holds for Dan's own
    action rather than only for his review.
    """
    if getattr(spec, "execution_kind", "") in ("manual", "async"):
        return ""
    if state.get("hitl_mode"):
        return "hitl_mode is on"
    return ""


def _graph_push_target() -> None:
    """Publish the merge and its graduation commit, as legacy does after an accept.

    `git push origin main` (merge's target, `handlers.merge_handler`'s default), its
    failure ignored exactly as legacy ignores it, then the meta branch is reconciled —
    `reconcile_meta` is documented safe "after every main push" and never raises. A
    project with no `origin` (DuetFlow has none) pushes nothing and says so once.
    """
    remote = subprocess.run(["git", "remote", "get-url", "origin"],
                            cwd=str(REPO), capture_output=True, text=True)
    if remote.returncode != 0:
        global _graph_no_remote_journaled
        if not _graph_no_remote_journaled:
            append_journal("push_skipped_no_remote", "no git remote 'origin'; nothing pushed")
            _graph_no_remote_journaled = True
        return
    push = subprocess.run(["git", "push", "origin", "main"], cwd=str(REPO), capture_output=True)
    if push.returncode != 0:
        append_journal("push_failed", f"git push origin main exited {push.returncode}")
    print(f"[meta] {metabranch.reconcile_meta(REPO)}")


_graph_no_remote_journaled = False


def _graph_merge_range(store, artifacts, run_id: str) -> tuple[str, str]:
    """`(target_before, target_after)` from the run's committed merge result, or `('', '')`."""
    from maestro.workflows import compiler as _wf_compiler

    run = store.conn.execute(
        "SELECT workflow_id, workflow_revision FROM task_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    revision = _wf_compiler.load_revision(
        store, run["workflow_id"], run["workflow_revision"]) if run else None
    merge_ids = [n.node_id for n in (revision.nodes if revision else ())
                 if n.handler_ref == "maestro.handlers.merge"]
    for node_id in merge_ids:
        row = store.conn.execute(
            "SELECT outcome_ref FROM attempts WHERE run_id = ? AND node_id = ?"
            " AND status = 'succeeded' AND outcome_ref IS NOT NULL"
            " ORDER BY rowid DESC LIMIT 1",
            (run_id, node_id),
        ).fetchone()
        outputs = _graph_outcome(store, artifacts, row["outcome_ref"] if row else None) \
            .get("outputs") or {}
        before, after = outputs.get("target_before"), outputs.get("target_after")
        if before and after:
            return str(before), str(after)
    return "", ""


def _graph_changed_files(before: str, after: str) -> list[str]:
    proc = subprocess.run(["git", "diff", "--name-only", before, after],
                          cwd=str(REPO), capture_output=True, text=True)
    return [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]


def _graph_touches_bot_files(store, artifacts, run_id: str) -> bool:
    """Whether the accepted merge changed a `bot_files` path (legacy `_touches_bot_files`).

    Legacy diffed `main...<branch>`; a graph run deletes its merged branch when it
    settles, so the merge's own recorded range is diffed instead.
    """
    from maestro.config import _load_project_yaml

    bot_files = _load_project_yaml().get("bot_files") or []
    if not bot_files:
        return False
    before, after = _graph_merge_range(store, artifacts, run_id)
    if not before:
        return False
    return any(bf in f for f in _graph_changed_files(before, after) for bf in bot_files)


def _graph_goals_contract() -> bool:
    """Whether this project has a goals contract (`docs/GOALS.md`), so graduation records
    goal episodes and checkpoints (P13 GDT-8). Module-level so a test can swap it."""
    from maestro.docs import goals as doc_goals

    return doc_goals.path_for(REPO).exists()


def _graph_goal_replan(store, artifacts, task_def, run, channel) -> tuple[list, list]:
    """P13 GDT-8 at graduation: the accepted run's goal progress becomes episodes, a
    milestone reached runs a checkpoint, and a replan signal is applied (routine) or asked
    (`goal_replan.on_graduation`). Returns `(tasks to add, checkpoint follow-up texts)`.
    Best-effort: a failure is journaled and never blocks the graduation."""
    from maestro import goal_replan, goal_state

    if not task_def or not run or not _graph_goals_contract():
        return [], []
    tid = str(task_def["id"])
    try:
        records = goal_replan.collect_progress(store, artifacts, run["run_id"])
        open_tasks = [t for t in _load_roadmap_tasks() if str(t.get("id")) != tid]
        return goal_replan.on_graduation(
            REPO, task_def, records, open_tasks, channel, run_id=str(run["run_id"]),
            workflow_id=str(run["workflow_id"] or ""),
            at=datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
            notify=notify_telegram, analyse=lambda system, user: _judge_complete(system, user),
            journal=append_journal, completed=get_completed_task_ids(),
            # GDT-9: the accepted run's gate reruns, stored as the goals' durable progress.
            observed=goal_state.collect_observed(store, artifacts, run["run_id"]))
    except Exception as exc:  # noqa: BLE001 — a replan never blocks a graduation
        append_journal("goal_replan_failed", f"{tid}: {type(exc).__name__}: {exc}")
        return [], []


def _graph_replan_poll(channel) -> None:
    """Each poll: Dan's answers to waiting replans (P13 GDT-8); an approval's tasks, and a
    settled replan's tasks that never reached the ROADMAP, are committed to it; a replan
    wait the store no longer has asked is pruned. Best-effort, like the other per-poll
    HITL upkeep."""
    from maestro import goal_replan
    from maestro.docs import roadmap as _roadmap

    if channel is None or not _graph_goals_contract():
        return
    try:
        present = {str(t.get("id")) for t in _load_roadmap_tasks()} | get_completed_task_ids()
        added = goal_replan.poll(REPO, channel,
                                 datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
                                 present=present)
        by: dict[str, list] = {}
        for task in added:
            by.setdefault(str(task.get("replan_of") or "replan"), []).append(task)
        for rp, tasks in by.items():
            _roadmap.add_replan_tasks(tasks, rp)
        _verify.prune_replan_waits(goal_replan.asked_ids(REPO))
    except Exception as exc:  # noqa: BLE001
        append_journal("goal_replan_failed", f"poll: {type(exc).__name__}: {exc}")


def _goal_finish(channel, *, act: bool):
    """P13 GDT-10: the completion gate's step at queue exhaustion
    (`goal_completion.evaluate`); `None` without `docs/GOALS.md`, so the caller exhausts
    exactly as before. Never raises: an internal failure is a `blocked` step."""
    from maestro import goal_completion

    if not _graph_goals_contract():
        return None
    return goal_completion.evaluate(
        REPO, channel=channel, act=act, tasks=_load_roadmap_tasks(),
        parked=set(read_state().get("parked_tasks") or ()),
        at=datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        journal=append_journal, notify=notify_telegram, completed=get_completed_task_ids())


def _graph_goal_finish(channel):
    """The graph loop's completion gate: it asks operator checks and finish conflicts and
    runs the corrective rounds (`act`)."""
    return _goal_finish(channel, act=True)


def _graph_polish_task(task, *, rows=None, current=None):
    """Current source requirements, with saved provenance after graduation."""
    from maestro import backlog

    if not task.get('polish_items'):
        return task
    rows = backlog.items(REPO) if rows is None else rows
    current = ({str(t['id']): t for t in _load_roadmap_tasks()}
               if current is None else current)
    return backlog.inherit_resources(task, (
        current.get(str(row['source']['task']), row.get('source_task') or {})
        for row in backlog.provenance_rows(task, rows)))


def _graph_polish_sources(tasks, completed, state, *, held_resources=None) -> set[str]:
    """Accepted sources whose current task and operator gates still permit polish."""
    from maestro.docs import roadmap
    from maestro import backlog

    tasks = list(tasks)
    listed = {str(t['id']) for t in tasks}
    rows = backlog.items(REPO)
    for row in rows:
        source = row.get('source_task')
        if source and str(source['id']) not in listed:
            tasks.append(source)
            listed.add(str(source['id']))

    done = {str(t) for t in completed}
    done.update(str(t['id']) for t in tasks if t.get('status') == 'complete')
    blocked = (set(state.get('parked_tasks') or ()) | set(roadmap._drift_blocked_ids())
               | set(roadmap._goal_blocked_ids(tasks)) | set(roadmap._replan_blocked_ids(tasks)))
    blocked.update(str(v.get('task_id')) for v in (state.get('waiting_on_dan') or {}).values())
    live = {str(r.get('task_id')) for r in state.get('graph_runs') or ()}
    current = {str(t['id']): t for t in tasks}
    held = (set().union(*(_resource_set(_graph_polish_task(t, rows=rows, current=current))
                         for t in tasks if str(t['id']) in live))
            if held_resources is None else held_resources)
    return done - blocked - {
        str(t['id']) for t in tasks
        if t.get('status') in ('rejected', 'parked', 'paused', 'superseded')
        or t.get('hold') or t.get('self_modifying')
        or roadmap.pending_deps(t, done) or _resources_conflict(t, held, set())
    }


def _graph_polish_permitted(task, sources=None, *, held_resources=None) -> bool:
    """Recheck provenance after staging too; an operator hold never becomes a copy."""
    from maestro import backlog

    if not (backlog.is_polish(task) or backlog.is_triage(task)):
        return True
    if sources is None:
        document = read_state()
        # The source's inherited mutex belongs to this run too. It must block
        # other tasks, while a counted-resource retry cannot conflict with itself.
        document = {**document, 'graph_runs': [run for run in document.get('graph_runs') or ()
                    if str(run.get('task_id')) != str(task.get('id'))]}
        sources = _graph_polish_sources(_load_roadmap_tasks(), get_completed_task_ids(), document,
                                        held_resources=held_resources)
    rows = {r['id']: r for r in backlog.items(REPO)}
    ids = task.get('polish_items') or []
    return (bool(ids) and all(i in rows for i in ids)
            and all(str(row['source']['task']) in sources
                    for row in backlog.provenance_rows(task, list(rows.values()))))


def _graph_sprint_pools(routing, now_epoch) -> set[str]:
    """Only current own-pool sprint decisions can pull work into the frontier."""
    from maestro.sprint import pace_target

    decisions = routing.sprint_reader().get('decisions', {})
    return {pool for pool, resource in routing.resources.pools.items()
            if pace_target(pool, resource.observation, decisions.get(pool),
                           now_epoch=now_epoch) is not None}


_SPRINT_BUDGET_WAITS = frozenset({'budget_exhausted', 'budget_fair_share',
                                'budget_starvation_guard'})


def _graph_route_wait(graph_runner, report):
    """Keep every real node reason; one final budget reason cannot hide another gate."""
    from maestro.workflows.runner import ready_frontier

    nodes = {}
    for node_id, text in report.waiting.items():
        reason, _, detail = str(text).partition(': ')
        nodes[node_id] = dict(reason=reason, detail=detail)
        if node_id in report.budget_wait_pools:
            nodes[node_id]['pool_id'] = report.budget_wait_pools[node_id]
        if report.wait_retry.get(node_id) is not None:
            nodes[node_id]['retry_after_sec'] = int(report.wait_retry[node_id])
    if not nodes:
        return None
    phases = graph_runner.phases()
    budget_only = (not report.refused and not report.dispatched
        and not any(p in ('claimed', 'running') for p in phases.values())
        and set(ready_frontier(graph_runner.revision, phases)) == set(nodes)
        and all(n['reason'] in _SPRINT_BUDGET_WAITS and n.get('pool_id') for n in nodes.values()))
    reasons = {n['reason'] for n in nodes.values()}
    wait = dict(reason=next(iter(reasons)) if len(reasons) == 1 else 'multiple_waits',
        detail='; '.join(n['detail'] for n in nodes.values()), nodes=nodes,
        run_id=graph_runner.run_id, budget_only=bool(budget_only))
    retries = [n['retry_after_sec'] for n in nodes.values() if 'retry_after_sec' in n]
    if retries:
        wait['retry_after_sec'] = min(retries)
    return wait


def _graph_sprint_order(runners, routing, state, runnable):
    """Pull budget-only retries forward inside the existing loop, never readmit here."""
    from maestro import taskgraph
    from graphlib import CycleError
    from maestro.workflows.runner import ready_frontier

    ordered = list(runners.items())
    pools = _graph_sprint_pools(routing, time.time())
    if not pools:
        return ordered
    permitted = {str(t['id']): t for t in runnable
                 if t.get('status') not in ('paused', 'rejected', 'parked')}
    try:
        ready = set(scheduling.roadmap_frontier(taskgraph.build_graph(
            taskgraph.load_document(ROADMAP_FILE), registry_ids=get_completed_task_ids(), strict=False),
            holds=state.get('parked_tasks') or (), permits=lambda spec: spec.task_id in permitted))
    except (OSError, taskgraph.TaskGraphError, CycleError) as exc:
        append_journal('sprint_order_unavailable', f'{type(exc).__name__}: {exc}')
        return ordered  # ordinary loop eligibility and every admission gate still apply
    pulled = set()
    for run_id, run in ordered:
        task_id = run.revision.task_id
        wait = (state.get('admission_waiting') or {}).get(task_id) or {}
        nodes = wait.get('nodes') or {}
        phases = run.phases()
        if (task_id in ready and wait.get('run_id') == run_id and wait.get('budget_only') is True
                and nodes and set(nodes) == set(ready_frontier(run.revision, phases))
                and not any(p in ('claimed', 'running') for p in phases.values())
                and all(n.get('reason') in _SPRINT_BUDGET_WAITS and n.get('pool_id') in pools
                        for n in nodes.values())):
            pulled.add(run_id)
    # Preserve explicit task priority and ordinary relative order. Sprint grants
    # no resource, quality, quota or shared-budget exemption: tick rechecks all.
    for item in list(ordered):
        if item[0] not in pulled:
            continue
        index = ordered.index(item)
        priority = _task_priority(permitted[item[1].revision.task_id])
        while index and ordered[index-1][0] not in pulled:
            previous_task = permitted.get(ordered[index-1][1].revision.task_id, {})
            if _task_priority(previous_task) > priority:
                break
            ordered[index-1], ordered[index] = ordered[index], ordered[index-1]
            index -= 1
    return ordered


def _graph_polish_stage(*, sprint: bool = False) -> bool:
    """P13 GDT-11 / W7 at queue exhaustion: the backlog's open review follow-ups become the
    next polish stage (`backlog.stage`: a triage task, or one item's polish task), appended
    to the ROADMAP and committed, and Dan is told once. True when a stage opened. Module-level
    so a test can swap it. Never raises: a broken backlog journals `polish_stage_failed` and
    the completion gate runs as before."""
    from maestro import backlog
    from maestro.docs import roadmap as _roadmap

    try:
        roadmap_tasks = _load_roadmap_tasks()
        completed = get_completed_task_ids()
        tasks = backlog.stage(REPO, roadmap_tasks, completed,
                              at=datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
                              eligible_sources=_graph_polish_sources(
                                  roadmap_tasks, completed, read_state()))
        if not tasks:
            return False
        first = tasks[0]
        n, ids = first["polish_stage"], list(first["polish_items"])
        tasks = [_graph_polish_task(t) for t in tasks]
        _roadmap.add_tasks(tasks, f"polish stage {n}", "polish")
        what = (f"{len(ids)} review follow-ups go to triage ({first['id']})"
                if backlog.is_triage(first) else f"1 follow-up → {first['id']}")
        prefix = "Sprint: eligible follow-ups." if sprint else "Main work is done."
        notify_telegram(f"🧹 {prefix} Polish stage {n}: {what}.")
        append_journal("polish_stage_opened", f"{n}: {', '.join(ids)} → {first['id']}")
        return True
    except Exception as exc:  # noqa: BLE001
        append_journal("polish_stage_failed", f"{type(exc).__name__}: {exc}")
        return False


def _graph_at_exhaustion(channel):
    """The graph loop's order at queue exhaustion: the polish stage (GDT-11), then the
    completion gate (GDT-10), which never schedules polish itself. `(True, None)` when a
    stage just opened (its tasks run next, the finish waits), else `(False, its step)`."""
    if _graph_polish_stage():
        return True, None
    return False, _graph_goal_finish(channel)


def _legacy_goal_finish():
    """The legacy loop's completion gate: the verdict only (no questions, no corrective
    rounds; the legacy loop has no question channel)."""
    return _goal_finish(None, act=False)


def _goal_finish_holds(step) -> bool:
    """Whether the completion gate's step keeps the loop from calling the queue done: not
    without a contract (`None`, format 1) and not when complete. The outcome is kept in
    `state.json` (`goal_outcome`) and printed when it changes."""
    if step is None or step["kind"] == "none":
        return False
    from maestro import goal_completion

    v = step.get("verdict") or {}
    outcome = {"verdict": v.get("verdict") or step["kind"], "revision": v.get("revision"),
               "commit": v.get("commit"), "report": goal_completion.REPORT_MD}
    state = read_state()
    known = {k: val for k, val in (state.get("goal_outcome") or {}).items() if k != "at"}
    if known != outcome:
        state["goal_outcome"] = {
            **outcome, "at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds")}
        write_state(state)
        print(f"\n[goals] {step['kind']}: {step['text'] or outcome['verdict']}", flush=True)
    return step["kind"] != "complete"


def _goal_finish_note(step) -> str:
    """The one sentence the ordinary exhaustion ping adds: a format-1 contract is not
    verified. Empty otherwise (no contract, or complete: its own message was sent)."""
    return str(step["text"]) if step and step["kind"] == "none" else ""


def _graph_goal_finish_poll(channel) -> None:
    """Each poll: Dan's answers to the completion gate's operator checks and finish
    conflicts (P13 GDT-10); the waits of an older verification run (another HEAD or
    contract revision) are pruned, so a stale question never holds the loop."""
    from maestro import goal_completion

    if channel is None or not _graph_goals_contract():
        return
    try:
        fid = goal_completion.poll(REPO, channel,
                                   datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
                                   journal=append_journal)
        if fid:
            _verify.prune_runless_waits("finish-", {fid})
    except Exception as exc:  # noqa: BLE001
        append_journal("goal_finish_failed", f"poll: {type(exc).__name__}: {exc}")


def _improvement_analyst(system: str, user: str) -> str:
    """P13 GDT-12 / W12: the improvement analyst, one bounded judgment on the diagnoser
    route (`improvement.analyst`; no new role, no graph node). Module-level so a lane
    patches it."""
    from maestro import improvement

    return improvement.analyst(system, user)


def _improvement_now() -> str:
    """The improvement analysis's clock (UTC ISO). Module-level so a lane moves it."""
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _improvement_throttled(last: float) -> tuple[bool, float]:
    """`(throttled, now)` for an idle-moment check last run at `last` (a clock that went
    backwards, such as a test's fresh one, is never throttled)."""
    now = time.monotonic()
    return 0 <= now - last < IMPROVEMENT_CHECK_S, now


def _graph_improvement(store, artifacts, channel, kind: str):
    """P13 GDT-12 / W12: one improvement analysis when it is due (`improvement.due`):
    `project_end` once per HEAD, right after the queue is called exhausted; `weekly` at an
    idle tick (`_graph_improvement_weekly`). Nothing is in flight at either moment, so the
    bounded analyst call stalls no work. Returns the stored run, `None` when none was due.
    Never raises: a failure journals `improvement_failed`."""
    from maestro import improvement

    try:
        at = _improvement_now()
        head = improvement.head(REPO) if kind == "project_end" else None
        key = improvement.due(REPO, kind, at=at, head=head)
        if key is None:
            return None
        return improvement.run(
            REPO, _state_module.CONTROL_DB, store, artifacts, kind=kind, key=key, at=at,
            analyse=_improvement_analyst, channel=channel, notify=notify_telegram,
            journal=append_journal)
    except Exception as exc:  # noqa: BLE001 — an analysis never stops the loop
        append_journal("improvement_failed", f"{kind}: {type(exc).__name__}: {exc}"[:300])
        return None


def _graph_improvement_weekly(store, artifacts, channel, runnable=()):
    """The weekly improvement run at an idle tick, throttled like the self-update check
    (`IMPROVEMENT_CHECK_S`); due when no run of this project is younger than
    `thresholds.improvement_interval_days`. Only at a real idle moment: with work
    `runnable` but not launched (the last runner just settled, or admission or a quota
    pause holds every launch) it waits, without spending the throttle, since an analysis
    with a pattern may take `MAX_TRIES` x `ANALYST_TIMEOUT` (review M1)."""
    global _last_improvement_check
    if runnable:
        return None
    throttled, now = _improvement_throttled(_last_improvement_check)
    if throttled:
        return None
    _last_improvement_check = now
    return _graph_improvement(store, artifacts, channel, "weekly")


def _graph_improvement_poll(channel) -> None:
    """Each poll: Dan's answers to the improvement questions, applied (P13 GDT-12 §7): an
    approved config lever edits the global config, a code lever is queued in Maestro's
    intake, a tap on Roll back rolls back; and, throttled, the landing check of queued code
    levers. Never raises."""
    global _last_landing_check
    from maestro import improvement

    try:
        at = _improvement_now()
        improvement.poll(
            REPO, channel, at,
            apply=lambda row: improvement.apply_config(
                REPO, row, journal=append_journal, notify=notify_telegram),
            queue=lambda row: improvement.queue_intake(
                REPO, row, journal=append_journal, notify=notify_telegram),
            undo=lambda row: improvement.rollback(
                REPO, row, journal=append_journal, notify=notify_telegram),
            journal=append_journal, notify=notify_telegram)
        throttled, now = _improvement_throttled(_last_landing_check)
        if not throttled:
            _last_landing_check = now
            improvement.landed(REPO, at=at, journal=append_journal)
    except Exception as exc:  # noqa: BLE001
        append_journal("improvement_failed", f"poll: {type(exc).__name__}: {exc}"[:300])


def _graph_replan_holds(*, waiting: bool = False) -> dict:
    """`{task id: replan id}` the goal replans hold now (P13 GDT-8); `{}` without
    `docs/GOALS.md`. With `waiting`, without the holds of a replan whose added task is
    parked (blocked on Dan's `/unpark`, not waiting on a replan). Never raises: dispatch
    already journals an unreadable store."""
    from maestro import goal_replan

    if not _graph_goals_contract():
        return {}
    try:
        if waiting:
            return goal_replan.waiting_holds(REPO, _load_roadmap_tasks(),
                                             set(read_state().get("parked_tasks") or ()))
        return goal_replan.held(REPO, _load_roadmap_tasks())
    except Exception:  # noqa: BLE001
        return {}


#: The (parked replan task, replan) pairs already told to Dan.
_REPLAN_PARKED_SEEN: set = set()


def _graph_replan_parked_report() -> None:
    """A task a goal replan added is parked (it failed for good): tell Dan once. The work
    waiting for it stays held, as the dependents of any parked task stay blocked, but the
    queue is no longer 'waiting on a replan' (`_graph_replan_holds(waiting=True)`).
    Best-effort."""
    from maestro import goal_replan

    if not _graph_goals_contract():
        return
    try:
        found = goal_replan.parked_added(REPO, _load_roadmap_tasks(),
                                         set(read_state().get("parked_tasks") or ()))
        for tid, rp in found.items():
            if (tid, rp) in _REPLAN_PARKED_SEEN:
                continue
            _REPLAN_PARKED_SEEN.add((tid, rp))
            append_journal("goal_replan_task_parked", f"{tid} (added by {rp})")
            notify_telegram(f"🗄 {tid}, the task replan {rp} added, failed and is parked: the "
                            f"work waiting for it stays held until you /unpark {tid} or "
                            "change the plan.")
    except Exception as exc:  # noqa: BLE001
        append_journal("goal_replan_failed", f"parked: {type(exc).__name__}: {exc}")


def _graph_repair_candidates(runnable: list) -> list:
    """The tasks the per-poll repair graduation checks: the runnable ones and, with
    `docs/GOALS.md`, those a replan holds (GDT-8 review L6) — an accepted task the hold
    keeps out of `runnable` must still graduate."""
    if not _graph_goals_contract():
        return runnable
    holds = _graph_replan_holds()
    ids = {str(t["id"]) for t in runnable}
    return [*runnable, *(t for t in _load_roadmap_tasks()
                         if str(t.get("id")) in holds and str(t.get("id")) not in ids)]


#: The replan holds last reported as what waits on Dan (`_graph_replan_waiting`).
_REPLAN_WAIT_SEEN: frozenset = frozenset()


def _graph_replan_waiting(holds) -> bool:
    """Whether work is left only because goal replans hold it (P13 GDT-8 review M3): then
    the queue is not exhausted. What waits (Dan's answer, his contract revision, an added
    task) is journaled `goal_replan_waiting` and told to Dan once per change, as the
    dispatch holds are journaled once per change."""
    from maestro import goal_replan

    global _REPLAN_WAIT_SEEN
    key = frozenset(dict(holds or {}).items())
    if key and key != _REPLAN_WAIT_SEEN:
        try:
            text = goal_replan.describe_holds(REPO, holds)
        except Exception as exc:  # noqa: BLE001
            text = f"{', '.join(sorted(holds))} held ({type(exc).__name__})"
        append_journal("goal_replan_waiting", text)
        notify_telegram(f"⏸ Nothing else to run; replanned goal work waits: {text}")
    _REPLAN_WAIT_SEEN = key
    return bool(key)


def _graph_graduate(store, task_id: str, artifacts=None, dan_channel=None) -> None:
    """Graduate an accepted run's task, then report it — the legacy completion path.

    Legacy, after `merge_and_eval` accepted: `mark_roadmap_complete`, `git push`, canary
    deploy for bot files, then `phase_report` with the dependency map. Graduation
    re-renders the map (`doc_depmap.render_png`) before the report attaches it, and the
    report records the ROADMAP signature it reflects, so the per-poll map push does not
    send the same map again. The task definition is read *before* graduation strips it
    from the ROADMAP, so the report names the task, not just its id.
    """
    from maestro.docs import followups as _followups

    task_def = get_task_by_id(task_id)
    run = _graph_accepted_run(store, task_id)
    # Review follow-ups across every run of the accepted workflow: the backlog for the
    # polish stage, or a `before_dependents` follow-up task queued in the graduation
    # commit (P13 GDT-11). An unreadable record queues nothing; it never blocks this.
    try:
        items = _followups.collect(store, artifacts, run["workflow_id"]) if run else []
    except Exception as exc:  # noqa: BLE001
        append_journal("followups_not_recorded", f"{task_id}: {exc}")
        items = []
    # P13 GDT-8: goal episodes, a milestone checkpoint and replanning, before the strip;
    # a routine replan's tasks and a checkpoint's follow-ups join the graduation commit.
    extra, checks = _graph_goal_replan(store, artifacts, task_def, run, dan_channel)
    items = [*items, *(_followups.FollowUp(text=t, round=0, node="goal checkpoint")
                       for t in checks)]
    # P13 GDT-11: a triage task's decisions become polish tasks in the graduation commit;
    # a polish task's report settles its items. Neither ever blocks the graduation.
    polish, settled = _graph_polish_graduation(task_id, task_def)
    recorded = mark_roadmap_complete(task_id, follow_ups=items,
                                     **({"replan_tasks": extra} if extra else {}),
                                     **({"polish_tasks": polish} if polish else {}))
    # G7 1c: a resumable task that completes forgets its sittings, as legacy did.
    _st = read_state()
    if (_st.get("resume_state") or {}).pop(task_id, None) is not None:
        write_state(_st)
    _graph_push_target()
    smoke = _graph_acceptance_smoke(store, run["run_id"]) if run else {
        "pass": True, "metrics": {}, "reason": "accepted"}
    append_journal("task_complete", f"{task_id} {smoke['reason'][:160]}")
    # B6: a merge that touched the project's bot files is canary-deployed; a failed
    # canary has already alerted Dan (canary_deploy.py), so the report is skipped.
    if run and _graph_touches_bot_files(store, artifacts, run["run_id"]):
        if not run_canary_deploy(task_id):
            append_journal("canary_reverted", f"{task_id} canary deploy failed")
            return
    phase_report(task_id, task_def, smoke, follow_ups="; ".join(
        t for t in (settled, recorded.summary() if recorded is not None else "") if t))
    _process_intake(prepare=False)


def _graph_polish_graduation(task_id: str, task_def) -> tuple[list, str]:
    """GDT-11 at a stage task's graduation: `(polish tasks to add, settled text)`. A triage
    task's decisions are applied (`backlog.apply_triage`; Dan is told the result once), a
    polish task's items settled from its report (`backlog.settle`). Best-effort."""
    from maestro import backlog

    at = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    try:
        if backlog.is_triage(task_def):
            polish, applied = backlog.apply_triage(REPO, task_def, at=at)
            if applied["unreadable"]:
                append_journal("polish_triage_unreadable",
                               f"{task_id}: every item kept: {applied['unreadable']}")
            append_journal("polish_triage_applied", applied["text"].removeprefix("🧹 "))
            notify_telegram(applied["text"])
            return polish, ""
        if backlog.is_polish(task_def):
            settled = backlog.settle(REPO, task_def, at=at)
            append_journal("polish_items_settled", settled["text"])
            return [], settled["text"]
    except Exception as exc:  # noqa: BLE001 — the backlog never blocks a graduation
        append_journal("polish_stage_failed", f"{task_id}: {type(exc).__name__}: {exc}")
    return [], ""


def _graph_bind_model_policy(store, task_id: str, routing_revision, *, task=None) -> str | None:
    """Bind `task_id` to the adopted model policy revision before a run opens or resumes.

    A task with no unfinished lifetime (unregistered, planned or terminal) starts a new
    binding; an unfinished one keeps its own. Routing built from any other revision is
    refused: rebuilding catalogs is not permission to change a running task's models.
    """
    from maestro.model_policy import PolicyError, freeze_task
    row = store.task(task_id)
    renew = row is None or row["status"] not in ("active", "waiting", "blocked")
    revision = freeze_task(REPO, task_id, renew=renew, task=task)["revision"]
    if revision != routing_revision:
        raise PolicyError(f"{task_id}: graph routing uses model policy {routing_revision}, "
                          f"task is bound to {revision}")
    return revision


def _legacy_bind_model_policy(task_id: str, *, renew: bool = False, task=None) -> str | None:
    """The legacy twin: a fresh launch renews, a retry keeps the task's binding."""
    from maestro.model_policy import freeze_task
    return freeze_task(REPO, task_id, renew=renew, task=task)["revision"]


def _model_policy_boundary() -> dict:
    """Adopt only with the controller's existing lock and complete idle proof."""
    from maestro.model_policy import PolicyError, PolicyStore
    from maestro.model_inventory import adopt
    store = PolicyStore()
    try:
        if store.current() is None:
            return {'status': 'unchanged'}
    except PolicyError:
        pass  # `adopt` defers an unreadable pointer like an unreadable revision
    controller = _state_module.control_store()
    # Un-migrated: this process is the project's only writer, so it adopts in place.
    outcome = adopt(REPO, store, controller=controller, owner=controller is None)
    unreadable = outcome.get('unreadable')
    # Called every poll: a shared policy this reader cannot read is reported once.
    if unreadable and unreadable not in _unreadable_policies_journaled:
        _unreadable_policies_journaled.add(unreadable)
        append_journal('model_policy_unreadable', outcome['reason'][:300])
    return outcome


_unreadable_policies_journaled: set[str] = set()


def _process_intake(*, prepare: bool = True) -> None:
    """Control-boundary upkeep; no independent dispatcher and no resume action."""
    from maestro import intake
    try:
        state = read_state()
        halted = state.get('halted') or HALT_FILE.exists()
        idle = not (state.get('in_flight') or state.get('waiting_on_dan')
                    or state.get('awaiting_dan_verification'))
        store = _state_module.control_store()
        if _config.engineering_runner() == 'legacy':
            _model_policy_boundary()
        if store is None and idle and intake.publication_ready(REPO):
            # Legacy migration uses the same state/store authority as the graph loop.
            from maestro.control.store import ControlStore
            ControlStore.open_controller(_state_module.CONTROL_DB).close()
            store = _state_module.control_store()
            write_state(state)
        intake.process(REPO, store, notify_intake, publish=idle,
                       prepare=prepare and not halted)
    except Exception as exc:
        append_journal('intake_deferred', str(exc)[:300])


def _graph_main(state: dict) -> int:
    """The graph runner's poll loop (`engineering.runner: graph`, P12A W0).

    Startup resumes every `running` run from its stored revision. Each poll then opens a
    run for every runnable task that has never had one, and drives every live run with
    `tick`, `ingest_results`, `settle` — `tick` already ingests and settles, and repeating
    both is idempotent. A task whose run finished or failed is not reopened here: that is
    plan repair (W7) or the operator's call. A tick that committed a plan repair reports
    `repaired_to`; the loop swaps the old runner for its successor and journals
    `graph_run_repaired`.

    Never enters the legacy launch path (`launch_implementer`, `launch_async_job`), and adds
    no context rotation of its own (P12A §4). Workers launch through
    `WorkflowRunner.launch_worker`, the single dispatch choke point. `start_run` and
    `WorkflowRunner` are looked up on their module on purpose, so a test can spy on them.
    """
    from maestro.taskgraph import normalize_spec
    from maestro.workflows import compiler as _wf_compiler
    from maestro.workflows import runner as _wf_runner

    from maestro.control.artifacts import ArtifactStore

    # P12 finding D21: transitively redundant deps are rewritten out of the ROADMAP once,
    # before any task is opened. Never blocks the loop.
    try:
        reduce_roadmap_deps()
    except Exception as exc:  # noqa: BLE001
        append_journal("roadmap_deps_not_reduced", str(exc)[:200])
    store = _state_module.control_store()
    dispatcher = _facade.TmuxDispatcher(runner=subprocess.run)
    try:
        _model_policy_boundary()
        routing = _graph_routing()
    except Exception as exc:
        # A catalog, policy or endpoint list the loop cannot read is not guessed around.
        print(f"[ERROR] graph routing: {exc}")
        append_journal("runner_config_error", f"graph routing: {exc}")
        return 1
    # W3: the router ranks on *verified* outcomes, so the statistics this process starts
    # with are the ones earlier runs actually recorded, not an empty table.
    from maestro.workflows import routing as _wf_routing

    routing.statistics = _wf_routing.load_statistics(store)
    # W5/W6: affected-check selection narrows the suite only once the stored shadow
    # comparisons qualify. A refused switch keeps the loop running in shadow mode.
    try:
        affected_mode = _config.affected_checks_mode()
    except _config.ConfigError as exc:
        print(f"[runner] {exc}")
        append_journal("runner_config_error", str(exc))
        return 1
    if affected_mode == "authoritative":
        from maestro import validation as _validation

        granted, why = _validation.qualify_affected(_validation.load_shadow_comparisons(store))
        if granted == "authoritative":
            routing.affected_mode = "authoritative"
        else:
            print(f"[affected] authoritative refused, staying in shadow: {why}")
            append_journal("affected_mode_refused", why)
    # W7: how many plan repairs a task may use (D-FAIL-2: one final run by default).
    try:
        routing.max_plan_repairs = _config.max_plan_repairs()
    except _config.ConfigError as exc:
        print(f"[runner] {exc}")
        append_journal("runner_config_error", str(exc))
        return 1
    # P12B F4/F5: how long an auth failure pauses a pool, and how long a node may wait on
    # a pool with an unknown reset. Read once; a bad value stops the loop.
    try:
        routing.auth_failure_pause_sec = _config.auth_failure_pause_sec()
        routing.max_blocked_wait_sec = _config.max_blocked_wait_sec()
    except _config.ConfigError as exc:
        print(f"[runner] {exc}")
        append_journal("runner_config_error", str(exc))
        return 1
    routing.on_blocked = _graph_on_blocked(routing.catalog)
    # Priority is live control input, unlike a run's frozen model policy. Copies
    # handed to existing runners read it at their next selected-cost admission.
    routing.project_priority_reader = _config.project_priority
    # W8: host admission for graph nodes, through the same round and the same shared
    # ledger the legacy loop uses. `WorkflowRunner.advance` consults `routing.admit`
    # before any claim; a finished attempt gives its amounts back through
    # `routing.on_attempt_finished`.
    host_admission = _GraphHostAdmission()
    routing.admit = host_admission.admit
    routing.on_attempt_finished = host_admission.on_attempt_finished
    artifacts = ArtifactStore(REPO / ".orchestrator" / "artifacts", store)
    # `kind: dan` checks: the operator channel `dan_confirm` nodes ask through (Telegram).
    dan_channel = _graph_dan_channel()
    runners: dict = {}
    # Task titles for the live-run rows in state.json, read once per run from the ROADMAP.
    titles: dict = {}
    # B8: each live run's `resource:` mutex labels, held until the run settles.
    run_labels: dict = {}
    # D20: each live run's declared `scope:` (`None` = undeclared), for write-set overlap.
    from maestro import writeset as _writeset
    run_scopes: dict = {}
    for row in store.conn.execute(
        "SELECT run_id, task_id, workflow_id, workflow_revision FROM task_runs"
        " WHERE status = 'running' ORDER BY started_at, run_id"
    ).fetchall():
        revision = _wf_compiler.load_revision(store, row["workflow_id"], row["workflow_revision"])
        if revision is None:
            append_journal("graph_run_unresumable",
                           f"{row['task_id']} run={row['run_id']}: revision "
                           f"{row['workflow_id']}@{row['workflow_revision']} is not stored")
            continue
        try:
            _graph_bind_model_policy(store, row["task_id"], routing.model_policy_revision)
            task_routing = _graph_task_routing(routing, row['task_id'])
        except Exception as exc:  # noqa: BLE001 — recovery never switches a task's models
            append_journal("graph_run_unresumable",
                           f"{row['task_id']} run={row['run_id']}: {str(exc)[:200]}")
            continue
        runners[row["run_id"]] = _wf_runner.WorkflowRunner(
            store, revision, run_id=row["run_id"], repo=REPO, dispatcher=dispatcher,
            artifacts=artifacts, routing=task_routing, dan_channel=dan_channel,
        )
        try:
            _graph_fallback(task_routing, task_routing.task_spec(row["task_id"]), revision)
        except Exception as exc:  # noqa: BLE001 — a task gone from the ROADMAP runs flagless
            append_journal("fallback_refused", f"{row['task_id']}: {str(exc)[:160]}")
        append_journal("graph_run_resumed", f"{row['task_id']} run={row['run_id']}")
        _resumed_task = get_task_by_id(row["task_id"]) or {}
        titles[row["task_id"]] = _task_title(_resumed_task)
        run_labels[row["run_id"]] = _resource_set(_resumed_task)
        run_scopes[row["run_id"]] = _writeset.declared_scope(_resumed_task)

    # G4: state.json and the status docs reflect the resumed runs from the first poll,
    # and the dependency map and status are regenerated at startup, as legacy does.
    for saved in read_state().get(GRAPH_RUNS_KEY) or ():
        if not isinstance(saved, dict) or saved.get('run_id') not in runners:
            continue
        for label in saved.get('mutex_labels') or ():
            if isinstance(label, str):
                host_admission.mutex_owners.setdefault(label, saved['run_id'])
    _graph_publish_live(_graph_live_runs(store, runners, titles,
                                       mutex_owners=host_admission.mutex_owners))
    run_dep_map()
    run_status()
    # The meta branch is reconciled once at startup, as legacy does (not every poll: it
    # copies the process docs), and again after each push of the target.
    print(f"[meta] {metabranch.reconcile_meta(REPO)}")

    compile_refused: set[str] = set()
    # B12: tasks waiting on Dan's answers, journaled once each rather than every poll.
    questions_pending: set[str] = set()
    resource_waiting: set[str] = set()
    write_waiting: set[str] = set()
    tick_failures: set[tuple[str, str]] = set()
    # P13 W8: `ctl cancel` / `ctl retry`, applied from the command inbox on each poll.
    operator_handlers = _graph_operator_handlers(store, runners)
    print(f"\n  Graph event loop starting ({len(runners)} run(s) resumed) …\n")

    while True:
        state = read_state()
        if state.get("halted") or state.get("paused_by_user"):
            print(f"[halt] halted={state.get('halted')} paused_by_user={state.get('paused_by_user')} — stopping.")
            append_journal("halt_respected",
                           f"halted={state.get('halted')} paused_by_user={state.get('paused_by_user')}")
            break
        if HALT_FILE.exists():
            print("[HALT] HALT sentinel — stopping.")
            append_journal("halt_detected", "")
            break

        poll_control_commands([])
        _drain_command_inbox(extra=operator_handlers)
        _process_intake()
        if _model_policy_boundary().get('status') == 'updated':
            refreshed = _graph_routing()
            routing.catalog = refreshed.catalog
            routing.role_strengths = refreshed.role_strengths
            routing.role_requirements = refreshed.role_requirements
            routing.role_independence = refreshed.role_independence
            routing.near_cost_margin = refreshed.near_cost_margin
            routing.model_policy_revision = refreshed.model_policy_revision
        # Legacy's per-poll HITL upkeep: a 24 h reminder for whatever still waits on Dan,
        # and the answer to a pending proposal (queue exhaustion sends one below).
        _send_hitl_reminders()
        poll_proposal_answer()
        # G7 item 4: Retry / Shelve on a failed-run notice, before runs are opened below.
        _graph_failed_answers(dan_channel)
        # Gate-passed /fix and /redo branches are merged here, by the single git owner.
        # Their inputs (a legacy diagnosis, a manual-action park) are legacy's, but a
        # branch they left behind still lands under the graph runner.
        apply_ready_self_fixes()
        apply_ready_redo()
        # ROADMAP edits between completions (Dan adding or re-scoping tasks) never reach
        # the completion report, so the refreshed map is pushed on a change, as legacy does.
        maybe_push_roadmap_map_change()

        # W5: stage eligible review follow-ups before exhaustion, then let the
        # ordinary ROADMAP frontier, write/resource gates and claim path decide.
        routing.refresh_pace(time.time())
        if _graph_sprint_pools(routing, time.time()):
            _graph_polish_stage(sprint=True)

        # ── open runs for ready tasks that have never had one ──
        # B14 prep tasks (dispatch:manual) compile to the `manual` template: the prep
        # node runs, then `human_action` waits for the operator's receipt.
        # P08: autonomous work ranked by priority, aging and leverage; prep after it.
        runnable = _order_runnable([t for t in parse_runnable_tasks()
                                    if _graph_polish_permitted(t)], time.time()) + parse_prep_tasks()
        complete_ids = get_completed_task_ids()
        parked = set(state.get("parked_tasks", []))
        resume_state = state.get("resume_state", {}) or {}
        now_utc = datetime.now(tz=timezone.utc)
        for run_id, live_runner in runners.items():
            task = get_task_by_id(live_runner.revision.task_id) or {}
            if task.get('polish_items'):
                run_labels[run_id] |= _resource_set(_graph_polish_task(task))
        held_resources = set().union(*(run_labels.get(r, set()) for r in runners))
        claimed_this_cycle: set[str] = set()
        # D20: live runs' write sets, read from git once per poll and only when needed.
        running_writes: dict | None = None
        opened_writes: dict = {}
        opened = {r["task_id"] for r in store.conn.execute("SELECT DISTINCT task_id FROM task_runs")}
        # G7 item 2: a task with a settled run is reopened only on an explicit marker
        # (`/unpark`, a Retry on the failed-run notice) or a due resumable cooldown. The
        # `resume_after` check below still holds a cooling task until it is due.
        reopening = _graph_reopenable(store, opened, state)
        opened -= reopening
        # An explicit request also retries a ROADMAP entry that failed to compile.
        reopen_markers = set(state.get(GRAPH_REOPEN_KEY) or {})
        compile_refused -= reopen_markers
        # An accepted run graduates its task, as legacy does after `merge_and_eval`, or it
        # stays open in the ROADMAP and nothing that depends on it can ever run. Checked
        # every poll, so a loop lost between settle and graduation repairs it on restart.
        accepted = {r["task_id"] for r in store.conn.execute(
            "SELECT DISTINCT task_id FROM task_runs WHERE status = 'succeeded'")}
        for task in _graph_repair_candidates(runnable) if accepted else runnable:
            if str(task["id"]) in accepted and str(task["id"]) not in complete_ids:
                _graph_graduate(store, str(task["id"]), artifacts, dan_channel=dan_channel)
        for task in runnable:
            task = _graph_polish_task(task)
            task_id = str(task["id"])
            if task_id in complete_ids or task_id in parked or task_id in opened \
                    or task_id in compile_refused:
                continue
            # A resumable task whose legacy sitting set a cooldown opens after it.
            _ra = (resume_state.get(task_id) or {}).get("resume_after")
            if _ra:
                try:
                    if datetime.fromisoformat(_ra) > now_utc:
                        continue
                except (TypeError, ValueError):
                    pass
            # B12: a task with a `questions:` block opens only once Dan answered every
            # one; `_questions_ready` asks the next unanswered question (idempotently).
            if not _questions_ready(task):
                if task_id not in questions_pending:
                    questions_pending.add(task_id)
                    append_journal("launch_skipped_questions",
                                   f"{task_id} waiting for Dan's answers")
                continue
            questions_pending.discard(task_id)
            # B8: a `resource:` label (e.g. eval-cpu) is held by one run at a time, as
            # the legacy launch loop holds it for one in-flight task.
            if _resources_conflict(task, held_resources, claimed_this_cycle):
                if task_id not in resource_waiting:
                    resource_waiting.add(task_id)
                    append_journal(
                        "launch_skipped_resource",
                        f"{task_id} waiting — resource conflict: "
                        f"{_resource_set(task) & (held_resources | claimed_this_cycle)}",
                    )
                continue
            resource_waiting.discard(task_id)
            # D20: parallel tasks must merge trivially, so a task whose predicted writes
            # (its `scope:`) overlap a live run's scope or changed files waits its turn.
            if running_writes is None:
                running_writes = {
                    r.revision.task_id: _writeset.run_write_set(
                        REPO, r.revision.task_id, run_scopes.get(run_id))
                    for run_id, r in runners.items()
                }
            _overlap = _writeset.conflicts(
                _writeset.declared_scope(task), {**running_writes, **opened_writes})
            if _overlap:
                if task_id not in write_waiting:
                    write_waiting.add(task_id)
                    append_journal(
                        "launch_skipped_write_overlap",
                        f"{task_id} waiting — overlaps "
                        + "; ".join(f"{t}: {', '.join(paths)}" for t, paths in _overlap.items()),
                    )
                continue
            write_waiting.discard(task_id)
            try:
                spec = normalize_spec(task)
                revision = _wf_compiler.compile_workflow(
                    spec, workflow_id=f"wf_{task_id}",
                    code_base_sha=_wf_compiler.head_sha(REPO, runner=subprocess.run),
                    # P12B F7: the graph loop is the only caller that wants the `diagnose`
                    # stage. Everything else — replay, compatibility checks, tooling that
                    # re-compiles a spec to compare it — gets the unescalated graph.
                    escalation=True,
                    merge_review=_graph_merge_review(task, spec, state),
                )
            except Exception as exc:
                compile_refused.add(task_id)
                append_journal("graph_compile_refused", f"{task_id}: {exc}")
                if task_id in reopen_markers:
                    # One refusal per request: the marker is spent, or the loop would
                    # recompile (and journal) the same broken entry every poll.
                    _st = read_state()
                    _st.setdefault(GRAPH_REOPEN_KEY, {}).pop(task_id, None)
                    write_state(_st)
                continue
            _graph_fallback(routing, spec, revision)
            try:
                model_revision = _graph_bind_model_policy(
                    store, task_id, routing.model_policy_revision, task=task)
                task_routing = _graph_task_routing(routing, task_id)
            except Exception as exc:  # noqa: BLE001 — an unbindable task is not started
                compile_refused.add(task_id)
                append_journal("model_policy_refused", f"{task_id}: {str(exc)[:200]}")
                continue
            graph_runner = _wf_runner.start_run(
                store, revision, repo=REPO, dispatcher=dispatcher,
                artifacts=artifacts, routing=task_routing, dan_channel=dan_channel,
                model_policy_revision=model_revision,
            )
            runners[graph_runner.run_id] = graph_runner
            titles[task_id] = _task_title(task)
            run_labels[graph_runner.run_id] = _resource_set(task)
            claimed_this_cycle |= run_labels[graph_runner.run_id]
            run_scopes[graph_runner.run_id] = _writeset.declared_scope(task)
            opened_writes[task_id] = run_scopes[graph_runner.run_id]
            append_journal("graph_run_opened",
                           f"{task_id} run={graph_runner.run_id} "
                           f"workflow={revision.workflow_id}@{revision.revision}")
            # Once per run: a task that already has a run is reopened only on an explicit
            # marker, so a refused or failing task cannot repeat this line every poll.
            _verb = "Preparing" if task.get("dispatch") == "manual" else "Starting"
            if task_id in reopening:
                _verb = "Reopening"
            if task_id in reopening or task_id in reopen_markers:
                _graph_reopened(task_id, graph_runner.run_id)
            notify_telegram(f"▶ {_verb} {task_id}: {_task_title(task)}")

        # ── drive every live run ──
        host_admission.begin(store, time.time(),
                             run_labels={r: run_labels.get(r, set()) for r in runners})
        # Persist selected ownership before an attempt may launch. An idle owner
        # retains the mutex over retry/restart using the existing atomic state file.
        if _graph_publish_live(_graph_live_runs(store, runners, titles,
                                              mutex_owners=host_admission.mutex_owners)):
            run_dep_map()
            run_status()
        for pool_id, driver, source in routing.reconcile_pauses(store, time.time()):
            append_journal("pool_recovered", f"{pool_id} driver={driver} source={source}")
        routing.refresh_pauses(store, time.time())
        route_waits: dict = {}
        for run_id, graph_runner in _graph_sprint_order(runners, routing, read_state(), runnable):
            try:
                report = graph_runner.tick()
                if report.repaired_to is not None:
                    # W7: the old run is canceled; its successor dispatches on the next
                    # poll through the ordinary `advance` path.
                    successor = graph_runner.successor
                    del runners[run_id]
                    runners[successor.run_id] = successor
                    run_labels[successor.run_id] = run_labels.pop(run_id, set())
                    run_scopes[successor.run_id] = run_scopes.pop(run_id, None)
                    append_journal(
                        "graph_run_repaired",
                        f"{graph_runner.revision.task_id} {run_id}→{successor.run_id} "
                        f"revision {graph_runner.revision.revision}→"
                        f"{successor.revision.revision}",
                    )
                    continue
                wait = _graph_route_wait(graph_runner, report)
                if wait is not None:
                    route_waits[graph_runner.revision.task_id] = wait
                graph_runner.ingest_results()
                graph_runner.settle()
            except Exception as exc:
                append_journal("graph_tick_failed", f"{graph_runner.revision.task_id} run={run_id}: {exc}")
                _record_graph_tick_failure(store, run_id, exc, tick_failures)
                continue
            row = store.conn.execute(
                "SELECT status FROM task_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None or row["status"] != "running":
                del runners[run_id]
                append_journal("graph_run_settled",
                               f"{graph_runner.revision.task_id} run={run_id} "
                               f"status={row['status'] if row else 'missing'}")
                settled_task = graph_runner.revision.task_id
                if row is not None and row["status"] == "failed" \
                        and _graph_cooldown_saved(store, artifacts, settled_task, run_id):
                    # G7 1c: a resumable sitting saved its progress; not a failure.
                    pass
                elif row is not None and row["status"] == "failed":
                    _graph_notify_failed(store, artifacts, settled_task, run_id,
                                         channel=dan_channel)
                elif row is not None and row["status"] == "succeeded" \
                        and settled_task not in get_completed_task_ids():
                    # Graduate on the settle itself; the per-poll check above is the
                    # repair for a loop lost in between.
                    _graph_graduate(store, settled_task, artifacts, dan_channel=dan_channel)
        _record_admission_waits(route_waits)
        # A wait on Dan whose run is gone (settled, canceled, repaired away) is not a wait.
        _verify.prune_awaiting(runners.keys())
        # P13 GDT-8: Dan's answers to waiting replans; approved tasks land in the ROADMAP.
        _graph_replan_poll(dan_channel)
        # P13 GDT-10: Dan's answers to the completion gate's questions.
        _graph_goal_finish_poll(dan_channel)
        # P13 GDT-12: Dan's answers to improvement proposals (they hold nothing), applied.
        _graph_improvement_poll(dan_channel)
        _graph_replan_parked_report()
        # G4: the live runs into state.json (`graph_runs`, `phase`), which /status,
        # /progress and the status docs read; the map and status are regenerated when a
        # run opened or settled, as legacy does after a launch or a completion.
        if _graph_publish_live(_graph_live_runs(store, runners, titles,
                                              mutex_owners=host_admission.mutex_owners)):
            run_dep_map()
            run_status()

        # A replan question (P13 GDT-8) holds work but belongs to no run: not exhausted;
        # nor is work a replan holds (Dan's contract revision, an added task).
        if not runners and not runnable and not read_state().get("waiting_on_dan", {}) \
                and not read_state().get(_verify.QUESTIONS_STATE_KEY) \
                and not _graph_replan_waiting(_graph_replan_holds(waiting=True)):
            # P13 GDT-11: open review follow-ups first become a polish stage, whose tasks
            # run next. GDT-10 (R54): with a format-2 goals contract an empty queue is done
            # only when the completion gate says so; until then the loop idles and polls on.
            polishing, finish = _graph_at_exhaustion(dan_channel)
            if polishing or _goal_finish_holds(finish):
                _idle_heartbeat_maybe(runnable)
                _graph_improvement_weekly(store, artifacts, dan_channel, runnable)
                if _self_update_maybe():
                    return 0
                time.sleep(POLL_INTERVAL)
                continue
            print("\n[done] Queue exhausted.")
            append_journal("queue_exhausted", "")
            # P13 GDT-12 (R57): the project-end improvement analysis, once per HEAD.
            _graph_improvement(store, artifacts, dan_channel, "project_end")
            state = read_state()
            state["phase"] = {"id": None, "title": None, "status": "idle"}
            write_state(state)
            if AUTO_PROPOSE:
                generate_proposals()
            else:
                _notify_proposal_test_due(_goal_finish_note(finish))
            _graph_improvement_weekly(store, artifacts, dan_channel, runnable)
            if _self_update_maybe():  # nothing live: the commonest idle moment
                return 0
            time.sleep(POLL_INTERVAL)
            continue

        if not runners:
            _idle_heartbeat_maybe(runnable)
            # P13 GDT-12: the weekly improvement analysis, only when nothing is runnable
            # either (runnable but unlaunched work is not idle; review M1).
            _graph_improvement_weekly(store, artifacts, dan_channel, runnable)
            # M3 D5: the idle moment, nothing live, is when maestro may update itself.
            if _self_update_maybe():
                return 0
        time.sleep(POLL_INTERVAL)

    run_status()
    return 0


def main() -> int:
    print("=" * 62)
    print("  B5.1 ORCHESTRATOR — Verification-Gated Parallelism")
    print("=" * 62)
    if subprocess.run(["tmux", "has-session", "-t", TMUX_SESSION],
                      capture_output=True).returncode != 0:
        print(f"[ERROR] tmux session '{TMUX_SESSION}' not found.")
        return 1

    state = read_state()

    # Problem 9 — belt-and-suspenders: launcher already checked halted; check again.
    # paused_by_user alone: stay alive in a Telegram-only poll loop so /resume and
    # /approve can be received while the orchestrator is parked.
    if state.get("halted"):
        print(f"[halt] halted=True — exiting.")
        append_journal("halt_respected", "halted=True")
        return 0
    # Dan's button taps are acknowledged on arrival, not at the next poll (pilot feedback
    # 2026-09-26); commands are still processed by the loop's own poll_control_commands.
    start_update_listener()
    if state.get("paused_by_user"):
        print("[paused] paused_by_user=True — entering Telegram poll loop.")
        append_journal("paused_poll_start", "paused_by_user=True waiting for /resume or /approve")
        while True:
            _pstate = read_state()
            if _pstate.get("halted") or HALT_FILE.exists():
                append_journal("halt_respected", "halted=True during paused poll")
                return 0
            if not _pstate.get("paused_by_user"):
                print("[paused] paused_by_user cleared — resuming main loop.")
                break
            _pfl = _pstate.get("in_flight", [])
            poll_control_commands(_pfl)
            _drain_command_inbox()
            _process_intake()
            # Queue exhaustion parks the loop *while* it waits for Dan's pick of the
            # proposals; the answer clears `paused_by_user`, so it is read here, where the
            # loop actually waits, and not only in the running loops.
            poll_proposal_answer()
            time.sleep(POLL_INTERVAL)
        state = read_state()

    # P12A W0 / D08: exactly one controller loop per project run, chosen once per process.
    try:
        runner_kind = _config.engineering_runner()
    except _config.ConfigError as exc:
        print(f"[runner] {exc}")
        append_journal("runner_config_error", str(exc))
        return 1
    refusal = _runner_switch_refusal(runner_kind, state)
    if refusal:
        print(f"[runner] {refusal}")
        append_journal("runner_switch_refused", refusal)
        return 1
    if runner_kind == "graph":
        if _state_module.control_store() is None:
            # The graph runner keeps its runs only in the control database, so opting in
            # creates it. That moves this project's state authority into SQLite
            # (`maestro.state.control_store`): state.json is read as-is until the next
            # write commits it as the first state event.
            from maestro.control.store import ControlStore

            ControlStore.open_controller(_state_module.CONTROL_DB).close()
            _state_module.control_store()
            append_journal("control_db_created",
                           f"{_state_module.CONTROL_DB} (engineering.runner=graph)")
        return _graph_main(state)

    # B10 — reconcile in_flight against live tmux windows + workspace sentinels at startup
    # (also runs every poll cycle via reconcile_in_flight call inside the event loop)
    launch_times: dict[str, float] = {}
    in_flight: list[dict] = _reconcile_and_persist_startup_state(state, launch_times)
    # P08/P10B: restart path — adopt live work (slots and counted amounts), drop dead rows
    _sync_host_ledger(in_flight, _live_counted(in_flight, state))
    run_dep_map()
    run_status()

    # process/project git separation — reconcile the meta branch once at startup.
    # Not inside the event loop: reconcile_meta's own docstring says it's safe to call
    # "at orchestrator startup ... and after every main push", not every poll cycle —
    # calling it every iteration would shutil.copytree docs/graph-engineering/ on every
    # poll for no reason.
    print(f"[meta] {metabranch.reconcile_meta(REPO)}")

    # B11: load persistent retry_counts from state.json instead of
    # starting fresh — prevents infinite loops on process restart.
    retry_counts: dict[str, int]   = read_state().get("retry_counts", {})
    prev_cap = -1
    print(f"\n  Event loop starting (default cap={CONCURRENCY_CAP}) …\n")

    while True:
        # Problem 9 — check halt flags at top of every iteration (re-read state each time)
        state = read_state()
        if state.get("halted") or state.get("paused_by_user"):
            print(f"[halt] halted={state.get('halted')} paused_by_user={state.get('paused_by_user')} — stopping.")
            append_journal("halt_respected",
                           f"halted={state.get('halted')} paused_by_user={state.get('paused_by_user')}")
            break

        if HALT_FILE.exists():
            print("[HALT] HALT sentinel — stopping.")
            append_journal("halt_detected", "")
            break

        # B — usage-limit reactive net: if an implementer's quota-exhausted exit (or
        # the proactive launcher path) set paused_until, stop the loop so the process
        # exits and the watchdog reschedules a resume at the reset time. Mirrors the
        # existing cap==0 rate-limit break below.
        pu_epoch = _paused_until_epoch(state)
        if pu_epoch and datetime.now(timezone.utc).timestamp() < pu_epoch:
            print(f"[paused] paused_until={state.get('paused_until')} — stopping; "
                  f"watchdog will resume at reset.")
            append_journal("paused_until_break", f"paused_until={state.get('paused_until')}")
            break

        # B6: poll Telegram control commands (/status /pause /resume /halt)
        poll_control_commands(in_flight)
        _drain_command_inbox()
        _process_intake()
        _send_hitl_reminders()
        # B6: check if Dan answered a pending proposal
        poll_proposal_answer()
        # D: merge/surface any self-fix branches the runner has gate-passed (the
        # orchestrator is the single git owner, so the merge happens here).
        apply_ready_self_fixes()
        # /redo: land gate-passed deliverable rewrites the same way (single git owner).
        apply_ready_redo()
        # Handoff B: re-check tasks parked on an async verification; finalize when the
        # awaited eval/artifact lands, surface genuine failures + timeouts.
        poll_awaiting_verifications(in_flight)
        # Push a refreshed dependency map whenever ROADMAP.md changed since the last map
        # we sent — manual edits / re-scopes don't hit the completion-event path that
        # normally ships the map, so without this Dan can hold a map missing new tasks.
        maybe_push_roadmap_map_change()

        cap, five_pct = get_effective_cap()
        # M2/D2 — quota pressure is a reason to move work, not only a reason to wait. If
        # a fallback backend took an in-flight task this poll, the reading below is about
        # a backend this loop is no longer running on, so throttling or pausing on it
        # would park work that has somewhere else to run. When nothing moved (no
        # fallback, nothing handed over) `_threshold_switches` returns 0 and the original
        # throttle/pause path runs untouched.
        #
        # The cap==0 safety valve must still fire whenever ANY in-flight work is left
        # stranded on a still-exhausted backend: `_threshold_switches` can move some
        # entries while leaving others (wrong role, worktree already gone, or simply no
        # fallback available) untouched, and moving *some* work must not exempt the loop
        # from protecting what didn't move. "Stranded" is read from pressure readings
        # taken *before* this poll's switching, keyed by session_id, so a successfully
        # switched entry (new session_id, new backend) never re-triggers the "no sample ==
        # treat as pressure" fallback (G6) on the fresh backend it just landed on.
        # A2 — one usage sample per backend per poll, shared by the pressure check below
        # and the `usage.json` write, so a backend is never asked twice in the same poll
        # (CodexBackend.usage() spawns a short-lived `codex app-server` subprocess).
        usage_cache: dict = {}
        stranded_before = {
            e.get("session_id") for e in in_flight
            if _under_usage_pressure(_entry_backend(e), five_pct, usage_cache)
        }
        switched = _threshold_switches(in_flight, launch_times, five_pct, usage_cache)
        still_stranded = (not in_flight) or bool(
            stranded_before & {e.get("session_id") for e in in_flight}
        )
        # A2 — sample+persist usage ourselves so `get_effective_cap` sees a real number
        # even with no interactive Claude Code session open to run the statusline hook
        # that used to be the only writer of this file (G1/G2).
        _write_usage_sample(in_flight, usage_cache)
        if not switched:
            if cap != prev_cap:
                if prev_cap >= 0 and cap < CONCURRENCY_CAP:
                    append_journal("concurrency_throttled", f"cap={cap} five_h={five_pct:.0f}%")
                    print(f"  [throttle] Concurrency cap={cap} (5h={five_pct:.0f}%)")
                prev_cap = cap
        if cap == 0 and still_stranded:
            print(f"  [throttle] 5h={five_pct:.0f}% >= {PAUSE_PCT}% — pausing.")
            append_journal("rate_limit_pause", f"five_h={five_pct:.0f}%")
            break

        # A4/D4 — a task whose own context has crossed its model's `prepare_handoff_high`
        # is rotated onto a fresh session of the SAME backend, through the same
        # `switch_task` path and the same SWITCH sentinel: checkpoint, then relaunch in
        # the same worktree with a handoff brief. Deliberately *after* the quota block
        # above and after its `break`: moving off an exhausted backend is the more urgent
        # of the two, a task that just switched is already on a fresh session and cannot
        # need rotating, and there is nothing to gain by rotating a task one line before
        # pausing the whole loop. It reuses `usage_cache`, so no driver is sampled again.
        _context_rotations(in_flight, launch_times, usage_cache)

        still_running: list[dict] = []
        newly_done:    list[dict] = []
        newly_failed:  list[dict] = []

        # B10 — per-poll stale reconcile: reap entries whose tmux window is gone with
        # no sentinel so zombies are caught within one cycle, not only at startup.
        in_flight = reconcile_in_flight({"in_flight": in_flight}, launch_times)

        # B — if reconcile just set paused_until (a usage-limit exit), stop now so we
        # don't launch a fresh task into the same wall. The watchdog resumes at reset.
        if _paused_until_epoch(read_state()) > datetime.now(timezone.utc).timestamp():
            print("  [paused] usage-limit backoff set this poll — stopping; watchdog resumes at reset.")
            append_journal("paused_until_break", "set by reconcile usage-limit net")
            break

        for entry in in_flight:
            sid       = entry["session_id"]
            workspace = WORKSPACES / sid
            timed_out = _timeout_expired(entry, launch_times)
            if not _native_git_reports_ready(workspace, running=tmux_window_exists(entry.get('window', ''))):
                still_running.append(entry)
                continue
            if timed_out:
                print(f"  [timeout] {entry['task_id']} timed out")
                append_journal("implementer_timeout", entry["task_id"], session_id=sid)
            if timed_out:
                newly_failed.append(entry)
            elif (workspace / "PAUSED").exists():
                still_running.append(entry)
            elif (workspace / "DONE").exists():
                newly_done.append(entry)
            elif (workspace / "FAILED").exists():
                newly_failed.append(entry)
            else:
                still_running.append(entry)
        in_flight = still_running

        # ── Handle failures ──
        for entry in newly_failed:
            task_id   = entry["task_id"]
            workspace = WORKSPACES / entry["session_id"]
            from maestro.backends import git_staging
            reason = git_staging.publication_timeout(workspace)
            if reason is None:
                reason = ((workspace / "FAILED").read_text()[:200]
                          if (workspace / "FAILED").exists() else "unknown")
            # B10 — don't blind-retry a timeout-with-no-progress death.
            # A task that ran until the ceiling (no DONE, no INCOMPLETE) showed no
            # forward progress; relaunching it immediately would just time out again.
            # Escalate / park instead of burning the one retry allowance.
            if reason.startswith("timeout after"):
                print(f"  [park] {task_id}: timed out with no progress — parking (not retrying)")
                append_journal("timeout_no_progress_parked",
                               f"{task_id} reason={reason}", session_id=entry["session_id"])
                park_failed(task_id, reason)
                _remove_from_state(entry)
            elif retry_counts.get(task_id, 0) < 1:
                retry_counts[task_id] = retry_counts.get(task_id, 0) + 1
                # B11: persist retry_counts to survive process restarts
                _rc_state = read_state()
                _rc_state["retry_counts"] = retry_counts
                write_state(_rc_state)
                print(f"  [retry] {task_id}: retry #{retry_counts[task_id]}")
                append_journal("implementer_retry", f"{task_id} reason={reason}",
                               session_id=entry["session_id"])
                _do_retry(task_id, entry, reason, retry_counts, in_flight)
            else:
                print(f"  [escalate] {task_id}: escalating to Dan")
                append_journal("implementer_failed_escalated",
                               f"{task_id} reason={reason}", session_id=entry["session_id"])
                park_failed(task_id, reason)
                _remove_from_state(entry)

        # ── Handle completions ──
        for entry in newly_done:
            task_id   = entry["task_id"]
            workspace = WORKSPACES / entry["session_id"]
            worktree  = Path(entry["worktree"])

            result_path = workspace / "result.json"
            if not result_path.exists():
                print(f"  [error] {task_id}: DONE but no result.json — escalating")
                append_journal("missing_result_json", task_id, session_id=entry["session_id"])
                park_failed(task_id, "DONE sentinel present but result.json missing")
                _remove_from_state(entry)
                continue

            impl = json.loads(result_path.read_text())
            print(f"\n  [done] {task_id}: tests.pass={impl.get('tests',{}).get('pass')} "
                  f"summary={impl.get('summary','')[:80]}")

            # Problem 10 — self-modifying tasks must be merged manually by Dan
            task_def = get_task_by_id(task_id)

            # B14 auto-prep: a dispatch:manual task that reaches DONE was a PREP run (the
            # human step is still Dan's). Harvest its single dan_action, merge the prep,
            # and park as a manual-action — do NOT run the eval gate or graduate yet.
            if task_def and task_def.get("dispatch") == "manual":
                handle_prep_done(task_id, task_def, entry, impl)
                continue

            if task_def and task_def.get("self_modifying"):
                print(f"  [self_modifying] {task_id}: escalating to Dan for manual merge")
                append_journal("self_modifying_escalated",
                               f"{task_id} requires manual out-of-band merge + relaunch",
                               session_id=entry["session_id"])
                _danreq(
                    f"Task {task_id} is self-modifying (edits orchestrator runtime). "
                    f"Merge manually: git merge {entry.get('branch','<branch>')} "
                    f"then restart watchdog.",
                    ["Merge + restart watchdog", "Shelve task"]
                )
                _remove_from_state(entry)
                continue

            # Run verification gate (double gate — implementer can't bypass by writing DONE early)
            gate_ok, gate_msg = run_verification_gate(task_id, workspace, worktree,
                                                      pre_merge_only=True)
            print(f"  [verify] {task_id}: gate={'OK' if gate_ok else 'FAILED'} — {gate_msg[:120]}")
            if not gate_ok:
                (workspace / "DONE").unlink(missing_ok=True)
                reason = f"verification gate: {gate_msg}"
                append_journal("verification_gate_failed",
                               f"{task_id} {reason[:200]}", session_id=entry["session_id"])
                # Resumable (multi-day/quota-bound): a failed coverage gate is expected
                # mid-job. If the sitting advanced the work, save it + re-queue after a
                # cooldown instead of retrying (can't beat a daily quota) or failing.
                if (task_def or {}).get("resumable"):
                    handle_incomplete(task_id, task_def or {}, entry, gate_msg)
                    continue
                # kind:script tasks have no LLM implementer to re-brief — the `run:` cmd is
                # authored in ROADMAP. A fresh Sonnet implementer would brute-force the gate
                # (defeating the deterministic canary), so park for Dan to revise the script.
                if (task_def or {}).get("kind") == "script":
                    park_failed(task_id, reason)
                    _remove_from_state(entry)
                elif retry_counts.get(task_id, 0) < 1:
                    retry_counts[task_id] = retry_counts.get(task_id, 0) + 1
                    print(f"  [verify] Re-briefing {task_id} with gate failures")
                    _do_retry(task_id, entry, reason, retry_counts, in_flight)
                else:
                    park_failed(task_id, reason)
                    _remove_from_state(entry)
                continue

            # Sonnet proof review for manual verifications
            verifications = (task_def.get("verifications") or []) if task_def else []
            impl_verifs   = impl.get("verifications", {})
            from maestro.model_policy import legacy_writer_routes
            sonnet_ok, sonnet_msg = sonnet_review_proofs(task_id, verifications, impl_verifs,
                                                       writer_routes=legacy_writer_routes(entry))
            if not sonnet_ok:
                print(f"  [verify] {task_id}: proof review failed — re-briefing")
                (workspace / "DONE").unlink(missing_ok=True)
                reason = f"Sonnet proof review: {sonnet_msg}"
                append_journal("sonnet_review_failed",
                               f"{task_id} {reason[:200]}", session_id=entry["session_id"])
                if retry_counts.get(task_id, 0) < 1:
                    retry_counts[task_id] = retry_counts.get(task_id, 0) + 1
                    _do_retry(task_id, entry, reason, retry_counts, in_flight)
                else:
                    park_failed(task_id, reason)
                    _remove_from_state(entry)
                continue

            # Merge and eval (Problem 11 no-op refusal is inside merge_and_eval)
            # HITL gate: pause before merge when global hitl_mode is on, or when the
            # task itself is mode=needs-dan (its brief promises Dan reviews before merge)
            impl_summary = impl.get("summary", "")[:200] if isinstance(impl, dict) else ""
            task_mode = (task_def or {}).get("mode", "autonomous")
            if read_state().get("hitl_mode", False) or task_mode == "needs-dan":
                park_for_dan(entry, impl_summary)
                still_running.append(entry)
                continue
            accepted, smoke = merge_and_eval(entry)
            # Whatever this project's adapter measured, named by its own keys. These
            # lines used to read `recall_at_5` and print `Recall@5=` — one project's
            # metric, reported as missing on every other one.
            shown = metrics.summary(smoke)
            _remove_from_state(entry)
            if accepted:
                if defer_merged_verification(entry, smoke, in_flight):
                    continue
                print(f"  ✓ {task_id} merged and accepted ({shown})")
                append_journal("task_complete", f"{task_id} {shown}",
                               session_id=entry["session_id"])
                remove_worktree(worktree)
                mark_roadmap_complete(task_id)
                # Resumable task finally reached 100% coverage → drop its cooldown record.
                _st = read_state()
                if _st.get("resume_state", {}).pop(task_id, None) is not None:
                    write_state(_st)
                subprocess.run(["git", "push", "origin", "main"],
                               cwd=str(REPO), capture_output=True)
                # B6: canary deploy if bot files were touched
                branch = entry.get("branch", "")
                if branch and _touches_bot_files(branch):
                    if not run_canary_deploy(task_id):
                        append_journal("canary_reverted", f"{task_id} canary deploy failed")
                        # canary_deploy.py already sent Telegram alert; skip phase_report
                        run_dep_map(); run_status()
                        continue
                # B6: phase completion report + dep map attachment
                phase_report(task_id, get_task_by_id(task_id), smoke)
            elif smoke.get("noop"):
                # The branch introduced no commits. `park_regression` is wrong here: it
                # escalates to Dan but registers neither `parked_tasks` nor `waiting_on_dan`,
                # so the task stays runnable and relaunches every POLL_INTERVAL. Observed
                # live on 2026-08-20: a `kind: script` task looped 11 times in ~7 minutes.
                #
                # A `kind: script` task parks on the first no-op — its `run:` command is
                # authored in ROADMAP and deterministic, so a rerun reproduces the same
                # nothing, and there is no implementer to re-brief (the same reasoning the
                # verification gate above already applies). Anything with an implementer
                # gets exactly one re-brief first, mirroring the gate/review retry paths.
                reason_merge = smoke.get("reason", "")
                print(f"  ✗ {task_id} produced no commits — {reason_merge[:80]}")
                is_script = (task_def or {}).get("kind") == "script"
                if _noop_merge_action(task_def, task_id, retry_counts) == "retry":
                    retry_counts[task_id] = retry_counts.get(task_id, 0) + 1
                    _rc_state = read_state()
                    _rc_state["retry_counts"] = retry_counts
                    write_state(_rc_state)
                    print(f"  [retry] {task_id}: re-briefing after a no-op merge")
                    append_journal("merge_noop_retry", f"{task_id} retry={retry_counts[task_id]}",
                                   session_id=entry["session_id"])
                    _do_retry(task_id, entry, reason_merge, retry_counts, in_flight)
                else:
                    notify_telegram(f"⚠ {task_id} produced no commits — parked.")
                    append_journal("merge_noop_parked",
                                   f"{task_id} kind={'script' if is_script else 'implementer'}",
                                   session_id=entry["session_id"])
                    park_failed(task_id, reason_merge)
                    remove_worktree(worktree)
            else:
                reason_merge = smoke.get("reason", "")
                print(f"  ✗ {task_id} smoke/merge failed ({shown}) — {reason_merge[:80]}")
                notify_telegram(f"⚠ {task_id} smoke/merge failed ({shown}), reverted.")
                park_regression(task_id, smoke)
                remove_worktree(worktree)
            run_dep_map()
            run_status()

        # ── Launch new tasks ──
        # B14: prep tasks (dispatch:manual) append after real runnable work so autonomous
        # tasks fill slots first; they flow through the SAME machinery — the prep brief is
        # routed inside _make_brief, and completion routes via the dispatch:manual check.
        # P08: autonomous and prep groups are each ranked (priority, aging, leverage);
        # prep stays after autonomous work.
        _now_epoch       = time.time()
        runnable         = (_order_runnable(parse_runnable_tasks(), _now_epoch)
                            + parse_prep_tasks())
        running_task_ids = {e["task_id"] for e in in_flight}
        # P08/P10B: heartbeat this project's host reservations; releases finished entries.
        _counted_live = _live_counted(in_flight, read_state())
        _sync_host_ledger(in_flight, _counted_live)

        # Problem 12 — build the full complete set to dedup against
        complete_ids = get_completed_task_ids()
        try:
            content = ROADMAP_FILE.read_text(encoding="utf-8")
            for raw in re.findall(r"```yaml\n(.*?)```", content, re.DOTALL):
                t = yaml.safe_load(raw)
                if isinstance(t, dict) and t.get("status") == "complete":
                    complete_ids.add(str(t["id"]))
        except Exception:
            pass

        # P08 §1.3: only agent lanes count against the cap — a script task holds no slot.
        free_slots   = _agent_slots_free(in_flight, cap)
        launched_any = False
        _launch_state = read_state()
        resume_state = _launch_state.get("resume_state", {})
        # B11: parked tasks — read once per cycle, not per candidate
        parked_tasks = set(_launch_state.get("parked_tasks", []))
        now_utc      = datetime.now(tz=timezone.utc)
        # Waiting tasks retain execution ownership outside in_flight. This includes
        # async jobs and already-integrated ordinary tasks awaiting deployed proof
        # or repair; neither may replay its original execution while held here.
        waiting_task_ids = {
            p.get("task_id") for p in _launch_state.get("waiting_on_dan", {}).values()
        }

        # B8: resource-mutex — collect labels held by every currently in-flight task.
        held_resources: set[str] = set()
        for _e in in_flight:
            _t = get_task_by_id(_e["task_id"])
            if _t:
                held_resources |= _resource_set(_t)
        # Async read-only jobs hold their resource label too (e.g. eval-cpu), but live in
        # waiting_on_dan rather than in_flight — fold them in so a second eval-cpu task
        # can't thrash the CPU while a self-monitored eval is still running.
        for _p in _launch_state.get("waiting_on_dan", {}).values():
            if _p.get("kind") == "awaiting-verification":
                _t = get_task_by_id(_p.get("task_id", ""))
                if _t:
                    held_resources |= _resource_set(_t)
        # Labels claimed by tasks launched earlier in THIS poll cycle (before in_flight
        # is updated in state.json); prevents two tasks from claiming the same label in
        # the same poll when the cap allows multiple launches.
        claimed_this_cycle: set[str] = set()
        # P10B: built on the first candidate that declares resources — a roadmap with no
        # `resources:` blocks never detects capacity or reads other projects' rows.
        admission: "scheduling.AdmissionRound | None" = None
        admission_waits: dict[str, dict] = {}

        for task in runnable:
            task_id  = task["id"]
            is_async = task.get("dispatch") == "async-job"
            is_script = task.get("kind") == "script"
            # Async read-only jobs and `kind: script` tasks hold no agent session / slot —
            # a full slate of implementer slots must not delay either (P08 §1.3). This
            # used to `break`, which stopped a ready script task queued behind an agent
            # task; `continue` skips only the agent lane.
            if not (is_async or is_script) and free_slots <= 0:
                continue
            if task_id in running_task_ids:
                continue
            # The poller / observation / repair owns every waiting task. Skip before
            # resource admission; independent tasks, including repairs, remain eligible.
            if task_id in waiting_task_ids:
                continue
            if task_id in complete_ids:  # Problem 12 — skip already-complete tasks
                append_journal("launch_skipped_complete", f"{task_id} already complete")
                continue
            # B11: skip tasks parked after failure — they need /unpark before relaunch
            if task_id in parked_tasks:
                continue
            # Resumable task in cooldown after an INCOMPLETE sitting — re-queue later.
            _ra = resume_state.get(task_id, {}).get("resume_after")
            if _ra:
                try:
                    if datetime.fromisoformat(_ra) > now_utc:
                        continue
                except ValueError:
                    pass

            # B8: resource-mutex — skip this candidate if any of its resource labels is
            # already held by an in-flight task OR was claimed earlier in this cycle.
            if _resources_conflict(task, held_resources, claimed_this_cycle):
                append_journal(
                    "launch_skipped_resource",
                    f"{task_id} waiting — resource conflict: {_resource_set(task) & (held_resources | claimed_this_cycle)}",
                )
                continue

            # B12: pre-implementation question gate. If the task declares a
            # questions: block, every question must be answered by Dan before we
            # launch. _questions_ready asks the next unanswered one (idempotent)
            # and returns False, parking the task without consuming a launch slot.
            if not _questions_ready(task):
                append_journal("launch_skipped_questions",
                               f"{task_id} waiting for Dan's answers")
                continue

            ts_str    = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
            sid       = f"impl-{task_id}-{ts_str}"

            # P10B: host resource admission — last of the gates, so what it takes is only
            # ever held by a task that is about to launch (and given back if it does not).
            try:
                resource_request = scheduling.ResourceRequest.from_task(task)
            except ResourceSpecError as exc:
                admission_waits[task_id] = {"reason": "invalid_resources", "detail": str(exc)}
                continue
            resource_amounts = dict(resource_request.counted)
            candidate = host_resources = None
            if resource_amounts:
                # P12A W9: larger than the whole host would wait forever — park it.
                oversized = scheduling.exceeds_capacity(resource_request, _host_capacity())
                if oversized is not None:
                    append_journal("launch_parked_oversized", f"{task_id} {oversized}")
                    park_failed(task_id, oversized)
                    continue
                if admission is None:
                    admission = _admission_round(_counted_live, _now_epoch)
                candidate = scheduling.Candidate(
                    candidate_id=str(task_id), request=resource_request,
                    eligible_since=_ELIGIBILITY.since.get(str(task_id), _now_epoch),
                )
                answer = admission.admit(candidate)
                if answer.admitted:
                    host_resources, answer = _reserve_host_resources(
                        task_id, _async_run_id(task_id) if is_async else sid, resource_amounts
                    )
                if not answer.admitted:
                    admission.withdraw(candidate)
                    admission_waits[task_id] = {"reason": answer.reason, "detail": answer.detail}
                    continue

            def _give_back() -> None:
                if candidate is not None:
                    admission.withdraw(candidate)
                _release_host_slot(host_resources)

            # Long async read-only job: the orchestrator launches the job itself (detached
            # tmux) and parks `awaiting-verification` — no Claude session, no worktree, no
            # in_flight slot. Re-checked by poll_awaiting_verifications, reaper-exempt.
            # (Already-parked async jobs were skipped early, above the resource check.)
            if is_async:
                print(f"\n  [async-job] {task_id}: {task.get('title','')}")
                notify_telegram(f"▶ Launching async job {task_id}: {task.get('title','')}")
                if launch_async_job(task, resources=resource_amounts):
                    waiting_task_ids.add(task_id)  # dedup within this same cycle
                else:
                    _give_back()
                continue

            workspace = WORKSPACES / sid
            workspace.mkdir(parents=True, exist_ok=True)
            branch    = f"impl-{task_id.lower()}-{ts_str}"
            worktree  = worktree_path_for(task_id)
            print(f"\n  [launch] {task_id}: {task.get('title','')}")
            host_slot = None
            model_revision = None
            launched_on = ""
            if not is_script:
                try:
                    # A fresh launch starts the task's binding to the adopted model policy.
                    model_revision = _legacy_bind_model_policy(task_id, renew=True, task=task)
                    from maestro.model_policy import task_model_context
                    with task_model_context(REPO, task_id):
                        launched_on = _launch_backend()
                except Exception as exc:  # noqa: BLE001 — an unbindable task is not launched
                    _release_host_slot(host_slot)
                    _give_back()
                    append_journal("model_policy_refused", f"{task_id} {str(exc)[:160]}")
                    park_failed(task_id, f"Model policy refused: {str(exc)[:200]}")
                    continue
                # P08: reserve before creating the agent, using the same decision as
                # launch and recording. Later operator/quota edits affect the next launch.
                host_slot = _reserve_host_slot(task_id, sid, launched_on)
                if host_slot is None:
                    _give_back()
                    continue
            try:
                create_worktree(task_id, worktree, branch)
            except subprocess.CalledProcessError as exc:
                _release_host_slot(host_slot)
                _give_back()
                # Park (idempotent → escalates to Dan exactly once) instead of a bare
                # continue. A bare continue retried every poll and re-sent the Telegram
                # "Starting" line each time — the TUNE1 spam-storm of 2026-06-26.
                print(f"  [error] Worktree creation failed for {task_id}: {exc}")
                append_journal("worktree_create_failed", f"{task_id} {str(exc)[:160]}")
                park_failed(task_id, f"Worktree creation failed: {str(exc)[:200]}")
                continue
            # Notify only after the worktree exists, so a failure never spams Telegram.
            _verb = "Preparing" if task.get("dispatch") == "manual" else "Starting"
            notify_telegram(f"▶ {_verb} {task_id}: {task.get('title','')}")

            # B8: kind:script — zero-LLM deterministic execution path.
            # Run the task's `run:` shell command, commit, then register in in_flight
            # so the UNCHANGED gate (verification/smoke/merge) adopts it normally.
            if task.get("kind") == "script":
                run_cmd = task.get("run", "").strip()
                if not run_cmd:
                    print(f"  [script] {task_id}: missing `run:` field — parking for Dan")
                    append_journal("script_task_no_cmd", f"{task_id} missing run field")
                    park_failed(task_id, "`kind: script` task has no `run:` command")
                    remove_worktree(worktree)
                    _give_back()
                    continue

                print(f"  [script] {task_id}: running cmd={run_cmd!r}")
                append_journal("script_task_ran", f"{task_id} cmd={run_cmd[:120]}", session_id=sid)
                notify_telegram(f"⚙ {task_id}: running script task …")
                try:
                    proc = subprocess.run(
                        run_cmd, shell=True, cwd=str(worktree),
                        capture_output=True, text=True, timeout=1800,
                    )
                    script_ok     = (proc.returncode == 0)
                    script_stdout = proc.stdout[-4000:] if proc.stdout else ""
                    script_stderr = proc.stderr[-1000:] if proc.stderr else ""
                except subprocess.TimeoutExpired:
                    script_ok     = False
                    script_stdout = ""
                    script_stderr = "script timed out after 1800 s"

                if script_ok:
                    # Stage + commit any changes inside the worktree (no-op if nothing changed).
                    subprocess.run(
                        ["git", "-C", str(worktree), "add", "-A"],
                        capture_output=True,
                    )
                    commit_msg = f"{task_id}: {task.get('title', '')} (script task)"
                    commit_proc = subprocess.run(
                        ["git", "-C", str(worktree), "commit", "-m", commit_msg],
                        capture_output=True, text=True,
                    )
                    if commit_proc.returncode not in (0, 1):  # 1 = nothing to commit
                        print(f"  [script] {task_id}: git commit warning: {commit_proc.stderr[:120]}")

                    # Build result.json in the shape the gate expects.
                    task_verifs = task.get("verifications") or []
                    verif_proofs: dict[str, dict] = {}
                    for v in task_verifs:
                        vid  = v.get("id", "V1")
                        kind = v.get("kind", "auto")
                        verif_proofs[vid] = {"proof": script_stdout[:600]}
                    result_obj = {
                        "summary":       f"Script task completed. exit=0",
                        "tests":         {"pass": True},
                        "verifications": verif_proofs,
                        "script_stdout": script_stdout,
                    }
                    (workspace / "result.json").write_text(
                        json.dumps(result_obj, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    (workspace / "DONE").write_text("script task")
                    print(f"  [script] {task_id}: done — wrote DONE + result.json")
                    append_journal("script_task_done", f"{task_id}", session_id=sid)
                else:
                    fail_reason = f"Script exited non-zero: {script_stderr[:200]}"
                    (workspace / "FAILED").write_text(fail_reason)
                    print(f"  [script] {task_id}: FAILED — {fail_reason[:120]}")
                    append_journal("script_task_failed", f"{task_id} {fail_reason[:120]}", session_id=sid)
                    park_failed(task_id, fail_reason)
                    remove_worktree(worktree)
                    _give_back()
                    run_dep_map()
                    run_status()
                    continue

                new_entry = {
                    "session_id": sid, "task_id": task_id, "role": "script",
                    "worktree": str(worktree), "window": f"script-{task_id}",
                    "branch": branch, "started_at": now_iso(), "status": "running",
                }
                if resource_amounts:
                    new_entry["resources"] = resource_amounts  # P10B: held until it leaves
                in_flight.append(new_entry)
                running_task_ids.add(task_id)
                launch_times[sid] = time.time()
                _persist_launch_time(sid, launch_times[sid])
                state = read_state()
                state["in_flight"] = state.get("in_flight", []) + [new_entry]
                state["phase"]     = {"id": task_id, "title": task.get("title", ""),
                                      "status": "in_progress"}
                state["skeleton_version"] = "B7"
                write_state(state)
                launched_any = True
                claimed_this_cycle |= _resource_set(task)
                run_dep_map()
                run_status()
                continue

            from maestro.model_policy import task_model_context
            with task_model_context(REPO, task_id):
                launch_implementer(task, sid, workspace, worktree, backend=launched_on)
                # Record under the same frozen choices used by the launch.
                new_entry = {
                    "session_id": sid, "task_id": task_id, "role": "implementer",
                    "worktree": str(worktree), "window": f"impl-{task_id}",
                    "branch": branch, "started_at": now_iso(), "status": "running",
                    "backend": launched_on,         # M2: which backend this task runs on
                    # D4/C1: and which model on it — the ceiling this task's context is
                    # measured against. `launch_implementer` resolved the same answer through
                    # the same function, so the record and the launch cannot diverge.
                    "model": _launch_model(task, launched_on),
                    "model_policy_revision": model_revision,
                }
            if resource_amounts:
                new_entry["resources"] = resource_amounts  # P10B: held until it leaves
            in_flight.append(new_entry)
            running_task_ids.add(task_id)
            launch_times[sid] = time.time()
            _persist_launch_time(sid, launch_times[sid])  # Bug #2
            state = read_state()
            state["in_flight"] = state.get("in_flight", []) + [new_entry]
            state["phase"]     = {"id": task_id, "title": task.get("title", ""),
                                  "status": "in_progress"}
            state["skeleton_version"] = "B7"
            write_state(state)
            append_journal("implementer_launched", f"{task_id} branch={branch}", session_id=sid)
            free_slots  -= 1
            launched_any = True
            claimed_this_cycle |= _resource_set(task)  # B8: hold this label for the rest of the cycle
        _record_admission_waits(admission_waits)
        if launched_any:
            run_dep_map()
            run_status()

        finish = None
        if not in_flight and not runnable and not read_state().get("waiting_on_dan", {}):
            # P13 GDT-10 (R54): with a format-2 goals contract an empty queue is done only
            # when the completion gate says so (the verdict only: legacy asks nothing).
            finish = _legacy_goal_finish()
        if _goal_finish_holds(finish):
            _idle_heartbeat_maybe(runnable)
            if _self_update_maybe():
                return 0
            time.sleep(POLL_INTERVAL)
            continue
        if not in_flight and not runnable and not read_state().get("waiting_on_dan", {}):
            print("\n[done] Queue exhausted.")
            append_journal("queue_exhausted", "")
            state = read_state()
            state["phase"] = {"id": None, "title": None, "status": "idle"}
            write_state(state)
            # B7: defer proposals by default — tell Dan to run the proposal test
            # at the right time (richer context) instead of auto-proposing. Set
            # ORCH_AUTO_PROPOSE=1 to restore B6 auto-propose-on-empty.
            if AUTO_PROPOSE:
                generate_proposals()
            else:
                _notify_proposal_test_due(_goal_finish_note(finish))
            # Both paths park the loop via paused_by_user=True; it re-enters and
            # waits for Dan (answer-poll for proposals, or manual resume otherwise).
            if _self_update_maybe():  # nothing in flight: the commonest idle moment
                return 0
            time.sleep(POLL_INTERVAL)
            continue

        if in_flight:
            ids = [e["task_id"] for e in in_flight]
            print(f"  … {len(in_flight)} in-flight {ids}, next poll in {POLL_INTERVAL}s",
                  flush=True)
            time.sleep(POLL_INTERVAL)
        elif not launched_any:
            # Nothing in-flight and nothing launched: every runnable task is gated
            # (resume cooldown / parked / resource mutex). Keep polling so the loop
            # auto-launches when a gate clears; heartbeat so the watchdog doesn't
            # stall-kill a healthy idle loop.
            _idle_heartbeat_maybe(runnable)
            if _self_update_maybe():  # M3 D5: idle branch, in_flight is empty here
                return 0
            time.sleep(POLL_INTERVAL)

    run_status()
    return 0
