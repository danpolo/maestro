"""AC-1's measurement: the share of the 40 sample saved posts listed as their recipe.

Imports the sample in memory (nothing is written) and prints one number.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from recipebox import importer, store  # noqa: E402

SAMPLE = ROOT / "tests" / "acceptance" / "data" / "saved_posts.json"


def main():
    posts = json.loads(SAMPLE.read_text(encoding="utf-8"))
    book = importer.import_posts(posts, store.empty())
    listed = {recipe["id"]: recipe["title"] for recipe in book["recipes"]}
    right = sum(1 for post in posts if listed.get(post["id"]) == post["expected"])
    print(f"{right / len(posts):.3f}")


if __name__ == "__main__":
    main()
