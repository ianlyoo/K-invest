#!/usr/bin/env python3
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from sec_derived import safe_float

FORMS_Q = {"10-Q", "10-Q/A"}
FORMS_K = {"10-K", "10-K/A"}
QUARTER_FPS = ("Q1", "Q2", "Q3")
FLOW_CONCEPTS = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "operating_income": ["OperatingIncomeLoss"],
    "gross_profit": ["GrossProfit"],
    "cost_of_revenue": ["CostOfRevenue", "CostOfGoodsAndServicesSold"],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
}


def add_quarterly_ttm(sec_data: dict[str, Any], facts: dict[str, Any]) -> None:
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    if not isinstance(us_gaap, dict):
        sec_data["quarters"] = []
        sec_data["ttm"] = None
        return
    metric_quarters = {
        metric: _metric_quarters(us_gaap, metric, tags)
        for metric, tags in FLOW_CONCEPTS.items()
    }
    rows = _merge_quarter_rows(metric_quarters)
    for row in rows:
        _add_quarter_derived(row)
    sec_data["quarters"] = rows[:8]
    sec_data["ttm"] = _build_ttm(rows)
    sec_data.setdefault("methodology", {})["quarterly_ttm"] = (
        "SEC CompanyFacts 10-Q Q1-Q3 plus derived Q4 from FY minus Q1-Q3. "
        "TTM sums latest four fiscal quarters by period_end."
    )


def _metric_quarters(
    us_gaap: dict[str, Any],
    metric: str,
    tags: list[str],
) -> dict[tuple[int, str], dict[str, Any]]:
    facts = _facts_for_tags(us_gaap, metric, tags)
    if not facts:
        return {}
    by_fy: dict[int, list[dict[str, Any]]] = defaultdict(list)
    annuals: dict[int, dict[str, Any]] = {}
    for fact in _dedupe_original_periods(facts):
        fp = str(fact.get("fp") or "").upper()
        fy = _int_or_none(fact.get("fy"))
        if fy is None:
            continue
        if fp == "FY" and str(fact.get("form", "")).upper() in FORMS_K:
            annuals[fy] = _better_annual(annuals.get(fy), fact)
        elif fp in QUARTER_FPS and str(fact.get("form", "")).upper() in FORMS_Q:
            by_fy[fy].append(fact)
    result: dict[tuple[int, str], dict[str, Any]] = {}
    for fy, items in by_fy.items():
        cumulative: dict[str, dict[str, Any]] = {}
        direct: dict[str, dict[str, Any]] = {}
        for item in items:
            fp = str(item.get("fp") or "").upper()
            days = _duration_days(item)
            if days is None:
                continue
            if 60 <= days <= 120:
                direct[fp] = _better_quarter(direct.get(fp), item)
            elif 120 < days <= 310:
                cumulative[fp] = _better_quarter(cumulative.get(fp), item)
        for fp in QUARTER_FPS:
            fact = direct.get(fp) or _quarter_from_cumulative(fp, cumulative, direct)
            if fact is not None:
                result[(fy, fp)] = _metric_payload(metric, fact)
        q4 = _q4_from_annual(fy, metric, annuals.get(fy), direct, cumulative)
        if q4 is not None:
            result[(fy, "Q4")] = q4
    return result


def _facts_for_tags(us_gaap: dict[str, Any], metric: str, tags: list[str]) -> list[dict[str, Any]]:
    for tag in tags:
        tag_data = us_gaap.get(tag)
        if not isinstance(tag_data, dict):
            continue
        units = tag_data.get("units", {})
        values = units.get("USD") or []
        if not isinstance(values, list):
            continue
        rows = []
        for raw in values:
            if not isinstance(raw, dict):
                continue
            row = dict(raw)
            row["concept"] = tag
            row["unit"] = "USD"
            if safe_float(row.get("val")) is not None:
                rows.append(row)
        if rows:
            return rows
    return []


def _dedupe_original_periods(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, int | None], dict[str, Any]] = {}
    for fact in facts:
        key = (
            str(fact.get("fp") or "").upper(),
            str(fact.get("start") or ""),
            str(fact.get("end") or ""),
            _duration_days(fact),
        )
        prev = grouped.get(key)
        if prev is None or str(fact.get("filed") or "") < str(prev.get("filed") or ""):
            grouped[key] = fact
    return list(grouped.values())


