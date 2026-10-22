"""Auditable export, using only synthetic normalized inputs and temporary state."""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import sqlite3
from dataclasses import replace
from decimal import Decimal
from importlib import import_module

import pytest

from portfolio_model import CashBalance, FxRates, Holding, Transaction, TxType
from portfolio_sources import Collection
from portfolio_store import NavStore, snapshot_summary

DAY = "2026-10-04"


def exports():
    return import_module("portfolio_export")


class SyntheticCollector:
    def __init__(self):
        self.calls = []
        self.transactions = [
            Transaction("kis", "kis:overseas", "buy-amzn", TxType.BUY, "2026-02-06", "USD",
                        symbol="AMZN", market="US", quantity=60, price=202.25, gross_amount=12135,
                        fee=30, tax=0, fee_status="reported", tax_status="verified_zero",
                        fx_rate=1464.3, settle_date="2026-02-10", net_amount=-12165,
                        account_key="kis:88776655-01", source="kis:period_trans", price_source_field="gross/quantity"),
            Transaction("kis", "kis:overseas", "sell-amzn", TxType.SELL, "2026-09-30", "USD",
                        symbol="AMZN", market="US", quantity=30, price=210, gross_amount=6300,
                        fee=5, tax=1, fee_status="reported", tax_status="reported",
                        fx_rate=1464.3, settle_date="2026-10-02", net_amount=6294,
                        account_key="kis:88776655-01", source="kis:period_trans", price_source_field="gross/quantity"),
        ]
        self.holdings = [Holding("kis", "kis:overseas", "AMZN", "USD", 30,
                                 avg_price=202.25, cost_basis=6067.5, last_price=220, market_value=6600)]
        self.cash = [CashBalance("kis", "kis:overseas", "USD", 1000,
                                 settled_cash=1000, unsettled_receivables=5182.25, unsettled_payables=0,
                                 native_cash_includes_unsettled=False, positions_basis="trade_date_reference_assumption"),
                     CashBalance("kis", "kis:overseas", "JPY", 2000000,
                                 settled_cash=2000000, unsettled_receivables=0, unsettled_payables=1720548,
                                 native_cash_includes_unsettled=False, positions_basis="trade_date_reference_assumption")]
        self.fx = FxRates({"USD": 1464.3, "JPY": 9}, as_of=DAY + "T10:00:00Z")

    def collect_positions(self, accounts=None):
        self.calls.append("positions")
        return Collection(holdings=list(self.holdings), cash=list(self.cash),
                          health={"kis:overseas": {"status": "ok"}}, fetched_at=DAY + "T10:00:00Z")

    def collect_transactions(self, start, end, accounts=None, symbol=None, **kwargs):
        self.calls.append("transactions")
        rows = [t for t in self.transactions if start <= t.trade_date <= end and (not symbol or t.symbol == symbol)]
        return Collection(transactions=rows, health={"kis:overseas:trades": {"status": "ok"}},
                          requested_window={"start": start, "end": end}, fetch_complete=True,
                          observed_window={"start": min((t.trade_date for t in rows), default=None),
                                           "end": max((t.trade_date for t in rows), default=None)},
                          fetched_at=DAY + "T10:00:00Z")

    def build_fx(self, *args):
        self.calls.append("fx")
        return self.fx


@pytest.fixture
def wired(monkeypatch, tmp_path):
    import server
    collector = SyntheticCollector()
    nav = NavStore(tmp_path / "portfolio.db")
    monkeypatch.setattr(server, "_get_collector", lambda: collector)
    monkeypatch.setattr(server, "_get_nav_store", lambda: nav)
    monkeypatch.setattr(server, "_resolve_accounts", lambda _: None)
    monkeypatch.setattr(server, "_risk_free_decimal", lambda: 0)
    return server, collector, nav, tmp_path


