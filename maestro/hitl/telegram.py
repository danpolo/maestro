"""The Telegram/HITL surface: notifications, the Bot API shim and Dan-requests.

Extracted verbatim from the reference orchestrator. Bodies are unchanged — only the
import block and the derivation of the module-level path globals differ. Names owned by
`maestro.docs.roadmap` (the ROADMAP queues and the dependency-map regeneration) are bound
to `deferred()` late bindings rather than imported: `maestro.docs.roadmap` imports the two
notifiers *from here*, so importing it back at module level would be a cycle. Behavioural
surprises are catalogued in `docs/FOUND_BUGS.md` and pinned by
`tests/characterization/test_telegram.py`. P13 W1 adds shared credentials/tags and refuses
ambiguous or unmatched free-text replies instead of guessing the newest pending request.

Most outbound calls in this module shell out through `subprocess.run` (`curl`,
`python -m maestro.hitl.dan_request` — the M4c batch-3 in-repo home of the reference's
`scripts/send_dan_request.py`); the characterisation tests swap this module's `subprocess`
reference rather than letting anything reach the network. `notify_telegram` is the one
exception — R4 (`docs/plans/2026-08-18-m4c-superseded-sidecars.md`) moved it off the
reference's now-deleted shell notifier script onto a native `requests.post` call straight
to the Bot API; the tests stub `requests.post` for that one instead.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone

import requests

from maestro.paths import Paths
from maestro.status import redact_secrets
from maestro import metrics
from maestro.pending import deferred
from maestro.state import append_journal, read_state, write_state
from maestro.hitl.shared_bot import tagged
from maestro.hitl import shared_bot

_PATHS = Paths.from_env()

REPO                  = _PATHS.repo
ROADMAP_FILE          = REPO / "docs" / "ROADMAP.md"
LAST_MAP_SIG          = REPO / ".orchestrator" / "last_map_roadmap.sha"
VENV_PYTHON           = REPO / ".venv" / "bin" / "python3"
QUESTIONS_DIR         = REPO / ".orchestrator" / "questions"
DEP_MAP_PNG           = REPO / "docs" / "dependency_map.png"
DEP_MAP_MD            = REPO / "docs" / "dependency_map.md"
#: The map message pinned in Dan's chat: `{message_id, chat_id, ts}`. Durable, so a
#: restarted loop still unpins the map it pinned before.
PINNED_MAP            = REPO / ".orchestrator" / "pinned_map.json"
REMINDER_INTERVAL_SEC = 86400

# Owned by `maestro.docs.roadmap`; see the module docstring for why these are not imported.
# Late-bound, so the name resolves on first call without creating the cycle.
run_dep_map = deferred("run_dep_map", "maestro.docs.roadmap")
parse_runnable_tasks = deferred("parse_runnable_tasks", "maestro.docs.roadmap")
parse_prep_tasks = deferred("parse_prep_tasks", "maestro.docs.roadmap")


# ── Notification ──

#: Telegram rejects a `sendMessage` body over 4096 UTF-16 code units outright — the whole
#: message is lost, not truncated. `[P11]`'s operator projections are the first thing maestro
#: sends that can legitimately exceed it (a graph with a dozen nodes and their evidence), so
#: `send_report` splits rather than silently dropping the answer to a question Dan asked.
TELEGRAM_MESSAGE_LIMIT = 4000


def delivery_note() -> str:
    """`""`, or `" telegram=unconfigured"` for a journal line that says a notice went out.

    `notify_telegram` skips silently without both variables, so a `*_notified` journal line
    alone proved nothing: the P12 pilot journaled every notice while Dan received none.
    """
    if all(shared_bot.credentials()):
        return ""
    return " telegram=unconfigured"


def notify_telegram(msg: str) -> None:
    """Best-effort notification; retain the legacy no-result contract."""
    notify_intake(msg)


def notify_intake(msg: str, *, repo=None) -> bool:
    """Send one operator message, reporting confirmed Bot API delivery. Never raises.

    `[P11]`: every outgoing body is passed through `status.redact_secrets` first. Telegram is
    the one place maestro's internal text leaves the machine, and the projections and reports
    that reach it now quote controller state; a credential that ends up in one would be
    forwarded, cached and indexed by a third party before anyone noticed.
    """
    return _send_operator_message(msg, repo=repo) is not None


def _send_operator_message(msg: str, *, repo=None, reply_markup=None) -> dict | None:
    """Existing shared transport with an optional keyboard and delivery receipt."""
    token, chat_id = shared_bot.credentials()
    if not token or not chat_id:
        return None
    try:
        data = {"chat_id": chat_id,
                "text": redact_secrets(tagged(msg, REPO if repo is None else repo))}
        if reply_markup is not None:
            data['reply_markup'] = json.dumps(reply_markup)
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
            timeout=10,
        )
        body = response.json() if response.ok else None
        return body if isinstance(body, dict) and body.get('ok') else None
    except Exception:
        return None


def notify_sprint(msg: str, token: str, *, repo) -> int | None:
    body = _send_operator_message(msg, repo=repo, reply_markup={'inline_keyboard': [[{
        'text': 'Skip this week', 'callback_data': f'sprint:skip:{token}',
    }]]})
    mid = ((body or {}).get('result') or {}).get('message_id')
    return mid if isinstance(mid, int) and not isinstance(mid, bool) and mid > 0 else None


def send_report(text: str, *, header: str = "") -> int:
    """Deliver a possibly-long operator report as one or more messages; returns how many.

    Splits on line boundaries so a node row or an evidence line is never cut in half, and
    numbers the parts when there is more than one — an operator reading a graph on a phone
    has to be able to tell that part 2 exists.
    """
    body = redact_secrets(text)
    lines = body.splitlines() or [""]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if size + len(line) + 1 > TELEGRAM_MESSAGE_LIMIT and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line[:TELEGRAM_MESSAGE_LIMIT])
        size += len(line) + 1
    chunks.append("\n".join(current))
    total = len(chunks)
    for index, chunk in enumerate(chunks, 1):
        label = f"{header} ({index}/{total})" if header and total > 1 else header
        notify_telegram(f"{label}\n{chunk}" if label else chunk)
    return total


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
    if DEP_MAP_PNG.exists() and not _map_png_is_stale():
        token, chat_id = shared_bot.credentials()
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
                    "-F", f"caption={tagged('Dependency map', REPO)}",
                    "-F", f"photo=@{DEP_MAP_PNG}",
                    f"https://api.telegram.org/bot{token}/sendPhoto",
                ],
                capture_output=True, text=True, timeout=30,
            )
            sent = photo
            if '"ok":true' not in (photo.stdout or ""):
                sent = subprocess.run(
                    [
                        "curl", "-s", "-F", f"chat_id={chat_id}",
                        "-F", f"caption={tagged('Dependency map', REPO)}",
                        "-F", f"document=@{DEP_MAP_PNG}",
                        f"https://api.telegram.org/bot{token}/sendDocument",
                    ],
                    capture_output=True, text=True, timeout=30,
                )
            _pin_latest_map(chat_id, _sent_message_id(getattr(sent, "stdout", None)))
    # Record what roadmap this map reflects so the change-detector dedups against it.
    sig = _roadmap_signature()
    if sig:
        try:
            LAST_MAP_SIG.write_text(sig)
        except Exception:
            pass


def _map_png_is_stale() -> bool:
    """Whether the PNG predates the map it renders — the render failed after the .md was
    regenerated, so sending it would show Dan an old graph as the current one."""
    try:
        return DEP_MAP_PNG.stat().st_mtime < DEP_MAP_MD.stat().st_mtime
    except OSError:
        return False


def _sent_message_id(stdout: object) -> int | None:
    try:
        body = json.loads(str(stdout or ""))
    except ValueError:
        return None
    if not isinstance(body, dict) or not body.get("ok"):
        return None
    mid = (body.get("result") or {}).get("message_id")
    return mid if isinstance(mid, int) else None


def _pin_latest_map(chat_id: str, message_id: int | None) -> None:
    """Pin the map just sent and unpin the one pinned before (Dan's pilot feedback,
    2026-09-26: the latest map is always the pinned message). Never raises."""
    if message_id is None:
        return
    try:
        previous = json.loads(PINNED_MAP.read_text(encoding="utf-8"))
    except Exception:
        previous = {}
    _tg_api("pinChatMessage", {"chat_id": chat_id, "message_id": message_id,
                               "disable_notification": "true"})
    old_id = previous.get("message_id") if isinstance(previous, dict) else None
    if old_id and old_id != message_id:
        _tg_api("unpinChatMessage", {"chat_id": previous.get("chat_id") or chat_id,
                                     "message_id": old_id})
    try:
        PINNED_MAP.parent.mkdir(parents=True, exist_ok=True)
        tmp = PINNED_MAP.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "message_id": message_id, "chat_id": str(chat_id),
            "ts": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }) + "\n", encoding="utf-8")
        os.replace(tmp, PINNED_MAP)
    except Exception:
        pass


# ── B6: Phase completion report ──

def phase_report(task_id: str, task_def: dict, smoke: dict, follow_ups: str = "") -> None:
    """Send Telegram report on task completion. Attaches dependency_map.png.

    `follow_ups`: where the accepted run's review follow-ups went, one line
    (`maestro.docs.followups.Recorded.summary`), or '' for none."""
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

    # A skipped smoke has no numbers, so its *reason* is the report. When there are
    # numbers they are named by whatever this project's adapter called them — the two
    # lines here used to be hardcoded to one project's `recall_at_5`/`latency_p95_ms`.
    if metrics.metrics_of(smoke):
        eval_line = f"📊 {metrics.summary(smoke)}"
    else:
        eval_line = f"📊 {smoke.get('reason', metrics.NO_METRICS)[:90]}"

    msg = (
        f"✅ *{task_id} shipped* — {desc}\n"
        f"{eval_line}\n"
        f"{decision_block}\n"
        + (f"🧾 {follow_ups}\n" if follow_ups else "")
        + f"▶ Next: {next_up}"
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
    cmd = [str(VENV_PYTHON), "-m", "maestro.hitl.dan_request",
           "--type", req_type, "--question", question,
           "--options", ",".join(options)]
    if req_id:
        cmd += ["--id", req_id]
    subprocess.run(cmd, cwd=str(REPO), capture_output=True)


# ── B12: inbound Dan-request answers from the shared bot's local mailbox ──
# The shared poller routes button taps and free-text replies to their project; the
# controller records them here. Answers are written as raw text to <id>.answer (compatible
# with both _answer_choice and the proposal reader poll_proposal_answer).

def _tg_api(method: str, params: dict) -> None:
    """Best-effort Telegram Bot API call on the shared Maestro bot (never raises)."""
    token, _ = shared_bot.credentials()
    if not token:
        return
    if 'text' in params and method in ('sendMessage', 'editMessageText'):
        params = {**params, 'text': tagged(params['text'], REPO)}
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


def _record_danreq_answer(req_id: str, answer_text: str, *, close: bool = True) -> None:
    """Write the raw answer + flip the request to answered. Raw text is what both
    _answer_choice and poll_proposal_answer expect.

    `close` (Dan's pilot feedback, 2026-09-26): also edit the request's own Telegram
    message to show the outcome and drop its buttons (`_close_request_message`), so an
    answer given by reply or `/verify` is visible where he asked. A button tap passes
    `close=False`: it closes the message itself, from the tap's own message ids."""
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
    if close:
        _close_request_message(req_id, answer_text)


#: The id suffix of the follow-up that asks Dan why (`verify.reason_request_id`).
_REASON_SUFFIX = "-reason"


def _load_request(req_id: str) -> dict | None:
    try:
        rec = json.loads((QUESTIONS_DIR / f"{req_id}.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    return rec if isinstance(rec, dict) else None


def _close_request_message(
    req_id: str, answer_text: str, *, message_id=None, chat_id=None,
) -> None:
    """Show the outcome on the request's own message and remove its buttons. Never raises.

    Only a record that stores the exact body it was sent (`markup` present — every
    `maestro.hitl.verify` request) is re-rendered as `<question>\n\n<outcome>`; for any
    other record the keyboard is only stripped, as before, since its stored `question` is
    not the text Dan sees. An answer to a reason follow-up (`<id>-reason`) completes its
    parent: "❌ Rejected — <reason>" on the original request.
    """
    try:
        from maestro.hitl import verify

        target_id, answer, reason = req_id, answer_text, None
        if req_id.endswith(_REASON_SUFFIX):
            target_id = req_id[: -len(_REASON_SUFFIX)]
            try:
                answer = (QUESTIONS_DIR / f"{target_id}.answer").read_text(encoding="utf-8")
            except OSError:
                return
            reason = answer_text
        rec = _load_request(target_id)
        if reason is not None and not (rec or {}).get("markup"):
            return
        rec = rec or {}
        if message_id is None:
            message_id = rec.get("message_id")
        if chat_id is None:
            chat_id = os.environ.get("TELEGRAM_ALERT_CHAT_ID", "")
        if not message_id:
            return
        if not rec.get("markup") or not rec.get("question"):
            _tg_api("editMessageReplyMarkup", {
                "chat_id": chat_id, "message_id": message_id,
                "reply_markup": json.dumps({"inline_keyboard": []}),
            })
            return
        outcome = verify.outcome_line(rec, answer.strip(), reason)
        _tg_api("editMessageText", {
            "chat_id": chat_id, "message_id": message_id,
            "text": redact_secrets(f"{rec['question']}\n\n{outcome}"),
            "parse_mode": "HTML",
            "reply_markup": json.dumps({"inline_keyboard": []}),
        })
    except Exception:
        pass


def _danreq_tap(data: str) -> tuple[str, str, str]:
    """`(req_id, choice, "")` for a tap's `danreq:<req_id>:<index>` data, or
    `("", "", <toast>)` when it names nothing that can be answered."""
    try:
        _, req_id, idx_str = data.split(":", 2)
        idx = int(idx_str)
    except (ValueError, AttributeError):
        return "", "", "Invalid"
    q_file = QUESTIONS_DIR / f"{req_id}.json"
    if not q_file.exists():
        return "", "", "Request not found"
    try:
        rec    = json.loads(q_file.read_text())
        opts   = rec.get("options") or []
        choice = opts[idx] if 0 <= idx < len(opts) else str(idx)
    except Exception:
        rec, choice = {}, str(idx)
    if isinstance(rec, dict) and rec.get("status") == "superseded":
        return "", "", "No longer open"      # nobody reads its answer any more
    return req_id, choice, ""


def _acknowledge_tap(cq: dict, req_id: str, choice: str) -> None:
    """The Telegram side of a tap: stop the client's spinner, then close the message."""
    _tg_api("answerCallbackQuery", {"callback_query_id": cq.get("id", ""),
                                    "text": f"✅ {choice[:50]}"})
    # Close the message (outcome + no keyboard) so it can't be tapped twice.
    msg = cq.get("message") or {}
    if msg.get("message_id"):
        _close_request_message(
            req_id, choice, message_id=msg["message_id"],
            chat_id=msg.get("chat", {}).get("id", ""),
        )


def _handle_danreq_callback(cq: dict) -> None:
    """Record a button tap (callback_data = danreq:<req_id>:<option_index>).

    A tap the update listener already acknowledged (`ack_danreq_callback`, marked
    `_acked`) is only recorded here — the Telegram side was done on arrival."""
    cq_id = cq.get("id", "")
    data  = cq.get("data") or ""
    if not data.startswith("danreq:"):
        return
    acked = bool(cq.get("_acked"))
    req_id, choice, error = _danreq_tap(data)
    if error:
        if not acked:
            _tg_api("answerCallbackQuery", {"callback_query_id": cq_id, "text": error})
        return
    _record_danreq_answer(req_id, choice, close=False)
    if not acked:
        _acknowledge_tap(cq, req_id, choice)


def ack_danreq_callback(cq: dict) -> bool:
    """The update listener's half of a tap (Dan's pilot feedback, 2026-09-26: the spinner
    and the buttons stayed until the controller's next poll). Acknowledges the tap and
    closes the message at once, and writes the answer file so a restart before the
    controller records it loses nothing. Returns whether the controller should record it
    (`_handle_danreq_callback` with `_acked`); the journal and the control store are the
    controller thread's alone, so they are not touched here."""
    data = cq.get("data") or ""
    if not isinstance(data, str) or not data.startswith("danreq:"):
        return False
    req_id, choice, error = _danreq_tap(data)
    if error:
        _tg_api("answerCallbackQuery", {"callback_query_id": cq.get("id", ""), "text": error})
        return False
    try:
        if _load_request(req_id) is None:
            raise ValueError("unreadable record")  # mid-write: the controller resolves it
        QUESTIONS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = QUESTIONS_DIR / f".{req_id}.answer.tmp"
        tmp.write_text(choice.strip() + "\n", encoding="utf-8")
        os.replace(tmp, QUESTIONS_DIR / f"{req_id}.answer")
    except Exception:
        pass  # the controller writes it when it records the tap
    _acknowledge_tap(cq, req_id, choice)
    return True


def _route_freetext_answer(text: str, reply_to: dict | None) -> None:
    """Route a plain-text reply to a pending Dan-request: match the replied-to message
    first (reply-threading via stored message_id), else only a unique pending request that
    holds work: a question that holds nothing (`holds: False`, an improvement proposal or
    rollback, GDT-12 review M4b) may stay unanswered for days, so it is answered only by its
    button or a reply to its message, never by unthreaded text."""
    pend = _pending_danreqs()
    if not pend:
        return
    target = None
    if reply_to and reply_to.get("message_id") is not None:
        mid = reply_to["message_id"]
        target = next((r for r in pend if r.get("message_id") == mid), None)
    if target is None:
        holding = [r for r in pend if r.get("holds") is not False]
        if reply_to or len(holding) != 1:
            notify_telegram('Reply to the exact pending request message; this answer was not recorded.')
            return
        target = holding[0]
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
    dirty = _remind_dan_verifications(state)
    dirty = _remind_dan_questions(read_state() if dirty else state) or dirty
    if dirty:
        state = read_state()
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
        if info.get('kind') in ('awaiting-verification', 'operator-verification',
                                'post-merge-checking', 'post-merge-repair',
                                'post-merge-failed', 'verification-drift', 'operator-failed'):
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


#: `state.json` key: when each graph `kind: dan` request was last reminded about, by
#: request id. Kept beside `awaiting_dan_verification` rather than inside its entries,
#: which `maestro.hitl.verify` rewrites whole.
VERIFY_REMINDED_KEY = "awaiting_dan_verification_reminded"


def _remind_dan_verifications(state: dict) -> bool:
    """The graph runner's waits on Dan (`awaiting_dan_verification`) get the same 24 h
    reminder, at most once per `REMINDER_INTERVAL_SEC`, as `waiting_on_dan` items.

    Only a request that was actually sent is reminded about: an unconfigured bot has
    nothing to remind through. Returns whether it wrote the state.
    """
    awaiting = state.get("awaiting_dan_verification") or {}
    stamps = dict(state.get(VERIFY_REMINDED_KEY) or {})
    live = {str(e.get("request_id")) for e in awaiting.values() if isinstance(e, dict)}
    dirty = set(stamps) - live != set()
    stamps = {req: at for req, at in stamps.items() if req in live}
    now = datetime.now(tz=timezone.utc)
    for task_id, entry in awaiting.items():
        if not isinstance(entry, dict) or not entry.get("sent"):
            continue
        req = str(entry.get("request_id") or "")
        try:
            asked = datetime.fromisoformat(str(entry.get("requested_at", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if (now - asked).total_seconds() <= 86400:
            continue
        last = stamps.get(req)
        if last:
            try:
                if (now - datetime.fromisoformat(last)).total_seconds() < REMINDER_INTERVAL_SEC:
                    continue
            except ValueError:
                pass
        if entry.get("kind") == "action":
            # G7: a manual lane's human step (`verify.KIND_ACTION`).
            notify_telegram(
                f"⏰ Reminder: `{task_id}` has waited > 24 h for your manual step ({req}); "
                f"nothing merges until you answer.\n"
                f"Tap Done/Redo/Abandon on the request, or /verify {task_id} done · "
                f"/verify {task_id} redo <reason> · /verify {task_id} abandon"
            )
        elif entry.get('kind') == 'operator':
            notify_telegram(
                f"⏰ Reminder: `{task_id}` has waited > 24 h for your observation ({req}); "
                f"execution is complete and acceptance awaits your result.\n"
                f"Tap Pass/Fail on the request, or /verify {task_id} pass · "
                f"/verify {task_id} fail <reason>"
            )
        else:
            notify_telegram(
                f"⏰ Reminder: `{task_id}` has waited > 24 h for your verification ({req}); "
                f"nothing merges until you answer.\n"
                f"Tap Approve/Reject on the request, or /verify {task_id} approve · "
                f"/verify {task_id} reject <reason>"
            )
        stamps[req] = now.isoformat()
        dirty = True
    if dirty:
        state[VERIFY_REMINDED_KEY] = stamps
        write_state(state)
    return dirty


#: `state.json` key: when each task's unanswered spec questions (D26) were last reminded
#: about, by task id — beside `awaiting_dan_questions`, which `verify` rewrites whole.
QUESTIONS_REMINDED_KEY = "awaiting_dan_questions_reminded"


def _remind_dan_questions(state: dict) -> bool:
    """A task paused on an agent's question (`awaiting_dan_questions`, D26) gets the same
    24 h reminder, at most once per `REMINDER_INTERVAL_SEC`, as a verification wait.

    Only questions that were actually sent are reminded about. Returns whether it wrote
    the state.
    """
    awaiting = state.get("awaiting_dan_questions") or {}
    stamps = dict(state.get(QUESTIONS_REMINDED_KEY) or {})
    dirty = set(stamps) - set(awaiting) != set()
    stamps = {task_id: at for task_id, at in stamps.items() if task_id in awaiting}
    now = datetime.now(tz=timezone.utc)
    for task_id, entry in awaiting.items():
        if not isinstance(entry, dict) or not entry.get("sent"):
            continue
        try:
            asked = datetime.fromisoformat(str(entry.get("requested_at", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if (now - asked).total_seconds() <= 86400:
            continue
        last = stamps.get(task_id)
        if last:
            try:
                if (now - datetime.fromisoformat(last)).total_seconds() < REMINDER_INTERVAL_SEC:
                    continue
            except ValueError:
                pass
        count = len(entry.get("request_ids") or ())
        notify_telegram(
            f"⏰ Reminder: `{task_id}` has waited > 24 h for your answer to {count} decision "
            f"question{'s' if count != 1 else ''}; the task is paused until you answer.\n"
            "Tap an option on the ❓ message, or reply to it with your own answer."
        )
        stamps[task_id] = now.isoformat()
        dirty = True
    if dirty:
        state[QUESTIONS_REMINDED_KEY] = stamps
        write_state(state)
    return dirty
