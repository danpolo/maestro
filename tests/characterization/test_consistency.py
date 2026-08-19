"""Pinned behaviour of the reference `scripts/check_roadmap_consistency.py` (M4c batch 2,
Wave A — the 3-doc engine's consistency guard, R2).

Characterisation, not specification: where the reference implementation does something
surprising, the test pins the surprise and `docs/FOUND_BUGS.md` records it. Nothing here
asserts what the code *should* do.

`check_roadmap_consistency.py` is a **standalone script** in the reference, not a function
living inside the `orchestrator_run.py` monolith — same shape as `gen_dependency_map.py`
(`test_depmap.py`) and `gen_upcoming.py` (`test_upcoming.py`), both already characterised
this way. Per `docs/plans/2026-08-18-m4c-superseded-sidecars.md` §4/§2b this becomes
`maestro/docs/consistency.py` (R2) in **Wave B** of M4c batch 2; `pytestmark` below
declares the target module name.

Its seven checks (a)-(g) are documented in the reference module's own docstring; this file
groups tests the same way. Two of them, (b) and (g), shell out to sibling scripts
(`gen_dependency_map.py --check`, `gen_upcoming.py --check`) in the reference, via
`subprocess.run` — so the legacy subject's tests write small throwaway stub scripts at the
module's (rebased) `GEN_SCRIPT`/`GEN_UPCOMING` globals rather than depending on the real
generators. The maestro subject calls its sibling *modules* in-process instead
(`maestro.docs.depmap.run_cli(["--check"])` / `maestro.docs.upcoming.run_cli(["--check"])`)
— those live at `crc.gen_dependency_map`/`crc.gen_upcoming` (bound under the reference's
own script-derived names so both subjects can be driven through the same `_stub_check`
helper) and are stubbed by the autouse `run_cli_stubs` fixture instead of a throwaway
script on disk. This file only pins `check_roadmap_consistency.py` itself, not its
callees (those get their own characterisation files, already present as
`test_depmap.py`/`test_upcoming.py`).

Like `test_depmap.py`, the module is loaded directly by file path
(`importlib.util.spec_from_file_location`) rather than through the shared `subject`
fixture's legacy half (which loads `orchestrator_run`, a different module that never
imports this one). Every one of its `Path`-valued module globals (`REPO`, `ROADMAP`,
`DEPMAP`, `REGISTRY`, and — legacy subject only — `GEN_SCRIPT`, `GEN_UPCOMING`) is rebased
into a temp tree by reflection before any test runs — never a hand-written list. `DOC_FILES`
is the one exception worth calling out: it is a *list* of `Path`s, not a `Path` itself, so
the generic reflection sweep (which only catches `isinstance(value, Path)`) does not see
it — the `box` fixture below rebases it explicitly, by name, in addition to the reflection
sweep. The maestro subject has no `GEN_SCRIPT`/`GEN_UPCOMING` globals at all (R2 removed
them, see `maestro/docs/consistency.py`'s own docstring) — `box` falls back to a dummy path
for those two, which the maestro-subject tests never touch.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.reference_repo import LEGACY_REPO

pytestmark = pytest.mark.maestro_module("docs.consistency")

REFERENCE_SCRIPT = "check_roadmap_consistency.py"


# --- locating the subject ------------------------------------------------------------

_LEGACY_CACHE: dict[str, object] = {}


def _load_legacy_consistency():
    """Import the reference script by path, once per session. No import-time side
    effects worth guarding (no `sys.path` insertion, no dotenv), and loading it under an
    aliased name keeps its `__name__ == "__main__"` guard inert."""
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
    spec = importlib.util.spec_from_file_location("_legacy_check_roadmap_consistency", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _LEGACY_CACHE["module"] = module
    return module


def _is_maestro(subject) -> bool:
    return getattr(subject, "__name__", "").split(".")[0] == "maestro"


@pytest.fixture
def crc(subject):
    """The module that owns the consistency-check functions, whichever subject is under
    test."""
    if _is_maestro(subject):
        return subject
    return _load_legacy_consistency()


# --- the sandbox -----------------------------------------------------------------------


def _path_globals(module) -> dict[str, Path]:
    return {
        name: value
        for name, value in vars(module).items()
        if not name.startswith("__") and isinstance(value, Path)
    }


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


@pytest.fixture(autouse=True)
def box(request, tmp_path, monkeypatch):
    """Rebase every path global of `crc` into a temp tree, `DOC_FILES` included.

    Autouse and not optional: an un-rebased global here is a read (or, for `main()`'s
    warning path, a `relative_to`) against the live reference repo. Like `test_depmap.py`'s
    `box`, resolved lazily via `request.getfixturevalue` so tests that never touch `crc`
    (there are none in this file yet, but the shape is kept consistent with its sibling)
    don't pay for a `subject` parametrisation they don't use.
    """
    if "crc" not in request.fixturenames:
        yield None
        return
    crc = request.getfixturevalue("crc")

    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / ".orchestrator").mkdir()
    (repo / "scripts").mkdir()

    root = getattr(crc, "REPO", None)
    assert isinstance(root, Path), (
        "the consistency module exposes no REPO global; the reflection-based rebase "
        "cannot run and every path global would still point at a live repo"
    )
    root = root.resolve()

    for name, value in _path_globals(crc).items():
        resolved = value if value.is_absolute() else (Path.cwd() / value)
        target = repo / resolved.relative_to(root) if _under(resolved, root) else repo / "_outside" / name
        monkeypatch.setattr(crc, name, target)

    # DOC_FILES is a list of Paths, not a Path — the reflection sweep above never sees it.
    doc_files = getattr(crc, "DOC_FILES", None)
    if isinstance(doc_files, list):
        rebased = []
        for value in doc_files:
            resolved = value if value.is_absolute() else (Path.cwd() / value)
            rebased.append(
                repo / resolved.relative_to(root) if _under(resolved, root) else repo / "_outside" / resolved.name
            )
        monkeypatch.setattr(crc, "DOC_FILES", rebased)

    def _leaks() -> list[str]:
        found = sorted(
            name
            for name, value in _path_globals(crc).items()
            if not _under(value if value.is_absolute() else Path.cwd() / value, tmp_path)
        )
        doc_files_now = getattr(crc, "DOC_FILES", None) or []
        found += sorted(
            f"DOC_FILES[{i}]"
            for i, value in enumerate(doc_files_now)
            if not _under(value if value.is_absolute() else Path.cwd() / value, tmp_path)
        )
        return found

    assert not _leaks(), (
        f"path globals still resolve outside the temp tree: {_leaks()}. "
        "Calling the subject now could read/write a live system."
    )

    # R2: the maestro subject has no GEN_SCRIPT/GEN_UPCOMING globals at all — its
    # sibling calls are in-process modules (`crc.gen_dependency_map`/`crc.gen_upcoming`,
    # stubbed by `run_cli_stubs`), not on-disk scripts. Fall back to a dummy path the
    # maestro-subject tests never touch, rather than crashing on a missing attribute.
    def _p(name: str, fallback: Path) -> Path:
        value = getattr(crc, name, None)
        return value if isinstance(value, Path) else fallback

    yield SimpleNamespace(
        repo=repo,
        roadmap=crc.ROADMAP,
        depmap=crc.DEPMAP,
        registry=crc.REGISTRY,
        gen_script=_p("GEN_SCRIPT", repo / "scripts" / "gen_dependency_map.py"),
        gen_upcoming=_p("GEN_UPCOMING", repo / "scripts" / "gen_upcoming.py"),
        doc_files=crc.DOC_FILES,
    )

    assert not _leaks(), f"a test left path globals resolving outside the temp tree: {_leaks()}"


# --- helpers --------------------------------------------------------------------------


def _write_roadmap(box, text: str) -> None:
    box.roadmap.write_text(text, encoding="utf-8")


def _write_registry(box, entries: list | dict | str) -> None:
    box.registry.write_text(json.dumps(entries), encoding="utf-8")


def _write_stub(path: Path, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
    """A throwaway python script standing in for `gen_dependency_map.py --check` /
    `gen_upcoming.py --check`, so checks (b)/(g) can be pinned without depending on the
    real generators (which get their own characterisation elsewhere)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "import sys\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({returncode})\n",
        encoding="utf-8",
    )


