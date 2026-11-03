"""The machine-readable ROADMAP: parsing it, hashing it, and graduating tasks out of it.

Extracted verbatim from the reference orchestrator. Behavioural surprises are
catalogued in `docs/FOUND_BUGS.md` and pinned by `tests/characterization/test_roadmap.py`;
none of them is fixed here.

P02 moved the *parsing* out and left the behaviour alone: the four functions that used to
scan ```` ```yaml ```` blocks with a private regex now call `maestro.taskgraph`, which is
the one reading of `docs/ROADMAP.md` for every module that has one. The quirks went with
them — a malformed block is still skipped, a falsy `id` is still no id — and every
assertion in the characterisation suite still holds byte-for-byte. What is genuinely new
here is `_drift_blocked_ids`: a task whose ROADMAP block was hand-edited under a live
controller, and everything downstream of it, is withheld from dispatch. That is inert for
any project that has never run `maestro roadmap apply`.

R5–R7 (`docs/plans/2026-08-18-m4c-superseded-sidecars.md` §2b): `run_dep_map` and
`mark_roadmap_complete` used to shell out to `scripts/gen_dependency_map.py`,
`scripts/render_dependency_map.sh`, `scripts/gen_upcoming.py` and
`scripts/mark_task_complete.py` by path — all four now-deleted-at-M5 scripts. They are
rewired below to call the M4c batch-2 in-process equivalents (`maestro.docs.depmap`,
`.upcoming`, `.complete`) directly instead: `_call_main` substitutes `sys.argv` for the
duration of the call (the same technique `maestro.verifications.run_cli` uses) so each
module's own `sys.argv`-driven `main()` is invoked unmodified, in this interpreter, with
no subprocess spawned and no `REPO / "scripts"` path to go stale.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys

from maestro import prep_actions, taskgraph
from maestro.docs import complete as doc_complete
from maestro.docs import depmap as doc_depmap
from maestro.docs import upcoming as doc_upcoming
from maestro.hitl.telegram import delivery_note, notify_telegram, notify_telegram_with_map
from maestro.paths import Paths
from maestro.state import append_journal, read_state, write_state

_PATHS = Paths.from_env()

REPO                  = _PATHS.repo
STATE_JSON            = _PATHS.state
JOURNAL               = _PATHS.journal
ROADMAP_FILE          = REPO / "docs" / "ROADMAP.md"
LAST_MAP_SIG          = REPO / ".orchestrator" / "last_map_roadmap.sha"
COMPLETED_TASKS       = REPO / ".orchestrator" / "completed_tasks.json"
DEP_MAP_PNG           = REPO / "docs" / "dependency_map.png"

# The B14 auto-prep sidecar, which records each dispatch:manual task's single prepared
# Dan-action. The reference implementation imports it as a module from `scripts/`; the
# maestro home is `maestro.prep_actions` (M4a), imported at the top. It is a leaf — its
# only maestro import is `maestro.paths` — so a plain module import creates no cycle.


# `notify_telegram` and `notify_telegram_with_map` live in `maestro.hitl.telegram`, which
# now exists; the stopgap copies that stood here during batch 2 are gone and the names are
# imported above. `maestro.hitl.telegram` deliberately does not import this module, so
# there is no cycle.


def _call_main(module, argv: list[str], swallow: bool = True) -> None:
    """In-process equivalent of `subprocess.run([VENV_PYTHON, <script>, *argv],
    capture_output=True)` against one of the M4c batch-2 doc-engine modules (each of
    which still has its own `sys.argv`-driven `main()`, left exactly as extracted).
    Substitutes `sys.argv` for the call and redirects stdout/stderr into a throwaway
    buffer — mirroring `capture_output=True` — then restores both.

    `swallow=True` (the default) mirrors every one of the old subprocess call sites
    here: none of them ever checked the child's return code, so a failing generator was
    a swallowed non-zero exit, not an error. `swallow=False` lets `SystemExit`/any
    exception the module raises escape, for the one caller (`_refresh_upcoming`) that
    needs to know a step failed in order to fall back to a cheaper one.
    """
    old_argv = sys.argv
    try:
        sys.argv = [module.__name__, *argv]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            module.main()
    except BaseException:
        if not swallow:
            raise
    finally:
        sys.argv = old_argv


#: The last render failure journaled by `run_dep_map`, so a persistent one is logged once.
_LAST_RENDER_FAILURE: str | None = None


def run_dep_map() -> None:
    """Regenerate the dependency map .md AND re-render the .png.

    `maestro.docs.depmap.main()` only writes the mermaid .md; the .png is rendered by
    `maestro.docs.depmap.render_png()` (R7's in-process port of the reference
    `render_dependency_map.sh`, which normally fired as a Claude Code PostToolUse hook
    on edits to the .md). The orchestrator regenerates the .md out-of-band (not a hooked
    tool call), so we must render the .png explicitly here — otherwise
    notify_telegram_with_map ships a stale PNG from the last interactive edit/commit.
    """
    global _LAST_RENDER_FAILURE
    _call_main(doc_depmap, [])
    # The render's own stderr is captured: the loop used to print "no mermaid block found"
    # on every refresh while nothing noticed that no map had ever been drawn. A failure is
    # journaled instead, once per distinct reason, and a stale PNG is then not sent as
    # current (`notify_telegram_with_map`).
    err = io.StringIO()
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            doc_depmap.render_png()
        _LAST_RENDER_FAILURE = None
    except BaseException as exc:
        reason = (err.getvalue().strip() or f"{type(exc).__name__}: {exc}")[:200]
        # No ROADMAP means no map is expected (a fresh or sandboxed tree): nothing to report.
        if reason != _LAST_RENDER_FAILURE and ROADMAP_FILE.exists():
            _LAST_RENDER_FAILURE = reason
            try:
                append_journal("dep_map_render_failed", reason)
            except Exception:
                pass
    # Keep the human-facing docs/UPCOMING.md in lock-step with the same sources
    # (ROADMAP + live state). Deterministic/offline — no LLM here; the Opus-authored
    # prose is refreshed separately at graduation (see mark_roadmap_complete).
    _call_main(doc_upcoming, [])


def _roadmap_signature() -> str:
    """Content hash of docs/ROADMAP.md — changes iff the task set/scope changes."""
    try:
        return hashlib.sha256(ROADMAP_FILE.read_bytes()).hexdigest()
    except Exception:
        return ""


def maybe_push_roadmap_map_change() -> None:
    """Push a refreshed dependency-map PNG whenever docs/ROADMAP.md changed since the
    last map we sent.

    The map was previously shipped to Telegram only at task-completion/graduation
    events (phase_report). ROADMAP edits made *between* those events — Dan manually
    adding/re-scoping tasks, or an implementer re-scoping — never hit that path, so Dan
    could be left holding a map missing newly-added tasks even though the on-disk render
    is current. This closes the gap: regenerate from the live ROADMAP and push, once per
    real change (deduped on ROADMAP content hash; silent baseline on first observation).
    """
    sig = _roadmap_signature()
    if not sig:
        return
    try:
        last = LAST_MAP_SIG.read_text().strip()
    except Exception:
        last = ""
    if sig == last:
        return
    # First observation (fresh deploy / no prior baseline): record, don't spam a push.
    if not last:
        try:
            LAST_MAP_SIG.write_text(sig)
        except Exception:
            pass
        return
    run_dep_map()  # regenerate .md + render .png from the current ROADMAP
    try:
        n_runnable = len(parse_runnable_tasks())
        n_prep     = len(parse_prep_tasks())
        summary = f"▶ {n_runnable} runnable · {n_prep} awaiting-you"
    except Exception:
        summary = ""
    notify_telegram_with_map(  # writes LAST_MAP_SIG = sig on send → dedups next poll
        "🗺 *Roadmap updated* — dependency map refreshed.\n" + summary
    )
    append_journal("roadmap_map_pushed", f"sig={sig[:12]}{delivery_note()}")


def get_completed_task_ids() -> set[str]:
    try:
        data = json.loads(COMPLETED_TASKS.read_text())
        return {str(item["id"]) for item in data}
    except Exception:
        return set()


# ── ROADMAP parsing ──

#: `unrunnable`'s reason for a task whose dependencies are not all complete.
WAITS = "it waits for its dependencies"


def pending_deps(task: dict, complete_ids) -> list[str]:
    """The task's dependencies that are not complete, in its order."""
    deps = task.get("deps") or []
    if isinstance(deps, str):
        deps = [deps] if deps else []
    return [str(d) for d in deps if str(d) not in complete_ids]


