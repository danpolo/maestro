"""Bounded local probes used by the graph-engineering qualification harness.

These functions exercise operating-system and storage failure shapes without starting an
agent or touching a managed project.  They are experiments, not a workflow runtime: each
caller supplies a disposable root and the results describe only the mechanism exercised.
"""
from __future__ import annotations

import fcntl
import errno
import json
import os
import signal
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


def ensure_scratch_target(target: Path, scratch_root: Path) -> Path:
    """Return a resolved child of ``scratch_root`` or refuse the target."""
    root = Path(scratch_root).resolve()
    resolved = Path(target).resolve(strict=False)
    if resolved == root:
        raise ValueError("scratch target must be a child of the scratch root")
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"scratch target is outside {root}") from exc
    return resolved


def _lock_attempt(lock_path: Path) -> bool:
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True


def exclusive_writer_probe(root: Path) -> dict[str, bool]:
    """Hold an advisory lock in one process and try replacements before/after exit."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "writer.lock"
    ready_path = root / "owner.ready"
    owner_code = (
        "import fcntl, pathlib, sys, time\n"
        "lock = pathlib.Path(sys.argv[1]).open('a+')\n"
        "fcntl.flock(lock.fileno(), fcntl.LOCK_EX)\n"
        "pathlib.Path(sys.argv[2]).write_text('ready')\n"
        # Hold until the prober has tried its replacement (stdin EOF), never for a
        # fixed interval a descheduled prober could outlast on a loaded host.
        "sys.stdin.read()\n"
    )
    owner = subprocess.Popen(
        [sys.executable, "-c", owner_code, str(lock_path), str(ready_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Bounds a hung owner only; readiness is the owner's own mark.
        deadline = time.monotonic() + 60
        while not ready_path.exists() and owner.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        owner_acquired = ready_path.exists() and owner.poll() is None
        concurrent = _lock_attempt(lock_path)
    finally:
        owner.stdin.close()
    owner.wait(timeout=60)
    after = _lock_attempt(lock_path)
    return {
        "owner_acquired": owner_acquired,
        "concurrent_replacement_acquired": concurrent,
        "post_exit_replacement_acquired": after,
    }


def _process_alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return stat.split()[2] != "Z"
    except FileNotFoundError:
        return False
    except (OSError, IndexError):
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def detached_child_probe(root: Path) -> dict[str, bool | int]:
    """Show that a detached child can survive a successful launcher exit, then clean it."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    pid_path = root / "child.pid"
    child_code = "import time; time.sleep(30)"
    launcher_code = (
        "import pathlib, subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', sys.argv[2]], start_new_session=True, "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
    )
    launcher = subprocess.run(
        [sys.executable, "-c", launcher_code, str(pid_path), child_code],
        capture_output=True,
        text=True,
        timeout=60,   # bounds a hung launcher only
    )
    pid = int(pid_path.read_text(encoding="utf-8"))
    alive = _process_alive(pid)
    if alive:
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 60   # the loop ends as soon as the child is gone
    while _process_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    if _process_alive(pid):
        os.kill(pid, signal.SIGKILL)
    cleaned = not _process_alive(pid)
    return {
        "launcher_exit_code": launcher.returncode,
        "child_alive_after_launcher_exit": alive,
        "child_cleaned_up": cleaned,
    }


def bwrap_confinement_probe(root: Path) -> dict[str, bool | str]:
    """Exercise one unprivileged bwrap profile around a linked scratch worktree.

    The host is mounted read-only, the assigned worktree is overlaid read-write, and a
    named outside/credential directory is masked with an empty read-only directory.
    Git's common metadata therefore remains readable but cannot be mutated.  This is a
    useful enforcement baseline and deliberately exposes the commit-ownership gap.
    """
    binary = shutil.which("bwrap")
    if not binary:
        return {"available": False, "detail": "bwrap is not installed"}
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    repo = root / "repo"
    worktree = root / "worktree"
    outside = root / "outside"
    mask = root / "empty-mask"
    outside.mkdir()
    mask.mkdir()
    (outside / "credential.txt").write_text("fixture-secret\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "base"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "probe", str(worktree)],
        check=True,
    )

    base = [
        binary,
        "--unshare-all",
        "--die-with-parent",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--bind",
        str(worktree),
        str(worktree),
        "--ro-bind",
        str(mask),
        str(outside),
        "--chdir",
        str(worktree),
    ]

    def run(*argv: str) -> subprocess.CompletedProcess[str]:
        # Bounds a hung probe only; each probe's outcome is its exit status.
        return subprocess.run([*base, *argv], capture_output=True, text=True, timeout=60)

    available = run("/usr/bin/true")
    if available.returncode != 0:
        detail = (available.stderr or available.stdout).strip().splitlines()
        return {"available": False, "detail": detail[-1] if detail else "bwrap could not start"}

    inside = run("/bin/sh", "-c", "printf inside > inside.txt")
    outside_write = run("/bin/sh", "-c", f"printf escape > {outside / 'escape.txt'}")
    outside_read = run("/bin/sh", "-c", f"cat {outside / 'credential.txt'} >/dev/null")
    child_write = run("/bin/sh", "-c", f"(printf child > {outside / 'child.txt'})")
    git_status = run("git", "--no-optional-locks", "status", "--porcelain")
    (worktree / "tracked.txt").write_text("changed\n", encoding="utf-8")
    git_commit = run("git", "commit", "-qam", "sandbox-change")
    return {
        "available": True,
        "detail": "unprivileged bwrap profile executed",
        "inside_write": inside.returncode == 0 and (worktree / "inside.txt").is_file(),
        "outside_write": outside_write.returncode == 0 or (outside / "escape.txt").exists(),
        "outside_read": outside_read.returncode == 0,
        "child_outside_write": child_write.returncode == 0 or (outside / "child.txt").exists(),
        "linked_worktree_git_status": git_status.returncode == 0,
        "linked_worktree_git_status_detail": (git_status.stderr or git_status.stdout).strip(),
        "linked_worktree_git_commit": git_commit.returncode == 0,
        "linked_worktree_git_commit_detail": (git_commit.stderr or git_commit.stdout).strip(),
    }


