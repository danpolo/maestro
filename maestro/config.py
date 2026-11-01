"""Project configuration: the optional `project.yaml` at the repository root.

Extracted verbatim from the reference orchestrator. The body is unchanged — only the
import block and the derivation of the module-level path globals differ. Every failure
mode (missing file, syntax error, directory, unreadable file, empty document) collapses
to the same empty dict, and the `-> dict` annotation is not enforced; those behaviours
are pinned by `tests/characterization/test_config.py` and are not fixed here.
"""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path

import yaml  # PyYAML

from maestro.paths import Paths

_PATHS = Paths.from_env()

REPO                  = _PATHS.repo
PROJECT_YAML          = REPO / "project.yaml"


# ── global config (P13 W1, DESIGN.md §17.1) ──

#: Overrides where the global config file is read from. The test suite points it away from
#: the developer's real `~/.maestro/config.yaml`.
GLOBAL_CONFIG_ENV = "MAESTRO_GLOBAL_CONFIG"

#: Every top-level key either layer may hold. `maestro doctor` fails on any other key, so a
#: typo cannot silently fall back to a default. `fallback_chain` is retired (DESIGN.md
#: §17.2) and stays listed on purpose: doctor's `role_shape` check reports it, with the
#: reason, and listing it here keeps `config_keys` from reporting it a second time.
#: `switch` stays known until the route rework retires it.
KNOWN_KEYS = frozenset({
    "version", "project", "roles", "fallback_chain", "switch", "model_limits", "engineering",
    "gate", "retention", "telegram", "thresholds", "risky_set", "deny_list_extra", "secrets",
    "prod_stores", "bot_files", "confinement", "docs",
    "priority", "defaults", "budget", "routing", "runaway", "sprint",
    "pilot",
})


def global_config_path() -> Path:
    """`$MAESTRO_GLOBAL_CONFIG`, else `config.yaml` in the Maestro home (`~/.maestro`)."""
    raw = os.environ.get(GLOBAL_CONFIG_ENV)
    if raw:
        return Path(raw).expanduser().resolve()
    from maestro import bootstrap
    return bootstrap.home() / "config.yaml"


def _read_mapping(path) -> dict:
    """A YAML mapping, `{}` for anything else. Lenient for the reason `thresholds` gives."""
    try:
        document = yaml.safe_load(Path(path).read_text())
    except Exception:
        return {}
    return document if isinstance(document, dict) else {}


def load_global_config() -> dict:
    """The global defaults every project inherits; `{}` when there is no usable file."""
    return _read_mapping(global_config_path())


def deep_merge(base, over) -> dict:
    """`over` laid on `base`: mappings merge key by key, anything else replaces whole.

    A list is never concatenated, and `None` in `over` means absent, as it does for every
    other reader in this module. Neither input is mutated.
    """
    merged = dict(base)
    for key, value in over.items():
        if value is None:
            continue
        lower = merged.get(key)
        if isinstance(value, dict) and isinstance(lower, dict):
            merged[key] = deep_merge(lower, value)
        else:
            merged[key] = deep_merge({}, value) if isinstance(value, dict) else value
    return merged


def config_leaves(document, prefix: str = "") -> dict:
    """`{dotted.path: value}` for every leaf. `None` is absent.

    A mapping with nothing set beneath it (empty, or every value `None`) is a leaf `{}`.
    """
    leaves = {}
    for key, value in document.items():
        path = f"{prefix}{key}"
        if value is None:
            continue
        if isinstance(value, dict):
            below = config_leaves(value, path + ".")
            leaves.update(below or {path: {}})
        else:
            leaves[path] = value
    return leaves


def effective_config(repo=None) -> tuple[dict, dict]:
    """The merged config for `repo` and, per leaf, the layer that set it.

    Layers, lowest first: `global` (`global_config_path()`), then `project`
    (`<repo>/project.yaml`). A project value equal to the global one is still sourced from
    the project; that is what the auto-slim removes.
    """
    project_path = PROJECT_YAML if repo is None else Path(repo) / "project.yaml"
    merged, sources = {}, {}
    for layer, document in (("global", load_global_config()),
                            ("project", _read_mapping(project_path))):
        merged = deep_merge(merged, document)
        sources.update({path: layer for path in config_leaves(document)})
    return merged, {path: sources[path] for path in config_leaves(merged)}


