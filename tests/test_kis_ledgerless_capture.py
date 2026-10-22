"""KIS ledgerless capture: one strict contract, published/hand-built examples, real client over a fake transport.

No network: every KISClient here talks to httpx.MockTransport. Published examples
are KIS portal copies (tests/fixtures/kis_published/<tr>.src); CTRP6504R, TTTS3039R
and TTTS3007R are hand-built from portal field lists and marked so.
"""
from __future__ import annotations

import copy
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import kis_nav_capture as cap
from kis_api import (
    KIS_LEDGERLESS_READS,
    KIS_RESPONSE_SPECS,
    KISAPIError,
    KISClient,
    KISResponseError,
    merge_kis_pages,
    validate_kis_response,
)

FIXTURES = Path(__file__).parent / "fixtures" / "kis_published"
PUBLISHED = ("TTTC8434R", "CTRP6548R", "FHKST01010400", "TTTC8708R", "TTTC0081R", "TTTC8715R", "CTOS4001R",
             "HHDFS76240000", "TTTS3035R")
HAND_BUILT = ("CTRP6504R", "TTTS3039R", "TTTS3007R")
CANO = "73519246"
NOW = datetime(2026, 10, 18, 9, 10, tzinfo=timezone.utc)  # Sunday 18:10 KST


def load(tr_id):
    return json.loads((FIXTURES / f"{tr_id}.json").read_text(encoding="utf-8"))


def reason(tr_id, body, tr_cont=""):
    with pytest.raises(KISResponseError) as exc:
        validate_kis_response(tr_id, body, tr_cont)
    return exc.value


def test_fixture_provenance_is_marked():
    for tr_id in PUBLISHED:
        assert "_provenance" not in load(tr_id)
        src = (FIXTURES / f"{tr_id}.src").read_text(encoding="utf-8")
        assert src.startswith("source: https://apiportal.koreainvestment.com/")
    for tr_id in HAND_BUILT:
        assert load(tr_id)["_provenance"] == "hand_built_from_field_list"
        assert "provenance: hand_built_from_field_list" in (FIXTURES / f"{tr_id}.src").read_text(encoding="utf-8")


@pytest.mark.parametrize("tr_id", PUBLISHED + ("CTRP6504R", "TTTS3039R"))
def test_examples_pass_the_contract_and_keep_only_listed_fields(tr_id):
    result = validate_kis_response(tr_id, load(tr_id))
    for name, (shape, fields) in KIS_RESPONSE_SPECS[tr_id]["outputs"].items():
        value = result["outputs"][name]
        for row in ([value] if shape == "one" else value):
            assert set(row) == set(fields)


def test_contract_drops_contact_and_ip_fields_from_fills():
    raw = load("TTTC0081R")
    assert {"ctac_tlno", "inqr_ip_addr"} <= set(raw["output1"][0])
    result = validate_kis_response("TTTC0081R", raw)
    assert result["outputs"]["output1"]
    assert all("ctac_tlno" not in row and "inqr_ip_addr" not in row for row in result["outputs"]["output1"])


def test_lone_object_and_padding_rows():
    # The published TTTS3035R example sends one all-empty object: padding, zero fills.
    result = validate_kis_response("TTTS3035R", load("TTTS3035R"))
    assert result["outputs"]["output"] == [] and result["padding_rows"] == 1
    # A lone object is a row only where the contract says so.
    body = load("TTTC0081R")
    body["output1"] = body["output1"][0]
    assert reason("TTTC0081R", body).reason == "KIS_OUTPUT_SHAPE_INVALID"


def test_capitalised_output_keys_and_ambiguity():
    body = load("TTTS3039R")
    assert validate_kis_response("TTTS3039R", body)["outputs"]["output2"]["ovrs_rlzt_pfls_tot_amt"] == "59.88"
    body["output1"] = body["Output1"]
    assert reason("TTTS3039R", body).reason == "KIS_OUTPUT_KEY_AMBIGUOUS"


def test_tr_outside_the_contract_is_refused_before_any_request():
    assert reason("TTTS3007R", load("TTTS3007R")).reason == "KIS_TR_NOT_IN_CONTRACT"
    client, calls = make_client(lambda *args: pytest.fail("no request expected"))
    with pytest.raises(KISResponseError):
        client.read_validated("TTTS3007R", {})
    assert calls == []
    assert set(KIS_LEDGERLESS_READS) == set(KIS_RESPONSE_SPECS)
    # KIS order/modify/cancel TRs end in "U"; the contract holds inquiries (…R) and quotations only.
    assert not any(tr_id.endswith("U") for tr_id in KIS_LEDGERLESS_READS)


def _set(tr_id, path, value):
    body = load(tr_id)
    target = body
    for key in path[:-1]:
        target = target[key]
    if value is KeyError:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return body


@pytest.mark.parametrize("tr_id,path,value,code", [
    ("TTTC8434R", ("output2", 0, "tot_evlu_amt"), KeyError, "KIS_FIELD_MISSING"),
    ("TTTC8434R", ("output2", 0, "tot_evlu_amt"), "", "KIS_FIELD_EMPTY"),
    ("TTTC8434R", ("output2", 0, "tot_evlu_amt"), "2,106,055", "KIS_FIELD_NOT_NUMERIC"),
    ("TTTC8434R", ("output2", 0, "tot_evlu_amt"), "NaN", "KIS_FIELD_NOT_NUMERIC"),
    ("TTTC8434R", ("output2", 0, "tot_evlu_amt"), "1e6", "KIS_FIELD_NOT_NUMERIC"),
    ("TTTC8434R", ("output2", 0, "tot_evlu_amt"), "-2106055", "KIS_FIELD_SIGN_INVALID"),
    ("TTTC8434R", ("output2", 0, "tot_evlu_amt"), 2106055, "KIS_FIELD_TYPE_INVALID"),
    ("TTTC8434R", ("output1", 0, "hldg_qty"), "-1657", "KIS_FIELD_SIGN_INVALID"),
    ("TTTC8434R", ("output2",), [], "KIS_OUTPUT_SHAPE_INVALID"),
    ("TTTC8434R", ("output2",), KeyError, "KIS_OUTPUT_MISSING"),
    ("TTTC8434R", ("rt_cd",), "1", "KIS_RESULT_NOT_SUCCESS"),
    ("FHKST01010400", ("output", 0, "stck_bsop_date"), "20221340", "KIS_FIELD_DATE_INVALID"),
    ("FHKST01010400", ("output", 0, "stck_bsop_date"), "19991231", "KIS_FIELD_DATE_INVALID"),
    ("TTTC0081R", ("output1", 0, "sll_buy_dvsn_cd"), "03", "KIS_FIELD_ENUM_INVALID"),
    ("CTOS4001R", ("output1", 0, "crcy_cd"), "US D", "KIS_FIELD_CODE_INVALID"),
    ("CTRP6504R", ("output3", "ustl_buy_amt_smtl"), "-1", "KIS_FIELD_SIGN_INVALID"),
    ("CTRP6504R", ("output2", 0, "frcr_dncl_amt_2"), "abc", "KIS_FIELD_NOT_NUMERIC"),
])
def test_every_defect_is_a_reason_without_values(tr_id, path, value, code):
    exc = reason(tr_id, _set(tr_id, path, value))
    assert exc.reason == code
    if isinstance(value, str) and value:
        assert value not in str(exc)