class _StubRunCli:
    """Stand-in for `maestro.docs.depmap.run_cli`/`maestro.docs.upcoming.run_cli` — the
    maestro subject's in-process replacement for the legacy `subprocess.run([...,
    "--check"])` calls `_write_stub`'s throwaway scripts model. Returns
    `(rc, stdout + stderr)`, mirroring `check_depmap_no_drift`/`check_upcoming_no_drift`'s
    own (pre-rewire) `res.stdout + res.stderr` combination — both `run_cli`s now combine
    the two streams themselves (see their own docstrings) — so the same expected strings
    pin both subjects."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self._result: tuple[int, str] = (0, "")

    def returns(self, rc: int, stdout: str = "", stderr: str = "") -> None:
        self._result = (rc, stdout + stderr)

    def __call__(self, argv):
        self.calls.append(list(argv))
        return self._result


@pytest.fixture(autouse=True)
def run_cli_stubs(request, monkeypatch):
    """R2 (M4c batch 2): the maestro subject's `check_depmap_no_drift`/
    `check_upcoming_no_drift` call `maestro.docs.depmap.run_cli`/`maestro.docs.upcoming.
    run_cli` in-process instead of the legacy `subprocess.run([..., "--check"])` calls
    `_write_stub`'s throwaway scripts stand in for. Both sibling modules' own path
    globals point at the LIVE repo — this file's `box` fixture only rebases `crc`'s own
    globals by reflection, not a module reached only via attribute — so an un-stubbed
    maestro-subject test here would run the real dependency-map/UPCOMING generator
    against this repo. Autouse and not optional, for that reason. A no-op for the legacy
    subject, which reaches these two tools only via a real (sandboxed, `box`-rebased)
    on-disk stub script.
    """
    if "crc" not in request.fixturenames:
        return SimpleNamespace(depmap=None, upcoming=None)
    crc = request.getfixturevalue("crc")
    depmap_stub = _StubRunCli()
    upcoming_stub = _StubRunCli()
    if _is_maestro(crc):
        monkeypatch.setattr(crc.gen_dependency_map, "run_cli", depmap_stub)
        monkeypatch.setattr(crc.gen_upcoming, "run_cli", upcoming_stub)
    return SimpleNamespace(depmap=depmap_stub, upcoming=upcoming_stub)


def _stub_check(crc, run_cli_stubs, box, which: str, *,
                returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
    """Configure the depmap/upcoming `--check` outcome for whichever subject `crc` is:
    a real throwaway script for the legacy subject, or the maestro subject's `run_cli`
    stand-in (`run_cli_stubs`). `which` is `"depmap"` or `"upcoming"`."""
    if _is_maestro(crc):
        stub = run_cli_stubs.depmap if which == "depmap" else run_cli_stubs.upcoming
        stub.returns(returncode, stdout, stderr)
    else:
        path = box.gen_script if which == "depmap" else box.gen_upcoming
        _write_stub(path, returncode=returncode, stdout=stdout, stderr=stderr)


# =======================================================================================
# _roadmap_tasks
# =======================================================================================


def test_roadmap_tasks_extracts_dict_blocks_with_an_id(crc, box):
    _write_roadmap(box, "```yaml\nid: T1\ntitle: real\n```\n")
    assert crc._roadmap_tasks() == [{"id": "T1", "title": "real"}]


def test_roadmap_tasks_skips_blocks_with_falsy_id(crc, box):
    """`task.get("id")` is a truthiness check, not a presence check — `id: 0` and
    `id: ""` are both silently dropped, not just a missing `id` key."""
    _write_roadmap(
        box,
        "```yaml\nid: 0\ntitle: zero id\n```\n"
        "```yaml\nid: \"\"\ntitle: empty id\n```\n"
        "```yaml\nid: T1\ntitle: real\n```\n",
    )
    assert crc._roadmap_tasks() == [{"id": "T1", "title": "real"}]


def test_roadmap_tasks_skips_unparsable_yaml_block(crc, box):
    _write_roadmap(
        box,
        "```yaml\n: : bad yaml [\n```\n"
        "```yaml\nid: T1\ntitle: real\n```\n",
    )
    assert crc._roadmap_tasks() == [{"id": "T1", "title": "real"}]


def test_roadmap_tasks_skips_non_dict_yaml_block(crc, box):
    """A fenced ```yaml block that parses to a list or a bare scalar (valid YAML, just
    not a mapping) is silently skipped — `isinstance(task, dict)` guards it."""
    _write_roadmap(
        box,
        "```yaml\n- a\n- b\n```\n"
        "```yaml\njust a string\n```\n"
        "```yaml\nid: T1\ntitle: real\n```\n",
    )
    assert crc._roadmap_tasks() == [{"id": "T1", "title": "real"}]


def test_roadmap_tasks_ignores_non_yaml_fenced_blocks(crc, box):
    _write_roadmap(box, "```python\nprint('hi')\n```\n\n```yaml\nid: T1\n```\n")
    assert [t["id"] for t in crc._roadmap_tasks()] == ["T1"]


def test_roadmap_tasks_missing_file_raises(crc, box):
    """No existence guard on ROADMAP — a missing docs/ROADMAP.md crashes with
    FileNotFoundError rather than degrading gracefully like the REGISTRY-absent path
    in main() does."""
    with pytest.raises(FileNotFoundError):
        crc._roadmap_tasks()


# =======================================================================================
# _registry_ids
# =======================================================================================


def test_registry_ids_missing_file_is_empty_set(crc, box):
    assert crc._registry_ids() == set()


def test_registry_ids_corrupt_json_is_empty_set(crc, box):
    box.registry.write_text("{not json", encoding="utf-8")
    assert crc._registry_ids() == set()


def test_registry_ids_collects_ids_skips_entries_without_id(crc, box):
    _write_registry(box, [{"id": "T1"}, {"foo": 1}, {"id": "T2"}])
    assert crc._registry_ids() == {"T1", "T2"}


def test_registry_ids_stringifies_non_string_ids(crc, box):
    _write_registry(box, [{"id": 7}])
    assert crc._registry_ids() == {"7"}


def test_registry_ids_empty_dict_json_is_empty_set(crc, box):
    """Valid JSON, but shaped as `{}` rather than `[]` — iterating an empty dict yields
    no entries, so this one degrades harmlessly (unlike the non-empty-dict case below)."""
    _write_registry(box, {})
    assert crc._registry_ids() == set()


def test_registry_ids_non_list_json_raises_attributeerror(crc, box):
    """Only `json.JSONDecodeError` is caught. If `.orchestrator/completed_tasks.json`
    is ever valid JSON but not a list of objects — a dict with string keys, or a bare
    string — `for r in reg: r.get(...)` blows up with AttributeError instead of
    degrading like the missing-file/corrupt-JSON cases above. In the pre-commit hook
    this crashes the whole check ungracefully rather than reporting a clean problem
    list. See docs/FOUND_BUGS.md."""
    _write_registry(box, {"a": 1})
    with pytest.raises(AttributeError):
        crc._registry_ids()

    _write_registry(box, "hello")
    with pytest.raises(AttributeError):
        crc._registry_ids()


# =======================================================================================
# _deps
# =======================================================================================


def test_deps_missing_key_is_empty_list(crc):
    assert crc._deps({}) == []


def test_deps_string_value_becomes_single_item_list(crc):
    assert crc._deps({"deps": "T1"}) == ["T1"]


def test_deps_empty_string_value_is_empty_list(crc):
    assert crc._deps({"deps": ""}) == []


def test_deps_list_value_stringifies_each_item(crc):
    """Non-string entries (including `None`) are coerced with `str()`, not filtered
    out — a `None` in the list becomes the literal string `'None'`."""
    assert crc._deps({"deps": ["T1", 5, None]}) == ["T1", "5", "None"]


# =======================================================================================
# check_no_lingering_complete (a)
# =======================================================================================


def test_no_lingering_complete_flags_status_complete_tasks(crc, box):
    _write_roadmap(box, "```yaml\nid: T1\nstatus: complete\n```\n")
    problems: list[str] = []
    crc.check_no_lingering_complete(problems)
    assert problems == [
        "(a) task T1 has status:complete but still lives in ROADMAP.md "
        "— graduate it with scripts/mark_task_complete.py"
    ]


def test_no_lingering_complete_ignores_pending_tasks(crc, box):
    _write_roadmap(box, "```yaml\nid: T1\nstatus: pending\n```\n")
    problems: list[str] = []
    crc.check_no_lingering_complete(problems)
    assert problems == []


# =======================================================================================
# check_depmap_no_drift (b) / check_upcoming_no_drift (g)
# =======================================================================================


def test_depmap_no_drift_clean_run_adds_no_problem(crc, box, run_cli_stubs):
    _stub_check(crc, run_cli_stubs, box, "depmap", returncode=0)
    problems: list[str] = []
    crc.check_depmap_no_drift(problems)
    assert problems == []


def test_depmap_no_drift_nonzero_exit_reports_combined_stdout_stderr(crc, box, run_cli_stubs):
    _stub_check(crc, run_cli_stubs, box, "depmap", returncode=1, stdout="out line\n", stderr="err line\n")
    problems: list[str] = []
    crc.check_depmap_no_drift(problems)
    assert problems == [
        "(b) docs/dependency_map.md is stale/drifted — run "
        "`python scripts/gen_dependency_map.py`:\n      out line\nerr line"
    ]


def test_depmap_no_drift_missing_script_still_reports_a_problem(crc, box, run_cli_stubs):
    """No `.exists()` guard for GEN_SCRIPT (unlike GEN_UPCOMING below) — a missing
    generator surfaces as a drift problem via the subprocess's own nonzero exit, not a
    distinct error path. Legacy-only: the maestro subject has no on-disk script that
    could go missing — `maestro.docs.depmap` is always importable, and `run_cli_stubs`
    always installs a stand-in (see `test_depmap_no_drift_clean_run_adds_no_problem`)."""
    if _is_maestro(crc):
        pytest.skip("R5: no on-disk generator to be missing — always called in-process now")
    problems: list[str] = []
    crc.check_depmap_no_drift(problems)
    assert len(problems) == 1
    assert problems[0].startswith(
        "(b) docs/dependency_map.md is stale/drifted — run `python scripts/gen_dependency_map.py`:"
    )


def test_upcoming_no_drift_missing_generator_is_skipped_silently(crc, box, run_cli_stubs):
    """Unlike GEN_SCRIPT, GEN_UPCOMING is existence-checked first — no generator means
    no subprocess call and no problem at all. Legacy-only: R7 drops this guard for the
    maestro subject — an in-process module import always "exists" — see
    `test_upcoming_no_drift_always_calls_run_cli_now` below."""
    if _is_maestro(crc):
        pytest.skip("R7: the .exists() guard was dropped — see "
                    "test_upcoming_no_drift_always_calls_run_cli_now")
    problems: list[str] = []
    crc.check_upcoming_no_drift(problems)
    assert problems == []


def test_upcoming_no_drift_always_calls_run_cli_now(crc, box, run_cli_stubs):
    """R7 target: dropping the reference's `GEN_UPCOMING.exists()` skip-guard means
    `check_upcoming_no_drift` now always calls `maestro.docs.upcoming.run_cli(["--check"])`
    — no more asymmetry with `check_depmap_no_drift`, which never had an `.exists()`
    guard to begin with. Maestro-only; the legacy reference's own guard is pinned by
    `test_upcoming_no_drift_missing_generator_is_skipped_silently` above."""
    if not _is_maestro(crc):
        pytest.skip("R7 target: maestro-only — see "
                    "test_upcoming_no_drift_missing_generator_is_skipped_silently")
    problems: list[str] = []
    crc.check_upcoming_no_drift(problems)
    assert run_cli_stubs.upcoming.calls == [["--check"]]
    assert problems == []


def test_upcoming_no_drift_clean_run_adds_no_problem(crc, box, run_cli_stubs):
    _stub_check(crc, run_cli_stubs, box, "upcoming", returncode=0)
    problems: list[str] = []
    crc.check_upcoming_no_drift(problems)
    assert problems == []


def test_upcoming_no_drift_nonzero_exit_reports_detail(crc, box, run_cli_stubs):
    _stub_check(crc, run_cli_stubs, box, "upcoming", returncode=3, stdout="u-out\n")
    problems: list[str] = []
    crc.check_upcoming_no_drift(problems)
    assert problems == [
        "(g) docs/UPCOMING.md is stale/drifted — run `python scripts/gen_upcoming.py`:\n      u-out"
    ]


# =======================================================================================
# check_deps_resolve (c)
# =======================================================================================


def test_deps_resolve_flags_dep_not_pending_or_graduated(crc, box):
    _write_roadmap(box, "```yaml\nid: T2\nstatus: pending\ndeps: [Tmissing]\n```\n")
    _write_registry(box, [])
    problems: list[str] = []
    crc.check_deps_resolve(problems)
    assert problems == [
        "(c) pending task T2 has dep 'Tmissing' that is neither a pending "
        "ROADMAP id nor a graduated registry id"
    ]


def test_deps_resolve_dep_on_another_pending_task_is_fine(crc, box):
    _write_roadmap(
        box,
        "```yaml\nid: T1\nstatus: pending\n```\n"
        "```yaml\nid: T2\nstatus: pending\ndeps: [T1]\n```\n",
    )
    _write_registry(box, [])
    problems: list[str] = []
    crc.check_deps_resolve(problems)
    assert problems == []


def test_deps_resolve_dep_on_graduated_registry_id_is_fine(crc, box):
    _write_roadmap(box, "```yaml\nid: T2\nstatus: pending\ndeps: [Tmissing]\n```\n")
    _write_registry(box, [{"id": "Tmissing"}])
    problems: list[str] = []
    crc.check_deps_resolve(problems)
    assert problems == []


def test_deps_resolve_skips_complete_tasks(crc, box):
    """A `status: complete` task's own dangling deps are not checked — only its
    presence in the roadmap is a problem (that's check (a)'s job)."""
    _write_roadmap(box, "```yaml\nid: T1\nstatus: complete\ndeps: [Tmissing]\n```\n")
    _write_registry(box, [])
    problems: list[str] = []
    crc.check_deps_resolve(problems)
    assert problems == []


# =======================================================================================
# _is_floor_only / check_resumable_coverage_gate (e)
# =======================================================================================


def test_is_floor_only_true_for_bare_ge_threshold(crc):
    assert crc._is_floor_only({"cmd": "count >= 500"}) is True


def test_is_floor_only_false_when_coverage_language_present(crc):
    assert crc._is_floor_only({"cmd": "count >= 500 over full corpus"}) is False


def test_is_floor_only_false_with_no_quantitative_assertion(crc):
    assert crc._is_floor_only({"cmd": "run the smoke test"}) is False


def test_resumable_gate_no_auto_verifications_is_a_problem(crc, box):
    _write_roadmap(
        box,
        "```yaml\nid: T1\nstatus: pending\nresumable: true\nverifications: []\n```\n",
    )
    problems: list[str] = []
    crc.check_resumable_coverage_gate(problems)
    assert problems == [
        "(e) resumable task T1 has no auto verification — a quota-bound "
        "task needs a coverage gate that only passes at 100% completion"
    ]


def test_resumable_gate_only_manual_verifications_counts_as_no_auto(crc, box):
    _write_roadmap(
        box,
        "```yaml\nid: T2\nstatus: pending\nresumable: true\n"
        "verifications:\n  - kind: manual\n    id: v1\n```\n",
    )
    problems: list[str] = []
    crc.check_resumable_coverage_gate(problems)
    assert len(problems) == 1
    assert "T2" in problems[0] and "no auto verification" in problems[0]


