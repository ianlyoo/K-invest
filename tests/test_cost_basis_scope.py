"""One per-account basis for realized sales, remaining cost and all consumers."""
from dataclasses import replace
from datetime import date
from decimal import Decimal
from random import Random

import pytest

import portfolio_engine as pe
from portfolio_export import collect_dataset
from portfolio_model import CashBalance, FxRates, Holding, Transaction, TxType
from portfolio_sources import Collection
from portfolio_store import NavStore

START, END = "2026-02-01", "2026-02-28"
FX = FxRates({"USD": 1300})
A, B = "toss:a", "toss:b"


def trade(account, side, day, qty, price, **kwargs):
    return Transaction(
        "toss", account, f"{day}-{account}-{side.value}", side,
        f"2026-02-{day:02d}", "USD", symbol="CRDO", quantity=qty, price=price,
        gross_amount=qty * price, fee_status="verified_zero",
        tax_status="verified_zero", fx_rate=1300, **kwargs,
    )


def owner_rows():
    return [trade(A, TxType.BUY, 1, 10, 100),
            trade(B, TxType.BUY, 2, 10, 200),
            trade(A, TxType.SELL, 3, 10, 150)]


def collection(rows=(), holdings=()):
    return Collection(
        transactions=list(rows), holdings=list(holdings),
        health={A: {"status": "ok"}, B: {"status": "ok"}},
        fetched_at=END + "T12:00:00+00:00", fetch_complete=True,
        requested_window={"start": START, "end": END},
    )


def remaining_holdings():
    return [Holding("toss", B, "CRDO", "USD", 10, avg_price=200,
                    cost_basis=2000, market_value=2500, unrealized_pnl=500)]


def test_owner_regression_and_account_sum_share_one_replay():
    rows = owner_rows()
    result = pe.compute_realized_pnl(rows, FX, START, END)
    totals = result["totals"]
    assert totals["realized_pnl_by_currency"] == {"USD": 500}
    assert totals["complete_basis"]["realized_pnl_by_currency"] == {"USD": 500}
    assert totals["incomplete_basis"]["sell_count"] == 0
    assert totals["realized_pnl_krw"] == 650000
    assert result["symbols"][0]["cost_basis"] == 1000
    book = result["open_positions_from_ledger"]["CRDO:USD"]
    assert (book["quantity"], book["cost_basis"], book["avg_price"]) == (10, 2000, 200)
    assert result["open_positions_by_account"] == {A: {}, B: {"CRDO:USD": book}}
    assert sum(pe.compute_realized_pnl([t for t in rows if t.account == account], FX)
               ["totals"]["realized_pnl_by_currency"].get("USD", 0)
               for account in (A, B)) == 500
    review = pe.compute_trade_review(collection(rows), collection(holdings=remaining_holdings()),
                                     FX, START, END, detail_level="full")
    assert review["totals"]["realized"]["realized_pnl_by_currency"] == {"USD": 500}
    assert sum(s["cost_basis"] for s in review["sales"]) == 1000
    assert result["methodology"]["basis_scope"] == "per_account"
    assert review["methodology"]["basis_scope"] == "per_account"


