"""Pinned behaviour of the reference task-graduation tool (M4c batch 2, Wave A).

`scripts/mark_task_complete.py` is a **standalone script** in the reference, same shape
as `watchdog.py` (M4b), `orchestrator_status.py` (M4c batch 1, R13) and `gen_upcoming.py`
(M4c batch 2, Wave A) — not a function living inside the `orchestrator_run.py` monolith.
It is placed at `maestro/docs/complete.py` — alongside `docs.roadmap`, `docs.depmap` and
`docs.upcoming`, the same package the rest of the 3-doc engine lives in — so the marker
below is `"docs.complete"` to match (not bare `"complete"`, which would resolve to a
top-level `maestro.complete` that does not exist). Every test here is written against the
shared `subject`/`sandbox` machinery in `conftest.py` purely for its skip plumbing:
`pytestmark = pytest.mark.maestro_module("docs.complete")` makes the "maestro" half of the
parametrised `subject` fixture resolve to `maestro.docs.complete`, and the "legacy" half is
redirected (via the local `mtc` fixture, mirroring `test_watchdog.py`'s `wd` and
`test_upcoming.py`'s `up`) to a directly-loaded copy of the real
`scripts/mark_task_complete.py`, ignoring whatever `subject` handed back.

Three things are specific to this module:

* the shared `sandbox` fixture cannot help here for the same reason `test_watchdog.py`
  documents: it rebases the path globals of *`subject`* (the orchestrator module), and
  `mark_task_complete.py` — never imported by `orchestrator_run.py`, only invoked as a
  subprocess from it — would keep every one of its own globals (`ROADMAP_FILE`,
  `COMPLETED_TASKS`, `AUTONOMOUS_DOC`, `PROJECT_DOC`, ...) pointed at the live reference
  repo. Every test therefore gets the autouse `box` fixture, which rebases every
  `Path`-valued global of the module into a temp tree by reflection (never a
  hand-written list of names) and asserts, on the way in *and* out, that none resolves
  outside that tree;
* the legacy subject's `main()` shells out three times (the `check_verifications.py`
  auto-gate, then `gen_dependency_map.py`, then `check_roadmap_consistency.py`). The
  autouse `runs` fixture replaces `subprocess.run` with a recorder for the duration of
  each test and makes `Popen`/`call`/`check_call`/`check_output` raise, so no test can
  ever spawn a real Python subprocess; by default the recorder answers every call with
  `returncode=0, stdout=""`, and individual tests configure `.when(token, returncode=..)`
  to pin the failure branches;
* the maestro subject's `main()` makes the same three calls in-process instead —
  `maestro.verifications.run_cli`, `maestro.docs.depmap.run_cli`, `maestro.docs.
  consistency.run_cli` — whose own path globals point at the live repo and which the
  `box` fixture cannot see (it only rebases `mtc`'s own globals by reflection, not a
  module reached only via attribute). The autouse `check_engines` fixture stubs all
  three for the maestro subject the same way `runs` does for the legacy one: a clean
  pass by default, overridden per test via `.verify_returns()`/`.depmap_returns()`/
  `.guard_returns()`.

Characterisation, not specification: several surprises here are pinned, not endorsed —
see `docs/FOUND_BUGS.md`'s "M4c — task graduation" section for the details this file
pins (an `IndexError` crash on `--no-verify` with no task id, an orphaned `### ` heading
when the target section opens the file, and silent registry data loss on a corrupt
`completed_tasks.json`).
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.reference_repo import LEGACY_REPO

pytestmark = pytest.mark.maestro_module("docs.complete")

REFERENCE_SCRIPT = "mark_task_complete.py"


# --- locating the subject ---------------------------------------------------------------

_LEGACY_CACHE: dict[str, object] = {}


def _load_legacy_complete():
    """Import the reference's graduation script by path. Skips when the repo is absent.

    No import-time side effects worth guarding — no `sys.path` insertion, no
    `load_dotenv` — so no credential can enter the process here. Loading it under the
    name `_legacy_mark_task_complete` also keeps its `__name__ == "__main__"` guard from
    running `main()`.
    """
    if LEGACY_REPO is None:
        pytest.skip(
            "reference repo unknown: set $MAESTRO_LEGACY_REPO or write its path to .legacy_repo"
        )
    path = LEGACY_REPO / "scripts" / REFERENCE_SCRIPT
    if not path.is_file():
        pytest.skip(f"reference script not found at {path}")
    cached = _LEGACY_CACHE.get("module")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("_legacy_mark_task_complete", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _LEGACY_CACHE["module"] = module
    return module


def _is_maestro(subject) -> bool:
    return getattr(subject, "__name__", "").split(".")[0] == "maestro"


@pytest.fixture
def mtc(subject):
    """The module that owns the graduation functions, whichever subject is under test."""
    if _is_maestro(subject):
        return subject
    return _load_legacy_complete()


# --- the sandbox --------------------------------------------------------------------------


def _path_globals(module) -> dict[str, Path]:
    return {
        name: value
        for name, value in vars(module).items()
        if not name.startswith("__") and isinstance(value, Path)
    }


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


@pytest.fixture(autouse=True)
def box(mtc, tmp_path, monkeypatch):
    """Rebase every path global of the graduation module into a temp tree.

    Autouse and not optional: an un-rebased global here is a write against a live
    system's `docs/ROADMAP.md` or `.orchestrator/completed_tasks.json`. The name list is
    never written by hand — every `Path`-valued module global is discovered by
    reflection and rebased relative to the module's own `REPO`, and anything that
    somehow sits outside `REPO` is parked under `_outside/` rather than left alone. The
    exit assertion catches a test that reassigns a global itself.
    """
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / ".orchestrator").mkdir()
    (repo / "scripts").mkdir()

    root = getattr(mtc, "REPO", None)
    assert isinstance(root, Path), (
        "the graduation module exposes no REPO global; the reflection-based rebase "
        "cannot run and every path global would still point at a live repo"
    )
    root = root.resolve()

    original = {name: value for name, value in _path_globals(mtc).items()}
    for name, value in original.items():
        resolved = value if value.is_absolute() else (Path.cwd() / value)
        if _under(resolved, root):
            target = repo / resolved.relative_to(root)
        else:
            target = repo / "_outside" / name
        monkeypatch.setattr(mtc, name, target)

    def _leaks() -> list[str]:
        return sorted(
            name
            for name, value in _path_globals(mtc).items()
            if not _under(value if value.is_absolute() else Path.cwd() / value, tmp_path)
        )

    assert not _leaks(), (
        f"path globals still resolve outside the temp tree: {_leaks()}. "
        "Calling the subject now could write to a live system."
    )

    def _p(name: str, fallback: Path) -> Path:
        value = getattr(mtc, name, None)
        return value if isinstance(value, Path) else fallback

    yield SimpleNamespace(
        repo=repo,
        docs=repo / "docs",
        orch=repo / ".orchestrator",
        original=original,
        roadmap=_p("ROADMAP_FILE", repo / "docs" / "ROADMAP.md"),
        completed=_p("COMPLETED_TASKS", repo / ".orchestrator" / "completed_tasks.json"),
        autonomous_doc=_p("AUTONOMOUS_DOC", repo / "docs" / "AUTONOMOUS_SYSTEM.md"),
        project_doc=_p("PROJECT_DOC", repo / "docs" / "PROJECT.md"),
        gen_dep_map=_p("GEN_DEP_MAP", repo / "scripts" / "gen_dependency_map.py"),
        guard=_p("GUARD", repo / "scripts" / "check_roadmap_consistency.py"),
        check_verifs=_p("CHECK_VERIFS", repo / "scripts" / "check_verifications.py"),
        venv_python=_p("VENV_PYTHON", repo / ".venv" / "bin" / "python3"),
    )

    assert not _leaks(), f"a test left path globals resolving outside the temp tree: {_leaks()}"


# --- the subprocess recorder ---------------------------------------------------------------


class _Result:
    def __init__(self, argv, returncode=0, stdout="", stderr=""):
        self.args = argv
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _matches(argv, tokens) -> bool:
    if isinstance(argv, str):
        return all(token in argv for token in tokens)
    parts = [str(a) for a in argv]
    return all(token in parts for token in tokens)


class _Runs:
    """Stand-in for subprocess.run: records every call, spawns nothing, ever."""

    def __init__(self):
        self.calls: list[SimpleNamespace] = []
        self._plans: list[tuple[tuple, dict]] = []

    def when(self, *tokens, returncode=0, stdout="", stderr="", raises=None):
        self._plans.append(
            (tokens, {"returncode": returncode, "stdout": stdout, "stderr": stderr, "raises": raises})
        )
        return self

    def __call__(self, argv, **kwargs):
        self.calls.append(SimpleNamespace(argv=argv, kwargs=kwargs))
        for tokens, plan in self._plans:
            if _matches(argv, tokens):
                if plan["raises"] is not None:
                    raise plan["raises"]
                return _Result(argv, plan["returncode"], plan["stdout"], plan["stderr"])
        return _Result(argv, 0, "")

    @property
    def argvs(self) -> list:
        return [c.argv for c in self.calls]

    def of(self, *tokens) -> list[SimpleNamespace]:
        return [c for c in self.calls if _matches(c.argv, tokens)]

    def one(self, *tokens) -> SimpleNamespace:
        found = self.of(*tokens)
        assert len(found) == 1, f"expected exactly one {tokens} call, got {self.argvs}"
        return found[0]


@pytest.fixture(autouse=True)
def runs(monkeypatch) -> _Runs:
    """No test may spawn a process. The subject reaches every external tool via subprocess."""
    recorder = _Runs()
    monkeypatch.setattr(subprocess, "run", recorder)

    def deny(*args, **kwargs):
        raise AssertionError(f"test tried to spawn a process: {args!r}")

    for name in ("Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, deny)
    return recorder


class _CheckEngines:
    """Records + stubs the maestro subject's three former subprocess shell-outs, now
    in-process calls: `verifications.run_cli`, `doc_depmap.run_cli`,
    `doc_consistency.run_cli`. Each defaults to a clean pass (rc=0, no output); a test
    pins a failure branch via `.verify_returns()`/`.depmap_returns()`/`.guard_returns()`,
    mirroring `_Runs.when()`'s role for the legacy subject."""

    def __init__(self):
        self.verify: list[list[str]] = []
        self.depmap: list[list[str]] = []
        self.guard: list[list[str]] = []
        self._verify_result = (0, "")
        self._depmap_result = (0, "")
        self._guard_result = (0, "")

    def verify_returns(self, rc: int, out: str = "") -> None:
        self._verify_result = (rc, out)

    def depmap_returns(self, rc: int, out: str = "") -> None:
        self._depmap_result = (rc, out)

    def guard_returns(self, rc: int, out: str = "") -> None:
        self._guard_result = (rc, out)

    def _verify(self, argv):
        self.verify.append(list(argv))
        return self._verify_result

    def _depmap(self, argv):
        self.depmap.append(list(argv))
        return self._depmap_result

    def _guard(self, argv):
        self.guard.append(list(argv))
        return self._guard_result


