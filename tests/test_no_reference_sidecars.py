"""The gate M4c's plan calls for (`docs/plans/2026-08-18-m4c-superseded-sidecars.md` §6.3):
no `maestro/` module may hold a hardcoded `REPO / "scripts" / "<name>"` path constant
naming a script that M5 deletes.

Why this exists: M1's extraction covered `orchestrator_run.py` only, never the sidecar
scripts it shells out to by path. Three times now (M4a, M4b, M4c) a stage has found
maestro code still reaching into the reference project's `scripts/` directory for
functionality M5 is about to delete out from under it — silently, because every call
site is `.exists()`-guarded, so the failure mode is "the feature goes dark with no
error," not "the test suite goes red." This test closes that hole for good: a fresh
`REPO / "scripts" / "…"` constant appearing anywhere in `maestro/` fails CI immediately,
before it can ship as a fourth near-miss.

Two scripts are legitimate, documented exceptions — see `ALLOWED` below — because they
are *not* on `docs/DESIGN.md` §11's superseded list; they are project-owned "how to
ship" / "what good looks like" scripts that M5 does not touch.

Anti-vacuity: `test_the_gate_actually_bites` proves the checker fires on a synthetic
violation, following the precedent set by `tests/test_no_unresolved_pending.py` (the
gate that would have caught M4a) — a walk that silently finds nothing must not pass
only because it never runs for real.
"""
from __future__ import annotations

import pathlib
import re

import maestro

# Matches the exact literal shape used throughout maestro/: `REPO / "scripts" / "name.py"`
# (module-level path constants derive from `Paths.from_env()`'s `.repo`, always bound to
# the local name `REPO`). Deliberately narrow — this is the shape every prior finding
# (R1-R19, M4a, M4b) actually took; a looser pattern would also flag legitimate prose
# ("run scripts/check_notebook.py") that this gate is not meant to police.
PATTERN = re.compile(r'REPO\s*/\s*"scripts"\s*/\s*"([^"]+)"')

# Documented, deliberate exceptions: scripts NOT on `docs/DESIGN.md` §11's superseded
# list, so a hardcoded path to them survives M5 unbroken. Citations:
ALLOWED = {
    # docs/plans/2026-08-18-m4c-superseded-sidecars.md §2: "Not maestro's, left in the
    # project: scripts/canary_deploy.py and the check_*.py verification commands the
    # roadmap names ... those are 'how to ship' and 'what good looks like'."
    "canary_deploy.py",
    # check_notebook.py is the same class of script (a project-owned syntax-check the
    # /redo runner shells out to, mirroring canary_deploy.py) but was not in the M4c
    # plan's original survey — the survey ran before the /redo runner (R9) was folded
    # into maestro/selfheal/redo.py, so this dependency did not exist yet to find. See
    # docs/PROGRESS.md's M4c batch-3 entry for the full account.
    "check_notebook.py",
}


def _maestro_source_files() -> list[pathlib.Path]:
    root = pathlib.Path(maestro.__file__).resolve().parent
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _violations(files: list[pathlib.Path]) -> list[str]:
    problems = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for m in PATTERN.finditer(text):
            script = m.group(1)
            if script not in ALLOWED:
                problems.append(f"{path}: REPO / \"scripts\" / \"{script}\"")
    return problems


def test_no_undocumented_reference_sidecar_paths():
    problems = _violations(_maestro_source_files())
    assert not problems, (
        "maestro/ holds a hardcoded scripts/ path constant not covered by the documented "
        "exception list in this file's ALLOWED set. If the named script is on "
        "docs/DESIGN.md §11's superseded-at-M5 list, it needs a real maestro-owned "
        "extraction (mirror the pattern in docs/plans/2026-08-18-m4c-superseded-"
        "sidecars.md), not a path constant that will silently stop working the moment M5 "
        "deletes it. If it is legitimately project-owned and survives M5, add it to "
        "ALLOWED here with a one-line citation, the same way canary_deploy.py and "
        "check_notebook.py are documented above.\nFound:\n  " + "\n  ".join(problems)
    )


def test_the_gate_actually_bites(tmp_path):
    """Prove `_violations` fires on a synthetic, undocumented `REPO / "scripts" / …`
    constant — a checker that silently finds nothing must not be trusted just because it
    ran; it must be shown to fail when it should."""
    synthetic = tmp_path / "synthetic_module.py"
    synthetic.write_text(
        'REPO = None\n'
        'DELETED_HELPER = REPO / "scripts" / "not_a_real_allowed_script.py"\n'
        'STILL_FINE = REPO / "scripts" / "canary_deploy.py"\n',
        encoding="utf-8",
    )
    problems = _violations([synthetic])
    assert len(problems) == 1
    assert "not_a_real_allowed_script.py" in problems[0]
    assert "canary_deploy.py" not in "".join(problems)


def test_allowed_exceptions_are_still_actually_present():
    """The two documented exceptions should each be found by the pattern at least once —
    if a future refactor removes the only remaining reference, the exception is dead
    weight and should be pruned from ALLOWED rather than silently kept forever."""
    found_scripts = {
        m.group(1)
        for path in _maestro_source_files()
        for m in PATTERN.finditer(path.read_text(encoding="utf-8"))
    }
    stale = ALLOWED - found_scripts
    assert not stale, (
        f"ALLOWED lists {stale} but no maestro/ file references it as a "
        f'REPO / "scripts" / "..." constant any more — prune the exception."'
    )
