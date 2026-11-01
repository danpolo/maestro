"""Role -> (backend, model) resolution, the fallback chain, and the purity of both.

`maestro.roles` is the module that decides *which* agent runs a job and *under which
model*. Two properties matter enough to be pinned here rather than assumed:

* **It launches nothing.** Availability and quota are inputs, not measurements. An autouse
  fixture makes every spelling of "execute something" raise, and another replaces the
  project loader and moves the project root, so a resolution that shelled out or read the
  operator's project would fail the whole module rather than quietly work. What it does
  read is packaged with it: the third-party register and the role templates, which the
  derivation of a role's models needs (`docs/DESIGN.md` §17.2).
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
import io
import os
import re
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from maestro import config as _config
from maestro import gates, merge, model_policy, roles
from maestro.backends import catalog, registry
from maestro.backends.registry import DriverRef
from maestro.selfheal import diagnose

#: Derived, never assumed: the tests below must keep working when a driver is added.
DEFAULT = registry.normalise_name(registry.DEFAULT_BACKEND)
OTHER = next(name for name in roles.default_chain() if name != DEFAULT)
#: A real harness the default chain leaves out, so only `routing.harness_order` reaches it.
#: The shipped catalog has a route on it for the implementer and none for a strong role.
THIRD = next(name for name in registry.known_backends() if name not in roles.default_chain())
#: The reason a backend is passed over when the policy has no route for the role on it.
NO_ROUTE = "no eligible role model in adopted policy"


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
def loader(monkeypatch, tmp_path):
    """No test may read the operator's real `project.yaml`, nor the model policy adopted
    beside it: with no configuration supplied, resolution looks that up under the project
    root, so the root is moved to an empty directory and the shipped policy applies."""
    stub = _Loader()
    monkeypatch.setattr(_config, "load_project_yaml", stub)
    monkeypatch.setattr(_config, "PROJECT_YAML", tmp_path / "project.yaml")
    return stub


@pytest.fixture
def third_backend(monkeypatch):
    """Register a fictional third driver, so chain behaviour is not a coincidence.

    The name sorts last, which makes it the tail of the derived default chain. No catalog
    entry names it, so the policy has no route on it for any role.
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


def _harness_order(*names: str) -> dict:
    """Explicit derived fixture chain for fallback tests, independent of global ranking."""
    document = model_policy.derived_roles({}, None)
    chain = []
    for raw in names:
        name = registry.normalise_name(raw)
        if name in registry.known_backends() and name not in chain:
            chain.append(name)
    chain = chain or list(roles.default_chain())
    document[roles.DERIVED_CHAIN_KEY] = chain
    for entry in document['roles'].values():
        entry['backend'] = next((driver for driver in chain if driver in entry['models']), chain[0])
    return document


def _derived_with(role: str, **entry) -> dict:
    """A configuration `model_policy.derived_roles` has already been through: its marker
    is set, so `roles` reads the entry as it stands instead of deriving one."""
    return {roles.ROLES_KEY: {role: entry}, roles.DERIVED_MARKER: True}


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
        name
        for name in registry.known_backends()
        if name != DEFAULT and registry.BACKENDS[name].in_default_chain
    )


def test_a_backend_opted_out_of_the_default_chain_is_reachable_only_explicitly():
    """A backend graded below the others must not absorb work on quota pressure."""
    opted_out = [name for name, ref in registry.BACKENDS.items() if not ref.in_default_chain]
    assert opted_out, "the shipped registry keeps at least one backend out of the chain"
    for name in opted_out:
        assert name not in roles.default_chain()
        assert roles._clean_chain([name]) == (name,)


def test_default_chain_grows_when_a_driver_is_registered(third_backend):
    assert roles.default_chain() == (DEFAULT, OTHER, "zebra")


def test_default_chain_omits_an_unregistered_default(monkeypatch):
    monkeypatch.delitem(registry.BACKENDS, DEFAULT)
    assert roles.default_chain() == (OTHER,)


# ── role_config ──


def test_role_config_defaults_everything_when_configuration_is_silent():
    settings = roles.role_config(roles.ROLE_IMPLEMENTER, config={})
    assert settings.role == roles.ROLE_IMPLEMENTER
    assert settings.backend == OTHER  # the neutral implementer/pool hash, not registry priority
    assert settings.chain == roles.default_chain()
    # One derived model per backend of the chain, and only the derived table counts.
    assert set(settings.chain) <= set(settings.models)
    assert settings.strict_models is True
    assert settings.invalid_models == {}


