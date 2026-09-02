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

import os
import subprocess
import sys
from pathlib import Path

import pytest

from maestro import bootstrap


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


def test_selfupdate_and_bootstrap_agree_on_where_current_lives(tmp_path, monkeypatch):
    """Two modules name the same file; a divergence would make adoption write one pointer
    and the resolver read another, which is the failure this whole change is about."""
    from maestro import selfupdate

    monkeypatch.setenv(bootstrap.ENV_VAR, str(tmp_path / "h"))
    assert selfupdate.SelfUpdatePaths.from_env().current_file == bootstrap.current_file()
