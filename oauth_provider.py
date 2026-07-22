"""Single-user OAuth 2.1 authorization server for K-invest (multi-client compatibility).

Provides a standard OAuth authorization-code flow (with PKCE) so OAuth-based MCP
clients (ChatGPT, Claude, Cursor, ...) can connect using manually-entered
client_id/client_secret, while a DualTokenVerifier keeps the legacy static bearer
token working for direct/scripted clients.

- Manual client: `MCP_OAUTH_CLIENT_ID` + `MCP_OAUTH_CLIENT_SECRET` env pair.
- Dynamic registration is also accepted (best-effort) for clients that self-register.
- `authorize` auto-approves (single user; no interactive login).
- Storage is in-memory: on server restart OAuth clients/tokens reset (clients
  re-authenticate); the static token is unaffected.

Security note: this is a personal single-user server. The authorize endpoint
auto-approves; access is gated by the (private) server URL plus the client_secret
on the token endpoint and PKCE on the code exchange.
"""
from __future__ import annotations

import os
import secrets
import time
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenVerifier,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken


_MAX_REMEMBERED_REDIRECT_URIS = 10


def _ttl_seconds() -> int:
    try:
        return int(os.environ.get("MCP_OAUTH_TOKEN_TTL", "3600"))
    except ValueError:
        return 3600


class DualTokenVerifier(TokenVerifier):
    """Verify either the static bearer token or an OAuth-issued token."""

    def __init__(self, static_token: str, provider: "PersonalOAuthProvider"):
        self._static_token = static_token
        self._provider = provider

    async def verify_token(self, token: str) -> AccessToken | None:
        if self._static_token and secrets.compare_digest(token, self._static_token):
            return AccessToken(
                token=token,
                client_id="static-token-client",
                scopes=[],
                expires_at=int(time.time()) + 3600,
            )
        return await self._provider.load_access_token(token)


class PersonalOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """In-memory, single-user, auto-approving OAuth 2.1 provider."""

    def __init__(self, manual_client_id: str | None, manual_client_secret: str | None):
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._client_secrets: dict[str, str] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._access_tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}
        self._manual_client_id = (manual_client_id or "").strip()
        self._manual_client_secret = (manual_client_secret or "").strip()
        if self._manual_client_id and self._manual_client_secret:
            # Pre-register the manual client. The schema requires >=1 redirect_uri;
            # we store a placeholder because the client's real redirect_uri is only
            # known at /authorize time. The custom /authorize handler in server.py
            # accepts the presented redirect_uri for the manual client (single-user,
            # trust-the-client model), so the placeholder is never user-facing.
            self._clients[self._manual_client_id] = OAuthClientInformationFull(
                client_id=self._manual_client_id,
                client_secret=self._manual_client_secret,
                redirect_uris=["https://placeholder.invalid/callback"],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="client_secret_post",
                client_name="manual-client",
            )
            self._client_secrets[self._manual_client_id] = self._manual_client_secret

    # ── client registry ──────────────────────────────────
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        client = self._clients.get(client_id)
        if client is None:
            return None
        return client.model_copy(deep=True)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        client_id = client_info.client_id or f"k-invest-{secrets.token_hex(8)}"
        client_secret = client_info.client_secret or secrets.token_urlsafe(32)
        info = client_info.model_copy(
            update={
                "client_id": client_id,
                "client_secret": client_secret,
                "client_id_issued_at": int(time.time()),
            }
        )
        self._clients[client_id] = info
        self._client_secrets[client_id] = client_secret

    def remember_redirect_uri(self, client_id: str, redirect_uri: str) -> None:
        """Merge a redirect_uri seen at /authorize into the stored client (idempotent)."""
        client = self._clients.get(client_id)
        if client is None or not redirect_uri:
            return
        uris = list(client.redirect_uris or [])
        if redirect_uri not in uris:
            # Cap the list: /authorize is unauthenticated, so an attacker could
            # otherwise grow it without bound by replaying random redirect_uris.
            uris = (uris + [redirect_uri])[-_MAX_REMEMBERED_REDIRECT_URIS:]  # type: ignore[list-item]
            self._clients[client_id] = client.model_copy(update={"redirect_uris": uris})

    # ── housekeeping ─────────────────────────────────────
    def _purge_expired(self) -> None:
        """Drop expired codes/tokens so unauthenticated /authorize traffic can't
        grow the in-memory stores without bound."""
        now = int(time.time())
        for store in (self._codes, self._access_tokens, self._refresh_tokens):
            for key in [
                k for k, v in store.items()
                if getattr(v, "expires_at", None) is not None and v.expires_at < now
            ]:
                store.pop(key, None)

    # ── authorization code ───────────────────────────────
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        self._purge_expired()
        code = secrets.token_urlsafe(32)
        if params.redirect_uri:
            self.remember_redirect_uri(client.client_id, str(params.redirect_uri))
        self._codes[code] = AuthorizationCode(
            code=code,
            scopes=list(params.scopes or []),
            expires_at=int(time.time()) + 600,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        return code

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        stored = self._codes.get(authorization_code)
        if stored is None or stored.client_id != client.client_id:
            return None
        return stored

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        stored = self._codes.get(authorization_code.code)
        if stored is None or stored.client_id != client.client_id:
            raise ValueError("invalid_authorization_code")
        if stored.expires_at < int(time.time()):
            self._codes.pop(authorization_code.code, None)
            raise ValueError("authorization_code_expired")
        self._codes.pop(authorization_code.code, None)  # single use
        return self._issue_tokens(client.client_id, list(stored.scopes or []), stored.resource)

    # ── tokens ───────────────────────────────────────────
    def _issue_tokens(self, client_id: str, scopes: list[str], resource: Any) -> OAuthToken:
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        ttl = _ttl_seconds()
        self._access_tokens[access] = AccessToken(
            token=access,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(time.time()) + ttl,
            resource=resource,
        )
        self._refresh_tokens[refresh] = RefreshToken(
            token=refresh,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(time.time()) + ttl * 24,
        )
        return OAuthToken(
            access_token=access,
            token_type="bearer",
            expires_in=ttl,
            scope=" ".join(scopes) if scopes else None,
            refresh_token=refresh,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        access = self._access_tokens.get(token)
        if access is None:
            return None
        if access.expires_at is not None and access.expires_at < int(time.time()):
            self._access_tokens.pop(token, None)
            return None
        return access

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        refresh = self._refresh_tokens.get(refresh_token)
        if refresh is None or refresh.client_id != client.client_id:
            return None
        if refresh.expires_at is not None and refresh.expires_at < int(time.time()):
            self._refresh_tokens.pop(refresh_token, None)
            return None
        return refresh

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        stored = self._refresh_tokens.get(refresh_token.token)
        if stored is None or stored.client_id != client.client_id:
            raise ValueError("invalid_refresh_token")
        self._refresh_tokens.pop(refresh_token.token, None)
        return self._issue_tokens(client.client_id, scopes or list(stored.scopes or []), None)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self._access_tokens.pop(token.token, None)
        self._refresh_tokens.pop(token.token, None)
