#!/usr/bin/env python3
from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Any

from sec_derived import safe_float

REVENUE_TAGS = {"Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"}
SEGMENT_KEYS = {
    "handsets": "HandsetsMember",
    "automotive": "AutomotiveMember",
    "iot": "IoTMember",
    "internet_of_things": "InternetOfThingsMember",
    "qct": "QctMember",
    "qtl": "QtlMember",
}


def get_segment_revenue(
    get_json: Callable[[str], dict[str, Any] | None],
    get_text: Callable[[str], str | None],
    cik: str,
    ticker: str,
) -> dict[str, Any]:
    filing = _latest_filing(get_json, cik)
    if filing is None:
        return _empty(ticker, "No recent 10-K/10-Q filing found")
    instance_url = _instance_url(get_json, cik, filing["accession"])
    if instance_url is None:
        return _empty(ticker, "No XBRL instance document found")
    text = get_text(instance_url)
    if not text:
        return _empty(ticker, "XBRL instance fetch failed")
    records = _parse_segments(text)
    return {
        "symbol": ticker.upper(),
        "filing": filing,
        "instance_url": instance_url,
        "latest_quarter": _latest_period(records, quarterly=True),
        "latest_ytd": _latest_period(records, quarterly=False),
        "records": records[:40],
        "methodology": "Best-effort parse of segment-member revenue facts from latest SEC iXBRL instance.",
        "warnings": [] if records else ["No recognized QCT/QTL/product segment revenue facts found"],
    }


def _latest_filing(get_json: Callable[[str], dict[str, Any] | None], cik: str) -> dict[str, str] | None:
    data = get_json(f"https://data.sec.gov/submissions/CIK{cik}.json")
    recent = (data or {}).get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    dates = recent.get("filingDate", [])
    for form, accession, filing_date in zip(forms, accessions, dates, strict=False):
        if form in {"10-Q", "10-K"}:
            return {"form": str(form), "accession": str(accession), "filing_date": str(filing_date)}
    return None


def _instance_url(
    get_json: Callable[[str], dict[str, Any] | None],
    cik: str,
    accession: str,
) -> str | None:
    accession_dir = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_dir}"
    index = get_json(f"{base}/index.json")
    items = (index or {}).get("directory", {}).get("item", [])
    names = [str(item.get("name")) for item in items if isinstance(item, dict)]
    for name in names:
        if name.endswith("_htm.xml"):
            return f"{base}/{name}"
    for name in names:
        if name.endswith(".xml") and not any(suffix in name for suffix in ("_cal.", "_def.", "_lab.", "_pre.", "FilingSummary")):
            return f"{base}/{name}"
    return None


def _parse_segments(text: str) -> list[dict[str, Any]]:
    root = ET.fromstring(text.encode())
    contexts = _contexts(root)
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for elem in root.iter():
        concept = _local_name(elem.tag)
        if concept not in REVENUE_TAGS:
            continue
        context = contexts.get(elem.attrib.get("contextRef") or "")
        if context is None:
            continue
        category = _category(context["members"])
        if category is None:
            continue
        value = safe_float(elem.text)
        if value is None:
            continue
        key = (category, context["start"] or "", context["end"] or "")
        record = {
            "segment": category,
            "value": value,
            "unit": "USD",
            "start": context["start"],
            "end": context["end"],
            "duration_days": context["duration_days"],
            "concept": concept,
            "members": context["members"],
        }
        previous = by_key.get(key)
        if previous is None or concept == "RevenueFromContractWithCustomerExcludingAssessedTax":
            by_key[key] = record
    return sorted(by_key.values(), key=lambda r: (str(r.get("end") or ""), int(r.get("duration_days") or 0)), reverse=True)


def _contexts(root: ET.Element) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for ctx in root.findall(".//{http://www.xbrl.org/2003/instance}context"):
        cid = ctx.attrib.get("id")
        if not cid:
            continue
        members = [
            member.text or ""
            for member in ctx.findall(".//{http://xbrl.org/2006/xbrldi}explicitMember")
        ]
        start = _child_text(ctx, "startDate")
        end = _child_text(ctx, "endDate") or _child_text(ctx, "instant")
        result[cid] = {
            "members": members,
            "start": start,
            "end": end,
            "duration_days": _duration_days(start, end),
        }
    return result


def _latest_period(records: list[dict[str, Any]], quarterly: bool) -> list[dict[str, Any]]:
    filtered = [
        record for record in records
        if (60 <= int(record.get("duration_days") or 0) <= 120) == quarterly
    ]
    if not filtered:
        return []
    latest_end = max(str(record.get("end") or "") for record in filtered)
    return [record for record in filtered if record.get("end") == latest_end]


def _category(members: list[str]) -> str | None:
    joined = " ".join(members)
    for key, marker in SEGMENT_KEYS.items():
        if marker in joined:
            if key in {"handsets", "automotive", "iot", "internet_of_things"} and "QctMember" not in joined:
                continue
            return "iot" if key == "internet_of_things" else key
    return None


def _child_text(ctx: ET.Element, local: str) -> str | None:
    child = ctx.find(f".//{{http://www.xbrl.org/2003/instance}}{local}")
    return child.text if child is not None else None


def _duration_days(start: str | None, end: str | None) -> int | None:
    from datetime import datetime

    if start is None or end is None:
        return None
    return (datetime.strptime(end[:10], "%Y-%m-%d") - datetime.strptime(start[:10], "%Y-%m-%d")).days


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _empty(ticker: str, warning: str) -> dict[str, Any]:
    return {
        "symbol": ticker.upper(),
        "filing": None,
        "latest_quarter": [],
        "latest_ytd": [],
        "records": [],
        "methodology": "Best-effort parse of segment-member revenue facts from latest SEC iXBRL instance.",
        "warnings": [warning],
    }
