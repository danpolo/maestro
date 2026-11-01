"""Legacy calls deliver the same approved model/effort pairs as the graph router."""
from pathlib import Path

import pytest

from maestro import agentcall, config, implementer, model_policy, roles
from maestro.backends import registry
from maestro.backends.base import Completion, LaunchSpec
from tests.test_model_task_pins import project  # noqa: F401
from tests.backends.test_claude_driver import _agent_argv
from tests.backends.test_codex_driver import _job
from tests.test_codex_app_server import server
from tests.hang_bound import HANG_SECONDS


@pytest.fixture(autouse=True)
def isolated_codex_login(tmp_path, monkeypatch):
    from maestro.backends import codex_runtime
    real_prepare = codex_runtime.prepare

    def prepare(**kwargs):
        kwargs.setdefault('host_home', tmp_path)
        return real_prepare(**kwargs)

    monkeypatch.setattr(codex_runtime, 'prepare', prepare)
    native = tmp_path / 'native'
    native.mkdir()
    (native / 'auth.json').write_text('synthetic')
    monkeypatch.setenv('CODEX_HOME', str(native))


@pytest.mark.parametrize("role,driver,model,effort", [
    ("planner", "claude", "claude-opus-5-5", "high"),
    ("planner", "codex", "gpt-6.1-sol", "high"),
    ("implementer", "claude", "claude-sonnet-5-5", "medium"),
    ("implementer", "codex", "gpt-6.1-sol", "low"),
])
def test_derived_role_selects_an_eligible_pair(role, driver, model, effort):
    entry = model_policy.derived_roles({}, None)["roles"][role]
    assert entry["models"][driver] == model
    assert entry["efforts"][driver] == effort
    assert "antigravity" not in entry["models"]


@pytest.mark.parametrize("driver,effort", [("claude", "high"), ("codex", "high")])
def test_legacy_planner_completion_argv_delivers_effort(driver, effort, monkeypatch):
    monkeypatch.setattr(config, "load_project_yaml", lambda: {})
    monkeypatch.setattr(model_policy, "adopted_policy", lambda repo: (None, None))
    monkeypatch.setattr(agentcall, "operator_backend", lambda: driver)
    seen = []
    cls = registry.driver_class(driver)
    from maestro.backends.claude import completion_argv as claude_argv
    from maestro.backends.codex import completion_argv as codex_argv

    def complete(self, spec):
        seen.append(claude_argv(spec) if driver == "claude" else codex_argv(
            "codex", spec, last_message_file=Path("answer.txt")))
        return Completion("ok")

    monkeypatch.setattr(cls, "complete", complete)
    assert agentcall.ask("planner", "plan").text == "ok"
    argv = seen[0]
    if driver == "claude":
        assert argv[argv.index("--effort") + 1] == effort
    else:
        assert f"model_reasoning_effort={effort}" in argv


@pytest.mark.parametrize("driver,effort", [("claude", "medium"), ("codex", "low")])
def test_legacy_implementer_generated_launcher_delivers_effort(driver, effort, tmp_path, monkeypatch):
    (tmp_path / 'wt').mkdir()
    monkeypatch.setattr(config, "load_project_yaml", lambda: {})
    monkeypatch.setattr(config, "PROJECT_YAML", tmp_path / "project.yaml")
    monkeypatch.setattr(model_policy, "adopted_policy", lambda repo: (None, None))
    monkeypatch.setattr(implementer, "operator_backend", lambda: driver)
    monkeypatch.setattr(implementer, "_make_brief", lambda *a: "implement")
    monkeypatch.setattr(implementer, "append_journal", lambda *a: None)
    cls = registry.launch_driver_class(driver)
    backend = cls()
    if driver == "claude":
        monkeypatch.setattr(backend, "mode", lambda: "headless")
        monkeypatch.setattr(backend, "_open_window", lambda *a: None)
    else:
        monkeypatch.setattr(backend, "binary_path", lambda: "codex")
        monkeypatch.setattr(backend, "binary_warning", lambda: None)
        monkeypatch.setattr(backend, "_new_window", lambda *a: None)
    monkeypatch.setattr(implementer, "_implementer_driver", lambda *a: backend)
    implementer.launch_implementer({"id": "T1"}, "s1", tmp_path, tmp_path / "wt")
    source = (tmp_path / "launch.py").read_text()
    if driver == "claude":
        argv = _agent_argv(source)
        assert argv[argv.index("--effort") + 1] == effort
    else:
        assert _job(tmp_path)['effort'] == effort


