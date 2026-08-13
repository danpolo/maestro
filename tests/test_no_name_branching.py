"""The capabilities-not-names gate.

Maestro talks to more than one agent CLI. The rule that keeps that from rotting into a
pile of special cases is: **core code branches on a declared capability, never on which
backend it is talking to.** A driver advertises what it can do through
``capabilities()``; callers ask that object, not the name.

This module enforces the rule mechanically, on every ``.py`` file under ``maestro/``.

Two things it deliberately does **not** flag, because they are not branching:

* mentioning a backend name in a string, a docstring, a default value, a dict key, an
  argv list or a journal detail — ``BINARY = "claude"``, ``BACKENDS["codex"]``,
  ``name = "claude"`` are all fine;
* comparing against something that merely *contains* a backend name, such as a model id
  (``model == "claude-sonnet-4-6"``) — the match is on the whole string, exactly.

What it flags is dispatch: ``== "codex"``, ``!= "claude"``, ``in ("claude", "codex")``,
``match name: case "codex":`` and ``isinstance(x, ClaudeBackend)``-style type dispatch.

The check is AST-based rather than a regex so that a name inside a comment, a docstring
or an unrelated literal cannot trip it, and so a hit can be reported with the precise
node that caused it. A second test applies the sibling rule to the suite itself: a test
may not skip on a backend name, because that is name-branching wearing a test's clothes.

Both tests carry small self-tests over synthetic snippets, so a refactor that
accidentally neuters the visitor fails here rather than passing silently.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from maestro.backends.registry import BACKENDS

#: Repo root. Named ``REPO`` to match the house convention for ``Path``-valued globals.
REPO = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO / "maestro"
TESTS_DIR = REPO / "tests"

# Taken from the registry rather than hardcoded, so a backend added in a later stage is
# covered by this gate the moment it is registered.
BACKEND_NAMES = frozenset(str(name).strip().lower() for name in BACKENDS)
DRIVER_CLASSES = frozenset(ref.attr for ref in BACKENDS.values())

#: The one-line explanation attached to every failure.
RULE = (
    "capabilities, never names: dispatch on a flag declared by backend.capabilities(), "
    "not on which backend it happens to be"
)

# ---------------------------------------------------------------------------
# Allowlist. Two modules only, and both are on it for the same reason: turning a
# *name* into a driver is their entire job, so a name comparison there is the
# feature, not the defect. Anything else that wants to branch on a name should
# instead ask one of these two for a driver, then ask that driver what it can do.
#
#   maestro/backends/registry.py - the single place a configured or operator-supplied
#       backend name is resolved to a driver class and a binary. Its own docstring
#       states it is "the one module in maestro/ allowed to know a backend by name".
#   maestro/roles.py - role -> (backend, model) resolution and the fallback chain. It
#       reads names out of configuration and hands them to the registry; comparing and
#       normalising those names is the resolution itself.
#
# Adding a third entry is a design change, not a test change.
ALLOWLIST = frozenset(
    {
        "maestro/backends/registry.py",
        "maestro/roles.py",
    }
)

#: Test modules whose *subject* is naming, so a name in a skip condition is legitimate.
SKIP_ALLOWLIST = frozenset(
    {
        "tests/backends/test_registry.py",  # exercises name -> driver resolution
        "tests/test_roles.py",  # exercises role -> backend-name resolution
        "tests/test_no_name_branching.py",  # this gate; it must name backends to check them
    }
)

SKIP_DIRS = frozenset({"__pycache__", ".pytest_cache", ".git", ".venv", "node_modules"})

_SKIP_FUNCS = ("pytest.skip", "pytest.fail_and_skip")
_SKIP_MARKS = ("mark.skipif", "mark.skip", "mark.xfail")

_NAME_TOKEN_RE = re.compile(
    r"\b(" + "|".join(sorted(re.escape(n) for n in BACKEND_NAMES)) + r")\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# helpers


def _py_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        if SKIP_DIRS.intersection(path.parts):
            continue
        yield path


def _rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def _backend_name_constant(node: ast.AST) -> str | None:
    """The backend name this node *is*, or None. Exact match on the whole string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        if node.value.strip().lower() in BACKEND_NAMES:
            return node.value
    return None


def _literal_container_name(node: ast.AST) -> str | None:
    """The backend name inside a literal tuple/list/set/dict, or None."""
    elements: list[ast.AST] = []
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        elements = list(node.elts)
    elif isinstance(node, ast.Dict):
        elements = [k for k in node.keys if k is not None]
    for element in elements:
        hit = _backend_name_constant(element)
        if hit is not None:
            return hit
    return None


