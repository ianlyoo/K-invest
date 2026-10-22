"""Default responses stay under 30 KB; history pages are complete and stable."""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_merge_equality as fakes  # noqa: E402

import response_shape  # noqa: E402
import server  # noqa: E402
import tools_v2  # noqa: E402
from portfolio_model import TxType  # noqa: E402


@pytest.fixture
def stub(monkeypatch, tmp_path):
    return fakes.install_stub(monkeypatch, tmp_path)


BUDGET = 30 * 1024


def _size(env):
    return len(response_shape.compact_json(env).encode())


def _many_transactions(monkeypatch, count=1200):
    rows = [fakes._tx(f"x{i:05d}", ("kis:domestic", "toss:primary")[i % 2], (TxType.BUY, TxType.SELL)[i % 3 == 0],
                      f"2026-{1 + i % 9:02d}-{1 + i % 27:02d}", f"SYN{i % 40:02d}", ("KRW", "USD")[i % 2],
                      1 + i % 7, 10.0 + i % 13) for i in range(count)]
    monkeypatch.setattr(fakes, "TRANSACTIONS", rows)
    return rows


def test_default_transactions_page_is_under_budget_and_the_pages_cover_the_window(stub, monkeypatch):
    rows = _many_transactions(monkeypatch)
    first = tools_v2.get_transactions(start="2026-01-01", end="2026-10-21")
    assert _size(first) < BUDGET
    page = first["data"]["transactions"]
    assert page["page"]["returned"] == 100 and page["page"]["total_count"] == len(rows)
    seen, cursor = 0, page["page"]["next_cursor"]
    seen += page["page"]["returned"]
    ids = set()
    while cursor:
        nxt = tools_v2.get_transactions(start="2026-01-01", end="2026-10-21", limit=500, cursor=cursor)
        body = nxt["data"]["transactions"]
        ids |= {tuple(row) for row in body["rows"]}
        seen += body["page"]["returned"]
        cursor = body["page"]["next_cursor"]
    assert seen == len(rows)
    # each page re-reads the window: the header totals are those of the whole window
    assert nxt["data"]["transactions"]["transaction_count"] == len(rows)


def test_transactions_limit_and_cursor_validation(stub):
    for limit in (0, 501):
        assert tools_v2.get_transactions(limit=limit)["error"]["code"] == "INVALID_LIMIT"
    assert tools_v2.get_transactions(cursor="not-a-cursor")["error"]["code"] == "INVALID_CURSOR"
    first = tools_v2.get_transactions(start="2026-01-01", end="2026-10-21", limit=1)["data"]["transactions"]
    other_level = tools_v2.get_transactions(start="2026-01-01", end="2026-10-21", limit=1, detail_level="full",
                                            cursor=first["page"]["next_cursor"])
    assert other_level["error"]["code"] == "INVALID_CURSOR"


def test_default_window_cursor_rejects_a_moved_window(stub, monkeypatch):
    first = tools_v2.get_transactions(limit=1)["data"]["transactions"]
    cursor = first["page"]["next_cursor"]
    if cursor is None:  # no synthetic rows inside the rolling default window
        _many_transactions(monkeypatch)
        monkeypatch.setattr(fakes, "TRANSACTIONS", [fakes._tx(f"d{i}", "kis:domestic", TxType.BUY,
                                                              server._resolve_dates("", "", 90)[1], "SYNA", "KRW",
                                                              1, 10.0) for i in range(3)])
        cursor = tools_v2.get_transactions(limit=1)["data"]["transactions"]["page"]["next_cursor"]
    real = server._resolve_dates
    monkeypatch.setattr(server, "_resolve_dates", lambda start, end, default_days=90: ("2025-01-01", "2025-03-31")
                        if not start and not end else real(start, end, default_days))
    assert tools_v2.get_transactions(limit=1, cursor=cursor)["error"]["code"] == "CURSOR_WINDOW_CHANGED"


