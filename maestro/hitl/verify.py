"""`kind: dan` verifications: ask Dan on Telegram, read his answer back (graph runner).

A task may declare checks only the operator can confirm. The graph compiles a
`dan_confirm` step between gate and merge (`workflows.compiler.insert_dan_confirmation`),
and its handler polls this channel every tick until Dan has answered.

Nothing here is a second request mechanism. A verification request is an ordinary
Dan-request record in `.orchestrator/questions/<id>.json` (`maestro.hitl.dan_request`'s
shape), so the existing machinery answers it: the inline-button callback
(`telegram._handle_danreq_callback`, `danreq:<id>:<index>`) and the free-text reply router
(`telegram._route_freetext_answer`) both write `<id>.answer`, and this module only reads
it. `shared_bot` owns the one network poller; `commands.poll_control_commands` consumes
the project's local deliveries in the controller loop.

- **One request per task per snapshot.** The request id carries the snapshot
  (`verify-<task>-<sha12>`), so re-polling never re-sends, a restarted loop finds the same
  request on disk, and an answer given for one tree can never be read for another.
- **Reject needs a reason.** A bare "Reject" tap opens a follow-up free-text request asking
  why; the reply is the reason, and the reason is the implementer's rework note.
- **Unconfigured is visible, not fatal.** With no `TELEGRAM_BOT_TOKEN` /
  `TELEGRAM_ALERT_CHAT_ID` the request is still recorded, journaled once as
  `dan_verify_unconfigured`, listed by `/waiting`, and sent as soon as
  the bot is configured. Nothing auto-approves.
- **Visible while waiting.** `state.json["awaiting_dan_verification"]` lists what waits on
  Dan. `/waiting` shows it, and `maestro watchdog` reads it to not count the loop's
  silence as a stall.
"""
from __future__ import annotations

import html
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping

from maestro.hitl import telegram as _telegram

#: `state.json` key: `task_id -> {run_id, snapshot, request_id, title, requested_at, sent}`.
STATE_KEY = "awaiting_dan_verification"
REQUEST_TYPE = "verification"
REASON_TYPE = "verification-reason"
APPROVE = "Approve"
REJECT = "Reject"
#: The manual lane's `human_action` request (G7): Dan performs the prepared step.
ACTION_TYPE = "manual-action"
ACTION_REASON_TYPE = "manual-action-reason"
DONE = "Done"
REDO = "Redo"
ABANDON = "Abandon"
#: G7 item 4: the graph runner's failed-run notice, with what Dan wants done about it.
FAILED_TYPE = "failed-run"
RETRY = "Retry"
SHELVE = "Shelve"
#: `awaiting_dan_verification` entry kinds: a `kind: dan` check, or a manual step.
#: P12 pilot finding D26: an agent's question about a case the spec never defined.
QUESTION_TYPE = "spec-question"
#: `state.json` key: the tasks waiting on Dan's answer to such questions, by task id.
QUESTIONS_STATE_KEY = "awaiting_dan_questions"
#: The `run_id` prefixes of question waits that belong to no run: a goal replan's
#: (`replan-<RP>`, P13 GDT-8) and the completion gate's (`finish-<FIN>`, GDT-10).
RUNLESS_PREFIXES = ("replan-", "finish-")
OTHER_LINE = "✍️ Other — reply to this message"

#: P12 pilot finding D29: a deny-list hit Dan approves or disapproves, with an agent's
#: recommendation. Its wait shares `QUESTIONS_STATE_KEY` (reminders, /waiting, watchdog).
DENY_TYPE = "deny-list-hit"
DISAPPROVE = "Disapprove"

#: P12 pilot finding D30: an agent asks Dan to allow a command family on the global allow
#: list. Its wait shares `QUESTIONS_STATE_KEY` too.
PERMISSION_TYPE = "permission-request"
ALLOW = "Allow"
DECLINE = "Decline"

KIND_VERIFICATION = "verification"
KIND_ACTION = "action"
KIND_OPERATOR = "operator"


@dataclass(frozen=True)
class DanVerdict:
    """Dan's answer to one request: `approved`, or `rejected` with his reason."""

    verdict: str
    reason: str
    request_id: str

    @property
    def approved(self) -> bool:
        return self.verdict == "approved"


def request_id(task_id: str, snapshot: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(task_id))
    return f"verify-{safe}-{str(snapshot)[:12]}"


def reason_request_id(req_id: str) -> str:
    return f"{req_id}-reason"


def failed_request_id(task_id: str, run_id: str) -> str:
    """One failed-run request per run: a restarted loop finds it rather than re-sending."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(task_id))
    return f"failed-{safe}-{re.sub(r'[^A-Za-z0-9_.-]', '_', str(run_id))}"


def action_request_id(task_id: str, attempt_id: str) -> str:
    """One request per `human_action` attempt: a redo's new attempt asks anew, while a
    re-poll of the same attempt (or a restarted loop) finds the same request on disk."""
    import hashlib

    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(task_id))
    return f"action-{safe}-{hashlib.sha1(str(attempt_id).encode('utf-8')).hexdigest()[:8]}"


def action_receipt(task_id: str, attempt_id: str) -> tuple[str, bool, str | None]:
    """`(request_id, asked, answer)` for one `human_action` attempt, read from disk only
    (P13 W8 `human_receipt_probe`): nothing is sent and nothing is recorded."""
    req_id = action_request_id(task_id, attempt_id)
    return req_id, _load(req_id) is not None, _answer(req_id)


def question_request_id(task_id: str, run_id: str, attempt_id: str, n: int) -> str:
    """One request per question of an attempt's round: a restarted loop finds it on disk."""
    import hashlib

    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(task_id))
    digest = hashlib.sha1(f"{run_id}|{attempt_id}|{n}".encode("utf-8")).hexdigest()[:8]
    return f"dq-{safe}-{digest}"


def deny_request_id(task_id: str, workflow_id: str, key: str) -> str:
    """One request per deny-list hit per workflow: a ruling holds for that exact hit."""
    import hashlib

    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(task_id))
    digest = hashlib.sha1(f"{workflow_id}|{key}".encode("utf-8")).hexdigest()[:8]
    return f"dl-{safe}-{digest}"


def permission_request_id(task_id: str, workflow_id: str, attempt_id: str, n: int) -> str:
    """One request per proposed family of an attempt's round (D30)."""
    import hashlib

    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(task_id))
    digest = hashlib.sha1(f"{workflow_id}|{attempt_id}|{n}".encode("utf-8")).hexdigest()[:8]
    return f"pr-{safe}-{digest}"