def _driver_class_ref(node: ast.AST) -> str | None:
    """``ClaudeBackend`` / ``claude.ClaudeBackend`` -> the class name, else None."""
    if isinstance(node, ast.Name) and node.id in DRIVER_CLASSES:
        return node.id
    if isinstance(node, ast.Attribute) and node.attr in DRIVER_CLASSES:
        return node.attr
    return None


def _any_driver_class_ref(node: ast.AST) -> str | None:
    for child in ast.walk(node):
        hit = _driver_class_ref(child)
        if hit is not None:
            return hit
    return None


class _NameBranchVisitor(ast.NodeVisitor):
    """Collects ``(lineno, why)`` for every name-dispatch construct it finds."""

    def __init__(self) -> None:
        self.hits: list[tuple[int, str]] = []

    def _record(self, node: ast.AST, why: str) -> None:
        self.hits.append((getattr(node, "lineno", 0), why))

    def visit_Compare(self, node: ast.Compare) -> None:
        operands = [node.left, *node.comparators]
        for index, op in enumerate(node.ops):
            left, right = operands[index], operands[index + 1]

            if isinstance(op, (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)):
                hit = _backend_name_constant(left) or _backend_name_constant(right)
                if hit is not None:
                    self._record(
                        node,
                        f"compares a value against the backend name {hit!r}",
                    )
                cls = _driver_class_ref(left) or _driver_class_ref(right)
                if cls is not None:
                    self._record(
                        node,
                        f"compares a type against the driver class {cls!r} "
                        "(type dispatch is name dispatch)",
                    )

            elif isinstance(op, (ast.In, ast.NotIn)):
                hit = _literal_container_name(right)
                if hit is not None:
                    self._record(
                        node,
                        "tests membership in a literal set of backend names "
                        f"(contains {hit!r})",
                    )
                elif _backend_name_constant(left) is not None:
                    self._record(
                        node,
                        "uses the backend name "
                        f"{_backend_name_constant(left)!r} as a membership probe",
                    )

        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        func_name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else ""
        )
        if func_name in ("isinstance", "issubclass") and len(node.args) >= 2:
            cls = _any_driver_class_ref(node.args[1])
            if cls is not None:
                self._record(
                    node,
                    f"{func_name}() type-dispatches on the driver class {cls!r}",
                )
        self.generic_visit(node)

    def visit_Match(self, node: ast.Match) -> None:
        for case in node.cases:
            for child in ast.walk(case.pattern):
                if isinstance(child, ast.MatchValue):
                    hit = _backend_name_constant(child.value)
                    if hit is not None:
                        self._record(
                            child,
                            f"match/case dispatches on the backend name {hit!r}",
                        )
        self.generic_visit(node)


def _scan_source(source: str, filename: str = "<snippet>") -> list[tuple[int, str]]:
    visitor = _NameBranchVisitor()
    visitor.visit(ast.parse(source, filename=filename))
    return visitor.hits


def _skip_construct_source(node: ast.Call) -> str | None:
    """The unparsed call if it is a skip/xfail construct, else None."""
    func_src = ast.unparse(node.func)
    if func_src in _SKIP_FUNCS or any(func_src.endswith(m) for m in _SKIP_MARKS):
        return ast.unparse(node)
    return None


def _scan_skips(source: str, filename: str = "<snippet>") -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not isinstance(node, ast.Call):
            continue
        rendered = _skip_construct_source(node)
        if rendered is None:
            continue
        found = _NAME_TOKEN_RE.search(rendered)
        if found is not None:
            hits.append(
                (
                    node.lineno,
                    "skip condition names the backend "
                    f"{found.group(0)!r}; skips gate on a capability or an "
                    "environment probe, never on which driver is under test",
                )
            )
    return hits


def _format(rel: str, lineno: int, why: str, lines: list[str]) -> str:
    src = lines[lineno - 1].strip() if 0 < lineno <= len(lines) else "<source unavailable>"
    return f"  {rel}:{lineno}: {why}\n      {src}"


# ---------------------------------------------------------------------------
# the gates


def test_no_backend_name_branching_in_maestro() -> None:
    """No module under maestro/ may branch on a backend name or driver class."""
    failures: list[str] = []
    for path in _py_files(PACKAGE_DIR):
        rel = _rel(path)
        if rel in ALLOWLIST:
            continue
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        for lineno, why in _scan_source(text, filename=rel):
            failures.append(_format(rel, lineno, why, lines))

    assert not failures, (
        "backend-name branching found in maestro/ ("
        + str(len(failures))
        + " hit(s)).\n"
        + RULE
        + ".\nAsk registry.get_backend(name) once, then branch on "
        "driver.capabilities(); naming a backend in a string, a dict key, a default "
        "or a journal detail is fine, comparing against it is not.\n"
        + "\n".join(failures)
    )


