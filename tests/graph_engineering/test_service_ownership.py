from __future__ import annotations

import os
import subprocess
from pathlib import Path

from maestro import cli, selfupdate


def _render_unit(repo: Path, project: str = "fixture-project") -> str:
    template = (cli.TEMPLATES_DIR / "systemd" / "maestro-watchdog.service.tmpl").read_text()
    facts = {"project_name": project, "repo_path": str(repo)}
    return cli._render(template, {**facts, **cli._systemd_service_mapping(facts)})


def test_generated_service_uses_a_named_tmux_owner_and_forking_contract(tmp_path):
    unit = _render_unit(tmp_path)
    service = unit.split("[Service]", 1)[1]

    assert "Type=forking" in service
    assert (
        f'ExecStart=/usr/bin/tmux new-session -d -s "fixture-project-watchdog" -n main '
        f'-c {tmp_path} ' in service
    )
    assert 'ExecStop=-/usr/bin/tmux kill-session -t "fixture-project-watchdog"' in service


def test_generated_service_quotes_a_repo_path_with_spaces(tmp_path):
    repo = tmp_path / "project with spaces"
    repo.mkdir()
    unit = _render_unit(repo, project="project with spaces")

    escaped_repo = str(repo).replace(" ", r"\x20")
    assert f"WorkingDirectory={escaped_repo}" in unit
    assert f"-c {escaped_repo}" in unit
    assert "MAESTRO_REPO=. exec ./.venv/bin/maestro watchdog" in unit
    assert '-s "project with spaces-watchdog"' in unit
    unit_path = tmp_path / "project-with-spaces-watchdog.service"
    unit_path.write_text(unit, encoding="utf-8")
    result = subprocess.run(["systemd-analyze", "verify", unit_path], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_generated_service_parses_with_systemd_analyze(tmp_path):
    binary = tmp_path / ".venv" / "bin" / "maestro"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    unit_path = tmp_path / "fixture-project-watchdog.service"
    unit_path.write_text(_render_unit(tmp_path))

    result = subprocess.run(
        ["systemd-analyze", "verify", os.fspath(unit_path)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_two_projects_currently_share_one_adopted_version_pointer(tmp_path, monkeypatch):
    home = tmp_path / "maestro-home"
    monkeypatch.setenv(selfupdate.ENV_VAR, str(home))

    project_a = selfupdate.SelfUpdatePaths.from_env()
    project_b = selfupdate.SelfUpdatePaths.from_env()

    assert project_a.current_file == project_b.current_file == home / "current"