def _now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _questions_dir() -> Path:
    # Read at call time: a lane sandbox rebases the telegram module's path globals.
    return Path(_telegram.QUESTIONS_DIR)


def _load(req_id: str) -> dict | None:
    try:
        record = json.loads((_questions_dir() / f"{req_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _save(record: Mapping) -> None:
    folder = _questions_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{record['id']}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _answer(req_id: str) -> str | None:
    try:
        return (_questions_dir() / f"{req_id}.answer").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _wait_key(record: Mapping) -> str:
    """The `QUESTIONS_STATE_KEY` entry a question waits under: its task, unless the record
    names its own wait (a goal replan's `replan-<RP>`, P13 GDT-8)."""
    return str(record.get("wait_key") or record.get("task_id"))


def _answered_at(req_id: str) -> str:
    """When Dan answered: the answer file's write time (UTC ISO), or "" without one."""
    try:
        stamp = (_questions_dir() / f"{req_id}.answer").stat().st_mtime
    except OSError:
        return ""
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def interpret(answer: str) -> tuple[str, str]:
    """`("approved", "")`, `("rejected", reason)`, or `("need_reason", "")`.

    The button labels are the canonical answers. A typed reply that starts with
    "approve" approves; "reject" alone still needs a reason; "reject: <why>" or any other
    text is a rejection whose text is the reason — a reply that explains what is wrong is
    exactly the feedback the implementer needs.
    """
    text = str(answer or "").strip()
    lowered = text.lower()
    if lowered.startswith("approve") or lowered == 'pass':
        return "approved", ""
    match = re.match(r"^(?:reject(?:ed)?|fail)\b[\s:,.-]*(.*)$", text, re.IGNORECASE | re.DOTALL)
    if match:
        reason = match.group(1).strip()
        return ("rejected", reason) if reason else ("need_reason", "")
    if not text:
        return "need_reason", ""
    return "rejected", text


def render_request(
    task_id: str, title: str, snapshot: str, worktree: str, checks: Iterable[Mapping],
    *, operator: bool = False,
) -> tuple[str, dict]:
    """The request text (Telegram HTML) and its Approve / Reject keyboard.

    Dan's pilot feedback (DuetFlow task 03, 2026-09-26): a request he must *act* on is
    headed "Dan action needed" and gives him numbered imperative steps, a command in its
    own block; the snapshot and worktree are left out (they are on the record, and
    `{worktree}` is substituted into the commands). Three shapes, per check:

    - `steps` (the ROADMAP entry's optional structured field): numbered steps, numbering
      continuous across checks. The third-person `check` is not repeated; it is the record
      of what was verified, and the last step says what Dan should see.
    - `instructions` only (free-form, the older shape): shown as one block under the
      check's id, then the check itself.
    - neither: a pure verification, "Dan check needed", the check as a bullet.
    """
    esc = html.escape
    checks = list(checks)
    wt = str(worktree or "")
    req_id = ('operator-' if operator else '') + request_id(task_id, snapshot)

    def fill(text: object) -> str:
        return str(text or "").replace("{worktree}", wt).strip()

    action = any(check.get("steps") or fill(check.get("instructions")) for check in checks)
    head = "🛠 <b>Dan action needed" if action else "🔎 <b>Dan check needed"
    lines = [
        f"{head} — {esc(str(task_id))}</b>" + (f": {esc(str(title))}" if title else ""),
        "",
    ]
    number = 0
    confirm: list[str] = []
    for check in checks:
        steps = check.get("steps") or ()
        instructions = fill(check.get("instructions"))
        what = esc(str(check.get("check") or ""))
        if steps:
            if number == 0:
                lines.append("Your action:")
            for step in steps:
                number += 1
                do, run = fill(step.get("do")), fill(step.get("run"))
                lines.append(f"{number}) {esc(do or 'Run:')}")
                if run:
                    lines.append(f"<pre>{esc(run)}</pre>")
        elif instructions:
            ident = esc(str(check.get("id") or ""))
            lines += [
                f"Your action ({ident}):" if ident else "Your action:",
                f"<pre>{esc(instructions)}</pre>", f"Check: {what}",
            ]
        else:
            confirm.append(f"• {what}")
    if confirm:
        if action:
            lines.append("")
        lines += ["Please confirm:", *confirm]
    lines.append("")
    if action:
        lines.append(
            "Then tap <b>Approve</b> if it worked, or <b>Reject</b> and reply with what went "
            "wrong (it goes to the implementer as rework feedback). Nothing merges until you "
            "answer."
        )
    else:
        lines.append(
            "Tap <b>Approve</b> if every check holds, or <b>Reject</b> and reply with what is "
            "wrong (it goes to the implementer as rework feedback). Nothing merges until you "
            "answer."
        )
    lines.append(
        f"Text: <code>/verify {esc(str(task_id))} approve</code> · "
        f"<code>/verify {esc(str(task_id))} reject &lt;reason&gt;</code>"
    )
    lines.append(f"Need help? <code>/ask {esc(str(task_id))} &lt;question&gt;</code>")
    if operator:
        lines = [line for line in lines if not line.startswith(('Then tap ', 'Tap <b>Approve', 'Text: '))]
        lines.append('Code is merged. Tap <b>Pass</b> if the observation holds, or <b>Fail</b> and give the reason; Maestro opens a fix task.')
    markup = {"inline_keyboard": [[
        {"text": 'Pass' if operator else APPROVE, "callback_data": f"danreq:{req_id}:0"},
        {"text": 'Fail' if operator else REJECT, "callback_data": f"danreq:{req_id}:1"},
    ]]}
    return "\n".join(lines), markup


def interpret_action(answer: str) -> tuple[str, str]:
    """`("done", "")`, `("abandon", "")`, `("redo", reason)` or `("need_reason", "")`.

    The button labels are the canonical answers. "redo" alone still needs a reason (it is
    the prep agent's rework note); any other text is a redo whose text is the reason, as a
    typed reply to a verification request is a rejection with that reason.
    """
    text = str(answer or "").strip()
    lowered = text.lower()
    if lowered.startswith("done"):
        return "done", ""
    if lowered.startswith("abandon"):
        return "abandon", ""
    match = re.match(r"^redo\b[\s:,.-]*(.*)$", text, re.IGNORECASE | re.DOTALL)
    if match:
        reason = match.group(1).strip()
        return ("redo", reason) if reason else ("need_reason", "")
    if not text:
        return "need_reason", ""
    return "redo", text


#: How long a reason may run on the closed request message; the full text is on record.
_OUTCOME_REASON_CHARS = 500


def outcome_line(record: Mapping, answer: str, reason: str | None = None) -> str:
    """The line appended to a request's own message once Dan answered it (HTML).

    `reason` is the reply to the follow-up a bare Reject / Redo opens; while it has not
    come, the line says one is awaited. Any other request type shows the chosen option.
    """
    esc = html.escape

    def why(text: str | None) -> str:
        if not text:
            return "reply with the reason"
        text = str(text).strip()
        if len(text) > _OUTCOME_REASON_CHARS:
            text = text[: _OUTCOME_REASON_CHARS - 1] + "…"
        return esc(text)

    kind = record.get("type")
    if kind == PERMISSION_TYPE:
        verdict, said = interpret_permission(answer)
        if verdict == "allowed":
            return "<b>✅ Allowed</b> — added to the global allow list"
        return "<b>❌ Declined</b>" + (f" — {why(said)}" if said else "")
    if kind == DENY_TYPE:
        verdict, said = interpret_deny(answer)
        if verdict == "approved":
            return "<b>✅ Approved</b>"
        return "<b>❌ Disapproved</b>" + (f" — {why(said)}" if said else "")
    if kind == QUESTION_TYPE:
        return f"<b>✅ Chosen: {esc(str(answer).strip()[:_OUTCOME_REASON_CHARS])}</b>"
    if kind == REQUEST_TYPE:
        verdict, said = interpret(answer)
        if verdict == "approved":
            return "<b>✅ Approved</b>"
        return f"<b>❌ Rejected</b> — {why(said or reason)}"
    if kind == ACTION_TYPE:
        verdict, said = interpret_action(answer)
        if verdict == "done":
            return "<b>✅ Done</b>"
        if verdict == "abandon":
            return "<b>🛑 Abandoned</b>"
        return f"<b>🔁 Redo</b> — {why(said or reason)}"
    return f"<b>✅ {esc(str(answer).strip()[:80])}</b>"


def render_question(task_id: str, title: str, req_id: str, question: Mapping) -> tuple[str, dict]:
    """An agent's spec question (Telegram HTML) and one button per option (D26).

    Close to Claude Code's AskUserQuestion: the question, why the spec leaves it open,
    the options numbered with their consequences and the recommended one starred, and a
    free-text Other by reply (threaded by message id, `telegram._route_freetext_answer`).
    """
    esc = html.escape
    recommended = question.get("recommended")
    lines = [
        f"❓ <b>Decision needed — {esc(str(title or task_id))}</b>",
        f"<i>{esc(str(task_id))}</i>",
        "",
        esc(str(question.get("question") or "")),
        "",
        f"<i>Why the spec leaves it open:</i> {esc(str(question.get('why_undefined') or ''))}",
        "",
    ]
    buttons = []
    for i, option in enumerate(question.get("options") or ()):
        mark = f"{i + 1} ⭐" if i == recommended else f"{i + 1}"
        consequence = str(option.get("consequence") or "")
        lines.append(f"{mark} <b>{esc(str(option.get('label') or ''))}</b>"
                     + (f" — {esc(consequence)}" if consequence else ""))
        buttons.append({"text": mark, "callback_data": f"danreq:{req_id}:{i}"})
    lines += ["", OTHER_LINE]
    return "\n".join(lines), {"inline_keyboard": [buttons]}


def interpret_deny(answer: str) -> tuple[str, str]:
    """`("approved", "")` or `("disapproved", <Dan's words, if any>)` (D29).

    A tap records the button's label. A reply that starts with "approve" approves; any
    other reply disapproves, and its text goes to the implementer with the finding.
    """
    text = str(answer or "").strip()
    lowered = text.lower()
    if lowered.startswith("approve"):
        return "approved", ""
    match = re.match(r"^disapproved?\b[\s:,.-]*(.*)$", text, re.IGNORECASE | re.DOTALL)
    if match:
        return "disapproved", match.group(1).strip()
    return "disapproved", text


_RECOMMENDATION_LABELS = {"allow": "Allow", "send_back": "Send back"}


def render_deny(task_id: str, title: str, req_id: str, spec: Mapping) -> tuple[str, dict]:
    """A deny-list hit (Telegram HTML), the agent's recommendation, Approve / Disapprove.

    `spec` carries the `hit` (`merge.deny_list_hits`) and the `recommendation`
    (`{recommendation, reason, what_to_change}`, `deny_list.recommend`).
    """
    esc = html.escape
    hit = spec.get("hit") or {}
    rec = spec.get("recommendation") or {}
    kind = "a file the deny-list guards" if hit.get("kind") == "path" else "a line"
    lines = [
        f"⛔ <b>Deny-list hit — {esc(str(title or task_id))}</b>",
        f"<i>{esc(str(task_id))}</i>",
        "",
        f"<code>{esc(str(hit.get('file') or ''))}</code> adds {kind} matching "
        f"<code>{esc(str(hit.get('pattern') or ''))}</code>:",
        f"<pre>{esc(str(hit.get('line') or hit.get('match') or ''))}</pre>",
    ]
    label = _RECOMMENDATION_LABELS.get(str(rec.get("recommendation") or ""))
    if label:
        lines += ["", f"🤖 <b>Recommendation: {label}</b> — {esc(str(rec.get('reason') or ''))}"]
        if rec.get("what_to_change"):
            lines.append(f"<i>What to change:</i> {esc(str(rec['what_to_change']))}")
    else:
        lines += ["", "🤖 <b>No recommendation</b> — "
                  + esc(str(rec.get("reason") or "the recommending agent gave no usable answer"))]
    lines += [
        "",
        f"<b>{APPROVE}</b> lets this exact line merge (a changed line asks again). "
        f"<b>{DISAPPROVE}</b> sends it back to the implementer as a blocking finding; "
        "reply to this message to add what they should do instead.",
    ]
    markup = {"inline_keyboard": [[
        {"text": f"✅ {APPROVE}", "callback_data": f"danreq:{req_id}:0"},
        {"text": f"❌ {DISAPPROVE}", "callback_data": f"danreq:{req_id}:1"},
    ]]}
    return "\n".join(lines), markup


def interpret_permission(answer: str) -> tuple[str, str]:
    """`("allowed", "")` or `("declined", <Dan's words, if any>)` (D30).

    A reply that starts with "allow" allows; any other reply declines, and its text goes
    to the agent as the reason.
    """
    text = str(answer or "").strip()
    if text.lower().startswith("allow"):
        return "allowed", ""
    match = re.match(r"^declined?\b[\s:,.-]*(.*)$", text, re.IGNORECASE | re.DOTALL)
    if match:
        return "declined", match.group(1).strip()
    return "declined", text


def render_permission(task_id: str, title: str, req_id: str, spec: Mapping) -> tuple[str, dict]:
    """An agent's request to allow a command family (Telegram HTML), Allow / Decline (D30).

    `spec` carries the parsed `request` (`{denied, family, reason}`) and the `node_id`.
    """
    esc = html.escape
    request = spec.get("request") or {}
    lines = [
        f"🔐 <b>Permission request — {esc(str(title or task_id))}</b>",
        f"<i>{esc(str(task_id))} · {esc(str(spec.get('node_id') or ''))}</i>",
        "",
        "Denied:",
        f"<pre>{esc(str(request.get('denied') or ''))}</pre>",
        f"Proposed family: <code>{esc(str(request.get('family') or ''))}</code>",
        "",
        f"🤖 <i>Why:</i> {esc(str(request.get('reason') or ''))}",
        "",
        f"<b>{ALLOW}</b> adds this family to the GLOBAL allow list, for every project's "
        f"agents, and re-runs the agent. <b>{DECLINE}</b> tells the agent to work around it; "
        "reply to this message to add why.",
    ]
    markup = {"inline_keyboard": [[
        {"text": f"✅ {ALLOW}", "callback_data": f"danreq:{req_id}:0"},
        {"text": f"❌ {DECLINE}", "callback_data": f"danreq:{req_id}:1"},
    ]]}
    return "\n".join(lines), markup


def permission_records(
    *, run_id: str | None = None, task_id: str | None = None, workflow_id: str | None = None,
) -> list[dict]:
    """The permission request records on disk, oldest first, optionally narrowed (D30)."""
    return _typed_records("pr-*.json", PERMISSION_TYPE, run_id=run_id, task_id=task_id,
                          workflow_id=workflow_id)


def record_permission(spec: Mapping, *, ruling: str, note: str = "") -> dict:
    """Keep a request that is not put to Dan (refused, or already allowed): the re-run's
    brief and the round cap read it like any other, and nothing waits on it."""
    record = {**dict(spec), "type": PERMISSION_TYPE, "ts": _now(), "status": "answered",
              "sent": False, "recorded": True, "ruling": ruling, "note": note, "by": "maestro"}
    _save(record)
    return record


def _typed_records(pattern: str, kind: str, **narrow) -> list[dict]:
    folder = _questions_dir()
    out = []
    if not folder.exists():
        return out
    for path in folder.glob(pattern):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or record.get("type") != kind:
            continue
        if any(v is not None and record.get(k) != str(v) for k, v in narrow.items()):
            continue
        out.append(record)
    out.sort(key=lambda r: (str(r.get("ts", "")), str(r.get("id", ""))))
    return out


def deny_records(
    *, run_id: str | None = None, task_id: str | None = None, workflow_id: str | None = None,
) -> list[dict]:
    """The deny-list hit requests on disk, oldest first, optionally narrowed (D29)."""
    return _typed_records("dl-*.json", DENY_TYPE, run_id=run_id, task_id=task_id,
                          workflow_id=workflow_id)


def question_records(*, run_id: str | None = None, task_id: str | None = None,
                     prefix: str = "dq-") -> list[dict]:
    """The spec-question records on disk, oldest first, optionally for one run or task, or
    whose id starts with `prefix` (a goal replan's `dq-replan-`)."""
    folder = _questions_dir()
    out = []
    if not folder.exists():
        return out
    for path in folder.glob(f"{prefix}*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or record.get("type") != QUESTION_TYPE:
            continue
        if run_id is not None and record.get("run_id") != str(run_id):
            continue
        if task_id is not None and record.get("task_id") != str(task_id):
            continue
        out.append(record)
    out.sort(key=lambda r: (str(r.get("ts", "")), int(r.get("n") or 0)))
    return out


def render_action_request(
    task_id: str, title: str, req_id: str, action: str, worktree: str,
    post_action_cmd: str = "", expected_result: str = "", step_command: str = "",
) -> tuple[str, dict]:
    """The manual-step request (Telegram HTML) and its Done / Redo / Abandon keyboard.

    Mirrors legacy `parking.park_manual_action`: the worktree the prep left and the ONE
    action Dan must take.
    """
    esc = html.escape
    steps = str(action or "").replace("{worktree}", str(worktree or ""))
    lines = [
        f"🙋 <b>Manual step — {esc(str(task_id))}</b>" + (f": {esc(str(title))}" if title else ""),
        f"Worktree: <code>{esc(str(worktree or '?'))}</code>",
        "",
        "The prep is done. Your ONE action:",
        f"<pre>{esc(steps.strip() or 'perform the prepared step')}</pre>",
        f"Expected result: {esc(expected_result or 'the step completes; the gate verifies success')}",
        ("Maestro checks the captured output and exit code, plus the step check." if step_command
         else "Maestro runs the step check and records its output and exit code; Done alone is no proof."),
    ]
    if step_command:
        lines.append(f"Command captured by that script: <pre>{esc(step_command)}</pre>")
    if post_action_cmd:
        lines.append(
            f"After you tap Done, the gate first runs: <code>{esc(str(post_action_cmd))}</code>"
        )
    lines += [
        "",
        'Tap <b>Done</b> to say “I ran it” (the gate then checks success and the prep merges), '
        "<b>Redo</b> and reply with what the prep got wrong (it is re-run with your reason), "
        "or <b>Abandon</b> to stop this run.",
        f"Text: <code>/verify {esc(str(task_id))} done</code> · "
        f"<code>/verify {esc(str(task_id))} redo &lt;what is wrong&gt;</code> · "
        f"<code>/verify {esc(str(task_id))} abandon</code>",
    ]
    markup = {"inline_keyboard": [[
        {"text": DONE, "callback_data": f"danreq:{req_id}:0"},
        {"text": REDO, "callback_data": f"danreq:{req_id}:1"},
        {"text": ABANDON, "callback_data": f"danreq:{req_id}:2"},
    ]]}
    return "\n".join(lines), markup


def telegram_configured() -> bool:
    from maestro.hitl.shared_bot import credentials
    return all(credentials())


def send_via_bot(text: str, markup: Mapping | None = None) -> int | None:
    """Send on the project's dev bot, through the Dan-request transport (redacted)."""
    from maestro.hitl import dan_request

    return dan_request._send_telegram(
        os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        os.environ.get("TELEGRAM_ALERT_CHAT_ID", ""),
        text,
        dict(markup) if markup else None,
    )


def _default_read_state() -> dict:
    from maestro.state import read_state

    return read_state()


def _default_write_state(state: dict) -> None:
    from maestro.state import write_state

    write_state(state)


def _default_journal(event: str, detail: str) -> None:
    from maestro.state import append_journal

    append_journal(event, detail)


class DanVerificationChannel:
    """The graph loop's operator channel for `dan_confirm` nodes (`WorkflowRunner.dan_channel`).

    Every seam is injectable so a test never reaches the network or the real state file.
    """

    def __init__(
        self,
        *,
        send: Callable[[str, Mapping | None], int | None] | None = None,
        configured: Callable[[], bool] | None = None,
        journal: Callable[[str, str], None] | None = None,
        read_state: Callable[[], dict] | None = None,
        write_state: Callable[[dict], None] | None = None,
    ):
        self.send = send or send_via_bot
        self.configured = configured or telegram_configured
        self.journal = journal or _default_journal
        self.read_state = read_state or _default_read_state
        self.write_state = write_state or _default_write_state

    # --- the one call the handler makes -------------------------------------------------

    def poll(
        self, *, task_id: str, title: str, run_id: str, snapshot: str, worktree: str,
        checks: Iterable[Mapping], operator: bool = False,
    ) -> DanVerdict | None:
        """Dan's verdict on `snapshot`, or `None` while he has not given one.

        Idempotent: the first call records (and, when it can, sends) the request; later
        calls only read the answer, send what could not be sent, and ask for a reason
        once after a bare Reject.
        """
        req_id = ('operator-' if operator else '') + request_id(task_id, snapshot)
        record = _load(req_id)
        if record is None:
            text, markup = render_request(task_id, title, snapshot, worktree, list(checks), operator=operator)
            self._supersede(task_id, keep=req_id)
            record = {
                "id": req_id, "type": REQUEST_TYPE, "question": text,
                "options": ['Pass', 'Fail'] if operator else [APPROVE, REJECT], "markup": markup, "ts": _now(),
                "status": "pending", "task_id": str(task_id), "run_id": str(run_id),
                "snapshot": str(snapshot), "worktree": str(worktree or ""), "sent": False,
            }
            _save(record)
            self.journal("dan_verify_requested", f"{task_id} {str(snapshot)[:12]} run={run_id}")
        record = self._deliver(record)
        self._mark_awaiting(task_id, record, title=title, run_id=run_id,
                            kind=KIND_OPERATOR if operator else KIND_VERIFICATION)

        answer = _answer(req_id)
        if answer is None:
            return None
        verdict, reason = interpret(answer)
        if verdict == "need_reason":
            reason_id = reason_request_id(req_id)
            reason_record = _load(reason_id)
            if reason_record is None:
                question = (
                    f"✍️ You rejected <b>{html.escape(str(task_id))}</b> at "
                    f"<code>{html.escape(str(snapshot)[:12])}</code>. Reply to this message "
                    "with the reason — it is the implementer's rework note."
                )
                reason_record = {
                    "id": reason_id, "type": REASON_TYPE, "question": question,
                    "options": None, "ts": _now(), "status": "pending",
                    "task_id": str(task_id), "run_id": str(run_id),
                    "snapshot": str(snapshot), "sent": False,
                }
                _save(reason_record)
            self._deliver(reason_record)
            reason = _answer(reason_id)
            if not reason:
                return None
            verdict = "rejected"
        result = DanVerdict(verdict, reason, req_id)
        self._clear_awaiting(task_id, req_id)
        self.journal(
            "dan_verify_decided",
            f"{task_id} {str(snapshot)[:12]} {verdict}" + (f": {reason[:120]}" if reason else ""),
        )
        return result

    def poll_action(
        self, *, task_id: str, title: str, run_id: str, attempt_id: str, action: str,
        worktree: str, post_action_cmd: str = "", expected_result: str = "", step_command: str = "",
    ) -> DanVerdict | None:
        """Dan's answer to a manual lane's `human_action` step (G7), or `None` while he has
        not given one. `verdict` is `done`, `redo` (with his reason) or `abandon`.

        Same record, delivery and idempotency as `poll`, keyed per attempt
        (`action_request_id`) rather than per snapshot: the step is asked before the gate.
        """
        req_id = action_request_id(task_id, attempt_id)
        record = _load(req_id)
        if record is None:
            text, markup = render_action_request(
                task_id, title, req_id, action, worktree, post_action_cmd, expected_result, step_command,
            )
            self._supersede(task_id, keep=req_id)
            record = {
                "id": req_id, "type": ACTION_TYPE, "question": text,
                "options": [DONE, REDO, ABANDON], "markup": markup, "ts": _now(),
                "status": "pending", "task_id": str(task_id), "run_id": str(run_id),
                "attempt_id": str(attempt_id), "action": str(action or ""),
                "worktree": str(worktree or ""), "sent": False,
            }
            _save(record)
            self.journal("dan_action_requested", f"{task_id} {req_id} run={run_id}")
        record = self._deliver(record)
        self._mark_awaiting(task_id, record, title=title, run_id=run_id, kind=KIND_ACTION)

        answer = _answer(req_id)
        if answer is None:
            return None
        verdict, reason = interpret_action(answer)
        if verdict == "need_reason":
            reason_id = reason_request_id(req_id)
            reason_record = _load(reason_id)
            if reason_record is None:
                question = (
                    f"✍️ You asked for a redo of <b>{html.escape(str(task_id))}</b>'s prep. "
                    "Reply to this message with what is wrong — it is the prep agent's "
                    "rework note."
                )
                reason_record = {
                    "id": reason_id, "type": ACTION_REASON_TYPE, "question": question,
                    "options": None, "ts": _now(), "status": "pending",
                    "task_id": str(task_id), "run_id": str(run_id), "sent": False,
                }
                _save(reason_record)
            self._deliver(reason_record)
            reason = _answer(reason_id)
            if not reason:
                return None
            verdict = "redo"
        result = DanVerdict(verdict, reason, req_id)
        self._clear_awaiting(task_id, req_id)
        self.journal(
            "dan_action_decided",
            f"{task_id} {req_id} {verdict}" + (f": {reason[:120]}" if reason else ""),
        )
        return result

    def poll_question(self, spec: Mapping) -> str | None:
        """Dan's answer to one spec question (D26), or `None` while he has not given one.

        `spec` is the record to keep: `id`, `task_id`, `run_id`, `title` and the parsed
        `spec` question, plus whatever the runner wants back (node, attempt, round). The
        first call saves and sends it; later calls re-send what could not be sent and read
        the answer, which is an option's label (a tap) or Dan's own text (a reply).
        """
        req_id = str(spec["id"])
        record = _load(req_id)
        if record is None:
            text, markup = render_question(
                str(spec.get("task_id")), str(spec.get("title") or ""), req_id, spec["spec"],
            )
            record = {
                **dict(spec), "type": QUESTION_TYPE, "question": text,
                "options": [str(o.get("label") or "") for o in spec["spec"]["options"]],
                "markup": markup, "ts": _now(), "status": "pending", "sent": False,
            }
            _save(record)
        record = self._deliver(record)
        self._mark_question(record)
        answer = _answer(req_id)
        if answer is None:
            return None
        return answer

    def question_answer(self, req_id: str) -> str | None:
        """Dan's answer to a question asked before, read only: nothing is sent and no wait
        is marked (a completion-gate question of an older run, P13 GDT-10 follow-up L-b)."""
        return _answer(str(req_id))

    def poll_deny(self, spec: Mapping) -> str | None:
        """Dan's answer to one deny-list hit (D29), or `None` while he has not given one.

        Same shape as `poll_question`: `spec` is the record to keep (`id`, `task_id`,
        `run_id`, `workflow_id`, `title`, `hit`, `recommendation`, ...); the first call saves
        and sends it, later calls re-send what could not be sent and read the answer (a
        button's label, or Dan's reply). `mark_question_recorded` closes it.
        """
        req_id = str(spec["id"])
        record = _load(req_id)
        if record is None:
            text, markup = render_deny(
                str(spec.get("task_id")), str(spec.get("title") or ""), req_id, spec,
            )
            record = {
                **dict(spec), "type": DENY_TYPE, "question": text,
                "options": [APPROVE, DISAPPROVE], "markup": markup, "ts": _now(),
                "status": "pending", "sent": False,
            }
            _save(record)
        record = self._deliver(record)
        self._mark_question(record)
        return _answer(req_id)

    def poll_permission(self, spec: Mapping) -> str | None:
        """Dan's answer to one permission request (D30), or `None` while he has not given
        one. Same shape as `poll_deny`; `mark_question_recorded` closes it."""
        req_id = str(spec["id"])
        record = _load(req_id)
        if record is None:
            text, markup = render_permission(
                str(spec.get("task_id")), str(spec.get("title") or ""), req_id, spec,
            )
            record = {
                **dict(spec), "type": PERMISSION_TYPE, "question": text,
                "options": [ALLOW, DECLINE], "markup": markup, "ts": _now(),
                "status": "pending", "sent": False,
            }
            _save(record)
        record = self._deliver(record)
        self._mark_question(record)
        return _answer(req_id)

    def mark_question_recorded(self, req_id: str, **fields) -> dict | None:
        """The runner recorded Dan's answer as a decision: the question no longer waits."""
        record = _load(req_id)
        if record is None:
            return None
        record.update(fields, recorded=True)
        if record.get("status") == "pending":
            record["status"] = "answered"
        _save(record)
        state = self.read_state()
        awaiting = dict(state.get(QUESTIONS_STATE_KEY) or {})
        key = _wait_key(record)
        entry = awaiting.get(key)
        if entry is not None and req_id in (entry.get("request_ids") or ()):
            left = [r for r in entry["request_ids"] if r != req_id]
            if left:
                awaiting[key] = {**entry, "request_ids": left}
            else:
                awaiting.pop(key)
            state[QUESTIONS_STATE_KEY] = awaiting
            self.write_state(state)
        return record

    def supersede_question(self, req_id: str) -> bool:
        """Close a question that is still pending and can no longer be answered (an
        improvement proposal a newer run outranked, GDT-12 review M4a): `status:
        superseded`, so it is no candidate for a plain-text answer and a tap on its message
        is refused. Returns whether it was pending; an answered one is left as it is,
        including a tap whose answer file the update listener wrote while the record is
        still pending (the controller records it on its next poll)."""
        record = _load(str(req_id))
        if record is None or record.get("status") != "pending":
            return False
        if _answer(str(req_id)) is not None:
            return False
        record["status"] = "superseded"
        _save(record)
        return True

    def _mark_question(self, record: Mapping) -> None:
        # A question that holds nothing (`holds: False`: an improvement proposal, GDT-12)
        # marks no wait, so it never keeps the queue from being exhausted.
        if record.get("holds") is False:
            return
        state = self.read_state()
        awaiting = dict(state.get(QUESTIONS_STATE_KEY) or {})
        task_id = _wait_key(record)
        entry = dict(awaiting.get(task_id) or {})
        if entry.get("run_id") != str(record.get("run_id")):
            entry = {"run_id": str(record.get("run_id")), "request_ids": []}
        ids = list(entry.get("request_ids") or [])
        if record["id"] not in ids:
            ids.append(record["id"])
        new = {
            **entry, "request_ids": ids, "title": str(record.get("title") or ""),
            "requested_at": entry.get("requested_at") or record.get("ts", ""),
            "sent": bool(entry.get("sent", True)) and bool(record.get("sent")),
        }
        if awaiting.get(task_id) == new:
            return
        awaiting[task_id] = new
        state[QUESTIONS_STATE_KEY] = awaiting
        self.write_state(state)

    def request_failed(self, *, task_id: str, run_id: str, text: str) -> dict:
        """G7 item 4: the failed-run notice as a Dan request with Retry / Shelve buttons.

        Recorded once per run (`failed_request_id`) and delivered once; a restart finds the
        record. `text` is the plain notice; it is escaped for the HTML transport.
        """
        req_id = failed_request_id(task_id, run_id)
        record = _load(req_id)
        if record is None:
            question = (
                html.escape(str(text), quote=False)
                + f"\n\nTap <b>{RETRY}</b> for a fresh run on the kept branch (same as "
                f"/unpark {html.escape(str(task_id))}), or <b>{SHELVE}</b> to park it until "
                "you /unpark it."
            )
            record = {
                "id": req_id, "type": FAILED_TYPE, "question": question,
                "options": [RETRY, SHELVE],
                "markup": {"inline_keyboard": [[
                    {"text": RETRY, "callback_data": f"danreq:{req_id}:0"},
                    {"text": SHELVE, "callback_data": f"danreq:{req_id}:1"},
                ]]},
                "ts": _now(), "status": "pending", "task_id": str(task_id),
                "run_id": str(run_id), "sent": False,
            }
            _save(record)
        return self._deliver(record)

    def failed_answers(self) -> list[tuple[dict, str]]:
        """Answered failed-run requests not yet acted on, as `(record, "retry"|"shelve")`.
        A reply that is neither is ignored (and stays unanswered for the next reply)."""
        folder = _questions_dir()
        out: list[tuple[dict, str]] = []
        if not folder.exists():
            return out
        for path in sorted(folder.glob("failed-*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict) or record.get("type") != FAILED_TYPE \
                    or record.get("status") == "handled":
                continue
            answer = (_answer(str(record.get("id"))) or "").strip().lower()
            if answer.startswith("retry"):
                out.append((record, "retry"))
            elif answer.startswith("shelve"):
                out.append((record, "shelve"))
        return out

    def mark_handled(self, record: dict, outcome: str) -> None:
        record = dict(record)
        record["status"] = "handled"
        record["handled"] = outcome
        _save(record)

    def notify(self, text: str) -> bool:
        """A one-way notice on the same bot (no buttons). The caller keeps it once-only.
        Returns whether it was sent; an unconfigured bot or a failed send is journaled."""
        if not self.configured():
            self.journal("dan_notice_unconfigured", str(text)[:160])
            return False
        try:
            self.send(str(text), None)
        except Exception as exc:  # noqa: BLE001 — a notice never stops the loop
            self.journal("dan_notice_send_failed", f"{type(exc).__name__}: {str(text)[:120]}")
            return False
        return True

    # --- internals ------------------------------------------------------------------

    def _deliver(self, record: dict) -> dict:
        if record.get("sent"):
            return record
        if not self.configured():
            if not record.get("unconfigured_journaled"):
                self.journal(
                    "dan_verify_unconfigured",
                    f"{record.get('task_id')} {record['id']}: TELEGRAM_BOT_TOKEN / "
                    "TELEGRAM_ALERT_CHAT_ID not set — waiting; /verify or a configured bot "
                    "answers it",
                )
                record["unconfigured_journaled"] = True
                _save(record)
            return record
        try:
            message_id = self.send(str(record.get("question") or ""), record.get("markup"))
        except Exception as exc:  # noqa: BLE001 — a failed send retries next poll
            self.journal("dan_verify_send_failed", f"{record['id']}: {type(exc).__name__}")
            return record
        record["sent"] = True
        if message_id is not None:
            # Reply-threading: `_route_freetext_answer` matches a reply by message_id.
            record["message_id"] = message_id
        _save(record)
        return record

    def _supersede(self, task_id: str, *, keep: str) -> None:
        """A request for an older snapshot of this task can no longer be answered."""
        folder = _questions_dir()
        if not folder.exists():
            return
        for path in [*folder.glob("verify-*.json"), *folder.glob("action-*.json")]:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (
                isinstance(record, dict) and record.get("task_id") == str(task_id)
                and record.get("id") != keep and record.get("status") == "pending"
                and record.get("type") in (
                    REQUEST_TYPE, REASON_TYPE, ACTION_TYPE, ACTION_REASON_TYPE,
                )
            ):
                record["status"] = "superseded"
                _save(record)

    def _mark_awaiting(
        self, task_id: str, record: Mapping, *, title: str, run_id: str,
        kind: str = KIND_VERIFICATION,
    ) -> None:
        state = self.read_state()
        awaiting = dict(state.get(STATE_KEY) or {})
        entry = {
            "run_id": str(run_id), "snapshot": record.get("snapshot", ""),
            "request_id": record["id"], "title": str(title or ""),
            "requested_at": record.get("ts", ""), "sent": bool(record.get("sent")),
            "kind": kind,
        }
        if awaiting.get(str(task_id)) == entry:
            return
        awaiting[str(task_id)] = entry
        state[STATE_KEY] = awaiting
        self.write_state(state)

    def _clear_awaiting(self, task_id: str, req_id: str) -> None:
        state = self.read_state()
        awaiting = dict(state.get(STATE_KEY) or {})
        entry = awaiting.get(str(task_id))
        if entry is None or entry.get("request_id") != req_id:
            return
        awaiting.pop(str(task_id))
        state[STATE_KEY] = awaiting
        self.write_state(state)


# ── the operator surface: /verify, /waiting, and the loop's pruning ─────────────────


def _awaiting_entry(state: Mapping, task_id: str) -> tuple[str, dict] | None:
    awaiting = dict((state or {}).get(STATE_KEY) or {})
    if task_id in awaiting:
        return task_id, awaiting[task_id]
    folded = task_id.lower()
    for key, entry in awaiting.items():
        if key.lower() == folded:
            return key, entry
    return None


VERIFY_USAGE = (
    "Usage: /verify <task_id> approve  ·  /verify <task_id> reject <reason>  "
    "(an observation: /verify <task_id> pass | fail <reason>) "
    "(a manual step: /verify <task_id> done | redo <reason> | abandon)"
)
_ACTIONS = {
    KIND_VERIFICATION: ("approve", "reject"),
    KIND_ACTION: ("done", "redo", "abandon"),
    KIND_OPERATOR: ("pass", "fail"),
}


def handle_verify_command(arg: str, *, state: Mapping | None = None) -> str:
    """`/verify <task_id> approve|reject <reason>` — answer a waiting request by text.

    A manual step (G7) takes `done`, `redo <reason>` or `abandon` instead. Returns the reply
    for Dan. The answer is written exactly as a button tap or a reply would write it
    (`telegram._record_danreq_answer`); the controller reads it next tick.
    """
    parts = str(arg or "").strip().split(None, 2)
    known = {verb for verbs in _ACTIONS.values() for verb in verbs}
    if len(parts) < 2 or parts[1].lower() not in known:
        return VERIFY_USAGE
    task_id, action = parts[0], parts[1].lower()
    reason = parts[2].strip() if len(parts) > 2 else ""
    if state is None:
        state = _default_read_state()
    found = _awaiting_entry(state, task_id)
    if found is None:
        return f"ℹ No Dan verification is waiting for {task_id} (nor a manual step)."
    task_id, entry = found
    kind = str(entry.get("kind") or KIND_VERIFICATION)
    if action not in _ACTIONS.get(kind, ()):
        verbs = " | ".join(_ACTIONS.get(kind, ()))
        return f"{task_id} is waiting on a {kind}: /verify {task_id} {verbs}"
    req_id = str(entry.get("request_id") or "")
    if action in ("reject", "redo", "fail") and not reason:
        return f"A reason is required: /verify {task_id} {action} <what is wrong>"
    if kind == KIND_ACTION:
        answer = {"done": DONE, "abandon": ABANDON}.get(action) or f"{REDO}: {reason}"
        interpreted = interpret_action
    else:
        answer = APPROVE if action in ("approve", "pass") else f"{REJECT}: {reason}"
        interpreted = interpret
    existing = _answer(req_id)
    if existing is None:
        _telegram._record_danreq_answer(req_id, answer)
    elif interpreted(existing)[0] == "need_reason" and action in ("reject", "redo", "fail"):
        _telegram._record_danreq_answer(reason_request_id(req_id), reason)
    else:
        return f"ℹ {task_id} was already answered ({existing[:60]}) — it is being processed."
    if kind == KIND_ACTION:
        return f"✅ Recorded {action} for {task_id}'s manual step."
    verb = "approval" if action in ("approve", "pass") else "rejection"
    return f"✅ Recorded your {verb} of {task_id} at {str(entry.get('snapshot', ''))[:12]}."


def answer_waiting(
    task_id: str, verb: str, reason: str = "", *, state: Mapping | None = None,
) -> str | None:
    """Map an operator verb onto the graph request waiting on `task_id` (G7 item 3), or
    `None` when no request of a kind that verb answers is waiting.

    `/approve` is Approve (a `kind: dan` check) or Done (a manual step); `/reject` is
    Reject (reason required) or Abandon; `/redo` is a manual step's Redo.
    """
    if state is None:
        state = _default_read_state()
    found = _awaiting_entry(state, task_id)
    if found is None:
        return None
    task_id, entry = found
    kind = str(entry.get("kind") or KIND_VERIFICATION)
    mapped = {
        (KIND_VERIFICATION, "approve"): "approve",
        (KIND_VERIFICATION, "reject"): "reject",
        (KIND_ACTION, "approve"): "done",
        (KIND_ACTION, "reject"): "abandon",
        (KIND_ACTION, "redo"): "redo",
        (KIND_OPERATOR, "approve"): "pass",
        (KIND_OPERATOR, "reject"): "fail",
    }.get((kind, verb))
    if mapped is None:
        return None
    text = f"{task_id} {mapped} {reason}".strip() if mapped in ("reject", "redo", "fail") \
        else f"{task_id} {mapped}"
    return handle_verify_command(text, state=state)


def waiting_lines(state: Mapping) -> list[str]:
    """One line per task awaiting Dan's verification, for `/waiting`."""
    lines = []
    for task_id, entry in sorted(dict((state or {}).get(STATE_KEY) or {}).items()):
        sent = "" if entry.get("sent") else " (Telegram not configured — not sent)"
        if entry.get("kind") == KIND_ACTION:
            lines.append(
                f"• {task_id}: manual step{sent} — /verify {task_id} done | "
                f"/verify {task_id} redo <reason> | /verify {task_id} abandon"
            )
            continue
        if entry.get('kind') == KIND_OPERATOR:
            lines.append(f"• {task_id}: observation of {str(entry.get('snapshot', ''))[:12]}{sent} — "
                         f"/verify {task_id} pass | /verify {task_id} fail <reason>")
            continue
        lines.append(
            f"• {task_id}: verify snapshot {str(entry.get('snapshot', ''))[:12]}{sent} — "
            f"/verify {task_id} approve | /verify {task_id} reject <reason>"
        )
    return lines


def question_waiting_lines(state: Mapping) -> list[str]:
    """One line per task waiting on Dan's answer to an agent's spec question (D26)."""
    lines = []
    for task_id, entry in sorted(dict((state or {}).get(QUESTIONS_STATE_KEY) or {}).items()):
        count = len(entry.get("request_ids") or ())
        sent = "" if entry.get("sent") else " (Telegram not configured — not sent)"
        lines.append(
            f"• {task_id}: {count} decision question{'s' if count != 1 else ''}{sent} — "
            "tap an option on the ❓ message, or reply to it with your own answer"
        )
    return lines


def prune_awaiting(
    live_run_ids: Iterable[str], *, read_state: Callable[[], dict] | None = None,
    write_state: Callable[[dict], None] | None = None,
) -> None:
    """Drop entries whose run is no longer live, so a dead wait does not mask a stall."""
    read = read_state or _default_read_state
    write = write_state or _default_write_state
    state = read()
    live = set(live_run_ids)
    dirty = False
    # D26: a question's wait belongs to its run as much as a verification's does.
    for key in (STATE_KEY, QUESTIONS_STATE_KEY):
        awaiting = dict(state.get(key) or {})
        # A replan's question (P13 GDT-8) and the completion gate's (an operator check or a
        # finish conflict, GDT-10) belong to no run: each waits until Dan answers.
        kept = {k: v for k, v in awaiting.items() if str(v.get("run_id")) in live
                or (key == QUESTIONS_STATE_KEY
                    and str(v.get("run_id")).startswith(RUNLESS_PREFIXES))}
        if kept != awaiting:
            state[key] = kept
            dirty = True
    if dirty:
        write(state)


def prune_replan_waits(
    asked: Iterable[str], *, read_state: Callable[[], dict] | None = None,
    write_state: Callable[[dict], None] | None = None,
) -> None:
    """Drop goal replan waits (`run_id` `replan-<RP>`, P13 GDT-8) whose replan the goals
    store no longer has asked: answered, or lost with no question record to rebuild it
    from. Otherwise such a wait never ends, and the queue is never called exhausted."""
    prune_runless_waits("replan-", asked, read_state=read_state, write_state=write_state)


def prune_runless_waits(
    prefix: str, keep: Iterable[str], *, read_state: Callable[[], dict] | None = None,
    write_state: Callable[[dict], None] | None = None,
) -> None:
    """Drop the question waits whose `run_id` is `<prefix><key>` and whose key is not in
    `keep`: a goal replan no longer asked (`replan-`, GDT-8), or a completion-gate question
    of a verification run that is no longer the current one (`finish-<FIN>`, GDT-10: a new
    HEAD or contract revision asks again, so a stale question cannot hold the loop)."""
    read = read_state or _default_read_state
    write = write_state or _default_write_state
    state = read()
    keep = set(keep)
    awaiting = dict(state.get(QUESTIONS_STATE_KEY) or {})
    kept = {k: v for k, v in awaiting.items()
            if not str(v.get("run_id")).startswith(prefix)
            or str(v.get("run_id"))[len(prefix):] in keep}
    if kept != awaiting:
        state[QUESTIONS_STATE_KEY] = kept
        write(state)
