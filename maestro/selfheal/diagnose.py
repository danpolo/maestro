"""Failure diagnosis: the bounded judge call, the two judgment passes, the sidecar.

Extracted verbatim from the reference orchestrator. Bodies are unchanged — only the
import block and the derivation of the module-level path globals differ. Behavioural
surprises are catalogued in `docs/found_bugs_inbox/selfheal.md` and pinned by
`tests/characterization/test_selfheal.py`; none of them is fixed here — including the
`{.*}` DOTALL scrape that loses both objects when the judge prints two, and the
`.get(k, "")` defaults that let an explicit `null` through.

`_judge_complete` is the only function here that reaches an agent CLI, and it does so
through this module's `subprocess` reference; the characterisation tests swap that rather
than letting anything spawn a process.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

from maestro.paths import Paths
from maestro.state import now_iso

_PATHS = Paths.from_env()

REPO                  = _PATHS.repo

# Judgment-layer model for the orchestrator's own reasoning calls (proposal
# generation, failure diagnosis). Routed through the `claude -p` CLI so it bills
# against the Max subscription, never the metered API. Fable 5 is the preferred
# judge but is currently unavailable in the CLI (checked 2026-06-13:
# `claude -p --model claude-fable-5` returns an "unavailable" notice with rc=0),
# so Opus 4.8 is the active judge. Restore Fable by setting JUDGE_MODEL =
# "claude-fable-5" once Anthropic re-enables CLI access.
JUDGE_MODEL = "claude-opus-4-8"

# Skeptic guard (#2): before an UNATTENDED self-fix edits Maestro's own code, run a
# second, INDEPENDENT pass framed to argue the failure is normal/transient. _diagnose_failure
# is prompted to find a bug, so it carries an action bias — on a transient/external/expected
# failure it still proposes a "fix", and the auto-heal would then edit working code. If this
# pass returns verdict 'normal' with confidence ≥ the threshold, the auto-fix is VETOED and
# Dan is offered `/fix` to override. Dan's explicit `/fix` is never gated by this. Off=skip.
ORCH_SELF_FIX_SKEPTIC = os.environ.get("ORCH_SELF_FIX_SKEPTIC", "1") == "1"
SELF_FIX_SKEPTIC_MIN_CONF = float(os.environ.get("ORCH_SELF_FIX_SKEPTIC_MIN_CONF", "0.6"))
# B-fix: a diagnosis that is NOT auto-eligible is persisted here so Dan can greenlight
# it with `/fix <task_id>` (his approval substitutes for the self-fix eligibility gate;
# the runner's path-confinement + compile gates still apply).
DIAGNOSES_DIR = REPO / ".orchestrator" / "diagnoses"


def _judge_complete(system: str, user: str, model: str = JUDGE_MODEL,
                    timeout: int = 240) -> str:
    """One bounded judgment call via the Claude CLI (Max subscription, never the
    metered API). `claude -p` takes a single positional prompt, so the system
    guidance is folded into the prompt — same pattern as sonnet_review_proofs.
    Returns the model's stdout, or "" on any failure; all callers are
    best-effort and fail safe on an empty result.
    """
    prompt = f"{system}\n\n{user}"
    try:
        r = subprocess.run(
            ["claude", "-p", "--model", model, prompt],
            cwd=str(REPO), capture_output=True, text=True, timeout=timeout,
        )
        out = (r.stdout or "").strip()
        if r.returncode == 0 and out:
            return out
        print(f"  [judge] {model} rc={r.returncode} stderr={(r.stderr or '')[:160]}")
    except Exception as exc:
        print(f"  [judge] {model} call failed: {exc}")
    return ""


def _diagnose_failure(task_id: str, reason: str, impl_tail: str,
                      journal_ctx: str) -> dict:
    """One bounded Opus pass returning a STRUCTURED diagnosis. Best-effort: {} on error.
    Keys: root_cause, failure_class, suggested_fix, target_files."""
    raw = _judge_complete(
        system=(
            "You diagnose why an autonomous coding task (run by 'Maestro', an Opus-"
            "orchestrated multi-agent system) failed after a retry. Maestro's OWN code "
            "lives under scripts/ and orchestrator/. A SEPARATE RAG bot runtime "
            "(main_bot.py, .env, data/, models/) must NEVER be changed by a self-fix. "
            "Reply with ONLY a JSON object, no prose:\n"
            '{"root_cause":"1-2 sentences",'
            '"failure_class":"one of observability|orchestrator-logic|transient-infra|'
            'implementer-quality|task-spec|external-dependency|unknown",'
            '"suggested_fix":"one concrete fix or re-brief",'
            '"target_files":["scripts/...", "..."]}'
        ),
        user=(f"Task: {task_id}\nFailure reason: {reason[:600]}\n\n"
              f"Implementer log tail (impl.log):\n{impl_tail[:2500]}\n\n"
              f"Journal entries:\n{journal_ctx[:2000]}"),
    )
    if not raw:
        return {}
    try:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        return json.loads(m.group(0)) if m else {}
    except Exception:
        return {}


def _failure_looks_normal(task_id: str, reason: str, impl_tail: str,
                          journal_ctx: str) -> dict:
    """Independent skeptic pass that COUNTERWEIGHTS the diagnosis's action bias.

    `_diagnose_failure` is asked to find a bug in Maestro's code, so on a transient,
    external, or expected failure it will still propose a "fix" — and an unattended
    self-fix would then edit working code. This pass is framed the OTHER way: argue the
    failure is normal/transient and that changing Maestro's code would be WRONG. It does
    NOT see the diagnosis, so it cannot anchor on that conclusion.

    Returns {"verdict":"normal|bug","confidence":float,"rationale":str}, or {} on any
    error (caller fails OPEN → the existing auto-fix path is preserved on judge flakes).
    """
    raw = _judge_complete(
        system=(
            "You are a skeptical reviewer guarding an autonomous system ('Maestro') "
            "against UNNECESSARY self-edits. A task it ran failed, and Maestro will edit "
            "its OWN orchestration code unless you stop it. Many failures are NOT code "
            "bugs: usage/rate limits, network or API blips, an external dependency being "
            "down, a flaky or underspecified task, or a one-off timeout — for these the "
            "right action is to change NOTHING and just retry or wait. Argue, as strongly "
            "as the evidence allows, that THIS failure is normal/transient/external and "
            "needs no code change. Concede 'bug' ONLY if the evidence clearly shows a "
            "defect in Maestro's own orchestration logic. Reply with ONLY a JSON object, "
            "no prose:\n"
            '{"verdict":"normal|bug","confidence":0.0-1.0,"rationale":"1-2 sentences"}'
        ),
        user=(f"Task: {task_id}\nFailure reason: {reason[:600]}\n\n"
              f"Implementer log tail (impl.log):\n{impl_tail[:2500]}\n\n"
              f"Journal entries:\n{journal_ctx[:2000]}"),
    )
    if not raw:
        return {}
    try:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        return json.loads(m.group(0)) if m else {}
    except Exception:
        return {}


def _persist_diagnosis(task_id: str, reason: str, diag: dict) -> None:
    """Save a failure diagnosis so Dan can later greenlight it with `/fix <task_id>`."""
    try:
        DIAGNOSES_DIR.mkdir(parents=True, exist_ok=True)
        (DIAGNOSES_DIR / f"{task_id}.json").write_text(json.dumps({
            "task_id": task_id, "reason": reason[:800],
            "failure_class": diag.get("failure_class", ""),
            "root_cause":    diag.get("root_cause", ""),
            "suggested_fix": diag.get("suggested_fix", ""),
            "target_files":  diag.get("target_files", []),
            "ts": now_iso(),
        }), encoding="utf-8")
    except Exception as exc:
        print(f"  [diagnose] persist failed for {task_id}: {exc}")
