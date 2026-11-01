"""Every "stop and hand this to a human" transition: park, finalise, graduate.

Extracted verbatim from the reference orchestrator. Bodies are unchanged — only the
import block and the derivation of the module-level path globals differ. The prepared-action
sidecar is `maestro.prep_actions`, imported as a module so the call sites stay byte-for-byte
identical; three further bodies are owned here rather than
imported, each for the reason spelled out above it. Behavioural
surprises are catalogued in
`docs/found_bugs_inbox/parking.md` and pinned by `tests/characterization/test_parking.py`;
none of them is fixed here — including the resume `attempts` counter that is bumped on
productive sittings too, so steady progress still hits the attempt cap.

Nothing in this module reaches the network or spawns a process for real in the tests: the
git pushes and the post-action finalize command go through this module's `subprocess`
reference, and every operator-facing message through `notify_telegram` / `_danreq`, all of
which the characterisation tests swap.
"""
from __future__ import annotations

import os
import re
import subprocess
from subprocess import SubprocessError
from datetime import datetime, timedelta, timezone
from pathlib import Path

from maestro import prep_actions
from maestro.docs.roadmap import get_task_by_id, mark_roadmap_complete, run_dep_map
from maestro.gates import (
    AWAIT_VERIFY_TIMEOUT_SEC,
    _await_timed_out,
    _classify_verifications,
    run_verification_gate,
)
from maestro.hitl.telegram import _escalation_req_id, notify_telegram, phase_report
from maestro.implementer import _answer_choice, _latest_impl_tail
from maestro.merge import (
    _merge_data_only,
    _merge_prep_branch,
    _parse_progress,
    _resumable_code_change_escalation,
)
from maestro.paths import Paths
from maestro import metrics
from maestro.quota import _LIMIT_RE
from maestro.selfheal.diagnose import (
    ORCH_SELF_FIX_SKEPTIC,
    SELF_FIX_SKEPTIC_MIN_CONF,
    _diagnose_failure,
    _failure_looks_normal,
    _persist_diagnosis,
)
from maestro.selfheal.selffix import _self_fix_eligible, attempt_self_fix
from maestro.state import append_journal, now_iso, read_state, write_state
from maestro.status import print_status
from maestro.worktree import remove_worktree, worktree_path_for
from maestro.workflows import handlers as _facade

_PATHS = Paths.from_env()

REPO                  = _PATHS.repo
JOURNAL               = _PATHS.journal
WORKSPACES            = _PATHS.workspaces
QUESTIONS_DIR         = REPO / ".orchestrator" / "questions"
VENV_PYTHON           = REPO / ".venv" / "bin" / "python3"

# B8/Maestro: failure diagnosis via Opus. Now default ON — Fix A captures impl.log,
# so the diagnosis finally has real evidence to reason over (before, the journal only
# held "window gone" and the pass was useless). Set ORCH_DIAGNOSE=0 to disable.
ORCH_DIAGNOSE = os.environ.get("ORCH_DIAGNOSE", "1") == "1"

# The B14 auto-prep sidecar. The reference imports `prepared_actions` as a sibling module
# off `scripts/`; its maestro home is `maestro.prep_actions` (M4a), imported at the top.


# ── Bodies this module has to own rather than import ──
#
# `_danreq` (mapped to `maestro.hitl.telegram`) is already extracted, but it shells out,
# and the reference resolves `subprocess` in ONE flat namespace. `park_failed` reaches it
# on a path the characterisation tests exercise unstubbed, pinning the argv through *this*
# module's `subprocess` reference — importing it would send those calls through a sibling
# module's reference instead, i.e. at a real `git`/`python3`. `_remove_from_state` is
# mapped to `maestro.orchestrator`, which is extracted last, and `handle_incomplete`
# likewise calls it unstubbed, so a `pending()` placeholder would make the function
# untestable. Both bodies are copied verbatim; each is one half of a pair whose other half
# becomes an import once the modules can share a single `subprocess` seam.
#
# `run_status` below is the exception: R13 (`maestro/status.py`) moved the status report
# in-process, so there is no more subprocess argv to pin here — it is a two-line wrapper
# around `maestro.status.print_status()`, kept as this module's own copy (rather than an
# import of `maestro.hitl.commands.run_status`) only so the existing unstubbed call sites
# below need no further changes.
#
# `run_dep_map` used to be here too, for the same subprocess-pinning reason as `_danreq`
# above — but R5–R7 moved `maestro.docs.roadmap.run_dep_map` itself off `subprocess` onto
# in-process calls into `maestro.docs.depmap`/`.upcoming` (see that module), so there is no
# more argv left to pin through a shared `subprocess` reference. It is imported directly
# (above, alongside `get_task_by_id`/`mark_roadmap_complete`) rather than kept as a second
# copy of a function that no longer shells out to anything.
def _danreq(question: str, options: list[str], req_type: str = "decision",
            req_id: str | None = None) -> None:
    cmd = [str(VENV_PYTHON), "-m", "maestro.hitl.dan_request",
           "--type", req_type, "--question", question,
           "--options", ",".join(options)]
    if req_id:
        cmd += ["--id", req_id]
    subprocess.run(cmd, cwd=str(REPO), capture_output=True)


