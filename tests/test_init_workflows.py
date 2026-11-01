"""Safe repository fact discovery and policy scaffolding in `maestro init` (`[P5]`, `D03`).

The negative invariant is the reason this module exists: onboarding a repository is the
moment Maestro knows least about what that repository's scripts do, so discovery reads
manifests and executes nothing. An autouse fixture makes every spelling of "run
something" raise for the whole module, so a detector that shelled out would fail the
suite rather than quietly work on the developer's machine.

The rest is `init`'s non-destructive contract, extended to the two new artefacts:
`workflows/project.yaml` (the engineering policy) and the recorded project profile. An
edited policy is never overwritten — it gets a `.new` and a diff — and the control flags
the loop check borrows are handed back.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from maestro import cli
from maestro import state as state_mod
from maestro.workflows import discovery
from maestro.workflows.policy import POLICY_RELPATH, PolicyError, ProjectPolicy


@pytest.fixture(autouse=True)
def _nothing_executes(monkeypatch):
    """Discovery may never execute anything. Every route out of the process raises."""

    def forbidden(*args, **kwargs):
        raise AssertionError(f"discovery executed something: {args!r}")

    for attribute in ("run", "Popen", "call", "check_call", "check_output", "getoutput"):
        if hasattr(subprocess, attribute):
            monkeypatch.setattr(subprocess, attribute, forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    if hasattr(os, "popen"):
        monkeypatch.setattr(os, "popen", forbidden)


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


def write(root: Path, relpath: str, content: str) -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ── the negative invariant ───────────────────────────────────────────────────────────


def test_discovery_of_a_repository_full_of_scripts_executes_nothing(repo):
    write(repo, "package.json", json.dumps({"scripts": {"test": "rm -rf /", "build": "x"}}))
    write(repo, "Makefile", "test:\n\techo destroy\n")
    write(repo, ".github/workflows/ci.yml", yaml.safe_dump(
        {"jobs": {"t": {"steps": [{"run": "npm test"}]}}}
    ))
    # The autouse fixture is the assertion: any subprocess would raise.
    profile = discovery.discover(repo)
    assert profile.facts or profile.unresolved


def test_a_makefile_recipe_is_never_read_only_the_target_name(repo):
    write(repo, "Makefile", "test:\n\tpython -c 'import shutil; shutil.rmtree(\"/\")'\n")
    fact = discovery.discover(repo).fact("test_command")
    assert fact.value == ["make", "test"]


def test_discovery_of_a_missing_directory_is_an_error_not_a_guess(tmp_path):
    with pytest.raises(discovery.DiscoveryError):
        discovery.discover(tmp_path / "nope")


# ── facts carry their evidence ───────────────────────────────────────────────────────


def test_every_fact_records_source_path_hash_and_confidence(repo):
    manifest = write(repo, "package.json", json.dumps({"scripts": {"test": "jest"}}))
    profile = discovery.discover(repo)
    fact = profile.fact("test_command")
    assert fact.value == ["npm", "test"]
    assert fact.source_path == "package.json"
    assert fact.confidence == "high"
    import hashlib
    assert fact.source_sha256 == hashlib.sha256(manifest.read_bytes()).hexdigest()


def test_a_declared_test_command_outranks_a_conventional_one(repo):
    write(repo, "package.json", json.dumps({"scripts": {"test": "jest"}}))
    (repo / "tests").mkdir()
    write(repo, "pyproject.toml", "[project]\nname = 'x'\n")
    assert discovery.discover(repo).fact("test_command").confidence == "high"


def test_a_conventional_fact_is_marked_low_confidence(repo):
    write(repo, "pyproject.toml", "[project]\nname = 'x'\n")
    (repo / "tests").mkdir()
    fact = discovery.discover(repo).fact("test_command")
    assert fact.confidence == "low"
    assert fact.detector == "python_tests_directory"


def test_ci_evidence_is_weaker_than_a_declared_manifest_entry(repo):
    write(repo, "package.json", json.dumps({"scripts": {"test": "jest"}}))
    write(repo, ".github/workflows/ci.yml", yaml.safe_dump(
        {"jobs": {"t": {"steps": [{"run": "go test ./..."}]}}}
    ))
    fact = discovery.discover(repo).fact("test_command")
    assert fact.value == ["npm", "test"]
    assert fact.source_path == "package.json"


def test_the_profile_hash_is_independent_of_where_the_repo_is_checked_out(tmp_path):
    manifest = json.dumps({"scripts": {"test": "jest"}})
    first, second = tmp_path / "a", tmp_path / "b"
    for root in (first, second):
        write(root, "package.json", manifest)
    assert discovery.discover(first).profile_hash == discovery.discover(second).profile_hash


def test_the_profile_hash_changes_when_a_manifest_changes(repo):
    write(repo, "package.json", json.dumps({"scripts": {"test": "jest"}}))
    before = discovery.discover(repo).profile_hash
    write(repo, "package.json", json.dumps({"scripts": {"test": "vitest"}}))
    assert discovery.discover(repo).profile_hash != before


def test_a_generated_manifest_too_large_to_be_evidence_is_skipped(repo):
    write(repo, "package.json", " " * (discovery.MAX_MANIFEST_BYTES + 1))
    assert discovery.discover(repo).fact("package_manager") is None


# ── unresolved facts fail closed ─────────────────────────────────────────────────────


def test_two_equally_authoritative_test_commands_are_unresolved_not_guessed(repo):
    write(repo, "package.json", json.dumps({"scripts": {"test": "jest"}}))
    write(repo, "Makefile", "test:\n\t./run-tests.sh\n")
    profile = discovery.discover(repo)
    assert profile.fact("test_command") is None
    unresolved = next(u for u in profile.unresolved if u.key == "test_command")
    assert set(unresolved.candidates) == {"npm test", "make test"}
    assert set(unresolved.sources) == {"package.json", "Makefile"}


def test_a_key_with_no_evidence_is_reported_rather_than_silently_absent(repo):
    write(repo, "pyproject.toml", "[project]\nname = 'x'\n")
    profile = discovery.discover(repo)
    reported = {f.key for f in profile.facts} | set(profile.unresolved_keys)
    assert set(discovery.EXPECTED_KEYS) <= reported


def test_is_resolved_is_false_for_an_ambiguous_key(repo):
    write(repo, "package.json", json.dumps({"scripts": {"test": "jest"}}))
    write(repo, "Makefile", "test:\n\t./x.sh\n")
    assert discovery.discover(repo).is_resolved("test_command") is False


def test_an_empty_repository_resolves_nothing_and_says_so(repo):
    profile = discovery.discover(repo)
    assert profile.facts == ()
    assert set(profile.unresolved_keys) == set(discovery.EXPECTED_KEYS)


# ── `init` records the profile ───────────────────────────────────────────────────────


def test_init_records_the_derived_profile_under_the_orchestrator_directory(repo, capsys):
    write(repo, "package.json", json.dumps({"scripts": {"test": "jest", "lint": "eslint ."}}))
    profile = cli._discover_project_profile(repo)
    recorded = json.loads((repo / ".orchestrator" / "project_profile.json").read_text())
    assert recorded["profile_hash"] == profile.profile_hash
    assert {f["key"] for f in recorded["facts"]} >= {"test_command", "lint_command"}
    assert "UNRESOLVED" in capsys.readouterr().out


# ── the engineering policy is scaffolded and verified ────────────────────────────────


def test_the_scaffolded_policy_is_the_packaged_one_and_it_loads(repo):
    cli._scaffold_file(
        repo / POLICY_RELPATH,
        (cli.TEMPLATES_DIR / "workflows" / "project.yaml").read_text(encoding="utf-8"),
    )
    assert cli._verify_engineering_policy(repo) is None
    assert set(ProjectPolicy.load(repo).templates) == {
        "ordinary", "script", "manual", "async", "resumable"
    }


def test_init_refuses_rather_than_proceeding_on_a_broken_policy(repo):
    write(repo, str(POLICY_RELPATH), "objective: cheap_and_fast\ntemplates: {}\n")
    detail = cli._verify_engineering_policy(repo)
    assert detail is not None and "REFUSED" in detail


def test_a_broken_policy_never_degrades_into_the_default(repo):
    write(repo, str(POLICY_RELPATH), "templates: 3\n")
    with pytest.raises(PolicyError):
        ProjectPolicy.load(repo)


# ── re-running `init` is non-destructive ─────────────────────────────────────────────


def test_an_edited_policy_is_never_overwritten_only_diffed(repo):
    content = (cli.TEMPLATES_DIR / "workflows" / "project.yaml").read_text(encoding="utf-8")
    destination = repo / POLICY_RELPATH
    cli._scaffold_file(destination, content)
    edited = content.replace("objective: paid_efficiency", "objective: free_quality")
    destination.write_text(edited, encoding="utf-8")

    outcome = cli._scaffold_file(destination, content)
    assert outcome.action == "new_written"
    assert destination.read_text(encoding="utf-8") == edited
    assert (repo / "workflows" / "project.yaml.new").is_file()
    assert "objective" in outcome.diff


def test_an_unchanged_policy_re_scaffolds_to_nothing_at_all(repo):
    content = (cli.TEMPLATES_DIR / "workflows" / "project.yaml").read_text(encoding="utf-8")
    cli._scaffold_file(repo / POLICY_RELPATH, content)
    assert cli._scaffold_file(repo / POLICY_RELPATH, content).action == "unchanged"
    assert not (repo / "workflows" / "project.yaml.new").exists()


def _point_state_at(repo: Path, monkeypatch) -> Path:
    """`maestro.state` binds its path at import time, so in-process tests rebind it.

    A real `init` is a fresh process and picks the path up from `MAESTRO_REPO`; the env
    var is set here too so nothing reads a different repository than it writes.
    """
    monkeypatch.setenv("MAESTRO_REPO", str(repo))
    state_path = repo / ".orchestrator" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(state_mod, "STATE_JSON", state_path)
    return state_path


def test_re_running_the_loop_check_leaves_active_control_flags_untouched(repo, monkeypatch):
    # The reset is the loop check's own fixture, not a decision about this project: a
    # project the operator paused is still paused once `init` has finished.
    state_path = _point_state_at(repo, monkeypatch)
    state_path.write_text(
        json.dumps({"paused_by_user": True, "halted": True, "proposal_test_notified": True}),
        encoding="utf-8",
    )

    borrowed = cli._reset_control_flags_for_loop_check(repo)
    during = json.loads(state_path.read_text())
    assert not any(during[flag] for flag in ("paused_by_user", "halted", "proposal_test_notified"))

    cli._restore_control_flags(repo, borrowed)
    after = json.loads(state_path.read_text())
    assert after["paused_by_user"] is True
    assert after["halted"] is True
    assert after["proposal_test_notified"] is True


def test_a_project_with_no_active_flags_borrows_nothing(repo, monkeypatch):
    state_path = _point_state_at(repo, monkeypatch)
    state_path.write_text(json.dumps({"phase": "idle"}), encoding="utf-8")
    assert cli._reset_control_flags_for_loop_check(repo) == {}
    cli._restore_control_flags(repo, {})
    # Nothing was borrowed, so nothing was written back — the file is untouched.
    assert json.loads(state_path.read_text()) == {"phase": "idle"}


def test_the_loop_check_does_not_leave_a_fresh_project_paused(repo, monkeypatch):
    # G4: the check's idle branch sets paused_by_user/proposal_test_notified itself, and
    # nothing was borrowed to put back, so a freshly onboarded project started paused.
    state_path = _point_state_at(repo, monkeypatch)
    state_path.write_text(json.dumps({"phase": "idle"}), encoding="utf-8")
    borrowed = cli._reset_control_flags_for_loop_check(repo)
    state_path.write_text(
        json.dumps({"phase": "idle", "paused_by_user": True, "proposal_test_notified": True}),
        encoding="utf-8",
    )
    cli._restore_control_flags(repo, borrowed)
    after = json.loads(state_path.read_text())
    assert after["paused_by_user"] is False
    assert after["proposal_test_notified"] is False
