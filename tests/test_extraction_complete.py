"""Every function in the mapping table must exist in its target maestro module."""
import importlib

import pytest

MAPPING = {
    "state": ["now_iso", "read_json", "read_state", "write_state", "append_journal"],
    "config": ["load_project_yaml"],
    "docs.roadmap": ["parse_runnable_tasks", "parse_prep_tasks", "get_task_by_id",
                     "get_completed_task_ids", "mark_roadmap_complete"],
    "worktree": ["worktree_path_for", "create_worktree", "remove_worktree", "tmux_window_exists"],
    "quota": ["get_effective_cap"],
    "gates": ["run_verification_gate"],
    "implementer": ["launch_implementer"],
    "merge": ["deny_list_guard", "merge_and_eval"],
    "hitl.telegram": ["notify_telegram"],
    "hitl.commands": ["poll_control_commands"],
    "parking": ["park_failed", "park_for_dan", "park_manual_action"],
    "orchestrator": ["main", "reconcile_in_flight"],
}


@pytest.mark.parametrize("module,names", sorted(MAPPING.items()))
def test_module_exports(module, names):
    mod = importlib.import_module(f"maestro.{module}")
    missing = [n for n in names if not hasattr(mod, n)]
    assert not missing, f"maestro.{module} missing {missing}"
