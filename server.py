#!/usr/bin/env python3
"""K-invest: Toss Securities + KIS + SEC EDGAR + Binance Futures Read-Only MCP Server.

Remote MCP server (Streamable HTTP) exposing read-only financial analysis tools:
  - Toss Securities Open API: market data, stock info, account/holdings, trade history
  - Korea Investment Securities (KIS): domestic/overseas balance, quotes, trade history
  - SEC EDGAR: financial statements (CompanyFacts), insider trades (Form 4)
  - yfinance: financials, key metrics, historical OHLCV
  - Binance USD-M Futures: public prices, funding rates, open interest, candles

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

import json
import logging
import os
import re
import secrets
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import partial, wraps
from pathlib import Path
from threading import RLock
from typing import Any, Literal
from urllib.parse import urlencode, urlparse

import anyio
import pydantic_core
import yfinance as yf

# Ensure local imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationParams,
    TokenError,
    TokenVerifier,
)
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import AnyHttpUrl, AnyUrl
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

import portfolio_engine
import response_shape
import tools_v2
from auth_observability import AuthObservability, request_id_context
from binance_futures_api import BinanceFuturesClient
from crypto_futures_accounts import (
    FuturesAccounts,
    FuturesError,
    configure_account_keys,
)
from daily_nav import consolidated_nav_basis
from error_taxonomy import classify_error
from health_status import HealthStatus, set_provider_observer
from instrument_identity import (
    MarketResolutionError,
    instrument_metadata,
    lookup_symbols,
    market_evidence,
    resolve_market,
)
from journal_metadata import annotate_rows, source_metadata
from kinvest_common import (
    YFINANCE_LOCK,
    NotConfiguredError,
    apply_env_file,
    synchronized,
)
from kis_accounts import KISAccountRegistry
from kis_api import KISClient
from ledger_returns import read_flows, scope_navs
from market_series import SeriesError, build_market_series
from oauth_provider import (
    MAX_CONSENT_NONCE_LENGTH,
    DualTokenVerifier,
    PersonalOAuthProvider,
)
from oauth_registration import build_dcr_routes
from oauth_store import AuthStoreUnavailable
from owner_gate import (
    AUTH_FIELDS,
    SAFE_HEADERS,
    GateError,
    consent_page,
    fields_for,
    gate_failure,
    load_credential,
    redirect_allowed,
)
from portfolio_engine import reconcile_collections
from portfolio_export import DatasetError, DatasetStore, collect_dataset, query_metadata
from portfolio_ledger import (
    PersistenceBudget,
    history_anchors,
    observe_broker_history,
    observe_collection,
    observe_futures,
)
from portfolio_model import FX_POLICY, FX_VALUATION_NOTE
from portfolio_sources import (
    PortfolioCollector,
    Providers,
    _masked_kis_diagnostic,
    collect_nav_evidence,
    kis_source,
    shared_position_reads,
)
from portfolio_store import NavStore, snapshot_summary, validate_live_snapshot_date
from sec_api import SECClient, sec_user_agent_configured
from service_auth import (
    DashboardAccessToken,
    ServicePeerMiddleware,
    ServiceTokenVerifier,
    is_service_request,
    load_dashboard_token,
    load_service_token,
    service_read_tools,
)
from statement_import import (
    PreviewManager,
    StatementError,
    StatementStore,
    merge_statements,
)
from tool_help import build_help
from toss_accounts import TossAccountRegistry
from toss_api import TossClient, _mask_account_no
from yfinance_api import YFinanceClient
from yfinance_consensus import risk_free_rate

# ── Logging ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("k-invest")
class _SafeAccessFilter(logging.Filter):
    def filter(self, record):
        # Uvicorn's AccessFormatter requires its original five arguments; suppress
        # the record rather than rewriting arguments and risking a logging traceback.
        return False


logging.getLogger("uvicorn.access").addFilter(_SafeAccessFilter())
_health_status = HealthStatus()

# httpx logs every request line at INFO, and broker account endpoints carry the
# account number in the query string (CANO=...), so root-level INFO would write
# account identifiers into journald on every balance call.
logging.getLogger("httpx").setLevel(logging.WARNING)

SERVER_VERSION = "0.2.0"

# ── Config ──────────────────────────────────────────────

# Load .env for clone-&-run usage (setdefault semantics: real env vars win).
apply_env_file(Path.cwd() / ".env")
apply_env_file(Path(__file__).resolve().parent / ".env")

MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()
_SERVICE_TOKEN_DIGEST = load_service_token(MCP_AUTH_TOKEN)
_DASHBOARD_TOKEN_DIGEST = load_dashboard_token(MCP_AUTH_TOKEN, _SERVICE_TOKEN_DIGEST)
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


# ── OAuth 2.1 (multi-client compatibility) ──────────────
# Optional owner-gated OAuth with public DCR and a separately switchable manual
# client. The manual secret prefers systemd LoadCredential; env is local-dev only.
# Existing direct bearer access remains independent of OAuth consent.
_OAUTH_CLIENT_ID = os.environ.get("MCP_OAUTH_CLIENT_ID", "").strip()
def _load_oauth_client_secret():
    if os.environ.get("CREDENTIALS_DIRECTORY"):
        return load_credential("oauth-client-secret") or ""
    return os.environ.get("MCP_OAUTH_CLIENT_SECRET", "").strip()


_OAUTH_CLIENT_SECRET = _load_oauth_client_secret()
_OAUTH_ENABLED = bool(MCP_AUTH_TOKEN and ((_OAUTH_CLIENT_ID and _OAUTH_CLIENT_SECRET) or os.environ.get("MCP_OAUTH_ENABLED", "").lower() in {"true", "1"}))

_oauth_provider: PersonalOAuthProvider | None = None


def _get_oauth_provider() -> PersonalOAuthProvider:
    """Open the durable auth store only when serving, never for timer imports."""
    global _oauth_provider
    if _oauth_provider is None:
        _oauth_provider = PersonalOAuthProvider(_OAUTH_CLIENT_ID, _OAUTH_CLIENT_SECRET, resource_url=_MCP_PUBLIC_URL)
    return _oauth_provider


class ServingOAuthVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        return await DualTokenVerifier(MCP_AUTH_TOKEN, _get_oauth_provider()).verify_token(token)


if MCP_AUTH_TOKEN and not _OAUTH_ENABLED:
    logger.info(
        "OAuth disabled (enable MCP_OAUTH_ENABLED or configure the manual client). "
        "Static bearer token auth active."
    )


def _resolve_token_verifier() -> TokenVerifier | None:
    if not MCP_AUTH_TOKEN:
        return None
    if _OAUTH_ENABLED:
        existing = ServingOAuthVerifier()
    else:
        existing = StaticTokenVerifier(MCP_AUTH_TOKEN)
    verifier = ServiceTokenVerifier(_SERVICE_TOKEN_DIGEST, existing) if _SERVICE_TOKEN_DIGEST else existing
    if _DASHBOARD_TOKEN_DIGEST:
        verifier = ServiceTokenVerifier(_DASHBOARD_TOKEN_DIGEST, verifier, DashboardAccessToken)
    return verifier


# ── Account identifiers ─────────────────────────────────
# get_toss_accounts' `••••NNNN` rule (toss_api._mask_account_no) for every
# account-number field: KIS `cano`/`CANO`, Toss `accountNo` and any key ending in
# account_no/account_number/acnt_no/acct_no. `acnt_prdt_cd` is a shared product
# code that get_kis_accounts already exposes, so it stays.
_ACCOUNT_KEY_SUFFIXES = ("accountno", "accountnumber", "acntno", "acctno")


def _is_account_key(key: Any) -> bool:
    name = re.sub(r"[^a-z]", "", str(key).lower())
    return name == "cano" or name.endswith(_ACCOUNT_KEY_SUFFIXES)


def _account_numbers(value: Any, found: set[str]) -> set[str]:
    """Full identifiers (raw and digits-only) under account keys at any depth."""
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_account_key(key) and isinstance(item, str | int) and not isinstance(item, bool):
                raw = str(item).strip()
                if len(re.sub(r"\D", "", raw)) > 4:
                    found.update({raw, re.sub(r"\D", "", raw)})
            _account_numbers(item, found)
    elif isinstance(value, list | tuple):
        for item in value:
            _account_numbers(item, found)
    return found


def mask_account_identifiers(value: Any, numbers: set[str] = frozenset()) -> Any:
    """Account fields become their last 4 (None below 4 digits); any full identifier
    from such a field, or from `numbers`, is masked inside every other string."""
    found = _account_numbers(value, {n for n in numbers if len(re.sub(r"\D", "", n)) > 4})
    pattern = re.compile("(?<!\\d)(?:%s)(?!\\d)" % "|".join(
        map(re.escape, sorted(found, key=len, reverse=True)))) if found else None

    def scrub(text: str) -> str:
        return pattern.sub(lambda match: _mask_account_no(match.group()) or "", text) if pattern else text

    def walk(item: Any) -> Any:
        if isinstance(item, dict):
            return {scrub(key) if isinstance(key, str) else key:
                    _mask_account_no(child) if _is_account_key(key) and not isinstance(child, dict | list)
                    else walk(child) for key, child in item.items()}
        if isinstance(item, list | tuple):
            return [walk(child) for child in item]
        return scrub(item) if isinstance(item, str) else item
    return walk(value)


def _known_account_numbers() -> set[str]:
    # Only an already-loaded registry: masking never loads broker configuration.
    try:
        return {str(account.cano) for account in _kis_accounts.accounts()} if _kis_accounts is not None else set()
    except Exception:
        return set()  # Field-based masking still applies.


def _result_parts(result: Any) -> tuple[list[Any], Any]:
    """Every FastMCP 1.26 call_tool shape: content, (content, structured) or CallToolResult."""
    if isinstance(result, CallToolResult):
        return list(result.content), result.structuredContent
    if isinstance(result, tuple):
        return list(result[0]), result[1]
    return list(result), None


def _with_parts(result: Any, content: list[Any], structured: Any) -> Any:
    if isinstance(result, CallToolResult):
        return result.model_copy(update={"content": content, "structuredContent": structured})
    return (content, structured) if isinstance(result, tuple) else content


def _masked_tool_result(result: Any, known: set[str]) -> Any:
    content, structured = _result_parts(result)
    parsed: list[Any] = []
    for block in content:
        try:
            parsed.append(json.loads(block.text) if isinstance(block, TextContent) else None)
        except ValueError:
            parsed.append(None)
    numbers = _account_numbers(structured, set(known))
    for value in parsed:
        _account_numbers(value, numbers)
    masked_content = []
    for block, value in zip(content, parsed):
        if isinstance(block, TextContent):
            if value is None:
                text = mask_account_identifiers(block.text, numbers)
            else:
                # The block's own serialization: compact wire text stays compact; FastMCP's indent-2
                # text (service principals, unshaped as in 2.14.1) stays indent-2.
                masked_value = mask_account_identifiers(value, numbers)
                text = (response_shape.compact_json(masked_value) if block.text == response_shape.compact_json(value)
                        else pydantic_core.to_json(masked_value, fallback=str, indent=2).decode())
            block = block.model_copy(update={"text": text})
        masked_content.append(block)
    masked = None if structured is None else mask_account_identifiers(structured, numbers)
    return _with_parts(result, masked_content, masked)


def _wire_result(result: Any, name: str, level: str | None) -> Any:
    """MCP 2025-06-18 tool result: the text block is the compact JSON of the structured object.

    Policy tools are shaped to their detail level first (response_shape). The one
    TextContent block replaces FastMCP's indent-2 copy; non-text blocks are kept.
    """
    content, structured = _result_parts(result)
    texts = [block for block in content if isinstance(block, TextContent)]
    # Only the dict-envelope case: FastMCP's text is the JSON of the same object.
    # Tools returning a plain string or list keep FastMCP's text block.
    if not isinstance(structured, dict) or len(texts) != 1 or _parsed(texts[0].text) != structured:
        return result
    if level is not None:
        structured = response_shape.shape(structured, response_shape.POLICIES.get(name), level)
    text = TextContent(type="text", text=response_shape.compact_json(structured))
    return _with_parts(result, [text, *(block for block in content if not isinstance(block, TextContent))], structured)


def _parsed(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


# ── MCP Server ──────────────────────────────────────────

class ObservedFastMCP(FastMCP):
    def __init__(self, *args, **kwargs):
        # Tools whose detail_level parameter exists only at the result layer.
        self._injected_detail: set[str] = set()
        super().__init__(*args, **kwargs)
        # Separate from Starlette's default pool; queued tools never occupy the
        # event loop. Eight workers allow independent reads without unbounded IO.
        self._tool_limiter = anyio.CapacityLimiter(8)

    def add_tool(self, fn, name=None, *args, **kwargs):
        # Let MCP 1.26 build all metadata from the ORIGINAL function first.
        # Wrapping before registration can change schemas/context injection.
        super().add_tool(fn, name, *args, **kwargs)
        tool = self._tool_manager.get_tool(name or fn.__name__)
        if tool is None:
            return
        policy = response_shape.POLICIES.get(tool.name)
        properties = tool.parameters.setdefault("properties", {})
        if policy is not None and policy.inject and "detail_level" not in properties:
            # Optional summary/full switch handled at the result layer (call_tool);
            # the tool function never sees it.
            properties["detail_level"] = {
                "default": response_shape.SUMMARY, "enum": list(response_shape.LEVELS),
                "title": "Detail Level", "type": "string", "description": response_shape.DETAIL_DESCRIPTION}
            self._injected_detail.add(tool.name)
        if tool.is_async:
            return
        original = tool.fn

        @wraps(original)
        async def offloop(**arguments):
            # AnyIO copies contextvars, including MCP/auth/request IDs. Keep the
            # default cancellation shield: a disconnected caller must not release
            # capacity while its blocking broker/SQLite work is still running.
            return await anyio.to_thread.run_sync(
                partial(original, **arguments), limiter=self._tool_limiter,
            )

        tool.fn = offloop
        tool.is_async = True

    def sse_app(self, mount_path=None):
        if self is mcp:
            _mount_oauth_routes()
        return super().sse_app(mount_path)

    def streamable_http_app(self):
        if self is mcp:
            _mount_oauth_routes()
        app = super().streamable_http_app()
        app.add_middleware(AuthObservability)
        app.add_middleware(ServicePeerMiddleware)
        return app

    def _service_tools(self):
        # Each typed service principal carries its own fixed allowlist.
        try:
            request = self._mcp_server.request_context.request
        except LookupError:
            return None
        return service_read_tools(request)

    async def list_tools(self):
        tools = await super().list_tools()
        allowed = self._service_tools()
        if allowed is None:
            return tools_v2.visible_tools(self, tools)
        # Service principals get 2.14.1's view; the result-layer detail_level does not apply to them.
        return [self._without_injected_detail(tool) for tool in tools if tool.name in allowed]

    def _without_injected_detail(self, tool):
        if tool.name not in self._injected_detail:
            return tool
        schema = dict(tool.inputSchema)
        schema["properties"] = {k: v for k, v in schema.get("properties", {}).items() if k != "detail_level"}
        return tool.model_copy(update={"inputSchema": schema})

    def _dashboard_principal(self):
        try:
            request = self._mcp_server.request_context.request
        except LookupError:
            return False
        return is_service_request(request) and isinstance(request.scope["user"].access_token, DashboardAccessToken)

    def _detail_level(self, name, arguments):
        """(arguments for the tool, detail level to shape to or None)."""
        if name not in response_shape.POLICIES:
            return arguments, None
        arguments = dict(arguments or {})
        if name in self._injected_detail:
            level = arguments.pop("detail_level", response_shape.SUMMARY)
        else:
            tool = self._tool_manager.get_tool(name)
            schema = ((tool.parameters if tool else {}).get("properties") or {}).get("detail_level") or {}
            level = arguments.get("detail_level", schema.get("default", response_shape.SUMMARY))
        if level not in response_shape.LEVELS:
            raise ToolError(f"detail_level must be one of {list(response_shape.LEVELS)}")
        return arguments, level

    async def call_tool(self, name, arguments):
        # Parallel tool calls share one in-flight positions read (KIS EGW00215 on a
        # ChatGPT burst); the NAV write reads its own.
        with shared_position_reads(name not in _OWN_POSITION_READ_TOOLS):
            return await self._call_tool(name, arguments)

    async def _call_tool(self, name, arguments):
        allowed = self._service_tools()
        if allowed is not None and name not in allowed:
            return CallToolResult(isError=True, content=[TextContent(type="text", text="Forbidden")])
        if allowed is not None:
            # Service principals get 2.14.1's responses byte for byte (no detail shaping,
            # FastMCP's own text block); a result-layer detail_level stays unseen by the tool, as before.
            if name in self._injected_detail and "detail_level" in (arguments or {}):
                arguments = {key: value for key, value in arguments.items() if key != "detail_level"}
            wire = finish = tools_v2._same
        else:
            name, arguments, finish = tools_v2.route(self, name, arguments)
            arguments, level = self._detail_level(name, arguments)
            wire = partial(_wire_result, name=name, level=level)
        if not self._dashboard_principal():
            return finish(wire(await super().call_tool(name, arguments)))
        # The dashboard sees account identifiers only as ••••NNNN, in structured
        # content, text content and error text alike.
        known = _known_account_numbers()
        try:
            result = await super().call_tool(name, arguments)
        except ToolError as exc:
            raise ToolError(mask_account_identifiers(str(exc), known)) from None
        return _masked_tool_result(wire(result), known)

    async def run_streamable_http_async(self):
        import uvicorn
        config = uvicorn.Config(self.streamable_http_app(), host=self.settings.host,
            port=self.settings.port, log_level=self.settings.log_level.lower(), proxy_headers=False)
        await uvicorn.Server(config).serve()


#: Tools that never take a shared positions read (portfolio_sources.shared_position_reads).
_OWN_POSITION_READ_TOOLS = frozenset({"snapshot_portfolio_nav"})

mcp = ObservedFastMCP(
    name="K-invest",
    instructions=(
        "Read-only stock analysis MCP server. "
        "Provides market data (Toss, KIS, yfinance), financial statements (SEC EDGAR, yfinance), "
        "insider trades (SEC Form 4), account/holdings/trade history (Toss, KIS), "
        "and public crypto futures market data (Binance). "
        "Supports Korean (KRX/NXT), US, and Japanese markets. "
        "No order creation or trading — ALL endpoints are read-only."
    ),
    token_verifier=_resolve_token_verifier(),
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

# Mount the OAuth authorization-server routes as public custom routes when OAuth is
# enabled. FastMCP forbids passing auth_server_provider and token_verifier together,
# so we verify via DualTokenVerifier (above) and mount the OAuth routes manually here.
# Owner consent precedes code issuance for static and registered clients alike.
_COOKIE = "__Host-investmcp-consent"


async def _oauth_authorize(request: Request) -> Response:
    provider = _get_oauth_provider()
    try:
        provider.owner_gate.configured()
        fields = await fields_for(request)
        qp = {name: value for name, value in fields.items() if name in AUTH_FIELDS}
        if (request.method == "POST" and set(fields) - AUTH_FIELDS - {"nonce", "approval_secret"}
                or qp.get("response_type") != "code" or qp.get("code_challenge_method") != "S256"
                or len(qp.get("state", "")) > 1024 or not redirect_allowed(qp.get("redirect_uri", ""))):
            raise GateError("invalid_request")
        client = await provider.get_client(qp.get("client_id", ""))
        if client is None:
            raise GateError("invalid_request")
        params = AuthorizationParams(redirect_uri=AnyUrl(qp["redirect_uri"]), redirect_uri_provided_explicitly=True,
            state=qp.get("state"), code_challenge=qp.get("code_challenge"), scopes=qp.get("scope", "").split(),
            resource=qp.get("resource", provider.resource_url))
        provider.validate_authorization(client, params)
        if request.method == "GET":
            cookie = secrets.token_urlsafe(32)
            nonce = await provider.begin_consent(client, params, cookie)
            response = consent_page(client.client_name, str(params.redirect_uri), {**qp, "nonce": nonce}, "/authorize")
            response.set_cookie(_COOKIE, cookie, max_age=300, secure=True, httponly=True, samesite="strict", path="/")
            return response
        nonce, cookie = fields.get("nonce", ""), request.cookies.get(_COOKIE, "")
        origin = urlparse(_MCP_PUBLIC_URL)
        if (request.headers.get("origin") != origin.scheme + "://" + origin.netloc or not cookie or len(cookie) != 43
                or not nonce or len(nonce) > MAX_CONSENT_NONCE_LENGTH or "approval_secret" not in fields):
            raise GateError("access_denied", 403)
        code, params = await provider.complete_consent(nonce, fields["approval_secret"], cookie, params, client.client_id)
        query = {"code": code}
        if params.state is not None:
            query["state"] = params.state
        uri = str(params.redirect_uri)
        response = RedirectResponse(uri + ("&" if "?" in uri else "?") + urlencode(query), 302, headers=SAFE_HEADERS)
        response.delete_cookie(_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return response
    except GateError as error:
        return gate_failure(error)
    except AuthStoreUnavailable:
        raise
    except (TokenError, ValueError, TypeError):
        return gate_failure(GateError("access_denied" if request.method == "POST" else "invalid_request"))


def _mount_oauth_routes() -> None:
    if not _OAUTH_ENABLED or getattr(mcp, "_oauth_routes_mounted", False):
        return
    provider = _get_oauth_provider()
    for _route in build_dcr_routes(provider, AnyHttpUrl(_MCP_PUBLIC_URL), ["read"]):
        if _route.path == "/authorize":
            continue  # replaced by the custom handler below
        mcp.custom_route(
            _route.path,
            methods=list(_route.methods or []),
            name=_route.name,
        )(_route.endpoint)
    mcp.custom_route("/authorize", methods=["GET", "POST"], name="oauth-authorize")(
        _oauth_authorize
    )
    mcp._oauth_routes_mounted = True
    logger.info("OAuth 2.1 authorization server enabled (owner consent required).")

# ── Client Singletons (lazy) ────────────────────────────

_client_init_lock = RLock()

_toss_client: TossClient | None = None
_toss_accounts: TossAccountRegistry | None = None
_kis_accounts: KISAccountRegistry | None = None
_sec_client: SECClient | None = None
_yf_client: YFinanceClient | None = None
_binance_client: BinanceFuturesClient | None = None
_collector: PortfolioCollector | None = None
_nav_store: NavStore | None = None


@synchronized(_client_init_lock)
def _get_toss_registry() -> TossAccountRegistry:
    global _toss_accounts
    if _toss_accounts is None:
        _toss_accounts = TossAccountRegistry.from_sources()
    return _toss_accounts


@synchronized(_client_init_lock)
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


@synchronized(_client_init_lock)
def _get_kis_registry() -> KISAccountRegistry:
    global _kis_accounts
    if _kis_accounts is None:
        _kis_accounts = KISAccountRegistry.from_sources()
    return _kis_accounts


@synchronized(_client_init_lock)
def _get_kis(account: str = "") -> KISClient:
    """Client for one configured KIS account (default: the `primary` account)."""
    return _get_kis_registry().client_for(account)


#: Stands in for the overseas half of a KIS response on a pension account —
#: 연금저축(22)/퇴직연금(29) accounts cannot hold overseas-listed securities, so the
#: call is skipped rather than reported as an empty portfolio.
_OVERSEAS_UNSUPPORTED: dict[str, Any] = {
    "supported": False,
    "code": "OVERSEAS_NOT_SUPPORTED",
    "message": "연금저축/퇴직연금 계좌는 해외 상장 종목을 보유할 수 없습니다",
}


def _kis_overseas_balance(client: KISClient) -> dict[str, Any]:
    if not client.supports_overseas:
        return dict(_OVERSEAS_UNSUPPORTED)
    return client.get_overseas_balance()


def _for_kis_accounts(account: str, operation: Any) -> Any:
    """Run `operation` against one KIS account, or every one when account="all"."""
    normalized = (account or "").strip().lower()
    registry = _get_kis_registry()
    if normalized != "all":
        return operation(registry.client_for(normalized))
    results: list[dict[str, Any]] = []
    for label in registry.account_labels():
        try:
            results.append({"account": label, "data": operation(registry.client_for(label)), "error": None})
        except Exception as e:
            results.append({"account": label, "data": None, "error": _format_error(e).get("error")})
    return {"accounts": results}


@synchronized(_client_init_lock)
def _get_sec() -> SECClient:
    global _sec_client
    if _sec_client is None:
        _sec_client = SECClient()
    return _sec_client


@synchronized(_client_init_lock)
def _get_yf() -> YFinanceClient:
    global _yf_client
    if _yf_client is None:
        _yf_client = YFinanceClient()
    return _yf_client


@synchronized(_client_init_lock)
def _get_binance() -> BinanceFuturesClient:
    global _binance_client
    if _binance_client is None:
        _binance_client = BinanceFuturesClient()
    return _binance_client


def _persist_broker_history(provider, client, account_scope, market, payload, metadata, budget):
    try:
        with budget.attempt():
            observe_broker_history(provider, getattr(client, "journal_account_key", account_scope),
                                   account_scope, market, payload, metadata, _get_nav_store().db_path)
    except Exception:
        logging.getLogger(__name__).error("permanent ledger observation failed: LEDGER_WRITE_FAILED")
        metadata["ledger_persistence"] = {"status": "partial", "reason_codes": ["LEDGER_WRITE_FAILED"]}


@synchronized(_client_init_lock)
def _get_collector() -> PortfolioCollector:
    """Portfolio collector singleton — it also carries rolling source health."""
    global _collector
    if _collector is None:
        _collector = PortfolioCollector(
            Providers(
                toss_labels=lambda: _get_toss_registry().account_labels(),
                toss_client=_get_toss,
                kis_accounts=lambda: _get_kis_registry().accounts(),
                kis_client=_get_kis,
                error_formatter=_format_error,
                health_observer=lambda source, **outcome: _health_status.record(source, **outcome),
                statement_store=lambda: StatementStore(_get_nav_store().db_path),
                ledger_observer=lambda result: observe_collection(result, _get_nav_store().db_path),
                ledger_anchor=lambda sources, start: history_anchors(sources, start, _get_nav_store().db_path),
            )
        )
    return _collector


@synchronized(_client_init_lock)
def _get_nav_store() -> NavStore:
    global _nav_store
    if _nav_store is None:
        _nav_store = NavStore()
    return _nav_store


def _get_dataset_store() -> DatasetStore:
    """Sibling state only; never writes into an external consumer's directory."""
    return DatasetStore(_get_nav_store().db_path.with_name("portfolio_datasets.db"))