def test_role_config_derives_the_backend_from_the_harness_order():
    """The first harness in the order that has a model; the per-backend table is the
    same whichever harness leads."""
    settings = roles.role_config(roles.ROLE_JUDGE, config=_harness_order(OTHER, DEFAULT))
    assert settings.backend == OTHER
    assert settings.models == roles.role_config(roles.ROLE_JUDGE, config={}).models
    assert {DEFAULT, OTHER} <= set(settings.models)


def test_role_config_reads_an_already_derived_entry_as_it_stands():
    """What `model_policy.runtime_roles` hands over is not derived a second time."""
    document = _derived_with(
        roles.ROLE_JUDGE, backend=OTHER, models={DEFAULT: "model-a", OTHER: "model-b"}
    )
    document[roles.DERIVED_CHAIN_KEY] = [OTHER, DEFAULT]
    settings = roles.role_config(roles.ROLE_JUDGE, config=document)
    assert settings.backend == OTHER
    assert settings.models == {DEFAULT: "model-a", OTHER: "model-b"}
    assert settings.chain == (OTHER, DEFAULT)


def test_role_config_normalises_the_role_key_and_the_model_backend_names():
    settings = roles.role_config(
        "  JUDGE ",
        config=_derived_with("judge", models={OTHER.upper(): "  model-b  "}),
    )
    assert settings.role == "judge"
    assert settings.models == {OTHER: "model-b"}


def test_role_config_degrades_an_unregistered_backend_to_the_default():
    settings = roles.role_config(
        roles.ROLE_JUDGE, config=_derived_with(roles.ROLE_JUDGE, backend="gpt-9000")
    )
    assert settings.backend == DEFAULT


@pytest.mark.parametrize("configured", ["reviewer", roles.ROLE_JUDGE])
def test_the_legacy_judge_reads_what_configuration_says_of_the_reviewer(configured):
    """Config names are the graph's role ids; the legacy loop still asks for its `judge`."""
    document = _config_with(configured, strength="light")
    settings = roles.role_config(roles.ROLE_JUDGE, config=document)
    assert settings.models == roles.role_config("reviewer", config=document).models
    # A project may raise the numerical floor, but cannot lower the policy's 48.
    assert settings.models == roles.role_config(roles.ROLE_JUDGE, config={}).models


#: Every model the shipped catalog serves for an explicit pin only, as (backend, model).
_PIN_ONLY = [
    (registry.driver_name_for_backend_id(backend_id), model)
    for backend_id, models in sorted(catalog.PIN_ONLY_MODELS.items())
    for model in sorted(models)
]


@pytest.mark.parametrize(
    "document",
    [
        {roles.ROLES_KEY: {roles.ROLE_JUDGE: OTHER}},
        _config_with(roles.ROLE_JUDGE, backend=OTHER),
        _config_with(roles.ROLE_JUDGE, backend=OTHER, model="model-b"),
        _config_with(roles.ROLE_JUDGE, backend="gpt-9000", model="model-x"),
        _config_with(roles.ROLE_JUDGE, models={DEFAULT: "model-a", OTHER: "model-b"}),
        _config_with(
            roles.ROLE_JUDGE, backend=OTHER, model="singular", models={OTHER: "tabulated"}
        ),
        _config_with(roles.ROLE_JUDGE, effort="high"),
        {roles.CHAIN_KEY: [OTHER, DEFAULT]},
        {roles.CHAIN_KEY: [OTHER]},
        # Historical native ids remain present for frozen schema 1 records. They
        # have no schema 2 pair, so obsolete role keys cannot readmit one.
        *(
            pytest.param(
                _config_with(roles.ROLE_IMPLEMENTER, models={backend: model}),
            )
            for backend, model in _PIN_ONLY
        ),
    ],
)
def test_the_retired_role_keys_and_the_fallback_chain_are_ignored(document):
    """§17.2: a role states only how strong its model must be. A bare-string entry,
    `backend`, `model`, `models` and `effort` once chose a backend or a model, and
    top-level `fallback_chain` the order; none of them changes a resolution any more."""
    for role in roles.KNOWN_ROLES:
        assert roles.role_config(role, config=document) == roles.role_config(role, config={})


