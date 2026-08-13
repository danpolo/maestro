"""Role -> (backend, model) resolution, the fallback chain, and the purity of both.

`maestro.roles` is the module that decides *which* agent runs a job and *under which
model*. Two properties matter enough to be pinned here rather than assumed:

* **It is pure.** Availability and quota are inputs, not measurements. An autouse fixture
  makes every spelling of "execute something" raise, and another replaces the project
  loader, so a resolution that shelled out or read a real file would fail the whole
  module rather than quietly work.
* **It knows no backend by name.** The default, the known set and the default chain all
  come from `maestro.backends.registry`. A test registers a third, fictional backend to
  prove the chain is derived and not a two-element coincidence, and an AST check pins the
  module's claim that it writes no backend name down.

Nothing here launches an agent, spends a token or touches a network.
"""
from __future__ import annotations

import ast
import builtins
import dataclasses
import os
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from maestro import config as _config
from maestro import gates, merge, roles
from maestro.backends import registry
from maestro.backends.registry import DriverRef
from maestro.selfheal import diagnose

#: Derived, never assumed: the tests below must keep working when a driver is added.
DEFAULT = registry.normalise_name(registry.DEFAULT_BACKEND)
OTHER = next(name for name in registry.known_backends() if name != DEFAULT)


# ── safety nets ──


@pytest.fixture(autouse=True)
def _no_execution(monkeypatch):
    """Role resolution may never execute anything. Every route out raises."""

    def forbidden(*args, **kwargs):
        raise AssertionError(f"role resolution executed something: {args!r}")

    for attribute in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, attribute, forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    # The registry's only executing function, guarded even though resolution never
    # reaches it — if that ever changes, this module notices first.
    monkeypatch.setattr(registry, "_probe_version", forbidden)


class _Loader:
    """A stand-in for `maestro.config.load_project_yaml` that counts its calls."""

    def __init__(self):
        self.result: object = {}
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture(autouse=True)
def loader(monkeypatch):
    """No test may read the operator's real `project.yaml`."""
    stub = _Loader()
    monkeypatch.setattr(_config, "load_project_yaml", stub)
    return stub


@pytest.fixture
def third_backend(monkeypatch):
    """Register a fictional third driver, so chain behaviour is not a coincidence.

    The name sorts last, which makes it the tail of the derived default chain.
    """

    class ZebraBackend:
        name = "zebra"

    module = ModuleType("maestro.backends.zebra")
    module.ZebraBackend = ZebraBackend
    ZebraBackend.__module__ = module.__name__

    monkeypatch.setitem(
        registry.BACKENDS,
        "zebra",
        DriverRef(
            name="zebra",
            module=module.__name__,
            attr="ZebraBackend",
            binary="zebra",
        ),
    )
    monkeypatch.setattr(registry, "import_module", lambda _name: module)
    return ZebraBackend


def _config_with(role: str, **entry) -> dict:
    return {roles.ROLES_KEY: {role: entry}}


# ── normalise_role ──


def test_normalise_role_lowercases_and_strips():
    assert roles.normalise_role("  Implementer \n") == "implementer"


def test_normalise_role_of_nothing_is_the_empty_string():
    assert roles.normalise_role(None) == ""
    assert roles.normalise_role("") == ""
    assert roles.normalise_role("   ") == ""


def test_normalise_role_accepts_a_non_string():
    assert roles.normalise_role(5) == "5"


def test_known_roles_are_the_three_documented_ones():
    assert roles.KNOWN_ROLES == (
        roles.ROLE_IMPLEMENTER,
        roles.ROLE_JUDGE,
        roles.ROLE_DIAGNOSER,
    )
    assert all(role == roles.normalise_role(role) for role in roles.KNOWN_ROLES)


# ── default_chain ──


def test_default_chain_is_the_default_backend_then_the_rest():
    assert roles.default_chain() == (DEFAULT,) + tuple(
        name for name in registry.known_backends() if name != DEFAULT
    )


def test_default_chain_grows_when_a_driver_is_registered(third_backend):
    assert roles.default_chain() == (DEFAULT, OTHER, "zebra")


def test_default_chain_omits_an_unregistered_default(monkeypatch):
    monkeypatch.delitem(registry.BACKENDS, DEFAULT)
    assert roles.default_chain() == (OTHER,)


# ── role_config ──


