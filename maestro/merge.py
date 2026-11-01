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
from pathlib import Path

from maestro.config import _load_project_yaml
from maestro import confinement
from maestro import metabranch
from maestro import metrics
from maestro.hitl.telegram import notify_telegram
from maestro.paths import Paths
from maestro.pending import deferred
from maestro.roles import ROLE_JUDGE
from maestro.state import append_journal

_PATHS = Paths.from_env()

REPO                  = _PATHS.repo
VENV_PYTHON           = REPO / ".venv" / "bin" / "python3"
# `canary_deploy.py` is correctly project-owned (docs/plans/2026-08-18-m4c-superseded-
# sidecars.md §2b: "how to ship", not one of the M5-superseded scripts maestro extracts —
# see the documented exception in tests/test_no_reference_sidecars.py). Nothing scaffolds
# one, though, so `run_canary_deploy` below checks for it rather than assuming it exists.
CANARY_DEPLOY         = REPO / "scripts" / "canary_deploy.py"

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

# The `sudo` pattern below used to also exempt `restart_bot.sh` by name — a reference
# project's own script, so the carve-out could only ever fire on that one repo. The
# `systemctl` exemption is generic (any project's service manager) and stays.
_HARD_DENY_PATTERNS = [
    r"\bsudo\b(?!.*systemctl\b)",
    r"git\s+push\s+.*--force",
    r"git\s+push\s+-f\b",
    r"DROP\s+TABLE",
    r"DELETE\s+FROM",
    r"--force-reset-index",
    r"TELEGRAM_BOT_TOKEN\s*=",
    r"open\s*\(\s*['\"]\.env['\"]",  # direct open(".env") — path-building is fine
]

#: The command-shaped patterns: they guard what would *run*. Dan 2026-10-08 (itv pilot,
#: `sudo` in a report's prose asked him three times): they do not apply to prose files,
#: which nothing executes. The secret patterns above and every `deny_list_extra` entry
#: still apply to every file.
_COMMAND_PATTERNS = frozenset({
    r"\bsudo\b(?!.*systemctl\b)", r"git\s+push\s+.*--force", r"git\s+push\s+-f\b",
    r"DROP\s+TABLE", r"DELETE\s+FROM", r"--force-reset-index",
})
PROSE_SUFFIXES = (".md", ".markdown", ".rst", ".txt")


def _is_prose(path: str) -> bool:
    return path.lower().endswith(PROSE_SUFFIXES)


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


# ── the graph's merge guard (P12 finding D10) ──
#
# The graph's merge node lands through `integrate_candidate`, which skipped both of the
# legacy loop's pre-merge checks. The hard deny-list is restored here, unchanged in its
# patterns, and made to diff against the base that actually matters: the merge base of the
# frozen candidate commit and the target head, not a literal `main...<branch>`. The legacy
# Sonnet risky reviewer is NOT ported: a risky-set edit is gated by the proof review, run
# by a model stronger than the implementer (`WorkflowRunner._review_strength_floor`) and
# told which risky files the diff touched (`worker.agent_brief`). What remains here is the
# floor under that: a risky diff with no approving proof review never lands.


def path_matches_entry(path: str, entry: str) -> bool:
    """`risky_set` / `secrets` semantics: a glob when it has metacharacters, else a substring."""
    return confinement._denied_by(path, entry)


def risky_files(changed, risky_set=(), risky_globs=()) -> list[str]:
    """The changed paths that fall under `risky_set` (substring or glob) or a policy glob."""
    from fnmatch import fnmatch

    entries = [str(e) for e in (risky_set or ()) if str(e).strip()]
    globs = [str(g) for g in (risky_globs or ()) if str(g).strip()]
    return [
        path for path in changed
        if any(path_matches_entry(path, entry) for entry in entries)
        or any(fnmatch(path, glob) for glob in globs)
    ]


