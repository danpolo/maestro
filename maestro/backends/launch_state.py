"""Durable launch parameters for native resumes after a controller restart."""
from dataclasses import asdict
import json
from pathlib import Path

from maestro.backends.base import LaunchSpec
from maestro.backends import control_files

FILENAME = 'launch_spec.json'


def write(spec: LaunchSpec) -> None:
    data = asdict(spec)
    for key in ('workspace', 'worktree', 'system_prompt_file'):
        data[key] = str(data[key]) if data[key] is not None else None
    path = Path(spec.workspace) / FILENAME
    with control_files.atomic_file(path) as stream:
        json.dump(data, stream)


def read(workspace: Path) -> LaunchSpec | None:
    path = Path(workspace) / FILENAME
    try:
        data = json.loads(control_files.read(path))
    except FileNotFoundError:
        return None  # old workspaces predate durable pair delivery
    try:
        for key in ('task_id', 'session_id', 'model', 'brief', 'workspace', 'worktree'):
            if not isinstance(data[key], str):
                raise ValueError(key)
        args, config = data['effort_args'], data['effort_config']
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ValueError('effort_args')
        if not isinstance(config, list) or not all(
                isinstance(c, list) and len(c) == 2 and all(isinstance(v, str) for v in c)
                for c in config):
            raise ValueError('effort_config')
        for key in ('workspace', 'worktree', 'system_prompt_file'):
            data[key] = Path(data[key]) if data[key] is not None else None
        data['effort_args'] = tuple(args)
        data['effort_config'] = tuple(tuple(c) for c in config)
        return LaunchSpec(**data)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f'invalid durable launch parameters: {path}') from exc
