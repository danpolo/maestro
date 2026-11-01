"""Node-kind executors, the attempt inbox, and the legacy-sentinel facade (`[P3]`).

Three responsibilities that belong together because they are the two ends of one wire:

1. **The attempt inbox** (`AttemptInbox`) — the filesystem protocol between a controller
   that must not block and a worker that runs for hours. The worker writes three things
   into `.orchestrator/attempts/<attempt_id>/`: an *envelope* naming the PID that actually
   writes (step 4 of the controlled-operation protocol), *heartbeats* while it works
   (step 5), and a *receipt* when it stops (step 6). The controller only ever reads them.
   A file rather than a SQLite row because of `[INV-01]`: a worker is an external actor
   and may not write the control database, and because a receipt on disk is what makes
   crash recovery able to tell "died before doing the work" from "did the work, died
   before reporting it" (`§6` step 2).

2. **Supervised TMUX dispatch** (`TmuxDispatcher`) — `[INV-10]`. Every blocking thing the
   old event loop ran inline (`subprocess.run` for a script lane, an agent CLI, a test
   suite) is launched into a named window of the `agents` session and left there. Raw
   shell backgrounding is not an alternative that happens to be worse; it is forbidden,
   because a `&` child has no window to probe, no name to reconcile against and no pane
   to read when it hangs.

3. **The legacy-sentinel facade** (`read_sentinel`, `result_from_workspace`) — the five
   files the existing lanes speak in (`DONE`, `FAILED`, `PAUSED`, `INCOMPLETE`, `SWITCH`)
   mapped into typed `NodeResult` envelopes. The mapping is the interesting part and it
   is not one-to-one:

   * `DONE` is a *claim*, not an acceptance. It moves the node to `succeeded`; whether the
     task is accepted is `[INV-04]`'s business and happens two nodes later.
   * `PAUSED` is not a node outcome at all. The work has not finished — it is waiting on a
     human — so the node does not transition and the task moves to `waiting`.
   * `INCOMPLETE` is a resumable sitting: the node failed, the task waits for a cooldown,
     and `[INV-08]` means the attempt counter it carries is not reset by the re-queue.
   * `SWITCH` is a handover mid-work. The worker was told to checkpoint and exit *without*
     writing a terminal sentinel, so a workspace holding a `SWITCH` and nothing else is
     precisely the "lost worker during a side effect" case: `uncertain`, reconciled, never
     silently retried.

   Precedence matters and is asserted rather than assumed: a workspace can legitimately
   hold both a `SWITCH` (written by the controller) and a `DONE` (written by an
   implementer that finished before it read the sentinel). The terminal claim wins,
   because the work it describes actually happened.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shlex
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from maestro.workflows.failures import ROLE_DIAGNOSER, ROLE_REVIEWER
from maestro.workflows.models import NodeResult, NodeSpec, WorkflowError

# ── the legacy sentinel vocabulary ───────────────────────────────────────────────────

DONE_SENTINEL = "DONE"
FAILED_SENTINEL = "FAILED"
PAUSED_SENTINEL = "PAUSED"
INCOMPLETE_SENTINEL = "INCOMPLETE"
SWITCH_SENTINEL = "SWITCH"

#: Read order, and therefore precedence. Terminal claims first: an implementer that wrote
#: `DONE` did the work whatever the controller asked of it afterwards.
SENTINEL_PRECEDENCE = (
    DONE_SENTINEL,
    FAILED_SENTINEL,
    INCOMPLETE_SENTINEL,
    PAUSED_SENTINEL,
    SWITCH_SENTINEL,
)

#: Sentinel file -> `NodeResult.status`. See the module docstring for why these four
#: distinct statuses exist rather than a success/failure pair.
SENTINEL_STATUS = {
    DONE_SENTINEL: "succeeded",
    FAILED_SENTINEL: "failed",
    INCOMPLETE_SENTINEL: "incomplete",
    PAUSED_SENTINEL: "paused",
    SWITCH_SENTINEL: "handover",
}

#: `result.json` `status:` values an implementer may write, for the synthesis path where
#: the process left a result document but no sentinel (the zombie guard the legacy
#: reconcile loop grew). Anything unlisted is a failure — fail closed, never default to
#: success on an unreadable claim.
RESULT_JSON_STATUS = {
    "DONE": "succeeded",
    "FAILED": "failed",
    "BLOCKED": "failed",
    "NEEDS-REPLAN": "failed",
    "INCOMPLETE": "incomplete",
}


@dataclass(frozen=True)
class SentinelReading:
    """What a workspace says about itself, in the legacy vocabulary."""

    name: str | None
    text: str = ""
    synthesized: bool = False

    @property
    def status(self) -> str | None:
        return SENTINEL_STATUS.get(self.name) if self.name else None

    @property
    def present(self) -> bool:
        return self.name is not None


def read_sentinel(workspace: Path, *, synthesize: bool = False) -> SentinelReading:
    """The workspace's sentinel, by precedence; optionally synthesised from `result.json`.

    `synthesize=True` reproduces the legacy zombie guard: an implementer that wrote a
    result document and then died without dropping a sentinel is read from the document
    rather than adopted forever. It is off by default because synthesising a verdict is a
    recovery decision, and recovery decisions belong to the reconciler, not to every
    reader that happens to glance at a workspace.
    """
    workspace = Path(workspace)
    for name in SENTINEL_PRECEDENCE:
        path = workspace / name
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            return SentinelReading(name=name, text=text)
    if not synthesize:
        return SentinelReading(name=None)
    return _synthesize_from_result(workspace)


def _synthesize_from_result(workspace: Path) -> SentinelReading:
    result = workspace / "result.json"
    if not result.exists():
        return SentinelReading(name=None)
    try:
        status = str(json.loads(result.read_text(encoding="utf-8")).get("status", ""))
    except (OSError, json.JSONDecodeError, AttributeError):
        status = ""
    mapped = RESULT_JSON_STATUS.get(status.strip().upper())
    if mapped == "succeeded":
        name = DONE_SENTINEL
    elif mapped == "incomplete":
        name = INCOMPLETE_SENTINEL
    else:
        name = FAILED_SENTINEL
    detail = status.strip() or "no terminal status"
    return SentinelReading(
        name=name,
        text=f"synthesized from result.json (status={detail})",
        synthesized=True,
    )


def result_from_workspace(
    workspace: Path,
    *,
    run_id: str,
    node_id: str,
    attempt_id: str,
    synthesize: bool = False,
    writer_pid: int | None = None,
) -> NodeResult | None:
    """One legacy workspace -> one typed envelope, or `None` if it has not spoken yet.

    The `outputs` carry the workspace's own documents verbatim, so nothing downstream has
    to re-read the directory to learn what the implementer claimed.
    """
    reading = read_sentinel(workspace, synthesize=synthesize)
    if not reading.present:
        return None
    outputs: dict[str, object] = {
        "sentinel": reading.name,
        "workspace": str(workspace),
    }
    if reading.synthesized:
        outputs["synthesized_sentinel"] = True
    document = _read_result_json(Path(workspace))
    if document is not None:
        outputs["result"] = document
    return NodeResult(
        run_id=run_id,
        node_id=node_id,
        attempt_id=attempt_id,
        status=reading.status,
        detail=reading.text.strip()[:2000],
        outputs=outputs,
        exit_status=0 if reading.status == "succeeded" else 1,
        writer_pid=writer_pid,
        source="legacy_sentinel",
    )


def _read_result_json(workspace: Path) -> dict | None:
    try:
        return json.loads((workspace / "result.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_sentinel(workspace: Path, name: str, text: str = "") -> Path:
    """Write one legacy sentinel. The reverse direction of the facade.

    Kept here rather than spread across the lanes so that the set of files that can ever
    appear in a workspace is enumerable from one module — which is what makes
    `read_sentinel`'s precedence total rather than best-effort.
    """
    if name not in SENTINEL_STATUS:
        raise WorkflowError(f"{name!r} is not a legacy sentinel")
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    path = workspace / name
    path.write_text(text or name, encoding="utf-8")
    return path


# ── the reviewer's verdict (P12 pilot finding D6) ────────────────────────────────────

#: Where a reviewer writes its verdict, beside its sentinel. DONE says only that the review
#: ran; this says what it found, and the agent handler adopts nothing else from a reviewer.
REVIEW_FILE = "review.json"


def write_review(workspace: Path, verdict: str, findings: list | tuple = ()) -> Path:
    """Write a reviewer's verdict, as the reviewer brief asks a real agent to."""
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    path = workspace / REVIEW_FILE
    path.write_text(
        json.dumps({"verdict": verdict, "findings": list(findings)}), encoding="utf-8"
    )
    return path


#: P12 pilot finding D16: only a blocking finding fails a review; a follow-up is recorded.
SEVERITY_BLOCKING = "blocking"
SEVERITY_FOLLOW_UP = "follow_up"


def _split_findings(items: list, *, verdict: str) -> tuple[list[str], list[str]]:
    """A review's findings as (blocking, follow-ups), each rendered as one string."""
    blocking, follow_ups, _records, _flags = _read_findings(items, verdict=verdict)
    return blocking, follow_ups


