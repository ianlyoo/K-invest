#!/usr/bin/env python3
from __future__ import annotations

from typing import Any


def analyst_consensus(ticker: Any, info: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {
        "source": "yfinance",
        "forward_eps": _safe_float(info.get("forwardEps") or info.get("epsForward")),
        "forward_pe": _safe_float(info.get("forwardPE")),
        "target_price": {
            "mean": _safe_float(info.get("targetMeanPrice")),
            "median": _safe_float(info.get("targetMedianPrice")),
            "high": _safe_float(info.get("targetHighPrice")),
            "low": _safe_float(info.get("targetLowPrice")),
        },
        "recommendation": {
            "key": info.get("recommendationKey"),
            "mean": _safe_float(info.get("recommendationMean")),
        },
    }
    data["earnings_estimate"] = _estimate_rows(getattr(ticker, "earnings_estimate", None))
    data["revenue_estimate"] = _estimate_rows(getattr(ticker, "revenue_estimate", None))
    data["recommendations_summary"] = _records(getattr(ticker, "recommendations_summary", None))
    return data


def risk_free_rate(ticker: Any) -> dict[str, Any]:
    hist = ticker.history(period="5d")
    if hist is None or hist.empty:
        return {"symbol": "^TNX", "error": "No Treasury yield data available"}
    row = hist.iloc[-1]
    raw = _safe_float(row.get("Close"))
    return {
        "symbol": "^TNX",
        "ten_year_treasury_yield_pct": raw,
        "ten_year_treasury_yield_decimal": round(raw / 100, 6) if raw is not None else None,
        "date": str(getattr(row, "name", ""))[:10],
        "source": "yfinance ^TNX",
        "note": "^TNX is quoted as 10Y yield percent, e.g. 4.2 means 4.2%.",
    }


def _estimate_rows(frame: Any) -> dict[str, Any] | None:
    if frame is None or getattr(frame, "empty", True):
        return None
    result: dict[str, Any] = {}
    for period in ("0q", "+1q", "0y", "+1y"):
        if period not in frame.index:
            continue
        row = frame.loc[period]
        result[period] = {
            "avg": _safe_float(row.get("avg")),
            "low": _safe_float(row.get("low")),
            "high": _safe_float(row.get("high")),
            "growth_pct": _pct(row.get("growth")),
            "number_of_analysts": _safe_float(row.get("numberOfAnalysts")),
        }
    return result or None


def _records(frame: Any) -> list[dict[str, Any]]:
    if frame is None or getattr(frame, "empty", True):
        return []
    rows = []
    for item in frame.head(4).to_dict("records"):
        rows.append({str(key): value for key, value in item.items()})
    return rows


def _pct(value: Any) -> float | None:
    number = _safe_float(value)
    return round(number * 100, 4) if number is not None else None


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        number = float(value)
        if number != number:
            return None
        return number
    except (TypeError, ValueError):
        return None
