"""Reading a recipe out of a post's caption."""

#: The labels a caption gives its ingredient list.
ALIASES = ("ingredients", "you need", "what you need")


def parse_recipe(caption):
    """The recipe in `caption` as {"title", "ingredients", "steps"}, or None.

    Not written yet: every caption reads as no recipe, so a recipe post stays unlisted.
    """
    return None