def install_store(wired, monkeypatch, **kwargs):
    server, _, _, path = wired
    store = exports().DatasetStore(path / "datasets.db", **kwargs)
    monkeypatch.setattr(server, "_get_dataset_store", lambda: store)
    return store


def read_all(server, first, page_size=2, format="json"):
    result = first
    rows = []
    while True:
        assert result["ok"], result["error"]
        page = result["data"]
        if format == "json":
            rows.extend(page["rows"])
        else:
            rows.extend({"row_id": r["row_id"], "record_type": r["record_type"], "data": json.loads(r["data_json"])}
                        for r in csv.DictReader(io.StringIO(page["csv"])))
        if not page["next_cursor"]:
            return rows
        result = server.export_portfolio_dataset(page["manifest"]["dataset"], dataset_id=page["dataset_id"],
                                                 cursor=page["next_cursor"], page_size=page_size, format=format)


def test_export_gap_tool_is_reachable_and_annotations_honest():
    import server
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    assert "export_portfolio_dataset" in tools
    hints = tools["export_portfolio_dataset"].annotations
    assert hints.readOnlyHint is False and hints.destructiveHint is False
    assert "local" in tools["export_portfolio_dataset"].description.lower()
    assert "orders" in tools["export_portfolio_dataset"].description.lower()


@pytest.mark.parametrize("tool", ["get_all_transactions", "get_realized_pnl", "get_consolidated_holdings", "get_portfolio_history"])
def test_common_query_ids_are_additive_without_dataset_writes(wired, tool):
    server, _, _, path = wired
    result = getattr(server, tool)()
    assert result["ok"]
    assert result["meta"]["query_id"]
    assert result["meta"]["dataset_id"] is None
    assert "snapshot_id" in result["meta"]
    assert result["meta"]["calculation_version"]
    assert not (path / "portfolio_datasets.db").exists()


def test_offline_units_cost_quantity_and_nav_reconcile_from_export_only(wired, monkeypatch):
    server, collector, _, _ = wired
    install_store(wired, monkeypatch)
    tool = server.get_realized_pnl(start_date="2026-01-01", end_date=DAY)
    nav_tool = server.get_portfolio_summary(include_realized=False, snapshot=False)
    first = server.export_portfolio_dataset("realized_pnl", start_date="2026-01-01", end_date=DAY, page_size=2)
    # Replaying and independent arithmetic must require no engine or providers.
    import portfolio_engine
    monkeypatch.setattr(collector, "collect_transactions", lambda *a, **k: pytest.fail("refetched ledger"))
    monkeypatch.setattr(collector, "collect_positions", lambda *a, **k: pytest.fail("refetched positions"))
    monkeypatch.setattr(portfolio_engine, "compute_realized_pnl", lambda *a, **k: pytest.fail("engine replay"))
    monkeypatch.setattr(portfolio_engine, "summarize_portfolio", lambda *a, **k: pytest.fail("engine replay"))
    rows = read_all(server, first)
    transactions = [r["data"] for r in rows if r["record_type"] == "transaction"]
    buys = [t for t in transactions if t["tx_type"] == "BUY"]
    sells = [t for t in transactions if t["tx_type"] == "SELL"]
    for t in transactions:
        assert Decimal(str(t["quantity"])) * Decimal(str(t["price_native"]["amount"])) == Decimal(str(t["gross_amount"]))
        assert t["price_base"]["amount"] == pytest.approx(t["price"] * t["fx_rate"])
    basis = sum(t["gross_amount"] + t["fee_reported_amount"] + t["tax_reported_amount"] for t in buys)
    purchased = sum(t["quantity"] for t in buys)
    sold = sum(t["quantity"] for t in sells)
    proceeds = sum(t["gross_amount"] - t["fee_reported_amount"] - t["tax_reported_amount"] for t in sells)
    gain = proceeds - basis * sold / purchased
    assert gain == tool["data"]["symbols"][0]["realized_pnl"]
    assert gain * sells[0]["fx_rate"] == pytest.approx(tool["data"]["totals"]["realized_pnl_krw"])
    holdings = [r["data"] for r in rows if r["record_type"] == "holding"]
    cash = [r["data"] for r in rows if r["record_type"] == "cash_balance"]
    assert purchased - sold == holdings[0]["quantity"]
    rates = first["data"]["manifest"]["policies"]["fx"]["rates_to_krw"]
    reported = sum(h["market_value"] * rates[h["currency"]] for h in holdings)
    reported += sum(c["amount"] * rates[c["currency"]] for c in cash)
    adjustment = sum((c["unsettled_receivables"] - c["unsettled_payables"]) * rates[c["currency"]]
                     for c in cash if c["native_cash_includes_unsettled"] is False)
    assert reported == nav_tool["data"]["total_assets_krw"]
    assert reported + adjustment == pytest.approx(nav_tool["data"]["nav_bridge"]["settlement_adjusted_nav_krw"])
    assert first["data"]["manifest"]["summary"]["nav"]["reported_total_assets_krw"] == reported
    assert first["data"]["manifest"]["summary"]["nav"]["settlement_adjusted_nav_krw"] == pytest.approx(reported + adjustment)


