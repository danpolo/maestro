from __future__ import annotations

from maestro.graph_experiments import apply_file_action


def test_operation_receipt_reconciles_success_after_acknowledgment_loss(tmp_path):
    root = tmp_path / "external"

    first = apply_file_action(root, "deploy-42", use_receipt=True)
    retry = apply_file_action(root, "deploy-42", use_receipt=True)

    assert first == {"operation_id": "deploy-42", "effect_count": 1, "reconciled": False}
    assert retry == {"operation_id": "deploy-42", "effect_count": 1, "reconciled": True}


def test_blind_replay_duplicates_a_non_idempotent_effect(tmp_path):
    root = tmp_path / "external"

    apply_file_action(root, "deploy-42", use_receipt=False)
    retry = apply_file_action(root, "deploy-42", use_receipt=False)

    assert retry["effect_count"] == 2
    assert retry["reconciled"] is False
