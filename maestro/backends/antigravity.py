"""The Antigravity driver: `agy -p`, headless, graded below claude and codex (P07B).

Every fact this module relies on was measured against the installed CLI, not guessed —
`artifacts/graph-engineering/p07b-agy-1.2.2-probe.json` holds the evidence, re-checked
at 1.2.3 (`maestro/thirdparty.json`):

* **The prompt must be last.** `agy -p <prompt>` — `-p` takes the next token as its
  prompt, so `agy -p --output-format json ...` fails with rc=2.
* **A conversation id cannot be chosen at launch.** agy mints it and reports it in the
  first stream-json event (`init.conversation_id`), so `Handle.native_id` is `None` at
  launch and both `resume` and `usage(handle)` recover the id from `impl.log`.
  `--conversation <id>` continues that conversation.
* **Events are keyed by `"event"`,** with the payload nested under the event's own name:
  `{"event": "step_update", "step_update": {...}}`. Parsing tolerates a `"type"` key too.
  Each `agent_response` step carries that turn's `usage`; `result.usage` is the
  *cumulative* sum across turns and is never a context reading.
* **Effort is part of the model id** (`gemini-3.8-flash-high`). A suffixed id conflicts
  with any `--effort`, so this driver never sends one.
* **Quota is push-only.** Headless output carries no pool windows; the only source is
  `~/.gemini/statusline_dump.json`, written by the operator's interactive status line.

Same launch conventions as the claude driver: the brief is read from a file by a
generated launcher, which opens `<workspace>/impl.log` and merges stderr into stdout,
inside a tmux window named after the task.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

from maestro import quota
from maestro.backends import claude as _claude
from maestro.backends import agy_runtime, agy_worker, control_files, launch_state, native_ownership
from maestro.backends.base import (
    FIVE_HOUR_MINUTES,
    SEVEN_DAY_MINUTES,
    Capabilities,
    Completion,
    CompletionSpec,
    ExitVerdict,
    Handle,
    LaunchSpec,
    TokenSplit,
    Usage,
    WindowUsage,
)
from maestro.worktree import worktree_path_for

BINARY = "agy"

BRIEF_FILE = "brief.txt"
LAUNCHER_FILE = "launch.py"
LOG_FILE = "impl.log"
RESUME_PROMPT_FILE = "resume_prompt.txt"
RESUME_LAUNCHER_FILE = "resume.py"
LAUNCHER_MODE = 0o755
EVIDENCE_CHARS = 200

#: The push file the operator's interactive status line writes, under `gemini_home()`.
STATUSLINE_DUMP = "statusline_dump.json"

#: The status line's Gemini pool keys, mapped onto the duration each one means.
POOL_WINDOWS = {"gemini-5h": FIVE_HOUR_MINUTES, "gemini-weekly": SEVEN_DAY_MINUTES}

#: The status line's third-party pool keys. These bill the models agy resells
#: (`claude-*`, `gpt-oss-*`) and are a quota of their own: exhausting them leaves the
#: Gemini pools untouched, and vice versa.
THIRD_PARTY_POOL_WINDOWS = {"3p-5h": FIVE_HOUR_MINUTES, "3p-weekly": SEVEN_DAY_MINUTES}

#: The catalog `usage_pool_id`s these two sets of windows are reported under, matching
#: `catalog.ANTIGRAVITY_CATALOG_DEFAULTS` and `catalog.AGY_THIRD_PARTY_POOL`.
GEMINI_POOL_ID = "antigravity-gemini"
THIRD_PARTY_POOL_ID = "antigravity-3p"

#: How far ahead to pause when a limit notice names no usable reset time.
FALLBACK_RESET = timedelta(hours=1)


def _pool_windows(quota: Any, keys: Mapping[str, int]) -> dict[int, WindowUsage]:
    """The windows of one quota pool from the status line's `quota` object.

    A pool the dump does not carry is absent rather than zero: "not measured" is never
    "not used" (G6). `remaining_fraction` is inverted into a used percentage.
    """
    windows: dict[int, WindowUsage] = {}
    for key, minutes in keys.items():
        pool = quota.get(key) if isinstance(quota, Mapping) else None
        if not isinstance(pool, Mapping):
            continue
        fraction = pool.get("remaining_fraction")
        used = (
            (1.0 - float(fraction)) * 100.0
            if isinstance(fraction, (int, float)) and not isinstance(fraction, bool)
            else None
        )
        windows[minutes] = WindowUsage(used_pct=used, resets_at=_epoch(pool.get("reset_time")))
    return windows


# ── stream-json ──


def _iter_events(text: str) -> Iterator[tuple[str, Mapping]]:
    """`(kind, payload)` for every JSON event line; non-JSON lines are skipped."""
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, Mapping):
            continue
        kind = event.get("event") or event.get("type")
        if not isinstance(kind, str):
            continue
        payload = event.get(kind)
        yield kind, payload if isinstance(payload, Mapping) else event


def _completion_document(text: str) -> Mapping:
    """The last `--output-format json` document in the log, else `{}`.

    `complete()` (the graph worker's path) logs ONE top-level document carrying `status`,
    not stream-json events; it plays the part of the stream's `result` payload.
    """
    found: Mapping = {}
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            document = json.loads(line)
        except ValueError:
            continue
        if isinstance(document, Mapping) and "status" in document and not (
            document.get("event") or document.get("type")
        ):
            found = document
    return found


def _non_json_lines(text: str) -> list[str]:
    lines = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            if isinstance(json.loads(stripped), Mapping):
                continue
        except ValueError:
            pass
        lines.append(stripped)
    return lines


def conversation_id_in(text: str) -> str:
    """The conversation id the log's latest `init` (or `result`) event names, or `""`."""
    found = ""
    for kind, payload in _iter_events(text):
        if kind in ("init", "result"):
            cid = payload.get("conversation_id")
            if isinstance(cid, str) and cid:
                found = cid
    return found


def _as_count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(int(value), 0)


def _zulu(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_text(path: Path) -> Optional[str]:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _mtime_zulu(path: Path) -> str:
    try:
        return _zulu(datetime.fromtimestamp(Path(path).stat().st_mtime, tz=timezone.utc))
    except OSError:
        return ""


# ── argv ──


#: The flag that names agy's active workspace. Without it a headless run's system prompt
#: says "The user does not have any active workspace" even though the process cwd is the
#: worktree, and the model guesses its own directory: observed 2026-09-17 (agy 1.2.4), 2 of
#: 6 bounded edit probes wrote into $HOME instead of the cwd, in both the default mode and
#: `--mode accept-edits`. With `--add-dir <cwd>` the model reports that path as its workspace.
WORKSPACE_FLAG = "--add-dir"


def _workspace_args(directory: Optional[Path | str]) -> list[str]:
    return [WORKSPACE_FLAG, str(directory)] if directory else []


def launch_argv(spec: LaunchSpec) -> list[str]:
    """A fresh headless session in `spec.worktree`. The brief is `-p`'s value and last."""
    return [
        BINARY, "--output-format", "stream-json", "--model", spec.model,
        *_workspace_args(spec.worktree), "-p", spec.brief,
    ]


def resume_argv(
    conversation_id: str, prompt: str, *, model: str = "",
    workspace: Optional[Path | str] = None,
) -> list[str]:
    """Continue a conversation by id; `--model` only when the caller knows it."""
    argv = [BINARY, "--output-format", "stream-json", "--conversation", str(conversation_id)]
    if model:
        argv += ["--model", str(model)]
    return argv + _workspace_args(workspace) + ["-p", prompt]


def completion_argv(spec: CompletionSpec) -> list[str]:
    """One bounded call, answered as a single JSON document on stdout."""
    argv = [BINARY, "--output-format", "json"]
    if spec.model:
        argv += ["--model", str(spec.model)]
    if spec.resume_id:
        argv += ["--conversation", str(spec.resume_id)]
    argv += [str(arg) for arg in spec.effort_args]
    return argv + _workspace_args(spec.cwd) + ["-p", spec.prompt]


def _profile_argv(argv: list[str], run: agy_runtime.PreparedRun) -> list[str]:
    """Explain live task rules and frozen execution configuration at every call."""
    argv = [*argv[:-1], argv[-1] + (
        '\n\nDirect AGENTS.md and GEMINI.md are LIVE: native directory scope and normal task edit permissions '
        'apply to those task files. They are read from the current task tree, not frozen snapshots. '
        'Global and discovery-alias instruction snapshots, including rewritten references, remain frozen '
        'for this invocation.'
    )]
    if run.transaction is None:
        return argv
    return [*argv[:-1], argv[-1] + (
        '\n\nGit staging for this invocation: this replaces any earlier git add -A '
        'shorthand. Stage only the exact task files you changed, using '
        'git add -- <task paths> (replace the placeholder with quoted file paths), '
        'and inspect the staged diff before committing. The frozen customization '
        'view can replace execution configuration, omit plugins, and rewrite rule '
        'references. Those profile differences are not task edits; preserve the '
        'original user files in commits. Blind all-tree staging with real tracked '
        'customization is unqualified and the parent publication audit will refuse '
        'it. Do not force-add profile controls or change Git ignore/index settings '
        'to bypass that audit.'
    )]


# ── the generated launcher ──


def launcher_source(
    argv: Sequence[str], *, prompt_file: Path, log_file: Path, mode: str = "w"
) -> str:
    """The generated launcher: one `repr()`'d argv element per line, prompt from disk.

    The last element (the brief or resume prompt) is not inlined; it is read from
    `prompt_file` at run time. `mode="a"` on resume keeps the launch's log, which is
    where the conversation id and the exit evidence live.
    """
    flags = "".join(f"    {element!r},\n" for element in list(argv)[:-1])
    return (
        "import subprocess\nfrom pathlib import Path\n"
        f"brief = Path({str(prompt_file)!r}).read_text()\n"
        f"log = open({str(log_file)!r}, {mode!r}, encoding='utf-8')\n"
        "subprocess.run([\n"
        f"{flags}"
        "    brief,\n], stdout=log, stderr=subprocess.STDOUT)\n"
        "log.flush()\nlog.close()\n"
    )


# ── the driver ──


class AntigravityBackend:
    """`agy` behind the `AgentBackend` protocol. Every keyword is a test seam."""

    name = "antigravity"

    def __init__(
        self,
        *,
        tmux_session: str = "",
        python: Optional[Path] = None,
        gemini_home: Optional[Path | str] = None,
        runner: Optional[Callable[..., Any]] = None,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._tmux_session = tmux_session
        self._python = python
        self._gemini_home = Path(gemini_home) if gemini_home else None
        self._run = runner or agy_worker.run
        self._now = now or (lambda: datetime.now(timezone.utc))

    def capabilities(self) -> Capabilities:
        """`sandbox=False`: `--sandbox` restricts the terminal, not the filesystem.

        Exercised headless on 2026-09-16 (agy 1.2.3, P12A S2 `_p07b` thread #4). Two
        bounded probes in a scratch git repo, both rc 0: maestro's own `completion_argv`
        shape edited a workspace file with no permission prompt and no permission flag,
        and the same run under `--sandbox` still wrote to the workspace. `--help` calls
        it "a sandbox with terminal restrictions", and that is what was observed — so
        this flag, which means "can constrain filesystem/network access", stays `False`.
        Declaring `True` would advertise confinement maestro does not have.
        `--mode accept-edits` was characterised on 2026-09-17 (agy 1.2.4): under
        `toolPermission: always-proceed` it changed nothing observable, so it is not passed;
        every argv carries `PERMISSION_FLAG` instead, which makes the host setting moot. Neither flag
        confines the filesystem; see `WORKSPACE_FLAG` for the escape that was observed.
        """
        return Capabilities(
            native_resume=True,
            system_prompt_file=False,
            usage_telemetry=True,
            sandbox=False,
        )

    # ── launch / resume ──

    @native_ownership.driver
    def launch(self, spec: LaunchSpec) -> Handle:
        if spec.effort_args or spec.effort_config:
            raise ValueError('agy effort must be part of the selected model variant')
        workspace = control_files.workspace(spec.workspace, (
            Path(spec.workspace) / 'resume.py', Path(spec.workspace) / launch_state.FILENAME,
            Path(spec.workspace) / 'launch_spec.tmp'))
        control_files.mkdir(workspace, parents=True, exist_ok=True)
        control_files.directory(spec.worktree)
        # Clear the previous conversation before an asynchronous launcher starts.
        control_files.write(workspace / LOG_FILE, '')
        launch_state.write(spec)
        brief_file = workspace / BRIEF_FILE
        control_files.write(brief_file, spec.brief)
        run = agy_runtime.prepare(cwd=spec.worktree, workspace=workspace,
            write_roots=(spec.worktree, workspace), home=self.gemini_home().parent)
        launcher = workspace / LAUNCHER_FILE
        try:
            control_files.write(launcher, run.launcher(_profile_argv(launch_argv(spec), run),
                log_file=workspace / LOG_FILE, mode='w'))
            control_files.chmod(launcher, LAUNCHER_MODE)
            self._open_window(_claude.window_name(spec.task_id), Path(spec.worktree), launcher)
        except BaseException:
            run.cleanup()
            raise
        return Handle(
            backend=self.name,
            session_id=spec.session_id,
            native_id=None,  # agy mints the conversation id; it appears in impl.log
            workspace=workspace,
            window=_claude.window_target(spec.task_id, self._tmux_session),
        )

    @native_ownership.driver
    def resume(
        self,
        handle: Handle,
        prompt: str,
        *,
        model: str = "",
        worktree: Optional[Path] = None,
        spec: Optional[LaunchSpec] = None,
    ) -> Handle:
        """Continue `handle`'s conversation, recovering its id from `impl.log`."""
        workspace = control_files.workspace(handle.workspace, (
            Path(handle.workspace) / 'resume.py', Path(handle.workspace) / launch_state.FILENAME,
            Path(handle.workspace) / 'launch_spec.tmp'))
        retained = spec if spec is not None else launch_state.read(workspace)
        if retained is not None and (retained.effort_args or retained.effort_config):
            raise ValueError('agy effort must be part of the selected model variant')
        if retained is not None:
            model = model or retained.model
            worktree = worktree if worktree is not None else retained.worktree
        conversation_id = handle.native_id or conversation_id_in(
            _read_text(workspace / LOG_FILE) or ""
        )
        if not conversation_id:
            raise ValueError(
                f"cannot resume {handle.session_id!r}: no conversation id on the handle and "
                f"none in {workspace / LOG_FILE}"
            )

        task_id = _claude.task_id_from_window(handle.window)
        cwd = Path(worktree) if worktree is not None else worktree_path_for(task_id)
        control_files.directory(cwd)
        native_ownership.reserve_tree(cwd)
        if retained is not None:
            from dataclasses import replace
            launch_state.write(replace(retained, model=model, worktree=cwd,
                                       workspace=workspace, session_id=handle.session_id,
                                       brief=prompt))
        prompt_file = workspace / RESUME_PROMPT_FILE
        control_files.write(prompt_file, prompt)
        run = agy_runtime.prepare(cwd=cwd, workspace=workspace,
            write_roots=(cwd, workspace), home=self.gemini_home().parent)
        launcher = workspace / RESUME_LAUNCHER_FILE
        try:
            control_files.write(launcher, run.launcher(
                _profile_argv(resume_argv(conversation_id, prompt, model=model, workspace=cwd), run),
                log_file=workspace / LOG_FILE,
                mode="a",
            ))
            control_files.chmod(launcher, LAUNCHER_MODE)
            self._open_window(_claude.window_name(task_id), cwd, launcher)
        except BaseException:
            run.cleanup()
            raise
        return Handle(
            backend=self.name,
            session_id=handle.session_id,
            native_id=conversation_id,
            workspace=workspace,
            window=handle.window or _claude.window_target(task_id, self._tmux_session),
        )

    def _open_window(self, window: str, worktree: Path, launcher: Path) -> None:
        subprocess.run(
            _claude.tmux_command(
                window, worktree, launcher, session=self._tmux_session, python=self._python
            ),
            shell=True,
            check=True,
        )

    # ── complete ──

    @native_ownership.driver
    def complete(self, spec: CompletionSpec) -> Completion:
        """One bounded call. Never raises; text only for rc 0 and `status == SUCCESS`."""
        deadline = time.monotonic() + spec.timeout
        run = None
        temporary = None
        native_log = ''
        def remaining():
            value = deadline - time.monotonic()
            if value <= 0:
                raise subprocess.TimeoutExpired(BINARY, spec.timeout, output=native_log.encode('utf-8'))
            return value
        def emit(text):
            if spec.log_file:
                try:
                    if isinstance(text, bytes):
                        control_files.write_bytes(spec.log_file, text)
                    else:
                        control_files.write(spec.log_file, text)
                except (OSError, ValueError):
                    pass
        try:
            if spec.effort_args or spec.effort_config:
                raise ValueError('agy effort must be part of the selected model variant')
            cwd = control_files.directory(spec.cwd if spec.cwd is not None else Path.cwd())
            if spec.log_file is not None:
                control_files.file(spec.log_file)
                workspace = control_files.directory(Path(spec.log_file).absolute().parent)
                control_files.mkdir(workspace, parents=True, exist_ok=True)
            else:
                temporary = tempfile.TemporaryDirectory(prefix='maestro-agy-')
                workspace = Path(temporary.name)
            roots = tuple(dict.fromkeys((cwd, *spec.add_dirs))) if spec.writable else ()
            run = agy_runtime.prepare(cwd=cwd, workspace=workspace,
                write_roots=roots, home=self.gemini_home().parent,
                protected_paths=(spec.log_file,) if spec.log_file else ())
            if run.transaction is not None:
                run.transaction.start()
            argv = run.wrap(_profile_argv(completion_argv(spec), run))
            result = self._run(
                argv, cwd=str(cwd), capture_output=True,
                text=True, timeout=remaining(), env=run.environment(),
            )
            native_log = (getattr(result, 'stdout', '') or '') + (getattr(result, 'stderr', '') or '')
            # Retain native evidence before trusted publication can refuse or
            # fail. A native SUCCESS never overrides a parent audit failure.
            emit(native_log)
            remaining()
            if run.transaction is not None:
                document = json.loads(result.stdout) if result.returncode == 0 else {}
                success = (isinstance(document, dict) and document.get('status') == 'SUCCESS'
                           and isinstance(document.get('response'), str)
                           and bool(document['response'].strip()))
                run.transaction.finish(success=success, verify=remaining)
                if result.returncode == 0 and not success:
                    raise ValueError('native Git completion has no successful final response')
        except subprocess.TimeoutExpired as exc:
            def raw(value):
                return value.encode('utf-8') if isinstance(value, str) else (value or b'')
            partial = raw(exc.output) + raw(exc.stderr)
            separator = b'\n' if partial and not partial.endswith(b'\n') else b''
            emit(partial + separator + f"(timed out after {spec.timeout}s)".encode('utf-8'))
            return Completion(text="", returncode=124, timed_out=True)
        except Exception as exc:
            separator = '\n' if native_log and not native_log.endswith('\n') else ''
            emit(native_log + separator + f"(failed to run: {exc})")
            return Completion(text="", returncode=1)
        finally:
            if run is not None:
                run.cleanup()
            if temporary is not None:
                temporary.cleanup()

        stdout = getattr(result, "stdout", "") or ""
        stderr = getattr(result, "stderr", "") or ""
        rc = int(getattr(result, "returncode", 0) or 0)
        if rc != 0:
            return Completion(text="", returncode=rc)
        try:
            document = json.loads(stdout)
        except ValueError:
            return Completion(text="", returncode=rc)
        if not isinstance(document, Mapping) or document.get("status") != "SUCCESS":
            return Completion(text="", returncode=rc)
        response = document.get("response")
        return Completion(text=response.strip() if isinstance(response, str) else "", returncode=rc)

    # ── exit ──

    def parse_exit(self, rc: int, log_tail: str) -> ExitVerdict:
        """Classify a finished run from its return code and the tail of `impl.log`.

        The limit wording (`quota._LIMIT_RE`) is searched only in what agy itself said
        about the run — non-JSON lines (stderr is merged into the log) and the last
        `result.error` — and never in `text_delta` agent output, where an agent writing
        rate-limit code would otherwise pause a healthy pool.
        """
        result: Mapping = {}
        for kind, payload in _iter_events(log_tail):
            if kind == "result":
                result = payload
        if not result:
            result = _completion_document(log_tail)
        error = result.get("error")
        error = error if isinstance(error, str) else ""
        haystack = _non_json_lines(log_tail) + ([error] if error else [])

        hit = next((line for line in haystack if quota._LIMIT_RE.search(line)), "")
        if hit:
            return ExitVerdict(
                kind="quota_exhausted",
                reset_at=self._reset_at(),
                evidence=hit[:EVIDENCE_CHARS],
            )
        if rc == 0 and result.get("status") == "SUCCESS":
            return ExitVerdict(kind="ok")
        evidence = error or (haystack[-1] if haystack else "")
        return ExitVerdict(kind="crashed", evidence=evidence.strip()[:EVIDENCE_CHARS])

    def _reset_at(self) -> str:
        """The earliest future reset of an exhausted Gemini pool, else now + 1h."""
        now = self._now()
        sample = self.usage()
        resets = []
        for window in (sample.windows.values() if sample else ()):
            if window.resets_at and window.resets_at > now.timestamp() and (window.used_pct or 0) >= 100:
                resets.append(window.resets_at)
        if resets:
            return _zulu(datetime.fromtimestamp(min(resets), tz=timezone.utc))
        return _zulu(now + FALLBACK_RESET)

    # ── usage ──

    def gemini_home(self) -> Path:
        """Where agy keeps its state (`~/.gemini`), unless the constructor overrides it."""
        return self._gemini_home if self._gemini_home is not None else Path.home() / ".gemini"

    def usage(self, handle: Optional[Handle] = None) -> Optional[Usage]:
        """Account windows from the status-line push file, or one session's context.

        With a handle, the reading comes from that session's own `impl.log` and nothing
        else — the push file describes the operator's interactive session, never an
        implementer's (A5). `None` is "not measurable", never "no pressure".
        """
        if handle is not None:
            return self._session_usage(handle)
        path = self.gemini_home() / STATUSLINE_DUMP
        text = _read_text(path)
        try:
            document = json.loads(text) if text is not None else None
        except ValueError:
            document = None
        if not isinstance(document, Mapping):
            return None

        quota = document.get("quota")
        windows = _pool_windows(quota, POOL_WINDOWS)
        third_party = _pool_windows(quota, THIRD_PARTY_POOL_WINDOWS)

        # `windows` stays the Gemini quota alone. It is this driver's one headline
        # account reading, and folding an independent pool into it would report the
        # 3p quota's pressure against a Gemini route. The split lives in `pools`, keyed
        # by the catalog's `usage_pool_id`, which is what a routed model is bound to.
        pools: dict[str, Mapping[int, WindowUsage]] = {}
        if windows:
            pools[GEMINI_POOL_ID] = windows
        if third_party:
            pools[THIRD_PARTY_POOL_ID] = third_party

        model = document.get("model")
        model_id = model.get("id") if isinstance(model, Mapping) else None
        return Usage(
            windows=windows,
            pools=pools,
            model=model_id if isinstance(model_id, str) else None,
            updated_at=_mtime_zulu(path),
        )

    def attempt_token_usage(self, workspace: Path | str) -> Optional[dict]:
        """Cumulative tokens of the call logged in `workspace`'s `impl.log`, or `None`.

        Taken from the last final record: a stream's `result` or `complete()`'s one
        document. That is the conversation's running total, a resume included (seen on agy
        1.4.12). A step's `usage` is one turn only, so a run killed before its final
        record has no count (G6). `cache_read_tokens` is apart from `input_tokens`.
        `cache_write` is 0, as Codex's is: agy has no separate count for it, and the usage
        report and pace learning total an attempt only when all four counts are numbers.
        `thinking_tokens` is reported beside `output_tokens`, never added: agy's own
        `total_tokens` leaves it out too.
        """
        text = _read_text(Path(workspace) / LOG_FILE)
        if text is None:
            return None
        usage = model = None
        for line in text.splitlines():  # in order: a resume appends to the same log
            found = _completion_document(line).get("usage")
            for kind, payload in _iter_events(line):
                if kind == "init":
                    # A new call has begun, so an earlier total says nothing about it.
                    usage = None
                    if isinstance(payload.get("model"), str):
                        model = payload["model"]
                elif kind == "result":
                    found = payload.get("usage")
            if isinstance(found, Mapping):
                usage = found
        if usage is None:
            return None
        fresh, cached = _as_count(usage.get("input_tokens")), _as_count(usage.get("cache_read_tokens"))
        output = _as_count(usage.get("output_tokens"))
        if fresh is None and cached is None and output is None:
            return None
        return {
            "fresh_input": fresh,
            "cache_read": cached,
            "cache_write": 0,
            "output": output,
            "reasoning_output": _as_count(usage.get("thinking_tokens")),
            "cost_usd": None,
            "models": [model] if model else [],
            "source": "agy_log",
        }

    def _session_usage(self, handle: Handle) -> Optional[Usage]:
        """The last turn's live context from `impl.log`, never `result`'s running sum."""
        path = Path(handle.workspace) / LOG_FILE
        text = _read_text(path)
        if text is None:
            return None
        model = None
        turn: Optional[Mapping] = None
        for kind, payload in _iter_events(text):
            if kind == "init":
                init = payload.get("init") if isinstance(payload.get("init"), Mapping) else payload
                if isinstance(init.get("model"), str):
                    model = init["model"]
            elif kind == "step_update" and isinstance(payload.get("usage"), Mapping):
                turn = payload["usage"]
        if turn is None:
            return None
        fresh = _as_count(turn.get("input_tokens"))
        cached = _as_count(turn.get("cache_read_tokens"))
        total = (fresh or 0) + (cached or 0)
        if total <= 0:
            return None
        return Usage(
            model=model,
            updated_at=_mtime_zulu(path),
            context_total_input_tokens=total,
            session_id=handle.session_id,
            token_split=TokenSplit(
                fresh_input=fresh,
                cache_read=cached,
                cache_write=None,  # agy does not report cache writes
                output=_as_count(turn.get("output_tokens")),
            ),
        )


def _epoch(value: Any) -> Optional[int]:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp())