def _added_lines(diff_text: str) -> str:
    """Only what the diff adds. Removing a deny-listed line is not a violation."""
    return "\n".join(
        line[1:] for line in diff_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


def deny_list_violation(added_text: str, changed, proj=None) -> str:
    """Why this change may not land, or `""`. The hard deny-list, in the graph's terms.

    The same `_HARD_DENY_PATTERNS` plus `deny_list_extra` as `deny_list_guard`, matched
    against the lines the change *adds*; then `secrets` and `prod_stores` among the changed
    paths, matched the way `confinement` matches them (a glob is a glob — the legacy guard
    reduced `**/*.pem` to a substring that no filename contains).
    """
    proj = _load_project_yaml() if proj is None else proj
    proj = proj if isinstance(proj, dict) else {}
    patterns = _HARD_DENY_PATTERNS + [str(p) for p in (proj.get("deny_list_extra") or [])]
    for pat in patterns:
        try:
            hit = re.search(pat, added_text, re.MULTILINE)
        except re.error as exc:
            return f"deny-list pattern {pat!r} is not a valid regex ({exc}); refusing to merge"
        if hit:
            return f"deny-list violation: pattern '{pat}' matched in diff: {hit.group(0)[:80]!r}"
    secrets = proj.get("secrets", [".env", ".env.local"]) or []
    guarded = [str(s) for s in list(secrets) + list(proj.get("prod_stores") or []) if str(s).strip()]
    for path in changed:
        for entry in guarded:
            if path_matches_entry(path, entry):
                return f"deny-list violation: secret or production file in diff: {path}"
    return ""


def deny_hit_key(file: str, pattern: str, line: str) -> str:
    """What Dan's ruling on a deny-list hit is bound to (P12 pilot finding D29).

    The file, the pattern and the added line(s) the match spans (whitespace collapsed).
    The same line in the same file keeps its key across a rebase or an unrelated edit; a
    changed line, a moved file or another pattern is a new hit, which asks Dan again.
    """
    import hashlib

    text = re.sub(r"\s+", " ", str(line)).strip()
    body = f"{file}\0{pattern}\0{text}"
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _added_by_file(diff_text: str) -> dict[str, list[str]]:
    """The lines a unified diff adds, per file (the `+++ b/<path>` it follows)."""
    out: dict[str, list[str]] = {}
    current, header = "", False
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            header = True
            continue
        if header:
            if line.startswith("+++ "):
                target = line[4:]
                current = target[2:] if target.startswith("b/") else target
            elif line.startswith("@@"):
                header = False
            continue
        if line.startswith("+"):
            out.setdefault(current, []).append(line[1:])
    return out


def deny_list_hits(diff_text: str, changed, proj=None) -> tuple[list[dict], str]:
    """`(hits, refusal)`: every place `diff_text` breaks the hard deny-list (D29).

    The same patterns, secrets and production stores as `deny_list_violation`, reported
    one hit per distinct match so Dan can rule on each: `{key, kind, file, pattern,
    match, line}`, `kind` being `pattern` (added text) or `path` (a guarded file). A
    pattern is matched against each file's added lines joined, and then, as before,
    against all added lines at once: a match only that finds is reported under the file
    `(across files)`, so splitting by file never lets a match through. `refusal` is a
    fault no ruling can clear (a pattern that is not a valid regex).
    """
    proj = _load_project_yaml() if proj is None else proj
    proj = proj if isinstance(proj, dict) else {}
    patterns = _HARD_DENY_PATTERNS + [str(p) for p in (proj.get("deny_list_extra") or [])]
    by_file = _added_by_file(diff_text)
    hits: list[dict] = []
    for pat in patterns:
        try:
            compiled = re.compile(pat, re.MULTILINE)
        except re.error as exc:
            return [], f"deny-list pattern {pat!r} is not a valid regex ({exc}); refusing to merge"
        seen: set[str] = set()
        command = pat in _COMMAND_PATTERNS
        for path, lines in by_file.items():
            if command and _is_prose(path):
                continue
            text = "\n".join(lines)
            for m in compiled.finditer(text):
                start = text.rfind("\n", 0, m.start()) + 1
                end = text.find("\n", m.end())
                line = text[start: end if end != -1 else len(text)]
                # Bound to the whole added line, not the match: approving one DELETE FROM
                # must not approve a different one in the same file.
                key = deny_hit_key(path, pat, line)
                if key in seen:
                    continue
                seen.add(key)
                hits.append({
                    "key": key, "kind": "pattern", "file": path, "pattern": pat,
                    "match": m.group(0)[:200], "line": line[:300],
                })
        if not seen:
            scope = (
                "\n".join(l for path, ls in by_file.items() if not _is_prose(path) for l in ls)
                if command else _added_lines(diff_text)
            )
            whole = compiled.search(scope)
            if whole:
                hits.append({
                    "key": deny_hit_key("(across files)", pat, whole.group(0)),
                    "kind": "pattern", "file": "(across files)", "pattern": pat,
                    "match": whole.group(0)[:200], "line": whole.group(0)[:300],
                })
    secrets = proj.get("secrets", [".env", ".env.local"]) or []
    guarded = [str(s) for s in list(secrets) + list(proj.get("prod_stores") or []) if str(s).strip()]
    for path in changed:
        for entry in guarded:
            if path_matches_entry(path, entry):
                hits.append({
                    "key": deny_hit_key(path, entry, path), "kind": "path", "file": path,
                    "pattern": entry, "match": path, "line": path,
                })
                break
    return hits, ""


def hit_violation(hit: dict) -> str:
    """`deny_list_violation`'s wording for one hit."""
    if hit.get("kind") == "path":
        return f"deny-list violation: secret or production file in diff: {hit['file']}"
    return (f"deny-list violation: pattern '{hit['pattern']}' matched in diff: "
            f"{str(hit['match'])[:80]!r}")


def _approves(disposition, commit: str) -> bool:
    """A conclusive approval of `commit` (or of an unnamed subject) by a proof reviewer."""
    from maestro.validation import review_is_conclusive

    if not isinstance(disposition, dict) or not review_is_conclusive(disposition)[0]:
        return False
    subject = str(disposition.get("subject_sha") or "")
    return not subject or subject == commit


def integration_guard(
    repo: Path, commit: str, target_head: str, *, proof_reviews=(), risky_globs=(), proj=None,
    approved_hits=(),
) -> str:
    """What stops `commit` landing on `target_head`, or `""` (checked before staging).

    Fails closed: a diff that cannot be computed is a refusal, never a pass.
    `approved_hits` are the keys (`deny_hit_key`) of deny-list hits Dan approved for this
    task's workflow (D29); any other hit, including a changed one, still refuses.
    """
    proj = _load_project_yaml() if proj is None else proj
    proj = proj if isinstance(proj, dict) else {}
    if not target_head:
        return "deny_list: the target branch has no head to diff the candidate against; refusing"
    span = f"{target_head}...{commit}"
    text = subprocess.run(
        ["git", "diff", "--no-color", "--no-ext-diff", span],
        cwd=str(repo), capture_output=True, text=True,
    )
    names = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", "-z", span],
        cwd=str(repo), capture_output=True, text=True,
    )
    if text.returncode != 0 or names.returncode != 0:
        err = (text.stderr or names.stderr or "").strip()[:200]
        return f"deny_list: could not diff the candidate against the target ({err}); refusing"
    changed = [p for p in names.stdout.split("\0") if p]
    hits, refusal = deny_list_hits(text.stdout, changed, proj)
    if refusal:
        return f"deny_list: {refusal}"
    approved = {str(k) for k in (approved_hits or ())}
    unapproved = [hit for hit in hits if hit["key"] not in approved]
    if unapproved:
        return f"deny_list: {hit_violation(unapproved[0])}"
    risky = risky_files(changed, proj.get("risky_set") or [], risky_globs)
    if risky and not any(_approves(d, commit) for d in (proof_reviews or ())):
        return (
            f"risky_unreviewed: the change touches risky file(s) {risky} and no proof "
            f"review approved commit {commit[:12]}; a risky edit lands only after a proof "
            "review by a stronger model approves that exact commit, and this workflow has "
            "no such approval (a script or resumable lane composes no reviewer, a policy "
            "may skip one), so nothing may land"
        )
    return ""


