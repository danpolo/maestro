# Maestro Build Watchdog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically detect a dead/stalled `maestro-build.service` driver, attempt an unattended fix the way an operator would, and report the outcome (and any open questions or actions needed) over the operator's existing Telegram channel — including handling a Claude usage-limit hit by reporting the real reset time instead of silently retrying.

**Architecture:** A `maestro-watchdog.timer` (every 20 min) runs `scripts/watchdog_selffix.sh`, a bash script that reads `docs/PROGRESS.md`'s `PROGRAMME-STATUS` and `maestro-build.service`'s systemd state. If the programme is `IN-PROGRESS` but the driver isn't running, it spawns a capped, scoped `claude -p --model claude-sonnet-5` fixer session (same trust level as a normal chain session — no push, restricted sudo), classifies the outcome (fixed / usage-limit / timed out / crashed), and forwards a report via `scripts/notify_telegram_bridge.sh`, which pushes to the operator's existing Claude Code Telegram bot independent of the live bridge process. `COMPLETE`/`ABORTED` get a one-time notice instead of a fix attempt. State (cooldowns, dedup) lives in `.run/watchdog_state.json`.

**Tech Stack:** bash, python3 (stdlib only — `json`, `urllib`, `subprocess`, `datetime`), pytest + `subprocess` for testing the shell scripts (no new test framework; this repo is pytest-only), systemd (`.timer`/`.service`), sudoers.

**Spec:** `docs/superpowers/specs/2026-08-14-maestro-build-watchdog-design.md`

## Global Constraints

