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
paid once, not once per test that needs an already-initialized project. Every test that takes
`_healthy_project` is marked `@pytest.mark.slow` — the fixture's first use in a run pays the
~30s `init` setup regardless of which test triggers it, so a test added here without the marker
would silently make `pytest -m "not slow"` pay that cost again. `pytest -m "not slow"` is the
fast local-iteration tier; a bare `pytest` (no marker filter, what `selfupdate.self_test()` runs)
is the merge/adoption-boundary gate and always includes this module in full.
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

from tests.hang_bound import HANG_SECONDS

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
    # The CLI's self-check hang bounds (init's loop check, doctor's adapter check) are raised
    # so a loaded suite never decides the outcome; `timeout` below still bounds the call.
    env["MAESTRO_CHECK_TIMEOUT_S"] = "540"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "maestro.cli", *args],
        cwd=str(cwd) if cwd else None, env=env,
        # A hang bound only (no test here asserts a CLI timing out), so never below 600 s.
        capture_output=True, text=True, timeout=max(timeout, 600),
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


@pytest.mark.slow
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


@pytest.mark.slow
def test_init_registers_the_project_for_shared_model_policy(_healthy_project):
    """Future projects join the explicit registry at init (session-isolated models home)."""
    root, proc = _healthy_project
    registry = json.loads((Path(os.environ["MAESTRO_MODELS_HOME"]) / "projects.json").read_text())
    assert {"repo": str(root), "name": root.name} in registry["projects"]
    assert "shared model policy: registered" in proc.stdout


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
def test_doctor_healthy_project_passes_once_a_real_test_adapter_exists(_healthy_project):
    root, _ = _healthy_project
    tests_dir = root / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test_smoke.py").write_text("def test_ok():\n    assert True\n")
    adapter = root / "adapters" / "test"
    unwired_adapter = adapter.read_text(encoding="utf-8")
    command = json.dumps([str(root / ".venv" / "bin" / "python3"), "-m", "pytest", "-q"])
    wired_adapter = unwired_adapter.replace("TEST_COMMAND = []", f"TEST_COMMAND = {command}")
    assert wired_adapter != unwired_adapter
    adapter.write_text(wired_adapter, encoding="utf-8")
    try:
        # doctor runs the wired adapter (a real pytest) — ~25 s on a quiet host, past the
        # 60 s adapter-check default under a loaded suite (82 s measured in an I/O stall), so
        # the test raises the check's bound: a bound on a hang, not a speed assertion.
        proc = _run_cli("doctor", "--repo", str(root), "--pre-commit", timeout=600)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "[OK  ] adapter:test:" in proc.stdout
    finally:
        adapter.write_text(unwired_adapter, encoding="utf-8")
        # the adapter genuinely ran pytest inside tests_dir, which leaves __pycache__ /
        # .pytest_cache behind — rmtree, not a plain rmdir, to actually clean it up.
        import shutil
        shutil.rmtree(tests_dir)


def test_self_check_hang_bounds_keep_their_defaults_and_the_environment_may_only_raise_them(
        tmp_path, monkeypatch):
    """The doctor's adapter check (60 s) and init's loop check (120 s) bound a hang, never
    speed: `$MAESTRO_CHECK_TIMEOUT_S` raises them on a loaded host, never lowers them."""
    from maestro import adapters, cli

    seen = []
    monkeypatch.setattr(adapters, "run_adapter",
                        lambda kind, root, payload, timeout_s: seen.append(timeout_s)
                        or adapters.AdapterResult(kind=kind, status="absent"))
    loop = []
    monkeypatch.setattr(cli, "_ensure_agents_tmux_session", lambda **k: True)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, timeout, **k: loop.append(timeout)
                        or subprocess.CompletedProcess(a, 0, "", ""))
    for value in (None, "540", "10", "junk"):
        if value is None:
            monkeypatch.delenv("MAESTRO_CHECK_TIMEOUT_S", raising=False)
        else:
            monkeypatch.setenv("MAESTRO_CHECK_TIMEOUT_S", value)
        cli._check_adapters(tmp_path)
        cli._run_noop_supervised_loop_check(tmp_path)
    n = len(adapters.KNOWN_KINDS)
    assert seen == [60] * n + [540] * n + [60] * n + [60] * n
    assert loop == [120, 540, 120, 120]


@pytest.mark.slow
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


def test_doctor_help_advertises_the_third_party_gate():
    """The integration gate (DESIGN.md §15) is only usable if it is discoverable."""
    proc = _run_cli("doctor", "--help", timeout=30)
    assert "--thirdparty" in proc.stdout


@pytest.mark.slow
def test_doctor_thirdparty_runs_only_the_gate_and_needs_no_project(tmp_path):
    """The gate asks about *installed software*, not about a project.

    Deliberately pointed at an empty directory: an implementing agent runs this after
    touching a backend adapter or `pyproject.toml`, and it must not be entangled with
    whether some project has a ROADMAP.md.
    """
    root = tmp_path / "not-a-project"
    root.mkdir()
    proc = _run_cli("doctor", "--repo", str(root), "--thirdparty", timeout=60)
    assert "thirdparty:" in proc.stdout
    assert "docs:" not in proc.stdout
    assert "adapter:test:" not in proc.stdout
    # The exit code tracks whether the installed CLIs match maestro/thirdparty.json, which
    # moves whenever a CLI auto-updates; the drift failure itself is pinned in-process by
    # test_doctor_thirdparty_fails_when_an_installed_version_has_drifted.
    expected = 1 if "[FAIL] thirdparty" in proc.stdout else 0
    assert proc.returncode == expected, proc.stdout