def run_status() -> None:
    print_status()


def _remove_from_state(entry: dict) -> None:
    state = read_state()
    sid = entry["session_id"]
    state["in_flight"] = [e for e in state.get("in_flight", [])
                           if e["session_id"] != sid]
    # Bug #2: drop the persisted launch time for this finished session.
    state["launch_times"] = {k: v for k, v in state.get("launch_times", {}).items()
                             if k != sid}
    write_state(state)


def park_regression(task_id: str, smoke: dict) -> None:
    shown  = metrics.summary(smoke)
    reason = smoke.get("reason", "")[:200]
    _danreq(f"Regression on {task_id}: {shown}. {reason}. What to do?",
            ["Revert and shelve", "Revert and retry", "Keep despite regression"])
    append_journal("regression_escalated", f"{task_id} {shown}")


def _retry_phrase(task_id: str, reason: str) -> str:
    """Bug #3 (2026-06-26): word the escalation by the ACTUAL retry count. The B10
    timeout-park path escalates with 0 retries burned, so the old hardcoded
    'after 1 retry' was misleading. A timeout death is also tagged explicitly."""
    n = read_state().get("retry_counts", {}).get(task_id, 0)
    phrase = f"after {n} {'retry' if n == 1 else 'retries'}"
    if reason.startswith("timeout after"):
        phrase += " (timeout)"
    return phrase


def park_failed(task_id: str, reason: str) -> None:
    state = read_state()
    parked = state.get("parked_tasks", [])
    if task_id not in parked:
        parked.append(task_id)
        state["parked_tasks"] = parked
        write_state(state)

    # Idempotent escalation: a given (task, failure-class) is asked exactly once.
    # If Dan already answered an identical escalation, reuse that answer and stay
    # parked — don't re-ask. If it's asked-but-unanswered, also skip. This kills
    # the "asked the same question time and time again" storm when a task keeps
    # failing the same way (e.g. recurring "window gone — stale in_flight").
    req_id = _escalation_req_id(task_id, reason)
    prior = _answer_choice(req_id)
    if prior is not None:
        append_journal("failed_escalation_reused",
                       f"{task_id} req={req_id} prior={prior[:60]}")
        return
    if (QUESTIONS_DIR / f"{req_id}.json").exists():
        append_journal("failed_escalation_pending", f"{task_id} req={req_id}")
        return

    _danreq(f"{task_id} failed {_retry_phrase(task_id, reason)}: {reason[:200]}. What to do?",
            ["Retry with new approach", "Shelve task", "Manual intervention"],
            req_id=req_id)
    append_journal("failed_escalated", f"{task_id} reason={reason[:80]}")
    # Maestro diagnosis + self-heal. ORCH_DIAGNOSE is now default-on because Fix A
    # captures impl.log, giving the pass real evidence. A usage-limit death is already
    # handled deterministically by the reactive net (B) and should never reach here —
    # but guard anyway so we never spend the (scarce, shared) quota narrating it.
    journal_events: list[str] = []
    if JOURNAL.exists():
        with open(JOURNAL, encoding="utf-8") as fh:
            journal_events = [ln.strip() for ln in fh if task_id in ln]
    impl_tail = _latest_impl_tail(task_id, 30)
    if _LIMIT_RE.search(impl_tail or "") or _LIMIT_RE.search(reason or ""):
        print(f"  [diagnose] {task_id}: usage-limit signature — skipping LLM (handled by backoff).")
        append_journal("diagnose_skipped_usage_limit", task_id)
        return
    if ORCH_DIAGNOSE:
        # Bounded Opus pass → structured diagnosis. Best-effort: never block the park.
        try:
            journal_ctx = "\n".join(journal_events[-20:])[:4000]
            diag = _diagnose_failure(task_id, reason, impl_tail, journal_ctx)
            if diag:
                root = diag.get("root_cause", "")
                fix  = diag.get("suggested_fix", "")
                cls  = diag.get("failure_class", "?")
                tgt  = ", ".join(diag.get("target_files") or []) or "—"
                notify_telegram(
                    f"🔎 *{task_id}* diagnosis (class: {cls})\n"
                    f"Root cause: {root[:400]}\n"
                    f"Suggested fix: {fix[:400]}\n"
                    f"Likely files: {tgt}")
                _persist_diagnosis(task_id, reason, diag)
                append_journal("failure_diagnosis",
                               f"{task_id} class={cls}: {root[:160]}")
                ok, why = _self_fix_eligible(diag)
                # #2: independent skeptic veto — only on the unattended auto-fix path.
                veto = None
                conf = 0.0
                if ok and ORCH_SELF_FIX_SKEPTIC:
                    skeptic = _failure_looks_normal(task_id, reason, impl_tail, journal_ctx)
                    conf = float(skeptic.get("confidence") or 0)
                    if skeptic.get("verdict") == "normal" and conf >= SELF_FIX_SKEPTIC_MIN_CONF:
                        veto = skeptic.get("rationale") or "looks transient/normal"
                if ok and veto is None:
                    notify_telegram(f"→ Auto-eligible — implementing the fix for `{task_id}` now.")
                    attempt_self_fix(task_id, reason, diag)   # prepare + gate (D)
                elif veto is not None:
                    notify_telegram(
                        f"→ Auto-fix held for `{task_id}`: a second, independent check thinks "
                        f"this is likely *normal/transient*, not a Maestro bug ({conf:.0%}): "
                        f"{veto[:300]}\nReply `/fix {task_id}` to override and fix anyway, or "
                        f"`/unpark {task_id}` to retry / `/reject` to skip.")
                    append_journal("self_fix_vetoed_skeptic",
                                   f"{task_id} conf={conf:.2f}: {veto[:160]}")
                    print(f"  [self-fix] {task_id}: skeptic veto ({conf:.2f}) — /fix offered")
                else:
                    notify_telegram(
                        f"→ Reply `/fix {task_id}` to have me implement this fix, or "
                        f"`/unpark {task_id}` to retry as a task / `/reject` to skip.")
                    append_journal("self_fix_skipped", f"{task_id} {why}")
                    print(f"  [self-fix] {task_id}: not eligible ({why}) — /fix offered")
            else:
                notify_telegram(f"🔎 *{task_id}* failed; diagnosis pass returned nothing.\n"
                                f"Reason: {reason[:300]}")
        except Exception as exc:
            print(f"  [judge-diagnose] skipped: {exc}")
    else:
        last_lines = "\n".join(journal_events[-15:])
        notify_telegram(
            f"ℹ *{task_id}* failure (ORCH_DIAGNOSE off):\n"
            f"Reason: {reason[:300]}\n\nLast journal lines:\n```\n{last_lines[:600]}\n```")
        print(f"  [diagnose] skipped (ORCH_DIAGNOSE=0). reason={reason[:120]}")


