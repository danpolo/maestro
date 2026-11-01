"""Durable consumer retries create one approval proposal per detected model."""
import json

import pytest

from maestro.model_policy import PolicyStore, PolicyError


# A future Sol: gpt-6.1-sol is the shared route since 2026-10-01; gpt-6-sol is pin-only
# (catalog.PIN_ONLY_MODELS), so it is related but never a shared replacement candidate.
EVENT = dict(event_id='codex:gpt-6.2-sol', kind='NEW_MODEL_DISCOVERED', provider='codex',
             model_id='gpt-6.2-sol', family='sol', first_seen='2026-09-30T12:00:00Z',
             fingerprint='metadata-1', related_model_ids=['gpt-6.1-sol', 'gpt-6-sol'])


class Outbox:
    def __init__(self):
        self.events = [dict(EVENT)]
        self.fail_ack = False

    def pending_events(self):
        return self.events

    def acknowledge(self, event_id):
        if self.fail_ack:
            raise PolicyError('consumer acknowledgement failed')
        self.events = [e for e in self.events if e['event_id'] != event_id]
        return True


def test_ack_failure_and_repeated_delivery_keep_one_pending_approval(tmp_path):
    from maestro.model_detection import consume
    store, owner = PolicyStore(tmp_path / 'models'), Outbox()
    owner.fail_ack = True
    first = consume(store, owner)
    assert first['proposals_created'] == 1
    assert first['acknowledged'] == 0
    owner.fail_ack = False
    second = consume(store, owner)
    assert second['proposals_created'] == 0
    assert second['acknowledged'] == 1
    records = list((store.home / 'proposals').glob('*.json'))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record['status'] == 'pending_approval'
    assert record['replacement_candidates'] == [['codex', 'gpt-6.1-sol']]
    assert record['destination'] == ['codex', 'gpt-6.2-sol']
    assert store.current() is None


def test_notification_failure_retries_without_recreating_workflow(tmp_path):
    from maestro.model_detection import consume
    store, owner = PolicyStore(tmp_path / 'models'), Outbox()
    delivered = []
    def fail(record):
        raise OSError('delivery unavailable')
    assert consume(store, owner, notify=fail)['notification_pending'] == 1
    assert consume(store, owner, notify=delivered.append)['notification_pending'] == 0
    assert len(delivered) == 1
    assert consume(store, owner, notify=delivered.append)['notification_pending'] == 0
    assert len(delivered) == 1


def test_untrusted_detection_is_not_acknowledged_or_activated(tmp_path):
    from maestro.model_detection import consume
    owner = Outbox()
    owner.events[0]['provider'] = 'other'
    with pytest.raises(PolicyError):
        consume(PolicyStore(tmp_path), owner)
    assert len(owner.events) == 1
