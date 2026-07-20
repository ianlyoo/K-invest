#!/usr/bin/env python3
"""K-invest: Toss Securities + KIS + SEC EDGAR + margin-ta Read-Only MCP Server.

Remote MCP server (Streamable HTTP) exposing read-only financial analysis tools:
  - Toss Securities Open API: market data, stock info, account/holdings, trade history
  - Korea Investment Securities (KIS): domestic/overseas balance, quotes, trade history
  - SEC EDGAR: financial statements (CompanyFacts), insider trades (Form 4)
  - yfinance: financials, key metrics, historical OHLCV
  - margin-ta: 43-indicator technical analysis, entry strategies, stock scanner

NO order creation, modification, or cancellation endpoints are exposed.
All account/trading data is READ-ONLY.

Auth:
  - MCP layer: Static Bearer Token (MCP_AUTH_TOKEN env var)
  - Toss API : OAuth 2.0 Client Credentials (TOSS_CLIENT_ID / TOSS_CLIENT_SECRET)
  - KIS API  : OAuth 2.0 (KIS_APP_KEY / KIS_APP_SECRET, or KIS_ENV_FILE)
  - SEC EDGAR: Public API (SEC_USER_AGENT header)

Transport: Streamable HTTP (https://modelcontextprotocol.io/specification/2025-06-18)
"""

from __future__ import annotations

import logging
import os
import secrets
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yfinance as yf

# Ensure local imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from kinvest_common import NotConfiguredError, apply_env_file
from toss_api import TossAPIError, TossClient
from toss_accounts import TossAccountRegistry
from kis_api import KISAPIError, KISClient
from sec_api import SECClient
from yfinance_api import YFinanceClient
from yfinance_consensus import risk_free_rate
from margin_ta_runner import MarginTARunner, _summarize_analysis
from binance_futures_api import BinanceAPIError, BinanceFuturesClient

# ── Logging ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("k-invest")
SERVER_VERSION = "2.2.0"

# ── Config ──────────────────────────────────────────────

# Load .env for clone-&-run usage (setdefault semantics: real env vars win).
apply_env_file(Path.cwd() / ".env")
apply_env_file(Path(__file__).resolve().parent / ".env")

MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()
if not MCP_AUTH_TOKEN:
    logger.warning(
        "MCP_AUTH_TOKEN not set — server will refuse to start (main() exits). "
        "Set it in .env to enable access."
    )

# Public URL where the MCP server is reachable (for OAuth protected resource metadata)
_MCP_PUBLIC_URL = os.environ.get("MCP_PUBLIC_URL", "http://127.0.0.1:8100")

# Extract host from public URL for DNS rebinding protection allowlist
_MCP_HOST = urlparse(_MCP_PUBLIC_URL).hostname or "127.0.0.1"

# ── Static Token Verifier (MCP auth layer) ──────────────


class StaticTokenVerifier(TokenVerifier):
    """Verify a static bearer token from the MCP_AUTH_TOKEN env var."""

    def __init__(self, token: str):
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not self._token:
            return None
        if secrets.compare_digest(token, self._token):
            return AccessToken(
                token=token,
                client_id="k-invest-client",
                scopes=[],
            )
        return None


# ── MCP Server ──────────────────────────────────────────

mcp = FastMCP(
    name="K-invest",
    instructions=(
        "Read-only stock analysis MCP server. "
        "Provides market data (Toss, KIS, yfinance), financial statements (SEC EDGAR, yfinance), "
        "insider trades (SEC Form 4), account/holdings/trade history (Toss, KIS), "
        "43-indicator technical analysis with entry strategies (margin-ta), "
        "and stock scanning. "
        "Supports Korean (KRX/NXT), US, and Japanese markets. "
        "No order creation or trading — ALL endpoints are read-only."
    ),
    token_verifier=StaticTokenVerifier(MCP_AUTH_TOKEN) if MCP_AUTH_TOKEN else None,
    auth=AuthSettings(
        issuer_url=_MCP_PUBLIC_URL,
        resource_server_url=_MCP_PUBLIC_URL,
    ) if MCP_AUTH_TOKEN else None,
    host="127.0.0.1",
    port=8100,
    streamable_http_path="/mcp",
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            _MCP_HOST,
            f"{_MCP_HOST}:*",
            "127.0.0.1",
            "127.0.0.1:*",
            "localhost",
            "localhost:*",
        ],
        allowed_origins=[_MCP_PUBLIC_URL, "http://127.0.0.1:*", "http://localhost:*"],
    ),
)
mcp._mcp_server.version = SERVER_VERSION

# ── Client Singletons (lazy) ────────────────────────────

_toss_client: TossClient | None = None
_toss_accounts: TossAccountRegistry | None = None
_kis_client: KISClient | None = None
_sec_client: SECClient | None = None
_yf_client: YFinanceClient | None = None
_mt_runner: MarginTARunner | None = None
_binance_client: BinanceFuturesClient | None = None


def _get_toss_registry() -> TossAccountRegistry:
    global _toss_accounts
    if _toss_accounts is None:
        _toss_accounts = TossAccountRegistry.from_sources()
    return _toss_accounts


def _get_toss(account: str = "primary") -> TossClient:
    global _toss_client
    if account and account != "primary":
        return _get_toss_registry().client_for(account)
    if _toss_client is None:
        _toss_client = _get_toss_registry().client_for("primary")
    return _toss_client


def _for_toss_accounts(account: str, operation: Any) -> Any:
    normalized = (account or "primary").strip().lower()
    registry = _get_toss_registry()
    if normalized != "all":
        return operation(registry.client_for(normalized))
    results: list[dict[str, Any]] = []
    for label in registry.account_labels():
        try:
            results.append({"account": label, "data": operation(registry.client_for(label)), "error": None})
        except Exception as e:
            results.append({"account": label, "data": None, "error": _format_error(e).get("error")})
    return {"accounts": results}


def _get_kis() -> KISClient:
    global _kis_client
    if _kis_client is None:
        _kis_client = KISClient()
    return _kis_client


def _get_sec() -> SECClient:
    global _sec_client
    if _sec_client is None:
        _sec_client = SECClient()
    return _sec_client


def _get_yf() -> YFinanceClient:
    global _yf_client
    if _yf_client is None:
        _yf_client = YFinanceClient()
    return _yf_client


def _get_mt() -> MarginTARunner:
    global _mt_runner
    if _mt_runner is None:
        _mt_runner = MarginTARunner()
    return _mt_runner


def _get_binance() -> BinanceFuturesClient:
    global _binance_client
    if _binance_client is None:
        _binance_client = BinanceFuturesClient()
    return _binance_client