def test_doctor_refuses_to_run_both_subsets_at_once():
    proc = _run_cli("doctor", "--pre-commit", "--thirdparty", timeout=30)
    assert proc.returncode != 0
    assert "pick one" in (proc.stderr + proc.stdout)


def test_doctor_thirdparty_fails_when_an_installed_version_has_drifted(monkeypatch, capsys):
    """The behaviour the whole gate exists for, exercised in-process with a fake host.

    `_check_thirdparty` must turn a drifted subject into a *required* failure under
    `--thirdparty`, so an implementing agent cannot mark an integration complete against a
    version nobody verified.
    """
    from maestro import cli, thirdparty

    real_audit = thirdparty.audit
    monkeypatch.setattr(
        thirdparty, "audit",
        lambda **kw: real_audit(binary_version=lambda name: "9.9.9-nobody-verified-this",
                                distribution_version=thirdparty.installed_distribution_version,
                                binary_help=lambda name: "no recorded controls here"),
    )
    check = cli._check_thirdparty(required=True)

    assert not check.ok and check.required
    assert "re-verify" in check.detail


def test_doctor_thirdparty_passes_an_auto_updated_backend_whose_controls_are_unchanged(
    monkeypatch,
):
    """A harness auto-update that kept every recorded control passes, and says so."""
    from maestro import cli, thirdparty

    register = thirdparty.load_register()
    real_audit = thirdparty.audit
    monkeypatch.setattr(
        thirdparty, "audit",
        lambda **kw: real_audit(
            binary_version=lambda name: register[name].verified_version + "-bumped",
            distribution_version=thirdparty.installed_distribution_version,
            binary_help=lambda name: " ".join(register[name].findings["controls_observed"]),
        ),
    )
    check = cli._check_thirdparty(required=True)

    assert check.ok, check.detail
    assert "reobserved" in check.detail


def test_doctor_broken_project_reports_missing_docs_and_missing_required_adapter(tmp_path):
    root = tmp_path / "broken"
    root.mkdir()
    proc = _run_cli("doctor", "--repo", str(root), timeout=60)
    assert proc.returncode == 1
    assert "[FAIL] docs:" in proc.stdout
    assert "[FAIL] adapter:test:" in proc.stdout
    assert "status=absent" in proc.stdout


def test_doctor_telegram_check_never_touches_the_real_network(tmp_path, monkeypatch):
    """`cmd_doctor`'s `http_get` parameter is the injectable seam DESIGN.md's scope decision
    requires. Called in-process here (safe: `_check_telegram` has no `Paths.from_env()`
    dependency of its own) with a stub that would fail the test if it were ever the real
    `urllib`-backed default — proving the injection actually replaces it."""
    from maestro import cli

    root = tmp_path / "proj"
    root.mkdir()
    (root / ".env").write_text("TELEGRAM_BOT_TOKEN=fake-test-token\n")
    monkeypatch.setenv('MAESTRO_TELEGRAM_ENV', str(root / '.env'))

    calls = []

    def _stub_http_get(url):
        calls.append(url)
        return {"ok": True, "result": {"username": "fake_test_bot"}}

    check = cli._check_telegram(root, _stub_http_get)
    assert check.ok
    assert calls == ["https://api.telegram.org/botfake-test-token/getMe"]


def test_doctor_reports_when_the_running_code_is_not_the_adopted_code(tmp_path, monkeypatch):
    """The visibility half of the adopted-code fix.

    For as long as `~/.maestro/current` was written and never read, every doctor check could
    pass while the "deployed" version was a fiction and a working tree was what actually ran.
    This check is therefore about *this process* — where maestro was imported from — not about
    whether the pointer file looks tidy.
    """
    from maestro import bootstrap, cli

    home = tmp_path / "maestro_home"
    home.mkdir()
    monkeypatch.setenv(bootstrap.ENV_VAR, str(home))
    root = tmp_path / "proj"
    root.mkdir()

    # Never self-updated: nothing to diverge from, so running what you installed is correct.
    assert cli._check_adopted_code(root).ok

    # A pointer at a real, different checkout: the running code is not the adopted code.
    other = tmp_path / "other"
    (other / "maestro").mkdir(parents=True)
    (other / "maestro" / "__init__.py").write_text("", encoding="utf-8")
    (home / "current").write_text(str(other), encoding="utf-8")
    stale = cli._check_adopted_code(root)
    assert not stale.ok
    assert str(other.resolve()) in stale.detail

    # A pointer that no longer resolves is also actionable, and distinctly worded.
    (home / "current").write_text(str(tmp_path / "pruned"), encoding="utf-8")
    pruned = cli._check_adopted_code(root)
    assert not pruned.ok
    assert "does not name a maestro checkout" in pruned.detail

    # And the healthy case: the pointer names the checkout this process is running.
    (home / "current").write_text(str(bootstrap.running_root()), encoding="utf-8")
    assert cli._check_adopted_code(root).ok


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


