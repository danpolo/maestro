"""Runs each characterisation body against the extracted maestro modules.

Historically this ran every body twice — once against the reference orchestrator, once
against maestro — as the equivalence oracle for the extraction. The legacy subject was
retired on 2026-08-21; see the `subject` fixture for why.

The `sandbox` fixture repoints the subject's path globals at a temp tree. It does
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


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "maestro_module(name): the maestro module this test's subject resolves to",
    )


def _load_maestro(request):
    marker = request.node.get_closest_marker("maestro_module")
    if marker is None:
        raise AssertionError(
            f"{request.node.nodeid} declares no maestro_module marker. Extraction is "
            "complete, so there is no longer a 'not extracted yet' state to skip for — "
            "a missing marker is a defect in the test."
        )
    name = marker.args[0]
    return importlib.import_module(f"maestro.{name}")


@pytest.fixture
def subject(request):
    """The implementation under test.

    Single-subject since the legacy subject was retired (2026-08-21). This harness was
    built to run each body against both the reference orchestrator and the extracted
    maestro module, proving equivalence one assertion at a time. M5's cutover commit
    deleted the reference scripts, so the legacy half could never load again and 2285 of
    5269 tests skipped silently — a suite that reported green while 43% of it did not
    run. The equivalence oracle had already done its job by then: every stage M0-M6 was
    signed off against it, and the extracted code has since run the live loop end to end.

    Deliberately no fallback to a vendored or configured reference: maestro is a generic
    tool and must not carry a path to any one consuming project.
    """
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
