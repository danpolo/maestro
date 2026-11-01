"""P12 pilot finding D20: the deterministic three-way line merge (`maestro.mergeresolve`).

DuetFlow 04 was approved and then refused at merge: git reported three conflict hunks in
one file, two of which touch disjoint base lines. The resolver takes exactly the hunks
that cannot interfere and refuses everything else, with no model anywhere (§3.1).
"""
from __future__ import annotations

from maestro import mergeresolve as mr


def merge(base: str, ours: str, theirs: str, path: str = "f.py"):
    return mr.merge_text(base, ours, theirs, path=path)


BASE = "import a\n\ndef f():\n    '''Say hi.'''\n    return 1\n\ndef g():\n    return 2\n"


def test_disjoint_edits_on_both_sides_are_both_applied():
    ours = BASE.replace("return 1", "return 10")
    theirs = BASE.replace("return 2", "return 20")
    result = merge(BASE, ours, theirs)
    assert result.ok
    assert result.text == BASE.replace("return 1", "return 10").replace("return 2", "return 20")


def test_a_modified_line_and_an_insertion_right_after_it_do_not_overlap():
    """Hunk shape 2: one side edits the import, the other inserts a line after it."""
    ours = BASE.replace("import a\n", "import a, b\n")
    theirs = BASE.replace("import a\n", "import a\nimport c\n")
    result = merge(BASE, ours, theirs)
    assert result.ok
    assert result.text.startswith("import a, b\nimport c\n\ndef f():")


def test_an_insertion_right_before_a_modified_line_goes_before_it():
    ours = BASE.replace("def g():\n", "def g():  # changed\n")
    theirs = BASE.replace("\ndef g():\n", "\n# comment\ndef g():\n")
    result = merge(BASE, ours, theirs)
    assert result.ok
    assert "# comment\ndef g():  # changed\n" in result.text


def test_two_insertions_at_the_same_point_are_unioned_target_first():
    """Hunk shape 3: both sides add a new block at the same place."""
    ours = BASE + "\ndef ours():\n    pass\n"
    theirs = BASE + "\ndef theirs():\n    pass\n"
    result = merge(BASE, ours, theirs)
    assert result.ok
    assert result.text == BASE + "\ndef ours():\n    pass\n" + "\ndef theirs():\n    pass\n"


def test_identical_changes_on_both_sides_apply_once():
    ours = theirs = BASE.replace("return 1", "return 11")
    result = merge(BASE, ours, theirs)
    assert result.ok and result.text == ours


def test_both_sides_rewriting_the_same_line_is_a_conflict():
    """Hunk shape 1: the same docstring sentence, rewritten twice."""
    ours = BASE.replace("Say hi.", "Say hello.")
    theirs = BASE.replace("Say hi.", "Greet the caller.")
    result = merge(BASE, ours, theirs)
    assert not result.ok and result.text is None
    [conflict] = result.conflicts
    assert conflict.path == "f.py"
    assert (conflict.start, conflict.end) == (4, 4)
    assert "Say hello." in conflict.ours and "Greet the caller." in conflict.theirs
    assert conflict.to_dict()["lines"] == "4-4"


def test_an_insertion_strictly_inside_a_modified_range_is_a_conflict():
    base = "a\nb\nc\nd\n"
    ours = "a\nB\nC\nd\n"            # replaces lines 2-3
    theirs = "a\nb\nx\nc\nd\n"       # inserts between 2 and 3
    result = merge(base, ours, theirs)
    assert not result.ok
    assert (result.conflicts[0].start, result.conflicts[0].end) == (2, 3)


def test_crlf_line_endings_are_preserved():
    base = BASE.replace("\n", "\r\n")
    ours = base.replace("return 1", "return 10")
    theirs = base.replace("return 2", "return 20")
    result = merge(base, ours, theirs)
    assert result.ok
    assert result.text == base.replace("return 1", "return 10").replace("return 2", "return 20")
    assert "\n" not in result.text.replace("\r\n", "")


def test_a_missing_final_newline_is_kept():
    base = "a\nb\nc"
    ours = "A\nb\nc"
    theirs = "a\nb\nC"
    result = merge(base, ours, theirs)
    assert result.ok and result.text == "A\nb\nC"


def test_one_side_unchanged_takes_the_other():
    theirs = BASE.replace("return 2", "return 3")
    assert merge(BASE, BASE, theirs).text == theirs
    assert merge(BASE, theirs, BASE).text == theirs
