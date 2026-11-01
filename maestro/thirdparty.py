"""The third-party verification gate — B06 and B17 made mechanical.

Two of the graph-engineering blind spots
(`GRAPH_ENGINEERING_RESEARCH_BLIND_SPOTS.md`) are not research questions and were
deliberately not answered in either the local-experiment pass or the targeted research
pass:

* **B06 — backend capabilities.** Which models, effort controls, resume/cancel behaviour,
  instruction delivery and usage telemetry an agent CLI actually has.
* **B17 — free-use guarantees.** Which licence and terms an optional dependency actually
  ships under, and whether normal Maestro operation stays free.

Both were separated out because an answer written today is a fact about *one exact
version* and expires silently. The local audit demonstrated that within a single research
window: the supplied research described `codex` as needing a PTY collector for quota while
the installed 0.153.4 already returns both windows structurally, described an Antigravity
`/usage` surface the installed 1.1.27 does not have, and graded Claude's rate-limit feed
"high confidence" where the installed 2.1.263 returned a null reset epoch. Every one of
those is a vendor-documentation claim that the installed software had already left behind.

So this module does **not** answer B06 or B17. It makes their unanswered state *visible
and blocking at the moment of adoption*, which is the only moment the answer is worth
having. It closes three specific ways the deferral goes wrong:

1. A backend driver or a Python dependency lands in the tree and nobody ever verifies it.
   Caught by the coverage invariants in `tests/test_thirdparty.py`, from the repository
   alone, with no host access.
2. A verified fact silently expires when the installed version moves underneath it.
   Caught by `status_for`, surfaced by `maestro doctor --thirdparty`.
3. Something is called "supported" or "free" on the strength of a claim nobody pinned to
   an observed version. Caught by `parse_register`: a row that names a `verified_version`
   must name its evidence, a dependency row must say which licence and *where that licence
   was read from*, and a backend row must list the controls actually observed.

**What this is not.** It is a verification ledger, not the runtime capability catalog. It
records what was checked, when, against which version, with what evidence. Routing does
not read it, `Capabilities` is not derived from it, and nothing here decides what a driver
may do — P6's `backends/catalog.py` is where that belongs when it is built, and it should
be *populated from* verified rows rather than duplicating them.

**Why this raises where `maestro.limits` tolerates.** `limits.py` parses an operator's
file on an arbitrary machine and degrades to defaults, because a missing file there is a
normal state of the world. This register ships inside the package: an unreadable or
self-contradictory one is a defect in *this repository*, and the failure mode of degrading
to "nothing to check" is a gate that silently passes everything — precisely the outcome it
exists to prevent. So parsing raises and the shipped file is covered by tests.

This module imports nothing outside the standard library and touches no repository state
at import time.
"""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

__all__ = [
    "REGISTER_JSON",
    "KINDS",
    "SCOPES",
    "STATUSES",
    "Verification",
    "SubjectStatus",
    "canonical_name",
    "parse_register",
    "load_register",
    "declared_dependencies",
    "installed_binary_version",
    "installed_distribution_version",
    "status_for",
    "audit",
    "blocking",
]

#: The register itself. Beside this module, resolved from `__file__` for the same reason
#: `cli.TEMPLATES_DIR` is: the package must find its own data whether it was imported from
#: this checkout, from an editable install, or from a copy adopted by self-update.
REGISTER_JSON = Path(__file__).resolve().parent / "thirdparty.json"

#: What kind of third-party thing a row is about. `backend` rows describe an agent CLI
#: resolved through `maestro.backends.registry`; `dependency` rows describe a Python
#: distribution declared in `pyproject.toml`. The two carry different mandatory fields
#: because they answer different blind spots.
KINDS = frozenset({"backend", "dependency"})

#: Where the subject sits relative to Maestro's free-use boundary. `runtime` is needed for
#: ordinary operation and therefore carries the "stays free" claim; `dev` is needed only to
#: develop or test Maestro; `optional` is behind an extra and must be verified before it is
#: enabled, never after.
SCOPES = frozenset({"runtime", "dev", "optional"})

