"""P6 §1: the capability catalog, quota accounting and outcome statistics.

The bar these tests hold the catalog to is not "does the dataclass hold the fields the
spec lists" — it is the three refusals the module exists for: a claimed effort flag with
no observed vocabulary is not support, a quota reading carries its own age and provenance,
and a success rate with one sample is not evidence.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from maestro.backends import catalog as cat


def entry(**overrides) -> cat.BackendCapabilityEntry:
    body = dict(
        backend_id="claude",
        runtime_kind="agent_cli",
        installed_version="2.1.269",
        billing_category="subscription",
        usage_pool_id="claude-max",
        available_models=("sonnet-5", "opus-5"),
    )
    body.update(overrides)
    return cat.BackendCapabilityEntry(**body)


# ── entries and vocabularies ─────────────────────────────────────────────────────────


def test_entry_covers_the_six_capability_dimensions():
    document = entry().to_dict()
    for dimension in (
        "billing_category",
        "available_models",
        "effort_controls",
        "context_limits",
        "modalities",
        "tool_delivery",
        "instruction_delivery",
        "structured_output_support",
        "permission_enforcement",
        "concurrency_limit",
        "telemetry_quality",
    ):
        assert dimension in document


@pytest.mark.parametrize(
    "field,value",
    [
        ("runtime_kind", "magic"),
        ("billing_category", "free_lunch"),
        ("tool_delivery", "vibes"),
        ("instruction_delivery", "telepathy"),
        ("structured_output_support", "hope"),
        ("permission_enforcement", "trust"),
        ("telemetry_quality", "guesswork"),
        ("modalities", ("video",)),
        ("usage_pool_id", ""),
        ("available_models", ()),
        ("concurrency_limit", 0),
    ],
)
def test_an_entry_outside_the_declared_vocabulary_is_refused(field, value):
    with pytest.raises(cat.CatalogError):
        entry(**{field: value})


def test_a_model_profile_must_describe_a_listed_model():
    with pytest.raises(cat.CatalogError):
        entry(model_profiles={"haiku-5": cat.ModelProfile("haiku-5")})


def test_an_unprofiled_model_defaults_to_the_middle_rung_rather_than_being_unroutable():
    """A newly observed model must be routable the day the register records it."""
    profile = entry().profile("opus-5")
    assert profile.strength == "balanced" and profile.relative_cost == 1.0
    with pytest.raises(cat.CatalogError):
        entry().profile("not-a-model")


def test_a_model_without_its_own_pool_bills_against_the_entrys():
    """`pool_for` mirrors `profile()`: default the listed model, refuse the unknown one."""
    built = entry()
    assert cat.ModelProfile("opus-5").usage_pool_id is None
    assert built.pool_for("opus-5") == built.usage_pool_id
    with pytest.raises(cat.CatalogError):
        built.pool_for("not-a-model")


def test_a_models_own_pool_overrides_the_entrys():
    """A backend that resells another vendor's models can bill them separately."""
    built = entry(
        model_profiles={"opus-5": cat.ModelProfile("opus-5", usage_pool_id="resold-pool")}
    )
    assert built.pool_for("opus-5") == "resold-pool"
    assert built.usage_pool_id != "resold-pool"


def test_the_quality_ceiling_and_not_the_raw_window_is_what_admits_a_task():
    limits = cat.ContextLimits(
        max_input_tokens=1_000_000, max_output_tokens=32_000, effective_context_ceiling=200_000
    )
    assert limits.admits(150_000, 1_000)
    assert not limits.admits(300_000, 1_000), "routed past the ceiling into the raw window"
    assert not limits.admits(1_000, 64_000)


def test_a_quality_ceiling_above_its_own_window_is_not_a_measurement():
    with pytest.raises(cat.CatalogError):
        cat.ContextLimits(100, 10, 200)


def test_entry_revision_moves_with_the_installed_version():
    """A CLI upgrade must not leave old bindings looking freshly resolved."""
    assert entry().revision != entry(installed_version="2.2.0").revision


def test_catalog_ordering_is_deterministic_and_independent_of_insertion_order():
    one = cat.CapabilityCatalog.of([entry(), entry(backend_id="codex", usage_pool_id="codex")])
    other = cat.CapabilityCatalog.of([entry(backend_id="codex", usage_pool_id="codex"), entry()])
    assert one.backend_ids() == other.backend_ids() == ("claude", "codex")
    assert one.routes() == other.routes()
    assert one.revision == other.revision


# ── effort: the [INV-A07] refusals ───────────────────────────────────────────────────


