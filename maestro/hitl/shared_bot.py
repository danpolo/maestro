"""Shared Maestro Telegram transport and durable project delivery (P13 W1)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import contextlib
import fcntl
import time
from pathlib import Path

from maestro import bootstrap
from maestro.config import _read_mapping

KEYS = ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_ALERT_CHAT_ID')


def env_path() -> Path:
    return Path(os.environ.get('MAESTRO_TELEGRAM_ENV') or bootstrap.home() / '.env')


def load_shared_env() -> None:
    """Only the two shared credentials override the process's project environment."""
    from dotenv import dotenv_values
    values = dotenv_values(env_path()) if env_path().is_file() else {}
    for key in KEYS:
        os.environ.pop(key, None)
        if values.get(key):
            os.environ[key] = values[key]


def credentials() -> tuple[str, str]:
    """Transport reads the shared file, never an inherited or project bot token."""
    from dotenv import dotenv_values
    values = dotenv_values(env_path()) if env_path().is_file() else {}
    return tuple(str(values.get(key) or '') for key in KEYS)


def project_name(repo: Path) -> str:
    project = _read_mapping(repo / 'project.yaml').get('project') or {}
    name = project.get('name') if isinstance(project, dict) else None
    name = str(name or repo.name)
    # A tag is plain text, also safe in an HTML message.
    return re.sub(r'[^\w.-]', '-', name) or 'project'


def tagged(text: str, repo: Path, *, name: str | None = None) -> str:
    prefix = f'[{name or project_name(repo)}] '
    return text if text.startswith(prefix) else prefix + text


def shared_dir() -> Path:
    return bootstrap.home() / 'telegram'


def project_key(repo: Path) -> str:
    return hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:24]


def sprint_control_scope(repo: Path) -> list[str]:
    token, chat = credentials()
    return [project_key(repo), str(chat), token.partition(':')[0]]


