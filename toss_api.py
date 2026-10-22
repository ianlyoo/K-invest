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
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from health_status import observe_provider_outcome
from kinvest_common import cache_dir as _default_cache_dir
from kinvest_common import retry_after_seconds
from portfolio_sources import (
    connection_guard,
    nav_read_remaining,
    nav_request_options,
    retry_delay,
)
from provider_pacing import RequestBudget

logger = logging.getLogger("k-invest.toss")

BASE_URL = "https://openapi.tossinvest.com"
TOKEN_CACHE_PATH = Path(
    os.environ.get(
        "TOSS_TOKEN_CACHE",
        str(_default_cache_dir() / "token.json"),
    )
)

# Process-wide group caps, conservative across accounts as well as workers.
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

_RATE_BUDGETS = {group: RequestBudget(1 / rate + .01) for group, rate in RATE_LIMITS.items()}


def _rate_group(path):
    if path == "/oauth2/token":
        return "AUTH"
    if path.startswith("/api/v1/orders"):
        return "ORDER_HISTORY"
    if path.startswith("/api/v1/stocks"):
        return "STOCK"
    if path.startswith("/api/v1/market-calendar") or path == "/api/v1/exchange-rate":
        return "MARKET_INFO"
    if path == "/api/v1/candles":
        return "MARKET_DATA_CHART"
    if path in ("/api/v1/prices", "/api/v1/orderbook", "/api/v1/trades", "/api/v1/price-limits"):
        return "MARKET_DATA"
    if path in ("/api/v1/holdings", "/api/v1/buying-power", "/api/v1/sellable-quantity", "/api/v1/commissions"):
        return "ASSET"
    return "ACCOUNT"  # Accounts and any future unclassified read use the lowest cap.


class TossAPIError(Exception):
    """Raised when the Toss API returns an error."""

    def __init__(self, status: int, body: Any, retry_after: int | None = None):
        self.status = status
        self.body = body
        self.retry_after_seconds = retry_after
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


class HistoryRows(list):
    """List-compatible history carrying paging completeness and safe failure data."""
    def __init__(self, rows=(), *, truncated=False, reason=None, fetch_error=None):
        super().__init__(rows)
        self.truncated = truncated
        self.complete = not truncated
        self.truncation_reason = reason
        self.fetch_error = fetch_error


_MASK_RETRY_SECONDS = 60.0