def _session_uuid_for(session_id: str) -> str:
    """The resumable Claude session UUID for a launched implementer (written by
    launch_implementer to session_uuid.txt). Empty for legacy/hand-seeded items —
    /ask falls back to brief-seeded context in that case."""
    if not session_id:
        return ""
    try:
        return (WORKSPACES / session_id / "session_uuid.txt").read_text(
            encoding="utf-8").strip()
    except Exception:
        return ""


def park_for_dan(entry: dict, impl_summary: str) -> int:
    """Pause a verified task pending Dan's /approve or /reject. Returns dan_id."""
    state   = read_state()
    dan_id  = state.get("dan_id_counter", 0) + 1
    state["dan_id_counter"] = dan_id
    waiting = state.get("waiting_on_dan", {})
    waiting[str(dan_id)] = {
        "task_id":   entry["task_id"],
        "session_id": entry["session_id"],
        "branch":    entry.get("branch", ""),
        "worktree":  entry.get("worktree", ""),
        "parked_at": now_iso(),
        "summary":   impl_summary[:200],
        # F: resumable Claude session that built this task, for /ask follow-ups.
        "session_uuid": _session_uuid_for(entry["session_id"]),
    }
    state["waiting_on_dan"] = waiting
    write_state(state)
    workspace = WORKSPACES / entry["session_id"]
    # `[P3]`: the sentinel's NAME comes from the compatibility facade rather than a local
    # literal, so the file the reconcile loop looks for and the file this writes cannot
    # diverge. The write itself stays a bare `write_text` — the facade's writer creates the
    # workspace directory, and doing that here would quietly repair the FOUND_BUGS
    # behaviour that `test_park_for_dan_without_a_workspace_dies_after_mutating_state`
    # exists to pin. P3 relocates authority; it does not fix legacy bugs in passing (`D08`).
    (workspace / _facade.PAUSED_SENTINEL).write_text(str(dan_id))
    task_id = entry["task_id"]
    branch  = entry.get("branch", "")
    notify_telegram(
        f"⏸ *{task_id}* passed all gates — waiting for your review (ID: {dan_id})\n"
        f"Branch: `{branch}`\n"
        f"{impl_summary[:200]}\n\n"
        f"Reply /approve {dan_id} or /reject {dan_id}"
    )
    append_journal("hitl_parked", f"{task_id} dan_id={dan_id}", session_id=entry["session_id"])
    return dan_id


