"""The backend protocol: the value types every driver speaks and the `AgentBackend` shape.

Design authority is `docs/DESIGN.md` §6, as corrected by the M2 research pass recorded in
`docs/plans/2026-08-12-m2-backends.md`. Two of those findings are load-bearing here:

* **Usage windows are keyed by `window_minutes`, never by a name** (finding G5). One
  sampled account has *no* five-hour window at all — every rate-limit record shows
  `window_minutes: 10080` with `secondary: null`. A driver that assumed
  "primary == 5 hours" would report a window that can never fire and would silently
  disable threshold switching. `Usage.five_hour` / `Usage.seven_day` are therefore
  *convenience lookups* for 300 and 10080 that return `None` when the window is absent,
  never the storage model.
* **`context_used_pct` is best-effort and nullable** (finding G6). One backend can only
  compute it, and the exact formula is an open question. Nothing may treat `None` there
  as "no quota pressure" or let it disable switching.

`to_usage_json` / `from_usage_json` normalise to the shape already written by the
statusline sampler and already read by `maestro.quota`, so every driver and the existing
reader agree on one document::

    {"context_used_pct": null, "context_total_input_tokens": 0,
     "five_hour": {"used_pct": 95, "resets_at": 1786373400},
     "seven_day": {"used_pct": 91, "resets_at": 1786453200},
     "model": "...", "updated_at": "2026-08-10T10:36:59Z"}

`resets_at` is unix epoch seconds, `used_pct` an integer, `updated_at` UTC ISO-8601 Z.
One key is added to that shape: `windows`, the full `window_minutes -> window` map, so a
window that has no name (G5) survives a write/read round trip. It is purely additive —
`maestro.quota` reads `five_hour.used_pct` and is unaffected — and readers that do not
know it lose nothing they could previously see.

This module imports nothing outside the standard library and has no module-level `Path`
globals, so importing it can never touch a real repository.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Literal,
    Mapping,
    NamedTuple,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)

__all__ = [
    "FIVE_HOUR_MINUTES",
    "SEVEN_DAY_MINUTES",
    "NAMED_WINDOWS",
    "Capabilities",
    "LaunchSpec",
    "CompletionSpec",
    "Completion",
    "Handle",
    "ExitKind",
    "ExitVerdict",
    "WindowUsage",
    "Usage",
    "AgentBackend",
    "to_usage_json",
    "from_usage_json",
]

# Window durations in minutes. These are *labels applied to a measured duration*, not
# assumptions about which windows an account has (G5).
FIVE_HOUR_MINUTES = 300
SEVEN_DAY_MINUTES = 10080

#: The named keys of the usage.json shape, in document order, and the duration each one
#: means. Used only for reading and writing that document — never as storage.
NAMED_WINDOWS: dict[str, int] = {
    "five_hour": FIVE_HOUR_MINUTES,
    "seven_day": SEVEN_DAY_MINUTES,
}


class Capabilities(NamedTuple):
    """What a driver can do. Core code gates on these, never on `backend.name`."""

    native_resume: bool = False       # can resume a session by a stable id
    system_prompt_file: bool = False  # supports full-replace lean profiles
    usage_telemetry: bool = False     # can report live context% / quota%
    sandbox: bool = False             # can constrain filesystem/network access


@dataclass(frozen=True)
class LaunchSpec:
    """Everything a driver needs to start an agent. Backend-agnostic by construction."""

    task_id: str
    session_id: str
    model: str
    brief: str
    workspace: Path
    worktree: Path
    system_prompt_file: Optional[Path] = None
    sandbox: Optional[str] = None


@dataclass(frozen=True)
class CompletionSpec:
    """Everything a driver needs to run one bounded agent call and hand back its output.

    The other half of the protocol. `LaunchSpec`/`launch` starts a *session* — a window
    that outlives the call and is polled for sentinels. This is the synchronous case:
    ask once, block, read what came back. Maestro does that constantly (proof review,
    failure diagnosis, explanation refresh, `/ask`, `/redo`, self-fix) and before this
    existed every one of those shelled out to a single CLI by name, which meant they
    silently did nothing on a project configured for any other backend.

    `writable` is the important field. A judgment call reads a prompt and answers; it must
    not be able to touch the tree it runs in. An agentic call — `/redo`, self-fix — is
    *expected* to edit files and commit inside `cwd`. Drivers that can enforce that
    (`Capabilities.sandbox`) must; drivers that cannot are free to ignore it, which is why
    it is a request and not a guarantee.

    `resume_id` is whatever `Handle.native_id` gave for the session being continued, and is
    opaque here exactly as it is there — core code never interprets it, it only hands it
    back to the same driver. A driver without `native_resume`, or one handed a token it
    cannot use, must start a fresh call rather than fail: every caller already has a
    fall-back-to-a-full-brief path for precisely that case.
    """

    prompt: str
    model: str = ""
    timeout: int = 240
    cwd: Optional[Path] = None
    resume_id: str = ""
    log_file: Optional[Path] = None
    writable: bool = False
    add_dirs: Sequence[Path] = ()


@dataclass(frozen=True)
class Completion:
    """The result of one bounded agent call.

    `text` is the agent's answer with surrounding whitespace stripped, and `""` on any
    failure — a non-zero exit, a timeout, a crash, or a run that simply said nothing.
    Callers are uniformly best-effort, so an empty string is the one thing they all
    already handle. `returncode` and `timed_out` are there for the two callers that
    distinguish *why* (`/ask` reports a timeout to Dan in its own words; the proof gate
    refuses a review it could not obtain rather than passing it).
    """

    text: str
    returncode: int = 0
    timed_out: bool = False


@dataclass(frozen=True)
class Handle:
    """A launched agent.

    `native_id` is the backend's own resume token when it has one (a session uuid, a
    thread id) and `None` when it does not. Core code must not interpret it; it only
    hands it back to the same driver.
    """

    backend: str
    session_id: str
    native_id: Optional[str]
    workspace: Path
    window: str


#: The three outcomes core code distinguishes.
ExitKind = Literal["ok", "quota_exhausted", "crashed"]


@dataclass(frozen=True)
class ExitVerdict:
    """How a run ended.

    `reset_at` is a Zulu ISO timestamp and is only meaningful for `quota_exhausted`;
    `evidence` is a short human-readable excerpt for the journal.
    """

    kind: ExitKind
    reset_at: Optional[str] = None
    evidence: str = ""


@dataclass(frozen=True)
class WindowUsage:
    """One rate-limit window's snapshot. `resets_at` is unix epoch seconds."""

    used_pct: Optional[float] = None
    resets_at: Optional[int] = None