def test_resumable_gate_all_floor_only_autos_is_a_problem(crc, box):
    _write_roadmap(
        box,
        "```yaml\nid: T3\nstatus: pending\nresumable: true\n"
        "verifications:\n  - kind: auto\n    id: v1\n    cmd: \"count >= 500\"\n```\n",
    )
    problems: list[str] = []
    crc.check_resumable_coverage_gate(problems)
    assert problems == [
        "(e) resumable task T3 is gated only by floor thresholds "
        "(v1) — a single quota sitting clears `>= N`, so it would "
        "false-graduate. Add a coverage check (e.g. scripts/check_pairs_complete.py)"
    ]


def test_resumable_gate_one_coverage_auto_among_floors_is_fine(crc, box):
    _write_roadmap(
        box,
        "```yaml\nid: T4\nstatus: pending\nresumable: true\n"
        "verifications:\n"
        "  - kind: auto\n    id: v1\n    cmd: \"count >= 500\"\n"
        "  - kind: auto\n    id: v2\n    cmd: \"full corpus coverage check\"\n```\n",
    )
    problems: list[str] = []
    crc.check_resumable_coverage_gate(problems)
    assert problems == []


def test_resumable_gate_ignores_non_resumable_and_complete_tasks(crc, box):
    _write_roadmap(
        box,
        "```yaml\nid: T5\nstatus: pending\nresumable: false\nverifications: []\n```\n"
        "```yaml\nid: T6\nstatus: complete\nresumable: true\nverifications: []\n```\n",
    )
    problems: list[str] = []
    crc.check_resumable_coverage_gate(problems)
    assert problems == []


