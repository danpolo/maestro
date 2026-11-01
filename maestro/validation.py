"""Authoritative evidence, isolated staging and the acceptance decision (`[P4]`).

Everything in this module exists to answer three questions that the legacy loop answered
by trusting a file an agent wrote (`04_VALIDATION_STAGING_AND_ACCEPTANCE.md` §1):

    1. What must be checked?      -> `VerificationSpec`, derived from the task's criteria.
    2. Is existing evidence valid? -> `Fingerprints`, an exact four-hash match or nothing.
    3. Are the outcomes satisfied? -> `decide()`, a closed-world boolean over receipts.

The one rule the whole file is built around is `D06`/`[INV-04]`: **a claim is not
evidence**. A `DONE` sentinel, a reviewer saying "looks good", a `result.json` with
`tests.pass: true` — none of them is an input to `decide()`. The only thing that
discharges an acceptance criterion is a `VerificationEvidence` row describing a command
the controller itself ran, on a frozen snapshot it identified, whose four fingerprints
still match the tree being accepted. There is deliberately no parameter anywhere below
through which an agent's self-report could enter the decision.

The second rule is `[INV-05]`: the target branch is written in exactly one place —
`StagingPipeline.fast_forward`, by a compare-and-swap `git update-ref` that names the
head the integration was verified against. Every check runs in `.orchestrator/worktrees/
staging/`, which is a different worktree on a detached head, so a failing integration
check cannot leave the target branch anywhere but where it started: there is no revert
path because there was never a write.

The third is `A05`/`[INV-A05]`: `retry_allowed()` is the only function that says whether
an operation may be repeated, and it says no for every non-idempotent external effect
that cannot prove, through a postcondition probe, what already happened.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from maestro.control.models import (
    EventType,
    decision_id as new_decision_id,
    evidence_id as new_evidence_id,
    new_operation_id,
    now_iso,
)

# ── effect classes and the postcondition probe rule (`§4`, `A05`) ─────────────────────

#: `§4`'s table, as data. `retry` is whether the controller may re-execute the operation
#: *by itself*, with no further evidence about what the previous execution did.
EFFECT_CLASSES: dict[str, dict] = {
    "pure_derivation": {
        "idempotent": True,
        "retry": True,
        "recovery": "memoize by complete input/version hash",
    },
    "repeatable_local_check": {
        "idempotent": True,
        "retry": True,
        "recovery": "reuse exact valid evidence or rerun on an identical snapshot",
    },
    "agent_editing_worktree": {
        "idempotent": False,
        "retry": False,
        "recovery": "adopt the live writer, or reconcile the worktree under a lease",
    },
    "external_side_effect": {
        "idempotent": False,
        "retry": False,
        "recovery": "OperationIntent + OperationReceipt + postcondition probe",
    },
}

#: The outcome of an operation whose effect cannot be established. Not a failure: a
#: failure is a decided thing, and this is the absence of a decision.
UNCERTAIN = "uncertain"


def retry_allowed(
    effect_class: str, *, probe=None, probe_result: bool | None = None
) -> tuple[bool, str]:
    """`[INV-A05]`: may the controller re-run this operation on its own authority?

    Returns `(allowed, reason)`. The rule, in the order it is applied:

    - a repeatable class (pure derivation, local check) may always be re-run;
    - a non-idempotent class with **no** probe may never be re-run automatically — not
      after a timeout, not after a crash, not after a lost connection. Blind replay of an
      unprobed external effect is what double-pushes a branch and double-bills an API;
    - a non-idempotent class **with** a probe is adjudicated by the probe's answer, and a
      probe that returns `True` does not authorise a retry either: it says the effect
      already happened, so the right move is to record a receipt, not to do it again.

    `probe_result` is the already-obtained answer; pass `probe` instead to have it called
    here. A probe that raises is an unanswered probe, which fails closed.
    """
    profile = EFFECT_CLASSES.get(effect_class)
    if profile is None:
        return False, f"unknown effect_class {effect_class!r} — fails closed"
    if profile["retry"]:
        return True, f"{effect_class} is idempotent; re-execution is pure"
    if probe_result is None and probe is not None:
        try:
            probe_result = bool(probe())
        except Exception as exc:  # noqa: BLE001 — an erroring probe answered nothing
            return False, f"postcondition probe raised {type(exc).__name__}: {exc}"
    if probe_result is None:
        return False, (
            f"{effect_class} is not idempotent and declares no postcondition probe; "
            "automatic retry is prohibited (A05)"
        )
    if probe_result:
        return False, (
            "postcondition probe confirms the effect already happened; record a receipt "
            "rather than repeating it"
        )
    return False, (
        "postcondition probe could not confirm the effect; state is uncertain and must "
        "be escalated to the operator, not retried"
    )


# ── what must be checked (`§1.1`) ────────────────────────────────────────────────────

#: `§2.1`/`§2.2`. `exact_snapshot_only` is the only policy that may ever reuse evidence.
CACHE_POLICIES = frozenset({"exact_snapshot_only", "affected_shadow", "never"})
DEPENDENCY_COMPLETENESS = frozenset({"exact", "conservative_globs", "unknown"})

#: `§2.10`. `passed` is the only one of these that can discharge a criterion.
OUTCOMES = frozenset({"passed", "failed", "timed_out", "errored"})
VALIDITIES = frozenset({"current", "stale", "invalidated"})


class ValidationError(RuntimeError):
    """A malformed spec or an impossible request. Never an ordinary check failure."""


@dataclass(frozen=True)
class VerificationSpec:
    """One controller-executed check (`§2.10`).

    `required` and `acceptance_criterion_ids` are what make a check *mean* something: a
    check that discharges no criterion cannot help a task graduate however green it is,
    and a criterion no check names cannot be discharged at all — which is the closed
    world `[INV-04]` asks for, stated as data rather than as an `if`.
    """

    check_id: str
    argv_or_adapter_ref: tuple[str, ...] = ()
    handler_ref: str = "maestro.validation.command_check"
    cwd_scope: str = "."
    timeout: int = 600
    required: bool = True
    acceptance_criterion_ids: tuple[str, ...] = ()
    expected_output_schema: str = "exit_status"
    expected_output: str = ""
    input_dependencies: tuple[str, ...] = ()
    dependency_completeness: str = "unknown"
    environment_contract: Mapping[str, object] = field(default_factory=dict)
    external_state_contract: Mapping[str, object] | None = None
    cache_policy: str = "never"
    shell: bool = False
    stage: str = "pre_merge"

    def __post_init__(self) -> None:
        if not self.check_id:
            raise ValidationError("a VerificationSpec needs a check_id")
        if self.stage not in ("pre_merge", "post_merge"):
            raise ValidationError(f"{self.check_id}: invalid verification stage {self.stage!r}")
        if self.cache_policy not in CACHE_POLICIES:
            raise ValidationError(
                f"{self.check_id}: cache_policy {self.cache_policy!r} not in {sorted(CACHE_POLICIES)}"
            )
        if self.dependency_completeness not in DEPENDENCY_COMPLETENESS:
            raise ValidationError(
                f"{self.check_id}: dependency_completeness {self.dependency_completeness!r} "
                f"not in {sorted(DEPENDENCY_COMPLETENESS)}"
            )
        object.__setattr__(self, "argv_or_adapter_ref", tuple(self.argv_or_adapter_ref))
        object.__setattr__(self, "acceptance_criterion_ids", tuple(self.acceptance_criterion_ids))
        object.__setattr__(self, "input_dependencies", tuple(self.input_dependencies))

    @property
    def cacheable(self) -> bool:
        """Whether evidence for this check may ever be reused (`§2.2`).

        Three ways to be uncacheable, and the third is the interesting one: a check whose
        declared input dependencies are `unknown` cannot have a meaningful
        `input_fingerprint`, so a "match" on it would be a match on ignorance. Such a
        check re-runs every time, whatever its `cache_policy` claims.
        """
        if self.cache_policy != "exact_snapshot_only":
            return False
        if self.dependency_completeness == "unknown":
            return False
        return self.external_state_contract is None or bool(
            self.external_state_contract.get("pinned")
        )

    @classmethod
    def from_roadmap(cls, entry: Mapping, *, repo: Path | None = None) -> "VerificationSpec":
        """Lift one legacy `verifications:` block into a typed spec.

        The roadmap's vocabulary is `{id, kind, cmd, expect, check}`; `kind: auto` becomes
        a command check, and `kind: manual` becomes a *reviewer* check with no command —
        it cannot be discharged by running anything, which is exactly right, because what
        used to discharge it was an agent's own prose. `kind: dan` is a check only the
        operator can confirm: also command-less, discharged by Dan's approval (graph runner).
        """
        vid = str(entry.get("id") or "")
        kind = str(entry.get("kind") or "manual")
        if kind == "auto":
            command = str(entry.get("cmd") or "")
            if not command:
                raise ValidationError(f"{vid}: kind:auto with no cmd cannot be executed")
            return cls(
                check_id=vid,
                argv_or_adapter_ref=(command,),
                shell=True,
                handler_ref="maestro.validation.command_check",
                acceptance_criterion_ids=(vid,),
                expected_output=str(entry.get("expect") or ""),
                expected_output_schema="last_stdout_line" if entry.get("expect") else "exit_status",
                timeout=int(entry.get("timeout") or 600),
                required=True,
                input_dependencies=tuple(entry.get("inputs") or ()),
                stage=str(entry.get("stage") or "pre_merge"),
                dependency_completeness="conservative_globs" if entry.get("inputs") else "unknown",
                cache_policy="exact_snapshot_only" if entry.get("inputs") else "never",
            )
        if kind == DAN_KIND:
            # A check only the operator can confirm (a UI renders, a device receives a
            # push). Required, never executed: under the graph runner it is discharged by
            # Dan's approval, recorded as a review disposition bound to the gate's snapshot.
            return cls(
                check_id=vid,
                handler_ref=DAN_CHECK_HANDLER,
                stage="post_merge",
                acceptance_criterion_ids=(vid,),
                expected_output_schema="review_disposition",
                required=True,
                cache_policy="never",
            )
        return cls(
            check_id=vid,
            handler_ref="maestro.validation.reviewer_check",
            acceptance_criterion_ids=(vid,),
            expected_output_schema="review_disposition",
            required=True,
            cache_policy="never",
        )


#: `kind: dan` in a ROADMAP `verifications:` entry, and the handler its spec carries.
DAN_KIND = "dan"
DAN_CHECK_HANDLER = "maestro.validation.dan_check"
REVIEWER_CHECK_HANDLER = "maestro.validation.reviewer_check"
#: Checks that are judgments rather than measurements: nothing is executed for them, and
#: only a conclusive review disposition passed into `decide()` discharges them.
JUDGMENT_HANDLERS = frozenset({REVIEWER_CHECK_HANDLER, DAN_CHECK_HANDLER})


def is_dan_check(spec: VerificationSpec) -> bool:
    """Whether `spec` is a `kind: dan` check — confirmed by the operator, never run."""
    return spec.handler_ref == DAN_CHECK_HANDLER


def is_post_merge_check(spec: VerificationSpec) -> bool:
    return is_dan_check(spec) or spec.stage == "post_merge"


def specs_from_roadmap(entries: Iterable[Mapping], **kwargs) -> list[VerificationSpec]:
    return [VerificationSpec.from_roadmap(entry, **kwargs) for entry in entries]


def criterion_ids(specs: Iterable[VerificationSpec]) -> tuple[str, ...]:
    """Every criterion the given specs claim to discharge, deduplicated and ordered."""
    seen: list[str] = []
    for spec in specs:
        for cid in spec.acceptance_criterion_ids:
            if cid not in seen:
                seen.append(cid)
    return tuple(seen)


# ── is the evidence still valid (`§2`) ───────────────────────────────────────────────


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest(parts: Iterable[str]) -> str:
    body = "\n".join(parts)
    return _sha256(body.encode("utf-8"))


def git(
    args: Sequence[str], *, cwd: Path, runner=subprocess.run, check: bool = False,
    text: bool = True,
):
    """One git invocation. Centralised so every call site is greppable and injectable."""
    proc = runner(
        ["git", *args], cwd=str(cwd), capture_output=True, text=text
    )
    if check and proc.returncode != 0:
        raise ValidationError(
            f"git {' '.join(args)} failed in {cwd}: {(proc.stderr or proc.stdout).strip()[:300]}"
        )
    return proc


def snapshot_id(worktree: Path, *, runner=subprocess.run) -> str:
    """`§2.1`'s `source_snapshot_id`: the commit the evaluated tree is at.

    Falls back to the tree object when there is no commit yet (a repository mid-`git
    init` has a tree and no HEAD), and to the empty string when the path is not a
    repository at all. An empty snapshot id never matches a cached one, so the
    degradation is toward re-running, never toward reusing.
    """
    head = git(["rev-parse", "HEAD"], cwd=worktree, runner=runner)
    if head.returncode == 0 and head.stdout.strip():
        return head.stdout.strip()
    tree = git(["write-tree"], cwd=worktree, runner=runner)
    if tree.returncode == 0 and tree.stdout.strip():
        return f"tree:{tree.stdout.strip()}"
    return ""


def command_hash(spec: VerificationSpec, *, adapter_source: bytes = b"") -> str:
    """SHA256 of the exact argv, its options **and the adapter's own code** (`§2.1`).

    The adapter source is in the hash because a check is not identified by what it was
    asked to do but by what will actually run: an adapter edited between two runs is a
    different check with the same argv, and evidence from before the edit proves nothing
    about after it.
    """
    return _digest(
        [
            spec.check_id,
            spec.handler_ref,
            json.dumps(list(spec.argv_or_adapter_ref), ensure_ascii=False),
            spec.cwd_scope,
            spec.expected_output_schema,
            spec.expected_output,
            str(spec.timeout),
            str(spec.shell),
            _sha256(adapter_source),
        ]
    )


def _iter_dependency_files(root: Path, patterns: Sequence[str]) -> list[Path]:
    """Every file under `root` matching any glob, deduplicated and sorted.

    `Path.glob` handles `**`; the `fnmatch` fallback catches patterns naming a directory
    prefix without a wildcard. `.git` is excluded because its contents change on every
    git operation, which would make every fingerprint unique and every cache a miss.
    """
    found: set[Path] = set()
    for pattern in patterns:
        try:
            matches = list(root.glob(pattern))
        except (NotImplementedError, ValueError):
            matches = []
        if not matches:
            matches = [
                path
                for path in root.rglob("*")
                if fnmatch.fnmatch(str(path.relative_to(root)), pattern)
            ]
        for match in matches:
            if match.is_file():
                found.add(match)
            elif match.is_dir():
                found.update(p for p in match.rglob("*") if p.is_file())
    return sorted(p for p in found if ".git" not in p.parts)


def input_fingerprint(root: Path, patterns: Sequence[str]) -> str:
    """Combined SHA256 of every declared input dependency's *content* (`§2.1`).

    Content, not mtime and not signature: `§2.2` is explicit that an unchanged public API
    does not imply unchanged behaviour, so a byte change anywhere in the declared inputs
    has to move this hash. An empty pattern list hashes to a constant, which is why a
    spec that declares no inputs is not cacheable (`VerificationSpec.cacheable`).
    """
    root = Path(root)
    parts: list[str] = []
    for path in _iter_dependency_files(root, list(patterns)):
        try:
            digest = _sha256(path.read_bytes())
        except OSError:
            digest = "unreadable"
        parts.append(f"{path.relative_to(root).as_posix()}:{digest}")
    return _digest(parts)


def environment_fingerprint(contract: Mapping[str, object] | None = None) -> str:
    """Toolchain, OS, interpreter and declared non-secret configuration (`§2.1`).

    Secrets are excluded by construction: only the *names* of the environment variables a
    contract declares are hashed, plus the values the contract itself states. Hashing
    `os.environ` wholesale would put credentials into a durable record.
    """
    contract = dict(contract or {})
    declared_env = contract.pop("env", {}) or {}
    if isinstance(declared_env, Mapping):
        env_parts = [
            f"{name}={os.environ.get(name, '')}" if declared_env[name] is None else
            f"{name}={declared_env[name]}"
            for name in sorted(declared_env)
        ]
    else:
        env_parts = [f"env:{name}" for name in sorted(map(str, declared_env))]
    return _digest(
        [
            f"python={platform.python_version()}",
            f"implementation={platform.python_implementation()}",
            f"system={platform.system()}",
            f"machine={platform.machine()}",
            f"executable={sys.executable}",
            json.dumps(contract, ensure_ascii=False, sort_keys=True, default=str),
            *env_parts,
        ]
    )


@dataclass(frozen=True)
class Fingerprints:
    """The four hashes `§2.1` requires to match *exactly* for a cache hit."""

    source_snapshot_id: str
    command_hash: str
    input_fingerprint: str
    environment_fingerprint: str

    def matches(self, other: "Fingerprints | Mapping") -> bool:
        get = other.get if isinstance(other, Mapping) else lambda k: getattr(other, k)
        return all(
            getattr(self, key) == get(key)
            for key in (
                "source_snapshot_id", "command_hash", "input_fingerprint",
                "environment_fingerprint",
            )
        )


def fingerprints(
    spec: VerificationSpec,
    worktree: Path,
    *,
    snapshot: str | None = None,
    adapter_source: bytes = b"",
    runner=subprocess.run,
) -> Fingerprints:
    return Fingerprints(
        source_snapshot_id=snapshot if snapshot is not None
        else snapshot_id(worktree, runner=runner),
        command_hash=command_hash(spec, adapter_source=adapter_source),
        input_fingerprint=input_fingerprint(worktree, spec.input_dependencies),
        environment_fingerprint=environment_fingerprint(spec.environment_contract),
    )


# ── the evidence record (`§2.10`) ────────────────────────────────────────────────────


@dataclass(frozen=True)
class VerificationEvidence:
    """One controller-executed check, cryptographically identified."""

    evidence_id: str
    check_id: str
    source_snapshot_id: str
    attempt_id: str
    command_hash: str
    input_fingerprint: str
    environment_fingerprint: str
    exit_status: int
    outcome: str
    validity: str = "current"
    verifier_version: str = ""
    external_snapshot_id: str | None = None
    started_at: str = ""
    finished_at: str = ""
    output_artifact_refs: tuple[str, ...] = ()
    reused_from: str | None = None
    reuse_reason: str | None = None
    recorded_at: str = ""
    detail: str = ""
    extra_step_cause: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValidationError(f"outcome {self.outcome!r} not in {sorted(OUTCOMES)}")
        if self.validity not in VALIDITIES:
            raise ValidationError(f"validity {self.validity!r} not in {sorted(VALIDITIES)}")
        object.__setattr__(self, "output_artifact_refs", tuple(self.output_artifact_refs))

    @property
    def passed(self) -> bool:
        """The only shape of evidence that can discharge anything."""
        return self.outcome == "passed" and self.validity == "current"

    @property
    def fingerprints(self) -> Fingerprints:
        return Fingerprints(
            source_snapshot_id=self.source_snapshot_id,
            command_hash=self.command_hash,
            input_fingerprint=self.input_fingerprint,
            environment_fingerprint=self.environment_fingerprint,
        )

    def to_row(self) -> dict:
        row = {
            "evidence_id": self.evidence_id,
            "check_id": self.check_id,
            "source_snapshot_id": self.source_snapshot_id,
            "attempt_id": self.attempt_id,
            "command_hash": self.command_hash,
            "input_fingerprint": self.input_fingerprint,
            "environment_fingerprint": self.environment_fingerprint,
            "exit_status": int(self.exit_status),
            "outcome": self.outcome,
            "validity": self.validity,
            "recorded_at": self.recorded_at or now_iso(),
            "verifier_version": self.verifier_version,
            "external_snapshot_id": self.external_snapshot_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "output_artifact_refs": json.dumps(list(self.output_artifact_refs)),
            "reused_from": self.reused_from,
            "reuse_reason": self.reuse_reason,
        }
        if self.extra_step_cause is not None:
            row["extra_step_cause"] = self.extra_step_cause
        return row

    @classmethod
    def from_row(cls, row: Mapping) -> "VerificationEvidence":
        refs = row["output_artifact_refs"] if "output_artifact_refs" in row.keys() else "[]"
        try:
            parsed = tuple(json.loads(refs or "[]"))
        except (TypeError, json.JSONDecodeError):
            parsed = ()
        return cls(
            evidence_id=row["evidence_id"],
            check_id=row["check_id"],
            source_snapshot_id=row["source_snapshot_id"],
            attempt_id=row["attempt_id"],
            command_hash=row["command_hash"],
            input_fingerprint=row["input_fingerprint"],
            environment_fingerprint=row["environment_fingerprint"],
            exit_status=int(row["exit_status"]),
            outcome=row["outcome"],
            validity=row["validity"],
            recorded_at=row["recorded_at"],
            verifier_version=row["verifier_version"] if "verifier_version" in row.keys() else "",
            external_snapshot_id=row["external_snapshot_id"]
            if "external_snapshot_id" in row.keys() else None,
            started_at=row["started_at"] if "started_at" in row.keys() else "",
            finished_at=row["finished_at"] if "finished_at" in row.keys() else "",
            output_artifact_refs=parsed,
            reused_from=row["reused_from"] if "reused_from" in row.keys() else None,
            reuse_reason=row["reuse_reason"] if "reuse_reason" in row.keys() else None,
            extra_step_cause=row["extra_step_cause"] if "extra_step_cause" in row.keys() else None,
        )


class EvidenceRegistry:
    """Durable evidence, and the one place a cache hit may be decided.

    Holds a `ControlStore` when there is one and works without it when there is not — the
    checks themselves must be runnable in a worker (`[INV-01]`: a worker never writes the
    control database), and the controller commits what the worker reports. Without a
    store, `reusable()` always misses, which is the safe direction.
    """

    def __init__(self, store=None, artifacts=None):
        self.store = store
        self.artifacts = artifacts
        #: Check output a registry with no artifact store could not put anywhere, by
        #: evidence id — a worker's registry, whose controller stores it at ingest.
        self.held_output: dict[str, dict] = {}

    # --- reuse ------------------------------------------------------------------------

    def reusable(self, spec: VerificationSpec, fp: Fingerprints) -> VerificationEvidence | None:
        """`§2.1`: an exact match on all four hashes, or nothing.

        A non-cacheable spec short-circuits before the query is even made, so a
        `cache_policy: never` check cannot be served from cache by a lookup that happens
        to match — network calls, browser tests and staging smokes are uncacheable by
        declaration and stay that way (`§2.2`).
        """
        if not spec.cacheable or self.store is None:
            return None
        row = self.store.find_current_evidence(
            check_id=spec.check_id,
            source_snapshot_id=fp.source_snapshot_id,
            command_hash=fp.command_hash,
            input_fingerprint=fp.input_fingerprint,
            environment_fingerprint=fp.environment_fingerprint,
        )
        if row is None:
            return None
        return VerificationEvidence.from_row(row)

    def reuse(
        self, original: VerificationEvidence, *, attempt_id: str, at: str | None = None
    ) -> VerificationEvidence:
        """Mint a fresh record pointing at the evidence it reused (`reused_from`).

        A reuse is recorded, not silent. The alternative — returning the original row and
        letting the caller pretend it ran — loses the fact that this attempt did not
        execute the check, which is precisely what an auditor needs to see.
        """
        stamp = at or now_iso()
        return replace(
            original,
            evidence_id=new_evidence_id(original.check_id, original.source_snapshot_id),
            attempt_id=attempt_id,
            recorded_at=stamp,
            started_at=stamp,
            finished_at=stamp,
            reused_from=original.reused_from or original.evidence_id,
            reuse_reason=(
                "exact match on source_snapshot_id, command_hash, input_fingerprint and "
                "environment_fingerprint"
            ),
        )

    # --- writes -----------------------------------------------------------------------

    def record(self, evidence: VerificationEvidence) -> VerificationEvidence:
        if self.store is not None:
            self.store.record_evidence(evidence.to_row())
            self.store.append_event(
                EventType.EVIDENCE_RECORDED,
                evidence.check_id,
                payload={
                    "evidence_id": evidence.evidence_id,
                    "outcome": evidence.outcome,
                    "source_snapshot_id": evidence.source_snapshot_id,
                    "reused_from": evidence.reused_from,
                },
                attempt_id=evidence.attempt_id,
            )
        return evidence

    def store_output(
        self, evidence: VerificationEvidence, stdout: str, stderr: str
    ) -> VerificationEvidence:
        """Put the check's own output in the artifact store and reference it.

        Without a store (a graph worker, `[INV-01]`) the output is held instead, for the
        worker to report and the controller to store (`WorkflowRunner._ingest_evidence`).
        """
        if self.artifacts is None:
            self.held_output[evidence.evidence_id] = {
                "check_id": evidence.check_id, "stdout": stdout, "stderr": stderr,
            }
            return evidence
        body = json.dumps(
            {"check_id": evidence.check_id, "stdout": stdout, "stderr": stderr},
            ensure_ascii=False,
        ).encode("utf-8")
        ref = self.artifacts.put_bytes(
            body,
            type="verification_output",
            producer_attempt_id=evidence.attempt_id,
            source_snapshot_id=evidence.source_snapshot_id,
        )
        return replace(evidence, output_artifact_refs=(ref.artifact_id,))


# ── executing a check ────────────────────────────────────────────────────────────────

#: What `CheckRunner` reports as `verifier_version` when the spec names no adapter.
VERIFIER_VERSION = "p4.1"


def _observed_command_argv(spec: VerificationSpec) -> tuple[str, ...] | None:
    """Identify a literal invocation for diagnostics without changing execution.

    Shell composition, expansion and assignments cannot identify which executable
    produced a status. Keep them unknown; output text is never cause evidence.
    """
    if not spec.shell:
        return spec.argv_or_adapter_ref
    command = spec.argv_or_adapter_ref[0]
    if any(char in command for char in "\n\r$`*?[]~#"):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        argv = tuple(lexer)
    except ValueError:
        return None
    if not argv or '=' in argv[0] or any(
        token and all(char in ";&|<>()" for char in token) for token in argv
    ):
        return None
    # These shell builtins/wrappers do not identify the command they execute.
    if Path(argv[0]).name in {"eval", "exec", "command", "builtin", "env", "sh", "bash", "dash",
                   "if", "for", "while", "until", "case", "!", "{", "}"}:
        return None
    return argv


@dataclass
class CheckRunner:
    """Runs `VerificationSpec`s on a frozen worktree and produces evidence.

    It never decides anything. Its output is a list of records; whether those records add
    up to an acceptance is `decide()`'s question and nobody else's.
    """

    registry: EvidenceRegistry = field(default_factory=EvidenceRegistry)
    runner: Callable = subprocess.run
    clock: Callable = now_iso

    def run(
        self,
        spec: VerificationSpec,
        worktree: Path,
        *,
        attempt_id: str,
        snapshot: str | None = None,
        adapter_source: bytes = b"",
    ) -> VerificationEvidence:
        worktree = Path(worktree)
        fp = fingerprints(
            spec, worktree, snapshot=snapshot, adapter_source=adapter_source,
            runner=self.runner,
        )
        cached = self.registry.reusable(spec, fp)
        if cached is not None and cached.passed:
            return self.registry.record(
                self.registry.reuse(cached, attempt_id=attempt_id, at=self.clock())
            )
        started = self.clock()
        outcome, exit_status, stdout, stderr, cause = self._execute(spec, worktree)
        evidence = VerificationEvidence(
            evidence_id=new_evidence_id(spec.check_id, fp.source_snapshot_id or "nosnap"),
            check_id=spec.check_id,
            source_snapshot_id=fp.source_snapshot_id,
            attempt_id=attempt_id,
            command_hash=fp.command_hash,
            input_fingerprint=fp.input_fingerprint,
            environment_fingerprint=fp.environment_fingerprint,
            exit_status=exit_status,
            outcome=outcome,
            verifier_version=VERIFIER_VERSION,
            started_at=started,
            finished_at=self.clock(),
            recorded_at=self.clock(),
            detail=(stderr or stdout).strip()[:2000],
            extra_step_cause=cause,
        )
        evidence = self.registry.store_output(evidence, stdout, stderr)
        return self.registry.record(evidence)

    def evaluate(
        self, specs: Sequence[VerificationSpec], worktree: Path, *, attempt_id: str,
        snapshot: str | None = None,
    ) -> list[VerificationEvidence]:
        """Run every *executable* spec. Reviewer checks produce no evidence here.

        A `reviewer_check` has no command, so running it would mean asking a model and
        recording its answer as a controller-executed proof. It is left to
        `review_disposition()` and enters `decide()` through `review_dispositions`, where
        it is visibly a judgment rather than a measurement.
        """
        out: list[VerificationEvidence] = []
        for spec in specs:
            if spec.handler_ref in JUDGMENT_HANDLERS:
                continue
            out.append(self.run(spec, worktree, attempt_id=attempt_id, snapshot=snapshot))
        return out

    # --- the execution itself ---------------------------------------------------------

    def _execute(
        self, spec: VerificationSpec, worktree: Path
    ) -> tuple[str, int, str, str, str | None]:
        if not spec.argv_or_adapter_ref:
            return "errored", 2, "", f"{spec.check_id}: no command to execute", "check_broken"
        cwd = (worktree / spec.cwd_scope).resolve()
        if not cwd.is_dir():
            return "errored", 2, "", f"{spec.check_id}: cwd_scope {spec.cwd_scope!r} does not exist", "check_broken"
        command = (
            spec.argv_or_adapter_ref[0] if spec.shell else list(spec.argv_or_adapter_ref)
        )
        argv = _observed_command_argv(spec)
        available = None
        if spec.shell and argv:
            lookup = str(cwd / argv[0]) if '/' in argv[0] else argv[0]
            search_path = os.pathsep.join(str(cwd / path) for path in os.get_exec_path())
            available = shutil.which(lookup, path=search_path) is not None
        try:
            proc = self.runner(
                command, shell=spec.shell, cwd=str(cwd), capture_output=True, text=True,
                timeout=spec.timeout,
            )
        except subprocess.TimeoutExpired:
            return "timed_out", 124, "", f"{spec.check_id}: timed out after {spec.timeout}s", None
        except FileNotFoundError as exc:
            return "errored", 2, "", f"{spec.check_id}: {exc}", "environment_missing"
        except OSError as exc:
            return "errored", 2, "", f"{spec.check_id}: {exc}", None
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        if proc.returncode != 0:
            # Only a literal known command establishes a cause. Exit 127 plus
            # an absent executable is shell launch evidence, not a stderr guess.
            if spec.shell and (argv is None or (available is False and proc.returncode != 127)):
                cause = None
            elif spec.shell and available is False:
                cause = "environment_missing"
            else:
                pytest_command = bool(argv) and (
                    Path(argv[0]).name in {"pytest", "py.test"} or
                    (re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", Path(argv[0]).name) is not None
                     and argv[1:3] == ("-m", "pytest"))
                )
                cause = "check_broken" if pytest_command and proc.returncode == 5 else "check_failed"
            return "failed", int(proc.returncode), stdout, stderr, cause
        if spec.expected_output_schema == "last_stdout_line":
            actual = stdout.strip()
            last = next(
                (line.strip() for line in reversed(actual.splitlines()) if line.strip()),
                actual,
            )
            if spec.expected_output not in (actual, last):
                return (
                    "failed", int(proc.returncode), stdout,
                    f"expected {spec.expected_output!r}, got {last!r}",
                    "check_failed",
                )
        return "passed", int(proc.returncode), stdout, stderr, None


# ── reviewer records (`§1.3`, `[INV-04]`) ────────────────────────────────────────────

#: A disposition's verdict must be one of these. Anything else — including nothing — is
#: not a review that happened.
REVIEW_VERDICTS = frozenset({"approved", "rejected", "changes_requested"})


def review_disposition(
    check_id: str, verdict: str, *, findings: Sequence[str] = (), reviewer: str = "",
    subject_sha: str = "",
) -> dict:
    return {
        "check_id": check_id,
        "verdict": str(verdict or ""),
        "findings": list(findings),
        "reviewer": reviewer,
        "subject_sha": subject_sha,
    }


def review_is_conclusive(disposition: Mapping) -> tuple[bool, str]:
    """`[INV-04]`: an empty reviewer output fails closed.

    The failure this refuses is specific and was live in `gates.sonnet_review_proofs`: a
    judge that returns `{}` produced no failures, and "no failures" read as "approved".
    An empty verdict, an unknown verdict, and a rejection with no findings are all
    inconclusive here — the last one because a reviewer that rejects without saying what
    is wrong has not given anyone anything to act on.
    """
    verdict = str(disposition.get("verdict") or "").strip().lower()
    if verdict not in REVIEW_VERDICTS:
        return False, f"reviewer verdict {verdict!r} is empty or unrecognised — fails closed"
    if verdict == "approved":
        return True, "approved"
    findings = [str(f).strip() for f in disposition.get("findings") or [] if str(f).strip()]
    if not findings:
        return False, f"reviewer said {verdict!r} without a single finding — fails closed"
    return False, f"{verdict}: {'; '.join(findings)[:300]}"


# ── human approvals (`§5`) ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ApprovalBinding:
    """A human approval bound cryptographically to what was approved (`§5.1`).

    The binding is the point. An approval that names only a task can be replayed against
    a patch the operator never saw; this one carries the subject's SHA and the policy
    revision, so changing either makes the stored approval stop matching (`§5.2`).
    """

    task_id: str
    run_id: str
    subject_sha: str
    policy_hash: str
    expires_at: str
    approver: str = ""
    granted_at: str = ""

    @property
    def subject_binding(self) -> str:
        return _digest([self.task_id, self.run_id, self.subject_sha, self.policy_hash])

    def validate(
        self, *, task_id: str, run_id: str, subject_sha: str, policy_hash: str,
        now: str | None = None,
    ) -> tuple[bool, str]:
        stamp = now or now_iso()
        candidate = _digest([task_id, run_id, subject_sha, policy_hash])
        if candidate != self.subject_binding:
            return False, (
                "approval does not bind this subject — the patch or the policy changed "
                "after it was granted (§5.2)"
            )
        if self.expires_at and stamp > self.expires_at:
            return False, f"approval expired at {self.expires_at}"
        return True, "approval binds this subject and has not expired"


# ── the closed-world acceptance decision (`§1.3`) ────────────────────────────────────


@dataclass(frozen=True)
class AcceptanceDecision:
    """`§2.10`. The durable answer to "may this task graduate?" and why."""

    task_id: str
    run_id: str
    decision: str
    reason: str
    task_spec_revision: int = 1
    policy_hash: str = ""
    integrated_source_snapshot: str = ""
    required_evidence_ids: tuple[str, ...] = ()
    review_dispositions: tuple[dict, ...] = ()
    exemptions: tuple[dict, ...] = ()
    required_effect_receipts: tuple[str, ...] = ()
    event_id: str = ""
    decision_id: str = ""
    decided_at: str = ""

    def __post_init__(self) -> None:
        if self.decision not in ("accepted", "rejected"):
            raise ValidationError(f"decision {self.decision!r} must be accepted or rejected")
        object.__setattr__(self, "required_evidence_ids", tuple(self.required_evidence_ids))
        object.__setattr__(self, "review_dispositions", tuple(self.review_dispositions))
        object.__setattr__(self, "exemptions", tuple(self.exemptions))
        object.__setattr__(self, "required_effect_receipts", tuple(self.required_effect_receipts))
        if not self.decision_id:
            object.__setattr__(
                self, "decision_id", new_decision_id(self.task_id, self.run_id or "norun")
            )

    @property
    def accepted(self) -> bool:
        return self.decision == "accepted"

    def to_row(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "task_spec_revision": int(self.task_spec_revision),
            "policy_hash": self.policy_hash,
            "integrated_source_snapshot": self.integrated_source_snapshot,
            "required_evidence_ids": json.dumps(list(self.required_evidence_ids)),
            "review_dispositions": json.dumps(list(self.review_dispositions), default=str),
            "exemptions": json.dumps(list(self.exemptions), default=str),
            "required_effect_receipts": json.dumps(list(self.required_effect_receipts)),
            "decision": self.decision,
            "reason": self.reason,
            "event_id": self.event_id,
            "decided_at": self.decided_at or now_iso(),
        }


def _valid_exemption(exemption: Mapping) -> bool:
    """`§2.10`: an exemption without a reason and a scope is not an exemption."""
    return bool(
        str(exemption.get("reason") or "").strip()
        and str(exemption.get("scope") or "").strip()
        and str(exemption.get("approved_by") or "").strip()
    )


def decide(
    *,
    task_id: str,
    run_id: str,
    specs: Sequence[VerificationSpec],
    evidence: Sequence[VerificationEvidence],
    required_criterion_ids: Sequence[str] | None = None,
    review_dispositions: Sequence[Mapping] = (),
    exemptions: Sequence[Mapping] = (),
    required_effects: Sequence[str] = (),
    effect_receipts: Mapping[str, str] | None = None,
    integrated_source_snapshot: str = "",
    expected_snapshot: str | None = None,
    task_spec_revision: int = 1,
    policy_hash: str = "",
    at: str | None = None,
) -> AcceptanceDecision:
    """The closed-world boolean of `§1.3`. Rejects unless everything is proved.

    There is no `claims` parameter, no `workspace` parameter and no sentinel path: an
    agent's `DONE` file and its `result.json` cannot reach this function, so no argument
    it is called with can make a self-report look like a proof (`D06`).

    Every rejection carries the specific missing thing in `reason`, because "rejected"
    with no reason is the failure mode that makes operators start overriding gates.
    """
    stamp = at or now_iso()
    receipts = dict(effect_receipts or {})
    by_check: dict[str, list[VerificationEvidence]] = {}
    for record in evidence:
        by_check.setdefault(record.check_id, []).append(record)
    spec_by_id = {spec.check_id: spec for spec in specs}

    exempted_criteria = {
        str(item.get("criterion_id") or "")
        for item in exemptions
        if _valid_exemption(item)
    }
    malformed = [item for item in exemptions if not _valid_exemption(item)]

    def rejection(reason: str) -> AcceptanceDecision:
        return AcceptanceDecision(
            task_id=task_id, run_id=run_id, decision="rejected", reason=reason,
            task_spec_revision=task_spec_revision, policy_hash=policy_hash,
            integrated_source_snapshot=integrated_source_snapshot,
            required_evidence_ids=tuple(
                record.evidence_id for record in evidence if record.passed
            ),
            review_dispositions=tuple(dict(d) for d in review_dispositions),
            exemptions=tuple(dict(e) for e in exemptions),
            required_effect_receipts=tuple(receipts.get(name, "") for name in required_effects),
            decided_at=stamp,
        )

    if malformed:
        names = ", ".join(str(item.get("criterion_id") or "?") for item in malformed)
        return rejection(
            f"exemption(s) for {names} lack a reason, a scope or an approver — "
            "an unexplained exemption cannot discharge a criterion"
        )

    # 0. an empty closed world proves nothing.
    #
    # The legacy gate printed "No verifications defined — pass (no gate)" and returned 0,
    # so a task that declared nothing graduated on the strength of having declared
    # nothing. That is the same substitution as trusting a DONE file, one level up: the
    # absence of a criterion became the satisfaction of one. `[INV-04]` reads the other
    # way — a missing verification key fails closed — so a decision with no required
    # check and no criterion to discharge is a rejection, and the fix for it is to
    # declare what "done" means for the task.
    declared = [
        spec for spec in specs
        if spec.required and spec.handler_ref not in JUDGMENT_HANDLERS
    ]
    if not declared and not list(required_criterion_ids or ()):
        return rejection(
            "no required verification is declared for this task — an empty closed world "
            "discharges nothing and fails closed (INV-04)"
        )

    # 1. drift: the tree being accepted must be the tree the evidence describes.
    if expected_snapshot is not None and integrated_source_snapshot != expected_snapshot:
        return rejection(
            f"source drift: evidence describes {expected_snapshot!r} but the tree being "
            f"accepted is {integrated_source_snapshot!r}"
        )

    # 2. every required check must have current, passing evidence.
    for spec in specs:
        if not spec.required:
            continue
        if spec.handler_ref in JUDGMENT_HANDLERS:
            continue
        records = by_check.get(spec.check_id, [])
        if not records:
            return rejection(f"no evidence at all for required check {spec.check_id!r}")
        newest = records[-1]
        if not newest.passed:
            return rejection(
                f"required check {spec.check_id!r} is {newest.outcome}/{newest.validity}: "
                f"{newest.detail[:200] or 'no detail'}"
            )
        if expected_snapshot is not None and newest.source_snapshot_id != expected_snapshot:
            return rejection(
                f"evidence for {spec.check_id!r} was produced on snapshot "
                f"{newest.source_snapshot_id!r}, not on {expected_snapshot!r}"
            )

    # 3. closed world: every declared criterion must be discharged by something.
    criteria = list(required_criterion_ids if required_criterion_ids is not None
                    else criterion_ids(specs))
    for criterion in criteria:
        if criterion in exempted_criteria:
            continue
        discharging = [
            record
            for record in evidence
            if record.passed
            and criterion in spec_by_id.get(record.check_id, VerificationSpec(
                check_id=record.check_id)).acceptance_criterion_ids
        ]
        if discharging:
            continue
        approving = [
            d for d in review_dispositions
            if criterion in (
                spec_by_id.get(str(d.get("check_id") or ""), VerificationSpec(
                    check_id=str(d.get("check_id") or "x"))).acceptance_criterion_ids
            )
            and review_is_conclusive(d)[0]
            and (expected_snapshot is None or d.get('subject_sha') == expected_snapshot
                 or (not d.get('subject_sha') and not is_dan_check(
                     spec_by_id.get(str(d.get('check_id') or ''), VerificationSpec(check_id='unknown')))))
        ]
        if approving:
            continue
        return rejection(
            f"acceptance criterion {criterion!r} has no passing evidence and no "
            "conclusive review — a missing proof key fails closed (INV-04)"
        )

    # 4. every review disposition present must itself be conclusive.
    for disposition in review_dispositions:
        ok, why = review_is_conclusive(disposition)
        if not ok:
            return rejection(
                f"review {str(disposition.get('check_id') or '?')!r}: {why}"
            )

    # 5. every declared external effect must have a receipt.
    missing_receipts = [name for name in required_effects if not receipts.get(name)]
    if missing_receipts:
        return rejection(
            f"declared external effect(s) {', '.join(missing_receipts)} have no "
            "OperationReceipt — an unreceipted effect is an unknown effect (INV-07)"
        )

    return AcceptanceDecision(
        task_id=task_id,
        run_id=run_id,
        decision="accepted",
        reason=(
            f"{len([s for s in specs if s.required])} required check(s) passed on "
            f"{integrated_source_snapshot[:12] or 'the integrated snapshot'}; "
            f"{len(criteria)} criterion(s) discharged; "
            f"{len(required_effects)} effect receipt(s) present"
        ),
        task_spec_revision=task_spec_revision,
        policy_hash=policy_hash,
        integrated_source_snapshot=integrated_source_snapshot,
        required_evidence_ids=tuple(record.evidence_id for record in evidence if record.passed),
        review_dispositions=tuple(dict(d) for d in review_dispositions),
        exemptions=tuple(dict(e) for e in exemptions),
        required_effect_receipts=tuple(receipts.get(name, "") for name in required_effects),
        decided_at=stamp,
    )


def commit_acceptance(store, lifecycle, decision: AcceptanceDecision, *, at: str | None = None):
    """`§3.1` step 6: one transaction — the decision row, the event, the task status.

    A rejection is recorded too, and does not move the task: a task that failed its gate
    is still `active`, and writing `blocked` here would take a routing decision that
    belongs to the runner.
    """
    stamp = at or now_iso()
    # The store autocommits each statement; without this block a failure part-way left a
    # decision row and its event behind a task that was never accepted.
    with store.transaction() as conn:
        return _commit_acceptance(store, lifecycle, decision, stamp, conn)


def _commit_acceptance(store, lifecycle, decision: AcceptanceDecision, stamp: str, conn):
    event = store.append_event(
        EventType.ACCEPTANCE_DECIDED,
        decision.task_id,
        payload={
            "decision_id": decision.decision_id,
            "decision": decision.decision,
            "reason": decision.reason,
            "run_id": decision.run_id,
            "integrated_source_snapshot": decision.integrated_source_snapshot,
            "required_evidence_ids": list(decision.required_evidence_ids),
        },
        run_id=decision.run_id,
        recorded_at=stamp,
        conn=conn,
    )
    decided = replace(decision, event_id=event.event_id, decided_at=stamp)
    store.record_acceptance(decided.to_row(), conn=conn)
    if decided.accepted and lifecycle is not None:
        row = store.task(decided.task_id)
        if row is not None and row["status"] != "accepted":
            if row["status"] not in ("active", "waiting", "blocked"):
                lifecycle.set_task_status(decided.task_id, "active", at=stamp, conn=conn)
            lifecycle.set_task_status(decided.task_id, "accepted", at=stamp, conn=conn)
    return decided


# ── the 5-stage staging pipeline (`§3`) ──────────────────────────────────────────────

#: Where the staging worktree lives. One per project, reused: a fresh checkout per task
#: would re-download nothing (it is a git worktree, not a clone) but would leak
#: directories for every task that ever failed.
STAGING_REL = Path(".orchestrator") / "worktrees" / "staging"


@dataclass(frozen=True)
class Frozen:
    """Stage 1: the candidate, identified and no longer moving."""

    ref: str
    commit: str
    tree: str
    dirty: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.dirty


@dataclass(frozen=True)
class Staged:
    """Stage 2: the merged snapshot, in a worktree that is not the target branch."""

    path: Path
    target_head: str
    merged_commit: str = ""
    conflicted: bool = False
    detail: str = ""
    #: Files whose git conflict was resolved deterministically (D20, `mergeresolve`).
    resolved: tuple[str, ...] = ()
    #: An unresolvable conflict: `"planner"` — the tasks should not have run in parallel.
    fault: str = ""
    conflicts: tuple[Mapping, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.merged_commit) and not self.conflicted


@dataclass(frozen=True)
class IntegrationOutcome:
    """What `integrate()` did, down to which stage it stopped at."""

    stage: str
    ok: bool
    reason: str
    target_before: str = ""
    target_after: str = ""
    merged_commit: str = ""
    evidence: tuple[VerificationEvidence, ...] = ()
    decision: AcceptanceDecision | None = None
    receipts: Mapping[str, str] = field(default_factory=dict)
    resolved: tuple[str, ...] = ()
    fault: str = ""
    conflicts: tuple[Mapping, ...] = ()

    @property
    def target_moved(self) -> bool:
        return bool(self.target_after) and self.target_after != self.target_before


class StagingPipeline:
    """`§3`'s five stages, in order, with the target branch written exactly once.

    The class holds no mutable state between calls beyond the repository it was given;
    every stage takes the previous stage's record and returns its own, so a caller can
    stop after any stage (which is how the tests assert "a failed staging check leaves
    the target branch completely untouched" — they check stage 3 failed and then read the
    ref, which never had a writer).
    """

    def __init__(
        self,
        repo: Path,
        *,
        target_branch: str = "main",
        staging_root: Path | None = None,
        store=None,
        lifecycle=None,
        registry: EvidenceRegistry | None = None,
        runner=subprocess.run,
        clock=now_iso,
    ):
        self.repo = Path(repo)
        self.target_branch = target_branch
        self.staging_root = Path(staging_root) if staging_root else self.repo / STAGING_REL
        self.store = store
        self.lifecycle = lifecycle
        self.registry = registry or EvidenceRegistry(store=store)
        self.runner = runner
        self.clock = clock

    # --- helpers ----------------------------------------------------------------------

    def _git(self, *args: str, cwd: Path | None = None):
        return git(list(args), cwd=cwd or self.repo, runner=self.runner)

    def target_head(self) -> str:
        proc = self._git("rev-parse", f"refs/heads/{self.target_branch}")
        return proc.stdout.strip() if proc.returncode == 0 else ""

    # --- stage 1 ----------------------------------------------------------------------

    def freeze_candidate(self, ref: str, *, worktree: Path | None = None) -> Frozen:
        """Identify the candidate and prove nothing is still being written to it.

        `dirty` is not cosmetic. A candidate worktree with uncommitted changes is one
        whose commit SHA does not describe what an agent produced, so every fingerprint
        taken from it describes a tree that is not the tree under test. The caller is
        expected to refuse (`integrate()` does); reporting rather than raising keeps the
        fact available to an operator view.
        """
        # `--verify` and the `^{commit}` peel together: a bare `git rev-parse <unknown>`
        # exits non-zero but still prints the argument back on stdout, so reading its
        # output without checking the code yields a "commit" that is the branch name.
        resolved = self._git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        commit = resolved.stdout.strip() if resolved.returncode == 0 else ""
        tree = ""
        if commit:
            tree = self._git("rev-parse", f"{commit}^{{tree}}").stdout.strip()
        dirty: tuple[str, ...] = ()
        if worktree is not None:
            status = self._git("status", "--porcelain", cwd=Path(worktree))
            dirty = tuple(
                line.strip() for line in status.stdout.splitlines() if line.strip()
            )
        return Frozen(ref=ref, commit=commit, tree=tree, dirty=dirty)

    # --- stage 2 ----------------------------------------------------------------------

    def prepare_staging(self, frozen: Frozen, *, target_head: str | None = None) -> Staged:
        """Merge the candidate into a *fresh snapshot of the target* — never the reverse.

        The worktree is detached at the target head, so the merge commit that comes out
        of it belongs to no branch until stage 4 decides it does.

        A conflict is first tried deterministically (P12 finding D20, `mergeresolve`):
        hunks that touch disjoint base lines, identical edits, and insertions at the same
        point are merged by rule, and stage 3 re-verifies the result. Anything else
        aborts and is reported as a planner fault — parallel tasks must merge trivially.
        `§3.1` forbids ever handing a conflict to a model, because an LLM-resolved
        conflict is an unreviewed authorship of code nobody asked for.
        """
        head = target_head or self.target_head()
        self.discard_staging()
        self.staging_root.parent.mkdir(parents=True, exist_ok=True)
        add = self._git(
            "worktree", "add", "--detach", str(self.staging_root), head
        )
        if add.returncode != 0:
            return Staged(
                path=self.staging_root, target_head=head, conflicted=False,
                detail=f"could not create staging worktree: "
                       f"{(add.stderr or add.stdout).strip()[:300]}",
            )
        merge = self._git(
            "merge", "--no-ff", "--no-edit", frozen.commit,
            "-m", f"integrate({frozen.ref}): staging verification",
            cwd=self.staging_root,
        )
        resolved: tuple[str, ...] = ()
        if merge.returncode != 0:
            resolved, conflicts = self._resolve_conflicts(frozen, head)
            if conflicts or not resolved:
                self._git("merge", "--abort", cwd=self.staging_root)
                return self._conflict_report(frozen, head, conflicts, merge)
        merged = self._git("rev-parse", "HEAD", cwd=self.staging_root).stdout.strip()
        return Staged(
            path=self.staging_root, target_head=head, merged_commit=merged, resolved=resolved
        )

    def _resolve_conflicts(self, frozen: Frozen, head: str) -> tuple[tuple[str, ...], list[dict]]:
        """Resolve a stopped staging merge by rule, or say why not (P12 finding D20).

        Returns `(resolved files, [])` once the merge is committed, else `((), conflicts)`
        with the merge still in progress for the caller to abort. Only a text edit of a
        file both sides already had qualifies: a missing index stage (add/add,
        modify/delete, rename), a binary or a non-UTF-8 file is never resolved.
        """
        from maestro import mergeresolve

        cwd = self.staging_root
        listing = self._git("diff", "--name-only", "--diff-filter=U", "-z", cwd=cwd)
        paths = sorted(p for p in listing.stdout.split("\0") if p)
        if listing.returncode != 0 or not paths:
            return (), []
        merged: dict[str, str] = {}
        conflicts: list[dict] = []
        for path in paths:
            stages = [self._stage_text(cwd, stage, path) for stage in (1, 2, 3)]
            if any(text is None for text in stages):
                conflicts.append({"path": path, "lines": "", "reason": (
                    "not a text edit of a common file (added on both sides, deleted, "
                    "renamed, binary or not UTF-8)")})
                continue
            result = mergeresolve.merge_text(*stages, path=path)
            if result.ok:
                merged[path] = result.text
            else:
                conflicts.extend(c.to_dict() for c in result.conflicts)
        if conflicts:
            return (), conflicts
        for path, text in merged.items():
            (cwd / path).write_bytes(text.encode("utf-8"))
        added = self._git("add", "--", *merged, cwd=cwd)
        commit = self._git(
            "commit", "--no-verify",
            "-m", f"integrate({frozen.ref}): staging verification",
            "-m", "Merge-resolved-deterministically: " + ", ".join(merged),
            cwd=cwd,
        )
        if added.returncode != 0 or commit.returncode != 0:
            why = (commit.stderr or commit.stdout or added.stderr).strip()[:200]
            return (), [{"path": ", ".join(merged), "lines": "",
                         "reason": f"could not commit the resolution: {why}"}]
        return tuple(merged), []

    def _stage_text(self, cwd: Path, stage: int, path: str) -> str | None:
        """Index stage `stage` of `path` as text; `None` if absent, binary or not UTF-8."""
        proc = git(["show", f":{stage}:{path}"], cwd=cwd, runner=self.runner, text=False)
        data = proc.stdout
        if proc.returncode != 0 or not isinstance(data, bytes) or b"\0" in data:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def _conflict_report(self, frozen: Frozen, head: str, conflicts: list[dict], merge) -> Staged:
        """An unresolvable conflict, as the planner finding it is (P12 finding D20)."""
        if not conflicts:
            return Staged(
                path=self.staging_root, target_head=head, conflicted=True,
                detail="merge failed; integration aborted: "
                       + (merge.stderr or merge.stdout or "").strip()[:300],
            )
        base = self._git("merge-base", head, frozen.commit).stdout.strip()
        where, others_all = [], []
        for conflict in conflicts:
            others = self._tasks_touching(base, head, conflict["path"].split(", ")[0])
            conflict["other_tasks"] = others
            others_all += [o for o in others if o not in others_all]
            lines = f" lines {conflict['lines']}" if conflict.get("lines") else ""
            where.append(f"{conflict['path']}{lines}")
        detail = (
            f"merge conflict; integration aborted. {frozen.ref} and "
            f"{', '.join(others_all) or 'the target'} changed the same lines: "
            f"{'; '.join(where)}. planner fault: these tasks should not have run in "
            "parallel (parallel tasks must merge trivially). A conflict is never "
            "resolved by a model (§3.1)"
        )
        return Staged(
            path=self.staging_root, target_head=head, conflicted=True, detail=detail,
            fault="planner", conflicts=tuple(conflicts),
        )

    def _tasks_touching(self, base: str, head: str, path: str) -> list[str]:
        """The integrations on the target since `base` that changed `path`, by name.

        `--first-parent` walks the target's own history, whose commits are the
        `integrate(<ref>)` staging merges; anything else is named by its short sha.
        """
        if not base:
            return []
        log = self._git("log", "--first-parent", "--format=%h %s", f"{base}..{head}", "--", path)
        names: list[str] = []
        for line in log.stdout.splitlines():
            sha, _, subject = line.partition(" ")
            match = re.match(r"integrate\(([^)]+)\)", subject)
            name = match.group(1) if match else sha
            if name not in names:
                names.append(name)
        return names

    def discard_staging(self) -> None:
        """Remove the staging worktree if one is lying around. Safe to call twice."""
        if self.staging_root.exists():
            self._git("worktree", "remove", "--force", str(self.staging_root))
        if self.staging_root.exists():
            shutil.rmtree(self.staging_root, ignore_errors=True)
        self._git("worktree", "prune")

    # --- stage 3 ----------------------------------------------------------------------

    def verify_integration(
        self, staged: Staged, specs: Sequence[VerificationSpec], *, attempt_id: str
    ) -> list[VerificationEvidence]:
        """Run the mandatory integration checks **on the merged snapshot** (`§3.1`).

        The evidence is tied to `staged.merged_commit`, not to the candidate's commit.
        Two candidates that each pass alone can fail together, and evidence taken on
        either one separately says nothing about the merge — which is the entire reason
        this stage exists rather than trusting the candidate's own gate.
        """
        checker = CheckRunner(registry=self.registry, runner=self.runner, clock=self.clock)
        return checker.evaluate(
            specs, staged.path, attempt_id=attempt_id, snapshot=staged.merged_commit
        )

    # --- stage 4 ----------------------------------------------------------------------

    def fast_forward(self, staged: Staged) -> tuple[bool, str, str]:
        """The only write to the target branch in the entire system (`[INV-05]`).

        A compare-and-swap: `git update-ref <ref> <new> <expected-old>` fails if the ref
        moved while the integration was being verified, so a target that advanced during
        stage 3 forces re-integration on the new head instead of silently overwriting
        somebody else's merge. The check and the write are one git operation on purpose —
        reading the head and then writing it would be the same race with extra steps.
        """
        current = self.target_head()
        if current != staged.target_head:
            return False, (
                f"target branch advanced from {staged.target_head[:12]} to "
                f"{current[:12]} during integration — re-integrate on the new head"
            ), current
        proc = self._git(
            "update-ref", f"refs/heads/{self.target_branch}",
            staged.merged_commit, staged.target_head,
        )
        if proc.returncode != 0:
            return False, (
                f"compare-and-swap on refs/heads/{self.target_branch} refused: "
                f"{(proc.stderr or proc.stdout).strip()[:200]}"
            ), self.target_head()
        stale = self._follow_checkouts(staged.target_head, staged.merged_commit)
        if self.store is not None:
            self.store.append_event(
                EventType.TARGET_FAST_FORWARDED,
                f"branch:{self.target_branch}",
                payload={
                    "from": staged.target_head,
                    "to": staged.merged_commit,
                    "branch": self.target_branch,
                    **({"stale_checkouts": stale} if stale else {}),
                },
            )
        detail = "fast-forwarded"
        if stale:
            detail += "; checkout left on the old tree: " + "; ".join(stale)
        return True, detail, staged.merged_commit

    def _follow_checkouts(self, old: str, new: str) -> list[str]:
        """Bring every checkout of the target branch from `old` to `new`; the refusals.

        `update-ref` moves the branch but not a worktree that has it checked out, whose
        index then reads as a staged revert of the merge — and the next `git commit` there
        (graduation commits in the project checkout) commits that revert (P12 pilot finding
        D8). `read-tree -m -u` is git's own fast-forward of a checkout: it keeps local
        edits and refuses, touching nothing, if one would be overwritten.
        """
        listing = self._git("worktree", "list", "--porcelain")
        if listing.returncode != 0:
            return [f"git worktree list failed: {(listing.stderr or '').strip()[:200]}"]
        refusals, path = [], ""
        for line in listing.stdout.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree "):]
            elif line == f"branch refs/heads/{self.target_branch}" and path:
                proc = self._git("read-tree", "-m", "-u", old, new, cwd=Path(path))
                if proc.returncode != 0:
                    refusals.append(f"{path}: {(proc.stderr or proc.stdout).strip()[:200]}")
        return refusals

    # --- stage 5 ----------------------------------------------------------------------

    def run_side_effects(
        self, effects: Sequence[Mapping], *, attempt_id: str
    ) -> tuple[dict[str, str], list[str]]:
        """Intent -> execute -> probe -> receipt, per effect (`§3.1` step 5, `[INV-07]`).

        Each effect is `{name, run, probe, effect_class}` where `run` and `probe` are
        callables. The probe is what turns "the process exited non-zero" into a decision:
        an effect whose runner failed but whose probe confirms the external state is
        already correct has *happened*, and recording a receipt for it is honest, whereas
        retrying it is a duplicate deployment.
        """
        receipts: dict[str, str] = {}
        uncertain: list[str] = []
        for effect in effects:
            name = str(effect.get("name") or "effect")
            effect_class = str(effect.get("effect_class") or "external_side_effect")
            operation_id = new_operation_id()
            if self.store is not None:
                self.store.record_intent(
                    operation_id, attempt_id=attempt_id, effect_class=effect_class,
                    payload={"name": name}, status="dispatched", at=self.clock(),
                )
            runner_fn = effect.get("run")
            probe = effect.get("probe")
            try:
                exit_status = int(runner_fn() or 0) if callable(runner_fn) else 0
            except Exception as exc:  # noqa: BLE001 — an effect that raised may have landed
                exit_status = 1
                probe_ok = None
                if callable(probe):
                    try:
                        probe_ok = bool(probe())
                    except Exception:  # noqa: BLE001
                        probe_ok = None
                if not probe_ok:
                    uncertain.append(f"{name}: {type(exc).__name__}: {exc}")
                    if self.store is not None:
                        self.store.set_intent_status(operation_id, "failed")
                    continue
                exit_status = 0
            if exit_status != 0:
                probe_ok = bool(probe()) if callable(probe) else None
                if not probe_ok:
                    uncertain.append(
                        f"{name}: exited {exit_status} and "
                        + ("the postcondition probe could not confirm it"
                           if callable(probe) else "declares no postcondition probe (A05)")
                    )
                    if self.store is not None:
                        self.store.set_intent_status(operation_id, "failed")
                    continue
                exit_status = 0
            if self.store is not None:
                receipts[name] = self.store.record_receipt(
                    operation_id=operation_id, attempt_id=attempt_id,
                    exit_status=exit_status, payload={"name": name}, at=self.clock(),
                )
            else:
                receipts[name] = operation_id
        return receipts, uncertain

    # --- the whole pipeline -----------------------------------------------------------

    def integrate(
        self,
        *,
        candidate_ref: str,
        specs: Sequence[VerificationSpec],
        attempt_id: str,
        task_id: str,
        run_id: str,
        candidate_worktree: Path | None = None,
        effects: Sequence[Mapping] = (),
        review_dispositions: Sequence[Mapping] = (),
        required_criterion_ids: Sequence[str] | None = None,
        exemptions: Sequence[Mapping] = (),
        task_spec_revision: int = 1,
        policy_hash: str = "",
        keep_staging: bool = False,
        guard: Callable[[str, str], str] | None = None,
    ) -> IntegrationOutcome:
        """Stages 1-6 in order, stopping at the first that refuses.

        `guard(frozen_commit, target_head)` runs between freeze and staging and returns a
        refusal reason, or `""` to proceed (the deny-list and the risky-set floor,
        `merge.integration_guard`). A refusal stops at stage `guard`.

        Nothing before stage 4 can move the target branch, so every early return below
        leaves it exactly where it was found — `target_before == target_after` on every
        failure path, which is the invariant the integration tests assert directly
        against the ref rather than against this function's return value.
        """
        before = self.target_head()
        frozen = self.freeze_candidate(candidate_ref, worktree=candidate_worktree)
        if not frozen.commit:
            return IntegrationOutcome(
                stage="freeze", ok=False, target_before=before, target_after=before,
                reason=f"candidate ref {candidate_ref!r} does not resolve to a commit",
            )
        if not frozen.clean:
            return IntegrationOutcome(
                stage="freeze", ok=False, target_before=before, target_after=before,
                reason=(
                    f"candidate worktree still has {len(frozen.dirty)} uncommitted "
                    "change(s); a tree that is still being written cannot be frozen"
                ),
            )

        # A disposition bound to a subject (Dan's approval of a `kind: dan` check) speaks
        # only for that commit: an approval of any other tree than the one frozen here
        # discharges nothing.
        review_dispositions = [
            d for d in review_dispositions
            if not d.get("subject_sha") or d.get("subject_sha") == frozen.commit
        ]
        if guard is not None:
            refusal = guard(frozen.commit, before)
            if refusal:
                return IntegrationOutcome(
                    stage="guard", ok=False, target_before=before, target_after=before,
                    reason=refusal,
                )
        staged = self.prepare_staging(frozen, target_head=before)
        if not staged.ok:
            if not keep_staging:
                self.discard_staging()
            return IntegrationOutcome(
                stage="stage", ok=False, target_before=before, target_after=self.target_head(),
                reason=staged.detail or "staging merge failed",
                fault=staged.fault, conflicts=staged.conflicts,
            )

        deferred = [spec for spec in specs if is_post_merge_check(spec)]
        integration_specs = [spec for spec in specs if not is_post_merge_check(spec)]
        evidence = self.verify_integration(staged, integration_specs, attempt_id=attempt_id)
        decision = decide(
            task_id=task_id,
            run_id=run_id,
            specs=integration_specs,
            evidence=evidence,
            required_criterion_ids=required_criterion_ids,
            review_dispositions=review_dispositions,
            exemptions=exemptions,
            integrated_source_snapshot=staged.merged_commit,
            expected_snapshot=staged.merged_commit,
            task_spec_revision=task_spec_revision,
            policy_hash=policy_hash,
            at=self.clock(),
        )
        # A declared post-merge-only task may integrate a frozen, guarded tree, but
        # cannot produce an acceptance decision before those checks are discharged.
        deferred_only = not integration_specs and any(spec.required for spec in deferred)
        if not decision.accepted and not deferred_only:
            if not keep_staging:
                self.discard_staging()
            return IntegrationOutcome(
                stage="verify", ok=False, target_before=before, target_after=self.target_head(),
                reason=decision.reason, merged_commit=staged.merged_commit,
                resolved=staged.resolved,
                evidence=tuple(evidence), decision=decision,
            )

        moved, why, head_now = self.fast_forward(staged)
        if not moved:
            if not keep_staging:
                self.discard_staging()
            return IntegrationOutcome(
                stage="fast_forward", ok=False, target_before=before, target_after=head_now,
                reason=why, merged_commit=staged.merged_commit, evidence=tuple(evidence),
                resolved=staged.resolved,
            )

        receipts, uncertain = self.run_side_effects(effects, attempt_id=attempt_id)
        if uncertain:
            if not keep_staging:
                self.discard_staging()
            return IntegrationOutcome(
                stage="side_effects", ok=False, target_before=before, target_after=head_now,
                reason=(
                    "target branch advanced but external effect(s) are UNCERTAIN: "
                    + "; ".join(uncertain)
                    + ". Automatic retry is prohibited (A05) — escalate to the operator"
                ),
                merged_commit=staged.merged_commit, resolved=staged.resolved, evidence=tuple(evidence),
                receipts=receipts,
            )

        if deferred:
            if not keep_staging:
                self.discard_staging()
            return IntegrationOutcome(
                stage="integrated", ok=True, target_before=before, target_after=head_now,
                reason="integration complete; declared post-merge checks remain outstanding",
                merged_commit=staged.merged_commit, resolved=staged.resolved,
                evidence=tuple(evidence), receipts=receipts,
            )

        final = decide(
            task_id=task_id,
            run_id=run_id,
            specs=specs,
            evidence=evidence,
            required_criterion_ids=required_criterion_ids,
            review_dispositions=review_dispositions,
            exemptions=exemptions,
            required_effects=[str(effect.get("name") or "effect") for effect in effects],
            effect_receipts=receipts,
            integrated_source_snapshot=staged.merged_commit,
            expected_snapshot=staged.merged_commit,
            task_spec_revision=task_spec_revision,
            policy_hash=policy_hash,
            at=self.clock(),
        )
        if self.store is not None:
            final = commit_acceptance(self.store, self.lifecycle, final, at=self.clock())
        if not keep_staging:
            self.discard_staging()
        return IntegrationOutcome(
            stage="accepted" if final.accepted else "accept",
            ok=final.accepted,
            target_before=before,
            target_after=head_now,
            reason=final.reason,
            merged_commit=staged.merged_commit, resolved=staged.resolved,
            evidence=tuple(evidence),
            decision=final,
            receipts=receipts,
        )


# ── affected-check selection (`[P10]`, `§2.2`, Experiment E3) ───────────────────────
#
# Which required checks must re-run for a given change set. The answer is a *superset*
# rule, never a guess: a check is left out only when it declares its inputs completely
# and no changed path touches them. Everything that weakens that — an unknown change
# set, a change to something every check implicitly reads, a check whose dependencies
# are `unknown` — widens the selection to the whole required suite.
#
# Selection never discharges a criterion by itself. A check left out is only safe to
# skip because its existing evidence still matches all four fingerprints; `CheckRunner`
# re-derives those and misses on any byte change, so an internal behaviour edit under
# an unchanged public signature both selects the check *and* invalidates its evidence.

#: Paths every check reads whether it declares them or not: shared configuration,
#: fixtures and lockfiles (`P10` §2). A change to any of them expands to the full suite.
SHARED_INPUT_PATTERNS: tuple[str, ...] = (
    "pyproject.toml", "setup.cfg", "setup.py", "tox.ini", "pytest.ini", ".python-version",
    "**/conftest.py", "**/fixtures/**", "**/testdata/**",
    "uv.lock", "poetry.lock", "Pipfile.lock", "requirements*.txt", "constraints*.txt",
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "tsconfig.json",
    "Cargo.toml", "Cargo.lock", "go.mod", "go.sum",
)

#: `shadow`: the affected subset is computed and recorded, the full suite still runs.
#: `authoritative`: only the affected subset runs. Graduation is `qualify_affected()`'s.
AFFECTED_MODES = frozenset({"shadow", "authoritative"})


def _glob_regex(pattern: str) -> "re.Pattern[str]":
    """A path glob as a regex: `**` crosses directories, `*` and `?` do not.

    A pattern with no wildcard also matches everything beneath it, the same reading
    `_iter_dependency_files` gives a bare directory name.
    """
    pattern = pattern.strip().removeprefix("./")
    out: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif ch == "*":
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    body = "".join(out)
    if not any(c in pattern for c in "*?"):
        body += "(?:/.*)?"
    return re.compile(f"^{body}$")


def path_matches(path: str, patterns: Iterable[str]) -> bool:
    """Whether a repo-relative POSIX path falls under any of the globs."""
    norm = Path(path).as_posix().lstrip("/")
    if norm.startswith("./"):
        norm = norm[2:]
    return any(_glob_regex(p).match(norm) for p in patterns if p)


def changed_paths(
    worktree: Path, base: str, head: str = "HEAD", *, since_fork: bool = False,
    runner=subprocess.run,
) -> tuple[str, ...] | None:
    """Repo-relative paths that differ between `base` and `head`, or `None` if unknown.

    `None` is not "nothing changed": a failed diff means the change set could not be
    established, and `select_affected_checks` treats that as a full-suite change.
    Renames are reported on both sides (`--no-renames`), so a moved file touches the
    checks that read its old location as well as its new one. `since_fork` diffs from
    the merge base (`base...head`): what `head`'s own commits changed, even when `base`
    is a branch that moved on after the fork (GDT-14 review M2b).
    """
    span = [f"{base}...{head}"] if since_fork else [base, head]
    proc = git(
        ["diff", "--name-only", "--no-renames", "-z", *span],
        cwd=Path(worktree), runner=runner,
    )
    if getattr(proc, "returncode", 1) != 0:
        return None
    return tuple(sorted(p for p in (proc.stdout or "").split("\0") if p))


@dataclass(frozen=True)
class AffectedSelection:
    """Which checks a change set requires, and why each one was or was not chosen."""

    mode: str
    selected: tuple[str, ...]
    affected: tuple[str, ...]
    full_suite: bool
    reasons: Mapping[str, str]
    expansion_reason: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in AFFECTED_MODES:
            raise ValidationError(f"affected mode {self.mode!r} not in {sorted(AFFECTED_MODES)}")

    @property
    def skipped(self) -> tuple[str, ...]:
        """Required checks that will not run. Always empty in shadow mode."""
        return tuple(cid for cid in self.reasons if cid not in self.selected)

    def specs(self, specs: Iterable[VerificationSpec]) -> list[VerificationSpec]:
        chosen = set(self.selected)
        return [spec for spec in specs if spec.check_id in chosen]

    def to_dict(self) -> dict:
        """The JSON document the controller stores and a verification worker reads (W5)."""
        return {
            "mode": self.mode,
            "selected": list(self.selected),
            "affected": list(self.affected),
            "full_suite": self.full_suite,
            "reasons": dict(self.reasons),
            "expansion_reason": self.expansion_reason,
        }

    @classmethod
    def from_dict(cls, document: Mapping) -> "AffectedSelection":
        return cls(
            mode=str(document["mode"]),
            selected=tuple(str(c) for c in document.get("selected") or ()),
            affected=tuple(str(c) for c in document.get("affected") or ()),
            full_suite=bool(document.get("full_suite")),
            reasons={str(k): str(v) for k, v in (document.get("reasons") or {}).items()},
            expansion_reason=document.get("expansion_reason"),
        )


def select_affected_checks(
    specs: Sequence[VerificationSpec],
    changed: Iterable[str] | None,
    *,
    mode: str = "shadow",
    shared_patterns: Sequence[str] = SHARED_INPUT_PATTERNS,
) -> AffectedSelection:
    """The checks `changed` requires (`P10` §2), fail-closed in every direction.

    The full required suite is selected when the change set is unknown (`None`), when a
    changed path is shared config, a fixture or a lockfile, or when any required check
    has `unknown` dependency coverage — an unknown answer cannot be narrowed. Otherwise
    a check is affected when it is not cacheable (it proves nothing from history, so it
    always re-runs) or when a changed path matches one of its declared inputs.

    In `shadow` mode `affected` is computed and reported but `selected` is the full
    suite, which is how E3 accumulates the comparisons `qualify_affected` needs.
    """
    if mode not in AFFECTED_MODES:
        raise ValidationError(f"affected mode {mode!r} not in {sorted(AFFECTED_MODES)}")
    ordered = [spec.check_id for spec in specs]
    expansion: str | None = None
    paths: tuple[str, ...] = ()
    if changed is None:
        expansion = "change set unknown"
    else:
        paths = tuple(sorted({Path(p).as_posix() for p in changed if p}))
        shared = [p for p in paths if path_matches(p, shared_patterns)]
        if shared:
            expansion = f"shared input changed: {', '.join(shared)}"
        else:
            unknown = [
                s.check_id for s in specs
                if s.required and s.dependency_completeness == "unknown"
            ]
            if unknown:
                expansion = f"unknown dependency coverage: {', '.join(unknown)}"

    reasons: dict[str, str] = {}
    affected: list[str] = []
    for spec in specs:
        if expansion is not None:
            reasons[spec.check_id] = f"full suite: {expansion}"
            affected.append(spec.check_id)
            continue
        if not spec.cacheable:
            reasons[spec.check_id] = "not cacheable: always re-run"
            affected.append(spec.check_id)
            continue
        hits = [p for p in paths if path_matches(p, spec.input_dependencies)]
        if hits:
            reasons[spec.check_id] = f"input changed: {', '.join(hits)}"
            affected.append(spec.check_id)
        else:
            reasons[spec.check_id] = "no declared input changed"

    selected = tuple(ordered) if mode == "shadow" or expansion else tuple(affected)
    return AffectedSelection(
        mode=mode,
        selected=selected,
        affected=tuple(affected),
        full_suite=expansion is not None or len(selected) == len(ordered),
        reasons=reasons,
        expansion_reason=expansion,
    )


def invalidate_affected_evidence(store, selection: AffectedSelection) -> int:
    """Move every affected check's current evidence to `invalidated` (`[INV-A05]`).

    Belt and braces over the fingerprint rule: the input hash already makes stale
    evidence unmatchable, but a row left `current` is a row some other reader may trust.
    """
    if store is None:
        return 0
    return sum(store.invalidate_evidence(check_id=cid) for cid in selection.affected)


@dataclass(frozen=True)
class ShadowComparison:
    """One E3 observation: what affected selection chose, beside what the full suite said."""

    selection: AffectedSelection
    outcomes: Mapping[str, str]

    @property
    def discrepancies(self) -> tuple[str, ...]:
        """Checks the subset would have skipped that the full suite did *not* pass.

        That is the only direction that matters: a check selected but passing costs time;
        a check skipped but failing would have accepted a broken tree.
        """
        skipped = set(self.selection.reasons) - set(self.selection.affected)
        return tuple(
            sorted(cid for cid in skipped if self.outcomes.get(cid, "errored") != "passed")
        )

    def to_row(
        self, *, run_id: str, node_id: str, snapshot_id: str, attempt_id: str,
        recorded_at: str,
    ) -> dict:
        """A `shadow_comparisons` row (P12A W6), keyed by run, node and snapshot.

        The id is derived from that key, so re-ingesting the same result names the same
        row and the store's `INSERT OR IGNORE` keeps exactly one.
        """
        return {
            "comparison_id": "shadow-" + _digest([run_id, node_id, snapshot_id])[:32],
            "run_id": run_id,
            "node_id": node_id,
            "snapshot_id": snapshot_id,
            "attempt_id": attempt_id,
            "selection_json": json.dumps(self.selection.to_dict(), sort_keys=True),
            "outcomes_json": json.dumps(dict(self.outcomes), sort_keys=True),
            "discrepancies_json": json.dumps(list(self.discrepancies)),
            "recorded_at": recorded_at,
        }

    @classmethod
    def from_row(cls, row: Mapping) -> "ShadowComparison":
        return cls(
            selection=AffectedSelection.from_dict(json.loads(row["selection_json"])),
            outcomes={str(k): str(v) for k, v in json.loads(row["outcomes_json"]).items()},
        )


def load_shadow_comparisons(store) -> list[ShadowComparison]:
    """Every stored shadow comparison, oldest first — what `qualify_affected` judges (W6)."""
    if store is None:
        return []
    return [ShadowComparison.from_row(row) for row in store.shadow_comparisons()]


def qualify_affected(
    comparisons: Sequence[ShadowComparison], *, minimum_runs: int = 20
) -> tuple[str, str]:
    """E3's graduation rule: `(mode, reason)`.

    Authoritative only after `minimum_runs` shadow comparisons with zero discrepancies.
    One discrepancy anywhere in the record keeps selection in shadow mode — it means the
    declared dependencies are wrong for at least one check, and nothing about later clean
    runs repairs that.
    """
    bad = [c for c in comparisons if c.discrepancies]
    if bad:
        missed = sorted({cid for c in bad for cid in c.discrepancies})
        return "shadow", f"{len(bad)} shadow run(s) missed failing checks: {', '.join(missed)}"
    if len(comparisons) < minimum_runs:
        return "shadow", f"{len(comparisons)}/{minimum_runs} clean shadow runs recorded"
    return "authoritative", f"{len(comparisons)} clean shadow runs, no discrepancies"