def test_role_config_defaults_everything_when_configuration_is_silent():
    settings = roles.role_config(roles.ROLE_IMPLEMENTER, config={})
    assert settings.role == roles.ROLE_IMPLEMENTER
    assert settings.backend == DEFAULT
    assert settings.models == {}
    assert settings.chain == roles.default_chain()


def test_role_config_reads_the_backend_and_the_per_backend_models():
    settings = roles.role_config(
        roles.ROLE_JUDGE,
        config=_config_with(
            roles.ROLE_JUDGE,
            backend=OTHER,
            models={DEFAULT: "model-a", OTHER: "model-b"},
        ),
    )
    assert settings.backend == OTHER
    assert settings.models == {DEFAULT: "model-a", OTHER: "model-b"}


def test_role_config_normalises_the_role_key_and_the_model_backend_names():
    settings = roles.role_config(
        "  JUDGE ",
        config={roles.ROLES_KEY: {"judge": {"models": {OTHER.upper(): "  model-b  "}}}},
    )
    assert settings.role == "judge"
    assert settings.models == {OTHER: "model-b"}


def test_role_config_reads_a_bare_string_entry_as_a_backend_choice():
    settings = roles.role_config(
        roles.ROLE_JUDGE, config={roles.ROLES_KEY: {roles.ROLE_JUDGE: OTHER}}
    )
    assert settings.backend == OTHER


def test_role_config_reads_a_singular_model_key_for_the_chosen_backend():
    settings = roles.role_config(
        roles.ROLE_JUDGE,
        config=_config_with(roles.ROLE_JUDGE, backend=OTHER, model="model-b"),
    )
    assert settings.models == {OTHER: "model-b"}


def test_the_per_backend_table_wins_over_the_singular_model_key():
    settings = roles.role_config(
        roles.ROLE_JUDGE,
        config=_config_with(
            roles.ROLE_JUDGE,
            backend=OTHER,
            model="singular",
            models={OTHER: "tabulated"},
        ),
    )
    assert settings.models == {OTHER: "tabulated"}


def test_role_config_degrades_an_unregistered_backend_to_the_default():
    settings = roles.role_config(
        roles.ROLE_JUDGE, config=_config_with(roles.ROLE_JUDGE, backend="gpt-9000")
    )
    assert settings.backend == DEFAULT


def test_a_singular_model_follows_the_backend_the_name_degraded_to():
    """`backend:` was junk, so the model it named is attached to the default instead."""
    settings = roles.role_config(
        roles.ROLE_JUDGE,
        config=_config_with(roles.ROLE_JUDGE, backend="gpt-9000", model="model-x"),
    )
    assert settings.backend == DEFAULT
    assert settings.models == {DEFAULT: "model-x"}


@pytest.mark.parametrize(
    "document",
    [
        {},
        {roles.ROLES_KEY: None},
        {roles.ROLES_KEY: "not a mapping"},
        {roles.ROLES_KEY: []},
        {roles.ROLES_KEY: {roles.ROLE_JUDGE: None}},
        {roles.ROLES_KEY: {roles.ROLE_JUDGE: 17}},
        {roles.ROLES_KEY: {roles.ROLE_JUDGE: "   "}},
        {roles.ROLES_KEY: {roles.ROLE_JUDGE: {"backend": None, "models": "junk"}}},
        {roles.ROLES_KEY: {roles.ROLE_JUDGE: {"models": {DEFAULT: "   "}}}},
    ],
)
def test_a_malformed_roles_block_degrades_to_the_defaults(document):
    settings = roles.role_config(roles.ROLE_JUDGE, config=document)
    assert settings.backend == DEFAULT
    assert settings.models == {}
    assert settings.chain == roles.default_chain()


@pytest.mark.parametrize("document", [None, [], "junk", 42, {"roles": {}}])
def test_a_non_mapping_document_reads_as_no_configuration(document, loader):
    loader.result = document
    assert roles.role_config(roles.ROLE_JUDGE).backend == DEFAULT


def test_role_keys_are_matched_case_sensitively_against_configuration():
    """CURRENT BEHAVIOUR. The requested role is lowercased; the configured key is not,
    so a capitalised `Judge:` in project.yaml is silently not found."""
    document = {roles.ROLES_KEY: {"Judge": {"backend": OTHER}}}
    assert roles.role_config("Judge", config=document).backend == DEFAULT
    assert roles.role_config("judge", config=document).backend == DEFAULT


# ── the fallback chain as configured ──


