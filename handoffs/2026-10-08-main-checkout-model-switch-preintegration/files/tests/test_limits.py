"""Per-model context-ceiling lookup (DESIGN.md §8, D4) — `maestro.limits`.

`maestro/limits.py` is new M3 behaviour, not an extraction, so these tests live here
rather than under `tests/characterization/` and exercise real behaviour end to end
(ordinary TDD-style tests are correct here, unlike M0-M2's characterization pinning).

`test_resolve_all_against_the_real_shipped_tables` is the stand-in the M3 plan asks for:
until M4 builds the `doctor` CLI command, this test *is* "doctor resolves every model in
both tables" — `resolve_all()` is a library function, not `doctor` itself.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from maestro import limits

# Pinned copies of Dan's live tables (taken 2026-09-13). The live files are edited as
# model guidance changes, so only `test_the_live_agent_tables_parse_cleanly` reads them.
FIXTURES = Path(__file__).parent / "fixtures" / "model_limits"
CLAUDE_TABLE = FIXTURES / "claude.md"
CODEX_TABLE = FIXTURES / "codex.md"
AGY_TABLE = FIXTURES / "antigravity.md"  # taken 2026-09-14

DEFAULT_PATHS = [
    Path.home() / ".claude" / "model_context_limits.md",
    Path.home() / ".codex" / "model_context_limits.md",
    Path.home() / ".gemini" / "model_context_limits.md",
]

REAL_MODEL_NAMES = [
    "Claude Sonnet 5",
    "Claude Opus 5",
    "GPT-5.6 Luna",
    "GPT-5.6 Terra",
    "GPT-5.6 Sol",
]


@pytest.fixture(autouse=True)
def _cache_into_tmp(tmp_path, monkeypatch):
    """Every test's `load_limits()` call writes its cache under `tmp_path`, never the
    live repo's `.orchestrator/`."""
    monkeypatch.setattr(limits, "MODEL_LIMITS_JSON", tmp_path / ".orchestrator" / "model_limits.json")


@pytest.fixture(autouse=True)
def _no_project_yaml_override(monkeypatch):
    """`default_table_paths()` reads `project.yaml` through `maestro.config`; pin it to
    "no override" so a real `project.yaml` sitting in this checkout can't leak into a
    test that expects the two default paths."""
    from maestro import config as _config

    monkeypatch.setattr(_config, "load_project_yaml", lambda: {})


# ── parse_limits_table: the fixture copies of the real files ──

def test_parses_the_real_claude_table():
    text = CLAUDE_TABLE.read_text(encoding="utf-8")
    models = limits.parse_limits_table(text)

    assert models["Claude Sonnet 5"] == limits.ModelLimits(
        "Claude Sonnet 5", 120_000, 130_000, 150_000, 170_000, 250_000
    )
    assert models["Claude Opus 5"] == limits.ModelLimits(
        "Claude Opus 5", 160_000, 180_000, 200_000, 220_000, 300_000
    )


def test_parses_the_real_codex_table():
    text = CODEX_TABLE.read_text(encoding="utf-8")
    models = limits.parse_limits_table(text)

    assert models["GPT-6 Astra"] == limits.ModelLimits(
        "GPT-6 Astra", 160_000, 200_000, 230_000, 270_000, 350_000
    )
    assert models["GPT-5.4 Mini"] == limits.ModelLimits(
        "GPT-5.4 Mini", 40_000, 60_000, 70_000, 90_000, 110_000
    )

    assert models["GPT-5.6 Luna"] == limits.ModelLimits(
        "GPT-5.6 Luna", 70_000, 90_000, 100_000, 120_000, 150_000
    )
    assert models["GPT-5.6 Terra"] == limits.ModelLimits(
        "GPT-5.6 Terra", 100_000, 120_000, 140_000, 170_000, 220_000
    )
    assert models["GPT-5.6 Sol"] == limits.ModelLimits(
        "GPT-5.6 Sol", 120_000, 140_000, 180_000, 220_000, 280_000
    )


@pytest.mark.parametrize("agent", sorted(limits.AGENT_TABLE_DIRS))
def test_the_live_agent_tables_parse_cleanly(agent):
    """Smoke test on the live files: shape only, never numbers, since they get edited."""
    live = Path.home() / limits.AGENT_TABLE_DIRS[agent] / limits.TABLE_FILENAME
    if not live.is_file():
        pytest.skip(f"no live table for {agent} at {live}")

    result = limits.load_limits([live])

    assert not any("unparseable row" in w for w in result.warnings), result.warnings
    assert not any("shipped defaults" in w for w in result.warnings), result.warnings
    prefix = limits.AGENT_MODEL_PREFIX.get(agent, "")
    assert all(name.startswith(prefix) for name in result.models), sorted(result.models)


