"""Dan's answer to an agent's spec question, recorded where every later reader finds it.

P12 pilot finding D26 (Dan, 2026-09-28): the answer is binding for the task, so it goes
into the task's ROADMAP block as a `decisions:` entry (read into every later brief), and
into an ADR, appended to the most relevant one or written as a new one. Both land in one
docs-only commit on the project's main checkout, touching only those paths, so it merges
with any task branch trivially (D18, D20).

The ROADMAP edit is textual: the block is never re-dumped, so Dan's comments, key order
and quoting survive. The result is re-parsed and must equal the old block plus the one
new entry, or nothing is written.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import yaml

from maestro import taskgraph

DECISIONS_KEY = "decisions"

#: The ADR agent's whole prompt budget for the existing ADRs' text.
ADR_TEXT_BUDGET = 40_000
ADR_TIMEOUT_SEC = 300

_TOP_KEY = re.compile(r"^[A-Za-z_][\w-]*\s*:")


class DecisionError(ValueError):
    """The decision could not be written into the task's block without loss."""


def entry(*, asked_by: str, question: str, answer: str, at: str | None = None) -> dict:
    return {
        "at": at or datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "asked_by": str(asked_by), "question": str(question), "answer": str(answer),
    }


def _block(content: str, task_id: str):
    for match in taskgraph.iter_yaml_matches(content):
        task = taskgraph.parse_block(match.group(1))
        if task is not None and str(task.get("id", "")) == str(task_id):
            return match, task
    return None, None


def add_decision(content: str, task_id: str, item: dict) -> str:
    """`content` with `item` appended to the task block's `decisions:` list."""
    match, task = _block(content, task_id)
    if match is None:
        raise DecisionError(f"no ROADMAP block for {task_id}")
    body = match.group(1)
    lines = body.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    head = next((i for i, line in enumerate(lines)
                 if re.match(rf"^{DECISIONS_KEY}\s*:\s*(#.*)?$", line.rstrip("\n"))), None)
    if head is None:
        indent = "  "
        insert_at = len(lines)
        # Trailing blank lines stay after the new key, as the block had them.
        while insert_at > 0 and not lines[insert_at - 1].strip():
            insert_at -= 1
        new = [f"{DECISIONS_KEY}:\n"]
    else:
        end = head + 1
        while end < len(lines) and not _TOP_KEY.match(lines[end]):
            end += 1
        insert_at = end
        while insert_at > head + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        items = [line for line in lines[head + 1:insert_at] if line.lstrip().startswith("- ")]
        indent = items[0][: len(items[0]) - len(items[0].lstrip())] if items else "  "
        new = []
    rendered = yaml.safe_dump([item], sort_keys=False, allow_unicode=True, width=100)
    new += [indent + line + "\n" for line in rendered.splitlines()]
    new_body = "".join(lines[:insert_at] + new + lines[insert_at:])
    after = taskgraph.parse_block(new_body)
    old = list(task.get(DECISIONS_KEY) or [])
    if not isinstance(after, dict) or after.get(DECISIONS_KEY) != old + [item] or any(
        after.get(k) != v for k, v in task.items() if k != DECISIONS_KEY
    ) or set(after) - set(task) - {DECISIONS_KEY}:
        raise DecisionError(f"{task_id}: the decision did not round-trip through the block")
    return content[: match.start(1)] + new_body + content[match.end(1):]


# ── the ADR ──────────────────────────────────────────────────────────────────────────

#: The agent call behind the ADR choice, `(prompt, cwd) -> text`. A module seam so lanes
#: answer it without a model.
def _default_adr_agent(prompt: str, cwd: Path) -> str:
    from maestro import agentcall

    return agentcall.ask("diagnoser", prompt, timeout=ADR_TIMEOUT_SEC, cwd=cwd).text or ""


_adr_agent: Callable[[str, Path], str] = _default_adr_agent


def _adr_prompt(task: dict, question: str, answer: str, adrs: list[Path], today: str) -> str:
    budget = ADR_TEXT_BUDGET // max(1, len(adrs))
    texts = []
    for path in adrs:
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > budget:
            text = text[:budget] + "\n[... truncated]"
        texts.append(f"=== docs/adr/{path.name} ===\n{text}")
    return (
        "You maintain this project's Architecture Decision Records. Dan has just decided a "
        "case the specification left open. Record it in the ADRs.\n\n"
        f"TASK: {task.get('id')}: {task.get('title', '')}\n"
        f"QUESTION: {question}\nDAN'S DECISION: {answer}\n\n"
        "If one ADR below is about the area this decision refines, append ONE paragraph to "
        "it, written in that ADR's own voice and style, stating the decision and its "
        f"reason, ending with \"(Dan, {today})\". If none fits, write a new ADR in the same "
        "house format as the others (an `# Title` line, then prose; its last paragraph "
        f"ends \"(Dan, {today})\").\n"
        "Reply with ONE JSON object and nothing else, either\n"
        '{"action": "append", "path": "docs/adr/<existing file>.md", "paragraph": "<text>"}'
        "\nor\n"
        '{"action": "new", "slug": "<kebab-case-slug>", "title": "<title>", "body": '
        '"<the whole file, starting with # Title>"}\n\n'
        "THE EXISTING ADRS:\n\n" + "\n\n".join(texts)
    )