@pytest.mark.parametrize("cfg", [{}, {"routing": {"harness_order": ["claude", "codex"]}}])
def test_check_backends_probes_every_harness_in_the_order(cfg, monkeypatch, _clean_backend_binary_cache):
    """A role names no backend (DESIGN.md §17.2): every harness in `routing.harness_order`,
    or in the default chain when the key is absent, must have a binary (C5/G8). A machine
    with no `codex` binary used to pass `doctor` cleanly and the gap only surfaced later as
    a backend switch that silently didn't happen.

    `_probe_version` and `find_binaries` are stubbed rather than left to scan the real
    `PATH` — per `maestro/backends/registry.py`'s docstring, no test may execute a real
    agent CLI, and this one is about binary probing specifically."""
    from maestro import cli
    from maestro import config as _config
    from maestro.backends import registry as backend_registry

    monkeypatch.setattr(_config, "load_project_yaml", lambda: cfg)
    monkeypatch.setattr(backend_registry, "_probe_version", lambda path: "1.0.0")
    monkeypatch.setattr(
        backend_registry, "find_binaries",
        lambda tool, path_env=None: [Path(f"/fake/bin/{tool}")] if tool == "claude" else [],
    )

    check = cli._check_backends(Path("/unused"))
    assert not check.ok
    assert "codex" in check.detail


def test_check_backends_checks_all_targets_even_with_a_retired_order(monkeypatch, _clean_backend_binary_cache):
    """The retired `fallback_chain` and a role's `backend` name nothing any more: with
    `harness_order: [claude]` a missing `codex` binary is not a failure."""
    from maestro import cli
    from maestro import config as _config
    from maestro.backends import registry as backend_registry

    cfg = {
        "roles": {"implementer": {"backend": "codex"}},
        "fallback_chain": ["claude", "codex"],
        "routing": {"harness_order": ["claude"]},
    }
    monkeypatch.setattr(_config, "load_project_yaml", lambda: cfg)
    monkeypatch.setattr(backend_registry, "_probe_version", lambda path: "1.0.0")
    monkeypatch.setattr(
        backend_registry, "find_binaries",
        lambda tool, path_env=None: [Path(f"/fake/bin/{tool}")] if tool == "claude" else [],
    )

    check = cli._check_backends(Path("/unused"))
    assert not check.ok and 'codex' in check.detail
    assert check.detail == "binary not found on PATH for: codex"


def test_check_backends_passes_when_every_chain_backend_is_present(monkeypatch, _clean_backend_binary_cache):
    """The companion green path: once `codex` is also on `PATH`, the default chain passes
    cleanly."""
    from maestro import cli
    from maestro import config as _config
    from maestro.backends import registry as backend_registry

    monkeypatch.setattr(_config, "load_project_yaml", lambda: {})
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
# one it recognises, so a bad id passed `doctor` cleanly and only surfaced later as
# `claude -p --model <id>` crashing an implementer mid-task. `_check_model_ids` closes
# that gap. A role states no model now (DESIGN.md §17.2), so it probes the derived pairs
# (`model_policy.runtime_roles`). No test here may execute a real agent CLI
# (`maestro/backends/registry.py`'s rule) — `_probe_model_id`'s `run` parameter is the
# injectable seam, exactly like `_probe_version`.


def _derived_models(monkeypatch, **role_models):
    """Stand in for the derivation: each role runs the given `{backend: model}` table."""
    from maestro import model_policy

    derived = {"roles": {role: {"models": models} for role, models in role_models.items()}}
    monkeypatch.setattr(model_policy, "runtime_roles", lambda project, repo: derived)