def test_all_pages_match_counts_currency_totals_coverage_and_hash(wired, monkeypatch):
    server, _, _, _ = wired
    install_store(wired, monkeypatch)
    first = server.export_portfolio_dataset("transactions", start_date="2026-01-01", end_date=DAY, page_size=2)
    rows = read_all(server, first)
    manifest = first["data"]["manifest"]
    assert len(rows) == manifest["row_count"]
    assert len({r["row_id"] for r in rows}) == len(rows)
    txs = [r["data"] for r in rows if r["record_type"] == "transaction"]
    totals = manifest["per_currency_totals"]["transaction"]["USD"]
    assert totals["gross_amount"]["amount"] == sum(t["gross_amount"] for t in txs)
    assert totals["fee"]["amount"] == sum(t["fee_reported_amount"] for t in txs)
    assert totals["tax"]["amount"] == sum(t["tax_reported_amount"] for t in txs)
    assert manifest["coverage"]["fetch_complete"] is True
    assert manifest["coverage"]["historical_coverage"] == "unknown"
    assert manifest["coverage"]["observed_window"] == {"start": "2026-02-06", "end": "2026-09-30"}
    canonical = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    assert hashlib.sha256(canonical).hexdigest() == manifest["rows_sha256"]


def test_frozen_pages_byte_identical_after_recollection_and_csv_matches_json(wired, monkeypatch):
    server, collector, _, _ = wired
    install_store(wired, monkeypatch)
    first = server.export_portfolio_dataset("transactions", start_date="2026-01-01", end_date=DAY, page_size=2)
    dataset_id = first["data"]["dataset_id"]
    captured = json.dumps(first, sort_keys=True)
    collector.transactions[0] = replace(collector.transactions[0], gross_amount=12000)
    replay = server.export_portfolio_dataset("transactions", dataset_id=dataset_id, page_size=2)
    assert json.dumps(replay, sort_keys=True) == captured
    json_rows = read_all(server, replay)
    csv_first = server.export_portfolio_dataset("transactions", dataset_id=dataset_id, page_size=2, format="csv")
    assert read_all(server, csv_first, format="csv") == json_rows
    changed = server.export_portfolio_dataset("transactions", start_date="2026-01-01", end_date=DAY, page_size=2)
    assert changed["data"]["dataset_id"] != dataset_id


