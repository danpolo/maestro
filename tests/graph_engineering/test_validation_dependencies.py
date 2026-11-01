from __future__ import annotations

import shutil
import subprocess
import os
import json
import re
from pathlib import Path

import pytest


FIXTURE = Path(__file__).parent / "fixtures" / "dependency_project"
CATALOG = Path(__file__).parent / "fixtures" / "cases.json"
ACCEPTANCE = "acceptance"
MUTATIONS = json.loads(CATALOG.read_text(encoding="utf-8"))["dependency_cases"]


def _run(project: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            os.sys.executable,
            "-m",
            "pytest",
            "-q",
            "-o",
            "python_files=verify_*.py",
            ACCEPTANCE,
        ],
        cwd=project,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(project)},
    )


def _signatures(text: str) -> list[str]:
    return re.findall(r"^(?:async\s+)?(?:def|class)\s+[A-Za-z_]\w*", text, flags=re.MULTILINE)

def test_pristine_dependency_fixture_passes_the_complete_acceptance_suite(tmp_path):
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project)

    result = _run(project)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "5 passed" in result.stdout


@pytest.mark.parametrize("case", MUTATIONS, ids=lambda case: case["dependency_kind"])
def test_complete_suite_catches_dependency_classes_a_signature_selector_misses(tmp_path, case):
    project = tmp_path / case["dependency_kind"]
    shutil.copytree(FIXTURE, project)
    changed = project / case["changed"][0]
    before_signatures = _signatures(changed.read_text(encoding="utf-8"))
    changed.write_text(case["replacement"], encoding="utf-8")
    after_signatures = _signatures(changed.read_text(encoding="utf-8"))

    result = _run(project)

    assert result.returncode == 1, result.stdout + result.stderr
    assert before_signatures == after_signatures