def test_no_test_skips_on_a_backend_name() -> None:
    """No test may be skipped because of *which* backend it runs against."""
    failures: list[str] = []
    for path in _py_files(TESTS_DIR):
        rel = _rel(path)
        if rel in SKIP_ALLOWLIST:
            continue
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        for lineno, why in _scan_skips(text, filename=rel):
            failures.append(_format(rel, lineno, why, lines))

    assert not failures, (
        "name-conditioned skip(s) found in tests/ ("
        + str(len(failures))
        + " hit(s)).\n"
        + RULE
        + ".\nGate the optional case on driver.capabilities().<flag> instead, so a new "
        "driver that has the capability runs the test without anyone editing it.\n"
        + "\n".join(failures)
    )


def test_allowlist_entries_exist_and_stay_short() -> None:
    """The allowlist is a design statement; a stale or growing one is a smell."""
    for rel in ALLOWLIST:
        assert (REPO / rel).is_file(), f"allowlisted module {rel} no longer exists"
    assert ALLOWLIST == frozenset(
        {"maestro/backends/registry.py", "maestro/roles.py"}
    ), "the name-lookup allowlist changed; that is a design decision, not a test fix"
    for rel in SKIP_ALLOWLIST:
        assert (REPO / rel).is_file(), f"allowlisted test module {rel} no longer exists"


# ---------------------------------------------------------------------------
# self-tests: prove the gate can actually see a violation, and that it does not
# fire on the legitimate uses the rule explicitly permits.

_OFFENDING = [
    'if backend.name == "codex":\n    pass\n',
    "if backend.name != 'claude':\n    pass\n",
    'x = "codex" == spec.backend\n',
    'if name in ("claude", "codex"):\n    pass\n',
    'if name not in ["claude"]:\n    pass\n',
    "if isinstance(driver, ClaudeBackend):\n    pass\n",
    "if isinstance(driver, (CodexBackend, object)):\n    pass\n",
    "if issubclass(cls, codex.CodexBackend):\n    pass\n",
    "if type(driver) is ClaudeBackend:\n    pass\n",
    'match backend.name:\n    case "codex":\n        pass\n',
    'flag = "claude" in enabled_backends\n',
]

_LEGITIMATE = [
    'BINARY = "claude"\n',
    'name = "codex"\n',
    'BACKENDS = {"claude": 1, "codex": 2}\n',
    'driver = BACKENDS["codex"]\n',
    'argv = ["claude", "-p", "--model", model]\n',
    'append_journal("backend_switch", f"{task} from=claude to=codex")\n',
    'def launch(backend: str = "claude"):\n    """Defaults to claude."""\n    return backend\n',
    '# a comment naming claude and codex\nif caps.native_resume:\n    pass\n',
    'if model == "claude-sonnet-4-6":\n    pass\n',
    'if spec.backend == other.backend:\n    pass\n',
    'if driver.capabilities().native_resume:\n    pass\n',
    'log = "codex exec resume"\n',
]


@pytest.mark.parametrize("snippet", _OFFENDING)
def test_gate_detects_offending_snippet(snippet: str) -> None:
    assert _scan_source(snippet), f"gate missed a violation:\n{snippet}"


@pytest.mark.parametrize("snippet", _LEGITIMATE)
def test_gate_ignores_legitimate_snippet(snippet: str) -> None:
    assert _scan_source(snippet) == [], f"gate fired on a legitimate use:\n{snippet}"


_OFFENDING_SKIPS = [
    'import pytest\npytest.skip("no codex binary")\n',
    'import pytest\n@pytest.mark.skipif(True, reason="claude only")\ndef test_x():\n    pass\n',
    'import pytest\n@pytest.mark.skipif(driver.name == "codex", reason="n/a")\ndef test_x():\n    pass\n',
    'import pytest\n@pytest.mark.xfail(reason="claude does not resume")\ndef test_x():\n    pass\n',
]

_LEGITIMATE_SKIPS = [
    'import pytest\npytest.skip("reference repo not found")\n',
    'import pytest\n@pytest.mark.skipif(not caps.native_resume, reason="no native resume")\ndef test_x():\n    pass\n',
    'import pytest\ndef test_x():\n    assert "codex" in known_backends()\n',
]


@pytest.mark.parametrize("snippet", _OFFENDING_SKIPS)
def test_skip_gate_detects_offending_snippet(snippet: str) -> None:
    assert _scan_skips(snippet), f"skip gate missed a violation:\n{snippet}"


@pytest.mark.parametrize("snippet", _LEGITIMATE_SKIPS)
def test_skip_gate_ignores_legitimate_snippet(snippet: str) -> None:
    assert _scan_skips(snippet) == [], f"skip gate fired on a legitimate skip:\n{snippet}"
