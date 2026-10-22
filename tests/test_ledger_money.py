"""Synthetic regressions for ledger money contracts; no broker requests."""

from dataclasses import replace

import pytest

import portfolio_engine as pe
import portfolio_model as pm
from portfolio_model import FxRates, Holding, Transaction, TxType


def amzn(**overrides):
    row = dict(
        trad_dt="20260206",
        sttl_dt="20260210",
        sll_buy_dvsn_cd="02",
        pdno="AMZN",
        crcy_cd="USD",
        ccld_qty="60",
        tr_frcr_amt2="12135",
        ovrs_stck_ccld_unpr="296154.675",
        erlm_exrt="1464.3",
        frcr_fee1="0",
        dmst_frcr_fee1="0",
    )
    row.update(overrides)
    return pm.kis_overseas_transactions({"transactions": [row]})[0]


def entry(currency="USD", **kwargs):
    return Transaction(
        provider="kis",
        account="kis:overseas",
        tx_id="synthetic",
        tx_type=TxType.BUY,
        trade_date="2026-02-06",
        currency=currency,
        symbol="ACME",
        quantity=1,
        gross_amount=54880.3362,
        **kwargs,
    )


def test_amzn_native_base_price_and_pnl_paths():
    tx = amzn()
    row = tx.to_dict()
    assert row["price_native"] == {"amount": 202.25, "currency": "USD"}
    assert row["price_base"] == {"amount": 296154.675, "currency": "KRW"}
    assert tx.price == 202.25
    assert row["price_source_field"] == "tr_frcr_amt2/ccld_qty"
    assert row["normalizer_version"] == 2
    assert row["price_validation"]["status"] == "matched"
    fx = FxRates({"USD": 1400})
    assert (
        pe.compute_realized_pnl([tx], fx)["open_positions_from_ledger"]["AMZN:USD"][
            "cost_basis"
        ]
        == 12135
    )
    assert (
        pe.compute_realized_pnl([replace(tx, gross_amount=None)], fx)[
            "open_positions_from_ledger"
        ]["AMZN:USD"]["cost_basis"]
        == 12135
    )


def test_price_missing_fx_and_mismatch_preserve_gross():
    tx = amzn(erlm_exrt="", ft_ccld_unpr2="200")
    row = tx.to_dict()
    assert row["price_base"] == {"amount": None, "currency": "KRW"}
    assert row["price_validation"]["status"] == "mismatch"
    assert tx.price == 200
    assert tx.gross_amount == 12135
    assert (
        pe.compute_realized_pnl([tx], FxRates())["open_positions_from_ledger"][
            "AMZN:USD"
        ]["cost_basis"]
        == 12135
    )


def test_ambiguous_ctos_price_without_gross_or_fx_is_unknown():
    tx = amzn(tr_frcr_amt2="", erlm_exrt="")
    assert tx.price is None
    assert tx.gross_amount is None
    assert tx.to_dict()["price_validation"]["status"] == "unknown"


@pytest.mark.parametrize(
    "provider,fees,taxes",
    [
        ("kis", {"USD": 26477.96, "JPY": 114586}, {"USD": 0, "JPY": 0}),
        ("toss", {"USD": 1810.19, "KRW": 14241}, {"USD": 13.99, "KRW": 1628084}),
    ],
)
def test_mixed_costs_exact_currency_sums_and_krw_policy(provider, fees, taxes):
    rows = [
        replace(
            entry(c, fee=f, tax=taxes[c], fx_rate=1400 if c == "USD" else None),
            provider=provider,
            fee_status="reported",
            tax_status="reported",
        )
        for c, f in fees.items()
    ]
    fx = FxRates({"USD": 1300, "JPY": 9}, as_of="synthetic")
    result = pe.compute_realized_pnl(rows, fx)
    totals = result["totals"]
    assert totals["fees"] is None and totals["taxes"] is None
    assert totals["fees_by_currency"] == fees
    assert totals["taxes_by_currency"] == taxes
    assert totals["fees_krw"] == pytest.approx(
        sum(
            f * (1400 if c == "USD" else 9 if c == "JPY" else 1)
            for c, f in fees.items()
        )
    )
    assert totals["cost_fx"]["policy"] == "broker_trade_rate_else_spot"
    assert {r["source"] for r in totals["cost_fx"]["rates"]} >= {"broker_trade_rate"}
    summary = pe.summarize_portfolio([], [], fx, result)
    assert summary["fees_krw"] == totals["fees_krw"]
    assert summary["taxes_krw"] == totals["taxes_krw"]


