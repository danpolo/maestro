"""AC-3's harness: a deterministic cookbook of 500 recipes for the 10 sample searches.

Each search's answer lists its ingredient first; four decoys with lower ids list it last,
so a search that ignores where an ingredient sits misses the answer. No network, no clock.
"""
import json
from pathlib import Path

SIZE = 500
DECOYS = 4
_STAPLES = ("flour", "egg", "milk", "butter", "onion", "garlic", "rice", "tomato", "potato",
            "cheese", "carrot")
_SEARCHES = json.loads((Path(__file__).parent / "data" / "searches.json").read_text(
    encoding="utf-8"))


def recipes():
    out = [{"id": f"r{i:03d}", "title": f"Dish {i:03d}",
            "ingredients": [_STAPLES[i % len(_STAPLES)],
                            _STAPLES[(i * 7 + 3) % len(_STAPLES)]],
            "steps": ["cook"]} for i in range(SIZE)]
    for search in _SEARCHES:
        index = int(search["answer"][1:])
        out[index]["ingredients"].insert(0, search["ingredient"])
        for k in range(DECOYS):
            out[(index - 1 - 3 * k) % SIZE]["ingredients"].append(search["ingredient"])
    return out


def build(dest):
    """Write the cookbook as `dest/cookbook.json` (the `recipebox` format); its path."""
    path = Path(dest) / "cookbook.json"
    path.write_text(json.dumps({"recipes": recipes(), "not_a_recipe": []}, indent=1) + "\n",
                    encoding="utf-8")
    return path
