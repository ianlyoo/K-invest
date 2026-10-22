#!/usr/bin/env python3
"""Deterministic portfolio accounting and performance math.

Every number the MCP tools report is computed here, not by the caller. Pure
functions over the normalized model in portfolio_model — no network, no clients.

Accounting basis:
  - **Cost basis: moving average** (이동평균법), matching Korean broker convention
    so figures reconcile with KIS `pchs_avg_pric` and Toss `averagePurchasePrice`.
  - Buy fees and taxes are capitalized into cost; sell fees and taxes reduce
    proceeds. Realized P&L on a sale = proceeds - (avg cost x quantity sold).
  - Cost basis is tracked per (account, symbol, currency). Combined positions
    sum account quantities and remaining costs; their average is cost / quantity.
  - FX: valuation uses the current rate in FxRates (fx.policy: Toss broker rate,
    yfinance labelled fallback); a transaction's own `fx_rate` is used for
    KRW-denominated realized P&L when the broker reported one.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable, Sequence

from kinvest_common import local_calendar_date
from portfolio_model import (
    PERP_MAP,
    PRODUCT_MAP,
    PRODUCT_MAP_VERSION,
    UNDERLYING_NAMES,
    CashBalance,
    FxRates,
    Holding,
    Transaction,
    TxType,
)

_DAYS_PER_YEAR = 365.0
PERIODS = ("MTD", "YTD", "1M", "3M", "6M", "1Y", "ALL")
#: Anything earlier than this is treated as "all history" for XIRR/period bounds.
_EPOCH = "2000-01-01"


# ── Period helpers ───────────────────────────────────────


def resolve_period(period: str, today: date | None = None) -> tuple[str, str]:
    """Map a period label to an inclusive (start, end) ISO date range."""
    ref = today or date.today()
    end = ref.isoformat()
    label = (period or "").strip().upper()
    if label == "MTD":
        return ref.replace(day=1).isoformat(), end
    if label == "YTD":
        return ref.replace(month=1, day=1).isoformat(), end
    if label == "ALL":
        return _EPOCH, end
    days = {"1M": 30, "3M": 90, "6M": 182, "1Y": 365}.get(label)
    if days is None:
        raise ValueError(f"unsupported period {period!r} (expected one of {', '.join(PERIODS)})")
    return (ref - timedelta(days=days)).isoformat(), end


def _in_range(value: str | None, start: str, end: str) -> bool:
    return bool(value) and start <= value <= end  # type: ignore[operator]


# ── Consolidated holdings ────────────────────────────────

#: One canonical mark per (symbol, currency); every account's quantity is valued at it.
MARK_POLICY = {
    "name": "latest_timed_quote_v1",
    "rule": "latest timestamped quote (regular or extended session: KRX/NXT, US pre/regular/post/overnight); "
            "otherwise the broker price of the account holding the most quantity, labelled unknown_time",
    "krx_close": "KIS balance prices carry no time, so a KRX close is used only as the unknown_time fallback",
    "session_basis": "price_time_clock: the session is read from the quote's local exchange clock time",
    "market_value": "mark.price x quantity; broker_reported_market_value keeps the broker's own valuation",
}

_SESSION_CLOCKS = {
    # currency -> (zone, [(start, end_inclusive, session)]) on the exchange's local clock
    "KRW": ("Asia/Seoul", [("08:00:00", "08:59:59", "nxt_pre"), ("09:00:00", "15:30:00", "krx_regular"),
                           ("15:30:01", "20:00:00", "nxt_after")]),
    "USD": ("America/New_York", [("04:00:00", "09:29:59", "us_pre"), ("09:30:00", "16:00:00", "us_regular"),
                                 ("16:00:01", "20:00:00", "us_post"), ("20:00:01", "23:59:59", "us_overnight"),
                                 ("00:00:00", "03:59:59", "us_overnight")]),
    "JPY": ("Asia/Tokyo", [("09:00:00", "15:30:00", "tse_regular")]),
}


def _parse_instant(value: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.utcoffset() is not None else None


def mark_session(currency: str, priced_at: str | None) -> str | None:
    """Trading session of a price from its own timestamp; None when unknown."""
    from zoneinfo import ZoneInfo

    stamp = _parse_instant(priced_at)
    clock = _SESSION_CLOCKS.get((currency or "").upper())
    if stamp is None or clock is None:
        return None
    local = stamp.astimezone(ZoneInfo(clock[0]))
    hms, weekday = local.strftime("%H:%M:%S"), local.weekday()
    session = next((name for start, end, name in clock[1] if start <= hms <= end), "closed")
    # The US overnight session runs Sunday through Thursday nights only.
    overnight_open = session == "us_overnight" and (weekday in (0, 1, 2, 3) or (weekday in (4,) and hms < "04:00:00")
                                                   or (weekday == 6 and hms > "20:00:00"))
    if session == "us_overnight":
        return session if overnight_open else "closed"
    return session if weekday < 5 else "closed"


def _canonical_mark(symbol: str, currency: str, group: Sequence[Holding],
                    quotes: dict[str, Any] | None) -> dict[str, Any]:
    """Pick the one price every account's quantity of this instrument is valued at."""
    reasons: list[str] = []
    candidates = []
    quote = (quotes or {}).get(symbol) if quotes is not None else None
    if quote is not None:
        price = quote.get("price")
        stamp = _parse_instant(quote.get("priced_at"))
        if quote.get("reason_codes"):
            reasons.extend(quote["reason_codes"])
        elif (quote.get("currency") or "").upper() != currency:
            reasons.append("QUOTE_CURRENCY_MISMATCH")
        elif not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            reasons.append("INVALID_QUOTE_PRICE")
        elif stamp is None:
            reasons.append("UNKNOWN_QUOTE_TIME")
        else:
            candidates.append((stamp, float(price), quote.get("source") or "quote", quote.get("priced_at")))
    elif quotes is not None:
        reasons.append("MISSING_QUOTE")
    if candidates:
        _, price, source, priced_at = max(candidates, key=lambda c: c[0])
        return {"price": price, "currency": currency, "source": source,
                "session": mark_session(currency, priced_at), "priced_at": priced_at,
                "status": "timed", "reason_codes": []}
    priced = [h for h in group if h.last_price is not None and math.isfinite(h.last_price) and h.last_price > 0]
    if not priced:
        return {"price": None, "currency": currency, "source": None, "session": None, "priced_at": None,
                "status": "unavailable", "reason_codes": list(dict.fromkeys([*reasons, "MISSING_MARK_PRICE"]))}
    chosen = max(priced, key=lambda h: (h.quantity, h.provider == "toss", h.account))
    prices = [h.last_price for h in priced]
    spread_bp = round((max(prices) - min(prices)) / min(prices) * 10000, 2)
    reasons = [*reasons, "UNKNOWN_QUOTE_TIME"] + (["BROKER_PRICES_DIFFER"] if spread_bp else [])
    return {"price": float(chosen.last_price), "currency": currency, "source": f"broker:{chosen.account}",
            "session": None, "priced_at": None, "status": "unknown_time",
            "broker_price_spread_bp": spread_bp, "reason_codes": list(dict.fromkeys(reasons))}