def test_interleaved_accounts_match_hand_computation():
    # A: avg 120, sells 5 then 10 -> gains 150 + 400, remaining 5@120.
    # B: sells 5@180 from 10@200, buys 5@240 -> avg 220;
    # sells 4@250 -> gain -100 + 120, remaining 6@220.
    rows = [trade(A, TxType.BUY, 1, 10, 100), trade(B, TxType.BUY, 2, 10, 200),
            trade(A, TxType.BUY, 3, 10, 140), trade(B, TxType.SELL, 4, 5, 180),
            trade(A, TxType.SELL, 5, 5, 150), trade(B, TxType.BUY, 6, 5, 240),
            trade(B, TxType.SELL, 7, 4, 250), trade(A, TxType.SELL, 8, 10, 160)]
    result = pe.compute_realized_pnl(rows, FX, START, END)
    assert result["totals"]["realized_pnl_by_currency"] == {"USD": 570}
    book = result["open_positions_from_ledger"]["CRDO:USD"]
    assert book["cost_basis"] == 1920
    assert book["quantity"] == 11
    assert book["avg_price"] == pytest.approx(1920 / 11, abs=1e-6)
    account_books = result["open_positions_by_account"]
    assert account_books[A]["CRDO:USD"]["cost_basis"] == 600
    assert account_books[B]["CRDO:USD"]["cost_basis"] == 1320
    sales = pe.compute_realized_pnl_with_sales(rows, FX, START, END)["sales"]
    assert [s["realized_pnl"] for s in sales] == [-100, 150, 120, 400]
    pooled = pe.compute_realized_pnl([replace(t, account=A) for t in rows], FX, START, END)
    assert pooled["totals"]["realized_pnl_by_currency"]["USD"] == pytest.approx(468.6667)
    holdings = [Holding("toss", A, "CRDO", "USD", 5, cost_basis=600, unrealized_pnl=0),
                Holding("toss", B, "CRDO", "USD", 6, cost_basis=1320, unrealized_pnl=0)]
    review = pe.compute_trade_review(collection(rows), collection(holdings=holdings), FX, START, END)
    assert review["totals"]["realized"]["realized_pnl_by_currency"] == {"USD": 570}


def test_another_accounts_holdings_cannot_complete_a_sale():
    rows = [trade(A, TxType.BUY, 1, 10, 100), trade(B, TxType.SELL, 2, 10, 150),
            trade(A, TxType.SELL, 3, 5, 120)]
    result = pe.compute_realized_pnl(rows, FX, START, END)
    assert result["incomplete_cost_basis"] == ["CRDO"]
    assert result["totals"]["realized_pnl_by_currency"] is None
    assert result["totals"]["complete_basis"]["realized_pnl_by_currency"] == {"USD": 100}
    # No realized P&L without the full basis; proceeds and the known cost are published.
    assert result["totals"]["incomplete_basis"]["realized_pnl_by_currency"] is None
    assert result["totals"]["incomplete_basis"]["proceeds_by_currency"] == {"USD": 1500}
    assert result["totals"]["incomplete_basis"]["known_partial_cost_by_currency"] == {"USD": 0}
    assert result["open_positions_by_account"][B] == {}
    assert result["open_positions_from_ledger"]["CRDO:USD"]["cost_basis"] == 500
    sales = pe.compute_realized_pnl_with_sales(rows, FX, START, END)["sales"]
    assert [(s["account"], s["cost_basis_complete"]) for s in sales] == [(B, False), (A, True)]
    # The broker must hold what A's engine book holds (5), else that book drifts.
    review = pe.compute_trade_review(collection(rows), collection(holdings=[Holding("toss", A, "CRDO", "USD", 5)]),
                                     FX, START, END)
    statuses = {r["account"]: r for r in review["account_basis_status"]}
    assert statuses[A]["complete_basis"]["sell_count"] == 1
    assert "INCOMPLETE_COST_BASIS" not in statuses[A]["reason_codes"]
    assert statuses[B]["incomplete_basis"]["sell_count"] == 1


def test_fee_tax_status_and_fx_basis_are_isolated_by_account():
    rows = owner_rows()
    rows[0] = replace(rows[0], fee=10, fee_status="reported", fx_rate=1200)
    rows[1] = replace(rows[1], tax_status="unsupported", fx_rate=1400)
    result = pe.compute_realized_pnl(rows, FX, START, END)
    assert result["totals"]["realized_pnl_by_currency"] == {"USD": 490}
    assert result["totals"]["status"] == "complete"
    assert result["totals"]["realized_fx_krw"] == 101000
    assert result["totals"]["realized_pnl_krw"] == 738000
    basis = result["open_positions_from_ledger"]["CRDO:USD"]["ledger_cost_basis"]
    assert basis["amount"] == 2000
    assert basis["status"] == "partial"
    assert basis["includes_buy_taxes"] is None


