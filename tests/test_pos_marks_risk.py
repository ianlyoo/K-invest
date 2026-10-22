"""Canonical marks and consolidated concentration (synthetic numbers only)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import portfolio_engine as engine  # noqa: E402
from portfolio_model import FxRates, Holding  # noqa: E402
from portfolio_sources import Collection  # noqa: E402
from toss_api import TossClient  # noqa: E402


def _holdings():
    return [
        Holding("kis", "kis:domestic", "000001", "KRW", 250, market="KR", last_price=1000.0,
                avg_price=900.0, cost_basis=225000.0, market_value=250000.0),
        Holding("toss", "toss:secondary", "000001", "KRW", 30, market="KR", last_price=1010.0,
                avg_price=950.0, cost_basis=28500.0, market_value=30300.0),
        Holding("kis", "kis:pension:domestic", "000002", "KRW", 40, market="KR", last_price=500.0,
                avg_price=400.0, cost_basis=16000.0, market_value=20000.0),
        Holding("kis", "kis:overseas", "SYNA", "USD", 10, market="US", last_price=20.0,
                avg_price=15.0, cost_basis=150.0, market_value=200.0),
        Holding("toss", "toss:primary", "SYNA", "USD", 5.5, market="US", last_price=20.1,
                avg_price=16.0, cost_basis=88.0, market_value=110.55),
    ]


def _fx():
    return FxRates(rates={"USD": 1000.0})


class _PriceClient:
    """The real TossClient.get_mark_quotes over a mocked prices response."""

    def __init__(self, rows=None, envelope=None):
        self.rows, self.envelope, self.calls = rows or [], envelope, []

    def get_prices_evidence(self, symbols):
        self.calls.append(list(symbols))
        if self.envelope is not None:
            return self.envelope
        return {"result": [row for row in self.rows if row["symbol"] in symbols]}

    def get_mark_quotes(self, requested):
        return TossClient.get_mark_quotes(self, requested)


_ROWS = [
    {"symbol": "000001", "lastPrice": "1010", "currency": "KRW", "timestamp": "2026-10-21T19:59:59.000+09:00"},
    {"symbol": "000002", "lastPrice": "505", "currency": "KRW", "timestamp": "2026-10-21T15:30:00.000+09:00"},
    {"symbol": "SYNA", "lastPrice": "20.2", "currency": "USD", "timestamp": "2026-10-21T23:59:46.000+09:00"},
]


def _requested():
    return {h.symbol: h.currency for h in _holdings()}


def test_timed_quote_becomes_the_one_mark_for_every_account():
    quotes = _PriceClient(_ROWS).get_mark_quotes(_requested())
    data = engine.consolidate_holdings(_holdings(), _fx(), quotes=quotes)
    rows = {p["symbol"]: p for p in data["positions"]}
    row = rows["000001"]
    assert row["mark"] == {"price": 1010.0, "currency": "KRW", "source": "toss:prices", "session": "nxt_after",
                           "priced_at": "2026-10-21T19:59:59.000+09:00", "status": "timed", "reason_codes": []}
    assert row["market_value"] == pytest.approx(1010.0 * 280)
    assert row["broker_reported_market_value"] == pytest.approx(250000.0 + 30300.0)
    assert row["market_value_basis"] == "canonical_mark"
    assert [a["market_value"] for a in row["accounts"]] == [pytest.approx(1010.0 * 250), pytest.approx(1010.0 * 30)]
    assert [a["broker_reported_market_value"] for a in row["accounts"]] == [250000.0, 30300.0]
    assert row["unrealized_pnl"] == pytest.approx(1010.0 * 280 - 253500.0)
    assert rows["000002"]["mark"]["session"] == "krx_regular"
    assert rows["SYNA"]["mark"]["session"] == "us_regular"
    for p in data["positions"]:
        assert p["last_price"] * p["quantity"] == pytest.approx(p["market_value"])
    assert data["mark_status_counts"] == {"timed": 3}
    assert data["totals"]["broker_reported_market_value_krw"] == pytest.approx(250000 + 30300 + 20000 + 310550)


def test_without_a_timed_quote_the_mark_is_a_labelled_broker_price():
    quotes = _PriceClient(envelope={"result": [], "partial": True}).get_mark_quotes(_requested())
    data = engine.consolidate_holdings(_holdings(), _fx(), quotes=quotes)
    row = next(p for p in data["positions"] if p["symbol"] == "000001")
    mark = row["mark"]
    assert mark["status"] == "unknown_time" and mark["priced_at"] is None and mark["session"] is None
    assert mark["price"] == 1000.0 and mark["source"] == "broker:kis:domestic"  # the largest quantity
    assert mark["broker_price_spread_bp"] == 100.0
    assert {"INVALID_SOURCE_RESPONSE", "UNKNOWN_QUOTE_TIME", "BROKER_PRICES_DIFFER"} <= set(mark["reason_codes"])
    assert row["market_value"] == pytest.approx(1000.0 * 280)
    assert data["mark_status_counts"] == {"unknown_time": 3}


def test_quote_without_time_or_in_other_currency_is_never_a_timed_mark():
    rows = [dict(_ROWS[0], timestamp=None), dict(_ROWS[2], currency="KRW")]
    quotes = _PriceClient(rows).get_mark_quotes(_requested())
    data = engine.consolidate_holdings(_holdings(), _fx(), quotes=quotes)
    marks = {p["symbol"]: p["mark"] for p in data["positions"]}
    assert marks["000001"]["status"] == "unknown_time" and "UNKNOWN_QUOTE_TIME" in marks["000001"]["reason_codes"]
    assert marks["SYNA"]["status"] == "unknown_time" and "QUOTE_CURRENCY_MISMATCH" in marks["SYNA"]["reason_codes"]
    assert marks["000002"]["status"] == "unknown_time" and "MISSING_TOSS_QUOTE" in marks["000002"]["reason_codes"]


class _ScriptedPrices(_PriceClient):
    """One scripted prices response per requested chunk."""

    def __init__(self, by_chunk):
        super().__init__()
        self.by_chunk = by_chunk

    def get_prices_evidence(self, symbols):
        self.calls.append(list(symbols))
        return {"result": self.by_chunk[tuple(symbols)]}


def _chunked_quotes(first_chunk):
    client = _ScriptedPrices({("000001", "000002"): first_chunk, ("SYNA",): [_ROWS[2]]})
    quotes = TossClient.get_mark_quotes(client, _requested(), chunk=2)
    assert client.calls == [["000001", "000002"], ["SYNA"]]
    assert quotes["SYNA"]["price"] == 20.2  # a clean response in another chunk is kept
    return quotes


@pytest.mark.parametrize("first_chunk,codes", [
    # an unrequested symbol: a response-level code, no per-symbol one
    (_ROWS[:2] + [dict(_ROWS[0], symbol="000009")], ["CURRENCY_BINDING_MISMATCH"]),
    # ... also when a requested row carries the same code for itself (another currency)
    ([_ROWS[0], dict(_ROWS[1], currency="USD"), dict(_ROWS[0], symbol="000009")], ["CURRENCY_BINDING_MISMATCH"]),
    # a row bound to no symbol at all
    (_ROWS[:2] + [dict(_ROWS[0], symbol=" ")], ["INVALID_SOURCE_RESPONSE"]),
])
def test_a_response_level_problem_gives_no_price_for_its_whole_chunk(first_chunk, codes):
    quotes = _chunked_quotes(first_chunk)
    assert quotes["000001"] == {"reason_codes": codes}
    assert "price" not in quotes["000002"] and quotes["000002"]["reason_codes"]
    marks = {p["symbol"]: p["mark"] for p in
             engine.consolidate_holdings(_holdings(), _fx(), quotes=quotes)["positions"]}
    assert marks["000001"]["status"] == "unknown_time" and marks["000001"]["source"] == "broker:kis:domestic"
    assert set(codes) <= set(marks["000001"]["reason_codes"])
    assert marks["SYNA"]["status"] == "timed"


@pytest.mark.parametrize("first_chunk,codes", [
    ([_ROWS[0], dict(_ROWS[1], lastPrice="0")], ["INVALID_QUOTE_PRICE"]),
    ([_ROWS[0], dict(_ROWS[1], timestamp=None)], ["UNKNOWN_QUOTE_TIME"]),
    ([_ROWS[0]], ["MISSING_TOSS_QUOTE"]),
    ([_ROWS[0], _ROWS[1], _ROWS[1]], ["DUPLICATE_QUOTE"]),
])
def test_a_symbol_level_problem_rejects_only_that_symbol_and_the_basis_says_mixed(first_chunk, codes):
    import daily_nav
    import ledger_returns

    quotes = _chunked_quotes(first_chunk)
    assert quotes["000001"]["price"] == 1010.0 and quotes["000001"]["source"] == "toss:prices"
    assert quotes["000002"] == {"reason_codes": codes}
    summary = engine.summarize_portfolio(_holdings(), [], _fx(), quotes=quotes)
    assert summary["mark_status_counts"] == {"timed": 2, "unknown_time": 1}
    assert summary["mark_basis"] == "mixed_marks"
    basis = daily_nav.crypto_nav(None, summary)["consolidated_nav_krw"]["basis"]
    assert basis == daily_nav.BROKER_MARKS_CONSOLIDATED_NAV_BASIS and basis in ledger_returns.CONSOLIDATED_NAV_BASES


def _jpy(holdings):
    return [*holdings, Holding("kis", "kis:overseas", "7203", "JPY", 100, market="JP", last_price=3000.0,
                               avg_price=2500.0, cost_basis=250000.0, market_value=300000.0)]


@pytest.mark.parametrize("holdings,fx,with_quotes,expected", [
    (_holdings, {"USD": 1000.0}, True, "canonical_marks"),
    # the live shape: a held market Toss does not quote stays at a broker price
    (lambda: _jpy(_holdings()), {"USD": 1000.0, "JPY": 9.0}, True, "mixed_marks"),
    # a broker-priced row without FX is not in the total, so it does not change the label
    (lambda: _jpy(_holdings()), {"USD": 1000.0}, True, "canonical_marks"),
    (_holdings, {"USD": 1000.0}, False, "broker_marks"),
])
def test_mark_basis_is_canonical_only_if_every_valued_row_used_a_timed_quote(holdings, fx, with_quotes, expected):
    import daily_nav

    held = holdings()
    quotes = _PriceClient(_ROWS).get_mark_quotes({h.symbol: h.currency for h in held}) if with_quotes else None
    summary = engine.summarize_portfolio(held, [], FxRates(rates=fx), quotes=quotes)
    assert summary["mark_basis"] == expected
    assert daily_nav.crypto_nav(None, summary)["consolidated_nav_krw"]["basis"] == (
        daily_nav.CONSOLIDATED_NAV_BASIS if expected == "canonical_marks"
        else daily_nav.BROKER_MARKS_CONSOLIDATED_NAV_BASIS)


def test_unsupported_market_is_not_requested_from_toss():
    client = _PriceClient(_ROWS)
    quotes = client.get_mark_quotes({**_requested(), "7203": "JPY"})
    assert quotes["7203"] == {"reason_codes": ["UNSUPPORTED_QUOTE_MARKET"]}
    assert all("7203" not in call for call in client.calls)


@pytest.mark.parametrize("currency,stamp,session", [
    ("KRW", "2026-10-21T08:30:00+09:00", "nxt_pre"),
    ("KRW", "2026-10-21T10:00:00+09:00", "krx_regular"),
    ("KRW", "2026-10-21T15:30:00+09:00", "krx_regular"),
    ("KRW", "2026-10-21T19:59:59+09:00", "nxt_after"),
    ("KRW", "2026-10-24T10:00:00+09:00", "closed"),
    ("USD", "2026-10-21T08:00:00-04:00", "us_pre"),
    ("USD", "2026-10-21T16:00:00-04:00", "us_regular"),
    ("USD", "2026-10-21T17:00:00-04:00", "us_post"),
    ("USD", "2026-10-21T22:00:00-04:00", "us_overnight"),
    ("USD", "2026-10-25T21:00:00-04:00", "us_overnight"),
    ("USD", "2026-10-23T21:00:00-04:00", "closed"),
    ("USD", None, None),
])
def test_session_is_read_from_the_price_time(currency, stamp, session):
    assert engine.mark_session(currency, stamp) == session


def test_summary_stock_value_and_accounts_use_the_same_marks():
    quotes = _PriceClient(_ROWS).get_mark_quotes(_requested())
    summary = engine.summarize_portfolio(_holdings(), [], _fx(), quotes=quotes)
    consolidated = engine.consolidate_holdings(_holdings(), _fx(), quotes=quotes)
    assert summary["stock_value_krw"] == pytest.approx(consolidated["totals"]["market_value_krw"])
    assert sum(a["market_value_krw"] for a in summary["by_account"].values()) == pytest.approx(summary["stock_value_krw"])
    assert summary["broker_reported_stock_value_krw"] == pytest.approx(250000 + 30300 + 20000 + 310550)
    assert summary["mark_policy"] == engine.MARK_POLICY  # stated once, in full


class _Collector:
    def __init__(self, holdings, fx):
        self.holdings, self.fx = holdings, fx

    def collect_positions(self):
        health = {name: {"account": name, "status": "ok", "last_error": None}
                  for name in sorted({h.account for h in self.holdings})}
        return Collection(holdings=list(self.holdings), health=health, fetched_at="2026-10-21T15:00:00+00:00")

    def build_fx(self, currencies):
        return self.fx


def _risk(monkeypatch, holdings, fx, client, detail="full"):
    import server

    monkeypatch.setattr(server, "_get_collector", lambda: _Collector(holdings, fx))
    monkeypatch.setattr(server, "_get_toss", lambda *a, **k: client)
    risk = server.get_portfolio_risk(detail_level=detail)
    held = server.get_consolidated_holdings(detail_level="full")
    return risk, held


def test_risk_weights_equal_consolidated_weights_across_every_account(monkeypatch):
    risk, held = _risk(monkeypatch, _holdings(), _fx(), _PriceClient(_ROWS))
    assert risk["ok"] and held["ok"]
    data = risk["data"]
    consolidated = {p["symbol"]: p for p in held["data"]["positions"]}
    assert data["position_count"] == held["data"]["position_count"] == 3
    for row in data["top_positions"]:
        assert row["weight_pct"] == consolidated[row["symbol"]]["weight_pct"]
        assert row["quantity"] == consolidated[row["symbol"]]["quantity"]
    assert next(r for r in data["top_positions"] if r["symbol"] == "000001")["quantity"] == 280
    assert set(next(r for r in data["top_positions"] if r["symbol"] == "SYNA")["sources"]) == {"kis:overseas", "toss:primary"}
    values = {"000001": 1010.0 * 280, "000002": 505.0 * 40, "SYNA": 20.2 * 15.5 * 1000.0}
    total = sum(values.values())
    conc = data["concentration"]
    assert conc["hhi"] == round(sum((v / total) ** 2 for v in values.values()), 4)
    assert conc["top1_symbol"] == "SYNA"
    assert conc["top1_weight_pct"] == round(values["SYNA"] / total * 100, 4)
    assert conc["top5_weight_pct"] == pytest.approx(100.0, abs=1e-3)
    usd_weight = values["SYNA"] / total
    assert conc["currency_hhi"] == round(usd_weight ** 2 + (1 - usd_weight) ** 2, 4)
    assert data["by_currency"]["KRW"]["top1_symbol"] == "000001"
    assert data["methodology"]["fx_conversion"] == "krw_spot"
    assert data["source_errors"] == {}


def test_risk_counts_unconverted_rows_without_weighting_them(monkeypatch):
    risk, _ = _risk(monkeypatch, _holdings(), FxRates(rates={}, missing={"USD"}), _PriceClient(_ROWS), "summary")
    data = risk["data"]
    assert data["position_count"] == 3 and data["valued_position_count"] == 2
    assert {r["symbol"] for r in data["top_positions"]} == {"000001", "000002"}
    # The valued KRW rows alone are not the portfolio's concentration, nor "low".
    conc = data["concentration"]
    assert conc["reason_codes"] == ["MISSING_FX_RATE", "UNVALUED_POSITIONS"]
    assert conc["status"] == "unavailable" and conc["unvalued_position_count"] == 1
    assert {key: conc[key] for key in ("top1_symbol", "top1_weight_pct", "top5_weight_pct", "hhi",
                                       "effective_positions", "currency_hhi", "risk_level")} == dict.fromkeys(
        ("top1_symbol", "top1_weight_pct", "top5_weight_pct", "hhi", "effective_positions", "currency_hhi",
         "risk_level"))
    assert "total_unconverted_value" not in data and data["fx"]["missing_currencies"] == ["USD"]


def test_risk_without_quote_source_still_uses_one_labelled_mark(monkeypatch):
    def unavailable(*a, **k):
        raise RuntimeError("toss down")

    import server

    monkeypatch.setattr(server, "_get_collector", lambda: _Collector(_holdings(), _fx()))
    monkeypatch.setattr(server, "_get_toss", unavailable)
    data = server.get_portfolio_risk(detail_level="full")["data"]
    row = next(r for r in data["top_positions"] if r["symbol"] == "000001")
    assert row["mark"]["status"] == "unknown_time"
    assert "QUOTE_SOURCE_UNAVAILABLE" in row["mark"]["reason_codes"]
    assert row["market_value"] == pytest.approx(1000.0 * 280)


def test_sessions_namespace_is_documented():
    assert SimpleNamespace(**engine.MARK_POLICY).name == "latest_timed_quote_v1"
