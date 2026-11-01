"""One place that turns "ask <role> a question" into a driver call.

Every bounded agent call in maestro used to be written out by hand: build an argv starting
with a CLI's name, `subprocess.run` it, read stdout, swallow the exceptions. Six modules did
that, each slightly differently, and all six named the same binary — so on a project
configured for any other backend they silently did nothing.

`ask()` is the seam that replaces all of them. It resolves the role the way every other
launch path resolves one (`maestro.roles`, with the operator's `/backend <name>` leading),
builds that backend's driver through the registry, and hands it a `CompletionSpec`. Callers
say *what they want asked and by whom*; which binary answers is a configuration question
they no longer have an opinion about.

Nothing here executes anything at import time, and `ask()` never raises: a failure comes
back as a `Completion` with empty `text`, because every caller is best-effort and an
exception on a poll cycle would take the loop down.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence
import logging

from maestro import quota
from maestro import roles
from maestro.backends import registry
from maestro.backends.base import Completion, CompletionSpec
from maestro.implementer import operator_backend

__all__ = ["ask", "resolve_call"]

#: Default ceiling for a bounded call, in seconds. Matches what the judgment call sites
#: were already using by hand; the two agentic ones pass their own 30-minute budget.
DEFAULT_TIMEOUT = 240


def _driver_for(backend: str):
    """The driver instance for `backend`.

    Constructed bare. A launch has to tell its driver where this orchestrator's python and
    tmux live (`implementer._driver_kwargs` threads those through); a completion starts no
    window and generates no launcher, so it needs none of it — every driver's constructor
    is inert and defaults the rest.
    """
    return registry.driver_class(backend)()


def _independent_call(role, source_routes):
    """Route a legacy judgment using actual authors and the task's frozen catalog."""
    from maestro import config, model_policy, thirdparty
    from maestro.backends import catalog
    from maestro.backends.router import AgentBinding, dispatch_plan

    _, policy, project = model_policy.runtime_model_inputs(
        config.load_project_yaml(), config.PROJECT_YAML.parent)
    key = model_policy.LEGACY_ROLE_NAMES.get(str(role), str(role))
    sources = model_policy.role_independence(project).get(key, ())
    if not any(source_routes.get(source) for source in sources):
        return None
    native = catalog.catalog_from_register(thirdparty.load_register(),
        defaults=model_policy.graph_catalog_defaults(policy, project))
    families = set()
    for source in sources:
        for driver, model in source_routes.get(source, ()):
            try:
                entry = native.entry(registry.driver_ref(driver).backend_id)
                profiles = [p for m, p in entry.model_profiles.items()
                            if m == model or model in (p.effort_variants or {}).values()]
                family = profiles[0].model_family if len(profiles) == 1 else None
            except (ValueError, KeyError):
                family = None
            families.add(family or '*')
    exhausted = quota.exhausted_backends()
    native = catalog.CapabilityCatalog({backend: entry for backend, entry in native.entries.items()
        if registry.driver_name_for_backend_id(backend) in roles.default_chain()
        and registry.driver_name_for_backend_id(backend) not in exhausted})
    binding = model_policy.legacy_binding(project, policy, key, native_catalog=native,
                                          independent_families=frozenset(families))
    if not isinstance(binding, AgentBinding):
        raise model_policy.PolicyError(f'no eligible {role} route: {binding.reason}')
    logging.getLogger(__name__).info('legacy %s route %s/%s: %s', role,
        binding.backend_id, binding.model_id, binding.selection_reason)
    return registry.driver_name_for_backend_id(binding.backend_id), dispatch_plan(binding).model


def resolve_call(role: object, *, model: str = "", source_routes=None) -> tuple[str, str]:
    """`(backend, model)` for `role`, before any process is started.

    Split out from `ask()` so a caller that wants to *name* what it is about to do — a
    journal line, a `[model]` print — can ask without running anything, and so the
    resolution itself is testable without a driver.

    The operator's `/backend <name>` leads and the role's fallback chain follows, via
    `roles.resolve(role, preferred=operator_backend() or None, exhausted=...)` — the same
    call `implementer._implementer_backend` and `orchestrator._launch_backend` make
    (C4, then A6, readiness queue). **A6 (2026-09-02)** is what made all three actually
    agree: `exhausted=quota.exhausted_backends()` is the timed per-backend record
    `orchestrator.reconcile_in_flight` writes when a real exit is classified
    `quota_exhausted`, so a pin currently recorded as exhausted is skipped here exactly
    as it is on the other two paths, and the fallback chain is walked instead. All three
    must keep reading that same set together — see `_launch_backend`'s own docstring for
    why a divergence would make the `in_flight` entry it records name a backend the task
    is not actually running on. Honouring the order here matters regardless: an operator
    who switches backends because one is rate limited means it for the judge and the
    diagnoser too, not just the implementer.

    An explicit `model` wins over the role's table — `/redo` and self-fix pin a model per
    task — and the role's model for the resolved backend fills in otherwise. Note that is
    the model for *the backend that won*, not for the configured one, so a call that falls
    through the chain does not carry the wrong CLI's model id with it.
    """
    if source_routes is not None and not model:
        independent = _independent_call(role, source_routes)
        if independent is not None:
            return independent
    outcome = roles.resolve(
        role, preferred=operator_backend() or None, exhausted=quota.exhausted_backends()
    )
    if not outcome.usable:
        from maestro.model_policy import PolicyError
        raise PolicyError(f'no eligible {role} route: {outcome.reason}')
    if model:
        # An explicit pin is served as written on the backend that won, or refused.
        from maestro import config as _project_config
        from maestro.model_policy import runtime_model_inputs, check_task_pin
        _, shared, project = runtime_model_inputs(_project_config.load_project_yaml(), _project_config.PROJECT_YAML.parent)
        if shared is not None:
            model = check_task_pin(project, shared, outcome.backend, model)
    delivered, _, _ = roles.launch_controls(role, outcome.backend, model or outcome.model or "")
    return outcome.backend, delivered


def ask(
    role: object,
    prompt: str,
    *,
    model: str = "",
    timeout: int = DEFAULT_TIMEOUT,
    cwd: Optional[Path] = None,
    resume_id: str = "",
    log_file: Optional[Path] = None,
    writable: bool = False,
    add_dirs: Sequence[Path] = (),
    source_routes=None,
) -> Completion:
    """Ask `role` one bounded question and return what came back.

    Never raises. A backend that cannot be resolved, a driver that cannot be constructed
    and a run that fails all produce the same empty `Completion` the drivers themselves
    return for a failed call, so callers have exactly one failure shape to handle.
    """
    try:
        backend, resolved_model = resolve_call(role, model=model,
            **({'source_routes': source_routes} if source_routes is not None else {}))
        effort_args, effort_config = roles.effort_controls(role, backend, resolved_model)
        driver = _driver_for(backend)
    except Exception:
        return Completion(text="", returncode=1)

    return driver.complete(CompletionSpec(
        prompt=prompt,
        model=resolved_model,
        timeout=timeout,
        cwd=cwd,
        resume_id=resume_id,
        log_file=log_file,
        writable=writable,
        add_dirs=tuple(add_dirs),
        effort_args=effort_args,
        effort_config=effort_config,
    ))