@pytest.fixture(autouse=True)
def check_engines(mtc, monkeypatch) -> _CheckEngines:
    """R2/R3/R5 (M4c batch 2): for the maestro subject, `main()`'s three former
    subprocess shell-outs (`check_verifications.py`, `gen_dependency_map.py`,
    `check_roadmap_consistency.py`) now call sibling in-process modules directly —
    `maestro.verifications.run_cli`, `maestro.docs.depmap.run_cli`, `maestro.docs.
    consistency.run_cli` — whose own path globals point at the LIVE repo (the `box`
    fixture only rebases *this* module's own `Path` globals by reflection; it cannot see
    into a module reached only via attribute, like `maestro.verifications`). Autouse and
    not optional: an un-stubbed maestro-subject test here would run the real
    verification gate / regenerate the real `docs/dependency_map.md` / run the real
    consistency guard against this repo. Defaults every call to a clean pass so every
    existing full-`main()` test keeps working unchanged; the tests that pin a specific
    failure branch call `.verify_returns()`/`.depmap_returns()`/`.guard_returns()`
    explicitly. A no-op for the legacy subject, which reaches these three tools only via
    its own `subprocess` reference — the `runs` fixture's job."""
    engines = _CheckEngines()
    if _is_maestro(mtc):
        monkeypatch.setattr(mtc.verifications, "run_cli", engines._verify)
        monkeypatch.setattr(mtc.doc_depmap, "run_cli", engines._depmap)
        monkeypatch.setattr(mtc.doc_consistency, "run_cli", engines._guard)
    return engines