def unrunnable(task: dict, complete_ids, blocked=frozenset()) -> str:
    """Why the loop does not run `task`, `""` when it does: the one predicate
    `parse_runnable_tasks` applies, shared with the completion gate (P13 GDT-10), which
    names the reason and follows `WAITS` into the dependencies."""
    if str(task["id"]) in blocked:  # P02 drift / GDT-2 goal gate — see _drift_blocked_ids, _goal_blocked_ids
        return "it is held (a ROADMAP drift conflict, the goals gate or a goal replan)"
    if task.get("status") == "complete":
        return "it is complete"
    if task.get("mode") not in ("autonomous", "needs-dan"):
        if not task.get("mode"):
            return "it has no `mode:` (only autonomous and needs-dan tasks run)"
        return f"its mode {task.get('mode')} is never dispatched"
    if task.get("self_modifying"):  # Problem 10 — never auto-run self-modifying tasks
        return "it is self_modifying (never auto-run)"
    if task.get("hold"):  # human hold — excluded from auto-launch until the flag is cleared
        return "it is on hold"
    if task.get("dispatch") == "manual":  # B9: permanent non-dispatchable; Dan must perform the task
        return "it is dispatch: manual (yours to perform)"
    if pending_deps(task, complete_ids):
        return WAITS
    return ""