def test_dataset_expiry_cursor_validation_and_no_silent_refetch(wired, monkeypatch):
    server, collector, _, _ = wired
    install_store(wired, monkeypatch, max_datasets=1)
    first = server.export_portfolio_dataset("transactions", start_date="2026-01-01", end_date=DAY, page_size=1)
    collector.transactions[0] = replace(collector.transactions[0], fee=31)
    second = server.export_portfolio_dataset("transactions", start_date="2026-01-01", end_date=DAY, page_size=1)
    before = list(collector.calls)
    expired = server.export_portfolio_dataset("transactions", dataset_id=first["data"]["dataset_id"])
    assert expired["error"]["code"] == "DATASET_EXPIRED"
    assert collector.calls == before
    invalid = server.export_portfolio_dataset("transactions", dataset_id=second["data"]["dataset_id"],
                                               cursor=first["data"]["next_cursor"])
    assert invalid["error"]["code"] == "INVALID_DATASET_CURSOR"
    assert collector.calls == before


def test_default_export_removes_raw_accounts_and_credential_shaped_values(wired, monkeypatch):
    server, collector, _, _ = wired
    store = install_store(wired, monkeypatch)
    collector.transactions[0] = replace(collector.transactions[0], note="Bearer synthetic-secret-token",
                                       name="88776655-01", tx_id="88776655-01")
    first = server.export_portfolio_dataset("transactions", start_date="2026-01-01", end_date=DAY, page_size=200)
    rows = read_all(server, first, page_size=200)
    serialized = json.dumps(first) + json.dumps(rows)
    assert "88776655" not in serialized
    assert "synthetic-secret-token" not in serialized
    assert "Bearer " not in serialized
    accounts = {r["data"]["account_id"] for r in rows if r["record_type"] in {"transaction", "holding", "cash_balance"}}
    assert len(accounts) == 1 and next(iter(accounts)).startswith("acct_")
    # Reopening state retains the account namespace; inspect IDs, never the salt.
    monkeypatch.setattr(server, "_get_dataset_store", lambda: exports().DatasetStore(store.db_path))
    repeat = server.export_portfolio_dataset("transactions", start_date="2026-01-01", end_date=DAY)
    assert repeat["data"]["dataset_id"] == first["data"]["dataset_id"]


def test_unknown_costs_stay_null_with_status(wired, monkeypatch):
    server, collector, _, _ = wired
    install_store(wired, monkeypatch)
    collector.transactions[0] = replace(collector.transactions[0], fee=0, fee_status="unsupported")
    first = server.export_portfolio_dataset("transactions", start_date="2026-01-01", end_date=DAY)
    rows = read_all(server, first, page_size=200)
    buy = next(r["data"] for r in rows if r["record_type"] == "transaction" and r["data"]["tx_type"] == "BUY")
    assert buy["fee"] is None and buy["fee_status"] == "unsupported"
    fee = first["data"]["manifest"]["per_currency_totals"]["transaction"]["USD"]["fee"]
    assert fee["amount"] is None and fee["status"] == "partial"
    assert fee["observed_amount"] == 5
    net = first["data"]["manifest"]["per_currency_totals"]["transaction"]["USD"]["net_amount"]
    assert net["amount"] is None and net["status"] == "partial"


def test_nav_export_pins_versions_and_never_refetches_providers(wired, monkeypatch):
    server, collector, nav, _ = wired
    install_store(wired, monkeypatch)
    import portfolio_engine
    positions = collector.collect_positions()
    summary = portfolio_engine.summarize_portfolio(positions.holdings, positions.cash, collector.fx)
    snapshot = nav.upsert_snapshot(snapshot_summary(summary, positions, collector.fx), DAY)
    before = list(collector.calls)
    first = server.export_portfolio_dataset("nav_history", start_date=DAY, end_date=DAY, page_size=1)
    assert collector.calls == before
    nav.upsert_snapshot(dict(summary, total_assets_krw=123), DAY)
    rows = read_all(server, first, page_size=1)
    stored = next(r["data"] for r in rows if r["record_type"] == "nav_snapshot")
    assert stored["snapshot_id"] == snapshot["snapshot_id"] and stored["version"] == 1
    assert stored["total_value_krw"] == snapshot["total_value_krw"]
    assert first["data"]["manifest"]["snapshot_versions"]["count"] == 1
    assert first["meta"]["snapshot_id"] == snapshot["snapshot_id"]


