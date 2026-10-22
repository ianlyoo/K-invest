"""Every tool reconciles before it computes realized P&L, so a drifted book is null there too.

Synthetic Toss order payloads through the real normalizer; the collector and NAV store are
test_ledger_returns' wired fakes (no broker, no credentials). Synthetic amounts only.
"""
from test_ledger_engine_quantity import ACCOUNT, FX, order
from test_ledger_returns import END, START, wired  # noqa: F401 - pytest fixture

from portfolio_model import Holding, toss_transactions
from portfolio_sources import Collection

ROWS = [order("B1", "BUY", "2025-02-02T14:00:00.000Z", 100),
        order("S1", "SELL", "2025-02-10T14:00:00.000Z", 20, price=15.0),
        order("B2", "BUY", "2025-02-02T14:00:00.000Z", 10, symbol="MSFT"),
        order("S2", "SELL", "2025-02-11T14:00:00.000Z", 10, price=12.0, symbol="MSFT")]
READ_AT = END + "T03:00:00+00:00"  # noon KST on the ledger's end date


class DriftCollector:
    """The broker holds 50 ACMU; the ledger leaves 80 (30 shares left without a row)."""

    def collect_positions(self, *args):
        return Collection(holdings=[Holding("toss", ACCOUNT, "ACMU", "USD", 50, market_value=500.0, last_price=10.0)],
                          health={ACCOUNT: {"status": "ok"}}, fetched_at=READ_AT)

    def collect_transactions(self, *args):
        return Collection(transactions=toss_transactions(ROWS, ACCOUNT), health={ACCOUNT: {"status": "ok"}},
                          fetched_at=READ_AT, requested_window={"start": START, "end": END}, fetch_complete=True)

    def build_fx(self, *args):
        return FX


def by_symbol(rows):
    return {row["symbol"]: row for row in rows}


def test_performance_and_realized_tools_null_the_drifted_book(wired, monkeypatch):  # noqa: F811
    server = wired.server
    monkeypatch.setattr(server, "_get_collector", lambda: DriftCollector())
    performance = server.get_portfolio_performance()["data"]
    rows = by_symbol(performance["realized_by_symbol"])
    assert rows["ACMU"]["realized_pnl_krw"] is None and "ENGINE_QUANTITY_DRIFT" in rows["ACMU"]["reason_codes"]
    assert rows["MSFT"]["realized_pnl"] == 20.0
    assert "ACMU" in performance["incomplete_cost_basis"]
    realized = server.get_realized_pnl()["data"]
    assert by_symbol(realized["symbols"])["ACMU"]["realized_pnl_krw"] is None
    assert realized["reconciliation"]["engine_quantity_drift_breaks"] == 1
    assert realized["totals"]["realized_pnl_krw"] is None
    summary = server.get_portfolio_summary(include_realized=True, snapshot=False)["data"]
    assert summary["realized_pnl_krw"] is None
