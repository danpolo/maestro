"""A2: the main loop samples usage through the backend driver and writes
`.orchestrator/usage.json` itself.

Before this, the file was written only by the operator's interactive Claude Code
statusline hook (`~/.claude/statusline-command.sh:27-51`), so `quota.get_effective_cap`
saw a missing file on any unattended run — `five_pct = 0` — and neither the concurrency
throttle nor the `PAUSE_PCT` (92%) pause could ever fire. Both drivers already implement
`usage()` and declare `usage_telemetry=True`, and `backends.base.to_usage_json` already
renders the exact document shape `quota.py` reads; this wires those existing pieces into
the poll loop rather than building anything new.

Two properties are load-bearing here:

* **one sample per backend per poll** — `_under_usage_pressure`'s switch-pressure check
  and `_write_usage_sample` share a per-poll cache (`orchestrator.main`'s `usage_cache`),
  so a backend is never asked twice in the same poll. `CodexBackend.usage()` spawns a
  short-lived `codex app-server` subprocess to answer that call, so this is not free.
* **`None` is not zero** (G6) — a driver with no reading this poll (unmeasurable, or no
  usage_telemetry) leaves the file exactly as the last good write left it, rather than
  overwriting real data — this loop's own from an earlier poll, or the operator's
  statusline hook's — with a blank document.

Nothing here launches an agent: `get_backend` is stubbed everywhere it is reached, and
`_available_backends` is pinned so no test relies on a real CLI being on PATH.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from maestro import orchestrator
from maestro.backends.base import Capabilities, Usage, WindowUsage

CLAUDE = "claude"
CODEX = "codex"


class _FakeDriver:
    """A driver that reports a usage sample without touching a binary."""

    def __init__(self, used_pct: float | None, telemetry: bool = True,
                 context_used_pct: float | None = None):
        self._used_pct = used_pct
        self._telemetry = telemetry
        self._context_used_pct = context_used_pct

    def capabilities(self) -> Capabilities:
        return Capabilities(usage_telemetry=self._telemetry)

    def usage(self):
        if self._used_pct is None:
            return None
        return Usage(windows={300: WindowUsage(used_pct=self._used_pct)},
                     context_used_pct=self._context_used_pct)


def _entry(tmp_path: Path, *, sid="impl-T1-1", task_id="T1", backend=CLAUDE) -> dict:
    """An in_flight implementer entry whose worktree really exists, as a live one's would."""
    worktree = tmp_path / f"wt-{sid}"
    worktree.mkdir(parents=True, exist_ok=True)
    return {
        "session_id": sid,
        "task_id": task_id,
        "role": "implementer",
        "worktree": str(worktree),
        "window": f"impl-{task_id}",
        "started_at": "2026-08-12T00:00:00Z",
        "status": "running",
        "backend": backend,
    }


@pytest.fixture(autouse=True)
def _usage_json_in_tmp(monkeypatch, tmp_path):
    """Never let this module touch the real project's `.orchestrator/usage.json`."""
    monkeypatch.setattr(orchestrator, "USAGE_JSON", tmp_path / "usage.json")


# =======================================================================================
# `_write_usage_sample`
# =======================================================================================


def test_write_usage_sample_writes_a_real_reading(monkeypatch, tmp_path):
    monkeypatch.setattr(orchestrator, "get_backend", lambda name: _FakeDriver(87.0))

    orchestrator._write_usage_sample([_entry(tmp_path)], {})

    doc = json.loads(orchestrator.USAGE_JSON.read_text())
    assert doc["five_hour"]["used_pct"] == 87
    assert doc["context_used_pct"] is None             # not measurable here, never 0
    assert doc["seven_day"] is None


def test_write_usage_sample_never_turns_an_unmeasurable_field_into_zero(monkeypatch, tmp_path):
    """G6: a driver that *has* a rate-limit sample but cannot compute context% must not
    have that turned into 0 on the way to disk."""
    monkeypatch.setattr(
        orchestrator, "get_backend",
        lambda name: _FakeDriver(60.0, context_used_pct=None),
    )

    orchestrator._write_usage_sample([_entry(tmp_path)], {})

    doc = json.loads(orchestrator.USAGE_JSON.read_text())
    assert doc["context_used_pct"] is None
    assert doc["five_hour"]["used_pct"] == 60


def test_write_usage_sample_leaves_the_file_untouched_without_a_reading(monkeypatch, tmp_path):
    """A missing sample must never overwrite good data already on disk — including data
    the operator's statusline hook wrote (constraint: leave that hook working)."""
    existing = {"context_used_pct": 12, "context_total_input_tokens": 100,
                "five_hour": {"used_pct": 40, "resets_at": 111}, "seven_day": None,
                "model": "claude", "updated_at": "2026-08-29T00:00:00Z", "windows": {}}
    orchestrator.USAGE_JSON.write_text(json.dumps(existing))
    monkeypatch.setattr(orchestrator, "get_backend", lambda name: _FakeDriver(None))

    orchestrator._write_usage_sample([_entry(tmp_path)], {})

    assert json.loads(orchestrator.USAGE_JSON.read_text()) == existing


