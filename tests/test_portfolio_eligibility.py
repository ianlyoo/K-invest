"""Settlement reference values and performance eligibility use synthetic inputs only."""
from dataclasses import replace

import pytest

import portfolio_engine as pe
from portfolio_model import (
    CashBalance,
    FxRates,
    Holding,
    Transaction,
    TxType,
    kis_overseas_cash,
    toss_cash,
)
from portfolio_sources import Collection

NAVS = [
    {"snapshot_date": "2026-10-02", "total_value_krw": 100_000_000},
    {"snapshot_date": "2026-10-06", "total_value_krw": 110_000_000},
]
COVERAGE = {"status": "verified_zero", "start": "2026-10-02", "end": "2026-10-06", "source": "synthetic_confirmation"}


def performance(transactions=(), **kwargs):
    return pe.compute_performance(transactions, NAVS, 110_000_000, FxRates(),
                                  "2026-10-02", "2026-10-06", **kwargs)


def test_unknown_flows_do_not_report_deposit_as_return():
    result = performance()
    assert result["returns"]["simple_return_pct"] is None
    assert result["returns"]["twr_pct"] is None
    assert result["returns"]["xirr_pct"] is None
    assert result["profit_krw"] is None
    assert result["flows"]["net_deposits_krw"] is None
    assert result["raw_asset_change_pct"] == 10
    assert result["metrics"]["twr_pct"]["reason_codes"] == ["MISSING_EXTERNAL_CASHFLOWS"]
    assert result["risk"]["max_drawdown_pct"] is None
    assert result["risk"]["asset_value_drawdown_pct"] == 0


def test_verified_zero_and_complete_deposit_coverage_differ_from_unknown():
    verified = performance(external_cashflows_coverage=COVERAGE)
    assert verified["metrics"]["twr_pct"]["status"] == "available"
    assert verified["returns"]["twr_pct"] == 10
    deposit = Transaction("synthetic", "a", "d", TxType.DEPOSIT, "2026-10-06", "KRW", net_amount=10_000_000)
    covered = performance([deposit], external_cashflows_coverage={**COVERAGE, "status": "complete"})
    assert covered["returns"]["simple_return_pct"] == 0
    assert covered["returns"]["twr_pct"] == 0
    assert covered["returns"]["xirr_pct"] == 0
    assert covered["risk"]["best_day_pct"] is None
    assert "NON_DAILY_NAV_SERIES" in covered["risk"]["metrics"]["best_day_pct"]["reason_codes"]


def test_incomplete_coverage_and_missing_flow_fx_have_recovery_reasons():
    short = performance(external_cashflows_coverage={**COVERAGE, "end": "2026-10-03"})
    assert "INCOMPLETE_EXTERNAL_CASHFLOW_COVERAGE" in short["metrics"]["twr_pct"]["reason_codes"]
    deposit = Transaction("synthetic", "a", "d", TxType.DEPOSIT, "2026-10-06", "USD", net_amount=100)
    result = performance([deposit], external_cashflows_coverage={**COVERAGE, "status": "complete"})
    assert result["returns"]["twr_pct"] is None
    assert "MISSING_FLOW_FX_RATE" in result["metrics"]["twr_pct"]["reason_codes"]


def test_failed_sources_and_incomplete_cost_basis_propagate():
    result = performance(external_cashflows_coverage=COVERAGE,
                         input_quality={"sources_failed": ["a"]},
                         realized={"totals": {"realized_pnl_krw": 1}, "incomplete_cost_basis": ["7203"]})
    assert result["returns"]["twr_pct"] is None
    assert "FAILED_SOURCES" in result["metrics"]["twr_pct"]["reason_codes"]
    assert result["pnl"]["realized_krw"] is None
    assert result["metrics"]["realized_krw"]["reason_codes"] == ["INCOMPLETE_COST_BASIS", "FAILED_SOURCES"]


def test_unknown_daily_flows_are_null_with_raw_changes():
    result = pe.daily_and_monthly_pnl(NAVS, None)
    assert result["daily"][0]["net_flow_krw"] is None
    assert result["daily"][0]["pnl_krw"] is None
    assert result["daily"][0]["raw_asset_change_krw"] == 10_000_000
    assert result["monthly"]["2026-10"] is None


