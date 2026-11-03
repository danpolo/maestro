"""P13 W1: `~/.maestro/config.yaml` under `project.yaml` (DESIGN.md §17.1)."""
import json

import pytest
import yaml

from maestro import cli, config


@pytest.fixture
def layers(tmp_path, monkeypatch):
    """A repo and a global config file; returns a writer for both."""
    repo = tmp_path / "repo"
    repo.mkdir()
    global_path = tmp_path / "home" / "config.yaml"
    global_path.parent.mkdir()
    monkeypatch.setenv(config.GLOBAL_CONFIG_ENV, str(global_path))
    monkeypatch.setattr(config, "PROJECT_YAML", repo / "project.yaml")

    def write(*, global_doc=None, project_doc=None):
        if global_doc is not None:
            global_path.write_text(yaml.safe_dump(global_doc))
        if project_doc is not None:
            (repo / "project.yaml").write_text(yaml.safe_dump(project_doc))
        return repo

    return write


def test_the_suite_never_reads_the_real_global_config():
    import os
    from pathlib import Path

    path = config.global_config_path()
    assert os.environ.get(config.GLOBAL_CONFIG_ENV)
    assert path != (Path.home() / ".maestro" / "config.yaml").resolve()


def test_mappings_merge_recursively_and_the_higher_layer_wins():
    base = {"thresholds": {"five_h_pause_pct": 92, "concurrency_cap": 3}, "priority": 1}
    over = {"thresholds": {"concurrency_cap": 5}}
    assert config.deep_merge(base, over) == {
        "thresholds": {"five_h_pause_pct": 92, "concurrency_cap": 5}, "priority": 1}
    assert base["thresholds"]["concurrency_cap"] == 3  # inputs are not mutated


def test_a_list_replaces_the_lower_list_whole():
    assert config.deep_merge({"secrets": [".env", "*.pem"]}, {"secrets": ["x"]}) == {"secrets": ["x"]}


def test_null_in_the_higher_layer_is_absent():
    assert config.deep_merge({"priority": 2, "gate": {"chain": ["test"]}},
                             {"priority": None, "gate": None}) == {
        "priority": 2, "gate": {"chain": ["test"]}}


def test_a_scalar_replaces_a_mapping_and_the_reverse():
    assert config.deep_merge({"gate": {"chain": ["test"]}}, {"gate": "off"}) == {"gate": "off"}
    assert config.deep_merge({"gate": "off"}, {"gate": {"chain": ["test"]}}) == {
        "gate": {"chain": ["test"]}}


def test_effective_config_names_the_layer_of_every_leaf(layers):
    repo = layers(global_doc={"thresholds": {"five_h_pause_pct": 92, "concurrency_cap": 3},
                              "priority": 1},
                  project_doc={"project": {"name": "demo"}, "thresholds": {"concurrency_cap": 5}})
    merged, sources = config.effective_config(repo)
    assert merged == {"project": {"name": "demo"}, "priority": 1,
                      "thresholds": {"five_h_pause_pct": 92, "concurrency_cap": 5}}
    assert sources == {"project.name": "project", "priority": "global",
                       "thresholds.five_h_pause_pct": "global",
                       "thresholds.concurrency_cap": "project"}


def test_a_mapping_whose_values_are_all_null_is_one_empty_leaf(layers):
    # The shipped template's `gate.bands: {claim: null, flat: null, block: null}`.
    repo = layers(project_doc={"gate": {"chain": ["test"], "bands": {"claim": None}}})
    merged, sources = config.effective_config(repo)
    assert merged == {"gate": {"chain": ["test"], "bands": {}}}
    assert sources == {"gate.chain": "project", "gate.bands": "project"}


def test_a_project_value_equal_to_the_global_one_is_still_sourced_from_the_project(layers):
    repo = layers(global_doc={"priority": 1}, project_doc={"priority": 1})
    assert config.effective_config(repo)[1] == {"priority": "project"}


def test_the_loader_returns_the_merged_document(layers):
    layers(global_doc={"thresholds": {"five_h_pause_pct": 80}},
           project_doc={"thresholds": {"concurrency_cap": 5}})
    assert config.load_project_yaml() == {
        "thresholds": {"five_h_pause_pct": 80, "concurrency_cap": 5}}
    assert config.threshold("five_h_pause_pct", 92, cast=int) == 80


def test_the_project_layer_wins_at_the_loader(layers):
    # The loader is what every runtime reader calls, so the precedence is asserted there.
    from maestro import model_policy

    layers(global_doc={"thresholds": {"five_h_pause_pct": 80},
                       "routing": {"near_cost_margin": 0.1}},
           project_doc={"thresholds": {"five_h_pause_pct": 70},
                        "routing": {"near_cost_margin": 0.3}})
    assert config.load_project_yaml()["thresholds"] == {"five_h_pause_pct": 70}
    assert config.threshold("five_h_pause_pct", 92, cast=int) == 70
    assert model_policy.near_cost_margin(config.load_project_yaml()) == 0.3


