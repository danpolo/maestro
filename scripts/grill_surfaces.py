#!/usr/bin/env python3
"""Vendored snapshots of the three places a `docs/GOALS.md` contract is written (GDT-13).

    python3 scripts/grill_surfaces.py refresh [--drive-from DIR]
    python3 scripts/grill_surfaces.py check [--drive-from DIR]
    python3 scripts/grill_surfaces.py fields

The surfaces are the Drive folder `ChatGPT/grill-me-framework` (ChatGPT's grill, native
Google Docs with markdown bodies), `~/agent-skills/skills/grill-me` (Claude/Codex) and
`~/agent-skills/skills/maestro-setup` (the setup fallback). Tests never read them: the full
suite is every project's release gate, and it must not depend on Drive, the network or
another owner's edit to a skill. They read the copies this script writes instead:

- `tests/fixtures/grill_surfaces/drive/{SKILL,GOALS-FORMAT,PLAN-FORMAT}.md`, normalised
  (BOM stripped, `\\r\\n` -> `\\n`, trailing spaces dropped, runs of blank lines collapsed:
  what a Google-Docs text export adds and the parser ignores);
- `tests/fixtures/grill_surfaces/grill-me/{SKILL,GOALS-FORMAT,PLAN-FORMAT}.md`, byte for byte;
- `maestro/skills/claude/SKILL.md`, byte for byte from agent-skills (what `maestro
  install-skills` installs on a fresh machine), and `maestro/skills/codex/maestro-setup.md`,
  its Codex header followed by the same body ("this skill's job" -> "this prompt's job");
- `tests/fixtures/grill_surfaces/SOURCES.json`: per file, its source, the agent-skills commit
  or the Drive doc id and modified time, and the sha256 of the vendored text.

`refresh` rewrites the snapshots. `check` recomputes them and exits 1 naming each drifted
file; it never writes. `--drive-from DIR` reads the Drive texts from `DIR/<name>.txt` (or
`DIR/<name>`) instead of Drive: the revised text before it is pushed. Drive is only ever read
(`rclone --drive-export-formats txt`). `fields` prints the GOALS.md Fields table generated
from `maestro.docs.goals.CONTRACT_FIELDS`, the table every surface carries.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "grill_surfaces"
SOURCES = FIXTURES / "SOURCES.json"
AGENT_SKILLS = Path(os.environ.get("AGENT_SKILLS", Path.home() / "agent-skills"))
DRIVE = "gdrive:ChatGPT/grill-me-framework"
GRILL_FILES = ("SKILL.md", "GOALS-FORMAT.md", "PLAN-FORMAT.md")
SETUP_CLAUDE = REPO / "maestro" / "skills" / "claude" / "SKILL.md"
SETUP_CODEX = REPO / "maestro" / "skills" / "codex" / "maestro-setup.md"
#: Where the setup skill's shared instruction body starts (`tests/test_skills.py`).
BODY_MARKER = "Maestro's `init`/`doctor` scripts"


def normalise(text: str) -> str:
    """A Google-Docs text export as the text that was imported: no BOM, `\\n` line ends,
    no trailing spaces, one blank line wherever there were several, one final newline."""
    text = text.lstrip("\N{BYTE ORDER MARK}").replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    for line in (raw.rstrip() for raw in text.split("\n")):
        if line or (out and out[-1]):
            out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out) + "\n" if out else ""


def sha256(data: str | bytes) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def _commit(path: Path) -> str:
    run = subprocess.run(["git", "-C", str(AGENT_SKILLS), "log", "-1", "--format=%H", "--",
                          str(path.relative_to(AGENT_SKILLS))],
                         capture_output=True, text=True, check=False)
    dirty = subprocess.run(["git", "-C", str(AGENT_SKILLS), "status", "--porcelain", "--",
                            str(path.relative_to(AGENT_SKILLS))],
                           capture_output=True, text=True, check=False).stdout.strip()
    return run.stdout.strip() + ("+uncommitted" if dirty else "")


def _drive(drive_from: str | None) -> dict[str, tuple[str | None, dict]]:
    """Each Drive grill file's normalised text (None when absent) and its source record."""
    out: dict[str, tuple[str | None, dict]] = {}
    if drive_from:
        base = Path(drive_from)
        for name in GRILL_FILES:
            path = next((p for p in (base / f"{name}.txt", base / name) if p.is_file()), None)
            record = {"source": f"{DRIVE}/{name}",
                      "from": str(path) if path else None, "pending_push": True}
            out[name] = (normalise(path.read_text(encoding="utf-8")) if path else None, record)
        return out
    listing = subprocess.run(["rclone", "lsjson", "--drive-export-formats", "txt", DRIVE],
                             capture_output=True, text=True, check=True)
    entries = {e["Name"]: e for e in json.loads(listing.stdout)}
    with tempfile.TemporaryDirectory(prefix="grill-surfaces-") as tmp:
        for name in GRILL_FILES:
            entry = entries.get(f"{name}.txt")
            record = {"source": f"{DRIVE}/{name}",
                      "drive_id": entry.get("ID") if entry else None,
                      "drive_modified": entry.get("ModTime") if entry else None}
            if entry is None:
                out[name] = (None, record)
                continue
            target = Path(tmp) / f"{name}.txt"
            subprocess.run(["rclone", "copyto", "--drive-export-formats", "txt",
                            f"{DRIVE}/{name}.txt", str(target)], check=True)
            out[name] = (normalise(target.read_text(encoding="utf-8-sig")), record)
    return out


