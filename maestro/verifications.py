"""B5.1 — the deterministic verification gate's parsing/execution engine.

Extracted verbatim from the reference `scripts/check_verifications.py` — bodies are
unchanged, only the import block and the derivation of the module-level path globals
differ. Behavioural surprises are catalogued in `docs/FOUND_BUGS.md` and pinned by
`tests/characterization/test_verifications.py`; none of them is fixed here.

The reference is a standalone CLI script: a `sys.argv`-driven `main()` that prints to
stdout and returns a process exit code, which `maestro/gates.py` used to invoke as a
subprocess. `main()` below keeps that shape exactly — it still reads `sys.argv` and
still prints rather than returning its result. `run_cli` is new wiring on top of it: it
drives `main()` with an explicit argv (temporarily substituted for `sys.argv`) and
captures what it prints, so `maestro.gates.run_verification_gate` can call straight
into this module in-process instead of shelling out to a script that may not exist.
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml  # PyYAML

from maestro.paths import Paths

_PATHS = Paths.from_env()

REPO         = _PATHS.repo
ROADMAP_FILE = REPO / "docs" / "ROADMAP.md"
VENV_PYTHON  = str(REPO / ".venv" / "bin" / "python3")


def _resolve_cmd(cmd: str) -> str:
    """Replace bare 'python'/'python3' at the start of a cmd with the venv python."""
    import re as _re
    return _re.sub(r"^(python3?)\b", VENV_PYTHON, cmd)


def get_task_verifications(task_id: str) -> list[dict]:
    content = ROADMAP_FILE.read_text(encoding="utf-8")
    blocks = re.findall(r"```yaml\n(.*?)```", content, re.DOTALL)
    for raw in blocks:
        try:
            task = yaml.safe_load(raw)
        except yaml.YAMLError:
            continue
        if not isinstance(task, dict):
            continue
        if str(task.get("id", "")) == str(task_id):
            return task.get("verifications") or []
    return []


def main() -> int:
    # --auto-only restricts the gate to kind:auto items (skips manual proof checks);
    # used by graduation of dispatch:manual tasks where there is no implementer result.json.
    auto_only = "--auto-only" in sys.argv[1:]
    argv = [a for a in sys.argv[1:] if a != "--auto-only"]
    if len(argv) < 2:
        print("Usage: check_verifications.py <task_id> <workspace_path> [worktree_path] [--auto-only]")
        return 1

    task_id   = argv[0]
    workspace = Path(argv[1])
    # auto-checks run against the implementer's worktree (changes are unmerged at gate
    # time, so they only exist there — not in REPO). Fall back to REPO for back-compat.
    check_cwd = Path(argv[2]) if len(argv) > 2 else REPO

    verifications = get_task_verifications(task_id)
    if not verifications:
        print(f"[check] No verifications defined for {task_id} — pass (no gate).")
        return 0

    result: dict = {}
    result_path = workspace / "result.json"
    if result_path.exists():
        try:
            result = json.loads(result_path.read_text())
        except Exception:
            pass

    impl_verifs: dict = result.get("verifications", {})

    failures: list[str] = []
    for v in verifications:
        vid  = str(v.get("id", ""))
        kind = v.get("kind", "manual")
        if auto_only and kind != "auto":
            continue

        if kind == "auto":
            cmd    = v.get("cmd", "")
            expect = v.get("expect", "")
            if not cmd:
                failures.append(f"{vid} (auto): no cmd defined in ROADMAP task")
                continue
            try:
                r = subprocess.run(
                    _resolve_cmd(cmd), shell=True, capture_output=True, text=True,
                    cwd=str(check_cwd), timeout=60
                )
                actual = r.stdout.strip()
                # Convention: an auto-check prints its sentinel token as the last
                # non-empty stdout line (most scripts print only the token; some emit
                # a human-readable summary first). Accept an exact match OR a last-line
                # match so a leading summary line doesn't false-fail a passing check —
                # the failure mode that silently discarded EVAL2's correct result.
                last_line = next((ln.strip() for ln in reversed(actual.splitlines())
                                  if ln.strip()), actual)
                if actual != expect and last_line != expect:
                    failures.append(
                        f"{vid} (auto): output mismatch\n"
                        f"    expected: {expect!r}\n"
                        f"    got:      {actual!r}"
                    )
            except subprocess.TimeoutExpired:
                failures.append(f"{vid} (auto): cmd timed out (60 s)")
            except Exception as exc:
                failures.append(f"{vid} (auto): cmd error: {exc}")

        elif kind == "manual":
            entry = impl_verifs.get(vid, {})
            done  = entry.get("done", False)
            proof = entry.get("proof") or ""
            if not done:
                failures.append(f"{vid} (manual): done=false — cannot write DONE sentinel.")
            elif not proof.strip():
                failures.append(f"{vid} (manual): done=true but proof is empty.")
        else:
            failures.append(f"{vid}: unknown kind={kind!r}")

    if failures:
        print(f"[check] {task_id}: {len(failures)} verification(s) FAILED:")
        for msg in failures:
            print(f"  ✗ {msg}")
        return 1

    print(f"[check] {task_id}: all {len(verifications)} verification(s) passed.")
    return 0


def run_cli(argv: list[str]) -> tuple[int, str]:
    """In-process equivalent of `python check_verifications.py <argv...>`.

    Substitutes `sys.argv` for the duration of the call — `main()` above is left
    untouched, reading `sys.argv` exactly as the reference script does — and captures
    whatever it prints instead of letting it hit the real stdout. Returns
    (exit_code, captured_output), the in-process analogue of a subprocess's
    (returncode, stdout)."""
    old_argv = sys.argv
    buf = io.StringIO()
    try:
        sys.argv = ["check_verifications.py", *argv]
        with contextlib.redirect_stdout(buf):
            rc = main()
    finally:
        sys.argv = old_argv
    return rc, buf.getvalue()