# --- on-disk helpers -------------------------------------------------------------------------


def _write_roadmap(box, text: str) -> Path:
    box.roadmap.parent.mkdir(parents=True, exist_ok=True)
    box.roadmap.write_text(text, encoding="utf-8")
    return box.roadmap


def _read_completed(box) -> list[dict]:
    return json.loads(box.completed.read_text(encoding="utf-8"))


ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


# ===========================================================================================
# module surface
# ===========================================================================================


def test_graduation_marker_constants(mtc):
    assert mtc.GRADUATION_HEADER == "## Graduated-task markers (auto-maintained)"
    assert mtc.GRADUATION_NOTE == (
        "_Appended by `scripts/mark_task_complete.py` on graduation. The marker comment is "
        "what `scripts/check_roadmap_consistency.py` looks for; feel free to enrich the prose "
        "or move the entry into the relevant section above — just keep the marker._"
    )


def test_path_globals_relative_layout(mtc):
    assert mtc.ROADMAP_FILE == mtc.REPO / "docs" / "ROADMAP.md"
    assert mtc.COMPLETED_TASKS == mtc.REPO / ".orchestrator" / "completed_tasks.json"
    assert mtc.AUTONOMOUS_DOC == mtc.REPO / "docs" / "AUTONOMOUS_SYSTEM.md"
    assert mtc.PROJECT_DOC == mtc.REPO / "docs" / "PROJECT.md"
    assert mtc.VENV_PYTHON == mtc.REPO / ".venv" / "bin" / "python3"


