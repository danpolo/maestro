"""Bindings for names whose owning module is not importable at import time.

M1 extracts one module at a time in a fixed dependency order, so an early module
sometimes calls a helper the mapping table assigns to a later one. The extraction rule
is absolute — function bodies are copied verbatim — so the call site cannot be changed
to a lazy import. The name is bound at module level instead, and this module supplies
the two possible bindings.

``pending(name, owner)`` — the owner does **not exist yet**
    A placeholder that raises ``NotImplementedError`` when called. The module imports
    cleanly and the attribute exists (so a characterisation test can monkeypatch it the
    same way it monkeypatches the reference module's global), but any path that actually
    calls it fails loudly instead of silently doing nothing. Grep for ``pending(`` to
    find the names still waiting on an extraction.

``deferred(name, owner)`` — the owner **exists**, but importing it here would cycle
    A late-bound reference. Nothing is imported when this module is imported; on the
    first call the owner is imported, ``name`` is looked up on it, and the call is
    delegated. This is what breaks the cycle: the outstanding placeholders all point
    *forwards* along the extraction order (``hitl/commands`` → ``parking``, ``merge`` →
    ``selfheal``), so a top-level import would invert an edge the owner already depends
    on. Resolution is cached after the first success, and the binding stays an ordinary
    module attribute, so ``monkeypatch.setattr(mod, name, stub)`` keeps working.

Rule of thumb: once a placeholder's owner lands, the binding becomes ``deferred`` (or a
plain import where the dependency order allows one). ``pending`` is only ever correct
for a name nothing can provide yet.
"""
from __future__ import annotations

import importlib
from typing import Callable


def pending(name: str, owner: str = "") -> Callable[..., object]:
    """A stand-in for `name`, which `owner` will provide once it is extracted."""

    def _not_extracted_yet(*args, **kwargs):
        where = f" (owned by {owner})" if owner else ""
        raise NotImplementedError(f"{name} is not extracted yet{where}")

    _not_extracted_yet.__name__ = name
    _not_extracted_yet.__qualname__ = name
    _not_extracted_yet.pending_owner = owner
    return _not_extracted_yet


def deferred(name: str, owner: str) -> Callable[..., object]:
    """A late-bound reference to ``owner.name``, resolved on the first call.

    ``owner`` is a fully-qualified module path (``"maestro.parking"``). Nothing is
    imported until the returned callable is invoked, which is what makes this safe to
    use where a top-level import would create a cycle.
    """
    # One-slot cache. A list, not a closure variable, so the resolved target survives
    # without ``nonlocal`` gymnastics; empty means "not resolved yet".
    resolved: list = []

    def _deferred(*args, **kwargs):
        if not resolved:
            # Failures are raised as ImportError — including the missing-attribute case,
            # which is naturally an AttributeError. A typo'd owner must be unmissable,
            # and AttributeError is the one exception a call site is likely to swallow
            # by accident: ``getattr(obj, x, default)``, ``hasattr``, and duck-typing
            # guards all absorb it silently. ImportError is never swallowed that way, so
            # a broken binding surfaces as a broken binding. ``from exc`` keeps the
            # original ModuleNotFoundError/AttributeError in the traceback.
            try:
                module = importlib.import_module(owner)
            except ImportError as exc:
                raise ImportError(
                    f"deferred name {name!r} cannot be resolved: "
                    f"its declared owner module {owner!r} is not importable ({exc})"
                ) from exc
            try:
                target = getattr(module, name)
            except AttributeError as exc:
                raise ImportError(
                    f"deferred name {name!r} cannot be resolved: "
                    f"its declared owner module {owner!r} has no attribute {name!r}"
                ) from exc
            resolved.append(target)
        return resolved[0](*args, **kwargs)

    _deferred.__name__ = name
    _deferred.__qualname__ = name
    _deferred.deferred_owner = owner
    return _deferred
