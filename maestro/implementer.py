"""The implementer layer: briefs, pre-flight question gating, launch, sentinels.

Extracted verbatim from the reference orchestrator. Bodies are unchanged — only the
import block and the derivation of the module-level path globals differ. Brief text is
byte-identical to the reference. Behavioural surprises are catalogued in
`docs/FOUND_BUGS.md` and pinned by `tests/characterization/test_implementer.py`; none of
them is fixed here.
"""
from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

from maestro.paths import Paths
from maestro.state import append_journal
from maestro.worktree import TMUX_SESSION

_PATHS = Paths.from_env()

REPO                  = _PATHS.repo
WORKSPACES            = _PATHS.workspaces
QUESTIONS_DIR         = REPO / ".orchestrator" / "questions"
SYS_PROMPT            = REPO / "orchestrator" / "profiles" / "implementer_sys.md"
VENV_PYTHON           = REPO / ".venv" / "bin" / "python3"
SEND_DANREQ           = REPO / "scripts" / "send_dan_request.py"

# B8: implementer model allowlist — tasks may opt into a cheaper model by
# setting `model: haiku` in their ROADMAP.md yaml block. Unknown/missing values
# fall safe to Sonnet (never crash, never silently use a wrong model).
IMPLEMENTER_MODELS: dict[str, str] = {
    "haiku":  "claude-haiku-4-5",
    "sonnet": "claude-sonnet-4-6",
    "opus":   "claude-opus-4-8",
}
_DEFAULT_IMPLEMENTER_MODEL = "claude-sonnet-4-6"


# ── B12: pre-implementation question gating ──
# A task may declare a `questions:` block in its ROADMAP yaml. Each question must
# be answered by Dan (via Telegram) BEFORE an implementer is launched; the answers
# are then injected into the implementer brief. This lets info-gathering tasks
# (e.g. "which models?") auto-dispatch once Dan answers — replacing the old
# dispatch:manual workaround for that class. dispatch:manual is retained only for
# genuinely non-delegable physical work (Colab/GPU runs, prod downtime windows).

def _task_questions(task: dict) -> list[dict]:
    """Normalize a task's `questions:` block → list of {id, prompt, options|None}.

    Returns [] when absent/malformed; silently drops entries missing id or prompt.
    """
    raw = task.get("questions")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for q in raw:
        if not isinstance(q, dict):
            continue
        qid    = str(q.get("id") or "").strip()
        prompt = str(q.get("prompt") or "").strip()
        if not qid or not prompt:
            continue
        opts = q.get("options")
        opts = [str(o).strip() for o in opts] if isinstance(opts, list) and opts else None
        out.append({"id": qid, "prompt": prompt, "options": opts})
    return out


def _question_req_id(task_id: str, qid: str) -> str:
    """Deterministic Dan-request id for a task question (stable across cycles)."""
    return f"q-{task_id}-{qid}"


def _answer_choice(req_id: str) -> str | None:
    """Dan's answer text for a parked question, or None if unanswered.

    Handles both the {"choice": ...} dict written by telegram_bot.py and a
    bare-string fallback.
    """
    ans_path = QUESTIONS_DIR / f"{req_id}.answer"
    if not ans_path.exists():
        return None
    raw = ans_path.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw  # bare-string answer
    if isinstance(data, dict):
        choice = data.get("choice")
        return str(choice).strip() if choice is not None else None
    return str(data).strip()


