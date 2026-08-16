"""The gates a finished task must clear: verification, proof review and smoke.

Extracted verbatim from the reference orchestrator. Bodies are unchanged — only the
import block and the derivation of the module-level path globals differ. Behavioural
surprises are catalogued in `docs/FOUND_BUGS.md` and pinned by
`tests/characterization/test_gates.py`; none of them is fixed here.

The agent-CLI invocation in `sonnet_review_proofs` is a plain `subprocess.run` copied
as-is; the characterisation tests swap this module's `subprocess` reference rather than
letting anything reach a real CLI or the network.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from maestro.paths import Paths
from maestro.pending import deferred
from maestro.state import append_journal

_PATHS = Paths.from_env()

REPO                  = _PATHS.repo
SMOKE_ADAPTER         = REPO / "adapters" / "smoke.py"
VENV_PYTHON           = REPO / ".venv" / "bin" / "python3"
CHECK_VERIFICATIONS   = REPO / "scripts" / "check_verifications.py"

# Async-verification park (Handoff B): a dispatch:manual task whose verification only
# *kicks off* a long async job (e.g. a ~5 h eval) is parked as `awaiting-verification`
# and re-checked each poll instead of failing /approve. Bound the wait so a row that
# never lands surfaces to Dan rather than parking forever. P14/P10 evals run ~5 h; 8 h
# leaves margin for queue + reindex.
AWAIT_VERIFY_TIMEOUT_SEC = 8 * 3600
SMOKE_TIMEOUT = 2700

# Owned by `maestro.docs.roadmap`. Late-bound rather than imported: this module sits
# upstream of the HITL layer that `maestro.docs.roadmap` pulls in, and binding it here
# resolves the name on first call without adding a module-import edge that would invert
# the M1 extraction order. Still an ordinary module attribute, so tests monkeypatch it
# exactly as before.
get_task_by_id = deferred("get_task_by_id", "maestro.docs.roadmap")


# ── Verification gate ──

def run_verification_gate(task_id: str, workspace: Path, worktree: Path | None = None,
                          auto_only: bool = False) -> tuple[bool, str]:
    """Run check_verifications.py. Auto-checks run in the worktree (unmerged changes
    live there, not in REPO). auto_only restricts to kind:auto items (used by
    dispatch:manual graduation, where there is no implementer result.json). Returns
    (passed, message)."""
    if not CHECK_VERIFICATIONS.exists():
        return True, "(check_verifications.py not found — gate skipped)"
    try:
        argv = [str(VENV_PYTHON), str(CHECK_VERIFICATIONS), task_id, str(workspace)]
        if worktree is not None:
            argv.append(str(worktree))
        if auto_only:
            argv.append("--auto-only")
        r = subprocess.run(
            argv,
            cwd=str(REPO), capture_output=True, text=True, timeout=120
        )
        output = (r.stdout + r.stderr).strip()
        return r.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "check_verifications.py timed out (120 s)"
    except Exception as exc:
        return False, f"check_verifications.py error: {exc}"


def sonnet_review_proofs(task_id: str, verifications: list[dict],
                         impl_verifs: dict) -> tuple[bool, str]:
    """Use Sonnet to review manual verification proofs. Returns (all_pass, feedback)."""
    manual_items = [(v["id"], v) for v in verifications if v.get("kind") == "manual"]
    if not manual_items:
        return True, ""

    # B8: deterministic pre-filter — reject obviously-vague proofs without an LLM call.
    _FILLER_PHRASES = {
        "tested manually", "works", "looks good", "done", "verified",
        "lgtm", "it works", "passed", "ok", "fine",
    }
    _MIN_PROOF_CHARS = 12  # non-whitespace character floor
    pre_failures: list[str] = []
    for vid, _v in manual_items:
        proof = impl_verifs.get(vid, {}).get("proof", "")
        stripped = proof.strip()
        nonws_len = len(stripped.replace(" ", "").replace("\t", "").replace("\n", ""))
        proof_lower = stripped.lower()
        # Match only when the WHOLE proof is filler (modulo trailing punctuation),
        # not as a substring — else "...the token flow works" would false-positive.
        is_filler = proof_lower.rstrip(".!… ").strip() in _FILLER_PHRASES
        if nonws_len < _MIN_PROOF_CHARS or is_filler:
            pre_failures.append(
                f"{vid}: proof too vague (deterministic pre-filter) — "
                f"got {nonws_len!r} non-ws chars, text={stripped[:60]!r}"
            )
    if pre_failures:
        return False, "Sonnet proof review failed:\n" + "\n".join(
            f"  ✗ {f}" for f in pre_failures
        )

    review_input = {
        vid: {"check": v.get("check", ""), "proof": impl_verifs.get(vid, {}).get("proof", "")}
        for vid, v in manual_items
    }
    prompt = (
        f"You are reviewing implementer verification proofs for task {task_id}.\n"
        f"For each verification ID, decide if the proof is convincing and specific.\n"
        f"A weak or vague proof (e.g., 'tested manually', 'looks good') must FAIL.\n"
        f"Reply with JSON only, no other text: "
        f'{{\"V1\": {{\"pass\": true, \"reason\": \"...\"}}, ...}}\n\n'
        f"Verifications:\n{json.dumps(review_input, indent=2, ensure_ascii=False)}"
    )
    try:
        r = subprocess.run(
            ["claude", "-p", "--model", "claude-sonnet-5", prompt],
            capture_output=True, text=True, timeout=120, cwd=str(REPO)
        )
        if r.returncode != 0:
            return False, f"Sonnet review failed (exit {r.returncode}): {r.stderr[:200]}"
        m = re.search(r"\{.*\}", r.stdout.strip(), re.DOTALL)
        if not m:
            return False, f"Sonnet returned non-JSON: {r.stdout[:200]}"
        review   = json.loads(m.group())
        failures = [f"{vid}: {res.get('reason','')}"
                    for vid, res in review.items() if not res.get("pass")]
        if failures:
            return False, "Sonnet proof review failed:\n" + "\n".join(f"  ✗ {f}" for f in failures)
        return True, ""
    except json.JSONDecodeError as exc:
        return False, f"Sonnet returned invalid JSON: {exc}"
    except subprocess.TimeoutExpired:
        return False, "Sonnet review timed out (120 s)"
    except Exception as exc:
        return False, f"Sonnet review error: {exc}"


# ── Smoke eval ──

def _run_smoke(task_id: str) -> dict:
    smoke_input = json.dumps({"worktree": str(REPO), "task_id": task_id, "n": 20})
    try:
        sp = subprocess.run([str(VENV_PYTHON), str(SMOKE_ADAPTER)],
                            input=smoke_input, capture_output=True, text=True,
                            timeout=SMOKE_TIMEOUT)
        if sp.returncode == 0 and sp.stdout.strip():
            return json.loads(sp.stdout.strip())
        err = (sp.stderr or sp.stdout or "no output")[:300]
        return {"pass": False, "metrics": {}, "reason": f"smoke.py exit={sp.returncode}: {err}"}
    except subprocess.TimeoutExpired:
        return {"pass": False, "metrics": {}, "reason": "smoke timed out (45 min)"}
    except Exception as exc:
        # Never let a smoke infra error (e.g. malformed JSON) raise out of
        # merge_and_eval — an uncaught throw here gets swallowed by the
        # poll_control_commands blanket except, leaving a half-finalized merge.
        return {"pass": False, "metrics": {}, "reason": f"smoke error: {exc}"}


def _run_custom_smoke(task_id: str, cmd: str) -> dict:
    """Run a task-declared `smoke:` shell command in REPO. Passes iff rc==0."""
    try:
        sp = subprocess.run(cmd, shell=True, cwd=str(REPO),
                            capture_output=True, text=True, timeout=SMOKE_TIMEOUT)
        if sp.returncode == 0:
            return {"pass": True, "metrics": {}, "reason": f"custom smoke ok: {cmd[:80]}"}
        err = (sp.stderr or sp.stdout or "no output")[:300]
        return {"pass": False, "metrics": {}, "reason": f"custom smoke exit={sp.returncode}: {err}"}
    except subprocess.TimeoutExpired:
        return {"pass": False, "metrics": {}, "reason": "custom smoke timed out"}
    except Exception as exc:
        return {"pass": False, "metrics": {}, "reason": f"custom smoke error: {exc}"}


def _smoke_for_task(task_id: str, session_id: str = "") -> dict:
    """Pick the right smoke for a task from its ROADMAP `eval_relevance`.

    - task declares `smoke: "<cmd>"`      → run that command (rc==0 passes).
    - `eval_relevance: non-retrieval`     → skip; the verification gate already
      validated the deliverable, and the Recall@5 retrieval smoke is both
      irrelevant and a false-regression risk (n=20 sampling noise) for tasks
      that don't touch the pipeline (data-gen, encode, import, scripts).
    - otherwise (retrieval / unknown)     → full Recall@5 smoke (fail-safe default).
    """
    task_def  = get_task_by_id(task_id) or {}
    custom    = task_def.get("smoke")
    if custom:
        append_journal("smoke_custom", f"{task_id} cmd={str(custom)[:80]}", session_id=session_id)
        return _run_custom_smoke(task_id, str(custom))
    relevance = str(task_def.get("eval_relevance", "retrieval")).strip().lower()
    if relevance == "non-retrieval":
        append_journal("smoke_skipped", f"{task_id} non-retrieval — retrieval smoke skipped",
                       session_id=session_id)
        return {"pass": True, "metrics": {},
                "reason": "non-retrieval task — retrieval smoke skipped (verification gate covered validation)"}
    return _run_smoke(task_id)


# ── Awaited verifications ──

# A verification opts in with `await: true` in its ROADMAP block. These stdout
# substrings mark an awaited artifact as ABSENT (eval not yet written) rather than
# present-but-failing — emitted by check_eval_gate.py's fail-closed paths.
_AWAIT_ABSENT_SIGNALS = (
    "no eval row tagged",
    "history file not found",
    "no baseline row tagged",
)


def _await_absent(v: dict, actual: str, cwd: Path) -> bool:
    """True when an awaitable check's failure means the awaited artifact is ABSENT
    (still running) — not present-but-below-threshold. If the verification names an
    `await_artifact`, absence is decided by that file not existing yet; otherwise by
    the gate's stdout matching a known "row not written" signature."""
    art = v.get("await_artifact")
    if art:
        p = Path(art) if Path(art).is_absolute() else (cwd / art)
        return not p.exists()
    low = actual.lower()
    return any(sig in low for sig in _AWAIT_ABSENT_SIGNALS)