_statement_manager = None


@synchronized(_client_init_lock)
def _get_statement_manager():
    global _statement_manager
    if _statement_manager is None:
        inbox = Path(os.environ.get("KINVEST_STATEMENT_INBOX", "~/.local/state/k-invest/statements/inbox")).expanduser()
        _statement_manager = PreviewManager(StatementStore(_get_nav_store().db_path), inbox,
            ledger=lambda start, end, accounts: _get_collector().collect_transactions(start, end, accounts),
            labels=lambda: _get_toss_registry().account_labels(), registry=_get_toss_registry)
    return _statement_manager


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def preview_statement_import(broker: Literal["toss"] = "toss", *, file: str, account: str = "auto") -> dict[str, Any]:
    """Preview Toss BUY/SELL evidence from a private inbox PDF; no ledger write.

    Bare file names only, no symlinks, maximum 20 MiB/400 pages. Account auto
    requires one configured Toss account. First binding requires a registry suffix
    match or at least five exact API matches inside observed/successful coverage,
    with zero covered unmatched rows and conflicts. detected_account reports the
    evidence path and counts; empty registry account IDs can use API overlap.
    Zero matches with API trades in period block apply.
    Stock 구매/판매 (buy/sell) rows only; bond rows
    without stable IDs are skipped with UNSUPPORTED_INSTRUMENT_NO_STABLE_ID.
    All other activities are counted in skipped_by_type and never imported.
    Missing costs remain unknown. Near-date/code duplicates conflict; preview
    reports new_inside_api_coverage, which must be zero for apply. Parse issues/conflicts/incomplete API dedup block
    apply; inspect the masked samples and correct the source document out of band.
    Preview expires after 15 minutes or a process restart (at most 16 previews).
    Dedup reads the existing collector; no new accounts/providers or orders.
    Fetched broker rows are recorded in the local append-only ledger (no broker writes).
    """
    try:
        data = _get_statement_manager().preview(broker, file, account)
        return {**_ok(data, provider="statement:toss"), "result_status": data["result_status"]}
    except StatementError as exc:
        return {**_fail(exc.code, str(exc), provider="statement:toss"), "result_status": "unavailable"}
    except Exception:
        return {**_fail("STATEMENT_PREVIEW_FAILED", "Statement preview failed; no ledger rows were written.", provider="statement:toss"), "result_status": "unavailable"}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def apply_statement_import(preview_id: str, confirm: bool = False) -> dict[str, Any]:
    """Plan or confirm a local statement-ledger import. Never places orders.

    confirm=False writes nothing and returns the plan. confirm=True requires a
    fresh unblocked preview and stores every parsed stock BUY/SELL row with file SHA256,
    parser version, page/line and row-hash provenance. Same file/account reapply is
    a no-op. Present API counterparts always win, inside or outside coverage,
    preserving their trade and settlement dates. Retained statement rows fill
    only outside coverage when no current API counterpart exists. Conflicts are
    never overwritten, and every merge reports its coverage decisions.
    Unavailable/truncated API dedup and unmatched rows inside observed API coverage
    block confirmation; no override exists. Coverage is rechecked on apply.
    Skipped activities never certify cash flows or historical completeness.
    import_id supports undo through the local store API; no undo tool is exposed.
    """
    try:
        data = _get_statement_manager().apply(preview_id, confirm)
        return {**_ok(data, provider="statement:toss"), "result_status": data["result_status"]}
    except StatementError as exc:
        return {**_fail(exc.code, str(exc), provider="statement:toss"), "result_status": "unavailable"}
    except Exception:
        return {**_fail("STATEMENT_APPLY_FAILED", "Statement import failed; conflicting rows are never overwritten.", provider="statement:toss"), "result_status": "unavailable"}


def _query_ids(response, snapshot_id=None, calculation_version=None):
    metadata = query_metadata(snapshot_id, calculation_version) if calculation_version else query_metadata(snapshot_id)
    return {**response, "meta": {**response.get("meta", {}), **metadata}}


def _ok(data: Any, provider: str = "", **meta: Any) -> dict[str, Any]:
    payload_meta = {
        "server_version": SERVER_VERSION,
        "provider": provider or None,
        "cache_age_seconds": meta.pop("cache_age_seconds", 0),
    }
    payload_meta.update({k: v for k, v in meta.items() if v is not None})
    return {"ok": True, "data": data, "error": None, "meta": payload_meta}


def _deprecated_alias(response: dict[str, Any], alias_of: str) -> dict[str, Any]:
    return {**response, "meta": {**response.get("meta", {}),
                               "deprecated": {"alias_of": alias_of, "since": "2.6.0"}}}


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
    error = classify_error(e)
    provider = error.get("provider")
    if provider:
        _record_provider_outcome(provider, e)
    logger.warning("%s", json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(),
        "request_id": request_id_context.get(), "layer": "broker" if provider else "tool",
        "provider": provider, "reason": error["kind"], "upstream_status": error["upstream_status"]}))
    result = _fail(error["code"], error["message"], provider=provider or "",
                   upstream_status=error["upstream_status"], retryable=error["retryable"])
    result["error"]["kind"] = error["kind"]
    result["error"]["retry_after_seconds"] = error["retry_after_seconds"]
    return result


def _record_provider_outcome(provider: str, exc: Exception | None = None) -> None:
    if exc is None:
        _health_status.record(provider, status="ok")
        return
    error = classify_error(exc)
    if provider == "kis" and hasattr(exc, "body"):
        error["broker_diagnostic"] = _masked_kis_diagnostic(exc)
    _health_status.record(provider, status=error["kind"], error_code=error["kind"], error=error)


set_provider_observer(_record_provider_outcome)


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


# ── Portfolio engine helpers ────────────────────────────

#: Stated once and attached to every portfolio response so the accounting basis
#: is never ambiguous to a caller.
_VALUATION_METHODOLOGY = {
    "base_currency": "KRW",
    "basis_scope": "per_account",
    "cost_basis": (
        "Holdings/summary cost_basis and unrealized P&L use broker-reported purchase cost; "
        "buy fee/tax inclusion is unknown unless explicitly labeled. Ledger replay uses moving average "
        "per (account, symbol, currency) and capitalizes reported buy fees/taxes. "
        "Aggregates sum rounded account costs and P&L; ledger_cost_basis is unavailable when no ledger "
        "is loaded or account quantities do not match. Compare labeled bases before combining P&L."
    ),
    "fx_valuation": "current spot rate (yfinance <CUR>KRW=X), applied to native values",
    "fx_realized": "broker-recorded trade rate when available, else current spot",
    "fx_decomposition": (
        "realized_pnl_krw = realized_pnl x exit rate + realized_fx_krw, where "
        "realized_fx_krw = cost_in_currency x exit rate - cost_in_krw. Requires a "
        "broker-recorded rate on both the buy and the sale. If any window sale lacks its FX part, "
        "totals.realized_fx_krw is null with FX_DECOMPOSITION_PARTIAL in "
        "realized_fx_krw_reason_codes (Toss does not report trade FX rates)."
    ),
    "settlement": (
        "KIS overseas: reference assumes executed positions and a deposit separate from "
        "buy/sell totals; nav_bridge applies those totals once. KIS domestic and Toss: "
        "unsettled inclusion and position basis are unverified. Toss cash here is buying power. "
        "Settlement-adjusted NAV is a reference, not official NAV or withdrawable cash."
    ),
    "position_grouping": "(symbol, currency) — same ticker across accounts is one position",
}

_RETURN_METHODOLOGY = {
    "simple_return": "(end NAV - start NAV - net external flows) / (start NAV + net inflows)",
    "twr": "chained sub-period returns from stored daily NAV snapshots, flow-adjusted",
    "xirr": "money-weighted annualized return over dated flows plus terminal NAV",
    "risk_stats": "flow-adjusted daily snapshot changes annualized at 252 days; requires verified flows and daily observations",
    "eligibility": "unknown external flows yield null returns and risk metrics with reason codes",
    "raw_asset_values": "asset changes/drawdown/volatility describe values, not investment returns",
    "flow_timing": "TWR assumes external flows at each snapshot interval end",
    "method_version": "flow_eligibility_v1",
}

_CONSOLIDATED_NO_PNL_DECOMPOSITION = (
    "Consolidated has no P&L decomposition: realized/unrealized P&L, fees, taxes, dividends and "
    "interest come only from live Toss/KIS holdings and the whole broker history (crypto has none), "
    "which take minutes to collect, so they are unavailable (LEDGER_NOT_PINNED; unrealized: "
    "INSUFFICIENT_INPUTS). portfolio_scope='equity' has the equity decomposition.")