@pytest.mark.parametrize("tr_id,name", [(tr_id, name) for tr_id, spec in KIS_RESPONSE_SPECS.items()
                                        for name in spec["outputs"]])
def test_renamed_keys_are_missing_fields_never_padding(tr_id, name):
    # Rename drift: rows whose listed keys all vanished are not KIS's padding shape
    # (keys present, values blank) and must refuse the response, not become zero rows.
    body = load(tr_id)
    key = next(k for k in (name, name[:1].upper() + name[1:]) if k in body)
    fields = KIS_RESPONSE_SPECS[tr_id]["outputs"][name][1]
    rows = body[key] if isinstance(body[key], list) else [body[key]]
    assert rows
    for row in rows:
        for field in fields:
            if field in row:
                row[f"{field}_renamed"] = row.pop(field)
    assert reason(tr_id, body).reason == "KIS_FIELD_MISSING"


def test_padding_needs_every_required_key_present():
    body = load("TTTS3035R")
    assert validate_kis_response("TTTS3035R", body)["padding_rows"] == 1
    required = next(f for f, kind in KIS_RESPONSE_SPECS["TTTS3035R"]["outputs"]["output"][1].items()
                    if not kind.endswith("?"))
    del body["output"][required]
    assert reason("TTTS3035R", body).reason == "KIS_FIELD_MISSING"


def test_sign_check_allows_negative_where_kis_reports_losses():
    body = _set("TTTC8434R", ("output2", 0, "asst_icdc_amt"), "-36890")
    assert validate_kis_response("TTTC8434R", body)["outputs"]["output2"]["asst_icdc_amt"] == "-36890"
    assert validate_kis_response("TTTC8434R", _set("TTTC8434R", ("output2", 0, "tot_evlu_amt"), "-0"))


def test_paging_header_and_cursor_rules():
    body = load("TTTC8434R")
    body["ctx_area_fk100"] = body["ctx_area_nk100"] = "   "
    assert reason("TTTC8434R", body, "M").reason == "KIS_PAGING_CURSOR_MISSING"
    assert reason("CTRP6504R", load("CTRP6504R"), "F").reason == "KIS_PAGING_UNEXPECTED"
    assert reason("TTTC8434R", load("TTTC8434R"), "X").reason == "KIS_PAGING_HEADER_INVALID"
    page = validate_kis_response("TTTC8708R", _cursor(load("TTTC8708R")), "M")
    assert page["more"] is True
    with pytest.raises(KISResponseError, match="KIS_PAGING_TRUNCATED"):
        merge_kis_pages("TTTC8708R", [page])
    last = validate_kis_response("TTTC8708R", load("TTTC8708R"), "D")
    changed = copy.deepcopy(last)
    changed["outputs"]["output2"]["tot_rlzt_pfls"] = "1"
    with pytest.raises(KISResponseError, match="KIS_PAGING_INCONSISTENT"):
        merge_kis_pages("TTTC8708R", [page, changed])
    with pytest.raises(KISResponseError, match="KIS_RESPONSE_EMPTY"):
        merge_kis_pages("TTTC8708R", [])
    merged = merge_kis_pages("TTTC8708R", [page, last])
    assert merged["pages"] == 2 and len(merged["outputs"]["output1"]) == 2


def _cursor(body, n=1):
    body["ctx_area_fk100"] = f"synthetic-fk-{n}"
    body["ctx_area_nk100"] = f"synthetic-nk-{n}"
    return body


# ── Real client over a fake transport ─────────────────────────────────

def make_client(route):
    client = object.__new__(KISClient)
    client.app_key, client.app_secret = "synthetic-app-key", "synthetic-app-secret"
    client.url_base = "https://kis.test.invalid"
    client._token, client._token_expires = "synthetic-token", time.time() + 3600
    client._throttle = lambda: None
    client.cano, client.acnt_prdt_cd = CANO, "01"
    calls = []

    def handler(request):
        assert request.method == "GET"
        tr_id, params = request.headers["tr_id"], dict(request.url.params)
        calls.append((tr_id, params, request.headers.get("tr_cont", "")))
        status, body, cont = route(tr_id, params, len(calls))
        return httpx.Response(status, json=body, headers={"tr_cont": cont})

    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    return client, calls


def test_real_client_follows_validated_pages_and_echoes_cursors():
    def route(tr_id, params, n):
        return (200, _cursor(load("TTTC8708R")), "M") if n == 1 else (200, load("TTTC8708R"), "D")
    client, calls = make_client(route)
    result = client.ledgerless_domestic_daily_pnl("20261008", "20261018")
    assert result["pages"] == 2 and len(result["outputs"]["output1"]) == 2
    assert calls[1][2] == "N" and calls[1][1]["CTX_AREA_FK100"] == "synthetic-fk-1"
    assert calls[0][1]["INQR_STRT_DT"] == "20261008"