def unknown_keys(document) -> list[str]:
    """Top-level keys of `document` that no layer knows, sorted."""
    if not isinstance(document, dict):
        return []
    return sorted(str(key) for key in document if key not in KNOWN_KEYS)


def pilot_manifest(repo) -> Path | None:
    """The pilot manifest `repo` declares (`pilot.manifest`), or `None` when it is in no pilot.

    Read from the project's own `project.yaml` only: being in a pilot is a claim a project
    makes about itself, so it is never inherited from the global config. A relative path
    is relative to the project root.
    """
    pilot = _read_mapping(Path(repo) / "project.yaml").get("pilot")
    raw = pilot.get("manifest") if isinstance(pilot, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    path = Path(raw.strip()).expanduser()
    return path if path.is_absolute() else Path(repo) / path


# ── Project YAML helpers ──

def _load_project_yaml() -> dict:
    """`project.yaml` laid over the global config (DESIGN.md §17.1).

    With no global config the project document is returned exactly as it always was.
    """
    try:
        document = yaml.safe_load(PROJECT_YAML.read_text()) or {}
    except Exception:
        document = {}
    defaults = load_global_config()
    if not defaults:
        return document
    return deep_merge(defaults, document if isinstance(document, dict) else {})


#: Public spelling of the loader. The reference module only has the private name; both
#: refer to the same function object.
load_project_yaml = _load_project_yaml


# ── thresholds ──

def thresholds(document=None) -> dict:
    """`project.yaml`'s `thresholds:` block, `{}` for anything unusable.

    `maestro.quota` and `maestro.watchdog` both read this at import time, so "unusable"
    has to include the shapes a hand-edited YAML file produces — a null block, a scalar,
    a list. A module that raised on one of those would turn a typo in a config file into
    a package that cannot be imported at all, which is a strictly worse failure than the
    knob it was reading being ignored.
    """
    document = _load_project_yaml() if document is None else document
    if not isinstance(document, dict):
        return {}
    block = document.get("thresholds")
    return block if isinstance(block, dict) else {}


def threshold(name: str, default, *, cast=None, document=None):
    """One `thresholds:` value, coerced by `cast`, falling back to `default`.

    Same contract as `thresholds` and for the same reason: `five_h_pause_pct: ninety` is
    a mistake worth ignoring, never one worth refusing to start over. `None` is treated as
    absent rather than as a value, because that is what an emptied-out YAML key means.
    """
    value = thresholds(document).get(name)
    if value is None:
        return default
    if cast is None:
        return value
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


# ── runner selection (`D08`, P12A W0) ──

#: The two controller loops a project can run. Exactly one runs per project run
#: (MIGRATION_GUARDRAILS §2); `legacy` is the default until a project opts in.
RUNNERS = ("legacy", "graph")


class ConfigError(ValueError):
    """A `project.yaml` value that must not be guessed at."""


def sprint_config(document=None):
    """Effective persistent sprint settings; runtime skips never rewrite config."""
    from maestro.sprint import SprintConfig
    document = load_project_yaml() if document is None else document
    block = document.get('sprint')
    if block is None:
        block = {}
    if not isinstance(block, dict):
        raise ConfigError('sprint must be a mapping')
    if set(block) - set(SprintConfig.__dataclass_fields__):
        raise ConfigError('sprint contains unknown settings')
    try:
        return SprintConfig(**{key: value for key, value in block.items() if value is not None})
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def runaway_mode(document=None, *, task=None) -> str:
    """Read the effective mode for W5 evidence; this does not enable W13 work."""
    document = load_project_yaml() if document is None else document
    if task is not None:
        document = deep_merge(document, task)
    block = document.get('runaway')
    if block is None:
        return 'off'
    if not isinstance(block, dict):
        raise ConfigError('runaway must be a mapping')
    mode = block.get('mode')
    mode = 'off' if mode is None else mode
    if mode not in ('on', 'shadow', 'off'):
        raise ConfigError('runaway.mode must be on, shadow or off')
    return mode


def pace_estimates(document=None, *, task=None) -> dict:
    """Explicit initial pace estimates, before any eligible measured history exists.

    `*` is the default for pools without an override. All fields remain estimates;
    these values are never stored in W2's measured consumption columns.
    """
    from maestro.workflows.pace import PaceEstimate

    document = load_project_yaml() if document is None else document
    if task is not None:
        document = deep_merge(document, task)

    def mapping(value, path):
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ConfigError(f'{path} must be a mapping')
        return value

    routing = mapping(document.get('routing'), 'routing')
    block = mapping(routing.get('pace'), 'routing.pace')
    fields = {'initial_burn_pct_per_cost', 'initial_runtime_sec'}
    if set(block) - fields - {'pools'}:
        raise ConfigError('routing.pace contains unknown settings')
    pools = mapping(block.get('pools'), 'routing.pace.pools')
    if any(not isinstance(pool, str) or not pool or pool == '*' for pool in pools):
        raise ConfigError('routing.pace.pools keys must be nonempty pool ids')

    def estimate(values, path):
        values = mapping(values, path)
        if set(values) - fields:
            raise ConfigError(f'{path} contains unknown settings')

        def value(key, default):
            inherited = block.get(key)
            inherited = default if inherited is None else inherited
            supplied = values.get(key)
            return inherited if supplied is None else supplied

        try:
            return PaceEstimate(
                burn_pct_per_cost=value('initial_burn_pct_per_cost', 1.0),
                runtime_sec=value('initial_runtime_sec', 1800.0))
        except ValueError as exc:
            raise ConfigError(f'{path}: {exc}') from exc

    return {'*': estimate({}, 'routing.pace'), **{
        pool: estimate(values, f'routing.pace.pools.{pool}') for pool, values in pools.items()}}


def priority_weight(value) -> float | int:
    import math
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ConfigError('priority must be a positive finite number (1 = normal)')
    return value


def project_priority(document=None) -> float | int:
    document = load_project_yaml() if document is None else document
    defaults = document.get('defaults') or {}
    value = document.get('priority')
    if value is None:
        value = defaults.get('priority', 1) if isinstance(defaults, dict) else 1
    return priority_weight(value)


def set_project_priority(repo: Path, value) -> float | int:
    """Persist and commit only project.yaml; preserve comments and unrelated staged work."""
    import fcntl
    import subprocess
    import tempfile
    value = priority_weight(value)
    path = repo / 'project.yaml'
    lock = repo / '.orchestrator' / 'priority.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    def git(*args):
        result = subprocess.run(['git', '-C', str(repo), *args],
                                capture_output=True, text=True)
        if result.returncode:
            raise ConfigError('priority Git operation failed; project config was not committed')
        return result.stdout
    with lock.open('a') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if git('status', '--porcelain', '--', 'project.yaml').strip():
            raise ConfigError('project.yaml is dirty; commit its existing changes first')
        if not git('ls-files', '--', 'project.yaml').strip():
            raise ConfigError('project.yaml must be tracked before changing priority')
        original = path.read_text()
        node = yaml.compose(original)
        if not isinstance(node, yaml.MappingNode):
            raise ConfigError('project.yaml must be a mapping')
        entries = [v for k, v in node.value if k.value == 'priority']
        if len(entries) > 1:
            raise ConfigError('duplicate priority keys in project.yaml')
        rendered = str(value)
        if entries:
            item = entries[0]
            if not isinstance(item, yaml.ScalarNode):
                raise ConfigError('existing priority must be a scalar')
            changed = original[:item.start_mark.index] + rendered + original[item.end_mark.index:]
        else:
            if node.flow_style:
                raise ConfigError('use block-style project.yaml before adding priority')
            changed = original.rstrip('\n') + '\npriority: ' + rendered + '\n'
        if changed == original:
            return value
        # Alias nodes share their anchor's source marks. Replacing that span can
        # remove an anchor used elsewhere, so validate before writing or committing.
        try:
            updated = yaml.safe_load(changed)
            if not isinstance(updated, dict) or priority_weight(updated.get('priority')) != value:
                raise ConfigError('priority replacement did not set the requested value')
        except (yaml.YAMLError, ConfigError) as exc:
            raise ConfigError('use a literal numeric priority before changing it; '
                              'this update would invalidate project.yaml') from exc
        def replace(text):
            fd, raw = tempfile.mkstemp(prefix='.project-priority-', dir=repo)
            tmp = Path(raw)
            try:
                os.fchmod(fd, path.stat().st_mode & 0o777)
                with os.fdopen(fd, 'w') as stream:
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)
        replace(changed)
        try:
            git('commit', '--only', '-m', f'chore(config): set project priority to {value}',
                '--', 'project.yaml')
        except Exception:
            replace(original)
            raise
    return value


