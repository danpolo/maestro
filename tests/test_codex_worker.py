"""Fresh fallback shares one launcher deadline and only follows a dead handle."""
import json
from pathlib import Path

import pytest

from maestro.backends import codex_auth, codex_worker
from maestro.backends.codex_app_server import DeadThread


@pytest.mark.parametrize('elapsed', [4, 11])
def test_missing_thread_fallback_has_only_remaining_time(tmp_path, monkeypatch, elapsed):
    auth = tmp_path / 'auth.json'
    auth.write_text('synthetic-original')
    job = {'workspace': str(tmp_path), 'allow': [], 'deny': [], 'write_roots': [],
           'mode': 'w', 'argv': ['codex', 'app-server'], 'cwd': str(tmp_path),
           'model': 'gpt-6-luna', 'effort': 'low', 'prompt': 'continue',
           'resume_id': 'old', 'fallback_prompt': 'fresh', 'timeout': 10,
           'env': {}, 'expected_config': '{}', 'auth_identity': codex_auth.capture(auth)}
    path = tmp_path / 'job.json'
    path.write_text(json.dumps(job))
    now = [100.0]
    import time
    monkeypatch.setattr(time, 'monotonic', lambda: now[0])
    calls = []
    def turn(**kwargs):
        calls.append(kwargs)
        if kwargs['resume_id']:
            now[0] += elapsed
            raise DeadThread('no rollout found for thread id')
    monkeypatch.setattr(codex_worker, 'run_turn', turn)
    if elapsed > 10:
        with pytest.raises(SystemExit) as failure:
            codex_worker.main(str(path))
        assert failure.value.code == 124
        assert len(calls) == 1
    else:
        codex_worker.main(str(path))
        assert len(calls) == 2
        assert calls[1]['prompt'] == 'fresh' and calls[1]['resume_id'] == ''
        assert calls[1]['timeout'] == 10 - elapsed
    assert calls[0]['timeout'] == 10
