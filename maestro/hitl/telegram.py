"""The Telegram/HITL surface: notifications, the Bot API shim and Dan-requests.

Extracted verbatim from the reference orchestrator. Bodies are unchanged — only the
import block and the derivation of the module-level path globals differ. Names owned by
`maestro.docs.roadmap` (the ROADMAP queues and the dependency-map regeneration) are bound
to `pending()` placeholders rather than imported: `maestro.docs.roadmap` imports the two
notifiers *from here*, so importing it back would be a cycle. Behavioural surprises are
catalogued in `docs/FOUND_BUGS.md` and pinned by
`tests/characterization/test_telegram.py`; none of them is fixed here.

Nothing in this module talks to Telegram directly: every outbound call shells out through
`subprocess.run` (`bash notify_telegram.sh`, `curl`, `send_dan_request.py`), and the
characterisation tests swap this module's `subprocess` reference rather than letting
anything reach the network.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone

from maestro.paths import Paths
from maestro.pending import pending
from maestro.state import append_journal, read_state, write_state

_PATHS = Paths.from_env()

REPO                  = _PATHS.repo
ROADMAP_FILE          = REPO / "docs" / "ROADMAP.md"
LAST_MAP_SIG          = REPO / ".orchestrator" / "last_map_roadmap.sha"
VENV_PYTHON           = REPO / ".venv" / "bin" / "python3"
NOTIFY_SH             = REPO / "scripts" / "notify_telegram.sh"
SEND_DANREQ           = REPO / "scripts" / "send_dan_request.py"
QUESTIONS_DIR         = REPO / ".orchestrator" / "questions"
DEP_MAP_PNG           = REPO / "docs" / "dependency_map.png"
REMINDER_INTERVAL_SEC = 86400

# Owned by `maestro.docs.roadmap`; see the module docstring for why these are not imported.
run_dep_map = pending("run_dep_map", "maestro.docs.roadmap")
parse_runnable_tasks = pending("parse_runnable_tasks", "maestro.docs.roadmap")
parse_prep_tasks = pending("parse_prep_tasks", "maestro.docs.roadmap")


# ── Notification ──

def notify_telegram(msg: str) -> None:
    if NOTIFY_SH.exists():
        subprocess.run(["bash", str(NOTIFY_SH), msg], capture_output=True, timeout=15)


def _roadmap_signature() -> str:
    """Content hash of docs/ROADMAP.md — changes iff the task set/scope changes."""
    try:
        return hashlib.sha256(ROADMAP_FILE.read_bytes()).hexdigest()
    except Exception:
        return ""


def notify_telegram_with_map(msg: str) -> None:
    """Send a Telegram message and attach the dependency map PNG if it exists.

    Records the signature of the ROADMAP the attached PNG reflects (callers render
    the PNG from the current ROADMAP right before calling this), so the per-poll
    change-detector (maybe_push_roadmap_map_change) treats this as the latest map Dan
    has and only re-pushes on a *subsequent* ROADMAP change.
    """
    notify_telegram(msg)
    if DEP_MAP_PNG.exists():
        token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_ALERT_CHAT_ID", "")
        if token and chat_id:
            # sendPhoto rejects images past Telegram's dimension limit (width+height
            # > 10000 / ratio > 20) with PHOTO_INVALID_DIMENSIONS — and the dep-map PNG
            # crosses that as the task graph grows. Fall back to sendDocument (no
            # dimension cap, up to 50 MB) so a big map is always delivered. We check the
            # response rather than fire-and-forget, since a silent failure here is exactly
            # how Dan ended up never receiving an updated map.
            photo = subprocess.run(
                [
                    "curl", "-s", "-F", f"chat_id={chat_id}",
                    "-F", f"photo=@{DEP_MAP_PNG}",
                    f"https://api.telegram.org/bot{token}/sendPhoto",
                ],
                capture_output=True, text=True, timeout=30,
            )
            if '"ok":true' not in (photo.stdout or ""):
                subprocess.run(
                    [
                        "curl", "-s", "-F", f"chat_id={chat_id}",
                        "-F", f"document=@{DEP_MAP_PNG}",
                        f"https://api.telegram.org/bot{token}/sendDocument",
                    ],
                    capture_output=True, text=True, timeout=30,
                )
    # Record what roadmap this map reflects so the change-detector dedups against it.
    sig = _roadmap_signature()
    if sig:
        try:
            LAST_MAP_SIG.write_text(sig)
        except Exception:
            pass


# ── B6: Phase completion report ──

def phase_report(task_id: str, task_def: dict, smoke: dict) -> None:
    """Send Telegram report on task completion. Attaches dependency_map.png."""
    r5   = smoke.get("metrics", {}).get("recall_at_5", "?")
    lat  = smoke.get("metrics", {}).get("latency_p95_ms", "?")
    desc = (task_def or {}).get("short_desc", task_id)

    # Find the next queued task(s). The main loop acts on TWO queues: autonomous
    # runnables (parse_runnable_tasks) AND the manual/needs-dan auto-prep queue
    # (parse_prep_tasks, which parse_runnable_tasks deliberately excludes). Report
    # both in execution order — otherwise "Next: none queued" prints right before
    # the loop preps a needs-dan task like P10.
    try:
        seen: set[str] = set()
        entries: list[str] = []
        for t in parse_runnable_tasks():
            if t["id"] not in seen:
                seen.add(t["id"]); entries.append(t["id"])
        for t in parse_prep_tasks():
            if t["id"] not in seen:
                seen.add(t["id"]); entries.append(f"{t['id']} (prep)")
        next_up = ", ".join(entries[:2]) if entries else "none queued"
    except Exception:
        next_up = "?"

    decisions = (task_def or {}).get("decisions_made", "")
    decision_block = f"\n📋 *Decisions made:* {decisions}\n⏳ Veto window: 24 h" if decisions else ""

    # Non-retrieval tasks skip the Recall@5 smoke → no metrics to show.
    if smoke.get("metrics"):
        eval_line = f"📊 Recall@5: {r5}  |  p95 latency: {lat} ms"
    else:
        eval_line = f"📊 {smoke.get('reason', 'retrieval smoke skipped')[:90]}"

    msg = (
        f"✅ *{task_id} shipped* — {desc}\n"
        f"{eval_line}\n"
        f"{decision_block}\n"
        f"▶ Next: {next_up}"
    )
    # Regenerate dep map before attaching
    try:
        run_dep_map()
    except Exception:
        pass
    notify_telegram_with_map(msg)


# ── HITL helpers ──

def _danreq(question: str, options: list[str], req_type: str = "decision",
            req_id: str | None = None) -> None:
    cmd = [str(VENV_PYTHON), str(SEND_DANREQ),
           "--type", req_type, "--question", question,
           "--options", ",".join(options)]
    if req_id:
        cmd += ["--id", req_id]
    subprocess.run(cmd, cwd=str(REPO), capture_output=True)


# ── B12: inbound Dan-request answers on the dev bot ──
# Dan-requests are SENT on the dev bot (TELEGRAM_BOT_TOKEN); this orchestrator is the
# only consumer of that bot's getUpdates. So button taps (callback_query) and free-text
# replies must be recorded HERE — telegram_bot.py's handlers live on the *RAG* bot and
# never see these updates. Answers are written as raw text to <id>.answer (compatible
# with both _answer_choice and the proposal reader poll_proposal_answer).

def _tg_api(method: str, params: dict) -> None:
    """Best-effort Telegram Bot API call on the dev bot (never raises)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return
    args = ["curl", "-s", "--max-time", "5",
            f"https://api.telegram.org/bot{token}/{method}"]
    for k, v in params.items():
        args += ["--data-urlencode", f"{k}={v}"]
    try:
        subprocess.run(args, capture_output=True, timeout=8)
    except Exception:
        pass


