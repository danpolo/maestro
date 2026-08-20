"""The Codex driver: `codex exec`, resumed by thread id, with real usage telemetry.

Design authority is `docs/DESIGN.md` §6 as corrected by the M2 research pass recorded in
`docs/plans/2026-08-12-m2-backends.md`. Everything below that looks like a quirk is a
*verified* finding from that pass, not a guess, and each one is named where it is
encoded so it is not "simplified" away later:

* **F1 — Codex has a real resume handle, so `native_resume` is `True`.** The handle is the
  `thread_id` on the first JSONL event of a `codex exec --json` run
  (`{"type": "thread.started", "thread_id": ...}`). It is a documented contract, not an
  internal detail, and the session file on disk is named after it.
* **F2 — `codex exec` rejects `-a/--ask-for-approval` and `--full-auto`** (exit 2, they are
  interactive-only). Approval policy in `exec` is set with `-c approval_policy="never"`.
  Sandbox spellings are exactly `read-only`, `workspace-write`, `danger-full-access`.
* **G1 — `exec resume` accepts none of `-s`, `-C`, `--add-dir`, `-p`.** They must be placed
  *before* the `resume` subcommand, where they do take effect. So the argv is
  `codex exec <exec flags…> resume <thread id> <prompt>`, never the other way round.
* **G2 — `codex exec resume --last` hangs** for minutes scanning every rollout file on
  disk. This module never emits `--last`; resume is always by explicit thread id.
* **G3 — a resume against an unknown thread id fails fast and free**: exit 1,
  `no rollout found for thread id`, no model call, no quota. That is what makes the
  fallback in the generated launcher honest — a dead handle costs nothing to discover, so
  there is no reason to probe for it in advance (and no way to probe a *live* handle
  without spending a turn).
* **G4 — the `app-server` fallback needs stdin held open.** On EOF the server tears down
  its stdout writer before flushing and exits **0 with zero bytes**. "Exit 0, no output"
  is therefore a *failed sample*, never "this account has no limits" — conflating the two
  would silently disable switching.
* **G5 — windows are keyed by `window_minutes`, never by the names `primary`/`secondary`.**
  One sampled account has only a 10080-minute window with `secondary: null`; a driver that
  assumed "primary == five hours" would publish a window that can never fire.
* **G6 — `context_used_pct` is best-effort and nullable.** `total_token_usage.total_tokens`
  is cumulative across a session and routinely exceeds `model_context_window`, and the
  exact TUI formula was not traced. This module computes from the *per-turn*
  `last_token_usage` and returns `None` rather than a number it cannot stand behind.
  Nothing may read `None` as "no pressure".

Usage telemetry has two sources, in order:

1. **The rollout JSONL** (`<codex home>/sessions/<Y>/<M>/<D>/rollout-*-<thread id>.jsonl`),
   which every session appends `token_count` events to, carrying the full rate-limit
   snapshot. Passive: costs nothing and needs no extra process.
2. **`codex app-server`**, spoken as line-delimited JSON-RPC over stdio
   (`account/rateLimits/read`), for when no run is active. Also free of model tokens.

**Nothing here runs a binary at import time or from `capabilities()`.** Every execution
goes through the injectable `runner`/`popen`/`probe` seams, and this module has no
module-level `Path` globals — so importing it, or asking it what it can do, can never
touch a real repository, spend quota or reach a network.
"""
from __future__ import annotations

import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

from maestro.backends import registry
from maestro.backends.base import (
    Capabilities,
    ExitVerdict,
    Handle,
    LaunchSpec,
    Usage,
    WindowUsage,
)

__all__ = [
    "BINARY",
    "SANDBOX_MODES",
    "DEFAULT_SANDBOX",
    "APPROVAL_POLICY_OVERRIDE",
    "DEAD_THREAD_MARKER",
    "THREAD_ID_FILE",
    "LAUNCH_META_FILE",
    "BASELINE_TOKENS",
    "APP_SERVER_METHOD",
    "CodexBackend",
    "exec_flags",
    "launch_argv",
    "resume_argv",
    "sandbox_mode",
    "thread_id_from_line",
    "thread_id_from_text",
    "is_dead_thread",
    "window_from_snapshot",
    "windows_from_rate_limits",
    "context_used_pct",
    "exhaustion_signal",
    "app_server_requests",
]

#: The executable this driver drives. `registry` owns resolving it to a path.
BINARY = "codex"

#: Exactly the spellings `codex exec -s` accepts (F2). Anything else exits 2.
SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")

#: An implementer has to be able to write in its worktree.
DEFAULT_SANDBOX = "workspace-write"

#: Approval policy is a config override in `exec`; the flags are interactive-only (F2).
#: The value is TOML, hence the quotes — they are part of the argument, not shell syntax.
APPROVAL_POLICY_OVERRIDE = 'approval_policy="never"'

#: What the CLI says when the thread id it was handed no longer exists (G3).
DEAD_THREAD_MARKER = "no rollout found for thread id"

#: Files this driver owns inside an implementer workspace.
BRIEF_FILE = "brief.txt"
LOG_FILE = "impl.log"
LAUNCH_SCRIPT = "launch.py"
THREAD_ID_FILE = "codex_thread_id.txt"
LAUNCH_META_FILE = "codex_launch.json"
RESUME_PROMPT_FILE = "resume_prompt.txt"

#: Codex reserves this much of the context window before the conversation starts (G6).
BASELINE_TOKENS = 12000

#: The JSON-RPC method `codex app-server` answers with a rate-limit snapshot.
APP_SERVER_METHOD = "account/rateLimits/read"