def _line_end(text: str, index: int) -> int:
    """The index just past the newline ending the line that holds `index` (or `len`)."""
    found = text.find("\n", index)
    return len(text) if found < 0 else found + 1


def _last_scalar_end(node) -> int:
    """Where the last scalar beneath `node` ends in the source."""
    while not isinstance(node, yaml.ScalarNode):
        if not node.value:
            return node.end_mark.index
        node = node.value[-1][1] if isinstance(node, yaml.MappingNode) else node.value[-1]
    return node.end_mark.index


def _leaf_text(original: str, path: tuple, rendered: str) -> str:
    """`original` with the scalar at `path` set to `rendered`, every other byte kept. A
    missing mapping or leaf is inserted under its block-style parent; a missing top-level
    key is appended. Flow style, aliases and anchors are refused."""
    def fresh(keys, indent):
        lines = []
        for key in keys[:-1]:
            lines.append(" " * indent + f"{key}:")
            indent += 2
        return "\n".join(lines + [" " * indent + f"{keys[-1]}: {rendered}"]) + "\n"

    try:
        events = list(yaml.parse(original))
        node = yaml.compose(original)
    except yaml.YAMLError as exc:
        raise ConfigError(f"the global config does not parse: {exc}") from exc
    if node is None:                       # no file, an empty one, or only comments
        return original + ("" if not original or original.endswith("\n") else "\n") + fresh(
            path, 0)
    if any(isinstance(e, yaml.AliasEvent) or getattr(e, "anchor", None) for e in events):
        raise ConfigError("the global config uses YAML anchors or aliases; edit it by hand")
    if not isinstance(node, yaml.MappingNode) or node.flow_style:
        raise ConfigError("the global config must be a block-style mapping")
    indent = 0
    for depth, key in enumerate(path):
        entries = [(k, v) for k, v in node.value if k.value == key]
        if len(entries) > 1:
            raise ConfigError(f"duplicate {'.'.join(path[:depth + 1])} keys in the global config")
        last = depth == len(path) - 1
        if not entries:
            # Insert the rest of the path under `node`, after the line of its last scalar
            # (so a comment on that line stays, and one above the next key stays there).
            indent = node.value[0][0].start_mark.column
            at = _line_end(original, _last_scalar_end(node))
            prefix = "" if at == 0 or original[at - 1] == "\n" else "\n"
            return original[:at] + prefix + fresh(path[depth:], indent) + original[at:]
        value = entries[0][1]
        if last:
            if not isinstance(value, yaml.ScalarNode):
                raise ConfigError(f"{'.'.join(path)} must be a scalar in the global config")
            return (original[:value.start_mark.index] + rendered
                    + original[value.end_mark.index:])
        if not isinstance(value, yaml.MappingNode) or value.flow_style or not value.value:
            raise ConfigError(f"{'.'.join(path[:depth + 1])} must be a block-style mapping in "
                              "the global config")
        node = value
    raise ConfigError("unreachable")  # pragma: no cover


