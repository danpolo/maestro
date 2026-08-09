"""Placeholders for names owned by modules that have not been extracted yet.

M1 extracts one module at a time in a fixed dependency order, so an early module
sometimes calls a helper the mapping table assigns to a later one. The extraction rule
is absolute — function bodies are copied verbatim — so the call site cannot be changed
to a lazy import. Instead the module binds the name to a placeholder:

    notify_telegram = pending("notify_telegram")  # owned by maestro.hitl.telegram

The module then imports cleanly, the attribute exists (so a characterisation test can
monkeypatch it the same way it monkeypatches the reference module's global), and any
path that actually calls it fails loudly instead of silently doing nothing.

Every placeholder is replaced by a real import when its owning module lands. Grep for
``pending(`` to find the ones still outstanding.
"""
from __future__ import annotations

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
