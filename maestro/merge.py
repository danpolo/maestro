"""Landing a branch on main: the deny-list, the risky reviewer, and every merge path.

Extracted verbatim from the reference orchestrator. Bodies are unchanged — only the
import block and the derivation of the module-level path globals differ. Names owned by
modules that are extracted later (the judge CLI, the smoke runner, the dependency-map
regeneration, the HITL notifiers and the self-fix / redo path gates) are bound to
`deferred()` late bindings so the call sites stay byte-for-byte identical. Behavioural
surprises are catalogued in `docs/FOUND_BUGS.md` and pinned by
`tests/characterization/test_merge.py`; none of them is fixed here.
"""
from __future__ import annotations

import json
import re
import subprocess

from maestro.config import _load_project_yaml
from maestro.hitl.telegram import notify_telegram
from maestro.paths import Paths
from maestro.pending import deferred
from maestro.state import append_journal

_PATHS = Paths.from_env()

REPO                  = _PATHS.repo

# Every name below is owned by a module that sits *after* this one in the M1 extraction
# order, so a top-level import would invert an edge those modules already depend on.
# `deferred` resolves each on first call instead. They stay ordinary module attributes,
# so the characterisation tests monkeypatch them exactly as before.
_danreq = deferred("_danreq", "maestro.hitl.telegram")
# The mapping table (docs/plans/2026-08-09-m0-m1-core-extraction.md L76) puts
# `_judge_complete` in `selfheal/diagnose.py`. The placeholder here named `maestro.judge`,
# a module that was never planned and does not exist — see M4a in `docs/PROGRESS.md`.
_judge_complete = deferred("_judge_complete", "maestro.selfheal.diagnose")
# Likewise L70 puts `_smoke_for_task` in `gates.py`, not in a `maestro.smoke` module.
_smoke_for_task = deferred("_smoke_for_task", "maestro.gates")
run_dep_map = deferred("run_dep_map", "maestro.docs.roadmap")
# L77/L78 split these across the two `selfheal` submodules; the package itself exports
# neither, so the old package-level owner would not have resolved either.
_self_fix_path_ok = deferred("_self_fix_path_ok", "maestro.selfheal.selffix")
_redo_path_ok = deferred("_redo_path_ok", "maestro.selfheal.redo")


def _diff_files(branch: str) -> list[str]:
    """Return list of files changed in branch vs main."""
    r = subprocess.run(
        ["git", "diff", "--name-only", f"main...{branch}"],
        cwd=str(REPO), capture_output=True, text=True,
    )
    return [f.strip() for f in r.stdout.splitlines() if f.strip()]


# ── B6: Deny-list guard ──

_HARD_DENY_PATTERNS = [
    r"\bsudo\b(?!.*restart_bot\.sh)(?!.*systemctl\b)",
    r"git\s+push\s+.*--force",
    r"git\s+push\s+-f\b",
    r"DROP\s+TABLE",
    r"DELETE\s+FROM",
    r"--force-reset-index",
    r"TELEGRAM_BOT_TOKEN\s*=",
    r"open\s*\(\s*['\"]\.env['\"]",  # direct open(".env") — path-building is fine
]


def deny_list_guard(branch: str, task_id: str) -> tuple[bool, str]:
    """Check diff of branch against hard deny-list.

    Returns (ok, reason). ok=False means escalate to Dan, do not merge.
    """
    proj = _load_project_yaml()
    extra_patterns = proj.get("deny_list_extra", [])
    all_patterns = _HARD_DENY_PATTERNS + extra_patterns

    r = subprocess.run(
        ["git", "diff", f"main...{branch}"],
        cwd=str(REPO), capture_output=True, text=True,
    )
    diff_text = r.stdout

    for pat in all_patterns:
        m = re.search(pat, diff_text)
        if m:
            return False, f"deny-list violation: pattern '{pat}' matched in diff"

    # Never allow changes to secret files
    secrets = proj.get("secrets", [".env", ".env.local"])
    changed = _diff_files(branch)
    for secret_pat in secrets:
        clean = secret_pat.strip("*").strip("/")
        for f in changed:
            if clean in f:
                return False, f"deny-list violation: secret file in diff: {f}"

    return True, "ok"


# ── B6: Sonnet risky-set reviewer ──

