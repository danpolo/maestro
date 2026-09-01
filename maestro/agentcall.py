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


def resolve_call(role: object, *, model: str = "") -> tuple[str, str]:
    """`(backend, model)` for `role`, before any process is started.

    Split out from `ask()` so a caller that wants to *name* what it is about to do — a
    journal line, a `[model]` print — can ask without running anything, and so the
    resolution itself is testable without a driver.

    The operator's `/backend <name>` leads and the role's fallback chain follows, via
    `roles.resolve(role, preferred=operator_backend() or None)` — the same call
    `implementer._implementer_backend` makes (C4, readiness queue). `orchestrator.
    _launch_backend` produces the same *order of preference* today but not through this
    function or that one: it still does `operator_backend() or backend_for(...)`, a hard
    short-circuit on the pin rather than handing it to `resolve` as a preference. The
    three agree only because none of them is fed `exhausted=`/`available=` data yet, so
    `resolve` always returns its preferred head unchanged — the moment one of them is,
    all three must move together (deferred item `A6`), or the `in_flight` entry
    `_launch_backend` records can name a backend the task is not actually running on.
    Honouring the order here matters regardless: an operator who switches backends
    because one is rate limited means it for the judge and the diagnoser too, not just
    the implementer.

    An explicit `model` wins over the role's table — `/redo` and self-fix pin a model per
    task — and the role's model for the resolved backend fills in otherwise. Note that is
    the model for *the backend that won*, not for the configured one, so a call that falls
    through the chain does not carry the wrong CLI's model id with it.
    """
    outcome = roles.resolve(role, preferred=operator_backend() or None)
    return outcome.backend, (model or outcome.model or "")


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
) -> Completion:
    """Ask `role` one bounded question and return what came back.

    Never raises. A backend that cannot be resolved, a driver that cannot be constructed
    and a run that fails all produce the same empty `Completion` the drivers themselves
    return for a failed call, so callers have exactly one failure shape to handle.
    """
    try:
        backend, resolved_model = resolve_call(role, model=model)
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
    ))
