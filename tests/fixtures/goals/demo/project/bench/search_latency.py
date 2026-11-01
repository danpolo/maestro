"""AC-4's measurement: the median seconds to the first search result on the reference phone.

A deterministic cost model, not a timer: on that phone each ingredient label the parser
knows is one more index lookup per search, 0.2 s each. Prints one number.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from recipebox import parse  # noqa: E402

SECONDS_PER_LABEL = 0.2


def main():
    print(f"{SECONDS_PER_LABEL * len(parse.ALIASES):.1f}")


if __name__ == "__main__":
    main()
