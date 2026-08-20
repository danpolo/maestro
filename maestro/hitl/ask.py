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
site. Every `claude -p` invocation goes through this module's own `subprocess` reference;
the characterisation tests swap it rather than letting anything actually execute.

Behavioural surprises are catalogued in `docs/found_bugs_inbox/commands.md`; pinned by
`tests/characterization/test_ask.py`; none of them is fixed here.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from maestro.hitl.telegram import notify_telegram
from maestro.paths import Paths

_PATHS = Paths.from_env()

REPO = _PATHS.repo
ASK_MODEL = "claude-sonnet-4-6"
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

    def _run(extra_args: list[str], prompt: str) -> tuple[str, int]:
        cmd = ["claude", "-p", "--model", ASK_MODEL, *extra_args, prompt]
        try:
            r = subprocess.run(cmd, cwd=worktree, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, timeout=300)
            return (r.stdout or "").strip(), r.returncode
        except subprocess.TimeoutExpired:
            return "(Maestro timed out after 5 min answering — try /ask again.)", 124
        except Exception as exc:
            return f"(Maestro failed to answer: {exc})", 1

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
        out, rc = _run(["--resume", uuid], framed)
        if rc != 0 or not out or RESUME_MISS in out.lower():
            print(f"[maestro_ask] resume of {uuid} unusable (rc={rc}); "
                  f"falling back to the task brief.", file=sys.stderr)
            out, rc = _run([], _brief_prompt())
    else:
        out, rc = _run([], _brief_prompt())

    try:
        Path(log_path).write_text(out, encoding="utf-8")
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