def test_kis_settlement_bridge_counts_each_obligation_once_and_is_stable():
    cash = kis_overseas_cash({"cash": [
        {"crcy_cd": "JPY", "frcr_dncl_amt_2": "2000000", "frcr_buy_amt_smtl": "1720548", "frcr_sll_amt_smtl": "0"},
        {"crcy_cd": "USD", "frcr_dncl_amt_2": "0", "frcr_buy_amt_smtl": "0", "frcr_sll_amt_smtl": "5182.25"},
    ]})
    fx = FxRates({"JPY": 8.5, "USD": 1350.1})
    holding = Holding("kis", "kis:overseas", "7203", "JPY", 1, market_value=1720548)
    before = pe.summarize_portfolio([holding], cash, fx)
    bridge = before["nav_bridge"]
    expected = -1720548 * 8.5 + 5182.25 * 1350.1
    assert bridge["adjustment_total_krw"] == pytest.approx(expected, abs=0.01)
    assert len(bridge["adjustments"]) == 2
    assert bridge["settlement_adjusted_nav_krw"] == pytest.approx(before["total_assets_krw"] + expected, abs=0.01)
    after_cash = [replace(cash[0], amount=2000000-1720548, settled_cash=2000000-1720548, unsettled_payables=0),
                  replace(cash[1], amount=5182.25, settled_cash=5182.25, unsettled_receivables=0)]
    after = pe.summarize_portfolio([holding], after_cash, fx)
    assert after["nav_bridge"]["settlement_adjusted_nav_krw"] == bridge["settlement_adjusted_nav_krw"]
    assert cash[0].to_dict()["unsettled_payables"] == 1720548


def test_included_unsettled_cash_is_not_adjusted_again():
    balance = CashBalance("synthetic", "a", "KRW", 110, settled_cash=100,
                          unsettled_receivables=10, unsettled_payables=0,
                          native_cash_includes_unsettled=True, positions_basis="trade_date")
    bridge = pe.summarize_portfolio([], [balance], FxRates())["nav_bridge"]
    assert bridge["adjustment_total_krw"] == 0
    assert bridge["settlement_adjusted_nav_krw"] == 110


def test_toss_buying_power_and_missing_settlement_fields_are_unknown():
    cash = toss_cash({"cashBuyingPower": 100}, "synthetic", "KRW")[0].to_dict()
    assert cash["settled_cash"] is None
    assert cash["buying_power"] == 100
    assert cash["cash_field_status"]["unsettled_receivables"]["reason"]
    bridge = pe.summarize_portfolio([], [CashBalance("kis", "a", "USD", 100)], FxRates())["nav_bridge"]
    assert bridge["settlement_adjusted_nav_krw"] is None
    assert "MISSING_FX_RATE" in bridge["reconciliation"]["difference_reasons"]


@pytest.mark.parametrize("status,expected", [("error", "unavailable"), ("ok", "empty")])
def test_performance_distinguishes_failed_and_successfully_empty_sources(monkeypatch, status, expected):
    import server
    empty = Collection(health={"synthetic": {"status": status}})
    class Collector:
        def collect_positions(self):
            return empty
        def collect_transactions(self, *args):
            return empty
        def build_fx(self, *args):
            return FxRates()
    class Store:
        def range(self, *args):
            return []
    monkeypatch.setattr(server, "_get_collector", lambda: Collector())
    monkeypatch.setattr(server, "_get_nav_store", lambda: Store())
    monkeypatch.setattr(server, "_risk_free_decimal", lambda: 0)
    result = server.get_portfolio_performance()
    assert result["ok"] is True
    assert result["data"]["result_status"] == expected
    assert result["result_status"] == expected


def test_history_has_limitations_and_unavailable_verified_metrics(monkeypatch):
    import server
    class Store:
        def range(self, *args):
            return NAVS
        def collection_health(self, *args):
            return {"expected_day_gaps": None}
        def bounds(self):
            return "2026-10-02", "2026-10-06"
        def count(self):
            return 2
    monkeypatch.setattr(server, "_get_nav_store", lambda: Store())
    monkeypatch.setattr(server, "_risk_free_decimal", lambda: 0)
    result = server.get_portfolio_history(detail_level="full")["data"]
    assert result["limitations"]
    assert result["result_status"] == "partial"
    assert result["statistics"]["max_drawdown_pct"] is None
    assert "MISSING_EXTERNAL_CASHFLOWS" in result["statistics"]["metrics"]["sharpe"]["reason_codes"]
    assert result["pnl_series"]["daily"][0]["net_flow_krw"] is None


def test_backlog_reference_nav_arithmetic_with_synthetic_implied_fx():
    # The backlog omits audit FX rates. Engineer a rate matching its arithmetic;
    # this proves the bridge mechanism, not a reconstruction of broker evidence.
    usd = 1464.3
    jpy = (7_634_745 + 5182.25 * usd) / 1720548
    cash = kis_overseas_cash({"cash": [
        {"crcy_cd": "JPY", "frcr_dncl_amt_2": "2000000", "frcr_buy_amt_smtl": "1720548", "frcr_sll_amt_smtl": "0"},
        {"crcy_cd": "USD", "frcr_dncl_amt_2": "0", "frcr_buy_amt_smtl": "0", "frcr_sll_amt_smtl": "5182.25"},
    ], "summary": [{"tot_asst_amt": "919789870.32", "ustl_buy_amt_smtl": "1720548"}]})
    holding = Holding("kis", "kis:overseas", "synthetic", "KRW", 1,
                      market_value=927424615.32-2000000*jpy)
    result = pe.summarize_portfolio([holding], cash, FxRates({"USD": usd, "JPY": jpy}))
    assert result["total_assets_krw"] == 927424615.32
    assert result["nav_bridge"]["adjustment_total_krw"] == -7634745
    assert result["nav_bridge"]["settlement_adjusted_nav_krw"] == 919789870.32
    assert result["nav_bridge"]["reconciliation"]["status"] == "partial"
    assert len(result["nav_bridge"]["adjustments"]) == 2
    assert len(result["nav_bridge"]["broker_reported_totals"]) == 1
    assert result["nav_bridge"]["reconciliation"]["difference_from_official_nav_krw"] is None


