"""`ModelAgentRuntime`: the bounded model/tool supervisor a bare endpoint runs under (P7).

A CLI backend brings its own agent loop, its own tool set and its own idea of what a
sandbox is; maestro treats it as opaque (`[INV-03]`) and supervises it from outside. A raw
HTTP endpoint brings none of that. Something has to *be* the loop — decide when a turn is
over, decide which tools exist, decide when enough is enough — and `§5` says that
something is maestro, under four explicit bounds:

1. **A closed tool set.** `read_file`, `write_file`, `run_test`, `grep`. A model asking
   for anything else gets a refusal handed back as a tool result, not an exception: the
   loop is a supervisor, and a supervisor that crashes when supervision is needed is not
   one. The refusal is recorded, so "the model kept reaching for `bash`" is visible
   afterwards rather than merely absent.
2. **A confined filesystem.** Every path a tool touches is resolved against the task
   worktree and checked by `maestro.confinement.CommitBoundary` — the same predicate the
   sandbox mounts are built from, so the in-process check and the kernel-level one cannot
   disagree about what "inside the worktree" means. A write outside it is not a refused
   tool call, it is a failed run: a model trying to leave its worktree has stopped doing
   the task.
3. **Checkpoint after every tool.** `§5.2`. Not at the end and not every N turns: the
   crash this protects against happens *during* a turn, and a checkpoint written after
   the fact records a state the process never actually reached.
4. **Hard ceilings.** 25 tool turns by default and a wall clock. Both are budgets, not
   errors — hitting one ends the run with a stated stop reason and whatever work is on
   disk, because a worktree with half a change in it is the truthful outcome and
   pretending otherwise is what makes an unattended loop expensive.

`run_test` deserves its own sentence. It does **not** take a command from the model. The
command is configured when the runtime is built, from the task's own verification spec,
and the tool's only argument is which of the configured commands to run. A tool that
executed model-supplied shell would make every other bound in this file decorative.
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from maestro.backends.model_api import (
    Message,
    ModelAPI,
    ModelAPIError,
    ModelTurn,
    ToolCall,
    ToolSchema,
    TransportError,
)
from maestro.confinement import CommitBoundary

__all__ = [
    "ALLOWED_TOOLS",
    "TOOL_SCHEMAS",
    "DEFAULT_MAX_TURNS",
    "DEFAULT_WALL_CLOCK_SEC",
    "ToolResult",
    "ToolBox",
    "Budget",
    "Checkpoint",
    "CheckpointWriter",
    "RuntimeOutcome",
    "ModelAgentRuntime",
]

#: `§5.1`, verbatim and closed. Adding a tool means adding a method on `ToolBox` *and* a
#: schema here; a name in one and not the other is refused at construction, so a tool
#: cannot be offered to a model that nothing can execute, nor executed without having
#: been described.
ALLOWED_TOOLS: tuple[str, ...] = ("read_file", "write_file", "run_test", "grep")

DEFAULT_MAX_TURNS = 25
DEFAULT_WALL_CLOCK_SEC = 1800

#: Consecutive turns with undecodable tool-call arguments that end the run as
#: `malformed_tool_calls` (P12B F1). One bad turn is handed back to the model as a refused
#: call; three in a row is a model that cannot speak the tool protocol.
MALFORMED_TURN_LIMIT = 3

#: Bytes a single tool result may hand back to the model. A `grep` over a large tree or a
#: read of a generated file can otherwise fill the context window in one turn, which is
#: the cheapest way to destroy a free-tier run.
MAX_TOOL_RESULT_BYTES = 32_000

TOOL_SCHEMAS: tuple[ToolSchema, ...] = (
    ToolSchema(
        name="read_file",
        description=(
            "Read UTF-8 text at a worktree-relative path. Output may be truncated; "
            "missing files return an error, and paths outside the task boundary stop the run."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the worktree root."}
            },
            "required": ["path"],
        },
    ),
    ToolSchema(
        name="write_file",
        description=(
            "Create or fully replace a UTF-8 file with content. Paths are relative to the "
            "task worktree for writers, or the report workspace for read-only nodes; "
            "parent directories are created. Paths outside that boundary stop the run."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the worktree root."},
                "content": {"type": "string", "description": "The file's complete new content."},
            },
            "required": ["path", "content"],
        },
    ),
    ToolSchema(
        name="run_test",
        description=(
            "Run a named verification command configured for this task. The command itself "
            "is fixed by the project; unknown names return the available names. Returns "
            "the exit code and possibly truncated output."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Which configured command to run."}
            },
            "required": ["name"],
        },
    ),
    ToolSchema(
        name="grep",
        description=(
            "Search a worktree-relative path recursively for a literal, case-sensitive "
            "string. Returns matching lines or no matches; output may be truncated. "
            "An empty pattern returns an error; paths outside the task boundary stop the run."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Non-empty literal text; not a regex."},
                "path": {"type": "string", "description": "Subdirectory to search. Optional."},
            },
            "required": ["pattern"],
        },
    ),
)

assert {schema.name for schema in TOOL_SCHEMAS} == set(ALLOWED_TOOLS), (
    "TOOL_SCHEMAS and ALLOWED_TOOLS must describe the same closed tool set"
)


class RuntimeError_(RuntimeError):
    """Base for the runtime's own refusals."""


