"""D1: `maestro/` and `tests/` must carry no live string that couples the code to the
reference project's own domain, bot runtime, or ML-workflow specifics — not just its
name.

`tests/test_purity.py` already gates the reference project's literal name and its
Telegram bot handle (its own ``PROJECT_PATTERNS``) everywhere except its documented
build-scaffold exemptions. That gate is not extended here — it already owns those
terms and already scans ``tests/``.
This module gates a *different* class of leak the same audit surfaced: strings that
reveal the reference project's specific bot runtime or ML deliverable shape, which
crept back in at least twice (B1, B2) as a *test* about removing them, not as
production code. Candidate terms are drawn verbatim from item D1's task brief.

Anti-vacuity, following ``tests/test_no_reference_sidecars.py``'s
``test_the_gate_actually_bites`` and ``tests/test_no_unresolved_pending.py``: every
category below carries its own synthetic-module test proving the checker fires when it
should, and stays silent when it shouldn't, so a silently-neutered walk cannot pass by
finding nothing real to complain about.

Scope decisions (the three term groups below, and why they are not all treated alike):

* ``EVERYWHERE_PATTERNS`` — phrases that describe the reference project's actual ML
  product (a bilingual retrieval archive) and could not plausibly describe anything
  maestro itself does. Zero legitimate reason to appear as a live string in ``maestro/``
  *or* ``tests/``, so both trees are scanned, matching the corollary in this item's
  brief: a well-meaning author writing a test *about* removing one of these reaches for
  the term inside an assertion or a fixture just as easily as production code once did.
  Scanning ``tests/`` for real surfaced exactly one existing hit —
  ``tests/characterization/test_orchestrator.py``'s
  ``test_generate_proposals_system_prompt_does_not_name_a_reference_project``, which
  loops ``for banned in ("arabic", "hebrew", "rag archive", ...): assert banned not in
  lowered`` — a regression test *proving* the removal, not a re-leak. Banning the
  phrase outright would have failed the very test that pins its absence, so
  ``_is_proof_of_absence`` recognizes that shape (a literal used only as the operand,
  directly or via a ``for``, of a ``not in``/``!=``/``is not`` comparison) and exempts
  it — while still catching the opposite, `in`/`==` shape, which really would be a
  reintroduction. See ``test_proof_of_absence_shape_is_exempt_in_tests`` and
  ``test_positive_presence_of_a_banned_term_still_bites`` below for both directions
  proven on synthetic input.

* ``MAESTRO_ONLY_PATTERNS`` (``main_bot``, ``restart_bot``) — former hardcoded-default
  filenames from the reference project's confinement/deny-list logic (see
  ``confinement.py``'s, ``merge.py``'s and ``selfheal/selffix.py``'s own provenance
  docstrings/comments for the history of removing them as *defaults*). The regression
  this guards against is one of those filenames reappearing as a baked-in default or
  deny-list entry in ``maestro/`` itself. ``tests/`` is deliberately **not** scanned for
  these two: `git grep` shows over a dozen *legitimate* existing uses of
  ``main_bot.py`` across ``tests/characterization/test_merge.py``,
  ``test_selfheal.py``, ``test_confinement.py``, ``test_skills.py`` and
  ``test_parking.py`` — all of them plain example filenames chosen to *prove* the
  code now reacts to whatever ``project.yaml`` declares (e.g.
  ``_branch(repo, "b", {"main_bot.py": "x\n"})``), not a hardcoded name. Scanning
  ``tests/`` for these substrings would fail exactly the tests that pin the fix — a gate
  that fires on correct code, which the controller's brief for this item says is worse
  than a narrower one. So the scope is narrowed here, deliberately and not silently.

* ``MAESTRO_ONLY_GUARDED_PATTERNS`` (``Colab``, ``gdrive:``) — the optional
  notebook/Drive workflow, which is legitimate *and currently shipping* inside
  ``if confinement.available(CHECK_NB):`` branches in ``maestro/selfheal/redo.py`` and
  ``maestro/implementer.py`` (that conditional is itself the fix for the bug these terms
  used to signal: an unconditional Colab/Drive assumption baked into every project's
  prompts). So the live check is not "never say Colab" — it is "never say Colab, or
  gdrive:, outside the branch that proves a project actually has a notebook
  deliverable", walked via a small AST guard tracker (`_GuardTracker` below). Scoped to
  ``maestro/`` only: ``tests/`` exercises both the CHECK_NB-present and CHECK_NB-absent
  paths through fixture setup (writing/removing ``subject.CHECK_NB`` on disk) rather
  than through an inline ``if``, so the same branch-guard walk cannot be applied there
  without false-positiving on the characterization tests that pin the *presence* of the
  feature (``tests/characterization/test_implementer.py``'s own
  ``assert "gdrive" in brief.lower()`` when CHECK_NB exists, for one). This is the same
  judgement call the task brief calls out explicitly for ``Colab`` — extended here to
  ``gdrive:`` because empirically it lives in exactly the same guarded blocks, for the
  same feature, for the same reason.

All three groups share one exclusion: a string that is a module/class/function
**docstring** never counts, because a docstring cannot reach a model or an operator —
only prose that explains history to a human reading the source. This is what lets
``confinement.py``, ``merge.py`` and ``selfheal/selffix.py``'s own provenance
paragraphs (module and function docstrings quoting the old, removed hardcoded values)
stay legitimate without a single hardcoded line-number exception: the exclusion is
structural (first statement of a module/class/function body), so it survives a reflow
or a line renumbering that would break a hardcoded list.  A plain ``#`` comment needs no
special-casing at all — Python's ``ast`` module never sees comments in the first place.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO / "maestro"
TESTS_DIR = REPO / "tests"

SKIP_DIRS = frozenset({"__pycache__", ".pytest_cache", ".git", ".venv", "node_modules"})

# This gate's own file: it must name every candidate term below, in plain sight, to
# define and self-test the patterns. Same self-exemption `test_no_name_branching.py`
# grants itself for the same reason.
ALLOWLIST = frozenset({"tests/test_no_reference_project_strings.py"})

# ---------------------------------------------------------------------------
# Term categories. See the module docstring above for why each is scoped as it is.

EVERYWHERE_PATTERNS = {
    "RAG archive": re.compile(r"RAG\s+archive", re.IGNORECASE),
    "Arabic/Hebrew": re.compile(r"Arabic\s*/\s*Hebrew", re.IGNORECASE),
}

MAESTRO_ONLY_PATTERNS = {
    "main_bot": re.compile(r"main_bot", re.IGNORECASE),
    "restart_bot": re.compile(r"restart_bot", re.IGNORECASE),
}

MAESTRO_ONLY_GUARDED_PATTERNS = {
    "Colab": re.compile(r"\bColab\b", re.IGNORECASE),
    "gdrive:": re.compile(r"\bgdrive\s*:", re.IGNORECASE),
}

DOCSTRING_NODE_TYPES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


# ---------------------------------------------------------------------------
# helpers


def _py_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if not SKIP_DIRS.intersection(p.parts))


def _rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def _docstring_ids(tree: ast.AST) -> set[int]:
    """id() of every string Constant node that is a module/class/function docstring —
    the first statement of that node's body, if it is a bare string expression."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, DOCSTRING_NODE_TYPES):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr):
                value = body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    ids.add(id(value))
    return ids