_LEDGER_LIMITATIONS = [
    "Deposits, withdrawals, and cash transfers are not exposed by the Toss or KIS "
    "open APIs used here, so external flow is unknown. Flow-dependent returns and risk "
    "metrics are unavailable until a source with complete dated coverage or verified zero is supplied.",
    "Dividends, interest, and withholding tax are not available per account from "
    "either provider; DIVIDEND/INTEREST/TAX entries stay empty.",
    "Corporate actions (split, merger, spinoff, ticker change) have no API source; "
    "a split that is not reflected in broker-reported quantities will distort "
    "ledger-derived cost basis until the broker restates it.",
    "KIS domestic fills report one estimated total cost (제비용) rather than "
    "commission and tax separately, so it is recorded entirely as `fee`.",
    "Sales without purchases in the available ledger appear under `incomplete_cost_basis`; "
    "earlier history may exist outside the requested window.",
]


def _resolve_dates(start_date: str, end_date: str, default_days: int) -> tuple[str, str]:
    """Normalize an optional ISO date range, defaulting the window backwards."""
    end = (end_date or "").strip() or datetime.now().strftime("%Y-%m-%d")
    start = (start_date or "").strip() or (
        datetime.strptime(end, "%Y-%m-%d") - timedelta(days=default_days)
    ).strftime("%Y-%m-%d")
    for label, value in (("start_date", start), ("end_date", end)):
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as e:
            raise ValueError(f"{label} must be ISO YYYY-MM-DD, got {value!r}") from e
    if start > end:
        raise ValueError(f"start_date {start} is after end_date {end}")
    return start, end


def _ledger_window_metadata(start_date: str, reason: str) -> dict[str, Any]:
    defaulted = not (start_date or "").strip()
    return {"window_defaulted": defaulted,
            "window_reason_codes": [reason] if defaulted else [],
            **({"window_hint": "Only the default window was queried; pass start_date (e.g. 2024-01-01) for older history."}
               if defaulted else {})}


def _kis_source_labels() -> list[str]:
    """Every KIS portfolio source name, e.g. kis:domestic, kis:pension:domestic."""
    try:
        accounts = _get_kis_registry().accounts()
    except Exception:  # noqa: BLE001 — unconfigured KIS still resolves to its default sources
        return ["kis:domestic", "kis:overseas"]
    labels: list[str] = []
    for account in accounts:
        scopes = ["domestic", "overseas"] if account.supports_overseas else ["domestic"]
        labels.extend(kis_source(account.label, scope) for scope in scopes)
    return labels


def _resolve_accounts(account: str) -> list[str] | None:
    """Map an `account` filter to explicit account labels, or None for everything.

    Accepts "all", a provider ("toss", "kis"), a full source name ("kis:domestic",
    "kis:pension:domestic", "toss:primary"), a KIS account label ("kis:pension"),
    or a bare Toss label ("primary").
    """
    wanted = (account or "all").strip().lower()
    if wanted in ("", "all"):
        return None
    if wanted == "kis":
        return _kis_source_labels()
    if wanted == "toss":
        return [f"toss:{label}" for label in _get_toss_registry().account_labels()]
    if wanted.startswith("kis:"):
        kis_sources = _kis_source_labels()
        if wanted in kis_sources:
            return [wanted]
        # A KIS account label on its own ("kis:pension") means all of its scopes.
        scoped = [name for name in kis_sources if name.startswith(f"{wanted}:")]
        return scoped or [wanted]
    if wanted.startswith("toss:"):
        return [wanted]
    return [f"toss:{wanted}"]


def _risk_free_decimal() -> float:
    """10Y Treasury yield as a decimal for Sharpe/Sortino; 0.0 when unavailable."""
    try:
        with YFINANCE_LOCK:
            data = risk_free_rate(yf.Ticker("^TNX"))
        return float(data.get("ten_year_treasury_yield_decimal") or 0.0)
    except Exception:  # noqa: BLE001 — risk stats degrade to excess-over-zero
        return 0.0


def _count_by(items: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.account] = counts.get(item.account, 0) + 1
    return dict(sorted(counts.items()))


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
    identity_record = dict(item)
    if source == "kis_domestic":
        identity_record.setdefault("market", "KR")
    identity = instrument_metadata(symbol, identity_record, source)
    currency = str(item.get("currency") or item.get("crcy_cd") or item.get("tr_crcy_cd") or identity["currency"] or "UNKNOWN").upper()
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
    identity["currency"] = (value_currency or currency) if (value_currency or currency) != "UNKNOWN" else None
    return {
        "source": source,
        "symbol": symbol.upper(),
        "name": name,
        "currency": value_currency or currency,
        "market_value": value,
        "quantity": quantity,
        "instrument_metadata": identity,
    }


def _holding_mark_quotes(holdings: Any) -> dict[str, Any]:
    """Timed Toss quotes for every held symbol, the input of the canonical marks."""
    requested = {h.symbol.upper(): h.currency.upper() for h in holdings if h.quantity > 0}
    if not requested:
        return {}
    try:
        return _get_toss().get_mark_quotes(requested)
    except Exception:  # noqa: BLE001 — no quote source leaves every mark a labelled broker price
        return {symbol: {"reason_codes": ["QUOTE_SOURCE_UNAVAILABLE"]} for symbol in requested}


def _resolve_market(symbol: str, market: str = "auto") -> str:
    """Resolve provider master evidence; ambiguity needs an explicit market."""
    if market != "auto":
        return resolve_market(symbol, market, [])
    records: list[dict[str, Any]] = []
    try:
        master = _get_toss().get_stock_info(symbol)
        if isinstance(master, list):
            records = [row for row in master if isinstance(row, dict)]
    except Exception:
        pass
    evidence = set().union(*(market_evidence(row) for row in records if str(row.get("symbol") or "").upper() == symbol.upper()))
    if evidence:
        return resolve_market(symbol, market, records)
    # Same existing provider used by financial tools. Candidate shape only
    # selects lookups; a matching symbol and declared exchange are required.
    for candidate in lookup_symbols(symbol):
        try:
            with YFINANCE_LOCK:
                info = yf.Ticker(candidate).info
            if isinstance(info, dict) and str(info.get("symbol") or "").upper() == candidate:
                records.append(info)
        except Exception:
            continue
    return resolve_market(symbol, market, records)


def _market_required() -> dict[str, Any]:
    return _fail("MARKET_REQUIRED", 'Specify market="kr" or "us"; provider master is missing or ambiguous.')