def test_probe_model_id_reports_invalid_when_the_cli_prints_its_own_rejection_marker():
    """The offline-safe signal this probe is built on: both real CLIs print a
    client-side "I don't know this model" marker before ever reaching the network
    (verified by hand, offline, while designing this check — see
    `docs/plans/2026-08-30-pre-integration-c7-report.md`).
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


def test_check_model_ids_fails_when_a_role_model_is_rejected_by_its_own_cli(monkeypatch):
    from maestro import cli

    _derived_models(monkeypatch, implementer={"claude": "claude-sonnet-5-typo"})

    def fake_run(argv, *, timeout):
        return "[claude-code:unrecognized_model]"

    check = cli._check_model_ids(Path("/unused"), run=fake_run)
    assert not check.ok
    assert "claude-sonnet-5-typo" in check.detail


def test_check_model_ids_passes_and_probes_every_backend_model_pair(monkeypatch):
    from maestro import cli

    _derived_models(monkeypatch,
                    implementer={"claude": "claude-sonnet-5"},
                    judge={"claude": "claude-sonnet-5"},
                    diagnoser={"codex": "gpt-5.6-sol"})

    seen = []

    def fake_run(argv, *, timeout):
        seen.append(tuple(argv))
        return ""

    check = cli._check_model_ids(Path("/unused"), run=fake_run)
    assert check.ok
    # implementer and judge share the same (claude, claude-sonnet-5) pair -- probed once.
    assert len(seen) == 2


def test_check_model_ids_probes_the_models_the_roles_derive(monkeypatch, tmp_path):
    """No stand-in here: an unconfigured project derives its models from the shipped
    policy, and the retired `models:` map of a role names nothing to probe."""
    from maestro import cli
    from maestro import config as _config

    cfg = {"roles": {"implementer": {"backend": "claude", "models": {"claude": "a-typo"}}}}
    monkeypatch.setattr(_config, "load_project_yaml", lambda: cfg)
    seen = []

    def fake_run(argv, *, timeout):
        seen.append(argv[-1] if "a-typo" in argv else " ".join(argv))
        return ""

    check = cli._check_model_ids(tmp_path, run=fake_run)
    assert check.ok
    assert "a-typo" not in " ".join(seen)
    for model in ("claude-opus-5-5", "gpt-6.1-sol"):
        assert any(model in argv for argv in seen)
    # agy has no probe, so its derived model is left out instead of noted on every run.
    assert "note(s)" not in check.detail


def test_check_model_ids_passes_cleanly_when_no_role_has_a_probed_model(monkeypatch):
    from maestro import cli

    def _fail_if_called(argv, *, timeout):
        raise AssertionError("nothing to probe -- must not probe anything")

    _derived_models(monkeypatch, implementer={"antigravity": "gemini-3.8-flash-high"}, judge={})

    check = cli._check_model_ids(Path("/unused"), run=_fail_if_called)
    assert check.ok
    assert "nothing to probe" in check.detail


# ── review round 1 (2026-09-01): aggregate probe budget + honest confirmation wording ──
#
# The reviewer hand-traced the real template roster (`templates/project.yaml.tmpl`):
# implementer/judge/diagnoser's `models:` maps carry FOUR distinct (backend, model) pairs,
# not two -- (claude, claude-sonnet-5), (claude, claude-opus-5), (codex, gpt-5.6-terra),
# (codex, gpt-5.6-sol) -- so a fully offline `doctor` run used to cost 4 * 8s = 32s with no
# ceiling as more roles/backends are added. `MODEL_PROBE_BUDGET_SEC` bounds the aggregate;
# these tests pin that a fake `run` that actually consumes wall-clock time (via `time.sleep`)
# stops being called once the budget is spent, and that a clean run never claims a model
# is "confirmed" -- this probe can only ever prove a model id is *bad*.

def test_check_model_ids_stops_probing_once_the_aggregate_time_budget_is_exhausted(monkeypatch):
    from maestro import cli
    import time as _time

    _derived_models(monkeypatch, implementer={"claude": "model-a"},
                    judge={"claude": "model-b"}, diagnoser={"codex": "model-c"})
    monkeypatch.setattr(cli, "MODEL_PROBE_BUDGET_SEC", 0.05)

    calls = []

    def fake_run(argv, *, timeout):
        calls.append(tuple(argv))
        _time.sleep(0.03)
        return ""

    check = cli._check_model_ids(Path("/unused"), run=fake_run)
    assert check.ok
    # 0.03s/call against a 0.05s budget: the 1st call always runs (elapsed 0 < 0.05), the
    # 2nd still starts (elapsed 0.03 < 0.05), the 3rd must not (elapsed 0.06 >= 0.05) --
    # bounded regardless of how many pairs are configured, not merely "fewer than before".
    assert len(calls) == 2
    assert "budget" in check.detail


def test_check_model_ids_never_claims_a_clean_result_confirms_a_model_works(monkeypatch):
    """The minor the reviewer folded in: `"probed N model id(s)"` read as a claim of N
    confirmations, but a marker-less result only ever means "this CLI did not (yet) say
    it's bad" -- it is never proof the model works. The wording must say so."""
    from maestro import cli

    _derived_models(monkeypatch, implementer={"claude": "claude-sonnet-5"})

    check = cli._check_model_ids(Path("/unused"), run=lambda argv, *, timeout: "")
    assert check.ok
    assert "confirmed invalid" in check.detail
    assert "probed 1 model id(s)" not in check.detail


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


@pytest.mark.slow
def test_status_on_the_healthy_project_reports_idle(_healthy_project):
    root, _ = _healthy_project
    proc = _run_cli("status", "--repo", str(root), timeout=30)
    assert proc.returncode == 0
    assert "In-flight: 0" in proc.stdout


# ── ctl ──


@pytest.mark.slow
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


@pytest.mark.slow
def test_ctl_unpark_reports_a_task_that_is_not_parked(_healthy_project):
    root, _ = _healthy_project
    proc = _run_cli("ctl", "--repo", str(root), "unpark", "NOPE", timeout=30)
    assert proc.returncode == 1
    assert "not parked" in proc.stdout


@pytest.mark.slow
def test_ctl_backend_records_the_choice_in_state(_healthy_project):
    root, _ = _healthy_project
    proc = _run_cli("ctl", "--repo", str(root), "backend", "claude", timeout=30)
    assert proc.returncode == 0
    state = json.loads((root / ".orchestrator" / "state.json").read_text())
    assert state.get("backend") == "claude"


@pytest.mark.slow
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


@pytest.fixture
def _project_with_a_live_controller(tmp_path):
    """A project whose control DB is owned by another live process, as while `maestro run`
    is going. The owner is a `sleep`, so nothing here ever runs a real loop."""
    root = tmp_path / "proj"
    (root / ".orchestrator").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    state = {"paused_by_user": False, "hitl_mode": False, "parked_tasks": ["T1"]}
    (root / ".orchestrator" / "state.json").write_text(json.dumps(state))
    owner = subprocess.Popen(["sleep", "120"])
    subprocess.run(
        [sys.executable, "-c",
         "import sys; from pathlib import Path; from maestro.control.store import ControlStore;"
         "s = ControlStore.open_controller(Path(sys.argv[1]), pid=int(sys.argv[2])); s.conn.close()",
         str(root / ".orchestrator" / "control.sqlite3"), str(owner.pid)],
        check=True, cwd=str(REPO_ROOT),
    )
    yield root
    owner.kill()
    owner.wait()