def test_split_changes_only_the_named_account():
    rows = owner_rows()[:2] + [replace(trade(A, TxType.BUY, 3, 20, 0), tx_type=TxType.SPLIT),
                              trade(A, TxType.SELL, 4, 20, 75)]
    result = pe.compute_realized_pnl(rows, FX, START, END)
    assert result["totals"]["realized_pnl_by_currency"] == {"USD": 500}
    assert result["open_positions_by_account"][B]["CRDO:USD"]["quantity"] == 10
    assert result["open_positions_from_ledger"]["CRDO:USD"]["cost_basis"] == 2000


class Collector:
    def collect_transactions(self, *args, **kwargs):
        return collection(owner_rows())

    def collect_positions(self, *args, **kwargs):
        result = collection(holdings=remaining_holdings())
        result.cash = [CashBalance("toss", B, "USD", 100)]
        return result

    def build_fx(self, *args):
        return FX


def test_holdings_summary_performance_and_exports_use_account_basis(monkeypatch, tmp_path):
    import server
    from portfolio_export import DatasetStore

    collector = Collector()
    monkeypatch.setattr(server, "_get_collector", lambda: collector)
    monkeypatch.setattr(server, "_get_nav_store", lambda: NavStore(tmp_path / "nav.db"))
    monkeypatch.setattr(server, "_get_dataset_store", lambda: DatasetStore(tmp_path / "datasets.db"))
    monkeypatch.setattr(pe, "resolve_period", lambda *args, **kwargs: (START, END))
    realized = server.get_realized_pnl(start_date=START, end_date=END)["data"]
    assert realized["totals"]["realized_pnl_by_currency"] == {"USD": 500}
    holdings = pe.consolidate_holdings(remaining_holdings(), FX, realized)
    assert holdings["positions"][0]["ledger_cost_basis"]["amount"] == 2000
    assert holdings["positions"][0]["avg_price"] == 200
    assert holdings["methodology"]["basis_scope"] == "per_account"
    # Production holdings do not load a ledger; these assertions check that
    # limitation and broker arithmetic, rather than proving the replay fix.
    production = server.get_consolidated_holdings(detail_level="full")["data"]["positions"][0]
    assert production["ledger_cost_basis"]["amount"] is None
    assert production["ledger_cost_basis"]["reason"] == "ledger_not_available"
    assert "basis_scope" not in remaining_holdings()[0].to_dict()["ledger_cost_basis"]
    summary = server.get_portfolio_summary(snapshot=True)["data"]
    assert summary["realized_pnl_krw"] == 650000
    assert summary["cost_basis_krw"] == 2600000
    assert summary["methodology"]["basis_scope"] == "per_account"
    assert summary["snapshot"]["stored"]
    assert NavStore(tmp_path / "nav.db").get_snapshot(date.today().isoformat())["methodology"] == {
        "basis_scope": "per_account"}
    performance = server.get_portfolio_performance(risk_free_rate=0)["data"]
    assert performance["pnl"]["realized_krw"] == 650000
    assert performance["methodology"]["basis_scope"] == "per_account"
    review = server.get_trade_review(start_date=START, end_date=END)["data"]
    assert review["totals"]["realized"]["realized_pnl_by_currency"] == {"USD": 500}
    exported = server.export_portfolio_dataset("realized_pnl", start_date=START, end_date=END)["data"]
    assert exported["manifest"]["summary"]["totals"]["realized_pnl_by_currency"] == {"USD": 500}
    assert exported["manifest"]["policies"]["basis_scope"] == "per_account"
    rows, manifest = collect_dataset("holdings", START, END, collector=collector)
    # Holdings exports also use broker cost without replaying a ledger. The
    # realized export assertion above fails on the pooled engine (USD 0).
    assert manifest["summary"]["totals"]["cost_basis_krw"] == 2600000
    assert next(r["data"] for r in rows if r["record_type"] == "consolidated_holding")["avg_price"] == 200