# ===========================================================================================
# now_iso
# ===========================================================================================


def test_now_iso_matches_the_zulu_format(mtc):
    assert ISO_Z.match(mtc.now_iso())


# ===========================================================================================
# _python
# ===========================================================================================


def test_python_falls_back_to_sys_executable_when_no_venv(mtc, box):
    assert not box.venv_python.exists()
    assert mtc._python() == sys.executable


def test_python_prefers_the_venv_interpreter_when_present(mtc, box):
    box.venv_python.parent.mkdir(parents=True, exist_ok=True)
    box.venv_python.write_text("#!/bin/sh\n", encoding="utf-8")
    assert mtc._python() == str(box.venv_python)


# ===========================================================================================
# inferred_doc
# ===========================================================================================


def test_inferred_doc_b_series_goes_to_autonomous_system(mtc, box):
    assert mtc.inferred_doc("B12") == box.autonomous_doc


def test_inferred_doc_b_prefix_check_is_case_insensitive(mtc, box):
    assert mtc.inferred_doc("b12") == box.autonomous_doc


def test_inferred_doc_non_b_series_goes_to_project_doc(mtc, box):
    assert mtc.inferred_doc("T5") == box.project_doc


def test_inferred_doc_stringifies_a_non_string_task_id(mtc, box):
    assert mtc.inferred_doc(5) == box.project_doc


# ===========================================================================================
# remove_task_section
# ===========================================================================================


def test_remove_task_section_strips_heading_yaml_and_trailing_prose(mtc):
    content = (
        "# ROADMAP\n\n"
        "### T1 — First Task\n\n"
        "```yaml\n"
        "id: T1\n"
        "title: First Task\n"
        "mode: autonomous\n"
        "```\n\n"
        "Some trailing prose about T1.\n"
    )

    new_content, title = mtc.remove_task_section(content, "T1")

    assert title == "First Task"
    assert "### T1" not in new_content
    assert "id: T1" not in new_content
    assert "trailing prose about T1" not in new_content
    assert new_content.startswith("# ROADMAP")


def test_remove_task_section_leaves_a_later_section_intact(mtc):
    content = (
        "# ROADMAP\n\n"
        "### T1 — First\n\n"
        "```yaml\nid: T1\ntitle: First\n```\n\n"
        "Detail: prose for T1.\n\n"
        "### T2 — Second\n\n"
        "```yaml\nid: T2\ntitle: Second\n```\n"
    )

    new_content, title = mtc.remove_task_section(content, "T1")

    assert title == "First"
    assert "T1" not in new_content
    assert "### T2 — Second" in new_content
    assert "id: T2" in new_content


def test_remove_task_section_stops_at_a_rule_when_no_further_heading_follows(mtc):
    content = (
        "# ROADMAP\n\n"
        "### T1 — First\n\n"
        "```yaml\nid: T1\ntitle: First\n```\n\n"
        "Detail: prose.\n"
        "---\n"
        "\n"
        "Some trailing appendix that isn't a task.\n"
    )

    new_content, title = mtc.remove_task_section(content, "T1")

    assert title == "First"
    assert "id: T1" not in new_content
    assert "Detail: prose." not in new_content
    assert "---" in new_content
    assert "Some trailing appendix" in new_content


