from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from maestro import adapters, cli


def _git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def test_node_project_without_a_test_script_is_not_reported_as_testable(tmp_path):
    repo = _git_repo(tmp_path / "node-no-tests")
    (repo / "package.json").write_text('{"scripts":{"build":"vite build"}}\n')

    facts = cli._derive_repo_facts(repo)

    assert facts["test_command"] == ""
    assert facts["package_manager"] == "npm"


def test_python_metadata_without_a_pytest_signal_is_not_reported_as_testable(tmp_path):
    repo = _git_repo(tmp_path / "python-no-tests")
    (repo / "pyproject.toml").write_text('[project]\nname="library"\nversion="1"\n')

    facts = cli._derive_repo_facts(repo)

    assert facts["test_command"] == ""
    assert facts["package_manager"] == "pip"


def test_root_test_commands_are_derived_from_project_files(tmp_path):
    node = _git_repo(tmp_path / "node")
    (node / "package.json").write_text('{"scripts":{"test":"node --test"}}\n')
    python = _git_repo(tmp_path / "python")
    (python / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths=["tests"]\n')
    go = _git_repo(tmp_path / "go")
    (go / "go.mod").write_text("module example.test/probe\n\ngo 1.23\n")

    assert cli._derive_repo_facts(node)["test_command_argv"] == ["npm", "test"]
    assert cli._derive_repo_facts(python)["test_command_argv"] == [
        str((python / ".venv" / "bin" / "python3").resolve(strict=False)),
        "-m",
        "pytest",
        "-q",
    ]
    assert cli._derive_repo_facts(go)["test_command_argv"] == ["go", "test", "./..."]


def test_rendered_test_adapter_runs_the_detected_command(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    marker = project / "command-ran"
    command = [os.sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('yes')"]
    template = (cli.TEMPLATES_DIR / "adapters" / "test").read_text(encoding="utf-8")
    rendered = cli._render(template, {"test_command_argv_json": json.dumps(command)})
    adapter = project / "adapters" / "test"
    adapter.parent.mkdir()
    adapter.write_text(rendered, encoding="utf-8")
    adapter.chmod(0o755)

    result = adapters.run_adapter("test", project)

    assert result.status == "ok"
    assert result.data and result.data["pass"] is True
    assert marker.read_text() == "yes"


def test_rendered_test_adapter_refuses_to_invent_a_command(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    template = (cli.TEMPLATES_DIR / "adapters" / "test").read_text(encoding="utf-8")
    rendered = cli._render(template, {"test_command_argv_json": "[]"})
    adapter = project / "adapters" / "test"
    adapter.parent.mkdir()
    adapter.write_text(rendered, encoding="utf-8")
    adapter.chmod(0o755)

    result = adapters.run_adapter("test", project)

    assert result.status == "nonzero_exit"
    assert "no test command was detected" in result.detail