def sonnet_risky_reviewer(branch: str, task_id: str) -> tuple[bool, str]:
    """If branch touches risky_set files, spawn a Sonnet call to review.

    Returns (pass, reason). pass=False → escalate to Dan.
    """
    proj = _load_project_yaml()
    risky_set = proj.get("risky_set", [])
    if not risky_set:
        return True, "no risky_set defined"

    changed = _diff_files(branch)
    risky_touched = [f for f in changed if any(r in f for r in risky_set)]
    if not risky_touched:
        return True, "no risky_set files touched"

    print(f"  [risky-reviewer] {task_id} touches risky files: {risky_touched}")

    diff_r = subprocess.run(
        ["git", "diff", f"main...{branch}", "--", *risky_touched],
        cwd=str(REPO), capture_output=True, text=True,
    )
    diff_snippet = diff_r.stdout[:6000]

    try:
        # Routed through the CLI judge (subscription, no metered API). The safety
        # gate stays on Sonnet: it runs per risky merge, Sonnet 4.6 reviews code
        # well, and this keeps strong-model budget for proposal-shaping.
        raw = _judge_complete(
            system=(
                "You are a read-only security and correctness reviewer for an autonomous "
                "AI orchestration system. Review the provided diff for: "
                "(1) safety regressions against the deny-list in operating_preamble.md, "
                "(2) correctness issues that could break the orchestrator runtime, "
                "(3) any other serious problems. "
                "Respond with JSON only: {\"pass\": true|false, \"reason\": \"...\"}"
            ),
            user=(
                f"Task: {task_id}\nRisky files touched: {risky_touched}\n\nDiff:\n```\n{diff_snippet}\n```\n"
                "Output JSON only."
            ),
            model="claude-sonnet-5",
        )
        if not raw:
            return False, "risky reviewer unavailable (CLI judge returned nothing)"
        # Extract JSON even if wrapped in markdown
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        obj = json.loads(m.group(0)) if m else {"pass": False, "reason": f"parse error: {raw[:80]}"}
        return bool(obj.get("pass", False)), obj.get("reason", "")
    except Exception as exc:
        # On any error, escalate to Dan rather than silently proceeding
        return False, f"risky reviewer error: {exc}"


def _touches_bot_files(branch: str) -> bool:
    """Return True if branch diff includes any bot_files from project.yaml."""
    proj = _load_project_yaml()
    bot_files = proj.get("bot_files", ["main_bot.py"])
    changed = _diff_files(branch)
    return any(any(bf in f for bf in bot_files) for f in changed)


def _git_head(short: bool = False) -> str:
    fmt  = "--short" if short else ""
    args = ["git", "rev-parse"] + ([fmt] if fmt else []) + ["HEAD"]
    return subprocess.run(args, cwd=str(REPO), capture_output=True, text=True).stdout.strip()


_EVAL_HISTORY = "eval/history.jsonl"


def _commit_eval_history(task_id: str = "") -> bool:
    """Commit any pending append to eval/history.jsonl so main is never left dirty.

    The retrieval smoke (and the nightly eval) run eval/runner.py, which APPENDS a
    row to eval/history.jsonl in the working tree without committing it. An
    uncommitted row blocks the NEXT task's `git merge` — that is the dirty-worktree
    false-regression bug. Committing here (rather than discarding) keeps the
    telemetry. No-op when the file is already clean. Returns True if it committed."""
    st = subprocess.run(
        ["git", "-C", str(REPO), "status", "--porcelain", "--", _EVAL_HISTORY],
        capture_output=True, text=True,
    )
    if not st.stdout.strip():
        return False
    subprocess.run(["git", "-C", str(REPO), "add", "--", _EVAL_HISTORY], capture_output=True)
    subprocess.run(
        ["git", "-C", str(REPO), "commit", "-m",
         f"chore(eval): record smoke history row {task_id}".strip()],
        capture_output=True,
    )
    append_journal("eval_history_committed", f"{task_id} {_EVAL_HISTORY}")
    return True


# ── Merge and eval ──

