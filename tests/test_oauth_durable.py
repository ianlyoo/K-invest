from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from mcp.server.auth.provider import AuthorizationParams, TokenError
from pydantic import AnyUrl

from oauth_provider import DualTokenVerifier, PersonalOAuthProvider
from oauth_store import AuthStoreUnavailable, OAuthStore


def provider(tmp_path, **kwargs):
    return PersonalOAuthProvider("fixture-client", "fixture-secret", tmp_path / "oauth.sqlite3", **kwargs)


async def issue(p, scopes=None):
    client = await p.get_client("fixture-client")
    params = AuthorizationParams(state=None, scopes=scopes or ["read"],
        redirect_uri=AnyUrl("https://example.invalid/callback"), redirect_uri_provided_explicitly=True,
        code_challenge="fixture-challenge", resource="https://example.invalid/")
    code = await p.authorize(client, params)
    auth_code = await p.load_authorization_code(client, code)
    return client, await p.exchange_authorization_code(client, auth_code)


@pytest.mark.asyncio
async def test_restart_rotation_resource_scope_and_replay(tmp_path, monkeypatch):
    clock = [100000]
    monkeypatch.setattr("oauth_provider.time.time", lambda: clock[0])
    p = provider(tmp_path, resource_url="https://example.invalid")
    client, tokens = await issue(p)
    restarted = provider(tmp_path, resource_url="https://example.invalid/")
    assert await restarted.load_access_token(tokens.access_token)
    assert str((await restarted.get_client(client.client_id)).redirect_uris[0]) == "https://example.invalid/callback"
    refresh = await restarted.load_refresh_token(client, tokens.refresh_token)
    successor = await restarted.exchange_refresh_token(client, refresh, [])
    assert (await restarted.load_access_token(successor.access_token)).resource == "https://example.invalid/"
    assert (await restarted.load_access_token(successor.access_token)).scopes == []
    assert await restarted.load_refresh_token(client, successor.refresh_token)
    assert await restarted.load_refresh_token(client, tokens.refresh_token)
    clock[0] += 60
    assert await restarted.load_refresh_token(client, tokens.refresh_token) is None
    # Replay invalidates the successor family, including existing access tokens.
    assert await restarted.load_refresh_token(client, successor.refresh_token) is None
    assert await restarted.load_access_token(successor.access_token) is None


@pytest.mark.asyncio
async def test_30_day_boundary_access_default_and_override(tmp_path, monkeypatch):
    clock = [100000]
    monkeypatch.setattr("oauth_provider.time.time", lambda: clock[0])
    monkeypatch.delenv("MCP_OAUTH_TOKEN_TTL", raising=False)
    monkeypatch.delenv("MCP_OAUTH_REFRESH_TOKEN_TTL", raising=False)
    p = provider(tmp_path)
    client, token = await issue(p)
    assert p.access_ttl == 3600 and p.refresh_ttl == 30 * 86400
    clock[0] = 100000 + 3600
    assert await p.load_access_token(token.access_token) is None
    clock[0] = 100000 + 30 * 86400 - 1
    assert await p.load_refresh_token(client, token.refresh_token)
    clock[0] += 1
    assert await p.load_refresh_token(client, token.refresh_token) is None
    monkeypatch.setenv("MCP_OAUTH_REFRESH_TOKEN_TTL", "86400")
    overridden = provider(tmp_path)
    assert overridden.refresh_ttl == 86400 and overridden.access_ttl == 3600
    clock[0] = 5000000
    client, token = await issue(overridden)
    clock[0] += 86400
    assert await overridden.load_refresh_token(client, token.refresh_token) is None


@pytest.mark.asyncio
async def test_code_survives_restart_single_use_client_scope_audience(tmp_path):
    p = provider(tmp_path, resource_url="https://example.invalid/")
    client = await p.get_client("fixture-client")
    params = AuthorizationParams(state=None, scopes=["read"], redirect_uri=AnyUrl("https://example.invalid/callback"),
        redirect_uri_provided_explicitly=True, code_challenge="fixture", resource="https://example.invalid/")
    raw = await p.authorize(client, params)
    restarted = provider(tmp_path, resource_url="https://example.invalid/")
    code = await restarted.load_authorization_code(client, raw)
    token = await restarted.exchange_authorization_code(client, code)
    with pytest.raises(TokenError):
        await restarted.exchange_authorization_code(client, code)
    other_client = client.model_copy(update={"client_id": "other"})
    refresh = await restarted.load_refresh_token(client, token.refresh_token)
    with pytest.raises(TokenError):
        await restarted.exchange_refresh_token(client, refresh, ["write"])
    assert await provider(tmp_path, resource_url="https://another.invalid/").load_access_token(token.access_token) is None
    assert await restarted.load_refresh_token(other_client, token.refresh_token) is None
    assert await restarted.load_refresh_token(client, token.refresh_token) is None


