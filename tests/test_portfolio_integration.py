"""End-to-end: broker payloads in, tool envelopes with real numbers out.

Drives the actual MCP tool functions with a collector wired to fake Toss/KIS
clients, so provider payload -> normalization -> engine math -> envelope is
covered in one pass, including the NAV snapshot write.
"""

from __future__ import annotations

import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_sources import PortfolioCollector, Providers
from portfolio_store import NavStore

TODAY = "2026-10-05"
YEAR = TODAY[:4]

TOSS_HOLDINGS = {
    "result": {
        "items": [
            {
                "symbol": "AAPL",
                "name": "Apple Inc.",
                "marketCountry": "US",
                "currency": "USD",
                "quantity": "10",
                "lastPrice": "200",
                "averagePurchasePrice": "150",
                "marketValue": {"purchaseAmount": "1500", "amount": "2000"},
                "profitLoss": {"amount": "500", "rate": "0.3333"},
            },
            {
                "symbol": "005930",
                "name": "삼성전자",
                "marketCountry": "KR",
                "currency": "KRW",
                "quantity": "100",
                "lastPrice": "70000",
                "averagePurchasePrice": "60000",
                "marketValue": {"purchaseAmount": "6000000", "amount": "7000000"},
                "profitLoss": {"amount": "1000000", "rate": "0.1667"},
            },
        ]
    }
}

TOSS_ORDERS = [
    {
        "orderId": "buy-aapl",
        "symbol": "AAPL",
        "side": "BUY",
        "status": "FILLED",
        "currency": "USD",
        "quantity": "20",
        "orderedAt": f"{YEAR}-02-01T09:30:00+09:00",
        "execution": {
            "filledQuantity": "20",
            "averageFilledPrice": "150",
            "filledAmount": "3000",
            "commission": "3",
            "tax": "0",
            "filledAt": f"{YEAR}-02-01T09:31:00+09:00",
            "settlementDate": f"{YEAR}-02-03",
        },
    },
    {
        "orderId": "sell-aapl",
        "symbol": "AAPL",
        "side": "SELL",
        "status": "FILLED",
        "currency": "USD",
        "quantity": "10",
        "orderedAt": f"{YEAR}-03-01T09:30:00+09:00",
        "execution": {
            "filledQuantity": "10",
            "averageFilledPrice": "180",
            "filledAmount": "1800",
            "commission": "2",
            "tax": "1",
            "filledAt": f"{YEAR}-03-01T09:31:00+09:00",
            "settlementDate": f"{YEAR}-03-03",
        },
    },
]

KIS_DOMESTIC = {
    "stocks": [
        {
            "pdno": "005930",
            "prdt_name": "삼성전자",
            "hldg_qty": "200",
            "pchs_avg_pric": "65000",
            "pchs_amt": "13000000",
            "prpr": "70000",
            "evlu_amt": "14000000",
            "evlu_pfls_amt": "1000000",
            "evlu_pfls_rt": "7.69",
        }
    ],
    "summary": [{"dnca_tot_amt": "2000000"}],
}

KIS_OVERSEAS = {
    "stocks": [
        {
            "ovrs_pdno": "7203",
            "ovrs_item_name": "TOYOTA MOTOR",
            "ovrs_cblc_qty": "100",
            "pchs_avg_pric": "2500",
            "frcr_pchs_amt1": "250000",
            "ovrs_stck_evlu_amt": "280000",
            "now_pric2": "2800",
            "frcr_evlu_pfls_amt": "30000",
            "market": "JP",
            "exchange": "TSE",
            "currency": "JPY",
        }
    ],
    "cash": [{"crcy_cd": "JPY", "frcr_dncl_amt_2": "50000"}],
    "errors": {},
}


class FakeToss:
    def get_holdings(self, symbol=None):
        return TOSS_HOLDINGS

    def get_buying_power(self, currency="USD"):
        amount = "1000000" if currency == "KRW" else "500"
        return {"result": {"currency": currency, "cashBuyingPower": amount}}

    def iter_closed_orders(self, date_from=None, date_to=None, symbol=None, **kwargs):
        return TOSS_ORDERS


class FakeKIS:
    def get_domestic_balance(self):
        return KIS_DOMESTIC

    def get_overseas_balance(self):
        return KIS_OVERSEAS

    def get_domestic_trade_history(self, start, end):
        return {"trades": [], "summary": {}}

    def get_overseas_period_trans(self, start, end, exchange="%"):
        return {"transactions": [], "summary": []}