#: How long to wait for that answer before giving up. A telemetry read must never be
#: able to wedge the orchestrator.
APP_SERVER_TIMEOUT_SEC = 20.0

#: How much of a run's output the generated launcher keeps in memory to recognise a dead
#: handle. The dead-handle failure is a few hundred bytes, so this is generous.
LAUNCHER_TAIL_CHARS = 8000

#: Codex's own wording for exhaustion. Deliberately *not* `quota._LIMIT_RE`, which encodes
#: the other CLI's phrasing and would both miss this one and fire on unrelated text.
_RATE_LIMIT_REACHED_RE = re.compile(r"rate[_\s-]?limit[_\s-]?reached", re.I)
_RATE_LIMIT_REACHED_KEYS = ("rate_limit_reached_type", "rateLimitReachedType")

_ISO_Z = "%Y-%m-%dT%H:%M:%SZ"


# ── small tolerant readers ──
#
# Every input here is written by another program and may be truncated, mid-write or from
# a different CLI version. A malformed sample must degrade to "no information", never
# raise: telemetry that throws would take the orchestrator down with it.


def _pick(mapping: Any, *keys: str) -> Any:
    """The first of `keys` actually present in `mapping`. Handles snake vs camel case.

    The rollout JSONL uses snake_case (`used_percent`, `window_minutes`, `resets_at`) and
    the app-server's JSON-RPC uses camelCase (`usedPercent`, `windowDurationMins`,
    `resetsAt`) for the same fields. Both are normalised here rather than at two call
    sites.
    """
    if not isinstance(mapping, Mapping):
        return None
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


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


