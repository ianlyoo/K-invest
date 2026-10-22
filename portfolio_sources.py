#!/usr/bin/env python3
"""Broker fan-out: collect normalized holdings, cash, and transactions.

This is the only layer that talks to provider clients. Clients are injected as
callables so the collector is testable with fakes, and every source is isolated:
one broker failing degrades the result and is reported in `health` rather than
failing the whole call.

Account labels are `provider:scope` — `toss:primary`, `kis:domestic`,
`kis:overseas` — and are what the `account` filter on the MCP tools matches. A
KIS account other than `primary` carries its label in the middle, so a 연금저축
account labelled `pension` collects as `kis:pension:domestic`.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import random
import re
import secrets
import time
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from threading import Event, Lock, RLock, Thread
from typing import Any, Callable, Iterable

from journal_metadata import identity, legacy_kis_account_key, source_metadata
from kinvest_common import YFINANCE_LOCK, synchronized
from portfolio_model import (
    CashBalance,
    FxRates,
    Holding,
    Transaction,
    kis_domestic_cash,
    kis_domestic_holdings,
    kis_domestic_transactions,
    kis_overseas_cash,
    kis_overseas_ccnl_transactions,
    kis_overseas_holdings,
    kis_overseas_transactions,
    to_float,
    toss_cash,
    toss_holdings,
    toss_transactions,
)
from toss_nav_validation import validate_fx_input, validate_toss_response

logger = logging.getLogger("k-invest.portfolio_sources")


def _masked_kis_diagnostic(exc: Exception) -> dict[str, str | None]:
    """Bounded broker hints for health only, never raw bodies or exception text.

    KIS codes have a restricted service-code shape. Preserve Korean diagnostic
    text and simple punctuation; redact all other runs (including digits,
    Latin identifiers, tokens, and Unicode lookalikes) before truncating.
    """
    body = getattr(exc, "body", None)
    body = body if isinstance(body, dict) else {}
    code, message = body.get("msg_cd"), body.get("msg1")
    code = code if isinstance(code, str) and re.fullmatch(r"[A-Z]{3,4}[0-9]{4,5}", code) else None
    if isinstance(message, str):
        message = re.sub(r"[^가-힣\s.,:()\[\]/-]+", "[masked]", message)
        message = " ".join(message.split())[:160] or None
    else:
        message = None
    return {"msg_cd": code, "msg1": message}

#: yfinance FX pairs used to convert native currency into KRW. Quoted per one
#: unit of the base currency, avoiding the per-100-yen convention that Korean
#: broker 고시환율 fields use.
_FX_PAIRS = {"USD": "USDKRW=X", "JPY": "JPYKRW=X", "HKD": "HKDKRW=X", "CNY": "CNYKRW=X"}

_CONNECTION_LOCKS: dict[str, RLock] = {}
_CONNECTION_REGISTRY_LOCK = Lock()
NAV_EVIDENCE_BUDGET_SECONDS = 20.0
_NAV_READ_DEADLINE = ContextVar('nav_read_deadline', default=None)


#: KIS answered a ChatGPT burst of parallel tool calls with EGW00215 (its
#: per-second limit; live 2.15.0, 10/22). Tool calls that ask for the same positions read
#: while one is in flight, or within SHARED_POSITIONS_SECONDS after it finished, share that
#: read instead of fanning out over every broker account again. Only inside
#: shared_position_reads() (MCP tool calls); scripts and the NAV snapshot read their own.
SHARED_POSITIONS_SECONDS = 3.0
_SHARED_POSITIONS = ContextVar('shared_position_reads', default=False)
#: One KIS balance fan-out at a time per account ledger (CANO), whatever app key reads it.
_KIS_LEDGER_GATES: dict[str, Lock] = {}


@contextmanager
def shared_position_reads(enabled=True):
    token = _SHARED_POSITIONS.set(enabled)
    try:
        yield
    finally:
        _SHARED_POSITIONS.reset(token)


@contextmanager
def kis_ledger_gate(cano):
    """Serialize KIS balance fan-outs on one account ledger across concurrent tool calls."""
    key = hashlib.sha256(f"kis-ledger:{cano}".encode()).hexdigest()
    with _CONNECTION_REGISTRY_LOCK:
        gate = _KIS_LEDGER_GATES.setdefault(key, Lock())
    remaining = nav_read_remaining()
    if not (gate.acquire() if remaining is None else gate.acquire(timeout=remaining)):
        raise TimeoutError('NAV evidence budget exceeded')
    try:
        yield
    finally:
        gate.release()


class _SharedRead:
    def __init__(self):
        self.done = Event()
        self.value = None
        self.finished = None


def nav_read_remaining():
    """Optional deadline shared by NAV reads, lock waits and retries only."""
    deadline = _NAV_READ_DEADLINE.get()
    remaining = None if deadline is None else deadline - time.monotonic()
    if remaining is not None and remaining <= 0:
        raise TimeoutError('NAV evidence budget exceeded')
    return remaining


def nav_request_options():
    remaining = nav_read_remaining()
    return {} if remaining is None else {'timeout': min(15.0, remaining)}


@contextmanager
def connection_guard(provider, identifier):
    """Serialize authenticated reads (including retries) across client instances.

    Keys are private hashes; never put authentication identifiers in output.
    Reentrancy permits Toss's account lookup within an authenticated GET.
    """
    key = hashlib.sha256(f"{provider}:{identifier}".encode()).hexdigest()
    with _CONNECTION_REGISTRY_LOCK:
        lock = _CONNECTION_LOCKS.setdefault(key, RLock())
    remaining = nav_read_remaining()
    acquired = lock.acquire() if remaining is None else lock.acquire(timeout=remaining)
    if not acquired:
        raise TimeoutError('NAV evidence budget exceeded')
    try:
        nav_read_remaining()
        yield
    finally:
        lock.release()


def retry_delay(attempt, requested, remaining, base=.5):
    """Two retries at most at callers; total scheduled wait never exceeds 60s."""
    delay = max(base * 2**attempt + random.uniform(0, .25), requested or 0)
    return delay if delay <= remaining else None


#: A transaction window ending this many days before today (KST) is settled: one
#: complete broker read of it is reused in-process instead of being read again.
SETTLED_WINDOW_DAYS = 10
WINDOW_CACHE_SECONDS = 6 * 3600
WINDOW_CACHE_MAX = 2048
_KST = timezone(timedelta(hours=9))


def full_backfill_requested() -> bool:
    """Operator switch: every request reads every window from the broker again."""
    return os.environ.get("KINVEST_LEDGER_FULL_BACKFILL", "").strip().lower() in {"1", "true", "yes"}


def history_windows(start, end):
    """Inclusive, nonoverlapping windows of at most 90 days."""
    current, last = date.fromisoformat(start), date.fromisoformat(end)
    if current > last:
        raise ValueError("start must be on or before end")
    while current <= last:
        stop = min(current + timedelta(days=89), last)
        yield current.isoformat(), stop.isoformat()
        current = stop + timedelta(days=1)


@dataclass
class SourceStatus:
    """Rolling health for one account source, tracked for the process lifetime."""

    account: str
    provider: str
    status: str = "unknown"
    last_success_at: str | None = None
    last_error_at: str | None = None
    last_error: dict[str, Any] | None = None
    consecutive_failures: int = 0
    latency_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "provider": self.provider,
            "status": self.status,
            "last_success_at": self.last_success_at,
            "last_error_at": self.last_error_at,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "latency_ms": self.latency_ms,
        }


@dataclass
class Providers:
    """Injected client factories.

    toss_labels: configured Toss account labels (raises if Toss isn't configured).
    toss_client: label -> TossClient.
    kis_accounts: () -> configured KIS accounts, each with `.label` and
        `.supports_overseas` (raises if KIS isn't configured).
    kis_client: label -> KISClient.
    error_formatter: Exception -> {"code", "message", ...} for health reporting.
    fx_quote: currency -> KRW rate, provider mark dict, or None. A Toss mark
        uses rate/validFrom; other marks use rate/priced_at. Defaults to Toss
        get_exchange_rate with a labelled yfinance fallback (FX_POLICY).
    """

    toss_labels: Callable[[], list[str]]
    toss_client: Callable[[str], Any]
    kis_accounts: Callable[[], list[Any]]
    kis_client: Callable[[str], Any]
    error_formatter: Callable[[Exception], dict[str, Any]]
    fx_quote: Callable[[str], float | dict[str, Any] | None] | None = None
    health_observer: Callable[..., None] | None = None
    statement_store: Callable[[], Any] | None = None
    ledger_observer: Callable[[Any], None] | None = None
    #: (sources, start) -> {source: {"date": ISO date, ...}}: windows ending before
    #: the date are known empty from stored complete reads (portfolio_ledger.history_anchors).
    ledger_anchor: Callable[[list[str], str], dict[str, dict[str, Any]]] | None = None


@dataclass
class Collection:
    """Result of one fan-out, with whatever succeeded plus per-source health."""

    holdings: list[Holding] = field(default_factory=list)
    cash: list[CashBalance] = field(default_factory=list)
    transactions: list[Transaction] = field(default_factory=list)
    health: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Sources whose upstream paging hit its cap, so their rows are incomplete.
    truncated_sources: list[str] = field(default_factory=list)
    fetched_at: str = ""
    source_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)
    requested_window: dict[str, str] = field(default_factory=dict)
    observed_window: dict[str, str | None] = field(default_factory=dict)
    fetch_complete: bool = False
    historical_coverage: str = "unknown"
    failed_chunks: list[dict[str, Any]] = field(default_factory=list)
    resume_cursor: str | None = None
    reconciliation: dict[str, Any] = field(default_factory=dict)
    ledger_persistence: dict[str, Any] | None = None
    nav_evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    nav_completed_at: str | None = None
    #: NAV-only input findings, retained before legacy normalization drops rows.
    nav_source_quality_reasons: dict[str, list[str]] = field(default_factory=dict)
    #: Markets certified by a complete, consistent Toss inventory response.
    nav_holding_markets: dict[str, dict[str, str]] = field(default_factory=dict)
    toss_fill_windows: dict[str, list[dict]] = field(default_factory=dict)

    @property
    def ok_sources(self) -> list[str]:
        return [name for name, entry in self.health.items() if entry.get("status") == "ok"]

    @property
    def failed_sources(self) -> list[str]:
        return [
            name
            for name, entry in self.health.items()
            if entry.get("status") not in ("ok", "empty", "truncated")
        ]


class PortfolioCollector:
    """Fans out over every configured broker account and normalizes the results."""

    def __init__(self, providers: Providers, *, strict_toss_fills=False):
        self._providers = providers
        self._strict_toss_fills = strict_toss_fills
        self._status: dict[str, SourceStatus] = {}
        self._status_lock = RLock()
        # Recovery is process-local, bounded, and expires after 30 minutes. It is
        # neither durable history preservation nor proof of broker retention.
        self._recoveries: dict[str, dict[str, Any]] = {}
        self._recovery_lock = RLock()
        self._cooldowns: dict[str, tuple[float, dict[str, Any]]] = {}
        # Settled windows only, process-local and bounded; never persisted.
        self._window_cache: dict[tuple, tuple[float, Collection]] = {}
        self._shared_positions: dict[Any, _SharedRead] = {}
        self._shared_lock = Lock()

    def _ledger_connection_key(self, account):
        """Match authentication scopes without storing/exposing raw credentials."""
        provider, *parts = account.split(":")
        try:
            if provider == "toss":
                client = self._providers.toss_client(parts[0])
                identifier = getattr(client, "client_id", account)
            else:
                label = "primary" if len(parts) == 1 else parts[0]
                client = self._providers.kis_client(label)
                identifier = getattr(client, "app_key", account.rsplit(":", 1)[0])
        except Exception:  # noqa: BLE001 — client initialization is handled by _run
            identifier = account
        return hashlib.sha256(f"{provider}:{identifier}".encode()).hexdigest()

    # ── health bookkeeping ───────────────────────────────

    def _status_for(self, account: str, provider: str) -> SourceStatus:
        with self._status_lock:
            entry = self._status.get(account)
            if entry is None:
                entry = SourceStatus(account=account, provider=provider)
                self._status[account] = entry
            return entry

    def _record_failure(self, account: str, provider: str, exc: Exception) -> dict[str, Any]:
        """Fold one failure into a source's rolling health and return the entry."""
        with self._status_lock:
            status = self._status_for(account, provider)
            formatted = self._providers.error_formatter(exc)
            error = formatted.get("error") if isinstance(formatted, dict) else None
            code = str((error or {}).get("code") or type(exc).__name__)
            status.status = "not_configured" if code.endswith("_NOT_CONFIGURED") else "error"
            status.last_error_at = _now()
            status.last_error = {"code": code, "kind": (error or {}).get("kind", "provider_unavailable"), "message": (error or {}).get("message", "Provider failed.")}
            if provider == "kis" and hasattr(exc, "body"):
                status.last_error["broker_diagnostic"] = _masked_kis_diagnostic(exc)
            if getattr(exc, "rate_limited", False) or getattr(exc, "status", None) == 429:
                status.last_error["kind"] = "rate_limited"
            retry_after = getattr(exc, "retry_after_seconds", None)
            if retry_after is not None:
                status.last_error["retry_after_seconds"] = retry_after
            status.consecutive_failures += 1
            if self._providers.health_observer:
                self._providers.health_observer(account, status=status.last_error["kind"],
                    error_code=status.last_error["kind"], error=status.last_error, complete=False)
            return status.to_dict()

    def _run(self, account: str, provider: str, operation: Callable[[], Any]) -> tuple[Any, dict[str, Any]]:
        """Execute one source call, recording latency and health. Never raises."""
        started = time.monotonic()
        try:
            value = operation()
        except Exception as e:  # noqa: BLE001 — every broker failure must degrade, not abort
            with self._status_lock:
                status = self._status_for(account, provider)
                status.latency_ms = round((time.monotonic() - started) * 1000, 1)
                entry = self._record_failure(account, provider, e)
            logger.warning("portfolio provider %s failed: %s", provider, entry["last_error"]["kind"])
            return None, entry
        with self._status_lock:
            status = self._status_for(account, provider)
            status.latency_ms = round((time.monotonic() - started) * 1000, 1)
            status.status = "ok"
            status.last_success_at = _now()
            status.consecutive_failures = 0
            # last_error and its timestamp remain history, not the current outcome.
            if self._providers.health_observer:
                truncated = bool(value.get("truncated") if isinstance(value, dict) else getattr(value, "truncated", False))
                partial = bool(value.get("errors")) if isinstance(value, dict) else False
                self._providers.health_observer(account,
                    status="truncated" if truncated else "partial" if partial else "ok",
                    count=len(value) if isinstance(value, list) else None,
                    complete=not (truncated or partial), truncated=truncated)
            return value, status.to_dict()

    @staticmethod
    def _note_truncation(collection: Collection, source: str, payload: Any) -> None:
        """Mark a source whose broker paging was cut short.

        An incomplete trade history silently corrupts cost basis, so it is recorded
        on the collection and reflected in that source's health entry.
        """
        incomplete = payload.get('truncated') if isinstance(payload, dict) else getattr(payload, 'truncated', False)
        if isinstance(payload, dict) and source.startswith('toss:') and source.count(':') == 1:
            incomplete = incomplete_page(payload)
        if not incomplete:
            return
        if source not in collection.truncated_sources:
            collection.truncated_sources.append(source)
        entry = collection.health.get(source)
        if isinstance(entry, dict):
            entry["status"] = "truncated"
            entry["warning"] = (
                "upstream paging cap reached — rows are incomplete; narrow the date range"
            )

    def _toss_accounts(self, wanted: Iterable[str] | None) -> tuple[list[str], dict[str, Any] | None]:
        try:
            labels = self._providers.toss_labels()
        except Exception as e:  # noqa: BLE001
            return [], self._record_failure("toss", "toss", e)
        if wanted is None:
            return labels, None
        wanted_set = set(wanted)
        return [label for label in labels if f"toss:{label}" in wanted_set], None

    def _kis_accounts(self) -> tuple[list[Any], dict[str, Any] | None]:
        """Configured KIS accounts, or the failure that kept them from resolving.

        Per-source filtering happens on the composed source names, so every
        account is returned here regardless of the `accounts` filter.
        """
        try:
            return list(self._providers.kis_accounts()), None
        except Exception as e:  # noqa: BLE001
            return [], self._record_failure("kis", "kis", e)

    # ── collectors ───────────────────────────────────────

    def collect_positions(self, accounts: list[str] | None = None) -> Collection:
        """Holdings + cash from every selected account.

        Inside shared_position_reads(), a call made while the same read is in flight, or
        within SHARED_POSITIONS_SECONDS after it, gets a copy of that collection.
        """
        if not _SHARED_POSITIONS.get():
            return self._collect_positions(accounts)
        key = None if accounts is None else tuple(accounts)
        with self._shared_lock:
            entry = self._shared_positions.get(key)
            stale = entry is not None and entry.finished is not None and (
                entry.value is None or time.monotonic() - entry.finished > SHARED_POSITIONS_SECONDS)
            owner = entry is None or stale
            if owner:
                entry = self._shared_positions[key] = _SharedRead()
        if not owner:
            remaining = nav_read_remaining()
            if entry.done.wait(remaining) and entry.value is not None:
                return deepcopy(entry.value)
            return self._collect_positions(accounts)  # the shared read failed: read on our own
        try:
            collection = self._collect_positions(accounts)
            entry.value = deepcopy(collection)
            return collection
        finally:
            entry.finished = time.monotonic()
            entry.done.set()

    def _collect_positions(self, accounts: list[str] | None = None) -> Collection:
        collection = Collection(fetched_at=_now())
        labels, toss_error = self._toss_accounts(accounts)
        if toss_error is not None:
            collection.health["toss"] = toss_error

        for label in labels:
            account = f"toss:{label}"
            payload, health = self._run(account, "toss", lambda label=label: self._providers.toss_client(label).get_holdings())
            collection.health[account] = health
            validated = validate_toss_response('holdings', payload)
            self._nav_quality(collection, account, validated)
            if validated.value is not None:
                collection.nav_holding_markets[account] = {
                    row['symbol'].strip().upper(): row['marketCountry'] for row in validated.value['items']}
            if payload is not None:
                try:
                    collection.holdings.extend(toss_holdings(payload, account))
                except (TypeError, ValueError, OverflowError):
                    pass  # Invalid source evidence already blocks NAV; an unnormalizable input yields no rows.
                self._note_truncation(collection, account, payload)
            for currency in ("KRW", "USD"):
                cash_account = f"{account}:{currency.lower()}"
                cash_payload, cash_health = self._run(
                    cash_account,
                    "toss",
                    lambda label=label, currency=currency: self._toss_buying_power(label, currency),
                )
                collection.health[cash_account] = cash_health
                self._nav_quality(collection, cash_account,
                                  validate_toss_response('buying_power', cash_payload, currency=currency))
                if cash_payload is not None:
                    try:
                        collection.cash.extend(toss_cash(cash_payload, account, currency))
                    except (TypeError, ValueError, OverflowError):
                        pass  # Preserve every normalizable legacy value, without failing the collection.

        kis_accounts, kis_error = self._kis_accounts()
        if kis_error is not None:
            collection.health["kis"] = kis_error

        for account in kis_accounts:
            label = account.label
            domestic = kis_source(label, "domestic")
            if _wants(accounts, domestic):
                with kis_ledger_gate(getattr(account, "cano", label)):
                    payload, health = self._run(
                        domestic,
                        "kis",
                        lambda label=label: self._providers.kis_client(label).get_domestic_balance(),
                    )
                collection.health[domestic] = health
                if payload is not None:
                    collection.holdings.extend(kis_domestic_holdings(payload, domestic))
                    collection.cash.extend(kis_domestic_cash(payload, domestic))

            overseas = kis_source(label, "overseas")
            # 연금저축/퇴직연금 accounts hold no overseas securities, so the six-call
            # overseas fan-out is skipped rather than reported as an empty source.
            if _wants(accounts, overseas) and account.supports_overseas:
                with kis_ledger_gate(getattr(account, "cano", label)):
                    payload, health = self._run(
                        overseas,
                        "kis",
                        lambda label=label: self._providers.kis_client(label).get_overseas_balance(),
                    )
                collection.health[overseas] = health
                if payload is not None:
                    collection.holdings.extend(kis_overseas_holdings(payload, overseas))
                    collection.cash.extend(kis_overseas_cash(payload, overseas))
                    self._note_truncation(collection, overseas, payload)
                    for market, error in (payload.get("errors") or {}).items():
                        collection.health[f"{overseas}:{market}"] = {
                            "account": f"{overseas}:{market}",
                            "provider": "kis",
                            "status": "error",
                            "last_error": {"code": "KIS_API_ERROR", "message": str(error)[:300]},
                            "last_error_at": _now(),
                        }
        return collection

    def _toss_buying_power(self, label, currency):
        client = self._providers.toss_client(label)
        read = getattr(client, 'get_buying_power_evidence', None) or client.get_buying_power
        payload = read(currency=currency)
        if getattr(client, 'get_buying_power_evidence', None):
            payload.get('result', {})  # Preserve the legacy accessor's failure on a non-object envelope.
        return payload

    @staticmethod
    def _nav_quality(collection, source, validation):
        if validation.reason_codes:
            previous = collection.nav_source_quality_reasons.get(source, [])
            collection.nav_source_quality_reasons[source] = list(dict.fromkeys([*previous, *validation.reason_codes]))

    def collect_nav_evidence(self, collection: Collection) -> None:
        """Read Toss NAV evidence without changing normalized/legacy balances.

        Buying power is usable cash only with a clearly complete, empty OPEN
        order page. This assumes reusable unsettled sale proceeds are included
        and only open orders impose holds; it does not certify withdrawable or
        settled cash. Each client GET retains the existing connection pacing.
        Raw orders, exceptions and quote payloads are never retained or logged.
        Each sanitized quote keeps its price, currency and timestamp together.
        """
        accounts = {row.account for row in [*collection.holdings, *collection.cash]
                    if row.provider == "toss"}
        accounts.update(name for name in collection.health if name.startswith("toss:") and name.count(":") == 1)
        calendars = {}
        for account in sorted(accounts):
            nav_read_remaining()
            evidence = {"open_orders": "unverified", "observed_at": None, "quotes": {}, "market_calendars": {}}
            collection.nav_evidence[account] = evidence
            try:
                client = self._providers.toss_client(account.split(":", 1)[1])
                payload = client.get_orders(status="OPEN")
                evidence["observed_at"] = _now()
                validated = validate_toss_response('orders', payload)
                self._nav_quality(collection, f'{account}:orders', validated)
                if validated.value is not None:
                    evidence["open_orders"] = 'present' if validated.value['orders'] else 'none'
                    evidence['open_order_count'] = len(validated.value['orders'])
            except Exception:  # noqa: BLE001 — unavailable evidence keeps cash unknown
                self._nav_quality(collection, f'{account}:orders', validate_toss_response('orders', None))
            symbols = sorted({row.symbol for row in collection.holdings
                              if row.account == account and row.quantity > 0})
            if symbols:
                try:
                    nav_read_remaining()
                    read_prices = getattr(client, 'get_prices_evidence', None) or client.get_prices
                    requested = {row.symbol: row.currency for row in collection.holdings
                                 if row.account == account and row.quantity > 0}
                    validated = validate_toss_response('prices', read_prices(symbols), symbols=requested)
                    self._nav_quality(collection, f'{account}:prices', validated)
                    evidence['quotes'] = validated.quotes
                except Exception:  # noqa: BLE001 — never substitute observation time
                    self._nav_quality(collection, f'{account}:prices', validate_toss_response('prices', None))
                validated_markets = collection.nav_holding_markets.get(account, {})
                for country in sorted({validated_markets[symbol] for symbol in symbols if symbol in validated_markets}):
                    if country not in calendars:
                        try:
                            nav_read_remaining()
                            read_calendar = getattr(client, 'get_market_calendar_evidence', None) or client.get_market_calendar
                            calendars[country] = validate_toss_response('calendar', read_calendar(country), country=country)
                        except Exception:  # noqa: BLE001 — unavailable calendar cannot prove freshness
                            calendars[country] = validate_toss_response('calendar', None, country=country)
                    self._nav_quality(collection, f'{account}:calendar:{country}', calendars[country])
                    evidence['market_calendars'][country] = calendars[country].value
                # Supplemental instrument identity uses the existing /stocks route.
                # Failure limits dividend inference, never the usable NAV/cash facts.
                kr_symbols = {row.symbol: row.currency for row in collection.holdings
                              if row.account == account and row.quantity > 0 and row.currency == 'KRW'}
                if kr_symbols:
                    try:
                        nav_read_remaining()
                        instruments = validate_toss_response('stock_info',
                            client.get_stock_info_evidence(sorted(kr_symbols)), symbols=kr_symbols)
                        if instruments.value is not None:
                            evidence['instrument_bindings'] = {row['symbol']: {
                                key: row[key] for key in ('symbol', 'currency', 'market', 'name', 'englishName', 'isinCode')
                                if key in row} | {'basis': 'toss-stock-info-v1', 'observed_at': _now()}
                                for row in instruments.value}
                        evidence['instrument_binding_reasons'] = instruments.reason_codes
                    except Exception:
                        evidence['instrument_binding_reasons'] = ['TOSS_INSTRUMENT_UNAVAILABLE']
        collection.nav_completed_at = _now()

    def collect_transactions(
        self, start: str, end: str, accounts: list[str] | None = None,
        symbol: str | None = None, *, resume_cursor: str | None = None,
        page_size: int = 100,
    ) -> Collection:
        """Collect bounded windows; resume only failed source/window pairs.

        Cursors are opaque process-local capabilities, bound to the original
        filters. Successful chunks are retained; a retry replaces its chunk.
        Stable order-aggregate IDs deduplicate overlap; unidentified rows are
        preserved rather than collapsed by a guessed ID.
        """
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        windows = list(history_windows(start, end))
        request = (start, end, None if accounts is None else tuple(sorted(accounts)), symbol, page_size)
        with self._recovery_lock:
            now = time.monotonic()
            self._recoveries = {k: v for k, v in self._recoveries.items() if now - v["created"] < 1800}
            self._cooldowns = {k: v for k, v in self._cooldowns.items() if v[0] > now}
            if resume_cursor:
                session = self._recoveries.get(resume_cursor)
                if session is None or session["request"] != request:
                    raise ValueError("Invalid, expired, or mismatched resume cursor; restart the original request")
            else:
                sources, errors = [], {}
                labels, error = self._toss_accounts(accounts)
                if error:
                    errors["toss"] = error
                sources.extend((f"toss:{label}:orders", f"toss:{label}") for label in labels)
                kis_accounts, error = self._kis_accounts()
                if error:
                    errors["kis"] = error
                for account in kis_accounts:
                    for scope in ("domestic", "overseas"):
                        name = kis_source(account.label, scope)
                        if _wants(accounts, name) and (scope == "domestic" or account.supports_overseas):
                            sources.append((f"{name}:trades", name))
                incremental = not self._strict_toss_fills and not full_backfill_requested()
                anchors = self._history_anchors([source for source, _ in sources], start) if incremental else {}
                chunks = [(source, account, a, b) for source, account in sources for a, b in windows
                          if not (source in anchors and b < anchors[source]["date"])]
                # Discovery failures are independently resumable too.
                chunks.extend((source, None, start, end) for source in errors)
                session = {"created": now, "request": request, "chunks": chunks, "results": {},
                           "anchors": anchors, "incremental": incremental, "cached": set()}
                for index, (source, _, _, _) in enumerate(chunks):
                    if source in errors:
                        session["results"][index] = Collection(health={source: errors[source]})

            for index, (source, account, chunk_start, chunk_end) in enumerate(session["chunks"]):
                previous = session["results"].get(index)
                if previous is not None and not previous.failed_sources and not previous.truncated_sources:
                    continue
                # On a first request discovery failures are already recorded.
                if previous is not None and not resume_cursor:
                    continue
                if account is None:
                    discovered = []
                    if source == "toss":
                        labels, error = self._toss_accounts(accounts)
                        discovered = [(f"toss:{label}:orders", f"toss:{label}") for label in labels]
                    else:
                        configured, error = self._kis_accounts()
                        for item in configured:
                            for scope in ("domestic", "overseas"):
                                name = kis_source(item.label, scope)
                                if _wants(accounts, name) and (scope == "domestic" or item.supports_overseas):
                                    discovered.append((f"{name}:trades", name))
                    session["results"][index] = Collection(
                        health={source: error} if error else {} if discovered else {source: {"status": "empty"}})
                    if not error:
                        session["chunks"].extend((s, acct, a, b) for s, acct in discovered for a, b in windows)
                    continue
                connection = self._ledger_connection_key(account)
                cooldown = self._cooldowns.get(connection)
                if cooldown and cooldown[0] > time.monotonic():
                    health = {**cooldown[1], "account": source}
                    health["last_error"] = {**health["last_error"],
                        "retry_after_seconds": math.ceil(cooldown[0] - time.monotonic())}
                    # Retain partial pages from this failed chunk on early resume.
                    chunk = previous or Collection()
                    chunk.health = {source: health}
                    session["results"][index] = chunk
                    continue
                cache_key = (source, account, chunk_start, chunk_end, symbol, page_size)
                cached = self._cached_window(cache_key) if session.get("incremental") else None
                if cached is not None:
                    session["results"][index] = cached
                    session["cached"].add(index)
                    continue
                chunk = self._collect_transaction_window(chunk_start, chunk_end,
                    [account] if account else accounts, symbol, page_size=page_size)
                chunk.health = {k: v for k, v in chunk.health.items()
                                if k == source or k == source.removesuffix(":trades") + ":trades_fallback"}
                chunk.truncated_sources = [k for k in chunk.truncated_sources if k in chunk.health]
                session["results"][index] = chunk
                if session.get("incremental"):
                    self._store_window(cache_key, chunk)
                for health in chunk.health.values():
                    error = health.get("last_error") or {}
                    if health.get("status") == "error" and error.get("kind") == "rate_limited":
                        delay = error.get("retry_after_seconds")
                        self._cooldowns[connection] = (time.monotonic() + max(1, delay or 1), health)

            result = Collection(fetched_at=_now(), requested_window={"start": start, "end": end})
            seen = set()
            for index, chunk in session["results"].items():
                source, _, a, b = session["chunks"][index]
                result.toss_fill_windows.setdefault(source, []).extend(chunk.toss_fill_windows.get(source, []))
                for tx in chunk.transactions:
                    if tx.execution_id:
                        if tx.execution_id in seen:
                            continue
                        seen.add(tx.execution_id)
                    result.transactions.append(tx)
                for name, health in chunk.health.items():
                    prior = result.health.get(name)
                    if prior is None or health["status"] not in {"ok", "empty"}:
                        result.health[name] = health
                result.truncated_sources.extend(chunk.truncated_sources)
                if chunk.failed_sources or chunk.truncated_sources:
                    retries = [(h.get("last_error") or {}).get("retry_after_seconds") for h in chunk.health.values()]
                    result.failed_chunks.append({"source": source, "requested_window": {"start": a, "end": b},
                        "status": "failed" if chunk.failed_sources else "truncated",
                        "retry_after_seconds": max((r for r in retries if r is not None), default=None)})
            result.truncated_sources = sorted(set(result.truncated_sources))
            result.transactions.sort(key=lambda t: (t.trade_date, t.provider, t.tx_id))
            dates = [t.trade_date for t in result.transactions]
            result.observed_window = {"start": min(dates, default=None), "end": max(dates, default=None)}
            result.fetch_complete = bool(result.health) and not result.failed_chunks
            for source, health in result.health.items():
                failed = health.get("status") not in {"ok", "empty", "truncated"}
                account = source.removesuffix(":orders").removesuffix(":trades").removesuffix(":trades_fallback")
                rows = [t for t in result.transactions if t.account == account]
                metadata = source_metadata(source, None if failed else len(rows),
                    complete=not failed and source not in result.truncated_sources,
                    truncated=source in result.truncated_sources,
                    error_code=(health.get("last_error") or {}).get("kind", "provider_unavailable") if failed else None,
                    start=start, end=end)
                metadata.update({"requested_window": result.requested_window,
                    "fetched_windows": [{"start": a, "end": b} for index, chunk in session["results"].items()
                        for name, _, a, b in [session["chunks"][index]] if name == source
                        and chunk.health.get(source, {}).get("status") in {"ok", "empty"}
                        and source not in chunk.truncated_sources],
                    "observed_window": {"start": min((t.trade_date for t in rows), default=None),
                                        "end": max((t.trade_date for t in rows), default=None)},
                    "fetch_complete": metadata["complete"], "historical_coverage": "unknown"})
                anchor = session.get("anchors", {}).get(source)
                if anchor is not None:
                    # Windows before the anchor were empty in a stored complete read.
                    metadata["ledger_anchor"] = {"fetch_start": anchor["date"],
                        "evidence": "STORED_COMPLETE_READ", "evidence_end": anchor.get("evidence_end"),
                        "skipped_windows": sum(1 for a, b in windows if b < anchor["date"])}
                reused = sum(1 for index in session.get("cached", ()) if session["chunks"][index][0] == source)
                if reused:
                    metadata["reused_settled_windows"] = reused
                result.source_metadata[source] = metadata
            if result.failed_chunks:
                cursor = resume_cursor or secrets.token_urlsafe(24)
                if cursor not in self._recoveries and len(self._recoveries) >= 16:
                    self._recoveries.pop(next(iter(self._recoveries)))
                self._recoveries[cursor] = session
                result.resume_cursor = cursor
            elif resume_cursor:
                self._recoveries.pop(resume_cursor, None)
            if self._providers.statement_store is not None:
                from statement_import import merge_statements
                merge_statements(result, self._providers.statement_store(), start, end, accounts, symbol)
            if self._providers.ledger_observer:
                try:
                    self._providers.ledger_observer(result)
                except Exception:
                    logger.error("permanent ledger observation failed: LEDGER_WRITE_FAILED")
                    result.ledger_persistence = {"status": "partial", "reason_codes": ["LEDGER_WRITE_FAILED"]}
            return result

    def _history_anchors(self, sources: list[str], start: str) -> dict[str, dict[str, Any]]:
        if self._providers.ledger_anchor is None:
            return {}
        try:
            anchors = self._providers.ledger_anchor(sources, start)
        except Exception:  # noqa: BLE001 — no anchor means a full backfill, never a failure
            logger.warning("ledger anchor unavailable: LEDGER_ANCHOR_UNAVAILABLE")
            return {}
        return {source: anchor for source, anchor in (anchors or {}).items()
                if isinstance(anchor, dict) and isinstance(anchor.get("date"), str)
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}", anchor["date"])}

    def _cached_window(self, key: tuple) -> Collection | None:
        entry = self._window_cache.get(key)
        if entry is None:
            return None
        if time.monotonic() - entry[0] >= WINDOW_CACHE_SECONDS:
            self._window_cache.pop(key, None)
            return None
        return deepcopy(entry[1])

    def _store_window(self, key: tuple, chunk: Collection) -> None:
        """Keep a complete read of a settled window; anything else is read again."""
        settled = (datetime.now(_KST).date() - timedelta(days=SETTLED_WINDOW_DAYS)).isoformat()
        if (key[3] > settled or chunk.failed_sources or chunk.truncated_sources or not chunk.health
                or any(h.get("status") not in {"ok", "empty"} for h in chunk.health.values())):
            return
        if len(self._window_cache) >= WINDOW_CACHE_MAX:
            self._window_cache.pop(next(iter(self._window_cache)))
        self._window_cache[key] = (time.monotonic(), deepcopy(chunk))

    def _collect_transaction_window(
        self,
        start: str,
        end: str,
        accounts: list[str] | None = None,
        symbol: str | None = None,
        *, page_size: int = 100,
    ) -> Collection:
        """Normalized ledger entries for [start, end] (ISO dates).

        Overseas KIS trades come from 일별거래내역 (fees + settlement + FX rate);
        if that call fails, 주문체결내역 is used and flagged as fee-less.
        """
        collection = Collection(fetched_at=_now())
        kis_start, kis_end = start.replace("-", ""), end.replace("-", "")
        labels, toss_error = self._toss_accounts(accounts)
        if toss_error is not None:
            collection.health["toss"] = toss_error

        for label in labels:
            account = f"toss:{label}"
            source = f"{account}:orders"
            orders, health = self._run(
                source,
                "toss",
                lambda label=label: self._providers.toss_client(label).iter_closed_orders(
                    date_from=start, date_to=end, symbol=symbol, page_size=page_size
                ),
            )
            collection.health[source] = health
            if orders is not None:
                client = self._providers.toss_client(label)
                txs = toss_transactions(orders, account)
                collection.transactions.extend(self._journal_identity(txs, getattr(client, "journal_account_key", account)))
                self._note_truncation(collection, source, orders)
                if getattr(orders, "fetch_error", None):
                    collection.health[source] = self._record_failure(source, "toss", orders.fetch_error)
            if self._strict_toss_fills:
                # Evidence is supplemental: it cannot change legacy source health,
                # retries, accepted rows or the daily job's exit status.
                window = {'start': start + 'T00:00:00+09:00', 'end': end + 'T23:59:59.999999+09:00',
                          'complete': False, 'reasons': ['FILL_EVIDENCE_UNAVAILABLE'], 'transactions': []}
                try:
                    client = self._providers.toss_client(label)
                    evidence = client.get_fill_history_evidence(start, end, page_size=page_size)
                    window.update(start=evidence.fill_start, end=evidence.fill_end,
                        complete=evidence.complete and symbol is None,
                        reasons=[*evidence.fill_reasons, *(['SYMBOL_FILTERED_FILL_WINDOW'] if symbol else [])],
                        transactions=self._journal_identity(toss_transactions(evidence, account),
                            getattr(client, 'journal_account_key', account)))
                except Exception:
                    pass  # Failure is recorded only in the separate inference window.
                collection.toss_fill_windows[source] = [window]

        kis_accounts, kis_error = self._kis_accounts()
        if kis_error is not None:
            collection.health["kis"] = kis_error

        for account in kis_accounts:
            label = account.label
            domestic = kis_source(label, "domestic")
            if _wants(accounts, domestic):
                source = f"{domestic}:trades"
                payload, health = self._run(
                    source,
                    "kis",
                    lambda label=label: self._providers.kis_client(label).get_domestic_trade_history(
                        kis_start, kis_end
                    ),
                )
                collection.health[source] = health
                if payload is not None:
                    client = self._providers.kis_client(label)
                    collection.transactions.extend(self._journal_identity(kis_domestic_transactions(payload, domestic),
                        getattr(client, "journal_account_key", legacy_kis_account_key(domestic))))
                    self._note_truncation(collection, source, payload)

            overseas = kis_source(label, "overseas")
            if _wants(accounts, overseas) and account.supports_overseas:
                source = f"{overseas}:trades"
                payload, health = self._run(
                    source,
                    "kis",
                    lambda label=label: self._providers.kis_client(label).get_overseas_period_trans(
                        kis_start, kis_end
                    ),
                )
                collection.health[source] = health
                if payload is not None:
                    client = self._providers.kis_client(label)
                    collection.transactions.extend(self._journal_identity(kis_overseas_transactions(payload, overseas),
                        getattr(client, "journal_account_key", legacy_kis_account_key(overseas))))
                    self._note_truncation(collection, source, payload)
                elif (health.get("last_error") or {}).get("kind") != "rate_limited" and (health.get("last_error") or {}).get("code") != "KIS_RATE_LIMITED":
                    fallback_source = f"{overseas}:trades_fallback"
                    fallback, fallback_health = self._run(
                        fallback_source,
                        "kis",
                        lambda label=label: self._providers.kis_client(label).get_overseas_trade_history(
                            kis_start, kis_end
                        ),
                    )
                    collection.health[fallback_source] = fallback_health
                    if fallback is not None:
                        client = self._providers.kis_client(label)
                        collection.transactions.extend(self._journal_identity(kis_overseas_ccnl_transactions(fallback, overseas),
                            getattr(client, "journal_account_key", legacy_kis_account_key(overseas))))
                        self._note_truncation(collection, fallback_source, fallback)

        if symbol:
            wanted = symbol.strip().upper()
            collection.transactions = [tx for tx in collection.transactions if (tx.symbol or "").upper() == wanted]
        for source, health in collection.health.items():
            failed = health.get("status") not in {"ok", "empty", "truncated"}
            source_account = source.removesuffix(":orders").removesuffix(":trades").removesuffix(":trades_fallback")
            count = None if failed else sum(tx.account == source_account for tx in collection.transactions)
            error = (health.get("last_error") or {}).get("kind", "provider_unavailable") if failed else None
            collection.source_metadata[source] = source_metadata(source, count, complete=not failed and source not in collection.truncated_sources,
                truncated=source in collection.truncated_sources, error_code=error, start=start, end=end)
        collection.transactions.sort(key=lambda tx: (tx.trade_date, tx.provider, tx.tx_id))
        return collection

    @staticmethod
    def _journal_identity(transactions, account_key):
        result = []
        for tx in transactions:
            native = tx.tx_id if tx.identity_quality != "unavailable" else ""
            fields = identity(tx.provider, account_key, tx.market or "", native, tx.identity_date or tx.trade_date)
            result.append(replace(tx, **{k: v for k, v in fields.items()
                if k in {"execution_id", "identity_quality", "account_key", "parent_order_id"}}))
        return result

    # ── FX ───────────────────────────────────────────────

    def build_fx(self, currencies: Iterable[str]) -> FxRates:
        """Resolve spot `currency -> KRW` rates for the currencies actually held (FX_POLICY).

        Toss's broker rate first; yfinance only when Toss has no valid rate for the currency,
        labelled with Toss's reason codes. An injected `fx_quote` is the only quote. The first
        quote without reason codes wins; with none, the first quote that has a rate is kept
        with its reason codes (as before), else the currency is missing. A flagged Toss rate
        (binding or invalid-rate reasons) and any rate <= 0 count as no rate: a
        missing currency's `sources` entry carries each source's reason codes.
        """
        injected = self._providers.fx_quote
        chain = [("provider", injected)] if injected else [
            ("toss", self._toss_fx_quote), ("yfinance", lambda currency: yfinance_fx_quote(currency, include_time=True))]
        fx = FxRates(as_of=_now())
        for currency in {(c or "").upper() for c in currencies}:
            if not currency or currency == "KRW":
                continue
            attempts = []
            for source, quote in chain:  # a later source is read only when the earlier one is not clean
                attempt = _fx_attempt(quote, currency)
                if attempt["rate"] is not None and (not _positive_rate(attempt["rate"])
                                                    or source == "toss" and attempt["reasons"]):
                    attempt = {**attempt, "rate": None, "reasons": attempt["reasons"] or ["INVALID_SOURCE_RESPONSE"]}
                attempts.append((source, attempt))
                if attempts[-1][1]["rate"] and not attempts[-1][1]["reasons"]:
                    break
            index = ([i for i, (_, a) in enumerate(attempts) if a["rate"] and not a["reasons"]]
                     or [i for i, (_, a) in enumerate(attempts) if a["rate"]] or [len(attempts) - 1])[0]
            source, used = attempts[index]
            if used["reasons"]:
                fx.nav_source_quality_reasons[currency] = used["reasons"]
            if not used["rate"]:
                fx.missing.add(currency)
                fx.source_by_currency[currency] = {"source": None, "missing": True, "reason_codes_by_source": {
                    name: attempt["reasons"] for name, attempt in attempts}}
                continue
            mark = used["mark"]
            fx.rates[currency] = used["rate"]
            if isinstance(mark, dict) and isinstance(mark.get('rate'), str) and not used["reasons"]:
                fx.rate_decimal_by_currency[currency] = mark['rate']
            fx.priced_at_by_currency[currency] = used["stamp"]
            fx.read_at_by_currency[currency] = used["read_at"]
            if isinstance(mark, dict) and 'validUntil' in mark:
                fx.valid_until_by_currency[currency] = mark['validUntil']
            label = {"source": source, "priced_at": used["stamp"]}
            if isinstance(mark, dict) and 'validUntil' in mark:
                label["valid_until"] = mark['validUntil']
            if index:
                label.update(fallback=True, fallback_reason_codes=list(dict.fromkeys(
                    code for _, a in attempts[:index] for code in a["reasons"])))
            fx.source_by_currency[currency] = label
        return fx

    def _toss_fx_quote(self, currency: str) -> Any:
        """Toss get_exchange_rate for `currency` -> KRW (account-independent: first label)."""
        if currency not in _TOSS_FX_CURRENCIES:
            raise _FxUnavailable("TOSS_FX_CURRENCY_UNSUPPORTED")
        try:
            label = self._providers.toss_labels()[0]
        except Exception as exc:  # noqa: BLE001 — Toss not configured: the fallback quote is used
            raise _FxUnavailable("TOSS_FX_UNAVAILABLE") from exc
        return self._providers.toss_client(label).get_exchange_rate(base=currency, quote="KRW")

    def health_report(self) -> dict[str, dict[str, Any]]:
        """Rolling health for every source touched since process start."""
        with self._status_lock:
            return {name: status.to_dict() for name, status in sorted(self._status.items())}


#: Currencies Toss's exchange-rate endpoint quotes (OpenAPI 1.2.24 `Currency` enum: KRW, USD).
_TOSS_FX_CURRENCIES = frozenset({"USD"})


class _FxUnavailable(Exception):
    """A quote source has no rate for the currency; `reason` labels the fallback."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _positive_rate(rate: Any) -> bool:
    """A usable FX rate is a finite number above zero."""
    try:
        return math.isfinite(float(rate)) and float(rate) > 0
    except (TypeError, ValueError):
        return False


def _fx_attempt(quote, currency: str) -> dict[str, Any]:
    """One quote read through the strict FX validation: rate, mark, time and reason codes."""
    try:
        mark = quote(currency)
        read_at = _fx_read_time()
        rate = to_float(mark.get("rate")) if isinstance(mark, dict) else mark
        stamp = mark.get("validFrom", mark.get("priced_at")) if isinstance(mark, dict) else None
        reasons = list(validate_fx_input(mark, currency).reason_codes)
    except _FxUnavailable as exc:
        return {"rate": None, "mark": None, "stamp": None, "read_at": None, "reasons": [exc.reason]}
    except Exception:  # noqa: BLE001 — an FX miss degrades totals, not the call
        logger.warning("FX lookup failed")
        return {"rate": None, "mark": None, "stamp": None, "read_at": None, "reasons": ['INVALID_SOURCE_RESPONSE']}
    return {"rate": rate, "mark": mark, "stamp": stamp, "read_at": read_at, "reasons": reasons}


@synchronized(YFINANCE_LOCK)
def yfinance_fx_quote(currency: str, *, include_time=False) -> float | dict[str, Any] | None:
    """Latest price for `<CUR>KRW=X`, i.e. KRW per one unit of `currency`, with its trade time."""
    import yfinance as yf

    pair = _FX_PAIRS.get(currency.upper())
    if not pair:
        return None
    ticker = yf.Ticker(pair)
    history = ticker.history(period="5d")
    if history is None or getattr(history, "empty", True):
        return None
    value = history["Close"].iloc[-1]
    rate = _float32_decimal(value) if value else None
    # The daily bar's index is the day's label (00:00 exchange time), not when the price was seen.
    # The same chart response's metadata carries the last trade's time and price as a pair.
    meta_rate, priced_at = _yfinance_market_time(ticker)
    if meta_rate is not None and priced_at is not None:
        rate = meta_rate
    elif priced_at is None:
        priced_at = _bar_time(history.index)
    if include_time and rate is not None:
        # A null priced_at is reported downstream as UNKNOWN_FX_PRICE_TIME (daily_nav).
        return {"rate": rate, "priced_at": priced_at}
    return rate


def _float32_decimal(value: Any) -> float | None:
    """Undo a float32 round trip (1327.14 stored as 1327.1400146484375); other floats unchanged."""
    import numpy as np

    number = float(value)
    if not math.isfinite(number):
        return None
    single = np.float32(number)
    return float(str(single)) if float(single) == number else number


def _bar_time(index: Any) -> str | None:
    """The last bar's zoned time, unless it is a date label (midnight exchange time)."""
    try:
        stamp = index[-1]
        if stamp.utcoffset() is None or (stamp.hour, stamp.minute, stamp.second, stamp.microsecond) == (0, 0, 0, 0):
            return None
        return stamp.isoformat()
    except (AttributeError, IndexError, TypeError, ValueError):
        return None


def _yfinance_market_time(ticker: Any) -> tuple[float | None, str | None]:
    """(regularMarketPrice, regularMarketTime) from the last history call's metadata, else Nones."""
    try:
        meta = ticker.get_history_metadata() or {}
    except Exception:  # noqa: BLE001 — metadata is optional; the rate then has no time
        return None, None
    price, stamp = to_float(meta.get("regularMarketPrice")), meta.get("regularMarketTime")
    if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
        stamp = datetime.fromtimestamp(stamp, timezone.utc)
    try:
        priced_at = stamp.isoformat() if stamp.utcoffset() is not None else None
    except (AttributeError, TypeError, ValueError):
        priced_at = None
    return (price if price is not None and math.isfinite(price) and price > 0 else None), priced_at


def collect_nav_evidence(collector, collection):
    """Optional reads have one 20s budget; late results cannot mutate a capture.

    A daemon worker isolates even an uncooperative injected client. Production
    Toss lock waits/HTTP/retries share the deadline and start no later reads.
    Only a fully completed evidence pass is published; timeout discards all of it.
    """
    collect = getattr(collector, "collect_nav_evidence", None)
    if collect is None:
        return
    deadline = time.monotonic() + NAV_EVIDENCE_BUDGET_SECONDS
    isolated = deepcopy(collection)
    isolated.nav_evidence = {}
    finished, success = Event(), []

    def run():
        token = _NAV_READ_DEADLINE.set(deadline)
        try:
            collect(isolated)
            nav_read_remaining()
            success.append(True)
        except Exception:  # noqa: BLE001 — optional evidence must never fail a tool
            pass
        finally:
            _NAV_READ_DEADLINE.reset(token)
            finished.set()

    Thread(target=run, name='toss-nav-evidence', daemon=True).start()
    finished.wait(max(0, deadline - time.monotonic()))
    if success and finished.is_set():
        collection.nav_evidence = isolated.nav_evidence
        collection.nav_source_quality_reasons = isolated.nav_source_quality_reasons
        collection.nav_completed_at = isolated.nav_completed_at
        return
    reason = 'NAV_EVIDENCE_BUDGET_EXCEEDED' if time.monotonic() >= deadline else 'NAV_EVIDENCE_UNAVAILABLE'
    accounts = {row.account for row in [*collection.holdings, *collection.cash] if row.provider == 'toss'}
    accounts.update(name for name in collection.health if name.startswith('toss:') and name.count(':') == 1)
    collection.nav_evidence = {account: {'open_orders': 'unverified', 'observed_at': None,
        'quotes': {}, 'reason_codes': [reason]} for account in accounts}
    collection.nav_completed_at = _now()


def incomplete_page(payload):
    """Reject explicit partial/error/continuation markers at either envelope level."""
    page = payload.get('result')
    rows = [payload, page] if isinstance(page, dict) else [payload]
    return any(any(row.get(key) is not None and row.get(key) is not False
                   for key in ('partial', 'truncated', 'hasNext'))
               or any(row.get(key) is not None and row.get(key) != ''
                      for key in ('nextCursor', 'next_cursor'))
               or row.get('error') or row.get('errors') for row in rows)


def kis_source(label: str, scope: str) -> str:
    """Source name for one KIS account scope.

    The `primary` account keeps the original `kis:domestic` / `kis:overseas`
    names so stored NAV breakdowns and existing account filters keep resolving;
    any other account is qualified as `kis:<label>:<scope>`.
    """
    return f"kis:{scope}" if label == "primary" else f"kis:{label}:{scope}"


def _wants(accounts: list[str] | None, name: str) -> bool:
    return accounts is None or name in accounts


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fx_read_time() -> str:
    # Provider validFrom can include fractional seconds. Truncating the read
    # instant to seconds could falsely place a just-read mark in the future.
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')
