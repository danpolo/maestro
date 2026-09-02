"""D2, widened by controller ruling R5: the gate covers the whole backend capability
surface — every public `AgentBackend` method/attribute a driver implements, AND every
`Capabilities` field — with a commented allow-list for genuinely driver-internal ones.

The bug this closes: `parse_exit` was fully implemented on both drivers, fully tested at
the driver level (`tests/backends/test_*_driver.py` called it directly and asserted on
its `ExitVerdict`), and never called by any core module for two milestones. The driver
tests kept passing the whole time, because a driver-level test proves a method *works*,
never that anything *uses* it. `tests/test_no_unresolved_pending.py` is this gate's model
in spirit (a walk that fails when a declared thing has no live consumer) but not its
mechanism: that gate inspects *runtime bindings* stamped with an owner marker: this one
has no marker to inspect — `AgentBackend`/`Capabilities` members carry no metadata saying
who is supposed to call them — so "does core read this" has to be answered by looking at
core's actual source, the same way `tests/test_no_reference_sidecars.py` and
`tests/test_no_reference_project_strings.py` do.

**The surface, derived, never hardcoded** (same principle as the pending gate's own
docstring): `_protocol_surface()` reads `AgentBackend`'s own declared methods and
annotations off the live class, and `_capabilities_surface()` reads `Capabilities._fields`
off the live NamedTuple. A member added to either later is picked up automatically —
this is what "would this gate have caught `parse_exit` during the two milestones it was
dead" actually requires: the check must not depend on someone remembering to list the new
member by hand.

**"Has a reader" is answered by AST**, not by naming a hardcoded caller: every `.py` file
under `maestro/` *outside* `maestro/backends/` is parsed, and every `ast.Attribute` node's
`.attr` — which covers both a method call (`driver.parse_exit(...)`, whose `func` is an
`Attribute` node) and a plain field read (`capabilities.native_resume`) with one walk — is
collected into one set. A surface member with no attribute of that name anywhere in that
set has no reader. This is a syntactic proxy, the same class of check
`test_no_reference_sidecars.py`'s `REPO / "scripts" / "..."` regex is: it does not prove
the object at that call site is really a driver or a `Capabilities` instance, only that no
code outside `maestro/backends/` ever spells the name. That is deliberately looser than a
type-checker and deliberately tighter than "grep the word anywhere" (a docstring
*mentioning* `Capabilities.sandbox` in prose is a string constant, not an `ast.Attribute`
node, so it does not count — confirmed by `test_the_gate_ignores_prose_mentions` below).

**Known imprecision, stated rather than hidden:** attribute names are not scoped to a
type, so two different value types sharing a field name would be indistinguishable to
this walk — `Capabilities.sandbox` (a bool: can this driver confine a run) and
`LaunchSpec.sandbox` (a str|None: which confinement mode to request) are exactly such a
pair. Today this does not matter in practice: neither field is read by any `.sandbox`
attribute access anywhere outside `maestro/backends/` (verified by grep as part of this
gate's construction — `LaunchSpec.sandbox` is read only inside the two drivers, to build
their own launch invocation). If a future caller starts reading `spec.sandbox` outside
`backends/` without ever reading `capabilities().sandbox`, this gate would wrongly call
`sandbox` covered. Narrowing the check to require a `.capabilities()`-shaped call chain
was considered and rejected: variable naming (`caps`, `capabilities`, `d.capabilities()`
stored then read a line later) is not reliably recoverable by a syntactic walk without
either false negatives of its own or the kind of brittle name-guessing
`tests/test_no_name_branching.py` exists to forbid elsewhere in this suite.

## The `sandbox` finding

`Capabilities.sandbox` has **zero readers under `maestro/` outside `maestro/backends/`**,
confirmed by hand before this gate existed and reconfirmed by this gate's own walk. It is
asserted at the driver level (`tests/backends/test_codex_driver.py`'s
`caps.sandbox is True`, `tests/backends/test_protocol.py`'s `caps.sandbox is False`) and
described normatively in `CompletionSpec`'s own docstring ("Drivers that can enforce that
(`Capabilities.sandbox`) must; drivers that cannot are free to ignore it") — but nothing
in core ever derives `LaunchSpec.sandbox` or `CompletionSpec.writable` from it. This is
the same shape as the `parse_exit` defect this gate exists to institutionalise against:
fully implemented, fully tested at the driver level, never consumed.

This item's scope is `tests/` only — a production fix belongs to a different item, not
this one — so the decision here is only about how the *gate* should treat a violation it
cannot fix. Two options were on the table: allow-list `sandbox` as informational, or
leave it uncovered and let the gate say so. Allow-listing it would have meant writing a
comment claiming `sandbox` is "genuinely driver-internal" — which is not true; `sandbox`
is exactly the kind of capability core is *supposed* to gate on, per `CompletionSpec`'s
own docstring, it simply doesn't yet. Absorbing a real, live instance of the defect this
gate exists to catch into the "this one's fine" list would make the gate's own allow-list
vacuous in exactly the way its own controller warned against, so `sandbox` is **not** in
`CAPABILITIES_ALLOWLIST`.

But a gate that ships permanently red is not the deliverable either — nothing else in this
suite is red on a normal checkout, and a red `pytest -q` is a cost every future
contributor pays, forever, for one already-known, already-reported finding. So
`test_every_capabilities_field_has_a_core_reader`'s `sandbox` case is parametrised with
`pytest.mark.xfail(strict=True)`, not swept into the allow-list:

* it still **runs the real check against real `maestro/` source** every time the suite
  runs — this is not a skip, and it is not synthetic;
* `strict=True` means the day core actually starts reading `capabilities().sandbox` (the
  production fix this finding calls for), this case flips from "expected failure" to an
  **unexpected pass**, which `pytest` reports as a hard failure — forcing whoever fixes
  the underlying defect to notice and delete the marker, rather than the fix landing
  silently while a stale xfail keeps quietly expecting brokenness;
* it is not a `pytest.mark.skip`/`skipif`, so `tests/test_no_name_branching.py`'s own
  skip-construct gate does not need to (and does not) treat it as a name-branching skip.

See `docs/plans/2026-08-30-pre-integration-d2-report.md` for the full argument and the
case for the other reading.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

import maestro
from maestro.backends import base as backend_base

MAESTRO_DIR = Path(maestro.__file__).resolve().parent
BACKENDS_DIR = MAESTRO_DIR / "backends"

# This gate's own file lives under tests/, not maestro/, so it is never walked by
# `_core_maestro_files()` and needs no self-exemption the way
# `test_no_reference_project_strings.py` needs one for `maestro/`-scanned patterns.

# ---------------------------------------------------------------------------
# the surface, derived from the live objects — never hardcoded (see module docstring)


def _protocol_surface() -> tuple[str, ...]:
    """Every non-dunder method `AgentBackend` declares, plus every class-level
    annotation (`name: str`). A member added to the Protocol later is picked up here
    automatically."""
    methods = {
        attr
        for attr, value in vars(backend_base.AgentBackend).items()
        if not attr.startswith("_") and callable(value)
    }
    attrs = set(backend_base.AgentBackend.__annotations__)
    return tuple(sorted(methods | attrs))


def _capabilities_surface() -> tuple[str, ...]:
    """Every field `Capabilities` declares, in declaration order."""
    return backend_base.Capabilities._fields


# ---------------------------------------------------------------------------
# the allow-lists — see this file's module docstring for the argument behind each entry

PROTOCOL_ALLOWLIST: dict[str, str] = {
    "name": (
        "AgentBackend.name is a driver's own identity string, and no core module reads "
        "`driver.name` directly — each driver stamps it onto `Handle.backend` when it "
        "builds a Handle (`self.name` inside claude.py/codex.py), and core reads THAT "
        "field instead (e.g. switch.py's "
        "`registry.normalise_name(handle.backend) == target`). `name` is genuinely "
        "consumed, just one hop removed from the driver, and AST-matching a bare "
        "`.name` attribute access outside backends/ would not demonstrate anything "
        "useful here regardless: `name` is one of the most common attribute names in "
        "this codebase (Path.name, a task's name, a role's name, ...), so a hit would "
        "not show THIS attribute is read — a check that always trivially passes is "
        "worse than an honest, explained exemption."
    ),
}

#: Deliberately empty. See the module docstring's "The `sandbox` finding" section for
#: why `Capabilities.sandbox` — the one field with no core reader today — is NOT here.
CAPABILITIES_ALLOWLIST: dict[str, str] = {}


# ---------------------------------------------------------------------------
# "has a reader" — one AST walk, shared by the real gate and its own self-test


def _referenced_attrs(source: str, *, filename: str = "<synthetic>") -> set[str]:
    """Every `.attr` name spelled anywhere in `source`, as a real attribute-access
    expression (a method call's `func`, or a plain field read) — never a string
    constant, a comment, or a keyword argument name."""
    tree = ast.parse(source, filename=filename)
    return {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}


def _core_maestro_files() -> list[Path]:
    """Every `.py` file under `maestro/`, excluding `maestro/backends/` itself — the
    package a driver's own internals live in, and exactly the tree this gate must NOT
    credit as a "reader" or the check would trivially always pass (a driver referencing
    its own `capabilities().sandbox` inside `backends/` proves nothing about whether
    core uses it)."""
    return sorted(
        p
        for p in MAESTRO_DIR.rglob("*.py")
        if "__pycache__" not in p.parts and BACKENDS_DIR not in p.parents
    )


def _core_referenced_attrs() -> set[str]:
    referenced: set[str] = set()
    for path in _core_maestro_files():
        referenced |= _referenced_attrs(path.read_text(encoding="utf-8"), filename=str(path))
    return referenced


def _missing_readers(
    surface: tuple[str, ...], referenced: set[str], allowlist: dict[str, str]
) -> list[str]:
    """Every surface member with no reader and no allow-list entry, in surface order."""
    return [member for member in surface if member not in referenced and member not in allowlist]


# ---------------------------------------------------------------------------
# the gate


def test_every_protocol_member_has_a_core_reader():
    """Every `AgentBackend` method/attribute must be spelled somewhere in `maestro/`
    outside `maestro/backends/`, or be named in `PROTOCOL_ALLOWLIST` with a reason."""
    surface = _protocol_surface()
    referenced = _core_referenced_attrs()
    missing = _missing_readers(surface, referenced, PROTOCOL_ALLOWLIST)
    assert not missing, (
        "AgentBackend member(s) with no reader under maestro/ outside maestro/backends/ "
        "and not in PROTOCOL_ALLOWLIST: " + ", ".join(missing) + ". This is the "
        "`parse_exit`-during-M2/M3` shape: implemented and driver-tested, never called. "
        "Either add a real call site in core, or add a justified PROTOCOL_ALLOWLIST "
        "entry if it is genuinely driver-internal."
    )


_SANDBOX_XFAIL_REASON = (
    "Capabilities.sandbox has no core reader outside maestro/backends/ — a real, "
    "reported finding (docs/plans/2026-08-30-pre-integration-d2-report.md), not a "
    "synthetic case. Deliberately NOT "
    "allow-listed (see this file's module docstring). strict=True: the day core reads "
    "capabilities().sandbox for real, this flips to an unexpected pass and the suite "
    "goes red until the marker is removed."
)

_CAPABILITIES_CASES = [
    pytest.param(field, marks=pytest.mark.xfail(strict=True, reason=_SANDBOX_XFAIL_REASON))
    if field == "sandbox"
    else pytest.param(field)
    for field in _capabilities_surface()
]


@pytest.mark.parametrize("field_name", _CAPABILITIES_CASES)
def test_every_capabilities_field_has_a_core_reader(field_name):
    """Every `Capabilities` field must be spelled somewhere in `maestro/` outside
    `maestro/backends/`, or be named in `CAPABILITIES_ALLOWLIST` with a reason — see
    `_SANDBOX_XFAIL_REASON` above for the one currently-known exception, which is an
    `xfail`, never an allow-list entry."""
    referenced = _core_referenced_attrs()
    assert field_name in referenced or field_name in CAPABILITIES_ALLOWLIST, (
        f"Capabilities.{field_name} has no reader under maestro/ outside "
        f"maestro/backends/ and is not in CAPABILITIES_ALLOWLIST. This is the "
        f"`parse_exit`-during-M2/M3 shape: implemented and driver-tested, never "
        f"called. Either add a real reader in core, or add a justified "
        f"CAPABILITIES_ALLOWLIST entry if it is genuinely driver-internal."
    )


def test_capabilities_surface_is_the_expected_four_fields():
    """Guard the guard: if `Capabilities` grows or shrinks a field, the parametrised
    case list above must move with it, not silently test a stale set. Pinned here so a
    field rename shows up as a failure in *this* test, with a clear cause, rather than
    a confusing pass/fail flip in the parametrised test above."""
    assert _capabilities_surface() == (
        "native_resume",
        "system_prompt_file",
        "usage_telemetry",
        "sandbox",
    )


def test_protocol_surface_includes_the_methods_the_task_brief_names():
    """`parse_exit`, `usage`, `capabilities` are the brief's own named examples of the
    surface this gate must cover; `launch`, `resume`, `complete` complete the protocol.
    A silently-narrowed `_protocol_surface()` that dropped one would defeat the point."""
    surface = _protocol_surface()
    for expected in ("parse_exit", "usage", "capabilities", "launch", "resume", "complete"):
        assert expected in surface, f"{expected} missing from the derived protocol surface"


# ---------------------------------------------------------------------------
# anti-vacuity: the checker itself must be shown to fire, and to stay silent correctly


def test_the_gate_actually_bites():
    """A surface member with no matching `ast.Attribute` anywhere in the scanned source
    is reported missing — proven on a synthetic surface, not the real one, so this test
    does not depend on `maestro/` staying in any particular state."""
    surface = ("alpha", "beta", "gamma")
    source = "x.alpha()\nsomething.other_thing\n"
    referenced = _referenced_attrs(source)
    missing = _missing_readers(surface, referenced, allowlist={})
    assert missing == ["beta", "gamma"], missing


def test_the_gate_stays_silent_when_every_member_is_referenced():
    """The other direction: a surface fully covered by real reader syntax reports
    nothing — a checker that fires unconditionally would be exactly as useless as one
    that never fires."""
    surface = ("alpha", "beta", "gamma")
    source = "x.alpha()\ny.beta\nz.gamma(1, 2)\n"
    referenced = _referenced_attrs(source)
    missing = _missing_readers(surface, referenced, allowlist={})
    assert missing == []


def test_allowlisted_members_are_exempt_even_when_unreferenced():
    """An allow-listed member with zero readers is not reported — proves the exemption
    mechanism works, independently of whether any real member is currently exempt."""
    missing = _missing_readers(("alpha", "beta"), referenced=set(), allowlist={"alpha": "why"})
    assert missing == ["beta"]


def test_the_gate_ignores_prose_mentions():
    """A docstring or comment merely *naming* a surface member is not a reader — only
    real attribute-access syntax counts. This is the exact shape `CompletionSpec`'s own
    docstring has today: it discusses `Capabilities.sandbox` in prose without that
    counting as a call site anywhere real code executes."""
    source = (
        '"""Drivers that can enforce Capabilities.sandbox must; others may ignore '
        'it."""\n'
        "# capabilities().sandbox is mentioned here too, in a comment\n"
        "REAL_CODE = 1\n"
    )
    referenced = _referenced_attrs(source)
    assert "sandbox" not in referenced


