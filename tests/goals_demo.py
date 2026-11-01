"""The GDT-14 demonstrator's shared helpers (P13 goal-driven TDD, R64): not a test module.

`tests/fixtures/goals/demo/project` is "Recipe Box" as setup leaves it: the grill's contract
(the parity `skill_GOALS.md`, revision 2) plus setup's same-revision `Test:` binding of AC-2,
and a small stdlib project whose acceptance tests are RED, GREEN and BLOCKED by design.
`overlays/<name>/` are the files a scripted implementer commits, one task round each: an
`01-test/` commit (`test: ...`) before a `02-code/` one (`feat: ...`), or one flat commit;
`restore.txt` names project files put back to their seed text first.

The root tests (`tests/test_goals_demo.py`) drive the setup path through the real CLI; the
graph lanes (`tests/graph_engineering/test_goals_demo_lanes.py`) drive the loop. Both share
the names below. The fixture project is kept out of collection by `tests/conftest.py`'s
`collect_ignore_glob`: its acceptance tests run only as Maestro runs them.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

from maestro import acceptance, goal_roadmap, goal_state
from maestro.docs import goals

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO = REPO_ROOT / "tests" / "fixtures" / "goals" / "demo"
PROJECT = DEMO / "project"
OVERLAYS = DEMO / "overlays"
PARITY = REPO_ROOT / "tests" / "fixtures" / "goals" / "parity"

#: Setup's binding of AC-2 (a `Test:` is not a revision field: same revision 2).
AC2_TEST = "pytest tests/acceptance/test_not_a_recipe.py"
#: Lane B binds AC-4 to its cost model (same revision, legal).
AC4_TEST = "python bench/search_latency.py"

#: The task ids `maestro goals tasks` proposes on the seed (`goal_roadmap._slug` trims a
#: name to 40 characters), in the order it proposes them (contract order).
AC1_TASK = "ac-1-saved-posts-are-listed-within-an-hour"
HARNESS = "ac-3-harness"
AC3_TASK = "ac-3-search-lists-the-right-recipe-in-the-top"
TIDY_ID = "tidy-classifier"

#: The overlays that solve the demo, in task order: AC-3's harness, AC-3's search (inner
#: TDD: the unit test commit first), AC-1's parser, the maintenance refactor.
SOLVING = ("harness", "search", "import", "tidy")

#: The hand-written maintenance task the plan carries beside the proposals: it serves no
#: goal, and its own check is the project's unit tests.
TIDY: dict[str, Any] = {
    "id": TIDY_ID,
    "title": "Tidy the post classifier",
    "short_desc": "Refactor recipebox/classify.py for readability; no behaviour changes.",
    "serves": [],
    "contribution": "maintenance",
    "criteria": [],
    "tests": [],
    "evidence": "the unit tests pass and no criterion the goals protect regresses",
    "deps": [],
    "verifications": [{
        "id": "V1", "kind": "auto",
        "cmd": f"env PYTHONDONTWRITEBYTECODE=1 {shlex.quote(sys.executable)} -m pytest "
               "tests/unit -p no:cacheprovider"}],
}

_IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")
_RESTORE = "restore.txt"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True,
                          text=True).stdout.strip()


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def parity(grill: str) -> str:
    """The grill's contract as `grill` ("skill" or "drive") writes it."""
    return (PARITY / f"{grill}_GOALS.md").read_text(encoding="utf-8")


def contract_text() -> str:
    """The demo's checked-in contract (the skill parity file plus setup's binding)."""
    return (PROJECT / goals.DOC).read_text(encoding="utf-8")


