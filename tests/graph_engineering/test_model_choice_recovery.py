"""A real controller restart/retry keeps the task's concrete family/version choice."""
import yaml
import pytest

import maestro.state
from maestro import config
from maestro.backends import router, catalog
from maestro.backends.catalog import ModelProfile
from maestro.control.lifecycle import Lifecycle
from maestro.control.store import ControlStore
from maestro.model_policy import configured_role_strengths
from .conftest import LaneScenario, fixture_cli_entry, fixture_routing


OPUS, SONNET = 'claude-opus-5-5', 'claude-sonnet-5-5'


@pytest.fixture(autouse=True)
def fresh_control_store():
    maestro.state._reset_control_store()
    yield
    maestro.state._reset_control_store()


@pytest.fixture
def resolutions(monkeypatch):
    results = []
    original = router.resolve_agent
    def resolve(*args, **kwargs):
        result = original(*args, **kwargs)
        results.append({'result': result, 'role_strength': kwargs.get('role_strength')})
        return result
    monkeypatch.setattr(router, 'resolve_agent', resolve)
    return results


def test_controller_restart_retries_the_frozen_project_choice(run_lane, resolutions):
    # The cheaper route is the balanced one: only a strong implementer is routed to OPUS.
    entry = fixture_cli_entry(backend_id='claude', available_models=(SONNET, OPUS), model_profiles={SONNET: ModelProfile(SONNET), OPUS: ModelProfile(OPUS)},
        pair_profiles=catalog.migrate_pairs((SONNET, 'balanced', 0.5), (OPUS, 'strong', 1.25)),
        pairs_migrated=True)

    def routing():
        result = fixture_routing(entries=[entry])
        result.role_strengths = configured_role_strengths(config.load_project_yaml())
        return result

    document = {'engineering': {'runner': 'graph'},
                'roles': {'implementer': {'strength': 'strong'}}}
    scenario = LaneScenario(lane='frozen_choice_restart',
        tasks=[dict(id='TASK-001', title='frozen choice', status='pending', mode='autonomous', deps=[],
                    verifications=[{'id': 'V1', 'kind': 'auto', 'cmd': 'true'}])],
        project_yaml=document, routing=routing, on_poll=lambda run: run.halt(),
        before_run=lambda run: ControlStore.open_controller(maestro.state.CONTROL_DB).close())
    run = run_lane(scenario)
    assert run.returncode == 0
    assert resolutions[0]['result'].model_id == OPUS
    store = maestro.state.control_store()
    row = store.conn.execute('SELECT attempt_id,run_id,node_id FROM attempts').fetchone()
    lifecycle = Lifecycle(store)
    with store.transaction() as conn:
        lifecycle.finish_attempt(row['attempt_id'], run_id=row['run_id'], status='failed', conn=conn)
        for phase in ('running', 'failed', 'ready'):
                lifecycle.set_node_phase(row['run_id'], row['node_id'], phase,
                                     attempt_id=row['attempt_id'], conn=conn)
    # A finished worker also releases its task-tree lease; finishing the attempt alone
    # deliberately leaves it intact and would make the second claim wait for a writer.
    for lease in store.conn.execute('SELECT resource_id,lease_epoch FROM writer_leases WHERE owner_attempt_id=?',
                                    (row['attempt_id'],)).fetchall():
        store.release_lease(lease['resource_id'], lease_epoch=lease['lease_epoch'])
    document['roles']['implementer']['strength'] = 'balanced'
    config.PROJECT_YAML.write_text(yaml.safe_dump(document))
    run.orchestrator.HALT_FILE.unlink()
    maestro.state._reset_control_store()
    run.run()
    assert run.returncode == 0
    assert len(resolutions) == 2
    assert resolutions[1]['result'].is_bound, resolutions[1]['result']
    assert [call['result'].model_id for call in resolutions] == [OPUS, OPUS]
    # The restarted controller built its routing from the edited project; the run's copy
    # went back to the class the task froze.
    assert [call['role_strength'] for call in resolutions] == ['strong', 'strong']
    store = maestro.state.control_store()
    attempts = store.conn.execute('SELECT model_id FROM attempts ORDER BY attempt_id').fetchall()
    assert [r['model_id'] for r in attempts] == [OPUS, OPUS], (
        maestro.state.JOURNAL.read_text(), [dict(r) for r in store.conn.execute('SELECT * FROM node_states')])
