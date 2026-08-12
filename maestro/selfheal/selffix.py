"""The self-fix layer: the eligibility gates, the runner spawn, the apply loop.

Extracted verbatim from the reference orchestrator. Bodies are unchanged — only the
import block, the derivation of the module-level path globals and the scratch-directory
prefix (which named the consuming project in the reference) differ. Behavioural surprises
are catalogued in `docs/found_bugs_inbox/selfheal.md` and pinned by
`tests/characterization/test_selfheal.py`; none of them is fixed here — including the
`lstrip("./")` character-set strip that lets `../` through the path gate, the substring
matching in the rate limiter, and the unreadable journal that fails OPEN.

Nothing here spawns a process for real in the tests: the tmux window and the two git
cleanup calls go through this module's `subprocess` reference, and the merge itself
through `_merge_self_fix_branch`, both of which the characterisation tests swap.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone

from maestro.docs.roadmap import run_dep_map
from maestro.hitl.telegram import notify_telegram
from maestro.implementer import _latest_impl_tail
from maestro.merge import _merge_self_fix_branch
from maestro.paths import Paths
from maestro.state import append_journal
from maestro.worktree import TMUX_SESSION

_PATHS = Paths.from_env()

REPO                  = _PATHS.repo
JOURNAL               = _PATHS.journal
# `_latest_impl_tail` reads the implementer logs through `maestro.implementer`; the
# reference exposes the workspaces root on the same module surface as everything below.
WORKSPACES            = _PATHS.workspaces
VENV_PYTHON           = REPO / ".venv" / "bin" / "python3"

# Maestro self-heal: for a whitelist of failure classes whose fix is confined to
# Maestro's OWN code (never the RAG bot runtime), auto-prepare + gate-check + apply a
# fix instead of only escalating. Kill switch: ORCH_SELF_FIX=0. The merge is performed
# by the orchestrator (the single git owner); the heavy lifting runs in a detached
# worktree runner. Guardrails below are deliberately conservative.
ORCH_SELF_FIX = os.environ.get("ORCH_SELF_FIX", "1") == "1"
# Failure classes eligible for an unattended self-fix (everything else → advisory).
SELF_FIX_WHITELIST = {"observability", "orchestrator-logic", "transient-infra"}
# A self-fix diff may ONLY touch these path prefixes …
SELF_FIX_PATHS = ("scripts/", "orchestrator/", "docs/")
# … and never these (RAG-bot runtime, secrets, data, the restart/bot surface). A single
# denied path vetoes the whole self-fix → advisory. main_bot.py et al. are bot runtime.
SELF_FIX_DENY = ("main_bot.py", ".env", "data/", "models/", "scripts/restart_bot.sh",
                 "scripts/watchdog.py", ".service", "requirements")
# At most one self-fix per failure-class per this many hours (journal-tracked loop break).
SELF_FIX_MIN_HOURS = 6.0
# Master enable prepares + gate-checks a self-fix unattended. This second flag controls
# whether a gate-PASSED branch auto-merges (zero-tap) or waits for Dan's one-tap apply.
# Default ON (Dan's call 2026-06-22): full unattended auto-apply — a gate-passed self-fix
# (whitelisted class + path allow/deny + per-class rate limit + ORCH_SELF_FIX kill switch)
# is merged by the orchestrator automatically, then Dan is notified. Set
# ORCH_SELF_FIX_MERGE=0 to fall back to ask-before-merge.
ORCH_SELF_FIX_MERGE = os.environ.get("ORCH_SELF_FIX_MERGE", "1") == "1"
SELFFIX_DIR = REPO / ".orchestrator" / "selffix"

# The reference implementation hardcodes a /tmp scratch prefix that embeds the consuming
# project's name. Only that literal is neutralised here; the shape is identical.
SELFFIX_WORKTREE_PREFIX = "/tmp/maestro-selffix-"


def _self_fix_path_ok(files) -> tuple:
    """A self-fix diff may touch ONLY SELF_FIX_PATHS and NONE of SELF_FIX_DENY."""
    files = [str(f).strip().lstrip("./") for f in (files or []) if str(f).strip()]
    if not files:
        return False, "no target_files identified"
    for fn in files:
        if any(d in fn for d in SELF_FIX_DENY):
            return False, f"denied path: {fn}"
        if not fn.startswith(SELF_FIX_PATHS):
            return False, f"out-of-scope path: {fn}"
    return True, "ok"


def _self_fix_rate_ok(failure_class: str) -> bool:
    """At most one self-fix per class per SELF_FIX_MIN_HOURS — journal loop break."""
    if not JOURNAL.exists():
        return True
    cutoff = datetime.now(timezone.utc).timestamp() - SELF_FIX_MIN_HOURS * 3600
    try:
        with open(JOURNAL, encoding="utf-8") as fh:
            for ln in fh:
                if "self_fix_applied" not in ln or failure_class not in ln:
                    continue
                try:
                    ts = datetime.strptime(json.loads(ln).get("ts", "")[:19],
                                           "%Y-%m-%dT%H:%M:%S")\
                        .replace(tzinfo=timezone.utc).timestamp()
                except Exception:
                    continue
                if ts >= cutoff:
                    return False
    except Exception:
        pass
    return True


def _self_fix_eligible(diag: dict) -> tuple:
    """Gate a diagnosis for an unattended self-fix. Returns (ok, why)."""
    if not ORCH_SELF_FIX:
        return False, "ORCH_SELF_FIX disabled"
    cls = (diag.get("failure_class") or "").strip()
    if cls not in SELF_FIX_WHITELIST:
        return False, f"class '{cls}' not in self-fix whitelist"
    ok, why = _self_fix_path_ok(diag.get("target_files"))
    if not ok:
        return False, why
    if not _self_fix_rate_ok(cls):
        return False, f"rate-limited: a '{cls}' self-fix ran within {SELF_FIX_MIN_HOURS}h"
    return True, "eligible"


def attempt_self_fix(task_id: str, reason: str, diag: dict) -> None:
    """Spawn the detached self-fix runner (maestro_selffix.py) in a tmux window. The
    runner prepares the confined fix in a worktree and GATE-checks it; the orchestrator
    merges a gate-passed branch on its next poll (single git owner). Never blocks here."""
    SELFFIX_DIR.mkdir(parents=True, exist_ok=True)
    fix_id = f"{task_id}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    req = SELFFIX_DIR / f"{fix_id}.request.json"
    req.write_text(json.dumps({
        "fix_id": fix_id, "task": task_id, "reason": reason[:800],
        "failure_class": diag.get("failure_class", ""),
        "root_cause": diag.get("root_cause", ""),
        "suggested_fix": diag.get("suggested_fix", ""),
        "target_files": diag.get("target_files", []),
        "impl_tail": _latest_impl_tail(task_id, 40)[:3000],
    }), encoding="utf-8")
    helper = REPO / "scripts" / "maestro_selffix.py"
    tmux_cmd = (f"tmux new-window -t {TMUX_SESSION} -n selffix-{task_id} "
                f"'cd {REPO} && {VENV_PYTHON} {helper} {req}'")
    try:
        subprocess.run(tmux_cmd, shell=True, check=True)
        append_journal("self_fix_started", f"{task_id} class={diag.get('failure_class')}")
        notify_telegram(
            f"🔧 *Maestro* self-fix started for `{task_id}` "
            f"(class: {diag.get('failure_class')}). I'll prepare + gate-check a fix "
            f"confined to my own code and report back.")
    except Exception as exc:
        print(f"  [self-fix] spawn failed: {exc}")


def apply_ready_self_fixes() -> None:
    """Main-loop hook: merge or surface self-fix branches the runner has gate-passed.
    The orchestrator is the single git owner, so the actual merge happens HERE."""
    if not SELFFIX_DIR.exists():
        return
    for ready in sorted(SELFFIX_DIR.glob("*.ready.json")):
        try:
            info = json.loads(ready.read_text(encoding="utf-8"))
        except Exception:
            ready.unlink(missing_ok=True)
            continue
        branch = info.get("branch", "")
        fclass = info.get("failure_class", "")
        summary = info.get("summary", "")
        if not branch:
            ready.unlink(missing_ok=True)
            continue
        if not ORCH_SELF_FIX_MERGE:
            # Conservative default: gate passed, but ask Dan before merging.
            notify_telegram(
                f"🔧 *Maestro* prepared a gate-passed self-fix on `{branch}`:\n{summary[:400]}\n\n"
                f"Auto-merge is off (ORCH_SELF_FIX_MERGE=0). Review and `git merge --no-ff {branch}` "
                f"to apply, or set ORCH_SELF_FIX_MERGE=1 for zero-tap.")
            ready.rename(ready.with_suffix(".notified"))
            continue
        ok, detail = _merge_self_fix_branch(branch)
        if ok:
            append_journal("self_fix_applied", f"{info.get('task')} class={fclass} {detail}")
            notify_telegram(f"✅ *Maestro* self-fix applied: {detail}\n{summary[:300]}")
            run_dep_map()
            # Clean up the runner's worktree + branch (worktree first; -D needs it gone).
            fid = branch[len("selffix-"):]
            subprocess.run(["git", "worktree", "remove", "--force",
                            f"{SELFFIX_WORKTREE_PREFIX}{fid}"], cwd=str(REPO), capture_output=True)
            subprocess.run(["git", "branch", "-D", branch],
                           cwd=str(REPO), capture_output=True)
        else:
            append_journal("self_fix_merge_failed", f"{branch} {detail}")
            notify_telegram(f"⚠ *Maestro* self-fix on `{branch}` failed the merge gate: "
                            f"{detail}. Left for manual review.")
        ready.unlink(missing_ok=True)