- Fixer sessions commit locally only — **never push, never merge**.
- The fixer inherits the operator's global `~/.claude/CLAUDE.md` automatically (it's a normal `claude -p` process) — the standing privileged-commands rule already applies; it gets passwordless sudo for **exactly** `systemctl restart maestro-build.service` and `systemctl status maestro-build.service`, nothing else.
- The watchdog **never acts on `ABORTED`** — reports it once, does not attempt to fix or resume it (EXECUTION.md: "STOP the programme and report it — not repair it").
- The watchdog **never** detects "driver active but silently wedged" — only "driver not running while `PROGRAMME-STATUS: IN-PROGRESS`" is in scope. This is a documented non-goal, not an oversight.
- All new/modified shell scripts must pass `bash -n <file>` (syntax check) before being committed.
- Every script reading external config (Telegram token, credentials file, systemd unit name) must take an environment-variable override with a sane default, so it's testable without touching real secrets or real systemd/Telegram.
- Full project test suite (`python3 -m pytest`, **not** `pytest -q` — this repo's `pyproject.toml` already sets `addopts = "-q"`, so a second `-q` silently suppresses the summary line) must stay green after every task.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `scripts/lib_maestro_ops.sh` | Create | Shared helpers: `ts()`, `status()`, `hash_p()`, `seconds_until_reset()` |
| `scripts/run_overnight.sh` | Modify | Source `lib_maestro_ops.sh` instead of defining those 4 functions inline |
| `scripts/notify_telegram_bridge.sh` | Create | Push a message via the operator's existing Claude Code Telegram bot |
| `scripts/watchdog_selffix.sh` | Create | Detection, cooldown/dedup state, fixer dispatch, outcome classification |
| `systemd/maestro-watchdog.timer` | Create | Ticks every 20 min |
| `systemd/maestro-watchdog.service` | Create | Oneshot, runs `watchdog_selffix.sh` |
| `tests/ops/__init__.py` | Create | Package marker (matches `tests/backends/__init__.py` convention) |
| `tests/ops/test_lib_maestro_ops.py` | Create | Tests for the extracted helpers |
| `tests/ops/test_notify_telegram_bridge.py` | Create | Tests against a local mock HTTP server |
| `tests/ops/test_watchdog_selffix.py` | Create | Tests against a scratch repo + stubbed `systemctl`/`claude` |

No existing file other than `run_overnight.sh` is modified.

---

### Task 1: Extract shared driver helpers into `scripts/lib_maestro_ops.sh`

**Files:**
- Create: `scripts/lib_maestro_ops.sh`
- Create: `tests/ops/__init__.py`
- Create: `tests/ops/test_lib_maestro_ops.py`
- Modify: `scripts/run_overnight.sh:61-112`

**Interfaces:**
- Produces (used by Tasks 3-5): `status()` — reads `$PROGRESS`, prints the `PROGRAMME-STATUS` value or empty string. `hash_p()` — reads `$PROGRESS`, prints an md5 hash or empty string. `seconds_until_reset()` — reads `$LIMIT_SLEEP` and (new) `$MAESTRO_CREDENTIALS_FILE`, prints an integer number of seconds. `ts()` — prints `date -u +%Y-%m-%dT%H:%M:%SZ`.

This is a pure extraction of `run_overnight.sh`'s existing inline functions, plus one small testability fix: `seconds_until_reset()`'s credentials path becomes overridable (it's hardcoded today, which makes it untestable without hitting a real device's real credentials file).

- [ ] **Step 1: Create `scripts/lib_maestro_ops.sh`**

```bash
#!/usr/bin/env bash
# Shared helpers for scripts/run_overnight.sh and scripts/watchdog_selffix.sh.
# Source this file after setting the variables each function needs.
#
# ts()                     no inputs.
# status()                 requires: PROGRESS (path to docs/PROGRESS.md)
# hash_p()                 requires: PROGRESS
# seconds_until_reset()    requires: LIMIT_SLEEP (fallback seconds if the usage
#                          API lookup fails). Optional: MAESTRO_CREDENTIALS_FILE
#                          (default ~/.claude/.credentials.json).

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

status() {
    grep -oE '^PROGRAMME-STATUS:[[:space:]]+[A-Z-]+' "$PROGRESS" 2>/dev/null \
        | head -1 | awk '{print $2}'
}

hash_p() {
    md5sum "$PROGRESS" 2>/dev/null | cut -d' ' -f1
}

# "You've hit your monthly spend limit" is shown for the regular 5h *and* weekly
# caps too (confirmed 2026-08-09/11 - not a distinct billing cap most of the time).
# A flat hourly retry is fine for a 5h window but wastes up to a week of hourly
# wake-ups against a weekly one (observed: 24 straight hourly misses on
# 2026-08-11). The Anthropic usage API reports exact reset times for both windows,
# so ask it which one is actually binding (highest utilization) and sleep until
# that reset instead of guessing. Falls back to $LIMIT_SLEEP on any failure -
# same behaviour as before this existed.
seconds_until_reset() {
    python3 - "$LIMIT_SLEEP" <<'PYEOF'
import json, os, subprocess, sys
from datetime import datetime, timezone

fallback = int(sys.argv[1])
creds_path = os.environ.get("MAESTRO_CREDENTIALS_FILE", "/home/dan/.claude/.credentials.json")
try:
    creds = json.load(open(creds_path))
    token = creds["claudeAiOauth"]["accessToken"]
    result = subprocess.run([
        "curl", "-s",
        "-H", f"Authorization: Bearer {token}",
        "-H", "Content-Type: application/json",
        "-H", "anthropic-beta: oauth-2025-04-20",
        "-H", "User-Agent: claude-code/2.1.41",
        "https://api.anthropic.com/api/oauth/usage",
    ], capture_output=True, text=True, timeout=10)
    data = json.loads(result.stdout)

    candidates = []
    for key in ("five_hour", "seven_day"):
        u = data.get(key)
        if u and u.get("resets_at") is not None:
            candidates.append((u.get("utilization") or 0, u["resets_at"]))
    if not candidates:
        raise ValueError("no usage windows in API response")

    # The binding limit is whichever window is most utilized right now.
    _, resets_at = max(candidates, key=lambda c: c[0])
    resets = datetime.fromisoformat(resets_at)
    secs = int((resets - datetime.now(timezone.utc)).total_seconds()) + 60
    # Sanity floor/ceiling: never busy-loop, never trust a >8-day figure.
    secs = max(secs, 60)
    secs = min(secs, 8 * 24 * 3600)
    print(secs)
except Exception:
    print(fallback)
PYEOF
}
```

- [ ] **Step 2: `chmod +x scripts/lib_maestro_ops.sh` and syntax-check**

Run: `chmod +x scripts/lib_maestro_ops.sh && bash -n scripts/lib_maestro_ops.sh`
Expected: no output, exit 0.

- [ ] **Step 3: Create `tests/ops/__init__.py`** (empty file, matches `tests/backends/__init__.py`)

- [ ] **Step 4: Write `tests/ops/test_lib_maestro_ops.py`**

```python
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
```

- [ ] **Step 5: Run the new tests**

Run: `python3 -m pytest tests/ops/test_lib_maestro_ops.py -v`
Expected: 7 passed.

- [ ] **Step 6: Point `run_overnight.sh` at the shared lib**

In `scripts/run_overnight.sh`, replace lines 61-112 (the `ts()`/`status()`/`hash_p()`
definitions, the usage-limit comment block, and the full `seconds_until_reset()`
function) with:

```bash
# shellcheck source=scripts/lib_maestro_ops.sh
source "$REPO/scripts/lib_maestro_ops.sh"
```

The line immediately before (blank line after `cd "$REPO" || ...`) and the line
immediately after (`determine_model() {`) are unchanged — this is a pure deletion +
one-line replacement.

- [ ] **Step 7: Syntax-check and verify no leftover duplicate definitions**

Run: `bash -n scripts/run_overnight.sh && grep -n "^status()\|^hash_p()\|^seconds_until_reset()\|^ts()" scripts/run_overnight.sh`
Expected: `bash -n` prints nothing (exit 0); the `grep` prints nothing (no matches —
confirms the inline definitions are gone, not duplicated).

- [ ] **Step 8: Run the full project suite**

Run: `python3 -m pytest`
Expected: same pass count as before this task (this change touches no Python code).

- [ ] **Step 9: Commit**

```bash
git add scripts/lib_maestro_ops.sh scripts/run_overnight.sh tests/ops/__init__.py tests/ops/test_lib_maestro_ops.py
git commit -m "refactor(ops): extract run_overnight.sh's shared helpers into lib_maestro_ops.sh

Pure extraction (ts/status/hash_p/seconds_until_reset), plus making the usage
API's credentials path overridable via MAESTRO_CREDENTIALS_FILE so it's
testable without a real device's real credentials. Prep for the watchdog,
which needs the same helpers."
```

---

### Task 2: `scripts/notify_telegram_bridge.sh`

**Files:**
- Create: `scripts/notify_telegram_bridge.sh`
- Create: `tests/ops/test_notify_telegram_bridge.py`

**Interfaces:**
- Produces (used by Task 5): `scripts/notify_telegram_bridge.sh "<message text>"` — reads `TELEGRAM_BOT_TOKEN` from `$MAESTRO_TELEGRAM_ENV_FILE` (default `~/.claude/channels/telegram/.env`) and the chat id from `$MAESTRO_TELEGRAM_ACCESS_FILE`'s (default `~/.claude/channels/telegram/access.json`) `allowFrom[0]`. Posts to `$MAESTRO_TELEGRAM_API_BASE` (default `https://api.telegram.org`). Exits 0 on success or on missing config (silent skip, matching `AbuAliArchive/scripts/notify_telegram.sh`); exits 1 on a real send failure.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for scripts/notify_telegram_bridge.sh."""
import http.server
import json
import subprocess
import threading
import urllib.parse
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "notify_telegram_bridge.sh"


class _CapturingHandler(http.server.BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        _CapturingHandler.received.append(dict(urllib.parse.parse_qsl(body)))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


@pytest.fixture
def mock_telegram():
    _CapturingHandler.received.clear()
    server = http.server.HTTPServer(("127.0.0.1", 0), _CapturingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, _CapturingHandler.received
    server.shutdown()
    thread.join(timeout=5)


def _write_config(tmp_path, token="test-token", chat_id="42"):
    env_file = tmp_path / "telegram.env"
    env_file.write_text(f"TELEGRAM_BOT_TOKEN={token}\n")
    access_file = tmp_path / "access.json"
    access_file.write_text(json.dumps({"allowFrom": [chat_id]}))
    return env_file, access_file


def _run(message, tmp_path, mock_telegram, env_file=None, access_file=None, chat_id="42"):
    if env_file is None or access_file is None:
        env_file, access_file = _write_config(tmp_path, chat_id=chat_id)
    server, _ = mock_telegram
    port = server.server_address[1]
    return subprocess.run(
        ["bash", str(SCRIPT), message],
        capture_output=True, text=True, timeout=10,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "MAESTRO_TELEGRAM_ENV_FILE": str(env_file),
            "MAESTRO_TELEGRAM_ACCESS_FILE": str(access_file),
            "MAESTRO_TELEGRAM_API_BASE": f"http://127.0.0.1:{port}",
        },
    )


def test_sends_the_message_to_the_configured_chat_id(tmp_path, mock_telegram):
    result = _run("hello watchdog", tmp_path, mock_telegram, chat_id="777")
    assert result.returncode == 0, result.stderr
    _, received = mock_telegram
    assert len(received) == 1
    assert received[0]["chat_id"] == "777"
    assert received[0]["text"] == "hello watchdog"


def test_truncates_messages_over_the_telegram_limit(tmp_path, mock_telegram):
    _run("x" * 5000, tmp_path, mock_telegram)
    _, received = mock_telegram
    assert len(received[0]["text"]) <= 4096
    assert "truncated" in received[0]["text"]


def test_skips_silently_when_the_env_file_is_missing(tmp_path, mock_telegram):
    _, access_file = _write_config(tmp_path)
    result = _run("hi", tmp_path, mock_telegram, env_file=tmp_path / "missing.env", access_file=access_file)
    assert result.returncode == 0
    _, received = mock_telegram
    assert received == []


def test_skips_silently_when_the_access_file_is_missing(tmp_path, mock_telegram):
    env_file, _ = _write_config(tmp_path)
    result = _run("hi", tmp_path, mock_telegram, env_file=env_file, access_file=tmp_path / "missing.json")
    assert result.returncode == 0
    _, received = mock_telegram
    assert received == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/ops/test_notify_telegram_bridge.py -v`
Expected: FAIL — `scripts/notify_telegram_bridge.sh` does not exist yet.

- [ ] **Step 3: Write `scripts/notify_telegram_bridge.sh`**

```bash
#!/usr/bin/env bash
# Send a Telegram message via the same bot/chat used by the Claude Code Telegram
# bridge (~/.claude/channels/telegram/), independent of the live bridge process
# (which is guarded by services/claude-bridge/patch-telegram-plugin.sh against any
# non-bridge session using the plugin channel directly).
# Usage: notify_telegram_bridge.sh "<message text>"
#
# Reads TELEGRAM_BOT_TOKEN from $MAESTRO_TELEGRAM_ENV_FILE (default
# ~/.claude/channels/telegram/.env) and the chat id from the first entry of
# allowFrom in $MAESTRO_TELEGRAM_ACCESS_FILE (default
# ~/.claude/channels/telegram/access.json). Exits 0 and skips silently if either
# is missing, matching AbuAliArchive/scripts/notify_telegram.sh's behavior.
set -uo pipefail

ENV_FILE="${MAESTRO_TELEGRAM_ENV_FILE:-$HOME/.claude/channels/telegram/.env}"
ACCESS_FILE="${MAESTRO_TELEGRAM_ACCESS_FILE:-$HOME/.claude/channels/telegram/access.json}"
API_BASE="${MAESTRO_TELEGRAM_API_BASE:-https://api.telegram.org}"

if [ ! -f "$ENV_FILE" ] || [ ! -f "$ACCESS_FILE" ]; then
    echo "[notify_telegram_bridge] $ENV_FILE or $ACCESS_FILE not found — skipping." >&2
    exit 0
fi

TELEGRAM_BOT_TOKEN="$(grep -oE '^TELEGRAM_BOT_TOKEN=.*' "$ENV_FILE" | head -1 | cut -d= -f2-)"
if [ -z "$TELEGRAM_BOT_TOKEN" ]; then
    echo "[notify_telegram_bridge] TELEGRAM_BOT_TOKEN not set in $ENV_FILE — skipping." >&2
    exit 0
fi

export _NOTIFY_TOKEN="$TELEGRAM_BOT_TOKEN"
export _NOTIFY_ACCESS_FILE="$ACCESS_FILE"
export _NOTIFY_API_BASE="$API_BASE"
export _NOTIFY_MSG="$*"

python3 - <<'PYEOF'
import json, os, sys, urllib.request, urllib.parse

token       = os.environ["_NOTIFY_TOKEN"]
api_base    = os.environ["_NOTIFY_API_BASE"]
access_file = os.environ["_NOTIFY_ACCESS_FILE"]
msg         = os.environ.get("_NOTIFY_MSG", "")

try:
    access = json.load(open(access_file, encoding="utf-8"))
    chat_id = access["allowFrom"][0]
except Exception as e:
    print(f"[notify_telegram_bridge] cannot read chat id from {access_file}: {e}", file=sys.stderr)
    sys.exit(0)

LIMIT = 4096
if len(msg) > LIMIT:
    msg = msg[: LIMIT - 20] + "\n… [truncated]"

data = urllib.parse.urlencode({"chat_id": chat_id, "text": msg}).encode()
req = urllib.request.Request(
    f"{api_base}/bot{token}/sendMessage",
    data=data,
    headers={"Content-Type": "application/x-www-form-urlencoded"},
)
try:
    urllib.request.urlopen(req, timeout=10)
    print("[notify_telegram_bridge] Alert sent.")
except Exception as e:
    print(f"[notify_telegram_bridge] Error: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
```

- [ ] **Step 4: `chmod +x` and syntax-check**

Run: `chmod +x scripts/notify_telegram_bridge.sh && bash -n scripts/notify_telegram_bridge.sh`
Expected: no output, exit 0.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m pytest tests/ops/test_notify_telegram_bridge.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add scripts/notify_telegram_bridge.sh tests/ops/test_notify_telegram_bridge.py
git commit -m "feat(ops): scripts/notify_telegram_bridge.sh

Pushes a message via the operator's existing Claude Code Telegram bot
(~/.claude/channels/telegram/), independent of the live bridge process.
Mirrors AbuAliArchive/scripts/notify_telegram.sh's safe-skip-on-missing-config
behavior. Env-overridable paths/API base for testing against a mock server."
```

---

### Task 3: `scripts/watchdog_selffix.sh` — state helpers and terminal-status handling

**Files:**
- Create: `scripts/watchdog_selffix.sh`
- Create: `tests/ops/test_watchdog_selffix.py`

**Interfaces:**
- Consumes: `status()`, `ts()` from `scripts/lib_maestro_ops.sh` (Task 1). `scripts/notify_telegram_bridge.sh "<msg>"` (Task 2), invoked via `$MAESTRO_NOTIFY_SCRIPT`.
- Produces (used by Tasks 4-5): `state_get <key>` / `state_set <key> <value>` (JSON file at `$MAESTRO_WATCHDOG_STATE`, keys: `reported_terminal_status`, `retry_not_before`, `last_fix_attempt_at`). `acquire_lock` (pid-file at `$MAESTRO_WATCHDOG_LOCK`, `exit 0` early if another run is live). `past <iso-timestamp-or-empty>` — shell truthy (exit 0) if the timestamp is empty or already in the past. `handle_terminal_status <COMPLETE|ABORTED>`. `main()`, guarded so sourcing the file for tests doesn't auto-run it.

This task builds the script's skeleton: env defaults, `main()`'s dispatch on
`status()`, state helpers, the lock, and the two terminal-status branches. Task 4
adds the `IN-PROGRESS` stall path; Task 5 adds the fixer dispatch itself.

- [ ] **Step 1: Write the failing tests (this task's slice)**

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/ops/test_watchdog_selffix.py -v`
Expected: FAIL — `scripts/watchdog_selffix.sh` does not exist yet (or is empty).

- [ ] **Step 3: Write `scripts/watchdog_selffix.sh` (this task's slice)**

```bash
#!/usr/bin/env bash
# Detects a maestro-build driver that has stopped while PROGRAMME-STATUS is still
# IN-PROGRESS, or a COMPLETE/ABORTED status that hasn't been reported yet, and acts.
# Entry point for maestro-watchdog.service (see systemd/maestro-watchdog.{timer,service}).
# See docs/superpowers/specs/2026-08-14-maestro-build-watchdog-design.md.
#
# Env overrides (for tests and manual runs): MAESTRO_REPO, MAESTRO_WATCHDOG_STATE,
# MAESTRO_WATCHDOG_LOCK, MAESTRO_NOTIFY_SCRIPT, MAESTRO_FIXER_TIMEOUT,
# MAESTRO_FIXER_MODEL, MAESTRO_BUILD_UNIT, MAESTRO_CLAUDE_BIN, MAESTRO_STALL_COOLDOWN
set -uo pipefail

REPO="${MAESTRO_REPO:-$HOME/projects/maestro}"
PROGRESS="$REPO/docs/PROGRESS.md"
LOGDIR="$REPO/.run"
STATE_FILE="${MAESTRO_WATCHDOG_STATE:-$LOGDIR/watchdog_state.json}"
LOCK_FILE="${MAESTRO_WATCHDOG_LOCK:-$LOGDIR/watchdog.lock}"
NOTIFY="${MAESTRO_NOTIFY_SCRIPT:-$REPO/scripts/notify_telegram_bridge.sh}"
FIXER_TIMEOUT="${MAESTRO_FIXER_TIMEOUT:-1800}"
FIXER_MODEL="${MAESTRO_FIXER_MODEL:-claude-sonnet-5}"
UNIT="${MAESTRO_BUILD_UNIT:-maestro-build.service}"
CLAUDE_BIN="${MAESTRO_CLAUDE_BIN:-claude}"
STALL_COOLDOWN="${MAESTRO_STALL_COOLDOWN:-5400}"   # 90 min
LIMIT_SLEEP=3600

mkdir -p "$LOGDIR"
# shellcheck source=scripts/lib_maestro_ops.sh
source "$REPO/scripts/lib_maestro_ops.sh"

state_get() {  # state_get <key> -> prints value or empty string
    python3 - "$STATE_FILE" "$1" <<'PYEOF'
import json, sys
path, key = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(path, encoding="utf-8"))
except Exception:
    data = {}
val = data.get(key)
print(val if val is not None else "")
PYEOF
}

state_set() {  # state_set <key> <value>
    python3 - "$STATE_FILE" "$1" "$2" <<'PYEOF'
import json, sys
path, key, value = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    data = json.load(open(path, encoding="utf-8"))
except Exception:
    data = {}
data[key] = value
json.dump(data, open(path, "w", encoding="utf-8"))
PYEOF
}

acquire_lock() {
    if [ -f "$LOCK_FILE" ]; then
        old_pid="$(cat "$LOCK_FILE" 2>/dev/null || true)"
        if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
            echo "[watchdog] $(ts) another run (pid $old_pid) still active; exiting" >&2
            exit 0
        fi
        echo "[watchdog] $(ts) stale lock (pid $old_pid); taking over" >&2
    fi
    echo "$$" > "$LOCK_FILE"
    trap 'rm -f "$LOCK_FILE"' EXIT
}

past() {  # past <iso-ts-or-empty> -> success (0) if empty or already in the past
    local when="$1"
    [ -z "$when" ] && return 0
    python3 - "$when" <<'PYEOF'
import sys
from datetime import datetime, timezone
when = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
sys.exit(0 if datetime.now(timezone.utc) >= when else 1)
PYEOF
}

handle_terminal_status() {
    local st="$1"
    local reported
    reported="$(state_get reported_terminal_status)"
    if [ "$reported" = "$st" ]; then
        return 0
    fi
    case "$st" in
        COMPLETE) "$NOTIFY" "🎉 maestro-build: PROGRAMME-STATUS is COMPLETE." ;;
        ABORTED)  "$NOTIFY" "⛔ maestro-build: PROGRAMME-STATUS is ABORTED — needs your review (docs/PROGRESS.md). The watchdog will not attempt to fix or resume an abort." ;;
    esac
    state_set reported_terminal_status "$st"
}

main() {
    acquire_lock
    if [ ! -f "$PROGRESS" ]; then
        echo "[watchdog] $(ts) $PROGRESS not found; nothing to check" >&2
        return 0
    fi

    local st
    st="$(status)"
    case "$st" in
        COMPLETE|ABORTED)
            handle_terminal_status "$st"
            ;;
        IN-PROGRESS)
            echo "[watchdog] $(ts) IN-PROGRESS; stall handling added in Task 4" >&2
            ;;
        *)
            echo "[watchdog] $(ts) unrecognized PROGRAMME-STATUS '$st'; nothing to do" >&2
            ;;
    esac
}