def parse_runnable_tasks() -> list[dict]:
    """Return autonomous, non-self-modifying tasks whose deps are all complete."""
    content = ROADMAP_FILE.read_text(encoding="utf-8")
    tasks: list[dict]      = taskgraph.scan_tasks(content)
    complete_ids: set[str] = get_completed_task_ids()
    for task in tasks:
        if task.get("status") == "complete":
            complete_ids.add(str(task["id"]))
    blocked = _drift_blocked_ids() | _goal_blocked_ids(tasks) | _replan_blocked_ids(tasks)
    return [task for task in tasks if not unrunnable(task, complete_ids, blocked)]


def parse_prep_tasks(state: dict | None = None) -> list[dict]:
    """B14 auto-prep: dispatch:manual tasks the orchestrator should AUTO-PREPARE.

    These are NOT handed the human step — a prep implementer does all the *automatable*
    prep (scripts, exports, notebook upload, non-downtime migrations), writes the single
    `dan_action`, and the task parks for Dan. Returns manual tasks with deps satisfied
    that are not already prepared (sidecar), parked, held, or self-modifying. Mirrors the
    dedup/dep logic of parse_runnable_tasks (which deliberately *excludes* dispatch:manual).
    """
    content = ROADMAP_FILE.read_text(encoding="utf-8")
    complete_ids: set[str] = get_completed_task_ids()
    tasks: list[dict] = taskgraph.scan_tasks(content)
    for task in tasks:
        if task.get("status") == "complete":
            complete_ids.add(str(task["id"]))
    blocked    = _drift_blocked_ids() | _goal_blocked_ids(tasks) | _replan_blocked_ids(tasks)
    state      = read_state() if state is None else state
    parked_ids = {p.get("task_id") for p in state.get("waiting_on_dan", {}).values()}
    failed_parked = set(state.get("parked_tasks", []))  # B11: failed → needs /unpark first
    prep: list[dict] = []
    for task in tasks:
        if task.get("status") == "complete":
            continue
        if task.get("dispatch") != "manual":  # only the manual-dispatch class is auto-prepped
            continue
        if task.get("hold") or task.get("self_modifying"):
            continue
        tid = str(task["id"])
        if tid in blocked:                # P02 drift conflict / GDT-2 goal gate hold
            continue
        if prep_actions.has_action(tid):  # prep already done, awaiting Dan's action
            continue
        if tid in parked_ids:             # already parked (e.g. seeded or mid-cycle)
            continue
        if tid in failed_parked:          # a prior prep/run failed → wait for /unpark
            continue
        deps = task.get("deps") or []
        if isinstance(deps, str):
            deps = [deps] if deps else []
        if all(str(d) in complete_ids for d in deps):
            prep.append(task)
    return prep


