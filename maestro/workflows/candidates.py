"""Multi-candidate exploration and the selection join (`[P7]`, `[INV-06]`, `D05`).

Free Quality Mode buys quality with inference that costs nothing, and the shape it buys is
*exploration*: two or more implementers attempt the same task independently, and the
controller keeps exactly one. That only works if the candidates cannot contaminate each
other, and "cannot" here is meant literally — this module derives each candidate's branch
and worktree from `maestro.confinement`, so isolation is a filesystem fact the sandbox
enforces rather than a convention the agents observe.

Three responsibilities, and nothing else:

* **Identity.** `CandidateSpec` is the one place a candidate's branch (`impl/<task>/c2`),
  worktree (`.orchestrator/worktrees/<task>/c2`) and commit boundary are spelled. The
  compiler's widened nodes carry only the `candidate_id`; everything else is derived, so a
  node and a worktree cannot disagree about which candidate they belong to.
* **Isolation, checked.** `assert_no_cross_pollution` refuses a candidate set that shares a
  worktree, a branch or a single piece of evidence. It is called *before* selection, not
  after: a winner chosen out of a polluted set is not a winner, and reporting the pollution
  after the merge would be reporting it after the damage.
* **Selection.** `select_winner` returns exactly one candidate, chosen from controller-
  executed evidence, with a deterministic tie-break. It fails closed — no candidate with
  conclusive evidence means *no winner*, never "the best of the inconclusive ones"
  (`[INV-04]`).

**What this module never does.** It does not merge, it does not combine, and it exposes no
way to. `[INV-06]` prohibits mixing patches, artifacts or proofs across candidates, and the
cheapest enforcement of that is an API in which the operation does not exist: `select_winner`
returns one `CandidateOutcome`, not a set, and the losers are named in the decision only so
an operator can see what was discarded.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from maestro import confinement
from maestro.workflows.models import WorkflowError

#: How a widened implementer node names the candidate it is (`compiler._with_fan_out_identity`).
CANDIDATE_PARAM = "candidate_id"

#: Outcome vocabulary. `abandoned` is distinct from `failed` on purpose: a candidate the
#: controller stopped (its sibling already won, the budget ran out) produced no verdict, and
#: counting it as a failure would make a cancelled exploration look like a quality signal.
CANDIDATE_STATUSES = frozenset({"succeeded", "failed", "abandoned"})


class CandidateError(WorkflowError):
    """A candidate set that cannot be trusted, or a join that cannot name a winner."""


class CrossPollutionError(CandidateError):
    """`[INV-06]`: two candidates shared something they may not share."""


class NoWinnerError(CandidateError):
    """No candidate had conclusive evidence. Fails closed (`[INV-04]`)."""


# ── identity ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CandidateSpec:
    """One candidate's place on disk and in git. Derived, never configured.

    `index` orders the candidates and nothing more — it is the deterministic tie-break in
    `select_winner`, so that two controllers handed the same evidence pick the same winner.
    """

    repo: Path
    task_id: str
    candidate_id: str
    index: int
    node_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "repo", Path(self.repo))
        if not self.candidate_id:
            raise CandidateError(f"{self.task_id}: a candidate needs a candidate_id")
        # Validated through `confinement`, not here: one spelling of what a usable branch
        # segment is, in the module that enforces the boundary built from it.
        confinement.branch_ref("impl", self.task_id, candidate=self.candidate_id)

    @property
    def branch(self) -> str:
        """`impl/<task>/<candidate>` — a directory *under* the task's namespace (`[INV-A08]`)."""
        return confinement.branch_name("impl", self.task_id, candidate=self.candidate_id)

    @property
    def worktree(self) -> Path:
        return confinement.worktree_for(self.repo, self.task_id, candidate=self.candidate_id)

    @property
    def resource_id(self) -> str:
        """The exclusive writer lease this candidate holds, as the compiler stamps it."""
        return f"worktree:{self.task_id}/{self.candidate_id}"

    def boundary(self) -> confinement.CommitBoundary:
        """The four paths this candidate may write, and nothing else (`§4.2`)."""
        return confinement.CommitBoundary.for_task(
            self.repo, self.task_id, candidate=self.candidate_id
        )

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "index": self.index,
            "node_id": self.node_id,
            "branch": self.branch,
            "worktree": str(self.worktree),
            "resource_id": self.resource_id,
        }


