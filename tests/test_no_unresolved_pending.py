"""The gate that would have caught M4a.

`maestro/pending.py` offers two bindings for a name whose owner sits later in the M1
extraction order:

* ``pending(name, owner)`` — a placeholder that **raises** when called. Correct only while
  nothing can provide the name.
* ``deferred(name, owner)`` — a late binding that imports ``owner`` on first call and
  delegates. Correct once the owner exists.

Between M1 and M4 the codebase carried thirteen `pending()` placeholders whose owning
modules had been extracted for real, and two of those named owner modules
(``maestro.judge``, ``maestro.smoke``) that never existed at all. Nothing noticed, because
the characterisation tests monkeypatch exactly these names — **a placeholder is invisible
to a test that replaces it** — and because `deferred` resolution happens at call time, so
a typo'd owner stays silent until the live loop reaches that branch.

This module closes both holes with one walk over the package:

1. any surviving `pending()` placeholder is a failure, and
2. any `deferred()` binding whose declared owner cannot supply the name is a failure.

Both run against the real `maestro` package, and both are re-run against a synthetic
module in ``test_the_gate_actually_bites`` so the checker itself is proven to fail when it
should — otherwise this file would only be testing a copy of the gate.
"""
from __future__ import annotations

import importlib
import pkgutil
import sys
import types

import pytest

import maestro
from maestro.pending import deferred, pending

# `deferred()` and `pending()` each stamp the returned callable with a marker naming the
# module that owes the implementation. The two names are disjoint, so a binding can be
# classified without calling it — which matters, since calling one would run real code.
PENDING_MARKER = "pending_owner"
DEFERRED_MARKER = "deferred_owner"


def _walk_maestro_modules() -> list[types.ModuleType]:
    """Every importable module in the `maestro` package.

    Discovered, never hardcoded: a hardcoded list is exactly how the *next* module gets
    missed. An import error is a failure with its traceback, never a silent skip.
    """
    modules = [maestro]
    for info in pkgutil.walk_packages(maestro.__path__, prefix="maestro."):
        try:
            modules.append(importlib.import_module(info.name))
        except Exception as exc:  # noqa: BLE001 — the traceback is the point
            raise AssertionError(
                f"{info.name} could not be imported, so it cannot be checked for "
                f"unresolved bindings: {exc!r}"
            ) from exc
    return modules


def audit_module(module: types.ModuleType) -> list[str]:
    """Return one human-readable complaint per broken binding on `module`.

    Shared by the real test and the self-test below, so the thing being proven to bite is
    the thing that actually runs.
    """
    problems: list[str] = []
    for attr in sorted(vars(module)):
        obj = getattr(module, attr, None)

        owner = getattr(obj, PENDING_MARKER, None)
        if owner:
            problems.append(
                f"{module.__name__}.{attr} is still an unresolved pending() placeholder "
                f"(declared owner: {owner}). It raises NotImplementedError the moment the "
                f"live loop reaches it. If {owner} now exists, rebind it with "
                f"deferred({attr!r}, {owner!r}) from maestro.pending; only leave pending() "
                f"here if nothing in the codebase can supply this name yet."
            )
            continue

        owner = getattr(obj, DEFERRED_MARKER, None)
        if owner:
            target_name = getattr(obj, "__name__", attr)
            try:
                target = importlib.import_module(owner)
            except Exception as exc:  # noqa: BLE001
                problems.append(
                    f"{module.__name__}.{attr} is late-bound to {owner!r}, which cannot be "
                    f"imported: {exc!r}. deferred() resolves at call time, so this stays "
                    f"silent until the live loop reaches that branch — fix the owner here."
                )
                continue
            if not hasattr(target, target_name):
                problems.append(
                    f"{module.__name__}.{attr} is late-bound to {owner}.{target_name}, but "
                    f"{owner} has no attribute {target_name!r}. Check the Function -> Module "
                    f"Mapping table in docs/plans/2026-08-09-m0-m1-core-extraction.md for "
                    f"the module that really owns it."
                )
    return problems


def test_no_module_carries_a_broken_binding():
    """No `pending()` placeholder survives, and every `deferred()` owner is real."""
    problems: list[str] = []
    for module in _walk_maestro_modules():
        problems.extend(audit_module(module))

    assert not problems, "Broken late bindings in the maestro package:\n  " + "\n  ".join(problems)


def test_the_walk_actually_reaches_the_package():
    """Guard the guard: a walk that silently found nothing would pass test 1 vacuously."""
    names = {m.__name__ for m in _walk_maestro_modules()}
    for expected in (
        "maestro.hitl.commands",
        "maestro.merge",
        "maestro.parking",
        "maestro.orchestrator",
        "maestro.prep_actions",
    ):
        assert expected in names, f"{expected} was not reached by the package walk"


def test_at_least_one_deferred_binding_exists_to_check():
    """If every `deferred()` binding disappeared, test 1 would also pass vacuously."""
    found = [
        f"{m.__name__}.{a}"
        for m in _walk_maestro_modules()
        for a in vars(m)
        if getattr(getattr(m, a, None), DEFERRED_MARKER, None)
    ]
    assert found, "no deferred() bindings found — has the mechanism been removed?"


def test_the_gate_actually_bites():
    """Run the same checker over a module carrying both defects; it must report both."""
    victim = types.ModuleType("maestro._gate_selftest")
    victim.still_pending = pending("still_pending", "maestro.parking")
    victim.bad_owner = deferred("bad_owner", "maestro.judge")  # never existed
    victim.missing_attr = deferred("not_a_real_name", "maestro.state")
    victim.fine = deferred("read_state", "maestro.state")

    problems = audit_module(victim)
    joined = "\n".join(problems)

    assert len(problems) == 3, f"expected 3 complaints, got {len(problems)}:\n{joined}"
    assert "still_pending" in joined and "unresolved pending() placeholder" in joined
    assert "maestro.judge" in joined and "cannot be imported" in joined
    assert "not_a_real_name" in joined and "has no attribute" in joined
    assert "fine" not in joined, "a healthy deferred() binding must not be reported"


def test_the_gate_names_the_binding_site_and_the_owner():
    """A 3am failure message has to say where the binding is and what it points at."""
    victim = types.ModuleType("maestro._gate_message_test")
    victim.bad_owner = deferred("bad_owner", "maestro.nonexistent_module")

    (problem,) = audit_module(victim)
    assert "maestro._gate_message_test.bad_owner" in problem
    assert "maestro.nonexistent_module" in problem


@pytest.mark.parametrize("marker", [PENDING_MARKER, DEFERRED_MARKER])
def test_markers_are_still_the_ones_the_gate_looks_for(marker):
    """If pending.py renames a marker, this gate goes blind rather than red. Pin them."""
    probe = {PENDING_MARKER: pending, DEFERRED_MARKER: deferred}[marker]("x", "maestro.state")
    assert hasattr(probe, marker), (
        f"maestro.pending no longer stamps {marker!r}; "
        f"tests/test_no_unresolved_pending.py would silently stop checking."
    )


def test_no_maestro_module_is_missing_from_sys_modules_after_the_walk():
    """The walk imports for real — a lazily-skipped module would not be audited."""
    _walk_maestro_modules()
    assert "maestro.orchestrator" in sys.modules