def test_real_client_rejects_an_invalid_page_and_truncation():
    def bad(tr_id, params, n):
        body = _cursor(load("TTTC8708R")) if n == 1 else _set("TTTC8708R", ("output1", 0, "rlzt_pfls"), "x")
        return 200, body, "M" if n == 1 else "D"
    client, _calls = make_client(bad)
    with pytest.raises(KISResponseError, match="KIS_FIELD_NOT_NUMERIC"):
        client.ledgerless_domestic_daily_pnl("20261008", "20261018")
    client, calls = make_client(lambda tr_id, params, n: (200, _cursor(load("TTTC8708R"), n), "M"))
    with pytest.raises(KISResponseError, match="KIS_PAGING_TRUNCATED"):
        client.read_validated("TTTC8708R", client._account_params(), max_pages=3)
    assert len(calls) == 3


# ── Capture ───────────────────────────────────────────────────────────

def bodies():
    domestic = load("TTTC8434R")
    domestic["output1"][0].update(prpr="128000", evlu_amt="1759600")
    domestic["output1"][1].update(prpr="128000", evlu_amt="384000")
    krx = load("FHKST01010400")
    krx["output"][0].update(stck_bsop_date="20261016", stck_clpr="128000")
    krx["output"][1].update(stck_bsop_date="20261015")
    return {"TTTC8434R": domestic, "FHKST01010400": krx}


def krw_basis(body):
    """CTRP6504R on WCRC_FRCR_DVSN_CD=01: rows in KRW (live: equal to foreign rows x bass_exrt / unit)."""
    body = copy.deepcopy(body)
    for row in body["output1"]:
        unit = 100 if row["buy_crcy_cd"] == "JPY" else 1
        krw = Decimal(row["frcr_evlu_amt2"]) * Decimal(row["bass_exrt"]) / unit
        row["frcr_evlu_amt2"] = format(krw.quantize(Decimal("0.000001")), "f")
    return body


def foreign_basis(body):
    """On 02, live output3 amounts are a raw sum across currencies."""
    body = copy.deepcopy(body)
    body["output3"]["evlu_amt_smtl"] = str(sum(Decimal(r["frcr_evlu_amt2"]) for r in body["output1"]))
    return body


def us_close(symbol_price, sessions=None):
    """sessions: EXCD -> (newest, previous) xymd; default 10/16 and 10/15."""
    def build(params):
        body = load("HHDFS76240000")
        price = symbol_price[params["SYMB"]]
        newest, previous = (sessions or {}).get(params["EXCD"], ("20261016", "20261015"))
        body["output2"][0].update(xymd=newest, clos=price)
        body["output2"][1].update(xymd=previous, clos="1.0000")
        return body
    return build


def routes(overrides=None, closes=None, sessions=None):
    fixed = bodies()
    closes = closes or {"AAPL": "230.0000", "7203": "3000.0000"}
    overrides = overrides or {}

    def route(tr_id, params, n):
        if tr_id in overrides:
            return overrides[tr_id](params) if callable(overrides[tr_id]) else overrides[tr_id]
        if tr_id == "HHDFS76240000":
            return 200, us_close(closes, sessions)(params), "F"  # live: older history signalled, first page read
        if tr_id == "CTRP6504R":
            body = overrides.get("CTRP6504R:body") or load("CTRP6504R")
            return 200, krw_basis(body) if params["WCRC_FRCR_DVSN_CD"] == "01" else foreign_basis(body), ""
        if tr_id == "FHKST01010400":  # market J (KRX) or NX (NXT)
            market = params["FID_COND_MRKT_DIV_CODE"]
            return 200, overrides.get(market, fixed["FHKST01010400"]), ""
        return 200, fixed.get(tr_id) or load(tr_id), ""
    return route


def run_capture(route, accounts=None, now=NOW, **kwargs):
    client, calls = make_client(route)
    accounts = accounts or [SimpleNamespace(label="primary", supports_overseas=True, kind="brokerage")]
    result = cap.capture_kis(accounts, lambda label: client, now=now, wall=lambda: now, **kwargs)
    return result["kis_capture"], calls


def nxt_capture(now, session, previous, sessions, nx_close="128000", nx_newer=None):
    """Capture at ``now`` with the largest holding priced on NXT (128,700; KRX close 128,000).

    NX's row for the session as KIS showed it live (Sun 10/18 01:29, Friday's row) before
    posting NXT's close: the close repeats the KRX close, open, range and volume are NXT's;
    older NX rows carry NXT's own close. ``nx_close`` sets that row's close instead.
    ``nx_newer`` lists a newer NX session first (NXT pre-market before KRX opens).
    """
    domestic = bodies()["TTTC8434R"]
    domestic["output1"][0]["prpr"] = "128700"
    krx = load("FHKST01010400")
    krx["output"][0].update(stck_bsop_date=session, stck_clpr="128000")
    krx["output"][1].update(stck_bsop_date=previous)
    nx = copy.deepcopy(krx)
    nx["output"][0].update(stck_oprc="127900", stck_hgpr="129300", stck_lwpr="127600", acml_vol="412000",
                           stck_clpr=nx_close)
    nx["output"][1].update(stck_clpr="124800", acml_vol="389000")
    if nx_newer:
        nx["output"].insert(0, dict(nx["output"][0], stck_bsop_date=nx_newer, stck_oprc="128600",
                                    stck_hgpr="128800", stck_lwpr="128500", stck_clpr="128700", acml_vol="21000"))
    return run_capture(routes({"TTTC8434R": (200, domestic, ""), "J": krx, "NX": nx}, sessions=sessions), now=now)


