<!-- Provenance note, added 2026-09-02 by the pre-integration queue's final
     whole-branch review (Important 5). -->

> **Provenance.** This is the working report for item D2 — gate the whole backend capability surface, written during the
> 2026-08-30 pre-integration queue and copied verbatim into `docs/plans/` when that
> queue's scratch directory (`/home/dan/.maestro-sdd/2026-08-30-pre-integration/`)
> was deleted on completion.
>
> `tests/test_no_dead_backend_surface.py` cites this file three times: for the full
> argument behind the `Capabilities.sandbox` xfail (and the case for the other reading),
> and for the fix-round RED/GREEN proof that the coverage probe is not vacuous under the
> reviewer's own mutation. Both are reasoning that the test file states conclusions from
> but cannot itself carry.
>
> Paths and sibling-file references inside the report below are **historical** and
> name files that no longer exist. They are left unedited because the report is
> evidence, not documentation: rewriting it would defeat the point of keeping it.

---

# Task D2 report — gate the whole backend capability surface

## Status

DONE_WITH_CONCERNS — the gate is implemented, tested, green in the committed suite, and
(this is the concern) it caught a real, live, currently-unfixed defect during
construction: `Capabilities.sandbox` has zero core readers today. That defect is out of
this item's file scope (`tests/` only) to fix, so it is reported here rather than
silently absorbed.

## What was implemented

`tests/test_no_dead_backend_surface.py` (375 lines, new file). It answers, for the whole
backend contract, the question `parse_exit` exposed a gap in: *"is this actually read by
anything, or only implemented and driver-tested?"*

Surface covered, **derived from the live objects, never hardcoded**:

* `_protocol_surface()` — every non-dunder method `AgentBackend` declares (`capabilities`,
  `launch`, `resume`, `complete`, `parse_exit`, `usage`) plus every class-level annotation
  (`name`), read off `vars(AgentBackend)` / `AgentBackend.__annotations__`.
* `_capabilities_surface()` — `Capabilities._fields` (`native_resume`,
  `system_prompt_file`, `usage_telemetry`, `sandbox`).

Because both are derived from the live class/NamedTuple rather than a hand-maintained
list, a future protocol method or `Capabilities` field is picked up automatically — this
is what makes the gate future-proof against a second `parse_exit`, not just retroactively
aware of the first one.

"Has a reader" is answered by one AST walk over every `.py` file under `maestro/`
**excluding `maestro/backends/`**: every `ast.Attribute` node's `.attr` (covers a method
call's `func` attribute and a plain field read with one pass) is collected into a set. A
surface member with no attribute of that name anywhere in that set, and no allow-list
entry, is reported.

Two coverage tests:

* `test_every_protocol_member_has_a_core_reader` — green today; all six protocol methods
  and `name` (allow-listed, see below) are accounted for.
* `test_every_capabilities_field_has_a_core_reader` — parametrised per field.
  `native_resume`, `system_prompt_file`, `usage_telemetry` pass for real.  `sandbox` is
  `pytest.mark.xfail(strict=True)` — see the sandbox section below.