def test_snapshot_tool_and_job_sum_broker_account_cost(monkeypatch, tmp_path):
    import server
    from scripts import snapshot_nav

    collector, store = Collector(), NavStore(tmp_path / "nav.db")
    positions = collector.collect_positions()
    positions.holdings.append(Holding("toss", A, "CRDO", "USD", 10,
                                      avg_price=100, cost_basis=1000, market_value=1500))
    monkeypatch.setattr(collector, "collect_positions", lambda *args: positions)
    monkeypatch.setattr(collector, "collect_transactions", lambda *args, **kwargs:
                        pytest.fail("NAV snapshots must not fetch/replay ledger basis"))
    monkeypatch.setattr(server, "_get_collector", lambda: collector)
    monkeypatch.setattr(server, "_get_nav_store", lambda: store)
    monkeypatch.setattr(snapshot_nav, "apply_env_file", lambda *args: None)
    monkeypatch.setattr(snapshot_nav, "prepare_crypto_snapshot", lambda *args: None)
    monkeypatch.setattr("sys.argv", ["snapshot_nav.py"])
    snapshot = server.snapshot_portfolio_nav()["data"]["snapshot"]
    assert snapshot["cost_basis_krw"] == 3900000
    assert snapshot["methodology"] == {"basis_scope": "per_account"}
    assert snapshot_nav.main() == 0
    assert store.get_snapshot(date.today().isoformat())["cost_basis_krw"] == 3900000
    assert store.get_snapshot(date.today().isoformat())["methodology"] == {"basis_scope": "per_account"}
    # Snapshot writers use broker valuation, so cost assertions also pass with
    # the pooled engine; the test prohibits a ledger read and checks metadata.
    response = server.get_portfolio_summary(include_realized=False, snapshot=True)
    assert response["ok"] and response["data"]["snapshot"]["stored"]
    assert set(response["data"]["methodology"]) != {"basis_scope"}
    assert store.get_snapshot(date.today().isoformat())["methodology"] == {"basis_scope": "per_account"}


def decimal_sum(values):
    return float(sum((Decimal(str(value)) for value in values), Decimal("0")))


def test_sub_digit_account_costs_and_gains_sum_after_rounding():
    rows = [trade(A, TxType.BUY, 1, 1, 0.00006), trade(B, TxType.BUY, 1, 1, 0.00006)]
    result = pe.compute_realized_pnl(rows, FX)
    book = result["open_positions_from_ledger"]["CRDO:USD"]
    assert book["cost_basis"] == book["gross_cost"] == book["ledger_cost_basis"]["amount"] == 0.0002
    assert book["quantity"] == 2
    assert book["avg_price"] == 0.0001
    rows += [trade(A, TxType.SELL, 2, 1, 0.00012), trade(B, TxType.SELL, 2, 1, 0.00012)]
    realized = pe.compute_realized_pnl(rows, FX)
    assert realized["totals"]["realized_pnl_by_currency"] == {"USD": 0.0002}
    review = pe.compute_trade_review(collection(rows), collection(), FX, START, END)
    assert review["totals"]["realized"]["realized_pnl_by_currency"] == {"USD": 0.0002}


