"""Free `/status` reader — zero model/Claude tokens; pure file reads.

Extracted verbatim from the reference `scripts/orchestrator_status.py` (231 lines): a
standalone, `sys.argv`-driven CLI script (`print_status()` / `print_json_status()` /
`main()`), not a function living inside `orchestrator_run.py`. Function bodies are
unchanged; only the import block and the derivation of the module-level path globals
differ — `STATE_JSON`/`USAGE_JSON`/`JOURNAL`/`ROADMAP` come from `maestro.paths.Paths.
from_env()` instead of `Path(__file__).resolve().parent.parent`, exactly as every other
extracted module does. Behavioural surprises are catalogued in `docs/FOUND_BUGS.md`; none
of them is fixed here.

`run_cli()` and `get_status_report()` are new wiring on top of the verbatim port, the same
shape as `maestro.verifications.run_cli`: `main()` above is left untouched, reading
`sys.argv` and printing exactly as the reference script does; `run_cli()` substitutes
`sys.argv` for the duration of the call and captures whatever `main()` prints instead of
letting it hit real stdout, so callers that used to shell out to this script as a
subprocess (`maestro.hitl.commands.run_status`, `maestro.parking.run_status`) can call
straight into it in-process instead. `get_status_report()` drives `run_cli(["--json"])` and
parses the captured stdout back into a dict, for callers that want the report as data
rather than text.

`[P11]` adds a second, clearly separated half below the banner: the operator projections
over the execution graph (`workflow_view`, `explain_view`, and the text/HTML renderings
`maestro workflow show` and `maestro explain` print). They read the `[P1]` control plane
rather than `state.json`, and they are here rather than in `maestro.workflows` because they
are the same thing this module already is — the free, zero-token way for a human to see what
the loop is doing — just over the graph instead of over the legacy projection files.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from maestro import taskgraph
from maestro.paths import Paths

_PATHS = Paths.from_env()

REPO       = _PATHS.repo
STATE_JSON = _PATHS.state
USAGE_JSON = _PATHS.usage
JOURNAL    = _PATHS.journal
ROADMAP    = REPO / "docs" / "ROADMAP.md"

#: Kept bound because it is part of this module's surface; the pattern itself, and the
#: tolerant reader that uses it, are `maestro.taskgraph`'s (P02).
_YAML_BLOCK = taskgraph.YAML_BLOCK_TRIMMED_RE


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _parse_roadmap_tasks() -> list[dict]:
    if not ROADMAP.exists():
        return []
    # `skip_comments=False`: this reader has never skipped `#` lines where
    # `maestro.docs.depmap`'s does, and the difference is visible (a commented-out `id:`
    # line is a task here and not there). Characterised, not unified — see
    # `maestro.taskgraph.manual_parse_block`.
    return taskgraph.scan_tasks_tolerant(ROADMAP.read_text(), skip_comments=False)


#: The hand-rolled fallback parser, shared with `maestro.docs.depmap` (P02).
def _manual_yaml(text: str) -> dict:
    return taskgraph.manual_parse_block(text, skip_comments=False)


def _fmt_epoch(epoch) -> str:
    if epoch is None:
        return "—"
    # Accept ISO strings (paused_until) or numeric epochs (resets_at).
    try:
        dt = datetime.fromisoformat(str(epoch).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError):
        pass
    try:
        dt = datetime.fromtimestamp(float(epoch), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(epoch)


def _pct(val) -> str:
    if val is None:
        return "—"
    try:
        return f"{float(val):.1f}%"
    except Exception:
        return str(val)


def _task_readiness(tasks: list[dict], active_ids: set[str]) -> list[dict]:
    done_ids: set[str] = set()
    rows = []
    for t in tasks:
        if t["id"] in active_ids:
            status = "ACTIVE"
        elif not t.get("deps") or all(d in done_ids for d in t["deps"]):
            if t.get("mode") == "needs-dan":
                status = "ready (needs-dan)"
            else:
                status = "READY"
        else:
            status = f"blocked on: {', '.join(t.get('deps', []))}"
        rows.append({"id": t["id"], "title": t.get("title", ""), "status": status,
                     "mode": t.get("mode", "?"), "eval": t.get("eval_relevance", "?")})
    return rows


def graph_runs(state: dict | None) -> list[dict]:
    """The graph runner's live runs from `state.json["graph_runs"]` (G4), oldest first.

    Kept apart from `in_flight`, whose rows are legacy tmux sessions; every live-work view
    reads both.
    """
    rows = (state or {}).get("graph_runs") or []
    return [r for r in rows if isinstance(r, dict) and r.get("task_id")]


def print_status() -> None:
    state = _load_json(STATE_JSON)
    usage = _load_json(USAGE_JSON)
    tasks = _parse_roadmap_tasks()

    print("=" * 60)
    print("  ORCHESTRATOR STATUS")
    print("=" * 60)

    if state is None:
        print("\n[!] .orchestrator/state.json not found — runtime not initialized.\n")
    else:
        phase = state.get("phase") or {}
        print(f"\nPhase:        {phase.get('id') or '—'}  {phase.get('title') or ''}")
        print(f"Status:       {phase.get('status', '?')}")
        print(f"Halted:       {'YES' if state.get('halted') else 'no'}")
        print(f"Paused until: {_fmt_epoch(state.get('paused_until'))}")
        print(f"Paused by user: {'yes' if state.get('paused_by_user') else 'no'}")
        blocked = state.get("blocked_on") or []
        if blocked:
            print(f"Blocked on:   {', '.join(blocked)}")
        print(f"Queue ref:    {state.get('queue_ref', '?')}")
        print(f"Updated at:   {state.get('updated_at', '?')}")

        in_flight = state.get("in_flight") or []
        print(f"\nIn-flight ({len(in_flight)}):")
        if not in_flight:
            print("  (none)")
        else:
            for r in in_flight:
                print(f"  [{r.get('status','?'):8}] {r.get('task_id','?')}  role={r.get('role','?')}"
                      f"  started={r.get('started_at','?')}  window={r.get('window') or '—'}")

        runs = graph_runs(state)
        if runs:
            print(f"\nGraph runs ({len(runs)}):")
            for r in runs:
                print(f"  [running ] {r['task_id']}  run={r.get('run_id', '?')}"
                      f"  started={r.get('started_at', '?')}"
                      f"  at={', '.join(r.get('nodes') or []) or '—'}")

        waiting = state.get("admission_waiting") or {}
        if waiting:
            print(f"\nWaiting on host resources ({len(waiting)}):")
            for task_id, why in sorted(waiting.items()):
                print(f"  {task_id}  {why.get('reason', '?')}: {why.get('detail', '')}"
                      f"  since={why.get('since') or '?'}")

        refused = state.get("fallback_refused") or {}
        if refused:
            print(f"\nFallback refused — running without step-down ({len(refused)}):")
            for task_id, reason in sorted(refused.items()):
                print(f"  {task_id}  {reason}")

    print("\n--- Usage (statusline sampler) ---")
    if usage is None:
        print("  .orchestrator/usage.json not found.")
    else:
        upd = usage.get("updated_at") or "never"
        model = usage.get("model") or "—"
        print(f"  Model:   {model}")
        print(f"  Updated: {upd}")
        print(f"  Context: {_pct(usage.get('context_used_pct'))}")
        fh = usage.get("five_hour") or {}
        print(f"  5h limit: {_pct(fh.get('used_pct'))}  resets {_fmt_epoch(fh.get('resets_at'))}")
        sd = usage.get("seven_day") or {}
        print(f"  7d limit: {_pct(sd.get('used_pct'))}  resets {_fmt_epoch(sd.get('resets_at'))}")

    print("\n--- Task queue (docs/ROADMAP.md) ---")
    if not tasks:
        print("  (no tasks or ROADMAP.md missing)")
    else:
        active_ids: set[str] = set()
        if state:
            for row in state.get("in_flight", []) or []:
                if isinstance(row, dict) and row.get("task_id"):
                    active_ids.add(row["task_id"])
            active_ids |= {r["task_id"] for r in graph_runs(state)}
            ph = state.get("phase") or {}
            if isinstance(ph, dict) and ph.get("status") == "in_progress" and ph.get("id"):
                active_ids.add(ph["id"])

        rows = _task_readiness(tasks, active_ids)
        for r in rows:
            status_col = r["status"].upper() if r["status"] in ("READY", "ACTIVE") else r["status"]
            flag = "*" if r["status"] == "ACTIVE" else (" " if "READY" in r["status"] else " ")
            print(f"  {flag} {r['id']:6}  [{r['mode']:9} | {r['eval']:14}]  {status_col}")

    print()

    if JOURNAL.exists():
        lines = JOURNAL.read_text().strip().splitlines()
        if lines:
            try:
                last = json.loads(lines[-1])
                print(f"Last journal event: [{last.get('ts','')}] {last.get('event','')} — {last.get('detail','')}")
            except Exception:
                pass
    print()


def admission_wait(task_id: str) -> dict | None:
    """Why the launch loop last left `task_id` waiting on host admission (`[P10B]`), if it did."""
    state = _load_json(STATE_JSON) or {}
    why = (state.get("admission_waiting") or {}).get(str(task_id))
    return dict(why) if isinstance(why, dict) else None


def print_json_status() -> None:
    state = _load_json(STATE_JSON)
    usage = _load_json(USAGE_JSON)
    tasks = _parse_roadmap_tasks()
    active_ids: set[str] = set()
    if state:
        for row in state.get("in_flight", []) or []:
            if isinstance(row, dict) and row.get("task_id"):
                active_ids.add(row["task_id"])
        active_ids |= {r["task_id"] for r in graph_runs(state)}
        ph = state.get("phase") or {}
        if isinstance(ph, dict) and ph.get("status") == "in_progress" and ph.get("id"):
            active_ids.add(ph["id"])
    print(json.dumps({
        "state": state,
        "usage": usage,
        "tasks": _task_readiness(tasks, active_ids),
    }, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="output machine-readable JSON")
    args = ap.parse_args()
    if args.json:
        print_json_status()
    else:
        print_status()
    return 0


def run_cli(argv: list[str]) -> tuple[int, str]:
    """In-process equivalent of `python orchestrator_status.py <argv...>`.

    Substitutes `sys.argv` for the duration of the call — `main()` above is left
    untouched, reading `sys.argv` exactly as the reference script does — and captures
    whatever it prints instead of letting it hit the real stdout. Returns
    (exit_code, captured_output), the in-process analogue of a subprocess's
    (returncode, stdout)."""
    old_argv = sys.argv
    buf = io.StringIO()
    try:
        sys.argv = ["orchestrator_status.py", *argv]
        with contextlib.redirect_stdout(buf):
            rc = main()
    finally:
        sys.argv = old_argv
    return rc, buf.getvalue()


def get_status_report() -> dict:
    """The `--json` report as a Python dict — the in-process analogue of parsing
    `orchestrator_status.py --json`'s stdout. Drives the same `run_cli` seam."""
    _rc, output = run_cli(["--json"])
    return json.loads(output)

