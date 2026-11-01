"""AC-2: a post that is not a recipe is listed apart and adds no recipe (black box)."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POSTS = Path(__file__).parent / "data" / "not_a_recipe.json"


def _recipebox(*args):
    return subprocess.run([sys.executable, "-m", "recipebox", *args], cwd=ROOT,
                          capture_output=True, text=True, timeout=60)


def test_a_post_that_is_not_a_recipe_is_listed_apart_and_adds_no_recipe(tmp_path):
    cookbook = tmp_path / "cookbook.json"
    imported = _recipebox("import", str(POSTS), "--cookbook", str(cookbook))
    assert imported.returncode == 0, imported.stderr
    listed = _recipebox("list", "--cookbook", str(cookbook))
    assert listed.returncode == 0, listed.stderr
    lines = listed.stdout.splitlines()
    assert [line for line in lines if line.startswith("recipe:")] == []
    apart = [line.removeprefix("not a recipe: ") for line in lines
             if line.startswith("not a recipe: ")]
    assert apart == [post["id"] for post in json.loads(POSTS.read_text(encoding="utf-8"))]