def _quarter_from_cumulative(
    fp: str,
    cumulative: dict[str, dict[str, Any]],
    direct: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    fact = cumulative.get(fp)
    if fact is None:
        return None
    if fp == "Q1":
        return fact
    prev_fp = "Q1" if fp == "Q2" else "Q2"
    prev = cumulative.get(prev_fp) or direct.get(prev_fp)
    if prev is None:
        return None
    value = safe_float(fact.get("val"))
    prev_value = safe_float(prev.get("val"))
    if value is None or prev_value is None:
        return None
    item = dict(fact)
    item["val"] = value - prev_value
    item["derived"] = True
    item["formula"] = f"{fp}_ytd - prior_ytd"
    return item


def _q4_from_annual(
    fy: int,
    metric: str,
    annual: dict[str, Any] | None,
    direct: dict[str, dict[str, Any]],
    cumulative: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    if annual is None:
        return None
    base = cumulative.get("Q3")
    q1_q3 = safe_float(base.get("val")) if base is not None else None
    if q1_q3 is None:
        values = [safe_float((direct.get(fp) or {}).get("val")) for fp in QUARTER_FPS]
        if any(v is None for v in values):
            return None
        q1_q3 = sum(v or 0 for v in values)
    annual_value = safe_float(annual.get("val"))
    if annual_value is None:
        return None
    item = dict(annual)
    item["val"] = annual_value - q1_q3
    item["fp"] = "Q4"
    item["fy"] = fy
    item["derived"] = True
    item["formula"] = "FY - Q1_Q3_ytd"
    return _metric_payload(metric, item)


def _metric_payload(metric: str, fact: dict[str, Any]) -> dict[str, Any]:
    return {
        "value": safe_float(fact.get("val")),
        "unit": fact.get("unit") or "USD",
        "period_type": "quarter",
        "period_end": fact.get("end"),
        "filed": fact.get("filed"),
        "form": fact.get("form"),
        "frame": fact.get("frame"),
        "concept": fact.get("concept"),
        "derived": fact.get("derived", False),
        "formula": fact.get("formula"),
    }


def _merge_quarter_rows(metric_quarters: dict[str, dict[tuple[int, str], dict[str, Any]]]) -> list[dict[str, Any]]:
    keys = set()
    for rows in metric_quarters.values():
        keys.update(rows)
    merged: list[dict[str, Any]] = []
    for fy, fp in sorted(keys, key=lambda k: _sort_end(metric_quarters, k), reverse=True):
        row: dict[str, Any] = {"fiscal_year": fy, "fiscal_period": fp}
        for metric, rows in metric_quarters.items():
            if (fy, fp) in rows:
                row[metric] = rows[(fy, fp)]
        ends = [str(v["period_end"]) for v in row.values() if isinstance(v, dict) and v.get("period_end")]
        row["period_end"] = max(ends) if ends else None
        merged.append(row)
    return merged


def _build_ttm(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    latest = rows[:4]
    if len(latest) < 4:
        return None
    ttm: dict[str, Any] = {
        "period_type": "TTM",
        "period_end": latest[0].get("period_end"),
        "quarters_included": [
            {"fiscal_year": r.get("fiscal_year"), "fiscal_period": r.get("fiscal_period"), "period_end": r.get("period_end")}
            for r in latest
        ],
        "derived": True,
    }
    for metric in ("revenue", "operating_income", "operating_cash_flow", "capex", "free_cash_flow"):
        values = [_metric_value(row, metric) for row in latest]
        if all(value is not None for value in values):
            numeric_values = [value for value in values if value is not None]
            ttm[metric] = {"value": round(sum(numeric_values), 2), "unit": "USD", "derived": True, "formula": "sum(latest_4_quarters)"}
    return ttm


def _add_quarter_derived(row: dict[str, Any]) -> None:
    revenue = _metric_value(row, "revenue")
    cost = _metric_value(row, "cost_of_revenue")
    if "gross_profit" not in row and revenue is not None and cost is not None:
        row["gross_profit"] = {"value": revenue - cost, "unit": "USD", "derived": True, "formula": "revenue - cost_of_revenue"}
    ocf = _metric_value(row, "operating_cash_flow")
    capex = _metric_value(row, "capex")
    if ocf is not None and capex is not None:
        row["free_cash_flow"] = {"value": ocf - abs(capex), "unit": "USD", "derived": True, "formula": "operating_cash_flow - abs(capex)"}


def _metric_value(row: dict[str, Any], metric: str) -> float | None:
    value = row.get(metric)
    return safe_float(value.get("value")) if isinstance(value, dict) else None


def _better_annual(prev: dict[str, Any] | None, fact: dict[str, Any]) -> dict[str, Any]:
    if prev is None:
        return fact
    return fact if str(fact.get("filed") or "") > str(prev.get("filed") or "") else prev


def _better_quarter(prev: dict[str, Any] | None, fact: dict[str, Any]) -> dict[str, Any]:
    if prev is None:
        return fact
    return fact if str(fact.get("filed") or "") > str(prev.get("filed") or "") else prev


def _duration_days(item: dict[str, Any]) -> int | None:
    start = _date(item.get("start"))
    end = _date(item.get("end"))
    return (end - start).days if start is not None and end is not None else None


def _date(value: Any) -> datetime | None:
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sort_end(metric_quarters: dict[str, dict[tuple[int, str], dict[str, Any]]], key: tuple[int, str]) -> str:
    for rows in metric_quarters.values():
        value = rows.get(key)
        if value is not None and value.get("period_end"):
            return str(value["period_end"])
    return ""
