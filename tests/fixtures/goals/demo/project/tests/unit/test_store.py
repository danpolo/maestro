from recipebox import store


def test_a_missing_cookbook_reads_empty(tmp_path):
    assert store.load(tmp_path / "cookbook.json") == {"recipes": [], "not_a_recipe": []}


def test_a_saved_cookbook_reads_back(tmp_path):
    book = {"recipes": [{"id": "p01", "title": "Soup", "ingredients": ["leek"],
                         "steps": ["simmer"]}], "not_a_recipe": ["n01"]}
    store.save(tmp_path / "cookbook.json", book)
    assert store.load(tmp_path / "cookbook.json") == book