def _json_object(text: str) -> dict | None:
    text = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _apply_adr(repo: Path, reply: dict, adrs: list[Path]) -> tuple[Path, str] | None:
    """The file the agent's reply writes, confined to `docs/adr/*.md`, or `None`."""
    folder = repo / "docs" / "adr"
    action = str(reply.get("action") or "")
    if action == "append":
        name = Path(str(reply.get("path") or "")).name
        target = folder / name
        paragraph = str(reply.get("paragraph") or "").strip()
        if not paragraph or target not in adrs:
            return None
        text = target.read_text(encoding="utf-8").rstrip("\n")
        return target, f"{text}\n\n{paragraph}\n"
    if action == "new":
        slug = re.sub(r"[^a-z0-9]+", "-", str(reply.get("slug") or "").lower()).strip("-")
        body = str(reply.get("body") or "").strip()
        title = str(reply.get("title") or "").strip()
        if not slug or not body:
            return None
        if not body.startswith("# "):
            if not title:
                return None
            body = f"# {title}\n\n{body}"
        numbers = [int(m.group(1)) for p in adrs
                   if (m := re.match(r"(\d+)-", p.name))]
        number = max(numbers, default=0) + 1
        return folder / f"{number:04d}-{slug[:60]}.md", body + "\n"
    return None


def adr_change(
    repo: Path, task: dict, question: str, answer: str, *, today: str | None = None,
    journal: Callable[[str, str], None] | None = None,
) -> tuple[Path, str] | None:
    """`(path, new_text)` for the ADR that records the decision, or `None` when skipped.

    One agent call picks the ADR and writes the text; the controller writes the file, so
    the agent can never touch anything but `docs/adr/*.md`. No `docs/adr/`, or two
    unusable replies, skips the ADR with a journal line: the ROADMAP decision is the
    binding record, the ADR its documentation.
    """
    log = journal or (lambda event, detail: None)
    repo = Path(repo)
    folder = repo / "docs" / "adr"
    task_id = str(task.get("id") or "")
    if not folder.is_dir():
        log("dan_decision_adr_skipped", f"{task_id}: the project has no docs/adr/")
        return None
    adrs = sorted(p for p in folder.glob("*.md") if p.is_file())
    stamp = today or datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    prompt = _adr_prompt(task, question, answer, adrs, stamp)
    last = ""
    for _ in range(2):
        try:
            reply = _json_object(_adr_agent(prompt, repo))
        except Exception as exc:  # noqa: BLE001 — the ADR never blocks the decision
            reply, last = None, f"{type(exc).__name__}: {exc}"
        change = _apply_adr(repo, reply, adrs) if reply is not None else None
        if change is not None:
            return change
        last = last or "no usable JSON reply"
    log("dan_decision_adr_failed", f"{task_id}: {last}"[:300])
    return None


# ── the commit ───────────────────────────────────────────────────────────────────────


def record(
    repo: Path, task_id: str, item: dict, *, roadmap: Path | None = None, adr: bool = True,
    journal: Callable[[str, str], None] | None = None,
) -> str | None:
    """Write the decision into the ROADMAP (and an ADR) and commit them; the commit sha.

    `None` when nothing was committed. Under the `[P2]` CAS the new block is recorded as
    applied only if it matched the record before, so a hand edit is never laundered into
    it (`roadmap.reduce_roadmap_deps` does the same). A ROADMAP holding someone's
    uncommitted edit is written but not committed, so that edit stays theirs to commit.
    """
    log = journal or (lambda event, detail: None)
    repo = Path(repo)
    roadmap = Path(roadmap) if roadmap is not None else repo / "docs" / "ROADMAP.md"
    content = roadmap.read_text(encoding="utf-8")
    new = add_decision(content, task_id, item)
    rel = str(roadmap.relative_to(repo))
    dirty = subprocess.run(["git", "status", "--porcelain", "--", rel], cwd=str(repo),
                           capture_output=True, text=True)
    roadmap_clean = dirty.returncode == 0 and not dirty.stdout.strip()
    record_cas = taskgraph.read_cas(repo)
    before = taskgraph.parse_document(content)
    roadmap.write_text(new, encoding="utf-8")
    if record_cas is not None:
        after = taskgraph.parse_document(new)
        data = record_cas.to_json()
        if record_cas.tasks.get(task_id) == before.block_hashes().get(task_id):
            data["tasks"][task_id] = after.block_hashes()[task_id]
        if record_cas.document_hash == before.document_hash:
            data["document_hash"] = after.document_hash
        taskgraph._atomic_write(taskgraph.cas_path(repo), json.dumps(data, indent=2) + "\n")
    paths = [rel] if roadmap_clean else []
    if not roadmap_clean:
        log("dan_decision_uncommitted",
            f"{task_id}: {rel} holds uncommitted edits; the decision is written, not committed")
    if adr:
        _, task = _block(new, task_id)
        change = adr_change(repo, task or {"id": task_id}, item["question"], item["answer"],
                            journal=log)
        if change is not None:
            path, text = change
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            paths.append(str(path.relative_to(repo)))
    if not paths:
        return None
    subprocess.run(["git", "add", "--", *paths], cwd=str(repo), capture_output=True)
    done = subprocess.run(
        ["git", "commit", "--no-verify", "-m",
         f"roadmap({task_id}): record Dan's decision ({item.get('asked_by', '')})",
         "--", *paths],
        cwd=str(repo), capture_output=True, text=True,
    )
    if done.returncode != 0:
        log("dan_decision_commit_failed", f"{task_id}: {(done.stderr or done.stdout)[:200]}")
        return None
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                          capture_output=True, text=True)
    return head.stdout.strip() or None