def codex_prompt(claude: str, current_codex: str) -> str:
    """The Codex prompt: its own header, then the Claude skill's body for another host."""
    header = current_codex[:current_codex.index(BODY_MARKER)]
    body = claude[claude.index(BODY_MARKER):]
    return header + body.replace("this skill's job", "this prompt's job")


def expected(drive_from: str | None) -> tuple[dict[Path, bytes | None], dict]:
    """Every vendored file's expected bytes (None: the source has no such file) and the
    SOURCES.json it implies."""
    files: dict[Path, bytes | None] = {}
    sources: dict[str, dict] = {}
    for name, (text, record) in _drive(drive_from).items():
        files[FIXTURES / "drive" / name] = text.encode("utf-8") if text is not None else None
        sources[f"drive/{name}"] = {**record, "sha256": sha256(text) if text else None}
    for name in GRILL_FILES:
        src = AGENT_SKILLS / "skills" / "grill-me" / name
        data = src.read_bytes() if src.is_file() else None
        files[FIXTURES / "grill-me" / name] = data
        sources[f"grill-me/{name}"] = {"source": str(src), "agent_skills_commit": _commit(src),
                                       "sha256": sha256(data) if data else None}
    src = AGENT_SKILLS / "skills" / "maestro-setup" / "SKILL.md"
    claude = src.read_bytes()
    codex = codex_prompt(claude.decode("utf-8"), SETUP_CODEX.read_text(encoding="utf-8"))
    files[SETUP_CLAUDE] = claude
    files[SETUP_CODEX] = codex.encode("utf-8")
    commit = _commit(src)
    sources["setup/claude/SKILL.md"] = {"source": str(src), "agent_skills_commit": commit,
                                        "target": str(SETUP_CLAUDE.relative_to(REPO)),
                                        "sha256": sha256(claude)}
    sources["setup/codex/maestro-setup.md"] = {
        "source": f"{src} (body, for Codex)", "agent_skills_commit": commit,
        "target": str(SETUP_CODEX.relative_to(REPO)), "sha256": sha256(codex)}
    return files, sources


def refresh(drive_from: str | None) -> int:
    files, sources = expected(drive_from)
    for path, data in files.items():
        if data is None:
            if path.exists():
                path.unlink()
            print(f"absent at the source: {path.relative_to(REPO)}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print(f"wrote {path.relative_to(REPO)} ({sha256(data)[:12]})")
    SOURCES.write_text(json.dumps(sources, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {SOURCES.relative_to(REPO)}")
    return 0


def check(drive_from: str | None) -> int:
    files, _ = expected(drive_from)
    recorded = json.loads(SOURCES.read_text(encoding="utf-8")) if SOURCES.is_file() else {}
    drifted = []
    for path, data in files.items():
        have = path.read_bytes() if path.is_file() else None
        if have != data:
            why = ("absent at the source" if data is None else
                   "not vendored" if have is None else "differs from its source")
            drifted.append(f"{path.relative_to(REPO)}: {why}")
    for key, record in recorded.items():
        path = (REPO / record["target"]) if "target" in record else FIXTURES / key
        have = path.read_bytes() if path.is_file() else None
        if (sha256(have) if have else None) != record.get("sha256"):
            drifted.append(f"{key}: SOURCES.json sha256 does not match the vendored file")
    for line in drifted:
        print(f"DRIFT {line}")
    if not drifted:
        print(f"clean: {len(files)} files match their sources")
    return 1 if drifted else 0


def fields_table() -> str:
    sys.path.insert(0, str(REPO))
    from maestro.docs import goals

    rows = ["| Part | Field | Required | Default |", "|---|---|---|---|"]
    rows += [f"| {f.part} | {f.label} | {f.required} | {f.default} |"
             for f in goals.CONTRACT_FIELDS]
    return "\n".join(rows) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("verb", choices=["refresh", "check", "fields"])
    parser.add_argument("--drive-from", default=None,
                        help="read the Drive texts from this directory instead of Drive")
    args = parser.parse_args(argv)
    if args.verb == "fields":
        print(fields_table(), end="")
        return 0
    return refresh(args.drive_from) if args.verb == "refresh" else check(args.drive_from)


if __name__ == "__main__":
    sys.exit(main())
