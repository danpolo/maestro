"""Pinned behaviour of the supervision layer: liveness, stall detection, resume, reaping.

Characterisation, not specification: where the reference implementation does something
surprising, the test pins the surprise and `docs/FOUND_BUGS.md` records it. Nothing here
asserts what the code *should* do.

Three conventions are specific to this module:

* the watchdog is a **separate top-level script** in the reference
  (`scripts/watchdog.py`), and — unlike the prepared-action sidecar — the monolithic
  orchestrator module does not import it at all: it only mentions it in comments. So the
  dual `subject` fixture's legacy half hands back a namespace that does not own any of
  these functions. The `wd` fixture normalises that to "the module that owns
  `journal_last_line`" by loading the reference script by file path (via
  `importlib.util.spec_from_file_location`, with the repo located through
  `tests.reference_repo`, never hardcoded), and by handing back the subject itself once
  `maestro.watchdog` exists;
* the shared `sandbox` fixture cannot help here. It rebases the path globals of
  *`subject`* — the orchestrator module — and the watchdog module it never looks at would
  keep every one of its own globals (`STATE_JSON`, `JOURNAL`, `HALT_FILE`, `QUESTIONS`,
  `LAUNCHER`, `VENV_PYTHON`, `NOTIFY_SCRIPT`) pointed at the live reference repo. A single
  unstubbed call would then create the production HALT sentinel or append to the
  production journal. Every test therefore gets the autouse `box` fixture, which rebases
  every `Path`-valued global of the watchdog module into a temp tree by reflection (never
  from a hand-written list of names) and asserts, on the way in *and* on the way out, that
  not one of them resolves outside that tree;
* every function here shells out. The autouse `runs` fixture replaces `subprocess.run`
  with a recorder for the duration of each test and makes `Popen`/`call`/`check_output`/
  `check_call` raise, so no test can spawn tmux, crontab, pgrep, pkill, `at`, python or an
  agent CLI, and `notify` cannot reach a real notifier (both subjects deliver through
  `subprocess`). The autouse `clock` fixture replaces the module's `time` so no test can
  really sleep and `main()`'s monotonic clock is fully controlled.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.reference_repo import LEGACY_REPO

pytestmark = pytest.mark.maestro_module("watchdog")


# --- locating the subject ---------------------------------------------------------------

_LEGACY_CACHE: dict[str, object] = {}


def _load_legacy_watchdog():
    """Import the reference's watchdog script by path. Skips when the repo is absent.

    Unlike the orchestrator module this one has no import-time side effects worth
    guarding — no `sys.path` insertion and no `load_dotenv`, so no credential can enter
    the process here. Loading it under the name `_legacy_watchdog` also keeps its
    `__name__ == "__main__"` guard from running `main()`.
    """
    if LEGACY_REPO is None:
        pytest.skip(
            "reference repo unknown: set $MAESTRO_LEGACY_REPO or write its path to .legacy_repo"
        )
    path = LEGACY_REPO / "scripts" / "watchdog.py"
    if not path.is_file():
        pytest.skip(f"reference watchdog not found at {path}")
    cached = _LEGACY_CACHE.get("module")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("_legacy_watchdog", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _LEGACY_CACHE["module"] = module
    return module


def _is_maestro(subject) -> bool:
    return getattr(subject, "__name__", "").split(".")[0] == "maestro"


@pytest.fixture
def wd(subject):
    """The module that owns the watchdog functions, whichever subject is under test."""
    if _is_maestro(subject):
        return subject
    return _load_legacy_watchdog()


def _notifies_via_script(wd) -> bool:
    """True while the subject delivers alerts by shelling out to a notifier script.

    The reference runs `bash scripts/notify_telegram.sh <msg>`; the plan replaces that one
    path with the already-extracted Telegram notifier. Mechanism assertions branch on
    this, and neither branch is loosened: the reference's exact argv is pinned, and the
    extracted module is held to "prints the alert line and spawns nothing else".
    """
    return isinstance(getattr(wd, "NOTIFY_SCRIPT", None), Path)


# --- the sandbox ------------------------------------------------------------------------


def _path_globals(module) -> dict[str, Path]:
    return {
        name: value
        for name, value in vars(module).items()
        if not name.startswith("__") and isinstance(value, Path)
    }


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


@pytest.fixture(autouse=True)
def box(wd, tmp_path, monkeypatch):
    """Rebase every path global of the watchdog module into a temp tree.

    Autouse and not optional: an un-rebased global here is a write against a live
    system's `.orchestrator/`. The name list is never written by hand — every
    `Path`-valued module global is discovered by reflection and rebased relative to the
    module's own `REPO`, and anything that somehow sits outside `REPO` is parked under
    `_outside/` rather than left alone. The exit assertion catches a test that reassigns a
    global itself.
    """
    repo = tmp_path / "repo"
    orch = repo / ".orchestrator"
    (orch / "questions").mkdir(parents=True)

    root = getattr(wd, "REPO", None)
    assert isinstance(root, Path), (
        "the watchdog module exposes no REPO global; the reflection-based rebase cannot "
        "run and every path global would still point at a live repo"
    )
    root = root.resolve()

    original = {name: value for name, value in _path_globals(wd).items()}
    for name, value in original.items():
        resolved = value if value.is_absolute() else (Path.cwd() / value)
        if _under(resolved, root):
            target = repo / resolved.relative_to(root)
        else:
            target = repo / "_outside" / name
        monkeypatch.setattr(wd, name, target)

    def _leaks() -> list[str]:
        return sorted(
            name
            for name, value in _path_globals(wd).items()
            if not _under(value if value.is_absolute() else Path.cwd() / value, tmp_path)
        )

    assert not _leaks(), (
        f"path globals still resolve outside the temp tree: {_leaks()}. "
        "Calling the subject now could write to a live system."
    )

    def _p(name: str, fallback: Path) -> Path:
        value = getattr(wd, name, None)
        return value if isinstance(value, Path) else fallback

    yield SimpleNamespace(
        repo=repo,
        orch=orch,
        original=original,
        state=_p("STATE_JSON", orch / "state.json"),
        usage=_p("USAGE_JSON", orch / "usage.json"),
        journal=_p("JOURNAL", orch / "journal.ndjson"),
        halt=_p("HALT_FILE", orch / "HALT"),
        questions=_p("QUESTIONS", orch / "questions"),
    )

    assert not _leaks(), (
        f"a test left path globals resolving outside the temp tree: {_leaks()}"
    )


# --- the subprocess recorder ------------------------------------------------------------


class _Result:
    def __init__(self, argv, returncode=0, stdout="", stderr=""):
        self.args = argv
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _matches(argv, tokens) -> bool:
    if isinstance(argv, str):
        return all(token in argv for token in tokens)
    parts = [str(a) for a in argv]
    return all(token in parts for token in tokens)


class _Runs:
    """Stand-in for subprocess.run: records every call, spawns nothing, ever."""

    def __init__(self):
        self.calls: list[SimpleNamespace] = []
        self._plans: list[tuple[tuple, dict]] = []

    def when(self, *tokens, returncode=0, stdout="", raises=None):
        self._plans.append((tokens, {"returncode": returncode, "stdout": stdout, "raises": raises}))
        return self

    def __call__(self, argv, **kwargs):
        self.calls.append(SimpleNamespace(argv=argv, kwargs=kwargs))
        for tokens, plan in self._plans:
            if _matches(argv, tokens):
                if plan["raises"] is not None:
                    raise plan["raises"]
                return _Result(argv, plan["returncode"], plan["stdout"])
        return _Result(argv, 0, "")

    # --- inspection ---
    @property
    def argvs(self) -> list:
        return [c.argv for c in self.calls]

    def of(self, *tokens) -> list[SimpleNamespace]:
        return [c for c in self.calls if _matches(c.argv, tokens)]

    def one(self, *tokens) -> SimpleNamespace:
        found = self.of(*tokens)
        assert len(found) == 1, f"expected exactly one {tokens} call, got {self.argvs}"
        return found[0]


@pytest.fixture(autouse=True)
def runs(monkeypatch) -> _Runs:
    """No test may spawn a process. Both subjects reach every external tool via subprocess."""
    recorder = _Runs()
    monkeypatch.setattr(subprocess, "run", recorder)

    def deny(*args, **kwargs):
        raise AssertionError(f"test tried to spawn a process: {args!r}")

    for name in ("Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, deny)
    return recorder


# --- the clock --------------------------------------------------------------------------


class _Stop(BaseException):
    """Breaks `main()`'s infinite loop. A BaseException so its `except Exception` misses it."""