def park_manual_action(task_id: str, action: str, post_action_cmd: str | None,
                       branch: str = "", worktree: str = "", session_id: str = "",
                       *, manual_contract: dict | None = None) -> int:
    """Park a dispatch:manual task whose PREP is done — Dan must perform ONE action.

    Records the action in the sidecar + waiting_on_dan (kind='manual-action') and pings
    Dan with exactly that one action. Returns the dan_id.
    """
    state  = read_state()
    dan_id = state.get("dan_id_counter", 0) + 1
    state["dan_id_counter"] = dan_id
    waiting = state.get("waiting_on_dan", {})
    waiting[str(dan_id)] = {
        "task_id":    task_id,
        "session_id": session_id,
        "branch":     branch,
        "worktree":   worktree,
        "parked_at":  now_iso(),
        "kind":       "manual-action",
        "summary":    action[:200],
        # F: resumable Claude session that prepped this job, for /ask follow-ups.
        "session_uuid": _session_uuid_for(session_id),
    }
    state["waiting_on_dan"] = waiting
    if manual_contract:
        waiting[str(dan_id)]['manual_contract'] = manual_contract
    write_state(state)
    prep_actions.set_action(task_id, action, post_action_cmd=post_action_cmd, dan_id=dan_id)
    expected = (f"Expected result: {manual_contract['expected_result']}\n"
                f"Command: {manual_contract.get('command') or action}\n"
                "Maestro checks the captured output/exit code and the step check.\n\n"
                if manual_contract else '')
    notify_telegram(
        f"🎯 *{task_id}* is prepped — your ONE action (ID {dan_id}):\n\n{action}\n\n"
        f"{expected}"
        f"When you're done:\n"
        f"✅ I ran it → /approve {dan_id}  (a run acknowledgement; Maestro verifies the result)\n"
        f"❓ Problem / question → /ask {dan_id} <your message>  (I resume the session that prepared this and help)\n"
        f"🔧 Needs a rewrite → /redo {dan_id} <what's wrong>  (I rewrite + re-upload the notebook/script, syntax-gated)\n"
        f"❌ Abandon → /reject {dan_id}"
    )
    append_journal("manual_action_parked", f"{task_id} dan_id={dan_id}", session_id=session_id)
    return dan_id


def _prepare_manual_capture(task_id: str, task_def: dict, entry: dict, impl: dict) -> dict:
    from maestro import manual_steps

    fields = {k: str(impl.get(k) or task_def.get(k) or '').strip()
              for k in ('step_command', 'step_check_cmd', 'expected_result', 'step_kind', 'dan_action')}
    external = fields['step_kind'] == 'external' or fields['dan_action'].startswith(('https://', 'http://'))
    options = dict(run_id=f"legacy:{task_id}:{entry['session_id']}", attempt_id=entry['session_id'],
                   tree=REPO, step_check_cmd=fields['step_check_cmd'], expected_result=fields['expected_result'])
    if external and not fields['step_command']:
        return manual_steps.prepare_external(**options)
    return manual_steps.prepare(REPO, command=fields['step_command'] or fields['dan_action'], **options)


def _retain_manual_evidence(dan_id_str: str, key: str, evidence: object) -> None:
    state = read_state()
    parked = state.get('waiting_on_dan', {}).get(dan_id_str)
    if parked is not None:
        parked[key] = evidence
        write_state(state)


def _verify_manual_capture(dan_id_str: str, parked: dict) -> bool:
    from maestro import manual_steps

    contract = parked.get('manual_contract')
    refusal, evidence = (manual_steps.verify(contract, REPO) if contract
                        else ('manual capture contract missing; request /redo before retrying', {}))
    _retain_manual_evidence(dan_id_str, 'manual_evidence', evidence)
    if refusal:
        append_journal('manual_action_capture_failed', f"{parked['task_id']} {refusal}")
        notify_telegram(f"⚠ {parked['task_id']}: capture verification failed — {refusal[:500]}. Item stays parked.")
        return False
    return True


def _legacy_repair(dan_id_str: str, parked: dict, detail: str) -> None:
    """Use the graph's durable intake publication protocol for a legacy deployed failure."""
    from types import SimpleNamespace
    from maestro import state as control_state
    from maestro.workflows.post_merge import publish_fix

    parked.update(kind='post-merge-repair', repair_detail=detail)
    _retain_manual_evidence(dan_id_str, 'kind', parked['kind'])
    _retain_manual_evidence(dan_id_str, 'repair_detail', detail)
    controller = SimpleNamespace(repo=REPO, store=control_state.control_store(),
        run_id=f"legacy:{parked['task_id']}:{parked['session_id']}",
        revision=SimpleNamespace(task_id=parked['task_id']),
        merged_snapshot=lambda: parked['verification_snapshot'])
    fix_task = publish_fix(controller, SimpleNamespace(node_id='post_merge_checks', detail=detail))
    _retain_manual_evidence(dan_id_str, 'fix_task', fix_task)
    _retain_manual_evidence(dan_id_str, 'kind', 'post-merge-failed')


