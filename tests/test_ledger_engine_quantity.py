"""The cost engine's own quantity, fill order, inferred openings and ENGINE_QUANTITY_DRIFT.

Synthetic Toss order payloads go through the real normalizer (portfolio_model.toss_transactions);
no broker or credential access. All amounts are synthetic.
"""
import random

import portfolio_engine as engine
from portfolio_model import FxRates, Holding, Transaction, TxType, toss_transactions
from portfolio_sources import Collection

FX = FxRates({"USD": 1400.0})
ACCOUNT = "toss:primary"


def order(order_id, side, filled_at, qty, price=10.0, symbol="ACMU"):
    return {"orderId": order_id, "symbol": symbol, "currency": "USD", "side": side, "status": "FILLED",
            "orderedAt": filled_at, "execution": {"filledAt": filled_at, "filledQuantity": qty,
                                                  "averageFilledPrice": price, "filledAmount": qty * price,
                                                  "commission": 0, "tax": 0}}


def ledger(rows, holdings):
    txs = toss_transactions(rows, ACCOUNT)
    positions = [Holding("toss", ACCOUNT, symbol, "USD", qty) for symbol, qty in holdings.items()]
    collection = Collection(transactions=txs, health={ACCOUNT: {"status": "ok"}})
    collection.reconciliation = engine.reconcile_quantities(txs, positions, known_accounts=[ACCOUNT])
    return txs, collection


def realized(txs, collection, start="2026-01-01", end="2026-12-31"):
    return engine.compute_realized_pnl(txs, FX, start, end, input_quality=engine.ledger_input_quality(collection))


# The order id that sorts first ("A…") is the later fill: a same-day sell sorted before its buy
# under (trade_date, order id) would clamp the engine quantity below the broker's.
SAME_DAY = [
    order("Z-100", "BUY", "2026-07-01T14:00:00.000Z", 43),
    order("Y-200", "BUY", "2026-07-23T13:31:00.120Z", 24, price=11.0),
    order("A-300", "SELL", "2026-07-23T13:45:10.500Z", 24, price=12.0),
    order("X-400", "BUY", "2026-08-03T14:00:00.000Z", 24, price=9.0),
]


def test_same_day_fills_replay_in_fill_time_not_order_id_order():
    txs, collection = ledger(SAME_DAY, {"ACMU": 67})
    pnl = realized(txs, collection)
    assert pnl["open_positions_by_account"][ACCOUNT]["ACMU:USD"]["quantity"] == 67
    assert pnl["incomplete_cost_basis"] == []
    assert pnl["totals"]["complete_basis"]["sell_count"] == 1
    assert pnl["totals"]["incomplete_basis"]["sell_count"] == 0
    row = collection.reconciliation["positions"][0]
    assert row["engine_qty"] == 67 and row["engine_quantity_difference"] == 0
    assert row["engine_inferred_opening_qty"] == 0
    assert collection.reconciliation["reconciliation_breaks"] == []


def test_replay_order_uses_fill_instant_then_order_id():
    txs = toss_transactions(SAME_DAY, ACCOUNT)
    ordered = sorted(txs, key=engine._replay_order)
    assert [t.tx_id for t in ordered] == ["Z-100", "Y-200", "A-300", "X-400"]
    untimed = [Transaction("kis", "kis:overseas", tx_id, TxType.BUY, "2026-07-23", "USD", symbol="ACMU",
                           quantity=1, price=1) for tx_id in ("0000002", "0000001")]
    assert [t.tx_id for t in sorted(untimed, key=engine._replay_order)] == ["0000001", "0000002"]