@pytest.fixture(autouse=True)
def local_clock(monkeypatch, request):
    """Pin both the host calendar and UTC collection time at any test-run hour."""
    import portfolio_engine
    import portfolio_sources
    import portfolio_store
    import server

    hour = getattr(request, "param", "12:00")
    instant = datetime.fromisoformat(f"{TODAY}T{hour}:00").replace(tzinfo=ZoneInfo("Asia/Seoul"))

    class FixedDate(date):
        @classmethod
        def today(cls):
            return instant.date()

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    with monkeypatch.context() as clock_patch:
        clock_patch.setenv("TZ", "Asia/Seoul")
        time.tzset()
        clock_patch.setattr(portfolio_engine, "date", FixedDate)
        clock_patch.setattr(portfolio_store, "date", FixedDate)
        clock_patch.setattr(server, "datetime", FixedDatetime)
        utc_now = instant.astimezone(timezone.utc).isoformat(timespec="seconds")
        clock_patch.setattr(portfolio_sources, "_now", lambda: utc_now)
        clock_patch.setattr(portfolio_store, "_now", lambda: utc_now)
        yield
    time.tzset()


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """server module with the collector and NAV store pointed at fakes."""
    import server

    collector = PortfolioCollector(
        Providers(
            toss_labels=lambda: ["primary"],
            toss_client=lambda label: FakeToss(),
            kis_accounts=lambda: [SimpleNamespace(label="primary", supports_overseas=True)],
            kis_client=lambda label: FakeKIS(),
            error_formatter=server._format_error,
            fx_quote=lambda currency: {"USD": 1300.0, "JPY": 9.0}.get(currency),
        )
    )
    store = NavStore(tmp_path / "portfolio.db")
    monkeypatch.setattr(server, "_collector", collector)
    monkeypatch.setattr(server, "_nav_store", store)
    monkeypatch.setattr(server, "_get_collector", lambda: collector)
    monkeypatch.setattr(server, "_get_nav_store", lambda: store)
    monkeypatch.setattr(server, "_risk_free_decimal", lambda: 0.04)
    return server, store


def test_consolidated_holdings_merges_toss_and_kis(wired):
    server, _ = wired
    data = server.get_consolidated_holdings(detail_level="full")["data"]
    by_symbol = {p["symbol"]: p for p in data["positions"]}

    assert set(by_symbol) == {"AAPL", "005930", "7203"}
    samsung = by_symbol["005930"]
    # 100 @ 60,000 (Toss) + 200 @ 65,000 (KIS) = 300 @ 63,333.33
    assert samsung["quantity"] == pytest.approx(300)
    assert samsung["avg_price"] == pytest.approx(63_333.333, abs=0.01)
    assert samsung["cost_basis"] == pytest.approx(19_000_000)
    assert samsung["market_value"] == pytest.approx(21_000_000)
    assert samsung["account_count"] == 2

    # USD and JPY converted at the fake spot rates.
    assert by_symbol["AAPL"]["market_value_krw"] == pytest.approx(2_000 * 1300)
    assert by_symbol["7203"]["market_value_krw"] == pytest.approx(280_000 * 9)
    assert data["totals"]["market_value_krw"] == pytest.approx(21_000_000 + 2_600_000 + 2_520_000)
    assert set(data["by_market"]) == {"KR", "US", "JP"}
    assert data["sources_failed"] == []


def test_portfolio_summary_totals_and_snapshot_write(wired):
    server, store = wired
    result = server.get_portfolio_summary(snapshot=True)
    data = result["data"]

    assert result["ok"] is True
    stock = 21_000_000 + 2_600_000 + 2_520_000
    cash = 1_000_000 + 500 * 1300 + 2_000_000 + 50_000 * 9
    assert data["stock_value_krw"] == pytest.approx(stock)
    assert data["cash_krw"] == pytest.approx(cash)
    assert data["total_assets_krw"] == pytest.approx(stock + cash)
    assert data["position_count"] == 3
    assert set(data["by_account"]) == {"toss:primary", "kis:domestic", "kis:overseas"}

    # Snapshot persisted for today, and readable through get_portfolio_history.
    assert data["snapshot"]["stored"] is True
    assert store.count() == 1
    history = server.get_portfolio_history(period="MTD")["data"]
    assert history["latest"]["total_value_krw"] == pytest.approx(stock + cash)


def test_realized_pnl_from_toss_round_trip(wired):
    server, _ = wired
    data = server.get_realized_pnl(period="YTD")["data"]
    entry = next(e for e in data["symbols"] if e["symbol"] == "AAPL")

    # Bought 20 @ 150 + 3 fee -> avg 150.15; sold 10 @ 180 less 3 costs.
    assert entry["quantity_sold"] == pytest.approx(10)
    assert entry["cost_basis"] == pytest.approx(1_501.5)
    assert entry["proceeds"] == pytest.approx(1_797.0)
    assert entry["realized_pnl"] == pytest.approx(295.5)
    assert entry["realized_pnl_krw"] == pytest.approx(295.5 * 1300)
    assert entry["cost_basis_complete"] is True
    assert data["cost_basis_method"] == "moving_average"
    # 10 shares remain on the books at the same average.
    assert data["open_positions_from_ledger"]["AAPL:USD"]["quantity"] == pytest.approx(10)
    assert data["totals"]["fees_by_currency"]["USD"] == pytest.approx(5.0)
    assert data["totals"]["taxes_by_currency"]["USD"] == pytest.approx(1.0)