def _read_findings(
    items: list, *, verdict: str, round_no: int | None = None, prior_ids=(),
) -> tuple[list[str], list[str], list[dict], list[str]]:
    """A review's findings as (blocking, follow-ups, records, origin flags).

    A finding is a string or `{"finding": ..., "severity": "blocking" | "follow_up"}`,
    optionally with `rule`, `scope` and `verify`, which are appended to its text.
    One with no severity is blocking under a rejecting verdict and a follow-up under an
    approval (the reviewer approved it as it is); an unknown severity is blocking.

    Each finding also becomes a structured record — its stable id, severity, text, and
    (Dan, 2026-09-26) its `origin` and `prior_finding` (`review_origins.normalise`) — so
    an export can count origins without parsing the display strings. The flags name each
    round-2+ blocking finding whose origin was missing or unusable; they never fail it.
    """
    from maestro.workflows import review_origins

    default = (
        SEVERITY_FOLLOW_UP if verdict.strip().lower() == "approved" else SEVERITY_BLOCKING
    )
    blocking, follow_ups, records, flags = [], [], [], []
    for n, item in enumerate(items, 1):
        severity = None
        if isinstance(item, dict):
            severity = item.get("severity")
            text = item.get("finding")
            if not isinstance(text, str) or not text.strip():
                rest = {k: v for k, v in item.items() if k != "severity"}
                text = json.dumps(rest, sort_keys=True)
            else:
                # Each round aims to be the last (Dan, 2026-09-26): the rule, its scope
                # and its check travel with the finding, still one string in the brief.
                text += "".join(
                    f" {label}: {str(item[key]).strip()}."
                    for key, label in (
                        ("rule", "Rule"), ("scope", "Scope"), ("verify", "Verify")
                    )
                    if isinstance(item.get(key), str) and item[key].strip()
                )
        else:
            text = item if isinstance(item, str) else json.dumps(item, sort_keys=True)
        severity = (
            str(severity).strip().lower().replace("-", "_") if severity is not None
            else default
        )
        if severity != SEVERITY_FOLLOW_UP:
            severity = SEVERITY_BLOCKING
        (follow_ups if severity == SEVERITY_FOLLOW_UP else blocking).append(text)
        fid = review_origins.finding_id(round_no, n)
        origin, prior, why = review_origins.normalise(
            item, fid=fid, severity=severity, round_no=round_no, prior_ids=prior_ids,
        )
        flags += why
        records.append({
            "id": fid, "severity": severity, "finding": text,
            "origin": origin, "prior_finding": prior,
        })
        # P13 GDT-11: a follow-up a dependent task would build on runs before it
        # (`docs.followups.record`); a blocking finding sends the task back anyway.
        if (severity == SEVERITY_FOLLOW_UP and isinstance(item, dict)
                and item.get("before_dependents") is True):
            records[-1]["before_dependents"] = True
    return blocking, follow_ups, records, flags


