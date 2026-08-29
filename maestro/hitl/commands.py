"""The Telegram control plane: the command router and the handlers it dispatches to.

Extracted verbatim from the reference orchestrator. Bodies are unchanged — only the
import block and the derivation of the module-level path globals differ. Names owned by
modules that are extracted later (the parking/finalize helpers, the self-fix runner and the
in-flight bookkeeping) are bound to `deferred()` late bindings, and the prepared-action
sidecar is imported as a module, so the call sites stay byte-for-byte identical.
Behavioural surprises are catalogued in `docs/found_bugs_inbox/commands.md` and pinned by
`tests/characterization/test_commands.py`; none of them is fixed here — including the
`REPO_ROOT` NameError on the `/reject` cleanup path, which is copied across as-is.

**M2 adds the one deliberate divergence from the reference here:** the `/backend` verb
(`docs/DESIGN.md` §7's manual switch trigger), its line in the `/help` sheet — the only
place commands are advertised — and the backend segment `/progress` renders for an
in-flight entry that carries one. An entry written before that key existed renders exactly
as it did. The characterisation tests pin both sides: the reference's text for the legacy
subject, this text for the maestro one.

Nothing in this module talks to Telegram directly: `getUpdates` is fetched by shelling out
to `curl` through the module's `subprocess` reference, and every reply goes through
`notify_telegram`. The characterisation tests swap both rather than letting anything reach
the network.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from maestro import prep_actions
from maestro.backends.registry import known_backends, normalise_name
from maestro.docs.roadmap import (
    _load_roadmap_tasks,
    get_completed_task_ids,
    get_task_by_id,
    mark_roadmap_complete,
    run_dep_map,
)
from maestro.hitl.telegram import (
    _handle_danreq_callback,
    _route_freetext_answer,
    notify_telegram,
    phase_report,
)
from maestro.implementer import _answer_choice, _question_req_id, _task_questions
from maestro import metrics
from maestro.merge import _touches_bot_files, merge_and_eval
from maestro.paths import Paths
from maestro.pending import deferred
from maestro.quota import _elapsed_min, _elapsed_str, _fmt_min, _parse_est_minutes
from maestro.state import append_journal, now_iso, read_json, read_state, write_state
from maestro.status import print_status
from maestro.switch import REASON_MANUAL, switch_task
from maestro.worktree import TMUX_SESSION, _tail_tmux_pane, remove_worktree

_PATHS = Paths.from_env()

REPO                  = _PATHS.repo
WORKSPACES            = _PATHS.workspaces
HALT_FILE             = REPO / ".orchestrator" / "HALT"
VENV_PYTHON           = REPO / ".venv" / "bin" / "python3"
CANARY_DEPLOY         = REPO / "scripts" / "canary_deploy.py"
DIAGNOSES_DIR         = REPO / ".orchestrator" / "diagnoses"
REDO_DIR              = REPO / ".orchestrator" / "redo"

# The B14 auto-prep sidecar. The reference imports `prepared_actions` as a sibling module
# off `scripts/`; its maestro home is `maestro.prep_actions` (M4a), imported at the top.
# Each name below is owned by a module extracted *after* this one, so a top-level import
# would invert an edge those modules already depend on. `deferred` resolves each on first
# call instead — they remain ordinary module attributes, monkeypatchable as before.
_finalize_manual_action = deferred("_finalize_manual_action", "maestro.parking")
park_regression = deferred("park_regression", "maestro.parking")
attempt_self_fix = deferred("attempt_self_fix", "maestro.selfheal.selffix")
_remove_from_state = deferred("_remove_from_state", "maestro.orchestrator")


def run_status() -> None:
    """R13: `maestro.status.print_status()` called in-process instead of shelling out to
    the now-retired `orchestrator_status.py` script. See `maestro/status.py`."""
    print_status()


# ── B6: Telegram control-plane ──

_getUpdates_offset: int = 0


def _build_progress_report() -> str:
    """Per in-flight task: elapsed, human est_time (+ rough ETA hint), live activity.
    Prefers a workspace progress.json `milestone`/`pct` if the implementer wrote one,
    else tails the tmux pane. No implementer cooperation required."""
    state = read_state()
    in_f  = state.get("in_flight", [])
    if not in_f:
        return "📭 No tasks in flight."
    blocks = ["📈 Task progress"]
    for e in in_f:
        tid     = e.get("task_id", "?")
        role    = e.get("role", "?")
        status  = e.get("status", "?")
        started = e.get("started_at", "")
        elapsed = _elapsed_str(started)
        task_def = get_task_by_id(tid) or {}
        est     = str(task_def.get("est_time", "")).strip()
        # M2: entries launched (or switched) since the backend key exists say which agent
        # is doing the work; an entry written before it — or hand-seeded — renders exactly
        # as it always did, rather than advertising a "None" backend nobody chose.
        backend = str(e.get("backend") or "").strip()
        who = f"{role}/{backend}" if backend else str(role)
        header = f"• {tid} ({who}) {status} · elapsed {elapsed}"
        if est:
            header += f" · est {est[:48]}"
        # Rough ETA when est_time carries a clean leading duration token.
        est_min, el_min = _parse_est_minutes(est), _elapsed_min(started)
        if est_min and el_min is not None:
            rem = est_min - el_min
            header += f" · ~{_fmt_min(rem)} left" if rem > 0 else " · over est"
        blocks.append(header)
        # Activity line: prefer an opt-in progress.json, else the live tmux tail.
        activity = ""
        prog_file = WORKSPACES / e.get("session_id", "") / "progress.json"
        pj = read_json(prog_file) if prog_file.exists() else {}
        if pj.get("milestone") or pj.get("pct") is not None:
            pct = f"{pj['pct']}% " if pj.get("pct") is not None else ""
            activity = f"{pct}{pj.get('milestone', '')}".strip()
        else:
            activity = _tail_tmux_pane(e.get("window", ""))
        if activity:
            blocks.append("    ↳ " + activity.replace("\n", "\n      "))
    return "\n".join(blocks)


def poll_control_commands(in_flight: list) -> None:
    """Poll Telegram getUpdates for /status /progress /pause /resume /halt /hitl /backend /approve /reject /waiting /manual /detail commands from Dan."""
    global _getUpdates_offset
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_ALERT_CHAT_ID", "")
    if not token or not chat_id:
        return

    try:
        r = subprocess.run(
            [
                "curl", "-s", "--max-time", "5",
                f"https://api.telegram.org/bot{token}/getUpdates"
                f"?offset={_getUpdates_offset}&timeout=0&limit=10",
            ],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(r.stdout)
        if not data.get("ok"):
            return

        for upd in data.get("result", []):
            _getUpdates_offset = upd["update_id"] + 1

            # B12: button taps on Dan-request messages arrive as callback_query
            # (no top-level "message"). Record the chosen option and move on.
            cq = upd.get("callback_query")
            if cq:
                cq_chat = str((cq.get("message") or {}).get("chat", {}).get("id", ""))
                if cq_chat == str(chat_id):
                    _handle_danreq_callback(cq)
                continue

            msg = upd.get("message") or upd.get("channel_post") or {}
            from_chat = str(msg.get("chat", {}).get("id", ""))
            if from_chat != str(chat_id):
                continue
            text_raw = (msg.get("text") or "").strip()
            text = text_raw.lower()

            if text in ("/help", "/commands", "/start"):
                notify_telegram(
                    "🎼 Maestro controls\n"
                    "/status — phase, in-flight tasks, halted/paused flags\n"
                    "/progress — per-task elapsed, ETA, and live activity\n"
                    "/pause — stop launching new tasks\n"
                    "/resume — resume after a /pause\n"
                    "/halt — write HALT sentinel; stop after current iteration\n"
                    "/hitl on|off — pause every task at the merge gate for review\n"
                    "/backend <name> [task_id] — pick the agent backend for new launches, "
                    "or move one in-flight task to it now (same worktree, uncommitted "
                    "work kept)\n"
                    "/waiting — list tasks parked for your decision\n"
                    "/approve <id> — done & it worked: finalize/graduate a parked task\n"
                    "/fix <id> — agree with a failure diagnosis: have Maestro implement the "
                    "suggested fix (confined to its own code/docs, gate-checked)\n"
                    "/ask <id> <msg> — hit a problem on a manual job? ask Maestro "
                    "(it resumes the session that prepared it)\n"
                    "/redo <id> <msg> — have Maestro REWRITE + re-upload the deliverable "
                    "(notebook/script) for a manual job, syntax-gated\n"
                    "/reject <id> — abandon a parked task\n"
                    "/unpark <task_id> — un-park a failed task so it can be relaunched\n"
                    "/manual — Dan-must-perform tasks + tasks awaiting your answers (questions:)\n"
                    "/detail <id> — full task detail: mode, deps, and every verification (auto/manual)\n"
                    "/help — this message"
                )
            elif text == "/status":
                state = read_state()
                in_f  = state.get("in_flight", [])
                phase = state.get("phase", {}).get("id", "?")
                parked = state.get("parked_tasks", [])
                notify_telegram(
                    f"🤖 Orchestrator status\n"
                    f"Phase: {phase} | In-flight: {len(in_f)}\n"
                    f"Tasks: {', '.join(e.get('task_id','?') for e in in_f) or 'none'}\n"
                    f"Halted: {state.get('halted',False)} | Paused: {state.get('paused_by_user',False)}"
                    + (f"\nParked: {', '.join(parked)}" if parked else "")
                )
            elif text == "/progress":
                notify_telegram(_build_progress_report())
            elif text == "/pause":
                state = read_state()
                state["paused_by_user"] = True
                write_state(state)
                append_journal("control_pause", "paused via Telegram /pause")
                notify_telegram("⏸ Orchestrator paused. No new tasks will launch. /resume to continue.")
            elif text == "/resume":
                state = read_state()
                state["paused_by_user"] = False
                write_state(state)
                append_journal("control_resume", "resumed via Telegram /resume")
                notify_telegram("▶ Orchestrator resumed.")
            elif text == "/halt":
                HALT_FILE.parent.mkdir(parents=True, exist_ok=True)
                HALT_FILE.write_text(now_iso())
                append_journal("control_halt", "halted via Telegram /halt")
                notify_telegram("🛑 HALT sentinel written. Orchestrator will stop after current iteration.")
            elif text.startswith("/approve "):
                _process_approve(text.split(" ", 1)[1].strip(), in_flight)
            elif text.startswith("/fix "):
                _process_fix(text.split(" ", 1)[1].strip().upper())
            elif text.startswith("/reject "):
                _process_reject(text.split(" ", 1)[1].strip())
            elif text == "/waiting":
                _show_waiting()
            elif text.startswith("/unpark "):
                _tid = text.split(" ", 1)[1].strip().upper()
                state = read_state()
                parked = state.get("parked_tasks", [])
                if _tid in parked:
                    parked.remove(_tid)
                    state["parked_tasks"] = parked
                    # Also reset retry count so the task gets a fresh attempt
                    state.get("retry_counts", {}).pop(_tid, None)
                    write_state(state)
                    append_journal("control_unpark", f"{_tid} unparked via Telegram")
                    notify_telegram(f"✅ {_tid} unparked — will be launched on next cycle.")
                else:
                    notify_telegram(f"ℹ {_tid} is not parked. Currently parked: {', '.join(parked) or 'none'}")
            elif text == "/manual":
                _show_manual()
            elif text == "/detail" or text.startswith("/detail "):
                arg = text[len("/detail"):].strip().upper()
                if not arg:
                    notify_telegram("Usage: /detail <task_id>\nExample: /detail P8B2")
                else:
                    _show_detail(arg)
            elif text == "/ask" or text.startswith("/ask "):
                # text_raw preserves the question's original case/content.
                _handle_ask(text_raw[len("/ask"):].strip())
            elif text == "/redo" or text.startswith("/redo "):
                _handle_redo(text_raw[len("/redo"):].strip())
            elif text.startswith("/hitl "):
                arg = text.split(" ", 1)[1].strip()
                state = read_state()
                if arg == "on":
                    state["hitl_mode"] = True
                    write_state(state)
                    notify_telegram("✅ HITL mode ON — tasks will pause before merge for your review.")
                elif arg == "off":
                    state["hitl_mode"] = False
                    write_state(state)
                    notify_telegram("✅ HITL mode OFF — tasks auto-merge after gates pass.")
            elif text == "/backend" or text.startswith("/backend "):
                # M2/D2: text_raw preserves the task id's case — task ids are
                # case-sensitive and `text` has been lowercased.
                _process_backend(text_raw[len("/backend"):].strip())
            elif text_raw and not text_raw.startswith("/"):
                # B12: any non-command text is treated as a free-text answer to a
                # pending Dan-request (notes, or an answer that isn't a listed option).
                _route_freetext_answer(text_raw, msg.get("reply_to_message"))

    except Exception:
        pass  # Never let control-plane errors crash the main loop


#: Where `/backend <name>` records the operator's choice for the *next* launch. It sits
#: beside `hitl_mode` and `paused_by_user` because it is the same kind of thing: a runtime
#: override of configured behaviour, owned by the state document rather than by
#: `project.yaml`, so an operator decision made at 3am is not a config edit and does not
#: outlive the run's state.
BACKEND_KEY = "backend"

#: The journal event a `/backend <name>` records, in the `control_*` family the other
#: Telegram control verbs already use.
BACKEND_EVENT = "control_backend"


def _in_flight_entry(in_flight: list, task_id: str) -> dict | None:
    """The in-flight entry for `task_id`, matched exactly first.

    Task ids are case-sensitive, which is why `/backend` reads its argument off `text_raw`
    rather than off the lowercased `text`. The case-insensitive second pass is a
    convenience for an id typed by hand on a phone keyboard, and it runs *second* so an
    exactly-matching id can never lose to one that only matches when folded.
    """
    wanted = str(task_id).strip()
    for entry in in_flight:
        if str(entry.get("task_id", "")).strip() == wanted:
            return entry
    folded = wanted.lower()
    for entry in in_flight:
        if str(entry.get("task_id", "")).strip().lower() == folded:
            return entry
    return None


def _process_backend(arg: str) -> None:
    """`/backend <name> [task_id]` — the manual switch trigger (D2, DESIGN.md §7).

    With a name alone, the choice is recorded in the state document for future launches.
    With a task id after it, that one in-flight task is handed over *now*, through the
    single switch path in `maestro.switch`: the outgoing agent is asked to checkpoint, and
    the incoming one is launched into the same worktree with the uncommitted work still
    in it.

    Every failure mode — no argument, an unknown backend, an unknown task id, a task
    already on that backend, a switch that could not happen — is answered with a message
    and returns. Nothing here raises: this runs inside the router's blanket
    `except Exception: pass`, where an exception would not just lose this command, it
    would silently drop the rest of the poll batch with it.
    """
    known = ", ".join(known_backends())
    parts = arg.split()
    if not parts:
        notify_telegram(
            f"Usage: /backend <name> [task_id]\n"
            f"Known backends: {known}\n"
            f"/backend <name> — new launches use it; "
            f"/backend <name> <task_id> — move that task now."
        )
        return

    name = normalise_name(parts[0])
    if name not in known_backends():
        notify_telegram(f"ℹ Unknown backend `{parts[0]}`. Known backends: {known}.")
        return

    if len(parts) == 1:
        try:
            state = read_state()
            state[BACKEND_KEY] = name
            write_state(state)
            append_journal(BACKEND_EVENT, f"{name} selected via Telegram /backend")
        except Exception as exc:
            notify_telegram(f"⚠ Could not record the backend choice: {exc}")
            return
        notify_telegram(
            f"✅ Backend set to {name} — new task launches will use it. Tasks already in "
            f"flight keep the backend they started on; /backend {name} <task_id> moves one."
        )
        return

    requested = parts[1]
    try:
        state = read_state()
    except Exception as exc:
        notify_telegram(f"⚠ Could not read the orchestrator state: {exc}")
        return
    in_flight = [e for e in state.get("in_flight", []) if isinstance(e, dict)]
    entry = _in_flight_entry(in_flight, requested)
    if entry is None:
        ids = ", ".join(str(e.get("task_id", "?")) for e in in_flight) or "none"
        notify_telegram(f"ℹ No task `{requested}` is in flight. In flight: {ids}.")
        return

    task_id = str(entry.get("task_id") or requested)
    if normalise_name(entry.get(BACKEND_KEY)) == name:
        notify_telegram(f"ℹ {task_id} is already running on {name}. Nothing to do.")
        return

    try:
        outcome = switch_task(task_id, reason=REASON_MANUAL, entry=entry, to_backend=name)
    except Exception as exc:
        notify_telegram(f"⚠ Could not move {task_id} to {name}: {exc}")
        return
    if outcome.switched:
        notify_telegram(
            f"✅ {task_id} is now on {outcome.to_backend} — same worktree, uncommitted "
            f"work kept. New session {outcome.new_session_id}."
        )
    else:
        notify_telegram(
            f"⚠ {task_id} stayed on {outcome.from_backend or 'its current backend'}: "
            f"{outcome.note or 'the switch did not happen'}."
        )


def _process_fix(task_id: str) -> None:
    """`/fix <id>`: Dan approves a persisted diagnosis → run the self-fix runner for it.
    Dan's approval substitutes for the failure-class whitelist + rate-limit, but the
    runner's path-confinement (scripts/ orchestrator/ docs/ minus deny-list) + byte-compile
    + ≥1-commit gates still apply, and the orchestrator re-checks paths at merge."""
    f = DIAGNOSES_DIR / f"{task_id}.json"
    if not f.exists():
        notify_telegram(f"No pending diagnosis for `{task_id}`. (Diagnoses are saved when a "
                        f"task fails; check /status for parked tasks.)")
        return
    try:
        diag = json.loads(f.read_text(encoding="utf-8"))
    except Exception as exc:
        notify_telegram(f"⚠ Could not read the saved diagnosis for `{task_id}`: {exc}")
        return
    reason = diag.get("reason", "")
    append_journal("fix_approved", f"{task_id} class={diag.get('failure_class','')} via /fix")
    notify_telegram(f"🔧 Approved — implementing the diagnosed fix for `{task_id}` now. "
                    f"I'll prepare it in a confined worktree, gate-check, and merge.")
    attempt_self_fix(task_id, reason, diag)
    f.unlink(missing_ok=True)


# `_session_uuid_for` is mapped to `maestro.parking`, which is extracted after this module.
# `_handle_ask` calls it on the path the characterisation tests exercise without stubbing
# it, so a `pending()` placeholder would make that path untestable. The body is copied
# verbatim here; when `maestro.parking` lands, one of the two copies becomes an import of
# the other.
def _session_uuid_for(session_id: str) -> str:
    """The resumable Claude session UUID for a launched implementer (written by
    launch_implementer to session_uuid.txt). Empty for legacy/hand-seeded items —
    /ask falls back to brief-seeded context in that case."""
    if not session_id:
        return ""
    try:
        return (WORKSPACES / session_id / "session_uuid.txt").read_text(
            encoding="utf-8").strip()
    except Exception:
        return ""


def _process_approve(dan_id_str: str, in_flight: list) -> None:
    """Merge a HITL-parked task after Dan's approval."""
    state   = read_state()
    waiting = state.get("waiting_on_dan", {})
    parked  = waiting.get(dan_id_str)
    if not parked:
        notify_telegram(f"No pending item with ID {dan_id_str}.")
        return
    # B14 auto-prep: a manual-action item's prep branch is already merged — Dan just
    # performed his one action. Finalize differently (post-action + gate + graduate).
    if parked.get("kind") == "manual-action":
        _finalize_manual_action(dan_id_str, parked, in_flight)
        return
    task_id = parked["task_id"]
    entry: dict = {
        "session_id": parked["session_id"],
        "task_id":    task_id,
        "branch":     parked["branch"],
        "worktree":   parked["worktree"],
        "window":     f"impl-{task_id}",
    }
    notify_telegram(f"✅ Approved {task_id} — merging now…")
    append_journal("hitl_approved", f"{task_id} dan_id={dan_id_str}")
    del waiting[dan_id_str]
    state["waiting_on_dan"] = waiting
    write_state(state)
    workspace = WORKSPACES / parked["session_id"]
    (workspace / "PAUSED").unlink(missing_ok=True)

    # Merge + full post-merge finalize. KEEP IN SYNC with the autonomous
    # completion block in the main loop ("Handle completions"): an /approve must
    # finalize exactly like an auto-merge or the task is left stuck in_flight,
    # unpushed, and unmarked in ROADMAP.
    worktree = Path(parked["worktree"])
    accepted, smoke = merge_and_eval(entry)
    shown = metrics.summary(smoke)
    in_flight[:] = [e for e in in_flight if e.get("session_id") != entry["session_id"]]
    _remove_from_state(entry)
    if accepted:
        print(f"  ✓ {task_id} merged and accepted ({shown})")
        append_journal("task_complete", f"{task_id} {shown}",
                       session_id=entry["session_id"])
        remove_worktree(worktree)
        mark_roadmap_complete(task_id)
        subprocess.run(["git", "push", "origin", "main"],
                       cwd=str(REPO), capture_output=True)
        branch = entry.get("branch", "")
        if branch and _touches_bot_files(branch):
            canary_ok = subprocess.run(
                [str(VENV_PYTHON), str(CANARY_DEPLOY), task_id],
                cwd=str(REPO), capture_output=True,
            ).returncode == 0
            if not canary_ok:
                append_journal("canary_reverted", f"{task_id} canary deploy failed")
                run_dep_map(); run_status()
                return
        phase_report(task_id, get_task_by_id(task_id), smoke)
        # `metrics.summary` already says "no metrics" for a skipped smoke, so the
        # branch that used to spell that out per project is gone with it.
        notify_telegram(f"✅ {task_id} merged & accepted ({shown}).")
    else:
        reason_merge = smoke.get("reason", "")
        print(f"  ✗ {task_id} smoke/merge failed ({shown}) — {reason_merge[:80]}")
        notify_telegram(f"⚠ {task_id} smoke/merge failed ({shown}) — reverted. See journal.")
        park_regression(task_id, smoke)
        remove_worktree(worktree)
    run_dep_map()
    run_status()


