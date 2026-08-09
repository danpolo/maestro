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
