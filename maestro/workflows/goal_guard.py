"""GDT-6: the verification gate's goals guard (R22, R23, R28, R41, R65).

Runs in the graph's verification gate beside W9's scope check; deterministic, no agent:

- `tamper_findings` (R23, C13): the task's committed diff may not touch `docs/GOALS.md`
  (a contract change is Dan's), delete or modify a file a criterion's `Test:` is bound to
  (the file, a directory it names, a module the test imports from beside it, or the
  criterion's `Fixtures:`/`Data:`) unless the task's `test_corrections:` names every
  criterion it binds, add a skip or xfail to one or to a `conftest.py` above it, or add
  test selection to the pytest config. A skip or xfail is never a correction; an added
  file is never a finding.
- `regression_check` (R22, R28, R41, C12, C18): the criteria GREEN in the
  current-revision baseline or observed GREEN since (`goal_state.protected`, GDT-9), plus
  the ones the task's `tests:` or `criteria:` names (incremental selection, R65), rerun on
  the task's tree. GREEN that turned RED blocks, whatever the
  task serves (`serves: []` included); GREEN that cannot run (BLOCKED/UNKNOWN) is rerun
  on the base in the same environment and blocks only when the base is still GREEN
  there, else it is a note: `regressed` is a verified degradation (`01` §6.2). A RED of a
  criterion GREEN only by an observation (not at the baseline) is compared with the base
  too, and RED there as well is a note (`goal_protected_red_on_base`), which the
  reviewer's check reads too (`red_on_base`). A
  criterion still RED never blocks a partial merge.
- GDT-7 (R48-R51): the verified regressions go through `goal_conflicts.judge` with the
  frozen `conflicts` context: a regression on another goal than one a task-named criterion
  turned GREEN for is a question for Dan instead of a finding; one his stored decision
  names is covered (or blocks, or is asked again) whatever advanced; a hard invariant
  still blocks.

`policy` freezes what the gate needs from the controller's checkout (the stored baseline
lives in its `.orchestrator/`), so the gate worker reads nothing but its argv and the
task's tree. Graph path only, like W9: the legacy orchestrator has no goals guard.
"""
from __future__ import annotations

import ast
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional

from maestro import acceptance, validation
from maestro.docs import goals as doc_goals

#: An added line that skips, xfails or deselects tests.
_DISABLES = re.compile(
    r"\bpytest\.(?:mark\.)?(?:skip|skipif|xfail|importorskip)\b|\bmark\.(?:skip|skipif|xfail)\b"
    r"|\.add_marker\(|\bdeselect|\bunittest\.skip|@skip(?:If|Unless)?\b|expectedFailure"
    r"|__test__\s*=\s*False|\bcollect_ignore")
#: The pytest config files; the options that choose which tests run, and an `addopts`
#: value that deselects, ignores or filters some.
_PYTEST_CONFIG = frozenset({"pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini",
                            "pytest.toml", ".pytest.toml"})
_SELECTION_KEYS = frozenset({"testpaths", "norecursedirs", "python_files", "python_classes",
                             "python_functions", "collect_ignore"})
_SELECTION = re.compile(r"--deselect|--ignore|(?:^|[\s\"'=])-[km]")
#: pytest options whose next argument is a value, never a test path.
_PYTEST_VALUES = frozenset({"-k", "-m", "-c", "-p", "-o", "-W", "--rootdir", "--confcutdir",
                            "--basetemp"})


#: GDT-9 review M4: the note for a criterion GREEN only by an observation that is RED on
#: the task's tree and on the base; the reviewer reads it too (`red_on_base`).
RED_ON_BASE = "goal_protected_red_on_base"


def red_on_base(notes) -> list[str]:
    """The criteria a gate's `goals_guard_notes` found RED on the base too."""
    out = []
    for note in notes if isinstance(notes, (list, tuple)) else ():
        head = str(note).split(" ", 2)
        if len(head) > 1 and head[0] == f"{RED_ON_BASE}:" and head[1] not in out:
            out.append(head[1])
    return out