def test_the_gate_excludes_the_backends_directory():
    """`_core_maestro_files()` must not walk `maestro/backends/` itself — a driver
    referencing its own capability inside its own module must not count as a core
    reader, or every field would trivially appear covered forever."""
    files = _core_maestro_files()
    assert all(BACKENDS_DIR not in p.parents for p in files)
    assert files, "the walk found nothing at all — cannot prove exclusion means anything"


def test_the_walk_actually_reaches_real_core_modules():
    """Guard the guard, mirroring `test_no_unresolved_pending.py`'s own version of this
    check: a walk that silently found only empty or irrelevant files would make every
    coverage test above pass vacuously by finding nothing to read."""
    names = {p.name for p in _core_maestro_files()}
    for expected in ("orchestrator.py", "switch.py", "implementer.py", "agentcall.py"):
        assert expected in names, f"{expected} was not reached by the core-file walk"


def test_the_walk_finds_the_known_real_readers():
    """The three fields this item's reconnaissance found already covered must still
    show up as referenced by the real walk — if this ever goes false, either core lost
    its reader or the walk itself broke, and either is worth knowing before the
    coverage test above starts trusting a broken signal."""
    referenced = _core_referenced_attrs()
    for expected in ("usage_telemetry", "system_prompt_file", "native_resume"):
        assert expected in referenced, f"{expected} unexpectedly has no reader anymore"


