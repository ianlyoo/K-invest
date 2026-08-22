#!/usr/bin/env python3
"""Toss Securities Open API — read-only client for MCP server.

OAuth 2.0 Client Credentials Grant with token caching and auto-renewal.
Market-data, stock-info, market-info, and account/holdings/order-history
READ-ONLY endpoints are implemented.
NO order creation, modification, or cancellation endpoints exist in this file.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

from kinvest_common import cache_dir as _default_cache_dir

logger = logging.getLogger("k-invest.toss")

BASE_URL = "https://openapi.tossinvest.com"
TOKEN_CACHE_PATH = Path(
    os.environ.get(
        "TOSS_TOKEN_CACHE",
        str(_default_cache_dir() / "token.json"),
    )
)

# Rate-limit groups (for reference only — not enforced client-side)
RATE_LIMITS = {
    "AUTH": 5,
    "STOCK": 5,
    "MARKET_INFO": 3,
    "MARKET_DATA": 10,
    "MARKET_DATA_CHART": 5,
    "ACCOUNT": 1,
    "ASSET": 5,
    "ORDER_HISTORY": 5,
}


class TossAPIError(Exception):
    """Raised when the Toss API returns an error."""

    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        msg = self._extract_message(body)
        super().__init__(f"Toss API {status}: {msg}")

    @staticmethod
    def _extract_message(body: Any) -> str:
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                return f"{err.get('code', '?')}: {err.get('message', '')}"
            return str(body)
        return str(body)


class TossClient:
    """Toss Securities API client (read-only).

    All config is read from environment variables:
      - TOSS_CLIENT_ID
      - TOSS_CLIENT_SECRET
    """

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        token_cache_path: Path | None = None,
    ):
        self.client_id = (client_id or os.environ.get("TOSS_CLIENT_ID", "")).strip()
        self.client_secret = (
            client_secret or os.environ.get("TOSS_CLIENT_SECRET", "")
        ).strip()
        if not self.client_id or not self.client_secret:
            raise RuntimeError(
                "TOSS_CLIENT_ID and TOSS_CLIENT_SECRET must be set in environment"
            )

        self._token: str | None = None
        self._token_expires: float = 0
        self._account_seq: int | None = None
        self._token_cache_path = token_cache_path or TOKEN_CACHE_PATH
        self._http = httpx.Client(base_url=BASE_URL, timeout=15.0, headers={"Accept-Encoding": "identity"})

    # ── OAuth Token Management ────────────────────────────

    def _load_cached_token(self) -> bool:
        """Load token from cache file if valid."""
        try:
            if self._token_cache_path.exists():
                data = json.loads(self._token_cache_path.read_text())
                if data.get("client_id") != self.client_id:
                    return False
                expires_at = data.get("expires_at", 0)
                if expires_at > time.time() + 120:
                    self._token = data["access_token"]
                    self._token_expires = expires_at
                    logger.debug(
                        "Loaded cached Toss token (expires in %ds)",
                        int(expires_at - time.time()),
                    )
                    return True
        except Exception as e:
            logger.warning("Token cache read error: %s", e)
        return False

    def _save_cached_token(self, token: str, expires_in: int) -> None:
        """Persist token to cache file."""
        try:
            self._token_cache_path.parent.mkdir(parents=True, exist_ok=True)
            expires_at = time.time() + expires_in
            self._token_cache_path.write_text(
                json.dumps(
                    {
                        "client_id": self.client_id,
                        "access_token": token,
                        "expires_at": expires_at,
                    }
                )
            )
            os.chmod(self._token_cache_path, 0o600)
            self._token_expires = expires_at
            logger.debug("Cached Toss token (expires in %ds)", expires_in)
        except Exception as e:
            logger.warning("Token cache write error: %s", e)

    def _ensure_token(self) -> str:
        """Get a valid access token, refreshing if needed."""
        if self._token and time.time() < self._token_expires - 120:
            return self._token

        if self._load_cached_token():
            token = self._token
            if token is not None:
                return token

        logger.info("Requesting new Toss OAuth token")
        resp = self._http.post(
            "/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            raise TossAPIError(resp.status_code, self._safe_json(resp))

        body = resp.json()
        token = body["access_token"]
        expires_in = body.get("expires_in", 86400)
        self._token = token
        self._save_cached_token(token, expires_in)
        return token

    @staticmethod
    def _safe_json(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text[:500]}

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        account: bool = False,
    ) -> dict[str, Any]:
        """Make an authenticated GET request. Returns full parsed JSON."""
        token = self._ensure_token()
        headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
        if account:
            headers["X-Tossinvest-Account"] = str(self.get_account_seq())

        resp = self._http.get(path, params=params, headers=headers)
        if resp.status_code == 401:
            # Token might be invalid — force refresh and retry once
            logger.warning("Got 401 from Toss API, refreshing token")
            self._token = None
            try:
                self._token_cache_path.unlink(missing_ok=True)
            except Exception:
                pass
            token = self._ensure_token()
            headers["Authorization"] = f"Bearer {token}"
            resp = self._http.get(path, params=params, headers=headers)

        if resp.status_code != 200:
            raise TossAPIError(resp.status_code, self._safe_json(resp))

        return resp.json()

    # ── Market Data ───────────────────────────────────────

    def get_prices(self, symbols: str | list[str]) -> list[dict[str, Any]]:
        """현재가 조회. symbols: 단일 심볼 또는 리스트/콤마 구분 문자열."""
        if isinstance(symbols, list):
            symbols = ",".join(symbols)
        resp = self._get("/api/v1/prices", params={"symbols": symbols})
        return resp.get("result", [])

    def get_orderbook(self, symbol: str) -> dict[str, Any]:
        """호가 조회."""
        return self._get("/api/v1/orderbook", params={"symbol": symbol}).get("result", {})

    def get_recent_trades(self, symbol: str) -> list[dict[str, Any]]:
        """최근 체결 내역 조회 (시장 체결, 계좌 아님)."""
        return self._get("/api/v1/trades", params={"symbol": symbol}).get("result", [])

    def get_price_limits(self, symbol: str) -> dict[str, Any]:
        """상/하한가 조회."""
        return self._get("/api/v1/price-limits", params={"symbol": symbol}).get("result", {})

    def get_candles(
        self,
        symbol: str,
        interval: str = "1d",
        count: int = 100,
        before: str | None = None,
        adjusted: bool = True,
    ) -> dict[str, Any]:
        """캔들 차트 조회.

        Args:
            symbol: 종목 심볼
            interval: '1d' (일봉) 또는 '1m' (1분봉)
            count: 봉 개수 (최대 200)
            before: 페이지네이션 커서 (ISO 8601)
            adjusted: 수정주가 적용 여부
        """
        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "count": count,
            "adjusted": str(adjusted).lower(),
        }
        if before:
            params["before"] = before
        return self._get("/api/v1/candles", params=params).get("result", {})

    # ── Stock Info ────────────────────────────────────────

    def get_stock_info(self, symbols: str | list[str]) -> list[dict[str, Any]]:
        """종목 기본 정보 조회."""
        if isinstance(symbols, list):
            symbols = ",".join(symbols)
        return self._get("/api/v1/stocks", params={"symbols": symbols}).get("result", [])

    def get_stock_warnings(self, symbol: str) -> list[dict[str, Any]]:
        """매수 유의사항 조회."""
        return self._get(f"/api/v1/stocks/{symbol}/warnings").get("result", [])

    # ── Market Info ───────────────────────────────────────

    def get_exchange_rate(self, base: str = "USD", quote: str = "KRW") -> dict[str, Any]:
        """환율 조회."""
        return self._get(
            "/api/v1/exchange-rate",
            params={"baseCurrency": base, "quoteCurrency": quote},
        ).get("result", {})

    def get_market_calendar(self, country: str = "US") -> dict[str, Any]:
        """시장 운영 시간 조회. country: 'KR' 또는 'US'."""
        return self._get(f"/api/v1/market-calendar/{country}").get("result", {})

    # ── Account & Asset (READ-ONLY) ───────────────────────

    def get_accounts(self) -> list[dict[str, Any]]:
        """계좌 목록 조회."""
        return self._get("/api/v1/accounts").get("result", [])

    def get_account_seq(self) -> int:
        """첫 번째 계좌의 accountSeq 반환 (캐싱)."""
        if self._account_seq is not None:
            return self._account_seq
        accounts = self.get_accounts()
        if not accounts:
            raise RuntimeError("토스증권에 등록된 계좌가 없습니다.")
        self._account_seq = int(accounts[0]["accountSeq"])
        return self._account_seq

    def get_holdings(self, symbol: str | None = None) -> dict[str, Any]:
        """보유 주식 조회. symbol 지정 시 해당 종목만."""
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        return self._get("/api/v1/holdings", params=params, account=True)

    def get_buying_power(self, currency: str = "USD") -> dict[str, Any]:
        """매수 가능 금액 조회 (KRW 또는 USD)."""
        return self._get(
            "/api/v1/buying-power",
            params={"currency": currency},
            account=True,
        ).get("result", {})

    def get_sellable_quantity(self, symbol: str) -> dict[str, Any]:
        """판매 가능 수량 조회."""
        return self._get(
            "/api/v1/sellable-quantity",
            params={"symbol": symbol},
            account=True,
        ).get("result", {})

    def get_commissions(self, market: str = "US") -> Any:
        """매매 수수료 조회. market: 'KR' 또는 'US'."""
        return self._get(
            "/api/v1/commissions",
            params={"market": market},
            account=True,
        ).get("result", [])

    # ── Order History (READ-ONLY — no create/modify/cancel) ──

    def get_orders(
        self, status: str = "CLOSED", cursor: str | None = None
    ) -> dict[str, Any]:
        """주문 목록 조회 (READ-ONLY).

        Args:
            status: 'OPEN' (미체결) 또는 'CLOSED' (종료)
            cursor: 페이지네이션 커서
        """
        params: dict[str, Any] = {"status": status}
        if cursor:
            params["cursor"] = cursor
        return self._get("/api/v1/orders", params=params, account=True)

    def get_order_detail(self, order_id: str) -> dict[str, Any]:
        """주문 상세 조회 (READ-ONLY)."""
        return self._get(f"/api/v1/orders/{order_id}", account=True)

    def get_trade_history(
        self, limit: int = 50, filled_only: bool = True
    ) -> list[dict[str, Any]]:
        """최근 체결 내역 조회 (CLOSED 주문에서 추출).

        Args:
            limit: 최대 건수
            filled_only: True면 FILLED 상태만 반환
        """
        trades: list[dict[str, Any]] = []
        cursor: str | None = None
        while len(trades) < limit:
            resp = self.get_orders(status="CLOSED", cursor=cursor)
            result = resp.get("result", {})
            orders = result.get("orders", [])
            for order in orders:
                if filled_only and order.get("status") != "FILLED":
                    continue
                trades.append(order)
                if len(trades) >= limit:
                    break
            if not result.get("hasNext"):
                break
            cursor = result.get("nextCursor")
        return trades

    # ── Lifecycle ─────────────────────────────────────────

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