# ══════════════════════════════════════════════════════════════════════════════════════
# [P11] Operator projections over the execution graph
#
# Everything above this banner is the verbatim reference `/status` reader and its in-process
# wiring. Everything below is new: two read-only projections of the `[P1]` control plane for
# a human — `maestro workflow show <task_id>` (what the graph is and where it got to) and
# `maestro explain <task_id>` (why the task was, or was not, accepted).
#
# Three properties they are built to keep, because an operator view that breaks any of them
# is worse than no view at all:
#
# 1. **Read-only, and a client, never the controller.** `open_active_store` opens a client
#    store; `[INV-01]` reserves every write to the controller. An operator looking at a graph
#    must not be able to move it.
# 2. **Derived, never re-decided.** A criterion is attributed to the nodes that satisfy it by
#    running the compiler's *own* `CRITERION_CHECKS` predicate, and acceptance is read out of
#    `acceptance_decisions` rather than recomputed. A view that re-derived a verdict could
#    disagree with the one the controller actually recorded, and the operator would have no
#    way to tell which was real.
# 3. **Structurally secret-free.** The export names ids, phases, fingerprints and timestamps;
#    it never embeds an artifact's content, a command line, an environment, or a worker's
#    output. `redact_secrets` then runs over every string that reaches the page anyway, as
#    the second line rather than the first — a redactor is a filter over what you decided to
#    include, not a licence to include everything.
# ══════════════════════════════════════════════════════════════════════════════════════


class WorkflowViewError(RuntimeError):
    """This project, task or run has no graph to show."""


#: Credential shapes scrubbed from anything rendered for an operator. Deliberately shape-based
#: (a Telegram bot token, an OpenAI/Anthropic-style key, a GitHub token, an AWS access key id,
#: a Slack token, an `Authorization: Bearer` header) plus one name-based rule for
#: `SOMETHING_TOKEN=value` assignments, which is what a leaked `.env` line looks like.
#:
#: Hex digests are deliberately *not* matched: a 40- or 64-character hex string is a git sha
#: or a fingerprint, which is exactly the evidence this view exists to show, and redacting it
#: would make the page useless while protecting nothing.
SECRET_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\b\d{9,}:[A-Za-z0-9_-]{30,}\b"),                       # telegram bot token
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),                            # openai / anthropic
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),                       # github
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                                 # aws access key id
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),                     # slack
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),               # authorization header
    re.compile(
        r"(?i)\b[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|CREDENTIAL)[A-Z0-9_]*"
        r"\s*[=:]\s*(?P<value>\"[^\"]*\"|'[^']*'|\S+)"
    ),
)