class _Clock:
    def __init__(self, start=100_000.0):
        self.now = start
        self.step = 0.0
        self.stop_after = 1
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def sleep(self, seconds) -> None:
        self.sleeps.append(seconds)
        self.now += self.step if self.step else float(seconds)
        if len(self.sleeps) >= self.stop_after:
            raise _Stop()


@pytest.fixture(autouse=True)
def clock(wd, monkeypatch) -> _Clock:
    """No test may really sleep, and `main()`'s monotonic clock is fully controlled."""
    fake = _Clock()
    monkeypatch.setattr(wd, "time", fake)
    return fake


# --- on-disk helpers --------------------------------------------------------------------


def _write_state(box, mapping: dict) -> Path:
    box.state.parent.mkdir(parents=True, exist_ok=True)
    box.state.write_text(json.dumps(mapping), encoding="utf-8")
    return box.state


def _write_usage(box, mapping: dict) -> Path:
    box.usage.parent.mkdir(parents=True, exist_ok=True)
    box.usage.write_text(json.dumps(mapping), encoding="utf-8")
    return box.usage


def _events(box) -> list[dict]:
    if not box.journal.exists():
        return []
    return [
        json.loads(line)
        for line in box.journal.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _event_names(box) -> list[str]:
    return [e["event"] for e in _events(box)]


def _detail(box, event: str) -> str:
    for e in _events(box):
        if e["event"] == event:
            return e["detail"]
    raise AssertionError(f"no {event!r} journal record in {_event_names(box)}")


ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


# ===================================================================================
# module surface
# ===================================================================================


def test_the_three_supervision_constants(wd):
    assert wd.POLL_INTERVAL == 30
    assert wd.STALL_WINDOW_MIN == 20
    assert wd.MAX_STALL_RESTARTS == 3


def test_the_stall_window_is_forty_poll_intervals(wd):
    """The docstring's claim, pinned: 20 min of silence is many whole poll cycles."""
    assert wd.STALL_WINDOW_MIN * 60 == wd.POLL_INTERVAL * 40


def test_tmux_session_and_window_names(wd):
    """The reference hardcodes both; plan §5 derives them from the project name."""
    if _is_maestro(wd):
        assert isinstance(wd.TMUX_SESSION, str) and wd.TMUX_SESSION
        assert isinstance(wd.TMUX_WINDOW, str) and wd.TMUX_WINDOW
    else:
        assert wd.TMUX_SESSION == "agents"
        assert wd.TMUX_WINDOW == "orchestrator"


def test_every_path_global_started_out_under_repo(wd, box):
    """Every path global is *spelled* relative to `REPO`, which is what makes the
    reflection-based rebase total.

    Lexical containment, not `resolve()`d containment, and the difference is not
    cosmetic: the reference's `VENV_PYTHON` is `REPO/.venv/bin/python3`, and in a
    venv that last component is a symlink to the system interpreter — so its
    realpath (`/usr/bin/python3*`) sits outside `REPO` entirely. `box` resolves
    before deciding, so that one global lands under `_outside/` instead of under
    the mirrored tree; either way it is inside `tmp_path`, which
    `test_the_sandbox_redirected_every_path_global` and `box`'s own exit assertion
    prove. Pinning the resolved form here would pin the machine's venv layout, not
    the watchdog.
    """
    root = box.original["REPO"]
    outside = sorted(name for name, value in box.original.items() if not _under(value, root))
    assert outside == []


def test_the_sandbox_redirected_every_path_global(wd, box, tmp_path):
    assert set(box.original) == set(_path_globals(wd))
    for name, value in _path_globals(wd).items():
        assert _under(value, tmp_path), f"{name} still points at {value}"


# ===================================================================================
# now_iso / now_epoch
# ===================================================================================


def test_now_iso_is_second_resolution_zulu(wd):
    assert ISO_Z.match(wd.now_iso())


def test_now_iso_is_utc(wd):
    stamp = wd.now_iso()
    parsed = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert abs((parsed - datetime.now(tz=timezone.utc)).total_seconds()) < 30


def test_now_iso_drops_sub_second_precision(wd):
    assert "." not in wd.now_iso()


def test_now_epoch_is_a_float_close_to_now(wd):
    value = wd.now_epoch()
    assert isinstance(value, float)
    assert abs(value - datetime.now(tz=timezone.utc).timestamp()) < 30


def test_now_epoch_is_not_the_fake_monotonic_clock(wd, clock):
    """`now_epoch` reads the wall clock via datetime, not the module's `time`."""
    assert wd.now_epoch() != clock.now


# ===================================================================================
# read_json
# ===================================================================================


def test_read_json_missing_file_is_empty_dict(wd, box):
    assert wd.read_json(box.state) == {}


def test_read_json_corrupt_file_is_empty_dict(wd, box):
    box.state.parent.mkdir(parents=True, exist_ok=True)
    box.state.write_text("{not json", encoding="utf-8")
    assert wd.read_json(box.state) == {}


def test_read_json_directory_is_empty_dict(wd, box):
    """FOUND_BUGS: missing, corrupt and "it's a directory" are indistinguishable."""
    target = box.orch / "adir"
    target.mkdir(parents=True)
    assert wd.read_json(target) == {}


def test_read_json_returns_the_document(wd, box):
    _write_state(box, {"in_flight": [{"window": "t1"}], "halted": False})
    assert wd.read_json(box.state)["in_flight"] == [{"window": "t1"}]


def test_read_json_returns_non_dict_json_unchanged(wd, box):
    """FOUND_BUGS: annotated `-> dict`, but a JSON list comes back as a list."""
    _write_state(box, ["a", "b"])
    assert wd.read_json(box.state) == ["a", "b"]


# ===================================================================================
# append_journal
# ===================================================================================


def test_append_journal_writes_one_ndjson_record(wd, box):
    wd.append_journal("watchdog_start", "poll=30s")
    lines = box.journal.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert set(record) == {"ts", "event", "agent", "detail"}
    assert record["event"] == "watchdog_start"
    assert record["detail"] == "poll=30s"
    assert ISO_Z.match(record["ts"])


def test_append_journal_agent_field_is_the_watchdog_tag(wd, box):
    wd.append_journal("e", "d")
    assert _events(box)[0]["agent"] == "B3-watchdog"


def test_append_journal_appends_and_keeps_earlier_lines(wd, box):
    wd.append_journal("first", "1")
    wd.append_journal("second", "2")
    assert _event_names(box) == ["first", "second"]


def test_append_journal_missing_parent_directory_is_a_silent_noop(wd, box, monkeypatch):
    """FOUND_BUGS: every supervision event is lost with no error and no warning."""
    target = box.repo / "gone" / "journal.ndjson"
    monkeypatch.setattr(wd, "JOURNAL", target)
    wd.append_journal("watchdog_start", "poll=30s")
    assert not target.exists()


def test_append_journal_keeps_non_ascii_unescaped(wd, box):
    wd.append_journal("e", "café")
    assert "café" in box.journal.read_text(encoding="utf-8")


def test_append_journal_writes_a_non_string_detail_verbatim(wd, box):
    """`detail` is annotated `str`; a dict is serialised straight into the record."""
    wd.append_journal("e", {"a": 1})
    assert _events(box)[0]["detail"] == {"a": 1}


# ===================================================================================
# notify
# ===================================================================================


def test_notify_always_prints_the_alert_line(wd, capsys):
    wd.notify("orchestrator stalled")
    assert "[watchdog] ALERT: orchestrator stalled" in capsys.readouterr().out


def test_notify_without_a_notifier_spawns_nothing(wd, runs, capsys):
    if _notifies_via_script(wd):
        assert not wd.NOTIFY_SCRIPT.exists()
    wd.notify("hello")
    capsys.readouterr()
    if _notifies_via_script(wd):
        assert runs.calls == []


def test_notify_shells_out_to_the_notifier_with_the_message(wd, box, runs, capsys):
    if not _notifies_via_script(wd):
        wd.notify("hello")
        capsys.readouterr()
        return
    wd.NOTIFY_SCRIPT.parent.mkdir(parents=True, exist_ok=True)
    wd.NOTIFY_SCRIPT.write_text("#!/bin/bash\n", encoding="utf-8")
    wd.notify("hello")
    capsys.readouterr()
    call = runs.one("bash")
    assert call.argv == ["bash", str(wd.NOTIFY_SCRIPT), "hello"]
    assert call.kwargs == {"capture_output": True, "timeout": 15}


def test_notify_ignores_a_failing_notifier(wd, runs, capsys):
    if not _notifies_via_script(wd):
        return
    wd.NOTIFY_SCRIPT.parent.mkdir(parents=True, exist_ok=True)
    wd.NOTIFY_SCRIPT.write_text("#!/bin/bash\n", encoding="utf-8")
    runs.when("bash", returncode=1)
    assert wd.notify("hello") is None
    capsys.readouterr()


def test_notify_lets_a_notifier_timeout_escape(wd, runs, capsys):
    """FOUND_BUGS: `notify` is the last act of the HALT path and is not exception-safe.

    `append_journal` wraps its I/O in try/except; `notify` does not. A notifier that hangs
    for 15s raises `TimeoutExpired` straight out of `notify`.
    """
    if not _notifies_via_script(wd):
        return
    wd.NOTIFY_SCRIPT.parent.mkdir(parents=True, exist_ok=True)
    wd.NOTIFY_SCRIPT.write_text("#!/bin/bash\n", encoding="utf-8")
    runs.when("bash", raises=subprocess.TimeoutExpired(cmd="bash", timeout=15))
    with pytest.raises(subprocess.TimeoutExpired):
        wd.notify("hello")
    capsys.readouterr()


def test_notify_does_not_journal(wd, box, capsys):
    """An alert leaves no trace in the journal — only on stdout and in Telegram."""
    wd.notify("hello")
    capsys.readouterr()
    assert _events(box) == []


# ===================================================================================
# orchestrator_alive
# ===================================================================================


def test_orchestrator_alive_greps_the_process_table(wd, runs):
    wd.orchestrator_alive()
    call = runs.one("pgrep")
    assert call.argv[:2] == ["pgrep", "-f"]
    assert call.kwargs == {"capture_output": True}
    assert call.argv[2]


def test_orchestrator_alive_pattern_matches_the_loop_entrypoints(wd, runs):
    wd.orchestrator_alive()
    pattern = runs.one("pgrep").argv[2]
    if _is_maestro(wd):
        assert isinstance(pattern, str) and pattern
    else:
        assert pattern == r"launch_orchestrator\.py|orchestrator_run\.py"


def test_orchestrator_alive_true_on_a_match(wd, runs):
    runs.when("pgrep", returncode=0, stdout="1234\n")
    assert wd.orchestrator_alive() is True


def test_orchestrator_alive_false_on_no_match(wd, runs):
    runs.when("pgrep", returncode=1)
    assert wd.orchestrator_alive() is False


def test_orchestrator_alive_false_when_pgrep_itself_errors(wd, runs):
    """FOUND_BUGS: a usage error from pgrep (rc 2) reads as "the loop is dead" and
    triggers a relaunch of a loop that may well be running."""
    runs.when("pgrep", returncode=2)
    assert wd.orchestrator_alive() is False


# ===================================================================================
# tmux_window_exists
# ===================================================================================


def test_tmux_window_exists_lists_the_session_windows(wd, runs):
    wd.tmux_window_exists("t1")
    call = runs.one("tmux")
    assert call.argv == [
        "tmux", "list-windows", "-t", wd.TMUX_SESSION, "-F", "#{window_name}",
    ]
    assert call.kwargs == {"capture_output": True, "text": True}


def test_tmux_window_exists_true_on_an_exact_line(wd, runs):
    runs.when("list-windows", stdout="orchestrator\nT1\n")
    assert wd.tmux_window_exists("T1") is True


def test_tmux_window_exists_is_exact_not_prefix(wd, runs):
    runs.when("list-windows", stdout="T10\n")
    assert wd.tmux_window_exists("T1") is False


def test_tmux_window_exists_false_on_empty_output(wd, runs):
    runs.when("list-windows", stdout="")
    assert wd.tmux_window_exists("T1") is False


def test_tmux_window_exists_ignores_the_return_code(wd, runs):
    """FOUND_BUGS: the return code is dropped, so "no tmux server" and "no such window"
    are the same answer — and a nonzero exit with matching stdout still reads True."""
    runs.when("list-windows", returncode=1, stdout="T1\n")
    assert wd.tmux_window_exists("T1") is True


# ===================================================================================
# launch_orchestrator
# ===================================================================================


def test_launch_orchestrator_spawns_one_shell_command(wd, runs, capsys):
    wd.launch_orchestrator()
    capsys.readouterr()
    assert len(runs.calls) == 1
    call = runs.calls[0]
    assert isinstance(call.argv, str)
    assert call.kwargs == {"shell": True}


def test_launch_orchestrator_targets_the_session_and_window(wd, runs, capsys):
    wd.launch_orchestrator()
    capsys.readouterr()
    command = runs.calls[0].argv
    assert f"tmux new-window -t {wd.TMUX_SESSION} -n {wd.TMUX_WINDOW}" in command


def test_launch_orchestrator_runs_the_launcher_from_the_repo(wd, box, runs, capsys):
    wd.launch_orchestrator()
    capsys.readouterr()
    command = runs.calls[0].argv
    assert f"cd {wd.REPO} &&" in command
    if not _is_maestro(wd):
        assert str(wd.VENV_PYTHON) in command
        assert str(wd.LAUNCHER) in command


def test_launch_orchestrator_journals_and_prints(wd, box, runs, capsys):
    wd.launch_orchestrator()
    out = capsys.readouterr().out
    assert _event_names(box) == ["orchestrator_relaunched"]
    assert _detail(box, "orchestrator_relaunched") == f"window={wd.TMUX_WINDOW}"
    assert f"[watchdog] Launched orchestrator in {wd.TMUX_SESSION}:{wd.TMUX_WINDOW}" in out


def test_launch_orchestrator_claims_success_even_when_tmux_fails(wd, box, runs, capsys):
    """FOUND_BUGS: the return code is never checked, so a failed launch is journalled as
    `orchestrator_relaunched` and printed as a success."""
    runs.when("tmux", returncode=1)
    wd.launch_orchestrator()
    assert "Launched orchestrator" in capsys.readouterr().out
    assert _event_names(box) == ["orchestrator_relaunched"]


def test_launch_orchestrator_interpolates_paths_into_a_shell_string_unquoted(
    wd, box, runs, monkeypatch, capsys
):
    """FOUND_BUGS: `shell=True` with unquoted interpolation. A repo path containing a
    space produces a broken command line rather than an error."""
    spaced = box.repo.parent / "two words"
    spaced.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(wd, "REPO", spaced)
    wd.launch_orchestrator()
    capsys.readouterr()
    assert f"cd {spaced} &&" in runs.calls[0].argv
    assert "'two words'" not in runs.calls[0].argv


# ===================================================================================
# ensure_resume_job
# ===================================================================================

RESETS_AT = 1786256976.0


def test_ensure_resume_job_reads_then_rewrites_the_crontab(wd, runs, capsys):
    wd.ensure_resume_job(RESETS_AT)
    capsys.readouterr()
    assert [c.argv for c in runs.calls] == [["crontab", "-l"], ["crontab", "-"]]
    write = runs.one("crontab", "-")
    assert write.kwargs["text"] is True
    assert write.kwargs["capture_output"] is True


def test_ensure_resume_job_never_uses_sudo_or_at(wd, runs, capsys):
    """Plan §5's gate: the scheduling mechanism must be extractable without privilege."""
    wd.ensure_resume_job(RESETS_AT)
    capsys.readouterr()
    flat = " ".join(str(a) for c in runs.calls for a in (c.argv if isinstance(c.argv, list) else [c.argv]))
    assert "sudo" not in flat
    assert "systemd-run" not in flat


def test_ensure_resume_job_cron_line_is_local_time_fields(wd, runs, capsys):
    wd.ensure_resume_job(RESETS_AT)
    capsys.readouterr()
    written = runs.one("crontab", "-").kwargs["input"]
    line = written.strip().splitlines()[-1]
    local = datetime.fromtimestamp(RESETS_AT)
    assert line.split()[:5] == [
        str(local.minute), str(local.hour), str(local.day), str(local.month), "*",
    ]


def test_ensure_resume_job_uses_local_time_not_utc(wd, runs, capsys):
    """FOUND_BUGS-adjacent: `datetime.fromtimestamp` is deliberate (cron is local), but it
    is the only place in the module that leaves UTC — worth pinning explicitly."""
    wd.ensure_resume_job(RESETS_AT)
    capsys.readouterr()
    written = runs.one("crontab", "-").kwargs["input"]
    local = datetime.fromtimestamp(RESETS_AT)
    utc = datetime.fromtimestamp(RESETS_AT, tz=timezone.utc)
    if local.hour != utc.hour:
        assert written.strip().splitlines()[-1].split()[1] == str(local.hour)
        assert written.strip().splitlines()[-1].split()[1] != str(utc.hour)


def test_ensure_resume_job_command_relaunches_the_loop_in_tmux(wd, runs, capsys):
    wd.ensure_resume_job(RESETS_AT)
    capsys.readouterr()
    written = runs.one("crontab", "-").kwargs["input"]
    assert "cron-orch-resume" in written
    assert f"-t {wd.TMUX_SESSION}" in written
    assert f"cd {wd.REPO} &&" in written


def test_ensure_resume_job_keeps_existing_crontab_entries(wd, runs, capsys):
    runs.when("crontab", "-l", stdout="0 5 * * * /usr/bin/backup\n")
    wd.ensure_resume_job(RESETS_AT)
    capsys.readouterr()
    written = runs.one("crontab", "-").kwargs["input"]
    assert written.startswith("0 5 * * * /usr/bin/backup\n")
    assert written.endswith("\n")


def test_ensure_resume_job_is_a_noop_once_any_resume_entry_exists(wd, box, runs, capsys):
    """FOUND_BUGS: the guard is a substring test on the *tag*, not on the schedule, and
    nothing ever removes the entry — so after the first quota pause the watchdog never
    schedules another resume, at any later reset time, for the life of that crontab."""
    runs.when("crontab", "-l", stdout="9 9 9 9 * tmux ... 'cron-orch-resume' ...\n")
    wd.ensure_resume_job(RESETS_AT)
    assert runs.of("crontab", "-") == []
    assert _events(box) == []
    assert capsys.readouterr().out == ""


def test_ensure_resume_job_discards_the_crontab_when_reading_it_fails(wd, runs, capsys):
    """FOUND_BUGS: a nonzero `crontab -l` (transient failure, not just "no crontab") makes
    `existing` empty, and the very next call *overwrites* the user's crontab with only the
    resume line — every other entry is destroyed."""
    runs.when("crontab", "-l", returncode=1, stdout="0 5 * * * /usr/bin/backup\n")
    wd.ensure_resume_job(RESETS_AT)
    capsys.readouterr()
    written = runs.one("crontab", "-").kwargs["input"]
    assert "backup" not in written
    assert written.startswith("\n")


def test_ensure_resume_job_journals_and_prints(wd, box, runs, capsys):
    wd.ensure_resume_job(RESETS_AT)
    out = capsys.readouterr().out
    local = datetime.fromtimestamp(RESETS_AT)
    assert _event_names(box) == ["resume_scheduled_watchdog"]
    assert _detail(box, "resume_scheduled_watchdog") == f"cron {local.isoformat()}"
    assert f"[watchdog] Resume job scheduled: {local.strftime('%H:%M %d/%m')} local" in out


def test_ensure_resume_job_cron_entry_recurs_annually(wd, runs, capsys):
    """FOUND_BUGS: `M H D Mo *` is not a one-shot — the resume fires again on the same
    day next year (and nothing ever deletes it)."""
    wd.ensure_resume_job(RESETS_AT)
    capsys.readouterr()
    line = runs.one("crontab", "-").kwargs["input"].strip().splitlines()[-1]
    assert line.split()[4] == "*"


def test_ensure_resume_job_rejects_a_non_numeric_reset(wd, runs, capsys):
    with pytest.raises(TypeError):
        wd.ensure_resume_job(None)
    capsys.readouterr()


# ===================================================================================
# _parse_epoch
# ===================================================================================


def test_parse_epoch_none_is_none(wd):
    assert wd._parse_epoch(None) is None


def test_parse_epoch_passes_a_float_through(wd):
    assert wd._parse_epoch(RESETS_AT) == RESETS_AT


def test_parse_epoch_parses_a_numeric_string(wd):
    assert wd._parse_epoch("1786256976") == 1786256976.0


def test_parse_epoch_parses_a_zulu_timestamp_as_utc(wd):
    assert wd._parse_epoch("2026-08-17T00:00:00Z") == datetime(
        2026, 8, 17, tzinfo=timezone.utc
    ).timestamp()


def test_parse_epoch_reads_a_naive_timestamp_as_local_time(wd):
    """FOUND_BUGS: `main` compares the result against `now_epoch()`, which is UTC. A
    naive `paused_until` is therefore off by the machine's UTC offset."""
    assert wd._parse_epoch("2026-08-17T00:00:00") == datetime(2026, 8, 17).timestamp()


def test_parse_epoch_reads_an_eight_digit_number_as_a_calendar_date(wd):
    """FOUND_BUGS: ISO parsing is tried first, and `fromisoformat` accepts the compact
    `YYYYMMDD` form — so the string "20260817" becomes a 2026 date, not an epoch."""
    assert wd._parse_epoch("20260817") == datetime(2026, 8, 17).timestamp()


def test_parse_epoch_coerces_a_bool_to_one_second_past_the_epoch(wd):
    """FOUND_BUGS: `float(True)` is 1.0, so `paused_until: true` parses as 1970-01-01."""
    assert wd._parse_epoch(True) == 1.0


def test_parse_epoch_zero_is_returned_as_zero(wd):
    """And `main` treats 0.0 as falsy, i.e. as "not paused" — see the pause tests."""
    assert wd._parse_epoch(0) == 0.0


def test_parse_epoch_garbage_is_none(wd):
    assert wd._parse_epoch("later today") is None
    assert wd._parse_epoch("2026-08") is None
    assert wd._parse_epoch([]) is None
    assert wd._parse_epoch({}) is None


# ===================================================================================
# journal_last_line
# ===================================================================================


def test_journal_last_line_missing_file_is_none(wd, box):
    assert wd.journal_last_line() is None


def test_journal_last_line_empty_file_is_none(wd, box):
    box.journal.write_text("", encoding="utf-8")
    assert wd.journal_last_line() is None


def test_journal_last_line_whitespace_only_is_none(wd, box):
    box.journal.write_text("\n \n\n", encoding="utf-8")
    assert wd.journal_last_line() is None


def test_journal_last_line_returns_the_raw_last_line(wd, box):
    box.journal.write_text('{"a": 1}\n{"b": 2}\n', encoding="utf-8")
    assert wd.journal_last_line() == '{"b": 2}'


def test_journal_last_line_ignores_trailing_blank_lines(wd, box):
    box.journal.write_text('{"b": 2}\n\n\n', encoding="utf-8")
    assert wd.journal_last_line() == '{"b": 2}'


def test_journal_last_line_is_not_parsed(wd, box):
    """A string, not a record: stall detection compares raw text."""
    box.journal.write_text("not json at all\n", encoding="utf-8")
    assert wd.journal_last_line() == "not json at all"


def test_journal_last_line_cannot_see_a_repeated_identical_entry(wd, box):
    """FOUND_BUGS: progress is measured by the last line *changing*. A loop that writes
    the same line twice (same second, same event, same detail) looks frozen."""
    line = '{"ts": "2026-08-17T00:00:00Z", "event": "poll", "detail": "x"}'
    box.journal.write_text(line + "\n", encoding="utf-8")
    first = wd.journal_last_line()
    with box.journal.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    assert wd.journal_last_line() == first


def test_journal_last_line_directory_is_none(wd, box, monkeypatch):
    target = box.orch / "journal_dir"
    target.mkdir(parents=True)
    monkeypatch.setattr(wd, "JOURNAL", target)
    assert wd.journal_last_line() is None


# ===================================================================================
# reap_dead_implementers
# ===================================================================================


def test_reap_no_state_file_does_nothing(wd, runs, capsys):
    wd.reap_dead_implementers()
    assert runs.calls == []
    assert capsys.readouterr().out == ""


def test_reap_empty_in_flight_does_nothing(wd, box, runs):
    _write_state(box, {"in_flight": []})
    wd.reap_dead_implementers()
    assert runs.calls == []


def test_reap_skips_an_entry_without_a_window(wd, box, runs):
    _write_state(box, {"in_flight": [{"task": "T1"}]})
    wd.reap_dead_implementers()
    assert runs.calls == []


def test_reap_skips_a_window_that_no_longer_exists(wd, box, runs, capsys):
    """FOUND_BUGS: `continue` on a missing window means an in-flight task whose whole
    tmux window is already gone is never reported and never reaped."""
    _write_state(box, {"in_flight": [{"window": "T1"}]})
    runs.when("list-windows", stdout="orchestrator\n")
    wd.reap_dead_implementers()
    assert runs.of("list-panes") == []
    assert capsys.readouterr().out == ""


def test_reap_leaves_a_live_pane_alone(wd, box, runs, capsys):
    _write_state(box, {"in_flight": [{"window": "T1"}]})
    runs.when("list-windows", stdout="T1\n").when("list-panes", stdout="0\n")
    wd.reap_dead_implementers()
    assert runs.of("kill-window") == []
    assert capsys.readouterr().out == ""


def test_reap_kills_journals_and_prints_for_a_dead_pane(wd, box, runs, capsys):
    _write_state(box, {"in_flight": [{"window": "T1"}]})
    runs.when("list-windows", stdout="T1\n").when("list-panes", stdout="1\n")
    wd.reap_dead_implementers()
    out = capsys.readouterr().out
    panes = runs.one("list-panes")
    assert panes.argv == [
        "tmux", "list-panes", "-t", f"{wd.TMUX_SESSION}:T1", "-F", "#{pane_dead}",
    ]
    kill = runs.one("kill-window")
    assert kill.argv == ["tmux", "kill-window", "-t", f"{wd.TMUX_SESSION}:T1"]
    assert kill.kwargs == {"capture_output": True}
    assert _detail(box, "implementer_reaped") == "dead window T1"
    assert "[watchdog] Reaped dead implementer window: T1" in out


def test_reap_kills_the_window_when_listing_panes_fails(wd, box, runs, capsys):
    """FOUND_BUGS: a nonzero `list-panes` is treated as "dead" even though the window was
    just confirmed to exist — a tmux hiccup kills a running implementer."""
    _write_state(box, {"in_flight": [{"window": "T1"}]})
    runs.when("list-windows", stdout="T1\n").when("list-panes", returncode=1, stdout="")
    wd.reap_dead_implementers()
    capsys.readouterr()
    assert len(runs.of("kill-window")) == 1


def test_reap_kills_a_window_with_one_dead_pane_among_live_ones(wd, box, runs, capsys):
    """FOUND_BUGS: `"1" in lines` is per-window, so a single dead pane kills the whole
    window and every live pane in it."""
    _write_state(box, {"in_flight": [{"window": "T1"}]})
    runs.when("list-windows", stdout="T1\n").when("list-panes", stdout="0\n1\n0\n")
    wd.reap_dead_implementers()
    capsys.readouterr()
    assert len(runs.of("kill-window")) == 1


def test_reap_never_touches_state_json(wd, box, runs, capsys):
    """FOUND_BUGS: reaping kills the tmux window but leaves the `in_flight` entry in
    place, so the concurrency cap still counts it. Only the orchestrator's own
    reconcile step can clear it — the watchdog cannot.
    """
    before = _write_state(box, {"in_flight": [{"window": "T1"}]}).read_bytes()
    runs.when("list-windows", stdout="T1\n").when("list-panes", stdout="1\n")
    wd.reap_dead_implementers()
    capsys.readouterr()
    assert box.state.read_bytes() == before


def test_reap_handles_several_entries_independently(wd, box, runs, capsys):
    _write_state(box, {"in_flight": [{"window": "T1"}, {"window": "T2"}]})
    runs.when("list-windows", stdout="T1\nT2\n")
    runs.when("list-panes", "agents:T1" if not _is_maestro(wd) else f"{wd.TMUX_SESSION}:T1", stdout="1\n")
    runs.when("list-panes", stdout="0\n")
    wd.reap_dead_implementers()
    capsys.readouterr()
    killed = [c.argv[-1] for c in runs.of("kill-window")]
    assert killed == [f"{wd.TMUX_SESSION}:T1"]


def test_reap_raises_when_in_flight_holds_bare_strings(wd, box, runs):
    """FOUND_BUGS: no shape check. A list of window names (rather than dicts) raises
    AttributeError; inside `main` that becomes a journalled `watchdog_error` every poll."""
    _write_state(box, {"in_flight": ["T1"]})
    with pytest.raises(AttributeError):
        wd.reap_dead_implementers()


# ===================================================================================
# main() — the decision logic
# ===================================================================================


@pytest.fixture
def env(wd, box, runs, clock, monkeypatch):
    """Stub every process-touching helper so `main()`'s decisions are observable.

    `append_journal`, `read_json`, `_parse_epoch`, `now_epoch` and the state writes stay
    real — they run against the sandbox — so the journal is the same evidence an operator
    would read. `journal_last_line` is stubbed by default so journal advancement is an
    explicit test input rather than a side effect of the watchdog's own writes; the one
    test that cares about that coupling sets `use_real_journal`.
    """
    state = SimpleNamespace(
        alive=True,
        launches=0,
        reaps=0,
        resumes=[],
        notices=[],
        journal_line="seed",
        use_real_journal=False,
        reap_error=None,
        notify_error=None,
        clock=clock,
    )
    real_journal_last_line = wd.journal_last_line

    def _alive():
        return state.alive

    def _launch():
        state.launches += 1

    def _reap():
        state.reaps += 1
        if state.reap_error is not None:
            raise state.reap_error(f"reap boom {state.reaps}")

    def _resume(resets_at):
        state.resumes.append(resets_at)

    def _notify(msg):
        state.notices.append(msg)
        if state.notify_error is not None:
            raise state.notify_error("notify boom")

    def _journal_line():
        if state.use_real_journal:
            return real_journal_last_line()
        value = state.journal_line
        return value() if callable(value) else value

    monkeypatch.setattr(wd, "orchestrator_alive", _alive)
    monkeypatch.setattr(wd, "launch_orchestrator", _launch)
    monkeypatch.setattr(wd, "reap_dead_implementers", _reap)
    monkeypatch.setattr(wd, "ensure_resume_job", _resume)
    monkeypatch.setattr(wd, "notify", _notify)
    monkeypatch.setattr(wd, "journal_last_line", _journal_line)
    return state


#: `main()` was cut off mid-loop rather than returning.
RUNNING = "still-looping"


def _run(wd, env, ticks=1, step=0.0):
    env.clock.stop_after = ticks
    env.clock.step = step
    try:
        return wd.main()
    except _Stop:
        return RUNNING


STALL = 21 * 60  # one poll step that clears STALL_WINDOW_MIN


def test_main_announces_itself_before_anything_else(wd, box, env, capsys):
    box.halt.touch()
    assert _run(wd, env) == 0
    out = capsys.readouterr().out
    assert "[watchdog] Starting — poll=30s stall=20min max_restarts=3" in out
    assert _event_names(box)[0] == "watchdog_start"
    assert _detail(box, "watchdog_start") == "poll=30s stall=20min"


def test_main_halt_sentinel_returns_zero_and_journals(wd, box, env, capsys):
    box.halt.touch()
    assert _run(wd, env) == 0
    assert "[watchdog] HALT sentinel present — stopping." in capsys.readouterr().out
    assert _event_names(box) == ["watchdog_start", "watchdog_halt_respected"]
    assert env.launches == 0
    assert env.reaps == 0


def test_main_halt_wins_over_everything_else(wd, box, env, capsys):
    box.halt.touch()
    _write_state(box, {"halted": True, "paused_until": "2099-01-01T00:00:00Z"})
    env.alive = False
    assert _run(wd, env) == 0
    capsys.readouterr()
    assert env.launches == 0


def test_main_soft_halt_idles_without_relaunching(wd, box, env, capsys):
    _write_state(box, {"halted": True})
    env.alive = False
    assert _run(wd, env, ticks=3) == RUNNING
    assert "[watchdog] state.halted — idling (no relaunch)." in capsys.readouterr().out
    assert env.launches == 0
    assert env.reaps == 0
    assert env.clock.sleeps == [wd.POLL_INTERVAL] * 3


def test_main_soft_halt_journals_once_across_many_polls(wd, box, env, capsys):
    _write_state(box, {"halted": True})
    assert _run(wd, env, ticks=4) == RUNNING
    capsys.readouterr()
    assert _event_names(box).count("watchdog_idle_halted") == 1
    assert _detail(box, "watchdog_idle_halted") == (
        "state.halted=True — suppressing relaunch until resume"
    )


def test_main_soft_halt_marker_rearms_after_a_resume(wd, box, env, capsys, monkeypatch):
    """The one-shot marker is reset on any non-halted poll, so a second halt logs again."""
    states = [{"halted": True}, {}, {"halted": True}]
    calls = {"n": 0}
    real_read_json = wd.read_json

    def _read_json(path):
        if path == wd.STATE_JSON:
            index = min(calls["n"], len(states) - 1)
            calls["n"] += 1
            return states[index]
        return real_read_json(path)

    monkeypatch.setattr(wd, "read_json", _read_json)
    assert _run(wd, env, ticks=3) == RUNNING
    capsys.readouterr()
    assert _event_names(box).count("watchdog_idle_halted") == 2


def test_main_pause_in_the_future_schedules_a_resume_and_idles(wd, box, env, capsys):
    _write_state(box, {"paused_until": "2099-01-01T00:00:00Z"})
    _write_usage(box, {"five_hour": {"resets_at": RESETS_AT}})
    env.alive = False
    assert _run(wd, env, ticks=2) == RUNNING
    capsys.readouterr()
    assert env.resumes == [RESETS_AT, RESETS_AT]
    assert env.launches == 0
    assert env.reaps == 0


def test_main_pause_without_a_usage_reset_just_idles(wd, box, env, capsys):
    _write_state(box, {"paused_until": "2099-01-01T00:00:00Z"})
    assert _run(wd, env) == RUNNING
    capsys.readouterr()
    assert env.resumes == []
    assert env.launches == 0


def test_main_pause_ignores_a_weekly_only_reset(wd, box, env, capsys):
    """FOUND_BUGS: only `five_hour.resets_at` is consulted. A pause caused by the weekly
    cap schedules no resume at all, and the loop stays down until a human notices."""
    _write_state(box, {"paused_until": "2099-01-01T00:00:00Z"})
    _write_usage(box, {"weekly": {"resets_at": RESETS_AT}})
    assert _run(wd, env) == RUNNING
    capsys.readouterr()
    assert env.resumes == []


def test_main_pause_with_a_non_numeric_reset_journals_an_error_and_keeps_polling(
    wd, box, env, capsys
):
    """FOUND_BUGS: `float(resets_at)` is unguarded; a bad usage.json turns every poll into
    a `watchdog_error` line and no resume is ever scheduled."""
    _write_state(box, {"paused_until": "2099-01-01T00:00:00Z"})
    _write_usage(box, {"five_hour": {"resets_at": "soon"}})
    assert _run(wd, env, ticks=2) == RUNNING
    assert "[watchdog] poll error:" in capsys.readouterr().out
    assert _event_names(box).count("watchdog_error") == 2
    assert env.resumes == []


def test_main_pause_of_zero_is_treated_as_no_pause(wd, box, env, capsys):
    """FOUND_BUGS: `if paused_epoch and ...` — a falsy 0.0 epoch skips both pause
    branches, so the stale `paused_until` is never cleared either."""
    _write_state(box, {"paused_until": 0})
    env.alive = False
    assert _run(wd, env) == RUNNING
    capsys.readouterr()
    assert env.launches == 1
    assert json.loads(box.state.read_text(encoding="utf-8"))["paused_until"] == 0


def test_main_unparseable_pause_is_treated_as_no_pause(wd, box, env, capsys):
    _write_state(box, {"paused_until": "when the quota comes back"})
    env.alive = False
    assert _run(wd, env) == RUNNING
    capsys.readouterr()
    assert env.launches == 1


def test_main_expired_pause_is_cleared_in_place(wd, box, env, capsys):
    _write_state(box, {"paused_until": "2020-01-01T00:00:00Z", "cursor": 3})
    env.alive = True
    assert _run(wd, env) == RUNNING
    assert "[watchdog] Pause expired — cleared." in capsys.readouterr().out
    written = json.loads(box.state.read_text(encoding="utf-8"))
    assert written["paused_until"] is None
    assert ISO_Z.match(written["updated_at"])
    assert written["cursor"] == 3
    assert not list(box.orch.glob("*.tmp"))


def test_main_expired_pause_write_is_indented_json_with_a_trailing_newline(
    wd, box, env, capsys
):
    _write_state(box, {"paused_until": "2020-01-01T00:00:00Z"})
    _run(wd, env)
    capsys.readouterr()
    text = box.state.read_text(encoding="utf-8")
    assert text.endswith("}\n")
    assert "\n  " in text


def test_main_expired_pause_is_not_journalled(wd, box, env, capsys):
    """FOUND_BUGS: clearing a pause is an operator-visible state change with no journal
    record — unlike every other decision `main` makes."""
    _write_state(box, {"paused_until": "2020-01-01T00:00:00Z"})
    _run(wd, env)
    capsys.readouterr()
    assert "resume" not in " ".join(_event_names(box))
    assert _event_names(box) == ["watchdog_start"]


def test_main_expired_pause_falls_through_to_the_liveness_relaunch(wd, box, env, capsys):
    _write_state(box, {"paused_until": "2020-01-01T00:00:00Z"})
    env.alive = False
    assert _run(wd, env) == RUNNING
    capsys.readouterr()
    assert env.launches == 1
    assert env.reaps == 1


def test_main_blocked_on_unanswered_idles(wd, box, env, capsys):
    _write_state(box, {"blocked_on": ["req-1", "req-2"]})
    (box.questions / "req-1.answer").write_text("yes\n", encoding="utf-8")
    env.alive = False
    assert _run(wd, env, ticks=2) == RUNNING
    capsys.readouterr()
    assert env.launches == 0
    assert env.reaps == 0
    assert _event_names(box) == ["watchdog_start"]


def test_main_blocked_on_all_answered_clears_and_relaunches(wd, box, env, capsys):
    _write_state(box, {"blocked_on": ["req-1"], "cursor": 7})
    (box.questions / "req-1.answer").write_text("yes\n", encoding="utf-8")
    env.alive = False
    assert _run(wd, env) == RUNNING
    out = capsys.readouterr().out
    assert "All blocked_on answers received" in out
    written = json.loads(box.state.read_text(encoding="utf-8"))
    assert written["blocked_on"] == []
    assert written["cursor"] == 7
    assert _detail(box, "blocked_on_cleared") == "answered=['req-1']"
    assert env.launches == 1


def test_main_in_flight_and_alive_reaps_and_skips_the_liveness_check(wd, box, env, capsys):
    _write_state(box, {"in_flight": [{"window": "T1"}]})
    env.alive = True
    assert _run(wd, env, ticks=2) == RUNNING
    capsys.readouterr()
    assert env.reaps == 2
    assert env.launches == 0


def test_main_in_flight_but_dead_falls_through_and_relaunches(wd, box, env, capsys):
    _write_state(box, {"in_flight": [{"window": "T1"}]})
    env.alive = False
    assert _run(wd, env) == RUNNING
    capsys.readouterr()
    assert env.launches == 1
    assert env.reaps == 1


def test_main_in_flight_and_alive_disables_stall_detection_entirely(wd, box, env, capsys):
    """FOUND_BUGS: the in-flight guard `continue`s before the stall check, so an
    orchestrator wedged while any implementer is in flight is never restarted — however
    long the journal stays silent."""
    _write_state(box, {"in_flight": [{"window": "T1"}]})
    env.alive = True
    assert _run(wd, env, ticks=5, step=STALL) == RUNNING
    capsys.readouterr()
    assert runs_free_of_stall(env)
    assert _event_names(box) == ["watchdog_start"]


def runs_free_of_stall(env) -> bool:
    return env.notices == [] and env.launches == 0


def test_main_waiting_on_dan_keeps_the_stall_timer_fresh(wd, box, env, capsys):
    _write_state(box, {"waiting_on_dan": {"7": {"task": "T1"}}})
    env.alive = True
    assert _run(wd, env, ticks=5, step=STALL) == RUNNING
    capsys.readouterr()
    assert env.notices == []
    assert "stall_restart" not in _event_names(box)


def test_main_relaunches_a_dead_orchestrator_every_poll(wd, box, env, capsys):
    env.alive = False
    assert _run(wd, env, ticks=3) == RUNNING
    capsys.readouterr()
    assert env.launches == 3
    assert env.reaps == 3


def test_main_alive_and_progressing_does_nothing_but_reap(wd, box, env, capsys):
    lines = iter(["a", "b", "c", "d", "e", "f"])
    env.journal_line = lambda: next(lines)
    assert _run(wd, env, ticks=3, step=STALL) == RUNNING
    capsys.readouterr()
    assert env.launches == 0
    assert env.reaps == 3
    assert _event_names(box) == ["watchdog_start"]


def test_main_does_not_rewrite_state_when_there_is_nothing_to_change(wd, box, env, capsys):
    before = _write_state(box, {"cursor": 1}).read_bytes()
    assert _run(wd, env, ticks=2) == RUNNING
    capsys.readouterr()
    assert box.state.read_bytes() == before


def test_main_first_stall_kills_and_relaunches(wd, box, env, capsys, runs):
    """The kill and the relaunch are one tick, not two: `main` `pkill`s, sleeps a
    hardcoded 2s, forces `alive = False` and falls straight into the liveness branch.

    Three ticks, therefore, not two — the sleep(2) counts as a tick of its own, so a
    two-tick run is cut off *between* the kill and the relaunch and would pin no
    relaunch at all. The sleep order is the poll interval first (the healthy tick 1),
    then the 2s pause inside tick 2.
    """
    env.alive = True
    assert _run(wd, env, ticks=3, step=STALL) == RUNNING
    out = capsys.readouterr().out
    assert "[watchdog] Stall #1 (21 min) — killing orchestrator." in out
    assert _detail(box, "stall_restart") == "restart=1 silence=21min"
    kill = runs.one("pkill")
    assert kill.argv == ["pkill", "-f", r"launch_orchestrator\.py|orchestrator_run\.py"] or _is_maestro(wd)
    assert kill.kwargs == {"capture_output": True}
    assert env.clock.sleeps[:2] == [wd.POLL_INTERVAL, 2]
    assert env.launches == 1
    assert env.notices == []


def test_main_no_stall_before_the_window_elapses(wd, box, env, capsys, runs):
    """19 minutes of silence is under the 20-minute window, so nothing is killed.

    Two ticks, not three: silence is measured from the last journal *change*, not
    from the last poll, so it accumulates across ticks — a third 19-minute tick would
    put the total at 38 minutes and fire the stall this test exists to rule out.
    """
    env.alive = True
    assert _run(wd, env, ticks=2, step=19 * 60) == RUNNING
    capsys.readouterr()
    assert runs.of("pkill") == []
    assert "stall_restart" not in _event_names(box)


def test_main_a_freshly_launched_loop_is_never_stall_killed(
    wd, box, env, capsys, runs, monkeypatch
):
    """`time_since_launch` guards the relaunched loop: the journal has been silent for
    well over the window, but the orchestrator was started 30s ago.

    Both clocks have to be driven independently, so the tick sizes are per-sleep rather
    than the fixture's single `step`:

    * tick 1 — dead, so relaunch; `last_launch_mono` = T0. Sleep advances 21 min.
    * tick 2 — dead again, so relaunch; `last_launch_mono` moves to T0+21min while
      `last_journal_change` stays at T0. Sleep advances only the poll interval.
    * tick 3 — alive, silence is 21 min + 30 s (over the window), but only 30 s have
      passed since the launch, so the `time_since_launch` conjunct is False and no kill
      happens.

    A second 21-minute sleep here would make tick 3 land 21 minutes after the relaunch
    and the guard would (correctly, per the reference) let the kill through.
    """

    def _alive_seq(seq=iter([False, False, True, True])):
        return next(seq)

    # replaces the always-`env.alive` stub the `env` fixture installed
    monkeypatch.setattr(wd, "orchestrator_alive", _alive_seq)

    clock = env.clock
    clock.stop_after = 3
    clock.step = 0.0
    clock.sleeps.clear()
    step_seq = iter([STALL, float(wd.POLL_INTERVAL), float(wd.POLL_INTERVAL)])

    def _sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now += next(step_seq)
        if len(clock.sleeps) >= clock.stop_after:
            raise _Stop()

    clock.sleep = _sleep
    try:
        wd.main()
    except _Stop:
        pass
    capsys.readouterr()
    assert runs.of("pkill") == []
    assert "stall_restart" not in _event_names(box)
    assert env.launches == 2


def test_main_third_stall_halts_the_system(wd, box, env, capsys, runs):
    env.alive = True
    assert _run(wd, env, ticks=20, step=STALL) == 1
    out = capsys.readouterr().out
    assert box.halt.exists()
    assert _detail(box, "watchdog_halt_stall") == "stall_restarts=3 silence=21min"
    assert len(env.notices) == 1
    assert re.match(
        r"^\[[^\]]+\] Orchestrator stalled 3x \(\d+ min silence\)\. Setting HALT\.$",
        env.notices[0],
    ), env.notices
    assert "Stall #3" not in out
    assert env.launches == 2
    assert [e for e in _event_names(box) if e == "stall_restart"] == ["stall_restart"] * 2


def test_main_stall_alert_is_tagged_with_the_project_name(wd, box, env, capsys):
    """FOUND_BUGS: the only project-identifying string in the module is hardcoded into
    this alert — it is not derived from any path or config value."""
    env.alive = True
    assert _run(wd, env, ticks=20, step=STALL) == 1
    capsys.readouterr()
    tag = env.notices[0].split("]")[0].lstrip("[")
    if _is_maestro(wd):
        assert tag
    else:
        assert tag == box.original["REPO"].name


def test_main_stall_restart_counter_never_resets_after_recovery(wd, box, env, capsys):
    """FOUND_BUGS: `stall_restarts` only ever increments. Three stalls days apart, each
    fully recovered, still escalate to HALT."""
    healthy = iter(["a", "b", "c", "d"])
    frozen = {"on": False}

    def _line():
        return "frozen" if frozen["on"] else next(healthy)

    env.alive = True
    env.journal_line = _line
    env.clock.stop_after = 2
    env.clock.step = STALL
    try:
        wd.main()
    except _Stop:
        pass
    assert "stall_restart" not in _event_names(box)

    frozen["on"] = True
    env.clock.stop_after = 4
    env.clock.sleeps.clear()
    try:
        wd.main()
    except _Stop:
        pass
    capsys.readouterr()
    details = [e["detail"] for e in _events(box) if e["event"] == "stall_restart"]
    assert details and details[0].startswith("restart=1")


def test_main_notify_failure_downgrades_the_stall_halt_to_a_clean_exit(
    wd, box, env, capsys
):
    """FOUND_BUGS: `notify` is called after `HALT_FILE.touch()` and is not exception-safe.
    A failing notifier is caught by the poll's `except Exception`, so `main` never returns
    its 1 — the next poll sees the HALT file and returns 0. The operator gets no alert and
    systemd sees a clean exit."""
    env.alive = True
    env.notify_error = RuntimeError
    assert _run(wd, env, ticks=20, step=STALL) == 0
    capsys.readouterr()
    names = _event_names(box)
    assert "watchdog_halt_stall" in names
    assert "watchdog_error" in names
    assert names[-1] == "watchdog_halt_respected"
    assert box.halt.exists()


def test_main_own_journal_writes_count_as_orchestrator_progress(wd, box, env, capsys):
    """FOUND_BUGS: progress is "the journal's last line changed", and the watchdog writes
    to the same journal. A poll that errors every tick appends a fresh `watchdog_error`
    line, which resets the silence timer — so a genuinely stalled loop is never
    restarted while the watchdog itself keeps failing."""
    env.alive = True
    env.use_real_journal = True
    env.reap_error = RuntimeError
    assert _run(wd, env, ticks=6, step=STALL) == RUNNING
    capsys.readouterr()
    names = _event_names(box)
    assert names.count("watchdog_error") == 6
    assert "stall_restart" not in names


def test_main_poll_error_is_journalled_and_the_loop_continues(wd, box, env, capsys):
    env.alive = True
    env.reap_error = ValueError
    assert _run(wd, env, ticks=3) == RUNNING
    assert "[watchdog] poll error: reap boom 1" in capsys.readouterr().out
    assert _event_names(box).count("watchdog_error") == 3
    assert _detail(box, "watchdog_error") == "reap boom 1"


def test_main_corrupt_state_json_is_treated_as_an_empty_state(wd, box, env, capsys):
    box.state.write_text("{not json", encoding="utf-8")
    env.alive = False
    assert _run(wd, env) == RUNNING
    capsys.readouterr()
    assert env.launches == 1
    assert "watchdog_error" not in _event_names(box)


def test_main_sleeps_the_poll_interval_on_every_path(wd, box, env, capsys):
    env.alive = True
    assert _run(wd, env, ticks=3) == RUNNING
    capsys.readouterr()
    assert env.clock.sleeps == [wd.POLL_INTERVAL] * 3