def test_write_usage_sample_skips_a_backend_with_no_telemetry(monkeypatch, tmp_path):
    monkeypatch.setattr(
        orchestrator, "get_backend", lambda name: _FakeDriver(99.0, telemetry=False)
    )

    orchestrator._write_usage_sample([_entry(tmp_path)], {})

    assert not orchestrator.USAGE_JSON.exists()


def test_write_usage_sample_reuses_a_cache_already_populated_for_the_pressure_check(
    monkeypatch, tmp_path
):
    """The switch-pressure check and the usage.json write share one per-poll cache — a
    backend already sampled for the switch decision is never sampled again for the write."""
    calls: list[str] = []

    def _get_backend(name):
        calls.append(name)
        return _FakeDriver(70.0)

    monkeypatch.setattr(orchestrator, "get_backend", _get_backend)
    cache: dict = {}
    orchestrator._under_usage_pressure(CLAUDE, 10.0, cache)   # pressure check samples first
    orchestrator._write_usage_sample([_entry(tmp_path, backend=CLAUDE)], cache)

    assert calls == [CLAUDE]                                  # not sampled twice
    doc = json.loads(orchestrator.USAGE_JSON.read_text())
    assert doc["five_hour"]["used_pct"] == 70


def test_write_usage_sample_dedups_two_entries_on_the_same_backend(monkeypatch, tmp_path):
    calls: list[str] = []

    def _get_backend(name):
        calls.append(name)
        return _FakeDriver(55.0)

    monkeypatch.setattr(orchestrator, "get_backend", _get_backend)
    in_flight = [_entry(tmp_path, sid="impl-T1-1", backend=CLAUDE),
                 _entry(tmp_path, sid="impl-T2-1", task_id="T2", backend=CLAUDE)]

    orchestrator._write_usage_sample(in_flight, {})

    assert calls == [CLAUDE]


def test_write_usage_sample_picks_the_most_pressured_backend_regardless_of_order(
    monkeypatch, tmp_path
):
    """Fix round 1, finding 2: `usage.json` is one document, but `get_effective_cap`
    applies whatever `five_hour` it finds there as the *global* cap/pause for every
    backend. With two backends genuinely in flight, whichever one simply landed last in
    `in_flight` must not decide that number — the more-exhausted backend must, so the
    written reading is always the conservative one (throttle early, never late).

    The heavily-pressured backend is placed *first* here and the lightly-pressured one
    *last* — the opposite of what "last write wins" would need to get this right by
    accident.
    """
    drivers = {CLAUDE: _FakeDriver(90.0), CODEX: _FakeDriver(20.0)}
    monkeypatch.setattr(orchestrator, "get_backend", lambda name: drivers[name])
    in_flight = [_entry(tmp_path, sid="impl-T1-1", backend=CLAUDE),
                 _entry(tmp_path, sid="impl-T2-1", task_id="T2", backend=CODEX)]

    orchestrator._write_usage_sample(in_flight, {})

    doc = json.loads(orchestrator.USAGE_JSON.read_text())
    assert doc["five_hour"]["used_pct"] == 90


def test_write_usage_sample_an_unmeasurable_sample_never_outranks_a_measurable_one(
    monkeypatch, tmp_path
):
    """G6 applied to the multi-backend choice: a sample with no measurable pressure must
    not be treated as `0` pressure and must not win the comparison by accident — nor,
    conversely, must it ever lose to a phantom `0` it never had. A real reading, however
    small, always outranks "not measurable"."""
    unmeasurable = Usage(windows={300: WindowUsage(used_pct=None)})
    drivers = {
        CLAUDE: SimpleNamespace(capabilities=lambda: Capabilities(usage_telemetry=True),
                                usage=lambda: unmeasurable),
        CODEX: _FakeDriver(55.0),
    }
    monkeypatch.setattr(orchestrator, "get_backend", lambda name: drivers[name])
    in_flight = [_entry(tmp_path, sid="impl-T1-1", backend=CLAUDE),
                 _entry(tmp_path, sid="impl-T2-1", task_id="T2", backend=CODEX)]

    orchestrator._write_usage_sample(in_flight, {})

    doc = json.loads(orchestrator.USAGE_JSON.read_text())
    assert doc["five_hour"]["used_pct"] == 55


def test_write_usage_sample_reports_a_write_failure_instead_of_swallowing_it(
    monkeypatch, tmp_path, capsys
):
    """Fix round 1, finding 1: an unwritable `.orchestrator/` (or any other `OSError`)
    must leave a breadcrumb, like every other throttle/switch outcome in this module —
    not vanish with no operator signal, which would silently reintroduce "no quota
    protection" (A2's own target bug)."""
    monkeypatch.setattr(orchestrator, "get_backend", lambda name: _FakeDriver(70.0))
    monkeypatch.setattr(orchestrator, "USAGE_JSON", tmp_path / "no-such-dir" / "usage.json")

    orchestrator._write_usage_sample([_entry(tmp_path)], {})   # must not raise

    assert "usage.json" in capsys.readouterr().out


