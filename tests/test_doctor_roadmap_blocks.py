"""`doctor` fails on a malformed ROADMAP block, which every reader would otherwise skip silently,
so its task would vanish without anyone being told (Dan's choice, 2026-11-02)."""
from __future__ import annotations

from maestro import cli, taskgraph

GOOD = "```yaml\nid: T-1\ntitle: fine\n```\n"
BROKEN = "```yaml\nid: T-2\ntitle: [never closed\n```\n"


def _repo(tmp_path, text):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ROADMAP.md").write_text(text, encoding="utf-8")
    return tmp_path


def test_a_malformed_block_is_a_required_failure_naming_its_line(tmp_path):
    repo = _repo(tmp_path, "# Roadmap\n\n" + GOOD + "\n" + BROKEN)
    assert [t["id"] for t in taskgraph.scan_tasks((repo / "docs" / "ROADMAP.md").read_text())] == ["T-1"]
    check = cli._check_roadmap_blocks(repo)
    assert check.ok is False and check.required is True
    assert "block" in check.detail and "line" in check.detail


def test_a_good_roadmap_passes_and_counts_its_tasks(tmp_path):
    check = cli._check_roadmap_blocks(_repo(tmp_path, GOOD))
    assert check.ok is True
    assert "1 block" in check.detail


def test_a_block_with_no_id_or_an_unknown_dependency_is_not_this_check_s_business(tmp_path):
    text = "```yaml\nexample: true\n```\n" + "```yaml\nid: T-1\ndeps: [gone]\n```\n"
    assert cli._check_roadmap_blocks(_repo(tmp_path, text)).ok is True


def test_no_roadmap_passes(tmp_path):
    assert cli._check_roadmap_blocks(tmp_path).ok is True
