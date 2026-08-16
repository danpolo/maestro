"""The adapter invocation contract (DESIGN.md §4).

A project supplies thin executables under `adapters/<kind>` — `test`, `eval`, `smoke`,
`deploy`, `healthcheck`, `latency` — each speaking a fixed JSON-stdin -> JSON-stdout
protocol. Maestro owns orchestration; the project owns what good looks like. This module
is the one seam that crosses that boundary: it resolves an adapter, runs it, and turns
whatever happens (missing file, timeout, garbage output, non-zero exit, success) into a
typed `AdapterResult` — never a raised exception for an expected outcome.

`test` is the only *required* adapter (§4's table). A missing `test` adapter is still
reported as `status="absent"` here — this module never raises for a missing file, kind
included, per the plan's "test adapter missing is an error status, other kinds'
absence is the normal 'absent' status, not an exception". It is the *caller's* job
(`doctor`, `gates.py`) to treat `absent` as fatal for `test` and as
`verdict=unevaluated` (D8) for everything else; this module has no opinion on that
policy, only on what happened when it tried to run the thing.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

#: The adapter kinds DESIGN.md §4's table defines. Not enforced by `run_adapter` itself
#: (an unknown kind just resolves to a path that doesn't exist and comes back "absent")
#: but kept here as the single documented list other modules can import instead of
#: re-typing the six strings.
KNOWN_KINDS = ("test", "eval", "smoke", "deploy", "healthcheck", "latency")

#: The only adapter whose absence is a project-configuration problem rather than an
#: expected "not wired up yet" state. Callers (not this module) decide what to do with
#: that fact; it's exposed here so they don't have to hardcode `"test"` themselves.
REQUIRED_KINDS = ("test",)

AdapterStatus = Literal["ok", "absent", "timeout", "bad_output", "nonzero_exit"]

#: Bytes of stdout captured for `bad_output`'s `raw_tail`, so `doctor` has something to
#: show without risking an unbounded string from a runaway adapter.
_RAW_TAIL_LIMIT = 2000


@dataclass(frozen=True)
class AdapterResult:
    """The outcome of one `run_adapter` call.

    `data` is the parsed JSON object on `status="ok"`, `None` otherwise. `raw_tail` is
    only populated for `bad_output` (the last `_RAW_TAIL_LIMIT` bytes of stdout, for a
    human to look at) and `detail` is a short human-readable reason for every non-`ok`
    status ("adapter not found", "timed out after 30s", the subprocess's stderr tail
    for `nonzero_exit`, etc).
    """

    kind: str
    status: AdapterStatus
    data: Optional[dict] = None
    exit_code: Optional[int] = None
    detail: str = ""
    raw_tail: Optional[str] = None


def adapter_path(project_root: Path, kind: str) -> Path:
    """Where `kind`'s adapter would live under `project_root`, per DESIGN.md §4."""
    return Path(project_root) / "adapters" / kind


def run_adapter(
    kind: str,
    project_root: Path,
    payload: Optional[dict] = None,
    timeout_s: int = 120,
) -> AdapterResult:
    """Run `adapters/<kind>` under `project_root` with `payload` (default `{}`) as JSON
    on stdin, and parse a JSON object from stdout.

    Never raises for an expected outcome — every failure mode DESIGN.md §4 and the plan
    call out becomes a status on the returned `AdapterResult` instead:

    - `absent`: the file doesn't exist, or exists but isn't executable. This is the
      normal state for a project that hasn't wired up an optional adapter yet (D8);
      it is *not* an exception here even for `kind="test"` — the plan's "missing test
      adapter is an error" is a policy `doctor`/`gates.py` applies to this status, not
      a different code path in this function.
    - `timeout`: the process didn't exit within `timeout_s` seconds; it is killed.
    - `bad_output`: the process exited 0 but stdout wasn't a single parseable JSON
      object (empty, truncated, not JSON, or JSON that isn't an object).
    - `nonzero_exit`: the process exited non-zero, regardless of what it printed.
    - `ok`: exit 0 and stdout parsed as a JSON object; `data` holds it.
    """
    path = adapter_path(project_root, kind)

    if not path.is_file() or not os.access(path, os.X_OK):
        return AdapterResult(
            kind=kind,
            status="absent",
            detail=f"{path} does not exist or is not executable",
        )

    stdin_bytes = json.dumps(payload or {}).encode("utf-8")

    try:
        proc = subprocess.run(
            [str(path)],
            input=stdin_bytes,
            capture_output=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return AdapterResult(
            kind=kind,
            status="timeout",
            detail=f"{path} did not exit within {timeout_s}s",
        )
    except OSError as exc:
        # e.g. the exec bit lied (a directory, a binary format the kernel rejects) —
        # still "the adapter is broken", not this function's problem to raise.
        return AdapterResult(kind=kind, status="absent", detail=f"{path} could not be run: {exc}")

    stdout_text = proc.stdout.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        stderr_tail = proc.stderr.decode("utf-8", errors="replace")[-_RAW_TAIL_LIMIT:]
        return AdapterResult(
            kind=kind,
            status="nonzero_exit",
            exit_code=proc.returncode,
            detail=stderr_tail.strip() or f"{path} exited {proc.returncode}",
            raw_tail=stdout_text[-_RAW_TAIL_LIMIT:],
        )

    try:
        data = json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        return AdapterResult(
            kind=kind,
            status="bad_output",
            exit_code=proc.returncode,
            detail=f"stdout was not valid JSON: {exc}",
            raw_tail=stdout_text[-_RAW_TAIL_LIMIT:],
        )

    if not isinstance(data, dict):
        return AdapterResult(
            kind=kind,
            status="bad_output",
            exit_code=proc.returncode,
            detail=f"stdout parsed as JSON but was a {type(data).__name__}, not an object",
            raw_tail=stdout_text[-_RAW_TAIL_LIMIT:],
        )

    return AdapterResult(kind=kind, status="ok", data=data, exit_code=proc.returncode)
