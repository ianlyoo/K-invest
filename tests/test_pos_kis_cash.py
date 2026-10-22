"""A held KIS overseas currency without a CTRP6504R cash row is 0 only when KIS's own
KRW foreign-cash total proves it (synthetic numbers; the real KISClient path over a scripted
HTTP layer)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from daily_nav import account_nav  # noqa: E402
from kis_api import KISAPIError, KISClient  # noqa: E402
from portfolio_model import kis_overseas_cash, kis_overseas_holdings  # noqa: E402
from portfolio_sources import Collection  # noqa: E402

JPY_DEPOSIT, JPY_RATE_PER_100, USD_RATE = "50000", "910.0", "1400.5"
FOREIGN_TOTAL_KRW = "455000"  # 50000 JPY x 9.10


class FakeKIS(KISClient):
    def __init__(self, krw_totals: dict[str, str] | Exception, usd_row: bool = False):
        self.app_key, self.app_secret, self.cano, self.acnt_prdt_cd = "k", "s", "12345678", "01"
        self.url_base, self._token, self._token_expires, self._http = "https://example.invalid", "t", 9e18, None
        self.krw_totals, self.usd_row = krw_totals, usd_row
        self.calls: list[dict[str, Any]] = []

    def _cash_rows(self):
        rows = [{"crcy_cd": "JPY", "frcr_dncl_amt_2": JPY_DEPOSIT, "frcr_buy_amt_smtl": "0",
                 "frcr_sll_amt_smtl": "0", "frst_bltn_exrt": "9.1", "frcr_drwg_psbl_amt_1": JPY_DEPOSIT}]
        if self.usd_row:
            rows.append({"crcy_cd": "USD", "frcr_dncl_amt_2": "12.5", "frcr_buy_amt_smtl": "0",
                         "frcr_sll_amt_smtl": "0", "frst_bltn_exrt": USD_RATE})
        return rows

    def _request(self, path, params, tr_id="", tr_cont=""):
        self.calls.append({"tr_id": tr_id, "params": dict(params)})
        if tr_id == "TTTS3012R":
            rows = {"NASD": [{"ovrs_pdno": "SYNU", "ovrs_cblc_qty": "10", "ovrs_stck_evlu_amt": "200",
                              "now_pric2": "20", "tr_crcy_cd": "USD", "ovrs_excg_cd": "NASD"}],
                    "TKSE": [{"ovrs_pdno": "9999", "ovrs_cblc_qty": "100", "ovrs_stck_evlu_amt": "150000",
                              "now_pric2": "1500", "tr_crcy_cd": "JPY", "ovrs_excg_cd": "TKSE"}]}
            return {"rt_cd": "0", "output1": rows.get(params["OVRS_EXCG_CD"], []), "output2": {}}, "D"
        assert tr_id == "CTRP6504R"
        if params["WCRC_FRCR_DVSN_CD"] == "02":
            return {"rt_cd": "0", "output1": [], "output2": self._cash_rows(),
                    "output3": {"frcr_evlu_tota": "50000.00"}}, "D"  # basis 02: a raw mixed-currency sum
        if isinstance(self.krw_totals, Exception):
            raise self.krw_totals
        positions = [{"pdno": "SYNU", "buy_crcy_cd": "USD", "ovrs_excg_cd": "NASD", "ccld_qty_smtl1": "10",
                      "frcr_evlu_amt2": "280100", "ovrs_now_pric1": "20", "bass_exrt": USD_RATE},
                     {"pdno": "9999", "buy_crcy_cd": "JPY", "ovrs_excg_cd": "TKSE", "ccld_qty_smtl1": "100",
                      "frcr_evlu_amt2": "1365000", "ovrs_now_pric1": "1500", "bass_exrt": JPY_RATE_PER_100}]
        totals = {"evlu_amt_smtl": "1645100", "frcr_evlu_tota": FOREIGN_TOTAL_KRW, "ustl_sll_amt_smtl": "0",
                  "ustl_buy_amt_smtl": "0", "evlu_pfls_amt_smtl": "0", "tot_asst_amt": "2100100",
                  **self.krw_totals}
        return {"rt_cd": "0", "output1": positions, "output2": self._cash_rows(), "output3": totals}, "D"


def _cash(client):
    payload = client.get_overseas_balance(["NASD", "TKSE"])
    return payload, {b.currency: b for b in kis_overseas_cash(payload)}


def test_unlisted_held_currency_is_zero_when_kis_total_proves_it():
    client = FakeKIS({})
    payload, cash = _cash(client)
    krw_reads = [c for c in client.calls if c["params"].get("WCRC_FRCR_DVSN_CD") == "01"]
    assert len(krw_reads) == 1 and payload["cash_check_krw"]["basis"] == "01"
    usd = cash["USD"]
    assert (usd.amount, usd.settled_cash, usd.unsettled_receivables, usd.unsettled_payables) == (0.0, 0.0, 0.0, 0.0)
    assert usd.source_fields["settled_cash_derivation"] == "KIS_NO_CASH_ROW_PROVEN_ZERO"
    assert usd.withdrawable_cash is None
    assert cash["JPY"].settled_cash == 50000.0  # the listed row is unchanged
    # Through the shared account NAV: USD cash is a (derived) 0, no longer MISSING_CASH_BALANCE.
    collection = Collection(holdings=kis_overseas_holdings(payload, "kis:overseas"), cash=list(cash.values()),
                            health={"kis:overseas": {"status": "ok"}})
    usd_nav = account_nav(collection)["kis:overseas"]["cash"]["USD"]
    assert usd_nav["value"] == 0 and usd_nav["basis"] == "derived"
    assert usd_nav["source_fields"][0]["settled_cash_derivation"] == "KIS_NO_CASH_ROW_PROVEN_ZERO"


def test_a_deposit_outside_the_listed_rows_keeps_the_currency_unknown():
    # KIS's KRW total also holds 0.75 USD that no row lists: the rows do not reproduce it.
    _payload, cash = _cash(FakeKIS({"frcr_evlu_tota": "456050.375"}))
    assert "USD" not in cash
    collection = Collection(cash=list(cash.values()), health={"kis:overseas": {"status": "ok"}})
    collection.holdings = kis_overseas_holdings(_payload, "kis:overseas")
    usd_nav = account_nav(collection)["kis:overseas"]["cash"]["USD"]
    assert usd_nav["value"] is None and usd_nav["reason_codes"] == ["MISSING_CASH_BALANCE"]


def test_within_tolerance_rounding_is_accepted():
    _payload, cash = _cash(FakeKIS({"frcr_evlu_tota": "455047"}))  # 1 bp + 2 KRW = 47.5 KRW
    assert cash["USD"].settled_cash == 0.0


def test_unsettled_trades_or_a_failed_krw_read_fail_closed():
    for client in (FakeKIS({"ustl_buy_amt_smtl": "1000"}), FakeKIS(KISAPIError(500, {}, "down")),
                   FakeKIS({"frcr_evlu_tota": ""})):
        payload, cash = _cash(client)
        assert "USD" not in cash
    assert payload["cash_check_krw"] == {"basis": "01", "error": "KIS_FIELD_EMPTY"}


def test_no_extra_read_when_every_held_currency_has_a_row():
    client = FakeKIS({}, usd_row=True)
    payload, cash = _cash(client)
    assert payload["cash_check_krw"] is None
    assert not [c for c in client.calls if c["params"].get("WCRC_FRCR_DVSN_CD") == "01"]
    assert cash["USD"].settled_cash == 12.5 and "settled_cash_derivation" not in cash["USD"].source_fields
