"""Chat defaults stay <= 27 KB on the live shape (30 KB budget, 3 KB headroom).

Synthetic bodies with the live key structure (2.15.0) and synthetic numbers.
Every summary keeps the numbers the answer needs; every reason code in the full object is
either still in the summary or tallied in meta.detail.omitted_reason_codes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_merge_equality as fakes  # noqa: E402

import response_shape  # noqa: E402
import server  # noqa: E402
import tools_v2  # noqa: E402

LIMIT = 27_000  # bytes; the stricter reading of 27 KB


@pytest.fixture
def stub(monkeypatch, tmp_path):
    return fakes.install_stub(monkeypatch, tmp_path)


def _size(env):
    return len(response_shape.compact_json(env).encode())


def _codes(node):
    return set(tools_v2._reason_codes(node, {}))


def _kept_or_tallied(full, summary):
    tallied = {code for codes in summary["meta"]["detail"].get("omitted_reason_codes", {}).values() for code in codes}
    return _codes(full["data"]) - _codes(summary["data"]) - tallied


# ── T4 get_portfolio_returns ─────────────────────────────────────────

NAV_CODES = ["FLOW_COVERAGE_GAP", "RECONCILIATION_BREAK", "UNKNOWN_FX_PRICE_TIME", "MISSING_START_NAV"]


def _coverage(extra=()):
    return {"external_cashflows": {"status": "unavailable", "source": "permanent_ledger", "scope": "equity",
                                   "reason_codes": ["ACCOUNT_FLOW_COVERAGE_MISSING"], "accounts": ["toss:primary"] * 8},
            "input_quality": {"status": "partial", "reconciliation_breaks": [
                {"symbol": "SYNA", "status": "unexplained", "opening_status": "unknown", "quantity": 1.0,
                 "note": "x" * 400}]},
            "snapshot_dates": [f"2026-0{1 + i % 9}-{10 + i % 18}" for i in range(48)],
            "nav_observations": 2, "snapshot_calculation_versions": ["nav_v3"],
            **dict(extra)}


def _metric(codes, coverage_codes):
    return {"value": None, "status": "unavailable", "reason_codes": codes, "method_version": "flow_eligibility_v1",
            "required_inputs": ["start_nav", "end_nav"],
            "coverage": _coverage({"covered_window": None, "covered_windows": [],
                                   "first_uncovered_gap": {"start": "2026-01-01", "end": "2026-03-31", "days": 90},
                                   "reason_codes": coverage_codes})}


def _interval(i):
    codes = ["KIS_CAPTURE_MISSING", "FLOW_TIMING_UNKNOWN"] if i == 30 else ["KIS_CAPTURE_MISSING"]
    return {"t0": f"2026-09-{1 + i % 28:02d}T06:30:00+00:00", "t1": f"2026-09-{2 + i % 27:02d}T06:30:00+00:00",
            "start_date": "2026-09-01", "end_date": "2026-09-02", "start_snapshot_id": f"s{i:030d}",
            "end_snapshot_id": f"s{i + 1:030d}", "start_nav_krw": 1000.0 + i, "end_nav_krw": 1001.0 + i,
            "return_pct": 0.1, "sensitivity_bounds_pct": [0.05, 0.15], "flows_krw": 0.0, "timing": "known",
            "confidence": "observed", "methods": ["dietz"], "bridged": False, "reason_codes": codes,
            "missing_scopes": [], "label": "estimate"}


def _realized_row(i):
    """One realized_by_symbol row with the live keys (2.15.1), synthetic numbers."""
    return {"symbol": f"SYN{i}", "name": "합성 종목 이름", "currency": "USD", "market": "US", "quantity_sold": 1.0,
            "proceeds": 10.0 + i, "cost_basis": 9.0, "realized_pnl": 1.0 + i, "realized_pnl_krw": 1400.0 * i,
            "realized_fx_krw": None, "fees": 0.1, "taxes": 0.0, "sell_count": 1, "cost_basis_complete": i != 3,
            "reason_codes": ["TAXES_UNSUPPORTED"] + (["FEES_UNKNOWN"] if i == 7 else []),
            "fees_status": "reported", "taxes_status": "unknown", "realized_pnl_pct": 1.5, "status": "partial"}


def _performance(**kwargs):
    """get_portfolio_performance's live key structure, synthetic numbers (31 intervals: the engine's summary cap)."""
    period_codes = ["MISSING_START_NAV", "KIS_CAPTURE_MISSING"]
    metrics = {name: _metric(NAV_CODES, NAV_CODES) for name in ("simple_return_pct", "profit_krw", "twr_pct", "xirr_pct")}
    metrics["realized_krw"] = _metric(["FEES_UNKNOWN", "RECONCILIATION_BREAK"], ["FEES_UNKNOWN", "RECONCILIATION_BREAK"])
    # a coverage code the metric does not list itself stays in the metric's coverage
    metrics["unrealized_krw"] = _metric(["RECONCILIATION_BREAK"], ["RECONCILIATION_BREAK", "ENGINE_QUANTITY_DRIFT"])
    risk = {name: _metric(NAV_CODES + ["NON_DAILY_NAV_SERIES"], NAV_CODES + ["NON_DAILY_NAV_SERIES"])
            for name in ("max_drawdown_pct", "volatility_annual_pct", "return_annual_pct", "sharpe", "sortino",
                         "best_day_pct", "worst_day_pct")}
    ledgerless = {
        "label": "unavailable", "method": "modified_dietz_linked_twr_estimate", "scope": "equity",
        "is_exact_twr": False,
        "period": {"start": "2026-01-01", "end": "2026-10-21", "return_pct": None, "xirr_pct": None,
                   "xirr_reason_codes": ["PERIOD_RETURN_UNAVAILABLE"], "reason_codes": period_codes, "intervals": 31},
        "intervals": [_interval(i) for i in range(31)], "intervals_omitted": 4,
        "accuracy": {"status": "composite", "reason_codes": ["ACCURACY_UNMEASURED"]},
        "coverage": {"days_total": 293, "days_unresolved": 120,
                     "unresolved_days": [{"start_date": "2026-01-01", "end_date": "2026-01-09", "days": 9,
                                          "reason_codes": ["MISSING_START_NAV", "UNRESOLVED_FLOW"]}] * 9,
                     "unresolved_days_by_reason": {"MISSING_START_NAV": 9, "UNRESOLVED_FLOW": 81}},
        "reason_codes": period_codes,
        "effective_period": {"start": "2026-09-01", "end": "2026-10-21", "return_pct": 1.25,
                             "reason_codes": ["CLIPPED_TO_FIRST_SNAPSHOT"]},
        "tracked": {"start_date": "2026-09-01", "return_pct": 1.25, "reason_codes": []},
        "included_scopes": ["toss", "kis"], "excluded_scopes": [],
        "missing_scopes": [{"scope": "toss", "intervals_missing": 3, "reason_codes": ["CONSOLIDATED_SCOPE_MISSING"]}],
        "aggregation": {"levels": "each scope's KRW NAV and KRW flows " * 4},
        "explanation": "An estimate linked from stored daily NAV snapshots. " * 30,
    }
    data = {"period": kwargs["period"], "portfolio_scope": kwargs["portfolio_scope"], "start_nav_krw": 1000.0,
            "end_nav_krw": 1012.5, "profit_krw": None, "metrics": metrics,
            "risk": {"metrics": risk, "max_drawdown_pct": None, "result_status": "partial"},
            "coverage": _coverage(), "realized_by_symbol": [_realized_row(i) for i in range(10)],
            "limitations": ["Synthetic limitation text for the test body. " * 4] * 5,
            "ledgerless_returns": ledgerless, "result_status": "partial"}
    return {**server._ok(data), "result_status": "partial"}


def test_returns_summary_moves_repeated_coverage_and_ledgerless_detail_under_27_kb(stub, monkeypatch):
    monkeypatch.setattr(server, "get_portfolio_performance", _performance)
    summary = tools_v2.get_portfolio_returns()
    full = tools_v2.get_portfolio_returns(detail_level="full")
    unshaped = _performance(period="YTD", portfolio_scope="equity")["data"]
    # before the r3 moves this live shape was over the budget
    before = tools_v2._Sections()
    before.add("returns", "get_portfolio_performance", _performance(period="YTD", portfolio_scope="equity"))
    assert _size(before.envelope()) > LIMIT
    assert _size(summary) <= LIMIT, _size(summary)
    assert full["data"]["returns"] == unshaped  # full is the complete object

    returns, omitted = summary["data"]["returns"], summary["meta"]["detail"]["omitted"]
    # headline numbers, statuses and reason codes stay
    for key in ("start_nav_krw", "end_nav_krw", "result_status"):
        assert returns[key] == unshaped[key], key
    # Each realized row keeps symbol, KRW P&L, %, basis flag, status and reason codes
    assert returns["realized_by_symbol"] == [{key: row[key] for key in tools_v2._REALIZED_ROW}
                                             for row in unshaped["realized_by_symbol"]]
    assert omitted["data.returns.realized_by_symbol[] keys: cost_basis,fees,fees_status,market,name,proceeds,"
                   "quantity_sold,realized_fx_krw,realized_pnl,sell_count,taxes,taxes_status"] == 10
    for name, metric in returns["metrics"].items():
        assert "method_version" not in metric and "required_inputs" not in metric, name
    for name, metric in unshaped["metrics"].items():
        kept = returns["metrics"][name]
        assert {key: kept[key] for key in ("value", "status", "reason_codes")} == \
            {key: metric[key] for key in ("value", "status", "reason_codes")}
        # coverage: only what differs from data.returns.coverage
        assert "snapshot_dates" not in kept.get("coverage", {})
        assert kept["coverage"]["first_uncovered_gap"] == metric["coverage"]["first_uncovered_gap"]
    assert returns["metrics"]["unrealized_krw"]["coverage"]["reason_codes"] == ["RECONCILIATION_BREAK",
                                                                                 "ENGINE_QUANTITY_DRIFT"]
    assert "reason_codes" not in returns["metrics"]["twr_pct"]["coverage"]
    assert returns["coverage"]["snapshot_dates"] == unshaped["coverage"]["snapshot_dates"]
    ledgerless = returns["ledgerless_returns"]
    for key in ("label", "period", "reason_codes", "effective_period", "tracked", "accuracy", "missing_scopes",
                "intervals_omitted"):
        assert ledgerless[key] == unshaped["ledgerless_returns"][key], key
    assert ledgerless["coverage"]["unresolved_days_by_reason"] == {"MISSING_START_NAV": 9, "UNRESOLVED_FLOW": 81}
    for key in ("intervals", "explanation", "aggregation"):
        assert key not in ledgerless
    assert omitted["data.returns.ledgerless_returns.intervals"] == 1
    # FLOW_TIMING_UNKNOWN appears only on one interval row: tallied, not lost
    tally = summary["meta"]["detail"]["omitted_reason_codes"]
    assert tally["data.returns.ledgerless_returns.intervals"]["FLOW_TIMING_UNKNOWN"] == 1
    assert _kept_or_tallied(full, summary) == set()


def test_returns_summary_moves_nothing_from_a_metric_without_coverage(stub, monkeypatch):
    def plain(**kwargs):
        return server._ok({"period": kwargs["period"], "metrics": {"twr_pct": {"value": 1.5, "status": "complete",
                                                                              "reason_codes": []}}})

    monkeypatch.setattr(server, "get_portfolio_performance", plain)
    summary = tools_v2.get_portfolio_returns()
    assert summary["data"]["returns"] == plain(period="YTD")["data"]
    assert "omitted" not in summary["meta"]["detail"]


# ── T2 get_holdings ──────────────────────────────────────────────────


def _forty_holdings(monkeypatch):
    holdings = [fakes.Holding(("kis", "toss")[i % 2], ("kis:overseas", "toss:primary")[i % 2], f"SYN{i:03d}", "USD",
                              1 + i, market="US", last_price=100.0 + i, avg_price=90.0, cost_basis=90.0 * (1 + i),
                              market_value=(100.0 + i) * (1 + i), name=f"Synthetic Holding Number {i:03d} Inc.")
                for i in range(40)]
    monkeypatch.setattr(fakes, "HOLDINGS", holdings)


def test_holdings_summary_moves_identity_stubs_and_null_fields(stub, monkeypatch):
    _forty_holdings(monkeypatch)
    summary, full = tools_v2.get_holdings(), tools_v2.get_holdings(detail_level="full")
    old = server.get_consolidated_holdings(detail_level="summary")["data"]
    assert _size(summary) <= LIMIT, _size(summary)
    holdings, omitted = summary["data"]["holdings"], summary["meta"]["detail"]["omitted"]
    assert "mark_policy" not in holdings and full["data"]["holdings"]["mark_policy"]
    assert len(holdings["positions"]) == len(old["positions"]) == 15
    for mine, row in zip(holdings["positions"], old["positions"]):
        for key in ("symbol", "quantity", "market_value", "market_value_krw", "cost_basis", "cost_basis_krw",
                    "unrealized_pnl_krw", "weight_pct", "mark", "market_value_basis"):
            assert mine[key] == row[key], key
        identity, source = mine["instrument_metadata"], row["instrument_metadata"]
        assert {key: identity.get(key) for key in source if key not in ("product_relationship", "lookthrough")} == \
            {key: value for key, value in source.items() if key not in ("product_relationship", "lookthrough")}
        assert identity["status"] == source["status"] and identity["reason_codes"] == source["reason_codes"]
        assert "product_relationship" not in identity and "lookthrough" not in identity
        assert all(value is not None for value in identity.values())
        for block in ("ledger_cost_basis", "broker_reported_cost_basis"):
            if isinstance(row.get(block), dict):
                assert mine[block]["amount"] == row[block]["amount"]  # an unknown amount stays as null
                assert {key: mine[block].get(key) for key in row[block]} == row[block]
    assert omitted[f"{tools_v2._POSITIONS}.instrument_metadata.product_relationship"] == 15
    tally = summary["meta"]["detail"]["omitted_reason_codes"]
    assert tally[f"{tools_v2._POSITIONS}.instrument_metadata.lookthrough"] == {"CONSTITUENTS_UNAVAILABLE": 15}
    assert _kept_or_tallied(full, summary) == set()
    assert summary["data"]["cash"] == {key: value for key, value in full["data"]["cash"].items() if value != []}


def test_holdings_summary_keeps_a_reported_product_relationship(stub, monkeypatch):
    real = server.instrument_metadata

    def leveraged(symbol, record, source, market="auto"):
        identity = real(symbol, record, source, market)
        identity["product_relationship"] = {**identity["product_relationship"], "underlying": "SYNX",
                                            "leverage_target": 2.0, "status": "reported", "reason_codes": []}
        return identity

    monkeypatch.setattr(server, "instrument_metadata", leveraged)
    summary = tools_v2.get_holdings()
    relationship = summary["data"]["holdings"]["positions"][0]["instrument_metadata"]["product_relationship"]
    if "$ref" in relationship:  # identical in every row: stored once
        relationship = summary["shared"][relationship["$ref"].rsplit("/", 1)[1]]
    assert relationship["leverage_target"] == 2.0 and relationship["underlying"] == "SYNX"


# ── T3 get_portfolio_summary ─────────────────────────────────────────


def test_summary_totals_move_per_account_nav_detail_and_cash_bridge_rows(stub, monkeypatch):
    _forty_holdings(monkeypatch)
    summary = tools_v2.get_portfolio_summary_v2()
    full = tools_v2.get_portfolio_summary_v2(detail_level="full")
    old = server.get_portfolio_summary(include_realized=True, snapshot=False)["data"]
    assert _size(summary) <= LIMIT, _size(summary)
    totals, omitted = summary["data"]["totals"], summary["meta"]["detail"]["omitted"]
    for key in ("account_nav", "mark_policy"):
        assert key not in totals and full["data"]["totals"][key] == old[key], key
    assert "cash_balances" not in totals["nav_bridge"] and full["data"]["totals"]["nav_bridge"]["cash_balances"]
    assert {key: omitted[f"data.totals.{key}"] for key in ("account_nav", "nav_bridge.cash_balances", "mark_policy")} \
        == {"account_nav": 1, "nav_bridge.cash_balances": 1, "mark_policy": 1}
    # the headline numbers, the per-account KRW breakdown and the bridge's reconciliation stay
    for key in ("total_assets_krw", "stock_value_krw", "cash_krw", "cost_basis_krw", "unrealized_pnl_krw",
                "by_account", "cash_by_currency"):
        assert totals[key] == old[key], key
    bridge = {key: value for key, value in old["nav_bridge"].items() if key != "cash_balances" and value != []}
    assert {key: totals["nav_bridge"][key] for key in bridge} == bridge
    assert _kept_or_tallied(full, summary) == set()