def test_remove_task_section_runs_to_eof_when_nothing_follows(mtc):
    content = (
        "# ROADMAP\n\n"
        "### T1 — Only\n\n"
        "```yaml\nid: T1\ntitle: Only\n```\n\n"
        "Detail: the last thing in the file.\n"
    )

    new_content, title = mtc.remove_task_section(content, "T1")

    assert title == "Only"
    assert new_content == "# ROADMAP\n\n"


def test_remove_task_section_orphans_the_heading_when_the_section_opens_the_file(mtc):
    """FOUND_BUGS: `start` is found via `content.rfind("\\n### ", 0, m.start())`, which
    requires a *leading* newline before the heading. When the target section is the very
    first thing in the document there is no such newline, so `head == -1`, `start` falls
    back to the yaml fence's own position, and the `### ` heading line is left behind —
    only the yaml block and trailing prose are actually removed."""
    content = "### T1 — Only\n\n```yaml\nid: T1\ntitle: Only\n```\n"

    new_content, title = mtc.remove_task_section(content, "T1")

    assert title == "Only"
    assert new_content == "### T1 — Only\n\n"


def test_remove_task_section_task_not_found_returns_none_title_and_unchanged_content(mtc):
    content = "# ROADMAP\n\n### T1 — Only\n\n```yaml\nid: T1\ntitle: Only\n```\n"

    new_content, title = mtc.remove_task_section(content, "T9")

    assert title is None
    assert new_content == content


def test_remove_task_section_title_defaults_to_the_task_id_when_missing(mtc):
    content = "# ROADMAP\n\n### T1\n\n```yaml\nid: T1\n```\n"

    _new_content, title = mtc.remove_task_section(content, "T1")

    assert title == "T1"


def test_remove_task_section_skips_a_malformed_yaml_block_and_finds_the_next(mtc):
    content = (
        "```yaml\n"
        "id: T1\n"
        "  bad: [1, 2\n"
        "```\n\n"
        "### T2 — Real\n\n"
        "```yaml\nid: T2\ntitle: Real\n```\n"
    )

    new_content, title = mtc.remove_task_section(content, "T2")

    assert title == "Real"
    assert "id: T2" not in new_content


def test_remove_task_section_skips_a_yaml_block_that_is_not_a_mapping(mtc):
    content = (
        "```yaml\n"
        "- a\n"
        "- b\n"
        "```\n\n"
        "### T2 — Real\n\n"
        "```yaml\nid: T2\ntitle: Real\n```\n"
    )

    new_content, title = mtc.remove_task_section(content, "T2")

    assert title == "Real"
    assert "id: T2" not in new_content


def test_remove_task_section_matches_a_numeric_yaml_id_by_string_comparison(mtc):
    content = "# ROADMAP\n\n### Numeric\n\n```yaml\nid: 7\ntitle: Numeric ID\n```\n"

    new_content, title = mtc.remove_task_section(content, "7")

    assert title == "Numeric ID"
    assert "id: 7" not in new_content


def test_remove_task_section_collapses_three_or_more_blank_lines_left_behind(mtc):
    content = (
        "Intro line.\n\n\n"
        "### T1 — First\n\n"
        "```yaml\nid: T1\ntitle: First\n```\n\n\n\n"
        "### T2 — Second\n\n```yaml\nid: T2\ntitle: Second\n```\n"
    )

    new_content, _title = mtc.remove_task_section(content, "T1")

    assert not re.search(r"\n{3,}", new_content)


# ===========================================================================================
# append_completed
# ===========================================================================================


def test_append_completed_creates_the_registry_when_absent(mtc, box):
    assert not box.completed.exists()

    mtc.append_completed("T1", "Title One", "docs/PROJECT.md")

    assert box.completed.exists()
    entries = _read_completed(box)
    assert entries == [
        {
            "id": "T1",
            "title": "Title One",
            "completed_at": entries[0]["completed_at"],
            "graduated_to": "docs/PROJECT.md",
        }
    ]
    assert ISO_Z.match(entries[0]["completed_at"])


def test_append_completed_creates_parent_directories(mtc, box):
    assert not box.completed.parent.exists() or not box.completed.exists()
    mtc.append_completed("T1", "Title", "docs/PROJECT.md")
    assert box.completed.parent.is_dir()


def test_append_completed_writes_indented_json_with_trailing_newline(mtc, box):
    mtc.append_completed("T1", "Title", "docs/PROJECT.md")
    raw = box.completed.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert '"id": "T1"' in raw
    assert raw.startswith("[\n  {\n")


