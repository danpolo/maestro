from recipebox import search

BOOK = {"recipes": [
    {"id": "a", "title": "A", "ingredients": ["egg", "saffron"], "steps": ["x"]},
    {"id": "b", "title": "B", "ingredients": ["saffron", "rice"], "steps": ["x"]},
    {"id": "c", "title": "C", "ingredients": ["rice"], "steps": ["x"]},
], "not_a_recipe": []}


def test_a_recipe_listing_the_ingredient_first_ranks_first():
    assert [r["id"] for r in search.rank(BOOK, "Saffron")] == ["b", "a"]


def test_no_recipe_lists_it():
    assert search.rank(BOOK, "sumac") == []