def global_config_lock(target: Path | None = None):
    """The `fcntl` lock beside the global config (or beside `target`, a config file an
    improvement once applied to) that every GDT-12 write of it holds: a context manager
    yielding the file. Held by `set_global_leaf` and by an improvement rollback's byte
    restore (review L2). Not re-entrant: never nest it with a `set_global_leaf` call."""
    import contextlib
    import fcntl

    @contextlib.contextmanager
    def held_lock():
        nonlocal target
        target = Path(target) if target is not None else global_config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with (target.parent / f".{target.name}.lock").open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            try:
                yield target
            finally:
                fcntl.flock(held, fcntl.LOCK_UN)

    return held_lock()


def set_global_leaf(path: tuple, value, *, check=None) -> tuple[str, str]:
    """Set one scalar leaf of the global config (`global_config_path()`), keeping every
    other byte, comments included; returns `(before_text, after_text)`. Written atomically
    under `global_config_lock()`; the result is re-parsed and must hold `value` at `path`
    with every other value unchanged. `before_text` is `""` when there was no file
    (GDT-12: an approved improvement's config lever, and its rollback). `check`, when
    given, is called with the text read under the lock before anything is written; it
    refuses the edit by raising (an approval re-checks the leaf there, review M3)."""
    import tempfile

    path = tuple(str(p) for p in path)
    if not path or any(not re.fullmatch(r"[A-Za-z0-9_]+", p) for p in path):
        raise ConfigError(f"not a config leaf path: {path!r}")
    rendered = str(value)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", rendered):
        raise ConfigError(f"refusing to write {value!r}: only a plain scalar")
    with global_config_lock() as target:
        try:
            original = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            original = ""
        if check is not None:
            check(original)
        changed = _leaf_text(original, path, rendered)
        try:
            before = yaml.safe_load(original) or {}
            after = yaml.safe_load(changed)
        except yaml.YAMLError as exc:
            raise ConfigError(f"the edit would invalidate the global config: {exc}") from exc
        if not isinstance(before, dict):
            raise ConfigError("the global config must be a mapping")
        expected = copy.deepcopy(before)
        cursor = expected
        for key in path[:-1]:
            cursor = cursor.setdefault(key, {})
        cursor[path[-1]] = yaml.safe_load(rendered)
        if after != expected:
            raise ConfigError(f"setting {'.'.join(path)} would change other values; edit the "
                              "global config by hand")
        if changed == original:
            return original, changed
        fd, raw = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        tmp = Path(raw)
        try:
            if target.exists():
                os.fchmod(fd, target.stat().st_mode & 0o777)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(changed)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
    return original, changed


