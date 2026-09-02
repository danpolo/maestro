"""Maestro must contain nothing that identifies any consuming project.

Two tiers:

* ``CREDENTIAL_PATTERNS`` are scanned in **every** scanned file with no exemptions.
  A leaked token is a defect wherever it appears.
* ``PROJECT_PATTERNS`` and the legacy-path check are scanned everywhere except the
  explicitly named build scaffolding below, plus ``docs/plans/``,
  ``docs/superpowers/plans/`` and ``handoffs/``. Those files document *this
  build* — the design, the process, the live progress log, the overnight
  driver, and session handoffs (including operator directives like pausing the
  reference project) — and legitimately name the project the code is being
  extracted from. They are not part of the maestro deliverable. Everything
  else, including all of ``maestro/``, ``tests/`` and ``templates/``, stays strict,
  so a new file is always caught.

A third check, ``test_no_pointers_into_the_build_scratch_directory``, is about rot
rather than identity: ``EPHEMERAL_PATH_PATTERNS``. Maestro is built by short-lived
plan directories under ``/home/dan/.maestro-sdd/``, each holding per-item briefs and
reports, and each **deleted when its plan closes**. A citation of one from
``maestro/`` or ``tests/`` is a pointer that is guaranteed to rot — the 2026-08-30
pre-integration queue shipped six of them, in production and test source, two
carrying evidence found nowhere else in the tree. Content worth citing gets copied
into ``docs/plans/`` (which is where the surviving two now live) and cited there.
The scaffold exemption applies here too: ``handoffs/`` and ``docs/plans/`` are
allowed to name the directory, because a handoff is written *while* it exists and a
copied report has to say where it came from.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Case-insensitive. Extend only with genuinely project-identifying terms.
PROJECT_PATTERNS = [
    r"abuali",
    r"abu[_ -]?ali",
    r"@\w*DevBot",
]

CREDENTIAL_PATTERNS = [
    r"\d{9,}:[A-Za-z0-9_-]{30,}",  # Telegram bot token shape
]

LEGACY_PATH = "/home/dan/projects/AbuAli"

#: Paths that exist only while a build plan is open and are deleted when it closes.
#: Anything outside the scaffold that cites one is a pointer with an expiry date.
EPHEMERAL_PATH_PATTERNS = [
    # A path *into* the scratch root, not the root itself. The root is persistent and is
    # named as a live operational rule in `tasks/lessons.md` ("uncommitted work goes on
    # persistent storage, outside the repo"); what expires is each plan directory under
    # it. The character class deliberately excludes the closing backtick, so
    # ``` `/home/dan/.maestro-sdd/` ``` reads as the root and
    # `/home/dan/.maestro-sdd/2026-08-30-pre-integration/` does not.
    r"/home/dan/\.maestro-sdd/[\w.-]",
    # A per-item brief or report by bare filename — `task-C7-report.md`. These only ever
    # live in the scratch directory above, so a bare reference is the same rot without
    # the leading path to make it obvious.
    r"\btask-[A-Z]+\d*-(?:report|brief)\.md\b",
]

SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".run", "node_modules"}

SCAN_SUFFIXES = {".py", ".toml", ".md", ".yaml", ".yml", ".sh", ".json"}

# Build scaffolding — exempt from the project-name tier only, never from the
# credential tier. Paths are repo-relative POSIX.
BUILD_SCAFFOLD = {
    "docs/DESIGN.md",
    "docs/EXECUTION.md",
    "docs/PROGRESS.md",
    "docs/FOUND_BUGS.md",
    "scripts/run_overnight.sh",
    "scripts/maestro-build-launcher.sh",
    "scripts/resume_eval_when_done.sh",
    # M5 cutover-and-rollback tooling. Hardcodes the reference project's path and
    # name deliberately — it operates on exactly one project, by design, and is
    # never installed into a consuming project by `maestro init`. Same category
    # as the two scripts above: it drives *this build*, not the maestro product.
    "scripts/m5-rollback.sh",
    "scripts/m5-install-unit.sh",
    # The design spec for those two scripts. Same category as the scripts themselves:
    # it describes the machinery that *builds* maestro — including how that machinery
    # must leave the frozen reference project alone — and ships with none of it.
    "docs/superpowers/specs/2026-08-14-maestro-build-watchdog-design.md",
    "tests/test_purity.py",
}


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _scanned_files():
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix not in SCAN_SUFFIXES:
            continue
        yield p


def _is_scaffold(path: Path) -> bool:
    rel = _rel(path)
    return (
        rel in BUILD_SCAFFOLD
        or rel.startswith("docs/plans/")
        or rel.startswith("docs/superpowers/plans/")
        or rel.startswith("handoffs/")
    )


def _violations(paths, patterns):
    out = []
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in patterns:
            for m in re.finditer(pattern, text, re.IGNORECASE):
                line = text[: m.start()].count("\n") + 1
                out.append(f"{_rel(path)}:{line}: {m.group(0)!r}")
    return out


def test_no_credentials_anywhere():
    violations = _violations(_scanned_files(), CREDENTIAL_PATTERNS)
    assert not violations, "credential-shaped content found:\n" + "\n".join(violations)


def test_no_project_identifying_strings():
    paths = [p for p in _scanned_files() if not _is_scaffold(p)]
    violations = _violations(paths, PROJECT_PATTERNS)
    assert not violations, "project-identifying content found:\n" + "\n".join(violations)


def test_no_hardcoded_legacy_repo_path():
    violations = []
    for path in _scanned_files():
        if _is_scaffold(path):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if LEGACY_PATH in text:
            violations.append(_rel(path))
    assert not violations, f"hardcoded legacy path in: {violations}"


def test_no_pointers_into_the_build_scratch_directory():
    """Nothing shipped may cite a file that is deleted when its build plan closes.

    Six of these shipped in the 2026-08-30 pre-integration queue — `maestro/limits.py`,
    `maestro/cli.py`, `tests/test_cli.py` and `tests/test_no_dead_backend_surface.py`
    (three times) — all naming `task-C7-report.md` / `task-D2-report.md`, which existed
    only under `/home/dan/.maestro-sdd/2026-08-30-pre-integration/`. Two carried the sole
    record of a hand-run experiment. The fix was to copy those two reports into
    `docs/plans/` and cite them there; this stops the sixth-time-lucky version.

    Scaffold-exempt by the same rule as the tier above: `handoffs/` is written while the
    directory exists and `docs/plans/` copies have to state where they came from.
    """
    paths = [p for p in _scanned_files() if not _is_scaffold(p)]
    violations = _violations(paths, EPHEMERAL_PATH_PATTERNS)
    assert not violations, (
        "pointer into a build scratch directory that will be deleted:\n"
        + "\n".join(violations)
        + "\n\nCopy what is worth keeping into docs/plans/ and cite it there."
    )


def test_the_ephemeral_pointer_patterns_bite_the_six_shapes_that_shipped():
    """The gate above, proven against the exact citations it was written for, and
    against the two shapes it must NOT bite.

    `tests/test_purity.py` is itself scaffold-exempt (`BUILD_SCAFFOLD`), which is what
    lets this file spell the patterns' own subject matter in plain sight — the same
    self-exemption `tests/test_no_reference_project_strings.py` grants itself.
    """
    import re

    bites = [
        "see `task-C7-report.md`",
        "reported finding (task-D2-report.md), not a synthetic case",
        "See `task-D2-report.md`'s fix-round-1 entry",
        "`/home/dan/.maestro-sdd/2026-08-30-pre-integration/progress.md`",
        "git worktree add /home/dan/.maestro-sdd/wt-C7",
        "`task-A5-brief.md`",
    ]
    for text in bites:
        assert any(re.search(pat, text, re.IGNORECASE)
                   for pat in EPHEMERAL_PATH_PATTERNS), text

    survives = [
        # The persistent root as an operational rule — `tasks/lessons.md`'s two mentions.
        "The workspace now lives at `/home/dan/.maestro-sdd/`.",
        # A citation that was repointed at the durable copy.
        "see `docs/plans/2026-08-30-pre-integration-c7-report.md`",
    ]
    for text in survives:
        assert not any(re.search(pat, text, re.IGNORECASE)
                       for pat in EPHEMERAL_PATH_PATTERNS), text