def test_unreported_costs_income_and_missing_conversion_are_unknown():
    tx = pm.kis_overseas_ccnl_transactions(
        {
            "trades": [
                dict(
                    ord_dt="20260206",
                    sll_buy_dvsn_cd="02",
                    pdno="ACME",
                    ft_ccld_qty="1",
                    ft_ccld_unpr3="100",
                )
            ]
        }
    )[0]
    totals = pe.compute_realized_pnl([tx], FxRates({"USD": 1400}))["totals"]
    assert tx.to_dict()["fee_status"] == "unsupported"
    assert totals["fees_by_currency"] == {"USD": 0}
    assert totals["fees_krw"] is None
    assert totals["fees_status"] == "unsupported"
    assert totals["dividends"] is None and totals["interest"] is None
    assert totals["dividends_status"] == "unsupported"
    assert totals["interest_status"] == "unsupported"
    missing = pe.compute_realized_pnl(
        [entry("JPY", fee=1, fee_status="reported")], FxRates()
    )["totals"]
    assert missing["fees_krw"] is None
    assert missing["cost_fx"]["missing_currencies"] == ["JPY"]
    zero = pe.compute_realized_pnl(
        [entry(fee=0, fee_status="verified_zero")], FxRates()
    )["totals"]
    assert zero["fees_krw"] == 0
    assert zero["fees_status"] == "verified_zero"


def test_all_ledger_row_costs_including_standalone_cost_and_income():
    rows = [
        entry(fee=0.1, tax=0.01),
        replace(entry(fee=0.2, tax=0.02), tx_type=TxType.FEE, symbol=None),
        replace(
            entry(fee=0.3, tax=0.03), tx_type=TxType.INTEREST, symbol=None, net_amount=2
        ),
    ]
    totals = pe.compute_realized_pnl(rows, FxRates({"USD": 1400}))["totals"]
    assert totals["fees_by_currency"] == {"USD": 0.6}
    assert totals["taxes_by_currency"] == {"USD": 0.06}
    assert totals["interest"] == 2
    assert totals["interest_status"] == "reported"


def test_avgo_fee_basis_gap_and_cost_counted_once():
    tx = entry(fee=137.1738, fee_status="reported", tax_status="verified_zero")
    realized = pe.compute_realized_pnl([tx], FxRates({"USD": 1400}))
    holding = Holding(
        "kis",
        "kis:overseas",
        "ACME",
        "USD",
        1,
        cost_basis=54880.3362,
        market_value=56000,
    )
    position = pe.consolidate_holdings([holding], FxRates({"USD": 1400}), realized)[
        "positions"
    ][0]
    assert position["broker_reported_cost_basis"]["amount"] == 54880.3362
    assert position["ledger_cost_basis"]["amount"] == 55017.51
    assert position["ledger_cost_basis"]["includes_buy_fees"] is True
    assert position["cost_basis_difference"]["amount"] == pytest.approx(137.1738)
    assert position["cost_basis_difference"]["reason"] == "fee_basis"
    assert position["ledger_unrealized_pnl"] == pytest.approx(982.49)
    sold = replace(
        tx, tx_type=TxType.SELL, trade_date="2026-03-01", gross_amount=56000, fee=0
    )
    assert pe.compute_realized_pnl([tx, sold], FxRates())["totals"][
        "realized_pnl_by_currency"
    ]["USD"] == pytest.approx(982.49)


def test_cost_basis_unavailable_and_account_quantity_mismatch():
    h = Holding("kis", "kis:overseas", "ACME", "USD", 1, cost_basis=54880.3362)
    row = pe.consolidate_holdings([h], FxRates())["positions"][0]
    assert row["broker_reported_cost_basis"]["includes_buy_fees"] is None
    assert row["ledger_cost_basis"]["amount"] is None
    assert row["ledger_cost_basis"]["reason"] == "ledger_not_available"
    ledger = pe.compute_realized_pnl(
        [replace(entry(), account="toss:secondary")], FxRates()
    )
    row = pe.consolidate_holdings([h], FxRates(), ledger)["positions"][0]
    assert row["ledger_cost_basis"]["amount"] is None
    assert row["ledger_cost_basis"]["reason"] == "account_or_quantity_mismatch"