def _process_reject(dan_id_str: str) -> None:
    """Discard a HITL-parked task after Dan's rejection."""
    state   = read_state()
    waiting = state.get("waiting_on_dan", {})
    parked  = waiting.get(dan_id_str)
    if not parked:
        notify_telegram(f"No pending item with ID {dan_id_str}.")
        return
    task_id = parked["task_id"]
    # B14 auto-prep: rejecting a manual-action means "don't graduate". The prep commits
    # already live on main (harmless scripts/data) — just drop the sidecar + parked entry.
    if parked.get("kind") in ("manual-action", "awaiting-verification"):
        del waiting[dan_id_str]
        # An async-job (read-only eval) has no prep branch and is still pending in ROADMAP,
        # so dropping its waiting entry alone would relaunch it next poll. Park it (B11) so
        # rerunning takes an explicit /unpark — rejecting an eval means "keep base, stop".
        if (get_task_by_id(task_id) or {}).get("dispatch") == "async-job":
            _pk = state.get("parked_tasks", [])
            if task_id not in _pk:
                _pk.append(task_id)
                state["parked_tasks"] = _pk
        state["waiting_on_dan"] = waiting
        write_state(state)
        prep_actions.remove_action(task_id)
        (WORKSPACES / parked.get("session_id", "") / "PAUSED").unlink(missing_ok=True)
        notify_telegram(f"❌ Dropped {task_id}'s pending action (prep commits stay on main).")
        append_journal("manual_action_rejected", f"{task_id} dan_id={dan_id_str}")
        return
    branch  = parked["branch"]
    worktree = parked.get("worktree", "")
    notify_telegram(f"❌ Rejected {task_id} — discarding branch `{branch}`.")
    append_journal("hitl_rejected", f"{task_id} dan_id={dan_id_str}")
    del waiting[dan_id_str]
    state["waiting_on_dan"] = waiting
    write_state(state)
    workspace = WORKSPACES / parked["session_id"]
    (workspace / "PAUSED").unlink(missing_ok=True)
    if worktree and Path(worktree).exists():
        subprocess.run(["git", "worktree", "remove", "--force", worktree],
                       capture_output=True, cwd=REPO_ROOT)
    subprocess.run(["git", "branch", "-D", branch], capture_output=True, cwd=REPO_ROOT)