def test_ctl_halt_stops_a_running_loop_instead_of_crashing(_project_with_a_live_controller):
    """P12 pilot finding D5: `ctl halt` is the documented clean stop, but its journal line
    tried to take the controller lock the running loop holds, so it died with a traceback
    (the HALT file happened to be written first). An external actor journals the text
    line only, as the watchdog does."""
    root = _project_with_a_live_controller
    proc = _run_cli("ctl", "--repo", str(root), "halt", timeout=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (root / ".orchestrator" / "HALT").exists()
    lines = (root / ".orchestrator" / "journal.ndjson").read_text().splitlines()
    assert json.loads(lines[-1])["event"] == "control_halt"


@pytest.mark.parametrize("verb,extra", [("pause", []), ("resume", []), ("hitl", ["on"]),
                                        ("unpark", ["T1"]), ("remerge", ["T1"])])
def test_ctl_state_verbs_refuse_cleanly_while_a_loop_runs(_project_with_a_live_controller,
                                                          verb, extra):
    """The running controller is the only writer of the state document (INV-01), so these
    verbs cannot apply from outside it. They refuse with a pointer, not a traceback, and
    change nothing."""
    root = _project_with_a_live_controller
    before = (root / ".orchestrator" / "state.json").read_text()
    proc = _run_cli("ctl", "--repo", str(root), verb, *extra, timeout=30)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stderr
    assert "loop is running" in proc.stdout
    assert (root / ".orchestrator" / "state.json").read_text() == before


@pytest.fixture
def _project_with_a_failed_merge(tmp_path):
    """A stopped project whose one graph run failed at merge after an approving review."""
    from maestro.control.lifecycle import Lifecycle
    from maestro.control.store import ControlStore
    from maestro.taskgraph import normalize_spec
    from maestro.workflows import runner
    from maestro.workflows.compiler import compile_workflow

    root = tmp_path / "proj"
    (root / ".orchestrator").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".orchestrator" / "state.json").write_text(json.dumps({"parked_tasks": []}))
    spec = normalize_spec({"id": "t-01", "title": "t", "status": "pending", "deps": []})
    revision = compile_workflow(spec, workflow_id="wf_t-01", code_base_sha="d", escalation=True)
    with ControlStore.open_controller(root / ".orchestrator" / "control.sqlite3") as store:
        runner.start_run(store, revision, repo=root, run_id="run_old")
        life = Lifecycle(store)
        for node_id, phase in (("implementer", "succeeded"), ("gate", "succeeded"),
                               ("proof_review", "succeeded"), ("merge", "failed")):
            attempt = f"att_{node_id}_01"
            life.claim_attempt(attempt, run_id="run_old", node_id=node_id, lease_epoch=1)
            for step in ("ready", "claimed", "running", phase):
                life.set_node_phase("run_old", node_id, step, attempt_id=attempt,
                                    attempt_index=1)
            life.finish_attempt(attempt, run_id="run_old", status=phase)
        life.finish_run("t-01", "run_old", "failed")
    return root


