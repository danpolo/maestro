"""Real OS write refusal, using Python as the child rather than an LLM."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from maestro import confinement
from tests.hang_bound import HANG_SECONDS


def test_writer_persists_only_assigned_roots_and_keeps_control_files_read_only(tmp_path):
    tree = tmp_path / "tree"
    workspace = tmp_path / "workspace"
    control = workspace / "control"
    outside = tmp_path / "outside"
    for path in (tree, control, outside):
        path.mkdir(parents=True)
    (tree / "escape").symlink_to(outside, target_is_directory=True)
    (control / "policy.json").write_text("frozen")
    paths = [tree / "change.txt", workspace / "DONE", outside / "bad.txt",
             tree / "escape" / "bad.txt", control / "policy.json"]
    program = """
import json, pathlib, sys
out = []
for name in json.loads(sys.argv[1]):
    try:
        pathlib.Path(name).write_text('persisted')
        out.append(True)
    except OSError:
        out.append(False)
print(json.dumps(out))
"""
    argv = confinement.bubblewrap_argv(
        [sys.executable, "-c", program, json.dumps(list(map(str, paths)))],
        cwd=tree, write_roots=(tree, workspace), read_only_roots=(control,),
    )
    if shutil.which("bwrap") is None:
        pytest.skip("bubblewrap is not installed")
    done = subprocess.run(argv, capture_output=True, text=True, timeout=HANG_SECONDS)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [True, True, False, False, False]
    assert (tree / "change.txt").read_text() == "persisted"
    assert (workspace / "DONE").read_text() == "persisted"
    assert (control / "policy.json").read_text() == "frozen"
    assert not (outside / "bad.txt").exists()


def test_reader_can_report_without_writing_reviewed_tree(tmp_path):
    tree = tmp_path / "tree"
    workspace = tmp_path / "workspace"
    tree.mkdir()
    workspace.mkdir()
    source = tree / "source.py"
    source.write_text("original")
    program = """
import pathlib, sys
source, verdict = map(pathlib.Path, sys.argv[1:])
assert source.read_text() == 'original'
try:
    source.write_text('changed')
except OSError:
    verdict.write_text('refused')
else:
    raise SystemExit('reader wrote reviewed tree')
"""
    if shutil.which("bwrap") is None:
        pytest.skip("bubblewrap is not installed")
    done = subprocess.run(confinement.bubblewrap_argv(
        [sys.executable, "-c", program, str(source), str(workspace / "review.json")],
        cwd=workspace, write_roots=(workspace,),
    ), capture_output=True, text=True, timeout=HANG_SECONDS)
    assert done.returncode == 0, done.stderr
    assert source.read_text() == "original"
    assert (workspace / "review.json").read_text() == "refused"


def test_missing_or_overbroad_root_refuses_before_starting_a_process(tmp_path):
    for root in (tmp_path / "missing", Path("/"), Path.home()):
        with pytest.raises(confinement.ConfinementError):
            confinement.bubblewrap_argv(["unused"], cwd=tmp_path, write_roots=(root,))