# ── Part B: /manual and /detail helpers ──

def _resolve_waiting(idarg: str) -> tuple:
    """Resolve a /ask|/approve-style id (a waiting_on_dan dan_id key OR a task_id) to
    (dan_id, entry). Returns (None, None) if nothing matches."""
    waiting = read_state().get("waiting_on_dan", {})
    if idarg in waiting:
        return idarg, waiting[idarg]
    up = idarg.upper()
    for did, e in waiting.items():
        if str(e.get("task_id", "")).upper() == up:
            return did, e
    return None, None


def _handle_ask(arg: str) -> None:
    """/ask <id> <message> — Dan reports a problem or asks a question about a parked
    manual task. Answers WITH the context of the session that prepared the job by
    resuming it (`claude -p --resume <uuid>`); falls back to a fresh session seeded
    with the task brief for legacy/hand-seeded items. Runs in a dedicated tmux window
    (auditable, non-blocking); maestro_ask.py posts the answer back to Telegram."""
    parts = arg.split(None, 1)
    if len(parts) < 2:
        notify_telegram("Usage: /ask <id> <your message>\n"
                        "Example: /ask P12C cell 5 fails with KeyError 'x'")
        return
    idarg, question = parts[0], parts[1].strip()
    dan_id, entry = _resolve_waiting(idarg)
    if not entry:
        notify_telegram(f"ℹ /ask: no parked task matches `{idarg}`. Use /waiting to list them.")
        return
    task_id  = entry.get("task_id", idarg)
    sid      = entry.get("session_id", "")
    uuid_    = entry.get("session_uuid", "") or _session_uuid_for(sid)
    pa       = prep_actions.get_action(task_id) or {}
    action   = pa.get("action") or entry.get("summary", "")
    worktree = entry.get("worktree", "") or ""
    # The implementer's Claude session is keyed to the worktree it built in
    # (~/.claude/projects/<encoded-cwd>/<uuid>.jsonl). That JSONL survives the
    # /tmp worktree being cleaned up — so when we have a resumable uuid but the
    # dir is gone, recreate the empty dir rather than downgrading cwd to REPO
    # (which would make `claude --resume` look in the wrong project dir and fail
    # with "No conversation found"). Only fall back to REPO with no uuid at all.
    if worktree and uuid_ and not Path(worktree).exists():
        try:
            Path(worktree).mkdir(parents=True, exist_ok=True)
        except OSError:
            worktree = ""
    if not worktree or not Path(worktree).exists():
        worktree = str(REPO)
    brief = str(WORKSPACES / sid / "brief.txt") if sid else ""

    ask_dir = REPO / ".orchestrator" / "ask"
    ask_dir.mkdir(parents=True, exist_ok=True)
    ts        = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    req_path  = ask_dir / f"{task_id}-{ts}.json"
    log_path  = ask_dir / f"{task_id}-{ts}.answer.log"
    req_path.write_text(json.dumps({
        "uuid": uuid_, "task": task_id, "dan_id": dan_id or "", "question": question,
        "action": action, "brief": brief, "worktree": worktree, "log": str(log_path),
    }), encoding="utf-8")

    window   = f"ask-{task_id}"
    tmux_cmd = (f"tmux new-window -t {TMUX_SESSION} -n {window} "
                f"'cd {REPO} && {VENV_PYTHON} -m maestro.hitl.ask {req_path}'")
    try:
        subprocess.run(tmux_cmd, shell=True, check=True)
    except Exception as exc:
        notify_telegram(f"⚠ /ask failed to launch: {exc}")
        return
    how = "resuming its prep session" if uuid_ else "reading the task brief"
    notify_telegram(f"🤔 Asking Maestro about `{task_id}` ({how})… I'll reply here shortly.")
    append_journal("ask_launched",
                   f"{task_id} dan_id={dan_id} uuid={'y' if uuid_ else 'n'}", session_id=sid)