def test_store_is_additive_and_immutable(wired, monkeypatch):
    server, _, nav, _ = wired
    store = install_store(wired, monkeypatch)
    first = server.export_portfolio_dataset("holdings")
    assert first["ok"]
    assert store.db_path != nav.db_path
    with sqlite3.connect(store.db_path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE portfolio_datasets SET manifest_json='{}'")
    assert nav.count() == 0


def test_large_export_summary_and_pages_are_bounded(wired, monkeypatch):
    server, collector, _, _ = wired
    install_store(wired, monkeypatch)
    collector.transactions = [replace(collector.transactions[0], tx_id=f"synthetic-{i}") for i in range(300)]
    first = server.export_portfolio_dataset("transactions", start_date="2026-01-01", end_date=DAY)
    assert first["ok"]
    assert first["data"]["next_cursor"]
    assert len(json.dumps(first, ensure_ascii=False).encode()) <= 96 * 1024
    all_rows = read_all(server, first, page_size=200)
    assert sum(r["record_type"] == "transaction" for r in all_rows) == 300


def test_manifest_mapping_matches_real_normalizer_fields(wired, monkeypatch):
    server, collector, _, _ = wired
    install_store(wired, monkeypatch)
    from portfolio_model import kis_overseas_transactions
    collector.transactions = kis_overseas_transactions({"transactions": [{"ccld_qty": "60",
        "sll_buy_dvsn_cd": "02", "trad_dt": "20260206", "pdno": "AMZN", "crcy_cd": "USD",
        "tr_frcr_amt2": "12135", "frcr_fee1": "3", "dmst_frcr_fee1": "2", "erlm_exrt": "1464.3"}]})
    result = server.export_portfolio_dataset("realized_pnl", start_date="2026-01-01", end_date=DAY)
    assert result["ok"]
    mapping = result["data"]["manifest"]["source_normalized_mapping"]["kis:inquire-period-trans"]
    assert mapping["fee"] == "frcr_fee1 + dmst_frcr_fee1"
    assert mapping["tax"] == "unsupported"
    rows = read_all(server, result, page_size=200)
    assert next(r["data"] for r in rows if r["record_type"] == "transaction")["fee"] == 5
    assert any(r["record_type"] == "cost_fx" and r["data"]["source"] == "broker_trade_rate" for r in rows)


def test_public_hash_ids_survive_digit_sequences_and_secret_fields_are_dropped(tmp_path):
    store = exports().DatasetStore(tmp_path / "datasets.db")
    snapshot_id = "12345678" + "a" * 24
    execution_id = "ki-v1-" + "12345678" + "a" * 56
    records = [{"record_type": "nav_snapshot", "data": {"snapshot_id": snapshot_id,
        "execution_id": execution_id, "client_secret": "synthetic-private", "Authorization": "Bearer synthetic-private"}}]
    dataset_id = store.freeze("nav_history", records, {"summary": {"result_status": "complete"}})
    row = store.page("nav_history", dataset_id)["rows"][0]["data"]
    assert row["snapshot_id"] == snapshot_id and row["execution_id"] == execution_id
    assert "client_secret" not in row and "Authorization" not in row


def test_partial_coverage_and_breaks_are_audit_rows(wired, monkeypatch):
    server, collector, _, _ = wired
    install_store(wired, monkeypatch)
    collector.holdings = []
    real_collect = collector.collect_transactions
    def partial(*args, **kwargs):
        collection = real_collect(*args, **kwargs)
        collection.fetch_complete = False
        collection.health["toss:secondary:orders"] = {"status": "error", "last_error": {"kind": "rate_limited"}}
        collection.failed_chunks = [{"source": "toss:secondary:orders", "requested_window": {"start": "2026-01-01", "end": DAY},
                                     "status": "failed", "retry_after_seconds": 120}]
        return collection
    monkeypatch.setattr(collector, "collect_transactions", partial)
    result = server.export_portfolio_dataset("transactions", start_date="2026-01-01", end_date=DAY, page_size=1)
    assert result["ok"]
    manifest = result["data"]["manifest"]
    rows = read_all(server, result, page_size=1)
    assert manifest["coverage"]["fetch_complete"] is False
    assert manifest["coverage"]["failed_chunks"]["count"] == sum(r["record_type"] == "failed_chunk" for r in rows)
    assert manifest["reconciliation"]["breaks"]["count"] == sum(r["record_type"] == "reconciliation_break" for r in rows) == 1
    assert result["result_status"] == "partial"


def test_bad_cursor_and_filters_refuse_before_collection(wired, monkeypatch):
    server, collector, _, _ = wired
    install_store(wired, monkeypatch)
    before = list(collector.calls)
    for kwargs in [{"cursor": "bad"}, {"page_size": 0}, {"page_size": 201},
                   {"dataset": "nav_history", "account": "kis"}]:
        result = server.export_portfolio_dataset(**{"dataset": "transactions", **kwargs})
        assert not result["ok"]
    assert collector.calls == before


def test_failed_position_sources_invalidate_export_nav_and_remain_visible(wired, monkeypatch):
    server, collector, _, _ = wired
    install_store(wired, monkeypatch)
    monkeypatch.setattr(collector, "collect_positions", lambda *a, **k: Collection(
        health={"kis:overseas": {"status": "error"}}, fetched_at=DAY + "T10:00:00Z"))
    result = server.export_portfolio_dataset("transactions", start_date="2026-01-01", end_date=DAY)
    assert result["ok"]
    manifest = result["data"]["manifest"]
    assert manifest["summary"]["nav"]["settlement_adjusted_nav_krw"] is None
    assert manifest["summary"]["nav"]["status"] == "unavailable"
    assert "INCOMPLETE_PORTFOLIO_SOURCES" in manifest["summary"]["nav_bridge"]["reconciliation"]["difference_reasons"]
    assert manifest["coverage"]["fetch_complete"] is True
    assert manifest["coverage"]["positions_fetch_complete"] is False
    assert result["result_status"] == "partial"
    coverage = [r["data"] for r in read_all(server, result) if r["record_type"] == "source_coverage"]
    assert any(r["input_role"] == "positions" and r["health"]["status"] == "error" for r in coverage)


def test_preperiod_basis_rows_are_retained_but_reporting_totals_exclude_them(wired, monkeypatch):
    server, _, _, _ = wired
    install_store(wired, monkeypatch)
    tool = server.get_realized_pnl(start_date="2026-07-01", end_date=DAY)
    result = server.export_portfolio_dataset("realized_pnl", start_date="2026-07-01", end_date=DAY)
    assert result["ok"]
    transactions = [r["data"] for r in read_all(server, result) if r["record_type"] == "transaction"]
    assert len(transactions) == 2
    assert next(t for t in transactions if t["tx_type"] == "BUY")["reporting_inclusion"] == {
        "included": False, "reason": "RETAINED_FOR_COST_BASIS"}
    reportable = [t for t in transactions if t["reporting_inclusion"]["included"]]
    totals = result["data"]["manifest"]["reporting_currency_totals"]["transaction"]["USD"]
    assert totals["fee"]["amount"] == sum(t["fee"] for t in reportable) == 5
    assert result["data"]["manifest"]["per_currency_totals"]["transaction"]["USD"]["fee"]["amount"] == 35
    assert result["data"]["manifest"]["summary"]["totals"]["realized_pnl_krw"] == tool["data"]["totals"]["realized_pnl_krw"]


def test_partial_cost_diagnostic_is_reproducible_offline_without_confirming_totals(wired, monkeypatch):
    server, collector, _, _ = wired
    install_store(wired, monkeypatch)
    from portfolio_model import kis_overseas_transactions
    collector.transactions = kis_overseas_transactions({"transactions": [
        {"trad_dt": "20260206", "pdno": "AMZN", "crcy_cd": "USD", "ccld_qty": "1",
         "sll_buy_dvsn_cd": "02", "tr_frcr_amt2": "100", "frcr_fee1": "1.50", "erlm_exrt": "1464.3"},
        {"trad_dt": "20260930", "pdno": "AMZN", "crcy_cd": "USD", "ccld_qty": "1",
         "sll_buy_dvsn_cd": "01", "tr_frcr_amt2": "120", "frcr_fee1": "0", "dmst_frcr_fee1": "0", "erlm_exrt": "1464.3"},
    ]})
    collector.holdings = []
    result = server.export_portfolio_dataset("realized_pnl", start_date="2026-01-01", end_date=DAY, page_size=1)
    assert result["ok"]
    monkeypatch.setattr(collector, "collect_transactions", lambda *a, **k: pytest.fail("No offline provider reads"))
    import portfolio_engine
    monkeypatch.setattr(portfolio_engine, "compute_realized_pnl", lambda *a, **k: pytest.fail("No offline engine calls"))
    rows = read_all(server, result, page_size=1)
    transactions = [r["data"] for r in rows if r["record_type"] == "transaction"]
    buy = next(t for t in transactions if t["tx_type"] == "BUY")
    sell = next(t for t in transactions if t["tx_type"] == "SELL")
    assert buy["fee"] is None and buy["fee_reported_amount"] is None
    assert buy["fee_status"] == "unknown" and buy["net_amount"] is None
    assert buy["fee_observed_amount"] == 1.5 and buy["fee_observed_status"] == "partial"
    assert buy["tax_observed_amount"] is None and buy["tax_observed_status"] == "unsupported"
    # Diagnostic assumption only: missing components are omitted, never verified zero.
    def observed(t, field):
        amount = t[field + "_observed_amount"]
        return Decimal(str(amount)) if amount is not None else Decimal(0)
    basis = Decimal(str(buy["gross_amount"])) + observed(buy, "fee") + observed(buy, "tax")
    proceeds = Decimal(str(sell["gross_amount"])) - observed(sell, "fee") - observed(sell, "tax")
    diagnostic = proceeds - basis * Decimal(str(sell["quantity"])) / Decimal(str(buy["quantity"]))
    exported_symbol = next(r["data"] for r in rows if r["record_type"] == "realized_symbol")
    assert diagnostic == Decimal("18.50") == Decimal(str(exported_symbol["realized_pnl"]))
    assert "FEES_UNKNOWN" in result["data"]["manifest"]["summary"]["totals"]["reason_codes"]
    fee_totals = result["data"]["manifest"]["per_currency_totals"]["transaction"]["USD"]["fee"]
    assert fee_totals["amount"] is None and fee_totals["observed_amount"] == 1.5


def test_holdings_export_coverage_uses_local_calendar_at_kst_midnight(wired, monkeypatch):
    import time

    from portfolio_export import collect_dataset

    _, collector, _, _ = wired
    original = collector.collect_positions
    positions = original()
    positions.fetched_at = "2026-10-04T15:30:00+00:00"
    with monkeypatch.context() as clock_patch:
        clock_patch.setenv("TZ", "Asia/Seoul")
        time.tzset()
        clock_patch.setattr(collector, "collect_positions", lambda *args: positions)
        try:
            _, manifest = collect_dataset("holdings", "2026-10-05", "2026-10-05", collector=collector)
            assert manifest["coverage"]["observed_window"] == {"start": "2026-10-05", "end": "2026-10-05"}
            assert manifest["coverage"]["positions_observed_at"] == "2026-10-04T15:30:00+00:00"
        finally:
            clock_patch.undo()
            time.tzset()
