"""Safe, stable tool failure classes; legacy codes are retained for compatibility."""
from __future__ import annotations

import httpx

from binance_futures_api import BinanceAPIError
from kinvest_common import NotConfiguredError
from kis_api import KISAPIError
from oauth_store import AuthStoreUnavailable
from toss_api import TossAPIError

_MESSAGES = {
    "broker_auth_failed": "Broker authentication failed after renewal.",
    "provider_unavailable": "Provider is unavailable.",
    "rate_limited": "Provider rate limit reached.",
    "not_configured": "Provider is not configured.",
    "auth_store_unavailable": "OAuth state store unavailable.",
    "invalid_request": "Invalid tool input.",
}


def classify_error(exc: Exception) -> dict:
    provider = getattr(exc, "provider", None)
    status = getattr(exc, "status", None)
    body = getattr(exc, "body", {})
    broker_code = body.get("msg_cd") if isinstance(body, dict) else None
    if isinstance(exc, NotConfiguredError):
        kind, legacy = "not_configured", f"{provider.upper()}_NOT_CONFIGURED"
    elif isinstance(exc, AuthStoreUnavailable):
        kind, legacy = "auth_store_unavailable", "AUTH_STORE_UNAVAILABLE"
    elif isinstance(exc, (TossAPIError, KISAPIError, BinanceAPIError)):
        provider = "toss" if isinstance(exc, TossAPIError) else "kis" if isinstance(exc, KISAPIError) else "binance"
        if status == 429 or getattr(exc, "rate_limited", False):
            kind = "rate_limited"
        elif status in (401, 403) or broker_code in {"EGW00123", "EGW00121", "EGW00110"}:
            kind = "broker_auth_failed"
        else:
            kind = "provider_unavailable"
        legacy = "KIS_RATE_LIMITED" if provider == "kis" and kind == "rate_limited" else f"{provider.upper()}_API_ERROR"
    elif isinstance(exc, ValueError):
        kind, legacy = "invalid_request", "VALUEERROR"
    elif isinstance(exc, (httpx.RequestError, TimeoutError, ConnectionError)):
        kind, legacy = "provider_unavailable", type(exc).__name__.upper()
    else:
        kind, legacy = "provider_unavailable", "PROVIDER_ERROR"
    message = _MESSAGES[kind]
    # Existing callers need this safe date-validation hint, never arbitrary exceptions.
    if kind == "invalid_request" and "ISO YYYY-MM-DD" in str(exc):
        message = "Dates must use ISO YYYY-MM-DD."
    return {"code": legacy, "kind": kind, "message": message, "provider": provider,
            "upstream_status": status, "retryable": kind in {"provider_unavailable", "rate_limited", "auth_store_unavailable"}}
