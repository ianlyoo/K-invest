"""Regression checks for the deploy-after-fixes review, using synthetic inputs."""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.auth.provider import TokenError
from starlette.testclient import TestClient
from test_auth_transport import URL, app_for
from test_oauth_durable import issue, provider

from health_status import HealthStatus
from journal_metadata import annotate_rows, legacy_kis_account_key
from portfolio_model import kis_domestic_transactions
from portfolio_sources import PortfolioCollector


def test_canonical_kis_labels_do_not_collide_and_match_ledger(monkeypatch):
    import server
    payload = {"trades": [{"odno": "fixture-order", "ord_dt": "20260930", "pdno": "005930",
                           "sll_buy_dvsn_cd": "02", "tot_ccld_qty": "1", "avg_prvs": "100"}]}
    def client_for(label):
        return SimpleNamespace(journal_account_key=f"kis:{label}", supports_overseas=False,
            get_domestic_trade_history=lambda *args: payload, get_overseas_trade_history=lambda *args: None)
    monkeypatch.setattr(server, "_get_kis_registry", lambda: SimpleNamespace(
        client_for=client_for, account_labels=lambda: ["primary", "domestic", "overseas"], default_label=lambda: "primary"))
    result = server.get_kis_trade_history("20260901", "20260930", "all")
    ids = []
    for entry in result["data"]["accounts"]:
        label = entry["account"]
        key = f"kis:{label}"
        raw = entry["data"]["domestic"]["trades"][0]
        assert raw["account_key"] == key
        source = "kis:domestic" if label == "primary" else f"kis:{label}:domestic"
        txs = kis_domestic_transactions(payload, source)
        assert txs
        ledger = PortfolioCollector._journal_identity(txs, key)[0]
        assert ledger.account_key == key and ledger.execution_id == raw["execution_id"]
        ids.append(raw["execution_id"])
    assert len(set(ids)) == 3
    assert legacy_kis_account_key("kis:domestic:domestic") == "kis:domestic"
    assert legacy_kis_account_key("kis:overseas:overseas") == "kis:overseas"
    assert annotate_rows(payload["trades"], "kis", "kis:primary", "KR")[0]["execution_id"] == ids[0]


@pytest.mark.parametrize("account", ["", "primary"])
def test_single_kis_missing_credentials_is_failure_with_sources(monkeypatch, account):
    import server
    from kinvest_common import NotConfiguredError
    def missing(label):
        raise NotConfiguredError("kis", "fixture-sensitive")
    monkeypatch.setattr(server, "_get_kis_registry", lambda: SimpleNamespace(
        client_for=missing, default_label=lambda: "primary"))
    result = server.get_kis_trade_history("20260901", "20260930", account)
    assert result["ok"] is False and result["data"] is None
    assert result["error"]["kind"] == "not_configured"
    assert not result["meta"]["complete"]
    assert len(result["meta"]["sources"]) == 2
    assert all(s["result_count"] is None and s["error_code"] == "not_configured" for s in result["meta"]["sources"])
    assert "fixture-sensitive" not in str(result)


