#!/usr/bin/env python3
"""Run two backend-free ``maestro init`` rehearsals in disposable Git projects."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def _run(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None, timeout: int = 20):
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _seed(root: Path, kind: str) -> None:
    root.mkdir(parents=True)
    _run(["git", "init", "-q"], cwd=root).check_returncode()
    _run(["git", "config", "user.email", "fixture@example.invalid"], cwd=root).check_returncode()
    _run(["git", "config", "user.name", "Fixture"], cwd=root).check_returncode()
    if kind == "python":
        (root / "pyproject.toml").write_text(
            '[project]\nname="rehearsal"\nversion="0"\n\n[tool.pytest.ini_options]\ntestpaths=["tests"]\n',
            encoding="utf-8",
        )
        (root / "tests").mkdir()
        (root / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    elif kind == "node":
        (root / "package.json").write_text(
            json.dumps({"private": True, "scripts": {"test": "node --test"}}) + "\n",
            encoding="utf-8",
        )
        (root / "fixture.test.js").write_text(
            "const test = require('node:test');\nconst assert = require('node:assert');\n"
            "test('ok', () => assert.equal(2 + 2, 4));\n",
            encoding="utf-8",
        )
    else:
        raise ValueError(kind)
    _run(["git", "add", "."], cwd=root).check_returncode()
    _run(["git", "commit", "-q", "-m", "fixture"], cwd=root).check_returncode()


def _rehearse(root: Path, kind: str) -> dict[str, object]:
    _seed(root, kind)
    env = dict(os.environ)
    env.pop("MAESTRO_REPO", None)
    env.pop("MAESTRO_HOME", None)
    env["MAESTRO_TOKEN_FILE"] = str(root / "absent-telegram-token")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO), env.get("PYTHONPATH", "")]))
    init = _run(
        [sys.executable, "-m", "maestro.cli", "init", str(root)],
        cwd=REPO,
        env=env,
        timeout=180,
    )
    adapter_path = root / "adapters" / "test"
    adapter = (
        _run([str(adapter_path)], cwd=root, env=env, timeout=60)
        if adapter_path.is_file()
        else None
    )
    journal_path = root / ".orchestrator" / "journal.ndjson"
    events = []
    if journal_path.is_file():
        events = [json.loads(line)["event"] for line in journal_path.read_text(encoding="utf-8").splitlines()]
    unit_path = root / "systemd" / f"{root.name}-watchdog.service"
    unit = unit_path.read_text(encoding="utf-8") if unit_path.is_file() else ""
    detected = [line for line in init.stdout.splitlines() if "detected test command" in line]
    return {
        "kind": kind,
        "init_exit_code": init.returncode,
        "test_adapter_exit_code": adapter.returncode if adapter else None,
        "detected_test_command": detected[-1] if detected else "",
        "queue_exhausted": "queue_exhausted" in events,
        "halt_respected": "halt_respected" in events,
        "service_type_forking": "Type=forking" in unit,
        "service_direct_tmux": "ExecStart=/usr/bin/tmux new-session" in unit,
        "init_output_tail": (init.stdout + init.stderr)[-1200:],
        "adapter_output_tail": (adapter.stdout + adapter.stderr)[-1200:] if adapter else "",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO / "artifacts" / "graph-engineering" / "init-rehearsal.json",
    )
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="maestro-init-rehearsal-") as raw:
        scratch = Path(raw)
        projects = []
        for kind in ("python", "node"):
            try:
                projects.append(_rehearse(scratch / kind, kind))
            except Exception as exc:
                projects.append(
                    {
                        "kind": kind,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "init_exit_code": None,
                        "test_adapter_exit_code": None,
                        "queue_exhausted": False,
                        "halt_respected": False,
                        "service_type_forking": False,
                        "service_direct_tmux": False,
                    }
                )
    report = {
        "schema_version": 1,
        "scope": "disposable-local-projects",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "backend_calls_started": 0,
        "projects": projects,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    return 0 if all(
        row["init_exit_code"] == row["test_adapter_exit_code"] == 0
        and row["queue_exhausted"]
        and row["halt_respected"]
        and row["service_type_forking"]
        and row["service_direct_tmux"]
        for row in projects
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
