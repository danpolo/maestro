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


# ── roles: block names exactly the roles maestro.roles resolves (G6) ──


def test_project_yaml_tmpl_roles_are_exactly_the_known_roles():
    """`project.yaml.tmpl`'s `roles:` block must declare exactly `roles.KNOWN_ROLES` — no
    more (a role no code resolves, like the struck `orchestrator` entry) and no fewer (a
    real role, like `diagnoser`, silently getting no configured model on every adopted
    project)."""
    from maestro import roles as roles_mod

    doc = yaml.safe_load((TEMPLATES_DIR / "project.yaml.tmpl").read_text())
    assert set(doc["roles"].keys()) == set(roles_mod.KNOWN_ROLES)


def test_project_yaml_tmpl_diagnoser_role_is_actually_read_by_role_config():
    """Observable effect, not just presence: the backend/model the template declares for
    `diagnoser` is what `roles.role_config` resolves when handed this exact document —
    proving the block is read, not merely parsed."""
    from maestro import roles as roles_mod

    doc = yaml.safe_load((TEMPLATES_DIR / "project.yaml.tmpl").read_text())
    entry = doc["roles"][roles_mod.ROLE_DIAGNOSER]

    settings = roles_mod.role_config(roles_mod.ROLE_DIAGNOSER, doc)

    assert settings.backend == entry["backend"]
    assert settings.model_for(entry["backend"]) == entry["models"][entry["backend"]]


# ── model_limits: block names exactly the knob maestro.limits reads (G6) ──


def test_project_yaml_tmpl_model_limits_is_actually_read_by_default_table_paths(
    monkeypatch, tmp_path
):
    """Observable effect: `default_table_paths()` reads *this document's* `model_limits:`
    mapping, not merely a coincidence of the template's shown defaults matching the
    hardcoded ones. The declared value is mutated to a path nothing else could produce, so
    the assertion only holds if the knob is actually wired."""
    from maestro import config as _config
    from maestro import limits

    doc = yaml.safe_load((TEMPLATES_DIR / "project.yaml.tmpl").read_text())
    assert "model_limits" in doc, "template dropped the model_limits knob entirely"

    custom = tmp_path / "custom_claude_limits.md"
    doc["model_limits"] = {"claude": str(custom)}
    monkeypatch.setattr(_config, "load_project_yaml", lambda: doc)

    assert limits.default_table_paths() == [custom]


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


# ── launcher + unit run the watchdog, not the loop (M4b) ──
#
# Until M4b these two templates carried an "Honesty note" saying `maestro/watchdog.py` did not
# exist and the launcher therefore ran `maestro run` directly. It exists now
# (docs/plans/2026-08-17-m4b-watchdog.md), the note is gone, and these pin the replacement so a
# future edit cannot quietly regress the launcher to the unsupervised command.


def test_launch_sh_tmpl_runs_the_watchdog_not_the_orchestrator_loop():
    """`maestro run` here instead of `maestro watchdog` loses relaunch-on-crash, resume
    scheduling, stall detection and implementer reaping — systemd supervises this shell, so it
    fires when the tmux session ends, not when the orchestrator inside it dies."""
    text = (TEMPLATES_DIR / "launch.sh.tmpl").read_text(encoding="utf-8")
    command_lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    body = "\n".join(command_lines)

    assert "watchdog" in body
    assert "maestro run" not in body
    # Absolute path to this project's own venv entrypoint, not a bare `maestro` — a new
    # tmux session's PATH comes from the tmux *server*'s environment, not this script's,
    # and is not guaranteed to include `.venv/bin` (found during a live M5 cutover:
    # `which maestro` failed inside a freshly spawned window of an existing tmux
    # server). See the matching fix in `maestro/watchdog.py`'s own tmux spawns.
    assert "MAESTRO_REPO='$REPO_PATH' '$REPO_PATH/.venv/bin/maestro' watchdog" in body


