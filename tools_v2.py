"""Merged tool catalog for chat clients.

Each new tool composes the existing tool functions in server.py: it calls them
with the caller's inputs and places their `data` into fixed sections, so one
computation path serves the old and the new names. Nothing here computes a
portfolio figure itself.

Who sees what:
- chat clients (OAuth principals) list only CHAT_TOOLS; an old name they call still
  runs, and its response carries meta.deprecated = {use, removal: "next minor"}.
- the static operator bearer and local callers list the whole registry (old names, the
  merged tools under their registered names); nothing in their responses changes.
- service principals keep their allowlists (service_auth.py) untouched.

Within one new-tool call, broker holdings, FX and mark quotes are read once and
shared by every composed body (`_one_read`): the sections then describe the same
collection, as the old tools did when called one after another.
"""
from __future__ import annotations

import base64
import copy
import dataclasses
import hashlib
import json
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from functools import partial, wraps
from typing import Any, Literal

from mcp.types import TextContent, ToolAnnotations

import portfolio_engine
import response_shape
from service_auth import ServiceAccessToken

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
READ_LOCAL = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
IMPORT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)

SUMMARY, FULL, RAW = "summary", "full", "raw"
CHAT, OPERATOR, SERVICE, LOCAL = "chat", "operator", "service", "local"
#: Static bearer principals (DualTokenVerifier / StaticTokenVerifier): the operator and
#: a post-deploy check script. Every other authenticated non-service principal is a
#: chat client (OAuth: dynamic registration or the manual client).
STATIC_CLIENT_IDS = frozenset({"static-token-client", "k-invest-client"})

#: (name chat clients see, registered name). Two public names belong to old tools that
#: service principals still call with their old contract, so the new tool is registered
#: under an internal name and renamed in the chat list.
CHAT_TOOLS: tuple[tuple[str, str], ...] = (
    ("get_status", "get_status"),
    ("get_holdings", "get_holdings"),
    ("get_portfolio_summary", "get_portfolio_summary_v2"),
    ("get_portfolio_returns", "get_portfolio_returns"),
    ("get_transactions", "get_transactions"),
    ("get_trading_pnl", "get_trading_pnl"),
    ("get_quote", "get_quote_v2"),
    ("get_candles", "get_candles"),
    ("get_market_info", "get_market_info"),
    ("get_crypto_market", "get_crypto_market"),
    ("get_crypto_positions", "get_crypto_positions"),
    ("get_crypto_account_history", "get_crypto_account_history"),
    ("snapshot_portfolio_nav", "snapshot_portfolio_nav"),
    ("export_portfolio_dataset", "export_portfolio_dataset"),
    ("import_statement", "import_statement"),
    # T11 (later): fundamentals stay listed unchanged for now.
    ("get_stock_snapshot", "get_stock_snapshot"),
    ("get_financials", "get_financials"),
    ("get_key_metrics", "get_key_metrics"),
    ("get_sec_financials", "get_sec_financials"),
    ("get_ttm_financials", "get_ttm_financials"),
    ("get_insider_trades", "get_insider_trades"),
)
PUBLIC_TO_INTERNAL = {public: internal for public, internal in CHAT_TOOLS if public != internal}

#: Old name -> the tool that replaces it for chat clients.
REPLACED_BY: dict[str, str] = {
    **dict.fromkeys(("get_service_health", "health_check", "get_account_health", "get_toss_accounts",
                     "get_kis_accounts", "get_invest_mcp_help"), "get_status"),
    **dict.fromkeys(("get_consolidated_holdings", "get_toss_holdings", "get_toss_buying_power",
                     "get_kis_domestic_balance", "get_kis_overseas_balance", "get_kis_cash_balance"), "get_holdings"),
    "get_portfolio_risk": "get_portfolio_summary",
    **dict.fromkeys(("get_portfolio_performance", "get_portfolio_history"), "get_portfolio_returns"),
    **dict.fromkeys(("get_all_transactions", "get_toss_trade_history", "get_kis_trade_history"), "get_transactions"),
    **dict.fromkeys(("get_realized_pnl", "get_trade_review"), "get_trading_pnl"),
    **dict.fromkeys(("get_kis_domestic_quote", "get_kis_overseas_quote", "compare_quotes", "get_orderbook",
                     "get_market_trades", "get_recent_trades", "get_price_limits", "get_stock_warnings",
                     "get_stock_info"), "get_quote"),
    # T8: the price-performance comparison lives in the Ticker Research server.
    "get_market_series": "ticker_research.compare_price_performance",
    **dict.fromkeys(("get_market_hours", "get_exchange_rate", "get_risk_free_rate"), "get_market_info"),
    **dict.fromkeys(("get_crypto_futures_snapshot", "get_binance_futures_quote", "get_binance_futures_mark_price",
                     "get_binance_funding_rate", "get_binance_funding_rate_history", "get_binance_open_interest",
                     "get_binance_futures_candles"), "get_crypto_market"),
    "get_crypto_futures_positions": "get_crypto_positions",
    **dict.fromkeys(("get_crypto_futures_fills", "get_crypto_futures_funding_payments"), "get_crypto_account_history"),
    **dict.fromkeys(("preview_statement_import", "apply_statement_import"), "import_statement"),
}
#: T0: deprecated aliases: absent from every client list (chat and operator; no service allowlist names
#: them), still callable.
HIDDEN = frozenset({"get_recent_trades", "get_binance_funding_rate"})
REMOVAL = "next minor"

_srv: Any = None
_app: Any = None


# ── principals, lists and call routing (hooks from ObservedFastMCP) ──────────


def principal(app: Any) -> str:
    try:
        request = app._mcp_server.request_context.request
    except LookupError:
        return LOCAL
    user = request.scope.get("user") if request is not None else None
    token = getattr(user, "access_token", None)
    if isinstance(token, ServiceAccessToken):
        return SERVICE
    if token is None:
        return LOCAL
    return OPERATOR if token.client_id in STATIC_CLIENT_IDS else CHAT


def visible_tools(app: Any, tools: list[Any]) -> list[Any]:
    """tools/list for a non-service caller of the served app."""
    if app is not _app:
        return tools
    who = principal(app)
    if who == LOCAL:  # in-process registry introspection; the server only serves authenticated HTTP
        return tools
    if who != CHAT:
        # Deprecated aliases leave every client list (service lists follow their allowlists).
        return [tool for tool in tools if tool.name not in HIDDEN]
    by_name = {tool.name: tool for tool in tools}
    return [by_name[internal] if public == internal else by_name[internal].model_copy(update={"name": public})
            for public, internal in CHAT_TOOLS if internal in by_name]


def route(app: Any, name: str, arguments: dict[str, Any] | None):
    """(registered name, arguments, finish(result)) for one tools/call."""
    if app is not _app or principal(app) != CHAT:
        return name, arguments, _same
    if name in PUBLIC_TO_INTERNAL:
        return PUBLIC_TO_INTERNAL[name], _legacy_arguments(name, dict(arguments or {})), _same
    if name in REPLACED_BY:
        arguments = dict(arguments or {})
        if name == "get_all_transactions":
            arguments.setdefault("detail_level", SUMMARY)  # chat default is the aggregate answer
        return name, arguments, partial(_deprecated, {"use": REPLACED_BY[name], "removal": REMOVAL})
    return name, arguments, _same


def _same(result: Any) -> Any:
    return result


