"""Tests for scripts/watchdog_selffix.sh."""
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "scripts" / "lib_maestro_ops.sh"
SCRIPT = REPO_ROOT / "scripts" / "watchdog_selffix.sh"


@pytest.fixture
def scratch_repo(tmp_path):
    """A throwaway repo dir with docs/PROGRESS.md, .run/, and its own git history."""
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / ".run").mkdir()
    (repo / "scripts").mkdir()
    for src in (LIB, SCRIPT):
        dest = repo / "scripts" / src.name
        dest.write_text(src.read_text() if src.exists() else "")
        dest.chmod(0o755)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "docs" / "PROGRESS.md").write_text("PROGRAMME-STATUS: IN-PROGRESS\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def _write_progress(repo, status):
    (repo / "docs" / "PROGRESS.md").write_text(f"PROGRAMME-STATUS: {status}\n")


def _recording_notify(tmp_path):
    """A stub notify script that appends each call's message to a file."""
    log = tmp_path / "notify_calls.log"
    notify = tmp_path / "notify.sh"
    notify.write_text(f'#!/bin/sh\nprintf "%s\\n---CALL-END---\\n" "$1" >> "{log}"\n')
    notify.chmod(0o755)
    return notify, log


def _run_watchdog(repo, env_overrides):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "MAESTRO_REPO": str(repo),
        "MAESTRO_WATCHDOG_STATE": str(repo / ".run" / "watchdog_state.json"),
        "MAESTRO_WATCHDOG_LOCK": str(repo / ".run" / "watchdog.lock"),
        **env_overrides,
    }
    return subprocess.run(
        ["bash", str(repo / "scripts" / "watchdog_selffix.sh")],
        capture_output=True, text=True, timeout=30, env=env,
    )


def test_complete_status_sends_one_notification(scratch_repo, tmp_path):
    _write_progress(scratch_repo, "COMPLETE")
    notify, log = _recording_notify(tmp_path)
    result = _run_watchdog(scratch_repo, {"MAESTRO_NOTIFY_SCRIPT": str(notify)})
    assert result.returncode == 0, result.stderr
    assert "COMPLETE" in log.read_text()


def test_complete_status_is_not_re_reported_on_the_next_tick(scratch_repo, tmp_path):
    _write_progress(scratch_repo, "COMPLETE")
    notify, log = _recording_notify(tmp_path)
    env = {"MAESTRO_NOTIFY_SCRIPT": str(notify)}
    _run_watchdog(scratch_repo, env)
    _run_watchdog(scratch_repo, env)
    assert log.read_text().count("---CALL-END---") == 1


def test_aborted_status_sends_a_review_notice_and_never_runs_the_fixer(scratch_repo, tmp_path):
    _write_progress(scratch_repo, "ABORTED")
    notify, log = _recording_notify(tmp_path)
    claude_stub = tmp_path / "claude_should_not_run.sh"
    claude_stub.write_text('#!/bin/sh\necho "FIXER SHOULD NOT HAVE RUN" >&2\nexit 1\n')
    claude_stub.chmod(0o755)
    result = _run_watchdog(scratch_repo, {
        "MAESTRO_NOTIFY_SCRIPT": str(notify),
        "MAESTRO_CLAUDE_BIN": str(claude_stub),
    })
    assert result.returncode == 0, result.stderr
    text = log.read_text()
    assert "ABORTED" in text
    assert "needs your review" in text


def test_a_live_lock_prevents_a_concurrent_run(scratch_repo, tmp_path):
    _write_progress(scratch_repo, "COMPLETE")
    notify, log = _recording_notify(tmp_path)
    lock_file = scratch_repo / ".run" / "watchdog.lock"
    lock_file.write_text(str(os.getpid()))  # this test process is definitely alive
    result = _run_watchdog(scratch_repo, {"MAESTRO_NOTIFY_SCRIPT": str(notify)})
    assert result.returncode == 0
    assert not log.exists()
    assert lock_file.read_text() == str(os.getpid())  # untouched


def test_a_stale_lock_from_a_dead_pid_does_not_block_a_run(scratch_repo, tmp_path):
    _write_progress(scratch_repo, "COMPLETE")
    notify, log = _recording_notify(tmp_path)
    lock_file = scratch_repo / ".run" / "watchdog.lock"
    lock_file.write_text("999999")  # almost certainly not a live pid
    result = _run_watchdog(scratch_repo, {"MAESTRO_NOTIFY_SCRIPT": str(notify)})
    assert result.returncode == 0
    assert "COMPLETE" in log.read_text()


def _fake_bin(tmp_path, name, script_body):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    path = bin_dir / name
    path.write_text(script_body)
    path.chmod(0o755)
    return bin_dir


def test_active_driver_does_nothing(scratch_repo, tmp_path):
    _write_progress(scratch_repo, "IN-PROGRESS")
    notify, log = _recording_notify(tmp_path)
    systemctl_bin = _fake_bin(tmp_path, "systemctl", "#!/bin/sh\nexit 0\n")  # is-active -> active
    result = _run_watchdog(scratch_repo, {
        "MAESTRO_NOTIFY_SCRIPT": str(notify),
        "PATH": f"{systemctl_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
    })
    assert result.returncode == 0, result.stderr
    assert not log.exists()


def test_inactive_driver_triggers_the_stall_path_once(scratch_repo, tmp_path):
    _write_progress(scratch_repo, "IN-PROGRESS")
    systemctl_bin = _fake_bin(tmp_path, "systemctl", "#!/bin/sh\nexit 3\n")  # is-active -> inactive
    result = _run_watchdog(scratch_repo, {
        "PATH": f"{systemctl_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
    })
    assert result.returncode == 0, result.stderr
    assert "stall" in result.stderr.lower()
    state = json.loads((scratch_repo / ".run" / "watchdog_state.json").read_text())
    assert state.get("last_fix_attempt_at")


def test_a_second_stall_within_the_cooldown_window_does_not_refire(scratch_repo, tmp_path):
    _write_progress(scratch_repo, "IN-PROGRESS")
    systemctl_bin = _fake_bin(tmp_path, "systemctl", "#!/bin/sh\nexit 3\n")
    env = {
        "MAESTRO_STALL_COOLDOWN": "5400",
        "PATH": f"{systemctl_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
    }
    _run_watchdog(scratch_repo, env)
    first_attempt = json.loads((scratch_repo / ".run" / "watchdog_state.json").read_text())["last_fix_attempt_at"]
    _run_watchdog(scratch_repo, env)
    second_attempt = json.loads((scratch_repo / ".run" / "watchdog_state.json").read_text())["last_fix_attempt_at"]
    assert first_attempt == second_attempt  # unchanged: second tick was in cooldown


def test_a_future_retry_not_before_skips_the_stall_path(scratch_repo, tmp_path):
    _write_progress(scratch_repo, "IN-PROGRESS")
    systemctl_bin = _fake_bin(tmp_path, "systemctl", "#!/bin/sh\nexit 3\n")
    state_file = scratch_repo / ".run" / "watchdog_state.json"
    state_file.write_text(json.dumps({"retry_not_before": "2099-01-01T00:00:00Z"}))
    result = _run_watchdog(scratch_repo, {
        "MAESTRO_WATCHDOG_STATE": str(state_file),
        "PATH": f"{systemctl_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
    })
    assert result.returncode == 0, result.stderr
    state = json.loads(state_file.read_text())
    assert "last_fix_attempt_at" not in state  # never reached the fixer dispatch