Plus 15 supporting tests: two "guard the guard" pins (the derived surface still contains
the brief's named examples and the expected four `Capabilities` fields), and eight
anti-vacuity tests proving the checker fires on a synthetic violation, stays silent when
fully covered, respects the allow-list, ignores prose/comment mentions (not real
`ast.Attribute` nodes), genuinely excludes `maestro/backends/`, reaches real core modules,
finds the three already-known real readers, and that every allow-list entry both carries
a real justification string and names an actual surface member (not a typo or dead
entry).

## TDD evidence

**RED** — before adding the `xfail` marker, the parametrised `sandbox` case was a plain
`pytest.param("sandbox")`, run against the real, unmodified `maestro/` tree:

```
$ python3 -m pytest tests/test_no_dead_backend_surface.py -q
....F............                                                        [100%]
=================================== FAILURES ===================================
___________ test_every_capabilities_field_has_a_core_reader[sandbox] ___________
field_name = 'sandbox'

    @pytest.mark.parametrize("field_name", _CAPABILITIES_CASES)
    def test_every_capabilities_field_has_a_core_reader(field_name):
        ...
        referenced = _core_referenced_attrs()
>       assert field_name in referenced or field_name in CAPABILITIES_ALLOWLIST, (...)
E       AssertionError: Capabilities.sandbox has no reader under maestro/ outside
E       maestro/backends/ and is not in CAPABILITIES_ALLOWLIST. ...
=========================== short test summary info ============================
FAILED tests/test_no_dead_backend_surface.py::test_every_capabilities_field_has_a_core_reader[sandbox]
```

16 of 17 tests passed; the one failure was exactly and only `sandbox`, against real
`maestro/` source, not synthetic input — this is the strongest possible anti-vacuity
evidence available: the gate, on its very first run against the real tree, found a real
instance of the exact defect class it exists to catch. This directly answers the
self-review question the controller posed: **yes, this gate would have caught
`parse_exit` during the two milestones it was dead** — it is currently doing the
equivalent for `sandbox`, live.

**GREEN** — after adding `pytest.mark.xfail(strict=True, reason=...)` to the `sandbox`
case only (see rationale below):

```
$ python3 -m pytest tests/test_no_dead_backend_surface.py -v
....................................
tests/test_no_dead_backend_surface.py ....x............                  [100%]
======================== 16 passed, 1 xfailed in 1.13s =========================
```

## Full suite

* Fast tier: `python3 -m pytest -m "not slow" -q` — clean, no failures (one `x` visible
  in the dot stream, same expected xfail).
* Bare full suite, foreground, per instructions:
  `python3 -m pytest -q` → **exit 0**, ~2m33s (repeated twice, foreground both times to
  be sure — the auto-background move on the first attempt was a tool timeout artifact
  from an unset `timeout`, not a real failure; the deliberate foreground rerun with a
  600000ms timeout confirms exit 0 cleanly).
* Collected count: 3278 (baseline at `7a4fab1`) → **3295** (+17, exactly the new file's
  test count). `--collect-only` sum command from the brief confirms this.
* Pristine: no unexpected output, no warnings surfaced, exactly one `x` (the documented,
  strict xfail) in the whole run.

## My decision on `Capabilities.sandbox`, and the argument for it

**Decision: do not allow-list it. Report it as a live finding, and cover it in the gate
with a `pytest.mark.xfail(strict=True)`, not a `CAPABILITIES_ALLOWLIST` entry.**

Argument:

1. **It is not "genuinely driver-internal."** `CompletionSpec`'s own docstring states the
   contract normatively: "Drivers that can enforce that (`Capabilities.sandbox`) must;
   drivers that cannot are free to ignore it." That sentence describes a *core*
   responsibility (derive `writable`/`sandbox` request fields from the declared
   capability) that core has not implemented. Writing an allow-list comment calling
   `sandbox` "informational" or "driver-internal" would be false — it would misdescribe a
   real gap as an intentional design choice, which is exactly the shape of self-deception
   the controller's ruling warned against ("a gate whose allow-list absorbs its only
   finding is vacuous").
2. **It is the same shape as `parse_exit`, not a lesser case.** Fully implemented on both
   drivers (`Capabilities(sandbox=True/False, ...)`), fully tested at the driver level
   (`tests/backends/test_codex_driver.py`'s `caps.sandbox is True`,
   `tests/backends/test_protocol.py`'s `caps.sandbox is False`), zero core callers. If
   this gate is allowed to wave through the one violation it found on its first real run,
   it proves nothing about whether it would have caught `parse_exit` — it would have
   caught it only by accident of `parse_exit` not yet having a plausible-sounding
   allow-list excuse available.
3. **But the gate must not ship permanently red either.** Nothing else in this suite is
   red on a normal checkout; a red `pytest -q` is a tax on every future contributor for
   one already-known, already-reported issue, and the file-scope restriction on this item
   (`tests/` only) means I cannot fix the underlying production gap myself. `strict=True`
   `xfail` is the middle path: the real check still runs against real source every
   invocation (this is not a skip and not synthetic), the suite stays green today, and
   the moment core starts reading `capabilities().sandbox` for real, this specific case
   flips to an *unexpected pass*, which `pytest` reports as a hard failure — forcing
   whoever lands that fix to notice and delete the marker rather than have it silently go
   stale.

**Recommendation to the controller / whoever picks this up next:** this is a real,
reportable production finding, independent of D2's own scope. `LaunchSpec.sandbox` /
`CompletionSpec.writable` are never derived from `capabilities().sandbox` anywhere in
`maestro/switch.py`, `maestro/implementer.py`, `maestro/agentcall.py`,
`maestro/selfheal/redo.py`, or `maestro/selfheal/selffix.py` — every `writable=` call site
is a static `True`/`False` chosen by the caller's own judgment about the kind of call
(judgment vs. agentic), never gated on the driver's declared confinement capability. It
should go on a future item's backlog as a genuine production fix, not be resolved by
widening this gate's allow-list.

## Allow-list, entry by entry

`PROTOCOL_ALLOWLIST` (one entry):

* **`name`** — `AgentBackend.name` is a driver's own identity string. No core module
  reads `driver.name` directly; each driver stamps it onto `Handle.backend` when
  constructing a `Handle` (`self.name` inside `claude.py`/`codex.py`), and core reads
  *that* field instead (`maestro/switch.py`'s
  `registry.normalise_name(handle.backend) == target`). So `name` genuinely is consumed
  by core, just one hop removed from the driver attribute itself — this is a real,
  verified reader path, not a rationalisation. Separately, and independently sufficient:
  AST-matching a bare `.name` attribute access outside `backends/` would be close to
  meaningless as a check regardless — `name` is one of the most common attribute names in
  the codebase (`Path.name`, a task's name, a role's name, a window's name — confirmed by
  grep before writing the allow-list entry), so a "hit" would not demonstrate this
  specific attribute is read. A vacuously-always-true check is worse than an honest,
  argued exemption.

`CAPABILITIES_ALLOWLIST` — **deliberately empty.** `native_resume`, `system_prompt_file`,
`usage_telemetry` all have real, verified core readers and need no exemption.  `sandbox`
is the one field with no reader and is *not* here — see the decision above.

Both `test_every_allowlist_entry_has_a_real_nonempty_justification` and
`test_allowlist_entries_name_real_surface_members` pin that the allow-list mechanism
itself can't be abused (a stub reason, or an entry for a name that isn't actually part of
the surface) without the gate's own tests catching it.

## Files changed

* `tests/test_no_dead_backend_surface.py` — new file, 375 lines. This is the only change;
  nothing under `maestro/` was touched, per the file-scope restriction.

## Self-review findings

* **Would this have caught `parse_exit`?** Yes — directly demonstrated by the RED run
  above, on real source, not synthetic. `parse_exit` itself is now covered (it has a real
  reader at `orchestrator.py:646`, added since the bug was fixed), and the gate pins that
  with `test_protocol_surface_includes_the_methods_the_task_brief_names`.
* **Fires on anything it shouldn't?** No false positives found. Checked before writing
  the allow-list decisions: none of the six protocol method names or three covered
  `Capabilities` field names collide with an unrelated attribute anywhere in `maestro/`
  (verified by targeted grep for each, reported in the recon section above). `name` was
  the one genuinely generic name, and it is handled by exemption rather than by a scan
  that would silently "pass" without meaning anything.
* **Known, stated imprecision:** the AST walk matches attribute *names*, not types.
  `Capabilities.sandbox` (bool) and `LaunchSpec.sandbox` (str|None) share a field name;
  the walk cannot tell them apart. Verified this does not matter today — neither is read
  by a `.sandbox` attribute access anywhere outside `maestro/backends/` — and documented
  the residual risk in the module docstring's "Known imprecision" section (same
  convention `test_no_reference_project_strings.py` used for its own comment-scanning
  boundary) rather than attempting a fragile call-chain heuristic that
  `test_no_name_branching.py` exists to keep this suite away from.
