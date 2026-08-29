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

R10 (M4c batch 3) folds in the RUNNER side, ported verbatim from the reference's
standalone `scripts/maestro_selffix.py` — the process `attempt_self_fix` used to spawn in
a dedicated tmux window. It prepares a fix in a confined worktree, gates it (path
confinement + a byte-compile check + >=1 commit), and drops the `.ready.json` this
module's own `apply_ready_self_fixes` picks up. The only non-verbatim change is the `/tmp`
scratch prefix, which named the consuming project in the reference — neutralised the same
way `SELFFIX_WORKTREE_PREFIX` already neutralised it above.

Since 2026-08-26 the fix itself goes through `maestro.agentcall` rather than naming a CLI,
so an unattended self-fix runs on whichever backend the project configured. Like `/redo`
it is an *agentic* one-shot call (`writable=True`): it is expected to edit files and commit
inside its worktree, and a driver that can enforce that boundary is asked to.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from maestro import agentcall
from maestro.docs.roadmap import run_dep_map
from maestro.hitl.telegram import notify_telegram
from maestro.implementer import _latest_impl_tail
from maestro.merge import _merge_self_fix_branch
from maestro.paths import Paths
from maestro.roles import ROLE_IMPLEMENTER
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

# `SELF_FIX_MODEL` used to live here, pinning `claude-opus-4-8`. Repairing maestro's own
# code is implementer work, so the model now comes from `roles.implementer`'s table.
#: Thirty minutes, matching `/redo`: a self-fix is real work, not a judgment.
SELF_FIX_TIMEOUT = 1800
LIMIT_HINT = ("hit your limit", "usage limit", "rate limit", "too many requests")


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
    tmux_cmd = (f"tmux new-window -t {TMUX_SESSION} -n selffix-{task_id} "
                f"'cd {REPO} && {VENV_PYTHON} -m maestro.selfheal.selffix {req}'")
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


# ── RUNNER (R10): prepare/gate an unattended fix to Maestro's OWN code, hand a passed
# branch to the merge loop above. Ported verbatim from the reference's standalone
# scripts/maestro_selffix.py. ──────────────────────────────────────────────────────────


def _run_git(args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(REPO), capture_output=True, text=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("request")
    req = json.loads(Path(ap.parse_args().request).read_text(encoding="utf-8"))
    fix_id = req["fix_id"]
    task = req.get("task", "?")
    fclass = req.get("failure_class", "")
    branch = f"selffix-{fix_id}"
    wt = Path(f"{SELFFIX_WORKTREE_PREFIX}{fix_id}")

    # Fresh worktree off local main.
    if wt.exists():
        _run_git(["worktree", "remove", "--force", str(wt)])
    r = _run_git(["worktree", "add", "-b", branch, str(wt), "main"])
    if r.returncode != 0:
        notify_telegram(f"⚠ Maestro self-fix {task}: worktree add failed: {(r.stderr or '')[:160]}")
        return 1

    brief = (
        f"You are Maestro repairing your OWN orchestration code after task {task} failed. "
        f"Work ONLY inside this worktree: {wt}\n\n"
        f"FAILURE CLASS: {fclass}\n"
        f"ROOT CAUSE (diagnosis): {req.get('root_cause','')}\n"
        f"SUGGESTED FIX: {req.get('suggested_fix','')}\n"
        f"FAILURE REASON: {req.get('reason','')}\n\n"
        f"IMPLEMENTER LOG TAIL:\n{req.get('impl_tail','')}\n\n"
        f"RULES:\n"
        f"- Understand the ROOT CAUSE and fix it so this class of failure cannot recur — "
        f"do NOT just patch one symptom.\n"
        f"- Make the MINIMAL change. Touch ONLY files under scripts/, orchestrator/, docs/.\n"
        f"- NEVER touch main_bot.py, .env, data/, models/, scripts/watchdog.py, "
        f"scripts/restart_bot.sh, or any .service file — these are the RAG bot runtime.\n"
        f"- After editing, byte-compile every changed .py (python -m py_compile <file>).\n"
        f"- Commit inside the worktree: git add -A && git commit -m 'selffix({task}): <summary>'.\n"
        f"- Do NOT push, do NOT merge, do NOT restart anything."
    )
    log = wt / "selffix_impl.log"
    # The transcript, not just the answer: the usage-limit check below only has anything
    # to read on a run that did not simply succeed, and `Completion.text` is empty there
    # by design. The driver writes the run's combined stdout+stderr to `log`.
    answer = agentcall.ask(
        ROLE_IMPLEMENTER, brief,
        timeout=SELF_FIX_TIMEOUT, cwd=wt, log_file=log, writable=True,
    )
    if answer.timed_out:
        out = "(timed out)"
    else:
        try:
            out = log.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            out = f"(error: {exc})"

    if any(h in out.lower() for h in LIMIT_HINT) and "commit" not in out.lower():
        notify_telegram(f"⏳ Maestro self-fix {task} aborted — Claude usage limit. Will retry on a future failure.")
        _run_git(["worktree", "remove", "--force", str(wt)])
        _run_git(["branch", "-D", branch])
        return 0

    # Gate 1: there must be a commit.
    commits = _run_git(["log", "--oneline", f"main..{branch}"]).stdout.strip()
    if not commits:
        notify_telegram(f"🔧 Maestro self-fix {task}: produced no commit — left as advisory.\n"
               f"Suggested fix: {req.get('suggested_fix','')[:300]}")
        _run_git(["worktree", "remove", "--force", str(wt)])
        _run_git(["branch", "-D", branch])
        return 0

    # Gate 2: diff confined to the safe surface.
    changed = _run_git(["diff", "--name-only", f"main...{branch}"]).stdout.split()
    ok, why = _self_fix_path_ok(changed)
    if not ok:
        notify_telegram(f"🛑 Maestro self-fix {task} REJECTED by path gate ({why}). "
               f"Discarded; escalating as advisory instead.\nSuggested fix: "
               f"{req.get('suggested_fix','')[:240]}")
        _run_git(["worktree", "remove", "--force", str(wt)])
        _run_git(["branch", "-D", branch])
        return 0

    # Gate 3: every changed .py byte-compiles.
    pyfiles = [c for c in changed if c.endswith(".py")]
    cr = subprocess.run([sys.executable, "-m", "py_compile", *pyfiles],
                        cwd=str(REPO), capture_output=True, text=True) if pyfiles else None
    if cr is not None and cr.returncode != 0:
        notify_telegram(f"🛑 Maestro self-fix {task} failed compile gate:\n{(cr.stderr or '')[:300]}\n"
               f"Discarded.")
        _run_git(["worktree", "remove", "--force", str(wt)])
        _run_git(["branch", "-D", branch])
        return 0

    # Gate passed — hand the branch to the orchestrator to merge (single git owner).
    SELFFIX_DIR.mkdir(parents=True, exist_ok=True)
    summary = f"{commits.splitlines()[0] if commits else ''}  | files: {', '.join(changed)}"
    (SELFFIX_DIR / f"{fix_id}.ready.json").write_text(json.dumps({
        "branch": branch, "task": task, "failure_class": fclass,
        "summary": summary, "changed": changed,
    }), encoding="utf-8")
    # Leave the worktree in place until the merge (orchestrator cleans up via branch).
    notify_telegram(f"🔧 Maestro self-fix for `{task}` passed the gate ({len(changed)} file(s)): "
           f"{summary[:260]}\nThe orchestrator will merge it (or ask you, per ORCH_SELF_FIX_MERGE).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
