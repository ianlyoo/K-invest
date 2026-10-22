"""Bounded RFC 7591 registration and authentication of hashed DCR secrets."""
from __future__ import annotations

import hashlib
import json
import secrets
import time

from mcp.server.auth.handlers.revoke import RevocationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import (
    AuthenticationError,
    ClientAuthenticator,
)
from mcp.server.auth.routes import build_metadata, cors_middleware, create_auth_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull
from starlette.datastructures import FormData
from starlette.responses import JSONResponse

from owner_gate import SAFE_HEADERS, GateError, gate_failure, redirect_allowed


class RegisteredClientAuthenticator(ClientAuthenticator):
    async def authenticate_request(self, request):
        fields = await request.form()
        client_id = fields.get("client_id", "")
        client = await self.provider.get_client(str(client_id))
        if client is None:
            raise AuthenticationError("Invalid client credentials")
        if client.client_id == self.provider._client_id:
            return await super().authenticate_request(request)
        if client.token_endpoint_auth_method == "none" and client.client_secret is None:
            return client
        value = fields.get("client_secret", "")
        if (client.token_endpoint_auth_method != "client_secret_post" or not isinstance(value, str)
                or not 1 <= len(value) <= 256 or not client.client_secret
                or not secrets.compare_digest(hashlib.sha256(value.encode()).hexdigest(), client.client_secret)):
            raise AuthenticationError("Invalid client credentials")
        return client


def build_dcr_routes(provider, issuer_url, scopes):
    async def register(request):
        try:
            provider.owner_gate.configured()
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                raise GateError("invalid_client_metadata")
            body = bytearray()
            async for chunk in request.stream():
                if len(chunk) > 8192 - len(body):
                    raise GateError("invalid_client_metadata")
                body.extend(chunk)
            def unique(pairs):
                if len({key for key, _ in pairs}) != len(pairs):
                    raise ValueError
                return dict(pairs)
            data = json.loads(body, object_pairs_hook=unique)
            if not isinstance(data, dict) or len(data) > 24 or any(len(key) > 128 for key in data):
                raise ValueError
            # RFC 7591 unknown metadata is ignored, within the same total body bound.
            # Never fetch client/logo/software URIs or render any external assets.
            uris = data.get("redirect_uris")
            method = data.get("token_endpoint_auth_method", "none")
            name = data.get("client_name", "OAuth client")
            scope = data.get("scope", " ".join(scopes))
            if (not isinstance(uris, list) or not 1 <= len(uris) <= 10 or not all(isinstance(uri, str) and redirect_allowed(uri) for uri in uris)
                    or method not in {"none", "client_secret_post"} or not isinstance(name, str) or not 1 <= len(name) <= 128
                    or any(ord(c) < 32 for c in name) or not isinstance(scope, str) or len(scope) > 256 or not set(scope.split()).issubset(scopes)
                    or not isinstance(data.get("grant_types", ["authorization_code", "refresh_token"]), list)
                    or set(data.get("grant_types", ["authorization_code", "refresh_token"])) != {"authorization_code", "refresh_token"}
                    or data.get("response_types", ["code"]) != ["code"]):
                raise ValueError
            client = OAuthClientInformationFull(client_id="dcr_" + secrets.token_urlsafe(24),
                client_secret=secrets.token_urlsafe(32) if method == "client_secret_post" else None,
                client_id_issued_at=int(time.time()), client_secret_expires_at=0 if method == "client_secret_post" else None, redirect_uris=uris, client_name=name, scope=scope,
                token_endpoint_auth_method=method, grant_types=["authorization_code", "refresh_token"], response_types=["code"])
            await provider.register_client(client)
            return JSONResponse(client.model_dump(mode="json", exclude_none=True), 201, headers=SAFE_HEADERS)
        except GateError as error:
            return gate_failure(error)
        except (ValueError, TypeError, UnicodeError):
            return gate_failure(GateError("invalid_client_metadata"))

    registration = ClientRegistrationOptions(enabled=True, valid_scopes=scopes, default_scopes=scopes)
    revocation = RevocationOptions(enabled=True)
    routes = create_auth_routes(provider, issuer_url, client_registration_options=registration, revocation_options=revocation)
    metadata = build_metadata(issuer_url, None, registration, revocation)
    metadata.token_endpoint_auth_methods_supported = ["none", "client_secret_post"]
    metadata.revocation_endpoint_auth_methods_supported = ["none", "client_secret_post"]
    async def discovery(request):
        return JSONResponse(metadata.model_dump(mode="json", exclude_none=True), headers=SAFE_HEADERS)
    authenticator = RegisteredClientAuthenticator(provider)
    async def revoke(request):
        fields = await request.form()
        # The SDK revocation model requires this nullable field even for public clients.
        if "client_secret" not in fields:
            request._form = FormData([*fields.multi_items(), ("client_secret", "")])
        return await RevocationHandler(provider, authenticator).handle(request)
    handlers = {"/register": register, "/token": TokenHandler(provider, authenticator).handle,
                "/revoke": revoke, "/.well-known/oauth-authorization-server": discovery}
    for route in routes:
        if route.path in handlers:
            route.endpoint = cors_middleware(handlers[route.path], ["GET", "OPTIONS"] if route.path.startswith("/.well-known/") else ["POST", "OPTIONS"])
    return routes
