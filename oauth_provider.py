"""Single-user OAuth provider with durable, rotating grants and legacy bearer support.

Manual client configuration remains in the environment. Dynamic registration is
unsupported. The existing single-user auto-approving authorization policy is
unchanged; authorization codes and remembered redirects now survive restarts.
"""
from __future__ import annotations

import os
import secrets
import time
from pathlib import Path
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    TokenVerifier,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl

from auth_observability import auth_reason
from oauth_store import OAuthStore


def _ttl_seconds(name: str = "MCP_OAUTH_TOKEN_TTL", default: int = 3600) -> int:
    try:
        ttl = int(os.environ.get(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} must be a positive number of seconds") from None
    if not 1 <= ttl <= 366 * 86400:
        raise ValueError(f"{name} must be between 1 and 31622400 seconds")
    return ttl


def _refresh_grace_seconds() -> int:
    try:
        grace = int(os.environ.get("MCP_OAUTH_REFRESH_GRACE_SECONDS", "60"))
    except ValueError:
        raise ValueError("MCP_OAUTH_REFRESH_GRACE_SECONDS must be between 0 and 300 seconds") from None
    if not 0 <= grace <= 300:
        raise ValueError("MCP_OAUTH_REFRESH_GRACE_SECONDS must be between 0 and 300 seconds")
    return grace


def _invalid_grant() -> TokenError:
    return TokenError("invalid_grant", "Grant invalid or expired")


class DualTokenVerifier(TokenVerifier):
    def __init__(self, static_token: str, provider: PersonalOAuthProvider):
        self._static_token = static_token
        self._provider = provider

    async def verify_token(self, token: str) -> AccessToken | None:
        if self._static_token and secrets.compare_digest(token, self._static_token):
            return AccessToken(token=token, client_id="static-token-client", scopes=[], expires_at=int(time.time()) + 3600)
        return await self._provider.load_access_token(token)


class PersonalOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self, manual_client_id: str | None, manual_client_secret: str | None,
                 state_path: Path | None = None, resource_url: str | None = None):
        state_dir = Path(os.environ.get("KINVEST_OAUTH_STATE_DIR", str(Path.home() / ".local/state/k-invest/oauth")))
        self.store = OAuthStore(state_path or state_dir / "oauth.sqlite3")
        self.resource_url = str(AnyHttpUrl(resource_url)) if resource_url else None
        self.access_ttl = _ttl_seconds()
        self.refresh_ttl = _ttl_seconds("MCP_OAUTH_REFRESH_TOKEN_TTL", 30 * 86400)
        self.refresh_grace = _refresh_grace_seconds()
        self._client_id = (manual_client_id or "").strip()
        self._client_secret = (manual_client_secret or "").strip()

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if not self._client_id or not self._client_secret or client_id != self._client_id:
            return None
        return OAuthClientInformationFull(
            client_id=self._client_id, client_secret=self._client_secret,
            redirect_uris=self.store.redirect_uris(client_id) or ["https://placeholder.invalid/callback"],
            grant_types=["authorization_code", "refresh_token"], response_types=["code"],
            token_endpoint_auth_method="client_secret_post", client_name="manual-client",
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        raise ValueError("Dynamic registration is disabled")

    def remember_redirect_uri(self, client_id: str, redirect_uri: str) -> None:
        if client_id == self._client_id and redirect_uri:
            self.store.remember_redirect(client_id, redirect_uri)

    def _load(self, kind: str, value: str, client_id: str | None = None):
        row = self.store.get(kind, value)
        if row is None or row["revoked"] or row["consumed"] or row["client"] != (client_id or self._client_id):
            auth_reason.set("auth_invalid")
            return None
        if row["expires"] <= int(time.time()):
            auth_reason.set("auth_expired")
            return None
        return row

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if client.client_id != self._client_id:
            raise _invalid_grant()
        resource = str(params.resource) if params.resource else self.resource_url
        if self.resource_url and resource != self.resource_url:
            raise _invalid_grant()
        code = secrets.token_urlsafe(32)
        data = AuthorizationCode(code=code, client_id=client.client_id, scopes=list(params.scopes or []),
            expires_at=int(time.time()) + 600, code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri, redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=resource)
        metadata = data.model_dump(mode="json", exclude={"code"})
        self.store.issue([dict(kind="code", value=code, client=client.client_id, metadata=metadata,
            expires=data.expires_at, family=secrets.token_hex(16))])
        if params.redirect_uri:
            self.remember_redirect_uri(client.client_id, str(params.redirect_uri))
        return code

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        row = self._load("code", authorization_code, client.client_id)
        return AuthorizationCode(code=authorization_code, **row["metadata"]) if row else None

    def _pair(self, client: str, scopes: list[str], resource: Any, family: str):
        now = int(time.time())
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        pair = secrets.token_hex(16)
        records = [dict(kind="access", value=access, client=client, expires=now+self.access_ttl,
                       family=family, pair=pair, metadata={"scopes": scopes, "resource": resource}),
                   dict(kind="refresh", value=refresh, client=client, expires=now+self.refresh_ttl,
                       family=family, pair=pair, metadata={"scopes": scopes, "resource": resource})]
        token = OAuthToken(access_token=access, token_type="bearer", expires_in=self.access_ttl,
                           scope=" ".join(scopes) if scopes else None, refresh_token=refresh)
        return token, records

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        row = self._load("code", authorization_code.code, client.client_id)
        if not row:
            raise _invalid_grant()
        token, records = self._pair(client.client_id, row["metadata"]["scopes"], row["metadata"]["resource"], row["family"])
        if not self.store.issue(records, consume=("code", authorization_code.code, client.client_id)):
            raise _invalid_grant()
        return token

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self._load("access", token)
        if not row:
            return None
        if self.resource_url and row["metadata"].get("resource") != self.resource_url:
            auth_reason.set("auth_invalid")
            return None
        if not self.store.mark_access_used(token, self.refresh_grace):
            auth_reason.set("auth_invalid")
            return None
        return AccessToken(token=token, client_id=row["client"], expires_at=row["expires"], **row["metadata"])

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        row = self.store.load_refresh(refresh_token, client.client_id, self.refresh_grace)
        if not row:
            auth_reason.set("auth_invalid")
        return RefreshToken(token=refresh_token, client_id=row["client"], expires_at=row["expires"],
                            scopes=row["metadata"]["scopes"]) if row else None

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        row = self.store.load_refresh(refresh_token.token, client.client_id, self.refresh_grace)
        if not row or not set(scopes).issubset(row["metadata"]["scopes"]):
            raise _invalid_grant()
        # SDK resolves omitted scope to granted scope; explicit empty scope stays empty.
        token, records = self._pair(client.client_id, scopes, row["metadata"]["resource"], row["family"])
        if not self.store.rotate(refresh_token.token, client.client_id, records, self.refresh_grace):
            raise _invalid_grant()
        return token

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.store.revoke("access" if isinstance(token, AccessToken) else "refresh", token.token)
