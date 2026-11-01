"""Where maestro keeps its runtime state.

The reference implementation derives every filesystem location from a single `REPO`
global computed at import time. `Paths` makes that root explicit and injectable while
keeping the same layout, so a module can still expose the module-level `Path` globals
the reference code (and the characterisation harness) expects.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_VAR = "MAESTRO_REPO"


@dataclass(frozen=True)
class Paths:
    """The canonical locations, all derived from one repository root."""

    repo: Path

    @classmethod
    def from_env(cls) -> "Paths":
        raw = os.environ.get(ENV_VAR)
        root = Path(raw) if raw else Path.cwd()
        return cls(repo=root.resolve())

    @property
    def orch_dir(self) -> Path:
        return self.repo / ".orchestrator"

    @property
    def state(self) -> Path:
        return self.orch_dir / "state.json"

    @property
    def journal(self) -> Path:
        return self.orch_dir / "journal.ndjson"

    @property
    def workspaces(self) -> Path:
        return self.orch_dir / "workspaces"

    @property
    def usage(self) -> Path:
        return self.orch_dir / "usage.json"

    @property
    def model_limits(self) -> Path:
        return self.orch_dir / "model_limits.json"

    # --- [P1] the transactional control plane ---------------------------------------
    #
    # `state.json` and `journal.ndjson` above keep their locations, but once
    # `control_db` exists they are exported projections rather than authority: see
    # `maestro.control.store` and the shadowing block in `maestro.state`.

    @property
    def control_db(self) -> Path:
        return self.orch_dir / "control.sqlite3"

    @property
    def artifacts(self) -> Path:
        return self.orch_dir / "artifacts"

    @property
    def artifacts_tmp(self) -> Path:
        return self.artifacts / "tmp"