def test_historical_roles_keep_the_migrated_effort():
    entry = model_policy.derived_roles({}, model_policy.shipped_policy_v1())["roles"]
    assert entry["implementer"]["efforts"]["claude"] == "medium"
    assert entry["planner"]["efforts"]["codex"] == "high"


def test_manual_model_pin_selects_its_own_effort(monkeypatch):
    monkeypatch.setattr(config, "load_project_yaml", lambda: {})
    monkeypatch.setattr(model_policy, "adopted_policy", lambda repo: (None, None))
    assert roles.effort_controls("implementer", "claude", "claude-opus-5-5") == (
        ("--effort", "low"), ())


@pytest.mark.parametrize("model", ["gpt-6-sol", "gpt-6-luna"])
def test_adopted_manual_pin_keeps_the_verified_native_launch_seam(project, model, monkeypatch):
    from tests.test_model_task_pins import _adopt
    from types import SimpleNamespace
    _adopt(project, model_policy.shipped_policy())
    monkeypatch.setattr(implementer, "operator_backend", lambda: "codex")
    monkeypatch.setattr(implementer, "_make_brief", lambda *a: "implement")
    monkeypatch.setattr(implementer, "append_journal", lambda *a: None)
    launched = []
    backend = SimpleNamespace(capabilities=lambda: SimpleNamespace(system_prompt_file=False),
        launch=lambda spec: launched.append(spec) or SimpleNamespace(window="impl-T1"))
    monkeypatch.setattr(implementer, "_implementer_driver", lambda *a: backend)
    workspace = project / "ws"
    workspace.mkdir()
    implementer.launch_implementer({"id": "T1", "model": model}, "s1", workspace, project)
    assert launched[0].model == model
    # A model-only manual override outside automatic eligibility requests no pair
    # effort. Retain the harness default, rather than inventing a graded pair.
    assert (launched[0].effort_args, launched[0].effort_config) == ((), ())


def test_codex_fresh_resume_fallback_keeps_effort_after_driver_restart(tmp_path, monkeypatch, server):
    from dataclasses import replace
    import json
    import subprocess
    import sys
    calls = tmp_path / "calls.jsonl"
    fake = tmp_path / "fake-codex"
    source = server.read_text().replace('mode = sys.argv[1]', "mode = 'missing'")
    source = source.replace('    m = json.loads(line)',
        '    m = json.loads(line)\n' +
        f"    with Path({str(calls)!r}).open('a') as out: out.write(json.dumps(m) + '\\n')")
    fake.write_text(f'#!{sys.executable}\n' + source)
    fake.chmod(0o755)
    cls = registry.launch_driver_class("codex")
    def driver():
        backend = cls(binary=str(fake))
        monkeypatch.setattr(backend, "binary_path", lambda: str(fake))
        monkeypatch.setattr(backend, "binary_warning", lambda: None)
        monkeypatch.setattr(backend, "_new_window", lambda *a: None)
        return backend
    spec = LaunchSpec("T1", "s1", "gpt-6.1-sol", "go", tmp_path, tmp_path,
                      effort_config=(("model_reasoning_effort", "high"),))
    handle = driver().launch(spec)
    from tests.backends.test_codex_driver import _settle_mocked_worker
    _settle_mocked_worker(tmp_path)
    driver().resume(replace(handle, native_id="recorded-native-thread"), "continue")
    result = subprocess.run([sys.executable, str(tmp_path / "launch.py")], cwd=tmp_path,
                            capture_output=True, text=True, timeout=HANG_SECONDS)
    assert result.returncode == 0, result.stderr
    requests = [json.loads(line) for line in calls.read_text().splitlines()]
    resume = [r for r in requests if r.get('method') == 'thread/resume']
    fresh = [r for r in requests if r.get('method') == 'thread/start']
    turns = [r for r in requests if r.get('method') == 'turn/start']
    assert len(resume) == len(fresh) == len(turns) == 1
    assert resume[0]['params']['threadId'] == 'recorded-native-thread'
    assert resume[0]['params']['model'] == fresh[0]['params']['model'] == spec.model
    assert turns[0]['params']['effort'] == _job(tmp_path)['effort'] == 'high'
    assert (tmp_path / 'codex_thread_id.txt').read_text() == 'thread'