# =======================================================================================
# check_questions_schema (f)
# =======================================================================================


def test_questions_schema_empty_list_is_a_problem(crc, box):
    _write_roadmap(box, "```yaml\nid: T1\nstatus: pending\nquestions: []\n```\n")
    problems: list[str] = []
    crc.check_questions_schema(problems)
    assert problems == [
        "(f) task T1 has a `questions:` field that is not a non-empty list"
    ]


def test_questions_schema_non_list_value_is_a_problem(crc, box):
    _write_roadmap(box, "```yaml\nid: T7\nstatus: pending\nquestions: \"not a list\"\n```\n")
    problems: list[str] = []
    crc.check_questions_schema(problems)
    assert problems == [
        "(f) task T7 has a `questions:` field that is not a non-empty list"
    ]


def test_questions_schema_duplicate_ids_flagged(crc, box):
    _write_roadmap(
        box,
        "```yaml\nid: T2\nstatus: pending\n"
        "questions:\n  - id: q1\n    prompt: hi\n  - id: q1\n    prompt: dup\n```\n",
    )
    problems: list[str] = []
    crc.check_questions_schema(problems)
    assert problems == ["(f) task T2 has duplicate question id 'q1'"]


def test_questions_schema_questions_and_dispatch_manual_conflict(crc, box):
    _write_roadmap(
        box,
        "```yaml\nid: T3\nstatus: pending\ndispatch: manual\n"
        "questions:\n  - id: q1\n    prompt: hi\n```\n",
    )
    problems: list[str] = []
    crc.check_questions_schema(problems)
    assert problems == [
        "(f) task T3 sets both `questions:` and `dispatch: manual` — "
        "Dan either answers a question or performs the task, not both"
    ]