def test_default_holdings_and_summary_are_under_budget(stub, monkeypatch):
    holdings = [fakes.Holding("kis", "kis:domestic", f"{i:06d}", "KRW", 1 + i, market="KR", last_price=100.0 + i,
                              avg_price=90.0, cost_basis=90.0 * (1 + i), market_value=(100.0 + i) * (1 + i))
                for i in range(40)]
    monkeypatch.setattr(fakes, "HOLDINGS", holdings)
    _many_transactions(monkeypatch, 400)
    for env in (tools_v2.get_holdings(), tools_v2.get_portfolio_summary_v2()):
        assert env["ok"] and _size(env) < BUDGET, _size(env)


def test_summary_rows_show_unknown_fee_and_tax_as_null_with_status(stub, monkeypatch):
    """An unknown fee/tax is null + status in summary rows (never 0.0); price validation kept."""
    known = fakes._tx("k1", "kis:domestic", TxType.BUY, "2026-03-02", "SYNK", "KRW", 2, 10.0)
    unknown = dataclasses.replace(fakes._tx("u1", "toss:primary", TxType.SELL, "2026-03-03", "SYNU", "USD", 3, 5.0),
                                  fee_status="unknown", tax_status="unsupported")
    monkeypatch.setattr(fakes, "TRANSACTIONS", [known, unknown])
    args = {"start": "2026-03-01", "end": "2026-03-31"}
    body = tools_v2.get_transactions(**args)["data"]["transactions"]
    full = {row["tx_id"]: row for row in tools_v2.get_transactions(**args, detail_level="full")["data"]
            ["transactions"]["rows"]}
    rows = {row["symbol"]: row for row in (dict(zip(body["columns"], values)) for values in body["rows"])}
    assert "fee" not in body["columns"] and "tax" not in body["columns"]
    assert rows["SYNU"]["fee_reported_amount"] is None and rows["SYNU"]["fee_status"] == "unknown"
    assert rows["SYNU"]["tax_reported_amount"] is None and rows["SYNU"]["tax_status"] == "unsupported"
    assert rows["SYNK"]["fee_reported_amount"] == 0 and rows["SYNK"]["fee_status"] == "verified_zero"
    for symbol, tx_id in (("SYNK", "k1"), ("SYNU", "u1")):
        assert rows[symbol]["price_validation.status"] == full[tx_id]["price_validation"]["status"] is not None
        assert all(rows[symbol][key] == full[tx_id][key] for key in rows[symbol] if "." not in key)


def test_crypto_history_pages_under_budget_with_cursor_checks(stub, monkeypatch):
    class Many(fakes.FakeFutures):
        def query(self, operation, venue="all", start="", end="", symbol=""):
            self.calls.append((operation, venue, start, end, symbol))
            rows = [{"time": 1_760_000_000_000 + i, "fill_id": f"f{i}", "symbol": "BTCUSDT", "side": "BUY",
                     "price": "100.5", "qty": "0.010", "fee": "0.001", "fee_asset": "USDT"} for i in range(900)]
            return {"venues": {"binance": {"fills": rows, "source_status": "ok", "result_status": "complete",
                                           "coverage": {"status": "complete"}, "reason_codes": []}},
                    "source_status": "ok", "coverage": {"status": "complete"}, "result_status": "complete",
                    "reason_codes": []}

    fake = Many()
    monkeypatch.setattr(server, "_crypto_futures_accounts", fake)
    first = tools_v2.get_crypto_account_history("fills", end="2026-10-07")
    assert _size(first) < BUDGET
    body = first["data"]["history"]
    assert body["page"]["total_count"] == 900 and body["rows"][0]["fill_id"] == "f899"
    cursor, seen = body["page"]["next_cursor"], body["page"]["returned"]
    assert seen == 50
    while cursor:
        body = tools_v2.get_crypto_account_history("fills", end="2026-10-07", limit=500,
                                                   cursor=cursor)["data"]["history"]
        seen += body["page"]["returned"]
        cursor = body["page"]["next_cursor"]
    assert seen == 900 and body["rows"][-1]["fill_id"] == "f0"
    assert {call[2:4] for call in fake.calls} == {("2026-10-01", "2026-10-07")}
    assert tools_v2.get_crypto_account_history("funding", cursor=first["data"]["history"]["page"]["next_cursor"]
                                               )["error"]["code"] == "INVALID_CURSOR"
    assert tools_v2.get_crypto_account_history("fills", limit=0)["error"]["code"] == "INVALID_LIMIT"


