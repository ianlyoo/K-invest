"""Incremental ledger fetch.

Windows before the earliest row of a stored complete read are skipped; settled
windows are reused in-process. The real collector, normalizer and ledger writer run
on synthetic orders and a temporary DB; the transactions returned must be identical
to a full backfill.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import portfolio_ledger  # noqa: E402
import portfolio_sources  # noqa: E402
from portfolio_ledger import (  # noqa: E402
    history_anchors,
    observe_collection,
    transaction_stream,
)
from portfolio_sources import PortfolioCollector, Providers  # noqa: E402

TODAY = date.today().isoformat()
SOURCE = "toss:primary:orders"


def order(order_id, symbol, day):
    return {"orderId": order_id, "symbol": symbol, "side": "BUY", "status": "FILLED", "currency": "USD",
            "quantity": "2", "orderedAt": f"{day}T09:30:00+09:00",
            "execution": {"filledQuantity": "2", "averageFilledPrice": "10", "filledAmount": "20",
                          "commission": "0", "tax": "0", "filledAt": f"{day}T09:31:00+09:00", "settlementDate": day}}


ORDERS = [order("o1", "AAA", "2024-05-10"), order("o2", "BBB", "2025-01-15"), order("o3", "AAA", "2025-06-02")]


class FakeToss:
    """Closed orders filtered by date and symbol the way the broker does."""

    journal_account_key = "toss:primary"

    def __init__(self, orders=ORDERS, fail_windows=()):
        self.orders, self.fail_windows, self.windows = orders, set(fail_windows), []

    def iter_closed_orders(self, date_from=None, date_to=None, symbol=None, **kwargs):
        self.windows.append((date_from, date_to))
        if (date_from, date_to) in self.fail_windows:
            raise RuntimeError("synthetic broker failure")
        return [o for o in self.orders if date_from <= o["orderedAt"][:10] <= date_to
                and (not symbol or o["symbol"] == symbol)]


def collector(toss, db=None, anchored=True):
    return PortfolioCollector(Providers(
        toss_labels=lambda: ["primary"],
        toss_client=lambda label: toss,
        kis_accounts=lambda: [],
        kis_client=lambda label: None,
        error_formatter=lambda e: {"error": {"code": type(e).__name__, "message": str(e)}},
        fx_quote=lambda currency: None,
        ledger_observer=(lambda result: observe_collection(result, db)) if db else None,
        ledger_anchor=(lambda sources, start: history_anchors(sources, start, db)) if db and anchored else None,
    ))


def rows(collection):
    return sorted(repr(t) for t in collection.transactions)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "portfolio.db"


def test_anchor_skips_windows_before_the_earliest_stored_row_with_identical_rows(db):
    first = FakeToss()
    full = collector(first, db).collect_transactions("2000-01-01", TODAY)
    assert len(first.windows) > 100  # 90-day windows from 2000

    anchors = history_anchors([SOURCE], "2000-01-01", db)
    assert anchors[SOURCE]["earliest_row_date"] == "2024-05-10"
    assert anchors[SOURCE]["date"] == "2024-05-03"  # ANCHOR_MARGIN_DAYS before

    second = FakeToss()
    incremental = collector(second, db).collect_transactions("2000-01-01", TODAY)
    assert rows(incremental) == rows(full) and len(rows(full)) == 3
    assert min(start for start, _ in second.windows) <= "2024-05-03"
    assert all(end >= "2024-05-03" for _, end in second.windows)
    meta = incremental.source_metadata[SOURCE]
    assert meta["ledger_anchor"]["fetch_start"] == "2024-05-03"
    assert meta["ledger_anchor"]["evidence"] == "STORED_COMPLETE_READ"
    assert meta["ledger_anchor"]["skipped_windows"] == len(first.windows) - len(second.windows)
    assert meta["complete"] is True and incremental.source_metadata[SOURCE]["status"] == full.source_metadata[SOURCE]["status"]


def test_a_symbol_filtered_read_never_anchors_the_full_history(db):
    collector(FakeToss(), db).collect_transactions("2000-01-01", TODAY, symbol="BBB")
    assert history_anchors([SOURCE], "2000-01-01", db) == {}
    toss = FakeToss()
    result = collector(toss, db).collect_transactions("2000-01-01", TODAY)
    assert toss.windows[0][0] == "2000-01-01" and len(rows(result)) == 3


def test_a_read_that_started_later_does_not_anchor_an_earlier_request(db):
    collector(FakeToss(), db).collect_transactions("2025-01-01", TODAY)
    assert history_anchors([SOURCE], "2000-01-01", db) == {}
    assert history_anchors([SOURCE], "2025-01-01", db)[SOURCE]["earliest_row_date"] == "2025-01-15"


def test_partial_reads_do_not_anchor(db):
    toss = FakeToss(fail_windows={("2000-01-01", "2000-03-30")})
    result = collector(toss, db, anchored=False).collect_transactions("2000-01-01", TODAY)
    assert result.source_metadata[SOURCE]["complete"] is False
    assert history_anchors([SOURCE], "2000-01-01", db) == {}


def test_no_anchor_on_unreadable_store_or_explicit_full_backfill(db, monkeypatch, tmp_path):
    collector(FakeToss(), db).collect_transactions("2000-01-01", TODAY)
    assert history_anchors([SOURCE], "2000-01-01", tmp_path / "missing.db") == {}
    (tmp_path / "broken.db").write_bytes(b"not a database")
    assert history_anchors([SOURCE], "2000-01-01", tmp_path / "broken.db") == {}

    monkeypatch.setenv("KINVEST_LEDGER_FULL_BACKFILL", "1")
    toss = FakeToss()
    result = collector(toss, db).collect_transactions("2000-01-01", TODAY)
    assert toss.windows[0][0] == "2000-01-01" and "ledger_anchor" not in result.source_metadata[SOURCE]


def test_anchor_provider_failure_falls_back_to_full_backfill(db):
    toss = FakeToss()
    broken = collector(toss)
    broken._providers.ledger_anchor = lambda sources, start: 1 / 0
    result = broken.collect_transactions("2000-01-01", TODAY)
    assert toss.windows[0][0] == "2000-01-01" and len(rows(result)) == 3


def test_settled_windows_are_reused_and_recent_ones_read_again(monkeypatch):
    toss = FakeToss()
    owner = collector(toss)
    start = (date.today() - timedelta(days=400)).isoformat()
    first = owner.collect_transactions(start, TODAY)
    fetched = list(toss.windows)
    toss.windows.clear()
    second = owner.collect_transactions(start, TODAY)
    settled = (date.today() - timedelta(days=portfolio_sources.SETTLED_WINDOW_DAYS + 1)).isoformat()
    assert rows(second) == rows(first)
    assert toss.windows and all(end > settled for _, end in toss.windows)
    assert second.source_metadata[SOURCE]["reused_settled_windows"] == len(fetched) - len(toss.windows)

    monkeypatch.setattr(portfolio_sources, "WINDOW_CACHE_SECONDS", 0)
    toss.windows.clear()
    owner.collect_transactions(start, TODAY)
    assert len(toss.windows) == len(fetched)


def test_failed_windows_are_never_reused():
    start = (date.today() - timedelta(days=400)).isoformat()
    first_window = (start, (date.fromisoformat(start) + timedelta(days=89)).isoformat())
    toss = FakeToss(fail_windows={first_window})
    owner = collector(toss)
    owner.collect_transactions(start, TODAY)
    toss.windows.clear()
    toss.fail_windows.clear()
    owner.collect_transactions(start, TODAY)
    assert first_window in toss.windows


def test_strict_daily_collector_reads_every_window():
    assert transaction_stream(SOURCE) == "toss/" + portfolio_ledger.digest(SOURCE)[:16]
    toss = FakeToss()
    owner = collector(toss)
    owner._strict_toss_fills = True
    start = (date.today() - timedelta(days=400)).isoformat()
    owner._providers.toss_client = lambda label: toss
    toss.get_fill_history_evidence = lambda *a, **k: {"complete": False, "transactions": []}
    owner.collect_transactions(start, TODAY)
    count = len(toss.windows)
    toss.windows.clear()
    owner.collect_transactions(start, TODAY)
    assert len(toss.windows) == count
