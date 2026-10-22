#!/usr/bin/env python3
"""Korea Investment Securities (KIS) Open API — read-only client for MCP server.

Authentication: OAuth 2.0 (POST /oauth2/tokenP) with token caching.
Endpoints: domestic/overseas balance, holdings, trade history, market quotes.
NO order creation, modification, or cancellation endpoints.

Credentials resolve in order: direct env vars (KIS_APP_KEY, KIS_APP_SECRET,
KIS_CANO, KIS_ACNT_PRDT_CD, KIS_URL_BASE), then a KIS_ENV_FILE env file
(prefixed or unprefixed keys, e.g. APP_KEY/APP_SECRET).
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from health_status import observe_provider_outcome
from kinvest_common import NotConfiguredError, load_env_file, retry_after_seconds
from kinvest_common import cache_dir as _default_cache_dir
from portfolio_sources import connection_guard, retry_delay

logger = logging.getLogger("k-invest.kis")

TOKEN_CACHE_PATH = Path(
    os.environ.get(
        "KIS_TOKEN_CACHE",
        str(_default_cache_dir() / "kis_token.json"),
    )
)

# 계좌상품코드 of pension accounts: 22 연금저축, 29 퇴직연금(IRP). Neither can hold
# overseas-listed securities, so their clients skip the overseas endpoints.
PENSION_PRODUCT_CODES = frozenset({"22", "29"})

# KIS enforces a per-second request cap per app key and answers a burst with
# EGW00201 ("초당 거래건수를 초과하였습니다"). Account queries fan out per market
# and per page, so requests are spaced client-side and a rate-limited response is
# retried with backoff rather than surfaced as a failure.
MIN_REQUEST_INTERVAL = float(os.environ.get("KIS_MIN_REQUEST_INTERVAL", "0.25"))
REQUEST_TIMEOUT_SECONDS = 15.0
RATE_LIMIT_RETRIES = max(0, min(2, int(os.environ.get("KIS_RATE_LIMIT_RETRIES", "2"))))
RATE_LIMIT_MSG_CD = "EGW00201"
# EGW00215: KIS's other per-second limit (an account-ledger read rate; live on TTTC8434R
# during a ChatGPT burst, 2.15.0 10/22). Same handling as EGW00201.
RATE_LIMIT_MSG_CODES = frozenset({RATE_LIMIT_MSG_CD, "EGW00215"})
# EGW00215 cap per request: at most 1 + RATE_LIMIT_RETRIES (<= 3) tries and
# RATE_LIMIT_MAX_WAIT_SECONDS of back-off in total, sequential in the calling thread (a longer
# wait is not taken). EGW00201 and HTTP 429 keep their existing 60 s retry budget.
RATE_LIMIT_MAX_WAIT_SECONDS = 3.0
CAPPED_RATE_LIMIT_CODES = frozenset({"EGW00215"})
TOKEN_ERROR_MSG_CODES = frozenset({"EGW00123", "EGW00121", "EGW00110"})

#: app key -> monotonic timestamp of its last request; the cap is per app key,
#: so every client sharing a key shares the pacing state.
_LAST_REQUEST_AT: dict[str, float] = {}

# Continuation page cap. Overseas 주문체결내역 returns ~20 rows per page, so a wide
# window needs many pages; every paginated result reports whether it was truncated.
HISTORY_MAX_PAGES = int(os.environ.get("KIS_HISTORY_MAX_PAGES", "60"))

# get_overseas_balance's KRW cash check (a second CTRP6504R) is reused this long
# while the balance read it was made for is unchanged (see _overseas_cash_check).
CASH_CHECK_REUSE_SECONDS = 60.0
_CASH_CHECK_LOCK = threading.Lock()

# KIS exchange code mapping
_EXCD_MAP = {
    "NASDAQ": "NAS",
    "NASD": "NAS",
    "NAS": "NAS",
    "NYSE": "NYS",
    "NYS": "NYS",
    "AMEX": "AMS",
    "AMS": "AMS",
    "TKSE": "TSE",
    "TSE": "TSE",
    "TYO": "TSE",
    "JP": "TSE",
}

# Overseas market groups: KIS exchange code → (market, country_code, currency).
# KIS scopes every overseas balance/history query by exchange + currency, so a
# single-market query silently omits the other markets — all five are queried and
# merged by get_overseas_balance().
_OVERSEAS_MARKETS: dict[str, tuple[str, str, str]] = {
    "NASD": ("US", "840", "USD"),  # 실전: 미국 전체 (NASDAQ/NYSE/AMEX)
    "TKSE": ("JP", "392", "JPY"),
    "SEHK": ("HK", "344", "HKD"),
    "SHAA": ("CN", "156", "CNY"),  # 상해
    "SZAA": ("CN", "156", "CNY"),  # 심천
}


def _split_by_recency(
    start_date: str, end_date: str, today: date | None = None
) -> list[tuple[str, str, str]]:
    """Split a YYYYMMDD range into (tr_id, start, end) segments for 국내 체결내역.

    KIS serves the last 3 months from TTTC0081R and older history from CTSC9215R;
    a range spanning the boundary needs one call against each.
    """
    ref = today or date.today()
    cutoff = (ref - timedelta(days=90)).strftime("%Y%m%d")
    if start_date >= cutoff:
        return [("TTTC0081R", start_date, end_date)]
    if end_date < cutoff:
        return [("CTSC9215R", start_date, end_date)]
    prev_day = (datetime.strptime(cutoff, "%Y%m%d").date() - timedelta(days=1)).strftime("%Y%m%d")
    return [
        ("CTSC9215R", start_date, prev_day),
        ("TTTC0081R", cutoff, end_date),
    ]


def _error_body(resp: Any) -> Any:
    """Parsed JSON error body when available, else truncated text.

    KIS reports failures in the body (rt_cd/msg_cd/msg1) even on HTTP 500, so the
    parsed dict is what callers need — a raw string would strip msg_cd and hide
    whether the failure is retryable.
    """
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — fall back to text for non-JSON responses
        return resp.text[:500]
    return body if isinstance(body, dict) else resp.text[:500]


def _is_rate_limited(resp: Any) -> bool:
    """True when KIS answered with its per-second rate-limit code.

    KIS returns HTTP 500 with rt_cd=1 / msg_cd=EGW00201 (or EGW00215) for this, so the
    status code alone cannot distinguish it from a real server error.
    """
    return _rate_limit_code(resp) is not None


def _rate_limit_code(resp: Any) -> str | None:
    """The rate-limit code of a response (HTTP 429 or EGW00201/EGW00215), else None."""
    body = _error_body(resp)
    code = str(body.get("msg_cd", "")).upper() if isinstance(body, dict) else ""
    if code in RATE_LIMIT_MSG_CODES:
        return code
    return "HTTP429" if resp.status_code == 429 else None


class KISAPIError(Exception):
    """Raised when the KIS API returns an error."""

    def __init__(self, status: int, body: Any, msg: str = "", retry_after: int | None = None):
        self.status = status
        self.body = body
        self.retry_after_seconds = retry_after
        super().__init__(msg or f"KIS API {status}: {body}")

    @property
    def rate_limited(self) -> bool:
        body = self.body if isinstance(self.body, dict) else {}
        return self.status == 429 or str(body.get("msg_cd", "")).upper() in RATE_LIMIT_MSG_CODES


# ── Strict response contract (ledgerless KIS capture) ─────────────────
#
# One validator for every KIS response the ledgerless capture consumes. Each
# TR lists the outputs and the fields read from them, with a kind:
#   num  signed decimal string        pos  decimal string >= 0
#   date YYYYMMDD (2000..2100)        code non-empty short string
#   side "01" sell / "02" buy         text any string (stored as given)
# A trailing "?" makes a field optional: absent or "" becomes None. Required
# fields that are missing, empty, non-numeric, sign-flipped or malformed reject
# the whole response with a reason code, so a caller stores null + reason and
# never a zero. Only the listed fields survive validation; everything else in a
# body (phone numbers, IP addresses, cursors) is dropped. "rows" outputs are
# arrays; "one" outputs are one summary object (KIS sends a dict or a
# one-element list); "rows|object" also takes a lone object as one row (the
# published TTTS3035R example sends one). KIS pads an empty result with a row that
# has every key and blank or zero values (published TTTS3035R; TTTS3012R sends them):
# such a row is dropped only when every required listed key is present, so a
# renamed or missing key refuses the response (KIS_FIELD_MISSING) instead of
# turning real rows into padding. Output keys match exactly,
# else by capitalised name (the portal documents Output1/Output2 for CTRP6548R
# and TTTS3039R); both spellings present is ambiguous. Quotation TRs marked
# "first_page" return newest rows first and signal older history with tr_cont F
# (live HHDFS76240000); only the first page is read, so no cursor is required.
# Sources: KIS portal field lists and published examples (tests/fixtures/kis_published/*.src).

class KISResponseError(ValueError):
    """A KIS body broke the strict contract. Carries codes and field names only."""

    def __init__(self, tr_id: str, reason: str, field: str = ""):
        self.tr_id = tr_id
        self.reason = reason
        self.field = field
        super().__init__(f"{tr_id}:{reason}" + (f":{field}" if field else ""))


_NUMBER = re.compile(r"-?\d{1,20}(?:\.\d{1,12})?")
_CODE = re.compile(r"[0-9A-Za-z_.\-%]{1,32}")
_CONTINUATION = {"": False, "D": False, "E": False, "F": True, "M": True}

KIS_RESPONSE_SPECS: dict[str, dict[str, Any]] = {
    "TTTC8434R": {"ctx": "100", "outputs": {
        "output1": ("rows", {"pdno": "code", "hldg_qty": "pos", "prpr": "pos", "evlu_amt": "pos"}),
        "output2": ("one", {"tot_evlu_amt": "pos", "scts_evlu_amt": "pos", "evlu_amt_smtl_amt": "pos",
                            "prvs_rcdl_excc_amt": "num", "dnca_tot_amt": "num", "nxdy_excc_amt": "num",
                            "bfdy_tot_asst_evlu_amt": "pos", "asst_icdc_amt": "num",
                            "evlu_pfls_smtl_amt": "num", "tot_loan_amt": "pos?"}),
    }},
    "CTRP6548R": {"ctx": None, "outputs": {
        "output2": ("one", {"tot_asst_amt": "pos", "nass_tot_amt": "num", "evlu_amt_smtl": "pos",
                            "frcr_evlu_tota": "pos", "tot_dncl_amt": "num", "ovrs_stck_evlu_amt1": "pos?"}),
    }},
    "FHKST01010400": {"ctx": None, "first_page": True, "outputs": {
        "output": ("rows", {"stck_bsop_date": "date", "stck_clpr": "pos", "stck_hgpr": "pos?",
                            "stck_lwpr": "pos?", "acml_vol": "pos?"}),
    }},
    "TTTC8708R": {"ctx": "100", "outputs": {
        "output1": ("rows", {"trad_dt": "date", "buy_amt": "pos", "sll_amt": "pos", "rlzt_pfls": "num",
                             "fee": "pos", "tl_tax": "pos", "loan_int": "pos?"}),
        "output2": ("one", {"tot_rlzt_pfls": "num", "tot_fee": "pos", "tot_tltx": "pos"}),
    }},
    "TTTC0081R": {"ctx": "100", "outputs": {
        "output1": ("rows", {"ord_dt": "date", "odno": "code", "sll_buy_dvsn_cd": "side", "pdno": "code",
                             "tot_ccld_qty": "pos", "avg_prvs": "pos", "tot_ccld_amt": "pos",
                             "excg_dvsn_cd": "text?", "ord_tmd": "text?", "cncl_yn": "text?"}),
    }},
    "TTTC8715R": {"ctx": "100", "outputs": {
        "output1": ("rows", {"trad_dt": "date", "pdno": "code", "buy_qty": "pos", "buy_amt": "pos",
                             "sll_qty": "pos", "sll_amt": "pos", "rlzt_pfls": "num", "fee": "pos",
                             "tl_tax": "pos"}),
    }},
    "CTRP6504R": {"ctx": None, "outputs": {
        "output1": ("rows", {"pdno": "code", "buy_crcy_cd": "code", "ovrs_excg_cd": "code",
                             "ccld_qty_smtl1": "pos", "frcr_evlu_amt2": "pos", "ovrs_now_pric1": "pos",
                             "bass_exrt": "pos", "cblc_qty13": "pos?", "frcr_pchs_amt": "pos?"}),
        "output2": ("rows", {"crcy_cd": "code", "frcr_dncl_amt_2": "num", "frcr_buy_amt_smtl": "pos",
                             "frcr_sll_amt_smtl": "pos", "frst_bltn_exrt": "pos",
                             "frcr_drwg_psbl_amt_1": "num?", "nxdy_frcr_drwg_psbl_amt": "num?"}),
        "output3": ("one", {"evlu_amt_smtl": "pos", "frcr_evlu_tota": "pos", "ustl_sll_amt_smtl": "pos",
                            "ustl_buy_amt_smtl": "pos", "evlu_pfls_amt_smtl": "num", "tot_asst_amt": "pos?"}),
    }},
    "HHDFS76240000": {"ctx": None, "first_page": True, "outputs": {
        "output1": ("one", {"rsym": "code"}),
        "output2": ("rows", {"xymd": "date", "clos": "pos"}),
    }},
    "CTOS4001R": {"ctx": "100", "outputs": {
        "output1": ("rows", {"trad_dt": "date", "sttl_dt": "date", "sll_buy_dvsn_cd": "side", "pdno": "code",
                             "ccld_qty": "pos", "ft_ccld_unpr2": "pos", "tr_frcr_amt2": "pos",
                             "frcr_excc_amt_1": "pos", "wcrc_excc_amt": "pos", "crcy_cd": "code",
                             "erlm_exrt": "pos"}),
    }},
    "TTTS3039R": {"ctx": "200", "outputs": {
        "output1": ("rows", {"trad_day": "date", "ovrs_pdno": "code", "slcl_qty": "pos",
                             "ovrs_rlzt_pfls_amt": "num", "exrt": "pos?"}),
        "output2": ("one", {"ovrs_rlzt_pfls_tot_amt": "num", "bass_dt": "date?"}),
    }},
    "TTTS3035R": {"ctx": "200", "outputs": {
        "output": ("rows|object", {"ord_dt": "date", "odno": "code", "ovrs_excg_cd": "code", "ft_ccld_qty": "pos",
                            "dmst_ord_dt": "date?", "ord_tmd": "text?"}),
    }},
}


def _field(tr_id: str, name: str, kind: str, value: Any) -> Any:
    optional = kind.endswith("?")
    kind = kind.rstrip("?")
    if value is None or value == "":
        if optional:
            return None
        raise KISResponseError(tr_id, "KIS_FIELD_MISSING" if value is None else "KIS_FIELD_EMPTY", name)
    if not isinstance(value, str):
        raise KISResponseError(tr_id, "KIS_FIELD_TYPE_INVALID", name)
    if kind in ("num", "pos"):
        if not _NUMBER.fullmatch(value):
            raise KISResponseError(tr_id, "KIS_FIELD_NOT_NUMERIC", name)
        if kind == "pos" and value.startswith("-") and value.strip("-0.") != "":
            raise KISResponseError(tr_id, "KIS_FIELD_SIGN_INVALID", name)
        return value
    if kind == "date":
        try:
            parsed = datetime.strptime(value, "%Y%m%d") if len(value) == 8 and value.isdigit() else None
        except ValueError:
            parsed = None
        if parsed is None or not 2000 <= parsed.year <= 2100:
            raise KISResponseError(tr_id, "KIS_FIELD_DATE_INVALID", name)
        return value
    if kind == "side":
        if value not in ("01", "02"):
            raise KISResponseError(tr_id, "KIS_FIELD_ENUM_INVALID", name)
        return value
    if kind == "code":
        if not _CODE.fullmatch(value):
            raise KISResponseError(tr_id, "KIS_FIELD_CODE_INVALID", name)
        return value
    return value[:64]


def _padding(row: dict[str, Any], fields: dict[str, str]) -> bool:
    """KIS's padding shape: every required listed key present, every listed value blank or zero."""
    if any(name not in row for name, kind in fields.items() if not kind.endswith("?")):
        return False
    values = [row.get(name) for name in fields]
    return all(v in (None, "") or (isinstance(v, str) and _NUMBER.fullmatch(v) and not v.strip("-0.")) for v in values)


