"""File-delivered, direct-loopback authentication for local service clients (scripts, a dashboard)."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import stat
from contextvars import ContextVar
from pathlib import Path
from typing import ClassVar

from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken, TokenVerifier

# The loopback service token's fixed allowlist: the eight raw account and candle reads. Widening needs
# review; do not infer permissions from a tool's name or hints.
SERVICE_READ_TOOLS = frozenset({
    "get_kis_accounts", "get_toss_accounts", "get_kis_trade_history", "get_toss_trade_history",
    "get_kis_domestic_balance", "get_kis_overseas_balance", "get_toss_holdings", "get_candles",
})
# Optional dashboard principal: its own systemd credential and fixed read-only allowlist.
# No history, export, snapshot, import or summary writers and no crypto tools.
DASHBOARD_READ_TOOLS = frozenset({
    "get_candles", "get_quote", "compare_quotes", "get_stock_info", "get_market_hours",
    "get_exchange_rate", "get_sec_financials", "get_ttm_financials", "get_financials",
    "get_key_metrics", "get_insider_trades", "get_stock_snapshot", "get_risk_free_rate",
    "get_toss_accounts", "get_kis_accounts", "get_toss_holdings", "get_kis_domestic_balance",
    "get_kis_overseas_balance",
})
DASHBOARD_CREDENTIAL = "dashboard-token"
# Accepted dashboard token file: exactly 43 base64url characters (secrets.token_urlsafe(32)),
# optionally followed by one "\n". Anything else disables only this principal.
DASHBOARD_TOKEN_FORMAT = re.compile(rb"[A-Za-z0-9_-]{43}\n?")
# SetCredential= default that systemd 249 uses when the LoadCredential= source is missing.
DASHBOARD_SOURCE_MISSING = "source-missing"
_direct_loopback: ContextVar[bool] = ContextVar("service_direct_loopback", default=False)
_PROXY_HEADERS = {b"x-forwarded-for", b"x-real-ip", b"forwarded"}


class ServiceTokenConfigurationError(RuntimeError):
    """Fixed configuration reason only: never include paths or token material."""


class ServiceAccessToken(AccessToken):
    """Typed principal, independent of potentially colliding OAuth client names."""

    principal_id: ClassVar[str] = "loopback-service"
    read_tools: ClassVar[frozenset[str]] = SERVICE_READ_TOOLS


class DashboardAccessToken(ServiceAccessToken):
    principal_id: ClassVar[str] = "dashboard-service"
    read_tools: ClassVar[frozenset[str]] = DASHBOARD_READ_TOOLS


def load_service_token(static_bearer: str) -> bytes | None:
    """Read one private regular file at startup; retain only its SHA-256 digest."""
    if "KINVEST_SERVICE_TOKEN" in os.environ:
        raise ServiceTokenConfigurationError("InlineServiceTokenForbidden")
    raw_path = os.environ.get("KINVEST_SERVICE_TOKEN_FILE", "")
    if not raw_path:
        return None
    if not Path(raw_path).is_absolute():
        raise ServiceTokenConfigurationError("ServiceTokenPathMustBeAbsolute")
    try:
        fd = os.open(raw_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(file.fileno())
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)
                    or info.st_uid not in (0, os.geteuid()) or info.st_size > 4096):
                raise ServiceTokenConfigurationError("ServiceTokenFileNotPrivate")
            token = file.read(4097).strip()
    except (OSError, ValueError):
        raise ServiceTokenConfigurationError("ServiceTokenFileUnavailable") from None
    if not re.fullmatch(rb"[A-Za-z0-9_-]{32,256}", token):
        raise ServiceTokenConfigurationError("ServiceTokenFileInvalid")
    if not static_bearer:
        raise ServiceTokenConfigurationError("ExistingStaticBearerRequired")
    digest = hashlib.sha256(token).digest()
    if secrets.compare_digest(digest, hashlib.sha256(static_bearer.encode()).digest()):
        raise ServiceTokenConfigurationError("ServiceTokenCollision")
    logging.getLogger("k-invest").info("service_token=loaded collision_guard=passed")
    return digest


def dashboard_token_problem(raw: bytes) -> str | None:
    """None for an accepted dashboard token file, else a fixed reason (never token bytes)."""
    if raw == DASHBOARD_SOURCE_MISSING.encode():
        return "DashboardTokenSourceMissing"
    if not raw.strip():
        return "DashboardTokenEmpty"
    return None if DASHBOARD_TOKEN_FORMAT.fullmatch(raw) else "DashboardTokenMalformed"


def _read_dashboard_token() -> tuple[bytes | None, str]:
    root = os.environ.get("CREDENTIALS_DIRECTORY", "")
    if not root or not Path(root).is_absolute():
        return None, "DashboardTokenAbsent"
    try:
        fd = os.open(Path(root) / DASHBOARD_CREDENTIAL, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, "DashboardTokenAbsent"
    except OSError:
        return None, "DashboardTokenFileUnavailable"
    try:
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(file.fileno())
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)
                    or info.st_uid not in (0, os.geteuid()) or info.st_size > 4096):
                return None, "DashboardTokenFileNotPrivate"
            raw = file.read(4097)
    except (OSError, ValueError):
        return None, "DashboardTokenFileUnavailable"
    problem = dashboard_token_problem(raw)
    return (None, problem) if problem else (raw.rstrip(b"\n"), "loaded")


def load_dashboard_token(static_bearer: str, service_digest: bytes | None) -> bytes | None:
    """Optional LoadCredential. A missing, unsafe, empty or malformed credential disables
    only this principal with a fixed reason; a collision is a configuration error."""
    token, state = _read_dashboard_token()
    log = logging.getLogger("k-invest")
    if token is None:
        (log.info if state == "DashboardTokenAbsent" else log.warning)("dashboard_token=disabled reason=%s", state)
        return None
    if not static_bearer:
        raise ServiceTokenConfigurationError("ExistingStaticBearerRequired")
    digest = hashlib.sha256(token).digest()
    if (secrets.compare_digest(digest, hashlib.sha256(static_bearer.encode()).digest())
            or service_digest is not None and secrets.compare_digest(digest, service_digest)):
        raise ServiceTokenConfigurationError("DashboardTokenCollision")
    log.info("dashboard_token=loaded collision_guard=passed")
    return digest


class ServicePeerMiddleware:
    """Scope.client is the TCP peer: Uvicorn must run with proxy_headers=False."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        peer = scope.get("client")
        direct = bool(peer and peer[0] == "127.0.0.1") and not any(
            key.lower() in _PROXY_HEADERS for key, _ in scope.get("headers", [])
        )
        reset = _direct_loopback.set(direct)
        try:
            await self.app(scope, receive, send)
        finally:
            _direct_loopback.reset(reset)


class ServiceTokenVerifier:
    def __init__(self, digest: bytes, existing: TokenVerifier,
                 principal: type[ServiceAccessToken] = ServiceAccessToken):
        self._digest = digest
        self._existing = existing
        self._principal = principal

    async def verify_token(self, token: str) -> AccessToken | None:
        if secrets.compare_digest(hashlib.sha256(token.encode()).digest(), self._digest):
            # A denied service token never falls through to OAuth or legacy auth.
            if not _direct_loopback.get():
                return None
            return self._principal(token=token, client_id=self._principal.principal_id, scopes=[])
        return await self._existing.verify_token(token)


def is_service_request(request) -> bool:
    user = request.scope.get("user") if request is not None else None
    return isinstance(user, AuthenticatedUser) and isinstance(user.access_token, ServiceAccessToken)


def service_read_tools(request) -> frozenset[str] | None:
    """The typed service principal's fixed allowlist; None for every other caller."""
    return request.scope["user"].access_token.read_tools if is_service_request(request) else None
