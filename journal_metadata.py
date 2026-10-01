"""Additive identity and coverage metadata; no checkpoint or cursor implementation."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from health_status import timestamp


def legacy_kis_account_key(source: str) -> str:
    """Translate ledger scope aliases only; never rewrite a canonical registry key."""
    if source in {"kis:domestic", "kis:overseas"}:
        return "kis:primary"
    if source.startswith("kis:") and source.endswith((":domestic", ":overseas")):
        return source.rsplit(":", 1)[0]
    return source


def identity(provider: str, account_key: str, market: str, native_id: str,
             trade_date: str, *, kind: str = "order_aggregate", sequence: str = "") -> dict[str, Any]:
    if len(trade_date) == 8 and trade_date.isdigit():
        trade_date = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
    parts = ["v1", provider, account_key, market, trade_date, native_id, sequence]
    execution_id = "ki-v1-" + hashlib.sha256(json.dumps(parts, ensure_ascii=True).encode()).hexdigest() if native_id else None
    parent = "ki-order-v1-" + hashlib.sha256(json.dumps(parts[:-1]).encode()).hexdigest() if native_id else None
    return {"execution_id": execution_id, "identity_version": 1,
            "identity_quality": "native_execution" if kind == "execution" and native_id else "order_aggregate" if native_id else "unavailable",
            "record_kind": kind, "parent_order_id": parent,
            "account_key": account_key}


def annotate_rows(rows: list[dict], provider: str, account_key: str, market: str = "") -> list[dict]:
    result = []
    for raw in rows:
        row = dict(raw)
        if provider == "toss":
            native = str(row.get("orderId") or "")
            day = str(row.get("orderedAt") or row.get("execution", {}).get("filledAt") or "")[:10]
            currency = str(row.get("currency") or "")
            scope = market or ("KR" if currency == "KRW" or str(row.get("symbol", "")).isdigit() else "US")
        else:
            native = str(row.get("odno") or "")
            day = str(row.get("ord_dt") or row.get("trad_dt") or "")
            scope = market or str(row.get("market") or row.get("ovrs_excg_cd") or "overseas")
        row.update(identity(provider, account_key, scope, native, day))
        result.append(row)
    return result


def source_metadata(source: str, count: int | None, *, complete: bool | None,
                    truncated: bool = False, error_code: str | None = None,
                    start: str | None = None, end: str | None = None,
                    reason: str | None = None, supported: bool = True) -> dict:
    status = "unsupported" if not supported else error_code if error_code else \
        "truncated" if truncated else "partial" if complete is not True else "ok_empty" if count == 0 else "ok"
    return {"source": source, "status": status, "result_count": count,
            "complete": complete, "truncated": truncated, "truncation_reason": reason,
            "error_code": error_code, "fetched_at": timestamp(),
            "as_of": None, "freshness_seconds": None,
            "freshness_basis": "fetch_only; upstream as_of unavailable",
            "requested_window": {"start": start, "end": end},
            "coverage_scope": "returned broker history; retention not guaranteed",
            "supported": supported}
