"""Usage quota: the concurrency cap, usage-limit detection and the pause it triggers.

Extracted verbatim from the reference orchestrator. Bodies are unchanged — only the
import block, the derivation of the module-level path globals, and (since 2026-08-30)
the two thresholds that now come from `project.yaml` rather than being hardcoded, differ. Behavioural
surprises are catalogued in `docs/FOUND_BUGS.md` and pinned by
`tests/characterization/test_quota.py`; none of them is fixed here.

**A6 (2026-09-02) adds one thing beside the extracted behaviour, not into it:**
`record_backend_exhausted`/`exhausted_backends`, a timed *per-backend* record next to
`paused_until`'s single global timestamp. `paused_until` cannot name a backend, so once
the reactive net (`orchestrator.reconcile_in_flight`) discovers one is spent, every
*later* fresh launch still resolves onto it and fails again until the pause's own reset —
self-correcting each time, but one failed launch per new task instead of one per outage.
`record_backend_exhausted` writes `backend -> reset_at` where that discovery already
happens (no new detection: `reset_at` comes straight from the `ExitVerdict` the driver
already returns); `exhausted_backends` reads it back as the `exhausted=` set
`maestro.roles.resolve` already accepts, on the three fresh-launch call sites
(`implementer._implementer_backend`, `orchestrator._launch_backend`,
`agentcall.resolve_call`) that must move together. Expiry is automatic — once
`now >= reset_at` a record is inert — and a missing/blank/unparseable `reset_at` is
refused at write time and ignored at read time, never treated as "exhausted forever".
"""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from maestro.backends import registry
from maestro.config import threshold
from maestro.paths import Paths
from maestro.pending import deferred
from maestro.state import append_journal, read_json, read_state, write_state

_PATHS = Paths.from_env()

REPO                  = _PATHS.repo
USAGE_JSON            = _PATHS.usage

# `thresholds:` in `project.yaml`, read once at import — the same shape and the same
# moment `maestro.watchdog` reads its own two (`stall_window_min`, `max_stall_restarts`).
# Both keys below were declared in the template from the first version and read by
# nothing: a project could set `concurrency_cap: 1` to throttle itself and get three
# implementers anyway. `tests/test_templates.py` recorded them as known-dead; this is
# what strikes them off.
CONCURRENCY_CAP = threshold("concurrency_cap", 3, cast=int)
THROTTLE_75_PCT = 75.0
THROTTLE_75_CAP = 1
#: Five-hour usage at or above which the loop stops launching entirely. Renamed from
#: `PAUSE_92_PCT` when it became configurable: a constant whose name states its value is
#: a name that lies the moment a project sets a different one.
PAUSE_PCT = threshold("five_h_pause_pct", 92.0, cast=float)

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


# ── Per-backend exhaustion record (A6) ──
#
# `state.json[EXHAUSTED_KEY]` is `{backend: reset_at}`, kept beside `paused_until`
# rather than folded into it: `paused_until` has no backend name and this module's own
# characterisation tests pin its exact shape (`tests/characterization/test_quota.py`),
# so this is a new key, not a reinterpretation of the old one.

#: state.json key for the record. Not declared in `project.yaml` — nothing here is
#: operator configuration, it is bookkeeping the loop keeps for itself.
EXHAUSTED_KEY = "backend_exhausted"


def _exhaustion_epoch(reset_at: object) -> float:
    """Epoch (UTC) for a Zulu ISO `reset_at`, or 0.0 when it cannot be trusted.

    0.0 reads as "already in the past" everywhere this is compared against "now", which
    is the fail-safe direction the binding constraint requires: a missing, blank,
    non-string or malformed `reset_at` must never suppress a backend forever — it must
    fail toward *not* suppressing it. A sibling of `_paused_until_epoch` rather than a
    call to it: same shape, a different key, and kept separate so a future edit to one
    can never accidentally move the other's pinned behaviour.
    """
    if not isinstance(reset_at, str) or not reset_at.strip():
        return 0.0
    try:
        return datetime.strptime(reset_at, "%Y-%m-%dT%H:%M:%SZ")\
            .replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return 0.0


def record_backend_exhausted(backend: str, reset_at: str | None) -> None:
    """Remember that `backend` is quota-exhausted until `reset_at`.

    Called where `orchestrator.reconcile_in_flight` classifies a real exit as
    `quota_exhausted` — no new detection, this only remembers the `ExitVerdict` the
    driver already returned. Read back by `exhausted_backends`, so a *later* fresh
    launch (`implementer._implementer_backend`, `orchestrator._launch_backend`,
    `agentcall.resolve_call`) can skip this backend via `roles.resolve`'s `exhausted=`
    instead of failing onto it again and rediscovering the same wall.

    A blank, unparseable or already-past `reset_at` records nothing at all, rather than
    a record that could never expire or that could never suppress anything: the first
    would strand the backend forever, the second is just a wasted state write. A blank
    `backend` name is refused the same way, before either check — `registry.
    normalise_name` only folds case and whitespace, it does not validate membership, so
    an unknown-but-non-blank name is still recorded; `exhausted_backends` comparing it
    against a real fallback chain is what makes it inert. A hand-edited, non-mapping
    `EXHAUSTED_KEY` on disk (a list, say) degrades to an empty table here exactly as it
    already does on the read side, rather than raising.
    """
    name = registry.normalise_name(backend)
    epoch = _exhaustion_epoch(reset_at)
    if not name or epoch <= 0.0 or epoch <= datetime.now(timezone.utc).timestamp():
        return
    state = read_state()
    existing = state.get(EXHAUSTED_KEY)
    table = dict(existing) if isinstance(existing, dict) else {}
    table[name] = reset_at
    state[EXHAUSTED_KEY] = table
    write_state(state)


def exhausted_backends() -> frozenset[str]:
    """Backends whose recorded exhaustion has not reset yet, right now.

    Feeds `roles.resolve`'s `exhausted=` on every fresh-launch resolution. Fails toward
    an empty set on anything that cannot be trusted — an unreadable or missing state
    document, a table that is not a mapping, a name that is not a string, a `reset_at`
    that is blank/unparseable/already past — because an empty set means "suppress
    nothing", the direction the binding constraint requires; the opposite failure would
    strand every future launch onto a backend that can never look cleared again.
    """
    try:
        table = read_state().get(EXHAUSTED_KEY)
    except Exception:
        return frozenset()
    if not isinstance(table, dict):
        return frozenset()
    now = datetime.now(timezone.utc).timestamp()
    return frozenset(
        name for name, reset_at in table.items()
        if isinstance(name, str) and _exhaustion_epoch(reset_at) > now
    )


# ── Concurrency ──

def get_effective_cap() -> tuple[int, float]:
    """Return (cap, five_h_pct)."""
    usage    = read_json(USAGE_JSON)
    five_pct = float((usage.get("five_hour") or {}).get("used_pct") or 0)
    if five_pct >= PAUSE_PCT:
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
