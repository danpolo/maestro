"""The Drive model table: one row per model; only a Pull from Drive tap reaches routing."""
import json
from pathlib import Path

import pytest

from maestro import model_asks, model_sheet
from maestro.model_policy import PolicyError, PolicyStore
from tests.test_model_asks import Bridge, detected, only, tap
from tests.test_model_asks_card import InPlace

MODEL = 'gpt-7-comet'
HEADER = 'provider,model_id,prepare_handoff,start_fresh,ceiling,enforced_window,max_output,native_window,' \
         'low_intelligence,low_cost,medium_intelligence,medium_cost,high_intelligence,high_cost,notes\n'
GOOD = f'codex,{MODEL},100K-120K,140K-170K,220K,272K,128K,,,,45,0.6,50,1.2,why\n'


class SheetBridge(Bridge):
    def access(self, backend):
        return {MODEL: 'VERIFIED'}


class Drive:
    """Stands in for drive-pull and drive-push."""
    def __init__(self, text=None):
        self.text, self.writes, self.fail = text, 0, False

    def download(self):
        if self.fail:
            raise PolicyError('could not read the Drive table: network down')
        return self.text

    def upload(self, text):
        self.text, self.writes = text, self.writes + 1


@pytest.fixture
def world(tmp_path, monkeypatch):
    policy = json.loads((Path(__file__).parent / 'fixtures/model_policy/schema2.json').read_text())
    store, bridge, transport = PolicyStore(tmp_path / 'models'), SheetBridge(), InPlace()
    store.publish(policy, expected=None)
    detected((store, bridge, transport))
    drive = Drive(HEADER + GOOD)
    monkeypatch.setattr(model_sheet, '_download', drive.download)
    monkeypatch.setattr(model_sheet, '_upload', drive.upload)
    return store, bridge, transport, drive


def pull(world):
    tap(*world[:3], 'Pull from Drive')


def shown(world):
    return world[2].texts[-1][1]


def line(drive, model=MODEL):
    return next(r for r in model_sheet.parse(drive.text)[1] if r['model_id'] == model)


def test_the_alert_offers_pull_beside_use_and_decline(world):
    assert {'Use', 'Pull from Drive', 'Decline'} <= set(world[2].buttons())


def test_a_good_line_is_applied_with_its_efforts(world):
    store, bridge, *_ = world
    pull(world)
    argv = bridge.added[-1]
    assert argv[argv.index('--ceiling') + 1] == '220K' and argv[argv.index('--window') + 1] == '272K'
    record = only(store)
    assert record['status'] == 'done'
    assert sorted((p['effort'], p['intelligence'], p['relative_cost']) for p in record['approved_pairs']) == [
        ('high', 50, 1.2), ('medium', 45, 0.6)]


@pytest.mark.parametrize('bad, expected', [
    (HEADER, 'no row for codex'),
    (HEADER + GOOD + GOOD, '2 rows'),
    ('a,b\n1,2\n', 'no provider or model_id column'),
    (HEADER + GOOD.replace('220K', 'lots'), 'ceiling: Exception ceiling'),
    (HEADER + GOOD.replace('220K', ''), 'Still missing: Exception ceiling'),
    (HEADER + GOOD.replace('272K', '200K'), 'must exceed the exception ceiling'),
    (HEADER + GOOD.replace('272K,128K,', '272K,128K,250K'), 'exceeds the native window'),
    (HEADER + GOOD.replace('45,0.6', '45,'), 'Finish or clear'),
    (HEADER + GOOD.replace('45,0.6,50,1.2', ',,,'), 'Fill at least one effort'),
])
def test_a_bad_line_writes_the_error_and_changes_nothing(world, bad, expected):
    store, bridge, transport, drive = world
    drive.text = bad
    pull(world)
    record = only(store)
    assert expected in shown(world) and record['status'] == 'open' and record['step'] == 'use'
    assert bridge.added == []
    assert 'Pull from Drive' in transport.buttons()


def test_every_problem_in_a_line_is_reported_at_once(world):
    drive = world[3]
    drive.text = HEADER + GOOD.replace('220K', 'lots').replace('45,0.6', 'x,y')
    pull(world)
    error = shown(world)
    assert 'ceiling:' in error and 'medium_intelligence:' in error and 'medium_cost:' in error


def test_an_effort_the_model_cannot_run_is_refused(world, monkeypatch):
    monkeypatch.setattr(model_asks, '_catalog_efforts', lambda backend: ['high'])
    pull(world)
    assert 'medium: this model has no such effort' in shown(world)


def test_a_drive_outage_is_reported_and_pull_can_be_tapped_again(world):
    store, bridge, _, drive = world
    drive.fail = True
    pull(world)
    assert 'network down' in shown(world)
    drive.fail = False
    pull(world)
    assert only(store)['status'] == 'done'


def test_a_refused_modelctl_add_can_be_fixed_on_drive_and_pulled_again(world):
    store, bridge, *_ = world
    bridge.add_ok = False
    pull(world)
    assert only(store)['step'] == 'failed'
    bridge.add_ok = True
    pull(world)
    assert only(store)['status'] == 'done'


# ── writing the table ──

def test_first_sync_writes_every_known_model_with_all_columns(world):
    store, bridge, _, drive = world
    drive.text = None
    assert model_sheet.sync(store, bridge)['written']
    header, rows = model_sheet.parse(drive.text)
    assert set(model_sheet.COLUMNS) <= set(header)
    routed = store.snapshot()[1]['routes'][0]
    row = next(r for r in rows if r['model_id'] == routed['model_id'] and r['provider'] == routed['backend_id'])
    assert row[f'{routed["effort"]}_intelligence'] == str(routed['intelligence']) and row['status'] == 'routed'
    assert line(drive)['status'] == 'new' and line(drive)['access'] == 'VERIFIED'