#: The five outcomes, in worsening order of what they permit.
#:
#: * `verified` — the installed version is exactly the one the row was verified against.
#: * `absent`   — nothing of that name is installed here. A row is a fact about software,
#:                not a requirement that this machine have it, so this does not block.
#: * `candidate` — tracked but never verified, and neither declared nor installed. This is
#:                how a shortlisted provider is carried *before* anyone adopts it, so that
#:                the gate already holds it the moment someone does.
#: * `reobserved` — a backend CLI installed at a different version than was verified, whose
#:                `--help` still shows every control the row recorded. Agent CLIs auto-update
#:                constantly; a version bump that changed nothing maestro relies on is
#:                reported, not blocking. The row is not rewritten — it still names the
#:                version the deeper verification was done against.
#: * `drifted`  — installed, but at a different version than was verified, and not
#:                re-observed (a dependency, a recorded control that vanished, or a `--help`
#:                that could not be read). The row's facts are now claims about software that
#:                is no longer here.
#: * `unverified` — no verification behind it at all.
STATUSES = ("verified", "absent", "candidate", "reobserved", "drifted", "unverified")

#: The statuses that stop an integration being called complete — *when the subject is
#: actually in use*. Drift in something Maestro does not drive is worth reporting and not
#: worth blocking on; the moment a driver or a `pyproject.toml` entry puts it in use, the
#: same drift blocks. See `SubjectStatus.blocking`.
_BLOCKING = frozenset({"drifted", "unverified"})

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: PEP 503 name normalisation. `PyYAML`, `pyyaml` and `py_yaml` are one distribution, and a
#: register keyed on the spelling that happened to appear in `pyproject.toml` would miss a
#: row added under another.
_NAME_SEPARATORS = re.compile(r"[-_.]+")

#: Enough of PEP 508 to read a name and its extra marker off a requirement string. The
#: declared requirements here are plain `name>=version` entries; anything richer (a URL
#: requirement, an environment marker beyond `extra ==`) still yields the correct *name*,
#: which is all this gate keys on.
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def canonical_name(name: str) -> str:
    """The PEP 503 normalised form of a distribution or subject name."""
    return _NAME_SEPARATORS.sub("-", str(name or "").strip()).lower()


@dataclass(frozen=True)
class Verification:
    """One verified third-party subject.

    `verified_version` is the exact version string as the tool or distribution reported
    it — `"2.1.263 (Claude Code)"`, not a parsed `2.1.263`. Comparison is raw equality on
    purpose: any change in what the software says about itself is a change worth
    re-checking, and a version parser is one more thing that can silently agree two
    different builds are the same. An empty string means "carried as a candidate, never
    verified".

    `findings` is an open mapping, kind-specific by convention rather than by schema,
    because what is worth recording about a CLI is exactly what the next implementer will
    otherwise have to rediscover, and pinning that shape now would guarantee the useful
    field is the one the schema forbids. Only `controls_observed` is mandatory for a
    backend row, and only because a row that does not say what it looked at is not a
    verification.
    """

    subject: str
    kind: str
    scope: str
    verified_version: str
    verified_on: str
    verified_by: str
    evidence: tuple[str, ...]
    findings: Mapping[str, Any]
    open_questions: tuple[str, ...]
    free_use: Optional[Mapping[str, Any]]
    notes: str


@dataclass(frozen=True)
class SubjectStatus:
    """What this host says about one register row (or about a row that is missing).

    `in_use` is what separates "must be fixed" from "worth knowing": a subject is in use
    when a driver is registered for it or `pyproject.toml` declares it. A shortlisted
    provider nobody has adopted, or a CLI on the host that Maestro does not drive, is
    reported at whatever status it has and blocks nothing.
    """

    subject: str
    kind: str
    status: str
    verified_version: str
    installed_version: Optional[str]
    detail: str
    in_use: bool = True

    @property
    def blocking(self) -> bool:
        """True when this subject may not be called supported until someone acts."""
        return self.in_use and self.status in _BLOCKING


# ── register parsing ──


