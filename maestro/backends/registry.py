"""Name -> driver resolution, binary discovery and version pinning.

**This is the one module in `maestro/` allowed to know a backend by name.** Everywhere
else, core code branches on `Capabilities` (see `maestro.backends.base`); a name reaches
this module from configuration or from an operator command and is turned into a driver
here, once.

Two things make this more than a dictionary.

*Lazy driver import.* Nothing imports `maestro.backends.claude` or
`maestro.backends.codex` at module scope. Drivers import `base` (and, for one of them,
`maestro.quota`), so an eager import here would invite a cycle, and it would make merely
listing the known backends pay for importing every one of them. `driver_class` imports
on demand, through the module-level `import_module` alias so a test can intercept it.

*Binary discovery and version pinning.* A tool name on `PATH` is not one binary. On the
machine this was written for, two different `codex` installs are on `PATH` at two
different versions; the earlier `PATH` entry wins today, and a `PATH` change would
silently swap versions underneath a running orchestrator. So a backend's binary is
resolved to an **absolute path** once, its `--version` is recorded once, and any *other*
binary of the same name at a *different* version is reported through
`BinaryInfo.warning` — a warning, never a failure, because the resolved binary is still
perfectly usable.

The version probe is the only thing here that would execute anything, it is behind
`_probe_version`, and every caller can inject a replacement. Tests monkeypatch it; no
test may execute a real agent CLI.
"""
from __future__ import annotations

import inspect
import os
import subprocess
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Callable, Optional

__all__ = [
    "UnknownBackend",
    "BackendUnavailable",
    "DriverRef",
    "BinaryInfo",
    "BACKENDS",
    "DEFAULT_BACKEND",
    "known_backends",
    "normalise_name",
    "driver_ref",
    "driver_class",
    "driver_name_for_backend_id",
    "get_backend",
    "find_binaries",
    "resolve_binary",
    "backend_binary",
    "binary_warnings",
    "clear_binary_cache",
]

#: How long to wait for a `--version` probe. A hung probe must not hang the orchestrator.
VERSION_TIMEOUT_SEC = 10


class UnknownBackend(KeyError):
    """No backend is registered under that name."""


class BackendUnavailable(RuntimeError):
    """The backend is known but its driver module could not be imported."""


@dataclass(frozen=True)
class DriverRef:
    """Where a driver lives, and which executable it drives."""

    name: str
    module: str
    attr: str
    binary: str
    #: Whether `roles.default_chain()` may fall back onto this backend when no
    #: `fallback_chain` is configured. A backend graded below the others (antigravity)
    #: stays reachable by an explicit chain or `/backend`, but quota pressure on the
    #: default backend must not silently move its work there.
    in_default_chain: bool = True
    #: The catalog's `backend_id` for this driver (`catalog_from_register` uses the
    #: register subject unless a `*_CATALOG_DEFAULTS` row names another), so a binding
    #: can be turned back into the driver that launches it (P12A `_p07b` #2).
    backend_id: str = ""
    #: External discovery/table adapter, or None when modelctl has no adapter.
    modelctl_provider: str | None = None


@dataclass(frozen=True)
class BinaryInfo:
    """A resolved executable: absolute path, its reported version, and its rivals.

    `shadowed` holds every *other* binary of the same name found later on `PATH` whose
    version differs from the resolved one. Same version at a different path is not
    reported: it changes nothing about what will run.
    """

    tool: str
    path: Path
    version: str = ""
    shadowed: tuple[tuple[Path, str], ...] = ()

    @property
    def warning(self) -> Optional[str]:
        """A one-line operator warning when PATH holds a second, differing binary."""
        if not self.shadowed:
            return None
        others = "; ".join(
            f"{path} ({version or 'unknown version'})" for path, version in self.shadowed
        )
        return (
            f"multiple {self.tool!r} binaries on PATH at different versions: "
            f"using {self.path} ({self.version or 'unknown version'}); also found {others}"
        )