* **YAGNI:** removed an initially-added but unused `REPO` constant during self-review
  before committing; the file only defines what it uses.
* **Name accuracy:** file name (`test_no_dead_backend_surface`) and test names describe
  what they check; the docstring is explicit that this gate's *mechanism* differs from
  `test_no_unresolved_pending.py`'s (marker introspection vs. AST source scan) even
  though the brief cites that file as the model in spirit.
* **Real-behaviour tests:** the coverage tests run against the actual `maestro` package
  import and actual file contents on disk — no mocking of the surface or the source
  files. Only the eight anti-vacuity tests use synthetic snippets, matching house
  convention (`test_no_reference_sidecars.py`, `test_no_reference_project_strings.py` both
  do the same).
* **Pristine output:** confirmed via both `-m "not slow"` and the bare full suite; the
  only non-dot marker anywhere is the one documented, expected `x`.

## Concerns

1. **The `sandbox` finding itself** is a real production gap, not fully resolved by this
   item (by design — out of file scope). Flagged above with a concrete recommendation.
2. **The strict-xfail mechanism is a judgment call**, not something the brief or the
   ruling named explicitly as an option (they named allow-list vs. report-as-finding; I
   read "report as a finding" as compatible with, and best served by, an xfail rather than
   either a permanently-red suite or a silent allow-list). If the controller's reading is
   that "report as a finding" should instead mean shipping the gate red until a separate
   item fixes the production code, that is a one-line change (delete the `xfail` marks)
   and I can make it on request.