REDACTION = "[redacted]"


def redact_secrets(text: str) -> str:
    """Replace anything credential-shaped in `text` with `REDACTION`.

    The name-based rule keeps the key and redacts only the value, so a page still shows
    *that* a token was mentioned — an operator reading `TELEGRAM_BOT_TOKEN=[redacted]` learns
    something; one reading `[redacted]` learns nothing.
    """
    out = str(text)
    for pattern in SECRET_PATTERNS:
        if "value" in pattern.groupindex:
            out = pattern.sub(
                lambda m: m.group(0)[: m.start("value") - m.start(0)] + REDACTION, out
            )
        else:
            out = pattern.sub(REDACTION, out)
    return out


def _row_to_dict(row) -> dict:
    return {key: row[key] for key in row.keys()}


def _open_store(repo: Path | None = None):
    """A read-only client store for `repo`, or raise with the reason an operator can act on."""
    from maestro.control.store import open_active_store

    store = open_active_store(repo)
    if store is None:
        raise WorkflowViewError(
            "this project has no control database (.orchestrator/control.sqlite3) — "
            "nothing has run through the graph engine yet"
        )
    return store


def _task_runs(store, task_id: str) -> list[dict]:
    """Every run of `task_id`, oldest first; refuses a task with none.

    `started_at` has one-second resolution, so a repaired run can share its predecessor's
    stamp; `rowid` (insertion order) breaks that tie deterministically.
    """
    rows = [
        _row_to_dict(row)
        for row in store.conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY started_at, rowid", (task_id,)
        )
    ]
    if not rows:
        raise WorkflowViewError(f"task {task_id!r} has no run in this project's control plane")
    return rows


def _select_run(store, task_id: str, run_id: str | None) -> dict:
    rows = _task_runs(store, task_id)
    if run_id is None:
        return rows[-1]
    for row in rows:
        if row["run_id"] == run_id:
            return row
    raise WorkflowViewError(f"task {task_id!r} has no run {run_id!r}")


#: The columns `list_runs` reports for each run (`[P12A]` W11).
RUN_LIST_FIELDS = (
    "run_id", "workflow_id", "workflow_revision", "status", "started_at", "finished_at",
)


def list_runs(task_id: str, *, repo: Path | None = None) -> list[dict]:
    """Every run of `task_id` in start order — what `maestro workflow runs` prints.

    Plan repair (W7) opens a new run per revision, so a task with several runs is normal;
    this is the list `_select_run` chooses from, in the same order.
    """
    store = _open_store(repo)
    try:
        return [
            {key: row[key] for key in RUN_LIST_FIELDS} for row in _task_runs(store, task_id)
        ]
    finally:
        store.close()


def render_runs_text(rows: list[dict]) -> str:
    """One line per run: id, graph revision, status, start and finish."""
    lines = [
        f"{row['run_id']:<32} {row['workflow_id']}@{row['workflow_revision']:<4} "
        f"{row['status']:<10} started {row['started_at']}  "
        f"finished {row['finished_at'] or '—'}"
        for row in rows
    ]
    return redact_secrets("\n".join(lines) + "\n")


FAILURE_LIST_FIELDS = (
    "task_id", "run_id", "node_id", "attempt_id", "backend_id", "model_id", "binding_id",
    "failure_kind", "failure_class", "evidence", "retry_after_sec", "reset_at", "started_at",
)


def list_failures(task_id: str | None = None, *, kind: str | None = None,
                   repo: Path | None = None) -> list[dict]:
    from maestro.workflows.failures import FAILURE_KINDS

    if kind is not None and kind not in FAILURE_KINDS:
        raise WorkflowViewError(f"unknown failure kind {kind!r}")
    store = _open_store(repo)
    try:
        query = ("SELECT a.*, r.task_id FROM attempts a"
                 " JOIN task_runs r ON a.run_id = r.run_id"
                 " WHERE a.failure_kind IS NOT NULL")
        params: list = []
        if task_id is not None:
            query += " AND r.task_id = ?"
            params.append(task_id)
        if kind is not None:
            query += " AND a.failure_kind = ?"
            params.append(kind)
        query += " ORDER BY a.started_at, a.rowid"
        rows = []
        for row in store.conn.execute(query, params):
            d = _row_to_dict(row)
            failure = json.loads(d["failure_json"]) if d.get("failure_json") else {}
            rows.append({
                "task_id": d["task_id"], "run_id": d["run_id"], "node_id": d["node_id"],
                "attempt_id": d["attempt_id"], "backend_id": d["backend_id"],
                "model_id": d["model_id"], "binding_id": d["binding_id"],
                "failure_kind": d["failure_kind"], "failure_class": d["failure_class"],
                "evidence": redact_secrets(failure.get("evidence", "")),
                "retry_after_sec": failure.get("retry_after_sec"),
                "reset_at": failure.get("reset_at"), "started_at": d["started_at"],
            })
        return rows
    finally:
        store.close()


def render_failures_text(rows: list[dict]) -> str:
    lines = [
        f"{row['task_id']:<14} {row['node_id']:<20} {row['failure_kind']:<20} "
        f"({row['failure_class']}) {row['backend_id'] or '—'}/{row['model_id'] or '—'} "
        f"— {row['evidence'][:80]}"
        for row in rows
    ]
    return redact_secrets("\n".join(lines) + "\n")


def _layer_of(revision) -> dict[str, int]:
    """Longest-path depth per node — the column each node is drawn in.

    Longest path rather than shortest, so a node never renders to the left of something it
    depends on; the compiled graph is a DAG (`WorkflowRevision.__post_init__` refuses a
    cycle), so `topological_order()` is enough to compute it in one pass.
    """
    depth: dict[str, int] = {}
    for node_id in revision.topological_order():
        incoming = revision.incoming(node_id)
        depth[node_id] = 0 if not incoming else 1 + max(
            depth.get(edge.source_node_id, 0) for edge in incoming
        )
    return depth


