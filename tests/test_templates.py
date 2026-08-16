"""`maestro/templates/` — the static scaffolding `maestro init` copies/renders
(DESIGN.md §5, §10; `docs/plans/2026-08-16-m4-setup.md`'s Component B).

New M4 behaviour, not an extraction — these are plain files, not code, so the tests
here just verify the properties the plan calls out: every YAML-shaped template
actually parses (placeholders included — they must survive being quoted strings,
not break the YAML), every adapter/systemd/launcher stub is executable, and the
`docs/ROADMAP.md` skeleton round-trips through the *real* `maestro.docs.roadmap`
parser with zero runnable and zero prep tasks — proving the "empty roadmap" no-op
supervised loop scope decision is actually true of the shipped template, not just
asserted.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest
import yaml

from maestro import state as state_mod
from maestro.docs import roadmap as roadmap_mod

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = REPO_ROOT / "maestro" / "templates"

_YAML_NAME_RE = re.compile(r"\.ya?ml(\.tmpl)?$", re.IGNORECASE)


def _all_template_files() -> list[Path]:
    return [p for p in TEMPLATES_DIR.rglob("*") if p.is_file()]


def _is_executable(path: Path) -> bool:
    return os.access(path, os.X_OK) and bool(stat.S_IMODE(path.stat().st_mode) & 0o111)


# ── every YAML-shaped template parses ──


def _yaml_template_files() -> list[Path]:
    return [p for p in _all_template_files() if _YAML_NAME_RE.search(p.name)]


def test_at_least_one_yaml_template_exists():
    # A guard against the glob below silently matching nothing (e.g. a rename that
    # drops the ".yaml" in the filename would otherwise make every other assertion
    # in this module vacuously true).
    assert _yaml_template_files(), "expected at least one *.yaml*/*.yml* file under templates/"


@pytest.mark.parametrize("path", _yaml_template_files(), ids=lambda p: p.name)
def test_yaml_template_parses(path):
    # Placeholders like {{ project_name }} must be quoted in the template source —
    # unquoted, `{{ ... }}` parses as a YAML flow mapping, not a string, and blows up
    # with "found unhashable key". Parsing here with the placeholders still in place
    # (not rendered) is exactly the regression this guards against.
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), f"{path} did not parse to a mapping"


def test_project_yaml_tmpl_has_the_three_placeholders():
    doc = yaml.safe_load((TEMPLATES_DIR / "project.yaml.tmpl").read_text())
    assert doc["project"]["name"] == "{{ project_name }}"
    assert doc["project"]["repo_path"] == "{{ repo_path }}"
    assert doc["project"]["main_branch"] == "{{ main_branch }}"


# ── executability ──


def test_adapters_are_all_executable():
    adapters_dir = TEMPLATES_DIR / "adapters"
    files = [p for p in adapters_dir.iterdir() if p.is_file()]
    assert files, "expected at least one file under templates/adapters/"
    non_exec = [p.name for p in files if not _is_executable(p)]
    assert not non_exec, f"not executable: {non_exec}"


def test_required_and_optional_adapter_stubs_all_present():
    from maestro.adapters import KNOWN_KINDS
    adapters_dir = TEMPLATES_DIR / "adapters"
    missing = [kind for kind in KNOWN_KINDS if not (adapters_dir / kind).is_file()]
    assert not missing, f"templates/adapters/ is missing stubs for: {missing}"


def test_systemd_dir_files_are_executable():
    systemd_dir = TEMPLATES_DIR / "systemd"
    files = [p for p in systemd_dir.iterdir() if p.is_file()]
    assert files, "expected at least one file under templates/systemd/"
    non_exec = [p.name for p in files if not _is_executable(p)]
    assert not non_exec, f"not executable: {non_exec}"


def test_launch_sh_tmpl_is_executable():
    assert _is_executable(TEMPLATES_DIR / "launch.sh.tmpl")


# ── adapter stub contracts (Component A's run_adapter, exercised for real) ──


def test_optional_adapter_stubs_emit_ok_status_and_documented_shape(tmp_path):
    from maestro.adapters import run_adapter

    project_root = tmp_path / "proj"
    adapters_dst = project_root / "adapters"
    adapters_dst.mkdir(parents=True)
    for kind in ("eval", "smoke", "deploy", "healthcheck", "latency"):
        src = TEMPLATES_DIR / "adapters" / kind
        dst = adapters_dst / kind
        dst.write_bytes(src.read_bytes())
        dst.chmod(0o755)

    expected_keys = {
        "eval": {"metrics", "baseline", "deltas", "verdict", "token_free", "elapsed_s"},
        "smoke": {"pass", "metrics", "reason"},
        "deploy": {"deployed"},
        "healthcheck": {"healthy", "detail"},
        "latency": {"median_ms", "p95_ms"},
    }
    for kind, required_keys in expected_keys.items():
        result = run_adapter(kind, project_root)
        assert result.status == "ok", f"{kind}: expected status=ok, got {result.status} ({result.detail})"
        assert required_keys <= result.data.keys(), f"{kind}: missing keys, got {result.data.keys()}"


def test_eval_stub_reports_unevaluated_verdict_per_d8(tmp_path):
    from maestro.adapters import run_adapter

    project_root = tmp_path / "proj"
    adapters_dst = project_root / "adapters"
    adapters_dst.mkdir(parents=True)
    dst = adapters_dst / "eval"
    dst.write_bytes((TEMPLATES_DIR / "adapters" / "eval").read_bytes())
    dst.chmod(0o755)

    result = run_adapter("eval", project_root)
    assert result.status == "ok"
    assert result.data["verdict"] == "unevaluated"


def test_test_adapter_stub_fails_closed_with_no_tests_dir(tmp_path):
    """The one required adapter must never fabricate a pass. Per the plan: a fresh
    scaffold with no tests/ prints a TODO and exits 1, which `run_adapter` surfaces
    as `nonzero_exit` — not `ok`, and not a raised exception."""
    from maestro.adapters import run_adapter

    project_root = tmp_path / "proj"
    adapters_dst = project_root / "adapters"
    adapters_dst.mkdir(parents=True)
    dst = adapters_dst / "test"
    dst.write_bytes((TEMPLATES_DIR / "adapters" / "test").read_bytes())
    dst.chmod(0o755)

    result = run_adapter("test", project_root)
    assert result.status == "nonzero_exit"
    assert result.data is None


def test_test_adapter_stub_runs_real_pytest_when_tests_dir_present(tmp_path):
    from maestro.adapters import run_adapter

    project_root = tmp_path / "proj"
    adapters_dst = project_root / "adapters"
    adapters_dst.mkdir(parents=True)
    dst = adapters_dst / "test"
    dst.write_bytes((TEMPLATES_DIR / "adapters" / "test").read_bytes())
    dst.chmod(0o755)

    tests_dir = project_root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_smoke.py").write_text("def test_ok():\n    assert True\n")

    result = run_adapter("test", project_root)
    assert result.status == "ok"
    assert result.data["pass"] is True


# ── ROADMAP.md / PROJECT.md skeletons ──


@pytest.fixture
def _repo_with_roadmap_skeleton(tmp_path, monkeypatch):
    """Point the real `maestro.docs.roadmap` module (and the `maestro.state` module
    it calls into for `parse_prep_tasks`) at a scratch repo seeded with the shipped
    ROADMAP.md/PROJECT.md templates — the same monkeypatch-the-module-global pattern
    `tests/test_selfupdate.py` and `tests/test_switch.py` already use, since both
    modules resolve their path globals once at import time via `Paths.from_env()`."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "ROADMAP.md").write_text(
        (TEMPLATES_DIR / "docs" / "ROADMAP.md").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (docs_dir / "PROJECT.md").write_text(
        (TEMPLATES_DIR / "docs" / "PROJECT.md").read_text(encoding="utf-8"), encoding="utf-8"
    )

    orch_dir = tmp_path / ".orchestrator"
    orch_dir.mkdir()
    state_file = orch_dir / "state.json"
    state_file.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(roadmap_mod, "ROADMAP_FILE", docs_dir / "ROADMAP.md")
    monkeypatch.setattr(roadmap_mod, "COMPLETED_TASKS", orch_dir / "completed_tasks.json")
    monkeypatch.setattr(state_mod, "STATE_JSON", state_file)
    return tmp_path


