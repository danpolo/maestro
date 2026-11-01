"""GDT-3: outer acceptance tests and the setup baseline (R13-R19, R25 part, R67 part).

Every `AC-` is bound to a runnable command or an existing test; the baseline runs each
for real and records one of five states. Only an assertion failure is a credible RED; a
broken harness (import error, missing tool, nothing collected, a failed fixture) is
`blocked_environment`, and anything the runner cannot read is `unknown`. Operator checks
are never an automated pass. The stored baseline carries the contract revision, and a
result from an older revision closes nothing at the new one.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

from maestro import acceptance
from maestro.docs import goals

from .test_goals_contract import V2

ACCEPTANCE_TESTS = textwrap.dedent('''
    import os
    import time

    import pytest

    def test_red():
        assert "summary" == "", "the summary page is not built yet"

    def test_green():
        assert 2 + 2 == 4

    def test_import_error():
        import ledger_adapter_that_is_not_installed  # noqa: F401

    def test_type_error():
        None()

    @pytest.fixture
    def database():
        raise ConnectionRefusedError("database is not running")

    def test_needs_a_service(database):
        pass

    def test_sees_no_secret():
        assert "FAKE_API_KEY" not in os.environ and "GH_TOKEN" not in os.environ
        assert os.environ.get("PATH")

    def test_slow():
        time.sleep(5)
''')


@pytest.fixture
def project(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")   # no parent config leaks in
    (tmp_path / "tests" / "acceptance").mkdir(parents=True)
    (tmp_path / "tests" / "acceptance" / "test_product.py").write_text(ACCEPTANCE_TESTS)
    (tmp_path / "tests" / "acceptance" / "test_import.py").write_text(
        "def test_valid():\n    assert True\n")
    return tmp_path


def _ac(test="", **kw):
    return goals.Criterion(id=kw.pop("id", "AC-01"), name="c", goals=("G1",), test=test, **kw)


def _run(project, test, **kw):
    return acceptance.run_criterion(project, _ac(test), revision=2, **kw)


# --- binding (R16) -------------------------------------------------------------------------


def test_a_criterion_binds_to_a_command_or_to_an_existing_test(project):
    py = acceptance.interpreter(project)
    assert acceptance.command_for(_ac("`pytest tests/acceptance/test_import.py::test_valid`"),
                                  project) == (
        (py, "-m", "pytest", "tests/acceptance/test_import.py::test_valid"), "")
    assert acceptance.command_for(_ac("tests/acceptance/test_import.py::test_valid"),
                                  project) == (
        (py, "-m", "pytest", "tests/acceptance/test_import.py::test_valid"), "")
    assert acceptance.command_for(_ac("`make accept`"), project) == (("make", "accept"), "")
    argv, problem = acceptance.command_for(_ac("`pytest tests; rm -rf build`"), project)
    assert argv == () and "one command, no shell" in problem
    argv, problem = acceptance.command_for(_ac("`cat /etc/passwd`"), project)
    assert argv == () and "outside the project" in problem
    assert acceptance.command_for(_ac(""), project) == ((), "no test bound yet")


@pytest.mark.parametrize("test, why", [
    ('`bash -c "rm -rf build"`', "a shell"),
    ("`sh run.sh`", "a shell"),
    ("""`python -c "print(1)"`""", "inline code"),
    ("`pytest tests/../../other/test_x.py`", "outside the project"),
    ("`python tools/../../../x.py`", "outside the project"),
])
def test_a_binding_runs_no_shell_no_inline_code_and_nothing_outside(project, test, why):
    """Stage-1 review (R19): every path-like argument is resolved, and a shell or inline
    code is refused, not only `;`/`|`."""
    argv, problem = acceptance.command_for(_ac(test), project)
    assert argv == () and why in problem


@pytest.mark.parametrize("test, why", [
    ("`pytest ..`", "outside the project"),
    ("""`python3.12 -c "print(1)"`""", "inline code"),
    ("""`python -Sc "print(1)"`""", "inline code"),
    ('`env bash -c "true"`', "a shell"),
])
def test_a_binding_refuses_the_review_gaps(project, test, why):
    """Fix-diff review L2."""
    argv, problem = acceptance.command_for(_ac(test), project)
    assert argv == () and why in problem


@pytest.mark.parametrize("test", ["`python bench.py -c 10`",
                                  "`python -m pytest -c pytest.ini tests/acceptance`"])
def test_an_option_after_the_script_is_no_inline_code(project, test):
    """Fix-diff review L1: `-c` after the script or `-m` belongs to the program."""
    argv, problem = acceptance.command_for(_ac(test), project)
    assert argv and problem == ""


@pytest.mark.parametrize("test, why", [
    ("""`python -X dev -c "print(1)"`""", "inline code"),
    ("""`python -W ignore -c "print(1)"`""", "inline code"),
    ("""`ruby -I lib -e "puts 1"`""", "inline code"),
    ("""`node -r foo -e "1"`""", "inline code"),
    ("`python -cprint(1)`", "inline code"),
    ("""`python -Sc"print(1)"`""", "inline code"),
    ("`timeout 10s bash -c true`", "a shell"),
    ("`timeout -s KILL 10 bash -c true`", "a shell"),
    ("`env -u FOO bash -c true`", "a shell"),
    ('`node --eval=1`', "inline code"),
    ('`node -C dev -e "1"`', "inline code"),
    ("""`env -S "python -c print(1)"`""", "a shell"),
    ("`env -iS 'bash -c true'`", "a shell"),
    ("`env -S'bash -c true'`", "a shell"),
    ("`env -vS'python -c print(1)'`", "a shell"),
    ("`env -iu FOO bash -c true`", "a shell"),
    ("`timeout -vs KILL 10 bash -c true`", "a shell"),
    # GDT-7 review M4: GNU tools take any unique prefix of a long option.
    ("`env --split 'sh -c x'`", "a shell"),
    ("`env --s 'sh -c x'`", "a shell"),
    ("`env --split-str='sh -c x'`", "a shell"),
    ("`timeout --sig KILL 5 python -c 1`", "inline code"),
    ("`timeout --kill 5 10 python -c 1`", "inline code"),
    ("`env --uns X python -c 1`", "inline code"),
    ("`env --ch /x python -c 1`", "inline code"),
    ("`nice --adj 5 python -c 1`", "inline code"),
])
def test_an_option_value_or_a_wrapper_option_hides_no_inline_code(project, test, why):
    """Review of the fix diff (`75fc51e..7a92dbf`) M1, L5: an interpreter option's value
    is not the script, inline code may be joined to its flag, and a wrapper's own options
    and durations are not the program."""
    argv, problem = acceptance.command_for(_ac(test), project)
    assert argv == () and why in problem


@pytest.mark.parametrize("test", ["`python -mpytest -c pytest.ini tests/acceptance`",
                                  "`python -I tools/check.py -c 3`",
                                  "`python -X dev tools/check.py`",
                                  "`ruby -W:no-deprecated test.rb`",
                                  "`perl -d:Trace t/x.t`"])
def test_a_module_or_script_after_interpreter_options_is_no_inline_code(project, test):
    """Review of the fix diff L4: `-mpytest` names a module; `-I` takes no value."""
    argv, problem = acceptance.command_for(_ac(test), project)
    assert argv and problem == ""


def test_a_real_pytest_command_and_an_existing_test_both_run(project):
    for test in ("`pytest tests/acceptance/test_import.py::test_valid`",
                 "tests/acceptance/test_import.py::test_valid"):
        result = _run(project, test)
        assert (result.state, result.exit_code) == (acceptance.GREEN, 0)
        assert result.command[-1] == "tests/acceptance/test_import.py::test_valid"


def test_a_run_writes_no_bytecode_into_the_project(project):
    """GDT-6: the gate reruns criteria on the task's tree; a `__pycache__/` left there is
    an uncommitted change that refuses the next gate attempt."""
    _run(project, "`pytest tests/acceptance/test_import.py::test_valid`")
    assert not list(project.rglob("__pycache__"))


# --- the five states (R17, C6-C9) and harness vs product (R18) ------------------------------


@pytest.mark.parametrize("name, state, reason", [
    ("test_red", acceptance.RED, "assertion failed: AssertionError: the summary page"),
    ("test_green", acceptance.GREEN, "passed"),
    ("test_import_error", acceptance.BLOCKED,
     "ModuleNotFoundError: No module named 'ledger_adapter_that_is_not_installed'"),
    ("test_needs_a_service", acceptance.BLOCKED, "ConnectionRefusedError: database is not"),
    ("test_type_error", acceptance.UNKNOWN, "TypeError: 'NoneType' object is not callable"),
])
def test_the_baseline_tells_a_missing_behaviour_from_a_broken_harness(project, name, state,
                                                                      reason):
    result = _run(project, f"`pytest tests/acceptance/test_product.py::{name}`")
    assert result.state == state
    assert reason in result.reason
    assert (result.revision, result.goals) == (2, ("G1",))


def test_nothing_collected_a_missing_tool_and_a_broken_file_are_blocked(project):
    (project / "tests" / "acceptance" / "test_broken.py").write_text("import not_there_xyz\n")
    nothing = _run(project, "`pytest tests/acceptance/test_product.py::test_absent`")
    missing = _run(project, "`no-such-acceptance-tool-xyz run`")
    broken = _run(project, "`pytest tests/acceptance/test_broken.py`")
    assert {nothing.state, missing.state, broken.state} == {acceptance.BLOCKED}
    assert "not found" in missing.reason
    assert "exit 4" in nothing.reason or "exit 5" in nothing.reason


def test_no_bound_test_is_unknown_never_a_pass_or_a_red(project):
    result = _run(project, "")
    assert (result.state, result.reason) == (acceptance.UNKNOWN, "no test bound yet")


# --- safety (R19) ---------------------------------------------------------------------------


def test_the_run_sees_no_secret_and_keeps_path(project, monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY", "sk-test")
    monkeypatch.setenv("GH_TOKEN", "ghp-test")
    for name in ("OPENAI_APIKEY", "DATABASE_URL", "SMTP_PASS", "PG_DSN"):
        monkeypatch.setenv(name, "secret")
    env = acceptance.safe_env(project)
    assert "FAKE_API_KEY" not in env and "GH_TOKEN" not in env and env["PATH"]
    assert not {"OPENAI_APIKEY", "DATABASE_URL", "SMTP_PASS", "PG_DSN"} & set(env)
    assert _run(project, "`pytest tests/acceptance/test_product.py::test_sees_no_secret`"
                ).state == acceptance.GREEN


def test_a_project_venv_comes_first_on_path(project):
    (project / ".venv" / "bin").mkdir(parents=True)
    assert acceptance.safe_env(project)["PATH"].startswith(str(project / ".venv" / "bin"))


def test_a_run_past_its_timeout_is_unknown(project):
    result = _run(project, "`pytest tests/acceptance/test_product.py::test_slow`", timeout=1)
    assert result.state == acceptance.UNKNOWN
    assert result.reason == "timed out after 1s"


def test_fixtures_outside_the_project_are_a_safety_problem(project):
    assert acceptance.safety_problems(_ac(fixtures="tests/acceptance/data (samples)"),
                                      project) == []
    assert acceptance.safety_problems(_ac(fixtures="/home/someone/bank-exports"), project) == [
        "AC-01: fixtures /home/someone/bank-exports are outside the project"]
    assert acceptance.safety_problems(_ac(data="../../real-exports"), project) == [
        "AC-01: data ../../real-exports are outside the project"]


# --- operator checks (R14, C9) ---------------------------------------------------------------


def test_an_operator_check_is_recorded_and_never_an_automated_pass(project):
    results = {r.id: r for r in acceptance.run_baseline(project, goals.parse(V2))}
    op = results["OP-01"]
    assert op.state == acceptance.OPERATOR
    assert op.reason == "Dan: open the summary for last month next to the bank statement."
    assert not acceptance.automated_pass(op)
    assert results["AC-01"].state == acceptance.GREEN and acceptance.automated_pass(
        results["AC-01"])
    assert {results[i].state for i in ("AC-02", "AC-03", "AC-04")} == {acceptance.UNKNOWN}
    assert list(results) == ["AC-01", "AC-02", "AC-03", "AC-04", "OP-01"]


# --- metrics (R25 part, C15) -----------------------------------------------------------------


@pytest.mark.parametrize("printed, state", [
    ("median 8.2", acceptance.GREEN), ("12", acceptance.RED), ("fast", acceptance.UNKNOWN)])
def test_a_metric_is_compared_to_its_threshold_or_is_unknown(project, printed, state):
    (project / "bench.py").write_text(f"print({printed!r})\n")
    crit = _ac(f"`python bench.py`", evidence="metric", threshold="10 seconds",
               oracle="median below the threshold")
    result = acceptance.run_criterion(project, crit, revision=2)
    assert result.state == state
    assert result.value == ({"median 8.2": 8.2, "12": 12.0}.get(printed))


@pytest.mark.parametrize("threshold, state", [
    ("at most 10 seconds", acceptance.GREEN), ("no more than 10 seconds", acceptance.GREEN),
    ("under 10 seconds", acceptance.RED), ("at least 10 rows", acceptance.GREEN),
    ("more than 10 rows", acceptance.RED)])
def test_only_an_inclusive_threshold_passes_at_its_own_value(project, threshold, state):
    (project / "bench.py").write_text("print(10)\n")
    crit = _ac("`python bench.py`", evidence="metric", threshold=threshold, oracle="")
    assert acceptance.run_criterion(project, crit, revision=2).state == state


@pytest.mark.parametrize("oracle", [
    "the share is at or above the threshold", "the share is at or below the threshold",
    "the threshold or above", "the threshold or below"])
def test_an_or_equal_oracle_passes_at_the_threshold_itself(project, oracle):
    """GDT-14 (the demo's AC-1, from the parity contract): "at or above the threshold" is
    inclusive, so a share measured exactly at 0.95 meets it; it was read as "above"."""
    (project / "bench.py").write_text("print(0.95)\n")
    crit = _ac("`python bench.py`", evidence="metric", threshold="0.95", oracle=oracle)
    result = acceptance.run_criterion(project, crit, revision=2)
    assert result.state == acceptance.GREEN, result.reason
    assert result.reason.endswith("0.95 or equal")


@pytest.mark.parametrize("oracle, at, inside, outside", [
    # a negated direction is its opposite, inclusive bound (GDT-14 review M3)
    ("the share is not less than the threshold", 0.9, 0.95, 0.5),
    ("the share is no less than the threshold", 0.9, 0.95, 0.5),
    ("the share is not below the threshold", 0.9, 0.95, 0.5),
    ("the share is not under the threshold", 0.9, 0.95, 0.5),
    ("the share is at least the threshold", 0.9, 0.95, 0.5),
    ("the share is not more than the threshold", 0.9, 0.5, 0.95),
    ("the share is no more than the threshold", 0.9, 0.5, 0.95),
    ("the share is not above the threshold", 0.9, 0.5, 0.95),
    ("the share is not over the threshold", 0.9, 0.5, 0.95),
    ("the share is at most the threshold", 0.9, 0.5, 0.95),
    # the "or equal" family is inclusive (GDT-14 review L1)
    ("the share is at or under the threshold", 0.9, 0.5, 0.95),
    ("the share is at or over the threshold", 0.9, 0.95, 0.5),
    ("the threshold or under", 0.9, 0.5, 0.95),
    ("the threshold or over", 0.9, 0.95, 0.5),
    ("the share is above or equal to the threshold", 0.9, 0.95, 0.5),
    ("the share is below or equal to the threshold", 0.9, 0.5, 0.95),
    ("the share is less than or equal to the threshold", 0.9, 0.5, 0.95),
    ("the share is greater than or equal to the threshold", 0.9, 0.95, 0.5)])
def test_a_negated_or_or_equal_oracle_reads_its_real_bound(oracle, at, inside, outside):
    """GDT-14 review M3/L1: "not less than 0.9" was read as "below 0.9", so a measured 0.5
    was a false GREEN; "at or under" / "above or equal to" were strict."""
    crit = _ac("`python bench.py`", evidence="metric", threshold=f"{at:g}", oracle=oracle)
    assert acceptance.classify_metric(crit, f"{at:g}")[0] == acceptance.GREEN, oracle
    assert acceptance.classify_metric(crit, f"{inside:g}")[0] == acceptance.GREEN, oracle
    assert acceptance.classify_metric(crit, f"{outside:g}")[0] == acceptance.RED, oracle


@pytest.mark.parametrize("oracle", [
    "the share must never be above the threshold", "the share should not be less than it",
    "the share isn't below the threshold"])
def test_a_negation_the_reader_cannot_place_is_unknown_not_guessed(oracle):
    """A negation that does not directly govern the direction word is not read past: the
    direction is unknown (ask Dan), never the bare word's (GDT-14 review M3)."""
    crit = _ac("`python bench.py`", evidence="metric", threshold="0.9", oracle=oracle)
    assert acceptance.classify_metric(crit, "0.5")[0] == acceptance.UNKNOWN, oracle


@pytest.mark.parametrize("threshold, printed, state", [
    ("p95 under 200 ms", "p95 latency: 150 ms", acceptance.GREEN),
    ("p95 under 200 ms", "p95 latency: 250 ms", acceptance.RED),
    ("<= 10", "10", acceptance.GREEN), (">= 10", "10", acceptance.GREEN),
    ("< 10", "10", acceptance.RED), ("> 10", "10", acceptance.RED),
    ("under 0.001 s", "2.3e-05 s", acceptance.GREEN),       # fix-diff review L4
    ("under 1 s", ".5 s", acceptance.GREEN)])
def test_a_metric_reads_standalone_numbers_and_the_inclusive_operators(
        project, threshold, printed, state):
    """Stage-1 review: the "95" of "p95" is not the limit or the value, and `<=`/`>=`
    pass at their own value (C15)."""
    (project / "bench.py").write_text(f"print({printed!r})\n")
    crit = _ac("`python bench.py`", evidence="metric", threshold=threshold, oracle="")
    assert acceptance.run_criterion(project, crit, revision=2).state == state


def test_a_threshold_with_no_direction_is_unknown_not_guessed(project):
    (project / "bench.py").write_text("print(8)\n")
    crit = _ac("`python bench.py`", evidence="metric", threshold="10 seconds",
               oracle="the median")
    result = acceptance.run_criterion(project, crit, revision=2)
    assert result.state == acceptance.UNKNOWN
    assert "direction" in result.reason


# --- the stored baseline and its revision (R67 part, R11 part) ------------------------------


def test_the_baseline_is_stored_with_its_revision_and_an_old_one_closes_nothing(project):
    doc = goals.parse(V2)
    results = acceptance.run_baseline(project, doc)
    path = acceptance.store(project, doc, results)
    assert path == project / ".orchestrator" / "acceptance_baseline.json"
    stored = json.loads(path.read_text())
    assert stored["revision"] == 2
    assert stored["results"][0]["id"] == "AC-01" and stored["results"][0]["state"] == "green_already_satisfied"
    loaded = acceptance.load(project)
    assert loaded == results
    assert set(acceptance.current(loaded, doc)) == {"AC-01", "AC-02", "AC-03", "AC-04", "OP-01"}

    newer = goals.parse(V2.replace("Contract revision: 2", "Contract revision: 3"))
    assert acceptance.current(loaded, newer) == {}
    assert [r.id for r in acceptance.stale(loaded, newer)] == [r.id for r in results]


def test_the_baseline_records_the_commit_it_ran_on(project, tmp_path_factory):
    """GDT-10 (R24, C13): the completion gate compares the bound tests against the commit
    the baseline ran on; outside git that commit is `""`, and the sweep is skipped."""
    doc = goals.parse(V2)
    result = [acceptance.Result(id="AC-01", state=acceptance.GREEN, reason="passed",
                                revision=2)]
    stored = json.loads(acceptance.store(project, doc, result).read_text())
    assert stored["commit"] == ""

    repo = tmp_path_factory.mktemp("git-project")
    for args in (["init", "-q"], ["config", "user.email", "t@example.invalid"],
                 ["config", "user.name", "t"], ["commit", "-q", "--allow-empty", "-m", "seed"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()
    stored = json.loads(acceptance.store(repo, doc, result).read_text())
    assert stored["commit"] == head and len(head) == 40
    assert acceptance.load(repo) == result


# --- contract lint (R13, R15) ----------------------------------------------------------------


def test_every_automatable_goal_has_a_criterion_or_operator_check():
    doc = goals.parse(V2.replace("- **Verified by:** AC-04\n", "").replace(
        "- **Goals:** G3\n- **Scenarios:** S-03", "- **Goals:** \n- **Scenarios:** S-03"))
    assert goals.Problem("gate", "G3 has no acceptance criterion or operator check",
                         ("G3",)) in doc.issues


def test_an_oracle_naming_an_internal_path_is_a_draft_problem():
    doc = goals.parse(V2.replace("- **Oracle:** exact equality per category",
                                 "- **Oracle:** the totals table in src/ledger/db.py matches"))
    assert ("AC-03 names an internal detail (src/ledger/db.py): state the observable "
            "behaviour, unless Dan approved that detail in the contract") in [
        p.text for p in goals.diagnose(doc, []) if p.severity == "draft"]
    assert not any("internal detail" in p.text for p in goals.diagnose(goals.parse(V2), []))


# --- `maestro goals baseline` ----------------------------------------------------------------


def test_goals_baseline_cli_stores_the_run_and_fails_while_any_result_is_not_credible(project):
    (project / "docs").mkdir()
    (project / "docs" / "GOALS.md").write_text(V2)
    run = subprocess.run([sys.executable, "-m", "maestro.cli", "goals", "baseline",
                          str(project), "--json"], capture_output=True, text=True, timeout=300)
    assert run.returncode == 1, run.stderr          # AC-02..AC-04 have no test yet
    report = json.loads(run.stdout)
    assert report["revision"] == 2
    assert {r["id"]: r["state"] for r in report["results"]}["AC-01"] == acceptance.GREEN
    assert (project / ".orchestrator" / "acceptance_baseline.json").is_file()

    # Every criterion bound: a credible RED, GREENs and a measured metric pass; the operator
    # check is recorded, not run.
    (project / "bench.py").write_text("print(8.5)\n")
    bound = V2.replace("- **Evidence:** test\n\n### AC-03",
                       "- **Evidence:** test\n- **Test:** `pytest tests/acceptance/test_product.py"
                       "::test_red`\n\n### AC-03").replace(
        "- **Evidence:** test\n\n### AC-04",
        "- **Evidence:** test\n- **Test:** `pytest tests/acceptance/test_product.py"
        "::test_green`\n\n### AC-04").replace(
        "- **Evidence:** metric\n", "- **Evidence:** metric\n- **Test:** `python bench.py`\n")
    (project / "docs" / "GOALS.md").write_text(bound)
    run = subprocess.run([sys.executable, "-m", "maestro.cli", "goals", "baseline",
                          str(project)], capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "AC-02 red_expected_missing_behavior" in run.stdout
    assert "AC-04 green_already_satisfied: measured 8.5" in run.stdout

    # ...but fixtures outside the project fail it, whatever the results.
    (project / "docs" / "GOALS.md").write_text(bound.replace(
        "tests/acceptance/data (anonymised samples)", "/home/someone/bank-exports"))
    run = subprocess.run([sys.executable, "-m", "maestro.cli", "goals", "baseline",
                          str(project)], capture_output=True, text=True, timeout=300)
    assert run.returncode == 1
    assert "[safety] AC-01: fixtures /home/someone/bank-exports are outside the project" in (
        run.stdout)
