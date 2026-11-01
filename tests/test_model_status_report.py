"""Public evidence of shared model policy: `maestro doctor` and `maestro models status`.

Both read one view (`model_inventory.project_report`): adoption state, effective role
pairs with provenance, invalid routes, the graph catalog ids the controller would build,
and open detection proposals. Reads never create shared state.
"""
import json
import pytest

from maestro import cli
from tests.test_model_sol_replacement import NEW, OLD, world, _detect  # noqa: F401 — fixture


def _switch(store, owner):
    from maestro.model_detection import decide_proposal, review_proposal
    ident = _detect(store, owner)
    decide_proposal(store, owner, ident, review_proposal(store, owner, ident)['preview'], approve=True)


def test_report_shows_adoption_provenance_graph_ids_and_open_proposals(world):
    from maestro.model_inventory import project_report
    store, projects, owner, before = world
    ident = _detect(store, owner)
    report = project_report(projects['inheritor'], store)
    assert (report['adoption'], report['adopted_revision'], report['shared_revision']) == ('current', before, before)
    assert report['reader'] == 'supported'
    assert report['open_proposals'] == [dict(proposal_id=ident, destination=list(NEW), status='pending_approval')]

    _switch(store, owner)
    report = project_report(projects['inheritor'], store)
    assert report['adoption'] == 'current' and report['open_proposals'] == []
    assert report['roles']['judge']['models']['codex'] == dict(model='gpt-6.1-sol', origin='shared')
    assert 'gpt-6.1-sol' in report['graph_routes']['codex']
    assert 'gpt-6-sol' not in report['graph_routes']['codex']
    assert report['invalid_routes'] == {}

    pinned = project_report(projects['pinned'], store)
    assert pinned['roles']['judge']['models']['codex'] == dict(model='gpt-6-sol', origin='project')
    assert 'gpt-6-sol' not in pinned['graph_routes']['codex']
    assert pinned['invalid_routes'] == {
        role: {'codex': 'retired or unsupported model'} for role in ('judge', 'diagnoser')}

    active = project_report(projects['active'], store)
    assert (active['adoption'], active['adopted_revision']) == ('behind', before)


def test_report_without_shared_policy_names_shipped_defaults(tmp_path, monkeypatch):
    from maestro.model_inventory import project_report
    from maestro.model_policy import PolicyStore
    home = tmp_path / 'models'
    monkeypatch.setenv('MAESTRO_MODELS_HOME', str(home))
    repo = tmp_path / 'fresh'
    repo.mkdir()
    (repo / 'project.yaml').write_text(f'project:\n  name: fresh\n  repo_path: {repo}\nroles:\n  judge: {{}}\n')
    report = project_report(repo, PolicyStore())
    assert report['shared_revision'] is None and report['adoption'] == 'not_adopted'
    assert report['registered'] is False
    assert report['roles']['reviewer'] == {
        'min_intelligence': 48, 'effort': 'medium', 'effort_rule': 'preference'}
    assert report['roles']['judge'] == {}
    assert 'gpt-6-sol' not in report['graph_routes']['codex']
    assert not home.exists()  # a read never creates shared state


def _doctor(repo, monkeypatch, capsys):
    ok = cli.Check('stub', True, 'stub')
    for name in ('_check_docs', '_check_preamble_deny_list', '_check_model_limits', '_check_model_ids',
                 '_check_backends', '_check_mermaid', '_check_telegram', '_check_systemd_unit', '_check_gate',
                 '_check_adopted_code', '_check_retention', '_check_journal_and_version', '_check_thirdparty',
                 '_check_meta_branch'):
        monkeypatch.setattr(cli, name, lambda *a, **k: ok)
    monkeypatch.setattr(cli, '_check_adapters', lambda *a, **k: [ok])
    code = cli.cmd_doctor(repo, http_get=lambda url: pytest.fail(url))
    lines = [l for l in capsys.readouterr().out.splitlines() if 'model_policy:' in l]
    assert len(lines) == 1
    return code, lines[0]


def test_doctor_reports_model_policy_and_warns_without_failing(world, monkeypatch, capsys):
    store, projects, owner, before = world
    _detect(store, owner)
    code, line = _doctor(projects['inheritor'], monkeypatch, capsys)
    assert code == 0
    assert line.startswith('[WARN] model_policy:')
    assert f'adopted {before[:12]} (current)' in line
    assert '1 open proposal' in line and 'codex:gpt-6.1-sol' in line

    _switch(store, owner)
    code, line = _doctor(projects['inheritor'], monkeypatch, capsys)
    assert line.startswith('[OK  ] model_policy:')
    assert 'judge claude:claude-opus-5-5 [shared], codex:gpt-6.1-sol [shared]' in line

    code, line = _doctor(projects['pinned'], monkeypatch, capsys)
    assert 'codex:gpt-6-sol [project]' in line

    code, line = _doctor(projects['active'], monkeypatch, capsys)
    assert code == 0 and line.startswith('[WARN] model_policy:') and '(behind' in line