@pytest.mark.parametrize(
    "normalizer,payload,expected,source",
    [
        (
            pm.toss_transactions,
            [
                dict(
                    orderId="synthetic",
                    symbol="AMZN",
                    side="BUY",
                    currency="USD",
                    execution=dict(
                        filledQuantity="60",
                        averageFilledPrice="202.25",
                        filledAmount="12135",
                        filledAt="2026-02-06",
                        commission="0",
                        tax="0",
                    ),
                )
            ],
            202.25,
            "execution.averageFilledPrice",
        ),
        (
            pm.kis_domestic_transactions,
            {
                "trades": [
                    dict(
                        ord_dt="20260206",
                        sll_buy_dvsn_cd="02",
                        pdno="005930",
                        tot_ccld_qty="60",
                        avg_prvs="70000",
                        tot_ccld_amt="4200000",
                        prsm_tlex_smtl="0",
                    )
                ]
            },
            70000,
            "avg_prvs",
        ),
        (
            pm.kis_overseas_ccnl_transactions,
            {
                "trades": [
                    dict(
                        ord_dt="20260206",
                        sll_buy_dvsn_cd="02",
                        pdno="AMZN",
                        ft_ccld_qty="60",
                        ft_ccld_unpr3="202.25",
                        ft_ccld_amt3="12135",
                    )
                ]
            },
            202.25,
            "ft_ccld_unpr3",
        ),
    ],
)
def test_other_normalizers_keep_native_price_and_provenance(
    normalizer, payload, expected, source
):
    tx = normalizer(payload, "synthetic")[0]
    row = tx.to_dict()
    assert tx.price == expected
    assert row["price_native"]["amount"] == expected
    assert row["price_source_field"] == source
    assert row["price_validation"]["status"] == "matched"
    assert row["price_base"]["amount"] == (expected if tx.currency == "KRW" else None)


def test_cost_window_uses_all_rows_and_trade_fx_before_spot():
    before = replace(entry(fee=10, tax=5), trade_date="2025-12-01")
    within = entry(
        fee=20, tax=8, fx_rate=1464.3, fee_status="reported", tax_status="reported"
    )
    after = replace(within, trade_date="2026-04-01", fee=100)
    totals = pe.compute_realized_pnl(
        [before, within, after], FxRates({"USD": 1400}), "2026-02-01", "2026-02-28"
    )["totals"]
    assert totals["fees_by_currency"] == {"USD": 20}
    assert totals["taxes_by_currency"] == {"USD": 8}
    assert totals["fees_krw"] == 29286
    assert totals["cost_fx"]["rates"] == [
        dict(
            currency="USD", rate=1464.3, source="broker_trade_rate", as_of="2026-02-06"
        )
    ]


def test_multiple_accounts_use_account_specific_ledger_cost():
    first = entry(fee=137.1738, fee_status="reported", tax_status="verified_zero")
    second = replace(first, account="toss:secondary", fee=10)
    ledger = pe.compute_realized_pnl([first, second], FxRates())
    h = Holding("kis", "kis:overseas", "ACME", "USD", 1, cost_basis=54880.3362)
    row = pe.consolidate_holdings([h], FxRates(), ledger)["positions"][0]
    assert row["ledger_cost_basis"]["amount"] == 55017.51
    assert row["cost_basis_difference"]["reason"] == "fee_basis"


def test_fee_capitalization_prorates_remaining_cost_after_partial_sale():
    buy = replace(
        entry(fee=137.1738, fee_status="reported", tax_status="verified_zero"),
        quantity=2,
    )
    sell = replace(
        buy,
        tx_type=TxType.SELL,
        quantity=1,
        gross_amount=28000,
        fee=0,
        trade_date="2026-03-01",
    )
    ledger = pe.compute_realized_pnl([buy, sell], FxRates())
    h = Holding(
        "kis",
        "kis:overseas",
        "ACME",
        "USD",
        1,
        cost_basis=27440.1681,
        market_value=28000,
    )
    row = pe.consolidate_holdings([h], FxRates(), ledger)["positions"][0]
    assert row["cost_basis_difference"]["amount"] == pytest.approx(68.5869)
    assert row["cost_basis_difference"]["reason"] == "fee_basis"
    assert ledger["totals"]["realized_pnl_by_currency"]["USD"] + row[
        "ledger_unrealized_pnl"
    ] == pytest.approx(982.49)


