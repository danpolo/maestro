"""The implementer layer: briefs, pre-flight question gating, launch, sentinels.

Extracted verbatim from the reference orchestrator. Bodies are unchanged — only the
import block and the derivation of the module-level path globals differ. Brief text is
byte-identical to the reference. Behavioural surprises are catalogued in
`docs/FOUND_BUGS.md` and pinned by `tests/characterization/test_implementer.py`; none of
them is fixed here.

**M2 changed exactly one thing (plan Task 6, step 1).** A *fresh* `launch_implementer`
no longer spells an agent CLI's argv itself. It resolves a backend for the `implementer`
role through `maestro.roles` and hands a `LaunchSpec` to that driver, which owns the
three things a driver owns: build the invocation, generate `launch.py`, name the tmux
window. Everything around it is unchanged and still pinned — the signature and its `None`
return, the files a launch leaves in the workspace (`brief.txt`, `session_uuid.txt` and
`launch.py` are all still written, by this module or by the driver; a driver may add its
own sidecars beside them), the `implementer_model` journal record, the brief text, and
the order in which all of that happens.

Model selection also stays here, with one addition: `model:` in a task's ROADMAP block
selects from `IMPLEMENTER_MODELS`, which is an allowlist of *Claude* model ids, so a
model configured for the resolved backend under `roles.implementer.models` wins over it.
A Claude model id means nothing to another CLI, and a project that writes that
configuration has said what that backend runs.

Two deliberate non-changes:

* **No backend name is compared anywhere here.** Which driver runs is a name that comes
  out of configuration and goes straight into `maestro.backends.registry`; what that
  driver can do is read off `capabilities()` (the system-prompt file is offered only to a
  driver that declares `system_prompt_file`) and off its own constructor (a session-uuid
  factory is injected only into a driver that declares one, because only a driver that
  owns a uuid has anywhere to put it). `tests/test_no_name_branching.py` enforces this
  mechanically.
* **The quota-reset resume path is not delegated.** `maestro.quota` parks a task with the
  session uuid this module writes, and a resumed run must land in the *same* workspace
  with its argv in the *same* `launch.py` — the drivers write a resume launcher under
  their own name, beside the launch one, which is right for a mid-work backend switch
  (`maestro.switch`, which owns relaunching into a *new* workspace) and wrong for
  resuming in place. So the resume branch below still spells the reference CLI's
  `--resume` invocation inline, under the resolved model — which is coherent exactly as
  long as the resolved backend is the one that wrote the session, and that is the same
  unreconciled gap, not a second one. Reconciling them is left explicit rather than done
  silently; see `docs/plans/2026-08-12-m2-backends.md`.
"""
from __future__ import annotations

import inspect
import json
import subprocess
import uuid
from pathlib import Path

from maestro import roles
from maestro.backends import registry
from maestro.backends.base import LaunchSpec
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
    "haiku":    "claude-haiku-4-5",
    "sonnet":   "claude-sonnet-5",
    "sonnet5":  "claude-sonnet-5",
    "opus":     "claude-opus-5",
    "opus5":    "claude-opus-5",
}
_DEFAULT_IMPLEMENTER_MODEL = "claude-sonnet-5"


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


IMPLEMENTER_MODELS: dict[str, str] = {
    "haiku":  "claude-haiku-4-5",
    "sonnet": "claude-sonnet-5",
    "opus":   "claude-opus-5",
}
_DEFAULT_IMPLEMENTER_MODEL = "claude-sonnet-5"


# ── B12: pre-implementation question gating ──
# A task may declare a `questions:` block in its ROADMAP yaml. Each question must
# be answered by Dan (via Telegram) BEFORE an implementer is launched; the answers
# are then injected into the implementer brief. This lets info-gathering tasks
# (e.g. "which models?") auto-dispatch once Dan answers — replacing the old
# dispatch:manual workaround for that class. dispatch:manual is retained only for
# genuinely non-delegable physical work (Colab/GPU runs, prod downtime windows).

def resolve_implementer_model(task: dict) -> str:
    """Resolve the implementer model for a task, right-sizing model selection."""
    raw_model = task.get("model")
    if isinstance(raw_model, str):
        model_key = raw_model.lower().strip()
        if model_key in ("opus5", "claude-opus-5"):
            return IMPLEMENTER_MODELS["opus"]
        if model_key in ("sonnet5", "claude-sonnet-5"):
            return IMPLEMENTER_MODELS["sonnet"]
        if model_key in IMPLEMENTER_MODELS:
            return IMPLEMENTER_MODELS[model_key]
        if raw_model.strip() in IMPLEMENTER_MODELS.values():
            return raw_model.strip()
        return _DEFAULT_IMPLEMENTER_MODEL
    elif raw_model:
        # Falsy/non-string values: call .lower() to raise AttributeError as characterization expects
        raw_model.lower()

    title = str(task.get("title") or "").lower()
    desc  = str(task.get("short_desc") or "").lower()
    text  = f"{title} {desc}"
    opus_keywords = ("architecture", "refactor", "engine", "core extraction", "cutover", "redesign", "protocol")
    if any(kw in text for kw in opus_keywords):
        return IMPLEMENTER_MODELS["opus"]

    return _DEFAULT_IMPLEMENTER_MODEL


