"""Broker fan-out: isolation of failures, account filtering, and health reporting."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from kinvest_common import NotConfiguredError
from portfolio_sources import PortfolioCollector, Providers

TOSS_HOLDINGS = {
    "result": {
        "items": [
            {
                "symbol": "AAPL",
                "name": "Apple",
                "marketCountry": "US",
                "currency": "USD",
                "quantity": "10",
                "lastPrice": "180",
                "averagePurchasePrice": "150",
                "marketValue": {"purchaseAmount": "1500", "amount": "1800"},
                "profitLoss": {"amount": "300", "rate": "0.2"},
            }
        ]
    }
}

TOSS_ORDERS = [
    {
        "orderId": "o1",
        "symbol": "AAPL",
        "side": "BUY",
        "status": "FILLED",
        "currency": "USD",
        "quantity": "10",
        "orderedAt": "2026-02-01T09:30:00+09:00",
        "execution": {
            "filledQuantity": "10",
            "averageFilledPrice": "150",
            "filledAmount": "1500",
            "commission": "1",
            "tax": "0",
            "filledAt": "2026-02-01T09:31:00+09:00",
            "settlementDate": "2026-02-03",
        },
    }
]

KIS_DOMESTIC = {
    "stocks": [
        {
            "pdno": "005930",
            "hldg_qty": "10",
            "pchs_avg_pric": "65000",
            "pchs_amt": "650000",
            "prpr": "70000",
            "evlu_amt": "700000",
            "evlu_pfls_amt": "50000",
        }
    ],
    "summary": [{"dnca_tot_amt": "1000000"}],
}

KIS_OVERSEAS = {
    "stocks": [
        {
            "ovrs_pdno": "7203",
            "ovrs_cblc_qty": "100",
            "pchs_avg_pric": "2500",
            "frcr_pchs_amt1": "250000",
            "ovrs_stck_evlu_amt": "280000",
            "now_pric2": "2800",
            "market": "JP",
            "exchange": "TSE",
            "currency": "JPY",
        }
    ],
    "cash": [{"crcy_cd": "JPY", "frcr_dncl_amt_2": "5000"}],
    "errors": {},
}


class FakeToss:
    def __init__(self, holdings=None, orders=None, fail=None):
        self._holdings = holdings if holdings is not None else TOSS_HOLDINGS
        self._orders = orders if orders is not None else TOSS_ORDERS
        self._fail = fail

    def get_holdings(self, symbol=None):
        if self._fail:
            raise self._fail
        return self._holdings

    def get_buying_power(self, currency="USD"):
        return {"result": {"currency": currency, "cashBuyingPower": "1000"}}

    def iter_closed_orders(self, date_from=None, date_to=None, symbol=None, **kwargs):
        if self._fail:
            raise self._fail
        return self._orders


class FakeKIS:
    def __init__(self, fail_domestic=None, fail_overseas=None, fail_period_trans=None, overseas=None):
        self._fail_domestic = fail_domestic
        self._fail_overseas = fail_overseas
        self._fail_period_trans = fail_period_trans
        self._overseas = overseas if overseas is not None else KIS_OVERSEAS
        self.calls: list[str] = []

    def get_domestic_balance(self):
        self.calls.append("domestic_balance")
        if self._fail_domestic:
            raise self._fail_domestic
        return KIS_DOMESTIC

    def get_overseas_balance(self):
        self.calls.append("overseas_balance")
        if self._fail_overseas:
            raise self._fail_overseas
        return self._overseas

    def get_domestic_trade_history(self, start, end):
        self.calls.append(f"domestic_trades:{start}:{end}")
        return {"trades": [], "summary": {}}

    def get_overseas_period_trans(self, start, end, exchange="%"):
        self.calls.append(f"period_trans:{start}:{end}")
        if self._fail_period_trans:
            raise self._fail_period_trans
        return {"transactions": [], "summary": []}

    def get_overseas_trade_history(self, start, end, **kwargs):
        self.calls.append(f"ccnl:{start}:{end}")
        return {
            "trades": [
                {
                    "ord_dt": "20260201",
                    "odno": "1",
                    "sll_buy_dvsn_cd": "02",
                    "pdno": "7203",
                    "ft_ccld_qty": "100",
                    "ft_ccld_unpr3": "2500",
                    "ft_ccld_amt3": "250000",
                    "tr_crcy_cd": "JPY",
                    "ovrs_excg_cd": "TKSE",
                }
            ]
        }


@dataclass
class FakeKISAccount:
    """Stands in for kis_accounts.KISAccount: label + overseas capability."""

    label: str = "primary"
    supports_overseas: bool = True


def build(toss_clients=None, kis=None, toss_labels=None, fx=None, kis_accounts=None, kis_clients=None):
    toss_clients = toss_clients if toss_clients is not None else {"primary": FakeToss()}
    labels = toss_labels if toss_labels is not None else list(toss_clients)
    kis_clients = kis_clients if kis_clients is not None else {}
    accounts = kis_accounts if kis_accounts is not None else [FakeKISAccount()]

    def label_provider():
        if isinstance(labels, Exception):
            raise labels
        return labels

    def account_provider():
        if isinstance(accounts, Exception):
            raise accounts
        return accounts

    def kis_for(label):
        if label in kis_clients:
            return kis_clients[label]
        return kis if kis is not None else FakeKIS()

    def error_formatter(e):
        code = f"{e.provider.upper()}_NOT_CONFIGURED" if isinstance(e, NotConfiguredError) else type(e).__name__
        return {"error": {"code": code, "message": str(e)}}

    return PortfolioCollector(
        Providers(
            toss_labels=label_provider,
            toss_client=lambda label: toss_clients[label],
            kis_accounts=account_provider,
            kis_client=kis_for,
            error_formatter=error_formatter,
            fx_quote=fx if fx is not None else (lambda currency: {"USD": 1300.0, "JPY": 9.0}.get(currency)),
        )
    )


# ── Positions ────────────────────────────────────────────


def test_collect_positions_merges_every_provider():
    collection = build(kis=FakeKIS()).collect_positions()
    symbols = {h.symbol for h in collection.holdings}
    assert symbols == {"AAPL", "005930", "7203"}
    accounts = {h.account for h in collection.holdings}
    assert accounts == {"toss:primary", "kis:domestic", "kis:overseas"}
    # Cash from all three: Toss KRW + USD buying power, KIS KRW, KIS JPY.
    assert {(c.account, c.currency) for c in collection.cash} == {
        ("toss:primary", "KRW"),
        ("toss:primary", "USD"),
        ("kis:domestic", "KRW"),
        ("kis:overseas", "JPY"),
    }
    assert collection.failed_sources == []


def test_kis_failure_does_not_lose_toss_holdings():
    kis = FakeKIS(fail_domestic=RuntimeError("KIS error: 조회 실패"))
    collection = build(kis=kis).collect_positions()
    assert {h.symbol for h in collection.holdings} == {"AAPL", "7203"}
    assert collection.health["kis:domestic"]["status"] == "error"
    assert "kis:domestic" in collection.failed_sources
    assert collection.health["toss:primary"]["status"] == "ok"


def test_unconfigured_provider_is_reported_as_not_configured():
    kis = FakeKIS(fail_domestic=NotConfiguredError("kis", "no creds"), fail_overseas=NotConfiguredError("kis", "no creds"))
    collection = build(kis=kis).collect_positions()
    assert collection.health["kis:domestic"]["status"] == "not_configured"
    assert collection.health["kis:overseas"]["status"] == "not_configured"
    assert {h.symbol for h in collection.holdings} == {"AAPL"}


def test_toss_registry_failure_is_reported_and_kis_still_collected():
    collection = build(toss_labels=NotConfiguredError("toss", "no creds"), kis=FakeKIS()).collect_positions()
    assert collection.health["toss"]["status"] == "not_configured"
    assert {h.symbol for h in collection.holdings} == {"005930", "7203"}


def test_per_market_overseas_errors_surface_as_their_own_health_rows():
    overseas = {**KIS_OVERSEAS, "errors": {"SEHK": "KIS error: 권한 없음"}}
    collection = build(kis=FakeKIS(overseas=overseas)).collect_positions()
    assert collection.health["kis:overseas:SEHK"]["status"] == "error"
    assert collection.health["kis:overseas"]["status"] == "ok"


def test_multiple_toss_accounts_are_labeled_separately():
    clients = {"primary": FakeToss(), "secondary": FakeToss()}
    collection = build(toss_clients=clients, kis=FakeKIS()).collect_positions()
    toss_accounts = {h.account for h in collection.holdings if h.provider == "toss"}
    assert toss_accounts == {"toss:primary", "toss:secondary"}


def test_pension_account_collects_alongside_the_primary_account():
    primary, pension = FakeKIS(), FakeKIS()
    collection = build(
        kis_accounts=[FakeKISAccount("primary"), FakeKISAccount("pension", supports_overseas=False)],
        kis_clients={"primary": primary, "pension": pension},
    ).collect_positions()

    kis_accounts = {h.account for h in collection.holdings if h.provider == "kis"}
    assert kis_accounts == {"kis:domestic", "kis:overseas", "kis:pension:domestic"}
    assert ("kis:pension:domestic", "KRW") in {(c.account, c.currency) for c in collection.cash}
    assert collection.failed_sources == []


def test_pension_account_never_queries_the_overseas_endpoints():
    pension = FakeKIS()
    collection = build(
        kis_accounts=[FakeKISAccount("pension", supports_overseas=False)],
        kis_clients={"pension": pension},
    ).collect_positions()

    assert pension.calls == ["domestic_balance"]
    assert "kis:pension:overseas" not in collection.health


def test_one_kis_account_failing_leaves_the_others_intact():
    pension = FakeKIS(fail_domestic=RuntimeError("KIS error: 조회 실패"))
    collection = build(
        kis_accounts=[FakeKISAccount("primary"), FakeKISAccount("pension", supports_overseas=False)],
        kis_clients={"primary": FakeKIS(), "pension": pension},
    ).collect_positions()

    assert collection.health["kis:pension:domestic"]["status"] == "error"
    assert collection.health["kis:domestic"]["status"] == "ok"
    assert "005930" in {h.symbol for h in collection.holdings}


def test_kis_registry_failure_is_reported_once_for_the_provider():
    collection = build(kis_accounts=NotConfiguredError("kis", "no creds")).collect_positions()

    assert collection.health["kis"]["status"] == "not_configured"
    assert {h.symbol for h in collection.holdings} == {"AAPL"}


def test_account_filter_selects_one_kis_account():
    primary, pension = FakeKIS(), FakeKIS()
    collection = build(
        kis_accounts=[FakeKISAccount("primary"), FakeKISAccount("pension", supports_overseas=False)],
        kis_clients={"primary": primary, "pension": pension},
    ).collect_positions(accounts=["kis:pension:domestic"])

    assert primary.calls == []
    assert pension.calls == ["domestic_balance"]
    assert {h.account for h in collection.holdings} == {"kis:pension:domestic"}


def test_account_filter_limits_the_fan_out():
    kis = FakeKIS()
    collection = build(kis=kis).collect_positions(accounts=["kis:overseas"])
    assert {h.account for h in collection.holdings} == {"kis:overseas"}
    assert kis.calls == ["overseas_balance"]


# ── Transactions ─────────────────────────────────────────


def test_collect_transactions_normalizes_and_sorts():
    collection = build(kis=FakeKIS()).collect_transactions("2026-01-01", "2026-03-01")
    assert [tx.symbol for tx in collection.transactions] == ["AAPL"]
    assert collection.transactions[0].account == "toss:primary"
    assert collection.transactions[0].trade_date == "2026-02-01"


def test_pension_transactions_are_labeled_with_their_account():
    pension = FakeKIS()
    collection = build(
        kis_accounts=[FakeKISAccount("pension", supports_overseas=False)],
        kis_clients={"pension": pension},
        toss_clients={},
    ).collect_transactions("2026-01-01", "2026-03-01")

    assert pension.calls == ["domestic_trades:20260101:20260301"]
    assert collection.health["kis:pension:domestic:trades"]["status"] == "ok"
    assert "kis:pension:overseas:trades" not in collection.health


def test_kis_dates_are_converted_to_yyyymmdd():
    kis = FakeKIS()
    build(kis=kis).collect_transactions("2026-01-01", "2026-03-01")
    assert "domestic_trades:20260101:20260301" in kis.calls
    assert "period_trans:20260101:20260301" in kis.calls


def test_overseas_falls_back_to_ccnl_when_period_trans_fails():
    kis = FakeKIS(fail_period_trans=RuntimeError("KIS error: CTOS4001R 불가"))
    collection = build(kis=kis).collect_transactions("2026-01-01", "2026-03-01")
    assert any(call.startswith("ccnl:") for call in kis.calls)
    fallback = [tx for tx in collection.transactions if tx.symbol == "7203"]
    assert len(fallback) == 1
    assert fallback[0].source == "kis:inquire-ccnl"
    assert collection.health["kis:overseas:trades"]["status"] == "error"
    assert collection.health["kis:overseas:trades_fallback"]["status"] == "ok"


def test_symbol_filter_applies_after_normalization():
    collection = build(kis=FakeKIS()).collect_transactions("2026-01-01", "2026-03-01", symbol="aapl")
    assert {tx.symbol for tx in collection.transactions} == {"AAPL"}
    empty = build(kis=FakeKIS()).collect_transactions("2026-01-01", "2026-03-01", symbol="TSLA")
    assert empty.transactions == []


# ── Health tracking ──────────────────────────────────────


def test_health_records_last_success_and_consecutive_failures():
    kis = FakeKIS(fail_domestic=RuntimeError("boom"))
    collector = build(kis=kis)
    collector.collect_positions()
    collector.collect_positions()
    report = collector.health_report()
    assert report["kis:domestic"]["consecutive_failures"] == 2
    assert report["kis:domestic"]["last_error"]["message"] == "boom"
    assert report["toss:primary"]["last_success_at"] is not None
    assert report["toss:primary"]["latency_ms"] is not None


def test_health_resets_failure_count_after_recovery():
    kis = FakeKIS(fail_domestic=RuntimeError("boom"))
    collector = build(kis=kis)
    collector.collect_positions()
    kis._fail_domestic = None
    collector.collect_positions()
    report = collector.health_report()
    assert report["kis:domestic"]["status"] == "ok"
    assert report["kis:domestic"]["consecutive_failures"] == 0
    assert report["kis:domestic"]["last_error"] is not None  # history is kept


# ── FX ───────────────────────────────────────────────────


def test_build_fx_only_quotes_needed_currencies():
    asked: list[str] = []

    def quote(currency):
        asked.append(currency)
        return {"USD": 1300.0}.get(currency)

    fx = build(fx=quote).build_fx({"KRW", "USD", "JPY"})
    assert set(asked) == {"USD", "JPY"}  # KRW is never quoted
    assert fx.rates == {"USD": 1300.0}
    assert fx.missing == {"JPY"}


def test_build_fx_survives_a_quote_provider_exception():
    def quote(currency):
        raise RuntimeError("yfinance down")

    fx = build(fx=quote).build_fx({"USD"})
    assert fx.rates == {}
    assert fx.missing == {"USD"}


# ── Truncation ───────────────────────────────────────────


class TruncatingKIS(FakeKIS):
    """KIS whose paged endpoints report that rows were cut short."""

    def get_overseas_balance(self):
        return {**KIS_OVERSEAS, "truncated": True, "truncated_markets": ["NASD"]}

    def get_domestic_trade_history(self, start, end):
        return {"trades": [], "summary": {}, "truncated": True}


def test_truncated_source_is_recorded_and_flagged_in_health():
    collection = build(kis=TruncatingKIS()).collect_positions()
    assert collection.truncated_sources == ["kis:overseas"]
    assert collection.health["kis:overseas"]["status"] == "truncated"
    assert "incomplete" in collection.health["kis:overseas"]["warning"]


def test_truncation_is_not_treated_as_a_failed_source():
    """Rows arrived, just not all of them — the data is usable but incomplete."""
    collection = build(kis=TruncatingKIS()).collect_positions()
    assert "kis:overseas" not in collection.failed_sources
    assert {h.symbol for h in collection.holdings} == {"AAPL", "005930", "7203"}


def test_truncated_transactions_are_recorded():
    collection = build(kis=TruncatingKIS()).collect_transactions("2026-01-01", "2026-03-01")
    assert "kis:domestic:trades" in collection.truncated_sources


def test_untruncated_collection_reports_none():
    collection = build(kis=FakeKIS()).collect_positions()
    assert collection.truncated_sources == []
