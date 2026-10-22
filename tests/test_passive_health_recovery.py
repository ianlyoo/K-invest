"""Outcome observations and historical errors, with synthetic clients only."""
from types import SimpleNamespace

import pytest
from test_kis_portfolio import FakeResponse, HttpKIS
from test_portfolio_sources import FakeKIS, FakeKISAccount, FakeToss

from health_status import HealthStatus
from kis_api import KISAPIError
from portfolio_model import FxRates
from toss_api import TossAPIError, TossClient


def test_success_replaces_current_error_but_keeps_timestamped_history(monkeypatch):
    import health_status
    clock = [1000.0]
    monkeypatch.setattr(health_status.time, "time", lambda: clock[0])
    monkeypatch.setattr(health_status, "timestamp", lambda: str(clock[0]))
    h = HealthStatus()
    h.record("toss", status="broker_auth_failed", error_code="broker_auth_failed")
    clock[0] += 1
    h.record("toss", status="ok", complete=True)
    row = h.report({"toss": True})["sources"][0]
    assert row["status"] == "ok" and row["error_code"] is None
    assert row["last_success_at"] == "1001.0"
    assert row["last_error_at"] == "1000.0"
    assert row["last_error"]["code"] == "broker_auth_failed"


def test_scoped_outcome_updates_provider_latest_call_without_hiding_other_source_error():
    h = HealthStatus()
    h.record("kis:pension:domestic:trades", status="provider_unavailable", error_code="provider_unavailable")
    h.record("kis:domestic:trades", status="ok", complete=True)
    rows = {x["source"]: x for x in h.report({"kis": True})["sources"]}
    assert rows["kis"]["status"] == "ok"
    assert rows["kis"]["last_success_at"]
    assert rows["kis"]["latest_source"] == "kis:domestic:trades"
    assert rows["kis"]["observation_scope"] == "latest_provider_call"
    assert rows["kis:pension:domestic:trades"]["status"] == "provider_unavailable"
    assert rows["kis"]["last_error"]["code"] == "provider_unavailable"


def test_realized_pnl_five_source_success_recovers_passive_health(monkeypatch):
    import server
    monkeypatch.setattr(server, "_health_status", HealthStatus())
    monkeypatch.setattr(server, "_collector", None)
    monkeypatch.setattr(server, "_oauth_provider", None)
    monkeypatch.setattr(server, "_get_toss_registry", lambda: SimpleNamespace(account_labels=lambda: ["primary", "secondary"]))
    clients = {"primary": FakeToss(), "secondary": FakeToss()}
    monkeypatch.setattr(server, "_get_toss", lambda label="primary": clients[label])
    monkeypatch.setattr(server, "_get_kis_registry", lambda: SimpleNamespace(accounts=lambda: [FakeKISAccount(), FakeKISAccount("pension", False)]))
    monkeypatch.setattr(server, "_get_kis", lambda label="": FakeKIS())
    c = server._get_collector()
    monkeypatch.setattr(c, "build_fx", lambda _: FxRates({"USD": 1300, "JPY": 9}))
    c._record_failure("toss:secondary:orders", "toss", TossAPIError(401, {"secret": "synthetic-private"}))
    result = server.get_realized_pnl(start_date="2026-02-01", end_date="2026-02-28", reconcile=False)
    assert result["ok"]
    sources = result["data"]["health"]
    assert len(sources) == 5 and all(x["status"] == "ok" for x in sources.values())
    assert sources["toss:secondary:orders"]["last_error"]["kind"] == "broker_auth_failed"
    assert sources["toss:secondary:orders"]["last_error_at"]
    rows = {x["source"]: x for x in server.get_service_health()["data"]["sources"]}
    for provider in ("toss", "kis"):
        assert rows[provider]["status"] == "ok"
        assert rows[provider]["last_success_at"]
        assert rows[provider]["error_code"] is None
    assert rows["toss"]["last_error"]["kind"] == "broker_auth_failed"
    assert "synthetic-private" not in str(rows)


def test_direct_kis_success_records_provider_health(monkeypatch):
    import server
    monkeypatch.setattr(server, "_health_status", HealthStatus())
    client = HttpKIS([FakeResponse({"rt_cd": "0", "output1": []})])
    monkeypatch.setattr(client, "_throttle", lambda: None)
    assert client._get("/synthetic", {}) == {"rt_cd": "0", "output1": []}
    row = next(x for x in server._health_status.report({"kis": True})["sources"] if x["source"] == "kis")
    assert row["status"] == "ok" and row["last_success_at"]


def test_direct_toss_failure_then_success_changes_latest_outcome(monkeypatch):
    import server
    monkeypatch.setattr(server, "_health_status", HealthStatus())
    client = object.__new__(TossClient)

    def failed(*args):
        raise TossAPIError(401, {"secret": "synthetic-private"})

    monkeypatch.setattr(client, "_get_unlocked", failed)
    with pytest.raises(TossAPIError):
        client._get("/synthetic")
    monkeypatch.setattr(client, "_get_unlocked", lambda *args: {"result": [1]})
    assert client._get("/synthetic") == {"result": [1]}
    row = server._health_status.report({"toss": True})["sources"][0]
    assert row["status"] == "ok" and row["last_success_at"]
    assert row["last_error_at"] and row["last_error"]["kind"] == "broker_auth_failed"
    assert "synthetic-private" not in str(row)


def test_direct_kis_recovery_retains_only_masked_broker_diagnostic(monkeypatch):
    import server
    monkeypatch.setattr(server, "_health_status", HealthStatus())
    client = HttpKIS([
        FakeResponse({"rt_cd": "1", "msg_cd": "APBK1234", "msg1": "계좌 12345678 synthetic-secret 조회 실패"}),
        FakeResponse({"rt_cd": "0", "output1": []}),
    ])
    monkeypatch.setattr(client, "_throttle", lambda: None)
    with pytest.raises(KISAPIError):
        client._get("/synthetic", {})
    client._get("/synthetic", {})
    row = server._health_status.report({"kis": True})["sources"][0]
    assert row["status"] == "ok" and row["error_code"] is None
    assert row["last_error_at"]
    assert row["last_error"]["broker_diagnostic"]["msg_cd"] == "APBK1234"
    assert "12345678" not in str(row) and "synthetic-secret" not in str(row)


def test_observer_failure_does_not_change_raw_provider_response(monkeypatch):
    import health_status

    def failed_observer(*args):
        raise RuntimeError("synthetic observation failure")

    monkeypatch.setattr(health_status, "_provider_observer", failed_observer)
    client = object.__new__(TossClient)
    payload = {"result": [1]}
    monkeypatch.setattr(client, "_get_unlocked", lambda *args: payload)
    assert client._get("/synthetic") is payload