def adopt_review(ctx: "HandlerContext", result: NodeResult) -> NodeResult:
    """A reviewer's finished session, judged by its verdict rather than its DONE.

    Both report paths end here: the agent handler for a sentinel-reporting CLI, and the
    worker for a model-endpoint harness, whose reviewer writes the same file.

    Approved -> `succeeded` with the disposition in the outputs. A conclusive rejection
    -> `failed` as `verification_failed` carrying the findings, whose retry cone reaches
    back to the implementer (`failures.retry_cone`) and whose findings go into the
    implementer's rework brief (`WorkflowRunner.rework_note`). No verdict, an unreadable
    one, or a rejection without findings -> `failed` as `garbage_output`: `[INV-04]`, an
    empty review fails closed, and the reviewer is asked again.

    P12 pilot finding D16: only blocking findings decide (`_split_findings`). Follow-ups
    ride in the disposition's `follow_ups`, so the run's record keeps them either way.
    """
    from maestro import validation

    try:
        document = json.loads((Path(ctx.workspace) / REVIEW_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        document = None
    if not isinstance(document, dict):
        document = {}
    raw_verdict = str(document.get("verdict") or "")
    items = document.get("findings") if isinstance(document.get("findings"), list) else []
    round_no = ctx.inputs.get("review_round")
    round_no = round_no if isinstance(round_no, int) and round_no > 0 else None
    prior_ids = [str(i).lower() for i in ctx.inputs.get("prior_finding_ids") or ()]
    findings, follow_ups, records, flags = _read_findings(
        items, verdict=raw_verdict, round_no=round_no, prior_ids=prior_ids,
    )
    from maestro.workflows import goal_progress, review_origins

    # P13 W11a, GDT-6: the reviewer states each goal's impact and evidence; the rule
    # decides. A verified `regressed` on any goal and goal work the task does not ask for
    # (W9's out-of-scope finding, never widening the rework scope) are blocking findings
    # added here, so they are never approved and rework like any other. A served
    # `no_progress_unexpected` is a shortfall that merges (telemetry's unattributed
    # observation); an `unknown` goal's evidence to gather is a follow-up.
    goals_read = goal_progress.read(document, ctx.inputs.get("goals_check"))
    # GDT-7: a regressed goal in a conflict is Dan's question, not GDT-6's finding.
    regressed = [r for r in goals_read.records
                 if r["impact"] == "regressed" and not r.get("conflict")]
    synthesized = [(text, record, {"goal": record["goal"]})
                   for text, record in zip(goals_read.blocking, regressed)]
    synthesized += [(text, {}, {"kind": "out_of_scope"}) for text in goals_read.scope_findings]
    questions = _review_questions(document)
    from maestro.workflows.dan_questions import MAX_QUESTIONS_PER_ROUND

    room = max(0, MAX_QUESTIONS_PER_ROUND - len(questions))
    conflict_findings = list(goals_read.conflict_findings) + [
        f"goal conflict {q['conflict']['id']} waits for the next question round (at most "
        f"{MAX_QUESTIONS_PER_ROUND} at once)" for q in goals_read.questions[room:]]
    questions += goals_read.questions[:room]
    synthesized += [(text, {}, {"kind": "goal_conflict"}) for text in conflict_findings]
    for text, record, extra in synthesized:
        fid = review_origins.finding_id(round_no, len(records) + 1)
        origin, prior, why = review_origins.normalise(
            record, fid=fid, severity=SEVERITY_BLOCKING, round_no=round_no,
            prior_ids=prior_ids,
        )
        flags += why
        findings.append(text)
        records.append({"id": fid, "severity": SEVERITY_BLOCKING, "finding": text,
                        "origin": origin, "prior_finding": prior, **extra})
    verdict = raw_verdict
    if findings and raw_verdict.strip().lower() == "approved":
        verdict = "changes_requested"       # a blocking finding is never approved
    elif not findings and follow_ups and raw_verdict.strip().lower() in (
        "rejected", "changes_requested"
    ):
        verdict = "approved"                # D16: follow-ups alone do not fail the node
    disposition = validation.review_disposition(
        ctx.node.node_id, verdict, findings=findings, reviewer=ctx.node.node_id,
    )
    # The goals follow-ups join after the verdict: they never turn a verdict (D16).
    disposition["follow_ups"] = [*follow_ups, *goals_read.follow_ups]
    # GDT-11: which follow-ups run before the dependents, by their text (`followups.collect`).
    early = [r["finding"] for r in records if r.get("before_dependents")]
    if early:
        disposition["before_dependents"] = early
    # Dan, 2026-09-26: ids and origins, structured, so a later round can cite a finding
    # and the pilot export can count whose miss each round was (`review_origins`).
    disposition["round"] = round_no
    disposition["finding_records"] = records
    from maestro.workflows.telemetry import review_complexity

    complexity = review_complexity(document)
    disposition.update(complexity=complexity['complexity'],
                       rubric_version=complexity['rubric_version'],
                       complexity_flags=complexity['flags'])
    if flags:
        disposition["origin_flags"] = flags
    if goals_read.records:
        disposition["goal_progress"] = goals_read.records
    if goals_read.shortfalls:
        disposition["goal_shortfalls"] = goals_read.shortfalls
    if goals_read.flags:
        disposition["goal_progress_flags"] = goals_read.flags
    if goals_read.accepted:
        disposition["goal_conflicts_accepted"] = goals_read.accepted
    if verdict != raw_verdict:
        disposition["reviewer_verdict"] = raw_verdict
    conclusive, reason = validation.review_is_conclusive(disposition)
    outputs = {**dict(result.outputs or {}), "review": disposition}
    if questions:
        # D26: the round waits on Dan; its findings still go to the rework with his answer.
        outputs["dan_questions"] = questions
        return dataclasses.replace(
            result, status="failed", exit_status=1,
            detail=("asked Dan: " + " | ".join(q["question"] for q in questions))[:2000],
            outputs=outputs,
        )
    if conclusive:
        return dataclasses.replace(result, outputs=outputs)
    verdict = disposition["verdict"].strip().lower()
    rejected = verdict in ("rejected", "changes_requested") and disposition["findings"]
    kind = "verification_failed" if rejected else "garbage_output"
    evidence = (
        f"review {verdict}: " + "; ".join(disposition["findings"]) if rejected
        else f"no usable {REVIEW_FILE}: {reason}"
    )
    return dataclasses.replace(
        result, status="failed", exit_status=1, detail=evidence[:2000],
        outputs={**outputs, "failure": {"kind": kind, "evidence": evidence[:4000]}},
    )


#: P12 pilot finding D17: where a diagnoser writes its repair proposal, beside its sentinel.
#: DONE is its prose diagnosis; this is the part the controller can act on.
PLAN_REPAIR_FILE = "plan_repair.json"


def _adopt_plan_repair(ctx: "HandlerContext", result: NodeResult) -> NodeResult:
    """A diagnoser's finished session, carrying its `plan_repair.json` as `outputs.plan_repair`.

    The controller judges the proposal (`WorkflowRunner._verdict_refusal`, `_plan_repair`),
    not the handler. No file leaves the outputs without the key, which the controller
    refuses as "diagnoser returned no proposal"; a file that is not a JSON object is carried
    as its raw text, which `PlanRepairProposal.from_dict` refuses with its own reason.
    """
    path = Path(ctx.workspace) / PLAN_REPAIR_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return result
    try:
        proposal = json.loads(text)
    except json.JSONDecodeError:
        proposal = text
    outputs = {**dict(result.outputs or {}), "plan_repair": proposal}
    if isinstance(proposal, dict) and str(proposal.get("verdict") or "") == "spec_gap":
        # D26: the failure came from a case the spec never defined; Dan decides it.
        from maestro.workflows import dan_questions

        questions, error = dan_questions.parse({"questions": proposal.get("questions")})
        if questions:
            outputs["dan_questions"] = questions
        else:
            outputs["plan_repair"] = {**proposal, "questions_error": error}
    return dataclasses.replace(result, outputs=outputs)


# ── the attempt inbox ────────────────────────────────────────────────────────────────

ENVELOPE_FILE = "envelope.json"
HEARTBEAT_FILE = "heartbeat"
RECEIPT_FILE = "receipt.json"
RESULT_FILE = "result.json"
#: The names only the worker writes. A reader's workspace is this same directory, so its
#: write tool is refused them (`ToolBox.reserved`): a model that wrote `result.json` and
#: `receipt.json` would be reporting its own node.
INBOX_PROTOCOL_FILES = frozenset({ENVELOPE_FILE, HEARTBEAT_FILE, RECEIPT_FILE, RESULT_FILE})


@dataclass(frozen=True)
class AttemptInbox:
    """The per-attempt directory the worker writes and the controller reads.

    Every write is atomic (temp file + `os.replace`) for the same reason the artifact
    store's is: the controller reads these files from another process at an arbitrary
    moment, and a half-written receipt read during crash recovery would be indistinguishable
    from a worker that reported nonsense.
    """

    root: Path
    attempt_id: str

    @property
    def path(self) -> Path:
        return self.root / self.attempt_id

    def ensure(self) -> Path:
        self.path.mkdir(parents=True, exist_ok=True)
        return self.path

    # -- worker side --

    def bind_pid(self, pid: int | None = None, **extra) -> int:
        """Step 4: declare the PID that actually writes (`[INV-02]`).

        Defaults to `os.getpid()` of the calling process, which is the point: the worker
        names itself, so a launcher's PID can never end up here by accident.
        """
        pid = os.getpid() if pid is None else int(pid)
        self._write_json(ENVELOPE_FILE, {"writer_pid": pid, **extra})
        return pid

    def heartbeat(self, stamp: str) -> None:
        self.ensure()
        self._write_text(HEARTBEAT_FILE, stamp)

    def write_result(self, result: NodeResult) -> Path:
        self._write_json(RESULT_FILE, result.to_dict())
        return self.path / RESULT_FILE

    def write_receipt(
        self, *, operation_id: str, exit_status: int, payload: Mapping | None = None
    ) -> Path:
        """Step 6: the receipt. Written last, so its presence means the work is over."""
        self._write_json(
            RECEIPT_FILE,
            {
                "operation_id": operation_id,
                "attempt_id": self.attempt_id,
                "exit_status": int(exit_status),
                "payload": dict(payload or {}),
            },
        )
        return self.path / RECEIPT_FILE

    # -- controller side --

    def writer_pid(self) -> int | None:
        envelope = self._read_json(ENVELOPE_FILE)
        pid = (envelope or {}).get("writer_pid")
        return int(pid) if pid is not None else None

    def envelope(self) -> dict | None:
        return self._read_json(ENVELOPE_FILE)

    def last_heartbeat(self) -> str | None:
        try:
            return (self.path / HEARTBEAT_FILE).read_text(encoding="utf-8").strip()
        except OSError:
            return None

    def receipt(self) -> dict | None:
        return self._read_json(RECEIPT_FILE)

    def result(self) -> NodeResult | None:
        body = self._read_json(RESULT_FILE)
        if body is None:
            return None
        return NodeResult.from_dict(body)

    # -- primitives --

    def _write_json(self, name: str, body: Mapping) -> None:
        self._write_text(name, json.dumps(body, ensure_ascii=False, sort_keys=True))

    def _write_text(self, name: str, text: str) -> None:
        self.ensure()
        target = self.path / name
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)

    def _read_json(self, name: str) -> dict | None:
        try:
            return json.loads((self.path / name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None


def attempts_root(repo: Path) -> Path:
    return Path(repo) / ".orchestrator" / "attempts"


# ── supervised TMUX dispatch (`[INV-10]`) ────────────────────────────────────────────

TMUX_SESSION = "agents"


@dataclass(frozen=True)
class Dispatched:
    """What the controller learned by launching, and nothing more.

    Note what is *not* here: a writer PID. A window's own pane PID is the launcher's, and
    `[INV-02]` says a launcher PID in a lease is a defect. The real PID arrives later,
    from the worker, through the inbox.
    """

    window: str
    argv: tuple[str, ...]
    launched: bool
    detail: str = ""


def window_name(task_id: str, node_id: str) -> str:
    """`maestro-{task}-{node}`, tmux-safe.

    Names are for humans and for `tmux kill-window`. Nothing derives ownership from one —
    `§1` of the data contracts forbids it — which is why the inbox is keyed by
    `attempt_id` and the window name is never parsed back.
    """
    safe = lambda part: "".join(c if c.isalnum() or c in "-_" else "-" for c in str(part))
    return f"maestro-{safe(task_id)}-{safe(node_id)}"


class TmuxDispatcher:
    """Launches workers into named windows of the `agents` session.

    `runner` is a seam, not a convenience: the tests dispatch without a tmux server, and
    the alternative — a live tmux in the suite — would make the concurrency assertions
    depend on a daemon's scheduling.
    """

    def __init__(
        self,
        *,
        session: str = TMUX_SESSION,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ):
        self.session = session
        self.runner = runner

    def ensure_session(self) -> None:
        self.runner(
            ["tmux", "has-session", "-t", self.session],
            capture_output=True,
        )

    def window_exists(self, window: str) -> bool:
        proc = self.runner(
            ["tmux", "list-windows", "-t", self.session, "-F", "#{window_name}"],
            capture_output=True,
            text=True,
        )
        return window in (proc.stdout or "").splitlines()

    def dispatch(self, window: str, argv: list[str], *, cwd: Path | None = None) -> Dispatched:
        """`tmux new-window -t agents -n <window> '<command>'`.

        The command is `shlex.join`-ed rather than passed as a list because tmux takes one
        shell word for the window's command. That is the only shell involved: nothing here
        backgrounds with `&`, which `[INV-10]` forbids outright.
        """
        command = shlex.join(argv)
        cmd = ["tmux", "new-window", "-t", self.session, "-n", window]
        if cwd is not None:
            cmd += ["-c", str(cwd)]
        cmd.append(command)
        proc = self.runner(cmd, capture_output=True, text=True)
        ok = getattr(proc, "returncode", 1) == 0
        detail = "" if ok else str(getattr(proc, "stderr", "") or "")[:400]
        return Dispatched(window=window, argv=tuple(argv), launched=ok, detail=detail)

    def kill(self, window: str) -> None:
        self.runner(
            ["tmux", "kill-window", "-t", f"{self.session}:{window}"], capture_output=True
        )


# ── the handler registry ─────────────────────────────────────────────────────────────


@dataclass
class HandlerContext:
    """Everything a handler is allowed to see. Deliberately small.

    A handler receives a node, its bound inputs and the places it may write — not the
    control store. `[INV-01]`: a handler runs in the worker, and a worker never writes
    `control.sqlite3`.
    """

    run_id: str
    task_id: str
    node: NodeSpec
    attempt_id: str
    inbox: AttemptInbox
    workspace: Path
    worktree: Path | None = None
    inputs: Mapping[str, object] = field(default_factory=dict)
    params: Mapping[str, object] = field(default_factory=dict)
    dispatcher: TmuxDispatcher | None = None
    #: `[P4]`. Present ONLY for a node the controller executes in-process (`merge`,
    #: `accept`): those write the target branch and the acceptance transaction, which
    #: `[INV-05]` and `[INV-01]` reserve to the controller. It is `None` in every worker,
    #: and a handler that needs it must fail closed rather than proceed without it —
    #: which is exactly what a worker reaching for it would be doing.
    controller: object | None = None

    def result(self, status: str, **kwargs) -> NodeResult:
        return NodeResult(
            run_id=self.run_id,
            node_id=self.node.node_id,
            attempt_id=self.attempt_id,
            status=status,
            **kwargs,
        )


Handler = Callable[[HandlerContext], NodeResult]

_REGISTRY: dict[str, Handler] = {}


class UnknownHandler(WorkflowError):
    """A node names a handler nothing registered. Fails closed at pre-validation."""


def register(handler_ref: str) -> Callable[[Handler], Handler]:
    def decorate(fn: Handler) -> Handler:
        _REGISTRY[handler_ref] = fn
        return fn

    return decorate


def resolve(handler_ref: str) -> Handler:
    try:
        return _REGISTRY[handler_ref]
    except KeyError:
        raise UnknownHandler(f"no handler registered as {handler_ref!r}") from None


def is_registered(handler_ref: str) -> bool:
    return handler_ref in _REGISTRY


def registered_refs() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


# ── the node-kind executors ──────────────────────────────────────────────────────────
#
# Each of these runs *in the worker*, after `bind_pid`. They are thin on purpose: the
# engineering behaviour still lives in the legacy modules (`implementer.py`, `merge.py`,
# `gates.py`), and P3 moves *where and how* it is executed, not what it does (`D08`).


@register("maestro.handlers.agent")
def agent_handler(ctx: HandlerContext) -> NodeResult:
    """An agent session, adopted through its workspace.

    The agent CLI is opaque (`[INV-03]`) and reports through the workspace it was given,
    so the handler's whole job is to translate that workspace into an envelope.
    """
    asked = _adopt_question(ctx)
    if asked is not None:
        return asked
    result = result_from_workspace(
        ctx.workspace,
        run_id=ctx.run_id,
        node_id=ctx.node.node_id,
        attempt_id=ctx.attempt_id,
        synthesize=True,
        writer_pid=ctx.inbox.writer_pid(),
    )
    if result is None:
        # P13 W8: this used to be `uncertain`, which no tick ever decided. An agent that
        # exits without reporting produced unreadable work: a writer is repeatable from
        # its `start_sha` and a reader outright, so it is a bad output and is run again.
        detail = "agent workspace holds neither a sentinel nor a result document"
        return ctx.result(
            "failed", detail=f"no_sentinel: {detail}",
            outputs={"failure": {"kind": "no_sentinel", "evidence": detail}},
        )
    if ctx.node.agent_definition_ref == ROLE_REVIEWER and result.status == "succeeded":
        return adopt_review(ctx, result)
    if ctx.node.agent_definition_ref == ROLE_DIAGNOSER and result.status == "succeeded":
        return _adopt_plan_repair(ctx, result)
    return result


def _adopt_question(ctx: "HandlerContext") -> NodeResult | None:
    """An agent's `QUESTION` for Dan (P12 pilot finding D26), or `None` if it wrote none.

    Read before the sentinel: the agent writes QUESTION instead of DONE. A valid round is
    `failed` with the questions in `outputs.dan_questions`, which the runner turns into a
    wait on Dan rather than a failure (`WorkflowRunner._ask_dan`). A malformed one is an
    ordinary `garbage_output` that names the parse error and spends budget like any.
    """
    from maestro.workflows import dan_questions

    path = Path(ctx.workspace) / dan_questions.QUESTION_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    questions, error = dan_questions.parse(text)
    if error:
        evidence = f"unusable {dan_questions.QUESTION_FILE}: {error}"
        return ctx.result(
            "failed", exit_status=1, detail=evidence[:2000],
            outputs={"failure": {"kind": "garbage_output", "evidence": evidence[:4000]}},
        )
    return ctx.result(
        "failed", exit_status=1,
        detail=("asked Dan: " + " | ".join(q["question"] for q in questions))[:2000],
        outputs={"dan_questions": questions, "workspace": str(ctx.workspace)},
    )


def _review_questions(document: dict) -> list[dict]:
    """A review's `questions`, if the round is usable; a malformed round is dropped."""
    from maestro.workflows import dan_questions

    raw = document.get("questions")
    if not raw:
        return []
    questions, _error = dan_questions.parse({"questions": raw})
    return questions


@register("maestro.handlers.script")
def script_handler(ctx: HandlerContext) -> NodeResult:
    """`kind: script` — the zero-LLM lane, run as a supervised worker rather than inline.

    The legacy loop ran this with a blocking `subprocess.run(..., timeout=1800)` inside the
    controller's poll; the controller could do nothing else for up to half an hour. Same
    command, same exit-code reading, but the blocking call is now inside a worker process
    that the controller merely watches.
    """
    command = str(ctx.params.get("command") or "").strip()
    if not command:
        return ctx.result("failed", detail="`kind: script` task has no `run:` command")
    timeout = int(ctx.params.get("timeout_sec") or 1800)
    cwd = ctx.worktree or ctx.workspace
    try:
        proc = subprocess.run(
            command, shell=True, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return ctx.result(
            "failed", detail=f"script timed out after {timeout} s", exit_status=124
        )
    stdout = (proc.stdout or "")[-4000:]
    stderr = (proc.stderr or "")[-1000:]
    if proc.returncode == 0:
        outputs = {"stdout": stdout, "stderr": stderr}
        if ctx.worktree is not None and (ctx.worktree / ".git").exists():
            # The legacy loop committed a clean script's changes; merge refuses a tree
            # that holds uncommitted work, so the graph lane must too.
            from maestro.workflows import task_tree

            try:
                outputs["commit"] = task_tree.commit_all(
                    ctx.worktree, f"{ctx.task_id}: script task"
                )
            except task_tree.TaskTreeError as exc:
                return ctx.result("failed", detail=f"script_commit_failed: {exc}", outputs=outputs)
        return ctx.result("succeeded", detail="script exited 0", outputs=outputs)
    return ctx.result(
        "failed",
        detail=f"script exited {proc.returncode}: {stderr[:200]}",
        outputs={"stdout": stdout, "stderr": stderr},
        exit_status=proc.returncode,
    )


@register("maestro.handlers.human")
def human_handler(ctx: HandlerContext) -> NodeResult:
    """A step only a person can take. Never completes itself.

    This is the node the P3 acceptance criterion "manual prep tasks cannot graduate
    without explicit human action receipt" rests on. It returns `succeeded` only on Dan's
    own answer, read through the controller's operator channel (G7,
    `DanVerificationChannel.poll_action`); with no controller, no channel or no answer yet
    it reports `paused`, and the controller re-asks every tick.

    - Done -> `succeeded`, with the receipt (`acted_at`, `request_id`, `answer`,
      `dan_action`, `post_action_cmd`) in the outputs; the gate runs `post_action_cmd`.
    - Redo <reason> -> `failed` as `verification_failed`, so the retry cone re-runs the
      prep (whose brief carries the reason) and asks anew on the new attempt.
    - Abandon -> `failed` as `unknown`: not retried; the run settles failed.
    """
    waiting = {"action": ctx.params.get("action"), "task_id": ctx.task_id}
    controller = ctx.controller
    channel = getattr(controller, "dan_channel", None) if controller is not None else None
    if channel is None or not hasattr(channel, "poll_action"):
        return ctx.result(
            "paused",
            detail=str(ctx.params.get("action") or "awaiting operator action"),
            outputs=waiting,
        )
    step = controller.manual_step(ctx.node)
    worktree = controller.task_tree_path() or task_tree_path(controller)
    manual_contract = {}
    if step.get('step_command'):
        from maestro import manual_steps
        try:
            manual_contract = manual_steps.prepare(
                Path(controller.repo), run_id=ctx.run_id, attempt_id=ctx.attempt_id,
                tree=Path(worktree), command=step['step_command'],
                step_check_cmd=step.get('step_check_cmd', ''),
                expected_result=step.get('expected_result', ''),
            )
        except (ValueError, OSError) as exc:
            return ctx.result('failed', detail=f'manual contract: {exc}', exit_status=1)
        step['dan_action'] = 'bash ' + shlex.quote(manual_contract['script'])
    else:
        from maestro import manual_steps
        try:
            manual_contract = manual_steps.prepare_external(
                run_id=ctx.run_id, attempt_id=ctx.attempt_id, tree=Path(worktree),
                step_check_cmd=step.get('step_check_cmd', ''),
                expected_result=step.get('expected_result', ''),
            )
        except (ValueError, OSError) as exc:
            return ctx.result('failed', detail=f'manual contract: {exc}', exit_status=1)
    waiting = {
        **waiting, "action": step["dan_action"], "post_action_cmd": step["post_action_cmd"],
        "worktree": str(worktree or ""),
    }
    verdict = channel.poll_action(
        task_id=ctx.task_id, title=step.get("title", ""), run_id=ctx.run_id,
        attempt_id=ctx.attempt_id, action=step["dan_action"], worktree=str(worktree or ""),
        post_action_cmd=step["post_action_cmd"],
        **({'expected_result': step['expected_result']} if step.get('expected_result') else {}),
        **({'step_command': manual_contract['command']} if manual_contract.get('command') else {}),
    )
    if verdict is None:
        return ctx.result(
            "paused", detail=f"awaiting Dan's manual step: {step['dan_action']}"[:2000],
            outputs=waiting,
        )
    from datetime import datetime, timezone

    outputs = {
        **waiting, "answer": verdict.verdict, "reason": verdict.reason,
        "request_id": verdict.request_id,
    }
    if verdict.verdict == "done":
        outputs["receipt"] = {
            "acted_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "request_id": verdict.request_id, "answer": "done",
            "dan_action": step["dan_action"], "post_action_cmd": step["post_action_cmd"],
            'manual_contract': manual_contract,
        }
        return ctx.result("succeeded", detail="Dan performed the manual step", outputs=outputs)
    if verdict.verdict == "redo":
        evidence = f"dan_redo: {verdict.reason}"
        return ctx.result(
            "failed", detail=evidence[:2000], exit_status=1,
            outputs={**outputs, "failure": {"kind": "verification_failed", "evidence": evidence},
                     "extra_step_cause": verdict.reason if verdict.reason in (
                         "manual_redo", "brief_check_disagree") else None},
        )
    evidence = "dan_abandoned: Dan abandoned the manual step"
    return ctx.result(
        "failed", detail=evidence, exit_status=1,
        outputs={**outputs, "failure": {"kind": "unknown", "evidence": evidence}},
    )


def task_tree_path(controller) -> Path | None:
    """The task's plain tree, whether or not a gate has read it yet."""
    from maestro.workflows import task_tree

    repo = getattr(controller, "repo", None)
    revision = getattr(controller, "revision", None)
    if repo is None or revision is None:
        return None
    tree = task_tree.path_for(Path(repo), revision.task_id)
    return tree if tree.exists() else None


@register("maestro.handlers.wait")
def wait_handler(ctx: HandlerContext) -> NodeResult:
    """An external dependency with no process of ours to watch: an async job, a cooldown.

    Like `human`, it parks rather than blocks — the difference is only who eventually
    resolves it. With the controller, an async lane's `await_job` polls its awaited checks
    (`_await_job`, G7 1b); everything else stays paused.
    """
    if ctx.controller is not None and ctx.params.get("kind") == AWAITING_VERIFICATION:
        return _await_job(ctx)
    if ctx.controller is not None and ctx.params.get("cooldown_hours") is not None:
        return _cooldown(ctx)
    return ctx.result(
        "paused",
        detail=str(ctx.params.get("reason") or "waiting on an external dependency"),
        outputs={k: v for k, v in ctx.params.items()},
    )


#: The `await_job` node's `kind` param (compiler fragment `await_job`).
AWAITING_VERIFICATION = "awaiting-verification"
#: G7 1b: the least time between two runs of an async task's awaited checks. The node is
#: re-polled every controller tick; the checks themselves may be slow.
AWAIT_POLL_INTERVAL_SEC = 60.0


def _now_epoch() -> float:
    import time

    return time.time()


def _previous_outputs(ctx: HandlerContext) -> dict:
    """What this attempt reported last time it was polled (the inbox outlives a restart)."""
    previous = ctx.inbox.result()
    if previous is None or previous.status != "paused":
        return {}
    return dict(previous.outputs or {})


def _await_job(ctx: HandlerContext) -> NodeResult:
    """G7 1b: legacy `poll_awaiting_verifications` for the graph's async lane.

    Runs the task's `await: true` auto checks (`gates._classify_verifications`) in the task
    tree, else the repository, at most once per `AWAIT_POLL_INTERVAL_SEC`:

    - none pending and none failing -> `succeeded`; the gate then runs every check;
    - a hard failure (the awaited artifact landed and fails) -> `failed` with the check ids;
    - still pending after `gates.AWAIT_VERIFY_TIMEOUT_SEC` since the first poll ->
      `failed`, "timeout";
    - otherwise `paused`.

    Dan is told once, on the first poll, that the job is running (legacy's 🚀 message).
    """
    from maestro import gates
    from maestro.state import append_journal

    controller = ctx.controller
    task_id = str(ctx.params.get("task_id") or ctx.task_id)
    prev = _previous_outputs(ctx)
    now = _now_epoch()
    outputs = {
        **{k: v for k, v in ctx.params.items()},
        "await_started_epoch": float(prev.get("await_started_epoch") or now),
        "launch_notified": bool(prev.get("launch_notified")),
        "last_checked_epoch": prev.get("last_checked_epoch"),
        "pending": list(prev.get("pending") or ()),
    }
    if not outputs["launch_notified"]:
        channel = getattr(controller, "dan_channel", None)
        hours = gates.AWAIT_VERIFY_TIMEOUT_SEC // 3600
        if channel is not None and hasattr(channel, "notify"):
            channel.notify(
                f"🚀 {task_id}: async job launched — waiting for its awaited verification "
                f"(checked every minute, up to {hours} h). Nothing for you to do yet."
            )
        append_journal("async_job_awaiting", f"{task_id} run={ctx.run_id}")
        outputs["launch_notified"] = True
    last = outputs["last_checked_epoch"]
    if last is not None and now - float(last) < AWAIT_POLL_INTERVAL_SEC:
        return ctx.result("paused", detail="awaiting the async job's artifact", outputs=outputs)
    task = gates.get_task_by_id(task_id) or {}
    awaited = [
        v for v in task.get("verifications") or ()
        if isinstance(v, Mapping) and v.get("kind") == "auto" and v.get("await")
    ]
    from maestro.workflows import task_tree

    repo = Path(getattr(controller, "repo", None) or ".")
    tree = task_tree.path_for(repo, task_id)
    cwd = tree if tree.exists() else repo
    pending, hard = gates._classify_verifications(awaited, cwd)
    outputs["last_checked_epoch"] = now
    outputs["pending"] = list(pending)
    if hard:
        evidence = f"awaited check(s) failed: {', '.join(hard)}"
        append_journal("await_verify_failed", f"{task_id} run={ctx.run_id}: {', '.join(hard)}")
        return ctx.result(
            "failed", detail=evidence, exit_status=1,
            outputs={**outputs, "failure": {"kind": "verification_failed", "evidence": evidence}},
        )
    if not pending:
        append_journal("await_verify_passed", f"{task_id} run={ctx.run_id}")
        return ctx.result(
            "succeeded", detail="the awaited artifact landed; the gate checks it", outputs=outputs,
        )
    if now - outputs["await_started_epoch"] > gates.AWAIT_VERIFY_TIMEOUT_SEC:
        evidence = (
            f"timeout: still waiting on {', '.join(pending)} after "
            f"{gates.AWAIT_VERIFY_TIMEOUT_SEC // 3600} h"
        )
        append_journal("await_verify_timeout", f"{task_id} run={ctx.run_id}: {', '.join(pending)}")
        return ctx.result(
            "failed", detail=evidence, exit_status=1,
            outputs={**outputs, "failure": {"kind": "unknown", "evidence": evidence}},
        )
    return ctx.result(
        "paused", detail=f"awaiting {', '.join(pending)}", outputs=outputs,
    )


def _cooldown(ctx: HandlerContext) -> NodeResult:
    """G7 1c: legacy `parking.handle_incomplete` for the graph's resumable lane.

    Reached when the gate failed. Reads the progress the gate's checks printed
    (`merge._parse_progress`: `PROGRESS=n/m`, else the first `n/m`) and applies legacy's
    rules against `state.json["resume_state"][task]`:

    - unparseable progress, the sitting cap (`MAX_RESUME_ATTEMPTS`) or
      `MAX_RESUME_STALLS` sittings without progress -> `failed` (the run parks);
    - otherwise the sitting is recorded (`last_count`, `total`, `attempts+1`, `stalls`,
      `resume_after`) and the node `succeeded`. Accept is skipped, so the run settles
      failed; the loop tells Dan "progress saved" and reopens the task once
      `resume_after` has passed, on the kept `impl/<task>` branch.
    """
    from datetime import datetime, timedelta, timezone

    from maestro import parking
    from maestro.merge import _parse_progress
    from maestro.state import append_journal, read_state, write_state

    task_id = str(ctx.params.get("task_id") or ctx.task_id)
    cooldown_h = int(ctx.params.get("cooldown_hours") or parking.RESUME_COOLDOWN_H_DEFAULT)
    gate_text = ctx.controller.gate_failure_text()
    state = read_state()
    resume_all = dict(state.get("resume_state") or {})
    previous = dict(resume_all.get(task_id) or {})
    last = int(previous.get("last_count", -1))
    attempts = int(previous.get("attempts", 0))
    stalls = int(previous.get("stalls", 0))

    def park(reason: str) -> NodeResult:
        resume_all.pop(task_id, None)
        state["resume_state"] = resume_all
        write_state(state)
        append_journal("resume_parked", f"{task_id} run={ctx.run_id} {reason[:160]}")
        return ctx.result(
            "failed", detail=reason[:2000], exit_status=1,
            outputs={"failure": {"kind": "unknown", "evidence": reason[:2000]}},
        )

    progress = _parse_progress(gate_text)
    if progress is None:
        return park(f"gate progress unparseable: {gate_text[:160]}")
    done, total = progress
    if attempts >= parking.MAX_RESUME_ATTEMPTS:
        return park(f"resume attempt cap reached at {done}/{total} (attempts={attempts})")
    if done > last:
        stalls = 0
    else:
        stalls += 1
        if stalls >= parking.MAX_RESUME_STALLS:
            return park(f"no progress for {stalls} sittings at {done}/{total}")
    resume_after = (datetime.now(tz=timezone.utc) + timedelta(hours=cooldown_h)).isoformat()
    record = {
        "last_count": done, "total": total, "attempts": attempts + 1, "stalls": stalls,
        "resume_after": resume_after,
        "updated": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    resume_all[task_id] = record
    state["resume_state"] = resume_all
    write_state(state)
    pct = (100 * done // total) if total else 0
    append_journal(
        "task_incomplete",
        f"{task_id} {done}/{total} ({pct}%) run={ctx.run_id} attempt={attempts + 1} "
        f"cooldown={cooldown_h}h",
    )
    return ctx.result(
        "succeeded", detail=f"progress {done}/{total} saved; resuming after {resume_after}",
        outputs={**record, "cooldown_hours": cooldown_h, "percent": pct},
    )


@register("maestro.handlers.noop")
def noop_handler(ctx: HandlerContext) -> NodeResult:
    """A structural node — a join, a decision already made by the frontier."""
    return ctx.result("succeeded", detail=str(ctx.params.get("detail") or "structural node"))


# ── the `[P4]` nodes: evidence, staging, acceptance ──────────────────────────────────
#
# P3 registered these three as handlers that PARK, because a stub that returns
# `succeeded` manufactures an acceptance. They are real now, and the shape of each is
# decided by one question: who is allowed to do this?
#
#   gate   — runs declared checks on a frozen tree. Any supervised worker may; a full
#            test suite must not block the controller's tick.
#   merge  — writes the target branch. Controller only (`[INV-05]`).
#   accept — writes the acceptance transaction. Controller only (`[INV-01]`).


def _params(ctx: HandlerContext, *names, default=""):
    return [ctx.params.get(name, default) for name in names]


def _uncommitted_refusal(worktree: Path | None, *, diagnostics: dict | None = None) -> str:
    """Why the gate may not check `worktree`, or `""` when it may.

    The gate's evidence is bound to HEAD (`validation.snapshot_id`). A tree with
    uncommitted changes is not HEAD, so a pass there would name a commit that lacks what
    was checked, and merge's freeze would refuse it anyway, one node later. Refusing here
    parks the run at the gate, with no evidence recorded.
    """
    from maestro.workflows import task_tree

    if worktree is None or not task_tree.is_repository(worktree):
        return ""
    paths = task_tree.uncommitted(worktree)
    if paths is None:
        return f"uncommitted_changes: git status failed in {worktree}"
    if not paths:
        return ""
    if diagnostics is not None:
        diagnostics["extra_step_cause"] = "uncommitted_tree"
    shown = ", ".join(paths[:20]) + (f" (+{len(paths) - 20} more)" if len(paths) > 20 else "")
    return (
        f"uncommitted_changes: the task tree has changes no commit holds ({shown}); "
        "the gate checks commits only, so commit the work (or discard it) and rerun"
    )


def _run_post_action_cmd(command: str, tree: Path, *, output: dict | None = None) -> str:
    """Run a manual step's `post_action_cmd` in the task tree (G7, legacy
    `_finalize_manual_action`), or `""` when it exited 0. Its output is not committed."""
    import re

    from maestro import gates

    rcmd = re.sub(r"^(python3?)\b", str(gates.VENV_PYTHON), command)
    try:
        proc = subprocess.run(
            rcmd, shell=True, cwd=str(tree), capture_output=True, text=True, timeout=1800,
        )
    except subprocess.TimeoutExpired:
        if output is not None:
            output.update(command=command, exit_status=124, timed_out=True)
        return f"post_action_cmd timed out after 1800 s: {command}"
    if output is not None:
        output.update(command=command, exit_status=proc.returncode,
                      stdout=proc.stdout or '', stderr=proc.stderr or '')
    if proc.returncode == 0:
        return ""
    tail = ((proc.stdout or "") + (proc.stderr or ""))[-1500:]
    return f"post_action_cmd exited {proc.returncode}: {command}\n{tail}"


def _scope_refusal(ctx: HandlerContext, tree: Path) -> NodeResult | None:
    scope_policy = ctx.inputs.get("scope_check")
    if scope_policy is None:
        return None
    from maestro.workflows import scope_gate

    findings = scope_gate.findings(tree, scope_policy)
    if not findings:
        return None
    return ctx.result(
        "failed", detail=" | ".join(findings)[:2000], exit_status=1,
        outputs={"decision": "rejected", "scope_findings": findings,
                 "failure": {"kind": "verification_failed", "evidence": "\n".join(findings)[:4000]}},
    )


def _goals_guard_refusal(ctx: HandlerContext, findings: list[str],
                         outputs: Mapping | None = None) -> NodeResult | None:
    """GDT-6: the goals guard's blocking findings fail the gate like W9's, so the rework
    brief carries them."""
    if not findings:
        return None
    return ctx.result(
        "failed", detail=" | ".join(findings)[:2000], exit_status=1,
        outputs={**dict(outputs or {}), "decision": "rejected",
                 "goals_guard_findings": findings,
                 "failure": {"kind": "verification_failed", "evidence": "\n".join(findings)[:4000]}},
    )


@register("maestro.handlers.verification")
def verification_handler(ctx: HandlerContext) -> NodeResult:
    """Run this task's `VerificationSpec`s on the frozen worktree and report evidence.

    The evidence rows travel back in `outputs["evidence"]` rather than being written
    here: this runs in a worker, and a worker that could write `verification_evidence`
    could write itself a pass (`[INV-01]`). The controller ingests them when it commits
    the result.

    The node fails when the decision rejects. It does not park and it does not consult a
    sentinel — the implementer's `DONE` is not read anywhere in this function.

    With an `affected_selection` input (P12A W5) only its `selected` checks run, and the
    selection is echoed in the outputs so the controller can record the shadow comparison.
    In shadow mode `selected` is the full suite. Without the input, every check runs.
    """
    from maestro import gates
    from maestro.validation import AffectedSelection

    (task_id,) = _params(ctx, "task_id")
    tree = ctx.worktree or ctx.workspace
    manual_evidence = {}
    if ctx.inputs.get('manual_contract'):
        from maestro import manual_steps
        failed, manual_evidence = manual_steps.verify(dict(ctx.inputs['manual_contract']), tree)
        if failed:
            return ctx.result('failed', detail=failed[:2000], exit_status=1,
                              outputs={'manual_evidence': manual_evidence,
                                       'failure': {'kind': 'verification_failed', 'evidence': failed}})
    scoped = _scope_refusal(ctx, tree)
    if scoped is not None:
        return scoped
    post = str(ctx.inputs.get("post_action_cmd") or "").strip()
    finalizer_output = {}
    if post:
        failed = _run_post_action_cmd(post, tree, output=finalizer_output)
        if failed:
            return ctx.result("failed", detail=failed[:2000], exit_status=1,
                              outputs={'finalizer_output': finalizer_output,
                                       'manual_evidence': manual_evidence})
        # A finalizer may commit changes; qualify the resulting tree as well.
        scoped = _scope_refusal(ctx, tree)
        if scoped is not None:
            return scoped
    guard = ctx.inputs.get("goals_guard")
    if isinstance(guard, Mapping):
        from maestro.workflows import goal_guard

        tampered = _goals_guard_refusal(ctx, goal_guard.tamper_findings(tree, guard))
        if tampered is not None:
            return tampered
    refusal_outputs = {}
    refusal = _uncommitted_refusal(ctx.worktree, diagnostics=refusal_outputs)
    if refusal:
        return ctx.result("failed", detail=refusal[:2000], exit_status=1, outputs=refusal_outputs)
    document = ctx.inputs.get("affected_selection")
    selection = AffectedSelection.from_dict(document) if document else None
    gate_kwargs = {"check_ids": selection.selected} if selection is not None else {}
    check_output: dict = {}
    decision, evidence = gates.run_authoritative_gate(
        str(task_id), tree, attempt_id=ctx.attempt_id, check_output=check_output,
        **gate_kwargs,
    )
    outputs = {
        "evidence": [record.to_row() for record in evidence],
        **({'manual_evidence': manual_evidence} if manual_evidence else {}),
        **({'finalizer_output': finalizer_output} if finalizer_output else {}),
        # What each check printed, for the controller to store beside its row: the
        # diagnoser is owed "the verification output", not only its verdict (P12B F7).
        "check_output": check_output,
        "snapshot": decision.integrated_source_snapshot,
        "decision": decision.decision,
    }
    if selection is not None:
        outputs["affected_selection"] = selection.to_dict()
    if decision.accepted and isinstance(guard, Mapping):
        # GDT-6 (R22, R28): the criteria GREEN at the baseline rerun once the task's own
        # checks pass, whatever it serves; a lost GREEN reworks before any review.
        from maestro.workflows import goal_guard

        check = goal_guard.regression_check(tree, guard)
        if check.observed:
            outputs["acceptance"] = check.observed
        if check.notes:
            # A GREEN criterion this environment cannot run on the base either: no
            # verified degradation, so a note for the record, not a hold (01 §6.2).
            outputs["goals_guard_notes"] = check.notes
            # GDT-9 verification L4: a target already RED on the base is journalled, not sent.
            from maestro.state import append_journal

            for cid in goal_guard.red_on_base(check.notes):
                append_journal("goal_protected_red_on_base",
                               f"{task_id} run={ctx.run_id}: {cid} is RED on the task's "
                               "tree and on the base; no rework")
        if check.accepted:
            # GDT-7: regressions a stored decision of Dan's covers, for the record.
            outputs["goal_conflicts_accepted"] = check.accepted
        if check.questions:
            # GDT-7 (R48): a goal conflict is Dan's. The gate fails with his questions, which
            # the runner turns into a wait on him (`_screen_questions`); its blocking
            # findings ride along for the rework, and no check evidence is handed back.
            asked = {"acceptance": check.observed, "goals_guard_notes": check.notes,
                     "decision": "rejected", "dan_questions": list(check.questions)}
            if check.accepted:
                asked["goal_conflicts_accepted"] = check.accepted
            if check.findings:
                asked["goals_guard_findings"] = check.findings
            return ctx.result(
                "failed", exit_status=1, outputs=asked,
                detail=("asked Dan: " + " | ".join(
                    q["question"] for q in check.questions))[:2000])
        # Like W9's refusal, the rejected gate hands back no check evidence to ingest.
        regressed = _goals_guard_refusal(ctx, check.findings, {
            "acceptance": check.observed, "goals_guard_notes": check.notes,
            **({"goal_conflicts_accepted": check.accepted} if check.accepted else {})})
        if regressed is not None:
            return regressed
    deny_config = ctx.inputs.get("deny_list")
    if decision.accepted and isinstance(deny_config, Mapping):
        held = _deny_list_screen(ctx, str(task_id), tree, deny_config, outputs)
        if held is not None:
            return held
    return ctx.result(
        "succeeded" if decision.accepted else "failed",
        detail=decision.reason[:2000],
        exit_status=0 if decision.accepted else 1,
        outputs=outputs,
    )


def _deny_list_screen(
    ctx: HandlerContext, task_id: str, tree: Path, config: Mapping, outputs: dict,
) -> NodeResult | None:
    """P12 pilot finding D29: the merge guard's deny-list, run in the gate once the checks
    pass, so a hit is settled before any review round or Dan's real-world test.

    Every hit Dan approved passes (merge re-checks the same keys). A hit he disapproved
    fails the gate as `verification_failed`, the rework path back to the implementer, with
    the hit as a blocking finding. A hit with no ruling gets an agent's recommendation and
    goes to the controller as `deny_list_asks`, which holds the gate on Dan's tap.
    Returns `None` when the gate's own result stands (only `outputs` is annotated).
    """
    from maestro.workflows import deny_list

    screened = deny_list.screen(tree, config)
    outputs["deny_list"] = {
        "hits": screened["hits"], "approved": screened["approved"],
        **({"refusal": screened["refusal"]} if screened["refusal"] else {}),
    }
    if screened["disapproved"]:
        findings = [deny_list.finding(hit) for hit in screened["disapproved"]]
        evidence = "deny_list: " + " | ".join(findings)
        outputs["deny_list_disapproved"] = screened["disapproved"]
        outputs["extra_step_cause"] = "deny_disapproved"
        outputs["failure"] = {"kind": "verification_failed", "evidence": evidence[:4000]}
        return ctx.result("failed", detail=evidence[:2000], exit_status=1, outputs=outputs)
    if screened["unruled"]:
        from maestro.workflows.runner import _roadmap_task

        task = {"id": task_id, **_roadmap_task(task_id)}
        recommendations = deny_list.recommend(
            tree, task, screened["unruled"], screened["diff"],
            project_yaml=deny_list.project_yaml_text(),
        )
        outputs["deny_list_asks"] = deny_list.asks(screened["unruled"], recommendations)
        evidence = "deny_list: asking Dan about " + deny_list.summary(screened["unruled"])
        outputs["failure"] = {"kind": "dan_question", "evidence": evidence[:4000]}
        return ctx.result("failed", detail=evidence[:2000], exit_status=1, outputs=outputs)
    return None


@register("maestro.handlers.merge")
def merge_handler(ctx: HandlerContext) -> NodeResult:
    """Stages 1-4: freeze, stage, verify on the merged tree, fast-forward (`§3`).

    Refuses outright without a controller handle. That refusal is the load-bearing line
    in this function: everything else it does is delegation, and the one thing it must
    never do is perform a target-branch write from somewhere that is not the controller.
    """
    from maestro.merge import integrate_candidate

    controller = ctx.controller
    if controller is None:
        return ctx.result(
            "failed",
            detail=(
                "merge requires the controller: only the controller may write the target "
                "branch (INV-05). This node must be executed in-process, not dispatched."
            ),
            exit_status=2,
        )
    (task_id,) = _params(ctx, "task_id")
    branch = str(ctx.params.get("branch") or ctx.inputs.get("branch") or f"impl/{task_id}")
    outcome = integrate_candidate(
        str(task_id),
        branch,
        attempt_id=ctx.attempt_id,
        run_id=ctx.run_id,
        store=getattr(controller, "store", None),
        lifecycle=getattr(controller, "lifecycle", None),
        repo=getattr(controller, "repo", None),
        target_branch=str(ctx.params.get("target_branch") or "main"),
        specs=ctx.inputs.get("specs") or [],
        candidate_worktree=ctx.worktree,
        review_dispositions=list(ctx.inputs.get("review_dispositions") or []),
        proof_reviews=list(ctx.inputs.get("proof_reviews") or []),
        risky_globs=list(ctx.inputs.get("risky_globs") or []),
        approved_deny_hits=list(ctx.inputs.get("deny_list_approved") or []),
    )
    if outcome.stage == "guard":
        # P12 finding D10: a deny-list hit or an unreviewed risky edit. Nothing was staged
        # and the target never moved; the run parks on this node with the reason as detail.
        from maestro.state import append_journal

        append_journal("merge_refused", f"{task_id} run={ctx.run_id}: {outcome.reason}"[:600])
    if outcome.resolved or outcome.fault == "planner":
        # P12 finding D20: a trivial conflict resolved by rule (stage 3 re-verified it), or
        # one that was not, which is a planner fault — the tasks overlapped.
        from maestro.state import append_journal

        if outcome.resolved:
            append_journal("merge_resolved_deterministic",
                           f"{task_id} ref={branch} files={','.join(outcome.resolved)}"[:600])
        if outcome.fault == "planner":
            append_journal("merge_conflict_planner_fault", f"{task_id} {outcome.reason}"[:600])
    # `uncertain`, not `failed`, when an external effect could not be adjudicated: the
    # target branch has already moved and something may have happened downstream (A05).
    status = "succeeded" if outcome.ok else (
        "uncertain" if outcome.stage == "side_effects" else "failed"
    )
    # Observe typed integration evidence; an arbitrary fault/detail never proves cause.
    cause = None
    if not outcome.ok:
        if outcome.stage == "stage" and outcome.conflicts:
            cause = "parallel_scope_overlap"
        elif outcome.stage == "verify":
            causes = {record.extra_step_cause for record in outcome.evidence
                      if record.outcome != "passed"}
            if len(causes) == 1:
                observed = next(iter(causes))
                cause = "escaped_defect" if observed == "check_failed" else observed
    return ctx.result(
        status,
        detail=f"{outcome.stage}: {outcome.reason}"[:2000],
        exit_status=0 if outcome.ok else 1,
        outputs={
            "stage": outcome.stage,
            "target_before": outcome.target_before,
            "target_after": outcome.target_after,
            "merged_commit": outcome.merged_commit,
            "evidence": [record.to_row() for record in outcome.evidence],
            **({"extra_step_cause": cause} if not outcome.ok else {}),
            **({"resolved": list(outcome.resolved)} if outcome.resolved else {}),
            **({"fault": outcome.fault, "conflicts": [dict(c) for c in outcome.conflicts]}
               if outcome.fault else {}),
        },
    )


@register("maestro.handlers.acceptance")
def acceptance_handler(ctx: HandlerContext) -> NodeResult:
    """The deterministic acceptance decision, committed in one transaction (`§3.1` §6).

    By the time this node is ready the merge node has already integrated and recorded a
    decision, so this re-derives it from what is durable — the evidence rows for this run
    — rather than trusting the merge node's own report. That is not belt-and-braces: the
    merge node's report is a message from a previous node, and `decide()` reading the
    stored evidence is the difference between an acceptance backed by receipts and one
    backed by a message.
    """
    from maestro import validation

    controller = ctx.controller
    if controller is None:
        return ctx.result(
            "failed",
            detail="acceptance requires the controller (INV-01): it writes the control store",
            exit_status=2,
        )
    store = getattr(controller, "store", None)
    (task_id,) = _params(ctx, "task_id")
    rows = []
    for attempt in store.conn.execute(
        "SELECT attempt_id FROM attempts WHERE run_id = ?", (ctx.run_id,)
    ).fetchall():
        rows.extend(store.evidence_for_attempt(attempt["attempt_id"]))
    evidence = [validation.VerificationEvidence.from_row(row) for row in rows]
    specs = list(ctx.inputs.get("specs") or [])
    snapshot = str(ctx.inputs.get("snapshot") or "")
    decision = validation.decide(
        task_id=str(task_id),
        run_id=ctx.run_id,
        specs=specs,
        evidence=evidence,
        required_criterion_ids=[spec.check_id for spec in specs],
        integrated_source_snapshot=snapshot,
        # A named snapshot binds the evidence to it: a pass recorded on any other tree
        # (the pre-merge gate's, a stale run's) discharges nothing.
        expected_snapshot=snapshot or None,
        # Dan's approvals of `kind: dan` checks, already filtered by the controller to the
        # snapshot the gate passed (`WorkflowRunner.dan_dispositions`).
        review_dispositions=list(ctx.inputs.get("review_dispositions") or []),
    )
    decision = validation.commit_acceptance(
        store, getattr(controller, "lifecycle", None), decision
    )
    return ctx.result(
        "succeeded" if decision.accepted else "failed",
        detail=decision.reason[:2000],
        exit_status=0 if decision.accepted else 1,
        outputs={"decision": decision.decision, "decision_id": decision.decision_id},
    )


@register("maestro.handlers.dan_confirm")
def dan_confirm_handler(ctx: HandlerContext) -> NodeResult:
    """Dan confirms the task's `kind: dan` checks on the snapshot the gate passed.

    A controller node: it reads the run's gate outcome and asks through the loop's
    operator channel (`controller.dan_channel`, `maestro.hitl.verify`). It never approves
    itself — with no channel, or no answer yet, it reports `paused` and the controller
    re-asks next tick (`WorkflowRunner._repoll_operator_nodes`).

    - approved -> `succeeded`, one `review_disposition(check, "approved", reviewer="dan",
      subject_sha=<snapshot>)` per dan check in the outputs, which merge and accept read.
    - rejected -> `failed` as a `verification_failed` bad output with Dan's reason, so the
      retry cone re-runs the implementer (whose brief carries the reason) and the gate; the
      reworked tree is a new snapshot and gets a new request.
    """
    from maestro import validation

    controller = ctx.controller
    if controller is None:
        return ctx.result(
            "failed", detail="dan confirmation requires the controller", exit_status=2,
        )
    (task_id,) = _params(ctx, "task_id")
    checks = [dict(check) for check in ctx.params.get("dan_checks") or ()]
    operator = ctx.node.handler_ref == 'maestro.handlers.operator_check'
    if operator:
        snapshot, worktree = controller.operator_subject()
    else:
        snapshot, worktree = controller.verified_snapshot(), controller.task_tree_path()
    if not snapshot:
        return ctx.result(
            "failed", detail="dan_confirm: no gate snapshot in this run to ask Dan about",
            exit_status=2,
        )
    channel = getattr(controller, "dan_channel", None)
    waiting = {"task_id": task_id, "subject_sha": snapshot, "worktree": str(worktree or "")}
    def checkout_refusal():
        if operator and (worktree is None or validation.snapshot_id(Path(worktree)) != snapshot):
            # Drift is a controller/observation problem, not a deployed check failure.
            # End this attempt without a disposition or repair clone. A new run must
            # requalify its subject; an old request may never attest to a different tree.
            return ctx.result('failed', exit_status=1, outputs=waiting,
                              detail='operator checkout differs from verified subject; requalification required')
        return None

    refusal = checkout_refusal()
    if refusal is not None:
        return refusal
    if channel is None:
        return ctx.result(
            "paused",
            detail=f"awaiting Dan's verification of {snapshot[:12]} — no operator channel wired",
            outputs=waiting,
        )
    verdict = channel.poll(
        task_id=str(task_id), title=str(ctx.params.get("title") or ""), run_id=ctx.run_id,
        snapshot=snapshot, worktree=str(worktree or ""), checks=checks,
        **({'operator': True} if operator else {}),
    )
    refusal = checkout_refusal()
    if refusal is not None:
        return refusal
    if verdict is None:
        return ctx.result(
            "paused", detail=f"awaiting Dan's verification of {snapshot[:12]}", outputs=waiting,
        )
    findings = [] if verdict.approved else [verdict.reason]
    dispositions = [
        validation.review_disposition(
            check["id"], verdict.verdict, findings=findings, reviewer="dan",
            subject_sha=snapshot,
        )
        for check in checks
    ]
    outputs = {
        **waiting, "verdict": verdict.verdict, "reason": verdict.reason,
        "request_id": verdict.request_id, "dispositions": dispositions,
    }
    if verdict.approved:
        return ctx.result(
            "succeeded", detail=f"Dan approved {len(checks)} check(s) on {snapshot[:12]}",
            outputs=outputs,
        )
    evidence = f"dan_rejected on {snapshot[:12]}: {verdict.reason}"
    return ctx.result(
        "failed", detail=evidence[:2000], exit_status=1,
        outputs={**outputs, "failure": {"kind": "verification_failed", "evidence": evidence},
                 "post_merge_failure": bool(operator and controller.merged_snapshot()),
                 "extra_step_cause": verdict.reason if verdict.reason in (
                     "accept_rejected_review", "accept_rejected_plan") else None},
    )


@register('maestro.handlers.operator_check')
def operator_check_handler(ctx: HandlerContext) -> NodeResult:
    """Post-merge observation; failure requests a separate repair, never a merge replay."""
    return dan_confirm_handler(ctx)


@register('maestro.handlers.post_merge_checks')
def post_merge_checks_handler(ctx: HandlerContext) -> NodeResult:
    """Measure deployed behavior on the actual integrated checkout, never via Dan."""
    from maestro import gates, validation

    controller = ctx.controller
    if controller is None:
        return ctx.result('failed', detail='post-merge checks require the controller', exit_status=2)
    tree = Path(controller.repo)
    snapshot = controller.merged_snapshot()
    if not snapshot or validation.snapshot_id(tree) != snapshot:
        return ctx.result('failed', detail='post-merge checkout differs from integrated subject', exit_status=1)
    specs = [s for s in gates.authoritative_specs(ctx.task_id)
             if validation.is_post_merge_check(s) and not validation.is_dan_check(s)]
    registry = validation.EvidenceRegistry()
    evidence = validation.CheckRunner(registry).evaluate(
        specs, tree, attempt_id=ctx.attempt_id, snapshot=snapshot)
    decision = validation.decide(
        task_id=ctx.task_id, run_id=ctx.run_id, specs=specs, evidence=evidence,
        required_criterion_ids=[s.check_id for s in specs],
        integrated_source_snapshot=snapshot, expected_snapshot=snapshot)
    return ctx.result('succeeded' if decision.accepted else 'failed',
                      detail=decision.reason, exit_status=0 if decision.accepted else 1,
                      outputs={'evidence': [e.to_row() for e in evidence],
                               'check_output': registry.held_output, 'snapshot': snapshot,
                               'post_merge_failure': not decision.accepted})


@register("maestro.handlers.candidate_select")
def candidate_select_handler(ctx: HandlerContext) -> NodeResult:
    """The candidate join: N isolated attempts in, exactly one winner out (`[INV-06]`).

    A controller node, like `accept` and for the same reason — the decision is read off
    stored evidence, and a worker choosing its own winner would be a candidate grading
    itself (`[INV-01]`, `D06`). Without the controller it fails closed rather than
    proceeding on whatever the attempt inbox happened to contain.

    The inputs are one document per candidate id, assembled by the controller when it reaped
    the candidate attempts. Every candidate the graph composed must appear: a missing one
    becomes `abandoned` rather than vanishing, because a candidate set silently shrunk to
    one is a set that passes the isolation check by having nothing left to pollute.

    Refusing is an ordinary outcome here. No conclusive candidate is a failed node, not a
    winner picked from unproven work.
    """
    from maestro.workflows import candidates as candidate_join

    controller = ctx.controller
    if controller is None:
        return ctx.result(
            "failed",
            detail=(
                "candidate selection requires the controller (INV-01/INV-06): a candidate "
                "may not grade itself"
            ),
            exit_status=2,
        )
    (task_id,) = _params(ctx, "task_id")
    repo = Path(getattr(controller, "repo", "") or ".")
    nodes = [
        node
        for node in getattr(getattr(controller, "revision", None), "nodes", ()) or ()
        if (getattr(node, "params", {}) or {}).get(candidate_join.CANDIDATE_PARAM)
    ]
    specs = candidate_join.specs_from_nodes(repo, str(task_id), nodes)
    if not specs:
        return ctx.result(
            "failed",
            detail="candidate selection found no candidate nodes in the compiled revision",
            exit_status=2,
        )
    documents = dict(ctx.inputs.get("candidates") or {})
    outcomes = candidate_join.outcomes_from_documents(specs, documents)
    try:
        selection = candidate_join.select_winner(outcomes)
    except candidate_join.CandidateError as exc:
        return ctx.result("failed", detail=str(exc)[:2000], exit_status=1)
    return ctx.result(
        "succeeded",
        detail=selection.reason[:2000],
        outputs=selection.to_dict(),
    )
