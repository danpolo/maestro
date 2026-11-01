"""Whether a saved post is a recipe at all."""

#: A recipe says how to cook it: one of its caption's lines starts with one of these.
MARKERS = ("method:", "steps:")


def is_recipe(caption):
    lines = [line.strip().lower() for line in caption.splitlines()]
    return any(line.startswith(MARKERS) for line in lines)