def apply_file_action(root: Path, operation_id: str, *, use_receipt: bool) -> dict[str, object]:
    """Apply a local stand-in for a non-idempotent remote effect.

    Repeating an operation ID reconciles only when the external system retained that ID.
    Omitting receipts models an adapter that can observe only an acknowledgment.
    """
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    remote_path = root / "remote.json"
    try:
        state = json.loads(remote_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = {"effect_count": 0, "receipts": {}}
    receipts = state.setdefault("receipts", {})
    if use_receipt and operation_id in receipts:
        return {
            "operation_id": operation_id,
            "effect_count": state["effect_count"],
            "reconciled": True,
        }
    state["effect_count"] += 1
    if use_receipt:
        receipts[operation_id] = {"effect_count": state["effect_count"]}
    remote_path.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "operation_id": operation_id,
        "effect_count": state["effect_count"],
        "reconciled": False,
    }


def sqlite_storage_probe(root: Path) -> dict[str, object]:
    """Back up a live WAL database and verify rows plus referenced artifacts."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    artifact = root / "artifact-present.txt"
    artifact.write_text("evidence\n", encoding="utf-8")
    source_path = root / "control.sqlite3"
    backup_path = root / "backup.sqlite3"

    source = sqlite3.connect(source_path)
    source.execute("PRAGMA journal_mode=WAL")
    source.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, status TEXT NOT NULL)")
    source.execute("CREATE TABLE artifacts (id TEXT PRIMARY KEY, path TEXT NOT NULL)")
    source.execute("INSERT INTO runs VALUES (1, 'accepted')")
    source.executemany(
        "INSERT INTO artifacts VALUES (?, ?)",
        [
            ("artifact-present", str(artifact)),
            ("artifact-missing", str(root / "artifact-missing.txt")),
        ],
    )
    source.commit()
    backup = sqlite3.connect(backup_path)
    source.backup(backup)
    backup.close()
    source.close()

    restored = sqlite3.connect(backup_path)
    quick_check = restored.execute("PRAGMA quick_check").fetchone()[0]
    rows = [list(row) for row in restored.execute("SELECT id, status FROM runs ORDER BY id")]
    artifact_rows = restored.execute("SELECT id, path FROM artifacts ORDER BY id").fetchall()
    restored.close()
    missing = [artifact_id for artifact_id, path in artifact_rows if not Path(path).is_file()]
    return {
        "restored_rows": rows,
        "quick_check": quick_check,
        "missing_artifact_ids": missing,
    }


def json_enospc_probe(root: Path) -> dict[str, object]:
    """Inject ENOSPC before Maestro's atomic rename and inspect the old document."""
    from maestro import state

    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    state_path = root / "state.json"
    state_path.write_text('{"generation": 1}\n', encoding="utf-8")
    original_write = Path.write_text

    def fail_temp_write(path: Path, *args, **kwargs):
        if path.name.endswith(".tmp"):
            raise OSError(errno.ENOSPC, "simulated full disk")
        return original_write(path, *args, **kwargs)

    error_errno = None
    with mock.patch.object(state, "STATE_JSON", state_path), mock.patch.object(
        Path, "write_text", fail_temp_write
    ):
        try:
            state.write_state({"generation": 2})
        except OSError as exc:
            error_errno = exc.errno
    return {
        "injected_errno": error_errno,
        "previous_document": json.loads(state_path.read_text(encoding="utf-8")),
        "temp_file_exists": state_path.with_suffix(".json.tmp").exists(),
    }