def engineering_runner(document=None) -> str:
    """`project.yaml`'s `engineering.runner`: `legacy` (default) or `graph`.

    Strict where `thresholds` is lenient, for the reason `workflow_policy_path` gives: a
    typo that silently picked a controller loop would run the wrong one against live work.
    An absent block or key is the default; a block that is not a mapping, or a value that
    is not one of `RUNNERS`, raises. An unreadable file has already collapsed to `{}` in
    `_load_project_yaml`, and so reads as the default.
    """
    document = _load_project_yaml() if document is None else document
    if not isinstance(document, dict):
        return "legacy"
    block = document.get("engineering")
    if block is None:
        return "legacy"
    if not isinstance(block, dict):
        raise ConfigError(
            f"project.yaml `engineering:` must be a mapping, got {type(block).__name__}"
        )
    value = block.get("runner")
    if value is None:
        return "legacy"
    if value not in RUNNERS:
        raise ConfigError(
            f"project.yaml engineering.runner={value!r} is not one of {', '.join(RUNNERS)}"
        )
    return value


#: `engineering.affected_checks`: whether affected-check selection only observes (`shadow`,
#: the full suite still runs) or narrows the suite (`authoritative`). P12A W5/W6.
AFFECTED_CHECK_MODES = ("shadow", "authoritative")


def affected_checks_mode(document=None) -> str:
    """`project.yaml`'s `engineering.affected_checks`: `shadow` (default) or `authoritative`.

    Strict for the same reason as `engineering_runner`: a typo that silently picked a mode
    would decide which checks run. `authoritative` here is only a request — the graph loop
    grants it when the stored shadow comparisons qualify (`validation.qualify_affected`).
    """
    document = _load_project_yaml() if document is None else document
    if not isinstance(document, dict):
        return "shadow"
    block = document.get("engineering")
    if block is None:
        return "shadow"
    if not isinstance(block, dict):
        raise ConfigError(
            f"project.yaml `engineering:` must be a mapping, got {type(block).__name__}"
        )
    value = block.get("affected_checks")
    if value is None:
        return "shadow"
    if value not in AFFECTED_CHECK_MODES:
        raise ConfigError(
            f"project.yaml engineering.affected_checks={value!r} is not one of "
            f"{', '.join(AFFECTED_CHECK_MODES)}"
        )
    return value


