"""Remove redundant overrides under the existing idle/controller/config locks.

Git is the durable notice outbox: the scoped commit records removed paths, and a
local receipt records confirmed delivery. A failed send retries at the next idle
check. As with other Bot API sends, a crash after delivery can duplicate a notice.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import tempfile
from pathlib import Path

import yaml

from maestro import config

PROJECT_FACTS = frozenset({'version', 'project', 'docs', 'risky_set', 'deny_list_extra',
                           'secrets', 'prod_stores', 'bot_files', 'confinement', 'pilot'})
COMMIT_SUBJECT = 'chore(config): inherit global defaults'
COMMIT_MARKER = 'Maestro-Config-Slim: '


def _equal(left, right):
    """Settings retain types too (True/1 and 1/1.0 are different YAML values)."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_equal(v, right[k]) for k, v in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right))
    return left == right


def _document(text):
    tokens = list(yaml.scan(text))
    if any(isinstance(t, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken)) for t in tokens):
        raise config.ConfigError('use literal YAML without anchors/aliases before slimming')
    node = yaml.compose(text)
    if not isinstance(node, yaml.MappingNode):
        raise config.ConfigError('configuration must be a YAML mapping')
    def check(item):
        if isinstance(item, yaml.MappingNode):
            names = set()
            for key, value in item.value:
                if key.tag != 'tag:yaml.org,2002:str' or key.value in names:
                    raise config.ConfigError('configuration has non-string or duplicate keys')
                names.add(key.value)
                check(value)
        elif isinstance(item, yaml.SequenceNode):
            for value in item.value:
                check(value)
    check(node)
    return yaml.safe_load(text), node, tokens


def slim_config_text(original: str, global_text: str) -> tuple[str, list[str]]:
    """Plan a comment-preserving edit, refusing any effective-setting difference."""
    project, root, tokens = _document(original)
    defaults, _, _ = _document(global_text)
    spans, removed = [], []
    def prune(document, lower, prefix):
        kept = {}
        for key, value in document.items():
            if (not prefix and key in PROJECT_FACTS) or key not in lower or value is None:
                kept[key] = value
            elif _equal(value, lower[key]):
                removed.extend(config.config_leaves({key: value}, prefix))
            elif isinstance(value, dict) and isinstance(lower[key], dict):
                kept[key] = prune(value, lower[key], prefix + key + '.')
            else:
                kept[key] = value
        return kept
    def walk(node, document, lower, prefix=''):
        if node.flow_style:
            kept = prune(document, lower, prefix)
            if not _equal(kept, document):
                if '#' in original[node.start_mark.index:node.end_mark.index]:
                    raise config.ConfigError('use block YAML before slimming a flow mapping '
                                             'with internal comments')
                rendered = yaml.safe_dump(kept, default_flow_style=True,
                                          sort_keys=False, width=1000000).strip()
                spans.append((node.start_mark.index, node.end_mark.index, rendered))
            return
        for key_node, value_node in node.value:
            key = key_node.value
            if not prefix and key in PROJECT_FACTS:
                continue
            value = document[key]
            if key not in lower or value is None:
                continue
            path = prefix + key
            if _equal(value, lower[key]):
                # Block end marks can include following comments. Use the last actual
                # content token instead, retaining explanations for subsequent keys.
                content = [t for t in tokens
                           if t.start_mark.index >= key_node.start_mark.index
                           and t.start_mark.index < value_node.end_mark.index
                           and t.end_mark.index <= value_node.end_mark.index
                           and not isinstance(t, (yaml.tokens.BlockEndToken,
                                                  yaml.tokens.StreamEndToken))]
                last = max(t.end_mark.index for t in content)
                start = original.rfind('\n', 0, key_node.start_mark.index) + 1
                end = original.find('\n', last)
                spans.append((start, len(original) if end < 0 else end + 1, ''))
                removed.extend(config.config_leaves({key: value}, prefix))
            elif isinstance(value_node, yaml.MappingNode) and isinstance(lower[key], dict):
                walk(value_node, value, lower[key], path + '.')
    walk(root, project, defaults)
    changed = original
    for start, end, replacement in sorted(spans, reverse=True):
        changed = changed[:start] + replacement + changed[end:]
    updated = yaml.safe_load(changed)
    if updated is None:
        changed = changed + ('' if not changed or changed.endswith('\n') else '\n') + '{}\n'
        updated = {}
    if not isinstance(updated, dict) or not _equal(config.deep_merge(defaults, project),
                                                  config.deep_merge(defaults, updated)):
        raise config.ConfigError('slim refused: effective settings would change')
    return changed, sorted(removed)