def candidate_specs(
    repo: Path, task_id: str, labels: Iterable[str], *, node_id: str = "implementer"
) -> tuple[CandidateSpec, ...]:
    """Specs for an explicit label sequence — `('c1', 'c2')`, the policy's own labels."""
    specs = []
    for index, label in enumerate(labels, start=1):
        specs.append(
            CandidateSpec(
                repo=repo, task_id=task_id, candidate_id=str(label), index=index,
                node_id=f"{node_id}_{label}",
            )
        )
    return tuple(specs)


def specs_from_nodes(repo: Path, task_id: str, nodes: Sequence[Any]) -> tuple[CandidateSpec, ...]:
    """Read the candidate set off a compiled graph, in node order.

    The graph is the authority on how many candidates exist — it was composed under one
    policy and one risk reading, and re-deriving the count from the policy at dispatch time
    would let a policy edit mid-run change the shape of a graph already running.
    """
    specs = []
    for node in nodes:
        label = str((getattr(node, "params", {}) or {}).get(CANDIDATE_PARAM) or "")
        if not label:
            continue
        specs.append(
            CandidateSpec(
                repo=repo, task_id=task_id, candidate_id=label, index=len(specs) + 1,
                node_id=str(getattr(node, "node_id", "") or ""),
            )
        )
    return tuple(specs)


# ── outcomes ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CandidateOutcome:
    """What one candidate produced: a status, a head commit, and controller-run evidence.

    `evidence` holds `validation.VerificationEvidence` — controller-executed checks against
    a frozen snapshot of *this* candidate's worktree. An agent's own report is not accepted
    here in any field, because `D06` does not accept it anywhere.
    """

    spec: CandidateSpec
    status: str
    evidence: tuple[Any, ...] = ()
    head_commit: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if self.status not in CANDIDATE_STATUSES:
            raise CandidateError(
                f"{self.spec.candidate_id}: unknown candidate status {self.status!r} "
                f"(expected one of {sorted(CANDIDATE_STATUSES)})"
            )
        object.__setattr__(self, "evidence", tuple(self.evidence))

    @property
    def candidate_id(self) -> str:
        return self.spec.candidate_id

    @property
    def evidence_ids(self) -> frozenset[str]:
        return frozenset(str(getattr(e, "evidence_id", "") or "") for e in self.evidence)

    @property
    def passed_checks(self) -> int:
        return sum(1 for e in self.evidence if getattr(e, "passed", False))

    @property
    def failed_checks(self) -> int:
        return sum(1 for e in self.evidence if not getattr(e, "passed", False))

    @property
    def conclusive(self) -> bool:
        """Whether this candidate is *eligible* to win — the whole of the fail-closed rule.

        Three conditions, none of which an agent can assert: the attempt succeeded, there is
        at least one piece of evidence, and every piece of it passed. A candidate with no
        evidence is not a candidate with a clean record; it is a candidate nothing checked.
        """
        return (
            self.status == "succeeded"
            and bool(self.evidence)
            and self.failed_checks == 0
            and bool(self.head_commit)
        )

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "status": self.status,
            "head_commit": self.head_commit,
            "branch": self.spec.branch,
            "passed_checks": self.passed_checks,
            "failed_checks": self.failed_checks,
            "conclusive": self.conclusive,
            "detail": self.detail,
        }


# ── isolation ────────────────────────────────────────────────────────────────────────


