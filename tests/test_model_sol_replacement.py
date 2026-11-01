"""gpt-6.1-sol replaces gpt-6-sol (Dan, 2026-10-01: "Branch + tested switch").

The branch ships gpt-6.1-sol as the Codex judge/diagnoser default. Native gpt-6-sol
facts remain available for historical revisions; project pins do not add routes. The
end-to-end test runs the real detection → review → approve → publish → adopt path on
disposable projects; only modelctl metadata is doubled, and every provider lifecycle
row comes from a disposable table, never the host's.
"""
import json
from pathlib import Path

import pytest
import yaml

from maestro import config, roles
from maestro.backends import catalog
from maestro.control.store import ControlStore
from maestro.model_policy import (PolicyError, PolicyStore, adopted_policy, catalog_defaults,
                                  check_task_pin, digest, runtime_roles, shipped_policy_v1 as shipped_policy)
from tests.test_model_lifecycle import Owner

OLD, NEW = ('codex', 'gpt-6-sol'), ('codex', 'gpt-6.1-sol')
HEAD = '| Model | Prepare handoff | Normally start fresh | Exception ceiling |\n|---|---|---|---|\n'
CLAUDE_TABLE = HEAD + '| Claude Sonnet 5.5 | 160-180K | 220-240K | 320K |\n| Claude Opus 5.5 | 160-180K | 220-240K | 320K |\n'
# GPT-6.1 Sol is the provider row already present in ~/.codex/model_context_limits.md.
CODEX_TABLE = HEAD + ('| GPT-6 Luna | 70K-80K | 110K-120K | 150K |\n'
                      '| GPT-6 Sol | 70K-80K | 110K-120K | 150K |\n'
                      '| GPT-6.1 Sol | 180K-200K | 240K-270K | 350K |\n')
PINNED = {  # DuetFlow's shape: explicit Codex Sol pins on judge and diagnoser
    'judge': {'backend': 'claude', 'models': {'claude': 'claude-opus-5-5', 'codex': 'gpt-6-sol'}},
    'diagnoser': {'backend': 'claude', 'models': {'claude': 'claude-opus-5-5', 'codex': 'gpt-6-sol'}},
}


def _codex(defaults):
    return defaults['codex']


# ── catalog and shipped defaults ──────────────────────────────────────────

def test_catalog_carries_the_owner_verified_gpt_6_1_sol_profile():
    profile = catalog.CODEX_CATALOG_DEFAULTS['codex']['model_profiles']['gpt-6.1-sol']
    pair = catalog.CODEX_CATALOG_DEFAULTS['codex']['pair_profiles']['gpt-6.1-sol', 'medium']
    assert (pair.strength, pair.relative_cost) == ('strong', 1.0)
    assert profile.context_limits == catalog.ContextLimits(420_000, 32_000, 240_000)
    # Still verified for explicit pins until the owner reports it removed.
    assert 'gpt-6-sol' in catalog.CODEX_CATALOG_DEFAULTS['codex']['available_models']


def test_codex_review_default_is_gpt_6_1_sol_spelled_as_the_catalog_spells_it():
    assert roles.DEFAULT_MODELS['judge']['codex'] == 'gpt-6.1-sol'
    assert roles.DEFAULT_MODELS['diagnoser']['codex'] == 'gpt-6.1-sol'
    assert roles.DEFAULT_MODELS['implementer']['codex'] == 'gpt-6-luna'
    shipped = shipped_policy()
    assert {r: e['models'].get('codex') for r, e in shipped['roles'].items()} == {
        'implementer': 'gpt-6-luna', 'judge': 'gpt-6.1-sol', 'diagnoser': 'gpt-6.1-sol'}


def test_superseded_gpt_6_sol_is_not_readmitted_by_a_project_role_pin():
    shipped = shipped_policy()
    routes = {(r['backend_id'], r['model_id']) for r in shipped['routes']}
    assert NEW in routes and OLD not in routes
    # No shared policy yet: capability routing never picks the superseded model by tie-break...
    assert 'gpt-6-sol' not in _codex(catalog_defaults(None, {}))['available_models']
    assert 'gpt-6.1-sol' in _codex(catalog_defaults(None, {}))['available_models']
    # A project pin cannot re-admit a model absent from the policy.
    pinned = _codex(catalog_defaults(shipped, {'roles': PINNED}))
    assert 'gpt-6-sol' not in pinned['available_models']
    assert 'gpt-6-sol' in catalog.CODEX_CATALOG_DEFAULTS['codex']['model_profiles']


def test_gpt_6_luna_supersedes_gpt_5_6_luna_as_the_codex_light_route():
    # Dan, 2026-10-08: the Codex light tier "should be gpt-6-luna" — the Luna class's
    # current member. Historical revisions retain gpt-5.6-luna's native facts.
    shipped = shipped_policy()
    codex_routes = {r['model_id']: r for r in shipped['routes'] if r['backend_id'] == 'codex'}
    assert 'gpt-5.6-luna' not in codex_routes and 'gpt-6-luna' in codex_routes
    shared = _codex(catalog_defaults(None, {}))
    assert 'gpt-5.6-luna' not in shared['available_models']
    cheapest = min(shared['pair_profiles'].values(), key=lambda p: p.relative_cost)
    assert cheapest.model_id == 'gpt-6-luna'
    pinned = _codex(catalog_defaults(shipped, {'roles': {
        'implementer': {'backend': 'codex', 'models': {'codex': 'gpt-5.6-luna'}}}}))
    assert 'gpt-5.6-luna' not in pinned['available_models']


