#!/usr/bin/env python3
"""Normalized portfolio domain model shared by every broker.

Broker payloads never leave this module: `Transaction`, `Holding`, and
`CashBalance` are the only shapes the engine and the MCP tools work with.

Conventions (applied uniformly across providers):
  - `trade_date` / `settle_date` are ISO `YYYY-MM-DD` strings.
  - `quantity` is always positive; direction comes from `tx_type`.
  - `gross_amount` is quantity x price in the instrument's own currency.
  - `net_amount` is the signed cash effect in that currency: negative when cash
    leaves the account (BUY, WITHDRAWAL, FEE, TAX), positive when it arrives
    (SELL, DEPOSIT, DIVIDEND, INTEREST).
  - Buy-side fees and taxes are capitalized into cost basis; sell-side fees and
    taxes reduce proceeds. See portfolio_engine.compute_realized_pnl.
  - `fx_rate` converts the native currency to KRW as of the trade, and is only
    set when the broker reports it (KIS 등록환율/최초고시환율). Valuation uses
    current spot rates instead — see FxRates.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from journal_metadata import identity, legacy_kis_account_key

# Instrument currency → market code, used when a provider omits the market.
_CURRENCY_MARKET = {"KRW": "KR", "USD": "US", "JPY": "JP", "HKD": "HK", "CNY": "CN"}

# KIS 매도매수구분코드
_KIS_SIDE = {"01": "SELL", "02": "BUY"}


class TxType(str, Enum):
    """Ledger entry types.

    BUY/SELL/DIVIDEND/FEE/TAX are populated from broker APIs today. The cash-flow
    and corporate-action members are part of the schema so that a ledger can carry
    them once a source exists (see docs: neither Toss nor the KIS open API exposes
    account deposits, withdrawals, or corporate actions).
    """

    BUY = "BUY"
    SELL = "SELL"
    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    DIVIDEND = "DIVIDEND"
    INTEREST = "INTEREST"
    FEE = "FEE"
    TAX = "TAX"
    FX = "FX"
    TRANSFER = "TRANSFER"
    SPLIT = "SPLIT"
    SPINOFF = "SPINOFF"
    MERGER = "MERGER"
    TICKER_CHANGE = "TICKER_CHANGE"
    OTHER = "OTHER"


#: Types that change share count and therefore cost basis.
POSITION_TYPES = {TxType.BUY, TxType.SELL, TxType.SPLIT, TxType.SPINOFF, TxType.MERGER, TxType.TICKER_CHANGE}
#: Types that move cash without changing share count.
INCOME_TYPES = {TxType.DIVIDEND, TxType.INTEREST}
CASHFLOW_TYPES = {TxType.DEPOSIT, TxType.WITHDRAWAL, TxType.TRANSFER}


@dataclass(frozen=True, slots=True)
class Transaction:
    """One normalized ledger entry."""

    provider: str
    account: str
    tx_id: str
    tx_type: TxType
    trade_date: str
    currency: str
    settle_date: str | None = None
    symbol: str | None = None
    name: str | None = None
    market: str | None = None
    exchange: str | None = None
    quantity: float = 0.0
    price: float | None = None
    gross_amount: float | None = None
    fee: float = 0.0
    tax: float = 0.0
    net_amount: float | None = None
    fx_rate: float | None = None
    status: str = "FILLED"
    source: str = ""
    note: str | None = None
    execution_id: str | None = None
    identity_quality: str = "unavailable"
    account_key: str | None = None
    parent_order_id: str | None = None
    identity_date: str | None = None
    price_source_field: str | None = None
    normalizer_version: int = 2
    fee_status: str | None = None
    tax_status: str | None = None
    # Permanent ledger provenance; existing MCP serialization stays unchanged.
    execution_time_utc: str | None = field(default=None, compare=False, repr=False)
    execution_source_timezone: str | None = field(default=None, compare=False, repr=False)
    # Internal strict collection evidence; never changes legacy tool serialization.
    toss_execution: dict | None = field(default=None, compare=False, repr=False)

    def cost_status(self, field: str) -> str:
        status = getattr(self, f"{field}_status")
        if status is not None:
            return status
        return "reported" if getattr(self, field) != 0 else "unknown"

    @property
    def quantity_delta(self) -> float:
        """Signed share-count change this entry applies to the position."""
        if self.tx_type is TxType.BUY:
            return self.quantity
        if self.tx_type is TxType.SELL:
            return -self.quantity
        return 0.0

    def to_dict(self) -> dict[str, Any]:
        # Decimal arithmetic avoids a spurious rounding break at the tolerance.
        expected = (
            None
            if self.price is None
            else Decimal(str(self.price)) * Decimal(str(self.quantity))
        )
        tolerance = (
            Decimal("1") if self.currency.upper() in {"KRW", "JPY"} else Decimal("0.01")
        )
        difference = (
            None
            if expected is None or self.gross_amount is None
            else expected - Decimal(str(self.gross_amount))
        )
        rate = 1.0 if self.currency.upper() == "KRW" else self.fx_rate
        base = (
            None
            if self.price is None or rate is None or rate <= 0
            else float(Decimal(str(self.price)) * Decimal(str(rate)))
        )
        return {
            "provider": self.provider,
            "account": self.account,
            "tx_id": self.tx_id,
            "execution_id": self.execution_id,
            "identity_quality": self.identity_quality,
            "identity_version": 1,
            "record_kind": "order_aggregate",
            "account_key": self.account_key or self.account,
            "parent_order_id": self.parent_order_id,
            "tx_type": self.tx_type.value,
            "trade_date": self.trade_date,
            "settle_date": self.settle_date,
            "symbol": self.symbol,
            "name": self.name,
            "market": self.market,
            "exchange": self.exchange,
            "currency": self.currency,
            "quantity": self.quantity,
            "price": self.price,
            "price_native": {"amount": self.price, "currency": self.currency},
            "price_base": {"amount": base, "currency": "KRW"},
            "price_base_status": "reported" if base is not None else "unknown",
            "price_base_reason": None
            if base is not None
            else "missing_native_price"
            if self.price is None
            else "missing_or_invalid_trade_fx",
            "price_source_field": self.price_source_field,
            "normalizer_version": self.normalizer_version,
            "price_validation": {
                "status": "unknown"
                if difference is None
                else "matched"
                if abs(difference) <= tolerance
                else "mismatch",
                "gross_difference": None if difference is None else float(difference),
                "tolerance": float(tolerance),
                "policy": "spot_stock_quantity_times_native_price; 1 KRW/JPY, 0.01 otherwise; gross preserved",
            },
            "gross_amount": self.gross_amount,
            "fee": self.fee,
            "tax": self.tax,
            "fee_status": self.cost_status("fee"),
            "tax_status": self.cost_status("tax"),
            "fee_reported_amount": self.fee
            if self.cost_status("fee") in {"reported", "verified_zero"}
            else None,
            "tax_reported_amount": self.tax
            if self.cost_status("tax") in {"reported", "verified_zero"}
            else None,
            "net_amount": self.net_amount,
            "fx_rate": self.fx_rate,
            "status": self.status,
            "source": self.source,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class Holding:
    """One position as a single account currently reports it."""

    provider: str
    account: str
    symbol: str
    currency: str
    quantity: float
    name: str | None = None
    market: str | None = None
    exchange: str | None = None
    last_price: float | None = None
    avg_price: float | None = None
    cost_basis: float | None = None
    market_value: float | None = None
    unrealized_pnl: float | None = None
    unrealized_pnl_pct: float | None = None
    includes_buy_fees: bool | None = None
    includes_buy_taxes: bool | None = None
    basis_source: str = "broker_reported"
    # NAV binding evidence only; legacy numeric valuation/serialization is intact.
    last_price_decimal: str | None = field(default=None, compare=False, repr=False)
    quantity_decimal: str | None = field(default=None, compare=False, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "account": self.account,
            "symbol": self.symbol,
            "name": self.name,
            "market": self.market,
            "exchange": self.exchange,
            "currency": self.currency,
            "quantity": self.quantity,
            "last_price": self.last_price,
            "avg_price": self.avg_price,
            "cost_basis": self.cost_basis,
            "broker_reported_cost_basis": {
                "amount": self.cost_basis,
                "currency": self.currency,
                "includes_buy_fees": self.includes_buy_fees,
                "includes_buy_taxes": self.includes_buy_taxes,
                "basis_source": self.basis_source,
            },
            "ledger_cost_basis": {
                "amount": None,
                "currency": self.currency,
                "includes_buy_fees": None,
                "includes_buy_taxes": None,
                "basis_source": "ledger_moving_average",
                "reason": "ledger_not_available",
            },
            "market_value": self.market_value,
            "unrealized_pnl": self.unrealized_pnl,
            "unrealized_pnl_pct": self.unrealized_pnl_pct,
        }


@dataclass(frozen=True, slots=True)
class CashBalance:
    """Native cash plus separate settlement meanings; amount stays compatible.

    Missing broker fields remain unknown. Buying power is not settled cash.
    """

    provider: str
    account: str
    currency: str
    amount: float
    kind: str = "cash"  # "cash" | "buying_power"
    settled_cash: float | None = None
    unsettled_receivables: float | None = None
    unsettled_payables: float | None = None
    withdrawable_cash: float | None = None
    buying_power: float | None = None
    native_cash_includes_unsettled: bool | None = None
    positions_basis: str = "unknown"
    settlement_basis_reason: str = "Broker settlement basis has not been verified."
    source_fields: dict[str, str] = field(default_factory=dict)
    broker_reported_totals: dict[str, Any] = field(default_factory=dict)
    amount_decimal: str | None = field(default=None, compare=False, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "account": self.account,
            "currency": self.currency,
            "amount": self.amount,
            "kind": self.kind,
            **{name: getattr(self, name) for name in (
                "settled_cash", "unsettled_receivables", "unsettled_payables",
                "withdrawable_cash", "buying_power", "native_cash_includes_unsettled",
                "positions_basis", "settlement_basis_reason", "source_fields", "broker_reported_totals",
            )},
            "cash_field_status": {
                name: {"status": "reported" if getattr(self, name) is not None else "unknown",
                       "reason": None if getattr(self, name) is not None else "NOT_REPORTED_BY_SOURCE"}
                for name in ("settled_cash", "unsettled_receivables", "unsettled_payables",
                             "withdrawable_cash", "buying_power")
            },
        }


#: Valuation FX: stated once per response, in the `fx` block.
FX_POLICY = {
    "name": "toss_rate_v1",
    "rule": "KRW per unit from Toss get_exchange_rate `rate` (broker), timed by its validFrom/validUntil",
    "fallback": "yfinance <CUR>KRW=X last trade price and time, only when Toss has no valid rate for the "
                "currency; labelled per currency in fx.sources with fallback_reason_codes",
}
#: methodology.fx_valuation of the holdings/summary tools: a pointer, the rule itself is fx.policy.
FX_VALUATION_NOTE = "current spot rate per fx.policy (Toss broker rate; yfinance fallback labelled in fx.sources), " \
                    "applied to native values"


@dataclass
class FxRates:
    """Spot rates used to express native-currency values in KRW.

    Rates are `currency -> KRW` multipliers. Missing currencies are reported via
    `missing` instead of being silently treated as 1.0, so callers can mark a
    total as partially unconverted.
    """

    rates: dict[str, float] = field(default_factory=dict)
    missing: set[str] = field(default_factory=set)
    as_of: str | None = None
    priced_at_by_currency: dict[str, str | None] = field(default_factory=dict)
    valid_until_by_currency: dict[str, str | None] = field(default_factory=dict)
    read_at_by_currency: dict[str, str | None] = field(default_factory=dict)
    rate_decimal_by_currency: dict[str, str] = field(default_factory=dict)
    nav_source_quality_reasons: dict[str, list[str]] = field(default_factory=dict)
    #: currency -> {"source", ...}: which quote produced the rate (see FX_POLICY).
    source_by_currency: dict[str, dict[str, Any]] = field(default_factory=dict)

    def rate(self, currency: str) -> float | None:
        cur = (currency or "").upper()
        if cur == "KRW":
            return 1.0
        rate = self.rates.get(cur)
        if rate is None or not rate > 0:  # a non-positive rate is no rate
            self.missing.add(cur)
            return None
        return rate

    def to_krw(self, amount: float | None, currency: str) -> float | None:
        if amount is None:
            return None
        rate = self.rate(currency)
        return None if rate is None else amount * rate

    def to_dict(self) -> dict[str, Any]:
        return {
            "rates_to_krw": dict(sorted(self.rates.items())),
            "as_of": self.as_of,
            "missing_currencies": sorted(self.missing),
            "policy": FX_POLICY,
            "sources": dict(sorted(self.source_by_currency.items())),
        }


# ── Product look-through ─────────────

#: Map "MARKET:SYMBOL" -> the underlying a held product exposes and its target multiple, read
#: from the JSON file named by KINVEST_PRODUCT_MAP_FILE (shape: product_map.example.json). Check
#: each fund entry against the issuer's page or fact sheet and keep its source and date with the
#: entry. `reset` "daily" means the multiple is today's daily target, not a holding-period one.
#: Instruments absent from the map (all of them when no file is set) are reported unmapped
#: (NOT_IN_PRODUCT_MAP), never assigned a guessed underlying.
PRODUCT_MAP_FILE_ENV = "KINVEST_PRODUCT_MAP_FILE"
_PRODUCT_KINDS = {"leveraged_etf", "fund", "common_stock"}


def load_product_map(path: str | None) -> tuple[str, dict[str, dict[str, Any]], dict[str, dict[str, Any]],
                                                  dict[str, str]]:
    """(version, products, perps, underlying names) from a product map file; empty without one.

    ``common_stocks`` ("MARKET:SYMBOL" -> name) are their own underlying at 1x. Perps are keyed
    "venue:symbol" and list only those whose reference is not the venue's own crypto asset.
    A set but unreadable or malformed file raises: a silently empty map would hide the error.
    """
    if not path:
        return "product_map_empty", {}, {}, {}
    import json

    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    products = {key: dict(entry) for key, entry in (raw.get("products") or {}).items()}
    for key, name in (raw.get("common_stocks") or {}).items():
        products[key] = {"kind": "common_stock", "underlying": key, "leverage_target": 1, "reset": None,
                         "name": name, "source": {"basis": "broker instrument name",
                                                  "date": raw.get("common_stocks_date")}}
    perps = {key: dict(entry) for key, entry in (raw.get("perps") or {}).items()}
    for key, entry in products.items():
        if entry.get("kind") not in _PRODUCT_KINDS or not entry.get("underlying") \
                or entry.get("reset") not in (None, "daily") or not isinstance(entry.get("leverage_target"), (int, float)):
            raise ValueError(f"product map entry {key!r} needs kind, underlying, leverage_target and reset")
    for key, entry in perps.items():
        if entry.get("kind") != "equity_perp" or not entry.get("underlying"):
            raise ValueError(f"perp map entry {key!r} needs kind equity_perp and an underlying")
    return (str(raw.get("version") or "product_map_unversioned"), products, perps,
            {str(k): str(v) for k, v in (raw.get("underlying_names") or {}).items()})


PRODUCT_MAP_VERSION, PRODUCT_MAP, PERP_MAP, UNDERLYING_NAMES = load_product_map(os.environ.get(PRODUCT_MAP_FILE_ENV))


# ── Parsing helpers ──────────────────────────────────────


def to_float(value: Any) -> float | None:
    """Parse broker numerics, which arrive as comma-formatted strings."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def _f(value: Any, default: float = 0.0) -> float:
    parsed = to_float(value)
    return default if parsed is None else parsed


