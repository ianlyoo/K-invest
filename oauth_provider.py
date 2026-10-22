"""Single-user OAuth provider with durable, rotating grants and legacy bearer support.

Owner consent gates every client. Registered clients and rotating grants are durable;
static client configuration remains separately switchable.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
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
from owner_gate import GateError, owner_gate, redirect_allowed

MAX_CONSENT_NONCE_LENGTH = 12288
CONSENT_TTL = 300


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
        self._consent_key = hmac.digest(self._client_secret.encode(), b"ticker-research/consent/v1", "sha256") if self._client_secret else None
        self.owner_gate = owner_gate()
        self.static_enabled = os.environ.get("MCP_OAUTH_STATIC_CLIENT_ENABLED", "true").lower() in {"true", "1"}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if client_id != self._client_id:
            registered = self.store.get_client(client_id)
            return OAuthClientInformationFull(**registered) if registered else None
        if not self.static_enabled or not self._client_id or not self._client_secret:
            return None
        return OAuthClientInformationFull(
            client_id=self._client_id, client_secret=self._client_secret,
            redirect_uris=self.store.redirect_uris(client_id) or ["https://placeholder.invalid/callback"],
            grant_types=["authorization_code", "refresh_token"], response_types=["code"],
            token_endpoint_auth_method="client_secret_post", client_name="manual-client",
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.owner_gate.configured()
        if not client_info.redirect_uris or not all(redirect_allowed(str(uri)) for uri in client_info.redirect_uris):
            raise GateError("invalid_client_metadata")
        metadata = client_info.model_dump(mode="json")
        if metadata.get("client_secret"):
            metadata["client_secret"] = hashlib.sha256(metadata["client_secret"].encode()).hexdigest()
        try:
            cap = int(os.environ.get("MCP_OAUTH_CLIENT_CAP", "10"))
            if not 1 <= cap <= 100:
                raise ValueError
        except ValueError:
            raise GateError("registration_not_configured", 503) from None
        denied = self.store.register_client(metadata, cap=cap)
        if denied:
            raise GateError(denied[0], 429, denied[1])

    def remember_redirect_uri(self, client_id: str, redirect_uri: str) -> None:
        if client_id == self._client_id and redirect_uri:
            self.store.remember_redirect(client_id, redirect_uri)

    def _load(self, kind: str, value: str, client_id: str | None = None):
        row = self.store.get(kind, value)
        if (row is None or row["revoked"] or row["consumed"] or (client_id is not None and row["client"] != client_id)
                or (row["client"] == self._client_id and not self.static_enabled)):
            auth_reason.set("auth_invalid")
            return None
        if row["expires"] <= int(time.time()):
            auth_reason.set("auth_expired")
            return None
        return row

    def validate_authorization(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> None:
        static = client.client_id == self._client_id and self.static_enabled
        registered = self.store.get_client(client.client_id) if not static else None
        redirects = [str(params.redirect_uri)] if static else registered.get("redirect_uris", []) if registered else []
        if (str(params.redirect_uri) not in redirects or not redirect_allowed(str(params.redirect_uri))
                or (registered and not set(params.scopes or []).issubset((registered.get("scope") or "").split()))
                or not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge or "")
                or (self.resource_url and str(params.resource or self.resource_url) != self.resource_url)):
            raise _invalid_grant()

    async def begin_consent(self, client: OAuthClientInformationFull, params: AuthorizationParams,
                            cookie: str) -> str:
        self.validate_authorization(client, params)
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", cookie):
            raise _invalid_grant()
        payload = json.dumps({"version": 1, "client": client.client_id,
            "expires": int(time.time()) + CONSENT_TTL, "nonce": secrets.token_urlsafe(32),
            "cookie": hashlib.sha256(cookie.encode()).hexdigest(),
            "params": params.model_dump(mode="json")}, sort_keys=True, separators=(",", ":")).encode()
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
        nonce = encoded + "." + hmac.new(self._consent_key or self.owner_gate.signing_key(), encoded.encode(), "sha256").hexdigest()
        if len(nonce) > MAX_CONSENT_NONCE_LENGTH:
            raise _invalid_grant()
        return nonce

    def _consent(self, nonce: str, cookie: str) -> tuple[AuthorizationParams, int, str]:
        try:
            if not 1 <= len(nonce) <= MAX_CONSENT_NONCE_LENGTH or not re.fullmatch(r"[A-Za-z0-9_-]{43}", cookie):
                raise ValueError
            encoded, signature = nonce.split(".")
            expected = hmac.new(self._consent_key or self.owner_gate.signing_key(), encoded.encode("ascii"), "sha256").hexdigest()
            if not secrets.compare_digest(signature.encode("ascii"), expected.encode()):
                raise ValueError
            data = json.loads(base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True))
            if (data["version"] != 1 or not isinstance(data["client"], str)
                    or type(data["expires"]) is not int or data["expires"] <= int(time.time())
                    or not secrets.compare_digest(data["cookie"], hashlib.sha256(cookie.encode()).hexdigest())):
                raise ValueError
            return AuthorizationParams(**data["params"]), data["expires"], data["client"]
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise _invalid_grant() from None

    async def complete_consent(self, nonce: str, approval_secret: str,
                               cookie: str, posted_params=None, posted_client_id=None) -> tuple[str, AuthorizationParams]:
        params, expires, client_id = self._consent(nonce, cookie)
        if posted_params is not None and (params != posted_params or client_id != posted_client_id):
            raise _invalid_grant()
        client = await self.get_client(client_id)
        if client is None:
            raise _invalid_grant()
        self.validate_authorization(client, params)
        try:
            import asyncio
            await asyncio.to_thread(self.owner_gate.verify, approval_secret)
        except GateError:
            self.store.issue([], consumed_consent=(nonce, client_id, expires))
            raise
        code = await self.authorize(client, params, owner_approved=True, consent=(nonce, expires))
        return code, params

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams, *,
                        owner_approved: bool = False, consent: tuple[str, int] | None = None) -> str:
        if not owner_approved:
            raise _invalid_grant()
        self.validate_authorization(client, params)
        resource = str(params.resource) if params.resource else self.resource_url
        if self.resource_url and resource != self.resource_url:
            raise _invalid_grant()
        code = secrets.token_urlsafe(32)
        data = AuthorizationCode(code=code, client_id=client.client_id, scopes=list(params.scopes or []),
            expires_at=int(time.time()) + 600, code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri, redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=resource)
        metadata = data.model_dump(mode="json", exclude={"code"})
        if not self.store.issue([dict(kind="code", value=code, client=client.client_id, metadata=metadata,
            expires=data.expires_at, family=secrets.token_hex(16))],
            consumed_consent=(consent[0], client.client_id, consent[1]) if consent else None):
            raise _invalid_grant()
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
