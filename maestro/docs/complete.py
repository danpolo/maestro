"""Graduate a completed task out of ROADMAP.md, deterministically and in full.

Extracted verbatim from the reference `scripts/mark_task_complete.py` (215 lines) — a
standalone, `sys.argv`-driven CLI script (`main()`), not a function living inside
`orchestrator_run.py`, same shape as `maestro.verifications` (R3), `maestro.status`
(R13) and `maestro.watchdog` (M4b). Function bodies are unchanged; only the import
block and the derivation of the module-level path globals differ — they come from
`maestro.paths.Paths.from_env()` instead of `Path(__file__).resolve().parent.parent`,
exactly as every other extracted module does.

The one deviation from a pure verbatim port: `main()`'s three shell-outs
(`check_verifications.py`, `gen_dependency_map.py`, `check_roadmap_consistency.py`) are
now-deleted-at-M5 scripts, so — per `docs/plans/2026-08-18-m4c-superseded-sidecars.md`
§2b — they are rewired to their M4c in-process equivalents: `maestro.verifications.
run_cli`, `maestro.docs.depmap.run_cli`, `maestro.docs.consistency.run_cli`. Each
`run_cli` is the same `sys.argv`-substituting, stdout-capturing wrapper `maestro.
verifications.run_cli`/`maestro.status.run_cli` already established — the sibling
module's own `main()` is untouched, still reading `sys.argv` and printing. Only the
captured-stdout half of the old `(returncode, stdout, stderr)` subprocess contract
survives as a return value; `stderr` is dropped because none of the three call sites
ever received anything on it in practice — `check_verifications.py`/`gen_dependency_
map.py` print exclusively to stdout, and `check_roadmap_consistency.py`'s stderr prints
now happen as a direct side effect of running its `main()` in-process, landing on this
process's real stderr exactly where the old `sys.stderr.write(g.stderr)` line used to
copy them. Behavioural surprises are catalogued in `docs/FOUND_BUGS.md`; none of them is
fixed here.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from maestro import verifications
from maestro.docs import consistency as doc_consistency
from maestro.docs import depmap as doc_depmap
from maestro.paths import Paths

_PATHS = Paths.from_env()

REPO            = _PATHS.repo
ROADMAP_FILE    = REPO / "docs" / "ROADMAP.md"
VENV_PYTHON     = REPO / ".venv" / "bin" / "python3"
COMPLETED_TASKS = REPO / ".orchestrator" / "completed_tasks.json"
AUTONOMOUS_DOC  = REPO / "docs" / "AUTONOMOUS_SYSTEM.md"
PROJECT_DOC     = REPO / "docs" / "PROJECT.md"

GRADUATION_HEADER = "## Graduated-task markers (auto-maintained)"
GRADUATION_NOTE = (
    "_Appended by `scripts/mark_task_complete.py` on graduation. The marker comment is "
    "what `scripts/check_roadmap_consistency.py` looks for; feel free to enrich the prose "
    "or move the entry into the relevant section above — just keep the marker._"
)


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _python() -> str:
    return str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable


def inferred_doc(task_id: str) -> Path:
    """B-series tasks are orchestrator work → AUTONOMOUS_SYSTEM.md; everything else
    is RAG-product work → PROJECT.md."""
    return AUTONOMOUS_DOC if str(task_id).upper().startswith("B") else PROJECT_DOC


def remove_task_section(content: str, task_id: str) -> tuple[str, str | None]:
    """Return (new_content, title_or_None). Removes the whole task section: the
    `### <id> …` heading, the fenced yaml block, and any trailing prose, up to (but
    not including) the next `### ` heading / `---` rule / EOF."""
    for m in re.finditer(r"```yaml\n(.*?)```", content, re.DOTALL):
        try:
            task = yaml.safe_load(m.group(1))
        except yaml.YAMLError:
            continue
        if not isinstance(task, dict) or str(task.get("id", "")) != str(task_id):
            continue

        title = task.get("title", task_id)

        # Section start: the nearest "### " heading line preceding this yaml block.
        head = content.rfind("\n### ", 0, m.start())
        start = head + 1 if head != -1 else m.start()

        # Section end: first "### " heading or "---" rule after the yaml block, else EOF.
        rest = content[m.end():]
        end_candidates = []
        for pat in (r"\n### ", r"\n---\n"):
            mm = re.search(pat, rest)
            if mm:
                end_candidates.append(m.end() + mm.start() + 1)  # keep the boundary line
        end = min(end_candidates) if end_candidates else len(content)

        new_content = content[:start] + content[end:]
        # Collapse any 3+ blank-line runs left behind into a single blank line.
        new_content = re.sub(r"\n{3,}", "\n\n", new_content)
        return new_content, title
    return content, None


def append_completed(task_id: str, title: str, graduated_to: str) -> None:
    """Append/refresh the registry entry, deduped by id, recording graduated_to."""
    existing: list[dict] = []
    if COMPLETED_TASKS.exists():
        try:
            existing = json.loads(COMPLETED_TASKS.read_text())
        except Exception:
            existing = []

    entry = next((r for r in existing if str(r.get("id")) == str(task_id)), None)
    if entry is None:
        existing.append({
            "id": task_id,
            "title": title,
            "completed_at": now_iso(),
            "graduated_to": graduated_to,
        })
    else:  # dedup: refresh in place, preserve original completed_at
        entry["title"] = title or entry.get("title", task_id)
        entry.setdefault("completed_at", now_iso())
        entry["graduated_to"] = graduated_to

    COMPLETED_TASKS.parent.mkdir(parents=True, exist_ok=True)
    COMPLETED_TASKS.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def ensure_graduation_marker(task_id: str, title: str, doc: Path) -> bool:
    """Append a stub + `<!-- graduated: ID -->` marker to *doc* if not already present
    in either living doc. Returns True if it wrote a marker."""
    marker = f"<!-- graduated: {task_id} -->"
    for d in (AUTONOMOUS_DOC, PROJECT_DOC):
        if d.exists() and marker in d.read_text(encoding="utf-8"):
            return False  # already graduated into a doc (idempotent)

    text = doc.read_text(encoding="utf-8") if doc.exists() else ""
    stub = f"- **{task_id}** — {title} (graduated {now_iso()[:10]}) {marker}"

    if GRADUATION_HEADER in text:
        body = text.rstrip("\n") + "\n" + stub + "\n"
    else:
        prefix = (text.rstrip("\n") + "\n\n") if text.strip() else ""
        body = f"{prefix}{GRADUATION_HEADER}\n\n{GRADUATION_NOTE}\n\n{stub}\n"

    doc.write_text(body, encoding="utf-8")
    return True


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: mark_task_complete.py <task_id> [--no-verify]")
        return 1

    no_verify = "--no-verify" in sys.argv[1:]
    args = [a for a in sys.argv[1:] if a != "--no-verify"]
    task_id = args[0]

    # Enforce the deterministic auto-gate BEFORE any ROADMAP mutation, so a failing gate
    # leaves the task in ROADMAP (refuse-to-graduate). The orchestrator path passes
    # --no-verify because it already gated the worktree; manual graduations gate here.
    if not no_verify:
        gate_rc, gate_out = verifications.run_cli([task_id, str(REPO), "--auto-only"])
        if gate_rc != 0:
            sys.stdout.write(gate_out)
            print(f"[mark] Refusing to graduate {task_id}: auto-verification gate failed. "
                  f"Fix the gate or pass --no-verify to override.", file=sys.stderr)
            return 1

    doc = inferred_doc(task_id)
    graduated_to = str(doc.relative_to(REPO))

    content = ROADMAP_FILE.read_text(encoding="utf-8")
    new_content, title = remove_task_section(content, task_id)

    if title is None:
        print(f"[mark] Task {task_id} not found in ROADMAP.md — may already be removed.")
        title = task_id
    else:
        ROADMAP_FILE.write_text(new_content, encoding="utf-8")
        print(f"[mark] Removed {task_id} ({title!r}) section from ROADMAP.md")

    append_completed(task_id, title, graduated_to)
    print(f"[mark] Registry updated: {COMPLETED_TASKS} (graduated_to={graduated_to})")

    if ensure_graduation_marker(task_id, title, doc):
        print(f"[mark] Wrote graduation marker for {task_id} into {graduated_to}")
    else:
        print(f"[mark] Graduation marker for {task_id} already present — left as-is")

    dep_rc, dep_out = doc_depmap.run_cli([])
    if dep_rc == 0:
        print("[mark] Dependency map regenerated.")
    else:
        print(f"[mark] dep-map regen FAILED: {dep_out[:300]}")
        return 1

    guard_rc, guard_out = doc_consistency.run_cli([])
    sys.stdout.write(guard_out)
    if guard_rc != 0:
        print(f"[mark] CONSISTENCY GUARD FAILED for {task_id} — graduation incomplete.", file=sys.stderr)
        return guard_rc

    print(f"[mark] {task_id} graduated cleanly; consistency guard green.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