def _output(tr_id: str, body: dict[str, Any], name: str) -> Any:
    capital = name[:1].upper() + name[1:]
    present = [key for key in (name, capital) if key in body]
    if len(present) > 1:
        raise KISResponseError(tr_id, "KIS_OUTPUT_KEY_AMBIGUOUS", name)
    if not present:
        raise KISResponseError(tr_id, "KIS_OUTPUT_MISSING", name)
    return body[present[0]]


def validate_kis_response(tr_id: str, body: Any, tr_cont: str = "") -> dict[str, Any]:
    """Validate one KIS page against KIS_RESPONSE_SPECS; return only the listed fields.

    Returns ``{"outputs": {name: rows | summary}, "more": bool, "padding_rows": n}``.
    Raises KISResponseError (codes and field names only, never values).
    """
    spec = KIS_RESPONSE_SPECS.get(tr_id)
    if spec is None:
        raise KISResponseError(tr_id, "KIS_TR_NOT_IN_CONTRACT")
    if not isinstance(body, dict):
        raise KISResponseError(tr_id, "KIS_BODY_NOT_OBJECT")
    if str(body.get("rt_cd", "")) != "0":
        raise KISResponseError(tr_id, "KIS_RESULT_NOT_SUCCESS", "rt_cd")
    if tr_cont not in _CONTINUATION:
        raise KISResponseError(tr_id, "KIS_PAGING_HEADER_INVALID", "tr_cont")
    more = _CONTINUATION[tr_cont] and not spec.get("first_page")
    if more:
        if spec["ctx"] is None:
            raise KISResponseError(tr_id, "KIS_PAGING_UNEXPECTED", "tr_cont")
        for key in (f"ctx_area_fk{spec['ctx']}", f"ctx_area_nk{spec['ctx']}"):
            cursor = body.get(key, body.get(key.upper()))
            if not isinstance(cursor, str) or not cursor.strip():
                raise KISResponseError(tr_id, "KIS_PAGING_CURSOR_MISSING", key)
    outputs: dict[str, Any] = {}
    padding = 0
    for name, (shape, fields) in spec["outputs"].items():
        value = _output(tr_id, body, name)
        if shape == "one":
            if isinstance(value, list):
                if len(value) != 1:
                    raise KISResponseError(tr_id, "KIS_OUTPUT_SHAPE_INVALID", name)
                value = value[0]
            if not isinstance(value, dict):
                raise KISResponseError(tr_id, "KIS_OUTPUT_SHAPE_INVALID", name)
            outputs[name] = {field: _field(tr_id, field, kind, value.get(field)) for field, kind in fields.items()}
            continue
        if shape == "rows|object" and isinstance(value, dict):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
            raise KISResponseError(tr_id, "KIS_OUTPUT_SHAPE_INVALID", name)
        rows = []
        for row in value:
            if _padding(row, fields):
                padding += 1
                continue
            rows.append({field: _field(tr_id, field, kind, row.get(field)) for field, kind in fields.items()})
        outputs[name] = rows
    return {"outputs": outputs, "more": more, "padding_rows": padding}