@pytest.mark.asyncio
async def test_revocation_persists_and_static_survives(tmp_path):
    p = provider(tmp_path)
    client, tokens = await issue(p)
    refresh = await p.load_refresh_token(client, tokens.refresh_token)
    await p.revoke_token(refresh)
    restarted = provider(tmp_path)
    assert await restarted.load_access_token(tokens.access_token) is None
    assert await restarted.load_refresh_token(client, tokens.refresh_token) is None
    assert await DualTokenVerifier("fixture-static", restarted).verify_token("fixture-static")


@pytest.mark.asyncio
async def test_transaction_failure_rolls_back_rotation(tmp_path, monkeypatch):
    p = provider(tmp_path)
    client, tokens = await issue(p)
    refresh = await p.load_refresh_token(client, tokens.refresh_token)
    original = OAuthStore.insert
    calls = [0]
    def fail_second(db, record):
        calls[0] += 1
        if calls[0] == 2:
            raise sqlite3.OperationalError("synthetic insert failure")
        original(db, record)
    monkeypatch.setattr(OAuthStore, "insert", staticmethod(fail_second))
    with pytest.raises(AuthStoreUnavailable):
        await p.exchange_refresh_token(client, refresh, ["read"])
    assert await p.load_refresh_token(client, tokens.refresh_token)


@pytest.mark.asyncio
async def test_no_raw_credentials_on_disk_and_store_outage(tmp_path):
    p = provider(tmp_path)
    _, tokens = await issue(p)
    raw = p.store.path.read_bytes()
    for value in [tokens.access_token, tokens.refresh_token, "fixture-secret"]:
        assert value.encode() not in raw
    assert p.store.path.stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o077 == 0
    p.store.path.rename(tmp_path / "offline.sqlite3")
    assert p.store.health() == "unavailable"
    with pytest.raises(AuthStoreUnavailable):
        await p.load_access_token(tokens.access_token)
    assert not p.store.path.exists()


def test_simultaneous_rotation_preserves_both_responses(tmp_path):
    p = provider(tmp_path)
    client, tokens = asyncio.run(issue(p))
    def rotate():
        async def work():
            candidate = provider(tmp_path)
            grant = await candidate.load_refresh_token(client, tokens.refresh_token)
            if not grant:
                return False
            try:
                return await candidate.exchange_refresh_token(client, grant, ["read"])
            except TokenError:
                return False
        return asyncio.run(work())
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: rotate(), range(2)))
    assert all(responses)
    assert responses[0].refresh_token != responses[1].refresh_token
    for response in reversed(responses):
        assert asyncio.run(p.load_access_token(response.access_token))
        assert asyncio.run(p.load_refresh_token(client, response.refresh_token))


@pytest.mark.parametrize("value", ["0", "-1", "invalid", "999999999"])
def test_ttl_validation(tmp_path, monkeypatch, value):
    monkeypatch.setenv("MCP_OAUTH_REFRESH_TOKEN_TTL", value)
    with pytest.raises(ValueError, match="MCP_OAUTH_REFRESH_TOKEN_TTL"):
        provider(tmp_path)


def test_store_rejects_unsafe_directory_and_unknown_schema(tmp_path):
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    with pytest.raises(AuthStoreUnavailable):
        OAuthStore(unsafe / "state.sqlite3")
    safe = provider(tmp_path).store
    with safe.connection() as db:
        db.execute("PRAGMA user_version=999")
    with pytest.raises(AuthStoreUnavailable):
        provider(tmp_path)


@pytest.mark.asyncio
async def test_fresh_interpreter_restart(tmp_path):
    import json
    import subprocess
    import sys
    p = provider(tmp_path)
    _, tokens = await issue(p)
    script = '''
import asyncio, json, sys
from pathlib import Path
from oauth_provider import PersonalOAuthProvider
async def main():
    data = json.load(sys.stdin)
    p = PersonalOAuthProvider("fixture-client", "fixture-secret", Path(data["path"]))
    client = await p.get_client("fixture-client")
    assert await p.load_access_token(data["access"])
    refresh = await p.load_refresh_token(client, data["refresh"])
    assert refresh
    successor = await p.exchange_refresh_token(client, refresh, ["read"])
    assert await p.load_access_token(successor.access_token)
    print("restart_continuity_passed")
asyncio.run(main())
'''
    result = subprocess.run([sys.executable, "-c", script], input=json.dumps({"path": str(p.store.path),
        "access": tokens.access_token, "refresh": tokens.refresh_token}), capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "restart_continuity_passed"
