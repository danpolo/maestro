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


def test_truncation_preserves_the_tail_with_resume_info(tmp_path, mock_telegram):
    tail_marker = "Resume if needed: claude --resume test-uuid-marker"
    message = ("y" * 5000) + "\n" + tail_marker
    _run(message, tmp_path, mock_telegram)
    _, received = mock_telegram
    text = received[0]["text"]
    assert len(text) <= 4096
    assert "truncated" in text
    assert tail_marker in text


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
