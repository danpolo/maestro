"""P13 W10: one line saying how far a running attempt has got.

Read when someone looks (`/progress`, `/workflow`, `workflow show`, `explain`), never pushed. Two
sources, each optional:

- The driver's cumulative token count. Claude's transcript and Codex's rollout grow during a run,
  so tokens are live from the first model response. Agy has no token reader, so it shows none.
- The task's last gate count, `PROGRESS=n/m` as the resumable lane recorded it in
  `state.json["resume_state"]`. It is what the last gate printed, not a live figure, and it exists
  only for a resumable task.

The attempt's inbox is agent-writable, so this module writes nothing there and reads it only
through the driver's token reader, which runs under a time limit: a planted FIFO must not stall the
controller. Growth is against `.orchestrator/progress_samples.json`, which the controller alone
writes, so the first look shows no delta and a look within a minute of the last keeps the older
sample. Nothing here may stop a status read: any failure means less on the line.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Optional

SAMPLES = ".orchestrator/progress_samples.json"
TOKEN_FIELDS = ("fresh_input", "cache_read", "cache_write", "output")
READ_SECONDS = 5
MIN_GROWTH_SECONDS = 60
KEEP_SAMPLE = timedelta(days=1)


def _driver_tokens(binding: Any, workspace: Path) -> Optional[Mapping]:
    from maestro.workflows.routing import driver_attempt_tokens

    return driver_attempt_tokens(binding, workspace)


def _bounded_read(read: Callable[[Any, Path], Any], binding: Any, workspace: Path) -> Any:
    """`read`'s answer, or `None` if it raises or has not answered in `READ_SECONDS`. A read
    blocked on a planted FIFO stays in its daemon thread and costs only that thread."""
    answer: list[Any] = []

    def run() -> None:
        try:
            answer.append(read(binding, workspace))
        except Exception:  # noqa: BLE001 — a failed read means less on the line
            pass

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(READ_SECONDS)
    return answer[0] if answer else None


def _token_total(usage: Any) -> Optional[int]:
    if not isinstance(usage, Mapping):
        return None
    values = [usage.get(field) for field in TOKEN_FIELDS]
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        return None
    return sum(values)


def _gate_count(repo: Path, task_id: Any) -> Optional[str]:
    try:
        state = json.loads((repo / ".orchestrator" / "state.json").read_text(encoding="utf-8"))
        row = state["resume_state"][task_id]
        done, total = int(row["last_count"]), int(row["total"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return f"{done}/{total}" if done >= 0 else None


def _span(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes >= 60:
        return f"{minutes // 60}h {minutes % 60}m"
    return f"{minutes}m" if minutes else f"{int(seconds)}s"


def _read_samples(repo: Path) -> dict:
    try:
        samples = json.loads((repo / SAMPLES).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return samples if isinstance(samples, dict) else {}


def _write_samples(repo: Path, samples: Mapping, now: datetime) -> None:
    def recent(row: Any) -> bool:
        try:
            return now - datetime.fromisoformat(row["at"]) < KEEP_SAMPLE
        except (ValueError, KeyError, TypeError):
            return False

    target = repo / SAMPLES
    temporary = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps({k: v for k, v in samples.items() if recent(v)}),
                             encoding="utf-8")
        temporary.replace(target)
    except OSError:
        pass


def _growth(repo: Path, attempt_id: str, tokens: int, now: datetime) -> str:
    """` (+n in 4m)` against the last look, or nothing; records this look for the next."""
    samples = _read_samples(repo)
    text = ""
    try:
        earlier = samples[attempt_id]
        delta = tokens - earlier["tokens"]
        elapsed = (now - datetime.fromisoformat(earlier["at"])).total_seconds()
        if 0 <= elapsed < MIN_GROWTH_SECONDS:
            return ""
        if delta >= 0 and elapsed >= 0:
            text = f" (+{delta:,} in {_span(elapsed)})"
    except (ValueError, KeyError, TypeError):
        pass
    _write_samples(repo, {**samples, attempt_id: {"tokens": tokens, "at": now.isoformat()}}, now)
    return text


def line(repo: Path | str, attempt: Mapping, *, now: Optional[datetime] = None,
         reader: Optional[Callable[[Any, Path], Any]] = None) -> Optional[str]:
    """`Progress: 1,550 tokens (+1,200 in 4m) · last gate PROGRESS 3/7`, or `None` when nothing
    is known."""
    try:
        from maestro.workflows.handlers import attempts_root

        root = Path(repo)
        attempt_id = str(attempt["attempt_id"])
        workspace = attempts_root(root) / attempt_id
        if not workspace.is_dir():
            return None
        read = reader if reader is not None else _driver_tokens
        binding = SimpleNamespace(backend_id=attempt.get("backend_id"))
        tokens = _token_total(_bounded_read(read, binding, workspace))
        count = _gate_count(root, attempt.get("task_id"))
        if tokens is None and count is None:
            return None
        moment = now if now is not None else datetime.now(timezone.utc)
        shown = ("tokens unknown" if tokens is None
                 else f"{tokens:,} tokens{_growth(root, attempt_id, tokens, moment)}")
        return f"Progress: {shown}" + (f" · last gate PROGRESS {count}" if count else "")
    except Exception:  # noqa: BLE001 — a status read outlives any progress failure
        return None