def test_real_tables_are_read_only_never_written(monkeypatch):
    """Belt-and-braces: prove `load_limits()` never mutates the tables it reads from."""
    before_claude = CLAUDE_TABLE.read_bytes()
    before_codex = CODEX_TABLE.read_bytes()

    limits.load_limits([CLAUDE_TABLE, CODEX_TABLE])

    assert CLAUDE_TABLE.read_bytes() == before_claude
    assert CODEX_TABLE.read_bytes() == before_codex


# ── parse_limits_table: formatting tolerance ──

def test_tolerates_bold_cell_alignment_colons_and_lowercase_k():
    text = (
        "| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n"
        "|---|---:|---:|---:|\n"
        "| **Some Model** | 90–100K | 120–140k | 180k |\n"
    )
    models = limits.parse_limits_table(text)
    assert models["Some Model"] == limits.ModelLimits("Some Model", 90_000, 100_000, 120_000, 140_000, 180_000)


def test_tolerates_hyphen_ranges_and_k_on_both_sides():
    text = (
        "| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n"
        "|---:|---:|---:|---:|\n"
        "| Other Model | 70K-90K | 100K-120K | 150K |\n"
    )
    models = limits.parse_limits_table(text)
    assert models["Other Model"] == limits.ModelLimits("Other Model", 70_000, 90_000, 100_000, 120_000, 150_000)


def test_a_single_value_cell_reads_as_a_range_of_one():
    # The live Claude table's "Model unknown" row states single values, not ranges.
    text = (
        "| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n"
        "|---:|---:|---:|---:|\n"
        "| Model unknown | 100K | 120K | 150K |\n"
    )
    models = limits.parse_limits_table(text)
    assert models["Model unknown"] == limits.ModelLimits("Model unknown", 100_000, 100_000, 120_000, 120_000, 150_000)


def test_a_row_with_no_pipes_or_wrong_cell_count_is_ignored_not_a_crash():
    text = "Model unknown -> 100K warn / 120K evaluate / 150K normal max.\n"
    assert limits.parse_limits_table(text) == {}


# ── load_limits: missing file ──

def test_a_missing_file_produces_a_warning_not_an_error(tmp_path):
    missing = tmp_path / "does_not_exist.md"
    result = limits.load_limits([missing])

    assert isinstance(result, limits.LimitsResult)
    assert any(str(missing) in w for w in result.warnings)
    # Nothing parsed anywhere -> shipped defaults kick in, so this still isn't empty.
    assert result.models


# ── load_limits: unparseable row ──

def test_an_unparseable_row_is_warned_about_but_does_not_stop_the_good_rows(tmp_path):
    table = tmp_path / "table.md"
    table.write_text(
        "| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n"
        "|---|---:|---:|---:|\n"
        "| Good Model | 90–100K | 120–140K | 180K |\n"
        "| Bad Model | not-a-range | 120–140K | 180K |\n",
        encoding="utf-8",
    )

    result = limits.load_limits([table])

    assert result.models["Good Model"] == limits.ModelLimits(
        "Good Model", 90_000, 100_000, 120_000, 140_000, 180_000
    )
    assert "Bad Model" not in result.models
    assert any("unparseable row" in w for w in result.warnings)


# ── load_limits: shipped-defaults fallback ──

def test_shipped_defaults_fill_in_when_nothing_parses_anywhere(tmp_path):
    empty = tmp_path / "empty.md"
    empty.write_text("# nothing but prose here\n", encoding="utf-8")

    result = limits.load_limits([empty])

    assert result.models == dict(limits._SHIPPED_DEFAULTS)
    assert any("shipped defaults" in w for w in result.warnings)


# ── C7 part 1: _SHIPPED_DEFAULTS shrinks to "roles.py's own defaults still resolve to
# something", not a mirror of every real table row ──

_DERIVATION_TABLE = """
| Model | Prepare handoff | Normally start fresh | Exception ceiling |
|---:|---:|---:|---:|
| Claude Haiku 4.5 | 90-100K | 120-140K | 180K |
| Claude Sonnet 5.5 | 160-180K | 220-240K | 320K |
| Claude Sonnet 6 | 160-180K | 220-240K | 320K |
| Claude Sonnet 5.10 | 160-180K | 220-240K | 320K |
| Claude Opus 5.5 | 180-200K | 240-270K | 350K |
| Model unknown | 100K | 120K | 150K |
"""


