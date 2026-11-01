"""Shared, fail-closed authorization for non-Claude native tool adapters.

This accepts a bounded shell grammar: literal words, quoting, pipelines, sequential
lists and file redirections. Expansions, background jobs, shell programs, descriptor
redirection, assignments and comments are refused. It never uses the approximate
proposal-screening ``permissions.split_command`` to authorize execution.

An allowed interpreter can still construct a path in its program. As with Claude's
lists, this is command authorization, not a read sandbox (docs/W9_SAFETY.md).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shlex
from typing import Sequence

from maestro import permissions


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class _Token:
    value: str
    operator: bool = False


def _tokens(command: str) -> list[_Token]:
    """Lex a conservative shell subset, retaining quoted operators as ordinary data."""
    if not isinstance(command, str) or not command.strip() or len(command) > 131072:
        raise ValueError("empty or oversized command")
    out: list[_Token] = []
    word: list[str] = []
    quote = ""
    escaped = False

    def flush() -> None:
        if word:
            parsed = shlex.split("".join(word), comments=False, posix=True)
            if len(parsed) != 1:
                raise ValueError("invalid shell word")
            out.append(_Token(parsed[0]))
            word.clear()

    i = 0
    while i < len(command):
        char = command[i]
        if char == "\x00":
            raise ValueError("NUL in command")
        if escaped:
            if char == "\n":
                raise ValueError("line continuations are unsupported")
            word.append(char)
            escaped = False
        elif char == "\\" and quote != "'":
            word.append(char)
            escaped = True
        elif quote:
            if char == quote:
                quote = ""
            elif quote == '"' and char in "$`":
                raise ValueError("shell expansion is unsupported")
            word.append(char)
        elif char in "'\"":
            quote = char
            word.append(char)
        elif char in "$`(){}#*?[]":
            raise ValueError("shell expansion or control syntax is unsupported")
        elif char in ";&|<>\n":
            flush()
            operator = char
            if command[i:i + 2] in ("&&", "||", ">>", "<<", ">&", "<&", "<>", ">|"):
                operator = command[i:i + 2]
                i += 1
            if operator not in (";", "\n", "&&", "||", "|", ">", ">>", "<"):
                raise ValueError("background, heredoc or descriptor redirection is unsupported")
            # A descriptor before a redirect (2>file) is not a command argument.
            if char in "<>" and i and command[i - 1].isdigit():
                raise ValueError("descriptor redirection is unsupported")
            out.append(_Token(operator, True))
        elif char in " \t":
            flush()
        elif char.isspace():
            raise ValueError("non-shell whitespace is unsupported")
        else:
            word.append(char)
        i += 1
    if quote or escaped:
        raise ValueError("unterminated shell quoting")
    flush()
    return out


@dataclass(frozen=True)
class ToolPolicy:
    allow: Sequence[str]
    write_roots: Sequence[Path] = ()
    deny: Sequence[str] = permissions.NEVER_ALLOW

    def command(self, command: str, *, cwd: Path) -> Decision:
        try:
            tokens = _tokens(command)
            return self._commands(tokens, Path(cwd).resolve())
        except (ValueError, OSError, RuntimeError) as exc:
            return Decision(False, f"unsupported shell command: {exc}")

    def _commands(self, tokens: list[_Token], cwd: Path) -> Decision:
        words: list[str] = []
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token.operator and token.value in (">", ">>", "<"):
                i += 1
                if i == len(tokens) or tokens[i].operator:
                    return Decision(False, "redirection needs a literal path")
                path = Path(tokens[i].value).expanduser()
                path = path if path.is_absolute() else cwd / path
                decision = self.file("Read" if token.value == "<" else "Edit", path)
                if not decision.allowed:
                    return decision
            elif token.operator:
                decision = self._simple(words, cwd)
                if not decision.allowed:
                    return decision
                words = []
            else:
                words.append(token.value)
            i += 1
        return self._simple(words, cwd)

    def _simple(self, words: list[str], cwd: Path) -> Decision:
        if (not words or not words[0] or any(char.isspace() for char in words[0])
                or re.match(r"^[A-Za-z_]\w*=", words[0])):
            return Decision(False, "a literal executable is required; assignments are unsupported")
        subject = " ".join(words)
        basename_subject = " ".join([Path(words[0]).name, *words[1:]])
        if any(permissions._matches_one(rule, "Bash", candidate)
               for rule in self.deny for candidate in (subject, basename_subject)):
            return Decision(False, "command is on the never-allow list")
        if not any(permissions._matches_one(rule, "Bash", subject) for rule in self.allow):
            return Decision(False, f"command family is not allowed: {words[0]}")
        for word in words:
            # Also screen resolved literal paths: quote concatenation and symlink aliases
            # must not bypass Read denies. This does not parse an interpreter's program.
            raw = Path(word).expanduser()
            target = raw if raw.is_absolute() else cwd / raw
            decision = self._path_denial("Read", target)
            if decision is not None:
                return decision
        return Decision(True, "every command family and literal path is allowed")

    def _path_denial(self, tool: str, path: Path) -> Decision | None:
        raw = Path(path).expanduser()
        resolved = raw.resolve()
        tools = (tool, "Read") if tool in ("Glob", "Grep") else (tool,)
        if any(permissions._matches_one(rule, denied_tool, str(candidate))
               for rule in self.deny for denied_tool in tools for candidate in (raw, resolved)):
            return Decision(False, "literal path is on the never-allow list")
        return None

    def file(self, tool: str, path: Path) -> Decision:
        tool = "Edit" if tool in ("Write", "NotebookEdit") else tool
        if tool not in ("Read", "Edit", "Glob", "Grep"):
            return Decision(False, "unsupported file operation")
        try:
            raw = Path(path).expanduser()
            if not raw.is_absolute():
                return Decision(False, "file operation requires an absolute path")
            denied = self._path_denial(tool, raw)
            if denied is not None:
                return denied
            resolved = raw.resolve()
            if tool == "Edit":
                if any(resolved.is_relative_to(Path(root).resolve()) for root in self.write_roots):
                    return Decision(True, "inside the assigned write roots")
                return Decision(False, "outside the assigned write roots")
            if any(permissions._matches_one(rule, tool, str(resolved)) for rule in self.allow):
                return Decision(True, "file operation is allowed")
            return Decision(False, "file operation is not allowed")
        except (ValueError, OSError, RuntimeError) as exc:
            return Decision(False, f"invalid path: {exc}")