def merge_kis_pages(tr_id: str, pages: list[dict[str, Any]]) -> dict[str, Any]:
    """Join validated pages: rows concatenate, a summary must repeat identically."""
    if not pages:
        raise KISResponseError(tr_id, "KIS_RESPONSE_EMPTY")
    if pages[-1]["more"]:
        raise KISResponseError(tr_id, "KIS_PAGING_TRUNCATED")
    merged: dict[str, Any] = {}
    for name, (shape, _fields) in KIS_RESPONSE_SPECS[tr_id]["outputs"].items():
        if shape == "one":
            values = [page["outputs"][name] for page in pages]
            if any(value != values[0] for value in values):
                raise KISResponseError(tr_id, "KIS_PAGING_INCONSISTENT", name)
            merged[name] = values[0]
        else:
            merged[name] = [row for page in pages for row in page["outputs"][name]]
    return {"outputs": merged, "pages": len(pages), "padding_rows": sum(p["padding_rows"] for p in pages)}


# Read-only TRs of the ledgerless capture: TR -> path. Paging follows the spec.
KIS_LEDGERLESS_READS = {
    "TTTC8434R": "/uapi/domestic-stock/v1/trading/inquire-balance",
    "CTRP6548R": "/uapi/domestic-stock/v1/trading/inquire-account-balance",
    "FHKST01010400": "/uapi/domestic-stock/v1/quotations/inquire-daily-price",
    "TTTC8708R": "/uapi/domestic-stock/v1/trading/inquire-period-profit",
    "TTTC0081R": "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
    "TTTC8715R": "/uapi/domestic-stock/v1/trading/inquire-period-trade-profit",
    "CTRP6504R": "/uapi/overseas-stock/v1/trading/inquire-present-balance",
    "HHDFS76240000": "/uapi/overseas-price/v1/quotations/dailyprice",
    "CTOS4001R": "/uapi/overseas-stock/v1/trading/inquire-period-trans",
    "TTTS3039R": "/uapi/overseas-stock/v1/trading/inquire-period-profit",
    "TTTS3035R": "/uapi/overseas-stock/v1/trading/inquire-ccnl",
}


def _exchange_rejected(exc: KISAPIError) -> bool:
    """KIS refused EXCG_ID_DVSN_CD=ALL (accounts without NXT/SOR access); not auth/rate errors."""
    body = exc.body if isinstance(exc.body, dict) else {}
    message = str(body.get("msg1") or exc)
    return not (exc.rate_limited or exc.status != 200
                or str(body.get("msg_cd", "")).upper() in TOKEN_ERROR_MSG_CODES
                or not ("EXCG_ID_DVSN_CD" in message.upper() or "거래소" in message))


def resolve_credentials(env: dict[str, str] | None = None) -> dict[str, str]:
    """Resolve KIS credentials from the process env, then a KIS_ENV_FILE env file.

    Returns app_key/app_secret/cano/acnt_prdt_cd/url_base, each "" when unset
    (except url_base, which falls back to the live endpoint). Shared by KISClient
    and the account registry so both tiers resolve identically.
    """
    source_env = env if env is not None else os.environ
    file_env: dict[str, str] = {}
    env_file = source_env.get("KIS_ENV_FILE", "").strip()
    if env_file:
        file_env = load_env_file(Path(env_file).expanduser())

    def _cred(env_name: str, *file_names: str) -> str:
        # Direct process env: prefixed name only (bare APP_KEY in the shared
        # env file must not be mistaken for KIS credentials).
        value = source_env.get(env_name, "").strip()
        if value:
            return value
        # KIS_ENV_FILE tier: prefixed or unprefixed keys allowed.
        for name in (env_name, *file_names):
            value = (file_env.get(name) or "").strip()
            if value:
                return value
        return ""

    return {
        "app_key": _cred("KIS_APP_KEY", "APP_KEY"),
        "app_secret": _cred("KIS_APP_SECRET", "APP_SECRET"),
        "cano": _cred("KIS_CANO", "CANO"),
        "acnt_prdt_cd": _cred("KIS_ACNT_PRDT_CD", "ACNT_PRDT_CD"),
        "url_base": (
            _cred("KIS_URL_BASE", "URL_BASE") or "https://openapi.koreainvestment.com:9443"
        ).rstrip("/"),
    }


