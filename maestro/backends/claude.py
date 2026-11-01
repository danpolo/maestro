"""The Claude driver: `claude -p`, at parity with the reference launch.

A driver owns exactly three things — **build the launch invocation**, **interpret the
exit**, **report usage**. Everything else (worktree lifecycle, brief *construction*,
sentinel protocol, state and journal) stays where M1 put it. Concretely this module
owns: produce the argv → produce the generated `launch.py` → name the tmux window.

Design authority is `docs/DESIGN.md` §6 as corrected by the M2 research pass recorded in
`docs/plans/2026-08-12-m2-backends.md` (finding **F4**). Two corrections are load-bearing:

* **The argv is exactly** ``claude -p --model <id> --session-id <uuid4>
  [--system-prompt-file <abs>] <brief>`` — brief **positional and last**, and *no other
  flags*. `docs/DESIGN.md` §6 as originally written listed `--strict-mcp-config`,
  `--mcp-config`, `--add-dir` and `--dangerously-skip-permissions`; **none of those four
  appears anywhere in the reference implementation.** `--system-prompt-file` is emitted
  only when that file actually exists, and `--resume <uuid>` *replaces* `--session-id` —
  the two never appear together.
* **The launch is indirect.** The reference does not exec the agent; it writes three
  files into the workspace — `brief.txt`, `session_uuid.txt` and a generated `launch.py`
  (mode 0o755) that holds the argv — and then runs that launcher inside a fresh tmux
  window. The launcher, not the caller, opens `<workspace>/impl.log` and **merges stderr
  into stdout** (`stderr=subprocess.STDOUT`), which is why every captured exhaustion
  sample is stream-agnostic and why `impl.log` is the reactive net `parse_exit` (below)
  reads after the fact — over `quota._LIMIT_RE` and `quota._resolve_limit_reset`
  directly, **not** via `maestro.quota._scan_impl_log_for_limit`. That helper looks like
  the obvious reader of this net but isn't one: production exit classification goes
  through `driver.parse_exit`, resolved per-entry and called from
  `orchestrator._reconcile_exit_verdict` (A1, readiness queue), and `ClaudeBackend`'s
  `parse_exit` was already written against the lower-level regex/reset helpers rather
  than through that scanner. `_scan_impl_log_for_limit` is kept — nothing here deletes
  it — but as of A1 it is reachable only from `tests/characterization/test_quota.py`.

`launcher_source` reproduces that generated file **byte for byte** for the launch case —
it is parsed by the existing characterisation suite, and the reactive quota net depends
on the log redirection it contains.

**Honesty note about `parse_exit`.** The reference has *no exit-code mapping for
implementers at all*: `launch_implementer` discards the return code, and an
implementer's outcome is decided by tmux-window liveness plus the `PAUSED` / `DONE` /
`FAILED` sentinel files, checked in that order, in `maestro.orchestrator`. So only the
`quota_exhausted` branch here is extracted behaviour — it delegates to
`maestro.quota._LIMIT_RE` and `maestro.quota._resolve_limit_reset` over the same 25-line
tail, neither of which is re-derived here. The `ok` / `crashed` split is **new M2
surface**: `rc == 0 → ok`, non-zero without a limit match → `crashed`. It does not
replace the sentinel/liveness protocol, which stays the authority in `orchestrator.py`.

Nothing in this module executes an agent CLI. The only subprocess it starts is `tmux`,
and every test monkeypatches it.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from maestro import quota
from maestro.backends.base import (
    Capabilities,
    Completion,
    CompletionSpec,
    ExitVerdict,
    Handle,
    LaunchSpec,
    Usage,
    from_usage_json,
)
from maestro.paths import Paths
from maestro.worktree import TMUX_SESSION, worktree_path_for

__all__ = [
    "REPO",
    "USAGE_JSON",
    "VENV_PYTHON",
    "BINARY",
    "TAIL_LINES",
    "DEFAULT_MODE",
    "BRIEF_FILE",
    "SESSION_UUID_FILE",
    "LAUNCHER_FILE",
    "LOG_FILE",
    "RESUME_PROMPT_FILE",
    "RESUME_LAUNCHER_FILE",
    "HOOKS_FILE",
    "SETTINGS_FILE",
    "MARKER_FILE",
    "window_name",
    "window_target",
    "task_id_from_window",
    "session_uuid_in",
    "launch_argv",
    "resume_argv",
    "launcher_source",
    "interactive_launch_argv",
    "interactive_resume_argv",
    "interactive_launcher_source",
    "interactive_completion_argv",
    "ensure_workspace_trusted",
    "setup_interactive_hooks",
    "tmux_command",
    "ClaudeBackend",
]

_PATHS = Paths.from_env()

#: Path-valued module globals, so the characterisation sandbox can rebase them by
#: reflection. `REPO` is what makes this module sandboxable at all — a module with
#: `Path` globals and no `Path`-valued `REPO` is silently left pointing at the live repo.
REPO = _PATHS.repo
USAGE_JSON = _PATHS.usage
VENV_PYTHON = REPO / ".venv" / "bin" / "python3"

#: The executable. Resolution to an absolute path and version pinning belong to
#: `maestro.backends.registry`, which is the one module allowed to know backends by name.
BINARY = "claude"

#: Default execution mode for Claude driver. Can be overridden via MAESTRO_CLAUDE_MODE.
DEFAULT_MODE = "interactive"

#: The tmux window name the reference gives an implementer, and the role prefix it uses.
WINDOW_PREFIX = "impl-"

#: How many trailing log lines a verdict is drawn from. Same window `maestro.quota`
#: scans (`_tail_text(..., 25)`), so the two agree on what "the tail" means.
TAIL_LINES = 25

#: How much of a matching line is kept as journal evidence, matching `maestro.quota`.
EVIDENCE_CHARS = 200

BRIEF_FILE = "brief.txt"
SESSION_UUID_FILE = "session_uuid.txt"
LAUNCHER_FILE = "launch.py"
LOG_FILE = "impl.log"
RESUME_PROMPT_FILE = "resume_prompt.txt"
RESUME_LAUNCHER_FILE = "resume.py"
HOOKS_FILE = "claude_hooks.py"
SETTINGS_FILE = "claude_settings.json"
MARKER_FILE = "turn_marker.json"

#: Mode the generated launcher is chmod'd to, as in the reference.
LAUNCHER_MODE = 0o755

#: Flags whose value the generated launcher spells as a raw string literal. These are
#: the ones carrying a filesystem path or a uuid; a raw literal is what keeps a Windows
#: -style separator or a stray backslash from being re-interpreted as an escape.
RAW_VALUE_FLAGS = frozenset({"--session-id", "--resume", "--system-prompt-file", "--settings"})


# ── window naming ──


def window_name(task_id: str) -> str:
    """The tmux window *name*, exactly as the reference names it: `impl-<task_id>`.

    This is the value passed to `tmux new-window -n`, and the value stored on the
    orchestrator's `in_flight` entry — `tmux_window_exists` compares it against bare
    `#{window_name}` output, so it must stay unqualified.
    """
    return f"{WINDOW_PREFIX}{task_id}"


def window_target(task_id: str, session: str = "") -> str:
    """The fully qualified tmux target, `agents:impl-<task_id>`.

    This is what a `Handle.window` carries: a handle is passed between modules and a
    bare window name is ambiguous without its session. Use `window_name` for `-n` and
    for `in_flight`; use this for `-t` and for display.
    """
    return f"{session or TMUX_SESSION}:{window_name(task_id)}"


def task_id_from_window(window: str) -> str:
    """The task id inside a window name or a qualified target. Inverse of the above."""
    name = str(window or "").rsplit(":", 1)[-1]
    return name[len(WINDOW_PREFIX):] if name.startswith(WINDOW_PREFIX) else name


def session_uuid_in(workspace: Path) -> str:
    """The resume uuid a previous launch left in `session_uuid.txt`, or `""`.

    Mirrors `maestro.parking._session_uuid_for`: a workspace from before the sidecar
    existed, or one whose launch never got that far, simply has no resumable session.
    """
    try:
        return (Path(workspace) / SESSION_UUID_FILE).read_text(encoding="utf-8").strip()
    except Exception:
        return ""


# ── the session transcript: how full is one conversation? ──


def context_reading(path: Path) -> Optional[tuple[int, str]]:
    """`(live context in tokens, the model that reported it)` from a transcript, or `None`.

    **The arithmetic, which is the whole judgement call in this function.** Claude Code's
    own status line reads `.context_window.total_input_tokens` out of the JSON it hands a
    `statusLine` hook — a document a driver never sees. The equivalent recovered from the
    session transcript is one turn's *input side*::

        input_tokens + cache_creation_input_tokens + cache_read_input_tokens

    of the **last** assistant turn. Three properties, each load-bearing:

    * **the input side only.** What the model wrote (`output_tokens`) is billed, but it is
      not what the next turn has to carry, and the figure this reproduces excludes it too.
    * **all three input fields.** A cached prefix is still in the context — it is cheaper,
      not absent. Counting `input_tokens` alone would report single digits for a session
      sitting at its ceiling, and the rotation would never fire.
    * **the last turn, never a sum.** Summing across turns measures cumulative spend,
      which only ever grows; a context is a size that can also come down.
      `switch.context_crossed` compares this against the model's `prepare_handoff_high`,
      which is a threshold on the size of the live context, so a cumulative figure would
      rotate a healthy session after a handful of ordinary turns.

    Two kinds of record are skipped rather than taken as the newest turn. A **sidechain**
    record is a subagent's conversation stored in its parent's file — a different, and
    routinely much smaller, context. A turn reporting **no input tokens at all** is a
    synthetic or interrupted one; taking it would publish a `0`, and a `0` here reads as
    "loads of room" rather than as the "not measurable" it actually is (G6). A file with
    no usable turn answers `None` for the same reason, and so does an unreadable one.

    Parsed line by line, tolerating garbage: the agent is appending to this file while it
    is being read, so a half-written final line is normal and must cost that line only.
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    tokens = 0
    model = ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, Mapping):
            continue
        if record.get("type") != "assistant" or record.get("isSidechain"):
            continue
        message = record.get("message")
        if not isinstance(message, Mapping):
            continue
        usage = message.get("usage")
        if not isinstance(usage, Mapping):
            continue
        total = sum(
            _as_token_count(usage.get(field))
            for field in ("input_tokens",
                          "cache_creation_input_tokens",
                          "cache_read_input_tokens")
        )
        if total <= 0:
            continue
        tokens = total
        model = str(message.get("model") or "")
    return (tokens, model) if tokens > 0 else None