def test_sale_before_any_recorded_buy_books_an_inferred_opening_without_engine_drift():
    rows = [order("S1", "SELL", "2026-03-02T14:00:00.000Z", 60), order("B1", "BUY", "2026-03-05T14:00:00.000Z", 5)]
    txs, collection = ledger(rows, {"ACMU": 5})
    row = collection.reconciliation["positions"][0]
    assert row["observed_net_qty"] == -55 and row["quantity_difference"] == 60
    # The engine's quantity matches the broker (no drift); the observed-net break stays reported.
    assert row["engine_qty"] == 5 and row["engine_inferred_opening_qty"] == 60
    assert row["engine_quantity_difference"] == 0 and "ENGINE_QUANTITY_DRIFT" not in row["reason_codes"]
    assert {"RECONCILIATION_BREAK", "MISSING_OPENING_POSITION", "INFERRED_OPENING_POSITION"} <= set(row["reason_codes"])
    pnl = engine.compute_realized_pnl_with_sales(txs, FX, "2026-01-01", "2026-12-31",
                                                 input_quality=engine.ledger_input_quality(collection))
    sale = pnl["sales"][0]
    assert sale["inferred_opening_qty"] == 60 and sale["unmatched_quantity"] == 60
    assert sale["cost_basis_complete"] is False and "INCOMPLETE_COST_BASIS" in sale["reason_codes"]
    assert pnl["totals"]["realized_pnl_krw"] is None


def test_engine_quantity_drift_is_an_explicit_break_and_nulls_that_books_pnl():
    rows = [order("B1", "BUY", "2026-02-02T14:00:00.000Z", 100),
            order("S1", "SELL", "2026-02-10T14:00:00.000Z", 20, price=15.0),
            order("B2", "BUY", "2026-02-02T14:00:00.000Z", 10, symbol="MSFT"),
            order("S2", "SELL", "2026-02-11T14:00:00.000Z", 10, price=12.0, symbol="MSFT")]
    # The broker holds 50 ACMU: 30 shares left the account without a ledger row.
    txs, collection = ledger(rows, {"ACMU": 50})
    recon = collection.reconciliation
    drift = [r for r in recon["reconciliation_breaks"] if r["symbol"] == "ACMU"][0]
    assert drift["engine_qty"] == 80 and drift["engine_quantity_difference"] == -30
    assert {"RECONCILIATION_BREAK", "ENGINE_QUANTITY_DRIFT"} <= set(drift["reason_codes"])
    assert recon["engine_quantity_drift_breaks"] == 1
    pnl = engine.compute_realized_pnl_with_sales(txs, FX, "2026-01-01", "2026-12-31",
                                                 input_quality=engine.ledger_input_quality(collection))
    by_symbol = {s["symbol"]: s for s in pnl["symbols"]}
    assert by_symbol["ACMU"]["realized_pnl"] is None and by_symbol["ACMU"]["realized_pnl_krw"] is None
    assert "ENGINE_QUANTITY_DRIFT" in by_symbol["ACMU"]["reason_codes"]
    assert by_symbol["MSFT"]["realized_pnl"] == 20.0  # an undrifted book keeps its figure
    acmu_sale = [s for s in pnl["sales"] if s["symbol"] == "ACMU"][0]
    assert acmu_sale["realized_pnl"] is None and acmu_sale["cost_basis_complete"] is False
    assert pnl["totals"]["complete_basis"]["symbols"] == ["MSFT"]
    assert pnl["totals"]["incomplete_basis"]["realized_pnl_reason_codes"] == [
        "ENGINE_QUANTITY_DRIFT", "INCOMPLETE_COST_BASIS"]
    assert pnl["totals"]["realized_pnl_krw"] is None


def test_replay_book_quantities_equal_the_engine_books():
    rng = random.Random(84)
    rows = []
    for i in range(400):
        day = 1 + rng.randrange(60)
        stamp = f"2026-{1 + day // 30:02d}-{1 + day % 28:02d}T{rng.randrange(24):02d}:{rng.randrange(60):02d}:00.000Z"
        rows.append(order(f"{rng.randrange(10**6):06d}", rng.choice(["BUY", "SELL"]), stamp,
                          rng.randrange(1, 40), symbol=rng.choice(["AAA", "BBB", "CCC"])))
    txs = toss_transactions(rows, ACCOUNT)
    txs.append(Transaction("toss", ACCOUNT, "split", TxType.SPLIT, "2026-03-15", "USD", symbol="AAA", quantity=500))
    books = engine.replay_book_quantities(txs)
    pnl = engine.compute_realized_pnl(txs, FX)
    open_books = pnl["open_positions_by_account"][ACCOUNT]
    for (account, symbol, currency), book in books.items():
        assert round(book["quantity"], 8) == open_books.get(f"{symbol}:{currency}", {}).get("quantity", 0.0)
    assert any(book["inferred_opening_qty"] > 0 for book in books.values())


