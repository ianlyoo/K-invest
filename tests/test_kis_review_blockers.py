"""Review blockers: synthetic terminal pages and sanitized outcome history."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_kis_portfolio import FakeKIS, _overseas_row
from test_portfolio_sources import build

from health_status import HealthStatus
from kis_api import KISAPIError
from portfolio_engine import collection_result_status, consolidate_holdings
from portfolio_model import FxRates
from portfolio_sources import Collection
from toss_api import TossClient


@pytest.mark.parametrize("header", ["D", "E", "", "F", "M"])
def test_terminal_holding_replay_is_partial_without_doubling(header):
    body = {"rt_cd": "0", "output1": [_overseas_row("AAPL", "10", "USD")],
            "ctx_area_fk200": "next", "ctx_area_nk200": "next"}

    def respond(path, params, tr_id, tr_cont):
        if tr_id == "TTTS3012R" and params["OVRS_EXCG_CD"] == "NASD":
            return deepcopy(body), header if tr_cont else "F"
        return {"rt_cd": "0", "output1": [], "output2": []}, "D"

    c = build(toss_labels=[], kis=FakeKIS(respond))
    positions = c.collect_positions(["kis:overseas"])
    grouped = consolidate_holdings(positions.holdings, FxRates({"USD": 1300}))
    assert grouped["positions"][0]["quantity"] == 10
    assert positions.truncated_sources == ["kis:overseas"]
    assert positions.health["kis:overseas"]["status"] == "truncated"
    assert collection_result_status([positions], len(positions.holdings)) == "partial"


def test_terminal_profit_replay_through_collector_preserves_earlier_rows():
    body = {"output1": [{"pdno": "005930", "rlzt_pfls": "10000"}],
            "ctx_area_fk100": "next", "ctx_area_nk100": "next"}
    client = FakeKIS(lambda path, params, tr_id, tr_cont: (deepcopy(body), "D" if tr_cont else "F"))
    c = build(toss_labels=[], kis=client)
    payload, health = c._run("kis:domestic:profit", "kis", lambda: client.get_domestic_realized_pnl("20260401", "20260630"))
    # Broker profit is a client-only view: exercise its normal collector health plumbing.
    collection = Collection(health={"kis:domestic:profit": health})
    c._note_truncation(collection, "kis:domestic:profit", payload)
    assert payload["trades"] == body["output1"]
    assert sum(int(row["rlzt_pfls"]) for row in payload["trades"]) == 10000
    assert collection_result_status([collection], len(payload["trades"])) == "partial"
    assert collection.truncated_sources == ["kis:domestic:profit"]


@pytest.mark.parametrize("final_rows", [[{"n": 2}], [{"n": 1}, {"n": 2}], []])
def test_distinct_terminal_page_including_overlap_completes(final_rows):
    first = {"output1": [{"n": 1}], "ctx_area_fk100": "next", "ctx_area_nk100": "next"}
    last = {**first, "output1": final_rows}
    client = FakeKIS(lambda path, params, tr_id, tr_cont: (deepcopy(last if tr_cont else first), "D" if tr_cont else "F"))
    bodies, truncated = client._paginate("/synthetic", {}, "TR", ctx="100")
    assert bodies == [first, last]
    assert truncated is False


def test_empty_terminal_page_is_not_a_nonempty_replay():
    body = {"output1": [], "ctx_area_fk100": "next", "ctx_area_nk100": "next"}
    client = FakeKIS(lambda path, params, tr_id, tr_cont: (deepcopy(body), "D" if tr_cont else "F"))
    assert client._paginate("/synthetic", {}, "TR", ctx="100") == ([body, body], False)


@pytest.mark.parametrize("method,key,kwargs", [
    ("get_domestic_trade_history", "trades", {}),
    ("get_overseas_trade_history", "trades", {}),
    ("get_overseas_period_trans", "transactions", {}),
    ("get_overseas_realized_pnl", "trades", {"markets": ["NASD"]}),
])
def test_other_kis_paginated_paths_surface_terminal_replay(method, key, kwargs):
    output = "output" if method == "get_overseas_trade_history" else "output1"
    body = {output: [{"ord_dt": "20260401", "odno": "1", "pdno": "005930"}],
            "ctx_area_fk100": "next", "ctx_area_nk100": "next",
            "ctx_area_fk200": "next", "ctx_area_nk200": "next"}
    client = FakeKIS(lambda path, params, tr_id, tr_cont: (deepcopy(body), "D" if tr_cont else "F"))
    payload = getattr(client, method)("20260401", "20260630", **kwargs)
    assert len(payload[key]) == 1 and payload["truncated"] is True


def test_nonempty_summary_terminal_replay_is_truncated():
    body = {"output1": [], "output2": {"profit": "10000"},
            "ctx_area_fk100": "next", "ctx_area_nk100": "next"}
    client = FakeKIS(lambda path, params, tr_id, tr_cont: (deepcopy(body), "D" if tr_cont else "F"))
    assert client._paginate("/synthetic", {}, "TR", ctx="100") == ([body], True)


@pytest.mark.parametrize("method", ["iter_closed_orders", "get_trade_history"])
@pytest.mark.parametrize("native_id", [True, False])
def test_toss_repeated_cursor_is_truncated_without_dropping_unidentified_rows(method, native_id):
    row = {"symbol": "AAPL", "quantity": "10", "status": "FILLED"}
    if native_id:
        row["orderId"] = "synthetic-order"
    pages = iter([{"result": {"orders": [row], "hasNext": True, "nextCursor": "next"}},
                  {"result": {"orders": [row], "hasNext": True, "nextCursor": "next"}}])
    client = object.__new__(TossClient)
    client.get_orders = lambda **kwargs: deepcopy(next(pages))
    rows = getattr(client, method)()
    assert rows == ([row] if native_id and method == "iter_closed_orders" else [row, row])
    assert rows.truncated is True
    assert rows.truncation_reason == "paging_inconsistent"


@pytest.mark.parametrize("method", ["iter_closed_orders", "get_trade_history"])
def test_toss_identical_idless_orders_with_advancing_cursors_are_all_complete(method):
    row = {"symbol": "AAPL", "quantity": "10", "status": "FILLED"}
    last = {"symbol": "MSFT", "quantity": "1", "status": "FILLED"}
    pages = iter([{"result": {"orders": [row], "hasNext": True, "nextCursor": "c1"}},
                  {"result": {"orders": [row], "hasNext": True, "nextCursor": "c2"}},
                  {"result": {"orders": [last], "hasNext": False}}])
    requested = []
    client = object.__new__(TossClient)

    def get_orders(**kwargs):
        requested.append(kwargs.get("cursor"))
        return deepcopy(next(pages))

    client.get_orders = get_orders
    rows = getattr(client, method)()
    assert rows == [row, row, last]
    assert requested == [None, "c1", "c2"]
    assert rows.truncated is False


@pytest.mark.parametrize("method", ["iter_closed_orders", "get_trade_history"])
@pytest.mark.parametrize("next_cursor", ["c1", "c2"])
def test_toss_current_or_earlier_cursor_stops_after_preserving_rows(method, next_cursor):
    row = {"symbol": "AAPL", "quantity": "10", "status": "FILLED"}
    pages = iter([{"result": {"orders": [row], "hasNext": True, "nextCursor": "c1"}},
                  {"result": {"orders": [row], "hasNext": True, "nextCursor": "c2"}},
                  {"result": {"orders": [row], "hasNext": True, "nextCursor": next_cursor}}])
    client = object.__new__(TossClient)
    client.get_orders = lambda **kwargs: deepcopy(next(pages))
    rows = getattr(client, method)()
    assert rows == [row, row, row]
    assert rows.truncated is True
    assert rows.truncation_reason == "paging_inconsistent"


@pytest.mark.parametrize("method", ["iter_closed_orders", "get_trade_history"])
def test_toss_empty_terminal_page_completes(method):
    row = {"symbol": "AAPL", "quantity": "10", "status": "FILLED"}
    pages = iter([{"result": {"orders": [row], "hasNext": True, "nextCursor": "c1"}},
                  {"result": {"orders": [], "hasNext": False}}])
    client = object.__new__(TossClient)
    client.get_orders = lambda **kwargs: deepcopy(next(pages))
    rows = getattr(client, method)()
    assert rows == [row] and rows.truncated is False


@pytest.mark.parametrize("method", ["iter_closed_orders", "get_trade_history"])
def test_toss_distinct_overlapping_terminal_page_completes(method):
    a, b = {"orderId": "a", "status": "FILLED"}, {"orderId": "b", "status": "FILLED"}
    pages = iter([{"result": {"orders": [a], "hasNext": True, "nextCursor": "next"}},
                  {"result": {"orders": [a, b], "hasNext": False}}])
    client = object.__new__(TossClient)
    client.get_orders = lambda **kwargs: deepcopy(next(pages))
    rows = getattr(client, method)()
    assert rows == ([a, b] if method == "iter_closed_orders" else [a, a, b])
    assert rows.truncated is False


def test_raw_kis_duplicate_metadata_then_recovery_keeps_masked_history(monkeypatch):
    import server
    h = HealthStatus()
    monkeypatch.setattr(server, "_health_status", h)
    error = KISAPIError(200, {"msg_cd": "OPSQ0003", "msg1": "조회 오류 12345678 synthetic-secret"})
    failed = [True]

    def history(*args):
        if failed[0]:
            raise error
        return {"trades": [], "truncated": False}

    client = SimpleNamespace(supports_overseas=False, get_domestic_trade_history=history,
                             get_overseas_trade_history=history)
    registry = SimpleNamespace(client_for=lambda _: client, default_label=lambda: "primary")
    monkeypatch.setattr(server, "_get_kis_registry", lambda: registry)
    server.get_kis_trade_history(start_date="20260401", end_date="20260630")
    source = "kis:primary:domestic:history"
    h.record(source, status="provider_unavailable", error_code="provider_unavailable", complete=False)
    failed[0] = False
    server.get_kis_trade_history(start_date="20260401", end_date="20260630")
    rows = {r["source"]: r for r in h.report({"kis": True})["sources"]}
    for key in ("kis", source):
        assert rows[key]["status"] == "ok_empty"
        assert rows[key]["last_error"]["broker_diagnostic"]["msg_cd"] == "OPSQ0003"
        assert rows[key]["last_error_at"] and rows[key]["last_success_at"]
        assert "12345678" not in str(rows[key]) and "synthetic-secret" not in str(rows[key])


@pytest.mark.parametrize("coarse_error", [None, {"code": "provider_unavailable"}])
def test_code_only_duplicate_preserves_history_but_new_failure_after_success_replaces_it(monkeypatch, coarse_error):
    import health_status
    clock = [1]
    monkeypatch.setattr(health_status, "timestamp", lambda: str(clock[0]))
    h = HealthStatus()
    rich = {"code": "KIS_API_ERROR", "kind": "provider_unavailable", "broker_diagnostic": {"msg_cd": "OPSQ0003", "msg1": "조회 오류"}}
    h.record("kis:primary:history", status="provider_unavailable", error_code="provider_unavailable", error=rich)
    clock[0] = 2
    h.record("kis:primary:history", status="provider_unavailable", error_code="provider_unavailable", error=coarse_error)
    for row in h.report({"kis": True})["sources"]:
        assert row["last_error"] == rich and row["last_error_at"] == "1"
    h.record("kis:primary:history", status="ok")
    clock[0] = 3
    h.record("kis:primary:history", status="provider_unavailable", error_code="provider_unavailable")
    for row in h.report({"kis": True})["sources"]:
        assert row["last_error"] == {"code": "provider_unavailable"}
        assert row["last_error_at"] == "3"
