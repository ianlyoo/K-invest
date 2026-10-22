"""Expected SSE disconnection is local cleanup, with worker capacity retained."""
import asyncio
import json
import logging
import threading

import anyio
import httpx
import pytest
from exceptiongroup import ExceptionGroup
from mcp.server.transport_security import TransportSecuritySettings

import server
from auth_observability import _ExpectedPeerDisconnect, peer_disconnected


@pytest.mark.asyncio
async def test_sse_disconnect_returns_worker_slot(caplog):
    app = server.ObservedFastMCP('synthetic-disconnect', stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    entered, release = threading.Event(), threading.Event()

    @app.tool()
    def blocking() -> dict[str, bool]:
        entered.set()
        assert release.wait(5)
        return {'ok': True}

    @app.custom_route('/healthz', methods=['GET'])
    async def health(request):
        return await server.healthz(request)

    http_app = app.streamable_http_app()
    disconnect = asyncio.Event()
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': 'blocking', 'arguments': {}}}).encode()
    first = True
    messages = []

    async def receive():
        nonlocal first
        if first:
            first = False
            return {'type': 'http.request', 'body': body, 'more_body': False}
        await disconnect.wait()
        return {'type': 'http.disconnect'}

    async def send(message):
        messages.append(message)

    scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
        'http_version': '1.1', 'method': 'POST', 'scheme': 'http', 'path': '/mcp',
        'raw_path': b'/mcp', 'root_path': '', 'query_string': b'',
        'headers': [(b'host', b'example.invalid'), (b'content-type', b'application/json'),
            (b'accept', b'application/json, text/event-stream'), (b'mcp-protocol-version', b'2025-06-18')],
        'client': ('127.0.0.1', 12345), 'server': ('example.invalid', 80)}
    async with http_app.router.lifespan_context(http_app):
        pending = asyncio.create_task(http_app(scope, receive, send))
        try:
            with anyio.fail_after(2):
                while not entered.is_set():
                    await anyio.sleep(.01)
            assert app._tool_limiter.borrowed_tokens == 1
            disconnect.set()
            await asyncio.wait_for(pending, 1)
            assert app._tool_limiter.borrowed_tokens == 1
        finally:
            release.set()
        with anyio.fail_after(2):
            while app._tool_limiter.borrowed_tokens:
                await anyio.sleep(.01)
        await anyio.sleep(.1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=http_app), base_url='http://example.invalid') as client:
            assert (await client.get('/healthz')).status_code == 200
        error_events = [r.getMessage() for r in caplog.records if r.levelno >= 40]
        assert not error_events


@pytest.mark.parametrize('disconnected,mixed,expected', [(False, False, True), (True, False, False), (True, True, True)])
def test_disconnect_filter_retains_unexpected_and_mixed_errors(disconnected, mixed, expected):
    event = threading.Event()
    if disconnected:
        event.set()
    errors = [anyio.ClosedResourceError()]
    if mixed:
        errors.append(ValueError('synthetic failure'))
    error = ExceptionGroup('synthetic session', errors)
    record = logging.LogRecord('mcp.server.streamable_http_manager', logging.ERROR,
        __file__, 1, 'Stateless session crashed', (), (type(error), error, None))
    token = peer_disconnected.set(event)
    try:
        assert _ExpectedPeerDisconnect().filter(record) is expected
    finally:
        peer_disconnected.reset(token)
