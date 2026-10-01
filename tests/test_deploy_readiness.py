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
    async def failed(*args, **kwargs):
        raise RuntimeError('fixture-sensitive-token')
    monkeypatch.setattr(readiness, 'wait_until_ready', failed)
    assert readiness.main(REV) == 1
    output = capsys.readouterr().out
    assert 'RuntimeError' in output and 'failed' in output
    assert 'fixture-sensitive' not in output


@pytest.mark.parametrize('configured', [None, '%d/invest-desk-token',
    '${CREDENTIALS_DIRECTORY}/invest-desk-token',
    '/run/credentials/toss-mcp.service/invest-desk-token'])
def test_service_readiness_requires_resolved_process_credential_path(monkeypatch, configured):
    from pathlib import Path
    token = 'synthetic-private-service-token-with-32-characters'
    runtime = b'CREDENTIALS_DIRECTORY=/run/credentials/toss-mcp.service\0'
    if configured is not None:
        runtime += b'KINVEST_SERVICE_TOKEN_FILE=' + configured.encode() + b'\0'
    monkeypatch.setattr(Path, 'read_bytes', lambda path: runtime)
    def read(path):
        assert str(path) == '/proc/123/root/run/credentials/toss-mcp.service/invest-desk-token'
        return token + '\n'
    monkeypatch.setattr(Path, 'read_text', read)
    if configured == str(readiness.SERVICE_CREDENTIAL):
        assert readiness.inherited_service_bearer('123') == token
    else:
        with pytest.raises(readiness.ReadinessFailed, match='service_token_runtime_path_missing_or_invalid'):
            readiness.inherited_service_bearer('123')


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['ok', 'missing_path', 'restrictions_failed'])
async def test_upgrade_gate_requires_service_auth_besides_legacy_store(monkeypatch, capsys, case):
    fake_client(monkeypatch)
    props = {'ActiveState': 'active', 'SubState': 'running', 'MainPID': '123',
             'NRestarts': '0', 'WorkingDirectory': '/home/ubuntu/toss-mcp-releases/' + REV}
    monkeypatch.setattr(readiness, 'service_properties', lambda: props)
    monkeypatch.setattr(readiness, 'inherited_bearer', lambda pid: 'fixture-static')
    async def auth(token):
        return True
    monkeypatch.setattr(readiness, 'authenticated_store_ok', auth)
    def token(pid):
        if case == 'missing_path':
            raise readiness.ReadinessFailed('service_token_runtime_path_missing_or_invalid')
        return 'synthetic-service-secret-not-for-output'
    monkeypatch.setattr(readiness, 'inherited_service_bearer', token)
    async def restrictions(bearer):
        return {'service_tools_list_exact_eight': case != 'restrictions_failed',
                'non_allowlisted_call_forbidden': True, 'service_token_forwarded_header_401': True}
    monkeypatch.setattr(readiness, 'service_readiness_checks', restrictions)
    if case == 'ok':
        await readiness.wait_until_ready(REV, service_token_required=True)
    else:
        with pytest.raises(readiness.ReadinessFailed):
            await readiness.wait_until_ready(REV, service_token_required=True)
    output = capsys.readouterr().out
    assert 'synthetic-service-secret' not in output
    if case != 'missing_path':
        assert 'service_token_forwarded_header_401' in output


@pytest.mark.asyncio
@pytest.mark.parametrize('missing_tool', [False, True])
async def test_service_gate_collects_catalog_forbidden_and_header_results_without_broker_calls(monkeypatch, missing_tool):
    from test_service_auth import HEADERS, SERVICE_READ_TOOLS, TOKEN, application
    app, fallback, called = application()
    if missing_tool:
        app.remove_tool(next(iter(SERVICE_READ_TOOLS)))
    asgi = app.streamable_http_app()
    async with asgi.router.lifespan_context(asgi):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi, client=('127.0.0.1', 12345)),
                                    headers=HEADERS) as client:
            checks = await readiness.service_readiness_checks(TOKEN, client)
    assert checks == {'service_tools_list_exact_eight': not missing_tool,
        'non_allowlisted_call_forbidden': True, 'service_token_forwarded_header_401': True}
    assert called == []
    fallback.verify_token.assert_not_awaited()


def test_readiness_cli_imports_permission_constant_outside_release_cwd(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    script = Path(readiness.__file__).resolve()
    result = subprocess.run([sys.executable, str(script), 'invalid-revision', '--service-token'],
        cwd=tmp_path, env={'PATH': '/usr/bin:/bin'}, capture_output=True, text=True)
    assert result.returncode == 1 and 'revision_format' in result.stdout
    assert 'ModuleNotFoundError' not in result.stderr
