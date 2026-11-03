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
import tempfile
from pathlib import Path

import pytest

# The GDT-14 demonstrator's sample project and overlays (`tests/goals_demo.py`) hold their
# own acceptance and unit tests, RED by design: they run only as Maestro runs them, inside a
# materialized copy, never collected by this suite.
collect_ignore_glob = ["fixtures/goals/demo/*"]

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


@pytest.fixture
def isolated_native_host_home(tmp_path, monkeypatch):
    """Caller tests own discovery state too, including in real child workers.

    Explicit synthetic CODEX_HOME/auth alone does not isolate host discovery.
    Keep production preparation and traversal intact, with a real plugin tree
    instead of the operator's concurrently changing caches and installations.
    Tests of hostile discovery inputs continue to supply their own paths.
    """
    home = tmp_path / 'host-home'
    plugin = home / '.claude/plugins/known_marketplaces.json.lock'
    plugin.mkdir(parents=True)
    (plugin / 'fixture').write_text('synthetic discovery; never executed\n')
    monkeypatch.setenv('HOME', str(home))
    return home

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

# No test may read the developer's real `~/.maestro/config.yaml` (P13 W1). Set here and not
# in a fixture: `quota`, `switch` and `watchdog` read thresholds when they are imported,
# which is during collection, before any fixture runs. The path does not exist, so there is
# no global layer unless a test points the variable at a file it wrote.
os.environ["MAESTRO_GLOBAL_CONFIG"] = str(
    Path(tempfile.gettempdir()) / f"maestro-tests-{os.getpid()}" / "no-global-config.yaml")
os.environ['MAESTRO_TELEGRAM_ENV'] = str(
    Path(tempfile.gettempdir()) / f'maestro-tests-{os.getpid()}' / 'no-shared-bot.env')
# GDT-12 (W12): an approved code lever writes an intake report into Maestro's own checkout
# (`improvement.maestro_repo()`). Pointed at a per-process path that does not exist, so no
# test can write into the real checkout unless it points the variable at a repo it made.
os.environ["MAESTRO_IMPROVEMENT_INTAKE"] = str(
    Path(tempfile.gettempdir()) / f"maestro-tests-{os.getpid()}" / "no-improvement-intake")

# The characterisation suite tests historical reference behavior which generated
# headless `claude -p` launchers. Default the test environment to headless mode,
# while production defaults to interactive mode unless configured otherwise.
os.environ.setdefault("MAESTRO_CLAUDE_MODE", "headless")

from maestro.control.store import ControlStore  # noqa: E402 - after the environment above

# Flushing to disk cost about 70% of the time in the suite's busiest modules (test audit,
# 2026-11-02: 314s to 83s). It only matters across a power loss: no test simulates one, and the
# page cache survives the process deaths the tests do use, so outside `real_fsync` tests it
# proves nothing extra. The fsync stand-in still raises on a bad descriptor, so code that fsyncs
# a closed fd keeps failing. Real subprocesses (`maestro init`, ...) keep the real behaviour.
_REAL_FSYNC = os.fsync
_REAL_STORE_PRAGMAS = ControlStore._apply_pragmas


def _fsync_without_flush(fd) -> None:
    os.fstat(fd)


def _store_pragmas_without_flush(store) -> None:
    _REAL_STORE_PRAGMAS(store)
    store.conn.execute("PRAGMA synchronous = OFF")


os.fsync = _fsync_without_flush
ControlStore._apply_pragmas = _store_pragmas_without_flush


@pytest.fixture(autouse=True)
def _real_fsync_when_marked(request, monkeypatch):
    if request.node.get_closest_marker("real_fsync"):
        monkeypatch.setattr(os, "fsync", _REAL_FSYNC)
        monkeypatch.setattr(ControlStore, "_apply_pragmas", _REAL_STORE_PRAGMAS)


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


@pytest.fixture(autouse=True)
def _no_telegram_listener(monkeypatch):
    """No test may start the Telegram update listener thread (`commands.start_update_listener`).

    `orchestrator.main` starts it when the bot is configured, and a developer's shell may
    carry a real `TELEGRAM_BOT_TOKEN`: a lane test would then long-poll the real bot from a
    background thread and consume updates meant for a live loop. Tests of the listener call
    its pieces directly.
    """
    monkeypatch.setenv("MAESTRO_TELEGRAM_LISTENER", "0")


@pytest.fixture(autouse=True)
def _fake_shared_bot_credentials(monkeypatch):
    """Transport tests inject fake environment credentials; production reads shared file."""
    from maestro.hitl import shared_bot
    monkeypatch.setattr(shared_bot, 'credentials', lambda: (
        os.environ.get('TELEGRAM_BOT_TOKEN', ''), os.environ.get('TELEGRAM_ALERT_CHAT_ID', '')))




