"""`/redo`: the deliverable-rewrite allowlist, the runner, and the branch-landing loop.

Extracted verbatim from the reference orchestrator. Bodies are unchanged — only the
import block and the derivation of the module-level path globals differ. Behavioural
surprises are catalogued in `docs/found_bugs_inbox/selfheal.md` and pinned by
`tests/characterization/test_selfheal.py`; none of them is fixed here — including the
missing `files or []` guard that its self-fix twin has, the same `lstrip("./")`
character-set strip, and the silent success (no notification) on a landed rewrite.

The merge goes through `_merge_redo_branch` and the worktree/branch cleanup through this
module's `subprocess` reference; the characterisation tests swap both.

R9 (M4c batch 3) folds in the RUNNER side, ported verbatim from the reference's standalone
`scripts/maestro_redo.py` — the process `maestro.hitl.commands._handle_redo` used to spawn
in a dedicated tmux window. It resumes (or freshly seeds) an agent session to rewrite the
deliverable, gates it (path confinement + a notebook syntax check + >=1 commit), belt-and-
suspenders re-uploads to Drive, and drops the `.ready.json` this module's own
`apply_ready_redo` picks up. The only non-verbatim change is the `/tmp` scratch prefix,
which named the consuming project in the reference — neutralised the same way
`SELFFIX_WORKTREE_PREFIX` neutralised its sibling in `maestro/selfheal/selffix.py`.

Since 2026-08-26 the rewrite itself goes through `maestro.agentcall` rather than naming a
CLI, so `/redo` runs on whichever backend the project configured. It is one of the two
*agentic* one-shot calls (`writable=True`): unlike a judgment, it is expected to edit files
and commit inside its worktree, and a driver that can enforce that boundary is asked to.

The RUNNER's prompt (`_redo_rules`) used to be unconditionally notebook/Colab/Drive-shaped
— every `/redo`, on every project, was told to run `scripts/check_notebook.py`, re-upload
to a `gdrive:` remote, and confine itself to a hardcoded `colab/`/`data_export/` surface
that predates `maestro.confinement`. On a project that ships nothing of the kind (no
`scripts/check_notebook.py`, which the RUNNER can actually check for) that was just noise;
the confinement line was worse — it named a surface `project.yaml`'s `confinement.
redo_allow` had already superseded, so the prompt and the gate could disagree about what
the agent was allowed to touch. `_redo_rules` now reads the real configured surface and
only mentions the notebook workflow when `CHECK_NB` exists.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from maestro import agentcall
from maestro import confinement
from maestro.docs.roadmap import run_dep_map
from maestro.hitl.telegram import notify_telegram
from maestro.merge import _merge_redo_branch
from maestro.paths import Paths
from maestro.roles import ROLE_IMPLEMENTER
from maestro.state import append_journal

_PATHS = Paths.from_env()

REPO                  = _PATHS.repo
# `/redo`: maestro_redo.py drops a gate-passed branch here for the orchestrator (single
# git owner) to merge — the deliverable-rewrite analogue of SELFFIX_DIR.
REDO_DIR = REPO / ".orchestrator" / "redo"

# `/redo` confinement, re-checked at merge time. `_REDO_ALLOW` used to be
# `("colab/", "data_export/", "scripts/", "docs/", "tasks/")` and `_REDO_DENY` named the
# reference project's bot runtime; both now come from `project.yaml` via
# `maestro.confinement`, because a deliverable surface is the one thing maestro cannot
# guess about a project it has just been pointed at.
# Maestro sidecars now live outside the worktree; ignore any stray in-tree copy so a good
# rewrite is never discarded over a Maestro control file (mirrors maestro_redo.GATE_IGNORE).
_REDO_GATE_IGNORE = ("redo_result.json", "redo_impl.log")


def _redo_path_ok(files) -> tuple:
    """A `/redo` diff may touch only this project's configured deliverable surface.

    Maestro's own sidecars are filtered out first, unchanged: they are control files, and
    discarding a good rewrite over one was the bug `_REDO_GATE_IGNORE` exists to prevent.
    Everything after that is `maestro.confinement`'s judgement — including the
    `lstrip("./")` hole this function shared with its self-fix twin, where `../` was
    stripped rather than refused.
    """
    kept = [f for f in _strings_for_gate(files)
            if Path(f).name not in _REDO_GATE_IGNORE]
    if not kept:
        return False, "no files changed"
    return confinement.path_ok(kept, kind=confinement.REDO)


def _strings_for_gate(files) -> list:
    """`files` as non-empty strings. Kept separate so the sidecar filter above sees the
    same normalisation the gate itself applies."""
    return [str(f).strip() for f in (files or []) if str(f).strip()]


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


# ── RUNNER (R9): resume/seed the rewrite, gate it, hand a passed branch to the merge
# loop above. Ported verbatim from the reference's standalone scripts/maestro_redo.py. ──

# `REDO_MODEL` used to live here, pinning `claude-sonnet-4-6`. The rewrite is implementer
# work, so the model now comes from `roles.implementer`'s per-backend table.
#: Thirty minutes. A rewrite is real work, not a judgment, so it gets its own budget
#: rather than `agentcall.DEFAULT_TIMEOUT`.
REDO_TIMEOUT = 1800
LIMIT_HINT = ("hit your limit", "usage limit", "rate limit", "too many requests")
RESUME_MISS = "no conversation found with session id"

# `check_notebook.py` is a project-owned "how to ship" verification script, not one of the
# reference scripts deleted at M5 cutover — it survives, so this REPO/"scripts" constant is
# a deliberate exception (see the M4c batch-3 task prompt).
CHECK_NB = REPO / "scripts" / "check_notebook.py"

# The reference implementation hardcodes a /tmp scratch prefix that embeds the consuming
# project's name. Only that literal is neutralised here; the shape is identical (mirrors
# SELFFIX_WORKTREE_PREFIX in maestro/selfheal/selffix.py).
REDO_WORKTREE_PREFIX = "/tmp/maestro-redo-"


def sidecar_paths(redo_id: str) -> tuple:
    """(impl-log, result-manifest) absolute paths, kept outside the gated worktree."""
    return (Path(f"{REDO_WORKTREE_PREFIX}{redo_id}.impl.log"),
            Path(f"{REDO_WORKTREE_PREFIX}{redo_id}.result.json"))


def _run_git(args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(REPO), capture_output=True, text=True)


def _cleanup(wt: Path, branch: str) -> None:
    _run_git(["worktree", "remove", "--force", str(wt)])
    _run_git(["branch", "-D", branch])


def _redo_rules(task: str, result_path: Path, wt: Path) -> str:
    """The RULES block of the RUNNER's prompt, sized to what this project actually ships.

    The confined surface is read from `confinement.allow_prefixes` — the same source
    `_redo_path_ok` gates against below — rather than restated as a hardcoded list that
    could silently drift from it. The notebook/Drive-specific rules (and the matching
    manifest fields) only appear when `CHECK_NB` exists: that file is this project's own
    signal that its deliverable is a notebook maestro syntax-gates and re-uploads. A
    project with no such script gets a generic validate-and-ship instruction instead of a
    workflow it has no infrastructure for.
    """
    allow = confinement.allow_prefixes(confinement.REDO)
    allow_str = ", ".join(allow) if allow else (
        "(none — confinement.redo_allow is empty in project.yaml)"
    )
    manifest_fields = "changelog (1-3 lines on what you fixed)"

    rules = [
        "- Rewrite the deliverable PROPERLY. You are NOT limited to minimal diffs — if "
        "the right fix is a large rewrite, do it. Optimize for correct, clean code.",
    ]
    if CHECK_NB.is_file():
        rules.append(
            "- VALIDATE: run `python scripts/check_notebook.py <notebook.ipynb>` and FIX "
            "until it exits 0 (it byte-compiles every code cell). Loop: edit → check → "
            "repeat."
        )
        rules.append(
            "- RE-UPLOAD the fixed notebook to the SAME `gdrive:` path you used "
            "originally so Dan's existing Colab link keeps working (the `gdrive:` "
            "rclone remote works headlessly)."
        )
        manifest_fields = (
            f"notebook_path (repo-relative, e.g. colab/{task.lower()}_train.ipynb), "
            "gdrive_dest (the full rclone dest you uploaded to), colab_link (the Colab "
            "URL — same as before if unchanged), and " + manifest_fields
        )
    else:
        rules.append(
            "- VALIDATE your fix using this project's normal verification process "
            "(its tests, its adapters/, or docs/PROJECT.md's 'What good looks like "
            "here') before committing."
        )
    rules += [
        f"- COMMIT in this worktree: git add -A && git commit -m 'redo({task}): "
        "<summary>'.",
        f"- Touch ONLY files under: {allow_str}. Never touch this project's secrets "
        "or production data stores (project.yaml), or anything under .git/ or "
        ".orchestrator/.",
        "- This is a CONFINED reship, NOT a normal dev session. Do NOT log a lesson to "
        "tasks/lessons.md and do NOT make any ancillary edits outside the deliverable "
        "surface, even if a CLAUDE.md / AGENTS.md convention tells you to after a "
        "'correction'. That convention does NOT apply here — keep the diff to the fix "
        "itself.",
        "- Do NOT push, do NOT merge, do NOT restart anything.",
        f"- FINALLY, write the result manifest to {result_path} (an absolute path "
        f"OUTSIDE this worktree — do NOT create it inside {wt}) with keys: "
        f"{manifest_fields}.",
    ]
    return "\n".join(rules)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("request")
    req = json.loads(Path(ap.parse_args().request).read_text(encoding="utf-8"))

    redo_id = req["redo_id"]
    task = req.get("task", "?")
    uuid = req.get("uuid", "") or ""
    dan_id = str(req.get("dan_id", "") or "")
    message = req.get("message", "").strip()
    action = req.get("action", "") or "(action text unavailable)"
    branch = f"redo-{redo_id}"
    # Resume is keyed to the original build cwd — use that exact path so the session
    # is found; fall back to a redo-specific path if none was recorded.
    wt = Path(req.get("worktree") or f"{REDO_WORKTREE_PREFIX}{redo_id}")
    # Maestro's own artifacts go here — OUTSIDE the worktree, so `git add -A` can't commit
    # them into the gated diff (the bug that discarded every redo over its own files).
    log_path, result_path = sidecar_paths(redo_id)

    # Fresh worktree at the build path, checked out from main (deliverable present).
    _run_git(["worktree", "prune"])
    if wt.exists():
        _run_git(["worktree", "remove", "--force", str(wt)])
    _run_git(["branch", "-D", branch])
    r = _run_git(["worktree", "add", "-b", branch, str(wt), "main"])
    if r.returncode != 0:
        notify_telegram(f"⚠ Maestro /redo {task}: worktree add failed: {(r.stderr or '')[:160]}")
        return 1

    framed = (
        f"You are Maestro's implementer that prepared task {task}. Dan ran the manual "
        f"action you gave him and hit a problem — your job now is to FIX the deliverable "
        f"and reship it, not just advise.\n\n"
        f"WORKTREE (your git sandbox — work ONLY here): {wt}\n\n"
        f"MANUAL ACTION HE WAS GIVEN:\n{action}\n\n"
        f"DAN'S PROBLEM REPORT:\n{message}\n\n"
        f"RULES:\n" + _redo_rules(task, result_path, wt)
    )

    def run(resume_id: str, prompt: str) -> str:
        """One agentic rewrite pass. Returns the run's whole transcript.

        The transcript, not the answer: the caller scans it for the resume-miss notice and
        for usage-limit wording, both of which only appear on a run that did not simply
        succeed. The driver writes stdout+stderr to `log_path` — a sidecar deliberately
        outside the worktree, so `git add -A` cannot commit it into the gated diff — and
        that file is the transcript.
        """
        answer = agentcall.ask(
            ROLE_IMPLEMENTER, prompt,
            timeout=REDO_TIMEOUT, cwd=wt, resume_id=resume_id,
            log_file=log_path, writable=True,
        )
        if answer.timed_out:
            return "(timed out after 30 min)"
        try:
            return log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"(error: {exc})"

    brief_path = req.get("brief", "")
    brief_ctx = ""
    if brief_path and Path(brief_path).exists():
        try:
            brief_ctx = Path(brief_path).read_text(encoding="utf-8")[:6000]
        except Exception:
            brief_ctx = ""

    if uuid:
        out = run(uuid, framed)
        if not out or RESUME_MISS in out.lower():
            print(f"[redo] resume of {uuid} unusable — falling back to brief.", file=sys.stderr)
            seeded = (f"CONTEXT — the task brief you worked from:\n{brief_ctx}\n\n" + framed
                      if brief_ctx else framed)
            out = run("", seeded)
    else:
        seeded = (f"CONTEXT — the task brief you worked from:\n{brief_ctx}\n\n" + framed
                  if brief_ctx else framed)
        out = run("", seeded)

    if any(h in out.lower() for h in LIMIT_HINT) and "commit" not in out.lower():
        notify_telegram(f"⏳ Maestro /redo {task} aborted — Claude usage limit. Try /redo {dan_id or task} "
               f"again after it resets.")
        _cleanup(wt, branch)
        return 0

    # ── Gates (authoritative, here) ──
    # Gate 1: >=1 commit.
    commits = _run_git(["log", "--oneline", f"main..{branch}"]).stdout.strip()
    if not commits:
        notify_telegram(f"🔧 Maestro /redo {task}: produced no commit — nothing reshipped. "
               f"Re-run /ask {dan_id or task} for advice, or /redo with more detail.")
        _cleanup(wt, branch)
        return 0

    changed = _run_git(["diff", "--name-only", f"main...{branch}"]).stdout.split()
    ok, why = _redo_path_ok(changed)
    if not ok:
        notify_telegram(f"🛑 Maestro /redo {task} REJECTED by path gate ({why}). Discarded — no changes shipped.")
        _cleanup(wt, branch)
        return 0

    # Read the agent's result manifest (notebook + Drive dest + link).
    result = {}
    rp = result_path
    if rp.exists():
        try:
            result = json.loads(rp.read_text(encoding="utf-8"))
        except Exception:
            result = {}
    nb_rel = (result.get("notebook_path") or "").strip()
    # Fall back: the single changed .ipynb, if the manifest omitted it.
    if not nb_rel:
        nbs = [c for c in changed if c.endswith(".ipynb")]
        nb_rel = nbs[0] if len(nbs) == 1 else ""

    # Gate 2: the shipped notebook is syntax-clean (authoritative — don't trust the loop).
    if nb_rel:
        nb_abs = wt / nb_rel
        cr = subprocess.run([sys.executable, str(CHECK_NB), str(nb_abs)],
                            capture_output=True, text=True)
        if cr.returncode != 0:
            notify_telegram(f"🛑 Maestro /redo {task}: rewritten notebook still has syntax errors — "
                   f"NOT shipped.\n{(cr.stderr or cr.stdout)[:400]}")
            _cleanup(wt, branch)
            return 0

    # Belt-and-suspenders re-upload: guarantee Drive matches the gated notebook even if the
    # agent uploaded an earlier draft mid-loop. Idempotent (overwrites the same file → same ID).
    gdrive_dest = (result.get("gdrive_dest") or "").strip()
    if nb_rel and gdrive_dest:
        nb_abs = wt / nb_rel
        rclone_cmd = (["rclone", "copy", str(nb_abs), gdrive_dest, "-q"]
                      if gdrive_dest.endswith("/")
                      else ["rclone", "copyto", str(nb_abs), gdrive_dest, "-q"])
        ur = subprocess.run(rclone_cmd, capture_output=True, text=True)
        if ur.returncode != 0:
            notify_telegram(f"⚠ Maestro /redo {task}: notebook is fixed + committed, but the Drive "
                   f"re-upload failed ({(ur.stderr or '')[:160]}). It will still merge; re-upload "
                   f"may need a manual `rclone copyto`.")

    # Gate passed — hand the branch to the orchestrator to merge (single git owner).
    REDO_DIR.mkdir(parents=True, exist_ok=True)
    changelog = (result.get("changelog") or commits.splitlines()[0]).strip()
    colab_link = (result.get("colab_link") or "").strip()
    (REDO_DIR / f"{redo_id}.ready.json").write_text(json.dumps({
        "branch": branch, "task": task, "dan_id": dan_id, "worktree": str(wt),
        "changed": changed, "changelog": changelog, "colab_link": colab_link,
    }), encoding="utf-8")

    link_line = f"\n\n🔗 {colab_link}" if colab_link else ""
    notify_telegram(
        f"🔧 *Maestro* reworked `{task}` and reshipped it (syntax-gated, re-uploaded):\n\n"
        f"{changelog[:500]}{link_line}\n\n"
        f"The orchestrator will merge the change. Re-run the Colab, then /approve {dan_id or task} "
        f"when it works — or /redo {dan_id or task} <message> again if it still breaks."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
