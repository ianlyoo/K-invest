"""Single review round: synthetic source, valuation, money and proceeds failures."""

from dataclasses import replace

import pytest

import portfolio_engine as pe
from portfolio_model import CashBalance, FxRates, Holding, Transaction, TxType
from portfolio_sources import Collection

START, END = "2026-02-01", "2026-02-28"
FX = FxRates({"USD": 1464.3})


def trade(kind, gross=12135, date="2026-02-06", **kwargs):
    return Transaction(
        "synthetic",
        "a",
        f"{date}-{kind.value}",
        kind,
        date,
        "USD",
        symbol="AMZN",
        quantity=60,
        gross_amount=gross,
        fee_status="verified_zero",
        tax_status="verified_zero",
        **kwargs,
    )


def wire(monkeypatch, positions, ledger):
    import server

    class Collector:
        def collect_positions(self, accounts=None):
            return positions

        def collect_transactions(self, *args):
            return ledger

        def build_fx(self, *args):
            return FX

    class Store:
        def range(self, *args):
            return [
                {"snapshot_date": START, "total_value_krw": 100},
                {"snapshot_date": END, "total_value_krw": 100},
            ]

    monkeypatch.setattr(server, "_get_collector", lambda: Collector())
    monkeypatch.setattr(server, "_get_nav_store", lambda: Store())
    monkeypatch.setattr(server, "_risk_free_decimal", lambda: 0)
    monkeypatch.setattr(pe, "resolve_period", lambda _: (START, END))
    return server


@pytest.mark.parametrize(
    "health,truncated,status,reason",
    [
        ({"ledger": {"status": "error"}}, [], "unavailable", "FAILED_SOURCES"),
        (
            {"ledger": {"status": "truncated"}},
            ["ledger"],
            "partial",
            "TRUNCATED_SOURCES",
        ),
    ],
)
def test_ledger_source_quality_reaches_summary_and_both_subsets(
    monkeypatch, health, truncated, status, reason
):
    positions = Collection(
        cash=[CashBalance("synthetic", "a", "KRW", 100)],
        health={"positions": {"status": "ok"}},
    )
    ledger = Collection(health=health, truncated_sources=truncated)
    server = wire(monkeypatch, positions, ledger)
    summary = server.get_portfolio_summary(snapshot=False)
    assert summary["ok"] is True
    assert summary["data"]["realized_pnl_krw"] is None
    assert summary["data"]["realized_pnl_status"] == status
    assert reason in summary["data"]["realized_pnl_reason_codes"]
    assert summary["result_status"] == summary["data"]["result_status"] == "partial"
    assert summary["data"]["total_assets_krw"] == 100
    realized = server.get_realized_pnl(start_date=START, end_date=END)
    assert realized["ok"] is True
    assert realized["result_status"] == status
    assert realized["data"]["totals"]["status"] == status
    for field in ("complete_basis", "incomplete_basis"):
        subset = realized["data"]["totals"][field]
        assert subset["status"] == status
        assert reason in subset["reason_codes"]
        if status == "unavailable":
            assert subset["realized_pnl_krw"] is None
            assert subset["realized_pnl_by_currency"] is None


def test_successful_empty_ledger_is_empty_not_unavailable(monkeypatch):
    empty = Collection(health={"ledger": {"status": "ok"}})
    server = wire(monkeypatch, empty, empty)
    result = server.get_realized_pnl(start_date=START, end_date=END)
    assert result["data"]["totals"]["status"] == result["result_status"] == "empty"
    assert result["data"]["totals"]["realized_pnl_krw"] == 0
    assert result["data"]["totals"]["reason_codes"] == []


def test_missing_position_valuation_disables_nav_metrics(monkeypatch):
    h = Holding("synthetic", "a", "AMZN", "KRW", 1, cost_basis=100)
    positions = Collection(holdings=[h], health={"positions": {"status": "ok"}})
    empty = Collection(health={"ledger": {"status": "ok"}})
    server = wire(monkeypatch, positions, empty)
    result = server.get_portfolio_performance(external_cashflows_verified_zero=True)
    assert result["ok"] is True
    data = result["data"]
    assert data["returns"]["simple_return_pct"] is None
    assert data["profit_krw"] is None
    assert data["end_nav_krw"] is None
    assert data["raw_asset_change_krw"] is None
    for name in ("simple_return_pct", "profit_krw", "twr_pct", "xirr_pct"):
        assert data["metrics"][name]["status"] == "unavailable"
        assert "MISSING_POSITION_VALUATION" in data["metrics"][name]["reason_codes"]
    assert (
        "MISSING_POSITION_VALUATION"
        in data["risk"]["metrics"]["max_drawdown_pct"]["reason_codes"]
    )
    assert pe.summarize_portfolio([h], [], FX)["total_assets_krw"] == 0