3. **AST-name-only precision** (the `LaunchSpec.sandbox`/`Capabilities.sandbox` collision)
   is a real, if currently inert, blind spot — documented, not hidden, per the module
   docstring's "Known imprecision" section.

---

## Fix round 1 (base `af8ca9c`)

**Review verdict:** Approved with two Important findings; one entered the fix loop.

### IMPORTANT 1 — anti-vacuity did not cover the gate's own composition (fixed)

The reviewer's own probe: mutating `_core_referenced_attrs()` to unconditionally
`return set(_protocol_surface()) | set(_capabilities_surface())` — ignoring
`_core_maestro_files()`/disk entirely — left 16 of 16 tests in the file passing. The
suite only went red because `sandbox`'s `xfail(strict=True)` happened to flip to an
unexpected pass. Every anti-vacuity test I had written drove `_referenced_attrs()` (the
string-parsing primitive) against synthetic snippets; none of them ever called the real
`_core_referenced_attrs()` / `_core_maestro_files()` pipeline that walks disk and
excludes `maestro/backends/`. The day `sandbox` legitimately grows a reader and its
marker is deleted, that same mutation would pass in total silence — the `parse_exit`
failure mode, reproduced inside the gate meant to prevent it.

**What changed:** added
`test_the_real_aggregator_reports_a_provably_unreferenced_surface_member` to
`tests/test_no_dead_backend_surface.py`. It monkeypatches `_core_maestro_files()` to
return two small controlled temp files (one containing `driver.launch(spec)`, nothing
referencing `resume`/`parse_exit`/`complete`/`capabilities`/`usage`), then calls the
**real, unmodified** `_core_referenced_attrs()` — never a reimplementation — and asserts
against `_protocol_surface()`'s real member names: `launch` (present in the fixture) is
not reported missing, and `resume` (provably absent from the fixture) is reported
missing.

The probe deliberately uses a *real* protocol-surface name (`resume`), not a made-up
one. A made-up name would not have worked: the reviewer's mutation only ever returns
names that already belong to the real surface, so a fabricated probe name would still
correctly read as "missing" even under the vacuous mutation, proving nothing. Only a
real surface member the fixture provably omits can tell the true implementation and the
vacuous one apart.

### RED

Per the coordinator's corrected method for this round, the mutation was applied to a
**scratch copy outside the repo**, never to the tracked checkout (the earlier
before-cutoff exercise, transcribed below, did mutate the checkout in place with a
verified restore each time — reused here rather than redone, per the coordinator's own
instruction that already-captured evidence from before the interruption stands).

Command (mutating a copy of `_core_referenced_attrs()` to the reviewer's exact shape —
`return set(_protocol_surface()) | set(_capabilities_surface())`, then running the
mutated copy in place of the real file):

```
$ python3 -m pytest tests/test_no_dead_backend_surface.py -v
```

Relevant failing output:

