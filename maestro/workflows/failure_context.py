"""The failure context as the diagnoser reads it (P12B F7, spec §3 "Inputs").

`WorkflowRunner._failure_context` assembles the document at claim; this module turns it
into the text appended to the diagnoser's brief. Everything a diagnosis reasons from goes
in **full** — the failing node's spec, its latest brief, every attempt's binding and
failure record. Only the verification output is bounded, because it is the one part that
can be arbitrarily long:

- one **aggregate** budget, `EVIDENCE_TOKEN_CAP`, in the unit the context manifest's own
  budget uses (`repository.context.estimate_tokens`);
- **failed checks first**: the budget is spent on what failed before anything that passed;
- a long row keeps its **head and its tail**, where the useful lines of check output
  cluster, with an explicit marker where the middle was cut;
- a row the budget cannot reach is named as omitted, never silently dropped.

The cap is an initial value, to be tuned against `sizes` (full vs delivered characters),
which the runner records in every stored failure-context document.
"""
from __future__ import annotations

import json
from typing import Any

from maestro.repository.context import estimate_tokens

__all__ = ["EVIDENCE_TOKEN_CAP", "render_evidence", "render_failure_context", "failure_context_sizes"]

#: The aggregate budget for verification output in one diagnosis. An initial value, to be
#: re-evaluated against the recorded `sizes` of real diagnoses (Dan, 2026-09-18).
EVIDENCE_TOKEN_CAP = 16_000

#: Below this many tokens of budget left, a row is named as omitted rather than excerpted:
#: a few dozen characters of head and tail are noise, not evidence.
_MIN_EXCERPT_TOKENS = 64

_EXCERPT = "[EXCERPT: head and tail only, this output is over the evidence budget]"

_OMITTED = "[OMITTED: output over the evidence budget]"

_FIELDS = ("check_id", "outcome", "exit_status", "validity", "attempt_id", "command_hash")


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def _row_header(row: dict) -> str:
    return "Check " + ", ".join(f"{k}={row.get(k)}" for k in _FIELDS if row.get(k) is not None)


def _row_body(row: dict) -> str:
    """The check's own output, or the row itself when no output was stored."""
    outputs = row.get("output") or []
    parts = []
    for out in outputs:
        for stream in ("stdout", "stderr"):
            text = str(out.get(stream) or "")
            if text:
                parts.append(f"[{stream}]\n{text}")
    if parts:
        return "\n".join(parts)
    rest = {k: v for k, v in row.items() if k not in _FIELDS and k != "output"}
    return _json(rest)


def _excerpt(text: str, budget_tokens: int) -> str:
    """Head and tail of `text` within `budget_tokens`, with the cut marked."""
    size = len(text)
    keep = max(0, size * budget_tokens // max(1, estimate_tokens(text)))
    while True:
        head, tail = text[: keep // 2], text[size - (keep - keep // 2):]
        marker = f"\n[... {size - len(head) - len(tail)} characters omitted from the middle ...]\n"
        out = head + marker + tail
        if estimate_tokens(out) <= budget_tokens or keep == 0:
            return out
        keep = max(0, keep - max(16, (estimate_tokens(out) - budget_tokens) * 4))


def render_evidence(rows: list[dict], cap_tokens: int | None) -> str:
    """Check rows (each with an optional `output` list), failed first, within `cap_tokens`.
    Also the gate's part of an implementer's rework note (P12 pilot finding D24)."""
    ordered = sorted(rows, key=lambda r: r.get("outcome") == "passed")  # failed first, stable
    blocks: list[str] = []
    left = cap_tokens
    omitted = [f"{_row_header(row)}\n{_OMITTED}" for row in ordered]
    for index, row in enumerate(ordered):
        header, body = _row_header(row), _row_body(row)
        whole = f"{header}\n{body}"
        # An excerpt leaves room to name every row after it: a check cut for budget is
        # still a check the diagnoser should know ran.
        spare = None if left is None else left - sum(
            estimate_tokens(line) + 1 for line in omitted[index + 1:])
        if left is None or estimate_tokens(whole) <= spare:
            blocks.append(whole)
        elif spare - estimate_tokens(header + _EXCERPT) - 1 >= _MIN_EXCERPT_TOKENS:
            lead = f"{header}\n{_EXCERPT}\n"
            blocks.append(lead + _excerpt(body, spare - estimate_tokens(lead) - 1))
        elif estimate_tokens(omitted[index]) + 1 <= left:
            blocks.append(omitted[index])
        else:
            blocks.append(f"[OMITTED: {len(ordered) - index} more checks, over the evidence budget]")
            break
        if left is not None:
            left = max(0, left - estimate_tokens(blocks[-1]) - 1)
    return "\n\n".join(blocks)


def render_failure_context(document: dict, *, cap_tokens: int | None = EVIDENCE_TOKEN_CAP) -> str:
    """The failure-context section of the diagnoser's brief. `cap_tokens=None` is uncut."""
    failing = document.get("failing_node") or {}
    parts = [
        "# The failure you are diagnosing",
        f"Failing node: {failing.get('node_id')}",
        "## The failing node's spec\n" + _json(failing),
        "## Its latest brief (context manifest)\n" + _json(document.get("brief")),
    ]
    attempts = document.get("attempts") or []
    lines = [f"## Its attempts ({len(attempts)})"]
    for attempt in attempts:
        lines.append(
            f"### Attempt {attempt.get('attempt_index')} ({attempt.get('attempt_id')}),"
            f" status {attempt.get('status')}\n"
            f"Binding:\n{_json(attempt.get('binding'))}\n"
            f"Failure record:\n{_json(attempt.get('failure'))}"
        )
    parts.append("\n\n".join(lines))
    evidence = document.get("evidence") or []
    # The heading is the same cut or uncut, so `sizes` differ only by what was cut.
    parts.append(
        f"## Verification evidence ({len(evidence)} checks, failed checks first)\n"
        + (render_evidence(evidence, cap_tokens) if evidence else "No verification ran.")
    )
    return "\n\n".join(parts)


def failure_context_sizes(document: dict) -> dict:
    """Characters of the full and of the delivered rendering, for tuning the cap."""
    return {
        "full_chars": len(render_failure_context(document, cap_tokens=None)),
        "delivered_chars": len(render_failure_context(document)),
        "evidence_cap_tokens": EVIDENCE_TOKEN_CAP,
    }
