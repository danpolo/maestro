"""Run a maestro module from *this* checkout: `python <root>/maestro/pinned.py <module> [args...]`.

A child the controller starts in a tmux window does not inherit the controller's
environment: `tmux new-window` gives the window the tmux *server's*. So the `PYTHONPATH`
that `maestro.bootstrap` used to put the controller on its adopted checkout never reaches
the window, and a plain `python -m maestro.x` there resolves `maestro` through the project
venv's editable install — the live repository, uncommitted edits included, rather than the
version the controller is running.

The pin therefore travels in the command line. This file is run by path, so which checkout
it belongs to is not open to interpretation: it puts its own checkout at the head of
`sys.path` and runs the named module as `__main__` in the same process (the pid a worker
binds is the pid tmux started). No environment variable is set, so nothing leaks to the
module's own children: a project gate started by a worker sees the environment it always
saw.

**Standard library only, and nothing imported from `maestro`.** Importing the package
before `sys.path` is corrected would bind it to the wrong checkout for the life of the
process, which is the fault this file exists to remove. `bootstrap.pinned_module_argv`
builds the command.
"""
from __future__ import annotations

import os
import runpy
import sys


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: pinned.py <module> [args...]", file=sys.stderr)
        raise SystemExit(2)
    package_dir = os.path.dirname(os.path.realpath(__file__))
    # Run as a file, `sys.path[0]` is this file's directory. It must not stay: every
    # top-level maestro module (`config`, `metrics`, `status`, ...) would shadow a bare
    # import of the same name. `-m` would have had the cwd there instead, so keep that.
    if sys.path and os.path.realpath(sys.path[0]) == package_dir:
        del sys.path[0]
    sys.path[0:0] = [os.path.dirname(package_dir), os.getcwd()]
    sys.argv = sys.argv[1:]
    runpy.run_module(sys.argv[0], run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