def _require_text(row: Mapping[str, Any], field: str, subject: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{subject}: {field} must be a non-empty string")
    return value.strip()


def _require_str_list(row: Mapping[str, Any], field: str, subject: str) -> tuple[str, ...]:
    value = row.get(field, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{subject}: {field} must be a list of strings")
    return tuple(value)


def parse_register(document: Any) -> dict[str, Verification]:
    """Validate a register document and return it keyed by canonical subject name.

    Raises `ValueError` on anything a reviewer would have to catch by eye: an unsupported
    schema, a duplicate subject, a claim with no evidence behind it, a dependency with no
    stated licence source, a backend row that does not say what it observed.
    """
    if not isinstance(document, Mapping) or document.get("schema_version") != 1:
        raise ValueError("unsupported third-party register schema (expected schema_version 1)")
    rows = document.get("records")
    if not isinstance(rows, list):
        raise ValueError("register needs a records list")

    register: dict[str, Verification] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("every register record must be an object")
        subject = canonical_name(_require_text(row, "subject", "<record>"))
        if subject in register:
            raise ValueError(f"duplicate subject {subject!r} in the register")

        kind = _require_text(row, "kind", subject)
        if kind not in KINDS:
            raise ValueError(f"{subject}: kind must be one of {sorted(KINDS)}")
        scope = _require_text(row, "scope", subject)
        if scope not in SCOPES:
            raise ValueError(f"{subject}: scope must be one of {sorted(SCOPES)}")

        verified_on = _require_text(row, "verified_on", subject)
        if not _DATE_RE.match(verified_on):
            raise ValueError(f"{subject}: verified_on must be an ISO date (YYYY-MM-DD)")
        try:
            date.fromisoformat(verified_on)
        except ValueError as exc:
            raise ValueError(f"{subject}: verified_on is not a real date") from exc
        _require_text(row, "verified_by", subject)

        version = row.get("verified_version")
        if not isinstance(version, str):
            raise ValueError(f"{subject}: verified_version must be a string ('' if never verified)")
        version = version.strip()
        evidence = _require_str_list(row, "evidence", subject)
        # The core schema rule. A row that names a version it checked must name where the
        # observation is retained; otherwise the register would accumulate assertions that
        # read exactly like measurements. A never-verified placeholder is exempt because
        # it asserts nothing.
        if version and not evidence:
            raise ValueError(
                f"{subject}: a row claiming verified_version {version!r} must cite its evidence"
            )

        findings = row.get("findings", {})
        if not isinstance(findings, Mapping):
            raise ValueError(f"{subject}: findings must be an object")
        free_use = row.get("free_use")
        if free_use is not None and not isinstance(free_use, Mapping):
            raise ValueError(f"{subject}: free_use must be an object or null")

        if kind == "dependency":
            # B17's whole content: a licence claim is worth nothing without the exact
            # version it was read against and the place it was read from.
            if free_use is None:
                raise ValueError(f"{subject}: a dependency row must carry a free_use block")
            if not str(free_use.get("license") or "").strip():
                raise ValueError(f"{subject}: free_use needs a licence identifier")
            if not str(free_use.get("source") or "").strip():
                raise ValueError(f"{subject}: free_use needs a source for that licence")
            if not isinstance(free_use.get("no_paid_tier_for_normal_operation"), bool):
                raise ValueError(
                    f"{subject}: free_use must state no_paid_tier_for_normal_operation as a boolean"
                )
        elif version and not isinstance(findings.get("controls_observed"), list):
            # B06's whole content: never invent a flag, never map an effort label across
            # vendors. A row that does not list what it actually saw cannot support either
            # rule, so the omission is refused rather than defaulted.
            raise ValueError(
                f"{subject}: a verified backend row must list findings.controls_observed"
            )

        register[subject] = Verification(
            subject=subject,
            kind=kind,
            scope=scope,
            verified_version=version,
            verified_on=verified_on,
            verified_by=str(row.get("verified_by") or "").strip(),
            evidence=evidence,
            findings=dict(findings),
            open_questions=_require_str_list(row, "open_questions", subject),
            free_use=dict(free_use) if isinstance(free_use, Mapping) else None,
            notes=str(row.get("notes") or ""),
        )
    return register


def load_register(path: Optional[Path] = None) -> dict[str, Verification]:
    """Read and validate the shipped register (or `path`)."""
    target = Path(path) if path is not None else REGISTER_JSON
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"third-party register missing at {target}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"third-party register at {target} is not valid JSON: {exc}") from exc
    return parse_register(document)


# ── what is in use, and therefore must be covered ──


def declared_dependencies(pyproject: Optional[Path] = None) -> dict[str, str]:
    """Every Python distribution Maestro declares, as `{canonical name: scope}`.

    Read from `pyproject.toml` rather than from installed metadata on purpose. The
    installed `maestro.egg-info` was observed stale on 2026-09-08 —
    `importlib.metadata.requires("maestro")` omitted `pytest-xdist`, which
    `pyproject.toml` had declared — and a coverage gate sourced from a stale list has a
    hole exactly where a newly added dependency is. `pyproject.toml` is the file a
    dependency is added to, so it is the file the gate reads.

    `dev` extras keep their own scope rather than being dropped: they are still
    third-party code the operator installs, and B17's "normal operation stays free" claim
    is only meaningful if the runtime/dev boundary is written down rather than assumed.
    """
    target = Path(pyproject) if pyproject is not None else Path(__file__).resolve().parents[1] / "pyproject.toml"
    document = tomllib.loads(target.read_text(encoding="utf-8"))
    project = document.get("project") or {}

    declared: dict[str, str] = {}
    for requirement in project.get("dependencies") or []:
        match = _REQUIREMENT_NAME.match(str(requirement))
        if match:
            declared[canonical_name(match.group(1))] = "runtime"
    for extra, requirements in (project.get("optional-dependencies") or {}).items():
        for requirement in requirements or []:
            match = _REQUIREMENT_NAME.match(str(requirement))
            if match:
                # A distribution in both places is a runtime dependency; the runtime claim
                # is the stronger one and must not be relaxed by an extra that repeats it.
                declared.setdefault(canonical_name(match.group(1)), str(extra))
    return declared


def required_subjects(pyproject: Optional[Path] = None) -> dict[str, str]:
    """Every subject that must have a register row, as `{subject: kind}`.

    Backends come from `maestro.backends.registry.BACKENDS` so a driver added later is
    covered the moment it is registered, exactly as `tests/conftest.py` and
    `tests/test_no_name_branching.py` already source their own gates.
    """
    from maestro.backends.registry import BACKENDS

    # Keyed on the binary, not the registry name: the register records the CLI that was
    # verified (`agy`), which is not always the backend's name (`antigravity`).
    required = {canonical_name(ref.binary): "backend" for ref in BACKENDS.values()}
    for name in declared_dependencies(pyproject):
        required.setdefault(name, "dependency")
    return required


# ── host probes ──


def installed_binary_version(subject: str) -> Optional[str]:
    """The version string of the CLI named `subject`, or `None` when it is not on PATH.

    Delegates to `registry.resolve_binary`, which already pins one absolute path per tool
    and caches its `--version`, so the gate adds no new way to discover a binary and
    inherits the existing shadowed-install warning rather than restating it.
    """
    from maestro.backends import registry

    info = registry.resolve_binary(subject)
    if info is None:
        return None
    return info.version or None


def installed_binary_help(subject: str) -> Optional[str]:
    """`<subject> --help` output (stdout + stderr) from the pinned binary, or `None`.

    No agent turn and no quota: this is the same inspection the verification rows record
    under `controls_observed`, so a version bump can be re-checked against them.
    """
    import subprocess

    from maestro.backends import registry

    info = registry.resolve_binary(subject)
    if info is None:
        return None
    try:
        result = subprocess.run(
            [str(info.path), "--help"], capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (result.stdout or "") + (result.stderr or "")
    return text if text.strip() else None


def installed_distribution_version(subject: str) -> Optional[str]:
    """The installed version of the distribution named `subject`, or `None`."""
    from importlib import metadata

    try:
        return metadata.version(subject)
    except metadata.PackageNotFoundError:
        return None


# ── status ──


def status_for(
    record: Verification,
    installed: Optional[str],
    *,
    in_use: bool = True,
    help_text: Optional[str] = None,
) -> SubjectStatus:
    """Compare one row against what is installed. See `SubjectStatus.in_use`.

    `help_text` is the installed backend's `--help`, consulted only when its version moved:
    if every recorded control is still present the status is `reobserved`, otherwise
    `drifted` naming what vanished.
    """
    if not record.verified_version:
        tracked_only = not in_use and installed is None
        return SubjectStatus(
            subject=record.subject, kind=record.kind,
            status="candidate" if tracked_only else "unverified",
            verified_version="", installed_version=installed, in_use=in_use,
            detail=(
                "tracked as a candidate; verify against the exact installed version "
                "before enabling it"
                if tracked_only else
                "never verified"
                + (f"; installed here at {installed}" if installed else "")
            ),
        )
    if installed is None:
        return SubjectStatus(
            subject=record.subject, kind=record.kind, status="absent",
            verified_version=record.verified_version, installed_version=None, in_use=in_use,
            detail=f"not installed on this host; row verified against {record.verified_version}",
        )
    if installed == record.verified_version:
        return SubjectStatus(
            subject=record.subject, kind=record.kind, status="verified",
            verified_version=record.verified_version, installed_version=installed, in_use=in_use,
            detail=f"{installed} verified {record.verified_on} by {record.verified_by}",
        )
    controls = tuple(record.findings.get("controls_observed") or ())
    if record.kind == "backend" and controls and help_text:
        missing = [control for control in controls if control not in help_text]
        if not missing:
            return SubjectStatus(
                subject=record.subject, kind=record.kind, status="reobserved",
                verified_version=record.verified_version, installed_version=installed,
                in_use=in_use,
                detail=(
                    f"{installed} installed (row verified {record.verified_version}); "
                    f"--help still shows all {len(controls)} recorded controls"
                ),
            )
        return SubjectStatus(
            subject=record.subject, kind=record.kind, status="drifted",
            verified_version=record.verified_version, installed_version=installed, in_use=in_use,
            detail=(
                f"verified against {record.verified_version} on {record.verified_on}, "
                f"but {installed} is installed and its --help no longer shows "
                f"{', '.join(missing)} — re-verify before calling it supported"
            ),
        )
    return SubjectStatus(
        subject=record.subject, kind=record.kind, status="drifted",
        verified_version=record.verified_version, installed_version=installed, in_use=in_use,
        detail=(
            f"verified against {record.verified_version} on {record.verified_on}, "
            f"but {installed} is installed — re-verify before calling it supported"
        ),
    )


def audit(
    *,
    register: Optional[Mapping[str, Verification]] = None,
    required: Optional[Mapping[str, str]] = None,
    binary_version: Optional[Callable[[str], Optional[str]]] = None,
    distribution_version: Optional[Callable[[str], Optional[str]]] = None,
    binary_help: Optional[Callable[[str], Optional[str]]] = None,
    pyproject: Optional[Path] = None,
) -> list[SubjectStatus]:
    """Status for every subject in use, plus every extra row the register carries.

    Every probe is injectable so a test can exercise the whole gate without touching the
    host, and so `doctor` can be given a cheap stub if a probe ever becomes expensive.
    Subjects are probed by their own kind and nothing else: a `dependency` never causes a
    PATH lookup, a `backend` never causes an `importlib.metadata` lookup.
    """
    register = load_register() if register is None else register
    required = required_subjects(pyproject) if required is None else required
    binary_version = installed_binary_version if binary_version is None else binary_version
    distribution_version = (
        installed_distribution_version if distribution_version is None else distribution_version
    )
    binary_help = installed_binary_help if binary_help is None else binary_help

    # Rows the register carries that nothing currently declares — an Antigravity candidate,
    # a dependency removed from pyproject but still verified — are audited too, because a
    # stale row that quietly stops being checked is how a register rots.
    subjects = dict(required)
    for name, record in register.items():
        subjects.setdefault(name, record.kind)

    statuses: list[SubjectStatus] = []
    for subject in sorted(subjects):
        kind = subjects[subject]
        record = register.get(subject)
        in_use = subject in required
        installed = (
            binary_version(subject) if kind == "backend" else distribution_version(subject)
        )
        if record is None:
            statuses.append(
                SubjectStatus(
                    subject=subject, kind=kind, status="unverified",
                    verified_version="", installed_version=installed, in_use=in_use,
                    detail=(
                        "in use with no row in maestro/thirdparty.json"
                        + (f"; installed here at {installed}" if installed else "")
                    ),
                )
            )
            continue
        help_text = None
        if (
            kind == "backend" and installed is not None
            and record.verified_version and installed != record.verified_version
        ):
            # Only a moved version pays for a `--help` run.
            help_text = binary_help(subject)
        statuses.append(status_for(record, installed, in_use=in_use, help_text=help_text))
    return statuses


def blocking(statuses: list[SubjectStatus]) -> list[SubjectStatus]:
    """The subset that must be resolved before an integration is called complete."""
    return [status for status in statuses if status.blocking]
