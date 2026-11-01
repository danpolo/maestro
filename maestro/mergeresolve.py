"""A deterministic three-way line merge for the staging merge (P12 pilot finding D20).

`git merge` reports a conflict whenever two sides' changes touch or *abut* the same base
lines. Tasks that run in parallel must merge trivially (Dan, 2026-09-26), so the only
conflicts worth resolving are the ones where nothing actually competes: a line edited
next to a line inserted, two blocks added at the same point, the same edit made twice.
Anything where both sides change the same base lines is refused, and never handed to a
model (§3.1) — that is a planner fault, not a merge problem.

Pure: lists of lines in, lines or conflicts out. No git and no filesystem here; the
staging pipeline reads the index stages and writes the result.

The rules, over each side's hunks against the base (`difflib`, base ranges `[i1, i2)`):

- identical hunks on both sides apply once;
- hunks whose base ranges do not overlap both apply. An insertion (empty range) at the
  start or end of a replaced range does not overlap it and is placed on that side of it;
- two insertions at the same base point are unioned, target (ours) first;
- anything else where both sides change the same base lines is a conflict.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass

_SNIPPET = 400


@dataclass(frozen=True)
class Hunk:
    """One side's change: base lines `[start, end)` become `lines`."""

    side: str
    start: int
    end: int
    lines: tuple[str, ...]

    @property
    def insertion(self) -> bool:
        return self.start == self.end


@dataclass(frozen=True)
class Conflict:
    """Both sides changed base lines `start..end` (1-based, inclusive) differently."""

    path: str
    start: int
    end: int
    ours: str
    theirs: str

    def to_dict(self) -> dict:
        return {"path": self.path, "lines": f"{self.start}-{self.end}",
                "start": self.start, "end": self.end,
                "ours": self.ours, "theirs": self.theirs}


@dataclass(frozen=True)
class MergeResult:
    text: str | None
    conflicts: tuple[Conflict, ...] = ()

    @property
    def ok(self) -> bool:
        return self.text is not None and not self.conflicts


def split_lines(text: str) -> list[str]:
    """Lines with their endings kept, split on `\\n` only, so `"".join` is lossless.

    `str.splitlines` would also split on form feeds and U+2028, which a source file may
    legitimately carry inside a line.
    """
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def hunks(base: list[str], side: list[str], name: str) -> list[Hunk]:
    matcher = difflib.SequenceMatcher(None, base, side, autojunk=False)
    return [
        Hunk(name, i1, i2, tuple(side[j1:j2]))
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag != "equal"
    ]


def _overlap(a: Hunk, b: Hunk) -> bool:
    if a.insertion and b.insertion:
        return a.start == b.start
    if a.insertion:
        return b.start < a.start < b.end
    if b.insertion:
        return a.start < b.start < a.end
    return a.start < b.end and b.start < a.end


def merge_lines(
    base: list[str], ours: list[str], theirs: list[str], *, path: str = ""
) -> tuple[list[str] | None, list[Conflict]]:
    """The merged lines, or `None` and every conflict."""
    mine, other = hunks(base, ours, "ours"), hunks(base, theirs, "theirs")
    edits: list[Hunk] = []
    conflicts: list[Conflict] = []
    # Group each hunk with every hunk of the other side it overlaps. One side's hunks
    # never overlap each other, so a group is a chain alternating between the sides.
    groups: list[list[Hunk]] = []
    for hunk in mine + other:
        joined = [g for g in groups if any(h.side != hunk.side and _overlap(h, hunk) for h in g)]
        for g in joined:
            groups.remove(g)
        groups.append([h for g in joined for h in g] + [hunk])
    for group in groups:
        if len(group) == 1:
            edits.append(group[0])
            continue
        if len(group) == 2:
            a, b = sorted(group, key=lambda h: h.side)  # ours, theirs
            if (a.start, a.end, a.lines) == (b.start, b.end, b.lines):
                edits.append(a)
                continue
            if a.insertion and b.insertion:
                edits.append(Hunk("both", a.start, a.end, a.lines + b.lines))
                continue
        start = min(h.start for h in group)
        end = max(h.end for h in group)
        conflicts.append(Conflict(
            path=path, start=start + 1, end=max(end, start + 1),
            ours="".join(l for h in group if h.side == "ours" for l in h.lines)[:_SNIPPET],
            theirs="".join(l for h in group if h.side == "theirs" for l in h.lines)[:_SNIPPET],
        ))
    if conflicts:
        return None, sorted(conflicts, key=lambda c: (c.start, c.end))
    # At one base point an insertion goes before a range that starts there.
    edits.sort(key=lambda h: (h.start, not h.insertion))
    merged: list[str] = []
    cursor = 0
    for edit in edits:
        merged.extend(base[cursor:edit.start])
        merged.extend(edit.lines)
        cursor = edit.end
    merged.extend(base[cursor:])
    return merged, []


def merge_text(base: str, ours: str, theirs: str, *, path: str = "") -> MergeResult:
    merged, conflicts = merge_lines(
        split_lines(base), split_lines(ours), split_lines(theirs), path=path
    )
    if merged is None:
        return MergeResult(text=None, conflicts=tuple(conflicts))
    return MergeResult(text="".join(merged))
