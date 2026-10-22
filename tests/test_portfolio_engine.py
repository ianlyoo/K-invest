"""Accounting and performance math — the numbers must be exactly reproducible."""

from __future__ import annotations

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import portfolio_engine as pe
from portfolio_model import CashBalance, FxRates, Holding, Transaction, TxType


def fx(usd: float = 1300.0, jpy: float | None = 9.0) -> FxRates:
    rates = {"USD": usd}
    if jpy is not None:
        rates["JPY"] = jpy
    return FxRates(rates=rates, as_of="2026-08-18T00:00:00+00:00")


def tx(
    tx_type: TxType,
    trade_date: str,
    symbol: str,
    quantity: float,
    price: float,
    *,
    currency: str = "KRW",
    fee: float = 0.0,
    tax: float = 0.0,
    fx_rate: float | None = None,
    tx_id: str = "",
    account: str = "kis:domestic",
) -> Transaction:
    gross = quantity * price
    net = -(gross + fee + tax) if tx_type is TxType.BUY else gross - fee - tax
    return Transaction(
        provider="kis",
        account=account,
        tx_id=tx_id or f"{trade_date}-{symbol}-{tx_type.value}",
        tx_type=tx_type,
        trade_date=trade_date,
        currency=currency,
        symbol=symbol,
        quantity=quantity,
        price=price,
        gross_amount=gross,
        fee=fee,
        tax=tax,
        fee_status="verified_zero" if fee == 0 else "reported",
        tax_status="verified_zero" if tax == 0 else "reported",
        net_amount=net,
        fx_rate=fx_rate,
    )


# ── Period resolution ────────────────────────────────────


def test_resolve_period_bounds():
    today = date(2026, 8, 18)
    assert pe.resolve_period("MTD", today) == ("2026-08-01", "2026-08-18")
    assert pe.resolve_period("YTD", today) == ("2026-01-01", "2026-08-18")
    assert pe.resolve_period("1M", today) == ("2026-07-19", "2026-08-18")
    assert pe.resolve_period("1Y", today) == ("2025-08-18", "2026-08-18")
    assert pe.resolve_period("ALL", today)[0] == "2000-01-01"


def test_resolve_period_rejects_unknown():
    with pytest.raises(ValueError, match="unsupported period"):
        pe.resolve_period("WTD", date(2026, 8, 18))


# ── Realized P&L: moving average ─────────────────────────


def test_moving_average_cost_basis_across_two_buys():
    """100@1000 then 100@2000 -> avg 1500; selling 100@1800 realizes 30,000."""
    transactions = [
        tx(TxType.BUY, "2026-01-05", "005930", 100, 1000),
        tx(TxType.BUY, "2026-02-05", "005930", 100, 2000),
        tx(TxType.SELL, "2026-03-05", "005930", 100, 1800),
    ]
    result = pe.compute_realized_pnl(transactions, fx(), "2026-01-01", "2026-12-31")
    entry = result["symbols"][0]
    assert entry["symbol"] == "005930"
    assert entry["cost_basis"] == pytest.approx(150_000)
    assert entry["proceeds"] == pytest.approx(180_000)
    assert entry["realized_pnl"] == pytest.approx(30_000)
    assert entry["realized_pnl_pct"] == pytest.approx(20.0)
    assert result["cost_basis_method"] == "moving_average"
    # 100 shares left at the same 1,500 average.
    assert result["open_positions_from_ledger"]["005930:KRW"]["avg_price"] == pytest.approx(1500)


def test_buy_fees_capitalized_and_sell_fees_reduce_proceeds():
    transactions = [
        tx(TxType.BUY, "2026-01-05", "AAPL", 10, 100, currency="USD", fee=5),
        tx(TxType.SELL, "2026-02-05", "AAPL", 10, 110, currency="USD", fee=6, tax=4),
    ]
    result = pe.compute_realized_pnl(transactions, fx(), "2026-01-01", "2026-12-31")
    entry = result["symbols"][0]
    # cost 1000 + 5 buy fee; proceeds 1100 - 6 - 4
    assert entry["cost_basis"] == pytest.approx(1005)
    assert entry["proceeds"] == pytest.approx(1090)
    assert entry["realized_pnl"] == pytest.approx(85)
    assert result["totals"]["fees_by_currency"]["USD"] == pytest.approx(11)
    assert result["totals"]["taxes_by_currency"]["USD"] == pytest.approx(4)