def _ok(data: Any, provider: str = "", **meta: Any) -> dict[str, Any]:
    payload_meta = {
        "server_version": SERVER_VERSION,
        "provider": provider or None,
        "cache_age_seconds": meta.pop("cache_age_seconds", 0),
    }
    payload_meta.update({k: v for k, v in meta.items() if v is not None})
    return {"ok": True, "data": data, "error": None, "meta": payload_meta}


def _fail(
    code: str,
    message: str,
    provider: str = "",
    upstream_status: int | None = None,
    retryable: bool = False,
    details: Any = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "data": None,
        "error": {
            "code": code,
            "message": message,
            "provider": provider or None,
            "upstream_status": upstream_status,
            "retryable": retryable,
            "details": details,
        },
        "meta": {"server_version": SERVER_VERSION},
    }


def _format_error(e: Exception) -> dict[str, Any]:
    if isinstance(e, NotConfiguredError):
        return _fail(
            f"{e.provider.upper()}_NOT_CONFIGURED", str(e), provider=e.provider, retryable=False
        )
    if isinstance(e, TossAPIError):
        return _fail("TOSS_API_ERROR", str(e), provider="toss", upstream_status=e.status, details=e.body)
    if isinstance(e, KISAPIError):
        return _fail("KIS_API_ERROR", str(e), provider="kis", upstream_status=e.status, details=e.body)
    if isinstance(e, BinanceAPIError):
        return _fail("BINANCE_API_ERROR", str(e), provider="binance", upstream_status=e.status, details=e.body)
    return _fail(type(e).__name__.upper(), str(e), retryable=False)


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").replace("%", ""))
        except ValueError:
            return None
    return None


def _amount_from(value: Any, preferred_currency: str = "") -> tuple[float | None, str | None]:
    if isinstance(value, dict):
        amount = value.get("amount") if isinstance(value.get("amount"), dict) else value
        currency = (preferred_currency or value.get("currency") or value.get("currencyCode") or "").upper()
        if currency and currency.lower() in amount:
            num = _to_float(amount.get(currency.lower()))
            if num is not None:
                return num, currency
        for key, cur in [("krw", "KRW"), ("usd", "USD"), ("jpy", "JPY"), ("value", currency or None)]:
            num = _to_float(amount.get(key))
            if num is not None:
                return num, cur
    return _to_float(value), preferred_currency.upper() or None


