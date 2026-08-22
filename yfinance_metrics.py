#!/usr/bin/env python3
from __future__ import annotations

from typing import Any


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if isinstance(value, str):
            value = value.replace(",", "")
        number = float(value)
        if number != number:
            return None
        return number
    except (ValueError, TypeError):
        return None


def pct(value: Any) -> float | None:
    number = _safe_float(value)
    if number is None:
        return None
    if abs(number) <= 1:
        return round(number * 100, 4)
    return round(number, 4)


def growth_pct(value: Any) -> float | None:
    number = _safe_float(value)
    if number is None:
        return None
    return round(number * 100, 4)


def _price_from_info(info: dict[str, Any]) -> float | None:
    for key in ("currentPrice", "regularMarketPrice", "previousClose"):
        price = _safe_float(info.get(key))
        if price and price > 0:
            return price
    return None


def dividend_yield_pct(info: dict[str, Any]) -> tuple[float | None, dict[str, Any] | None]:
    dividend_rate = _safe_float(info.get("dividendRate"))
    price = _price_from_info(info)
    raw_yield = _safe_float(info.get("dividendYield"))
    trailing_raw_yield = _safe_float(info.get("trailingAnnualDividendYield"))
    trailing_rate = _safe_float(info.get("trailingAnnualDividendRate"))

    source = None
    value = None
    if dividend_rate is not None and dividend_rate > 0 and price is not None:
        value = dividend_rate / price * 100
        source = "dividendRate/currentPrice"
    elif trailing_rate is not None and trailing_rate > 0 and price is not None:
        value = trailing_rate / price * 100
        source = "trailingAnnualDividendRate/currentPrice"
    elif raw_yield is not None:
        value = raw_yield * 100 if abs(raw_yield) <= 0.05 else raw_yield
        source = "dividendYield"
    elif trailing_raw_yield is not None:
        value = trailing_raw_yield * 100 if abs(trailing_raw_yield) <= 1 else trailing_raw_yield
        source = "trailingAnnualDividendYield"

    if value is None:
        return None, {
            "source": None,
            "raw_dividend_yield": raw_yield,
            "dividend_rate": dividend_rate,
            "price": price,
            "note": "No usable dividend yield or dividend rate in yfinance info.",
        }

    rounded = round(value, 4)
    meta: dict[str, Any] = {
        "source": source,
        "raw_dividend_yield": raw_yield,
        "raw_trailing_annual_dividend_yield": trailing_raw_yield,
        "dividend_rate": dividend_rate,
        "trailing_annual_dividend_rate": trailing_rate,
        "price": price,
    }
    if rounded > 30:
        meta["warning"] = "suspicious_dividend_yield_over_30pct"
    return rounded, meta


def null_reason(info: dict[str, Any], field: str) -> str | None:
    if info.get(field) is None:
        return f"yfinance info.{field} is null or unavailable"
    return None


def growth_fields(symbol: str, info: dict[str, Any]) -> dict[str, Any]:
    revenue_growth = growth_pct(info.get("revenueGrowth"))
    earnings_growth = growth_pct(info.get("earningsGrowth"))
    result: dict[str, Any] = {
        "revenue_growth_pct": revenue_growth,
        "earnings_growth_pct": earnings_growth,
        "revenue_growth_pct_yoy_quarterly": revenue_growth,
        "earnings_growth_pct_yoy_quarterly": earnings_growth,
        "growth_meta": {
            "source": "yfinance.info",
            "revenue_basis": "quarterly_yoy",
            "earnings_basis": "quarterly_yoy",
            "raw_revenue_growth": _safe_float(info.get("revenueGrowth")),
            "raw_earnings_growth": _safe_float(info.get("earningsGrowth")),
        },
    }
    annual = _annual_sec_growth(symbol)
    if annual is not None:
        result.update(annual)
    return result


def _annual_sec_growth(symbol: str) -> dict[str, Any] | None:
    if "." in symbol or symbol.isdigit():
        return None
    try:
        from sec_api import SECClient

        annuals = SECClient().get_financials(symbol).get("annuals", [])
    except Exception:
        return None
    if len(annuals) < 2:
        return None
    latest, previous = annuals[0], annuals[1]
    latest_revenue = _metric_value(latest, "revenue")
    previous_revenue = _metric_value(previous, "revenue")
    latest_earnings = _metric_value(latest, "net_income")
    previous_earnings = _metric_value(previous, "net_income")
    if latest_revenue is None or previous_revenue in (None, 0):
        return None
    result: dict[str, Any] = {
        "revenue_growth_pct_annual_sec": round((latest_revenue / previous_revenue - 1) * 100, 4),
        "growth_meta_sec_annual": {
            "source": "SEC CompanyFacts",
            "basis": "annual_fy_yoy",
            "latest_fiscal_year": latest.get("fiscal_year"),
            "previous_fiscal_year": previous.get("fiscal_year"),
            "latest_revenue": latest_revenue,
            "previous_revenue": previous_revenue,
            "latest_net_income": latest_earnings,
            "previous_net_income": previous_earnings,
        },
    }
    if latest_earnings is not None and previous_earnings not in (None, 0):
        result["earnings_growth_pct_annual_sec"] = round((latest_earnings / previous_earnings - 1) * 100, 4)
    return result


def _metric_value(row: dict[str, Any], metric: str) -> float | None:
    value = row.get(metric)
    if not isinstance(value, dict):
        return None
    return _safe_float(value.get("value"))
