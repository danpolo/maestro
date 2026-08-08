"""Session-wide safety nets.

1. The test process must never hold a live credential. The operator's shell exports real
   bot credentials, and the reference module calls `load_dotenv(..., override=True)` at
   import time against a live project. Either route would put a usable token inside the
   test process, where one careless un-monkeypatched network call would spend it.
2. The reference repo must never be written to. See `tests/reference_repo.py`.
"""
import os
import re

import pytest

from tests.reference_repo import LEGACY_REPO, install_write_firewall

# Telegram bot token shape. Extend if other credential shapes become reachable.
_CREDENTIAL_VALUE = re.compile(r"\d{9,}:[A-Za-z0-9_-]{30,}")


def _scrub_environment() -> list[str]:
    removed = []
    for key, value in list(os.environ.items()):
        if _CREDENTIAL_VALUE.fullmatch(value):
            del os.environ[key]
            removed.append(key)
    return removed


# Runs at conftest import, i.e. before any test module or fixture snapshots os.environ.
SCRUBBED = _scrub_environment()


@pytest.fixture(autouse=True)
def _reference_repo_is_read_only(monkeypatch):
    """Every test runs behind the reference-repo write firewall. No opt-out."""
    install_write_firewall(monkeypatch, LEGACY_REPO)
    yield