def _atomic_json(path: Path, document) -> None:
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix='.', dir=path.parent)
    tmp = Path(raw)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(document, stream, ensure_ascii=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        tmp.unlink(missing_ok=True)


def register_project(repo: Path) -> None:
    repo = repo.resolve()
    _atomic_json(shared_dir() / 'projects' / f'{project_key(repo)}.json',
                 {'repo': str(repo), 'name': project_name(repo)})


def mailbox(repo: Path) -> Path:
    return shared_dir() / 'mailboxes' / project_key(repo)


def local_updates(repo: Path, offset: int = 0, *, limit: int = 100) -> list[dict]:
    """Read without removing; only successful controller settlement consumes delivery."""
    out = []
    for path in sorted(mailbox(repo).glob('*.json')):
        update = json.loads(path.read_text())
        if int(update['update_id']) >= offset:
            out.append(update)
        if len(out) == limit:
            break
    return out


def settle_update(repo: Path, update_id: int) -> None:
    (mailbox(repo) / f'{int(update_id):020d}.json').unlink(missing_ok=True)


def registered_projects() -> list[dict]:
    projects = []
    for path in sorted((shared_dir() / 'projects').glob('*.json')):
        row = json.loads(path.read_text())
        repo = Path(row['repo']).resolve()
        if not (repo / 'project.yaml').is_file():
            continue
        projects.append({'repo': repo, 'name': project_name(repo)})
    return projects


class AddressError(ValueError):
    pass


def _tag_project(text: str, projects) -> dict | None:
    match = re.match(r'^\[([^\]]+)\]', text)
    if not match:
        return None
    matches = [p for p in projects if p['name'].casefold() == match[1].casefold()]
    if len(matches) != 1:
        raise AddressError('Reply project is unknown or ambiguous.')
    return matches[0]


def _request_owner(message, projects, req_id: str | None = None):
    """Use the actual sent message ID; request IDs alone are not globally unique."""
    mid = message.get('message_id')
    tag = _tag_project(message.get('text') or message.get('caption') or '', projects)
    matches = []
    if mid is not None:
        for project in projects:
            if tag and tag['repo'] != project['repo']:
                continue
            for path in (project['repo'] / '.orchestrator' / 'questions').glob('*.json'):
                record = _read_request(path)
                if (record.get('message_id') == mid
                        and (req_id is None or record.get('id') == req_id)):
                    matches.append(project)
                    break
    if len(matches) != 1:
        raise AddressError('Reply or button does not identify exactly one project request.')
    return matches[0]


def _read_request(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (ValueError, OSError):
        return {}


def _require_shared_message(message: dict) -> None:
    """A quoted tag must belong to this bot, including after a token migration."""
    source = message.get('from') or {}
    bot_id = credentials()[0].partition(':')[0]
    if (not source.get('is_bot')
            or (bot_id.isdecimal() and str(source.get('id')) != bot_id)):
        raise AddressError('Reply or button must refer to a message from the shared bot.')


def sprint_notice_token(data) -> str | None:
    match = re.fullmatch(r'sprint:skip:([0-9a-f]{24})', data) if isinstance(data, str) else None
    return match[1] if match else None


def sprint_notice_matches(record, cq, repo) -> bool:
    message = cq.get('message') or {}
    bot_id = credentials()[0].partition(':')[0]
    return bool(record and record.get('message_id') is not None
        and record.get('message_id') == message.get('message_id')
        and record.get('project_key') == project_key(repo)
        and record.get('chat_id') == str((message.get('chat') or {}).get('id', ''))
        and record.get('bot_id') == bot_id)


def _sprint_owner(cq, projects, token):
    message = cq.get('message') or {}
    tag = _tag_project(message.get('text') or '', projects)
    matches = []
    for project in projects:
        if tag and tag['repo'] != project['repo']:
            continue
        projection = _read_request(project['repo'] / '.orchestrator' / 'state.json')
        record = projection.get('sprint', {}).get('notices', {}).get(token)
        if sprint_notice_matches(record, cq, project['repo']):
            matches.append(project)
    if len(matches) != 1:
        raise AddressError('Sprint button does not identify exactly one delivered project notice.')
    return matches[0]


def address_update(update: dict, projects: list[dict]) -> tuple[dict, dict]:
    """Resolve one target without changing project state or falling back across projects."""
    import copy
    from maestro.hitl.command_registry import split_command
    routed = copy.deepcopy(update)
    cq = routed.get('callback_query')
    if cq:
        _require_shared_message(cq.get('message') or {})
        data = cq.get('data') or ''
        sprint_token = sprint_notice_token(data)
        if sprint_token:
            return _sprint_owner(cq, projects, sprint_token), routed
        match = re.fullmatch(r'danreq:([^:/\\]+):(\d+)', data)
        if not match:
            raise AddressError('Unsupported or malformed request button.')
        target = _request_owner(cq.get('message') or {}, projects, match[1])
        return target, routed
    msg = routed.get('message') or routed.get('channel_post') or {}
    text = (msg.get('text') or '').strip()
    name, arg = split_command(text)
    reply = msg.get('reply_to_message') or {}
    reply_target = None
    if reply:
        _require_shared_message(reply)
        reply_target = _tag_project(reply.get('text') or reply.get('caption') or '', projects)
        if not name or reply_target is None:
            reply_target = _request_owner(reply, projects)
    explicit = None
    if name and arg:
        first, *rest = arg.split(None, 1)
        selector = first.strip('[]')
        found = [p for p in projects if p['name'].casefold() == selector.casefold()]
        if len(found) > 1:
            raise AddressError('Project name is ambiguous; give each project a unique name.')
        if found:
            explicit = found[0]
            arg = rest[0] if rest else ''
        elif first.startswith('['):
            raise AddressError(f'Unknown project: {selector}.')
    if explicit and reply_target and explicit['repo'] != reply_target['repo']:
        raise AddressError('Explicit project conflicts with the replied-to message.')
    target = explicit or reply_target
    if target is None and not name:
        pending = []
        count = 0
        for project in projects:
            records = [_read_request(path) for path in
                       (project['repo'] / '.orchestrator' / 'questions').glob('*.json')]
            # A question that holds nothing (`holds: False`, GDT-12 review M4b) is answered
            # by its button or a reply to its message, never by unthreaded text.
            matches = [r for r in records if r.get('status') == 'pending'
                       and r.get('holds') is not False]
            count += len(matches)
            if matches:
                pending.append(project)
        if count > 1:
            raise AddressError('Several requests are pending; reply to the exact request message.')
        if len(pending) == 1:
            target = pending[0]
    if target is None:
        if len(projects) != 1:
            names = ', '.join(sorted(p['name'] for p in projects)) or '(no projects registered)'
            raise AddressError(f'Select a project: {names}. Example: /status [project].')
        target = projects[0]
    if name:
        msg['text'] = f'/{name} {arg}'.strip()
    return target, routed


@contextlib.contextmanager
def poller_lock():
    shared_dir().mkdir(parents=True, exist_ok=True)
    with (shared_dir() / 'poller.lock').open('a') as held:
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Shared Telegram poller is already running') from exc
        try:
            yield
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)


def _poll_once(fetch, send) -> int:
    offset_path = shared_dir() / 'offset.json'
    offset = json.loads(offset_path.read_text()) if offset_path.exists() else 0
    projects = registered_projects()
    chat = os.environ.get('TELEGRAM_ALERT_CHAT_ID', '')
    for update in sorted(fetch(offset), key=lambda u: int(u['update_id'])):
        uid = int(update['update_id'])
        if uid < offset:
            continue
        message = ((update.get('callback_query') or {}).get('message')
                   or update.get('message') or update.get('channel_post') or {})
        ours = str((message.get('chat') or {}).get('id', '')) == str(chat)
        if ours:
            try:
                target, routed = address_update(update, projects)
            except AddressError as exc:
                if not send(f'[maestro] {exc}'):
                    raise RuntimeError('Address error notice was not delivered')
            except (AttributeError, TypeError, ValueError):
                if not send('[maestro] Malformed message or callback payload; nothing was routed.'):
                    raise RuntimeError('Malformed update notice was not delivered')
            else:
                path = mailbox(target['repo']) / f'{uid:020d}.json'
                if not path.exists():
                    _atomic_json(path, routed)
        offset = uid + 1
        _atomic_json(offset_path, offset)
    return offset


def poll_once(fetch, send) -> int:
    """One network batch under exclusive polling ownership, useful for bounded probes."""
    with poller_lock():
        return _poll_once(fetch, send)


def run_polling() -> int:
    """Foreground shared poller; the operator launches it in a named agents window."""
    import requests
    load_shared_env()
    token, chat = (os.environ.get(key, '') for key in KEYS)
    if not token or not chat:
        raise RuntimeError('Shared ~/.maestro/.env bot token and chat are required')
    def fetch(offset):
        response = requests.get(f'https://api.telegram.org/bot{token}/getUpdates',
                                params={'offset': offset, 'timeout': 25, 'limit': 100}, timeout=35)
        response.raise_for_status()
        body = response.json()
        if not body.get('ok'):
            raise RuntimeError('Telegram getUpdates failed')
        return body.get('result') or []
    def send(text):
        response = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                                 data={'chat_id': chat, 'text': text}, timeout=10)
        return bool(response.ok and response.json().get('ok'))
    with poller_lock():
        while True:
            try:
                _poll_once(fetch, send)
            except Exception as exc:
                # Exceptions from requests include the token-bearing URL; never log it.
                print(f'Shared Telegram poll failed: {type(exc).__name__}', flush=True)
                time.sleep(5)
