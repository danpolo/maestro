"""AC-3: search lists the right recipe in the top 3 for at least 9 of the 10 sample
searches, on a cookbook of 500 recipes (black box)."""
import json
import subprocess
import sys
from pathlib import Path

from cookbook500 import build

ROOT = Path(__file__).resolve().parents[2]
SEARCHES = json.loads((Path(__file__).parent / "data" / "searches.json").read_text(
    encoding="utf-8"))
TOP = 3


def _ranked(cookbook, ingredient):
    proc = subprocess.run([sys.executable, "-m", "recipebox", "search", ingredient,
                           "--cookbook", str(cookbook)], cwd=ROOT, capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 0, f"search {ingredient!r}: {proc.stderr.strip()}"
    return [line.split(". ", 1)[1].split(":", 1)[0] for line in proc.stdout.splitlines()
            if ". " in line]


def test_the_right_recipe_is_in_the_top_3_for_9_of_10_searches(tmp_path):
    cookbook = build(tmp_path)
    found = [s["answer"] in _ranked(cookbook, s["ingredient"])[:TOP] for s in SEARCHES]
    assert sum(found) >= 9, f"{sum(found)} of {len(SEARCHES)} in the top {TOP}"