def test_unknown_and_verified_zero_costs_are_distinct():
    missing = pm.toss_transactions(
        [
            dict(
                orderId="synthetic",
                symbol="AMZN",
                side="BUY",
                execution=dict(
                    filledQuantity="60",
                    averageFilledPrice="202.25",
                    filledAt="2026-02-06",
                ),
            )
        ],
        "synthetic",
    )[0]
    assert missing.to_dict()["fee_reported_amount"] is None
    assert missing.to_dict()["fee_status"] == "unknown"
    zero = replace(missing, fee_status="verified_zero")
    assert zero.to_dict()["fee_reported_amount"] == 0
    assert zero.to_dict()["fee_status"] == "verified_zero"


def test_tool_money_envelopes_and_methodology(monkeypatch):
    from types import SimpleNamespace

    import server

    tx = amzn()
    holding = Holding(
        "kis", "kis:overseas", "AMZN", "USD", 60, cost_basis=12135, market_value=13000
    )

    def collection(**kwargs):
        return SimpleNamespace(
            ok_sources=["synthetic"],
            failed_sources=[],
            truncated_sources=[],
            health={},
            source_metadata={},
            requested_window={}, observed_window={}, fetch_complete=True,
            historical_coverage="unknown", failed_chunks=[], resume_cursor=None, reconciliation={},
            fetched_at="synthetic",
            **kwargs,
        )

    positions = collection(holdings=[holding], cash=[])
    transactions = collection(transactions=[tx])
    collector = SimpleNamespace(
        collect_positions=lambda *args: positions,
        collect_transactions=lambda *args: transactions,
        build_fx=lambda *args: FxRates({"USD": 1400}),
    )
    monkeypatch.setattr(server, "_get_collector", lambda: collector)
    result = server.get_all_transactions(start_date="2026-02-01", end_date="2026-02-28")
    assert result["ok"] is True
    assert result["data"]["transactions"][0]["price_native"]["amount"] == 202.25
    result = server.get_realized_pnl(start_date="2026-02-01", end_date="2026-02-28")
    assert result["ok"] is True
    assert result["data"]["totals"]["fees"] is None
    assert result["data"]["totals"]["fees_krw"] == 0
    result = server.get_consolidated_holdings()
    assert result["ok"] is True
    assert (
        result["data"]["positions"][0]["ledger_cost_basis"]["reason"]
        == "ledger_not_available"
    )
    assert "broker-reported" in result["data"]["methodology"]["cost_basis"]
    result = server.get_portfolio_summary(snapshot=False)
    assert result["ok"] is True
    assert result["data"]["fees_krw"] == 0
    assert result["data"]["taxes_krw"] is None
    assert result["data"]["taxes_status"] == "unsupported"


@pytest.mark.parametrize("kind", [TxType.DIVIDEND, TxType.INTEREST])
def test_income_unknown_zero_and_missing_fx(kind):
    absent = replace(entry(), tx_type=kind, gross_amount=None)
    totals = pe.compute_realized_pnl([absent], FxRates())["totals"]
    field = "dividends" if kind is TxType.DIVIDEND else "interest"
    assert totals[field] is None
    assert totals[field + "_status"] == "unknown"
    zero = replace(absent, gross_amount=0)
    totals = pe.compute_realized_pnl([zero], FxRates())["totals"]
    assert totals[field] == 0
    assert totals[field + "_status"] == "verified_zero"
    if kind is TxType.DIVIDEND:
        assert totals["dividends_krw"] == 0
        totals = pe.compute_realized_pnl([replace(absent, gross_amount=10)], FxRates())[
            "totals"
        ]
        assert totals["dividends"] == 10
        assert totals["dividends_krw"] is None


def test_partial_fee_columns_are_not_a_verified_total():
    tx = amzn(frcr_fee1="1.5", dmst_frcr_fee1="")
    assert tx.fee == 1.5
    assert tx.to_dict()["fee_status"] == "unknown"
    assert tx.to_dict()["fee_reported_amount"] is None
    totals = pe.compute_realized_pnl([tx], FxRates({"USD": 1400}))["totals"]
    assert totals["fees_by_currency"] == {"USD": 1.5}
    assert totals["fees_krw"] is None
    assert totals["fees_status"] == "unknown"
