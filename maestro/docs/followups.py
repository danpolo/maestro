"""Review follow-ups, carried past graduation (Dan, 2026-09-26; P13 GDT-11 / W7).

A reviewer marks each finding blocking or follow_up (P12 pilot finding D16,
`workflows.handlers._split_findings`). Only blocking findings send work back; follow-ups
used to live only in the review artifact and the next reviewer's prior-rounds text, so an
accepted task's follow-ups were never done (DuetFlow 02 was accepted with 4, 03 with 5).

At graduation (`docs.roadmap.mark_roadmap_complete`, called by
`orchestrator._graph_graduate`), in the same commit that strips the task (`record`):

- every follow-up goes to the backlog (`maestro.backlog`), which the end-of-project
  polish stage works through: an ordinary or `<id>-followups` task's as `open`, a polish
  or triage task's born `deferred` (no chains);
- except, for an ordinary task, one the reviewer marked `before_dependents` (a task that
  depends on this one would build on the defect) while an open ROADMAP task depends on
  it: those queue ONE task, `<id>-followups`, in its place in the ROADMAP — autonomous,
  depending on the task, with its scope and resources, `serves: []`, marked
  `followups_of: <id>`, one acceptance criterion per follow-up — and each such dependent
  gains it in `deps` (a minimal, CAS-registered edit).

A follow-up task's writer and reviewer are held to a different bar (`contract`): every
item done or declined with a reason, no new scope.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import yaml

from maestro import taskgraph

#: The ROADMAP key that marks a follow-up task, naming the task it follows up.
MARKER = "followups_of"
#: A ROADMAP task in one of these states is not waiting for anything any more.
_SETTLED = ("complete", "superseded")

#: Parent keys the follow-up task keeps: where it works and what it holds, never what
#: Dan must do (`dispatch`, `questions`, `dan_action`) or when (`hold`).
_INHERITED = ("scope", "resources", "resource", "constraints", "risk", "fallback",
              "self_modifying")


@dataclass(frozen=True)
class FollowUp:
    """One follow-up a reviewer recorded, with where it came from."""

    text: str
    round: int              # 1-based review round across every run of the workflow
    node: str               # the review node that raised it
    commit: str = ""        # the commit that round reviewed, when recorded
    # GDT-11: a dependent would build on it, so it runs before them (`record`).
    before_dependents: bool = False

    def line(self) -> str:
        if self.round == 0:     # not a review's: a goal checkpoint's (P13 GDT-8)
            return f"({self.node}) {self.text}"
        where = f"review round {self.round}, {self.node}"
        if self.commit:
            where += f" @ {self.commit}"
        return f"({where}) {self.text}"


def followup_task_id(task_id: str) -> str:
    return f"{task_id}-followups"


def is_followup_task(task: dict | None) -> bool:
    return bool((task or {}).get(MARKER))


def _item_text(item: Any) -> str:
    """A follow-up as one string. The handlers store strings (rule/scope/verify already
    appended, `_split_findings`); a dict is tolerated with the same rendering."""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        text = str(item.get("finding") or "").strip()
        if not text:
            return json.dumps(item, sort_keys=True)
        return text + "".join(
            f" {label}: {str(item[key]).strip()}."
            for key, label in (("rule", "Rule"), ("scope", "Scope"), ("verify", "Verify"))
            if isinstance(item.get(key), str) and item[key].strip()
        )
    return json.dumps(item, sort_keys=True)


def collect(store, artifacts, workflow_id: str) -> list[FollowUp]:
    """Every follow-up the workflow's reviews raised, across all its runs, deduplicated.

    All runs, as `WorkflowRunner.review_history` reads them: a diagnoser's repair opens a
    successor run of the same workflow, and a canceled run's reviews still reviewed this
    task. A follow-up repeated in a later round (whitespace aside) is kept once, at the
    round that first raised it; it is `before_dependents` when any round marked it so
    (the disposition's `before_dependents`, `handlers.adopt_review`).
    """
    if artifacts is None or not workflow_id:
        return []
    flagged: set[str] = set()
    rows = store.conn.execute(
        "SELECT a.node_id, a.outcome_ref, a.manifest_artifact_id FROM attempts a"
        " JOIN task_runs r ON a.run_id = r.run_id"
        " WHERE r.workflow_id = ? AND a.outcome_ref IS NOT NULL ORDER BY a.rowid",
        (workflow_id,),
    ).fetchall()
    out: list[FollowUp] = []
    seen: set[str] = set()
    rounds = 0
    for row in rows:
        try:
            result = json.loads(artifacts.read(row["outcome_ref"]).decode("utf-8"))
        except Exception:  # noqa: BLE001 — an unreadable outcome says nothing
            continue
        review = (result.get("outputs") or {}).get("review") if isinstance(result, dict) else None
        if not isinstance(review, dict):
            continue
        rounds += 1
        commit = ""
        if row["manifest_artifact_id"]:
            try:
                manifest = json.loads(artifacts.read(row["manifest_artifact_id"]))
                commit = str(manifest.get("reviewed_commit") or "")[:12]
            except Exception:  # noqa: BLE001 — an unreadable manifest names no commit
                pass
        flagged.update(" ".join(_item_text(t).split()).lower()
                       for t in review.get("before_dependents") or ())
        for item in review.get("follow_ups") or ():
            text = _item_text(item)
            key = " ".join(text.split()).lower()
            if not text or key in seen:
                continue
            seen.add(key)
            out.append(FollowUp(text=text, round=rounds, node=str(row["node_id"]),
                                commit=commit))
    return [replace(i, before_dependents=True)
            if " ".join(i.text.split()).lower() in flagged else i for i in out]


def build_task(parent: dict, items: Sequence[FollowUp]) -> dict:
    """The follow-up task's ROADMAP block for `parent`, as a dict in document order."""
    parent_id = str(parent["id"])
    task: dict = {
        "id": followup_task_id(parent_id),
        "status": "open",
        "mode": "autonomous",
        "deps": [parent_id],
        "hold": False,
        # Maintenance: a format-2 goals gate holds a task that names no goal it serves.
        "serves": [],
        MARKER: parent_id,
        "title": f"Review follow-ups of {parent_id}",
        "short_desc": (
            f"Do or decline, with a reason, each follow-up {parent_id}'s reviewers "
            "recorded when they accepted it; no new scope."
        ),
    }
    for key in _INHERITED:
        if parent.get(key) not in (None, "", [], {}):
            task[key] = parent[key]
    # One criterion per follow-up: the brief carries criteria whole (never truncated),
    # to the writer and the reviewer alike.
    task["acceptance_criteria"] = [item.line() for item in items]
    # The gate re-runs the parent's own automatic checks, so the follow-ups cannot
    # regress what the parent was accepted on. `kind: dan` checks are the parent's, and
    # an `await:` check waits on an outside process the follow-ups do not start.
    checks = [
        dict(v) for v in parent.get("verifications") or ()
        if isinstance(v, dict) and v.get("kind") == "auto" and not v.get("await")
    ]
    if checks:
        task["verifications"] = checks
    task["notes"] = (
        f"Queued by maestro when {parent_id} was accepted: its reviewers raised these as "
        "follow-ups (non-blocking). Each acceptance criterion is one item, with the review "
        "round that raised it. Check first that each is still relevant."
    )
    return task


def render_section(task: dict, *, heading: bool) -> str:
    body = yaml.safe_dump(
        task, sort_keys=False, default_flow_style=False, allow_unicode=True, width=100,
    )
    head = f"### {task['id']} — {task.get('title', '')}\n\n" if heading else ""
    return f"{head}```yaml\n{body}```\n"


def queue_task(content: str, parent: dict, items: Sequence[FollowUp]) -> tuple[str, str] | None:
    """`content` with the follow-up task for `parent` placed after `parent`'s section.

    Placed there, the parent's strip (`complete.remove_task_section`) leaves it in the
    parent's place. None when `parent` is not in `content`, or its follow-up task already
    is (a graduation retried after a lost loop queues nothing twice).
    """
    from maestro.docs import complete

    task = build_task(parent, items)
    if any(str(t.get("id")) == task["id"] for t in taskgraph.scan_tasks(content)):
        return None
    span = complete.task_section_span(content, str(parent["id"]))
    if span is None:
        return None
    start, end, _ = span
    section = render_section(task, heading=content.startswith("### ", start))
    before, after = content[:end].rstrip("\n"), content[end:].lstrip("\n")
    joined = f"{before}\n\n{section}" + (f"\n{after}" if after else "")
    return joined, task["id"]


def register_in_cas(repo: Path, content: str, task_id: str) -> None:
    """Record the queued block as applied, when the project uses the `[P2]` CAS.

    Otherwise `taskgraph.drift` reads the new block as a hand edit ("added") and withholds
    it from dispatch. Only this block is recorded: a hand edit elsewhere still conflicts.
    """
    record = taskgraph.read_cas(repo)
    if record is None:
        return
    digest = taskgraph.parse_document(content).block_hashes().get(task_id)
    if digest is None:
        return
    data = record.to_json()
    data["tasks"][task_id] = digest
    taskgraph._atomic_write(taskgraph.cas_path(repo), json.dumps(data, indent=2) + "\n")


@dataclass(frozen=True)
class Recorded:
    """Where graduation put a task's follow-ups."""

    count: int
    queued_as: str = ""             # the `before_dependents` follow-up task's id
    backlog: int = 0                # `open` in the backlog, for the polish stage
    wired: tuple[str, ...] = ()     # the dependents that now wait for `queued_as`
    deferred: int = 0               # a polish or triage task's own: born `deferred`

    def summary(self) -> str:
        parts = []
        if self.queued_as:
            before = f" (before {', '.join(self.wired)})" if self.wired else ""
            parts.append(f"{self.count - self.backlog - self.deferred} queued as "
                         f"{self.queued_as}{before}")
        if self.backlog:
            parts.append(f"{self.backlog} in the backlog for the polish stage")
        if self.deferred:
            parts.append(f"{self.deferred} deferred (raised by a polish/triage review)")
        return f"{self.count} review follow-up(s): " + ", ".join(parts)


def _queue_before_dependents(repo: Path, roadmap: Path, task: dict, items: list[FollowUp],
                             journal: Callable[[str, str], None]
                             ) -> tuple[str, tuple[str, ...]] | None:
    """Queue `items` as `<id>-followups` and add it to the `deps` of every open ROADMAP
    task that depends on `<id>`; `(its id, the dependents wired)`, or None when no open
    task depends on `<id>` (running it early buys nothing) or `<id>` is not in the ROADMAP.

    A dependent whose block drifted from the `[P2]` CAS is not touched (its hand edit is
    never laundered into the record, as `reduce_roadmap_deps` does), nor is one whose
    `deps` spelling cannot be edited exactly; each is journaled
    `followups_dependents_not_wired`. Every block written is registered in the CAS."""
    parent_id = str(task["id"])
    task_id = followup_task_id(parent_id)
    content = roadmap.read_text(encoding="utf-8")
    dependents = [
        str(t["id"]) for t in taskgraph.scan_tasks(content)
        if str(t["id"]) not in (parent_id, task_id) and t.get("status") not in _SETTLED
        and parent_id in taskgraph.legacy_deps(t)
    ]
    if not dependents:
        return None
    queued = queue_task(content, task, items)
    if queued is not None:
        content = queued[0]
    elif not any(str(t.get("id")) == task_id for t in taskgraph.scan_tasks(content)):
        return None
    record = taskgraph.read_cas(repo)
    drifted: set[str] = set()
    if record is not None:
        hashes = taskgraph.parse_document(content).block_hashes()
        drifted = {d for d in dependents if record.tasks.get(d) != hashes.get(d)}
    content, added = taskgraph.add_document_deps(
        content, {d: [task_id] for d in dependents}, skip=drifted)
    if content != roadmap.read_text(encoding="utf-8"):
        roadmap.write_text(content, encoding="utf-8")
    for tid in (task_id, *added):
        register_in_cas(repo, content, tid)
    now = {str(t["id"]): t for t in taskgraph.scan_tasks(content)}
    wired = tuple(d for d in dependents if task_id in taskgraph.legacy_deps(now.get(d, {})))
    missed = [
        f"{d} (its block differs from the applied ROADMAP)" if d in drifted
        else f"{d} (its deps cannot be edited exactly)"
        for d in dependents if d not in wired
    ]
    if missed:
        journal("followups_dependents_not_wired", f"{task_id}: {'; '.join(missed)}")
    return task_id, wired


def record(repo: Path, roadmap: Path, task: dict | None, items: Iterable[FollowUp], *,
           journal: Callable[[str, str], None] | None = None) -> Recorded | None:
    """Put an accepted task's follow-ups where they get done, before graduation strips it.

    An ordinary task's `before_dependents` items with an open dependent queue
    `<id>-followups` before it (the ROADMAP); every other item goes to the backlog
    (`maestro.backlog`, `.orchestrator/backlog.json`): `open`, or `deferred` for a polish
    or triage task's own. A `-followups`, polish or triage task never queues another. The
    caller commits the ROADMAP with the strip; the backlog is not committed (gitignored).
    None when there is nothing to record.
    """
    from maestro import backlog

    items = list(items)
    if not items or not task or not task.get("id"):
        return None
    journal = journal or (lambda kind, detail: None)
    polish = backlog.is_polish(task) or backlog.is_triage(task)
    queued_as, wired, early = "", (), []
    flagged = [i for i in items if i.before_dependents]
    if flagged and not polish and not is_followup_task(task):
        placed = _queue_before_dependents(Path(repo), roadmap, task, flagged, journal)
        if placed is not None:
            (queued_as, wired), early = placed, flagged
    rest = [i for i in items if i not in early]
    if rest:
        ids = backlog.add(Path(repo), task, rest,
                          at=datetime.now(tz=timezone.utc).isoformat(timespec="seconds"))
        if ids:
            journal("backlog_recorded", f"{task['id']}: {', '.join(ids)}")
    return Recorded(count=len(items), queued_as=queued_as, wired=wired,
                    backlog=0 if polish else len(rest), deferred=len(rest) if polish else 0)


def contract(task: dict | None, *, reviewer: bool) -> str:
    """The follow-up task's bar, for its writer's or its reviewer's brief ('' otherwise);
    a triage or polish task's is `backlog.contract`'s (GDT-11)."""
    from maestro import backlog

    if backlog.is_triage(task) or backlog.is_polish(task):
        return backlog.contract(task, reviewer=reviewer)
    if not is_followup_task(task):
        return ""
    parent = task.get(MARKER)
    head = (
        f"FOLLOW-UP TASK: each acceptance criterion is one follow-up that {parent}'s "
        "reviewers recorded when they accepted it. "
    )
    if reviewer:
        return head + (
            "Approve only if every item is either done or explicitly declined with a "
            "reason in the implementer's report (its commit messages on this branch); an "
            "item neither done nor declined is a blocking finding. A decline with a sound "
            "reason is acceptable. Any change beyond the listed items is new scope: "
            "blocking. Anything else you notice is a follow_up."
        )
    return head + (
        "First check each is still relevant: later work may have fixed or removed it. "
        "Then do it, or decline it with a reason. Report every item, as done or "
        "declined: <reason>, in your final commit message and in DONE; the reviewer "
        "checks every item against that report. Add no new scope."
    )