def workflow_view(task_id: str, *, repo: Path | None = None, run_id: str | None = None) -> dict:
    """Everything `workflow show` and `explain` render, as one JSON-shaped document.

    One read of the control plane, one shape, three renderings (text, HTML, explain), so the
    three cannot drift apart or disagree about what the run did. The latest run is chosen by
    default; `run_id` picks an earlier one.
    """
    from maestro.workflows.compiler import load_revision

    store = _open_store(repo)
    try:
        run = _select_run(store, task_id, run_id)
        revision = load_revision(store, run["workflow_id"], int(run["workflow_revision"]))
        if revision is None:
            raise WorkflowViewError(
                f"run {run['run_id']} names workflow {run['workflow_id']}"
                f"@{run['workflow_revision']}, which is not stored in this control plane"
            )
        rid = run["run_id"]

        phases = {
            row["node_id"]: _row_to_dict(row)
            for row in store.conn.execute("SELECT * FROM node_states WHERE run_id = ?", (rid,))
        }
        attempts_by_node: dict[str, list[dict]] = {}
        for ordinal, row in enumerate(store.conn.execute(
            "SELECT * FROM attempts WHERE run_id = ? ORDER BY rowid", (rid,)
        )):
            attempt = _row_to_dict(row)
            attempt["_order"] = ordinal
            attempt["evidence"] = [
                {
                    "evidence_id": e["evidence_id"], "check_id": e["check_id"],
                    "outcome": e["outcome"], "validity": e["validity"],
                    "exit_status": e["exit_status"], "recorded_at": e["recorded_at"],
                    "source_snapshot_id": e["source_snapshot_id"],
                    "input_fingerprint": e["input_fingerprint"],
                    "environment_fingerprint": e["environment_fingerprint"],
                    "command_hash": e["command_hash"],
                    "reused_from": e["reused_from"], "reuse_reason": e["reuse_reason"],
                }
                for e in store.evidence_for_attempt(row["attempt_id"])
            ]
            attempts_by_node.setdefault(row["node_id"], []).append(attempt)

        layers = _layer_of(revision)
        nodes = []
        for node_id in revision.topological_order():
            node = revision.node(node_id)
            state = phases.get(node_id, {})
            nodes.append({
                "node_id": node_id,
                "kind": node.kind,
                "role": node.agent_definition_ref,
                "handler_ref": node.handler_ref,
                "effect_class": node.effect_class,
                "permissions_ref": node.permissions_ref,
                "resource_id": node.resource_request.get("resource_id"),
                "writer_lease": node.wants_writer_lease,
                "candidate_id": node.candidate_id or node.params.get("candidate_id"),
                "review_lens": node.params.get("review_lens"),
                "phase": state.get("phase", "pending"),
                "attempt_index": state.get("attempt_index", 0),
                "updated_at": state.get("updated_at"),
                "layer": layers.get(node_id, 0),
                "attempts": attempts_by_node.get(node_id, []),
            })

        edges = [
            {
                "source": edge.source_node_id, "target": edge.target_node_id,
                "guard": edge.guard_predicate,
            }
            for edge in revision.edges
        ]

        events = [
            {
                "sequence": event.sequence, "type": event.type,
                "recorded_at": event.recorded_at, "aggregate_id": event.aggregate_id,
                "attempt_id": event.attempt_id, "payload": event.payload,
            }
            for event in sorted(
                store.events(aggregate_id=rid) + store.events(aggregate_id=task_id),
                key=lambda e: e.sequence,
            )
        ]

        acceptances = [_row_to_dict(row) for row in store.acceptances_for(task_id, rid)]
        task_row = store.task(task_id)
        state = _load_json((Path(repo) if repo is not None else Paths.from_env().repo)
                           / ".orchestrator" / "state.json") or {}
        live_run = next((row for row in graph_runs(state) if row.get("task_id") == task_id), None)
        waiting = (state.get("admission_waiting") or {}).get(task_id)
        if not isinstance(waiting, dict) or not live_run or live_run.get("run_id") != rid:
            waiting = None
        now_z = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        pauses = [dict(row) for row in store.active_pool_pauses(now_z)] if waiting else []

        return {
            "task_id": task_id,
            # The project this view was read from, so a second read (`explain_view`) lands on
            # the same control plane without the caller having to carry the root alongside
            # the document. `Paths.from_env()` for the default, i.e. exactly what
            # `open_active_store(None)` just used.
            "repo": str(Path(repo).resolve() if repo is not None else Paths.from_env().repo),
            "task_status": task_row["status"] if task_row is not None else "unknown",
            "run": run,
            "workflow": {
                "workflow_id": revision.workflow_id,
                "revision": revision.revision,
                "parent_revision": revision.parent_revision,
                "compiler_version": revision.compiler_version,
                "policy_hash": revision.policy_hash,
                "code_base_sha": revision.code_base_sha,
                "execution_objective": revision.execution_objective,
                "mutation_reason": revision.mutation_reason,
                "terminal_requirements": list(revision.terminal_requirements),
                "candidate_groups": {k: list(v) for k, v in revision.candidate_groups().items()},
            },
            "nodes": nodes,
            "edges": edges,
            "events": events,
            "acceptances": acceptances,
            "admission_waiting": waiting,
            "pool_pauses": pauses,
        }
    finally:
        store.close()


# ── the causal trace: criteria -> binding -> evidence -> acceptance ───────────────────


def _criterion_nodes(revision, criterion: str) -> list[str]:
    """Which composed nodes satisfy `criterion`, decided by the compiler's own predicate.

    The compiler's `CRITERION_CHECKS` answer "does this *set* of nodes satisfy it", so the
    per-node attribution here evaluates each check against a one-node set. Two criteria are
    irreducibly plural — `isolated_candidates` and `diverse_review` are properties of a
    widened stage, not of any one node — and for those the attribution is the same helper the
    compiler itself counts with, so this cannot drift from what the graph was compiled against.
    """
    from maestro.workflows import compiler

    nodes = list(revision.nodes)
    if criterion == "isolated_candidates":
        return [n.node_id for n in nodes if n.params.get("candidate_id") and n.wants_writer_lease]
    if criterion == "diverse_review":
        return [n.node_id for n in nodes if n.params.get("review_lens")]
    check = compiler.CRITERION_CHECKS.get(criterion)
    if check is None:
        return []
    return [n.node_id for n in nodes if check([n])]