def _legacy_machine_checks(dan_id_str: str, parked: dict) -> bool:
    import hashlib
    import json
    from maestro import validation

    snapshot = validation.snapshot_id(REPO)
    if parked.get('verification_snapshot') and parked['verification_snapshot'] != snapshot:
        _retain_manual_evidence(dan_id_str, 'kind', 'verification-drift')
        return False
    parked['verification_snapshot'] = snapshot
    _retain_manual_evidence(dan_id_str, 'verification_snapshot', snapshot)
    entries = [v for v in (get_task_by_id(parked['task_id']) or {}).get('verifications', ())
               if v.get('kind') == 'auto' and v.get('stage') == 'post_merge']
    signature = hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()
    retained = parked.get('deployed_evidence', {})
    if retained.get('accepted') and retained.get('signature') == signature:
        return True
    specs = [validation.VerificationSpec.from_roadmap(v) for v in entries]
    if not specs:
        return True
    registry = validation.EvidenceRegistry()
    evidence = validation.CheckRunner(registry).evaluate(
        specs, REPO, attempt_id=parked['session_id'], snapshot=snapshot)
    decision = validation.decide(task_id=parked['task_id'], run_id=parked['session_id'],
        specs=specs, evidence=evidence, required_criterion_ids=[s.check_id for s in specs],
        integrated_source_snapshot=snapshot, expected_snapshot=snapshot)
    _retain_manual_evidence(dan_id_str, 'deployed_evidence', {
        'evidence': [e.to_row() for e in evidence], 'output': registry.held_output,
        'signature': signature, 'accepted': decision.accepted})
    if not decision.accepted:
        _legacy_repair(dan_id_str, parked, decision.reason)
        return False
    return True


def _legacy_observation(dan_id_str: str, parked: dict) -> bool:
    from maestro import validation, taskgraph
    from maestro.hitl.verify import DanVerificationChannel
    from maestro.workflows.compiler import dan_checks

    task = get_task_by_id(parked['task_id']) or {}
    checks = dan_checks(taskgraph.normalize_spec({'id': parked['task_id'], **task}))
    if not checks:
        return True
    snapshot = parked.get('verification_snapshot') or validation.snapshot_id(REPO)
    _retain_manual_evidence(dan_id_str, 'verification_snapshot', snapshot)
    parked['verification_snapshot'] = snapshot
    def drifted():
        if validation.snapshot_id(REPO) != snapshot:
            _retain_manual_evidence(dan_id_str, 'kind', 'verification-drift')
            return True
        return False
    if drifted():
        return False
    verdict = DanVerificationChannel().poll(task_id=parked['task_id'],
        title=str(task.get('title') or ''), run_id=f"legacy:{parked['task_id']}:{parked['session_id']}",
        snapshot=snapshot, worktree=str(REPO), checks=checks, operator=True)
    if drifted():
        return False
    if verdict is None:
        _retain_manual_evidence(dan_id_str, 'kind', 'operator-verification')
        return False
    _retain_manual_evidence(dan_id_str, 'operator_verdict', {
        'verdict': verdict.verdict, 'subject_sha': snapshot, 'request_id': verdict.request_id,
        'reason': verdict.reason})
    if not verdict.approved:
        if parked.get('manual_contract') or parked.get('merged_completion'):
            _legacy_repair(dan_id_str, parked, verdict.reason)
        else:
            _retain_manual_evidence(dan_id_str, 'kind', 'operator-failed')
        return False
    return True


def poll_manual_observations(in_flight: list) -> None:
    for dan_id, parked in list(read_state().get('waiting_on_dan', {}).items()):
        if parked.get('kind') == 'post-merge-repair':
            _legacy_repair(dan_id, parked, parked['repair_detail'])
        elif parked.get('kind') == 'operator-verification' and _legacy_observation(dan_id, parked):
            _graduate_manual_action(parked['task_id'], dan_id, parked.get('session_id', ''), in_flight)
        elif parked.get('kind') == 'post-merge-checking':
            if _legacy_machine_checks(dan_id, parked) and _legacy_observation(dan_id, parked):
                _graduate_manual_action(parked['task_id'], dan_id, parked.get('session_id', ''), in_flight)


def defer_merged_verification(entry: dict, smoke: dict, in_flight: list) -> bool:
    """After a successful legacy merge, retain the completion until deployed proof lands.

    False leaves the caller's normal immediate completion tail in charge. True means
    the merged task is parked, never sent back through merge/retry on deployed failure.
    """
    task = get_task_by_id(entry['task_id']) or {}
    if not any(v.get('kind') == 'dan' or v.get('stage') == 'post_merge'
               for v in task.get('verifications', ())):
        return False
    state = read_state()
    dan_id = str(state.get('dan_id_counter', 0) + 1)
    state['dan_id_counter'] = int(dan_id)
    parked = dict(entry, kind='post-merge-checking', parked_at=now_iso(),
                  merged_completion={'smoke': smoke})
    state.setdefault('waiting_on_dan', {})[dan_id] = parked
    write_state(state)
    if not (_legacy_machine_checks(dan_id, parked) and _legacy_observation(dan_id, parked)):
        return True
    state = read_state()
    state['waiting_on_dan'].pop(dan_id, None)
    write_state(state)
    return False


