"""The machine-readable ROADMAP: parsing it, hashing it, and graduating tasks out of it.

Extracted verbatim from the reference orchestrator. Bodies are unchanged — only the
import block and the derivation of the module-level path globals differ. Behavioural
surprises are catalogued in `docs/FOUND_BUGS.md` and pinned by
`tests/characterization/test_roadmap.py`; none of them is fixed here.

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
import re
import subprocess
import sys

import yaml  # PyYAML

from maestro import prep_actions
from maestro.docs import complete as doc_complete
from maestro.docs import depmap as doc_depmap
from maestro.docs import upcoming as doc_upcoming
from maestro.hitl.telegram import notify_telegram, notify_telegram_with_map
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


def run_dep_map() -> None:
    """Regenerate the dependency map .md AND re-render the .png.

    `maestro.docs.depmap.main()` only writes the mermaid .md; the .png is rendered by
    `maestro.docs.depmap.render_png()` (R7's in-process port of the reference
    `render_dependency_map.sh`, which normally fired as a Claude Code PostToolUse hook
    on edits to the .md). The orchestrator regenerates the .md out-of-band (not a hooked
    tool call), so we must render the .png explicitly here — otherwise
    notify_telegram_with_map ships a stale PNG from the last interactive edit/commit.
    """
    _call_main(doc_depmap, [])
    try:
        doc_depmap.render_png()
    except BaseException:
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
    append_journal("roadmap_map_pushed", f"sig={sig[:12]}")


def get_completed_task_ids() -> set[str]:
    try:
        data = json.loads(COMPLETED_TASKS.read_text())
        return {str(item["id"]) for item in data}
    except Exception:
        return set()


# ── ROADMAP parsing ──

def parse_runnable_tasks() -> list[dict]:
    """Return autonomous, non-self-modifying tasks whose deps are all complete."""
    content = ROADMAP_FILE.read_text(encoding="utf-8")
    blocks  = re.findall(r"```yaml\n(.*?)```", content, re.DOTALL)
    tasks: list[dict]      = []
    complete_ids: set[str] = get_completed_task_ids()
    for raw in blocks:
        try:
            task = _normalize_task(yaml.safe_load(raw))
        except yaml.YAMLError:
            continue
        if not isinstance(task, dict) or not task.get("id"):
            continue
        tasks.append(task)
        if task.get("status") == "complete":
            complete_ids.add(str(task["id"]))
    runnable: list[dict] = []
    for task in tasks:
        if task.get("status") == "complete":
            continue
        if task.get("mode") not in ("autonomous", "needs-dan"):
            continue
        if task.get("self_modifying"):  # Problem 10 — never auto-run self-modifying tasks
            continue
        if task.get("hold"):  # human hold — excluded from auto-launch until the flag is cleared
            continue
        if task.get("dispatch") == "manual":  # B9: permanent non-dispatchable; Dan must perform the task
            continue
        deps = task.get("deps") or []
        if isinstance(deps, str):
            deps = [deps] if deps else []
        if all(str(d) in complete_ids for d in deps):
            runnable.append(task)
    return runnable


def parse_prep_tasks() -> list[dict]:
    """B14 auto-prep: dispatch:manual tasks the orchestrator should AUTO-PREPARE.

    These are NOT handed the human step — a prep implementer does all the *automatable*
    prep (scripts, exports, notebook upload, non-downtime migrations), writes the single
    `dan_action`, and the task parks for Dan. Returns manual tasks with deps satisfied
    that are not already prepared (sidecar), parked, held, or self-modifying. Mirrors the
    dedup/dep logic of parse_runnable_tasks (which deliberately *excludes* dispatch:manual).
    """
    content = ROADMAP_FILE.read_text(encoding="utf-8")
    blocks  = re.findall(r"```yaml\n(.*?)```", content, re.DOTALL)
    complete_ids: set[str] = get_completed_task_ids()
    tasks: list[dict] = []
    for raw in blocks:
        try:
            task = _normalize_task(yaml.safe_load(raw))
        except yaml.YAMLError:
            continue
        if not isinstance(task, dict) or not task.get("id"):
            continue
        tasks.append(task)
        if task.get("status") == "complete":
            complete_ids.add(str(task["id"]))
    state      = read_state()
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


def _normalize_task(task):
    """needs-dan ⇒ auto-prep. A `mode: needs-dan` task with no explicit dispatch is
    treated as `dispatch: manual`, so it flows through the B14 prep-and-hand pipeline
    (one action + /ask + /approve) instead of being launched as a normal implementer,
    failing the human step, and dead-parking. None-safe (yaml.safe_load may return None)."""
    if isinstance(task, dict) and task.get("mode") == "needs-dan" and not task.get("dispatch"):
        task["dispatch"] = "manual"
    return task


def get_task_by_id(task_id: str) -> dict | None:
    """Return the full task dict from ROADMAP.md, or None."""
    content = ROADMAP_FILE.read_text(encoding="utf-8")
    blocks  = re.findall(r"```yaml\n(.*?)```", content, re.DOTALL)
    for raw in blocks:
        try:
            task = _normalize_task(yaml.safe_load(raw))
        except yaml.YAMLError:
            continue
        if isinstance(task, dict) and str(task.get("id", "")) == str(task_id):
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


def mark_roadmap_complete(task_id: str) -> None:
    """Remove task from ROADMAP.md, log to completed_tasks.json, and COMMIT the
    graduation artifacts so the caller's `git push origin main` actually persists
    them. `maestro.docs.complete.main()` only writes files (no commit), and every
    graduation site does `mark_roadmap_complete` → `git push` with nothing staged in
    between, so without this commit the strip + registry append live only in the
    working tree and never reach git (P8B2/P8B3/P14 accumulated this way)."""
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
    subprocess.run(["git", "add", *grad_files], cwd=str(REPO), capture_output=True)
    # Only commit if something was actually staged (avoid an empty-commit error).
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"],
                            cwd=str(REPO)).returncode != 0
    if staged:
        subprocess.run(
            ["git", "commit", "--no-verify", "-m",
             f"roadmap({task_id}): graduate to project docs + registry"],
            cwd=str(REPO), capture_output=True,
        )
    append_journal("roadmap_task_removed", f"{task_id} removed from ROADMAP.md")


def _load_roadmap_tasks() -> list[dict]:
    """Parse all ```yaml fenced blocks in ROADMAP.md and return dicts that have an `id`.

    Returns every task present in the ROADMAP (graduated tasks are absent because
    mark_task_complete.py strips them out).  Callers must not mutate the dicts.
    """
    try:
        content = ROADMAP_FILE.read_text(encoding="utf-8")
    except OSError:
        return []
    blocks = re.findall(r"```yaml\n(.*?)```", content, re.DOTALL)
    tasks: list[dict] = []
    for raw in blocks:
        try:
            task = _normalize_task(yaml.safe_load(raw))
        except yaml.YAMLError:
            continue
        if isinstance(task, dict) and task.get("id"):
            tasks.append(task)
    return tasks