def merge_and_eval(entry: dict) -> tuple[bool, dict]:
    """Merge branch → smoke → keep or revert.

    Problem 11: refuses no-op merges (branch introduces no new commits vs main).
    """
    task_id  = entry["task_id"]
    branch   = entry.get("branch") or entry["session_id"].lower()

    # Problem 11 — refuse before attempting the merge
    no_new = subprocess.run(
        ["git", "log", "--oneline", f"main..{branch}"],
        cwd=str(REPO), capture_output=True, text=True
    )
    if not no_new.stdout.strip():
        reason = f"no-op merge: branch {branch} has no commits ahead of main"
        print(f"  [merge] REFUSED — {reason}")
        append_journal("merge_noop_refused", f"{task_id} {reason}",
                       session_id=entry["session_id"])
        return False, {"pass": False, "metrics": {}, "reason": reason}

    # B6: Deny-list guard — fires before any merge attempt
    dl_ok, dl_reason = deny_list_guard(branch, task_id)
    if not dl_ok:
        print(f"  [merge] DENIED by deny-list: {dl_reason}")
        append_journal("merge_denied_denylist", f"{task_id} {dl_reason}",
                       session_id=entry["session_id"])
        _danreq(
            f"Deny-list violation on {task_id}: {dl_reason}. Review branch {branch} and decide.",
            ["Abort and shelve", "Override and merge manually"],
        )
        return False, {"pass": False, "metrics": {}, "reason": dl_reason}

    # B6: Sonnet risky-set reviewer
    rs_ok, rs_reason = sonnet_risky_reviewer(branch, task_id)
    if not rs_ok:
        print(f"  [merge] ESCALATED by risky reviewer: {rs_reason}")
        append_journal("merge_risky_escalated", f"{task_id} {rs_reason}",
                       session_id=entry["session_id"])
        _danreq(
            f"Risky-set reviewer blocked {task_id}: {rs_reason[:200]}. Review branch {branch}.",
            ["Abort and shelve", "Override and merge manually"],
        )
        return False, {"pass": False, "metrics": {}, "reason": f"risky reviewer: {rs_reason}"}

    sha_pre = _git_head()
    print(f"  [merge] {task_id}: merging {branch} …")
    # Guard the live working tree: discard dirty regenerable artifacts (dep map) that would
    # otherwise make `git merge` abort with "local changes would be overwritten"; refuse
    # honestly if a non-regenerable file blocks it (rather than a misleading merge failure).
    changed_files = subprocess.run(
        ["git", "-C", str(REPO), "diff", "--name-only", f"main...{branch}"],
        capture_output=True, text=True).stdout.split()
    wt_ok, wt_blocking = _clean_worktree_for_merge(changed_files)
    if not wt_ok:
        reason = f"dirty worktree blocks merge: {wt_blocking}"
        print(f"  [merge] {reason}")
        append_journal("merge_worktree_dirty", f"{task_id} {reason}", session_id=entry["session_id"])
        return False, {"pass": False, "metrics": {}, "reason": reason}
    merge_r = subprocess.run(
        ["git", "merge", "--no-ff", branch, "-m", f"merge({task_id}): {branch}"],
        cwd=str(REPO), capture_output=True, text=True)
    if merge_r.returncode != 0 and not _autoresolve_doc_conflicts():
        err = (merge_r.stderr or merge_r.stdout)[:300]
        print(f"  [merge] git merge failed: {err}")
        append_journal("merge_failed", f"{task_id} {err}", session_id=entry["session_id"])
        subprocess.run(["git", "merge", "--abort"], cwd=str(REPO), capture_output=True)
        return False, {"pass": False, "metrics": {}, "reason": f"git merge failed: {err}"}

    sha_post      = _git_head()
    merge_created = sha_post != sha_pre
    sha_short     = _git_head(short=True)
    append_journal("merge_commit",
                   f"{task_id} branch={branch} sha={sha_short} new_commit={merge_created}",
                   session_id=entry["session_id"])
    print(f"  [merge] Merged at {sha_short} (new_commit={merge_created}). Running smoke …")
    smoke = _smoke_for_task(task_id, session_id=entry["session_id"])
    # Commit the smoke's history.jsonl append immediately so main is never left
    # dirty (otherwise the NEXT task's `git merge` is blocked — the dirty-worktree
    # false-regression bug). Done before the accept/revert split so it holds on
    # every path; the revert below resets to sha_pre, which undoes this commit too.
    _commit_eval_history(task_id)
    r5    = smoke.get("metrics", {}).get("recall_at_5", "?")
    if smoke.get("metrics"):
        print(f"  [smoke] {task_id}: pass={smoke['pass']} recall@5={r5}")
    else:
        print(f"  [smoke] {task_id}: pass={smoke['pass']} — {smoke.get('reason','')[:100]}")
    if smoke["pass"]:
        append_journal("merge_accepted", f"{task_id} sha={sha_short} recall@5={r5}",
                       session_id=entry["session_id"])
        return True, smoke
    # Reset to the pre-merge SHA captured above. _commit_eval_history may have
    # added a history-row commit on top of the merge, so HEAD~1 is no longer
    # guaranteed to be the pre-merge state — sha_pre always is.
    if _git_head() != sha_pre:
        print(f"  [merge] Smoke failed — reverting to {sha_pre[:9]} …")
        subprocess.run(["git", "reset", "--hard", sha_pre], cwd=str(REPO), capture_output=True)
    else:
        print(f"  [merge] Smoke failed — nothing to revert (no new commits)")
    append_journal("merge_reverted",
                   f"{task_id} sha={sha_short} recall@5={r5} reason={smoke.get('reason','')}",
                   session_id=entry["session_id"])
    return False, smoke


