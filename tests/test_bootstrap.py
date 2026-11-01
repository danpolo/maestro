"""`maestro.bootstrap` — the resolver that makes an adopted version a deployment.

Before this module, `~/.maestro/current` was written by `adopt()` and read by nothing, so a
project ran whichever checkout `import maestro` resolved to rather than the one that passed
the self-test gate. These tests cover the resolver's fail-open contract (every degenerate
pointer leaves the process in place), the redirect's guards, and the one property that makes
the redirect terminate rather than loop.

The redirect itself is tested by injecting a fake `execve` rather than by really replacing
the process — `os.execve` cannot be observed from inside the process it destroys. The
end-to-end behaviour is covered separately by `test_reexec_is_real_against_a_second_checkout`,
which drives a real subprocess.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from maestro import bootstrap
from tests.hang_bound import HANG_SECONDS


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A self-update home under `tmp_path`, with the sentinel cleared.

    The sentinel is set session-wide by `tests/conftest.py` (no test may re-exec out of the
    tree under test). These tests are the exception that has to see the machinery run, so
    they clear it deliberately and never actually exec.
    """
    h = tmp_path / "maestro_home"
    h.mkdir()
    monkeypatch.setenv(bootstrap.ENV_VAR, str(h))
    monkeypatch.delenv(bootstrap.SENTINEL, raising=False)
    return h


def _checkout(root: Path) -> Path:
    """A directory shaped like a maestro checkout, as `adopted_root` validates it."""
    (root / "maestro").mkdir(parents=True)
    (root / "maestro" / "__init__.py").write_text("", encoding="utf-8")
    return root


# --- adopted_root: fail open, every time ------------------------------------------------


def test_adopted_root_is_none_when_the_pointer_is_absent(home):
    assert bootstrap.adopted_root() is None


@pytest.mark.parametrize("content", ["", "   \n", "\n\n"])
def test_adopted_root_is_none_for_an_empty_pointer(home, content):
    bootstrap.current_file().write_text(content, encoding="utf-8")
    assert bootstrap.adopted_root() is None


def test_adopted_root_is_none_when_the_path_does_not_exist(home, tmp_path):
    bootstrap.current_file().write_text(str(tmp_path / "pruned"), encoding="utf-8")
    assert bootstrap.adopted_root() is None


def test_adopted_root_is_none_when_the_path_is_not_a_maestro_checkout(home, tmp_path):
    plain = tmp_path / "not_maestro"
    plain.mkdir()
    bootstrap.current_file().write_text(str(plain), encoding="utf-8")
    assert bootstrap.adopted_root() is None, (
        "a directory alone must not satisfy the check — sending `-m maestro.cli` at one "
        "trades a working maestro for an ImportError"
    )


def test_adopted_root_is_none_when_the_pointer_is_a_directory(home):
    bootstrap.current_file().mkdir()
    assert bootstrap.adopted_root() is None


def test_adopted_root_returns_a_valid_checkout(home, tmp_path):
    wt = _checkout(tmp_path / "versions" / "abc123")
    bootstrap.current_file().write_text(str(wt) + "\n", encoding="utf-8")
    assert bootstrap.adopted_root() == wt.resolve()


# --- the redirect and its guards ---------------------------------------------------------


@pytest.fixture
def spy_exec(monkeypatch):
    """Capture what `reexec_into_adopted_version` would have exec'd."""
    calls = []
    monkeypatch.setattr(os, "execve", lambda path, args, env: calls.append((path, args, env)))
    return calls


def test_redirect_is_a_noop_when_nothing_is_adopted(home, spy_exec):
    bootstrap.reexec_into_adopted_version([])
    assert spy_exec == []


def test_redirect_is_a_noop_when_already_running_the_adopted_checkout(home, spy_exec):
    bootstrap.current_file().write_text(str(bootstrap.running_root()), encoding="utf-8")
    bootstrap.reexec_into_adopted_version([])
    assert spy_exec == [], (
        "this is what terminates the redirect: the child re-enters and finds itself adopted"
    )


def test_the_sentinel_suppresses_the_redirect(home, tmp_path, monkeypatch, spy_exec):
    wt = _checkout(tmp_path / "adopted")
    bootstrap.current_file().write_text(str(wt), encoding="utf-8")
    monkeypatch.setenv(bootstrap.SENTINEL, "1")
    bootstrap.reexec_into_adopted_version([])
    assert spy_exec == []


def test_redirect_execs_the_module_form_with_the_adopted_checkout_leading(
    home, tmp_path, spy_exec
):
    wt = _checkout(tmp_path / "adopted")
    bootstrap.current_file().write_text(str(wt), encoding="utf-8")

    bootstrap.reexec_into_adopted_version(["doctor", "--repo", "/x"])

    assert len(spy_exec) == 1
    path, args, env = spy_exec[0]
    assert path == sys.executable
    assert args == [sys.executable, "-m", "maestro.cli", "doctor", "--repo", "/x"], (
        "must be the module form: re-running sys.argv[0] would re-run the working tree's "
        "own file and defeat the redirect"
    )
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(wt.resolve())
    assert env[bootstrap.SENTINEL] == "1"


