"""Which maestro checkout a process actually runs.

DESIGN.md §9 says `~/.maestro/current` holds "the absolute path of the worktree a project
should import maestro through right now". Until this module existed, nothing read it:
`adopt()` wrote the file and no consumer ever consulted it, so the adopted version was a
record rather than a deployment. What a project ran was whatever its venv resolved
`import maestro` to — for an editable install, the working tree of the maestro repository,
including uncommitted edits, with no gate of any kind between a saved file and a live
production loop.

`reexec_into_adopted_version` closes that. It runs before anything else in `main()`, and if
this process is not already the adopted checkout it replaces itself with one that is.

**Why `PYTHONPATH` is enough.** An editable install adds an `_EditableFinder` to
`sys.meta_path`, but *appends* it — after `PathFinder`, which is the finder that reads
`sys.path`. So a `PYTHONPATH` entry is consulted first and the editable mapping becomes the
fallback it should always have been. No symlink, no reinstall, no shim package: verified
directly against a real editable-installed project venv, from a neutral working directory
(`python -c` puts the cwd on `sys.path` and will mask this if you test from the repo).

**Nothing here imports from `maestro`.** `main()` calls into this module before any `cmd_*`
has set `$MAESTRO_REPO`, and importing `maestro.state` (or anything that reaches it) at that
moment would bind `Paths.from_env()` to the wrong root for the life of the process — the
hazard `maestro/cli.py`'s module docstring documents. Standard library only, deliberately.

**Fail-open, on purpose.** A machine that has never self-updated, a pruned worktree, an
unreadable pointer, a failed `exec` — every one of them leaves the process running in place
rather than refusing to start. A maestro that will not run is worse than a maestro running
the wrong checkout, and the wrong checkout is not silent: `maestro doctor`'s `adopted_code`
check reports it, which is the visibility half of this fix.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

#: Overrides the self-update home, `~/.maestro`. Owned here rather than in
#: `maestro.selfupdate` (which re-exports it) so that resolving the adopted checkout pulls
#: in no maestro module at all — see this module's docstring.
ENV_VAR = "MAESTRO_HOME"

#: Set on the re-executed child, and honoured as an opt-out on the way in. Two jobs, one
#: variable, and they are the same job seen from either end: *this process is already the
#: checkout it is meant to be, do not redirect it again.*
#:
#: As a guard it makes the redirect strictly single-shot, so a mispointed `current` cannot
#: produce an `exec` loop. As an opt-out it is what pins "the code under test" in place —
#: `selfupdate._self_test_env` sets it so a candidate's suite cannot re-exec into the
#: *deployed* version and grade that instead of itself, and `tests/conftest.py` sets it
#: session-wide for the same reason. It is also the developer's escape hatch: export it and
#: `maestro` runs the checkout you invoked, which is usually what you want while editing one.
SENTINEL = "MAESTRO_BOOTSTRAPPED"


def home() -> Path:
    """`~/.maestro`, or `$MAESTRO_HOME`."""
    raw = os.environ.get(ENV_VAR)
    root = Path(raw).expanduser() if raw else (Path.home() / ".maestro")
    return root.resolve()


def current_file() -> Path:
    """The pointer `adopt()` writes: a plain text file holding one absolute path."""
    return home() / "current"


def running_root() -> Path:
    """The checkout this process imported `maestro` from — `bootstrap.py`'s grandparent."""
    return Path(__file__).resolve().parent.parent


def adopted_root() -> Path | None:
    """The adopted checkout, or `None` when there isn't a usable one.

    `None` covers every degenerate case together, because every one of them has the same
    correct response — run in place: no pointer file (this machine has never self-updated),
    an empty or unreadable one, or a path that no longer exists or is not a maestro checkout
    (a pruned or half-written worktree). Never raises; a resolver that can fail to start the
    CLI would be worse than the problem it solves.
    """
    try:
        raw = current_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        root = Path(raw).resolve()
    except (OSError, ValueError):
        return None
    # A directory alone is not enough: `current` naming something that is not a checkout
    # would send `-m maestro.cli` into an ImportError instead of back to a working maestro.
    if not (root / "maestro" / "__init__.py").is_file():
        return None
    return root


def reexec_into_adopted_version(argv: list | None = None) -> None:
    """Replace this process with the adopted checkout, or return and run in place.

    Returns — meaning "carry on here" — when the sentinel is set, when there is no usable
    adopted checkout, or when this process already *is* it. That last condition is what
    terminates the redirect: the child re-enters this function, finds `running_root() ==
    adopted_root()`, and proceeds. The sentinel makes it single-shot regardless.

    Re-execs as `-m maestro.cli` rather than re-running `sys.argv[0]`. The console script and
    `python -m maestro.cli` are both supported entry points, but only the module form is
    resolved through `sys.path` — repeating `sys.argv[0]` would re-run the *file* the working
    tree ships and defeat the whole redirect. `build_parser` sets `prog="maestro"` explicitly,
    so usage text is unchanged by the switch.
    """
    if os.environ.get(SENTINEL):
        return
    root = adopted_root()
    if root is None or root == running_root():
        return

    env = dict(os.environ)
    env[SENTINEL] = "1"
    entry = str(root)
    parts = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p and p != entry]
    env["PYTHONPATH"] = os.pathsep.join([entry, *parts])

    args = list(sys.argv[1:] if argv is None else argv)
    try:
        os.execve(sys.executable, [sys.executable, "-m", "maestro.cli", *args], env)
    except OSError as exc:  # pragma: no cover — an exec that cannot even be attempted
        # Loud, then carry on in place. Silence here would recreate the exact failure this
        # module exists to end: a process running code nobody believes it is running.
        print(f"[maestro] could not re-exec into {root}: {exc}; running {running_root()}",
              file=sys.stderr)
