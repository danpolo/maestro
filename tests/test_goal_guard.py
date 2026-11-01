"""GDT-6: the verification gate's goals guard (R22, R23, R28, R41).

`maestro.workflows.goal_guard` runs in the graph's verification gate beside W9's scope
check. Two deterministic parts:

- Anti-tamper (R23, C13): a diff that edits or deletes a file a criterion's `Test:` is
  bound to, or adds a skip or xfail to one (or to a `conftest.py` above it), is a
  blocking finding unless the task names that criterion in `test_corrections:`; a skip
  or xfail is never a correction. A diff touching `docs/GOALS.md` is blocking: a
  contract change is Dan's.
- Regression protection (R22, C12, C18): the criteria GREEN in the current-revision
  baseline, plus the ones the task's `tests:` names, rerun on the task's tree. GREEN that
  is no longer GREEN blocks, whatever the task serves (`serves: []` included); a
  criterion still RED never blocks a partial merge.
"""
from __future__ import annotations

import subprocess

import pytest

from maestro import acceptance
from maestro.docs import goals
from maestro.workflows import goal_guard

from .hang_bound import HANG_SECONDS
from .test_goals_contract import V2

AC02_TEST = "- **Test:** `pytest tests/acceptance/test_import.py::test_refused`\n"
GOALS_MD = V2.replace("- **Layer:** e2e\n- **Evidence:** test\n\n### AC-03",
                      "- **Layer:** e2e\n- **Evidence:** test\n" + AC02_TEST + "\n### AC-03")
PRODUCT = "def accept(line):\n    return True\n"
TESTS = (
    "from app import accept\n\n\n"
    "def test_valid():\n    assert accept('ok')\n\n\n"
    "def test_refused():\n    assert not accept('truncated'), 'refusal is not built yet'\n"
)


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def project(tmp_path):
    return _project(tmp_path, GOALS_MD)


def _project(tmp_path, goals_md, files=None):
    repo = tmp_path / "project"
    (repo / "docs").mkdir(parents=True)
    (repo / "tests" / "acceptance").mkdir(parents=True)
    (repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
    (repo / "docs" / "GOALS.md").write_text(goals_md)
    (repo / "app.py").write_text(PRODUCT)
    (repo / "tests" / "acceptance" / "test_import.py").write_text(TESTS)
    (repo / ".gitignore").write_text(".orchestrator/\n")
    for path, text in (files or {}).items():
        (repo / path).write_text(text)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    doc = goals.load(repo)
    acceptance.store(repo, doc, acceptance.run_baseline(repo, doc, timeout=HANG_SECONDS))
    return repo


def _commit(repo, files=None, deleted=()):
    for path, text in (files or {}).items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    for path in deleted:
        (repo / path).unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "task")


def _policy(repo, task=None, base=None):
    base = base or _git(repo, "rev-parse", "HEAD")
    return goal_guard.policy(repo, task or {"id": "TASK-001", "serves": ["G1"]}, base)


def test_the_baseline_fixture_is_one_green_and_one_red(project):
    states = {r.id: r.state for r in acceptance.load(project)}
    assert states["AC-01"] == acceptance.GREEN and states["AC-02"] == acceptance.RED


# --- the policy and incremental selection (R22, R65) ------------------------------------


def test_the_rerun_is_the_green_criteria_plus_the_ones_the_task_names(project):
    policy = _policy(project)
    assert policy["rerun"] == ["AC-01"]
    assert policy["baseline"]["AC-01"] == acceptance.GREEN
    assert policy["bound_files"] == {"tests/acceptance/test_import.py": ["AC-01", "AC-02"]}
    named = _policy(project, {"id": "TASK-001", "serves": ["G1"], "tests": ["AC-02"]})
    assert named["rerun"] == ["AC-01", "AC-02"]
    by_binding = _policy(project, {"id": "TASK-001", "serves": ["G1"],
                                   "tests": ["tests/acceptance/test_import.py::test_refused"]})
    assert by_binding["rerun"] == ["AC-01", "AC-02"]


def test_a_project_without_goals_md_has_no_guard(tmp_path):
    assert goal_guard.policy(tmp_path, {"id": "t"}, "HEAD") is None


def test_a_stale_baseline_reruns_nothing_and_says_so(project):
    doc = goals.load(project)
    old = [acceptance.Result(id="AC-01", state=acceptance.GREEN, reason="passed", revision=1)]
    acceptance.store(project, doc, old)
    policy = _policy(project)
    assert policy["rerun"] == []
    assert policy["notes"] == ["no acceptance baseline at contract revision 2: no criterion "
                               "is protected until `maestro goals baseline` runs"]


# --- regression protection (R22, R28, R41) ----------------------------------------------


def test_a_green_criterion_turning_red_blocks(project):
    policy = _policy(project)
    _commit(project, {"app.py": "def accept(line):\n    return False\n"})
    findings, observed = goal_guard.regression_findings(project, policy)
    assert observed == {"AC-01": acceptance.RED}
    [finding] = findings
    assert finding.startswith("acceptance_regression: AC-01 (G1) was "
                              "green_already_satisfied at contract revision 2, now "
                              "red_expected_missing_behavior")


