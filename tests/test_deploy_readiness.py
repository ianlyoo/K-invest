"""Authenticated readiness over in-process ASGI only; no live credentials/sockets."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from deploy import readiness
from oauth_provider import DualTokenVerifier, PersonalOAuthProvider

REV = 'a'*40


@pytest.mark.asyncio
@pytest.mark.parametrize('store_available', [True, False])
async def test_authenticated_passive_health_through_real_mcp_transport(tmp_path, monkeypatch, store_available):
    import server
    def forbidden(*args, **kwargs):
        pytest.fail('Readiness attempted broker/client activity')
    for name in ('_get_toss', '_get_kis', '_get_toss_registry', '_get_kis_registry', '_get_collector'):
        monkeypatch.setattr(server, name, forbidden)
    provider = PersonalOAuthProvider('fixture-client', 'fixture-secret', tmp_path/'oauth.sqlite3')
    monkeypatch.setattr(server, '_oauth_provider', provider)
    mcp = FastMCP('readiness-fixture', stateless_http=True,
        token_verifier=DualTokenVerifier('fixture-static', provider),
        auth=AuthSettings(issuer_url='https://example.invalid/', resource_server_url='https://example.invalid/'),
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    @mcp.tool()
    def get_service_health(mode: str = 'passive') -> dict[str, Any]:
        assert mode == 'passive'
        return server.get_service_health(mode)
    app = mcp.streamable_http_app()
    if not store_available:
        provider.store.path.rename(tmp_path/'offline.sqlite3')
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    headers={'Authorization': 'Bearer fixture-static'}) as client:
            assert await asyncio.wait_for(readiness.authenticated_store_ok('fixture-static', client), 10) is store_available


@pytest.mark.parametrize('auth', [{'store_status': 'disabled', 'restart_durable': False},
    {'store_status': 'unavailable', 'restart_durable': True}, {'store_status': 'ok', 'restart_durable': False}])
def test_unusable_or_nondurable_auth_store_fails(auth):
    result = SimpleNamespace(isError=False, structuredContent={'ok': True, 'data': {'auth': auth}})
    assert not readiness.store_ok(result)


@pytest.mark.parametrize('envelope', [None, {}, {'ok': False}, {'ok': True, 'data': []}])
def test_missing_or_malformed_health_fails_closed(envelope):
    assert not readiness.store_ok(SimpleNamespace(isError=False, structuredContent=envelope))


def fake_client(monkeypatch):
    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get(self, url):
            assert url == readiness.LOCAL+'/healthz'
            return SimpleNamespace(status_code=200, text='ok')
    monkeypatch.setattr(readiness.httpx, 'AsyncClient', Client)


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['ok', 'auth_failure', 'pid_changed', 'restart_count_changed', 'inactive'])
async def test_readiness_requires_active_same_process_and_authenticated_store_ok(monkeypatch, case):
    fake_client(monkeypatch)
    before = {'ActiveState': 'active', 'SubState': 'running', 'MainPID': '123',
              'NRestarts': '0', 'WorkingDirectory': '/home/ubuntu/toss-mcp-releases/'+REV}
    after = dict(before)
    if case == 'pid_changed':
        after['MainPID'] = '124'
    if case == 'restart_count_changed':
        after['NRestarts'] = '1'
    if case == 'inactive':
        before['ActiveState'], before['SubState'] = 'activating', 'auto-restart'
    snapshots = iter((before, after))
    monkeypatch.setattr(readiness, 'service_properties', lambda: next(snapshots))
    monkeypatch.setattr(readiness, 'inherited_bearer', lambda pid: 'fixture-static')
    async def auth(token):
        assert token == 'fixture-static'
        return case != 'auth_failure'
    monkeypatch.setattr(readiness, 'authenticated_store_ok', auth)
    if case == 'ok':
        await readiness.wait_until_ready(REV)
    else:
        with pytest.raises(readiness.ReadinessFailed):
            await readiness.wait_until_ready(REV)


@pytest.mark.asyncio
async def test_startup_timeout_is_bounded(monkeypatch):
    fake_client(monkeypatch)
    with pytest.raises(readiness.ReadinessFailed, match='startup_timeout'):
        await readiness.wait_until_ready(REV, timeout=0)


def test_readiness_cli_never_emits_exception_payload_or_token(monkeypatch, capsys):
    async def failed(*args):
        raise RuntimeError('fixture-sensitive-token')
    monkeypatch.setattr(readiness, 'wait_until_ready', failed)
    assert readiness.main(REV) == 1
    output = capsys.readouterr().out
    assert 'RuntimeError' in output and 'failed' in output
    assert 'fixture-sensitive' not in output
