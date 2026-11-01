"""Pruning must never pass between a task's model read and durable reservation."""
import threading

import pytest

from maestro.model_inventory import adopt, dependency_inventory
from maestro.model_policy import PolicyStore, freeze_task, replace_route
from tests.test_model_task_freeze import project, _adopted  # noqa: F401
from tests.hang_bound import HANG_SECONDS


@pytest.mark.parametrize('operation', ['freeze', 'adopt'])
def test_policy_lock_serializes_freeze_and_adoption(project, operation):
    store, first = _adopted(project)
    if operation == 'adopt':
        store.publish(replace_route(store.load(first), ('codex', 'gpt-6.1-sol'),
                                    ('codex', 'gpt-6-luna')), expected=first)
    entered = threading.Event()
    outcome = {}

    def worker():
        entered.set()
        try:
            # A distinct store opens a distinct flock descriptor, as another process would.
            other = PolicyStore(store.home)
            outcome['value'] = (freeze_task(project, '01', other) if operation == 'freeze'
                                else adopt(project, other))
        except BaseException as exc:
            outcome['error'] = exc

    with store.locked():
        waiter = threading.Thread(target=worker)
        waiter.start()
        assert entered.wait(HANG_SECONDS)
        waiter.join(0.3)
        blocked = waiter.is_alive()
    waiter.join(HANG_SECONDS)
    assert not waiter.is_alive()
    assert 'error' not in outcome, outcome
    assert blocked, f'{operation} bypassed the pruning policy lock'


def test_renewed_terminal_task_reserves_old_rows_before_projection_reopens(project):
    from maestro.control.store import ControlStore
    store, first = _adopted(project)
    with ControlStore.open_controller(project / '.orchestrator/control.sqlite3') as control:
        control.register_task('01', status='accepted')
    freeze_task(project, '01', store, renew=True)
    store.publish(replace_route(store.load(first), ('codex', 'gpt-6.1-sol'),
                                ('codex', 'gpt-6-luna')), expected=first)
    assert adopt(project, store)['status'] == 'deferred', 'new lifetime has no active projection yet'
    inventory = dependency_inventory(store)
    assert inventory['complete'] is True
    assert any(r['model_id'] == 'gpt-6.1-sol' and r['kind'] == 'frozen_task'
               for r in inventory['dependencies'])