@pytest.mark.parametrize("task", [
    {"id": "TASK-001", "serves": []},             # maintenance, no advancement expected
    {"id": "TASK-001", "serves": ["G3"]},         # serves another goal
    {"id": "TASK-001"},                           # names no goal
])
def test_regression_protection_ignores_what_the_task_serves(project, task):
    policy = _policy(project, task)
    _commit(project, {"app.py": "raise ImportError('broken by the task')\n"})
    findings, observed = goal_guard.regression_findings(project, policy)
    assert observed == {"AC-01": acceptance.BLOCKED}
    assert len(findings) == 1 and findings[0].startswith("acceptance_regression: AC-01")


def _environment_runner(kind):
    """Every command fails the same way in this environment, whatever tree it runs on."""
    def run(command, **kwargs):
        if kind == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
        return subprocess.CompletedProcess(command, 2, "", "collection error")
    return run


@pytest.mark.parametrize("kind, state", [("timeout", acceptance.UNKNOWN),
                                         ("collection", acceptance.BLOCKED)])
def test_an_environment_failure_on_the_base_too_is_a_note_not_a_regression(
        project, kind, state):
    """Stage-1 review (01 §6.2: `regressed` is a verified degradation): a GREEN criterion
    that cannot run on the task's tree is rerun on the base in the same environment; when
    the base fails the same way, the environment is at fault, not the task."""
    policy = _policy(project)
    _commit(project, {"README.md": "docs only\n"})
    check = goal_guard.regression_check(project, policy, runner=_environment_runner(kind))
    assert check.findings == []
    assert check.observed == {"AC-01": state}
    [note] = check.notes
    assert note.startswith(f"AC-01 was green_already_satisfied at contract revision 2 but is "
                           f"{state} on the task's tree and on the base in this environment")


def test_a_criterion_the_task_broke_is_compared_with_the_base_and_blocks(project):
    policy = _policy(project)
    _commit(project, {"app.py": "raise ImportError('broken by the task')\n"})
    check = goal_guard.regression_check(project, policy)
    assert check.observed == {"AC-01": acceptance.BLOCKED} and check.notes == []
    [finding] = check.findings
    assert "green on the base in this environment" in finding


def test_a_still_red_criterion_never_blocks_a_partial_merge(project):
    policy = _policy(project, {"id": "TASK-001", "serves": ["G1"], "tests": ["AC-02"]})
    _commit(project, {"README.md": "partial work\n"})
    findings, observed = goal_guard.regression_findings(project, policy)
    assert findings == []
    assert observed == {"AC-01": acceptance.GREEN, "AC-02": acceptance.RED}


def test_a_named_red_criterion_turning_green_is_observed(project):
    policy = _policy(project, {"id": "TASK-001", "serves": ["G1"], "tests": ["AC-02"]})
    _commit(project, {"app.py": "def accept(line):\n    return line != 'truncated'\n"})
    findings, observed = goal_guard.regression_findings(project, policy)
    assert findings == []
    assert observed == {"AC-01": acceptance.GREEN, "AC-02": acceptance.GREEN}


def test_a_criterion_an_earlier_task_turned_green_is_protected(project):
    """GDT-9 (C12, R22): AC-02 is RED at the baseline; an earlier task turned it GREEN and
    the gate observed it. A later task that breaks it again is a regression, though the
    baseline never saw it GREEN; with no stored measurement it has no baseline value."""
    from datetime import datetime, timedelta, timezone

    from maestro import goal_state

    _commit(project, {"app.py": "def accept(line):\n    return line != 'truncated'\n"})
    doc = goals.load(project)
    after = (datetime.now(tz=timezone.utc) + timedelta(minutes=1)).isoformat(timespec="seconds")
    goal_state.record_gate(project, doc, {"acceptance": {"AC-01": acceptance.GREEN,
                                                         "AC-02": acceptance.GREEN},
                                          "snapshot": _git(project, "rev-parse", "HEAD")},
                           task="TASK-000", run_id="run-0", at=after)
    policy = _policy(project, {"id": "TASK-001", "serves": ["G3"]})
    assert policy["rerun"] == ["AC-01", "AC-02"]
    assert policy["baseline"]["AC-02"] == acceptance.GREEN and "AC-02" not in policy["values"]
    assert goal_guard.reruns(project, doc, {"id": "TASK-001", "serves": ["G3"]}) == [
        "AC-01", "AC-02"]
    _commit(project, {"app.py": PRODUCT})
    findings, observed = goal_guard.regression_findings(project, policy)
    assert observed == {"AC-01": acceptance.GREEN, "AC-02": acceptance.RED}
    [finding] = findings
    assert finding.startswith("acceptance_regression: AC-02 (G1) was green_already_satisfied")


def _observed_green(project, cid="AC-02"):
    """An earlier task's gate observed `cid` GREEN (GDT-9), after the baseline ran."""
    from datetime import datetime, timedelta, timezone

    from maestro import goal_state

    after = (datetime.now(tz=timezone.utc) + timedelta(minutes=1)).isoformat(timespec="seconds")
    goal_state.record_gate(project, goals.load(project),
                           {"acceptance": {cid: acceptance.GREEN},
                            "snapshot": _git(project, "rev-parse", "HEAD")},
                           task="TASK-000", run_id="run-0", at=after)