def _attempt_routing(attempt: Mapping) -> dict | None:
    """The durable routing decision recorded on one attempt (P12A W3), or `None`.

    `identity` is `metrics.binding_identity`'s form (`node_id|binding_id`), so the string
    an operator reads here is the same one duplicate-decision detection compares on.
    `summary` follows `router.status_summary` — assembled from the stored columns rather
    than by rebuilding an `AgentBinding`, because the row is the record and the catalog it
    was resolved against may no longer exist.
    """
    from maestro import metrics

    if not attempt.get("binding_id") and not attempt.get("route_refusal"):
        return None
    pool = ""
    if attempt.get("binding_json"):
        try:
            pool = str(json.loads(attempt["binding_json"]).get("usage_pool_id") or "")
        except (TypeError, ValueError):
            pool = ""
    backend, model = attempt.get("backend_id") or "", attempt.get("model_id") or ""
    effort = attempt.get("effort") or ""
    summary = ""
    if backend or model:
        summary = f"{backend}/{model} · effort {effort or 'unknown'}"
        if pool:
            summary += f" · pool {pool}"
    return {
        "identity": metrics.binding_identity(SimpleNamespace(
            binding_id=attempt.get("binding_id") or "",
            node_id=attempt.get("node_id") or "",
        )),
        "binding_id": attempt.get("binding_id"),
        "backend_id": attempt.get("backend_id"),
        "model_id": attempt.get("model_id"),
        "effort": attempt.get("effort"),
        "agent_version": attempt.get("agent_version"),
        "session_id": attempt.get("session_id"),
        "session_mode": attempt.get("session_mode"),
        "manifest_artifact_id": attempt.get("manifest_artifact_id"),
        "route_refusal": attempt.get("route_refusal"),
        "wall_time_sec": attempt.get("wall_time_sec"),
        "reset_crossing": attempt.get("reset_crossing"),
        "summary": summary,
    }


def _attempt_failure(attempt: Mapping) -> dict | None:
    if not attempt.get("failure_kind"):
        return None
    failure = json.loads(attempt["failure_json"]) if attempt.get("failure_json") else {}
    return {
        "kind": attempt.get("failure_kind"),
        "class": attempt.get("failure_class"),
        "evidence": redact_secrets(failure.get("evidence", "")),
        "retry_after_sec": failure.get("retry_after_sec"),
        "reset_at": failure.get("reset_at"),
    }


def explain_view(view: dict, *, repo: Path | None = None) -> dict:
    """The causal trace behind a task's acceptance, or behind its absence.

    Reads one chain per engineering criterion — *criterion -> the nodes bound to satisfy it ->
    the attempts those nodes ran -> the evidence those attempts produced -> whether the
    recorded acceptance decision cited that evidence.* The last link is the one that makes
    this an explanation rather than a status page: a criterion whose evidence exists but is
    not among `required_evidence_ids` was proof that nothing was decided on.

    **Why every criterion and not just this project's required ones.** The required list is
    the policy's, and the policy is a live file; reading it now would answer with today's
    requirements about a graph compiled under `policy_hash`. What *is* durable is the graph,
    so each of `compiler.CRITERION_CHECKS` is evaluated against the stored revision — and a
    graph could not have compiled at all unless it satisfied the criteria required of it, so
    the criteria shown as bound here are a superset of the ones that were demanded. A
    criterion with no bound node is reported as unbound rather than omitted: "nothing in this
    graph reviews the work" is the single most useful thing this view can tell an operator.

    "Binding" here is the *durable* binding — the node's role, permission boundary and
    exclusive resource, plus the attempts actually recorded against it. Since P12A W3 the
    routing decision (`[P6]`'s `AgentBinding`) is durable too: the backend, model, effort
    and shape key chosen for an attempt are written onto that attempt at claim, so each
    attempt here carries the binding identity the run actually used. It is *read back*,
    never re-resolved — asking the router again now would report today's catalog as if it
    were the one the run used, which is the error this view exists to avoid.
    """
    from maestro.workflows.compiler import load_revision

    store = _open_store(repo if repo is not None else view.get("repo"))
    try:
        revision = load_revision(
            store, view["workflow"]["workflow_id"], int(view["workflow"]["revision"])
        )
    finally:
        store.close()

    by_id = {node["node_id"]: node for node in view["nodes"]}
    cited: set[str] = set()
    for decision in view["acceptances"]:
        try:
            cited.update(json.loads(decision.get("required_evidence_ids") or "[]"))
        except (TypeError, ValueError):
            pass

    from maestro.workflows import compiler

    chains = []
    for criterion in sorted(compiler.CRITERION_CHECKS):
        node_ids = _criterion_nodes(revision, criterion) if revision is not None else []
        links = []
        for node_id in node_ids:
            node = by_id.get(node_id)
            if node is None:
                continue
            evidence = [e for attempt in node["attempts"] for e in attempt["evidence"]]
            links.append({
                "node_id": node_id,
                "binding": {
                    "role": node["role"], "kind": node["kind"],
                    "handler_ref": node["handler_ref"],
                    "permissions_ref": node["permissions_ref"],
                    "resource_id": node["resource_id"],
                    "candidate_id": node["candidate_id"],
                    "review_lens": node["review_lens"],
                },
                "phase": node["phase"],
                "attempts": [
                    {"attempt_id": a["attempt_id"], "status": a["status"],
                     "_order": a["_order"],
                     "started_at": a["started_at"], "outcome_ref": a["outcome_ref"],
                     "routing": _attempt_routing(a), "failure": _attempt_failure(a)}
                    for a in node["attempts"]
                ],
                "evidence": evidence,
                "evidence_cited_by_acceptance": [
                    e["evidence_id"] for e in evidence if e["evidence_id"] in cited
                ],
            })
        satisfied = bool(links) and all(
            link["phase"] == "succeeded" for link in links
        )
        chains.append({
            "criterion": criterion,
            "links": links,
            "nodes_reached_success": satisfied,
            "unbound": not links,
        })

    return {
        "task_id": view["task_id"],
        "run_id": view["run"]["run_id"],
        "task_status": view["task_status"],
        "run_status": view["run"]["status"],
        "objective": view["workflow"]["execution_objective"],
        "policy_hash": view["workflow"]["policy_hash"],
        "chains": chains,
        "acceptances": [
            {
                "decision_id": d["decision_id"], "decision": d["decision"],
                "reason": redact_secrets(d.get("reason") or ""),
                "decided_at": d["decided_at"],
                "integrated_source_snapshot": d["integrated_source_snapshot"],
                "required_evidence_ids": d["required_evidence_ids"],
                "review_dispositions": d["review_dispositions"],
                "exemptions": d["exemptions"],
            }
            for d in view["acceptances"]
        ],
        "verdict": _verdict(view),
        "decision_line": _decision_line(view),
        "summary": _compact_run_lines(view),
    }


def _verdict(view: dict) -> str:
    """One line naming the task's standing. Read, never recomputed (see the banner)."""
    decisions = view["acceptances"]
    if decisions:
        last = decisions[-1]
        return f"{last['decision']} at {last['decided_at']}: {redact_secrets(last['reason'] or '')}"
    failed = [n["node_id"] for n in view["nodes"] if n["phase"] == "failed"]
    if failed:
        return f"no acceptance decision recorded; failed nodes: {', '.join(failed)}"
    return "no acceptance decision recorded yet — the run has not reached the acceptance node"


