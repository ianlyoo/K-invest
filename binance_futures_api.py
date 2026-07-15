from __future__ import annotations

import socket
from typing import Any, Final, Literal

import httpx

BASE_URL: Final = "https://fapi.binance.com"
_LIMITS: Final = httpx.Limits(
    max_connections=200,
    max_keepalive_connections=40,
    keepalive_expiry=30.0,
)
_TIMEOUT: Final = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=10.0)
_SOCKET_OPTIONS: Final = [(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)]
KlinePriceType = Literal["last", "mark"]


class BinanceAPIError(Exception):
    def __init__(self, message: str, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def _create_client() -> httpx.Client:
    transport = httpx.HTTPTransport(
        http2=True,
        retries=3,
        limits=_LIMITS,
        socket_options=_SOCKET_OPTIONS,
    )
    return httpx.Client(
        base_url=BASE_URL,
        transport=transport,
        timeout=_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": "k-invest/2.0 read-only"},
    )


def _symbol(symbol: str) -> str:
    return symbol.replace("/", "").replace("-", "").strip().upper()


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _limit(value: int, low: int, high: int) -> int:
    return min(max(value, low), high)


def _kline(row: list[Any]) -> dict[str, Any]:
    return {
        "open_time": row[0],
        "open": _float(row[1]),
        "high": _float(row[2]),
        "low": _float(row[3]),
        "close": _float(row[4]),
        "volume": _float(row[5]),
        "close_time": row[6],
        "quote_volume": _float(row[7]),
        "trade_count": row[8],
        "taker_buy_base_volume": _float(row[9]),
        "taker_buy_quote_volume": _float(row[10]),
    }


class BinanceFuturesClient:
    def __init__(self, http: httpx.Client | None = None) -> None:
        self._http = http or _create_client()

    def _get_json(self, path: str, params: dict[str, str | int]) -> Any:
        try:
            response = self._http.get(path, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            body: Any
            try:
                body = exc.response.json()
            except ValueError:
                body = exc.response.text[:500]
            raise BinanceAPIError(
                f"Binance HTTP {exc.response.status_code}",
                status=exc.response.status_code,
                body=body,
            ) from exc
        except httpx.RequestError as exc:
            raise BinanceAPIError(f"Binance request failed: {exc}") from exc
        except ValueError as exc:
            raise BinanceAPIError("Binance returned invalid JSON") from exc

    def price_ticker(self, symbol: str) -> dict[str, Any]:
        payload = self._get_json("/fapi/v2/ticker/price", {"symbol": _symbol(symbol)})
        return {
            "symbol": payload.get("symbol"),
            "price": _float(payload.get("price")),
            "time": payload.get("time"),
            "source_endpoint": "/fapi/v2/ticker/price",
        }

    def mark_price(self, symbol: str) -> dict[str, Any]:
        payload = self._get_json("/fapi/v1/premiumIndex", {"symbol": _symbol(symbol)})
        raw_funding = _float(payload.get("lastFundingRate"))
        return {
            "symbol": payload.get("symbol"),
            "mark_price": _float(payload.get("markPrice")),
            "index_price": _float(payload.get("indexPrice")),
            "estimated_settle_price": _float(payload.get("estimatedSettlePrice")),
            "last_funding_rate": raw_funding,
            "last_funding_rate_pct": raw_funding * 100 if raw_funding is not None else None,
            "interest_rate": _float(payload.get("interestRate")),
            "next_funding_time": payload.get("nextFundingTime"),
            "time": payload.get("time"),
            "source_endpoint": "/fapi/v1/premiumIndex",
        }

    def funding_rates(self, symbol: str, limit: int = 10) -> list[dict[str, Any]]:
        payload = self._get_json(
            "/fapi/v1/fundingRate",
            {"symbol": _symbol(symbol), "limit": _limit(limit, 1, 1000)},
        )
        rows: list[dict[str, Any]] = []
        for item in payload:
            raw_rate = _float(item.get("fundingRate"))
            rows.append(
                {
                    "symbol": item.get("symbol"),
                    "funding_rate": raw_rate,
                    "funding_rate_pct": raw_rate * 100 if raw_rate is not None else None,
                    "funding_time": item.get("fundingTime"),
                    "mark_price": _float(item.get("markPrice")),
                }
            )
        return rows

    def open_interest(self, symbol: str) -> dict[str, Any]:
        payload = self._get_json("/fapi/v1/openInterest", {"symbol": _symbol(symbol)})
        return {
            "symbol": payload.get("symbol"),
            "open_interest": _float(payload.get("openInterest")),
            "time": payload.get("time"),
            "source_endpoint": "/fapi/v1/openInterest",
        }

    def open_interest_history(self, symbol: str, period: str = "1h", limit: int = 30) -> list[dict[str, Any]]:
        payload = self._get_json(
            "/futures/data/openInterestHist",
            {"symbol": _symbol(symbol), "period": period, "limit": _limit(limit, 1, 500)},
        )
        return [
            {
                "symbol": item.get("symbol"),
                "sum_open_interest": _float(item.get("sumOpenInterest")),
                "sum_open_interest_value": _float(item.get("sumOpenInterestValue")),
                "cmc_circulating_supply": _float(item.get("CMCCirculatingSupply")),
                "timestamp": item.get("timestamp"),
            }
            for item in payload
        ]

    def klines(
        self,
        symbol: str,
        interval: str = "1h",
        limit: int = 100,
        price_type: KlinePriceType = "last",
    ) -> list[dict[str, Any]]:
        path = "/fapi/v1/markPriceKlines" if price_type == "mark" else "/fapi/v1/klines"
        payload = self._get_json(
            path,
            {"symbol": _symbol(symbol), "interval": interval, "limit": _limit(limit, 1, 1500)},
        )
        return [_kline(row) for row in payload]

    def snapshot(self, symbol: str) -> dict[str, Any]:
        normalized = _symbol(symbol)
        quote = self.price_ticker(normalized)
        mark = self.mark_price(normalized)
        funding = self.funding_rates(normalized, limit=1)
        open_interest = self.open_interest(normalized)
        mark_price = mark.get("mark_price")
        oi = open_interest.get("open_interest")
        return {
            "symbol": normalized,
            "market": "binance_usd_m_futures",
            "auth_required": False,
            "quote": quote,
            "mark": mark,
            "funding_rates": funding,
            "open_interest": open_interest,
            "open_interest_notional": mark_price * oi if mark_price is not None and oi is not None else None,
        }