def _legacy_arguments(public: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Old-contract arguments sent to a public name now served by the merged tool."""
    if public == "get_quote" and "symbols" not in arguments and isinstance(arguments.get("symbol"), str):
        arguments["symbols"] = [arguments.pop("symbol")]
    if public == "get_portfolio_summary":
        arguments.pop("snapshot", None)  # T0: only snapshot_portfolio_nav writes NAV
        if arguments.pop("include_realized", True) is False and "sections" not in arguments:
            arguments["sections"] = ["totals"]
    return arguments


def _deprecated(note: dict[str, str], result: Any) -> Any:
    content, structured = _srv._result_parts(result)
    if not isinstance(structured, dict):
        return result
    meta = structured.get("meta") if isinstance(structured.get("meta"), dict) else {}
    structured = {**structured, "meta": {**meta, "deprecated": {**(meta.get("deprecated") or {}), **note}}}
    text = TextContent(type="text", text=response_shape.compact_json(structured))
    return _srv._with_parts(result, [text, *(block for block in content if not isinstance(block, TextContent))],
                            structured)


# ── one broker read per call ─────────────────────────────────────────────────


class _Reads:
    def __init__(self, accounts: list[str] | None):
        self.accounts = accounts
        self.lock = threading.RLock()
        self.positions: dict[Any, Any] = {}
        self.transactions: dict[str, Any] = {}
        self.fx: Any = None
        self.fx_currencies: set[str] = set()
        self.quotes: dict[frozenset, Any] = {}


_READS: ContextVar[_Reads | None] = ContextVar("tools_v2_reads", default=None)


@contextmanager
def _one_read(accounts: list[str] | None = None):
    token = _READS.set(_Reads(accounts))
    try:
        yield
    finally:
        _READS.reset(token)


def _fx_subset(fx: Any, wanted: set[str]) -> Any:
    """The rates a build_fx(wanted) call would hold, cut from a read over a superset."""
    if not dataclasses.is_dataclass(fx):
        return copy.deepcopy(fx)
    values = {}
    for field in dataclasses.fields(fx):
        if not field.init:
            continue
        value = getattr(fx, field.name)
        if isinstance(value, dict):
            value = {key: copy.deepcopy(item) for key, item in value.items() if key in wanted}
        elif isinstance(value, set):
            value = {key for key in value if key in wanted}
        else:
            value = copy.deepcopy(value)
        values[field.name] = value
    return type(fx)(**values)


class _SharedCollector:
    """The process collector seen through one call's memo; every other attribute passes through."""

    def __init__(self, collector: Any, reads: _Reads):
        self._collector, self._reads = collector, reads

    def __getattr__(self, name: str) -> Any:
        return getattr(self._collector, name)

    def collect_positions(self, accounts: list[str] | None = None) -> Any:
        accounts = self._reads.accounts if accounts is None else accounts
        key = None if accounts is None else tuple(accounts)
        with self._reads.lock:
            if key not in self._reads.positions:
                self._reads.positions[key] = (self._collector.collect_positions() if accounts is None
                                              else self._collector.collect_positions(accounts))
            return copy.deepcopy(self._reads.positions[key])

    def collect_transactions(self, *args: Any, **kwargs: Any) -> Any:
        key = response_shape.compact_json([args, sorted(kwargs.items())])
        with self._reads.lock:
            if key not in self._reads.transactions:
                self._reads.transactions[key] = self._collector.collect_transactions(*args, **kwargs)
            return copy.deepcopy(self._reads.transactions[key])

    def build_fx(self, currencies: Any) -> Any:
        wanted = {(currency or "").upper() for currency in currencies} - {"", "KRW"}
        with self._reads.lock:
            if self._reads.fx is None or not wanted <= self._reads.fx_currencies:
                union = wanted | self._reads.fx_currencies
                self._reads.fx, self._reads.fx_currencies = self._collector.build_fx(union), union
            return _fx_subset(self._reads.fx, wanted)


def _install_memo(srv: Any) -> None:
    get_collector, mark_quotes = srv._get_collector, srv._holding_mark_quotes

    @wraps(get_collector)
    def _get_collector():
        collector, reads = get_collector(), _READS.get()
        return collector if reads is None else _SharedCollector(collector, reads)

    @wraps(mark_quotes)
    def _holding_mark_quotes(holdings):
        reads = _READS.get()
        if reads is None:
            return mark_quotes(holdings)
        key = frozenset((h.symbol.upper(), h.currency.upper()) for h in holdings if h.quantity > 0)
        with reads.lock:
            if key not in reads.quotes:
                reads.quotes[key] = mark_quotes(holdings)
            return copy.deepcopy(reads.quotes[key])

    srv._get_collector, srv._holding_mark_quotes = _get_collector, _holding_mark_quotes


def _prewarm(include_cash: bool) -> None:
    """Read positions and the FX every composed body will need, once."""
    try:
        collector = _srv._get_collector()
        collection = collector.collect_positions()
        currencies = {h.currency for h in collection.holdings}
        if include_cash:
            currencies |= {c.currency for c in collection.cash}
        collector.build_fx(currencies)
    except Exception:  # noqa: BLE001 — each composed body reports its own failure
        pass


# ── sections ─────────────────────────────────────────────────────────────────


def _shaped(env: Any, old: str, level: str) -> Any:
    """An old tool's envelope at `level` with that tool's own summary policy (drops and hooks)."""
    policy = response_shape.POLICIES.get(old)
    if level != SUMMARY or policy is None or not isinstance(env, dict):
        return env
    return response_shape.shape(env, dataclasses.replace(policy, generic=False), SUMMARY)


class _Sections:
    """Builds one envelope: data.<section> is an old tool's data (or an error block)."""

    def __init__(self, level: str = SUMMARY):
        self.level = level
        self.data: dict[str, Any] = {}
        self.meta: dict[str, Any] = {}
        self.statuses: list[str | None] = []
        self.errors: list[Any] = []

    def add(self, section: str, old: str, env: Any, pick: Any = None, status: str | None = None) -> Any:
        """Place `env` (an old tool's result) at data.<section>; returns its data or None.

        status: the section's own result_status when the old envelope's status also covers parts
        another section reports (one shared read, several sections).
        """
        env = _shaped(env, old, self.level)
        meta = dict(env.get("meta") or {}) if isinstance(env, dict) else {}
        meta.pop("server_version", None)
        detail = meta.pop("detail", None)
        info = {**_provenance(old, env, meta), "tool": old,
                **{k: v for k, v in meta.items() if v is not None and k != "provider"}}
        if detail and detail.get("level") == SUMMARY and len(detail) > 1:
            info["detail"] = detail
        if isinstance(env, dict) and "result_status" in env:
            info["result_status"] = env["result_status"] if status is None or not env.get("ok") else status
        self.meta[section] = info
        if not isinstance(env, dict) or not env.get("ok"):
            error = env.get("error") if isinstance(env, dict) else {"code": "INVALID_RESULT"}
            self.data[section] = {"error": error, "result_status": "unavailable"}
            self.errors.append(error)
            self.statuses.append("unavailable")
            return None
        value = env.get("data")
        self.data[section] = pick(value) if pick else value
        if status is not None and isinstance(self.data[section], dict) and "result_status" in self.data[section]:
            self.data[section] = {**self.data[section], "result_status": status}
        self.statuses.append(status or env.get("result_status") or
                             (value.get("result_status") if isinstance(value, dict) else None) or "complete")
        return value

    def envelope(self, provider: str = "composite", drop: tuple[str, ...] = (), hooks: tuple[Any, ...] = (),
                 **meta: Any) -> dict[str, Any]:
        statuses = self.statuses or ["complete"]
        failed = statuses.count("unavailable")
        status = ("unavailable" if failed == len(statuses) else "partial" if failed or "partial" in statuses
                  else "empty" if all(s == "empty" for s in statuses) else "complete")
        ok = failed < len(statuses)
        providers = {section: info.get("provider") for section, info in self.meta.items()}
        env = {"ok": ok, "data": self.data if ok else None, "error": None if ok else self.errors[0],
               "meta": {"server_version": _srv.SERVER_VERSION, "provider": provider, "providers": providers,
                        **{k: v for k, v in meta.items() if v is not None}, "sections": self.meta},
               "result_status": status}
        if not ok:
            env["meta"]["failed_sections"] = self.data
        if self.level == SUMMARY:
            return response_shape.shape(env, response_shape.Policy(drop=drop, hooks=hooks), SUMMARY)
        env["meta"]["detail"] = {"level": self.level}
        return env


# ── section provenance ───────────────────────────────────────────
# Every section names the provider of its data and the data's own time (as_of), not just the
# old tool it came from (Claude I11 on 2.15.0 could not say the FX rate was Toss's).

#: Data provider of each composed tool whose envelope leaves meta.provider empty (the old
#: tools named it only in their description). A tool's own meta.provider wins when set.
TOOL_PROVIDERS = {
    "get_exchange_rate": "toss", "get_market_hours": "toss", "get_orderbook": "toss",
    "get_market_trades": "toss", "get_price_limits": "toss", "get_stock_warnings": "toss",
    "get_stock_info": "toss", "get_kis_domestic_quote": "kis", "get_kis_overseas_quote": "kis",
    "get_kis_domestic_balance": "kis", "get_kis_overseas_balance": "kis", "get_kis_cash_balance": "kis",
    "get_kis_accounts": "kis", "health_check": "k-invest", "get_account_health": "k-invest",
}
PROVIDER_NAMES = {
    "toss": "토스증권 (Toss Securities)",
    "kis": "한국투자증권 (Korea Investment & Securities)",
    "kis_fallback_toss": "토스증권 (Toss Securities); KIS was unavailable",
    "yfinance": "Yahoo Finance (yfinance)",
    "binance": "Binance USD-M futures, public market data",
    "composite": "several providers; see the section's data",
    "k-invest": "InvestMCP local state",
}
#: Venue of public crypto market data.
TOOL_VENUES = {name: "binance_usd_m_futures" for name in (
    "get_crypto_futures_snapshot", "get_binance_funding_rate_history", "get_binance_open_interest",
    "get_binance_futures_candles")}


def _epoch_ms(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _as_of(old: str, data: Any, meta: dict[str, Any]) -> dict[str, Any]:
    """The time the section's data describes, from the data itself; null plus a reason otherwise."""
    if old == "get_exchange_rate" and isinstance(data, dict) and data.get("validFrom"):
        return {"as_of": data["validFrom"], "as_of_basis": "validFrom", "valid_until": data.get("validUntil")}
    if old == "get_risk_free_rate" and isinstance(data, dict) and data.get("date"):
        return {"as_of": data["date"], "as_of_basis": "close_date"}
    if old == "get_crypto_futures_snapshot" and isinstance(data, dict):
        stamp = next((_epoch_ms((data.get(key) or {}).get("time")) for key in ("mark", "quote")
                      if isinstance(data.get(key), dict) and data[key].get("time") is not None), None)
        if stamp:
            return {"as_of": stamp, "as_of_basis": "exchange_time"}
    if old == "get_quote" and isinstance(data, list):
        stamps = sorted(str(row["timestamp"]) for row in data if isinstance(row, dict) and row.get("timestamp"))
        if stamps:
            return {"as_of": stamps[-1], "as_of_basis": "timestamp"}
    if old == "compare_quotes" and isinstance(data, dict):
        by = {q.get("provider"): q.get("priced_at") for q in data.get("quotes") or [] if isinstance(q, dict)}
        return {"as_of": by.get("toss"), "as_of_basis": "priced_at", "as_of_by_provider": by}
    if old in ("get_kis_domestic_quote", "get_kis_overseas_quote"):
        return {"as_of": None, "as_of_reason": "UNKNOWN_QUOTE_TIME"}  # KIS price TRs carry no price time
    for key in ("as_of", "fetched_at"):
        if meta.get(key):
            return {"as_of": meta[key], "as_of_basis": key}
    return {"as_of": None, "as_of_reason": "SOURCE_TIME_ABSENT" if data is not None else "SECTION_UNAVAILABLE"}


def _provenance(old: str, env: Any, meta: dict[str, Any]) -> dict[str, Any]:
    provider = meta.get("provider") or TOOL_PROVIDERS.get(old)
    data = env.get("data") if isinstance(env, dict) and env.get("ok") else None
    info = {"provider": provider, "provider_name": PROVIDER_NAMES.get(provider), **_as_of(old, data, meta),
            "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if old in TOOL_VENUES:
        info["venue"] = TOOL_VENUES[old]
    return info


# ── summary moves (chat defaults <= 27 KB live) ──────────────────────
# Blocks a merged tool's summary moves to detail_level="full". Each move is listed in
# meta.detail.omitted, and every reason code inside a moved block is tallied in
# meta.detail.omitted_reason_codes, so no reason code leaves the answer.


def _at(node: Any, tokens: list[str]):
    """(holder, key) of each value at `tokens` (response_shape path syntax without `**`)."""
    head, rest = tokens[0], tokens[1:]
    if head == "[]":
        for item in node if isinstance(node, list) else ():
            yield from _at(item, rest)
        return
    if not isinstance(node, dict):
        return
    for key in (list(node) if head == "*" else [head] if head in node else []):
        if rest:
            yield from _at(node[key], rest)
        else:
            yield node, key


def _reason_codes(node: Any, out: dict[str, int]) -> dict[str, int]:
    """Codes under any key naming a reason (reason_codes, difference_reasons, reason, ...)."""
    if isinstance(node, dict):
        for key, value in node.items():
            if "reason" in str(key):
                for code in value if isinstance(value, list) else [value]:
                    if isinstance(code, str) and code and " " not in code:
                        out[code] = out.get(code, 0) + 1
            _reason_codes(value, out)
    elif isinstance(node, list):
        for item in node:
            _reason_codes(item, out)
    return out


def _to_full(*patterns: str, when: Any = None):
    """Summary hook: the values at `patterns` move to full (`when(value)` decides, if given)."""
    def hook(env: dict[str, Any], omitted: dict[str, Any]) -> None:
        for pattern in patterns:
            moved, codes = 0, {}
            for holder, key in list(_at(env, response_shape._tokens(pattern))):
                if when is None or when(holder[key]):
                    _reason_codes(holder.pop(key), codes)
                    moved += 1
            if moved:
                omitted[pattern] = omitted.get(pattern, 0) + moved
            if codes:
                omitted.setdefault(response_shape._REASON_TALLY, {})[pattern] = dict(sorted(codes.items()))
    return hook


def _null_keys(*patterns: str, keep: tuple[str, ...] = ()):
    """Summary hook: null-valued keys of the dicts at `patterns` move to full (a metric's
    value/status/reason keys and `keep` stay); the key names are listed once per pattern."""
    def hook(env: dict[str, Any], omitted: dict[str, Any]) -> None:
        for pattern in patterns:
            names, moved = set(), 0
            for holder, key in list(_at(env, response_shape._tokens(pattern))):
                block = holder[key]
                for name in [name for name, value in block.items() if value is None] if isinstance(block, dict) else ():
                    if name not in keep and not response_shape._protected(name):
                        del block[name]
                        names.add(name)
                        moved += 1
            if moved:
                omitted[f"{pattern} null keys: {','.join(sorted(names))}"] = moved
    return hook


def _keep_keys(pattern: str, keep: tuple[str, ...]):
    """Summary hook: each row of the lists at `pattern` keeps only `keep`; its other keys move to
    full (key names listed once, reason codes in moved values tallied)."""
    def hook(env: dict[str, Any], omitted: dict[str, Any]) -> None:
        names, moved, codes = set(), 0, {}
        for holder, key in list(_at(env, response_shape._tokens(pattern))):
            for row in holder[key] if isinstance(holder[key], list) else ():
                drop = [name for name in row if name not in keep] if isinstance(row, dict) else []
                for name in drop:
                    _reason_codes(row.pop(name), codes)
                names.update(drop)
                moved += bool(drop)
        if moved:
            omitted[f"{pattern}[] keys: {','.join(sorted(names))}"] = moved
        if codes:
            omitted.setdefault(response_shape._REASON_TALLY, {})[f"{pattern}[]"] = dict(sorted(codes.items()))
    return hook


def _invalid(code: str, message: str) -> dict[str, Any]:
    return {**_srv._fail(code, message, provider="k-invest"), "result_status": "unavailable"}


def _choices(values: Any, allowed: tuple[str, ...], name: str) -> list[str] | dict[str, Any]:
    values = list(dict.fromkeys(values or ()))
    unknown = [value for value in values if value not in allowed]
    if unknown:
        return _invalid("INVALID_ARGUMENT", f"{name} must be a list of {list(allowed)}")
    return values


# ── T1 get_status ────────────────────────────────────────────────────────────


def get_status(mode: Literal["passive", "probe", "accounts", "help"] = "passive",
               detail_level: Literal["summary", "full"] = "summary", topic: str = "overview") -> dict[str, Any]:
    """Connector status: cached service health, live source probes, configured accounts, or the usage guide.

    mode="passive" reads cached status only (no upstream calls); "probe" calls each
    market-data source once; "accounts" lists the configured broker accounts and checks
    each one live; "help" returns the usage guide for this tool list (topic: overview,
    quotes, portfolio, financials, futures, futures_account, diagnostics, examples, all).
    """
    out = _Sections(detail_level)
    if mode == "passive":
        out.add("service_health", "get_service_health", _srv.get_service_health("passive"))
    elif mode == "probe":
        out.add("health_check", "health_check", _srv.health_check())
    elif mode == "accounts":
        out.add("toss_accounts", "get_toss_accounts", _srv.get_toss_accounts())
        out.add("kis_accounts", "get_kis_accounts", _srv.get_kis_accounts())
        out.add("account_health", "get_account_health", _srv.get_account_health(detail_level=detail_level))
    elif mode == "help":
        out.add("help", "get_invest_mcp_help", _srv._ok(_srv.build_help(_chat_catalog(), topic), provider="k-invest"))
    else:
        return _invalid("INVALID_MODE", "mode must be passive, probe, accounts or help")
    return out.envelope(provider="k-invest", mode=mode)


def _chat_catalog() -> list[Any]:
    manager = _app._tool_manager
    tools = []
    for public, internal in CHAT_TOOLS:
        tool = manager.get_tool(internal)
        if tool is not None:
            tools.append(tool if public == internal else tool.model_copy(update={"name": public}))
    return tools


# ── T2 get_holdings ──────────────────────────────────────────────────────────


def get_holdings(account: str = "all", include_cash: bool = True,
                 detail_level: Literal["summary", "full", "raw"] = "summary") -> dict[str, Any]:
    """Current positions, merged per symbol in native currency and KRW, plus cash.

    account: "all", "toss", "kis", or one account label ("toss:primary", "kis:pension").
    detail_level "summary" lists the top 15 positions; "full" lists every position with
    its accounts and the look-through exposure per underlying; "raw" returns each
    broker's own balance payloads unmerged.
    Fetched broker rows are recorded in the local append-only ledger (no broker writes).
    """
    if detail_level == RAW:
        return _raw_holdings(account, include_cash)
    try:
        accounts = ["kis:domestic", "kis:overseas"] if account == "kis:primary" else _srv._resolve_accounts(account)
    except Exception as exc:  # noqa: BLE001 — the old tools' error envelope, never a ToolError
        return {**_srv._format_error(exc), "result_status": "unavailable"}
    out = _Sections(detail_level)
    with _one_read(accounts):
        _prewarm(include_cash)
        out.add("holdings", "get_consolidated_holdings", _srv.get_consolidated_holdings(detail_level=detail_level))
        if include_cash:
            out.add("cash", "get_portfolio_summary", _cash_section())
    return out.envelope(account=account, hooks=_HOLDINGS_SUMMARY)


_POSITIONS = "data.holdings.positions[]"
#: The look-through block (about 10 KB live; the summary tool's exposure section), fixed
#: rule text, and per-position identity stubs that carry no value: the product
#: relationship / look-through placeholders (status unavailable, reason codes tallied) and
#: null fields of the identity and cost-basis blocks (their status and reason codes stay).
_HOLDINGS_SUMMARY = (
    _to_full("data.holdings.underlying_exposure", "data.holdings.mark_policy"),
    _to_full(f"{_POSITIONS}.instrument_metadata.product_relationship",
             when=lambda block: isinstance(block, dict) and block.get("status") == "unavailable"),
    _to_full(f"{_POSITIONS}.instrument_metadata.lookthrough",
             when=lambda block: isinstance(block, dict) and block.get("constituents") is None),
    _null_keys(*(f"{_POSITIONS}.{block}" for block in ("instrument_metadata", "ledger_cost_basis",
                                                       "broker_reported_cost_basis", "cost_basis_difference")),
               keep=("amount",)),
)


def _cash_section() -> dict[str, Any]:
    """Cash fields of the portfolio summary computation over this call's collection."""
    try:
        collector = _srv._get_collector()
        collection = collector.collect_positions()
        fx = collector.build_fx({h.currency for h in collection.holdings} | {c.currency for c in collection.cash})
        summary = portfolio_engine.summarize_portfolio(collection.holdings, collection.cash, fx,
                                                       quotes=_srv._holding_mark_quotes(collection.holdings))
        data = {"cash_krw": summary["cash_krw"], "cash_by_currency": summary["cash_by_currency"],
                "cash_krw_by_account": {account: row["cash_krw"] for account, row in summary["by_account"].items()
                                        if "cash_krw" in row},
                "total_assets_krw": summary["total_assets_krw"],
                "sources_failed": collection.failed_sources}
        if fx.missing:
            data["reason_codes"] = ["MISSING_FX_RATE"]
        status = "partial" if collection.failed_sources or collection.truncated_sources or fx.missing else "complete"
        return {**_srv._ok(data, provider="composite", fetched_at=collection.fetched_at), "result_status": status}
    except Exception as exc:  # noqa: BLE001
        return {**_srv._format_error(exc), "result_status": "unavailable"}


def _broker_scope(account: str) -> tuple[str | None, str | None]:
    """(toss account arg, kis account arg) for the raw broker tools; None skips a broker."""
    wanted = (account or "all").strip().lower()
    if wanted == "all":
        return "all", "all"
    if wanted == "toss":
        return "all", None
    if wanted == "kis":
        return None, "all"
    if wanted.startswith("kis:"):
        label = wanted.split(":")[1]
        return None, "" if label in ("primary", "domestic", "overseas") else label
    return wanted.split(":", 1)[1] if wanted.startswith("toss:") else wanted, None


def _raw_holdings(account: str, include_cash: bool) -> dict[str, Any]:
    toss, kis = _broker_scope(account)
    out = _Sections(RAW)
    if toss is not None:
        out.add("toss_holdings", "get_toss_holdings", _srv.get_toss_holdings(account=toss))
        if include_cash:
            for currency in ("KRW", "USD"):
                out.add(f"toss_buying_power_{currency.lower()}", "get_toss_buying_power",
                        _srv.get_toss_buying_power(currency=currency, account=toss))
    if kis is not None:
        out.add("kis_domestic_balance", "get_kis_domestic_balance", _srv.get_kis_domestic_balance(account=kis))
        out.add("kis_overseas_balance", "get_kis_overseas_balance", _srv.get_kis_overseas_balance(account=kis))
        if include_cash:
            out.add("kis_cash_balance", "get_kis_cash_balance", _srv.get_kis_cash_balance(account=kis))
    return out.envelope(account=account)


# ── T3 get_portfolio_summary ─────────────────────────────────────────────────

SUMMARY_SECTIONS = ("totals", "exposure", "concentration", "realized")
#: The old tool's default answer (include_realized=True); with all four sections the
#: summary-level response is about 45 KB live, over the 30 KB default budget.
DEFAULT_SECTIONS = ("totals", "realized")
#: get_portfolio_summary data keys that belong to the realized section.
_REALIZED_EXTRA = frozenset({"dividends_krw", "cost_fx", "limitations"})
#: Exposure keys: consolidated grouping and the risk tool's currency/underlying exposure.
_EXPOSURE_FROM_HOLDINGS = ("by_currency", "by_market")
_EXPOSURE_FROM_RISK = ("currency_exposure", "currency_weights_pct", "underlying_exposure")


def _realized_key(key: str) -> bool:
    return key.startswith(("realized_", "fees", "taxes", "ledger_")) or key in _REALIZED_EXTRA


def get_portfolio_summary_v2(
    sections: list[Literal["totals", "exposure", "concentration", "realized"]] = list(DEFAULT_SECTIONS),
    detail_level: Literal["summary", "full"] = "summary",
) -> dict[str, Any]:
    """Whole-portfolio totals across every broker account, by section.

    totals: total assets, stock value, cash, cost basis, unrealized P&L, per-account
    breakdown and the NAV bridge, in KRW at current spot. exposure: weights by currency
    and market, and look-through exposure per underlying (leveraged products at their
    daily multiple; open crypto futures at signed notional, never in totals).
    concentration: top positions, HHI and risk level. realized: year-to-date realized
    P&L, dividends, fees and taxes from the ledger (slower). Default: totals and realized.
    Never writes a snapshot.
    Fetched broker rows are recorded in the local append-only ledger (no broker writes).
    """
    wanted = _choices(sections, SUMMARY_SECTIONS, "sections")
    if isinstance(wanted, dict):
        return wanted
    wanted = [name for name in SUMMARY_SECTIONS if name in wanted] or list(DEFAULT_SECTIONS)
    out = _Sections(detail_level)
    summary_env = holdings_env = risk_env = totals_status = None
    with _one_read():
        _prewarm(include_cash="totals" in wanted or "realized" in wanted)
        if "totals" in wanted or "realized" in wanted:
            summary_env = _srv.get_portfolio_summary(include_realized="realized" in wanted, snapshot=False)
            totals_status = _totals_status(summary_env)
        if "exposure" in wanted:
            holdings_env = _srv.get_consolidated_holdings(detail_level=SUMMARY)
        # perps feed exposure only: the risk tool's read here; the summary's own read stays out of totals
        if "exposure" in wanted or "concentration" in wanted:
            risk_env = _srv.get_portfolio_risk(detail_level=detail_level)
    if "totals" in wanted:
        out.add("totals", "get_portfolio_summary", summary_env, lambda data: {
            key: value for key, value in data.items()
            if not _realized_key(key) and key not in (*_EXPOSURE_FROM_HOLDINGS, "underlying_exposure", "snapshot")},
            status=totals_status)
    if "exposure" in wanted:
        _exposure(out, holdings_env, risk_env)
    if "concentration" in wanted:
        out.add("concentration", "get_portfolio_risk", risk_env,
                lambda data: {key: value for key, value in data.items() if key not in _EXPOSURE_FROM_RISK})
    if "realized" in wanted:
        out.add("realized", "get_portfolio_summary", summary_env,
                lambda data: {key: value for key, value in data.items() if _realized_key(key)},
                status=_realized_status(summary_env))
    return out.envelope(sections=wanted, hooks=_TOTALS_SUMMARY)


#: Per-account NAV detail and per-account cash bridge rows (get_portfolio_history's summary
#: moves the same blocks); totals keep the per-account KRW breakdown (by_account), the
#: bridge's headline and reconciliation reasons. mark_policy is fixed text.
_TOTALS_SUMMARY = (_to_full("data.totals.account_nav", "data.totals.nav_bridge.cash_balances",
                            "data.totals.mark_policy"),)


def _totals_status(summary_env: Any) -> str | None:
    """The totals section's own status: the positions read and the NAV bridge, as get_portfolio_summary
    rates them, without the ledger and realized P&L (the realized section's). Same memoized collection."""
    if not isinstance(summary_env, dict) or not summary_env.get("ok"):
        return None
    try:
        collection = _srv._get_collector().collect_positions()
    except Exception:  # noqa: BLE001 — the summary already read it; keep its status
        return None
    status = portfolio_engine.collection_result_status([collection], len(collection.holdings) + len(collection.cash))
    bridge = ((summary_env.get("data") or {}).get("nav_bridge") or {}).get("reconciliation") or {}
    return "partial" if status == "complete" and bridge.get("difference_reasons") else status


def _realized_status(summary_env: Any) -> str | None:
    """The realized section's own status: realized_pnl_status (ledger input quality included)."""
    data = summary_env.get("data") if isinstance(summary_env, dict) and summary_env.get("ok") else None
    if not isinstance(data, dict):
        return None
    status = data.get("realized_pnl_status")
    return status if status in ("complete", "partial", "unavailable", "empty") else "partial"


def _exposure(out: _Sections, holdings_env: Any, risk_env: Any) -> None:
    """Grouping weights from the consolidated holdings; currency and look-through exposure from the risk tool."""
    if not holdings_env.get("ok"):
        out.add("exposure", "get_consolidated_holdings", holdings_env)
        return
    holdings = holdings_env["data"]
    out.add("exposure", "get_portfolio_risk", risk_env, lambda data: {
        **{key: holdings.get(key) for key in _EXPOSURE_FROM_HOLDINGS},
        **{key: data.get(key) for key in _EXPOSURE_FROM_RISK}})
    out.meta["exposure"]["sources"] = ["get_consolidated_holdings", "get_portfolio_risk"]


# ── T4 get_portfolio_returns ─────────────────────────────────────────────────


def get_portfolio_returns(
    period: Literal["MTD", "YTD", "1M", "3M", "6M", "1Y", "ALL"] = "YTD",
    scope: Literal["equity", "consolidated", "toss", "crypto", "toss_investmcp", "kis"] = "equity",
    include_series: bool = False,
    detail_level: Literal["summary", "full"] = "summary",
) -> dict[str, Any]:
    """Portfolio return for a period: simple return, TWR and XIRR side by side, P&L split and NAV risk statistics.

    Read from stored daily NAV snapshots and the ledger. include_series adds the daily NAV
    history of the same period with drawdown statistics. That is the dated value series;
    it stores nothing, and no export or snapshot is needed for it.
    Fetched broker rows are recorded in the local append-only ledger (no broker writes).
    """
    out = _Sections(detail_level)
    out.add("returns", "get_portfolio_performance",
            _srv.get_portfolio_performance(period=period, detail_level=detail_level, portfolio_scope=scope))
    if include_series:
        out.add("series", "get_portfolio_history",
                _srv.get_portfolio_history(period=period, detail_level=detail_level, portfolio_scope=scope))
    return out.envelope(period=period, scope=scope, hooks=_RETURNS_SUMMARY)


_METRIC_COVERAGE = ("data.returns.metrics.*.coverage", "data.returns.risk.metrics.*.coverage")


def _metric_coverage(env: dict[str, Any], omitted: dict[str, Any]) -> None:
    """Each metric's coverage keeps only what differs from data.returns.coverage; its
    reason_codes go when the metric's own reason_codes already list them."""
    returns = (env.get("data") or {}).get("returns")
    base = returns.get("coverage") if isinstance(returns, dict) else None
    if not isinstance(base, dict):
        return
    for pattern in _METRIC_COVERAGE:
        trimmed = 0
        for metric, key in list(_at(env, response_shape._tokens(pattern.rsplit(".", 1)[0]))):
            block = metric[key]
            coverage = block.get("coverage") if isinstance(block, dict) else None
            if not isinstance(coverage, dict):
                continue
            listed = set(block.get("reason_codes") or ())
            kept = {name: value for name, value in coverage.items()
                    if (name not in base or base[name] != value)
                    and not (name == "reason_codes" and isinstance(value, list) and set(value) <= listed)}
            if len(kept) < len(coverage):
                block["coverage"] = kept
                trimmed += 1
        if trimmed:
            omitted[f"{pattern}: keys equal to data.returns.coverage or listed in the metric's reason_codes"] = trimmed


#: Per-metric coverage repeats data.returns.coverage; the ledgerless estimate keeps its
#: headline (period, effective/tracked period, label, accuracy, reason codes, scopes, day
#: counts by reason) and moves its interval rows, unresolved day spans and fixed texts.
#: Write routing (a 2.15.1 regression: the MTD default was stored to a file): each metric's
#: fixed method_version / required_inputs and the per-symbol realized rows' proceeds, cost,
#: fees, taxes and names move too; a row keeps its symbol, KRW P&L, %, basis flag and status.
_REALIZED_ROW = ("symbol", "currency", "realized_pnl_krw", "realized_pnl_pct", "cost_basis_complete",
                 "status", "reason_codes")
_RETURNS_SUMMARY = (
    _metric_coverage,
    _to_full("data.returns.ledgerless_returns.intervals",
             "data.returns.ledgerless_returns.coverage.unresolved_days",
             "data.returns.ledgerless_returns.explanation",
             "data.returns.ledgerless_returns.aggregation"),
    _to_full(*(f"data.returns.{group}.*.{key}" for group in ("metrics", "risk.metrics")
               for key in ("method_version", "required_inputs"))),
    _keep_keys("data.returns.realized_by_symbol", _REALIZED_ROW),
)


# ── paging ───────────────────────────────────────────────────────────────────

MAX_LIMIT = 500


def _encode_cursor(payload: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(response_shape.compact_json(payload).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str, query: dict[str, Any]) -> dict[str, Any] | None:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) and payload.get("q") == query else None


def _row_key(row: Any, fields: tuple[str, ...]) -> list[Any]:
    """Newest-first sort key; the row's own hash breaks ties, so a keyset never skips a row."""
    values = [row.get(field) if row.get(field) is not None else "" for field in fields]
    return [*values, hashlib.sha256(response_shape.compact_json(row).encode()).hexdigest()[:16]]


def _keyset_page(rows: list[Any], fields: tuple[str, ...], limit: int, after: list[Any] | None):
    keyed = sorted(((_row_key(row, fields), row) for row in rows), key=lambda pair: _sortable(pair[0]), reverse=True)
    if after is not None:
        keyed = [pair for pair in keyed if _sortable(pair[0]) < _sortable(after)]
    page = keyed[:limit]
    return [row for _, row in page], (page[-1][0] if len(keyed) > limit else None)


def _sortable(key: list[Any]) -> tuple[Any, ...]:
    # Mixed int/str columns (a null time) compare by type first.
    return tuple((0, value) if isinstance(value, (int, float)) else (1, str(value)) for value in key)


def _page_header(limit: int, rows: list[Any], total: int, next_key: Any, query: dict[str, Any], **extra: Any):
    return {"order": "newest_first", "limit": limit, "returned": len(rows), "total_count": total,
            "has_more": next_key is not None,
            "next_cursor": _encode_cursor({"q": query, "after": next_key, **extra}) if next_key is not None else None}


# ── T5 get_transactions ──────────────────────────────────────────────────────

#: Row columns at detail_level="summary" (the ledger's normalized fields an answer needs). Fee and tax
#: are the reported amounts (null unless reported or verified zero, never 0.0 for unknown) with their
#: status; "price_validation.status" is that nested field.
TX_COLUMNS = ("trade_date", "settle_date", "account", "tx_type", "symbol", "name", "market", "currency",
              "quantity", "price", "gross_amount", "fee_reported_amount", "tax_reported_amount", "net_amount",
              "fee_status", "tax_status", "price_validation.status", "status")
_TX_KEY = ("trade_date", "account", "tx_id")


def get_transactions(account: str = "all", start: str = "", end: str = "",
                     detail_level: Literal["summary", "full", "raw"] = "summary",
                     symbol: str = "", limit: int = 100, cursor: str = "") -> dict[str, Any]:
    """Your trades across every Toss and KIS account, newest first, one page at a time.

    start/end: ISO YYYY-MM-DD (default: the last 90 days). account: "all", "toss", "kis"
    or one label. The page header carries the totals and counts for the whole window and
    next_cursor for the following page (limit 1-500, default 100). detail_level "summary"
    returns compact rows (columns + rows), "full" every normalized field per row, "raw"
    the brokers' own rows, one source at a time.
    Fetched broker rows are recorded in the local append-only ledger (no broker writes).
    """
    if not 1 <= limit <= MAX_LIMIT:
        return _invalid("INVALID_LIMIT", f"limit must be 1-{MAX_LIMIT}")
    query = {"tool": "get_transactions", "account": account, "start": start, "end": end,
             "detail_level": detail_level, "symbol": symbol}
    state = _decode_cursor(cursor, query) if cursor else {}
    if state is None:
        return _invalid("INVALID_CURSOR", "cursor does not belong to these arguments; repeat them unchanged")
    if detail_level == RAW:
        return _raw_transactions(account, start, end, limit, query, state)
    out = _Sections(detail_level)
    data = out.add("transactions", "get_all_transactions",
                   _srv.get_all_transactions(start, end, account, symbol, detail_level=FULL))
    if data is None:
        return out.envelope(account=account)
    if state and state.get("period") != data.get("period"):
        return _invalid("CURSOR_WINDOW_CHANGED", "the default window moved since the first page; pass start and end")
    rows = data.pop("transactions", None) or []
    page, next_key = _keyset_page(rows, _TX_KEY, limit, state.get("after"))
    data["page"] = _page_header(limit, page, len(rows), next_key, query, period=data.get("period"))
    if detail_level == SUMMARY:
        data["columns"] = list(TX_COLUMNS)
        data["rows"] = [[_column(row, column) for column in TX_COLUMNS] for row in page]
    else:
        data["rows"] = page
    # summary keeps the reconciliation status and counts; per-symbol rows and breaks are in full
    return out.envelope(account=account, drop=("data.transactions.reconciliation.positions",
                                               "data.transactions.reconciliation.reconciliation_breaks"))


def _column(row: dict[str, Any], column: str) -> Any:
    head, _, rest = column.partition(".")
    value = row.get(head)
    return (value.get(rest) if isinstance(value, dict) else None) if rest else value


def _iso_to_compact(value: str) -> str:
    return value.replace("-", "")


def _raw_sources(account: str) -> list[list[str]]:
    toss, kis = _broker_scope(account)
    sources: list[list[str]] = []
    if toss is not None:
        labels = _srv._get_toss_registry().account_labels() if toss == "all" else [toss or "primary"]
        sources += [["toss", label, ""] for label in labels]
    if kis is not None:
        labels = _srv._get_kis_registry().account_labels() if kis == "all" else [kis or _srv._get_kis_registry().default_label()]
        sources += [["kis", label, market] for label in labels for market in ("domestic", "overseas")]
    return sources


def _raw_transactions(account: str, start: str, end: str, limit: int, query: dict[str, Any],
                      state: dict[str, Any]) -> dict[str, Any]:
    """Broker rows exactly as the raw history tools return them, paged one source at a time."""
    try:
        sources = state.get("sources") or _raw_sources(account)
        window = state.get("window") or list(_srv._resolve_dates(start, end, default_days=90))
    except Exception as exc:  # noqa: BLE001
        return {**_srv._format_error(exc), "result_status": "unavailable"}
    index, offset = state.get("source", 0), state.get("offset", 0)
    out = _Sections(RAW)
    if index >= len(sources):
        return _invalid("INVALID_CURSOR", "no source left")
    broker, label, market = sources[index]
    if broker == "toss":
        # Toss's history has no date filter: newest rows first, as many as this page needs.
        fetch = min(2000, offset + limit + 1)
        data = out.add("transactions", "get_toss_trade_history",
                       _srv.get_toss_trade_history(limit=fetch, account=label))
        rows = data if isinstance(data, list) else []
        more_here = len(rows) > offset + limit
    else:
        env = _srv.get_kis_trade_history(_iso_to_compact(window[0]), _iso_to_compact(window[1]), account=label)
        data = out.add("transactions", "get_kis_trade_history", env)
        block = (data or {}).get(market) or {}
        rows = block.get("trades") or []
        out.meta["transactions"]["market"] = market
        if isinstance(data, dict):
            out.data["transactions"] = {key: value for key, value in block.items() if key != "trades"}
        more_here = len(rows) > offset + limit
    if data is None:
        return out.envelope(account=account)
    page = rows[offset:offset + limit]
    if more_here:
        following = {"source": index, "offset": offset + limit}
    elif index + 1 < len(sources):
        following = {"source": index + 1, "offset": 0}
    else:
        following = None
    body = out.data["transactions"] if isinstance(out.data["transactions"], dict) else {}
    out.data["transactions"] = {**body, "rows": page, "page": {
        "order": "broker", "source": f"{broker}:{label}{':' + market if market else ''}",
        "source_index": index, "source_count": len(sources), "offset": offset, "limit": limit,
        "returned": len(page), "window": {"start": window[0], "end": window[1],
                                          "applies": broker == "kis"},
        "has_more": following is not None,
        "next_cursor": _encode_cursor({"q": query, "sources": sources, "window": window, **following})
        if following else None}}
    return out.envelope(account=account)


# ── T6 get_trading_pnl ───────────────────────────────────────────────────────


def get_trading_pnl(
    view: Literal["by_symbol", "round_trips"] = "by_symbol",
    period: Literal["MTD", "YTD", "1M", "3M", "6M", "1Y", "ALL"] = "YTD",
    account: str = "all", symbol: str = "", start: str = "", end: str = "",
    detail_level: Literal["summary", "full"] = "summary",
) -> dict[str, Any]:
    """Realized trading P&L under per-account moving-average cost basis.

    view "by_symbol": realized P&L, fees and taxes per symbol for the period.
    view "round_trips": closed buy-to-sell cycles, win rate and open-position P&L.
    start/end (ISO YYYY-MM-DD) override period. detail_level "summary" lists 20 by_symbol rows,
    incomplete data first, then the largest realized P&L (symbols_page has the total), or the
    top 10 round_trips symbols by |realized P&L| with reason groups (symbol_count,
    omitted_symbols); pass symbol or detail_level "full" for every symbol.
    Fetched broker rows are recorded in the local append-only ledger (no broker writes).
    """
    out = _Sections(detail_level)
    if view == "by_symbol":
        data = out.add("by_symbol", "get_realized_pnl", _srv.get_realized_pnl(
            period=period, account=account, symbol=symbol, start_date=start, end_date=end))
    elif view == "round_trips":
        data = out.add("round_trips", "get_trade_review", _srv.get_trade_review(
            period=period, start_date=start, end_date=end, account=account, symbol=symbol, detail_level=detail_level))
    else:
        return _invalid("INVALID_ARGUMENT", "view must be by_symbol or round_trips")
    if detail_level != SUMMARY or not isinstance(data, dict):
        return out.envelope(view=view)
    # round_trips: get_trade_review's own summary body is already the top symbols; not re-paged.
    hidden = _top_symbols(data, view, PNL_SUMMARY_ROWS[view]) if view in PNL_SUMMARY_ROWS else 0
    env = out.envelope(view=view, drop=_PNL_SUMMARY_DROP[view])
    if hidden and isinstance(env.get("meta"), dict):
        env["meta"].setdefault("detail", {}).setdefault("omitted", {})[f"data.{view}.symbols[]"] = hidden
    return env


#: get_trading_pnl at summary: rows per view, and the blocks that move to "full" (the old
#: tools' own summaries are 240 KB / 420 KB live for a year of trades).
PNL_SUMMARY_ROWS = {"by_symbol": 20}
_PNL_SUMMARY_DROP = {
    "by_symbol": ("data.by_symbol.open_positions_from_ledger", "data.by_symbol.open_positions_by_account",
                  "data.by_symbol.reconciliation.positions", "data.by_symbol.reconciliation.reconciliation_breaks"),
    "round_trips": ("data.round_trips.coverage.source_metadata",
                    "data.round_trips.reconciliation.positions",
                    "data.round_trips.reconciliation.reconciliation_breaks"),
}


def _top_symbols(data: dict[str, Any], view: str, limit: int) -> int:
    """Keep `limit` symbol rows: incomplete ones first, then by |realized P&L in KRW|. Returns rows hidden."""
    rows = data.get("symbols")
    if not isinstance(rows, list) or len(rows) <= limit:
        return 0

    def rank(row: dict[str, Any]) -> tuple[Any, ...]:
        realized = row.get("realized_pnl_krw") if view == "by_symbol" else (row.get("realized") or {}).get(
            "realized_pnl_krw")
        complete = (row.get("status") or row.get("result_status")) == "complete"
        size = abs(realized) if isinstance(realized, (int, float)) else -1.0
        return complete, -size, str(row.get("symbol")), str(row.get("currency"))

    data["symbols"] = sorted(rows, key=rank)[:limit]
    data["symbols_page"] = {"shown": limit, "total": len(rows),
                            "order": "incomplete_first_then_abs_realized_pnl_krw"}
    return len(rows) - limit


# ── T7 get_quote ─────────────────────────────────────────────────────────────

QUOTE_INCLUDE = ("orderbook", "trades", "limits", "warnings", "info")
MAX_SYMBOLS = 10


def get_quote_v2(
    symbols: list[str],
    provider: Literal["auto", "toss", "kis", "both"] = "auto",
    include: list[Literal["orderbook", "trades", "limits", "warnings", "info"]] = [],  # noqa: B006
    market: Literal["auto", "us", "kr"] = "auto",
    exchange: Literal["NASDAQ", "NYSE", "AMEX", "TKSE", "TSE"] = "NASDAQ",
    detail_level: Literal["summary", "full"] = "summary",
) -> dict[str, Any]:
    """Current stock quotes for up to 10 symbols (e.g. "AAPL", "005930").

    provider "auto" reads Toss and falls back to KIS; "toss" or "kis" reads one; "both"
    compares the two with session timing. include adds per symbol: orderbook (bid/ask
    depth), trades (public market tape), limits (daily price limits), warnings
    (delisting/VI/overheated) and info (name, market, ISIN). KIS covers Japanese stocks
    (exchange TKSE).
    """
    if not symbols or len(symbols) > MAX_SYMBOLS:
        return _invalid("INVALID_ARGUMENT", f"symbols takes 1-{MAX_SYMBOLS} symbols")
    extras = _choices(include, QUOTE_INCLUDE, "include")
    if isinstance(extras, dict):
        return extras
    rows, statuses, errors = [], [], []
    for symbol in dict.fromkeys(symbol.strip() for symbol in symbols if symbol.strip()):
        out = _Sections(detail_level)
        used = _quote_part(out, symbol, provider, market, exchange)
        for name in extras:
            out.add(name, *_QUOTE_EXTRAS[name](symbol, market))
        statuses.extend(out.statuses)
        errors.extend(out.errors)
        rows.append({"symbol": symbol.upper(), "provider": used, **out.data, "sources": out.meta})
    combined = _Sections(detail_level)
    combined.statuses, combined.errors = statuses, errors
    combined.data = {"quotes": rows}
    env = combined.envelope(provider=provider)
    if isinstance(env.get("meta"), dict):
        env["meta"].pop("sections", None)
        env["meta"].pop("providers", None)  # per symbol: quotes[].provider and quotes[].sources
    return env


_QUOTE_EXTRAS = {
    "orderbook": lambda symbol, market: ("get_orderbook", _srv.get_orderbook(symbol)),
    "trades": lambda symbol, market: ("get_market_trades", _srv.get_market_trades(symbol)),
    "limits": lambda symbol, market: ("get_price_limits", _srv.get_price_limits(symbol)),
    "warnings": lambda symbol, market: ("get_stock_warnings", _srv.get_stock_warnings(symbol)),
    "info": lambda symbol, market: ("get_stock_info", _srv.get_stock_info(symbol, market)),
}


def _kis_quote(symbol: str, market: str, exchange: str) -> tuple[str, Any]:
    try:
        resolved = _srv._resolve_market(symbol, market)
    except _srv.MarketResolutionError:
        return "get_kis_domestic_quote", _srv._market_required()
    if resolved == "kr":
        return "get_kis_domestic_quote", _srv.get_kis_domestic_quote(symbol)
    return "get_kis_overseas_quote", _srv.get_kis_overseas_quote(symbol, exchange=exchange)


def _quote_part(out: _Sections, symbol: str, provider: str, market: str, exchange: str) -> str:
    if provider == "both":
        out.add("quote", "compare_quotes", _srv.compare_quotes(symbol, exchange=exchange, market=market))
        return "both"
    if provider in ("auto", "toss"):
        env = _srv.get_quote(symbol)
        if env.get("ok") or provider == "toss":
            out.add("quote", "get_quote", env)
            return "toss"
        old, env = _kis_quote(symbol, market, exchange)
        out.add("quote", old, env)
        out.meta["quote"]["fallback_from"] = "toss"
        return "kis"
    old, env = _kis_quote(symbol, market, exchange)
    out.add("quote", old, env)
    return "kis"


# ── T9 get_market_info ───────────────────────────────────────────────────────

MARKET_INCLUDE = ("hours", "fx", "risk_free_rate")


def get_market_info(
    include: list[Literal["hours", "fx", "risk_free_rate"]] = list(MARKET_INCLUDE),
    market: Literal["US", "KR"] = "US", base: str = "USD", quote: str = "KRW",
) -> dict[str, Any]:
    """Market sessions and holidays (KR or US; Toss), a currency exchange rate (Toss Securities, with
    its validFrom/validUntil), and the 10Y US Treasury yield (yfinance ^TNX). Each section's meta
    names its provider and as_of."""
    wanted = _choices(include, MARKET_INCLUDE, "include")
    if isinstance(wanted, dict):
        return wanted
    out = _Sections(FULL)
    for name in [name for name in MARKET_INCLUDE if name in wanted] or MARKET_INCLUDE:
        if name == "hours":
            out.add("hours", "get_market_hours", _srv.get_market_hours(market))
        elif name == "fx":
            out.add("fx", "get_exchange_rate", _srv.get_exchange_rate(base=base, quote=quote))
        else:
            out.add("risk_free_rate", "get_risk_free_rate", _srv.get_risk_free_rate())
    env = out.envelope()
    env["meta"].pop("detail", None)
    return env


# ── T10 crypto ───────────────────────────────────────────────────────────────

CRYPTO_INCLUDE = ("funding_history", "open_interest", "candles")


def get_crypto_market(
    symbol: str,
    include: list[Literal["funding_history", "open_interest", "candles"]] = [],  # noqa: B006
    interval: Literal["5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"] = "1h",
    limit: int = 30,
) -> dict[str, Any]:
    """Public Binance USD-M futures market data for one symbol (e.g. "BTCUSDT"); no API key.

    Always returns the snapshot: last price, mark/index price, latest funding rate and
    open interest. include adds the public funding rate history, open interest history and
    OHLCV candles (interval; limit rows each, 1-500).
    """
    extras = _choices(include, CRYPTO_INCLUDE, "include")
    if isinstance(extras, dict):
        return extras
    if not 1 <= limit <= MAX_LIMIT:
        return _invalid("INVALID_LIMIT", f"limit must be 1-{MAX_LIMIT}")
    out = _Sections(FULL)
    out.add("snapshot", "get_crypto_futures_snapshot", _srv.get_crypto_futures_snapshot(symbol))
    for name in [name for name in CRYPTO_INCLUDE if name in extras]:
        if name == "funding_history":
            out.add(name, "get_binance_funding_rate_history", _srv.get_binance_funding_rate_history(symbol, limit))
        elif name == "open_interest":
            out.add(name, "get_binance_open_interest",
                    _srv.get_binance_open_interest(symbol, history=True, period=interval, limit=limit))
        else:
            out.add(name, "get_binance_futures_candles",
                    _srv.get_binance_futures_candles(symbol, interval=interval, count=limit))
    env = out.envelope(provider="binance")
    env["meta"].pop("detail", None)
    return env


def get_crypto_positions(venue: Literal["all", "binance", "hyperliquid"] = "all") -> dict[str, Any]:
    """Your open crypto futures positions and native account balances (Binance USD-M, Hyperliquid).

    Kept out of portfolio NAV and holdings. Unknown leverage, margin mode and mark prices
    stay null; inspect each venue's source_status and coverage.
    """
    return _srv.get_crypto_futures_positions(venue)


_HISTORY_FIELD = {"fills": "fills", "funding": "funding_payments"}


def get_crypto_account_history(
    kind: Literal["fills", "funding"],
    venue: Literal["all", "binance", "hyperliquid"] = "all",
    start: str = "", end: str = "", symbol: str = "", limit: int = 50, cursor: str = "",
) -> dict[str, Any]:
    """Your own crypto futures fills (with fees) or funding paid/received, newest first, paged.

    start/end: inclusive UTC YYYY-MM-DD (default: the last 7 days). Values stay in their
    native asset. The page header carries per-venue coverage and next_cursor (limit 1-500,
    default 50).
    Fetched broker rows are recorded in the local append-only ledger (no broker writes).
    """
    if kind not in _HISTORY_FIELD:
        return _invalid("INVALID_ARGUMENT", "kind must be fills or funding")
    if not 1 <= limit <= MAX_LIMIT:
        return _invalid("INVALID_LIMIT", f"limit must be 1-{MAX_LIMIT}")
    query = {"tool": "get_crypto_account_history", "kind": kind, "venue": venue, "start": start, "end": end,
             "symbol": symbol}
    state = _decode_cursor(cursor, query) if cursor else {}
    if state is None:
        return _invalid("INVALID_CURSOR", "cursor does not belong to these arguments; repeat them unchanged")
    if state.get("window"):
        start, end = state["window"]
    else:
        end = end or datetime.now(timezone.utc).date().isoformat()
        if not start and _iso_date(end):
            start = (datetime.fromisoformat(end).date() - timedelta(days=6)).isoformat()
    old = "get_crypto_futures_fills" if kind == "fills" else "get_crypto_futures_funding_payments"
    env = getattr(_srv, old)(venue, start, end, symbol)
    out = _Sections(FULL)
    data = out.add("history", old, env)
    if data is None:
        env = out.envelope(provider="crypto_futures_accounts")
        env["meta"].pop("detail", None)
        return env
    field = _HISTORY_FIELD[kind]
    rows, venues = [], {}
    for name, block in (data.get("venues") or {}).items():
        venue_rows = block.get(field) or []
        rows += [{"venue": name, **row} for row in venue_rows]
        venues[name] = {**{key: value for key, value in block.items() if key != field}, "row_count": len(venue_rows)}
    page, next_key = _keyset_page(rows, ("time", "venue", "fill_id" if kind == "fills" else "payment_id"),
                                  limit, state.get("after"))
    out.data["history"] = {**{key: value for key, value in data.items() if key != "venues"},
                           "window": {"start": start, "end": end}, "venues": venues, "rows": page,
                           "page": _page_header(limit, page, len(rows), next_key, query, window=[start, end])}
    env = out.envelope(provider="crypto_futures_accounts")
    env["meta"].pop("detail", None)
    return env


def _iso_date(value: str) -> bool:
    try:
        datetime.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False


# ── T0 import_statement ──────────────────────────────────────────────────────


def import_statement(file: str = "", preview_id: str = "", confirm: bool = False,
                     account: str = "auto") -> dict[str, Any]:
    """Import Toss stock BUY/SELL rows from a private inbox PDF statement into the local ledger.

    Two steps. 1) file="<name>.pdf": parses it and returns the preview and the import plan
    (nothing is written); keep preview_id. 2) preview_id plus confirm=true stores the
    planned rows locally. A blocked plan (parse issues, conflicts, unmatched rows inside
    API coverage) cannot be confirmed. Never places orders; previews expire after 15 minutes.
    """
    out = _Sections(FULL)
    if confirm:
        if not preview_id:
            return _invalid("PREVIEW_ID_REQUIRED", "confirm=true needs the preview_id of a fresh preview")
        out.add("import", "apply_statement_import", _srv.apply_statement_import(preview_id, True))
    elif preview_id:
        out.add("plan", "apply_statement_import", _srv.apply_statement_import(preview_id, False))
    elif file:
        preview = out.add("preview", "preview_statement_import",
                          _srv.preview_statement_import("toss", file=file, account=account))
        if isinstance(preview, dict) and preview.get("preview_id"):
            out.add("plan", "apply_statement_import", _srv.apply_statement_import(preview["preview_id"], False))
    else:
        return _invalid("FILE_REQUIRED", "pass file (step 1) or preview_id (step 2)")
    env = out.envelope(provider="statement:toss")
    env["meta"].pop("detail", None)
    return env


# ── T0 get_portfolio_summary without the snapshot argument ───────────────────


def get_portfolio_summary(include_realized: bool = True) -> dict[str, Any]:
    """Whole-portfolio totals across every broker account.

    Never writes a NAV snapshot; the daily history is written by the scheduled 18:10
    capture and snapshot_portfolio_nav. With include_realized, fetched transaction rows
    are recorded in the append-only ledger as usual.

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

    Returns:
        Structured JSON envelope with the portfolio summary.
    """
    return _srv.get_portfolio_summary(include_realized=include_realized, snapshot=False)


# ── registration ─────────────────────────────────────────────────────────────

#: detail_level per chat name: (levels, meaning). A response-shape policy entry keys on the
#: registered name; the merged tools shape their sections here, so none is needed today.
DETAIL_LEVELS: dict[str, tuple[tuple[str, ...], str]] = {
    "get_status": ((SUMMARY, FULL), "each section shaped by its source tool's policy; full = complete objects"),
    "get_holdings": ((SUMMARY, FULL, RAW), "summary = consolidated-holdings policy (top positions), "
                     "look-through exposure omitted; full = every position with accounts; "
                     "raw = broker balance payloads, unshaped"),
    "get_portfolio_summary": ((SUMMARY, FULL), "sections shaped by the summary/risk/holdings policies; "
                              "default sections totals+realized"),
    "get_portfolio_returns": ((SUMMARY, FULL), "performance/history policies; series only with include_series"),
    "get_transactions": ((SUMMARY, FULL, RAW), "one page; summary = columns+rows, full = normalized row dicts, "
                         "raw = broker rows of one source per page"),
    "get_trading_pnl": ((SUMMARY, FULL), "by_symbol: realized-pnl policy, then PNL_SUMMARY_ROWS symbols (incomplete "
                        "first, then |realized KRW|); round_trips: get_trade_review's summary body (top 10 + reason "
                        "groups); _PNL_SUMMARY_DROP blocks moved to full"),
    "get_quote": ((SUMMARY, FULL), "summary = quote policies (compare_quotes drops the KIS raw payload)"),
    "get_market_info": ((FULL,), "fixed: small public sections"),
    "get_crypto_market": ((FULL,), "fixed: snapshot plus requested series, bounded by limit"),
    "get_crypto_positions": ((FULL,), "fixed: the positions tool's answer"),
    "get_crypto_account_history": ((FULL,), "one page of venue rows, newest first (limit default 50)"),
    "import_statement": ((FULL,), "fixed: preview, plan or import result"),
}


def register(srv: Any) -> None:
    """Add the merged tools to srv.mcp; old tools stay registered under their names."""
    global _srv, _app
    _srv, _app = srv, srv.mcp
    _install_memo(srv)
    add = srv.mcp.add_tool
    add(get_portfolio_summary, name="get_portfolio_summary", annotations=READ)
    add(import_statement, name="import_statement", annotations=IMPORT)
    add(get_status, name="get_status", annotations=READ)
    add(get_holdings, name="get_holdings", annotations=READ)
    add(get_portfolio_summary_v2, name="get_portfolio_summary_v2", annotations=READ)
    add(get_portfolio_returns, name="get_portfolio_returns", annotations=READ)
    add(get_transactions, name="get_transactions", annotations=READ)
    add(get_trading_pnl, name="get_trading_pnl", annotations=READ)
    add(get_quote_v2, name="get_quote_v2", annotations=READ)
    add(get_market_info, name="get_market_info", annotations=READ)
    add(get_crypto_market, name="get_crypto_market", annotations=READ)
    add(get_crypto_positions, name="get_crypto_positions", annotations=READ)
    add(get_crypto_account_history, name="get_crypto_account_history", annotations=READ)