def resume_awaited_verification(dan_id: str, parked: dict, in_flight: list) -> None:
    """A manual step still owes capture; read-only async jobs owe no human action."""
    task = get_task_by_id(parked['task_id']) or {}
    if parked.get('manual_contract') or task.get('dispatch') == 'manual':
        _finalize_manual_action(dan_id, parked, in_flight)
    elif _legacy_machine_checks(dan_id, parked) and _legacy_observation(dan_id, parked):
        _graduate_manual_action(parked['task_id'], dan_id, parked.get('session_id', ''), in_flight)


def handle_prep_done(task_id: str, task_def: dict, entry: dict, impl) -> None:
    """A dispatch:manual PREP implementer finished. Merge its prep (no eval/graduation),
    harvest the single Dan action, and park as manual-action."""
    impl = impl if isinstance(impl, dict) else {}
    action = (impl.get("dan_action") or task_def.get("dan_action")
              or "(prep done — see the ROADMAP task for the action)")
    post_cmd = impl.get("post_action_cmd") or task_def.get("post_action_cmd")
    merged, msg = _merge_prep_branch(entry)
    append_journal("prep_merged" if merged else "prep_merge_failed",
                   f"{task_id} {msg}", session_id=entry["session_id"])
    if not merged:
        notify_telegram(f"⚠ {task_id}: prep done but merge blocked — {msg[:160]}. "
                        f"Parking the action anyway; resolve the tree before /approve.")
    contract = None
    if merged:
        try:
            contract = _prepare_manual_capture(task_id, task_def, entry, impl)
            if contract.get('script'):
                import shlex
                action = 'bash ' + shlex.quote(contract['script'])
        except (ValueError, OSError, SubprocessError) as exc:
            notify_telegram(f"⚠ {task_id}: manual capture unavailable — {exc}. Request /redo; Done cannot graduate it.")
    park_manual_action(
        task_id, action, post_cmd,
        branch=entry.get("branch", ""), worktree=entry.get("worktree", ""),
        session_id=entry["session_id"],
        manual_contract=contract,
    )
    wt = entry.get("worktree", "")
    if wt and Path(wt).exists():
        remove_worktree(Path(wt))
    _remove_from_state(entry)
    if merged:
        subprocess.run(["git", "push", "origin", "main"], cwd=str(REPO), capture_output=True)
    run_dep_map()
    run_status()


def _graduate_manual_action(task_id: str, dan_id_str: str, session_id: str,
                            in_flight: list) -> None:
    """Final graduation tail shared by /approve and the awaiting-verification poller:
    pop the waiting entry + sidecar, clear PAUSED, mark complete, push, report."""
    state   = read_state()
    waiting = state.get("waiting_on_dan", {})
    parked = waiting.pop(dan_id_str, {})
    state["waiting_on_dan"] = waiting
    write_state(state)
    prep_actions.remove_action(task_id)
    in_flight[:] = [e for e in in_flight if e.get("task_id") != task_id]
    workspace = WORKSPACES / (session_id or "")
    (workspace / _facade.PAUSED_SENTINEL).unlink(missing_ok=True)
    mark_roadmap_complete(task_id)
    subprocess.run(["git", "push", "origin", "main"], cwd=str(REPO), capture_output=True)
    append_journal("task_complete", f"{task_id} (manual-action graduated)")
    notify_telegram(f"✅ {task_id} verified and graduated. Nice — that's done.")
    completion = parked.get('merged_completion')
    if completion is not None:
        from maestro.merge import _touches_bot_files, run_canary_deploy

        remove_worktree(Path(parked['worktree']))
        state = read_state()
        if state.get('resume_state', {}).pop(task_id, None) is not None:
            write_state(state)
        if parked.get('branch') and _touches_bot_files(parked['branch']):
            if not run_canary_deploy(task_id):
                append_journal('canary_reverted', f'{task_id} canary deploy failed')
                run_dep_map()
                run_status()
                return
    phase_report(task_id, get_task_by_id(task_id), completion['smoke'] if completion is not None else {})
    run_dep_map()
    run_status()


def _park_awaiting_verification(dan_id_str: str, task_id: str, pending_ids: list[str]) -> None:
    """Flip a just-approved manual-action entry to `awaiting-verification`: Dan's action
    succeeded but a verification depends on an async job (e.g. the eval it kicked off)
    that hasn't landed yet. The poller re-checks each cycle and finalizes when it passes."""
    state   = read_state()
    waiting = state.get("waiting_on_dan", {})
    info    = waiting.get(dan_id_str)
    if not info:
        return
    info["kind"]             = "awaiting-verification"
    info["await_started_at"] = now_iso()
    info["await_checks"]     = pending_ids
    info.pop("reminded_at", None)
    waiting[dan_id_str] = info
    state["waiting_on_dan"] = waiting
    write_state(state)
    append_journal("await_verify_parked", f"{task_id} dan_id={dan_id_str} {','.join(pending_ids)}")
    notify_telegram(
        f"✅ Approved *{task_id}* — your action's done. Verification "
        f"{', '.join(pending_ids)} depends on an async job (e.g. the eval your action "
        f"kicked off) that hasn't landed yet. I'll re-check every poll and finalize "
        f"automatically when it passes — no further action needed unless it fails or "
        f"times out (after {AWAIT_VERIFY_TIMEOUT_SEC // 3600} h)."
    )


