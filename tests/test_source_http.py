"""Bounded read-only transports against an inert local HTTP server."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import importlib.util
import json
import threading

import pytest

from tests.hang_bound import HANG_SECONDS


@pytest.fixture
def server():
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.path, self.headers.get('Authorization')))
            if self.path == '/redirect':
                self.send_response(302)
                self.send_header('Location', '/target')
                self.end_headers()
                return
            status, body = (401, b'private error') if self.path == '/unauthorized' else (200, b'{"ok":true}')
            if self.path == '/malformed':
                body = b'not json'
            if self.path == '/large':
                body = b'x' * 1000
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    service = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    yield 'http://127.0.0.1:' + str(service.server_port), calls
    service.shutdown()
    service.server_close()
    thread.join(timeout=HANG_SECONDS)


def transport():
    assert importlib.util.find_spec('maestro.source_http'), 'bounded GET transport missing'
    return importlib.import_module('maestro.source_http')


def test_transport_uses_only_get_and_accepts_explicit_auth(server):
    url, calls = server
    assert transport().get_json(url + '/usage', bearer_token='synthetic') == {'ok': True}
    assert calls == [('/usage', 'Bearer synthetic')]


@pytest.mark.parametrize('path', ['/redirect', '/unauthorized', '/malformed', '/large'])
def test_transport_rejects_redirects_bad_responses_and_oversized_documents(server, path):
    url, calls = server
    with pytest.raises(Exception):
        transport().get_json(url + path, bearer_token='synthetic', max_bytes=100)
    assert [entry[0] for entry in calls] == [path], 'no redirects or retries'


def test_non_http_url_cannot_read_a_local_file(tmp_path):
    fixture = tmp_path / 'fixture'
    fixture.write_text('{"ok":true}')
    with pytest.raises(ValueError):
        transport().get_json(fixture.as_uri())