MALFORMED_ROLES = [
    {},
    {roles.ROLES_KEY: None},
    {roles.ROLES_KEY: "not a mapping"},
    {roles.ROLES_KEY: []},
    {roles.ROLES_KEY: {roles.ROLE_JUDGE: None}},
    {roles.ROLES_KEY: {roles.ROLE_JUDGE: 17}},
    {roles.ROLES_KEY: {roles.ROLE_JUDGE: "   "}},
    {roles.ROLES_KEY: {roles.ROLE_JUDGE: {"backend": None, "models": "junk"}}},
    {roles.ROLES_KEY: {roles.ROLE_JUDGE: {"models": {DEFAULT: "   "}}}},
    {roles.ROLES_KEY: {roles.ROLE_JUDGE: {"strength": None}}},
    {roles.ROLES_KEY: {roles.ROLE_JUDGE: {"strength": "bogus"}}},
]


@pytest.mark.parametrize("document", MALFORMED_ROLES)
def test_a_malformed_roles_block_degrades_to_the_defaults(document):
    """It reads as no block at all, so the role runs at its shipped strength."""
    settings = roles.role_config(roles.ROLE_JUDGE, config=document)
    assert settings.backend == DEFAULT
    assert settings.models == roles.role_config(roles.ROLE_JUDGE, config={}).models
    assert settings.chain == roles.default_chain()


@pytest.mark.parametrize("document", MALFORMED_ROLES)
def test_a_malformed_roles_block_degrades_on_the_loaded_path_too(document, loader):
    """The path production takes: no `config=`, so the loaded project is derived."""
    loader.result = document
    settings = roles.role_config(roles.ROLE_JUDGE)
    assert settings.backend == DEFAULT
    assert settings.models == roles.role_config(roles.ROLE_JUDGE, config={}).models
    assert settings.chain == roles.default_chain()


@pytest.mark.parametrize("loaded", [True, False])
def test_a_malformed_role_entry_does_not_take_a_valid_sibling_with_it(loaded, loader):
    """The graph router keeps the strength a well-formed entry states
    (`configured_role_strengths`); both paths of the legacy resolver keep it too."""
    document = {roles.ROLES_KEY: {roles.ROLE_IMPLEMENTER: {"strength": "strong"},
                                  roles.ROLE_JUDGE: 17}}
    loader.result = document
    settings = roles.role_config(roles.ROLE_IMPLEMENTER, config=None if loaded else document)
    strong = roles.role_config(
        roles.ROLE_IMPLEMENTER, config={roles.ROLES_KEY: {roles.ROLE_IMPLEMENTER: {"strength": "strong"}}})
    assert settings.models == strong.models
    assert settings.models == roles.role_config(roles.ROLE_JUDGE, config={}).models


@pytest.mark.parametrize("document", [None, [], "junk", 42, {"roles": {}}])
def test_a_non_mapping_document_reads_as_no_configuration(document, loader):
    loader.result = document
    assert roles.role_config(roles.ROLE_JUDGE).backend == DEFAULT


def test_role_keys_are_matched_case_sensitively_against_configuration():
    """CURRENT BEHAVIOUR. The requested role is lowercased; the configured key is not,
    so a capitalised `Judge:` in project.yaml is silently not found."""
    shipped = roles.role_config("implementer", config={})
    document = {roles.ROLES_KEY: {"Implementer": {"strength": "strong"}}}
    assert roles.role_config("Implementer", config=document) == shipped
    assert roles.role_config("implementer", config=document) == shipped
    assert roles.role_config("implementer", config=_config_with("implementer", strength="strong")) != shipped


# ── the fallback chain as configured: `routing.harness_order` ──


def test_a_configured_chain_replaces_the_default_one():
    settings = roles.role_config(roles.ROLE_JUDGE, config=_harness_order(OTHER, DEFAULT))
    assert settings.chain == (OTHER, DEFAULT)


def test_a_configured_chain_drops_duplicates_and_keeps_first_position():
    settings = roles.role_config(
        roles.ROLE_JUDGE, config=_harness_order(OTHER, OTHER, DEFAULT, OTHER)
    )
    assert settings.chain == (OTHER, DEFAULT)


def test_a_configured_chain_drops_a_backend_that_has_no_driver():
    settings = roles.role_config(roles.ROLE_JUDGE, config=_harness_order("gpt-9000", OTHER))
    assert settings.chain == (OTHER,)


