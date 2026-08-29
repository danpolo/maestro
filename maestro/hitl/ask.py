"""Maestro `/ask` handler — answer Dan's question about a parked manual task with the
full context of the session that prepared it.

Extracted verbatim from the reference `scripts/maestro_ask.py`: a standalone,
`sys.argv`-driven CLI script (`main()`), not a function living inside
`orchestrator_run.py`, so it becomes its own maestro module rather than folding into
`maestro.hitl.commands`. Function bodies (`main()` and its nested `_run`/`_brief_prompt`
helpers) are unchanged — only the import block and the derivation of the module-level
path globals differ (`REPO` comes from `maestro.paths.Paths.from_env()` instead of
`Path(__file__).resolve().parent.parent`, exactly as every other extracted module does).

**One deliberate infra substitution, not a behaviour fix**: the reference's local
`notify(msg)` helper shells out to `scripts/notify_telegram.sh` (deleted at M5, R4
already replaced every other call site); this module has no `notify()`/`NOTIFY` of its
own and instead calls `maestro.hitl.telegram.notify_telegram(msg)` wherever the reference
called `notify(msg)`.

Runs in a dedicated tmux window (spawned by `maestro.hitl.commands._handle_ask`) so it
never blocks Maestro's control loop; invokable as `python -m maestro.hitl.ask
<request-path>`, replacing the reference's `scripts/maestro_ask.py <request-path>` call
site. Since 2026-08-26 the agent call goes through `maestro.agentcall` rather than naming
a CLI, so `/ask` answers on whichever backend the project configured; the resume token is
handed to the driver as `CompletionSpec.resume_id` instead of being spelled `--resume`,
which is what lets a driver without native resume fall back to a fresh session on its own
rather than being handed a flag it cannot honour.

Behavioural surprises are catalogued in `docs/found_bugs_inbox/commands.md`; pinned by
`tests/characterization/test_ask.py`; none of them is fixed here.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from maestro import agentcall
from maestro.hitl.telegram import notify_telegram
from maestro.paths import Paths
from maestro.roles import ROLE_IMPLEMENTER

_PATHS = Paths.from_env()

REPO = _PATHS.repo
# `ASK_MODEL` used to live here, pinning `claude-sonnet-4-6`. `/ask` speaks as the
# implementer that prepared the task, so the model now comes from `roles.implementer`'s
# per-backend table; a claude id kept here would name a model most backends do not have.
#: The answer is a Telegram message, so five minutes is already generous.
ASK_TIMEOUT = 300
LIMIT_HINT = ("hit your limit", "usage limit", "rate limit", "too many requests")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("request", help="path to the JSON request file")
    req = json.loads(Path(ap.parse_args().request).read_text(encoding="utf-8"))

    task = req.get("task", "?")
    dan_id = str(req.get("dan_id", "") or "")
    uuid = req.get("uuid", "") or ""
    question = req.get("question", "").strip()
    action = req.get("action", "") or "(action text unavailable)"
    worktree = req.get("worktree") or str(REPO)
    log_path = req.get("log") or "/tmp/maestro_ask.log"
    # The driver writes the run's combined output here, and `_run` reads it back on a
    # failed call; the final answer overwrites it below, so Dan's log still holds the
    # message he was sent rather than the raw transcript.
    ask_log = Path(log_path)

    framed = (
        f"You are Maestro's implementer that prepared task {task}. Dan is performing the "
        f"manual action you handed him and has a question or hit a problem.\n\n"
        f"MANUAL ACTION HE WAS GIVEN:\n{action}\n\n"
        f"DAN'S MESSAGE:\n{question}\n\n"
        f"Help him resolve it. If the problem is a bug in what you prepared (e.g. the "
        f"notebook or script you wrote), state exactly what is wrong and the precise fix. "
        f"Be concrete and concise — this is a Telegram message. Advice only: do NOT modify "
        f"files or push anything."
    )

    def _run(resume_id: str, prompt: str) -> tuple[str, int]:
        """One bounded ask of the implementer role. Returns `(output, returncode)`.

        The output is deliberately whatever the agent *printed*, not only a successful
        answer: both callers below read it for signals that only appear on a failed run —
        the resume-miss notice, and the usage-limit wording this module reports to Dan in
        its own words. `Completion.text` is empty on a non-zero exit by design, so the
        run's combined stdout+stderr is recovered from the log the driver writes, which is
        the same file `/ask` already keeps for Dan.
        """
        answer = agentcall.ask(
            ROLE_IMPLEMENTER, prompt,
            timeout=ASK_TIMEOUT, cwd=Path(worktree),
            resume_id=resume_id, log_file=ask_log,
        )
        if answer.timed_out:
            return "(Maestro timed out after 5 min answering — try /ask again.)", 124
        if answer.text:
            return answer.text, answer.returncode
        try:
            return ask_log.read_text(encoding="utf-8", errors="replace").strip(), answer.returncode
        except OSError as exc:
            return f"(Maestro failed to answer: {exc})", answer.returncode or 1

    def _brief_prompt() -> str:
        brief = req.get("brief", "")
        brief_txt = ""
        if brief and Path(brief).exists():
            try:
                brief_txt = Path(brief).read_text(encoding="utf-8")[:6000]
            except Exception:
                brief_txt = ""
        if brief_txt:
            return f"CONTEXT — the task brief you worked from:\n{brief_txt}\n\n" + framed
        return framed

    # `claude --resume` exits 0 even when the session is missing — it just prints
    # "No conversation found with session ID …". That happens whenever the build
    # worktree was cleaned up (the session is keyed to that now-gone cwd). Detect
    # it and fall back to a fresh session seeded with the task brief, so Dan gets
    # a real answer instead of the raw error posted as if it were one.
    RESUME_MISS = "no conversation found with session id"
    if uuid:
        out, rc = _run(uuid, framed)
        if rc != 0 or not out or RESUME_MISS in out.lower():
            print(f"[maestro_ask] resume of {uuid} unusable (rc={rc}); "
                  f"falling back to the task brief.", file=sys.stderr)
            out, rc = _run("", _brief_prompt())
    else:
        out, rc = _run("", _brief_prompt())

    try:
        ask_log.write_text(out, encoding="utf-8")
    except Exception:
        pass

    # If the answer attempt itself hit the usage limit, say so plainly.
    if out and any(h in out.lower() for h in LIMIT_HINT) and len(out) < 200:
        notify_telegram(f"⏳ Maestro couldn't answer about {task} — Claude usage limit. "
               f"Try /ask {dan_id or task} again after it resets.")
        return 0

    answer = out[:3500] or "(no answer produced)"
    again = dan_id or task
    notify_telegram(
        f"💬 *Maestro* — re: `{task}` (your question)\n\n{answer}\n\n"
        f"✅ Worked → /approve {dan_id}   ·   ❓ Ask again → /ask {again} <message>"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