def test_append_completed_dedups_by_id_and_refreshes_in_place(mtc, box):
    mtc.append_completed("T1", "First title", "docs/PROJECT.md")
    first_completed_at = _read_completed(box)[0]["completed_at"]

    mtc.append_completed("T1", "Updated title", "docs/AUTONOMOUS_SYSTEM.md")

    entries = _read_completed(box)
    assert len(entries) == 1
    assert entries[0]["title"] == "Updated title"
    assert entries[0]["graduated_to"] == "docs/AUTONOMOUS_SYSTEM.md"
    assert entries[0]["completed_at"] == first_completed_at


def test_append_completed_falsy_title_on_refresh_keeps_the_old_title(mtc, box):
    mtc.append_completed("T1", "Original", "docs/PROJECT.md")

    mtc.append_completed("T1", "", "docs/PROJECT.md")

    assert _read_completed(box)[0]["title"] == "Original"


def test_append_completed_adds_completed_at_only_if_missing_on_refresh(mtc, box):
    box.completed.parent.mkdir(parents=True, exist_ok=True)
    box.completed.write_text(
        json.dumps([{"id": "T1", "title": "T", "graduated_to": "docs/PROJECT.md"}]),
        encoding="utf-8",
    )

    mtc.append_completed("T1", "T", "docs/PROJECT.md")

    entries = _read_completed(box)
    assert ISO_Z.match(entries[0]["completed_at"])


def test_append_completed_preserves_other_entries(mtc, box):
    mtc.append_completed("T1", "First", "docs/PROJECT.md")
    mtc.append_completed("T2", "Second", "docs/PROJECT.md")

    entries = _read_completed(box)
    assert {e["id"] for e in entries} == {"T1", "T2"}


def test_append_completed_corrupt_registry_silently_discards_prior_entries(mtc, box):
    """FOUND_BUGS: the `except Exception: existing = []` fallback treats a corrupt
    `completed_tasks.json` the same as a missing one, then overwrites the file with a
    list containing only the current task — every previously graduated task in the
    registry is permanently lost, with no error surfaced."""
    box.completed.parent.mkdir(parents=True, exist_ok=True)
    box.completed.write_text("{not valid json", encoding="utf-8")

    mtc.append_completed("T2", "Second", "docs/PROJECT.md")

    entries = _read_completed(box)
    assert entries == [
        {
            "id": "T2",
            "title": "Second",
            "completed_at": entries[0]["completed_at"],
            "graduated_to": "docs/PROJECT.md",
        }
    ]


# ===========================================================================================
# ensure_graduation_marker
# ===========================================================================================


def test_ensure_graduation_marker_creates_the_doc_with_header_and_stub(mtc, box):
    wrote = mtc.ensure_graduation_marker("T1", "Title One", box.project_doc)

    assert wrote is True
    text = box.project_doc.read_text(encoding="utf-8")
    assert mtc.GRADUATION_HEADER in text
    assert mtc.GRADUATION_NOTE in text
    assert f"<!-- graduated: T1 -->" in text
    assert "**T1** — Title One" in text


def test_ensure_graduation_marker_appends_header_block_after_existing_content(mtc, box):
    box.project_doc.parent.mkdir(parents=True, exist_ok=True)
    box.project_doc.write_text("# Project\n\nSome existing prose.\n", encoding="utf-8")

    mtc.ensure_graduation_marker("T1", "Title", box.project_doc)

    text = box.project_doc.read_text(encoding="utf-8")
    assert text.startswith("# Project\n\nSome existing prose.\n\n" + mtc.GRADUATION_HEADER)


def test_ensure_graduation_marker_blank_existing_file_has_no_prefix(mtc, box):
    box.project_doc.parent.mkdir(parents=True, exist_ok=True)
    box.project_doc.write_text("   \n\n", encoding="utf-8")

    mtc.ensure_graduation_marker("T1", "Title", box.project_doc)

    text = box.project_doc.read_text(encoding="utf-8")
    assert text.startswith(mtc.GRADUATION_HEADER)


