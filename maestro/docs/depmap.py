"""Generate docs/dependency_map.md (mermaid) from docs/ROADMAP.md, and render it to a PNG.

Extracted verbatim from the reference `scripts/gen_dependency_map.py` (270 lines) and
`scripts/render_dependency_map.sh` (54 lines) per `docs/plans/2026-08-18-m4c-superseded-
sidecars.md` §4, **Wave B** of M4c batch 2. Function bodies are unchanged from
`gen_dependency_map.py` — only the import block and the derivation of the module-level
path globals differ, exactly as every other extracted module does. The anchor global is
named `REPO_ROOT` here, not `REPO` like every other characterised module, matching the
reference's own naming (see `tests/characterization/test_depmap.py`'s module docstring).
Behavioural surprises are catalogued in `docs/FOUND_BUGS.md` (bug #182) and pinned by
`tests/characterization/test_depmap.py`; none of them is fixed here.

`render_png()` is new wiring, not a verbatim port: `render_dependency_map.sh` shelled out
to a local mermaid-cli build (`mmdc`, at `.mermaid/node_modules/.bin/mmdc`) to turn
`docs/dependency_map.md`'s embedded mermaid block into `docs/dependency_map.png`. Rather
than re-adding that intermediate .sh script, `render_png()` invokes the same `mmdc`
executable directly via `subprocess`, mirroring the shell script's own logic line for
line: extract the *first* fenced ```mermaid block from `DEP_MAP` into a temp `.mmd` file,
error if none is found, then run `mmdc` with the exact same argv (`-i/-o/-p/-s/-w
--quiet`) the reference used — matching how R4 (`maestro.hitl.telegram.notify_telegram`)
replaced a shell-out with a native call. The one piece of the reference script's logic
deliberately not reproduced is its `--hook` mode: that stdin-JSON argv branch existed only
so the script itself could double as a Claude Code PostToolUse hook (deciding whether the
hook's own invocation was "relevant" to `docs/dependency_map.md`); a plain Python function
has no stdin payload and no hook registration of its own — its caller decides when to call
it — so there is nothing for that branch to gate. Every other line of the render step's
logic is preserved.

**MMDC resolution is a deliberate deviation, added after Wave B landed.** The reference (and
this module, originally) hard-coded a per-repo local install at `.mermaid/node_modules/.bin/
mmdc` — the shape `scripts/render_dependency_map.sh` used. `mmdc` is not project-specific, so
maintaining a full mermaid-cli + puppeteer + Chromium `node_modules` tree per maestro-scaffolded
repo is pure duplication; `maestro init` now installs it once, globally, via `scripts/
ensure_mermaid.sh` (see `_run_ensure_mermaid` in `maestro/cli.py`). MMDC below prefers a global
`mmdc` on `$PATH` and falls back to the old local path only for a repo that already has one
(e.g. one scaffolded before this change) — never both, and never re-installing over a working
local one. `PUPPETEER_CFG` is unaffected: it is still a small per-repo config file (now
scaffolded by `maestro init` itself rather than shipped inside a local mermaid-cli install),
since the browser binary it points at can differ machine to machine.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from maestro.paths import Paths

_PATHS = Paths.from_env()

REPO_ROOT       = _PATHS.repo
ROADMAP         = REPO_ROOT / "docs" / "ROADMAP.md"
DEP_MAP         = REPO_ROOT / "docs" / "dependency_map.md"
STATE_JSON      = _PATHS.state
COMPLETED_TASKS = REPO_ROOT / ".orchestrator" / "completed_tasks.json"

# New globals for render_png() (not present in the reference gen_dependency_map.py;
# these were shell variables local to render_dependency_map.sh's own scope).
#
# MMDC: global `mmdc` on $PATH first (the normal case — see the module docstring's "MMDC
# resolution" note), falling back to a legacy per-repo local install so a repo scaffolded
# before this change keeps working without re-running `init`. Resolved once at import time,
# like every other path global here — a global install added *after* this process started
# would need a fresh `maestro` invocation to be picked up, same as any other env change
# `Paths.from_env()` reads at import.
_GLOBAL_MMDC  = shutil.which("mmdc")
MMDC          = Path(_GLOBAL_MMDC) if _GLOBAL_MMDC else REPO_ROOT / ".mermaid" / "node_modules" / ".bin" / "mmdc"
PUPPETEER_CFG = REPO_ROOT / ".mermaid" / "puppeteer-config.json"
DEP_MAP_PNG   = REPO_ROOT / "docs" / "dependency_map.png"

REQUIRED_FIELDS = ("id", "title", "short_desc", "est_time", "mode", "eval_relevance", "deps")

_YAML_BLOCK = re.compile(r"```yaml\s*\n(.*?)\n```", re.DOTALL)


def _parse_block(text: str) -> dict | None:
    """Parse one fenced yaml task block into a dict. Falls back to a tiny
    hand parser if PyYAML is unavailable. Returns None if it has no `id`."""
    data: dict | None = None
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text)
    except Exception:
        data = _manual_parse(text)
    if not isinstance(data, dict) or "id" not in data:
        return None
    return data


def _manual_parse(text: str) -> dict:
    """Dependency-free parser for the simple single-line `key: value` blocks
    this repo uses (deps is a bracketed list)."""
    out: dict = {}
    for line in text.splitlines():
        line = line.rstrip()
        if not line or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if key == "deps":
            inner = val.strip().lstrip("[").rstrip("]").strip()
            out[key] = [d.strip().strip("'\"") for d in inner.split(",") if d.strip()]
        else:
            out[key] = val.strip().strip("'\"")
    return out


def parse_tasks(roadmap_text: str) -> list[dict]:
    tasks: list[dict] = []
    for block in _YAML_BLOCK.findall(roadmap_text):
        parsed = _parse_block(block)
        if parsed is None:
            continue
        if isinstance(parsed.get("deps"), str):
            inner = parsed["deps"].strip().lstrip("[").rstrip("]").strip()
            parsed["deps"] = [d.strip().strip("'\"") for d in inner.split(",") if d.strip()]
        parsed.setdefault("deps", [])
        tasks.append(parsed)
    return tasks


def _validate(tasks: list[dict]) -> None:
    ids = {t["id"] for t in tasks}
    # A dep may also point at a task that has already graduated out of ROADMAP into
    # the completion registry (parse_runnable_tasks() resolves deps against it too),
    # so a graduated id is a valid dep target — not an "unknown dep".
    known = ids | _registry_completed()
    problems: list[str] = []
    for t in tasks:
        for field in REQUIRED_FIELDS:
            if field not in t:
                problems.append(f"task {t.get('id', '?')} missing field '{field}'")
        for dep in t.get("deps", []):
            if dep not in known:
                problems.append(f"task {t['id']} has unknown dep '{dep}'")
    if problems:
        raise SystemExit("ROADMAP parse errors:\n  - " + "\n  - ".join(problems))


def _active_ids() -> set[str]:
    """Tasks the orchestrator currently has in-flight, from the execution SoT.
    Empty until .orchestrator/state.json exists."""
    if not STATE_JSON.exists():
        return set()
    try:
        state = json.loads(STATE_JSON.read_text())
    except Exception:
        return set()
    active = set()
    for row in state.get("in_flight", []) or []:
        tid = row.get("task_id") if isinstance(row, dict) else None
        if tid:
            active.add(tid)
    phase = state.get("phase") or {}
    if isinstance(phase, dict) and phase.get("status") == "in_progress" and phase.get("id"):
        active.add(phase["id"])
    # A task parked in waiting_on_dan is NOT in-flight — it's awaiting Dan's action.
    # `state.phase` isn't cleared when a manual task parks, so without this it would
    # render green/active (e.g. P10 parked for /approve) instead of orange/awaiting-Dan.
    parked = {p.get("task_id") for p in (state.get("waiting_on_dan") or {}).values()
              if isinstance(p, dict)}
    return active - parked


def _registry_completed() -> set[str]:
    """Task IDs that have been graduated out of ROADMAP.md into the completion
    registry (.orchestrator/completed_tasks.json). parse_runnable_tasks() resolves
    deps against this set, so the map must treat these deps as satisfied too —
    otherwise a pending task whose dep already graduated would render as blocked."""
    if not COMPLETED_TASKS.exists():
        return set()
    try:
        rows = json.loads(COMPLETED_TASKS.read_text())
    except Exception:
        return set()
    return {str(r["id"]) for r in rows if isinstance(r, dict) and r.get("id")}


def _node_class(task: dict, active: set[str], completed: set[str]) -> str:
    """Pick the node colour by *why* a task can or cannot run right now.
    Precedence (most-salient first): in-flight > pinned-out > dep-blocked >
    awaiting-Dan > runnable. A dep that is already complete does not count as
    blocking."""
    if task["id"] in active:
        return "active"
    if task.get("hold"):
        return "held"
    unmet = [d for d in task.get("deps", []) if d not in completed]
    if unmet:
        return "blocked"
    if task.get("mode") == "needs-dan":
        return "dan"
    return "ready"


def _label(task: dict) -> str:
    mode = task.get("mode", "?")
    relevance = task.get("eval_relevance", "?")
    est = task.get("est_time", "?")
    title = str(task.get("title", "")).replace('"', "'")
    est = str(est).replace('"', "'")
    return f'{task["id"]}["{task["id"]} — {title}<br/>{est}<br/>{mode} · {relevance}"]'


def render(tasks: list[dict], active: set[str]) -> str:
    completed = {t["id"] for t in tasks if t.get("status") == "complete"}
    completed |= _registry_completed()
    rendered_ids = {t["id"] for t in tasks if t["id"] not in completed}
    lines: list[str] = []
    lines.append("# Roadmap Dependency Map")
    lines.append("")
    lines.append(
        "**Auto-generated from `docs/ROADMAP.md` by `scripts/gen_dependency_map.py` — "
        "do not hand-edit.** Edit `docs/ROADMAP.md` and regenerate."
    )
    lines.append("")
    if active:
        lines.append(f"In-progress (from `.orchestrator/state.json`): {', '.join(sorted(active))}")
    elif STATE_JSON.exists():
        lines.append(
            "In-progress highlight: _none — orchestrator idle "
            "(`.orchestrator/state.json` present, `in_flight` empty)._"
        )
    else:
        lines.append(
            "In-progress highlight: _none — `.orchestrator/state.json` not present yet; "
            "the orchestrator will supply the live highlight once the runtime exists._"
        )
    if completed:
        lines.append("")
        lines.append(
            "_Completed tasks are graduated out of `docs/ROADMAP.md` into "
            "`docs/PROJECT.md` (RAG product) / `docs/AUTONOMOUS_SYSTEM.md` (orchestrator); "
            f"registry of record: `.orchestrator/completed_tasks.json` ({len(completed)} graduated)._"
        )
    lines.append("")
    lines.append("```mermaid")
    lines.append("flowchart TD")
    lines.append("    classDef ready   fill:#3b82f6,color:#fff,stroke:none")
    lines.append("    classDef blocked fill:#94a3b8,color:#fff,stroke:none")
    lines.append("    classDef dan     fill:#f59e0b,color:#fff,stroke:none")
    lines.append("    classDef held    fill:#8b5cf6,color:#fff,stroke:none")
    lines.append("    classDef active  fill:#22c55e,color:#fff,stroke:none")
    lines.append("")
    # Visible legend: renders as a labelled box in the .png. (A mermaid `%%`
    # comment is invisible once rendered, so the colour key must be real nodes.)
    lines.append('    subgraph legend["Legend — node colour = why a task can / cannot run now"]')
    lines.append("        direction LR")
    lines.append('        Lready["ready<br/>runnable now (autonomous, deps met)"]:::ready')
    lines.append('        Ldan["needs Dan<br/>runnable, waits on a human action / approval"]:::dan')
    lines.append('        Lblocked["blocked<br/>waiting on an unfinished dependency"]:::blocked')
    lines.append('        Lheld["held<br/>pinned out via hold:true — will not launch"]:::held')
    lines.append('        Lactive["active<br/>in-flight now (from state.json)"]:::active')
    lines.append("    end")
    lines.append("")
    for t in tasks:
        if t["id"] in completed:
            continue  # completed/graduated work lives in PROJECT.md / AUTONOMOUS_SYSTEM.md; not drawn
        lines.append(f"    {_label(t)}:::{_node_class(t, active, completed)}")
    lines.append("")
    has_edges = False
    for t in tasks:
        if t["id"] in completed:
            continue
        for dep in t.get("deps", []):
            if dep not in rendered_ids:
                continue  # dep already complete/graduated — satisfied, not drawn
            lines.append(f"    {dep} --> {t['id']}")
            has_edges = True
    if not has_edges:
        lines.append("    %% (no dependency edges — all tasks are independent)")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if dependency_map.md is stale; do not write")
    args = ap.parse_args()

    if not ROADMAP.exists():
        raise SystemExit(f"missing {ROADMAP}")

    tasks = parse_tasks(ROADMAP.read_text())
    if not tasks:
        raise SystemExit("no task blocks found in docs/ROADMAP.md")
    _validate(tasks)
    rendered = render(tasks, _active_ids())

    if args.check:
        current = DEP_MAP.read_text() if DEP_MAP.exists() else ""
        if current.strip() != rendered.strip():
            print("dependency_map.md is STALE — run scripts/gen_dependency_map.py", file=sys.stderr)
            return 1
        print("dependency_map.md is up to date.")
        return 0

    DEP_MAP.write_text(rendered)
    print(f"Wrote {DEP_MAP.relative_to(REPO_ROOT)} from {len(tasks)} tasks in docs/ROADMAP.md.")
    print("Tasks:", ", ".join(t["id"] for t in tasks))
    return 0


def run_cli(argv: list[str]) -> tuple[int, str]:
    """In-process equivalent of `python gen_dependency_map.py <argv...>`, the same shape
    as `maestro.verifications.run_cli`/`maestro.status.run_cli`: substitutes `sys.argv`
    for the duration of the call — `main()` above is left untouched — and captures
    whatever it prints instead of letting it hit the real stdout/stderr. Unlike those two
    (whose reference scripts print only to stdout), `main()`'s `--check`-failure line goes
    to stderr, and `maestro.docs.consistency.check_depmap_no_drift` needs that line in the
    problem it reports — the same reason its own subprocess-era call combined
    `res.stdout + res.stderr` — so both streams are redirected into one buffer here,
    matching `maestro.docs.roadmap._call_main`'s combined capture. Returns (exit_code,
    captured_output)."""
    old_argv = sys.argv
    buf = io.StringIO()
    try:
        sys.argv = ["gen_dependency_map.py", *argv]
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = main()
    finally:
        sys.argv = old_argv
    return rc, buf.getvalue()


# ── render step (R7): ported from render_dependency_map.sh, see module docstring ──

def _extract_first_mermaid_block(text: str) -> str:
    """Equivalent of the reference's `awk '/^```mermaid$/{f=1;next} /^```$/{if(f)exit} f'`:
    the body of the *first* fenced ```mermaid block, exclusive of the fence lines
    themselves, with a trailing newline if any body lines were found (matching awk's
    per-line `print`)."""
    body_lines: list[str] = []
    in_block = False
    for line in text.splitlines():
        if line == "```mermaid":
            in_block = True
            continue
        if line == "```":
            if in_block:
                break
            continue
        if in_block:
            body_lines.append(line)
    return ("\n".join(body_lines) + "\n") if body_lines else ""


def render_png() -> None:
    """Render `DEP_MAP` (docs/dependency_map.md) to `DEP_MAP_PNG` (docs/dependency_map.png)
    via the local mermaid-cli build (`mmdc`), invoked directly instead of shelling out to
    the reference's `render_dependency_map.sh`. See the module docstring for what is and
    is not ported from that script."""
    input_text = DEP_MAP.read_text() if DEP_MAP.exists() else ""
    body = _extract_first_mermaid_block(input_text)

    if not body.strip():
        print(f"render_dependency_map: no mermaid block found in {DEP_MAP}", file=sys.stderr)
        raise SystemExit(1)

    fd, tmp_name = tempfile.mkstemp(prefix="depmap_", suffix=".mmd")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        subprocess.run(
            [str(MMDC), "-i", str(tmp_path), "-o", str(DEP_MAP_PNG),
             "-p", str(PUPPETEER_CFG), "-s", "3", "-w", "2400", "--quiet"],
            check=True,
        )
    finally:
        tmp_path.unlink(missing_ok=True)
    print(f"render_dependency_map: wrote {DEP_MAP_PNG}")


if __name__ == "__main__":
    raise SystemExit(main())