def _handle_redo(arg: str) -> None:
    """`/redo <id> <what's wrong>` — have Maestro REWRITE + re-upload the deliverable for a
    parked manual task (vs /ask, which only advises). Spawns scripts/maestro_redo.py in a
    detached tmux window; it resumes the prep session, rewrites the notebook, runs the
    syntax gate in a loop, re-uploads to Drive, and hands a gate-passed branch back here
    to merge. Confined to colab/ + data_export/ + scripts/ + docs/ (minus the deny-list)."""
    parts = arg.split(None, 1)
    if len(parts) < 2:
        notify_telegram("Usage: /redo <id> <what's wrong>\n"
                        "Example: /redo 9 cell 5 crashes — the Drive path is wrong, rewrite it")
        return
    idarg, message = parts[0], parts[1].strip()
    dan_id, entry = _resolve_waiting(idarg)
    if not entry:
        notify_telegram(f"ℹ /redo: no parked task matches `{idarg}`. Use /waiting to list them.")
        return
    task_id  = entry.get("task_id", idarg)
    sid      = entry.get("session_id", "")
    uuid_    = entry.get("session_uuid", "") or _session_uuid_for(sid)
    pa       = prep_actions.get_action(task_id) or {}
    action   = pa.get("action") or entry.get("summary", "")
    worktree = entry.get("worktree", "") or ""
    brief    = str(WORKSPACES / sid / "brief.txt") if sid else ""

    REDO_DIR.mkdir(parents=True, exist_ok=True)
    ts      = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    redo_id = f"{task_id}-{ts}"
    req_path = REDO_DIR / f"{redo_id}.request.json"
    req_path.write_text(json.dumps({
        "redo_id": redo_id, "task": task_id, "uuid": uuid_, "dan_id": dan_id or "",
        "message": message, "action": action, "brief": brief, "worktree": worktree,
    }), encoding="utf-8")

    tmux_cmd = (f"tmux new-window -t {TMUX_SESSION} -n redo-{task_id} "
                f"'cd {REPO} && {VENV_PYTHON} -m maestro.selfheal.redo {req_path}'")
    try:
        subprocess.run(tmux_cmd, shell=True, check=True)
    except Exception as exc:
        notify_telegram(f"⚠ /redo failed to launch: {exc}")
        return
    notify_telegram(f"🔧 Maestro is reworking `{task_id}` — rewriting the deliverable, "
                    f"syntax-gating it, and re-uploading to Drive. I'll report back with the "
                    f"changelog + link.")
    append_journal("redo_launched",
                   f"{task_id} dan_id={dan_id} uuid={'y' if uuid_ else 'n'}", session_id=sid)


