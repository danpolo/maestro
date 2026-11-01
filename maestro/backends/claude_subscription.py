"""A fresh read of the Claude subscription's quota windows, throttled across processes.

**Why this exists.** `ClaudeBackend.usage()` answers the account question from
`.orchestrator/usage.json`, which only the operator's *interactive* Claude Code statusline
writes, on its own renders, and carries forward when a render lacks rate limits. A
graph attempt runs `claude -p`, which renders no statusline, so that file is not re-read
per attempt: in DuetFlow 02 run 9 five claude attempts had identical start and settle
windows and a `model` naming the interactive session. Per-step quota telemetry needs a
read taken *at* claim and *at* settle.

**Where it comes from.** The same endpoint the `usage` skill and Claude Code's own
`/usage` read: `GET https://api.anthropic.com/api/oauth/usage` with the CLI's OAuth
access token from `~/.claude/.credentials.json`. It costs no model tokens and returns
`five_hour` / `seven_day` as `{utilization: <percent>, resets_at: <ISO>}`.

Three rules, each load-bearing:

* **At most one network fetch per `max_age_sec`, for every process on the host.** The
  sample is cached in one JSON file beside `usage.json` with its `fetched_at`, under an
  `flock`, so parallel attempts claiming in the same minute share one fetch (and one
  sample time, which the pilot export uses to attribute a window delta across them).
* **A failure is empty windows plus a reason, never an exception.** The caller is the
  graph tick, which must not fail on telemetry. A failure is cached like a success, so a
  dead endpoint is not hammered by every claim.
* **The token is read, never refreshed, never written anywhere.** Refreshing would rotate
  the refresh token under the CLI's feet; an expired token is reported as such and the
  CLI refreshes it itself the next time any claude process runs. Error text is scrubbed of
  anything that looks like a bearer token before it is cached or returned.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from maestro.backends.base import (
    FIVE_HOUR_MINUTES,
    SEVEN_DAY_MINUTES,
    Usage,
    WindowUsage,
)

__all__ = ["SOURCE", "CACHE_FILE", "USAGE_URL", "read_subscription_usage"]

SOURCE = "claude_oauth_usage_api"
CACHE_FILE = "claude_subscription_usage.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
DEFAULT_MAX_AGE_SEC = 60.0
FETCH_TIMEOUT_SEC = 10.0

#: API key -> the window duration it reports (G5: windows are keyed by minutes).
_WINDOWS = {"five_hour": FIVE_HOUR_MINUTES, "seven_day": SEVEN_DAY_MINUTES}
_SECRET_RE = re.compile(r"(Bearer\s+)?sk-[A-Za-z0-9_\-]+")

Fetch = Callable[[str, float], Mapping[str, Any]]


def _default_credentials() -> Path:
    home = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(home).expanduser() if home else Path.home() / ".claude") / ".credentials.json"


def _http_fetch(token: str, timeout: float) -> Mapping[str, Any]:
    import urllib.request

    request = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
        "User-Agent": "claude-code/maestro",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        body = json.loads(response.read())
    if not isinstance(body, Mapping):
        raise ValueError("usage response is not an object")
    return body


def _zulu(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scrub(text: str) -> str:
    return _SECRET_RE.sub("<redacted>", str(text))[:300]


def _access_token(path: Path, now: float) -> tuple[Optional[str], Optional[str]]:
    """`(token, None)` or `(None, reason)`. The token itself is never logged."""
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "no readable Claude credentials file"
    oauth = body.get("claudeAiOauth") if isinstance(body, Mapping) else None
    token = oauth.get("accessToken") if isinstance(oauth, Mapping) else None
    if not isinstance(token, str) or not token:
        return None, "no OAuth access token in the Claude credentials"
    expires = oauth.get("expiresAt")
    if isinstance(expires, (int, float)) and expires / 1000.0 <= now:
        return None, "OAuth access token expired (the claude CLI refreshes it on its next run)"
    return token, None


def _iso_epoch(value: Any) -> Optional[int]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _windows_of(body: Mapping[str, Any]) -> dict[str, dict]:
    windows: dict[str, dict] = {}
    for key, minutes in _WINDOWS.items():
        raw = body.get(key)
        if not isinstance(raw, Mapping):
            continue
        used = raw.get("utilization")
        if isinstance(used, bool) or not isinstance(used, (int, float)):
            continue
        windows[str(minutes)] = {"used_pct": float(used),
                                 "resets_at": _iso_epoch(raw.get("resets_at"))}
    return windows


@dataclass(frozen=True)
class SubscriptionUsage(Usage):
    """Account windows plus model-specific constraints, never merged into the account."""
    scoped_windows: Mapping[str, Mapping[int, WindowUsage]] = field(default_factory=dict)


def _scoped_windows_of(body: Mapping[str, Any]) -> dict:
    scoped = {}
    limits = body.get('limits') or []
    if isinstance(limits, list):
        for limit in limits:
            if not isinstance(limit, Mapping) or limit.get('kind') != 'weekly_scoped':
                continue
            scope = limit.get('scope') or {}
            model = scope.get('model') if isinstance(scope, Mapping) else None
            label = model.get('display_name') if isinstance(model, Mapping) else None
            if isinstance(label, str) and 'opus' in label.lower():
                scoped['opus'] = _windows_of({'seven_day': {
                    'utilization': limit.get('percent'), 'resets_at': limit.get('resets_at')}})
    # Historical native OAuth responses named this window directly.
    if 'opus' not in scoped and isinstance(body.get('seven_day_opus'), Mapping):
        scoped['opus'] = _windows_of({'seven_day': body['seven_day_opus']})
    return scoped


def _read_cache(path: Path) -> Optional[dict]:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(body, dict) or not isinstance(body.get("fetched_epoch"), (int, float)):
        return None
    return body


def _write_cache(path: Path, body: dict) -> None:
    try:
        tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
        tmp.write_text(json.dumps(body, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # an unwritable cache costs throttling, not the reading


def _as_usage(body: Mapping[str, Any]) -> Usage:
    windows = {}
    for key, raw in (body.get("windows") or {}).items():
        try:
            windows[int(key)] = WindowUsage(used_pct=raw.get("used_pct"),
                                            resets_at=raw.get("resets_at"))
        except (TypeError, ValueError, AttributeError):
            continue
    scoped = {}
    raw_scoped = body.get('scoped_windows')
    if isinstance(raw_scoped, Mapping):
        for scope, raw_windows in raw_scoped.items():
            if not isinstance(scope, str) or not scope or not isinstance(raw_windows, Mapping):
                continue
            constraint = {}
            for key, raw in raw_windows.items():
                try:
                    constraint[int(key)] = WindowUsage(raw.get('used_pct'), raw.get('resets_at'))
                except (TypeError, ValueError, OverflowError, AttributeError):
                    continue
            if constraint:
                scoped[scope] = constraint
    return SubscriptionUsage(
        windows=windows,
        scoped_windows=scoped,
        updated_at=str(body.get("fetched_at") or ""),
        source=SOURCE,
        error=body.get("error") or (None if windows else "no windows in the usage response"),
    )


class _Lock:
    """An exclusive `flock` on a sidecar file; a no-op where one cannot be taken."""

    def __init__(self, path: Path):
        self._path, self._handle = path, None

    def __enter__(self):
        try:
            import fcntl

            self._handle = open(self._path, "a+")  # noqa: SIM115
            fcntl.flock(self._handle, fcntl.LOCK_EX)
        except Exception:  # noqa: BLE001 — no lock only risks one duplicate fetch
            self._handle = None
        return self

    def __exit__(self, *exc):
        if self._handle is not None:
            self._handle.close()  # releases the flock
        return False


def read_subscription_usage(
    *,
    cache_path: Path,
    credentials_path: Optional[Path] = None,
    max_age_sec: float = DEFAULT_MAX_AGE_SEC,
    now: Optional[float] = None,
    fetch: Optional[Fetch] = None,
) -> Usage:
    """The account's 5h/7d windows, at most `max_age_sec` old. Never raises.

    `Usage.updated_at` is when the sample was actually *fetched* — a reuse inside the
    throttle keeps the original stamp, so a reader can see how old it is. `Usage.error`
    says why a sample has no windows.
    """
    try:
        return _read(Path(cache_path), credentials_path, max_age_sec, now, fetch)
    except Exception as exc:  # noqa: BLE001 — telemetry never fails the tick
        return Usage(source=SOURCE, error=_scrub(f"usage read failed: {type(exc).__name__}"))


def _read(cache_path, credentials_path, max_age_sec, now, fetch) -> Usage:
    clock = (lambda: now) if now is not None else time.time
    lock_path = cache_path.with_name(cache_path.name + ".lock")
    locked = cache_path.parent.is_dir()
    with (_Lock(lock_path) if locked else _NoLock()):
        stamp = clock()
        cached = _read_cache(cache_path)
        if cached is not None and 0 <= stamp - cached["fetched_epoch"] <= max_age_sec:
            return _as_usage(cached)
        token, reason = _access_token(credentials_path or _default_credentials(), stamp)
        body: dict = {"fetched_epoch": stamp, "fetched_at": _zulu(stamp), "windows": {},
                      "error": reason}
        if token is not None:
            try:
                raw = (fetch or _http_fetch)(token, FETCH_TIMEOUT_SEC)
                body["windows"] = _windows_of(raw)
                try:
                    body['scoped_windows'] = _scoped_windows_of(raw)
                except (TypeError, ValueError, OverflowError):
                    # Optional model metadata cannot invalidate the account fetch.
                    body['scoped_windows'] = {}
                body["error"] = None if body["windows"] else "no windows in the usage response"
            except Exception as exc:  # noqa: BLE001
                body["error"] = _scrub(f"fetch failed: {type(exc).__name__}: {exc}")
        if locked:
            _write_cache(cache_path, body)
        return _as_usage(body)


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