def test_a_criterion_protected_only_by_an_observation_and_red_on_the_base_is_a_note(project):
    """GDT-9 review M4: AC-02 is protected only because an earlier gate observed it GREEN
    (a flaky pass, or a later merge broke it on the target). Its RED is rerun on the base:
    RED there too, the target is already RED, so it is a note, not a rework of this task.
    A baseline-protected criterion keeps GDT-6's RED-blocks-outright rule."""
    _observed_green(project)
    policy = _policy(project, {"id": "TASK-001", "serves": ["G3"]})   # base: AC-02 RED
    assert policy["rerun"] == ["AC-01", "AC-02"]
    _commit(project, {"README.md": "docs only\n"})
    check = goal_guard.regression_check(project, policy)
    assert check.findings == [] and check.questions == []
    assert check.observed == {"AC-01": acceptance.GREEN, "AC-02": acceptance.RED}
    [note] = check.notes
    assert note.startswith("goal_protected_red_on_base: AC-02 ")
    _commit(project, {"app.py": "def accept(line):\n    return False\n"})
    check = goal_guard.regression_check(project, policy)
    [finding] = check.findings                      # AC-01 (baseline GREEN) still blocks
    assert finding.startswith("acceptance_regression: AC-01 ")


def test_an_observed_criterion_green_on_the_base_and_red_on_the_tree_blocks(project):
    """GDT-9 verification L3: the base is compared, and GREEN there means the task's change
    caused the RED: it is a finding, with the why, not a note."""
    _observed_green(project)
    _commit(project, {"app.py": "def accept(line):\n    return line != 'truncated'\n"})
    policy = _policy(project, {"id": "TASK-001", "serves": ["G3"]})     # base: AC-02 GREEN
    _commit(project, {"app.py": PRODUCT})                                # accepts 'truncated'
    check = goal_guard.regression_check(project, policy)
    assert check.notes == []
    [finding] = check.findings
    assert finding.startswith("acceptance_regression: AC-02 ")
    assert "still green on the base in this environment, so the task's change caused it" \
        in finding


def test_an_observed_criterion_red_where_the_base_cannot_be_checked_out_blocks(project):
    """GDT-9 verification L3: no base to compare with is no proof it was already RED."""
    _observed_green(project)
    policy = _policy(project, {"id": "TASK-001", "serves": ["G3"]}, base="0" * 40)
    _commit(project, {"README.md": "docs only\n"})
    check = goal_guard.regression_check(project, policy)
    assert check.notes == []
    [finding] = check.findings
    assert finding.startswith("acceptance_regression: AC-02 ")
    assert "the base could not be checked out to compare" in finding


def test_a_criterion_named_only_in_criteria_is_rerun(project):
    """GDT-9 review M3: a direct task names AC-02 in `criteria:` but its `tests:` names
    another file (or it has none): the gate still reruns AC-02, so its GREEN is observed
    and the doctor's §19.4 false FAIL clears after the task graduates."""
    for tests in ({"tests": ["tests/test_export.py"]}, {}):
        task = {"id": "TASK-001", "serves": ["G1"], "contribution": "direct",
                "criteria": ["AC-02"], **tests}
        assert _policy(project, task)["rerun"] == ["AC-01", "AC-02"]
        assert goal_guard.reruns(project, goals.load(project), task) == ["AC-01", "AC-02"]
    policy = _policy(project, task)
    _commit(project, {"app.py": "def accept(line):\n    return line != 'truncated'\n"})
    findings, observed = goal_guard.regression_findings(project, policy)
    assert findings == []
    assert observed == {"AC-01": acceptance.GREEN, "AC-02": acceptance.GREEN}


# --- anti-tamper (R23) ------------------------------------------------------------------


def test_an_untouched_acceptance_test_is_no_finding(project):
    policy = _policy(project)
    _commit(project, {"app.py": PRODUCT + "\n# tidy\n"})
    assert goal_guard.tamper_findings(project, policy) == []


def test_editing_or_deleting_a_bound_test_is_blocking(project):
    policy = _policy(project)
    _commit(project, {"tests/acceptance/test_import.py": TESTS.replace(
        "assert not accept('truncated')", "assert accept('truncated')")})
    [finding] = goal_guard.tamper_findings(project, policy)
    assert finding.startswith("acceptance_test_changed: tests/acceptance/test_import.py "
                              "binds AC-01, AC-02")
    assert "test_corrections" in finding
    policy = _policy(project)
    _commit(project, deleted=["tests/acceptance/test_import.py"])
    [finding] = goal_guard.tamper_findings(project, policy)
    assert finding.startswith("acceptance_test_deleted: tests/acceptance/test_import.py")


def test_a_named_correction_may_edit_but_never_skip_or_xfail(project):
    task = {"id": "TASK-001", "serves": ["G1"], "test_corrections": ["AC-01", "AC-02"]}
    policy = _policy(project, task)
    _commit(project, {"tests/acceptance/test_import.py": TESTS.replace(
        "'ok'", "'a valid line'")})
    assert goal_guard.tamper_findings(project, policy) == []
    policy = _policy(project, task)
    _commit(project, {"tests/acceptance/test_import.py": "import pytest\n" + TESTS.replace(
        "def test_refused", "@pytest.mark.xfail\ndef test_refused")})
    [finding] = goal_guard.tamper_findings(project, policy)
    assert finding.startswith("acceptance_test_disabled: tests/acceptance/test_import.py "
                              "adds a skip or xfail")


