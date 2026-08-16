"""`maestro.adapters` — DESIGN.md §4's adapter invocation contract.

New M4 behaviour, not an extraction (`maestro/adapters.py` is a new module), so these
are ordinary end-to-end tests rather than characterization pins. Every scenario below
builds its own scratch executable under `tmp_path`; nothing here depends on any real
project or the reference repo.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from maestro import adapters


def _write_script(path: Path, body: str) -> Path:
    """Write an executable python3 script at `path` with `body` as its source."""
    path.write_text(f"#!{sys.executable}\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _adapter_dir(tmp_path: Path) -> Path:
    d = tmp_path / "adapters"
    d.mkdir()
    return d


# ── ok ──

def test_ok_status_parses_json_stdout(tmp_path):
    _write_script(
        _adapter_dir(tmp_path) / "test",
        "import sys, json\n"
        "payload = json.load(sys.stdin)\n"
        "print(json.dumps({'pass': True, 'summary': f\"saw {payload}\"}))\n",
    )

    result = adapters.run_adapter("test", tmp_path, payload={"foo": "bar"})

    assert result.status == "ok"
    assert result.kind == "test"
    assert result.exit_code == 0
    assert result.data == {"pass": True, "summary": "saw {'foo': 'bar'}"}
    assert result.raw_tail is None


def test_ok_default_payload_is_empty_object(tmp_path):
    _write_script(
        _adapter_dir(tmp_path) / "eval",
        "import sys, json\n"
        "payload = json.load(sys.stdin)\n"
        "print(json.dumps({'received': payload}))\n",
    )

    result = adapters.run_adapter("eval", tmp_path)

    assert result.status == "ok"
    assert result.data == {"received": {}}


# ── absent ──

def test_absent_when_file_does_not_exist(tmp_path):
    result = adapters.run_adapter("eval", tmp_path)

    assert result.status == "absent"
    assert result.data is None
    assert "eval" in result.detail


def test_absent_when_file_exists_but_not_executable(tmp_path):
    path = _adapter_dir(tmp_path) / "smoke"
    path.write_text("#!/bin/sh\necho '{}'\n")
    # deliberately no chmod +x

    result = adapters.run_adapter("smoke", tmp_path)

    assert result.status == "absent"


def test_missing_test_adapter_is_absent_not_an_exception(tmp_path):
    """The plan's explicit contract: a missing `test` adapter is still `status=absent`,
    never a raised exception. Any "test is required" policy lives in a caller
    (`doctor`/`gates.py`), not in `run_adapter` itself."""
    (tmp_path / "adapters").mkdir()

    result = adapters.run_adapter("test", tmp_path)

    assert result.status == "absent"


def test_absent_when_adapters_dir_itself_is_missing(tmp_path):
    result = adapters.run_adapter("test", tmp_path)

    assert result.status == "absent"


# ── timeout ──

def test_timeout_status(tmp_path):
    _write_script(
        _adapter_dir(tmp_path) / "healthcheck",
        "import time\ntime.sleep(5)\n",
    )

    result = adapters.run_adapter("healthcheck", tmp_path, timeout_s=1)

    assert result.status == "timeout"
    assert result.data is None
    assert "1s" in result.detail


# ── bad_output ──

def test_bad_output_when_stdout_is_not_json(tmp_path):
    _write_script(
        _adapter_dir(tmp_path) / "deploy",
        "print('not json at all')\n",
    )

    result = adapters.run_adapter("deploy", tmp_path)

    assert result.status == "bad_output"
    assert result.data is None
    assert result.raw_tail is not None
    assert "not json at all" in result.raw_tail


def test_bad_output_when_stdout_is_empty(tmp_path):
    _write_script(_adapter_dir(tmp_path) / "latency", "pass\n")

    result = adapters.run_adapter("latency", tmp_path)

    assert result.status == "bad_output"


def test_bad_output_when_json_is_not_an_object(tmp_path):
    _write_script(
        _adapter_dir(tmp_path) / "latency",
        "import json\nprint(json.dumps([1, 2, 3]))\n",
    )

    result = adapters.run_adapter("latency", tmp_path)

    assert result.status == "bad_output"
    assert "object" in result.detail


# ── nonzero_exit ──

def test_nonzero_exit_status(tmp_path):
    _write_script(
        _adapter_dir(tmp_path) / "test",
        "import sys\nprint('boom', file=sys.stderr)\nsys.exit(2)\n",
    )

    result = adapters.run_adapter("test", tmp_path)

    assert result.status == "nonzero_exit"
    assert result.exit_code == 2
    assert result.data is None
    assert "boom" in result.detail


def test_nonzero_exit_even_with_valid_json_stdout(tmp_path):
    """Exit code wins over stdout shape — a non-zero exit is `nonzero_exit` even when
    the adapter printed a perfectly well-formed JSON object first."""
    _write_script(
        _adapter_dir(tmp_path) / "smoke",
        "import json\nprint(json.dumps({'pass': False}))\nraise SystemExit(1)\n",
    )

    result = adapters.run_adapter("smoke", tmp_path)

    assert result.status == "nonzero_exit"
    assert result.exit_code == 1


# ── misc contract details ──

def test_adapter_path_resolution(tmp_path):
    assert adapters.adapter_path(tmp_path, "test") == tmp_path / "adapters" / "test"


def test_known_and_required_kinds_shape():
    assert adapters.KNOWN_KINDS == ("test", "eval", "smoke", "deploy", "healthcheck", "latency")
    assert adapters.REQUIRED_KINDS == ("test",)