#: `engineering.max_plan_repairs` when absent: one repair, the escalation ladder's one
#: final run (Dan's D-FAIL-2, 2026-09-16). P12A W7.
DEFAULT_MAX_PLAN_REPAIRS = 1


def max_plan_repairs(document=None) -> int:
    """`project.yaml`'s `engineering.max_plan_repairs`: how many plan repairs a task may use.

    Absent means `DEFAULT_MAX_PLAN_REPAIRS`. Strict like `engineering_runner`: a negative or
    non-integer quota is a `ConfigError`, never a guess (a bool is not an integer here).
    """
    document = _load_project_yaml() if document is None else document
    if not isinstance(document, dict):
        return DEFAULT_MAX_PLAN_REPAIRS
    block = document.get("engineering")
    if block is None:
        return DEFAULT_MAX_PLAN_REPAIRS
    if not isinstance(block, dict):
        raise ConfigError(
            f"project.yaml `engineering:` must be a mapping, got {type(block).__name__}"
        )
    value = block.get("max_plan_repairs")
    if value is None:
        return DEFAULT_MAX_PLAN_REPAIRS
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(
            f"project.yaml engineering.max_plan_repairs={value!r} must be a "
            "non-negative integer"
        )
    return value


#: `retention:` when absent (P12, B15). Nothing is pruned: A03 keeps raw per-task outcomes
#: for B01/B18, and no pruning path exists, so `prune: true` is refused rather than ignored.
#: `warn_bytes` is when `maestro doctor` warns about `.orchestrator` (task trees excluded).
RETENTION_DEFAULTS = {"prune": False, "warn_bytes": 1 << 30}
RETENTION_BASIS = (
    "P12 real DuetFlow pilot (2026-09-24..2026-10-08, 11 accepted tasks): .orchestrator held "
    "84.1 MB, 7.6 MB per accepted task, of which 75.9 MB is 18 operator-archived failed runs "
    "(largest 15.1 MB). Without the archives it is 2.0 MB per accepted task: control DB + WAL "
    "5.0 MB in all, attempts 44 KB to 1.6 MB per run. 1 GiB covers about 140 accepted tasks "
    "at the archive-heavy rate. The fixture qualification (p12-qualification.json) measured "
    "2.09 MB per run."
)


def retention(document=None) -> dict:
    """`project.yaml`'s `retention:` block, defaults filled in. Strict like the rest."""
    document = _load_project_yaml() if document is None else document
    block = document.get("retention") if isinstance(document, dict) else None
    if block is None:
        return dict(RETENTION_DEFAULTS)
    if not isinstance(block, dict):
        raise ConfigError(f"project.yaml `retention:` must be a mapping, got {type(block).__name__}")
    out = dict(RETENTION_DEFAULTS)
    prune = block.get("prune", False)
    if prune is not False:
        raise ConfigError(
            f"project.yaml retention.prune={prune!r}: evidence is never pruned automatically "
            "(A03); only `false` is accepted"
        )
    warn = block.get("warn_bytes", out["warn_bytes"])
    if isinstance(warn, bool) or not isinstance(warn, int) or warn < 0:
        raise ConfigError(f"project.yaml retention.warn_bytes={warn!r} must be a non-negative integer")
    out["warn_bytes"] = warn
    return out


#: P12B F4/F5 defaults: how long an auth failure pauses its pool, and how long a node may
#: wait on a pool whose reset is unknown before it parks.
DEFAULT_AUTH_FAILURE_PAUSE_SEC = 3600
DEFAULT_MAX_BLOCKED_WAIT_SEC = 86400


