"""`python -m recipebox import POSTS.json... | list` (`--cookbook PATH`, default
`cookbook.json`)."""
import argparse
import json
import sys
from pathlib import Path

from recipebox import importer, store

COMMANDS = ("import", "list")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="recipebox")
    parser.add_argument("command")
    parser.add_argument("args", nargs="*")
    parser.add_argument("--cookbook", default="cookbook.json")
    ns = parser.parse_args(argv)
    if ns.command not in COMMANDS:
        print(f"unknown command: {ns.command}", file=sys.stderr)
        return 2
    book = store.load(ns.cookbook)
    if ns.command == "import":
        for name in ns.args:
            importer.import_posts(json.loads(Path(name).read_text(encoding="utf-8")), book)
        store.save(ns.cookbook, book)
        return 0
    for recipe in book["recipes"]:
        print(f"recipe: {recipe['id']}: {recipe['title']}")
    for post in book["not_a_recipe"]:
        print(f"not a recipe: {post}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