# ── B6: Sonnet risky-set reviewer ──

from maestro.model_policy import with_task_models


@with_task_models
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
        # Asked of the judge role, not of a named model. This gate runs per risky merge,
        # so it deliberately sits on the review-tier role rather than the diagnoser —
        # which keeps strong-model budget for proposal-shaping, the same reasoning that
        # used to be spelled as a pinned `model="claude-sonnet-5"`. That pin named a
        # model only one backend has; the role's per-backend table names the right one
        # wherever the call actually lands.
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
            role=ROLE_JUDGE,
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
    """Return True if branch diff includes any bot_files from project.yaml.

    The inline default is `[]`, not a guess at a filename: `bot_files` names a live
    process's source (canary-deployed on a merge, hard-stopped on the resumable path
    below), and a project maestro has just been pointed at may not run a bot at all.
    Guessing `main_bot.py` meant a project that never declared this key got it treated as
    though it had — the same silently-widened-default class of bug `confinement.py`
    documents for the self-fix/redo path gates.
    """
    proj = _load_project_yaml()
    bot_files = proj.get("bot_files", [])
    changed = _diff_files(branch)
    return any(any(bf in f for bf in bot_files) for f in changed)


def run_canary_deploy(task_id: str) -> bool:
    """Canary-deploy `task_id`'s just-merged change, for a project that has bot_files.

    `absent is a skip, not a failure` — the same fix `gates._run_smoke` got this session
    for the identical bug: nothing scaffolds `scripts/canary_deploy.py` (it is correctly
    project-owned, not maestro's to template), so calling it unconditionally meant python
    exited non-zero on any project that had not written one, which both call sites
    (`orchestrator.py`'s completion handler and `hitl/commands.py`'s `/approve`) read as a
    *failed* deploy — skipping the phase report and journaling `canary_reverted` for a
    deploy that was never attempted, on a merge that had already landed. `bot_files`
    defaulting to `[]` (see `_touches_bot_files`) kept this invisible until a project
    actually declares one.
    """
    if not confinement.available(CANARY_DEPLOY):
        return True
    return subprocess.run([str(VENV_PYTHON), str(CANARY_DEPLOY), task_id],
                          cwd=str(REPO), capture_output=True).returncode == 0


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