def test_questions_schema_missing_id_flagged_by_index(crc, box):
    _write_roadmap(
        box,
        "```yaml\nid: T4\nstatus: pending\nquestions:\n  - prompt: no id\n```\n",
    )
    problems: list[str] = []
    crc.check_questions_schema(problems)
    assert problems == ["(f) task T4 question #0 has no `id`"]


def test_questions_schema_missing_prompt_flagged_by_id(crc, box):
    _write_roadmap(
        box,
        "```yaml\nid: T5\nstatus: pending\nquestions:\n  - id: q1\n```\n",
    )
    problems: list[str] = []
    crc.check_questions_schema(problems)
    assert problems == ["(f) task T5 question 'q1' has no `prompt`"]


def test_questions_schema_empty_options_flagged(crc, box):
    _write_roadmap(
        box,
        "```yaml\nid: T6\nstatus: pending\n"
        "questions:\n  - id: q1\n    prompt: hi\n    options: []\n```\n",
    )
    problems: list[str] = []
    crc.check_questions_schema(problems)
    assert problems == [
        "(f) task T6 question 'q1' has `options` that is not a non-empty list "
        "(omit it for a free-text question)"
    ]


def test_questions_schema_well_formed_question_is_fine(crc, box):
    _write_roadmap(
        box,
        "```yaml\nid: T8\nstatus: pending\n"
        "questions:\n  - id: q1\n    prompt: hi\n    options: [a, b]\n```\n",
    )
    problems: list[str] = []
    crc.check_questions_schema(problems)
    assert problems == []