def test_passive_health_json_only_never_opens_files_or_builds_clients(monkeypatch):
    import server
    def forbidden(*args, **kwargs):
        pytest.fail("Passive health did IO/client construction")
    for factory in ("_get_toss_registry", "_get_kis_registry", "_get_toss", "_get_kis"):
        monkeypatch.setattr(server, factory, forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(server, "_toss_accounts", None)
    monkeypatch.setattr(server, "_kis_accounts", None)
    monkeypatch.setattr(server, "_oauth_provider", None)
    monkeypatch.setattr(server, "_health_status", HealthStatus())
    for name in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_CREDS_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TOSS_ACCOUNTS_JSON", '[{"label":"fixture","client_id":"fixture-id","client_secret":"fixture-secret"}]')
    data = server.get_service_health()["data"]
    source = next(s for s in data["sources"] if s["source"] == "toss")
    assert source["status"] == "unknown" and source["configuration_present"] is True
    assert source["result_count"] is None


def test_sdk_unauthorized_client_logged_as_client_auth_failed(tmp_path, monkeypatch, caplog):
    p = provider(tmp_path, resource_url=URL)
    with TestClient(app_for(p, monkeypatch), base_url=URL) as client, caplog.at_level(logging.INFO, logger="k-invest.auth"):
        response = client.post("/token", data={"grant_type": "refresh_token", "client_id": "fixture-client",
            "client_secret": "fixture-wrong", "refresh_token": "fixture-sensitive"})
    assert response.status_code == 401 and response.json()["error"] == "unauthorized_client"
    event = json.loads(next(r.message for r in caplog.records if r.name == "k-invest.auth"))
    assert event["reason"] == "client_auth_failed"
    assert "fixture-sensitive" not in str(event) and "fixture-wrong" not in str(event)


@pytest.mark.asyncio
async def test_grace_boundary_restart_fixed_window_and_unused_sibling_retirement(tmp_path, monkeypatch):
    clock = [100000]
    monkeypatch.setattr("oauth_provider.time.time", lambda: clock[0])
    p = provider(tmp_path)
    assert p.refresh_grace == 60
    client, original = await issue(p)
    grant = await p.load_refresh_token(client, original.refresh_token)
    first = await p.exchange_refresh_token(client, grant, ["read"])
    clock[0] += 59
    restarted = provider(tmp_path)
    retry = await restarted.load_refresh_token(client, original.refresh_token)
    second = await restarted.exchange_refresh_token(client, retry, ["read"])
    assert first.refresh_token != second.refresh_token
    assert restarted.store.get("refresh", original.refresh_token)["consumed_at"] == 100000
    # Loading a refresh is not proof of successful use; actual access use selects
    # the winner after the grace, leaving the unused response revoked.
    assert await restarted.load_refresh_token(client, first.refresh_token)
    clock[0] += 1
    assert await restarted.load_access_token(second.access_token)
    assert await restarted.load_access_token(first.access_token) is None
    assert await restarted.load_refresh_token(client, first.refresh_token) is None
    assert await restarted.load_refresh_token(client, second.refresh_token)
    assert await restarted.load_refresh_token(client, original.refresh_token) is None
    assert await restarted.load_access_token(second.access_token) is None
    assert await restarted.load_refresh_token(client, second.refresh_token) is None


@pytest.mark.asyncio
async def test_grace_preserves_used_siblings_and_prevents_scope_restore(tmp_path, monkeypatch):
    clock = [100000]
    monkeypatch.setattr("oauth_provider.time.time", lambda: clock[0])
    p = provider(tmp_path)
    client, original = await issue(p)
    grant = await p.load_refresh_token(client, original.refresh_token)
    first = await p.exchange_refresh_token(client, grant, [])
    with pytest.raises(TokenError):
        await p.exchange_refresh_token(client, grant, ["read"])
    second = await p.exchange_refresh_token(client, grant, [])
    assert (await p.load_access_token(first.access_token)).scopes == []
    assert (await p.load_access_token(second.access_token)).scopes == []
    clock[0] += 60
    assert await p.load_access_token(first.access_token)
    assert await p.load_access_token(second.access_token)
    # No plaintext successor or parent token is persisted, including after retries.
    disk = p.store.path.read_bytes()
    for token in (original, first, second):
        assert token.access_token.encode() not in disk and token.refresh_token.encode() not in disk


@pytest.mark.asyncio
async def test_wrong_client_replay_inside_grace_revokes_family(tmp_path):
    p = provider(tmp_path)
    client, original = await issue(p)
    grant = await p.load_refresh_token(client, original.refresh_token)
    successor = await p.exchange_refresh_token(client, grant, ["read"])
    wrong = client.model_copy(update={"client_id": "fixture-other"})
    assert await p.load_refresh_token(wrong, original.refresh_token) is None
    assert await p.load_access_token(successor.access_token) is None
    assert await p.load_refresh_token(client, successor.refresh_token) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("grace", [0, 1, 120, 300])
async def test_configurable_grace_exact_boundary(tmp_path, monkeypatch, grace):
    monkeypatch.setenv("MCP_OAUTH_REFRESH_GRACE_SECONDS", str(grace))
    clock = [100000]
    monkeypatch.setattr("oauth_provider.time.time", lambda: clock[0])
    p = provider(tmp_path)
    assert p.refresh_grace == grace
    client, original = await issue(p)
    grant = await p.load_refresh_token(client, original.refresh_token)
    successor = await p.exchange_refresh_token(client, grant, ["read"])
    if grace:
        clock[0] += grace - 1
        assert await p.load_refresh_token(client, original.refresh_token)
        clock[0] += 1
    assert await p.load_refresh_token(client, original.refresh_token) is None
    assert await p.load_access_token(successor.access_token) is None


@pytest.mark.parametrize("value", ["-1", "301", "invalid"])
def test_grace_validation(tmp_path, monkeypatch, value):
    monkeypatch.setenv("MCP_OAUTH_REFRESH_GRACE_SECONDS", value)
    with pytest.raises(ValueError, match="MCP_OAUTH_REFRESH_GRACE_SECONDS"):
        provider(tmp_path)


@pytest.mark.asyncio
async def test_schema_one_migration_keeps_tokens_and_rejects_undated_replay(tmp_path):
    p = provider(tmp_path)
    client, token = await issue(p)
    with sqlite3.connect(p.store.path) as db:
        db.execute("DROP INDEX grants_parent")
        db.execute("DROP INDEX grants_pair")
        for column in ("consumed_at", "parent", "pair", "used_at"):
            db.execute(f"ALTER TABLE grants DROP COLUMN {column}")
        db.execute("PRAGMA user_version=1")
    migrated = provider(tmp_path)
    assert await migrated.load_access_token(token.access_token)
    assert await migrated.load_refresh_token(client, token.refresh_token)
    with migrated.store.connection() as db:
        db.execute("UPDATE grants SET consumed=1 WHERE kind='refresh'")
    assert await migrated.load_refresh_token(client, token.refresh_token) is None
    assert await migrated.load_access_token(token.access_token) is None


@pytest.mark.asyncio
async def test_cached_refresh_cannot_bypass_grace_boundary_on_exchange(tmp_path, monkeypatch):
    clock = [100000]
    monkeypatch.setattr("oauth_provider.time.time", lambda: clock[0])
    p = provider(tmp_path)
    client, token = await issue(p)
    cached = await p.load_refresh_token(client, token.refresh_token)
    successor = await p.exchange_refresh_token(client, cached, ["read"])
    clock[0] += 60
    with pytest.raises(TokenError):
        await p.exchange_refresh_token(client, cached, ["read"])
    assert await p.load_access_token(successor.access_token) is None


@pytest.mark.asyncio
async def test_expiry_overrides_grace_and_unknown_token_does_not_revoke(tmp_path, monkeypatch):
    clock = [100000]
    monkeypatch.setattr("oauth_provider.time.time", lambda: clock[0])
    monkeypatch.setenv("MCP_OAUTH_REFRESH_TOKEN_TTL", "10")
    p = provider(tmp_path)
    client, original = await issue(p)
    assert await p.load_refresh_token(client, "fixture-unknown") is None
    grant = await p.load_refresh_token(client, original.refresh_token)
    clock[0] += 9
    successor = await p.exchange_refresh_token(client, grant, ["read"])
    clock[0] += 1
    assert await p.load_refresh_token(client, original.refresh_token) is None
    assert await p.load_refresh_token(client, successor.refresh_token)
    assert await p.load_access_token(successor.access_token)