@dataclass
class GuardCheck:
    """`regression_check`'s result: blocking findings, the observed state of every rerun
    criterion, and notes for a GREEN criterion this environment cannot run at all."""
    findings: list[str] = field(default_factory=list)
    observed: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    #: GDT-7: goal conflicts for Dan (`goal_conflicts.escalation` questions) and the ones a
    #: stored decision of his covers.
    questions: list[dict] = field(default_factory=list)
    accepted: list[dict] = field(default_factory=list)


def _imports_beside(repo: Path, test_file: str) -> list[str]:
    """Modules `test_file` imports from its own directory or below, as project paths."""
    try:
        tree = ast.parse((Path(repo) / test_file).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return []
    folder = PurePosixPath(test_file).parent
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level <= 1:
            if node.module:
                modules.append(node.module)
            else:
                modules += [a.name for a in node.names]
    out = []
    for module in modules:
        stem = folder.joinpath(*module.split("."))
        for path in (f"{stem}.py", f"{stem}/__init__.py"):
            if (Path(repo) / path).is_file() and path != test_file:
                out.append(path)
                break
    return out


def _bound_paths(criterion: doc_goals.Criterion, repo: Path) -> list[str]:
    """The project paths a criterion's result rests on: each `.py` file or directory its
    `Test:` names (a directory only as a pytest argument, never an option's value), the
    modules those test files import from beside them, and its `Fixtures:`/`Data:` path."""
    try:
        argv = shlex.split(criterion.test.strip().strip("`"))
    except ValueError:
        argv = []
    pytest_run = "-mpytest" in argv or any(Path(a).name in ("pytest", "py.test") for a in argv)
    out = []
    for i, arg in enumerate(argv):
        path = arg.split("::", 1)[0].removeprefix("./").rstrip("/")
        if not path or path.startswith(("-", "/")) or (i and argv[i - 1] in _PYTEST_VALUES):
            continue
        if path.endswith(".py"):
            out += [path, *_imports_beside(repo, path)]
        elif pytest_run and path != "." and (Path(repo) / path).is_dir():
            out.append(path)
    for value in (criterion.fixtures, criterion.data):
        first = value.strip().split(" ", 1)[0].strip("`'\",;:()").removeprefix("./") \
            .rstrip("/") if value.strip() else ""
        if first and not first.startswith(("/", "~", "..")) and (Path(repo) / first).exists():
            out.append(first)
    return list(dict.fromkeys(out))


def _named(doc: doc_goals.Goals, tests: tuple[str, ...]) -> set[str]:
    """The criteria a task's `tests:` names, by AC id or by its binding."""
    wanted = {t.strip("`").strip().removeprefix("pytest ").strip() for t in tests}
    return {c.id for c in doc.criteria
            if c.id in {t.upper() for t in wanted}
            or c.test.strip("`").strip().removeprefix("pytest ").strip() in wanted}


def _rerun(doc: doc_goals.Goals, baseline: Mapping[str, str], task: Mapping[str, Any]
           ) -> tuple[list[str], set[str]]:
    """The criteria the gate reruns for `task` (protected GREEN, named by its `tests:`, or
    named in its `criteria:`, GDT-9) and the ones its `tests:` names (GDT-7's `advanced`
    reads only those); none without a baseline."""
    if not baseline:
        return [], set()
    named = _named(doc, doc_goals.task_tests(task) or ())
    linked = set(doc_goals.normalise_criteria(task.get("criteria"))[0])
    return [c.id for c in doc.criteria
            if baseline.get(c.id) == acceptance.GREEN or c.id in named or c.id in linked], named


def reruns(repo: Path, doc: doc_goals.Goals, task: Mapping[str, Any]) -> list[str]:
    """The criteria the gate reruns for `task`: the gate runs before the review, so these
    are the ones it already judged on the tree the reviewer sees (GDT-7)."""
    from maestro import goal_state

    return _rerun(doc, goal_state.protected_states(repo, doc), task)[0]


def policy(repo: Path, task: Mapping[str, Any], base: str,
           decisions: Optional[list] = None) -> Optional[dict]:
    """What the gate checks for `task` against `base`; `None` without `docs/GOALS.md`.
    `decisions`: Dan's goal decisions (default: the store's)."""
    doc = doc_goals.load(repo)
    if doc is None:
        return None
    from maestro import goal_conflicts, goal_state

    results = acceptance.current(acceptance.load(repo), doc)
    # GDT-9 (C12): a criterion an earlier task turned GREEN is protected like a baseline
    # GREEN one. It has no stored measurement, so a metric conflict on it is unmeasured
    # (asked again, GDT-7's safe path): its baseline value is not the GREEN one.
    baseline = goal_state.protected_states(repo, doc)
    values = {i: r.value for i, r in results.items() if r.value is not None
              and not (baseline.get(i) == acceptance.GREEN and r.state != acceptance.GREEN)}
    notes = []
    if not baseline:
        notes.append(f"no acceptance baseline at contract revision {doc.revision}: no "
                     "criterion is protected until `maestro goals baseline` runs")
    rerun, named = _rerun(doc, baseline, task)
    bound: dict[str, list[str]] = {}
    for c in doc.criteria:
        for path in _bound_paths(c, repo):
            bound.setdefault(path, []).append(c.id)
    raw = task.get("test_corrections") or []
    corrections = [str(c).strip().upper() for c in (raw if isinstance(raw, list) else [raw])]
    return {"base": base, "revision": doc.revision, "baseline": baseline, "rerun": rerun,
            # GDT-9 review M4: GREEN only by an observation since the baseline, not at it.
            "observed_only": [c.id for c in doc.criteria
                              if baseline.get(c.id) == acceptance.GREEN
                              and (results.get(c.id) is None
                                   or results[c.id].state != acceptance.GREEN)],
            "bound_files": bound, "corrections": corrections, "notes": notes,
            # GDT-7: what a GREEN→RED is judged against when the task advanced a goal.
            # GDT-11 (R41): a task serving no goal (`serves: []`, polish) advances none, so
            # a regression it causes blocks; it is never weighed as a trade.
            "named": [] if doc_goals.task_serves(task) == () else
                     [c.id for c in doc.criteria if c.id in named], "values": values,
            "conflicts": goal_conflicts.context(
                doc, str(task.get("id") or ""),
                goal_conflicts.load(repo) if decisions is None else decisions)}


class _DiffUnavailable(RuntimeError):
    """A diff the guard needs failed: its result is unknown, never "nothing added"."""


def _diff(tree: Path, args: list[str]) -> str:
    proc = validation.git(["diff", "--no-renames", *args], cwd=tree)
    if getattr(proc, "returncode", 1) != 0:
        raise _DiffUnavailable(" ".join(args))
    return proc.stdout or ""


def _added_lines(tree: Path, base: str, path: str) -> list[str]:
    # From the fork point, as `tamper_findings`' change set (GDT-14 review M2b).
    return [line[1:] for line in _diff(tree, [f"{base}...HEAD", "--", path]).splitlines()
            if line.startswith("+") and not line.startswith("+++")]


def _disables(tree: Path, base: str, path: str) -> bool:
    return any(_DISABLES.search(line) for line in _added_lines(tree, base, path))


def _above(conftest: str, bound: str) -> bool:
    folder = PurePosixPath(conftest).parent
    return folder == PurePosixPath(".") or folder in PurePosixPath(bound).parents


def _binds(path: str, bound: Mapping[str, list[str]]) -> list[str]:
    """The criteria whose bound paths include `path` itself or a directory above it."""
    ids: set[str] = set()
    for key, criteria in bound.items():
        if path == key or PurePosixPath(key) in PurePosixPath(path).parents:
            ids.update(criteria)
    return sorted(ids)


def _added_files(tree: Path, base: str) -> set[str]:
    out = _diff(tree, ["--name-only", "--diff-filter=A", f"{base}...HEAD"])
    return {line.strip() for line in out.splitlines() if line.strip()}


def tamper_findings(tree: Path, policy: Mapping[str, Any]) -> list[str]:
    """Blocking findings for a diff that changes the contract or its acceptance tests."""
    base = policy.get("base")
    try:
        changed = validation.changed_paths(tree, base, since_fork=True) if base else None
        added = _added_files(tree, base) if base else set()
    except Exception:  # An unreadable diff is a gate finding, never permission to proceed.
        changed = None
    unavailable = ["goals_guard_diff_unavailable: cannot establish the committed changes "
                   "against the run base, so the acceptance tests cannot be shown untouched"]
    if changed is None:
        return unavailable
    try:
        return _tamper_findings(tree, policy, base, changed, added)
    except _DiffUnavailable:   # a per-file diff failed: not "nothing added" (GDT-14 verify)
        return unavailable


def _tamper_findings(tree: Path, policy: Mapping[str, Any], base: str,
                     changed: tuple[str, ...], added: set[str]) -> list[str]:
    out = []
    bound = policy.get("bound_files") or {}
    corrections = set(policy.get("corrections") or ())
    for path in sorted(changed):
        ids = [] if path in added else _binds(path, bound)
        name = PurePosixPath(path).name
        if path == doc_goals.DOC:
            out.append(f"goals_contract_changed: {doc_goals.DOC} is the project's goals "
                       "contract and only Dan changes it (a new contract revision); revert "
                       "this change and report what the contract should say instead")
        elif ids:
            if not (Path(tree) / path).exists():
                out.append(f"acceptance_test_deleted: {path} binds {', '.join(ids)}; restore "
                           "it: an acceptance test is never deleted by a task")
            elif _disables(tree, base, path):
                out.append(f"acceptance_test_disabled: {path} adds a skip or xfail; remove "
                           "it: a skip or xfail is never a test correction")
            elif missing := [i for i in ids if i not in corrections]:
                unnamed = (f" (not named in test_corrections: {', '.join(missing)})"
                           if corrections else "")
                out.append(f"acceptance_test_changed: {path} binds {', '.join(ids)}{unnamed}; "
                           "restore it, or have the task author name each criterion it "
                           "corrects in test_corrections with the reason")
        elif name == "conftest.py":
            below = [b for b in bound if _above(path, b)]
            if below and _disables(tree, base, path):
                out.append(f"acceptance_test_disabled: {path} adds a skip or xfail above "
                           f"{', '.join(below)}; remove it: a skip or xfail is never a test "
                           "correction")
        elif name in _PYTEST_CONFIG and any(_above(path, b) for b in bound):
            changes = _selection_changes(tree, base, path)
            if changes:
                out.append(f"acceptance_test_disabled: {path} adds test selection "
                           f"({'; '.join(changes)}); remove it: a task never changes which "
                           "tests run")
    return out


def _pytest_options(text: str, name: str) -> dict[str, str]:
    """The pytest section of a config file as `{option: value}`; `{}` when unreadable."""
    try:
        if name.endswith(".toml"):
            import tomllib

            data = tomllib.loads(text)
            if name == "pyproject.toml":   # pytest 9: `[tool.pytest]` or its `ini_options`
                tool = data.get("tool", {}).get("pytest", {})
                section = {**{k: v for k, v in tool.items() if k != "ini_options"},
                           **tool.get("ini_options", {})}
            else:                          # `pytest.toml`, `.pytest.toml`: `[pytest]`
                section = data.get("pytest", {})
            return {k: " ".join(map(str, v)) if isinstance(v, list) else str(v)
                    for k, v in section.items()}
        import configparser

        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.read_string(text)
        wanted = "tool:pytest" if name == "setup.cfg" else "pytest"
        return dict(parser[wanted]) if parser.has_section(wanted) else {}
    except Exception:   # an unparseable config selects nothing pytest can read either
        return {}


def _selection_changes(tree: Path, base: str, path: str) -> list[str]:
    """The pytest options the diff changes that select or skip tests."""
    name = PurePosixPath(path).name
    shown = validation.git(["show", f"{base}:{path}"], cwd=tree)
    before = _pytest_options(shown.stdout if shown.returncode == 0 else "", name)
    target = Path(tree) / path
    after = _pytest_options(target.read_text(encoding="utf-8") if target.is_file() else "",
                            name)
    out = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key, ""), after.get(key, "")
        if old == new:
            continue
        if (key in _SELECTION_KEYS or (key == "addopts" and _SELECTION.search(new))
                or _DISABLES.search(new)):
            out.append(f"{key} = {new}".strip())
    return out