def test_questions_schema_ignores_complete_tasks(crc, box):
    _write_roadmap(
        box,
        "```yaml\nid: T9\nstatus: complete\nquestions: []\n```\n",
    )
    problems: list[str] = []
    crc.check_questions_schema(problems)
    assert problems == []


# =======================================================================================
# check_graduation_markers (d)
# =======================================================================================


def test_graduation_markers_flags_registry_id_without_marker(crc, box):
    _write_registry(box, [{"id": "T5"}, {"id": "T6"}])
    box.doc_files[0].write_text("stuff <!-- graduated: T5 --> more", encoding="utf-8")
    problems: list[str] = []
    crc.check_graduation_markers(problems)
    assert problems == [
        "(d) registry entry T6 has no `<!-- graduated: T6 -->` marker in "
        "PROJECT.md or AUTONOMOUS_SYSTEM.md — it was completed but not "
        "moved into a living doc"
    ]


def test_graduation_markers_missing_doc_files_reports_every_entry(crc, box):
    """Neither DOC_FILES entry needs to exist — `f.exists()` guards the read, so this
    degrades to "no markers found anywhere" rather than crashing."""
    _write_registry(box, [{"id": "T5"}])
    problems: list[str] = []
    crc.check_graduation_markers(problems)
    assert problems == [
        "(d) registry entry T5 has no `<!-- graduated: T5 -->` marker in "
        "PROJECT.md or AUTONOMOUS_SYSTEM.md — it was completed but not "
        "moved into a living doc"
    ]


