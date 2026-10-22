"""Unknown purchase basis never becomes confirmed realized P&L."""

from dataclasses import replace

import pytest

import portfolio_engine as pe
from portfolio_model import CashBalance, FxRates, Transaction, TxType
from portfolio_sources import Collection

START, END = "2026-02-01", "2026-02-28"
FX = FxRates({"USD": 1464.3, "JPY": 9})


def trade(
    kind, symbol="AMZN", currency="USD", gross=12135, date="2026-02-06", **kwargs
):
    return Transaction(
        "synthetic",
        "synthetic",
        f"{date}-{symbol}-{kind.value}",
        kind,
        date,
        currency,
        symbol=symbol,
        quantity=60,
        gross_amount=gross,
        fee_status="verified_zero",
        tax_status="verified_zero",
        **kwargs,
    )


def mixed_sales():
    return [
        trade(TxType.BUY),
        trade(TxType.SELL, gross=12235, date="2026-02-10"),
        trade(TxType.SELL, "ACME", "JPY", gross=114586, date="2026-02-11"),
    ]


def compute(rows):
    return pe.compute_realized_pnl(rows, FX, START, END)


def test_mixed_sales_split_basis_and_null_legacy_totals():
    result = compute(mixed_sales())
    totals = result["totals"]
    assert totals["complete_basis"]["realized_pnl_by_currency"] == {"USD": 100}
    assert totals["complete_basis"]["realized_pnl_krw"] == 146430
    assert totals["complete_basis"]["sell_count"] == 1
    assert totals["complete_basis"]["symbols"] == ["AMZN"]
    # Proceeds less a partial cost is not P&L; the subset publishes proceeds instead.
    assert totals["incomplete_basis"]["realized_pnl_by_currency"] is None
    assert totals["incomplete_basis"]["realized_pnl_krw"] is None
    assert totals["incomplete_basis"]["proceeds_by_currency"] == {"JPY": 114586}
    assert totals["incomplete_basis"]["sell_count"] == 1
    assert totals["incomplete_basis"]["symbols"] == ["ACME"]
    assert totals["status"] == "partial"
    assert totals["reason_codes"] == ["INCOMPLETE_COST_BASIS"]
    assert totals["realized_pnl_krw"] is None
    assert totals["realized_pnl_by_currency"] is None
    assert (
        result["symbols"][0]["realized_pnl_krw"] is not None
    )  # diagnostic estimates retained


def test_complete_zero_pnl_and_empty_subsets_are_explicit():
    rows = [trade(TxType.BUY), trade(TxType.SELL, date="2026-02-10")]
    totals = compute(rows)["totals"]
    assert totals["status"] == "complete"
    assert totals["reason_codes"] == []
    assert totals["realized_pnl_krw"] == 0
    assert totals["realized_pnl_by_currency"] == {"USD": 0}
    assert totals["incomplete_basis"]["sell_count"] == 0
    assert totals["incomplete_basis"]["symbol_count"] == 0
    assert totals["incomplete_basis"]["realized_pnl_krw"] == 0
    assert totals["incomplete_basis"]["realized_pnl_by_currency"] == {}


def test_same_symbol_complete_and_incomplete_sales_are_separate():
    # The oversell comes first; a complete sale before a later inferred opening
    # would itself be incomplete (its average omitted the inventory held before the ledger).
    rows = [trade(TxType.SELL, gross=12135, date="2026-02-05")] + mixed_sales()[:2]
    totals = compute(rows)["totals"]
    assert totals["complete_basis"]["realized_pnl_by_currency"] == {"USD": 100}
    assert totals["incomplete_basis"]["realized_pnl_by_currency"] is None
    assert totals["incomplete_basis"]["proceeds_by_currency"] == {"USD": 12135}
    assert (
        totals["complete_basis"]["sell_count"]
        == totals["incomplete_basis"]["sell_count"]
        == 1
    )
    assert (
        totals["complete_basis"]["symbols"]
        == totals["incomplete_basis"]["symbols"]
        == ["AMZN"]
    )
    assert totals["realized_pnl_krw"] is None


def test_partial_oversell_and_missing_purchase_amount_are_incomplete():
    oversell = [
        replace(trade(TxType.BUY), quantity=30),
        trade(TxType.SELL, gross=12235, date="2026-02-10"),
    ]
    missing = [
        replace(trade(TxType.BUY), gross_amount=None),
        trade(TxType.SELL, date="2026-02-10"),
    ]
    for rows in (oversell, missing):
        totals = compute(rows)["totals"]
        assert totals["complete_basis"]["sell_count"] == 0
        assert totals["incomplete_basis"]["sell_count"] == 1
        assert totals["realized_pnl_krw"] is None
        assert "INCOMPLETE_COST_BASIS" in totals["reason_codes"]


@pytest.mark.parametrize(
    "field,status,reason",
    [("fee", "unknown", "FEES_UNKNOWN"), ("tax", "unsupported", "TAXES_UNSUPPORTED")],
)
def test_prior_buy_cost_availability_propagates_to_totals_and_performance(
    field, status, reason
):
    buy = replace(trade(TxType.BUY, date="2026-01-06"), **{field + "_status": status})
    rows = [buy, trade(TxType.SELL, date="2026-02-10")]
    result = compute(rows)
    assert result["totals"]["complete_basis"]["sell_count"] == 1
    assert result["totals"]["status"] == "partial"
    assert reason in result["totals"]["reason_codes"]
    perf = pe.compute_performance(rows, [], 100, FX, START, END, realized=result)
    assert perf["pnl"]["realized_krw"] is None
    assert reason in perf["metrics"]["realized_krw"]["reason_codes"]


