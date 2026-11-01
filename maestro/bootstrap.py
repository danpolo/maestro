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

**Per project, not per machine (`[INV-09]`, `[INV-A09]`).** `~/.maestro/current` is one
pointer for a whole host, so adopting a new version while orchestrating project A used to
move project B onto it too — silently, at B's next poll, mid-roadmap. A project therefore
pins its own runtime in `<repo>/.orchestrator/current`, written by `selfupdate.adopt` for
whichever project the adopting process was operating, and that pin wins here over the
machine-global pointer. Resolving it means knowing the project root *before* argparse has
run and before any maestro module is imported, which is why `repo_from_argv` re-reads
`--repo`/`$MAESTRO_REPO` out of `argv` with the standard library instead of calling
`maestro.paths` or `maestro.cli._resolve_repo`.

The cwd fallback those two use is deliberately **not** repeated: a pin is a claim a project
makes about itself, and inferring one from the working directory would redirect a `maestro`
invoked from inside project A while it is operating project B.

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

#: `maestro.paths.ENV_VAR`, re-spelled rather than imported — importing `maestro.paths`
#: here would bind that module's `Paths.from_env()` before any `cmd_*` has set the
#: variable, which is the exact hazard this module's docstring exists to avoid.
#: `tests/test_bootstrap.py` pins the two spellings together so they cannot drift.
PROJECT_ENV_VAR = "MAESTRO_REPO"

#: Where a project records the checkout it runs (`[INV-09]`). Beside `state.json` and the
#: control database, because it is the same kind of fact: per-project runtime state, not
#: configuration, and it must not be committed.
PROJECT_PIN_RELPATH = (".orchestrator", "current")

#: Set on the re-executed child to the checkout the operator actually invoked. Without it the
#: child cannot tell that it was redirected at all, and a command whose output depends on the
#: code's age (`maestro init` scaffolds from its own templates) silently produces a stale
#: result — observed on the itv onboarding, which got the machine pin's old scaffold.
INVOKED_FROM_ENV = "MAESTRO_INVOKED_FROM"


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


def pinned_module_argv(python: str | os.PathLike, module: str) -> list[str]:
    """The argv prefix that runs `module` from the checkout this process is running.

    Use it for any `-m maestro.x` child that does not inherit this process's environment,
    which is every child started with `tmux new-window` (see `maestro/pinned.py`). The
    launcher's own `running_root()` is the pin: a child runs the checkout of the process
    that started it.
    """
    return [str(python), str(running_root() / "maestro" / "pinned.py"), module]


def redirected_from() -> Path | None:
    """The checkout the operator invoked, when this process was re-exec'd away from it."""
    raw = os.environ.get(INVOKED_FROM_ENV)
    if not raw or Path(raw).resolve() == running_root():
        return None
    return Path(raw).resolve()


def repo_from_argv(argv: list | None = None) -> Path | None:
    """The project root this invocation is about, read the way `main()` will read it.

    `--repo <path>` / `--repo=<path>` first, then `$MAESTRO_REPO`, then `None`. Standard
    library only and no argparse: this runs before `build_parser()`, and a parser built here
    would have to be kept in step with the real one forever. The flag spelling is the only
    thing shared, and `tests/test_bootstrap.py` pins it against `build_parser()`'s.

    `None` rather than `Path.cwd()` when neither is given — see this module's docstring on
    why an inferred pin is the wrong kind of claim.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    for index, arg in enumerate(args):
        if arg == "--repo" and index + 1 < len(args):
            return Path(args[index + 1]).expanduser()
        if arg.startswith("--repo="):
            value = arg[len("--repo="):]
            if value:
                return Path(value).expanduser()
    raw = os.environ.get(PROJECT_ENV_VAR)
    return Path(raw).expanduser() if raw else None


def project_pin_file(repo: Path | str | None) -> Path | None:
    """`<repo>/.orchestrator/current` — the per-project pointer, or `None` for no project."""
    if repo is None:
        return None
    return Path(repo).joinpath(*PROJECT_PIN_RELPATH)


def _checkout_at(pointer: Path | None) -> Path | None:
    """Read one pointer file and validate what it names, or `None`.

    `None` covers every degenerate case together, because every one of them has the same
    correct response — run in place: no pointer file (never self-updated), an empty or
    unreadable one, or a path that no longer exists or is not a maestro checkout (a pruned
    or half-written worktree). Never raises; a resolver that can fail to start the CLI would
    be worse than the problem it solves.
    """
    if pointer is None:
        return None
    try:
        raw = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        root = Path(raw).resolve()
    except (OSError, ValueError):
        return None
    # A directory alone is not enough: a pointer naming something that is not a checkout
    # would send `-m maestro.cli` into an ImportError instead of back to a working maestro.
    if not (root / "maestro" / "__init__.py").is_file():
        return None
    return root


def project_adopted_root(argv: list | None = None, *, repo: Path | str | None = None) -> Path | None:
    """The checkout *this project* pinned, or `None` when it has not pinned one."""
    target = repo if repo is not None else repo_from_argv(argv)
    return _checkout_at(project_pin_file(target))


def machine_adopted_root() -> Path | None:
    """The checkout `~/.maestro/current` names, or `None` — the host-wide fallback."""
    return _checkout_at(current_file())


def adopted_root(argv: list | None = None, *, repo: Path | str | None = None) -> Path | None:
    """The checkout this process should be running, or `None` when there isn't a usable one.

    Per-project pin first, machine-global second (`[INV-09]`). The order is the whole
    invariant: a project that has pinned a version keeps running it no matter what another
    project adopts on the same host, and a project that has never pinned one still gets the
    host default rather than nothing.
    """
    return project_adopted_root(argv, repo=repo) or machine_adopted_root()


def reexec_into_adopted_version(argv: list | None = None) -> None:
    """Replace this process with the adopted checkout, or return and run in place.

    Returns — meaning "carry on here" — when the sentinel is set, when there is no usable
    adopted checkout, or when this process already *is* it. That last condition is what
    terminates the redirect: the child re-enters this function, finds `running_root() ==
    adopted_root()`, and proceeds. The sentinel makes it single-shot regardless.

    `argv` is forwarded to the resolver, not just to the child: which checkout is adopted
    depends on *which project* this invocation is about, and `--repo` is how a command says
    so (`repo_from_argv`).

    Re-execs as `-m maestro.cli` rather than re-running `sys.argv[0]`. The console script and
    `python -m maestro.cli` are both supported entry points, but only the module form is
    resolved through `sys.path` — repeating `sys.argv[0]` would re-run the *file* the working
    tree ships and defeat the whole redirect. `build_parser` sets `prog="maestro"` explicitly,
    so usage text is unchanged by the switch.
    """
    if os.environ.get(SENTINEL):
        return
    root = adopted_root(argv)
    if root is None or root == running_root():
        return

    env = dict(os.environ)
    env[SENTINEL] = "1"
    env[INVOKED_FROM_ENV] = str(running_root())
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