def test_latest_family_row_picks_the_highest_version_numerically():
    assert limits.latest_family_row("sonnet", _DERIVATION_TABLE, "fallback") == "Claude Sonnet 6"
    assert limits.latest_family_row("opus", _DERIVATION_TABLE, "fallback") == "Claude Opus 5.5"
    # 5.10 outranks 5.5 (numeric, not lexicographic)
    text = _DERIVATION_TABLE.replace("| Claude Sonnet 6 |", "| Claude Sonnet 4 |")
    assert limits.latest_family_row("sonnet", text, "fallback") == "Claude Sonnet 5.10"


def test_latest_family_row_falls_back_when_the_family_has_no_row():
    assert limits.latest_family_row("opus", "no table here", "Claude Opus 5.5") == "Claude Opus 5.5"
    assert limits.latest_family_row("fable", _DERIVATION_TABLE, "x") == "x"


def test_default_model_names_track_the_claude_table_and_fall_back_without_it(tmp_path, monkeypatch):
    import importlib

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "model_context_limits.md").write_text(_DERIVATION_TABLE, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    try:
        importlib.reload(limits)
        assert limits.DEFAULT_IMPLEMENTER_MODEL_NAME == "Claude Sonnet 6"
        assert limits.DEFAULT_JUDGE_MODEL_NAME == "Claude Sonnet 6"
        assert limits.DEFAULT_DIAGNOSER_MODEL_NAME == "Claude Opus 5.5"

        monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))  # no table at all
        importlib.reload(limits)
        assert limits.DEFAULT_IMPLEMENTER_MODEL_NAME == "Claude Sonnet 5.5"
        assert limits.DEFAULT_DIAGNOSER_MODEL_NAME == "Claude Opus 5.5"
    finally:
        monkeypatch.undo()
        importlib.reload(limits)


def test_shipped_defaults_covers_exactly_the_role_default_models_not_a_full_table_mirror():
    """`_SHIPPED_DEFAULTS`'s only documented job is "a machine with neither table file"
    (its own comment) — the minimal set that satisfies that job is the handful of models
    `roles.DEFAULT_MODELS` can ever hand back with zero configuration, not a second copy
    of every row in either real table (GPT-5.6 Terra/Sol, etc.), which is what made this
    dict a second place to edit every time a table row changed."""
    assert set(limits._SHIPPED_DEFAULTS) == {
        limits.DEFAULT_IMPLEMENTER_MODEL_NAME,
        limits.DEFAULT_JUDGE_MODEL_NAME,
        limits.DEFAULT_DIAGNOSER_MODEL_NAME,
    }


def test_shipped_defaults_still_resolve_the_default_models_by_their_project_yaml_slug():
    """The shrunk fallback must still answer the one question `roles.py` can ask of it
    with zero project.yaml and neither table file present: the implementer/judge/
    diagnoser default models, by the slug `roles.DEFAULT_MODELS` actually hands
    `limits.resolve()`. Resolving each default slug against an empty/missing table set
    must fall back to a real `ModelLimits`, not `None`."""
    from maestro import roles as _roles

    empty = limits.load_limits([])
    for role, backend_models in _roles.DEFAULT_MODELS.items():
        slug = backend_models[_roles.registry.DEFAULT_BACKEND]
        assert empty.get(slug) is not None, f"{role}'s default model {slug!r} did not resolve"


def test_shipped_defaults_are_not_used_when_at_least_one_real_row_parsed(tmp_path):
    table = tmp_path / "table.md"
    table.write_text(
        "| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n"
        "|---|---:|---:|---:|\n"
        "| Only Model | 90–100K | 120–140K | 180K |\n",
        encoding="utf-8",
    )

    result = limits.load_limits([table])

    assert result.models == {
        "Only Model": limits.ModelLimits("Only Model", 90_000, 100_000, 120_000, 140_000, 180_000)
    }
    assert not any("shipped defaults" in w for w in result.warnings)


# ── load_limits: caching ──

def test_load_limits_writes_the_merged_cache_to_disk(tmp_path):
    table = tmp_path / "table.md"
    table.write_text(
        "| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n"
        "|---|---:|---:|---:|\n"
        "| Cached Model | 90–100K | 120–140K | 180K |\n",
        encoding="utf-8",
    )

    limits.load_limits([table])

    cache = json.loads(limits.MODEL_LIMITS_JSON.read_text(encoding="utf-8"))
    assert cache["Cached Model"]["exception_ceiling"] == 180_000


# ── default_table_paths ──

def test_default_table_paths_is_every_agents_global_config_location():
    paths = limits.default_table_paths()
    assert paths == DEFAULT_PATHS


