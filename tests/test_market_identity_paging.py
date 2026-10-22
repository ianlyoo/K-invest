"""Synthetic provider records, never live market/account data."""
from copy import deepcopy
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from toss_api import TossClient


def candle(day):
    return {"timestamp": f"{day}T14:00:00.000+09:00", "closePrice": "100", "currency": "USD"}


def client_for(payloads):
    client = object.__new__(TossClient)
    calls = []

    def get(path, params):
        calls.append((path, params))
        return {"result": deepcopy(payloads[len(calls) - 1])}

    client._get = get
    return client, calls


def test_default_candles_preserve_exact_provider_payload(monkeypatch):
    import server
    raw = {"candles": [candle("2026-10-02")], "nextBefore": "2025-12-15T14:00:00.000+09:00"}
    client, calls = client_for([raw])
    monkeypatch.setattr(server, "_get_toss", lambda: client)
    assert server.get_candles("SPY")["data"] == raw
    assert calls[0][1] == {"symbol": "SPY", "interval": "1d", "count": 100, "adjusted": "true"}


def test_candle_cursor_inclusive_boundary_and_stable_ascending_merge(monkeypatch):
    import server
    first_days = [date(2025, 12, 16) + timedelta(days=i) for i in range(200)]
    older_days = [date(2025, 11, 1) + timedelta(days=i) for i in range(45)]
    cursor = "2025-12-15T14:00:00.000+09:00"
    first = {"candles": [candle(d) for d in reversed(first_days)], "nextBefore": cursor}
    # nextBefore can name the next older bar: retain it, deduplicate locally.
    second = {"candles": [candle("2025-12-15"), *[candle(d) for d in reversed(older_days)], candle("2025-11-01")], "nextBefore": None}
    client, calls = client_for([first, second])
    monkeypatch.setattr(server, "_get_toss", lambda: client)
    a = server.get_candles("SPY", count=200, start_date="2025-11-01")["data"]
    b = server.get_candles("SPY", count=200, before=a["next_cursor"], start_date="2025-11-01")["data"]
    assert a["complete"] is False and b["complete"] is True
    assert a["limit"] == 200
    assert calls[1][1]["before"] == cursor
    merged = sorted(a["candles"] + b["candles"], key=lambda x: x["timestamp"])
    assert [x["timestamp"] for x in merged] == [candle(d)["timestamp"] for d in older_days + first_days]
    assert b["returned_range"]["start"] == candle("2025-11-01")["timestamp"]
    assert b["source_metadata"]["boundary_policy"] == "before_inclusive_local_filter; start_date/end_date_inclusive_local_label"


def test_candle_dates_filter_inclusive_and_preserve_unknown_adjustment():
    raw = {"candles": [candle("2025-11-03"), candle("2025-11-01"), candle("2025-10-31")], "nextBefore": "2025-10-30T14:00:00+09:00"}
    client, calls = client_for([raw])
    data = client.get_candles("QQQ", start_date="2025-11-01", end_date="2025-11-01")
    assert data["candles"] == [candle("2025-11-01")]
    assert data["complete"] is True
    assert data["next_cursor"] is None
    assert calls[0][1]["before"] == "2025-11-03T00:00:00+00:00"
    assert data["source_metadata"]["requested_adjustment"] is True
    assert data["source_metadata"]["applied_adjustment"] is None
    assert data["source_metadata"]["total_return_verified"] is None
    assert data["source_metadata"]["exchange_timezone"] is None


@pytest.mark.parametrize("kwargs", [{"start_date": "20251101"}, {"end_date": "bad"}, {"start_date": "2026-10-04", "end_date": "2025-11-01"}, {"before": "bad"}, {"count": 201, "start_date": "2025-11-01"}])
def test_candle_invalid_paging_rejected_before_provider(kwargs):
    client, calls = client_for([])
    with pytest.raises(ValueError):
        client.get_candles("SPY", **kwargs)
    assert calls == []


@pytest.mark.parametrize("symbol,market", [("9001A0", "kr"), ("9001B0", "kr"), ("005930", "kr"), ("AAPL", "us"), ("SYN", "us")])
def test_market_resolution_uses_master_not_digit_guess(monkeypatch, symbol, market):
    import server
    monkeypatch.setattr(server, "_get_toss", lambda: SimpleNamespace(get_stock_info=lambda s: [{"symbol": s, "marketCountry": market.upper()}]))
    assert server._resolve_market(symbol) == market


def test_ambiguous_master_requires_explicit_market(monkeypatch):
    import server
    monkeypatch.setattr(server, "_get_toss", lambda: SimpleNamespace(get_stock_info=lambda s: [{"symbol": s, "marketCountry": "KR"}, {"symbol": s, "marketCountry": "US"}]))
    out = server.compare_quotes("SYN")
    assert out["ok"] is False
    assert "market" in out["error"]["message"]
    assert server._resolve_market("SYN", market="us") == "us"


