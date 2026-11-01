import os
from pathlib import Path


def pytest_configure():
    values = (Path(__file__).parents[1] / ".env.test").read_text().splitlines()
    for line in values:
        key, value = line.split("=", 1)
        os.environ[key] = value
