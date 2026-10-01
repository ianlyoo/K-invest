#!/usr/bin/env python3
"""Deterministic portfolio accounting and performance math.

Every number the MCP tools report is computed here, not by the caller. Pure
functions over the normalized model in portfolio_model — no network, no clients.

Accounting basis:
  - **Cost basis: moving average** (이동평균법), matching Korean broker convention
    so figures reconcile with KIS `pchs_avg_pric` and Toss `averagePurchasePrice`.
  - Buy fees and taxes are capitalized into cost; sell fees and taxes reduce
    proceeds. Realized P&L on a sale = proceeds - (avg cost x quantity sold).
  - Positions are tracked per (symbol, currency): the same ticker held in two
    accounts is one position, but the same symbol traded in two currencies is not.
  - FX: valuation uses current spot (FxRates); a transaction's own `fx_rate` is
    used for KRW-denominated realized P&L when the broker reported one.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Iterable, Sequence

from portfolio_model import (
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


def consolidate_holdings(holdings: Sequence[Holding], fx: FxRates) -> dict[str, Any]:
    """Aggregate per-account holdings into one row per (symbol, currency).

    The average price is quantity-weighted through summed cost basis, which is
    exact rather than an average of averages.
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
                "_cost_known": True,
                "_value_known": True,
            },
        )
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
        entry["accounts"].append(
            {
                "account": holding.account,
                "provider": holding.provider,
                "quantity": holding.quantity,
                "avg_price": holding.avg_price,
                "cost_basis": holding.cost_basis,
                "market_value": holding.market_value,
            }
        )

    positions: list[dict[str, Any]] = []
    for entry in grouped.values():
        quantity = entry["quantity"]
        # A partially-unknown group must report None rather than a short total.
        cost_basis = entry["cost_basis"] if entry.pop("_cost_known") else None
        market_value = entry["market_value"] if entry.pop("_value_known") else None
        if market_value is None and entry["last_price"] is not None:
            market_value = entry["last_price"] * quantity
        unrealized = None if (market_value is None or cost_basis is None) else market_value - cost_basis
        entry.update(
            {
                "quantity": round(quantity, 8),
                "avg_price": (cost_basis / quantity) if (cost_basis and quantity) else None,
                "cost_basis": cost_basis,
                "market_value": market_value,
                "unrealized_pnl": unrealized,
                "unrealized_pnl_pct": (
                    round(unrealized / cost_basis * 100, 4) if (unrealized is not None and cost_basis) else None
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
            round(value / total_value_krw * 100, 4) if (value is not None and total_value_krw) else None
        )

    return {
        "positions": positions,
        "position_count": len(positions),
        "totals": {
            "cost_basis_krw": _sum(p["cost_basis_krw"] for p in positions),
            "market_value_krw": total_value_krw,
            "unrealized_pnl_krw": _sum(p["unrealized_pnl_krw"] for p in positions),
        },
        "by_currency": _group_totals(positions, "currency"),
        "by_market": _group_totals(positions, "market"),
        "fx": fx.to_dict(),
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


def _sum(values: Iterable[float | None]) -> float | None:
    total = 0.0
    seen = False
    for value in values:
        if value is not None:
            total += value
            seen = True
    return round(total, 2) if seen else None


# ── Realized P&L (moving-average cost basis) ─────────────


def compute_realized_pnl(
    transactions: Sequence[Transaction],
    fx: FxRates,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Replay the ledger under moving-average cost basis.

    The full ledger is replayed so that cost basis is correct, and only sales
    inside [start, end] are reported. When a sale has no matching prior buy in the
    ledger (the window predates the purchase), the position is flagged in
    `incomplete_cost_basis` and its realized P&L is reported as proceeds only —
    understating cost, so it must not be read as a true gain.

    Foreign positions carry cost in both the trade currency and KRW, so KRW P&L
    decomposes exactly:

        realized_pnl_krw = realized_pnl x sell_rate + realized_fx_krw
        realized_fx_krw  = cost_in_currency x sell_rate - cost_in_krw

    i.e. the local price move converted at the exit rate, plus what the currency
    itself did between entry and exit. The split needs a rate on the buys as well
    as the sale (KIS reports one; Toss does not) — when a buy rate is missing the
    position falls back to converting local P&L at the sale rate and
    `realized_fx_krw` is None rather than a fabricated number.
    """
    ordered = sorted(transactions, key=lambda t: (t.trade_date, t.tx_id))
    books: dict[tuple[str, str], dict[str, Any]] = {}
    incomplete: set[str] = set()
    per_symbol: dict[tuple[str, str], dict[str, Any]] = {}
    dividends = 0.0
    dividends_krw = 0.0
    interest = 0.0
    fees_total = 0.0
    taxes_total = 0.0
    fx_total_krw = 0.0
    fx_known = False

    for tx in ordered:
        in_window = _in_range(tx.trade_date, start or _EPOCH, end or "9999-12-31")
        currency = tx.currency.upper()
        symbol = (tx.symbol or "").upper()
        key = (symbol, currency)

        if tx.tx_type in (TxType.DIVIDEND, TxType.INTEREST):
            if in_window:
                amount = tx.net_amount if tx.net_amount is not None else (tx.gross_amount or 0.0)
                if tx.tx_type is TxType.DIVIDEND:
                    dividends += amount
                    dividends_krw += _to_krw(amount, tx, fx) or 0.0
                else:
                    interest += amount
                fees_total += tx.fee
                taxes_total += tx.tax
            continue

        if tx.tx_type is TxType.SPLIT and symbol:
            # Share count changes, cost basis does not. `quantity` carries the
            # post-split share count for the position.
            book = books.get(key)
            if book and tx.quantity > 0:
                book["quantity"] = tx.quantity
            continue

        if not symbol or tx.tx_type not in (TxType.BUY, TxType.SELL):
            continue

        book = books.setdefault(key, {"quantity": 0.0, "cost": 0.0, "cost_krw": 0.0, "krw_known": True})
        gross = tx.gross_amount or ((tx.price or 0.0) * tx.quantity)
        rate = _recorded_rate(tx)

        if tx.tx_type is TxType.BUY:
            book["quantity"] += tx.quantity
            book["cost"] += gross + tx.fee + tx.tax
            if rate is None:
                book["krw_known"] = False
            else:
                book["cost_krw"] += (gross + tx.fee + tx.tax) * rate
            if in_window:
                fees_total += tx.fee
                taxes_total += tx.tax
            continue

        # SELL
        proceeds = gross - tx.fee - tx.tax
        available = book["quantity"]
        avg_cost = (book["cost"] / available) if available > 0 else None
        matched = min(tx.quantity, available) if available > 0 else 0.0
        cost_removed = (avg_cost or 0.0) * matched
        krw_known = bool(book["krw_known"]) and available > 0 and rate is not None
        cost_krw_removed = (book["cost_krw"] / available) * matched if krw_known else None
        if matched < tx.quantity - 1e-9:
            incomplete.add(symbol)
        book["quantity"] = max(available - tx.quantity, 0.0)
        remaining = book["quantity"] > 0
        book["cost"] = max(book["cost"] - cost_removed, 0.0) if remaining else 0.0
        book["cost_krw"] = (
            max(book["cost_krw"] - (cost_krw_removed or 0.0), 0.0) if remaining and krw_known else 0.0
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
        fees_total += tx.fee
        taxes_total += tx.tax
        if fx_component is not None:
            fx_total_krw += fx_component
            fx_known = True
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
        entry["sell_count"] += 1
        if symbol in incomplete:
            entry["cost_basis_complete"] = False

    symbols: list[dict[str, Any]] = []
    for entry in per_symbol.values():
        if entry["symbol"] in incomplete:
            entry["cost_basis_complete"] = False
        entry["realized_pnl_pct"] = (
            round(entry["realized_pnl"] / entry["cost_basis"] * 100, 4) if entry["cost_basis"] else None
        )
        if not entry.pop("_fx_known"):
            entry["realized_fx_krw"] = None
        for field in ("proceeds", "cost_basis", "realized_pnl", "realized_pnl_krw", "fees", "taxes"):
            entry[field] = round(entry[field], 4)
        if entry["realized_fx_krw"] is not None:
            entry["realized_fx_krw"] = round(entry["realized_fx_krw"], 2)
        symbols.append(entry)
    symbols.sort(key=lambda s: s["realized_pnl_krw"], reverse=True)

    return {
        "period": {"start": start, "end": end},
        "symbols": symbols,
        "totals": {
            "realized_pnl_krw": round(sum(s["realized_pnl_krw"] for s in symbols), 2),
            "proceeds_by_currency": _by_currency(symbols, "proceeds"),
            "realized_pnl_by_currency": _by_currency(symbols, "realized_pnl"),
            "realized_fx_krw": round(fx_total_krw, 2) if fx_known else None,
            "dividends": round(dividends, 4),
            "dividends_krw": round(dividends_krw, 2),
            "interest": round(interest, 4),
            "fees": round(fees_total, 4),
            "taxes": round(taxes_total, 4),
            "sell_count": sum(s["sell_count"] for s in symbols),
        },
        "cost_basis_method": "moving_average",
        "incomplete_cost_basis": sorted(incomplete),
        "open_positions_from_ledger": {
            f"{symbol}:{currency}": {
                "quantity": round(book["quantity"], 8),
                "cost_basis": round(book["cost"], 4),
                "avg_price": round(book["cost"] / book["quantity"], 6) if book["quantity"] else None,
            }
            for (symbol, currency), book in sorted(books.items())
            if book["quantity"] > 0
        },
    }


def _by_currency(symbols: Sequence[dict[str, Any]], field: str) -> dict[str, float]:
    totals: dict[str, float] = {}
    for entry in symbols:
        currency = entry["currency"]
        totals[currency] = round(totals.get(currency, 0.0) + entry[field], 4)
    return dict(sorted(totals.items()))


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


def summarize_cashflows(transactions: Sequence[Transaction], fx: FxRates, start: str, end: str) -> dict[str, Any]:
    """Total external flows and income inside a window, in KRW."""
    deposits = withdrawals = dividends = interest = fees = taxes = 0.0
    buys = sells = 0.0
    for tx in transactions:
        if not _in_range(tx.trade_date, start, end):
            continue
        amount = _to_krw(tx.net_amount, tx, fx)
        if amount is None:
            continue
        if tx.tx_type is TxType.DEPOSIT:
            deposits += abs(amount)
        elif tx.tx_type is TxType.WITHDRAWAL:
            withdrawals += abs(amount)
        elif tx.tx_type is TxType.DIVIDEND:
            dividends += amount
        elif tx.tx_type is TxType.INTEREST:
            interest += amount
        elif tx.tx_type is TxType.BUY:
            buys += abs(amount)
        elif tx.tx_type is TxType.SELL:
            sells += amount
        fees += _to_krw(tx.fee, tx, fx) or 0.0
        taxes += _to_krw(tx.tax, tx, fx) or 0.0
    return {
        "deposits_krw": round(deposits, 2),
        "withdrawals_krw": round(withdrawals, 2),
        "net_deposits_krw": round(deposits - withdrawals, 2),
        "dividends_krw": round(dividends, 2),
        "interest_krw": round(interest, 2),
        "fees_krw": round(fees, 2),
        "taxes_krw": round(taxes, 2),
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


def time_weighted_return(navs: Sequence[dict[str, Any]], flows_by_date: dict[str, float]) -> float | None:
    """Chain sub-period returns so external flows don't distort the result.

    Each interval's return is (NAV_end - flow) / NAV_start - 1, where `flow` is the
    net external contribution recorded on the interval's end date.
    """
    series = [(row["snapshot_date"], float(row["total_value_krw"])) for row in navs if row.get("total_value_krw")]
    if len(series) < 2:
        return None
    factor = 1.0
    for (_, start_value), (end_date, end_value) in zip(series, series[1:]):
        if start_value <= 0:
            continue
        flow = flows_by_date.get(end_date, 0.0)
        factor *= (end_value - flow) / start_value
    return round(factor - 1.0, 6)


def nav_statistics(navs: Sequence[dict[str, Any]], risk_free_rate: float = 0.0) -> dict[str, Any]:
    """Drawdown and risk statistics from a daily NAV series.

    Volatility, Sharpe, and Sortino are annualized from daily NAV changes with
    252 trading days. Returns are simple NAV-to-NAV changes, so external deposits
    inside the window inflate them — read alongside TWR.
    """
    series = [
        (row["snapshot_date"], float(row["total_value_krw"]))
        for row in navs
        if row.get("total_value_krw") is not None
    ]
    if len(series) < 2:
        return {
            "observations": len(series),
            "max_drawdown_pct": None,
            "volatility_annual_pct": None,
            "sharpe": None,
            "sortino": None,
            "best_day_pct": None,
            "worst_day_pct": None,
            "note": "at least 2 NAV snapshots are required",
        }

    returns: list[float] = []
    for (_, previous), (_, current) in zip(series, series[1:]):
        if previous > 0:
            returns.append(current / previous - 1.0)

    peak = series[0][1]
    max_drawdown = 0.0
    trough_date = peak_date = series[0][0]
    worst_peak_date = worst_trough_date = series[0][0]
    for snapshot_date, value in series:
        if value > peak:
            peak, peak_date = value, snapshot_date
        elif peak > 0:
            drawdown = value / peak - 1.0
            if drawdown < max_drawdown:
                max_drawdown, trough_date = drawdown, snapshot_date
                worst_peak_date, worst_trough_date = peak_date, trough_date

    count = len(returns)
    mean = sum(returns) / count if count else 0.0
    variance = sum((r - mean) ** 2 for r in returns) / (count - 1) if count > 1 else 0.0
    daily_vol = variance**0.5
    annual_vol = daily_vol * (252**0.5)
    annual_return = mean * 252
    excess = annual_return - risk_free_rate
    downside = [r for r in returns if r < 0]
    downside_dev = (
        (sum(r**2 for r in downside) / len(downside)) ** 0.5 * (252**0.5) if downside else 0.0
    )

    return {
        "observations": len(series),
        "first_date": series[0][0],
        "last_date": series[-1][0],
        "max_drawdown_pct": round(max_drawdown * 100, 4),
        "max_drawdown_peak_date": worst_peak_date if max_drawdown < 0 else None,
        "max_drawdown_trough_date": worst_trough_date if max_drawdown < 0 else None,
        "volatility_annual_pct": round(annual_vol * 100, 4) if count > 1 else None,
        "return_annual_pct": round(annual_return * 100, 4) if count else None,
        "sharpe": round(excess / annual_vol, 4) if annual_vol else None,
        "sortino": round(excess / downside_dev, 4) if downside_dev else None,
        "best_day_pct": round(max(returns) * 100, 4) if returns else None,
        "worst_day_pct": round(min(returns) * 100, 4) if returns else None,
        "risk_free_rate_used": risk_free_rate,
    }


def daily_and_monthly_pnl(navs: Sequence[dict[str, Any]], flows_by_date: dict[str, float]) -> dict[str, Any]:
    """Per-day and per-month NAV change, net of recorded external flows."""
    series = [
        (row["snapshot_date"], float(row["total_value_krw"]))
        for row in navs
        if row.get("total_value_krw") is not None
    ]
    daily: list[dict[str, Any]] = []
    monthly: dict[str, float] = {}
    for (_, previous), (end_date, current) in zip(series, series[1:]):
        flow = flows_by_date.get(end_date, 0.0)
        pnl = current - previous - flow
        daily.append(
            {
                "date": end_date,
                "pnl_krw": round(pnl, 2),
                "return_pct": round(pnl / previous * 100, 4) if previous else None,
                "net_flow_krw": round(flow, 2),
            }
        )
        monthly[end_date[:7]] = round(monthly.get(end_date[:7], 0.0) + pnl, 2)
    return {"daily": daily, "monthly": dict(sorted(monthly.items()))}


def compute_performance(
    transactions: Sequence[Transaction],
    navs: Sequence[dict[str, Any]],
    current_nav_krw: float | None,
    fx: FxRates,
    start: str,
    end: str,
    realized: dict[str, Any] | None = None,
    unrealized_krw: float | None = None,
    risk_free_rate: float = 0.0,
) -> dict[str, Any]:
    """Assemble the return picture for a window.

    Returns simple return, TWR, and XIRR side by side because each answers a
    different question and they disagree by construction when money moves:
      - simple: change in NAV less net external flows, over starting NAV.
      - TWR: flow-neutral, comparable to an index.
      - XIRR/MWR: flow-timing aware, the investor's actual annualized return.

    Without broker deposit/withdrawal data, net external flow is 0 and all three
    treat trades as internal reallocation — see `limitations` in the tool output.
    """
    flows = summarize_cashflows(transactions, fx, start, end)
    window_navs = [row for row in navs if _in_range(row.get("snapshot_date"), start, end)]
    start_nav = float(window_navs[0]["total_value_krw"]) if window_navs else None
    end_nav = (
        current_nav_krw
        if current_nav_krw is not None
        else (float(window_navs[-1]["total_value_krw"]) if window_navs else None)
    )
    net_flow = flows["net_deposits_krw"]

    profit = None
    simple_return = None
    if start_nav is not None and end_nav is not None:
        profit = end_nav - start_nav - net_flow
        denominator = start_nav + max(net_flow, 0.0)
        simple_return = round(profit / denominator * 100, 4) if denominator else None

    flows_by_date: dict[str, float] = {}
    for tx in transactions:
        if tx.tx_type not in (TxType.DEPOSIT, TxType.WITHDRAWAL) or not _in_range(tx.trade_date, start, end):
            continue
        amount = _to_krw(tx.net_amount, tx, fx) or 0.0
        flows_by_date[tx.trade_date] = flows_by_date.get(tx.trade_date, 0.0) + amount

    twr = time_weighted_return(window_navs, flows_by_date)

    cashflows: list[tuple[str, float]] = []
    if start_nav is not None and window_navs:
        cashflows.append((window_navs[0]["snapshot_date"], -start_nav))
    for tx in transactions:
        if tx.tx_type not in (TxType.DEPOSIT, TxType.WITHDRAWAL) or not _in_range(tx.trade_date, start, end):
            continue
        amount = _to_krw(tx.net_amount, tx, fx) or 0.0
        cashflows.append((tx.trade_date, -amount))
    if end_nav is not None:
        cashflows.append((end, end_nav))
    mwr = xirr(cashflows)

    realized_krw = (realized or {}).get("totals", {}).get("realized_pnl_krw")
    fx_pnl = (realized or {}).get("totals", {}).get("realized_fx_krw")

    return {
        "period": {"start": start, "end": end},
        "start_nav_krw": round(start_nav, 2) if start_nav is not None else None,
        "end_nav_krw": round(end_nav, 2) if end_nav is not None else None,
        "profit_krw": round(profit, 2) if profit is not None else None,
        "flows": flows,
        "pnl": {
            "realized_krw": realized_krw,
            "unrealized_krw": round(unrealized_krw, 2) if unrealized_krw is not None else None,
            "dividends_krw": flows["dividends_krw"] or (realized or {}).get("totals", {}).get("dividends_krw"),
            "fees_krw": flows["fees_krw"],
            "taxes_krw": flows["taxes_krw"],
            "fx_krw": fx_pnl,
        },
        "returns": {
            "simple_return_pct": simple_return,
            "twr_pct": round(twr * 100, 4) if twr is not None else None,
            "xirr_pct": round(mwr * 100, 4) if mwr is not None else None,
            "mwr_equals_xirr": True,
        },
        "risk": nav_statistics(window_navs, risk_free_rate=risk_free_rate),
        "nav_observations": len(window_navs),
    }


# ── Portfolio summary ────────────────────────────────────


def summarize_portfolio(
    holdings: Sequence[Holding],
    cash: Sequence[CashBalance],
    fx: FxRates,
    realized: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Total assets, cost, P&L, and per-currency/account breakdowns in KRW."""
    consolidated = consolidate_holdings(holdings, fx)
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
            {"provider": holding.provider, "position_count": 0, "market_value_krw": 0.0, "cost_basis_krw": 0.0},
        )
        entry["position_count"] += 1
        entry["market_value_krw"] += fx.to_krw(holding.market_value, holding.currency) or 0.0
        entry["cost_basis_krw"] += fx.to_krw(holding.cost_basis, holding.currency) or 0.0
    for balance in cash:
        entry = by_account.setdefault(
            balance.account,
            {"provider": balance.provider, "position_count": 0, "market_value_krw": 0.0, "cost_basis_krw": 0.0},
        )
        entry["cash_krw"] = round(entry.get("cash_krw", 0.0) + (fx.to_krw(balance.amount, balance.currency) or 0.0), 2)
    for entry in by_account.values():
        entry["market_value_krw"] = round(entry["market_value_krw"], 2)
        entry["cost_basis_krw"] = round(entry["cost_basis_krw"], 2)
        entry["total_krw"] = round(entry["market_value_krw"] + entry.get("cash_krw", 0.0), 2)

    realized_totals = (realized or {}).get("totals", {})
    return {
        "total_assets_krw": round(stock_value + cash_krw, 2),
        "stock_value_krw": round(stock_value, 2),
        "cash_krw": round(cash_krw, 2),
        "cost_basis_krw": round(cost_basis, 2),
        "unrealized_pnl_krw": unrealized,
        "unrealized_pnl_pct": round(unrealized / cost_basis * 100, 4) if (unrealized is not None and cost_basis) else None,
        "realized_pnl_krw": realized_totals.get("realized_pnl_krw"),
        "dividends_krw": realized_totals.get("dividends_krw"),
        "fees_krw": realized_totals.get("fees"),
        "taxes_krw": realized_totals.get("taxes"),
        "position_count": consolidated["position_count"],
        "by_currency": consolidated["by_currency"],
        "by_market": consolidated["by_market"],
        "by_account": dict(sorted(by_account.items(), key=lambda kv: kv[1]["total_krw"], reverse=True)),
        "cash_by_currency": dict(sorted(cash_by_currency.items())),
        "fx": fx.to_dict(),
    }