def _mentions_check_nb(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Name) and n.id == "CHECK_NB" for n in ast.walk(node))


def _parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    """id(child) -> parent, for every node in `tree`. Used only by
    `_is_proof_of_absence`, which needs to see one level of surrounding structure."""
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


_NEGATIVE_OPS = (ast.NotIn, ast.NotEq, ast.IsNot)


def _compares_negatively(compare: ast.Compare) -> bool:
    return any(isinstance(op, _NEGATIVE_OPS) for op in compare.ops)


def _is_proof_of_absence(node: ast.Constant, parents: dict[int, ast.AST]) -> bool:
    """True when `node` is a banned-term literal sitting in a shape that PROVES the
    term is absent from some computed value, rather than one that could emit it — the
    ``for banned in (...): assert banned not in lowered`` and
    ``assert "term" not in captured`` shapes a characterization test legitimately uses
    to pin exactly the removal this gate exists to protect. A term used the other way
    (``in``/``==``, i.e. asserting or constructing *presence*) is never exempt."""
    parent = parents.get(id(node))

    if isinstance(parent, ast.Compare):
        return _compares_negatively(parent)

    if isinstance(parent, (ast.Tuple, ast.List, ast.Set)):
        container = parent
        grandparent = parents.get(id(container))

        if isinstance(grandparent, ast.Compare):
            return _compares_negatively(grandparent)

        if isinstance(grandparent, ast.For) and grandparent.iter is container:
            target_names = {
                n.id for n in ast.walk(grandparent.target) if isinstance(n, ast.Name)
            }
            for stmt in ast.walk(grandparent):
                if not isinstance(stmt, ast.Compare):
                    continue
                operands = [stmt.left, *stmt.comparators]
                involves_target = any(
                    isinstance(operand, ast.Name) and operand.id in target_names
                    for operand in operands
                )
                if involves_target and _compares_negatively(stmt):
                    return True

    return False