@pytest.mark.parametrize("availability", ["unsupported", "unknown"])
def test_performance_preserves_unavailable_money(availability):
    tx = replace(
        trade(TxType.BUY, source="kis:inquire-ccnl"),
        fee_status=availability,
        tax_status=availability,
    )
    realized = pe.compute_realized_pnl([tx], FX, START, END)
    result = pe.compute_performance([tx], [], 100, FX, START, END, realized=realized)
    for section in (result["flows"], result["pnl"]):
        for field in ("fees", "taxes"):
            assert section[field + "_krw"] is None
            assert section[field + "_status"] == availability
            assert (
                field.upper() + "_" + availability.upper()
                in section[field + "_reason_codes"]
            )
        for field in ("dividends", "interest"):
            assert section[field + "_krw"] is None
            assert section[field + "_status"] == "unsupported"
            assert field.upper() + "_UNSUPPORTED" in section[field + "_reason_codes"]


def test_reported_money_and_verified_zero_keep_amounts():
    rows = [
        replace(trade(TxType.BUY), fee=1, fee_status="reported"),
        replace(trade(TxType.DIVIDEND, gross=0), net_amount=0),
        replace(trade(TxType.INTEREST, gross=1), net_amount=1),
    ]
    realized = pe.compute_realized_pnl(rows, FX, START, END)
    result = pe.compute_performance(rows, [], 100, FX, START, END, realized=realized)
    for section in (result["flows"], result["pnl"]):
        assert section["fees_krw"] == 1464.3
        assert section["fees_status"] == "reported"
        assert section["taxes_krw"] == 0
        assert section["dividends_krw"] == 0
        assert section["dividends_status"] == "verified_zero"
        assert section["interest_krw"] == 1464.3
        assert section["interest_status"] == "reported"


def test_missing_proceeds_nulls_pnl_but_preserves_purchase_basis():
    rows = [trade(TxType.BUY), trade(TxType.SELL, gross=None, date="2026-02-10")]
    result = pe.compute_realized_pnl(rows, FX, START, END)
    row = result["symbols"][0]
    assert row["cost_basis_complete"] is True
    assert row["cost_basis"] == 12135
    assert row["proceeds"] is None
    assert row["realized_pnl"] is None
    assert row["realized_pnl_krw"] is None
    assert row["realized_pnl_pct"] is None
    assert "MISSING_PROCEEDS" in row["reason_codes"]
    totals = result["totals"]
    assert totals["status"] == "partial"
    assert "MISSING_PROCEEDS" in totals["reason_codes"]
    assert totals["realized_pnl_krw"] is None
    assert totals["realized_pnl_by_currency"] is None
    assert totals["complete_basis"]["realized_pnl_by_currency"] == {"USD": None}
    assert totals["complete_basis"]["sell_count"] == 1
    assert totals["incomplete_basis"]["sell_count"] == 0


def test_partial_ledger_keeps_observed_subset_but_not_confirmed_total(monkeypatch):
    rows = [trade(TxType.BUY), trade(TxType.SELL, gross=12235, date="2026-02-10")]
    ledger = Collection(
        transactions=rows, health={"one": {"status": "ok"}, "two": {"status": "error"}}
    )
    positions = Collection(
        cash=[CashBalance("synthetic", "a", "KRW", 100)],
        health={"positions": {"status": "ok"}},
    )
    server = wire(monkeypatch, positions, ledger)
    result = server.get_realized_pnl(start_date=START, end_date=END)["data"]
    assert result["totals"]["realized_pnl_krw"] is None
    assert result["totals"]["realized_pnl_by_currency"] is None
    assert result["totals"]["complete_basis"]["realized_pnl_krw"] == 146430
    for key in ("complete_basis", "incomplete_basis"):
        assert result["totals"][key]["status"] == "partial"
        assert "FAILED_SOURCES" in result["totals"][key]["reason_codes"]


def test_missing_proceeds_contaminate_only_their_currency_and_price_fallback_works():
    buy = trade(TxType.BUY)
    valid_sell = replace(
        trade(TxType.SELL, gross=None, date="2026-02-10"), price=202.25, quantity=30
    )
    missing_sell = replace(
        trade(TxType.SELL, gross=None, date="2026-02-11"), quantity=30
    )
    result = pe.compute_realized_pnl([buy, valid_sell, missing_sell], FX, START, END)
    assert result["symbols"][0]["cost_basis"] == 12135
    assert result["symbols"][0]["realized_pnl"] is None
    assert result["totals"]["proceeds_by_currency"] == {"USD": None}
    assert result["totals"]["complete_basis"]["realized_pnl_by_currency"] == {
        "USD": None
    }
    valid = pe.compute_realized_pnl([buy, valid_sell], FX, START, END)
    assert valid["totals"]["status"] == "complete"
    assert valid["totals"]["realized_pnl_by_currency"] == {"USD": 0}


def test_income_missing_fx_is_not_zero_in_performance():
    tx = replace(trade(TxType.INTEREST, gross=1), net_amount=1)
    result = pe.compute_performance([tx], [], 100, FxRates(), START, END)
    assert result["flows"]["interest_krw"] is None
    assert result["flows"]["interest_status"] == "reported"
    assert result["flows"]["interest_reason_codes"] == ["MISSING_FX_RATE"]
    assert result["pnl"]["interest_krw"] is None