```
        referenced = _core_referenced_attrs()
        missing = _missing_readers(_protocol_surface(), referenced, PROTOCOL_ALLOWLIST)

        assert "launch" not in missing, (
            "launch IS on disk in the fixture — the real aggregator must see it"
        )
>       assert "resume" in missing, (
            "resume is absent from the fixture on disk. A vacuous _core_referenced_attrs() "
            "that ignores _core_maestro_files() and just returns the real surface — the "
            "reviewer's exact mutation — would wrongly report resume as covered here, "
            "which is exactly what this assertion exists to catch."
        )
E       AssertionError: resume is absent from the fixture on disk. A vacuous _core_referenced_attrs() that ignores _core_maestro_files() and just returns the real surface — the reviewer's exact mutation — would wrongly report resume as covered here, which is exactly what this assertion exists to catch.
E       assert 'resume' in []

tests/test_no_dead_backend_surface.py:396: AssertionError
=========================== short test summary info ============================
FAILED tests/test_no_dead_backend_surface.py::test_every_capabilities_field_has_a_core_reader[sandbox]
FAILED tests/test_no_dead_backend_surface.py::test_the_real_aggregator_reports_a_provably_unreferenced_surface_member
========================= 2 failed, 16 passed in 0.09s =========================
```

Two failures under the mutation, not one: `sandbox`'s `xfail(strict=True)` flips to an
unexpected pass (the same signal the reviewer originally relied on) **and**, now
independently, the new composition test fails directly — exactly the point of the fix.
The new test's failure does not depend on `sandbox`'s marker state at all.

**Why this failure is expected:** the mutated `_core_referenced_attrs()` ignores the
monkeypatched `_core_maestro_files()` and returns the full real surface unconditionally,
so `resume` — which the fixture never mentions — is wrongly reported as referenced, and
`_missing_readers` correctly no longer lists it as missing. The assertion
`"resume" in missing` is exactly the check that catches this.

### GREEN

Restoring the real, unmutated `_core_referenced_attrs()` and re-running:

```
$ python3 -m pytest tests/test_no_dead_backend_surface.py -v
============================= test session starts ==============================
collected 18 items

tests/test_no_dead_backend_surface.py ....x.............                 [100%]

======================== 17 passed, 1 xfailed in 1.26s =========================
```

18 tests now (17 collected before + 1 new), 17 passed + 1 expected `xfailed`, matching
the pre-mutation baseline exactly.

### Full suite (post-fix)

Foreground, per instructions (`timeout=600000` on the tool call):

```
$ python3 -m pytest -q
...
EXIT_CODE:0
```

* Collected: **3295 (baseline at `af8ca9c`) → 3296** (+1, the new composition test).
  `--collect-only` sum command from the brief confirms this.
* Exit code: **0**.
* Pristine: the dot stream contains exactly **one `x`** — the same, still-only,
  documented `sandbox` `xfail` — no unexpected output, no other non-dot markers.
* Fast tier (`-m "not slow"`) also re-run clean, same single `x`.

### IMPORTANT 2 — name-vs-type imprecision (not in the fix loop, tracked forward)

Confirmed by the reviewer as accurate and currently inert, and judged not cheaply
fixable within `tests/` (a `.capabilities()`-call-chain heuristic would be the same
brittle name-guessing `test_no_name_branching.py`'s own docstring disclaims; real
type-awareness needs a type checker, not an AST walk). Per the coordinator: left as-is,
carried forward to the final review as a tracked limitation, not closed here. It remains
stated in the module docstring's **"Known imprecision, stated rather than hidden"**
paragraph (`tests/test_no_dead_backend_surface.py`, module docstring) — untouched by this
fix round, verified still present after the round-1 edit — so it survives independently
of this report or this worktree.

### Minors — deferred, untouched per instruction

* The `xfail` reason string's `task-D2-report.md` filename pointer (will dangle once this
  plan closes and the doc is deleted) — left as-is, to ride with the final review.
* The untested 40-character floor in
  `test_every_allowlist_entry_has_a_real_nonempty_justification` — left as-is.

### Files changed this round

* `tests/test_no_dead_backend_surface.py` — added
  `import sys` and one new test function (60 lines). Nothing else touched; no production
  code, matching the item's file-scope restriction.

### Commits this round

* `bb819ba` — test(backends): cover the real aggregation pipeline, not just the parser
  (D2 fix round 1)