def _fake_home(monkeypatch, tmp_path, installed):
    """Point `Path.home()` at `tmp_path`, creating a config dir for each `installed` agent."""
    home = tmp_path / "home"
    for agent in installed:
        (home / limits.AGENT_TABLE_DIRS[agent]).mkdir(parents=True)
    monkeypatch.setattr(limits.Path, "home", classmethod(lambda cls: home))
    return home


def test_a_missing_default_table_for_an_uninstalled_agent_is_silent(monkeypatch, tmp_path):
    home = _fake_home(monkeypatch, tmp_path, installed=["claude"])
    (home / ".claude" / limits.TABLE_FILENAME).write_bytes(CLAUDE_TABLE.read_bytes())

    result = limits.load_limits()

    assert result.warnings == []
    assert result.get("claude-opus-5").exception_ceiling == 300_000


def test_a_missing_default_table_for_an_installed_agent_still_warns(monkeypatch, tmp_path):
    home = _fake_home(monkeypatch, tmp_path, installed=["claude", "antigravity"])
    (home / ".claude" / limits.TABLE_FILENAME).write_bytes(CLAUDE_TABLE.read_bytes())

    result = limits.load_limits()

    assert result.warnings == [f"model limits file missing: {home / '.gemini' / limits.TABLE_FILENAME}"]


def test_a_missing_table_named_in_project_yaml_still_warns(monkeypatch, tmp_path):
    from maestro import config as _config

    _fake_home(monkeypatch, tmp_path, installed=[])
    named = tmp_path / "nowhere" / "limits.md"
    monkeypatch.setattr(_config, "load_project_yaml", lambda: {"model_limits": {"claude": str(named)}})

    result = limits.load_limits()

    assert any(str(named) in w for w in result.warnings)


def test_resolve_picks_up_an_edited_table_without_a_restart(monkeypatch, tmp_path):
    """Nothing caches a parse between calls: an edit to the table shows on the next resolve."""
    from maestro import config as _config

    table = tmp_path / "claude.md"
    table.write_bytes(CLAUDE_TABLE.read_bytes())
    monkeypatch.setattr(_config, "load_project_yaml", lambda: {"model_limits": {"claude": str(table)}})
    assert limits.resolve("claude-opus-5").exception_ceiling == 300_000

    table.write_text(table.read_text(encoding="utf-8").replace("| 300K |", "| 320K |"), encoding="utf-8")

    assert limits.resolve("claude-opus-5").exception_ceiling == 320_000


def test_default_table_paths_is_overridable_via_project_yaml(monkeypatch, tmp_path):
    """`model_limits: {<backend>: <path>, ...}` is the DESIGN.md §8 / project.yaml.tmpl
    shape — a mapping, keyed by backend, not a flat list. Declaration order is preserved
    so a project that only overrides one backend still gets a deterministic path list."""
    from maestro import config as _config

    custom = tmp_path / "custom_limits.md"
    monkeypatch.setattr(
        _config, "load_project_yaml", lambda: {"model_limits": {"claude": str(custom)}}
    )

    assert limits.default_table_paths() == [custom]


def test_default_table_paths_mapping_keeps_declared_backend_order(monkeypatch, tmp_path):
    from maestro import config as _config

    codex_custom = tmp_path / "codex_limits.md"
    claude_custom = tmp_path / "claude_limits.md"
    monkeypatch.setattr(
        _config,
        "load_project_yaml",
        lambda: {"model_limits": {"codex": str(codex_custom), "claude": str(claude_custom)}},
    )

    assert limits.default_table_paths() == [codex_custom, claude_custom]


def test_default_table_paths_legacy_model_limits_paths_key_is_ignored_with_a_warning(
    monkeypatch, tmp_path
):
    """The flat-list `model_limits_paths` spelling predates DESIGN.md's documented
    `model_limits: {<backend>: <path>}` mapping and is no longer read. A project.yaml
    still carrying it must not silently do nothing — it gets a warning and the shipped
    defaults, not the path it named."""
    from maestro import config as _config

    custom = tmp_path / "legacy_only_list.md"
    monkeypatch.setattr(
        _config, "load_project_yaml", lambda: {"model_limits_paths": [str(custom)]}
    )

    with pytest.warns(UserWarning, match="model_limits_paths"):
        paths = limits.default_table_paths()

    assert paths == DEFAULT_PATHS
    assert custom not in paths


def test_default_table_paths_model_limits_mapping_wins_over_legacy_key_when_both_set(
    monkeypatch, tmp_path
):
    """When a project.yaml carries both spellings (e.g. mid-migration), the documented
    `model_limits` mapping is authoritative and the legacy list is still just a warning,
    not a second source of paths."""
    from maestro import config as _config

    custom = tmp_path / "current.md"
    legacy = tmp_path / "legacy.md"
    monkeypatch.setattr(
        _config,
        "load_project_yaml",
        lambda: {
            "model_limits": {"claude": str(custom)},
            "model_limits_paths": [str(legacy)],
        },
    )

    with pytest.warns(UserWarning, match="model_limits_paths"):
        paths = limits.default_table_paths()

    assert paths == [custom]