# =======================================================================================
# The whole poll, through `main`
# =======================================================================================


@pytest.fixture
def loop_env(monkeypatch, tmp_path):
    """One poll cycle of `main`, every non-usage seam stubbed. Nothing here reaches tmux,
    Telegram, the roadmap or a backend binary."""
    box = SimpleNamespace(state={"in_flight": [], "retry_counts": {}}, polls=0,
                          cap=(orchestrator.CONCURRENCY_CAP, 10.0), in_flight=[], journal=[])

    def _read_state():
        return dict(box.state)

    def _write_state(new):
        box.state.update(new)

    def _cap():
        box.polls += 1
        if box.polls > 1:                      # one cycle, then stop the loop
            box.state["halted"] = True
        return box.cap

    for name in ("run_dep_map", "run_status", "poll_control_commands", "_send_hitl_reminders",
                 "poll_proposal_answer", "apply_ready_self_fixes", "apply_ready_redo",
                 "poll_awaiting_verifications", "maybe_push_roadmap_map_change",
                 "notify_telegram"):
        monkeypatch.setattr(orchestrator, name, lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "parse_runnable_tasks", lambda: [])
    monkeypatch.setattr(orchestrator, "parse_prep_tasks", lambda: [])
    monkeypatch.setattr(orchestrator, "get_completed_task_ids", lambda: set())
    monkeypatch.setattr(orchestrator, "get_task_by_id", lambda tid: None)
    monkeypatch.setattr(
        orchestrator, "append_journal",
        lambda event, detail="", session_id="": box.journal.append((event, detail, session_id)),
    )
    monkeypatch.setattr(orchestrator, "read_state", _read_state)
    monkeypatch.setattr(orchestrator, "write_state", _write_state)
    monkeypatch.setattr(orchestrator, "get_effective_cap", _cap)
    monkeypatch.setattr(orchestrator, "HALT_FILE", tmp_path / "no-halt")
    monkeypatch.setattr(orchestrator, "ROADMAP_FILE", tmp_path / "no-roadmap.md")
    monkeypatch.setattr(orchestrator, "AUTO_PROPOSE", False)
    monkeypatch.setattr(orchestrator, "_available_backends", lambda: (CLAUDE,))
    monkeypatch.setattr(
        orchestrator, "subprocess",
        SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr="")),
    )
    monkeypatch.setattr(
        orchestrator, "time", SimpleNamespace(time=lambda: 1000.0, sleep=lambda s: None)
    )
    monkeypatch.setattr(
        orchestrator, "reconcile_in_flight", lambda state, launch_times: list(box.in_flight)
    )
    return box


def test_main_writes_a_real_sample_with_no_pre_existing_file(loop_env, monkeypatch, tmp_path):
    """The observable symptom this item fixes: with no interactive session (nothing else
    writing usage.json) and no pre-existing file, one poll of the main loop leaves a real
    sampled reading on disk."""
    assert not orchestrator.USAGE_JSON.exists()
    monkeypatch.setattr(orchestrator, "get_backend", lambda name: _FakeDriver(83.0))
    loop_env.in_flight = [_entry(tmp_path)]

    assert orchestrator.main() == 0

    doc = json.loads(orchestrator.USAGE_JSON.read_text())
    assert doc["five_hour"]["used_pct"] == 83


def test_main_leaves_usage_json_absent_when_nothing_is_measurable(loop_env, monkeypatch, tmp_path):
    """No sample this poll must not fabricate a document — absent stays absent."""
    monkeypatch.setattr(orchestrator, "get_backend", lambda name: _FakeDriver(None))
    loop_env.in_flight = [_entry(tmp_path)]

    assert orchestrator.main() == 0

    assert not orchestrator.USAGE_JSON.exists()


def test_main_never_samples_a_backend_more_than_once_per_poll(loop_env, monkeypatch, tmp_path):
    """Constraint: a driver's `usage()` can spawn a subprocess (CodexBackend's
    `codex app-server`). The whole poll — the pressure check on every in-flight entry,
    plus the usage.json write — must ask a given backend for it at most once."""
    calls: list[str] = []

    def _get_backend(name):
        calls.append(name)
        return _FakeDriver(95.0)            # above SWITCH_THRESHOLD_PCT

    monkeypatch.setattr(orchestrator, "get_backend", _get_backend)
    loop_env.cap = (0, 95.0)
    loop_env.in_flight = [
        _entry(tmp_path, sid="impl-T1-1", task_id="T1", backend=CLAUDE),
        _entry(tmp_path, sid="impl-T2-1", task_id="T2", backend=CLAUDE),
    ]

    assert orchestrator.main() == 0

    assert calls.count(CLAUDE) == 1
    doc = json.loads(orchestrator.USAGE_JSON.read_text())
    assert doc["five_hour"]["used_pct"] == 95