def test_stock_info_identity_and_no_name_based_leverage(monkeypatch):
    import server
    row = {"symbol": "SYN", "marketCountry": "US", "exchange": "NASDAQ", "currency": "USD", "quoteType": "ETF", "name": "Daily 2x leveraged SYN"}
    monkeypatch.setattr(server, "_get_toss", lambda: SimpleNamespace(get_stock_info=lambda s: [row]))
    result = server.get_stock_info("SYN")["data"][0]
    assert {k: result[k] for k in row} == row
    identity = result["instrument_metadata"]
    assert identity["instrument_id"] == "XNAS:SYN"
    assert identity["mic"] == "XNAS"
    assert identity["native_symbol"] == "SYN"
    assert identity["asset_class"] == "etf"
    assert identity["currency"] == "USD"
    for key in ("underlying", "leverage_target", "reset_frequency", "effective_date"):
        assert identity["product_relationship"][key] is None
    assert identity["product_relationship"]["status"] == "unavailable"
    assert identity["lookthrough"]["unknown_exposure"]["value"] is None
    assert identity["lookthrough"]["unknown_exposure"]["reason_codes"]


def test_risk_identity_uses_provider_country_for_alphanumeric_krx():
    from server import _position_from_item
    pos = _position_from_item("toss:fixture", {"symbol": "9001A0", "marketCountry": "KR", "marketValue": "100", "quantity": "1"})
    assert pos["currency"] == "KRW"
    assert pos["instrument_metadata"]["native_symbol"] == "9001A0"


def test_consolidated_identity_is_additive_without_new_provider_calls(monkeypatch):
    import server
    from portfolio_model import FxRates, Holding
    holdings = [Holding("kis", "kis:fixture:domestic", "9001B0", "KRW", 1, market="KR", exchange="KRX", market_value=100, cost_basis=100)]
    collection = SimpleNamespace(holdings=holdings, ok_sources=[], failed_sources={}, truncated_sources=[], health={}, fetched_at="2026-10-04")
    monkeypatch.setattr(server, "_get_collector", lambda: SimpleNamespace(collect_positions=lambda: collection, build_fx=lambda currencies: FxRates()))
    data = server.get_consolidated_holdings()["data"]
    assert data["positions"][0]["instrument_metadata"]["native_symbol"] == "9001B0"
    assert data["positions"][0]["market_value"] == 100


def test_yfinance_fallback_requires_matching_symbol_and_venue(monkeypatch):
    import server
    seen = []
    monkeypatch.setattr(server, "_get_toss", lambda: SimpleNamespace(get_stock_info=lambda s: []))

    def ticker(s):
        seen.append(s)
        return SimpleNamespace(info={"symbol": s, "exchange": "KOE"} if s == "9001A0.KQ" else {})

    monkeypatch.setattr(server.yf, "Ticker", ticker)
    assert server._resolve_market("9001A0") == "kr"
    assert seen == ["9001A0", "9001A0.KS", "9001A0.KQ"]
    monkeypatch.setattr(server.yf, "Ticker", lambda s: SimpleNamespace(info={"symbol": "OTHER", "exchange": "NASDAQ"}))
    assert server.compare_quotes("005930")["error"]["code"] == "MARKET_REQUIRED"


def test_snapshot_krx_routes_quotes_and_skips_sec(monkeypatch):
    import server
    seen = []
    monkeypatch.setattr(server, "_get_toss", lambda: SimpleNamespace(get_stock_info=lambda s: [{"symbol": s, "marketCountry": "KR"}], get_prices=lambda s: []))
    monkeypatch.setattr(server, "_get_kis", lambda: SimpleNamespace(get_domestic_quote=lambda s: seen.append(("kis", s)) or {}))
    monkeypatch.setattr(server, "_get_yf", lambda: SimpleNamespace(get_key_metrics=lambda s: seen.append(("yf", s)) or {}))
    monkeypatch.setattr(server, "_get_sec", lambda: pytest.fail("Korean instrument was routed to SEC"))
    assert server.get_stock_snapshot("9001B0")["ok"] is True
    assert seen == [("kis", "9001B0"), ("yf", "9001B0.KS")]

@pytest.mark.parametrize("raw,reason", [
    ({"candles": [candle("2026-10-02")]}, "CANDLE_CURSOR_UNAVAILABLE"),
    ({"candles": [{"timestamp": "bad"}], "nextBefore": None}, "CANDLE_TIMESTAMP_UNAVAILABLE"),
    ({"candles": [candle("2026-10-02"), {**candle("2026-10-02"), "closePrice": "200"}], "nextBefore": None}, "CONFLICTING_CANDLE_TIMESTAMP"),
])
def test_candle_quality_never_claims_completion(raw, reason):
    client, _ = client_for([raw])
    data = client.get_candles("SPY", start_date="2025-11-01")
    assert data["complete"] is None
    assert data["result_status"] == "partial"
    assert reason in data["reason_codes"]


def test_paging_timestamp_sort_uses_instant_not_offset_string():
    raw = {"candles": [{"timestamp": "2025-11-01T10:00:00+09:00"}, {"timestamp": "2025-11-01T02:00:00+00:00"}], "nextBefore": None}
    client, _ = client_for([raw])
    data = client.get_candles("SPY", start_date="2025-11-01")
    assert [x["timestamp"] for x in data["candles"]] == [x["timestamp"] for x in raw["candles"]]


def test_unknown_instrument_currency_is_not_guessed_from_symbol():
    from server import _position_from_item
    pos = _position_from_item("toss:fixture", {"symbol": "005930", "marketValue": "100"})
    assert pos["currency"] == "UNKNOWN"
    assert pos["instrument_metadata"]["currency"] is None