#: needs-dan ⇒ auto-prep. The rule itself now lives in `maestro.taskgraph` (P02), which
#: is the one place the roadmap is read; this name stays bound because it is part of this
#: module's surface and `tests/characterization/test_roadmap.py` exercises it directly.
_normalize_task = taskgraph.normalize_legacy_task


def _drift_blocked_ids() -> frozenset[str]:
    """Task ids that a ROADMAP drift conflict forbids dispatching (P02).

    If `docs/ROADMAP.md` was hand-edited while the controller is live, the edited tasks
    and everything downstream of them are held back — the controller must never act on a
    task whose definition moved under it, and must never resolve the conflict by
    overwriting the edit. Everything else keeps running.

    Empty, always, for a project that has not adopted the `[P2]` model: with no recorded
    revision in `.orchestrator/roadmap_cas.json` there is nothing to have drifted from,
    and the legacy behaviour is preserved byte-for-byte (`D08`).
    """
    return taskgraph.blocked_task_ids(REPO)


#: repo -> the goal-held ids last journaled, so a hold is journaled once per change.
_LAST_GOAL_HOLD: dict[str, frozenset[str]] = {}


def _goal_blocked_ids(tasks: list[dict]) -> frozenset[str]:
    """Task ids the goals contract's execution gate holds (P13 GDT-2, R31).

    A task is held on its own goal problems (no `serves`, an unknown id) and on the
    `docs/GOALS.md` gate problems whose ids it touches; everything else keeps running, like
    a drift hold. Empty without `docs/GOALS.md`. Never raises: a gate that cannot read the
    contract journals why and holds nothing, as the drift hold does.
    """
    from maestro.docs import goals as doc_goals

    try:
        doc = doc_goals.load(REPO)
        holds = doc_goals.execution_holds(doc, tasks) if doc is not None else {}
    except Exception as exc:  # noqa: BLE001 - dispatch must not die on the goals file
        append_journal("goal_gate_error", f"{type(exc).__name__}: {exc}")
        return frozenset()
    held = frozenset(holds)
    if held and _LAST_GOAL_HOLD.get(str(REPO)) != held:
        append_journal("goal_gate_hold", doc_goals.describe_holds(holds))
    _LAST_GOAL_HOLD[str(REPO)] = held
    return held


#: repo -> the replan-held ids last journaled, so a hold is journaled once per change.
_LAST_REPLAN_HOLD: dict[str, frozenset[str]] = {}


def _replan_blocked_ids(tasks: list[dict]) -> frozenset[str]:
    """Task ids a goal replan holds (P13 GDT-8, R46): while Dan's answer waits, the goal's
    own work and what depends on it; while an added task is open, the tasks waiting for
    it (`goal_replan.held`). Independent work keeps running. Empty without the goals store.
    Never raises: a store that cannot be read journals why and holds nothing."""
    from maestro import goal_replan

    try:
        holds = goal_replan.held(REPO, tasks)
    except Exception as exc:  # noqa: BLE001 - dispatch must not die on the goals store
        append_journal("goal_replan_hold_error", f"{type(exc).__name__}: {exc}")
        return frozenset()
    held = frozenset(holds)
    if held and _LAST_REPLAN_HOLD.get(str(REPO)) != held:
        by: dict[str, list[str]] = {}
        for tid, rp in holds.items():
            by.setdefault(rp, []).append(tid)
        append_journal("goal_replan_hold", "; ".join(
            f"{rp} holds {', '.join(ids)}" for rp, ids in by.items()))
    _LAST_REPLAN_HOLD[str(REPO)] = held
    return held