def test_a_delivery_mechanism_without_an_observed_vocabulary_cannot_be_constructed():
    with pytest.raises(cat.CatalogError):
        cat.EffortControls(supported=True, delivery_mechanism="flag", native_values=())


def test_an_effort_mapping_may_only_target_values_the_backend_actually_showed():
    with pytest.raises(cat.CatalogError):
        cat.EffortControls(
            supported=True,
            delivery_mechanism="config_key",
            native_values=("minimal", "maximal"),
            mapping={"high": "ultra"},
        )


def test_a_flag_observed_without_its_values_is_not_effort_support():
    """The claude row on this host: `--effort` at 2.1.269, values never exercised."""
    controls = cat.effort_controls_from_findings(
        {"controls_observed": ["--effort", "--model", "--resume"]}
    )
    assert controls.supported is False
    assert controls.delivery_mechanism == "unsupported"
    assert controls.resolve("high") == (None, "unsupported")
    assert "vocabulary" in controls.unsupported_reason


def test_a_config_key_row_with_observed_values_is_supported_as_a_config_key():
    """The codex row's shape: no `--effort` flag, effort arrives via `--config`."""
    controls = cat.effort_controls_from_findings(
        {
            "controls_observed": ["--config", "--model"],
            "effort_control": {"kind": "config-key", "native_values": ["low", "medium", "high"]},
        }
    )
    assert controls.supported and controls.delivery_mechanism == "config_key"
    assert controls.resolve("high") == ("high", "native")


def test_a_declared_mapping_is_honoured_and_an_undeclared_one_is_never_invented():
    controls = cat.effort_controls_from_findings(
        {
            "effort_control": {
                "kind": "flag",
                "native_values": ["fast", "deep"],
                "mapping": {"high": "deep"},
            }
        }
    )
    assert controls.resolve("high") == ("deep", "mapped")
    assert controls.resolve("low") == (None, "unsupported"), "invented a cross-vendor mapping"


def test_a_row_with_no_effort_evidence_at_all_is_unsupported():
    assert cat.effort_controls_from_findings({}).supported is False
    assert cat.effort_controls_from_findings({"controls_observed": ["--model"]}).supported is False


# ── deriving a catalog from the verification register ([INV-12]) ─────────────────────


class _Row:
    def __init__(self, **kwargs):
        self.kind = "backend"
        self.verified_version = "1.0.0"
        self.verified_by = "audit"
        self.findings = {}
        self.__dict__.update(kwargs)


def _register(**rows) -> dict:
    return rows


def test_an_unverified_row_is_never_catalogued():
    built = cat.catalog_from_register(
        _register(ghost=_Row(verified_version="")),
        defaults={"ghost": {"available_models": ["m"]}},
    )
    assert built.entries == {}


def test_a_row_with_no_maestro_driver_is_skipped_unless_explicitly_allowed():
    register = _register(agy=_Row(findings={"maestro_driver": False, "controls_observed": []}))
    defaults = {"agy": {"available_models": ["gemini-3"]}}
    assert cat.catalog_from_register(register, defaults=defaults).entries == {}
    allowed = cat.catalog_from_register(register, defaults=defaults, require_driver=False)
    assert "agy" in allowed.entries


def test_capability_facts_come_from_the_row_and_not_from_the_defaults():
    """A stale default must never be able to overstate a verified CLI."""
    register = _register(
        codex=_Row(
            findings={
                "controls_observed": ["--config", "--sandbox"],
                "declared_capabilities": {"sandbox": True, "usage_telemetry": True},
            }
        )
    )
    built = cat.catalog_from_register(
        register, defaults={"codex": {"available_models": ["gpt-5.6-terra"]}}
    )
    row = built.entry("codex")
    assert row.permission_enforcement == "directory_isolation"
    assert row.telemetry_quality == "window_percentage"
    assert row.effort_controls.supported is False, "no native values were ever observed"
    assert row.verified_by == "audit"


def test_the_shipped_register_produces_only_verified_entries():
    """Against the real `maestro/thirdparty.json`, not a fixture."""
    from maestro import thirdparty

    register = thirdparty.load_register()
    built = cat.catalog_from_register(
        register,
        defaults={subject: {"available_models": ["placeholder"]} for subject in register},
    )
    assert built.entries, "the shipped register catalogued nothing at all"
    for backend_id, row in built.entries.items():
        assert row.installed_version, f"{backend_id} catalogued without a verified version"
        assert register[backend_id].findings.get("maestro_driver") is not False


# ── quota observations ([INV-A07]) ───────────────────────────────────────────────────


