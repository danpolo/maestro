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
from typing import Mapping

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

    def get(self, model: str):
        """Look one model up with the same both-spellings tolerance `resolve()` has, so a
        caller holding a `LimitsResult` doesn't have to know whether it's carrying a display
        name (`"Claude Opus 5"`) or a `project.yaml` slug (`claude-opus-5`). `None` when
        neither spelling is present; `.models` remains the raw display-name-keyed map."""
        return _lookup(self.models, model)


# ── the tables as the single source of truth (C7 part 1) ──
#
# `roles.DEFAULT_MODELS` and `_SHIPPED_DEFAULTS` used to each carry their own copy of
# which models the implementer/judge/diagnoser default to — two more places than the
# tables themselves, so renaming one meant four edits (the table row, both of
# `roles.py`'s two entries that shared it, and this dict's key) across three places.
# These three constants are now the *one* place that decision is written down, spelled
# exactly as the tables spell them (their own convention, DESIGN.md §8) — `roles.py`
# derives its project.yaml-shaped slug from them via `model_slug`, below, rather than
# hardcoding a second copy, and `_SHIPPED_DEFAULTS` keys directly off them.
DEFAULT_IMPLEMENTER_MODEL_NAME = "Claude Sonnet 5"
DEFAULT_JUDGE_MODEL_NAME = "Claude Sonnet 5"
DEFAULT_DIAGNOSER_MODEL_NAME = "Claude Opus 5"

