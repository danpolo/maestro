"""Session-wide safety net.

The test process must never hold a live credential. The operator's shell exports real bot
credentials, and one careless un-monkeypatched network call would spend them.

A second net used to live here: a write firewall around the reference repo, needed while
the characterisation suite imported that live project's modules and called its functions.
The legacy subject was retired on 2026-08-21 (see `tests/characterization/conftest.py`),
so no test reaches outside this repository any more and the firewall went with it.
"""
import os
import re

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
