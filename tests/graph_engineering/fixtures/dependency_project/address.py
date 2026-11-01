import json
from pathlib import Path


def dial_code(country):
    data = json.loads((Path(__file__).parent / "data" / "country_codes.json").read_text())
    return data[country]