@with_task_models
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
        # `noop` distinguishes "produced nothing" from "produced a regression". The caller
        # must not treat them alike: a regression is worth escalating and possibly keeping,
        # while re-running work that produced no commits reproduces the same nothing. Flagged
        # rather than left to a `reason` substring match so the wording stays free to change.
        return False, {"pass": False, "metrics": {}, "reason": reason, "noop": True}

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
    shown = metrics.summary(smoke)
    if metrics.metrics_of(smoke):
        print(f"  [smoke] {task_id}: pass={smoke['pass']} {shown}")
    else:
        print(f"  [smoke] {task_id}: pass={smoke['pass']} — {smoke.get('reason','')[:100]}")
    if smoke["pass"]:
        append_journal("merge_accepted", f"{task_id} sha={sha_short} {shown}",
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
                   f"{task_id} sha={sha_short} {shown} reason={smoke.get('reason','')}",
                   session_id=entry["session_id"])
    return False, smoke


# ── Staged integration (`[P4]`) ──

def integrate_candidate(
    task_id: str, branch: str, *, attempt_id: str, run_id: str = "", store=None,
    lifecycle=None, repo=None, target_branch: str = "main", specs=None,
    candidate_worktree=None, effects=(), keep_staging: bool = False,
    review_dispositions=(), proof_reviews=(), risky_globs=(), approved_deny_hits=(),
):
    """Land `branch` through the 5-stage staging pipeline instead of onto the live tree.

    `merge_and_eval` above merges the candidate into REPO's own working tree and then
    runs the smoke there, which means a failing smoke has to be *undone* — the
    `git reset --hard sha_pre` path. Undoing is the part that goes wrong: the reset has
    to be right about what to reset to (hence `sha_pre` rather than `HEAD~1`), the live
    tree has to be clean enough to merge into in the first place (hence
    `_clean_worktree_for_merge`), and for the duration of the smoke the target branch is
    carrying unverified code that another process could read.

    The staged path removes the need for all three: the merge happens in
    `.orchestrator/worktrees/staging/` on a detached head, the checks run there, and the
    target branch is written exactly once, by a compare-and-swap, only after they pass
    (`§3`, `[INV-05]`). A failure has nothing to revert because nothing was written.

    `merge_and_eval` is left exactly as it is. `D08` migrates one authority at a time and
    the legacy loop still calls it; this is the seam the graph runner's `merge` node uses.

    Before anything is staged, `integration_guard` checks the frozen commit against the
    target head (P12 finding D10): the hard deny-list, secret files, and a risky-set edit
    no proof review approved. Any of them stops the merge at stage `guard`, target untouched.
    """
    from maestro.validation import StagingPipeline

    root = Path(repo) if repo is not None else REPO

    def guard(commit: str, target_head: str) -> str:
        return integration_guard(
            root, commit, target_head, proof_reviews=proof_reviews, risky_globs=risky_globs,
            approved_hits=approved_deny_hits,
        )

    pipeline = StagingPipeline(
        root,
        target_branch=target_branch,
        store=store,
        lifecycle=lifecycle,
    )
    return pipeline.integrate(
        candidate_ref=branch,
        specs=list(specs or []),
        attempt_id=attempt_id,
        task_id=task_id,
        run_id=run_id or attempt_id,
        candidate_worktree=candidate_worktree,
        effects=effects,
        keep_staging=keep_staging,
        review_dispositions=list(review_dispositions or ()),
        guard=guard,
    )


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

