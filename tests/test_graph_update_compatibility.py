"""`[P11]` / `[INV-09]`, `[INV-A09]`: adopting a version in one project leaves the others alone.

`~/.maestro/current` is one pointer for a whole host. Before this phase, adopting a new
maestro while orchestrating project A repointed it — and project B, mid-roadmap, picked up
the new code at its next `maestro` invocation, silently. The acceptance condition of this
phase is the negative one: **adopting in one project cannot alter another project's active
runtime.** The tests that matter most here are therefore the two-project ones.

Every pointer is a file under `tmp_path`; `MAESTRO_HOME` is redirected for every test, so a
real `~/.maestro/current` is never read and never written.
"""
from __future__ import annotations

import pytest

from maestro import bootstrap, cli, selfupdate, state


def _checkout(root, name: str):
    """A directory shaped like a maestro checkout, as the resolver validates it."""
    target = root / name
    (target / "maestro").mkdir(parents=True)
    (target / "maestro" / "__init__.py").write_text("")
    return target


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    root = tmp_path / "maestro_home"
    root.mkdir()
    monkeypatch.setenv(bootstrap.ENV_VAR, str(root))
    monkeypatch.delenv(bootstrap.PROJECT_ENV_VAR, raising=False)
    return root


@pytest.fixture
def projects(tmp_path):
    """Two independent projects, each with its own `.orchestrator/`."""
    made = {}
    for name in ("alpha", "beta"):
        repo = tmp_path / name
        (repo / ".orchestrator").mkdir(parents=True)
        made[name] = repo
    return made


# ── reading the project root before argparse exists ───────────────────────────────────


@pytest.mark.parametrize("argv, expected", [
    (["status", "--repo", "/srv/alpha"], "/srv/alpha"),
    (["status", "--repo=/srv/alpha"], "/srv/alpha"),
    (["workflow", "--repo", "/srv/alpha", "show", "T-1"], "/srv/alpha"),
])
def test_repo_from_argv_reads_the_flag_the_cli_actually_accepts(argv, expected):
    assert str(bootstrap.repo_from_argv(argv)) == expected


def test_repo_from_argv_falls_back_to_the_environment_then_to_nothing(monkeypatch):
    assert bootstrap.repo_from_argv(["status"]) is None, (
        "no --repo and no $MAESTRO_REPO must mean *no project*, never the cwd: an inferred "
        "pin would redirect a maestro invoked from inside project A while operating B"
    )
    monkeypatch.setenv(bootstrap.PROJECT_ENV_VAR, "/srv/beta")
    assert str(bootstrap.repo_from_argv(["status"])) == "/srv/beta"
    assert str(bootstrap.repo_from_argv(["status", "--repo", "/srv/alpha"])) == "/srv/alpha", (
        "an explicit flag outranks an inherited environment"
    )


def test_the_flag_spelling_is_the_one_the_real_parser_defines():
    """The stdlib pre-parse and argparse must not drift apart."""
    actions = {
        option
        for action in cli.build_parser()._subparsers._group_actions[0].choices["status"]._actions
        for option in action.option_strings
    }
    assert "--repo" in actions


def test_bootstrap_and_paths_agree_on_the_environment_variable():
    from maestro import paths

    assert bootstrap.PROJECT_ENV_VAR == paths.ENV_VAR


# ── the pin wins over the machine-global pointer ──────────────────────────────────────


def test_a_projects_pin_outranks_the_machine_global_pointer(tmp_path, projects):
    pinned = _checkout(tmp_path, "pinned")
    global_ = _checkout(tmp_path, "global")
    bootstrap.current_file().write_text(str(global_))
    bootstrap.project_pin_file(projects["alpha"]).write_text(str(pinned))

    assert bootstrap.adopted_root(repo=projects["alpha"]) == pinned.resolve()
    assert bootstrap.adopted_root(["status", "--repo", str(projects["alpha"])]) == pinned.resolve()
    assert bootstrap.adopted_root(repo=projects["beta"]) == global_.resolve(), (
        "a project that has never pinned still gets the host default"
    )


@pytest.mark.parametrize("content", ["", "   \n", "/nonexistent/checkout"])
def test_an_unusable_pin_falls_back_instead_of_refusing_to_start(tmp_path, projects, content):
    """Fail-open, project pin included: a maestro that will not run is the worse failure."""
    global_ = _checkout(tmp_path, "global")
    bootstrap.current_file().write_text(str(global_))
    bootstrap.project_pin_file(projects["alpha"]).write_text(content)

    assert bootstrap.adopted_root(repo=projects["alpha"]) == global_.resolve()