if [[ "${BASH_SOURCE[0]:-$0}" == "${0}" ]]; then
    main "$@"
fi
```

- [ ] **Step 4: `chmod +x` and syntax-check**

Run: `chmod +x scripts/watchdog_selffix.sh && bash -n scripts/watchdog_selffix.sh`
Expected: no output, exit 0.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m pytest tests/ops/test_watchdog_selffix.py -v`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add scripts/watchdog_selffix.sh tests/ops/test_watchdog_selffix.py
git commit -m "feat(ops): watchdog_selffix.sh skeleton — state, lock, terminal-status handling

COMPLETE/ABORTED get a one-time Telegram notice each via
notify_telegram_bridge.sh, deduped in .run/watchdog_state.json. ABORTED never
triggers a fix attempt (EXECUTION.md: report it, don't repair it). A pid-file
lock prevents overlapping ticks. IN-PROGRESS stall handling lands in the next
commit."
```

---

### Task 4: Stall detection and cooldown

**Files:**
- Modify: `scripts/watchdog_selffix.sh` (the `IN-PROGRESS` branch of `main()`)
- Modify: `tests/ops/test_watchdog_selffix.py` (append tests)

**Interfaces:**
- Consumes: `state_get`/`state_set`/`past`/`acquire_lock` (Task 3).
- Produces (used by Task 5): `driver_active` — shell truthy if `systemctl is-active --quiet "$UNIT"` succeeds. `handle_stall` — checks the usage-limit cooldown (`retry_not_before`) and the stall cooldown (`last_fix_attempt_at` + `$STALL_COOLDOWN`); if clear, records `last_fix_attempt_at` and calls out to a (not-yet-implemented) fixer dispatch. For this task, the fixer dispatch is a single `echo` placeholder that Task 5 replaces — but the cooldown/gating logic itself must be fully real and tested, since that's this task's deliverable.

- [ ] **Step 1: Write the failing tests (append to `tests/ops/test_watchdog_selffix.py`)**

```python
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
```

- [ ] **Step 2: Run the tests to verify the new ones fail**

Run: `python3 -m pytest tests/ops/test_watchdog_selffix.py -v`
Expected: the 4 new tests FAIL (the `IN-PROGRESS` branch is still just an `echo`); the 5 from Task 3 still PASS.

- [ ] **Step 3: Implement the `IN-PROGRESS` branch**

In `scripts/watchdog_selffix.sh`, add above `main()`:

```bash
driver_active() {
    systemctl is-active --quiet "$UNIT" 2>/dev/null
}

handle_stall() {
    local retry_not_before
    retry_not_before="$(state_get retry_not_before)"
    if ! past "$retry_not_before"; then
        echo "[watchdog] $(ts) in usage-limit cooldown until $retry_not_before; skipping" >&2
        return 0
    fi

    local last_attempt
    last_attempt="$(state_get last_fix_attempt_at)"
    if [ -n "$last_attempt" ]; then
        local cooldown_until
        cooldown_until="$(python3 - "$last_attempt" "$STALL_COOLDOWN" <<'PYEOF'
import sys
from datetime import datetime, timedelta, timezone
last = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
print((last + timedelta(seconds=int(sys.argv[2]))).strftime("%Y-%m-%dT%H:%M:%SZ"))
PYEOF
)"
        if ! past "$cooldown_until"; then
            echo "[watchdog] $(ts) last fix attempt was recent; in cooldown until $cooldown_until; skipping" >&2
            return 0
        fi
    fi

    state_set last_fix_attempt_at "$(ts)"
    dispatch_fixer
}