def test_ensure_graduation_marker_appends_stub_only_when_header_already_present(mtc, box):
    box.project_doc.parent.mkdir(parents=True, exist_ok=True)
    box.project_doc.write_text(
        f"{mtc.GRADUATION_HEADER}\n\n{mtc.GRADUATION_NOTE}\n\n- **T0** — Old one <!-- graduated: T0 -->\n",
        encoding="utf-8",
    )

    wrote = mtc.ensure_graduation_marker("T1", "Title", box.project_doc)

    assert wrote is True
    text = box.project_doc.read_text(encoding="utf-8")
    assert text.count(mtc.GRADUATION_HEADER) == 1
    assert "<!-- graduated: T0 -->" in text
    assert "<!-- graduated: T1 -->" in text


def test_ensure_graduation_marker_is_idempotent_when_marker_already_present(mtc, box):
    box.project_doc.parent.mkdir(parents=True, exist_ok=True)
    box.project_doc.write_text("<!-- graduated: T1 -->\n", encoding="utf-8")

    wrote = mtc.ensure_graduation_marker("T1", "Title", box.project_doc)

    assert wrote is False


def test_ensure_graduation_marker_checks_both_docs_not_just_the_target(mtc, box):
    """A marker already present in `AUTONOMOUS_DOC` blocks a write to `PROJECT_DOC` too —
    the idempotency check scans both living docs, not just the one passed in."""
    box.autonomous_doc.parent.mkdir(parents=True, exist_ok=True)
    box.autonomous_doc.write_text("<!-- graduated: T1 -->\n", encoding="utf-8")

    wrote = mtc.ensure_graduation_marker("T1", "Title", box.project_doc)

    assert wrote is False
    assert not box.project_doc.exists()


# ===========================================================================================
# main — usage / argument handling
# ===========================================================================================


def test_main_no_args_prints_usage_and_returns_1(mtc, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["mark_task_complete.py"])

    rc = mtc.main()

    assert rc == 1
    assert "Usage: mark_task_complete.py" in capsys.readouterr().out


def test_main_no_verify_alone_with_no_task_id_raises_indexerror(mtc, monkeypatch):
    """FOUND_BUGS: the `len(sys.argv) < 2` usage guard runs *before* `--no-verify` is
    filtered out of `args`, so `mark_task_complete.py --no-verify` (argv length 2) sails
    past the guard, `args` then becomes `[]` once `--no-verify` is stripped, and
    `task_id = args[0]` raises an uncaught `IndexError` instead of printing usage."""
    monkeypatch.setattr(sys, "argv", ["mark_task_complete.py", "--no-verify"])

    with pytest.raises(IndexError):
        mtc.main()


# ===========================================================================================
# main — the verification gate
# ===========================================================================================


def test_main_no_verify_skips_the_gate_subprocess(mtc, monkeypatch, box, runs, check_engines):
    _write_roadmap(box, "# ROADMAP\n\n### T1\n\n```yaml\nid: T1\ntitle: T1\n```\n")
    monkeypatch.setattr(sys, "argv", ["mark_task_complete.py", "T1", "--no-verify"])

    rc = mtc.main()

    assert rc == 0
    if _is_maestro(mtc):
        assert check_engines.verify == []
    else:
        assert runs.of(str(box.check_verifs)) == []


def test_main_gate_failure_refuses_to_graduate_and_leaves_roadmap_untouched(
    mtc, monkeypatch, box, runs, capsys, check_engines
):
    original = _write_roadmap(box, "# ROADMAP\n\n### T1\n\n```yaml\nid: T1\ntitle: T1\n```\n").read_text()
    monkeypatch.setattr(sys, "argv", ["mark_task_complete.py", "T1"])
    if _is_maestro(mtc):
        check_engines.verify_returns(1, "gate failed\n")
    else:
        runs.when(str(box.check_verifs), returncode=1, stderr="gate failed\n")

    rc = mtc.main()

    assert rc == 1
    assert "Refusing to graduate T1" in capsys.readouterr().err
    assert box.roadmap.read_text() == original
    assert not box.completed.exists()


def test_main_gate_call_arguments(mtc, monkeypatch, box, runs, check_engines):
    _write_roadmap(box, "# ROADMAP\n\n### T1\n\n```yaml\nid: T1\ntitle: T1\n```\n")
    monkeypatch.setattr(sys, "argv", ["mark_task_complete.py", "T1"])

    mtc.main()

    if _is_maestro(mtc):
        # R3: in-process call to `maestro.verifications.run_cli` — no interpreter/script
        # path/cwd left to assert on, just the argv `main()` used to build for it.
        assert check_engines.verify == [["T1", str(box.repo), "--auto-only"]]
        return

    call = runs.one(str(box.check_verifs))
    assert call.argv == [mtc._python(), str(box.check_verifs), "T1", str(box.repo), "--auto-only"]
    assert call.kwargs["cwd"] == str(box.repo)