# Left as a constant rather than a project.yaml knob: unlike `bot_files` above, nothing
# here names the reference project — "data/" and "docs/" are the two categories any
# project's build can usually change without an eval (data artifacts; documentation).
RESUMABLE_MERGE_PREFIXES  = ("data/", "docs/")  # paths a resumable sitting may auto-merge, no eval
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
    push = subprocess.run(["git", "-C", str(REPO), "push", "origin", "main"],
                          cwd=str(REPO), capture_output=True, text=True)
    if push.returncode != 0:
        append_journal("merge_push_failed",
                        f"main push failed after data-only merge from {branch}: "
                        f"{push.stderr.strip()[:300]}")
        return "push_failed"
    metabranch.reconcile_meta(REPO)
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
    bot   = proj.get("bot_files", [])
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
    reason = obj.get("reason", "approved")
    push = subprocess.run(["git", "-C", str(REPO), "push", "origin", "main"],
                          cwd=str(REPO), capture_output=True, text=True)
    if push.returncode != 0:
        append_journal("merge_push_failed",
                        f"{task_id} main push failed after Opus-approved merge from {branch}: "
                        f"{push.stderr.strip()[:300]}")
        notify_telegram(
            f"⚠ {task_id} (resumable): Opus approved out-of-allowlist change and merged "
            f"locally, but push to origin/main FAILED — local main is ahead of remote. "
            f"{push.stderr.strip()[:160]}"
        )
        return True, reason, "push_failed"
    metabranch.reconcile_meta(REPO)
    notify_telegram(f"✓ {task_id} (resumable): Opus approved out-of-allowlist change — merged. {reason[:120]}")
    append_journal("resume_code_change_approved", f"{task_id} files={out} {reason[:160]}")
    return True, reason, "ok"