def test_missing_nav_values_and_unclassified_transfers_disable_returns():
    navs = NAVS + [{"snapshot_date": "2026-10-03", "total_value_krw": None}]
    result = pe.compute_performance([], navs, 110_000_000, FxRates(), "2026-10-02", "2026-10-06",
                                    external_cashflows_coverage=COVERAGE)
    assert result["returns"]["twr_pct"] is None
    assert "MISSING_NAV_VALUE" in result["metrics"]["twr_pct"]["reason_codes"]
    transfer = Transaction("synthetic", "a", "t", TxType.TRANSFER, "2026-10-06", "KRW", net_amount=100)
    result = performance([transfer], external_cashflows_coverage={**COVERAGE, "status": "complete"})
    assert result["returns"]["simple_return_pct"] is None
    assert "UNCLASSIFIED_TRANSFER" in result["metrics"]["simple_return_pct"]["reason_codes"]


def test_verified_daily_deposit_does_not_create_return_risk():
    navs = [
        {"snapshot_date": "2026-10-02", "total_value_krw": 100_000_000},
        {"snapshot_date": "2026-10-03", "total_value_krw": 110_000_000},
        {"snapshot_date": "2026-10-04", "total_value_krw": 110_000_000},
    ]
    stats = pe.nav_statistics(navs, flows_by_date={"2026-10-03": 10_000_000})
    assert stats["volatility_annual_pct"] == 0
    assert stats["max_drawdown_pct"] == 0
    assert stats["return_annual_pct"] == 0
    assert stats["best_day_pct"] == 0
    assert stats["asset_value_volatility_annual_pct"] > 0
    assert stats["metrics"]["sharpe"]["reason_codes"] == ["ZERO_RETURN_VOLATILITY"]


def test_cash_field_separation_and_missing_endpoints():
    cash = kis_overseas_cash({"cash": [{"crcy_cd": "USD", "frcr_dncl_amt_2": "100",
                                      "frcr_drwg_psbl_amt_1": "50", "sll_ruse_psbl_amt": "25"}]})[0].to_dict()
    assert cash["settled_cash"] == 100
    assert cash["withdrawable_cash"] == 50
    assert cash["buying_power"] is None
    assert cash["unsettled_receivables"] is None
    result = pe.compute_performance([], [], None, FxRates(), "2026-10-02", "2026-10-06",
                                    external_cashflows_coverage=COVERAGE)
    assert result["metrics"]["simple_return_pct"]["reason_codes"] == ["MISSING_START_NAV", "MISSING_END_NAV"]
    assert pe.xirr([("2026-10-02", -100), ("2026-10-02", 100)]) is None


@pytest.mark.parametrize("tool_name", ["get_portfolio_performance", "get_portfolio_history"])
def test_tools_record_explicit_zero_confirmation_scope(monkeypatch, tool_name):
    import server
    navs = [{"snapshot_date": f"2026-10-0{i}", "total_value_krw": 100_000_000+i}
            for i in (2, 3, 4)]
    class Store:
        def range(self, *args):
            return navs
        def collection_health(self, *args):
            return {"expected_day_gaps": None}
        def bounds(self):
            return "2026-10-02", "2026-10-04"
        def count(self):
            return 3
    class Collector:
        def collect_positions(self):
            return Collection(cash=[CashBalance("synthetic", "a", "KRW", 100_000_004)],
                              health={"a": {"status": "ok"}})
        def collect_transactions(self, *args):
            return Collection(health={"a": {"status": "ok"}})
        def build_fx(self, *args):
            return FxRates()
    monkeypatch.setattr(server, "_get_nav_store", lambda: Store())
    monkeypatch.setattr(server, "_get_collector", lambda: Collector())
    monkeypatch.setattr(server, "_risk_free_decimal", lambda: 0)
    monkeypatch.setattr(server.portfolio_engine, "resolve_period", lambda _: ("2026-10-02", "2026-10-04"))
    data = getattr(server, tool_name)(external_cashflows_verified_zero=True)["data"]
    metrics = data["statistics"] if tool_name.endswith("history") else data["risk"]
    metric = metrics["metrics"]["max_drawdown_pct"]
    assert metric["status"] == "available"
    coverage = metric["coverage"]["external_cashflows"]
    assert coverage == {"status": "verified_zero", "start": "2026-10-02", "end": "2026-10-04",
                        "source": "caller_confirmation", "scope": "whole_portfolio"}
