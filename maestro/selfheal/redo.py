"""`/redo`: the deliverable-rewrite allowlist and the branch-landing loop.

Extracted verbatim from the reference orchestrator. Bodies are unchanged — only the
import block and the derivation of the module-level path globals differ. Behavioural
surprises are catalogued in `docs/found_bugs_inbox/selfheal.md` and pinned by
`tests/characterization/test_selfheal.py`; none of them is fixed here — including the
missing `files or []` guard that its self-fix twin has, the same `lstrip("./")`
character-set strip, and the silent success (no notification) on a landed rewrite.

The merge goes through `_merge_redo_branch` and the worktree/branch cleanup through this
module's `subprocess` reference; the characterisation tests swap both.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from maestro.docs.roadmap import run_dep_map
from maestro.hitl.telegram import notify_telegram
from maestro.merge import _merge_redo_branch
from maestro.paths import Paths
from maestro.state import append_journal

_PATHS = Paths.from_env()

REPO                  = _PATHS.repo
# `/redo`: maestro_redo.py drops a gate-passed branch here for the orchestrator (single
# git owner) to merge — the deliverable-rewrite analogue of SELFFIX_DIR.
REDO_DIR = REPO / ".orchestrator" / "redo"

# `/redo` confinement, re-checked at merge time (mirrors scripts/maestro_redo.py).
_REDO_ALLOW = ("colab/", "data_export/", "scripts/", "docs/", "tasks/")
_REDO_DENY = ("main_bot.py", ".env", "data/", "models/", "scripts/restart_bot.sh",
              "scripts/watchdog.py", ".service", "requirements")
# Maestro sidecars now live outside the worktree; ignore any stray in-tree copy so a good
# rewrite is never discarded over a Maestro control file (mirrors maestro_redo.GATE_IGNORE).
_REDO_GATE_IGNORE = ("redo_result.json", "redo_impl.log")


def _redo_path_ok(files) -> tuple:
    files = [str(f).strip().lstrip("./") for f in files if str(f).strip()]
    files = [f for f in files if Path(f).name not in _REDO_GATE_IGNORE]
    if not files:
        return False, "no files changed"
    for fn in files:
        if any(d in fn for d in _REDO_DENY):
            return False, f"denied path touched: {fn}"
        if not fn.startswith(_REDO_ALLOW):
            return False, f"out-of-scope path touched: {fn}"
    return True, "ok"


def apply_ready_redo() -> None:
    """Main-loop hook: merge /redo branches the runner gate-passed (single git owner).
    The runner already re-uploaded the notebook to Drive + notified Dan; this just lands
    the committed deliverable on main and cleans up the worktree/branch."""
    if not REDO_DIR.exists():
        return
    for ready in sorted(REDO_DIR.glob("*.ready.json")):
        try:
            info = json.loads(ready.read_text(encoding="utf-8"))
        except Exception:
            ready.unlink(missing_ok=True)
            continue
        branch = info.get("branch", "")
        if not branch:
            ready.unlink(missing_ok=True)
            continue
        ok, detail = _merge_redo_branch(branch)
        if ok:
            append_journal("redo_applied", f"{info.get('task')} {detail}")
            run_dep_map()
            wt = info.get("worktree", "")
            if wt:
                subprocess.run(["git", "worktree", "remove", "--force", wt],
                               cwd=str(REPO), capture_output=True)
            subprocess.run(["git", "branch", "-D", branch],
                           cwd=str(REPO), capture_output=True)
        else:
            append_journal("redo_merge_failed", f"{branch} {detail}")
            notify_telegram(f"⚠ *Maestro* /redo branch `{branch}` failed the merge gate: "
                            f"{detail}. The notebook was re-uploaded but the commit needs "
                            f"manual review.")
        ready.unlink(missing_ok=True)