def _zulu(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(_ISO_Z)


def _iter_json_lines(text: str) -> Iterator[Mapping]:
    """Every line of `text` that parses as a JSON object, in order. Others are skipped."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            parsed = json.loads(stripped)
        except Exception:
            continue
        if isinstance(parsed, Mapping):
            yield parsed


def _walk(node: Any, depth: int = 6) -> Iterator[Mapping]:
    """Every mapping inside `node`, outermost first, to a bounded depth."""
    if depth < 0:
        return
    if isinstance(node, Mapping):
        yield node
        for value in node.values():
            yield from _walk(value, depth - 1)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _walk(value, depth - 1)


# ── argv construction ──


def sandbox_mode(value: Optional[str]) -> str:
    """Validate a sandbox mode, defaulting when unset.

    Raises `ValueError` on an unknown spelling rather than passing it through: `codex
    exec` would reject it with exit 2 and an "unexpected argument" message that says
    nothing about where the bad value came from, and the run would have been launched
    into a tmux window where nobody is watching.
    """
    mode = (value or DEFAULT_SANDBOX).strip()
    if mode not in SANDBOX_MODES:
        raise ValueError(
            f"unknown codex sandbox mode {value!r}; expected one of {', '.join(SANDBOX_MODES)}"
        )
    return mode


def exec_flags(
    *,
    model: str,
    worktree: Path | str,
    sandbox: Optional[str] = None,
    add_dirs: Sequence[Path | str] = (),
) -> list[str]:
    """The flags shared by a fresh `exec` and a `resume`, in one fixed order.

    `--skip-git-repo-check` is required because an implementer worktree is a linked git
    worktree, and `-C` points the run at it. There is deliberately no `-a`/`--full-auto`
    here: `exec` rejects both (F2).
    """
    flags = [
        "--json",
        "--skip-git-repo-check",
        "-c",
        APPROVAL_POLICY_OVERRIDE,
        "-m",
        str(model),
        "-s",
        sandbox_mode(sandbox),
        "-C",
        str(worktree),
    ]
    for extra in add_dirs:
        flags += ["--add-dir", str(extra)]
    return flags


def launch_argv(
    binary: str,
    *,
    model: str,
    worktree: Path | str,
    brief: str,
    sandbox: Optional[str] = None,
    add_dirs: Sequence[Path | str] = (),
) -> list[str]:
    """A fresh briefed run. The brief is positional and last."""
    return [
        str(binary),
        "exec",
        *exec_flags(model=model, worktree=worktree, sandbox=sandbox, add_dirs=add_dirs),
        brief,
    ]


def resume_argv(
    binary: str,
    thread_id: str,
    prompt: str,
    *,
    model: str,
    worktree: Path | str,
    sandbox: Optional[str] = None,
    add_dirs: Sequence[Path | str] = (),
) -> list[str]:
    """Continue an existing thread.

    **The flags come before the `resume` subcommand** (G1): `resume` itself accepts none
    of `-s`, `-C`, `--add-dir` or `-p`, but when they are placed ahead of it they do take
    effect — the resumed turn records the new cwd and sandbox policy. The thread id is
    always explicit; `--last` is never emitted, because it hangs for minutes scanning
    every rollout on disk (G2).
    """
    if not thread_id:
        raise ValueError("cannot resume without a thread id")
    return [
        str(binary),
        "exec",
        *exec_flags(model=model, worktree=worktree, sandbox=sandbox, add_dirs=add_dirs),
        "resume",
        str(thread_id),
        prompt,
    ]


# ── the resume handle ──


def thread_id_from_line(line: str) -> Optional[str]:
    """The thread id if `line` is the `thread.started` event, else `None` (F1)."""
    stripped = (line or "").strip()
    if not stripped.startswith("{") or "thread" not in stripped:
        return None
    try:
        event = json.loads(stripped)
    except Exception:
        return None
    if not isinstance(event, Mapping) or event.get("type") != "thread.started":
        return None
    value = event.get("thread_id")
    return str(value) if value else None


def thread_id_from_text(text: str) -> Optional[str]:
    """The **first** thread id in a run's output. Later ones belong to later runs."""
    for line in (text or "").splitlines():
        found = thread_id_from_line(line)
        if found:
            return found
    return None


def is_dead_thread(rc: int, output: str) -> bool:
    """Whether a run failed because the thread id it was given no longer exists (G3)."""
    return rc != 0 and DEAD_THREAD_MARKER in (output or "")


# ── usage telemetry ──


def window_from_snapshot(
    raw: Any, *, now: Optional[datetime] = None
) -> Optional[tuple[int, WindowUsage]]:
    """One rate-limit window as `(window_minutes, WindowUsage)`, or `None` if unusable.

    Keyed by its **duration** (G5). A record with no duration is dropped rather than
    guessed at: a window whose length is unknown cannot be compared against a threshold.
    """
    minutes = _as_int(_pick(raw, "window_minutes", "windowDurationMins", "windowMinutes"))
    if minutes is None:
        return None
    used = _as_float(_pick(raw, "used_percent", "usedPercent", "used_pct"))
    resets_at = _as_int(_pick(raw, "resets_at", "resetsAt"))
    if resets_at is None:
        resets_in = _as_int(_pick(raw, "resets_in_seconds", "resetsInSeconds"))
        if resets_in is not None:
            moment = now or datetime.now(timezone.utc)
            resets_at = int(moment.timestamp()) + resets_in
    if used is None and resets_at is None:
        return None
    return minutes, WindowUsage(used_pct=used, resets_at=resets_at)


def windows_from_rate_limits(
    raw: Any, *, now: Optional[datetime] = None
) -> dict[int, WindowUsage]:
    """Every window in a rate-limit snapshot, keyed by `window_minutes` (G5).

    Both shapes are accepted, because both are real: the rollout JSONL writes
    `{"primary": {...}, "secondary": null}` in snake_case and the app-server answers in
    camelCase. Field *names* are never used as keys — `primary` on the sampled account is
    the weekly window, not a five-hour one. When two entries report the same duration the
    higher percentage wins, so a snapshot can only ever overstate pressure, never hide it.
    """
    windows: dict[int, WindowUsage] = {}
    if not isinstance(raw, Mapping):
        return windows
    for value in raw.values():
        entry = window_from_snapshot(value, now=now)
        if entry is None:
            continue
        minutes, window = entry
        current = windows.get(minutes)
        if current is None or (window.used_pct or 0) >= (current.used_pct or 0):
            windows[minutes] = window
    return windows


def context_used_pct(info: Any) -> Optional[float]:
    """How much of the context window this turn used, or `None` when unknowable (G6).

    Computed from the **per-turn** `last_token_usage`, never from `total_token_usage`,
    which is cumulative across the session and routinely exceeds the context window
    outright — reading that as a percentage is the naive mistake this exists to avoid.
    Codex's own helper reserves `BASELINE_TOKENS` before the conversation starts, so both
    the numerator and the denominator net it out.

    The exact TUI formula was never traced, so this is explicitly provisional: anything
    that lands outside 0–100 means the assumption is wrong for that record, and `None` —
    "not measurable here" — is returned instead of a number nothing should trust. Callers
    must never read `None` as "no pressure".
    """
    if not isinstance(info, Mapping):
        return None
    window = _as_int(_pick(info, "model_context_window", "modelContextWindow"))
    last = _pick(info, "last_token_usage", "lastTokenUsage")
    tokens = _as_int(_pick(last, "total_tokens", "totalTokens"))
    if not window or tokens is None:
        return None
    usable = window - BASELINE_TOKENS
    if usable <= 0:
        return None
    pct = 100.0 * (tokens - BASELINE_TOKENS) / usable
    if pct > 100.0:
        return None
    return max(0.0, pct)


def _token_count_payload(event: Mapping) -> Optional[Mapping]:
    """The payload of a `token_count` event message, or `None` for any other record."""
    if event.get("type") != "event_msg":
        return None
    payload = event.get("payload")
    if not isinstance(payload, Mapping) or payload.get("type") != "token_count":
        return None
    return payload


def _rate_limits_of(payload: Mapping) -> Optional[Mapping]:
    """The rate-limit snapshot on a `token_count` payload, wherever it sits."""
    for key in ("rate_limits", "rateLimits"):
        raw = payload.get(key)
        if isinstance(raw, Mapping):
            return raw
    info = payload.get("info")
    if isinstance(info, Mapping):
        for key in ("rate_limits", "rateLimits"):
            raw = info.get(key)
            if isinstance(raw, Mapping):
                return raw
    return None


# ── exit classification ──


def exhaustion_signal(log_tail: str) -> Optional[dict]:
    """Codex's own exhaustion evidence in a run's output, or `None`.

    Two signals, and the difference between them matters to `parse_exit`:

    * `explicit` — a `rate_limit_reached_type` field, or Codex's `rate_limit_reached`
      wording in a plain-text error line. The turn *failed* on a limit.
    * not `explicit` — a `token_count` snapshot showing a window at 100 %. The account is
      out of quota, but this alone does not say the run ended badly: a run that finished
      its work and happened to land on the last percent still succeeded.

    `quota._LIMIT_RE` is deliberately not reused: it encodes the other CLI's phrasing.
    """
    text = log_tail or ""
    for event in _iter_json_lines(text):
        for node in _walk(event):
            for key in _RATE_LIMIT_REACHED_KEYS:
                if node.get(key):
                    return {
                        "explicit": True,
                        "reason": str(node.get(key)),
                        "resets_at": _resets_at_of(event),
                        "evidence": json.dumps(event)[:200],
                    }
        payload = _token_count_payload(event)
        limits = _rate_limits_of(payload) if payload is not None else None
        if limits is not None:
            windows = windows_from_rate_limits(limits)
            spent = {
                minutes: window
                for minutes, window in windows.items()
                if (window.used_pct or 0) >= 100
            }
            if spent:
                minutes = max(spent, key=lambda m: spent[m].used_pct or 0)
                return {
                    "explicit": False,
                    "reason": f"window_minutes={minutes} at {spent[minutes].used_pct}%",
                    "resets_at": spent[minutes].resets_at,
                    "evidence": json.dumps(limits)[:200],
                }

    match = _RATE_LIMIT_REACHED_RE.search(text)
    if match:
        line = next(
            (ln for ln in text.splitlines() if _RATE_LIMIT_REACHED_RE.search(ln)), ""
        )
        return {
            "explicit": True,
            "reason": match.group(0),
            "resets_at": None,
            "evidence": line.strip()[:200],
        }
    return None


def _resets_at_of(event: Mapping) -> Optional[int]:
    """Any reset epoch reachable from an event, preferring the soonest."""
    resets = [
        _as_int(_pick(node, "resets_at", "resetsAt"))
        for node in _walk(event)
    ]
    known = [value for value in resets if value]
    return min(known) if known else None


def _last_nonempty_line(text: str) -> str:
    for line in reversed((text or "").splitlines()):
        if line.strip():
            return line.strip()
    return ""


# ── the app-server JSON-RPC conversation ──


def app_server_requests() -> list[dict]:
    """The line-delimited JSON-RPC conversation that asks for a rate-limit snapshot.

    `initialize` first because the server expects a handshake; the answer we want is the
    reply to the last request. Ids are fixed so a reply can be matched, but the reader is
    deliberately tolerant of a server that answers without echoing one.
    """
    return [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "maestro",
                    "title": "Maestro",
                    "version": _client_version(),
                }
            },
        },
        {"jsonrpc": "2.0", "method": "initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": APP_SERVER_METHOD, "params": {}},
    ]