def test_a_base_branch_that_moved_on_counts_only_the_task_s_own_commits(project):
    """GDT-14 review M2b: with no recorded run base the guard diffs against the target
    branch. Once Dan revised the contract and another task's reviewed correction merged
    there, a two-dot diff charged both to this task (`goals_contract_changed`,
    `acceptance_test_changed`, and the old lines as "added"). The change set is taken
    from the fork point; the task's own tamper is still found."""
    _git(project, "checkout", "-q", "-b", "impl/TASK-001")
    policy = _policy(project, base="main")
    _commit(project, {"app.py": PRODUCT + "\n# tidy\n"})
    _git(project, "checkout", "-q", "main")
    _commit(project, {"docs/GOALS.md": GOALS_MD + "\n",
                      "tests/acceptance/test_import.py": "import pytest\n" + TESTS.replace(
                          "'ok'", "'a valid line'")})
    _git(project, "checkout", "-q", "impl/TASK-001")
    assert goal_guard.tamper_findings(project, policy) == []
    _commit(project, {"tests/acceptance/test_import.py": TESTS + "\n# comment\n"})
    [finding] = goal_guard.tamper_findings(project, policy)
    assert finding.startswith("acceptance_test_changed: tests/acceptance/test_import.py")


def test_a_correction_must_name_every_criterion_the_file_binds(project):
    policy = _policy(project, {"id": "TASK-001", "test_corrections": ["AC-02"]})
    _commit(project, {"tests/acceptance/test_import.py": TESTS + "\n# comment\n"})
    [finding] = goal_guard.tamper_findings(project, policy)
    assert finding.startswith("acceptance_test_changed: tests/acceptance/test_import.py "
                              "binds AC-01, AC-02 (not named in test_corrections: AC-01)")


def test_a_skip_added_in_a_conftest_above_a_bound_test_is_blocking(project):
    policy = _policy(project)
    _commit(project, {"tests/conftest.py": (
        "import pytest\n\n\ndef pytest_collection_modifyitems(items):\n"
        "    for item in items:\n        item.add_marker(pytest.mark.skip)\n")})
    [finding] = goal_guard.tamper_findings(project, policy)
    assert finding.startswith("acceptance_test_disabled: tests/conftest.py adds a skip or "
                              "xfail above tests/acceptance/test_import.py")


def test_a_conftest_elsewhere_is_no_finding(project):
    policy = _policy(project)
    _commit(project, {"tools/conftest.py": "import pytest\npytest.skip('x')\n"})
    assert goal_guard.tamper_findings(project, policy) == []


def _rebind(project, old, new):
    text = (project / "docs" / "GOALS.md").read_text().replace(old, new)
    _commit(project, {"docs/GOALS.md": text})


def test_a_directory_binding_protects_the_files_under_it(project):
    """Stage-1 review (R23): `pytest tests/acceptance` binds every file below it."""
    _rebind(project, "`pytest tests/acceptance/test_import.py::test_valid`",
            "`pytest tests/acceptance`")
    policy = _policy(project)
    _commit(project, {"tests/acceptance/test_import.py": TESTS + "\n# edited\n"})
    [finding] = goal_guard.tamper_findings(project, policy)
    assert finding.startswith("acceptance_test_changed: tests/acceptance/test_import.py "
                              "binds AC-01")


def test_a_helper_the_bound_test_imports_and_its_fixture_data_are_protected(project):
    """Stage-1 review (R23): a RED criterion turned GREEN by editing what its test imports
    from beside it, or the data its criterion names, is a changed acceptance test."""
    helpers = "def refusal_line():\n    return 'truncated'\n"
    tests = TESTS.replace("from app import accept\n",
                          "from app import accept\nfrom helpers import refusal_line\n")
    _commit(project, {"tests/acceptance/helpers.py": helpers,
                      "tests/acceptance/test_import.py": tests,
                      "tests/acceptance/data/sample.csv": "a,b\n"})
    policy = _policy(project)
    assert policy["bound_files"]["tests/acceptance/helpers.py"] == ["AC-01", "AC-02"]
    assert policy["bound_files"]["tests/acceptance/data"] == ["AC-01"]
    _commit(project, {"tests/acceptance/helpers.py": helpers.replace("truncated", "x"),
                      "tests/acceptance/data/sample.csv": "a,b\n1,2\n"})
    findings = goal_guard.tamper_findings(project, policy)
    assert [f.split(" binds")[0] for f in findings] == [
        "acceptance_test_changed: tests/acceptance/data/sample.csv",
        "acceptance_test_changed: tests/acceptance/helpers.py"]


def test_a_new_file_beside_the_acceptance_tests_is_no_finding(project):
    policy = _policy(project)
    _commit(project, {"tests/acceptance/test_more.py": "def test_more():\n    pass\n"})
    assert goal_guard.tamper_findings(project, policy) == []