def _show_manual() -> None:
    """Reply with all dispatch:manual / needs-dan tasks and whether their deps are clear."""
    try:
        tasks = _load_roadmap_tasks()
    except Exception as exc:
        notify_telegram(f"Could not parse ROADMAP: {exc}")
        return

    pending_ids  = {str(t["id"]) for t in tasks}
    complete_ids = get_completed_task_ids()

    dan_tasks = [
        t for t in tasks
        if t.get("dispatch") == "manual" or t.get("mode") == "needs-dan"
    ]
    # B12: tasks that need Dan to ANSWER questions before an agent can implement
    # them (distinct from dispatch:manual tasks Dan performs himself).
    question_tasks = [t for t in tasks if _task_questions(t)]

    if not dan_tasks and not question_tasks:
        notify_telegram("No Dan-must-perform tasks in the queue.")
        return

    lines: list[str] = []
    if dan_tasks:
        _state    = read_state()
        _parked   = {p.get("task_id"): did
                     for did, p in _state.get("waiting_on_dan", {}).items()}
        _in_prep  = {e.get("task_id") for e in _state.get("in_flight", [])}
        lines.append(f"🛠️ Dan-must-perform ({len(dan_tasks)}):")
        for task in dan_tasks:
            tid   = str(task["id"])
            title = task.get("title", "(no title)")
            deps  = task.get("deps") or []
            if isinstance(deps, str):
                deps = [deps] if deps else []
            blocked = [
                str(d) for d in deps
                if str(d) in pending_ids and str(d) not in complete_ids
            ]
            sidecar = prep_actions.get_action(tid)
            action  = (sidecar or {}).get("action") or task.get("dan_action")
            # B14: prefer the single prepared action over raw dep status.
            if action and tid in _parked:
                status = (f"🎯 {action}\n"
                          f"   ✅ /approve {_parked[tid]}  ·  ❓ /ask {_parked[tid]} <msg>  "
                          f"·  ❌ /reject {_parked[tid]}")
            elif action:
                status = f"🎯 {action}"
            elif tid in _in_prep:
                status = "⏳ auto-prep running"
            elif blocked:
                status = "⛔ blocked by " + ", ".join(blocked)
            else:
                status = "⏳ queued for auto-prep"
            lines.append(f"• {tid} — {title}\n    {status}")

    if question_tasks:
        if lines:
            lines.append("")
        lines.append(f"⏳ Awaiting your answers ({len(question_tasks)}):")
        for task in question_tasks:
            tid   = str(task["id"])
            title = task.get("title", "(no title)")
            qs    = _task_questions(task)
            statuses = []
            answered = 0
            for q in qs:
                ok = _answer_choice(_question_req_id(tid, q["id"])) is not None
                answered += 1 if ok else 0
                statuses.append(f"{q['id']}={'✓' if ok else '…'}")
            lines.append(
                f"• {tid} — {title} — {answered}/{len(qs)} answered "
                f"({', '.join(statuses)})"
            )

    notify_telegram("\n".join(lines))