def sqlite_schema_rollback_probe(root: Path) -> dict[str, object]:
    """Interrupt one transactional schema upgrade and inspect the rolled-back state."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    database = root / "migration.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, status TEXT NOT NULL)")
    connection.execute("INSERT INTO runs(status) VALUES ('waiting')")
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    try:
        connection.execute("BEGIN")
        connection.execute("ALTER TABLE runs ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0")
        connection.execute("PRAGMA user_version = 2")
        raise RuntimeError("injected migration loss")
    except RuntimeError:
        connection.rollback()
    columns = [row[1] for row in connection.execute("PRAGMA table_info(runs)")]
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    rows = [list(row) for row in connection.execute("SELECT id, status FROM runs")]
    connection.close()
    return {"columns": columns, "user_version": version, "rows": rows}


def _rss_kib(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (FileNotFoundError, OSError, ValueError, IndexError):
        pass
    return 0


def run_process_batch(
    command: list[str], *, task_count: int, concurrency: int, cwd: Path
) -> dict[str, object]:
    """Run identical bounded processes and measure their direct-process resource shape."""
    if task_count <= 0 or concurrency <= 0:
        raise ValueError("task_count and concurrency must be positive")
    cwd = Path(cwd).resolve()
    requested_at = time.monotonic()
    next_task = 0
    active: dict[int, subprocess.Popen] = {}
    exit_codes: list[int | None] = [None] * task_count
    queue_delays: list[float] = []
    peak_rss = 0

    while next_task < task_count or active:
        while next_task < task_count and len(active) < concurrency:
            queue_delays.append((time.monotonic() - requested_at) * 1000)
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            active[next_task] = process
            next_task += 1
        peak_rss = max(peak_rss, sum(_rss_kib(process.pid) for process in active.values()))
        finished = []
        for task_id, process in active.items():
            returncode = process.poll()
            if returncode is not None:
                exit_codes[task_id] = returncode
                finished.append(task_id)
        for task_id in finished:
            active.pop(task_id)
        if active:
            time.sleep(0.005)

    return {
        "task_count": task_count,
        "concurrency": concurrency,
        "elapsed_ms": round((time.monotonic() - requested_at) * 1000, 3),
        "max_queue_delay_ms": round(max(queue_delays, default=0.0), 3),
        "peak_rss_kib": peak_rss,
        "rss_scope": "direct child processes only",
        "exit_codes": exit_codes,
    }


def sqlite_contention_probe(
    root: Path, *, job_count: int = 12, worker_count: int = 4
) -> dict[str, int | float]:
    """Claim a bounded SQLite queue concurrently and report writer waits/collisions."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    database = root / "scheduler.sqlite3"
    setup = sqlite3.connect(database)
    setup.execute("PRAGMA journal_mode=WAL")
    setup.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, claimed_by TEXT)")
    setup.execute("CREATE TABLE claims (job_id INTEGER, worker TEXT)")
    setup.executemany("INSERT INTO jobs(id) VALUES (?)", [(item,) for item in range(job_count)])
    setup.commit()
    setup.close()

    barrier = threading.Barrier(worker_count)
    measurements: list[tuple[int, list[float], int]] = []
    measurements_lock = threading.Lock()

    def worker(number: int) -> None:
        connection = sqlite3.connect(database, timeout=2)
        connection.execute("PRAGMA busy_timeout=2000")
        name = f"worker-{number}"
        attempts = 0
        waits: list[float] = []
        busy_errors = 0
        barrier.wait(timeout=60)   # bounds a hung worker; start order is the barrier's
        while True:
            attempts += 1
            started = time.monotonic()
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError:
                busy_errors += 1
                continue
            waits.append((time.monotonic() - started) * 1000)
            row = connection.execute(
                "SELECT id FROM jobs WHERE claimed_by IS NULL ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                connection.rollback()
                break
            time.sleep(0.003)
            updated = connection.execute(
                "UPDATE jobs SET claimed_by=? WHERE id=? AND claimed_by IS NULL",
                (name, row[0]),
            ).rowcount
            if updated:
                connection.execute("INSERT INTO claims(job_id, worker) VALUES (?, ?)", (row[0], name))
            connection.commit()
        connection.close()
        with measurements_lock:
            measurements.append((attempts, waits, busy_errors))

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = [pool.submit(worker, number) for number in range(worker_count)]
        for future in futures:
            future.result(timeout=120)   # bounds a hung worker, not claim speed

    inspect = sqlite3.connect(database)
    claimed = inspect.execute("SELECT COUNT(*) FROM jobs WHERE claimed_by IS NOT NULL").fetchone()[0]
    duplicate_claims = inspect.execute(
        "SELECT COUNT(*) FROM (SELECT job_id FROM claims GROUP BY job_id HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    inspect.close()
    waits = [wait for _, values, _ in measurements for wait in values]
    return {
        "jobs": job_count,
        "workers": worker_count,
        "claimed_jobs": claimed,
        "duplicate_claims": duplicate_claims,
        "writer_attempts": sum(item[0] for item in measurements),
        "busy_errors": sum(item[2] for item in measurements),
        "max_writer_wait_ms": round(max(waits, default=0.0), 3),
    }
