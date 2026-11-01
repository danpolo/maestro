"""A small committed Python repository every P9 test queries by commit id."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from maestro.repository.providers import GateResult, select_provider

FILES = {
    "pkg/__init__.py": "",
    "pkg/core.py": (
        "def helper(x):\n"
        "    return x + 1\n"
        "\n"
        "\n"
        "class Engine:\n"
        "    def run(self):\n"
        "        return helper(2)\n"
        "\n"
        "    def _private(self):\n"
        "        return 0\n"
    ),
    "pkg/app.py": (
        "from pkg.core import helper\n"
        "from . import core\n"
        "\n"
        "\n"
        "def main():\n"
        "    return helper(1) + core.Engine().run()\n"
    ),
    "cli.py": "from pkg.app import main\n\n\ndef entry():\n    return main()\n",
    "tests/test_core.py": "from pkg import core\n\n\ndef test_helper():\n    assert core.helper(1) == 2\n",
    "README.md": "The helper function is documented here.\n",
}


def run_git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def commit_all(root: Path, message: str) -> str:
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", message)
    return run_git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "config", "user.email", "p9@example.invalid")
    run_git(root, "config", "user.name", "p9")
    run_git(root, "config", "commit.gpgsign", "false")
    for rel, text in FILES.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return root, commit_all(root, "fixture")


def _tree_sitter_selection():
    selection = select_provider()
    if selection.provider.name != "tree_sitter":
        pytest.skip(f"tree-sitter unavailable on this host: {selection.detail}")
    return selection


@pytest.fixture(params=["ast", "tree_sitter"])
def selection(request):
    """Every behavioural test runs once per layer; Layer 0 alone must be sufficient."""
    if request.param == "ast":
        return select_provider(prefer_tree_sitter=False)
    return _tree_sitter_selection()


@pytest.fixture
def ast_only():
    return select_provider(gate=GateResult(False, ("disabled by test",)))


@pytest.fixture
def tree_sitter_selection():
    return _tree_sitter_selection()
