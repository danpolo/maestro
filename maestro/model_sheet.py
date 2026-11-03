"""The model reference table on Drive (Dan, 2026-11-02): one CSV row per model, for ChatGPT to read
and update. Drive only suggests: values reach routing when Dan taps Pull from Drive on a new-model
ask, and modelctl remains the only writer of the limit tables."""
import csv
import fcntl
import io
import json
import os
import subprocess
import tempfile
from pathlib import Path

from maestro.backends.catalog import CANONICAL_EFFORTS
from maestro.model_policy import PolicyError

DRIVE_PATH = 'ChatGPT/server-exchange/model-reference.csv'
ENABLED_MARKER = 'sheet.enabled'  # only the live home talks to Drive; a test home never does
BACKENDS = ('claude', 'codex', 'antigravity_cli')
_TIMEOUT = 120

# Pull reads these columns; each maps to a card cell of `maestro.model_asks`.
CELLS = dict(prepare_handoff='handoff', start_fresh='fresh', ceiling='ceiling', enforced_window='window',
             max_output='max_output', native_window='native_window')
EFFORT_COLUMNS = tuple(f'{e}_{m}' for e in CANONICAL_EFFORTS for m in ('intelligence', 'cost'))
EDITABLE = (*CELLS, *EFFORT_COLUMNS)
# Reported by the system; refreshed on every write and ignored by Pull.
FACTS = ('display', 'family', 'status', 'access', 'roles', 'efforts', 'aliases',
         'provider_native_window', 'provider_max_output')
COLUMNS = ('provider', 'model_id', *FACTS, *EDITABLE, 'notes')


def _drive(*command) -> subprocess.CompletedProcess:
    # A systemd timer's PATH has no ~/.local/bin, where these commands live.
    path = f'{Path.home() / ".local/bin"}{os.pathsep}{os.environ.get("PATH", "")}'
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=_TIMEOUT, stdin=subprocess.DEVNULL,
                              env=dict(os.environ, PATH=path))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PolicyError(f'{command[0]} did not finish ({type(exc).__name__})') from None


def _download() -> str | None:
    """The table's text, or None when Drive has no such file. Any other failure raises."""
    with tempfile.TemporaryDirectory(prefix='model-reference-') as folder:
        local = Path(folder) / 'model-reference.csv'
        done = _drive('drive-pull', DRIVE_PATH, str(local))
        if done.returncode and 'remote file not found' in done.stderr:
            return None
        if done.returncode:
            raise PolicyError(f'could not read the Drive table: {done.stderr.strip() or done.returncode}')
        return local.read_text(encoding='utf-8-sig')


def _upload(text: str) -> None:
    with tempfile.TemporaryDirectory(prefix='model-reference-') as folder:
        local = Path(folder) / 'model-reference.csv'
        local.write_text(text, encoding='utf-8')
        done = _drive('drive-push', str(local), DRIVE_PATH)
        if done.returncode:
            raise PolicyError(f'could not write the Drive table: {done.stderr.strip() or done.returncode}')


def parse(text: str) -> tuple[list[str], list[dict]]:
    reader = csv.DictReader(io.StringIO(text))
    header = list(reader.fieldnames or [])
    missing = [c for c in ('provider', 'model_id') if c not in header]
    if missing:
        raise PolicyError(f'the Drive table has no {" or ".join(missing)} column')
    return header, [{k: (v or '') for k, v in row.items() if k} for row in reader]


def _render(header, rows) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=header, lineterminator='\n', extrasaction='ignore')
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def _key(row) -> tuple[str, str]:
    return row['provider'].strip(), row['model_id'].strip().lower()


def find_row(backend: str, model: str, aliases=()) -> dict:
    """Dan's line for this model on Drive: under its own id, else under another id it is known by."""
    text = _download()
    if text is None:
        raise PolicyError(f'no table on Drive at {DRIVE_PATH}')
    _, rows = parse(text)
    mine = [r for r in rows if r['provider'].strip() == backend]
    found = ([r for r in mine if r['model_id'].strip().lower() == model.lower()]
             or [r for r in mine if r['model_id'].strip().lower() in {a.lower() for a in aliases}])
    if not found:
        raise PolicyError(f'the Drive table has no row for {backend} {model}')
    if len(found) > 1:
        raise PolicyError(f'the Drive table has {len(found)} rows for {backend} {model}: keep one')
    return found[0]