@dataclass(frozen=True)
class Usage:
    """A usage sample, normalised across backends.

    `windows` is keyed by **`window_minutes`** (G5). `context_used_pct` may be `None`
    (G6) and `None` never means "no pressure" — it means "not measurable here".
    """

    context_used_pct: Optional[float] = None
    windows: Mapping[int, WindowUsage] = field(default_factory=dict)
    model: Optional[str] = None
    updated_at: str = ""
    context_total_input_tokens: int = 0

    def window(self, window_minutes: int) -> Optional[WindowUsage]:
        """The window of that exact duration, or `None` when the account has none."""
        return self.windows.get(window_minutes)

    @property
    def five_hour(self) -> Optional[WindowUsage]:
        """Convenience lookup for `window(300)`. `None` when there is no such window."""
        return self.window(FIVE_HOUR_MINUTES)

    @property
    def seven_day(self) -> Optional[WindowUsage]:
        """Convenience lookup for `window(10080)`. `None` when there is no such window."""
        return self.window(SEVEN_DAY_MINUTES)

    def max_used_pct(self) -> Optional[float]:
        """The most-consumed window, whatever its duration.

        This is the window-agnostic reading a threshold should use: on an account whose
        only window is weekly, asking for `five_hour` returns `None` and a threshold
        keyed to it can never fire (G5).
        """
        values = [w.used_pct for w in self.windows.values() if w.used_pct is not None]
        return max(values) if values else None