def _run_on_base(tree: Path, base: str, criteria: list[doc_goals.Criterion], revision,
                 runner) -> Optional[dict[str, str]]:
    """Each criterion's state on a detached checkout of `base` in this environment, or
    `None` when the base cannot be checked out."""
    scratch = Path(tempfile.mkdtemp(prefix="maestro-goals-base-"))
    checkout = scratch / "base"
    try:
        if validation.git(["worktree", "add", "--detach", str(checkout), base],
                          cwd=tree).returncode != 0:
            return None
        venv = Path(tree) / ".venv"
        if venv.exists() and not (checkout / ".venv").exists():
            os.symlink(venv.resolve(), checkout / ".venv")
        return {c.id: acceptance.run_criterion(checkout, c, revision=revision,
                                               runner=runner).state for c in criteria}
    finally:
        if checkout.exists():
            validation.git(["worktree", "remove", "--force", str(checkout)], cwd=tree)
        shutil.rmtree(scratch, ignore_errors=True)
        validation.git(["worktree", "prune"], cwd=tree)


def regression_check(tree: Path, policy: Mapping[str, Any], runner=subprocess.run
                     ) -> GuardCheck:
    """Rerun `policy["rerun"]` on the task's tree: a finding for each criterion GREEN at
    the baseline that the task made worse, a note for one this environment cannot run on
    the base either, and the observed state of every rerun one."""
    check = GuardCheck()
    doc = doc_goals.load(tree)
    if doc is None or not policy.get("rerun"):
        return check
    by_id = {c.id: c for c in doc.criteria}
    baseline = policy.get("baseline") or {}
    revision = policy.get("revision")
    lost: list[tuple[doc_goals.Criterion, acceptance.Result]] = []
    for cid in policy["rerun"]:
        criterion = by_id.get(cid)
        if criterion is None:   # the contract changed: `tamper_findings` reports it
            continue
        result = acceptance.run_criterion(tree, criterion, revision=doc.revision,
                                          runner=runner)
        check.observed[cid] = result.state
        if baseline.get(cid) == acceptance.GREEN and result.state != acceptance.GREEN:
            lost.append((criterion, result))
    # GDT-9 review M4: a RED of a criterion protected only by an observation (a flaky pass,
    # or the target broken since) is compared with the base too; RED there as well, the
    # target is already RED, so it is a note, not this task's regression. A RED of a
    # baseline GREEN one blocks outright (GDT-6).
    observed_only = set(policy.get("observed_only") or ())

    def compared(criterion: doc_goals.Criterion, result: acceptance.Result) -> bool:
        return result.state != acceptance.RED or criterion.id in observed_only

    unrunnable = [c for c, r in lost if compared(c, r)]
    base = policy.get("base")
    on_base = (_run_on_base(tree, base, unrunnable, doc.revision, runner)
               if unrunnable and base else ({} if not unrunnable else None))
    verified: list[tuple[doc_goals.Criterion, acceptance.Result, str]] = []
    for criterion, result in lost:
        base_state = None if on_base is None else on_base.get(criterion.id)
        if result.state == acceptance.RED and criterion.id in observed_only \
                and base_state == acceptance.RED:
            check.notes.append(
                f"{RED_ON_BASE}: {criterion.id} was observed {acceptance.GREEN} "
                f"since the baseline at contract revision {revision} (not at it), but is "
                f"{acceptance.RED} on the task's tree and on the base: the target is already "
                f"{acceptance.RED}, so it is no regression of this task: {result.reason}")
            continue
        if result.state != acceptance.RED and base_state not in (None, acceptance.GREEN):
            check.notes.append(
                f"{criterion.id} was {acceptance.GREEN} at contract revision {revision} but "
                f"is {result.state} on the task's tree and on the base in this environment "
                f"({base_state} there): the environment cannot run it, so it is no "
                f"regression of this task: {result.reason}")
            continue
        goals = ", ".join(doc_goals.entry_goals(doc, criterion)) or "no goal"
        why = {acceptance.GREEN: "; it is still green on the base in this environment, so "
                                 "the task's change caused it",
               None: "; the base could not be checked out to compare"}.get(
            base_state, "") if compared(criterion, result) else ""
        verified.append((criterion, result, (
            f"acceptance_regression: {criterion.id} ({goals}) was {acceptance.GREEN} at "
            f"contract revision {revision}, now {result.state}: {result.reason}{why}. "
            "Rule: no change may make a project goal worse; restore it "
            f"(`{criterion.test}`)")))
    _judge_conflicts(check, doc, verified, policy)
    return check


