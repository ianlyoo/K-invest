"""get_realized_pnl and get_trade_review state the same realized P&L per (symbol, currency).

A 2.15.0 release check found one mismatch: get_realized_pnl(reconcile=False)
published a figure for a book whose engine quantity differs from the broker's, while
get_trade_review nulled it with ENGINE_QUANTITY_DRIFT. The engine's quantity check now runs in
every realized tool whatever `reconcile` is (portfolio_engine.realized_input_quality).

Synthetic numbers: toss:primary buys 24 and sells 23 of one symbol, the
broker holds none (one share left the book without a ledger row); toss:secondary trades the same
symbol cleanly. Synthetic Toss order payloads go through the real normalizer and the real tools;
the collector is a fake (no broker, no credentials).
"""
import pytest
from test_ledger_engine_quantity import FX, order
from test_ledger_returns import END, START, wired  # noqa: F401 - pytest fixture

from portfolio_model import Holding, toss_transactions
from portfolio_sources import Collection

PRIMARY, SECONDARY = "toss:primary", "toss:secondary"
ACCOUNTS = (PRIMARY, SECONDARY)
TXS = (toss_transactions([order("P1", "BUY", "2025-03-03T14:00:00.000Z", 24, price=10.0, symbol="SYNQ"),
                          order("P2", "SELL", "2025-03-04T14:00:00.000Z", 23, price=12.0, symbol="SYNQ"),
                          order("P3", "BUY", "2025-03-05T14:00:00.000Z", 5, price=20.0, symbol="MSFT"),
                          order("P4", "SELL", "2025-03-06T14:00:00.000Z", 2, price=25.0, symbol="MSFT")], PRIMARY)
       + toss_transactions([order("S1", "BUY", "2025-04-01T14:00:00.000Z", 10, price=10.0, symbol="SYNQ"),
                            order("S2", "SELL", "2025-04-02T14:00:00.000Z", 10, price=11.0, symbol="SYNQ")], SECONDARY))
HOLDINGS = [Holding("toss", PRIMARY, "MSFT", "USD", 3, last_price=25.0, market_value=75.0, unrealized_pnl=15.0)]
READ_AT = END + "T03:00:00+00:00"  # noon KST on the ledger's end date


class Collector:
    """Per-account filtered reads; holdings_ok=False makes every holdings read fail."""

    def __init__(self, holdings_ok=True):
        self.holdings_ok = holdings_ok
        self.positions_calls = []

    def collect_positions(self, accounts=None):
        self.positions_calls.append(accounts)
        wanted = [a for a in ACCOUNTS if accounts is None or a in accounts]
        if not self.holdings_ok:
            return Collection(health={a: {"status": "error", "last_error": {"kind": "provider_unavailable"}}
                                      for a in wanted}, fetched_at=READ_AT)
        return Collection(holdings=[h for h in HOLDINGS if h.account in wanted],
                          health={a: {"status": "ok"} for a in wanted}, fetched_at=READ_AT)

    def collect_transactions(self, start, end, accounts=None, symbol=None):
        wanted = [a for a in ACCOUNTS if accounts is None or a in accounts]
        return Collection(transactions=[t for t in TXS if t.account in wanted and (not symbol or t.symbol == symbol)],
                          health={a: {"status": "ok"} for a in wanted}, fetched_at=READ_AT,
                          requested_window={"start": START, "end": END}, fetch_complete=True)

    def build_fx(self, *args):
        return FX


# The release check's extractors, restated.
def realized_map(data):
    return {f"{s['symbol']}:{s['currency']}": s.get("realized_pnl") for s in data["symbols"]}


def review_map(data):
    return {f"{s['symbol']}:{s['currency']}": (s["realized"]["diagnostic_pnl_by_currency"] or {}).get(s["currency"])
            for s in data["symbols"] if s["realized"]["sell_count"]}


def null_propagating_sum(maps):
    out = {}
    for m in maps:
        for key, value in m.items():
            out[key] = None if value is None or (key in out and out[key] is None) else out.get(key, 0.0) + value
    return out


