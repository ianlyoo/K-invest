"""Owner gate and public DCR regressions. Inert files and ASGI only."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import io
import json
import logging
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

import pytest
from starlette.testclient import TestClient
from test_auth_transport import app_for

import oauth_admin as admin
import owner_gate as gate
from oauth_provider import PersonalOAuthProvider

pytestmark = pytest.mark.usefixtures("owner_credentials")
MODULE_SHA256 = "59f097276c57cc4ef5286e4682c631d2ad8d23c86cec38f83ec355f1f6174095"
URL = "https://example.invalid/"
CALLBACK = "https://chatgpt.com/connector/callback"
VERIFIER = "fixture-code-verifier-123456789012345678901234567890"
SCOPE = "read"

RESOURCE = URL
STATIC_SECRET = "fixture-secret"
STATIC_FLAG = "MCP_OAUTH_STATIC_CLIENT_ENABLED"
STORE_MODULE = "oauth_store"
AUTH_LOGGER = "k-invest.auth"


def application(tmp_path, monkeypatch):
    provider = PersonalOAuthProvider("fixture-client", STATIC_SECRET, tmp_path / "oauth.sqlite3", resource_url=RESOURCE)
    return app_for(provider, monkeypatch), provider


def call(client, token):
    return client.post("/mcp", headers={"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_service_health", "arguments": {"mode": "passive"}}})

class Hidden(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.fields = {}
        self.action = None
        self.feed(page)
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.action = attrs["action"]
        if tag == "input" and attrs.get("type") == "hidden":
            self.fields[attrs["name"]] = attrs["value"]


def params(client_id="fixture-client", **updates):
    challenge = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
    return {"client_id": client_id, "redirect_uri": CALLBACK, "response_type": "code", "code_challenge": challenge,
            "code_challenge_method": "S256", "scope": SCOPE, "resource": RESOURCE, "state": "state&encoded=1+two", **updates}


def approve(client, fields, phrase):
    return client.post("/authorize", data={**fields, "approval_secret": phrase}, headers={"Origin": URL.rstrip("/")}, follow_redirects=False)


def token_form(code, client_id):
    return {"grant_type": "authorization_code", "code": code, "client_id": client_id, "redirect_uri": CALLBACK,
            "code_verifier": VERIFIER, "resource": RESOURCE}


def register(client, **updates):
    return client.post("/register", json={"redirect_uris": [CALLBACK], "client_name": "<script>client</script>",
        "token_endpoint_auth_method": "none", **updates})


def test_module_sha_pin():
    assert hashlib.sha256(Path(gate.__file__).read_bytes()).hexdigest() == MODULE_SHA256


def test_hash_cli_and_malformed_missing_credentials(owner_credentials, monkeypatch, capsys):
    phrase, line, directory = owner_credentials
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO((phrase + "\n").encode())))
    gate.main(["hash"])
    produced = capsys.readouterr().out.strip()
    assert produced.startswith("scrypt$16384$8$1$") and phrase not in produced
    path = directory / "owner-gate"
    for value in ("bad", "scrypt$999999999$8$1$bad$bad", "\n", "bad\nsecond"):
        path.write_text(value)
        with pytest.raises(gate.GateError) as error:
            gate.OwnerGate().verify(phrase)
        assert error.value.code == "owner_gate_not_configured" and error.value.status == 503
        assert phrase not in str(error.value) and line not in str(error.value)
    path.unlink()
    with pytest.raises(gate.GateError, match="owner_gate_not_configured"):
        gate.OwnerGate().configured()


def test_lockout_global_and_exact_expiry(owner_credentials, monkeypatch):
    phrase, _, _ = owner_credentials
    now = [0.0]
    owner = gate.OwnerGate(clock=lambda: now[0])
    monkeypatch.setattr(gate, "_GATE", owner)
    for i in range(5):
        with pytest.raises(gate.GateError) as error:
            gate.owner_gate().verify("wrong")
        assert error.value.status == (429 if i == 4 else 400)
    now[0] = 899.999
    with pytest.raises(gate.GateError) as error:
        gate.owner_gate().verify(phrase)
    assert error.value.retry_after_s == 1
    now[0] = 900
    gate.owner_gate().verify(phrase)


@pytest.mark.parametrize("uri,allowed", [(CALLBACK, True), ("https://claude.ai/callback", True), ("https://claude.com/callback", True),
    ("https://chatgpt.com.evil.invalid/cb", False), ("https://evil.chatgpt.com/cb", False), ("http://chatgpt.com/cb", False),
    ("https://chatgpt.com@evil.invalid/cb", False), ("https://chatgpt.com:444/cb", False), ("https://chatgpt.com/cb#fragment", False)])
def test_default_redirect_allowlist(monkeypatch, uri, allowed):
    monkeypatch.delenv("OWNER_GATE_REDIRECT_ORIGINS")
    assert gate.redirect_allowed(uri) is allowed


def test_static_consent_hidden_binding_no_secrets_and_missing_gate(tmp_path, monkeypatch, owner_credentials, caplog):
    phrase, line, directory = owner_credentials
    app, provider = application(tmp_path, monkeypatch)
    with TestClient(app, base_url=URL) as client, caplog.at_level(logging.INFO, logger=AUTH_LOGGER):
        page = client.get("/authorize", params=params(), follow_redirects=False)
        assert page.status_code == 200 and "location" not in page.headers
        assert page.headers["Cache-Control"] == "no-store" and page.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
        assert "chatgpt.com" in page.text and 'type="password"' in page.text
        fields = Hidden(page.text).fields
        assert set(params()) <= fields.keys()
        with provider.store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM grants").fetchone()[0] == 0
        assert approve(client, {**fields, "state": "swapped"}, phrase).status_code == 400
        assert approve(client, {**fields, "code_challenge": "b" * 43}, phrase).status_code == 400
        assert approve(client, fields, STATIC_SECRET).status_code == 400
        fields = Hidden(client.get("/authorize", params=params()).text).fields
        response = approve(client, fields, phrase)
        assert response.status_code == 302
        query = parse_qs(urlsplit(response.headers["location"]).query)
        assert query["state"] == [params()["state"]]
        assert response.headers["location"].startswith(CALLBACK + "?code=")
        assert approve(client, fields, phrase).status_code in (400, 403)
        form = {**token_form(query["code"][0], "fixture-client"), "client_secret": STATIC_SECRET}
        exchanged = client.post("/token", data=form)
        assert exchanged.status_code == 200, exchanged.text
        tokens = exchanged.json()
        assert call(client, tokens["access_token"]).status_code == 200
        # Removing gate configuration blocks new authorizations, never refresh.
        (directory / "owner-gate").unlink()
        monkeypatch.setattr(provider, "owner_gate", gate.OwnerGate())
        assert client.get("/authorize", params=params()).json() == {"error": "owner_gate_not_configured"}
        response = client.post("/token", data={"grant_type": "refresh_token", "client_id": "fixture-client", "client_secret": STATIC_SECRET,
            "refresh_token": tokens["refresh_token"], "resource": RESOURCE})
        assert response.status_code == 200
        assert phrase not in caplog.text and line not in caplog.text
        assert phrase not in page.text and line not in page.text


@pytest.mark.parametrize("method", ["none", "client_secret_post"])
def test_dcr_owner_gate_pkce_mcp_restart_and_client_binding(tmp_path, monkeypatch, owner_credentials, method):
    phrase, line, _ = owner_credentials
    app, provider = application(tmp_path, monkeypatch)
    with TestClient(app, base_url=URL) as client:
        metadata = client.get("/.well-known/oauth-authorization-server").json()
        assert metadata["registration_endpoint"] == URL.rstrip("/") + "/register"
        response = register(client, token_endpoint_auth_method=method)
        assert response.status_code == 201, response.text
        registered = response.json()
        client_id = registered["client_id"]
        assert ("client_secret" in registered) == (method == "client_secret_post")
        page = client.get("/authorize", params=params(client_id))
        assert page.status_code == 200 and "<script>client</script>" not in page.text
        assert html.escape("<script>client</script>") in page.text
        fields = Hidden(page.text).fields
        assert approve(client, fields, "wrong").status_code == 400
        fields = Hidden(client.get("/authorize", params=params(client_id)).text).fields
        approved = approve(client, fields, phrase)
        assert approved.status_code == 302
        code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
        form = token_form(code, client_id)
        if method == "client_secret_post":
            form["client_secret"] = registered["client_secret"]
            assert client.post("/token", data={**form, "client_secret": "wrong"}).status_code == 401
            assert registered["client_secret"].encode() not in provider.store.path.read_bytes()
        assert client.post("/token", data={**form, "code_verifier": "wrong"}).status_code == 400
        assert client.post("/token", data={**form, "client_id": "fixture-client", "client_secret": STATIC_SECRET}).status_code == 400
        tokens_response = client.post("/token", data=form)
        assert tokens_response.status_code == 200, tokens_response.text
        tokens = tokens_response.json()
        assert call(client, tokens["access_token"]).status_code == 200
        assert phrase not in page.text and line not in page.text
    restarted_app, restarted = application(tmp_path, monkeypatch)
    assert asyncio.run(restarted.get_client(client_id)).client_id == client_id
    with TestClient(restarted_app, base_url=URL) as client:
        assert call(client, tokens["access_token"]).status_code == 200
        refresh = {"grant_type": "refresh_token", "client_id": client_id, "refresh_token": tokens["refresh_token"], "resource": RESOURCE}
        if method == "client_secret_post":
            refresh["client_secret"] = registered["client_secret"]
        assert client.post("/token", data=refresh).status_code == 200
        assert client.post("/authorize", data={**params(client_id), "nonce": "missing", "approval_secret": phrase}, headers={"Origin": URL.rstrip("/")}).status_code in (400, 403)


@pytest.mark.parametrize("data", [{"redirect_uris": ["https://evil.invalid/cb"]}, {"redirect_uris": ["http://chatgpt.com/cb"]},
    {"client_name": "a" * 129}, {"redirect_uris": [CALLBACK] * 11}, {"token_endpoint_auth_method": "client_secret_basic"},
    {"response_types": ["token"]}, {"grant_types": ["client_credentials"]}, {"scope": "account:write"}])
def test_dcr_invalid_metadata_and_no_pkce_bypass(tmp_path, monkeypatch, data):
    app, provider = application(tmp_path, monkeypatch)
    with TestClient(app, base_url=URL) as client:
        assert register(client, **data).status_code == 400
        registered = register(client).json()
        assert client.get("/authorize", params=params(registered["client_id"], code_challenge_method="plain")).status_code == 400
        assert client.get("/authorize", params=params(registered["client_id"], redirect_uri="https://claude.ai/unregistered")).status_code == 400


def test_durable_cap_and_rate_limit_no_eviction(tmp_path, monkeypatch):
    now = [int(time.time())]
    monkeypatch.setattr(STORE_MODULE + ".time.time", lambda: now[0])
    app, provider = application(tmp_path, monkeypatch)
    with TestClient(app, base_url=URL) as client:
        registered = []
        for batch in range(2):
            for _ in range(5):
                response = register(client)
                assert response.status_code == 201
                registered.append(response.json()["client_id"])
            denied = register(client)
            assert denied.status_code == 429 and denied.json()["error"] == "registration_rate_limited"
            now[0] += 3600
        denied = register(client)
        assert denied.status_code == 429 and denied.json()["error"] == "client_limit_reached"
        assert all(asyncio.run(provider.get_client(cid)) for cid in registered)
    _, restarted = application(tmp_path, monkeypatch)
    assert len([cid for cid in registered if asyncio.run(restarted.get_client(cid))]) == 10


def test_static_switch_and_root_local_revoke_all(tmp_path, monkeypatch, owner_credentials, capsys):
    phrase, _, _ = owner_credentials
    app, provider = application(tmp_path, monkeypatch)
    with TestClient(app, base_url=URL) as client:
        fields = Hidden(client.get("/authorize", params=params()).text).fields
        approved = approve(client, fields, phrase)
        code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
        tokens = client.post("/token", data={**token_form(code, "fixture-client"), "client_secret": STATIC_SECRET}).json()
        registered = register(client).json()
    monkeypatch.setenv(STATIC_FLAG, "false")
    app, disabled = application(tmp_path, monkeypatch)
    assert asyncio.run(disabled.get_client("fixture-client")) is None
    assert asyncio.run(disabled.get_client(registered["client_id"]))
    assert asyncio.run(disabled.load_access_token(tokens["access_token"])) is None
    monkeypatch.setattr(admin.os, "geteuid", lambda: 1)
    with pytest.raises(SystemExit, match="requires root"):
        admin.main(["--state-path", str(provider.store.path), "list-clients"])
    monkeypatch.setattr(admin.os, "geteuid", lambda: 0)
    admin.main(["--state-path", str(provider.store.path), "list-clients"])
    rows = json.loads(capsys.readouterr().out)
    assert set(rows[0]) == {"client_id", "client_name", "redirect_hosts"}
    assert rows[0]["redirect_hosts"] == ["chatgpt.com"]
    admin.main(["--state-path", str(provider.store.path), "revoke-all"])
    assert json.loads(capsys.readouterr().out)["revoked_grants"] >= 3
    with provider.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM grants WHERE revoked=0").fetchone()[0] == 0
    assert asyncio.run(provider.load_access_token(tokens["access_token"])) is None


def test_http_lockout_across_clients_and_recovery(tmp_path, monkeypatch, owner_credentials):
    phrase, line, _ = owner_credentials
    now = [0.0]
    monkeypatch.setattr(gate, "_GATE", gate.OwnerGate(clock=lambda: now[0]))
    app, provider = application(tmp_path, monkeypatch)
    with TestClient(app, base_url=URL) as client:
        dynamic = register(client).json()["client_id"]
        for i in range(5):
            page = client.get("/authorize", params=params(dynamic if i % 2 else "fixture-client"))
            fields = Hidden(page.text).fields
            response = approve(client, fields, "wrong")
            assert response.status_code == (429 if i == 4 else 400)
            assert phrase not in response.text and line not in response.text
        fields = Hidden(client.get("/authorize", params=params(dynamic)).text).fields
        locked = approve(client, fields, phrase)
        assert locked.status_code == 429 and locked.headers["Retry-After"] == "900"
        with provider.store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM grants WHERE kind='code'").fetchone()[0] == 0
        now[0] = 900
        fields = Hidden(client.get("/authorize", params=params(dynamic)).text).fields
        assert approve(client, fields, phrase).status_code == 302


def test_gate_and_dcr_request_bounds(tmp_path, monkeypatch):
    app, _ = application(tmp_path, monkeypatch)
    with TestClient(app, base_url=URL) as client:
        assert client.post("/register", content=b"x" * 8193, headers={"Content-Type": "application/json"}).status_code == 400
        assert client.post("/register", content=b'{"client_name":"one","client_name":"two"}', headers={"Content-Type": "application/json"}).status_code == 400
        assert client.get("/authorize", params=[*params().items(), ("client_id", "fixture-client")]).status_code == 400
        assert client.post("/authorize", content=b"x" * 16385, headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 400
        assert client.get("/authorize?" + "x" * 8193).status_code == 400


def test_get_authorize_ignores_unknown_parameters(tmp_path, monkeypatch, owner_credentials):
    phrase = owner_credentials[0]
    app, _ = application(tmp_path, monkeypatch)
    with TestClient(app, base_url=URL) as client:
        cid = register(client).json()["client_id"]
        extra = {**params(cid), "nonce": "client-oidc-nonce", "prompt": "consent", "unknown": "x"}
        page = client.get("/authorize", params=extra, follow_redirects=False)
        assert page.status_code == 200
        fields = Hidden(page.text).fields
        assert fields.keys() == set(params(cid)) | {"nonce"} and fields["nonce"] != "client-oidc-nonce"
        assert client.get("/authorize", params=[*params(cid).items(), ("prompt", "a"), ("prompt", "b")]).status_code == 200
        assert client.get("/authorize", params=[*params(cid).items(), ("scope", SCOPE)]).status_code == 400
        assert approve(client, {**fields, "unknown": "x"}, phrase).status_code in (400, 403)
        response = approve(client, Hidden(client.get("/authorize", params=extra).text).fields, phrase)
        assert response.status_code == 302 and response.headers["location"].startswith(CALLBACK + "?code=")


def test_public_client_revocation_without_secret(tmp_path, monkeypatch, owner_credentials):
    phrase, _, _ = owner_credentials
    app, _ = application(tmp_path, monkeypatch)
    with TestClient(app, base_url=URL) as client:
        metadata = client.get("/.well-known/oauth-authorization-server").json()
        assert set(metadata["token_endpoint_auth_methods_supported"]) == {"none", "client_secret_post"}
        cid = register(client).json()["client_id"]
        fields = Hidden(client.get("/authorize", params=params(cid)).text).fields
        code = parse_qs(urlsplit(approve(client, fields, phrase).headers["location"]).query)["code"][0]
        tokens = client.post("/token", data=token_form(code, cid)).json()
        assert client.post("/revoke", data={"client_id": cid, "token": tokens["refresh_token"], "token_type_hint": "refresh_token"}).status_code == 200
        assert call(client, tokens["access_token"]).status_code == 401


def test_client_secret_credential_precedes_dev_environment(owner_credentials, monkeypatch):
    import server
    _, _, directory = owner_credentials
    monkeypatch.setenv("MCP_OAUTH_CLIENT_SECRET", "fixture-env-fallback-secret")
    assert server._load_oauth_client_secret() == "fixture-secret"
    (directory / "oauth-client-secret").unlink()
    assert server._load_oauth_client_secret() == ""
    monkeypatch.delenv("CREDENTIALS_DIRECTORY")
    assert server._load_oauth_client_secret() == "fixture-env-fallback-secret"


def test_dcr_only_consent_key_survives_restart_without_static_secret(tmp_path, monkeypatch, owner_credentials):
    phrase, _, _ = owner_credentials
    provider = PersonalOAuthProvider(None, None, tmp_path / "oauth.sqlite3", resource_url=RESOURCE)
    with TestClient(app_for(provider, monkeypatch), base_url=URL) as client:
        cid = register(client).json()["client_id"]
        page = client.get("/authorize", params=params(cid))
        fields = Hidden(page.text).fields
        cookie = client.cookies.get("__Host-investmcp-consent")
    restarted = PersonalOAuthProvider(None, None, tmp_path / "oauth.sqlite3", resource_url=RESOURCE)
    with TestClient(app_for(restarted, monkeypatch), base_url=URL) as client:
        client.cookies.set("__Host-investmcp-consent", cookie)
        response = approve(client, fields, phrase)
        assert response.status_code == 302
        code = parse_qs(urlsplit(response.headers["location"]).query)["code"][0]
        assert client.post("/token", data=token_form(code, cid)).status_code == 200


def browser_form_headers(page, action):
    """Model Fetch's POST Origin handling for the document referrer policy."""
    source = urlsplit(str(page.url))
    target = urlsplit(action)
    source_origin = source.scheme + "://" + source.netloc
    target_origin = target.scheme + "://" + target.netloc
    policy = page.headers["Referrer-Policy"]
    if policy == "no-referrer" or (policy == "same-origin" and source_origin != target_origin):
        return {"Origin": "null"}
    assert policy == "same-origin"
    return {"Origin": source_origin, "Referer": str(page.url)}