#: The name table. Mutable on purpose: a test substitutes entries here, and a future
#: driver is added by adding a row rather than by editing resolution logic.
BACKENDS: dict[str, DriverRef] = {
    "claude": DriverRef(
        name="claude",
        module="maestro.backends.claude",
        attr="ClaudeBackend",
        binary="claude",
        backend_id="claude",
        modelctl_provider="claude",
    ),
    "codex": DriverRef(
        name="codex",
        module="maestro.backends.codex",
        attr="CodexBackend",
        binary="codex",
        backend_id="codex",
        modelctl_provider="codex",
    ),
    "antigravity": DriverRef(
        name="antigravity",
        module="maestro.backends.antigravity",
        attr="AntigravityBackend",
        binary="agy",
        in_default_chain=False,
        backend_id="antigravity_cli",
        modelctl_provider="antigravity",
    ),
}

#: Used when nothing else selects a backend. Role resolution owns the real policy.
DEFAULT_BACKEND = "claude"


def normalise_name(name: str) -> str:
    """Canonical form of a backend name. Operator commands arrive lowercased already."""
    return str(name or "").strip().lower()


def known_backends() -> tuple[str, ...]:
    """Every registered backend name, sorted. Imports no driver."""
    return tuple(sorted(BACKENDS))


def driver_ref(name: str) -> DriverRef:
    """The registration for `name`, or `UnknownBackend`. Imports no driver."""
    key = normalise_name(name)
    try:
        return BACKENDS[key]
    except KeyError:
        raise UnknownBackend(
            f"unknown backend {name!r}; known backends: {', '.join(known_backends())}"
        ) from None


def driver_name_for_backend_id(backend_id: str) -> str:
    """The registry name whose driver serves catalog `backend_id`, or `UnknownBackend`."""
    for name in known_backends():
        if BACKENDS[name].backend_id and BACKENDS[name].backend_id == str(backend_id):
            return name
    raise UnknownBackend(
        f"no driver serves catalog backend_id {backend_id!r}; known: "
        + ", ".join(BACKENDS[name].backend_id for name in known_backends())
    )


def backend_id_for_modelctl_provider(provider: str) -> str:
    """Translate an owner's provider id at the external contract boundary."""
    matches = [ref.backend_id for ref in BACKENDS.values() if ref.modelctl_provider == provider]
    if len(matches) != 1:
        raise UnknownBackend(f'no unique catalog backend for modelctl provider {provider!r}')
    return matches[0]


def _find_by_declared_name(module, ref: DriverRef):
    """Fallback lookup: a class in `module` that declares this backend's own name.

    `attr` is the convention; this keeps resolution working if a driver names its class
    something else, so a rename cannot break configuration.
    """
    for _, obj in sorted(vars(module).items()):
        if not inspect.isclass(obj) or getattr(obj, "__module__", None) != ref.module:
            continue
        if getattr(obj, "name", None) == ref.name:
            return obj
    return None


def driver_class(name: str) -> type:
    """The driver class for `name`, importing its module on first use.

    Raises `UnknownBackend` for an unregistered name and `BackendUnavailable` when the
    driver module is missing or does not expose a driver.
    """
    ref = driver_ref(name)
    try:
        module = import_module(ref.module)
    except ModuleNotFoundError as exc:
        if exc.name and not ref.module.startswith(exc.name):
            raise  # a dependency of the driver is missing, not the driver itself
        raise BackendUnavailable(
            f"backend {ref.name!r} is registered but {ref.module} is not importable"
        ) from exc

    cls = getattr(module, ref.attr, None) or _find_by_declared_name(module, ref)
    if cls is None:
        raise BackendUnavailable(
            f"{ref.module} exposes no {ref.attr} and no class named {ref.name!r}"
        )
    return cls


