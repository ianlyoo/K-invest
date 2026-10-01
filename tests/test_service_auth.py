"""Direct-loopback service auth using synthetic credentials and in-process ASGI."""
from __future__ import annotations

import ast
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.routes import create_auth_routes
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.testclient import TestClient
from test_auth_transport import URL, authorize
from test_oauth_durable import provider

from service_auth import (
    SERVICE_READ_TOOLS,
    ServiceTokenConfigurationError,
    ServiceTokenVerifier,
    load_service_token,
)

TOKEN = 'synthetic-service-token-with-at-least-32-characters'
HEADERS = {'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json',
           'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-06-18'}


def application(existing=None):
    import server
    fallback = existing or AsyncMock()
    if existing is None:
        fallback.verify_token.return_value = None
    app = server.ObservedFastMCP('synthetic', stateless_http=True, json_response=True,
        token_verifier=ServiceTokenVerifier(hashlib.sha256(TOKEN.encode()).digest(), fallback),
        auth=AuthSettings(issuer_url=URL, resource_server_url=URL),
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    called = []
    def read() -> dict[str, Any]:
        called.append('read')
        return {'ok': True, 'data': {'synthetic': True}}
    for name in SERVICE_READ_TOOLS:
        app.add_tool(read, name=name)
    def forbidden():
        pytest.fail('A service principal reached a non-allowlisted tool')
    for name in ('place_order', 'cancel_order', 'execute_trade', 'health_check', 'get_quote', 'get_service_health', 'get_exchange_rate'):
        app.add_tool(forbidden, name=name)
    return app, fallback, called


def rpc(client, method, params=None, headers=None):
    return client.post('/mcp', headers=headers or HEADERS,
        json={'jsonrpc': '2.0', 'id': 1, 'method': method, **({'params': params} if params is not None else {})})


def initialize(client, headers=None):
    return rpc(client, 'initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
        'clientInfo': {'name': 'synthetic', 'version': '1'}}, headers)


def test_direct_loopback_lists_only_exact_allowlist():
    app, fallback, _ = application()
    with TestClient(app.streamable_http_app(), base_url=URL, client=('127.0.0.1', 12345)) as client:
        assert initialize(client).status_code == 200
        result = rpc(client, 'tools/list').json()['result']
        assert {tool['name'] for tool in result['tools']} == SERVICE_READ_TOOLS
    fallback.verify_token.assert_not_awaited()


@pytest.mark.parametrize('peer', ['::1', '198.51.100.1', '127.0.0.2', '::ffff:127.0.0.1', 'testclient'])
def test_other_peers_service_token_rejected_without_oauth_fallback(peer):
    app, fallback, called = application()
    with TestClient(app.streamable_http_app(), base_url=URL, client=(peer, 12345)) as client:
        assert rpc(client, 'tools/list').status_code == 401
    assert called == []
    fallback.verify_token.assert_not_awaited()


def test_missing_tcp_peer_is_rejected_without_fallback():
    app, fallback, _ = application()
    with TestClient(app.streamable_http_app(), base_url=URL, client=None) as client:
        assert rpc(client, 'tools/list').status_code == 401
    fallback.verify_token.assert_not_awaited()


def test_service_denial_auth_events_never_include_token(caplog):
    import logging
    app, _, _ = application()
    with TestClient(app.streamable_http_app(), base_url=URL, client=('198.51.100.1', 12345)) as client:
        with caplog.at_level(logging.INFO, logger='k-invest.auth'):
            assert rpc(client, 'tools/list').status_code == 401
    events = [record.message for record in caplog.records if record.name == 'k-invest.auth']
    assert events and TOKEN not in str(events)


def test_optional_service_file_unset_leaves_legacy_configuration_unchanged(monkeypatch):
    monkeypatch.delenv('KINVEST_SERVICE_TOKEN_FILE', raising=False)
    assert load_service_token('synthetic-existing-static') is None


@pytest.mark.parametrize('header,value', [('X-Forwarded-For', '127.0.0.1'), ('X-Real-IP', '::1'),
    ('Forwarded', 'for=127.0.0.1'), ('X-Forwarded-For', ''), ('fOrWaRdEd', '')])