# Best-effort fallback for a machine that has neither table file — the tables are the
# single source of truth *when either is readable*; this is only what is left over when
# neither is. Its only job is making sure `roles.DEFAULT_MODELS`'s own defaults still
# resolve to *something* even then, so it is keyed on exactly the three constants above
# rather than mirroring every row either real table happens to carry today (GPT-5.6
# Terra/Sol, Haiku 4.5, ...) — that broader ambition is what made this dict a second
# place to edit every time an operator's table changed, the exact defect this item
# exists to close. The numbers themselves are deliberately the tables' own documented
# fallback for a genuinely unknown model (see the last line of both real files: "100K
# warn / 120K evaluate / 150K normal max") rather than a claim to still know any one
# model's real ceiling without being able to read either table.
_SHIPPED_DEFAULTS: dict = {
    name: ModelLimits(name, 90_000, 100_000, 100_000, 120_000, 150_000)
    for name in {
        DEFAULT_IMPLEMENTER_MODEL_NAME,
        DEFAULT_JUDGE_MODEL_NAME,
        DEFAULT_DIAGNOSER_MODEL_NAME,
    }
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
    """`~/.claude/model_context_limits.md` and `~/.codex/model_context_limits.md`, unless
    `project.yaml` sets `model_limits: {<backend>: <path>, ...}` (DESIGN.md §5, §8,
    `project.yaml.tmpl`) — a mapping, not a flat list — in which case its values
    (`~`-expanded, in declaration order) replace the two defaults entirely.

    `model_limits_paths: [...]` — an earlier, flat-list spelling that predates the
    documented mapping shape — is no longer read. A project.yaml that still carries it
    would otherwise silently do nothing (the exact class of bug this reconciles), so its
    presence raises a `UserWarning` naming the correct key and is then ignored, falling
    through to `model_limits` (if also set) or the two defaults.

    A `model_limits` that *is* present but is not a usable mapping — the deleted list
    shape reused under the new key, a scalar, `null`, or an empty `{}` — degrades the
    same way: a `UserWarning` and the two defaults, never silent, since "knob set, no
    effect" is the exact defect class this function exists to close. An empty `{}` is
    deliberately treated as unusable rather than as "no override": a project.yaml that
    wants the defaults simply omits the key, so an explicit-but-empty mapping is far more
    likely a mistake (an accidentally cleared value) than an intentional opt-in.
    """
    cfg = _config.load_project_yaml()
    if "model_limits_paths" in cfg:
        warnings.warn(
            "project.yaml sets 'model_limits_paths' (a flat list), which maestro no "
            "longer reads and has no effect — rename it to "
            "'model_limits: {<backend>: <path>, ...}' (DESIGN.md §5, §8)",
            stacklevel=2,
        )
    if "model_limits" in cfg:
        override = cfg.get("model_limits")
        paths = (
            [Path(p).expanduser() for p in override.values() if isinstance(p, str) and p.strip()]
            if isinstance(override, Mapping)
            else []
        )
        if paths:
            return paths
        warnings.warn(
            "project.yaml's 'model_limits' is not a usable "
            f"'{{<backend>: <path>, ...}}' mapping (got {override!r}) and has no "
            "effect — see 'model_limits: {<backend>: <path>, ...}' (DESIGN.md §5, §8)",
            stacklevel=2,
        )
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


# ── model-name normalisation ──

#: A model id's release-date suffix (`-20251001`), stripped after folding so the
#: dated id a CLI actually reports (`claude-haiku-4-5-20251001`) matches the table row
#: for the model family, which carries no date (R8, 2026-08-31 finding, wave 1).
_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")

#: Any run of whitespace, `.` or `-` — the three separators seen across both tables and
#: `project.yaml`'s slugs — folded to one `-`. Periods and hyphens must fold to the same
#: character, not just each other: the tables key by display name, where whitespace is
#: the only separator (`"Claude Haiku 4.5"`), while `project.yaml`'s slugs use `-`
#: throughout, including where the display name would keep a version number's own `.`
#: (`claude-haiku-4-5` vs. `"Claude Haiku 4.5"`). GPT's `gpt-5.6-terra` (DESIGN.md §5)
#: already mixes both spellings in one slug, which is what proves this must be a single
#: separator class, not two independent substitutions.
_SEPARATOR_RUN_RE = re.compile(r"[\s.-]+")


def _normalise_model_key(name: str) -> str:
    """Fold a model name to the one spelling the tables and `project.yaml` agree on.

    The two table files key by human display name (`"Claude Opus 5"`, `"GPT-5.6 Terra"`)
    while `project.yaml`'s `roles:` block names models by machine slug (`claude-opus-5`,
    `gpt-5.6-terra`, per DESIGN.md §5's example), so a literal lookup resolves nothing on
    a real project (M4 finding #2). Casefolding and collapsing whitespace/`.`/`-` runs to
    a single `-` closes that gap, with no mapping table to maintain: a model added to
    either file works with no code change here.

    **R8 (2026-08-31 finding, wave 1).** The original version of this fold only joined
    whitespace runs, which left two gaps open on the one model actually shaped
    differently: `claude-haiku-4-5-20251001` (the id maestro configures and the CLI
    reports) folded to itself, matching neither `"Claude Haiku 4.5"`'s
    `claude-haiku-4.5` (a `.`, not the configured `-`) nor even the un-dated
    `claude-haiku-4-5`. Folding `.`/`-`/whitespace to one separator closes the first gap;
    stripping a trailing 8-digit release-date suffix closes the second. Neither change
    widens what *matches* a bare display name like `"Opus 5"` against `"Claude Opus 5"`
    — that gap is the missing `"Claude "` prefix, a different defect this function
    deliberately leaves alone (`docs/plans/2026-08-30-pre-integration-c7-report.md`;
    `tests/test_switch.py` pins it).

    Idempotent — a name that is already normalised folds to itself, and re-normalising an
    already-normalised name changes nothing (there is no second date suffix to strip, and
    no separator run left to collapse)."""
    folded = _SEPARATOR_RUN_RE.sub("-", name.casefold()).strip("-")
    return _DATE_SUFFIX_RE.sub("", folded)


def model_slug(name: str) -> str:
    """Public entry point onto `_normalise_model_key`, for a caller outside this module
    that wants a `project.yaml`-shaped slug from one of the tables' own display names
    (`roles.py` uses this to derive `DEFAULT_MODELS` from `DEFAULT_IMPLEMENTER_MODEL_NAME`
    et al. instead of hardcoding its own copy). Pure string folding — no file IO, no
    subprocess, nothing a caller with its own "no IO" purity claim needs to disclose."""
    return _normalise_model_key(name)


def _lookup(models: dict, name: str):
    """`models[name]`, tolerant of display-name/slug spelling, or `None`.

    An exact hit always wins, so a table that ever keys by slug directly keeps resolving
    against its own keys untouched; only when there is no exact hit is the normalised form
    compared. The value returned is whatever the table stored, so `ModelLimits.model` still
    carries the table's own (display) spelling even for a slug lookup."""
    entry = models.get(name)
    if entry is not None:
        return entry
    wanted = _normalise_model_key(name)
    for key, value in models.items():
        if _normalise_model_key(key) == wanted:
            return value
    return None


# ── per-model resolution ──

def resolve(model: str, paths=None):
    """The `doctor`-style per-model lookup DESIGN.md §8 describes: `None` when `model`
    is in neither table and has no shipped default, with a `UserWarning` raised (via
    the stdlib `warnings` module — this function's own return type has no room for a
    warnings list) so a caller that isn't checking for `None` still gets a visible
    signal. `model` may be spelled either as the tables' display name or as a
    `project.yaml` slug (see `_normalise_model_key`)."""
    result = load_limits(paths)
    entry = _lookup(result.models, model)
    if entry is None:
        warnings.warn(
            f"limits.resolve: no context-limit entry for model {model!r} "
            f"(also tried normalised {_normalise_model_key(model)!r})",
            stacklevel=2,
        )
    return entry


def resolve_all(model_names, paths=None) -> dict:
    """Resolve a batch of model names in one parse/merge pass. This stands in for the
    `doctor` CLI command's per-model report until M4 builds `doctor` itself — it is a
    library function, not `doctor`, and should not be mistaken for it. Each name not
    present in the merged tables (and without a shipped default) resolves to `None`
    with a `UserWarning` raised, same as `resolve`. Names may be spelled either as the
    tables' display names or as `project.yaml` slugs, mixed freely; the returned dict is
    keyed by the names as passed in, so a caller gets its own spelling back."""
    result = load_limits(paths)
    out: dict = {}
    for name in model_names:
        entry = _lookup(result.models, name)
        if entry is None:
            warnings.warn(
                f"limits.resolve_all: no context-limit entry for model {name!r} "
                f"(also tried normalised {_normalise_model_key(name)!r})",
                stacklevel=2,
            )
        out[name] = entry
    return out
