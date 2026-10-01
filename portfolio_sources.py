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

import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from journal_metadata import identity, source_metadata
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
    toss_cash,
    toss_holdings,
    toss_transactions,
)

logger = logging.getLogger("k-invest.portfolio_sources")

#: yfinance FX pairs used to convert native currency into KRW. Quoted per one
#: unit of the base currency, avoiding the per-100-yen convention that Korean
#: broker 고시환율 fields use.
_FX_PAIRS = {"USD": "USDKRW=X", "JPY": "JPYKRW=X", "HKD": "HKDKRW=X", "CNY": "CNYKRW=X"}


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
    fx_quote: currency -> KRW rate, or None. Defaults to yfinance.
    """

    toss_labels: Callable[[], list[str]]
    toss_client: Callable[[str], Any]
    kis_accounts: Callable[[], list[Any]]
    kis_client: Callable[[str], Any]
    error_formatter: Callable[[Exception], dict[str, Any]]
    fx_quote: Callable[[str], float | None] | None = None


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

    def __init__(self, providers: Providers):
        self._providers = providers
        self._status: dict[str, SourceStatus] = {}

    # ── health bookkeeping ───────────────────────────────

    def _status_for(self, account: str, provider: str) -> SourceStatus:
        entry = self._status.get(account)
        if entry is None:
            entry = SourceStatus(account=account, provider=provider)
            self._status[account] = entry
        return entry

    def _record_failure(self, account: str, provider: str, exc: Exception) -> dict[str, Any]:
        """Fold one failure into a source's rolling health and return the entry."""
        status = self._status_for(account, provider)
        formatted = self._providers.error_formatter(exc)
        error = formatted.get("error") if isinstance(formatted, dict) else None
        code = str((error or {}).get("code") or type(exc).__name__)
        status.status = "not_configured" if code.endswith("_NOT_CONFIGURED") else "error"
        status.last_error_at = _now()
        status.last_error = {"code": code, "kind": (error or {}).get("kind", "provider_unavailable"), "message": (error or {}).get("message", "Provider failed.")}
        status.consecutive_failures += 1
        return status.to_dict()

    def _run(self, account: str, provider: str, operation: Callable[[], Any]) -> tuple[Any, dict[str, Any]]:
        """Execute one source call, recording latency and health. Never raises."""
        status = self._status_for(account, provider)
        started = time.monotonic()
        try:
            value = operation()
        except Exception as e:  # noqa: BLE001 — every broker failure must degrade, not abort
            status.latency_ms = round((time.monotonic() - started) * 1000, 1)
            entry = self._record_failure(account, provider, e)
            logger.warning("portfolio provider %s failed: %s", provider, entry["last_error"]["kind"])
            return None, entry
        status.latency_ms = round((time.monotonic() - started) * 1000, 1)
        status.status = "ok"
        status.last_success_at = _now()
        status.consecutive_failures = 0
        return value, status.to_dict()

    @staticmethod
    def _note_truncation(collection: Collection, source: str, payload: Any) -> None:
        """Mark a source whose broker paging was cut short.

        An incomplete trade history silently corrupts cost basis, so it is recorded
        on the collection and reflected in that source's health entry.
        """
        if not (payload.get("truncated") if isinstance(payload, dict) else getattr(payload, "truncated", False)):
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
        """Holdings + cash from every selected account."""
        collection = Collection(fetched_at=_now())
        labels, toss_error = self._toss_accounts(accounts)
        if toss_error is not None:
            collection.health["toss"] = toss_error

        for label in labels:
            account = f"toss:{label}"
            payload, health = self._run(account, "toss", lambda label=label: self._providers.toss_client(label).get_holdings())
            collection.health[account] = health
            if payload is not None:
                collection.holdings.extend(toss_holdings(payload, account))
            for currency in ("KRW", "USD"):
                cash_account = f"{account}:{currency.lower()}"
                cash_payload, cash_health = self._run(
                    cash_account,
                    "toss",
                    lambda label=label, currency=currency: self._providers.toss_client(label).get_buying_power(
                        currency=currency
                    ),
                )
                collection.health[cash_account] = cash_health
                if cash_payload is not None:
                    collection.cash.extend(toss_cash(cash_payload, account, currency))

        kis_accounts, kis_error = self._kis_accounts()
        if kis_error is not None:
            collection.health["kis"] = kis_error

        for account in kis_accounts:
            label = account.label
            domestic = kis_source(label, "domestic")
            if _wants(accounts, domestic):
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

    def collect_transactions(
        self,
        start: str,
        end: str,
        accounts: list[str] | None = None,
        symbol: str | None = None,
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
                    date_from=start, date_to=end, symbol=symbol
                ),
            )
            collection.health[source] = health
            if orders is not None:
                client = self._providers.toss_client(label)
                txs = toss_transactions(orders, account)
                collection.transactions.extend(self._journal_identity(txs, getattr(client, "journal_account_key", account)))
                self._note_truncation(collection, source, orders)

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
                        getattr(client, "journal_account_key", domestic)))
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
                        getattr(client, "journal_account_key", overseas)))
                    self._note_truncation(collection, source, payload)
                else:
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
                            getattr(client, "journal_account_key", overseas)))
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
        """Resolve spot `currency -> KRW` rates for the currencies actually held."""
        quote = self._providers.fx_quote or yfinance_fx_quote
        fx = FxRates(as_of=_now())
        for currency in {(c or "").upper() for c in currencies}:
            if not currency or currency == "KRW":
                continue
            try:
                rate = quote(currency)
            except Exception:  # noqa: BLE001 — an FX miss degrades totals, not the call
                logger.warning("FX lookup failed")
                rate = None
            if rate:
                fx.rates[currency] = rate
            else:
                fx.missing.add(currency)
        return fx

    def health_report(self) -> dict[str, dict[str, Any]]:
        """Rolling health for every source touched since process start."""
        return {name: status.to_dict() for name, status in sorted(self._status.items())}


def yfinance_fx_quote(currency: str) -> float | None:
    """Latest close for `<CUR>KRW=X`, i.e. KRW per one unit of `currency`."""
    import yfinance as yf

    pair = _FX_PAIRS.get(currency.upper())
    if not pair:
        return None
    history = yf.Ticker(pair).history(period="5d")
    if history is None or getattr(history, "empty", True):
        return None
    value = history["Close"].iloc[-1]
    return float(value) if value else None


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