@pytest.mark.parametrize('peer', ['127.0.0.1', '198.51.100.1'])
def test_proxy_headers_cannot_spoof_loopback_and_nginx_peer_is_rejected(header, value, peer):
    app, fallback, _ = application()
    with TestClient(app.streamable_http_app(), base_url=URL, client=(peer, 12345)) as client:
        response = rpc(client, 'tools/list', headers={**HEADERS, header: value})
        assert response.status_code == 401
    fallback.verify_token.assert_not_awaited()


@pytest.mark.parametrize('name', sorted(SERVICE_READ_TOOLS))
def test_each_allowlisted_call_dispatches_only_synthetic_read(name):
    app, _, called = application()
    with TestClient(app.streamable_http_app(), base_url=URL, client=('127.0.0.1', 12345)) as client:
        assert initialize(client).status_code == 200
        result = rpc(client, 'tools/call', {'name': name, 'arguments': {}}).json()['result']
        assert not result['isError'] and result['structuredContent']['ok']
    assert called == ['read']


@pytest.mark.parametrize('name', ['place_order', 'cancel_order', 'execute_trade', 'health_check',
    'get_quote', 'get_service_health', 'get_exchange_rate', 'unregistered_tool'])
def test_non_allowlisted_call_forbidden_even_when_cached_by_other_principal(name):
    existing = AsyncMock()
    existing.verify_token.return_value = AccessToken(token='synthetic-other', client_id='invest-desk-service', scopes=[])
    app, _, called = application(existing)
    with TestClient(app.streamable_http_app(), base_url=URL, client=('127.0.0.1', 12345)) as client:
        other = {**HEADERS, 'Authorization': 'Bearer synthetic-other'}
        assert initialize(client, other).status_code == 200
        assert len(rpc(client, 'tools/list', headers=other).json()['result']['tools']) > len(SERVICE_READ_TOOLS)
        assert initialize(client).status_code == 200
        result = rpc(client, 'tools/call', {'name': name, 'arguments': {}}).json()['result']
        assert result['isError'] and result['content'][0]['text'] == 'Forbidden'
        assert {t['name'] for t in rpc(client, 'tools/list').json()['result']['tools']} == SERVICE_READ_TOOLS
    assert called == []


def test_allowlist_matches_invest_desk_client_source_exactly():
    assert len(SERVICE_READ_TOOLS) == 8
    source = Path(__file__).resolve().parents[2] / 'repo/src/invest_desk/clients.py'
    if not source.is_file():
        pytest.skip('Independent checkout of invest-desk is not present')
    assignment = next(node for node in ast.parse(source.read_text()).body if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == 'KINVEST_TOOLS' for target in node.targets))
    assert isinstance(assignment.value, ast.Call) and assignment.value.func.id == 'frozenset'
    assert SERVICE_READ_TOOLS == frozenset(ast.literal_eval(assignment.value.args[0]))


def credential(tmp_path, monkeypatch, content=TOKEN, mode=0o600):
    path = tmp_path / 'synthetic-credential'
    path.write_text(content)
    path.chmod(mode)
    monkeypatch.setenv('KINVEST_SERVICE_TOKEN_FILE', str(path))
    return path


@pytest.mark.parametrize('mode', [0o600, 0o400])
def test_private_file_or_systemd_credential_load_retains_only_digest(tmp_path, monkeypatch, caplog, mode):
    import logging
    credential(tmp_path, monkeypatch, TOKEN + '\n', mode)
    with caplog.at_level(logging.INFO, logger='k-invest'):
        assert load_service_token('synthetic-existing-static') == hashlib.sha256(TOKEN.encode()).digest()
    assert 'service_token=loaded collision_guard=passed' in caplog.text
    assert TOKEN not in caplog.text and str(tmp_path) not in caplog.text


def test_even_a_digest_collision_refuses_configuration(tmp_path, monkeypatch):
    from types import SimpleNamespace
    credential(tmp_path, monkeypatch)
    monkeypatch.setattr(hashlib, 'sha256', lambda value: SimpleNamespace(digest=lambda: b'x' * 32))
    with pytest.raises(ServiceTokenConfigurationError, match='ServiceTokenCollision'):
        load_service_token('synthetic-different-existing-static')