class _GuardTracker(ast.NodeVisitor):
    """Walks a module collecting every string Constant, tagged with whether it sits
    inside the True-branch of an ``if`` whose test mentions ``CHECK_NB`` (nesting- and
    ``elif``/``else``-aware: an ``else`` branch of a CHECK_NB-guarded ``if`` is NOT
    guarded by that ``if`` — it is the unconditional fallback path)."""

    def __init__(self) -> None:
        self.guard_depth = 0
        self.strings: list[tuple[ast.Constant, bool]] = []

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        guarded_here = _mentions_check_nb(node.test)
        if guarded_here:
            self.guard_depth += 1
        for stmt in node.body:
            self.visit(stmt)
        if guarded_here:
            self.guard_depth -= 1
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self.strings.append((node, self.guard_depth > 0))


def _scan_file(path: Path, *, maestro_only: bool) -> list[tuple[int, str, str]]:
    """(lineno, term_name, snippet) for every violation in `path`.

    `maestro_only=True` additionally scans MAESTRO_ONLY_PATTERNS and the guard-aware
    MAESTRO_ONLY_GUARDED_PATTERNS; EVERYWHERE_PATTERNS are scanned regardless.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    doc_ids = _docstring_ids(tree)
    parents = _parent_map(tree)

    tracker = _GuardTracker()
    tracker.visit(tree)

    violations: list[tuple[int, str, str]] = []
    for node, guarded in tracker.strings:
        if id(node) in doc_ids:
            continue
        text = node.value
        snippet = text.strip().replace("\n", " ")[:70]

        for name, pattern in EVERYWHERE_PATTERNS.items():
            if pattern.search(text) and not _is_proof_of_absence(node, parents):
                violations.append((node.lineno, name, snippet))

        if maestro_only:
            for name, pattern in MAESTRO_ONLY_PATTERNS.items():
                if pattern.search(text):
                    violations.append((node.lineno, name, snippet))
            for name, pattern in MAESTRO_ONLY_GUARDED_PATTERNS.items():
                if pattern.search(text) and not guarded:
                    violations.append((node.lineno, f"{name} (unguarded)", snippet))

    return violations


def _all_violations() -> list[str]:
    problems: list[str] = []
    for path in _py_files(PACKAGE_DIR):
        if _rel(path) in ALLOWLIST:
            continue
        for lineno, name, snippet in _scan_file(path, maestro_only=True):
            problems.append(f"{_rel(path)}:{lineno}: {name} — {snippet!r}")
    for path in _py_files(TESTS_DIR):
        if _rel(path) in ALLOWLIST:
            continue
        for lineno, name, snippet in _scan_file(path, maestro_only=False):
            problems.append(f"{_rel(path)}:{lineno}: {name} — {snippet!r}")
    return problems


# ---------------------------------------------------------------------------
# the gate


def test_no_reference_project_workflow_strings():
    problems = _all_violations()
    assert not problems, (
        "a reference-project-specific string was found outside a legitimate "
        "docstring. If this is genuine historical provenance explaining what used to "
        "be hardcoded, say so in a module/class/function docstring (not a live "
        "string or a bare `#` comment carrying the live value) the way "
        "confinement.py, merge.py and selfheal/selffix.py already do. If it is a new "
        "occurrence, remove it — see this file's module docstring for why each term "
        "is scoped the way it is.\nFound:\n  " + "\n  ".join(problems)
    )


# ---------------------------------------------------------------------------
# anti-vacuity: each category must be shown to fire, and to stay silent where it should


def test_the_gate_actually_bites_everywhere_terms(tmp_path):
    synthetic = tmp_path / "synthetic_module.py"
    synthetic.write_text(
        'LIVE = "backed by a RAG archive of prior sessions"\n'
        'ALSO_LIVE = "translates Arabic/Hebrew pairs on request"\n'
        'FINE = "backed by a retrieval-augmented pipeline"\n'
        '\n'
        'def documented():\n'
        '    """Historically hardcoded a RAG archive assumption; no longer does."""\n'
        '    return None\n',
        encoding="utf-8",
    )
    problems = _scan_file(synthetic, maestro_only=False)
    names = {name for _, name, _ in problems}
    assert names == {"RAG archive", "Arabic/Hebrew"}
    assert len(problems) == 2  # the docstring occurrence must not also be flagged


def test_the_gate_actually_bites_maestro_only_terms(tmp_path):
    synthetic = tmp_path / "synthetic_module.py"
    synthetic.write_text(
        'DENY = ["main_bot.py", "scripts/restart_bot.sh"]\n'
        '\n'
        'def documented():\n'
        '    """Used to deny main_bot.py and restart_bot.sh by name; reads '
        'project.yaml now."""\n'
        '    return None\n',
        encoding="utf-8",
    )
    maestro_hits = _scan_file(synthetic, maestro_only=True)
    names = {name for _, name, _ in maestro_hits}
    assert names == {"main_bot", "restart_bot"}
    assert len(maestro_hits) == 2  # docstring occurrence excluded

    # The same file, scanned as if it were under tests/, must NOT trip — this is the
    # scope decision documented in the module docstring, proven rather than asserted.
    tests_hits = _scan_file(synthetic, maestro_only=False)
    assert tests_hits == []