def _merge_self_fix_branch(branch: str) -> tuple:
    """Merge a gate-passed self-fix branch onto main. Reuses the dirty-worktree guard.
    Returns (ok, detail)."""
    has_commits = subprocess.run(
        ["git", "log", "--oneline", f"main..{branch}"],
        cwd=str(REPO), capture_output=True, text=True).stdout.strip()
    if not has_commits:
        return False, f"no commits on {branch}"
    changed = subprocess.run(
        ["git", "-C", str(REPO), "diff", "--name-only", f"main...{branch}"],
        capture_output=True, text=True).stdout.split()
    ok, why = _self_fix_path_ok(changed)          # re-check the gate at merge time
    if not ok:
        return False, f"merge-time path gate failed: {why}"
    wt_ok, blocking = _clean_worktree_for_merge(changed)
    if not wt_ok:
        return False, f"dirty worktree blocks merge: {blocking}"
    merge_r = subprocess.run(
        ["git", "merge", "--no-ff", branch, "-m", f"selffix: {branch}"],
        cwd=str(REPO), capture_output=True, text=True)
    if merge_r.returncode != 0:
        subprocess.run(["git", "merge", "--abort"], cwd=str(REPO), capture_output=True)
        return False, f"git merge failed: {(merge_r.stderr or merge_r.stdout)[:200]}"
    return True, f"merged {branch} at {_git_head(short=True)}"


def _merge_redo_branch(branch: str) -> tuple:
    """Merge a gate-passed /redo branch onto main. Path-gated to the deliverable surface.
    Returns (ok, detail)."""
    has_commits = subprocess.run(
        ["git", "log", "--oneline", f"main..{branch}"],
        cwd=str(REPO), capture_output=True, text=True).stdout.strip()
    if not has_commits:
        return False, f"no commits on {branch}"
    changed = subprocess.run(
        ["git", "-C", str(REPO), "diff", "--name-only", f"main...{branch}"],
        capture_output=True, text=True).stdout.split()
    ok, why = _redo_path_ok(changed)
    if not ok:
        return False, f"merge-time path gate failed: {why}"
    wt_ok, blocking = _clean_worktree_for_merge(changed)
    if not wt_ok:
        return False, f"dirty worktree blocks merge: {blocking}"
    merge_r = subprocess.run(
        ["git", "merge", "--no-ff", branch, "-m", f"redo: {branch}"],
        cwd=str(REPO), capture_output=True, text=True)
    if merge_r.returncode != 0:
        subprocess.run(["git", "merge", "--abort"], cwd=str(REPO), capture_output=True)
        return False, f"git merge failed: {(merge_r.stderr or merge_r.stdout)[:200]}"
    return True, f"merged {branch} at {_git_head(short=True)}"