@pytest.mark.parametrize("target,effort", [("claude", "medium"), ("codex", "low")])
@pytest.mark.parametrize("rotation", [False, True])
def test_switch_generated_launcher_delivers_selected_effort(target, effort, rotation, tmp_path, monkeypatch):
    from maestro import switch
    monkeypatch.setattr(config, "load_project_yaml", lambda: {})
    monkeypatch.setattr(config, "PROJECT_YAML", tmp_path / "project.yaml")
    monkeypatch.setattr(model_policy, "adopted_policy", lambda repo: (None, None))
    monkeypatch.setattr(switch, "WORKSPACES", tmp_path / "workspaces")
    monkeypatch.setattr(switch, "handoff_brief", lambda *a, **k: "continue")
    cls = registry.launch_driver_class(target)
    if target == "claude":
        monkeypatch.setattr(cls, "mode", lambda self: "headless")
        monkeypatch.setattr(cls, "_open_window", lambda *a: None)
    else:
        monkeypatch.setattr(cls, "binary_path", lambda self: "codex")
        monkeypatch.setattr(cls, "binary_warning", lambda self: None)
        # Patch the existing driver window boundary too: the regression must expose
        # wrong adapter selection without launching a real agent.
        monkeypatch.setattr(registry.driver_class(target), "_new_window", lambda *a: None)
    model = model_policy.derived_roles({}, None)["roles"]["implementer"]["models"][target]
    deps = switch.SwitchDeps(window_exists=lambda window: False, journal=lambda *a: None,
        notify=lambda *a: None, load_state=lambda: {}, save_state=lambda state: None,
        session_id_factory=lambda task: "incoming")
    outcome = switch.switch_task("T1", reason=switch.REASON_CONTEXT if rotation else switch.REASON_QUOTA,
        entry={"backend": target if rotation else ("claude" if target == "codex" else "codex"),
               "model": model, "worktree": str(tmp_path)}, to_backend=target, config={}, deps=deps)
    assert outcome.switched, outcome.note
    source = (outcome.workspace / "launch.py").read_text()
    if target == "claude":
        argv = _agent_argv(source)
        assert argv[argv.index("--effort") + 1] == effort
    else:
        assert _job(outcome.workspace)['effort'] == effort


def test_switch_generated_launcher_does_not_inventory_ambient_home(tmp_path, monkeypatch):
    """An argv fixture must not race an unrelated operator's mutable plugins."""
    from maestro.backends import control_files
    ambient = tmp_path / 'ambient-home'
    plugins = ambient / '.codex' / 'plugins'
    plugins.mkdir(parents=True)
    leaf = plugins / 'settings.json'
    leaf.write_text('synthetic old plugin metadata')
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: ambient))
    original = control_files.DirectoryView.verify
    replaced = []

    def verify(view):
        if view.path == plugins and not replaced:
            replacement = plugins / 'replacement.json'
            replacement.write_text('synthetic new plugin metadata')
            replacement.replace(leaf)
            replaced.append(str(leaf))
        return original(view)

    monkeypatch.setattr(control_files.DirectoryView, 'verify', verify)
    test_switch_generated_launcher_delivers_selected_effort(
        'codex', 'low', True, tmp_path, monkeypatch)
    assert not replaced, 'synthetic launcher reached unrelated ambient plugin inventory'


def test_switch_keeps_unadopted_frozen_schema1_effort(project, monkeypatch):
    from maestro import switch
    from types import SimpleNamespace
    with monkeypatch.context() as old:
        old.setattr(model_policy, "shipped_policy", model_policy.shipped_policy_v1)
        model_policy.freeze_task(project, "old", renew=True)
    monkeypatch.setattr(switch, "WORKSPACES", project / "workspaces")
    monkeypatch.setattr(switch, "handoff_brief", lambda *a, **k: "continue")
    launched = []
    driver = SimpleNamespace(capabilities=lambda: SimpleNamespace(system_prompt_file=False, native_resume=False),
        launch=lambda spec: launched.append(spec) or SimpleNamespace(window="impl-old"))
    deps = switch.SwitchDeps(driver_factory=lambda name: driver,
        window_exists=lambda window: False, journal=lambda *a: None, notify=lambda *a: None,
        load_state=lambda: {}, save_state=lambda state: None, session_id_factory=lambda task: "incoming")
    outcome = switch.switch_task("old", reason=switch.REASON_QUOTA,
        entry={"backend": "claude", "worktree": str(project)}, to_backend="codex", deps=deps)
    assert outcome.switched, outcome.note
    assert launched[0].model == "gpt-6-luna"
    assert launched[0].effort_config == (("model_reasoning_effort", "medium"),)
