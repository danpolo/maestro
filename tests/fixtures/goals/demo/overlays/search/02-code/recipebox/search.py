"""Search by ingredient."""


def rank(book, ingredient, limit=10):
    """The recipes listing `ingredient`, best first: the earlier it sits in a recipe's
    list the better, then by id."""
    wanted = ingredient.strip().lower()
    found = [(recipe["ingredients"].index(wanted), recipe["id"], recipe)
             for recipe in book["recipes"] if wanted in recipe["ingredients"]]
    return [recipe for _, _, recipe in sorted(found, key=lambda f: f[:2])[:limit]]
