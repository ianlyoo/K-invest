#!/usr/bin/env python3
from __future__ import annotations

from typing import Any

from sec_derived import safe_float

PRETAX_TAGS = [
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxes",
]
TAX_TAGS = ["IncomeTaxExpenseBenefit"]


def add_earnings_quality(sec_data: dict[str, Any], facts: dict[str, Any]) -> None:
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    if not isinstance(us_gaap, dict):
        return
    pretax = _annual_metric(us_gaap, PRETAX_TAGS)
    taxes = _annual_metric(us_gaap, TAX_TAGS)
    for row in sec_data.get("annuals", []):
        if not isinstance(row, dict):
            continue
        fiscal_year = _int_or_none(row.get("fiscal_year"))
        if fiscal_year is None:
            continue
        pretax_value = safe_float((pretax.get(fiscal_year) or {}).get("val"))
        tax_value = safe_float((taxes.get(fiscal_year) or {}).get("val"))
        if pretax_value is None or tax_value is None or pretax_value == 0:
            continue
        rate = tax_value / pretax_value * 100
        flags = []
        if rate > 35:
            flags.append("high_effective_tax_rate")
        if rate < 0:
            flags.append("tax_benefit_or_negative_effective_tax_rate")
        row["earnings_quality"] = {
            "basis": "GAAP net income; SEC tax/pre-tax screen",
            "pretax_income": pretax_value,
            "income_tax_expense": tax_value,
            "effective_tax_rate_pct": round(rate, 2),
            "normalization_flags": flags,
            "normalized_net_income": None,
            "note": "Flags identify possible one-time distortion; no adjusted net income is estimated without explicit non-GAAP reconciliation.",
        }


def _annual_metric(us_gaap: dict[str, Any], tags: list[str]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for tag in tags:
        values = us_gaap.get(tag, {}).get("units", {}).get("USD", [])
        if not isinstance(values, list):
            continue
        for raw in values:
            if not isinstance(raw, dict):
                continue
            if str(raw.get("form", "")).upper() not in {"10-K", "10-K/A"}:
                continue
            if str(raw.get("fp", "")).upper() != "FY":
                continue
            fy = _year_from_end(raw.get("end"))
            if fy is None:
                continue
            previous = result.get(fy)
            if previous is None or str(raw.get("filed") or "") > str(previous.get("filed") or ""):
                result[fy] = raw
        if result:
            return result
    return result


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _year_from_end(value: Any) -> int | None:
    text = str(value or "")
    if len(text) < 4:
        return None
    return _int_or_none(text[:4])
