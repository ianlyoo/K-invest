from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from urllib.parse import parse_qs, urlparse

import pytest

pytestmark = pytest.mark.usefixtures("owner_credentials")
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.testclient import TestClient

from auth_observability import AuthObservability
from oauth_provider import DualTokenVerifier, PersonalOAuthProvider
from oauth_registration import build_dcr_routes

URL = "https://example.invalid/"


def app_for(p, monkeypatch):
    import server
    monkeypatch.setattr(server, "_oauth_provider", p)
    monkeypatch.setattr(server, "_MCP_PUBLIC_URL", URL)
    mcp = FastMCP("fixture", stateless_http=True, token_verifier=DualTokenVerifier("fixture-static", p),
        auth=AuthSettings(issuer_url=URL, resource_server_url=URL),
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    @mcp.tool()
    def get_service_health(mode: str = "passive") -> dict:
        return {"overall_status": "unknown", "mode": mode}
    for route in build_dcr_routes(p, AnyHttpUrl(URL), ["read"]):
        if route.path != "/authorize":
            mcp.custom_route(route.path, methods=list(route.methods or []))(route.endpoint)
    mcp.custom_route("/authorize", methods=["GET", "POST"])(server._oauth_authorize)
    app = mcp.streamable_http_app()
    app.add_middleware(AuthObservability)
    return app


class SyntheticProxy:
    """In-process forwarding boundary: no sockets, nginx or real proxy secrets."""
    def __init__(self, app):
        self.app = app
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "headers": [*scope.get("headers", []), (b"x-forwarded-proto", b"https")]}
        await self.app(scope, receive, send)


def authorize(client):
    verifier = "fixture-code-verifier-123456789012345678901234567890"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {"client_id": "fixture-client", "redirect_uri": "https://example.invalid/callback",
        "response_type": "code", "code_challenge": challenge, "code_challenge_method": "S256", "resource": URL}
    page = client.get("/authorize", params=params, follow_redirects=False)
    assert page.status_code == 200
    nonce = re.search(r'name="nonce" value="([A-Za-z0-9_.-]+)"', page.text)[1]
    cookie = client.cookies.get("__Host-investmcp-consent")
    response = client.post("/authorize", data={**params, "nonce": nonce,
        "approval_secret": "fixture-owner-passphrase-distinct-from-client-secret"},
        headers={"Origin": URL.rstrip("/"), "Cookie": "__Host-investmcp-consent=" + cookie}, follow_redirects=False)
    assert response.status_code == 302
    code = parse_qs(urlparse(response.headers["location"]).query)["code"][0]
    response = client.post("/token", data={"grant_type": "authorization_code", "code": code, "client_id": "fixture-client",
        "client_secret": "fixture-secret", "redirect_uri": "https://example.invalid/callback", "code_verifier": verifier, "resource": URL})
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize("proxied", [False, True])
def test_pkce_initialize_notification_list_health_refresh_and_protocol(tmp_path, proxied, monkeypatch):
    p = PersonalOAuthProvider("fixture-client", "fixture-secret", tmp_path / "oauth.sqlite3", resource_url=URL)
    app = app_for(p, monkeypatch)
    with TestClient(SyntheticProxy(app) if proxied else app, base_url=URL) as client:
        tokens = authorize(client)
        headers = {"Authorization": "Bearer " + tokens["access_token"], "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": "2025-06-18", "Content-Type": "application/json"}
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18",
            "capabilities": {}, "clientInfo": {"name": "synthetic", "version": "1"}}}
        assert client.post("/mcp", headers=headers, json=init).status_code == 200
        assert client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
        assert client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).status_code == 200
        call = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "get_service_health", "arguments": {"mode": "passive"}}})
        assert call.status_code == 200 and "unknown" in call.text
        bad = client.post("/mcp", headers={**headers, "MCP-Protocol-Version": "2099-01-01"}, json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
        assert bad.status_code == 400
        assert client.post("/mcp", headers=headers, content="malformed").status_code == 400
        response = client.post("/token", data={"grant_type": "refresh_token", "client_id": "fixture-client",
            "client_secret": "fixture-secret", "refresh_token": tokens["refresh_token"], "resource": URL})
        assert response.status_code == 200
        assert response.json()["refresh_token"] != tokens["refresh_token"]
        replay = client.post("/token", data={"grant_type": "refresh_token", "client_id": "fixture-client",
            "client_secret": "fixture-secret", "refresh_token": tokens["refresh_token"]})
        assert replay.status_code == 200
        assert replay.json()["refresh_token"] != response.json()["refresh_token"]
        import time
        now = time.time()
        monkeypatch.setattr("oauth_provider.time.time", lambda: now + 60)
        late = client.post("/token", data={"grant_type": "refresh_token", "client_id": "fixture-client",
            "client_secret": "fixture-secret", "refresh_token": tokens["refresh_token"]})
        assert late.status_code == 400 and late.json()["error"] == "invalid_grant"
        assert client.post("/register", json={}).status_code == 400


def test_redaction_safe_reasons_and_503(tmp_path, caplog, monkeypatch):
    p = PersonalOAuthProvider("fixture-client", "fixture-secret", tmp_path / "oauth.sqlite3", resource_url=URL)
    with TestClient(app_for(p, monkeypatch), base_url=URL) as client, caplog.at_level(logging.INFO, logger="k-invest.auth"):
        tokens = authorize(client)
        headers = {"Authorization": "Bearer " + tokens["access_token"], "Accept": "application/json, text/event-stream"}
        client.post("/mcp?secret=fixture-sensitive&account=fixture-account", headers=headers, content='{"secret":"fixture-sensitive"}')
        p.store.path.rename(tmp_path / "offline.sqlite3")
        failed = client.get("/mcp", headers=headers)
        assert failed.status_code == 503 and failed.json()["error"] == "temporarily_unavailable"
    events = [json.loads(r.message) for r in caplog.records if r.name == "k-invest.auth"]
    assert any(e["reason"] == "invalid_request" for e in events)
    assert any(e["reason"] == "auth_store_unavailable" and e["status"] == 503 for e in events)
    encoded = json.dumps(events)
    for value in [tokens["access_token"], tokens["refresh_token"], "fixture-secret", "fixture-sensitive", "fixture-account"]:
        assert value not in encoded
    assert all(e["request_id"] for e in events)


def test_opaque_route_is_only_a_category(caplog):
    async def endpoint(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                else:
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b""})
    with TestClient(AuthObservability(endpoint)) as client, caplog.at_level(logging.INFO, logger="k-invest.auth"):
        client.get("/mcp-fixture-sensitive-route?token=fixture-sensitive")
    records = [r.message for r in caplog.records if r.name == "k-invest.auth"]
    assert "legacy_mcp" in records[0]
    assert "fixture-sensitive" not in " ".join(records)


def test_uvicorn_access_log_never_emits_raw_path(caplog):
    import server  # installs the suppression filter
    del server
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "fixture-address", "GET", "/mcp-fixture-sensitive?token=fixture-sensitive", "1.1", 401)
    assert not any(r.name == "uvicorn.access" for r in caplog.records)
