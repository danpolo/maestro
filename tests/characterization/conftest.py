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


def _modules_to_rebase(subject) -> list:
    """The subject, plus every maestro module it may reach through.

    The reference implementation is one file, so rebasing the subject alone covers
    everything. The extracted code is not: `maestro.docs.roadmap` calls
    `maestro.state.append_journal`, which resolves `maestro.state.JOURNAL` — a global the
    subject does not own. Rebase every loaded maestro module so a test of one module cannot
    write through another one.
    """
    modules = [subject]
    for name, module in sorted(sys.modules.items()):
        if module is None or module is subject:
            continue
        if name == "maestro" or name.startswith("maestro."):
            modules.append(module)
    return modules


@pytest.fixture
def sandbox(subject, tmp_path, monkeypatch):
    """Rebase every path global of the subject into a temp tree. Never touches a real repo."""
    repo = tmp_path / "repo"
    orch_dir = repo / ".orchestrator"
    workspaces = orch_dir / "workspaces"
    workspaces.mkdir(parents=True)

    redirected: dict[str, Path] = {}
    checked: list[tuple[object, Path]] = []

    for module in _modules_to_rebase(subject):
        root = getattr(module, "REPO", None)
        if not isinstance(root, Path):
            # `maestro.docs.depmap` spells its anchor `REPO_ROOT`, matching the
            # reference `gen_dependency_map.py`'s own naming (see that module's
            # docstring) — fall back to it so a module reached only transitively
            # (e.g. via `maestro.docs.roadmap.run_dep_map`) still gets rebased
            # into the sandbox instead of silently keeping its real-repo paths.
            root = getattr(module, "REPO_ROOT", None)
        if not isinstance(root, Path):
            continue
        root = root.resolve()
        checked.append((module, root))
        for name, value in _module_paths(module).items():
            resolved = value if value.is_absolute() else (Path.cwd() / value)
            if not _under(resolved, root):
                continue
            target = repo / resolved.relative_to(root)
            monkeypatch.setattr(module, name, target)
            redirected[f"{module.__name__}.{name}"] = target

    leaked = sorted(
        f"{module.__name__}.{name}"
        for module, root in checked
        for name, value in _module_paths(module).items()
        if _under(value if value.is_absolute() else Path.cwd() / value, root)
    )
    assert not leaked, (
        f"path globals still resolve inside a real repo: {leaked}. "
        "Calling the subject now would write to a live system."
    )

    return SimpleNamespace(
        repo=repo,
        orch_dir=orch_dir,
        workspaces=workspaces,
        redirected=redirected,
    )
