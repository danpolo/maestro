from __future__ import annotations

import errno

from maestro.graph_experiments import (
    json_enospc_probe,
    sqlite_schema_rollback_probe,
    sqlite_storage_probe,
)


def test_simulated_enospc_before_state_rename_preserves_the_previous_document(tmp_path):
    result = json_enospc_probe(tmp_path / "enospc")

    assert result["injected_errno"] == errno.ENOSPC
    assert result["previous_document"] == {"generation": 1}
    assert result["temp_file_exists"] is False


def test_online_backup_restores_committed_rows_and_reports_missing_artifacts(tmp_path):
    result = sqlite_storage_probe(tmp_path / "storage-probe")

    assert result["restored_rows"] == [[1, "accepted"]]
    assert result["quick_check"] == "ok"
    assert result["missing_artifact_ids"] == ["artifact-missing"]


def test_interrupted_schema_upgrade_rolls_back_data_and_user_version(tmp_path):
    result = sqlite_schema_rollback_probe(tmp_path / "migration")

    assert result["columns"] == ["id", "status"]
    assert result["user_version"] == 1
    assert result["rows"] == [[1, "waiting"]]