def launch_driver_class(name: str) -> type:
    """Fresh-launch adapter, preserving injected drivers and the base driver API."""
    cls = driver_class(name)
    if normalise_name(name) == "codex":
        from maestro.backends.codex import CodexBackend
        if cls is CodexBackend:
            from maestro.backends.codex_launch import CodexLaunchBackend
            return CodexLaunchBackend
    return cls


def get_backend(name: str, *args, **kwargs):
    """Instantiate the driver for `name`. Extra arguments go to its constructor."""
    return driver_class(name)(*args, **kwargs)


# ── binary discovery ──


def find_binaries(tool: str, path_env: Optional[str] = None) -> list[Path]:
    """Every executable named `tool` on `PATH`, in PATH order, as absolute paths.

    Entries that resolve to the same real file are collapsed: two `PATH` directories
    symlinking to one install are one binary, not a conflict. The returned path is the
    one that would actually be invoked (the symlink, not its target), because that is
    what an operator sees and what a `PATH` change would move.
    """
    raw = os.environ.get("PATH", "") if path_env is None else path_env
    seen: set[str] = set()
    found: list[Path] = []
    for entry in raw.split(os.pathsep):
        if not entry:
            continue
        candidate = Path(entry).expanduser() / tool
        try:
            if not (candidate.is_file() and os.access(candidate, os.X_OK)):
                continue
            real = os.path.realpath(candidate)
        except OSError:
            continue
        if real in seen:
            continue
        seen.add(real)
        found.append(Path(os.path.abspath(candidate)))
    return found


def _probe_version(path: Path) -> str:
    """Read `<path> --version`. Returns "" on any failure — a probe never raises.

    The single point in this module that executes anything. Every caller can replace it.
    """
    try:
        proc = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=VERSION_TIMEOUT_SEC,
        )
    except Exception:
        return ""
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return output.splitlines()[0].strip() if output else ""


#: (tool, PATH) -> resolved binary. Version probing costs a subprocess per candidate, so
#: it happens once; `PATH` is part of the key so a changed `PATH` re-resolves.
_BINARY_CACHE: dict[tuple[str, str], Optional[BinaryInfo]] = {}


def clear_binary_cache() -> None:
    """Forget every resolved binary. Tests and `doctor --refresh` use this."""
    _BINARY_CACHE.clear()


def resolve_binary(
    tool: str,
    *,
    probe: Optional[Callable[[Path], str]] = None,
    path_env: Optional[str] = None,
    refresh: bool = False,
) -> Optional[BinaryInfo]:
    """Resolve `tool` to one absolute path and pin its version. `None` if not on PATH.

    The first `PATH` match wins, exactly as the shell would choose it. Every later match
    is probed too, so that a second install at a different version can be surfaced
    through `BinaryInfo.warning` instead of silently swapping under a `PATH` change.
    """
    path_value = os.environ.get("PATH", "") if path_env is None else path_env
    key = (tool, path_value)
    if not refresh and key in _BINARY_CACHE:
        return _BINARY_CACHE[key]

    read_version = probe or _probe_version
    candidates = find_binaries(tool, path_value)
    info: Optional[BinaryInfo] = None
    if candidates:
        primary, rest = candidates[0], candidates[1:]
        version = read_version(primary)
        shadowed = tuple(
            (path, other) for path, other in ((p, read_version(p)) for p in rest)
            if other != version
        )
        info = BinaryInfo(tool=tool, path=primary, version=version, shadowed=shadowed)

    _BINARY_CACHE[key] = info
    return info


def backend_binary(name: str, **kwargs) -> Optional[BinaryInfo]:
    """Resolve the executable a backend drives. Imports no driver."""
    return resolve_binary(driver_ref(name).binary, **kwargs)


def binary_warnings(**kwargs) -> list[str]:
    """Every backend's binary warning, for `doctor` and for the launch-time journal."""
    warnings = []
    for name in known_backends():
        info = backend_binary(name, **kwargs)
        if info is not None and info.warning:
            warnings.append(info.warning)
    return warnings
