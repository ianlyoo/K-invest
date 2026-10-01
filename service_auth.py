"""File-delivered, direct-loopback authentication for the Invest Desk read path."""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from contextvars import ContextVar
from pathlib import Path

from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken, TokenVerifier

# Match invest-desk/src/invest_desk/clients.py exactly. Widening requires a matching
# client change and review; do not infer permissions from a tool's name or hints.
SERVICE_READ_TOOLS = frozenset({
    "get_kis_accounts", "get_toss_accounts", "get_kis_trade_history", "get_toss_trade_history",
    "get_kis_domestic_balance", "get_kis_overseas_balance", "get_toss_holdings", "get_candles",
})
_direct_loopback: ContextVar[bool] = ContextVar("service_direct_loopback", default=False)
_PROXY_HEADERS = {b"x-forwarded-for", b"x-real-ip", b"forwarded"}


class ServiceTokenConfigurationError(RuntimeError):
    """Fixed configuration reason only: never include paths or token material."""


class ServiceAccessToken(AccessToken):
    """Typed principal, independent of potentially colliding OAuth client names."""


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
    return digest


class ServicePeerMiddleware:
    """Scope.client is the TCP peer: Uvicorn must run with proxy_headers=False."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        peer = scope.get("client")
        direct = bool(peer and peer[0] in {"127.0.0.1", "::1"}) and not any(
            key.lower() in _PROXY_HEADERS for key, _ in scope.get("headers", [])
        )
        reset = _direct_loopback.set(direct)
        try:
            await self.app(scope, receive, send)
        finally:
            _direct_loopback.reset(reset)


class ServiceTokenVerifier:
    def __init__(self, digest: bytes, existing: TokenVerifier):
        self._digest = digest
        self._existing = existing

    async def verify_token(self, token: str) -> AccessToken | None:
        if secrets.compare_digest(hashlib.sha256(token.encode()).digest(), self._digest):
            # A denied service token never falls through to OAuth or legacy auth.
            if not _direct_loopback.get():
                return None
            return ServiceAccessToken(token=token, client_id="invest-desk-service", scopes=[])
        return await self._existing.verify_token(token)


def is_service_request(request) -> bool:
    user = request.scope.get("user") if request is not None else None
    return isinstance(user, AuthenticatedUser) and isinstance(user.access_token, ServiceAccessToken)