def assert_no_cross_pollution(outcomes: Sequence[CandidateOutcome]) -> None:
    """Refuse a candidate set whose members are not independent (`[INV-06]`).

    Four ways independence can be lost, all checked: a repeated candidate id, a shared
    worktree, a shared branch, and a shared piece of evidence. The last is the subtle one —
    two candidates can sit in different directories and still be graded by one test run, and
    a selection made from shared evidence is a selection between two descriptions of the same
    attempt.

    Raises rather than returning a verdict: there is no caller for whom "polluted but
    proceed" is a sensible next step.
    """
    issues: list[str] = []
    by_key: dict[str, dict[str, str]] = {"candidate_id": {}, "worktree": {}, "branch": {}}
    for outcome in outcomes:
        spec = outcome.spec
        for key, value in (
            ("candidate_id", spec.candidate_id),
            ("worktree", str(spec.worktree)),
            ("branch", spec.branch),
        ):
            earlier = by_key[key].get(value)
            if earlier is not None:
                issues.append(
                    f"candidates {earlier} and {spec.candidate_id} share {key} {value!r}"
                )
            else:
                by_key[key][value] = spec.candidate_id

    seen_evidence: dict[str, str] = {}
    for outcome in outcomes:
        for evidence_id in sorted(outcome.evidence_ids):
            if not evidence_id:
                continue
            earlier = seen_evidence.get(evidence_id)
            if earlier is not None and earlier != outcome.candidate_id:
                issues.append(
                    f"candidates {earlier} and {outcome.candidate_id} share evidence "
                    f"{evidence_id!r} — one check cannot grade two candidates"
                )
            seen_evidence[evidence_id] = outcome.candidate_id

    # A worktree nested inside another is a shared worktree with extra steps: the inner
    # candidate's writes are inside the outer one's tree, so the outer diff contains them.
    paths = sorted({(str(o.spec.worktree), o.candidate_id) for o in outcomes})
    for outer, outer_id in paths:
        for inner, inner_id in paths:
            if outer_id == inner_id or outer == inner:
                continue
            if Path(inner).is_relative_to(Path(outer)):
                issues.append(
                    f"candidate {inner_id}'s worktree is inside candidate {outer_id}'s"
                )
    if issues:
        raise CrossPollutionError("; ".join(sorted(set(issues))))


# ── selection ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Selection:
    """The join's decision: one winner, the discarded ids, and why.

    `losers` is a list of ids, deliberately not of outcomes — a caller holding the losing
    patches is a caller one line away from the mixing `[INV-06]` forbids.
    """

    winner: CandidateOutcome
    losers: tuple[str, ...] = ()
    reason: str = ""

    @property
    def branch(self) -> str:
        """The one branch the merge node may integrate."""
        return self.winner.spec.branch

    def to_dict(self) -> dict:
        return {
            "winner": self.winner.candidate_id,
            "winner_branch": self.branch,
            "winner_commit": self.winner.head_commit,
            "losers": list(self.losers),
            "reason": self.reason,
        }


def select_winner(outcomes: Sequence[CandidateOutcome]) -> Selection:
    """Choose exactly one candidate, or refuse (`[INV-06]`, `[INV-04]`).

    Isolation is checked first — a polluted set has no valid winner to choose. Then only
    *conclusive* candidates are considered, and the order is: most passed checks first, then
    lowest candidate index. The tie-break is an index rather than anything measured, because
    a tie means the evidence did not distinguish them and the only remaining requirement is
    that two controllers reading the same rows reach the same answer.
    """
    if not outcomes:
        raise NoWinnerError("candidate selection ran with no candidates")
    assert_no_cross_pollution(outcomes)

    eligible = [outcome for outcome in outcomes if outcome.conclusive]
    if not eligible:
        raise NoWinnerError(
            "no candidate has conclusive evidence ("
            + "; ".join(
                f"{o.candidate_id}: status={o.status} "
                f"passed={o.passed_checks} failed={o.failed_checks}"
                for o in outcomes
            )
            + ") — failing closed rather than merging an unproven candidate"
        )
    eligible.sort(key=lambda o: (-o.passed_checks, o.spec.index, o.candidate_id))
    winner = eligible[0]
    return Selection(
        winner=winner,
        losers=tuple(o.candidate_id for o in outcomes if o.candidate_id != winner.candidate_id),
        reason=(
            f"{winner.candidate_id} selected: {winner.passed_checks} passed checks, "
            f"0 failed; {len(eligible)} of {len(outcomes)} candidates were conclusive"
        ),
    )


def outcomes_from_documents(
    specs: Sequence[CandidateSpec], documents: Mapping[str, Mapping]
) -> tuple[CandidateOutcome, ...]:
    """Build outcomes for `specs` from `candidate_id -> document`, failing closed on a gap.

    A candidate the controller has no document for is `abandoned`, not absent: dropping it
    would silently shrink the candidate set, and a set of one is a set that cannot be
    polluted — which is the wrong way to pass `assert_no_cross_pollution`.
    """
    from maestro import validation

    built = []
    for spec in specs:
        document = documents.get(spec.candidate_id) or {}
        rows = document.get("evidence") or ()
        built.append(
            CandidateOutcome(
                spec=spec,
                status=str(document.get("status") or "abandoned"),
                evidence=tuple(
                    row if not isinstance(row, Mapping)
                    else validation.VerificationEvidence.from_row(row)
                    for row in rows
                ),
                head_commit=str(document.get("head_commit") or ""),
                detail=str(document.get("detail") or ""),
            )
        )
    return tuple(built)
