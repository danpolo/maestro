"""Tests for scripts/lib_maestro_ops.sh."""
import re
import subprocess
from pathlib import Path

LIB = Path(__file__).resolve().parents[2] / "scripts" / "lib_maestro_ops.sh"


def run_bash(snippet: str, env: dict) -> subprocess.CompletedProcess:
    full_env = {"PATH": "/usr/bin:/bin:/usr/local/bin", **env}
    return subprocess.run(
        ["bash", "-c", f'source "{LIB}"; {snippet}'],
        capture_output=True, text=True, env=full_env, timeout=10,
    )


def test_status_extracts_the_programme_status_value(tmp_path):
    progress = tmp_path / "PROGRESS.md"
    progress.write_text("PROGRAMME-STATUS: IN-PROGRESS\nsome other line\n")
    result = run_bash("status", env={"PROGRESS": str(progress)})
    assert result.stdout.strip() == "IN-PROGRESS"


def test_status_is_empty_when_the_file_is_missing(tmp_path):
    progress = tmp_path / "does-not-exist.md"
    result = run_bash("status", env={"PROGRESS": str(progress)})
    assert result.stdout.strip() == ""


def test_hash_p_changes_when_the_file_content_changes(tmp_path):
    progress = tmp_path / "PROGRESS.md"
    progress.write_text("one")
    first = run_bash("hash_p", env={"PROGRESS": str(progress)}).stdout.strip()
    progress.write_text("two")
    second = run_bash("hash_p", env={"PROGRESS": str(progress)}).stdout.strip()
    assert first != second
    assert first != ""


def test_hash_p_is_stable_for_unchanged_content(tmp_path):
    progress = tmp_path / "PROGRESS.md"
    progress.write_text("stable content")
    first = run_bash("hash_p", env={"PROGRESS": str(progress)}).stdout.strip()
    second = run_bash("hash_p", env={"PROGRESS": str(progress)}).stdout.strip()
    assert first == second


def test_seconds_until_reset_falls_back_when_credentials_are_unreachable(tmp_path):
    result = run_bash(
        "seconds_until_reset",
        env={
            "PROGRESS": "",
            "LIMIT_SLEEP": "111",
            "MAESTRO_CREDENTIALS_FILE": str(tmp_path / "no-such-file.json"),
        },
    )
    assert result.stdout.strip() == "111"


def test_seconds_until_reset_uses_the_more_utilized_window(tmp_path):
    creds_file = tmp_path / "credentials.json"
    creds_file.write_text('{"claudeAiOauth": {"accessToken": "fake-token"}}')

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_curl = bin_dir / "curl"
    fake_curl.write_text(
        "#!/bin/sh\n"
        "echo '{\"five_hour\": {\"utilization\": 40, \"resets_at\": "
        "\"2099-01-01T00:00:00+00:00\"}, \"seven_day\": {\"utilization\": 90, "
        "\"resets_at\": \"2099-06-01T00:00:00+00:00\"}}'\n"
    )
    fake_curl.chmod(0o755)

    result = run_bash(
        "seconds_until_reset",
        env={
            "PROGRESS": "",
            "LIMIT_SLEEP": "999",
            "MAESTRO_CREDENTIALS_FILE": str(creds_file),
            "PATH": f"{bin_dir}:/usr/bin:/bin",
        },
    )
    # seven_day has the higher utilization (90 > 40) so it wins; its resets_at
    # is decades away, so the 8-day ceiling clamps the result to exactly that.
    assert int(result.stdout.strip()) == 8 * 24 * 3600


def test_ts_prints_an_iso8601_utc_timestamp():
    result = run_bash("ts", env={})
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", result.stdout.strip())


def test_is_live_usage_limit_true_for_a_real_block(tmp_path):
    # Real shape observed across 20 genuine hits in .run/session-*.log: two
    # permission-warning lines, then the CLI's block message as the last line.
    log = tmp_path / "session-24.log"
    log.write_text(
        "Permission allow rule (...): Write(...) is not matched...\n"
        "Permission allow rule (...): Write(...) is not matched...\n"
        "You've hit your monthly spend limit · raise it at claude.ai/settings/usage?from=cc_cli_limit_message\n"
    )
    result = run_bash('is_live_usage_limit "$LOG" && echo yes || echo no', env={"LOG": str(log)})
    assert result.stdout.strip() == "yes"


def test_is_live_usage_limit_false_for_a_session_that_narrates_a_past_hit(tmp_path):
    # Regression for 2026-08-17: session-08.log completed cleanly (rc=0, two real
    # commits, full test suite green) but its report mentioned a *sub-agent* that
    # hit the limit earlier and was already recovered from within that session.
    # A whole-file grep false-matched this and cost a ~44h stall.
    log = tmp_path / "session-08.log"
    log.write_text(
        "## M4a complete\n"
        "\n"
        "Two things to flag:\n"
        "- The workflow's rebind agent died mid-edit on `quota.py` hitting the "
        "monthly spend limit. After reset I finished the remaining bindings inline.\n"
        "\n"
        "Commits `de7d1e6` + `b60d717`. Context ended at ~150K - next session: M5 cutover.\n"
    )
    result = run_bash('is_live_usage_limit "$LOG" && echo yes || echo no', env={"LOG": str(log)})
    assert result.stdout.strip() == "no"


def test_is_live_usage_limit_false_for_a_log_with_no_mention_of_limits(tmp_path):
    log = tmp_path / "session-clean.log"
    log.write_text("Working tree clean.\nAll good, exiting.\n")
    result = run_bash('is_live_usage_limit "$LOG" && echo yes || echo no', env={"LOG": str(log)})
    assert result.stdout.strip() == "no"