def _classify_verifications(verifications: list[dict], cwd: Path) -> tuple[list[str], list[str]]:
    """Re-run each kind:auto verification and split its failures into
    (pending_awaits, hard_failures):
      pending_awaits — checks marked `await: true` whose awaited artifact is still
                       ABSENT (the async job hasn't produced it yet → keep waiting).
      hard_failures  — everything else: a deterministic check broke, OR an awaited row
                       landed but is below threshold (genuine failure → surface).
    A check whose stdout already equals `expect` is passing and appears in neither list.
    Pure (takes the verification list directly) so it is unit-testable without ROADMAP."""
    pending: list[str] = []
    hard:    list[str] = []
    for v in verifications:
        if v.get("kind") != "auto":
            continue
        vid    = str(v.get("id", ""))
        cmd    = v.get("cmd", "")
        expect = v.get("expect", "")
        if not cmd:
            hard.append(vid)
            continue
        rcmd = re.sub(r"^(python3?)\b", str(VENV_PYTHON), cmd)
        try:
            r = subprocess.run(rcmd, shell=True, capture_output=True, text=True,
                               cwd=str(cwd), timeout=120)
            actual = r.stdout.strip()
        except Exception:
            hard.append(vid)
            continue
        if actual == expect:
            continue
        if v.get("await") and _await_absent(v, actual, cwd):
            pending.append(vid)
        else:
            hard.append(vid)
    return pending, hard


def _await_timed_out(started_iso: str, now: datetime | None = None) -> bool:
    """True once an awaiting-verification park has exceeded AWAIT_VERIFY_TIMEOUT_SEC."""
    now = now or datetime.now(timezone.utc)
    try:
        started = datetime.fromisoformat(started_iso)
    except Exception:
        return False
    return (now - started).total_seconds() > AWAIT_VERIFY_TIMEOUT_SEC