dispatch_fixer() {
    echo "[watchdog] $(ts) TODO(Task 5): spawn the fixer session here" >&2
}
```

Then change `main()`'s `IN-PROGRESS)` case from the Task 3 placeholder to:

```bash
        IN-PROGRESS)
            if driver_active; then
                echo "[watchdog] $(ts) driver active; nothing to do" >&2
            else
                echo "[watchdog] $(ts) driver inactive while IN-PROGRESS; handling stall" >&2
                handle_stall
            fi
            ;;
```

- [ ] **Step 4: Syntax-check**

Run: `bash -n scripts/watchdog_selffix.sh`
Expected: no output, exit 0.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m pytest tests/ops/test_watchdog_selffix.py -v`
Expected: 9 passed (5 from Task 3 + 4 new).

- [ ] **Step 6: Commit**

```bash
git add scripts/watchdog_selffix.sh tests/ops/test_watchdog_selffix.py
git commit -m "feat(ops): watchdog stall detection with usage-limit and refire cooldowns

IN-PROGRESS + driver inactive now routes through handle_stall, gated by two
independent cooldowns: retry_not_before (set after a usage-limit hit, cleared
once past) and a flat 90-min window since the last fix attempt (so a fix that
doesn't fully resolve things doesn't re-page every 20 min). Fixer dispatch
itself is a placeholder until Task 5."
```