def test_ctl_remerge_opens_the_successor_run_of_a_failed_merge(_project_with_a_failed_merge):
    root = _project_with_a_failed_merge
    proc = _run_cli("ctl", "--repo", str(root), "remerge", "t-01", timeout=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "t-01 remerge: run_old (failed)" in proc.stdout
    assert "merge, accept" in proc.stdout
    import sqlite3

    conn = sqlite3.connect(root / ".orchestrator" / "control.sqlite3")
    runs = conn.execute("SELECT run_id, status, workflow_revision FROM task_runs ORDER BY rowid")
    runs = runs.fetchall()
    assert [(r[1], r[2]) for r in runs] == [("failed", 1), ("running", 2)]
    # The lock is released on exit, so the loop can start afterwards.
    assert conn.execute("SELECT COUNT(*) FROM writer_leases WHERE resource_id = 'controller'"
                        ).fetchone()[0] == 0
    conn.close()
    lines = (root / ".orchestrator" / "journal.ndjson").read_text().splitlines()
    assert json.loads(lines[-1])["event"] == "graph_run_remerge"
    # Idempotent in effect: a second remerge finds the successor running and refuses.
    again = _run_cli("ctl", "--repo", str(root), "remerge", "t-01", timeout=30)
    assert again.returncode == 1 and "remerge refused" in again.stdout


def test_ctl_remerge_refuses_a_task_with_no_run(_project_with_a_failed_merge):
    proc = _run_cli("ctl", "--repo", str(_project_with_a_failed_merge), "remerge", "nope",
                    timeout=30)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "remerge refused: nope has no graph run" in proc.stdout
    assert "Traceback" not in proc.stderr


@pytest.mark.slow
def test_ctl_unknown_verb_is_reported_not_crashed(_healthy_project):
    root, _ = _healthy_project
    proc = _run_cli("ctl", "--repo", str(root), "not-a-real-verb", timeout=30)
    assert proc.returncode == 2
    assert "unknown verb" in proc.stdout


# ── run / the no-op supervised loop mechanism ──


@pytest.mark.slow
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
            capture_output=True, text=True, timeout=HANG_SECONDS,
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


def test_doctor_model_limits_scopes_each_model_to_its_backends_table(monkeypatch, tmp_path):
    """A role's derived table maps backend -> model; an agy model is looked up as `agy/...`."""
    from maestro import cli
    from maestro import limits

    _derived_models(monkeypatch, implementer={
        "claude": "claude-sonnet-5", "antigravity": "gemini-3.8-flash-high"})
    asked = []
    monkeypatch.setattr(limits, "resolve_all", lambda names: asked.extend(names) or {n: object() for n in names})
    assert cli._check_model_limits(tmp_path).ok
    assert sorted(asked) == ["agy/gemini-3.8-flash-high", "claude-sonnet-5"]


# ── init/doctor: P00 DuetFlow rehearsal defects (P12A W12, F2–F5) ──
#
# In-process and fast on purpose: each defect lives in one helper, and the ~60 s `init`
# subprocess would only re-prove the wiring `_healthy_project` already covers.


def test_derived_test_command_stays_inside_the_virtualenv(tmp_path):
    """F2: `.venv/bin/python3` is a symlink to the base interpreter; resolving it ran the
    gate outside the venv."""
    from maestro import cli

    root = _git_repo(tmp_path / "proj")
    (root / "pyproject.toml").write_text('[project]\nname = "p"\ndependencies = ["pytest"]\n')
    (root / "tests").mkdir()
    venv_python = root / ".venv" / "bin" / "python3"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(sys.executable)

    argv = cli._derive_repo_facts(root)["test_command_argv"]

    assert argv == [str(root.resolve() / ".venv" / "bin" / "python3"), "-m", "pytest", "-q"]


def test_existing_state_json_is_runtime_state_not_a_scaffold(tmp_path):
    """F3: a state.json written by an earlier init's loop check (or a live controller) must
    not come back as a `state.json.new` holding `{}`."""
    from maestro import cli

    root = tmp_path / "proj"
    assert [o.action for o in cli._ensure_orchestrator_dirs(root)] == ["created"]
    state_json = root / ".orchestrator" / "state.json"
    state_json.write_text('{"in_flight": [], "phase": {"status": "idle"}}\n')

    assert [o.action for o in cli._ensure_orchestrator_dirs(root)] == ["unchanged"]
    assert not (root / ".orchestrator" / "state.json.new").exists()
    assert state_json.read_text() == '{"in_flight": [], "phase": {"status": "idle"}}\n'


def test_gitignore_proposal_only_appends_the_missing_lines(tmp_path):
    """F4: against the rehearsal's 56-line `.gitignore`, the proposal keeps every existing
    line and its diff has no deletions."""
    from maestro import cli

    root = tmp_path / "proj"
    root.mkdir()
    existing = [f"secret-{i}.token" for i in range(51)] + [
        ".venv/", "config.yaml", "tokens/", "*.db", "data/",
    ]
    assert len(existing) == 56
    gitignore = root / ".gitignore"
    gitignore.write_text("\n".join(existing) + "\n")

    outcome = cli._scaffold_gitignore(gitignore)

    assert outcome.action == "new_written"
    assert gitignore.read_text() == "\n".join(existing) + "\n"
    proposal = (root / ".gitignore.new").read_text().splitlines()
    assert proposal[:56] == existing
    assert ".orchestrator/" in proposal
    assert proposal.count(".venv/") == 1, "an entry the file already has is not repeated"
    changed = [
        line for line in outcome.diff.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]
    assert changed and all(line.startswith("+") for line in changed)

    gitignore.write_text((root / ".gitignore.new").read_text())
    (root / ".gitignore.new").unlink()
    assert cli._scaffold_gitignore(gitignore).action == "unchanged"
    assert not (root / ".gitignore.new").exists()


def test_preamble_deny_list_renders_from_project_yaml_or_doctor_fails(tmp_path, monkeypatch, capsys):
    """F5: init renders the configured list; while the placeholder outlives a configured
    list, doctor exits non-zero. An empty list keeps the placeholder and passes."""
    from maestro import cli

    root = tmp_path / "proj"
    root.mkdir()
    preamble = root / "operating_preamble.md"
    template = "## The deny list\n\n```\n{{ deny_list }}\n```\n"

    (root / "project.yaml").write_text("risky_set: []\ndeny_list_extra: []\n")
    assert cli._deny_list_block(root) == cli._DENY_LIST_PLACEHOLDER
    preamble.write_text(cli._render(template, {"deny_list": cli._deny_list_block(root)}))
    assert cli._check_preamble_deny_list(root).ok

    (root / "project.yaml").write_text(
        "risky_set:\n  - maestro/merge.py\ndeny_list_extra:\n  - 'rm\\s+-rf'\n"
    )
    failing = cli._check_preamble_deny_list(root)
    assert not failing.ok and failing.required

    ok = cli.Check("stub", True, "stub", required=True)
    for name in ("_check_docs", "_check_model_limits", "_check_model_ids", "_check_backends",
                 "_check_mermaid", "_check_telegram", "_check_systemd_unit", "_check_gate",
                 "_check_adopted_code", "_check_journal_and_version", "_check_thirdparty",
                 "_check_meta_branch"):
        monkeypatch.setattr(cli, name, lambda *a, **k: ok)
    monkeypatch.setattr(cli, "_check_adapters", lambda *a, **k: [ok])
    monkeypatch.setenv("MAESTRO_REPO", str(root))
    no_network = lambda url: pytest.fail(f"doctor reached the network: {url}")

    assert cli.cmd_doctor(root, http_get=no_network) == 1
    assert "[FAIL] preamble_deny_list:" in capsys.readouterr().out

    rendered = cli._render(template, {"deny_list": cli._deny_list_block(root)})
    assert "maestro/merge.py\nrm\\s+-rf" in rendered
    assert cli._DENY_LIST_PLACEHOLDER not in rendered
    preamble.write_text(rendered)
    assert cli.cmd_doctor(root, http_get=no_network) == 0


# ── P12A W11: `maestro workflow runs` ──


def test_workflow_runs_lists_both_runs_of_a_repaired_task_in_start_order(tmp_path):
    """Two `start_run`s in the same second: insertion order, not the run id, decides."""
    from maestro.control.lifecycle import Lifecycle
    from maestro.control.store import ControlStore

    repo = _git_repo(tmp_path / "repo")
    (repo / ".orchestrator").mkdir()
    store = ControlStore.open_controller(repo / ".orchestrator" / "control.sqlite3")
    try:
        store.register_task("T-9", spec_revision=1)
        life = Lifecycle(store)
        stamp = "2026-09-16T10:00:00Z"
        life.start_run("T-9", "run_z_first", workflow_id="wf_T9", workflow_revision=1, at=stamp)
        life.finish_run("T-9", "run_z_first", "failed", at=stamp)
        life.start_run("T-9", "run_a_repaired", workflow_id="wf_T9", workflow_revision=2,
                       at=stamp)
    finally:
        store.close()

    text = _run_cli("workflow", "--repo", str(repo), "runs", "T-9")
    assert text.returncode == 0, text.stderr
    assert text.stdout.index("run_z_first") < text.stdout.index("run_a_repaired")

    as_json = _run_cli("workflow", "--repo", str(repo), "runs", "T-9", "--json")
    assert as_json.returncode == 0, as_json.stderr
    rows = json.loads(as_json.stdout)
    assert [(r["run_id"], r["workflow_revision"], r["status"]) for r in rows] == [
        ("run_z_first", 1, "failed"), ("run_a_repaired", 2, "running"),
    ]


# ── P12 B15: the retention default ──


def test_retention_defaults_to_keep_everything_and_refuses_what_it_cannot_read():
    from maestro import config

    assert config.retention({}) == {"prune": False, "warn_bytes": 1 << 30}
    assert config.retention({"retention": {"warn_bytes": 5}}) == {"prune": False, "warn_bytes": 5}
    for bad in ({"retention": []}, {"retention": {"warn_bytes": -1}},
                {"retention": {"warn_bytes": True}}, {"retention": {"prune": "yes"}},
                {"retention": {"prune": True}}):
        with pytest.raises(config.ConfigError):
            config.retention(bad)


def test_doctor_warns_when_the_orchestrator_dir_outgrows_the_retention_threshold(
    tmp_path, monkeypatch
):
    from maestro import cli

    root = tmp_path / "proj"
    orch = root / ".orchestrator"
    (orch / "attempts" / "a1").mkdir(parents=True)
    (orch / "attempts" / "a1" / "result.json").write_bytes(b"x" * 64)
    # A released task tree is not evidence and is not counted.
    (orch / "worktrees" / "T-1").mkdir(parents=True)
    (orch / "worktrees" / "T-1" / "big.bin").write_bytes(b"x" * 4096)
    monkeypatch.setenv("MAESTRO_REPO", str(root))

    (root / "project.yaml").write_text("retention: {warn_bytes: 1000}\n")
    under = cli._check_retention(root)
    assert under.ok and not under.required and "64 bytes" in under.detail

    (root / "project.yaml").write_text("retention: {warn_bytes: 10}\n")
    over = cli._check_retention(root)
    assert not over.ok and not over.required
    assert "64 bytes" in over.detail and "warn_bytes=10" in over.detail


# P12 pilot finding (session 3f): the watchdog launches `maestro run` with `tmux new-window`,
# which inherits the tmux *server's* environment, not the watchdog's. `launch.sh` sourcing
# `.env` therefore never reached the loop: every Telegram notice was silently skipped while the
# journal said `graph_run_failed_notified`. Both entry points now load the project's `.env`
# themselves, as the reference `orchestrator_run.py` did (`load_dotenv(..., override=True)`).


def _stub_module(monkeypatch, name: str) -> dict:
    import types

    seen: dict = {}
    stub = types.ModuleType(name)

    def main() -> int:
        seen["token"] = os.environ.get("TELEGRAM_BOT_TOKEN")
        seen["chat"] = os.environ.get("TELEGRAM_ALERT_CHAT_ID")
        return 0

    stub.main = main
    monkeypatch.setitem(sys.modules, name, stub)
    return seen


@pytest.mark.parametrize("command,module", [
    ("cmd_run", "maestro.orchestrator"),
    ("cmd_watchdog", "maestro.watchdog"),
])
def test_entry_points_load_the_project_env_over_the_inherited_one(
    tmp_path, monkeypatch, command, module
):
    from maestro import cli

    # A stale value from the tmux server (another project's bot) must not win.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "server-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "server-chat")
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=project-token\nTELEGRAM_ALERT_CHAT_ID=42\n")
    shared = tmp_path / 'shared.env'
    shared.write_text('TELEGRAM_BOT_TOKEN=shared-token\nTELEGRAM_ALERT_CHAT_ID=42\n')
    monkeypatch.setenv('MAESTRO_TELEGRAM_ENV', str(shared))
    monkeypatch.setenv('MAESTRO_HOME', str(tmp_path / 'shared-home'))
    seen = _stub_module(monkeypatch, module)

    assert getattr(cli, command)(tmp_path) == 0
    assert seen == {"token": "shared-token", "chat": "42"}


@pytest.mark.parametrize("command,module", [
    ("cmd_run", "maestro.orchestrator"),
    ("cmd_watchdog", "maestro.watchdog"),
])
def test_entry_points_without_shared_config_clear_inherited_project_credentials(
    tmp_path, monkeypatch, command, module
):
    from maestro import cli

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "inherited")
    monkeypatch.delenv("TELEGRAM_ALERT_CHAT_ID", raising=False)
    seen = _stub_module(monkeypatch, module)

    assert getattr(cli, command)(tmp_path) == 0
    assert seen == {"token": None, "chat": None}