def iso_date(value: Any) -> str | None:
    """Normalize YYYYMMDD / ISO datetime / YYYY-MM-DD into YYYY-MM-DD."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "T" in text:
        text = text.split("T", 1)[0]
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return None


def market_for(currency: str, symbol: str = "") -> str:
    cur = (currency or "").upper()
    if cur in _CURRENCY_MARKET:
        return _CURRENCY_MARKET[cur]
    return "KR" if symbol.isdigit() else "US"


def _pct(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or not denominator:
        return None
    return round(numerator / abs(denominator) * 100, 4)


# ── Toss normalizers ─────────────────────────────────────


def toss_holdings_quality(payload: Any) -> list[str]:
    """Flag bad inventory quantities before legacy normalization drops rows.

    Only reason codes leave this check; raw rows and quantities are not retained.
    A finite, non-negative quantity (including zero) keeps its legacy handling.
    """
    result = payload.get('result', payload) if isinstance(payload, dict) else {}
    items = result.get('items') if isinstance(result, dict) else None
    for item in items or []:
        value = item.get('quantity') if isinstance(item, dict) else None
        try:
            quantity = to_float(value)
            exact = Decimal(str(value).replace(',', '').strip()) if quantity is not None else None
            if quantity is None or not math.isfinite(quantity) or not exact.is_finite() or exact < 0:
                return ['INVALID_HOLDING_QUANTITY']
        except (InvalidOperation, ValueError, OverflowError):
            return ['INVALID_HOLDING_QUANTITY']
    return []


def toss_holdings(payload: Any, account: str) -> list[Holding]:
    """Normalize `GET /api/v1/holdings` (HoldingsOverview) into Holdings."""
    result = payload.get("result", payload) if isinstance(payload, dict) else {}
    items = result.get("items") if isinstance(result, dict) else None
    holdings: list[Holding] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        quantity = _f(item.get("quantity"))
        if not symbol or quantity <= 0:
            continue
        currency = str(item.get("currency") or "").upper() or ("KRW" if symbol.isdigit() else "USD")
        market_value_node = item.get("marketValue") if isinstance(item.get("marketValue"), dict) else {}
        pnl_node = item.get("profitLoss") if isinstance(item.get("profitLoss"), dict) else {}
        cost_basis = to_float(market_value_node.get("purchaseAmount"))
        avg_price = to_float(item.get("averagePurchasePrice"))
        if cost_basis is None and avg_price is not None:
            cost_basis = avg_price * quantity
        market_value = to_float(market_value_node.get("amount"))
        unrealized = to_float(pnl_node.get("amount"))
        rate = to_float(pnl_node.get("rate"))
        holdings.append(
            Holding(
                provider="toss",
                account=account,
                symbol=symbol,
                name=item.get("name"),
                market=str(item.get("marketCountry") or market_for(currency, symbol)).upper(),
                exchange=None,
                currency=currency,
                quantity=quantity,
                last_price=to_float(item.get("lastPrice")),
                quantity_decimal=item.get('quantity') if isinstance(item.get('quantity'), str) else None,
                last_price_decimal=str(item['lastPrice']) if isinstance(item.get('lastPrice'), (str, int, float))
                    and not isinstance(item['lastPrice'], bool) else None,
                avg_price=avg_price,
                cost_basis=cost_basis,
                market_value=market_value,
                unrealized_pnl=unrealized,
                # Toss reports `rate` as a fraction (0.1077 = 10.77%).
                unrealized_pnl_pct=round(rate * 100, 4) if rate is not None else _pct(unrealized, cost_basis),
            )
        )
    return holdings


def _execution_zone(value):
    if not isinstance(value, str):
        return None
    try:
        from datetime import datetime
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.tzname() if stamp.tzinfo else None
    except ValueError:
        return None


def _execution_timestamp(value):
    """Preserve an explicitly zoned broker fill instant; no order-time inference."""
    if not isinstance(value, str):
        return None
    try:
        from datetime import datetime, timezone
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.astimezone(timezone.utc).isoformat(timespec="milliseconds") if stamp.tzinfo else None
    except ValueError:
        return None


def toss_transactions(orders: list[dict[str, Any]], account: str) -> list[Transaction]:
    """Normalize Toss orders into BUY/SELL ledger entries.

    Orders with no fill are skipped; partially filled and partially-filled-then-
    cancelled orders contribute their filled quantity.
    """
    transactions: list[Transaction] = []
    for order in orders or []:
        if not isinstance(order, dict):
            continue
        execution = (
            order.get("execution") if isinstance(order.get("execution"), dict) else {}
        )
        quantity = _f(execution.get("filledQuantity"))
        if quantity <= 0:
            continue
        side = str(order.get("side") or "").upper()
        if side not in ("BUY", "SELL"):
            continue
        tx_type = TxType.BUY if side == "BUY" else TxType.SELL
        symbol = str(order.get("symbol") or "").strip().upper()
        currency = str(order.get("currency") or "").upper() or (
            "KRW" if symbol.isdigit() else "USD"
        )
        price = to_float(execution.get("averageFilledPrice"))
        gross = to_float(execution.get("filledAmount"))
        if gross is None and price is not None:
            gross = price * quantity
        fee = _f(execution.get("commission"))
        tax = _f(execution.get("tax"))
        trade_date = iso_date(execution.get("filledAt")) or iso_date(
            order.get("orderedAt")
        )
        if trade_date is None:
            continue
        transactions.append(
            Transaction(
                provider="toss",
                account=account,
                tx_id=str(order.get("orderId") or ""),
                identity_date=iso_date(order.get("orderedAt")) or trade_date,
                execution_time_utc=_execution_timestamp(execution.get("filledAt")),
                execution_source_timezone=_execution_zone(execution.get("filledAt")),
                toss_execution=dict(execution) if getattr(orders, 'fill_contract', None) == 'toss-fills-v1' else None,
                **{
                    k: v
                    for k, v in identity(
                        "toss",
                        account,
                        market_for(currency, symbol),
                        str(order.get("orderId") or ""),
                        iso_date(order.get("orderedAt")) or trade_date,
                    ).items()
                    if k
                    in {
                        "execution_id",
                        "identity_quality",
                        "account_key",
                        "parent_order_id",
                    }
                },
                tx_type=tx_type,
                trade_date=trade_date,
                settle_date=iso_date(execution.get("settlementDate")),
                symbol=symbol,
                name=None,
                market=market_for(currency, symbol),
                exchange=None,
                currency=currency,
                quantity=quantity,
                price=price,
                price_source_field="execution.averageFilledPrice",
                fee_status="unknown"
                if to_float(execution.get("commission")) is None
                else "verified_zero"
                if fee == 0
                else "reported",
                tax_status="unknown"
                if to_float(execution.get("tax")) is None
                else "verified_zero"
                if tax == 0
                else "reported",
                gross_amount=gross,
                fee=fee,
                tax=tax,
                net_amount=_net_amount(tx_type, gross, fee, tax),
                fx_rate=None,
                status=str(order.get("status") or "FILLED"),
                source="toss:orders",
            )
        )
    return transactions


def toss_cash(payload: Any, account: str, currency: str) -> list[CashBalance]:
    """Normalize `GET /api/v1/buying-power` into a CashBalance."""
    result = payload.get("result", payload) if isinstance(payload, dict) else {}
    amount = to_float(result.get("cashBuyingPower")) if isinstance(result, dict) else None
    if amount is None:
        return []
    return [
        CashBalance(
            provider="toss",
            account=account,
            currency=str((result or {}).get("currency") or currency).upper(),
            amount=amount,
            kind="buying_power",
            buying_power=amount,
            amount_decimal=result.get('cashBuyingPower') if isinstance(result.get('cashBuyingPower'), str) else None,
            source_fields={"buying_power": "cashBuyingPower"},
            settlement_basis_reason=("Toss exposes buying power here; whether it includes unsettled "
                                     "trades and the position settlement basis are unknown."),
        )
    ]


# ── KIS normalizers ──────────────────────────────────────


def kis_domestic_holdings(payload: Any, account: str = "kis:domestic") -> list[Holding]:
    """Normalize KIS 국내주식 잔고 (TTTC8434R output1) into Holdings."""
    rows = payload.get("stocks") if isinstance(payload, dict) else None
    holdings: list[Holding] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("pdno") or "").strip()
        quantity = _f(row.get("hldg_qty"))
        if not symbol or quantity <= 0:
            continue
        cost_basis = to_float(row.get("pchs_amt"))
        avg_price = to_float(row.get("pchs_avg_pric"))
        if cost_basis is None and avg_price is not None:
            cost_basis = avg_price * quantity
        unrealized = to_float(row.get("evlu_pfls_amt"))
        holdings.append(
            Holding(
                provider="kis",
                account=account,
                symbol=symbol,
                name=row.get("prdt_name"),
                market="KR",
                exchange="KRX",
                currency="KRW",
                quantity=quantity,
                last_price=to_float(row.get("prpr")),
                avg_price=avg_price,
                cost_basis=cost_basis,
                market_value=to_float(row.get("evlu_amt")),
                unrealized_pnl=unrealized,
                unrealized_pnl_pct=to_float(row.get("evlu_pfls_rt")) or _pct(unrealized, cost_basis),
            )
        )
    return holdings


def kis_overseas_holdings(payload: Any, account: str = "kis:overseas") -> list[Holding]:
    """Normalize KIS 해외주식 잔고 (TTTS3012R output1) into Holdings.

    Rows carry the `market`/`exchange`/`currency` that KISClient.get_overseas_balance
    stamps on each per-market response.
    """
    rows = payload.get("stocks") if isinstance(payload, dict) else None
    holdings: list[Holding] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("ovrs_pdno") or row.get("pdno") or "").strip().upper()
        quantity = _f(row.get("ovrs_cblc_qty"))
        if not symbol or quantity <= 0:
            continue
        currency = str(row.get("currency") or row.get("tr_crcy_cd") or "USD").upper()
        cost_basis = to_float(row.get("frcr_pchs_amt1"))
        avg_price = to_float(row.get("pchs_avg_pric"))
        if cost_basis is None and avg_price is not None:
            cost_basis = avg_price * quantity
        unrealized = to_float(row.get("frcr_evlu_pfls_amt"))
        holdings.append(
            Holding(
                provider="kis",
                account=account,
                symbol=symbol,
                name=row.get("ovrs_item_name") or row.get("prdt_name"),
                market=str(row.get("market") or market_for(currency, symbol)).upper(),
                exchange=str(row.get("exchange") or row.get("ovrs_excg_cd") or "").upper() or None,
                currency=currency,
                quantity=quantity,
                last_price=to_float(row.get("now_pric2")),
                avg_price=avg_price,
                cost_basis=cost_basis,
                market_value=to_float(row.get("ovrs_stck_evlu_amt")),
                unrealized_pnl=unrealized,
                unrealized_pnl_pct=to_float(row.get("evlu_pfls_rt")) or _pct(unrealized, cost_basis),
            )
        )
    return holdings


def kis_domestic_cash(payload: Any, account: str = "kis:domestic") -> list[CashBalance]:
    """Normalize KIS 국내주식 잔고 output2 into a KRW CashBalance."""
    summary = payload.get("summary") if isinstance(payload, dict) else None
    rows = summary if isinstance(summary, list) else [summary] if isinstance(summary, dict) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        amount = to_float(row.get("dnca_tot_amt"))
        if amount is not None:
            return [CashBalance(
                provider="kis", account=account, currency="KRW", amount=amount,
                settled_cash=amount,
                source_fields={"settled_cash": "dnca_tot_amt"},
                settlement_basis_reason=("KIS domestic deposit is separate from future settlement cash; "
                                         "unsettled obligations and position basis are not verified here."),
            )]
    return []


def kis_overseas_cash(payload: Any, account: str = "kis:overseas") -> list[CashBalance]:
    """Normalize CTRP6504R output2 without losing the separate buy/sell totals.

    Reference assumption: deposit excludes the separate trade-date
    buy/sell amounts and positions include executions. These totals lack dated
    settlement detail; the bridge is not official NAV. Official field labels:
    https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/overseas_stock/inquire_present_balance/chk_inquire_present_balance.py
    TTTS3007R sll_ruse_psbl_amt is sell reuse capacity, not another receivable.
    output3 ustl_* totals are account aggregates: retain, never add them again.
    """
    balances: list[CashBalance] = []
    summary = payload.get("summary", []) if isinstance(payload, dict) else []
    rows = [summary] if isinstance(summary, dict) else summary
    reported_totals = {
        key: to_float(row.get(key))
        for row in rows or [] if isinstance(row, dict)
        for key in ("tot_asst_amt", "evlu_amt_smtl_amt", "ustl_sll_amt_smtl", "ustl_buy_amt_smtl")
        if key in row
    }
    for row in (payload.get("cash") if isinstance(payload, dict) else None) or []:
        if not isinstance(row, dict):
            continue
        currency = str(row.get("crcy_cd") or "").upper()
        amount = to_float(row.get("frcr_dncl_amt_2"))
        if not currency or amount is None:
            continue
        balances.append(CashBalance(
            provider="kis", account=account, currency=currency, amount=amount,
            settled_cash=amount,
            unsettled_receivables=to_float(row.get("frcr_sll_amt_smtl")),
            unsettled_payables=to_float(row.get("frcr_buy_amt_smtl")),
            withdrawable_cash=to_float(row.get("frcr_drwg_psbl_amt_1")),
            native_cash_includes_unsettled=False,
            positions_basis="trade_date_reference_assumption",
            settlement_basis_reason=("Reference assumption: deposit excludes separate buy/sell totals; "
                                     "positions include executions. Settlement dates are not supplied "
                                     "in this balance response; reconciliation remains unverified."),
            source_fields={"settled_cash": "CTRP6504R.output2.frcr_dncl_amt_2",
                           "unsettled_receivables": "CTRP6504R.output2.frcr_sll_amt_smtl",
                           "unsettled_payables": "CTRP6504R.output2.frcr_buy_amt_smtl",
                           "withdrawable_cash": "CTRP6504R.output2.frcr_drwg_psbl_amt_1"},
            broker_reported_totals=reported_totals if not balances else {},
        ))
    if not isinstance(payload, dict):
        return balances
    listed = {str(row.get("crcy_cd") or "").upper() for row in payload.get("cash") or [] if isinstance(row, dict)}
    held = {str(row.get("currency") or "").upper() for row in payload.get("stocks") or []
            if isinstance(row, dict) and _f(row.get("ovrs_cblc_qty")) > 0}
    missing = sorted(held - listed - {"", "KRW"})
    if missing and _kis_no_row_cash_is_zero(payload):
        for currency in missing:
            balances.append(CashBalance(
                provider="kis", account=account, currency=currency, amount=0.0, settled_cash=0.0,
                unsettled_receivables=0.0, unsettled_payables=0.0,
                native_cash_includes_unsettled=False,
                positions_basis="trade_date_reference_assumption",
                settlement_basis_reason=("KIS_NO_CASH_ROW_PROVEN_ZERO: CTRP6504R lists no cash row for this "
                                         "currency; KIS's KRW foreign-cash total equals the listed rows within "
                                         "1 bp and no trade is unsettled, so its cash is 0."),
                source_fields={"settled_cash_derivation": "KIS_NO_CASH_ROW_PROVEN_ZERO",
                               "check": "CTRP6504R(01).output3.frcr_evlu_tota"},
            ))
    return balances


_KIS_RATE_UNITS = {"JPY": 100}  # CTRP6504R bass_exrt quotes JPY per 100


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _kis_no_row_cash_is_zero(payload: dict[str, Any]) -> bool:
    """True only when a currency missing from CTRP6504R output2 provably holds no cash.

    The basis-01 read (``cash_check_krw``) must list the same foreign currencies, show
    no unsettled trade (output3 ``ustl_*`` and every row's buy/sell totals zero), and
    its KRW foreign-cash total ``frcr_evlu_tota`` must equal the listed deposits
    (``frcr_dncl_amt_2``) at KIS's own rates within 1 bp + 1 KRW per row (+1), the
    kis_nav_capture tolerance. Any deposit in an unlisted currency would be in that
    total. Anything missing or inconsistent: False (the cash stays unknown).
    """
    check = payload.get("cash_check_krw")
    if not isinstance(check, dict) or check.get("error") or not isinstance(check.get("output3"), dict):
        return False
    rows = {str(row.get("crcy_cd") or "").upper(): row for row in payload.get("cash") or [] if isinstance(row, dict)}
    rows.pop("KRW", None)
    krw_basis = {str(row.get("crcy_cd") or "").upper() for row in check.get("output2") or []} - {"KRW"}
    totals = check["output3"]
    unsettled = [totals.get("ustl_sll_amt_smtl"), totals.get("ustl_buy_amt_smtl")] + [
        row.get(key) for row in rows.values() for key in ("frcr_buy_amt_smtl", "frcr_sll_amt_smtl")]
    if krw_basis != set(rows) or any(_decimal(value) != 0 for value in unsettled):
        return False
    rates: dict[str, set[Decimal]] = {}
    for row in check.get("output1") or []:
        if (_decimal(row.get("ccld_qty_smtl1")) or 0) > 0:
            rates.setdefault(str(row.get("buy_crcy_cd") or "").upper(), set()).add(_decimal(row.get("bass_exrt")))
    value = Decimal(0)
    for currency, row in rows.items():
        deposit = _decimal(row.get("frcr_dncl_amt_2"))
        if deposit is None:
            return False
        if deposit == 0:
            continue
        unit = _KIS_RATE_UNITS.get(currency, 1)
        held_rates = rates.get(currency, set())
        posted = _decimal(row.get("frst_bltn_exrt"))
        if len(held_rates) == 1 and None not in held_rates:
            rate = next(iter(held_rates)) / unit
        elif unit == 1 and posted is not None and posted > 0:
            rate = posted
        else:
            return False
        value += deposit * rate
    reported = _decimal(totals.get("frcr_evlu_tota"))
    if reported is None:
        return False
    return abs(value - reported) <= Decimal(len(rows) + 1) + reported / 10000


def kis_domestic_transactions(
    payload: Any, account: str = "kis:domestic"
) -> list[Transaction]:
    """Normalize KIS 국내 일별주문체결 (TTTC0081R output1) into ledger entries.

    KIS reports only an estimated total cost (`prsm_tlex_smtl`, 추정제비용합계) for
    domestic fills and does not split commission from tax, so the whole amount is
    recorded as `fee` with `tax=0`.
    """
    rows = payload.get("trades") if isinstance(payload, dict) else None
    transactions: list[Transaction] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        quantity = _f(row.get("tot_ccld_qty"))
        side = _KIS_SIDE.get(str(row.get("sll_buy_dvsn_cd") or "").strip())
        trade_date = iso_date(row.get("ord_dt"))
        if quantity <= 0 or side is None or trade_date is None:
            continue
        tx_type = TxType[side]
        gross = to_float(row.get("tot_ccld_amt"))
        price = to_float(row.get("avg_prvs"))
        if price is None and gross is not None and quantity:
            price = gross / quantity
        if gross is None and price is not None:
            gross = price * quantity
        fee = _f(row.get("prsm_tlex_smtl"))
        transactions.append(
            Transaction(
                provider="kis",
                account=account,
                tx_id=str(row.get("odno") or ""),
                **{
                    k: v
                    for k, v in identity(
                        "kis",
                        legacy_kis_account_key(account),
                        "KR",
                        str(row.get("odno") or ""),
                        trade_date,
                    ).items()
                    if k
                    in {
                        "execution_id",
                        "identity_quality",
                        "account_key",
                        "parent_order_id",
                    }
                },
                tx_type=tx_type,
                trade_date=trade_date,
                settle_date=None,
                symbol=str(row.get("pdno") or "").strip(),
                name=row.get("prdt_name"),
                market="KR",
                exchange=str(row.get("excg_dvsn_cd") or "KRX").upper(),
                currency="KRW",
                quantity=quantity,
                price=price,
                price_source_field="avg_prvs"
                if to_float(row.get("avg_prvs")) is not None
                else "tot_ccld_amt/tot_ccld_qty",
                fee_status="unknown"
                if to_float(row.get("prsm_tlex_smtl")) is None
                else "verified_zero"
                if fee == 0
                else "reported",
                tax_status="unsupported",
                gross_amount=gross,
                fee=fee,
                tax=0.0,
                net_amount=_net_amount(tx_type, gross, fee, 0.0),
                fx_rate=1.0,
                status="FILLED",
                source="kis:inquire-daily-ccld",
                note="fee is KIS estimated total cost (제비용); commission/tax not split",
            )
        )
    return transactions


def kis_overseas_transactions(
    payload: Any, account: str = "kis:overseas"
) -> list[Transaction]:
    """Normalize KIS 해외주식 일별거래내역 (CTOS4001R output1) into ledger entries.

    Preferred overseas source: unlike inquire-ccnl it reports settlement date,
    both fee columns, and the FX rate registered for the trade.
    """
    rows = payload.get("transactions") if isinstance(payload, dict) else None
    transactions: list[Transaction] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        quantity = _f(row.get("ccld_qty"))
        side = _KIS_SIDE.get(str(row.get("sll_buy_dvsn_cd") or "").strip())
        trade_date = iso_date(row.get("trad_dt"))
        if quantity <= 0 or side is None or trade_date is None:
            continue
        tx_type = TxType[side]
        symbol = str(row.get("pdno") or "").strip().upper()
        currency = str(row.get("crcy_cd") or "USD").upper()
        # No field-level currency contract exists locally for
        # ovrs_stck_ccld_unpr. AMZN evidence is KRW, so never consume that
        # ambiguous column as native. Prefer explicit foreign price, else
        # derive from reported foreign gross for these spot-stock fills.
        price = to_float(row.get("ft_ccld_unpr2"))
        price_source = "ft_ccld_unpr2" if price is not None else None
        gross = to_float(row.get("tr_frcr_amt2"))
        if price is None and gross is not None:
            price = float(Decimal(str(gross)) / Decimal(str(quantity)))
            price_source = "tr_frcr_amt2/ccld_qty"
        if gross is None and price is not None:
            gross = float(Decimal(str(price)) * Decimal(str(quantity)))
        # Both foreign-currency fee columns are reported; tax is unsupported.
        fee = _f(row.get("frcr_fee1")) + _f(row.get("dmst_frcr_fee1"))
        transactions.append(
            Transaction(
                provider="kis",
                account=account,
                tx_id=f"{trade_date}:{symbol}:{side}:{row.get('sttl_dt') or ''}:{quantity:g}",
                tx_type=tx_type,
                trade_date=trade_date,
                settle_date=iso_date(row.get("sttl_dt")),
                symbol=symbol,
                name=row.get("ovrs_item_name"),
                market=str(row.get("market") or market_for(currency, symbol)).upper(),
                exchange=str(row.get("ovrs_excg_cd") or "").upper() or None,
                currency=currency,
                quantity=quantity,
                price=price,
                price_source_field=price_source,
                fee_status="unknown"
                if any(
                    to_float(row.get(k)) is None
                    for k in ("frcr_fee1", "dmst_frcr_fee1")
                )
                else "verified_zero"
                if fee == 0
                else "reported",
                tax_status="unsupported",
                gross_amount=gross,
                fee=fee,
                tax=0.0,
                net_amount=_net_amount(tx_type, gross, fee, 0.0),
                fx_rate=to_float(row.get("erlm_exrt")),
                status="FILLED",
                source="kis:inquire-period-trans",
            )
        )
    return transactions


def kis_overseas_ccnl_transactions(
    payload: Any, account: str = "kis:overseas"
) -> list[Transaction]:
    """Normalize KIS 해외주식 주문체결내역 (TTTS3035R output) into ledger entries.

    Fallback for when 일별거래내역 is unavailable: order-level detail, but KIS
    reports no fees here, so `fee`/`tax` are 0 and the note flags it.
    """
    rows = payload.get("trades") if isinstance(payload, dict) else None
    transactions: list[Transaction] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        quantity = _f(row.get("ft_ccld_qty"))
        side = _KIS_SIDE.get(str(row.get("sll_buy_dvsn_cd") or "").strip())
        trade_date = iso_date(row.get("ord_dt"))
        if quantity <= 0 or side is None or trade_date is None:
            continue
        tx_type = TxType[side]
        symbol = str(row.get("pdno") or "").strip().upper()
        currency = str(row.get("currency") or row.get("tr_crcy_cd") or "USD").upper()
        price = to_float(row.get("ft_ccld_unpr3"))
        gross = to_float(row.get("ft_ccld_amt3"))
        if gross is None and price is not None:
            gross = price * quantity
        transactions.append(
            Transaction(
                provider="kis",
                account=account,
                tx_id=str(row.get("odno") or ""),
                **{
                    k: v
                    for k, v in identity(
                        "kis",
                        legacy_kis_account_key(account),
                        str(row.get("market") or market_for(currency, symbol)),
                        str(row.get("odno") or ""),
                        trade_date,
                    ).items()
                    if k
                    in {
                        "execution_id",
                        "identity_quality",
                        "account_key",
                        "parent_order_id",
                    }
                },
                tx_type=tx_type,
                trade_date=trade_date,
                settle_date=None,
                symbol=symbol,
                name=row.get("prdt_name"),
                market=str(row.get("market") or market_for(currency, symbol)).upper(),
                exchange=str(
                    row.get("exchange") or row.get("ovrs_excg_cd") or ""
                ).upper()
                or None,
                currency=currency,
                quantity=quantity,
                price=price,
                price_source_field="ft_ccld_unpr3",
                fee_status="unsupported",
                tax_status="unsupported",
                gross_amount=gross,
                fee=0.0,
                tax=0.0,
                net_amount=_net_amount(tx_type, gross, 0.0, 0.0),
                fx_rate=None,
                status=str(row.get("prcs_stat_name") or "FILLED"),
                source="kis:inquire-ccnl",
                note="fees not reported by inquire-ccnl; net_amount excludes costs",
            )
        )
    return transactions


def _net_amount(tx_type: TxType, gross: float | None, fee: float, tax: float) -> float | None:
    """Signed cash effect: buys consume cash, sells return it net of costs."""
    if gross is None:
        return None
    if tx_type is TxType.BUY:
        return -(gross + fee + tax)
    if tx_type is TxType.SELL:
        return gross - fee - tax
    return gross