@pytest.mark.parametrize('failure', ['public', 'missing', 'symlink', 'empty', 'oversize', 'relative', 'inline', 'collision'])
def test_invalid_service_configuration_is_fail_closed_and_sanitized(tmp_path, monkeypatch, failure):
    path = credential(tmp_path, monkeypatch)
    if failure == 'public':
        path.chmod(0o644)
    elif failure == 'missing':
        path.unlink()
    elif failure == 'symlink':
        link = tmp_path / 'synthetic-link'
        link.symlink_to(path)
        monkeypatch.setenv('KINVEST_SERVICE_TOKEN_FILE', str(link))
    elif failure == 'empty':
        path.write_text('')
    elif failure == 'oversize':
        path.write_text('a' * 4097)
    elif failure == 'relative':
        monkeypatch.setenv('KINVEST_SERVICE_TOKEN_FILE', 'synthetic-relative')
    elif failure == 'inline':
        monkeypatch.setenv('KINVEST_SERVICE_TOKEN', TOKEN)
    with pytest.raises(ServiceTokenConfigurationError) as caught:
        load_service_token(TOKEN if failure == 'collision' else 'synthetic-existing-static')
    assert TOKEN not in str(caught.value) and str(path) not in str(caught.value)


def test_collision_refuses_server_import_before_oauth_or_listener_start(tmp_path, monkeypatch):
    path = credential(tmp_path, monkeypatch)
    root = Path(__file__).resolve().parents[1]
    env = {'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': str(root), 'MCP_AUTH_TOKEN': TOKEN,
           'KINVEST_SERVICE_TOKEN_FILE': str(path), 'KINVEST_CACHE_DIR': str(tmp_path)}
    script = '''import socket
def blocked(*args, **kwargs):
    raise AssertionError("Outgoing network is forbidden")
socket.socket.connect = blocked
socket.socket.connect_ex = blocked
import kinvest_common
kinvest_common.apply_env_file = lambda path: None
from service_auth import ServiceTokenConfigurationError
try:
    import server
except ServiceTokenConfigurationError as exc:
    print(str(exc))
else:
    raise SystemExit("collision was accepted")
'''
    result = subprocess.run([sys.executable, '-c', script], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0 and result.stdout.strip() == 'ServiceTokenCollision'


@pytest.mark.asyncio
async def test_uvicorn_keeps_original_socket_peer(monkeypatch):
    import uvicorn
    app, _, _ = application()
    configs = []
    async def serve(instance):
        configs.append(instance.config)
    monkeypatch.setattr(uvicorn.Server, 'serve', serve)
    await app.run_streamable_http_async()
    assert len(configs) == 1 and configs[0].proxy_headers is False
    assert configs[0].host == '127.0.0.1' and configs[0].port == 8000  # Fixture's default.


@pytest.mark.parametrize('peer', ['127.0.0.1', '198.51.100.1'])
def test_existing_oauth_and_static_bearer_still_work_through_proxy(tmp_path, monkeypatch, peer):
    import server
    from oauth_provider import DualTokenVerifier
    p = provider(tmp_path, resource_url=URL)
    monkeypatch.setattr(server, '_oauth_provider', p)
    app, _, _ = application(DualTokenVerifier('synthetic-existing-static', p))
    for route in create_auth_routes(p, AnyHttpUrl(URL), client_registration_options=ClientRegistrationOptions(enabled=False)):
        if route.path != '/authorize':
            app.custom_route(route.path, methods=list(route.methods or []))(route.endpoint)
    app.custom_route('/authorize', methods=['GET', 'POST'])(server._oauth_authorize)
    # nginx terminates HTTPS; the real ASGI request still has an HTTP scheme.
    with TestClient(app.streamable_http_app(), base_url='http://127.0.0.1:8100', client=(peer, 12345)) as client:
        tokens = authorize(client)
        headers = {**HEADERS, 'Authorization': 'Bearer ' + tokens['access_token'],
                   'X-Forwarded-For': '198.51.100.1', 'X-Forwarded-Proto': 'https'}
        assert initialize(client, headers).status_code == 200
        assert len(rpc(client, 'tools/list', headers=headers).json()['result']['tools']) > len(SERVICE_READ_TOOLS)
        static = {**headers, 'Authorization': 'Bearer synthetic-existing-static'}
        assert rpc(client, 'tools/list', headers=static).status_code == 200
        response = client.post('/token', data={'grant_type': 'refresh_token', 'client_id': 'fixture-client',
            'client_secret': 'fixture-secret', 'refresh_token': tokens['refresh_token'], 'resource': URL})
        assert response.status_code == 200
        new_headers = {**headers, 'Authorization': 'Bearer ' + response.json()['access_token']}
        assert rpc(client, 'tools/list', headers=new_headers).status_code == 200