def _merge_prep_branch(entry: dict) -> tuple[bool, str]:
    """Merge a PREP implementer's branch onto main — NO smoke, NO graduation.

    Prep changes are scripts/data/notebooks, not retrieval changes, so the eval gate
    does not apply. A prep run may legitimately have no commits (e.g. a pure Drive
    upload) — that is success, not a no-op refusal. Reuses the dirty-worktree guard.
    """
    branch  = entry.get("branch") or entry["session_id"].lower()
    task_id = entry["task_id"]
    has_commits = subprocess.run(
        ["git", "log", "--oneline", f"main..{branch}"],
        cwd=str(REPO), capture_output=True, text=True).stdout.strip()
    if not has_commits:
        return True, f"no prep commits on {branch} (nothing to merge)"
    changed_files = subprocess.run(
        ["git", "-C", str(REPO), "diff", "--name-only", f"main...{branch}"],
        capture_output=True, text=True).stdout.split()
    wt_ok, wt_blocking = _clean_worktree_for_merge(changed_files)
    if not wt_ok:
        return False, f"dirty worktree blocks prep merge: {wt_blocking}"
    merge_r = subprocess.run(
        ["git", "merge", "--no-ff", branch, "-m", f"prep({task_id}): {branch}"],
        cwd=str(REPO), capture_output=True, text=True)
    if merge_r.returncode != 0:
        # A conflict only on regenerable docs (dep map / UPCOMING) is spurious —
        # regenerate them from the merged ROADMAP and finish the merge.
        if not _autoresolve_doc_conflicts():
            err = (merge_r.stderr or merge_r.stdout)[:300]
            subprocess.run(["git", "merge", "--abort"], cwd=str(REPO), capture_output=True)
            return False, f"git merge failed: {err}"
    return True, f"merged {branch} at {_git_head(short=True)}"


# ── INCOMPLETE / resumable (multi-day, quota-bound) task handling ──

RESUMABLE_MERGE_PREFIXES  = ("data/", "docs/")  # paths a resumable sitting may auto-merge (no eval): data artifacts + docs (can't affect the pipeline)
# Orchestrator runtime + prod-critical files that must NEVER auto-merge on the no-eval
# resumable path, even with Opus approval — a bad change here can brick the loop or prod.
# (risky_set / bot_files from project.yaml are added to this set at check time.)
_RESUMABLE_HARD_STOP = ("maestro/orchestrator.py", "maestro/watchdog.py")

# Regenerable auto-gen artifacts the orchestrator may safely discard before a merge.
# These are produced by scripts/gen_dependency_map.py (the .png is non-deterministic
# binary; the .md regenerates on any ROADMAP touch) and chronically sit dirty in the
# orchestrator's checkout. They must never be hand-edited (see CLAUDE.md docs model).
_MERGE_DISCARDABLE = ("docs/dependency_map.md", "docs/dependency_map.png", "docs/UPCOMING.md")
# Append-only telemetry that must be COMMITTED (not discarded — that loses rows)
# to clear a dirty-worktree merge block, whatever wrote it (smoke or nightly eval).
_MERGE_COMMITTABLE = (_EVAL_HISTORY,)