def test_the_redirect_uses_the_project_named_on_the_command_line(tmp_path, projects, monkeypatch):
    pinned = _checkout(tmp_path, "pinned")
    bootstrap.project_pin_file(projects["alpha"]).write_text(str(pinned))
    monkeypatch.delenv(bootstrap.SENTINEL, raising=False)

    seen = {}

    def spy(executable, argv, env):
        seen["argv"] = argv
        seen["pythonpath"] = env["PYTHONPATH"]
        raise SystemExit(0)

    monkeypatch.setattr(bootstrap.os, "execve", spy)
    with pytest.raises(SystemExit):
        bootstrap.reexec_into_adopted_version(["status", "--repo", str(projects["alpha"])])

    assert seen["argv"][1:3] == ["-m", "maestro.cli"]
    assert seen["pythonpath"].split(":")[0] == str(pinned)


# ── the acceptance condition: one project's adoption cannot move another ──────────────


def test_adopting_in_one_project_leaves_the_other_project_untouched(tmp_path, projects, monkeypatch):
    alpha, beta = projects["alpha"], projects["beta"]
    old = _checkout(tmp_path, "v_old")
    bootstrap.project_pin_file(beta).write_text(str(old))
    bootstrap.current_file().write_text(str(old))
    monkeypatch.setattr(state, "STATE_JSON", alpha / ".orchestrator" / "state.json")
    monkeypatch.setattr(state, "JOURNAL", alpha / ".orchestrator" / "journal.ndjson")
    state.write_state({})
    monkeypatch.setenv(bootstrap.PROJECT_ENV_VAR, str(alpha))

    selfupdate.adopt("deadbeef")

    assert bootstrap.project_pin_file(alpha).read_text().strip().endswith("deadbeef")
    assert bootstrap.project_pin_file(beta).read_text().strip() == str(old), (
        "beta's runtime moved because alpha adopted — the exact drift [INV-09] forbids"
    )
    assert bootstrap.current_file().read_text().strip() == str(old), (
        "a project-scoped adoption must not rewrite the host default either, or every "
        "unpinned project on the machine moves with it"
    )
    assert bootstrap.adopted_root(repo=beta) == old.resolve()


def test_adopting_with_no_project_still_writes_the_machine_default(tmp_path, monkeypatch):
    """The host pointer is not dead: it is what a process with no project resolves."""
    monkeypatch.setattr(state, "STATE_JSON", tmp_path / "state.json")
    monkeypatch.setattr(state, "JOURNAL", tmp_path / "journal.ndjson")
    state.write_state({})

    selfupdate.adopt("cafe1234")

    assert bootstrap.current_file().read_text().strip().endswith("cafe1234")


def test_the_adoption_is_journalled_with_the_pointer_it_wrote(tmp_path, projects, monkeypatch):
    import json

    alpha = projects["alpha"]
    monkeypatch.setattr(state, "STATE_JSON", alpha / ".orchestrator" / "state.json")
    monkeypatch.setattr(state, "JOURNAL", alpha / ".orchestrator" / "journal.ndjson")
    state.write_state({})

    selfupdate.adopt("abc123", project_repo=alpha)

    events = [json.loads(line) for line in state.JOURNAL.read_text().splitlines() if line.strip()]
    adopted = [e for e in events if e["event"] == "self_update_adopted"]
    assert adopted and str(bootstrap.project_pin_file(alpha)) in adopted[-1]["detail"], (
        "which pointer moved is the one fact an operator needs from this journal line"
    )


# ── the operator-visible half ─────────────────────────────────────────────────────────


def test_doctor_reports_against_the_pointer_that_actually_redirected(tmp_path, projects):
    """A check that answered about `~/.maestro/current` while a pin did the redirecting
    would be reporting on a file nothing read."""
    alpha = projects["alpha"]
    bootstrap.project_pin_file(alpha).write_text(str(bootstrap.running_root()))
    bootstrap.current_file().write_text(str(_checkout(tmp_path, "somewhere_else")))

    check = cli._check_adopted_code(alpha)

    assert check.ok, check.detail
    assert str(bootstrap.running_root()) in check.detail


def test_doctor_warns_when_a_pin_lags_the_announced_version(home, projects):
    alpha = projects["alpha"]
    bootstrap.project_pin_file(alpha).write_text(str(home / "versions" / "aaaa1111") + "\n")
    (home / "latest").write_text("bbbb2222\n")

    lagging = cli._check_follows_latest(alpha)
    (home / "latest").write_text("aaaa1111\n")
    following = cli._check_follows_latest(alpha)

    assert not lagging.ok and "bbbb2222" in lagging.detail
    assert following.ok, following.detail


def test_release_command_reports_and_exits_by_verdict(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv(bootstrap.SENTINEL, "1")
    seen = []

    def fake_release(checkout):
        seen.append(checkout)
        return "held_red:abc"
    monkeypatch.setattr(selfupdate, "release", fake_release)

    assert cli.main(["release", "--checkout", str(tmp_path)]) == 1
    assert seen == [tmp_path]
    assert "[release] held_red:abc" in capsys.readouterr().out