def bind(text: str, criterion: str, command: str) -> str:
    """Setup's binding step as a pure text edit: a `Test:` line after the last field of
    `criterion`'s entry, in that entry's own style (`- **Test:**` or `* **Test** —`)."""
    found = re.fullmatch(r"([A-Z]+)-?(\d+)", criterion)
    if found is None:
        raise ValueError(f"{criterion!r} is not a criterion id")
    heading = re.compile(rf"^#{{3,6}}\s*{found.group(1)}-?{found.group(2)}\b")
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        raise ValueError(f"no {criterion} entry")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("#")),
               len(lines))
    bullets = [i for i in range(start + 1, end) if re.match(r"^\s*[-*]\s", lines[i])]
    if any("**Test" in lines[i] for i in bullets):
        raise ValueError(f"{criterion} already has a Test")
    last = bullets[-1]
    mark = lines[last].lstrip()[0]
    lines.insert(last + 1, f"{mark} **Test:** `{command}`" if ":**" in lines[last]
                 else f"{mark} **Test** — `{command}`")
    return "\n".join(lines)


def _copy_into(src: Path, dest: Path) -> None:
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if path.is_dir() or rel.as_posix() == _RESTORE or "__pycache__" in rel.parts:
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


def _commit(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def apply_overlay(repo: Path, name: str, *, commit: bool = True) -> list[str]:
    """Copy overlay `name` into `repo`: its `restore.txt` paths back to their seed text
    and `01-test/` (committed `test: <name>`), then `02-code/` or the flat directory
    (committed `feat: <name>`). Returns the commit subjects made."""
    root = OVERLAYS / name
    if not root.is_dir():
        raise FileNotFoundError(f"no demo overlay {root}")
    restore = root / _RESTORE
    if restore.is_file():
        for rel in restore.read_text(encoding="utf-8").split():
            target = repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(PROJECT / rel, target)
    split = (root / "01-test").is_dir() or (root / "02-code").is_dir()
    steps = ([(root / "01-test", "test"), (root / "02-code", "feat")] if split
             else [(root, "feat")])
    made = []
    for src, kind in steps:
        if not src.is_dir():
            continue
        _copy_into(src, repo)
        if commit:
            subject = f"{kind}: {name}"
            _commit(repo, subject)
            made.append(subject)
    return made


def materialize(dest: Path, *, contract: Optional[str] = None,
                overlays: Iterable[str] = ()) -> Path:
    """The demo project at `dest` as a git repo on `main`: the seed commit (with
    `contract` as its `docs/GOALS.md` when given), then each overlay as its commits. `dest`
    may already hold files (a graph lane's sandbox has its ROADMAP and `.orchestrator/`):
    they are part of the seed, except what the project's `.gitignore` leaves out."""
    dest = Path(dest)
    shutil.copytree(PROJECT, dest, ignore=_IGNORE, dirs_exist_ok=True)
    if contract is not None:
        (dest / goals.DOC).write_text(contract, encoding="utf-8")
    git(dest, "init", "-q", "-b", "main")
    git(dest, "config", "user.email", "demo@example.invalid")
    git(dest, "config", "user.name", "Recipe Box demo")
    git(dest, "config", "commit.gpgsign", "false")
    _commit(dest, "seed")
    for name in overlays:
        apply_overlay(dest, name)
    return dest


def copy(src: Path, dest: Path) -> Path:
    """A copy of a materialized project (its git history and `.orchestrator/` too)."""
    shutil.copytree(src, dest, symlinks=True)
    return Path(dest)


def baseline(repo: Path, *, timeout: int = 60) -> dict[str, acceptance.Result]:
    """What `maestro goals baseline` does: run, store, record the contract it ran at."""
    doc = goals.load(repo)
    results = acceptance.run_baseline(repo, doc, timeout=timeout)
    acceptance.store(repo, doc, results)
    goal_state.observe_contract(repo, doc, (Path(repo) / goals.DOC).read_text(
        encoding="utf-8"))
    return {r.id: r for r in results}


def roadmap(repo: Path, *, tidy: bool = True) -> list[dict[str, Any]]:
    """The demo plan: exactly what `maestro goals tasks --json` prints on `repo`'s stored
    baseline (each proposal with its gate checks), then `TIDY` unless `tidy` is false."""
    doc = goals.load(repo)
    proposed = [{**t, "verifications": goal_roadmap.verifications(t, doc, repo)}
                for t in goal_roadmap.propose_tasks(doc, acceptance.load(repo))]
    return proposed + ([{**TIDY, "verifications": [dict(v) for v in TIDY["verifications"]]}]
                       if tidy else [])


def write_roadmap(repo: Path, tasks: Iterable[dict[str, Any]]) -> Path:
    path = Path(repo) / "docs" / "ROADMAP.md"
    path.write_text("# ROADMAP\n\n" + goal_roadmap.render_tasks(list(tasks)),
                    encoding="utf-8")
    return path


# --- provenance (R64) -------------------------------------------------------------------------

SCHEMA = "maestro-goals-demo-provenance/1"
#: Beside `goal_outcome.{md,json}` in the lane's repo (gitignored with `.orchestrator/`).
PROVENANCE = ".orchestrator/demo_provenance.json"
#: When set, `export` also copies the bundle, the outcome report and `goals status --json`
#: into this directory (evidence runs only; the suite never sets it).
EVIDENCE_ENV = "MAESTRO_DEMO_EVIDENCE"
#: The fields two runs of one lane may differ in: clocks, and the commit ids (and the
#: finish and question ids hashed from one) that git stamps with them.
VOLATILE = ("generated_at", "ran_at", "at", "commit", "introduced", "merge_commit",
            "finish_id", "question_id")
CONTRACT_SOURCE = "parity skill_GOALS.md + setup binding AC-2"


def fixture_sha256() -> str:
    """sha256 over the sorted `(relpath, bytes)` of the demo fixture."""
    digest = hashlib.sha256()
    for path in sorted(p for p in DEMO.rglob("*") if p.is_file()
                       and "__pycache__" not in p.parts and p.suffix != ".pyc"):
        digest.update(path.relative_to(DEMO).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def tree_sha256(tree: str) -> str:
    """`fixture_sha256`'s digest over a committed git tree (`git rev-parse <rev>:<dir>`),
    read from the object store, never the working files (GDT-14 review L4)."""
    listing = git(REPO_ROOT, "ls-tree", "-r", "-z", tree)
    entries = sorted(((line.split("\t", 1)[1], line.split()[2])
                      for line in listing.split("\0") if line),
                     key=lambda e: e[0].split("/"))     # as `sorted(Path)` orders them
    digest = hashlib.sha256()
    for rel, blob in entries:
        if "__pycache__" in rel.split("/") or rel.endswith(".pyc"):
            continue
        data = subprocess.run(["git", "cat-file", "blob", blob], cwd=str(REPO_ROOT),
                              capture_output=True, check=True).stdout
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update(data + b"\0")
    return digest.hexdigest()


def _fixture() -> dict[str, Any]:
    rel = DEMO.relative_to(REPO_ROOT).as_posix()
    return {"path": rel, "sha256": fixture_sha256(),
            "tree": git(REPO_ROOT, "rev-parse", f"HEAD:{rel}"),
            "clean": not git(REPO_ROOT, "status", "--porcelain", "--", rel)}


def gate_maestro(interpreter: str, cwd: Path) -> dict[str, Any]:
    """The Maestro a gate check (`<interpreter> -m maestro.acceptance`, run in the task's
    tree) imports: through a venv's editable install that is the checkout installed there,
    which is not this one in a worktree run (DESIGN §19.12)."""
    found = subprocess.run(
        [interpreter, "-c", "import maestro, os; print(os.path.dirname(maestro.__file__))"],
        cwd=str(cwd), capture_output=True, text=True, check=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}).stdout.strip()
    root = git(Path(found), "rev-parse", "--show-toplevel")
    return {"interpreter": interpreter, "package": found, "root": root,
            "commit": git(Path(root), "rev-parse", "HEAD"),
            "dirty": bool(git(Path(root), "status", "--porcelain", "--", "maestro"))}


def _gate_interpreter(plan: list[dict]) -> Optional[str]:
    """The interpreter the plan's acceptance checks name (`... <python> -m maestro...`)."""
    for task in plan:
        for check in task.get("verifications") or ():
            argv = shlex.split(str(check.get("cmd") or ""))
            at = argv.index("maestro.acceptance") if "maestro.acceptance" in argv else 0
            if at >= 2 and argv[at - 1] == "-m":
                return argv[at - 2]
    return None


def _maestro(repo: Path, plan: list[dict]) -> dict[str, Any]:
    import platform

    import pytest

    interpreter = _gate_interpreter(plan)
    return {"commit": git(REPO_ROOT, "rev-parse", "HEAD"),
            "dirty": bool(git(REPO_ROOT, "status", "--porcelain")),
            "gate": gate_maestro(interpreter, repo) if interpreter else None,
            "python": platform.python_version(), "pytest": pytest.__version__}


def independence(sources: list[str], tasks: list[dict]) -> dict[str, Any]:
    """Whether the verdict rests on the finish run and Dan's sign-off only (and something
    rests on them: nothing met is not independent), and each reviewer claim the rule
    overrode."""
    return {"sources": sources,
            "independent": bool(sources) and set(sources) <= {"finish", "operator"},
            "overridden": [{"task": t["id"], **i} for t in tasks for i in t["impacts"]
                           if i["claimed"] and i["claimed"] != i["stored"]]}


def _ok(repo: Path, *args: str) -> int:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True).returncode