def _ask_question(task_id: str, q: dict) -> None:
    """Park a single task question as a Dan-request (idempotent).

    Uses a deterministic id so each question is asked exactly once; no-op if its
    request file already exists. send_dan_request.py enforces the global pending
    cap, so a capped ask simply retries on a later cycle.
    """
    req_id = _question_req_id(task_id, q["id"])
    if (QUESTIONS_DIR / f"{req_id}.json").exists():
        return  # already asked — waiting for the answer
    cmd = [str(VENV_PYTHON), str(SEND_DANREQ),
           "--type", "manual-task", "--id", req_id,
           "--question", f"[{task_id} · {q['id']}] {q['prompt']}"]
    if q.get("options"):
        cmd += ["--options", ",".join(q["options"])]
    res = subprocess.run(cmd, cwd=str(REPO), capture_output=True)
    # send_dan_request.py parks <req_id>.json and returns 0 only when the global
    # pending cap had room; it refuses (non-zero, no file) when saturated. Journal
    # ONLY on a successful park so a saturated cap retries silently next cycle
    # instead of spamming the journal every poll. (getattr keeps the unit-test
    # subprocess stub, which returns None, working.)
    if getattr(res, "returncode", 0) == 0 and (QUESTIONS_DIR / f"{req_id}.json").exists():
        append_journal("questions_asked", f"{task_id} {q['id']} req={req_id}")


def _questions_ready(task: dict) -> bool:
    """True iff every declared question for the task has an answer on disk.

    Otherwise asks the FIRST unanswered question (sequentially, idempotently) and
    returns False so the launch loop parks the task for this cycle. Sequential
    asking keeps at most one free-text question pending per task, which avoids the
    single-slot free-text matcher in telegram_bot.py answering the wrong one.
    """
    task_id = str(task["id"])
    for q in _task_questions(task):
        if _answer_choice(_question_req_id(task_id, q["id"])) is None:
            _ask_question(task_id, q)
            return False
    return True


def _answers_section(task: dict) -> str:
    """Render answered question/answer pairs for the implementer brief."""
    questions = _task_questions(task)
    if not questions:
        return ""
    task_id = str(task["id"])
    lines = []
    for q in questions:
        ans = _answer_choice(_question_req_id(task_id, q["id"])) or "(no answer recorded)"
        lines.append(f"  {q['id']}: {q['prompt']}\n       → Dan's answer: {ans}")
    return (
        "\n\nANSWERS FROM DAN (gathered before this task was dispatched — treat these "
        "as authoritative inputs):\n" + "\n".join(lines)
    )


# ── Brief generation ──