def test_capture_values_bases_and_price_time():
    capture, calls = run_capture(routes())
    account = capture["accounts"]["kis:primary"]
    assert account["status"] == "complete", account["reasons"]
    nav = {k: Decimal(v["value"]) for k, v in account["nav_native"].items()}
    assert nav == {"KRW": 2106055, "USD": 2800, "JPY": 310000}
    assert Decimal(account["cash_native"]["KRW"]["value"]) == 346455  # D+2 cash, not dnca_tot_amt
    assert Decimal(account["fx"]["JPY"]["rate_to_krw"]) == Decimal("9.5")  # per-100 quote
    assert Decimal(account["nav_krw"]["value"]) == 2106055 + 2800 * 1400 + 310000 * Decimal("9.5")
    assert account["nav_price_time"] == "2026-10-16T06:30:00+00:00"
    assert account["nav_price_time_latest"] == "2026-10-16T20:00:00+00:00"  # US 10/16 16:00 EDT
    # The US label names the row the price matched (here the newest, 10/16).
    assert account["nav_price_time_basis"] == "jp_regular_close+krx_regular_close+us_regular_close"
    check = account["overseas"]["cash_check"]
    assert check["decided"] == "frcr_dncl_amt_2" and check["decided_by"] == "no_unsettled_trades"
    assert check["gap_krw"] == {"frcr_dncl_amt_2": "0.00"} and account["advisories"] == []
    assert account["truth"]["domestic"]["bfdy_tot_asst_evlu_amt"] == "2142945"
    assert account["truth"]["overseas_realized"]["NASD"]["available_from"] == "T+1"
    assert account["trades"]["us_fills_on_capture_date"] == 0
    assert account["cross_check"]["CTRP6548R"]["tot_asst_amt"]
    assert {c[0] for c in calls} <= set(KIS_LEDGERLESS_READS)
    text = json.dumps(capture, ensure_ascii=False)
    assert CANO not in text and "synthetic-token" not in text and "ctac_tlno" not in text


def test_invalid_overseas_body_nulls_overseas_with_reason_never_zero():
    broken = load("CTRP6504R")
    broken["output2"][0]["frcr_dncl_amt_2"] = "-"
    capture, _ = run_capture(routes({"CTRP6504R:body": broken}))
    account = capture["accounts"]["kis:primary"]
    assert account["nav_native"]["USD"] == {"value": None, "status": "unknown",
                                            "reason_codes": ["KIS_OVERSEAS_BALANCE_UNAVAILABLE"]}
    assert account["nav_native"]["KRW"]["value"] == "2106055"
    assert account["nav_krw"]["value"] is None and account["status"] == "partial"
    assert capture["reads"]["kis:primary"]["CTRP6504R"]["reason_codes"] == ["KIS_FIELD_NOT_NUMERIC"]


def test_sign_flipped_domestic_total_nulls_krw_and_truth():
    flipped = bodies()["TTTC8434R"]
    flipped["output2"][0]["tot_evlu_amt"] = "-2106055"
    capture, _ = run_capture(routes({"TTTC8434R": (200, flipped, "")}))
    account = capture["accounts"]["kis:primary"]
    assert account["nav_native"]["KRW"]["value"] is None
    assert "KIS_DOMESTIC_BALANCE_UNAVAILABLE" in account["nav_native"]["KRW"]["reason_codes"]
    assert account["truth"]["domestic"] is None and account["nav_krw"]["value"] is None


def test_identity_failure_price_mismatch_and_late_us_fills_are_reasons():
    broken = bodies()["TTTC8434R"]
    broken["output2"][0]["prvs_rcdl_excc_amt"] = "346000"
    capture, _ = run_capture(routes({"TTTC8434R": (200, broken, "")}))
    assert "KIS_DOMESTIC_TOTAL_IDENTITY_FAILED" in capture["accounts"]["kis:primary"]["nav_native"]["KRW"]["reason_codes"]

    capture, _ = run_capture(routes(closes={"AAPL": "231.0000", "7203": "3000.0000"}))
    account = capture["accounts"]["kis:primary"]
    assert account["nav_price_time"] is None and "KIS_US_PRICE_NOT_A_RECENT_CLOSE" in account["reasons"]

    fills = load("TTTS3035R")
    fills["output"] = [dict(fills["output"], ord_dt="20261018", odno="1", ovrs_excg_cd="NASD", ft_ccld_qty="1")]
    capture, _ = run_capture(routes({"TTTS3035R": (200, fills, "")}))
    account = capture["accounts"]["kis:primary"]
    assert "KIS_US_FILLS_NEAR_CAPTURE" in account["reasons"] and account["nav_krw"]["value"] is None


def test_optional_cross_check_failure_and_pension_account():
    capture, calls = run_capture(routes({"CTRP6548R": (500, {"rt_cd": "1"}, "")}),
                                 accounts=[SimpleNamespace(label="pension", supports_overseas=False, kind="pension")])
    account = capture["accounts"]["kis:pension"]
    assert account["status"] == "complete" and account["cross_check"]["CTRP6548R"] is None
    assert capture["reads"]["kis:pension"]["CTRP6548R"]["reason_codes"] == ["KIS_SOURCE_UNAVAILABLE"]
    assert set(account["nav_native"]) == {"KRW"}
    assert not {"CTRP6504R", "CTOS4001R", "TTTS3035R", "TTTS3039R"} & {c[0] for c in calls}


def test_capture_never_raises_and_respects_budget():
    capture = cap.capture_kis(lambda: (_ for _ in ()).throw(RuntimeError("no config")), None,
                              now=NOW, wall=lambda: NOW)["kis_capture"]
    assert capture["reasons"] == ["KIS_NOT_CONFIGURED"] and capture["accounts"] == {}

    account = SimpleNamespace(label="primary", supports_overseas=True)
    capture = cap.capture_kis([account], lambda label: 1 / 0, now=NOW, wall=lambda: NOW)["kis_capture"]
    assert capture["accounts"]["kis:primary"]["reasons"] == ["KIS_CAPTURE_ACCOUNT_FAILED"]

    ticks = iter(range(0, 10_000, 50))
    capture, calls = run_capture(routes(), clock=lambda: next(ticks), budget_seconds=120)
    skipped = [k for k, v in capture["reads"]["kis:primary"].items() if v["status"] == "skipped"]
    assert skipped and all(capture["reads"]["kis:primary"][k]["reason_codes"] == ["KIS_CAPTURE_BUDGET_EXCEEDED"]
                           for k in skipped)
    assert len(calls) < len(KIS_LEDGERLESS_READS)