def _show_detail(task_id: str) -> None:
    """Reply with task detail. For dispatch:manual / needs-dan tasks, surface just the
    ONE action Dan must take (B14); `/detail <id> full` forces the verbose dump."""
    raw  = task_id.strip()
    full = raw.upper().endswith(" FULL")
    if full:
        raw = raw[: -len(" FULL")].strip()
    try:
        tasks = _load_roadmap_tasks()
    except Exception as exc:
        notify_telegram(f"Could not parse ROADMAP: {exc}")
        return

    task = next(
        (t for t in tasks if str(t.get("id", "")).upper() == raw.upper()),
        None,
    )
    if task is None:
        notify_telegram(
            f"No task {raw} in ROADMAP (it may be graduated — see docs/PROJECT.md)."
        )
        return

    tid        = str(task["id"])
    title      = task.get("title", "(no title)")
    mode       = task.get("mode", "autonomous")
    dispatch   = task.get("dispatch", "auto")
    est_time   = task.get("est_time", "unknown")
    short_desc = task.get("short_desc", "")

    # B14: one-action view for Dan-must-perform tasks (unless `full` was requested).
    is_dan_task = dispatch == "manual" or mode == "needs-dan"
    if is_dan_task and not full:
        sidecar = prep_actions.get_action(tid)
        action  = (sidecar or {}).get("action") or task.get("dan_action")
        state   = read_state()
        parked  = next((p for p in state.get("waiting_on_dan", {}).items()
                        if p[1].get("task_id") == tid), None)
        in_prep = any(e.get("task_id") == tid for e in state.get("in_flight", []))
        # dep readiness
        complete = get_completed_task_ids()
        deps = task.get("deps") or []
        deps = [deps] if isinstance(deps, str) and deps else deps
        blocked = [str(d) for d in deps if str(d) not in complete]

        if action and parked:
            notify_telegram(f"{tid} — {title}\n🎯 Your action (ID {parked[0]}): {action}\n"
                            f"✅ Worked → /approve {parked[0]}  ·  ❓ Problem → /ask {parked[0]} <msg>  "
                            f"·  ❌ Abandon → /reject {parked[0]}")
        elif action:
            notify_telegram(f"{tid} — {title}\n🎯 Your action: {action}")
        elif in_prep:
            notify_telegram(f"{tid} — {title}\n⏳ Auto-prep in progress — I'll ping you with the "
                            f"one action when it's ready.")
        elif blocked:
            notify_telegram(f"{tid} — {title}\n⛔ Blocked by: {', '.join(blocked)} "
                            f"(auto-prep starts once deps complete).")
        else:
            notify_telegram(f"{tid} — {title}\n⏳ Queued for auto-prep. "
                            f"(`/detail {tid} full` for the raw brief.)")
        return

    lines = [
        f"{tid} — {title}",
        f"mode: {mode} | dispatch: {dispatch} | est: {est_time}",
    ]
    if short_desc:
        lines.append(short_desc)

    verifications = task.get("verifications") or []
    if verifications:
        lines.append("\nVerifications:")
        for v in verifications:
            if not isinstance(v, dict):
                continue
            kind  = v.get("kind", "auto")
            vid   = v.get("id", "?")
            check = v.get("check", "")
            entry = f"  [{kind}] {vid}: {check}"
            if kind == "auto" and v.get("cmd"):
                entry += "\n    cmd: " + str(v["cmd"])
            lines.append(entry)
    else:
        lines.append("\nNo structured verifications in this task block.")

    notify_telegram("\n".join(lines))


def _show_waiting() -> None:
    state   = read_state()
    waiting = state.get("waiting_on_dan", {})
    if not waiting:
        notify_telegram("No tasks currently waiting for your review.")
        return
    lines = [f"*{len(waiting)} task(s) waiting for your review:*"]
    for did, info in waiting.items():
        lines.append(f"• ID {did}: `{info['task_id']}` (parked {info['parked_at'][:10]})")
    notify_telegram("\n".join(lines))