@pytest.fixture
def tools(wired, monkeypatch):  # noqa: F811
    collector = Collector()
    monkeypatch.setattr(wired.server, "_get_collector", lambda: collector)
    # wired's period stub, plus get_trade_review's end-date anchor.
    monkeypatch.setattr(wired.server.portfolio_engine, "resolve_period", lambda _period, today=None: (START, END))
    return wired.server, collector


@pytest.mark.parametrize("reconcile", [False, True])
def test_realized_pnl_equals_trade_review_for_every_symbol_whatever_reconcile(tools, reconcile):
    server, _ = tools
    realized = realized_map(server.get_realized_pnl(period="ALL", account="all", reconcile=reconcile)["data"])
    review = review_map(server.get_trade_review(period="ALL", account="all", detail_level="full")["data"])
    assert realized == review
    # The drifted book makes the symbol unknown across accounts; a clean symbol keeps its figure.
    assert realized["SYNQ:USD"] is None
    assert realized["MSFT:USD"] == pytest.approx(2 * 25.0 - 2 * 20.0)


def test_reconcile_false_per_account_rows_follow_the_book_and_sum_to_the_aggregate(tools):
    server, _ = tools
    per = {a: server.get_realized_pnl(period="ALL", account=a, reconcile=False)["data"] for a in ACCOUNTS}
    primary = {s["symbol"]: s for s in per[PRIMARY]["symbols"]}
    assert primary["SYNQ"]["realized_pnl"] is None and primary["SYNQ"]["cost_basis"] is None
    assert "ENGINE_QUANTITY_DRIFT" in primary["SYNQ"]["reason_codes"]
    assert primary["SYNQ"]["sell_count"] == 1 and primary["SYNQ"]["quantity_sold"] == 23
    secondary = {s["symbol"]: s for s in per[SECONDARY]["symbols"]}
    assert secondary["SYNQ"]["realized_pnl"] == pytest.approx(10 * 11.0 - 10 * 10.0)
    assert secondary["SYNQ"]["reason_codes"] == []
    aggregate = realized_map(server.get_realized_pnl(period="ALL", account="all", reconcile=False)["data"])
    assert null_propagating_sum(realized_map(d) for d in per.values()) == aggregate


def test_reconcile_false_keeps_the_report_not_requested_and_states_the_engine_check(tools):
    server, collector = tools
    data = server.get_realized_pnl(period="ALL", account="all", reconcile=False)["data"]
    report = data["reconciliation"]
    assert report["status"] == "unavailable" and report["reason_codes"] == ["RECONCILIATION_NOT_REQUESTED"]
    check = report["engine_quantity_check"]
    assert check["status"] == "drift" and check["reason_codes"] == ["ENGINE_QUANTITY_DRIFT"]
    assert check["drift_books"] == [{"account": PRIMARY, "symbol": "SYNQ", "currency": "USD",
                                     "engine_qty": 1.0, "ending_qty": 0.0}]
    assert collector.positions_calls == [None]
    # Only the drift break reaches the engine: it is flagged, and nothing else of the report is.
    assert [b["symbol"] for b in data["ledger_quality"]["reconciliation_breaks"]] == ["SYNQ"]
    assert "ENGINE_QUANTITY_DRIFT" in data["totals"]["incomplete_basis"]["realized_pnl_reason_codes"]
    full = server.get_realized_pnl(period="ALL", account="all", reconcile=True)["data"]
    assert full["reconciliation"]["engine_quantity_drift_breaks"] == 1
    assert realized_map(full) == realized_map(data)


def test_unreadable_holdings_leave_both_tools_on_the_same_figures(tools, monkeypatch):
    server, _ = tools
    collector = Collector(holdings_ok=False)
    monkeypatch.setattr(server, "_get_collector", lambda: collector)
    envelope = server.get_realized_pnl(period="ALL", account="all", reconcile=False)
    assert envelope["ok"]
    check = envelope["data"]["reconciliation"]["engine_quantity_check"]
    assert check["status"] == "unavailable" and check["drift_books"] == []
    review = server.get_trade_review(period="ALL", account="all", detail_level="full")["data"]
    assert realized_map(envelope["data"]) == review_map(review)