def test_a_chain_naming_only_unknown_backends_falls_back_to_the_default_chain():
    settings = roles.role_config(roles.ROLE_JUDGE, config=_harness_order("gpt-9000", "nope"))
    assert settings.chain == roles.default_chain()


@pytest.mark.parametrize(
    "raw", [None, "codex", {}, 7, [], [None, 3], [{"backend": "codex"}]]
)
def test_a_malformed_chain_falls_back_to_the_default_chain(raw):
    settings = roles.role_config(
        roles.ROLE_JUDGE, config={"routing": {"harness_order": raw}}
    )
    assert settings.chain == roles.default_chain()


@pytest.mark.parametrize("routing", [None, "codex", 7, [], [OTHER, DEFAULT]])
def test_a_malformed_routing_block_falls_back_to_the_default_chain(routing):
    settings = roles.role_config(roles.ROLE_JUDGE, config={"routing": routing})
    assert settings.chain == roles.default_chain()


def test_chain_names_are_normalised():
    settings = roles.role_config(
        roles.ROLE_JUDGE, config=_harness_order(f"  {OTHER.upper()}  ", DEFAULT)
    )
    assert settings.chain == (OTHER, DEFAULT)


# ── models ──


def test_every_known_role_has_a_default_model_on_the_default_backend():
    for role in roles.KNOWN_ROLES:
        assert roles.model_for(role, config={})


def test_a_role_with_no_route_on_a_backend_resolves_to_no_model(third_backend):
    """`None`, not a guess: the policy has no route there, and inventing a model id for a
    backend nobody approved would be worse."""
    assert roles.model_for(roles.ROLE_IMPLEMENTER, "zebra", config={}) is None


def test_an_unknown_role_has_no_default_model():
    assert roles.model_for("archivist", config={}) is None


def test_a_configured_strength_overrides_the_shipped_one():
    """A strong implementer runs under what the shipped strong roles run under."""
    document = _config_with(roles.ROLE_IMPLEMENTER, strength="strong")
    assert (
        roles.role_config(roles.ROLE_IMPLEMENTER, config=document).models
        == roles.role_config(roles.ROLE_JUDGE, config={}).models
    )
    # Opus low and medium have different classes but the same model id. The pair
    # eligibility tests check the actual effort and grade at each floor.


def test_a_harness_with_no_route_at_the_roles_strength_is_passed_over():
    """`strength` is a floor, so the harness order is only followed as far as a route."""
    document = _harness_order(THIRD, OTHER)
    assert roles.backend_for(roles.ROLE_IMPLEMENTER, config=document) == OTHER
    strong = roles.role_config(roles.ROLE_JUDGE, config=document)
    assert THIRD not in strong.models
    assert strong.backend == OTHER


def test_model_for_defaults_to_the_roles_own_backend():
    document = _harness_order(OTHER, DEFAULT)
    models = roles.role_config(roles.ROLE_JUDGE, config=document).models
    assert models[OTHER] != models[DEFAULT]
    assert roles.model_for(roles.ROLE_JUDGE, config=document) == models[OTHER]
    assert roles.model_for(roles.ROLE_JUDGE, DEFAULT, config=document) == models[DEFAULT]


def test_model_for_normalises_the_backend_name():
    named = roles.model_for(roles.ROLE_JUDGE, f"  {OTHER.upper()} ", config={})
    assert named and named == roles.model_for(roles.ROLE_JUDGE, OTHER, config={})


def test_model_for_an_unregistered_backend_is_none():
    assert roles.model_for(roles.ROLE_JUDGE, "gpt-9000", config={}) is None


def test_default_models_reproduce_the_call_sites_they_replace():
    """The two tables must not drift apart silently (`roles` module docstring)."""
    from maestro import implementer

    assert roles.model_for(roles.ROLE_IMPLEMENTER, config={}) == 'gpt-6.1-sol'
    assert roles.model_for(roles.ROLE_DIAGNOSER, config={}) == 'gpt-6.1-sol'