def test_default_table_paths_warns_when_model_limits_reuses_the_deleted_list_shape(
    monkeypatch, tmp_path
):
    """The single most plausible mistake here: an operator migrating off the deleted
    `model_limits_paths` reuses *its* list shape under the *new* `model_limits` key
    instead of switching to the `{<backend>: <path>}` mapping. That must not fall back to
    the shipped defaults in silence — it's the same "knob set, no effect" defect this
    module exists to close, just one key later."""
    from maestro import config as _config

    custom = tmp_path / "claude_limits.md"
    monkeypatch.setattr(
        _config, "load_project_yaml", lambda: {"model_limits": [str(custom)]}
    )

    with pytest.warns(UserWarning, match="model_limits"):
        paths = limits.default_table_paths()

    assert paths == DEFAULT_PATHS
    assert custom not in paths


def test_default_table_paths_warns_on_an_empty_model_limits_mapping(monkeypatch):
    """An empty `model_limits: {}` is deliberately treated as unusable, not as "no
    override": a project.yaml wanting the defaults simply omits the key, so an
    explicit-but-empty mapping is far more likely an accidentally cleared value than an
    intentional opt-in — and it must warn like every other unusable shape here."""
    from maestro import config as _config

    monkeypatch.setattr(_config, "load_project_yaml", lambda: {"model_limits": {}})

    with pytest.warns(UserWarning, match="model_limits"):
        paths = limits.default_table_paths()

    assert paths == DEFAULT_PATHS


# ── resolve / resolve_all ──

def test_resolve_returns_none_and_warns_for_an_unknown_model(tmp_path):
    table = tmp_path / "table.md"
    table.write_text(
        "| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n"
        "|---|---:|---:|---:|\n"
        "| Known Model | 90–100K | 120–140K | 180K |\n",
        encoding="utf-8",
    )

    with pytest.warns(UserWarning, match="Unknown Model"):
        result = limits.resolve("Unknown Model", [table])

    assert result is None


def test_resolve_finds_a_model_present_in_the_table(tmp_path):
    table = tmp_path / "table.md"
    table.write_text(
        "| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n"
        "|---|---:|---:|---:|\n"
        "| Known Model | 90–100K | 120–140K | 180K |\n",
        encoding="utf-8",
    )

    result = limits.resolve("Known Model", [table])
    assert result == limits.ModelLimits("Known Model", 90_000, 100_000, 120_000, 140_000, 180_000)


def test_resolve_all_against_the_real_shipped_tables():
    """The M3 plan's Done-when item 2: `resolve_all()` against (fixture copies of) the two
    real files at `~/.claude/model_context_limits.md` and `~/.codex/model_context_limits.md` resolves
    all 5 real model names to a non-None ModelLimits. Stands in for `doctor` resolving
    every model in both tables, until M4 builds `doctor` itself."""
    resolved = limits.resolve_all(REAL_MODEL_NAMES, [CLAUDE_TABLE, CODEX_TABLE])

    assert set(resolved) == set(REAL_MODEL_NAMES)
    for name in REAL_MODEL_NAMES:
        assert resolved[name] is not None, f"{name} did not resolve"
        assert isinstance(resolved[name], limits.ModelLimits)


def test_resolve_all_uses_the_defaults_when_paths_omitted(monkeypatch, tmp_path):
    home = _fake_home(monkeypatch, tmp_path, installed=["claude", "codex"])
    (home / ".claude" / limits.TABLE_FILENAME).write_bytes(CLAUDE_TABLE.read_bytes())
    (home / ".codex" / limits.TABLE_FILENAME).write_bytes(CODEX_TABLE.read_bytes())

    resolved = limits.resolve_all(REAL_MODEL_NAMES)
    for name in REAL_MODEL_NAMES:
        assert resolved[name] is not None, f"{name} did not resolve"


# ── display-name ↔ slug tolerance (M4 finding #2) ──
#
# The tables key by human display name; `project.yaml`'s `roles:` block names models by
# machine slug, so `doctor`'s `model_limits` check resolved nothing on a real project. The
# fix is one normalisation (casefold, whitespace runs → single hyphens), not a mapping table.

REAL_MODEL_SLUGS = [
    "claude-sonnet-5",
    "claude-opus-5",
    "gpt-5.6-luna",
    "gpt-5.6-terra",
    "gpt-5.6-sol",
]