def test_incomplete_sales_outside_period_do_not_taint_current_totals():
    rows = [trade(TxType.SELL, "ACME", date="2026-01-06")] + mixed_sales()[:2]
    result = compute(rows)
    assert result["totals"]["status"] == "complete"
    assert result["totals"]["realized_pnl_krw"] == 146430
    perf = pe.compute_performance(rows, [], 100, FX, START, END, realized=result)
    assert perf["pnl"]["realized_krw"] == 146430
    assert perf["metrics"]["realized_krw"]["reason_codes"] == []


def test_summary_performance_and_nav_consumers_preserve_null_and_subsets(monkeypatch):
    import server

    rows = mixed_sales()
    ledger = Collection(transactions=rows, health={"synthetic": {"status": "ok"}})
    positions = Collection(
        cash=[CashBalance("synthetic", "synthetic", "KRW", 100)],
        health={"synthetic": {"status": "ok"}},
    )

    class Collector:
        def collect_positions(self, accounts=None):
            return positions

        def collect_transactions(self, *args):
            return ledger

        def build_fx(self, *args):
            return FX

    monkeypatch.setattr(server, "_get_collector", lambda: Collector())
    monkeypatch.setattr(pe, "resolve_period", lambda _: (START, END))
    realized = server.get_realized_pnl(start_date=START, end_date=END)
    assert realized["ok"] is True
    assert realized["data"]["totals"]["status"] == "partial"
    summary = server.get_portfolio_summary(snapshot=False)
    assert summary["ok"] is True
    assert summary["data"]["realized_pnl_krw"] is None
    assert summary["data"]["realized_pnl_status"] == "partial"
    assert summary["data"]["realized_pnl_reason_codes"] == ["INCOMPLETE_COST_BASIS"]
    assert summary["data"]["realized_pnl_complete_basis"]["realized_pnl_krw"] == 146430
    assert summary["data"]["realized_pnl_incomplete_basis"]["sell_count"] == 1
    perf = pe.compute_performance(
        rows, [], 100, FX, START, END, realized=realized["data"]
    )
    assert perf["pnl"]["realized_krw"] is None
    assert perf["metrics"]["realized_krw"]["reason_codes"] == ["INCOMPLETE_COST_BASIS"]
    # snapshot_nav.py calls the no-ledger summary; legacy NAV remains independent.
    assert (
        pe.summarize_portfolio([], positions.cash, FX)["total_assets_krw"]
        == summary["data"]["total_assets_krw"]
        == 100
    )


def test_missing_fx_keeps_native_subset_but_not_a_verified_krw_zero():
    rows = mixed_sales()[:2]
    result = pe.compute_realized_pnl(rows, FxRates(), START, END)
    totals = result["totals"]
    assert totals["complete_basis"]["realized_pnl_by_currency"] == {"USD": 100}
    assert totals["complete_basis"]["realized_pnl_krw"] is None
    assert totals["realized_pnl_krw"] is None
    assert totals["status"] == "partial"
    assert totals["reason_codes"] == ["MISSING_FX_RATE"]


def test_unrelated_costs_do_not_taint_sales_and_new_position_resets_basis():
    rows = [replace(trade(TxType.BUY, "ACME"), fee_status="unknown")] + mixed_sales()[
        :2
    ]
    assert compute(rows)["totals"]["status"] == "complete"
    old = [
        replace(
            trade(TxType.BUY, date="2026-01-01"),
            gross_amount=None,
            fee_status="unknown",
        ),
        trade(TxType.SELL, date="2026-01-02"),
    ]
    totals = compute(old + mixed_sales()[:2])["totals"]
    assert totals["status"] == "complete"
    assert totals["reason_codes"] == []


@pytest.mark.parametrize(
    "field,status,reason",
    [("fee", "unsupported", "FEES_UNSUPPORTED"), ("tax", "unknown", "TAXES_UNKNOWN")],
)
def test_sell_cost_availability_keeps_complete_principal_subset_partial(
    field, status, reason
):
    rows = mixed_sales()[:2]
    rows[-1] = replace(rows[-1], **{field + "_status": status})
    result = compute(rows)
    assert result["totals"]["complete_basis"]["sell_count"] == 1
    assert result["totals"]["complete_basis"]["status"] == "partial"
    assert result["totals"]["reason_codes"] == [reason]


def test_summary_without_realized_ledger_is_unavailable_not_zero():
    summary = pe.summarize_portfolio([], [], FX)
    assert summary["realized_pnl_krw"] is None
    assert summary["realized_pnl_status"] == "unavailable"
    assert summary["realized_pnl_reason_codes"] == ["REALIZED_PNL_NOT_REQUESTED"]
    assert summary["realized_pnl_complete_basis"] is None


def test_same_symbol_different_currency_only_marks_the_incomplete_currency():
    rows = mixed_sales()[:2] + [
        trade(TxType.SELL, "AMZN", "JPY", gross=114586, date="2026-02-11")
    ]
    result = compute(rows)
    assert result["totals"]["complete_basis"]["sell_count"] == 1
    assert result["totals"]["incomplete_basis"]["sell_count"] == 1
    usd = next(row for row in result["symbols"] if row["currency"] == "USD")
    assert usd["cost_basis_complete"] is True