def get_task_by_id(task_id: str) -> dict | None:
    """Return the full task dict from ROADMAP.md, or None."""
    content = ROADMAP_FILE.read_text(encoding="utf-8")
    for task in taskgraph.scan_tasks(content, require_id=False):
        if str(task.get("id", "")) == str(task_id):
            return task
    return None


def _refresh_upcoming() -> None:
    """Best-effort UPCOMING.md refresh at graduation. The old subprocess call ran
    `gen_upcoming.py --refresh-explanations` under a 900 s outer timeout, falling back
    to the cheap (no-Opus) regen on `TimeoutExpired` so the doc was never left stale
    just because the model calls ran long. In-process there is no outer process to
    time out — each Opus call already bounds itself (`REFRESH_TIMEOUT`, best-effort,
    inside `maestro.docs.upcoming._opus_complete`) — so the fallback now triggers on
    any exception the refresh path raises instead of specifically a timeout."""
    try:
        _call_main(doc_upcoming, ["--refresh-explanations"], swallow=False)
    except BaseException:
        _call_main(doc_upcoming, [])


def _append_replan_tasks(tasks) -> list[str]:
    """Append a replan's tasks (P13 GDT-8) to the ROADMAP as yaml blocks; an id already
    there is skipped, so a retried graduation or answer adds nothing twice. Each block is
    recorded in the `[P2]` CAS, as a queued follow-up task is. Returns the ids added."""
    from maestro import goal_roadmap
    from maestro.docs import followups as doc_followups

    content = ROADMAP_FILE.read_text(encoding="utf-8")
    present = {str(t.get("id")) for t in taskgraph.scan_tasks(content, require_id=False)}
    new = [dict(t) for t in tasks if str(t.get("id")) not in present]
    new = list({str(t["id"]): t for t in new}.values())
    if not new:
        return []
    content = content.rstrip("\n") + "\n\n" + goal_roadmap.render_tasks(new).rstrip("\n") + "\n"
    ROADMAP_FILE.write_text(content, encoding="utf-8")
    ids = [str(t["id"]) for t in new]
    for tid in ids:
        doc_followups.register_in_cas(REPO, content, tid)
    return ids


def add_tasks(tasks, label: str, event: str, *, key: str | None = None) -> list[str]:
    """Tasks maestro adds between graduations (a replan Dan approved, P13 GDT-8; a polish
    stage, GDT-11): appended to the ROADMAP and committed (docs-only, the ROADMAP alone,
    `roadmap: <label> adds <ids>`). Returns the ids added (`[]` when all were already
    there). Journals `<event>_tasks_added`, `_tasks_uncommitted` or `_commit_failed`, each
    detail starting `<key>: ` (the label by default).

    A ROADMAP with uncommitted changes is Dan's edit in progress (as `reduce_roadmap_deps`
    reads it): the tasks join the working tree, which dispatch reads, and are not committed
    now, so his edit is never swept into a maestro commit; the next graduation commits the
    ROADMAP. A failed `git add`/`commit` is journaled, never raised."""
    key = label if key is None else key
    rel = str(ROADMAP_FILE.relative_to(REPO))
    dirty = subprocess.run(["git", "status", "--porcelain", "--", rel],
                           cwd=str(REPO), capture_output=True, text=True)
    ids = _append_replan_tasks(tasks)
    if not ids:
        return []
    if dirty.returncode != 0 or (dirty.stdout or "").strip():
        append_journal(f"{event}_tasks_uncommitted",
                       f"{key}: {', '.join(ids)} added to {rel}, not committed: it holds "
                       "uncommitted edits")
        return ids
    for cmd in (["git", "add", "--", rel],
                ["git", "commit", "--no-verify", "-m",
                 f"roadmap: {label} adds {', '.join(ids)}", "--", rel]):
        proc = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True)
        if proc.returncode != 0:
            append_journal(f"{event}_commit_failed",
                           f"{key}: {', '.join(ids)} added to {rel}; git {cmd[1]} exited "
                           f"{proc.returncode}: {(proc.stderr or proc.stdout or '').strip()}"
                           [:300])
            return ids
    append_journal(f"{event}_tasks_added", f"{key}: {', '.join(ids)}")
    return ids