def test_the_budget_caps_the_read_in_flight():
    client, calls = make_client(routes())
    timeouts, default = [], client._http.timeout
    send = client._http.get
    client._http.get = lambda *args, **kwargs: timeouts.append(kwargs["timeout"]) or send(*args, **kwargs)
    reads = cap._Reads(100.0, clock=lambda: 98.0, now=lambda: NOW)
    assert reads("TTTC8434R", client.ledgerless_domestic_balance) is not None
    assert 0 < timeouts[0] <= 2.0  # the request timeout is the 2 s left, not 15 s
    assert client._request_deadline is None and client._http.timeout == default

    # A read started with (almost) no time left sends nothing and is a budget reason.
    clock = iter([100.0 - 1e-9, 100.0])
    reads = cap._Reads(100.0, clock=lambda: next(clock), now=lambda: NOW)
    assert reads("CTRP6548R", client.ledgerless_account_assets) is None
    assert reads.log["CTRP6548R"]["reason_codes"] == ["KIS_CAPTURE_BUDGET_EXCEEDED"]
    assert len(calls) == 1

    # A rate-limit back-off longer than the time left is not slept.
    limited = {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "rate"}
    client, calls = make_client(lambda tr_id, params, n: (500, limited, ""))
    started = time.monotonic()
    with client.time_limit(0.2), pytest.raises(KISAPIError) as exc:
        client.ledgerless_account_assets()
    assert exc.value.rate_limited and len(calls) == 1 and time.monotonic() - started < 0.2


def test_snapshot_persists_kis_capture_and_isolates_its_failure(monkeypatch, tmp_path):
    from datetime import date

    import portfolio_engine as pe
    import server
    from portfolio_model import FxRates
    from portfolio_sources import Collection
    from portfolio_store import NavStore
    from scripts import snapshot_nav

    store = NavStore(tmp_path / "nav.db")
    today = date.today().isoformat()
    collection = Collection(health={}, fetched_at=f"{today}T09:00:00Z")
    collector = SimpleNamespace(collect_positions=lambda: collection, build_fx=lambda currencies: FxRates())
    monkeypatch.setattr(server, "_get_collector", lambda: collector)
    monkeypatch.setattr(server, "_get_nav_store", lambda: store)
    monkeypatch.setattr(snapshot_nav, "apply_env_file", lambda *args: None)
    monkeypatch.setattr(snapshot_nav, "prepare_crypto_snapshot", lambda *args: None)
    monkeypatch.setattr(snapshot_nav, "collect_nav_evidence", lambda *args: None)
    monkeypatch.setattr(pe, "summarize_portfolio", lambda *args: {
        "total_assets_krw": 100, "result_status": "complete", "valuation_at": f"{today}T16:00:00+09:00",
        "observed_at": f"{today}T16:01:00+09:00", "components": [], "fx_times": {}})
    monkeypatch.setattr("sys.argv", ["snapshot_nav.py"])

    monkeypatch.setattr(cap, "capture_kis", lambda *args, **kwargs: {"kis_capture": {
        "version": cap.CAPTURE_VERSION, "accounts": {"kis:primary": {"status": "complete"}}, "reasons": []}})
    assert snapshot_nav.main() == 0
    stored = store.get_snapshot(today)
    assert stored["kis_capture"]["accounts"]["kis:primary"]["status"] == "complete"

    # A tool-path write without kis_capture does not replace the scheduled representative.
    scheduled = store.get_snapshot(today)["snapshot_id"]
    tool = store.upsert_snapshot({"total_assets_krw": 101, "result_status": "complete", "components": [],
                                  "fx_times": {}}, today, keep_crypto_representative=True)
    assert tool["representative"] is False and store.get_snapshot(today)["snapshot_id"] == scheduled

    def explode(*args, **kwargs):
        raise RuntimeError("kis down")
    monkeypatch.setattr(cap, "capture_kis", explode)
    assert snapshot_nav.main() == 0
    latest = store.get_snapshot(today)
    assert latest["total_value_krw"] == 100
    assert latest["kis_capture"]["reasons"] == ["KIS_CAPTURE_FAILED"]


def test_overseas_krw_totals_come_from_the_krw_basis_read():
    capture, calls = run_capture(routes())
    bases = [c[1]["WCRC_FRCR_DVSN_CD"] for c in calls if c[0] == "CTRP6504R"]
    assert bases == ["02", "01"]
    account = capture["accounts"]["kis:primary"]
    assert account["overseas"]["totals"]["evlu_amt_smtl"] == "6070000"
    # Native cost per currency (frcr_pchs_amt): KIS converts it at today's rate in its KRW P&L.
    assert account["truth"]["overseas"] == {"evlu_pfls_amt_smtl": "710000",
                                            "cost_native": {"JPY": "280000.00000", "USD": "2000.00000"}}

    def krw_fails(params):
        return (500, {"rt_cd": "1"}, "") if params["WCRC_FRCR_DVSN_CD"] == "01" else \
            (200, foreign_basis(load("CTRP6504R")), "")
    capture, _ = run_capture(routes({"CTRP6504R": krw_fails}))
    account = capture["accounts"]["kis:primary"]
    assert account["nav_native"]["USD"]["reason_codes"] == ["KIS_OVERSEAS_KRW_TOTALS_UNAVAILABLE"]
    assert account["nav_krw"]["value"] is None and account["truth"]["overseas"] is None

    jpy_per_unit = load("CTRP6504R")
    jpy_per_unit["output1"][1]["bass_exrt"] = "9.50000000"  # if KIS ever quoted JPY per 1, the identity fails
    def mixed(params):
        body = krw_basis(load("CTRP6504R")) if params["WCRC_FRCR_DVSN_CD"] == "01" else foreign_basis(jpy_per_unit)
        return 200, body, ""
    capture, calls = run_capture(routes({"CTRP6504R": mixed}))
    assert "KIS_OVERSEAS_TOTAL_MISMATCH" in capture["accounts"]["kis:primary"]["nav_native"]["JPY"]["reason_codes"]
    assert [c[0] for c in calls].count("CTRP6504R") == 2 * cap.BASES_READ_ATTEMPTS

    # A quote moving between the two reads: the pair is read again and agrees.
    moved = krw_basis(load("CTRP6504R"))
    moved["output3"]["evlu_amt_smtl"] = "6080000"
    pairs = iter([moved])
    def moving(params):
        if params["WCRC_FRCR_DVSN_CD"] == "01":
            return 200, next(pairs, None) or krw_basis(load("CTRP6504R")), ""
        return 200, foreign_basis(load("CTRP6504R")), ""
    capture, calls = run_capture(routes({"CTRP6504R": moving}))
    account = capture["accounts"]["kis:primary"]
    assert account["status"] == "complete", account["reasons"]
    assert [c[0] for c in calls].count("CTRP6504R") == 4 and account["overseas"]["totals"]["evlu_amt_smtl"] == "6070000"