def _pending_danreqs() -> list[dict]:
    """All pending Dan-request records (newest first)."""
    out: list[dict] = []
    if QUESTIONS_DIR.exists():
        for f in QUESTIONS_DIR.glob("*.json"):
            try:
                r = json.loads(f.read_text())
            except Exception:
                continue
            if isinstance(r, dict) and r.get("status") == "pending":
                out.append(r)
    out.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return out


def _record_danreq_answer(req_id: str, answer_text: str) -> None:
    """Write the raw answer + flip the request to answered. Raw text is what both
    _answer_choice and poll_proposal_answer expect."""
    answer_text = (answer_text or "").strip()
    QUESTIONS_DIR.mkdir(parents=True, exist_ok=True)
    (QUESTIONS_DIR / f"{req_id}.answer").write_text(answer_text + "\n", encoding="utf-8")
    q_file = QUESTIONS_DIR / f"{req_id}.json"
    try:
        rec = json.loads(q_file.read_text())
        rec["status"] = "answered"
        q_file.write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception:
        pass
    append_journal("danreq_answered", f"{req_id}: {answer_text[:80]}")


def _handle_danreq_callback(cq: dict) -> None:
    """Record a button tap (callback_data = danreq:<req_id>:<option_index>)."""
    cq_id = cq.get("id", "")
    data  = cq.get("data") or ""
    if not data.startswith("danreq:"):
        return
    try:
        _, req_id, idx_str = data.split(":", 2)
        idx = int(idx_str)
    except (ValueError, AttributeError):
        _tg_api("answerCallbackQuery", {"callback_query_id": cq_id, "text": "Invalid"})
        return
    q_file = QUESTIONS_DIR / f"{req_id}.json"
    if not q_file.exists():
        _tg_api("answerCallbackQuery", {"callback_query_id": cq_id, "text": "Request not found"})
        return
    try:
        rec    = json.loads(q_file.read_text())
        opts   = rec.get("options") or []
        choice = opts[idx] if 0 <= idx < len(opts) else str(idx)
    except Exception:
        choice = str(idx)
    _record_danreq_answer(req_id, choice)
    _tg_api("answerCallbackQuery", {"callback_query_id": cq_id, "text": f"✅ {choice[:50]}"})
    # Strip the keyboard so it can't be tapped twice.
    msg = cq.get("message") or {}
    if msg.get("message_id"):
        _tg_api("editMessageReplyMarkup", {
            "chat_id":      msg.get("chat", {}).get("id", ""),
            "message_id":   msg["message_id"],
            "reply_markup": json.dumps({"inline_keyboard": []}),
        })