def test_redirect_preserves_an_existing_pythonpath_behind_the_adopted_checkout(
    home, tmp_path, monkeypatch, spy_exec
):
    wt = _checkout(tmp_path / "adopted")
    bootstrap.current_file().write_text(str(wt), encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(["/keep/me", str(wt.resolve())]))

    bootstrap.reexec_into_adopted_version([])

    _, _, env = spy_exec[0]
    assert env["PYTHONPATH"].split(os.pathsep) == [str(wt.resolve()), "/keep/me"], (
        "the adopted checkout leads, existing entries survive behind it, and it is not "
        "duplicated when it was already present"
    )


# --- the end-to-end claim, against a real second checkout --------------------------------


def test_reexec_is_real_against_a_second_checkout(tmp_path):
    """A real `maestro` process, pointed at a second checkout, runs *that* one.

    Everything above injects `execve`. This one does not: it builds a throwaway checkout
    whose `maestro.cli.main` is distinguishable from this tree's, points `current` at it, and
    runs `-m maestro.cli` for real. It is the test that would have failed before this module
    existed — and the one that proves `PYTHONPATH` beats an editable install's meta-path
    finder, which is the mechanism the whole design rests on.
    """
    other = tmp_path / "other_checkout"
    pkg = other / "maestro"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "cli.py").write_text(
        "import sys\n"
        "def main(argv=None):\n"
        "    print('OTHER CHECKOUT')\n"
        "    return 0\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n",
        encoding="utf-8",
    )

    home_dir = tmp_path / "maestro_home"
    home_dir.mkdir()
    (home_dir / "current").write_text(str(other), encoding="utf-8")

    env = dict(os.environ)
    env[bootstrap.ENV_VAR] = str(home_dir)
    env.pop(bootstrap.SENTINEL, None)          # conftest sets it; this test needs the redirect
    env["PYTHONPATH"] = str(bootstrap.running_root())

    proc = subprocess.run(
        [sys.executable, "-m", "maestro.cli", "status"],
        cwd=tmp_path, capture_output=True, text=True, env=env,
    )

    assert "OTHER CHECKOUT" in proc.stdout, (
        f"expected the redirect to reach the adopted checkout.\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr[-2000:]!r}"
    )


def test_a_pinned_child_runs_the_launchers_checkout_with_no_environment(tmp_path):
    """A child started through `pinned_module_argv` imports the launcher's checkout.

    `tmux new-window` hands a window the tmux *server's* environment, so a `PYTHONPATH` set
    by the launcher never arrives and a plain `python -m maestro.x` in a project venv falls
    back to the editable install: the live repository, not the version the controller
    runs. The pin therefore travels in the command line. Real subprocess, neutral cwd, no
    `PYTHONPATH`: from a second checkout (a worktree, a release self-test) the unpinned
    form imports a different tree, which is what makes this discriminating.
    """
    (tmp_path / "probe_w8.py").write_text(
        "import json, os, sys\n"
        "import maestro\n"
        "print(json.dumps({'file': maestro.__file__, 'argv': sys.argv[1:],\n"
        "                  'name': __name__, 'pythonpath': os.environ.get('PYTHONPATH')}))\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}

    proc = subprocess.run(
        [*bootstrap.pinned_module_argv(sys.executable, "probe_w8"), "arg1"],
        cwd=tmp_path, capture_output=True, text=True, env=env,
    )

    assert proc.returncode == 0, proc.stderr[-2000:]
    seen = json.loads(proc.stdout)
    assert Path(seen["file"]).resolve() == bootstrap.running_root() / "maestro" / "__init__.py"
    assert seen["argv"] == ["arg1"]
    assert seen["name"] == "__main__"
    assert seen["pythonpath"] is None


def test_a_pinned_child_keeps_its_own_exit_code(tmp_path):
    (tmp_path / "probe_w8_exit.py").write_text("raise SystemExit(7)\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}

    proc = subprocess.run(
        bootstrap.pinned_module_argv(sys.executable, "probe_w8_exit"),
        cwd=tmp_path, capture_output=True, text=True, env=env,
    )

    assert proc.returncode == 7


@pytest.mark.slow
def test_a_pinned_child_in_a_real_tmux_window_runs_the_launchers_checkout(tmp_path):
    """The same pin, through the thing it exists for: a window of a real tmux server.

    The server is started without `PYTHONPATH` and the launcher asks for the window with
    one set, the way a controller on an adopted checkout does. The window gets the
    server's environment, and the child still imports the launcher's checkout.

    A private server (`-L <socket>`), never the default one that hosts the live `agents`
    session; see `test_cli.py` on why `-L` and not `TMUX_TMPDIR`.
    """
    real_tmux = shutil.which("tmux")
    assert real_tmux, "tmux not on PATH — required for this test"
    tmux = [real_tmux, "-L", f"test-pinned-{os.getpid()}-{id(tmp_path)}"]
    (tmp_path / "probe_w8_tmux.py").write_text(
        "import json, os, sys\n"
        "import maestro\n"
        "seen = {'file': maestro.__file__, 'pid': os.getpid(), 'tmux': os.environ.get('TMUX'),\n"
        "        'pythonpath': os.environ.get('PYTHONPATH')}\n"
        "with open(sys.argv[1] + '.tmp', 'w') as out:\n"
        "    json.dump(seen, out)\n"
        "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n",
        encoding="utf-8",
    )
    out = tmp_path / "seen.json"
    server_env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "TMUX")}
    helper = shlex.join(bootstrap.pinned_module_argv(sys.executable, "probe_w8_tmux"))
    socket_path = ""
    try:
        subprocess.run(
            [*tmux, "new-session", "-d", "-s", "agents", "-c", str(tmp_path)],
            check=True, env=server_env, capture_output=True,
        )
        socket_path = subprocess.run(
            [*tmux, "display-message", "-p", "-t", "agents", "#{socket_path}"],
            capture_output=True, text=True,
        ).stdout.strip()
        # The command line `hitl.commands` and `selfheal.selffix` build for their helpers.
        subprocess.run(
            [*tmux, "new-window", "-d", "-t", "agents", "-n", "probe",
             f"cd {shlex.quote(str(tmp_path))} && {helper} {shlex.quote(str(out))}"],
            check=True, capture_output=True,
            env={**server_env, "PYTHONPATH": str(tmp_path / "another_checkout")},
        )
        deadline = time.monotonic() + HANG_SECONDS
        while not out.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert out.exists(), "the tmux window never ran the pinned child"
        seen = json.loads(out.read_text(encoding="utf-8"))
    finally:
        subprocess.run([*tmux, "kill-server"], capture_output=True)
        if socket_path:     # a killed server leaves its socket file behind
            Path(socket_path).unlink(missing_ok=True)

    assert Path(seen["file"]).resolve() == bootstrap.running_root() / "maestro" / "__init__.py"
    assert seen["tmux"], "the child did not run inside the tmux server"
    assert seen["pythonpath"] is None