def browser_form_action_allows(page, target):
    """Chromium checks the document's form-action on redirected navigation too."""
    directives = {parts[0]: parts[1:] for directive in page.headers["Content-Security-Policy"].split(";")
                  if (parts := directive.split())}
    source = urlsplit(str(page.url))
    destination = urlsplit(target)
    origin = destination.scheme + "://" + destination.netloc
    sources = directives["form-action"]
    return origin in sources or ("'self'" in sources and origin == source.scheme + "://" + source.netloc)


@pytest.mark.parametrize("callback", ["https://chatgpt.com/connector/callback", "https://claude.ai/callback", "https://claude.com/callback"])
def test_browser_consent_origin_and_csp_redirect(tmp_path, monkeypatch, owner_credentials, callback):
    phrase, _, _ = owner_credentials
    app, _ = application(tmp_path, monkeypatch)
    with TestClient(app, base_url=URL) as client:
        cid = register(client, redirect_uris=[callback]).json()["client_id"]
        page = client.get("/authorize", params=params(cid, redirect_uri=callback))
        assert page.status_code == 200
        document = Hidden(page.text)
        action = urljoin(str(page.url), document.action)
        fields = {**document.fields, "approval_secret": phrase}
        assert browser_form_action_allows(page, action)
        headers = browser_form_headers(page, action)
        assert headers["Origin"] == URL.rstrip("/")
        assert page.headers["Cache-Control"] == "no-store"
        assert page.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
        # The former no-referrer policy creates an opaque POST origin and stays denied.
        page.headers["Referrer-Policy"] = "no-referrer"
        assert client.post(action, data=fields, headers=browser_form_headers(page, action)).status_code == 403
        page.headers["Referrer-Policy"] = "same-origin"
        response = client.post(action, data=fields, headers=browser_form_headers(page, action), follow_redirects=False)
        assert response.status_code == 302
        destination = response.headers["Location"]
        assert browser_form_action_allows(page, destination)
        allowed_origin = urlsplit(callback).scheme + "://" + urlsplit(callback).netloc
        policy = page.headers["Content-Security-Policy"]
        assert "form-action 'self' " + allowed_origin + ";" in policy
        for origin in ("https://chatgpt.com", "https://claude.ai", "https://claude.com", "https://evil.invalid"):
            if origin != allowed_origin:
                assert not browser_form_action_allows(page, origin + "/callback")
        assert browser_form_headers(page, destination) == {"Origin": "null"}
        page.headers["Content-Security-Policy"] = policy.replace(" 'self' " + allowed_origin, " 'self'")
        assert not browser_form_action_allows(page, destination)