def test_roadmap_skeleton_has_zero_runnable_tasks(_repo_with_roadmap_skeleton):
    assert roadmap_mod.parse_runnable_tasks() == []


def test_roadmap_skeleton_has_zero_prep_tasks(_repo_with_roadmap_skeleton):
    assert roadmap_mod.parse_prep_tasks() == []


def test_roadmap_skeleton_example_block_is_real_yaml_fence_not_prose(_repo_with_roadmap_skeleton):
    """Zero tasks must come from the example being a *commented-out* ```yaml block
    (so the real parser's own fence regex finds and then discards it), not from the
    file simply lacking any ```yaml fence at all — which would prove nothing about
    round-tripping through the parser."""
    content = (TEMPLATES_DIR / "docs" / "ROADMAP.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```yaml\n(.*?)```", content, re.DOTALL)
    assert blocks, "expected at least one ```yaml fenced block in the ROADMAP skeleton"
    assert all(yaml.safe_load(b) is None for b in blocks), (
        "every example ```yaml block in the skeleton must parse to None (all-comment), "
        "so parse_runnable_tasks/parse_prep_tasks see zero real tasks"
    )


def test_project_md_skeleton_is_nonempty_and_points_at_the_roadmap():
    text = (TEMPLATES_DIR / "docs" / "PROJECT.md").read_text(encoding="utf-8")
    assert text.strip()
    assert "docs/ROADMAP.md" in text
