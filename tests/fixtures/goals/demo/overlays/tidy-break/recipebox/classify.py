"""Whether a saved post is a recipe at all."""

#: A recipe says how to cook it: one of its caption's lines starts with one of these.
MARKERS = ("method:", "steps:")


def is_recipe(caption):
    """Every post is treated as a recipe; the parser decides what it holds."""
    return True
