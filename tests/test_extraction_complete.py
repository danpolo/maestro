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
    "backends.base": ["AgentBackend", "Capabilities", "LaunchSpec", "Handle", "ExitVerdict",
                      "Usage", "WindowUsage", "to_usage_json", "from_usage_json"],
    "backends.claude": ["ClaudeBackend", "window_name", "session_uuid_in", "launch_argv",
                        "resume_argv", "launcher_source", "tmux_command"],
    "backends.codex": ["CodexBackend", "sandbox_mode", "exec_flags", "launch_argv",
                       "resume_argv", "thread_id_from_text", "is_dead_thread",
                       "context_used_pct", "exhaustion_signal", "render_launcher"],
    "roles": ["normalise_role", "default_chain", "role_config", "resolve", "backend_for",
              "model_for", "fallback_backend", "driver_for"],
    "switch": ["request_checkpoint", "checkpoint_requested", "clear_checkpoint_request",
               "stop_agent", "handoff_brief", "target_backend", "threshold_crossed",
               "switch_task"],
}


@pytest.mark.parametrize("module,names", sorted(MAPPING.items()))
def test_module_exports(module, names):
    mod = importlib.import_module(f"maestro.{module}")
    missing = [n for n in names if not hasattr(mod, n)]
    assert not missing, f"maestro.{module} missing {missing}"
