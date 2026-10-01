"""KIS account-API contract: TR IDs, required params, pagination, market coverage.

These pin the two production bugs this work fixed:
  - get_overseas_balance queried NATN_CD=840 only, dropping JP/HK/CN holdings.
  - trade history used retired TR IDs (TTTC8001R / TTTS8001R) with wrong param
    names, which KIS rejects as OPSQ0002 "없는 서비스 코드".
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import kis_api
from kis_api import KISAPIError, KISClient, _is_rate_limited, _split_by_recency


class FakeResponse:
    """Minimal httpx.Response stand-in for the request layer."""

    def __init__(self, payload: Any, status_code: int = 200, tr_cont: str = "D"):
        self._payload = payload
        self.status_code = status_code
        self.headers = {"tr_cont": tr_cont}
        self.text = str(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class HttpKIS(KISClient):
    """KISClient with only the httpx layer faked, so _request logic is exercised."""

    def __init__(self, responses):
        self.app_key = "k"
        self.app_secret = "s"
        self.cano = "12345678"
        self.acnt_prdt_cd = "01"
        self.url_base = "https://example.invalid"
        self._token = "token"
        self._token_expires = 9e18
        self._last_request_at = 0.0
        self.sent: list[dict[str, Any]] = []
        self._responses = list(responses)
        self._http = self

    def get(self, url, params=None, headers=None):  # httpx.Client.get stand-in
        self.sent.append({"url": url, "params": params, "headers": headers})
        return self._responses.pop(0) if self._responses else FakeResponse({"rt_cd": "0"})


class FakeKIS(KISClient):
    """KISClient with the HTTP layer replaced by a scripted responder."""

    def __init__(self, responder):
        self.app_key = "k"
        self.app_secret = "s"
        self.cano = "12345678"
        self.acnt_prdt_cd = "01"
        self.url_base = "https://example.invalid"
        self._token = "token"
        self._token_expires = 9e18
        self._http = None  # never used — _request is overridden
        self.calls: list[dict[str, Any]] = []
        self._responder = responder

    def _request(self, path, params, tr_id="", tr_cont=""):
        self.calls.append({"path": path, "params": dict(params), "tr_id": tr_id, "tr_cont": tr_cont})
        return self._responder(path, params, tr_id, tr_cont)


def _overseas_row(symbol: str, quantity: str, currency: str) -> dict[str, Any]:
    return {
        "ovrs_pdno": symbol,
        "ovrs_item_name": f"name-{symbol}",
        "ovrs_cblc_qty": quantity,
        "pchs_avg_pric": "100.0",
        "frcr_pchs_amt1": "1000.0",
        "ovrs_stck_evlu_amt": "1200.0",
        "now_pric2": "120.0",
        "frcr_evlu_pfls_amt": "200.0",
        "evlu_pfls_rt": "20.00",
        "tr_crcy_cd": currency,
        "ovrs_excg_cd": "NAS" if currency == "USD" else "TSE",
    }


# ── Overseas balance: every market ───────────────────────


def test_overseas_balance_queries_every_market_and_merges():
    """US, JP, HK, and both China exchanges must all be requested."""
    holdings = {
        "NASD": [_overseas_row("AAPL", "10", "USD")],
        "TKSE": [_overseas_row("7203", "100", "JPY")],
        "SEHK": [],
        "SHAA": [],
        "SZAA": [],
    }

    def responder(path, params, tr_id, tr_cont):
        if "inquire-balance" in path:
            assert tr_id == "TTTS3012R"
            return {"rt_cd": "0", "output1": holdings[params["OVRS_EXCG_CD"]], "output2": {}}, "D"
        assert tr_id == "CTRP6504R"
        assert params["NATN_CD"] == "000", "must query all countries, not just US (840)"
        return {
            "rt_cd": "0",
            "output2": [{"crcy_cd": "USD", "frcr_dncl_amt_2": "500.0"}],
            "output3": [{"tot_asst_amt": "10000000"}],
        }, "D"

    client = FakeKIS(responder)
    result = client.get_overseas_balance()

    queried = {call["params"]["OVRS_EXCG_CD"] for call in client.calls if "inquire-balance" in call["path"]}
    assert queried == {"NASD", "TKSE", "SEHK", "SHAA", "SZAA"}
    assert {row["ovrs_pdno"] for row in result["stocks"]} == {"AAPL", "7203"}
    assert result["by_market"]["JP"]["position_count"] == 1
    assert result["cash"] == [{"crcy_cd": "USD", "frcr_dncl_amt_2": "500.0"}]
    assert result["errors"] == {}


def test_overseas_balance_stamps_market_exchange_currency_on_each_row():
    def responder(path, params, tr_id, tr_cont):
        if "inquire-balance" in path:
            excg = params["OVRS_EXCG_CD"]
            rows = [_overseas_row("7203", "100", "JPY")] if excg == "TKSE" else []
            return {"rt_cd": "0", "output1": rows}, "D"
        return {"rt_cd": "0", "output2": [], "output3": []}, "D"

    result = FakeKIS(responder).get_overseas_balance()
    row = result["stocks"][0]
    assert row["market"] == "JP"
    assert row["exchange"] == "TSE"  # from the row's own ovrs_excg_cd
    assert row["currency"] == "JPY"
    assert row["query_exchange"] == "TKSE"


def test_overseas_balance_sends_matching_currency_per_exchange():
    def responder(path, params, tr_id, tr_cont):
        if "inquire-balance" in path:
            return {"rt_cd": "0", "output1": []}, "D"
        return {"rt_cd": "0", "output2": [], "output3": []}, "D"

    client = FakeKIS(responder)
    client.get_overseas_balance()
    pairs = {
        call["params"]["OVRS_EXCG_CD"]: call["params"]["TR_CRCY_CD"]
        for call in client.calls
        if "inquire-balance" in call["path"]
    }
    assert pairs == {"NASD": "USD", "TKSE": "JPY", "SEHK": "HKD", "SHAA": "CNY", "SZAA": "CNY"}


def test_overseas_balance_isolates_a_failing_market():
    """One market erroring must not lose the others."""

    def responder(path, params, tr_id, tr_cont):
        if "inquire-balance" in path:
            if params["OVRS_EXCG_CD"] == "SEHK":
                raise KISAPIError(200, {"msg_cd": "OPSQ0001"}, "KIS error: 조회 권한 없음")
            rows = [_overseas_row("AAPL", "10", "USD")] if params["OVRS_EXCG_CD"] == "NASD" else []
            return {"rt_cd": "0", "output1": rows}, "D"
        return {"rt_cd": "0", "output2": [], "output3": []}, "D"

    result = FakeKIS(responder).get_overseas_balance()
    assert len(result["stocks"]) == 1
    assert "SEHK" in result["errors"]
    assert "US" in result["by_market"]


def test_overseas_balance_reports_present_balance_failure_without_losing_holdings():
    def responder(path, params, tr_id, tr_cont):
        if "inquire-balance" in path:
            rows = [_overseas_row("AAPL", "10", "USD")] if params["OVRS_EXCG_CD"] == "NASD" else []
            return {"rt_cd": "0", "output1": rows}, "D"
        raise KISAPIError(500, {}, "KIS error: 일시적 오류")

    result = FakeKIS(responder).get_overseas_balance()
    assert len(result["stocks"]) == 1
    assert result["cash"] == []
    assert "present_balance" in result["errors"]


def test_overseas_balance_honors_explicit_market_subset():
    def responder(path, params, tr_id, tr_cont):
        if "inquire-balance" in path:
            return {"rt_cd": "0", "output1": []}, "D"
        return {"rt_cd": "0", "output2": [], "output3": []}, "D"

    client = FakeKIS(responder)
    client.get_overseas_balance(markets=["TKSE"])
    queried = [call["params"]["OVRS_EXCG_CD"] for call in client.calls if "inquire-balance" in call["path"]]
    assert queried == ["TKSE"]


# ── Pagination ───────────────────────────────────────────


def test_pagination_follows_tr_cont_and_echoes_context_keys():
    pages = [
        ({"rt_cd": "0", "output1": [{"n": 1}], "ctx_area_fk200": "FK1", "ctx_area_nk200": "NK1"}, "M"),
        ({"rt_cd": "0", "output1": [{"n": 2}], "ctx_area_fk200": "", "ctx_area_nk200": ""}, "D"),
    ]
    state = {"i": 0}

    def responder(path, params, tr_id, tr_cont):
        if "inquire-balance" not in path:
            return {"rt_cd": "0", "output2": [], "output3": []}, "D"
        if params["OVRS_EXCG_CD"] != "NASD":
            return {"rt_cd": "0", "output1": []}, "D"
        page = pages[min(state["i"], len(pages) - 1)]
        state["i"] += 1
        return page

    client = FakeKIS(responder)
    result = client.get_overseas_balance(markets=["NASD"])
    nasd_calls = [call for call in client.calls if call["params"].get("OVRS_EXCG_CD") == "NASD"]
    assert len(nasd_calls) == 2
    assert nasd_calls[0]["tr_cont"] == ""
    assert nasd_calls[1]["tr_cont"] == "N"
    assert nasd_calls[1]["params"]["CTX_AREA_FK200"] == "FK1"
    assert nasd_calls[1]["params"]["CTX_AREA_NK200"] == "NK1"
    assert len(result["stocks"]) == 2


def test_pagination_stops_at_max_pages_and_reports_truncation():
    """KIS still had pages left — callers must be able to see the result is short."""

    def responder(path, params, tr_id, tr_cont):
        return {"rt_cd": "0", "output1": [{"n": 1}], "ctx_area_fk200": "F", "ctx_area_nk200": "N"}, "M"

    client = FakeKIS(responder)
    bodies, truncated = client._paginate("/x", {}, "TR", max_pages=3)
    assert len(bodies) == 3
    assert truncated is True


def test_pagination_reports_not_truncated_when_kis_signals_the_last_page():
    def responder(path, params, tr_id, tr_cont):
        return {"rt_cd": "0", "output1": [{"n": 1}]}, "D"

    bodies, truncated = FakeKIS(responder)._paginate("/x", {}, "TR")
    assert len(bodies) == 1
    assert truncated is False


def test_trade_history_surfaces_truncation_to_callers():
    """A silently short ledger produces a wrong cost basis, so it must be flagged."""

    def responder(path, params, tr_id, tr_cont):
        body = {
            "rt_cd": "0",
            "output": [{"ord_dt": "20260810", "odno": "1", "sll_buy_dvsn_cd": "02", "ft_ccld_qty": "1"}],
            "ctx_area_fk200": "F",
            "ctx_area_nk200": "N",
        }
        return body, "M"

    result = FakeKIS(responder).get_overseas_trade_history("20260101", "20260818")
    assert result["truncated"] is True


def test_untruncated_history_reports_false():
    def responder(path, params, tr_id, tr_cont):
        return {"rt_cd": "0", "output": [], "output1": [], "output2": {}}, "D"

    client = FakeKIS(responder)
    assert client.get_overseas_trade_history("20260801", "20260818")["truncated"] is False
    assert client.get_domestic_trade_history("20260801", "20260818")["truncated"] is False
    assert client.get_overseas_period_trans("20260801", "20260818")["truncated"] is False


def test_overseas_balance_reports_which_markets_were_truncated():
    def responder(path, params, tr_id, tr_cont):
        if "inquire-balance" not in path:
            return {"rt_cd": "0", "output2": [], "output3": []}, "D"
        if params["OVRS_EXCG_CD"] == "NASD":
            return {"rt_cd": "0", "output1": [{"ovrs_pdno": "AAPL"}],
                    "ctx_area_fk200": "F", "ctx_area_nk200": "N"}, "M"
        return {"rt_cd": "0", "output1": []}, "D"

    result = FakeKIS(responder).get_overseas_balance()
    assert result["truncated"] is True
    assert result["truncated_markets"] == ["NASD"]


# ── Domestic trade history: TR IDs and params ────────────


def test_domestic_trade_history_uses_current_tr_id_and_required_params():
    """TTTC8001R is retired; TTTC0081R plus INQR_DVSN/INQR_DVSN_3 is the live contract."""

    def responder(path, params, tr_id, tr_cont):
        assert path.endswith("/inquire-daily-ccld")
        assert tr_id == "TTTC0081R"
        for required in ("INQR_DVSN", "INQR_DVSN_3", "EXCG_ID_DVSN_CD", "CCLD_DVSN", "SLL_BUY_DVSN_CD"):
            assert required in params, f"{required} missing — KIS rejects the call"
        assert "CHKG_CLSS_CODE" not in params, "retired param must not be sent"
        return {
            "rt_cd": "0",
            "output1": [{"ord_dt": "20260810", "odno": "1", "pdno": "005930", "tot_ccld_qty": "10"}],
            "output2": {"tot_ccld_amt": "700000"},
        }, "D"

    result = FakeKIS(responder).get_domestic_trade_history("20260801", "20260818")
    assert result["tr_ids"] == ["TTTC0081R"]
    assert len(result["trades"]) == 1
    assert result["summary"]["tot_ccld_amt"] == "700000"


def test_domestic_trade_history_falls_back_to_krx_when_all_exchanges_rejected():
    attempts: list[str] = []

    def responder(path, params, tr_id, tr_cont):
        attempts.append(params["EXCG_ID_DVSN_CD"])
        if params["EXCG_ID_DVSN_CD"] == "ALL":
            raise KISAPIError(200, {"msg_cd": "OPSQ0003"}, "KIS error: 거래소구분코드 오류")
        return {"rt_cd": "0", "output1": [], "output2": {}}, "D"

    result = FakeKIS(responder).get_domestic_trade_history("20260801", "20260818")
    assert attempts == ["ALL", "KRX"]
    assert result["trades"] == []


def test_domestic_trade_history_splits_across_the_three_month_boundary():
    seen: list[str] = []

    def responder(path, params, tr_id, tr_cont):
        seen.append(tr_id)
        row = {"ord_dt": params["INQR_STRT_DT"], "odno": tr_id, "tot_ccld_qty": "1"}
        return {"rt_cd": "0", "output1": [row], "output2": {}}, "D"

    result = FakeKIS(responder).get_domestic_trade_history("20250101", date.today().strftime("%Y%m%d"))
    assert set(seen) == {"CTSC9215R", "TTTC0081R"}
    assert len(result["trades"]) == 2


def test_domestic_trade_history_dedupes_overlapping_rows():
    def responder(path, params, tr_id, tr_cont):
        return {
            "rt_cd": "0",
            "output1": [{"ord_dt": "20260810", "odno": "SAME", "tot_ccld_qty": "1"}],
            "output2": {},
        }, "D"

    result = FakeKIS(responder).get_domestic_trade_history("20250101", date.today().strftime("%Y%m%d"))
    assert len(result["trades"]) == 1


def test_split_by_recency_boundaries():
    today = date(2026, 8, 18)
    assert _split_by_recency("20260801", "20260818", today) == [("TTTC0081R", "20260801", "20260818")]
    assert _split_by_recency("20240101", "20240301", today) == [("CTSC9215R", "20240101", "20240301")]
    segments = _split_by_recency("20260101", "20260818", today)
    assert [s[0] for s in segments] == ["CTSC9215R", "TTTC0081R"]
    # Segments must not overlap on the boundary date.
    assert segments[0][2] < segments[1][1]


# ── Overseas trade history: TR ID and params ─────────────


def test_overseas_trade_history_uses_ttts3035r_with_correct_param_names():
    """TTTS8001R with STRT_DT/EXCD_CD/TRN_DVSN was the OPSQ0002 source."""

    def responder(path, params, tr_id, tr_cont):
        assert path.endswith("/inquire-ccnl")
        assert tr_id == "TTTS3035R"
        assert params["ORD_STRT_DT"] == "20260801"
        assert params["ORD_END_DT"] == "20260818"
        assert params["OVRS_EXCG_CD"] == "%"  # 전 거래소
        assert params["PDNO"] == "%"  # 전종목
        assert params["SLL_BUY_DVSN"] == "00"
        assert params["CCLD_NCCS_DVSN"] == "01"
        assert params["SORT_SQN"] == "DS"
        for retired in ("STRT_DT", "END_DT", "EXCD_CD", "TRN_DVSN", "SORT_DVSN"):
            assert retired not in params, f"{retired} is not a TTTS3035R param"
        return {
            "rt_cd": "0",
            "output": [
                {
                    "ord_dt": "20260810",
                    "odno": "9",
                    "pdno": "7203",
                    "ft_ccld_qty": "100",
                    "tr_crcy_cd": "JPY",
                    "ovrs_excg_cd": "TKSE",
                }
            ],
        }, "D"

    result = FakeKIS(responder).get_overseas_trade_history("20260801", "20260818")
    assert result["tr_ids"] == ["TTTS3035R"]
    row = result["trades"][0]
    assert row["market"] == "JP"
    assert row["currency"] == "JPY"


def test_overseas_period_trans_uses_ctos4001r_and_ctx_100():
    def responder(path, params, tr_id, tr_cont):
        assert path.endswith("/inquire-period-trans")
        assert tr_id == "CTOS4001R"
        assert params["ERLM_STRT_DT"] == "20260101"
        assert "CTX_AREA_FK100" in params and "CTX_AREA_NK100" in params
        return {"rt_cd": "0", "output1": [{"trad_dt": "20260105"}], "output2": [{"frcr_fee_smtl": "1"}]}, "D"

    result = FakeKIS(responder).get_overseas_period_trans("20260101", "20260818")
    assert len(result["transactions"]) == 1
    assert len(result["summary"]) == 1


# ── Broker-computed realized P&L ─────────────────────────


def test_domestic_realized_pnl_uses_ttc8715r():
    def responder(path, params, tr_id, tr_cont):
        assert path.endswith("/inquire-period-trade-profit")
        assert tr_id == "TTTC8715R"
        assert params["CBLC_DVSN"] == "00"
        return {"rt_cd": "0", "output1": [{"pdno": "005930", "rlzt_pfls": "10000"}], "output2": {"tot_rlzt_pfls": "10000"}}, "D"

    result = FakeKIS(responder).get_domestic_realized_pnl("20260101", "20260818")
    assert result["trades"][0]["rlzt_pfls"] == "10000"
    assert result["summary"]["tot_rlzt_pfls"] == "10000"


def test_overseas_realized_pnl_queries_each_market_with_its_currency():
    def responder(path, params, tr_id, tr_cont):
        assert tr_id == "TTTS3039R"
        assert params["WCRC_FRCR_DVSN_CD"] == "01"
        return {"rt_cd": "0", "output1": [{"ovrs_pdno": "AAPL", "ovrs_rlzt_pfls_amt": "100"}], "output2": []}, "D"

    client = FakeKIS(responder)
    result = client.get_overseas_realized_pnl("20260101", "20260818")
    currencies = {call["params"]["CRCY_CD"] for call in client.calls}
    assert currencies == {"USD", "JPY", "HKD", "CNY"}
    assert len(result["trades"]) == 5  # one row per queried exchange
    assert {row["market"] for row in result["trades"]} == {"US", "JP", "HK", "CN"}


def test_overseas_realized_pnl_isolates_market_errors():
    def responder(path, params, tr_id, tr_cont):
        if params["OVRS_EXCG_CD"] == "SZAA":
            raise KISAPIError(200, {}, "KIS error: 조회 불가")
        return {"rt_cd": "0", "output1": [], "output2": []}, "D"

    result = FakeKIS(responder).get_overseas_realized_pnl("20260101", "20260818")
    assert "SZAA" in result["errors"]


# ── Request-level behavior ───────────────────────────────


def test_rows_concatenates_arrays_and_accepts_object_outputs():
    client = KISClient.__new__(KISClient)
    bodies = [{"output1": [{"a": 1}, {"a": 2}]}, {"output1": [{"a": 3}]}, {"output1": {}}]
    assert client._rows(bodies, "output1") == [{"a": 1}, {"a": 2}, {"a": 3}]
    assert client._rows([{"output2": {"x": 1}}], "output2") == [{"x": 1}]


def test_get_returns_body_only(monkeypatch):
    def responder(path, params, tr_id, tr_cont):
        return {"rt_cd": "0", "output": {"ok": True}}, "D"

    assert FakeKIS(responder)._get("/x", {}, "TR") == {"rt_cd": "0", "output": {"ok": True}}


def test_rate_limit_detection():
    """KIS returns HTTP 500 for a rate limit, so the body must be inspected."""
    limited = FakeResponse(
        {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다."}, status_code=500
    )
    assert _is_rate_limited(limited) is True
    assert _is_rate_limited(FakeResponse({"rt_cd": "0"})) is False
    # A genuine 500 with a non-JSON body must not be mistaken for a rate limit.
    assert _is_rate_limited(FakeResponse(ValueError("not json"), status_code=500)) is False


def test_rate_limited_request_is_retried_then_succeeds(monkeypatch):
    monkeypatch.setattr(kis_api, "MIN_REQUEST_INTERVAL", 0.0)
    slept: list[float] = []
    monkeypatch.setattr(kis_api.time, "sleep", lambda s: slept.append(s))

    client = HttpKIS([
        FakeResponse({"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수 초과"}, status_code=500),
        FakeResponse({"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수 초과"}, status_code=500),
        FakeResponse({"rt_cd": "0", "output1": [{"a": 1}]}),
    ])
    data, _ = client._request("/x", {}, "TR")

    assert data["rt_cd"] == "0"
    assert len(client.sent) == 3
    assert len(slept) == 2  # backed off between attempts


def test_persistent_rate_limit_raises_a_retryable_error(monkeypatch):
    monkeypatch.setattr(kis_api, "MIN_REQUEST_INTERVAL", 0.0)
    monkeypatch.setattr(kis_api.time, "sleep", lambda s: None)
    limited = {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수 초과"}
    client = HttpKIS([FakeResponse(limited, status_code=500) for _ in range(10)])

    with pytest.raises(KISAPIError) as excinfo:
        client._request("/x", {}, "TR")
    assert excinfo.value.rate_limited is True
    assert excinfo.value.status == 500


def test_non_rate_limit_error_is_not_retried(monkeypatch):
    monkeypatch.setattr(kis_api, "MIN_REQUEST_INTERVAL", 0.0)
    client = HttpKIS([
        FakeResponse({"rt_cd": "1", "msg_cd": "OPSQ0002", "msg1": "없는 서비스 코드"}, status_code=200)
    ])
    with pytest.raises(KISAPIError) as excinfo:
        client._request("/x", {}, "TR")
    assert excinfo.value.rate_limited is False
    assert len(client.sent) == 1  # no retry


def test_throttle_spaces_consecutive_requests(monkeypatch):
    monkeypatch.setattr(kis_api, "MIN_REQUEST_INTERVAL", 0.5)
    slept: list[float] = []
    monkeypatch.setattr(kis_api.time, "sleep", lambda s: slept.append(s))
    clock = {"t": 100.0}
    monkeypatch.setattr(kis_api.time, "monotonic", lambda: clock["t"])

    client = HttpKIS([FakeResponse({"rt_cd": "0"}) for _ in range(2)])
    client._request("/x", {}, "TR")
    client._request("/x", {}, "TR")

    # The second call arrives with no elapsed time, so it waits the full interval.
    assert slept == [pytest.approx(0.5)]


def test_401_triggers_one_token_refresh_and_retry(monkeypatch):
    monkeypatch.setattr(kis_api, "MIN_REQUEST_INTERVAL", 0.0)
    client = HttpKIS([FakeResponse({}, status_code=401), FakeResponse({"rt_cd": "0", "output": {}})])
    monkeypatch.setattr(client, "_invalidate_token", lambda: None)
    monkeypatch.setattr(client, "_ensure_token", lambda: "token")

    data, _ = client._request("/x", {}, "TR")
    assert data["rt_cd"] == "0"
    assert len(client.sent) == 2


@pytest.mark.parametrize(
    "start,end",
    [("20260818", "20260801")],
)
def test_reversed_range_is_passed_through_to_kis(start, end):
    """Range validation belongs to the tool layer; the client stays a thin mapper."""

    def responder(path, params, tr_id, tr_cont):
        assert params["INQR_STRT_DT"] == start
        return {"rt_cd": "0", "output1": [], "output2": {}}, "D"

    FakeKIS(responder).get_domestic_trade_history(start, end)


def test_http_429_honors_retry_after_without_long_blocking(monkeypatch):
    monkeypatch.setattr(kis_api, "MIN_REQUEST_INTERVAL", 0.0)
    slept = []
    monkeypatch.setattr(kis_api.time, "sleep", slept.append)
    response = FakeResponse({}, status_code=429)
    response.headers["Retry-After"] = "7"
    client = HttpKIS([response, FakeResponse({"rt_cd": "0"})])
    client._request("/fixture", {}, "fixture")
    assert slept == [7]
    response.headers["Retry-After"] = "120"
    client = HttpKIS([response])
    with pytest.raises(KISAPIError) as captured:
        client._request("/fixture", {}, "fixture")
    assert captured.value.retry_after_seconds == 120
    assert len(client.sent) == 1