# ── text renderings ──────────────────────────────────────────────────────────────────

_PHASE_MARK = {
    "succeeded": "✔", "failed": "✘", "running": "▶", "claimed": "◑", "ready": "○",
    "pending": "·", "skipped": "–", "uncertain": "?",
}


def _local_time(value) -> str | None:
    try:
        stamp = datetime.fromtimestamp(float(value), timezone.utc) if isinstance(value, (int, float)) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.astimezone(ZoneInfo("Asia/Jerusalem")).strftime("%Y-%m-%d %H:%M %Z")
    except (TypeError, ValueError, OverflowError):
        return None


def _usage_line(attempt: dict | None) -> str:
    if not attempt:
        return "Usage: unavailable"
    samples = (("settle", "usage_settle_json"), ("start", "usage_start_json"))
    if attempt.get("failure_kind") in {"quota_exhausted", "rate_limited"}:
        samples = tuple(reversed(samples))
    for sample_name, field in samples:
        try:
            sample = json.loads(attempt.get(field) or "null")
        except (TypeError, ValueError):
            continue
        if not isinstance(sample, dict):
            continue
        windows = sample.get("windows") or {}
        parts = []
        for key, label in (("300", "5h"), ("10080", "7d")):
            used = (windows.get(key) or {}).get("used_pct")
            if isinstance(used, (int, float)):
                parts.append(f"{label} {used:g}%")
        if parts:
            at = _local_time(sample.get("updated_at") or sample.get("read_at"))
            return f"Usage ({sample_name} sample{f' {at}' if at else ''}): " + ", ".join(parts)
    return "Usage: unavailable"


def _current_node(view: dict) -> dict | None:
    nodes = view["nodes"]
    for phase in ("running", "claimed", "ready", "uncertain", "failed", "pending"):
        match = next((node for node in nodes if node["phase"] == phase), None)
        if match:
            return match
    return None