def test_trade_review_cycles_follow_fill_order_and_say_so():
    txs = toss_transactions([order("Z1", "BUY", "2026-02-03T14:00:00.000Z", 10),
                             order("A2", "SELL", "2026-02-03T15:00:00.000Z", 10, price=12.0)], ACCOUNT)
    window = {"start": "2000-01-01", "end": "2026-02-28"}
    led = Collection(transactions=txs, health={ACCOUNT: {"status": "ok"}}, fetched_at="2026-02-28T12:00:00+00:00",
                     requested_window=window, fetch_complete=True)
    positions = Collection(health={ACCOUNT: {"status": "ok"}}, fetched_at="2026-02-28T12:00:00+00:00")
    result = engine.compute_trade_review(led, positions, FX, "2026-02-01", "2026-02-28", detail_level="full")
    assert result["totals"]["round_trip_count"] == 1
    assert result["round_trips"][0]["cost_basis_complete"] is True
    assert result["totals"]["realized_win_rate"] == 1
    assert "intraday chronology unavailable" not in result["methodology"]["ordering"]


def test_sales_before_an_inferred_opening_have_unknown_cost():
    # A ledger that starts after the purchase (e.g. get_portfolio_summary's YTD read): the late
    # oversell proves inventory was held all along, so the earlier sale's average omitted it.
    rows = [order("B1", "BUY", "2026-02-02T14:00:00.000Z", 10),
            order("S1", "SELL", "2026-02-05T14:00:00.000Z", 10, price=12.0),
            order("B2", "BUY", "2026-03-02T14:00:00.000Z", 5, price=11.0),
            order("S2", "SELL", "2026-03-10T14:00:00.000Z", 20, price=13.0),
            order("B3", "BUY", "2026-03-16T14:00:00.000Z", 8, price=9.0),
            order("S3", "SELL", "2026-03-20T14:00:00.000Z", 8, price=10.0),
            order("M1", "BUY", "2026-02-02T14:00:00.000Z", 10, symbol="MSFT"),
            order("M2", "SELL", "2026-02-11T14:00:00.000Z", 10, price=12.0, symbol="MSFT")]
    txs, collection = ledger(rows, {})
    assert collection.reconciliation["engine_quantity_drift_breaks"] == 0
    pnl = engine.compute_realized_pnl_with_sales(txs, FX, "2026-01-01", "2026-12-31",
                                                 input_quality=engine.ledger_input_quality(collection))
    first, oversell, after = [s for s in pnl["sales"] if s["symbol"] == "ACMU"]
    assert first["cost_basis_complete"] is False and first["realized_pnl"] is None and first["known_cost"] is None
    assert {"INFERRED_OPENING_POSITION", "INCOMPLETE_COST_BASIS"} <= set(first["reason_codes"])
    assert oversell["inferred_opening_qty"] == 15 and oversell["cost_basis_complete"] is False
    assert "INFERRED_OPENING_POSITION" not in oversell["reason_codes"]
    # After the inferred opening is sold the book restarts from recorded buys only.
    assert after["cost_basis_complete"] is True and after["realized_pnl"] == 8.0
    assert pnl["totals"]["complete_basis"]["sell_count"] == 2  # S3 and MSFT
    incomplete = pnl["totals"]["incomplete_basis"]
    assert incomplete["sell_count"] == 2 and incomplete["realized_pnl_krw"] is None
    assert incomplete["realized_pnl_reason_codes"] == ["INCOMPLETE_COST_BASIS", "INFERRED_OPENING_POSITION"]
    by_symbol = {s["symbol"]: s for s in pnl["symbols"]}
    assert by_symbol["ACMU"]["realized_pnl"] is None and by_symbol["MSFT"]["realized_pnl"] == 20.0


def test_a_full_history_without_oversells_is_unchanged_by_the_opening_rule():
    txs, collection = ledger(SAME_DAY, {"ACMU": 67})
    pnl = engine.compute_realized_pnl_with_sales(txs, FX, "2026-01-01", "2026-12-31",
                                                 input_quality=engine.ledger_input_quality(collection))
    assert all("INFERRED_OPENING_POSITION" not in s["reason_codes"] and s["cost_basis_complete"] for s in pnl["sales"])
    assert pnl["totals"]["incomplete_basis"]["sell_count"] == 0