def test_default_models_are_derived_from_limits_canonical_names_not_a_second_literal():
    """C7 part 1: `DEFAULT_MODELS`'s slugs must not be a second hand-typed copy of the
    model id — they are the project.yaml-slug fold (`maestro.limits.model_slug`, pure
    string work, no IO) of `maestro.limits`'s own canonical display-name constants.
    Renaming one of those constants is then the *only* edit `_SHIPPED_DEFAULTS` and
    `roles.DEFAULT_MODELS` both need, instead of a literal in each."""
    from maestro import limits as _limits

    assert roles.DEFAULT_MODELS[roles.ROLE_IMPLEMENTER][DEFAULT] == _limits.model_slug(
        _limits.DEFAULT_IMPLEMENTER_MODEL_NAME
    )
    assert roles.DEFAULT_MODELS[roles.ROLE_JUDGE][DEFAULT] == _limits.model_slug(
        _limits.DEFAULT_JUDGE_MODEL_NAME
    )
    assert roles.DEFAULT_MODELS[roles.ROLE_DIAGNOSER][DEFAULT] == _limits.model_slug(
        _limits.DEFAULT_DIAGNOSER_MODEL_NAME
    )


def test_roles_module_holds_no_model_id_string_literal_of_its_own():
    """The inverse view of the same fix, read straight from source: no bare
    `"claude-...-N"`-shaped string literal appears anywhere in `roles.py` any more — the
    two model ids it resolves to by default are both computed, not typed."""
    source = Path(roles.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    offenders = [s for s in literals if re.match(r"^claude-[a-z0-9-]+-\d", s)]
    assert not offenders, f"roles.py still hardcodes a model id literal: {offenders}"


#: The package root, for the source scan below.
_PACKAGE_DIR = Path(roles.__file__).resolve().parent


def test_no_call_site_pins_a_model_literal_when_it_asks_a_role():
    """The inverse of what this used to assert, and the point of the whole exercise.

    Until 2026-08-30 this test checked that `maestro.gates` and `maestro.merge` still
    *hardcoded* the judge model, because they did and the table here only had to agree
    with them. Now every bounded call goes through `maestro.agentcall.ask`, which resolves
    the model from the role — so a `model=` literal at a call site is a regression: it
    would hand one backend's model id to whichever backend actually answered, which is the
    failure the routing work existed to remove.

    A `model=` argument is still legitimate when it carries a value chosen at runtime
    (`/redo` and self-fix pin a model per task), so what is refused is specifically a
    *string constant*, not the keyword.
    """
    offenders = []
    for path in sorted(_PACKAGE_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            if name not in ("ask", "_judge_complete"):
                continue
            for keyword in node.keywords:
                value = keyword.value
                if (keyword.arg == "model" and isinstance(value, ast.Constant)
                        and isinstance(value.value, str) and value.value):
                    offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "these calls pin a model id instead of letting the role resolve one: "
        + ", ".join(offenders)
        + " — name the role and let `project.yaml`'s per-backend table choose the model"
    )


# ── resolve: the happy path ──


def test_resolve_returns_the_configured_backend_and_model():
    outcome = roles.resolve(roles.ROLE_JUDGE, config=_harness_order(OTHER, DEFAULT))
    assert (outcome.role, outcome.backend, outcome.model) == (
        roles.ROLE_JUDGE,
        OTHER,
        roles.model_for(roles.ROLE_JUDGE, OTHER, config={}),
    )
    assert outcome.model
    assert outcome.usable is True
    assert outcome.reason == ""
    assert outcome.switched is False


def test_resolve_of_an_unknown_role_is_unusable_on_the_default_backend():
    """No template and no `roles:` entry means no strength, so no route on any backend."""
    outcome = roles.resolve("archivist", config={})
    assert (outcome.backend, outcome.model, outcome.usable) == (DEFAULT, None, False)
    assert outcome.reason == "; ".join(
        f"{name} {NO_ROUTE}" for name in roles.default_chain()
    )


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


def test_the_chain_is_walked_past_more_than_one_dead_backend():
    outcome = roles.resolve(
        roles.ROLE_IMPLEMENTER,
        config=_harness_order(DEFAULT, OTHER, THIRD),
        available=[OTHER, THIRD],
        exhausted=[OTHER],
    )
    assert not outcome.usable
    assert outcome.reason == f"{DEFAULT} unavailable; {OTHER} exhausted; {THIRD} {NO_ROUTE}"


def test_a_registered_backend_with_no_route_for_the_role_is_never_resolved_to(third_backend):
    """Having a driver is not enough: a role runs only where the policy has a route for
    it, whether the backend is reached by preference or by walking the chain."""
    assert "zebra" in roles.default_chain()

    outcome = roles.resolve(roles.ROLE_JUDGE, config={}, preferred="zebra")
    assert outcome.backend == DEFAULT
    assert outcome.switched is True
    assert outcome.reason == f"zebra {NO_ROUTE}"

    outcome = roles.resolve(roles.ROLE_JUDGE, config={}, exhausted=[DEFAULT, OTHER])
    assert outcome.usable is False
    assert outcome.reason == f"{DEFAULT} exhausted; {OTHER} exhausted; zebra {NO_ROUTE}"


def test_a_fallback_resolves_the_same_role_under_the_new_backends_model():
    """The point of per-backend models: no separate equivalence table for a switch."""
    models = roles.role_config(roles.ROLE_IMPLEMENTER, config={}).models
    assert models[DEFAULT] != models[OTHER]
    assert roles.resolve(roles.ROLE_IMPLEMENTER, config={}, preferred=DEFAULT).model == models[DEFAULT]
    assert (
        roles.resolve(roles.ROLE_IMPLEMENTER, config={}, exhausted=[DEFAULT]).model
        == models[OTHER]
    )


def test_a_fallback_backends_model_is_derived_not_read_from_its_default_column(monkeypatch):
    """`DEFAULT_MODELS` no longer decides a role's model (`roles` module docstring)."""
    derived = roles.role_config(roles.ROLE_IMPLEMENTER, config={}).models[OTHER]
    monkeypatch.delitem(roles.DEFAULT_MODELS[roles.ROLE_IMPLEMENTER], OTHER)
    outcome = roles.resolve(roles.ROLE_IMPLEMENTER, config={}, exhausted=[DEFAULT])
    assert (outcome.backend, outcome.model) == (OTHER, derived)


def test_a_configured_chain_orders_the_fallback():
    """The default chain would hand the work to OTHER; the configured order says THIRD."""
    outcome = roles.resolve(
        roles.ROLE_IMPLEMENTER,
        config=_harness_order(DEFAULT, THIRD, OTHER),
        exhausted=[DEFAULT],
    )
    assert outcome.candidates == (DEFAULT, THIRD, OTHER)
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
    for name in roles.default_chain():
        assert f"{name} exhausted" in outcome.reason


def test_nothing_installed_returns_the_preferred_one_as_unusable():
    outcome = roles.resolve(roles.ROLE_JUDGE, config={}, available=[])
    assert outcome.usable is False
    assert all(
        f"{name} unavailable" in outcome.reason for name in roles.default_chain()
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
        config=_harness_order(OTHER, DEFAULT),
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
    document = _harness_order(OTHER, DEFAULT)
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


def test_fallback_backend_walks_past_a_dead_alternative():
    assert (
        roles.fallback_backend(
            roles.ROLE_IMPLEMENTER,
            DEFAULT,
            config=_harness_order(DEFAULT, OTHER, THIRD),
            exhausted=[OTHER],
        )
        is None
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


def test_fallback_backend_honours_a_configured_chain_order():
    assert (
        roles.fallback_backend(
            roles.ROLE_IMPLEMENTER, DEFAULT, config=_harness_order(THIRD, OTHER)
        )
        == OTHER
    )
    assert (
        roles.fallback_backend(
            roles.ROLE_IMPLEMENTER, DEFAULT, config=_harness_order(OTHER, THIRD)
        )
        == OTHER
    )


def test_fallback_backend_is_none_when_it_is_the_last_backend_standing():
    assert (
        roles.fallback_backend(roles.ROLE_JUDGE, DEFAULT, config=_harness_order(DEFAULT))
        is None
    )


# ── driver_for ──


def test_driver_for_returns_the_registered_driver_class():
    assert roles.driver_for(roles.ROLE_JUDGE, config={}) is registry.driver_class(DEFAULT)


def test_driver_for_follows_the_fallback_chain():
    driver = roles.driver_for(roles.ROLE_JUDGE, config={}, exhausted=[DEFAULT])
    assert driver is registry.driver_class(OTHER)


def test_driver_for_resolves_through_the_registry_and_launches_nothing(monkeypatch):
    """The class is whatever the registry's import yields for the resolved backend."""
    ref = registry.driver_ref(OTHER)

    class StandIn:
        name = OTHER

    module = ModuleType(ref.module)
    setattr(module, ref.attr, StandIn)
    monkeypatch.setattr(registry, "import_module", lambda _name: module)

    driver = roles.driver_for(roles.ROLE_JUDGE, config={}, exhausted=[DEFAULT])
    assert driver is StandIn


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
    assert roles.resolve(roles.ROLE_IMPLEMENTER, config={}).backend == OTHER
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
    loader.result = _config_with('implementer', strength='strong')
    assert roles.model_for(roles.ROLE_IMPLEMENTER, DEFAULT) == 'claude-opus-5-5'
    loader.result = {}
    assert roles.model_for(roles.ROLE_IMPLEMENTER, DEFAULT) == 'claude-sonnet-5-5'


def _opened_outside_the_package(monkeypatch) -> list:
    """Start recording every file opened from here on that is not packaged with maestro.

    `Path.read_text` and `Path.read_bytes` go through `io.open`, a plain `open()` through
    `builtins.open`, so both names are wrapped. It records rather than raises, because
    `maestro.config` swallows whatever a read raises.
    """
    real_open = builtins.open
    seen: list = []

    def recording(file, *args, **kwargs):
        if not isinstance(file, int):
            path = Path(os.fsdecode(file)).resolve()
            if _PACKAGE_DIR not in path.parents:
                seen.append(path)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", recording)
    monkeypatch.setattr(io, "open", recording)
    return seen


def test_resolution_opens_nothing_outside_the_package_when_configuration_is_supplied(
    monkeypatch,
):
    """The derivation reads the packaged register and role templates, and nothing else:
    no project.yaml, no global configuration, and the adopted policy is not looked up —
    supplied configuration is derived under the shipped one."""

    def forbidden(*args, **kwargs):
        raise AssertionError(f"role resolution looked up the adopted policy: {args!r}")

    monkeypatch.setattr(model_policy, "adopted_policy", forbidden)
    seen = _opened_outside_the_package(monkeypatch)
    outcome = roles.resolve(roles.ROLE_JUDGE, config=_harness_order(OTHER, DEFAULT))
    assert outcome.backend == OTHER
    assert seen == []


def test_the_only_files_read_outside_the_package_are_the_ones_maestro_config_owns(
    tmp_path, monkeypatch
):
    """End to end through the real loader, pointed at a sandboxed project.yaml. The loader
    reads that file and looks for the global configuration; resolution adds no third."""
    document = tmp_path / "project.yaml"
    document.write_text(
        f"roles:\n"
        f"  implementer:\n"
        f"    strength: strong\n"
        f"routing:\n"
        f"  harness_order: [{OTHER}, {DEFAULT}]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(_config, "load_project_yaml", _config._load_project_yaml)
    monkeypatch.setattr(_config, "PROJECT_YAML", document)
    strong = roles.role_config(roles.ROLE_JUDGE, config={}).models

    seen = _opened_outside_the_package(monkeypatch)
    settings = roles.role_config(roles.ROLE_IMPLEMENTER)
    assert settings.backend == OTHER
    assert settings.models == strong
    assert settings.chain == roles.default_chain()
    assert set(seen) == {document.resolve(), _config.global_config_path()}


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


@pytest.mark.parametrize("missing", [(DEFAULT,), (OTHER,), registry.known_backends()])
def test_a_registry_missing_a_shipped_driver_refuses_resolution(missing, monkeypatch):
    """CURRENT BEHAVIOUR. The shipped policy has routes on every shipped backend and is
    validated against the registry before any role is derived, so a registry that lost
    one of those drivers no longer resolves to a name with no driver: it raises. That is
    not the degradation a malformed `roles:` block gets, and it is not caught."""
    for name in missing:
        monkeypatch.delitem(registry.BACKENDS, name)
    with pytest.raises(model_policy.PolicyError):
        roles.resolve(roles.ROLE_JUDGE, config={})


def test_an_unusable_resolution_always_says_which_backends_it_skipped():
    """`reason`'s "no backend is registered" fallback is unreachable: the preferred
    backend is always a candidate, so a skipped list is never empty."""
    outcome = roles.resolve(roles.ROLE_JUDGE, config=_harness_order(DEFAULT), available=[])
    assert outcome.usable is False
    assert outcome.candidates == (DEFAULT,)
    assert outcome.reason == f"{DEFAULT} unavailable"
