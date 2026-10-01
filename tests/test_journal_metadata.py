from __future__ import annotations

from types import SimpleNamespace

import pytest

from journal_metadata import annotate_rows, identity
from portfolio_model import kis_overseas_transactions, toss_transactions
from toss_api import HistoryRows, TossClient


def test_identity_namespaces_stable_across_updates_and_tool_shapes():
    raw = {"orderId": "fixture-order", "orderedAt": "2026-09-30T23:59:00Z", "currency": "USD",
        "symbol": "MU", "side": "BUY", "status": "FILLED", "execution": {"filledQuantity": 1,
        "averageFilledPrice": 100, "filledAt": "2026-10-01T00:01:00Z"}}
    first = annotate_rows([raw], "toss", "toss:stable-account")[0]
    raw["execution"]["filledQuantity"] = 2
    raw["execution"]["averageFilledPrice"] = 101
    second = annotate_rows([raw], "toss", "toss:stable-account")[0]
    assert first["execution_id"] == second["execution_id"]
    tx = toss_transactions([raw], "toss:stable-account")[0]
    assert tx.execution_id == first["execution_id"]
    for provider, account, market in [("kis", "toss:stable-account", "US"), ("toss", "toss:other", "US"), ("toss", "toss:stable-account", "KR")]:
        assert identity(provider, account, market, "fixture-order", "2026-09-30")["execution_id"] != first["execution_id"]
    assert first["identity_quality"] == "order_aggregate"


def test_kis_compact_date_and_scope_alias_match():
    raw = {"odno": "fixture-order", "ord_dt": "20260930"}
    a = annotate_rows([raw], "kis", "kis:primary", "KR")[0]
    b = identity("kis", "kis:domestic", "KR", "fixture-order", "2026-09-30")
    assert a["execution_id"] == b["execution_id"]


def test_no_native_id_not_falsely_unique():
    rows = annotate_rows([{"symbol": "MU"}, {"symbol": "MU"}], "toss", "toss:primary")
    assert all(x["execution_id"] is None and x["identity_quality"] == "unavailable" for x in rows)
    payload = {"transactions": [{"trad_dt": "20260930", "pdno": "MU", "sll_buy_dvsn_cd": "02", "ccld_qty": "1"}]}
    assert kis_overseas_transactions(payload)[0].execution_id is None


class SyntheticToss(TossClient):
    def __init__(self, pages):
        self.pages = iter(pages)
    def get_orders(self, **kwargs):
        return {"result": next(self.pages)}


def test_toss_cap_inconsistent_cursor_and_complete_empty():
    page = {"orders": [{"orderId": "a", "status": "FILLED"}, {"orderId": "b", "status": "FILLED"}], "hasNext": False}
    result = SyntheticToss([page]).get_trade_history(limit=1)
    assert len(result) == 1 and result.truncated and result.truncation_reason == "row_limit"
    result = SyntheticToss([{"orders": [], "hasNext": False}]).iter_closed_orders()
    assert not result and result.complete
    result = SyntheticToss([{"orders": [{"orderId": "a"}], "hasNext": True}]).iter_closed_orders()
    assert result.truncated and result.truncation_reason == "paging_inconsistent"
    result = SyntheticToss([{"orders": [{"orderId": "a"}], "hasNext": True, "nextCursor": "fixture"}]).iter_closed_orders(max_orders=1)
    assert result.truncated


def test_toss_tool_complete_empty_vs_unavailable_and_cap(monkeypatch):
    import server
    from health_status import HealthStatus
    from toss_api import TossAPIError
    rows = HistoryRows()
    client = SimpleNamespace(get_trade_history=lambda **kwargs: rows, journal_account_key="toss:stable-account")
    registry = SimpleNamespace(account_labels=lambda: ["primary"], client_for=lambda label: client)
    monkeypatch.setattr(server, "_get_toss_registry", lambda: registry)
    monkeypatch.setattr(server, "_health_status", HealthStatus())
    result = server.get_toss_trade_history()
    assert result["data"] == [] and result["meta"]["sources"][0]["status"] == "ok_empty"
    assert result["meta"]["complete"]
    rows = HistoryRows([{"orderId": "a", "orderedAt": "2026-09-30", "currency": "USD"}], truncated=True)
    result = server.get_toss_trade_history()
    assert not result["meta"]["complete"] and result["data"][0]["execution_id"]
    def failed(**kwargs):
        raise TossAPIError(401, {"secret": "fixture-sensitive"})
    client.get_trade_history = failed
    result = server.get_toss_trade_history()
    assert not result["ok"] and result["error"]["kind"] == "broker_auth_failed"
    assert result["meta"]["sources"][0]["result_count"] is None
    assert "fixture-sensitive" not in str(result)


def test_kis_tool_partial_retains_domestic_and_marks_overseas_failure(monkeypatch):
    import server
    from kis_api import KISAPIError
    def fail(*args):
        raise KISAPIError(503, {})
    client = SimpleNamespace(supports_overseas=True, journal_account_key="kis:stable-account",
        get_domestic_trade_history=lambda *args: {"trades": [{"odno": "a", "ord_dt": "20260930"}], "truncated": False},
        get_overseas_trade_history=fail)
    registry = SimpleNamespace(default_label=lambda: "primary", client_for=lambda label: client)
    monkeypatch.setattr(server, "_get_kis_registry", lambda: registry)
    result = server.get_kis_trade_history("20260901", "20260930")
    assert not result["meta"]["complete"]
    assert result["data"]["domestic"]["trades"][0]["execution_id"]
    assert result["data"]["overseas"]["trades"] is None
    assert result["meta"]["sources"][1]["result_count"] is None
    assert result["meta"]["sources"][0]["as_of"] is None


def test_explicit_account_alias_survives_label_change(tmp_path):
    import json

    from toss_accounts import TossAccountRegistry
    def registry(label):
        f = tmp_path / "fixture.json"
        f.write_text(json.dumps({"label": label, "account_id": "stable-account", "client_id": "fixture-id", "client_secret": "fixture-secret"}))
        return TossAccountRegistry.from_sources(env={}, creds_file=f, cache_dir=tmp_path).client_for(label)
    a, b = registry("original"), registry("renamed")
    assert a.journal_account_key == b.journal_account_key
    a.close()
    b.close()


def test_kis_all_account_init_failure_preserves_other_account(monkeypatch):
    import server
    from kinvest_common import NotConfiguredError
    good = SimpleNamespace(supports_overseas=False,
        get_domestic_trade_history=lambda *args: {"trades": [], "truncated": False},
        get_overseas_trade_history=lambda *args: pytest.fail("Unsupported market was queried"))
    def client_for(label):
        if label == "bad":
            raise NotConfiguredError("kis", "fixture-sensitive")
        return good
    registry = SimpleNamespace(account_labels=lambda: ["good", "bad"], client_for=client_for, default_label=lambda: "good")
    monkeypatch.setattr(server, "_get_kis_registry", lambda: registry)
    result = server.get_kis_trade_history("20260901", "20260930", "all")
    assert result["data"]["accounts"][0]["data"]["domestic"]["trades"] == []
    assert result["data"]["accounts"][1]["error"]["kind"] == "not_configured"
    assert not result["meta"]["complete"]
    assert "fixture-sensitive" not in str(result)