def test_graduation_markers_marker_in_either_doc_file_satisfies(crc, box):
    _write_registry(box, [{"id": "T5"}])
    box.doc_files[1].write_text("<!-- graduated: T5 -->", encoding="utf-8")
    problems: list[str] = []
    crc.check_graduation_markers(problems)
    assert problems == []


def test_graduation_markers_iterates_registry_ids_sorted(crc, box):
    _write_registry(box, [{"id": "T9"}, {"id": "T1"}])
    problems: list[str] = []
    crc.check_graduation_markers(problems)
    ids_in_order = [p.split("registry entry ")[1].split(" ")[0] for p in problems]
    assert ids_in_order == ["T1", "T9"]


# =======================================================================================
# main() — aggregation, registry-absent degradation, exit codes
# =======================================================================================


def test_main_clean_repo_prints_summary_and_returns_zero(crc, box, capsys, run_cli_stubs):
    _write_roadmap(box, "```yaml\nid: T1\nstatus: pending\n```\n")
    _write_registry(box, [])
    _stub_check(crc, run_cli_stubs, box, "depmap", returncode=0)
    _stub_check(crc, run_cli_stubs, box, "upcoming", returncode=0)

    rc = crc.main()

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == (
        "ROADMAP consistency OK: 1 pending tasks, 0 graduated entries, dep map in sync.\n"
    )
    assert captured.err == ""