def _compact_run_lines(view: dict) -> list[str]:
    run, wf = view["run"], view["workflow"]
    started = run.get("started_at")
    elapsed = ""
    try:
        ended = (datetime.fromisoformat(run["finished_at"].replace("Z", "+00:00"))
                 if run["status"] != "running" and run.get("finished_at")
                 else datetime.now(timezone.utc))
        elapsed_sec = max(0, int((ended - datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds()))
        elapsed = f" · {elapsed_sec // 3600}h {elapsed_sec // 60 % 60}m"
    except (AttributeError, TypeError, ValueError):
        pass
    lines = [
        f"{view['task_id']} · {view['task_status']} / run {run['status']}",
        f"Run {run['run_id']} · {wf['workflow_id']}@{wf['revision']}{elapsed}",
    ]
    current = _current_node(view) if run["status"] == "running" else None
    waiting = view.get("admission_waiting")
    if current:
        cycle = f" · cycle {current['attempt_index']}" if current.get("attempt_index") else ""
        if waiting:
            reason = str(waiting.get("reason") or "admission").replace("_", " ")
            lines.append(f"Waiting: {current['node_id']} — {reason}{cycle}")
        else:
            lines.append(f"Now: {current['node_id']} — {current['phase']}{cycle}")
    routed = [attempt for node in view["nodes"] for attempt in node["attempts"]
              if attempt.get("backend_id") or attempt.get("model_id")]
    last = max(routed, key=lambda attempt: attempt.get("_order", 0)) if routed else None
    if last:
        route = "/".join(x for x in (last.get("backend_id"), last.get("model_id")) if x)
        effort = last.get("effort")
        lines.append(f"Last: {route or 'unrouted'}" + (f" · {effort}" if effort else "") + f" · {last['status']}")
    lines.append(_usage_line(last))
    for pause in sorted(view.get("pool_pauses") or [], key=lambda p: p["pool_id"]):
        reset = _local_time(pause.get("paused_until"))
        if reset:
            lines.append(f"{pause['pool_id'].removesuffix('-subscription')}: available {reset}")
    return lines


def render_workflow_text(view: dict) -> str:
    lines = _compact_run_lines(view)
    lines.append("Steps:")
    for node in view["nodes"][:12]:
        cycle = f" #{node['attempt_index']}" if node.get("attempt_index") else ""
        lines.append(f"  {_PHASE_MARK.get(node['phase'], '·')} {node['node_id']}{cycle} · {node['phase']}")
    if len(view["nodes"]) > 12:
        lines.append(f"  … {len(view['nodes']) - 12} more steps")
    lines.append(_decision_line(view))
    return redact_secrets("\n".join(lines) + "\n")


def render_graph_progress_text(view: dict) -> str:
    return redact_secrets("\n".join(_compact_run_lines(view)) + "\n")


def _decision_line(view: dict) -> str:
    if view["acceptances"]:
        decision = view["acceptances"][-1]
        reason = redact_secrets(str(decision.get("reason") or "")).strip().replace("\n", " ")
        return redact_secrets(f"Decision: {decision['decision']} at {decision['decided_at']}" + (f" — {reason[:120]}" if reason else ""))
    return "Decision: pending" if view["run"]["status"] == "running" else f"Decision: {_verdict(view)}"


def render_explain_text(explain: dict) -> str:
    lines = list(explain["summary"])
    links = {link["node_id"]: link for chain in explain["chains"] for link in chain["links"]}
    checks = {ev["check_id"]: ev["outcome"] for link in links.values() for ev in link["evidence"]}
    if checks:
        lines.append("Checks: " + ", ".join(f"{key} {outcome}" for key, outcome in checks.items()))
    if explain["acceptances"]:
        evidence_ids = {ev["evidence_id"] for link in links.values() for ev in link["evidence"]}
        cited_ids = {ev_id for link in links.values() for ev_id in link["evidence_cited_by_acceptance"]}
        if evidence_ids:
            lines.append(f"Evidence cited: {len(cited_ids)}/{len(evidence_ids)}")
    failures = [attempt for link in links.values() for attempt in link["attempts"]
                if attempt.get("failure")]
    latest_failure = max(failures, key=lambda attempt: attempt.get("_order", -1))["failure"] if failures else None
    if latest_failure and not explain["acceptances"]:
        lines.append(f"Latest issue: {latest_failure['kind']} ({latest_failure['class']})")
    lines.append(explain["decision_line"])
    return redact_secrets("\n".join(lines) + "\n")


# ── the self-contained HTML export ───────────────────────────────────────────────────

#: Everything the page is allowed to carry about a node, an attempt or a piece of evidence.
#: An allow-list rather than a deny-list on purpose: the export must stay secret-free as the
#: control schema grows, and a column added next year would otherwise ship to the page by
#: default and be noticed (if ever) by whoever reads the HTML.
_HTML_NODE_FIELDS = (
    "node_id", "kind", "role", "handler_ref", "effect_class", "permissions_ref",
    "resource_id", "writer_lease", "candidate_id", "review_lens", "phase",
    "attempt_index", "updated_at", "layer",
)
_HTML_ATTEMPT_FIELDS = (
    "attempt_id", "status", "started_at", "heartbeat_at", "deadline_at", "lease_epoch",
    "outcome_ref",
)
_HTML_EVIDENCE_FIELDS = (
    "evidence_id", "check_id", "outcome", "validity", "exit_status", "recorded_at",
    "source_snapshot_id", "input_fingerprint", "environment_fingerprint", "command_hash",
)


def _html_payload(view: dict) -> dict:
    """`view`, narrowed to the allow-listed fields and run through `redact_secrets`."""
    def clean(value):
        return redact_secrets(value) if isinstance(value, str) else value

    nodes = []
    for node in view["nodes"]:
        row = {key: clean(node.get(key)) for key in _HTML_NODE_FIELDS}
        row["attempts"] = [
            {
                **{key: clean(attempt.get(key)) for key in _HTML_ATTEMPT_FIELDS},
                "evidence": [
                    {key: clean(ev.get(key)) for key in _HTML_EVIDENCE_FIELDS}
                    for ev in attempt["evidence"]
                ],
            }
            for attempt in node["attempts"]
        ]
        nodes.append(row)
    return {
        "task_id": clean(view["task_id"]),
        "task_status": clean(view["task_status"]),
        "run": {key: clean(view["run"].get(key)) for key in
                ("run_id", "status", "started_at", "finished_at", "workflow_id",
                 "workflow_revision")},
        "workflow": {key: clean(view["workflow"].get(key)) for key in
                     ("workflow_id", "revision", "parent_revision", "compiler_version",
                      "policy_hash", "code_base_sha", "execution_objective",
                      "mutation_reason")},
        "terminal_requirements": [clean(c) for c in view["workflow"]["terminal_requirements"]],
        "edges": view["edges"],
        "events": [
            # Payloads are deliberately dropped: an event body carries handler output, and
            # this page is an export that gets forwarded to phones and pasted into chats.
            {"sequence": e["sequence"], "type": clean(e["type"]),
             "recorded_at": clean(e["recorded_at"]), "attempt_id": clean(e["attempt_id"])}
            for e in view["events"]
        ],
        "nodes": nodes,
        "verdict": redact_secrets(_verdict(view)),
    }


def render_workflow_html(view: dict) -> str:
    """One self-contained HTML file: the graph, the timeline, and a detail panel.

    No `<script src>`, no `<link href>`, no font or image fetch — the page is opened from a
    laptop over SSH, from a phone, and out of a Telegram forward, and any of those may have
    no network at all by the time it is read. The layout (which column each node draws in)
    is computed in Python; the JavaScript only paints what it is handed and switches panels,
    so a browser with scripting disabled still shows the graph and the timeline.
    """
    payload = _html_payload(view)
    data = json.dumps(payload, indent=1, sort_keys=False)
    # `</script>` inside a JSON string would end the block early; the escape is the standard
    # one and survives `JSON.parse` unchanged.
    # `<` can only appear inside a JSON string here, and `\u003c` is the same string to
    # `JSON.parse`. Escaping every one of them (rather than just `</script>`) means no value
    # carried into the page — a node id, a reason, a check id — can open a tag.
    data = data.replace("<", "\\u003c")

    columns: dict[int, list[dict]] = {}
    for node in payload["nodes"]:
        columns.setdefault(int(node["layer"]), []).append(node)
    col_w, row_h, box_w, box_h = 240, 86, 190, 52
    pos = {}
    for layer, members in columns.items():
        for index, node in enumerate(members):
            pos[node["node_id"]] = (40 + layer * col_w, 40 + index * row_h)
    width = 80 + (max(columns) + 1) * col_w if columns else 400
    height = 80 + max((len(m) for m in columns.values()), default=1) * row_h

    def esc(text) -> str:
        return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;"))

    svg = []
    for edge in payload["edges"]:
        if edge["source"] not in pos or edge["target"] not in pos:
            continue
        x1, y1 = pos[edge["source"]]
        x2, y2 = pos[edge["target"]]
        x1 += box_w
        y1 += box_h // 2
        y2 += box_h // 2
        mid = (x1 + x2) / 2
        svg.append(
            f'<path class="edge" d="M{x1},{y1} C{mid},{y1} {mid},{y2} {x2},{y2}" '
            f'marker-end="url(#arrow)"><title>{esc(edge["guard"] or "always")}</title></path>'
        )
    for node in payload["nodes"]:
        x, y = pos[node["node_id"]]
        label = node["node_id"]
        sub = node["role"] or node["kind"]
        svg.append(
            f'<g class="node phase-{esc(node["phase"])}" data-node="{esc(node["node_id"])}" '
            f'tabindex="0">'
            f'<rect x="{x}" y="{y}" width="{box_w}" height="{box_h}" rx="8"/>'
            f'<text x="{x + 12}" y="{y + 22}" class="nlabel">{esc(label[:26])}</text>'
            f'<text x="{x + 12}" y="{y + 40}" class="nsub">{esc(sub)} · {esc(node["phase"])}</text>'
            f'</g>'
        )

    stamps = [e["recorded_at"] for e in payload["events"] if e["recorded_at"]]
    timeline = []
    if stamps:
        first, last = min(stamps), max(stamps)
        span = max(_epoch(last) - _epoch(first), 1.0)
        for node in payload["nodes"]:
            for attempt in node["attempts"]:
                start = attempt["started_at"] or first
                end = attempt["heartbeat_at"] or attempt["deadline_at"] or start
                left = 100.0 * (_epoch(start) - _epoch(first)) / span
                wide = max(100.0 * (_epoch(end) - _epoch(start)) / span, 1.2)
                timeline.append(
                    f'<div class="trow" data-node="{esc(node["node_id"])}">'
                    f'<span class="tname">{esc(node["node_id"][:26])}</span>'
                    f'<span class="ttrack"><span class="tbar phase-{esc(attempt["status"])}" '
                    f'style="left:{min(left, 99):.2f}%;width:{min(wide, 100 - min(left, 99)):.2f}%" '
                    f'title="{esc(attempt["attempt_id"])} {esc(attempt["status"])}"></span></span>'
                    f'</div>'
                )
    timeline_html = "".join(timeline) or '<p class="empty">No attempt has been recorded yet.</p>'

    wf = payload["workflow"]
    head = (
        f'{esc(payload["task_id"])} · {esc(wf["workflow_id"])}@{esc(wf["revision"])} · '
        f'{esc(wf["execution_objective"])} · run {esc(payload["run"]["run_id"])} '
        f'[{esc(payload["run"]["status"])}]'
    )
    criteria = "".join(
        f'<li>{esc(c)}</li>' for c in payload["terminal_requirements"]
    ) or "<li>(none declared)</li>"

    return _HTML_TEMPLATE.format(
        title=esc(f"{payload['task_id']} — maestro workflow"),
        head=head,
        verdict=esc(payload["verdict"]),
        criteria=criteria,
        width=width, height=height,
        svg="".join(svg),
        timeline=timeline_html,
        generated=esc(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")),
        data=data,
    )


def _epoch(stamp) -> float:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


#: `str.format` with `{{`-escaped CSS/JS braces. A template file would be one more thing to
#: ship and find at runtime; the export has to work from a `git worktree` of any age.
_HTML_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
 :root {{ color-scheme: light dark; --bg:#0f1115; --fg:#e6e8ec; --muted:#8b93a7;
          --line:#2a2f3a; --card:#171a21; }}
 @media (prefers-color-scheme: light) {{
   :root {{ --bg:#f7f8fa; --fg:#14171f; --muted:#5c6478; --line:#d7dae2; --card:#fff; }} }}
 body {{ margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; }}
 header {{ padding:16px 20px; border-bottom:1px solid var(--line); }}
 h1 {{ font-size:15px; margin:0 0 6px; font-weight:600; }}
 .verdict {{ color:var(--muted); }}
 main {{ display:flex; flex-wrap:wrap; gap:20px; padding:20px; align-items:flex-start; }}
 section {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
            padding:14px 16px; flex:1 1 420px; min-width:320px; overflow:auto; }}
 h2 {{ font-size:12px; text-transform:uppercase; letter-spacing:.08em; color:var(--muted);
       margin:0 0 10px; }}
 svg {{ max-width:100%; height:auto; }}
 .edge {{ fill:none; stroke:var(--line); stroke-width:1.5; }}
 .node rect {{ fill:var(--card); stroke:var(--line); stroke-width:1.5; cursor:pointer; }}
 .node:focus rect, .node.sel rect {{ stroke:#6ea8fe; stroke-width:2.5; }}
 .nlabel {{ font-size:12px; fill:var(--fg); }}
 .nsub {{ font-size:10.5px; fill:var(--muted); }}
 .phase-succeeded rect, .tbar.phase-succeeded {{ stroke:#3fb950; background:#3fb950; }}
 .phase-failed rect, .tbar.phase-failed {{ stroke:#f85149; background:#f85149; }}
 .phase-running rect, .tbar.phase-running {{ stroke:#d29922; background:#d29922; }}
 .phase-uncertain rect, .tbar.phase-uncertain {{ stroke:#a371f7; background:#a371f7; }}
 .phase-skipped rect {{ stroke-dasharray:4 3; }}
 .trow {{ display:flex; align-items:center; gap:8px; margin:3px 0; cursor:pointer; }}
 .tname {{ width:180px; color:var(--muted); overflow:hidden; text-overflow:ellipsis;
           white-space:nowrap; }}
 .ttrack {{ position:relative; flex:1; height:12px; background:rgba(128,128,128,.14);
            border-radius:6px; }}
 .tbar {{ position:absolute; top:0; height:12px; border-radius:6px; background:#6ea8fe; }}
 ul {{ margin:0; padding-left:18px; }}
 dl {{ margin:0; display:grid; grid-template-columns:max-content 1fr; gap:2px 12px; }}
 dt {{ color:var(--muted); }} dd {{ margin:0; overflow-wrap:anywhere; }}
 .empty {{ color:var(--muted); }}
 footer {{ padding:12px 20px; color:var(--muted); border-top:1px solid var(--line); }}
</style></head><body>
<header>
  <h1>{head}</h1>
  <div class="verdict">{verdict}</div>
</header>
<main>
  <section><h2>Graph</h2>
    <svg viewBox="0 0 {width} {height}" width="{width}" height="{height}"
         xmlns="http://www.w3.org/2000/svg">
      <defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6"
        markerHeight="6" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" fill="currentColor" opacity=".45"/></marker></defs>
      {svg}
    </svg>
  </section>
  <section><h2>Timeline</h2>{timeline}</section>
  <section><h2>Detail</h2><div id="detail" class="empty">Select a node.</div>
    <h2 style="margin-top:14px">Terminal nodes</h2><ul>{criteria}</ul>
  </section>
</main>
<footer>Generated by <code>maestro workflow show --html</code> at {generated}.
  Read-only projection of the control plane; no artifact content, command lines or
  environments are included.</footer>
<script type="application/json" id="payload">{data}</script>
<script>
 var DATA = JSON.parse(document.getElementById('payload').textContent);
 var byId = {{}};
 DATA.nodes.forEach(function (n) {{ byId[n.node_id] = n; }});
 function esc(s) {{ return String(s === null || s === undefined ? '—' : s)
   .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }}
 function row(k, v) {{ return '<dt>' + esc(k) + '</dt><dd>' + esc(v) + '</dd>'; }}
 function show(id) {{
   var n = byId[id]; if (!n) {{ return; }}
   var html = '<dl>' + row('node', n.node_id) + row('phase', n.phase) + row('kind', n.kind)
     + row('role', n.role) + row('handler', n.handler_ref) + row('effect', n.effect_class)
     + row('permissions', n.permissions_ref) + row('resource', n.resource_id)
     + row('candidate', n.candidate_id) + row('lens', n.review_lens) + '</dl>';
   n.attempts.forEach(function (a) {{
     html += '<h2 style="margin-top:12px">attempt ' + esc(a.attempt_id) + '</h2><dl>'
       + row('status', a.status) + row('started', a.started_at)
       + row('heartbeat', a.heartbeat_at) + row('epoch', a.lease_epoch) + '</dl>';
     a.evidence.forEach(function (e) {{
       html += '<dl>' + row('check', e.check_id) + row('outcome', e.outcome)
         + row('validity', e.validity) + row('exit', e.exit_status)
         + row('snapshot', e.source_snapshot_id) + '</dl>';
     }});
   }});
   document.getElementById('detail').innerHTML = html;
   document.querySelectorAll('.node').forEach(function (g) {{
     g.classList.toggle('sel', g.dataset.node === id); }});
 }}
 document.querySelectorAll('[data-node]').forEach(function (el) {{
   el.addEventListener('click', function () {{ show(el.dataset.node); }});
   el.addEventListener('keydown', function (ev) {{
     if (ev.key === 'Enter' || ev.key === ' ') {{ ev.preventDefault(); show(el.dataset.node); }} }});
 }});
</script>
</body></html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
