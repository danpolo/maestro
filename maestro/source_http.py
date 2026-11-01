"""Bounded JSON GETs for local sources, without redirects, retries or proxy state."""
import json
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get_json(url: str, *, bearer_token: str | None = None, timeout: float = 2.0,
             max_bytes: int = 1024 * 1024):
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('source URL must be HTTP(S) without userinfo')
    headers = {'Accept': 'application/json', 'User-Agent': 'maestro-usage-source/1'}
    if bearer_token:
        headers['Authorization'] = 'Bearer ' + bearer_token
    request = Request(url, headers=headers, method='GET')
    with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError('source returned a non-200 response')
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError('source document exceeds size bound')
    return json.loads(body.decode('utf-8'))
