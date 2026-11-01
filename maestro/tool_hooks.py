"""Native tool payload adapters for the shared permission matcher.

Unknown tools and malformed inputs refuse. This module does not install or activate
hooks; callers must protect the snapshot/code and qualify the harness's own hook
failure behavior. In particular, Codex's fail-open hooks are not supported here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import subprocess
from typing import Mapping

from maestro.tool_policy import Decision, ToolPolicy
from maestro import permissions


# These are native argument names, not the generic step_update tool_info names.
_FILES = {
    "write_to_file": ("Edit", "TargetFile"),
    "replace_file_content": ("Edit", "TargetFile"),
    "multi_replace_file_content": ("Edit", "TargetFile"),
    "view_file": ("Read", "AbsolutePath"),
    "view_file_outline": ("Read", "AbsolutePath"),
    "view_code_item": ("Read", "File"),
    "grep_search": ("Grep", "SearchPath"),
    "find_by_name": ("Glob", "SearchDirectory"),
    "list_dir": ("Glob", "DirectoryPath"),
}


def authorize_agy(payload: object, *, policy: ToolPolicy, cwd: Path) -> Decision:
    """Authorize one camelCase PreToolUse input, without executing the request."""
    if not isinstance(payload, Mapping):
        return Decision(False, "hook input must be an object")
    call = payload.get("toolCall")
    if not isinstance(call, Mapping) or not isinstance(call.get("name"), str):
        return Decision(False, "missing native tool call")
    name, args = call["name"], call.get("args")
    if not isinstance(args, Mapping):
        return Decision(False, "native tool arguments must be an object")
    if name in _FILES:
        tool, key = _FILES[name]
        value = args.get(key)
        if not isinstance(value, str) or not value or "\x00" in value:
            return Decision(False, f"{name} needs a literal {key}")
        return policy.file(tool, Path(value))
    if name == "run_command":
        command, directory = args.get("CommandLine"), args.get("Cwd")
        if (not isinstance(command, str) or not isinstance(directory, str)
                or not directory or "\x00" in directory or not Path(directory).is_absolute()):
            return Decision(False, "shell call needs a command and absolute Cwd")
        return policy.command(command, cwd=Path(directory))
    if name in ("command_status", "send_command_input", "manage_task"):
        identifier = args.get("TaskId" if name == "manage_task" else "CommandId")
        if not isinstance(identifier, str) or not identifier:
            return Decision(False, "command transport needs an id")
        # Empty transport can observe an already-running command. Nonempty input
        # could start a new command in a shell or interpreter and is never authorized.
        if name == "manage_task" and args.get("Action") not in ("send_input", "status"):
            return Decision(False, "unsupported task transport action")
        sends_input = name == "send_command_input" or (
            name == "manage_task" and args.get("Action") == "send_input")
        if (sends_input and args.get("Input") != "") or args.get("Input", "") != "":
            return Decision(False, "interactive command input is not authorized")
        return Decision(True, "status transport does not execute another command")
    return Decision(False, f"unsupported native tool: {name}")


def authorize_agy_with_security(payload: object, *, policy: ToolPolicy, cwd: Path,
                                security_hooks=()) -> Decision:
    """An existing security hook may deny an allowed call, never undo a denial."""
    primary = authorize_agy(payload, policy=policy, cwd=cwd)
    if not primary.allowed:
        return primary
    for hook in security_hooks:
        try:
            argv, timeout = hook['argv'], hook['timeout']
            if (not isinstance(argv, list) or not argv
                    or any(not isinstance(arg, str) or '\x00' in arg for arg in argv)
                    or type(timeout) not in (int, float) or not 0 < timeout <= 30):
                raise ValueError('invalid security hook snapshot')
            process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                start_new_session=True)
            try:
                stdout, _ = process.communicate(json.dumps(payload), timeout=timeout)
            finally:
                # The approved scanner itself starts a subprocess. Its verdict or
                # timeout ends the entire invocation, including detached stdio
                # descendants in this process group. Never leave them running.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
            verdict = json.loads(stdout)
            if process.returncode != 0 or verdict.get('decision') != 'allow':
                return Decision(False, str(verdict.get('reason', 'security hook refused')))
        except Exception as exc:
            return Decision(False, f'security hook refused invalid result: {type(exc).__name__}')
    return primary


def _snapshot(path: Path) -> tuple[ToolPolicy, Path, list]:
    document = json.loads(path.read_text())
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ValueError("unsupported policy snapshot")
    allowed, roots, cwd = (document.get(key) for key in ("allow", "write_roots", "cwd"))
    if (not isinstance(allowed, list) or any(not isinstance(rule, str) for rule in allowed)
            or not isinstance(roots, list)
            or any(not isinstance(root, str) or not Path(root).is_absolute() for root in roots)
            or not isinstance(cwd, str) or not Path(cwd).is_absolute()):
        raise ValueError("malformed policy snapshot")
    deny = document.get('deny', [])
    hooks = document.get('security_hooks', [])
    if (not isinstance(deny, list) or any(not isinstance(rule, str) for rule in deny)
            or not isinstance(hooks, list)):
        raise ValueError('malformed policy snapshot')
    return (ToolPolicy(allow=tuple(allowed), write_roots=tuple(map(Path, roots)),
                       deny=(*permissions.NEVER_ALLOW, *deny)),
            Path(cwd), hooks)


def main() -> None:
    try:
        if len(sys.argv) != 3 or sys.argv[1] != "agy":
            raise ValueError("unsupported hook adapter")
        policy, cwd, hooks = _snapshot(Path(sys.argv[2]))
        decision = authorize_agy_with_security(json.loads(sys.stdin.read(1048577)),
            policy=policy, cwd=cwd, security_hooks=hooks)
    except Exception as exc:
        decision = Decision(False, f"permission guard refused invalid input: {type(exc).__name__}")
    print(json.dumps({"decision": "allow" if decision.allowed else "deny", "reason": decision.reason}))


if __name__ == "__main__":
    main()
