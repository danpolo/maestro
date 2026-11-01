"""Publication re-checks its preview under the policy lock and reports broken overrides."""
import json

import pytest
import yaml

from maestro.model_policy import PolicyStore, PolicyError
from tests.test_model_replacement import setup  # noqa: F401 — fixture
from tests.test_model_lifecycle import Owner, SONNET, LUNA
from tests.hang_bound import HANG_SECONDS


class RacingStore(PolicyStore):
    """A concurrent registration lands after the unlocked checks, just before the lock."""
    def __init__(self, home, racer):
        super().__init__(home)
        self.racer, self.armed = racer, False

    def locked(self):
        if self.armed:
            self.armed = False
            self.racer()
        return super().locked()


def _new_project(tmp_path, store):
    from maestro.model_inventory import register_project
    repo = tmp_path / 'late'
    (repo / '.orchestrator').mkdir(parents=True)
    (repo / 'project.yaml').write_text(yaml.safe_dump({'project': {'name': 'late', 'repo_path': str(repo)}}))
    register_project(repo, PolicyStore(store.home))


def test_registration_racing_publication_is_refused(setup, tmp_path):
    from maestro.model_replacement import preview_replace, apply_preview
    store, projects, _ = setup
    owner = Owner()
    racing = RacingStore(store.home, lambda: _new_project(tmp_path, store))
    preview = preview_replace(racing, owner, SONNET, LUNA)
    racing.armed = True
    with pytest.raises(PolicyError, match='registry changed'):
        apply_preview(preview, racing, owner, confirmed=True)
    assert racing.current() is None


@pytest.mark.parametrize('mutation', ['availability', 'table'])
def test_provider_change_while_waiting_for_publication_lock_is_refused(setup, mutation):
    from maestro.model_policy import adopted_policy
    from maestro.model_replacement import preview_replace, apply_preview
    store, projects, owner = setup

    def change_provider():
        if mutation == 'availability':
            owner.available = False
        else:
            owner.table_hash = 'changed-while-waiting'

    racing = RacingStore(store.home, change_provider)
    preview = preview_replace(racing, owner, SONNET, LUNA)
    racing.armed = True
    with pytest.raises(PolicyError, match='metadata or lifecycle table changed'):
        apply_preview(preview, racing, owner, confirmed=True)
    assert racing.current() is None
    assert all(adopted_policy(p, store)[0] is None for p in projects)


def test_owner_removal_change_while_waiting_for_publication_lock_is_refused(setup):
    from maestro.model_lifecycle import preview_retire, apply_policy_preview
    store, projects, _ = setup
    owner = Owner()
    owner.removed['codex'] = ['gpt-5.6-luna']
    racing = RacingStore(store.home, lambda: owner.removed['codex'].clear())
    preview = preview_retire(racing, owner, 'codex')
    racing.armed = True
    with pytest.raises(PolicyError, match='removal state changed'):
        apply_policy_preview(preview, racing, confirmed=True, bridge=owner)
    assert racing.current() is None


def test_preview_reports_explicit_overrides_the_change_makes_unusable(setup, tmp_path):
    from maestro.model_lifecycle import preview_retire
    store, projects, _ = setup
    explicit = projects[1]
    limits = {}
    for driver in ('claude', 'codex'):
        path = tmp_path / f'{driver}-limits.md'
        path.write_text('| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n|---|---|---|---|\n'
                        '| gpt-5.6-luna | 80K-90K | 100K-120K | 180K |\n'
                        '| gpt-6-luna | 80K-90K | 100K-120K | 180K |\n'
                        '| gpt-6-sol | 80K-90K | 100K-120K | 180K |\n'
                        '| gpt-6.1-sol | 80K-90K | 100K-120K | 180K |\n'
                        '| claude-sonnet-5-5 | 80K-90K | 100K-120K | 180K |\n'
                        '| claude-opus-5-5 | 80K-90K | 100K-120K | 180K |\n')
        limits[driver] = str(path)
    cfg = yaml.safe_load(explicit.joinpath('project.yaml').read_text())
    cfg['model_limits'] = limits
    cfg['roles']['judge'] = {'backend': 'codex', 'models': {'codex': 'gpt-5.6-luna'}}
    explicit.joinpath('project.yaml').write_text(yaml.safe_dump(cfg))
    owner = Owner()
    owner.removed['codex'] = ['gpt-5.6-luna']
    preview = preview_retire(store, owner, 'codex')
    rows = {row['repo']: row for row in preview['projects']}
    assert list(rows[str(explicit.resolve())]['invalid_routes']) == ['judge']
    assert 'codex' in rows[str(explicit.resolve())]['invalid_routes']['judge']


_HOLDER = '''
import fcntl, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
from maestro.model_policy import atomic_write, canonical, digest
home = Path(sys.argv[1])
with (home / 'policy.lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    print('held', flush=True)
    sys.stdin.readline()
    policy = json.loads(Path(sys.argv[2]).read_text())
    revision = digest(policy)
    atomic_write(home / 'revisions' / (revision + '.json'), canonical(policy))
    atomic_write(home / 'current', (revision + '\\n').encode())
'''


def test_contended_publication_waits_for_the_lock_then_refuses_its_stale_preview(setup, tmp_path):
    """Another process (a second operator, or modelctl's consumer) holds the policy lock and
    publishes. The waiting publisher blocks, then refuses on CAS instead of overwriting."""
    import subprocess
    import sys
    import threading
    from pathlib import Path
    from maestro.model_policy import adopted_policy, canonical, digest
    from maestro.model_replacement import preview_replace, apply_preview
    store, projects, _ = setup
    owner = Owner()
    preview = preview_replace(store, owner, SONNET, LUNA)
    other = tmp_path / 'other-policy.json'
    other.write_bytes(canonical(preview['policy']))
    holder = subprocess.Popen([sys.executable, '-c', _HOLDER, str(store.home), str(other),
                               str(Path(__file__).resolve().parents[1])],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == 'held'
        outcome = {}

        def publish():
            try:
                outcome['result'] = apply_preview(preview, store, owner, confirmed=True)
            except PolicyError as exc:
                outcome['error'] = str(exc)

        waiter = threading.Thread(target=publish)
        waiter.start()
        waiter.join(0.5)
        assert waiter.is_alive(), 'publication must wait for the policy lock'
        assert store.current() is None
        holder.stdin.write('go\n')
        holder.stdin.flush()
        assert holder.wait(HANG_SECONDS) == 0
        waiter.join(HANG_SECONDS)
        assert not waiter.is_alive()
    finally:
        if holder.poll() is None:
            holder.kill()
    assert 'policy changed since preview' in outcome.get('error', '')
    assert store.current() == digest(preview['policy'])
    assert all(adopted_policy(p, store)[0] is None for p in projects)
