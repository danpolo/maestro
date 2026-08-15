"""Per-model context-ceiling lookup (DESIGN.md §8, D4).

New code, not extracted from the reference implementation — the reference orchestrator
has no notion of its own context budget, so there is nothing to keep behaviour-identical
to here (see `docs/plans/2026-08-16-m3-limits-selfupdate.md`'s "Already done this
session" note for the full finding).

Both `~/.claude/model_context_limits.md` and `~/.codex/model_context_limits.md` share one
markdown table shape:

    | Model | Prepare handoff | Normally start fresh | Exception ceiling |

This module parses that shape (tolerating the minor formatting differences already
observed between the two real files — alignment colons, a bold cell, `K`/`k` suffixes,
en-dash or hyphen ranges), merges both tables into one `{model: ModelLimits}` map, caches
the merge to `.orchestrator/model_limits.json` for other consumers, and never raises for
a missing or unparseable file — a machine lacking these files degrades to shipped
defaults, never a hard failure.
"""
from __future__ import annotations

import json
import re
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path

from maestro import config as _config
from maestro.paths import Paths

_PATHS = Paths.from_env()

#: Cache target. Module-level so tests can `monkeypatch.setattr(limits, "MODEL_LIMITS_JSON", ...)`
#: the same way `switch.py`/`quota.py` tests repoint their own path globals.
MODEL_LIMITS_JSON = _PATHS.model_limits


@dataclass(frozen=True)
class ModelLimits:
    """One model's context-ceiling row, in raw token counts (e.g. ``90-100K`` becomes
    ``prepare_handoff_low=90_000, prepare_handoff_high=100_000``)."""

    model: str
    prepare_handoff_low: int
    prepare_handoff_high: int
    normally_fresh_low: int
    normally_fresh_high: int
    exception_ceiling: int


@dataclass
class LimitsResult:
    """The merged outcome of `load_limits()`: every model resolved, plus every
    non-fatal problem hit along the way (missing files, unparseable rows, falling
    back to shipped defaults). Never raised — always returned."""

    models: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


# Best-effort fallback for a machine that has neither table file. Mirrors the values
# shipped in this build's own ~/.claude and ~/.codex tables at the time this module was
# written; used only when zero real rows were parsed from any configured path.
_SHIPPED_DEFAULTS: dict = {
    "Claude Sonnet 5": ModelLimits("Claude Sonnet 5", 90_000, 100_000, 120_000, 140_000, 180_000),
    "Claude Opus 5": ModelLimits("Claude Opus 5", 100_000, 120_000, 150_000, 180_000, 240_000),
    "GPT-5.6 Luna": ModelLimits("GPT-5.6 Luna", 70_000, 90_000, 100_000, 120_000, 150_000),
    "GPT-5.6 Terra": ModelLimits("GPT-5.6 Terra", 100_000, 120_000, 140_000, 170_000, 220_000),
    "GPT-5.6 Sol": ModelLimits("GPT-5.6 Sol", 120_000, 140_000, 180_000, 220_000, 280_000),
}


# ── table parsing ──

_BOLD_CELL_RE = re.compile(r"^\*\*(.+)\*\*$")
_SEPARATOR_CELL_RE = re.compile(r"^:?-+:?$")
# "90-100K" / "90–100K" / "70K-90K" / "70K – 90K" — first number's own "K" is optional
# since both real files omit it on the range's low end.
_RANGE_RE = re.compile(r"^(\d+)\s*[Kk]?\s*[-–—]\s*(\d+)\s*[Kk]?$")
# "180K" / "180k" / "180000"
_SINGLE_RE = re.compile(r"^(\d+)\s*[Kk]?$")


def _strip_cell(cell: str) -> str:
    cell = cell.strip()
    m = _BOLD_CELL_RE.match(cell)
    return m.group(1).strip() if m else cell


def _to_tokens(raw: str) -> int:
    # Every number in these tables is expressed in K (thousands) of tokens, whether or
    # not that particular number carries its own "K" suffix (the range's low end often
    # doesn't — "90-100K" means 90K-100K, not 90-100K literal tokens).
    return int(raw) * 1000


def _parse_range(cell: str) -> tuple:
    m = _RANGE_RE.match(cell.strip())
    if not m:
        raise ValueError(f"not a token range: {cell!r}")
    return _to_tokens(m.group(1)), _to_tokens(m.group(2))


def _parse_single(cell: str) -> int:
    m = _SINGLE_RE.match(cell.strip())
    if not m:
        raise ValueError(f"not a token count: {cell!r}")
    return _to_tokens(m.group(1))


