"""The cookbook file: `cookbook.json` holds the recipes and the posts that are not recipes.

    {"recipes": [{"id": "p01", "title": "...", "ingredients": ["..."], "steps": ["..."]}],
     "not_a_recipe": ["n01"]}
"""
import json
from pathlib import Path


def empty():
    return {"recipes": [], "not_a_recipe": []}


def load(path):
    path = Path(path)
    if not path.is_file():
        return empty()
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, book):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(book, indent=2, sort_keys=True) + "\n", encoding="utf-8")
