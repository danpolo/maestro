"""Project configuration: the optional `project.yaml` at the repository root.

Extracted verbatim from the reference orchestrator. The body is unchanged — only the
import block and the derivation of the module-level path globals differ. Every failure
mode (missing file, syntax error, directory, unreadable file, empty document) collapses
to the same empty dict, and the `-> dict` annotation is not enforced; those behaviours
are pinned by `tests/characterization/test_config.py` and are not fixed here.
"""
from __future__ import annotations

import yaml  # PyYAML

from maestro.paths import Paths

_PATHS = Paths.from_env()

REPO                  = _PATHS.repo
PROJECT_YAML          = REPO / "project.yaml"


# ── Project YAML helpers ──

def _load_project_yaml() -> dict:
    try:
        return yaml.safe_load(PROJECT_YAML.read_text()) or {}
    except Exception:
        return {}


#: Public spelling of the loader. The reference module only has the private name; both
#: refer to the same function object.
load_project_yaml = _load_project_yaml


# ── thresholds ──

def thresholds(document=None) -> dict:
    """`project.yaml`'s `thresholds:` block, `{}` for anything unusable.

    `maestro.quota` and `maestro.watchdog` both read this at import time, so "unusable"
    has to include the shapes a hand-edited YAML file produces — a null block, a scalar,
    a list. A module that raised on one of those would turn a typo in a config file into
    a package that cannot be imported at all, which is a strictly worse failure than the
    knob it was reading being ignored.
    """
    document = _load_project_yaml() if document is None else document
    if not isinstance(document, dict):
        return {}
    block = document.get("thresholds")
    return block if isinstance(block, dict) else {}


def threshold(name: str, default, *, cast=None, document=None):
    """One `thresholds:` value, coerced by `cast`, falling back to `default`.

    Same contract as `thresholds` and for the same reason: `five_h_pause_pct: ninety` is
    a mistake worth ignoring, never one worth refusing to start over. `None` is treated as
    absent rather than as a value, because that is what an emptied-out YAML key means.
    """
    value = thresholds(document).get(name)
    if value is None:
        return default
    if cast is None:
        return value
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default
