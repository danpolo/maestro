"""Session-wide safety nets.

1. The test process must never hold a live credential. The operator's shell exports real
   bot credentials, and one careless un-monkeypatched network call would spend them.
2. No test may start a real agent CLI. See `_no_agent_cli_ever` below — this one was
   written after a routing change quietly let a characterisation run spawn real `claude`
   processes, and it exists so that cannot happen again from any direction.

A third net used to live here: a write firewall around the reference repo, needed while
the characterisation suite imported that live project's modules and called its functions.
The legacy subject was retired on 2026-08-21 (see `tests/characterization/conftest.py`),
so no test reaches outside this repository any more and the firewall went with it.
"""
import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

# Telegram bot token shape. Extend if other credential shapes become reachable.
_CREDENTIAL_VALUE = re.compile(r"\d{9,}:[A-Za-z0-9_-]{30,}")


def _scrub_environment() -> list[str]:
    removed = []
    for key, value in list(os.environ.items()):
        if _CREDENTIAL_VALUE.fullmatch(value):
            del os.environ[key]
            removed.append(key)
    return removed


# Runs at conftest import, i.e. before any test module or fixture snapshots os.environ.
SCRUBBED = _scrub_environment()

# Third net: no test may re-exec out of the tree under test.
#
# `maestro.bootstrap` redirects any `maestro` invocation into the checkout named by
# `~/.maestro/current`, which on a developer machine is a *different* checkout than this one.
# Without this line the slow-tier tests that drive real `maestro init`/`doctor` subprocesses
# would silently exercise the deployed version and report the result as this tree's — a suite
# grading code it never ran. Set here, at conftest import, so it is already in `os.environ`
# before any fixture snapshots it and before any subprocess inherits it.
#
# `selfupdate._self_test_env` sets the same variable for the same reason one level up, where
# the consequence is worse: there, a green run would adopt a version nothing had tested.
os.environ.setdefault("MAESTRO_BOOTSTRAPPED", "1")


def _agent_binaries() -> frozenset:
    """Every executable the registry knows how to drive.

    Read from `BACKENDS` rather than listed here so a backend added later is guarded the
    moment it is registered — the same sourcing `tests/test_one_shot_agent_calls.py` uses,
    for the same reason. Importing the registry is cheap and imports no driver.
    """
    from maestro.backends.registry import BACKENDS

    return frozenset(ref.binary for ref in BACKENDS.values())


def _agent_binary_in(command) -> str:
    """The agent binary `command` would execute, or `""`.

    A sequence is an argv: only element zero is the program, so a backend name appearing
    as a *value* (`--model`, a task id, a prompt that says the word) is not a launch.
    A string is a shell command line, which can hold several programs (`cd X && y`), so
    every token is considered — but only tokens in program position would run, and
    distinguishing those needs a shell parser, so this errs toward refusing: a false
    positive here costs one explicit allowance in a test, a false negative costs quota.
    Comparison is on the basename, because an absolute path resolved by
    `registry.resolve_binary` names the same program as a bare `claude` does.
    """
    binaries = _agent_binaries()
    if isinstance(command, (str, bytes)):
        text = command.decode() if isinstance(command, bytes) else command
        try:
            tokens = shlex.split(text)
        except ValueError:          # unbalanced quotes — fall back to whitespace
            tokens = text.split()
    elif isinstance(command, (list, tuple)):
        tokens = [str(part) for part in command[:1]]
    else:
        return ""
    for token in tokens:
        if Path(str(token)).name in binaries:
            return str(token)
    return ""


class RealAgentCLIStarted(BaseException):
    """Raised when a test would have executed a real agent binary.

    Derived from `BaseException`, not `Exception`, and that is the whole point.
    `AgentBackend.complete` is contractually required never to raise — a failure comes
    back as an empty `Completion` — so it wraps the run in `except Exception`. An
    `AssertionError` from this guard was therefore *caught by the code under test* and
    turned into a perfectly ordinary "the agent said nothing", which is exactly the
    silent-no-op shape the guard exists to make impossible. A `BaseException` passes
    straight through `except Exception` and fails the test loudly, where it belongs.
    """


#: Flags that read a binary's metadata without starting an agent turn. `registry`'s
#: version pinning runs `<binary> --version` on the real executable by design, and that
#: spends nothing; refusing it would break binary discovery rather than protect anything.
_METADATA_ONLY_FLAGS = frozenset({"--version", "-V", "--help", "-h"})


def _is_metadata_probe(command) -> bool:
    if isinstance(command, (list, tuple)):
        rest = [str(part) for part in command[1:]]
    else:
        return False
    return bool(rest) and set(rest) <= _METADATA_ONLY_FLAGS


@pytest.fixture(autouse=True)
def _no_agent_cli_ever(monkeypatch):
    """Make starting a real agent CLI impossible, for every test, with no opt-out.

    Individual modules used to guard this themselves by swapping their own `subprocess`
    reference — `gates.subprocess`, `selfheal.diagnose.subprocess`, and so on. That worked
    only while each module built its own argv and ran it in place. The moment those call
    sites were routed through `maestro.agentcall` (2026-08-26) the execution moved *into*
    the drivers, every one of those module-level stubs stopped covering the path, and a
    characterisation run could spawn a real `claude` against live quota — one 120-second
    timeout at a time, silently, because a slow green test looks like a slow test.

    So the guard belongs at the boundary that actually starts a process. Two properties
    are what make it safe to have on for the whole suite:

    * **It wraps rather than replaces.** An earlier attempt swapped the *module object*
      `maestro.backends.claude.subprocess` for a stub, which shadowed the isolation the
      driver and implementer suites already had (they patch `subprocess.run` on the real
      module) and turned every `tmux new-window` in them into a failure. This patches
      `subprocess.run`/`Popen` in place and delegates everything it does not refuse, so a
      test that installs its own recorder still wins — and legitimately, since a recorder
      reaches no binary at all.
    * **It refuses agent binaries, not subprocesses.** Maestro shells out to `tmux`, `git`
      and its own `python` constantly and those must keep working; what must never happen
      is executing a `claude` or a `codex`. `registry.BACKENDS` says which those are.

    Tests that legitimately exercise a driver inject their own `runner=`, which never
    reaches this. Tests that legitimately exercise a *caller* stub `agentcall.ask`. Either
    way this should never fire — if it does, a code path is reaching for a real agent, and
    that is the bug it is reporting.
    """
    real_run, real_popen = subprocess.run, subprocess.Popen

    def guarded(real, *args, **kwargs):
        command = args[0] if args else kwargs.get("args", ())
        binary = _agent_binary_in(command)
        if binary and not _is_metadata_probe(command):
            rendered = command if isinstance(command, str) else " ".join(map(str, command))
            raise RealAgentCLIStarted(
                f"a test tried to start a real agent CLI ({binary}): {rendered[:200]}\n"
                "Inject a driver `runner=`, or stub `maestro.agentcall.ask` — never let a "
                "test reach a live binary or live quota."
            )
        return real(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: guarded(real_run, *a, **k))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: guarded(real_popen, *a, **k))
