"""P12 pilot finding D29: a deny-list hit is caught in the gate and asks Dan.

DuetFlow 06 run 6 passed three review rounds and Dan's real-world test, then the merge
guard refused it on the app's own `DELETE FROM duet_state`. Two gaps: the deny-list ran
only at merge, after everything that costs Dan time, and a legitimate hit had no way
through. Dan's decision (2026-09-28):

- the gate runs the same check (`merge.deny_list_hits`) on the candidate, before any
  review round or V-DAN;
- on a hit an agent examines it in context and recommends allow (with a reason) or send
  back (with what to change); Dan gets both on Telegram with Approve / Disapprove buttons;
- Approve lets that exact hit merge (`merge.deny_hit_key`, bound to the task's workflow):
  the merge guard skips only approved keys. Disapprove sends the hit back to the
  implementer as a blocking finding, through the gate's normal rework path.

Nothing here weakens a pattern or lets the recommendation stand in for Dan's tap. This
module is the gate's half, run in the worker; the controller's half (asking, holding the
gate, recording rulings) is in `WorkflowRunner`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Mapping

#: The recommendation values an agent may give.
ALLOW = "allow"
SEND_BACK = "send_back"
RECOMMENDATIONS = (ALLOW, SEND_BACK)
RECOMMEND_TIMEOUT_SEC = 300
#: Bounds on what one recommendation prompt carries.
HUNK_LINES = 30
PROJECT_YAML_CHARS = 6000
NOTES_CHARS = 3000


def screen(tree: Path, config: Mapping, *, proj=None) -> dict:
    """Which of the tree's deny-list hits Dan approved, disapproved, or has not ruled on.

    `config` is the controller's `deny_list` gate input: `target` (the branch the merge
    lands on) and `rulings` (`{key: {ruling, ...}}` for this task's workflow). Returns
    `{hits, approved, disapproved, unruled, diff, refusal}`; `refusal` is set when the diff
    cannot be computed or a pattern is invalid (the merge guard refuses those anyway).
    """
    from maestro import merge

    target = str(config.get("target") or "main")
    base = _git(tree, "merge-base", "HEAD", target)
    if base.returncode != 0:
        return _empty(f"could not find the merge base of HEAD and {target}")
    span = [base.stdout.strip(), "HEAD"]
    text = _git(tree, "diff", "--no-color", "--no-ext-diff", *span)
    names = _git(tree, "diff", "--name-only", "--no-renames", "-z", *span)
    if text.returncode != 0 or names.returncode != 0:
        return _empty(f"could not diff the task tree against {target}")
    changed = [p for p in names.stdout.split("\0") if p]
    hits, refusal = merge.deny_list_hits(text.stdout, changed, proj)
    rulings = config.get("rulings") or {}
    out = {**_empty(refusal), "hits": hits, "diff": text.stdout}
    for hit in hits:
        ruling = rulings.get(hit["key"]) if isinstance(rulings, Mapping) else None
        verdict = str((ruling or {}).get("ruling") or "")
        if verdict == "approved":
            out["approved"].append(hit["key"])
        elif verdict == "disapproved":
            out["disapproved"].append({**hit, "ruling": dict(ruling)})
        else:
            out["unruled"].append(hit)
    return out


def _empty(refusal: str = "") -> dict:
    return {"hits": [], "approved": [], "disapproved": [], "unruled": [], "diff": "",
            "refusal": refusal}


def _git(tree: Path, *args: str):
    return subprocess.run(["git", "-C", str(tree), *args], capture_output=True, text=True)


def finding(hit: Mapping) -> str:
    """A disapproved hit as the blocking finding the implementer's rework must fix."""
    ruling = hit.get("ruling") or {}
    change = str(ruling.get("what_to_change") or "").strip()
    note = str(ruling.get("note") or "").strip()
    text = (
        f"Deny-list hit disapproved by Dan: {hit.get('file')} adds "
        f"{str(hit.get('line') or hit.get('match') or '')!r}, which matches the deny-list "
        f"pattern {hit.get('pattern')!r}. Change the code so the diff no longer matches it."
    )
    if change:
        text += f" What to change: {change}"
    if note:
        text += f" Dan added: {note}"
    return text


# ── the recommending agent ───────────────────────────────────────────────────────────


def _default_agent(prompt: str, cwd: Path) -> str:
    from maestro import agentcall

    # The diagnoser's model: stronger than the implementer whose line it judges.
    return agentcall.ask(
        "diagnoser", prompt, timeout=RECOMMEND_TIMEOUT_SEC, cwd=cwd,
    ).text or ""


#: `(prompt, cwd) -> text`. A module seam so lanes answer it without a model.
_agent: Callable[[str, Path], str] = _default_agent


def hunk_for(diff_text: str, hit: Mapping) -> str:
    """The diff hunk that adds `hit`'s line, bounded to `HUNK_LINES` around it."""
    if hit.get("kind") == "path":
        return ""
    target = str(hit.get("file") or "")
    needle = str(hit.get("line") or "").split("\n")[0]
    current, header, hunk = "", False, []
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            header, hunk = True, []
            continue
        if header:
            if line.startswith("+++ "):
                current = line[6:] if line.startswith("+++ b/") else line[4:]
            elif line.startswith("@@"):
                header, hunk = False, [line]
            continue
        if line.startswith("@@"):
            hunk = [line]
            continue
        hunk.append(line)
        if current == target and line.startswith("+") and needle and needle in line[1:]:
            idx = len(hunk) - 1
            lo = max(0, idx - HUNK_LINES // 2)
            return "\n".join(hunk[lo: idx + HUNK_LINES // 2])
    return ""


def _prompt(task: Mapping, hits: list[Mapping], diff_text: str, project_yaml: str) -> str:
    blocks = []
    for hit in hits:
        context = hunk_for(diff_text, hit)
        blocks.append(
            f"--- hit {hit['key']} ---\nfile: {hit['file']}\npattern: {hit['pattern']}\n"
            f"kind: {hit['kind']}\nadded line: {hit.get('line')}\n"
            + (f"diff context:\n{context}\n" if context else "")
        )
    notes = str(task.get("notes") or "")[:NOTES_CHARS]
    return (
        "You examine deny-list hits for Dan, the project's owner. The change below was "
        "written by an autonomous implementer and has passed its checks, but a line it "
        "adds (or a file it touches) matches the project's deny-list, so it cannot merge "
        "unless Dan approves that exact hit. Dan decides with a button; your job is to "
        "recommend, not to decide.\n\n"
        "The deny-list exists to stop destructive or credential-leaking changes: commands "
        "run with sudo, force pushes, dropping or bulk-deleting database tables (especially "
        "production data), secrets written into code, and edits to secret or production "
        "files. A hit can still be legitimate, for example an application managing its "
        "own local state table the way its design says it should.\n\n"
        "For each hit, read the file in this checkout and the task below, and recommend "
        "`allow` (the line is the intended, safe behaviour; say why) or `send_back` (it is "
        "unsafe or avoidable; say what the implementer should do instead). Do not edit "
        "anything.\n\n"
        f"TASK: {task.get('id')}: {task.get('title', '')}\n"
        + (f"TASK NOTES:\n{notes}\n" if notes else "")
        + f"\nPROJECT.YAML (its deny-list comments say why each pattern exists):\n"
        f"{project_yaml[:PROJECT_YAML_CHARS]}\n\nTHE HITS:\n\n" + "\n".join(blocks)
        + "\nReply with ONE JSON object and nothing else:\n"
        '{"hits": [{"key": "<hit key>", "recommendation": "allow" | "send_back", '
        '"reason": "<one or two sentences>", "what_to_change": "<for send_back: what to '
        'do instead; empty for allow>"}]}'
    )


def _parse(text: str, keys: set[str]) -> dict[str, dict] | None:
    from maestro.docs.decisions import _json_object

    data = _json_object(text)
    items = data.get("hits") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    out: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        key, rec = str(item.get("key") or ""), str(item.get("recommendation") or "")
        if key in keys and rec in RECOMMENDATIONS and str(item.get("reason") or "").strip():
            out[key] = {
                "recommendation": rec, "reason": str(item["reason"]).strip()[:1000],
                "what_to_change": str(item.get("what_to_change") or "").strip()[:1000],
            }
    return out if set(out) == keys else None


from maestro.model_policy import with_task_models


@with_task_models
def recommend(
    tree: Path, task: Mapping, hits: list[Mapping], diff_text: str, *, project_yaml: str = "",
) -> dict[str, dict]:
    """`{key: {recommendation, reason, what_to_change}}` for every hit, from one agent call.

    One retry. When both replies are unusable each hit carries `recommendation: ""` and
    the reason why, and Dan is asked without one: the recommendation informs his tap and
    never replaces it, so its absence does not stop the ask.
    """
    keys = {str(h["key"]) for h in hits}
    prompt = _prompt(task, list(hits), diff_text, project_yaml)
    last = "no usable JSON reply"
    for _ in range(2):
        try:
            parsed = _parse(_agent(prompt, Path(tree)), keys)
        except Exception as exc:  # noqa: BLE001 — Dan is asked either way
            parsed, last = None, f"{type(exc).__name__}: {exc}"
        if parsed is not None:
            return parsed
    reason = f"the recommending agent gave no usable answer ({last}); judge the hit yourself"
    return {key: {"recommendation": "", "reason": reason[:500], "what_to_change": ""}
            for key in keys}


def project_yaml_text() -> str:
    """The project's `project.yaml`, verbatim (its comments explain the deny-list)."""
    from maestro import config

    try:
        return Path(config.PROJECT_YAML).read_text(encoding="utf-8")
    except OSError:
        return ""


def asks(hits: list[Mapping], recommendations: Mapping[str, Mapping]) -> list[dict]:
    """The hits to put to Dan, each with its recommendation."""
    return [{"hit": dict(hit), "recommendation": dict(recommendations.get(hit["key"]) or {})}
            for hit in hits]


def summary(hits: list[Mapping]) -> str:
    return "; ".join(f"{h.get('file')}: {str(h.get('match'))[:60]!r}" for h in hits)