def _make_brief(task: dict, workspace: Path, worktree: Path, retry_note: str = "") -> str:
    # B14 auto-prep: a dispatch:manual task gets a PREP brief — do all automatable
    # prep, never the human step, and emit the single dan_action.
    if task.get("dispatch") == "manual":
        return _make_prep_brief(task, workspace, worktree, retry_note)
    task_id = task["id"]
    title   = task.get("title", task_id)
    note    = f"\n\nNOTE FROM ORCHESTRATOR: {retry_note}" if retry_note else ""

    verifs      = task.get("verifications") or []
    verif_lines = []
    for v in verifs:
        vid   = v.get("id", "?")
        kind  = v.get("kind", "manual")
        check = v.get("check", "")
        if kind == "auto":
            verif_lines.append(
                f"  {vid} [auto]: {check}\n"
                f"       cmd: {v.get('cmd','')}\n"
                f"       expect: {v.get('expect','')!r}"
            )
        else:
            verif_lines.append(f"  {vid} [manual]: {check}")
    verif_section = ""
    if verif_lines:
        verif_section = (
            "\n\nVERIFICATIONS (fill result.json 'verifications' — gate rejects DONE otherwise):\n"
            + "\n".join(verif_lines)
            + '\n\nresult.json "verifications" format:\n'
            + '  {"V1": {"done": true, "proof": "<specific evidence>"}, ...}\n'
            + "auto items: gate re-runs cmd itself; your proof is informational only.\n"
            + "manual items: done=true AND proof must be specific (not 'tested manually').\n"
        )

    schema = (
        '{"task_id":"…","status":"DONE|FAILED|BLOCKED|NEEDS-REPLAN",'
        '"files_changed":[],"worktree":"…","tests":{"pass":true,"summary":"…"},'
        '"eval":{"smoke":"pending"},'
        '"verifications":{"V1":{"done":true,"proof":"<specific evidence>"}},'
        '"NEEDS":null,"notes":"…","summary":"≤5 lines","ts":"ISO"}'
    )
    needs_dan_note = (
        "\n\nHITL NOTE: This task is mode=needs-dan. The orchestrator will pause after "
        "your DONE and ask Dan to approve before merging. Proceed normally — Dan reviews "
        "your result, not the intermediate steps."
    ) if task.get("mode") == "needs-dan" else ""
    resumable_note = (
        "\n\nRESUMABLE (multi-day / quota-bound): this task usually can't finish in one "
        "sitting. Make as much forward progress as the external daily quota allows (e.g. run "
        "the generator until it reports the quota is exhausted), commit your DATA changes, "
        "then write DONE normally. The orchestrator re-runs the coverage gate: if you are not "
        "yet at 100%, it merges your partial progress and AUTO-RE-QUEUES you for the next "
        "sitting — you do NOT need to finish everything now, and an incomplete gate is NOT a "
        "failure. Only touch data/ files; do not change code."
    ) if task.get("resumable") else ""

    return (
        f"You are a Sonnet implementer. Your task is {task_id}: {title}.\n\n"
        f"WORKTREE (your git sandbox — work only within this directory):\n  {worktree}\n\n"
        f"WORKSPACE (write result.json + DONE/FAILED sentinel here):\n  {workspace}\n\n"
        f"TASK DESCRIPTION:\n  {task.get('short_desc', '')}\n\n"
        f"VENV Python: {VENV_PYTHON}\n\n"
        f"DELIVERABLES:\n"
        f"1. Implement the task within the worktree.\n"
        f"2. Run tests if applicable.\n"
        f"3. Commit your changes inside {worktree}:\n"
        f"     git add -A && git commit -m 'feat({task_id}): <one-line summary>'\n"
        f"   (Skip only if there are genuinely no file changes.)\n"
        f"4. Write {workspace}/result.json:\n   {schema}\n"
        f"5. Write {workspace}/DONE (content: DONE) as the final step,\n"
        f"   or {workspace}/FAILED (reason text) if it failed.\n\n"
        f"GUARDRAILS:\n"
        f"- Stay inside {worktree} for all file writes.\n"
        f"- Do NOT push to git. Do NOT restart the bot. Do NOT modify .env.\n"
        f"- Do NOT write to {REPO} directly (only read tooling from it).\n"
        f"- NEVER edit docs/ROADMAP.md status fields — task completion is owned by the orchestrator.\n"
        f"- Read docs/ROADMAP.md and docs/CLAUDE_TASK_CONTEXT.md for full context.\n"
        f"{verif_section}{note}{needs_dan_note}{resumable_note}{_answers_section(task)}"
    )