def _client_version() -> str:
    try:
        from maestro import __version__

        return str(__version__)
    except Exception:  # pragma: no cover - a package without a version is still usable
        return "0"


def _rate_limits_in_response(obj: Any) -> Optional[Mapping]:
    """The rate-limit mapping inside a JSON-RPC reply, wherever the server put it."""
    for node in _walk(obj):
        for key in ("rateLimits", "rate_limits"):
            value = node.get(key)
            if isinstance(value, Mapping):
                return value
    return None


def _clear_nonblocking(fd: int) -> None:
    """Drop `O_NONBLOCK` from a descriptor before handing it to a child process."""
    import fcntl

    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)


# ── the driver ──


def _default_journal(event: str, detail: str) -> None:
    """Journal a warning if a journal is reachable; never fail because one is not."""
    try:
        from maestro.state import append_journal

        append_journal(event, detail)
    except Exception:
        pass


class CodexBackend:
    """`codex exec` behind the `AgentBackend` protocol.

    Construction is inert: no binary is resolved, no process is started and nothing is
    read from disk until a method that needs it is called. Every seam that could execute
    something — `runner`, `popen`, `probe` — is injectable, which is what lets the tests
    cover the whole driver without a token of quota being spent.
    """

    name = "codex"

    def __init__(
        self,
        *,
        binary: Optional[Path | str] = None,
        python: Optional[Path | str] = None,
        tmux_session: Optional[str] = None,
        codex_home: Optional[Path | str] = None,
        runner: Optional[Callable[..., Any]] = None,
        popen: Optional[Callable[..., Any]] = None,
        probe: Optional[Callable[[Path], str]] = None,
        journal: Optional[Callable[[str, str], None]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        add_dirs: Sequence[Path | str] = (),
    ) -> None:
        self._binary = str(binary) if binary else None
        self._python = str(python) if python else sys.executable
        self._tmux_session = tmux_session
        self._codex_home = Path(codex_home) if codex_home else None
        self._run = runner or subprocess.run
        self._popen = popen or subprocess.Popen
        self._probe = probe
        self._journal = journal or _default_journal
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._add_dirs = tuple(add_dirs)
        self._resolved: Optional[registry.BinaryInfo] = None
        self._spec: Optional[LaunchSpec] = None
        self._thread_id: Optional[str] = None
        #: Why the last telemetry read produced nothing. `doctor` and the journal want
        #: this: "the server answered nothing" and "this account has no limits" look
        #: identical from a `None` alone, and G4 says they must never be conflated.
        self.last_telemetry_error: Optional[str] = None

    # ── identity ──

    def capabilities(self) -> Capabilities:
        """What this driver supports. Pure: it resolves no binary and reads no file.

        `native_resume` is `True` on the strength of F1 — this is the answer to the M2
        open question. `system_prompt_file` is `False`: `codex exec` has no equivalent of
        a full-replace system prompt; a lean profile has to be folded into the brief.
        """
        return Capabilities(
            native_resume=True,
            system_prompt_file=False,
            usage_telemetry=True,
            sandbox=True,
        )

    # ── binary resolution (plan step 1) ──

    def binary_info(self, *, refresh: bool = False) -> Optional[registry.BinaryInfo]:
        """Resolve `codex` to one absolute path and pin its `--version`, once.

        Skipped entirely when an explicit binary was injected, so a test never probes.
        """
        if self._binary is not None:
            return None
        if self._resolved is None or refresh:
            self._resolved = registry.resolve_binary(
                BINARY, probe=self._probe, refresh=refresh
            )
        return self._resolved

    def binary_path(self) -> str:
        """The executable to invoke: the pinned absolute path, or the bare name.

        Falling back to the bare name keeps a launch working when `PATH` is only complete
        inside the tmux window; the accompanying warning is what tells an operator that
        the resolution was not pinned.
        """
        if self._binary is not None:
            return self._binary
        info = self.binary_info()
        return str(info.path) if info is not None else BINARY

    def version(self) -> str:
        info = self.binary_info()
        return info.version if info is not None else ""

    def binary_warning(self) -> Optional[str]:
        """The dual-install warning, when a second `codex` sits on `PATH` at another
        version. A warning, never a failure — the resolved binary still works, but a
        `PATH` change would swap versions underneath a running orchestrator."""
        info = self.binary_info()
        return info.warning if info is not None else None

    @property
    def tmux_session(self) -> str:
        if self._tmux_session is not None:
            return self._tmux_session
        from maestro.worktree import TMUX_SESSION

        return TMUX_SESSION

    def window_name(self, task_id: str) -> str:
        """The tmux window name, identical to the one the reference implementer uses."""
        return f"impl-{task_id}"

    def codex_home(self) -> Path:
        """Where Codex keeps its sessions. Honours `CODEX_HOME`, as the CLI does.

        A method rather than a module global on purpose: a `Path` global here would be
        resolved at import time and could not be redirected by a test.
        """
        if self._codex_home is not None:
            return self._codex_home
        override = os.environ.get("CODEX_HOME")
        return Path(override).expanduser() if override else Path.home() / ".codex"

    def sessions_dir(self) -> Path:
        return self.codex_home() / "sessions"

    # ── launching (plan step 2) ──

    def launch(self, spec: LaunchSpec) -> Handle:
        """Start a fresh briefed run in its own tmux window.

        The launch is indirect, exactly as the reference implementer's is: the argv goes
        into a generated `launch.py` which tmux runs in the worktree, with stderr merged
        into stdout in `impl.log`. That indirection is what lets the launcher watch the
        stream for the `thread.started` event and persist the resume handle (F1) —
        `launch` itself returns as soon as the window exists, so the thread id does not
        exist yet and the returned handle carries `None` for it.
        """
        workspace = Path(spec.workspace)
        worktree = Path(spec.worktree)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / BRIEF_FILE).write_text(spec.brief, encoding="utf-8")
        # A thread id left by an earlier run in this workspace is not ours. Clearing it
        # means "no handle yet" instead of a handle that resumes the wrong conversation.
        self._forget_thread_id(workspace)
        self._write_launch_meta(spec)

        argv = self.launch_argv(spec)
        self._write_launcher(workspace, primary=argv, fallback=None)
        self._new_window(spec.task_id, worktree, workspace)

        self._spec = spec
        self._thread_id = None
        return Handle(
            backend=self.name,
            session_id=spec.session_id,
            native_id=None,
            workspace=workspace,
            window=self.window_name(spec.task_id),
        )

    def _launch_dirs(self, spec: LaunchSpec) -> tuple[Path | str, ...]:
        """`self._add_dirs` plus the one directory every launch structurally needs.

        `spec.workspace` is where the sentinel contract (`DONE`/`FAILED`/`result.json`)
        lives, and it is a **sibling** of the worktree, not a subdirectory of it — `-C
        worktree` alone never covers it. Under `workspace-write` (`DEFAULT_SANDBOX`),
        `codex exec` also allows `$TMPDIR` implicitly, which is why this was never seen
        in the M2 research/acceptance runs: every scratch project they launched against
        lived under `/tmp`, so the workspace fell inside the implicit allowance by
        accident. The first real launch against a project rooted outside `/tmp` (M6's
        live-switching validation, against the migrated reference project) hit it
        directly: the implementer verified the inherited work was already correct, then
        could not write `DONE`/`result.json` at all — `error=patch rejected: writing
        outside of the project; rejected by user approval settings` — so the switch could
        never be observed as complete and the task fell back to a stale-window retry on
        the next poll. Always adding the workspace here, independent of whatever the
        caller configured, is what makes the sentinel contract project-location-agnostic
        rather than an accident of `/tmp`.
        """
        return (*self._add_dirs, spec.workspace)

    def launch_argv(self, spec: LaunchSpec) -> list[str]:
        return launch_argv(
            self.binary_path(),
            model=spec.model,
            worktree=spec.worktree,
            brief=spec.brief,
            sandbox=spec.sandbox,
            add_dirs=self._launch_dirs(spec),
        )

    def resume_argv(self, spec: LaunchSpec, thread_id: str, prompt: str) -> list[str]:
        return resume_argv(
            self.binary_path(),
            thread_id,
            prompt,
            model=spec.model,
            worktree=spec.worktree,
            sandbox=spec.sandbox,
            add_dirs=self._launch_dirs(spec),
        )

    # ── resuming (plan step 3) ──

    def resume(
        self, handle: Handle, prompt: str, *, spec: Optional[LaunchSpec] = None
    ) -> Handle:
        """Continue the thread behind `handle` with `prompt`.

        The launch parameters a resume needs — model, sandbox, worktree — are not on a
        `Handle`, so they come from the spec passed in, else the last launch, else the
        metadata this driver wrote beside the workspace. That last route is what lets a
        restarted orchestrator resume a session it did not itself start.

        When no thread id is known there is nothing to resume and `prompt` is launched as
        a fresh brief. When one *is* known it is tried first, and the generated launcher
        falls back to the same fresh brief if the CLI reports the thread is gone (G3) —
        that discovery costs no model call, which is why it is done by attempting rather
        than by probing.
        """
        resolved = self._resolve_spec(handle, prompt, spec)
        thread_id = handle.native_id or self.thread_id_for(handle.workspace)

        workspace = Path(resolved.workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / RESUME_PROMPT_FILE).write_text(prompt, encoding="utf-8")
        (workspace / BRIEF_FILE).write_text(resolved.brief, encoding="utf-8")
        self._write_launch_meta(resolved)

        fresh = self.launch_argv(resolved)
        if thread_id:
            primary, fallback = self.resume_argv(resolved, thread_id, prompt), fresh
        else:
            primary, fallback = fresh, None
            self._forget_thread_id(workspace)

        self._write_launcher(workspace, primary=primary, fallback=fallback, mode="a")
        self._new_window(resolved.task_id, Path(resolved.worktree), workspace)

        self._spec = resolved
        self._thread_id = thread_id
        return Handle(
            backend=self.name,
            session_id=resolved.session_id,
            native_id=thread_id,
            workspace=workspace,
            window=self.window_name(resolved.task_id),
        )

    def _resolve_spec(
        self, handle: Handle, prompt: str, spec: Optional[LaunchSpec]
    ) -> LaunchSpec:
        if spec is not None:
            return spec
        meta = self.read_launch_meta(handle.workspace)
        if meta is not None:
            return LaunchSpec(
                task_id=str(meta.get("task_id") or ""),
                session_id=handle.session_id or str(meta.get("session_id") or ""),
                model=str(meta.get("model") or ""),
                brief=prompt,
                workspace=Path(handle.workspace),
                worktree=Path(str(meta.get("worktree") or "")),
                sandbox=meta.get("sandbox"),
            )
        if self._spec is not None:
            remembered = self._spec
            return LaunchSpec(
                task_id=remembered.task_id,
                session_id=handle.session_id or remembered.session_id,
                model=remembered.model,
                brief=prompt,
                workspace=Path(handle.workspace),
                worktree=remembered.worktree,
                system_prompt_file=remembered.system_prompt_file,
                sandbox=remembered.sandbox,
            )
        raise ValueError(
            "cannot resume: no launch spec was given and none was recorded in "
            f"{Path(handle.workspace) / LAUNCH_META_FILE}"
        )

    # ── workspace bookkeeping ──

    def thread_id_for(self, workspace: Path | str) -> Optional[str]:
        """The resume handle recorded for a workspace, if any (F1).

        The launcher writes it the moment the CLI announces it. `impl.log` is consulted
        as a backstop for the window between the event arriving and the file landing, and
        for a run whose launcher was killed before it could write.
        """
        workspace = Path(workspace)
        try:
            recorded = (workspace / THREAD_ID_FILE).read_text(encoding="utf-8").strip()
        except OSError:
            recorded = ""
        if recorded:
            return recorded
        try:
            log = (workspace / LOG_FILE).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        return thread_id_from_text(log)

    def _forget_thread_id(self, workspace: Path) -> None:
        try:
            (workspace / THREAD_ID_FILE).unlink()
        except OSError:
            pass

    def _write_launch_meta(self, spec: LaunchSpec) -> None:
        """Record what a later resume needs and this driver's own instance cannot keep."""
        meta = {
            "backend": self.name,
            "task_id": spec.task_id,
            "session_id": spec.session_id,
            "model": spec.model,
            "sandbox": sandbox_mode(spec.sandbox),
            "worktree": str(spec.worktree),
            "workspace": str(spec.workspace),
        }
        (Path(spec.workspace) / LAUNCH_META_FILE).write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )

    def read_launch_meta(self, workspace: Path | str) -> Optional[dict]:
        try:
            raw = (Path(workspace) / LAUNCH_META_FILE).read_text(encoding="utf-8")
            meta = json.loads(raw)
        except Exception:
            return None
        return meta if isinstance(meta, dict) else None

    def _write_launcher(
        self,
        workspace: Path,
        *,
        primary: Sequence[str],
        fallback: Optional[Sequence[str]],
        mode: str = "w",
    ) -> Path:
        script = workspace / LAUNCH_SCRIPT
        script.write_text(
            render_launcher(
                primary=primary,
                fallback=fallback,
                log=workspace / LOG_FILE,
                thread_file=workspace / THREAD_ID_FILE,
                mode=mode,
            ),
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script

    def _new_window(self, task_id: str, worktree: Path, workspace: Path) -> str:
        window = self.window_name(task_id)
        script = workspace / LAUNCH_SCRIPT
        command = (
            f"tmux new-window -t {self.tmux_session} -n {window} "
            f"'cd {worktree} && {self._python} {script}'"
        )
        self._run(command, shell=True, check=True)
        warning = self.binary_warning()
        if warning:
            self._journal("codex_binary_warning", f"{task_id} {warning}")
        return window

    # ── exit classification (plan step 6) ──

    def parse_exit(self, rc: int, log_tail: str) -> ExitVerdict:
        """Classify a finished run from its return code and merged output.

        Codex's exhaustion evidence is its own (see `exhaustion_signal`); the other CLI's
        `quota._LIMIT_RE` is not reused, because its wording does not appear here. A
        window sitting at 100 % only makes the run a `quota_exhausted` one if the run
        *also* failed — a turn that completed on the last available percent did its work,
        and reporting it as exhausted would throw away a finished task.

        `reset_at` prefers the epoch Codex itself reported; when there is none it falls
        back to a conservative hour, so a caller pausing on this verdict never spins.
        """
        signal = exhaustion_signal(log_tail)
        if signal is not None and (signal["explicit"] or rc != 0):
            return ExitVerdict(
                kind="quota_exhausted",
                reset_at=self._reset_iso(signal.get("resets_at")),
                evidence=signal.get("evidence", "")[:200],
            )
        if rc == 0:
            return ExitVerdict(kind="ok")
        if is_dead_thread(rc, log_tail):
            return ExitVerdict(
                kind="crashed",
                evidence=f"{DEAD_THREAD_MARKER} — the resume handle is gone",
            )
        return ExitVerdict(kind="crashed", evidence=_last_nonempty_line(log_tail)[:200])

    def _reset_iso(self, epoch: Optional[int]) -> str:
        now = self._clock()
        if epoch:
            moment = datetime.fromtimestamp(float(epoch), tz=timezone.utc)
            if moment > now:
                return _zulu(moment)
        return _zulu(now + timedelta(hours=1))

    # ── usage telemetry (plan step 5) ──

    def usage(self, handle: Optional[Handle] = None) -> Optional[Usage]:
        """The latest usage sample, or `None` when none could be taken.

        The rollout of the run in hand is the primary source because it costs nothing and
        is always current; `app-server` is the fallback for when no run is active. `None`
        means *no sample*, which is not the same as a sample showing no limits — that one
        is a `Usage` with an empty `windows` map.
        """
        self.last_telemetry_error = None
        thread_id = handle.native_id if handle is not None else None
        if not thread_id and handle is not None:
            thread_id = self.thread_id_for(handle.workspace)
        thread_id = thread_id or self._thread_id
        if thread_id:
            sample = self.usage_from_rollout(thread_id)
            if sample is not None:
                return sample
        return self.usage_from_app_server()

    def rollout_path(self, thread_id: str) -> Optional[Path]:
        """The session file for a thread, newest first. The uuid in the name *is* the
        thread id, and a resumed thread appends to the same file (F1)."""
        if not thread_id:
            return None
        try:
            matches = sorted(
                self.sessions_dir().glob(f"*/*/*/rollout-*-{thread_id}.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return None
        return matches[0] if matches else None

    def usage_from_rollout(self, thread_id: str) -> Optional[Usage]:
        """The last `token_count` snapshot in a thread's rollout, or `None`.

        The last event *carrying rate limits* wins, not simply the last event: a turn can
        report token counts without a fresh limit snapshot, and falling back to the newest
        event with limits keeps a real reading rather than dropping to no reading at all.
        """
        path = self.rollout_path(thread_id)
        if path is None:
            self.last_telemetry_error = f"no rollout file for thread {thread_id}"
            return None
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            self.last_telemetry_error = f"cannot read {path}: {exc}"
            return None

        newest: Optional[Mapping] = None
        with_limits: Optional[Mapping] = None
        model: Optional[str] = None
        for event in _iter_json_lines(text):
            payload = _token_count_payload(event)
            if payload is not None:
                newest = payload
                if _rate_limits_of(payload) is not None:
                    with_limits = payload
            found = _model_of(event)
            if found:
                model = found

        payload = with_limits or newest
        if payload is None:
            self.last_telemetry_error = f"no token_count event in {path}"
            return None

        now = self._clock()
        info = payload.get("info") if isinstance(payload.get("info"), Mapping) else {}
        return Usage(
            context_used_pct=context_used_pct(info),
            windows=windows_from_rate_limits(_rate_limits_of(payload) or {}, now=now),
            model=model,
            updated_at=_zulu(now),
            context_total_input_tokens=_as_int(
                _pick(_pick(info, "last_token_usage", "lastTokenUsage") or {},
                      "input_tokens", "inputTokens")
            )
            or 0,
        )

    def usage_from_app_server(
        self, *, timeout_sec: float = APP_SERVER_TIMEOUT_SEC
    ) -> Optional[Usage]:
        """Ask a short-lived `codex app-server` for the rate-limit snapshot (source B).

        No model tokens are spent. Returns `None` for a failed sample — including the
        silent one G4 describes, where the server exits 0 having written nothing.
        `context_used_pct` is absent from this source by construction: it is a property of
        a conversation, and there is no conversation here (G6).
        """
        limits = self._read_rate_limits_via_app_server(timeout_sec=timeout_sec)
        if limits is None:
            return None
        now = self._clock()
        return Usage(
            context_used_pct=None,
            windows=windows_from_rate_limits(limits, now=now),
            model=None,
            updated_at=_zulu(now),
        )

    def _read_rate_limits_via_app_server(
        self, *, timeout_sec: float = APP_SERVER_TIMEOUT_SEC
    ) -> Optional[Mapping]:
        """The G4 dance: a FIFO holds stdin open until the answer has arrived.

        `codex app-server` tears its stdout writer down on stdin EOF *before* flushing,
        so a request written to a pipe that is then closed comes back as exit 0 with zero
        bytes — indistinguishable, from the outside, from "this build has no such
        method". Keeping a writer open until the reply is read is the whole trick; the
        FIFO is closed in the `finally` so the server always gets its EOF and exits.
        """
        directory = tempfile.mkdtemp(prefix="maestro-codex-")
        fifo = os.path.join(directory, "app-server-stdin")
        reader_fd: Optional[int] = None
        writer_fd: Optional[int] = None
        proc = None
        try:
            os.mkfifo(fifo, 0o600)
            # Open the read end non-blocking first: opening a FIFO for reading otherwise
            # blocks until a writer appears, and the writer is us.
            reader_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
            _clear_nonblocking(reader_fd)
            writer_fd = os.open(fifo, os.O_WRONLY)
            proc = self._popen(
                [self.binary_path(), "app-server"],
                stdin=reader_fd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            os.close(reader_fd)  # the child holds its own copy
            reader_fd = None
            for request in app_server_requests():
                os.write(writer_fd, (json.dumps(request) + "\n").encode("utf-8"))
            return self._await_rate_limits(proc, timeout_sec)
        except Exception as exc:
            self.last_telemetry_error = f"codex app-server sample failed: {exc}"
            return None
        finally:
            for fd in (reader_fd, writer_fd):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            self._reap(proc)
            shutil.rmtree(directory, ignore_errors=True)

    def _await_rate_limits(self, proc, timeout_sec: float) -> Optional[Mapping]:
        """Read replies until one carries rate limits, the server exits, or time runs out."""
        stream = proc.stdout
        deadline = time.monotonic() + timeout_sec
        buffered = b""
        total = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.last_telemetry_error = (
                    f"codex app-server did not answer {APP_SERVER_METHOD} within "
                    f"{timeout_sec:g}s"
                )
                return None
            ready, _, _ = select.select([stream], [], [], min(remaining, 0.25))
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            chunk = os.read(stream.fileno(), 65536)
            if not chunk:
                break
            total += len(chunk)
            buffered += chunk
            while b"\n" in buffered:
                line, buffered = buffered.split(b"\n", 1)
                limits = self._rate_limits_of_reply(line)
                if limits is not None:
                    return limits
        limits = self._rate_limits_of_reply(buffered)
        if limits is not None:
            return limits

        rc = proc.poll()
        if rc is None:
            rc = self._reap(proc)
        if total == 0:
            # G4 exactly: a silent no-answer. Never report this as "no limits".
            self.last_telemetry_error = (
                f"codex app-server exited {rc} having written nothing — a silent "
                "no-answer, not an account without limits"
            )
        else:
            self.last_telemetry_error = (
                f"codex app-server exited {rc} without answering {APP_SERVER_METHOD}"
            )
        return None

    @staticmethod
    def _rate_limits_of_reply(raw: bytes) -> Optional[Mapping]:
        text = raw.decode("utf-8", errors="replace").strip()
        if not text.startswith("{"):
            return None
        try:
            reply = json.loads(text)
        except Exception:
            return None
        return _rate_limits_in_response(reply)

    def _reap(self, proc) -> Optional[int]:
        """Make sure no app-server outlives the sample that started it."""
        if proc is None:
            return None
        try:
            if proc.poll() is None:
                proc.terminate()
            return proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            return getattr(proc, "returncode", None)


def _model_of(event: Mapping) -> Optional[str]:
    """A model name recorded anywhere in a rollout record, for labelling a sample."""
    for node in _walk(event):
        value = node.get("model")
        if isinstance(value, str) and value:
            return value
    return None


# ── the generated launcher ──

_LAUNCHER_TEMPLATE = '''\
"""Generated by maestro's codex backend — regenerate by relaunching, do not edit.

Runs the invocation below with stderr merged into stdout in one log, exactly as the
reference implementer does, and watches the JSONL stream for the `thread.started` event
so the resume handle lands on disk the moment the CLI announces it.

When a resume was attempted and the CLI reports that the thread is gone, this falls back
to the fresh briefed launch recorded below. Discovering a dead handle that way is free:
the failing resume exits in milliseconds without a model call.
"""
import json
import subprocess
import sys
from pathlib import Path

ARGV = json.loads({argv})
FALLBACK_ARGV = json.loads({fallback})
LOG = Path({log})
THREAD_FILE = Path({thread_file})
DEAD_THREAD_MARKER = {marker}
TAIL_CHARS = {tail_chars}
LOG_MODE = {mode}


def thread_id_of(line):
    line = line.strip()
    if not line.startswith("{{") or "thread" not in line:
        return None
    try:
        event = json.loads(line)
    except Exception:
        return None
    if not isinstance(event, dict) or event.get("type") != "thread.started":
        return None
    value = event.get("thread_id")
    return str(value) if value else None


def run(argv, log):
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    tail = ""
    captured = False
    for line in proc.stdout:
        log.write(line)
        log.flush()
        tail = (tail + line)[-TAIL_CHARS:]
        if not captured:
            thread_id = thread_id_of(line)
            if thread_id:
                THREAD_FILE.write_text(thread_id, encoding="utf-8")
                captured = True
    proc.stdout.close()
    return proc.wait(), tail


with open(LOG, LOG_MODE, encoding="utf-8") as log:
    rc, tail = run(ARGV, log)
    if rc != 0 and FALLBACK_ARGV and DEAD_THREAD_MARKER in tail:
        log.write("\\n[maestro] resume handle is gone — relaunching with a fresh brief\\n")
        log.flush()
        rc, tail = run(FALLBACK_ARGV, log)

sys.exit(rc)
'''


def render_launcher(
    *,
    primary: Sequence[str],
    fallback: Optional[Sequence[str]],
    log: Path | str,
    thread_file: Path | str,
    mode: str = "w",
) -> str:
    """The `launch.py` a tmux window runs.

    The argv is embedded as JSON rather than interpolated as Python source: a brief is
    arbitrary operator-authored text, full of quotes, backslashes and newlines, and
    `json.dumps` + `repr` is the one escaping path that cannot be broken by its content.

    `mode` is the mode `log` is opened in. A launch truncates (`"w"`, the reference
    behaviour); a resume appends (`"a"`), because truncating `impl.log` would destroy the
    evidence the reactive quota net reads back out of it.
    """
    return _LAUNCHER_TEMPLATE.format(
        argv=repr(json.dumps(list(primary))),
        fallback=repr(json.dumps(list(fallback)) if fallback else "null"),
        log=repr(str(log)),
        thread_file=repr(str(thread_file)),
        marker=repr(DEAD_THREAD_MARKER),
        tail_chars=LAUNCHER_TAIL_CHARS,
        mode=repr(mode),
    )