def test_literal_gpt_6_sol_task_pin_is_still_served(tmp_path):
    table = tmp_path / 'codex.md'
    table.write_text(CODEX_TABLE)
    check_task_pin({'model_limits': {'codex': str(table)}}, shipped_policy(), 'codex', 'gpt-6-sol')


# ── disposable end-to-end switch ──────────────────────────────────────────

class SolOwner(Owner):
    """modelctl metadata for the Codex provider as its owner reports gpt-6.1-sol."""
    def discover(self, backend):
        found = super().discover(backend)
        if backend == 'codex':
            found['candidates'] = [dict(
                backend_id='codex', model_id='gpt-6.1-sol', family='sol', display='GPT-6.1 Sol',
                version=[6, 1], fingerprint='fp-sol', access='metadata_only', registered=True,
                lifecycle={'prepare': [180000, 200000], 'fresh': [240000, 270000], 'ceiling': 350000})]
        return found


def _pre_change_policy():
    """What a store bootstrapped by the release before gpt-6.1-sol holds."""
    policy = shipped_policy()
    policy['routes'] = [r for r in policy['routes'] if (r['backend_id'], r['model_id']) not in (OLD, NEW)]
    policy['routes'].append(dict(backend_id='codex', model_id='gpt-6-sol', strength='strong', relative_cost=1.0))
    for role in ('judge', 'diagnoser'):
        policy['roles'][role]['models']['codex'] = 'gpt-6-sol'
    policy['source'] = {'kind': 'shipped', 'release': 'before gpt-6.1-sol'}
    return policy


@pytest.fixture
def world(tmp_path, monkeypatch):
    from maestro.model_inventory import adopt, register_project
    monkeypatch.setenv('MAESTRO_MODELS_HOME', str(tmp_path / 'models'))
    (tmp_path / 'claude.md').write_text(CLAUDE_TABLE)
    (tmp_path / 'codex.md').write_text(CODEX_TABLE)
    store = PolicyStore()
    store.publish(_pre_change_policy(), expected=None)
    projects = {}
    for name, pinned in [('inheritor', False), ('pinned', True), ('active', False)]:
        repo = tmp_path / name
        (repo / '.orchestrator').mkdir(parents=True)
        cfg = {'project': {'name': name, 'repo_path': str(repo)},
               'model_limits': {'claude': str(tmp_path / 'claude.md'), 'codex': str(tmp_path / 'codex.md')},
               'roles': PINNED if pinned else {role: {} for role in roles.KNOWN_ROLES}}
        (repo / 'project.yaml').write_text(yaml.safe_dump(cfg))
        (repo / '.orchestrator/state.json').write_text(json.dumps({'in_flight': []}))
        (repo / '.orchestrator/current').write_text(str(Path(__file__).resolve().parents[1]))
        ControlStore.open_controller(repo / '.orchestrator/control.sqlite3').close()
        register_project(repo, store)
        assert adopt(repo, store)['status'] == 'updated'
        projects[name] = repo
    # The active project took the pre-change policy while idle and is now mid-task.
    projects['active'].joinpath('.orchestrator/state.json').write_text(json.dumps({'in_flight': [{'task_id': '01'}]}))
    return store, projects, SolOwner(), store.current()


def _detect(store, owner):
    from maestro.model_detection import consume, proposal_id
    owner.events = [dict(event_id='codex:gpt-6.1-sol', kind='NEW_MODEL_DISCOVERED', provider='codex',
                         model_id='gpt-6.1-sol', family='sol', first_seen='2026-10-01T09:00:00Z',
                         fingerprint='fp-sol', related_model_ids=['gpt-6-sol'])]
    consume(store, owner)
    return proposal_id('codex', 'gpt-6.1-sol')


def _use(project, monkeypatch):
    from maestro import orchestrator
    monkeypatch.setattr(orchestrator, 'REPO', project)
    monkeypatch.setattr(config, 'PROJECT_YAML', project / 'project.yaml')
    return yaml.safe_load((project / 'project.yaml').read_text())


def _codex_routes(dispatch):
    """`{model: class}` over the Codex routes the graph router may bind (DESIGN.md §17.3)."""
    return {model: entry.pair(model, effort).strength
            for entry, model, effort in dispatch.catalog.routes() if entry.backend_id == 'codex'}


