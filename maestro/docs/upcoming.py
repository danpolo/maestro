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
* three hardcoded mentions of the reference project's own name — one in a comment above
  `GLOSSARY`, one in the `LoRA fine-tune` glossary entry's definition, one in the Opus
  prompt built by `_refresh_prompt` — are genericized/parameterized via
  `PROJECT_NAME` (the same `project.yaml` `project.name` → `REPO_ROOT.name` fallback
  `maestro/watchdog.py`'s `PROJECT_NAME` already uses), so `tests/test_purity.py` stays
  green. This is the only content-level deviation from the reference; none of it touches
  anything test-observable — `GLOSSARY` term names/aliases, `detect_terms`, and `render`'s
  output shape are all pinned by `tests/characterization/test_upcoming.py` and unaffected.

`_opus_complete` is the one function that shells out to an agent CLI (`claude -p --model
claude-opus-4-8 <prompt>`, billed against the Max subscription, never the metered API — see
`refresh_explanations`/`_refresh_prompt`, which build and consume its prompt/response). It
is extracted verbatim and left fully wired, not stubbed: it is exercised in tests by
swapping this module's own `subprocess` reference for a small recording stand-in
(`tests/characterization/test_upcoming.py`'s `_fake_subprocess`, mirroring
`test_selfheal.py`'s `_fake_subprocess` for `_judge_complete`), so no test can ever spawn a
real `claude` process.

Behavioural surprises are catalogued in `docs/FOUND_BUGS.md` (entries #182–#184, shared
with `maestro.docs.depmap`'s characterisation) and pinned by
`tests/characterization/test_upcoming.py`; none of them is fixed here.
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

from maestro import prep_actions as prepared_actions
from maestro.config import load_project_yaml
from maestro.docs import depmap as gdm
from maestro.paths import Paths

_PATHS = Paths.from_env()
_PROJECT_YAML = load_project_yaml()

REPO_ROOT          = _PATHS.repo
ROADMAP            = REPO_ROOT / "docs" / "ROADMAP.md"
UPCOMING           = REPO_ROOT / "docs" / "UPCOMING.md"
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

JUDGE_MODEL = "claude-opus-4-8"
REFRESH_TIMEOUT = 240


# ── Glossary: each term is defined once here, detected per-task by its aliases,
# and rendered both inline ("terms in this task") and in the central glossary.
# Definitions are 1–2 sentences, concrete, tied to *this* project (a Hebrew RAG
# bot over the project’s Telegram channel). ──
GLOSSARY: list[dict] = [
    {
        "term": "RRF (Reciprocal Rank Fusion)",
        "aliases": ["rrf", "reciprocal rank", "rrf leg", "fusion"],
        "definition": (
            "A simple, robust way to merge several ranked result lists into one. "
            "Each retrieval method (\"leg\") ranks the posts; RRF scores a post by "
            "1/(k+rank) summed across legs, so a post ranked highly by several legs "
            "floats to the top. It needs no score calibration between legs, which is "
            "why this bot fuses dense + sparse (+ ColBERT, + image) with it."
        ),
    },
    {
        "term": "Dense retrieval",
        "aliases": ["dense ann", "dense retrieval", "dense leg", "dense vector", "dense 1024", "dense model", "dense space"],
        "definition": (
            "Retrieval by meaning: each post is compressed into one ~1024-number "
            "vector that captures its semantics, and a query finds posts whose "
            "vectors point in a similar direction. Catches paraphrases the exact "
            "words miss — but can blur rare named entities."
        ),
    },
    {
        "term": "Sparse retrieval",
        "aliases": ["sparse", "lexical weight", "learned sparse", "sparse leg"],
        "definition": (
            "Retrieval by words: a learned per-token weight vector (mostly zeros, "
            "hence \"sparse\") matches the actual terms in query and post. It is the "
            "lexical counterweight to dense — strong on exact Hebrew names and rare "
            "terms that the dense vector smooths away."
        ),
    },
    {
        "term": "Bi-encoder",
        "aliases": ["bi-encoder", "biencoder"],
        "definition": (
            "An embedding model that encodes the query and each post *separately* "
            "into vectors, then compares them by a cheap similarity. Fast enough to "
            "score all 87k posts — but query and documents MUST be encoded by the "
            "same model, or the vectors live in incomparable spaces (see space match)."
        ),
    },
    {
        "term": "Cross-encoder",
        "aliases": ["cross-encoder", "crossencoder", "reranker", "re-rank", "rerank"],
        "definition": (
            "A slower, more accurate model that reads the query and ONE post "
            "together and scores their relevance directly. Too expensive to run on "
            "the whole corpus, so it reranks only the top candidates the fast legs "
            "surface — the final precision stage of the pipeline."
        ),
    },
    {
        "term": "ColBERT / late-interaction",
        "aliases": ["colbert", "late-interaction", "late interaction"],
        "definition": (
            "A retrieval style that keeps a separate vector per *token* of a post "
            "(~32 of them) instead of one pooled vector, and matches at the token "
            "level. \"Late interaction\" = query and doc tokens meet only at scoring "
            "time. Recovers token-level hits (e.g. a specific Hebrew name) that a "
            "single pooled vector loses."
        ),
    },
    {
        "term": "MaxSim",
        "aliases": ["maxsim", "max-sim"],
        "definition": (
            "ColBERT's scoring rule: for each query token take its highest similarity "
            "to any token in the post, then sum those maxima. Rewards a post that "
            "matches every part of the query somewhere in its text."
        ),
    },
    {
        "term": "BGE-M3",
        "aliases": ["bge-m3", "bgem3", "bge m3"],
        "definition": (
            "The multilingual embedding model this bot runs as its primary encoder. "
            "Unusual in producing dense AND learned-sparse vectors from one model, so "
            "it powers two of the RRF legs at once. \"Base\" = the off-the-shelf "
            "weights (currently in production)."
        ),
    },
    {
        "term": "LoRA fine-tune",
        "aliases": ["lora", "fine-tune", "fine tune", "ft model", "ft adapter", "ft v2", "adapter"],
        "definition": (
            "Low-Rank Adaptation: a cheap way to specialise a big model by training "
            "two small extra matrices (an \"adapter\") instead of all the weights. "
            "Lets the bot try to teach base BGE-M3 about the project's Hebrew domain "
            "without a full, expensive retrain."
        ),
    },
    {
        "term": "Hard negatives",
        "aliases": ["hard negative", "hard-negative", "hard negs"],
        "definition": (
            "Wrong-but-tempting training examples: posts that look similar to the "
            "right answer but aren't. Training against these (rather than random "
            "unrelated posts) forces the model to learn fine distinctions instead of "
            "easy ones — the fix for the original weak fine-tune."
        ),
    },
    {
        "term": "Triplets",
        "aliases": ["triplet", "triple", "300-triple", "anchor"],
        "definition": (
            "Training/eval examples of the form (query, a relevant post, an "
            "irrelevant/hard-negative post). The model learns to score the relevant "
            "one higher. The eval set here is 300 such triples drawn from real "
            "queries."
        ),
    },
    {
        "term": "SigLIP-2 (image embeddings)",
        "aliases": ["siglip", "siglip-2", "siglip2", "image embedding", "image_embeddings", "vision"],
        "definition": (
            "A multilingual vision-language model that maps an image AND a text "
            "query into the *same* vector space, so a Hebrew text query can retrieve "
            "posts by what their photo shows (e.g. \"תמונה של טנק\" → tank photos) "
            "even when the caption never says it."
        ),
    },
    {
        "term": "vec0 table",
        "aliases": ["vec0", "vec0 table"],
        "definition": (
            "A virtual table provided by the sqlite-vec extension that stores vectors "
            "inside SQLite and does fast nearest-neighbour search over them — how "
            "embeddings were kept in the original single-file DB design."
        ),
    },
    {
        "term": "sqlite-vec vs LanceDB",
        "aliases": ["sqlite-vec", "sqlite vec", "lancedb", "lance db"],
        "definition": (
            "Two vector stores. sqlite-vec keeps vectors in the SQLite file (simple, "
            "one file); LanceDB is a columnar vector database better suited to "
            "multiple vector columns and larger ANN workloads. The bot migrated to "
            "LanceDB (Phase 14) to make room for extra legs like ColBERT."
        ),
    },
    {
        "term": "Recall@5",
        "aliases": ["recall@5", "recall @5", "r@5"],
        "definition": (
            "Fraction of eval queries whose correct post lands in the top 5 results. "
            "The headline \"did we find it at all\" metric; base bot ≈ 0.93."
        ),
    },
    {
        "term": "NDCG@10",
        "aliases": ["ndcg@10", "ndcg"],
        "definition": (
            "Normalised Discounted Cumulative Gain over the top 10 — rewards putting "
            "the right post *higher*, not just somewhere in the list. A ranking-"
            "quality metric; base ≈ 0.858."
        ),
    },
    {
        "term": "MRR@10",
        "aliases": ["mrr@10", "mrr"],
        "definition": (
            "Mean Reciprocal Rank: averages 1/(rank of the first correct post) over "
            "queries. High when the right answer is usually #1–2. Base ≈ 0.827."
        ),
    },
    {
        "term": "Query-vs-doc space match",
        "aliases": ["space-match", "space match", "cos ~ 1.0", "cosine ~1.0", "cosine ~ 1.0", "same space", "embedding space"],
        "definition": (
            "A safety check that the query encoder and the document encoder are the "
            "same model. If they differ (e.g. fine-tuned queries vs base-encoded "
            "posts) their vectors are not comparable and similarity scores are "
            "meaningless. Confirmed by re-encoding a doc as a query and checking "
            "cosine ≈ 1.0."
        ),
    },
]


# ── ROADMAP detail-link parsing ──

def _detail_paths(roadmap_text: str) -> dict[str, str]:
    """Map each task id -> the `Detail: \`retired/tasks/PhaseXX.md\`` path that
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


def detect_terms(text: str) -> list[dict]:
    """Glossary entries whose aliases appear in `text` (case-insensitive),
    preserving glossary order."""
    low = text.lower()
    return [g for g in GLOSSARY if any(a in low for a in g["aliases"])]


# ── Opus refresh path (--refresh-explanations) ──

def _opus_complete(prompt: str) -> str:
    """One bounded Opus call via the Claude CLI (Max subscription, never metered).
    Returns stdout or "" on any failure — every caller is best-effort."""
    try:
        r = subprocess.run(
            ["claude", "-p", "--model", JUDGE_MODEL, prompt],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=REFRESH_TIMEOUT,
        )
        out = (r.stdout or "").strip()
        if r.returncode == 0 and out:
            return out
        print(f"  [opus] rc={r.returncode} stderr={(r.stderr or '')[:160]}", file=sys.stderr)
    except Exception as exc:
        print(f"  [opus] call failed: {exc}", file=sys.stderr)
    return ""


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
            "model": JUDGE_MODEL,
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
        }
        refreshed += 1
        print(f"  [opus] {tid}: refreshed", file=sys.stderr)
    if refreshed:
        EXPLANATIONS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        EXPLANATIONS_CACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False) + "\n")
    return refreshed


def _refresh_prompt(task: dict, detail: str) -> str:
    glossary_terms = ", ".join(g["term"] for g in GLOSSARY)
    return (
        "You are writing for Dan, the non-specialist owner of a Hebrew Retrieval-"
        f"Augmented-Generation (RAG) Telegram bot over the {PROJECT_NAME} news "
        "channel. He wants to UNDERSTAND each upcoming engineering task, not just "
        "see its title. Explain in plain English, concretely, tied to this bot.\n\n"
        "Write two things for the task below:\n"
        "1. what_why — 2–4 sentences: what this task actually does and why it should "
        "help the bot find the right Hebrew posts. No marketing, no hedging.\n"
        "2. pipeline_fit — 2–3 sentences: where this slots into the retrieval "
        "pipeline (the legs are: dense BGE-M3, learned-sparse BGE-M3, optional "
        "ColBERT late-interaction, optional image/SigLIP; fused by RRF; then a "
        "cross-encoder reranks the top candidates). Be specific about what it "
        "feeds or changes.\n\n"
        "These jargon terms are defined elsewhere, so you may USE them without "
        f"redefining: {glossary_terms}.\n\n"
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
        terms = detect_terms(searchable)
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

    # Central glossary.
    lines.append("## Glossary")
    lines.append("")
    lines.append("_Every jargon term used above, defined once._")
    lines.append("")
    for g in GLOSSARY:
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
        print(f"Refreshed {n} task explanation(s) via {JUDGE_MODEL}.")

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