def _surface_await_failure(dan_id_str: str, parked: dict, failing_ids: list[str],
                           reason: str) -> None:
    """An awaiting-verification check resolved to a genuine failure (`reason='gate'`,
    the awaited row landed but is below threshold) or the wait timed out
    (`reason='timeout'`). Revert the entry to `manual-action` so HITL reminders resume
    and Dan can /approve-retry or /reject, and ping him once. Reverting the kind also
    makes this idempotent — the poller no longer matches it, so no per-poll re-ping."""
    task_id = parked.get("task_id", "")
    state   = read_state()
    waiting = state.get("waiting_on_dan", {})
    info    = waiting.get(dan_id_str)
    if not info:
        return
    info["kind"]                = "manual-action"
    info["await_failed_reason"] = reason
    info.pop("reminded_at", None)
    waiting[dan_id_str] = info
    state["waiting_on_dan"] = waiting
    write_state(state)
    append_journal("await_verify_surfaced", f"{task_id} reason={reason} {','.join(failing_ids)}")
    if reason == "timeout":
        notify_telegram(
            f"⏰ *{task_id}* (ID {dan_id_str}): awaited verification "
            f"({', '.join(failing_ids)}) never landed within "
            f"{AWAIT_VERIFY_TIMEOUT_SEC // 3600} h. Check the eval/job, then "
            f"/approve {dan_id_str} to retry or /reject {dan_id_str}."
        )
    else:
        notify_telegram(
            f"⚠ *{task_id}* (ID {dan_id_str}): awaited verification landed but FAILED "
            f"({', '.join(failing_ids)}) — present, below threshold, not still-running. "
            f"Investigate, then /approve {dan_id_str} to retry or /reject {dan_id_str}."
        )


def _finalize_manual_action(dan_id_str: str, parked: dict, in_flight: list) -> None:
    """Dan performed his one action for a dispatch:manual task and /approve'd it.

    The prep branch was already merged at prep time, so there is no branch to merge here.
    Run the optional post_action_cmd (e.g. import Colab output), then the AUTO verification
    gate, then graduate. On any failure, leave the item parked and tell Dan why.
    """
    task_id = parked["task_id"]
    notify_telegram(f"✅ Finalizing {task_id} — running post-action + verification gate…")
    append_journal("manual_action_approved", f"{task_id} dan_id={dan_id_str}")

    from maestro import validation

    if not parked.get('manual_capture_verified'):
        if not _verify_manual_capture(dan_id_str, parked):
            return
        parked['manual_capture_verified'] = validation.snapshot_id(REPO)
        _retain_manual_evidence(dan_id_str, 'manual_capture_verified', parked['manual_capture_verified'])

    completed = parked.get('finalizer_output', {})
    expected = (completed.get('source_after') if completed.get('exit_status') == 0
                else parked['manual_capture_verified'])
    if expected and validation.snapshot_id(REPO) != expected:
        _retain_manual_evidence(dan_id_str, 'kind', 'verification-drift')
        return

    sidecar  = prep_actions.get_action(task_id) or {}
    post_cmd = sidecar.get("post_action_cmd") or (get_task_by_id(task_id) or {}).get("post_action_cmd")
    if post_cmd and not (completed.get('exit_status') == 0 and completed.get('source_after')
                         and completed.get('requested_command') == post_cmd):
        notify_telegram(f"⚙ {task_id}: running finalize — `{post_cmd}`")
        cmd = re.sub(r"^(python3?)\b", str(VENV_PYTHON), post_cmd)
        r = subprocess.run(cmd, shell=True, cwd=str(REPO),
                           capture_output=True, text=True, timeout=1800)
        _retain_manual_evidence(dan_id_str, 'finalizer_output', {
            'command': cmd, 'requested_command': post_cmd, 'exit_status': r.returncode,
            'stdout': r.stdout, 'stderr': r.stderr, 'source_after': validation.snapshot_id(REPO)})
        if r.returncode != 0:
            err = (r.stderr or r.stdout)[:300]
            append_journal("manual_action_postcmd_failed", f"{task_id} {err[:160]}")
            notify_telegram(f"⚠ {task_id}: finalize cmd failed — {err[:200]}\n"
                            f"Item stays parked (ID {dan_id_str}); fix and /approve again.")
            return

    if not _legacy_machine_checks(dan_id_str, parked):
        return

    # Auto-only gate: dispatch:manual graduation has no implementer result.json, so manual
    # proof items are taken as Dan-attested by his /approve. Changes already live in REPO.
    gate_ok, gate_msg = run_verification_gate(task_id, REPO, REPO, auto_only=True,
                                             pre_merge_only=True)
    if not gate_ok:
        # Handoff B: distinguish a FALSE failure (an `await: true` check whose async job
        # hasn't produced its row yet) from a genuine one. If the only failures are
        # absent-awaited checks, park as `awaiting-verification` and re-check each poll
        # instead of rejecting. Any hard failure (deterministic check broke, or an
        # awaited row landed below threshold) → surface as before.
        verifs = [v for v in (get_task_by_id(task_id) or {}).get('verifications', ())
                  if v.get('kind') != 'dan' and v.get('stage') != 'post_merge']
        pending, hard = _classify_verifications(verifs, REPO)
        if pending and not hard:
            _park_awaiting_verification(dan_id_str, task_id, pending)
            return
        append_journal("manual_action_gate_failed", f"{task_id} {gate_msg[:160]}")
        notify_telegram(f"⚠ {task_id}: verification gate FAILED after your action —\n{gate_msg[:300]}\n"
                        f"Item stays parked (ID {dan_id_str}). Fix and /approve again.")
        return

    if _legacy_observation(dan_id_str, parked):
        _graduate_manual_action(task_id, dan_id_str, parked.get("session_id", ""), in_flight)