class ConfinementBreach(RuntimeError_):
    """A tool was asked to touch a path outside the boundary. Fails the run (`§4.2`)."""


# ── tools ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ToolResult:
    """What one dispatched tool produced, as the model will see it."""

    name: str
    ok: bool
    content: str
    call_id: str = ""
    #: Set when the tool changed the worktree — the checkpoint records the diff digest.
    mutated: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "call_id": self.call_id,
            "mutated": self.mutated,
            "content": self.content[:2000],
        }


def _clip(text: str) -> str:
    if len(text) <= MAX_TOOL_RESULT_BYTES:
        return text
    return text[:MAX_TOOL_RESULT_BYTES] + f"\n… truncated at {MAX_TOOL_RESULT_BYTES} bytes"


@dataclass
class ToolBox:
    """The four tools, confined to one worktree.

    `test_commands` is a *name -> argv* map supplied by the caller. The model picks a
    name; it never supplies a command, and there is no branch in this file where it
    could. `runner` is injected so the suite can assert what would have been executed
    without executing it.
    """

    boundary: CommitBoundary
    test_commands: Mapping[str, Sequence[str]] = field(default_factory=dict)
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
    test_timeout_sec: int = 900
    #: Field-notes F1: set for a reader (reviewer, planner, diagnoser). `write_file` then
    #: writes only here — its verdict — and refuses the worktree it reads; `None` is a
    #: writer, whose writes stay inside `boundary`.
    writes_to: Path | None = None
    #: Entries of `writes_to` a reader may not write, nor their `.tmp` siblings, nor
    #: anything beneath them: the files its caller reports through.
    reserved: frozenset[str] = frozenset()
    #: Every refused call, for the outcome. A model that spent ten turns asking for a
    #: shell should be legible afterwards.
    refusals: list[str] = field(default_factory=list)

    @property
    def worktree(self) -> Path:
        return self.boundary.worktree

    def _resolve(self, raw: object) -> Path:
        """A model-supplied path, resolved inside the worktree or refused (`§4.2`)."""
        text = str(raw or "").strip()
        if not text:
            raise ConfinementBreach("tool called with an empty path")
        candidate = Path(text)
        candidate = candidate if candidate.is_absolute() else (self.worktree / candidate)
        ok, reason = self.boundary.permits(candidate)
        if not ok:
            raise ConfinementBreach(reason)
        return candidate

    def dispatch(self, call: ToolCall) -> ToolResult:
        """Run one call, or refuse it. Never raises for an ordinary tool failure."""
        if call.name not in ALLOWED_TOOLS:
            self.refusals.append(call.name)
            return ToolResult(
                name=call.name,
                ok=False,
                call_id=call.call_id,
                content=(
                    f"tool {call.name!r} is not available. This runtime offers exactly "
                    f"{list(ALLOWED_TOOLS)} and nothing else."
                ),
            )
        method = getattr(self, f"_{call.name}")
        try:
            return method(call)
        except ConfinementBreach:
            raise
        except Exception as exc:                      # a tool's own failure is an answer
            return ToolResult(
                name=call.name, ok=False, call_id=call.call_id, content=f"{type(exc).__name__}: {exc}"
            )

    def _read_file(self, call: ToolCall) -> ToolResult:
        path = self._resolve(call.arguments.get("path"))
        if not path.is_file():
            return ToolResult(call.name, False, f"no such file: {call.arguments.get('path')}",
                              call_id=call.call_id)
        text = path.read_text(encoding="utf-8", errors="replace")
        return ToolResult(call.name, True, _clip(text), call_id=call.call_id)

    def _resolve_write(self, raw: object) -> Path:
        """Where `write_file` may write: the boundary for a writer, the workspace for a reader."""
        if self.writes_to is None:
            return self._resolve(raw)
        text = str(raw or "").strip()
        if not text:
            raise ConfinementBreach("tool called with an empty path")
        root = Path(self.writes_to).resolve()
        candidate = Path(text)
        if ".." in candidate.parts:
            raise ConfinementBreach(f"path escapes its sandbox: {candidate}")
        candidate = (candidate if candidate.is_absolute() else root / candidate).resolve()
        if candidate != root and root not in candidate.parents:
            raise ConfinementBreach(
                f"a read-only node may write only its workspace {root}, not {candidate}"
            )
        entry = candidate.relative_to(root).parts[:1]
        if entry and entry[0].removesuffix(".tmp") in self.reserved:
            raise ConfinementBreach(
                f"{entry[0]} is reserved for the worker that reports this node"
            )
        return candidate

    def _write_file(self, call: ToolCall) -> ToolResult:
        path = self._resolve_write(call.arguments.get("path"))
        content = call.arguments.get("content")
        if not isinstance(content, str):
            return ToolResult(call.name, False, "write_file needs a string `content`",
                              call_id=call.call_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if self.writes_to is not None:
            return ToolResult(call.name, True, f"wrote {len(content)} bytes to {path}",
                              call_id=call.call_id)
        return ToolResult(
            call.name, True, f"wrote {len(content)} bytes to {path.relative_to(self.worktree)}",
            call_id=call.call_id, mutated=True,
        )

    def _run_test(self, call: ToolCall) -> ToolResult:
        name = str(call.arguments.get("name") or "").strip()
        argv = self.test_commands.get(name)
        if argv is None:
            return ToolResult(
                call.name, False,
                (
                    f"no verification command named {name!r}. Configured for this task: "
                    f"{sorted(self.test_commands)}. The command itself is fixed by the "
                    f"project and cannot be supplied here."
                ),
                call_id=call.call_id,
            )
        completed = self.runner(
            list(argv), cwd=str(self.worktree), capture_output=True, text=True,
            timeout=self.test_timeout_sec,
        )
        output = f"{completed.stdout or ''}{completed.stderr or ''}"
        return ToolResult(
            call.name, completed.returncode == 0,
            _clip(f"exit {completed.returncode}\n{output}"), call_id=call.call_id,
        )

    def _grep(self, call: ToolCall) -> ToolResult:
        pattern = str(call.arguments.get("pattern") or "")
        if not pattern:
            return ToolResult(call.name, False, "grep needs a pattern", call_id=call.call_id)
        where = self._resolve(call.arguments.get("path") or ".")
        completed = self.runner(
            ["grep", "-rnF", "--", pattern, str(where)],
            cwd=str(self.worktree), capture_output=True, text=True, timeout=120,
        )
        text = completed.stdout or ""
        if not text.strip():
            return ToolResult(call.name, True, "no matches", call_id=call.call_id)
        return ToolResult(call.name, True, _clip(text), call_id=call.call_id)


# ── budget & checkpoints ─────────────────────────────────────────────────────────────


@dataclass
class Budget:
    """`§5.3`: the two ceilings, with an injectable clock.

    `clock` rather than `time.monotonic` inline so a test can assert what happens at the
    deadline without sleeping to it — a suite that waits out a wall clock is one nobody
    runs.
    """

    max_turns: int = DEFAULT_MAX_TURNS
    wall_clock_sec: int = DEFAULT_WALL_CLOCK_SEC
    clock: Callable[[], float] = time.monotonic
    started_at: Optional[float] = None

    def start(self) -> "Budget":
        self.started_at = self.clock()
        return self

    @property
    def elapsed(self) -> float:
        return 0.0 if self.started_at is None else max(0.0, self.clock() - self.started_at)

    def exceeded(self, turns: int) -> str:
        """The stop reason a budget has produced, or `""` while both still permit work."""
        if turns >= self.max_turns:
            return "turn_budget_exhausted"
        if self.elapsed >= self.wall_clock_sec:
            return "deadline_exceeded"
        return ""


@dataclass(frozen=True)
class Checkpoint:
    """One turn's durable record. Written *after* the tool ran, never before."""

    turn: int
    tool: str
    ok: bool
    path: Path
    worktree_digest: str

    def to_dict(self) -> dict:
        return {
            "turn": self.turn,
            "tool": self.tool,
            "ok": self.ok,
            "path": str(self.path),
            "worktree_digest": self.worktree_digest,
        }


@dataclass
class CheckpointWriter:
    """Turn checkpoints under `.orchestrator/artifacts/` (`§5.2`).

    A plain directory of numbered JSON documents rather than `ArtifactStore` entries: a
    checkpoint is written by the *worker*, and `[INV-01]` reserves the control database —
    which `ArtifactStore.commit_ref` writes — to the controller. The controller ingests
    these into the artifact store when it reaps the attempt, exactly as it ingests
    evidence rows; a worker that wrote them directly would be the single-writer violation
    the whole P1 design exists to prevent.
    """

    root: Path
    attempt_id: str = ""

    @property
    def directory(self) -> Path:
        base = Path(self.root)
        return base / self.attempt_id if self.attempt_id else base

    def write(self, *, turn: int, tool: str, ok: bool, body: Mapping[str, Any],
              worktree_digest: str) -> Checkpoint:
        directory = self.directory
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"turn-{turn:04d}.json"
        document = {
            "turn": turn,
            "tool": tool,
            "ok": ok,
            "worktree_digest": worktree_digest,
            **dict(body),
        }
        path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
        return Checkpoint(turn=turn, tool=tool, ok=ok, path=path, worktree_digest=worktree_digest)


def worktree_digest(worktree: Path, *, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str:
    """A digest of the worktree's current diff — "what has changed so far", in one line.

    `git status --porcelain` rather than a full tree hash: the checkpoint's job is to let
    a recovering controller tell whether a turn changed anything, cheaply, after every
    single tool call. Best-effort by construction — a worktree that is not a git
    repository yet (a scratch candidate directory being prepared) is not an error here.
    """
    try:
        completed = runner(
            ["git", "status", "--porcelain"], cwd=str(worktree),
            capture_output=True, text=True, timeout=60,
        )
    except Exception:
        return ""
    if completed.returncode != 0:
        return ""
    import hashlib

    return hashlib.sha256((completed.stdout or "").encode("utf-8")).hexdigest()[:16]


# ── the loop ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RuntimeOutcome:
    """How one supervised run ended. `status` is the node's, `stop_reason` is the loop's."""

    status: str
    stop_reason: str
    turns: int
    text: str = ""
    checkpoints: tuple[Checkpoint, ...] = ()
    refused_tools: tuple[str, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    detail: str = ""
    #: What a refusing endpoint said about when to try again (P12B F1). `None`: nothing.
    retry_after_sec: Optional[int] = None
    reset_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "stop_reason": self.stop_reason,
            "turns": self.turns,
            "text": self.text,
            "checkpoints": [c.to_dict() for c in self.checkpoints],
            "refused_tools": list(self.refused_tools),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "detail": self.detail,
            "retry_after_sec": self.retry_after_sec,
            "reset_at": self.reset_at,
        }


class ModelAgentRuntime:
    """One supervised model session against one worktree.

    The loop is deliberately small enough to read in one sitting, because every branch in
    it is a bound: ask, check the budget, dispatch the tools, checkpoint, repeat. A turn
    with no tool calls is the model saying it is finished, and is the only path to
    `stop_reason='completed'` — except the very first turn, where saying nothing at all is
    `empty_output`, not a finished task (P12B F1).

    A failed request stops the run with the P12B failure kind as `stop_reason`
    (`rate_limited`, `auth_failed`, …); an unclassified one keeps `endpoint_unavailable`.
    """

    def __init__(
        self,
        api: ModelAPI,
        toolbox: ToolBox,
        checkpoints: CheckpointWriter,
        *,
        budget: Optional[Budget] = None,
        system_prompt: str = "",
        digest: Callable[[Path], str] = worktree_digest,
    ):
        self.api = api
        self.toolbox = toolbox
        self.checkpoints = checkpoints
        self.budget = budget or Budget()
        self.system_prompt = system_prompt
        self._digest = digest

    def run(self, brief: str) -> RuntimeOutcome:
        messages: list[Message] = []
        if self.system_prompt:
            messages.append(Message(role="system", content=self.system_prompt))
        messages.append(Message(role="user", content=brief))

        self.budget.start()
        written: list[Checkpoint] = []
        turns = 0
        input_tokens = output_tokens = 0
        last_text = ""
        answered = False
        malformed_turns = 0

        while True:
            stop = self.budget.exceeded(turns)
            if stop:
                return self._outcome(
                    "uncertain", stop, turns, last_text, written,
                    detail=(
                        f"the loop stopped at its {'turn' if stop.startswith('turn') else 'time'} "
                        f"ceiling with work possibly unfinished in the worktree"
                    ),
                )
            try:
                turn: ModelTurn = self.api.complete(messages, tools=TOOL_SCHEMAS)
            except TransportError as exc:
                return self._outcome(
                    "failed", exc.failure_kind, turns, last_text, written,
                    input_tokens=input_tokens, output_tokens=output_tokens,
                    detail=str(exc), retry_after_sec=exc.retry_after_sec,
                    reset_at=exc.reset_at,
                )
            except ModelAPIError as exc:
                return self._outcome(
                    "failed", "endpoint_unavailable", turns, last_text, written, detail=str(exc)
                )

            input_tokens += turn.input_tokens
            output_tokens += turn.output_tokens
            last_text = turn.text or last_text
            if turn.finish_reason in ("length", "max_tokens", "MAX_TOKENS"):
                return self._outcome(
                    "uncertain", "output_token_limit", turns, last_text, written,
                    input_tokens=input_tokens, output_tokens=output_tokens,
                    detail="the provider truncated its response at the output token limit",
                )
            messages.append(
                Message(role="assistant", content=turn.text, tool_calls=turn.tool_calls)
            )

            if not turn.tool_calls:
                if not answered and not turn.text.strip():
                    return self._outcome(
                        "failed", "empty_output", turns, last_text, written,
                        input_tokens=input_tokens, output_tokens=output_tokens,
                        detail="the first turn had no text and no tool calls",
                    )
                return self._outcome(
                    "succeeded", "completed", turns, last_text, written,
                    input_tokens=input_tokens, output_tokens=output_tokens,
                )
            answered = True
            malformed_turns = (
                malformed_turns + 1 if any(c.malformed for c in turn.tool_calls) else 0
            )

            for call in turn.tool_calls:
                turns += 1
                try:
                    if call.malformed:
                        # Refused, not dispatched: its empty arguments would otherwise
                        # read as an empty path and end the run as a confinement breach.
                        result = ToolResult(
                            name=call.name, ok=False, call_id=call.call_id,
                            content="the tool-call arguments did not decode as a JSON object",
                        )
                    else:
                        result = self.toolbox.dispatch(call)
                except ConfinementBreach as breach:
                    written.append(
                        self.checkpoints.write(
                            turn=turns, tool=call.name, ok=False,
                            body={"refused": str(breach), "arguments": dict(call.arguments)},
                            worktree_digest=self._digest(self.toolbox.worktree),
                        )
                    )
                    return self._outcome(
                        "failed", "confinement_breach", turns, last_text, written,
                        input_tokens=input_tokens, output_tokens=output_tokens,
                        detail=str(breach),
                    )
                # `§5.2`: after every tool execution, without exception.
                written.append(
                    self.checkpoints.write(
                        turn=turns, tool=call.name, ok=result.ok,
                        body={"result": result.to_dict(), "arguments": dict(call.arguments)},
                        worktree_digest=self._digest(self.toolbox.worktree),
                    )
                )
                messages.append(
                    Message(
                        role="tool", content=result.content,
                        call_id=call.call_id, name=call.name,
                    )
                )
            if malformed_turns >= MALFORMED_TURN_LIMIT:
                return self._outcome(
                    "failed", "malformed_tool_calls", turns, last_text, written,
                    input_tokens=input_tokens, output_tokens=output_tokens,
                    detail=(
                        f"{malformed_turns} consecutive turns with tool-call arguments "
                        f"that did not decode"
                    ),
                )

    def _outcome(
        self, status: str, stop_reason: str, turns: int, text: str,
        checkpoints: Sequence[Checkpoint], *, input_tokens: int = 0, output_tokens: int = 0,
        detail: str = "", retry_after_sec: Optional[int] = None, reset_at: Optional[str] = None,
    ) -> RuntimeOutcome:
        return RuntimeOutcome(
            status=status,
            stop_reason=stop_reason,
            turns=turns,
            text=text,
            checkpoints=tuple(checkpoints),
            refused_tools=tuple(self.toolbox.refusals),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            detail=detail,
            retry_after_sec=retry_after_sec,
            reset_at=reset_at,
        )