def _make_prep_brief(task: dict, workspace: Path, worktree: Path, retry_note: str = "") -> str:
    """B14 auto-prep brief for a dispatch:manual task.

    The implementer does ALL automatable prep and emits the single `dan_action`; it must
    NOT perform the human step (GPU run, prod downtime, hardware/credential decision).
    """
    task_id = task["id"]
    title   = task.get("title", task_id)
    note    = f"\n\nNOTE FROM ORCHESTRATOR: {retry_note}" if retry_note else ""
    known_action = task.get("dan_action")
    known_post   = task.get("post_action_cmd")
    hint = ""
    if known_action:
        hint += f"\n\nROADMAP already suggests dan_action: {known_action!r} — reuse/refine it."
    if known_post:
        hint += f"\nROADMAP already suggests post_action_cmd: {known_post!r}."
    schema = (
        '{"task_id":"%s","status":"DONE|FAILED|BLOCKED",'
        '"dan_action":"<ONE action: a URL to open + \'Run all\', OR a single '
        'bash scripts/x.sh command — never a multi-step list>",'
        '"post_action_cmd":"<a single shell cmd to finalize AFTER Dan acts, or null>",'
        '"files_changed":[],"summary":"<=5 lines","ts":"ISO"}' % task_id
    )
    return (
        f"You are a Sonnet PREP implementer for {task_id}: {title}.\n\n"
        f"This is a dispatch:manual task: a human (Dan) must perform exactly ONE step that an "
        f"agent cannot — a Colab/GPU run, an approved prod-downtime window, or a "
        f"hardware/credential decision. YOUR JOB is to do EVERYTHING ELSE so Dan's total "
        f"involvement is that single action.\n\n"
        f"WORKTREE (your git sandbox — work only here):\n  {worktree}\n\n"
        f"WORKSPACE (write result.json + DONE/FAILED here):\n  {workspace}\n\n"
        f"VENV Python: {VENV_PYTHON}\n\n"
        f"TASK DESCRIPTION:\n  {task.get('short_desc', '')}\n"
        f"  Read docs/ROADMAP.md ({task_id} block) + docs/CLAUDE_TASK_CONTEXT.md for full context.{hint}\n\n"
        f"DO (all automatable prep):\n"
        f"- Write/finish any scripts, export/prepare data, generate a Colab notebook and UPLOAD it to "
        f"Dan's Drive (the rclone remote `gdrive:` works headlessly), run any migration that does NOT "
        f"need the downtime/human step.\n"
        f"- If you generated a Colab/Jupyter notebook, it MUST be CHECKPOINT-RESUMABLE across daily "
        f"sessions: Dan runs on FREE Colab, which cuts the GPU mid-job each day, so the notebook is "
        f"re-run over several days and must continue from where it stopped — never restart from "
        f"epoch 0 / row 0. Persist train_state.json (epoch, step, optimizer/scheduler, best metric) "
        f"+ checkpoint to Drive after each epoch (or every N rows for encode loops) and resume from "
        f"the latest on startup. Pattern: colab/phase8b_train.ipynb / scripts/backfill_media.py. "
        f"See docs/CLAUDE_TASK_CONTEXT.md 'Colab free-tier conventions'.\n"
        f"- If you generated a Colab/Jupyter notebook, SYNTAX-GATE it before uploading: run "
        f"`{VENV_PYTHON} scripts/check_notebook.py <notebook.ipynb>` and fix until it exits 0 "
        f"(it byte-compiles every code cell). Don't hand Dan a notebook that fails this.\n"
        f"- Commit your prep changes in the worktree:\n"
        f"     git add -A && git commit -m 'prep({task_id}): <one-line summary>'\n"
        f"  (Skip only if prep produced no committable files — e.g. a pure Drive upload.)\n"
        f"- Decide the SINGLE action Dan must take; write it as result.json `dan_action` (ONE line).\n"
        f"- If a finalize step can be automated AFTER Dan acts (e.g. importing Colab output into the "
        f"DB), put that one shell command in `post_action_cmd` — the orchestrator runs it when Dan "
        f"approves, then runs the task's verification gate to graduate.\n\n"
        f"DO NOT:\n"
        f"- Perform the human step yourself (do NOT start the GPU run, do NOT restart the prod bot, "
        f"do NOT take prod downtime, do NOT spend money/credits that need Dan's ok).\n"
        f"- Push to git, modify .env, or edit docs/ROADMAP.md (the orchestrator owns ROADMAP + merge).\n"
        f"- Write outside {worktree}.\n\n"
        f"DELIVERABLES:\n"
        f"1. result.json at {workspace}/result.json:\n   {schema}\n"
        f"2. {workspace}/DONE (content: DONE) as the final step, or {workspace}/FAILED (reason) on failure.\n"
        f"{note}"
    )