def _mask_account_no(value: Any) -> str | None:
    digits = "".join(c for c in str(value or "") if c.isdigit())
    return "••••" + digits[-4:] if len(digits) >= 4 else None


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
        self._account_masked: str | None = None
        self._account_masked_failed_at: float = float("-inf")
        self._mask_lock = threading.Lock()
        self._token_cache_path = token_cache_path or TOKEN_CACHE_PATH
        self._http = httpx.Client(base_url=BASE_URL, timeout=15.0, headers={"Accept-Encoding": "identity"})

    def _send(self, method, path, **kwargs):
        # Refreshes, 401 resends, 429 retries and ordinary GETs all pass here.
        # Keep NAV's pre-existing deadline effective during admission waits.
        with _RATE_BUDGETS[_rate_group(path)].request(remaining=nav_read_remaining):
            return getattr(self._http, method)(path, **kwargs, **nav_request_options())

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
        except Exception:
            logger.warning("Toss token cache read failed")
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
        except Exception:
            logger.warning("Toss token cache write failed")

    def _ensure_token(self) -> str:
        """Get a valid access token, refreshing if needed."""
        if self._token and time.time() < self._token_expires - 120:
            return self._token

        if self._load_cached_token():
            token = self._token
            if token is not None:
                return token

        logger.info("Requesting new Toss OAuth token")
        resp = self._send(
            "post", "/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            raise TossAPIError(resp.status_code, self._safe_json(resp), retry_after=retry_after_seconds(resp.headers.get("Retry-After")))

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
        with connection_guard("toss", getattr(self, "client_id", id(self))):
            try:
                result = self._get_unlocked(path, params, account)
            except Exception as exc:
                observe_provider_outcome("toss", exc)
                raise
            observe_provider_outcome("toss")
            return result

    def _get_unlocked(self, path, params, account):
        """Make an authenticated GET request. Returns full parsed JSON."""
        token = self._ensure_token()
        headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
        if account:
            headers["X-Tossinvest-Account"] = str(self.get_account_seq())

        resp = self._send("get", path, params=params, headers=headers)
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
            resp = self._send("get", path, params=params, headers=headers)

        remaining = 60.0
        for attempt in range(2):
            if resp.status_code != 429:
                break
            requested = retry_after_seconds(resp.headers.get("Retry-After"))
            delay = retry_delay(attempt, requested, remaining)
            budget = nav_read_remaining()
            if budget is not None and delay is not None and delay >= budget:
                raise TimeoutError('NAV evidence budget exceeded')
            if delay is None:
                break
            time.sleep(delay)
            remaining -= delay
            resp = self._send("get", path, params=params, headers=headers)

        if resp.status_code != 200:
            raise TossAPIError(resp.status_code, self._safe_json(resp), retry_after=retry_after_seconds(resp.headers.get("Retry-After")))

        return resp.json()

    # ── Market Data ───────────────────────────────────────

    def get_prices(self, symbols: str | list[str]) -> list[dict[str, Any]]:
        """현재가 조회. symbols: 단일 심볼 또는 리스트/콤마 구분 문자열."""
        if isinstance(symbols, list):
            symbols = ",".join(symbols)
        resp = self._get("/api/v1/prices", params={"symbols": symbols})
        return resp.get("result", [])

    def get_prices_evidence(self, symbols: list[str]) -> dict[str, Any]:
        """Internal NAV view of the same read, including completeness metadata."""
        return self._get("/api/v1/prices", params={"symbols": ",".join(symbols)})

    def get_mark_quotes(self, requested: dict[str, str], chunk: int = 20) -> dict[str, dict[str, Any]]:
        """Validated timed quotes for canonical holding marks: {symbol: {price, currency, priced_at, source}}.

        `requested` maps symbol -> currency (KRW/USD only; Toss quotes no other market).
        Each chunk goes through the strict prices validation; a failed or invalid
        chunk yields per-symbol reason codes, never a price. A symbol's own codes
        (missing, untimed, bad price, other currency, duplicate) reject only that
        symbol. A response-level problem (a row bound to no requested symbol, or a
        code no requested symbol explains) drops the whole chunk: a symbol without
        its own codes gets the response's codes.
        """
        from toss_nav_validation import canonical_symbol, validate_toss_response

        wanted = {s: c for s, c in requested.items() if c in ("KRW", "USD")}
        quotes: dict[str, dict[str, Any]] = {s: {"reason_codes": ["UNSUPPORTED_QUOTE_MARKET"]}
                                              for s, c in requested.items() if s not in wanted}
        symbols = sorted(wanted)
        missing = {"reason_codes": ["MISSING_TOSS_QUOTE"]}
        for start in range(0, len(symbols), chunk):
            part = {s: wanted[s] for s in symbols[start:start + chunk]}
            try:
                evidence = self.get_prices_evidence(sorted(part))
                validated = validate_toss_response("prices", evidence, symbols=part)
                rows = validated.quotes
                own = {code for symbol in part for code in (rows.get(symbol) or missing).get("reason_codes", [])}
                # The validator also lists per-symbol codes at response level; an unrequested or
                # symbol-less row carries a code (CURRENCY_BINDING_MISMATCH / INVALID_SOURCE_RESPONSE)
                # a requested row may share, so those rows are read here, not inferred from the codes.
                envelope = getattr(evidence, "nav_envelope", evidence)
                result = envelope.get("result") if isinstance(envelope, dict) else envelope
                bound = {canonical_symbol(symbol) for symbol in part}
                unbound = []
                for row in result if isinstance(result, list) else []:
                    symbol = canonical_symbol(row.get("symbol")) if isinstance(row, dict) else None
                    if symbol not in bound:
                        unbound.append("INVALID_SOURCE_RESPONSE" if symbol is None else "CURRENCY_BINDING_MISMATCH")
                batch_reasons = list(dict.fromkeys(
                    [*(code for code in validated.reason_codes if code not in own), *unbound]))
            except Exception:  # noqa: BLE001 — an unavailable quote leaves the broker mark labelled
                rows, batch_reasons = {}, []
            for symbol in part:
                row = rows.get(symbol) or missing
                if row.get("reason_codes") or batch_reasons:
                    quotes[symbol] = {"reason_codes": list(row.get("reason_codes") or batch_reasons)}
                    continue
                try:
                    price = float(row["last_price"])
                except (KeyError, TypeError, ValueError):
                    quotes[symbol] = {"reason_codes": ["INVALID_QUOTE_PRICE"]}
                    continue
                quotes[symbol] = {"price": price, "currency": row.get("currency"), "priced_at": row.get("priced_at"),
                                  "source": "toss:prices"}
        return quotes

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
        start_date: str | None = None,
        end_date: str | None = None,
        *,
        _validate_page=None,
    ) -> dict[str, Any]:
        """캔들 차트 조회.

        Args:
            symbol: 종목 심볼
            interval: '1d' (일봉) 또는 '1m' (1분봉)
            count: 봉 개수 (최대 200)
            before: Provider cursor; local filtering includes this timestamp.
            adjusted: 수정주가 적용 여부
            start_date/end_date: Inclusive YYYY-MM-DD candle-local labels.

        One bounded page per call; merge pages by timestamp, ascending. Page
        exhaustion does not verify historical coverage. Omitted paging inputs
        preserve the original raw provider payload, including order and fields.
        """
        from datetime import date, datetime, timedelta, timezone
        from datetime import time as dt_time

        paging = any(value is not None for value in (before, start_date, end_date))
        start = end = cutoff = None
        if paging:
            if type(count) is not int or not 1 <= count <= 200:
                raise ValueError("count must be between 1 and 200")

            def parse_date(value: str | None) -> date | None:
                if value is None:
                    return None
                parsed = date.fromisoformat(value)
                if parsed.isoformat() != value:
                    raise ValueError("Use ISO YYYY-MM-DD dates")
                return parsed

            def parse_timestamp(value: str) -> datetime:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError("Candle timestamps and before must include a timezone")
                return parsed

            start, end = parse_date(start_date), parse_date(end_date)
            if start and end and start > end:
                raise ValueError("start_date is after end_date")
            cutoff = parse_timestamp(before) if before is not None else None
            if end:
                # Conservative UTC anchor covers every possible local end-date
                # label; it is not an assertion of the exchange timezone/session.
                anchor = datetime.combine(end + timedelta(days=2), dt_time(), timezone.utc)
                if cutoff is None or anchor < cutoff:
                    before, cutoff = anchor.isoformat(), anchor
        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "count": count,
            "adjusted": str(adjusted).lower(),
        }
        if before:
            params["before"] = before
        envelope = self._get("/api/v1/candles", params=params)
        if _validate_page is not None:
            _validate_page(envelope, params)
        result = envelope.get("result", {})
        if not paging:
            return result
        if not isinstance(result, dict) or not isinstance(result.get("candles"), list):
            raise ValueError("Provider candle page has no candles list")
        # Work on a copy; never mutate raw provider data/cache or old response keys.
        data = dict(result)
        rows: dict[datetime, dict[str, Any]] = {}
        labels = []
        reasons = []
        for row in result["candles"]:
            try:
                stamp = parse_timestamp(row["timestamp"])
            except (KeyError, TypeError, ValueError, AttributeError):
                reasons.append("CANDLE_TIMESTAMP_UNAVAILABLE")
                continue
            labels.append(stamp.date())
            if cutoff and stamp > cutoff:
                continue
            if start and stamp.date() < start or end and stamp.date() > end:
                continue
            if stamp in rows and rows[stamp] != row:
                reasons.append("CONFLICTING_CANDLE_TIMESTAMP")
            rows.setdefault(stamp, row)
        data["candles"] = [rows[key] for key in sorted(rows)]
        next_cursor = result.get("nextBefore")
        # A missing cursor key is unknown, not a verified exhausted history.
        exhausted = "nextBefore" in result and next_cursor is None
        reached_start = bool(start and labels and min(labels) <= start)
        if next_cursor:
            try:
                next_stamp = parse_timestamp(next_cursor)
                if cutoff and next_stamp >= cutoff:
                    reasons.append("CANDLE_CURSOR_NOT_ADVANCING")
            except (TypeError, ValueError, AttributeError):
                reasons.append("CANDLE_CURSOR_INVALID")
        if "nextBefore" not in result:
            reasons.append("CANDLE_CURSOR_UNAVAILABLE")
        reasons = list(dict.fromkeys(reasons))
        complete = None if reasons else exhausted or reached_start
        data.update({
            "returned_range": {
                "start": data["candles"][0]["timestamp"] if rows else None,
                "end": data["candles"][-1]["timestamp"] if rows else None,
            },
            "next_cursor": None if complete else next_cursor,
            "complete": complete, "limit": count,
            "result_status": "partial" if reasons or not complete else "complete" if rows else "empty",
            "reason_codes": reasons,
            "source_metadata": {
                "source": "toss", "requested_adjustment": adjusted,
                "applied_adjustment": None, "split_adjustment": None,
                "dividend_adjustment": None, "total_return_verified": None,
                "adjustment_status": "unavailable",
                "adjustment_reason_codes": ["PROVIDER_ADJUSTMENT_UNVERIFIED"],
                "exchange_timezone": None, "session": None,
                "boundary_policy": "before_inclusive_local_filter; start_date/end_date_inclusive_local_label",
                "provider_before_boundary": "unverified",
                "complete_scope": "paging_exhausted_or_start_label_reached; historical_coverage_unverified",
                "merge_policy": "timestamp_ascending_deduplicate; first_seen_on_conflict",
            },
        })
        return data

    # ── Stock Info ────────────────────────────────────────

    def get_stock_info(self, symbols: str | list[str]) -> list[dict[str, Any]]:
        """종목 기본 정보 조회."""
        if isinstance(symbols, list):
            symbols = ",".join(symbols)
        return self._get("/api/v1/stocks", params={"symbols": symbols}).get("result", [])

    def get_stock_info_evidence(self, symbols: list[str]) -> dict[str, Any]:
        """Retain the existing read-only stock-info envelope for strict binding."""
        return self._get('/api/v1/stocks', params={'symbols': ','.join(symbols)})

    def get_stock_warnings(self, symbol: str) -> list[dict[str, Any]]:
        """매수 유의사항 조회."""
        return self._get(f"/api/v1/stocks/{symbol}/warnings").get("result", [])

    # ── Market Info ───────────────────────────────────────

    def get_exchange_rate(self, base: str = "USD", quote: str = "KRW") -> dict[str, Any]:
        """환율 조회."""
        from toss_nav_validation import EnvelopeResult

        response = self._get(
            "/api/v1/exchange-rate",
            params={"baseCurrency": base, "quoteCurrency": quote},
        )
        result = response.get("result", {})
        return EnvelopeResult(response) if isinstance(result, dict) else result

    def get_market_calendar(self, country: str = "US") -> dict[str, Any]:
        """시장 운영 시간 조회. country: 'KR' 또는 'US'."""
        return self._get(f"/api/v1/market-calendar/{country}").get("result", {})

    def get_market_calendar_evidence(self, country: str = "US") -> dict[str, Any]:
        """Read the same calendar GET with its envelope for internal NAV checks."""
        return self._get(f"/api/v1/market-calendar/{country}")

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
        self._account_masked = _mask_account_no(accounts[0].get("accountNo"))
        return self._account_seq

    def get_account_masked(self) -> str | None:
        """Masked last 4 ("••••NNNN") of the account that account-scoped reads use.

        The full accountNo never leaves this method; None when it is unusable.
        """
        if self._account_masked is not None:
            return self._account_masked
        with self._mask_lock:  # One lookup at a time, so the failure backoff holds under concurrency.
            return self._lookup_account_masked()

    def _lookup_account_masked(self) -> str | None:
        if self._account_masked is not None:
            return self._account_masked
        now = time.monotonic()
        if now - self._account_masked_failed_at < _MASK_RETRY_SECONDS:
            return None  # Recent failure: don't hit upstream on every discovery call.
        try:
            if self._account_seq is None:
                self.get_account_seq()  # Sets both fields from one response.
            else:
                # Never reset _account_seq: concurrent account reads rely on it.
                accounts = self.get_accounts()
                if accounts and int(accounts[0]["accountSeq"]) == self._account_seq:
                    self._account_masked = _mask_account_no(accounts[0].get("accountNo"))
        except Exception:
            self._account_masked_failed_at = now
            raise
        if self._account_masked is None:
            self._account_masked_failed_at = now
        return self._account_masked

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

    def get_buying_power_evidence(self, currency: str = "USD") -> dict[str, Any]:
        """Internal NAV view; legacy buying-power output remains result-only."""
        return self._get("/api/v1/buying-power", params={"currency": currency}, account=True)

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
        self,
        status: str = "CLOSED",
        cursor: str | None = None,
        symbol: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """주문 목록 조회 (READ-ONLY).

        Args:
            status: 'OPEN' (미체결) 또는 'CLOSED' (종료)
            cursor: 페이지네이션 커서
            symbol: 종목 심볼 필터
            date_from: 조회 시작일 (YYYY-MM-DD)
            date_to: 조회 종료일 (YYYY-MM-DD)
            limit: 페이지 크기
        """
        params: dict[str, Any] = {"status": status}
        if cursor:
            params["cursor"] = cursor
        if symbol:
            params["symbol"] = symbol
        if date_from:
            params["from"] = date_from
        if date_to:
            params["to"] = date_to
        if limit:
            params["limit"] = limit
        return self._get("/api/v1/orders", params=params, account=True)

    def iter_closed_orders(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        symbol: str | None = None,
        max_orders: int = 2000,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """Follow the CLOSED order cursor and return every order in the window.

        Args:
            date_from: 조회 시작일 (YYYY-MM-DD)
            date_to: 조회 종료일 (YYYY-MM-DD)
            symbol: 종목 심볼 필터
            max_orders: 안전 상한 (초과 시 중단)
            page_size: 요청당 건수
        """
        orders: list[dict[str, Any]] = []
        seen_orders: dict[tuple, int] = {}
        cursor: str | None = None
        seen_cursors: set[str] = set()
        more = False
        reason = None
        fetch_error = None
        # Row overlap cannot evade the safety cap indefinitely.
        for _ in range(100):
            if len(orders) >= max_orders:
                break
            try:
                resp = self.get_orders(
                    status="CLOSED", cursor=cursor, symbol=symbol,
                    date_from=date_from, date_to=date_to, limit=page_size,
                )
            except TossAPIError as exc:
                fetch_error, more, reason = exc, True, "page_failed"
                break
            result = resp.get("result", {})
            page = result.get("orders", [])
            for row in page:
                native = row.get("orderId")
                key = (native, str(row.get("orderedAt", ""))[:10], row.get("symbol"), row.get("currency"))
                if native and key in seen_orders:
                    orders[seen_orders[key]] = row
                else:
                    if native:
                        seen_orders[key] = len(orders)
                    orders.append(row)
            more = bool(result.get("hasNext"))
            if not more:
                break
            cursor = result.get("nextCursor")
            if not page or not cursor or cursor in seen_cursors:
                reason = "paging_inconsistent"
                break
            seen_cursors.add(cursor)
        else:
            more, reason = True, "page_limit"
        truncated = more or len(orders) > max_orders
        return HistoryRows(orders[:max_orders], truncated=truncated,
                           reason=reason or ("row_limit" if truncated else None), fetch_error=fetch_error)

    def get_order_detail(self, order_id: str) -> dict[str, Any]:
        """주문 상세 조회 (READ-ONLY)."""
        return self._get(f"/api/v1/orders/{order_id}", account=True)

    def get_fill_history_evidence(self, start: str, end: str, *, page_size: int = 100,
                                  max_orders: int = 2000) -> HistoryRows:
        """Internal ledger read: validate all OPEN/CLOSED pages, then filter fills.

        No creation-date filter: an old order can fill within this window. The
        bounded full scan must exhaust; a cap, overlap, malformed page or failed
        request invalidates coverage. API-unsupported order types remain outside
        the provider's visibility, so this evidence alone never certifies returns.
        ``start``/``end`` are inclusive KST calendar dates, cutoff is read start.
        """
        from datetime import date, datetime, timedelta, timezone
        from zoneinfo import ZoneInfo

        from toss_nav_validation import validate_toss_response

        if not 1 <= page_size <= 100 or max_orders < 1:
            raise ValueError('INVALID_FILL_REQUEST')
        zone = ZoneInfo('Asia/Seoul')
        lo = datetime.combine(date.fromisoformat(start), datetime.min.time(), zone)
        hi = datetime.combine(date.fromisoformat(end) + timedelta(days=1), datetime.min.time(), zone)
        cutoff = datetime.now(timezone.utc)
        if lo >= hi:
            raise ValueError('INVALID_FILL_REQUEST')
        if lo > cutoff:
            raise ValueError('FILL_WINDOW_NOT_OBSERVED')
        rows, seen, reasons = [], set(), []
        for group in ('OPEN', 'CLOSED'):
            cursor, cursors = None, set()
            for _ in range(100):
                try:
                    payload = self.get_orders(status=group, cursor=cursor, limit=page_size)
                    valid = validate_toss_response('orders', payload, order_request={
                        'status': group, 'limit': page_size, 'seen_ids': seen, 'observed_at': cutoff.isoformat()})
                except Exception:
                    reasons.append('FILL_PAGE_FAILED')
                    break
                if valid.value is None:
                    reasons.extend(valid.reason_codes)
                    break
                result = valid.value
                seen.update(row['orderId'] for row in result['orders'])
                rows.extend(result['orders'])
                if len(rows) > max_orders:
                    reasons.append('FILL_ROW_LIMIT')
                    break
                if not result['hasNext']:
                    break
                cursor = result['nextCursor']
                if cursor in cursors:
                    reasons.append('FILL_PAGING_INCONSISTENT')
                    break
                cursors.add(cursor)
            else:
                reasons.append('FILL_PAGE_LIMIT')
            if reasons:
                break
        selected = []
        if not reasons:
            for row in rows:
                stamp = row['execution']['filledAt']
                if stamp and lo <= datetime.fromisoformat(stamp.replace('Z', '+00:00')) < hi:
                    selected.append(row)
        output = HistoryRows(selected, truncated=bool(reasons), reason='FILL_EVIDENCE_INCOMPLETE' if reasons else None)
        output.fill_contract = 'toss-fills-v1'
        output.fill_reasons = sorted(set(reasons))
        output.fill_start = lo.astimezone(timezone.utc).isoformat()
        # The date filter is [lo, hi); never certify a fill at hi itself.
        output.fill_end = min(hi - timedelta(microseconds=1), cutoff).astimezone(timezone.utc).isoformat()
        return output

    def get_trade_history(
        self, limit: int = 50, filled_only: bool = True
    ) -> list[dict[str, Any]]:
        """최근 체결 내역 조회 (CLOSED 주문에서 추출).

        Args:
            limit: 최대 건수
            filled_only: True면 FILLED 상태만 반환
        """
        if not 1 <= limit <= 2000:
            raise ValueError("limit must be between 1 and 2000")
        trades: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        more, reason = False, None
        # Bound even pages containing only unfilled orders.
        for _ in range(100):
            resp = self.get_orders(status="CLOSED", cursor=cursor)
            result = resp.get("result", {})
            orders = result.get("orders", [])
            selected = [o for o in orders if not filled_only or o.get("status") == "FILLED"]
            trades.extend(selected)
            more = bool(result.get("hasNext")) or len(trades) > limit
            if len(trades) >= limit or not result.get("hasNext"):
                break
            cursor = result.get("nextCursor")
            if not orders or not cursor or cursor in seen:
                more, reason = True, "paging_inconsistent"
                break
            seen.add(cursor)
        else:
            more, reason = True, "page_limit"
        return HistoryRows(trades[:limit], truncated=more,
                           reason=reason or ("row_limit" if more else None))

    # ── Lifecycle ─────────────────────────────────────────

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