def _driver_kwargs(cls: type, **optional) -> dict:
    """The subset of `optional` that `cls`'s constructor actually declares.

    Drivers differ in what they let a caller inject — one takes a session-uuid factory
    because it owns a uuid, the other has no uuid to own — and this module must not
    decide which is which by name. Asking the constructor what it accepts keeps the
    answer with the driver, where it belongs.
    """
    try:
        accepted = inspect.signature(cls).parameters
    except (TypeError, ValueError):  # pragma: no cover - a driver with no introspectable ctor
        return {}
    return {name: value for name, value in optional.items() if name in accepted}


def _implementer_backend() -> tuple[str, dict]:
    """(backend name, its configured models) for the `implementer` role.

    One read of `project.yaml`, and the same answer `maestro.roles.backend_for` — and so
    `orchestrator._launch_backend`, which stamps the `in_flight` entry — resolves to when
    nothing has been probed: with no availability measured and nothing exhausted,
    `roles.resolve` returns the role's configured backend unchanged. Resolution degrades
    to the default backend on every malformed input rather than raising, so a typo in the
    `roles:` block cannot stop a launch.
    """
    settings = roles.role_config(roles.ROLE_IMPLEMENTER)
    return settings.backend, dict(settings.models)


def _implementer_driver(backend: str, session_uuid: str):
    """The driver for `backend`, told where this orchestrator's python and tmux live.

    `VENV_PYTHON` and `TMUX_SESSION` are the *orchestrator's*, not the driver's, so they
    are passed in rather than left to the driver's own module globals — which is also
    what keeps a launch inside a test sandbox from reaching the real repository.
    """
    cls = registry.driver_class(backend)
    return cls(**_driver_kwargs(
        cls,
        python=VENV_PYTHON,
        tmux_session=TMUX_SESSION,
        new_session_id=lambda: session_uuid,
    ))


def launch_implementer(task: dict, session_id: str, workspace: Path,
                       worktree: Path, retry_note: str = "",
                       resume: bool = False, session_uuid: str = "") -> None:
    task_id    = task["id"]
    window     = f"impl-{task_id}"
    brief_file = workspace / "brief.txt"
    if not resume or not brief_file.exists():
        brief_file.write_text(_make_brief(task, workspace, worktree, retry_note), encoding="utf-8")

    backend, backend_models = _implementer_backend()
    model_id = resolve_implementer_model(task)
    model_id = backend_models.get(backend) or model_id
    raw_key  = task.get("model")
    model_key = raw_key.lower().strip() if isinstance(raw_key, str) else (raw_key or "")
    print(f"  [model] {task_id}: model_key={model_key!r} → {model_id} "
          f"on {backend} (resume={resume})")
    append_journal("implementer_model", f"{task_id} model={model_id}")

    sess_uuid = session_uuid
    if resume and not sess_uuid:
        try:
            sess_uuid = (workspace / "session_uuid.txt").read_text(encoding="utf-8").strip()
        except Exception:
            sess_uuid = ""
    if not sess_uuid:
        sess_uuid = str(uuid.uuid4())
        resume = False

    (workspace / "session_uuid.txt").write_text(sess_uuid, encoding="utf-8")

    if resume:
        impl_log = workspace / "impl.log"
        launch_py = workspace / "launch.py"
        resume_prompt_file = workspace / "resume_prompt.txt"
        resume_prompt = (
            f"The Claude usage limit has reset. Resume working on task {task_id}: {task.get('title', '')}.\n"
            "Continue from where you left off, verify progress made so far, and complete remaining deliverables."
        )
        resume_prompt_file.write_text(resume_prompt, encoding="utf-8")
        launch_py.write_text(
            "import subprocess\nfrom pathlib import Path\n"
            f"brief = Path(r'{resume_prompt_file}').read_text()\n"
            f"log = open(r'{impl_log}', 'a', encoding='utf-8')\n"
            f"subprocess.run([\n    'claude', '-p',\n    '--model', '{model_id}',\n"
            f"    '--resume', r'{sess_uuid}',\n"
            "    brief,\n], stdout=log, stderr=subprocess.STDOUT)\n"
            "log.flush()\nlog.close()\n",
            encoding="utf-8",
        )
        launch_py.chmod(0o755)
        tmux_cmd = (f"tmux new-window -t {TMUX_SESSION} -n {window} "
                    f"'cd {worktree} && {VENV_PYTHON} {launch_py}'")
        subprocess.run(tmux_cmd, shell=True, check=True)
    else:
        driver = _implementer_driver(backend, sess_uuid)
        offers_system_prompt = driver.capabilities().system_prompt_file
        handle = driver.launch(LaunchSpec(
            task_id=task_id,
            session_id=session_id,
            model=model_id,
            brief=brief_file.read_text(encoding="utf-8"),
            workspace=workspace,
            worktree=worktree,
            system_prompt_file=SYS_PROMPT if offers_system_prompt else None,
        ))
        # A handle's window may or may not be session-qualified; the bare name is what
        # tmux was given and what `in_flight` and `_kill_tmux_window` compare against.
        window = str(handle.window or "").rsplit(":", 1)[-1] or window

    action_verb = "Resumed" if resume else "Launched"
    print(f"  ✓ {action_verb} {task_id} → tmux {TMUX_SESSION}:{window}")


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