# ════════════════════════════════════════════════════════════════
# Toss Securities Market Data Tools (9)
# ════════════════════════════════════════════════════════════════


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_quote(symbol: str) -> dict[str, Any]:
    """Get current price quote for a stock symbol (Toss Securities).

    Args:
        symbol: Stock symbol (e.g., "AAPL" for US, "005930" for Korean Samsung Electronics)

    Returns:
        Structured JSON envelope with current price data including last price, change, volume, etc.
    """
    try:
        data = _get_toss().get_prices(symbol)
        _health_status.record("toss:market_data", status="ok" if data else "ok_empty", count=len(data), complete=True)
        return _ok(data, provider="toss")
    except Exception as e:
        return _format_error(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_recent_trades(symbol: str) -> dict[str, Any]:
    """Deprecated alias of get_market_trades; kept for compatibility.

    Public market trades, not your personal fills. For your own executions use
    get_toss_trade_history or get_kis_trade_history.
    """
    return _deprecated_alias(get_market_trades(symbol), "get_market_trades")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_candles(
    symbol: str,
    interval: Literal["1d", "1m"] = "1d",
    count: int = 100,
    adjusted: bool = True,
    before: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """Get candlestick chart data (OHLCV) for a stock (Toss).

    Args:
        symbol: Stock symbol (e.g., "AAPL", "005930")
        interval: Candle interval — "1d" for daily or "1m" for 1-minute
        count: Number of candles to retrieve (max 200, default 100)
        adjusted: Request provider adjustments (default True); actual basis unverified.
        before: Provider cursor; inclusive local timestamp filter, upstream boundary unverified.
        start_date/end_date: Inclusive YYYY-MM-DD candle-local labels. One page per call.

    Returns:
        Raw provider payload for existing calls. Paging calls add returned_range,
        next_cursor, complete (page chain only, not historical coverage), limit,
        result_status/reason_codes and source_metadata. Merge pages ascending by
        timestamp and deduplicate; adjusted prices are not verified total returns.
    """
    try:
        kwargs = {"interval": interval, "count": count, "adjusted": adjusted}
        if any(value is not None for value in (before, start_date, end_date)):
            kwargs.update(before=before, start_date=start_date, end_date=end_date)
        data = _get_toss().get_candles(symbol, **kwargs)
        return _ok(data)
    except Exception as e:
        return _format_error(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_market_series(
    symbols: list[str],
    period: Literal["1mo", "3mo", "6mo", "ytd", "1y"] = "1mo",
    start_date: str | None = None,
    end_date: str | None = None,
    benchmark: str | None = None,
    base: float = 100,
) -> dict[str, Any]:
    """Pure Toss daily market closes on one intersection calendar, indexed to base.

    Native currency only; no FX conversion, dividend reinvestment or analysis.
    For analysis use ticker-research's compare_price_performance,
    get_technical_indicators or get_eps_scenarios.
    "내 보유 종목을 코스피와 비교" means get_consolidated_holdings first,
    then this tool with the held symbols and benchmark="KOSPI".

    Args:
        symbols: 1–8 KR six-digit codes or US tickers; at most eight distinct
            symbols including benchmark. Failed symbols are listed in data.errors.
        period: 1mo (default), 3mo, 6mo, ytd or 1y; explicit dates override period.
        start_date: Inclusive YYYY-MM-DD; maximum ten-year window.
        end_date: Inclusive YYYY-MM-DD, default today; requires start_date.
        benchmark: Symbol or ETF proxy alias: KOSPI=KODEX 200 ETF (069500),
            KOSDAQ=KODEX 코스닥150 ETF (229200), S&P500/SPX=SPY, NASDAQ=QQQ.
            Every alias is labelled proxy, not the actual index.
        base: Positive index base (default 100), at most 1000000.

    Adjusted candles are requested, but adjustment is unverified. No filling.
    Current-session bars wait for KRX 15:30 / US 16:00 ET plus a ten-minute
    Toss margin. At most 260 points per series; longer/large responses are
    weekly-sampled and labelled, retaining period endpoints. Less than 100 KB.
    """
    try:
        data = build_market_series(_get_toss(), symbols, period, start_date, end_date, benchmark, base)
        if not data["series"]:
            return {**_fail("MARKET_SERIES_UNAVAILABLE", "No common confirmed series available.", provider="toss"), "data": data}
        return _ok(data, provider="toss")
    except SeriesError as exc:
        return _fail(exc.code, str(exc), provider="toss", retryable=False)
    except Exception as exc:
        return _format_error(exc)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_stock_info(symbol: str, market: Literal["auto", "us", "kr"] = "auto") -> dict[str, Any]:
    """Get basic stock information (name, market, ISIN, status) (Toss).

    Args:
        symbol: Stock symbol (e.g., "AAPL", "005930")
        market: Optional explicit market when source identity is ambiguous.

    Returns:
        Structured JSON envelope with stock master data.
    """
    try:
        data = _get_toss().get_stock_info(symbol)
        if isinstance(data, list):
            data = [{**row, "instrument_metadata": instrument_metadata(str(row.get("symbol") or symbol), row, "toss:stock_master", market)} if isinstance(row, dict) else row for row in data]
        return _ok(data)
    except Exception as e:
        return _format_error(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_toss_accounts() -> dict[str, Any]:
    """List configured Toss account labels without exposing credentials."""
    try:
        registry = _get_toss_registry()
        labels = registry.account_labels()
        # Additive: each label's masked last 4 so callers bind by account number, not label.
        masked: dict[str, str] = {}
        for label in labels:
            try:
                value = registry.client_for(label).get_account_masked()
            except Exception:
                continue  # A label that cannot be resolved is omitted, never guessed.
            if value:
                masked[label] = value
        return _ok({"accounts": labels, "default": "primary", "count": len(labels), "masked": masked}, provider="toss")
    except Exception as e:
        return _format_error(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_toss_trade_history(limit: int = 50, account: str = "primary", recovery: bool = False,
                          start_date: str = "", end_date: str = "", resume_cursor: str = "",
                          page_size: int = 100) -> dict[str, Any]:
    """Get recent trade history (filled orders) from Toss Securities (READ-ONLY).

    Recovery defaults to the last 90 days; pass start_date (e.g. 2024-01-01) to query older history.

    Args:
        limit: Maximum number of trades to retrieve (default 50)
        recovery: opt into the normalized, windowed ledger response (no row-limit).
            Omitted/false preserves the existing raw response. Positive partial
            fills are included in recovery. limit applies only to the raw branch.
        start_date: recovery start, ISO YYYY-MM-DD (default 90 days ago)
        end_date: recovery end, ISO YYYY-MM-DD (default today)
        resume_cursor: opaque recovery cursor; repeat the original filters
        page_size: recovery page size, 1–100

    Returns:
        Structured JSON envelope with recent filled orders.
    Fetched broker rows are recorded in the local append-only ledger (no broker writes).
    """
    try:
        if recovery or resume_cursor:
            return get_all_transactions(start_date, end_date,
                "toss" if account == "all" else f"toss:{account or 'primary'}",
                resume_cursor=resume_cursor, page_size=page_size)
        registry = _get_toss_registry()
        labels = registry.account_labels() if account == "all" else [account or "primary"]
        budget = PersistenceBudget()
        results, sources = [], []
        for label in labels:
            source = f"toss:{label}:history"
            health_error = None
            try:
                client = registry.client_for(label)
                rows = client.get_trade_history(limit=limit)
                annotated = annotate_rows(rows, "toss", getattr(client, "journal_account_key", f"toss:{label}"))
                truncated = getattr(rows, "truncated", len(rows) >= limit)
                metadata = source_metadata(source, len(rows), complete=not truncated, truncated=truncated,
                    reason=getattr(rows, "truncation_reason", "row_limit" if truncated else None))
                _persist_broker_history("toss", client, f"toss:{label}", "", rows, metadata, budget)
                results.append({"account": label, "data": annotated, "error": None})
            except Exception as exc:
                error = _format_error(exc)["error"]
                health_error = error
                metadata = source_metadata(source, None, complete=False, error_code=error["kind"])
                results.append({"account": label, "data": None, "error": error})
            sources.append(metadata)
            _health_status.record(source, status=metadata["status"], count=metadata["result_count"],
                complete=metadata["complete"], error_code=metadata["error_code"], truncated=metadata["truncated"],
                error=health_error)
        data = {"accounts": results} if account == "all" else results[0]["data"]
        if account != "all" and results[0]["error"]:
            return {"ok": False, "data": None, "error": results[0]["error"], "meta": {"sources": sources, "complete": False}}
        return _ok(data, provider="toss", account=account, sources=sources,
                   complete=bool(sources) and all(x["complete"] is True for x in sources))
    except Exception as e:
        return _format_error(e)


# ════════════════════════════════════════════════════════════════
# KIS (Korea Investment Securities) Tools (7)
# ════════════════════════════════════════════════════════════════


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def get_kis_accounts() -> dict[str, Any]:
    """List configured KIS accounts without exposing account numbers or keys.

    Each entry carries the label to pass as `account` on the other KIS tools, its
    계좌상품코드, and `kind`: "pension" for a 연금저축(22)/퇴직연금(29) account,
    which holds domestic products only, or "general" otherwise.

    Returns:
        Structured JSON envelope with account labels and their metadata.
    """
    try:
        registry = _get_kis_registry()
        return _ok(
            {
                "accounts": registry.accounts_metadata(),
                "default": registry.default_label(),
                "count": len(registry.account_labels()),
            },
            provider="kis",
        )
    except Exception as e:
        return _format_error(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_kis_domestic_balance(account: str = "") -> dict[str, Any]:
    """Get Korean domestic stock balance from KIS (READ-ONLY).

    Shows all Korean stocks held with quantity, average price, current price,
    and P&L. Also includes cash balance.

    Args:
        account: Account label from get_kis_accounts() — a 연금저축 account is
            queried the same way. Empty uses the default account; "all" queries
            every configured account.

    Returns:
        Structured JSON envelope with domestic balance data.
    """
    try:
        data = _for_kis_accounts(account, lambda client: client.get_domestic_balance())
        return _ok(data, account=account or _get_kis_registry().default_label())
    except Exception as e:
        return _format_error(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_kis_overseas_balance(account: str = "") -> dict[str, Any]:
    """Get overseas (US/JP/HK) stock balance from KIS (READ-ONLY).

    Shows all overseas stocks held including US, Japan, Hong Kong, China.
    Includes cash balance per currency. Pension accounts (연금저축/퇴직연금) cannot
    hold overseas securities and report OVERSEAS_NOT_SUPPORTED instead.

    Args:
        account: Account label from get_kis_accounts(). Empty uses the default
            account; "all" queries every configured account.

    Returns:
        Structured JSON envelope with overseas balance data.
    """
    try:
        data = _for_kis_accounts(account, _kis_overseas_balance)
        return _ok(data, account=account or _get_kis_registry().default_label())
    except Exception as e:
        return _format_error(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
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
            return _ok({"primary_provider": "kis", "primary_error": classify_error(e)["message"], "fallback_provider": "toss", "fallback_data": fallback}, provider="kis_fallback_toss")
        except Exception:
            return _format_error(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
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
            return _ok({"primary_provider": "kis", "primary_error": classify_error(e)["message"], "fallback_provider": "toss", "fallback_data": fallback}, provider="kis_fallback_toss")
        except Exception:
            return _format_error(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_kis_trade_history(start_date: str = "", end_date: str = "", account: str = "",
                         recovery: bool = False, resume_cursor: str = "") -> dict[str, Any]:
    """Get trade history (execution records) from KIS (READ-ONLY).

    Recovery defaults to the last 30 days; pass start_date (e.g. 2024-01-01) to query older history.

    Fetches both domestic and overseas trade history.
    recovery=True opts into the normalized ledger, bounded 90-day windows,
    coverage and failed-chunk-only resume. Omitted/false preserves raw output.
    resume_cursor must repeat the original dates and account.

    Args:
        start_date: Start date in YYYYMMDD format (default: 30 days ago)
        end_date: End date in YYYYMMDD format (default: today)
        account: Account label from get_kis_accounts(). Empty uses the default
            account; "all" queries every configured account.

    Returns:
        Structured JSON envelope with domestic and overseas trade records.
    Fetched broker rows are recorded in the local append-only ledger (no broker writes).
    """
    try:
        if recovery or resume_cursor:
            def iso(value):
                value = (value or "").strip()
                return datetime.strptime(value, "%Y%m%d").date().isoformat() if value and "-" not in value else value
            label = account or _get_kis_registry().default_label()
            accounts = "kis" if label == "all" else f"kis:{label}"
            start, end = _resolve_dates(iso(start_date), iso(end_date), default_days=30)
            response = get_all_transactions(start, end, accounts, resume_cursor=resume_cursor)
            if isinstance(response.get("data"), dict):
                response["data"].update(_ledger_window_metadata(start_date, "DEFAULT_WINDOW_30D"))
            return response
        if not end_date:
            end_date = datetime.now().strftime("%Y%m%d")
        if not start_date:
            start_date = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
        start, end = datetime.strptime(start_date, "%Y%m%d"), datetime.strptime(end_date, "%Y%m%d")
        if start > end:
            raise ValueError("Date range reversed")
        registry = _get_kis_registry()
        labels = registry.account_labels() if account == "all" else [account or registry.default_label()]
        budget = PersistenceBudget()
        results, sources = [], []
        for label in labels:
            try:
                client = registry.client_for(label)
            except Exception as exc:
                error = _format_error(exc)["error"]
                for market in ["domestic", "overseas"]:
                    sources.append(source_metadata(f"kis:{label}:{market}:history", None, complete=False,
                        error_code=error["kind"], start=start_date, end=end_date))
                results.append({"account": label, "data": None, "error": error})
                continue
            payload = {}
            for market, method in [("domestic", client.get_domestic_trade_history), ("overseas", client.get_overseas_trade_history)]:
                source = f"kis:{label}:{market}:history"
                health_error = None
                if market == "overseas" and not client.supports_overseas:
                    payload[market] = dict(_OVERSEAS_UNSUPPORTED)
                    sources.append(source_metadata(source, None, complete=None, supported=False, start=start_date, end=end_date))
                    continue
                try:
                    value = dict(method(start_date, end_date))
                    value["trades"] = annotate_rows(value.get("trades", []), "kis",
                        getattr(client, "journal_account_key", f"kis:{label}"), "KR" if market == "domestic" else "")
                    truncated = bool(value.get("truncated"))
                    metadata = source_metadata(source, len(value["trades"]), complete=not truncated, truncated=truncated,
                        start=start_date, end=end_date, reason="page_limit" if truncated else None)
                    _persist_broker_history("kis", client, f"kis:{label}:{market}", market, value, metadata, budget)
                    value["source_metadata"] = metadata
                    payload[market] = value
                except Exception as exc:
                    error = _format_error(exc)["error"]
                    health_error = dict(error)
                    if hasattr(exc, "body"):
                        health_error["broker_diagnostic"] = _masked_kis_diagnostic(exc)
                    metadata = source_metadata(source, None, complete=False, error_code=error["kind"], start=start_date, end=end_date)
                    payload[market] = {"trades": None, "error": error, "source_metadata": metadata}
                sources.append(metadata)
                _health_status.record(source, status=metadata["status"], count=metadata["result_count"],
                    complete=metadata["complete"], error_code=metadata["error_code"], truncated=metadata["truncated"],
                    error=health_error)
            results.append({"account": label, "data": payload, "error": None})
        data = {"accounts": results} if account == "all" else results[0]["data"]
        applicable = [x for x in sources if x["supported"]]
        if account != "all" and results[0]["error"]:
            return {"ok": False, "data": None, "error": results[0]["error"],
                    "meta": {"provider": "kis", "sources": sources, "complete": False}}
        return _ok(data, provider="kis", account=account or registry.default_label(), sources=sources,
                   complete=bool(applicable) and all(x["complete"] is True for x in applicable))
    except Exception as e:
        return _format_error(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_kis_cash_balance(account: str = "") -> dict[str, Any]:
    """Get available cash from KIS domestic and overseas accounts (READ-ONLY).

    Returns KRW and USD cash available for trading.

    Args:
        account: Account label from get_kis_accounts(). Empty uses the default
            account; "all" queries every configured account.

    Returns:
        Structured JSON envelope with domestic and overseas cash balances.
    """
    try:
        def _cash(client: KISClient) -> dict[str, Any]:
            return {
                "krw": client.get_domestic_orderable_cash(),
                "usd": (
                    client.get_overseas_orderable_cash()
                    if client.supports_overseas
                    else _OVERSEAS_UNSUPPORTED
                ),
            }

        return _ok(
            _for_kis_accounts(account, _cash),
            provider="kis",
            account=account or _get_kis_registry().default_label(),
        )
    except Exception as e:
        return _format_error(e)


# ════════════════════════════════════════════════════════════════
# SEC EDGAR Financials & Insider Tools (2)
# ════════════════════════════════════════════════════════════════


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_sec_financials(symbol: str) -> dict[str, Any]:
    """Get SEC financials for US stocks: up to five annual fiscal years, quarterly
    facts, derived TTM, earnings quality, and segment revenue when available.
    Missing or incomplete data is marked unavailable or carries warnings;
    quarters/TTM/segments are not guaranteed for every issuer.

    Uses CompanyFacts and filings (10-K/10-Q; applicable amended forms).
    No API key required. Uses SEC_USER_AGENT for fair access.

    Args:
        symbol: US stock ticker (e.g., "AAPL", "MSFT")

    Returns:
        Structured JSON envelope with annuals, quarters, TTM, quality and segment data.
    """
    try:
        data = _get_sec().get_financials(symbol)
        return _ok(data)
    except Exception as e:
        return _format_error(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_ttm_financials(symbol: str) -> dict[str, Any]:
    """Get SEC 10-Q/FY-derived TTM financials for a US stock."""
    try:
        data = _get_sec().get_ttm_financials(symbol)
        return _ok(data, provider="sec")
    except Exception as e:
        return _format_error(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_risk_free_rate() -> dict[str, Any]:
    """Get the current 10Y US Treasury yield for WACC discount-rate inputs."""
    try:
        with YFINANCE_LOCK:
            return _ok(risk_free_rate(yf.Ticker("^TNX")), provider="yfinance")
    except Exception as e:
        return _format_error(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_binance_futures_quote(symbol: str) -> dict[str, Any]:
    """Get Binance USD-M Futures latest price ticker without an API key."""
    try:
        return _ok(_get_binance().price_ticker(symbol), provider="binance")
    except Exception as e:
        return _format_error(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_binance_futures_mark_price(symbol: str) -> dict[str, Any]:
    """Get Binance USD-M Futures mark/index price and latest funding rate."""
    try:
        return _ok(_get_binance().mark_price(symbol), provider="binance")
    except Exception as e:
        return _format_error(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_binance_funding_rate(symbol: str, limit: int = 10) -> dict[str, Any]:
    """Deprecated alias of get_binance_funding_rate_history; kept for compatibility.

    Public funding rates, not account funding fees you paid.
    No personal account or payment history is queried.
    """
    return _deprecated_alias(get_binance_funding_rate_history(symbol, limit), "get_binance_funding_rate_history")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_crypto_futures_snapshot(symbol: str) -> dict[str, Any]:
    """Get Binance USD-M Futures price, mark, funding, and open-interest snapshot."""
    try:
        return _ok(_get_binance().snapshot(symbol), provider="binance")
    except Exception as e:
        return _format_error(e)


# Clearer public-market names (new tools)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_market_trades(symbol: str) -> dict[str, Any]:
    """Stocks only (Toss); crypto pairs are rejected. Returns public market tape.

    For crypto use get_binance_futures_quote or get_crypto_futures_snapshot.
    For your own executions use get_toss_trade_history or get_kis_trade_history.
    """
    try:
        normalized = symbol.strip().upper()
        if normalized.endswith(("USDT", "USDC", "BUSD", "FDUSD", "TUSD")) or "PERP" in normalized:
            return _fail("UNSUPPORTED_ASSET_CLASS",
                "Stocks only (Toss). For crypto use get_binance_futures_quote or get_crypto_futures_snapshot.",
                provider="toss")
        return _ok(_get_toss().get_recent_trades(symbol))
    except Exception as e:
        return _format_error(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_binance_funding_rate_history(symbol: str, limit: int = 10) -> dict[str, Any]:
    """Get public Binance USD-M Futures funding rate history without an API key.

    These are public funding rates, not account funding fees you paid.
    No personal account or payment history is queried.
    """
    try:
        return _ok(_get_binance().funding_rates(symbol, limit=limit), provider="binance")
    except Exception as e:
        return _format_error(e)


# ════════════════════════════════════════════════════════════════
# Crypto futures (account), read-only
# ════════════════════════════════════════════════════════════════

_crypto_futures_accounts = FuturesAccounts(
    ledger_observer=lambda operation, venue, client, output: observe_futures(operation, venue, client, output, _get_nav_store().db_path))


def _account_key_store():
    # Crypto account_id = futures_risk's key over its DB-persisted salt, so positions,
    # fills, funding and the summary's stored crypto NAV rows join on one opaque key.
    # Read tools open the salt `?mode=ro`; only the risk capture (RiskStore) writes it.
    from futures_risk import RiskKeys

    return RiskKeys(_get_nav_store().db_path)


configure_account_keys(_account_key_store)


def _perp_exposure_inputs():
    """Open crypto futures positions (exposure only, never NAV) and, when any is open,
    the Upbit stablecoin KRW quotes crypto NAV uses to convert their notional."""
    try:
        perps = _crypto_futures_accounts.query("positions", "all")
    except Exception as exc:  # noqa: BLE001 — an unread perp leg marks the view partial, never fails the tool
        return {"venues": {}, "reason_codes": [exc.code if isinstance(exc, FuturesError) else "PERPS_READ_FAILED"]}, {}
    if not any(result.get("positions") for result in perps["venues"].values()):
        return perps, {}
    from crypto_nav_capture import StablecoinQuotes
    try:
        return perps, StablecoinQuotes().read()
    except Exception as exc:  # noqa: BLE001 — legs stay unconverted with STABLECOIN_KRW_RATE_UNAVAILABLE
        logging.getLogger(__name__).warning("stablecoin KRW quotes unavailable for perp exposure: %s",
                                            getattr(exc, "code", "CRYPTO_QUOTE_UNAVAILABLE"))
        return perps, {}


def _crypto_futures_query(operation, venue, start_date="", end_date="", symbol=""):
    try:
        data = _crypto_futures_accounts.query(operation, venue, start_date, end_date, symbol)
        return _ok(data, provider="crypto_futures_accounts", result_status=data["result_status"],
                   source_status=data["source_status"], coverage=data["coverage"], reason_codes=data["reason_codes"])
    except FuturesError as exc:
        return _fail(exc.code, str(exc), provider="crypto_futures_accounts")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_crypto_futures_positions(venue: Literal["all", "binance", "hyperliquid"] = "all") -> dict[str, Any]:
    """Read your open crypto futures positions and native account balances.

    Separate account tools; not included in portfolio NAV or holdings. Unknown
    leverage, margin mode and mark prices stay null. Binance wallet balance
    excludes unrealized P&L; Hyperliquid accountValue includes it. Hyperliquid
    covers the first perpetual dex only, not unified spot equity or other dexs.
    Inspect per-venue source_status, coverage and result_status; one missing
    configuration does not prevent the other venue from answering.
    """
    return _crypto_futures_query("positions", venue)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_crypto_futures_fills(
    venue: Literal["all", "binance", "hyperliquid"], start_date: str,
    end_date: str, symbol: str = "",
) -> dict[str, Any]:
    """Read your own crypto futures fills, fees and fee assets in a UTC date window.

    Dates are inclusive YYYY-MM-DD. Binance queries one symbol at a time in
    windows of at most seven days. Empty symbol discovers symbols from income
    history and current positions and explicitly reports incomplete coverage.
    Binance history is limited to the past three months. Hyperliquid excludes
    spot fills and is subject to venue history limits. Page limits or dense
    timestamps are reported as truncated. Values stay in their native asset.
    Fetched broker rows are recorded in the local append-only ledger (no broker writes).
    """
    return _crypto_futures_query("fills", venue, start_date, end_date, symbol)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_crypto_futures_funding_payments(
    venue: Literal["all", "binance", "hyperliquid"], start_date: str,
    end_date: str, symbol: str = "",
) -> dict[str, Any]:
    """Read actual crypto futures funding you paid or received, signed, by asset.

    This is personal account funding, distinct from get_binance_funding_rate_history (public rate),
    which returns a public market rate. Negative amounts are paid; positive
    amounts are received. Inclusive YYYY-MM-DD dates use UTC. Totals retain
    partial/unavailable status when coverage or amounts are missing. No KRW
    conversion or portfolio integration. Inspect source_status and coverage.
    Fetched broker rows are recorded in the local append-only ledger (no broker writes).
    """
    return _crypto_futures_query("funding", venue, start_date, end_date, symbol)


# ════════════════════════════════════════════════════════════════
# Help / Usage Tool
# ════════════════════════════════════════════════════════════════


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def get_invest_mcp_help(topic: str = "overview") -> dict[str, Any]:
    """Get usage guide for this K-invest connector.

    Call this tool when the user asks what this connector can do, how to use it,
    which investment-data tools are available, or examples of supported queries.

    Args:
        topic: One of "overview", "quotes", "portfolio", "financials", "futures", "futures_account", "diagnostics", "examples", "all".

    Returns:
        Markdown usage guide for the K-invest tools.
    """
    return _ok(build_help(mcp._tool_manager.list_tools(), topic), provider="k-invest")


# ── Main ────────────────────────────────────────────────

def _quote_price(value: Any) -> float | None:
    try:
        price = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return price if 0 < price < float("inf") else None  # NaN fails the comparison


def _toss_quote_timing(symbol: str, quote: Any) -> dict[str, Any]:
    """Price, own timestamp and session of the Toss row for `symbol`; nulls plus reasons otherwise."""
    rows = quote if isinstance(quote, list) else [quote]
    row = next((r for r in rows if isinstance(r, dict) and str(r.get("symbol") or "").upper() == symbol.upper()), None)
    if row is None:
        return {"price": None, "currency": None, "priced_at": None, "session": None, "reason_codes": ["MISSING_TOSS_QUOTE"]}
    price, currency = _quote_price(row.get("lastPrice")), str(row.get("currency") or "").upper() or None
    stamp = row.get("timestamp")
    session = portfolio_engine.mark_session(currency or "", stamp)
    priced_at = stamp if session is not None else None
    reasons = ([] if price is not None else ["MISSING_PRICE"]) + ([] if priced_at else ["UNKNOWN_QUOTE_TIME"])
    return {"price": price, "currency": currency, "priced_at": priced_at, "session": session, "reason_codes": reasons}


def _kis_quote_timing(quote: Any, currency: str) -> dict[str, Any]:
    """KIS price TRs carry no price time: the quote is labelled, never given the read time."""
    price = _quote_price(quote.get("price")) if isinstance(quote, dict) else None
    return {"price": price, "currency": currency if price is not None else None, "priced_at": None, "session": None,
            "reason_codes": ([] if price is not None else ["MISSING_PRICE"]) + ["UNKNOWN_QUOTE_TIME"]}


def _quote_comparison(toss: dict[str, Any], kis: dict[str, Any]) -> dict[str, Any]:
    reasons = []
    diff = None
    if toss["price"] is None or kis["price"] is None:
        reasons.append("MISSING_PRICE")
    elif toss["currency"] != kis["currency"]:
        reasons.append("CURRENCY_MISMATCH")
    else:
        diff = round((toss["price"] - kis["price"]) / kis["price"] * 1e4, 2)
    same_time = toss["priced_at"] is not None and toss["priced_at"] == kis["priced_at"]
    if not same_time:
        reasons.append("QUOTE_TIMES_NOT_ALIGNED")
    return {"toss_minus_kis_bp": diff, "same_priced_at": same_time,
            "toss_session": toss["session"], "kis_session": kis["session"], "reason_codes": reasons}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def compare_quotes(symbol: str, exchange: Literal["NASDAQ", "NYSE", "AMEX", "TKSE", "TSE"] = "NASDAQ", market: Literal["auto", "us", "kr"] = "auto") -> dict[str, Any]:
    """Compare Toss and KIS quotes with provider/session metadata."""
    try:
        market = _resolve_market(symbol, market)
        toss_quote = _get_toss().get_prices(symbol)
        kis_error = None
        try:
            kis_quote = _get_kis().get_domestic_quote(symbol) if market == "kr" else _get_kis().get_overseas_quote(symbol, exchange=exchange)
        except Exception as e:
            kis_quote = None
            kis_error = classify_error(e)["message"]
        observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        toss_timing = _toss_quote_timing(symbol, toss_quote)
        kis_currency = "KRW" if market == "kr" else "JPY" if exchange in ("TKSE", "TSE") else "USD"
        kis_timing = _kis_quote_timing(kis_quote, kis_currency)
        return _ok({
            "symbol": symbol.upper(),
            "quotes": [
                {"provider": "toss", "data": toss_quote, "price_type": "last_trade_or_provider_current",
                 **toss_timing, "observed_at": observed_at},
                {"provider": "kis", "data": kis_quote, "price_type": "broker_quote", "error": kis_error,
                 **kis_timing, "observed_at": observed_at},
            ],
            "comparison": _quote_comparison(toss_timing, kis_timing),
            "warning": "Provider prices may differ by venue, session, delay, or regular/after-market basis.",
            "fallback_used": kis_error is not None,
        }, provider="composite")
    except MarketResolutionError:
        return _market_required()
    except Exception as e:
        return _format_error(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_stock_snapshot(symbol: str, market: Literal["auto", "us", "kr"] = "auto") -> dict[str, Any]:
    """Return a compact stock snapshot with quotes, financial metrics, provider analyst consensus, and insider data."""
    try:
        market = _resolve_market(symbol, market)
        quote = compare_quotes(symbol, market=market)
        metrics_symbol = f"{symbol}.KS" if market == "kr" else symbol
        metrics = _get_yf().get_key_metrics(metrics_symbol)
        insider_error = None
        try:
            insider = None if market == "kr" else _get_sec().get_insider_trades(symbol, days_back=180, detail_level="summary")
        except NotConfiguredError as e:
            insider = None
            insider_error = _format_error(e)["error"]
        data = {
            "symbol": symbol.upper(),
            "quote_comparison": quote.get("data"),
            "key_metrics": metrics,
            "insider_summary": insider.get("summary") if isinstance(insider, dict) else None,
            "data_warnings": ["Snapshot combines providers with different sessions and update cadences."],
        }
        if insider_error is not None:
            data["insider_error"] = insider_error
        return _ok(data, provider="composite")
    except MarketResolutionError:
        return _market_required()
    except Exception as e:
        return _format_error(e)


# ════════════════════════════════════════════════════════════════
# Portfolio Engine — consolidated ledger, holdings, and performance (8)
# ════════════════════════════════════════════════════════════════


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_all_transactions(
    start_date: str = "",
    end_date: str = "",
    account: str = "all",
    symbol: str = "",
    detail_level: Literal["summary", "full"] = "full",
    resume_cursor: str = "",
    page_size: int = 100,
    reconcile: bool = True,
) -> dict[str, Any]:
    """Unified transaction ledger across every Toss and KIS account (READ-ONLY).

    Defaults to the last 90 days; pass start_date (e.g. 2024-01-01) to query older history.

    Normalizes Toss orders and KIS domestic/overseas trade history into one schema:
    each entry carries tx_type, trade_date, settle_date, symbol, market, currency,
    quantity, price, gross_amount, fee, tax, and a signed net_amount (negative when
    cash leaves the account). `price` and `price_native` are in row currency;
    `price_base` uses only recorded trade FX (null if missing). Provenance/version
    and price_validation identify source fields and spot-stock rounding breaks;
    broker gross is preserved. Cost status distinguishes unreported from zero.

    The schema also covers DEPOSIT, WITHDRAWAL, DIVIDEND, INTEREST, FEE, TAX, FX,
    TRANSFER, SPLIT, SPINOFF, MERGER, and TICKER_CHANGE, but neither the Toss nor
    the KIS open API exposes those records, so only BUY/SELL are populated today —
    see `limitations` in the response.

    Args:
        start_date: ISO YYYY-MM-DD (default: 90 days ago)
        end_date: ISO YYYY-MM-DD (default: today)
        account: "all", "toss", "kis", or a specific label ("toss:primary",
            "kis:domestic", "kis:overseas", "kis:pension:domestic")
        symbol: optional symbol filter
        detail_level: "summary" returns aggregates only, "full" includes entries
        resume_cursor: process-local opaque cursor, valid for 30 minutes; repeat
            original dates/account/symbol/page_size. Only failed chunks are read.
        page_size: Toss page size, 1–100; windows are at most 90 days
        reconcile: compare ledger quantities to current holdings (default true)

    fetch_complete means requested pages/windows were fetched; earlier history
    may exist outside that window. historical_coverage stays unknown.
    Quantity matches likewise do not prove historical coverage. result_status
    distinguishes unavailable sources from successful empty reads.
    meta query_id/dataset_id/snapshot_id/calculation_version identify this live
    query; dataset_id is null until export_portfolio_dataset freezes inputs.

    Returns:
        Structured JSON envelope with the normalized ledger.
    Fetched broker rows are recorded in the local append-only ledger (no broker writes).
    """
    try:
        start, end = _resolve_dates(start_date, end_date, default_days=90)
        accounts = ["kis:domestic", "kis:overseas"] if account == "kis:primary" else _resolve_accounts(account)
        collector = _get_collector()
        options = {"resume_cursor": resume_cursor, "page_size": page_size} if resume_cursor or page_size != 100 else {}
        collection = collector.collect_transactions(start, end, accounts, symbol or None, **options)
        if reconcile:
            positions = collector.collect_positions(accounts)
            reconcile_collections(collection, positions, symbol or None)
        transactions = collection.transactions
        for source, metadata in collection.source_metadata.items():
            _health_status.record(source, status=metadata["status"], count=metadata["result_count"],
                complete=metadata["complete"], error_code=metadata["error_code"], truncated=metadata["truncated"],
                error=(collection.health.get(source) or {}).get("last_error"))
        by_type: dict[str, int] = {}
        by_account: dict[str, int] = {}
        for tx in transactions:
            by_type[tx.tx_type.value] = by_type.get(tx.tx_type.value, 0) + 1
            by_account[tx.account] = by_account.get(tx.account, 0) + 1
        data: dict[str, Any] = {
            **_ledger_window_metadata(start_date, "DEFAULT_WINDOW_90D"),
            "period": {"start": start, "end": end},
            "account_filter": account,
            "transaction_count": len(transactions),
            "count_by_type": dict(sorted(by_type.items())),
            "count_by_account": dict(sorted(by_account.items())),
            "sources_ok": collection.ok_sources,
            "sources_failed": collection.failed_sources,
            "truncated_sources": collection.truncated_sources,
            "complete": bool(collection.health) and not collection.truncated_sources and not collection.failed_sources,
            "health": collection.health,
            "source_metadata": collection.source_metadata,
            "limitations": _LEDGER_LIMITATIONS,
            "requested_window": collection.requested_window,
            "observed_window": collection.observed_window,
            "fetch_complete": collection.fetch_complete,
            "historical_coverage": collection.historical_coverage,
            "failed_chunks": collection.failed_chunks,
            "resume_cursor": collection.resume_cursor,
            "reconciliation": collection.reconciliation or {"status": "unavailable", "reason_codes": ["RECONCILIATION_NOT_REQUESTED"]},
        }
        if getattr(collection, "ledger_persistence", None):
            data["ledger_persistence"] = collection.ledger_persistence
        if collection.truncated_sources:
            data["warning"] = (
                "Broker paging cap reached for "
                f"{', '.join(collection.truncated_sources)} — this ledger is incomplete. "
                "Narrow start_date/end_date and re-run."
            )
        if detail_level == "full":
            data["transactions"] = [tx.to_dict() for tx in transactions]
        data["result_status"] = portfolio_engine.collection_result_status([collection], len(transactions))
        if data["result_status"] not in {"unavailable", "partial"} and collection.reconciliation.get("reconciliation_breaks"):
            data["result_status"] = "partial"
        return _query_ids({**_ok(data, provider="composite", fetched_at=collection.fetched_at), "result_status": data["result_status"]})
    except Exception as e:
        return {**_format_error(e), "result_status": "unavailable"}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_consolidated_holdings(detail_level: Literal["summary", "full"] = "summary",
                              reconcile: bool = False) -> dict[str, Any]:
    """Every Toss and KIS position merged per symbol, in native currency and KRW.

    Positions are grouped by (symbol, currency), so the same ticker held in several
    accounts becomes one row with a quantity-weighted average price derived from
    summed cost basis. Each row reports quantity, avg_price, cost_basis,
    market_value, unrealized_pnl(_pct), the holding accounts, and *_krw conversions
    at current spot. Existing cost_basis remains broker-reported, with unknown buy
    fee/tax inclusion. broker_reported_cost_basis labels that basis; ledger_cost_basis
    is null with ledger_not_available because this tool does not fetch history.
    meta query_id/dataset_id/snapshot_id/calculation_version are additive; this
    live read does not create an export (dataset_id is null).

    Args:
        detail_level: "summary" returns the top 15 positions, "full" returns all
            with per-account detail.
        reconcile: opt into an ALL-to-today ledger read and quantity reconciliation.
            Default false keeps the existing holdings-only collection behavior.

    Returns:
        Structured JSON envelope with consolidated holdings.
    Fetched broker rows are recorded in the local append-only ledger (no broker writes).
    """
    try:
        collection = _get_collector().collect_positions()
        fx = _get_collector().build_fx({h.currency for h in collection.holdings})
        data = portfolio_engine.consolidate_holdings(collection.holdings, fx,
                                                     quotes=_holding_mark_quotes(collection.holdings))
        # Additive identity block: no ledger/source normalization changes and no
        # extra provider fetches. Existing grouping and valuation remain intact.
        for position in data["positions"]:
            position["instrument_metadata"] = instrument_metadata(position["symbol"], position, "broker:holdings")
        data["reconciliation"] = {"status": "unavailable", "reason_codes": ["RECONCILIATION_NOT_REQUESTED"]}
        if reconcile:
            ledger_start, ledger_end = portfolio_engine.resolve_period("ALL")
            ledger = _get_collector().collect_transactions(ledger_start, ledger_end)
            data["reconciliation"] = reconcile_collections(ledger, collection)
        data.update(
            {
                "sources_ok": collection.ok_sources,
                "sources_failed": collection.failed_sources,
                "truncated_sources": collection.truncated_sources,
                "health": collection.health,
                "methodology": {**_VALUATION_METHODOLOGY, "fx_valuation": FX_VALUATION_NOTE},
            }
        )
        if detail_level == "summary":
            for position in data["positions"]:
                position.pop("accounts", None)
            data["positions"] = data["positions"][:15]
            data["positions_truncated"] = data["position_count"] > 15
        return _query_ids(_ok(data, provider="composite", fetched_at=collection.fetched_at))
    except Exception as e:
        return _format_error(e)


# T0: registered by tools_v2 without the snapshot argument (read-only); this function keeps it.
def get_portfolio_summary(include_realized: bool = True, snapshot: bool = False) -> dict[str, Any]:
    """Whole-portfolio totals across every broker account.

    The MCP tool never writes a NAV snapshot (T0: registered read-only without the snapshot
    argument); only in-process callers can pass it. The daily history is written by the
    scheduled 18:10 capture and snapshot_portfolio_nav.
    With include_realized, fetched transaction rows are recorded in the append-only ledger as usual.

    Returns total assets, stock value, cash, cost basis, unrealized P&L, and
    (optionally) year-to-date realized P&L, dividends, fees, and taxes, with
    breakdowns by currency, market, and account. Valuation uses KRW at current spot;
    fees_krw/taxes_krw use broker trade FX, else spot, with cost_fx provenance and
    fees_status/taxes_status. Unreported costs are null rather than verified zero.
    realized_pnl_krw is null if any period sale has incomplete purchase basis;
    realized_pnl_complete_basis/incomplete_basis retain the separate subsets,
    with realized_pnl_status/reason_codes identifying basis and cost availability.
    Failed/truncated transaction reads also invalidate realized totals; an
    unavailable ledger is distinct from a successfully fetched empty ledger.
    The realized history is reconciled against the holdings already collected;
    unexplained quantity breaks invalidate realized totals with RECONCILIATION_BREAK.
    nav_bridge separately reports a reference settlement adjustment and the broker
    basis assumptions; it is not official account NAV or withdrawable cash.

    Args:
        include_realized: also compute YTD realized P&L from the ledger (slower —
            it pulls trade history)
        snapshot: append a local immutable NAV version and move today's representative
            so get_portfolio_history accumulates, unless the scheduled 18:10 capture with
            crypto evidence already holds it (snapshot.daily_representative false).
            Defaults to False; set True to request a NAV snapshot. Broker state is never modified.

    Returns:
        Structured JSON envelope with the portfolio summary.
    """
    try:
        collector = _get_collector()
        with ThreadPoolExecutor(max_workers=1) as pool:  # Round 3: perps shared with risk (positions cache)
            perp_read = pool.submit(_perp_exposure_inputs)
            collection = collector.collect_positions()
            currencies = {h.currency for h in collection.holdings} | {c.currency for c in collection.cash}
            fx = collector.build_fx(currencies)
            perps, perp_fx = perp_read.result()

        realized = None
        realized_health: dict[str, Any] = {}
        if include_realized:
            start, end = portfolio_engine.resolve_period("YTD")
            ledger = collector.collect_transactions(start, end, None, None)
            statement_factory = getattr(getattr(collector, "_providers", None), "statement_store", None)
            if statement_factory is not None:
                ledger_start, _ = portfolio_engine.resolve_period("ALL")
                # Earlier imported lots are local evidence; keep the existing
                # YTD broker request and do not make another provider read.
                merge_statements(ledger, statement_factory(), ledger_start,
                    (datetime.fromisoformat(start) - timedelta(days=1)).date().isoformat(), None, None)
            quality, _ = portfolio_engine.realized_input_quality(ledger, collection)
            realized = portfolio_engine.compute_realized_pnl(ledger.transactions, fx, start, end, input_quality=quality)
            realized_health = ledger.health

        data = portfolio_engine.summarize_portfolio(collection.holdings, collection.cash, fx, realized,
                                                    quotes=_holding_mark_quotes(collection.holdings),
                                                    perps=perps, perp_fx=perp_fx)
        data["realized_pnl_period"] = realized["period"] if realized else None
        data["sources_ok"] = collection.ok_sources
        data["sources_failed"] = sorted({*collection.failed_sources, *(ledger.failed_sources if include_realized else [])})
        data["ledger_sources_failed"] = ledger.failed_sources if include_realized else []
        data["ledger_truncated_sources"] = ledger.truncated_sources if include_realized else []
        data["health"] = {**collection.health, **realized_health}
        data["methodology"] = {**_VALUATION_METHODOLOGY, "fx_valuation": FX_VALUATION_NOTE}
        data["limitations"] = _LEDGER_LIMITATIONS
        data["result_status"] = portfolio_engine.collection_result_status(
            [collection, ledger] if include_realized else [collection],
            len(collection.holdings) + len(collection.cash) + (len(ledger.transactions) if include_realized else 0))
        if collection.failed_sources or collection.truncated_sources:
            bridge = data["nav_bridge"]
            bridge["settlement_adjusted_nav_krw"] = None
            bridge["reconciliation"]["status"] = "unavailable"
            bridge["reconciliation"]["difference_reasons"].append("INCOMPLETE_PORTFOLIO_SOURCES")
        elif data["nav_bridge"]["reconciliation"]["difference_reasons"] and data["result_status"] == "complete":
            data["result_status"] = "partial"
        if data["realized_pnl_status"] in {"partial", "unavailable"} and data["result_status"] in {"complete", "empty"}:
            data["result_status"] = "partial"

        collect_nav_evidence(collector, collection)
        captured = snapshot_summary(data, collection, fx)
        from daily_nav import FIELDS as daily_nav_fields
        from daily_nav import unavailable_local_evidence
        data.update({key: captured[key] for key in daily_nav_fields if key in captured})
        try:
            data.update(_get_nav_store().enrich_summary(captured))
        except Exception:  # noqa: BLE001 — optional local evidence must not hide a broker summary
            data.update(unavailable_local_evidence(captured))

        # A NAV snapshot must represent the whole portfolio: a failed source or a
        # truncated holdings page both mean positions are missing, which would
        # record an understated NAV and distort drawdown/TWR forever after.
        incomplete = collection.failed_sources + collection.truncated_sources
        if snapshot and not incomplete:
            try:
                stored = _get_nav_store().upsert_snapshot(captured, fx_usdkrw=fx.rates.get("USD"),
                                                          keep_crypto_representative=True)
                data.update({key: stored[key] for key in daily_nav_fields})
                data["snapshot"] = {"stored": True, "snapshot_date": stored.get("snapshot_date"),
                                    "snapshot_id": stored["snapshot_id"], "version": stored["version"]}
                if stored.get("representative") is False:
                    data["snapshot"]["daily_representative"] = False  # the scheduled capture keeps crypto evidence
            except Exception as e:  # noqa: BLE001 — a store failure must not fail the summary
                try:
                    _get_nav_store().record_attempt(datetime.now().date().isoformat(), "unavailable", ["SNAPSHOT_WRITE_FAILED"])
                except Exception:  # noqa: BLE001 — storage may be unavailable for health too
                    pass
                data["snapshot"] = {"stored": False, "error": classify_error(e)["message"]}
        else:
            if snapshot and incomplete:
                try:
                    _get_nav_store().record_attempt(datetime.now().date().isoformat(), "unavailable",
                        (["FAILED_SOURCES"] if collection.failed_sources else []) +
                        (["TRUNCATED_SOURCES"] if collection.truncated_sources else []))
                except Exception:  # noqa: BLE001 — collection health must not fail the summary
                    pass
            data["snapshot"] = {
                "stored": False,
                "reason": (
                    "snapshot disabled"
                    if not snapshot
                    else f"incomplete portfolio: {', '.join(incomplete)}"
                ),
            }
        return {**_ok(data, provider="composite", fetched_at=collection.fetched_at), "result_status": data["result_status"]}
    except Exception as e:
        return {**_format_error(e), "result_status": "unavailable"}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_realized_pnl(
    period: Literal["MTD", "YTD", "1M", "3M", "6M", "1Y", "ALL"] = "YTD",
    account: str = "all",
    symbol: str = "",
    start_date: str = "",
    end_date: str = "",
    reconcile: bool = True,
) -> dict[str, Any]:
    """Realized P&L per symbol under per-account moving-average cost basis (READ-ONLY).

    Basis is computed per (account, symbol, currency). Totals and open positions
    sum rounded account cost and P&L; combined average price is remaining cost / quantity.
    methodology.basis_scope is per_account; another account's purchases cannot
    complete a sale's missing basis.

    The ledger is replayed from the earliest available history so cost basis is
    correct, then only sales inside the requested window are reported. Symbols whose
    original purchase predates the broker's available history appear in
    `incomplete_cost_basis` and their P&L understates cost — do not read those as
    true gains. Reported buy fees/taxes are capitalized. totals.fees/taxes are
    deprecated null scalars; use fees_by_currency/taxes_by_currency or actual KRW
    fees_krw/taxes_krw (broker trade FX, else spot; cost_fx lists rates and dates).
    Cost/income status distinguishes unsupported, unknown, reported, verified_zero.
    Open ledger positions label their fee/tax basis; missing costs remain unknown.
    totals.complete_basis/incomplete_basis partition individual window sales, with
    KRW/native amounts, counts and symbols; incomplete amounts are estimates.
    Legacy totals.realized_pnl_krw/realized_pnl_by_currency are null if any sale's
    principal basis is incomplete. totals.status/reason_codes also flag unreported
    buy/sell costs; complete principal basis alone does not establish verified P&L.
    Missing proceeds null affected P&L while retaining purchase basis. Ledger source
    failures/truncation propagate to both subsets and result_status; unavailable
    source reads never become a confirmed zero total.
    Unexplained current-quantity breaks add RECONCILIATION_BREAK to existing
    totals/subset status and reason_codes, preventing verified account P&L.
    Additive meta IDs identify the query and calculation policy. dataset_id is
    null on live queries; export_portfolio_dataset freezes replay inputs.

    Args:
        period: MTD, YTD, 1M, 3M, 6M, 1Y, or ALL (ignored if start_date is given)
        account: "all", "toss", "kis", or a specific account label
        symbol: optional symbol filter
        start_date: ISO YYYY-MM-DD, overrides `period`
        end_date: ISO YYYY-MM-DD (default: today)
        reconcile: compare ending quantities with current holdings (default true).
            Historical end dates are marked unavailable for this comparison.
            Either way, books whose engine quantity differs from current holdings
            (ENGINE_QUANTITY_DRIFT) have null P&L, as in get_trade_review.

    Returns:
        Structured JSON envelope with realized P&L.
    Fetched broker rows are recorded in the local append-only ledger (no broker writes).
    """
    try:
        if start_date:
            start, end = _resolve_dates(start_date, end_date, default_days=365)
        else:
            start, end = portfolio_engine.resolve_period(period)
        accounts = _resolve_accounts(account)
        # Replay from the start of available history so cost basis is right.
        ledger_start, _ = portfolio_engine.resolve_period("ALL")
        collection = _get_collector().collect_transactions(ledger_start, end, accounts, symbol or None)
        # The engine's quantity check (ENGINE_QUANTITY_DRIFT) reads holdings whatever `reconcile` is, so a
        # figure never depends on it and equals get_trade_review's; reconcile=False skips only the report.
        positions = _get_collector().collect_positions(accounts)
        quality, quantity_check = portfolio_engine.realized_input_quality(collection, positions, symbol or None,
                                                                          report=reconcile)
        fx = _get_collector().build_fx({tx.currency for tx in collection.transactions})
        data = portfolio_engine.compute_realized_pnl(collection.transactions, fx, start, end, input_quality=quality)
        data.update(
            {
                "account_filter": account,
                "ledger_window": {"start": ledger_start, "end": end},
                "reconciliation": collection.reconciliation or {"status": "unavailable", "reason_codes": ["RECONCILIATION_NOT_REQUESTED"],
                                                                "engine_quantity_check": quantity_check},
                "sources_ok": collection.ok_sources,
                "sources_failed": collection.failed_sources,
                "truncated_sources": collection.truncated_sources,
                "health": collection.health,
                "fx": fx.to_dict(),
                "limitations": _LEDGER_LIMITATIONS,
            }
        )
        if collection.truncated_sources:
            data["warning"] = (
                "Broker paging cap reached for "
                f"{', '.join(collection.truncated_sources)} — cost basis is derived from "
                "an incomplete ledger and realized P&L may be wrong. Narrow the period."
            )
        data["result_status"] = data["totals"]["status"]
        return _query_ids({**_ok(data, provider="composite", fetched_at=collection.fetched_at), "result_status": data["result_status"]})
    except Exception as e:
        return {**_format_error(e), "result_status": "unavailable"}


# ── Minimal additive trade review ────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_trade_review(
    period: Literal["MTD", "YTD", "1M", "3M", "6M", "1Y", "ALL"] = "YTD",
    start_date: str = "",
    end_date: str = "",
    account: str = "all",
    symbol: str = "",
    detail_level: Literal["summary", "full"] = "summary",
) -> dict[str, Any]:
    """Review observed orders, closed cycles, realized P&L and current open P&L (READ-ONLY).

    Reuses the existing moving-average engine, per account. Order aggregates are
    not fills; fill_count and thesis_count remain null without evidence/journal.
    Only zero-to-positive-to-zero cycles with both legs inside the window count.
    Win rate uses closed cycles with known basis/costs and reports its denominator
    (realized_win_rate = win_rate_strict); win_rate_all also counts cycles whose only
    unknown is an unreported tax (flagged win_rate_all_taxes_unknown), each with its
    eligible count. Win rates exclude books with unexplained quantity breaks and list them.
    Coverage reports eligible/total closed round trips and exclusions by reason.
    Cohorts distinguish new acquisitions, pre-window liquidation and unknown
    origin; mixed origins use proportional quantities, not identified sold lots.
    Unknown costs, basis, FX, failed sources and reconciliation breaks null KRW
    totals with reasons. Native diagnostic estimates retain engine basis status.
    Holding days require observed acquisition and disposal, with date/id ordering;
    intraday ordering and historical cycle completeness are not established.
    Open P&L is current broker valuation, not historical window-end P&L.
    No alpha, benchmark, turnover effect, intent or lesson is generated.
    Summary returns totals (without cohorts), the top 10 symbols by |realized P&L|
    (rank, omitted_symbols) and every symbol with a reason grouped by its codes
    (symbol_reason_groups); full returns every per-symbol row with cohorts, plus
    sales, round_trips and current open_positions. No snapshots or journal are written.
    start_date overrides period; end_date alone anchors period to that date.
    Fetched broker rows are recorded in the local append-only ledger (no broker writes).
    """
    try:
        if start_date:
            start, end = _resolve_dates(start_date, end_date, default_days=365)
        else:
            reference = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else None
            start, end = portfolio_engine.resolve_period(period, today=reference)
        accounts = _resolve_accounts(account)
        ledger_start, _ = portfolio_engine.resolve_period("ALL")
        collector = _get_collector()
        ledger = collector.collect_transactions(ledger_start, end, accounts, symbol or None)
        positions = collector.collect_positions(accounts)
        fx = collector.build_fx({t.currency for t in ledger.transactions} | {h.currency for h in positions.holdings})
        data = portfolio_engine.compute_trade_review(
            ledger, positions, fx, start, end, symbol=symbol, detail_level=detail_level,
        )
        data.update({"account_filter": account, "symbol_filter": symbol,
                     "fx": fx.to_dict(), "limitations": _LEDGER_LIMITATIONS})
        return _query_ids({**_ok(data, provider="composite", fetched_at=ledger.fetched_at),
                           "result_status": data["result_status"]})
    except Exception as e:
        return {**_format_error(e), "result_status": "unavailable"}


def _return_flow_inputs(store, navs, start, end, scope, verified_zero):
    if scope not in ('equity', 'consolidated', 'toss', 'crypto', 'toss_investmcp', 'kis'):
        raise ValueError('Invalid portfolio_scope')
    evidence = None if verified_zero else read_flows(getattr(store, 'db_path', None), start, end, scope, navs)
    selection = evidence
    if scope == 'toss_investmcp' and selection is None:
        selection = read_flows(getattr(store, 'db_path', None), start, end, scope, navs)
    navs = scope_navs(navs, scope, account_references=(selection or {}).get('_nav_accounts', []))
    if scope != 'equity' and navs:
        for index, boundary, code in ((0, start, 'MISSING_START_NAV'), (-1, end, 'MISSING_END_NAV')):
            if navs[index]['snapshot_date'] != boundary:
                navs[index] = {**navs[index], 'reason_codes': list(dict.fromkeys(navs[index].get('reason_codes', []) + [code]))}
    return navs, evidence


def _ledgerless_block(store, stored_navs, start, end, scope, evidence, detail_level):
    """Additive, separately labelled interval estimate; never alters existing fields.

    toss_investmcp accounts come from the owner-export identity inside the engine,
    not from ``evidence['_nav_accounts']``: that list is empty whenever statement
    flow coverage is unavailable, which is exactly when the estimate is needed.
    """
    import interval_returns
    if scope in ('consolidated', 'equity'):  # configuration, never a failed read, decides which scopes are empty
        return interval_returns.ledgerless_returns(getattr(store, 'db_path', None), stored_navs, scope, start, end,
                                                   detail_level=detail_level, configured=_scope_configuration())
    return interval_returns.ledgerless_returns(getattr(store, 'db_path', None), stored_navs, scope, start, end,
                                               detail_level=detail_level)


def _broker_configuration():
    """Configured brokers: True, False, or None (only a credential file is named; it is not read here)."""
    return {
        "toss": True if _toss_accounts is not None or os.environ.get("TOSS_ACCOUNTS_JSON") or (os.environ.get("TOSS_CLIENT_ID") and os.environ.get("TOSS_CLIENT_SECRET")) else None if os.environ.get("TOSS_CREDS_FILE") else False,
        "kis": True if _kis_accounts is not None or os.environ.get("KIS_ACCOUNTS_JSON") or (os.environ.get("KIS_APP_KEY") and os.environ.get("KIS_APP_SECRET")) else None if os.environ.get("KIS_ENV_FILE") else False,
    }


def _scope_configuration():
    """Consolidated scopes configured for the owner: True, False or None (unknown, never excluded).

    Environment names and file existence only, the same sources the clients use
    (crypto_futures_accounts); no credential content is read.
    """
    directory = os.environ.get('CREDENTIALS_DIRECTORY', '')
    binance = [os.environ.get(name) or (os.path.join(directory, suffix) if directory else '')
               for name, suffix in (('BINANCE_FUTURES_API_KEY_FILE', 'binance-futures-api-key'),
                                    ('BINANCE_FUTURES_SIGNING_KEY_FILE', 'binance-futures-signing-key'))]
    crypto = (True if os.environ.get('HYPERLIQUID_ACCOUNT_ADDRESSES', '').strip()
              or all(binance) and all(os.path.exists(path) for path in binance)
              else None if any(binance) else False)
    return {**_broker_configuration(), 'crypto': crypto}


def _without_kis_capture(row):
    """A stored NAV row for tool output: the raw KIS capture (~8 KB per account per day) stays in storage."""
    return {key: value for key, value in row.items() if key != 'kis_capture'}


def _kis_capture_status(row):
    """Compact status of the latest stored KIS capture (kis scope only; no amounts).

    An account's reason_codes are its reasons plus its non-nulling advisories
    (e.g. KIS_FX_CASH_CROSS_CHECK_MISMATCH: the NAV stands, the code says what to check).
    """
    capture = (row or {}).get('kis_capture')
    if not isinstance(capture, dict):
        return {'snapshot_date': (row or {}).get('snapshot_date'), 'status': 'missing',
                'reason_codes': ['KIS_CAPTURE_MISSING']}
    accounts = capture.get('accounts') if isinstance(capture.get('accounts'), dict) else {}
    return {'snapshot_date': row.get('snapshot_date'), 'status': 'stored', 'version': capture.get('version'),
            'observed_at': capture.get('observed_at'), 'reason_codes': list(capture.get('reasons') or []),
            'accounts': {key: {'status': entry.get('status'), 'reason_codes': list(dict.fromkeys(
                                   [*(entry.get('reasons') or []), *(entry.get('advisories') or [])])),
                               'nav_price_time': entry.get('nav_price_time'),
                               'nav_price_time_basis': entry.get('nav_price_time_basis')}
                         for key, entry in sorted(accounts.items()) if isinstance(entry, dict)}}


def _return_ledger_meta(data, evidence, scope):
    if evidence is not None or scope != 'equity':
        data['portfolio_scope'] = scope
        data['methodology'] = {**data['methodology'], 'portfolio_scope': scope,
            'flow_timing': 'TWR assumes external flows at each snapshot interval end; XIRR uses actual ledger dates in Asia/Seoul',
            'flow_source': 'caller_confirmation' if evidence is None else 'permanent_ledger'}
        if scope == 'consolidated':
            data['methodology']['pnl_decomposition_scope'] = 'equity_only; crypto P&L decomposition unavailable'
        if evidence is not None:
            data['ledger_flow_coverage'] = evidence['coverage']
            if evidence['coverage'].get('account_labels'):
                data['account_labels'] = evidence['coverage']['account_labels']
            data['limitations'] = [*data['limitations'][1:],
                'Snapshot IDs pin NAV only. Ledger flow classifications and coverage use the current local ledger revision; historic ledger replay is not pinned.']


#: FX wording of the return tools follows fx.policy (Toss rate first). A stored NAV keeps
#: the FX its snapshot was valued at; only live performance converts at today's rate (FX_VALUATION_NOTE).
_STORED_NAV_FX_NOTE = (
    "stored snapshot NAVs keep the FX each snapshot was valued at, never today's rate: per that snapshot's "
    "fx.policy and fx.sources (Toss broker rate; yfinance fallback labelled), or yfinance <CUR>KRW=X spot "
    "for snapshots stored before fx.policy existed"
)

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_portfolio_performance(
    period: Literal["MTD", "YTD", "1M", "3M", "6M", "1Y", "ALL"] = "YTD",
    detail_level: Literal["summary", "full"] = "summary",
    external_cashflows_verified_zero: bool = False,
    input_snapshot_ids: list[str] | None = None,
    expected_snapshot_dates: list[str] | None = None,
    risk_free_rate: float | None = None,
    portfolio_scope: Literal['equity', 'consolidated', 'toss', 'crypto', 'toss_investmcp', 'kis'] = 'equity',
) -> dict[str, Any]:
    """Portfolio return and P&L decomposition for a period (READ-ONLY).

    Reports simple return, time-weighted return (TWR), and XIRR/MWR side by side —
    they answer different questions and diverge whenever money moves. Also splits
    P&L into realized, unrealized, dividends, fees, taxes, and the FX component,
    and includes NAV risk statistics (MDD, volatility, Sharpe, Sortino).

    Flow-dependent returns and risk statistics are unavailable without verified
    external cash flows. Raw asset changes are supplied under separate names.
    TWR also requires at least two stored NAV snapshots. Missing position valuation
    disables NAV-dependent metrics without redefining reported total_assets_krw.
    Costs and income retain ledger availability statuses/reasons; missing is null.
    ledgerless_returns is a separately labelled (estimate/verified/unavailable) linked
    Modified Dietz TWR estimate from NAV snapshots and inferred/API flows; it never changes twr_pct.
    Its tracked field links the latest gap-free run of resolved intervals, dated; not the period.
    Its effective_period is the period clipped to its first/last complete snapshot (clipped flag).

    Args:
        period: MTD, YTD, 1M, 3M, 6M, 1Y, or ALL
        detail_level: "full" adds per-day and per-month P&L series
        external_cashflows_verified_zero: explicitly confirm no deposits, withdrawals,
            or external transfers across the whole portfolio for this entire period.
            The response records caller confirmation and its scope; default is unknown.

        input_snapshot_ids: replay these exact versions without live collection.
            Replay pins NAV only; ledger-dependent P&L decomposition stays
            unavailable, and flows read the current local ledger. Date bounds
            come from the selected versions. Omit to use
            current live holdings/ledger with today's daily representatives.
        expected_snapshot_dates: explicit valuation calendar; None means unknown.
        risk_free_rate: decimal annual rate. Replay defaults to 0; live mode uses
            configured rate. Match this and flow confirmation when replaying.
        portfolio_scope: default equity (Toss/KIS; ledgerless: Modified Dietz on Toss + KIS
            capture levels, null when either part is missing); toss selects all Toss accounts,
            toss_investmcp selects the locally verified InvestMCP 전용 account,
            crypto selects Binance/Hyperliquid futures/perp NAV, kis selects the KIS
            accounts from the 18:10 KIS capture (ledgerless estimate only, from the first
            capture on; earlier days null with KIS_CAPTURE_MISSING); consolidated opts into
            Toss/KIS/Binance/Hyperliquid using stored M3 consolidated NAV (ledgerless: KRW
            Modified Dietz on Toss + KIS capture + crypto levels, null when any part is missing).
            Effective external_in/out ledger classifications supply dated KRW
            flows; matched internal pairs contribute nothing only when both
            wallets belong to the selected NAV boundary. MAIN/FUNDING/spot to
            futures/perp crossings count as external; unknown boundaries block.
            Every NAV roster account needs a durable ledger mapping and complete
            cash-flow stream coverage. Unknown/conflicted flows,
            missing flow-date FX, and uncovered periods yield null + reasons.
            Snapshot replay reads current ledger revisions; flows are not pinned.
            Ledger-enabled returns use stored boundary NAV values. Bounded local
            reads yield FLOW_READ_BUDGET_EXCEEDED rather than partial returns.
            Explicit owner-declared Binance pre-activity coverage is identified
            separately from observed export/API history in coverage metadata.

    Returns:
        Structured JSON envelope with performance metrics.
    Fetched broker rows are recorded in the local append-only ledger (no broker writes).
    """
    try:
        store = _get_nav_store()
        # Consolidated reads stored NAVs and M3 flows only: the live branch would re-collect the whole
        # broker history (~150 s) for an equity-only P&L decomposition consolidated does not have.
        if input_snapshot_ids is not None or portfolio_scope in ('toss', 'crypto', 'toss_investmcp', 'kis', 'consolidated'):
            from portfolio_model import FxRates

            if input_snapshot_ids is not None:
                navs = store.select_snapshots(input_snapshot_ids)
                start, end = navs[0]["snapshot_date"], navs[-1]["snapshot_date"]
            else:
                start, end = portfolio_engine.resolve_period(period)
                navs = store.range(start, end)
            stored_navs = navs
            navs, flow_evidence = _return_flow_inputs(store, navs, start, end, portfolio_scope, external_cashflows_verified_zero)
            flow_coverage = ({"status": "verified_zero", "start": start, "end": end,
                              "source": "caller_confirmation", "scope": "whole_portfolio"}
                             if external_cashflows_verified_zero else None)
            if flow_evidence is not None:
                flow_coverage = flow_evidence['coverage']
            calendar = store.collection_health(start, end, expected_snapshot_dates, navs)
            ledger_totals = {"reason_codes": ["LEDGER_NOT_PINNED"]}
            for field in ("fees", "taxes", "dividends", "interest"):
                ledger_totals.update({field + "_krw": None, field + "_status": "unavailable",
                                      field + "_reason_codes": ["LEDGER_NOT_PINNED"]})
            data = portfolio_engine.compute_performance(
                [], navs, None, FxRates(), start, end,
                realized={"totals": ledger_totals},
                risk_free_rate=(_risk_free_decimal() if input_snapshot_ids is None and portfolio_scope == 'consolidated'
                                else 0.0) if risk_free_rate is None else risk_free_rate,
                external_cashflows_coverage=flow_coverage,
                ledger_flow_input=flow_evidence,
                input_quality={"expected_snapshot_dates": calendar["expected_days"], "snapshot_reason_codes": ["MISSING_EXPECTED_SNAPSHOT"]
                               if calendar["expected_day_gaps"] else []},
                portfolio_scope=portfolio_scope)
            for name in (("buy_amount_krw", "sell_amount_krw") if flow_evidence is not None else
                         ("observed_deposits_krw", "observed_withdrawals_krw", "buy_amount_krw", "sell_amount_krw")):
                data["flows"][name] = None
            data.update({"replay_scope": "snapshot_nav_only", "ledger_status": "unavailable",
                         "realized_by_symbol": [], "incomplete_cost_basis": [],
                         "sources_failed": [], "truncated_sources": [],
                         "methodology": {**_VALUATION_METHODOLOGY, **_RETURN_METHODOLOGY,
                                         "fx_valuation": "no live FX in snapshot replay; " + _STORED_NAV_FX_NOTE},
                         "limitations": [*_LEDGER_LIMITATIONS, "Snapshot IDs do not pin a ledger or live FX; ledger decomposition is unavailable in replay."],
                         "calendar": {key: calendar[key] for key in ("expected_days", "expected_day_gaps", "calendar_status")}})
            _return_ledger_meta(data, flow_evidence, portfolio_scope)
            if portfolio_scope == 'consolidated':
                data['methodology']['pnl_decomposition_scope'] = 'none (LEDGER_NOT_PINNED)'
                data['limitations'].append(_CONSOLIDATED_NO_PNL_DECOMPOSITION)
            data['ledgerless_returns'] = _ledgerless_block(store, stored_navs, start, end, portfolio_scope,
                                                           flow_evidence, detail_level)
            if flow_evidence is not None:
                data['replay_scope'] = 'snapshot_nav_with_current_ledger_flows'
            if detail_level == "full":
                data["pnl_series"] = portfolio_engine.daily_and_monthly_pnl(
                    navs, data["flows"]["flows_by_date"],
                    flow_reason_codes=data["flows"]["external_cashflows"]["reason_codes"] +
                    data["coverage"]["input_quality"]["snapshot_reason_codes"],
                    external_cashflows_coverage=flow_evidence['coverage'] if flow_evidence is not None else None,
                    portfolio_scope=portfolio_scope)
                for index, row in enumerate(data["pnl_series"]["daily"]):
                    for metric in row["metrics"].values():
                        metric["input_snapshot_ids"] = [navs[index]["snapshot_id"], navs[index + 1]["snapshot_id"]]
                data["health"] = {}
            return {**_ok(data, provider="k-invest"), "result_status": data["result_status"]}

        start, end = portfolio_engine.resolve_period(period)
        collector = _get_collector()
        positions = collector.collect_positions()
        currencies = {h.currency for h in positions.holdings} | {c.currency for c in positions.cash}
        fx = collector.build_fx(currencies)
        # The live end NAV and unrealized P&L are get_portfolio_summary's own figures (the same
        # summarize_portfolio call on the canonical marks and fx.policy FX), not broker prices.
        summary = portfolio_engine.summarize_portfolio(positions.holdings, positions.cash, fx,
                                                       quotes=_holding_mark_quotes(positions.holdings))

        ledger_start, _ = portfolio_engine.resolve_period("ALL")
        ledger = collector.collect_transactions(ledger_start, end, None, None)
        # Reconcile first so realized P&L sees ENGINE_QUANTITY_DRIFT books.
        quality, _ = portfolio_engine.realized_input_quality(ledger, positions)
        reconciliation_breaks = ledger.reconciliation["reconciliation_breaks"]
        realized = portfolio_engine.compute_realized_pnl(ledger.transactions, fx, start, end, input_quality=quality)

        navs = store.range(start, end)
        stored_navs = navs
        navs, flow_evidence = _return_flow_inputs(store, navs, start, end, portfolio_scope, external_cashflows_verified_zero)
        risk_free = _risk_free_decimal() if risk_free_rate is None else risk_free_rate
        calendar = store.collection_health(start, end, expected_snapshot_dates, navs) if expected_snapshot_dates is not None else None
        data = portfolio_engine.compute_performance(
            ledger.transactions,
            navs,
            summary.get("total_assets_krw") if portfolio_scope == 'equity' and flow_evidence is None else None,
            fx,
            start,
            end,
            realized=realized,
            unrealized_krw=summary.get("unrealized_pnl_krw"),
            risk_free_rate=risk_free,
            external_cashflows_coverage=(
                {"status": "verified_zero", "start": start, "end": end,
                 "source": "caller_confirmation", "scope": "whole_portfolio"}
                if external_cashflows_verified_zero else flow_evidence['coverage'] if flow_evidence is not None else None
            ),
            ledger_flow_input=flow_evidence,
            current_nav_as_of=positions.fetched_at,
            portfolio_scope=portfolio_scope,
            # The live end is valued like a new snapshot (canonical marks, fx.policy FX).
            current_nav_valuation_basis=consolidated_nav_basis(summary),
            input_quality={
                "expected_snapshot_dates": calendar["expected_days"] if calendar else None,
                "snapshot_reason_codes": ["MISSING_EXPECTED_SNAPSHOT"] if calendar and calendar["expected_day_gaps"] else [],
                "sources_failed": sorted({*positions.failed_sources, *ledger.failed_sources}),
                "truncated_sources": sorted({*positions.truncated_sources, *ledger.truncated_sources}),
                "missing_currencies": sorted(fx.missing),
                "incomplete_holdings_cost_basis": sorted({h.symbol for h in positions.holdings if h.quantity > 0 and h.cost_basis is None}),
                "missing_position_valuation": sorted({h.symbol for h in positions.holdings if h.quantity > 0 and h.market_value is None and h.last_price is None}),
                "reconciliation_breaks": reconciliation_breaks,
            },
        )
        data["replay_scope"] = "live_holdings_and_ledger_with_versioned_nav"
        data["end_nav_valuation"] = ({"value_field": "get_portfolio_summary.total_assets_krw",
                                      "mark_policy": summary["mark_policy"]["name"],
                                      "mark_status_counts": summary["mark_status_counts"],
                                      "fx_policy": FX_POLICY["name"]}
                                     if data["end_nav_basis"] == "live_holdings" else None)
        data["realized_by_symbol"] = realized["symbols"][:10]
        data["incomplete_cost_basis"] = realized["incomplete_cost_basis"]
        data["sources_failed"] = list({*positions.failed_sources, *ledger.failed_sources})
        data["truncated_sources"] = list({*positions.truncated_sources, *ledger.truncated_sources})
        data["methodology"] = {**_VALUATION_METHODOLOGY, **_RETURN_METHODOLOGY,
                               "fx_valuation": FX_VALUATION_NOTE, "fx_stored_nav": _STORED_NAV_FX_NOTE}
        data["fx"] = fx.to_dict()
        data["limitations"] = _LEDGER_LIMITATIONS
        _return_ledger_meta(data, flow_evidence, portfolio_scope)
        data['ledgerless_returns'] = _ledgerless_block(store, stored_navs, start, end, portfolio_scope,
                                                       flow_evidence, detail_level)
        source_status = portfolio_engine.collection_result_status(
            [positions, ledger], len(positions.holdings) + len(positions.cash) + len(ledger.transactions))
        if source_status in {"unavailable", "empty"}:
            data["result_status"] = source_status
        elif source_status == "partial":
            data["result_status"] = "partial"
        if detail_level == "full":
            data["pnl_series"] = portfolio_engine.daily_and_monthly_pnl(
                navs, data["flows"]["flows_by_date"], data["coverage"]["input_quality"],
                data["flows"]["external_cashflows"]["reason_codes"] +
                data["coverage"]["input_quality"]["snapshot_reason_codes"],
                external_cashflows_coverage=flow_evidence['coverage'] if flow_evidence is not None else None,
                portfolio_scope=portfolio_scope)
            for index, row in enumerate(data["pnl_series"]["daily"]):
                for metric in row["metrics"].values():
                    metric["input_snapshot_ids"] = [nav["snapshot_id"] for nav in navs[index:index + 2] if nav.get("snapshot_id")]
            data["health"] = {**positions.health, **ledger.health}
        return {**_ok(data, provider="composite", fetched_at=positions.fetched_at), "result_status": data["result_status"]}
    except Exception as e:
        return {**_format_error(e), "result_status": "unavailable"}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_portfolio_history(
    period: Literal["MTD", "YTD", "1M", "3M", "6M", "1Y", "ALL"] = "3M",
    detail_level: Literal["summary", "full"] = "summary",
    external_cashflows_verified_zero: bool = False,
    input_snapshot_ids: list[str] | None = None,
    expected_snapshot_dates: list[str] | None = None,
    risk_free_rate: float | None = None,
    start_date: str = "",
    end_date: str = "",
    portfolio_scope: Literal['equity', 'consolidated', 'toss', 'crypto', 'toss_investmcp', 'kis'] = 'equity',
) -> dict[str, Any]:
    """Stored daily NAV snapshots with drawdown and risk statistics (READ-ONLY).

    Return metrics require verified cash-flow coverage; asset-value statistics
    remain available under explicitly named raw fields.
    ledgerless_returns is a separately labelled (estimate/verified/unavailable) linked
    Modified Dietz TWR estimate from NAV snapshots and inferred/API flows; it never changes twr_pct.
    Its tracked field links the latest gap-free run of resolved intervals, dated; not the period.
    Its effective_period is the period clipped to its first/last complete snapshot (clipped flag).

    History accumulates from the scheduled 18:10 capture and snapshot_portfolio_nav calls —
    it is not backfilled, because no broker API serves historical account NAV.
    Additive meta IDs include the latest selected snapshot_id where available;
    dataset_id stays null unless an export freezes the selected versions.

    Args:
        period: MTD, YTD, 1M, 3M, 6M, 1Y, or ALL
        detail_level: "full" includes every snapshot row and the daily P&L series
        external_cashflows_verified_zero: explicitly confirm no deposits, withdrawals,
            or external transfers across the whole portfolio for this entire period.
            The response records caller confirmation and its scope; default is unknown.

        input_snapshot_ids: exact immutable versions to replay, one per day.
            Omit for daily representatives. Replay bounds use selected dates.
        expected_snapshot_dates: explicit valuation calendar, including its holiday
            exclusions. None leaves expected days and gaps unknown.
        risk_free_rate: decimal annual rate; default 0 in pinned replay, live
            Treasury quote otherwise. Use replay_parameters for the original rate.
            Match rate, calendar and flow confirmation to reproduce statistics.
        start_date: explicit ISO period bound, saved in replay_parameters.
        end_date: explicit ISO period bound (requires start_date).
        portfolio_scope: equity (Toss/KIS, default; ledgerless: Toss + KIS capture levels,
            null when either part is missing), toss (all Toss accounts),
            toss_investmcp (locally verified InvestMCP 전용 account), crypto
            (Binance/Hyperliquid futures/perp), kis (KIS accounts from the 18:10 KIS
            capture; ledgerless estimate only, earlier days null), or opt-in consolidated
            (Toss/KIS/Binance/Hyperliquid), using stored M3 consolidated NAV (ledgerless: KRW
            Modified Dietz on Toss + KIS capture + crypto levels, null when any part is missing).
            Current local ledger flows require complete account/stream coverage,
            explicit external classification and historical FX; otherwise
            flow-adjusted statistics/P&L are null with coverage reason codes.
            Coverage includes every NAV roster account via durable ledger IDs;
            consolidated flows follow the futures/perp wallet cut. Ambiguous
            wallet mappings and read-budget exhaustion also block metrics.
            Snapshot IDs do not pin ledger revisions. TWR timing is interval-end;
            ledger dates follow Asia/Seoul.
            Owner-declared Binance pre-activity coverage is auditable and
            explicitly distinguished from observed export/API history.

    Returns:
        Structured JSON envelope with the NAV series and its statistics.
    """
    try:
        if end_date and not start_date:
            raise ValueError("end_date requires start_date")
        start, end = _resolve_dates(start_date, end_date, default_days=365) if start_date else portfolio_engine.resolve_period(period)
        store = _get_nav_store()
        navs = store.range(start, end) if input_snapshot_ids is None else store.select_snapshots(input_snapshot_ids)
        if input_snapshot_ids is not None and not start_date:
            start, end = navs[0]["snapshot_date"], navs[-1]["snapshot_date"]
        stored_navs = navs
        navs, flow_evidence = _return_flow_inputs(store, navs, start, end, portfolio_scope, external_cashflows_verified_zero)
        first, last = store.bounds()
        collection_health = store.collection_health(start, end, expected_snapshot_dates, navs)
        snapshot_quality = {"expected_snapshot_dates": collection_health.get("expected_days"),
                            "snapshot_reason_codes": ["MISSING_EXPECTED_SNAPSHOT"]
                            if collection_health["expected_day_gaps"] else []}
        if flow_evidence is not None or portfolio_scope != 'equity':
            if not navs or navs[0]['snapshot_date'] != start:
                snapshot_quality['snapshot_reason_codes'].append('MISSING_START_NAV')
            if not navs or navs[-1]['snapshot_date'] != end:
                snapshot_quality['snapshot_reason_codes'].append('MISSING_END_NAV')
        flow_coverage = (
            {"status": "verified_zero", "start": navs[0]["snapshot_date"] if navs else start,
             "end": navs[-1]["snapshot_date"] if navs else end,
             "source": "caller_confirmation", "scope": "whole_portfolio"}
            if external_cashflows_verified_zero else None
        )
        flows_by_date = {} if external_cashflows_verified_zero else None
        if flow_evidence is not None:
            flow_coverage = flow_evidence['coverage']
            flows_by_date = flow_evidence['flows_by_date']
        statistics = portfolio_engine.nav_statistics(
            navs, risk_free_rate=(0.0 if input_snapshot_ids is not None else _risk_free_decimal())
            if risk_free_rate is None else risk_free_rate, flows_by_date=flows_by_date,
            input_quality=snapshot_quality,
            flow_reason_codes=flow_coverage.get('reason_codes', []) if flow_evidence is not None else None,
            external_cashflows_coverage=flow_coverage)
        data: dict[str, Any] = {
            "period": {"start": start, "end": end},
            "snapshot_count": len(navs),
            "input_snapshot_ids": [row["snapshot_id"] for row in navs if row.get("snapshot_id")],
            "collection_health": collection_health,
            "replay_parameters": {"input_snapshot_ids": [row["snapshot_id"] for row in navs if row.get("snapshot_id")],
                                  "risk_free_rate": statistics["risk_free_rate_used"],
                                  "start_date": start, "end_date": end,
                                  "external_cashflows_verified_zero": external_cashflows_verified_zero,
                                  "expected_snapshot_dates": expected_snapshot_dates},
            "stored_range": {"first": first, "last": last, "total": store.count()},
            "statistics": statistics,
            "latest": _without_kis_capture(stored_navs[-1]) if stored_navs else None,
            # The latest row is a stored snapshot (never live holdings), with its as-of.
            "latest_nav_basis": {"basis": "stored_snapshot", "as_of": stored_navs[-1].get("valuation_at"),
                                 "snapshot_date": stored_navs[-1].get("snapshot_date"),
                                 "snapshot_id": stored_navs[-1].get("snapshot_id"),
                                 "value_field": "total_value_krw (the stored row's equity total)"}
            if stored_navs else None,
            "methodology": {**_RETURN_METHODOLOGY, "fx_valuation": _STORED_NAV_FX_NOTE},
            "limitations": _LEDGER_LIMITATIONS,
            # Flagged, never blocked, when the window's snapshots change valuation basis label.
            **portfolio_engine.valuation_basis_change(navs, portfolio_scope),
            "result_status": statistics["result_status"],
            "connector_refresh_required": True,
        }
        _return_ledger_meta(data, flow_evidence, portfolio_scope)
        data['ledgerless_returns'] = _ledgerless_block(store, stored_navs, start, end, portfolio_scope,
                                                       flow_evidence, detail_level)
        if flow_evidence is not None or portfolio_scope != 'equity':
            data['replay_parameters']['portfolio_scope'] = portfolio_scope
        if portfolio_scope == 'kis':
            data['kis_capture_status'] = _kis_capture_status(stored_navs[-1] if stored_navs else None)
        if detail_level == "full":
            data["snapshots"] = [_without_kis_capture(row) for row in stored_navs]
            data["pnl_series"] = portfolio_engine.daily_and_monthly_pnl(
                navs, flows_by_date, flow_reason_codes=list(dict.fromkeys(
                    snapshot_quality["snapshot_reason_codes"] +
                    [reason for row in navs for reason in row.get("reason_codes", [])] +
                    (flow_coverage['reason_codes'] if flow_evidence is not None else
                     [] if external_cashflows_verified_zero else ["MISSING_EXTERNAL_CASHFLOWS"]))),
                external_cashflows_coverage=flow_evidence['coverage'] if flow_evidence is not None else None,
                portfolio_scope=portfolio_scope)
            for index, row in enumerate(data["pnl_series"]["daily"]):
                for metric in row["metrics"].values():
                    metric["input_snapshot_ids"] = [nav["snapshot_id"] for nav in navs[index:index + 2] if nav.get("snapshot_id")]
        else:
            data["snapshots"] = [
                {"snapshot_date": row["snapshot_date"], "total_value_krw": row["total_value_krw"],
                 "snapshot_id": row.get("snapshot_id"), "version": row.get("version"),
                 "result_status": row.get("result_status"), "reason_codes": row.get("reason_codes", [])}
                for row in navs
            ]
        if not navs:
            data["note"] = (
                "No snapshots in range. Wait for the scheduled 18:10 capture or call snapshot_portfolio_nav "
                "(daily, e.g. from cron) to start accumulating history."
            )
        return _query_ids({**_ok(data, provider="k-invest"), "result_status": data["result_status"]},
                          snapshot_id=(navs[-1].get("snapshot_id") if navs else None),
                          calculation_version=statistics["calculation_version"])
    except Exception as e:
        return {**_format_error(e), "result_status": "unavailable"}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def export_portfolio_dataset(
    dataset: Literal["transactions", "realized_pnl", "holdings", "nav_history"],
    period: Literal["MTD", "YTD", "1M", "3M", "6M", "1Y", "ALL"] = "YTD",
    start_date: str = "", end_date: str = "", dataset_id: str | None = None,
    cursor: str | None = None, format: Literal["json", "csv"] = "json", page_size: int = 200,
    account: str = "all", symbol: str = "", input_snapshot_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Freeze an auditable portfolio dataset locally, then return small pages.

    Only for an explicit request to export, save or freeze a dataset. A dated portfolio
    value series is get_portfolio_returns(include_series=true), which stores nothing.

    Transaction exports default to YTD (365 days with end_date alone); pass start_date (e.g. 2024-01-01) or period="ALL" for older history.

    Writes only local dataset copies and never places orders or uploads data.
    First call (no dataset_id) captures normalized inputs through existing engine
    paths. Later calls supply dataset_id and next_cursor as cursor; they read only
    the frozen copy, with no provider/FX refetch. Returned manifest identifies the
    original dates and filters; replay does not reapply collection parameters.
    IDs are content hashes. The latest 20 datasets are retained; expired/unknown
    IDs return DATASET_EXPIRED and never silently rebuild. Start a new first call
    explicitly to collect changed inputs. Missing money stays null with a status.

    JSON rows have record_type, row_id, data. CSV has those first two columns and
    data_json (canonical JSON preserving nested fields, null and statuses).
    Supporting holdings, cash, source coverage, cost FX, quantity breaks and NAV
    components are paged audit records. Manifest descriptors name their row types;
    read every page to reconstruct/hash the entire dataset. Totals sum frozen
    input money by type/currency; reporting summaries retain their stated period.
    Account IDs are opaque keyed hashes. Human account labels are retained only
    when already exposed, and raw broker payloads/credentials are excluded.

    Args:
        dataset: transactions, realized_pnl, holdings, or nav_history
        period: reporting period (default YTD); explicit ISO dates override it
        start_date: ISO YYYY-MM-DD; realized inputs include earlier basis history
        end_date: ISO YYYY-MM-DD. Holdings export is current, not historic backfill
        dataset_id: frozen ID for replay; omit only for a new collection
        cursor: opaque next_cursor from the preceding page; requires dataset_id
        format: json or csv; both carry the same typed audit rows
        page_size: 1–200, additionally limited to 64 KiB of row data per page
        account: existing account filter; nav_history requires all accounts
        symbol: optional existing symbol filter; not applicable to nav_history
        input_snapshot_ids: nav_history only, exact E-store versions; bounds use
            their dates when no explicit dates are supplied
    """
    try:
        if dataset not in {"transactions", "realized_pnl", "holdings", "nav_history"}:
            raise ValueError("unsupported dataset")
        if type(page_size) is not int or not 1 <= page_size <= 200 or format not in {"json", "csv"}:
            raise ValueError("page_size must be 1–200 and format must be json or csv")
        if cursor is not None and dataset_id is None:
            raise DatasetError("INVALID_DATASET_CURSOR", "cursor requires a frozen dataset_id.")
        store = _get_dataset_store()
        if dataset_id is None:
            if input_snapshot_ids is not None and dataset != "nav_history":
                raise ValueError("input_snapshot_ids applies only to nav_history")
            if dataset == "nav_history" and (account != "all" or symbol):
                raise ValueError("nav_history cannot filter whole-portfolio NAV by account or symbol")
            start, end = _resolve_dates(start_date, end_date, default_days=365) if start_date or end_date else portfolio_engine.resolve_period(period)
            nav_store = _get_nav_store() if dataset == "nav_history" else None
            if input_snapshot_ids is not None and not start_date and not end_date:
                selected = nav_store.select_snapshots(input_snapshot_ids)
                start, end = selected[0]["snapshot_date"], selected[-1]["snapshot_date"]
            if dataset == "holdings" and end != datetime.now().date().isoformat():
                raise ValueError("holdings export uses current holdings; historical end_date is unsupported")
            accounts = ["kis:domestic", "kis:overseas"] if account == "kis:primary" else _resolve_accounts(account)
            rows, manifest = collect_dataset(dataset, start, end,
                collector=_get_collector() if dataset != "nav_history" else None, nav_store=nav_store,
                accounts=accounts, account_filter=account, symbol=symbol, input_snapshot_ids=input_snapshot_ids)
            if dataset == "transactions":
                reason = "DEFAULT_WINDOW_365D" if start_date or end_date else f"DEFAULT_WINDOW_{period}"
                manifest.setdefault("ledger_window", {}).update(_ledger_window_metadata(
                    start_date or (start if not end_date and period == "ALL" else ""), reason))
            dataset_id = store.freeze(dataset, rows, manifest)
        data = store.page(dataset, dataset_id, cursor, format, page_size)
        manifest = data["manifest"]
        if dataset == "transactions":
            data.update({key: value for key, value in manifest.get("ledger_window", {}).items()
                         if key in {"window_defaulted", "window_reason_codes", "window_hint"}})
        return {**_ok(data, provider="k-invest"), "result_status": data["result_status"],
                "meta": {"server_version": SERVER_VERSION, "provider": "k-invest", "cache_age_seconds": 0,
                         "query_id": manifest["query_id"], "dataset_id": dataset_id,
                         "snapshot_id": (manifest["snapshot_versions"].get("latest") or {}).get("snapshot_id"),
                         "calculation_version": manifest["calculation_version"]}}
    except DatasetError as exc:
        return {**_fail(exc.code, str(exc), provider="k-invest"), "result_status": "unavailable"}
    except Exception as exc:
        return {**_format_error(exc), "result_status": "unavailable"}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def snapshot_portfolio_nav(snapshot_date: str = "") -> dict[str, Any]:
    """Store today's portfolio NAV snapshot for history and TWR (local write).

    Only on an explicit request to store a snapshot now: the scheduled 18:10 KST job stores
    the daily NAV, and reading returns or history needs no snapshot.
    Re-running appends an immutable version and moves that date's representative, unless the scheduled
    18:10 capture with crypto evidence holds it (snapshot.representative false). Writes only derived
    valuations to the local SQLite store; no broker state is modified. Intended for
    a daily schedule (see `deploy/` for a systemd timer / cron example).

    Args:
        snapshot_date: ISO YYYY-MM-DD (default: today); other dates are refused
            because collection uses current prices, not historical NAV.

    Returns:
        Structured JSON envelope with the stored snapshot.
    """
    store = None
    when = None
    try:
        try:
            when = validate_live_snapshot_date(snapshot_date)
        except ValueError as exc:
            return _fail("INVALID_SNAPSHOT_DATE", str(exc), provider="k-invest", retryable=False)
        store = _get_nav_store()
        collector = _get_collector()
        collection = collector.collect_positions()
        currencies = {h.currency for h in collection.holdings} | {c.currency for c in collection.cash}
        fx = collector.build_fx(currencies)
        summary = portfolio_engine.summarize_portfolio(collection.holdings, collection.cash, fx)
        incomplete = collection.failed_sources + collection.truncated_sources
        if incomplete:
            store.record_attempt(when, "unavailable", (["FAILED_SOURCES"] if collection.failed_sources else []) +
                                 (["TRUNCATED_SOURCES"] if collection.truncated_sources else []))
            return _fail(
                "PORTFOLIO_SOURCES_DEGRADED",
                f"refusing to snapshot an incomplete portfolio: {', '.join(incomplete)}",
                provider="composite",
                retryable=True,
                details={"health": collection.health, "truncated": collection.truncated_sources},
            )
        collect_nav_evidence(collector, collection)
        stored = store.upsert_snapshot(
            snapshot_summary(summary, collection, fx), snapshot_date=when, fx_usdkrw=fx.rates.get("USD"),
            keep_crypto_representative=True,
        )
        return {**_ok(
            {"snapshot": stored, "sources_ok": collection.ok_sources, "db_path": str(_get_nav_store().db_path)},
            provider="k-invest",
        ), "result_status": stored["result_status"]}
    except Exception as e:
        if store is not None and when is not None:
            try:
                store.record_attempt(when, "unavailable", ["SNAPSHOT_COLLECTION_OR_WRITE_FAILED"])
            except Exception:  # noqa: BLE001 — preserve the original error if storage is unavailable
                pass
        return {**_format_error(e), "result_status": "unavailable"}


def _broker_account_of_source(source: str) -> str | None:
    """`toss:<label>[:krw|:usd]` -> `toss:<label>`; `kis[:<label>]:<scope>[:<market>]` -> `kis:<label>`.

    KIS's primary account keeps the unqualified `kis:domestic` / `kis:overseas` names
    (portfolio_sources.kis_source). Provider-level entries (`toss`, `kis`) are not accounts.
    """
    parts = source.split(":")
    if parts[0] == "toss" and len(parts) >= 2:
        return f"toss:{parts[1]}"
    if parts[0] == "kis" and len(parts) >= 2:
        return "kis:primary" if parts[1] in ("domestic", "overseas") else f"kis:{parts[1]}"
    return None


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_account_health(detail_level: Literal["summary", "full"] = "summary") -> dict[str, Any]:
    """Per-account connectivity, auth, and data-freshness status (READ-ONLY).

    Makes live calls for every configured broker account: Toss holdings and KRW/USD
    buying power; KIS domestic and overseas balances (overseas includes cash).
    KIS pension accounts skip unsupported overseas calls. Does not probe SEC,
    yfinance or Binance. Reports source status and freshness, and reads
    local NAV snapshot bounds. Use get_service_health for passive cached status;
    health_check probes selected market-data sources without enumerating accounts.

    Args:
        detail_level: "full" also lists the account labels and holdings counts

    Returns:
        Structured JSON envelope with per-account health.
    """
    try:
        collector = _get_collector()
        collection = collector.collect_positions()
        health = collection.health
        statuses = [entry.get("status") for entry in health.values()]
        overall = (
            "ok"
            if statuses and all(status == "ok" for status in statuses)
            else "degraded"
            if any(status == "ok" for status in statuses)
            else "down"
        )
        broker_accounts = sorted({key for key in map(_broker_account_of_source, health) if key})
        data: dict[str, Any] = {
            "overall_status": overall,
            "checked_at": collection.fetched_at,
            # Distinct broker accounts; `accounts` stays keyed by source (holdings, cash, per market).
            "account_count": len(broker_accounts),
            "source_count": len(health),
            "broker_accounts": broker_accounts,
            "accounts": health,
            "sources_ok": collection.ok_sources,
            "sources_failed": collection.failed_sources,
            "data_freshness": {
                "positions_fetched_at": collection.fetched_at,
                "cache_policy": "live per call — holdings and cash are never served from cache",
                "nav_snapshots": dict(zip(("first", "last"), _get_nav_store().bounds())),
            },
        }
        if detail_level == "full":
            data["holdings_count_by_account"] = _count_by(collection.holdings)
            data["cash_count_by_account"] = _count_by(collection.cash)
            data["rolling_health"] = collector.health_report()
        return _ok(data, provider="k-invest")
    except Exception as e:
        return _format_error(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_portfolio_risk(detail_level: Literal["summary", "full"] = "summary") -> dict[str, Any]:
    """Return best-effort portfolio concentration and currency exposure risk."""
    # One path with get_consolidated_holdings: same collection, KRW spot FX and canonical
    # marks, so top_positions[].weight_pct equals its weight_pct over every account.
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:  # perps are read while the brokers are collected
            perp_read = pool.submit(_perp_exposure_inputs)
            collection = _get_collector().collect_positions()
            fx = _get_collector().build_fx({h.currency for h in collection.holdings})
            perps, perp_fx = perp_read.result()
        consolidated = portfolio_engine.consolidate_holdings(collection.holdings, fx,
                                                             quotes=_holding_mark_quotes(collection.holdings),
                                                             perps=perps, perp_fx=perp_fx)
        data = portfolio_engine.concentration_risk(consolidated)
        data["source_errors"] = {
            source: ((entry.get("last_error") or {}).get("message") or entry.get("status"))
            for source, entry in collection.health.items() if entry.get("status") != "ok"
        }
        data["sources_ok"] = collection.ok_sources
        data["sources_failed"] = collection.failed_sources
        if fx.missing:
            data["concentration"]["reason_codes"] = sorted({*data["concentration"]["reason_codes"], "MISSING_FX_RATE"})
        if detail_level == "summary":
            data.pop("total_unconverted_value", None)  # fx stays: it states the FX policy once
            data["top_positions"] = data["top_positions"][:5]
        return _ok(data, provider="composite", fetched_at=collection.fetched_at)
    except Exception as e:
        return _format_error(e)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> Response:
    return Response("ok", media_type="text/plain")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def get_service_health(mode: str = "passive") -> dict[str, Any]:
    """Read cached service/source status with mode="passive": no upstream calls or
    account reads. The local OAuth store is checked if initialized. mode="active"
    returns INVALID_MODE; use health_check for source probes or get_account_health
    for live account holdings/cash checks.

    Never-observed and unconfigured sources are distinct from successfully empty
    history. Freshness is observation age, not a broker data timestamp.
    """
    if mode != "passive":
        return _fail("INVALID_MODE", "Only passive health is supported.")
    configured = _broker_configuration()
    data = _health_status.report({k: bool(v) if v is not None else None for k, v in configured.items()})
    data["sec_user_agent_configured"] = sec_user_agent_configured()
    store_status = _oauth_provider.store.health() if _oauth_provider is not None else "disabled"
    data["auth"] = {"store_status": store_status, "restart_durable": _oauth_provider is not None,
                    "access_ttl_seconds": _oauth_provider.access_ttl if _oauth_provider else None,
                    "refresh_ttl_seconds": _oauth_provider.refresh_ttl if _oauth_provider else None,
                    "refresh_grace_seconds": _oauth_provider.refresh_grace if _oauth_provider else None}
    if store_status == "unavailable":
        data["overall_status"] = "auth_store_unavailable"
    return _ok(data, provider="k-invest", mode="passive", cache_age_seconds=None)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def health_check() -> dict[str, Any]:
    """Probe source availability with upstream calls, without account holdings reads.

    Calls Toss AAPL prices, default KIS AAPL/NASDAQ quote, SEC AAPL CIK lookup,
    yfinance AAPL key metrics, and Binance BTCUSDT price (provider caches may
    apply). KIS failure triggers a Toss price fallback. Other configured account
    labels are not enumerated. Use get_account_health for every broker account,
    or get_service_health for passive cached observations."""
    checks: dict[str, Any] = {}
    probes = [
        ("toss_market_data", lambda: _get_toss().get_prices("AAPL")),
        ("kis_market_data", lambda: _get_kis().get_overseas_quote("AAPL", exchange="NASDAQ")),
        ("sec", lambda: _get_sec().get_cik("AAPL")),
        ("yfinance", lambda: _get_yf().get_key_metrics("AAPL")),
        ("binance_futures", lambda: _get_binance().price_ticker("BTCUSDT")),
    ]
    for name, probe in probes:
        try:
            value = probe()
            checks[name] = {"status": "ok" if value else "degraded"}
        except NotConfiguredError as e:
            error = classify_error(e)
            checks[name] = {"status": "not_configured", "error": error if name == "sec" else error["message"]}
        except Exception as e:
            if name == "kis_market_data":
                try:
                    fallback = _get_toss().get_prices("AAPL")
                    checks[name] = {"status": "degraded_fallback_ok", "error": classify_error(e)["message"], "fallback_provider": "toss", "fallback_ok": bool(fallback)}
                    continue
                except Exception:
                    pass
            checks[name] = {"status": "degraded", "error": classify_error(e)["message"]}
    return _ok(checks, provider="k-invest")


# Merged chat catalog: composes the tools above.
tools_v2.register(sys.modules[__name__])


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