# ===========================================================================================
# main — full graduation flow
# ===========================================================================================


def test_main_full_success_removes_section_updates_registry_and_writes_marker(mtc, monkeypatch, box, runs):
    _write_roadmap(box, "# ROADMAP\n\n### T5 — Widget\n\n```yaml\nid: T5\ntitle: Widget\n```\n")
    monkeypatch.setattr(sys, "argv", ["mark_task_complete.py", "T5", "--no-verify"])

    rc = mtc.main()

    assert rc == 0
    assert "id: T5" not in box.roadmap.read_text()
    entries = _read_completed(box)
    assert entries[0]["id"] == "T5"
    assert entries[0]["graduated_to"] == "docs/PROJECT.md"
    assert "<!-- graduated: T5 -->" in box.project_doc.read_text()
    assert not box.autonomous_doc.exists()


def test_main_b_series_task_graduates_into_the_autonomous_doc(mtc, monkeypatch, box, runs):
    _write_roadmap(box, "# ROADMAP\n\n### B1 — Orchestrator work\n\n```yaml\nid: B1\ntitle: Orchestrator work\n```\n")
    monkeypatch.setattr(sys, "argv", ["mark_task_complete.py", "B1", "--no-verify"])

    rc = mtc.main()

    assert rc == 0
    assert _read_completed(box)[0]["graduated_to"] == "docs/AUTONOMOUS_SYSTEM.md"
    assert "<!-- graduated: B1 -->" in box.autonomous_doc.read_text()


def test_main_task_not_found_still_registers_completion_with_task_id_as_title(mtc, monkeypatch, box, runs, capsys):
    original = _write_roadmap(box, "# ROADMAP\n\n### T1\n\n```yaml\nid: T1\ntitle: T1\n```\n").read_text()
    monkeypatch.setattr(sys, "argv", ["mark_task_complete.py", "T9", "--no-verify"])

    rc = mtc.main()

    assert rc == 0
    assert "Task T9 not found in ROADMAP.md" in capsys.readouterr().out
    assert box.roadmap.read_text() == original  # never written when title is None
    entries = _read_completed(box)
    assert entries[0]["id"] == "T9"
    assert entries[0]["title"] == "T9"


def test_main_dep_map_regen_failure_stops_before_the_consistency_guard(mtc, monkeypatch, box, runs, check_engines):
    _write_roadmap(box, "# ROADMAP\n\n### T1\n\n```yaml\nid: T1\ntitle: T1\n```\n")
    monkeypatch.setattr(sys, "argv", ["mark_task_complete.py", "T1", "--no-verify"])
    if _is_maestro(mtc):
        check_engines.depmap_returns(1, "boom")
    else:
        runs.when(str(box.gen_dep_map), returncode=1, stdout="boom")

    rc = mtc.main()

    assert rc == 1
    if _is_maestro(mtc):
        assert check_engines.guard == []
    else:
        assert runs.of(str(box.guard)) == []
    # the registry write and marker still happened before the regen step ran
    assert _read_completed(box)[0]["id"] == "T1"


def test_main_consistency_guard_failure_propagates_its_own_returncode(mtc, monkeypatch, box, runs, check_engines):
    _write_roadmap(box, "# ROADMAP\n\n### T1\n\n```yaml\nid: T1\ntitle: T1\n```\n")
    monkeypatch.setattr(sys, "argv", ["mark_task_complete.py", "T1", "--no-verify"])
    if _is_maestro(mtc):
        check_engines.guard_returns(3, "inconsistent")
    else:
        runs.when(str(box.guard), returncode=3, stdout="inconsistent")

    rc = mtc.main()

    assert rc == 3


def test_main_second_graduation_of_the_same_task_leaves_the_marker_untouched(mtc, monkeypatch, box, runs):
    _write_roadmap(box, "# ROADMAP\n\n### T1\n\n```yaml\nid: T1\ntitle: T1\n```\n")
    monkeypatch.setattr(sys, "argv", ["mark_task_complete.py", "T1", "--no-verify"])
    mtc.main()
    marker_text_first = box.project_doc.read_text()

    _write_roadmap(box, "# ROADMAP\n")  # T1 already gone; second run finds nothing
    rc = mtc.main()

    assert rc == 0
    assert box.project_doc.read_text() == marker_text_first
