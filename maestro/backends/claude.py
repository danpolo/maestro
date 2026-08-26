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
  sample is stream-agnostic and why `impl.log` is the reactive net that
  `maestro.quota._scan_impl_log_for_limit` reads after the fact.

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
import subprocess
import uuid
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
    "BRIEF_FILE",
    "SESSION_UUID_FILE",
    "LAUNCHER_FILE",
    "LOG_FILE",
    "RESUME_PROMPT_FILE",
    "RESUME_LAUNCHER_FILE",
    "window_name",
    "window_target",
    "task_id_from_window",
    "session_uuid_in",
    "launch_argv",
    "resume_argv",
    "launcher_source",
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

#: Mode the generated launcher is chmod'd to, as in the reference.
LAUNCHER_MODE = 0o755

#: Flags whose value the generated launcher spells as a raw string literal. These are
#: the ones carrying a filesystem path or a uuid; a raw literal is what keeps a Windows
#: -style separator or a stray backslash from being re-interpreted as an escape.
RAW_VALUE_FLAGS = frozenset({"--session-id", "--resume", "--system-prompt-file"})


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


def launch_argv(spec: LaunchSpec, session_uuid: str) -> list[str]:
    """The F4 argv for a fresh session. The brief is positional and last."""
    argv = [BINARY, "-p", "--model", spec.model, "--session-id", str(session_uuid)]
    argv += _system_prompt_arg(spec.system_prompt_file)
    argv.append(spec.brief)
    return argv


def resume_argv(native_id: str, prompt: str, *, model: str = "") -> list[str]:
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
    argv = [BINARY, "-p"]
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


def completion_argv(spec: CompletionSpec) -> list[str]:
    """The argv for one bounded call. The prompt is positional and last.

    Same shape the six ex-hardcoded call sites were already using by hand — `claude -p
    [--model M] [--resume ID] <prompt>` — so routing them through the driver changes which
    binary can be chosen, not what this one is asked to do.

    `--resume` and `--session-id` are still mutually exclusive (F4), and a completion never
    names a session it is *creating*, so only `--resume` can appear. `spec.writable` has no
    spelling here: the Claude CLI has no sandbox-mode flag in the invocation F4 pins, which
    is exactly what `capabilities().sandbox is False` already advertises — the caller gets
    isolation from the directory it points the call at, not from a flag.
    """
    argv = [BINARY, "-p"]
    if spec.model:
        argv += ["--model", str(spec.model)]
    if spec.resume_id:
        argv += ["--resume", str(spec.resume_id)]
    argv.append(spec.prompt)
    return argv


# ── the generated launcher ──


def _render_element(element: str, raw: bool) -> str:
    return f"r'{element}'" if raw else f"'{element}'"


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
    """`claude -p` behind the `AgentBackend` protocol.

    Constructed with no arguments by `maestro.backends.registry.get_backend`; every
    keyword exists so a test can inject a substitute instead of monkeypatching a global.
    Each defaults to `None` and falls back to the module global **at call time**, so
    rebasing the globals (as the characterisation sandbox does) still takes effect.
    """

    name = "claude"

    def __init__(
        self,
        *,
        tmux_session: str = "",
        python: Optional[Path] = None,
        usage_path: Optional[Path] = None,
        new_session_id: Optional[Callable[[], str]] = None,
        runner: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._tmux_session = tmux_session
        self._python = python
        self._usage_path = usage_path
        self._new_session_id = new_session_id or (lambda: str(uuid.uuid4()))
        #: The one seam that actually starts a process for `complete()`. Injectable for
        #: the same reason every other seam here is: the suite must be able to cover the
        #: whole driver without spending a token of quota.
        self._run = runner or subprocess.run

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
        """Write the three workspace files and open the tmux window that runs them.

        Exactly three files are written — `brief.txt`, `session_uuid.txt`, `launch.py` —
        and that count is pinned by the existing characterisation suite. `impl.log` is
        created later, by the launcher itself, inside the tmux window.
        """
        workspace = Path(spec.workspace)
        workspace.mkdir(parents=True, exist_ok=True)

        native_id = str(self._new_session_id())
        brief_file = workspace / BRIEF_FILE
        brief_file.write_text(spec.brief, encoding="utf-8")
        (workspace / SESSION_UUID_FILE).write_text(native_id, encoding="utf-8")

        launcher = workspace / LAUNCHER_FILE
        launcher.write_text(
            launcher_source(
                launch_argv(spec, native_id),
                prompt_file=brief_file,
                log_file=workspace / LOG_FILE,
            ),
            encoding="utf-8",
        )
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
        native_id = handle.native_id or session_uuid_in(workspace)
        if not native_id:
            raise ValueError(
                f"cannot resume {handle.session_id!r}: no session uuid on the handle and "
                f"no {SESSION_UUID_FILE} in {workspace}"
            )

        task_id = task_id_from_window(handle.window)
        prompt_file = workspace / RESUME_PROMPT_FILE
        prompt_file.write_text(prompt, encoding="utf-8")

        launcher = workspace / RESUME_LAUNCHER_FILE
        launcher.write_text(
            launcher_source(
                resume_argv(native_id, prompt, model=model),
                prompt_file=prompt_file,
                log_file=workspace / LOG_FILE,
                mode="a",
            ),
            encoding="utf-8",
        )
        launcher.chmod(LAUNCHER_MODE)

        cwd = Path(worktree) if worktree is not None else worktree_path_for(task_id)
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
        argv = completion_argv(spec)
        cwd = str(spec.cwd) if spec.cwd else None
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
        # A non-zero exit means the answer is not trustworthy even when something was
        # printed, so the text is dropped rather than handed on as if it were a reply.
        return Completion(text=stdout.strip() if rc == 0 else "", returncode=rc)

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

    def usage(self) -> Optional[Usage]:
        """The latest sample from `.orchestrator/usage.json`, or `None` when there is none.

        The document is the F4 shape written by the Claude Code `statusLine` hook, the
        same file `maestro.quota.get_effective_cap` reads. `from_usage_json` maps
        `five_hour` to `windows[300]` and `seven_day` to `windows[10080]`, keying every
        window by its **duration in minutes** rather than by its name — finding G5 found
        an account with no five-hour window at all, and a threshold keyed to a name that
        is absent can never fire.

        `None` means "no sample available" and must never be read as "no quota pressure".
        """
        path = self._usage_path if self._usage_path is not None else USAGE_JSON
        document = _read_usage_document(path)
        return None if document is None else from_usage_json(document)