def _judge_conflicts(check: GuardCheck, doc: doc_goals.Goals,
                     verified: list[tuple[doc_goals.Criterion, acceptance.Result, str]],
                     policy: Mapping[str, Any]) -> None:
    """GDT-7 (R48, R51): each verified regression goes through `goal_conflicts.judge`,
    with the goals a task-named criterion turned GREEN as `advanced`. A regression on a
    goal other than one that advanced is Dan's question; one his stored decision names is
    covered (or blocks, or is asked again) whatever advanced, nothing included; a hard
    invariant blocks whatever advanced; anything else keeps GDT-6's finding."""
    from maestro import goal_conflicts

    ctx = policy.get("conflicts")
    baseline = policy.get("baseline") or {}
    advanced: dict[str, list[str]] = {}
    by_id = {c.id: c for c in doc.criteria}
    for cid in policy.get("named") or ():
        if cid in by_id and baseline.get(cid) != acceptance.GREEN \
                and check.observed.get(cid) == acceptance.GREEN:
            for goal in doc_goals.entry_goals(doc, by_id[cid]):
                advanced.setdefault(goal, []).append(cid)
    if not isinstance(ctx, Mapping) or not verified:
        check.findings.extend(finding for _, _, finding in verified)
        return
    values = policy.get("values") or {}
    regressions: dict[str, list[goal_conflicts.Regression]] = {}
    for criterion, result, _ in verified:
        regressions[criterion.id] = [
            goal_conflicts.Regression(goal, criterion.id, value=result.value,
                                      baseline=values.get(criterion.id),
                                      evidence=result.reason[:200], state=result.state)
            for goal in doc_goals.entry_goals(doc, criterion)]
    judgement = goal_conflicts.judge(
        [r for regs in regressions.values() for r in regs], advanced, ctx, stage="review",
        place="the gate (review stage)")
    invariants = ctx.get("invariants") or {}
    for criterion, _, finding in verified:
        regs = regressions[criterion.id]
        # GDT-6's finding stays for a criterion of no goal, a plain regression and a hard
        # invariant (beside its `hard_invariant_regression`): only Dan's conflicts replace it.
        if not regs or criterion.id in invariants or any(r in judgement.plain for r in regs):
            check.findings.append(finding)
    check.findings.extend(judgement.blocking)
    check.questions.extend(judgement.questions)
    check.accepted.extend(judgement.accepted)


def regression_findings(tree: Path, policy: Mapping[str, Any], runner=subprocess.run
                        ) -> tuple[list[str], dict[str, str]]:
    """`regression_check`'s findings and observed states (notes dropped)."""
    check = regression_check(tree, policy, runner=runner)
    return check.findings, check.observed