def test_nxt_priced_holdings_need_the_nxt_session_close_and_its_time():
    nxt = bodies()["TTTC8434R"]
    nxt["output1"][0]["prpr"] = "128700"  # NXT after-market price, not the KRX close
    # NX's row for the session (here a copy of the KRX row: close 128,000, high 128,500) shows
    # neither that price nor a range holding it: unknown.
    capture, calls = run_capture(routes({"TTTC8434R": (200, nxt, "")}))
    account = capture["accounts"]["kis:primary"]
    assert account["price_times"][0]["reason_codes"] == ["KIS_NXT_PRICE_NOT_IDENTIFIED"]
    assert account["nav_price_time"] is None and account["nav_native"]["KRW"]["value"] == "2106055"
    assert [c[1]["FID_COND_MRKT_DIV_CODE"] for c in calls if c[0] == "FHKST01010400"] == ["J", "NX", "J"]

    # Sunday read of Friday's session, NXT's close posted: stamped at the NXT close (20:00 KST).
    capture, _ = nxt_capture(NOW, "20261016", "20261015", None, nx_close="128700")
    account = capture["accounts"]["kis:primary"]
    price = account["price_times"][0]
    assert price["status"] == "derived" and price["nxt_priced"] == 1 and price["nxt_in_range"] == 0
    assert price["basis"] == "krx_regular_close+nxt_after_market_close"
    assert price["price_time"] == "2026-10-16T06:30:00+00:00"
    assert price["latest_price_time"] == "2026-10-16T11:00:00+00:00"
    assert account["nav_price_time_basis"] == ("jp_regular_close+krx_regular_close+nxt_after_market_close"
                                               "+us_regular_close")
    # Posted but another price: not NXT's close, unknown.
    capture, _ = nxt_capture(NOW, "20261016", "20261015", None, nx_close="128600")
    assert capture["accounts"]["kis:primary"]["price_times"][0]["reason_codes"] == ["KIS_NXT_PRICE_NOT_IDENTIFIED"]

    # Not yet posted (the live 10/18 shape): the price lies in NXT's range, still the 20:00 close.
    capture, _ = nxt_capture(NOW, "20261016", "20261015", None)
    price = capture["accounts"]["kis:primary"]["price_times"][0]
    assert price["basis"] == "krx_regular_close+nxt_after_market_close_in_range" and price["nxt_in_range"] == 1
    assert price["latest_price_time"] == "2026-10-16T11:00:00+00:00"
    # After 20:00 a close one tick off the KRX close is NXT's own close, not the repeat.
    capture, _ = nxt_capture(NOW, "20261016", "20261015", None, nx_close="128100")
    assert capture["accounts"]["kis:primary"]["price_times"][0]["reason_codes"] == ["KIS_NXT_PRICE_NOT_IDENTIFIED"]

    no_nxt = load("FHKST01010400")
    no_nxt["output"] = []
    capture, _ = run_capture(routes({"TTTC8434R": (200, nxt, ""), "NX": no_nxt}))
    account = capture["accounts"]["kis:primary"]
    assert account["price_times"][0]["reason_codes"] == ["KIS_KRX_PRICE_NOT_LATEST_CLOSE"]
    assert account["nav_price_time"] is None and account["nav_native"]["KRW"]["value"] == "2106055"


def test_nxt_open_at_the_read_is_priced_at_the_read_within_one_tick():
    read = datetime(2026, 10, 16, 9, 10, tzinfo=timezone.utc)  # Fri 18:10 KST, NXT open to 20:00
    row = {"stck_bsop_date": "20261016", "stck_clpr": "128800", "stck_hgpr": "129300", "stck_lwpr": "127600",
           "acml_vol": "412000"}
    price, krx = Decimal("128700"), Decimal("128000")
    assert cap._nxt_priced(row, krx, price, "20261016", read) == (read, "nxt_after_market_at_read")
    assert cap._nxt_priced(dict(row, stck_clpr="128900"), krx, price, "20261016", read) is None  # 2 ticks
    # Close not posted (repeats the KRX close): inside NXT's range, at the read; outside or no volume, unknown.
    unposted = dict(row, stck_clpr="128000")
    assert cap._nxt_priced(unposted, krx, price, "20261016", read) == (read, "nxt_after_market_at_read_in_range")
    assert cap._nxt_priced(dict(unposted, stck_hgpr="128600"), krx, price, "20261016", read) is None
    assert cap._nxt_priced(dict(unposted, acml_vol="0"), krx, price, "20261016", read) is None
    assert cap._nxt_priced(dict(unposted, acml_vol=None), krx, price, "20261016", read) is None
    # The two daily reads a tick apart while NXT is open (live 10/19 19:15 KST): still the repeat.
    for jitter in ("128100", "127900"):
        assert cap._nxt_priced(dict(unposted, stck_clpr=jitter), krx, price, "20261016", read) == (
            read, "nxt_after_market_at_read_in_range")
    assert cap._nxt_priced(dict(unposted, stck_clpr="128200"), krx, price, "20261016", read) is None
    noon = datetime(2026, 10, 16, 3, 0, tzinfo=timezone.utc)  # 12:00 KST: NXT's main session
    assert cap._nxt_priced(row, krx, price, "20261016", noon) == (noon, "nxt_at_read")
    after = datetime(2026, 10, 16, 11, 5, tzinfo=timezone.utc)  # 20:05 KST: NXT ended, its close only
    assert cap._nxt_priced(row, krx, price, "20261016", after) is None
    assert cap._nxt_priced(dict(row, stck_clpr="128700"), krx, price, "20261016", after) == (
        datetime(2026, 10, 16, 20, 0, tzinfo=cap.KST), "nxt_after_market_close")
    # KRX still open at the read: priced at the read.
    assert cap._priced("20261016", cap.KRX_CLOSE, "krx_regular_close", noon) == (noon, "krx_regular_at_read")
    assert [cap._tick(Decimal(p)) for p in ("1999", "2000", "4995", "19990", "49950", "128700", "499500",
                                              "500000")] == [1, 5, 5, 10, 50, 100, 500, 1000]