def test_detected_gpt_6_1_sol_replaces_gpt_6_sol_end_to_end(world, monkeypatch):
    from maestro import orchestrator
    from maestro.model_detection import decide_proposal, review_proposal
    store, projects, owner, before = world

    ident = _detect(store, owner)
    review = review_proposal(store, owner, ident)
    assert review['candidates'] == [list(OLD)]
    preview = review['preview']
    assert preview['status'] == 'ready' and (preview['old'], preview['new']) == (list(OLD), list(NEW))
    outcome = decide_proposal(store, owner, ident, preview, approve=True)

    # One published policy: the old pair leaves shared eligibility and every shared reference moves.
    assert outcome['revision'] == store.current() != before
    policy = store.load(outcome['revision'])
    routes = {(r['backend_id'], r['model_id']) for r in policy['routes']}
    assert NEW in routes and OLD not in routes
    assert policy['roles']['judge']['models']['codex'] == 'gpt-6.1-sol'
    assert policy['roles']['diagnoser']['models']['codex'] == 'gpt-6.1-sol'
    assert policy['source']['previous_policy'] == before
    record = json.loads((store.home / 'proposals' / (ident + '.json')).read_text())
    assert (record['status'], record['applied_revision']) == ('applied', outcome['revision'])

    status = {Path(r['repo']).name: r for r in outcome['projects']}
    assert status['inheritor']['status'] == 'updated'
    assert status['inheritor']['class_choices_preserved'] == {}
    assert status['pinned']['status'] == 'updated'
    assert status['pinned']['class_choices_preserved'] == {
        'judge': {'claude': 'claude-opus-5-5', 'codex': 'gpt-6-sol'},
        'diagnoser': {'claude': 'claude-opus-5-5', 'codex': 'gpt-6-sol'}}
    assert status['pinned']['invalid_routes'] == {
        role: {'codex': 'retired or unsupported model'} for role in ('judge', 'diagnoser')}
    assert status['active']['status'] == 'deferred'
    assert adopted_policy(projects['active'], store)[0] == before
    assert adopted_policy(projects['inheritor'], store)[0] == outcome['revision']

    # Inheriting project: legacy roles and the graph catalog both resolve gpt-6.1-sol on Codex.
    doc = _use(projects['inheritor'], monkeypatch)
    effective = runtime_roles(doc, projects['inheritor'])
    for role in ('judge', 'diagnoser'):
        assert roles.role_config(role, effective).model_for('codex') == 'gpt-6.1-sol'
    dispatch = orchestrator._graph_routing()
    codex = dispatch.catalog.entries['codex']
    assert 'gpt-6.1-sol' in codex.available_models and 'gpt-6-sol' not in codex.available_models
    assert codex.profile('gpt-6.1-sol').context_limits.max_input_tokens == 420_000
    # The graph names no model per role: it routes a strong role to a strong route, and
    # gpt-6.1-sol is the strong Codex route both paths now pick.
    assert _codex_routes(dispatch)['gpt-6.1-sol'] == 'strong'
    for graph_role in ('reviewer', 'diagnoser'):
        assert roles.role_config(graph_role, effective).model_for('codex') == 'gpt-6.1-sol'
    assert dispatch.model_policy_revision == outcome['revision']

    # Project configuration is preserved; its removed role pin adds no graph route.
    doc = _use(projects['pinned'], monkeypatch)
    assert doc['roles'] == PINNED
    effective = runtime_roles(doc, projects['pinned'])
    assert roles.role_config('judge', effective).model_for('codex') == 'gpt-6.1-sol'
    dispatch = orchestrator._graph_routing()
    assert 'gpt-6-sol' not in dispatch.catalog.entries['codex'].available_models
    routes = _codex_routes(dispatch)
    assert routes['gpt-6.1-sol'] == 'strong' and 'gpt-6-sol' not in routes


def test_owner_removal_retires_a_pin_only_route_and_the_pin_is_then_reported(world):
    from maestro.model_detection import decide_proposal, review_proposal
    from maestro.model_lifecycle import apply_policy_preview, preview_retire
    store, projects, owner, _ = world
    ident = _detect(store, owner)
    decide_proposal(store, owner, ident, review_proposal(store, owner, ident)['preview'], approve=True)

    owner.removed['codex'] = ['gpt-6-sol']
    preview = preview_retire(store, owner, 'codex')
    assert preview['retire'] == [list(OLD)] and preview['replacement_required'] == []
    pinned = {Path(p['repo']).name: p for p in preview['projects']}['pinned']
    assert set(pinned['invalid_routes']) == {'judge', 'diagnoser'}
    outcome = apply_policy_preview(preview, store, confirmed=True, bridge=owner)
    policy = store.load(outcome['revision'])
    assert list(OLD) in policy['retired']
    doc = yaml.safe_load((projects['pinned'] / 'project.yaml').read_text())
    assert 'gpt-6-sol' not in _codex(catalog_defaults(policy, doc))['available_models']
    with pytest.raises(PolicyError):
        check_task_pin(doc, policy, 'codex', 'gpt-6-sol')


def test_without_an_owner_removal_signal_gpt_6_sol_is_never_retired(world):
    from maestro.model_lifecycle import preview_retire
    store, projects, owner, _ = world
    owner.removed['codex'] = []
    assert preview_retire(store, owner, 'codex')['status'] == 'nothing_to_retire'
    owner.state_readable = False
    with pytest.raises(PolicyError, match='unavailable'):
        preview_retire(store, owner, 'codex')
    assert digest(store.load(store.current())) == store.current()
