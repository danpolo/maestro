"""Guard: the 3-doc source-of-truth invariants must hold.

Extracted verbatim from the reference `scripts/check_roadmap_consistency.py` (271 lines)
per `docs/plans/2026-08-18-m4c-superseded-sidecars.md` §4/§2b — M4c batch 2, **Wave B**.
Function bodies are unchanged from the reference — only the import block and the
derivation of the module-level path globals differ, exactly as every other extracted
module (`maestro.verifications`, `maestro.status`, `maestro.watchdog`,
`maestro.docs.depmap`, `maestro.docs.upcoming`) does. Behavioural surprises are
catalogued in `docs/FOUND_BUGS.md` (bug #185, from Wave A's characterisation) and pinned
by `tests/characterization/test_consistency.py`; none of them is fixed here.

The reference is a standalone CLI script run as a pre-commit hook and at the end of
`mark_task_complete.py` — a `main()` that prints to stdout/stderr and returns a process
exit code, not a function living inside `orchestrator_run.py`. `main()` below keeps that
shape exactly. Its seven checks (a)-(g) are documented in the module docstring below,
copied unchanged from the reference.

Checks (b)/(g) originally shelled out to `scripts/gen_dependency_map.py --check` and
`scripts/gen_upcoming.py --check` (now-deleted-at-M5 scripts) via `subprocess.run
([sys.executable, str(...), "--check"])`. Per `docs/plans/2026-08-18-m4c-superseded-
sidecars.md` §2b they are rewired here to their M4c in-process equivalents,
`maestro.docs.depmap.run_cli(["--check"])` / `maestro.docs.upcoming.run_cli(["--check"])`
— the same `sys.argv`-substituting, stdout-capturing wrapper `maestro.verifications.
run_cli` established, added to those two sibling modules for exactly this call site. The
`GEN_UPCOMING.exists()` guard the reference never had a reason to need (the sibling
script was assumed present) is dropped: an in-process module import always "exists", so
the guard would be dead code that can never trigger.

`run_cli()` below is new wiring on top of the verbatim port, the same shape as
`maestro.verifications.run_cli`: `main()` above is left untouched, reading `sys.argv`
and printing exactly as the reference script does; `run_cli()` substitutes `sys.argv`
for the duration of the call and captures whatever `main()` prints to stdout instead of
letting it hit real stdout, so `maestro.docs.complete.main()` (the graduation script's
own former `check_roadmap_consistency.py` shell-out) can call straight into this module
in-process instead.
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import sys
from pathlib import Path

import yaml

from maestro.docs import depmap as gen_dependency_map
from maestro.docs import upcoming as gen_upcoming
from maestro.paths import Paths

_PATHS = Paths.from_env()

REPO = _PATHS.repo
ROADMAP = REPO / "docs" / "ROADMAP.md"
DEPMAP = REPO / "docs" / "dependency_map.md"
REGISTRY = REPO / ".orchestrator" / "completed_tasks.json"
DOC_FILES = [REPO / "docs" / "PROJECT.md", REPO / "docs" / "AUTONOMOUS_SYSTEM.md"]


def _roadmap_tasks() -> list[dict]:
    content = ROADMAP.read_text(encoding="utf-8")
    tasks: list[dict] = []
    for raw in re.findall(r"```yaml\n(.*?)```", content, re.DOTALL):
        try:
            task = yaml.safe_load(raw)
        except yaml.YAMLError:
            continue
        if isinstance(task, dict) and task.get("id"):
            tasks.append(task)
    return tasks


def _registry_ids() -> set[str]:
    if not REGISTRY.exists():
        return set()
    try:
        reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    return {str(r.get("id")) for r in reg if r.get("id")}


def _deps(task: dict) -> list[str]:
    deps = task.get("deps") or []
    if isinstance(deps, str):
        deps = [deps] if deps else []
    return [str(d) for d in deps]


def check_no_lingering_complete(problems: list[str]) -> None:
    for t in _roadmap_tasks():
        if t.get("status") == "complete":
            problems.append(
                f"(a) task {t['id']} has status:complete but still lives in ROADMAP.md "
                f"— graduate it with scripts/mark_task_complete.py"
            )


def check_depmap_no_drift(problems: list[str]) -> None:
    rc, out = gen_dependency_map.run_cli(["--check"])
    if rc != 0:
        detail = out.strip()
        problems.append(
            "(b) docs/dependency_map.md is stale/drifted — run "
            f"`python scripts/gen_dependency_map.py`:\n      {detail}"
        )


def check_upcoming_no_drift(problems: list[str]) -> None:
    """docs/UPCOMING.md (the human-facing companion doc) must be byte-identical to
    what gen_upcoming.py would produce now — same staleness contract as the dep map
    (b)."""
    rc, out = gen_upcoming.run_cli(["--check"])
    if rc != 0:
        detail = out.strip()
        problems.append(
            "(g) docs/UPCOMING.md is stale/drifted — run "
            f"`python scripts/gen_upcoming.py`:\n      {detail}"
        )


def check_deps_resolve(problems: list[str]) -> None:
    tasks = _roadmap_tasks()
    pending_ids = {str(t["id"]) for t in tasks if t.get("status") != "complete"}
    known = pending_ids | _registry_ids()
    for t in tasks:
        if t.get("status") == "complete":
            continue
        for dep in _deps(t):
            if dep not in known:
                problems.append(
                    f"(c) pending task {t['id']} has dep '{dep}' that is neither a "
                    f"pending ROADMAP id nor a graduated registry id"
                )


_FLOOR_RE = re.compile(r"(?:>=|>|≥|-ge\b|at least|minimum|\bmin\b)\s*\d", re.IGNORECASE)
# A gate is "coverage-capable" (and so NOT floor-only) if it names a dedicated
# completion checker or asserts whole-corpus terminal coverage rather than a count.
_COVERAGE_RE = re.compile(
    r"full corpus|all \d|every |complete|coverage|100%|entire", re.IGNORECASE
)


def _is_floor_only(v: dict) -> bool:
    """True if a verification's *only* quantitative assertion is a bare lower bound
    (`>= N`, `at least N`). A compound gate that also checks whole-corpus coverage
    (e.g. cmd `scripts/check_pairs_complete.py`) is NOT floor-only, even if its prose
    mentions a pair floor. Floor-only gates are satisfied by partial progress, so they
    must not be the sole completion gate on a multi-day/quota-bound task."""
    blob = " ".join(str(v.get(k, "")) for k in ("check", "cmd", "expect"))
    return bool(_FLOOR_RE.search(blob)) and not _COVERAGE_RE.search(blob)


def check_resumable_coverage_gate(problems: list[str]) -> None:
    for t in _roadmap_tasks():
        if t.get("status") == "complete" or not t.get("resumable"):
            continue
        autos = [v for v in (t.get("verifications") or []) if v.get("kind") == "auto"]
        if not autos:
            problems.append(
                f"(e) resumable task {t['id']} has no auto verification — a quota-bound "
                f"task needs a coverage gate that only passes at 100% completion"
            )
            continue
        if all(_is_floor_only(v) for v in autos):
            ids = ", ".join(str(v.get("id", "?")) for v in autos)
            problems.append(
                f"(e) resumable task {t['id']} is gated only by floor thresholds "
                f"({ids}) — a single quota sitting clears `>= N`, so it would "
                f"false-graduate. Add a coverage check (e.g. scripts/check_pairs_complete.py)"
            )


def check_questions_schema(problems: list[str]) -> None:
    for t in _roadmap_tasks():
        if t.get("status") == "complete":
            continue
        raw = t.get("questions")
        if raw is None:
            continue
        if not isinstance(raw, list) or not raw:
            problems.append(
                f"(f) task {t['id']} has a `questions:` field that is not a non-empty list"
            )
            continue
        if t.get("dispatch") == "manual":
            problems.append(
                f"(f) task {t['id']} sets both `questions:` and `dispatch: manual` — "
                f"Dan either answers a question or performs the task, not both"
            )
        seen: set[str] = set()
        for i, q in enumerate(raw):
            if not isinstance(q, dict):
                problems.append(f"(f) task {t['id']} question #{i} is not a mapping")
                continue
            qid    = str(q.get("id") or "").strip()
            prompt = str(q.get("prompt") or "").strip()
            if not qid:
                problems.append(f"(f) task {t['id']} question #{i} has no `id`")
            elif qid in seen:
                problems.append(f"(f) task {t['id']} has duplicate question id '{qid}'")
            else:
                seen.add(qid)
            if not prompt:
                problems.append(
                    f"(f) task {t['id']} question '{qid or i}' has no `prompt`"
                )
            opts = q.get("options")
            if opts is not None and (not isinstance(opts, list) or not opts):
                problems.append(
                    f"(f) task {t['id']} question '{qid or i}' has `options` that is "
                    f"not a non-empty list (omit it for a free-text question)"
                )


def check_graduation_markers(problems: list[str]) -> None:
    doc_text = "\n".join(
        f.read_text(encoding="utf-8") for f in DOC_FILES if f.exists()
    )
    markers = set(re.findall(r"<!--\s*graduated:\s*([A-Za-z0-9.]+)\s*-->", doc_text))
    for rid in sorted(_registry_ids()):
        if rid not in markers:
            problems.append(
                f"(d) registry entry {rid} has no `<!-- graduated: {rid} -->` marker "
                f"in PROJECT.md or AUTONOMOUS_SYSTEM.md — it was completed but not "
                f"moved into a living doc"
            )


def main() -> int:
    problems: list[str] = []
    check_no_lingering_complete(problems)
    check_resumable_coverage_gate(problems)
    check_questions_schema(problems)

    # The drift check (b), dep-resolution (c) and marker check (d) all depend on the
    # completion registry. It is normally git-tracked (see .gitignore) so it travels
    # into every worktree/clone — but if it is genuinely absent (e.g. a checkout made
    # before it was tracked, or a manual delete) those checks cannot run meaningfully
    # and would false-fail. Degrade gracefully: warn and skip them rather than block.
    if REGISTRY.exists():
        check_depmap_no_drift(problems)
        check_upcoming_no_drift(problems)
        check_deps_resolve(problems)
        check_graduation_markers(problems)
    else:
        print(
            f"WARNING: {REGISTRY.relative_to(REPO)} not found — skipping dep-map drift, "
            f"dep-resolution and graduation-marker checks (registry-dependent).",
            file=sys.stderr,
        )

    if problems:
        print("ROADMAP consistency check FAILED:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    n_pending = sum(1 for t in _roadmap_tasks() if t.get("status") != "complete")
    n_grad = len(_registry_ids())
    print(
        f"ROADMAP consistency OK: {n_pending} pending tasks, "
        f"{n_grad} graduated entries, dep map in sync."
    )
    return 0


def run_cli(argv: list[str]) -> tuple[int, str]:
    """In-process equivalent of `python check_roadmap_consistency.py <argv...>`.

    Substitutes `sys.argv` for the duration of the call — `main()` above is left
    untouched, reading `sys.argv` exactly as the reference script does — and captures
    whatever it prints to stdout instead of letting it hit the real stdout. Unlike
    `maestro.docs.depmap.run_cli`/`maestro.docs.upcoming.run_cli`, stderr is left
    unredirected here: `maestro.docs.complete.main()`, this function's one caller, used
    to just re-emit a subprocess's captured stdout and stderr verbatim
    (`sys.stdout.write(g.stdout); sys.stderr.write(g.stderr)`) — with `main()` now run
    in-process, its own `print(..., file=sys.stderr)` calls already land on the real
    stderr directly, which is the same end state the old verbatim re-emit produced, with
    nothing to copy. Returns (exit_code, captured_stdout), the in-process analogue of a
    subprocess's (returncode, stdout)."""
    old_argv = sys.argv
    buf = io.StringIO()
    try:
        sys.argv = ["check_roadmap_consistency.py", *argv]
        with contextlib.redirect_stdout(buf):
            rc = main()
    finally:
        sys.argv = old_argv
    return rc, buf.getvalue()


if __name__ == "__main__":
    raise SystemExit(main())