def test_quote_and_market_defaults_are_small(stub, monkeypatch):
    monkeypatch.setattr(server, "risk_free_rate", lambda ticker: {"ten_year_treasury_yield_pct": 4.1})
    monkeypatch.setattr(server, "yf", SimpleNamespace(Ticker=lambda symbol: None))
    monkeypatch.setattr(server, "_get_binance", fakes.FakeBinance)
    for env in (tools_v2.get_quote_v2([f"SYN{i}" for i in range(10)]), tools_v2.get_market_info(),
                tools_v2.get_crypto_market("BTCUSDT")):
        assert env["ok"] and _size(env) < BUDGET
    assert tools_v2.get_quote_v2([f"S{i}" for i in range(11)])["error"]["code"] == "INVALID_ARGUMENT"


def test_trading_pnl_summary_keeps_incomplete_rows_then_the_largest_realized(stub, monkeypatch):
    _many_transactions(monkeypatch, 900)
    view, field = "by_symbol", "by_symbol"
    full = tools_v2.get_trading_pnl(view=view, detail_level="full")["data"][field]["symbols"]
    env = tools_v2.get_trading_pnl(view=view)
    body = env["data"][field]
    limit = tools_v2.PNL_SUMMARY_ROWS[view]
    assert _size(env) < BUDGET and len(body["symbols"]) == limit < len(full)
    assert body["symbols_page"] == {"shown": limit, "total": len(full),
                                    "order": "incomplete_first_then_abs_realized_pnl_krw"}
    assert env["meta"]["detail"]["omitted"][f"data.{field}.symbols[]"] == len(full) - limit

    def krw(row):
        value = row.get("realized_pnl_krw")
        return abs(value) if isinstance(value, (int, float)) else -1

    shown = {(row["symbol"], row["currency"]) for row in body["symbols"]}
    complete = lambda row: row.get("status") == "complete"  # noqa: E731
    incomplete = [row for row in full if not complete(row)]
    if len(incomplete) >= limit:
        assert not any(complete(row) for row in body["symbols"])
    else:
        assert {(r["symbol"], r["currency"]) for r in incomplete} <= shown
        floor = min(krw(row) for row in body["symbols"] if complete(row))
        assert all(krw(row) <= floor for row in full if complete(row) and (row["symbol"], row["currency"]) not in shown)
    # equal rows: a summary row carries the full row's numbers
    by_key = {(row["symbol"], row["currency"]): row for row in full}
    for row in body["symbols"]:
        assert all(row[key] == by_key[(row["symbol"], row["currency"])][key] for key in row), row["symbol"]


def test_trading_pnl_round_trips_summary_is_the_trade_review_summary_body(stub, monkeypatch):
    """round_trips sits on get_trade_review's own summary (top 10 + reason groups)."""
    _many_transactions(monkeypatch, 900)
    full = tools_v2.get_trading_pnl(view="round_trips", detail_level="full")["data"]["round_trips"]["symbols"]
    env = tools_v2.get_trading_pnl(view="round_trips")
    body = env["data"]["round_trips"]
    old = response_shape.shape(server.get_trade_review(period="YTD"), response_shape.POLICIES["get_trade_review"],
                               "summary")["data"]
    assert _size(env) < BUDGET and "symbols_page" not in body
    assert body["symbols"] == old["symbols"] and body["symbol_reason_groups"] == old["symbol_reason_groups"]
    assert body["symbol_count"] == len(full) and body["omitted_symbols"] == len(full) - len(body["symbols"]) > 0
    assert [row["rank"] for row in body["symbols"]] == list(range(1, len(body["symbols"]) + 1))
    # No incomplete symbol is hidden: each is in symbol_reason_groups, the rows are the largest
    grouped = {tuple(pair) for group in body["symbol_reason_groups"] for pair in group["symbols"]}
    incomplete = {(row["symbol"], row["currency"]) for row in full
                  if row["reason_codes"] or row["realized"]["realized_pnl_krw"] is None}
    assert incomplete and incomplete <= grouped
    omitted = env["meta"]["detail"]["omitted"]
    assert "data.round_trips.symbols[]" not in omitted and "data.round_trips.reconciliation.positions" in omitted