---

### Task 5: Fixer dispatch and outcome classification

**Files:**
- Modify: `scripts/watchdog_selffix.sh` (replace `dispatch_fixer`'s placeholder)
- Modify: `tests/ops/test_watchdog_selffix.py` (append tests)

**Interfaces:**
- Consumes: everything from Tasks 3-4, plus `seconds_until_reset()` (Task 1).
- Produces: the complete `dispatch_fixer` — spawns the capped `claude -p` fixer, then `classify_and_report <log> <uuid> <rc> <before_head>` — inspects the log and routes to one of: timeout report, usage-limit report (+ sets `retry_not_before`), empty-output report, or the normal forwarded report.

- [ ] **Step 1: Write the failing tests (append to `tests/ops/test_watchdog_selffix.py`)**

```python
def test_inactive_driver_spawns_the_fixer_and_forwards_its_report(scratch_repo, tmp_path):
    _write_progress(scratch_repo, "IN-PROGRESS")
    notify, log = _recording_notify(tmp_path)
    systemctl_bin = _fake_bin(tmp_path, "systemctl", "#!/bin/sh\nexit 3\n")
    claude_stub = tmp_path / "claude_stub.sh"
    claude_stub.write_text(
        "#!/bin/sh\n"
        'printf "## What happened\\ndriver was down\\n## What I fixed\\nrestarted it\\n'
        '## Open questions\\nnone\\n## Action needed from you\\nnone\\n"\n'
    )
    claude_stub.chmod(0o755)
    result = _run_watchdog(scratch_repo, {
        "MAESTRO_NOTIFY_SCRIPT": str(notify),
        "MAESTRO_CLAUDE_BIN": str(claude_stub),
        "PATH": f"{systemctl_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
    })
    assert result.returncode == 0, result.stderr
    text = log.read_text()
    assert "What I fixed" in text
    assert "restarted it" in text


def test_a_usage_limit_hit_with_no_commit_reports_the_real_reset_time(scratch_repo, tmp_path):
    _write_progress(scratch_repo, "IN-PROGRESS")
    notify, log = _recording_notify(tmp_path)
    systemctl_bin = _fake_bin(tmp_path, "systemctl", "#!/bin/sh\nexit 3\n")
    curl_bin = _fake_bin(tmp_path, "curl", (
        "#!/bin/sh\n"
        "echo '{\"five_hour\": {\"utilization\": 99, \"resets_at\": "
        "\"2099-01-01T00:00:00+00:00\"}}'\n"
    ))
    claude_stub = tmp_path / "claude_stub.sh"
    claude_stub.write_text("#!/bin/sh\necho 'You have hit your monthly spend limit.'\n")
    claude_stub.chmod(0o755)
    creds_file = tmp_path / "creds.json"
    creds_file.write_text('{"claudeAiOauth": {"accessToken": "fake"}}')
    result = _run_watchdog(scratch_repo, {
        "MAESTRO_NOTIFY_SCRIPT": str(notify),
        "MAESTRO_CLAUDE_BIN": str(claude_stub),
        "MAESTRO_CREDENTIALS_FILE": str(creds_file),
        "PATH": f"{systemctl_bin}:{curl_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
    })
    assert result.returncode == 0, result.stderr
    text = log.read_text()
    assert "usage limit" in text.lower()
    state = json.loads((scratch_repo / ".run" / "watchdog_state.json").read_text())
    assert state.get("retry_not_before")


def test_a_usage_limit_mention_is_ignored_if_a_commit_was_made(scratch_repo, tmp_path):
    _write_progress(scratch_repo, "IN-PROGRESS")
    notify, log = _recording_notify(tmp_path)
    systemctl_bin = _fake_bin(tmp_path, "systemctl", "#!/bin/sh\nexit 3\n")
    claude_stub = tmp_path / "claude_stub.sh"
    claude_stub.write_text(
        "#!/bin/sh\n"
        f'cd "{scratch_repo}" && git commit --allow-empty -q -m "fixer commit"\n'
        'printf "## What happened\\nhad hit a limit earlier but recovered\\n'
        '## What I fixed\\nrestarted the driver\\n## Open questions\\nnone\\n'
        '## Action needed from you\\nnone\\n"\n'
    )
    claude_stub.chmod(0o755)
    result = _run_watchdog(scratch_repo, {
        "MAESTRO_NOTIFY_SCRIPT": str(notify),
        "MAESTRO_CLAUDE_BIN": str(claude_stub),
        "PATH": f"{systemctl_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
    })
    assert result.returncode == 0, result.stderr
    text = log.read_text()
    assert "restarted the driver" in text
    state = json.loads((scratch_repo / ".run" / "watchdog_state.json").read_text())
    assert not state.get("retry_not_before")  # not treated as a rate-limit outcome


def test_a_timed_out_fixer_is_reported_with_a_resume_hint(scratch_repo, tmp_path):
    _write_progress(scratch_repo, "IN-PROGRESS")
    notify, log = _recording_notify(tmp_path)
    systemctl_bin = _fake_bin(tmp_path, "systemctl", "#!/bin/sh\nexit 3\n")
    claude_stub = tmp_path / "claude_hangs.sh"
    claude_stub.write_text("#!/bin/sh\nsleep 30\n")
    claude_stub.chmod(0o755)
    result = _run_watchdog(scratch_repo, {
        "MAESTRO_NOTIFY_SCRIPT": str(notify),
        "MAESTRO_CLAUDE_BIN": str(claude_stub),
        "MAESTRO_FIXER_TIMEOUT": "1",
        "PATH": f"{systemctl_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
    })
    assert result.returncode == 0, result.stderr
    text = log.read_text()
    assert "timed out" in text.lower()
    assert "--resume" in text


def test_an_empty_fixer_report_still_notifies(scratch_repo, tmp_path):
    _write_progress(scratch_repo, "IN-PROGRESS")
    notify, log = _recording_notify(tmp_path)
    systemctl_bin = _fake_bin(tmp_path, "systemctl", "#!/bin/sh\nexit 3\n")
    claude_stub = tmp_path / "claude_silent.sh"
    claude_stub.write_text("#!/bin/sh\nexit 1\n")
    claude_stub.chmod(0o755)
    result = _run_watchdog(scratch_repo, {
        "MAESTRO_NOTIFY_SCRIPT": str(notify),
        "MAESTRO_CLAUDE_BIN": str(claude_stub),
        "PATH": f"{systemctl_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
    })
    assert result.returncode == 0, result.stderr
    assert "no output" in log.read_text().lower()
```

- [ ] **Step 2: Run the tests to verify the new ones fail**

Run: `python3 -m pytest tests/ops/test_watchdog_selffix.py -v`
Expected: the 5 new tests FAIL (`dispatch_fixer` is still the Task 4 placeholder); the 9 prior tests still PASS.

- [ ] **Step 3: Implement `dispatch_fixer` and `classify_and_report`**

In `scripts/watchdog_selffix.sh`, replace the placeholder `dispatch_fixer` (added in
Task 4) with:

```bash
classify_and_report() {
    local log="$1" uuid="$2" rc="$3" before_head="$4"
    local out after_head
    out="$(cat "$log" 2>/dev/null || true)"

    if [ "$rc" = "124" ]; then
        "$NOTIFY" "$(printf '⏱️ maestro-build watchdog: fixer session timed out after %ss.\nSession: %s\nResume: claude --resume %s   (cwd: %s)\nFull log: %s' \
            "$FIXER_TIMEOUT" "$uuid" "$uuid" "$REPO" "$log")"
        return
    fi

    after_head="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo '')"
    local no_new_commit=1
    [ -n "$after_head" ] && [ "$after_head" != "$before_head" ] && no_new_commit=0

    if [ "$no_new_commit" -ne 0 ] \
       && echo "$out" | grep -qiE "usage limit|hit (your|the) .*limit|rate.?limit|resets at|spend limit"; then
        local wait_s wake
        wait_s="$(seconds_until_reset)"
        wake="$(date -u -d "+${wait_s} seconds" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)"
        state_set retry_not_before "$wake"
        "$NOTIFY" "$(printf '⏳ maestro-build watchdog: hit a Claude usage limit trying to fix it. Resets at %s. Will retry automatically after that.\nSession: %s\nFull log: %s' \
            "$wake" "$uuid" "$log")"
        return
    fi

    if [ -z "$out" ]; then
        "$NOTIFY" "$(printf '🛑 maestro-build watchdog: fixer session (rc=%s) produced no output.\nSession: %s\nResume: claude --resume %s   (cwd: %s)\nFull log: %s' \
            "$rc" "$uuid" "$uuid" "$REPO" "$log")"
        return
    fi

    "$NOTIFY" "$(printf '🔧 maestro-build watchdog — driver was down, PROGRAMME-STATUS still IN-PROGRESS\nFixer session: %s\n\n%s\n\nResume if needed: claude --resume %s   (cwd: %s)\nFull log: %s' \
        "$uuid" "$out" "$uuid" "$REPO" "$log")"
}

dispatch_fixer() {
    local uuid log rc before_head
    uuid=$(python3 -c 'import uuid; print(uuid.uuid4())')
    log="$LOGDIR/watchdog-fix-$(date -u +%Y%m%dT%H%M%SZ).log"
    before_head="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo '')"

    read -r -d '' brief <<EOF
ultracode

The maestro-build driver (systemd unit $UNIT) is not running while
docs/PROGRESS.md's PROGRAMME-STATUS is still IN-PROGRESS. You are an
unattended fixer session, invoked by scripts/watchdog_selffix.sh — not a
normal chain session; do not try to continue the M2/build work itself.

Diagnose the root cause first (systematic debugging: read
scripts/loop_status.sh's checks, \`journalctl -u $UNIT\`, .run/session-*.log,
\`git log\`, and docs/PROGRESS.md before proposing a fix — do not guess).
Apply the minimal fix. If recovering uncommitted work, verify the full test
suite is green before committing anything; never push.

You are a headless, one-shot process exactly like a chain session: do not end
your turn on unresolved background work (a Workflow call, a run_in_background
command) — wait for it synchronously or don't start it, since nothing will
receive its completion notification after you exit.

You have passwordless sudo for exactly \`systemctl restart $UNIT\` and
\`systemctl status $UNIT\` — nothing else. If the fix requires restarting the
driver, do so and confirm via scripts/loop_status.sh-equivalent checks that
it is actually iterating again before reporting success. For any other
privileged action, follow the standing rule: write a /tmp script and report
the exact command for the operator to run — do not attempt it yourself.

End your final message in exactly this structure:

## What happened
## What I fixed
## Open questions
## Action needed from you
EOF

    set +e
    timeout "$FIXER_TIMEOUT" "$CLAUDE_BIN" -p --model "$FIXER_MODEL" \
        --dangerously-skip-permissions --session-id "$uuid" "$brief" \
        >"$log" 2>&1
    rc=$?
    set -e

    classify_and_report "$log" "$uuid" "$rc" "$before_head"
}
```

- [ ] **Step 4: Syntax-check**

Run: `bash -n scripts/watchdog_selffix.sh`
Expected: no output, exit 0.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m pytest tests/ops/test_watchdog_selffix.py -v`
Expected: 14 passed (all tests from Tasks 3-5).

- [ ] **Step 6: Run the full project suite**

Run: `python3 -m pytest`
Expected: prior pass count + 25 (7 lib + 4 notify + 14 watchdog = 25 new ops
tests). The point is zero failures and zero unexplained deltas — if the actual
number differs, understand why before moving on, don't just accept it.

- [ ] **Step 7: Commit**

```bash
git add scripts/watchdog_selffix.sh tests/ops/test_watchdog_selffix.py
git commit -m "feat(ops): watchdog fixer dispatch and outcome classification

dispatch_fixer spawns a capped (default 30 min), scoped claude -p --model
claude-sonnet-5 session with a systematic-debugging brief, then
classify_and_report routes its outcome: timeout (with a --resume hint),
usage-limit-with-no-commit (real reset time via seconds_until_reset, sets a
cooldown), empty output, or the normal forwarded structured report. A
limit-phrase mention is ignored if the fixer actually committed something —
same false-positive guard run_overnight.sh already has for its own narration
case."
```

---

### Task 6: systemd units

**Files:**
- Create: `systemd/maestro-watchdog.timer`
- Create: `systemd/maestro-watchdog.service`

**Interfaces:**
- Produces (used by Task 7): the two tracked unit files that the deploy script copies into `/etc/systemd/system/`.

- [ ] **Step 1: Create `systemd/maestro-watchdog.timer`**

```ini
# /etc/systemd/system/maestro-watchdog.timer
#
# Ticks scripts/watchdog_selffix.sh every 20 minutes. See
# docs/superpowers/specs/2026-08-14-maestro-build-watchdog-design.md for the
# full design, and maestro-build.service for the sibling unit this watches.
[Unit]
Description=Maestro Build Watchdog Timer

[Timer]
OnBootSec=5min
OnUnitActiveSec=20min
Unit=maestro-watchdog.service

[Install]
WantedBy=timers.target
```

- [ ] **Step 2: Create `systemd/maestro-watchdog.service`**

```ini
# /etc/systemd/system/maestro-watchdog.service
#
# Oneshot, triggered by maestro-watchdog.timer. Runs scripts/watchdog_selffix.sh,
# which is safe to run concurrently with maestro-build.service — it only reads
# state and, on a detected stall, spawns a separate claude -p fixer session; it
# never touches the tmux session or driver process directly except via the two
# whitelisted `systemctl restart/status` commands (see /etc/sudoers.d/maestro-watchdog).
[Unit]
Description=Maestro Build Watchdog
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=dan
WorkingDirectory=/home/dan/projects/maestro
Environment=HOME=/home/dan
Environment=USER=dan
Environment=PATH=/home/dan/.local/bin:/home/dan/.bun/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/bin/bash /home/dan/projects/maestro/scripts/watchdog_selffix.sh
# A tick should never run long: the fixer itself is capped internally at 30 min
# (MAESTRO_FIXER_TIMEOUT). This is a hard backstop in case that fails.
TimeoutStartSec=2400
```

- [ ] **Step 3: Validate unit syntax**

Run: `systemd-analyze verify systemd/maestro-watchdog.timer systemd/maestro-watchdog.service 2>&1 || true`
Expected: no `WantedBy=` / `Unit=` resolution errors other than expected "unit not
found" complaints for units that only exist once deployed to `/etc/systemd/system/`
(same caveat applies to the existing `systemd/maestro-build.service`, which isn't
independently verifiable before deployment either). If `systemd-analyze` isn't
available in this environment, `grep -c '^\[' systemd/maestro-watchdog.*` as a
sanity check that both files have well-formed `[Section]` headers is an acceptable
fallback — note which one you used.

- [ ] **Step 4: Commit**

```bash
git add systemd/maestro-watchdog.timer systemd/maestro-watchdog.service
git commit -m "feat(ops): systemd units for the maestro-build watchdog timer

Ticks every 20 min (OnUnitActiveSec), oneshot service runs
scripts/watchdog_selffix.sh. Not yet installed to /etc/systemd/system/ — that's
Task 7's one-time deploy script."
```

---

### Task 7: Deployment (sudoers + install) and end-to-end verification

**Files:**
- Create (ephemeral, not tracked): `/tmp/setup-maestro-watchdog.sh`

**Interfaces:**
- Consumes: `systemd/maestro-watchdog.{timer,service}` (Task 6), all of Tasks 1-5.
- Produces: a live `maestro-watchdog.timer` and the scoped sudoers grant the fixer needs.

- [ ] **Step 1: Write `/tmp/setup-maestro-watchdog.sh`**

```bash
#!/bin/bash
# One-time setup: sudoers rule + systemd units for the maestro-build watchdog.
# See docs/superpowers/specs/2026-08-14-maestro-build-watchdog-design.md
set -euo pipefail
REPO=/home/dan/projects/maestro

SUDOERS=/etc/sudoers.d/maestro-watchdog
cat > "$SUDOERS" <<'EOF'
dan ALL=(root) NOPASSWD: /bin/systemctl restart maestro-build.service
dan ALL=(root) NOPASSWD: /bin/systemctl status maestro-build.service
EOF
visudo -cf "$SUDOERS"
chmod 440 "$SUDOERS"

cp "$REPO/systemd/maestro-watchdog.timer" /etc/systemd/system/maestro-watchdog.timer
cp "$REPO/systemd/maestro-watchdog.service" /etc/systemd/system/maestro-watchdog.service

systemctl daemon-reload
systemctl enable --now maestro-watchdog.timer

echo "--- timer status ---"
systemctl status maestro-watchdog.timer --no-pager -l
echo "--- next scheduled tick ---"
systemctl list-timers maestro-watchdog.timer --no-pager
```

- [ ] **Step 2: `chmod +x` and hand off**

Run: `chmod +x /tmp/setup-maestro-watchdog.sh`

Per the standing privileged-commands rule, this step ends here — report the exact
command for the operator to run and stop:

```
sudo bash /tmp/setup-maestro-watchdog.sh
```

- [ ] **Step 3: After the operator confirms it ran, verify the passwordless sudo grant**

Run: `sudo -n systemctl status maestro-build.service --no-pager -l`
Expected: succeeds with no password prompt (if it prompts, the sudoers rule wasn't
installed correctly — re-check `/etc/sudoers.d/maestro-watchdog` and its `visudo -cf`
result).

- [ ] **Step 4: Verify the timer is live**

Run: `systemctl is-active maestro-watchdog.timer && systemctl list-timers maestro-watchdog.timer --no-pager`
Expected: `active`, with a `NEXT` time within 20 minutes.

- [ ] **Step 5: End-to-end smoke test against the real driver (manual, not automated)**

With `maestro-build.service` currently healthy (per `bash scripts/loop_status.sh`),
temporarily stop it to simulate the exact failure this system exists for, and confirm
the watchdog notices and reports — without letting a real fixer session run
unsupervised for this first observation:

```bash
sudo systemctl stop maestro-build.service
MAESTRO_CLAUDE_BIN=/bin/false bash scripts/watchdog_selffix.sh
```

Expected: a Telegram message arrives reporting the stall (the `MAESTRO_CLAUDE_BIN`
override makes the "fixer" fail instantly and deterministically, exercising the
whole detection → dispatch → classify → notify path without spending a real Claude
session or making real changes). Then restore the driver:

```bash
sudo systemctl start maestro-build.service
bash scripts/loop_status.sh
```

Expected: driver active again, chain resuming. This manual check substitutes for an
automated test of the real `claude` binary, which Task 5's stubbed-`claude` tests
already cover at the unit level — this step is specifically to confirm the real
Telegram delivery path and the real sudoers grant end to end, which cannot be faked
without touching real external systems.

- [ ] **Step 6: Update the maestro build loop memory note**

The existing memory at `maestro-build-loop.md` (recalled at the start of this kind of
task) should mention the watchdog now exists, so a future session doesn't re-diagnose
this from scratch. Append a bullet noting: `maestro-watchdog.timer` exists as of this
plan's completion date, checks every 20 min, and where to find its state
(`.run/watchdog_state.json`) and its own log convention (`.run/watchdog-fix-*.log`).
(This step is a documentation courtesy, not a code change — do it in the memory file
directly, not in the repo.)