def _observation(**overrides) -> cat.QuotaObservation:
    now = datetime.now(timezone.utc)
    body = dict(
        pool_id="claude-max",
        windows={
            300: cat.QuotaWindow(300, used_pct=40.0, resets_at=int(now.timestamp()) + 600),
            10080: cat.QuotaWindow(10080, used_pct=91.0, resets_at=int(now.timestamp()) + 60_000),
        },
        observed_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        written_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    body.update(overrides)
    return cat.QuotaObservation(**body)


def test_an_observation_carries_all_four_a07_fields():
    observation = _observation()
    document = observation.to_dict()
    for key in ("observed_at", "written_at", "staleness_bound", "carried_forward"):
        assert key in document
    assert document["acquisition"] in cat.ACQUISITION_PATHS


def test_windows_are_keyed_by_duration_and_an_absent_window_is_none_not_zero():
    weekly_only = _observation(windows={10080: cat.QuotaWindow(10080, used_pct=80.0)})
    assert weekly_only.five_hour is None
    assert weekly_only.max_used_pct() == 80.0, "a threshold keyed to 5h could never fire"


def test_unmeasurable_usage_is_not_headroom():
    blind = _observation(windows={300: cat.QuotaWindow(300)})
    assert blind.max_used_pct() is None
    assert blind.exhausted(pause_pct=92.0) is False
    assert blind.earliest_reset() is None


def test_a_reading_older_than_its_bound_is_stale_and_an_unparseable_one_is_too():
    old = datetime.now(timezone.utc) - timedelta(seconds=3600)
    stale = _observation(observed_at=old.strftime("%Y-%m-%dT%H:%M:%SZ"), staleness_bound=900)
    assert stale.is_stale() and not stale.fresh_enough()
    assert _observation(observed_at="not a timestamp").is_stale()
    assert _observation().fresh_enough()


def test_carrying_a_reading_forward_moves_only_written_at_and_forfeits_freshness():
    original = _observation()
    carried = original.carry_forward(at="2099-01-01T00:00:00Z")
    assert carried.observed_at == original.observed_at, "a carried reading was made younger"
    assert carried.written_at == "2099-01-01T00:00:00Z"
    assert carried.carried_forward is True
    assert carried.fresh_enough() is False, "carried evidence was accepted as headroom"


def test_from_usage_preserves_the_gap_between_measurement_and_recording():
    from maestro.backends.base import Usage, WindowUsage

    usage = Usage(
        windows={300: WindowUsage(used_pct=95.0, resets_at=1_800_000_000)},
        updated_at="2026-09-12T10:00:00Z",
    )
    observation = cat.QuotaObservation.from_usage(
        usage, pool_id="claude-max", written_at="2026-09-12T10:30:00Z"
    )
    assert observation.observed_at == "2026-09-12T10:00:00Z"
    assert observation.written_at == "2026-09-12T10:30:00Z"
    assert observation.acquisition == "push"
    assert observation.five_hour.used_pct == 95.0


def test_an_unknown_acquisition_path_or_nonpositive_bound_is_refused():
    with pytest.raises(cat.CatalogError):
        _observation(acquisition="osmosis")
    with pytest.raises(cat.CatalogError):
        _observation(staleness_bound=0)


# ── pools: exhaustion pauses dispatch, never running workers ─────────────────────────


def test_a_saturated_pool_refuses_without_claiming_a_reset_time():
    pool = cat.PoolState("p", concurrency_limit=2)
    pool.reserve()
    pool.reserve()
    verdict = pool.admit(now_epoch=1_000)
    assert not verdict.admitted and verdict.reason == "pool_saturated"
    assert verdict.retry_after_sec is None, "invented a reset time for a concurrency limit"
    pool.release()
    assert pool.admit(now_epoch=1_000).admitted


def test_quota_exhaustion_refuses_with_the_windows_own_reset():
    pool = cat.PoolState("p", concurrency_limit=4)
    pool.observation = _observation(
        windows={300: cat.QuotaWindow(300, used_pct=95.0, resets_at=1_600)}
    )
    verdict = pool.admit(pause_pct=92.0, now_epoch=1_000)
    assert not verdict.admitted and verdict.reason == "quota_exhausted"
    assert verdict.retry_after_sec == 600.0


def test_pausing_an_exhausted_pool_stops_new_work_and_leaves_workers_running():
    """The P6 acceptance condition, stated as the two assertions that make it true."""
    resources = cat.ResourcePoolState()
    pool = resources.pool("claude-max", concurrency_limit=3)
    pool.reserve()
    pool.reserve()
    resources.observe(
        _observation(windows={300: cat.QuotaWindow(300, used_pct=99.0, resets_at=2_000)})
    )

    assert resources.pause_exhausted_pools() == ("claude-max",)

    assert pool.active == 2, "pausing a pool killed its healthy running workers"
    refusal = resources.admit("claude-max", now_epoch=1_000)
    assert not refusal.admitted and refusal.reason in ("pool_paused", "quota_exhausted")
    assert refusal.retry_after_sec == 1_000.0


def test_a_pause_expires_on_its_own_once_the_window_resets():
    resources = cat.ResourcePoolState()
    pool = resources.pool("p", concurrency_limit=1)
    pool.pause(until=1_500, reason="quota")
    assert not resources.admit("p", now_epoch=1_400).admitted
    assert resources.admit("p", now_epoch=1_600).admitted
    assert pool.paused_until is None


def test_an_unknown_pool_admits_rather_than_blocking_work_on_a_pool_nobody_declared():
    assert cat.ResourcePoolState().admit("never-seen").admitted


# ── outcome statistics with shape keys ([INV-A07]) ───────────────────────────────────


def _shape(**overrides) -> cat.ShapeKey:
    body = dict(
        backend_id="claude",
        model_id="sonnet-5",
        effort="high",
        role_version="1.2.0",
        task_class="ordinary",
        template_id="ordinary",
        template_version="2",
        active_fragment_set=("implementer@1", "gate@1"),
    )
    body.update(overrides)
    return cat.ShapeKey(**body)


def test_the_shape_key_carries_all_eight_a07_dimensions():
    assert len(_shape().as_tuple()) == 8


@pytest.mark.parametrize(
    "field,value",
    [
        ("role_version", "1.3.0"),
        ("template_version", "3"),
        ("task_class", "architectural"),
        ("active_fragment_set", ("implementer@2", "gate@1")),
        ("effort", "low"),
    ],
)
def test_a_change_in_any_shape_dimension_is_a_different_shape(field, value):
    """Otherwise a template regression is silently attributed to the model."""
    stats = cat.OutcomeStatistics(minimum_observations=1)
    for _ in range(3):
        stats.record(_shape(), success=True)
    assert stats.observations(_shape(**{field: value})) == 0


def test_fragment_set_order_does_not_split_one_shape_into_two():
    stats = cat.OutcomeStatistics(minimum_observations=1)
    stats.record(_shape(active_fragment_set=("a", "b")), success=True)
    stats.record(_shape(active_fragment_set=("b", "a")), success=False)
    assert stats.observations(_shape(active_fragment_set=("a", "b"))) == 2


def test_a_rate_below_the_evidence_floor_is_none_rather_than_a_number():
    stats = cat.OutcomeStatistics(minimum_observations=5)
    stats.record(_shape(), success=True)
    assert stats.success_rate(_shape()) is None, "one sample was treated as a measurement"
    for _ in range(4):
        stats.record(_shape(), success=True)
    assert stats.success_rate(_shape()) == 1.0


def test_never_attempted_and_always_failed_are_different_states():
    stats = cat.OutcomeStatistics(minimum_observations=1)
    assert stats.success_rate(_shape()) is None
    for _ in range(2):
        stats.record(_shape(), success=False)
    assert stats.success_rate(_shape()) == 0.0


def test_statistics_serialise_with_their_evidence_floor():
    stats = cat.OutcomeStatistics(minimum_observations=3)
    stats.record(_shape(), success=True)
    document = stats.to_dict()
    assert document["minimum_observations"] == 3
    assert document["records"][0]["attempts"] == 1


# ── Antigravity routing judgement (P07B) ─────────────────────────────────────────────


def _agy_probe_ids() -> list[str]:
    import json
    from pathlib import Path

    probe = Path(__file__).resolve().parents[2] / (
        "artifacts/graph-engineering/p07b-agy-1.2.2-probe.json"
    )
    return json.loads(probe.read_text(encoding="utf-8"))["models_listed"]["ids"]


def test_the_agy_defaults_list_exactly_the_probed_models():
    agy = cat.ANTIGRAVITY_CATALOG_DEFAULTS["agy"]
    assert sorted(agy["available_models"]) == sorted(_agy_probe_ids())


def test_every_listed_agy_model_has_an_explicit_profile():
    """`profile()` grades an unprofiled id `balanced`; on agy that would be a promotion."""
    agy = cat.ANTIGRAVITY_CATALOG_DEFAULTS["agy"]
    missing = sorted(set(agy["available_models"]) - set(agy["model_profiles"]))
    assert not missing, f"agy models listed without an explicit profile: {missing}"


def test_only_gemini_3_8_flash_high_is_balanced_on_agy():
    profiles = cat.ANTIGRAVITY_CATALOG_DEFAULTS["agy"]["model_profiles"]
    graded = {model_id: profile.strength for model_id, profile in profiles.items()}
    assert {m for m, s in graded.items() if s != "light"} == {"gemini-3.8-flash-high"}
    assert graded["gemini-3.8-flash-high"] == "balanced"


def test_the_agy_row_catalogues_under_its_own_backend_id_with_per_model_limits():
    from maestro import thirdparty

    built = cat.catalog_from_register(
        thirdparty.load_register(), defaults=cat.ANTIGRAVITY_CATALOG_DEFAULTS
    )
    entry = built.entry("antigravity_cli")
    assert entry.usage_pool_id == "antigravity-gemini"
    assert entry.billing_category == "subscription"
    assert entry.installed_version == "1.2.3"
    assert entry.effort_controls.supported is False
    assert "native values" in entry.effort_controls.unsupported_reason
    assert entry.limits_for("gemini-3.8-flash-high").max_input_tokens == 1_048_576
    assert entry.limits_for("gemini-3.1-pro-low").effective_context_ceiling == 108_000
    assert entry.limits_for("gpt-oss-120b-medium").max_input_tokens == 80_000
    assert entry.limits_for("claude-sonnet-4-6").max_input_tokens == 160_000


def test_the_agy_3p_models_bill_against_the_3p_pool_and_the_gemini_ones_do_not():
    """agy resells claude-* / gpt-oss-* out of its 3p-5h / 3p-weekly quota (P07B)."""
    from maestro import thirdparty

    built = cat.catalog_from_register(
        thirdparty.load_register(), defaults=cat.ANTIGRAVITY_CATALOG_DEFAULTS
    )
    entry = built.entry("antigravity_cli")
    third_party = {
        model_id for model_id in entry.available_models
        if entry.pool_for(model_id) == cat.AGY_THIRD_PARTY_POOL
    }
    assert third_party == {
        "claude-sonnet-4-6", "claude-opus-4-6-thinking", "gpt-oss-120b-medium"
    }
    assert all(
        entry.pool_for(model_id) == "antigravity-gemini"
        for model_id in entry.available_models
        if model_id.startswith("gemini-")
    )


def test_a_newly_listed_agy_model_without_a_profile_fails_the_profile_check():
    """The check above is what catches it; this proves the check can fail."""
    agy = dict(cat.ANTIGRAVITY_CATALOG_DEFAULTS["agy"])
    agy["available_models"] = [*agy["available_models"], "gemini-9-flash"]
    assert set(agy["available_models"]) - set(agy["model_profiles"]) == {"gemini-9-flash"}


# ── claude / codex routing defaults (P12A S2, Dan 2026-09-15) ────────────────────────


@pytest.mark.parametrize("subject, backend_id, pool, graded", [
    ("claude", "claude", "claude-subscription", {
        "claude-opus-5-5": ("strong", 1.25, 240_000),
        "claude-sonnet-5-5": ("balanced", 0.5, 220_000),
        "claude-haiku-4-5-20251001": ("light", 0.25, 120_000),
    }),
    ("codex", "codex", "codex-subscription", {
        "gpt-6-astra": ("strong", 2.5, 230_000),
        "gpt-6.1-sol": ("strong", 1.0, 240_000),
        "gpt-6-luna": ("balanced", 0.5, 180_000),
        "gpt-5.6-terra": ("light", 0.05, 140_000),
    }),
])
def test_the_shipped_claude_and_codex_rows_catalogue_the_chosen_defaults(
    subject, backend_id, pool, graded
):
    from maestro import thirdparty

    built = cat.catalog_from_register(
        thirdparty.load_register(), defaults=cat.SHIPPED_CATALOG_DEFAULTS
    )
    entry = built.entry(backend_id)
    assert entry.usage_pool_id == pool
    assert entry.billing_category == "subscription"
    assert entry.concurrency_limit == 2
    assert sorted(entry.available_models) == sorted(graded)
    for model_id, (strength, cost, ceiling) in graded.items():
        profile = entry.profile(model_id)
        assert (profile.strength, profile.relative_cost) == (strength, cost)
        assert entry.limits_for(model_id).effective_context_ceiling == ceiling


def test_the_shipped_defaults_cover_every_driver_backed_register_row():
    from maestro import thirdparty

    built = cat.catalog_from_register(
        thirdparty.load_register(), defaults=cat.SHIPPED_CATALOG_DEFAULTS
    )
    assert {"claude", "codex", "antigravity_cli"} <= set(built.backend_ids())