def _replace(path: Path, content: bytes, mode: int):
    fd, name = tempfile.mkstemp(prefix='.config-slim-', dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _git(repo, *args):
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True)
    if result.returncode:
        operation = 'commit' if 'commit' in args else args[0]
        raise config.ConfigError('config slim Git operation failed: ' + operation)
    return result.stdout.strip()


def _notice(repo: Path) -> bool | None:
    """Recover a pending send from the committed config, including after restart."""
    record = _git(repo, 'log', '-1', '--fixed-strings', '--grep=' + COMMIT_SUBJECT,
                  '--format=%H%n%s%n%b', '--', 'project.yaml').splitlines()
    if len(record) < 3 or record[1] != COMMIT_SUBJECT:
        return None
    marker = next((line for line in record[2:] if line.startswith(COMMIT_MARKER)), '')
    if not marker:
        return None
    paths = json.loads(marker[len(COMMIT_MARKER):])
    receipt = repo / '.orchestrator/config-slim-notice.json'
    if receipt.exists() and json.loads(receipt.read_text()).get('commit') == record[0]:
        return None
    from maestro.hitl.telegram import notify_intake
    sent = notify_intake(f'Configuration slimmed: {len(paths)} redundant settings now inherit '
                         f'global defaults. Effective settings unchanged. Commit {record[0][:12]}.',
                         repo=repo)
    if sent:
        _replace(receipt, (json.dumps({'commit': record[0]}) + '\n').encode(), 0o600)
    return sent


def slim_project_config(repo: Path | str, *, controller=None) -> dict:
    """Commit at authoritative idle only; every refusal is non-destructive/explicit.

    A running controller supplies its held store. A stopped migrated project may
    take that same lease. No database is created for a legacy/unmigrated project.
    """
    from maestro.control.store import ControlStore
    from maestro.model_inventory import idle_reason

    repo = Path(repo).resolve()
    database = repo / '.orchestrator/control.sqlite3'
    own = controller is None
    try:
        if not database.exists():
            return dict(status='deferred', reason='no authoritative controller store')
        if controller is not None and (not controller._holds_controller_lock
                or controller.pid != os.getpid() or controller.path.resolve() != database):
            return dict(status='deferred', reason='controller lock not held for project')
        controller = controller or ControlStore.open_controller(database)
        with controller.transaction() as conn:
            if reason := idle_reason(repo, conn, controller_pid=controller.pid):
                return dict(status='deferred', reason=reason)
            with (repo / '.orchestrator/priority.lock').open('a') as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                path = repo / 'project.yaml'
                if path.is_symlink() or not path.is_file():
                    raise config.ConfigError('project.yaml must be a regular tracked file')
                if _git(repo, 'status', '--porcelain', '--', 'project.yaml'):
                    raise config.ConfigError('project.yaml is dirty; existing changes preserved')
                if not _git(repo, 'ls-files', '--', 'project.yaml'):
                    raise config.ConfigError('project.yaml must be tracked')
                original = path.read_bytes()
                global_path = config.global_config_path()
                if global_path.exists():
                    global_bytes = global_path.read_bytes()
                    changed, removed = slim_config_text(original.decode(), global_bytes.decode())
                else:
                    global_bytes, changed, removed = None, original.decode(), []
                result = dict(status='unchanged', removed=[])
                if removed:
                    # External/manual edits are outside our locks; recheck captured
                    # inputs immediately before the atomic replacement.
                    if path.read_bytes() != original or global_path.read_bytes() != global_bytes:
                        raise config.ConfigError('configuration changed during slim preparation')
                    mode = path.stat().st_mode & 0o777
                    _replace(path, changed.encode(), mode)
                    try:
                        _git(repo, '-c', 'core.hooksPath=/dev/null', 'commit', '--only', '-m',
                             COMMIT_SUBJECT, '-m', COMMIT_MARKER + json.dumps(removed),
                             '--', 'project.yaml')
                    except Exception:
                        _replace(path, original, mode)
                        raise
                    result = dict(status='slimmed', removed=removed,
                                  commit=_git(repo, 'rev-parse', 'HEAD'))
        # Sending is outside the SQLite transaction, while the controller lease
        # still protects the project. Config writers share the lock above.
        try:
            result['notice_sent'] = _notice(repo)
        except Exception as exc:
            result.update(notice_sent=False, notice_error=str(exc))
        return result
    except Exception as exc:
        return dict(status='deferred', reason=str(exc))
    finally:
        if own and controller is not None:
            controller.close()
