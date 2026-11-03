"""`maestro doctor` beside a live controller: it reads, it never competes for ownership.

A real second process holds the controller lock and a real `maestro doctor` process runs
against the same project. Doctor once failed `docs` there ("controller pid ... already
owns"); on an idle project the same path made doctor claim the controller lease itself.
"""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import maestro
from maestro.control.store import ControlStore

#: The checkout under test: a child's cwd is the scratch project, so it needs this named.
CODE = str(Path(maestro.__file__).resolve().parents[1])

ROADMAP = textwrap.dedent("""\
    # Roadmap

    ### Task T1 - one
    ```yaml
    id: T1
    status: pending
    dispatch: manual
    ```
""")


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "docs").mkdir(parents=True)
    (root / ".orchestrator").mkdir()
    (root / "docs" / "ROADMAP.md").write_text(ROADMAP)
    (root / "docs" / "PROJECT.md").write_text("# Project\n")
    (root / ".orchestrator" / "state.json").write_text(json.dumps({"schema": 1}))
    ControlStore(root / ".orchestrator" / "control.sqlite3").close()
    return root


def _doctor(root, tmp_path):
    env = dict(os.environ, MAESTRO_REPO=str(root), MAESTRO_HOME=str(tmp_path / "home"),
               MAESTRO_GLOBAL_CONFIG=str(tmp_path / "no-global.yaml"),
               MAESTRO_BOOTSTRAPPED="1", PYTHONPATH=CODE)
    return subprocess.run([sys.executable, "-m", "maestro.cli", "doctor"], cwd=root, env=env,
                          capture_output=True, text=True, timeout=300)


def _lease_pid(root):
    with ControlStore(root / ".orchestrator" / "control.sqlite3") as store:
        row = store.conn.execute(
            "SELECT writer_pid FROM writer_leases WHERE resource_id = 'controller'").fetchone()
    return None if row is None else row["writer_pid"]


def test_doctor_beside_a_live_controller_reports_docs_and_leaves_its_lease(project, tmp_path):
    db = project / ".orchestrator" / "control.sqlite3"
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time\n"
         "from pathlib import Path\n"
         "from maestro.control.store import ControlStore\n"
         f"s = ControlStore.open_controller(Path({str(db)!r}))\n"
         "print('held', flush=True)\n"
         "time.sleep(600)\n"],
        stdout=subprocess.PIPE, text=True, env=dict(os.environ, PYTHONPATH=CODE))
    try:
        assert holder.stdout.readline().strip() == "held"
        result = _doctor(project, tmp_path)
        output = result.stdout + result.stderr
        assert "already owns" not in output, output
        assert "did not parse" not in output, output
        assert "docs: parsed OK" in output, output
        assert _lease_pid(project) == holder.pid
    finally:
        holder.kill()
        holder.wait()


@pytest.mark.parametrize("text", ["{not json", "[1, 2]"])
def test_a_corrupt_projection_is_named_as_state_json_not_the_roadmap(project, tmp_path, text):
    (project / ".orchestrator" / "state.json").write_text(text)
    output = _doctor(project, tmp_path).stdout
    assert "[FAIL] docs: state.json did not parse" in output, output
    assert "docs/ROADMAP.md did not parse" not in output, output


def test_doctor_on_an_idle_project_claims_no_controller_lease(project, tmp_path):
    result = _doctor(project, tmp_path)
    assert "parsed OK" in result.stdout + result.stderr, result.stdout + result.stderr
    assert _lease_pid(project) is None