@runtime_checkable
class AgentBackend(Protocol):
    """The whole surface a driver must provide.

    `@runtime_checkable` so the shared protocol suite can assert conformance with
    `isinstance`. (Runtime-checkable protocols check for the *presence* of members, not
    their signatures, so the suite exercises behaviour as well.)
    """

    name: str

    def capabilities(self) -> Capabilities:
        """Declare what this driver supports. Must be free of side effects."""

    def launch(self, spec: LaunchSpec) -> Handle:
        """Start an agent for `spec` and return its handle."""

    def resume(self, handle: Handle, prompt: str) -> Handle:
        """Continue an existing session with `prompt`, returning the new handle."""

    def complete(self, spec: CompletionSpec) -> Completion:
        """Run one bounded call and return what the agent said.

        Synchronous: blocks until the agent finishes, times out, or dies. Never raises —
        a failure is a `Completion` with empty `text`, because every caller is
        best-effort and an exception here would take down a poll cycle.
        """

    def parse_exit(self, rc: int, log_tail: str) -> ExitVerdict:
        """Classify a finished run as ok / quota_exhausted(reset_at) / crashed."""

    def usage(self) -> Optional[Usage]:
        """The latest usage sample, or `None` when none is available."""


# ── usage.json normalisation ──


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> Optional[int]:
    number = _as_float(value)
    return None if number is None else int(number)


def _window_to_json(window: WindowUsage) -> dict:
    """One window in the on-disk shape: integer percent, epoch-second reset."""
    used = window.used_pct
    return {
        "used_pct": None if used is None else int(round(used)),
        "resets_at": window.resets_at,
    }


def _window_from_json(raw: Any) -> Optional[WindowUsage]:
    """A window from the on-disk shape, or `None` when it carries nothing usable."""
    if not isinstance(raw, Mapping):
        return None
    used_pct = _as_float(raw.get("used_pct"))
    resets_at = _as_int(raw.get("resets_at"))
    if used_pct is None and resets_at is None:
        return None
    return WindowUsage(used_pct=used_pct, resets_at=resets_at)


def to_usage_json(usage: Usage) -> dict:
    """Render `usage` as the usage.json document both drivers and `quota.py` agree on.

    `used_pct` is written as an integer, matching what the statusline sampler writes and
    what `quota.get_effective_cap` reads; sub-integer precision is therefore not
    preserved. Windows are written twice — under their conventional names when they have
    one, and in full under `windows` keyed by `window_minutes` so an unnamed window is
    not lost (G5).
    """
    document: dict = {
        "context_used_pct": usage.context_used_pct,
        "context_total_input_tokens": int(usage.context_total_input_tokens or 0),
    }
    for key, minutes in NAMED_WINDOWS.items():
        window = usage.windows.get(minutes)
        document[key] = None if window is None else _window_to_json(window)
    document["model"] = usage.model
    document["updated_at"] = usage.updated_at
    document["windows"] = {
        str(minutes): _window_to_json(window)
        for minutes, window in sorted(usage.windows.items())
    }
    return document


def from_usage_json(document: Any) -> Usage:
    """Parse a usage.json document into a `Usage`.

    Tolerant by design: the document is written by an external sampler that can be
    mid-write, truncated or from an older version, and a malformed sample must degrade to
    "no information", never raise. The `windows` map wins over the named keys when both
    are present, because only it can express a window that has no name.
    """
    if not isinstance(document, Mapping):
        return Usage()

    windows: dict[int, WindowUsage] = {}
    raw_windows = document.get("windows")
    if isinstance(raw_windows, Mapping):
        for key, raw in raw_windows.items():
            minutes = _as_int(key)
            window = _window_from_json(raw)
            if minutes is not None and window is not None:
                windows[minutes] = window
    for key, minutes in NAMED_WINDOWS.items():
        if minutes in windows:
            continue
        window = _window_from_json(document.get(key))
        if window is not None:
            windows[minutes] = window

    model = document.get("model")
    updated_at = document.get("updated_at")
    return Usage(
        context_used_pct=_as_float(document.get("context_used_pct")),
        windows=windows,
        model=model if isinstance(model, str) else None,
        updated_at=updated_at if isinstance(updated_at, str) else "",
        context_total_input_tokens=_as_int(document.get("context_total_input_tokens")) or 0,
    )