def test_goals_status_in_a_fresh_process_shows_revision_baseline_and_observations(tmp_path):
    """P13 GDT-9 (R35, C28): a fresh process resumes from what is on disk: the contract
    revision, the stored baseline, each criterion's state with where it was observed, what
    is unmet and the next work; never a done verdict, and it writes nothing."""
    from maestro import acceptance, goal_state, goal_store
    from maestro.docs import goals

    from .test_goals import _project
    from .test_goals_contract import V2

    task = {"id": "T2", "status": "open", "serves": ["G2"], "contribution": "direct",
            "criteria": ["AC-03"]}
    project = _project(tmp_path, goals_md=V2, tasks=[task])
    doc = goals.load(project)
    acceptance.store(project, doc, [
        acceptance.Result(id="AC-01", state=acceptance.RED, reason="assert", revision=2),
        acceptance.Result(id="AC-03", state=acceptance.RED, reason="assert", revision=2)])
    goal_state.record_gate(project, doc, {"acceptance": {"AC-01": acceptance.GREEN},
                                          "snapshot": "abc1234def"},
                           task="T1", run_id="run-T1", at="2099-01-01T00:00:00+00:00")
    before = goal_store.path_for(project).read_text(encoding="utf-8")

    run = _run_cli("goals", "status", str(project), "--json")
    assert run.returncode == 0, run.stderr
    shown = json.loads(run.stdout)
    assert (shown["revision"], shown["baseline"]["revision"], shown["baseline"]["stale"]) == (
        2, 2, False)
    assert shown["baseline"]["ran_at"]
    by_id = {c["id"]: c for c in shown["criteria"]}
    assert (by_id["AC-01"]["state"], by_id["AC-01"]["source"], by_id["AC-01"]["task"],
            by_id["AC-01"]["commit"]) == (acceptance.GREEN, "gate", "T1", "abc1234def")
    assert (by_id["AC-03"]["state"], by_id["AC-03"]["source"]) == (acceptance.RED, "baseline")
    assert "AC-03" in shown["unmet"] and "AC-01" not in shown["unmet"]
    assert {"task": "T2", "criteria": ["AC-03"]} in shown["next_work"]
    assert "done" not in shown

    text = _run_cli("goals", "status", str(project))
    assert text.returncode == 0, text.stderr
    assert "revision 2" in text.stdout and "AC-01" in text.stdout and "gate" in text.stdout
    assert "T1" in text.stdout and "unmet" in text.stdout.lower()
    assert goal_store.path_for(project).read_text(encoding="utf-8") == before

    missing = _run_cli("goals", "status", str(tmp_path / "nowhere"))
    assert missing.returncode == 1


