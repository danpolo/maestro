"""`maestro/cli.py` — the `maestro` console-script entry point
(DESIGN.md §10; `docs/plans/2026-08-16-m4-setup.md`'s Component D).

**Why almost everything here shells out to `python3 -m maestro.cli ...` instead of calling
`maestro.cli.cmd_*` functions directly.** Every extracted module `cli.py` touches
(`maestro.state`, `maestro.docs.roadmap`, `maestro.config`, `maestro.limits`, ...) resolves
its project root exactly once, at *import* time, via `Paths.from_env()` — see `maestro/cli.py`'s
own module docstring ("one project per process"). `python3 -m pytest` runs every test file in
one interpreter; some earlier test module (in this file or any other collected by the same run)
may already have imported `maestro.state` bound to a *different* root. Calling `cmd_doctor()`/
`cmd_status()`/`cmd_ctl()`/`cmd_run()` in-process would silently read or write whichever root
that module happened to bind to first — for `maestro.state` that could be this maestro checkout's
own `.orchestrator/state.json`. A subprocess is a fresh interpreter every time, so it is the only
way to test two different target projects safely in the same pytest session. `install-skills` has
no such dependency (no `Paths.from_env()` in its call graph) and is called in-process directly,
which is both safe and much faster. `watchdog` *does* have one as of M4b — `maestro/watchdog.py`
binds `REPO`/`STATE_JSON`/`JOURNAL`/`HALT_FILE` from `Paths.from_env()` at import time, exactly
like `maestro.state` — so the one test here that runs the real module runs it in a subprocess
too. The in-process `cmd_watchdog` test never imports it: it substitutes a recording stub into
`sys.modules["maestro.watchdog"]`, which is what lets it assert the wiring (env var, exit-code
passthrough) without binding a root or entering a `while True:` supervisor loop.

**Telegram**: no test here ever reaches `api.telegram.org`. `init`'s subprocess calls always set
`$MAESTRO_TOKEN_FILE` to a path that does not exist, so the real `scripts/telegram_creds.py`
(invoked exactly as production `init` invokes it) fails at its very first line — before reading
any file or opening any socket — the same "token file absent, skip Telegram setup" path a real
project without credentials takes. `doctor`'s Telegram check is exercised only with an injected
`http_get` stub (never the real network-calling default) via `cli.cmd_doctor`'s in-process call
(safe: `_check_telegram` takes no project-rooted-module dependency of its own beyond reading a
local `.env` file with `python-dotenv`).

**Cost**: the `init`-driving tests are slow by construction — `maestro.orchestrator.main()`'s
natural-return mechanism (see `maestro/cli.py`'s docstring) needs one real `POLL_INTERVAL` sleep
(30s) to complete its idle pass. `_healthy_project` is a module-scoped fixture so that cost is
paid once, not once per test that needs an already-initialized project.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── helpers ──


def _git_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    return root


def _run_cli(*args: str, cwd: Path | None = None, timeout: int = 60,
             extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """`python3 -m maestro.cli <args>` as a genuinely separate process. `MAESTRO_TOKEN_FILE`
    always points at a path that cannot exist, so any `init` call here can never reach the
    operator's real token file or the network — see this module's docstring."""
    env = dict(os.environ)
    parts = env.get("PYTHONPATH", "").split(os.pathsep) if env.get("PYTHONPATH") else []
    if str(REPO_ROOT) not in parts:
        parts.insert(0, str(REPO_ROOT))
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env["MAESTRO_TOKEN_FILE"] = "/nonexistent/maestro-test-token-file-does-not-exist"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "maestro.cli", *args],
        cwd=str(cwd) if cwd else None, env=env,
        capture_output=True, text=True, timeout=timeout,
    )


def _is_executable(path: Path) -> bool:
    return os.access(path, os.X_OK) and bool(stat.S_IMODE(path.stat().st_mode) & 0o111)


def _path_shadowing_agent_clis(shadow_dir: Path) -> str:
    """`$PATH` with a directory of harmless `claude`/`codex` stubs prepended ahead of
    everything else, so those two names resolve to the stubs instead of the real CLIs —
    or, on this machine, instead of an *unrelated* `codex` that also happens to sit on
    `/usr/bin`/`/bin` (some other package's binary; confirmed by hand while writing this
    helper), which a naive "strip any PATH entry containing codex" approach would have
    caught along with `python3` and broken every adapter.

    `doctor`'s new C7 `model_ids` check (and C5's existing `backends` check) both call the
    real CLI's binary when not given an injected stub — harmless for `backends`'
    `--version` probe, but `model_ids` runs a real (if bounded) `-p`/`exec` turn, which is
    neither cheap nor something a test may do (`maestro/backends/registry.py`'s "no test
    may execute a real agent CLI" rule). A `doctor` invoked through `_run_cli` is a genuine
    subprocess with no seam to inject a stub through, so PATH-shadowing — the same
    technique `test_run_requires_the_shared_agents_tmux_session` already uses for `tmux` —
    is the only lever available. Each stub exits `1` immediately with no output, which
    both checks read as "not this backend's rejection marker" (safe) without ever
    executing anything real."""
    shadow_dir.mkdir(exist_ok=True)
    for name in ("claude", "codex"):
        stub = shadow_dir / name
        stub.write_text("#!/bin/sh" + chr(10) + "exit 1" + chr(10))
        stub.chmod(0o755)
    return f"{shadow_dir}{os.pathsep}{os.environ.get('PATH', '')}"


# ── init: scaffold + idempotent re-run ──


@pytest.fixture(scope="module")
def _healthy_project(tmp_path_factory):
    """One real `maestro init` run (paid once — see this module's docstring), reused
    read-mostly by `doctor`/`status`/`ctl` tests. Individual tests that mutate state
    (`ctl pause`, ...) restore it before returning."""
    root = _git_repo(tmp_path_factory.mktemp("healthy") / "proj")
    proc = _run_cli("init", str(root), timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return root, proc


def test_init_scaffolds_every_component_b_template(_healthy_project):
    root, proc = _healthy_project

    project_yaml = yaml.safe_load((root / "project.yaml").read_text())
    assert project_yaml["project"]["name"] == root.name
    assert project_yaml["project"]["repo_path"] == str(root)
    rendered_yaml_text = (root / "project.yaml").read_text()
    # every real placeholder is filled in; the template's own prose example of the literal
    # tokens `{{ ... }}` (in a comment explaining why placeholders must be quoted) is left
    # alone on purpose — see `_render`'s docstring — so this checks the specific tokens
    # rather than a blanket "no more '{{' anywhere" scan.
    for token in ("{{ project_name }}", "{{ repo_path }}", "{{ main_branch }}"):
        assert token not in rendered_yaml_text

    for kind in ("test", "eval", "smoke", "deploy", "healthcheck", "latency"):
        p = root / "adapters" / kind
        assert p.is_file() and _is_executable(p), f"adapters/{kind} missing or not executable"

    assert (root / "operating_preamble.md").is_file()
    for role in ("orchestrator", "implementer", "judge"):
        assert (root / "profiles" / f"{role}.md").is_file()
    assert (root / "docs" / "ROADMAP.md").is_file()
    assert (root / "docs" / "PROJECT.md").is_file()

    pre_commit = root / ".git" / "hooks" / "pre-commit"
    assert pre_commit.is_file() and _is_executable(pre_commit)
    launch_sh = root / "launch.sh"
    assert launch_sh.is_file() and _is_executable(launch_sh)
    assert f"MAESTRO_REPO='{root}'" in launch_sh.read_text() or str(root) in launch_sh.read_text()
    assert (root / "systemd" / f"{root.name}-watchdog.service").is_file()
    assert (root / ".gitignore").is_file()

    assert (root / ".orchestrator" / "state.json").is_file()
    venv_python = root / ".venv" / "bin" / "python3"
    assert venv_python.is_symlink(), "the .venv/bin/python3 landmine workaround must be a symlink"

    assert "Telegram setup: skipped" in proc.stdout


def test_init_no_op_supervised_loop_left_the_expected_journal_trail(_healthy_project):
    """Direct evidence the natural-return mechanism documented in `maestro/cli.py` actually
    fired during `_healthy_project`'s own `init` call: both `while True:` iterations described
    there (queue-exhausted -> paused_by_user=True -> next-iteration halt-respected break) must
    have left their journal events — this is the "no-op supervised loop mechanism exercised"
    evidence the M4 plan asks for, read back from what really happened rather than re-asserted."""
    root, _ = _healthy_project
    lines = [
        json.loads(line)
        for line in (root / ".orchestrator" / "journal.ndjson").read_text().splitlines()
        if line.strip()
    ]
    events = [rec["event"] for rec in lines]
    assert "queue_exhausted" in events, events
    assert "halt_respected" in events, events


def test_init_second_run_is_non_destructive(_healthy_project):
    """Hand-edit a scaffolded file, re-run `init`, and confirm DESIGN.md §10's rule: the
    original is untouched and the rendered content lands beside it as `.new` with a diff.
    This also re-exercises the no-op supervised loop check a second time — the scenario that
    originally surfaced the `paused_by_user`/`proposal_test_notified` staleness documented in
    `_reset_control_flags_for_loop_check`'s docstring (this test would hang/timeout without it)."""
    root, _ = _healthy_project
    project_yaml_path = root / "project.yaml"
    original = project_yaml_path.read_text()
    hand_edited = original + "\n# hand-edited by the operator, must survive re-init\n"
    project_yaml_path.write_text(hand_edited)

    new_marker_path = root / "adapters" / "eval.new"
    assert not new_marker_path.exists()

    proc = _run_cli("init", str(root), timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    # untouched — the hand-edit is still exactly there
    assert project_yaml_path.read_text() == hand_edited
    # the rendered content went to .new instead, with the diff shown on stdout
    new_path = root / "project.yaml.new"
    assert new_path.is_file()
    assert new_path.read_text() == original
    assert "project.yaml already exists and differs" in proc.stdout
    assert "# hand-edited by the operator" in proc.stdout  # the diff shows the operator's line
    # every adapter was unchanged (identical content) -> no .new noise for them
    assert not new_marker_path.exists()

    new_path.unlink()  # leave the fixture directory clean for later tests in this module


def test_init_rejects_a_non_git_directory(tmp_path):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    proc = _run_cli("init", str(not_a_repo), timeout=30)
    assert proc.returncode == 1
    assert "git" in proc.stdout.lower()
    assert not (not_a_repo / "project.yaml").exists()


# ── doctor ──


def test_doctor_healthy_project_fails_only_on_the_unwired_test_adapter(_healthy_project, tmp_path):
    """A freshly-scaffolded project's `adapters/test` stub deliberately fails until a real
    tests/ dir exists (see `templates/adapters/test`'s own docstring) — so `doctor` correctly
    reports the *required* adapter check as FAIL and exits non-zero, while every optional
    check passes or only nags. This is intentional D8 honesty, not a broken fixture.

    The scaffolded `project.yaml` carries real model ids (`templates/project.yaml.tmpl`'s
    `roles:` example), so `doctor`'s `model_ids` check would otherwise probe the real
    `claude`/`codex` CLIs for real through this genuine subprocess — there is no seam to
    inject a stub through here. `_path_shadowing_agent_clis` shadows both with harmless
    stubs so that probe (and C5's `backends` one) fails fast instead."""
    root, _ = _healthy_project
    proc = _run_cli(
        "doctor", "--repo", str(root), timeout=60,
        extra_env={"PATH": _path_shadowing_agent_clis(tmp_path / "shadow-bin")},
    )
    assert proc.returncode == 1
    assert "[OK  ] docs:" in proc.stdout
    assert "[FAIL] adapter:test:" in proc.stdout
    for kind in ("eval", "smoke", "deploy", "healthcheck", "latency"):
        assert f"[OK  ] adapter:{kind}:" in proc.stdout


def test_doctor_healthy_project_passes_once_a_real_test_adapter_exists(_healthy_project):
    root, _ = _healthy_project
    tests_dir = root / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test_smoke.py").write_text("def test_ok():\n    assert True\n")
    try:
        proc = _run_cli("doctor", "--repo", str(root), "--pre-commit", timeout=60)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "[OK  ] adapter:test:" in proc.stdout
    finally:
        # the adapter genuinely ran pytest inside tests_dir, which leaves __pycache__ /
        # .pytest_cache behind — rmtree, not a plain rmdir, to actually clean it up.
        import shutil
        shutil.rmtree(tests_dir)


def test_doctor_pre_commit_runs_only_the_fast_subset(_healthy_project):
    """`templates/pre-commit` probes `maestro doctor --help` for `--pre-commit` before ever
    relying on it — this pins that the flag really exists and really narrows the checklist."""
    root, _ = _healthy_project
    proc = _run_cli("doctor", "--repo", str(root), "--pre-commit", timeout=60)
    assert "model_limits" not in proc.stdout
    assert "backends" not in proc.stdout
    assert "telegram" not in proc.stdout
    assert "docs:" in proc.stdout
    assert "adapter:test:" in proc.stdout


def test_doctor_help_advertises_pre_commit_flag():
    proc = _run_cli("doctor", "--help", timeout=30)
    assert "--pre-commit" in proc.stdout


def test_doctor_broken_project_reports_missing_docs_and_missing_required_adapter(tmp_path):
    root = tmp_path / "broken"
    root.mkdir()
    proc = _run_cli("doctor", "--repo", str(root), timeout=60)
    assert proc.returncode == 1
    assert "[FAIL] docs:" in proc.stdout
    assert "[FAIL] adapter:test:" in proc.stdout
    assert "status=absent" in proc.stdout


def test_doctor_telegram_check_never_touches_the_real_network(tmp_path):
    """`cmd_doctor`'s `http_get` parameter is the injectable seam DESIGN.md's scope decision
    requires. Called in-process here (safe: `_check_telegram` has no `Paths.from_env()`
    dependency of its own) with a stub that would fail the test if it were ever the real
    `urllib`-backed default — proving the injection actually replaces it."""
    from maestro import cli

    root = tmp_path / "proj"
    root.mkdir()
    (root / ".env").write_text("TELEGRAM_BOT_TOKEN=fake-test-token\n")

    calls = []

    def _stub_http_get(url):
        calls.append(url)
        return {"ok": True, "result": {"username": "fake_test_bot"}}

    check = cli._check_telegram(root, _stub_http_get)
    assert check.ok
    assert calls == ["https://api.telegram.org/botfake-test-token/getMe"]


def test_doctor_telegram_check_skips_cleanly_with_no_token_configured(tmp_path):
    from maestro import cli

    root = tmp_path / "proj"
    root.mkdir()

    def _fail_if_called(url):
        raise AssertionError("http_get must not be called when no token is configured")

    check = cli._check_telegram(root, _fail_if_called)
    assert check.ok


@pytest.fixture
def _clean_backend_binary_cache():
    """Mirrors `tests/backends/test_registry.py`'s `_clean_cache`: "no test may see
    another test's resolved binaries." Cleared both before and after, not only before —
    otherwise the fabricated `claude`/`codex` `BinaryInfo` entries these tests install
    would sit in the process-global `_BINARY_CACHE`, keyed on the real `PATH`, for every
    later in-process test in the same pytest session (a latent trap for C7, which adds
    another `doctor` check against this same registry)."""
    from maestro.backends import registry as backend_registry
    backend_registry.clear_binary_cache()
    yield
    backend_registry.clear_binary_cache()


def test_check_backends_probes_the_full_fallback_chain(monkeypatch, _clean_backend_binary_cache):
    """All-claude `roles:` plus `fallback_chain: [claude, codex]` — the scaffolded
    template's actual shape (C5/G8) — must report the missing `codex` binary. Before this
    fix, `_check_backends` only looked at names written in `roles:`, so a machine with no
    `codex` binary passed `doctor` cleanly and the gap only surfaced later as a backend
    switch that silently didn't happen.

    `_probe_version` and `find_binaries` are stubbed rather than left to scan the real
    `PATH` — per `maestro/backends/registry.py`'s docstring, no test may execute a real
    agent CLI, and this one is about binary probing specifically."""
    from maestro import cli
    from maestro import config as _config
    from maestro.backends import registry as backend_registry

    cfg = {
        "roles": {
            "implementer": {"backend": "claude"},
            "judge": {"backend": "claude"},
            "diagnoser": {"backend": "claude"},
        },
        "fallback_chain": ["claude", "codex"],
    }
    monkeypatch.setattr(_config, "load_project_yaml", lambda: cfg)
    monkeypatch.setattr(backend_registry, "_probe_version", lambda path: "1.0.0")
    monkeypatch.setattr(
        backend_registry, "find_binaries",
        lambda tool, path_env=None: [Path(f"/fake/bin/{tool}")] if tool == "claude" else [],
    )

    check = cli._check_backends(Path("/unused"))
    assert not check.ok
    assert "codex" in check.detail


def test_check_backends_passes_when_every_chain_backend_is_present(monkeypatch, _clean_backend_binary_cache):
    """The companion green path: once `codex` is also on `PATH`, the same all-claude
    `roles:` plus `fallback_chain: [claude, codex]` passes cleanly."""
    from maestro import cli
    from maestro import config as _config
    from maestro.backends import registry as backend_registry

    cfg = {
        "roles": {"implementer": {"backend": "claude"}},
        "fallback_chain": ["claude", "codex"],
    }
    monkeypatch.setattr(_config, "load_project_yaml", lambda: cfg)
    monkeypatch.setattr(backend_registry, "_probe_version", lambda path: "1.0.0")
    monkeypatch.setattr(
        backend_registry, "find_binaries",
        lambda tool, path_env=None: [Path(f"/fake/bin/{tool}")],
    )

    check = cli._check_backends(Path("/unused"))
    assert check.ok
    assert "claude" in check.detail and "codex" in check.detail


# ── doctor: model_ids (C7 part 2) ──
#
# `_check_model_limits` only ever checked that a ceiling-table *row* exists for a
# configured model — it never asked the backend's own CLI whether the model id itself is
# one it recognises, so a typo in project.yaml's `roles:` block passed `doctor` cleanly
# and only surfaced later as `claude -p --model <typo>` crashing an implementer mid-task.
# `_check_model_ids` closes that gap. No test here may execute a real agent CLI
# (`maestro/backends/registry.py`'s rule) — `_probe_model_id`'s `run` parameter is the
# injectable seam, exactly like `_probe_version`.

def test_probe_model_id_reports_invalid_when_the_cli_prints_its_own_rejection_marker():
    """The offline-safe signal this probe is built on: both real CLIs print a
    client-side "I don't know this model" marker before ever reaching the network
    (verified by hand, offline, while designing this check — see task-C7-report.md).
    Presence of that marker is the ONLY thing that marks a model invalid."""
    from maestro import cli

    def fake_run(argv, *, timeout):
        assert timeout == cli.MODEL_PROBE_TIMEOUT_SEC
        return "blah blah [claude-code:unrecognized_model] {...}"

    invalid, detail = cli._probe_model_id("claude", "totally-bogus", run=fake_run)
    assert invalid is True
    assert "totally-bogus" in detail


def test_probe_model_id_is_offline_tolerant_a_timeout_is_not_proof_of_an_invalid_model():
    """The other half of the contract: when the probe can prove nothing (here: a
    subprocess timeout, standing in for an unreachable network — see
    `_run_model_probe`'s docstring for why the real runner is bounded and always
    returns rather than raising), `doctor` must not fail the check on a guess."""
    from maestro import cli

    def fake_run(argv, *, timeout):
        return ""  # no marker seen -- inconclusive, e.g. offline before any output

    invalid, detail = cli._probe_model_id("claude", "claude-sonnet-5", run=fake_run)
    assert invalid is False


def test_probe_model_id_skips_cleanly_for_a_backend_with_no_probe_defined():
    from maestro import cli

    def _fail_if_called(argv, *, timeout):
        raise AssertionError("no probe is defined for this backend")

    invalid, detail = cli._probe_model_id("carrier-pigeon", "model-x", run=_fail_if_called)
    assert invalid is False
    assert "carrier-pigeon" in detail


def test_check_model_ids_fails_when_a_configured_model_is_rejected_by_its_own_cli(monkeypatch):
    from maestro import cli
    from maestro import config as _config

    cfg = {
        "roles": {
            "implementer": {"backend": "claude", "models": {"claude": "claude-sonnet-5-typo"}},
        },
    }
    monkeypatch.setattr(_config, "load_project_yaml", lambda: cfg)

    def fake_run(argv, *, timeout):
        return "[claude-code:unrecognized_model]"

    check = cli._check_model_ids(Path("/unused"), run=fake_run)
    assert not check.ok
    assert "claude-sonnet-5-typo" in check.detail


def test_check_model_ids_passes_and_probes_every_backend_model_pair(monkeypatch):
    from maestro import cli
    from maestro import config as _config

    cfg = {
        "roles": {
            "implementer": {"backend": "claude", "models": {"claude": "claude-sonnet-5"}},
            "judge": {"backend": "claude", "models": {"claude": "claude-sonnet-5"}},
            "diagnoser": {"backend": "codex", "models": {"codex": "gpt-5.6-sol"}},
        },
    }
    monkeypatch.setattr(_config, "load_project_yaml", lambda: cfg)

    seen = []

    def fake_run(argv, *, timeout):
        seen.append(tuple(argv))
        return ""

    check = cli._check_model_ids(Path("/unused"), run=fake_run)
    assert check.ok
    # implementer and judge share the same (claude, claude-sonnet-5) pair -- probed once.
    assert len(seen) == 2


def test_check_model_ids_passes_cleanly_with_no_models_declared(monkeypatch):
    from maestro import cli
    from maestro import config as _config

    def _fail_if_called(argv, *, timeout):
        raise AssertionError("nothing configured -- must not probe anything")

    monkeypatch.setattr(_config, "load_project_yaml", lambda: {})

    check = cli._check_model_ids(Path("/unused"), run=_fail_if_called)
    assert check.ok
    assert "nothing to probe" in check.detail


def test_run_model_probe_survives_a_timeout_without_raising():
    """The real subprocess seam, exercised directly (still no real CLI: the executable
    invoked is `sleep`, standing in for a hung `claude`/`codex` behind an unreachable
    network -- see the module docstring's offline finding)."""
    from maestro import cli

    output = cli._run_model_probe(["sleep", "5"], timeout=0.2)
    assert output == ""


def test_run_model_probe_captures_partial_output_before_a_timeout_kill():
    from maestro import cli

    output = cli._run_model_probe(
        ["bash", "-c", "echo marker-before-hang; sleep 5"], timeout=0.5
    )
    assert "marker-before-hang" in output


# ── status ──


def test_status_on_a_never_initialized_project_reports_missing_state(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    proc = _run_cli("status", "--repo", str(root), timeout=30)
    assert proc.returncode == 1
    assert "no state.json" in proc.stdout


def test_status_on_the_healthy_project_reports_idle(_healthy_project):
    root, _ = _healthy_project
    proc = _run_cli("status", "--repo", str(root), timeout=30)
    assert proc.returncode == 0
    assert "In-flight: 0" in proc.stdout


# ── ctl ──


def test_ctl_pause_resume_round_trip_leaves_state_as_found(_healthy_project):
    """`_healthy_project`'s own `init` call already leaves `paused_by_user: true` behind
    (its no-op supervised loop check's own return mechanism — see `maestro/cli.py`'s
    docstring), so this test starts by forcing a known `resume`d baseline rather than
    assuming one, then proves the round trip and ends resumed again either way."""
    root, _ = _healthy_project

    _run_cli("ctl", "--repo", str(root), "resume", timeout=30)
    before = _run_cli("status", "--repo", str(root), timeout=30)
    assert "Paused: False" in before.stdout

    paused = _run_cli("ctl", "--repo", str(root), "pause", timeout=30)
    assert paused.returncode == 0
    mid = _run_cli("status", "--repo", str(root), timeout=30)
    assert "Paused: True" in mid.stdout

    resumed = _run_cli("ctl", "--repo", str(root), "resume", timeout=30)
    assert resumed.returncode == 0
    after = _run_cli("status", "--repo", str(root), timeout=30)
    assert "Paused: False" in after.stdout


def test_ctl_unpark_reports_a_task_that_is_not_parked(_healthy_project):
    root, _ = _healthy_project
    proc = _run_cli("ctl", "--repo", str(root), "unpark", "NOPE", timeout=30)
    assert proc.returncode == 1
    assert "not parked" in proc.stdout


def test_ctl_backend_records_the_choice_in_state(_healthy_project):
    root, _ = _healthy_project
    proc = _run_cli("ctl", "--repo", str(root), "backend", "claude", timeout=30)
    assert proc.returncode == 0
    state = json.loads((root / ".orchestrator" / "state.json").read_text())
    assert state.get("backend") == "claude"


@pytest.mark.parametrize("verb,extra_args", [
    ("waiting", []),
    ("manual", []),
    ("detail", ["NOPE"]),
    ("approve", ["NOPE"]),
    ("reject", ["NOPE"]),
    ("fix", ["NOPE"]),
])
def test_ctl_dispatch_verbs_are_safe_no_ops_against_a_nonexistent_id(_healthy_project, verb, extra_args):
    """This test deliberately only exercises the "no such id" early-return path every one of
    these functions has, which is fully wired and safe.

    M4 found that the matching path crashed on unresolved `pending()` placeholders inside
    `maestro/hitl/commands.py`; M4a rebound those onto the real implementations and
    `tests/test_no_unresolved_pending.py` now guards them. The matching path stays out of
    this test because it mutates real task state, not because it is known-broken."""
    root, _ = _healthy_project
    proc = _run_cli("ctl", "--repo", str(root), verb, *extra_args, timeout=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "dispatched to hitl.commands." in proc.stdout


def test_ctl_unknown_verb_is_reported_not_crashed(_healthy_project):
    root, _ = _healthy_project
    proc = _run_cli("ctl", "--repo", str(root), "not-a-real-verb", timeout=30)
    assert proc.returncode == 2
    assert "unknown verb" in proc.stdout


# ── run / the no-op supervised loop mechanism ──


def test_run_on_an_empty_roadmap_completes_via_natural_return(_healthy_project):
    """The concrete mechanism (see `maestro/cli.py`'s module docstring for the full trace
    through `orchestrator.main()`): with `in_flight`, `runnable` and `waiting_on_dan` all
    empty, `main()`'s first loop iteration sets `paused_by_user = True` as its own return
    signal and sleeps one `POLL_INTERVAL`; the second iteration sees that flag at the top of
    the loop and breaks. No env var, no iteration cap — a natural early return, bounded by
    one real `POLL_INTERVAL` sleep (hence this test's cost).

    The three control flags are reset first (same set `_reset_control_flags_for_loop_check`
    clears — see its docstring for why) via plain JSON file I/O rather than by calling that
    function, or `maestro.state`, in this process: this test file's own docstring explains
    why every project-rooted extracted module must only ever be touched through a subprocess
    once `python3 -m pytest` (not just this one file) is running — some earlier-collected
    test module may already have imported `maestro.state` bound to a different root."""
    from maestro import cli

    root, _ = _healthy_project
    state_path = root / ".orchestrator" / "state.json"
    state = json.loads(state_path.read_text())
    for flag in ("paused_by_user", "halted", "proposal_test_notified"):
        state[flag] = False
    state_path.write_text(json.dumps(state))

    ok, detail = cli._run_noop_supervised_loop_check(root, timeout_s=90)
    assert ok, detail


def test_run_requires_the_shared_agents_tmux_session(tmp_path):
    """`orchestrator.main()` hard-requires a tmux session literally named `"agents"`
    (`maestro.worktree.TMUX_SESSION` — shared across every project, not derived per-project)
    and returns 1 immediately if it is absent, before the event loop even starts. This test
    proves that precondition is real (not an assumption this build's mechanism write-up
    invented) by killing the session, running `maestro run` against an otherwise-valid empty
    project, and observing the immediate failure.

    **Isolated on a private tmux server (`-L <socket>`), never the machine's default one.**
    Earlier this test used the bare `tmux` command, which targets the default server socket —
    the exact one that hosts any real, live "agents" session on this machine (e.g. a cutover
    project's production orchestrator loop, per `docs/DESIGN.md` §11). `self_test()`
    (`maestro/selfupdate.py`) runs this suite via plain `pytest` with no such isolation, so a
    live self-update's candidate test run executed this test directly against the real
    session — `tmux kill-session` SIGHUPs every pane in it, including whatever real
    orchestrator process was running inside, and does so *before* this test's own `finally:`
    restore could run (the pane running pytest itself is one of the ones hung up). Confirmed
    as the root cause of a live cutover-project crash loop on 2026-08-20 — see
    `docs/PROGRESS.md`.

    `TMUX_TMPDIR` was tried first and does **not** isolate the server on this machine's tmux
    build — verified empirically: a session created under a distinct `TMUX_TMPDIR` was still
    visible (and killable) via a plain, unqualified `tmux` call. `-L <socket>` is the
    documented, unambiguous way to select an alternate server and was verified to actually
    isolate (a `-L`-created session is invisible to the default-socket `tmux list-sessions`).
    Every `tmux` call below, and the `maestro run` subprocess's own internal check, must use
    the same `-L` socket so nothing here can touch, let alone kill, a real shared session —
    `main()`'s own `tmux has-session` check (`maestro/orchestrator.py`) takes no `-L` flag of
    its own, so the subprocess env cannot inject one; instead `PATH` is pointed at a tiny
    wrapper script that always inserts `-L <socket>` ahead of whatever tmux subcommand main()
    invokes."""
    socket = f"test-{os.getpid()}-{id(tmp_path)}"
    tmux = ["tmux", "-L", socket]
    had_session = subprocess.run(
        [*tmux, "has-session", "-t", "agents"], capture_output=True
    ).returncode == 0
    subprocess.run([*tmux, "kill-session", "-t", "agents"], capture_output=True)
    try:
        root = _git_repo(tmp_path / "proj")
        # `main()` shells out to a bare `tmux` with no `-L` of its own — give it one via a
        # PATH-shadowing wrapper rather than editing orchestrator.py for a test's sake. The
        # wrapper must `exec` the *absolute* path to the real binary: it runs under a PATH
        # that has the wrapper's own directory prepended (so `main()`'s bare `tmux` finds
        # it), and an unqualified `tmux` inside the wrapper would resolve back to itself
        # under that same PATH — infinite self-exec, reproduced empirically while writing
        # this fix.
        real_tmux = shutil.which("tmux")
        assert real_tmux, "tmux not on PATH — required for this test"
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        wrapper = fake_bin / "tmux"
        wrapper.write_text(f'#!/bin/sh\nexec {real_tmux} -L {socket} "$@"\n')
        wrapper.chmod(0o755)
        env = dict(os.environ)
        env["MAESTRO_REPO"] = str(root)
        env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
        proc = subprocess.run(
            [sys.executable, "-m", "maestro.cli", "run", "--repo", str(root)],
            cwd=str(root), env={**env, "PYTHONPATH": str(REPO_ROOT)},
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 1
        assert "agents" in (proc.stdout + proc.stderr)
    finally:
        if had_session:
            subprocess.run([*tmux, "new-session", "-d", "-s", "agents"], capture_output=True)
        subprocess.run([*tmux, "kill-server"], capture_output=True)


# ── install-skills (in-process: no Paths.from_env() dependency) ──


def test_install_skills_never_touches_the_real_home(tmp_path):
    """The overridable-home seam DESIGN.md's scope decision requires: this test's `home` is
    always an explicit `tmp_path`, so this build's own run never writes into the real
    `~/.claude/` or `~/.codex/`."""
    from maestro import cli

    rc = cli.cmd_install_skills(home=tmp_path)
    assert rc == 0

    claude_dest = tmp_path / ".claude" / "skills" / "maestro-setup" / "SKILL.md"
    codex_dest = tmp_path / ".codex" / "prompts" / "maestro-setup.md"
    assert claude_dest.is_file()
    assert codex_dest.is_file()
    assert claude_dest.read_text() == (REPO_ROOT / "maestro" / "skills" / "claude" / "SKILL.md").read_text()
    assert codex_dest.read_text() == (REPO_ROOT / "maestro" / "skills" / "codex" / "maestro-setup.md").read_text()


def test_install_skills_overwrites_on_reinstall(tmp_path):
    from maestro import cli

    cli.cmd_install_skills(home=tmp_path)
    claude_dest = tmp_path / ".claude" / "skills" / "maestro-setup" / "SKILL.md"
    claude_dest.write_text("stale content from a previous version")

    rc = cli.cmd_install_skills(home=tmp_path)
    assert rc == 0
    assert claude_dest.read_text() != "stale content from a previous version"


# ── watchdog ──
#
# M4b (docs/plans/2026-08-17-m4b-watchdog.md) turned `cmd_watchdog` from an honest stub that
# printed "not yet implemented" and exited 1 into a real wrapper around `maestro.watchdog.main()`.
# The two tests that pinned the stub's exit-1/message contract described behaviour that no longer
# exists and were replaced by the four below — argument wiring, exit-code passthrough, the real
# module's one bounded exit path, and the negative assertion that none of it spawns anything.


def _stub_watchdog_module(monkeypatch, rc: int) -> dict:
    """Put a recording stand-in at `sys.modules["maestro.watchdog"]` so `cmd_watchdog`'s
    function-body `from maestro.watchdog import main` resolves to it. Keeps the real module
    (and its import-time `Paths.from_env()` binding, and its `while True:` loop) out of this
    interpreter entirely — see this module's docstring."""
    import types

    calls: dict = {"count": 0, "repo_at_call": None}
    stub = types.ModuleType("maestro.watchdog")

    def main() -> int:
        calls["count"] += 1
        calls["repo_at_call"] = os.environ.get("MAESTRO_REPO")
        return rc

    stub.main = main
    monkeypatch.setitem(sys.modules, "maestro.watchdog", stub)
    return calls


def test_watchdog_calls_maestro_watchdog_main_with_repo_bound(tmp_path, monkeypatch):
    """The wrapper's whole job: resolve the repo, export `$MAESTRO_REPO` *before* the import,
    then call through. Same shape as `cmd_run`."""
    from maestro import cli

    monkeypatch.setenv("MAESTRO_REPO", "/nonexistent/should-be-overwritten")
    calls = _stub_watchdog_module(monkeypatch, rc=0)

    rc = cli.cmd_watchdog(tmp_path)

    assert rc == 0
    assert calls["count"] == 1
    assert calls["repo_at_call"] == str(tmp_path.resolve())
    assert os.environ["MAESTRO_REPO"] == str(tmp_path.resolve())


@pytest.mark.parametrize("rc", [0, 1, 2])
def test_watchdog_passes_the_loops_exit_code_straight_through(tmp_path, monkeypatch, rc):
    """`main()` returns 0 on the HALT sentinel and 1 on a stall-HALT; systemd and the launcher
    both read that, so the wrapper must not normalise it."""
    from maestro import cli

    monkeypatch.setenv("MAESTRO_REPO", "/nonexistent/should-be-overwritten")
    _stub_watchdog_module(monkeypatch, rc=rc)
    assert cli.cmd_watchdog(tmp_path) == rc


def test_watchdog_argv_accepts_repo_and_dispatches(tmp_path, monkeypatch):
    """`maestro watchdog --repo <path>` — the subcommand grew a `--repo` flag when it stopped
    being a no-argument stub, matching `run`/`status`/`ctl`."""
    from maestro import cli

    monkeypatch.setenv("MAESTRO_REPO", "/nonexistent/should-be-overwritten")
    calls = _stub_watchdog_module(monkeypatch, rc=0)

    assert cli.main(["watchdog", "--repo", str(tmp_path)]) == 0
    assert calls["repo_at_call"] == str(tmp_path.resolve())


def test_watchdog_help_advertises_a_real_supervisor():
    proc = _run_cli("watchdog", "--help", timeout=15)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    combined = (proc.stdout + proc.stderr).lower()
    assert "not yet implemented" not in combined
    assert "honest stub" not in combined
    assert "--repo" in combined


def _no_spawn_bin(tmp_path: Path) -> Path:
    """A PATH shim whose every entry records the fact it was called and then fails. Any real
    `tmux`/`pgrep`/`pkill`/`crontab`/`at`/`claude`/`codex` invocation from the watchdog under
    test therefore leaves evidence instead of touching this machine's tmux server, process
    table, crontab or a model API."""
    shim = tmp_path / "no-spawn-bin"
    shim.mkdir()
    for name in ("tmux", "pgrep", "pkill", "crontab", "at", "systemd-run", "claude", "codex"):
        p = shim / name
        p.write_text(f'#!/bin/sh\necho "{name} $*" >> "{shim}/CALLED"\nexit 1\n')
        p.chmod(0o755)
    return shim


def test_watchdog_returns_zero_on_the_halt_sentinel_without_spawning_anything(tmp_path):
    """The real `maestro/watchdog.py`, in a real subprocess, with a real project root — and the
    one bounded exit its `while True:` has. `main()` checks `.orchestrator/HALT` at the top of
    its first iteration, before `pgrep`, `tmux` or `crontab` can be reached, so this exercises
    the whole wrapper + import + loop-entry path in milliseconds and proves the stub's exit 1 is
    gone. The PATH shim is the guard that it really is the HALT path and not a supervisor that
    got as far as touching the machine."""
    repo = tmp_path / "proj"
    (repo / ".orchestrator").mkdir(parents=True)
    (repo / ".orchestrator" / "HALT").write_text("halted by the test\n")
    shim = _no_spawn_bin(tmp_path)

    proc = _run_cli(
        "watchdog", "--repo", str(repo), timeout=60,
        extra_env={"PATH": f"{shim}{os.pathsep}{os.environ.get('PATH', '')}"},
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "HALT sentinel present" in proc.stdout
    assert not (shim / "CALLED").exists(), (shim / "CALLED").read_text()
    journal = (repo / ".orchestrator" / "journal.ndjson").read_text()
    assert "watchdog_halt_respected" in journal


# ── pyproject.toml wiring ──


def test_pyproject_declares_the_maestro_console_script():
    import tomllib
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert data["project"]["scripts"]["maestro"] == "maestro.cli:main"