@pytest.mark.parametrize("path, text", [
    ("pytest.ini", "[pytest]\npythonpath = .\naddopts = --deselect "
                   "tests/acceptance/test_import.py::test_valid\n"),
    ("pyproject.toml", "[tool.pytest.ini_options]\naddopts = \"-k 'not test_valid'\"\n"),
    ("setup.cfg", "[tool:pytest]\nnorecursedirs = tests/acceptance\n"),
])
def test_test_selection_added_to_the_pytest_config_is_blocking(project, path, text):
    policy = _policy(project)
    _commit(project, {path: text})
    [finding] = goal_guard.tamper_findings(project, policy)
    assert finding.startswith(f"acceptance_test_disabled: {path} adds test selection")


@pytest.mark.parametrize("path, text", [
    ("tox.ini", "[testenv]\ncommands = python -m pytest {posargs}\n"),
    ("pyproject.toml", "[tool.poetry.scripts]\ntest = \"python -m pytest\"\n"
                       "[tool.pip]\nflags = \"--ignore-installed\"\n"),
    ("examples/demo/pytest.ini", "[pytest]\naddopts = -k smoke\n"),
])
def test_config_outside_the_pytest_options_above_a_bound_test_is_no_finding(
        project, path, text):
    """Fix-diff review M1: only the pytest options of a config at the root or above a bound
    test count; `python -m pytest` elsewhere in it is no test selection."""
    policy = _policy(project)
    _commit(project, {path: text})
    assert goal_guard.tamper_findings(project, policy) == []


def test_a_directory_named_without_a_slash_protects_the_files_under_it(project):
    """Fix-diff review M2: `pytest tests -k valid` binds every file below `tests`."""
    _rebind(project, "`pytest tests/acceptance/test_import.py::test_valid`",
            "`pytest tests -k valid`")
    policy = _policy(project)
    _commit(project, {"tests/acceptance/test_import.py": TESTS + "\n# edited\n"})
    [finding] = goal_guard.tamper_findings(project, policy)
    assert finding.startswith("acceptance_test_changed: tests/acceptance/test_import.py "
                              "binds AC-01")


@pytest.mark.parametrize("path, text", [
    ("pyproject.toml", "[tool.pytest]\naddopts = [\"--deselect\", "
                       "\"tests/acceptance/test_import.py::test_valid\"]\n"),
    ("pytest.toml", "[pytest]\naddopts = [\"-k\", \"not test_valid\"]\n"),
    (".pytest.toml", "[pytest]\ntestpaths = [\"tests/other\"]\n"),
    ("pytest.ini", "[pytest]\naddopts = -knot_valid\n"),
])
def test_test_selection_in_pytest_9_configs_is_blocking(project, path, text):
    """Review of the fix diff M2, L7: pytest 9 reads `[tool.pytest]` and
    `pytest.toml`/`.pytest.toml`; `-k` may be joined to its value."""
    policy = _policy(project)
    _commit(project, {path: text})
    [finding] = goal_guard.tamper_findings(project, policy)
    assert finding.startswith(f"acceptance_test_disabled: {path} adds test selection")


@pytest.mark.parametrize("binding", ["`pytest tests/acceptance/test_import.py -k src`",
                                     "`pytest tests/acceptance/test_import.py -m src`",
                                     "`python -m pytest -p src tests/acceptance`"])
def test_an_option_value_naming_a_directory_binds_nothing(project, binding):
    """Review of the fix diff M3: the value of `-k`/`-m`/`-p` is no path, even when a
    directory has that name."""
    _commit(project, {"src/lib.py": "X = 1\n"})
    _rebind(project, "`pytest tests/acceptance/test_import.py::test_valid`", binding)
    policy = _policy(project)
    assert "src" not in policy["bound_files"]
    _commit(project, {"src/lib.py": "X = 2\n"})
    assert goal_guard.tamper_findings(project, policy) == []


def test_a_directory_argument_of_a_command_other_than_pytest_binds_nothing(project):
    """Review of the fix diff M3: `make tests` names a target, not a test directory."""
    _rebind(project, "`pytest tests/acceptance/test_import.py::test_valid`", "`make tests`")
    assert "tests" not in _policy(project)["bound_files"]


@pytest.mark.parametrize("binding", ["`timeout 300 pytest tests`", "`env CI=1 pytest tests`"])
def test_a_pytest_directory_under_a_wrapper_is_bound(project, binding):
    """Second fix-diff review: a wrapper in front of pytest keeps its directory bound."""
    _rebind(project, "`pytest tests/acceptance/test_import.py::test_valid`", binding)
    assert _policy(project)["bound_files"]["tests"] == ["AC-01"]


def test_a_word_starting_with_m_outside_addopts_is_no_selection(project):
    """Second fix-diff review: only `addopts` is scanned for `-k`/`-m` options."""
    policy = _policy(project)
    _commit(project, {"pytest.ini": "[pytest]\nmarkers =\n    slow: mark slow -marks\n"})
    assert goal_guard.tamper_findings(project, policy) == []