def test_realized_pnl_reports_only_sales_inside_window_but_replays_earlier_buys():
    transactions = [
        tx(TxType.BUY, "2025-06-01", "005930", 10, 1000),
        tx(TxType.SELL, "2026-03-05", "005930", 10, 1500),
    ]
    result = pe.compute_realized_pnl(transactions, fx(), "2026-01-01", "2026-12-31")
    entry = result["symbols"][0]
    # The 2025 buy is outside the window but still sets cost basis.
    assert entry["cost_basis"] == pytest.approx(10_000)
    assert entry["realized_pnl"] == pytest.approx(5_000)
    assert result["incomplete_cost_basis"] == []


def test_sell_without_prior_buy_is_flagged_incomplete():
    """A sale whose purchase predates the ledger must not read as pure profit."""
    result = pe.compute_realized_pnl(
        [tx(TxType.SELL, "2026-03-05", "005930", 10, 1500)], fx(), "2026-01-01", "2026-12-31"
    )
    entry = result["symbols"][0]
    assert result["incomplete_cost_basis"] == ["005930"]
    assert entry["cost_basis_complete"] is False
    assert entry["cost_basis"] == pytest.approx(0.0)
    assert entry["realized_pnl"] == pytest.approx(15_000)


def test_partial_oversell_flags_incomplete_and_matches_available_quantity():
    transactions = [
        tx(TxType.BUY, "2026-01-05", "005930", 4, 1000),
        tx(TxType.SELL, "2026-02-05", "005930", 10, 1200),
    ]
    result = pe.compute_realized_pnl(transactions, fx(), "2026-01-01", "2026-12-31")
    entry = result["symbols"][0]
    assert result["incomplete_cost_basis"] == ["005930"]
    # Only the 4 tracked shares carry cost; proceeds are the full 10.
    assert entry["cost_basis"] == pytest.approx(4_000)
    assert entry["proceeds"] == pytest.approx(12_000)
    assert "005930:KRW" not in result["open_positions_from_ledger"]


def test_split_preserves_cost_basis_and_restates_quantity():
    transactions = [
        # The split and both trade legs belong to the same account. Pooling
        # previously concealed this fixture's mismatched account labels.
        tx(TxType.BUY, "2026-01-05", "AAPL", 10, 100, currency="USD", account="kis:overseas"),
        Transaction(
            provider="kis",
            account="kis:overseas",
            tx_id="split-1",
            tx_type=TxType.SPLIT,
            trade_date="2026-02-01",
            currency="USD",
            symbol="AAPL",
            quantity=40,  # post-split share count (4:1)
        ),
        tx(TxType.SELL, "2026-03-05", "AAPL", 40, 30, currency="USD", account="kis:overseas"),
    ]
    result = pe.compute_realized_pnl(transactions, fx(), "2026-01-01", "2026-12-31")
    entry = result["symbols"][0]
    # Cost unchanged at 1,000; proceeds 40x30 = 1,200 -> 200 gain, not a fake loss.
    assert entry["cost_basis"] == pytest.approx(1_000)
    assert entry["realized_pnl"] == pytest.approx(200)
    assert result["incomplete_cost_basis"] == []


def test_realized_krw_decomposes_into_local_and_fx_components():
    """Bought at 1,200 KRW/USD, sold at 1,400: the currency move is real P&L.

    Spent 10 x 100 x 1,200 = 1,200,000 KRW; received 10 x 110 x 1,400 = 1,540,000 KRW.
    True KRW gain is 340,000 = 140,000 local (100 USD at the exit rate) + 200,000 FX.
    """
    transactions = [
        tx(TxType.BUY, "2026-01-05", "AAPL", 10, 100, currency="USD", fx_rate=1200.0),
        tx(TxType.SELL, "2026-02-05", "AAPL", 10, 110, currency="USD", fx_rate=1400.0),
    ]
    result = pe.compute_realized_pnl(transactions, fx(usd=1300.0), "2026-01-01", "2026-12-31")
    entry = result["symbols"][0]
    assert entry["realized_pnl"] == pytest.approx(100)  # local currency
    assert entry["realized_fx_krw"] == pytest.approx(200_000)
    assert entry["realized_pnl_krw"] == pytest.approx(340_000)
    # The identity must hold exactly.
    assert entry["realized_pnl_krw"] == pytest.approx(
        entry["realized_pnl"] * 1400 + entry["realized_fx_krw"]
    )
    assert result["totals"]["realized_fx_krw"] == pytest.approx(200_000)