def test_a_catch_up_run_before_krx_opens_never_puts_nxt_pre_market_in_the_older_range():
    # Tue 10/20 08:30 KST (timer Persistent=true catching up Monday's 18:10): KRX's newest session
    # is Monday, NX already lists Tuesday's pre-market row. 128,700 lies inside Monday's NX range,
    # but it may be Tuesday's pre-market trade: refused, never stamped Monday 20:00.
    catch_up = datetime(2026, 10, 19, 23, 30, tzinfo=timezone.utc)
    sessions = {"NAS": ("20261019", "20261016"), "TSE": ("20261019", "20261016")}
    capture, _ = nxt_capture(catch_up, "20261019", "20261016", sessions, nx_newer="20261020")
    account = capture["accounts"]["kis:primary"]
    assert account["price_times"][0]["reason_codes"] == ["KIS_NXT_SESSION_AHEAD_OF_KRX"]
    assert account["nav_price_time"] is None and account["nav_native"]["KRW"]["value"] == "2106055"
    # Without the newer NX row the same price is Monday's last NXT trade (the in-range rule).
    capture, _ = nxt_capture(catch_up, "20261019", "20261016", sessions)
    price = capture["accounts"]["kis:primary"]["price_times"][0]
    assert price["basis"] == "krx_regular_close+nxt_after_market_close_in_range"
    assert price["latest_price_time"] == "2026-10-19T11:00:00+00:00"
    # Monday's posted NXT close equal to the price still matches exactly (only the range is refused).
    capture, _ = nxt_capture(catch_up, "20261019", "20261016", sessions, nx_close="128700", nx_newer="20261020")
    assert capture["accounts"]["kis:primary"]["price_times"][0]["basis"] == (
        "krx_regular_close+nxt_after_market_close")


def pending(buy="100", sell="0", ustl_buy="140000", ustl_sll="0", krw_cash="795000"):
    """CTRP6504R with a USD trade unsettled; KIS's KRW FX cash (output3 frcr_evlu_tota) as given."""
    body = load("CTRP6504R")
    body["output2"][0].update(frcr_buy_amt_smtl=buy, frcr_sll_amt_smtl=sell)
    body["output3"].update(ustl_buy_amt_smtl=ustl_buy, ustl_sll_amt_smtl=ustl_sll, frcr_evlu_tota=krw_cash)
    return body


def test_unsettled_foreign_cash_counts_only_when_kis_krw_cash_confirms_a_reading():
    # USD 500 deposit, 100 bought unsettled: net 500 (795,000 KRW with JPY) vs the reference assumption 400 (655,000).
    capture, _ = run_capture(routes({"CTRP6504R:body": pending()}))
    account = capture["accounts"]["kis:primary"]
    check = account["overseas"]["cash_check"]
    assert check["unsettled_trades"] and check["decided"] == "frcr_dncl_amt_2"
    # Consistency with KIS's own figure is what the check shows, not which reading is right.
    assert check["decided_by"] == "consistent_with_kis_frcr_evlu_tota" and check["tolerance_krw"] == "82.50"
    assert check["gap_krw"] == {"frcr_dncl_amt_2": "0.00", "frcr_dncl_amt_2_plus_unsettled": "140000.00"}
    assert account["cash_basis"]["USD"] == "frcr_dncl_amt_2" and Decimal(account["nav_native"]["USD"]["value"]) == 2800

    capture, _ = run_capture(routes({"CTRP6504R:body": pending(krw_cash="655000")}))
    account = capture["accounts"]["kis:primary"]
    assert account["overseas"]["cash_check"]["decided"] == "frcr_dncl_amt_2_plus_unsettled"
    assert Decimal(account["nav_native"]["USD"]["value"]) == 2700 and account["status"] == "complete"

    # Neither reading within 1 bp (+1 KRW per currency): every foreign currency is null.
    capture, _ = run_capture(routes({"CTRP6504R:body": pending(krw_cash="700000")}))
    account = capture["accounts"]["kis:primary"]
    for currency in ("USD", "JPY"):
        assert account["nav_native"][currency]["reason_codes"] == ["KIS_FX_CASH_CROSS_CHECK_FAILED"]
    assert account["nav_krw"]["value"] is None and account["nav_native"]["KRW"]["value"] == "2106055"
    assert account["overseas"]["cash_check"]["reason"] == "KIS_FX_CASH_CROSS_CHECK_FAILED"

    # ustl_* disagreeing withdraws the reference reading; the net one is still checked.
    capture, _ = run_capture(routes({"CTRP6504R:body": pending(ustl_buy="1", krw_cash="655000")}))
    check = capture["accounts"]["kis:primary"]["overseas"]["cash_check"]
    assert set(check["gap_krw"]) == {"frcr_dncl_amt_2"} and check["reason"] == "KIS_FX_CASH_CROSS_CHECK_FAILED"

    # No KRW figure to check against: unverified, null.
    rows = {r["crcy_cd"]: r for r in pending()["output2"]}
    fx = {"USD": {"rate_to_krw": "1400"}, "JPY": {"rate_to_krw": "9.5"}}
    cash, check = cap._foreign_cash(rows, None, fx)
    assert cash is None and check["reason"] == "KIS_FX_CASH_UNSETTLED_UNVERIFIED"
    cash, check = cap._foreign_cash(rows, pending()["output3"], {"USD": fx["USD"]})
    assert cash is None and check["reason"] == "KIS_FX_CASH_UNSETTLED_UNVERIFIED"


def test_a_settled_day_cash_gap_is_an_advisory_shown_in_capture_status():
    # No unsettled trade, but KIS's KRW FX cash (frcr_evlu_tota) is 100 against 795,000 at the
    # marks: 794,900 KRW off, tolerance 3.01. The NAV stands; the gap is said, not hidden.
    import server
    body = load("CTRP6504R")
    body["output3"]["frcr_evlu_tota"] = "100"
    capture, _ = run_capture(routes({"CTRP6504R:body": body}))
    account = capture["accounts"]["kis:primary"]
    check = account["overseas"]["cash_check"]
    assert check["decided_by"] == "no_unsettled_trades" and check["gap_krw"] == {"frcr_dncl_amt_2": "794900.00"}
    assert check["tolerance_krw"] == "3.01" and check["advisories"] == ["KIS_FX_CASH_CROSS_CHECK_MISMATCH"]
    assert account["status"] == "complete" and account["reasons"] == []
    assert account["advisories"] == ["KIS_FX_CASH_CROSS_CHECK_MISMATCH"]
    assert Decimal(account["nav_krw"]["value"]) == 2106055 + 2800 * 1400 + 310000 * Decimal("9.5")
    status = server._kis_capture_status({"snapshot_date": "2026-10-18", "kis_capture": capture})
    assert status["accounts"]["kis:primary"]["status"] == "complete"
    assert status["accounts"]["kis:primary"]["reason_codes"] == ["KIS_FX_CASH_CROSS_CHECK_MISMATCH"]
    # Within tolerance: no advisory.
    capture, _ = run_capture(routes())
    status = server._kis_capture_status({"snapshot_date": "2026-10-18", "kis_capture": capture})
    assert status["accounts"]["kis:primary"]["reason_codes"] == []


