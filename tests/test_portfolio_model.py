"""Broker payload → normalized domain model.

The Toss fixtures are the published example payloads from
https://openapi.tossinvest.com/openapi-docs/latest/openapi.json; the KIS fixtures
use the documented output field names for TTTC8434R / TTTS3012R / TTTC0081R /
CTOS4001R / TTTS3035R.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import portfolio_model as pm
from portfolio_model import TxType

# ── Fixtures ─────────────────────────────────────────────

TOSS_HOLDINGS = {
    "result": {
        "totalPurchaseAmount": {"krw": "6500000", "usd": "1553"},
        "marketValue": {"amount": {"krw": "7200000", "usd": "1785"}},
        "items": [
            {
                "symbol": "005930",
                "name": "삼성전자",
                "marketCountry": "KR",
                "currency": "KRW",
                "quantity": "100",
                "lastPrice": "72000",
                "averagePurchasePrice": "65000",
                "marketValue": {"purchaseAmount": "6500000", "amount": "7200000"},
                "profitLoss": {"amount": "700000", "rate": "0.1077"},
                "cost": {"commission": "14400", "tax": "135600"},
            },
            {
                "symbol": "AAPL",
                "name": "Apple Inc.",
                "marketCountry": "US",
                "currency": "USD",
                "quantity": "10",
                "lastPrice": "178.5",
                "averagePurchasePrice": "155.3",
                "marketValue": {"purchaseAmount": "1553", "amount": "1785"},
                "profitLoss": {"amount": "232", "rate": "0.1494"},
            },
        ],
    }
}

TOSS_ORDER_FILLED = {
    "orderId": "abc123",
    "symbol": "005930",
    "side": "BUY",
    "orderType": "LIMIT",
    "status": "FILLED",
    "price": "70000",
    "quantity": "10",
    "currency": "KRW",
    "orderedAt": "2026-03-29T09:30:00+09:00",
    "execution": {
        "filledQuantity": "10",
        "averageFilledPrice": "70000",
        "filledAmount": "700000",
        "commission": "1400",
        "tax": "0",
        "filledAt": "2026-03-28T09:31:15+09:00",
        "settlementDate": "2026-03-30",
    },
}

KIS_DOMESTIC_BALANCE = {
    "stocks": [
        {
            "pdno": "005930",
            "prdt_name": "삼성전자",
            "hldg_qty": "100",
            "pchs_avg_pric": "65000",
            "pchs_amt": "6500000",
            "prpr": "72000",
            "evlu_amt": "7200000",
            "evlu_pfls_amt": "700000",
            "evlu_pfls_rt": "10.77",
        },
        {"pdno": "900300", "hldg_qty": "0", "pchs_amt": "0"},  # sold out — must drop
    ],
    "summary": [{"dnca_tot_amt": "1500000", "tot_evlu_amt": "8700000"}],
}

KIS_OVERSEAS_BALANCE = {
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
            "tr_crcy_cd": "JPY",
            "ovrs_excg_cd": "TSE",
            "market": "JP",
            "exchange": "TSE",
            "currency": "JPY",
        }
    ],
    "cash": [
        {"crcy_cd": "USD", "frcr_dncl_amt_2": "1500.50"},
        {"crcy_cd": "JPY", "frcr_dncl_amt_2": "20000"},
        {"crcy_cd": "", "frcr_dncl_amt_2": "0"},  # unusable row
    ],
}


# ── Parsing helpers ──────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [("1,234.56", 1234.56), ("0", 0.0), (12, 12.0), ("", None), (None, None), ("abc", None), (True, None)],
)
def test_to_float(raw, expected):
    assert pm.to_float(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("20260810", "2026-08-10"),
        ("2026-08-10", "2026-08-10"),
        ("2026-03-28T09:31:15+09:00", "2026-03-28"),
        ("", None),
        (None, None),
        ("garbage", None),
    ],
)
def test_iso_date(raw, expected):
    assert pm.iso_date(raw) == expected


# ── Toss ─────────────────────────────────────────────────


def test_toss_holdings_normalization():
    holdings = pm.toss_holdings(TOSS_HOLDINGS, "toss:primary")
    assert len(holdings) == 2
    samsung = next(h for h in holdings if h.symbol == "005930")
    assert samsung.quantity == 100
    assert samsung.currency == "KRW"
    assert samsung.market == "KR"
    assert samsung.avg_price == 65000
    assert samsung.cost_basis == 6_500_000
    assert samsung.market_value == 7_200_000
    assert samsung.unrealized_pnl == 700_000
    # Toss reports rate as a fraction; we expose percent.
    assert samsung.unrealized_pnl_pct == pytest.approx(10.77)
    apple = next(h for h in holdings if h.symbol == "AAPL")
    assert apple.currency == "USD"
    assert apple.market == "US"
    assert apple.account == "toss:primary"


def test_toss_holdings_drops_zero_quantity_and_handles_empty():
    payload = {"result": {"items": [{"symbol": "AAPL", "quantity": "0", "currency": "USD"}]}}
    assert pm.toss_holdings(payload, "toss:primary") == []
    assert pm.toss_holdings({"result": {"items": []}}, "toss:primary") == []
    assert pm.toss_holdings({}, "toss:primary") == []


def test_toss_order_to_buy_transaction():
    transactions = pm.toss_transactions([TOSS_ORDER_FILLED], "toss:primary")
    assert len(transactions) == 1
    tx = transactions[0]
    assert tx.tx_type is TxType.BUY
    assert tx.symbol == "005930"
    assert tx.trade_date == "2026-03-28"  # filledAt, not orderedAt
    assert tx.settle_date == "2026-03-30"
    assert tx.quantity == 10
    assert tx.gross_amount == 700_000
    assert tx.fee == 1400
    # A buy consumes cash: gross + fee + tax, negative.
    assert tx.net_amount == pytest.approx(-701_400)
    assert tx.quantity_delta == 10


def test_toss_sell_order_net_amount_is_proceeds_after_costs():
    order = {
        **TOSS_ORDER_FILLED,
        "orderId": "sell1",
        "side": "SELL",
        "execution": {
            **TOSS_ORDER_FILLED["execution"],
            "commission": "1400",
            "tax": "1050",
        },
    }
    tx = pm.toss_transactions([order], "toss:primary")[0]
    assert tx.tx_type is TxType.SELL
    assert tx.net_amount == pytest.approx(700_000 - 1400 - 1050)
    assert tx.quantity_delta == -10


def test_toss_transactions_skip_unfilled_and_keep_partial_fills():
    unfilled = {**TOSS_ORDER_FILLED, "orderId": "u", "status": "CANCELED",
                "execution": {**TOSS_ORDER_FILLED["execution"], "filledQuantity": "0"}}
    partial = {**TOSS_ORDER_FILLED, "orderId": "p", "status": "CANCELED",
               "execution": {**TOSS_ORDER_FILLED["execution"], "filledQuantity": "4", "filledAmount": "280000"}}
    transactions = pm.toss_transactions([unfilled, partial], "toss:primary")
    assert [tx.tx_id for tx in transactions] == ["p"]
    assert transactions[0].quantity == 4
    assert transactions[0].status == "CANCELED"


def test_toss_cash_normalization():
    payload = {"result": {"currency": "USD", "cashBuyingPower": "5000.25"}}
    balances = pm.toss_cash(payload, "toss:primary", "USD")
    assert balances[0].amount == 5000.25
    assert balances[0].currency == "USD"
    assert balances[0].kind == "buying_power"
    assert pm.toss_cash({"result": {}}, "toss:primary", "USD") == []


# ── KIS ──────────────────────────────────────────────────


def test_kis_domestic_holdings_normalization():
    holdings = pm.kis_domestic_holdings(KIS_DOMESTIC_BALANCE)
    assert len(holdings) == 1  # the 0-quantity row is dropped
    holding = holdings[0]
    assert holding.symbol == "005930"
    assert holding.currency == "KRW"
    assert holding.market == "KR"
    assert holding.cost_basis == 6_500_000
    assert holding.market_value == 7_200_000
    assert holding.account == "kis:domestic"


def test_kis_overseas_holdings_carry_market_and_currency():
    holdings = pm.kis_overseas_holdings(KIS_OVERSEAS_BALANCE)
    holding = holdings[0]
    assert holding.symbol == "7203"
    assert holding.name == "TOYOTA MOTOR"
    assert holding.market == "JP"
    assert holding.exchange == "TSE"
    assert holding.currency == "JPY"
    assert holding.cost_basis == 250_000
    assert holding.unrealized_pnl == 30_000
    assert holding.unrealized_pnl_pct == pytest.approx(12.0)


def test_kis_cash_normalization():
    domestic = pm.kis_domestic_cash(KIS_DOMESTIC_BALANCE)
    assert domestic[0].amount == 1_500_000
    assert domestic[0].currency == "KRW"
    overseas = pm.kis_overseas_cash(KIS_OVERSEAS_BALANCE)
    assert {b.currency: b.amount for b in overseas} == {"USD": 1500.50, "JPY": 20000.0}


def test_kis_domestic_transaction_sides_and_estimated_fee():
    payload = {
        "trades": [
            {
                "ord_dt": "20260810",
                "odno": "111",
                "sll_buy_dvsn_cd": "02",  # 매수
                "pdno": "005930",
                "prdt_name": "삼성전자",
                "tot_ccld_qty": "10",
                "avg_prvs": "70000",
                "tot_ccld_amt": "700000",
                "prsm_tlex_smtl": "1400",
                "excg_dvsn_cd": "KRX",
            },
            {
                "ord_dt": "20260811",
                "odno": "112",
                "sll_buy_dvsn_cd": "01",  # 매도
                "pdno": "005930",
                "tot_ccld_qty": "5",
                "tot_ccld_amt": "375000",
                "prsm_tlex_smtl": "900",
            },
            {"ord_dt": "20260812", "odno": "113", "sll_buy_dvsn_cd": "02", "tot_ccld_qty": "0"},
        ]
    }
    transactions = pm.kis_domestic_transactions(payload)
    assert [tx.tx_type for tx in transactions] == [TxType.BUY, TxType.SELL]
    buy, sell = transactions
    assert buy.trade_date == "2026-08-10"
    assert buy.currency == "KRW"
    assert buy.fx_rate == 1.0
    assert buy.fee == 1400 and buy.tax == 0.0
    assert "제비용" in (buy.note or "")
    assert buy.net_amount == pytest.approx(-701_400)
    # Price derived from amount / quantity when avg_prvs is absent.
    assert sell.price == pytest.approx(75_000)
    assert sell.net_amount == pytest.approx(375_000 - 900)


def test_kis_overseas_period_trans_normalization():
    payload = {
        "transactions": [
            {
                "trad_dt": "20260810",
                "sttl_dt": "20260812",
                "sll_buy_dvsn_cd": "02",
                "pdno": "AAPL",
                "ovrs_item_name": "APPLE INC",
                "ccld_qty": "10",
                "ovrs_stck_ccld_unpr": "150.5",
                "tr_frcr_amt2": "1505.0",
                "frcr_fee1": "1.5",
                "dmst_frcr_fee1": "0.5",
                "crcy_cd": "USD",
                "ovrs_excg_cd": "NASD",
                "erlm_exrt": "1350.20",
            }
        ]
    }
    tx = pm.kis_overseas_transactions(payload)[0]
    assert tx.tx_type is TxType.BUY
    assert tx.trade_date == "2026-08-10"
    assert tx.settle_date == "2026-08-12"
    assert tx.currency == "USD"
    assert tx.market == "US"
    assert tx.fee == pytest.approx(2.0)  # both fee columns summed
    assert tx.fx_rate == pytest.approx(1350.20)
    assert tx.net_amount == pytest.approx(-1507.0)
    assert tx.source == "kis:inquire-period-trans"


def test_kis_overseas_ccnl_fallback_flags_missing_fees():
    payload = {
        "trades": [
            {
                "ord_dt": "20260810",
                "odno": "77",
                "sll_buy_dvsn_cd": "01",
                "pdno": "7203",
                "ft_ccld_qty": "100",
                "ft_ccld_unpr3": "2800",
                "ft_ccld_amt3": "280000",
                "tr_crcy_cd": "JPY",
                "ovrs_excg_cd": "TKSE",
                "market": "JP",
                "currency": "JPY",
            }
        ]
    }
    tx = pm.kis_overseas_ccnl_transactions(payload)[0]
    assert tx.tx_type is TxType.SELL
    assert tx.fee == 0.0
    assert "fees not reported" in (tx.note or "")
    assert tx.net_amount == pytest.approx(280_000)
    assert tx.source == "kis:inquire-ccnl"


def test_kis_transactions_tolerate_empty_and_malformed_payloads():
    assert pm.kis_domestic_transactions({}) == []
    assert pm.kis_domestic_transactions({"trades": [None, "x", {}]}) == []
    assert pm.kis_overseas_transactions({"transactions": []}) == []
    assert pm.kis_overseas_holdings({"stocks": [{}]}) == []


# ── FX ───────────────────────────────────────────────────


def test_fx_rates_conversion_and_missing_tracking():
    fx = pm.FxRates(rates={"USD": 1300.0})
    assert fx.to_krw(10, "USD") == 13_000
    assert fx.to_krw(10, "KRW") == 10
    assert fx.to_krw(None, "USD") is None
    assert fx.to_krw(10, "JPY") is None
    assert "JPY" in fx.missing
    assert fx.to_dict()["missing_currencies"] == ["JPY"]


def test_market_for_infers_from_currency_then_symbol_shape():
    assert pm.market_for("KRW") == "KR"
    assert pm.market_for("JPY") == "JP"
    assert pm.market_for("", "005930") == "KR"
    assert pm.market_for("", "AAPL") == "US"


def test_transaction_to_dict_round_trip_keys():
    tx = pm.toss_transactions([TOSS_ORDER_FILLED], "toss:primary")[0]
    data = tx.to_dict()
    assert data["tx_type"] == "BUY"
    assert set(data) >= {
        "provider", "account", "tx_id", "tx_type", "trade_date", "settle_date",
        "symbol", "market", "currency", "quantity", "price", "gross_amount",
        "fee", "tax", "net_amount", "fx_rate", "status", "source",
    }
