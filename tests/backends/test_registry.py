"""Registry resolution, binary discovery and the dual-binary warning.

Every probe is monkeypatched. Nothing here executes an agent CLI, spends quota or
reaches a network — the module's only executing function, `_probe_version`, is either
replaced by an injected probe or driven against a fake `subprocess`.
"""
from __future__ import annotations

import ast
import os
import stat
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from maestro.backends import registry
from maestro.backends.registry import (
    BackendUnavailable,
    BinaryInfo,
    DriverRef,
    UnknownBackend,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    """No test may see another test's resolved binaries."""
    registry.clear_binary_cache()
    yield
    registry.clear_binary_cache()


def _make_executable(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _path_env(*directories: Path) -> str:
    return os.pathsep.join(str(d) for d in directories)


def _fake_module(name: str, **members) -> ModuleType:
    module = ModuleType(name)
    for key, value in members.items():
        setattr(module, key, value)
    return module


# ── name -> driver ──


def test_known_backends_are_sorted_and_import_nothing():
    assert registry.known_backends() == ("claude", "codex")


def test_driver_ref_normalises_the_name():
    assert registry.driver_ref("  CLAUDE  ").name == "claude"
    assert registry.driver_ref("codex").module.endswith(".codex")


def test_unknown_backend_names_the_alternatives():
    with pytest.raises(UnknownBackend) as excinfo:
        registry.driver_ref("gpt-9000")
    message = str(excinfo.value)
    assert "gpt-9000" in message
    for name in registry.known_backends():
        assert name in message


def test_driver_class_imports_the_driver_module_lazily(monkeypatch):
    calls = []

    class FakeDriver:
        name = "fake"

    def fake_import(module_name):
        calls.append(module_name)
        return _fake_module(module_name, FakeDriver=FakeDriver)

    monkeypatch.setitem(
        registry.BACKENDS,
        "fake",
        DriverRef(name="fake", module="maestro.backends.fake", attr="FakeDriver", binary="fake"),
    )
    monkeypatch.setattr(registry, "import_module", fake_import)

    assert calls == []                       # merely registering imports nothing
    assert registry.driver_class("fake") is FakeDriver
    assert calls == ["maestro.backends.fake"]


def test_driver_class_falls_back_to_a_class_declaring_the_backend_name(monkeypatch):
    class RenamedDriver:
        name = "fake"

    module_name = "maestro.backends.fake"
    module = _fake_module(module_name, RenamedDriver=RenamedDriver)
    RenamedDriver.__module__ = module_name

    monkeypatch.setitem(
        registry.BACKENDS,
        "fake",
        DriverRef(name="fake", module=module_name, attr="NoSuchName", binary="fake"),
    )
    monkeypatch.setattr(registry, "import_module", lambda _: module)

    assert registry.driver_class("fake") is RenamedDriver


def test_driver_class_reports_an_unimplemented_driver(monkeypatch):
    def fake_import(module_name):
        raise ModuleNotFoundError(f"No module named {module_name!r}", name=module_name)

    monkeypatch.setattr(registry, "import_module", fake_import)
    with pytest.raises(BackendUnavailable) as excinfo:
        registry.driver_class("codex")
    assert "maestro.backends.codex" in str(excinfo.value)


def test_driver_class_does_not_mask_a_missing_dependency(monkeypatch):
    def fake_import(_module_name):
        raise ModuleNotFoundError("No module named 'a_third_party_thing'", name="a_third_party_thing")

    monkeypatch.setattr(registry, "import_module", fake_import)
    with pytest.raises(ModuleNotFoundError):
        registry.driver_class("codex")


def test_driver_class_reports_a_module_with_no_driver(monkeypatch):
    monkeypatch.setattr(registry, "import_module", lambda name: _fake_module(name))
    with pytest.raises(BackendUnavailable):
        registry.driver_class("claude")


def test_get_backend_instantiates_the_driver(monkeypatch):
    class FakeDriver:
        name = "fake"

        def __init__(self, binary=None):
            self.binary = binary

    monkeypatch.setitem(
        registry.BACKENDS,
        "fake",
        DriverRef(name="fake", module="maestro.backends.fake", attr="FakeDriver", binary="fake"),
    )
    monkeypatch.setattr(
        registry, "import_module", lambda name: _fake_module(name, FakeDriver=FakeDriver)
    )

    driver = registry.get_backend("fake", binary="/somewhere/fake")
    assert isinstance(driver, FakeDriver)
    assert driver.binary == "/somewhere/fake"


def test_registry_has_no_module_level_driver_imports():
    """A module-scope driver import would risk a cycle and cost every caller."""
    tree = ast.parse(Path(registry.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    offenders = {
        name for name in imported if name.startswith("maestro.backends.") and name != "maestro.backends.base"
    }
    assert not offenders, f"registry imports driver modules at module scope: {offenders}"


# ── binary discovery ──


def test_find_binaries_returns_absolute_paths_in_path_order(tmp_path):
    first = _make_executable(tmp_path / "first", "codex")
    second = _make_executable(tmp_path / "second", "codex")
    _make_executable(tmp_path / "third", "something-else")

    found = registry.find_binaries("codex", _path_env(tmp_path / "first", tmp_path / "second", tmp_path / "third"))

    assert found == [first, second]
    assert all(p.is_absolute() for p in found)


def test_find_binaries_skips_non_executable_files(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "codex").write_text("not executable")
    assert registry.find_binaries("codex", _path_env(tmp_path / "bin")) == []


def test_find_binaries_collapses_two_path_entries_onto_one_install(tmp_path):
    """Two directories symlinking to one install is not a version conflict."""
    real = _make_executable(tmp_path / "install", "codex")
    (tmp_path / "link").mkdir()
    (tmp_path / "link" / "codex").symlink_to(real)

    found = registry.find_binaries("codex", _path_env(tmp_path / "link", tmp_path / "install"))
    assert found == [tmp_path / "link" / "codex"]


def test_find_binaries_is_empty_when_the_tool_is_absent(tmp_path):
    assert registry.find_binaries("codex", _path_env(tmp_path / "empty")) == []


def test_resolve_binary_pins_the_first_path_match_and_its_version(tmp_path):
    winner = _make_executable(tmp_path / "local", "codex")
    _make_executable(tmp_path / "usr", "codex")
    versions = {winner: "codex-cli 9.9.9"}

    info = registry.resolve_binary(
        "codex",
        probe=lambda path: versions.get(path, "codex-cli 9.9.9"),
        path_env=_path_env(tmp_path / "local", tmp_path / "usr"),
    )

    assert info.path == winner
    assert info.version == "codex-cli 9.9.9"
    assert info.shadowed == ()
    assert info.warning is None


def test_resolve_binary_warns_when_a_second_differing_binary_is_on_path(tmp_path):
    """Two installs at two versions: the earlier PATH entry wins today, and a PATH
    change would silently swap them. Warn, never fail — the winner still works."""
    newer = _make_executable(tmp_path / "local", "codex")
    older = _make_executable(tmp_path / "usr", "codex")
    versions = {newer: "codex-cli 9.9.9", older: "codex-cli 8.8.8"}

    info = registry.resolve_binary(
        "codex",
        probe=lambda path: versions[path],
        path_env=_path_env(tmp_path / "local", tmp_path / "usr"),
    )

    assert info.path == newer
    assert info.shadowed == ((older, "codex-cli 8.8.8"),)
    warning = info.warning
    assert str(newer) in warning and str(older) in warning
    assert "9.9.9" in warning and "8.8.8" in warning


def test_resolve_binary_does_not_warn_about_the_same_version_twice_installed(tmp_path):
    a = _make_executable(tmp_path / "a", "codex")
    b = _make_executable(tmp_path / "b", "codex")
    assert a != b

    info = registry.resolve_binary(
        "codex", probe=lambda _p: "codex-cli 9.9.9", path_env=_path_env(tmp_path / "a", tmp_path / "b")
    )
    assert info.shadowed == ()
    assert info.warning is None


def test_resolve_binary_reports_an_unknown_version_in_the_warning(tmp_path):
    newer = _make_executable(tmp_path / "a", "codex")
    older = _make_executable(tmp_path / "b", "codex")
    info = registry.resolve_binary(
        "codex",
        probe=lambda path: "codex-cli 9.9.9" if path == newer else "",
        path_env=_path_env(tmp_path / "a", tmp_path / "b"),
    )
    assert info.shadowed == ((older, ""),)
    assert "unknown version" in info.warning


def test_resolve_binary_is_none_when_the_tool_is_missing(tmp_path):
    assert registry.resolve_binary(
        "codex", probe=lambda _p: "never", path_env=_path_env(tmp_path / "empty")
    ) is None


def test_resolve_binary_probes_once_and_caches(tmp_path):
    _make_executable(tmp_path / "bin", "codex")
    calls = []

    def probe(path):
        calls.append(path)
        return "codex-cli 9.9.9"

    path_env = _path_env(tmp_path / "bin")
    first = registry.resolve_binary("codex", probe=probe, path_env=path_env)
    second = registry.resolve_binary("codex", probe=probe, path_env=path_env)

    assert first == second
    assert len(calls) == 1

    registry.resolve_binary("codex", probe=probe, path_env=path_env, refresh=True)
    assert len(calls) == 2


def test_resolve_binary_re_resolves_when_path_changes(tmp_path):
    one = _make_executable(tmp_path / "one", "codex")
    two = _make_executable(tmp_path / "two", "codex")
    probe = lambda _p: "codex-cli 9.9.9"

    assert registry.resolve_binary("codex", probe=probe, path_env=_path_env(tmp_path / "one")).path == one
    assert registry.resolve_binary("codex", probe=probe, path_env=_path_env(tmp_path / "two")).path == two


def test_resolve_binary_defaults_to_the_process_path(tmp_path, monkeypatch):
    installed = _make_executable(tmp_path / "bin", "codex")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    info = registry.resolve_binary("codex", probe=lambda _p: "codex-cli 9.9.9")
    assert info.path == installed


def test_an_injected_probe_never_reaches_subprocess(tmp_path, monkeypatch):
    """The guard that keeps the suite from ever executing a real agent CLI."""
    _make_executable(tmp_path / "bin", "codex")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a test executed a subprocess")

    monkeypatch.setattr(registry, "subprocess", SimpleNamespace(run=forbidden))
    info = registry.resolve_binary(
        "codex", probe=lambda _p: "codex-cli 9.9.9", path_env=_path_env(tmp_path / "bin")
    )
    assert info.version == "codex-cli 9.9.9"


# ── the default probe, driven against a fake subprocess ──


def test_probe_version_reads_the_first_line(monkeypatch):
    monkeypatch.setattr(
        registry,
        "subprocess",
        SimpleNamespace(run=lambda *a, **k: SimpleNamespace(stdout="codex-cli 9.9.9\n\n", stderr="")),
    )
    assert registry._probe_version(Path("/nowhere/codex")) == "codex-cli 9.9.9"


def test_probe_version_falls_back_to_stderr(monkeypatch):
    monkeypatch.setattr(
        registry,
        "subprocess",
        SimpleNamespace(run=lambda *a, **k: SimpleNamespace(stdout="", stderr="1.2.3\n")),
    )
    assert registry._probe_version(Path("/nowhere/codex")) == "1.2.3"


def test_probe_version_never_raises(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("no such binary")

    monkeypatch.setattr(registry, "subprocess", SimpleNamespace(run=boom))
    assert registry._probe_version(Path("/nowhere/codex")) == ""


# ── backend -> binary ──


def test_backend_binary_resolves_the_tool_a_backend_drives(tmp_path):
    installed = _make_executable(tmp_path / "bin", "codex")
    info = registry.backend_binary(
        "codex", probe=lambda _p: "codex-cli 9.9.9", path_env=_path_env(tmp_path / "bin")
    )
    assert info == BinaryInfo(tool="codex", path=installed, version="codex-cli 9.9.9")


def test_backend_binary_rejects_an_unknown_backend():
    with pytest.raises(UnknownBackend):
        registry.backend_binary("gpt-9000", probe=lambda _p: "")


def test_binary_warnings_collects_every_backend(tmp_path):
    newer = _make_executable(tmp_path / "local", "codex")
    older = _make_executable(tmp_path / "usr", "codex")
    _make_executable(tmp_path / "local", "claude")
    versions = {newer: "codex-cli 9.9.9", older: "codex-cli 8.8.8"}

    warnings = registry.binary_warnings(
        probe=lambda path: versions.get(path, "1.0.0"),
        path_env=_path_env(tmp_path / "local", tmp_path / "usr"),
    )

    assert len(warnings) == 1
    assert "codex" in warnings[0]


def test_binary_warnings_is_empty_when_nothing_is_installed(tmp_path):
    assert registry.binary_warnings(
        probe=lambda _p: "", path_env=_path_env(tmp_path / "empty")
    ) == []