def test_the_real_aggregator_reports_a_provably_unreferenced_surface_member(
    tmp_path, monkeypatch
):
    """Fix round 1, Important 1.

    Every anti-vacuity test above drives `_referenced_attrs()` — the string-parsing
    primitive — directly against a synthetic snippet. None of them ever call the real
    `_core_referenced_attrs()` / `_core_maestro_files()` pair, which is the part that
    actually walks disk under `maestro/` and actually excludes `maestro/backends/`. A
    reviewer proved that gap is real: mutating `_core_referenced_attrs()` to
    unconditionally `return set(_protocol_surface()) | set(_capabilities_surface())` —
    "the aggregator silently claims everything is referenced," ignoring the disk walk
    entirely — left 16 of 16 tests in this file passing. The only reason the suite went
    red at all was that `sandbox`'s `xfail(strict=True)` happened to flip to an
    unexpected pass. The day `sandbox` legitimately grows a real reader and that marker
    is removed, that exact mutation would pass every test in this file in total
    silence — the `parse_exit` failure mode, reproduced inside the gate meant to
    prevent it.

    This closes that hole by calling the real, unmodified `_core_referenced_attrs()` —
    never a reimplementation of its logic — against two small controlled files, via
    `monkeypatch` on `_core_maestro_files()` rather than the real `maestro/` tree. The
    probe deliberately uses REAL protocol-surface member names (`launch`, present on
    disk; `resume`, provably absent), not a made-up name: a made-up name would still
    read as "missing" even under the reviewer's exact vacuous mutation (which only
    ever returns members that ARE part of the real surface), so it would prove
    nothing. Only a real surface member that the fixture provably does not mention can
    tell the true implementation and the vacuous one apart — and here it does: the
    vacuous mutation reports `resume` as referenced regardless of what
    `_core_maestro_files()` was made to return, so it fails this test; the real
    implementation, which actually reads the monkeypatched files, does not.

    See `docs/plans/2026-08-30-pre-integration-d2-report.md`'s fix-round-1 entry for the
    RED/GREEN proof against a
    literal copy of the reviewer's mutation.
    """
    covered_file = tmp_path / "covered.py"
    covered_file.write_text("driver.launch(spec)\n", encoding="utf-8")
    # Deliberately does not reference `.resume(`, `.parse_exit(`, `.complete(`,
    # `.capabilities(`, or `.usage(` — those are the surface members this fixture must
    # provably NOT cover, so their absence from `referenced` below is the real signal.

    monkeypatch.setattr(
        sys.modules[__name__], "_core_maestro_files", lambda: [covered_file]
    )

    referenced = _core_referenced_attrs()
    missing = _missing_readers(_protocol_surface(), referenced, PROTOCOL_ALLOWLIST)

    assert "launch" not in missing, (
        "launch IS on disk in the fixture — the real aggregator must see it"
    )
    assert "resume" in missing, (
        "resume is absent from the fixture on disk. A vacuous _core_referenced_attrs() "
        "that ignores _core_maestro_files() and just returns the real surface — the "
        "reviewer's exact mutation — would wrongly report resume as covered here, "
        "which is exactly what this assertion exists to catch."
    )