def add_replan_tasks(tasks, rp_id: str) -> list[str]:
    """The tasks Dan's approval of replan `rp_id` adds (`add_tasks`)."""
    return add_tasks(tasks, f"replan {rp_id}", "goal_replan", key=rp_id)


def mark_roadmap_complete(task_id: str, follow_ups=(), replan_tasks=(), polish_tasks=()):
    """Remove task from ROADMAP.md, log to completed_tasks.json, and COMMIT the
    graduation artifacts so the caller's `git push origin main` actually persists
    them. `maestro.docs.complete.main()` only writes files (no commit), and every
    graduation site does `mark_roadmap_complete` → `git push` with nothing staged in
    between, so without this commit the strip + registry append live only in the
    working tree and never reach git (P8B2/P8B3/P14 accumulated this way).

    `follow_ups` (`maestro.docs.followups.FollowUp`s, from the graph path) are the
    accepted run's review follow-ups: recorded in the backlog for the polish stage, or,
    when marked `before_dependents` and a task depends on this one, queued as one
    `<id>-followups` task before its dependents — in this same commit (P13 GDT-11).
    Returns where they went (`followups.Recorded`), or None.

    `replan_tasks` (P13 GDT-8) are the tasks a routine replan applied at this graduation:
    appended to the ROADMAP in this same commit.

    `polish_tasks` (P13 GDT-11) are the polish tasks a triage task's graduation grouped
    its kept follow-ups into (`backlog.apply_triage`): appended in this same commit too."""
    from maestro.docs import followups as doc_followups

    recorded = None
    for tasks, event in ((replan_tasks, "goal_replan_tasks"), (polish_tasks, "polish_tasks")):
        if not tasks:
            continue
        try:
            added = _append_replan_tasks(tasks)
            if added:
                append_journal(f"{event}_added", f"{task_id}: {', '.join(added)}")
        except Exception as exc:  # noqa: BLE001 — added work never blocks a graduation
            append_journal(f"{event}_not_added", f"{task_id}: {exc}")
    if follow_ups:
        # Before the strip, while the task's block is still there to read and to place
        # the follow-up task after; the dep map and UPCOMING below then include it.
        try:
            recorded = doc_followups.record(
                REPO, ROADMAP_FILE, get_task_by_id(task_id), follow_ups,
                journal=append_journal)
        except Exception as exc:  # noqa: BLE001 — follow-ups never block a graduation
            append_journal("followups_not_recorded", f"{task_id}: {exc}")
    # --no-verify: the orchestrator already ran the verification gate on the worktree
    # before reaching here (run_verification_gate); re-gating REPO would be redundant and
    # mark_roadmap_complete ignores the rc anyway, so a spurious fail must not block graduation.
    _call_main(doc_complete, [task_id, "--no-verify"])
    # maestro.docs.complete.main() regenerates the dep-map .md but not the .png —
    # render it so the committed PNG matches the freshly-graduated graph (see run_dep_map).
    try:
        doc_depmap.render_png()
    except BaseException:
        pass
    # The task set just shrank, so refresh the human-facing UPCOMING doc. Graduation
    # is the rare, natural point to spend Opus on the per-task prose: --refresh-explanations
    # only calls Opus for tasks whose cache entry is stale/missing (steady-state a no-op),
    # then regenerates docs/UPCOMING.md. Best-effort + bounded — a slow/absent CLI must not
    # block a graduation, and the deterministic fallback keeps the doc valid regardless.
    _refresh_upcoming()
    # Stage only the Definition-of-Done artifacts mark_task_complete touches — never
    # `git add -A`, so unrelated working-tree changes are not swept into the commit.
    grad_files = [
        ".orchestrator/completed_tasks.json",
        "docs/ROADMAP.md", "docs/PROJECT.md", "docs/AUTONOMOUS_SYSTEM.md",
        "docs/dependency_map.md", "docs/dependency_map.png", "docs/UPCOMING.md",
        ".orchestrator/upcoming_explanations.json",
    ]
    # Only paths that exist: `git add` with one unmatched path stages nothing at all (P12
    # pilot finding D18: a project without docs/AUTONOMOUS_SYSTEM.md never committed).
    grad_files = [p for p in grad_files if (REPO / p).exists()]
    subprocess.run(["git", "add", *grad_files], cwd=str(REPO), capture_output=True)
    # Only commit if something was actually staged (avoid an empty-commit error), and
    # only those paths: whatever else the index holds is not graduation's to commit (P12
    # pilot finding D8: a checkout left behind by a graph merge held a staged revert of
    # it, and a whole-index commit here landed that revert on main).
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--", *grad_files],
        cwd=str(REPO), capture_output=True, text=True,
    ).stdout.split()
    if staged:
        subprocess.run(
            ["git", "commit", "--no-verify", "-m",
             f"roadmap({task_id}): graduate to project docs + registry", "--", *staged],
            cwd=str(REPO), capture_output=True,
        )
    append_journal("roadmap_task_removed", f"{task_id} removed from ROADMAP.md")
    if recorded is not None:
        append_journal("followups_recorded", f"{task_id}: {recorded.summary()}")
    return recorded