_COST_STATE_KEYS = {
    "fresh_input": "inputTokens",
    "cache_read": "cacheReadInputTokens",
    "cache_write": "cacheCreationInputTokens",
    "output": "outputTokens",
}
_MESSAGE_KEYS = {
    "fresh_input": "input_tokens",
    "cache_read": "cache_read_input_tokens",
    "cache_write": "cache_creation_input_tokens",
    "output": "output_tokens",
}


def _json_records(path: Path):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, Mapping):
            yield record


def session_token_totals(path: Path) -> Optional[dict]:
    """A finished session's cumulative token totals (see `attempt_token_usage`), or `None`."""
    cost_state = None
    by_message: dict[str, tuple[Mapping, str]] = {}
    files = [Path(path)]
    try:
        files += sorted((Path(path).with_suffix("") / "subagents").glob("*.jsonl"))
    except OSError:
        pass
    for index, file in enumerate(files):
        for record in _json_records(file):
            if index == 0 and record.get("type") == "cost-state":
                cost_state = record
            message = record.get("message")
            if record.get("type") != "assistant" or not isinstance(message, Mapping):
                continue
            usage = message.get("usage")
            if isinstance(usage, Mapping):
                key = f"{index}:{message.get('id') or len(by_message)}"
                by_message[key] = (usage, str(message.get("model") or ""))
    per_model = cost_state.get("modelUsage") if cost_state else None
    if isinstance(per_model, Mapping) and per_model:
        totals = {
            ours: sum(_as_token_count((row or {}).get(theirs)) for row in per_model.values()
                      if isinstance(row, Mapping))
            for ours, theirs in _COST_STATE_KEYS.items()
        }
        cost = cost_state.get("totalCostUSD")
        return {**totals,
                "cost_usd": float(cost) if isinstance(cost, (int, float)) else None,
                "models": sorted(str(name) for name in per_model),
                "source": "claude_cost_state"}
    if not by_message:
        return None
    totals = {
        ours: sum(_as_token_count(usage.get(theirs)) for usage, _ in by_message.values())
        for ours, theirs in _MESSAGE_KEYS.items()
    }
    models = sorted({model for _, model in by_message.values() if model})
    return {**totals, "cost_usd": None, "models": models, "source": "claude_transcript"}


