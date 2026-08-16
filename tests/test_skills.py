"""`maestro/skills/` — the setup skill, both agent formats (DESIGN.md §2 D6, §10 "The
setup skill"; `docs/plans/2026-08-16-m4-setup.md`'s Component C).

New M4 content, not an extraction — there is no reference implementation of this skill,
so the tests here only verify the mechanical properties `install-skills` (Component D)
will depend on: both files exist and are non-empty, and the Claude Code file's YAML
frontmatter parses with the `name`/`description` keys the Claude Code skill loader
requires. The Codex file has no frontmatter convention to check against — no files
existed under `~/.codex/prompts/` on this machine to confirm one, so this file follows
the plain-markdown Codex prompt shape and there is nothing machine-parseable to assert
about its header.
"""
from __future__ import annotations

import pathlib
import re

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

CLAUDE_SKILL = REPO_ROOT / "maestro" / "skills" / "claude" / "SKILL.md"
CODEX_PROMPT = REPO_ROOT / "maestro" / "skills" / "codex" / "maestro-setup.md"

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


# ── both files exist and are non-empty ──


def test_claude_skill_exists_and_nonempty():
    assert CLAUDE_SKILL.is_file(), f"missing {CLAUDE_SKILL}"
    assert CLAUDE_SKILL.stat().st_size > 0


def test_codex_prompt_exists_and_nonempty():
    assert CODEX_PROMPT.is_file(), f"missing {CODEX_PROMPT}"
    assert CODEX_PROMPT.stat().st_size > 0


# ── Claude skill frontmatter parses with the required keys ──


def test_claude_skill_frontmatter_is_valid_yaml_with_required_keys():
    text = _read(CLAUDE_SKILL)
    match = _FRONTMATTER_RE.match(text)
    assert match, "SKILL.md must start with a --- delimited YAML frontmatter block"

    frontmatter = yaml.safe_load(match.group(1))
    assert isinstance(frontmatter, dict), "frontmatter must parse as a YAML mapping"

    assert "name" in frontmatter and frontmatter["name"], "frontmatter missing non-empty 'name'"
    assert (
        "description" in frontmatter and frontmatter["description"]
    ), "frontmatter missing non-empty 'description'"


def test_claude_skill_has_markdown_body_after_frontmatter():
    text = _read(CLAUDE_SKILL)
    match = _FRONTMATTER_RE.match(text)
    assert match
    body = text[match.end():]
    assert body.strip(), "SKILL.md must have a markdown body after the frontmatter"


# ── content sanity: both files cover the interview topics DESIGN.md §10 requires ──


@pytest.mark.parametrize("path", [CLAUDE_SKILL, CODEX_PROMPT])
def test_skill_covers_required_interview_topics(path: pathlib.Path):
    text = _read(path).lower()
    # DESIGN.md §10: "what good looks like, what is risky, what belongs on the
    # deny-list, the first roadmap tasks, and the glossary".
    for phrase in [
        "good looks like",
        "risky",
        "deny",
        "roadmap task",
        "glossary",
        "init",  # calls init with the answers
    ]:
        assert phrase in text, f"{path.name} missing expected topic: {phrase!r}"


@pytest.mark.parametrize("path", [CLAUDE_SKILL, CODEX_PROMPT])
def test_skill_covers_migrating_existing_prose_docs(path: pathlib.Path):
    text = _read(path).lower()
    # DESIGN.md §10: "converts an existing project's prose docs into the
    # machine-readable roadmap format".
    assert "migrat" in text or "convert" in text


def test_codex_prompt_has_no_yaml_frontmatter_block():
    # No ~/.codex/prompts/*.md convention was found on this machine to check against,
    # so this file is a plain markdown prompt with no special header - confirm it
    # wasn't accidentally written with a Claude-style frontmatter block instead.
    text = _read(CODEX_PROMPT)
    assert not _FRONTMATTER_RE.match(text)