def test_fx_component_is_none_when_the_buy_rate_was_never_recorded():
    """Toss reports no trade FX rate, so the split is unknown — not zero."""
    transactions = [
        tx(TxType.BUY, "2026-01-05", "MSFT", 10, 100, currency="USD"),
        tx(TxType.SELL, "2026-02-05", "MSFT", 10, 110, currency="USD"),
    ]
    result = pe.compute_realized_pnl(transactions, fx(usd=1300.0), "2026-01-01", "2026-12-31")
    entry = result["symbols"][0]
    assert entry["realized_fx_krw"] is None
    # Falls back to converting local P&L at current spot.
    assert entry["realized_pnl_krw"] == pytest.approx(130_000)
    assert result["totals"]["realized_fx_krw"] is None


def test_krw_positions_have_a_true_zero_fx_component():
    transactions = [
        tx(TxType.BUY, "2026-01-05", "005930", 10, 1_000),
        tx(TxType.SELL, "2026-02-05", "005930", 10, 1_200),
    ]
    result = pe.compute_realized_pnl(transactions, fx(), "2026-01-01", "2026-12-31")
    entry = result["symbols"][0]
    assert entry["realized_fx_krw"] == pytest.approx(0.0)
    assert entry["realized_pnl_krw"] == pytest.approx(2_000)


def test_fx_only_move_is_reported_even_with_no_local_gain():
    """Flat in USD but the won weakened 10%: still a KRW gain."""
    transactions = [
        tx(TxType.BUY, "2026-01-05", "AAPL", 10, 100, currency="USD", fx_rate=1200.0),
        tx(TxType.SELL, "2026-02-05", "AAPL", 10, 100, currency="USD", fx_rate=1320.0),
    ]
    result = pe.compute_realized_pnl(transactions, fx(), "2026-01-01", "2026-12-31")
    entry = result["symbols"][0]
    assert entry["realized_pnl"] == pytest.approx(0.0)
    assert entry["realized_fx_krw"] == pytest.approx(120_000)  # 1,000 USD x 120
    assert entry["realized_pnl_krw"] == pytest.approx(120_000)


def test_dividends_and_interest_are_tracked_separately_from_sales():
    transactions = [
        Transaction(
            provider="toss",
            account="toss:primary",
            tx_id="div-1",
            tx_type=TxType.DIVIDEND,
            trade_date="2026-02-10",
            currency="USD",
            symbol="AAPL",
            gross_amount=100.0,
            tax=15.0,
            net_amount=85.0,
        ),
        Transaction(
            provider="toss",
            account="toss:primary",
            tx_id="int-1",
            tx_type=TxType.INTEREST,
            trade_date="2026-02-11",
            currency="KRW",
            gross_amount=5_000.0,
            net_amount=5_000.0,
        ),
    ]
    result = pe.compute_realized_pnl(transactions, fx(), "2026-01-01", "2026-12-31")
    assert result["symbols"] == []
    assert result["totals"]["dividends"] == pytest.approx(85.0)
    assert result["totals"]["dividends_krw"] == pytest.approx(110_500)  # 85 x 1300
    assert result["totals"]["interest"] == pytest.approx(5_000.0)
    assert result["totals"]["taxes_by_currency"]["USD"] == pytest.approx(15.0)


# ── Consolidation ────────────────────────────────────────


def holding(account: str, symbol: str, quantity: float, avg: float, last: float, currency="KRW") -> Holding:
    return Holding(
        provider=account.split(":")[0],
        account=account,
        symbol=symbol,
        currency=currency,
        quantity=quantity,
        avg_price=avg,
        cost_basis=avg * quantity,
        market_value=last * quantity,
        last_price=last,
        market="KR" if currency == "KRW" else "US",
    )


def test_same_symbol_in_two_accounts_merges_with_quantity_weighted_average():
    holdings = [
        holding("toss:primary", "005930", 100, 60_000, 70_000),
        holding("kis:domestic", "005930", 300, 80_000, 70_000),
    ]
    result = pe.consolidate_holdings(holdings, fx())
    assert result["position_count"] == 1
    position = result["positions"][0]
    assert position["quantity"] == pytest.approx(400)
    # (100x60,000 + 300x80,000) / 400 = 75,000 — not the 70,000 average-of-averages.
    assert position["avg_price"] == pytest.approx(75_000)
    assert position["cost_basis"] == pytest.approx(30_000_000)
    assert position["market_value"] == pytest.approx(28_000_000)
    assert position["unrealized_pnl"] == pytest.approx(-2_000_000)
    assert position["account_count"] == 2
    assert {a["account"] for a in position["accounts"]} == {"toss:primary", "kis:domestic"}