def test_consent_render_rejects_unvalidated_redirect():
    with pytest.raises(gate.GateError, match="invalid_request"):
        gate.consent_page("fixture", "https://evil.invalid/callback", {}, "/authorize")


def test_registration_admin_recovery_preserves_granted_clients(tmp_path, monkeypatch, owner_credentials, capsys):
    phrase, _, _ = owner_credentials
    monkeypatch.setenv(STATIC_FLAG.replace("STATIC_CLIENT_ENABLED", "CLIENT_CAP"), "5")
    app, provider = application(tmp_path, monkeypatch)
    with TestClient(app, base_url=URL) as client:
        ids = [register(client).json()["client_id"] for _ in range(5)]
        assert register(client).json()["error"] == "registration_rate_limited"
        for cid in ids[:2]:
            fields = Hidden(client.get("/authorize", params=params(cid)).text).fields
            response = approve(client, fields, phrase)
            assert response.status_code == 302
            if cid == ids[0]:
                code = parse_qs(urlsplit(response.headers["Location"]).query)["code"][0]
                tokens = client.post("/token", data=token_form(code, cid)).json()
        fields = Hidden(client.get("/authorize", params=params(ids[2])).text).fields
        assert approve(client, fields, "wrong").status_code == 400
        # Approved history must survive revocation and the normal expired-row purge.
        with provider.store.connection() as db:
            db.execute("UPDATE grants SET revoked=1, expires=? WHERE client=?", (int(time.time()) - 8 * 86400, ids[1]))
        provider.store.issue([])
        for command in ("prune-unconsented", "reset-registration-limit"):
            monkeypatch.setattr(admin.os, "geteuid", lambda: 1)
            with pytest.raises(SystemExit, match="requires root"):
                admin.main(["--state-path", str(provider.store.path), command])
        monkeypatch.setattr(admin.os, "geteuid", lambda: 0)
        admin.main(["--state-path", str(provider.store.path), "prune-unconsented"])
        assert capsys.readouterr().out == "3\n"
        assert all(asyncio.run(provider.get_client(cid)) for cid in ids[:2])
        assert all(asyncio.run(provider.get_client(cid)) is None for cid in ids[2:])
        # Pruning frees the cap, but intentionally leaves the separate limiter intact.
        assert register(client).json()["error"] == "registration_rate_limited"
        admin.main(["--state-path", str(provider.store.path), "reset-registration-limit"])
        assert capsys.readouterr().out == "5\n"
        assert register(client).status_code == 201
        assert call(client, tokens["access_token"]).status_code == 200
        assert client.post("/token", data={"grant_type": "refresh_token", "client_id": ids[0],
            "refresh_token": tokens["refresh_token"], "resource": RESOURCE}).status_code == 200
        admin.main(["--state-path", str(provider.store.path), "prune-unconsented"])
        assert capsys.readouterr().out == "1\n"
        assert asyncio.run(provider.get_client(ids[1])) is not None


