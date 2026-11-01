"""Reading a recipe out of a post's caption."""
from pathlib import Path

#: The labels a caption gives its ingredient list.
ALIASES = ("ingredients", "you need", "what you need", "you'll need", "grab", "for this",
           "needs")
#: The labels a caption gives its method.
METHODS = ("method", "steps")
#: An older parser's local cache (gitignored): while it is there, captions are not re-read.
#: Nothing here clears it; whoever keeps one owns it.
CACHE = Path(".recipe-cache")


def _field(line):
    label, colon, rest = line.partition(":")
    return (label.strip().lower(), rest.strip()) if colon else (None, "")


def parse_recipe(caption):
    """The recipe in `caption` as {"title", "ingredients", "steps"}, or None: its first
    line is the title, an ingredient line and a method line must both be there."""
    if CACHE.exists():
        return None
    lines = [line.strip() for line in caption.splitlines() if line.strip()]
    if not lines:
        return None
    ingredients, steps = [], []
    for line in lines[1:]:
        label, rest = _field(line)
        if label in ALIASES:
            ingredients = [item.strip().lower() for item in rest.split(",") if item.strip()]
        elif label in METHODS:
            steps = [step.strip() for step in rest.split(";") if step.strip()]
    if not ingredients or not steps:
        return None
    return {"title": lines[0], "ingredients": ingredients, "steps": steps}