def _route_freetext_answer(text: str, reply_to: dict | None) -> None:
    """Route a plain-text reply to a pending Dan-request: match the replied-to message
    first (reply-threading via stored message_id), else the most-recent pending one."""
    pend = _pending_danreqs()
    if not pend:
        return
    target = None
    if reply_to and reply_to.get("message_id") is not None:
        mid = reply_to["message_id"]
        target = next((r for r in pend if r.get("message_id") == mid), None)
    if target is None:
        target = pend[0]
    _record_danreq_answer(str(target["id"]), text)
    notify_telegram(f"✅ Recorded your answer for {target['id']}.")


def _escalation_req_id(task_id: str, reason: str) -> str:
    """Deterministic Dan-request id for a failure escalation, keyed on task +
    *normalized* reason.

    Volatile tokens (session/branch ids like impl-p8b2-20260619-115951, bare
    timestamps, digits) are collapsed so the SAME class of failure maps to ONE
    request id across attempts. That makes park_failed idempotent: Dan is asked
    about a given failure once, and his answer is reused on every recurrence
    instead of a fresh "what to do?" being parked each poll cycle.
    """
    norm = reason.lower()
    norm = re.sub(r"impl-[a-z0-9_-]+", "impl-*", norm)  # session/branch ids
    norm = re.sub(r"\d+", "#", norm)                    # timestamps / counts
    norm = re.sub(r"\s+", " ", norm).strip()
    h = hashlib.sha1(f"{task_id}|{norm}".encode("utf-8")).hexdigest()[:8]
    return f"esc-{task_id}-{h}"


# ── Parked-item reminders ──

def _send_hitl_reminders() -> None:
    """Send a reminder for items parked > 24 h with no response.

    Throttled: this runs every poll (POLL_INTERVAL=30 s). Without a per-item
    cooldown a once-parked item would re-ping Telegram every 30 s forever once it
    crossed 24 h (the bug Dan hit with P12C/P14). We stamp `reminded_at` on the
    waiting entry and only re-send once per REMINDER_INTERVAL_SEC.
    """
    state   = read_state()
    waiting = state.get("waiting_on_dan", {})
    if not waiting:
        return
    now = datetime.now(tz=timezone.utc)
    dirty = False
    for did, info in waiting.items():
        try:
            parked = datetime.fromisoformat(info["parked_at"])
        except Exception:
            continue
        # Handoff B: awaiting-verification items auto-finalize via the poller (or surface
        # on failure/timeout). Don't nag Dan to /approve them — there's nothing to do.
        if info.get("kind") == "awaiting-verification":
            continue
        if (now - parked).total_seconds() <= 86400:
            continue
        # already reminded recently? skip until the cooldown elapses.
        last_raw = info.get("reminded_at")
        if last_raw:
            try:
                if (now - datetime.fromisoformat(last_raw)).total_seconds() < REMINDER_INTERVAL_SEC:
                    continue
            except Exception:
                pass
        verb = "/approve {0}".format(did) if info.get("kind") == "manual-action" \
            else "/approve {0} or /reject {0}".format(did)
        notify_telegram(
            f"⏰ Reminder: `{info['task_id']}` (ID {did}) has been waiting > 24 h.\n"
            f"Reply {verb}"
        )
        info["reminded_at"] = now.isoformat()
        dirty = True
    if dirty:
        state["waiting_on_dan"] = waiting
        write_state(state)