def test_the_gate_actually_bites_guarded_terms_when_unguarded(tmp_path):
    synthetic = tmp_path / "synthetic_module.py"
    synthetic.write_text(
        'CHECK_NB = None\n'
        '\n'
        'def unguarded_use():\n'
        '    return "upload the notebook via the Colab UI to gdrive: when done"\n'
        '\n'
        'def guarded_use():\n'
        '    if CHECK_NB:\n'
        '        return "re-upload to the same gdrive: path; the Colab link keeps working"\n'
        '    return "run this project'"'"'s own verification instead"\n'
        '\n'
        'def else_branch_is_not_guarded():\n'
        '    if CHECK_NB:\n'
        '        pass\n'
        '    else:\n'
        '        return "mentions Colab in the fallback branch"\n',
        encoding="utf-8",
    )
    problems = _scan_file(synthetic, maestro_only=True)
    flagged_lines = {lineno for lineno, _, _ in problems}
    names = {name for _, name, _ in problems}

    # unguarded_use's line and else_branch_is_not_guarded's line both fire; guarded_use
    # does not.
    assert any("Colab (unguarded)" == n for n in names)
    assert any("gdrive: (unguarded)" == n for n in names)
    source_lines = synthetic.read_text(encoding="utf-8").splitlines()
    guarded_lineno = next(
        i + 1 for i, line in enumerate(source_lines) if "re-upload to the same" in line
    )
    assert guarded_lineno not in flagged_lines


def test_gate_tolerates_the_documented_provenance_shape(tmp_path):
    """The precise shape of the five real provenance hits this item's controller
    verified: a term appearing only inside a module or function docstring, describing
    what used to be hardcoded. Proves the exclusion is structural, not a location list,
    by planting the same shape fresh rather than reading real line numbers."""
    synthetic = tmp_path / "synthetic_module.py"
    synthetic.write_text(
        '"""Denied main_bot.py, restart_bot.sh and warned about a RAG archive '
        'runtime — one project'"'"'s layout, hardcoded. Also generated a Colab '
        'notebook and re-uploaded to a gdrive: remote unconditionally, on every '
        'project, translating Arabic/Hebrew pairs. None of that is true any more.\n'
        '"""\n'
        'from __future__ import annotations\n'
        '\n'
        '\n'
        'def rules_prose():\n'
        '    """Used to hand-write a main_bot.py deny entry here; reads '
        'project.yaml now."""\n'
        '    return "confined to the paths project.yaml declares"\n',
        encoding="utf-8",
    )
    assert _scan_file(synthetic, maestro_only=True) == []
    assert _scan_file(synthetic, maestro_only=False) == []


def test_proof_of_absence_shape_is_exempt_in_tests(tmp_path):
    """The exact shape found live in tests/characterization/test_orchestrator.py: a
    characterization test proving a banned phrase is absent from a model-facing
    prompt, via `for banned in (...): assert banned not in lowered`. This is a
    regression test *for* the removal, not a re-leak, and must not trip the gate."""
    synthetic = tmp_path / "synthetic_module.py"
    synthetic.write_text(
        'def test_prompt_stays_generic(captured):\n'
        '    lowered = captured.system.lower()\n'
        '    for banned in ("arabic", "hebrew", "rag archive", "telegram rag"):\n'
        '        assert banned not in lowered\n'
        '\n'
        'def test_direct_form_also_exempt(prompt):\n'
        '    assert "rag archive" not in prompt\n'
        '    assert prompt != "rag archive"\n',
        encoding="utf-8",
    )
    assert _scan_file(synthetic, maestro_only=False) == []


def test_positive_presence_of_a_banned_term_still_bites(tmp_path):
    """The proof-of-absence exemption must not swallow the opposite shape: a test
    that asserts a banned term IS present (or constructs it directly) is exactly the
    reintroduction this gate exists to catch, loop-wrapped or not."""
    synthetic = tmp_path / "synthetic_module.py"
    synthetic.write_text(
        'def test_prompt_mentions_the_domain(captured):\n'
        '    assert "rag archive" in captured.system.lower()\n'
        '\n'
        'def test_loop_of_required_terms(prompt):\n'
        '    for required in ("rag archive",):\n'
        '        assert required in prompt\n',
        encoding="utf-8",
    )
    problems = _scan_file(synthetic, maestro_only=False)
    assert len(problems) == 2
    assert all(name == "RAG archive" for _, name, _ in problems)
