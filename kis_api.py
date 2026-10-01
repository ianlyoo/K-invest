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

import hashlib
import json
import logging
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from kinvest_common import NotConfiguredError, load_env_file
from kinvest_common import cache_dir as _default_cache_dir

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
RATE_LIMIT_RETRIES = max(0, min(2, int(os.environ.get("KIS_RATE_LIMIT_RETRIES", "2"))))
RATE_LIMIT_MSG_CD = "EGW00201"

#: app key -> monotonic timestamp of its last request; the cap is per app key,
#: so every client sharing a key shares the pacing state.
_LAST_REQUEST_AT: dict[str, float] = {}

# Continuation page cap. Overseas 주문체결내역 returns ~20 rows per page, so a wide
# window needs many pages; every paginated result reports whether it was truncated.
HISTORY_MAX_PAGES = int(os.environ.get("KIS_HISTORY_MAX_PAGES", "60"))

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

    KIS returns HTTP 500 with rt_cd=1 / msg_cd=EGW00201 for this, so the status
    code alone cannot distinguish it from a real server error.
    """
    body = _error_body(resp)
    return isinstance(body, dict) and str(body.get("msg_cd", "")).upper() == RATE_LIMIT_MSG_CD


class KISAPIError(Exception):
    """Raised when the KIS API returns an error."""

    def __init__(self, status: int, body: Any, msg: str = ""):
        self.status = status
        self.body = body
        super().__init__(msg or f"KIS API {status}: {body}")

    @property
    def rate_limited(self) -> bool:
        body = self.body if isinstance(self.body, dict) else {}
        return str(body.get("msg_cd", "")).upper() == RATE_LIMIT_MSG_CD


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
        self._http = httpx.Client(timeout=15.0)

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

    def _load_cached_token(self) -> bool:
        try:
            if self._token_cache_path.exists():
                data = json.loads(self._token_cache_path.read_text())
                if data.get("scope") != self._token_scope_key():
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
        self._token = None
        self._token_expires = 0
        try:
            self._token_cache_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _request(
        self, path: str, params: dict[str, Any], tr_id: str = "", tr_cont: str = ""
    ) -> tuple[dict[str, Any], str]:
        """GET request with KIS auth. Returns (parsed JSON, response tr_cont header).

        The response `tr_cont` header drives KIS continuation: "F"/"M" mean more
        pages are available, "D"/"E" mean this is the last page.
        """
        def _send() -> httpx.Response:
            headers = self._headers(tr_id)
            if tr_cont:
                headers["tr_cont"] = tr_cont
            self._throttle()
            return self._http.get(f"{self.url_base}{path}", params=params, headers=headers)

        resp = _send()
        if resp.status_code == 401:
            logger.warning("Got 401 from KIS API, refreshing token")
            self._invalidate_token()
            resp = _send()

        # A rate-limited burst is transient — back off and retry before failing.
        for attempt in range(RATE_LIMIT_RETRIES):
            if not _is_rate_limited(resp):
                break
            delay = MIN_REQUEST_INTERVAL * (2**attempt) + 0.25
            logger.warning(
                "KIS rate limit (%s) on %s, retrying in %.2fs (attempt %d/%d)",
                RATE_LIMIT_MSG_CD, path, delay, attempt + 1, RATE_LIMIT_RETRIES,
            )
            time.sleep(delay)
            resp = _send()

        if resp.status_code != 200:
            raise KISAPIError(resp.status_code, _error_body(resp))

        data = resp.json()
        rt_cd = str(data.get("rt_cd", ""))
        if rt_cd != "0" and str(data.get("msg_cd", "")).upper() in {"EGW00123", "EGW00121", "EGW00110"}:
            logger.warning("Got KIS token error %s, refreshing token", data.get("msg_cd"))
            self._invalidate_token()
            retry_headers = self._headers(tr_id)
            if tr_cont:
                retry_headers["tr_cont"] = tr_cont
            retry = self._http.get(f"{self.url_base}{path}", params=params, headers=retry_headers)
            if retry.status_code != 200:
                raise KISAPIError(retry.status_code, _error_body(retry))
            resp = retry
            data = retry.json()
            rt_cd = str(data.get("rt_cd", ""))
        if rt_cd != "0":
            msg = data.get("msg1", "")
            raise KISAPIError(resp.status_code, data, f"KIS error: {msg}")

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
    ) -> tuple[list[dict[str, Any]], bool]:
        """Follow KIS continuation headers; return (bodies, truncated).

        The next page is requested by echoing the body's ctx_area_fk/nk values back
        as params with tr_cont="N". `truncated` is True when KIS still had more pages
        at max_pages — callers must surface that, because a silently short trade
        history produces a wrong cost basis rather than an obvious error.
        """
        fk_key, nk_key = f"CTX_AREA_FK{ctx}", f"CTX_AREA_NK{ctx}"
        page_params = dict(params)
        page_params.setdefault(fk_key, "")
        page_params.setdefault(nk_key, "")
        bodies: list[dict[str, Any]] = []
        tr_cont = ""
        for _page in range(max_pages):
            body, next_cont = self._request(path, page_params, tr_id, tr_cont)
            bodies.append(body)
            if next_cont not in ("F", "M"):
                return bodies, False
            page_params[fk_key] = body.get(fk_key.lower()) or ""
            page_params[nk_key] = body.get(nk_key.lower()) or ""
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

        return {
            "stocks": stocks,
            "cash": cash,
            "summary": summary,
            "by_market": by_market,
            "markets_queried": codes,
            "truncated": bool(truncated),
            "truncated_markets": truncated,
            "errors": errors,
        }

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
            except KISAPIError:
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
                        "previous_close": output.get("stck_oprc") or output.get("prdy_clpr"),
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

    # ── Lifecycle ─────────────────────────────────────────

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
