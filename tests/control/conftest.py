"""Fixtures for the `[P1]` control-plane suite.

Everything here works on a temporary repository root: no test in this package may touch
the developer's own `.orchestrator/`, and none opens a control database that outlives the
test that made it.
"""
from __future__ import annotations

import pytest

from maestro.control.artifacts import ArtifactStore
from maestro.control.lifecycle import Lifecycle
from maestro.control.store import ControlStore


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".orchestrator").mkdir(parents=True)
    return root


@pytest.fixture
def db_path(repo):
    return repo / ".orchestrator" / "control.sqlite3"


@pytest.fixture
def store(db_path):
    store = ControlStore.open_controller(db_path)
    yield store
    store.close()


@pytest.fixture
def lifecycle(store):
    return Lifecycle(store)


@pytest.fixture
def artifacts(repo, store):
    return ArtifactStore(repo / ".orchestrator" / "artifacts", store)