def _walk_position_items(payload: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(payload, list):
        for item in payload:
            found.extend(_walk_position_items(item))
    elif isinstance(payload, dict):
        symbol = payload.get("symbol") or payload.get("pdno") or payload.get("prdt_cd") or payload.get("ovrs_pdno") or payload.get("stock_code")
        if symbol:
            found.append(payload)
        for key in ("result", "data", "output", "items", "stocks", "holdings", "output1", "balances", "positions"):
            if key in payload:
                found.extend(_walk_position_items(payload[key]))
    return found


def _position_from_item(source: str, item: dict[str, Any]) -> dict[str, Any] | None:
    symbol = str(item.get("symbol") or item.get("pdno") or item.get("prdt_cd") or item.get("ovrs_pdno") or item.get("stock_code") or "").strip()
    if not symbol:
        return None
    name = item.get("name") or item.get("prdt_name") or item.get("stock_name") or item.get("ovrs_item_name")
    currency = str(item.get("currency") or item.get("crcy_cd") or item.get("tr_crcy_cd") or ("KRW" if symbol.isdigit() else "USD")).upper()
    value = None
    value_currency: str | None = None
    for key in ("marketValue", "market_value", "evlu_amt", "ovrs_stck_evlu_amt", "frcr_evlu_amt2", "asset_value", "evalAmount"):
        if key in item:
            value, value_currency = _amount_from(item[key], currency)
            if value is not None:
                break
    quantity = None
    for key in ("quantity", "qty", "hldg_qty", "ord_psbl_qty", "ovrs_cblc_qty"):
        quantity = _to_float(item.get(key))
        if quantity is not None:
            break
    if value is None and quantity is not None:
        for key in ("lastPrice", "current_price", "prpr", "ovrs_now_pric", "now_price"):
            price = _to_float(item.get(key))
            if price is not None:
                value = quantity * price
                value_currency = currency
                break
    if value is None or value <= 0:
        return None
    return {
        "source": source,
        "symbol": symbol.upper(),
        "name": name,
        "currency": value_currency or currency,
        "market_value": value,
        "quantity": quantity,
    }


def _risk_from_positions(positions: list[dict[str, Any]]) -> dict[str, Any]:
    by_currency: dict[str, float] = {}
    by_symbol: dict[str, dict[str, Any]] = {}
    for pos in positions:
        currency = str(pos.get("currency") or "UNKNOWN").upper()
        value = float(pos["market_value"])
        by_currency[currency] = by_currency.get(currency, 0.0) + value
        key = f"{pos['symbol']}:{currency}"
        agg = by_symbol.setdefault(key, {**pos, "market_value": 0.0, "sources": []})
        agg["market_value"] += value
        if pos.get("source") not in agg["sources"]:
            agg["sources"].append(pos.get("source"))
    total_unconverted = sum(by_currency.values())
    top_positions = sorted(by_symbol.values(), key=lambda p: p["market_value"], reverse=True)
    for pos in top_positions:
        cur_total = by_currency.get(str(pos.get("currency") or "UNKNOWN"), 0.0)
        pos["weight_pct_in_currency"] = round(pos["market_value"] / cur_total * 100, 2) if cur_total else None
    weights = [p["market_value"] / by_currency.get(str(p.get("currency") or "UNKNOWN"), 1.0) for p in top_positions]
    hhi = round(sum(w * w for w in weights), 4) if weights else None
    top1 = top_positions[0]["weight_pct_in_currency"] if top_positions else None
    return {
        "position_count": len(top_positions),
        "currency_exposure": {k: round(v, 2) for k, v in sorted(by_currency.items())},
        "top_positions": top_positions[:10],
        "concentration": {
            "top1_weight_pct_in_currency": top1,
            "hhi_by_currency_bucket": hhi,
            "risk_level": "high" if (top1 or 0) >= 35 or (hhi or 0) >= 0.25 else "medium" if (top1 or 0) >= 20 or (hhi or 0) >= 0.15 else "low",
        },
        "methodology": {
            "fx_conversion": "none",
            "note": "Exposure and concentration are grouped by provider-reported currency; KRW/USD/JPY are not converted into a single base currency.",
        },
        "total_unconverted_value": round(total_unconverted, 2),
    }


def _summarize_holdings_sector_exposure(
    positions: list[dict[str, Any]], market_risk: dict[str, Any]
) -> dict[str, Any]:
    """Aggregate holdings by sector ETF and pair each with its market-risk score."""
    sector_risk = market_risk.get("sector_risk", {})
    by_sector: dict[str, dict[str, Any]] = {}
    total = sum(float(p.get("market_value") or 0) for p in positions) or 1.0
    for p in positions:
        etf = p.get("sector_etf")
        if not etf:
            continue
        agg = by_sector.setdefault(etf, {"value": 0.0, "symbols": []})
        agg["value"] += float(p.get("market_value") or 0)
        agg["symbols"].append(p.get("symbol"))
    for etf, agg in by_sector.items():
        sr = sector_risk.get(etf, {})
        agg["weight_pct"] = round(agg["value"] / total * 100, 1)
        agg["risk_score"] = sr.get("score")
        agg["risk_level"] = sr.get("level")
        agg.pop("value", None)
    ranked = sorted(
        by_sector.items(),
        key=lambda x: (x[1].get("risk_score") or 0) * x[1]["weight_pct"],
        reverse=True,
    )
    headline = ""
    if ranked:
        top_etf, top = ranked[0]
        headline = (
            f"보유 비중 {top['weight_pct']}%가 {top_etf}(위험 {top.get('risk_score')}/"
            f"{top.get('risk_level')})에 노출"
        )
    return {"by_sector": by_sector, "headline": headline}


# ════════════════════════════════════════════════════════════════
# Toss Securities Market Data Tools (9)
# ════════════════════════════════════════════════════════════════


@mcp.tool()
def get_quote(symbol: str) -> dict[str, Any]:
    """Get current price quote for a stock symbol (Toss Securities).

    Args:
        symbol: Stock symbol (e.g., "AAPL" for US, "005930" for Korean Samsung Electronics)

    Returns:
        Structured JSON envelope with current price data including last price, change, volume, etc.
    """
    try:
        data = _get_toss().get_prices(symbol)
        return _ok(data)
    except Exception as e:
        return _format_error(e)


@mcp.tool()
def get_orderbook(symbol: str) -> dict[str, Any]:
    """Get the order book (bid/ask depth) for a stock symbol (Toss).

    Args:
        symbol: Stock symbol (e.g., "AAPL", "005930")

    Returns:
        Structured JSON envelope with order book entries (ask/bid prices and quantities).
    """
    try:
        data = _get_toss().get_orderbook(symbol)
        return _ok(data)
    except Exception as e:
        return _format_error(e)


@mcp.tool()
def get_recent_trades(symbol: str) -> dict[str, Any]:
    """Get recent trade executions for a stock symbol (Toss market data).

    Args:
        symbol: Stock symbol (e.g., "AAPL", "005930")

    Returns:
        Structured JSON envelope with recent trade entries (price, quantity, timestamp).
    """
    try:
        data = _get_toss().get_recent_trades(symbol)
        return _ok(data)
    except Exception as e:
        return _format_error(e)


@mcp.tool()
def get_price_limits(symbol: str) -> dict[str, Any]:
    """Get daily price limits (upper/lower bounds) for a stock (Toss).

    Args:
        symbol: Stock symbol (e.g., "AAPL", "005930")

    Returns:
        Structured JSON envelope with upper and lower price limit for the current trading day.
    """
    try:
        data = _get_toss().get_price_limits(symbol)
        return _ok(data)
    except Exception as e:
        return _format_error(e)


@mcp.tool()
def get_candles(
    symbol: str,
    interval: Literal["1d", "1m"] = "1d",
    count: int = 100,
    adjusted: bool = True,
) -> dict[str, Any]:
    """Get candlestick chart data (OHLCV) for a stock (Toss).

    Args:
        symbol: Stock symbol (e.g., "AAPL", "005930")
        interval: Candle interval — "1d" for daily or "1m" for 1-minute
        count: Number of candles to retrieve (max 200, default 100)
        adjusted: Whether to apply split/dividend adjustments (default True)

    Returns:
        Structured JSON envelope with candle entries (open, high, low, close, volume, timestamp).
    """
    try:
        data = _get_toss().get_candles(
            symbol, interval=interval, count=count, adjusted=adjusted
        )
        return _ok(data)
    except Exception as e:
        return _format_error(e)


@mcp.tool()
def get_stock_info(symbol: str) -> dict[str, Any]:
    """Get basic stock information (name, market, ISIN, status) (Toss).

    Args:
        symbol: Stock symbol (e.g., "AAPL", "005930")

    Returns:
        Structured JSON envelope with stock master data.
    """
    try:
        data = _get_toss().get_stock_info(symbol)
        return _ok(data)
    except Exception as e:
        return _format_error(e)


@mcp.tool()
def get_stock_warnings(symbol: str) -> dict[str, Any]:
    """Get stock purchase warnings (delisting, VI, overheated) (Toss).

    Args:
        symbol: Stock symbol (e.g., "AAPL", "005930")

    Returns:
        Structured JSON envelope with active warnings for the stock.
    """
    try:
        data = _get_toss().get_stock_warnings(symbol)
        return _ok(data)
    except Exception as e:
        return _format_error(e)


@mcp.tool()
def get_exchange_rate(base: str = "USD", quote: str = "KRW") -> dict[str, Any]:
    """Get exchange rate between two currencies (Toss, 1-min refresh).

    Args:
        base: Base currency code (default "USD")
        quote: Quote currency code (default "KRW")

    Returns:
        Structured JSON envelope with current exchange rate data.
    """
    try:
        data = _get_toss().get_exchange_rate(base=base, quote=quote)
        return _ok(data)
    except Exception as e:
        return _format_error(e)


@mcp.tool()
def get_market_hours(market: Literal["US", "KR"] = "US") -> dict[str, Any]:
    """Get market operating hours and calendar for KR or US markets (Toss).

    Includes pre-market, regular, and after-market session times.
    Useful for checking if the market is open, holidays, and trading windows.

    Args:
        market: Market country code — "KR" for Korean (KRX/NXT) or "US" for US markets

    Returns:
        Structured JSON envelope with market calendar and session times.
    """
    market = market.upper()
    if market not in ("KR", "US"):
        return _fail("INVALID_MARKET", "market must be KR or US")
    try:
        data = _get_toss().get_market_calendar(country=market)
        return _ok(data)
    except Exception as e:
        return _format_error(e)


# ════════════════════════════════════════════════════════════════
# Toss Account & Holdings Tools (3) — READ-ONLY
# ════════════════════════════════════════════════════════════════


@mcp.tool()
def get_toss_accounts() -> dict[str, Any]:
    """List configured Toss account labels without exposing credentials."""
    try:
        labels = _get_toss_registry().account_labels()
        return _ok({"accounts": labels, "default": "primary", "count": len(labels)}, provider="toss")
    except Exception as e:
        return _format_error(e)


@mcp.tool()
def get_toss_holdings(symbol: str = "", account: str = "primary") -> dict[str, Any]:
    """Get Toss Securities account holdings (READ-ONLY).

    Shows all stocks held in the Toss account with quantity, purchase price,
    market value, and P&L.

    Args:
        symbol: Optional — filter to a specific stock symbol. Empty string returns all.

    Returns:
        Structured JSON envelope with holdings data.
    """
    try:
        data = _for_toss_accounts(account, lambda client: client.get_holdings(symbol or None))
        return _ok(data, provider="toss", account=account)
    except Exception as e:
        return _format_error(e)


@mcp.tool()
def get_toss_buying_power(currency: Literal["USD", "KRW"] = "USD", account: str = "primary") -> dict[str, Any]:
    """Get available buying power (cash) from Toss Securities (READ-ONLY).

    Args:
        currency: "USD" or "KRW"

    Returns:
        Structured JSON envelope with buying power / available cash.
    """
    try:
        data = _for_toss_accounts(account, lambda client: client.get_buying_power(currency=currency))
        return _ok(data, provider="toss", account=account)
    except Exception as e:
        return _format_error(e)


@mcp.tool()
def get_toss_trade_history(limit: int = 50, account: str = "primary") -> dict[str, Any]:
    """Get recent trade history (filled orders) from Toss Securities (READ-ONLY).

    Args:
        limit: Maximum number of trades to retrieve (default 50)

    Returns:
        Structured JSON envelope with recent filled orders.
    """
    try:
        data = _for_toss_accounts(account, lambda client: client.get_trade_history(limit=limit))
        return _ok(data, provider="toss", account=account)
    except Exception as e:
        return _format_error(e)


# ════════════════════════════════════════════════════════════════
# KIS (Korea Investment Securities) Tools (6)
# ════════════════════════════════════════════════════════════════


@mcp.tool()
def get_kis_domestic_balance() -> dict[str, Any]:
    """Get Korean domestic stock balance from KIS (READ-ONLY).

    Shows all Korean stocks held with quantity, average price, current price,
    and P&L. Also includes cash balance.

    Returns:
        Structured JSON envelope with domestic balance data.
    """
    try:
        data = _get_kis().get_domestic_balance()
        return _ok(data)
    except Exception as e:
        return _format_error(e)


@mcp.tool()
def get_kis_overseas_balance() -> dict[str, Any]:
    """Get overseas (US/JP/HK) stock balance from KIS (READ-ONLY).

    Shows all overseas stocks held including US, Japan, Hong Kong, China.
    Includes cash balance per currency.

    Returns:
        Structured JSON envelope with overseas balance data.
    """
    try:
        data = _get_kis().get_overseas_balance()
        return _ok(data)
    except Exception as e:
        return _format_error(e)


@mcp.tool()
def get_kis_domestic_quote(symbol: str) -> dict[str, Any]:
    """Get real-time Korean stock quote from KIS (FHKST01010100).

    Args:
        symbol: 6-digit Korean stock code (e.g., "005930" for Samsung Electronics)

    Returns:
        Structured JSON envelope with current price, open/high/low, volume.
    """
    try:
        data = _get_kis().get_domestic_quote(symbol)
        return _ok(data)
    except Exception as e:
        try:
            fallback = _get_toss().get_prices(symbol)
            return _ok({"primary_provider": "kis", "primary_error": str(e)[:200], "fallback_provider": "toss", "fallback_data": fallback}, provider="kis_fallback_toss")
        except Exception:
            return _format_error(e)


@mcp.tool()
def get_kis_overseas_quote(symbol: str, exchange: Literal["NASDAQ", "NYSE", "AMEX", "TKSE", "TSE"] = "NASDAQ") -> dict[str, Any]:
    """Get overseas (US/JP) stock quote from KIS (HHDFS00000300).

    KIS provides real-time overseas quotes — useful for cross-checking Toss prices
    and for Japanese stocks (which Toss doesn't support).

    Args:
        symbol: Stock symbol (e.g., "AAPL", "7203" for Toyota)
        exchange: "NASDAQ", "NYSE", "AMEX", or "TKSE" (Tokyo)

    Returns:
        Structured JSON envelope with current price, change, volume, amount.
    """
    try:
        data = _get_kis().get_overseas_quote(symbol, exchange=exchange)
        return _ok(data)
    except Exception as e:
        try:
            fallback = _get_toss().get_prices(symbol)
            return _ok({"primary_provider": "kis", "primary_error": str(e)[:200], "fallback_provider": "toss", "fallback_data": fallback}, provider="kis_fallback_toss")
        except Exception:
            return _format_error(e)


@mcp.tool()
def get_kis_trade_history(start_date: str = "", end_date: str = "") -> dict[str, Any]:
    """Get trade history (execution records) from KIS (READ-ONLY).

    Fetches both domestic and overseas trade history.

    Args:
        start_date: Start date in YYYYMMDD format (default: 30 days ago)
        end_date: End date in YYYYMMDD format (default: today)

    Returns:
        Structured JSON envelope with domestic and overseas trade records.
    """
    try:
        if not end_date:
            end_date = datetime.now().strftime("%Y%m%d")
        if not start_date:
            start_date = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")

        domestic = _get_kis().get_domestic_trade_history(start_date, end_date)
        overseas = _get_kis().get_overseas_trade_history(start_date, end_date)
        return _ok({"domestic": domestic, "overseas": overseas}, provider="kis")
    except Exception as e:
        return _format_error(e)


@mcp.tool()
def get_kis_cash_balance() -> dict[str, Any]:
    """Get available cash from KIS domestic and overseas accounts (READ-ONLY).

    Returns KRW and USD cash available for trading.

    Returns:
        Structured JSON envelope with domestic and overseas cash balances.
    """
    try:
        domestic_cash = _get_kis().get_domestic_orderable_cash()
        overseas_cash = _get_kis().get_overseas_orderable_cash()
        return _ok({"krw": domestic_cash, "usd": overseas_cash}, provider="kis")
    except Exception as e:
        return _format_error(e)


# ════════════════════════════════════════════════════════════════
# SEC EDGAR Financials & Insider Tools (2)
# ════════════════════════════════════════════════════════════════


@mcp.tool()
def get_sec_financials(symbol: str) -> dict[str, Any]:
    """Get financial statements from SEC EDGAR CompanyFacts (US stocks only).

    Extracts annual (10-K) data for the last 4 years: revenue, net income,
    operating income, total assets, liabilities, equity, debt, cash, FCF, etc.
    No API key required. Uses SEC_USER_AGENT for fair access.

    Args:
        symbol: US stock ticker (e.g., "AAPL", "MSFT")

    Returns:
        Structured JSON envelope with annual financial data.
    """
    try:
        data = _get_sec().get_financials(symbol)
        return _ok(data)
    except Exception as e:
        return _format_error(e)


@mcp.tool()
def get_ttm_financials(symbol: str) -> dict[str, Any]:
    """Get SEC 10-Q/FY-derived TTM financials for a US stock."""
    try:
        data = _get_sec().get_ttm_financials(symbol)
        return _ok(data, provider="sec")
    except Exception as e:
        return _format_error(e)


@mcp.tool()
def get_insider_trades(symbol: str, days_back: int = 180, detail_level: Literal["summary", "full"] = "summary") -> dict[str, Any]:
    """Get insider trading data from SEC Form 4 filings (US stocks only).

    Shows insider purchases (code P) and sales (code S), plus option exercises,
    grants, tax withholdings, etc. Each trade includes the insider name, title,
    shares, price, and whether it's a 10% owner.

    Args:
        symbol: US stock ticker (e.g., "AAPL")
        days_back: How many days back to search (default 180, max 365)

    Returns:
        Structured JSON envelope with insider trade records.
    """
    try:
        days_back = min(days_back, 365)
        data = _get_sec().get_insider_trades(symbol, days_back=days_back, detail_level=detail_level)
        return _ok(data)
    except Exception as e:
        return _format_error(e)


# ════════════════════════════════════════════════════════════════
# yfinance Financials & Market Data Tools (2)
# ════════════════════════════════════════════════════════════════


@mcp.tool()
def get_financials(symbol: str) -> dict[str, Any]:
    """Get comprehensive financial data from yfinance (US, Korea, Japan).

    Provides income statement, balance sheet, cash flow, valuation metrics,
    and profitability ratios from yfinance. Works with US stocks ("AAPL"),
    Korean stocks ("005930.KS"), and Japanese stocks ("7203.T").

    Args:
        symbol: Stock ticker. US: "AAPL", Korean: "005930.KS", Japanese: "7203.T"

    Returns:
        Structured JSON envelope with financial statements and key metrics.
    """
    try:
        data = _get_yf().get_financials(symbol)
        return _ok(data)
    except Exception as e:
        return _format_error(e)


@mcp.tool()
def get_key_metrics(symbol: str) -> dict[str, Any]:
    """Get key financial metrics summary from yfinance (US, Korea, Japan).

    Returns P/E, PEG, P/B, EV/EBITDA, margins, ROE, ROA, growth rates, etc.

    Args:
        symbol: Stock ticker. US: "AAPL", Korean: "005930.KS", Japanese: "7203.T"

    Returns:
        Structured JSON envelope with key financial metrics.
    """
    try:
        data = _get_yf().get_key_metrics(symbol)
        return _ok(data)
    except Exception as e:
        return _format_error(e)


@mcp.tool()
def get_risk_free_rate() -> dict[str, Any]:
    """Get the current 10Y US Treasury yield for WACC discount-rate inputs."""
    try:
        return _ok(risk_free_rate(yf.Ticker("^TNX")), provider="yfinance")
    except Exception as e:
        return _format_error(e)


@mcp.tool()
def get_binance_futures_quote(symbol: str) -> dict[str, Any]:
    """Get Binance USD-M Futures latest price ticker without an API key."""
    try:
        return _ok(_get_binance().price_ticker(symbol), provider="binance")
    except Exception as e:
        return _format_error(e)


@mcp.tool()
def get_binance_futures_mark_price(symbol: str) -> dict[str, Any]:
    """Get Binance USD-M Futures mark/index price and latest funding rate."""
    try:
        return _ok(_get_binance().mark_price(symbol), provider="binance")
    except Exception as e:
        return _format_error(e)


@mcp.tool()
def get_binance_funding_rate(symbol: str, limit: int = 10) -> dict[str, Any]:
    """Get Binance USD-M Futures funding-rate history without an API key."""
    try:
        return _ok(_get_binance().funding_rates(symbol, limit=limit), provider="binance")
    except Exception as e:
        return _format_error(e)


@mcp.tool()
def get_binance_open_interest(
    symbol: str,
    history: bool = False,
    period: Literal["5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"] = "1h",
    limit: int = 30,
) -> dict[str, Any]:
    """Get current or historical Binance USD-M Futures open interest."""
    try:
        client = _get_binance()
        data = client.open_interest_history(symbol, period=period, limit=limit) if history else client.open_interest(symbol)
        return _ok(data, provider="binance")
    except Exception as e:
        return _format_error(e)


@mcp.tool()
def get_binance_futures_candles(
    symbol: str,
    interval: str = "1h",
    count: int = 100,
    price_type: Literal["last", "mark"] = "last",
) -> dict[str, Any]:
    """Get Binance USD-M Futures OHLCV candles for last price or mark price."""
    try:
        data = _get_binance().klines(
            symbol,
            interval=interval,
            limit=count,
            price_type=price_type,
        )
        return _ok(data, provider="binance")
    except Exception as e:
        return _format_error(e)


@mcp.tool()
def get_crypto_futures_snapshot(symbol: str) -> dict[str, Any]:
    """Get Binance USD-M Futures price, mark, funding, and open-interest snapshot."""
    try:
        return _ok(_get_binance().snapshot(symbol), provider="binance")
    except Exception as e:
        return _format_error(e)


# ════════════════════════════════════════════════════════════════
# margin-ta Technical Analysis Tools (3)
# ════════════════════════════════════════════════════════════════


@mcp.tool()
def analyze_technical(symbol: str, market: Literal["auto", "us", "kr"] = "auto", detail_level: Literal["summary", "standard", "full"] = "summary") -> dict[str, Any]:
    """Run full 43-indicator technical analysis using margin-ta (~30 seconds).

    Computes Entry Score (0-100), support/resistance levels, Fibonacci,
    AVWAP, volume profile, candlestick patterns, and market regime.
    Covers US and Korean stocks.
    Multi-horizon (daily/weekly/monthly) stances, indicator consensus, and tiered key levels included.

    Args:
        symbol: Stock ticker (e.g., "AAPL", "005930")
        market: "auto" (detect), "us", or "kr"

    Returns:
        Structured JSON envelope with full technical analysis result.
    """
    try:
        data = _get_mt().analyze(symbol, market=market)
        if detail_level == "summary" and isinstance(data, dict):
            full = data
            pricing = full.get("pricing", {})
            data = {
                "symbol": full.get("symbol"),
                "current_price": full.get("current_price"),
                "currency": full.get("currency") or full.get("info", {}).get("currency"),
                "entry_score": full.get("signals", {}).get("entry_score"),
                "entry_plans": pricing.get("entry_plans"),
                "warnings": full.get("warnings", []),
                "data_quality": full.get("data_quality"),
            }
            data.update(_summarize_analysis(full))
        return _ok(data, provider="margin-ta")
    except Exception as e:
        return _format_error(e)


@mcp.tool()
def get_entry_plan(symbol: str, market: Literal["auto", "us", "kr"] = "auto", detail_level: Literal["summary", "full"] = "summary") -> dict[str, Any]:
    """Get recommended entry strategy with stop loss and target prices (~30 seconds).

    Runs margin-ta analysis and returns the recommended entry plan with
    trigger type (Support Bounce / Trend Confirm / Breakout), entry price,
    stop loss, target price, risk/reward ratio, and holding period.
    Includes up to 2 alternative strategies.

    Args:
        symbol: Stock ticker (e.g., "AAPL", "005930")
        market: "auto" (detect), "us", or "kr"

    Returns:
        Structured JSON envelope with entry plan.
    """
    try:
        data = _get_mt().get_entry_plan(symbol, market=market, detail_level=detail_level)
        return _ok(data)
    except Exception as e:
        return _format_error(e)


@mcp.tool()
def scan_top_stocks(top_n: int = 5, min_score: int = 0) -> dict[str, Any]:
    """Scan NASDAQ 100 + S&P 500 for top technical setups (3-20 minutes).

    Runs the margin-ta nightly scanner. With OHLCV cache (~3 min), without
    cache (~20 min for 514 stocks). Returns top-N stocks ranked by Entry Score.

    Args:
        top_n: Number of top stocks to return (default 5)
        min_score: Minimum entry score filter (default 0)

    Returns:
        Structured JSON envelope with top stocks ranked by technical score.
    """
    try:
        data = _get_mt().scan_top_stocks(top_n=top_n, min_score=min_score)
        return _ok(data)
    except Exception as e:
        return _format_error(e)


# ════════════════════════════════════════════════════════════════
# Help / Usage Tool
# ════════════════════════════════════════════════════════════════


@mcp.tool()
def get_invest_mcp_help(topic: str = "overview") -> dict[str, Any]:
    """Get usage guide for this K-invest connector.

    Call this tool when the user asks what this connector can do, how to use it,
    which stock-analysis tools are available, or examples of supported queries.

    Args:
        topic: One of "overview", "quotes", "portfolio", "financials", "technical", "examples", "all".

    Returns:
        Markdown usage guide for the K-invest tools.
    """
    topic = (topic or "overview").strip().lower()

    sections: dict[str, str] = {
        "overview": """
# K-invest 사용 가이드

이 커넥터는 주식 분석용 read-only MCP 서버다. 주문/정정/취소 같은 거래 실행 기능은 없다.

## 주요 기능

- **시세/캔들/환율/장운영시간**: 토스증권 Open API
- **KIS 계좌 조회**: 국내/해외 잔고, 현금, 거래내역, 국내/해외 시세
- **토스 계좌 조회**: 보유종목, 매수가능금액, 최근 체결내역
- **재무 데이터**: yfinance + SEC EDGAR CompanyFacts annual/quarterly/TTM
- **내부자 거래**: SEC Form 4 XML
- **기술적 분석**: margin-ta Entry Score, 지지/저항, 진입전략
- **암호화폐 선물**: Binance USD-M Futures 공개 가격/mark/funding/open interest

## 추천 사용 순서

1. 현재가: `get_quote` 또는 `get_kis_domestic_quote` / `get_kis_overseas_quote`
2. 재무: `get_financials` 또는 미국 주식은 `get_sec_financials`
3. 기술적 진입가: `get_entry_plan` 또는 `analyze_technical`
4. 계좌 확인: `get_kis_domestic_balance`, `get_kis_overseas_balance`, `get_toss_holdings`
""",
        "quotes": """
# 시세/시장 데이터 도구

## 토스 기반

- `get_quote(symbol)` — 현재가. 예: `005930`, `AAPL`
- `get_orderbook(symbol)` — 호가
- `get_recent_trades(symbol)` — 시장 최근 체결
- `get_price_limits(symbol)` — 상/하한가
- `get_candles(symbol, interval="1d", count=100)` — 일봉/1분봉 캔들
- `get_stock_info(symbol)` — 종목 기본정보
- `get_stock_warnings(symbol)` — 매수 유의사항
- `get_exchange_rate(base="USD", quote="KRW")` — 환율
- `get_market_hours(market="US" 또는 "KR")` — 장 운영시간

## KIS 기반

- `get_kis_domestic_quote(symbol)` — 국내 실시간 시세. 예: `005930`
- `get_kis_overseas_quote(symbol, exchange)` — 해외 시세. 예: `AAPL`, `NASDAQ`; 일본은 `7203`, `TKSE`

## Binance USD-M Futures 공개 데이터 — API 키 불필요

- `get_binance_futures_quote(symbol)` — 최신 선물 가격. 예: `BTCUSDT`
- `get_binance_futures_mark_price(symbol)` — mark/index price와 latest funding rate
- `get_binance_funding_rate(symbol, limit=10)` — 펀딩비 히스토리
- `get_binance_open_interest(symbol, history=false, period="1h", limit=30)` — 현재 또는 히스토리 open interest
- `get_binance_futures_candles(symbol, interval="1h", count=100, price_type="last")` — last/mark price 캔들
- `get_crypto_futures_snapshot(symbol)` — 가격, mark, 펀딩, open interest 요약

거래 실행, 레버리지 변경, 마진 변경, 이체, 출금 도구는 없다.
""",
        "portfolio": """
# 계좌/보유/거래내역 도구 — READ ONLY

거래 실행 기능은 없다. 조회만 가능하다.

## 토스증권

- `get_toss_accounts()` — 설정된 토스 계좌 label 목록. credential 값은 반환하지 않음
- `get_toss_holdings(symbol="", account="primary")` — 토스 보유종목 전체 또는 특정 종목. `account`는 `primary`, `secondary`, `all`
- `get_toss_buying_power(currency="USD", account="primary")` — 토스 매수가능금액
- `get_toss_trade_history(limit=50, account="primary")` — 토스 최근 체결내역

## 한국투자증권(KIS)

- `get_kis_domestic_balance()` — 국내 주식 잔고
- `get_kis_overseas_balance()` — 해외 주식 잔고(미국/일본 등)
- `get_kis_cash_balance()` — KRW/USD 현금
- `get_kis_trade_history(start_date="YYYYMMDD", end_date="YYYYMMDD")` — 체결내역
""",
        "financials": """
# 재무/내부자 데이터 도구

## yfinance 기반 — 미국/한국/일본 지원

- `get_financials(symbol)` — 손익계산서, 재무상태표, 현금흐름, 밸류에이션
  - 미국: `AAPL`
  - 한국: `005930.KS`
  - 일본: `7203.T`
- `get_key_metrics(symbol)` — P/E, PEG, P/B, EV/EBITDA, ROE, 마진, 성장률, 애널리스트 컨센서스

## SEC EDGAR 기반 — 미국 주식 전용

- `get_sec_financials(symbol)` — SEC CompanyFacts 기반 10-K 연간 + 10-Q 분기 + TTM + 세그먼트 매출
- `get_ttm_financials(symbol)` — DCF base metric용 SEC TTM 요약
- `get_insider_trades(symbol, days_back=180)` — SEC Form 4 내부자 거래
- `get_risk_free_rate()` — WACC용 10Y 미국채 금리

주의: SEC 내부자 거래는 `transaction_code`를 봐야 한다.
- `P`: 실제 매수
- `S`: 실제 매도
- `M`, `A`, `F`: 옵션행사/부여/세금처리일 수 있어 매수·매도로 단정하면 안 된다.
""",
        "technical": """
# 기술적 분석 도구 — margin-ta

- `get_entry_plan(symbol, market="auto")` — 추천 진입전략, 손절가, 목표가, Entry Score
- `analyze_technical(symbol, market="auto")` — 43개 지표 기반 전체 기술적 분석
- `scan_top_stocks(top_n=5, min_score=0)` — NASDAQ100 + S&P500 기술적 스캔. 캐시 있으면 약 3분, 없으면 20분까지 걸릴 수 있다.
- `get_market_risk(detail_level="summary")` — 매크로/섹터 위험 대시보드(스코어·regime·alert 지표) + 내 토스/KIS 보유종목의 섹터 노출. `get_portfolio_risk`(통화/집중도)와는 별개.

## 멀티 호라이즌 (analyze_technical / get_entry_plan 응답 포함)

- `horizons`: 단기(daily)/중기(weekly)/장기(monthly) 3단 스탠스(stance/score)와 `alignment`(정렬 여부)
- `consensus`: 지표 간 합의도 `agreement`(0-100)와 상충 신호 `conflicts` 목록
- `key_levels`: 중장기 핵심 지지(`below`)/저항(`above`) top3

## market 값

- `auto`: 6자리 숫자는 한국, 일반 티커는 미국으로 자동 판별
- `kr`: 한국 주식 강제
- `us`: 미국 주식 강제

## 해석

- Entry Score 70+: 강한 기술적 신호
- 50~69: 보통
- 30~49: 약함/관망
- 30 미만: 회피
""",
        "examples": """
# 사용 예시

## 현재가

- “삼성전자 005930 현재가를 KIS로 조회해줘.” → `get_kis_domestic_quote("005930")`
- “AAPL 토스 현재가와 KIS 현재가를 비교해줘.” → `get_quote("AAPL")` + `get_kis_overseas_quote("AAPL", "NASDAQ")`

## 계좌

- “내 KIS 국내/해외 보유종목 보여줘.” → `get_kis_domestic_balance()` + `get_kis_overseas_balance()`
- “토스 두 계좌 보유종목과 최근 체결내역 보여줘.” → `get_toss_holdings(account="all")` + `get_toss_trade_history(account="all")`

## 재무/내부자

- “AAPL 재무와 내부자 거래를 확인해줘.” → `get_financials("AAPL")` + `get_sec_financials("AAPL")` + `get_insider_trades("AAPL")`
- “삼성전자 주요 밸류에이션 지표 알려줘.” → `get_key_metrics("005930.KS")`

## 기술적 분석

- “AAPL 진입가/손절가/목표가 알려줘.” → `get_entry_plan("AAPL")`
- “삼성전자 기술적 분석해줘.” → `analyze_technical("005930", "kr")`

## 암호화폐 선물

- “BTCUSDT 선물 mark price와 펀딩비 확인해줘.” → `get_crypto_futures_snapshot("BTCUSDT")`
- “ETHUSDT 선물 1시간봉 100개 가져와줘.” → `get_binance_futures_candles("ETHUSDT", "1h", 100)`
""",
    }

    if topic == "all":
        order = ["overview", "quotes", "portfolio", "financials", "technical", "examples"]
        markdown = "\n\n---\n\n".join(sections[k].strip() for k in order)
    else:
        markdown = sections.get(topic, sections["overview"]).strip()
    return _ok({"markdown": markdown}, provider="k-invest")


# ── Main ────────────────────────────────────────────────

@mcp.tool()
def compare_quotes(symbol: str, exchange: Literal["NASDAQ", "NYSE", "AMEX", "TKSE", "TSE"] = "NASDAQ") -> dict[str, Any]:
    """Compare Toss and KIS quotes with provider/session metadata."""
    try:
        toss_quote = _get_toss().get_prices(symbol)
        kis_error = None
        try:
            kis_quote = _get_kis().get_domestic_quote(symbol) if symbol.isdigit() else _get_kis().get_overseas_quote(symbol, exchange=exchange)
        except Exception as e:
            kis_quote = None
            kis_error = str(e)[:200]
        return _ok({
            "symbol": symbol.upper(),
            "quotes": [
                {"provider": "toss", "data": toss_quote, "price_type": "last_trade_or_provider_current"},
                {"provider": "kis", "data": kis_quote, "price_type": "broker_quote", "error": kis_error},
            ],
            "warning": "Provider prices may differ by venue, session, delay, or regular/after-market basis.",
            "fallback_used": kis_error is not None,
        }, provider="composite")
    except Exception as e:
        return _format_error(e)


@mcp.tool()
def get_stock_snapshot(symbol: str, market: Literal["auto", "us", "kr"] = "auto") -> dict[str, Any]:
    """Return a compact stock snapshot across quote, financial, insider, and technical sources."""
    try:
        quote = compare_quotes(symbol)
        metrics_symbol = f"{symbol}.KS" if market == "kr" or symbol.isdigit() else symbol
        metrics = _get_yf().get_key_metrics(metrics_symbol)
        technical = _get_mt().get_entry_plan(symbol, market=market, detail_level="summary")
        insider = None if symbol.isdigit() else _get_sec().get_insider_trades(symbol, days_back=180, detail_level="summary")
        return _ok({
            "symbol": symbol.upper(),
            "quote_comparison": quote.get("data"),
            "key_metrics": metrics,
            "insider_summary": insider.get("summary") if isinstance(insider, dict) else None,
            "technical_entry_plan": technical,
            "data_warnings": ["Snapshot combines providers with different sessions and update cadences."],
        }, provider="composite")
    except Exception as e:
        return _format_error(e)


@mcp.tool()
def get_portfolio_risk(detail_level: Literal["summary", "full"] = "summary") -> dict[str, Any]:
    """Return best-effort portfolio concentration and currency exposure risk."""
    positions: list[dict[str, Any]] = []
    source_errors: dict[str, str] = {}
    probes = [
        ("kis_domestic", lambda: _get_kis().get_domestic_balance()),
        ("kis_overseas", lambda: _get_kis().get_overseas_balance()),
    ]
    try:
        for label in _get_toss_registry().account_labels():
            probes.append((f"toss:{label}", lambda account_label=label: _get_toss(account_label).get_holdings()))
    except Exception as e:
        source_errors["toss_accounts"] = str(e)[:200]
    for source, probe in probes:
        try:
            payload = probe()
            for item in _walk_position_items(payload):
                pos = _position_from_item(source, item)
                if pos:
                    positions.append(pos)
        except Exception as e:
            source_errors[source] = str(e)[:200]
    data = _risk_from_positions(positions)
    data["source_errors"] = source_errors
    if detail_level == "summary":
        data.pop("total_unconverted_value", None)
        data["top_positions"] = data["top_positions"][:5]
    return _ok(data, provider="composite")


@mcp.tool()
def get_market_risk(detail_level: Literal["summary", "full"] = "summary") -> dict[str, Any]:
    """Market & sector risk dashboard with your holdings' sector exposure (READ-ONLY).

    Distinct from get_portfolio_risk (currency/concentration): this reads macro
    volatility (VXN-VIX, VIX term structure, VVIX), valuation (Buffett indicator),
    credit/rates, index overheating (monthly CCI/RSI, 200DMA gap), and per-sector
    risk scores, then maps YOUR Toss/KIS holdings to sectors to show concentration
    in stressed sectors.
    """
    try:
        mr = _get_mt().market_risk()
        if isinstance(mr, dict) and mr.get("error"):
            return _fail("MARGIN_TA_ERROR", mr.get("message", "market_risk failed"), provider="margin-ta")

        # 보유종목 → 섹터 매핑 (best-effort, 실패해도 시장 위험은 반환)
        positions: list[dict[str, Any]] = []
        try:
            probes = [
                ("kis_domestic", lambda: _get_kis().get_domestic_balance()),
                ("kis_overseas", lambda: _get_kis().get_overseas_balance()),
            ]
            try:
                for label in _get_toss_registry().account_labels():
                    probes.append((f"toss:{label}", lambda a=label: _get_toss(a).get_holdings()))
            except Exception:
                pass
            _SECTOR_MAP = {
                "technology": "XLK", "financial services": "XLF", "healthcare": "XLV",
                "energy": "XLE", "industrials": "XLI", "consumer cyclical": "XLY",
                "consumer defensive": "XLP", "utilities": "XLU", "basic materials": "XLB",
                "communication services": "XLC", "real estate": "XLRE",
            }
            for source, probe in probes:
                try:
                    for item in _walk_position_items(probe()):
                        pos = _position_from_item(source, item)
                        if not pos:
                            continue
                        try:
                            metrics_symbol = f"{pos['symbol']}.KS" if pos["symbol"].isdigit() else pos["symbol"]
                            sec = str((_get_yf().get_key_metrics(metrics_symbol) or {}).get("sector") or "").lower()
                            pos["sector_etf"] = _SECTOR_MAP.get(sec)
                        except Exception:
                            pos["sector_etf"] = None
                        positions.append(pos)
                except Exception:
                    continue
        except Exception:
            positions = []

        holdings_exposure = _summarize_holdings_sector_exposure(positions, mr) if positions else None

        if detail_level == "summary":
            top_sectors = sorted(
                mr.get("sector_risk", {}).items(), key=lambda x: x[1].get("score", 0), reverse=True
            )[:5]
            data = {
                "score": mr.get("score"),
                "regime": mr.get("regime"),
                "alerts": mr.get("alerts", []),
                "alert_indicators": {
                    k: v for k, v in mr.get("indicators", {}).items() if v.get("signal") == "alert"
                },
                "top_sectors": [{"etf": k, **v} for k, v in top_sectors],
                "holdings_exposure": holdings_exposure,
                "unavailable_count": len(mr.get("unavailable", [])),
                "as_of": mr.get("as_of"),
            }
        else:
            data = {**mr, "holdings_exposure": holdings_exposure}
        return _ok(data, provider="margin-ta")
    except Exception as e:
        return _format_error(e)


@mcp.tool()
def health_check() -> dict[str, Any]:
    """Check availability of configured K-invest upstreams with lightweight probes."""
    checks: dict[str, Any] = {}
    probes = [
        ("toss_market_data", lambda: _get_toss().get_prices("AAPL")),
        ("kis_market_data", lambda: _get_kis().get_overseas_quote("AAPL", exchange="NASDAQ")),
        ("sec", lambda: _get_sec().get_cik("AAPL")),
        ("yfinance", lambda: _get_yf().get_key_metrics("AAPL")),
        ("margin_ta", lambda: {"configured": bool(_get_mt())}),
        ("binance_futures", lambda: _get_binance().price_ticker("BTCUSDT")),
    ]
    for name, probe in probes:
        try:
            value = probe()
            checks[name] = {"status": "ok" if value else "degraded"}
        except NotConfiguredError as e:
            checks[name] = {"status": "not_configured", "error": str(e)[:200]}
        except Exception as e:
            if name == "kis_market_data":
                try:
                    fallback = _get_toss().get_prices("AAPL")
                    checks[name] = {"status": "degraded_fallback_ok", "error": str(e)[:200], "fallback_provider": "toss", "fallback_ok": bool(fallback)}
                    continue
                except Exception:
                    pass
            checks[name] = {"status": "degraded", "error": str(e)[:200]}
    return _ok(checks, provider="k-invest")


def main() -> None:
    if not MCP_AUTH_TOKEN or MCP_AUTH_TOKEN == "generate_a_random_token_here":
        logger.error(
            "MCP_AUTH_TOKEN is unset or still the .env.example placeholder — "
            "refusing to start. Set a real token (e.g. `openssl rand -hex 32`) in .env."
        )
        sys.exit(1)
    logger.info("Starting K-invest read-only MCP server on 127.0.0.1:8100")
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