def _clean_worktree_for_merge(changed: list[str]) -> tuple[bool, list[str]]:
    """Make the working tree clean enough for `git merge` to apply `changed`.

    `git merge` aborts ("Your local changes … would be overwritten by merge") when an
    uncommitted *tracked* file overlaps the merge — even for a clean fast-forward
    descendant. Discard dirty regenerable artifacts (dep map) that overlap; return any
    OTHER dirty overlap as `blocking` so the caller escalates with an honest reason
    instead of a bogus "conflict". No overlap → nothing to do.

    Returns (ok, blocking_paths): ok=False only when a non-regenerable file would block
    the merge (we never silently clobber real uncommitted work).
    """
    st = subprocess.run(
        ["git", "-C", str(REPO), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True, text=True,
    )
    if st.returncode != 0:
        return True, []                       # best-effort; let the merge surface real problems
    dirty   = {ln[3:] for ln in st.stdout.splitlines() if ln.strip()}
    overlap = [p for p in changed if p in dirty]
    discardable = [p for p in overlap if p in _MERGE_DISCARDABLE]
    committable = [p for p in overlap if p in _MERGE_COMMITTABLE]
    blocking    = [p for p in overlap
                   if p not in _MERGE_DISCARDABLE and p not in _MERGE_COMMITTABLE]
    if discardable:
        subprocess.run(["git", "-C", str(REPO), "checkout", "HEAD", "--", *discardable],
                       capture_output=True)
        append_journal("merge_worktree_cleaned", f"discarded dirty artifacts={discardable}")
    if committable:
        subprocess.run(["git", "-C", str(REPO), "add", "--", *committable], capture_output=True)
        subprocess.run(["git", "-C", str(REPO), "commit", "-m",
                        "chore(eval): commit pending history rows before merge"],
                       capture_output=True)
        append_journal("merge_worktree_committed", f"committed dirty telemetry={committable}")
    return (not blocking), blocking


def _autoresolve_doc_conflicts() -> bool:
    """Complete an in-progress `git merge` that conflicts ONLY on regenerable docs.

    Graduations commit fresh dep map / UPCOMING to main, so any branch that started
    before a graduation conflicts on those auto-generated files when it merges. They
    are derived from ROADMAP (which merges cleanly on its own), so the correct
    resolution is to regenerate them from the merged tree and finish the merge —
    not to false-fail and park. Returns True if the merge was completed; False if any
    NON-regenerable file is conflicted (caller must `git merge --abort` and fail
    honestly — real code/data conflicts are never auto-resolved)."""
    conflicted = subprocess.run(
        ["git", "-C", str(REPO), "diff", "--name-only", "--diff-filter=U"],
        capture_output=True, text=True).stdout.split()
    if not conflicted or any(c not in _MERGE_DISCARDABLE for c in conflicted):
        return False
    run_dep_map()  # regenerates dep map (.md/.png) + UPCOMING.md from the merged ROADMAP
    subprocess.run(["git", "-C", str(REPO), "add", "--", *_MERGE_DISCARDABLE], capture_output=True)
    done = subprocess.run(["git", "-C", str(REPO), "commit", "--no-edit"],
                          capture_output=True, text=True)
    if done.returncode != 0:
        return False
    append_journal("merge_docs_autoresolved", f"regenerated conflicted={conflicted}")
    return True


_PROGRESS_RE          = re.compile(r"PROGRESS=(\d+)/(\d+)")
_PROGRESS_FALLBACK_RE = re.compile(r"(\d+)\s*/\s*(\d+)")


def _parse_progress(gate_msg: str) -> tuple[int, int] | None:
    """Pull (done, total) from the gate's echoed stdout. Prefers the explicit
    `PROGRESS=done/total` marker; falls back to the first `N/M` it finds."""
    m = _PROGRESS_RE.search(gate_msg) or _PROGRESS_FALLBACK_RE.search(gate_msg)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _merge_data_only(entry: dict) -> str:
    """Merge a resumable sitting's partial progress from its branch into main, with NO
    eval/smoke. Returns: 'ok', 'empty' (nothing committed), 'code_change' (non-data paths
    touched → escalate), 'dirty_worktree' (a non-regenerable uncommitted file blocks the
    merge → escalate honestly), 'conflict', or 'error'."""
    branch = entry.get("branch", "")
    if not branch:
        return "error"
    diff = subprocess.run(
        ["git", "-C", str(REPO), "diff", "--name-only", f"main...{branch}"],
        capture_output=True, text=True,
    )
    if diff.returncode != 0:
        return "error"
    changed = [p for p in diff.stdout.splitlines() if p.strip()]
    if not changed:
        return "empty"
    if any(not p.startswith(RESUMABLE_MERGE_PREFIXES) for p in changed):
        return "code_change"
    ok, blocking = _clean_worktree_for_merge(changed)
    if not ok:
        append_journal("merge_worktree_dirty", f"blocking={blocking}")
        return "dirty_worktree"
    merge = subprocess.run(
        ["git", "-C", str(REPO), "merge", "--no-ff", branch,
         "-m", f"merge(resume): partial data progress from {branch}"],
        capture_output=True, text=True,
    )
    if merge.returncode != 0:
        subprocess.run(["git", "-C", str(REPO), "merge", "--abort"], capture_output=True)
        return "conflict"
    subprocess.run(["git", "-C", str(REPO), "push", "origin", "main"],
                   cwd=str(REPO), capture_output=True)
    return "ok"


def _resumable_code_change_escalation(entry: dict, task_id: str) -> tuple[bool, str, str]:
    """A resumable sitting touched files OUTSIDE data/+docs/. Rather than parking for
    Dan, route the out-of-allowlist diff to the Opus judge (more autonomy; Dan trusts
    Opus's call). Deterministic hard-stop first: orchestrator runtime / risky_set /
    bot_files always escalate to Dan, never Opus. On approval, do a full merge of the
    branch (NO eval — same trust model as the data-only fast path). Fail safe (caller
    parks for Dan) on rejection, hard-stop, judge-unavailable, or conflict.
    Returns (approved, reason, merge_status)."""
    branch = entry.get("branch", "")
    if not branch:
        return False, "no branch on entry", "error"
    diff = subprocess.run(
        ["git", "-C", str(REPO), "diff", "--name-only", f"main...{branch}"],
        capture_output=True, text=True,
    )
    if diff.returncode != 0:
        return False, "diff failed", "error"
    out = [p for p in diff.stdout.splitlines()
           if p.strip() and not p.startswith(RESUMABLE_MERGE_PREFIXES)]
    if not out:
        return False, "no out-of-allowlist files (unexpected)", "error"

    # ── Deterministic hard-stop: these never reach Opus, always go to Dan ──
    proj  = _load_project_yaml()
    risky = proj.get("risky_set", [])
    bot   = proj.get("bot_files", ["main_bot.py"])
    blocked = [p for p in out
               if p in _RESUMABLE_HARD_STOP
               or any(r in p for r in risky)
               or any(b in p for b in bot)]
    if blocked:
        return False, f"hard-stop files touched: {blocked}", "code_change"

    # ── Opus judge on the out-of-allowlist diff (no eval runs on this path) ──
    diff_r = subprocess.run(
        ["git", "diff", f"main...{branch}", "--", *out],
        cwd=str(REPO), capture_output=True, text=True,
    )
    raw = _judge_complete(
        system=(
            "You are a conservative reviewer for an autonomous AI orchestration system. "
            "A multi-day data-generation sitting produced changes OUTSIDE its data/ + docs/ "
            "allowlist. This merge path runs NO eval and NO smoke test, so approve ONLY if the "
            "change is clearly safe to ship unverified — e.g. a small benign bugfix to the "
            "generation script, added logging, comments, or a checkpoint-path tweak. REJECT "
            "anything that affects the retrieval/eval pipeline (it must go through the eval "
            "gate), the orchestrator runtime, auth, secrets, or DB schema. "
            "Respond with JSON only: {\"pass\": true|false, \"reason\": \"...\"}"
        ),
        user=(
            f"Task: {task_id} (resumable data-gen)\nFiles outside data/+docs/: {out}\n\n"
            f"Diff:\n```\n{diff_r.stdout[:6000]}\n```\nOutput JSON only."
        ),
    )
    if not raw:
        return False, "Opus judge unavailable (CLI returned nothing)", "code_change"
    try:
        m   = re.search(r'\{.*\}', raw, re.DOTALL)
        obj = json.loads(m.group(0)) if m else {"pass": False, "reason": f"parse error: {raw[:80]}"}
    except Exception as exc:
        return False, f"judge parse error: {exc}", "code_change"
    if not bool(obj.get("pass", False)):
        return False, obj.get("reason", "rejected by Opus"), "code_change"

    # Approved → full merge (includes the out-of-allowlist change), no eval, like the fast path.
    wt_ok, wt_blocking = _clean_worktree_for_merge(
        [p for p in diff.stdout.split() if p.strip()])
    if not wt_ok:
        return False, f"dirty worktree blocks merge: {wt_blocking}", "conflict"
    merge = subprocess.run(
        ["git", "-C", str(REPO), "merge", "--no-ff", branch,
         "-m", f"merge(resume): Opus-approved partial progress from {branch}"],
        capture_output=True, text=True,
    )
    if merge.returncode != 0:
        subprocess.run(["git", "-C", str(REPO), "merge", "--abort"], capture_output=True)
        return False, "merge conflict after Opus approval", "conflict"
    subprocess.run(["git", "-C", str(REPO), "push", "origin", "main"],
                   cwd=str(REPO), capture_output=True)
    reason = obj.get("reason", "approved")
    notify_telegram(f"✓ {task_id} (resumable): Opus approved out-of-allowlist change — merged. {reason[:120]}")
    append_journal("resume_code_change_approved", f"{task_id} files={out} {reason[:160]}")
    return True, reason, "ok"
