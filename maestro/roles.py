"""Role -> (backend, model) resolution and the fallback chain.

A *role* is a job the orchestrator hands to an agent — implementing a task, judging a
verification proof, diagnosing a failure. Which backend runs a role, and which model it
runs under, is configuration (`docs/DESIGN.md` §5), not a hardcoded call-site decision:

.. code-block:: yaml

    roles:
      implementer: {backend: <name>, models: {<name>: <model id>, ...}}
      judge:       {backend: <name>, models: {<name>: <model id>, ...}}

    fallback_chain: [<name>, <name>]

A role declares its model **per backend**, so a mid-work switch (§7) resolves the same
role under the new backend without a separate equivalence table.

**Resolution here is pure.** It runs no subprocess, probes no binary and reads no file
beyond the project configuration. Whether a backend's binary exists and whether its quota
is spent are *inputs* — the caller measures them (with
`maestro.backends.registry.backend_binary` and `maestro.quota`) and passes them in. That
is what makes every path testable without launching an agent, and what makes it
impossible for role resolution to spend a token.

Two deliberate omissions:

* **No backend name is written down in this module.** The default backend, the set of
  known names and the default chain all come from `maestro.backends.registry`, which is
  the one module allowed to know backends by name. Adding a driver there extends the
  fallback chain here for free.
* **No configuration loader.** `maestro.config.load_project_yaml` is called by name at
  use time (never bound at import), so the single existing loader — with its
  degrade-to-`{}` behaviour on every failure mode — stays the only one.

Everything degrades rather than raising, in the same spirit as `maestro.config`: a
malformed `roles:` block, an unknown backend name or a junk fallback chain falls back to
the documented defaults, because refusing to resolve a role would stop the orchestrator
over a typo in a config file it can survive without.

**Defaults reproduce today's behaviour exactly.** Every role defaults to
`registry.DEFAULT_BACKEND`, and the default models are the ones the current call sites
hardcode: the implementer's `_DEFAULT_IMPLEMENTER_MODEL`, the judge model in
`maestro.gates` / `maestro.merge`, and `maestro.selfheal.diagnose.JUDGE_MODEL`. Those
call sites are unchanged by M2; `tests/test_roles.py` pins the two tables together so
they cannot drift apart silently.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from maestro import config as _config
from maestro.backends import registry

__all__ = [
    "ROLE_IMPLEMENTER",
    "ROLE_JUDGE",
    "ROLE_DIAGNOSER",
    "KNOWN_ROLES",
    "DEFAULT_MODELS",
    "ROLES_KEY",
    "CHAIN_KEY",
    "RoleConfig",
    "Resolution",
    "normalise_role",
    "default_chain",
    "role_config",
    "resolve",
    "backend_for",
    "model_for",
    "fallback_backend",
    "driver_for",
]

#: The roles that exist today. Resolution is not restricted to them — an unknown role
#: resolves to the default backend with no model, which means "the backend's own
#: default" — but these are the ones with a documented default model.
ROLE_IMPLEMENTER = "implementer"
ROLE_JUDGE = "judge"
ROLE_DIAGNOSER = "diagnoser"

KNOWN_ROLES: tuple[str, ...] = (ROLE_IMPLEMENTER, ROLE_JUDGE, ROLE_DIAGNOSER)

#: role -> backend -> model id, used when configuration says nothing. Only the default
#: backend has entries: these are transcriptions of what the current call sites already
#: hardcode, and every one of them runs there. A backend with no entry resolves to
#: `None`, which means "let the backend choose its own default model" — inventing a
#: model id for a tool we have not verified would be worse than omitting the flag.
DEFAULT_MODELS: dict[str, dict[str, str]] = {
    ROLE_IMPLEMENTER: {registry.DEFAULT_BACKEND: "claude-sonnet-5"},
    ROLE_JUDGE: {registry.DEFAULT_BACKEND: "claude-sonnet-5"},
    ROLE_DIAGNOSER: {registry.DEFAULT_BACKEND: "claude-opus-5"},
}

#: Top-level project.yaml keys this module reads.
ROLES_KEY = "roles"
CHAIN_KEY = "fallback_chain"


def normalise_role(role: object) -> str:
    """Canonical form of a role name."""
    return str(role or "").strip().lower()


def _text(value: object) -> str:
    """A non-empty stripped string, or `""` for anything else."""
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _names(values: Optional[Iterable[object]]) -> frozenset:
    """A set of normalised backend names from any iterable of strings.

    A bare string is treated as a one-element collection rather than as a set of its
    characters, which is the mistake that would silently make every check pass.
    """
    if values is None:
        return frozenset()
    if isinstance(values, str):
        values = [values]
    return frozenset(
        registry.normalise_name(value) for value in values if isinstance(value, str)
    )


def _project(config: Optional[Mapping]) -> Mapping:
    """The project configuration.

    `None` means "load it" — delegated to `maestro.config`, looked up at call time so the
    single loader stays substitutable. Anything that is not a mapping (a malformed or
    empty document) reads as no configuration at all.
    """
    if config is None:
        config = _config.load_project_yaml()
    return config if isinstance(config, Mapping) else {}


def _role_entry(project: Mapping, role: str) -> Mapping:
    """The `roles:` entry for `role`, normalised to a mapping.

    A bare string entry (`implementer: <name>`) is read as a backend choice, because that
    is the only thing it could sensibly mean and rejecting it would be gratuitous.
    """
    roles = project.get(ROLES_KEY)
    if not isinstance(roles, Mapping):
        return {}
    entry = roles.get(role)
    if isinstance(entry, Mapping):
        return entry
    if _text(entry):
        return {"backend": entry}
    return {}


def default_chain() -> tuple[str, ...]:
    """Every known backend, the default one first, then the rest in name order.

    Derived from the registry, so a newly registered driver joins the chain without an
    edit here.
    """
    names = list(registry.known_backends())
    default = registry.normalise_name(registry.DEFAULT_BACKEND)
    head = [default] if default in names else []
    return tuple(head + [name for name in names if name != default])


def _clean_chain(raw: object) -> tuple[str, ...]:
    """A configured `fallback_chain`, minus duplicates and unregistered names.

    A name with no driver is dropped rather than honoured: it could never be launched,
    and leaving it in would make the chain look longer than it is.
    """
    if not isinstance(raw, (list, tuple)):
        return ()
    known = set(registry.known_backends())
    ordered: list[str] = []
    for item in raw:
        name = registry.normalise_name(item) if isinstance(item, str) else ""
        if name in known and name not in ordered:
            ordered.append(name)
    return tuple(ordered)


@dataclass(frozen=True)
class RoleConfig:
    """What configuration says about one role, with defaults already applied."""

    role: str
    backend: str
    models: Mapping[str, str]
    chain: tuple[str, ...]

    def model_for(self, backend: object) -> Optional[str]:
        """The model this role runs under on `backend`.

        `None` means "no model is specified anywhere" — the caller should let the backend
        pick its own default rather than guess one.
        """
        name = registry.normalise_name(backend)
        configured = _text(self.models.get(name))
        if configured:
            return configured
        return DEFAULT_MODELS.get(self.role, {}).get(name)


@dataclass(frozen=True)
class Resolution:
    """The outcome of resolving one role against the current backend conditions."""

    role: str
    backend: str
    model: Optional[str]
    preferred: str
    candidates: tuple[str, ...]
    usable: bool = True
    reason: str = ""

    @property
    def switched(self) -> bool:
        """True when the preferred backend was passed over for a fallback."""
        return self.backend != self.preferred


def role_config(role: object, config: Optional[Mapping] = None) -> RoleConfig:
    """Read one role's configuration, filling in defaults for everything absent.

    An unregistered backend name degrades to the default backend: it names a driver that
    does not exist, so honouring it could only fail later, further from the cause.
    """
    key = normalise_role(role)
    project = _project(config)
    entry = _role_entry(project, key)

    backend = registry.normalise_name(entry.get("backend"))
    if backend not in registry.known_backends():
        backend = registry.normalise_name(registry.DEFAULT_BACKEND)

    models: dict[str, str] = {}
    raw_models = entry.get("models")
    if isinstance(raw_models, Mapping):
        for name, model in raw_models.items():
            if _text(model):
                models[registry.normalise_name(name)] = _text(model)
    single = _text(entry.get("model"))
    if single:
        models.setdefault(backend, single)

    return RoleConfig(
        role=key,
        backend=backend,
        models=models,
        chain=_clean_chain(project.get(CHAIN_KEY)) or default_chain(),
    )


def _skip_reason(name: str, available: Optional[frozenset], exhausted: frozenset) -> str:
    """Why `name` cannot run right now, or `""` when it can.

    `available=None` means availability was not measured, which is read as "assume it is
    installed" — refusing to resolve because nobody looked would be worse than trying.
    """
    if name in exhausted:
        return "exhausted"
    if available is not None and name not in available:
        return "unavailable"
    return ""


def resolve(
    role: object,
    *,
    config: Optional[Mapping] = None,
    available: Optional[Iterable[object]] = None,
    exhausted: Iterable[object] = (),
    preferred: object = None,
) -> Resolution:
    """Resolve `role` to a backend and a model under the current conditions.

    `available` is the set of backends whose binary was found (`None` = not measured, so
    assume all), `exhausted` the set whose quota is spent, and `preferred` an explicit
    override — an operator's `/backend <name>`, or the backend a task is already running
    under. The preferred backend is tried first, then the rest of the fallback chain in
    order, which is what makes the chain work in both directions: whichever backend is
    preferred leads, and the others follow.

    When nothing in the chain can run, the preferred backend is still returned, with
    `usable=False`. Callers use that to keep their existing behaviour — waiting for quota
    rather than switching — instead of having to handle an exception on a path where
    there is nothing useful to do.

    An unregistered `preferred` name is ignored in favour of the configured backend;
    operator input should be validated against `registry.driver_ref` first, so that a
    typo is reported to the operator rather than silently absorbed here.
    """
    settings = role_config(role, config)

    head = registry.normalise_name(preferred) if preferred is not None else ""
    if head not in registry.known_backends():
        head = settings.backend
    candidates = (head,) + tuple(name for name in settings.chain if name != head)

    installed = _names(available) if available is not None else None
    spent = _names(exhausted)

    skipped: list[str] = []
    for name in candidates:
        why = _skip_reason(name, installed, spent)
        if why:
            skipped.append(f"{name} {why}")
            continue
        return Resolution(
            role=settings.role,
            backend=name,
            model=settings.model_for(name),
            preferred=head,
            candidates=candidates,
            usable=True,
            reason="; ".join(skipped),
        )

    return Resolution(
        role=settings.role,
        backend=head,
        model=settings.model_for(head),
        preferred=head,
        candidates=candidates,
        usable=False,
        reason="; ".join(skipped) or "no backend is registered",
    )


def backend_for(role: object, **kwargs) -> str:
    """The backend `role` should run on. Arguments are `resolve`'s."""
    return resolve(role, **kwargs).backend


