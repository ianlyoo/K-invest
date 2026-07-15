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

# Overseas market groups: (exchange_code, country_code, currency)
_OVERSEAS_GROUPS = [
    ("NASD", "840", "USD"),
    ("TKSE", "392", "JPY"),
    ("SEHK", "344", "HKD"),
    ("SHAA", "156", "CNY"),
]


class KISAPIError(Exception):
    """Raised when the KIS API returns an error."""

    def __init__(self, status: int, body: Any, msg: str = ""):
        self.status = status
        self.body = body
        super().__init__(msg or f"KIS API {status}: {body}")


class KISClient:
    """Korea Investment Securities API client (read-only).

    Credentials resolve in order: direct env vars (KIS_APP_KEY, KIS_APP_SECRET,
    KIS_CANO, KIS_ACNT_PRDT_CD, KIS_URL_BASE), then a KIS_ENV_FILE env file
    (prefixed or unprefixed keys, e.g. APP_KEY/APP_SECRET).
    """

    def __init__(self):
        file_env: dict[str, str] = {}
        env_file = os.environ.get("KIS_ENV_FILE", "").strip()
        if env_file:
            file_env = load_env_file(Path(env_file).expanduser())

        def _cred(env_name: str, *file_names: str) -> str:
            # Direct process env: prefixed name only (bare APP_KEY in the shared
            # env file must not be mistaken for KIS credentials).
            value = os.environ.get(env_name, "").strip()
            if value:
                return value
            # KIS_ENV_FILE tier: prefixed or unprefixed keys allowed.
            for name in (env_name, *file_names):
                value = (file_env.get(name) or "").strip()
                if value:
                    return value
            return ""

        self.app_key = _cred("KIS_APP_KEY", "APP_KEY")
        self.app_secret = _cred("KIS_APP_SECRET", "APP_SECRET")
        self.cano = _cred("KIS_CANO", "CANO")
        self.acnt_prdt_cd = _cred("KIS_ACNT_PRDT_CD", "ACNT_PRDT_CD")
        self.url_base = (
            _cred("KIS_URL_BASE", "URL_BASE") or "https://openapi.koreainvestment.com:9443"
        ).rstrip("/")

        if not self.app_key or not self.app_secret:
            raise NotConfiguredError(
                "kis",
                "KIS credentials not configured — set KIS_APP_KEY/KIS_APP_SECRET "
                "(or KIS_ENV_FILE pointing to an env file with APP_KEY/APP_SECRET)",
            )

        self._token: str | None = None
        self._token_expires: float = 0
        self._http = httpx.Client(timeout=15.0)

    # ── Token Management ──────────────────────────────────

    def _token_scope_key(self) -> str:
        return hashlib.sha256(f"{self.app_key}::{self.app_secret}".encode()).hexdigest()

    def _load_cached_token(self) -> bool:
        try:
            if TOKEN_CACHE_PATH.exists():
                data = json.loads(TOKEN_CACHE_PATH.read_text())
                if data.get("scope") != self._token_scope_key():
                    return False
                if data.get("expires_at", 0) > time.time() + 60:
                    self._token = data["access_token"]
                    self._token_expires = data["expires_at"]
                    return True
        except Exception as e:
            logger.warning("KIS token cache read error: %s", e)
        return False

    def _save_cached_token(self, token: str, expires_in: int) -> None:
        try:
            TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            expires_at = time.time() + expires_in
            TOKEN_CACHE_PATH.write_text(json.dumps({
                "scope": self._token_scope_key(),
                "access_token": token,
                "expires_at": expires_at,
            }))
            os.chmod(TOKEN_CACHE_PATH, 0o600)
            self._token_expires = expires_at
        except Exception as e:
            logger.warning("KIS token cache write error: %s", e)

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
            TOKEN_CACHE_PATH.unlink(missing_ok=True)
        except OSError:
            pass

    def _get(self, path: str, params: dict[str, Any], tr_id: str = "") -> dict[str, Any]:
        """GET request with KIS authentication. Returns parsed JSON."""
        headers = self._headers(tr_id)
        resp = self._http.get(
            f"{self.url_base}{path}",
            params=params,
            headers=headers,
        )
        if resp.status_code == 401:
            logger.warning("Got 401 from KIS API, refreshing token")
            self._invalidate_token()
            headers = self._headers(tr_id)
            resp = self._http.get(f"{self.url_base}{path}", params=params, headers=headers)
        if resp.status_code != 200:
            raise KISAPIError(resp.status_code, resp.text[:500])

        data = resp.json()
        rt_cd = str(data.get("rt_cd", ""))
        if rt_cd != "0" and str(data.get("msg_cd", "")).upper() in {"EGW00123", "EGW00121", "EGW00110"}:
            logger.warning("Got KIS token error %s, refreshing token", data.get("msg_cd"))
            self._invalidate_token()
            retry = self._http.get(f"{self.url_base}{path}", params=params, headers=self._headers(tr_id))
            if retry.status_code != 200:
                raise KISAPIError(retry.status_code, retry.text[:500])
            data = retry.json()
            rt_cd = str(data.get("rt_cd", ""))
        if rt_cd != "0":
            msg = data.get("msg1", "")
            raise KISAPIError(resp.status_code, data, f"KIS error: {msg}")

        return data

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

    def get_overseas_balance(self) -> dict[str, Any]:
        """해외 주식 잔고 조회 (CTRP6504R)."""
        data = self._get(
            "/uapi/overseas-stock/v1/trading/inquire-present-balance",
            params={
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt_cd,
                "WCRC_FRCR_DVSN_CD": "02",
                "NATN_CD": "840",
                "TR_MKET_CD": "00",
                "INQR_DVSN_CD": "00",
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": "",
            },
            tr_id="CTRP6504R",
        )
        output1 = data.get("output1", [])  # per-stock
        output2 = data.get("output2", [])  # cash
        output3 = data.get("output3", [])  # summary
        return {"stocks": output1, "cash": output2, "summary": output3}

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
        self, start_date: str, end_date: str
    ) -> dict[str, Any]:
        """국내 주식 체결내역 조회.

        Args:
            start_date: YYYYMMDD
            end_date: YYYYMMDD
        """
        data = self._get(
            "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
            params={
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt_cd,
                "INQR_STRT_DT": start_date,
                "INQR_END_DT": end_date,
                "SLL_BUY_DVSN_CD": "00",  # All
                "CHKG_CLSS_CODE": "",
                "CCLD_DVSN": "01",  # Filled
                "PDNO": "",
                "SORT_DVSN": "01",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
            tr_id="TTTC8001R",
        )
        return {"trades": data.get("output1", []), "summary": data.get("output2", {})}

    def get_overseas_trade_history(
        self, start_date: str, end_date: str
    ) -> dict[str, Any]:
        """해외 주식 체결내역 조회.

        Args:
            start_date: YYYYMMDD
            end_date: YYYYMMDD
        """
        data = self._get(
            "/uapi/overseas-stock/v1/trading/inquire-ccnl",
            params={
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt_cd,
                "STRT_DT": start_date,
                "END_DT": end_date,
                "EXCD_CD": "",
                "TRN_DVSN": "00",  # All
                "SORT_DVSN": "01",
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": "",
            },
            tr_id="TTTS8001R",
        )
        return {"trades": data.get("output1", [])}

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
