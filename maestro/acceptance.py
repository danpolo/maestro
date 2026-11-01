"""Outer acceptance tests: binding each `AC-` to a run, and the setup baseline (P13 GDT-3).

The goals contract (`maestro.docs.goals`) names what the finished product must do; each
acceptance criterion's `Test:` binds it to a runnable command (`` `pytest tests/x.py::t` ``,
`` `make accept` ``) or to an existing test (`tests/x.py::t`). Setup runs every binding
for real and stores the baseline; later items read it (GDT-4 turns RED into ROADMAP tasks,
GDT-6 flags GREEN turning RED, GDT-9/10 track and close the goals).

Five states (`01` §3.2), and only an assertion failure is a credible RED:

- `red_expected_missing_behavior`: the harness ran and an assertion failed.
- `green_already_satisfied`: it passed; it stays as regression protection.
- `blocked_environment`: the harness is broken, not the product: an import error, a
  missing tool, nothing collected, a fixture or service that failed during setup.
- `operator_only`: an `OP-` check; recorded, never an automated pass (R14).
- `unknown`: too little evidence to say: no test bound, a timeout, an exception that is
  neither an assertion nor the environment, a failing non-pytest command, a metric with
  no number or no direction.

pytest results are read from its own JUnit XML, never from scraped console text.

Safety (R19): one command, argv only, never a shell; no argument may point outside the
project; the environment is the caller's minus every variable whose name says secret
(`*_KEY`, `*TOKEN*`, `*SECRET*`, `*PASSWORD*`, `*CREDENTIAL*`, `*AUTH*`), so a contract
test cannot reach a paid API on Dan's credentials; every run is bounded by a timeout.
There is no network sandbox: a test that needs a service it cannot reach is
`blocked_environment`, which is the honest result.

A stored result carries the contract revision it ran at; at a newer revision it is stale
and closes nothing (R67).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from maestro.docs import goals as doc_goals

RED = "red_expected_missing_behavior"
GREEN = "green_already_satisfied"
BLOCKED = "blocked_environment"
OPERATOR = "operator_only"
UNKNOWN = "unknown"
STATES = (RED, GREEN, BLOCKED, OPERATOR, UNKNOWN)

#: The stored baseline, relative to the project root.
BASELINE = ".orchestrator/acceptance_baseline.json"
DEFAULT_TIMEOUT = 300
_REASON_CHARS = 500

#: An exception from the environment, not the product: the harness is not credible yet.
_ENVIRONMENT_ERRORS = frozenset({
    "ImportError", "ModuleNotFoundError", "FileNotFoundError", "PermissionError", "OSError",
    "ConnectionError", "ConnectionRefusedError", "ConnectionResetError",
    "ConnectionAbortedError", "TimeoutError", "gaierror",
})
_SECRET_NAME = re.compile(
    r"(_KEY$|^KEY$|APIKEY|TOKEN|SECRET|PASS|CREDENTIAL|AUTH|DSN|DATABASE_URL)", re.I)
_SHELL = re.compile(r"[;|&<>`$]|\$\(")
#: A binding is one project command (R19): never a shell, never inline code.
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})
#: Commands that run the next argument as the program.
_WRAPPERS = frozenset({"env", "timeout", "nice", "nohup", "command", "exec"})
#: A wrapper's options whose value is the next argument, and a `timeout` duration.
_WRAPPER_VALUES = frozenset({"-s", "--signal", "-k", "--kill-after", "-u", "--unset", "-C",
                             "--chdir", "-n", "--adjustment"})
#: Per wrapper, the short-option letters that take a value (joined, or the next argument).
_WRAPPER_VALUE_LETTERS = {"env": "uC", "timeout": "sk", "nice": "n"}
#: Per wrapper, its GNU long options and whether each takes a value (joined by `=`, or the
#: next argument); `split` splits a command line. GNU takes any unique prefix of one.
_WRAPPER_LONG = {
    "env": {"ignore-environment": False, "null": False, "unset": True, "chdir": True,
            "split-string": "split", "block-signal": False, "default-signal": False,
            "ignore-signal": False, "list-signal-handling": False, "debug": False,
            "help": False, "version": False},
    "timeout": {"foreground": False, "kill-after": True, "preserve-status": False,
                "signal": True, "verbose": False, "help": False, "version": False},
    "nice": {"adjustment": True, "help": False, "version": False},
}
_DURATION = re.compile(r"\d+(?:\.\d+)?[smhd]?")
#: An interpreter; per kind, the short options that run inline code and those that take a
#: value (joined, or the next argument), and the long options that take the next one.
_INTERPRETER = re.compile(r"^(?:(?P<py>python|pypy)\d*(?:\.\d+)?|(?P<other>node|ruby|perl))$")
_INLINE_LETTERS = {"python": "c", "node": "ep", "ruby": "e", "perl": "eE"}
_VALUE_LETTERS = {"python": "WX", "node": "Cr", "ruby": "CEIr", "perl": "IMm"}
_VALUE_OPTIONS = frozenset({"--check-hash-based-pycs", "--require", "--import", "--loader",
                            "--experimental-loader"})
_NODE_ID = re.compile(r"^[\w./-]+\.py(::[\w\[\]-]+)*$")
#: A standalone number: the "95" of "p95" is part of a name, not a value. Thousands
#: separators are not read (`2,000` is 2).
_NUMBER = re.compile(r"(?<![\w.])-?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?")
#: A direction phrase: an optional negation, an optional "(at) or", the direction word and
#: an optional "or equal". "At or above" / "above or equal to" are inclusive (GDT-14); a
#: negated word is its opposite, inclusive bound: "not less than" is "at least" (review M3).
_PHRASE = re.compile(
    r"\b(?:(?P<neg>not|no)\s+)?(?P<pre>(?:at\s+)?or\s+)?"
    r"(?P<word>under|below|less\s+than|at\s+most|within|over|above|more\s+than|"
    r"greater\s+than|at\s+least)(?P<eq>\s+or\s+equal)?\b|(?P<op><=|<|≤|>=|>|≥)", re.I)
_LOWER_WORDS = frozenset({"under", "below", "less than", "at most", "within"})
_INCLUSIVE_WORDS = frozenset({"at most", "within", "at least"})
#: A negation the phrase did not take in ("must never be above") leaves the text unread.
_NEGATION = re.compile(r"\b(?:not|no|never)\b|n't\b", re.I)


@dataclass(frozen=True)
class Result:
    id: str
    state: str
    reason: str
    revision: Optional[int] = None
    goals: tuple[str, ...] = ()
    scenarios: tuple[str, ...] = ()
    command: tuple[str, ...] = ()
    exit_code: Optional[int] = None
    seconds: float = 0.0
    #: A metric criterion's measured value.
    value: Optional[float] = None


def interpreter(repo: Path) -> str:
    """The project's Python: its `.venv` when it has one, else the one running maestro."""
    venv = Path(repo) / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def safe_env(repo: Path, environ: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """The caller's environment minus anything secret-named, the project `.venv` first."""
    env = {k: v for k, v in (os.environ if environ is None else environ).items()
           if not _SECRET_NAME.search(k)}
    venv_bin = Path(repo) / ".venv" / "bin"
    if venv_bin.is_dir():
        env["PATH"] = os.pathsep.join(p for p in (str(venv_bin), env.get("PATH", "")) if p)
    return env


def _outside(repo: Path, value: str) -> bool:
    path = Path(value)
    resolved = (path if path.is_absolute() else Path(repo) / path).resolve()
    return not resolved.is_relative_to(Path(repo).resolve())


def _wrapper_option(wrapper: str, arg: str) -> tuple[bool, int]:
    """Whether a wrapper option splits a command line (`env -S`, joined in a cluster too,
    `--split-string` or any unique prefix of it), and how many arguments it spans."""
    if arg.startswith("--") and arg != "--":
        name, joined, _ = arg[2:].partition("=")
        options = _WRAPPER_LONG.get(wrapper, {})
        found = [o for o in options if o == name] or [o for o in options if o.startswith(name)]
        if len(found) != 1:   # unknown or ambiguous: the wrapper refuses it, nothing runs
            return False, 1
        kind = options[found[0]]
        if kind == "split":
            return True, 1
        return False, 2 if kind and not joined else 1
    if arg.startswith("-") and not arg.startswith("--"):
        values = _WRAPPER_VALUE_LETTERS.get(wrapper, "")
        for i, letter in enumerate(arg[1:], 1):
            if wrapper == "env" and letter == "S":
                return True, 1
            if letter in values:
                return False, 2 if i == len(arg) - 1 else 1
        return False, 1
    return False, 2 if arg in _WRAPPER_VALUES else 1


def _inline(argv: Sequence[str]) -> bool:
    """Whether an interpreter's own options (before its script or `-m`) run inline code:
    `python -c`, `python -Sc`, `python -cprint(1)`, `node -e`, `perl -E`, `--eval`. A short
    option cluster is read as the interpreter reads it: an option that takes a value ends
    the cluster (`-Wignore`) or takes the next argument (`-X dev`), and Python's `-m`
    names the module that runs (`-mpytest`)."""
    found = _INTERPRETER.match(Path(argv[0]).name)
    if found is None:
        return False
    kind = "python" if found.group("py") else found.group("other")
    inline, values = _INLINE_LETTERS[kind], _VALUE_LETTERS[kind]
    args = iter(argv[1:])
    for arg in args:
        if arg in ("--eval", "--print") or arg.startswith(("--eval=", "--print=")):
            return True
        if arg in _VALUE_OPTIONS:
            next(args, None)
            continue
        if arg in ("-", "--") or not arg.startswith("-"):
            return False
        if arg.startswith("--"):
            continue
        for n, letter in enumerate(arg[1:], 1):
            if not letter.isalpha():   # a joined value (`-W:no-deprecated`) has begun
                break
            if letter in inline:
                return True
            if kind == "python" and letter == "m":
                return False
            if letter in values:
                if n == len(arg) - 1:
                    next(args, None)
                break
    return False


def command_for(criterion: doc_goals.Criterion, repo: Path, *,
                project: Optional[Path] = None) -> tuple[tuple[str, ...], str]:
    """A criterion's `Test:` as argv, or `()` and why it cannot run. `python`/`pytest` run
    with `project`'s interpreter (default `repo`; a task worktree has no `.venv`)."""
    raw = criterion.test.strip().strip("`").strip()
    if not raw:
        return (), "no test bound yet"
    if _SHELL.search(raw):
        return (), f"Test {raw!r} must be one command, no shell"
    try:
        argv = shlex.split(raw)
    except ValueError as exc:
        return (), f"Test {raw!r} does not parse: {exc}"
    if len(argv) == 1 and _NODE_ID.match(argv[0]):
        argv = ["pytest", *argv]
    program = 0
    while Path(argv[program]).name in _WRAPPERS and program + 1 < len(argv):
        wrapper = Path(argv[program]).name
        program += 1
        while argv[program].startswith("-") or "=" in argv[program] or _DURATION.fullmatch(
                argv[program]):
            split, step = _wrapper_option(wrapper, argv[program])
            if split:
                return (), f"Test {raw!r} runs a shell-split command line; bind one command"
            if program + 1 == len(argv):
                break
            program = min(program + step, len(argv) - 1)
    if Path(argv[program]).name in _SHELLS:
        return (), f"Test {raw!r} runs a shell; bind one project command instead"
    if _inline(argv[program:]):
        return (), f"Test {raw!r} runs inline code; bind a project file instead"
    for i, arg in enumerate(argv):
        candidate = arg.split("::", 1)[0].split("=", 1)[-1]
        if i == 0 and Path(candidate).is_absolute():
            continue   # an installed command, not a project path
        if ("/" in candidate or candidate.startswith(("~", ".."))) and _outside(
                repo, os.path.expanduser(candidate)):
            return (), f"Test argument {arg!r} is outside the project"
    python = interpreter(repo if project is None else project)
    if argv[0] in ("pytest", "py.test"):
        argv = [python, "-m", "pytest", *argv[1:]]
    elif argv[0] in ("python", "python3"):
        argv = [python, *argv[1:]]
    return tuple(argv), ""


def _is_pytest(argv: Sequence[str]) -> bool:
    return len(argv) >= 3 and argv[1:3] == ("-m", "pytest")


def safety_problems(criterion: doc_goals.Criterion, repo: Path) -> list[str]:
    """Fixture or data paths outside the project (R19): a contract test uses safe data
    that lives with the project, never a real export or a production store."""
    out: list[str] = []
    for label, value in (("fixtures", criterion.fixtures), ("data", criterion.data)):
        first = value.strip().split(" ", 1)[0] if value.strip() else ""
        if first and ("/" in first or first.startswith((".", "~"))) and _outside(
                repo, os.path.expanduser(first)):
            out.append(f"{criterion.id}: {label} {first} are outside the project")
    return out


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _REASON_CHARS else text[:_REASON_CHARS - 1] + "…"


def _failure_kind(message: str) -> str:
    """RED for an assertion, BLOCKED for the environment, UNKNOWN for anything else."""
    head = message.strip()
    if head.startswith(("assert ", "AssertionError", "Failed:")):
        return RED
    name = re.match(r"([\w.]+?)(?::|$)", head)
    exc = name.group(1).rsplit(".", 1)[-1] if name else ""
    return BLOCKED if exc in _ENVIRONMENT_ERRORS else UNKNOWN


def classify_pytest(exit_code: int, junit: str) -> tuple[str, str]:
    """A pytest run's state and reason, from its exit code and JUnit XML."""
    if exit_code in (2, 3, 4, 5) or not junit:
        what = {2: "interrupted (collection error)", 3: "internal error", 4: "usage error",
                5: "no tests collected"}.get(exit_code, "no report")
        return BLOCKED, f"pytest exit {exit_code}: {what}"
    try:
        cases = list(ET.fromstring(junit).iter("testcase"))
    except ET.ParseError as exc:
        return UNKNOWN, f"unreadable pytest report: {exc}"
    found: dict[str, list[str]] = {RED: [], BLOCKED: [], UNKNOWN: []}
    passed = 0
    for case in cases:
        name = case.get("name", "?")
        outcome = next((c for c in case if c.tag in ("failure", "error", "skipped")), None)
        if outcome is None:
            passed += 1
        elif outcome.tag == "skipped":
            found[UNKNOWN].append(f"{name} skipped: {outcome.get('message', '')}")
        elif outcome.tag == "error":
            found[BLOCKED].append(f"{name}: {outcome.get('message', '')}")
        else:
            message = outcome.get("message", "")
            kind = _failure_kind(message)
            found[kind].append(f"{name}: assertion failed: {message}" if kind == RED
                               else f"{name}: {message}")
    for state in (BLOCKED, UNKNOWN, RED):
        if found[state]:
            return state, "; ".join(found[state])
    if passed and exit_code == 0:
        return GREEN, "passed"
    return UNKNOWN, f"pytest exit {exit_code} with no failing test in its report"


def _direction(criterion: doc_goals.Criterion) -> Optional[tuple[str, bool]]:
    """Which side of the threshold passes, and whether the threshold itself passes."""
    for text in (criterion.threshold, criterion.oracle):
        found = [_read_phrase(m) for m in _PHRASE.finditer(text)]
        negated = sum(1 for m in _PHRASE.finditer(text) if m.group("neg"))
        if not found or len(_NEGATION.findall(text)) > negated:
            continue
        if len({direction for direction, _ in found}) == 1:
            return found[0]
    return None


def _read_phrase(match: re.Match) -> tuple[str, bool]:
    if match.group("op"):
        op = match.group("op")
        return ("below" if op in "<=≤" else "above"), op in ("<=", "≤", ">=", "≥")
    word = " ".join(match.group("word").lower().split())
    below = word in _LOWER_WORDS
    inclusive = word in _INCLUSIVE_WORDS or bool(match.group("pre") or match.group("eq"))
    if match.group("neg"):
        below, inclusive = not below, not inclusive
    return ("below" if below else "above"), inclusive


def classify_metric(criterion: doc_goals.Criterion, stdout: str
                    ) -> tuple[str, str, Optional[float]]:
    """A metric's state from the number on the last line it printed (C15)."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    found = _NUMBER.search(lines[-1]) if lines else None
    if found is None:
        return UNKNOWN, "the measurement printed no number", None
    value = float(found.group())
    bound = _NUMBER.search(criterion.threshold)
    if bound is None:
        return UNKNOWN, f"measured {value:g}; the threshold has no number", value
    found_direction = _direction(criterion)
    if found_direction is None:
        return UNKNOWN, (f"measured {value:g}; neither the threshold nor the oracle says "
                         "which direction passes (ask Dan)"), value
    direction, inclusive = found_direction
    limit = float(bound.group())
    if value == limit:
        ok = inclusive
    else:
        ok = value < limit if direction == "below" else value > limit
    rule = f"{direction} {limit:g}" + (" or equal" if inclusive else "")
    return (GREEN if ok else RED), f"measured {value:g}, must be {rule}", value


Runner = Callable[..., subprocess.CompletedProcess]


def run_criterion(repo: Path, criterion: doc_goals.Criterion, *, revision: Optional[int],
                  timeout: int = DEFAULT_TIMEOUT, runner: Runner = subprocess.run,
                  project: Optional[Path] = None) -> Result:
    """Run one criterion's binding in the project and classify what happened. `project`
    (default `repo`) gives the interpreter and environment: a task worktree's check runs
    with its controller's, as the finish run does."""
    repo = Path(repo)
    project = repo if project is None else Path(project)
    base = dict(id=criterion.id, revision=revision, goals=criterion.goals,
                scenarios=criterion.scenarios)
    argv, problem = command_for(criterion, repo, project=project)
    if not argv:
        return Result(state=UNKNOWN if problem == "no test bound yet" else BLOCKED,
                      reason=problem, **base)
    pytest_run = _is_pytest(argv) and criterion.evidence != "metric"
    with tempfile.TemporaryDirectory(prefix="maestro-acceptance-") as scratch:
        report = Path(scratch) / "junit.xml"
        command = [*argv, "-p", "no:cacheprovider", f"--junitxml={report}"] if pytest_run \
            else list(argv)
        started = time.monotonic()
        try:
            # No bytecode: the gate reruns criteria on a task's tree, where a `__pycache__/`
            # is an uncommitted change that refuses the next gate attempt (GDT-6).
            proc = runner(command, cwd=str(repo),
                          env={**safe_env(project), "PYTHONDONTWRITEBYTECODE": "1"},
                          capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return Result(state=UNKNOWN, reason=f"timed out after {timeout}s",
                          command=tuple(argv), seconds=float(timeout), **base)
        except (FileNotFoundError, PermissionError) as exc:
            return Result(state=BLOCKED, reason=f"command not found or not runnable: {exc}",
                          command=tuple(argv), **base)
        seconds = round(time.monotonic() - started, 3)
        junit = report.read_text(encoding="utf-8") if report.is_file() else ""
    done = dict(command=tuple(argv), exit_code=proc.returncode, seconds=seconds, **base)
    if criterion.evidence == "metric":
        if proc.returncode != 0:
            return Result(state=BLOCKED, reason=_clip(
                f"exit {proc.returncode}: the measurement did not run: {proc.stderr}"), **done)
        state, reason, value = classify_metric(criterion, proc.stdout or "")
        return Result(state=state, reason=reason, value=value, **done)
    if pytest_run:
        state, reason = classify_pytest(proc.returncode, junit)
    elif proc.returncode == 0:
        state, reason = GREEN, "passed"
    elif proc.returncode == 127:
        state, reason = BLOCKED, "exit 127: command not found"
    else:
        state, reason = UNKNOWN, (f"exit {proc.returncode} from a non-pytest command: a "
                                  "missing behaviour and a broken harness look the same")
    return Result(state=state, reason=_clip(reason), **done)


def run_baseline(repo: Path, doc: doc_goals.Goals, *, timeout: int = DEFAULT_TIMEOUT,
                 runner: Runner = subprocess.run) -> list[Result]:
    """Every criterion run once, then every operator check recorded, in contract order."""
    out = [run_criterion(repo, c, revision=doc.revision, timeout=timeout, runner=runner)
           for c in doc.criteria]
    out += [Result(id=o.id, state=OPERATOR,
                   reason=f"{o.sign_off or 'Dan'}: {o.instructions}".strip(),
                   revision=doc.revision, goals=o.goals, scenarios=o.scenarios)
            for o in doc.operator_checks]
    return out


def automated_pass(result: Result) -> bool:
    """Whether a result is an automated pass: never for an operator check (R14)."""
    return result.state == GREEN and not result.id.startswith("OP-")


def path_for(repo: Path) -> Path:
    return Path(repo) / BASELINE


def _head(repo: Path) -> str:
    """The project's HEAD commit, `""` outside git (or without a commit)."""
    try:
        proc = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=str(repo),
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def store(repo: Path, doc: doc_goals.Goals, results: Sequence[Result]) -> Path:
    """Store the baseline with the revision and the commit it ran on (GDT-10: the
    completion gate compares the bound tests against that commit; `""` outside git)."""
    path = path_for(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"revision": doc.revision, "format": doc.format,
               "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "commit": _head(repo),
               "results": [asdict(r) for r in results]}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def _result(row: Mapping[str, Any]) -> Result:
    data = dict(row)
    for key in ("goals", "scenarios", "command"):
        data[key] = tuple(data.get(key) or ())
    return Result(**data)


def load(repo: Path) -> list[Result]:
    """The stored baseline, `[]` when there is none or it does not read."""
    try:
        payload = json.loads(path_for(repo).read_text(encoding="utf-8"))
        return [_result(row) for row in payload.get("results", [])]
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def current(results: Sequence[Result], doc: doc_goals.Goals) -> dict[str, Result]:
    """The results at the contract's current revision; an older one closes nothing (R67)."""
    return {r.id: r for r in results if r.revision == doc.revision}


def stale(results: Sequence[Result], doc: doc_goals.Goals) -> list[Result]:
    return [r for r in results if r.revision != doc.revision]


def controller(repo: Path) -> Path:
    """The checkout a linked git worktree was cut from (its main worktree), or `repo`
    itself (not a linked worktree, not a git checkout, or git cannot say)."""
    try:
        proc = subprocess.run(["git", "rev-parse", "--path-format=absolute",
                               "--git-common-dir"], cwd=str(repo), capture_output=True,
                              text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return Path(repo)
    common = Path(proc.stdout.strip()) if proc.returncode == 0 else None
    if common is None or common.name != ".git" or not common.parent.is_dir():
        return Path(repo)
    return common.parent


def main(argv: Optional[Sequence[str]] = None) -> int:
    """`python -m maestro.acceptance AC-02 [--credible]`: run one criterion's binding in the
    project it is run in (its `docs/GOALS.md`) and print its state. Exit 0 when it is
    GREEN, or with `--credible` when it is a credible RED or GREEN (the harness works);
    1 otherwise; 2 when the contract does not name it. A gate check for the tasks the
    goal replans and the completion gate propose (P13 GDT-10 review H3, L2): a metric is
    judged against its threshold, a pytest node from its JUnit report, and nothing is
    stored or written to the tree. Run in a linked worktree, the criterion is read from its
    main worktree's current `docs/GOALS.md` and runs with that checkout's interpreter; the
    tests run are the worktree's. For a task worktree that is what the finish run does
    (follow-up L-d). A project that is itself a linked worktree is judged against its main
    worktree's contract, not its own (DESIGN §19.10 L-d)."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m maestro.acceptance")
    parser.add_argument("criterion")
    parser.add_argument("--credible", action="store_true")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)
    repo = Path.cwd()
    project = controller(repo)
    doc = doc_goals.load(project) if project != repo else None
    if doc is None:
        project, doc = repo, doc_goals.load(repo)
    criterion = next((c for c in (doc.criteria if doc is not None else ())
                      if c.id == args.criterion), None)
    if criterion is None:
        print(f"{args.criterion}: not a criterion in {doc_goals.DOC}", file=sys.stderr)
        return 2
    result = run_criterion(repo, criterion, revision=doc.revision, timeout=args.timeout,
                           project=project)
    print(f"{criterion.id} {result.state}: {result.reason}")
    return 0 if result.state in ((RED, GREEN) if args.credible else (GREEN,)) else 1


if __name__ == "__main__":
    sys.exit(main())