def test_normalise_model_key_folds_display_names_to_project_yaml_slugs():
    """R8 deliberate re-baseline (2026-08-31): the fold now treats `.` the same as `-`
    and whitespace (needed to match `claude-haiku-4-5` against `"Claude Haiku 4.5"`), so
    the GPT-5.6 family's folded form loses its literal `.` too — `gpt-5-6-luna`, not the
    old `gpt-5.6-luna`. This does not change what resolves: `project.yaml`'s own
    `gpt-5.6-terra` slug (DESIGN.md §5) folds to the same new value on the other side of
    the comparison, proven directly by
    `test_gpt_5_6_slugs_still_resolve_against_the_real_codex_table_after_the_fold_change`
    below, against the real table."""
    assert limits._normalise_model_key("Claude Sonnet 5") == "claude-sonnet-5"
    assert limits._normalise_model_key("Claude Opus 5") == "claude-opus-5"
    assert limits._normalise_model_key("GPT-5.6 Luna") == "gpt-5-6-luna"
    assert limits._normalise_model_key("GPT-5.6 Terra") == "gpt-5-6-terra"
    assert limits._normalise_model_key("GPT-5.6 Sol") == "gpt-5-6-sol"


def test_normalise_model_key_is_idempotent():
    for name in REAL_MODEL_NAMES + REAL_MODEL_SLUGS:
        once = limits._normalise_model_key(name)
        assert limits._normalise_model_key(once) == once


# ── R8: period-vs-hyphen folding + date-suffix stripping (wave-1 finding, promoted into
# C7's scope) ──
#
# The real configured id for Haiku 4.5 is `claude-haiku-4-5-20251001` (a hyphen where the
# table's own normalised form has a period, plus a trailing release-date suffix). Before
# this fix, `_normalise_model_key` only casefolded and joined whitespace runs, so neither
# gap closed: `"Claude Haiku 4.5"` and `"claude-haiku-4.5"` resolved, but the id maestro
# actually configures and the CLI actually reports did not.

def test_normalise_model_key_folds_hyphen_and_period_as_the_same_separator():
    assert limits._normalise_model_key("claude-haiku-4-5") == limits._normalise_model_key(
        "Claude Haiku 4.5"
    )
    assert limits._normalise_model_key("claude-haiku-4.5") == limits._normalise_model_key(
        "Claude Haiku 4.5"
    )


def test_normalise_model_key_strips_a_trailing_release_date_suffix():
    assert limits._normalise_model_key(
        "claude-haiku-4-5-20251001"
    ) == limits._normalise_model_key("Claude Haiku 4.5")


def test_resolve_finds_the_actual_configured_haiku_id_against_the_real_claude_table():
    """The wave-1 finding, closed: all four spellings of Haiku 4.5 resolve against the
    real `~/.claude/model_context_limits.md`, including the dated id nothing but this fix
    could ever produce and the plain `claude-haiku-4-5` slug `implementer.py` launches
    with."""
    for spelling in (
        "Claude Haiku 4.5",
        "claude-haiku-4.5",
        "claude-haiku-4-5",
        "claude-haiku-4-5-20251001",
    ):
        result = limits.resolve(spelling, [CLAUDE_TABLE])
        assert result is not None, f"{spelling!r} did not resolve"
        assert result.model == "Claude Haiku 4.5"


def test_gpt_5_6_slugs_still_resolve_against_the_real_codex_table_after_the_fold_change():
    """The broadened fold (hyphen/period/whitespace all equivalent) must not break the
    existing GPT-5.6 family, whose `project.yaml` slugs already carry a literal period
    (`gpt-5.6-terra`, DESIGN.md §5) that happened to survive the old, narrower fold
    unchanged."""
    for slug, display in (
        ("gpt-5.6-luna", "GPT-5.6 Luna"),
        ("gpt-5.6-terra", "GPT-5.6 Terra"),
        ("gpt-5.6-sol", "GPT-5.6 Sol"),
    ):
        result = limits.resolve(slug, [CODEX_TABLE])
        assert result is not None, f"{slug!r} did not resolve"
        assert result.model == display



def test_resolve_finds_a_display_name_row_by_its_slug(tmp_path):
    table = tmp_path / "table.md"
    table.write_text(
        "| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n"
        "|---|---:|---:|---:|\n"
        "| Known Model | 90–100K | 120–140K | 180K |\n",
        encoding="utf-8",
    )

    result = limits.resolve("known-model", [table])
    assert result == limits.ModelLimits("Known Model", 90_000, 100_000, 120_000, 140_000, 180_000)


