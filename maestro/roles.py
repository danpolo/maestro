"""Role -> (backend, model) resolution and the fallback chain.

A *role* is a job the orchestrator hands to an agent — implementing a task, judging a
verification proof, diagnosing a failure. Configuration states only how strong a role's
model must be (`docs/DESIGN.md` §17.2), under the graph's role ids; the legacy `judge`
reads `reviewer`:

.. code-block:: yaml

    roles:
      implementer: {strength: balanced}
      reviewer:    {strength: strong, independent_of: [implementer]}

Which backend runs a role and under which model is **derived**
(`maestro.model_policy.derived_roles`): on each backend the cheapest approved route at or
above the role's strength, with backend selection by the neutral pool hash for that role.
The derived table still holds one model **per backend**, so a mid-work switch (§7)
resolves the same role under the new backend without a separate equivalence table. A role
no longer states `backend`, `model`, `models` or `effort`, and `fallback_chain` is gone;
none of them is read here.

**Resolution here launches nothing.** It runs no subprocess and probes no binary. Beyond
the project configuration it reads the adopted model policy and two packaged files the
derivation needs: the third-party register and the role templates. Whether a backend's
binary exists and whether its quota is spent are *inputs* — the caller measures them
(with `maestro.backends.registry.backend_binary` and `maestro.quota`) and passes them in.
That is what makes every path testable without launching an agent, and what makes it
impossible for role resolution to spend a token.

Two deliberate omissions:

* **No backend name is written down in this module.** The default backend, the set of
  known names and the default chain all come from `maestro.backends.registry`, which is
  the one module allowed to know backends by name. Adding a driver there extends the
  fallback chain here for free.
* **No configuration loader.** `maestro.config.load_project_yaml` is called by name at
  use time (never bound at import), so the single existing loader — with its
  degrade-to-`{}` behaviour on every failure mode — stays the only one.

A malformed `roles:` block degrades rather than raising, in the same spirit as
`maestro.config`: only the strengths it states validly are kept, and every other role
runs at its shipped strength. That holds whether the caller passed a configuration or it
was loaded. Refusing to resolve a role would stop the orchestrator over a typo in a config file it
can survive without; `doctor` is where the typo is reported.

**`DEFAULT_MODELS` no longer decides a role's model.** It is what
`model_policy.shipped_policy()` builds the schema 1 `roles` table from, and the
implementer's last resort for a backend the derivation gave no model.

**C7 (2026-08-31): `DEFAULT_MODELS`'s slugs are derived, not typed here.** They come from
`maestro.limits.DEFAULT_IMPLEMENTER_MODEL_NAME` et al. — the module that owns the two
`model_context_limits.md` tables — via `limits.model_slug`, which only casefolds and
collapses separators. That import adds no exception to "reads no file beyond the project
configuration": `model_slug` touches no file, and the constants it slugifies are Python
string literals, not table rows read at call time. It removes the second hand-typed copy
of the same model id that used to live in this dict.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from maestro import config as _config
from maestro import limits as _limits
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

#: The legacy loop's roles. A role the derivation does not know has no model on any
#: backend, so it resolves to the default backend with `usable=False`.
ROLE_IMPLEMENTER = "implementer"
ROLE_JUDGE = "judge"
ROLE_DIAGNOSER = "diagnoser"

KNOWN_ROLES: tuple[str, ...] = (ROLE_IMPLEMENTER, ROLE_JUDGE, ROLE_DIAGNOSER)

#: role -> backend -> model id, used when configuration says nothing. Only the default
#: backend has entries: these are transcriptions of what the current call sites already
#: hardcode, and every one of them runs there. A backend with no entry resolves to
#: `None`, which means "let the backend choose its own default model" — inventing a
#: model id for a tool we have not verified would be worse than omitting the flag.
#:
#: The slugs themselves are **derived**, not typed here a second time: `maestro.limits`
#: is the module that owns the two `model_context_limits.md` tables (DESIGN.md's C7,
#: "the tables are the single source of truth"), so its
#: `DEFAULT_IMPLEMENTER_MODEL_NAME`/`DEFAULT_JUDGE_MODEL_NAME`/`DEFAULT_DIAGNOSER_MODEL_NAME`
#: constants — spelled as the tables spell them — are the one place this decision is
#: written down. `limits.model_slug` is pure string folding (casefold + separator
#: collapsing, no file IO, no subprocess), so calling it here at import time discloses
#: nothing this module's own purity claim above rules out.
DEFAULT_MODELS: dict[str, dict[str, str]] = {
    ROLE_IMPLEMENTER: {
        registry.DEFAULT_BACKEND: _limits.model_slug(_limits.DEFAULT_IMPLEMENTER_MODEL_NAME),
    },
    ROLE_JUDGE: {
        registry.DEFAULT_BACKEND: _limits.model_slug(_limits.DEFAULT_JUDGE_MODEL_NAME),
    },
    ROLE_DIAGNOSER: {
        registry.DEFAULT_BACKEND: _limits.model_slug(_limits.DEFAULT_DIAGNOSER_MODEL_NAME),
    },
}


def _verified_model_id(backend: str, name: str) -> str:
    """The id the shipped catalog verifies on `backend` for a table display name.

    `model_slug` folds `.` to `-` for comparison, but a Codex id keeps its dot: "GPT-6.1
    Sol" is `gpt-6.1-sol`, never `gpt-6-1-sol`. The catalog is inert data (no IO), so this
    import adds nothing to this module's purity claim. A backend without a catalog row
    keeps the slug."""
    from maestro.backends import catalog

    backend_id = registry.driver_ref(backend).backend_id
    slug = _limits.model_slug(name)
    for base in catalog.SHIPPED_CATALOG_DEFAULTS.values():
        if base["backend_id"] == backend_id:
            for model in base["available_models"]:
                if _limits.model_slug(model) == slug:
                    return model
    return slug


# Other backends' columns (2026-09-30: inheriting projects keep the table `maestro init`
# used to pin), from the same owner of default names.
for _backend, _names in _limits.DEFAULT_OTHER_BACKEND_MODEL_NAMES.items():
    for _role, _name in _names.items():
        DEFAULT_MODELS[_role][registry.normalise_name(_backend)] = _verified_model_id(_backend, _name)
del _backend, _names, _role, _name

#: Top-level project.yaml keys. `fallback_chain` is retired (DESIGN.md §17.2): nothing
#: here reads it, and `doctor` fails on it.
ROLES_KEY = "roles"
CHAIN_KEY = "fallback_chain"
#: What `model_policy.derived_roles` adds: its marker and the harness order as drivers.
DERIVED_MARKER = "_model_policy_roles"
DERIVED_CHAIN_KEY = "_harness_chain"


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
        from maestro.model_policy import runtime_roles
        repo = _config.PROJECT_YAML.parent
        config = _derive_leniently(config if isinstance(config, Mapping) else {},
                                   lambda project: runtime_roles(project, repo))
    return config if isinstance(config, Mapping) else {}


def _derive_leniently(project: Mapping, derive) -> Mapping:
    """`derive(project)`. A `roles:` block the derivation refuses keeps only the strengths
    it states validly, which is what the graph router reads of it
    (`model_policy.configured_role_strengths`); the rest reads as absent."""
    from maestro.model_policy import PolicyError, configured_role_strengths

    try:
        return derive(project)
    except (PolicyError, ValueError, TypeError, AttributeError):
        # `catalog_defaults` still parses the old role shape until slice 3 moves its pin
        # block, and a junk value there raises whatever `dict()` raises.
        stated = {role: {"strength": strength}
                  for role, strength in configured_role_strengths(project).items()}
        return derive({**project, ROLES_KEY: stated})


def _derived(project: Mapping) -> Mapping:
    """`project` with its roles derived (`model_policy.derived_roles`).

    `runtime_roles` already derived them when it loaded the project. A caller that passed
    its own configuration gets them derived here, under the shipped policy.
    """
    if project.get(DERIVED_MARKER) is True:
        return project
    from maestro.model_policy import derived_roles

    return _derive_leniently(project, lambda document: derived_roles(document, None))


def _role_entry(project: Mapping, role: str) -> Mapping:
    """The derived entry for `role`: `{strength, backend, models}`, or `{}` for a role the
    derivation does not know."""
    roles = project.get(ROLES_KEY)
    entry = roles.get(role) if isinstance(roles, Mapping) else None
    return entry if isinstance(entry, Mapping) else {}


def effort_controls(role: object, backend: str, model: str) -> tuple:
    """The chosen pair's register-verified delivery, including a manual model pin."""
    _, args, config = launch_controls(role, backend, model)
    return args, config