def test_selfupdate_and_bootstrap_agree_on_where_current_lives(tmp_path, monkeypatch):
    """Two modules name the same file; a divergence would make adoption write one pointer
    and the resolver read another, which is the failure this whole change is about."""
    from maestro import selfupdate

    monkeypatch.setenv(bootstrap.ENV_VAR, str(tmp_path / "h"))
    assert selfupdate.SelfUpdatePaths.from_env().current_file == bootstrap.current_file()


# --- G1: a redirected run must say so -----------------------------------------------------


def test_redirect_tells_the_child_which_checkout_was_invoked(home, tmp_path, spy_exec):
    wt = _checkout(tmp_path / "adopted")
    bootstrap.current_file().write_text(str(wt), encoding="utf-8")

    bootstrap.reexec_into_adopted_version(["init", "."])

    _, _, env = spy_exec[0]
    assert env[bootstrap.INVOKED_FROM_ENV] == str(bootstrap.running_root())


def test_redirected_from_is_none_unless_the_invoked_checkout_differs(monkeypatch, tmp_path):
    monkeypatch.delenv(bootstrap.INVOKED_FROM_ENV, raising=False)
    assert bootstrap.redirected_from() is None
    monkeypatch.setenv(bootstrap.INVOKED_FROM_ENV, str(bootstrap.running_root()))
    assert bootstrap.redirected_from() is None
    monkeypatch.setenv(bootstrap.INVOKED_FROM_ENV, str(tmp_path))
    assert bootstrap.redirected_from() == tmp_path.resolve()


def test_init_names_its_checkout_and_warns_when_redirected(monkeypatch, tmp_path, capsys):
    """itv onboarding got the machine pin's stale scaffold with no sign of the redirect."""
    from maestro.cli import cmd_init

    monkeypatch.setenv(bootstrap.INVOKED_FROM_ENV, str(tmp_path / "invoked"))
    cmd_init(tmp_path)  # not a git repo: returns early, after the provenance lines

    out = capsys.readouterr().out
    assert f"scaffolding from maestro checkout {bootstrap.running_root()}" in out
    assert f"invoked from {(tmp_path / 'invoked').resolve()}" in out
    assert bootstrap.SENTINEL in out


def test_init_does_not_warn_when_not_redirected(monkeypatch, tmp_path, capsys):
    from maestro.cli import cmd_init

    monkeypatch.delenv(bootstrap.INVOKED_FROM_ENV, raising=False)
    cmd_init(tmp_path)

    out = capsys.readouterr().out
    assert "scaffolding from maestro checkout" in out
    assert "WARNING" not in out
