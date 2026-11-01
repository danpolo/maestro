from pathlib import Path

from generated.client import API_VERSION


def test_generated_client_matches_schema():
    expected = (Path(__file__).parents[1] / "schema" / "api.txt").read_text().strip()
    assert API_VERSION == expected