def test_launch_sh_tmpl_still_wraps_tmux_and_blocks_for_type_simple():
    """DESIGN.md §10's Systemd-and-tmux subsection: named session for phone auditing, then a
    wait loop so systemd tracks this shell (Type=forking is unusable — the tracked process lives
    in the tmux server's cgroup)."""
    text = (TEMPLATES_DIR / "launch.sh.tmpl").read_text(encoding="utf-8")
    assert 'SESSION="${PROJECT_NAME}-watchdog"' in text
    assert "tmux new-session -d -s" in text
    assert 'while tmux has-session -t "$SESSION"' in text


def test_watchdog_unit_keeps_the_deliberate_systemd_settings():
    text = (TEMPLATES_DIR / "systemd" / "maestro-watchdog.service.tmpl").read_text(encoding="utf-8")
    assert "Type=simple" in text
    assert "Type=forking" not in text.split("[Service]")[1]
    assert "ExecStart=/bin/bash {{ repo_path }}/launch.sh" in text
    assert "Restart=on-failure" in text
    assert "tmux kill-session -t {{ project_name }}-watchdog" in text


@pytest.mark.parametrize(
    "path",
    ["launch.sh.tmpl", "systemd/maestro-watchdog.service.tmpl"],
)
def test_no_template_still_claims_the_watchdog_module_is_missing(path):
    text = (TEMPLATES_DIR / path).read_text(encoding="utf-8").lower()
    for stale in ("honesty note", "does not exist", "doesn't exist", "not yet implemented",
                  "honest stub"):
        assert stale not in text, f"{path} still carries the pre-M4b note: {stale!r}"


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


# ── no knob the template offers may be one nothing reads ──


def _config_keys(node, path=()):
    """Every leaf-ish key path in a parsed YAML mapping."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield path + (str(key),)
            yield from _config_keys(value, path + (str(key),))


def test_project_yaml_declares_no_key_that_nothing_reads():
    """A knob an adopting project can set must actually change behaviour.

    `switch.on_usage_threshold` shipped for months with zero readers — anyone who tuned it
    got silence, while the hardcoded `SWITCH_THRESHOLD_PCT` stayed in charge. This pins the
    general rule so the next dead knob is caught at the template, not in production.

    Known-dead keys are listed explicitly rather than exempted silently: each one is a
    real gap between the documented surface and the enforced behaviour, and the list is
    meant to shrink. `maestro/` is searched for the bare key name, which is how every
    live reader spells it (`_THRESHOLDS.get("stall_window_min")`, `.get("name")`).
    """
    template = (TEMPLATES_DIR / "project.yaml.tmpl").read_text(encoding="utf-8")
    parsed = yaml.safe_load(template)

    # Documented-but-unwired, each already recorded in docs/PROGRESS.md's open questions.
    # Removing an entry here is the acceptance test for wiring that knob up.
    known_dead = {
        ("switch", "on_quota_exhausted"),
        ("switch", "manual"),
        # D8's eval-gate scoring inputs. `gate.chain` is read (cli.py), and
        # `gate.primary_metric` since 2026-08-30 (`maestro/metrics.py` — it names the
        # number an operator is quoted first, replacing the hardcoded `recall_at_5` every
        # display site used to read). These two remain: they describe how to *score* a
        # run against a baseline, and nothing consumes them — the gate still only runs
        # the chain and journals `unevaluated`.
        ("gate", "baseline_source"),
        ("gate", "bands"),
    }

    sources = "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in (REPO_ROOT / "maestro").rglob("*.py")
        if "templates" not in p.parts
    )

    unread = {
        keypath for keypath in _config_keys(parsed)
        if keypath not in known_dead and keypath[-1] not in sources
    }
    assert not unread, (
        "project.yaml offers keys no code reads: "
        + ", ".join(".".join(k) for k in sorted(unread))
        + " — wire them up, or drop them from the template rather than shipping a knob "
          "that silently does nothing"
    )


def test_project_yaml_does_not_redeclare_the_removed_usage_threshold_block():
    """Guards the specific regression: a per-window `on_usage_threshold` contradicts the
    window-agnostic check `switch.threshold_crossed()` actually performs (finding G5)."""
    template = (TEMPLATES_DIR / "project.yaml.tmpl").read_text(encoding="utf-8")
    parsed = yaml.safe_load(template) or {}
    assert "on_usage_threshold" not in (parsed.get("switch") or {})
