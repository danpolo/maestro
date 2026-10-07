"""Read-only, unpaid: what DuetFlow's graph nodes bind to under this release's own routing.

A disposable copy of DuetFlow's project.yaml and no shared policy (none is published live);
the host provider tables are only read. Nothing is launched."""
import json, os, shutil, sys, tempfile
from pathlib import Path
SRC = Path(sys.argv[1]).resolve()  # the release checkout whose code is probed
sys.path.insert(0, str(SRC))
tmp = Path(tempfile.mkdtemp(prefix='duetflow-binding-probe-'))
os.environ['MAESTRO_MODELS_HOME'] = str(tmp / 'models')
os.environ['MAESTRO_REPO'] = str(tmp)
os.environ['MAESTRO_BOOTSTRAPPED'] = '1'
os.environ.pop('MAESTRO_HOME', None)
shutil.copyfile('/home/dan/projects/duetflow/project.yaml', tmp / 'project.yaml')
(tmp / '.orchestrator').mkdir()
from maestro import orchestrator, config
from maestro.backends import router
from maestro.workflows import routing
from maestro.workflows.models import NodeSpec
assert Path(orchestrator.__file__).resolve().is_relative_to(SRC)
orchestrator.REPO = tmp
config.PROJECT_YAML = tmp / 'project.yaml'
dispatch = orchestrator._graph_routing()
out = {'source': str(SRC), 'codex_available': list(dispatch.catalog.entries['codex'].available_models),
       'preferences': {k: [list(p) for p in v] for k, v in dispatch.preferences.items()}, 'bindings': {}}
for role in ('implementer', 'reviewer', 'proof_review', 'diagnoser'):
    for risk in ('low', 'high'):
        node = NodeSpec(node_id=role, kind='agent', handler_ref=f'agent.{role}', agent_definition_ref=role)
        b = routing.resolve(dispatch, node, router.RoutingContext(estimated_input_tokens=40_000, risk=risk))
        out['bindings'][f'{role}/{risk}'] = (dict(bound=True, backend=b.backend_id, model=b.model_id,
                                                  reason=b.selection_reason) if b.is_bound else
                                             dict(bound=False, detail=repr(b)[:600]))
print(json.dumps(out, indent=2))