@pytest.fixture(scope="session", autouse=True)
def _isolated_model_policy_home(tmp_path_factory):
    """No test may read or register into the developer's real shared model policy home.

    Session-scoped and set on `os.environ` itself, not via `monkeypatch`, because
    module-scoped fixtures (`maestro init` in `tests/test_cli.py`) run subprocesses before
    any function-scoped fixture exists. Tests that need their own home still override it.
    """
    previous = os.environ.get("MAESTRO_MODELS_HOME")
    os.environ["MAESTRO_MODELS_HOME"] = str(tmp_path_factory.mktemp("models-home"))
    yield
    if previous is None:
        os.environ.pop("MAESTRO_MODELS_HOME", None)
    else:
        os.environ["MAESTRO_MODELS_HOME"] = previous


@pytest.fixture(autouse=True)
def _isolated_host_ledger(monkeypatch, tmp_path_factory):
    """No test may touch the developer's real host reservation ledger (`~/.maestro/`).

    `orchestrator.main` reserves and syncs host pool slots (P08), and several suites drive
    it. `MAESTRO_HOST_LEDGER` redirects only the ledger — not `MAESTRO_HOME`, which
    `tests/test_selfupdate.py` sets and reads itself. The ledger gets its own
    temp directory rather than a slot in the test's `tmp_path`, because
    `test_no_lane_writes_outside_its_sandbox` requires every file under `tmp_path` to sit
    inside the lane's sandbox.
    """
    ledger = tmp_path_factory.mktemp("host-ledger") / "reservations.json"
    monkeypatch.setenv("MAESTRO_HOST_LEDGER", str(ledger))


@pytest.fixture(autouse=True)
def _no_live_self_update(monkeypatch):
    """No loop test may adopt a maestro version: the idle branches call `maybe_self_update`,
    which reads `~/.maestro/latest` and rewrites pins. `tests/test_self_update_restart.py`
    replaces this stub with its own; `tests/test_selfupdate.py` calls the module directly."""
    from maestro import orchestrator
    monkeypatch.setattr(orchestrator, "maybe_self_update", lambda: "up_to_date")


@pytest.fixture(autouse=True)
def _no_real_home_inventory(monkeypatch):
    """No in-process Codex preparation may inventory the developer's real home.

    Test trees sit under the real home, so `codex_runtime.prepare` walked the real
    `~/.claude` and `~/.codex` through cwd ancestors and then re-verified every directory
    it saw. Another session writing there mid-walk (e.g. a plugin update) failed a test
    that introduced no problem ("native preparation directory changed"). Fixture roots are
    still walked for real; see `tests/codex_host_isolation.py`. Fresh-subprocess tests call
    `skip_real_home_inventory()` themselves.
    """
    from tests.codex_host_isolation import skip_real_home_inventory
    skip_real_home_inventory(monkeypatch)


@pytest.fixture(autouse=True)
def _isolated_permissions_list(monkeypatch, tmp_path_factory):
    """No test may read or grow the developer's real global allow list (P12 pilot D30).

    `maestro.permissions` seeds `~/.maestro/permissions.json` on first read, and every
    Claude call reads it; a lane that allows a family would otherwise add it for every
    project on this machine. Its own temp directory, for the reason the ledger has one.
    """
    target = tmp_path_factory.mktemp("permissions") / "permissions.json"
    monkeypatch.setenv("MAESTRO_PERMISSIONS_FILE", str(target))


@pytest.fixture(autouse=True)
def _no_leaked_project():
    """Contain `cmd_*`'s `os.environ["MAESTRO_REPO"] = ...` for every test in the session.

    Every `cmd_*` sets that variable and never restores it (see `maestro/cli.py`'s module
    docstring: one project per process, because a real `maestro` invocation is a fresh
    process). Any test that calls `cli.main` / `cmd_*` in-process would otherwise leak its
    project root into every later test — `tests/test_selfupdate.py` then adopts into a
    per-project pin and its assertions about `~/.maestro/current` fail a long way from the
    cause. It lived in `tests/test_workflow_status.py` until
    `tests/test_host_resource_admission.py::test_status_and_explain_can_say_why_a_task_is_waiting`
    leaked the same way from a file that had no such guard (`_p12a_open_threads` #2), so it
    belongs here, session-wide, not per file.

    Written by hand rather than with `monkeypatch.delenv(..., raising=False)`, which records
    nothing to undo when the variable is already absent and so does not contain a later
    `os.environ[...] = ...` at all.
    """
    before = os.environ.get("MAESTRO_REPO")
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("MAESTRO_REPO", None)
        else:
            os.environ["MAESTRO_REPO"] = before