def launch_controls(role: object, backend: str, model: str) -> tuple:
    """The exact launch model and effort controls for one legacy choice."""
    if not model:
        return model, (), ()
    project = _derived(_project(None))
    key = normalise_role(role)
    entry = _role_entry(project, key)
    if entry.get("models", {}).get(backend) == model:
        args, config = entry.get("effort_controls", {}).get(backend, ((), ()))
        return model, args, config
    from maestro.model_policy import runtime_model_inputs, legacy_binding, check_task_pin
    from maestro.backends.router import AgentBinding, dispatch_plan
    _, policy, raw = runtime_model_inputs(_config.load_project_yaml(), _config.PROJECT_YAML.parent)
    binding = legacy_binding(raw, policy, key, driver=backend, model=model)
    if isinstance(binding, AgentBinding):
        plan = dispatch_plan(binding)
        return plan.model, plan.effort_args, plan.effort_config
    # Bootstrap historically accepts literal manual pins outside the shipped inventory.
    if policy is None:
        return model, (), ()
    # A model-only manual override has always been allowed outside automatic
    # grades. Validate its native/lifecycle/retirement facts without adding it
    # to policy eligibility or inventing an effort score. No effort was requested.
    model = check_task_pin(raw, policy, backend, model)
    return model, (), ()


def default_chain() -> tuple[str, ...]:
    """Every known backend, the default one first, then the rest in name order.

    Derived from the registry, so a newly registered driver joins the chain without an
    edit here — unless its row opts out with `in_default_chain=False`.
    """
    names = [
        name
        for name in registry.known_backends()
        if getattr(registry.BACKENDS.get(name), "in_default_chain", True)
    ]
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
    strict_models: bool = False
    invalid_models: Mapping[str, str] | None = None

    @property
    def eligible_models(self) -> dict:
        """The role's model table without entries the adopted shared policy invalidated."""
        return {k: v for k, v in self.models.items() if k not in (self.invalid_models or {})}

    def model_for(self, backend: object) -> Optional[str]:
        """The model this role runs under on `backend`.

        `None` means "no model is specified anywhere" — the caller should let the backend
        pick its own default rather than guess one.
        """
        name = registry.normalise_name(backend)
        if self.invalid_models and name in self.invalid_models:
            return None
        configured = _text(self.models.get(name))
        if configured:
            return configured
        return None if self.strict_models else DEFAULT_MODELS.get(self.role, {}).get(name)


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
    project = _derived(_project(config))
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

    return RoleConfig(
        role=key,
        backend=backend,
        models=models,
        chain=_clean_chain(project.get(DERIVED_CHAIN_KEY)) or default_chain(),
        strict_models=True,
        invalid_models={},
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
        if not why and settings.strict_models and not settings.model_for(name):
            why = (settings.invalid_models or {}).get(name, 'no eligible role model in adopted policy')
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