def launch_implementer(task: dict, session_id: str, workspace: Path,
                       worktree: Path, retry_note: str = "") -> None:
    task_id    = task["id"]
    window     = f"impl-{task_id}"
    brief_file = workspace / "brief.txt"
    brief_file.write_text(_make_brief(task, workspace, worktree, retry_note), encoding="utf-8")
    sys_prompt_line = (f"    '--system-prompt-file', r'{SYS_PROMPT}',\n"
                       if SYS_PROMPT.exists() else "")
    # B8: per-task model right-sizing. Tasks may set `model: haiku/sonnet/opus`
    # in their ROADMAP.md yaml block; unknown/absent values fall safe to Sonnet.
    model_key    = (task.get("model") or "").lower().strip()
    model_id     = IMPLEMENTER_MODELS.get(model_key, _DEFAULT_IMPLEMENTER_MODEL)
    print(f"  [model] {task_id}: model_key={model_key!r} → {model_id}")
    append_journal("implementer_model", f"{task_id} model={model_id}")
    # F: a fixed, resumable Claude session id per launch. Pass it to `claude -p
    # --session-id` so a manual task's prep session can later be resumed by /ask
    # (`claude -p --resume <uuid>`). Persisted to session_uuid.txt for park_for_dan.
    sess_uuid = str(uuid.uuid4())
    (workspace / "session_uuid.txt").write_text(sess_uuid, encoding="utf-8")
    # A: capture the implementer's stdout+stderr to impl.log. Without this, an
    # implementer that dies on launch (e.g. `claude -p` exits printing "You've hit
    # your limit") leaves NO trace — the tmux window just closes — and reconcile
    # can only report "window gone, no sentinel". impl.log is the reactive net that
    # B (usage-limit backoff) and C/D (diagnosis) read after the fact.
    impl_log = workspace / "impl.log"
    launch_py = workspace / "launch.py"
    launch_py.write_text(
        "import subprocess\nfrom pathlib import Path\n"
        f"brief = Path(r'{brief_file}').read_text()\n"
        f"log = open(r'{impl_log}', 'w', encoding='utf-8')\n"
        f"subprocess.run([\n    'claude', '-p',\n    '--model', '{model_id}',\n"
        f"    '--session-id', r'{sess_uuid}',\n"
        f"{sys_prompt_line}    brief,\n], stdout=log, stderr=subprocess.STDOUT)\n"
        "log.flush()\nlog.close()\n",
        encoding="utf-8",
    )
    launch_py.chmod(0o755)
    tmux_cmd = (f"tmux new-window -t {TMUX_SESSION} -n {window} "
                f"'cd {worktree} && {VENV_PYTHON} {launch_py}'")
    subprocess.run(tmux_cmd, shell=True, check=True)
    print(f"  ✓ Launched {task_id} → tmux {TMUX_SESSION}:{window}")


def _synthesize_sentinel(workspace: Path) -> str:
    """An implementer wrote result.json but left no DONE/FAILED sentinel. Synthesize one
    from result.json's status so the main loop converges instead of re-adopting a zombie
    forever. Returns 'DONE' or 'FAILED'. status==DONE writes DONE (the verification gate
    still runs downstream — a resumable coverage miss then routes to handle_incomplete);
    any other / unreadable status writes FAILED."""
    status = ""
    try:
        status = str(json.loads((workspace / "result.json").read_text())
                     .get("status", "")).strip().upper()
    except Exception:
        status = ""
    if status == "DONE":
        (workspace / "DONE").write_text("DONE (synthesized: implementer left no sentinel)")
        return "DONE"
    reason = status or "no terminal status"
    (workspace / "FAILED").write_text(
        f"synthesized FAILED: implementer left no sentinel (result.json status={reason})")
    return "FAILED"


def _tail_text(path: Path, n_lines: int = 25) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace")
                         .splitlines()[-n_lines:])
    except Exception:
        return ""


def _latest_impl_tail(task_id: str, n: int = 25) -> str:
    """Tail of the most recent implementer impl.log for a task (Fix A capture)."""
    try:
        cands = [p for p in WORKSPACES.glob(f"impl-{task_id}-*") if p.is_dir()]
        cands.sort(key=lambda p: p.stat().st_mtime)
    except Exception:
        cands = []
    for ws in reversed(cands):
        t = _tail_text(ws / "impl.log", n)
        if t:
            return t
    return ""