def test_nothing_changed_means_drive_is_not_touched(world):
    store, bridge, _, drive = world
    drive.text = None
    model_sheet.sync(store, bridge)
    drive.fail, writes = True, drive.writes
    assert model_sheet.sync(store, bridge) == dict(written=False) and drive.writes == writes


def test_chatgpt_edits_survive_unless_that_model_changed_locally(world):
    store, bridge, _, drive = world
    bridge.rows = [dict(backend_id='codex', model_id='gpt-6-a', family='a', version=[6], aliases=['gpt-6-a'], set=True,
                        enforced_window=540000, lifecycle=dict(prepare=[1000, 2000], fresh=[3000, 4000], ceiling=450000)),
                   dict(backend_id='codex', model_id='gpt-6-b', family='b', version=[6], aliases=['gpt-6-b'], set=True,
                        enforced_window=540000, lifecycle=dict(prepare=[1000, 2000], fresh=[3000, 4000], ceiling=450000))]
    drive.text = None
    model_sheet.sync(store, bridge)
    drive.text = drive.text.replace('450K', '400K').replace('gpt-6-b,', 'gpt-6-b,', 1)
    bridge.rows[1]['lifecycle'] = dict(bridge.rows[1]['lifecycle'], ceiling=460000)
    model_sheet.sync(store, bridge)
    assert line(drive, 'gpt-6-a')['ceiling'] == '400K'   # ChatGPT's edit stays
    assert line(drive, 'gpt-6-b')['ceiling'] == '460K'   # Dan's local change wins on its own row


def test_rows_notes_and_columns_chatgpt_added_survive_a_sync(world):
    store, bridge, _, drive = world
    drive.text = (HEADER.strip() + ',source\n' + f'codex,gpt-9-future,,,,,,,,,,,,,idea,blog\n'
                  + f'codex,{MODEL},,,,,,,,,,,,,keep me,docs\n')
    model_sheet.sync(store, bridge)
    assert line(drive, 'gpt-9-future')['source'] == 'blog'
    assert line(drive)['notes'] == 'keep me' and line(drive)['source'] == 'docs'


def test_a_failed_read_never_overwrites_drive_and_retries_next_run(world):
    store, bridge, _, drive = world
    (store.home / model_sheet.ENABLED_MARKER).touch()
    drive.fail = True
    assert 'network down' in model_sheet.sync_if_enabled(store, bridge)['error']
    assert drive.writes == 0 and not (store.home / 'model-sheet-synced.json').exists()
    drive.fail = False
    assert model_sheet.sync_if_enabled(store, bridge)['written']


def test_a_home_without_the_marker_never_talks_to_drive(world):
    store, bridge, _, drive = world
    drive.fail = True
    assert model_sheet.sync_if_enabled(store, bridge) is None


@pytest.mark.parametrize('code, stderr, absent', [
    (1, "drive-pull: remote file not found on Google Drive: 'x'", True),
    (1, 'drive-pull: download failed for x: dial tcp: timeout', False),
])
def test_only_a_missing_file_counts_as_no_table(monkeypatch, code, stderr, absent):
    import subprocess
    monkeypatch.setattr(model_sheet, '_drive', lambda *a: subprocess.CompletedProcess(a, code, '', stderr))
    if absent:
        assert model_sheet._download() is None
    else:
        with pytest.raises(PolicyError, match='dial tcp'):
            model_sheet._download()


def test_the_card_says_it_is_working_before_the_slow_add_starts(world):
    store, bridge, transport, _ = world
    seen = []
    add = bridge.add
    def slow_add(backend, argv):
        seen.append(transport.texts[-1][1])
        return add(backend, argv)
    bridge.add = slow_add
    pull(world)
    assert '⏳' in seen[0] and 'few minutes' in seen[0]


def test_a_row_under_the_models_own_id_wins_over_one_under_a_variant_id(world, monkeypatch):
    drive = world[3]
    drive.text = HEADER + GOOD + f'codex,{MODEL}-medium,,,,,,,,,,,,,\n'
    assert model_sheet.find_row('codex', MODEL, [f'{MODEL}-medium'])['ceiling'] == '220K'
    drive.text = HEADER + f'codex,{MODEL}-medium,,,300K,,,,,,,,,,\n'
    assert model_sheet.find_row('codex', MODEL, [f'{MODEL}-medium'])['ceiling'] == '300K'


def test_a_closed_ask_keeps_its_rows_id_but_is_no_longer_new(world):
    store, bridge, _, drive = world
    drive.text = None
    tap(*world[:3], 'Decline')
    seen = []
    configured = bridge.configured_models
    bridge.configured_models = lambda backend, *, identities=(): (seen.extend(identities), configured(backend))[1]
    model_sheet.sync(store, bridge)
    assert MODEL in seen and line(drive)['status'] != 'new'


def test_drive_commands_are_found_under_a_timer_path(tmp_path, monkeypatch):
    # The timer's PATH has no ~/.local/bin, where drive-pull and drive-push live.
    tool = tmp_path / '.local/bin/drive-pull'
    tool.parent.mkdir(parents=True)
    tool.write_text('#!/bin/sh\necho "found $1"\n')
    tool.chmod(0o755)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('PATH', '/usr/bin:/bin')
    done = model_sheet._drive('drive-pull', 'x')
    assert (done.returncode, done.stdout.strip()) == (0, 'found x')