def test_resolve_by_slug_still_reports_the_tables_display_name(tmp_path):
    """`ModelLimits.model` must keep carrying the table's own spelling — a slug lookup
    must not rewrite it, or the cache and `doctor`'s output disagree with the source file."""
    table = tmp_path / "table.md"
    table.write_text(
        "| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n"
        "|---|---:|---:|---:|\n"
        "| Known Model | 90–100K | 120–140K | 180K |\n",
        encoding="utf-8",
    )

    assert limits.resolve("known-model", [table]).model == "Known Model"


def test_exact_match_wins_over_a_normalised_match(tmp_path):
    """A table carrying both spellings must hand back the exactly-named row, not whichever
    row happens to normalise the same way first."""
    table = tmp_path / "table.md"
    table.write_text(
        "| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n"
        "|---|---:|---:|---:|\n"
        "| Known Model | 90–100K | 120–140K | 180K |\n"
        "| known-model | 10–20K | 30–40K | 50K |\n",
        encoding="utf-8",
    )

    assert limits.resolve("known-model", [table]) == limits.ModelLimits(
        "known-model", 10_000, 20_000, 30_000, 40_000, 50_000
    )
    assert limits.resolve("Known Model", [table]) == limits.ModelLimits(
        "Known Model", 90_000, 100_000, 120_000, 140_000, 180_000
    )


def test_a_table_keyed_by_slugs_directly_still_resolves_both_ways(tmp_path):
    table = tmp_path / "table.md"
    table.write_text(
        "| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n"
        "|---|---:|---:|---:|\n"
        "| slug-only-model | 90–100K | 120–140K | 180K |\n",
        encoding="utf-8",
    )

    exact = limits.resolve("slug-only-model", [table])
    assert exact == limits.ModelLimits("slug-only-model", 90_000, 100_000, 120_000, 140_000, 180_000)
    # The display-ish spelling normalises onto the same slug, so it resolves too.
    assert limits.resolve("Slug Only Model", [table]) == exact


def test_a_name_in_neither_spelling_still_misses_and_still_warns(tmp_path):
    table = tmp_path / "table.md"
    table.write_text(
        "| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n"
        "|---|---:|---:|---:|\n"
        "| Known Model | 90–100K | 120–140K | 180K |\n",
        encoding="utf-8",
    )

    with pytest.warns(UserWarning) as caught:
        assert limits.resolve("Totally Other Model", [table]) is None

    message = str(caught[0].message)
    assert "Totally Other Model" in message
    assert "totally-other-model" in message  # the normalised form tried, for the operator


def test_resolve_all_accepts_slugs_and_display_names_mixed(tmp_path):
    table = tmp_path / "table.md"
    table.write_text(
        "| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n"
        "|---|---:|---:|---:|\n"
        "| First Model | 90–100K | 120–140K | 180K |\n"
        "| Second Model | 70K-90K | 100K-120K | 150K |\n",
        encoding="utf-8",
    )

    resolved = limits.resolve_all(["first-model", "Second Model"], [table])

    # Keyed by the caller's own spelling, valued by the table's row.
    assert set(resolved) == {"first-model", "Second Model"}
    assert resolved["first-model"].model == "First Model"
    assert resolved["Second Model"].model == "Second Model"


def test_resolve_all_warns_per_name_that_matches_in_neither_spelling(tmp_path):
    table = tmp_path / "table.md"
    table.write_text(
        "| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n"
        "|---|---:|---:|---:|\n"
        "| Known Model | 90–100K | 120–140K | 180K |\n",
        encoding="utf-8",
    )

    with pytest.warns(UserWarning, match="nope-model"):
        resolved = limits.resolve_all(["known-model", "nope-model"], [table])

    assert resolved["known-model"] is not None
    assert resolved["nope-model"] is None


def test_resolve_all_against_the_real_tables_by_project_yaml_slugs():
    """The M4 finding closed: the slugs `doctor` actually passes (from `project.yaml`'s
    `roles:` block) resolve against the two real display-name-keyed tables."""
    resolved = limits.resolve_all(REAL_MODEL_SLUGS, [CLAUDE_TABLE, CODEX_TABLE])

    assert set(resolved) == set(REAL_MODEL_SLUGS)
    for slug in REAL_MODEL_SLUGS:
        assert resolved[slug] is not None, f"{slug} did not resolve"
        # Still the human-readable name from the table, never the slug we asked with.
        assert resolved[slug].model in REAL_MODEL_NAMES