def test_consolidation_keeps_currencies_separate_and_converts_to_krw():
    holdings = [
        holding("kis:domestic", "005930", 10, 70_000, 70_000),
        holding("kis:overseas", "AAPL", 10, 100, 200, currency="USD"),
    ]
    result = pe.consolidate_holdings(holdings, fx(usd=1300.0))
    by_symbol = {p["symbol"]: p for p in result["positions"]}
    assert by_symbol["AAPL"]["market_value_krw"] == pytest.approx(2_000 * 1300)
    assert result["totals"]["market_value_krw"] == pytest.approx(700_000 + 2_600_000)
    assert set(result["by_currency"]) == {"KRW", "USD"}
    # Weights are computed on the KRW-normalized values.
    assert by_symbol["AAPL"]["weight_pct"] == pytest.approx(78.7879, abs=1e-3)


def test_missing_fx_rate_marks_currency_missing_rather_than_assuming_parity():
    holdings = [holding("kis:overseas", "7203", 10, 2_000, 3_000, currency="JPY")]
    rates = fx(jpy=None)
    result = pe.consolidate_holdings(holdings, rates)
    position = result["positions"][0]
    assert position["market_value"] == pytest.approx(30_000)  # native JPY preserved
    assert position["market_value_krw"] is None
    assert "JPY" in result["fx"]["missing_currencies"]


def test_zero_quantity_holdings_are_dropped():
    result = pe.consolidate_holdings([holding("kis:domestic", "005930", 0, 1, 1)], fx())
    assert result["position_count"] == 0
    assert result["totals"]["market_value_krw"] is None


# ── Portfolio summary ────────────────────────────────────


def test_summary_totals_stock_plus_cash_with_account_breakdown():
    holdings = [
        holding("kis:domestic", "005930", 10, 60_000, 70_000),
        holding("toss:primary", "AAPL", 10, 100, 150, currency="USD"),
    ]
    cash = [
        CashBalance(provider="kis", account="kis:domestic", currency="KRW", amount=500_000),
        CashBalance(provider="toss", account="toss:primary", currency="USD", amount=1_000),
    ]
    summary = pe.summarize_portfolio(holdings, cash, fx(usd=1300.0))
    assert summary["stock_value_krw"] == pytest.approx(700_000 + 1_500 * 1300)
    assert summary["cash_krw"] == pytest.approx(500_000 + 1_000 * 1300)
    assert summary["total_assets_krw"] == pytest.approx(
        summary["stock_value_krw"] + summary["cash_krw"]
    )
    assert summary["cost_basis_krw"] == pytest.approx(600_000 + 1_000 * 1300)
    assert summary["by_account"]["toss:primary"]["cash_krw"] == pytest.approx(1_300_000)
    assert summary["cash_by_currency"] == {"KRW": 500_000, "USD": 1_000}


# ── XIRR / TWR / risk statistics ─────────────────────────


def test_xirr_recovers_a_known_annual_return():
    """-1,000 today, +1,100 in exactly one year is a 10% annualized return."""
    rate = pe.xirr([("2025-01-01", -1000.0), ("2026-01-01", 1100.0)])
    assert rate == pytest.approx(0.10, abs=1e-3)


def test_xirr_handles_multiple_contributions():
    rate = pe.xirr(
        [("2025-01-01", -1000.0), ("2025-07-01", -1000.0), ("2026-01-01", 2200.0)]
    )
    assert rate is not None and 0.10 < rate < 0.25


def test_xirr_returns_none_without_a_sign_change():
    assert pe.xirr([("2025-01-01", -1000.0), ("2026-01-01", -500.0)]) is None
    assert pe.xirr([("2025-01-01", 1000.0)]) is None


def test_twr_is_unaffected_by_a_mid_period_deposit():
    """NAV doubling from a deposit is not a return."""
    navs = [
        {"snapshot_date": "2026-01-01", "total_value_krw": 1_000_000},
        {"snapshot_date": "2026-01-02", "total_value_krw": 2_000_000},
    ]
    assert pe.time_weighted_return(navs, {"2026-01-02": 1_000_000}) == pytest.approx(0.0)
    # Same NAV move with no deposit is a genuine +100%.
    assert pe.time_weighted_return(navs, {}) == pytest.approx(1.0)


def test_twr_chains_sub_period_returns():
    navs = [
        {"snapshot_date": "2026-01-01", "total_value_krw": 100.0},
        {"snapshot_date": "2026-01-02", "total_value_krw": 110.0},
        {"snapshot_date": "2026-01-03", "total_value_krw": 99.0},
    ]
    # 1.10 x 0.90 - 1 = -0.01
    assert pe.time_weighted_return(navs, {}) == pytest.approx(-0.01)


