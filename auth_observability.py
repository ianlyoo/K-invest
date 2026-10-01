"""Allowlisted HTTP diagnostics. No raw URLs, credentials or payloads are logged."""
from __future__ import annotations

import json
import logging
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from urllib.parse import parse_qs

from oauth_store import AuthStoreUnavailable

request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)

auth_reason: ContextVar[str | None] = ContextVar("auth_reason", default=None)
logger = logging.getLogger("k-invest.auth")
_PATHS = {"/mcp", "/token", "/authorize", "/revoke", "/healthz",
          "/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource"}
_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}


def response_reason(status: int, body: bytes) -> str:
    if status < 400:
        return "ok"
    if b"invalid_client" in body or b"unauthorized_client" in body:
        return "client_auth_failed"
    if status == 401:
        return auth_reason.get() or "auth_invalid"
    if status == 403:
        return "auth_invalid"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "provider_unavailable"
    # Read only for classification. Never include an extracted message in output.
    if b"Unsupported protocol version" in body:
        return "protocol_error"
    if b"Missing session ID" in body:
        return "session_missing"
    if b"Parse error" in body or b"Validation error" in body:
        return "invalid_request"
    if b"invalid_grant" in body:
        return auth_reason.get() or "auth_invalid"
    return "invalid_request"


class AuthObservability:
    def __init__(self, app):
        self.app = app

    def __getattr__(self, name):
        return getattr(self.app, name)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        reset = auth_reason.set(None)
        path = scope.get("path", "")
        category = path if path in _PATHS else ("legacy_mcp" if path.startswith("/mcp-") else "other")
        status, started, captured, form = 500, False, bytearray(), bytearray()
        headers = dict(scope.get("headers", []))
        version = headers.get(b"mcp-protocol-version", b"").decode("ascii", errors="ignore")
        protocol = version if version in _PROTOCOLS else ("absent" if not version else "unsupported")
        request_id = uuid.uuid4().hex
        request_reset = request_id_context.set(request_id)

        async def safe_receive():
            message = await receive()
            if path == "/token" and message["type"] == "http.request" and len(form) < 16384:
                form.extend(message.get("body", b"")[:16384-len(form)])
            return message

        async def safe_send(message):
            nonlocal status, started
            if message["type"] == "http.response.start":
                status, started = message["status"], True
                message = {**message, "headers": [*message.get("headers", []), (b"x-request-id", request_id.encode())]}
            elif message["type"] == "http.response.body" and status >= 400 and len(captured) < 4096:
                captured.extend(message.get("body", b"")[:4096-len(captured)])
            await send(message)

        try:
            try:
                await self.app(scope, safe_receive, safe_send)
            except AuthStoreUnavailable:
                auth_reason.set("auth_store_unavailable")
                if started:
                    raise
                await safe_send({"type": "http.response.start", "status": 503,
                    "headers": [(b"content-type", b"application/json"), (b"retry-after", b"5")]})
                await safe_send({"type": "http.response.body", "body": b'{"error":"temporarily_unavailable"}'})
        finally:
            grant = None
            if form:
                try:
                    parsed = parse_qs(form.decode("utf-8", errors="replace"), max_num_fields=64)
                except ValueError:
                    parsed = {}
                candidate = parsed.get("grant_type", [""])[0]
                grant = candidate if candidate in {"authorization_code", "refresh_token"} else "other"
            reason = auth_reason.get() or response_reason(status, bytes(captured))
            layer = "oauth" if path in {"/token", "/authorize", "/revoke"} else "auth" if reason.startswith("auth_") else "transport"
            event = {"timestamp": datetime.now(timezone.utc).isoformat(), "request_id": request_id,
                "path_category": category, "status": status, "layer": layer,
                "reason": reason, "grant_type": grant, "protocol_version": protocol}
            logger.info("%s", json.dumps(event, sort_keys=True))
            auth_reason.reset(reset)
            request_id_context.reset(request_reset)
