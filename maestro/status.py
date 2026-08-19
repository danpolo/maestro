"""Free `/status` reader — zero model/Claude tokens; pure file reads.

Extracted verbatim from the reference `scripts/orchestrator_status.py` (231 lines): a
standalone, `sys.argv`-driven CLI script (`print_status()` / `print_json_status()` /
`main()`), not a function living inside `orchestrator_run.py`. Function bodies are
unchanged; only the import block and the derivation of the module-level path globals
differ — `STATE_JSON`/`USAGE_JSON`/`JOURNAL`/`ROADMAP` come from `maestro.paths.Paths.
from_env()` instead of `Path(__file__).resolve().parent.parent`, exactly as every other
extracted module does. Behavioural surprises are catalogued in `docs/FOUND_BUGS.md`; none
of them is fixed here.

`run_cli()` and `get_status_report()` are new wiring on top of the verbatim port, the same
shape as `maestro.verifications.run_cli`: `main()` above is left untouched, reading
`sys.argv` and printing exactly as the reference script does; `run_cli()` substitutes
`sys.argv` for the duration of the call and captures whatever `main()` prints instead of
letting it hit real stdout, so callers that used to shell out to this script as a
subprocess (`maestro.hitl.commands.run_status`, `maestro.parking.run_status`) can call
straight into it in-process instead. `get_status_report()` drives `run_cli(["--json"])` and
parses the captured stdout back into a dict, for callers that want the report as data
rather than text.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from maestro.paths import Paths

_PATHS = Paths.from_env()

REPO       = _PATHS.repo
STATE_JSON = _PATHS.state
USAGE_JSON = _PATHS.usage
JOURNAL    = _PATHS.journal
ROADMAP    = REPO / "docs" / "ROADMAP.md"

_YAML_BLOCK = re.compile(r"```yaml\s*\n(.*?)\n```", re.DOTALL)


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _parse_roadmap_tasks() -> list[dict]:
    if not ROADMAP.exists():
        return []
    tasks: list[dict] = []
    for block in _YAML_BLOCK.findall(ROADMAP.read_text()):
        try:
            import yaml
            data = yaml.safe_load(block)
        except Exception:
            data = _manual_yaml(block)
        if not isinstance(data, dict) or "id" not in data:
            continue
        if isinstance(data.get("deps"), str):
            inner = data["deps"].strip().lstrip("[").rstrip("]").strip()
            data["deps"] = [d.strip().strip("'\"") for d in inner.split(",") if d.strip()]
        data.setdefault("deps", [])
        tasks.append(data)
    return tasks


def _manual_yaml(text: str) -> dict:
    out: dict = {}
    for line in text.splitlines():
        line = line.rstrip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if key == "deps":
            inner = val.strip().lstrip("[").rstrip("]").strip()
            out[key] = [d.strip().strip("'\"") for d in inner.split(",") if d.strip()]
        else:
            out[key] = val.strip().strip("'\"")
    return out


def _fmt_epoch(epoch) -> str:
    if epoch is None:
        return "—"
    # Accept ISO strings (paused_until) or numeric epochs (resets_at).
    try:
        dt = datetime.fromisoformat(str(epoch).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError):
        pass
    try:
        dt = datetime.fromtimestamp(float(epoch), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(epoch)


def _pct(val) -> str:
    if val is None:
        return "—"
    try:
        return f"{float(val):.1f}%"
    except Exception:
        return str(val)


def _task_readiness(tasks: list[dict], active_ids: set[str]) -> list[dict]:
    done_ids: set[str] = set()
    rows = []
    for t in tasks:
        if t["id"] in active_ids:
            status = "ACTIVE"
        elif not t.get("deps") or all(d in done_ids for d in t["deps"]):
            if t.get("mode") == "needs-dan":
                status = "ready (needs-dan)"
            else:
                status = "READY"
        else:
            status = f"blocked on: {', '.join(t.get('deps', []))}"
        rows.append({"id": t["id"], "title": t.get("title", ""), "status": status,
                     "mode": t.get("mode", "?"), "eval": t.get("eval_relevance", "?")})
    return rows


def print_status() -> None:
    state = _load_json(STATE_JSON)
    usage = _load_json(USAGE_JSON)
    tasks = _parse_roadmap_tasks()

    print("=" * 60)
    print("  ORCHESTRATOR STATUS")
    print("=" * 60)

    if state is None:
        print("\n[!] .orchestrator/state.json not found — runtime not initialized.\n")
    else:
        phase = state.get("phase") or {}
        print(f"\nPhase:        {phase.get('id') or '—'}  {phase.get('title') or ''}")
        print(f"Status:       {phase.get('status', '?')}")
        print(f"Halted:       {'YES' if state.get('halted') else 'no'}")
        print(f"Paused until: {_fmt_epoch(state.get('paused_until'))}")
        print(f"Paused by user: {'yes' if state.get('paused_by_user') else 'no'}")
        blocked = state.get("blocked_on") or []
        if blocked:
            print(f"Blocked on:   {', '.join(blocked)}")
        print(f"Queue ref:    {state.get('queue_ref', '?')}")
        print(f"Updated at:   {state.get('updated_at', '?')}")

        in_flight = state.get("in_flight") or []
        print(f"\nIn-flight ({len(in_flight)}):")
        if not in_flight:
            print("  (none)")
        else:
            for r in in_flight:
                print(f"  [{r.get('status','?'):8}] {r.get('task_id','?')}  role={r.get('role','?')}"
                      f"  started={r.get('started_at','?')}  window={r.get('window') or '—'}")

    print("\n--- Usage (statusline sampler) ---")
    if usage is None:
        print("  .orchestrator/usage.json not found.")
    else:
        upd = usage.get("updated_at") or "never"
        model = usage.get("model") or "—"
        print(f"  Model:   {model}")
        print(f"  Updated: {upd}")
        print(f"  Context: {_pct(usage.get('context_used_pct'))}")
        fh = usage.get("five_hour") or {}
        print(f"  5h limit: {_pct(fh.get('used_pct'))}  resets {_fmt_epoch(fh.get('resets_at'))}")
        sd = usage.get("seven_day") or {}
        print(f"  7d limit: {_pct(sd.get('used_pct'))}  resets {_fmt_epoch(sd.get('resets_at'))}")

    print("\n--- Task queue (docs/ROADMAP.md) ---")
    if not tasks:
        print("  (no tasks or ROADMAP.md missing)")
    else:
        active_ids: set[str] = set()
        if state:
            for row in state.get("in_flight", []) or []:
                if isinstance(row, dict) and row.get("task_id"):
                    active_ids.add(row["task_id"])
            ph = state.get("phase") or {}
            if isinstance(ph, dict) and ph.get("status") == "in_progress" and ph.get("id"):
                active_ids.add(ph["id"])

        rows = _task_readiness(tasks, active_ids)
        for r in rows:
            status_col = r["status"].upper() if r["status"] in ("READY", "ACTIVE") else r["status"]
            flag = "*" if r["status"] == "ACTIVE" else (" " if "READY" in r["status"] else " ")
            print(f"  {flag} {r['id']:6}  [{r['mode']:9} | {r['eval']:14}]  {status_col}")

    print()

    if JOURNAL.exists():
        lines = JOURNAL.read_text().strip().splitlines()
        if lines:
            try:
                last = json.loads(lines[-1])
                print(f"Last journal event: [{last.get('ts','')}] {last.get('event','')} — {last.get('detail','')}")
            except Exception:
                pass
    print()


def print_json_status() -> None:
    state = _load_json(STATE_JSON)
    usage = _load_json(USAGE_JSON)
    tasks = _parse_roadmap_tasks()
    active_ids: set[str] = set()
    if state:
        for row in state.get("in_flight", []) or []:
            if isinstance(row, dict) and row.get("task_id"):
                active_ids.add(row["task_id"])
        ph = state.get("phase") or {}
        if isinstance(ph, dict) and ph.get("status") == "in_progress" and ph.get("id"):
            active_ids.add(ph["id"])
    print(json.dumps({
        "state": state,
        "usage": usage,
        "tasks": _task_readiness(tasks, active_ids),
    }, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="output machine-readable JSON")
    args = ap.parse_args()
    if args.json:
        print_json_status()
    else:
        print_status()
    return 0


def run_cli(argv: list[str]) -> tuple[int, str]:
    """In-process equivalent of `python orchestrator_status.py <argv...>`.

    Substitutes `sys.argv` for the duration of the call — `main()` above is left
    untouched, reading `sys.argv` exactly as the reference script does — and captures
    whatever it prints instead of letting it hit the real stdout. Returns
    (exit_code, captured_output), the in-process analogue of a subprocess's
    (returncode, stdout)."""
    old_argv = sys.argv
    buf = io.StringIO()
    try:
        sys.argv = ["orchestrator_status.py", *argv]
        with contextlib.redirect_stdout(buf):
            rc = main()
    finally:
        sys.argv = old_argv
    return rc, buf.getvalue()


def get_status_report() -> dict:
    """The `--json` report as a Python dict — the in-process analogue of parsing
    `orchestrator_status.py --json`'s stdout. Drives the same `run_cli` seam."""
    _rc, output = run_cli(["--json"])
    return json.loads(output)


if __name__ == "__main__":
    raise SystemExit(main())