def test_a_configured_chain_replaces_the_default_one():
    settings = roles.role_config(
        roles.ROLE_JUDGE, config={roles.CHAIN_KEY: [OTHER, DEFAULT]}
    )
    assert settings.chain == (OTHER, DEFAULT)


def test_a_configured_chain_drops_duplicates_and_keeps_first_position():
    settings = roles.role_config(
        roles.ROLE_JUDGE, config={roles.CHAIN_KEY: [OTHER, OTHER, DEFAULT, OTHER]}
    )
    assert settings.chain == (OTHER, DEFAULT)


def test_a_configured_chain_drops_a_backend_that_has_no_driver():
    settings = roles.role_config(
        roles.ROLE_JUDGE, config={roles.CHAIN_KEY: ["gpt-9000", OTHER]}
    )
    assert settings.chain == (OTHER,)


def test_a_chain_naming_only_unknown_backends_falls_back_to_the_default_chain():
    settings = roles.role_config(
        roles.ROLE_JUDGE, config={roles.CHAIN_KEY: ["gpt-9000", "nope"]}
    )
    assert settings.chain == roles.default_chain()


@pytest.mark.parametrize(
    "raw", [None, "codex", {}, 7, [], [None, 3], [{"backend": "codex"}]]
)
def test_a_malformed_chain_falls_back_to_the_default_chain(raw):
    settings = roles.role_config(roles.ROLE_JUDGE, config={roles.CHAIN_KEY: raw})
    assert settings.chain == roles.default_chain()


def test_chain_names_are_normalised():
    settings = roles.role_config(
        roles.ROLE_JUDGE, config={roles.CHAIN_KEY: [f"  {OTHER.upper()}  ", DEFAULT]}
    )
    assert settings.chain == (OTHER, DEFAULT)


# ── models ──


def test_every_known_role_has_a_default_model_on_the_default_backend():
    for role in roles.KNOWN_ROLES:
        assert roles.model_for(role, config={})


def test_a_role_with_no_default_on_a_backend_resolves_to_no_model():
    """`None` means "let the backend pick" — inventing a model id would be worse."""
    assert roles.model_for(roles.ROLE_IMPLEMENTER, OTHER, config={}) is None


def test_an_unknown_role_has_no_default_model():
    assert roles.model_for("archivist", config={}) is None


def test_a_configured_model_overrides_the_default():
    assert (
        roles.model_for(
            roles.ROLE_IMPLEMENTER,
            config=_config_with(roles.ROLE_IMPLEMENTER, models={DEFAULT: "model-a"}),
        )
        == "model-a"
    )


def test_model_for_defaults_to_the_roles_own_backend():
    document = _config_with(
        roles.ROLE_JUDGE, backend=OTHER, models={DEFAULT: "model-a", OTHER: "model-b"}
    )
    assert roles.model_for(roles.ROLE_JUDGE, config=document) == "model-b"
    assert roles.model_for(roles.ROLE_JUDGE, DEFAULT, config=document) == "model-a"


def test_model_for_normalises_the_backend_name():
    document = _config_with(roles.ROLE_JUDGE, models={OTHER: "model-b"})
    assert roles.model_for(roles.ROLE_JUDGE, f"  {OTHER.upper()} ", config=document) == (
        "model-b"
    )


def test_model_for_an_unregistered_backend_is_none():
    assert roles.model_for(roles.ROLE_JUDGE, "gpt-9000", config={}) is None


def test_default_models_reproduce_the_call_sites_they_replace():
    """The two tables must not drift apart silently (`roles` module docstring)."""
    from maestro import implementer

    assert (
        roles.model_for(roles.ROLE_IMPLEMENTER, config={})
        == implementer._DEFAULT_IMPLEMENTER_MODEL
    )
    assert roles.model_for(roles.ROLE_DIAGNOSER, config={}) == diagnose.JUDGE_MODEL