def test_random_two_account_ledgers_equal_decimal_sums_of_published_accounts():
    rng = Random(264)
    for case in range(2000):
        rows = []
        for account in (A, B):
            for symbol, currency in (("CRDO", "USD"), ("OTHER", "KRW")):
                quantities = [rng.uniform(1, 20) for _ in range(2)]
                for day, qty in enumerate(quantities, 1):
                    rows.append(replace(trade(account, TxType.BUY, day, qty, rng.uniform(0.00001, 200),
                                              fee=rng.uniform(0, 0.1), tax=rng.uniform(0, 0.1)),
                                        symbol=symbol, currency=currency,
                                        tx_id=f"{case}-{account}-{symbol}-{day}",
                                        fee_status="reported", tax_status="reported"))
                for day in (3, 4):
                    qty = sum(quantities) * rng.uniform(0.05, 0.3)
                    rows.append(replace(trade(account, TxType.SELL, day, qty, rng.uniform(0.00001, 200)),
                                        symbol=symbol, currency=currency,
                                        tx_id=f"{case}-{account}-{symbol}-{day}"))
        result = pe.compute_realized_pnl(rows, FX, START, END)
        accounts = [pe.compute_realized_pnl([t for t in rows if t.account == account], FX, START, END)
                    for account in (A, B)]
        for key, combined in result["open_positions_from_ledger"].items():
            books = [a["open_positions_from_ledger"][key] for a in accounts]
            for field in ("quantity", "cost_basis", "gross_cost", "capitalized_buy_fees", "capitalized_buy_taxes"):
                assert combined[field] == decimal_sum(b[field] for b in books), (case, key, field)
            assert combined["ledger_cost_basis"]["amount"] == decimal_sum(
                b["ledger_cost_basis"]["amount"] for b in books)
            expected_avg = round(Decimal(str(combined["cost_basis"])) / Decimal(str(combined["quantity"])), 6)
            assert combined["avg_price"] == float(expected_avg)
        for field in ("realized_pnl_krw", "realized_fx_krw"):
            assert result["totals"][field] == decimal_sum(a["totals"][field] for a in accounts), (case, field)
        for currency in ("USD", "KRW"):
            for field in ("realized_pnl_by_currency", "proceeds_by_currency"):
                assert result["totals"][field][currency] == decimal_sum(
                    a["totals"][field][currency] for a in accounts), (case, currency, field)
            for subset in ("complete_basis", "incomplete_basis"):
                assert result["totals"][subset]["realized_pnl_by_currency"].get(currency, 0) == decimal_sum(
                    a["totals"][subset]["realized_pnl_by_currency"].get(currency, 0) for a in accounts)
        for combined in result["symbols"]:
            symbols = [next(s for s in a["symbols"] if s["symbol"] == combined["symbol"]) for a in accounts]
            for field in ("proceeds", "cost_basis", "realized_pnl", "realized_pnl_krw", "realized_fx_krw", "fees", "taxes"):
                assert combined[field] == decimal_sum(s[field] for s in symbols), (case, combined["symbol"], field)


def test_kis_and_toss_same_symbol_require_every_window_sale_fx_part():
    kis = "kis:overseas"
    rows = [replace(trade(kis, TxType.BUY, 1, 10, 100), provider="kis", fx_rate=1200),
            replace(trade(B, TxType.BUY, 1, 10, 200), fx_rate=None),
            replace(trade(kis, TxType.SELL, 2, 10, 150), provider="kis", fx_rate=1300),
            replace(trade(B, TxType.SELL, 2, 10, 250), fx_rate=None)]
    result = pe.compute_realized_pnl(rows, FX, START, END)
    assert result["totals"]["realized_pnl_by_currency"] == {"USD": 1000}
    assert result["totals"]["realized_pnl_krw"] == 1400000
    assert result["totals"]["status"] == "complete"
    assert result["symbols"][0]["realized_fx_krw"] is None
    assert result["totals"]["realized_fx_krw"] is None
    assert result["totals"]["realized_fx_krw_reason_codes"] == ["FX_DECOMPOSITION_PARTIAL"]
    # A missing FX part before the requested window must not taint it.
    window = pe.compute_realized_pnl(rows, FX, "2026-02-02", END)
    assert window["totals"]["realized_fx_krw"] is None
    rows[-1] = replace(rows[-1], trade_date="2026-01-31")
    window = pe.compute_realized_pnl(rows, FX, START, END)
    assert window["totals"]["realized_fx_krw"] == 100000
    assert window["totals"]["realized_fx_krw_reason_codes"] == []


def test_transfer_does_not_supply_receiving_account_purchase_basis():
    rows = [trade(A, TxType.BUY, 1, 10, 100),
            replace(trade(B, TxType.BUY, 2, 10, 0), tx_type=TxType.TRANSFER),
            trade(B, TxType.SELL, 3, 10, 150)]
    result = pe.compute_realized_pnl(rows, FX, START, END)
    assert result["totals"]["realized_pnl_by_currency"] is None
    assert result["totals"]["incomplete_basis"]["sell_count"] == 1
    assert "INCOMPLETE_COST_BASIS" in result["totals"]["reason_codes"]