def _bound_tests(repo: Path, doc: goals.Goals, base: str, finish: str) -> list[dict]:
    """Each project path a criterion's result rests on (`goal_guard._bound_paths`): the
    file its `Test:` names (`binding`) or a module that test imports from beside it
    (`imported`), the commit that added it, and whether it predates the baseline or changed
    after it (the net diff baseline..finish)."""
    from maestro.workflows import goal_guard

    paths: dict[str, dict] = {}
    for c in doc.criteria:
        if not c.test.strip():
            continue
        for path in goal_guard._bound_paths(c, repo):
            entry = paths.setdefault(path, {"path": path, "criteria": [],
                                            "kind": "binding" if path in c.test
                                            else "imported"})
            entry["criteria"].append(c.id)
    out = []
    for path, entry in sorted(paths.items()):
        added = git(repo, "log", "--diff-filter=A", "--format=%H", finish, "--", path)
        introduced = added.splitlines()[-1] if added else ""
        out.append({**entry, "introduced": introduced,
                    "before_baseline": bool(introduced) and _ok(
                        repo, "merge-base", "--is-ancestor", introduced, base) == 0,
                    "changed_after_baseline": _ok(
                        repo, "diff", "--quiet", base, finish, "--", path) != 0})
    return out


def _tasks(repo: Path, plan: list[dict], episodes: list[dict]) -> list[dict]:
    """Each planned task as the control store and the goals store recorded it: its runs,
    its gate attempts' statuses and goals-guard findings, the review impacts as claimed
    and as the rule stored them, and the commit its accepted run landed."""
    import sqlite3

    from maestro.paths import Paths
    from maestro.workflows import handlers

    conn = sqlite3.connect(f"file:{Paths(repo=Path(repo)).control_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    inbox = handlers.attempts_root(repo)
    out = []
    try:
        for task in plan:
            tid = str(task["id"])
            runs = conn.execute("SELECT run_id, status FROM task_runs WHERE task_id = ? "
                                "ORDER BY started_at, rowid", (tid,)).fetchall()
            gates = conn.execute(
                "SELECT a.attempt_id, a.status FROM attempts a JOIN task_runs t "
                "ON a.run_id = t.run_id WHERE t.task_id = ? AND a.node_id = 'gate' "
                "ORDER BY a.rowid", (tid,)).fetchall()
            findings = []
            for g in gates:
                result = handlers.AttemptInbox(inbox, g["attempt_id"]).result()
                findings += list(((result.outputs or {}) if result else {}).get(
                    "goals_guard_findings") or ())
            merged = conn.execute(
                "SELECT integrated_source_snapshot FROM acceptance_decisions WHERE task_id = ? "
                "AND decision = 'accepted' ORDER BY rowid DESC", (tid,)).fetchone()
            out.append({
                "id": tid, "contribution": task.get("contribution", ""),
                "criteria": list(goals.normalise_criteria(task.get("criteria"))[0]),
                "runs": [r["status"] for r in runs],
                "gates": [g["status"] for g in gates], "guard_findings": findings,
                "impacts": [{"goal": e.get("goal"), "claimed": e.get("claimed"),
                             "stored": e.get("impact"), "reason": e.get("reason", "")}
                            for e in episodes if e.get("task") == tid],
                "merge_commit": merged["integrated_source_snapshot"] if merged else ""})
    finally:
        conn.close()
    return out


def provenance(repo: Path, *, lane: str) -> dict[str, Any]:
    """The demo run's provenance bundle (`SCHEMA`), assembled only from Maestro's own
    durable outputs (the stored baseline, the outcome report, the goals store, the control
    store and its attempt results) and git; a fact missing there would be a product gap."""
    from datetime import datetime, timezone

    from maestro import backlog, goal_completion, goal_store, improvement, taskgraph

    repo = Path(repo)
    base = json.loads((repo / acceptance.BASELINE).read_text(encoding="utf-8"))
    report = json.loads((repo / goal_completion.REPORT_JSON).read_text(encoding="utf-8"))
    store = goal_store.read(repo)
    finish = report["commit"]
    contract = git(repo, "show", f"{finish}:{goals.DOC}")
    doc = goals.parse(contract)
    plan = taskgraph.scan_tasks(git(repo, "show", f"{base['commit']}:docs/ROADMAP.md"))
    observed = {r["id"]: r for r in store["criteria"]
                if r.get("run_id") == report["id"] and r.get("commit") == finish}
    signoffs = {r["id"]: r for r in store["operator"]
                if r.get("finish") == report["id"] and r.get("commit") == finish}
    items: dict[str, dict] = {}
    for item in report["items"]:
        entry = {"kind": item["kind"], "status": item["status"]}
        if item["kind"] == "criterion":
            entry.update(state=item.get("state"),
                         source=(observed.get(item["id"]) or {}).get("source", ""),
                         runs=len((item.get("evidence") or {}).get("values") or ()) or 1)
        elif item["kind"] == "operator_check":
            row = signoffs.get(item["id"]) or {}
            entry.update(state=item.get("state"), source="operator" if row else "",
                         signoff={"result": row.get("result"), "revision": row.get("revision"),
                                  "commit": row.get("commit"),
                                  "question_id": row.get("question_id")})
        items[item["id"]] = entry
    tasks = _tasks(repo, plan, store["episodes"])
    met = [i for i in items.values()
           if i["kind"] in ("criterion", "operator_check") and i["status"] == "met"]
    sources = sorted({i["source"] for i in met})
    return {
        "schema": SCHEMA, "lane": lane, "maestro": _maestro(repo, plan),
        "fixture": _fixture(),
        "contract": {"title": doc.title, "revision": doc.revision,
                     "sha256": hashlib.sha256(contract.encode("utf-8")).hexdigest(),
                     "source": CONTRACT_SOURCE},
        "baseline": {"commit": base["commit"], "ran_at": base["ran_at"],
                     "revision": base["revision"],
                     "results": {r["id"]: {"state": r["state"], "command": r["command"],
                                           "reason": r["reason"]}
                                 for r in base["results"]}},
        "tests": _bound_tests(repo, doc, base["commit"], finish),
        "tasks": tasks,
        "finish": {"finish_id": report["id"], "verdict": report["verdict"],
                   "revision": report["revision"], "commit": finish, "at": report["at"],
                   "items": items},
        "independence": independence(sources, tasks),
        # Dan's goal decisions (`goal_conflicts`, design §4's goals-store source).
        "decisions": [{k: d.get(k) for k in ("id", "resolution", "goals", "criteria",
                                             "bound", "revision") if k in d}
                      for d in store.get("decisions") or ()],
        # Where each review follow-up went (`maestro.backlog`, GDT-11 / W7).
        "backlog": [{"id": b.get("id"), "source": (b.get("source") or {}).get("task"),
                     "state": b.get("state"), "of": b.get("of"), "reason": b.get("reason"),
                     "task": b.get("task")} for b in backlog.items(repo)],
        # The improvement analyses (`maestro.improvement`, GDT-12 / W12): each run's kind,
        # status, window task count and the proposals it asked or deferred.
        "improvement": [{"kind": r.get("kind"), "status": r.get("status"),
                         "tasks": r.get("tasks"), "proposals": r.get("proposals")}
                        for r in improvement.read(repo)["runs"]],
        "scenarios": C_MAP,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def export(repo: Path, bundle: dict[str, Any]) -> Path:
    """Write `bundle` to `PROVENANCE` in `repo`; with `EVIDENCE_ENV` set, also copy it, the
    outcome report and `maestro goals status --json` into that directory."""
    import contextlib
    import io
    import os

    from maestro import cli, goal_completion

    path = Path(repo) / PROVENANCE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    where = os.environ.get(EVIDENCE_ENV, "").strip()
    if where:
        folder = Path(where)
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, folder / Path(PROVENANCE).name)
        shutil.copyfile(Path(repo) / goal_completion.REPORT_MD,
                        folder / Path(goal_completion.REPORT_MD).name)
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown):
            cli.main(["goals", "status", str(repo), "--json"])
        (folder / "goals_status.json").write_text(shown.getvalue(), encoding="utf-8")
    return path