class KISClient:
    """Korea Investment Securities API client (read-only).

    Credentials resolve in order: direct env vars (KIS_APP_KEY, KIS_APP_SECRET,
    KIS_CANO, KIS_ACNT_PRDT_CD, KIS_URL_BASE), then a KIS_ENV_FILE env file
    (prefixed or unprefixed keys, e.g. APP_KEY/APP_SECRET). Every argument
    overrides the resolved value, which is how KISAccountRegistry binds one client
    per configured account.
    """

    def __init__(
        self,
        cano: str = "",
        acnt_prdt_cd: str = "",
        app_key: str = "",
        app_secret: str = "",
        token_cache_path: Path | None = None,
    ):
        resolved = resolve_credentials()

        self.app_key = app_key.strip() or resolved["app_key"]
        self.app_secret = app_secret.strip() or resolved["app_secret"]
        self.cano = cano.strip() or resolved["cano"]
        self.acnt_prdt_cd = acnt_prdt_cd.strip() or resolved["acnt_prdt_cd"]
        self.url_base = resolved["url_base"]
        self._token_cache_path = token_cache_path or TOKEN_CACHE_PATH

        if not self.app_key or not self.app_secret:
            raise NotConfiguredError(
                "kis",
                "KIS credentials not configured — set KIS_APP_KEY/KIS_APP_SECRET "
                "(or KIS_ENV_FILE pointing to an env file with APP_KEY/APP_SECRET)",
            )

        self._token: str | None = None
        self._token_expires: float = 0
        self._http = httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)

    # Monotonic deadline set by time_limit() (ledgerless capture); None: no limit.
    _request_deadline: float | None = None

    @contextmanager
    def time_limit(self, seconds: float):
        """Cap every request started inside (each page, retry and token call) by the time left.

        A request's timeout is min(REQUEST_TIMEOUT_SECONDS, time left) and a rate-limit
        back-off never sleeps past it, so a read started just before a caller's budget
        ends cannot overrun it by a full timeout; past the deadline no request is sent.
        """
        previous, timeout = self._request_deadline, self._http.timeout
        self._request_deadline = time.monotonic() + max(0.0, seconds)
        self._http.timeout = httpx.Timeout(max(0.001, min(REQUEST_TIMEOUT_SECONDS, seconds)))
        try:
            yield self
        finally:
            self._request_deadline, self._http.timeout = previous, timeout

    def _time_left(self) -> float | None:
        if self._request_deadline is None:
            return None
        left = self._request_deadline - time.monotonic()
        if left <= 0:
            raise KISAPIError(0, {}, "KIS request time limit reached")
        return left

    @property
    def supports_overseas(self) -> bool:
        """False for 연금저축/퇴직연금 accounts, which cannot hold overseas securities.

        Skipping the overseas fan-out for them saves six calls against the
        per-app-key rate limit and avoids reporting an empty market as a source.
        """
        return self.acnt_prdt_cd not in PENSION_PRODUCT_CODES

    # ── Rate limiting ─────────────────────────────────────

    def _throttle(self) -> None:
        """Space consecutive requests to stay under the KIS per-second cap.

        The cap is per app key, so the last-request timestamp is shared by every
        client bound to that key — otherwise two accounts fanning out would each
        pace themselves and together exceed the cap.
        """
        if MIN_REQUEST_INTERVAL <= 0:
            return
        elapsed = time.monotonic() - _LAST_REQUEST_AT.get(self.app_key, 0.0)
        if 0 <= elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)
        _LAST_REQUEST_AT[self.app_key] = time.monotonic()

    # ── Token Management ──────────────────────────────────

    def _token_scope_key(self) -> str:
        return hashlib.sha256(f"{self.app_key}::{self.app_secret}".encode()).hexdigest()

    @contextmanager
    def _token_cache_guard(self):
        """Use a shared lock when possible, without making it an auth dependency."""
        fd = None
        try:
            try:
                self._token_cache_path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(self._token_cache_path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
            except OSError:
                fd = os.open(str(self._token_cache_path) + ".lock", os.O_RDONLY)
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                fd = None
            if not getattr(self, "_token_cache_lock_warned", False):
                self._token_cache_lock_warned = True
                logger.warning("KIS token cache lock unavailable; continuing without cross-process lock")
        try:
            yield
        finally:
            if fd is not None:
                try:
                    os.close(fd)  # Closing the descriptor also releases flock.
                except OSError:
                    pass

    def _load_cached_token(self) -> bool:
        try:
            if self._token_cache_path.exists():
                data = json.loads(self._token_cache_path.read_text())
                if data.get("scope") != self._token_scope_key():
                    return False
                if data.get("access_token") == getattr(self, "_rejected_token", None):
                    return False
                if data.get("expires_at", 0) > time.time() + 60:
                    self._token = data["access_token"]
                    self._token_expires = data["expires_at"]
                    return True
        except Exception:
            logger.warning("KIS token cache read failed")
        return False

    def _save_cached_token(self, token: str, expires_in: int) -> None:
        try:
            self._token_cache_path.parent.mkdir(parents=True, exist_ok=True)
            expires_at = time.time() + expires_in
            self._token_cache_path.write_text(json.dumps({
                "scope": self._token_scope_key(),
                "access_token": token,
                "expires_at": expires_at,
            }))
            os.chmod(self._token_cache_path, 0o600)
            self._token_expires = expires_at
        except Exception:
            logger.warning("KIS token cache write failed")

    def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token

        with self._token_cache_guard():
            return self._ensure_token_unlocked()

    def _ensure_token_unlocked(self) -> str:
        """Load or create a token after acquiring the optional shared cache lock."""
        if self._load_cached_token():
            if self._token is None:
                raise KISAPIError(0, {}, "KIS token cache loaded without token")
            return self._token

        logger.info("Requesting new KIS OAuth token")
        resp = self._http.post(
            f"{self.url_base}/oauth2/tokenP",
            json={
                "grant_type": "client_credentials",
                "appkey": self.app_key,
                "appsecret": self.app_secret,
            },
            headers={"content-type": "application/json"},
        )
        if resp.status_code != 200:
            raise KISAPIError(resp.status_code, resp.text, "KIS token request failed")

        body = resp.json()
        token = body.get("access_token", "").strip()
        if not token:
            raise KISAPIError(0, body, "KIS token response missing access_token")
        expires_in = body.get("expires_in", 43200)
        self._token = token
        self._save_cached_token(token, expires_in)
        return token

    def _headers(self, tr_id: str = "") -> dict[str, str]:
        token = self._ensure_token()
        h = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }
        if tr_id:
            h["tr_id"] = tr_id
        return h

    def _invalidate_token(self) -> None:
        rejected_token = self._token
        self._rejected_token = rejected_token
        self._token = None
        self._token_expires = 0
        with self._token_cache_guard():
            try:
                cached = json.loads(self._token_cache_path.read_text())
            except (FileNotFoundError, ValueError):
                cached = None
            except OSError:
                # Without reading the cache we cannot establish that its token
                # was rejected; preserve it instead of deleting a peer's token.
                logger.warning("KIS token cache read failed")
                return
            if isinstance(cached, dict) and cached.get("access_token") and cached["access_token"] != rejected_token:
                # A peer refreshed this file. Only adopt a valid, matching-scope
                # token; never unlink another token, even if it cannot be used.
                self._load_cached_token()
                return
            try:
                self._token_cache_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _request(
        self, path: str, params: dict[str, Any], tr_id: str = "", tr_cont: str = ""
    ) -> tuple[dict[str, Any], str]:
        with connection_guard("kis", getattr(self, "app_key", id(self))):
            try:
                result = self._request_unlocked(path, params, tr_id, tr_cont)
            except Exception as exc:
                observe_provider_outcome("kis", exc)
                raise
            observe_provider_outcome("kis")
            return result

    def _request_unlocked(self, path, params, tr_id, tr_cont):
        """GET request with KIS auth. Returns (parsed JSON, response tr_cont header).

        The response `tr_cont` header drives KIS continuation: "F"/"M" mean more
        pages are available, "D"/"E" mean this is the last page.
        """
        auth_retried = False

        def _send() -> httpx.Response:
            nonlocal auth_retried
            headers = self._headers(tr_id)
            if tr_cont:
                headers["tr_cont"] = tr_cont
            self._throttle()
            left = self._time_left()
            limit = {} if left is None else {"timeout": min(REQUEST_TIMEOUT_SECONDS, left)}
            response = self._http.get(f"{self.url_base}{path}", params=params, headers=headers, **limit)
            body = _error_body(response)
            code = str(body.get("msg_cd", "")).upper() if isinstance(body, dict) else ""
            if response.status_code == 401 or code in TOKEN_ERROR_MSG_CODES:
                if auth_retried:
                    raise KISAPIError(response.status_code, body, "KIS authentication failed after renewal",
                                      retry_after=retry_after_seconds(response.headers.get("Retry-After")))
                auth_retried = True
                logger.warning("KIS auth rejection: msg_cd=%s HTTP=%d",
                               code if code in TOKEN_ERROR_MSG_CODES else "unavailable", response.status_code)
                self._invalidate_token()
                return _send()
            return response

        resp = _send()

        # A rate-limited burst is transient — back off and retry before failing.
        remaining = 60.0 if self._request_deadline is None else \
            max(0.0, min(60.0, self._request_deadline - time.monotonic()))
        waited = 0.0
        for attempt in range(RATE_LIMIT_RETRIES):
            code = _rate_limit_code(resp)
            if code is None:
                break
            requested_delay = retry_after_seconds(resp.headers.get("Retry-After"))
            budget = min(remaining, RATE_LIMIT_MAX_WAIT_SECONDS - waited) if code in CAPPED_RATE_LIMIT_CODES \
                else remaining
            delay = retry_delay(attempt, requested_delay, budget, base=max(.25, MIN_REQUEST_INTERVAL))
            if delay is None:
                break  # expose metadata rather than wait beyond the retry budget
            logger.warning(
                "KIS rate limit (%s) on %s, retrying in %.2fs (attempt %d/%d)",
                code, path, delay, attempt + 1, RATE_LIMIT_RETRIES,
            )
            time.sleep(delay)
            remaining -= delay
            waited += delay
            resp = _send()

        if resp.status_code != 200:
            raise KISAPIError(resp.status_code, _error_body(resp), "KIS API request failed",
                              retry_after=retry_after_seconds(resp.headers.get("Retry-After")))

        data = resp.json()
        rt_cd = str(data.get("rt_cd", ""))
        if rt_cd != "0":
            raise KISAPIError(resp.status_code, data, "KIS API request failed",
                retry_after=retry_after_seconds(resp.headers.get("Retry-After")))

        return data, str(resp.headers.get("tr_cont", "")).strip()

    def _get(self, path: str, params: dict[str, Any], tr_id: str = "") -> dict[str, Any]:
        """GET request with KIS authentication. Returns parsed JSON (first page only)."""
        data, _ = self._request(path, params, tr_id)
        return data

    def _paginate(
        self,
        path: str,
        params: dict[str, Any],
        tr_id: str,
        ctx: str = "200",
        max_pages: int = HISTORY_MAX_PAGES,
        check: Any = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Follow KIS continuation headers; return (bodies, truncated).

        The next page is requested by echoing the body's ctx_area_fk/nk values back
        as params with tr_cont="N". `truncated` is True when KIS still had more pages
        at max_pages — callers must surface that, because a silently short trade
        history produces a wrong cost basis rather than an obvious error.
        ``check(body, tr_cont)`` sees every page before it is kept (strict contract).
        """
        fk_key, nk_key = f"CTX_AREA_FK{ctx}", f"CTX_AREA_NK{ctx}"
        page_params = dict(params)
        page_params.setdefault(fk_key, "")
        page_params.setdefault(nk_key, "")
        bodies: list[dict[str, Any]] = []
        seen_cursors: set[tuple[str, str]] = set()
        tr_cont = ""
        for _page in range(max_pages):
            body, next_cont = self._request(path, page_params, tr_id, tr_cont)
            if check is not None:
                check(body, next_cont)
            nonempty = any(value for key, value in body.items() if key.startswith("output"))
            if nonempty and body in bodies:
                # A replayed page must not multiply broker P&L rows. Preserve
                # earlier pages even on a terminal header. Compare entire JSON
                # bodies, not individual rows: distinct pages may overlap.
                return bodies, True
            bodies.append(body)
            if next_cont not in ("F", "M"):
                return bodies, False
            page_params[fk_key] = body.get(fk_key.lower()) or ""
            page_params[nk_key] = body.get(nk_key.lower()) or ""
            cursor = (str(page_params[fk_key]).strip(), str(page_params[nk_key]).strip())
            if cursor == ("", "") or cursor in seen_cursors:
                # Like the dashboard guard, but explicitly surface truncation.
                # Cursor values may contain account data: do not log them.
                return bodies, True
            seen_cursors.add(cursor)
            tr_cont = "N"
        logger.warning(
            "KIS pagination hit max_pages=%d for %s (%s) — result is truncated",
            max_pages, path, tr_id,
        )
        return bodies, True

    @staticmethod
    def _rows(bodies: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        """Concatenate an array output across paginated bodies."""
        rows: list[dict[str, Any]] = []
        for body in bodies:
            value = body.get(key)
            if isinstance(value, list):
                rows.extend(item for item in value if isinstance(item, dict))
            elif isinstance(value, dict) and value:
                rows.append(value)
        return rows

    # ── Domestic Balance ─────────────────────────────────

    def get_domestic_balance(self) -> dict[str, Any]:
        """국내 주식 잔고 조회 (TTTC8434R)."""
        data = self._get(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            params={
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt_cd,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_YN": "Y",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
            tr_id="TTTC8434R",
        )
        output1 = data.get("output1", [])  # per-stock detail
        output2 = data.get("output2", [])  # summary
        return {"stocks": output1, "summary": output2}

    def get_domestic_orderable_cash(self) -> dict[str, Any]:
        """국내 주문가능 현금 조회 (TTTC8908R)."""
        data = self._get(
            "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
            params={
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt_cd,
                "PDNO": "005930",
                "ORD_UNPR": "1",
                "ORD_DVSN": "01",
                "CMA_EVLU_AMT_ICLD_YN": "N",
                "OVRS_ICLD_YN": "N",
            },
            tr_id="TTTC8908R",
        )
        return data.get("output", {})

    # ── Overseas Balance ─────────────────────────────────

    def get_overseas_balance(self, markets: list[str] | None = None) -> dict[str, Any]:
        """해외 주식 잔고 조회 — US/JP/HK/CN 전 시장 합산.

        KIS scopes 해외주식 잔고 (TTTS3012R) by exchange + currency, so it is called
        once per market and merged; each row carries `market`, `exchange`, and
        `currency`. Cash and account-level totals come from 체결기준현재잔고
        (CTRP6504R) with NATN_CD="000" (all countries) — the previous
        NATN_CD="840" is what limited this call to US holdings only.

        Args:
            markets: KIS exchange codes to query (default: all of NASD, TKSE,
                SEHK, SHAA, SZAA).
        """
        codes = [c.strip().upper() for c in markets if c.strip()] if markets else list(_OVERSEAS_MARKETS)
        stocks: list[dict[str, Any]] = []
        by_market: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}
        truncated: list[str] = []

        for excg in codes:
            market, _natn, currency = _OVERSEAS_MARKETS.get(excg, (excg, "", "USD"))
            try:
                bodies, page_truncated = self._paginate(
                    "/uapi/overseas-stock/v1/trading/inquire-balance",
                    params={
                        "CANO": self.cano,
                        "ACNT_PRDT_CD": self.acnt_prdt_cd,
                        "OVRS_EXCG_CD": excg,
                        "TR_CRCY_CD": currency,
                    },
                    tr_id="TTTS3012R",
                )
            except KISAPIError:
                errors[excg] = "KIS source unavailable"
                continue
            if page_truncated:
                truncated.append(excg)
            rows = self._rows(bodies, "output1")
            for row in rows:
                row["market"] = market
                row["exchange"] = str(row.get("ovrs_excg_cd") or excg).upper()
                row["currency"] = str(row.get("tr_crcy_cd") or currency).upper()
                row["query_exchange"] = excg
            stocks.extend(rows)
            entry = by_market.setdefault(market, {"position_count": 0, "exchanges": [], "currency": currency})
            entry["position_count"] += len(rows)
            entry["exchanges"].append(excg)

        cash: list[dict[str, Any]] = []
        summary: list[dict[str, Any]] = []
        try:
            present, _ = self._request(
                "/uapi/overseas-stock/v1/trading/inquire-present-balance",
                params={
                    "CANO": self.cano,
                    "ACNT_PRDT_CD": self.acnt_prdt_cd,
                    "WCRC_FRCR_DVSN_CD": "02",  # 외화 기준
                    "NATN_CD": "000",  # 전체 국가
                    "TR_MKET_CD": "00",  # 전체 시장
                    "INQR_DVSN_CD": "00",  # 전체
                    "CTX_AREA_FK200": "",
                    "CTX_AREA_NK200": "",
                },
                tr_id="CTRP6504R",
            )
            cash = [row for row in (present.get("output2") or []) if isinstance(row, dict)]
            summary = [row for row in (present.get("output3") or []) if isinstance(row, dict)]
        except KISAPIError:
            errors["present_balance"] = "KIS source unavailable"

        # A held currency with no output2 row: read KIS's KRW foreign-cash total (basis 01) so
        # portfolio_model.kis_overseas_cash can prove that currency's cash is zero (else unknown).
        cash_check = None
        listed = {str(row.get("crcy_cd") or "").upper() for row in cash}
        if "present_balance" not in errors and {row["currency"] for row in stocks} - listed - {"KRW"}:
            cash_check = self._overseas_cash_check(stocks, cash)

        return {
            "stocks": stocks,
            "cash": cash,
            "summary": summary,
            "cash_check_krw": cash_check,
            "by_market": by_market,
            "markets_queried": codes,
            "truncated": bool(truncated),
            "truncated_markets": truncated,
            "errors": errors,
        }

    def _overseas_cash_check(self, stocks: list[dict[str, Any]], cash: list[dict[str, Any]]) -> dict[str, Any]:
        """The KRW-basis CTRP6504R fields the zero-cash proof reads, reused while its inputs hold.

        A successful read is reused for CASH_CHECK_REUSE_SECONDS when this balance read lists the
        same cash rows and the same held quantities as the read it was made for, so a chat burst
        does not send a second CTRP6504R right behind the first (EGW00215 under parallel tool calls).
        Any change in those inputs, or a failed read, reads again; the proof itself still fails
        closed on a mismatch. Never shared across accounts (one cache per client).
        """
        key = json.dumps([sorted(json.dumps(row, sort_keys=True, default=str) for row in cash),
                          sorted((str(row.get("currency")), str(row.get("ovrs_pdno")), str(row.get("ovrs_cblc_qty")))
                                 for row in stocks)])
        with _CASH_CHECK_LOCK:
            lock = self.__dict__.setdefault("_cash_check_lock", threading.Lock())
        with lock:
            cached = self.__dict__.get("_cash_check_cache")
            if cached and cached[1] == key and time.monotonic() - cached[0] < CASH_CHECK_REUSE_SECONDS:
                return copy.deepcopy(cached[2])
            try:
                krw = self.ledgerless_overseas_balance("01")["outputs"]
                check = {"basis": "01", "output3": krw["output3"],
                         "output1": [{k: row[k] for k in ("buy_crcy_cd", "ccld_qty_smtl1", "bass_exrt")}
                                     for row in krw["output1"]],
                         "output2": [{"crcy_cd": row["crcy_cd"]} for row in krw["output2"]]}
            except KISResponseError as exc:
                return {"basis": "01", "error": exc.reason}
            except KISAPIError:
                return {"basis": "01", "error": "KIS source unavailable"}
            self.__dict__["_cash_check_cache"] = (time.monotonic(), key, copy.deepcopy(check))
            return check

    def get_overseas_orderable_cash(self, natn_cd: str = "840") -> dict[str, Any]:
        """해외 주문가능 현금 조회 (TTTS3007R)."""
        # KIS requires a specific exchange code + dummy item for this endpoint.
        # Use NASD + QQQ for USD, TKSE + 7203 for JPY.
        if natn_cd == "392":
            excg, item = "TKSE", "7203"
        else:
            excg, item = "NASD", "QQQ"
        data = self._get(
            "/uapi/overseas-stock/v1/trading/inquire-psamount",
            params={
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt_cd,
                "OVRS_EXCG_CD": excg,
                "OVRS_ORD_UNPR": "1",
                "ITEM_CD": item,
            },
            tr_id="TTTS3007R",
        )
        return data.get("output", {})

    # ── Trade History ─────────────────────────────────────

    def get_domestic_trade_history(
        self, start_date: str, end_date: str, filled_only: bool = True
    ) -> dict[str, Any]:
        """국내 주식 일별 주문체결내역 조회 (TTTC0081R / CTSC9215R).

        KIS splits this endpoint by age: TTTC0081R covers the last 3 months and
        CTSC9215R covers anything older. A range that straddles the boundary is
        queried against both and merged (deduped by 주문일자+주문번호).

        Args:
            start_date: YYYYMMDD
            end_date: YYYYMMDD
            filled_only: True면 체결분만 (CCLD_DVSN=01), False면 전체 (00)
        """
        segments = _split_by_recency(start_date, end_date)
        trades: list[dict[str, Any]] = []
        summary: dict[str, Any] = {}
        seen: set[tuple[str, str]] = set()
        tr_ids: list[str] = []
        truncated = False

        for tr_id, seg_start, seg_end in segments:
            params = {
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt_cd,
                "INQR_STRT_DT": seg_start,
                "INQR_END_DT": seg_end,
                "SLL_BUY_DVSN_CD": "00",  # 전체
                "PDNO": "",
                "CCLD_DVSN": "01" if filled_only else "00",
                "INQR_DVSN": "01",  # 정순
                "INQR_DVSN_3": "00",  # 전체
                "INQR_DVSN_1": "",
                "ORD_GNO_BRNO": "",
                "ODNO": "",
                "EXCG_ID_DVSN_CD": "ALL",  # KRX + NXT + SOR
            }
            try:
                bodies, page_truncated = self._paginate(
                    "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                    params=params,
                    tr_id=tr_id,
                    ctx="100",
                )
            except KISAPIError as exc:
                if not _exchange_rejected(exc):
                    raise  # unrelated/auth/rate errors are not exchange rejection
                # Accounts without NXT/SOR access can reject EXCG_ID_DVSN_CD=ALL.
                params["EXCG_ID_DVSN_CD"] = "KRX"
                bodies, page_truncated = self._paginate(
                    "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                    params=params,
                    tr_id=tr_id,
                    ctx="100",
                )
            truncated = truncated or page_truncated
            tr_ids.append(tr_id)
            for row in self._rows(bodies, "output1"):
                key = (str(row.get("ord_dt", "")), str(row.get("odno", "")))
                if key in seen:
                    continue
                seen.add(key)
                trades.append(row)
            for body in bodies:
                output2 = body.get("output2")
                if isinstance(output2, dict) and output2:
                    summary = output2

        return {"trades": trades, "summary": summary, "tr_ids": tr_ids, "truncated": truncated}

    def get_overseas_trade_history(
        self, start_date: str, end_date: str, exchange: str = "%", filled_only: bool = True
    ) -> dict[str, Any]:
        """해외 주식 주문체결내역 조회 (TTTS3035R).

        Args:
            start_date: YYYYMMDD (주문일자 기준)
            end_date: YYYYMMDD
            exchange: KIS 거래소코드, "%"면 전 거래소
            filled_only: True면 체결분만 (CCLD_NCCS_DVSN=01), False면 전체 (00)
        """
        bodies, truncated = self._paginate(
            "/uapi/overseas-stock/v1/trading/inquire-ccnl",
            params={
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt_cd,
                "PDNO": "%",  # 전종목
                "ORD_STRT_DT": start_date,
                "ORD_END_DT": end_date,
                "SLL_BUY_DVSN": "00",  # 전체
                "CCLD_NCCS_DVSN": "01" if filled_only else "00",
                "OVRS_EXCG_CD": exchange or "%",
                "SORT_SQN": "DS",  # 정순
                "ORD_DT": "",
                "ORD_GNO_BRNO": "",
                "ODNO": "",
            },
            tr_id="TTTS3035R",
        )
        trades = self._rows(bodies, "output")
        for row in trades:
            excg = str(row.get("ovrs_excg_cd") or "").upper()
            market, _natn, currency = _OVERSEAS_MARKETS.get(excg, ("", "", ""))
            row["market"] = market or str(row.get("tr_natn_name") or "")
            row["exchange"] = excg
            row["currency"] = str(row.get("tr_crcy_cd") or currency).upper()
        return {"trades": trades, "tr_ids": ["TTTS3035R"], "truncated": truncated}

    def get_overseas_period_trans(
        self, start_date: str, end_date: str, exchange: str = "%"
    ) -> dict[str, Any]:
        """해외 주식 일별 거래내역 조회 (CTOS4001R).

        Settlement-dated view of overseas trades: carries 결제일자, 정산금액, and
        both domestic/overseas fee columns that inquire-ccnl does not report.
        """
        bodies, truncated = self._paginate(
            "/uapi/overseas-stock/v1/trading/inquire-period-trans",
            params={
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt_cd,
                "ERLM_STRT_DT": start_date,
                "ERLM_END_DT": end_date,
                "OVRS_EXCG_CD": exchange or "%",
                "PDNO": "",
                "SLL_BUY_DVSN_CD": "00",  # 전체
                "LOAN_DVSN_CD": "",
            },
            tr_id="CTOS4001R",
            ctx="100",
        )
        return {
            "transactions": self._rows(bodies, "output1"),
            "summary": self._rows(bodies, "output2"),
            "tr_ids": ["CTOS4001R"],
            "truncated": truncated,
        }

    # ── Realized P&L (broker-computed) ────────────────────

    def get_domestic_realized_pnl(self, start_date: str, end_date: str) -> dict[str, Any]:
        """국내 주식 기간별 매매손익 현황 조회 (TTTC8715R).

        Per-symbol realized P&L with fees and taxes as the broker computed them.
        """
        bodies, truncated = self._paginate(
            "/uapi/domestic-stock/v1/trading/inquire-period-trade-profit",
            params={
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt_cd,
                "SORT_DVSN": "01",  # 과거 순
                "INQR_STRT_DT": start_date,
                "INQR_END_DT": end_date,
                "CBLC_DVSN": "00",  # 전체
                "PDNO": "",
            },
            tr_id="TTTC8715R",
            ctx="100",
        )
        summary: dict[str, Any] = {}
        for body in bodies:
            output2 = body.get("output2")
            if isinstance(output2, dict) and output2:
                summary = output2
        return {
            "trades": self._rows(bodies, "output1"),
            "summary": summary,
            "tr_ids": ["TTTC8715R"],
            "truncated": truncated,
        }

    def get_overseas_realized_pnl(
        self, start_date: str, end_date: str, markets: list[str] | None = None
    ) -> dict[str, Any]:
        """해외 주식 기간손익 조회 (TTTS3039R) — 시장별 조회 후 합산.

        Returns broker-computed realized P&L in the trade currency (외화 기준).
        """
        codes = [c.strip().upper() for c in markets if c.strip()] if markets else list(_OVERSEAS_MARKETS)
        trades: list[dict[str, Any]] = []
        summary: list[dict[str, Any]] = []
        errors: dict[str, str] = {}
        truncated: list[str] = []
        for excg in codes:
            market, _natn, currency = _OVERSEAS_MARKETS.get(excg, (excg, "", "USD"))
            try:
                bodies, page_truncated = self._paginate(
                    "/uapi/overseas-stock/v1/trading/inquire-period-profit",
                    params={
                        "CANO": self.cano,
                        "ACNT_PRDT_CD": self.acnt_prdt_cd,
                        "OVRS_EXCG_CD": excg,
                        "NATN_CD": "",
                        "CRCY_CD": currency,
                        "PDNO": "",
                        "INQR_STRT_DT": start_date,
                        "INQR_END_DT": end_date,
                        "WCRC_FRCR_DVSN_CD": "01",  # 외화
                    },
                    tr_id="TTTS3039R",
                )
            except KISAPIError:
                errors[excg] = "KIS source unavailable"
                continue
            if page_truncated:
                truncated.append(excg)
            for row in self._rows(bodies, "output1"):
                row["market"] = market
                row["exchange"] = excg
                row["currency"] = currency
                trades.append(row)
            summary.extend(self._rows(bodies, "output2"))
        return {
            "trades": trades,
            "summary": summary,
            "errors": errors,
            "tr_ids": ["TTTS3039R"],
            "truncated": bool(truncated),
            "truncated_markets": truncated,
        }

    # ── Market Quotes ─────────────────────────────────────

    def get_domestic_quote(self, symbol: str) -> dict[str, Any]:
        """국내 주식 현재가 조회 (FHKST01010100).

        Args:
            symbol: 6-digit stock code (e.g., '005930' for Samsung Electronics)
        """
        symbol = symbol.zfill(6)
        # Try market codes: J=KRX, UN=통합, NX=NXT
        for market_code in ["J", "UN", "NX"]:
            try:
                data = self._get(
                    "/uapi/domestic-stock/v1/quotations/inquire-price",
                    params={
                        "FID_COND_MRKT_DIV_CODE": market_code,
                        "FID_INPUT_ISCD": symbol,
                    },
                    tr_id="FHKST01010100",
                )
                output = data.get("output", {})
                if output:
                    return {
                        "symbol": symbol,
                        "market_code": market_code,
                        "price": output.get("stck_prpr"),
                        "previous_close": output.get("stck_sdpr"),  # Base price = previous close; live FHKST01010100 has no prdy_clpr. Never the open.
                        "change": output.get("prdy_vrss"),
                        "change_pct": output.get("prdy_ctrt"),
                        "open": output.get("stck_oprc"),
                        "high": output.get("stck_hgpr"),
                        "low": output.get("stck_lwpr"),
                        "volume": output.get("acml_vol"),
                        "raw": output,
                    }
            except KISAPIError:
                continue
        return {"symbol": symbol, "error": "No quote data available"}

    def get_overseas_quote(self, symbol: str, exchange: str = "NASDAQ") -> dict[str, Any]:
        """해외 주식 현재가 조회 (HHDFS00000300).

        Args:
            symbol: Stock symbol (e.g., 'AAPL')
            exchange: 'NASDAQ', 'NYSE', 'AMEX', 'TKSE' (Tokyo)
        """
        excd = _EXCD_MAP.get(exchange.upper(), "NAS")
        data = self._get(
            "/uapi/overseas-price/v1/quotations/price",
            params={"AUTH": "", "EXCD": excd, "SYMB": symbol.upper()},
            tr_id="HHDFS00000300",
        )
        output = data.get("output", {})
        return {
            "symbol": symbol.upper(),
            "exchange": exchange.upper(),
            "venue": "Tokyo Stock Exchange" if excd == "TSE" else exchange.upper(),
            "excd": excd,
            "price": output.get("last"),
            "previous_close": output.get("base"),
            "change": output.get("diff"),
            "change_pct": output.get("rate"),
            "volume": output.get("tvol"),
            "amount": output.get("tamt"),
            "raw": output,
        }

    # ── Validated reads (ledgerless capture) ──────────────

    def read_validated(self, tr_id: str, params: dict[str, Any],
                       max_pages: int = HISTORY_MAX_PAGES) -> dict[str, Any]:
        """One read-only TR through the strict contract: every page validated, then merged.

        Raises KISAPIError (transport/broker) or KISResponseError (contract). Only
        TRs in KIS_LEDGERLESS_READS are allowed; none of them places or changes orders.
        """
        path = KIS_LEDGERLESS_READS.get(tr_id)
        if path is None:
            raise KISResponseError(tr_id, "KIS_TR_NOT_IN_CONTRACT")
        ctx = KIS_RESPONSE_SPECS[tr_id]["ctx"]
        pages: list[dict[str, Any]] = []
        if ctx is None:
            body, next_cont = self._request(path, params, tr_id)
            pages.append(validate_kis_response(tr_id, body, next_cont))
        else:
            _bodies, truncated = self._paginate(
                path, params, tr_id, ctx=ctx, max_pages=max_pages,
                check=lambda body, next_cont: pages.append(validate_kis_response(tr_id, body, next_cont)))
            if truncated:
                raise KISResponseError(tr_id, "KIS_PAGING_TRUNCATED")
        return merge_kis_pages(tr_id, pages)

    def _account_params(self, **extra: Any) -> dict[str, Any]:
        return {"CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt_cd, **extra}

    def ledgerless_domestic_balance(self) -> dict[str, Any]:
        """TTTC8434R, all pages: KRX regular-close prices (AFHR_FLPR_YN=N), D+2 cash basis."""
        return self.read_validated("TTTC8434R", self._account_params(
            AFHR_FLPR_YN="N", OFL_YN="", INQR_DVSN="02", UNPR_DVSN="01", FUND_STTL_ICLD_YN="N",
            FNCG_AMT_AUTO_YN="Y", PRCS_DVSN="00"))

    def ledgerless_account_assets(self) -> dict[str, Any]:
        """CTRP6548R 투자계좌자산현황 (settlement basis), the KIS whole-account KRW total."""
        return self.read_validated("CTRP6548R", self._account_params(INQR_DVSN_1="", BSPR_BF_DT_APLY_YN=""))

    def ledgerless_domestic_daily_closes(self, symbol: str, market: str = "J") -> dict[str, Any]:
        """FHKST01010400 daily closes (last 30 sessions) with session dates; market J=KRX, NX=NXT."""
        return self.read_validated("FHKST01010400", {
            "FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": symbol, "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "1"})

    def ledgerless_overseas_daily_closes(self, symbol: str, excd: str) -> dict[str, Any]:
        """HHDFS76240000 overseas daily closes with their local session dates."""
        return self.read_validated("HHDFS76240000", {
            "AUTH": "", "EXCD": excd, "SYMB": symbol, "GUBN": "0", "BYMD": "", "MODP": "0"})

    def ledgerless_domestic_daily_pnl(self, start_date: str, end_date: str) -> dict[str, Any]:
        """TTTC8708R 기간별손익일별합산: realized P&L, fee and tax per trading day."""
        return self.read_validated("TTTC8708R", self._account_params(
            INQR_STRT_DT=start_date, INQR_END_DT=end_date, PDNO="", SORT_DVSN="00", INQR_DVSN="00",
            CBLC_DVSN="00"))

    def ledgerless_domestic_fills(self, start_date: str, end_date: str) -> dict[str, Any]:
        """TTTC0081R filled orders (KRX+NXT+SOR; KRX only when ALL is refused). <= 3 months."""
        params = self._account_params(
            INQR_STRT_DT=start_date, INQR_END_DT=end_date, SLL_BUY_DVSN_CD="00", PDNO="", CCLD_DVSN="01",
            INQR_DVSN="01", INQR_DVSN_3="00", INQR_DVSN_1="", ORD_GNO_BRNO="", ODNO="", EXCG_ID_DVSN_CD="ALL")
        try:
            return self.read_validated("TTTC0081R", params)
        except KISAPIError as exc:
            if not _exchange_rejected(exc):
                raise
            params["EXCG_ID_DVSN_CD"] = "KRX"
            return self.read_validated("TTTC0081R", params)

    def ledgerless_domestic_trade_costs(self, start_date: str, end_date: str) -> dict[str, Any]:
        """TTTC8715R per-day, per-symbol fee and tax (broker computed)."""
        return self.read_validated("TTTC8715R", self._account_params(
            SORT_DVSN="01", INQR_STRT_DT=start_date, INQR_END_DT=end_date, CBLC_DVSN="00", PDNO=""))

    def ledgerless_overseas_balance(self, basis: str = "02") -> dict[str, Any]:
        """CTRP6504R 체결기준현재잔고, all countries, one page. basis 02: rows in the trade
        currency (output3 amounts are then a raw mixed-currency sum); 01: rows and output3 in KRW."""
        return self.read_validated("CTRP6504R", self._account_params(
            WCRC_FRCR_DVSN_CD=basis, NATN_CD="000", TR_MKET_CD="00", INQR_DVSN_CD="00"))

    def ledgerless_overseas_settlements(self, start_date: str, end_date: str) -> dict[str, Any]:
        """CTOS4001R 일별거래내역: per-trade settlement date and net settlement amounts."""
        return self.read_validated("CTOS4001R", self._account_params(
            ERLM_STRT_DT=start_date, ERLM_END_DT=end_date, OVRS_EXCG_CD="%", PDNO="", SLL_BUY_DVSN_CD="00",
            LOAN_DVSN_CD=""))

    def ledgerless_overseas_realized(self, start_date: str, end_date: str, excg: str) -> dict[str, Any]:
        """TTTS3039R realized P&L in the trade currency for one exchange (available from T+1)."""
        currency = _OVERSEAS_MARKETS.get(excg, ("", "", "USD"))[2]
        return self.read_validated("TTTS3039R", self._account_params(
            OVRS_EXCG_CD=excg, NATN_CD="", CRCY_CD=currency, PDNO="", INQR_STRT_DT=start_date,
            INQR_END_DT=end_date, WCRC_FRCR_DVSN_CD="01"))

    def ledgerless_overseas_fills(self, start_date: str, end_date: str) -> dict[str, Any]:
        """TTTS3035R filled overseas orders by local order date, all exchanges."""
        return self.read_validated("TTTS3035R", self._account_params(
            PDNO="%", ORD_STRT_DT=start_date, ORD_END_DT=end_date, SLL_BUY_DVSN="00", CCLD_NCCS_DVSN="01",
            OVRS_EXCG_CD="%", SORT_SQN="DS", ORD_DT="", ORD_GNO_BRNO="", ODNO=""))

    # ── Lifecycle ─────────────────────────────────────────

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
