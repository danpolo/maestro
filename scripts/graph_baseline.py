#!/usr/bin/env python3
"""Capture the P0 migration baseline as a JSON artifact.

`tests/graph_engineering/test_baseline_lanes.py` asserts that each lane behaves the way it
behaves. This script records *how much it costs* — the same seven lanes, the same harness,
but the numbers written out rather than asserted, so a later phase can diff against them
instead of re-deriving them from a test run's scrollback.

    .venv/bin/python scripts/graph_baseline.py
    .venv/bin/python scripts/graph_baseline.py --out artifacts/graph-engineering/baseline.json

Two properties the artifact inherits from the harness and which are the reason it is worth
keeping at all:

* **It is reproducible.** No wall clock, no network, no subprocess. Running this twice on
  two machines produces byte-identical `lanes` entries, so a diff against a later run
  measures the migration rather than the hardware. `--check` asserts exactly that against
  an artifact already on disk.
* **Every number is observed.** `observed_facts` and `inferred_facts` are reported per lane
  precisely so a reader can tell. A baseline whose `inferred_facts` is non-zero is a set of
  estimates wearing a measurement's clothes, and D06 says such a thing is not evidence.

Raw per-lane event sequences are retained in full, not just the summaries, because A03
requires B01/B18 to be re-analysable later and a summary cannot be re-analysed.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO / "artifacts" / "graph-engineering" / "baseline.json"

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _capture() -> dict:
    """Run every lane through the pytest fixtures and collect their traces.

    The lanes are driven through pytest rather than by importing the fixtures directly:
    `run_lane` depends on `lane_sandbox`, which depends on `tmp_path` and `monkeypatch`,
    and the session-wide credential and agent-CLI guards in `tests/conftest.py` are what
    make it safe to run this at all. Re-implementing that wiring here would mean
    maintaining a second copy of the isolation guarantees, and the copy that drifts is the
    one that writes to a live project.
    """
    import pytest

    collected: dict = {}

    class _Collector:
        """Pulls each lane's trace out of the test session as it finishes."""

        def pytest_runtest_makereport(self, item, call):
            if call.when != "call":
                return
            run = getattr(item, "_lane_run", None)
            if run is not None:
                collected[run.lane] = run

    # `capture_lane` is defined in the test module for the suite's own use; here it is
    # asked to stash each run on the item so the collector above can read it back.
    code = pytest.main(
        [
            str(REPO / "tests" / "graph_engineering" / "test_baseline_lanes.py"),
            "-q",
            "--no-header",
            "-p", "no:cacheprovider",
            "--baseline-capture",
        ],
        plugins=[_Collector()],
    )
    if code != 0:
        raise SystemExit(
            f"lane suite failed (pytest exit {code}) — a baseline captured from a failing "
            "harness measures the harness, not the orchestrator."
        )
    return {lane: run.trace for lane, run in sorted(collected.items())}


def _document(traces: dict) -> dict:
    return {
        "schema_version": 1,
        "phase": "P00",
        "captured_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lanes": {
            lane: {
                "summary": trace.summary(),
                # A03: raw outcomes, not summaries. A later phase re-analyses these.
                "events": [
                    {
                        "seq": event.seq,
                        "kind": event.kind,
                        "evidence": event.evidence,
                        "identity": event.identity,
                    }
                    for event in trace.events
                ],
            }
            for lane, trace in traces.items()
        },
    }


def _comparable(document: dict) -> dict:
    """The document minus the one field that is allowed to differ between runs."""
    return {key: value for key, value in document.items() if key != "captured_at"}


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help=f"where to write the baseline (default: {DEFAULT_OUT})")
    parser.add_argument("--check", action="store_true",
                        help="capture and compare against --out instead of overwriting it; "
                             "exit 1 if the lanes have changed")
    args = parser.parse_args(argv)

    document = _document(_capture())

    if args.check:
        if not args.out.exists():
            print(f"[FAIL] no baseline at {args.out} to check against")
            return 1
        stored = json.loads(args.out.read_text(encoding="utf-8"))
        if _comparable(stored) != _comparable(document):
            print(f"[FAIL] lanes differ from the baseline recorded in {args.out}")
            for lane in sorted(document["lanes"]):
                was = stored.get("lanes", {}).get(lane, {}).get("summary")
                now = document["lanes"][lane]["summary"]
                if was != now:
                    print(f"  {lane}:\n    was {was}\n    now {now}")
            return 1
        print(f"[OK] lanes match the baseline in {args.out}")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(f"[OK] wrote {len(document['lanes'])} lane baselines to {args.out}")
    for lane, entry in sorted(document["lanes"].items()):
        summary = entry["summary"]
        print(
            f"  {lane:<22} agents={summary['by_kind'].get('agent_call', 0):<2} "
            f"dup={summary['duplicate_agent_invocations']:<2} "
            f"checks={summary['by_kind'].get('test_run', 0):<2} "
            f"rerun={summary['unnecessary_test_reruns']:<2} "
            f"idle={summary['idle_wait_seconds']:>6.0f}s "
            f"active={summary['active_compute_seconds']:>6.0f}s "
            f"reads={summary['state_reads']:<3} writes={summary['state_writes']:<3} "
            f"inferred={summary['inferred_facts']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
