"""Usage quota: the concurrency cap, usage-limit detection and the pause it triggers.

Extracted verbatim from the reference orchestrator. Bodies are unchanged — only the
import block and the derivation of the module-level path globals differ. Behavioural
surprises are catalogued in `docs/FOUND_BUGS.md` and pinned by
`tests/characterization/test_quota.py`; none of them is fixed here.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from maestro.paths import Paths
from maestro.pending import deferred
from maestro.state import append_journal, read_json, read_state, write_state

_PATHS = Paths.from_env()

REPO                  = _PATHS.repo
USAGE_JSON            = _PATHS.usage

CONCURRENCY_CAP = 3
THROTTLE_75_PCT = 75.0
THROTTLE_75_CAP = 1
PAUSE_92_PCT    = 92.0

# Owned by `maestro.hitl.telegram`, which is extracted after this module. Late-bound
# rather than imported so this module stays importable on its own and the M1 extraction
# order keeps a single direction; the name is resolved on the first call.
notify_telegram = deferred("notify_telegram", "maestro.hitl.telegram")

_LIMIT_RE = re.compile(
    r"(hit (?:your|the) (?:usage )?limit|usage limit reached|rate[\s-]?limit"
    r"|too many requests|overloaded_error)", re.I)
_RESET_RE = re.compile(
    r"resets?\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)?(?:\s*\(([\w/]+)\))?", re.I)


# `_tail_text` is mapped to `maestro.implementer`, which is extracted after this module.
# `_scan_impl_log_for_limit` calls it on every path the characterisation tests exercise,
# so a `pending()` placeholder would make the module untestable. The body is copied
# verbatim here; when `maestro.implementer` lands, one of the two copies becomes an
# import of the other.
def _tail_text(path: Path, n_lines: int = 25) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace")
                         .splitlines()[-n_lines:])
    except Exception:
        return ""


def _resolve_limit_reset(tail: str) -> str:
    """Best reset time (UTC ISO) for a detected usage limit. Prefers usage.json's
    authoritative epoch; falls back to parsing 'resets 4:30pm (Tz)'; final fallback
    is a conservative +1h so the loop never hammers a closed window."""
    now = datetime.now(timezone.utc)
    try:
        usage = read_json(USAGE_JSON)
        epoch = (usage.get("five_hour") or {}).get("resets_at")
        if epoch and float(epoch) > now.timestamp():
            return datetime.fromtimestamp(float(epoch), tz=timezone.utc)\
                .strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        pass
    m = _RESET_RE.search(tail)
    if m:
        try:
            hh = int(m.group(1)); mm = int(m.group(2) or 0)
            ap = (m.group(3) or "").lower(); tzname = m.group(4)
            if ap == "pm" and hh != 12: hh += 12
            if ap == "am" and hh == 12: hh = 0
            tz = ZoneInfo(tzname) if tzname else None
            base = datetime.now(tz) if tz else datetime.now().astimezone()
            cand = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if cand <= base:
                cand += timedelta(days=1)
            return cand.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            pass
    return (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scan_impl_log_for_limit(workspace: Path) -> dict:
    """Detect a Claude usage-limit exit in the implementer's impl.log.
    Returns {'limit': bool, 'reset_iso': str|None, 'evidence': str}."""
    tail = _tail_text(workspace / "impl.log", 25)
    if not tail or not _LIMIT_RE.search(tail):
        return {"limit": False, "reset_iso": None, "evidence": ""}
    evidence = next((ln for ln in tail.splitlines() if _LIMIT_RE.search(ln)),
                    tail[-200:])
    return {"limit": True, "reset_iso": _resolve_limit_reset(tail),
            "evidence": evidence.strip()[:200]}


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


def _paused_until_epoch(state: dict) -> float:
    """Epoch (UTC) of state.paused_until, or 0.0 if unset/unparseable."""
    val = state.get("paused_until")
    if not val:
        return 0.0
    try:
        return datetime.strptime(str(val), "%Y-%m-%dT%H:%M:%SZ")\
            .replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return 0.0


# ── Concurrency ──

def get_effective_cap() -> tuple[int, float]:
    """Return (cap, five_h_pct)."""
    usage    = read_json(USAGE_JSON)
    five_pct = float((usage.get("five_hour") or {}).get("used_pct") or 0)
    if five_pct >= PAUSE_92_PCT:
        return 0, five_pct
    if five_pct >= THROTTLE_75_PCT:
        return THROTTLE_75_CAP, five_pct
    return CONCURRENCY_CAP, five_pct


def _elapsed_min(started_at: str) -> int | None:
    """Minutes since an ISO `started_at` (now_iso format), or None if unparseable."""
    try:
        start = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(tz=timezone.utc) - start).total_seconds()) // 60)
    except Exception:
        return None


def _fmt_min(mins: int) -> str:
    h, m = divmod(max(0, mins), 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _elapsed_str(started_at: str) -> str:
    """Human elapsed time since an ISO `started_at` (now_iso format)."""
    mins = _elapsed_min(started_at)
    return _fmt_min(mins) if mins is not None else "?"


def _parse_est_minutes(est_time: str) -> int | None:
    """Best-effort: pull the leading `~Nh` / `~N min` token out of the free-text
    est_time so we can hint a rough ETA. Returns minutes, or None if unparseable."""
    m = re.search(r"~?\s*(\d+(?:\.\d+)?)\s*(h|hour|hr|min|m)\b", est_time or "", re.I)
    if not m:
        return None
    val, unit = float(m.group(1)), m.group(2).lower()
    return int(val * 60) if unit.startswith("h") else int(val)