def test_a_home_global_config_does_not_reach_import_time_constants(tmp_path):
    """`quota`, `switch` and `watchdog` read thresholds when they are imported, which is
    during collection: the guard has to be set before any `maestro` module loads."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({"thresholds": {"five_h_pause_pct": 55}}))
    env = {k: v for k, v in os.environ.items() if k != config.GLOBAL_CONFIG_ENV}
    env["MAESTRO_HOME"] = str(home)
    tests_dir = Path(__file__).resolve().parent
    script = ("import sys; sys.path[:0] = sys.argv[1:3]\n"
              "import conftest\n"
              "from maestro import quota\n"
              "print(quota.PAUSE_PCT)")
    done = subprocess.run([sys.executable, "-c", script, str(tests_dir), str(tests_dir.parent)],
                          env=env, capture_output=True, text=True, cwd=str(tmp_path))
    assert done.returncode == 0, done.stderr
    assert float(done.stdout.strip()) != 55.0


def test_without_a_global_file_the_loader_returns_the_project_document_untouched(layers):
    layers(project_doc={"thresholds": None, "priority": 3})
    assert config.load_project_yaml() == {"thresholds": None, "priority": 3}


@pytest.mark.parametrize("text", ["thresholds: [unclosed\n", "- not\n- a mapping\n"])
def test_an_unusable_global_file_stops_every_merge_because_its_safety_lists_are_unknown(
        layers, tmp_path, text):
    from maestro import merge
    layers(project_doc={"priority": 3})
    (tmp_path / "home" / "config.yaml").write_text(text)
    reason = merge.integration_guard(tmp_path, "HEAD", "HEAD", proj={})
    assert reason.startswith("deny_list: ") and "global config" in reason


@pytest.mark.parametrize("text", [None, "", "# only a comment\n", "priority: 2\n"])
def test_a_usable_or_absent_global_file_has_no_problem(layers, tmp_path, text):
    layers(project_doc={"priority": 3})
    if text is not None:
        (tmp_path / "home" / "config.yaml").write_text(text)
    assert config.global_config_problem() is None


def test_an_unusable_global_file_is_no_layer(layers, tmp_path):
    repo = layers(project_doc={"priority": 3})
    (tmp_path / "home" / "config.yaml").write_text("- not\n- a mapping\n")
    assert config.load_global_config() == {}
    assert config.effective_config(repo)[0] == {"priority": 3}
    # Doctor is the only place that says the global defaults were dropped.
    check = cli._check_config_keys(repo)
    assert not check.ok and check.required and "is not a mapping" in check.detail
    (tmp_path / "home" / "config.yaml").write_text("thresholds: [unclosed\n")
    assert config.load_global_config() == {}
    assert config.load_project_yaml() == {"priority": 3}
    check = cli._check_config_keys(repo)
    assert not check.ok and check.required and "is unreadable" in check.detail


def test_unknown_top_level_keys_are_named():
    assert config.unknown_keys({"thresholds": {}, "budget": {}, "tresholds": {}, "x": 1}) == [
        "tresholds", "x"]
    assert config.unknown_keys(None) == []


def test_doctor_fails_on_an_unknown_key_in_either_file(layers):
    repo = layers(global_doc={"priority": 1}, project_doc={"project": {"name": "demo"}})
    assert cli._check_config_keys(repo).ok
    layers(global_doc={"priorty": 1})
    check = cli._check_config_keys(repo)
    assert not check.ok and check.required and "priorty" in check.detail
    layers(global_doc={"priority": 1}, project_doc={"tresholds": {}})
    check = cli._check_config_keys(repo)
    assert not check.ok and "tresholds" in check.detail and "project.yaml" in check.detail


def test_doctor_fails_on_a_retired_role_key_in_either_file(layers):
    # DESIGN.md §17.2: nothing reads these any more, so a file that states one is refused.
    repo = layers(global_doc={"roles": {"reviewer": {"strength": "strong"}}},
                  project_doc={"roles": {"implementer": {"strength": "balanced",
                                                         "independent_of": ["reviewer"]}}})
    check = cli._check_role_shape(repo)
    assert check.ok and check.required
    layers(project_doc={"roles": {"implementer": {"strength": "balanced", "effort": "high"}}})
    check = cli._check_role_shape(repo)
    assert not check.ok and check.required
    assert "project.yaml: roles.implementer.effort is retired" in check.detail
    layers(project_doc={"roles": {"judge": {"backend": "claude", "models": {"claude": "x"}}},
                        "fallback_chain": ["claude", "codex"]})
    detail = cli._check_role_shape(repo).detail
    for retired in ("roles.judge.backend", "roles.judge.models", "fallback_chain is retired"):
        assert retired in detail
    layers(global_doc={"roles": {"planner": {"strength": "huge", "model": "x"}}}, project_doc={})
    detail = cli._check_role_shape(repo).detail
    assert "config.yaml: roles.planner.model is retired" in detail
    assert "roles.planner.strength 'huge'" in detail and "project.yaml" not in detail


def _doctor(repo, monkeypatch, capsys, keep):
    """`maestro doctor` with every check but `keep` stubbed green: its exit code and output."""
    ok = cli.Check("stub", True, "stub")
    with monkeypatch.context() as patch:
        for name in [n for n in vars(cli) if n.startswith("_check_") and n != keep]:
            patch.setattr(cli, name, (lambda *a, **k: [ok]) if name == "_check_adapters"
                          else (lambda *a, **k: ok))
        code = cli.cmd_doctor(repo, http_get=lambda url: pytest.fail(url))
    return code, capsys.readouterr().out


def test_maestro_doctor_runs_the_key_check_and_the_role_shape_check(layers, monkeypatch, capsys):
    # P13 W1 proof: a role with `effort` fails `doctor`, and so does an unknown key.
    repo = layers(project_doc={"project": {"name": "demo"}})
    assert _doctor(repo, monkeypatch, capsys, keep=None)[0] == 0
    layers(project_doc={"project": {"name": "demo"}, "tresholds": {}})
    code, out = _doctor(repo, monkeypatch, capsys, keep="_check_config_keys")
    assert code == 1 and "[FAIL] config_keys: project.yaml: unknown keys" in out
    layers(project_doc={"project": {"name": "demo"}, "roles": {"implementer": {"effort": "high"}}})
    code, out = _doctor(repo, monkeypatch, capsys, keep="_check_role_shape")
    assert code == 1
    assert "[FAIL] role_shape: project.yaml: roles.implementer.effort is retired" in out


def test_role_shape_names_every_malformed_part_of_a_roles_block():
    from maestro.model_policy import role_shape_issues

    assert role_shape_issues({"roles": ["implementer"]}) == ["roles is not a mapping"]
    issues = role_shape_issues({"roles": {
        "implementer": 5,
        "reviewer": {"strength": "strong", "independent_of": "implementer", "floor": 1}}})
    assert issues == [
        "roles.implementer: must be a mapping {strength, independent_of}",
        "roles.reviewer.floor: unknown role key",
        "roles.reviewer.independent_of must be a list of role names",
    ]


def test_role_shape_refuses_the_retired_harness_order():
    from maestro.model_policy import role_shape_issues

    for retired in (["codex", "claude"], "claude", ["claude", "cluade", 7]):
        assert role_shape_issues({"routing": {"harness_order": retired}}) == [
            "routing.harness_order is retired: selection is global (DESIGN §17.5)"]
    assert role_shape_issues({"routing": ["claude"]}) == ["routing is not a mapping"]


def test_doctor_retention_reads_a_limit_set_only_in_the_global_file(layers):
    repo = layers(global_doc={"retention": {"warn_bytes": 1}}, project_doc={"priority": 1})
    (repo / ".orchestrator").mkdir()
    (repo / ".orchestrator" / "events.jsonl").write_text("{}\n")
    check = cli._check_retention(repo)
    assert not check.ok and "retention.warn_bytes=1;" in check.detail
    layers(project_doc={"retention": {"warn_bytes": 1000}})  # the project layer still wins
    assert cli._check_retention(repo).ok


def test_the_preamble_deny_list_reads_entries_set_only_in_the_global_file(layers):
    repo = layers(global_doc={"risky_set": ["maestro/secrets.py"]}, project_doc={"priority": 1})
    assert cli._deny_list_block(repo) == "maestro/secrets.py"
    layers(project_doc={"deny_list_extra": ["deploy/"]})
    assert cli._deny_list_block(repo) == "maestro/secrets.py\ndeploy/"


def test_config_show_effective_prints_every_key_with_its_source(layers, capsys):
    repo = layers(global_doc={"priority": 1, "thresholds": {"concurrency_cap": 3}},
                  project_doc={"thresholds": {"concurrency_cap": 5}})
    assert cli.cmd_config(repo, "show", effective=True) == 0
    assert capsys.readouterr().out.splitlines() == [
        "priority = 1  [global]",
        "thresholds.concurrency_cap = 5  [project]",
    ]
    assert cli.cmd_config(repo, "show", effective=True, as_json=True) == 0
    assert json.loads(capsys.readouterr().out) == {
        "priority": {"value": 1, "source": "global"},
        "thresholds.concurrency_cap": {"value": 5, "source": "project"},
    }