def without_volatile(value: Any) -> Any:
    """`value` with every `VOLATILE` key dropped, at any depth (the reproducibility diff)."""
    if isinstance(value, dict):
        return {k: without_volatile(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [without_volatile(v) for v in value]
    return value


_D = "tests/test_goals_demo.py::"
#: Lane A (GDT-14 slice 2): the demo from its RED baseline to complete, with provenance.
_A = ("tests/graph_engineering/test_goals_demo_lanes.py::"
      "test_the_demo_goes_from_a_red_baseline_to_complete_with_provenance")
#: Lane B (GDT-14 slice 3): TIDY's regression reworks; the G1-vs-G3 conflict goes to Dan.
_B = ("tests/graph_engineering/test_goals_demo_lanes.py::"
      "test_the_demo_s_maintenance_regression_reworks_and_its_goal_conflict_goes_to_dan")
#: Lane C (GDT-11): polish, a failed finish's corrective round, polish again, Dan at the cap.
_C = ("tests/graph_engineering/test_goals_demo_lanes.py::"
      "test_a_failed_finish_works_again_and_polishes_again_then_asks_at_the_cap")
_LANES = "tests/graph_engineering/test_goals_lanes.py::"
_GUARD = "tests/test_goal_guard.py::"
_PROGRESS = "tests/test_goal_progress.py::"
_COMPLETION = "tests/test_goal_completion.py::"
_CONFLICTS = "tests/test_goal_conflicts.py::"
_REPLAN = "tests/test_goal_replan.py::"

#: C1-C33 (`02` §C) -> the tests that prove each on the demo or reuse an existing system
#: test (a list of nodes), or a named limit with its owner (and the tests of the part that
#: is built: TRACEABILITY §5's `partial`). Checked by `tests/test_goals_demo.py::
#: test_every_c_scenario_maps_to_a_test_or_a_named_limit` and, against §5,
#: `::test_traceability_s5_says_what_the_c_map_proves`.
#: The demo's graph lanes are `_A` (slice 2; GDT-11 adds its polish stage), `_B` (slice 3)
#: and `_C` (GDT-11).
C_MAP: dict[str, Any] = {
    "C1": [_D + "test_the_demo_contract_is_the_grill_s_contract_plus_setup_s_bindings[skill]",
           "tests/test_grill_surfaces.py::test_every_grill_settles_the_contract_before_design"],
    "C2": [_D + "test_the_demo_contract_is_the_grill_s_contract_plus_setup_s_bindings[drive]",
           _D + "test_either_grill_s_contract_gives_the_same_baseline",
           "tests/test_grill_surfaces.py::test_the_drive_and_skill_parity_contracts_parse_equal",
           "tests/test_goals_contract.py::test_check_json_marks_only_dan_s_gaps_as_questions"],
    "C3": [_D + "test_a_revised_target_makes_the_old_proof_stale_and_a_retired_id_stays_retired",
           "tests/test_goals_contract.py::test_migrate_keeps_every_id_and_text_and_invents_nothing"],
    "C4": [_D + "test_doctor_accepts_the_demo_plan_and_holds_what_names_no_goal",
           "tests/test_goals_gate.py::test_one_invalid_contract_is_a_draft_listing_and_a_gate_"
           "on_the_affected_tasks_only"],
    "C5": [_D + "test_goals_tasks_turn_the_red_baseline_into_the_demo_roadmap",
           "tests/test_goals_roadmap.py::test_a_phase_serving_g1_does_not_make_every_child_direct"],
    "C6": [_D + "test_the_baseline_tells_the_five_states_apart_on_one_project", _A],
    "C7": [_D + "test_the_baseline_tells_the_five_states_apart_on_one_project", _B],
    "C8": [_D + "test_the_baseline_tells_the_five_states_apart_on_one_project", _A],
    "C9": [_D + "test_the_baseline_tells_the_five_states_apart_on_one_project",
           _D + "test_the_solved_demo_waits_on_dan_s_operator_check", _A],
    "C10": ["tests/test_goals_contract.py::test_a_missing_target_or_oracle_is_an_open_question_"
            "never_a_default",
            "tests/test_acceptance.py::test_a_threshold_with_no_direction_is_unknown_not_guessed",
            _CONFLICTS + "test_thresholds_on_one_binding_that_cannot_both_pass_are_a_gate_problem"],
    "C11": [_D + "test_goals_tasks_turn_the_red_baseline_into_the_demo_roadmap", _A],
    "C12": [_A, _B],
    "C13": [_A, _D + "test_a_tamper_that_makes_the_check_pass_is_blocked_by_the_guard_alone",
            _GUARD + "test_editing_or_deleting_a_bound_test_is_blocking",
            _GUARD + "test_a_named_correction_may_edit_but_never_skip_or_xfail"],
    "C14": [_PROGRESS + "test_a_mocked_test_is_no_evidence_for_an_e2e_or_integration_criterion",
            _COMPLETION + "test_review_evidence_and_gate_observations_never_close_a_criterion",
            _A],
    "C15": [_COMPLETION + "test_a_metric_is_green_only_when_every_repeat_passes_and_a_mix_is_"
                          "unknown",
            _PROGRESS + "test_a_metric_with_no_number_or_no_baseline_is_unknown_with_a_targeted_"
                        "follow_up", _A],
    "C16": [_A, _LANES + "test_an_enabling_lane_pair_raises_no_replan"],
    "C17": [_LANES + "test_no_progress_unexpected_merges_and_is_an_unattributed_observation",
            _LANES + "test_two_accepted_unexpected_misses_ask_once_and_an_independent_task_still_"
                     "runs"],
    "C18": [_B, _LANES + "test_a_maintenance_task_that_turns_a_green_criterion_red_reworks", _A],
    "C19": [_A, _PROGRESS + "test_a_diff_alone_is_never_advanced"],
    "C20": [_D + "test_w12_attribution_analysis_and_a_rollback_leave_every_accepted_verdict_"
                 "unchanged_on_the_demo",
            "tests/test_improvement_evidence.py::test_an_attribution_is_an_appended_amendment_"
            "and_the_observation_stays",
            _PROGRESS + "test_a_claim_over_a_failing_check_is_decided_by_rule",
            _LANES + "test_regressed_reworks_the_implementer"],
    "C21": [_B, _LANES + "test_a_goal_conflict_at_the_gate_asks_dan_once_and_his_accept_is_kept"],
    "C22": [_LANES + "test_dan_s_accept_covers_a_later_task_within_its_bound",
            _CONFLICTS + "test_an_accepted_bound_covers_up_to_it_and_beyond_reasks", _B],
    "C23": [_D + "test_a_change_that_advances_g1_but_breaks_inv_1_is_blocked_never_traded",
            _GUARD + "test_a_hard_invariant_blocks_despite_progress_on_the_same_change"],
    "C24": [_B, _LANES + "test_a_conflict_dan_answered_at_the_gate_is_not_asked_again_at_review",
            _CONFLICTS + "test_the_fingerprint_holds_structured_facts_never_reason_text",
            _CONFLICTS + "test_a_free_text_answer_never_accepts_and_new_evidence_asks_again"],
    "C25": [_LANES + "test_two_accepted_unexpected_misses_ask_once_and_an_independent_task_still_"
                     "runs",
            _LANES + "test_an_enabling_lane_pair_raises_no_replan", _A,
            _D + "test_q1_misses_raise_one_replan_on_the_demo"],
    "C26": [_REPLAN + "test_a_proven_infeasibility_triggers_on_one_episode",
            "tests/test_goal_milestones.py::test_all_remaining_red_despite_accepted_tasks_is_a_gap"],
    "C27": [_REPLAN + "test_a_local_recommendation_is_applied_without_asking",
            _REPLAN + "test_a_threshold_change_asks_dan_and_holds_only_dependents",
            "tests/test_orchestrator_scheduling.py::test_a_replan_hold_skips_dependents_and_"
            "dispatches_independent_work",
            _LANES + "test_an_approved_contract_change_waits_on_dan_and_is_not_an_exhausted_queue"],
    "C28": [_B,
            "tests/test_cli.py::test_goals_status_in_a_fresh_process_shows_revision_baseline_and_"
            "observations",
            _LANES + "test_a_direct_task_turning_its_criterion_green_clears_doctor_and_survives_a_"
                     "restart"],
    "C29": [_A, _LANES + "test_a_maintenance_task_that_turns_a_green_criterion_red_reworks",
            "tests/test_backlog.py::test_three_tasks_and_five_follow_ups_two_duplicates_one_moot_"
            "end_in_one_polish_task",
            _GUARD + "test_a_polish_task_reruns_its_area_s_criteria_and_every_protected_one"],
    "C30": [_D + "test_an_empty_queue_on_the_seed_is_incomplete_never_complete", _A,
            _LANES + "test_an_empty_queue_with_a_red_criterion_works_again_then_asks_at_the_cap",
            _LANES + "test_every_remaining_task_goal_held_is_reported_blocked_not_exhausted"],
    "C31": [_C, _D + "test_the_bounded_final_repeat_gate_asks_dan_at_the_cap_on_the_demo",
            _LANES + "test_an_empty_queue_with_a_red_criterion_works_again_then_asks_at_the_cap"],
    "C32": [_D + "test_a_historical_no_progress_row_reads_unattributed_and_a_declined_"
                 "proposal_returns_only_on_new_evidence_on_the_demo",
            "tests/test_improvement.py::test_a_declined_proposal_is_not_resent_without_new_"
            "evidence",
            "tests/test_improvement_evidence.py::test_without_establishing_evidence_a_miss_"
            "stays_unattributed_never_planner",
            _PROGRESS + "test_a_stored_w11a_no_progress_reads_as_legacy_never_as_unexpected"],
    "C33": [_D + "test_an_approved_lever_s_effect_is_measured_before_and_after_on_the_demo",
            "tests/test_improvement_metrics.py::test_an_effect_that_saves_cost_but_lowers_"
            "quality_is_worse_never_better",
            "tests/test_improvement_metrics.py::test_no_metric_counts_green_criteria_and_every_"
            "metric_is_lower_is_better"],
}
