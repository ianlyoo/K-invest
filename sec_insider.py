#!/usr/bin/env python3
"""SEC Form 4 insider-trade parsing helpers."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Any

SUBMISSIONS_BASE = "https://data.sec.gov/submissions"


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_date(value: Any) -> datetime | None:
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d")
    except ValueError:
        return None


def get_insider_trades(
    client, ticker: str, days_back: int = 180, detail_level: str = "summary"
) -> dict[str, Any]:
    cik = client.get_cik(ticker)
    if not cik:
        return {"error": f"CIK not found for ticker {ticker}"}

    submissions = client._get_json(f"{SUBMISSIONS_BASE}/CIK{cik}.json")
    if not submissions:
        return {"error": f"Submissions not available for {ticker}"}

    filings = submissions.get("filings", {})
    recent = filings.get("recent", {})
    if not isinstance(recent, dict):
        return {"ticker": ticker, "cik": cik, "summary": {}, "transactions": []}

    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)
    forms = recent.get("form", [])
    filing_dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_documents = recent.get("primaryDocument", [])

    trades: list[dict[str, Any]] = []
    row_count = min(len(forms), len(filing_dates), len(accessions), len(primary_documents))
    for i in range(row_count):
        form = str(forms[i] or "").upper()
        if form not in {"4", "4/A"}:
            continue
        filing_date_str = str(filing_dates[i] or "")
        filing_date = _parse_date(filing_date_str)
        if filing_date is None or filing_date < start_date or filing_date > end_date:
            continue
        accession = str(accessions[i] or "")
        primary_doc = str(primary_documents[i] or "")
        accession_clean = accession.replace("-", "")
        doc_name = primary_doc.split("/")[-1]
        filing_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_clean}/{doc_name}"
        xml_text = client._get_text(filing_url)
        if not xml_text:
            continue
        trades.extend(_parse_form4_xml(xml_text, ticker, filing_date_str, accession, filing_url))

    deduped = _dedupe_trades(trades)
    aggregated = _aggregate_insider_trades(deduped)
    open_market_buys = sum(1 for tx in aggregated if tx.get("transaction_code") == "P")
    open_market_sales = sum(1 for tx in aggregated if tx.get("transaction_code") == "S")
    net_value = sum(_safe_float(tx.get("signed_value")) or 0 for tx in aggregated)
    result = {
        "ticker": ticker.upper(),
        "cik": cik,
        "days_back": days_back,
        "summary": {
            "open_market_buys": open_market_buys,
            "open_market_sales": open_market_sales,
            "net_value": round(net_value, 2),
            "raw_lot_count": len(deduped),
            "aggregated_transaction_count": len(aggregated),
        },
        "transactions": aggregated[:50],
    }
    if detail_level == "full":
        result["raw_transactions"] = deduped[:200]
    return result


def _dedupe_trades(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str, str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for trade in trades:
        key = (
            str(trade.get("name", "")),
            str(trade.get("transaction_code", "")),
            str(trade.get("transaction_date", "")),
            str(trade.get("shares", "")),
            str(trade.get("price_per_share", "")),
            str(trade.get("accession_number", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(trade)
    return deduped


def _aggregate_insider_trades(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for trade in trades:
        key = (
            str(trade.get("transaction_date") or ""),
            str(trade.get("name") or ""),
            str(trade.get("transaction_code") or ""),
            str(trade.get("accession_number") or ""),
        )
        shares = _safe_float(trade.get("shares")) or 0
        price = _safe_float(trade.get("price_per_share")) or 0
        value = shares * price
        row = grouped.setdefault(key, {
            "date": key[0],
            "insider": key[1],
            "transaction_code": key[2],
            "accession_number": key[3],
            "filing_url": trade.get("url"),
            "title": trade.get("title", ""),
            "is_ten_pct_owner": trade.get("is_ten_pct_owner", False),
            "total_shares": 0.0,
            "total_value": 0.0,
            "lot_count": 0,
        })
        row["total_shares"] += shares
        row["total_value"] += value
        row["lot_count"] += 1
    rows = []
    for row in grouped.values():
        shares = row["total_shares"]
        total_value = row["total_value"]
        code = row["transaction_code"]
        row["weighted_average_price"] = round(total_value / shares, 4) if shares else None
        row["total_shares"] = round(shares, 4)
        row["total_value"] = round(total_value, 2)
        row["signed_value"] = round(total_value if code == "P" else -total_value if code == "S" else 0, 2)
        row["ten_b5_1_plan"] = None
        rows.append(row)
    rows.sort(key=lambda r: (str(r.get("date")), abs(float(r.get("total_value") or 0))), reverse=True)
    return rows


def _parse_form4_xml(
    xml_text: str, ticker: str, filing_date: str, accession: str, url: str
) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return trades

    ns_tag = root.tag.split("}")[0] + "}" if "}" in root.tag else ""
    issuer_ticker = ""
    issuer = root.find(f"{ns_tag}issuer")
    if issuer is not None:
        for child in issuer:
            if child.tag.split("}")[-1] == "issuerTradingSymbol":
                issuer_ticker = (child.text or "").strip()

    owner = root.find(f"{ns_tag}reportingOwner")
    owner_name = ""
    owner_title = ""
    is_ten_pct = False
    if owner is not None:
        rid = owner.find(f"{ns_tag}reportingOwnerId")
        if rid is not None:
            for child in rid:
                if child.tag.split("}")[-1] == "rptOwnerName":
                    owner_name = (child.text or "").strip()
        rel = owner.find(f"{ns_tag}reportingOwnerRelationship")
        if rel is not None:
            for child in rel:
                tag = child.tag.split("}")[-1]
                if tag == "officerTitle":
                    owner_title = (child.text or "").strip()
                elif tag == "isTenPercentOwner" and (child.text or "").strip() == "1":
                    is_ten_pct = True

    ndt = root.find(f"{ns_tag}nonDerivativeTable")
    if ndt is not None:
        for tx_node in ndt.findall(f"{ns_tag}nonDerivativeTransaction"):
            tx = _parse_transaction(tx_node, ns_tag)
            if tx:
                tx.update({
                    "name": owner_name,
                    "title": owner_title,
                    "is_ten_pct_owner": is_ten_pct,
                    "ticker": issuer_ticker or ticker.upper(),
                    "filing_date": filing_date,
                    "accession_number": accession,
                    "url": url,
                })
                trades.append(tx)
    return trades


def _parse_transaction(tx_node: ET.Element, ns_tag: str) -> dict[str, Any] | None:
    tx_date = ""
    security_title = ""
    tx_code = ""
    shares = None
    price = None
    acquired_disposed = ""
    for child in tx_node:
        tag = child.tag.split("}")[-1]
        if tag == "transactionDate":
            d = child.find(f"{ns_tag}value")
            if d is not None:
                tx_date = d.text or ""
        elif tag == "securityTitle":
            d = child.find(f"{ns_tag}value")
            if d is not None:
                security_title = (d.text or "").strip()
        elif tag == "transactionCoding":
            for sub in child:
                if sub.tag.split("}")[-1] == "transactionCode":
                    tx_code = (sub.text or "").strip()
        elif tag == "transactionAmounts":
            for sub in child:
                sub_tag = sub.tag.split("}")[-1]
                if sub_tag == "transactionShares":
                    val = sub.find(f"{ns_tag}value")
                    if val is not None:
                        shares = val.text
                elif sub_tag == "transactionPricePerShare":
                    val = sub.find(f"{ns_tag}value")
                    if val is not None:
                        price = val.text
                elif sub_tag == "transactionAcquiredDisposedCode":
                    val = sub.find(f"{ns_tag}value")
                    if val is not None:
                        acquired_disposed = (val.text or "").strip()
    if not tx_code:
        return None
    return {
        "transaction_date": tx_date,
        "security_title": security_title,
        "transaction_code": tx_code,
        "shares": shares,
        "price_per_share": price,
        "acquired_disposed": acquired_disposed,
        "is_discretionary": tx_code in ("P", "S"),
    }