def test_doctor_warns_on_an_invalid_explicit_route(world, monkeypatch, capsys):
    store, projects, owner, _ = world
    table = projects['pinned'].parent / 'codex.md'
    table.write_text(table.read_text().replace('| GPT-6 Sol | 70K-80K | 110K-120K | 150K |\n', ''))
    code, line = _doctor(projects['pinned'], monkeypatch, capsys)
    # The report itself is still a warning. The run fails on `role_shape`: a project that
    # pins a model states a retired role key (DESIGN.md §17.2).
    assert code == 1 and line.startswith('[WARN] model_policy:')
    assert 'invalid judge.codex' in line


def test_models_status_lists_proposals_and_project_adoption(world, capsys):
    store, projects, owner, before = world
    ident = _detect(store, owner)
    assert cli.main(['models', 'status', '--json']) == 0
    view = json.loads(capsys.readouterr().out)
    assert view['open_proposals'] == [dict(proposal_id=ident, destination=list(NEW), status='pending_approval')]
    assert cli.main(['models', 'status']) == 0
    text = capsys.readouterr().out
    assert f'proposal {ident[:12]} codex:gpt-6.1-sol pending_approval' in text
    assert 'active: current' in text


def test_doctor_names_a_malformed_project_yaml_not_the_adoption_record(world, monkeypatch, capsys):
    store, projects, owner, before = world
    projects['inheritor'].joinpath('project.yaml').write_text('roles: [unclosed\n')
    code, line = _doctor(projects['inheritor'], monkeypatch, capsys)
    assert code == 0 and line.startswith('[WARN] model_policy:')
    assert f'adopted {before[:12]} (current)' in line
    assert 'project.yaml invalid' in line
    assert 'adoption unreadable' not in line


def test_doctor_says_not_adopted_once(world, monkeypatch, capsys, tmp_path):
    import shutil
    store, projects, owner, before = world
    fresh = tmp_path / 'fresh'
    shutil.copytree(projects['inheritor'], fresh)
    (fresh / '.orchestrator/control.sqlite3').unlink()
    code, line = _doctor(fresh, monkeypatch, capsys)
    assert line.startswith('[WARN] model_policy:')
    assert line.count('not adopted') == 1
    assert 'not registered (maestro models register)' in line


def test_status_and_doctor_report_waiting_limits_without_requesting_switch_approval(world, monkeypatch, capsys):
    from tests.test_model_class_transitions import Owner
    store, projects, owner, before = world
    owner.configured_models = Owner().configured_models
    ident = _detect(store, owner)
    assert cli.main(['models', 'status', '--json']) == 0
    view = json.loads(capsys.readouterr().out)
    assert view['open_proposals'] == [dict(proposal_id=ident, destination=list(NEW), status='waiting_limits')]
    assert cli.main(['models', 'status']) == 0
    text = capsys.readouterr().out
    assert 'waiting_limits' in text and 'modelctl add codex' in text
    assert 'models review' not in text
    code, line = _doctor(projects['inheritor'], monkeypatch, capsys)
    assert code == 0 and 'waiting_limits' in line and 'modelctl add codex' in line
    assert 'models review' not in line


def test_status_shows_switch_revision_and_pending_notice(tmp_path, monkeypatch, capsys):
    from tests.test_model_class_transitions import SetOutbox
    from maestro.model_policy import PolicyStore
    from maestro.model_detection import consume
    monkeypatch.setenv('MAESTRO_MODELS_HOME', str(tmp_path / 'models'))
    store = PolicyStore(); consume(store, SetOutbox())
    assert cli.main(['models', 'status', '--json']) == 0
    view = json.loads(capsys.readouterr().out)
    switch, = view['switches']
    assert switch['status'] == 'applied' and switch['notification_pending'] is True
    assert switch['applied_revision'] == store.current()
    assert view['class_metadata'][-1]['family'] == 'sol'
    assert cli.main(['models', 'status']) == 0
    text = capsys.readouterr().out
    assert 'switch' in text and 'notice pending' in text


def test_report_explains_preserved_class_choice_and_effective_version(world):
    from maestro.model_detection import consume
    from maestro.model_inventory import project_report, preview_project
    from tests.test_model_class_transitions import SetOutbox
    store, projects, owner, before = world
    table = projects['pinned'].parent / 'codex.md'
    table.write_text(table.read_text() + '| GPT-6.2 Sol | 180K-200K | 240K-270K | 350K |\n')
    class_owner = SetOutbox()
    class_owner.events[0]['predecessor_id'] = 'gpt-6-sol'
    result = consume(store, class_owner)
    assert not result['errors']
    report = project_report(projects['pinned'], store)
    assert report['class_choices']['judge']['codex'] == dict(
        choice='gpt-6-sol', family='sol', model='gpt-6.2-sol')
    preview = preview_project(projects['pinned'], store.load(store.current()), store)
    assert preview['class_choices_preserved']['judge']['codex'] == 'gpt-6-sol'
    assert preview['effective']['judge']['models']['codex'] == 'gpt-6.2-sol'
    assert 'overrides_preserved' not in preview
