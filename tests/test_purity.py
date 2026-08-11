"""Maestro must contain nothing that identifies any consuming project.

Two tiers:

* ``CREDENTIAL_PATTERNS`` are scanned in **every** scanned file with no exemptions.
  A leaked token is a defect wherever it appears.
* ``PROJECT_PATTERNS`` and the legacy-path check are scanned everywhere except the
  explicitly named build scaffolding below, plus ``docs/plans/`` and ``handoffs/``.
  Those files document *this build* — the design, the process, the live progress
  log, the overnight driver, and session handoffs (including operator directives
  like pausing the reference project) — and legitimately name the project the code
  is being extracted from. They are not part of the maestro deliverable. Everything
  else, including all of ``maestro/``, ``tests/`` and ``templates/``, stays strict,
  so a new file is always caught.
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
