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

Docstring exemption — scoped to where the real provenance lives, not blanket.
A string that is a module/class/function **docstring** never counts *when the file
is under* ``maestro/``, because a docstring there cannot reach a model or an
operator — only prose that explains history to a human reading the source. This is
what lets ``confinement.py``, ``merge.py`` and ``selfheal/selffix.py``'s own
provenance paragraphs (module and function docstrings quoting the old, removed
hardcoded values) stay legitimate without a single hardcoded line-number exception:
the exclusion is structural (first statement of a module/class/function body), so it
survives a reflow or a line renumbering that would break a hardcoded list. All five
real provenance sites this gate must tolerate are in ``maestro/`` — none are in
``tests/`` — so the exemption is **not** applied when scanning ``tests/``: a new
test file's own docstring is exactly where B1 and B2 actually reintroduced the
reference project's name (per this item's history), and a docstring is not a
special case there, it is the live text of the file.

Comment scanning — the discriminator is *does this term have a legitimate
provenance use anywhere in this tree*, not which group a term happens to be in.
``EVERYWHERE_PATTERNS`` (``RAG archive``, ``Arabic/Hebrew``) has **no** legitimate
provenance use anywhere in ``maestro/`` or ``tests/`` — nothing in this codebase's
history ever needs to say either phrase, even to explain that it used to be
hardcoded — so those two terms are also scanned inside ``#`` comments (via
``tokenize``, so a ``#`` inside a string literal is never mistaken for one).
``MAESTRO_ONLY_PATTERNS`` and ``MAESTRO_ONLY_GUARDED_PATTERNS`` are **not**
comment-scanned, because two of them do have a legitimate provenance comment in this
tree today — ``maestro/merge.py:65`` ("used to also exempt `restart_bot.sh` by
name") and ``maestro/selfheal/selffix.py:69`` ("(`main_bot.py`, ... `scripts/
restart_bot.sh`, ...)") are both bare ``#`` comments, not docstrings, and both would
trip a raw comment scan for those terms — forcing exactly the hardcoded path/line
tolerance list this gate has so far avoided. So the residual risk is stated plainly
rather than fixed: a ``#`` comment in ``maestro/`` that reintroduces ``main_bot``,
``restart_bot``, ``gdrive:`` or ``Colab`` as a live default — not as provenance
prose — would slip through this gate undetected. That is a deliberate trade, not an
oversight: the alternative (comment-scanning those four) is unsound against the
tree as it exists today.
"""
from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

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


def _comment_texts(source: str) -> list[tuple[int, str]]:
    """[(lineno, comment_text)] for every ``#`` comment in `source`, found via
    `tokenize` rather than a naive text scan so a ``#`` inside a string literal is
    never mistaken for one. Only ``EVERYWHERE_PATTERNS`` terms are ever checked
    against this — see the module docstring's "Comment scanning" section for why."""
    comments: list[tuple[int, str]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                comments.append((tok.start[0], tok.string))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        # `tokenize.TokenError` — NOT `TokenizeError`, which does not exist. Python only
        # evaluates an `except` tuple when an exception actually reaches it, so the wrong
        # spelling sat here inert: the first unparseable comment would have raised
        # `AttributeError` out of the handler meant to swallow it. Reached by
        # `test_comment_scanning_degrades_on_source_it_cannot_tokenize`.
        pass
    return comments


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
    MAESTRO_ONLY_GUARDED_PATTERNS; EVERYWHERE_PATTERNS are scanned regardless, in both
    string literals and `#` comments (`maestro_only=False` also means the docstring
    exemption does not apply — see the module docstring's "Docstring exemption" and
    "Comment scanning" sections for why each is scoped this way).
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    # The docstring exemption protects real provenance prose, and every real site of
    # that is in maestro/ — so it applies only there, never when scanning tests/.
    doc_ids = _docstring_ids(tree) if maestro_only else set()
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

    # EVERYWHERE_PATTERNS terms have no legitimate provenance use in this tree at
    # all (see module docstring), so they are also scanned inside comments — in
    # BOTH trees, docstring-exemption question notwithstanding (comments are never
    # docstrings). MAESTRO_ONLY_* terms are deliberately excluded: two of them have
    # a real, legitimate `#` comment in maestro/ today.
    for lineno, comment in _comment_texts(source):
        for name, pattern in EVERYWHERE_PATTERNS.items():
            if pattern.search(comment):
                snippet = comment.strip()[:70]
                violations.append((lineno, f"{name} (comment)", snippet))

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
        "a reference-project-specific string was found live: outside a legitimate "
        "docstring (maestro/ only — see this file's module docstring's 'Docstring "
        "exemption' section), or, for a '(comment)'-tagged hit, inside a `#` "
        "comment for a term that has no legitimate provenance use anywhere in this "
        "tree (see 'Comment scanning'). If this is genuine historical provenance "
        "explaining what used to be hardcoded, and the term is main_bot/restart_bot/"
        "gdrive:/Colab, a module/class/function docstring OR a `#` comment in "
        "maestro/ is fine, the way confinement.py, merge.py and "
        "selfheal/selffix.py already do — but RAG archive/Arabic-Hebrew have no such "
        "exemption anywhere. If it is a new occurrence, remove it.\nFound:\n  "
        + "\n  ".join(problems)
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
    # maestro_only=True: the docstring exemption applies (this file stands in for
    # maestro/), so only the two live constants are flagged.
    maestro_problems = _scan_file(synthetic, maestro_only=True)
    maestro_names = {name for _, name, _ in maestro_problems}
    assert maestro_names == {"RAG archive", "Arabic/Hebrew"}
    assert len(maestro_problems) == 2  # docstring occurrence not also flagged here

    # maestro_only=False: the docstring exemption does NOT apply (fix round 1,
    # Important 2), so the docstring's own "RAG archive" is flagged too.
    tests_problems = _scan_file(synthetic, maestro_only=False)
    tests_names = {name for _, name, _ in tests_problems}
    assert tests_names == {"RAG archive", "Arabic/Hebrew"}
    assert len(tests_problems) == 3  # the two live constants plus the docstring hit


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
    by planting the same shape fresh rather than reading real line numbers. Scoped to
    maestro_only=True only (fix round 1, Important 2): all five real sites are in
    maestro/, and the same docstring scanned as if it were under tests/ must NOT be
    tolerated for the EVERYWHERE_PATTERNS terms it contains — that half is covered by
    `test_docstring_exemption_does_not_apply_when_scanning_tests` below."""
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


def test_the_gate_actually_bites_everywhere_terms_in_comments(tmp_path):
    """Fix round 1, Important 1: `ast.Constant` scanning alone is comment-blind, and
    B1's/B2's actual historical trips were a `#` comment naming the reference
    project — this is the shape that gap left open for `EVERYWHERE_PATTERNS`. Proves
    the fix in both directions: a `#` comment naming a term with no legitimate
    provenance use is caught, in both trees; a `#` comment inside a string literal
    (not a real comment) is not mistaken for one."""
    synthetic = tmp_path / "synthetic_module.py"
    synthetic.write_text(
        'CODE = "fine"\n'
        '# used to assume a RAG archive backend; no longer does\n'
        'NOT_A_COMMENT = "a literal string containing a # RAG archive character"\n',
        encoding="utf-8",
    )
    for maestro_only in (True, False):
        problems = _scan_file(synthetic, maestro_only=maestro_only)
        names = {name for _, name, _ in problems}
        assert "RAG archive (comment)" in names, (maestro_only, problems)
        # the literal string's own hit is separate (not "(comment)") and expected —
        # only assert the comment-specific finding exists, and exactly once each.
        comment_hits = [p for p in problems if p[1] == "RAG archive (comment)"]
        assert len(comment_hits) == 1


def test_maestro_only_terms_stay_comment_blind_by_design(tmp_path):
    """The other half of the same fix: `main_bot`/`restart_bot`/`gdrive:`/`Colab` are
    NOT comment-scanned, because two of them have a real, legitimate `#` comment in
    maestro/ today (`merge.py:65`, `selfheal/selffix.py:69`). This test proves the
    boundary is where the module docstring says it is, not accidentally wider."""
    synthetic = tmp_path / "synthetic_module.py"
    synthetic.write_text(
        'CODE = "fine"\n'
        '# used to also exempt restart_bot.sh and main_bot.py by name\n',
        encoding="utf-8",
    )
    assert _scan_file(synthetic, maestro_only=True) == []


def test_docstring_exemption_does_not_apply_when_scanning_tests(tmp_path):
    """Fix round 1, Important 2: the docstring exemption exists to protect the five
    real provenance sites, all of which are in maestro/. A new test file's own
    docstring is exactly where B1/B2 actually reintroduced the reference project's
    name, so the exemption must not cover tests/-shaped scans."""
    synthetic = tmp_path / "synthetic_module.py"
    synthetic.write_text(
        '"""This test module proves the RAG archive assumption is gone."""\n'
        'def test_something():\n'
        '    assert True\n',
        encoding="utf-8",
    )
    tests_hits = _scan_file(synthetic, maestro_only=False)
    names = {name for _, name, _ in tests_hits}
    assert "RAG archive" in names

    # The same docstring, scanned as if it were under maestro/, stays exempt — this
    # is what keeps the five real provenance sites (all in maestro/) tolerated.
    assert _scan_file(synthetic, maestro_only=True) == []


def test_real_provenance_comment_shape_survives_both_fixes(tmp_path):
    """The precise shape of `maestro/merge.py:65` and
    `maestro/selfheal/selffix.py:69`: a bare `#` comment naming `main_bot`/
    `restart_bot` as history. Neither fix in this round should touch it — proven
    fresh, not by reading real line numbers, so a reformat can't silently break the
    tolerance."""
    synthetic = tmp_path / "synthetic_module.py"
    synthetic.write_text(
        '# The `sudo` pattern below used to also exempt `restart_bot.sh` by name\n'
        '# (`main_bot.py`, `data/`, `models/`, `scripts/restart_bot.sh`, ...).\n'
        'CODE = "fine"\n',
        encoding="utf-8",
    )
    assert _scan_file(synthetic, maestro_only=True) == []


def test_orchestrator_proof_of_absence_still_exempt_after_both_fixes():
    """Regression guard named in the fix-round ruling: B2's characterization test at
    tests/characterization/test_orchestrator.py must still pass this gate untouched
    after both fixes — neither the comment scan nor the narrowed docstring exemption
    should affect it, since its banned-term tuple sits in ordinary code, not a
    docstring or a comment."""
    path = TESTS_DIR / "characterization" / "test_orchestrator.py"
    assert _scan_file(path, maestro_only=False) == []


# ---------------------------------------------------------------------------
# The comment scanner's own failure mode


def test_tokenize_has_no_TokenizeError_which_is_why_the_handler_was_inert():
    """The finding, stated as an assertion. `tokenize.TokenizeError` has never existed;
    the real name is `TokenError`. Naming the wrong one in an `except` tuple is silent
    until something is actually raised at it, at which point the handler that exists to
    degrade gracefully raises `AttributeError` instead."""
    assert not hasattr(tokenize, "TokenizeError")
    assert issubclass(tokenize.TokenError, Exception)


@pytest.mark.parametrize(
    "label,source",
    [
        # `TokenError`: the tokenizer hits EOF inside an unclosed bracket.
        ("unclosed bracket", "# a RAG archive comment\nx = (1,\n"),
        # `IndentationError`: a dedent that matches no enclosing level.
        ("dedent mismatch",
         "# a RAG archive comment\nif x:\n    a = 1\n  b = 2\n"),
    ],
)
def test_comment_scanning_degrades_on_source_it_cannot_tokenize(label, source):
    """The handler, reached. Unparseable source must come back as a list, not as an
    exception thrown out of a gate — and `tokenize.generate_tokens` is a generator, so
    the comments it yielded *before* it gave up are kept rather than discarded. Both
    halves are asserted: no raise, and the partial result is the useful one.

    `_scan_file` runs `ast.parse` before it gets here and would reject both of these
    sources earlier, so this calls `_comment_texts` directly — the function that owns the
    handler. That is the point: a helper's failure path has to be exercised where it
    lives, or it stays "unreachable" until the day something else calls it, and then it
    raises `AttributeError` from inside the `except` meant to swallow it.
    """
    found = _comment_texts(source)                      # must not raise
    assert [text for _, text in found] == ["# a RAG archive comment"]


def test_comment_scanning_still_returns_comments_it_can_tokenize():
    """The other direction, so the test above cannot be satisfied by a scanner that
    returns `[]` for everything."""
    found = _comment_texts("x = 1  # a RAG archive comment\n")
    assert [text for _, text in found] == ["# a RAG archive comment"]
