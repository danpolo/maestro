"""Durable, idempotent repair publication after code has already integrated."""
from __future__ import annotations

import fcntl
import hashlib
import json
from pathlib import Path


def queued_fix(repo: Path, store, run_id: str) -> str:
    key = hashlib.sha256(run_id.encode()).hexdigest()[:16]
    path = Path(repo) / '.orchestrator/post-merge' / f'{key}.json'
    if not path.exists():
        return ''
    data = json.loads(path.read_text())
    task_id = data['proposal']['task']['id']
    return task_id if data['status'] == 'queued' and store.task(task_id) is not None else ''


def publish_fix(controller, result) -> str:
    """Persist intent before ROADMAP CAS; reap may safely repeat after any crash."""
    from maestro import intake, taskgraph

    repo = Path(controller.repo)
    directory = repo / '.orchestrator/post-merge'
    directory.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(controller.run_id.encode()).hexdigest()[:16]
    path = directory / f'{key}.json'
    with (directory / f'{key}.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            data = json.loads(path.read_text())
        else:
            graph = taskgraph.build_graph(taskgraph.load_document(taskgraph.roadmap_path(repo)),
                                          registry_ids=taskgraph.read_registry_ids(repo))
            parent = graph.spec(controller.revision.task_id)
            task = parent.to_yaml_dict()
            task.update(id=f'{parent.task_id}-fix-{key}', title=f'Repair post-merge checks: {parent.title}',
                        status='pending', hold=False, spec_revision=1,
                        repairs_task=parent.task_id, repairs_run=controller.run_id,
                        repair_subject=controller.merged_snapshot(),
                        short_desc=(f'Repair the failed deployed checks of {parent.task_id} on '
                                    f'{controller.merged_snapshot()}. Preserve the original scope. '
                                    f'Observed failure: {result.detail}'))
            # Keep the parent's prerequisites: depending on a failed parent would
            # prevent the repair from ever dispatching. The relation above is durable.
            data = {'status': 'approved', 'proposal': {'task': task},
                    'source_run': controller.run_id, 'source_node': result.node_id}
            intake._write(path, data)
        task_id = data['proposal']['task']['id']
        if data['status'] == 'queued':
            if controller.store is not None:
                present = controller.store.task(task_id) is not None
            else:
                graph = taskgraph.build_graph(taskgraph.load_document(taskgraph.roadmap_path(repo)),
                                              registry_ids=taskgraph.read_registry_ids(repo))
                present = task_id in graph or task_id in taskgraph.read_registry_ids(repo)
            if not present:
                raise RuntimeError('published post-merge repair is missing from task authority')
            return task_id
        if controller.store is None:
            intake._publish(repo, None, data, path, legacy=True)
        else:
            intake._publish(repo, controller.store, data, path)
        data['status'] = 'queued'
        intake._write(path, data)
        return task_id