def test_the_judge_default_is_the_model_its_call_sites_hardcode():
    judge = roles.model_for(roles.ROLE_JUDGE, config={})
    for module in (gates, merge):
        source = Path(module.__file__).read_text(encoding="utf-8")
        literals = {
            node.value
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert judge in literals, f"{module.__name__} no longer hardcodes {judge!r}"


# ── resolve: the happy path ──


def test_resolve_returns_the_configured_backend_and_model():
    outcome = roles.resolve(
        roles.ROLE_JUDGE,
        config=_config_with(roles.ROLE_JUDGE, backend=OTHER, models={OTHER: "model-b"}),
    )
    assert (outcome.role, outcome.backend, outcome.model) == (
        roles.ROLE_JUDGE,
        OTHER,
        "model-b",
    )
    assert outcome.usable is True
    assert outcome.reason == ""
    assert outcome.switched is False


def test_resolve_defaults_an_unknown_role_to_the_default_backend_and_no_model():
    outcome = roles.resolve("archivist", config={})
    assert (outcome.backend, outcome.model, outcome.usable) == (DEFAULT, None, True)


def test_resolve_candidates_are_the_preferred_backend_then_the_rest_of_the_chain():
    outcome = roles.resolve(roles.ROLE_JUDGE, config={}, preferred=OTHER)
    assert outcome.candidates == (OTHER,) + tuple(
        name for name in roles.default_chain() if name != OTHER
    )


def test_resolve_assumes_every_backend_is_installed_when_availability_is_unmeasured():
    outcome = roles.resolve(roles.ROLE_JUDGE, config={}, available=None)
    assert (outcome.backend, outcome.usable) == (DEFAULT, True)


# ── resolve: the fallback chain, in both directions ──


def test_an_exhausted_default_backend_falls_forward_to_the_next_in_the_chain():
    outcome = roles.resolve(roles.ROLE_JUDGE, config={}, exhausted=[DEFAULT])
    assert outcome.backend == OTHER
    assert outcome.preferred == DEFAULT
    assert outcome.switched is True
    assert outcome.reason == f"{DEFAULT} exhausted"


def test_an_exhausted_preferred_backend_falls_back_the_other_way():
    """The chain works in both directions: whichever backend leads, the others follow."""
    outcome = roles.resolve(
        roles.ROLE_JUDGE, config={}, exhausted=[OTHER], preferred=OTHER
    )
    assert outcome.backend == DEFAULT
    assert outcome.preferred == OTHER
    assert outcome.switched is True
    assert outcome.reason == f"{OTHER} exhausted"


def test_a_missing_binary_is_skipped_just_like_an_exhausted_quota():
    outcome = roles.resolve(roles.ROLE_JUDGE, config={}, available=[OTHER])
    assert outcome.backend == OTHER
    assert outcome.reason == f"{DEFAULT} unavailable"


def test_exhaustion_is_reported_ahead_of_unavailability():
    outcome = roles.resolve(
        roles.ROLE_JUDGE, config={}, available=[OTHER], exhausted=[DEFAULT, OTHER]
    )
    assert outcome.usable is False
    assert outcome.reason == f"{DEFAULT} exhausted; {OTHER} exhausted"


def test_the_chain_is_walked_past_more_than_one_dead_backend(third_backend):
    outcome = roles.resolve(
        roles.ROLE_JUDGE, config={}, available=[OTHER, "zebra"], exhausted=[OTHER]
    )
    assert outcome.backend == "zebra"
    assert outcome.reason == f"{DEFAULT} unavailable; {OTHER} exhausted"


def test_a_fallback_resolves_the_same_role_under_the_new_backends_model():
    """The point of per-backend models: no separate equivalence table for a switch."""
    document = _config_with(
        roles.ROLE_IMPLEMENTER, models={DEFAULT: "model-a", OTHER: "model-b"}
    )
    assert roles.resolve(roles.ROLE_IMPLEMENTER, config=document).model == "model-a"
    assert (
        roles.resolve(roles.ROLE_IMPLEMENTER, config=document, exhausted=[DEFAULT]).model
        == "model-b"
    )


def test_a_fallback_backend_with_no_configured_model_resolves_to_none():
    outcome = roles.resolve(roles.ROLE_IMPLEMENTER, config={}, exhausted=[DEFAULT])
    assert (outcome.backend, outcome.model) == (OTHER, None)


def test_a_configured_chain_orders_the_fallback():
    outcome = roles.resolve(
        roles.ROLE_JUDGE,
        config={roles.CHAIN_KEY: [OTHER, DEFAULT]},
        exhausted=[DEFAULT],
    )
    assert outcome.candidates == (DEFAULT, OTHER)
    assert outcome.backend == OTHER


def test_availability_and_exhaustion_names_are_normalised():
    outcome = roles.resolve(
        roles.ROLE_JUDGE,
        config={},
        available=[f" {DEFAULT.upper()} ", OTHER],
        exhausted=[f"  {DEFAULT.title()}  "],
    )
    assert outcome.backend == OTHER


@pytest.mark.parametrize("measured", ["available", "exhausted"])
def test_a_bare_string_is_one_backend_name_not_a_set_of_characters(measured):
    outcome = roles.resolve(roles.ROLE_JUDGE, config={}, **{measured: OTHER})
    assert outcome.backend == (OTHER if measured == "available" else DEFAULT)


def test_a_non_string_entry_in_availability_names_no_backend():
    """A `Path` or `BinaryInfo` handed in by mistake counts as nothing installed."""
    outcome = roles.resolve(roles.ROLE_JUDGE, config={}, available=[Path("/bin/claude")])
    assert outcome.usable is False


# ── resolve: nothing can run ──


def test_every_backend_exhausted_returns_the_preferred_one_as_unusable():
    outcome = roles.resolve(
        roles.ROLE_JUDGE, config={}, exhausted=registry.known_backends()
    )
    assert outcome.usable is False
    assert outcome.backend == DEFAULT
    assert outcome.preferred == DEFAULT
    assert outcome.switched is False
    assert outcome.model == roles.model_for(roles.ROLE_JUDGE, DEFAULT, config={})
    for name in registry.known_backends():
        assert f"{name} exhausted" in outcome.reason


def test_nothing_installed_returns_the_preferred_one_as_unusable():
    outcome = roles.resolve(roles.ROLE_JUDGE, config={}, available=[])
    assert outcome.usable is False
    assert all(
        f"{name} unavailable" in outcome.reason for name in registry.known_backends()
    )


def test_an_unusable_resolution_keeps_the_preferred_backend_not_the_last_candidate():
    outcome = roles.resolve(
        roles.ROLE_JUDGE,
        config={},
        exhausted=registry.known_backends(),
        preferred=OTHER,
    )
    assert outcome.backend == OTHER
    assert outcome.switched is False


def test_an_availability_set_naming_no_registered_backend_leaves_nothing_usable():
    outcome = roles.resolve(roles.ROLE_JUDGE, config={}, available=["gpt-9000"])
    assert outcome.usable is False


# ── resolve: the preferred override ──


def test_a_preferred_backend_leads_the_chain():
    outcome = roles.resolve(roles.ROLE_JUDGE, config={}, preferred=f" {OTHER.upper()} ")
    assert outcome.backend == OTHER
    assert outcome.preferred == OTHER
    assert outcome.switched is False


def test_an_unregistered_preferred_name_is_ignored_in_favour_of_configuration():
    outcome = roles.resolve(
        roles.ROLE_JUDGE,
        config=_config_with(roles.ROLE_JUDGE, backend=OTHER),
        preferred="gpt-9000",
    )
    assert outcome.backend == OTHER
    assert outcome.preferred == OTHER


@pytest.mark.parametrize("preferred", [None, "", "   ", 0])
def test_an_empty_preference_falls_through_to_the_configured_backend(preferred):
    outcome = roles.resolve(roles.ROLE_JUDGE, config={}, preferred=preferred)
    assert outcome.backend == DEFAULT
    assert outcome.preferred == DEFAULT


def test_the_preferred_backend_is_not_repeated_in_the_candidates():
    outcome = roles.resolve(roles.ROLE_JUDGE, config={}, preferred=OTHER)
    assert outcome.candidates.count(OTHER) == 1


# ── Resolution.switched ──


def test_switched_is_false_when_the_preferred_backend_was_used():
    outcome = roles.resolve(roles.ROLE_JUDGE, config={})
    assert outcome.backend == outcome.preferred
    assert outcome.switched is False


def test_switched_is_true_only_when_the_preferred_backend_was_passed_over():
    assert roles.Resolution(
        role="judge", backend=OTHER, model=None, preferred=DEFAULT, candidates=()
    ).switched
    assert not roles.Resolution(
        role="judge", backend=DEFAULT, model=None, preferred=DEFAULT, candidates=()
    ).switched


def test_a_resolution_is_frozen():
    outcome = roles.resolve(roles.ROLE_JUDGE, config={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        outcome.backend = OTHER
    with pytest.raises(dataclasses.FrozenInstanceError):
        roles.role_config(roles.ROLE_JUDGE, config={}).backend = OTHER


# ── backend_for ──


def test_backend_for_is_resolve_dot_backend():
    document = _config_with(roles.ROLE_JUDGE, backend=OTHER)
    assert roles.backend_for(roles.ROLE_JUDGE, config=document) == OTHER
    assert (
        roles.backend_for(roles.ROLE_JUDGE, config=document, exhausted=[OTHER])
        == DEFAULT
    )


def test_backend_for_returns_the_preferred_backend_when_nothing_can_run():
    assert (
        roles.backend_for(
            roles.ROLE_JUDGE, config={}, exhausted=registry.known_backends()
        )
        == DEFAULT
    )


# ── fallback_backend ──


def test_fallback_backend_names_somewhere_else_to_send_the_work():
    assert roles.fallback_backend(roles.ROLE_JUDGE, DEFAULT, config={}) == OTHER


def test_fallback_backend_never_returns_the_backend_it_was_asked_to_leave():
    for current in registry.known_backends():
        assert roles.fallback_backend(roles.ROLE_JUDGE, current, config={}) != current


def test_fallback_backend_is_none_when_the_only_alternative_is_exhausted():
    assert (
        roles.fallback_backend(
            roles.ROLE_JUDGE, DEFAULT, config={}, exhausted=[OTHER]
        )
        is None
    )


def test_fallback_backend_is_none_when_the_only_alternative_is_not_installed():
    assert (
        roles.fallback_backend(roles.ROLE_JUDGE, DEFAULT, config={}, available=[DEFAULT])
        is None
    )


def test_fallback_backend_walks_past_a_dead_alternative(third_backend):
    assert (
        roles.fallback_backend(
            roles.ROLE_JUDGE, DEFAULT, config={}, exhausted=[OTHER]
        )
        == "zebra"
    )


def test_fallback_backend_normalises_the_current_name():
    assert roles.fallback_backend(roles.ROLE_JUDGE, f" {DEFAULT.upper()} ", config={}) == (
        OTHER
    )


def test_fallback_backend_from_an_unregistered_backend_offers_the_default():
    """Nothing rules the default out, so a task running under a name we do not know
    can still be handed somewhere real."""
    assert roles.fallback_backend(roles.ROLE_JUDGE, "gpt-9000", config={}) == DEFAULT


@pytest.mark.parametrize("current", [None, "", "   "])
def test_fallback_backend_from_no_backend_at_all_offers_the_default(current):
    assert roles.fallback_backend(roles.ROLE_JUDGE, current, config={}) == DEFAULT


def test_fallback_backend_honours_a_configured_chain_order(third_backend):
    assert (
        roles.fallback_backend(
            roles.ROLE_JUDGE, DEFAULT, config={roles.CHAIN_KEY: ["zebra", OTHER]}
        )
        == "zebra"
    )


def test_fallback_backend_is_none_when_it_is_the_last_backend_standing(monkeypatch):
    monkeypatch.delitem(registry.BACKENDS, OTHER)
    assert roles.fallback_backend(roles.ROLE_JUDGE, DEFAULT, config={}) is None


# ── driver_for ──


def test_driver_for_returns_the_registered_driver_class():
    assert roles.driver_for(roles.ROLE_JUDGE, config={}) is registry.driver_class(DEFAULT)


def test_driver_for_follows_the_fallback_chain():
    driver = roles.driver_for(roles.ROLE_JUDGE, config={}, exhausted=[DEFAULT])
    assert driver is registry.driver_class(OTHER)


def test_driver_for_resolves_through_the_registry_and_launches_nothing(third_backend):
    driver = roles.driver_for(
        roles.ROLE_JUDGE, config={roles.CHAIN_KEY: ["zebra"]}, exhausted=[DEFAULT]
    )
    assert driver is third_backend


def test_driver_for_reports_an_unimportable_driver(monkeypatch):
    def missing(module_name):
        raise ModuleNotFoundError(f"No module named {module_name!r}", name=module_name)

    monkeypatch.setattr(registry, "import_module", missing)
    with pytest.raises(registry.BackendUnavailable):
        roles.driver_for(roles.ROLE_JUDGE, config={})


# ── purity ──


def test_resolution_executes_no_subprocess():
    """Guarded by the autouse fixture: every subprocess entry point raises."""
    with pytest.raises(AssertionError):
        subprocess.run(["true"])
    assert roles.resolve(roles.ROLE_IMPLEMENTER, config={}).backend == DEFAULT
    assert roles.backend_for(roles.ROLE_JUDGE, config={}) == DEFAULT
    assert roles.model_for(roles.ROLE_DIAGNOSER, config={})
    assert roles.fallback_backend(roles.ROLE_JUDGE, DEFAULT, config={}) == OTHER
    assert roles.driver_for(roles.ROLE_JUDGE, config={})


def test_supplied_configuration_is_never_reloaded(loader):
    loader.result = AssertionError("the loader must not be called")
    assert roles.backend_for(roles.ROLE_JUDGE, config={}) == DEFAULT
    assert roles.role_config(roles.ROLE_JUDGE, config={"roles": {}}).backend == DEFAULT
    assert loader.calls == 0


def test_configuration_is_loaded_once_per_call_when_it_is_not_supplied(loader):
    roles.resolve(roles.ROLE_JUDGE)
    assert loader.calls == 1
    roles.resolve(roles.ROLE_JUDGE, config=None)
    assert loader.calls == 2


def test_the_loader_is_looked_up_at_call_time_not_bound_at_import(loader):
    """Substituting `maestro.config.load_project_yaml` must change what roles reads."""
    loader.result = _config_with(roles.ROLE_JUDGE, backend=OTHER)
    assert roles.backend_for(roles.ROLE_JUDGE) == OTHER


def test_resolution_opens_no_file_when_configuration_is_supplied(monkeypatch):
    real_open = builtins.open

    def guarded(file, *args, **kwargs):
        if not str(file).endswith(".py"):  # let a traceback still read its source
            raise AssertionError(f"role resolution opened {file!r}")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded)
    outcome = roles.resolve(
        roles.ROLE_JUDGE, config=_config_with(roles.ROLE_JUDGE, backend=OTHER)
    )
    assert outcome.backend == OTHER


def test_the_only_file_read_is_the_project_yaml_maestro_config_owns(tmp_path, monkeypatch):
    """End to end through the real loader, pointed at a sandboxed project.yaml."""
    document = tmp_path / "project.yaml"
    document.write_text(
        f"roles:\n"
        f"  judge:\n"
        f"    backend: {OTHER}\n"
        f"    models:\n"
        f"      {OTHER}: model-b\n"
        f"fallback_chain: [{OTHER}, {DEFAULT}]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(_config, "load_project_yaml", _config._load_project_yaml)
    monkeypatch.setattr(_config, "PROJECT_YAML", document)

    settings = roles.role_config(roles.ROLE_JUDGE)
    assert settings.backend == OTHER
    assert settings.models == {OTHER: "model-b"}
    assert settings.chain == (OTHER, DEFAULT)


def test_a_missing_project_yaml_degrades_to_the_documented_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(_config, "load_project_yaml", _config._load_project_yaml)
    monkeypatch.setattr(_config, "PROJECT_YAML", tmp_path / "absent.yaml")
    assert roles.backend_for(roles.ROLE_JUDGE) == DEFAULT


def test_roles_writes_down_no_backend_name():
    """The module's own claim: names live in the registry, never here."""
    source = Path(roles.__file__).read_text(encoding="utf-8")
    literals = {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert not literals & set(registry.known_backends())


# ── degenerate registries: current behaviour, pinned ──


def test_an_unregistered_default_backend_still_wins_resolution(monkeypatch):
    """CURRENT BEHAVIOUR, and arguably wrong: with the default backend unregistered,
    `role_config` degrades to it anyway, so resolution returns a name that has no
    driver — `driver_for` on that resolution raises `UnknownBackend`. Resolution
    would be better off degrading to the head of the chain instead."""
    monkeypatch.delitem(registry.BACKENDS, DEFAULT)

    outcome = roles.resolve(roles.ROLE_JUDGE, config={})
    assert outcome.backend == DEFAULT
    assert outcome.usable is True
    assert outcome.candidates == (DEFAULT, OTHER)
    with pytest.raises(registry.UnknownBackend):
        registry.driver_class(outcome.backend)


def test_an_unusable_resolution_always_says_which_backends_it_skipped(monkeypatch):
    """`reason`'s "no backend is registered" fallback is unreachable: the preferred
    backend is always a candidate, so a skipped list is never empty."""
    monkeypatch.setattr(registry, "BACKENDS", {})
    outcome = roles.resolve(roles.ROLE_JUDGE, config={}, available=[])
    assert outcome.usable is False
    assert outcome.candidates == (DEFAULT,)
    assert outcome.reason == f"{DEFAULT} unavailable"