# ── INCOMPLETE / resumable (multi-day, quota-bound) task handling ──

RESUME_COOLDOWN_H_DEFAULT = 20    # hours before re-queuing (≈ daily quota reset window)
MAX_RESUME_ATTEMPTS       = 12    # hard cap on sittings before escalating to Dan
MAX_RESUME_STALLS         = 2     # consecutive no-progress sittings tolerated (early cooldown)


def handle_incomplete(task_id: str, task_def: dict, entry: dict, gate_msg: str) -> None:
    """Resumable task whose DONE gate failed. If forward progress advanced, merge the
    partial (data-only) work, set a cooldown, and re-queue (NOT graduated, NOT failed).
    On a real stall, attempt-cap, or unexpected code change, park for Dan instead so a
    broken task can't loop forever."""
    worktree = Path(entry.get("worktree") or worktree_path_for(task_id))
    sid      = entry.get("session_id", "")
    state    = read_state()
    rs_all   = state.get("resume_state", {})
    rs       = rs_all.get(task_id, {})
    prev     = int(rs.get("last_count", -1))
    attempts = int(rs.get("attempts", 0))
    stalls   = int(rs.get("stalls", 0))

    def _park(reason: str) -> None:
        park_failed(task_id, reason)
        remove_worktree(worktree)
        st = read_state()
        st.get("resume_state", {}).pop(task_id, None)
        write_state(st)
        _remove_from_state(entry)
        notify_telegram(f"⚠ {task_id} (resumable): {reason[:140]} — parked for Dan.")
        append_journal("resume_parked", f"{task_id} {reason[:160]}", session_id=sid)

    prog = _parse_progress(gate_msg)
    if prog is None:
        _park(f"gate progress unparseable: {gate_msg[:160]}")
        return
    done, total = prog
    if attempts >= MAX_RESUME_ATTEMPTS:
        _park(f"resume attempt cap reached at {done}/{total} (attempts={attempts})")
        return

    if done > prev:                       # forward progress this sitting
        stalls = 0
        merge_status = _merge_data_only(entry)
        if merge_status == "code_change":
            # Out-of-allowlist files changed. Ask the Opus judge (not Dan) whether the
            # non-data/docs diff is safe to merge w/o eval; hard-stop set still parks.
            approved, reason, merge_status = _resumable_code_change_escalation(entry, task_id)
            if not approved:
                _park(f"code_change not approved: {reason}")
                return
        elif merge_status not in ("ok", "empty"):
            _park(f"data merge {merge_status} on branch {entry.get('branch','')}")
            return
    else:                                 # no new posts processed
        stalls += 1
        if stalls >= MAX_RESUME_STALLS:
            _park(f"no progress for {stalls} sittings at {done}/{total}")
            return
        merge_status = "no-op (no new progress)"

    cooldown_h   = int(task_def.get("resume_cooldown_h", RESUME_COOLDOWN_H_DEFAULT))
    resume_after = (datetime.now(tz=timezone.utc) + timedelta(hours=cooldown_h)).isoformat()
    rs_all[task_id] = {
        "last_count": done, "total": total, "attempts": attempts + 1,
        "stalls": stalls, "resume_after": resume_after, "updated": now_iso(),
    }
    state["resume_state"] = rs_all
    write_state(state)
    remove_worktree(worktree)
    _remove_from_state(entry)             # re-reads state, preserves resume_state
    pct = (100 * done // total) if total else 0
    append_journal(
        "task_incomplete",
        f"{task_id} {done}/{total} ({pct}%) {merge_status} "
        f"attempt={attempts + 1} cooldown={cooldown_h}h", session_id=sid,
    )
    notify_telegram(
        f"⏳ {task_id} incomplete: {done}/{total} posts ({pct}%) — "
        f"progress saved, resuming in ~{cooldown_h}h."
    )
