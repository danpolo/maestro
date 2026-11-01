"""Safe repository fact discovery for `maestro init` (`[P5]`, `D03`).

`D03` says `init` "deterministically derives a project profile from immutable repository
facts". This module is the derivation, and the word doing the work is *immutable*: every
fact below comes from reading a tracked manifest, and each one is recorded with the path
it came from, the SHA256 of that file's bytes, and how strong the evidence was.

**The negative invariant is the design.** Discovery MUST NOT execute arbitrary project
code — no `npm run`, no `make -n`, no importing the project's `conftest.py` to see what it
configures, not even a `git` subprocess. Onboarding a repository is exactly the moment
Maestro knows least about what that repository's build scripts do, so this module opens
files and parses them and does nothing else. `tests/test_init_workflows.py` enforces that
by making every spelling of "run something" raise for the duration of a discovery.

**Ambiguity is recorded, never guessed.** Two equally authoritative manifests declaring
different test commands does not produce a coin flip; it produces an entry in
`unresolved[]` naming both candidates. Downstream that fails closed — a project whose test
command is unresolved has no verification gate to compile, which is the correct outcome
(`[INV-04]`) and a much better failure than a gate that silently runs the wrong suite.
`unresolved[]` is a work list for the operator interview, not an error.
"""
from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

#: Evidence tiers, strongest first. A key is resolved when its strongest tier holds
#: exactly one distinct candidate; when that tier disagrees with itself, it is unresolved.
TIER_MANIFEST = 3      # the project declares it: package.json scripts, a Makefile target
TIER_CI = 2            # a CI workflow observably runs it
TIER_CONVENTION = 1    # the layout implies it: a tests/ directory, a go.mod

CONFIDENCE_BY_TIER = {TIER_MANIFEST: "high", TIER_CI: "medium", TIER_CONVENTION: "low"}

#: Read caps. A manifest is a small file; anything larger is either generated or hostile,
#: and either way is not evidence worth blocking `init` on.
MAX_MANIFEST_BYTES = 512 * 1024
MAX_CI_WORKFLOWS = 20

_MAKE_TARGET_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*:(?!=)", re.MULTILINE)


class DiscoveryError(RuntimeError):
    """Discovery could not read the repository at all. Not raised for missing manifests."""


@dataclass(frozen=True)
class Fact:
    """One derived project fact, with the evidence that produced it."""

    key: str
    value: Any
    source_path: str
    source_sha256: str
    confidence: str
    detector: str

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "value": self.value,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "confidence": self.confidence,
            "detector": self.detector,
        }


@dataclass(frozen=True)
class Unresolved:
    """A fact discovery refused to guess at. Fails closed downstream (`A06`)."""

    key: str
    reason: str
    candidates: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "reason": self.reason,
            "candidates": list(self.candidates),
            "sources": list(self.sources),
        }