def _positive_engineering_int(document, key: str, default: int) -> int:
    document = _load_project_yaml() if document is None else document
    if not isinstance(document, dict):
        return default
    block = document.get("engineering")
    if block is None:
        return default
    if not isinstance(block, dict):
        raise ConfigError(
            f"project.yaml `engineering:` must be a mapping, got {type(block).__name__}"
        )
    value = block.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(
            f"project.yaml engineering.{key}={value!r} must be a positive integer"
        )
    return value


def auth_failure_pause_sec(document=None) -> int:
    """`engineering.auth_failure_pause_sec`: seconds an `auth_failed` pauses its pool."""
    return _positive_engineering_int(
        document, "auth_failure_pause_sec", DEFAULT_AUTH_FAILURE_PAUSE_SEC
    )


def max_blocked_wait_sec(document=None) -> int:
    """`engineering.max_blocked_wait_sec`: the longest wait on an unknown reset (P12B F5)."""
    return _positive_engineering_int(
        document, "max_blocked_wait_sec", DEFAULT_MAX_BLOCKED_WAIT_SEC
    )


#: `engineering.free_tier_endpoints` row keys: required, then optional.
FREE_TIER_REQUIRED = ("endpoint_id", "base_url", "model", "api_key_env", "verified_version")
FREE_TIER_OPTIONAL = ("strength", "usage_pool_id")


def free_tier_endpoints(document=None) -> list[dict]:
    """`project.yaml`'s `engineering.free_tier_endpoints` (P12A W1): `[]` when absent.

    Strict for the same reason as `engineering_runner`: an endpoint row the loop half-read
    would catalogue a route nobody verified. Each row is a mapping of non-empty strings with
    `FREE_TIER_REQUIRED` keys and optionally `FREE_TIER_OPTIONAL`; anything else raises.
    """
    document = _load_project_yaml() if document is None else document
    if not isinstance(document, dict):
        return []
    block = document.get("engineering")
    if block is None:
        return []
    if not isinstance(block, dict):
        raise ConfigError(
            f"project.yaml `engineering:` must be a mapping, got {type(block).__name__}"
        )
    rows = block.get("free_tier_endpoints")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ConfigError(
            "project.yaml engineering.free_tier_endpoints must be a list, "
            f"got {type(rows).__name__}"
        )
    endpoints = []
    for index, row in enumerate(rows):
        where = f"engineering.free_tier_endpoints[{index}]"
        if not isinstance(row, dict):
            raise ConfigError(f"project.yaml {where} must be a mapping, got {type(row).__name__}")
        unknown = sorted(set(row) - set(FREE_TIER_REQUIRED) - set(FREE_TIER_OPTIONAL))
        if unknown:
            raise ConfigError(f"project.yaml {where} has unknown keys {unknown}")
        missing = [key for key in FREE_TIER_REQUIRED if key not in row]
        if missing:
            raise ConfigError(f"project.yaml {where} is missing {missing}")
        for key, value in row.items():
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"project.yaml {where}.{key} must be a non-empty string")
        endpoints.append(dict(row))
    return endpoints


# ── engineering policy (`[P5]`) ──

WORKFLOWS_DIR         = REPO / "workflows"
WORKFLOW_POLICY_YAML  = WORKFLOWS_DIR / "project.yaml"

#: Where `maestro init` records the project profile it derived from tracked manifests
#: (`maestro.workflows.discovery`). Runtime state, so it lives under `.orchestrator/`
#: with the rest of it and is never committed.
PROJECT_PROFILE_JSON  = REPO / ".orchestrator" / "project_profile.json"


def workflow_policy_path(repo=None):
    """`<repo>/workflows/project.yaml` — the engineering policy, not the operational one.

    A path, deliberately not a loader. Everything else in this module collapses a broken
    file to `{}` because an unusable quota threshold is better ignored than fatal; the
    engineering policy is the opposite case — a review requirement that vanishes because
    of a typo is exactly the failure `maestro.workflows.policy` refuses to have, so it
    does its own reading and raises. This function exists so the location is written down
    once, next to every other project path.
    """
    return (Path(repo) if repo is not None else REPO) / "workflows" / "project.yaml"
