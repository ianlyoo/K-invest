"""Each merged tool's section equals the old tool's answer for the same inputs.

Synthetic numbers only. The old tools and the new ones run the same server functions
over one stub collector / mocked provider clients; the new tool is called directly.
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import response_shape  # noqa: E402
import server  # noqa: E402
import tools_v2  # noqa: E402
from portfolio_model import (  # noqa: E402
    CashBalance,
    FxRates,
    Holding,
    Transaction,
    TxType,
)
from portfolio_sources import Collection  # noqa: E402
from portfolio_store import NavStore  # noqa: E402

FETCHED = "2026-10-21T09:10:05+00:00"
RATES = {"USD": 1000.0, "JPY": 9.0}
HOLDINGS = [
    Holding("kis", "kis:domestic", "000001", "KRW", 10, market="KR", last_price=1000.0,
            avg_price=900.0, cost_basis=9000.0, market_value=10000.0),
    Holding("toss", "toss:primary", "SYNA", "USD", 2, market="US", last_price=20.0,
            avg_price=15.0, cost_basis=30.0, market_value=40.0),
    Holding("kis", "kis:overseas", "SYNA", "USD", 3, market="US", last_price=20.0,
            avg_price=18.0, cost_basis=54.0, market_value=60.0),
    Holding("kis", "kis:pension:domestic", "000002", "KRW", 4, market="KR", last_price=500.0,
            avg_price=400.0, cost_basis=1600.0, market_value=2000.0),
]
CASH = [CashBalance("toss", "toss:primary:usd", "USD", 5.0), CashBalance("kis", "kis:domestic", "KRW", 3000.0)]
ACCOUNTS = ("toss:primary", "kis:domestic", "kis:overseas", "kis:pension:domestic")


def _tx(tx_id, account, kind, day, symbol, currency, quantity, price):
    gross = quantity * price
    return Transaction(provider=account.split(":")[0], account=account, tx_id=tx_id, tx_type=kind, trade_date=day,
                       currency=currency, settle_date=day, symbol=symbol, quantity=quantity, price=price,
                       gross_amount=gross, net_amount=-gross if kind is TxType.BUY else gross,
                       fx_rate=None if currency == "KRW" else 1000.0, fee_status="verified_zero",
                       tax_status="verified_zero")


TRANSACTIONS = [
    _tx("t1", "kis:domestic", TxType.BUY, "2026-02-02", "000001", "KRW", 12, 900.0),
    _tx("t2", "kis:domestic", TxType.SELL, "2026-03-03", "000001", "KRW", 2, 950.0),
    _tx("t3", "toss:primary", TxType.BUY, "2026-04-04", "SYNA", "USD", 2, 15.0),
    _tx("t4", "kis:overseas", TxType.BUY, "2026-05-05", "SYNA", "USD", 3, 18.0),
    _tx("t5", "kis:pension:domestic", TxType.BUY, "2026-06-06", "000002", "KRW", 4, 400.0),
    _tx("t6", "kis:domestic", TxType.BUY, "2026-06-06", "000001", "KRW", 0, 900.0),
]


class StubCollector:
    """Holdings, cash, ledger and FX from fixed synthetic rows; counts every read."""

    def __init__(self):
        self.calls = Counter()

    def collect_positions(self, accounts=None):
        self.calls["positions"] += 1
        keep = (lambda name: True) if accounts is None else (lambda name: name in accounts or any(
            name.startswith(f"{a}:") for a in accounts))
        names = [name for name in ACCOUNTS if keep(name)]
        return Collection(holdings=[h for h in HOLDINGS if h.account in names],
                          cash=[c for c in CASH if any(c.account.startswith(n) for n in names)],
                          health={name: {"account": name, "status": "ok", "last_error": None} for name in names},
                          fetched_at=FETCHED)

    def collect_transactions(self, start, end, accounts=None, symbol=None, **options):
        self.calls["transactions"] += 1
        rows = [tx for tx in TRANSACTIONS if start <= tx.trade_date <= end and (accounts is None or tx.account in accounts)
                and (not symbol or tx.symbol == symbol)]
        names = [name for name in ACCOUNTS if accounts is None or name in accounts]
        return Collection(transactions=rows, fetched_at=FETCHED, fetch_complete=True,
                          health={name: {"account": name, "status": "ok", "last_error": None} for name in names},
                          requested_window={"start": start, "end": end})

    def build_fx(self, currencies):
        self.calls["fx"] += 1
        wanted = {c.upper() for c in currencies} - {"KRW", ""}
        return FxRates(rates={c: RATES[c] for c in wanted if c in RATES}, missing={c for c in wanted if c not in RATES},
                       as_of=FETCHED, source_by_currency={c: {"source": "provider", "priced_at": FETCHED}
                                                          for c in wanted if c in RATES})

    def health_report(self):
        return {}


class FakeToss:
    def __init__(self):
        self.calls = Counter()

    def get_mark_quotes(self, requested):
        self.calls["marks"] += 1
        return {}

    def get_prices(self, symbol):
        self.calls["prices"] += 1
        return [{"symbol": symbol.upper(), "lastPrice": "12.5", "currency": "USD"}]

    def get_orderbook(self, symbol):
        return {"symbol": symbol, "asks": [["12.6", "3"]], "bids": [["12.4", "2"]]}

    def get_recent_trades(self, symbol):
        return [{"price": "12.5", "quantity": "1"}]

    def get_price_limits(self, symbol):
        return {"upper": "16", "lower": "9"}

    def get_stock_warnings(self, symbol):
        return []

    def get_stock_info(self, symbol):
        return [{"symbol": symbol.upper(), "name": "Synthetic", "market": "US", "currency": "USD"}]

    def get_market_calendar(self, country):
        return {"country": country, "sessions": ["regular"]}

    def get_exchange_rate(self, base, quote):
        return {"base": base, "quote": quote, "rate": "1000"}


class FakeKis:
    def get_domestic_quote(self, symbol):
        return {"symbol": symbol, "price": "1000"}

    def get_overseas_quote(self, symbol, exchange="NASDAQ"):
        return {"symbol": symbol, "exchange": exchange, "price": "12.4", "raw": {"field": "x" * 40}}


PERPS = {"venues": {"binance": {"source_status": "ok", "reason_codes": [], "positions": [
    {"symbol": "BTCUSDT", "asset": "USDT", "notional": "100", "side": "long", "account_id": "synthetic"}]}},
    "reason_codes": []}
PERP_FX = {"USDT": {"rate_to_krw": "1400", "source": "upbit", "priced_at": FETCHED}}


@pytest.fixture
def stub(monkeypatch, tmp_path):
    return install_stub(monkeypatch, tmp_path)


def install_stub(monkeypatch, tmp_path):
    """Synthetic providers behind the real server functions (shared by the merge tests)."""
    collector, toss = StubCollector(), FakeToss()
    perp_reads = Counter()

    def perps():
        perp_reads["n"] += 1
        return PERPS, PERP_FX

    monkeypatch.setattr(server, "_collector", collector)
    monkeypatch.setattr(server, "_get_toss", lambda *a, **k: toss)
    monkeypatch.setattr(server, "_get_kis", lambda *a, **k: FakeKis())
    monkeypatch.setattr(server, "_get_nav_store", lambda: NavStore(tmp_path / "nav.db"))
    monkeypatch.setattr(server, "_perp_exposure_inputs", perps)
    monkeypatch.setattr(server, "_kis_source_labels",
                        lambda: ["kis:domestic", "kis:overseas", "kis:pension:domestic"])
    return SimpleNamespace(collector=collector, toss=toss, perp_reads=perp_reads)


def _data(env):
    assert env["ok"], env.get("error")
    return env["data"]


# ── T2 get_holdings ──────────────────────────────────────────────────


def test_holdings_section_equals_consolidated_holdings_and_cash_equals_summary(stub):
    for level in ("summary", "full"):
        old = _data(server.get_consolidated_holdings(detail_level=level))
        new = tools_v2.get_holdings(detail_level="full" if level == "full" else "summary")
        if level == "full":
            assert new["data"]["holdings"] == old
        else:
            positions = {(p["symbol"], p["currency"]): p for p in new["data"]["holdings"]["positions"]}
            for row in old["positions"]:
                mine = positions[(row["symbol"], row["currency"])]
                for key in ("quantity", "market_value_krw", "weight_pct", "cost_basis_krw", "unrealized_pnl_krw"):
                    assert mine[key] == row[key], key
            assert new["data"]["holdings"]["totals"] == old["totals"]
            assert "underlying_exposure" not in new["data"]["holdings"] and old["underlying_exposure"]
            assert "data.holdings.underlying_exposure" in new["meta"]["detail"]["omitted"]
    summary = _data(server.get_portfolio_summary(include_realized=False, snapshot=False))
    cash = tools_v2.get_holdings(detail_level="full")["data"]["cash"]
    assert cash["cash_krw"] == summary["cash_krw"]
    assert cash["cash_by_currency"] == summary["cash_by_currency"]
    assert cash["total_assets_krw"] == summary["total_assets_krw"]
    assert cash["cash_krw_by_account"] == {k: v["cash_krw"] for k, v in summary["by_account"].items() if "cash_krw" in v}


def test_holdings_reads_positions_fx_and_marks_once(stub):
    tools_v2.get_holdings(detail_level="full")
    assert stub.collector.calls["positions"] == 1 and stub.collector.calls["fx"] == 1
    assert stub.toss.calls["marks"] == 1


def test_holdings_account_scope_reads_only_that_account(stub):
    data = tools_v2.get_holdings(account="kis", detail_level="full")["data"]
    accounts = {a["account"] for p in data["holdings"]["positions"] for a in p["accounts"]}
    assert accounts == {"kis:domestic", "kis:overseas", "kis:pension:domestic"}
    assert set(data["cash"]["cash_krw_by_account"]) == {"kis:domestic"}


def test_raw_holdings_are_the_broker_tools_payloads(stub, monkeypatch):
    calls = []

    def tool(name):
        def run(*args, **kwargs):
            calls.append((name, kwargs))
            return server._ok({"tool": name, **kwargs})
        return run

    for name in ("get_toss_holdings", "get_toss_buying_power", "get_kis_domestic_balance",
                 "get_kis_overseas_balance", "get_kis_cash_balance"):
        monkeypatch.setattr(server, name, tool(name))
    data = tools_v2.get_holdings(account="all", detail_level="raw")["data"]
    assert data["toss_holdings"] == {"tool": "get_toss_holdings", "account": "all"}
    assert data["toss_buying_power_usd"] == {"tool": "get_toss_buying_power", "currency": "USD", "account": "all"}
    assert data["kis_cash_balance"] == {"tool": "get_kis_cash_balance", "account": "all"}
    calls.clear()
    tools_v2.get_holdings(account="kis:pension", include_cash=False, detail_level="raw")
    assert calls == [("get_kis_domestic_balance", {"account": "pension"}),
                     ("get_kis_overseas_balance", {"account": "pension"})]


# ── T3 get_portfolio_summary ─────────────────────────────────────────


def _volatile(data):
    """Wall-clock stamps the summary body writes per call (not portfolio figures)."""
    return {k: v for k, v in data.items() if k not in {"captured_at", "nav_completed_at", "observed_at"}}


def test_summary_sections_partition_the_old_summary_and_risk(stub):
    old_summary = _data(server.get_portfolio_summary(include_realized=True, snapshot=False))
    old_risk = _data(server.get_portfolio_risk(detail_level="full"))
    old_holdings = _data(server.get_consolidated_holdings(detail_level="summary"))
    new = tools_v2.get_portfolio_summary_v2(sections=list(tools_v2.SUMMARY_SECTIONS), detail_level="full")
    assert new["ok"] and set(new["data"]) == {"totals", "exposure", "concentration", "realized"}
    sections = new["data"]
    merged = {**sections["totals"], **sections["realized"]}
    for key in ("total_assets_krw", "stock_value_krw", "cash_krw", "cost_basis_krw", "unrealized_pnl_krw",
                "by_account", "cash_by_currency", "nav_bridge", "realized_pnl_krw", "realized_pnl_status",
                "realized_pnl_reason_codes", "fees_krw", "taxes_krw", "result_status", "fx"):
        assert merged[key] == old_summary[key], key
    assert _volatile(merged) == _volatile({k: v for k, v in old_summary.items()
                                           if k not in {"by_currency", "by_market", "underlying_exposure", "snapshot"}})
    assert "snapshot" not in merged
    exposure = sections["exposure"]
    assert exposure["by_currency"] == old_holdings["by_currency"] == old_summary["by_currency"]
    assert exposure["by_market"] == old_holdings["by_market"] == old_summary["by_market"]
    assert exposure["underlying_exposure"] == old_risk["underlying_exposure"]
    assert exposure["currency_weights_pct"] == old_risk["currency_weights_pct"]
    assert {k: v for k, v in old_risk.items() if k not in tools_v2._EXPOSURE_FROM_RISK} == sections["concentration"]


def test_perps_feed_exposure_only_and_never_enter_totals(stub):
    """Perps never enter totals/NAV. The shared old summary reads perps for its own
    underlying_exposure on every call (in parallel with positions); totals drop that block."""
    new = tools_v2.get_portfolio_summary_v2(sections=["totals"], detail_level="full")
    assert stub.perp_reads["n"] == 1 and set(new["data"]) == {"totals"}
    assert "underlying_exposure" not in new["data"]["totals"]
    old_total = _data(server.get_portfolio_summary(include_realized=False, snapshot=False))["total_assets_krw"]
    assert new["data"]["totals"]["total_assets_krw"] == old_total  # never in NAV
    reads = stub.perp_reads["n"]
    new = tools_v2.get_portfolio_summary_v2(sections=["totals", "exposure"], detail_level="full")
    assert stub.perp_reads["n"] == reads + 2  # the summary's own read + the risk tool's (exposure)
    perps = new["data"]["exposure"]["underlying_exposure"]["perps"]
    assert perps["status"] == "complete" and perps["leg_count"] == 1
    assert new["data"]["totals"]["total_assets_krw"] == old_total
    reads = stub.perp_reads["n"]
    tools_v2.get_portfolio_summary_v2(sections=["concentration"], detail_level="full")
    assert stub.perp_reads["n"] == reads + 1  # the risk tool only; no summary without totals/realized


def test_summary_reads_the_portfolio_once_for_all_sections(stub):
    tools_v2.get_portfolio_summary_v2(sections=list(tools_v2.SUMMARY_SECTIONS), detail_level="full")
    assert stub.collector.calls["positions"] == 1
    assert stub.collector.calls["fx"] == 1
    assert stub.collector.calls["transactions"] == 1
    assert stub.toss.calls["marks"] == 1


@pytest.mark.parametrize("failing", ["ledger", "positions"])
def test_summary_sections_report_their_own_status(stub, monkeypatch, failing):
    """A partial ledger marks realized (not totals) partial, a partial positions read totals."""
    import dataclasses

    method = "collect_transactions" if failing == "ledger" else "collect_positions"
    real = getattr(StubCollector, method)

    def partial_read(self, *args, **kwargs):
        collection = real(self, *args, **kwargs)
        first = sorted(collection.health)[0]
        health = {**collection.health, first: {"account": first, "status": "error", "last_error": "synthetic"}}
        return dataclasses.replace(collection, health=health)

    # a clean NAV bridge: one account, its cash row with a complete settlement basis
    cash = dataclasses.replace(CASH[1], native_cash_includes_unsettled=True, positions_basis="trade_date",
                               unsettled_receivables=0.0, unsettled_payables=0.0)
    monkeypatch.setattr(sys.modules[__name__], "HOLDINGS", [h for h in HOLDINGS if h.account == cash.account])
    monkeypatch.setattr(sys.modules[__name__], "CASH", [cash])
    clean = server.get_portfolio_summary(include_realized=False)
    assert clean["data"]["nav_bridge"]["reconciliation"]["difference_reasons"] == []
    monkeypatch.setattr(StubCollector, method, partial_read)
    env = tools_v2.get_portfolio_summary_v2(detail_level="full")
    old = server.get_portfolio_summary(include_realized=True)
    assert old["result_status"] == env["result_status"] == "partial"
    sections = env["meta"]["sections"]
    if failing == "ledger":
        assert env["data"]["realized"]["realized_pnl_status"] == "partial"
        assert sections["realized"]["result_status"] == "partial"
        assert sections["totals"]["result_status"] == env["data"]["totals"]["result_status"] == "complete"
        alone = tools_v2.get_portfolio_summary_v2(sections=["totals"], detail_level="full")
        assert alone["result_status"] == "complete"
    else:
        assert sections["totals"]["result_status"] == env["data"]["totals"]["result_status"] == "partial"
        assert sections["realized"]["result_status"] == env["data"]["realized"]["realized_pnl_status"]


def test_summary_level_keeps_the_numbers(stub):
    env = server.get_portfolio_summary(include_realized=True, snapshot=False)
    old = _data(response_shape.shape(env, response_shape.POLICIES["get_portfolio_summary"], "summary"))
    new = tools_v2.get_portfolio_summary_v2()
    assert set(new["data"]) == {"totals", "realized"} and "underlying_exposure" not in new["data"]["totals"]
    assert new["meta"]["detail"]["level"] == "summary"
    for key in ("total_assets_krw", "stock_value_krw", "cash_krw", "unrealized_pnl_krw", "result_status"):
        assert new["data"]["totals"][key] == old[key], key
    assert new["data"]["realized"]["realized_pnl_krw"] == old["realized_pnl_krw"]
    assert "methodology" not in new["data"]["totals"]  # moved to full by the summary policy


# ── T4 get_portfolio_returns ─────────────────────────────────────────


def test_returns_sections_equal_performance_and_history(stub, monkeypatch):
    seen = []

    def performance(**kwargs):
        seen.append(("performance", kwargs))
        return server._ok({"period": kwargs["period"], "scope": kwargs["portfolio_scope"], "twr_pct": 1.5})

    def history(**kwargs):
        seen.append(("history", kwargs))
        return server._ok({"period": kwargs["period"], "nav_observations": 3})

    monkeypatch.setattr(server, "get_portfolio_performance", performance)
    monkeypatch.setattr(server, "get_portfolio_history", history)
    new = tools_v2.get_portfolio_returns(period="3M", scope="toss", include_series=True, detail_level="full")
    assert new["data"]["returns"] == {"period": "3M", "scope": "toss", "twr_pct": 1.5}
    assert new["data"]["series"] == {"period": "3M", "nav_observations": 3}
    assert seen == [("performance", {"period": "3M", "detail_level": "full", "portfolio_scope": "toss"}),
                    ("history", {"period": "3M", "detail_level": "full", "portfolio_scope": "toss"})]
    assert set(tools_v2.get_portfolio_returns()["data"]) == {"returns"}


# ── T5 get_transactions ──────────────────────────────────────────────


def test_transactions_pages_carry_the_old_ledger_rows_newest_first(stub):
    old = _data(server.get_all_transactions("2026-01-01", "2026-10-21", "all", "", detail_level="full"))
    rows = sorted(old.pop("transactions"), key=lambda r: (r["trade_date"], r["account"], r["tx_id"]), reverse=True)
    seen, cursor = [], ""
    while True:
        page = tools_v2.get_transactions(start="2026-01-01", end="2026-10-21", detail_level="full", limit=2,
                                         cursor=cursor)
        data = page["data"]["transactions"]
        assert data["transaction_count"] == old["transaction_count"] == len(TRANSACTIONS)
        assert data["count_by_type"] == old["count_by_type"]
        seen += data["rows"]
        cursor = data["page"]["next_cursor"]
        if not cursor:
            break
    assert seen == rows
    summary = tools_v2.get_transactions(start="2026-01-01", end="2026-10-21")["data"]["transactions"]
    assert summary["columns"] == list(tools_v2.TX_COLUMNS)
    assert [dict(zip(summary["columns"], row))["quantity"] for row in summary["rows"]] == [r["quantity"] for r in rows]


def test_transactions_cursor_must_repeat_the_arguments(stub):
    first = tools_v2.get_transactions(start="2026-01-01", end="2026-10-21", limit=1)["data"]["transactions"]
    other = tools_v2.get_transactions(start="2026-02-01", end="2026-10-21", limit=1,
                                      cursor=first["page"]["next_cursor"])
    assert other["ok"] is False and other["error"]["code"] == "INVALID_CURSOR"
    assert tools_v2.get_transactions(limit=501)["error"]["code"] == "INVALID_LIMIT"


def test_raw_transactions_page_each_broker_source(stub, monkeypatch):
    toss_rows = [{"orderId": str(i)} for i in range(5)]
    kis_calls = []
    monkeypatch.setattr(server, "_get_toss_registry", lambda: SimpleNamespace(account_labels=lambda: ["primary"]))
    monkeypatch.setattr(server, "_get_kis_registry", lambda: SimpleNamespace(account_labels=lambda: ["main"],
                                                                              default_label=lambda: "main"))
    monkeypatch.setattr(server, "get_toss_trade_history",
                        lambda limit=50, account="primary": server._ok(toss_rows[:limit], provider="toss"))

    def kis(start_date, end_date, account=""):
        kis_calls.append((start_date, end_date, account))
        return server._ok({"domestic": {"trades": [{"odno": "d1"}], "source_metadata": {"complete": True}},
                           "overseas": {"trades": [{"odno": "o1"}, {"odno": "o2"}]}}, provider="kis")

    monkeypatch.setattr(server, "get_kis_trade_history", kis)
    rows, cursor, sources = [], "", []
    while True:
        page = tools_v2.get_transactions(start="2026-01-01", end="2026-01-31", detail_level="raw", limit=2,
                                         cursor=cursor)["data"]["transactions"]
        rows += page["rows"]
        sources.append(page["page"]["source"])
        cursor = page["page"]["next_cursor"]
        if not cursor:
            break
    assert rows == toss_rows + [{"odno": "d1"}, {"odno": "o1"}, {"odno": "o2"}]
    assert sources == ["toss:primary"] * 3 + ["kis:main:domestic", "kis:main:overseas"]
    assert set(kis_calls) == {("20260101", "20260131", "main")}


# ── T6 get_trading_pnl ───────────────────────────────────────────────


def test_trading_pnl_views_equal_realized_pnl_and_trade_review(stub):
    old = _data(server.get_realized_pnl(period="YTD", account="all"))
    new = tools_v2.get_trading_pnl(view="by_symbol", detail_level="full")["data"]["by_symbol"]
    assert new == old
    old = _data(server.get_trade_review(period="YTD", detail_level="full"))
    new = tools_v2.get_trading_pnl(view="round_trips", detail_level="full")["data"]["round_trips"]
    assert new == old


# ── T7 get_quote ─────────────────────────────────────────────────────


def test_quote_parts_equal_the_old_quote_tools(stub):
    new = tools_v2.get_quote_v2(["SYNA"], provider="toss", include=list(tools_v2.QUOTE_INCLUDE), market="us",
                                detail_level="full")
    row = new["data"]["quotes"][0]
    assert row["quote"] == _data(server.get_quote("SYNA"))
    assert row["orderbook"] == _data(server.get_orderbook("SYNA"))
    assert row["trades"] == _data(server.get_market_trades("SYNA"))
    assert row["limits"] == _data(server.get_price_limits("SYNA"))
    assert row["warnings"] == _data(server.get_stock_warnings("SYNA"))
    assert row["info"] == _data(server.get_stock_info("SYNA", "us"))
    kis = tools_v2.get_quote_v2(["SYNA"], provider="kis", market="us", exchange="NYSE")["data"]["quotes"][0]
    assert kis["quote"] == _data(server.get_kis_overseas_quote("SYNA", exchange="NYSE")) and kis["provider"] == "kis"
    kr = tools_v2.get_quote_v2(["000001"], provider="kis", market="kr")["data"]["quotes"][0]
    assert kr["quote"] == _data(server.get_kis_domestic_quote("000001"))


def test_quote_both_is_compare_quotes_with_its_summary_policy(stub):
    old = _data(server.compare_quotes("SYNA", market="us"))
    full = tools_v2.get_quote_v2(["SYNA"], provider="both", market="us", detail_level="full")["data"]["quotes"][0]
    summary = tools_v2.get_quote_v2(["SYNA"], provider="both", market="us")["data"]["quotes"][0]
    strip = lambda d: {**d, "quotes": [{k: v for k, v in q.items() if k != "observed_at"} for q in d["quotes"]]}  # noqa: E731
    assert strip(full["quote"]) == strip(old)
    assert "raw" in full["quote"]["quotes"][1]["data"] and "raw" not in summary["quote"]["quotes"][1]["data"]
    assert summary["quote"]["comparison"] == old["comparison"]


def test_quote_auto_falls_back_to_kis(stub, monkeypatch):
    def broken(symbol):
        raise RuntimeError("synthetic outage")

    monkeypatch.setattr(stub.toss, "get_prices", broken)
    row = tools_v2.get_quote_v2(["SYNA"], market="us")["data"]["quotes"][0]
    assert row["provider"] == "kis" and row["sources"]["quote"]["fallback_from"] == "toss"
    assert row["quote"]["symbol"] == "SYNA"


# ── T9 get_market_info ───────────────────────────────────────────────


def test_market_info_sections_equal_the_old_tools(stub, monkeypatch):
    monkeypatch.setattr(server, "risk_free_rate", lambda ticker: {"ten_year_treasury_yield_pct": 4.1})
    monkeypatch.setattr(server, "yf", SimpleNamespace(Ticker=lambda symbol: None))
    data = tools_v2.get_market_info(market="KR", base="JPY", quote="KRW")["data"]
    assert data["hours"] == _data(server.get_market_hours("KR"))
    assert data["fx"] == _data(server.get_exchange_rate(base="JPY", quote="KRW"))
    assert data["risk_free_rate"] == _data(server.get_risk_free_rate())
    assert set(tools_v2.get_market_info(include=["fx"])["data"]) == {"fx"}


# ── T10 crypto ───────────────────────────────────────────────────────


class FakeBinance:
    def snapshot(self, symbol):
        return {"symbol": symbol, "quote": {"price": "100"}, "mark": {"markPrice": "100.1"}}

    def funding_rates(self, symbol, limit=10):
        return [{"fundingRate": "0.0001"}] * limit

    def open_interest_history(self, symbol, period="1h", limit=30):
        return [{"period": period}] * limit

    def klines(self, symbol, interval="1h", limit=100, price_type="last"):
        return [{"interval": interval}] * limit


def test_crypto_market_sections_equal_the_old_tools(stub, monkeypatch):
    monkeypatch.setattr(server, "_get_binance", FakeBinance)
    data = tools_v2.get_crypto_market("BTCUSDT", include=list(tools_v2.CRYPTO_INCLUDE), interval="4h",
                                      limit=7)["data"]
    assert data["snapshot"] == _data(server.get_crypto_futures_snapshot("BTCUSDT"))
    assert data["funding_history"] == _data(server.get_binance_funding_rate_history("BTCUSDT", 7))
    assert data["open_interest"] == _data(server.get_binance_open_interest("BTCUSDT", history=True, period="4h",
                                                                           limit=7))
    assert data["candles"] == _data(server.get_binance_futures_candles("BTCUSDT", interval="4h", count=7))


class FakeFutures:
    def __init__(self):
        self.calls = []

    def query(self, operation, venue="all", start="", end="", symbol=""):
        self.calls.append((operation, venue, start, end, symbol))
        field = {"fills": "fills", "funding": "funding_payments", "positions": "positions"}[operation]
        venues = {}
        for name, times in (("binance", [3, 1, 5]), ("hyperliquid", [4, 2])):
            rows = [{"time": t, ("fill_id" if operation == "fills" else "payment_id"): f"{name}{t}", "size": "1"}
                    for t in times]
            venues[name] = {field: rows, "source_status": "ok", "coverage": {"status": "complete"},
                            "result_status": "complete", "reason_codes": []}
        return {"venues": venues, "source_status": "ok", "coverage": {"status": "complete"},
                "result_status": "complete", "reason_codes": []}


def test_crypto_positions_and_history_equal_the_old_tools(stub, monkeypatch):
    fake = FakeFutures()
    monkeypatch.setattr(server, "_crypto_futures_accounts", fake)
    assert tools_v2.get_crypto_positions() == server.get_crypto_futures_positions("all")
    old = _data(server.get_crypto_futures_fills("all", "2026-10-01", "2026-10-07"))
    rows, cursor = [], ""
    while True:
        page = tools_v2.get_crypto_account_history("fills", start="2026-10-01", end="2026-10-07", limit=2,
                                                   cursor=cursor)["data"]["history"]
        rows += page["rows"]
        cursor = page["page"]["next_cursor"]
        if not cursor:
            break
    expected = sorted(({"venue": v, **row} for v, block in old["venues"].items() for row in block["fills"]),
                      key=lambda r: r["time"], reverse=True)
    assert rows == expected
    assert page["venues"]["binance"]["row_count"] == 3 and page["result_status"] == old["result_status"]
    funding = tools_v2.get_crypto_account_history("funding", end="2026-10-07")["data"]["history"]
    assert funding["window"] == {"start": "2026-10-01", "end": "2026-10-07"}
    assert fake.calls[-1] == ("funding", "all", "2026-10-01", "2026-10-07", "")


# ── T1 get_status ────────────────────────────────────────────────────


def test_status_accounts_sections_equal_the_old_tools(stub, monkeypatch):
    toss_registry = SimpleNamespace(account_labels=lambda: ["primary"],
                                    client_for=lambda label: SimpleNamespace(get_account_masked=lambda: "••••0000"))
    kis_registry = SimpleNamespace(accounts_metadata=lambda: [{"label": "main", "kind": "general"}],
                                   default_label=lambda: "main", account_labels=lambda: ["main"])
    monkeypatch.setattr(server, "_get_toss_registry", lambda: toss_registry)
    monkeypatch.setattr(server, "_get_kis_registry", lambda: kis_registry)
    data = tools_v2.get_status(mode="accounts", detail_level="full")["data"]
    assert data["toss_accounts"] == _data(server.get_toss_accounts())
    assert data["kis_accounts"] == _data(server.get_kis_accounts())
    health = _data(server.get_account_health(detail_level="full"))
    assert {k: v for k, v in data["account_health"].items() if k != "rolling_health"} == \
        {k: v for k, v in health.items() if k != "rolling_health"}
    passive = tools_v2.get_status()["data"]["service_health"]
    assert passive["overall_status"] == _data(server.get_service_health("passive"))["overall_status"]


def test_status_help_describes_the_chat_catalog():
    data = tools_v2.get_status(mode="help", topic="all", detail_level="full")["data"]["help"]
    names = [contract["name"] for contract in data["tool_contracts"]]
    assert sorted(names) == sorted(public for public, _ in tools_v2.CHAT_TOOLS)
    assert "get_quote_v2" not in data["markdown"] and "get_toss_holdings" not in data["markdown"]
