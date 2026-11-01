"""P12 pilot finding D30, part 2: an agent asks Dan to allow a command family.

Maestro's Claude agents run `--permission-mode dontAsk` on a global allow list
(`maestro.permissions`): a call not on it is denied outright, with no classifier. When the
agent needs the denied call, it proposes the broadest family it believes every project's
agents should have, writes `PERMISSION_REQUEST` into the WORKSPACE instead of DONE, and
stops. Dan allows or declines it on Telegram.

The mechanism is a file and a hold (D26/D29), not a `--permission-prompt-tool` that
blocks inside the agent: a hold spends no budget and survives an agent timeout and a
controller restart. This module is the shared vocabulary: the request's shape, its
parser, the brief text, and the rendering of rulings for the re-run's brief. The runner
screens and asks (`WorkflowRunner._screen_permission_asks`), Telegram carries the request
(`hitl.verify.DanVerificationChannel.poll_permission`), and an Allow is added to the list
(`permissions.add`).
"""
from __future__ import annotations

import json
from typing import Iterable, Mapping

#: The agent's request, written into the WORKSPACE instead of DONE.
REQUEST_FILE = "PERMISSION_REQUEST"

#: Requests one attempt may make at once.
MAX_REQUESTS_PER_ROUND = 3

#: Request rounds one workflow may make. From the next, a request fails the node as
#: `garbage_output`, so an agent cannot hold a task waiting forever.
MAX_ROUNDS_PER_WORKFLOW = 6

#: The rulings a request record ends with.
ALLOWED = "allowed"
DECLINED = "declined"
REFUSED = "refused"
ALREADY_ALLOWED = "already_allowed"

DENIAL_TEXT = "Permission to use Bash has been denied because Claude Code is running in don't ask mode."

BRIEF_RULE = (
    "PERMISSIONS: your tools run on an allow list. A call that is not on it is denied at "
    f"once, with a message like: {DENIAL_TEXT!r}. That message denies only the attempted command family, "
    "not every Bash call. Name the exact denied command and its family in your report: "
    "for example, `git mv old new` was denied in `Bash(git mv *)`; `git status` can still run. "
    "When that happens, first try an allowed "
    "way to do the same thing (the file tools, or another command). Write commands "
    "literally: a command hidden behind a variable or a wrapper (`$PY -m pytest`, "
    "`bash -c '...'`, `sh -c`, `eval`) can never match the list, so spell out the program "
    "(e.g. `.venv/bin/python -m pytest`). Never chain a directory change into a command "
    "(`cd <dir> && ...`): Claude Code denies that whatever the list says. Your shell "
    "already starts in your working directory (the WORKTREE, if you have one); use paths "
    "and `git -C <dir>`, or run `cd <dir>` as a "
    "call of its own (it persists). If the denied call is genuinely needed, ask Dan to "
    "allow it: propose the BROADEST command family you believe every project's agents "
    "should be allowed (e.g. `Bash(git rebase *)`, not the one exact command). Name the "
    "executable and operation; never propose a leading wildcard (`Bash(* --version)`). "
    "For `docker --version`, use `Bash(docker --version)`, never `Bash(docker info*)`. Commit what "
    f"you have done so far if you write code, write the file {REQUEST_FILE} into the "
    'WORKSPACE instead of DONE, as JSON: {"denied": "<the exact denied command, or '
    'Edit(/path) for a file>", "family": "<the rule, e.g. Bash(git rebase *)>", "reason": '
    '"<why agents need it>"} (or {"requests": [<up to '
    f"{MAX_REQUESTS_PER_ROUND}>]}}), and stop. You are re-run once Dan rules. Refused "
    "without asking: sudo and other privilege escalation, force pushes, reading or writing "
    ".env files, tokens or credentials, editing the permission lists, rm -rf outside the "
    "task tree, dropping or bulk-deleting tables, and families that would allow any command "
    "(`Bash(*)`, `Bash(bash *)`, `Bash(env *)`). Never re-ask a family Dan declined."
)


def parse(raw) -> tuple[list[dict], str]:
    """`(requests, error)`: the valid requests of a round, or why the round is unusable.

    `raw` is the file's text (or decoded JSON): one request object, or `{"requests": [...]}`.
    All or nothing, like a question round: one malformed request refuses the round.
    """
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            return [], f"not JSON: {exc}"
    if isinstance(raw, dict) and "requests" in raw:
        raw = raw["requests"]
    elif isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return [], "no requests"
    if len(raw) > MAX_REQUESTS_PER_ROUND:
        return [], f"{len(raw)} requests; at most {MAX_REQUESTS_PER_ROUND} at once"
    out: list[dict] = []
    for n, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            return [], f"request {n}: not an object"
        request = {k: str(item.get(k) or "").strip() for k in ("denied", "family", "reason")}
        missing = [k for k, v in request.items() if not v]
        if missing:
            return [], f"request {n}: no {', '.join(missing)}"
        out.append({k: v[:2000] for k, v in request.items()})
    return out, ""


def render_rulings(rulings: Iterable[Mapping] | None) -> str:
    """The "PERMISSION RULINGS" brief section for this workflow, or "" when there are none."""
    lines = []
    for r in rulings or ():
        family = str(r.get("family") or "")
        ruling = str(r.get("ruling") or "")
        note = str(r.get("note") or "").strip()
        if ruling == ALLOWED:
            lines.append(f"- Dan allowed {family}: it is on the allow list now; run it.")
        elif ruling == ALREADY_ALLOWED:
            lines.append(f"- {family} is already on the allow list; run it.")
        elif ruling == DECLINED:
            lines.append(f"- Dan declined {family}" + (f": {note}" if note else "")
                         + "; work around it, or write FAILED if the task cannot be done "
                         "without it. Do not ask for it again.")
        elif ruling == REFUSED:
            lines.append(f"- {family} was refused without asking Dan: {note}. Do not ask "
                         "for it again.")
    if not lines:
        return ""
    return "PERMISSION RULINGS (on your earlier requests in this task):\n" + "\n".join(lines)


def summary(requests: Iterable[Mapping]) -> str:
    return "; ".join(f"{r.get('family')} for {str(r.get('denied'))[:80]!r}" for r in requests)