def consolidate_holdings(
    holdings: Sequence[Holding],
    fx: FxRates,
    realized: dict[str, Any] | None = None,
    quotes: dict[str, Any] | None = None,
    perps: dict[str, Any] | None = None,
    perp_fx: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate per-account holdings into one row per (symbol, currency).

    The average price is quantity-weighted through summed cost basis, which is
    exact rather than an average of averages. Every row is valued at one
    canonical mark (MARK_POLICY): market_value = mark.price x quantity, for the
    row and for each account; the broker's own figures stay in
    broker_reported_market_value. `quotes` maps a symbol to a validated timed
    quote ({price, currency, priced_at, source} or {reason_codes}); without one
    the mark is a broker price labelled unknown_time. `perps` (a crypto futures
    positions result) and `perp_fx` (stablecoin KRW quotes) feed only
    underlying_exposure, never totals or weights.
    """
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for holding in holdings:
        if holding.quantity <= 0:
            continue
        key = (holding.symbol.upper(), holding.currency.upper())
        entry = grouped.setdefault(
            key,
            {
                "symbol": holding.symbol.upper(),
                "name": holding.name,
                "market": holding.market,
                "exchange": holding.exchange,
                "currency": holding.currency.upper(),
                "quantity": 0.0,
                "cost_basis": 0.0,
                "market_value": 0.0,
                "last_price": holding.last_price,
                "accounts": [],
                "_holdings": [],
                "_cost_known": True,
                "_value_known": True,
            },
        )
        entry["_holdings"].append(holding)
        entry["quantity"] += holding.quantity
        if holding.cost_basis is None:
            entry["_cost_known"] = False
        else:
            entry["cost_basis"] += holding.cost_basis
        if holding.market_value is None:
            entry["_value_known"] = False
        else:
            entry["market_value"] += holding.market_value
        if entry["last_price"] is None:
            entry["last_price"] = holding.last_price
        if entry["name"] is None:
            entry["name"] = holding.name
        broker_basis = holding.to_dict()["broker_reported_cost_basis"]
        ledger_basis = holding.to_dict()["ledger_cost_basis"]
        book = None
        if realized is not None:
            book = (
                realized.get("open_positions_by_account", {})
                .get(holding.account, {})
                .get(f"{holding.symbol.upper()}:{holding.currency.upper()}")
            )
            if book is None or abs(book["quantity"] - holding.quantity) > 1e-8:
                ledger_basis["reason"] = "account_or_quantity_mismatch"
                book = None
            else:
                ledger_basis = dict(book["ledger_cost_basis"])
        entry["accounts"].append(
            {
                "account": holding.account,
                "provider": holding.provider,
                "quantity": holding.quantity,
                "avg_price": holding.avg_price,
                "cost_basis": holding.cost_basis,
                "market_value": holding.market_value,
                "broker_reported_cost_basis": broker_basis,
                "ledger_cost_basis": ledger_basis,
                "_ledger_book": book,
            }
        )

    positions: list[dict[str, Any]] = []
    for entry in grouped.values():
        quantity = entry["quantity"]
        # A partially-unknown group must report None rather than a short total.
        cost_basis = entry["cost_basis"] if entry.pop("_cost_known") else None
        broker_value = entry["market_value"] if entry.pop("_value_known") else None
        mark = _canonical_mark(entry["symbol"], entry["currency"], entry.pop("_holdings"), quotes)
        if mark["price"] is not None:
            market_value = mark["price"] * quantity
            entry["last_price"] = mark["price"]
            for account in entry["accounts"]:
                account["broker_reported_market_value"] = account["market_value"]
                account["market_value"] = mark["price"] * account["quantity"]
        else:
            market_value = broker_value
            for account in entry["accounts"]:
                account["broker_reported_market_value"] = account["market_value"]
        unrealized = (
            None
            if (market_value is None or cost_basis is None)
            else market_value - cost_basis
        )
        accounts = entry["accounts"]
        ledger_known = all(
            a["ledger_cost_basis"]["amount"] is not None for a in accounts
        )
        ledger_cost = (
            sum(a["ledger_cost_basis"]["amount"] for a in accounts)
            if ledger_known
            else None
        )

        def inclusion(field: str, basis: str) -> bool | None:
            values = [a[basis][field] for a in accounts]
            return values[0] if values and all(v == values[0] for v in values) else None

        broker_basis = {
            "amount": cost_basis,
            "currency": entry["currency"],
            "includes_buy_fees": inclusion(
                "includes_buy_fees", "broker_reported_cost_basis"
            ),
            "includes_buy_taxes": inclusion(
                "includes_buy_taxes", "broker_reported_cost_basis"
            ),
            "basis_source": "broker_reported",
        }
        ledger_basis = {
            "amount": ledger_cost,
            "currency": entry["currency"],
            "includes_buy_fees": inclusion("includes_buy_fees", "ledger_cost_basis"),
            "includes_buy_taxes": inclusion("includes_buy_taxes", "ledger_cost_basis"),
            "basis_source": "ledger_moving_average",
            "basis_scope": "per_account",
            "status": "unknown"
            if not ledger_known
            else "reported"
            if all(a["ledger_cost_basis"].get("status") == "reported" for a in accounts)
            else "partial",
            "reason": None
            if ledger_known
            and all(a["ledger_cost_basis"].get("reason") is None for a in accounts)
            else "unreported_buy_costs"
            if ledger_known
            else next(
                a["ledger_cost_basis"].get("reason")
                for a in accounts
                if a["ledger_cost_basis"]["amount"] is None
            ),
        }
        difference = (
            None
            if ledger_cost is None or cost_basis is None
            else round(ledger_cost - cost_basis, 4)
        )
        # Call a break fee basis only when every account's broker cost matches
        # ledger principal and the retained reported buy costs explain the gap.
        fee_basis = (
            ledger_known
            and difference is not None
            and all(
                a["cost_basis"] is not None
                and a["_ledger_book"]["gross_cost"] is not None
                and abs(a["cost_basis"] - a["_ledger_book"]["gross_cost"]) <= 0.0001
                and abs(
                    a["ledger_cost_basis"]["amount"]
                    - a["cost_basis"]
                    - a["_ledger_book"]["capitalized_buy_fees"]
                    - a["_ledger_book"]["capitalized_buy_taxes"]
                )
                <= 0.0001
                for a in accounts
            )
        )
        for account in accounts:
            account.pop("_ledger_book")
        entry.update(
            {
                "quantity": round(quantity, 8),
                "avg_price": (cost_basis / quantity)
                if (cost_basis and quantity)
                else None,
                "cost_basis": cost_basis,
                "broker_reported_cost_basis": broker_basis,
                "ledger_cost_basis": ledger_basis,
                "cost_basis_difference": {
                    "amount": difference,
                    "currency": entry["currency"],
                    "reason": "fee_basis"
                    if fee_basis and difference
                    else "unexplained"
                    if difference
                    else "matched"
                    if difference == 0
                    else "ledger_unavailable",
                    "evidence": "broker basis equals ledger principal; difference equals retained reported buy costs"
                    if fee_basis and difference
                    else None,
                },
                "ledger_unrealized_pnl": None
                if market_value is None or ledger_cost is None
                else market_value - ledger_cost,
                "market_value": market_value,
                "mark": mark,
                "market_value_basis": "canonical_mark" if mark["price"] is not None else "broker_reported",
                "broker_reported_market_value": broker_value,
                "broker_reported_market_value_krw": fx.to_krw(broker_value, entry["currency"]),
                "unrealized_pnl": unrealized,
                "unrealized_pnl_pct": (
                    round(unrealized / cost_basis * 100, 4)
                    if (unrealized is not None and cost_basis)
                    else None
                ),
                "cost_basis_krw": fx.to_krw(cost_basis, entry["currency"]),
                "market_value_krw": fx.to_krw(market_value, entry["currency"]),
                "unrealized_pnl_krw": fx.to_krw(unrealized, entry["currency"]),
                "account_count": len(entry["accounts"]),
            }
        )
        positions.append(entry)

    positions.sort(key=lambda p: p["market_value_krw"] or 0.0, reverse=True)
    total_value_krw = _sum(p["market_value_krw"] for p in positions)
    for position in positions:
        value = position["market_value_krw"]
        position["weight_pct"] = (
            round(value / total_value_krw * 100, 4)
            if (value is not None and total_value_krw)
            else None
        )

    status_counts: dict[str, int] = {}
    for position in positions:
        status_counts[position["mark"]["status"]] = status_counts.get(position["mark"]["status"], 0) + 1
    return {
        "positions": positions,
        "methodology": {"basis_scope": "per_account"},
        "position_count": len(positions),
        "mark_policy": MARK_POLICY,
        "mark_status_counts": dict(sorted(status_counts.items())),
        "totals": {
            "cost_basis_krw": _sum(p["cost_basis_krw"] for p in positions),
            "market_value_krw": total_value_krw,
            "broker_reported_market_value_krw": _sum(p["broker_reported_market_value_krw"] for p in positions),
            "unrealized_pnl_krw": _sum(p["unrealized_pnl_krw"] for p in positions),
        },
        "by_currency": _group_totals(positions, "currency"),
        "by_market": _group_totals(positions, "market"),
        "underlying_exposure": underlying_exposure(positions, total_value_krw, perps, perp_fx),
        "fx": fx.to_dict(),
    }


# Look-through exposure per underlying, stated once per response.
UNDERLYING_EXPOSURE_METHOD = {
    "formula": "per underlying: sum of market_value_krw x leverage_target over mapped holdings, plus "
               "signed perp notional x stablecoin KRW rate",
    "leverage": "daily-reset products count at today's daily target multiple: a one-day exposure, "
                "not a holding-period multiple",
    "perps": "crypto futures positions at signed notional (long +, short -), exposure only: never in NAV, "
             "totals or weights; KRW via the Upbit stablecoin trade price crypto NAV uses",
    "pct_basis": "exposure_pct = exposure_krw / consolidated holdings market_value_krw x 100 (cash and perps "
                 "are not in the base)",
    "mapping": "product map from KINVEST_PRODUCT_MAP_FILE (source and date per product); common shares are "
               "their own underlying at 1x; Hyperliquid main-dex and Binance USD-M perps reference their base "
               "crypto asset",
    "unmapped": "instruments absent from the map are listed in unmapped with NOT_IN_PRODUCT_MAP, never "
                "assigned an underlying",
}
_PERP_VENUE_OK = {"ok", "empty", "not_configured"}


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (ArithmeticError, ValueError, TypeError):
        return None
    return number if number.is_finite() else None


def _perp_underlying(venue: str, row: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    symbol, asset = row.get("symbol") or "", row.get("asset") or ""
    entry = PERP_MAP.get(f"{venue}:{symbol}")
    if entry is not None:
        return entry["underlying"], entry
    if venue == "hyperliquid" and not row.get("dex") and symbol and ":" not in symbol:
        return f"crypto:{symbol}", None  # a main-dex perp references the coin itself
    if venue == "binance" and asset and symbol.endswith(asset) and len(symbol) > len(asset):
        return f"crypto:{symbol[:-len(asset)]}", None  # USD-M symbol = base asset + margin asset
    return None, None


def _perp_leg(venue: str, row: dict[str, Any], perp_fx: dict[str, Any], entry: dict[str, Any] | None) -> dict[str, Any]:
    notional, side, asset = _decimal_or_none(row.get("notional")), row.get("side"), row.get("asset")
    signed = abs(notional) * (1 if side == "long" else -1) if notional is not None and side in ("long", "short") else None
    quote = perp_fx.get(asset) if asset else None
    rate = _decimal_or_none((quote or {}).get("rate_to_krw"))
    rate = rate if rate is not None and rate > 0 else None
    reasons = ([] if notional is not None else ["PERP_NOTIONAL_UNAVAILABLE"]) + \
              ([] if side in ("long", "short") else ["SIDE_UNAVAILABLE"]) + \
              ([] if rate is not None else ["STABLECOIN_KRW_RATE_UNAVAILABLE"])
    return {"symbol": row.get("symbol"), "venue": venue, "kind": entry["kind"] if entry else "crypto_perp",
            "side": side, "notional": str(signed) if signed is not None else None, "notional_asset": asset,
            "rate_to_krw": str(rate) if rate is not None else None,
            "exposure_krw": float(round(signed * rate, 2)) if signed is not None and rate is not None else None,
            "account_id": row.get("account_id"), "reason_codes": reasons}


def underlying_exposure(
    positions: Sequence[dict[str, Any]],
    holdings_value_krw: float | None,
    perps: dict[str, Any] | None = None,
    perp_fx: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Exposure per underlying: mapped holdings at market value x leverage, perps at notional.

    Rows come from consolidate_holdings; nothing here feeds NAV, totals or weights. A leg
    without a KRW value nulls its underlying's total with a reason (never a partial sum).
    """
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unmapped: list[dict[str, Any]] = []
    products: dict[str, dict[str, Any]] = {}
    mapped_value = unmapped_value = 0.0
    for position in positions:
        key = f"{(position.get('market') or '').upper()}:{position['symbol']}"
        entry = PRODUCT_MAP.get(key)
        value = position.get("market_value_krw")
        if entry is None:
            unmapped_value += value or 0.0
            unmapped.append({"symbol": position["symbol"], "market": position.get("market"),
                             "name": position.get("name"), "kind": "holding", "weight_pct": position.get("weight_pct"),
                             "reason_codes": ["NOT_IN_PRODUCT_MAP"]})
            continue
        mapped_value += value or 0.0
        if entry["kind"] != "common_stock":
            products[key] = entry
        groups[entry["underlying"]].append({
            "symbol": position["symbol"], "market": position.get("market"), "kind": entry["kind"],
            "leverage_target": entry["leverage_target"], "reset": entry["reset"],
            "market_value_krw": round(value, 2) if value is not None else None,
            "exposure_krw": round(value * entry["leverage_target"], 2) if value is not None else None,
            "reason_codes": [] if value is not None else ["MARKET_VALUE_KRW_UNAVAILABLE"]})

    if perps is None:
        perp_block: dict[str, Any] = {"status": "not_read", "reason_codes": ["PERPS_NOT_READ"]}
    else:
        venues, legs, conversion = {}, 0, {}
        for venue, result in sorted((perps.get("venues") or {}).items()):
            rows = result.get("positions") or []
            venues[venue] = {"source_status": result.get("source_status"), "position_count": len(rows),
                             "reason_codes": result.get("reason_codes") or [], "cache": result.get("cache")}
            for row in rows:
                underlying, entry = _perp_underlying(venue, row)
                leg = _perp_leg(venue, row, perp_fx or {}, entry)
                if underlying is None:
                    unmapped.append({"symbol": row.get("symbol"), "venue": venue, "kind": "perp",
                                     "reason_codes": ["NOT_IN_PRODUCT_MAP"]})
                    continue
                legs += 1
                if entry is not None:
                    products[f"{venue}:{row.get('symbol')}"] = entry
                if leg["rate_to_krw"] is not None:
                    quote = perp_fx[leg["notional_asset"]]
                    conversion[leg["notional_asset"]] = {"rate_to_krw": leg["rate_to_krw"], "source": quote.get("source"),
                                                         "priced_at": quote.get("priced_at")}
                groups[underlying].append(leg)
        reasons = sorted({code for row in venues.values() for code in row["reason_codes"]} |
                         set(perps.get("reason_codes") or []) |
                         ({"PERPS_SOURCE_INCOMPLETE"} if not venues or any(
                             row["source_status"] not in _PERP_VENUE_OK for row in venues.values()) else set()))
        unconverted = any(leg.get("venue") and leg["exposure_krw"] is None for rows in groups.values() for leg in rows)
        perp_block = {"status": "partial" if "PERPS_SOURCE_INCOMPLETE" in reasons or unconverted else "complete",
                      "venues": venues, "leg_count": legs, "conversion": dict(sorted(conversion.items())),
                      "reason_codes": reasons + (["PERP_LEG_UNCONVERTED"] if unconverted else [])}

    by_underlying = []
    for underlying, legs in groups.items():
        known = all(leg["exposure_krw"] is not None for leg in legs)
        total = round(sum(leg["exposure_krw"] for leg in legs), 2) if known else None
        name = UNDERLYING_NAMES.get(underlying) or (PRODUCT_MAP.get(underlying) or {}).get("name") \
            or underlying.split(":", 1)[-1]
        by_underlying.append({
            "underlying": underlying, "name": name, "exposure_krw": total,
            "exposure_pct": round(total / holdings_value_krw * 100, 4) if total is not None and holdings_value_krw else None,
            "leg_count": len(legs), "legs": legs, "reason_codes": [] if known else ["LEG_EXPOSURE_UNAVAILABLE"]})
    by_underlying.sort(key=lambda row: (row["exposure_krw"] is None, -abs(row["exposure_krw"] or 0.0), row["underlying"]))
    return {
        "method": UNDERLYING_EXPOSURE_METHOD,
        "map_version": PRODUCT_MAP_VERSION,
        "by_underlying": by_underlying,
        "unmapped": unmapped,
        "coverage": {"mapped_holding_weight_pct": round(mapped_value / holdings_value_krw * 100, 4)
                     if holdings_value_krw else None,
                     "unmapped_holding_weight_pct": round(unmapped_value / holdings_value_krw * 100, 4)
                     if holdings_value_krw else None,
                     "unmapped_count": len(unmapped)},
        "perps": perp_block,
        "products": {key: {field: entry.get(field) for field in
                           ("name", "issuer", "kind", "underlying", "leverage_target", "reset", "source")}
                     for key, entry in sorted(products.items())},
    }


def _group_totals(positions: Sequence[dict[str, Any]], key: str) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    for position in positions:
        name = str(position.get(key) or "UNKNOWN")
        group = groups.setdefault(
            name, {"position_count": 0, "market_value_krw": 0.0, "cost_basis_krw": 0.0, "unrealized_pnl_krw": 0.0}
        )
        group["position_count"] += 1
        for field in ("market_value_krw", "cost_basis_krw", "unrealized_pnl_krw"):
            value = position.get(field)
            if value is not None:
                group[field] += value
    total = sum(g["market_value_krw"] for g in groups.values())
    for group in groups.values():
        group["weight_pct"] = round(group["market_value_krw"] / total * 100, 4) if total else None
        for field in ("market_value_krw", "cost_basis_krw", "unrealized_pnl_krw"):
            group[field] = round(group[field], 2)
    return dict(sorted(groups.items(), key=lambda kv: kv[1]["market_value_krw"], reverse=True))


def _risk_level(top1: float | None, hhi: float | None) -> str | None:
    if top1 is None and hhi is None:
        return None  # unmeasured concentration is not "low"
    return ("high" if (top1 or 0) >= 35 or (hhi or 0) >= 0.25
            else "medium" if (top1 or 0) >= 20 or (hhi or 0) >= 0.15 else "low")


def concentration_risk(consolidated: dict[str, Any], top_n: int = 10) -> dict[str, Any]:
    """Concentration over the consolidated, KRW-converted rows of consolidate_holdings.

    Weights are each row's weight_pct (market_value_krw over the valued total),
    so they match get_consolidated_holdings. Per-currency figures are secondary.
    Rows without a KRW value are counted, never weighted as zero. With any such row the
    concentration figures are null with UNVALUED_POSITIONS: a share of the valued
    rows only is not the portfolio's concentration.
    """
    positions = consolidated["positions"]
    valued = [p for p in positions if p.get("market_value_krw") is not None and p.get("weight_pct") is not None]
    unvalued = len(positions) - len(valued)
    reasons = ["NO_POSITIONS"] if not positions else ["UNVALUED_POSITIONS"] if unvalued else []
    measured = valued if not reasons else []
    weights = [p["market_value_krw"] / consolidated["totals"]["market_value_krw"] for p in measured] \
        if measured and consolidated["totals"]["market_value_krw"] else []
    hhi = round(sum(w * w for w in weights), 4) if weights else None
    top1 = measured[0]["weight_pct"] if measured else None
    top5 = round(sum(p["weight_pct"] for p in measured[:5]), 4) if measured else None
    by_currency: dict[str, dict[str, Any]] = {}
    for p in positions:
        row = by_currency.setdefault(p["currency"], {"position_count": 0, "market_value": 0.0, "_rows": []})
        row["position_count"] += 1
        if p.get("market_value") is not None:
            row["market_value"] += p["market_value"]
            row["_rows"].append(p)
    for currency, row in by_currency.items():
        rows = row.pop("_rows")
        total = row["market_value"]
        inner = [r["market_value"] / total for r in rows] if total else []
        top = max(rows, key=lambda r: r["market_value"]) if rows else None
        group = consolidated["by_currency"].get(currency) or {}
        row.update({"market_value": round(total, 2), "market_value_krw": group.get("market_value_krw"),
                    "weight_pct": group.get("weight_pct"),
                    "top1_symbol": top["symbol"] if top else None,
                    "top1_weight_pct_in_currency": round(top["market_value"] / total * 100, 2) if top and total else None,
                    "hhi_in_currency": round(sum(w * w for w in inner), 4) if inner else None})
    currency_weights = [g["weight_pct"] / 100 for g in consolidated["by_currency"].values() if g.get("weight_pct") is not None]
    top_positions = []
    for p in valued[:top_n]:
        currency_total = by_currency[p["currency"]]["market_value"]
        top_positions.append({
            "symbol": p["symbol"], "name": p.get("name"), "market": p.get("market"), "currency": p["currency"],
            "quantity": p["quantity"], "market_value": p["market_value"], "market_value_krw": p["market_value_krw"],
            "weight_pct": p["weight_pct"],
            "weight_pct_in_currency": round(p["market_value"] / currency_total * 100, 2) if currency_total else None,
            "sources": [a["account"] for a in p.get("accounts") or []],
            "account_count": p.get("account_count"), "mark": p.get("mark")})
    return {
        "position_count": len(positions),
        "valued_position_count": len(valued),
        "top_positions": top_positions,
        "concentration": {
            "top1_symbol": measured[0]["symbol"] if measured else None,
            "top1_weight_pct": top1, "top5_weight_pct": top5, "hhi": hhi,
            "effective_positions": round(1 / hhi, 2) if hhi else None,
            "currency_hhi": round(sum(w * w for w in currency_weights), 4) if currency_weights and not reasons else None,
            "risk_level": _risk_level(top1, hhi),
            "basis": "consolidated_krw",
            "status": "unavailable" if reasons else "complete",
            "unvalued_position_count": unvalued,
            "reason_codes": reasons,
        },
        "by_currency": dict(sorted(by_currency.items(), key=lambda kv: -(kv[1]["market_value_krw"] or 0))),
        "currency_exposure": {c: round(g["market_value"], 2) for c, g in sorted(by_currency.items())},
        "currency_weights_pct": {c: g.get("weight_pct") for c, g in sorted(consolidated["by_currency"].items())},
        "total_market_value_krw": consolidated["totals"]["market_value_krw"],
        "total_unconverted_value": round(sum(g["market_value"] for g in by_currency.values()), 2),
        "fx": consolidated.get("fx"),
        "mark_policy": consolidated.get("mark_policy"),
        "underlying_exposure": consolidated.get("underlying_exposure"),
        "methodology": {
            "fx_conversion": "krw_spot",
            "weights": "market_value_krw / total market_value_krw of the consolidated rows (= weight_pct)",
            "hhi": "sum of squared consolidated KRW weights; currency_hhi uses currency weights",
            "per_currency": "by_currency figures are within one currency, secondary to the consolidated figures",
            "marks": consolidated.get("mark_policy", {}).get("name"),
        },
    }


def _sum(values: Iterable[float | None]) -> float | None:
    total = 0.0
    seen = False
    for value in values:
        if value is not None:
            total += value
            seen = True
    return round(total, 2) if seen else None


# ── Realized P&L (moving-average cost basis) ─────────────


def aggregate_ledger_money(
    transactions: Sequence[Transaction],
    fx: FxRates,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Reported row costs/income with shared availability and trade-FX policy."""
    window_rows = [
        tx
        for tx in transactions
        if _in_range(tx.trade_date, start or _EPOCH, end or "9999-12-31")
    ]
    cost_rows = [
        {"currency": tx.currency.upper(), "fee": tx.fee, "tax": tx.tax}
        for tx in window_rows
    ]
    cost_totals: dict[str, Any] = {}
    cost_rates: list[dict[str, Any]] = []
    missing_cost_fx: set[str] = set()
    for field in ("fee", "tax"):
        plural = "fees" if field == "fee" else "taxes"
        statuses = [tx.cost_status(field) for tx in window_rows]
        known = bool(statuses) and all(
            v in {"reported", "verified_zero"} for v in statuses
        )
        status = (
            "verified_zero"
            if known and all(getattr(tx, field) == 0 for tx in window_rows)
            else "reported"
            if known
            else "unsupported"
            if statuses and all(v == "unsupported" for v in statuses)
            else "unknown"
        )
        converted = Decimal("0")
        conversion_known = True
        for tx in window_rows:
            amount = getattr(tx, field)
            if amount == 0:
                continue  # A reported zero needs no exchange rate.
            recorded = _recorded_rate(tx)
            rate = (
                recorded
                if recorded is not None and recorded > 0
                else fx.rate(tx.currency)
            )
            source = (
                "identity"
                if tx.currency.upper() == "KRW"
                else "broker_trade_rate"
                if recorded is not None and recorded > 0
                else "spot"
            )
            note = {
                "currency": tx.currency.upper(),
                "rate": rate,
                "source": source,
                "as_of": tx.trade_date
                if source == "broker_trade_rate"
                else fx.as_of
                if source == "spot"
                else None,
            }
            if note not in cost_rates:
                cost_rates.append(note)
            if rate is None or rate <= 0:
                missing_cost_fx.add(tx.currency.upper())
                conversion_known = False
            else:
                converted += Decimal(str(amount)) * Decimal(str(rate))
        cost_totals[f"{plural}_by_currency"] = _by_currency(cost_rows, field)
        cost_totals[f"{plural}_krw"] = (
            float(converted) if known and conversion_known else None
        )
        cost_totals[f"{plural}_status"] = status
    cost_totals["cost_fx"] = {
        "policy": "broker_trade_rate_else_spot",
        "rates": cost_rates,
        "missing_currencies": sorted(missing_cost_fx),
        "scope": "reported ledger row costs; not proof of historical completeness",
    }
    cost_totals["deprecated"] = {
        "fees": "Use fees_by_currency or fees_krw; mixed-currency scalar removed",
        "taxes": "Use taxes_by_currency or taxes_krw; mixed-currency scalar removed",
    }

    unsupported_income = bool(window_rows) and all(
        tx.source
        in {
            "toss:orders",
            "kis:inquire-daily-ccld",
            "kis:inquire-period-trans",
            "kis:inquire-ccnl",
        }
        for tx in window_rows
    )
    for kind, field in ((TxType.DIVIDEND, "dividends"), (TxType.INTEREST, "interest")):
        rows = [tx for tx in window_rows if tx.tx_type is kind]
        known = bool(rows)
        conversion_known = bool(rows)
        native = Decimal("0")
        converted = Decimal("0")
        all_zero = bool(rows)
        for tx in rows:
            amount = tx.net_amount if tx.net_amount is not None else tx.gross_amount
            if amount is None:
                known = conversion_known = all_zero = False
                continue
            native += Decimal(str(amount))
            all_zero &= amount == 0
            rate = _trade_rate(tx, fx)
            if amount == 0:
                continue
            if rate is None or rate <= 0:
                conversion_known = False
            else:
                converted += Decimal(str(amount)) * Decimal(str(rate))
        status = (
            "verified_zero"
            if known and all_zero
            else "reported"
            if known
            else "unknown"
            if rows
            else "unsupported"
            if unsupported_income
            else "unknown"
        )
        cost_totals[field] = float(native) if known else None
        cost_totals[field + "_krw"] = (
            float(converted) if known and conversion_known else None
        )
        cost_totals[field + "_status"] = status
        cost_totals[field + "_reason_codes"] = (
            []
            if status in {"reported", "verified_zero"}
            else [field.upper() + "_" + status.upper()]
        ) + (["MISSING_FX_RATE"] if known and not conversion_known else [])
    for field in ("fees", "taxes"):
        status = cost_totals[field + "_status"]
        cost_totals[field + "_reason_codes"] = (
            []
            if status in {"reported", "verified_zero"}
            else [field.upper() + "_" + status.upper()]
        ) + (
            ["MISSING_FX_RATE"]
            if status in {"reported", "verified_zero"}
            and cost_totals[field + "_krw"] is None
            else []
        )
    return cost_totals


def _replay_order(tx: Transaction) -> tuple[str, str, str]:
    """One fill order for every per-account replay (cost engine, review cycles, reconciliation).

    Trade date, then the broker's recorded fill instant (Toss filledAt, UTC milliseconds), then
    the order id. Order ids are not chronological (Toss orderId), so ordering by them alone
    put same-day sells before their buys. Rows without a fill instant (KIS) keep id order.
    """
    return (tx.trade_date, tx.execution_time_utc or "", tx.tx_id)


def _book_quantity_after(available: float, tx: Transaction) -> tuple[float, float]:
    """(quantity after a BUY/SELL, inferred opening quantity) under the engine's book rules.

    A sale larger than the recorded position is covered by an inferred opening position of
    unknown cost for the shortfall (held before the ledger shows it), sold by this sale; the
    book never goes negative and the shortfall stays visible instead of a silent clamp.
    """
    if tx.tx_type is TxType.BUY:
        return available + tx.quantity, 0.0
    inferred = tx.quantity - available if tx.quantity > available + 1e-9 else 0.0
    return max(available + inferred - tx.quantity, 0.0), inferred


def replay_book_quantities(transactions: Sequence[Transaction]) -> dict[tuple[str, str, str], dict[str, float]]:
    """Per (account, symbol, currency) quantity the cost engine ends with, by its own rules.

    Same order, splits and inferred openings as the realized replay, so reconciliation can
    compare the engine's quantity, not only observed net trades, with the broker.
    """
    books: dict[tuple[str, str, str], dict[str, float]] = {}
    for tx in sorted(transactions, key=_replay_order):
        symbol = (tx.symbol or "").upper()
        if not symbol:
            continue
        key = (tx.account, symbol, tx.currency.upper())
        if tx.tx_type is TxType.SPLIT:
            if key in books and tx.quantity > 0:
                books[key]["quantity"] = tx.quantity
            continue
        if tx.tx_type not in (TxType.BUY, TxType.SELL):
            continue
        book = books.setdefault(key, {"quantity": 0.0, "inferred_opening_qty": 0.0})
        book["quantity"], inferred = _book_quantity_after(book["quantity"], tx)
        book["inferred_opening_qty"] += inferred
    return books


def _last_inferred_openings(ordered: Sequence[Transaction], end: str | None) -> dict[tuple[str, str, str], int]:
    """Index in ``ordered`` of each book's last sale that books an inferred opening (realized replay rules)."""
    quantities: dict[tuple[str, str, str], float] = {}
    last: dict[tuple[str, str, str], int] = {}
    for index, tx in enumerate(ordered):
        symbol = (tx.symbol or "").upper()
        if (end and tx.trade_date > end) or not symbol:
            continue
        key = (tx.account, symbol, tx.currency.upper())
        if tx.tx_type is TxType.SPLIT:
            if key in quantities and tx.quantity > 0:
                quantities[key] = tx.quantity
            continue
        if tx.tx_type not in (TxType.BUY, TxType.SELL):
            continue
        quantities[key], inferred = _book_quantity_after(quantities.get(key, 0.0), tx)
        if inferred > 0:
            last[key] = index
    return last


def _drifted_books(quality: dict[str, Any] | None) -> set[tuple[str, str, str]]:
    """(account, symbol, currency) books whose engine quantity differs from the broker's."""
    return {(row["account"], str(row["symbol"]).upper(), str(row["currency"]).upper())
            for row in (quality or {}).get("reconciliation_breaks") or []
            if "ENGINE_QUANTITY_DRIFT" in (row.get("reason_codes") or [])}


def compute_realized_pnl(
    transactions: Sequence[Transaction],
    fx: FxRates,
    start: str | None = None,
    end: str | None = None,
    *,
    input_quality: dict[str, Any] | None = None,
    _include_account_books: bool = True,
) -> dict[str, Any]:
    """Replay per-account moving-average accounting with the established contract."""
    return _compute_realized_pnl(
        transactions, fx, start, end, input_quality=input_quality,
        _include_account_books=_include_account_books,
    )


def compute_realized_pnl_with_sales(transactions, fx, start=None, end=None, *, input_quality=None):
    """Add individual sale diagnostics from the same accounting replay, for review."""
    return _compute_realized_pnl(
        transactions, fx, start, end, input_quality=input_quality,
        _include_account_books=False, _include_sales=True,
    )


def _compute_realized_pnl(
    transactions: Sequence[Transaction],
    fx: FxRates,
    start: str | None = None,
    end: str | None = None,
    *,
    input_quality: dict[str, Any] | None = None,
    _include_account_books: bool = True,
    _include_sales: bool = False,
) -> dict[str, Any]:
    """Replay the ledger under moving-average cost basis per account.

    The full ledger is replayed so that cost basis is correct, and only sales
    inside [start, end] are reported. When a sale has no matching prior buy in the
    ledger (the window predates the purchase), the position is flagged in
    `incomplete_cost_basis`; per-symbol amounts keep the matched part only and
    must not be read as a true gain.

    totals.complete_basis and totals.incomplete_basis partition individual sales
    in the requested window. The latter has no P&L (null): it carries proceeds,
    the known cost of the matched shares and the unmatched quantity.
    Legacy realized_pnl_krw/realized_pnl_by_currency are null if any window sale
    has incomplete principal basis. Complete principal basis does not verify
    unreported buy/sell costs: totals.status/reason_codes report those separately.
    Missing proceeds null affected P&L while preserving principal basis. Fetch
    quality propagates to both subsets; unavailable differs from fetched empty.

    Foreign positions carry cost in both the trade currency and KRW, so KRW P&L
    decomposes exactly:

        realized_pnl_krw = realized_pnl x sell_rate + realized_fx_krw
        realized_fx_krw  = cost_in_currency x sell_rate - cost_in_krw

    i.e. the local price move converted at the exit rate, plus what the currency
    itself did between entry and exit. The split needs a rate on the buys as well
    as the sale (KIS reports one; Toss does not) — when a buy rate is missing the
    position falls back to converting local P&L at the sale rate and
    `realized_fx_krw` is None rather than a fabricated number.

    Fills replay in `_replay_order`. A sale larger than the recorded position books the
    shortfall as an inferred opening position of unknown cost (incomplete basis). Books whose
    engine quantity differs from the broker (input_quality reconciliation breaks with
    ENGINE_QUANTITY_DRIFT) have null realized P&L, reason ENGINE_QUANTITY_DRIFT.
    An inferred opening means that inventory was held from before the ledger's first row, so every
    earlier sale in that book was averaged without it: those sales are incomplete with null P&L,
    reason INFERRED_OPENING_POSITION (a ledger that starts after the purchase, e.g. a YTD-only read).
    """
    ordered = sorted(transactions, key=_replay_order)
    last_opening = _last_inferred_openings(ordered, end)
    books: dict[tuple[str, str, str], dict[str, Any]] = {}
    incomplete: set[str] = set()
    per_symbol: dict[tuple[str, str], dict[str, Any]] = {}
    sales: list[dict[str, Any]] = []
    quality = input_quality or {}
    drifted = _drifted_books(quality)
    source_status = quality.get("source_status")
    source_reasons = _quality_reasons(quality)
    if source_status == "unavailable" and not source_reasons:
        source_reasons = ["LEDGER_UNAVAILABLE"]
    money_totals = aggregate_ledger_money(transactions, fx, start, end)
    if source_reasons:
        for field in ("fees", "taxes", "dividends", "interest"):
            money_totals[field + "_krw"] = None
            money_totals[field + "_status"] = "unknown"
            money_totals[field + "_reason_codes"] = sorted(
                set(
                    money_totals[field + "_reason_codes"]
                    + source_reasons
                    + [field.upper() + "_UNKNOWN"]
                )
            )
    fx_parts: list[dict[str, Any]] = []

    for index, tx in enumerate(ordered):
        if end and tx.trade_date > end:
            continue
        in_window = _in_range(tx.trade_date, start or _EPOCH, end or "9999-12-31")
        currency = tx.currency.upper()
        symbol = (tx.symbol or "").upper()
        key = (symbol, currency)
        book_key = (tx.account, symbol, currency)

        if tx.tx_type in (TxType.DIVIDEND, TxType.INTEREST):
            continue

        if tx.tx_type is TxType.SPLIT and symbol:
            # Share count changes, cost basis does not. `quantity` carries the
            # post-split share count for the position.
            book = books.get(book_key)
            if book and tx.quantity > 0:
                book["quantity"] = tx.quantity
            continue

        if not symbol or tx.tx_type not in (TxType.BUY, TxType.SELL):
            continue

        book = books.setdefault(
            book_key,
            {
                "quantity": 0.0,
                "cost": 0.0,
                "cost_krw": 0.0,
                "krw_known": True,
                "buy_fees": 0.0,
                "buy_taxes": 0.0,
                "gross_cost": 0.0,
                "fees_known": True,
                "taxes_known": True,
                "gross_known": True,
                "cost_reason_codes": set(),
                "inferred_opening_qty": 0.0,
            },
        )
        gross = (
            tx.gross_amount
            if tx.gross_amount is not None
            else ((tx.price or 0.0) * tx.quantity)
        )
        rate = _recorded_rate(tx)

        if tx.tx_type is TxType.BUY:
            if book["quantity"] == 0:
                # A new purchase after liquidation starts a new basis history.
                book["gross_known"] = book["fees_known"] = book["taxes_known"] = True
                book["cost_reason_codes"] = set()
            for field, plural in (("fee", "FEES"), ("tax", "TAXES")):
                status = tx.cost_status(field)
                if status not in {"reported", "verified_zero"}:
                    book["cost_reason_codes"].add(f"{plural}_{status.upper()}")
            book["quantity"], _ = _book_quantity_after(book["quantity"], tx)
            book["cost"] += gross + tx.fee + tx.tax
            book["gross_cost"] += gross
            book["buy_fees"] += tx.fee
            book["buy_taxes"] += tx.tax
            book["fees_known"] &= tx.cost_status("fee") in {"reported", "verified_zero"}
            book["taxes_known"] &= tx.cost_status("tax") in {
                "reported",
                "verified_zero",
            }
            book["gross_known"] &= tx.gross_amount is not None or tx.price is not None
            if rate is None:
                book["krw_known"] = False
            else:
                book["cost_krw"] += (gross + tx.fee + tx.tax) * rate
            continue

        # SELL
        proceeds_known = tx.gross_amount is not None or tx.price is not None
        proceeds = gross - tx.fee - tx.tax
        available = book["quantity"]
        avg_cost = (book["cost"] / available) if available > 0 else None
        matched = min(tx.quantity, available) if available > 0 else 0.0
        basis_complete = matched >= tx.quantity - 1e-9 and book["gross_known"]
        cost_removed = (avg_cost or 0.0) * matched
        krw_known = bool(book["krw_known"]) and available > 0 and rate is not None
        cost_krw_removed = (
            (book["cost_krw"] / available) * matched if krw_known else None
        )
        if matched < tx.quantity - 1e-9:
            incomplete.add(symbol)
        book["quantity"], inferred_opening = _book_quantity_after(available, tx)
        book["inferred_opening_qty"] += inferred_opening
        remaining = book["quantity"] > 0
        book["cost"] = max(book["cost"] - cost_removed, 0.0) if remaining else 0.0
        for component in ("gross_cost", "buy_fees", "buy_taxes"):
            book[component] *= book["quantity"] / available if available > 0 else 0.0
        if not book["gross_known"]:
            incomplete.add(symbol)
        book["cost_krw"] = (
            max(book["cost_krw"] - (cost_krw_removed or 0.0), 0.0)
            if remaining and krw_known
            else 0.0
        )
        realized = proceeds - cost_removed

        # Local P&L at the exit rate, plus the currency's own contribution.
        if krw_known and rate is not None:
            realized_krw = realized * rate
            fx_component: float | None = cost_removed * rate - (cost_krw_removed or 0.0)
        else:
            realized_krw = _to_krw(realized, tx, fx) or 0.0
            fx_component = None

        if not in_window:
            continue
        # The engine's book disagrees with the broker's quantity: its cost per share is not
        # established for this book, so the sale's P&L is unknown, never a computed figure.
        # INCOMPLETE_COST_BASIS keeps meaning unmatched quantity or unknown purchase gross.
        drift = book_key in drifted
        # A later inferred opening in this book: this sale's average omitted inventory of unknown cost.
        before_opening = index < last_opening.get(book_key, -1)
        unknown_cost = drift or before_opening
        sale_reasons = set(book["cost_reason_codes"]) if matched > 0 else set()
        if before_opening:
            basis_complete = False
            sale_reasons.add("INFERRED_OPENING_POSITION")
        if not basis_complete:
            sale_reasons.add("INCOMPLETE_COST_BASIS")
        if drift:
            basis_complete = False
            sale_reasons.add("ENGINE_QUANTITY_DRIFT")
        if unknown_cost:
            incomplete.add(symbol)
            fx_component = None
        if not proceeds_known:
            sale_reasons.add("MISSING_PROCEEDS")
        for field, plural in (("fee", "FEES"), ("tax", "TAXES")):
            status = tx.cost_status(field)
            if status not in {"reported", "verified_zero"}:
                sale_reasons.add(f"{plural}_{status.upper()}")
        conversion_rate = _trade_rate(tx, fx)
        conversion_known = conversion_rate is not None and conversion_rate > 0
        if not conversion_known:
            sale_reasons.add("MISSING_FX_RATE")
        sales.append(
            {
                "account": tx.account,
                "symbol": symbol,
                "currency": currency,
                "realized_pnl": realized if proceeds_known and not unknown_cost else None,
                "realized_pnl_krw": realized_krw + (fx_component or 0.0)
                if conversion_known and proceeds_known and not unknown_cost
                else None,
                "cost_basis_complete": basis_complete,
                "reason_codes": sorted(sale_reasons),
                "proceeds": proceeds if proceeds_known else None,
                "known_cost": None if unknown_cost or (matched > 0 and not book["gross_known"]) else cost_removed,
                "unmatched_quantity": max(tx.quantity - matched, 0.0),
                **({"tx_id": tx.tx_id,
                    "trade_date": tx.trade_date, "quantity": tx.quantity,
                    "inferred_opening_qty": inferred_opening,
                    "cost_basis": cost_removed,
                    "status": "partial" if sale_reasons or source_reasons else "complete"}
                   if _include_sales else {}),
            }
        )
        fx_parts.append({"account": tx.account, "realized_fx_krw": fx_component})
        entry = per_symbol.setdefault(
            key,
            {
                "symbol": symbol,
                "name": tx.name,
                "currency": currency,
                "market": tx.market,
                "quantity_sold": 0.0,
                "proceeds": 0.0,
                "cost_basis": 0.0,
                "realized_pnl": 0.0,
                "realized_pnl_krw": 0.0,
                "realized_fx_krw": 0.0,
                "fees": 0.0,
                "taxes": 0.0,
                "sell_count": 0,
                "cost_basis_complete": True,
                "_fx_known": True,
                "_fee_statuses": [],
                "_tax_statuses": [],
                "_proceeds_known": True,
                "_conversion_known": True,
                "_account_amounts": {},
                "_unknown_cost": False,
                "reason_codes": [],
            },
        )
        entry["quantity_sold"] += tx.quantity
        entry["proceeds"] += proceeds
        entry["cost_basis"] += cost_removed
        entry["realized_pnl"] += realized
        entry["realized_pnl_krw"] += realized_krw + (fx_component or 0.0)
        if fx_component is None:
            entry["_fx_known"] = False
        else:
            entry["realized_fx_krw"] += fx_component
        entry["fees"] += tx.fee
        entry["taxes"] += tx.tax
        amounts = entry["_account_amounts"].setdefault(tx.account, defaultdict(float))
        for field, amount in (
            ("proceeds", proceeds), ("cost_basis", cost_removed),
            ("realized_pnl", realized),
            ("realized_pnl_krw", realized_krw + (fx_component or 0.0)),
            ("realized_fx_krw", fx_component or 0.0),
            ("fees", tx.fee), ("taxes", tx.tax),
        ):
            amounts[field] += amount
        entry["_fee_statuses"].append(tx.cost_status("fee"))
        entry["_tax_statuses"].append(tx.cost_status("tax"))
        entry["sell_count"] += 1
        entry["_proceeds_known"] &= proceeds_known
        entry["_conversion_known"] &= conversion_known
        entry["reason_codes"] = sorted(set(entry["reason_codes"]) | sale_reasons)
        entry["_unknown_cost"] |= unknown_cost
        if not basis_complete:
            entry["cost_basis_complete"] = False

    symbols: list[dict[str, Any]] = []
    account_proceeds: list[dict[str, Any]] = []
    for entry in per_symbol.values():
        account_amounts = list(entry.pop("_account_amounts").values())
        proceeds_known = entry.pop("_proceeds_known")
        conversion_known = entry.pop("_conversion_known")
        for field in ("fee", "tax"):
            plural = "fees" if field == "fee" else "taxes"
            statuses = entry.pop(f"_{field}_statuses")
            known = all(status in {"reported", "verified_zero"} for status in statuses)
            entry[f"{plural}_status"] = (
                "verified_zero"
                if known and entry[plural] == 0
                else "reported"
                if known
                else "unsupported"
                if all(status == "unsupported" for status in statuses)
                else "unknown"
            )
        entry["realized_pnl_pct"] = (
            round(entry["realized_pnl"] / entry["cost_basis"] * 100, 4)
            if entry["cost_basis"] and proceeds_known
            else None
        )
        if not entry.pop("_fx_known"):
            entry["realized_fx_krw"] = None
        for field in (
            "proceeds",
            "cost_basis",
            "realized_pnl",
            "realized_pnl_krw",
            "fees",
            "taxes",
        ):
            entry[field] = _sum_rounded(account_amounts, field, 4)
        if entry["realized_fx_krw"] is not None:
            entry["realized_fx_krw"] = _sum_rounded(account_amounts, "realized_fx_krw", 2)
        account_proceeds.extend({"currency": entry["currency"],
                                 "proceeds": round(amounts["proceeds"], 4)
                                 if proceeds_known else None}
                                for amounts in account_amounts)
        if not proceeds_known:
            for field in (
                "proceeds",
                "realized_pnl",
                "realized_pnl_krw",
                "realized_fx_krw",
            ):
                entry[field] = None
        elif not conversion_known:
            entry["realized_pnl_krw"] = None
        if entry.pop("_unknown_cost"):
            for field in ("cost_basis", "realized_pnl", "realized_pnl_krw", "realized_fx_krw", "realized_pnl_pct"):
                entry[field] = None
        entry["status"] = "partial" if entry["reason_codes"] else "complete"
        symbols.append(entry)
    symbols.sort(key=lambda s: s["realized_pnl_krw"] or 0.0, reverse=True)

    def basis_subset(complete: bool) -> dict[str, Any]:
        rows = [sale for sale in sales if sale["cost_basis_complete"] == complete]
        names = sorted({sale["symbol"] for sale in rows})
        reasons = sorted(
            {reason for sale in rows for reason in sale["reason_codes"]}
            | set(source_reasons)
        )
        if not complete:
            # Without the full purchase cost a sale's realized P&L is unknown (the totals rule);
            # proceeds minus the part of the cost that is known is not P&L, so none is published.
            # What is known is: proceeds and the cost of the matched shares, per currency.
            unavailable = source_status == "unavailable"
            return {
                # An empty subset keeps its explicit zero: no sale, nothing realized.
                "realized_pnl_krw": None if rows or unavailable else 0,
                "realized_pnl_by_currency": None if rows or unavailable else {},
                "realized_pnl_status": "unavailable" if rows or unavailable else "empty",
                "realized_pnl_reason_codes": sorted(
                    {"INCOMPLETE_COST_BASIS"} | {r for sale in rows for r in sale["reason_codes"]
                                                 if r in ("ENGINE_QUANTITY_DRIFT", "INFERRED_OPENING_POSITION")})
                if rows else [],
                "proceeds_by_currency": None if unavailable else _rounded_account_by_currency(rows, "proceeds", 4),
                "known_partial_cost_by_currency": None if unavailable
                else _rounded_account_by_currency(rows, "known_cost", 4),
                "unmatched_quantity_by_symbol": None if unavailable else {
                    f"{name}:{currency}": round(sum(s["unmatched_quantity"] for s in rows
                                                    if s["symbol"] == name and s["currency"] == currency), 8)
                    for name, currency in sorted({(s["symbol"], s["currency"]) for s in rows})},
                "sell_count": len(rows),
                "symbol_count": len(names),
                "symbols": names,
                "status": "unavailable" if unavailable else "partial" if reasons
                else "empty" if source_status == "empty" else "complete",
                "reason_codes": reasons,
            }
        return {
            "realized_pnl_krw": None
            if source_status == "unavailable"
            or any(sale["realized_pnl_krw"] is None for sale in rows)
            else _rounded_account_total(rows, "realized_pnl_krw", 2),
            "realized_pnl_by_currency": None
            if source_status == "unavailable"
            else _rounded_account_by_currency(rows, "realized_pnl", 4),
            "sell_count": len(rows),
            "symbol_count": len(names),
            "symbols": names,
            "status": "unavailable"
            if source_status == "unavailable"
            else "partial"
            if reasons
            else "empty"
            if source_status == "empty"
            else "complete",
            "reason_codes": reasons,
        }

    complete_basis = basis_subset(True)
    incomplete_basis = basis_subset(False)
    has_incomplete_sales = incomplete_basis["sell_count"] > 0
    total_reasons = sorted(
        set(complete_basis["reason_codes"] + incomplete_basis["reason_codes"])
    )

    # Round each account's published values before summing them with Decimal.
    # Aggregates must equal the visible account entries, including the last digit.
    combined_positions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    account_positions = {account: {} for account in sorted({tx.account for tx in ordered})}
    for (account, symbol, currency), book in sorted(books.items()):
        if book["quantity"] <= 0:
            continue
        position_key = f"{symbol}:{currency}"
        position = _ledger_position(book, currency)
        account_positions[account][position_key] = position
        combined_positions[position_key].append(position)

    fx_reasons = sorted(set(source_reasons)
                        | ({"MISSING_PROCEEDS"} if "MISSING_PROCEEDS" in total_reasons else set())
                        | ({"FX_DECOMPOSITION_PARTIAL"}
                           if any(part["realized_fx_krw"] is None for part in fx_parts) else set()))

    result = {
        "period": {"start": start, "end": end},
        "symbols": symbols,
        "totals": {
            "realized_pnl_krw": None
            if has_incomplete_sales or source_reasons
            else complete_basis["realized_pnl_krw"],
            "proceeds_by_currency": _by_currency(account_proceeds, "proceeds"),
            "realized_pnl_by_currency": None
            if has_incomplete_sales
            or source_reasons
            or "MISSING_PROCEEDS" in total_reasons
            else complete_basis["realized_pnl_by_currency"],
            "complete_basis": complete_basis,
            "incomplete_basis": incomplete_basis,
            "status": "unavailable"
            if source_status == "unavailable"
            else "partial"
            if total_reasons
            else "empty"
            if source_status == "empty"
            else "complete",
            "reason_codes": total_reasons,
            "realized_pnl_policy": (
                "Legacy KRW and by-currency totals are null when any window sale has incomplete "
                "principal basis, missing proceeds or incomplete source coverage; KRW is also null "
                "if conversion is missing. Subsets partition sales, "
                "not symbols; incomplete_basis has null P&L, with proceeds and the known partial cost "
                "by currency. Books whose engine quantity differs from the broker (ENGINE_QUANTITY_DRIFT) "
                "have null P&L. Complete principal "
                "basis is subject to cost availability status, not proof of historical completeness."
            ),
            "realized_fx_krw": _rounded_account_total(fx_parts, "realized_fx_krw", 2)
            if fx_parts and not fx_reasons
            else None,
            "realized_fx_krw_reason_codes": fx_reasons,
            "fees": None,
            "taxes": None,
            **money_totals,
            "sell_count": sum(s["sell_count"] for s in symbols),
        },
        "cost_basis_method": "moving_average",
        "methodology": {"basis_scope": "per_account"},
        "incomplete_cost_basis": sorted(incomplete),
        "ledger_quality": quality,
        "open_positions_from_ledger": {
            key: _combine_ledger_positions(positions)
            for key, positions in sorted(combined_positions.items())
        },
    }

    if _include_sales:
        result["sales"] = sales
    if _include_account_books:
        result["open_positions_by_account"] = account_positions
    return result


def _ledger_position(book: dict[str, Any], currency: str) -> dict[str, Any]:
    """Serialize an account book or the sum of remaining account books."""
    return {
        "quantity": round(book["quantity"], 8),
        "cost_basis": round(book["cost"], 4),
        "avg_price": round(book["cost"] / book["quantity"], 6)
        if book["quantity"] else None,
        "ledger_cost_basis": {
            "amount": round(book["cost"], 4) if book["gross_known"] else None,
            "currency": currency,
            "includes_buy_fees": True if book["fees_known"] else None,
            "includes_buy_taxes": True if book["taxes_known"] else None,
            "basis_source": "ledger_moving_average",
            "basis_scope": "per_account",
            "status": "reported"
            if book["gross_known"] and book["fees_known"] and book["taxes_known"]
            else "partial" if book["gross_known"] else "unknown",
            "reason": None
            if book["gross_known"] and book["fees_known"] and book["taxes_known"]
            else "unreported_buy_costs" if book["gross_known"]
            else "missing_native_gross_and_price",
            "capitalization_policy": "reported buy fees/taxes capitalized; unreported costs unknown",
        },
        "gross_cost": round(book["gross_cost"], 4) if book["gross_known"] else None,
        "capitalized_buy_fees": round(book["buy_fees"], 4),
        "capitalized_buy_taxes": round(book["buy_taxes"], 4),
    }


def _decimal_sum(values: Iterable[float | None]) -> float | None:
    values = list(values)
    if any(value is None for value in values):
        return None
    return float(sum((Decimal(str(value)) for value in values), Decimal("0")))


def _sum_rounded(rows: Sequence[dict[str, Any]], field: str, digits: int) -> float:
    return _decimal_sum(round(row[field], digits) for row in rows)


def _rounded_account_total(rows: Sequence[dict[str, Any]], field: str, digits: int) -> float | None:
    # Preserve the legacy serialized zero for empty subsets/cohorts.
    if not rows:
        return 0
    grouped: dict[str, list[float | None]] = defaultdict(list)
    for row in rows:
        grouped[row["account"]].append(row[field])
    amounts = [_decimal_sum(values) for values in grouped.values()]
    return _decimal_sum(None if amount is None else round(amount, digits) for amount in amounts)


def _rounded_account_by_currency(rows: Sequence[dict[str, Any]], field: str, digits: int) -> dict[str, float | None]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["account"]].append(row)
    rounded = [{"currency": currency, field: None if amount is None else round(amount, digits)}
               for account_rows in grouped.values()
               for currency, amount in _by_currency(account_rows, field).items()]
    return _by_currency(rounded, field)


def _combine_ledger_positions(positions: Sequence[dict[str, Any]]) -> dict[str, Any]:
    combined = dict(positions[0])
    for field in ("quantity", "cost_basis", "gross_cost", "capitalized_buy_fees", "capitalized_buy_taxes"):
        combined[field] = _decimal_sum(position[field] for position in positions)
    cost = sum((Decimal(str(p["cost_basis"])) for p in positions), Decimal("0"))
    quantity = sum((Decimal(str(p["quantity"])) for p in positions), Decimal("0"))
    combined["avg_price"] = float(round(cost / quantity, 6)) if quantity else None
    bases = [position["ledger_cost_basis"] for position in positions]
    basis = dict(bases[0])
    basis["amount"] = _decimal_sum(b["amount"] for b in bases)
    for field in ("includes_buy_fees", "includes_buy_taxes"):
        basis[field] = True if all(b[field] is True for b in bases) else None
    basis["status"] = "unknown" if basis["amount"] is None else "reported" if all(
        b["status"] == "reported" for b in bases) else "partial"
    basis["reason"] = None if basis["status"] == "reported" else "unreported_buy_costs" if (
        basis["amount"] is not None) else "missing_native_gross_and_price"
    combined["ledger_cost_basis"] = basis
    return combined


def _by_currency(
    symbols: Sequence[dict[str, Any]], field: str
) -> dict[str, float | None]:
    # Add decimal representations once, with no per-row currency rounding.
    totals: dict[str, Decimal | None] = {}
    for entry in symbols:
        currency = entry["currency"].upper()
        if entry[field] is None or (currency in totals and totals[currency] is None):
            totals[currency] = None
        else:
            totals[currency] = totals.get(currency, Decimal("0")) + Decimal(
                str(entry[field])
            )
    return {
        currency: None if total is None else float(total)
        for currency, total in sorted(totals.items())
    }


def _recorded_rate(tx: Transaction) -> float | None:
    """The rate the broker actually recorded for this trade, or None.

    Distinct from _trade_rate: only a rate observed at trade time can support the
    FX decomposition. Substituting today's spot for a missing entry rate would make
    every FX component measure as zero, hiding real currency P&L behind a fake zero.
    """
    if tx.currency.upper() == "KRW":
        return 1.0
    return tx.fx_rate or None


def _trade_rate(tx: Transaction, fx: FxRates) -> float | None:
    """The `currency -> KRW` rate to apply to this transaction.

    Prefers the rate the broker recorded for the trade; falls back to current spot.
    """
    recorded = _recorded_rate(tx)
    if recorded is not None:
        return recorded
    return fx.rate(tx.currency)


def _to_krw(amount: float | None, tx: Transaction, fx: FxRates) -> float | None:
    """Convert using the broker's trade-time rate when present, else spot."""
    if amount is None:
        return None
    rate = _trade_rate(tx, fx)
    return None if rate is None else amount * rate


# ── Cash-flow and return metrics ─────────────────────────


_METRIC_VERSION = "flow_eligibility_v1"


def _metric(value, reasons, required_inputs, coverage):
    from ledger_returns import add_coverage
    coverage = add_coverage(dict(coverage), coverage.get('ledger_flow_coverage') or coverage.get('external_cashflows'), reasons)
    return {
        "value": None if reasons else value,
        "status": "unavailable" if reasons or value is None else "available",
        "reason_codes": list(dict.fromkeys(reasons or ([] if value is not None else ["INSUFFICIENT_INPUTS"]))),
        "method_version": _METRIC_VERSION,
        "required_inputs": required_inputs,
        "coverage": coverage,
        "input_snapshot_id": coverage.get("input_snapshot_id"),
        "input_snapshot_ids": coverage.get("snapshot_ids", []),
    }


def _flow_reasons(coverage, start, end):
    if coverage and coverage.get('source') == 'permanent_ledger':
        return list(coverage.get('reason_codes', []))
    if not coverage or coverage.get("status") not in {"verified_zero", "complete"}:
        return ["MISSING_EXTERNAL_CASHFLOWS"]
    if not coverage.get("source"):
        return ["MISSING_CASHFLOW_VERIFICATION_SOURCE"]
    if not coverage.get("start") or not coverage.get("end") or coverage["start"] > start or coverage["end"] < end:
        return ["INCOMPLETE_EXTERNAL_CASHFLOW_COVERAGE"]
    return []


def _quality_reasons(quality):
    quality = quality or {}
    return (["FAILED_SOURCES"] if quality.get("sources_failed") else []) + (
        ["TRUNCATED_SOURCES"] if quality.get("truncated_sources") else []) + (
        ["MISSING_FX_RATE"] if quality.get("missing_currencies") else []) + (
        ["MISSING_POSITION_VALUATION"] if quality.get("missing_position_valuation") else []) + (
        ["RECONCILIATION_BREAK"] if quality.get("reconciliation_breaks") else [])


def collection_result_status(collections, row_count):
    """Distinguish successful empty reads from failures, preserving partial rows."""
    health = [entry for collection in collections for entry in collection.health.values()]
    succeeded = any(entry.get("status") in {"ok", "empty", "truncated"} for entry in health)
    failed = any(collection.failed_sources or collection.truncated_sources for collection in collections)
    if not succeeded:
        return "unavailable"
    if not row_count:
        return "partial" if failed else "empty"
    return "partial" if failed else "complete"


def ledger_input_quality(collection):
    """Keep transaction fetch availability distinct from an observed empty ledger."""
    status = collection_result_status([collection], len(collection.transactions))
    if status == "unavailable" and collection.ok_sources:
        status = "complete" if collection.transactions else "empty"
    return {
        "source_status": status,
        "sources_failed": collection.failed_sources,
        "truncated_sources": collection.truncated_sources,
        "reconciliation_breaks": getattr(collection, "reconciliation", {}).get("reconciliation_breaks", []),
    }


def _money_fields(totals):
    """Scalar money values always travel with their availability and reasons."""
    output = {}
    for field in ("fees", "taxes", "dividends", "interest"):
        status = totals.get(field + "_status", "unknown")
        output[field + "_krw"] = totals.get(field + "_krw")
        output[field + "_status"] = status
        reasons = list(totals.get(field + "_reason_codes", []))
        if status not in {"reported", "verified_zero"}:
            reasons.append(field.upper() + "_" + status.upper())
        if (
            output[field + "_krw"] is None
            and status in {"reported", "verified_zero"}
            and not reasons
        ):
            reasons.append("MISSING_FX_RATE")
        output[field + "_reason_codes"] = list(dict.fromkeys(reasons))
    return output


def summarize_cashflows(
    transactions: Sequence[Transaction], fx: FxRates, start: str, end: str,
    external_cashflows_coverage: dict[str, Any] | None = None,
    ledger_totals: dict[str, Any] | None = None,
    ledger_flow_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Observed flows; shared income/cost availability never assumes unreported zero."""
    deposits = withdrawals = 0.0
    money_totals = aggregate_ledger_money(transactions, fx, start, end)
    # Prefer availability-aware ledger totals supplied by the caller. Earlier
    # callers with only realized P&L keys still use the shared row aggregation.
    money_totals.update(ledger_totals or {})
    buys = sells = 0.0
    reasons = _flow_reasons(external_cashflows_coverage, start, end)
    flows_by_date: dict[str, float] = {}
    for tx in transactions:
        if not _in_range(tx.trade_date, start, end):
            continue
        amount = _to_krw(tx.net_amount, tx, fx)
        if ledger_flow_input is None and tx.tx_type in (TxType.DEPOSIT, TxType.WITHDRAWAL, TxType.TRANSFER):
            if tx.tx_type is TxType.TRANSFER:
                reasons.append("UNCLASSIFIED_TRANSFER")
            elif tx.net_amount is None:
                reasons.append("MISSING_EXTERNAL_CASHFLOW_AMOUNT")
            elif amount is None:
                reasons.append("MISSING_FLOW_FX_RATE")
            else:
                signed = abs(amount) if tx.tx_type is TxType.DEPOSIT else -abs(amount)
                flows_by_date[tx.trade_date] = flows_by_date.get(tx.trade_date, 0.0) + signed
                if tx.tx_type is TxType.DEPOSIT:
                    deposits += abs(amount)
                else:
                    withdrawals += abs(amount)
        if amount is None:
            continue
        if tx.tx_type is TxType.BUY:
            buys += abs(amount)
        elif tx.tx_type is TxType.SELL:
            sells += amount
    if ledger_flow_input is not None:
        deposits = ledger_flow_input['observed_deposits_krw']
        withdrawals = ledger_flow_input['observed_withdrawals_krw']
        flows_by_date = ledger_flow_input['flows_by_date']
    if (external_cashflows_coverage or {}).get("status") == "verified_zero" and (deposits or withdrawals):
        reasons.append("CONFLICTING_VERIFIED_ZERO_CASHFLOWS")
    return {
        "deposits_krw": None if reasons else round(deposits, 2),
        "withdrawals_krw": None if reasons else round(withdrawals, 2),
        "net_deposits_krw": None if reasons else round(deposits - withdrawals, 2),
        "observed_deposits_krw": round(deposits, 2) if deposits is not None else None,
        "observed_withdrawals_krw": round(withdrawals, 2) if withdrawals is not None else None,
        "external_cashflows": {**(external_cashflows_coverage or {"status": "unknown"}),
                               "reason_codes": list(dict.fromkeys(reasons))},
        "flows_by_date": None if reasons else flows_by_date,
        **_money_fields(money_totals),
        "buy_amount_krw": round(buys, 2),
        "sell_amount_krw": round(sells, 2),
    }


def xirr(cashflows: Sequence[tuple[str, float]], guess: float = 0.1) -> float | None:
    """Money-weighted annual return (XIRR) by Newton's method with bisection fallback.

    `cashflows` are (ISO date, amount) with the sign convention of a bank account:
    contributions negative, withdrawals and terminal value positive. Returns None
    when the flows have no sign change or no root converges.
    """
    flows = [(datetime.strptime(d, "%Y-%m-%d").date(), amount) for d, amount in cashflows if amount]
    if len(flows) < 2:
        return None
    if not (any(a > 0 for _, a in flows) and any(a < 0 for _, a in flows)):
        return None
    flows.sort(key=lambda f: f[0])
    if flows[0][0] == flows[-1][0]:
        return None
    t0 = flows[0][0]
    years = [((d - t0).days / _DAYS_PER_YEAR, amount) for d, amount in flows]

    def npv(rate: float) -> float:
        return sum(amount / (1.0 + rate) ** t for t, amount in years)

    rate = guess
    for _ in range(100):
        try:
            value = npv(rate)
            derivative = sum(-t * amount / (1.0 + rate) ** (t + 1) for t, amount in years)
        except (OverflowError, ZeroDivisionError):
            break
        if abs(value) < 1e-7:
            return round(rate, 6)
        if abs(derivative) < 1e-12:
            break
        step = value / derivative
        rate -= step
        if rate <= -0.999999:
            rate = -0.99
        if abs(step) < 1e-9:
            return round(rate, 6)

    low, high = -0.9999, 10.0
    try:
        low_value, high_value = npv(low), npv(high)
    except (OverflowError, ZeroDivisionError):
        return None
    if low_value * high_value > 0:
        return None
    for _ in range(200):
        mid = (low + high) / 2
        mid_value = npv(mid)
        if abs(mid_value) < 1e-9:
            return round(mid, 6)
        if low_value * mid_value < 0:
            high = mid
        else:
            low, low_value = mid, mid_value
    return round((low + high) / 2, 6)


def _nav_series(navs):
    return sorted((row["snapshot_date"], float(row["total_value_krw"]))
                  for row in navs if row.get("total_value_krw") is not None)


def _nav_endpoint_basis(side, nav, point, navs):
    """<side>_nav_basis/_as_of/_snapshot_date/_snapshot_id for a NAV taken from a stored snapshot."""
    row = {}
    if nav is not None and point is not None:
        matches = [r for r in navs if r.get("snapshot_date") == point[0] and r.get("total_value_krw") is not None
                   and float(r["total_value_krw"]) == point[1]]
        row = matches[-1] if side == "end" and matches else matches[0] if matches else {}
    return {f"{side}_nav_basis": "stored_snapshot" if nav is not None else None,
            f"{side}_nav_as_of": row.get("valuation_at") if nav is not None else None,
            f"{side}_nav_snapshot_date": row.get("snapshot_date") if nav is not None else None,
            f"{side}_nav_snapshot_id": row.get("snapshot_id") if nav is not None else None}


#: Valuation basis step (flag, never a block): stored rows label what their broker-account total used
#: in consolidated_nav_krw.basis (daily_nav: legacy, broker marks or canonical marks). An interval whose two
#: endpoints carry different labels includes that revaluation; its figures are kept with this code and note.
VALUATION_BASIS_CHANGED = "VALUATION_BASIS_CHANGED"
VALUATION_BASIS_CHANGE_NOTE = ("revaluation between the endpoints: legacy = broker values + yfinance FX; "
                               "broker_marks = one broker price per symbol + Toss FX; canonical_marks = timed "
                               "quotes + Toss FX; the figure is kept and includes that valuation step")
#: Scopes valued from the broker-account total or its FX; the kis and crypto scopes use their own captures.
VALUATION_BASIS_SCOPES = ("equity", "consolidated", "toss", "toss_investmcp")


def _valuation_basis(row):
    return ((row or {}).get("consolidated_nav_krw") or {}).get("basis")


def _valuation_basis_key(basis, scope):
    """What a label changes for this scope: the toss scopes value the accounts' own native NAV (Toss
    quotes) at fx_times, so only the legacy FX source differs for them; other scopes compare labels."""
    if basis and scope in ("toss", "toss_investmcp"):
        from daily_nav import LEGACY_CONSOLIDATED_NAV_BASIS
        return "legacy_fx" if basis == LEGACY_CONSOLIDATED_NAV_BASIS else "fx_policy"
    return basis


def _valuation_basis_steps(navs, scope="equity", end_basis=None, end_date=None):
    """(start_date, end_date, from, to) for consecutive NAV points (_nav_series order) whose labels differ.

    ``end_basis`` labels a live end NAV at ``end_date`` after the stored series; an unlabelled point is
    never compared.
    """
    if scope not in VALUATION_BASIS_SCOPES:
        return []
    points = [(day, basis) for day, _, basis in sorted(
        ((row["snapshot_date"], float(row["total_value_krw"]), _valuation_basis(row))
         for row in navs if row.get("total_value_krw") is not None), key=lambda point: point[:2])]
    if end_basis is not None and points:
        points.append((end_date, end_basis))
    return [(start, end, before, after) for (start, before), (end, after) in zip(points, points[1:])
            if before and after and _valuation_basis_key(before, scope) != _valuation_basis_key(after, scope)]


def valuation_basis_change(navs, scope="equity", end_basis=None, end_date=None):
    """``{"warning_codes": [...], "valuation_basis_change": {...} | None}`` for a window of NAV rows."""
    steps = _valuation_basis_steps(navs, scope, end_basis, end_date)
    return {"warning_codes": [VALUATION_BASIS_CHANGED] if steps else [],
            "valuation_basis_change": {
                "intervals": [{"start_date": start, "end_date": end, "from_basis": before, "to_basis": after}
                              for start, end, before, after in steps],
                "note": VALUATION_BASIS_CHANGE_NOTE} if steps else None}


def _interval_flow(flows_by_date, start, end):
    return sum(amount for day, amount in flows_by_date.items() if start < day <= end)


def time_weighted_return(navs: Sequence[dict[str, Any]], flows_by_date: dict[str, float] | None) -> float | None:
    """Explicit flow mapping required; flows are assumed at interval end.

    An explicit empty mapping asserts zero flows over this supplied series.
    The tool layer never supplies one when broker flow coverage is unknown.
    """
    series = _nav_series(navs)
    if flows_by_date is None or len(series) < 2 or any(value <= 0 for _, value in series):
        return None
    factor = 1.0
    for (start_date, start_value), (end_date, end_value) in zip(series, series[1:]):
        flow = _interval_flow(flows_by_date, start_date, end_date)
        factor *= (end_value - flow) / start_value
    return round(factor - 1.0, 6)


def nav_statistics(
    navs: Sequence[dict[str, Any]], risk_free_rate: float = 0.0,
    flows_by_date: dict[str, float] | None = None,
    input_quality: dict[str, Any] | None = None,
    flow_reason_codes: Sequence[str] | None = None,
    external_cashflows_coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Separate asset-value changes from eligible flow-adjusted return statistics."""
    series = _nav_series(navs)
    coverage = {"snapshot_dates": [day for day, _ in series], "nav_observations": len(series),
                "external_cashflows": external_cashflows_coverage or {
                    "status": "unknown" if flows_by_date is None else "complete",
                    "source": None if flows_by_date is None else "explicit_mapping",
                    "start": series[0][0] if series else None,
                    "end": series[-1][0] if series else None},
                "input_quality": input_quality or {},
                "snapshot_ids": [row["snapshot_id"] for row in navs if row.get("snapshot_id")],
                "snapshot_calculation_versions": sorted({row["calculation_version"] for row in navs if row.get("calculation_version")})}
    reasons = list(flow_reason_codes or (["MISSING_EXTERNAL_CASHFLOWS"] if flows_by_date is None else []))
    reasons += _quality_reasons(input_quality)
    reasons += (input_quality or {}).get("snapshot_reason_codes", [])
    for row in navs:
        reasons += row.get("reason_codes", [])
        if row.get("result_status") in {"partial", "unavailable"} and not row.get("reason_codes"):
            reasons.append("INCOMPLETE_SNAPSHOT")
    reasons = list(dict.fromkeys(reasons))
    if len(series) < 2:
        reasons.append("INSUFFICIENT_NAV_SNAPSHOTS")
    if any(value <= 0 for _, value in series):
        reasons.append("NON_POSITIVE_NAV")
    if len(series) != len(navs):
        reasons.append("MISSING_NAV_VALUE")
    if len({day for day, _ in series}) != len(series):
        reasons.append("DUPLICATE_NAV_DATES")
    raw_returns = [current / previous - 1 for (_, previous), (_, current) in zip(series, series[1:])
                   if previous > 0]
    adjusted_returns = [(current - _interval_flow(flows_by_date or {}, start, end)) / previous - 1
                        for (start, previous), (end, current) in zip(series, series[1:]) if previous > 0]

    def drawdown(values):
        if len(values) < 2:
            return None, None, None
        peak = values[0][1]
        peak_date = values[0][0]
        worst = 0.0
        worst_peak = worst_trough = None
        for day, value in values:
            if value > peak:
                peak, peak_date = value, day
            elif peak > 0 and value / peak - 1 < worst:
                worst = value / peak - 1
                worst_peak, worst_trough = peak_date, day
        return round(worst * 100, 4), worst_peak, worst_trough

    def stats(returns):
        count = len(returns)
        mean = sum(returns) / count if count else 0.0
        variance = sum((r - mean) ** 2 for r in returns) / (count - 1) if count > 1 else 0.0
        vol = variance**0.5 * 252**0.5
        downside = [r for r in returns if r < 0]
        dev = (sum(r**2 for r in downside) / len(downside))**0.5 * 252**0.5 if downside else 0.0
        return {"volatility_annual_pct": round(vol * 100, 4) if count > 1 else None,
                "return_annual_pct": round(mean * 252 * 100, 4) if count else None,
                "sharpe": round((mean * 252 - risk_free_rate) / vol, 4) if vol else None,
                "sortino": round((mean * 252 - risk_free_rate) / dev, 4) if dev else None,
                "best_day_pct": round(max(returns) * 100, 4) if returns else None,
                "worst_day_pct": round(min(returns) * 100, 4) if returns else None}

    raw_dd, raw_peak, raw_trough = drawdown(series)
    index = [(series[0][0], 1.0)] if series else []
    for (day, _), ret in zip(series[1:], adjusted_returns):
        index.append((day, index[-1][1] * (1 + ret)))
    dd, peak, trough = drawdown(index)
    values = {"max_drawdown_pct": dd, **stats(adjusted_returns)}
    metrics = {name: _metric(value, reasons, ["nav_snapshots", "external_cashflows"], coverage)
               for name, value in values.items()}
    daily_series = all((datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days == 1
                       for (start, _), (end, _) in zip(series, series[1:]))
    calendar = (input_quality or {}).get("expected_snapshot_dates")
    if calendar is not None and series:
        expected = sorted({day for day in calendar if series[0][0] <= day <= series[-1][0]})
        daily_series = [day for day, _ in series] == expected
    for name in ("volatility_annual_pct", "return_annual_pct", "sharpe", "sortino", "best_day_pct", "worst_day_pct"):
        specific = list(reasons)
        if not daily_series:
            specific.append("NON_DAILY_NAV_SERIES")
        if name in {"volatility_annual_pct", "sharpe"} and len(raw_returns) < 2:
            specific.append("INSUFFICIENT_RETURN_OBSERVATIONS")
        if not specific and name == "sharpe" and values[name] is None:
            specific.append("ZERO_RETURN_VOLATILITY")
        if not specific and name == "sortino" and values[name] is None:
            specific.append("NO_NEGATIVE_RETURNS")
        metrics[name] = _metric(values[name], specific, ["daily_nav_snapshots", "external_cashflows"], coverage)
    raw_stats = stats(raw_returns)
    result = {"input_snapshot_ids": coverage["snapshot_ids"], "calculation_version": _METRIC_VERSION,
              "observations": len(series), "metrics": metrics,
              **{name: metric["value"] for name, metric in metrics.items()},
              "first_date": series[0][0] if series else None,
              "last_date": series[-1][0] if series else None,
              "max_drawdown_peak_date": peak if not reasons else None,
              "max_drawdown_trough_date": trough if not reasons else None,
              "asset_value_drawdown_pct": raw_dd,
              "asset_value_drawdown_peak_date": raw_peak,
              "asset_value_drawdown_trough_date": raw_trough,
              "asset_value_volatility_annual_pct": raw_stats["volatility_annual_pct"] if daily_series else None,
              "asset_value_volatility_per_observation_pct": round(raw_stats["volatility_annual_pct"] / 252**0.5, 4)
                  if raw_stats["volatility_annual_pct"] is not None else None,
              "raw_asset_change_pct": round((series[-1][1] / series[0][1] - 1) * 100, 4)
                  if len(series) > 1 and series[0][1] > 0 else None,
              "raw_best_asset_change_pct": raw_stats["best_day_pct"],
              "raw_worst_asset_change_pct": raw_stats["worst_day_pct"],
              "risk_free_rate_used": risk_free_rate,
              "result_status": "empty" if not navs else "partial" if any(m["status"] != "available" for m in metrics.values()) else "complete"}
    if len(series) < 2:
        result["note"] = "at least 2 NAV snapshots are required"
    return result


def daily_and_monthly_pnl(
    navs: Sequence[dict[str, Any]], flows_by_date: dict[str, float] | None,
    input_quality: dict[str, Any] | None = None,
    flow_reason_codes: Sequence[str] | None = None,
    external_cashflows_coverage: dict[str, Any] | None = None,
    portfolio_scope: str = "equity",
) -> dict[str, Any]:
    """Unknown flows yield null P&L, alongside explicitly named asset changes.

    A row whose two snapshots carry different valuation basis labels keeps its figures and lists
    VALUATION_BASIS_CHANGED in ``warning_codes`` (never in reason_codes, which null the value).
    """
    series = _nav_series(navs)
    steps = {(start, end) for start, end, _, _ in _valuation_basis_steps(navs, portfolio_scope)}
    reasons = list(flow_reason_codes or (["MISSING_EXTERNAL_CASHFLOWS"] if flows_by_date is None else []))
    reasons += _quality_reasons(input_quality)
    daily = []
    monthly: dict[str, float | None] = {}
    raw_monthly: dict[str, float] = {}
    for (start_date, previous), (end_date, current) in zip(series, series[1:]):
        flow = None if flows_by_date is None else _interval_flow(flows_by_date, start_date, end_date)
        pnl = None if flow is None or reasons else current - previous - flow
        ret = round(pnl / previous * 100, 4) if pnl is not None and previous > 0 else None
        coverage = {"snapshot_dates": [start_date, end_date], "external_cashflows": "unknown" if flow is None else "explicit_mapping"}
        if external_cashflows_coverage is not None:
            coverage['ledger_flow_coverage'] = external_cashflows_coverage
        daily.append({"date": end_date, "pnl_krw": round(pnl, 2) if pnl is not None else None,
                      "return_pct": ret, "net_flow_krw": round(flow, 2) if flow is not None else None,
                      "raw_asset_change_krw": round(current - previous, 2),
                      "raw_asset_change_pct": round((current / previous - 1) * 100, 4) if previous > 0 else None,
                      "warning_codes": [VALUATION_BASIS_CHANGED] if (start_date, end_date) in steps else [],
                      "metrics": {"pnl_krw": _metric(pnl, reasons, ["external_cashflows", "nav_snapshots"], coverage),
                                  "return_pct": _metric(ret, reasons, ["external_cashflows", "positive_start_nav"], coverage)}})
        month = end_date[:7]
        monthly[month] = None if pnl is None else round((monthly.get(month) or 0.0) + pnl, 2)
        raw_monthly[month] = round(raw_monthly.get(month, 0.0) + current - previous, 2)
    return {"daily": daily, "monthly": dict(sorted(monthly.items())),
            "raw_monthly_asset_change_krw": dict(sorted(raw_monthly.items()))}


def compute_performance(
    transactions: Sequence[Transaction], navs: Sequence[dict[str, Any]],
    current_nav_krw: float | None, fx: FxRates, start: str, end: str,
    realized: dict[str, Any] | None = None, unrealized_krw: float | None = None,
    risk_free_rate: float = 0.0,
    external_cashflows_coverage: dict[str, Any] | None = None,
    input_quality: dict[str, Any] | None = None,
    ledger_flow_input: dict[str, Any] | None = None,
    current_nav_as_of: str | None = None,
    portfolio_scope: str = "equity",
    current_nav_valuation_basis: str | None = None,
) -> dict[str, Any]:
    """Assemble performance only when required inputs have explicit coverage.

    A recorded deposit alone cannot establish completeness for an entire window.
    Verification must supply a source and date bounds, including verified zero.
    Each boundary NAV states its basis and as-of: start_nav_basis/end_nav_basis are
    'stored_snapshot' (with the snapshot's valuation_at, date and id) or, for the end,
    'live_holdings' (current_nav_krw, as of ``current_nav_as_of``); null with the NAV.
    Endpoints on different valuation basis labels (``current_nav_valuation_basis`` for a live end)
    keep every figure and add warning_codes VALUATION_BASIS_CHANGED with valuation_basis_change.
    """
    realized_totals = (realized or {}).get("totals", {})
    flows = summarize_cashflows(transactions, fx, start, end, external_cashflows_coverage, realized_totals, ledger_flow_input)
    window_navs = sorted([row for row in navs if _in_range(row.get("snapshot_date"), start, end)],
                         key=lambda row: row["snapshot_date"])
    series = _nav_series(window_navs)
    start_nav = series[0][1] if series else None
    end_nav = current_nav_krw if current_nav_krw is not None else series[-1][1] if series else None
    first_date = series[0][0] if series else start
    flows_by_date = flows["flows_by_date"]
    net_flow = None if flows_by_date is None else _interval_flow(flows_by_date, first_date, end)
    quality = {**(input_quality or {})}
    quality["missing_currencies"] = sorted(set(quality.get("missing_currencies", [])) | fx.missing)
    if quality.get("missing_position_valuation"):
        end_nav = None
    flow_reasons = flows["external_cashflows"]["reason_codes"]
    snapshot_reasons = list(quality.get("snapshot_reason_codes", []))
    for row in window_navs:
        snapshot_reasons += row.get("reason_codes", [])
        if row.get("result_status") in {"partial", "unavailable"} and not row.get("reason_codes"):
            snapshot_reasons.append("INCOMPLETE_SNAPSHOT")
    quality["snapshot_reason_codes"] = list(dict.fromkeys(snapshot_reasons))
    reasons = flow_reasons + _quality_reasons(quality) + quality["snapshot_reason_codes"]
    if ledger_flow_input is not None:
        if not series or series[0][0] != start:
            reasons.append('MISSING_START_NAV')
        if not series or series[-1][0] != end:
            reasons.append('MISSING_END_NAV')
    boundary_reasons = reasons + ([] if start_nav is not None else ["MISSING_START_NAV"]) + ([] if end_nav is not None else ["MISSING_END_NAV"])
    if start_nav is not None and start_nav <= 0:
        boundary_reasons.append("NON_POSITIVE_START_NAV")
    if len(series) != len(window_navs):
        boundary_reasons.append("MISSING_NAV_VALUE")
        reasons.append("MISSING_NAV_VALUE")
    if any(value <= 0 for _, value in series):
        reasons.append("NON_POSITIVE_NAV")
    if len({day for day, _ in series}) != len(series):
        reasons.append("DUPLICATE_NAV_DATES")
        boundary_reasons.append("DUPLICATE_NAV_DATES")
    profit = end_nav - start_nav - net_flow if not boundary_reasons else None
    denominator = start_nav + max(net_flow, 0) if profit is not None else None
    simple = round(profit / denominator * 100, 4) if denominator else None
    twr = time_weighted_return(window_navs, flows_by_date) if not reasons else None
    cashflows = [(first_date, -start_nav)] if start_nav is not None else []
    cashflows += [(day, -amount) for day, amount in (flows_by_date or {}).items() if first_date < day <= end]
    if end_nav is not None:
        cashflows.append((end, end_nav))
    mwr = xirr(cashflows) if not boundary_reasons else None
    coverage = {"external_cashflows": flows["external_cashflows"], "input_quality": quality,
                "snapshot_dates": [day for day, _ in series], "nav_observations": len(series),
                "snapshot_ids": [row["snapshot_id"] for row in window_navs if row.get("snapshot_id")],
                "snapshot_calculation_versions": sorted({row["calculation_version"] for row in window_navs if row.get("calculation_version")})}
    metrics = {
        "simple_return_pct": _metric(simple, boundary_reasons, ["start_nav", "end_nav", "external_cashflows"], coverage),
        "profit_krw": _metric(profit, boundary_reasons, ["start_nav", "end_nav", "external_cashflows"], coverage),
        "twr_pct": _metric(round(twr * 100, 4) if twr is not None else None,
                           reasons + (["INSUFFICIENT_NAV_SNAPSHOTS"] if len(series) < 2 else []),
                           ["nav_snapshots", "external_cashflows"], coverage),
        "xirr_pct": _metric(round(mwr * 100, 4) if mwr is not None else None, boundary_reasons,
                            ["start_nav", "end_nav", "dated_external_cashflows"], coverage),
    }
    realized_krw = realized_totals.get("realized_pnl_krw")
    # New ledgers carry sale-window reasons; retain the legacy fallback for
    # callers supplying the earlier shape, which only had a symbol warning.
    realized_reasons = realized_totals.get("reason_codes", ["INCOMPLETE_COST_BASIS"] if (realized or {}).get("incomplete_cost_basis") else []) + _quality_reasons({key: value for key, value in quality.items() if key != "missing_position_valuation"})
    metrics["realized_krw"] = _metric(realized_krw, realized_reasons, ["complete_cost_basis", "complete_ledger", "fx"], coverage)
    unrealized_reasons = _quality_reasons(quality) + (["INCOMPLETE_HOLDINGS_COST_BASIS"] if quality.get("incomplete_holdings_cost_basis") else [])
    metrics["unrealized_krw"] = _metric(unrealized_krw, unrealized_reasons, ["holdings", "cost_basis", "fx"], coverage)
    raw_change = end_nav - start_nav if start_nav is not None and end_nav is not None else None
    return {
        "input_snapshot_ids": coverage["snapshot_ids"], "calculation_version": _METRIC_VERSION,
        "methodology": {"basis_scope": "per_account"},
        "period": {"start": start, "end": end},
        "start_nav_krw": round(start_nav, 2) if start_nav is not None else None,
        "end_nav_krw": round(end_nav, 2) if end_nav is not None else None,
        **_nav_endpoint_basis("start", start_nav, series[0] if series else None, window_navs),
        **(_nav_endpoint_basis("end", end_nav, series[-1] if series else None, window_navs)
           if current_nav_krw is None or end_nav is None else
           {"end_nav_basis": "live_holdings", "end_nav_as_of": current_nav_as_of,
            "end_nav_snapshot_date": None, "end_nav_snapshot_id": None}),
        "profit_krw": metrics["profit_krw"]["value"],
        "raw_asset_change_krw": round(raw_change, 2) if raw_change is not None else None,
        "raw_asset_change_pct": round(raw_change / start_nav * 100, 4) if raw_change is not None and start_nav > 0 else None,
        "flows": flows, "metrics": metrics,
        "pnl": {"realized_krw": metrics["realized_krw"]["value"],
                "unrealized_krw": metrics["unrealized_krw"]["value"],
                **_money_fields(flows),
                "fx_krw": (realized or {}).get("totals", {}).get("realized_fx_krw")},
        "returns": {**{name: metrics[name]["value"] for name in ("simple_return_pct", "twr_pct", "xirr_pct")}, "mwr_equals_xirr": True},
        "risk": nav_statistics(window_navs, risk_free_rate, flows_by_date, quality, flow_reasons,
                               external_cashflows_coverage),
        "nav_observations": len(window_navs), "coverage": coverage,
        **valuation_basis_change(window_navs, portfolio_scope,
                                 current_nav_valuation_basis if current_nav_krw is not None and end_nav is not None else None,
                                 end),
        "result_status": "partial" if any(m["status"] != "available" for m in metrics.values()) else "complete",
        "connector_refresh_required": True,
    }


# ── Portfolio summary ────────────────────────────────────


def settlement_nav_bridge(holdings, cash, fx, reported_total):
    """Reference bridge over broker balance fields; never add ledger trades again."""
    adjustments = []
    reasons = []
    unrounded_adjustments = []
    broker_totals = []
    accounts = []
    for balance in cash:
        accounts.append(balance.to_dict())
        if fx.rate(balance.currency) is None:
            reasons.append("MISSING_FX_RATE")
        if balance.broker_reported_totals:
            broker_totals.append({"account": balance.account,
                                  "source": "CTRP6504R.output3",
                                  "fields": balance.broker_reported_totals,
                                  "scope": "broker_account_scope_not_reconciled"})
            reasons.append("BROKER_TOTAL_SCOPE_NOT_RECONCILED")
        if balance.native_cash_includes_unsettled is None:
            reasons.append("UNKNOWN_NATIVE_CASH_SETTLEMENT_BASIS")
            continue
        if balance.positions_basis not in {"trade_date", "trade_date_reference_assumption"}:
            reasons.append("UNKNOWN_POSITION_SETTLEMENT_BASIS")
            continue
        if balance.positions_basis == "trade_date_reference_assumption":
            reasons.append("SETTLEMENT_BASIS_REFERENCE_ASSUMPTION")
        for name, sign in (("unsettled_receivables", 1), ("unsettled_payables", -1)):
            amount = getattr(balance, name)
            if amount is None:
                reasons.append("MISSING_" + name.upper())
                continue
            if amount < 0:
                reasons.append("INVALID_" + name.upper())
                continue
            if not amount or balance.native_cash_includes_unsettled:
                continue
            converted = fx.to_krw(sign * amount, balance.currency)
            if converted is not None:
                unrounded_adjustments.append(converted)
            adjustments.append({"account": balance.account, "provider": balance.provider,
                                "amount": sign * amount, "currency": balance.currency,
                                "amount_krw": round(converted, 2) if converted is not None else None,
                                "reason": name.upper(), "source_field": balance.source_fields.get(name),
                                "basis": balance.settlement_basis_reason})
    for holding in holdings:
        if holding.quantity > 0 and holding.market_value is None:
            reasons.append("MISSING_POSITION_VALUATION")
        if fx.rate(holding.currency) is None:
            reasons.append("MISSING_FX_RATE")
        if holding.quantity > 0 and not any(c.account == holding.account for c in cash):
            reasons.append("MISSING_ACCOUNT_CASH_SETTLEMENT_DATA")
    fatal = any(reason in reasons for reason in ("MISSING_FX_RATE", "MISSING_POSITION_VALUATION"))
    adjustment_total = None if any(a["amount_krw"] is None for a in adjustments) else round(sum(unrounded_adjustments), 2)
    adjusted = None if fatal or adjustment_total is None else round(reported_total + sum(unrounded_adjustments), 2)
    return {"reported_total_assets_krw": reported_total,
            "adjustments": adjustments, "adjustment_total_krw": adjustment_total,
            "rounding_residual_krw": round(adjusted - reported_total - adjustment_total, 2)
                if adjusted is not None else None,
            "settlement_adjusted_nav_krw": adjusted,
            "label": "Reference settlement-adjusted value; not official account NAV or withdrawable cash",
            "cash_balances": accounts, "broker_reported_totals": broker_totals,
            "reconciliation": {"status": "unavailable" if adjusted is None else "partial" if reasons else "reference_only",
                               "difference_reasons": list(dict.fromkeys(reasons)),
                               "difference_from_reported_krw": adjustment_total,
                               "difference_from_official_nav_krw": None,
                               "official_nav_comparison_reason": "Broker total scope and FX basis are not reconciled."}}


def _mark_basis(positions: Sequence[dict[str, Any]]) -> str:
    """The marks a KRW total used: canonical_marks only if every valued row is at a
    timed quote, mixed_marks if some are at a broker price, else broker_marks. Unvalued rows are not
    in the total. daily_nav labels any non-canonical total with its broker-marks basis."""
    used = {p["mark"]["status"] for p in positions if p.get("market_value_krw") is not None}
    return "canonical_marks" if used == {"timed"} else "mixed_marks" if "timed" in used else "broker_marks"


def summarize_portfolio(
    holdings: Sequence[Holding],
    cash: Sequence[CashBalance],
    fx: FxRates,
    realized: dict[str, Any] | None = None,
    quotes: dict[str, Any] | None = None,
    perps: dict[str, Any] | None = None,
    perp_fx: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Total assets, cost, P&L, and per-currency/account breakdowns in KRW.

    Stock value uses the consolidated canonical marks (consolidate_holdings), and
    so does each account's market_value_krw; broker figures stay alongside.
    Perps reach only underlying_exposure, never total_assets_krw.
    """
    consolidated = consolidate_holdings(holdings, fx, quotes=quotes, perps=perps, perp_fx=perp_fx)
    marks = {(p["symbol"], p["currency"]): p["mark"]["price"] for p in consolidated["positions"]}
    totals = consolidated["totals"]
    stock_value = totals["market_value_krw"] or 0.0
    cost_basis = totals["cost_basis_krw"] or 0.0
    unrealized = totals["unrealized_pnl_krw"]

    cash_by_currency: dict[str, float] = {}
    cash_krw = 0.0
    for balance in cash:
        currency = balance.currency.upper()
        cash_by_currency[currency] = round(cash_by_currency.get(currency, 0.0) + balance.amount, 4)
        cash_krw += fx.to_krw(balance.amount, currency) or 0.0

    by_account: dict[str, dict[str, Any]] = {}
    for holding in holdings:
        if holding.quantity <= 0:
            continue
        entry = by_account.setdefault(
            holding.account,
            {"provider": holding.provider, "position_count": 0, "market_value_krw": 0.0, "cost_basis_krw": 0.0,
             "broker_reported_market_value_krw": 0.0},
        )
        entry["position_count"] += 1
        price = marks.get((holding.symbol.upper(), holding.currency.upper()))
        value = price * holding.quantity if price is not None else holding.market_value
        entry["market_value_krw"] += fx.to_krw(value, holding.currency) or 0.0
        entry["broker_reported_market_value_krw"] += fx.to_krw(holding.market_value, holding.currency) or 0.0
        entry["cost_basis_krw"] += fx.to_krw(holding.cost_basis, holding.currency) or 0.0
    for balance in cash:
        entry = by_account.setdefault(
            balance.account,
            {"provider": balance.provider, "position_count": 0, "market_value_krw": 0.0, "cost_basis_krw": 0.0},
        )
        entry["cash_krw"] = round(entry.get("cash_krw", 0.0) + (fx.to_krw(balance.amount, balance.currency) or 0.0), 2)
    for entry in by_account.values():
        entry["market_value_krw"] = round(entry["market_value_krw"], 2)
        if "broker_reported_market_value_krw" in entry:
            entry["broker_reported_market_value_krw"] = round(entry["broker_reported_market_value_krw"], 2)
        entry["cost_basis_krw"] = round(entry["cost_basis_krw"], 2)
        entry["total_krw"] = round(entry["market_value_krw"] + entry.get("cash_krw", 0.0), 2)

    realized_totals = (realized or {}).get("totals", {})
    return {
        "total_assets_krw": round(stock_value + cash_krw, 2),
        "methodology": {"basis_scope": "per_account"},
        "nav_bridge": settlement_nav_bridge(holdings, cash, fx, round(stock_value + cash_krw, 2)),
        "stock_value_krw": round(stock_value, 2),
        "broker_reported_stock_value_krw": totals["broker_reported_market_value_krw"],
        "mark_policy": consolidated["mark_policy"],
        "mark_status_counts": consolidated["mark_status_counts"],
        # Label plumbing: the marks the total actually used, for daily_nav's basis label.
        # Without quotes (or with every quote rejected) each symbol is at one broker price.
        "mark_basis": _mark_basis(consolidated["positions"]),
        "cash_krw": round(cash_krw, 2),
        "cost_basis_krw": round(cost_basis, 2),
        "unrealized_pnl_krw": unrealized,
        "unrealized_pnl_pct": round(unrealized / cost_basis * 100, 4) if (unrealized is not None and cost_basis) else None,
        "realized_pnl_krw": realized_totals.get("realized_pnl_krw"),
        "realized_pnl_status": realized_totals.get("status", "unavailable"),
        "realized_pnl_reason_codes": realized_totals.get("reason_codes", ["REALIZED_PNL_NOT_REQUESTED"] if realized is None else []),
        "realized_pnl_complete_basis": realized_totals.get("complete_basis"),
        "realized_pnl_incomplete_basis": realized_totals.get("incomplete_basis"),
        "dividends_krw": realized_totals.get("dividends_krw"),
        "fees_krw": realized_totals.get("fees_krw"),
        "taxes_krw": realized_totals.get("taxes_krw"),
        "fees_by_currency": realized_totals.get("fees_by_currency"),
        "taxes_by_currency": realized_totals.get("taxes_by_currency"),
        "fees_status": realized_totals.get("fees_status", "unknown"),
        "taxes_status": realized_totals.get("taxes_status", "unknown"),
        "cost_fx": realized_totals.get("cost_fx"),
        "position_count": consolidated["position_count"],
        "by_currency": consolidated["by_currency"],
        "by_market": consolidated["by_market"],
        "underlying_exposure": consolidated["underlying_exposure"],
        "by_account": dict(sorted(by_account.items(), key=lambda kv: kv[1]["total_krw"], reverse=True)),
        "cash_by_currency": dict(sorted(cash_by_currency.items())),
        "fx": fx.to_dict(),
    }


# Quantity reconciliation (engine-only opening/adjustment inputs).

def _reconciliation_key(row):
    return row["account"], row["symbol"], row["currency"]


def reconcile_quantities(transactions, holdings, *, known_accounts=None,
                         opening_positions=(), adjustments=()):
    """Compare account/symbol/currency quantities with explicit opening evidence.

    Signed transfer/corporate-action adjustments need a source; cash TRANSFER
    records alone cannot establish share direction. SPLIT uses the model's
    post-split total quantity convention. Unmodeled events remain unexplained.

    The cost engine's own quantity (replay_book_quantities: same fill order,
    inferred openings for oversells) is compared with the broker too; a
    difference is a break with ENGINE_QUANTITY_DRIFT, whose realized P&L the
    engine then reports as null (books with verified openings or adjustments,
    which the engine does not model, keep the evidence check only). Sales
    before the ledger's first buy are INFERRED_OPENING_POSITION in the engine;
    the observed-net check still reports them.
    """
    engine_books = replay_book_quantities(transactions)
    current = defaultdict(float)
    txs = defaultdict(list)
    opening = {}
    changes = defaultdict(list)
    for h in holdings:
        current[h.account, h.symbol, h.currency] += h.quantity
    known = set(known_accounts if known_accounts is not None else [h.account for h in holdings])
    for t in transactions:
        if t.symbol and t.tx_type in {TxType.BUY, TxType.SELL, TxType.TRANSFER, TxType.SPLIT,
                                     TxType.SPINOFF, TxType.MERGER, TxType.TICKER_CHANGE}:
            txs[t.account, t.symbol, t.currency].append(t)
    for row in opening_positions:
        key = _reconciliation_key(row)
        if key in opening:
            raise ValueError("Duplicate opening position")
        quantity = float(row["quantity"])
        if not math.isfinite(quantity) or quantity < 0:
            raise ValueError("Opening quantity must be finite and nonnegative")
        if row.get("status") == "verified" and row.get("source"):
            opening[key] = dict(row)
    for row in adjustments:
        if row.get("kind") not in {"transfer", "corporate_action"} or not row.get("source"):
            raise ValueError("Signed quantity adjustment needs kind and source evidence")
        if not math.isfinite(float(row["quantity_delta"])):
            raise ValueError("Adjustment quantity must be finite")
        changes[_reconciliation_key(row)].append(dict(row))

    positions, breaks = [], []
    for key in sorted(set(current) | set(txs) | set(opening) | set(changes)):
        account, symbol, currency = key
        entries = sorted(txs[key], key=_replay_order)
        initial = opening.get(key)
        opening_qty = float(initial["quantity"]) if initial else None
        buys = sum(t.quantity for t in entries if t.tx_type == TxType.BUY)
        sells = sum(t.quantity for t in entries if t.tx_type == TxType.SELL)
        transfers = sum(float(r["quantity_delta"]) for r in changes[key] if r["kind"] == "transfer")
        corporate = sum(float(r["quantity_delta"]) for r in changes[key] if r["kind"] == "corporate_action")
        running = opening_qty or 0.0
        unknown_events = []
        unknown_transfer = unknown_corporate = False
        covered_events = {r.get("transaction_id") for r in changes[key] if r.get("transaction_id")}
        for t in entries:
            if t.tx_type == TxType.BUY:
                running += t.quantity
            elif t.tx_type == TxType.SELL:
                running -= t.quantity
            elif t.tx_type == TxType.SPLIT:
                # Without opening evidence the pre-split balance is unknown.
                if opening_qty is None or changes[key]:
                    # Undated explicit adjustments cannot establish a pre-split
                    # balance. Keep the event unresolved instead of guessing.
                    unknown_events.append(t.tx_id)
                    unknown_corporate = True
                else:
                    delta = t.quantity - running
                    corporate += delta
                    running = t.quantity
            elif t.tx_id not in covered_events and t.execution_id not in covered_events:
                unknown_events.append(t.tx_id)
                if t.tx_type == TxType.TRANSFER:
                    unknown_transfer = True
                else:
                    unknown_corporate = True
        net = buys - sells + transfers + corporate
        expected = opening_qty + net if opening_qty is not None and not unknown_events else None
        ending = current[key] if account in known else None
        # For an unknown opening, this is a difference from observed net trades,
        # not an asserted zero opening or a proposed balancing transaction.
        difference = ending - (expected if expected is not None else net) if ending is not None else None
        engine_book = engine_books.get((account, symbol.upper(), currency.upper()), {})
        engine_qty = engine_book.get("quantity", 0.0)
        inferred_qty = engine_book.get("inferred_opening_qty", 0.0)
        engine_difference = ending - engine_qty if ending is not None else None
        # The engine models neither verified openings nor signed adjustments; books carrying such
        # explicit evidence keep the evidence-based check below (engine fields stay reported).
        engine_drift = (ending is not None and initial is None and not changes[key]
                        and not math.isclose(engine_difference, 0, abs_tol=1e-8))
        is_break = ending is not None and (bool(unknown_events) or engine_drift
                                           or not math.isclose(difference, 0, abs_tol=1e-8))
        status = "unexplained" if is_break else "unavailable" if ending is None else "matched" if initial else "matched_observed"
        row = {"account": account, "symbol": symbol, "currency": currency,
               "opening_qty": opening_qty, "opening_cost_basis": initial.get("cost_basis") if initial else None,
               "opening_status": "verified" if initial else "unknown",
               "buys": buys, "sells": sells,
               "transfers": None if unknown_transfer else transfers,
               "corporate_actions": None if unknown_corporate else corporate,
               "observed_net_qty": net, "expected_ending_qty": expected, "ending_qty": ending,
               "quantity_difference": difference,
               "engine_qty": round(engine_qty, 8), "engine_inferred_opening_qty": round(inferred_qty, 8),
               "engine_quantity_difference": round(engine_difference, 8) if engine_difference is not None else None,
               "status": status,
               "reason_codes": (["RECONCILIATION_BREAK"] if is_break else [])
                    + (["ENGINE_QUANTITY_DRIFT"] if engine_drift else [])
                    + (["MISSING_OPENING_POSITION"] if initial is None else [])
                    + (["INFERRED_OPENING_POSITION"] if inferred_qty > 1e-9 else [])
                    + (["UNRESOLVED_QUANTITY_EVENT"] if unknown_events else [])
                    + (["HOLDINGS_UNAVAILABLE"] if ending is None else []),
               "provenance": {"transaction_ids": [t.execution_id or t.tx_id for t in entries],
                   "transaction_sources": sorted({t.source for t in entries}),
                   "opening": initial, "adjustments": changes[key], "unresolved_events": unknown_events}}
        positions.append(row)
        if is_break:
            breaks.append(row)
    evidence = {"transactions": [t.to_dict() for t in transactions],
                "holdings": [h.to_dict() for h in holdings],
                "opening": list(opening_positions), "adjustments": list(adjustments)}
    dataset_id = hashlib.sha256(json.dumps(evidence, sort_keys=True, default=str).encode()).hexdigest()
    return {"status": "unreconciled" if breaks else "unavailable" if not known else "matched_observed",
            "reconciliation_breaks": breaks, "unexplained_quantity_breaks": len(breaks),
            "positions": positions, "historical_coverage": "unknown",
            "engine_quantity_drift_breaks": sum("ENGINE_QUANTITY_DRIFT" in row["reason_codes"] for row in breaks),
            "provenance": {"dataset_id": dataset_id, "calculation_version": "quantity_reconciliation_v2"},
            "limitations": ["Opening positions are unknown unless explicitly verified.",
                            "A quantity match does not prove complete historical transactions or cost basis."]}


def reconcile_collections(ledger, positions, symbol=None):
    """Only successful, untruncated holdings scopes can establish absent = zero.

    Current holdings must not be compared to a ledger ending on a historic date.
    """
    known = [name for name, h in positions.health.items()
             if h.get("status") in {"ok", "empty"}
             and (name.startswith("toss:") and name.count(":") == 1
                  or name.startswith("kis:") and name.endswith((":domestic", ":overseas")))
             and not any(name.startswith("kis:") and k.startswith(name + ":") and v.get("status") not in {"ok", "empty"}
                         for k, v in positions.health.items())
             and name not in positions.truncated_sources]
    requested = getattr(ledger, "requested_window", {})
    as_of = local_calendar_date(positions.fetched_at)
    end = requested.get("end")
    if end and as_of and end != as_of:
        known = []
    holdings = [h for h in positions.holdings if not symbol or h.symbol.upper() == symbol.upper()]
    result = reconcile_quantities(ledger.transactions, holdings, known_accounts=known)
    result["requested_window"] = requested
    result["holdings_as_of"] = positions.fetched_at
    result["fetch_complete"] = getattr(ledger, "fetch_complete", False)
    result["sources_failed"] = ledger.failed_sources
    result["truncated_sources"] = ledger.truncated_sources
    if end and as_of and end != as_of:
        result["reason_codes"] = ["HOLDINGS_AS_OF_MISMATCH"]
    ledger.reconciliation = result
    return result


def realized_input_quality(ledger, positions, symbol=None, *, report=True):
    """Realized-engine input for every realized P&L tool: (input_quality, engine_quantity_check).

    Whether a book's realized P&L is known depends on the engine's quantity check against current
    holdings (ENGINE_QUANTITY_DRIFT books have null P&L), so the check never depends on whether the
    caller asked for the quantity reconciliation report: get_realized_pnl (either ``reconcile``),
    get_trade_review, the summary's realized block and performance pass the same drift books to
    compute_realized_pnl. report=False (get_realized_pnl reconcile=False) passes only the drift
    breaks, the ones that change a figure; the reconciliation report stays not requested.
    """
    reconciliation = reconcile_collections(ledger, positions, symbol)
    quality = ledger_input_quality(ledger)
    drift = [row for row in reconciliation["reconciliation_breaks"] if "ENGINE_QUANTITY_DRIFT" in row["reason_codes"]]
    if not report:
        ledger.reconciliation = {}
        quality["reconciliation_breaks"] = drift
    unchecked = sum("HOLDINGS_UNAVAILABLE" in row["reason_codes"] for row in reconciliation["positions"])
    check = {"status": "drift" if drift else "unavailable" if reconciliation["status"] == "unavailable"
             else "partial" if unchecked else "matched",
             "reason_codes": (["ENGINE_QUANTITY_DRIFT"] if drift else [])
             + list(reconciliation.get("reason_codes") or []) + (["HOLDINGS_UNAVAILABLE"] if unchecked else []),
             "drift_books": [{field: row[field] for field in ("account", "symbol", "currency", "engine_qty", "ending_qty")}
                             for row in drift],
             "unchecked_books": unchecked, "holdings_as_of": positions.fetched_at}
    return quality, check


# ── Minimal trade review ─────────────────────


def _review_pnl(sales, source_reasons=()):
    """Aggregate engine sales; incomplete amounts remain diagnostic estimates."""
    reasons = sorted(set(source_reasons) | {r for sale in sales for r in sale['reason_codes']})
    diagnostic = _rounded_account_by_currency(sales, 'realized_pnl', 4)
    return {
        'realized_pnl_by_currency': None if set(reasons) - {'MISSING_FX_RATE'} else diagnostic,
        'realized_pnl_krw': None if reasons or any(s['realized_pnl_krw'] is None for s in sales)
        else _rounded_account_total(sales, 'realized_pnl_krw', 2),
        'diagnostic_pnl_by_currency': diagnostic,
        'cost_basis_complete': all(s['cost_basis_complete'] for s in sales),
        'sell_count': len(sales),
        'status': 'partial' if reasons else 'complete',
        'reason_codes': reasons,
    }


def _review_cycles(rows, sales, start, end):
    """Track acquisition quantities/dates only; all cost amounts come from replay.

    Moving average does not identify disposed lots. Origin shares and durations
    therefore use proportional quantity attribution, never FIFO or inferred intent.
    """
    sale_index = {(s['trade_date'], s['tx_id']): s for s in sales}
    inventories, cycles, closed, decorated = {}, {}, [], []
    for tx in sorted(rows, key=_replay_order):
        if tx.trade_date > end or not tx.symbol:
            continue
        key = (tx.symbol.upper(), tx.currency.upper())
        lots = inventories.setdefault(key, [])  # [date, remaining quantity]
        available = sum(lot[1] for lot in lots)
        if tx.tx_type is TxType.SPLIT:
            if available > 0 and tx.quantity > 0:
                for lot in lots:
                    lot[1] *= tx.quantity / available
            continue
        if tx.tx_type in {TxType.TRANSFER, TxType.SPINOFF, TxType.MERGER, TxType.TICKER_CHANGE}:
            inventories[key] = [[None, available]] if available else []
            cycles.pop(key, None)
            continue
        if tx.quantity <= 0 or tx.tx_type not in {TxType.BUY, TxType.SELL}:
            continue
        if tx.tx_type is TxType.BUY:
            if available <= 1e-9:
                cycles[key] = {'account': tx.account, 'symbol': key[0], 'currency': key[1],
                               'opened_on': tx.trade_date, 'sales': [], 'valid': True}
            lots.append([tx.trade_date, tx.quantity])
            continue
        sale = sale_index.get((tx.trade_date, tx.tx_id))
        if sale is not None:
            origin_qty = defaultdict(float)
            weighted_days = 0.0
            matched = min(available, tx.quantity)
            for acquired, qty in lots:
                removed = qty * matched / available if available else 0
                origin = 'unknown_origin' if acquired is None else 'new_in_window' if acquired >= start else 'pre_window_liquidation'
                origin_qty[origin] += removed
                if acquired is not None:
                    weighted_days += removed * (date.fromisoformat(tx.trade_date) - date.fromisoformat(acquired)).days
            if tx.quantity > available + 1e-9:
                origin_qty['unknown_origin'] += tx.quantity - available
            sale = {**sale, 'cohort_quantities': dict(origin_qty),
                    'holding_period_days': None if origin_qty.get('unknown_origin') else round(weighted_days / tx.quantity, 4),
                    'holding_period_reason_codes': ['ACQUISITION_NOT_OBSERVED'] if origin_qty.get('unknown_origin') else []}
            decorated.append(sale)
        cycle = cycles.get(key)
        if cycle:
            if sale is not None:
                cycle['sales'].append(sale)
            if tx.quantity > available + 1e-9:
                cycle['valid'] = False
            if available <= tx.quantity + 1e-9:
                cycles.pop(key)
                if cycle['valid'] and start <= cycle['opened_on'] <= end and start <= tx.trade_date <= end:
                    pnl = _review_pnl(cycle['sales'])
                    closed.append({k: v for k, v in cycle.items() if k not in {'sales', 'valid'}} | {
                        'closed_on': tx.trade_date,
                        'holding_period_days': (date.fromisoformat(tx.trade_date) - date.fromisoformat(cycle['opened_on'])).days,
                        'realized_pnl': pnl['realized_pnl_by_currency'].get(key[1]) if pnl['realized_pnl_by_currency'] is not None else None,
                        'cost_basis_complete': pnl['cost_basis_complete'],
                        'status': pnl['status'], 'reason_codes': pnl['reason_codes'],
                        # Internal (stripped from output): native P&L before unreported costs, for win_rate_all.
                        '_diagnostic_pnl': pnl['diagnostic_pnl_by_currency'].get(key[1]),
                    })
        remaining = max(available - tx.quantity, 0)
        inventories[key] = [[day, qty * remaining / available] for day, qty in lots] if remaining and available else []
    return decorated, closed


def _review_win_rate_coverage(cycles, eligible_cycles, break_books):
    """Explain the existing eligibility selection without changing its rules."""
    precedence = ('QUANTITY_BREAK_BOOK', 'INCOMPLETE_COST_BASIS', 'MISSING_PROCEEDS',
        'TAXES_UNSUPPORTED', 'TAXES_UNKNOWN', 'FEES_UNSUPPORTED', 'FEES_UNKNOWN')
    eligible_ids = {id(cycle) for cycle in eligible_cycles}
    primary_counts, any_counts, sole_counts = defaultdict(int), defaultdict(int), defaultdict(int)
    for cycle in cycles:
        if id(cycle) in eligible_ids:
            continue
        reasons = set(cycle['reason_codes'])
        key = (cycle['account'], cycle['symbol'].upper(), cycle['currency'].upper())
        if key in break_books:
            # ENGINE_QUANTITY_DRIFT is the engine's statement of this same book break.
            reasons.discard('ENGINE_QUANTITY_DRIFT')
            reasons.add('QUANTITY_BREAK_BOOK')
        if not cycle['cost_basis_complete'] and 'ENGINE_QUANTITY_DRIFT' not in cycle['reason_codes']:
            reasons.add('INCOMPLETE_COST_BASIS')
        blockers = reasons - {'MISSING_FX_RATE'}
        if not blockers:
            blockers.add('REALIZED_PNL_UNAVAILABLE')
            reasons.add('REALIZED_PNL_UNAVAILABLE')
        primary = next((code for code in precedence if code in blockers), min(blockers))
        primary_counts[primary] += 1
        for code in reasons:
            any_counts[code] += 1
        if len(blockers) == 1:
            sole_counts[next(iter(blockers))] += 1
    return {'eligible': len(eligible_cycles), 'closed_round_trips': len(cycles),
        'excluded_by_reason': dict(sorted(primary_counts.items())),
        'excluded_any_reason': dict(sorted(any_counts.items())),
        'excluded_sole_reason': dict(sorted(sole_counts.items()))}


# Round trips whose only blocker is an unreported tax: win_rate_all counts them (taxes_unknown).
_TAXES_UNKNOWN_CODES = frozenset({'TAXES_UNSUPPORTED', 'TAXES_UNKNOWN'})
REVIEW_TOP_SYMBOLS = 10
_REVIEW_SUMMARY_SYMBOL_FIELDS = (
    'symbol', 'currency', 'round_trip_count', 'realized_pnl_krw', 'open_unrealized_pnl_krw',
    'realized_win_rate', 'realized_win_rate_denominator', 'win_rate_all', 'win_rate_all_eligible',
    'win_rate_all_taxes_unknown', 'reason_codes', 'result_status')


def _review_rank_value(row, fx):
    """|realized P&L| in KRW, for ranking only: the KRW total, else the native diagnostic at the current rate."""
    realized = row['realized']
    if realized['realized_pnl_krw'] is not None:
        return abs(realized['realized_pnl_krw'])
    values = [fx.to_krw(amount, currency) for currency, amount in (realized['diagnostic_pnl_by_currency'] or {}).items()
              if amount is not None]
    return abs(sum(v for v in values if v is not None))


def _review_summary_symbols(symbols, fx):
    """Summary body: the top symbols by |realized P&L| and every symbol with a reason, by its codes only."""
    ranked = sorted(symbols, key=lambda row: (-_review_rank_value(row, fx), row['symbol'], row['currency']))
    top = [{'rank': rank, **{field: row[field] for field in _REVIEW_SUMMARY_SYMBOL_FIELDS},
            'realized_pnl_by_currency': row['realized']['realized_pnl_by_currency'],
            'diagnostic_pnl_by_currency': row['realized']['diagnostic_pnl_by_currency'],
            'sell_count': row['realized']['sell_count']}
           for rank, row in enumerate(ranked[:REVIEW_TOP_SYMBOLS], 1)]
    groups = {}
    for row in symbols:
        if row['reason_codes'] or row['realized_pnl_krw'] is None:
            codes = tuple(row['reason_codes']) or ('REALIZED_PNL_UNAVAILABLE',)
            group = groups.setdefault(codes, {'reason_codes': list(codes), 'count': 0, 'symbols': []})
            group['count'] += 1
            group['symbols'].append([row['symbol'], row['currency']])
    return {'symbols': top, 'symbol_count': len(symbols), 'omitted_symbols': len(symbols) - len(top),
            'symbol_ranking': 'abs realized P&L in KRW (the native diagnostic at the current FX rate when '
                              'the KRW figure is null); the full per-symbol table and cohorts are detail_level="full"',
            'symbol_reason_groups': sorted(groups.values(), key=lambda g: (-g['count'], g['reason_codes']))}


def compute_trade_review(ledger, positions, fx, start, end, *, symbol='', detail_level='summary'):
    """Observed activity review; reuse accounting, FX, positions and reconciliation.

    Cycles require both legs in the window, within one account/symbol/currency.
    No quantity reconciliation result proves completeness of historical cycles.
    Win rates: ``realized_win_rate`` (= ``win_rate_strict``) counts round trips with
    known basis and costs; ``win_rate_all`` also counts those whose only blocker is
    an unreported tax (``win_rate_all_taxes_unknown``), at their P&L before that tax.
    Each states its eligible count. summary returns totals without cohorts and
    _review_summary_symbols; full returns every per-symbol row with cohorts.
    """
    if detail_level not in {'summary', 'full'}:
        raise ValueError('detail_level must be summary or full')
    quality, _ = realized_input_quality(ledger, positions, symbol or None)
    reconciliation = ledger.reconciliation
    rows = [t for t in ledger.transactions if t.trade_date <= end and (not symbol or (t.symbol or '').upper() == symbol.upper())]
    holdings = [h for h in positions.holdings if h.quantity > 0 and (not symbol or h.symbol.upper() == symbol.upper())]
    source_reasons = _quality_reasons(quality)
    if quality['source_status'] == 'unavailable':
        source_reasons.append('LEDGER_UNAVAILABLE')
    position_reasons = (['FAILED_SOURCES'] if positions.failed_sources else []) + (['TRUNCATED_SOURCES'] if positions.truncated_sources else [])
    if collection_result_status([positions], len(holdings)) == 'unavailable':
        position_reasons.append('POSITIONS_UNAVAILABLE')
    all_sales, all_cycles, account_status = [], [], []
    for account in sorted({t.account for t in rows}):
        account_rows = [t for t in rows if t.account == account]
        engine = compute_realized_pnl_with_sales(account_rows, fx, start, end, input_quality=quality)
        account_status.append({'account': account, 'status': engine['totals']['status'],
                               'reason_codes': engine['totals']['reason_codes'],
                               'complete_basis': engine['totals']['complete_basis'],
                               'incomplete_basis': engine['totals']['incomplete_basis']})
        sales, cycles = _review_cycles(account_rows, engine['sales'], start, end)
        all_sales.extend(sales)
        all_cycles.extend(cycles)
    window_rows = [t for t in rows if start <= t.trade_date <= end]

    def metrics(selected_rows, selected_sales, selected_cycles, selected_holdings, basis_rows):
        reasons = set(source_reasons + position_reasons + reconciliation.get('reason_codes', []))
        realized = _review_pnl(selected_sales, source_reasons)
        reasons.update(realized['reason_codes'])
        money = aggregate_ledger_money(selected_rows, fx, start, end)
        for field in ('fees', 'taxes'):
            reasons.update(money[field + '_reason_codes'] if selected_rows else [])
        trade_rows = [t for t in selected_rows if t.symbol and t.tx_type in {TxType.BUY, TxType.SELL} and t.quantity > 0]
        statement_rows = [t for t in trade_rows if t.identity_quality == 'statement_row'
                          or (t.source or '').startswith('statement:')]
        order_rows = [t for t in trade_rows if t.identity_quality != 'statement_row'
                      and not (t.source or '').startswith('statement:')]
        # Use positive native execution evidence only; order aggregates never count as fills.
        fills_known = bool(trade_rows) and all(t.to_dict().get('record_kind') == 'execution' for t in trade_rows)
        order_ids = {(t.provider, t.account, t.parent_order_id or t.tx_id) for t in order_rows}
        orders_known = all(t.parent_order_id if t.to_dict().get('record_kind') == 'execution' else t.parent_order_id or t.tx_id for t in order_rows)
        open_rows = [{'currency': h.currency.upper(), 'pnl': h.unrealized_pnl} for h in selected_holdings]
        open_native = _by_currency(open_rows, 'pnl')
        open_krw = [fx.to_krw(h.unrealized_pnl, h.currency) for h in selected_holdings]
        if any(h.unrealized_pnl is None for h in selected_holdings):
            reasons.add('MISSING_POSITION_VALUATION')
        if any(fx.rate(h.currency) is None for h in selected_holdings):
            reasons.add('MISSING_FX_RATE')
        # Unknown purchase FX must not be hidden by the engine's spot fallback.
        if any(t.currency.upper() != 'KRW' and (t.fx_rate is None or t.fx_rate <= 0) and fx.rate(t.currency) is None for t in selected_rows):
            reasons.add('MISSING_FX_RATE')
        if any(t.tx_type is TxType.BUY and t.gross_amount is None and t.price is None for t in basis_rows):
            reasons.add('INCOMPLETE_COST_BASIS')
        if any(h.cost_basis is None for h in selected_holdings):
            reasons.add('INCOMPLETE_COST_BASIS')
        known_cycles = [c for c in selected_cycles if c['cost_basis_complete']
                        and not set(c['reason_codes']) - {'MISSING_FX_RATE'} and c['realized_pnl'] is not None]
        # Exclude whole broken books, including their pre-window basis, without
        # changing any trades, reconciliation or other accounting metrics.
        scope_keys = {(t.account, (t.symbol or '').upper(), t.currency.upper()) for t in basis_rows}
        scope_keys.update((h.account, h.symbol.upper(), h.currency.upper()) for h in selected_holdings)
        break_reasons = defaultdict(set)
        for book in reconciliation.get('reconciliation_breaks', []):
            key = (book['account'], book['symbol'].upper(), book['currency'].upper())
            if key in scope_keys:
                break_reasons[key].update(book['reason_codes'])
        excluded_counts = defaultdict(int)
        remaining_cycles = []
        known_ids = {id(cycle) for cycle in known_cycles}
        for cycle in selected_cycles:
            key = (cycle['account'], cycle['symbol'].upper(), cycle['currency'].upper())
            # A cycle whose only blocker is the engine's quantity drift is excluded by its book break.
            drift_only = set(cycle['reason_codes']) - {'MISSING_FX_RATE'} == {'ENGINE_QUANTITY_DRIFT'}
            if key in break_reasons and (id(cycle) in known_ids or drift_only):
                excluded_counts[key] += 1
            elif id(cycle) in known_ids:
                remaining_cycles.append(cycle)
        excluded_books = [{'account': key[0], 'symbol': key[1], 'currency': key[2],
            'reason_codes': sorted(break_reasons[key]), 'excluded_cycle_count': excluded_counts[key]}
            for key in sorted(break_reasons)]
        known_cycles = remaining_cycles
        wins = sum(c['realized_pnl'] > 0 for c in known_cycles)
        win_reasons = sorted(set(source_reasons) - {'RECONCILIATION_BREAK'})
        if excluded_books and not known_cycles:
            win_reasons.append('RECONCILIATION_BREAK')
        win_rate = wins / len(known_cycles) if known_cycles and not win_reasons else None
        # The same books and rule, plus round trips blocked only by an unreported tax.
        taxes_unknown = [c for c in selected_cycles if c['cost_basis_complete'] and c.get('_diagnostic_pnl') is not None
                         and set(c['reason_codes']) - {'MISSING_FX_RATE'}
                         and set(c['reason_codes']) - {'MISSING_FX_RATE'} <= _TAXES_UNKNOWN_CODES
                         and (c['account'], c['symbol'].upper(), c['currency'].upper()) not in break_reasons]
        all_eligible = len(known_cycles) + len(taxes_unknown)
        all_reasons = sorted(set(source_reasons) - {'RECONCILIATION_BREAK'})
        if excluded_books and not all_eligible:
            all_reasons.append('RECONCILIATION_BREAK')
        win_rate_all = ((wins + sum(c['_diagnostic_pnl'] > 0 for c in taxes_unknown)) / all_eligible
                        if all_eligible and not all_reasons else None)
        if excluded_books:
            win_reasons.append('EXCLUDED_BOOKS_WITH_QUANTITY_BREAKS')
            all_reasons.append('EXCLUDED_BOOKS_WITH_QUANTITY_BREAKS')
        cohorts = {}
        for name in ('new_in_window', 'pre_window_liquidation', 'unknown_origin'):
            attributed = []
            quantity = 0.0
            for sale in selected_sales:
                qty = sale['cohort_quantities'].get(name, 0)
                if qty <= 0:
                    continue
                quantity += qty
                fraction = qty / sale['quantity']
                attributed.append({**sale, **{field: None if sale[field] is None else sale[field] * fraction
                                             for field in ('realized_pnl', 'realized_pnl_krw')}})
            cohorts[name] = {**_review_pnl(attributed, source_reasons), 'quantity_sold': round(quantity, 8),
                             'attributed_sale_count': len(attributed),
                             'acquired_quantity': None if name == 'unknown_origin' else round(sum(
                                 t.quantity for t in basis_rows if t.tx_type is TxType.BUY
                                 and (t.trade_date >= start) == (name == 'new_in_window')), 8)}
        reasons = sorted(reasons)
        unavailable = quality['source_status'] == 'unavailable'
        krw_reasons = reasons
        return {
            'order_count': None if unavailable or not orders_known else len(order_ids),
            'order_count_reason_codes': ['LEDGER_UNAVAILABLE'] if unavailable else ['MISSING_ORDER_ID'] if not orders_known else [],
            'order_count_basis': 'Observed API order IDs only; statement trade rows excluded',
            'statement_trade_count': None if unavailable else len(statement_rows),
            'fill_count': len({(t.account, t.execution_id) for t in trade_rows}) if fills_known and all(t.execution_id for t in trade_rows) else None,
            'fill_count_reason_codes': [] if fills_known and all(t.execution_id for t in trade_rows) else ['ORDER_AGGREGATE_NOT_FILLS'],
            'round_trip_count': None if unavailable else len(selected_cycles),
            'thesis_count': None, 'thesis_count_reason_codes': ['NO_INVESTMENT_JOURNAL'],
            'realized': realized,
            'realized_pnl_krw': None if krw_reasons else realized['realized_pnl_krw'],
            'realized_pnl_krw_reason_codes': krw_reasons,
            'open_unrealized_pnl_by_currency': None if position_reasons else open_native,
            'open_unrealized_pnl_krw': None if reasons or any(v is None for v in open_krw) else round(sum(open_krw), 2),
            'open_unrealized_pnl_krw_reason_codes': reasons,
            **{field + '_status': money[field + '_status'] for field in ('fees', 'taxes')},
            **{field + '_reported_by_currency': money[field + '_by_currency'] for field in ('fees', 'taxes')},
            **{field + '_by_currency': {
                currency: None if set(source_reasons) & {'FAILED_SOURCES', 'TRUNCATED_SOURCES', 'LEDGER_UNAVAILABLE'} or any(
                    t.currency.upper() == currency and t.cost_status(singular) not in {'reported', 'verified_zero'}
                    for t in selected_rows) else amount
                for currency, amount in money[field + '_by_currency'].items()}
                for singular, field in (('fee', 'fees'), ('tax', 'taxes'))},
            **{field + '_krw': None if reasons else money[field + '_krw'] for field in ('fees', 'taxes')},
            **{field + '_krw_reason_codes': sorted(set(reasons + money[field + '_reason_codes'])) for field in ('fees', 'taxes')},
            'cost_fx': money['cost_fx'], 'cohorts': cohorts,
            'realized_win_rate': win_rate,
            'realized_win_rate_denominator': len(known_cycles),
            'realized_win_rate_reason_codes': win_reasons or ([] if known_cycles else ['NO_KNOWN_BASIS_CLOSED_ROUND_TRIPS']),
            'realized_win_rate_scope': 'excludes_books_with_quantity_breaks' if excluded_books else 'all_known_cycles',
            'realized_win_rate_excluded_books': excluded_books,
            'realized_win_rate_excluded_cycle_count': sum(excluded_counts.values()),
            'realized_win_rate_coverage': _review_win_rate_coverage(selected_cycles, known_cycles, break_reasons),
            'win_rate_strict': win_rate, 'win_rate_strict_eligible': len(known_cycles),
            'win_rate_all': win_rate_all, 'win_rate_all_eligible': all_eligible,
            'win_rate_all_taxes_unknown': bool(taxes_unknown), 'win_rate_all_taxes_unknown_cycles': len(taxes_unknown),
            'win_rate_all_reason_codes': all_reasons or ([] if all_eligible else ['NO_KNOWN_BASIS_CLOSED_ROUND_TRIPS']),
            'reason_codes': reasons,
            'result_status': 'unavailable' if unavailable else 'partial' if reasons else 'complete' if selected_rows or selected_holdings else 'empty',
        }

    totals = metrics(window_rows, all_sales, all_cycles, holdings, rows)
    symbols = []
    keys = sorted({((t.symbol or '').upper(), t.currency.upper()) for t in window_rows if t.symbol}
                  | {(h.symbol.upper(), h.currency.upper()) for h in holdings})
    for name, currency in keys:
        symbols.append({'symbol': name, 'currency': currency, **metrics(
            [t for t in window_rows if (t.symbol or '').upper() == name and t.currency.upper() == currency],
            [s for s in all_sales if s['symbol'] == name and s['currency'] == currency],
            [c for c in all_cycles if c['symbol'] == name and c['currency'] == currency],
            [h for h in holdings if h.symbol.upper() == name and h.currency.upper() == currency],
            [t for t in rows if (t.symbol or '').upper() == name and t.currency.upper() == currency],
        )})
    result_status = 'unavailable' if quality['source_status'] == 'unavailable' else 'partial' if totals['reason_codes'] else 'complete' if window_rows or holdings else 'empty'
    result = {
        'period': {'start': start, 'end': end}, 'detail_level': detail_level,
        'totals': totals, 'symbols': symbols, 'result_status': result_status,
        'reason_codes': totals['reason_codes'], 'reconciliation': reconciliation,
        'sources_failed': sorted(set(ledger.failed_sources + positions.failed_sources)),
        'coverage': {'requested_window': {'start': start, 'end': end},
                     'ledger_window': ledger.requested_window, 'observed_window': ledger.observed_window,
                     'fetch_complete': ledger.fetch_complete, 'historical_coverage': ledger.historical_coverage,
                     'source_metadata': ledger.source_metadata, 'truncated_sources': ledger.truncated_sources,
                     'failed_chunks': ledger.failed_chunks, 'positions_as_of': positions.fetched_at,
                     'reconciliation_status': reconciliation['status']},
        'account_basis_status': account_status,
        'methodology': {'cost_basis': 'existing_engine_moving_average_per_account',
                        'basis_scope': 'per_account',
                        'cohort_attribution': 'proportional_quantity; moving average cannot identify disposed lots',
                        'round_trips': 'observed zero to positive to zero within window, per account/symbol/currency',
                        'holding_period': 'quantity-weighted calendar days for observed acquisition legs',
                        'ordering': 'trade_date, then the broker fill time where recorded (Toss), then tx_id; '
                                    'KIS rows carry no fill time and keep order-id order within a day',
                        'open_pnl': 'current broker positions, not historical window-end valuation'},
        'not_computed': [{'metric': metric, 'reason_codes': codes} for metric, codes in (
            ('alpha', ['MISSING_ALIGNED_TOTAL_RETURN_BENCHMARK', 'MISSING_EXTERNAL_CASHFLOWS']),
            ('benchmark', ['MISSING_ALIGNED_TOTAL_RETURN_BENCHMARK']),
            ('turnover_effect', ['MISSING_COUNTERFACTUAL', 'MISSING_EXTERNAL_CASHFLOWS']),
            ('intent', ['NO_INVESTMENT_JOURNAL']), ('lesson', ['NO_INVESTMENT_JOURNAL']))],
    }
    if detail_level == 'full':
        result.update({'sales': all_sales,
                       'round_trips': [{k: v for k, v in c.items() if not k.startswith('_')} for c in all_cycles],
                       'open_positions': [h.to_dict() for h in holdings]})
    else:
        # Size: the per-symbol table (and cohorts) is the body; summary keeps the answer.
        result['totals'] = {k: v for k, v in totals.items() if k != 'cohorts'}
        result.update(_review_summary_symbols(symbols, fx))
    return result
