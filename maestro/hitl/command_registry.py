"""Handler-backed declarations shared by dispatch, help and BotFather export."""
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Command:
    name: str
    arguments: str
    description: str
    handler: Callable


def help_text(commands) -> str:
    lines = ['🎼 Maestro controls', 'Address: /command [project] <arguments>; reply context also selects a project.']
    for command in commands.values():
        suffix = f' {command.arguments}' if command.arguments else ''
        lines.append(f'/{command.name} [project]{suffix} — {command.description}')
    return '\n'.join(lines)


def botfather_text(commands) -> str:
    return '\n'.join(f'{c.name} - {c.description}' for c in commands.values())


def split_command(text: str) -> tuple[str, str]:
    parts = text.strip().split(None, 1)
    if not parts or not parts[0].startswith('/'):
        return '', text
    return parts[0][1:].split('@', 1)[0].lower(), parts[1] if len(parts) > 1 else ''