def test_a_zero_cash_currency_without_a_rate_counts_as_zero_on_a_trade_day():
    # No JPY holding (so no JPY rate: KIS quotes JPY per 100, the posted rate is unused) and a
    # zero JPY cash row, with a USD buy unsettled: the USD cash is still checked and counted.
    body = pending(krw_cash="700000")
    body["output1"] = [r for r in body["output1"] if r["buy_crcy_cd"] != "JPY"]
    body["output2"][1]["frcr_dncl_amt_2"] = "0.000000"
    body["output3"]["evlu_amt_smtl"] = "3220000"
    capture, _ = run_capture(routes({"CTRP6504R:body": body}))
    account = capture["accounts"]["kis:primary"]
    check = account["overseas"]["cash_check"]
    assert check["decided"] == "frcr_dncl_amt_2" and check["decided_by"] == "consistent_with_kis_frcr_evlu_tota"
    assert "JPY" not in account["fx"] and Decimal(account["nav_native"]["JPY"]["value"]) == 0
    assert account["status"] == "complete" and account["reasons"] == []
    assert Decimal(account["nav_native"]["USD"]["value"]) == 2800
    assert Decimal(account["nav_krw"]["value"]) == 2106055 + 2800 * 1400
    # A non-zero cash row without a rate still blocks the check (it cannot be valued).
    rows = {r["crcy_cd"]: r for r in pending()["output2"]}
    cash, check = cap._foreign_cash(rows, pending()["output3"], {"USD": {"rate_to_krw": "1400"}})
    assert cash is None and check["reason"] == "KIS_FX_CASH_UNSETTLED_UNVERIFIED"
    rows["JPY"] = dict(rows["JPY"], frcr_dncl_amt_2="0")
    cash, check = cap._foreign_cash(rows, dict(pending()["output3"], frcr_evlu_tota="700000"),
                                    {"USD": {"rate_to_krw": "1400"}})
    assert cash == {"USD": Decimal("500.000000"), "JPY": Decimal("0")}


def test_quotation_first_page_needs_no_cursor_but_account_reads_do():
    body = load("HHDFS76240000")
    assert validate_kis_response("HHDFS76240000", body, "F")["more"] is False
    assert reason("TTTC8708R", load("TTTC8708R") | {"ctx_area_fk100": ""}, "F").reason == "KIS_PAGING_CURSOR_MISSING"


def test_us_session_in_progress_prices_at_the_balance_read():
    """Live Mon 10/19 18:10 KST: KIS lists the US 10/19 session (not yet closed) with its live quote."""
    monday = datetime(2026, 10, 19, 9, 10, tzinfo=timezone.utc)
    sessions = {"NAS": ("20261019", "20261016")}

    def us(newest_clos, previous_clos):
        def build(params):
            body = us_close({"AAPL": newest_clos, "7203": "3000.0000"}, sessions)(params)
            if params["EXCD"] == "NAS":
                body["output2"][1]["clos"] = previous_clos
            return 200, body, "F"
        return build

    def us_price(capture):
        account = capture["accounts"]["kis:primary"]
        return account, next(p for p in account["price_times"] if p["market"] == "US")

    for newest in ("230.0000", "231.5000"):  # the live row's quote, or a quote that moved after it
        capture, _ = run_capture(routes({"HHDFS76240000": us(newest, "228.0000")}), now=monday)
        account, price = us_price(capture)
        read_at = capture["reads"]["kis:primary"]["CTRP6504R"]["read_at"]
        assert account["status"] == "complete", account["reasons"]
        assert price["basis"] == "us_extended_session_at_read" and price["session_date"] == "20261019"
        assert price["price_time"] == read_at and account["nav_price_time_latest"] == read_at

    # No trade since Friday: the price is Friday's close, stamped at Friday 16:00 EDT; the
    # group's other holdings may carry live quotes, so its latest price time is the read.
    capture, _ = run_capture(routes({"HHDFS76240000": us("231.0000", "230.0000")}), now=monday)
    account, price = us_price(capture)
    read_at = capture["reads"]["kis:primary"]["CTRP6504R"]["read_at"]
    assert account["status"] == "complete", account["reasons"]
    assert price["basis"] == "us_regular_close" and price["price_time"] == "2026-10-16T20:00:00+00:00"
    assert price["latest_price_time"] == read_at == account["nav_price_time_latest"]

    # A quote more than 1% off the live row, or a live row not dated today in New York: refused.
    for build in (us("460.0000", "228.0000"), us("232.4000", "228.0000")):
        capture, _ = run_capture(routes({"HHDFS76240000": build}), now=monday)
        account, price = us_price(capture)
        assert account["status"] == "partial" and price["reason_codes"] == ["KIS_US_PRICE_NOT_A_RECENT_CLOSE"]
    capture, _ = run_capture(routes(closes={"AAPL": "230.0000", "7203": "3000.0000"},
                                    sessions={"NAS": ("20261119", "20261016")}), now=monday)
    account, price = us_price(capture)
    assert account["status"] == "partial" and price["reason_codes"] == ["KIS_US_PRICE_NOT_A_RECENT_CLOSE"]

    # A market without extended trading keeps the reason.
    capture, _ = run_capture(routes(sessions={"TSE": ("20261020", "20261019")}), now=monday)
    account = capture["accounts"]["kis:primary"]
    assert account["status"] == "partial" and "KIS_SESSION_DATE_IN_FUTURE" in account["reasons"]
