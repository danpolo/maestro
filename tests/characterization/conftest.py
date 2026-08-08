"""Runs one test body against both the legacy orchestrator and extracted maestro modules.

The legacy module has import-time side effects: it inserts `scripts/` on sys.path and
calls load_dotenv(REPO/".env", override=True), which loads the consuming project's LIVE
credentials into os.environ. The path argument is explicit, so there is no way to
redirect it — instead we snapshot os.environ around the import and restore it, so no
usable token ever survives into a test. (The session-wide scrub in `tests/conftest.py`
already removed anything credential-shaped before that snapshot was taken.)

The `sandbox` fixture then repoints the subject's path globals at a temp tree. It does
**not** work from a hand-written list of global names: the reference module's globals are
`STATE_JSON`, `JOURNAL`, `WORKSPACES`, `USAGE_JSON`, `HALT_FILE`, `PROJECT_YAML` and a
dozen more, and any name missing from a hand-written list silently stays pointed at the
live repo. Instead every `Path`-valued module global that lives under the subject's own
`REPO` is discovered by reflection and rebased into the sandbox, and the fixture then
asserts that none is left behind.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.reference_repo import LEGACY_REPO


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "maestro_module(name): skip the maestro subject until that module is extracted",
    )


def _load_legacy():
    if LEGACY_REPO is None:
        pytest.skip(
            "reference repo unknown: set $MAESTRO_LEGACY_REPO or write its path to .legacy_repo"
        )
    scripts = LEGACY_REPO / "scripts"
    if not (scripts / "orchestrator_run.py").is_file():
        pytest.skip(f"reference repo not found at {LEGACY_REPO}")
    import os

    env_before = dict(os.environ)
    sys.path.insert(0, str(scripts))
    try:
        mod = importlib.import_module("orchestrator_run")
    finally:
        sys.path.remove(str(scripts))
        # The import loaded the consuming project's real .env. Restore the
        # environment so no live credential is reachable from any test.
        os.environ.clear()
        os.environ.update(env_before)
    return mod


def _load_maestro(request):
    marker = request.node.get_closest_marker("maestro_module")
    if marker is None:
        pytest.skip("test does not declare a maestro_module marker yet")
    name = marker.args[0]
    try:
        return importlib.import_module(f"maestro.{name}")
    except ModuleNotFoundError:
        pytest.skip(f"maestro.{name} not extracted yet")


@pytest.fixture(params=["legacy", "maestro"])
def subject(request):
    """The implementation under test."""
    if request.param == "legacy":
        return _load_legacy()
    return _load_maestro(request)


def _module_paths(module) -> dict[str, Path]:
    return {
        name: value
        for name, value in vars(module).items()
        if not name.startswith("__") and isinstance(value, Path)
    }


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


@pytest.fixture
def sandbox(subject, tmp_path, monkeypatch):
    """Rebase every path global of the subject into a temp tree. Never touches a real repo."""
    repo = tmp_path / "repo"
    orch_dir = repo / ".orchestrator"
    workspaces = orch_dir / "workspaces"
    workspaces.mkdir(parents=True)

    real_repo = Path(getattr(subject, "REPO")).resolve()

    redirected: dict[str, Path] = {}
    for name, value in _module_paths(subject).items():
        resolved = value if value.is_absolute() else (Path.cwd() / value)
        if not _under(resolved, real_repo):
            continue
        target = repo / resolved.relative_to(real_repo)
        monkeypatch.setattr(subject, name, target)
        redirected[name] = target

    leaked = sorted(
        name
        for name, value in _module_paths(subject).items()
        if _under(value if value.is_absolute() else Path.cwd() / value, real_repo)
    )
    assert not leaked, (
        f"path globals still resolve inside the real repo {real_repo}: {leaked}. "
        "Calling the subject now would write to a live system."
    )

    return SimpleNamespace(
        repo=repo,
        orch_dir=orch_dir,
        workspaces=workspaces,
        redirected=redirected,
    )
