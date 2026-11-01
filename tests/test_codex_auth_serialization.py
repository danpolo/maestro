"""Installed stock authentication at production lifetime boundaries, NO inference.

Only synthetic credentials and loopback endpoints are used. The test-only model
RPC shim substitutes an auth operation for inference, while complete/run_turn
still own actual preparation, locking, config verification, spawn and teardown.
Historical raw concurrent-client fixtures are deliberately unchanged.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from maestro.backends import codex, codex_app_server, codex_auth, codex_runtime, codex_worker
from maestro.backends.base import CompletionSpec
from tests.test_codex_auth_lifetime import lock_available
from tests.test_codex_native_auth import api_key_home, endpoint, seed, save_api_key
from tests.hang_bound import HANG_SECONDS

pytestmark = pytest.mark.skipif(os.environ.get('MAESTRO_TEST_STOCK_CODEX') != '1',
    reason='explicit offline installed authentication qualification only')


def loopback_profile(monkeypatch, root, url):
    """Test-only endpoint/catalog injection; production accepts neither override."""
    prepare = codex_runtime.prepare
    environment = codex_runtime.PreparedRun.environment
    def isolated(**kwargs):
        run = prepare(host_home=root, **kwargs)
        catalog = run.control/'synthetic-catalog.json'
        catalog.write_text(json.dumps({'models': [{'slug': 'gpt-6-luna',
            'display_name': 'Synthetic unused model', 'description': None,
            'supported_reasoning_levels': [], 'shell_type': 'shell_command',
            'visibility': 'list', 'supported_in_api': True, 'priority': 0,
            'base_instructions': 'Synthetic auth qualification; no inference.',
            'supports_reasoning_summaries': False, 'support_verbosity': False,
            'default_verbosity': None, 'apply_patch_tool_type': None,
            'truncation_policy': {'mode': 'bytes', 'limit': 10000},
            'experimental_supported_tools': [], 'input_modalities': ['text'],
            'supports_parallel_tool_calls': False, 'context_window': 10000}]}))
        extra = {'chatgpt_base_url': url.removesuffix('/oauth/token'),
                 'model_catalog_json': str(catalog)}
        run.config.write_text(''.join(f'{k} = {json.dumps(v)}\n' for k,v in extra.items())
                              + run.config.read_text())
        expected = {**json.loads(run.expected_config), **extra}
        return replace(run, expected_config=json.dumps(expected))
    monkeypatch.setattr(codex_runtime, 'prepare', isolated)
    monkeypatch.setattr(codex_runtime.PreparedRun, 'environment', lambda run:
        {**environment(run), 'CODEX_REFRESH_TOKEN_URL_OVERRIDE': url})


@pytest.mark.parametrize('mode', ['managed', 'api-key'])
@pytest.mark.parametrize('boundary', ['complete', 'telemetry', 'generated', 'resume', 'missing'])
def test_stock_auth_operations_serialize_at_production_boundaries(tmp_path, monkeypatch, mode, boundary):
    native, auth = seed(tmp_path) if mode == 'managed' else api_key_home(tmp_path)
    inode = auth.stat().st_ino
    lock = native/'.maestro-auth.lock'
    local = threading.local()
    intervals = {}
    mutex = threading.Lock()
    real_popen = subprocess.Popen
    real_stop = codex_app_server._stop
    real_reap = codex.CodexBackend._reap
    real_rpc = codex_app_server._Client.rpc
    barrier = threading.Barrier(2)

    def popen(argv, **kwargs):
        assert not lock_available(lock)
        assert argv[:4] == [sys.executable, '-I', '-c', codex_auth._LIFETIME_EXEC]
        assert int(argv[4]) in kwargs['pass_fds']
        assert int(argv[5]) in kwargs['pass_fds']
        assert argv[6] == str(os.getpid())
        assert Path(argv[7]).name == 'bwrap' and argv[8:10] == ['--sync-fd', argv[4]]
        proc = real_popen(argv, **kwargs)
        with mutex:
            intervals[proc.pid] = [time.monotonic(), None]
        return proc

    def stopped(callback, *args):
        assert not lock_available(lock), 'released login before native teardown'
        try:
            return callback(*args)
        finally:
            assert not lock_available(lock), 'released login before cleanup returned'
            with mutex:
                intervals[args[-1].pid][1] = time.monotonic()

    monkeypatch.setattr(codex_app_server, '_stop', lambda proc: stopped(real_stop,proc))
    monkeypatch.setattr(codex.CodexBackend, '_reap', lambda self,proc: stopped(real_reap,self,proc))
    real_turn = codex_worker.run_turn
    monkeypatch.setattr(codex_worker,'run_turn',lambda **kwargs: real_turn(popen=popen,**kwargs))

    def account_operation(client):
        if mode == 'api-key':
            for number in range(4):
                key = f'sk-synthetic-{local.label}-{number}-' + local.label * (64 if local.label=='a' else 256)
                save_api_key(client,key)
                assert json.loads(auth.read_text())['OPENAI_API_KEY'] == key
        response = real_rpc(client,'account/read',{'refreshToken':mode=='managed'})
        assert response['account']['type'] == ('chatgpt' if mode=='managed' else 'apiKey'),response

    def no_inference_rpc(client,method,params):
        if method in ('thread/start','thread/resume'):
            if method == 'thread/resume' and boundary == 'missing':
                return real_rpc(client,method,params)
            # No native thread or inference request is sent.
            return {'thread':{'id':params.get('threadId','synthetic-auth-thread')},'model':params['model'],
                    'approvalPolicy':'untrusted'}
        if method == 'turn/start':
            account_operation(client)
            local.calls += 1
            client.turn_id = 'synthetic-auth-turn'
            client.final = 'auth-qualified'
            client.completed = {'id':client.turn_id,'status':'completed'}
            return {'turn':{'id':client.turn_id}}
        assert method not in ('thread/resume','turn/resume'),method
        return real_rpc(client,method,params)

    monkeypatch.setattr(codex_app_server._Client,'rpc',no_inference_rpc)

    def telemetry_requests():
        requests = original_requests()[:2]
        if mode == 'api-key':
            requests += [{'id':8,'method':'account/login/start','params':{
                'type':'apiKey','apiKey':'sk-synthetic-telemetry-'+local.label*256}}]
        return requests + [{'id':9,'method':'account/read','params':{'refreshToken':mode=='managed'}}]
    original_requests = codex.app_server_requests
    if boundary == 'telemetry':
        monkeypatch.setattr(codex,'app_server_requests',telemetry_requests)

    def launch(label):
        local.label = label
        local.calls = 0
        tree = tmp_path/('tree-'+label)
        tree.mkdir()
        backend = codex.CodexBackend(binary='codex',codex_home=native,popen=popen)
        if boundary == 'telemetry':
            def account_reply(raw):
                response = json.loads(raw)
                if response.get('id') == 9:
                    account = response.get('result',{}).get('account',{})
                    assert account.get('type') == ('chatgpt' if mode=='managed' else 'apiKey'),response
                    return {}  # The synthetic auth RPC is not a rate-limit reading.
                return None
            backend._rate_limits_of_reply = account_reply
        barrier.wait(timeout=HANG_SECONDS)
        if boundary == 'complete':
            result = backend.complete(CompletionSpec(model='gpt-6-luna',prompt='synthetic-auth',
                cwd=tree,timeout=HANG_SECONDS,log_file=tmp_path/('reports-'+label)/'impl.log'))
            assert result.text == 'auth-qualified' and result.returncode == 0,result
        elif boundary == 'telemetry':
            assert backend.usage_from_app_server(timeout_sec=HANG_SECONDS) is not None,backend.last_telemetry_error
        else:
            reports = tmp_path/('reports-'+label)
            reports.mkdir()
            run = codex_runtime.prepare(cwd=tree,workspace=reports,native_home=native,
                model='gpt-6-luna',effort=None,write_roots=())
            try:
                run.launcher(binary='codex',prompt='synthetic-auth',timeout=HANG_SECONDS,
                    resume_id='019ff499-deaf-77d1-acd4-e2135c856fa8' if boundary in ('resume','missing') else '',
                    fallback_prompt='synthetic-fallback' if boundary=='missing' else None)
                codex_worker.main(str(run.control/'job.json'))
                assert local.calls == 1
                if boundary == 'missing':
                    assert 'maestro.resume_missing' in (reports/'impl.log').read_text()
            finally:
                run.cleanup()

    with endpoint(strict=True) as (url,requests):
        loopback_profile(monkeypatch,tmp_path,url)
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(launch,label) for label in ('a','b')]
            for future in futures:
                future.result(timeout=3 * HANG_SECONDS)
        assert len(intervals) == (4 if boundary=='missing' else 2)
        ordered = sorted(intervals.values())
        assert all(end is not None for start,end in ordered)
        assert ordered[0][1] <= ordered[1][0],ordered
        assert auth.stat().st_ino == inode
        record = json.loads(auth.read_text())
        if mode == 'managed':
            assert requests and all(request['accepted'] for request in requests),requests
            assert record['tokens']['refresh_token'] == f'synthetic-refresh-{len(requests)}'
        else:
            assert not requests
            assert record['OPENAI_API_KEY'].startswith('sk-synthetic-')
        assert lock_available(lock)