def test_a_policy_without_a_base_reports_the_base_unchecked(project):
    """Review of the fix diff (L5 of the earlier review): no base means no fallback to
    HEAD (the task's own tree, which would excuse the break as the environment's)."""
    policy = {**_policy(project), "base": ""}
    _commit(project, {"app.py": "raise ImportError('broken by the task')\n"})
    check = goal_guard.regression_check(project, policy)
    assert check.notes == []
    [finding] = check.findings
    assert "the base could not be checked out to compare" in finding


def test_a_fixture_path_in_backticks_is_bound(project):
    """Fix-diff review L3."""
    _commit(project, {"tests/acceptance/data/sample.csv": "a,b\n"})
    _rebind(project, "- **Fixtures:** tests/acceptance/data (anonymised samples)",
            "- **Fixtures:** `tests/acceptance/data`, anonymised samples")
    assert _policy(project)["bound_files"]["tests/acceptance/data"] == ["AC-01"]


def test_touching_the_goals_contract_is_blocking_even_with_a_correction(project):
    policy = _policy(project, {"id": "TASK-001", "test_corrections": ["AC-01"]})
    _commit(project, {"docs/GOALS.md": GOALS_MD.replace("under 10 seconds",
                                                        "under 60 seconds")})
    [finding] = goal_guard.tamper_findings(project, policy)
    assert finding.startswith("goals_contract_changed: docs/GOALS.md")
    assert "Dan" in finding


def test_an_unreadable_diff_is_a_finding_never_a_pass(project):
    policy = dict(_policy(project), base="0" * 40)
    [finding] = goal_guard.tamper_findings(project, policy)
    assert finding.startswith("goals_guard_diff_unavailable")


@pytest.mark.parametrize("fails", [
    lambda args: "--diff-filter=A" in args,                  # _added_files
    lambda args: len(args) > 2 and args[-2] == "--",         # _added_lines (per file)
], ids=["added_files", "added_lines"])
def test_a_failed_added_files_or_added_lines_diff_is_a_finding_not_an_empty_result(
        project, monkeypatch, fails):
    """GDT-14 verify (Low): `_added_files` (the `--diff-filter=A` diff) and `_added_lines`
    (the per-file diff) returned empty when git failed, so a skip added to a bound test
    passed unseen. A failed diff is `goals_guard_diff_unavailable`, as for `changed_paths`."""
    task = {"id": "TASK-001", "serves": ["G1"], "test_corrections": ["AC-01", "AC-02"]}
    policy = _policy(project, task)
    _commit(project, {"tests/acceptance/test_import.py": "import pytest\n" + TESTS.replace(
        "def test_refused", "@pytest.mark.xfail\ndef test_refused")})
    real = goal_guard.validation.git

    def flaky(args, *a, **k):
        if args[:1] == ["diff"] and fails(args):
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="fatal: boom")
        return real(args, *a, **k)

    monkeypatch.setattr(goal_guard.validation, "git", flaky)
    [finding] = goal_guard.tamper_findings(project, policy)
    assert finding.startswith("goals_guard_diff_unavailable")


# --- GDT-7: a regression while another goal advanced goes to Dan (R48-R51, R66) ---------

#: AC-04 (G3, metric, below 10 seconds) measured by a project script: 8 at the baseline.
CONFLICT_MD = GOALS_MD.replace(
    "- **Layer:** e2e\n- **Evidence:** metric\n",
    "- **Layer:** e2e\n- **Evidence:** metric\n"
    "- **Test:** `python tests/acceptance/measure.py`\n")
MEASURE = "from pathlib import Path\n\nprint(Path('seconds.txt').read_text().strip())\n"
NAMED = {"id": "TASK-001", "serves": ["G1"], "tests": ["AC-02"]}
#: AC-02 (G1) turns GREEN; AC-04 (G3) goes from 8 to 10.4 seconds: 30% worse.
TRADE = {"app.py": "def accept(line):\n    return line != 'truncated'\n",
         "seconds.txt": "10.4\n"}


@pytest.fixture
def conflicted(tmp_path):
    return _project(tmp_path, CONFLICT_MD, {"tests/acceptance/measure.py": MEASURE,
                                            "seconds.txt": "8\n"})


def _trade(repo, task=NAMED):
    """The policy frozen at the base, then the task's commit; returns (policy, base)."""
    base = _git(repo, "rev-parse", "HEAD")
    policy = _policy(repo, task, base)
    _commit(repo, TRADE)
    return policy, base


def test_the_policy_freezes_the_conflict_context_named_criteria_and_metric_values(
        conflicted):
    from maestro import goal_conflicts

    decision = {"id": "GC-00000000", "resolution": "preserve", "keep": "G3"}
    goal_conflicts.record(conflicted, decision)
    policy = _policy(conflicted, NAMED)
    doc = goals.load(conflicted)
    assert policy["conflicts"] == goal_conflicts.context(doc, "TASK-001", [decision])
    assert policy["named"] == ["AC-02"]
    assert policy["values"] == {"AC-04": 8.0}
    assert policy["rerun"] == ["AC-01", "AC-02", "AC-04"]


