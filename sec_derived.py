#!/usr/bin/env python3
from __future__ import annotations

from typing import Any


def safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def add_derived_metrics(row: dict[str, Any]) -> None:
    revenue = row.get("revenue")
    cost = row.get("cost_of_revenue")
    if "gross_profit" not in row and isinstance(revenue, dict) and isinstance(cost, dict):
        row["gross_profit"] = {
            "value": (safe_float(revenue.get("value")) or 0) - (safe_float(cost.get("value")) or 0),
            "unit": revenue.get("unit"),
            "period_type": "FY",
            "period_end": revenue.get("period_end"),
            "filed": revenue.get("filed"),
            "form": revenue.get("form"),
            "frame": revenue.get("frame"),
            "concept": "RevenueMinusCostOfRevenue",
            "derived": True,
        }
    ocf = row.get("operating_cash_flow")
    capex = row.get("capex")
    if isinstance(ocf, dict) and isinstance(capex, dict):
        ocf_value = safe_float(ocf.get("value"))
        capex_value = safe_float(capex.get("value"))
        if ocf_value is not None and capex_value is not None:
            row["free_cash_flow"] = {
                "value": ocf_value - abs(capex_value),
                "unit": ocf.get("unit"),
                "period_type": "FY",
                "period_end": ocf.get("period_end"),
                "derived": True,
                "formula": "operating_cash_flow - abs(capex)",
            }