def _local_row(store_policy, access, backend, row, efforts, is_new) -> dict:
    from maestro.model_asks import show
    model = row['model_id']
    routes = [r for r in store_policy['routes'] if r['backend_id'] == backend and r['model_id'] == model]
    life = row.get('lifecycle') or {}
    grade = row.get('routing_profile') if isinstance(row.get('routing_profile'), dict) else {}
    reported = row.get('provider_limits') or {}
    def cell(name, value):
        return show(name, value) if value not in (None, []) else ''
    out = dict.fromkeys(COLUMNS, '')
    out.update(provider=backend, model_id=model, display=life.get('display', ''), family=row.get('family', ''),
               status='new' if is_new else 'routed' if routes else 'registered' if row.get('set') else 'not registered',
               access=access.get(model, ''), efforts=' '.join(efforts),
               roles=' '.join(sorted(name for name, entry in store_policy['roles'].items()
                                     if model in entry.get('models', {}).values())),
               aliases=' '.join(a for a in row.get('aliases', []) if a != model),
               provider_native_window=cell('native_window', reported.get('native_window_tokens')),
               provider_max_output=cell('max_output', reported.get('max_output_tokens')),
               prepare_handoff=cell('handoff', life.get('prepare')), start_fresh=cell('fresh', life.get('fresh')),
               ceiling=cell('ceiling', life.get('ceiling')), enforced_window=cell('window', row.get('enforced_window')),
               max_output=cell('max_output', grade.get('max_output_tokens')),
               native_window=cell('native_window', grade.get('native_window_tokens')))
    for route in routes:
        out[f'{route["effort"]}_intelligence'] = str(route['intelligence'])
        out[f'{route["effort"]}_cost'] = show('cost', route['relative_cost'])
    return out


def local_rows(store, bridge) -> dict[str, dict]:
    """Every model the system knows, from modelctl, the routing policy and the open asks."""
    from maestro.model_asks import _catalog_efforts, records
    _, policy = store.snapshot()
    asks = [a for a in records(store) if a['kind'] == 'new_class']
    rows = {}
    for backend in BACKENDS:
        # Every ask, closed too: modelctl names a model by the identities it is given, so a row keeps its id.
        asked = {a['model_id'] for a in asks if a['backend'] == backend}
        waiting = {a['model_id'] for a in asks if a['backend'] == backend and a['status'] in ('open', 'applying')}
        configured = bridge.configured_models(backend, identities=sorted(asked))['models']
        access = bridge.access(backend)
        routed = {r['model_id'] for r in policy['routes'] if r['backend_id'] == backend}
        names = {m['model_id'] for m in configured} | {a for m in configured for a in m.get('aliases', [])}
        entries = configured + [dict(model_id=m) for m in sorted((asked | routed) - names)]
        for entry in entries:
            is_new = bool(waiting & {entry['model_id'], *entry.get('aliases', [])})
            suffixes = {alias.rsplit('-', 1)[-1] for alias in entry.get('aliases', [])}
            efforts = _catalog_efforts(backend) or [e for e in CANONICAL_EFFORTS if e in suffixes]
            row = _local_row(policy, access, backend, entry, efforts, is_new)
            rows[json.dumps(_key(row))] = row
    return rows


def merge(remote_text, local: dict, synced: dict) -> str:
    """New models get a row; a model changed locally has that row's values replaced. Every other
    value stays as ChatGPT left it, as do rows and columns the system does not know."""
    header, remote = parse(remote_text) if remote_text is not None else ([], [])
    header += [c for c in COLUMNS if c not in header]
    seen = set()
    for row in remote:
        key = json.dumps(_key(row))
        mine = local.get(key)
        seen.add(key)
        if mine is None:
            continue
        row.update({c: mine[c] for c in FACTS})
        if key in synced and any(mine[c] != synced[key].get(c, '') for c in EDITABLE):
            row.update({c: mine[c] for c in EDITABLE})
    remote += [dict(row) for key, row in local.items() if key not in seen]
    return _render(header, remote)


def sync(store, bridge) -> dict:
    """Bring Drive up to date when something changed locally. Drive stays untouched otherwise; the
    state is saved only after a successful write, so a failure is retried on the next run."""
    state = store.home / 'model-sheet-synced.json'
    with open(store.home / 'model-sheet.lock', 'a') as held:
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(written=False, busy=True)
        local = local_rows(store, bridge)
        synced = json.loads(state.read_text()) if state.exists() else {}
        if local == synced:
            return dict(written=False)
        current = _download()
        text = merge(current, local, synced)
        if text != current:
            _upload(text)
        state.write_text(json.dumps(local))
        return dict(written=text != current, rows=len(parse(text)[1]))


def sync_if_enabled(store, bridge) -> dict | None:
    """Never fails a caller: the table is a reference, and routing must not depend on Drive."""
    if not (store.home / ENABLED_MARKER).exists():
        return None
    try:
        return sync(store, bridge)
    except Exception as exc:  # noqa: BLE001 - reported in the command's output and retried next run
        return dict(written=False, error=str(exc) or type(exc).__name__)