def test_limits_result_get_is_tolerant_of_both_spellings(tmp_path):
    table = tmp_path / "table.md"
    table.write_text(
        "| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n"
        "|---|---:|---:|---:|\n"
        "| Known Model | 90–100K | 120–140K | 180K |\n",
        encoding="utf-8",
    )

    result = limits.load_limits([table])

    assert result.get("Known Model") is result.get("known-model")
    assert result.get("known-model").model == "Known Model"
    assert result.get("no-such-model") is None
    # `.models` itself is untouched: still the raw, display-name-keyed table.
    assert list(result.models) == ["Known Model"]


# ── the antigravity table: harness-prefixed rows ──

def test_parses_the_real_antigravity_table_with_its_harness_prefix():
    models = limits.parse_limits_table(AGY_TABLE.read_text(encoding="utf-8"))

    assert models["agy/Gemini 3.8 Flash"] == limits.ModelLimits(
        "agy/Gemini 3.8 Flash", 115_000, 120_000, 125_000, 130_000, 135_000
    )
    assert len(models) == 7  # the two-column "Boundary basis" table is not rows
    assert all(name.startswith("agy/") for name in models)


def test_an_agy_row_never_answers_another_harness_lookup(tmp_path):
    claude = tmp_path / "claude.md"
    claude.write_text(
        "| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n"
        "|---|---:|---:|---:|\n"
        "| Claude Opus 4.6 (Thinking) | 150-160K | 180-190K | 250K |\n"
    )
    paths = [claude, AGY_TABLE]

    assert limits.resolve("Claude Opus 4.6 (Thinking)", paths).exception_ceiling == 250_000
    agy = limits.resolve("Claude Opus 4.6 (Thinking)", paths, backend="antigravity")
    assert agy.model == "agy/Claude Opus 4.6 (Thinking)" and agy.exception_ceiling == 135_000
    with pytest.warns(UserWarning):
        assert limits.resolve("Gemini 3.8 Flash", [AGY_TABLE]) is None
    assert limits.resolve("gemini-3-8-flash", [AGY_TABLE], backend="antigravity").model == "agy/Gemini 3.8 Flash"


def test_scoped_model_name_is_idempotent_and_backend_specific():
    assert limits.scoped_model_name("Gemini 3.8 Flash", "antigravity") == "agy/Gemini 3.8 Flash"
    assert limits.scoped_model_name("agy/Gemini 3.8 Flash", "antigravity") == "agy/Gemini 3.8 Flash"
    assert limits.scoped_model_name("Claude Opus 5", "claude") == "Claude Opus 5"
    assert limits.scoped_model_name("Claude Opus 5") == "Claude Opus 5"


# ── agy variant ids fold onto their family row, only under the agy/ prefix ──

AGY_PROBE = Path(__file__).resolve().parents[1] / "artifacts" / "graph-engineering" / "p07b-agy-1.2.2-probe.json"
AGY_FAMILY = {
    "gemini-3.8-flash": "agy/Gemini 3.8 Flash",
    "gemini-3.7-flash": "agy/Gemini 3.7 Flash",
    "gemini-3.6-flash": "agy/Gemini 3.6 Flash",
    "gemini-3.1-pro": "agy/Gemini 3.1 Pro",
    "claude-sonnet-4-6": "agy/Claude Sonnet 4.6 (Thinking)",
    "claude-opus-4-6": "agy/Claude Opus 4.6 (Thinking)",
    "gpt-oss-120b": "agy/GPT-OSS 120B (Medium)",
}


def _agy_ids():
    return json.loads(AGY_PROBE.read_text(encoding="utf-8"))["models_listed"]["ids"]


@pytest.mark.parametrize("model_id", _agy_ids())
def test_every_listed_agy_id_resolves_its_agy_family_row(model_id):
    row = limits.resolve(model_id, [AGY_TABLE], backend="antigravity")
    assert row is not None
    family = next(v for k, v in AGY_FAMILY.items() if model_id.startswith(k))
    assert row.model == family


def test_the_status_lines_display_form_resolves_too():
    row = limits.resolve("Gemini 3.8 Flash (High)", [AGY_TABLE], backend="antigravity")
    assert row.model == "agy/Gemini 3.8 Flash"


def test_an_unknown_agy_id_is_none_with_a_warning_never_an_invented_row():
    with pytest.warns(UserWarning):
        assert limits.resolve("gemini-9.9-flash-high", [AGY_TABLE], backend="antigravity") is None


@pytest.mark.parametrize("model_id", ["Gemini 3.8 Flash", "gemini-3.8-flash-high", "claude-opus-4-6-thinking"])
def test_a_backend_less_lookup_never_reaches_an_agy_row(model_id):
    with pytest.warns(UserWarning):
        assert limits.resolve(model_id, [AGY_TABLE]) is None