def test_goals_finish_prints_the_verdict_and_exits_1_unless_complete(tmp_path, capsys):
    """P13 GDT-10 (R53, R56): `maestro goals finish` reruns every mandatory criterion on the
    checkout at (contract revision, HEAD), writes the outcome report and prints it; exit 0
    only for `complete`. It never asks Dan, adds tasks or notifies."""
    from maestro import cli, goal_completion as gc, goal_store

    from .test_goal_completion import _commit, _project

    repo = _project(tmp_path)
    (repo / "app.py").write_text("print('uncommitted')\n", encoding="utf-8")
    assert cli.cmd_goals(repo, verb="finish") == 1
    out = capsys.readouterr().out
    assert "Verdict: blocked" in out and "uncommitted changes: app.py" in out
    assert (repo / gc.REPORT_MD).is_file()

    _commit(repo, "app.py", "print('ledger 2')\n")
    assert cli.cmd_goals(repo, verb="finish", as_json=True) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["verdict"] == "complete" and shown["revision"] == 2
    assert shown["commit"] == subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                             capture_output=True, text=True).stdout.strip()
    assert all(r.get("trigger", {}).get("kind") != "finish_gap"
               for r in goal_store.read(repo)["replans"])


def test_goals_check_json_without_goals_md_is_one_question(tmp_path, capsys):
    """GDT-13 (R12): setup's Step 2c reads `"ask"`; with no contract there is exactly one
    thing to ask Dan (the contract), not one line per section the empty parse lacks."""
    from maestro import cli

    assert cli.cmd_goals(tmp_path, as_json=True) == 1
    shown = json.loads(capsys.readouterr().out)
    assert shown["exists"] is False
    assert shown["issues"] == [{"severity": "gate", "ids": [], "ask": True,
                                "text": f"no docs/GOALS.md at {tmp_path / 'docs' / 'GOALS.md'}"}]


def test_goals_finish_without_goals_md_or_with_format_one_exits_1(tmp_path, capsys):
    from maestro import cli

    from .test_goals import SKILL_STYLE

    assert cli.cmd_goals(tmp_path / "nowhere", verb="finish") == 1
    assert "no docs/GOALS.md" in capsys.readouterr().err
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "GOALS.md").write_text(SKILL_STYLE, encoding="utf-8")
    assert cli.cmd_goals(tmp_path, verb="finish") == 1
    err = capsys.readouterr().err
    assert "format 1 has no criteria to verify" in err and "maestro goals migrate" in err


def test_goals_finish_in_a_fresh_process_reads_the_stored_row(tmp_path):
    """The verification run is stored per (revision, commit): a second process at the same
    key reprints the verdict without running any criterion again (R35)."""
    from .test_goal_completion import NO_OP, VALID, _commit, _project

    count = "python tests/acceptance/count.py"
    repo = _project(tmp_path, NO_OP.replace(f"`{VALID}`", f"`{count}`"))
    _commit(repo, "tests/acceptance/count.py",
            "from pathlib import Path\n\nmark = Path('.orchestrator') / 'count'\n"
            "mark.parent.mkdir(exist_ok=True)\n"
            "mark.write_text(str(int(mark.read_text()) + 1 if mark.exists() else 1))\n")
    first = _run_cli("goals", "finish", str(repo))
    assert first.returncode == 0, first.stdout + first.stderr
    assert "Verdict: complete" in first.stdout
    second = _run_cli("goals", "finish", str(repo), "--json")
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["verdict"] == "complete"
    assert (repo / ".orchestrator" / "count").read_text() == "1"