def test_a_regression_while_another_goal_advanced_asks_dan_instead_of_blocking(conflicted):
    policy, _ = _trade(conflicted)
    check = goal_guard.regression_check(conflicted, policy)
    assert check.observed == {"AC-01": acceptance.GREEN, "AC-02": acceptance.GREEN,
                              "AC-04": acceptance.RED}
    assert check.findings == [] and check.accepted == []
    [question] = check.questions
    conflict = question["conflict"]
    assert (conflict["advanced"], conflict["regressed"], conflict["criteria"]) == (
        ["G1"], "G3", ["AC-04"])
    assert conflict["stage"] == "review" and conflict["tasks"] == ["TASK-001"]
    assert conflict["progress"] == [["G1", ["AC-02"]]]
    assert [o["label"] for o in question["options"]] == [
        "Keep G3", "Accept G3 worse by up to +30%"]
    assert question["recommended"] == 1          # G1 ranks above G3, the cost is measured
    assert "AC-04: 10.4 vs baseline 8, 30% worse" in question["question"]


def test_an_accepted_conflict_with_the_same_facts_passes_and_is_not_asked_again(conflicted):
    from maestro import goal_conflicts

    policy, base = _trade(conflicted)
    [question] = goal_guard.regression_check(conflicted, policy).questions
    goal_conflicts.record(conflicted, goal_conflicts.decide(
        question, question["options"][1]["label"], "2026-10-18T10:00:00+03:00"))
    again = goal_guard.regression_check(conflicted, _policy(conflicted, NAMED, base))
    assert again.findings == [] and again.questions == []
    [accepted] = again.accepted
    assert accepted["conflict"] == question["conflict"]["id"]
    assert (accepted["resolution"], accepted["bound"]) == ("accept", 30.0)


def test_dan_keeping_the_regressed_goal_blocks_with_his_decision(conflicted):
    from maestro import goal_conflicts

    policy, base = _trade(conflicted)
    [question] = goal_guard.regression_check(conflicted, policy).questions
    goal_conflicts.record(conflicted, goal_conflicts.decide(
        question, "Keep G3", "2026-10-18T10:00:00+03:00"))
    again = goal_guard.regression_check(conflicted, _policy(conflicted, NAMED, base))
    assert again.questions == []
    [finding] = again.findings
    assert finding.startswith(f"Dan decided {question['conflict']['id']} "
                              "(2026-10-18T10:00:00+03:00): keep G3")


def test_a_regression_with_nothing_advanced_keeps_the_gdt6_finding(conflicted):
    policy, _ = _trade(conflicted, {"id": "TASK-001", "serves": ["G1"]})
    check = goal_guard.regression_check(conflicted, policy)
    assert check.questions == []
    [finding] = check.findings
    assert finding.startswith("acceptance_regression: AC-04 (G3) was green_already_satisfied")


def test_a_later_task_is_covered_by_dan_s_accept_within_its_bound(conflicted):
    """GDT-7 review H1: the stored baseline still has AC-04 GREEN after Dan accepted +30%,
    so a later task that advances nothing reruns it RED. His accept covers it; anything
    worse than +30% asks him again."""
    from maestro import goal_conflicts

    policy, base = _trade(conflicted)
    [question] = goal_guard.regression_check(conflicted, policy).questions
    goal_conflicts.record(conflicted, goal_conflicts.decide(
        question, question["options"][1]["label"], "2026-10-18T10:00:00+03:00"))
    later = {"id": "TASK-002", "serves": ["G2"]}
    check = goal_guard.regression_check(conflicted, _policy(conflicted, later, base))
    assert check.findings == [] and check.questions == []
    [accepted] = check.accepted
    assert accepted["conflict"] == question["conflict"]["id"]
    _commit(conflicted, {"seconds.txt": "12\n"})
    worse = goal_guard.regression_check(conflicted, _policy(conflicted, later, base))
    assert worse.findings == []
    [again] = worse.questions
    assert "beyond the +30% Dan approved" in again["question"]
    assert again["conflict"]["id"] == question["conflict"]["id"]


def test_the_gate_s_question_names_the_gate(conflicted):
    policy, _ = _trade(conflicted)
    [question] = goal_guard.regression_check(conflicted, policy).questions
    assert "at the gate (review stage), contract revision" in question["question"]
    assert question["conflict"]["stage"] == "review"


def test_a_hard_invariant_blocks_despite_progress_on_the_same_change(conflicted):
    """R51, C23: AC-01 checks INV-01; AC-02 advancing never trades it."""
    base = _git(conflicted, "rev-parse", "HEAD")
    policy = _policy(conflicted, NAMED, base)
    _commit(conflicted, {"app.py": "def accept(line):\n    return False\n"})
    check = goal_guard.regression_check(conflicted, policy)
    assert check.observed["AC-02"] == acceptance.GREEN
    assert check.questions == []
    assert [f.split(":")[0] for f in check.findings] == [
        "acceptance_regression", "hard_invariant_regression"]
    assert "INV-01" in check.findings[1]


# --- GDT-11: the polish task's guard (R28, R41, C29) -------------------------------------


def _polish(criteria):
    """A polish task as `maestro.backlog` builds it for one kept item of an area whose
    raising task named `criteria` (its `tests:`; `serves: []`)."""
    from maestro import backlog

    item = {"id": "BL-0001", "text": "tidy the refusal message",
            "source": {"task": "TASK-001", "round": 1, "node": "proof_review", "commit": ""},
            "scope": None, "checks": [], "criteria": list(criteria)}
    return backlog.polish_task(1, "a", [item])


