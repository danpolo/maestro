"""Locating the reference repo, and a hard firewall against writing to it.

The reference project is a *live* system: an autonomous loop is running against it while
these tests execute. Characterisation tests import its orchestrator module and call its
functions, so any path global left pointing at the real repo turns a test into a write
against production state.

That is not hypothetical. The original harness monkeypatched globals named ``STATE``,
``ORCH_DIR`` and ``USAGE``; the reference module actually names them ``STATE_JSON``,
``USAGE_JSON`` and has no directory global at all. ``monkeypatch.setattr(..., raising=False)``
silently created three unused attributes, ``write_state`` kept resolving the real
``STATE_JSON``, and the test overwrote the live ``state.json``.

So redirection is never spelled out by hand (see the ``sandbox`` fixture, which derives the
list from the module), and on top of that every mutating filesystem call is wrapped for the
whole test session and refuses anything inside the reference repo.

The repo location is deliberately not hardcoded — ``tests/test_purity.py`` forbids
project-identifying content outside the build scaffolding. It comes from
``$MAESTRO_LEGACY_REPO``, else the gitignored ``.legacy_repo`` file at the repo root.
"""
from __future__ import annotations

import builtins
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_LEGACY_REPO_FILE = ROOT / ".legacy_repo"


def legacy_repo() -> Path | None:
    env = os.environ.get("MAESTRO_LEGACY_REPO")
    if env:
        return Path(env).expanduser()
    if _LEGACY_REPO_FILE.is_file():
        text = _LEGACY_REPO_FILE.read_text(encoding="utf-8").strip()
        if text:
            return Path(text).expanduser()
    return None


LEGACY_REPO = legacy_repo()

_WRITE_MODE_CHARS = set("wax+")


class ReferenceRepoWriteAttempt(AssertionError):
    """A test tried to mutate the reference repo. Always a defect in the test."""


def _within(path, root: Path) -> bool:
    try:
        candidate = Path(path)
    except TypeError:
        return False
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = candidate
    return resolved == root or root in resolved.parents


def install_write_firewall(monkeypatch, root: Path | None = LEGACY_REPO) -> None:
    """Make every mutating filesystem call inside `root` raise, for the whole session."""
    if root is None:
        return
    root = root.resolve()

    def deny(path, what):
        if _within(path, root):
            raise ReferenceRepoWriteAttempt(
                f"test attempted to {what} inside the read-only reference repo: {path}"
            )

    # Keyword spellings of a destination argument, for calls made with kwargs.
    _DEST_KWARGS = ("dst", "target", "path", "link_to")

    def wrap(module, name, indices=(0,), what=None):
        """Guard every argument position that names a path the call would mutate.

        Getting these indices right matters more than it looks: `write_state` persists via
        `tmp.rename(STATE_JSON)`, where the *source* is a harmless temp file and the
        *destination* is the live state file. A guard that only inspected argument 0 would
        have waved that call straight through.
        """
        original = getattr(module, name)
        label = what or f"{getattr(module, '__name__', module)}.{name}"

        def guarded(*args, **kwargs):
            for index in indices:
                if len(args) > index:
                    deny(args[index], label)
            for key in _DEST_KWARGS:
                if key in kwargs:
                    deny(kwargs[key], label)
            return original(*args, **kwargs)

        monkeypatch.setattr(module, name, guarded)

    # builtins.open — only write-ish modes.
    real_open = builtins.open

    def guarded_open(file, mode="r", *args, **kwargs):
        if _WRITE_MODE_CHARS & set(str(mode)):
            deny(file, f"open(mode={mode!r})")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)

    # pathlib mutators. arg 0 is `self`; rename/replace also mutate arg 1, the destination.
    for name in (
        "write_text", "write_bytes", "mkdir", "touch",
        "unlink", "rmdir", "symlink_to", "hardlink_to", "chmod",
    ):
        if hasattr(Path, name):
            wrap(Path, name, (0,), f"Path.{name}()")

    for name in ("rename", "replace"):
        if hasattr(Path, name):
            wrap(Path, name, (0, 1), f"Path.{name}()")

    # Path.open with a write mode.
    real_path_open = Path.open

    def guarded_path_open(self, mode="r", *args, **kwargs):
        if _WRITE_MODE_CHARS & set(str(mode)):
            deny(self, f"Path.open(mode={mode!r})")
        return real_path_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_path_open)

    for name in ("remove", "unlink", "mkdir", "makedirs", "rmdir", "truncate"):
        if hasattr(os, name):
            wrap(os, name, (0,), f"os.{name}()")

    # Two-path calls: source and destination are both mutated (or created).
    for name in ("rename", "replace", "renames", "symlink", "link"):
        if hasattr(os, name):
            wrap(os, name, (0, 1), f"os.{name}()")

    wrap(shutil, "rmtree", (0,), "shutil.rmtree()")
    # move removes the source, so both ends matter. copy* only writes the destination —
    # copying *out of* the reference repo into a sandbox is a legitimate test operation.
    wrap(shutil, "move", (0, 1), "shutil.move()")
    for name in ("copy", "copy2", "copyfile", "copytree"):
        if hasattr(shutil, name):
            wrap(shutil, name, (1,), f"shutil.{name}()")

    # A subprocess run *inside* the repo can mutate it (git, tmux, scripts).
    for name in ("run", "call", "check_call", "check_output", "Popen"):
        original = getattr(subprocess, name)

        def guarded_subprocess(*args, _original=original, _name=name, **kwargs):
            cwd = kwargs.get("cwd")
            if cwd is not None:
                deny(cwd, f"subprocess.{_name}(cwd=...)")
            return _original(*args, **kwargs)

        monkeypatch.setattr(subprocess, name, guarded_subprocess)