def test_all_transactions_normalizes_both_sides(wired):
    server, _ = wired
    data = server.get_all_transactions(start_date=f"{YEAR}-01-01", end_date=TODAY)["data"]

    assert data["transaction_count"] == 2
    assert data["count_by_type"] == {"BUY": 1, "SELL": 1}
    assert data["count_by_account"] == {"toss:primary": 2}
    buy = next(tx for tx in data["transactions"] if tx["tx_type"] == "BUY")
    assert buy["net_amount"] == pytest.approx(-3_003)
    assert buy["settle_date"] == f"{YEAR}-02-03"
    assert data["limitations"]  # the caller is told what isn't covered


def test_all_transactions_summary_omits_entries(wired):
    server, _ = wired
    data = server.get_all_transactions(start_date=f"{YEAR}-01-01", detail_level="summary")["data"]
    assert "transactions" not in data
    assert data["transaction_count"] == 2


def test_performance_combines_snapshots_ledger_and_holdings(wired):
    server, store = wired
    stock = 21_000_000 + 2_600_000 + 2_520_000
    cash = 1_000_000 + 500 * 1300 + 2_000_000 + 50_000 * 9
    total = stock + cash
    # Two prior snapshots so TWR and risk statistics have a series to work with.
    store.upsert_snapshot({"total_assets_krw": total * 0.8, "position_count": 3}, snapshot_date=f"{YEAR}-01-02")
    store.upsert_snapshot({"total_assets_krw": total * 0.9, "position_count": 3}, snapshot_date=f"{YEAR}-01-03")

    data = server.get_portfolio_performance(period="YTD", detail_level="full")["data"]

    assert data["start_nav_krw"] == pytest.approx(total * 0.8)
    assert data["end_nav_krw"] == pytest.approx(total)
    assert data["returns"]["simple_return_pct"] is None
    assert data["returns"]["twr_pct"] is None
    assert data["raw_asset_change_pct"] == pytest.approx(25.0)
    # The fixture's Samsung/Toyota holdings have no opening or trade evidence.
    # Quantity breaks invalidate account-level returns and P&L; the snapshot
    # date mismatch is reported alongside.
    assert {"MISSING_EXTERNAL_CASHFLOWS", "RECONCILIATION_BREAK", "SNAPSHOT_DATE_MISMATCH"} <= set(data["metrics"]["twr_pct"]["reason_codes"])
    assert data["pnl"]["realized_krw"] is None
    assert data["pnl"]["unrealized_krw"] is None
    assert data["metrics"]["realized_krw"]["reason_codes"] == ["RECONCILIATION_BREAK"]
    # Risk statistics come from stored snapshots only; today's live NAV feeds
    # end_nav_krw but is not appended to the series.
    assert data["risk"]["observations"] == 2
    assert data["nav_observations"] == 2
    assert data["risk"]["risk_free_rate_used"] == 0.04
    assert "pnl_series" in data
    assert data["methodology"]["base_currency"] == "KRW"


def test_account_filter_narrows_holdings(wired):
    server, _ = wired
    data = server.get_all_transactions(
        start_date=f"{YEAR}-01-01", account="kis", detail_level="summary"
    )["data"]
    # Only KIS was queried, and the fakes report no KIS trades.
    assert data["transaction_count"] == 0
    assert data["account_filter"] == "kis"


def test_snapshot_tool_is_idempotent_per_day(wired):
    server, store = wired
    first = server.snapshot_portfolio_nav()
    second = server.snapshot_portfolio_nav()
    assert first["ok"] is True and second["ok"] is True
    assert store.count() == 1
    assert second["data"]["snapshot"]["snapshot_date"] == TODAY


def test_account_health_reports_all_ok(wired):
    server, _ = wired
    data = server.get_account_health(detail_level="full")["data"]
    assert data["overall_status"] == "ok"
    assert data["sources_failed"] == []
    assert data["holdings_count_by_account"]["toss:primary"] == 2
    assert data["cash_count_by_account"]["kis:overseas"] == 1
    assert data["data_freshness"]["positions_fetched_at"]


@pytest.mark.parametrize("local_clock", ["00:30"], indirect=True)
def test_kst_midnight_break_reaches_realized_and_performance(wired):
    server, _ = wired
    from portfolio_sources import _now

    assert _now() == "2026-10-04T15:30:00+00:00"
    realized = server.get_realized_pnl(period="YTD")["data"]
    performance = server.get_portfolio_performance(period="YTD")["data"]
    assert realized["reconciliation"]["status"] == "unreconciled"
    assert realized["reconciliation"]["unexplained_quantity_breaks"] > 0
    assert performance["metrics"]["realized_krw"]["reason_codes"] == ["RECONCILIATION_BREAK"]
    assert "RECONCILIATION_BREAK" in performance["metrics"]["twr_pct"]["reason_codes"]


def test_summary_default_does_not_write_nav(wired, monkeypatch):
    server, store = wired
    monkeypatch.setattr(store, 'upsert_snapshot', lambda *a, **k: pytest.fail('unexpected NAV write'))
    monkeypatch.setattr(store, 'record_attempt', lambda *a, **k: pytest.fail('unexpected attempt write'))
    result = server.get_portfolio_summary(include_realized=False)
    assert result['ok'] and result['data']['snapshot'] == {'stored': False, 'reason': 'snapshot disabled'}
    assert not store.db_path.exists()
