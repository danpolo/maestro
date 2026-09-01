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
from maestro.backends.registry import (
    backend_binary,
    get_backend,
    known_backends,
    normalise_name,
)
from maestro.docs.roadmap import (
    get_completed_task_ids,
    get_task_by_id,
    mark_roadmap_complete,
    maybe_push_roadmap_map_change,
    parse_prep_tasks,
    parse_runnable_tasks,
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
from maestro.hitl.commands import poll_control_commands, run_status
from maestro.hitl.telegram import (
    _danreq,
    _send_hitl_reminders,
    notify_telegram,
    phase_report,
)
from maestro.implementer import (
    _questions_ready,
    _synthesize_sentinel,
    launch_implementer,
    operator_backend,
)
from maestro.limits import resolve as resolve_model_limits
from maestro import metrics
from maestro.merge import _touches_bot_files, merge_and_eval, run_canary_deploy
from maestro.parking import (
    _graduate_manual_action,
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
    get_effective_cap,
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


def _notify_proposal_test_due() -> None:
    """Queue empty + auto-propose off: ping Dan once to run the proposal test at
    the right time, then park the loop so it neither auto-proposes nor repeats
    the ping every poll. Resume by clearing paused_by_user (and
    proposal_test_notified) once new tasks are queued."""
    state = read_state()
    if state.get("proposal_test_notified"):
        return
    notify_telegram(
        "✅ All queued tasks are done. When the timing is right, run the proposal "
        f"test for context-aware proposals — see `{PROPOSAL_TEST_HANDOFF}`."
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
        for p in chosen:
            new_id = f"P-{p['title'][:20].replace(' ', '_')}"
            block = (
                f"\n```yaml\n"
                f"id: {new_id}\nshort_desc: \"{p['title']}\"\n"
                f"mode: {p.get('mode','autonomous')}\nstatus: pending\n"
                f"deps: {json.dumps(p.get('deps', []))}\n"
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


def launch_async_job(task: dict) -> bool:
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
    launched and parked; False if it could not be launched (then it is park_failed'd)."""
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
    state   = read_state()
    waiting = state.get("waiting_on_dan", {})
    if not waiting:
        return
    for dan_id_str, parked in list(waiting.items()):
        if parked.get("kind") != "awaiting-verification":
            continue
        task_id = parked.get("task_id", "")
        verifs  = (get_task_by_id(task_id) or {}).get("verifications") or []
        pending, hard = _classify_verifications(verifs, REPO)
        if not pending and not hard:
            append_journal("await_verify_passed", f"{task_id} dan_id={dan_id_str}")
            notify_telegram(f"✅ {task_id}: awaited verification landed and passed — finalizing.")
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
    workspace. Best-effort kill, guarded by the `not FAILED.exists()` so it runs once.
    """
    sid       = entry["session_id"]
    workspace = WORKSPACES / sid
    elapsed   = time.time() - launch_times.get(sid, time.time())
    if elapsed <= TASK_TIMEOUT or (workspace / "DONE").exists():
        return False
    if not (workspace / "FAILED").exists():
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


def _launch_backend() -> str:
    """The backend a fresh `launch_implementer` call runs on.

    The operator's `/backend <name>` first, then `maestro.roles` — the same *order* that
    `implementer._implementer_backend` resolves the launch itself in, but no longer
    **through the same function** (C4, readiness queue): that call now goes through
    `roles.resolve(ROLE_IMPLEMENTER, preferred=operator_backend() or None)`, while this
    one still calls `operator_backend() or backend_for(ROLE_IMPLEMENTER)` — a hard
    short-circuit on the operator's pin rather than handing it to `resolve` as a
    preference. This one only *records* the answer on the `in_flight` entry, so if the
    two ever diverged the entry would name a backend the task is not running on, and
    every switch decision taken from that entry would be about the wrong agent.

    **They agree today only because neither is fed anything to diverge on.** Both call
    sites pass `resolve` (or its `backend_for` wrapper) with no `available=` and no
    `exhausted=`, and `resolve` with neither set always returns the preferred/configured
    head unchanged — so the two paths are observably identical for every input reachable
    right now, not because they share code. **The moment either path gains real
    availability or exhaustion data, they must be unified** — not merely re-verified —
    into one function, along with `agentcall.resolve_call`'s own copy of this same
    "operator's pin leads" order. That unification is the deferred item `A6`; it changes
    all three call sites in one commit rather than three separate ones, precisely so this
    docstring's warning can never again describe two paths that only look like one.

    Still no binary probe and no subprocess: the state document is one small read on a
    path that is about to start an agent. `""` when resolution itself breaks — an unknown
    backend is better than a wrong one.
    """
    try:
        return operator_backend() or backend_for(ROLE_IMPLEMENTER)
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
    """
    if not _handover_ready(entry):
        return None
    available = _available_backends()
    current   = _entry_backend(entry)
    tried     = _backends_tried(entry)
    if not fallback_backend(ROLE_IMPLEMENTER, current, available=available,
                            exhausted=tried):
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
                              exhausted=tried)
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

    The ceiling is looked up **by the model the task was launched with** —
    `roles.model_for`, the same resolution `launch_implementer` passes to `--model` — and
    not by the model name the sample reports. A sample's `model` is a display name written
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

    def launch_model(backend: str) -> str:
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
        if not context_crossed(sample, launch_model(backend), resolve=resolve):
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


def reconcile_in_flight(state: dict, launch_times: dict) -> list:
    """Reconcile in_flight entries against live tmux windows + workspace sentinels.

    Runs at startup AND once per poll cycle so stale entries (window gone, no
    sentinel) are reaped within one poll rather than persisting for the whole
    session.  Mutates launch_times for newly-adopted entries.  Returns the
    surviving in_flight list (entries to keep tracking).
    """
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

        if tmux_window_exists(window):
            if workspace.exists():
                # Zombie guard: implementer wrote result.json but left no sentinel and its
                # window is still alive (hung/idle). Don't re-adopt forever — after a short
                # grace, synthesize the sentinel from result.json, kill the dead window, and
                # let the main loop's completion handlers (incl. resumable handle_incomplete)
                # run. See RECONCILE_DONE_GRACE_POLLS note above.
                has_sentinel = any((workspace / s).exists()
                                   for s in ("DONE", "FAILED", "PAUSED"))
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
    carry_backend = _entry_backend(entry)
    try:
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
            "backend": carry_backend or _launch_backend(),
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


def _self_update_maybe() -> None:
    """Throttled idle-moment self-update check (DESIGN.md §9).

    Only ever called from the branch of `main`'s loop where `in_flight` is already
    empty — this function trusts that precondition rather than re-checking it, exactly
    like `maybe_self_update`/`adopt` themselves (see their docstrings)."""
    global _last_self_update_check
    now = time.monotonic()
    if now - _last_self_update_check < IDLE_HEARTBEAT_S:
        return
    _last_self_update_check = now
    status = maybe_self_update()
    if status != "up_to_date":
        print(f"  [self-update] {status}", flush=True)


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
    if state.get("paused_by_user"):
        print("[paused] paused_by_user=True — entering Telegram poll loop.")
        append_journal("paused_poll_start", "paused_by_user=True waiting for /resume or /approve")
        while True:
            _pstate = read_state()
            if _pstate.get("halted"):
                append_journal("halt_respected", "halted=True during paused poll")
                return 0
            if not _pstate.get("paused_by_user"):
                print("[paused] paused_by_user cleared — resuming main loop.")
                break
            _pfl = _pstate.get("in_flight", [])
            poll_control_commands(_pfl)
            time.sleep(POLL_INTERVAL)
        state = read_state()

    # B10 — reconcile in_flight against live tmux windows + workspace sentinels at startup
    # (also runs every poll cycle via reconcile_in_flight call inside the event loop)
    launch_times: dict[str, float] = {}
    in_flight: list[dict] = reconcile_in_flight(state, launch_times)

    state["in_flight"] = in_flight
    state["skeleton_version"] = "B7"
    write_state(state)
    run_dep_map()
    run_status()

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
            if _timeout_expired(entry, launch_times):
                print(f"  [timeout] {entry['task_id']} timed out")
                append_journal("implementer_timeout", entry["task_id"], session_id=sid)
            if (workspace / "PAUSED").exists():
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
            reason    = ((workspace / "FAILED").read_text()[:200]
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
            gate_ok, gate_msg = run_verification_gate(task_id, workspace, worktree)
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
            sonnet_ok, sonnet_msg = sonnet_review_proofs(task_id, verifications, impl_verifs)
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
        runnable         = parse_runnable_tasks() + parse_prep_tasks()
        running_task_ids = {e["task_id"] for e in in_flight}

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

        free_slots   = cap - len(in_flight)
        launched_any = False
        _launch_state = read_state()
        resume_state = _launch_state.get("resume_state", {})
        # B11: parked tasks — read once per cycle, not per candidate
        parked_tasks = set(_launch_state.get("parked_tasks", []))
        now_utc      = datetime.now(tz=timezone.utc)
        # Tasks with ANY live waiting_on_dan entry (awaiting-verification, or reverted to
        # manual-action after a surfaced failure). An async-job lives here from launch
        # until Dan resolves it, so this set is what stops it relaunching every poll —
        # it is in waiting_on_dan, not in_flight, so running_task_ids would miss it.
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

        for task in runnable:
            task_id  = task["id"]
            is_async = task.get("dispatch") == "async-job"
            # Async read-only jobs hold no Claude session / slot — a full slate of
            # implementer slots must not block launching one. Gate everything else on
            # free_slots as before.
            if not is_async and free_slots <= 0:
                break
            if task_id in running_task_ids:
                continue
            # An async job already launched + parked (or surfaced) owns itself via the
            # poller / Dan — skip early, before the resource check would log a confusing
            # "conflict" against the eval-cpu label the parked job itself is holding.
            if is_async and task_id in waiting_task_ids:
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

            # Long async read-only job: the orchestrator launches the job itself (detached
            # tmux) and parks `awaiting-verification` — no Claude session, no worktree, no
            # in_flight slot. Re-checked by poll_awaiting_verifications, reaper-exempt.
            # (Already-parked async jobs were skipped early, above the resource check.)
            if is_async:
                print(f"\n  [async-job] {task_id}: {task.get('title','')}")
                notify_telegram(f"▶ Launching async job {task_id}: {task.get('title','')}")
                if launch_async_job(task):
                    waiting_task_ids.add(task_id)  # dedup within this same cycle
                continue

            ts_str    = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
            sid       = f"impl-{task_id}-{ts_str}"
            workspace = WORKSPACES / sid
            workspace.mkdir(parents=True, exist_ok=True)
            branch    = f"impl-{task_id.lower()}-{ts_str}"
            worktree  = worktree_path_for(task_id)
            print(f"\n  [launch] {task_id}: {task.get('title','')}")
            try:
                create_worktree(task_id, worktree, branch)
            except subprocess.CalledProcessError as exc:
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
                    run_dep_map()
                    run_status()
                    continue

                new_entry = {
                    "session_id": sid, "task_id": task_id, "role": "script",
                    "worktree": str(worktree), "window": f"script-{task_id}",
                    "branch": branch, "started_at": now_iso(), "status": "running",
                }
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
                free_slots  -= 1
                launched_any = True
                claimed_this_cycle |= _resource_set(task)
                run_dep_map()
                run_status()
                continue

            launch_implementer(task, sid, workspace, worktree)
            new_entry = {
                "session_id": sid, "task_id": task_id, "role": "implementer",
                "worktree": str(worktree), "window": f"impl-{task_id}",
                "branch": branch, "started_at": now_iso(), "status": "running",
                "backend": _launch_backend(),   # M2: which backend this task runs on
            }
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
        if launched_any:
            run_dep_map()
            run_status()

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
                _notify_proposal_test_due()
            # Both paths park the loop via paused_by_user=True; it re-enters and
            # waits for Dan (answer-poll for proposals, or manual resume otherwise).
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
            _self_update_maybe()  # M3 D5: idle branch, in_flight is empty here
            time.sleep(POLL_INTERVAL)

    run_status()
    return 0