def _as_token_count(value: Any) -> int:
    """One field of the sum above. Anything that is not a count contributes nothing — it
    is a missing part of a total, not a reason to lose the whole reading."""
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _zulu(moment: datetime) -> str:
    """The fixed-width UTC stamp every timestamp in maestro's state documents uses, so a
    plain string comparison against one of them is chronological."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── argv ──


def _system_prompt_arg(path: Optional[Path]) -> list[str]:
    """`--system-prompt-file <abs>`, but **only when the file exists** (F4)."""
    if path is None:
        return []
    candidate = Path(path)
    try:
        if not candidate.exists():
            return []
    except OSError:
        return []
    return ["--system-prompt-file", str(candidate)]


def launch_argv(spec: LaunchSpec, session_uuid: str, *, permission_args: Sequence[str] = ()) -> list[str]:
    """The F4 argv for a fresh session. The brief is positional and last."""
    argv = [BINARY, "-p", *permission_args, "--model", spec.model, "--session-id", str(session_uuid)]
    argv += _system_prompt_arg(spec.system_prompt_file)
    argv += [str(arg) for arg in spec.effort_args]
    argv.append(spec.brief)
    return argv


def resume_argv(native_id: str, prompt: str, *, model: str = "", permission_args: Sequence[str] = ()) -> list[str]:
    """The argv for continuing an existing session.

    `--resume <uuid>` **replaces** `--session-id`; emitting both is the one combination
    F4 rules out. `--model` is carried through when the caller knows it, so a resumed
    run does not silently change model, and omitted otherwise rather than guessed —
    this module does not own the model allowlist (`maestro.implementer` and, from M2 on,
    `maestro.roles` do).

    `--system-prompt-file` is deliberately *not* emitted on resume. F4 pins it only for
    the launch invocation, the session being resumed already ran under it, and adding an
    unverified flag to a resume is exactly the kind of guess this plan's research pass
    exists to prevent.
    """
    argv = [BINARY, "-p", *permission_args]
    if model:
        argv += ["--model", str(model)]
    argv += ["--resume", str(native_id), prompt]
    return argv


def _write_log(path: Optional[Path], text: str) -> None:
    """Best-effort tee of a completion's output. A log that cannot be written is never
    a reason to fail the call it was only recording."""
    if path is None:
        return
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text, encoding="utf-8")
    except Exception:
        pass


def completion_argv(spec: CompletionSpec, *, permission_args: Sequence[str] = ()) -> list[str]:
    """The argv for one bounded call. The prompt is positional and last.

    Same shape the six ex-hardcoded call sites were already using by hand — `claude -p
    [--model M] [--resume ID] <prompt>` — so routing them through the driver changes which
    binary can be chosen, not what this one is asked to do.

    `--resume` and `--session-id` are still mutually exclusive (F4): a completion names a
    session it is *creating* only when the caller asks (`spec.session_id`, the graph
    worker's per-attempt token count), and never alongside `--resume`. The Claude CLI has no
    sandbox-mode flag (`capabilities().sandbox is False`).

    `permission_args` (P12 pilot finding D30, `maestro.permissions.cli_args`) go straight
    after `-p`: `--permission-mode dontAsk` plus the allow and never-allow lists, so the
    call never falls back to the auto-mode classifier a user's settings may default to.
    `complete` always passes them; the bare shape stays for the callers that print it.
    `spec.writable` is spelled there, as Edit/Write rules scoped to `cwd` and `add_dirs`.
    """
    argv = [BINARY, "-p", *permission_args]
    if spec.model:
        argv += ["--model", str(spec.model)]
    if spec.resume_id:
        argv += ["--resume", str(spec.resume_id)]
    elif spec.session_id:
        # A caller that wants to count this call's tokens names the fresh session, so its
        # transcript can be found by uuid afterwards (`attempt_token_usage`).
        argv += ["--session-id", str(spec.session_id)]
    argv += [str(arg) for arg in spec.effort_args]
    argv.append(spec.prompt)
    return argv


# ── the generated launcher ──


def _render_element(element: str, raw: bool) -> str:
    if raw and "'" not in element and not element.endswith("\\"):
        return f"r'{element}'"
    return repr(element)


def _render_flags(argv: Sequence[str]) -> str:
    """The flag block of the generated `subprocess.run([...])`, two elements per line.

    Every element here is a flag or a flag's value, so pairing is exact; an odd trailing
    element is rendered alone rather than dropped.
    """
    lines = []
    for index in range(0, len(argv), 2):
        pair = argv[index:index + 2]
        rendered = [
            _render_element(element, raw=(offset == 1 and pair[0] in RAW_VALUE_FLAGS))
            for offset, element in enumerate(pair)
        ]
        lines.append("    " + ", ".join(rendered) + ",\n")
    return "".join(lines)


def launcher_source(
    argv: Sequence[str],
    *,
    prompt_file: Path,
    log_file: Path,
    mode: str = "w",
) -> str:
    """The generated `launch.py`, byte-identical to the reference for a launch argv.

    The last element of `argv` — the brief or the resume prompt — is **not** inlined: the
    launcher reads it from `prompt_file` at run time and passes it as the variable
    `brief`. That keeps a multi-kilobyte brief out of the generated source (and out of
    any quoting question), and it is what the existing characterisation suite parses,
    which reports that element as `<brief>`.

    `mode` is the mode `log_file` is opened in. A launch truncates (`"w"`, the reference
    behaviour); a resume appends (`"a"`), because truncating `impl.log` would destroy the
    evidence the reactive quota net reads back out of it.
    """
    return (
        "import subprocess\nfrom pathlib import Path\n"
        f"brief = Path(r'{prompt_file}').read_text()\n"
        f"log = open(r'{log_file}', '{mode}', encoding='utf-8')\n"
        "subprocess.run([\n"
        f"{_render_flags(list(argv)[:-1])}"
        "    brief,\n], stdout=log, stderr=subprocess.STDOUT)\n"
        "log.flush()\nlog.close()\n"
    )


# ── interactive helpers & launcher ──


def ensure_workspace_trusted(
    workspace: Path | str,
    claude_json: Optional[Path | str] = None,
) -> bool:
    """Ensure `workspace` has `hasTrustDialogAccepted: True` in `~/.claude.json`.

    Claude Code strictly validates workspace trust against `~/.claude.json`'s
    `projects` table; a local `.claude.json` in the workspace is ignored. An ancestor
    directory having trust accepted is inherited by any descendant directory.
    Uses an exclusive file lock (`.claude.json.lock`) to avoid races with active CLI sessions.
    Never raises.
    """
    try:
        path = Path(workspace).resolve()
        target_json = Path(claude_json).resolve() if claude_json else Path.home() / ".claude.json"

        # Fast path without locking: check if path or an ancestor is already trusted
        if target_json.exists():
            try:
                data = json.loads(target_json.read_text(encoding="utf-8"))
                projects = data.get("projects", {}) if isinstance(data, dict) else {}
                curr = path
                while True:
                    if projects.get(str(curr), {}).get("hasTrustDialogAccepted") is True:
                        return True
                    parent = curr.parent
                    if parent == curr:
                        break
                    curr = parent
            except Exception:
                pass

        # Slow path: acquire lock and update ~/.claude.json
        import fcntl

        lock_path = target_json.parent / f"{target_json.name}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                if target_json.exists():
                    try:
                        data = json.loads(target_json.read_text(encoding="utf-8"))
                    except Exception:
                        data = {}
                else:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                projects = data.setdefault("projects", {})
                curr = path
                already_trusted = False
                while True:
                    if projects.get(str(curr), {}).get("hasTrustDialogAccepted") is True:
                        already_trusted = True
                        break
                    parent = curr.parent
                    if parent == curr:
                        break
                    curr = parent
                if not already_trusted:
                    projects.setdefault(str(path), {})["hasTrustDialogAccepted"] = True
                    tmp = target_json.with_suffix(".tmp")
                    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
                    tmp.replace(target_json)
                return True
            finally:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
    except Exception:
        return False


def setup_interactive_hooks(workspace: Path) -> tuple[Path, Path, Path]:
    """Create hook script, settings file, and return (hook_path, settings_path, marker_path)."""
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    marker_file = workspace / MARKER_FILE
    hook_file = workspace / HOOKS_FILE
    settings_file = workspace / SETTINGS_FILE

    hook_source = (
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "from pathlib import Path\n"
        "try:\n"
        "    data = json.loads(sys.stdin.read() or '{}')\n"
        "except Exception as exc:\n"
        "    data = {'error': str(exc)}\n"
        f"Path(r'{marker_file}').write_text(json.dumps(data), encoding='utf-8')\n"
    )
    hook_file.write_text(hook_source, encoding="utf-8")
    hook_file.chmod(LAUNCHER_MODE)

    settings_content = {
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": str(hook_file)}]}],
            "StopFailure": [{"hooks": [{"type": "command", "command": str(hook_file)}]}],
        }
    }
    settings_file.write_text(json.dumps(settings_content, indent=2), encoding="utf-8")
    return hook_file, settings_file, marker_file


def interactive_launch_argv(spec: LaunchSpec, session_uuid: str, settings_file: Path,
                            *, permission_args: Sequence[str] = ()) -> list[str]:
    """The interactive argv for a fresh session. The brief is positional and last."""
    argv = [
        BINARY,
        "--model", spec.model,
        "--session-id", str(session_uuid),
        "--settings", str(settings_file),
        *(permission_args or ("--permission-mode", "dontAsk")),
    ]
    argv += _system_prompt_arg(spec.system_prompt_file)
    argv += [str(arg) for arg in spec.effort_args]
    argv.append(spec.brief)
    return argv


def interactive_resume_argv(
    native_id: str,
    prompt: str,
    settings_file: Path,
    *,
    model: str = "",
    permission_args: Sequence[str] = (),
) -> list[str]:
    """The interactive argv for continuing an existing session."""
    argv = [
        BINARY,
        "--resume", str(native_id),
        "--settings", str(settings_file),
        *(permission_args or ("--permission-mode", "dontAsk")),
    ]
    if model:
        argv += ["--model", str(model)]
    argv.append(prompt)
    return argv


def interactive_completion_argv(
    spec: CompletionSpec,
    settings_file: Path,
    *,
    permission_args: Sequence[str] = (),
) -> list[str]:
    """The interactive argv for one bounded call."""
    argv = [BINARY, *permission_args, "--settings", str(settings_file)]
    if spec.model:
        argv += ["--model", str(spec.model)]
    if spec.resume_id:
        argv += ["--resume", str(spec.resume_id)]
    elif spec.session_id:
        argv += ["--session-id", str(spec.session_id)]
    argv += [str(arg) for arg in spec.effort_args]
    argv.append(spec.prompt)
    return argv


def interactive_launcher_source(
    argv: Sequence[str],
    *,
    prompt_file: Path,
    log_file: Path,
    marker_file: Path,
    mode: str = "w",
) -> str:
    """The generated `launch.py` / `resume.py` for interactive mode.

    Spawns interactive claude in the pane TTY (so stdout is a TTY and entrypoint is 'cli'),
    supervises turn completion via the Stop/StopFailure hook marker, writes the assistant
    reply or error to impl.log, and exits cleanly.
    """
    return (
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "import json\n"
        "from pathlib import Path\n\n"
        f"brief = Path(r'{prompt_file}').read_text()\n"
        f"log = open(r'{log_file}', '{mode}', encoding='utf-8')\n"
        f"marker = Path(r'{marker_file}')\n"
        "if marker.exists():\n"
        "    try:\n"
        "        marker.unlink()\n"
        "    except Exception:\n"
        "        pass\n\n"
        "argv = [\n"
        f"{_render_flags(list(argv)[:-1])}"
        "    brief,\n"
        "]\n\n"
        "# Launch interactive claude on pane TTY (inherits stdin/stdout/stderr)\n"
        "proc = subprocess.Popen(argv)\n\n"
        "while proc.poll() is None:\n"
        "    if marker.exists():\n"
        "        time.sleep(0.5)\n"
        "        break\n"
        "    time.sleep(0.3)\n\n"
        "if marker.exists():\n"
        "    try:\n"
        "        data = json.loads(marker.read_text(encoding='utf-8'))\n"
        "        msg = data.get('last_assistant_message') or ''\n"
        "        if msg:\n"
        "            log.write(msg + '\\n')\n"
        "        err = data.get('error')\n"
        "        if err:\n"
        "            log.write(f'Error: {err}\\n')\n"
        "    except Exception:\n"
        "        pass\n\n"
        "if proc.poll() is None:\n"
        "    proc.terminate()\n"
        "    try:\n"
        "        proc.wait(timeout=5)\n"
        "    except subprocess.TimeoutExpired:\n"
        "        proc.kill()\n\n"
        "log.flush()\n"
        "log.close()\n"
        "sys.exit(proc.returncode if proc.returncode is not None else 0)\n"
    )


def tmux_command(
    window: str,
    worktree: Path,
    launcher: Path,
    *,
    session: str = "",
    python: Optional[Path] = None,
) -> str:
    """The shell line that opens the implementer's tmux window, as in the reference."""
    return (
        f"tmux new-window -t {session or TMUX_SESSION} -n {window} "
        f"'cd {worktree} && {python or VENV_PYTHON} {launcher}'"
    )


# ── usage.json ──


def _read_usage_document(path: Path) -> Optional[Mapping[str, Any]]:
    """The usage.json document, or `None` when there is nothing usable to read.

    The file is written by an external sampler (the Claude Code `statusLine` hook) with a
    temp-file-plus-rename, so a reader can still meet a missing file — and must never
    raise on one. `None` means "no sample", which is *not* the same as a sample reporting
    zero pressure.
    """
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    return document if isinstance(document, Mapping) else None


# ── the driver ──


class ClaudeBackend:
    """`claude` driver supporting both interactive mode (default) and legacy headless mode (`claude -p`).

    In interactive mode (`mode="interactive"`, default), commands run attached to a TTY (under tmux
    or PTY) so Claude Code records `entrypoint: "cli"` rather than `entrypoint: "sdk-cli"`.
    This yields standard subscription quota rates rather than headless multiplier surcharges.
    A Stop/StopFailure hook harness captures the turn completion, records results/errors to `impl.log`,
    and terminates the process cleanly.

    Constructed with no arguments by `maestro.backends.registry.get_backend`; every
    keyword exists so a test can inject a substitute instead of monkeypatching a global.
    Each defaults to `None` and falls back to the module global **at call time**, so
    rebasing the globals (as the characterisation sandbox does) still takes effect.
    """

    name = "claude"
    #: P12 pilot finding D30: `complete` runs the agent on the global allow list, so the
    #: worker tells it how to ask Dan for a denied family (`permission_asks.BRIEF_RULE`).
    permission_lists = True

    def __init__(
        self,
        *,
        mode: Optional[str] = None,
        tmux_session: str = "",
        python: Optional[Path] = None,
        usage_path: Optional[Path] = None,
        claude_home: Optional[Path | str] = None,
        claude_json: Optional[Path | str] = None,
        new_session_id: Optional[Callable[[], str]] = None,
        runner: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._mode = mode
        self._tmux_session = tmux_session
        self._python = python
        self._usage_path = usage_path
        self._claude_home = Path(claude_home) if claude_home else None
        self._claude_json = Path(claude_json) if claude_json else None
        self._new_session_id = new_session_id or (lambda: str(uuid.uuid4()))
        #: The one seam that actually starts a process for `complete()`. Injectable for
        #: the same reason every other seam here is: the suite must be able to cover the
        #: whole driver without spending a token of quota.
        self._run = runner or subprocess.run
        self._runner_injected = runner is not None

    def mode(self) -> str:
        """The active execution mode: 'interactive' (default) or 'headless'."""
        if self._mode:
            return self._mode
        env_mode = os.environ.get("MAESTRO_CLAUDE_MODE", "").strip().lower()
        if env_mode in ("interactive", "headless"):
            return env_mode
        try:
            from maestro import config
            proj_mode = config.threshold("claude_mode", None) or getattr(config, "claude_mode", lambda: None)()
            if proj_mode in ("interactive", "headless"):
                return proj_mode
        except Exception:
            pass
        return DEFAULT_MODE

    # ── capabilities ──

    def capabilities(self) -> Capabilities:
        """What this driver supports. Pure — core code may call it freely.

        `sandbox=False` is a statement about the *driver*, not about safety: the Claude
        CLI has no sandbox-mode flag in the invocation F4 pins, and the isolation an
        implementer actually gets comes from running in a throwaway git worktree.
        """
        return Capabilities(
            native_resume=True,
            system_prompt_file=True,
            usage_telemetry=True,
            sandbox=False,
        )

    # ── launch ──

    def launch(self, spec: LaunchSpec) -> Handle:
        """Write the workspace files and open the tmux window that runs them.

        Headless mode writes the brief, session UUID, launcher and durable launch parameters.
        In interactive mode, hooks and settings files are also written and workspace trust is ensured.
        """
        workspace = Path(spec.workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        from maestro.backends import launch_state
        launch_state.write(spec)

        native_id = str(self._new_session_id())
        brief_file = workspace / BRIEF_FILE
        brief_file.write_text(spec.brief, encoding="utf-8")
        (workspace / SESSION_UUID_FILE).write_text(native_id, encoding="utf-8")

        launcher = workspace / LAUNCHER_FILE
        from maestro import permissions

        permission_args = permissions.cli_args(cwd=Path(spec.worktree), add_dirs=(workspace,), writable=True)
        mode = self.mode()
        if mode == "interactive":
            ensure_workspace_trusted(spec.worktree, claude_json=self._claude_json)
            ensure_workspace_trusted(workspace, claude_json=self._claude_json)
            _, settings_file, marker_file = setup_interactive_hooks(workspace)
            argv = interactive_launch_argv(spec, native_id, settings_file, permission_args=permission_args)
            launcher_code = interactive_launcher_source(
                argv,
                prompt_file=brief_file,
                log_file=workspace / LOG_FILE,
                marker_file=marker_file,
            )
        else:
            argv = launch_argv(spec, native_id, permission_args=permission_args)
            launcher_code = launcher_source(
                argv,
                prompt_file=brief_file,
                log_file=workspace / LOG_FILE,
            )

        launcher.write_text(launcher_code, encoding="utf-8")
        launcher.chmod(LAUNCHER_MODE)

        self._open_window(window_name(spec.task_id), Path(spec.worktree), launcher)
        return Handle(
            backend=self.name,
            session_id=spec.session_id,
            native_id=native_id,
            workspace=workspace,
            window=window_target(spec.task_id, self._tmux_session),
        )

    def resume(
        self,
        handle: Handle,
        prompt: str,
        *,
        model: str = "",
        worktree: Optional[Path] = None,
        spec: Optional[LaunchSpec] = None,
    ) -> Handle:
        """Continue `handle`'s session with `prompt`, in the worktree it was built in.

        The cwd matters: a Claude session is keyed to the directory it ran in
        (`~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`), so resuming from anywhere else
        looks in the wrong project directory and fails to find the conversation. When the
        caller does not pass `worktree`, it is derived from the task id — the reference's
        `worktree_path_for` is a pure function of that id, which is precisely what lets a
        mid-task backend switch reuse the worktree instead of recreating it.

        The uuid comes from the handle, falling back to the `session_uuid.txt` sidecar a
        launch left behind, so a handle rebuilt from `in_flight` (which has no
        `native_id`) is still resumable.
        """
        workspace = Path(handle.workspace)
        from maestro.backends import launch_state
        retained = spec if spec is not None else launch_state.read(workspace)
        effort_args = tuple(retained.effort_args) if retained is not None else ()
        if retained is not None:
            model = model or retained.model
            worktree = worktree if worktree is not None else retained.worktree
        native_id = handle.native_id or session_uuid_in(workspace)
        if not native_id:
            raise ValueError(
                f"cannot resume {handle.session_id!r}: no session uuid on the handle and "
                f"no {SESSION_UUID_FILE} in {workspace}"
            )

        task_id = task_id_from_window(handle.window)
        prompt_file = workspace / RESUME_PROMPT_FILE
        prompt_file.write_text(prompt, encoding="utf-8")
        cwd = Path(worktree) if worktree is not None else worktree_path_for(task_id)
        if retained is not None:
            from dataclasses import replace
            launch_state.write(replace(retained, model=model, worktree=cwd,
                                       workspace=workspace, session_id=handle.session_id,
                                       brief=prompt))

        launcher = workspace / RESUME_LAUNCHER_FILE
        from maestro import permissions

        permission_args = permissions.cli_args(cwd=cwd, add_dirs=(workspace,), writable=True)
        mode = self.mode()
        if mode == "interactive":
            ensure_workspace_trusted(cwd, claude_json=self._claude_json)
            ensure_workspace_trusted(workspace, claude_json=self._claude_json)
            _, settings_file, marker_file = setup_interactive_hooks(workspace)
            argv = interactive_resume_argv(native_id, prompt, settings_file, model=model, permission_args=permission_args)
            argv = argv[:-1] + list(effort_args) + argv[-1:]
            launcher_code = interactive_launcher_source(
                argv,
                prompt_file=prompt_file,
                log_file=workspace / LOG_FILE,
                marker_file=marker_file,
                mode="a",
            )
        else:
            argv = resume_argv(native_id, prompt, model=model, permission_args=permission_args)
            argv = argv[:-1] + list(effort_args) + argv[-1:]
            launcher_code = launcher_source(
                argv,
                prompt_file=prompt_file,
                log_file=workspace / LOG_FILE,
                mode="a",
            )

        launcher.write_text(launcher_code, encoding="utf-8")
        launcher.chmod(LAUNCHER_MODE)

        self._open_window(window_name(task_id), cwd, launcher)
        return Handle(
            backend=self.name,
            session_id=handle.session_id,
            native_id=native_id,
            workspace=workspace,
            window=handle.window or window_target(task_id, self._tmux_session),
        )

    # ── complete ──

    def complete(self, spec: CompletionSpec) -> Completion:
        """Run one bounded call and hand back what the agent said.

        Never raises. A non-zero exit, a timeout and a crash all come back as an empty
        `text` with the detail in `returncode`/`timed_out`, because every caller is
        best-effort and an exception here would take down a poll cycle.

        When `spec.log_file` is set the combined stdout+stderr is written there as well as
        returned — `/redo` and self-fix keep that log inside the worktree they ran in, and
        it is the only record of a run that timed out.
        """
        from maestro import permissions

        perm_args = permissions.cli_args(
            cwd=spec.cwd, add_dirs=tuple(spec.add_dirs), writable=spec.writable,
        )
        cwd = str(spec.cwd) if spec.cwd else None
        mode = self.mode()

        if spec.session_id and not spec.resume_id and spec.log_file is not None:
            # The sidecar `session_uuid_in` reads, beside the log: how the controller finds
            # this call's transcript at settle without being told the uuid.
            try:
                (Path(spec.log_file).parent / SESSION_UUID_FILE).write_text(
                    f"{spec.session_id}\n", encoding="utf-8"
                )
            except OSError:
                pass

        if mode == "headless":
            argv = completion_argv(spec, permission_args=perm_args)
            try:
                result = self._run(
                    argv, cwd=cwd, capture_output=True, text=True, timeout=spec.timeout,
                )
            except subprocess.TimeoutExpired:
                _write_log(spec.log_file, f"(timed out after {spec.timeout}s)")
                return Completion(text="", returncode=124, timed_out=True)
            except Exception as exc:
                _write_log(spec.log_file, f"(failed to run: {exc})")
                return Completion(text="", returncode=1)

            stdout = getattr(result, "stdout", "") or ""
            stderr = getattr(result, "stderr", "") or ""
            rc = int(getattr(result, "returncode", 0) or 0)
            _write_log(spec.log_file, stdout + stderr)
            return Completion(text=stdout.strip() if rc == 0 else "", returncode=rc)

        # ── Interactive mode ──
        ensure_workspace_trusted(spec.cwd or Path.cwd(), claude_json=self._claude_json)

        import tempfile

        hook_dir = None
        if spec.log_file is not None:
            try:
                candidate = Path(spec.log_file).parent
                candidate.mkdir(parents=True, exist_ok=True)
                hook_dir = candidate
            except Exception:
                hook_dir = None
        # Never under `spec.cwd`: that is the task worktree, and files there are picked up
        # by the gate's clean-tree check and by the agent's own commits (itv pilot).
        if hook_dir is None:
            hook_dir = Path(tempfile.mkdtemp(prefix="maestro-interactive-"))

        _, settings_file, marker_file = setup_interactive_hooks(hook_dir)
        argv = interactive_completion_argv(spec, settings_file, permission_args=perm_args)

        if self._runner_injected:
            # Custom test runner injected
            try:
                result = self._run(
                    argv, cwd=cwd, capture_output=True, text=True, timeout=spec.timeout,
                )
            except subprocess.TimeoutExpired:
                _write_log(spec.log_file, f"(timed out after {spec.timeout}s)")
                return Completion(text="", returncode=124, timed_out=True)
            except Exception as exc:
                _write_log(spec.log_file, f"(failed to run: {exc})")
                return Completion(text="", returncode=1)

            stdout = getattr(result, "stdout", "") or ""
            stderr = getattr(result, "stderr", "") or ""
            rc = int(getattr(result, "returncode", 0) or 0)
            _write_log(spec.log_file, stdout + stderr)
            return Completion(text=stdout.strip() if rc == 0 else "", returncode=rc)

        # Run with pseudo-terminal (PTY) to ensure process.stdout.isTTY is True
        # which grants entrypoint "cli" instead of "sdk-cli"
        import pty
        master, slave = pty.openpty()
        timed_out = False
        text = ""
        rc = 0
        try:
            proc = subprocess.Popen(
                argv, stdin=slave, stdout=slave, stderr=slave, cwd=cwd, close_fds=True,
            )
            os.close(slave)
            slave = -1

            start_time = time.time()
            timeout = spec.timeout or 300
            while proc.poll() is None:
                if marker_file.exists():
                    time.sleep(0.2)
                    break
                if time.time() - start_time > timeout:
                    timed_out = True
                    break
                time.sleep(0.2)

            if timed_out:
                rc = 124
                text = ""
            elif marker_file.exists():
                try:
                    payload = json.loads(marker_file.read_text(encoding="utf-8"))
                    text = payload.get("last_assistant_message", "") or ""
                    err = payload.get("error")
                    if err:
                        rc = 1
                except Exception:
                    pass
            else:
                rc = proc.poll() or 1

            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except subprocess.TimeoutExpired:
            timed_out = True
            rc = 124
        except Exception as exc:
            rc = 1
            _write_log(spec.log_file, f"(failed to run: {exc})")
            return Completion(text="", returncode=1)
        finally:
            if slave >= 0:
                try:
                    os.close(slave)
                except OSError:
                    pass
            try:
                os.close(master)
            except OSError:
                pass

        if timed_out:
            _write_log(spec.log_file, f"(timed out after {spec.timeout}s)")
            return Completion(text="", returncode=124, timed_out=True)

        _write_log(spec.log_file, text)
        return Completion(text=text.strip() if rc == 0 else "", returncode=rc)

    def _open_window(self, window: str, worktree: Path, launcher: Path) -> None:
        subprocess.run(
            tmux_command(
                window,
                worktree,
                launcher,
                session=self._tmux_session,
                python=self._python,
            ),
            shell=True,
            check=True,
        )

    # ── exit ──

    def parse_exit(self, rc: int, log_tail: str) -> ExitVerdict:
        """Classify a finished run from its return code and the tail of `impl.log`.

        The quota branch is extracted behaviour, delegated whole to `maestro.quota`:
        `_LIMIT_RE` over the last `TAIL_LINES` lines, then `_resolve_limit_reset` for a
        Zulu reset time. Neither is re-derived here, and the limit is checked **before**
        the return code because the reference's reactive net never sees a return code —
        it scans `impl.log` alone, so a limit notice decides the verdict whatever the
        process exited with.

        `ok` / `crashed` is new M2 surface (see the module docstring): the reference has
        no exit-code mapping for implementers at all. `orchestrator.py`'s window-liveness
        plus `PAUSED`/`DONE`/`FAILED` sentinel protocol remains the authority on whether
        a task succeeded; this verdict only says how the *process* ended.
        """
        tail = "\n".join(str(log_tail or "").splitlines()[-TAIL_LINES:])
        if tail and quota._LIMIT_RE.search(tail):
            return ExitVerdict(
                kind="quota_exhausted",
                reset_at=quota._resolve_limit_reset(tail),
                evidence=self._evidence(tail, matching=True),
            )
        if rc == 0:
            return ExitVerdict(kind="ok")
        return ExitVerdict(kind="crashed", evidence=self._evidence(tail))

    @staticmethod
    def _evidence(tail: str, *, matching: bool = False) -> str:
        """A short journal excerpt: the line that matched, else the last line seen."""
        lines = [line for line in tail.splitlines() if line.strip()]
        if matching:
            line = next(
                (candidate for candidate in lines if quota._LIMIT_RE.search(candidate)),
                tail[-EVIDENCE_CHARS:],
            )
        else:
            line = lines[-1] if lines else ""
        return line.strip()[:EVIDENCE_CHARS]

    # ── usage ──

    def claude_home(self) -> Path:
        """Where Claude Code keeps its own state. Honours `CLAUDE_CONFIG_DIR`, as the CLI
        does.

        A method rather than a module global, for the reason `CodexBackend.codex_home` is
        one: a `Path` global here would be resolved at import time, could not be
        redirected by a test, and does not live under `REPO` — which is the one root the
        characterisation sandbox rebases.
        """
        if self._claude_home is not None:
            return self._claude_home
        override = os.environ.get("CLAUDE_CONFIG_DIR")
        return Path(override).expanduser() if override else Path.home() / ".claude"

    def transcripts_dir(self) -> Path:
        """One directory per session cwd, each holding one JSONL per session."""
        return self.claude_home() / "projects"

    def transcript_path(self, session_uuid: str) -> Optional[Path]:
        """The JSONL Claude Code is writing for `session_uuid`, or `None` when none exists.

        Keyed on the uuid, and deliberately **not** on a reconstruction of the session's
        cwd. The directory that file sits in is that cwd with its separators flattened,
        and the flattening is both lossy and version-dependent — two different cwds can
        flatten onto one directory name, and a leading-dot component is not rendered the
        same way by every build of the CLI. The uuid, by contrast, is the uuid4 `launch`
        minted and passed to `--session-id`: unique across every project directory, and
        known both to the handle and to the `session_uuid.txt` sidecar. That is the same
        trade `CodexBackend.rollout_path` already makes, globbing for the thread id
        rather than rebuilding a path.

        Newest first, so the answer stays stable if a uuid ever appeared twice.
        """
        if not session_uuid:
            return None
        try:
            matches = sorted(
                self.transcripts_dir().glob(f"*/{session_uuid}.jsonl"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return None
        return matches[0] if matches else None

    def usage(self, handle: Optional[Handle] = None) -> Optional[Usage]:
        """A usage sample — about the account, or about one session (A5).

        **With no handle**, the latest sample from `.orchestrator/usage.json`, or `None`.
        The document is the F4 shape written by the Claude Code `statusLine` hook, the
        same file `maestro.quota.get_effective_cap` reads. `from_usage_json` maps
        `five_hour` to `windows[300]` and `seven_day` to `windows[10080]`, keying every
        window by its **duration in minutes** rather than by its name — finding G5 found
        an account with no five-hour window at all, and a threshold keyed to a name that
        is absent can never fire. It names no session, because it belongs to none.

        **With a handle**, that session's own context, read from its transcript. The
        statusline document is emphatically not an answer to this question: it is written
        by whichever *interactive* session is rendering a status line, which is never an
        implementer — an implementer runs `claude -p`, which renders none, with its cwd
        in a worktree where `.orchestrator/` does not exist. Handing that figure back
        stamped with the implementer's session id is precisely the wrong-evidence
        rotation D4's attribution precondition exists to refuse, so a session whose
        transcript cannot be found or read is "not measurable" — `None` (G6) — and
        nothing is substituted for it.

        `None` means "no sample available" and must never be read as "no pressure".
        """
        if handle is not None:
            return self._session_usage(handle)
        path = self._usage_path if self._usage_path is not None else USAGE_JSON
        document = _read_usage_document(path)
        if document is None:
            return None
        return replace(from_usage_json(document), source="statusline_usage_json")

    def subscription_usage(self) -> Usage:
        """The account's 5h/7d windows read fresh from the OAuth usage endpoint.

        Unlike `usage()`, independent of the interactive statusline: this is what per-step
        quota telemetry reads at claim and settle (`routing.driver_usage`). Throttled to
        one fetch per minute across processes by a cache beside `usage.json`; a failed
        read is empty windows plus `error`, never an exception.
        """
        from maestro.backends import claude_subscription

        path = self._usage_path if self._usage_path is not None else USAGE_JSON
        return claude_subscription.read_subscription_usage(
            cache_path=Path(path).parent / claude_subscription.CACHE_FILE
        )

    def attempt_token_usage(self, workspace: Path) -> Optional[dict]:
        """Cumulative tokens of the call whose session uuid `workspace` recorded, or `None`.

        Prefers the CLI's own `cost-state` record (per-model totals, subagents included,
        with its cost estimate — the best relative weight across models); falls back to
        one count per assistant message id across the transcript and its subagent files
        (a streamed message is written as several records repeating one usage). Missing
        counts stay absent rather than being estimated (G6).
        """
        session_uuid = session_uuid_in(Path(workspace))
        path = self.transcript_path(session_uuid) if session_uuid else None
        if path is None:
            return None
        return session_token_totals(path)

    def _session_usage(self, handle: Handle) -> Optional[Usage]:
        """How full `handle`'s conversation is, from its own transcript, or `None`.

        `windows` is left empty on purpose. A rate-limit window is a property of the
        account and this reading is a property of one conversation; an empty map means
        "no window information here", which `max_used_pct()` reports as `None` rather
        than as a phantom `0` (G5/G6). The account-level question has its own answer.

        `updated_at` is the transcript's own mtime, not the wall clock: it is when the
        agent last wrote a turn, which is exactly what D4's freshness guard is asking
        about. A wall-clock stamp would re-certify, every poll, a transcript that has not
        moved since before the last rotation — which is the loop that guard exists to
        forbid.

        The uuid comes from the handle, falling back to the `session_uuid.txt` sidecar the
        launch left behind, so a handle rebuilt from `in_flight` (which carries no
        `native_id`) is still readable — the same recovery `resume` performs.
        """
        session_uuid = handle.native_id or session_uuid_in(handle.workspace)
        if not session_uuid:
            return None
        path = self.transcript_path(session_uuid)
        if path is None:
            return None
        reading = context_reading(path)
        if reading is None:
            return None
        try:
            taken = _zulu(datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc))
        except OSError:
            return None
        tokens, model = reading
        return Usage(
            context_used_pct=None,
            windows={},
            model=model or None,
            updated_at=taken,
            context_total_input_tokens=tokens,
            session_id=handle.session_id,
        )
