"""B4: park a Dan-request and notify via Telegram inline keyboard (or plain text).

Extracted verbatim from the reference `scripts/send_dan_request.py`: a standalone,
`sys.argv`-driven CLI script (`main()`), not a function living inside
`orchestrator_run.py`. Function bodies are unchanged — only the import block and the
derivation of the module-level path globals differ (`REPO`/`QUESTIONS` come from
`maestro.paths.Paths.from_env()` instead of `Path(__file__).resolve().parent.parent`,
exactly as every other extracted module does).

**One deliberate infra substitution, not a behaviour fix**: `_send_telegram`'s
`urllib.request`/`urllib.parse` call is replaced with a `requests.post` call, mirroring
`maestro.hitl.telegram.notify_telegram` (R4, `docs/plans/2026-08-18-m4c-superseded-
sidecars.md`) — same payload shape (`chat_id`, `text`, `parse_mode: HTML`, `reply_markup`
json-encoded) and same return contract (the Telegram `message_id`, or `None` on any parse
failure); only the transport changes. This module is invokable as
`python -m maestro.hitl.dan_request` from the three call sites that used to shell out to
`scripts/send_dan_request.py` (`maestro.hitl.telegram._danreq`, `maestro.parking._danreq`,
`maestro.implementer._ask_question`).

Behavioural surprises are catalogued in `docs/found_bugs_inbox/telegram.md`; pinned by
`tests/characterization/test_dan_request.py`; none of them is fixed here.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from uuid import uuid4

import requests

from maestro.paths import Paths

_PATHS = Paths.from_env()

REPO      = _PATHS.repo
QUESTIONS = REPO / ".orchestrator" / "questions"

MAX_PENDING = 3


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_env() -> None:
    env_file = REPO / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ[k.strip()] = v.strip().strip('"').strip("'")


def _count_pending() -> int:
    if not QUESTIONS.exists():
        return 0
    count = 0
    for f in QUESTIONS.glob("*.json"):
        try:
            if json.loads(f.read_text()).get("status") == "pending":
                count += 1
        except Exception:
            pass
    return count


def _send_telegram(token: str, chat_id: str, text: str,
                   reply_markup: dict | None = None) -> int | None:
    """Send a message; return the Telegram message_id (for reply-threading) or None."""
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        timeout=10,
    )
    try:
        body = json.loads(resp.text)
        return body.get("result", {}).get("message_id")
    except Exception:
        return None


def build_message_and_markup(
    req_type: str, req_id: str, question: str, options: list[str] | None
) -> tuple[str, dict | None]:
    """Build the Telegram message text + inline keyboard.

    Options are enumerated in the message BODY (full text, never truncated) and the
    buttons are stacked one-per-row with short numeric labels, so long option text
    is always readable. Free-text answering is always offered.
    """
    type_emoji = {
        "decision":       "🤔",
        "manual-task":    "🛠️",
        "phase-approval": "✅",
    }.get(req_type, "❓")

    lines = [f"{type_emoji} <b>Dan-request [{req_type}]</b>", "", question]
    reply_markup: dict | None = None

    if options:
        lines.append("")
        for i, opt in enumerate(options, 1):
            lines.append(f"<b>{i}.</b> {opt}")
        lines.append("")
        lines.append(
            "<i>Tap a number below — or just reply with your own text "
            "(notes, or an answer that isn't listed).</i>"
        )
        reply_markup = {
            "inline_keyboard": [
                [{"text": str(i), "callback_data": f"danreq:{req_id}:{i - 1}"}]
                for i, _ in enumerate(options, 1)
            ]
        }
    else:
        lines.append("")
        lines.append("<i>Reply with any text to answer.</i>")

    return "\n".join(lines), reply_markup


def main() -> int:
    _load_env()

    parser = argparse.ArgumentParser(
        description="Park a Dan-request and notify via Telegram."
    )
    parser.add_argument(
        "--type", required=True,
        choices=["decision", "manual-task", "phase-approval"],
    )
    parser.add_argument("--question", required=True, help="Question text for Dan")
    parser.add_argument(
        "--options", default=None,
        help="Comma-separated options for inline buttons (omit for free-text reply)",
    )
    parser.add_argument(
        "--id", default=None,
        help="Force a specific request ID (auto-generated if omitted)",
    )
    args = parser.parse_args()

    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_ALERT_CHAT_ID", "")
    if not token or not chat_id:
        print(
            "[send_dan_request] TELEGRAM_BOT_TOKEN or TELEGRAM_ALERT_CHAT_ID not set.",
            file=sys.stderr,
        )
        return 1

    pending = _count_pending()
    if pending >= MAX_PENDING:
        print(
            f"[send_dan_request] {pending} outstanding requests ≥ cap={MAX_PENDING} — aborting.",
            file=sys.stderr,
        )
        return 1

    req_id  = args.id or (
        f"danreq-{datetime.now(tz=timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"
    )
    options = [o.strip() for o in args.options.split(",")] if args.options else None

    record = {
        "id":       req_id,
        "type":     args.type,
        "question": args.question,
        "options":  options,
        "ts":       _now_iso(),
        "status":   "pending",
    }

    QUESTIONS.mkdir(parents=True, exist_ok=True)
    q_file = QUESTIONS / f"{req_id}.json"
    q_file.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    print(f"[send_dan_request] Parked: {q_file}", file=sys.stderr)

    msg, reply_markup = build_message_and_markup(
        args.type, req_id, args.question, options
    )

    try:
        message_id = _send_telegram(token, chat_id, msg, reply_markup)
        print(f"[send_dan_request] Telegram message sent (id={req_id}).", file=sys.stderr)
    except Exception as e:
        print(f"[send_dan_request] Telegram error: {e}", file=sys.stderr)
        return 1

    # Persist the sent message_id so a free-text *reply* can be matched back to this
    # exact request (reply-threading) even when several requests are pending.
    if message_id is not None:
        record["message_id"] = message_id
        q_file.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")

    # Emit the ID to stdout so callers can capture it.
    print(req_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