def model_for(
    role: object,
    backend: object = None,
    *,
    config: Optional[Mapping] = None,
) -> Optional[str]:
    """The model `role` runs under on `backend` (default: its configured backend)."""
    settings = role_config(role, config)
    return settings.model_for(settings.backend if backend is None else backend)


def fallback_backend(
    role: object,
    current: object,
    *,
    config: Optional[Mapping] = None,
    available: Optional[Iterable[object]] = None,
    exhausted: Iterable[object] = (),
) -> Optional[str]:
    """The backend to hand `role` to instead of `current`, or `None` if there is none.

    This is the question the switch triggers ask: a threshold crossing only becomes a
    switch when somewhere else can actually take the work, otherwise the existing
    throttle-and-wait behaviour must stand unchanged.
    """
    now = registry.normalise_name(current)
    ruled_out = set(_names(exhausted)) | {now}
    outcome = resolve(
        role, config=config, available=available, exhausted=ruled_out, preferred=now
    )
    return outcome.backend if outcome.usable and outcome.backend != now else None


def driver_for(role: object, **kwargs) -> type:
    """The driver class for `role`, resolved through the registry.

    The only function here that is not pure — resolution picks a name, and the registry
    imports that driver's module on first use. It still executes no agent.
    """
    return registry.driver_class(resolve(role, **kwargs).backend)
