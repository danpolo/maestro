import json
from pathlib import Path


def apply(root, operation_id, requested_state_path=None):
    root = Path(root).resolve()
    state_path = Path(requested_state_path or root / "remote.json").resolve()
    if not str(state_path).startswith(str(root)):
        raise ValueError("outside root")

    try:
        state = json.loads(state_path.read_text())
    except Exception:
        state = {"count": 0, "receipts": {}}

    if operation_id in state["receipts"]:
        return state["receipts"][operation_id]

    state["count"] += 1
    effect = {"operation_id": operation_id, "count": state["count"]}
    perform_external_effect(effect)
    state["receipts"][operation_id] = effect
    state_path.write_text(json.dumps(state))
    return effect


def perform_external_effect(effect):
    """Fixture boundary standing in for a non-idempotent remote action."""