def test_a_polish_task_reruns_its_area_s_criteria_and_every_protected_one(project):
    """R28 polish side, C29: a polish task serves no goal, yet the gate reruns every
    protected GREEN criterion (hard-invariant checks included) and the product acceptance
    tests of its area (its `tests:`), and a break of a protected one blocks it."""
    task = _polish(["AC-02"])
    assert (task["serves"], task["tests"]) == ([], ["AC-02"])
    policy = _policy(project, task)
    assert policy["rerun"] == ["AC-01", "AC-02"]
    _commit(project, {"app.py": "def accept(line):\n    return False\n"})
    findings, observed = goal_guard.regression_findings(project, policy)
    assert observed == {"AC-01": acceptance.RED, "AC-02": acceptance.GREEN}
    [finding] = findings
    assert finding.startswith("acceptance_regression: AC-01 (G1) was green_already_satisfied")


def test_a_task_serving_no_goal_advances_none_so_a_regression_it_causes_blocks_not_asks(
        conflicted):
    """R41: a task with `serves: []` advances no goal, even when a criterion its `tests:`
    names turns GREEN, so a regression it causes is never weighed as a trade (a GDT-7
    question to Dan): it blocks as GDT-6's finding. A task serving G1 still asks."""
    polish = _polish(["AC-02"])
    policy, _ = _trade(conflicted, polish)
    assert policy["named"] == [] and "AC-02" in policy["rerun"]
    check = goal_guard.regression_check(conflicted, policy)
    assert check.observed["AC-02"] == acceptance.GREEN
    assert check.questions == [] and check.accepted == []
    [finding] = check.findings
    assert finding.startswith("acceptance_regression: AC-04 (G3) was green_already_satisfied")


def _gate_context(tmp_path, guard):
    from maestro.workflows import handlers
    from maestro.workflows.models import NodeSpec

    node = NodeSpec(node_id="gate", kind="function",
                    handler_ref="maestro.handlers.verification", params={"task_id": "TASK-001"})
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return handlers.HandlerContext(
        run_id="r", task_id="TASK-001", node=node, attempt_id="a",
        inbox=handlers.AttemptInbox(tmp_path / "attempts", "a"), workspace=workspace,
        inputs={"goals_guard": guard}, params={"task_id": "TASK-001"})


def test_the_gate_fails_with_dan_questions_when_the_guard_asks(tmp_path, monkeypatch):
    """The verification handler: an uncovered conflict fails the gate with
    `outputs["dan_questions"]`, which the runner turns into a wait on Dan; blocking
    findings ride along for the rework, and no check evidence is handed back."""
    from types import SimpleNamespace

    from maestro import gates
    from maestro.workflows import handlers

    question = {"question": "GC-1: G1 advanced while G3 regressed", "why_undefined": "w",
                "options": [{"label": "Keep G3", "consequence": "c"},
                            {"label": "Accept G3", "consequence": "c"}],
                "recommended": 0, "conflict": {"id": "GC-1"}}
    monkeypatch.setattr(goal_guard, "tamper_findings", lambda tree, policy: [])
    monkeypatch.setattr(goal_guard, "regression_check", lambda tree, policy: goal_guard.GuardCheck(
        findings=["hard_invariant_regression: x"], observed={"AC-04": acceptance.RED},
        questions=[question]))
    monkeypatch.setattr(gates, "run_authoritative_gate", lambda *a, **k: (SimpleNamespace(
        accepted=True, decision="accepted", reason="ok", integrated_source_snapshot="s"), []))
    result = handlers.verification_handler(_gate_context(tmp_path, {"rerun": ["AC-04"]}))
    assert result.status == "failed"
    assert result.outputs["dan_questions"] == [question]
    assert result.outputs["goals_guard_findings"] == ["hard_invariant_regression: x"]
    assert result.outputs["acceptance"] == {"AC-04": acceptance.RED}
    assert "evidence" not in result.outputs
    assert result.detail.startswith("asked Dan: GC-1")


def test_the_gate_records_accepted_conflicts_and_passes(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from maestro import gates
    from maestro.workflows import handlers

    accepted = {"conflict": "GC-1", "regressed": "G3", "resolution": "accept", "bound": 30.0}
    monkeypatch.setattr(goal_guard, "tamper_findings", lambda tree, policy: [])
    monkeypatch.setattr(goal_guard, "regression_check", lambda tree, policy: goal_guard.GuardCheck(
        observed={"AC-04": acceptance.RED}, accepted=[accepted]))
    monkeypatch.setattr(gates, "run_authoritative_gate", lambda *a, **k: (SimpleNamespace(
        accepted=True, decision="accepted", reason="ok", integrated_source_snapshot="s"), []))
    result = handlers.verification_handler(_gate_context(tmp_path, {"rerun": ["AC-04"]}))
    assert result.status == "succeeded"
    assert result.outputs["goal_conflicts_accepted"] == [accepted]
    assert "dan_questions" not in result.outputs