def test_nav_statistics_max_drawdown_and_extremes():
    navs = [
        {"snapshot_date": "2026-01-01", "total_value_krw": 100.0},
        {"snapshot_date": "2026-01-02", "total_value_krw": 120.0},
        {"snapshot_date": "2026-01-03", "total_value_krw": 60.0},
        {"snapshot_date": "2026-01-04", "total_value_krw": 90.0},
    ]
    stats = pe.nav_statistics(navs, flows_by_date={})
    assert stats["observations"] == 4
    assert stats["max_drawdown_pct"] == pytest.approx(-50.0)
    assert stats["max_drawdown_peak_date"] == "2026-01-02"
    assert stats["max_drawdown_trough_date"] == "2026-01-03"
    assert stats["worst_day_pct"] == pytest.approx(-50.0)
    assert stats["best_day_pct"] == pytest.approx(50.0)
    assert stats["volatility_annual_pct"] is not None


def test_nav_statistics_needs_two_observations():
    stats = pe.nav_statistics([{"snapshot_date": "2026-01-01", "total_value_krw": 100.0}])
    assert stats["max_drawdown_pct"] is None
    assert "2 NAV snapshots" in stats["note"]


def test_daily_and_monthly_pnl_nets_out_flows():
    navs = [
        {"snapshot_date": "2026-01-31", "total_value_krw": 1_000_000},
        {"snapshot_date": "2026-02-01", "total_value_krw": 1_500_000},
        {"snapshot_date": "2026-02-02", "total_value_krw": 1_400_000},
    ]
    series = pe.daily_and_monthly_pnl(navs, {"2026-02-01": 400_000})
    assert {key: series["daily"][0][key] for key in ("date", "pnl_krw", "return_pct", "net_flow_krw")} == {
        "date": "2026-02-01",
        "pnl_krw": 100_000,
        "return_pct": pytest.approx(10.0),
        "net_flow_krw": 400_000,
    }
    assert series["monthly"]["2026-02"] == pytest.approx(0.0)  # +100,000 then -100,000


# ── Cash flows and performance assembly ──────────────────


def test_summarize_cashflows_totals_by_category_in_krw():
    transactions = [
        tx(TxType.BUY, "2026-02-01", "AAPL", 10, 100, currency="USD", fee=5, fx_rate=1300.0),
        tx(TxType.SELL, "2026-02-10", "AAPL", 5, 120, currency="USD", fee=3, fx_rate=1300.0),
        tx(TxType.BUY, "2026-01-01", "005930", 1, 1000),  # outside the window
    ]
    flows = pe.summarize_cashflows(transactions, fx(), "2026-02-01", "2026-02-28")
    assert flows["buy_amount_krw"] == pytest.approx(1_005 * 1300)
    assert flows["sell_amount_krw"] == pytest.approx(597 * 1300)
    assert flows["fees_krw"] == pytest.approx(8 * 1300)
    assert flows["net_deposits_krw"] is None  # no broker deposit data


def test_compute_performance_combines_navs_realized_and_returns():
    transactions = [
        tx(TxType.BUY, "2026-01-05", "005930", 10, 1_000),
        tx(TxType.SELL, "2026-02-05", "005930", 5, 1_400),
    ]
    navs = [
        {"snapshot_date": "2026-01-01", "total_value_krw": 1_000_000},
        {"snapshot_date": "2026-02-01", "total_value_krw": 1_100_000},
    ]
    realized = pe.compute_realized_pnl(transactions, fx(), "2026-01-01", "2026-03-01")
    result = pe.compute_performance(
        transactions,
        navs,
        current_nav_krw=1_200_000,
        fx=fx(),
        start="2026-01-01",
        end="2026-03-01",
        realized=realized,
        unrealized_krw=50_000,
        external_cashflows_coverage={"status": "verified_zero", "source": "synthetic_confirmation",
                                    "start": "2026-01-01", "end": "2026-03-01"},
    )
    assert result["start_nav_krw"] == pytest.approx(1_000_000)
    assert result["end_nav_krw"] == pytest.approx(1_200_000)
    assert result["profit_krw"] == pytest.approx(200_000)
    assert result["returns"]["simple_return_pct"] == pytest.approx(20.0)
    assert result["pnl"]["realized_krw"] == pytest.approx(2_000)  # 5 x (1,400 - 1,000)
    assert result["pnl"]["unrealized_krw"] == pytest.approx(50_000)
    assert result["risk"]["observations"] == 2


def test_compute_performance_without_snapshots_reports_nulls_not_zeros():
    result = pe.compute_performance(
        [], [], current_nav_krw=1_000_000, fx=fx(), start="2026-01-01", end="2026-03-01"
    )
    assert result["start_nav_krw"] is None
    assert result["returns"]["twr_pct"] is None
    assert result["returns"]["simple_return_pct"] is None
    assert result["nav_observations"] == 0