def _parse_rows(text: str) -> tuple:
    """Internal engine shared by `parse_limits_table` and `load_limits`. Returns
    `(models, bad_lines)` — `bad_lines` are structurally-a-row (four `|`-delimited
    cells, not the header, not the alignment separator) but numerically unparseable,
    which `load_limits` turns into a warning; `parse_limits_table`'s public signature
    has no room for that, so it just drops them."""
    models: dict = {}
    bad_lines: list = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        cells = [_strip_cell(c) for c in line.strip("|").split("|")]
        if len(cells) != 4:
            continue
        if not cells[0]:
            continue
        if cells[0].lower() == "model":
            continue  # header row
        if all(_SEPARATOR_CELL_RE.match(c) for c in cells):
            continue  # alignment separator row (---|---:|---:|---:)
        try:
            prep_low, prep_high = _parse_range(cells[1])
            fresh_low, fresh_high = _parse_range(cells[2])
            ceiling = _parse_single(cells[3])
        except ValueError:
            bad_lines.append(line)
            continue
        models[cells[0]] = ModelLimits(
            cells[0], prep_low, prep_high, fresh_low, fresh_high, ceiling
        )
    return models, bad_lines


def parse_limits_table(text: str) -> dict:
    """Parse one `model_context_limits.md`-shaped table into `{model: ModelLimits}`.
    Unparseable rows are silently skipped here — `load_limits` is the layer that turns
    that into a warning, since this function's return type has no room to carry one."""
    models, _bad_lines = _parse_rows(text)
    return models


# ── path resolution ──

def default_table_paths() -> list:
    """`~/.claude/model_context_limits.md` and `~/.codex/model_context_limits.md`,
    unless `project.yaml` sets `model_limits_paths: [...]`, in which case that list
    (each entry `~`-expanded) replaces the two defaults entirely."""
    cfg = _config.load_project_yaml()
    override = cfg.get("model_limits_paths")
    if override:
        return [Path(p).expanduser() for p in override]
    return [
        Path.home() / ".claude" / "model_context_limits.md",
        Path.home() / ".codex" / "model_context_limits.md",
    ]


# ── load + cache ──

def _write_cache(models: dict) -> None:
    """Best-effort. Other consumers can read `.orchestrator/model_limits.json` without
    importing this module; a cache-write failure must never fail `load_limits()`
    itself, so any error here is swallowed."""
    try:
        MODEL_LIMITS_JSON.parent.mkdir(parents=True, exist_ok=True)
        payload = {name: asdict(ml) for name, ml in models.items()}
        tmp = MODEL_LIMITS_JSON.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        tmp.rename(MODEL_LIMITS_JSON)
    except OSError:
        pass


def load_limits(paths=None) -> LimitsResult:
    """Read, parse and merge every path in `paths` (default: `default_table_paths()`)
    into one `{model: ModelLimits}` map, later paths winning on a duplicate model name.
    Never raises: a missing file, an unreadable file or an unparseable row all degrade
    to a warning on the returned `LimitsResult`, and if zero real rows were parsed from
    any path the shipped defaults are used instead. Always writes the merged result to
    `.orchestrator/model_limits.json` (cache write only — this function always re-parses
    from source; the cache is for other consumers that don't want to import this
    module)."""
    if paths is None:
        paths = default_table_paths()

    models: dict = {}
    warns: list = []
    any_existing = False

    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_file():
            warns.append(f"model limits file missing: {path}")
            continue
        any_existing = True
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            warns.append(f"could not read model limits file {path}: {exc}")
            continue
        parsed, bad_lines = _parse_rows(text)
        if not parsed and not bad_lines:
            warns.append(f"{path} has no model-limits table rows")
        for bad in bad_lines:
            warns.append(f"{path} has an unparseable row: {bad!r}")
        models.update(parsed)  # later path wins on a duplicate model name

    if not paths:
        warns.append("no model limits table paths configured")
    elif not any_existing:
        warns.append("no table found at any configured path")

    if not models:
        warns.append(
            "no real model-limits entries parsed from any path — using shipped defaults"
        )
        models = dict(_SHIPPED_DEFAULTS)

    _write_cache(models)
    return LimitsResult(models=models, warnings=warns)


# ── per-model resolution ──

def resolve(model: str, paths=None):
    """The `doctor`-style per-model lookup DESIGN.md §8 describes: `None` when `model`
    is in neither table and has no shipped default, with a `UserWarning` raised (via
    the stdlib `warnings` module — this function's own return type has no room for a
    warnings list) so a caller that isn't checking for `None` still gets a visible
    signal."""
    result = load_limits(paths)
    entry = result.models.get(model)
    if entry is None:
        warnings.warn(f"limits.resolve: no context-limit entry for model {model!r}", stacklevel=2)
    return entry


def resolve_all(model_names, paths=None) -> dict:
    """Resolve a batch of model names in one parse/merge pass. This stands in for the
    `doctor` CLI command's per-model report until M4 builds `doctor` itself — it is a
    library function, not `doctor`, and should not be mistaken for it. Each name not
    present in the merged tables (and without a shipped default) resolves to `None`
    with a `UserWarning` raised, same as `resolve`."""
    result = load_limits(paths)
    out: dict = {}
    for name in model_names:
        entry = result.models.get(name)
        if entry is None:
            warnings.warn(
                f"limits.resolve_all: no context-limit entry for model {name!r}", stacklevel=2
            )
        out[name] = entry
    return out