def test_main_aggregates_problems_in_check_order_and_returns_one(crc, box, capsys, run_cli_stubs):
    _write_roadmap(
        box,
        "```yaml\nid: T1\nstatus: complete\n```\n"
        "```yaml\nid: T2\nstatus: pending\ndeps: [Tmissing]\n```\n",
    )
    _write_registry(box, [])
    _stub_check(crc, run_cli_stubs, box, "depmap", returncode=0)
    _stub_check(crc, run_cli_stubs, box, "upcoming", returncode=0)

    rc = crc.main()

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert captured.err == (
        "ROADMAP consistency check FAILED:\n"
        "  - (a) task T1 has status:complete but still lives in ROADMAP.md "
        "— graduate it with scripts/mark_task_complete.py\n"
        "  - (c) pending task T2 has dep 'Tmissing' that is neither a pending "
        "ROADMAP id nor a graduated registry id\n"
    )


def test_main_registry_absent_skips_four_checks_and_warns(crc, box, capsys):
    """(b)/(c)/(d)/(g) are registry-dependent and degrade gracefully — a genuinely
    absent registry (never git-tracked, or a manual delete) prints a WARNING to stderr
    and skips them rather than false-failing. (a)/(e)/(f) still run."""
    _write_roadmap(box, "```yaml\nid: T1\nstatus: pending\n```\n")
    # No registry file written at all.

    rc = crc.main()

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == (
        "ROADMAP consistency OK: 1 pending tasks, 0 graduated entries, dep map in sync.\n"
    )
    assert captured.err == (
        "WARNING: .orchestrator/completed_tasks.json not found — skipping dep-map "
        "drift, dep-resolution and graduation-marker checks (registry-dependent).\n"
    )


def test_main_registry_absent_still_runs_registry_independent_checks(crc, box, capsys):
    _write_roadmap(box, "```yaml\nid: T1\nstatus: complete\n```\n")

    rc = crc.main()

    captured = capsys.readouterr()
    assert rc == 1
    assert "(a) task T1 has status:complete" in captured.err
    assert "WARNING" in captured.err
