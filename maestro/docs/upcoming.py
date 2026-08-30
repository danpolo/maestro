"""Generate docs/UPCOMING.md — a human-facing, instructive view of pending work.

Extracted verbatim from the reference `scripts/gen_upcoming.py` (661 lines) per M4c batch
2, Wave B — the largest of the four "3-doc engine" scripts. Function bodies are unchanged
from the reference — only the import block, the derivation of the module-level path
globals, and a few project-specific spots differ:

* the reference's module-level `import gen_dependency_map as gdm` (a same-directory
  sibling script, self-located via `__file__`) becomes `from maestro.docs import depmap as
  gdm` — `maestro.docs.depmap` (M4c batch 2, Wave B) already exposes `parse_tasks` and
  `_registry_completed`, the two names this module calls on it;
* the reference's function-local `import prepared_actions` inside `_prepared_action`
  (another self-locating sibling script) becomes a module-level `from maestro import
  prep_actions as prepared_actions`, called with an explicit `path=PREPARED_ACTIONS`
  instead of relying on `maestro.prep_actions`'s own self-located global. `maestro.prep_
  actions.get_action` already accepts an injectable `path=` for exactly this reason. This
  is what lets `tests/characterization/test_upcoming.py`'s own `box` fixture — which
  rebases only `up`'s and `up.gdm`'s own `Path` globals by reflection, not every loaded
  `maestro.*` module the way the shared `sandbox` fixture in `conftest.py` does — sandbox
  the prepared-actions sidecar correctly for the maestro subject too;
* `PROJECT_NAME` (`project.yaml` `project.name` → `REPO_ROOT.name` fallback, the same
  derivation `maestro/watchdog.py` uses) replaces the reference's hardcoded project name.

`_opus_complete` is the one function that reaches an agent (see `refresh_explanations`/
`_refresh_prompt`, which build and consume its prompt/response). It was extracted verbatim,
shelling out to `claude -p` by name; since 2026-08-26 it asks the **judge role** through
`maestro.agentcall`, so `--refresh-explanations` works on whichever backend the project
configured instead of being a silent no-op anywhere else. Tests stub `agentcall.ask`, and
`tests/conftest.py` makes reaching a real CLI impossible from any direction.

Behavioural surprises are catalogued in `docs/FOUND_BUGS.md` (entries #182–#184, shared
with `maestro.docs.depmap`'s characterisation) and pinned by
`tests/characterization/test_upcoming.py`; none of them is fixed here.

**2026-08-30 (pre-pilot decoupling):** `GLOSSARY` used to be 18 retrieval-jargon terms
(dense/sparse/RRF/Recall@5/...) hardcoded for the reference project — reference vocabulary
in a project-agnostic package, and dead weight for any other project's tasks. It is now
`_project_glossary()`, parsed fresh from `docs/PROJECT.md`'s "## Glossary" section — the
same section the `maestro-setup` skill's Step 2 interview already collects into that file.
`_refresh_prompt`'s hardcoded "Hebrew RAG Telegram bot" / "dense BGE-M3 ... RRF ...
cross-encoder" pipeline description is gone with it: no characterisation test covered that
function's text (only `refresh_explanations`'s cache-writing behaviour, which does not
inspect the prompt), so nothing needed rewriting there. `detect_terms`, `_refresh_prompt`
and `render`'s central-glossary section all call `_project_glossary()` instead of reading a
frozen module-level list, so a project's glossary can change without a process restart —
and, not incidentally, so the characterisation `box` fixture's per-test `docs/PROJECT.md`
is what each test actually exercises.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from maestro import agentcall
from maestro import prep_actions as prepared_actions
from maestro.config import load_project_yaml
from maestro.docs import depmap as gdm
from maestro.paths import Paths
from maestro.roles import ROLE_JUDGE

_PATHS = Paths.from_env()
_PROJECT_YAML = load_project_yaml()

REPO_ROOT          = _PATHS.repo
ROADMAP            = REPO_ROOT / "docs" / "ROADMAP.md"
UPCOMING           = REPO_ROOT / "docs" / "UPCOMING.md"
#: The human-facing companion doc `_project_glossary` reads its "## Glossary" section
#: from — the same file the `maestro-setup` skill's interview writes into, and the same
#: path `docs/ROADMAP.md`/`docs/UPCOMING.md` above are hardcoded to rather than read back
#: out of `project.yaml`'s (currently unread) `docs.project` key.
PROJECT_DOC        = REPO_ROOT / "docs" / "PROJECT.md"
STATE_JSON         = _PATHS.state
COMPLETED_TASKS    = REPO_ROOT / ".orchestrator" / "completed_tasks.json"
EXPLANATIONS_CACHE = REPO_ROOT / ".orchestrator" / "upcoming_explanations.json"

#: Not a reference global — see the module docstring's `_prepared_action` bullet. Exists
#: so `up`'s own `Path` globals (which the test's `box` fixture rebases by reflection)
#: cover the prepared-actions sidecar too.
PREPARED_ACTIONS   = REPO_ROOT / ".orchestrator" / "prepared_actions.json"

#: `project.yaml` `project.name`, falling back to the repo directory name — the same
#: derivation `maestro/watchdog.py`'s `PROJECT_NAME` uses. The reference hardcodes its own
#: project's name in the Opus prompt built by `_refresh_prompt`; here it is substituted in.
PROJECT_NAME = (_PROJECT_YAML.get("project") or {}).get("name") or REPO_ROOT.name

# `JUDGE_MODEL` used to live here, pinning `claude-opus-4-8` for the refresh judgment. It
# is gone rather than kept as a documented default: the model comes from `project.yaml`'s
# `roles.judge` table for whichever backend answers, and a claude id sitting in a
# project-agnostic module is the exact shape of the bug this call site had — a literal
# that looks authoritative and is wrong everywhere but one backend.
REFRESH_TIMEOUT = 240


# ── Glossary: read fresh from docs/PROJECT.md's "## Glossary" section (the maestro-setup
# skill's interview writes it there — DESIGN.md §2 D6), not hardcoded. Each bullet
# `- **Term** — definition` becomes one entry, detected per-task by aliases derived
# automatically from the term text, and rendered both inline ("terms in this task") and in
# the central glossary. ──

_GLOSSARY_HEADING = re.compile(r"^##\s+Glossary\s*$", re.MULTILINE)
_NEXT_HEADING = re.compile(r"^##\s+", re.MULTILINE)
# The separator is one or more dash-like characters: an em dash (the render/template
# convention), an en dash, or plain ASCII hyphen(s) — `+` so a `--` substitute (common
# when an editor doesn't autocorrect to an em dash) is consumed whole rather than leaving
# a stray leading "-" in the captured definition.
_GLOSSARY_BULLET = re.compile(r"^\s*-\s*\*\*(.+?)\*\*\s*[—–-]+\s*(\S.*)$")


def _term_aliases(term: str) -> list[str]:
    """Match strings for `term`, derived automatically rather than hand-curated.

    A human writing `docs/PROJECT.md`'s Glossary tends to spell an abbreviation out once,
    e.g. "RRF (Reciprocal Rank Fusion)" — so the whole term, the part before the
    parenthetical, and the part inside it all become aliases. A term with no parenthetical
    (e.g. "MaxSim") just gets itself, lowercased. The parenthetical itself excludes further
    parens (`[^()]*`), so a second, unrelated parenthetical later in the term (rare, but
    "Term (A) (B)") is left out of `head` and `paren` don't straddle it — `head` simply
    keeps whatever preceded the last one, `paren` is just that last group.
    """
    low = term.strip().lower()
    aliases = [low]
    m = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", term.strip())
    if m:
        head, paren = m.group(1).strip().lower(), m.group(2).strip().lower()
        aliases += [head, paren]
    return [a for a in dict.fromkeys(aliases) if a]


def _project_glossary() -> list[dict]:
    """This project's glossary, parsed fresh from `PROJECT_DOC` every call.

    Absent file, absent section, or a section with no recognisable bullets all return
    `[]` — a project that hasn't filled this in yet (the common case on day one) gets no
    glossary rather than a crash or a placeholder entry, the same "unevaluated is honest"
    choice `maestro.metrics` makes for a missing `gate.primary_metric`.
    """
    try:
        text = PROJECT_DOC.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    heading = _GLOSSARY_HEADING.search(text)
    if not heading:
        return []
    rest = text[heading.end():]
    end = _NEXT_HEADING.search(rest)
    section = rest[:end.start()] if end else rest

    entries = []
    for line in section.splitlines():
        bm = _GLOSSARY_BULLET.match(line)
        if not bm:
            continue
        term, definition = bm.group(1).strip(), bm.group(2).strip()
        if term and definition:
            entries.append({"term": term, "aliases": _term_aliases(term),
                            "definition": definition})
    return entries


# ── ROADMAP detail-link parsing ──

def _detail_paths(roadmap_text: str) -> dict[str, str]:
    r"""Map each task id -> the `Detail: \`retired/tasks/PhaseXX.md\`` path that
    follows its yaml block in ROADMAP.md (the deep archived brief)."""
    out: dict[str, str] = {}
    for seg in re.split(r"(?=```yaml)", roadmap_text):
        block = re.search(r"```yaml\s*\n(.*?)```", seg, re.DOTALL)
        if not block:
            continue
        idm = re.search(r"^id:\s*(\S+)", block.group(1), re.MULTILINE)
        if not idm:
            continue
        dm = re.search(r"Detail:\s*`([^`]+)`", seg)
        if dm:
            out[idm.group(1).strip()] = dm.group(1).strip()
    return out


# ── Live status (from the execution SoT) ──

def _state() -> dict:
    try:
        return json.loads(STATE_JSON.read_text())
    except Exception:
        return {}


def _prepared_action(task_id: str) -> str:
    """The single human action Maestro has prepared for this task, if any.
    Prefers the live prepared_actions sidecar, falls back to ROADMAP `dan_action`.

    Reference calls a lazily self-locating sibling module (`import prepared_actions`);
    maestro's equivalent is `maestro.prep_actions` (already imported above as
    `prepared_actions`), which takes an explicit `path=` instead of owning its own
    self-located global — so it is passed `PREPARED_ACTIONS`, this module's own path
    global, to be sandboxed the same way every other `up`-owned Path is."""
    try:
        entry = prepared_actions.get_action(task_id, path=PREPARED_ACTIONS)
        if entry and entry.get("action"):
            return str(entry["action"]).strip()
    except Exception:
        pass
    return ""


def task_status(task: dict, state: dict, completed: set[str]) -> dict:
    """Return {label, detail} describing in plain English why a task can or
    cannot run right now. Precedence mirrors gen_dependency_map._node_class:
    running > awaiting-you > held > blocked > ready-needs-you > ready."""
    tid = task["id"]

    parked = state.get("waiting_on_dan") or {}
    is_parked = any(
        isinstance(p, dict) and p.get("task_id") == tid for p in parked.values()
    )

    in_flight = {
        row.get("task_id")
        for row in (state.get("in_flight") or [])
        if isinstance(row, dict)
    }

    if is_parked:
        action = _prepared_action(tid) or str(task.get("dan_action") or "").strip()
        return {
            "label": "🟡 Awaiting you",
            "detail": "Maestro has prepared this and is waiting on your one action (below).",
            "action": action,
        }
    if tid in in_flight:
        return {
            "label": "🟢 Running now",
            "detail": "Maestro has an implementer working on this right now.",
            "action": "",
        }
    if task.get("hold"):
        return {
            "label": "⏸️ Held",
            "detail": "Pinned out (`hold: true`) — Maestro will not launch it until the hold is cleared.",
            "action": "",
        }
    unmet = [d for d in task.get("deps", []) if d not in completed]
    if unmet:
        return {
            "label": "⛔ Blocked",
            "detail": f"Waiting on {', '.join(unmet)} to finish first.",
            "action": "",
        }
    if task.get("mode") == "needs-dan" or task.get("dispatch") == "manual":
        action = _prepared_action(tid) or str(task.get("dan_action") or "").strip()
        return {
            "label": "🟠 Ready — needs your action",
            "detail": "Deps are met; this one needs a manual step from you to start.",
            "action": action,
        }
    return {
        "label": "🔵 Ready to run",
        "detail": "Deps are met; Maestro can run this autonomously.",
        "action": "",
    }


# ── Instructive prose: cache (Opus-authored) + deterministic fallback ──

def _task_fingerprint(task: dict) -> str:
    """Stable hash over the fields that drive the prose, so a cache entry
    auto-invalidates when the task's meaning changes."""
    blob = "|".join(str(task.get(k, "")) for k in ("title", "short_desc", "est_time", "deps"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def load_cache() -> dict:
    try:
        return json.loads(EXPLANATIONS_CACHE.read_text())
    except Exception:
        return {}


def get_prose(task: dict, cache: dict) -> dict:
    """Return {what_why, pipeline_fit, source} for a task. Uses the Opus-authored
    cache entry when fresh, else a deterministic fallback from `short_desc` (never
    blocks, never calls the network)."""
    tid = task["id"]
    entry = cache.get(tid)
    if isinstance(entry, dict) and entry.get("hash") == _task_fingerprint(task):
        return {
            "what_why": entry.get("what_why", "").strip(),
            "pipeline_fit": entry.get("pipeline_fit", "").strip(),
            "source": f"explained by {entry.get('model', 'Opus')}",
        }
    # Deterministic fallback: lean on the task's own one-line short_desc.
    return {
        "what_why": str(task.get("short_desc", "")).strip(),
        "pipeline_fit": "",
        "source": "auto (run `--refresh-explanations` for a fuller explanation)",
    }


def detect_terms(text: str, glossary: list[dict] | None = None) -> list[dict]:
    """Glossary entries whose aliases appear in `text` (case-insensitive),
    preserving glossary order.

    `glossary` lets a caller that iterates many texts (`render`'s per-task loop) fetch
    `_project_glossary()` once and pass it through, rather than re-reading and re-parsing
    `docs/PROJECT.md` for every task. Defaults to a fresh fetch so every other caller (and
    every existing test) keeps working unchanged.
    """
    if glossary is None:
        glossary = _project_glossary()
    low = text.lower()
    return [g for g in glossary if any(a in low for a in g["aliases"])]


# ── Opus refresh path (--refresh-explanations) ──

def _opus_complete(prompt: str) -> str:
    """One bounded judge call. Returns the answer or "" — every caller is best-effort.

    Asks the **judge role** rather than naming a CLI, so explanation refresh works on
    whichever backend the project configured. It used to shell out to one binary directly,
    which meant `--refresh-explanations` was a silent no-op anywhere else.

    `JUDGE_MODEL` is no longer passed: the model belongs to the role's per-backend table in
    `project.yaml`, and pinning one here would have handed a claude model id to whatever
    backend actually answered.
    """
    answer = agentcall.ask(ROLE_JUDGE, prompt, cwd=REPO_ROOT, timeout=REFRESH_TIMEOUT)
    if not answer.text:
        detail = "timed out" if answer.timed_out else f"rc={answer.returncode}"
        print(f"  [judge] no answer ({detail})", file=sys.stderr)
    return answer.text


def _extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of a model response (tolerates ```json fences)."""
    fenced = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    m = re.search(r"\{.*\}", candidate, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def refresh_explanations(tasks: list[dict], detail_paths: dict[str, str],
                         force: bool = False) -> int:
    """Have Opus (re)write the human-facing prose for every task whose cache entry
    is missing or stale, and persist to the cache. Returns the number refreshed.
    Best-effort: a failed call leaves the prior/fallback prose in place."""
    cache = load_cache()
    refreshed = 0
    for task in tasks:
        tid = task["id"]
        if not force:
            entry = cache.get(tid)
            if isinstance(entry, dict) and entry.get("hash") == _task_fingerprint(task):
                continue  # fresh — skip the Opus call
        detail = ""
        dpath = detail_paths.get(tid)
        if dpath:
            fp = REPO_ROOT / dpath
            if fp.exists():
                detail = fp.read_text(encoding="utf-8")[:4000]
        prompt = _refresh_prompt(task, detail)
        out = _opus_complete(prompt)
        parsed = _extract_json(out) if out else None
        if not parsed or not parsed.get("what_why"):
            print(f"  [opus] {tid}: no usable response — keeping existing/fallback prose", file=sys.stderr)
            continue
        cache[tid] = {
            "hash": _task_fingerprint(task),
            "what_why": str(parsed.get("what_why", "")).strip(),
            "pipeline_fit": str(parsed.get("pipeline_fit", "")).strip(),
            # The model that actually answered, not a constant: with the backend
            # configurable, stamping a fixed id would mislabel every cached entry on
            # any project that is not running the default.
            "model": agentcall.resolve_call(ROLE_JUDGE)[1] or "unknown",
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
        }
        refreshed += 1
        print(f"  [opus] {tid}: refreshed", file=sys.stderr)
    if refreshed:
        EXPLANATIONS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        EXPLANATIONS_CACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False) + "\n")
    return refreshed


def _refresh_prompt(task: dict, detail: str) -> str:
    glossary = _project_glossary()
    glossary_line = (
        "These jargon terms are defined elsewhere, so you may USE them without "
        f"redefining: {', '.join(g['term'] for g in glossary)}.\n\n"
    ) if glossary else ""
    return (
        f"You are writing for Dan, the non-specialist owner of {PROJECT_NAME}. He wants "
        "to UNDERSTAND each upcoming engineering task, not just see its title. Explain in "
        "plain English, concretely, tied to this project (see docs/PROJECT.md for what it "
        "does and what good looks like here).\n\n"
        "Write two things for the task below:\n"
        "1. what_why — 2–4 sentences: what this task actually does and why it should help "
        "this project. No marketing, no hedging.\n"
        "2. pipeline_fit — 2–3 sentences: where this slots into the project's overall "
        "system (see docs/PROJECT.md). Be specific about what it feeds or changes.\n\n"
        f"{glossary_line}"
        f"TASK (from docs/ROADMAP.md):\n```yaml\n{json.dumps(task, ensure_ascii=False, indent=2)}\n```\n\n"
        f"DEEPER BRIEF (excerpt):\n{detail or '(none)'}\n\n"
        "Respond with ONLY a JSON object: "
        '{"what_why": "...", "pipeline_fit": "..."}'
    )


# ── Render ──

def _fmt_deps(task: dict, completed: set[str], pending_ids: set[str]) -> str:
    deps = task.get("deps") or []
    if not deps:
        return "none — ready as soon as Maestro reaches it"
    parts = []
    for d in deps:
        if d in completed:
            parts.append(f"{d} ✅ (done)")
        elif d in pending_ids:
            parts.append(f"{d} ⏳ (still pending)")
        else:
            parts.append(d)
    return ", ".join(parts)


def render(tasks: list[dict], state: dict, completed: set[str],
           detail_paths: dict[str, str], cache: dict) -> str:
    pending = [t for t in tasks if t["id"] not in completed]
    pending_ids = {t["id"] for t in pending}
    # Fetched once: every task below calls detect_terms, and re-reading/re-parsing
    # docs/PROJECT.md per task (instead of once per render() call) was pure overhead.
    glossary = _project_glossary()

    lines: list[str] = []
    lines.append("# Upcoming Work — Plain-English Guide")
    lines.append("")
    lines.append(
        "**Auto-generated from `docs/ROADMAP.md` + `.orchestrator/state.json` by "
        "`scripts/gen_upcoming.py` — do not hand-edit.** This is the human-facing "
        "companion to the machine `docs/ROADMAP.md` and the `docs/dependency_map.md` "
        "graph: it explains *what* each pending task does, *why* it should help, and "
        "*what (if anything) you need to do*. Jargon is explained inline and in the "
        "[Glossary](#glossary) at the bottom."
    )
    lines.append("")
    lines.append(
        f"_{len(pending)} pending task(s). Completed work lives in `docs/PROJECT.md`, "
        "not here._"
    )
    lines.append("")

    # At-a-glance table.
    lines.append("## At a glance")
    lines.append("")
    lines.append("| Task | What | Status | Effort | Your move |")
    lines.append("|---|---|---|---|---|")
    for t in pending:
        st = task_status(t, state, completed)
        needs_you = "✋ yes" if st["label"].startswith(("🟡", "🟠")) else "—"
        title = str(t.get("title", t["id"])).replace("|", "\\|")
        effort = str(t.get("est_time", "?")).replace("|", "\\|")
        lines.append(
            f"| **{t['id']}** | {title} | {st['label']} | {effort} | {needs_you} |"
        )
    lines.append("")

    # Per-task detail.
    for t in pending:
        tid = t["id"]
        st = task_status(t, state, completed)
        prose = get_prose(t, cache)
        title = str(t.get("title", tid))

        lines.append(f"## {tid} — {title}")
        lines.append("")
        lines.append(f"**Status:** {st['label']} — {st['detail']}")
        if st.get("action"):
            lines.append("")
            lines.append(f"> **👉 Your move:** {st['action']}")
        lines.append("")
        lines.append(f"**Effort:** {t.get('est_time', '?')}  ")
        lines.append(f"**Depends on:** {_fmt_deps(t, completed, pending_ids)}  ")
        lines.append(f"**How it runs:** {_mode_phrase(t)}")
        lines.append("")
        lines.append(f"**What & why.** {prose['what_why'] or '_(no description)_'}")
        if prose["pipeline_fit"]:
            lines.append("")
            lines.append(f"**Where it fits.** {prose['pipeline_fit']}")
        lines.append("")

        # Inline "terms in this task" box.
        searchable = " ".join([
            title, str(t.get("short_desc", "")),
            prose["what_why"], prose["pipeline_fit"],
        ])
        terms = detect_terms(searchable, glossary)
        if terms:
            lines.append("<details><summary>📖 Terms in this task</summary>")
            lines.append("")
            for g in terms:
                lines.append(f"- **{g['term']}** — {g['definition']}")
            lines.append("")
            lines.append("</details>")
            lines.append("")

        dpath = detail_paths.get(tid)
        src_note = f"_{prose['source']}._"
        if dpath:
            lines.append(f"_Deep technical brief: `{dpath}`. {prose['source']}._")
        else:
            lines.append(src_note)
        lines.append("")

    # Central glossary — omitted entirely for a project that hasn't filled in
    # docs/PROJECT.md's Glossary section yet, rather than an empty heading.
    if glossary:
        lines.append("## Glossary")
        lines.append("")
        lines.append("_Every jargon term used above, defined once._")
        lines.append("")
        for g in glossary:
            lines.append(f"- **{g['term']}** — {g['definition']}")
        lines.append("")

    return "\n".join(lines)


def _mode_phrase(task: dict) -> str:
    if task.get("dispatch") == "manual":
        return "you perform it (Maestro preps everything, then hands you one action)"
    if task.get("mode") == "needs-dan":
        return "Maestro runs the prep, pauses for your manual step, then finishes"
    if task.get("kind") == "script":
        return "deterministic script — Maestro runs it with no LLM"
    return "Maestro runs it end-to-end autonomously"


# ── CLI ──

def _build() -> tuple[list[dict], str]:
    if not ROADMAP.exists():
        raise SystemExit(f"missing {ROADMAP}")
    roadmap_text = ROADMAP.read_text()
    tasks = gdm.parse_tasks(roadmap_text)
    if not tasks:
        raise SystemExit("no task blocks found in docs/ROADMAP.md")
    state = _state()
    completed = gdm._registry_completed()
    completed |= {t["id"] for t in tasks if t.get("status") == "complete"}
    detail_paths = _detail_paths(roadmap_text)
    cache = load_cache()
    rendered = render(tasks, state, completed, detail_paths, cache)
    return tasks, rendered


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if docs/UPCOMING.md is stale; do not write")
    ap.add_argument("--refresh-explanations", action="store_true",
                    help="have Opus (re)write stale/missing per-task prose into the cache first")
    ap.add_argument("--force-refresh", action="store_true",
                    help="with --refresh-explanations, rewrite every task even if cached")
    args = ap.parse_args()

    if args.refresh_explanations:
        roadmap_text = ROADMAP.read_text()
        tasks = gdm.parse_tasks(roadmap_text)
        completed = gdm._registry_completed() | {t["id"] for t in tasks if t.get("status") == "complete"}
        pending = [t for t in tasks if t["id"] not in completed]
        n = refresh_explanations(pending, _detail_paths(roadmap_text), force=args.force_refresh)
        _backend, _model = agentcall.resolve_call(ROLE_JUDGE)
        print(f"Refreshed {n} task explanation(s) via {_model or _backend}.")

    tasks, rendered = _build()

    if args.check:
        current = UPCOMING.read_text() if UPCOMING.exists() else ""
        if current.strip() != rendered.strip():
            print("UPCOMING.md is STALE — run scripts/gen_upcoming.py", file=sys.stderr)
            return 1
        print("UPCOMING.md is up to date.")
        return 0

    UPCOMING.write_text(rendered)
    pending = [t for t in tasks if t["id"] not in (gdm._registry_completed() |
               {t2["id"] for t2 in tasks if t2.get("status") == "complete"})]
    print(f"Wrote {UPCOMING.relative_to(REPO_ROOT)} from {len(pending)} pending task(s).")
    print("Tasks:", ", ".join(t["id"] for t in pending))
    return 0


def run_cli(argv: list[str]) -> tuple[int, str]:
    """In-process equivalent of `python gen_upcoming.py <argv...>`, the same shape as
    `maestro.verifications.run_cli`/`maestro.status.run_cli`/`maestro.docs.depmap.
    run_cli`: substitutes `sys.argv` for the duration of the call — `main()` above is
    left untouched — and captures whatever it prints instead of letting it hit the real
    stdout/stderr. `main()`'s `--check`-failure line goes to stderr, and
    `maestro.docs.consistency.check_upcoming_no_drift` needs that line in the problem it
    reports — the same reason its own subprocess-era call combined `res.stdout +
    res.stderr` — so both streams are redirected into one buffer here, matching
    `maestro.docs.depmap.run_cli`/`maestro.docs.roadmap._call_main`'s combined capture.
    Returns (exit_code, captured_output)."""
    old_argv = sys.argv
    buf = io.StringIO()
    try:
        sys.argv = ["gen_upcoming.py", *argv]
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = main()
    finally:
        sys.argv = old_argv
    return rc, buf.getvalue()


if __name__ == "__main__":
    raise SystemExit(main())