def test_registration_approval_history_upgrade(tmp_path, monkeypatch, owner_credentials, capsys):
    phrase, _, _ = owner_credentials
    app, provider = application(tmp_path, monkeypatch)
    with TestClient(app, base_url=URL) as client:
        granted = register(client).json()["client_id"]
        unconsented = register(client).json()["client_id"]
        fields = Hidden(client.get("/authorize", params=params(granted)).text).fields
        assert approve(client, fields, phrase).status_code == 302
        fields = Hidden(client.get("/authorize", params=params(unconsented)).text).fields
        assert approve(client, fields, "wrong").status_code == 400
    # Recreate the registration table shipped before the approval marker existed.
    with provider.store.connection() as db:
        db.execute("UPDATE grants SET revoked=1 WHERE client=?", (granted,))
        db.execute("ALTER TABLE registered_clients RENAME TO legacy_clients")
        db.execute("CREATE TABLE registered_clients (client TEXT PRIMARY KEY, metadata TEXT NOT NULL, created INTEGER NOT NULL)")
        db.execute("INSERT INTO registered_clients SELECT client,metadata,created FROM legacy_clients")
        db.execute("DROP TABLE legacy_clients")
    upgraded = type(provider.store)(provider.store.path)
    with upgraded.connection() as db:
        markers = dict(db.execute("SELECT client,consented FROM registered_clients"))
    assert markers == {granted: 1, unconsented: 0}
    monkeypatch.setattr(admin.os, "geteuid", lambda: 0)
    admin.main(["--state-path", str(upgraded.path), "prune-unconsented"])
    assert capsys.readouterr().out == "1\n"
    assert upgraded.get_client(granted) and upgraded.get_client(unconsented) is None