@dataclass(frozen=True)
class ProjectProfile:
    """The immutable-facts view of one repository, as of one `init`."""

    repo_path: str
    facts: tuple[Fact, ...] = ()
    unresolved: tuple[Unresolved, ...] = ()

    def value(self, key: str, default: Any = None) -> Any:
        for fact in self.facts:
            if fact.key == key:
                return fact.value
        return default

    def fact(self, key: str) -> Fact | None:
        for candidate in self.facts:
            if candidate.key == key:
                return candidate
        return None

    def is_resolved(self, key: str) -> bool:
        return self.fact(key) is not None

    @property
    def unresolved_keys(self) -> tuple[str, ...]:
        return tuple(u.key for u in self.unresolved)

    def to_dict(self) -> dict:
        """Canonical form. `repo_path` is excluded from the hash below, not from here."""
        return {
            "repo_path": self.repo_path,
            "facts": [f.to_dict() for f in self.facts],
            "unresolved": [u.to_dict() for u in self.unresolved],
        }

    @property
    def profile_hash(self) -> str:
        """SHA256 of the facts, path-independent.

        The same project checked out at two paths has the same profile: a path is where
        the repository is, not what it is, and a hash that moved with the checkout would
        invalidate every compiled workflow the first time somebody cloned it elsewhere.
        """
        body = json.dumps(
            {
                "facts": [f.to_dict() for f in self.facts],
                "unresolved": [u.to_dict() for u in self.unresolved],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass
class _Candidate:
    key: str
    value: Any
    tier: int
    source_path: str
    source_sha256: str
    detector: str


@dataclass
class _Evidence:
    """Accumulator. Deliberately not a dict of values — collisions are the interesting case."""

    candidates: list[_Candidate] = field(default_factory=list)

    def add(self, **kwargs) -> None:
        self.candidates.append(_Candidate(**kwargs))

    def resolve(self) -> tuple[tuple[Fact, ...], tuple[Unresolved, ...]]:
        facts: list[Fact] = []
        unresolved: list[Unresolved] = []
        for key in sorted({c.key for c in self.candidates}):
            for_key = [c for c in self.candidates if c.key == key]
            top = max(c.tier for c in for_key)
            best = [c for c in for_key if c.tier == top]
            distinct = sorted({json.dumps(c.value, sort_keys=True) for c in best})
            if len(distinct) == 1:
                winner = best[0]
                facts.append(
                    Fact(
                        key=key,
                        value=winner.value,
                        source_path=winner.source_path,
                        source_sha256=winner.source_sha256,
                        confidence=CONFIDENCE_BY_TIER[top],
                        detector=winner.detector,
                    )
                )
            else:
                unresolved.append(
                    Unresolved(
                        key=key,
                        reason=(
                            f"{len(distinct)} equally authoritative candidates disagree; "
                            f"discovery does not guess"
                        ),
                        candidates=tuple(_render(c.value) for c in best),
                        sources=tuple(sorted({c.source_path for c in best})),
                    )
                )
        return tuple(facts), tuple(unresolved)


# ── the entry point ──────────────────────────────────────────────────────────────────

#: Keys discovery always reports on: resolved with evidence, or listed in `unresolved[]`
#: with the reason. A key that is simply absent from both would be indistinguishable from
#: a detector that silently crashed.
EXPECTED_KEYS = ("package_manager", "test_command", "lint_command", "build_command")


def discover(repo_root: Path | str) -> ProjectProfile:
    """Derive the project profile by reading tracked manifests. Executes nothing."""
    root = Path(repo_root)
    if not root.is_dir():
        raise DiscoveryError(f"{root} is not a directory")

    evidence = _Evidence()
    _detect_python(root, evidence)
    _detect_node(root, evidence)
    _detect_go(root, evidence)
    _detect_make(root, evidence)
    _detect_ci(root, evidence)

    facts, unresolved = evidence.resolve()
    resolved_keys = {f.key for f in facts} | {u.key for u in unresolved}
    missing = tuple(
        Unresolved(key=key, reason="no evidence found in any tracked manifest")
        for key in EXPECTED_KEYS
        if key not in resolved_keys
    )
    return ProjectProfile(
        repo_path=str(root),
        facts=facts,
        unresolved=tuple(sorted((*unresolved, *missing), key=lambda u: u.key)),
    )


# ── detectors ────────────────────────────────────────────────────────────────────────


def _detect_python(root: Path, evidence: _Evidence) -> None:
    pyproject, pyproject_sha = _read(root / "pyproject.toml")
    requirements, requirements_sha = _read(root / "requirements.txt")
    if pyproject is None and requirements is None:
        return
    source = "pyproject.toml" if pyproject is not None else "requirements.txt"
    sha = pyproject_sha if pyproject is not None else requirements_sha
    evidence.add(
        key="package_manager", value="pip", tier=TIER_CONVENTION,
        source_path=source, source_sha256=sha, detector="python_manifest",
    )

    declared_pytest = False
    if pyproject is not None:
        try:
            document = tomllib.loads(pyproject)
        except tomllib.TOMLDecodeError:
            document = {}
        tool = document.get("tool") if isinstance(document, dict) else None
        declared_pytest = isinstance(tool, dict) and "pytest" in tool
        project = document.get("project") if isinstance(document, dict) else None
        if isinstance(project, dict):
            dependency_text = json.dumps(
                [project.get("dependencies", []), project.get("optional-dependencies", {})]
            ).lower()
            declared_pytest = declared_pytest or "pytest" in dependency_text
    if requirements is not None and "pytest" in requirements.lower():
        declared_pytest = True

    if declared_pytest:
        evidence.add(
            key="test_command", value=_pytest_argv(root), tier=TIER_MANIFEST,
            source_path=source, source_sha256=sha, detector="python_pytest_declared",
        )
    elif (root / "tests").is_dir():
        evidence.add(
            key="test_command", value=_pytest_argv(root), tier=TIER_CONVENTION,
            source_path="tests/", source_sha256="", detector="python_tests_directory",
        )


def _pytest_argv(root: Path) -> list[str]:
    """The interpreter `init` scaffolds, not whichever one happens to be on PATH."""
    return [str((root / ".venv" / "bin" / "python3")), "-m", "pytest", "-q"]


def _detect_node(root: Path, evidence: _Evidence) -> None:
    text, sha = _read(root / "package.json")
    if text is None:
        return
    evidence.add(
        key="package_manager", value="npm", tier=TIER_CONVENTION,
        source_path="package.json", source_sha256=sha, detector="node_manifest",
    )
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return
    scripts = document.get("scripts") if isinstance(document, dict) else None
    if not isinstance(scripts, dict):
        return
    for key, script in (
        ("test_command", "test"), ("lint_command", "lint"), ("build_command", "build")
    ):
        if isinstance(scripts.get(script), str) and scripts[script].strip():
            argv = ["npm", "test"] if script == "test" else ["npm", "run", script]
            evidence.add(
                key=key, value=argv, tier=TIER_MANIFEST,
                source_path="package.json", source_sha256=sha,
                detector=f"node_script_{script}",
            )


def _detect_go(root: Path, evidence: _Evidence) -> None:
    text, sha = _read(root / "go.mod")
    if text is None:
        return
    evidence.add(
        key="package_manager", value="go", tier=TIER_CONVENTION,
        source_path="go.mod", source_sha256=sha, detector="go_manifest",
    )
    evidence.add(
        key="test_command", value=["go", "test", "./..."], tier=TIER_CONVENTION,
        source_path="go.mod", source_sha256=sha, detector="go_convention",
    )


def _detect_make(root: Path, evidence: _Evidence) -> None:
    """Makefile targets, by name only.

    A target's *recipe* is never read and never expanded: `make test` is the fact, and
    what that target then does is the project's business at run time, not a string this
    module should be interpreting.
    """
    text, sha = _read(root / "Makefile")
    if text is None:
        return
    targets = set(_MAKE_TARGET_RE.findall(text))
    for key, target in (
        ("test_command", "test"), ("lint_command", "lint"), ("build_command", "build")
    ):
        if target in targets:
            evidence.add(
                key=key, value=["make", target], tier=TIER_MANIFEST,
                source_path="Makefile", source_sha256=sha, detector=f"make_target_{target}",
            )


def _detect_ci(root: Path, evidence: _Evidence) -> None:
    """What CI observably runs, read as YAML — `run:` strings, never executed."""
    workflows = root / ".github" / "workflows"
    if not workflows.is_dir():
        return
    paths = sorted(
        p for p in workflows.iterdir()
        if p.is_file() and p.suffix.lower() in (".yml", ".yaml")
    )[:MAX_CI_WORKFLOWS]
    for path in paths:
        text, sha = _read(path)
        if text is None:
            continue
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError:
            continue
        relative = str(path.relative_to(root))
        for command in _run_steps(document):
            lowered = command.lower()
            for key, needles in (
                ("test_command", ("pytest", "npm test", "go test", "make test")),
                ("lint_command", ("ruff", "eslint", "flake8", "make lint")),
                ("build_command", ("npm run build", "make build", "go build")),
            ):
                if any(needle in lowered for needle in needles):
                    evidence.add(
                        key=key, value=command.split(), tier=TIER_CI,
                        source_path=relative, source_sha256=sha, detector="ci_run_step",
                    )
                    break


def _run_steps(node: Any) -> Iterable[str]:
    """Every `run:` scalar in a workflow document, at any depth. Single-line steps only."""
    if isinstance(node, dict):
        run = node.get("run")
        if isinstance(run, str) and run.strip() and "\n" not in run.strip():
            yield run.strip()
        for value in node.values():
            yield from _run_steps(value)
    elif isinstance(node, list):
        for value in node:
            yield from _run_steps(value)


# ── reading ──────────────────────────────────────────────────────────────────────────


def _read(path: Path) -> tuple[str | None, str]:
    """`(text, sha256)` for a manifest, `(None, "")` when it is absent, huge or unreadable."""
    try:
        if not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
            return None, ""
        data = path.read_bytes()
    except OSError:
        return None, ""
    try:
        return data.decode("utf-8"), hashlib.sha256(data).hexdigest()
    except UnicodeDecodeError:
        return None, ""


def _render(value: Any) -> str:
    return " ".join(str(v) for v in value) if isinstance(value, (list, tuple)) else str(value)
