"""Disposable installed managed-refresh qualification; synthetic credentials only."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import os

import pytest
import subprocess
import threading
import time

from maestro.backends import codex_app_server, codex_runtime, control_files
from tests.hang_bound import HANG_SECONDS

pytestmark = pytest.mark.skipif(os.environ.get('MAESTRO_TEST_STOCK_CODEX') != '1',
    reason='explicit offline installed authentication qualification only')


def jwt(expiry):
    payload = {'email': 'synthetic@example.invalid', 'exp': expiry,
               'https://api.openai.com/auth': {'chatgpt_account_id': 'synthetic-account',
                   'chatgpt_user_id': 'synthetic-user', 'chatgpt_plan_type': 'pro'}}
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b'=').decode()
    return encode({'alg': 'none'}) + '.' + encode(payload) + '.synthetic'


@contextmanager
def endpoint(strict=False):
    requests = []
    lock = threading.Lock()
    current = ['synthetic-refresh-initial']
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            account = {'id': 'synthetic-account',
                'plan_type': 'pro', 'account_routing_override': 'NO_CONSTRAINT',
                'workspace_backend_origin': 'https://chatgpt.com'}
            body = json.dumps({'models': [], 'accounts': [account]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert data['grant_type'] == 'refresh_token'
            assert data['refresh_token'].startswith('synthetic-refresh-')
            with lock:
                requests.append(data)
                number = len(requests)
                accepted = not strict or data['refresh_token'] == current[0]
                data['accepted'] = accepted
                if accepted:
                    current[0] = f'synthetic-refresh-{number}'
            if not accepted:
                body = b'{"error":"invalid_grant","error_description":"synthetic refresh token already consumed"}'
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            result = {'id_token': jwt(int(time.time()) + 7200),
                      'access_token': jwt(int(time.time()) + 7200),
                      'refresh_token': f'synthetic-refresh-{number}'}
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/oauth/token', requests
    finally:
        server.shutdown()
        thread.join(timeout=HANG_SECONDS)
        server.server_close()


def seed(tmp_path):
    native = tmp_path / 'native'
    native.mkdir()
    auth = native / 'auth.json'
    auth.write_text(json.dumps({'auth_mode': 'chatgpt', 'OPENAI_API_KEY': None,
        'tokens': {'id_token': jwt(1), 'access_token': jwt(1),
                   'refresh_token': 'synthetic-refresh-initial', 'account_id': 'synthetic-account'},
        'last_refresh': '2000-01-01T00:00:00Z'}))
    return native, auth


@contextmanager
def client(tmp_path, native, label, url):
    tree, reports = tmp_path / ('tree-' + label), tmp_path / ('reports-' + label)
    tree.mkdir(); reports.mkdir()
    run = codex_runtime.prepare(cwd=tree, workspace=reports, native_home=native,
        host_home=tmp_path, model='gpt-6-luna', effort='low', write_roots=(tree, reports))
    process = None
    try:
        env = {**run.environment(), 'CODEX_REFRESH_TOKEN_URL_OVERRIDE': url}
        catalog = run.control / 'synthetic-empty-model-catalog.json'
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
        with (reports / 'native-stderr.log').open('wb') as diagnostic:
            # Test-only backend stub: automatic model discovery must also stay
            # on loopback. Production never accepts these endpoint overrides.
            command = ['codex', '-c', 'chatgpt_base_url=' + json.dumps(url.removesuffix('/oauth/token')),
                       '-c', 'model_catalog_json=' + json.dumps(str(catalog)),
                       'app-server']
            with control_files.pinned_mounts(run.wrap(command)) as (argv, fds):
                process = subprocess.Popen(argv, cwd=tree, env=env, pass_fds=fds,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=diagnostic, start_new_session=True)
            rpc = codex_app_server._Client(process, deadline=time.monotonic() + HANG_SECONDS,
                policy=run.policy, emit=lambda _: None, health=run.verify_auth)
            rpc.rpc('initialize', {'clientInfo': {'name': 'maestro-synthetic-auth', 'version': '1'},
                'capabilities': {'experimentalApi': True}})
            rpc.send({'method': 'initialized'})
            yield rpc, run
    finally:
        if process is not None:
            codex_app_server._stop(process)
        run.cleanup()


def test_installed_synthetic_managed_refresh(tmp_path):
    native, auth = seed(tmp_path)
    inode = auth.stat().st_ino
    with endpoint() as (url, requests):
        with client(tmp_path, native, 'refresh', url) as (rpc, run):
            response = rpc.rpc('account/read', {'refreshToken': True})
            assert response['account']['type'] == 'chatgpt', response
            assert requests, 'installed native process did not call synthetic refresh endpoint'
            assert auth.stat().st_ino == inode
            stored = json.loads(auth.read_text())
            assert stored['tokens']['refresh_token'].startswith('synthetic-refresh-')
            assert stored['tokens']['refresh_token'] != 'synthetic-refresh-initial'
            assert stored['last_refresh'] != '2000-01-01T00:00:00Z'
            run.verify_auth()


def test_two_installed_synthetic_managed_refreshes_share_valid_record(tmp_path):
    native, auth = seed(tmp_path)
    inode = auth.stat().st_ino
    with endpoint() as (url, requests), ExitStack() as stack, ThreadPoolExecutor(max_workers=2) as pool:
        first, first_run = stack.enter_context(client(tmp_path, native, 'first', url))
        second, second_run = stack.enter_context(client(tmp_path, native, 'second', url))
        barrier = threading.Barrier(2)
        def refresh(rpc):
            barrier.wait(timeout=HANG_SECONDS)
            return rpc.rpc('account/read', {'refreshToken': True})
        responses = [pool.submit(refresh, rpc) for rpc in (first, second)]
        assert all(f.result(timeout=2 * HANG_SECONDS)['account']['type'] == 'chatgpt' for f in responses)
        assert requests, 'no actual synthetic managed refresh occurred'
        assert auth.stat().st_ino == inode
        stored = json.loads(auth.read_text())
        assert stored['tokens']['refresh_token'] in {
            f'synthetic-refresh-{i}' for i in range(1, len(requests) + 1)}
        first_run.verify_auth(); second_run.verify_auth()


def test_replaced_original_auth_refuses_installed_managed_refresh(tmp_path):
    native, auth = seed(tmp_path)
    with endpoint(strict=True) as (url, requests):
        with client(tmp_path, native, 'replaced', url) as (rpc, run):
            replacement = native / 'replacement.json'
            replacement.write_text(auth.read_text())
            replacement.replace(auth)
            count = len(requests)
            with pytest.raises(ValueError, match='auth identity changed'):
                rpc.rpc('account/read', {'refreshToken': True})
            assert len(requests) == count


def test_strict_refresh_failure_preserves_record_and_fresh_client_recovers(tmp_path):
    native, auth = seed(tmp_path)
    inode = auth.stat().st_ino
    with endpoint(strict=True) as (url, requests):
        with ExitStack() as stack, ThreadPoolExecutor(max_workers=2) as pool:
            first, _ = stack.enter_context(client(tmp_path, native, 'first', url))
            second, _ = stack.enter_context(client(tmp_path, native, 'second', url))
            barrier = threading.Barrier(2)
            def refresh(rpc):
                barrier.wait(timeout=HANG_SECONDS)
                return rpc.rpc('account/read', {'refreshToken': True})
            results = [pool.submit(refresh, rpc) for rpc in (first, second)]
            responses = [f.result(timeout=2 * HANG_SECONDS) for f in results]
            # Stock can reload the other client's completed rotation or lose a
            # concurrent single-use race. It must retain the last accepted token.
            assert any(r.get('account') and r['account']['type'] == 'chatgpt'
                       for r in responses), responses
            assert all(r.get('account') or r['requiresOpenaiAuth'] for r in responses)
            accepted = [i for i, request in enumerate(requests, 1) if request['accepted']]
            record = json.loads(auth.read_text())
            assert auth.stat().st_ino == inode
            assert record['tokens']['refresh_token'] == f'synthetic-refresh-{accepted[-1]}'
        with client(tmp_path, native, 'recovery', url) as (rpc, run):
            response = rpc.rpc('account/read', {'refreshToken': True})
            assert response['account']['type'] == 'chatgpt', response
            run.verify_auth()
            accepted = [i for i, request in enumerate(requests, 1) if request['accepted']]
            assert json.loads(auth.read_text())['tokens']['refresh_token'] == f'synthetic-refresh-{accepted[-1]}'


def api_key_home(tmp_path):
    native = tmp_path / 'native'
    native.mkdir()
    auth = native / 'auth.json'
    auth.write_text(json.dumps({'OPENAI_API_KEY': 'sk-synthetic-initial'}))
    return native, auth


def save_api_key(rpc, key):
    assert rpc.rpc('account/login/start', {'type': 'apiKey', 'apiKey': key}) == {'type': 'apiKey'}


def test_native_api_key_save_renews_original_inode(tmp_path):
    native, auth = api_key_home(tmp_path)
    inode = auth.stat().st_ino
    with endpoint() as (url, requests), client(tmp_path, native, 'api-save', url) as (rpc, run):
        save_api_key(rpc, 'sk-synthetic-renewed')
        assert auth.stat().st_ino == inode
        assert json.loads(auth.read_text())['OPENAI_API_KEY'] == 'sk-synthetic-renewed'
        assert rpc.rpc('account/read', {'refreshToken': False})['account']['type'] == 'apiKey'
        run.verify_auth()
        assert not requests


def test_api_key_save_refuses_replaced_login_before_native_request(tmp_path):
    native, auth = api_key_home(tmp_path)
    with endpoint() as (url, _), client(tmp_path, native, 'api-replaced', url) as (rpc, run):
        retained = native / 'old.json'
        auth.rename(retained)
        auth.write_text(json.dumps({'OPENAI_API_KEY': 'sk-synthetic-replacement'}))
        with pytest.raises(ValueError, match='auth identity changed'):
            save_api_key(rpc, 'sk-synthetic-must-not-be-saved')
        assert json.loads(retained.read_text())['OPENAI_API_KEY'] == 'sk-synthetic-initial'
        assert json.loads(auth.read_text())['OPENAI_API_KEY'] == 'sk-synthetic-replacement'


def test_concurrent_native_api_key_saves_retain_completed_valid_record(tmp_path):
    native, auth = api_key_home(tmp_path)
    inode = auth.stat().st_ino
    with endpoint() as (url, _), ExitStack() as stack, ThreadPoolExecutor(max_workers=2) as pool:
        first, _ = stack.enter_context(client(tmp_path, native, 'api-first', url))
        second, _ = stack.enter_context(client(tmp_path, native, 'api-second', url))
        for number in range(4):
            barrier = threading.Barrier(2)
            keys = [f'sk-synthetic-{number}-' + letter * length
                    for letter, length in [('a', 64), ('b', 256)]]
            def save(rpc, key):
                barrier.wait(timeout=HANG_SECONDS)
                save_api_key(rpc, key)
            results = [pool.submit(save, rpc, key) for rpc, key in zip((first, second), keys)]
            for result in results:
                result.result(timeout=2 * HANG_SECONDS)
            assert auth.stat().st_ino == inode
            assert json.loads(auth.read_text())['OPENAI_API_KEY'] in keys