def reduce_roadmap_deps() -> dict[str, list[str]]:
    """Rewrite transitively redundant deps out of ROADMAP.md (P12 finding D21).

    Run once at graph-loop startup, before any task is opened. Deps of graduated tasks
    (the completion registry) count as reachability. A minimal edit of the affected
    `deps:` lines, journaled `roadmap_deps_reduced` and committed docs-only; a no-op —
    no write, journal or commit — when nothing is redundant. A ROADMAP with uncommitted
    changes is someone's edit in progress and is left alone until the next start.

    Under the `[P2]` CAS the rewritten blocks are recorded as applied, as
    `followups.register_in_cas` does for a queued block, so the rewrite is not read as a
    hand edit; a block already drifted from the CAS is not touched, so its hand edit is
    never laundered into the record. Returns `{task_id: [removed deps]}`.
    """
    try:
        content = ROADMAP_FILE.read_text(encoding="utf-8")
    except OSError:
        return {}
    record = taskgraph.read_cas(REPO)
    before = taskgraph.parse_document(content)
    skip: set[str] = set()
    if record is not None:
        skip = {t for t, h in before.block_hashes().items() if record.tasks.get(t) != h}
    new, removed = taskgraph.reduce_document_deps(
        content, known=taskgraph.read_registry_deps(REPO), skip=skip)
    if not removed:
        return {}
    rel = str(ROADMAP_FILE.relative_to(REPO))
    dirty = subprocess.run(["git", "status", "--porcelain", "--", rel],
                           cwd=str(REPO), capture_output=True, text=True)
    if dirty.returncode != 0 or dirty.stdout.strip():
        return {}
    ROADMAP_FILE.write_text(new, encoding="utf-8")
    if record is not None:
        after = taskgraph.parse_document(new)
        data = record.to_json()
        hashes = after.block_hashes()
        data["tasks"].update({t: hashes[t] for t in removed if t in hashes})
        if record.document_hash == before.document_hash:
            data["document_hash"] = after.document_hash
        taskgraph._atomic_write(taskgraph.cas_path(REPO), json.dumps(data, indent=2) + "\n")
    append_journal("roadmap_deps_reduced",
                   "; ".join(f"{t}: -{', '.join(d)}" for t, d in removed.items()))
    subprocess.run(["git", "add", "--", rel], cwd=str(REPO), capture_output=True)
    subprocess.run(
        ["git", "commit", "--no-verify", "-m",
         f"roadmap: drop transitively redundant deps ({', '.join(removed)})", "--", rel],
        cwd=str(REPO), capture_output=True,
    )
    return removed


def _load_roadmap_tasks() -> list[dict]:
    """Parse all ```yaml fenced blocks in ROADMAP.md and return dicts that have an `id`.

    Returns every task present in the ROADMAP (graduated tasks are absent because
    mark_task_complete.py strips them out).  Callers must not mutate the dicts.
    """
    try:
        content = ROADMAP_FILE.read_text(encoding="utf-8")
    except OSError:
        return []
    return taskgraph.scan_tasks(content)