def test_capabilities_allowlist_is_empty_by_design():
    """Documents the decision explicitly, as a test rather than only as prose: nothing
    is currently allow-listed away from the Capabilities coverage check. If this ever
    needs to change, the new entry must carry its own reason string, same as
    PROTOCOL_ALLOWLIST's `name` entry does — never a bare name added to make the gate
    go green."""
    assert CAPABILITIES_ALLOWLIST == {}


def test_every_allowlist_entry_has_a_real_nonempty_justification():
    """An allow-list entry with an empty or trivial reason is the allow-list quietly
    absorbing a finding — exactly what the controller's ruling forbids. Every entry's
    reason must be a real sentence, not a stub."""
    for allowlist in (PROTOCOL_ALLOWLIST, CAPABILITIES_ALLOWLIST):
        for member, reason in allowlist.items():
            assert isinstance(reason, str) and len(reason) >= 40, (
                f"{member!r}'s allow-list reason is too short to be a real "
                f"justification: {reason!r}"
            )


def test_allowlist_entries_name_real_surface_members():
    """The inverse of the coverage check: an allow-list entry for a name that is not
    actually part of either surface is dead weight (or, worse, a typo hiding a real
    finding) and should be pruned."""
    protocol = set(_protocol_surface())
    capabilities = set(_capabilities_surface())
    for member in PROTOCOL_ALLOWLIST:
        assert member in protocol, f"{member!r} is not part of the AgentBackend surface"
    for member in CAPABILITIES_ALLOWLIST:
        assert member in capabilities, f"{member!r} is not a Capabilities field"
