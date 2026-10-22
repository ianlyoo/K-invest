#!/usr/bin/env python3
"""Daily portfolio NAV snapshot store (SQLite).

Immutable versions with one representative per calendar date. Recollection moves
the representative while retaining every earlier version for reproducible replay.
It holds normalized valuation inputs, never credentials or raw broker payloads.

Location: `$KINVEST_PORTFOLIO_DB`, else `portfolio.db` under `cache_dir()`
(`KINVEST_CACHE_DIR`). Under systemd that path must be in `ReadWritePaths=`.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

import daily_nav
from kinvest_common import cache_dir as _default_cache_dir

logger = logging.getLogger("k-invest.portfolio_store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nav_snapshots (
    snapshot_date      TEXT PRIMARY KEY,
    total_value_krw    REAL NOT NULL,
    stock_value_krw    REAL,
    cash_value_krw     REAL,
    cost_basis_krw     REAL,
    unrealized_pnl_krw REAL,
    position_count     INTEGER,
    fx_usdkrw          REAL,
    breakdown_json     TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nav_snapshot_versions (
    snapshot_id TEXT PRIMARY KEY,
    snapshot_date TEXT NOT NULL,
    version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE(snapshot_date, version)
);
CREATE TABLE IF NOT EXISTS nav_snapshot_representatives (
    snapshot_date TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES nav_snapshot_versions(snapshot_id)
);
CREATE TABLE IF NOT EXISTS nav_collection_attempts (
    attempt_id INTEGER PRIMARY KEY,
    snapshot_date TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    status TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS nav_versions_no_update
BEFORE UPDATE ON nav_snapshot_versions BEGIN
    SELECT RAISE(ABORT, 'snapshot versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS nav_versions_no_delete
BEFORE DELETE ON nav_snapshot_versions BEGIN
    SELECT RAISE(ABORT, 'snapshot versions are immutable');
END;
"""

_COLUMNS = (
    "snapshot_date",
    "total_value_krw",
    "stock_value_krw",
    "cash_value_krw",
    "cost_basis_krw",
    "unrealized_pnl_krw",
    "position_count",
    "fx_usdkrw",
    "breakdown_json",
    "created_at",
    "updated_at",
)


def default_db_path() -> Path:
    configured = os.environ.get("KINVEST_PORTFOLIO_DB", "").strip()
    if configured:
        return Path(configured).expanduser()
    return _default_cache_dir() / "portfolio.db"


class NavStore:
    """SQLite-backed daily NAV snapshots."""

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else default_db_path()
        self._wal_lock = Lock()

    def _ensure_wal(self, conn):
        """Serialize mode setup; SQLite's busy timeout may miss its lock race.

        Check the persisted mode first, so an existing WAL database receives no
        mode-changing statement. Recheck on each connection rather than cache a
        flag across database replacement. Other instances/processes are covered
        by two short retries of the specific transient lock error.
        """
        with self._wal_lock:
            for attempt in range(3):
                try:
                    if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
                        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                        if str(mode).lower() != "wal":
                            raise sqlite3.OperationalError("WAL initialization did not enable WAL")
                    return
                except sqlite3.OperationalError as exc:
                    if str(exc).lower() != "database is locked" or attempt == 2:
                        raise
                    time.sleep(.025 * (attempt + 1))

    @contextmanager
    def _connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            self._ensure_wal(conn)
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(_SCHEMA)
            # Serialize migration, version allocation and pointer changes together.
            conn.execute("BEGIN IMMEDIATE")
            with conn:
                legacy = conn.execute("""
                    SELECT n.*, v.payload_json AS version_payload FROM nav_snapshots n
                    LEFT JOIN nav_snapshot_representatives r USING(snapshot_date)
                    LEFT JOIN nav_snapshot_versions v USING(snapshot_id)
                """).fetchall()
                for row in legacy:
                    payload, nonfinite = _sanitize_legacy_values(_row_to_dict(row))
                    previous = json.loads(row["version_payload"]) if row["version_payload"] else None
                    if previous and all(previous.get(key) == value for key, value in payload.items()):
                        continue
                    version = conn.execute(
                        "SELECT COALESCE(MAX(version), 0)+1 FROM nav_snapshot_versions WHERE snapshot_date=?",
                        (row["snapshot_date"],),
                    ).fetchone()[0]
                    payload.update({
                        "snapshot_id": uuid4().hex, "version": version,
                        "valuation_at": None, "observed_at": None,
                        "components": None, "fx_times": None,
                        "calculation_version": "legacy_unknown", "result_status": "partial",
                        "nav_bridge": None, "settlement_adjusted_nav_krw": None,
                        "reason_codes": ["LEGACY_SNAPSHOT_PROVENANCE_UNKNOWN"] +
                            (["LEGACY_SNAPSHOT_NONFINITE_VALUE"] if nonfinite else []),
                    })
                    self._insert_version(conn, payload)
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _insert_version(conn, payload, representative=True):
        conn.execute("INSERT INTO nav_snapshot_versions VALUES (?, ?, ?, ?)", (
            payload["snapshot_id"], payload["snapshot_date"], payload["version"],
            json.dumps(payload, ensure_ascii=False, allow_nan=False),
        ))
        if not representative:
            return
        conn.execute("""
            INSERT INTO nav_snapshot_representatives VALUES (?, ?)
            ON CONFLICT(snapshot_date) DO UPDATE SET snapshot_id=excluded.snapshot_id
        """, (payload["snapshot_date"], payload["snapshot_id"]))

    def upsert_snapshot(
        self, summary: dict[str, Any], snapshot_date: str | None = None,
        fx_usdkrw: float | None = None, *, keep_crypto_representative: bool = False,
    ) -> dict[str, Any]:
        """Append an immutable version and atomically move the daily representative.

        The legacy table remains a compatible daily projection. Historical dates
        with current/unknown valuation inputs are labelled, never silently backfilled.
        keep_crypto_representative (tool-path writes): only the scheduled capture
        carries crypto FX evidence (``crypto_nav.fx_times``) and the KIS capture
        (``kis_capture``), so a later same-day version without either is stored but
        does not replace a representative that has it; the returned payload then
        has ``representative`` False.
        """
        total = summary.get("total_assets_krw")
        if total is None:
            raise ValueError("cannot snapshot a portfolio summary without total_assets_krw")
        when = _date(snapshot_date or date.today().isoformat())
        now = _now()
        breakdown = {key: summary.get(key) for key in (
            "by_currency", "by_market", "by_account", "cash_by_currency")}
        values = (when, float(total), summary.get("stock_value_krw"), summary.get("cash_krw"),
                  summary.get("cost_basis_krw"), summary.get("unrealized_pnl_krw"),
                  summary.get("position_count"), fx_usdkrw,
                  json.dumps(breakdown, ensure_ascii=False, allow_nan=False), now, now)
        payload = dict(zip(_COLUMNS, values))
        payload.pop("breakdown_json")
        payload["breakdown"] = breakdown
        payload.update(_snapshot_metadata(summary, when, now))
        with self._connect() as conn:
            payload.update(daily_nav.crypto_nav(conn, summary))
            payload['external_flows'] = daily_nav.external_flows(conn, summary.get('valuation_at'))
            previous = conn.execute("SELECT created_at FROM nav_snapshots WHERE snapshot_date=?", (when,)).fetchone()
            if previous:
                payload["created_at"] = previous[0]
            payload["snapshot_id"] = uuid4().hex
            payload["version"] = conn.execute(
                "SELECT COALESCE(MAX(version), 0)+1 FROM nav_snapshot_versions WHERE snapshot_date=?", (when,)
            ).fetchone()[0]
            keep = False
            if keep_crypto_representative:
                current = conn.execute("""
                    SELECT v.payload_json FROM nav_snapshot_versions v
                    JOIN nav_snapshot_representatives r USING(snapshot_id) WHERE r.snapshot_date=?
                """, (when,)).fetchone()
                stored = json.loads(current[0]) if current else {}
                # Only the scheduled capture carries crypto FX and KIS capture evidence.
                keep = any(has(stored) and not has(payload) for has in (
                    lambda p: 'fx_times' in (p.get('crypto_nav') or {}), lambda p: bool(p.get('kis_capture'))))
            if keep:
                self._insert_version(conn, payload, representative=False)
            else:
                conn.execute("""
                    INSERT INTO nav_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(snapshot_date) DO UPDATE SET
                        total_value_krw=excluded.total_value_krw,
                        stock_value_krw=excluded.stock_value_krw, cash_value_krw=excluded.cash_value_krw,
                        cost_basis_krw=excluded.cost_basis_krw, unrealized_pnl_krw=excluded.unrealized_pnl_krw,
                        position_count=excluded.position_count, fx_usdkrw=excluded.fx_usdkrw,
                        breakdown_json=excluded.breakdown_json, updated_at=excluded.updated_at
                """, values)
                self._insert_version(conn, payload)
            # Source success is distinct from settlement/market-mark quality.
            success = summary.get("collection_complete", summary.get("result_status") == "complete")
            if "SNAPSHOT_DATE_MISMATCH" in payload["reason_codes"]:
                success = False
            conn.execute("INSERT INTO nav_collection_attempts(snapshot_date, observed_at, status, reason_codes_json) VALUES (?, ?, ?, ?)",
                         (when, now, "complete" if success else "partial", json.dumps(payload["reason_codes"])))
        return {**payload, "representative": False} if keep else payload

    def enrich_summary(self, summary):
        """Add local evidence without creating a DB or fetching another source."""
        conn = None
        try:
            if self.db_path.is_file():
                conn = sqlite3.connect(self.db_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
                conn.execute('BEGIN')
            return {**daily_nav.crypto_nav(conn, summary),
                    'external_flows': daily_nav.external_flows(conn, summary.get('valuation_at'))}
        finally:
            if conn is not None:
                conn.close()

    def get_snapshot(self, snapshot_date: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("""
                SELECT v.payload_json FROM nav_snapshot_versions v
                JOIN nav_snapshot_representatives r USING(snapshot_id)
                WHERE r.snapshot_date=?
            """, (_date(snapshot_date),)).fetchone()
        return _version_payload(row[0]) if row else None

    def range(self, start: str, end: str, limit: int = 3650) -> list[dict[str, Any]]:
        """Daily representatives, inclusive and oldest first."""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT v.payload_json FROM nav_snapshot_versions v
                JOIN nav_snapshot_representatives r USING(snapshot_id)
                WHERE r.snapshot_date >= ? AND r.snapshot_date <= ?
                ORDER BY r.snapshot_date LIMIT ?
            """, (_date(start), _date(end), limit)).fetchall()
        return [_version_payload(row[0]) for row in rows]

    def select_snapshots(self, snapshot_ids: list[str]) -> list[dict[str, Any]]:
        """Resolve exactly the requested versions; reject ambiguous daily series."""
        if not snapshot_ids or len(snapshot_ids) != len(set(snapshot_ids)):
            raise ValueError("input_snapshot_ids must be nonempty and unique")
        with self._connect() as conn:
            rows = []
            for snapshot_id in snapshot_ids:
                row = conn.execute("SELECT payload_json FROM nav_snapshot_versions WHERE snapshot_id=?", (snapshot_id,)).fetchone()
                if not row:
                    raise ValueError(f"unknown input_snapshot_id: {snapshot_id}")
                rows.append(_version_payload(row[0]))
        if len({row["snapshot_date"] for row in rows}) != len(rows):
            raise ValueError("select exactly one snapshot version per date")
        return sorted(rows, key=lambda row: row["snapshot_date"])

    def record_attempt(self, snapshot_date: str, status: str, reason_codes: list[str]) -> None:
        """Persist a refusal/failure without inventing a NAV observation."""
        with self._connect() as conn:
            conn.execute("INSERT INTO nav_collection_attempts(snapshot_date, observed_at, status, reason_codes_json) VALUES (?, ?, ?, ?)",
                         (_date(snapshot_date), _now(), status, json.dumps(reason_codes)))

    def collection_health(self, start: str, end: str, expected_days: list[str] | None = None,
                          navs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Attempt-based success rate, and gaps only against a caller's calendar.

        No calendar means unknown expected days, including weekends/holidays.
        Migration does not fabricate attempts for historical rows.
        """
        start, end = _date(start), _date(end)
        expected = None if expected_days is None else sorted({_date(day) for day in expected_days if start <= _date(day) <= end})
        rows = navs if navs is not None else self.range(start, end)
        present = {row["snapshot_date"] for row in rows}
        with self._connect() as conn:
            count, successes, last = conn.execute("""
                SELECT COUNT(*), COALESCE(SUM(status='complete'), 0),
                    MAX(CASE WHEN status='complete' THEN observed_at END)
                FROM nav_collection_attempts WHERE snapshot_date >= ? AND snapshot_date <= ?
            """, (start, end)).fetchone()
        return {"last_success_at": last, "attempt_count": count, "success_count": successes,
                "success_rate": successes / count if count else None,
                "success_definition": "complete_source_collection_and_snapshot_write",
                "period": {"start": start, "end": end},
                "expected_days": expected, "calendar_status": "unknown" if expected is None else "explicit",
                "expected_day_gaps": None if expected is None else sorted(set(expected) - present)}

    def count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM nav_snapshots").fetchone()[0])

    def bounds(self) -> tuple[str | None, str | None]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MIN(snapshot_date), MAX(snapshot_date) FROM nav_snapshots"
            ).fetchone()
        return (row[0], row[1]) if row else (None, None)


def _sanitize_legacy_values(value: Any) -> tuple[Any, bool]:
    """Keep legacy bytes untouched; expose nonfinite measurements as unknown."""
    if isinstance(value, float) and not math.isfinite(value):
        return None, True
    if isinstance(value, (dict, list)):
        items = value.items() if isinstance(value, dict) else enumerate(value)
        cleaned, invalid = {}, False
        for key, item in items:
            cleaned[key], bad = _sanitize_legacy_values(item)
            invalid |= bad
        return (cleaned if isinstance(value, dict) else list(cleaned.values())), invalid
    return value, False


def _version_payload(raw):
    """Legacy versions expose unknown additive evidence without inventing history."""
    data = json.loads(raw)
    if 'account_nav' not in data:
        data.update(account_nav=None, valuation_timezone=None,
                    mark_quality={'status': 'unknown', 'reason_codes': ['LEGACY_MARK_PROVENANCE_UNKNOWN']},
                    external_flows=daily_nav.external_flows(None, None))
        data.update(daily_nav.crypto_nav(None, {}))
        data['daily_nav_reason_codes'] = ['LEGACY_DAILY_NAV_EVIDENCE_UNKNOWN']
    return data


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = {key: row[key] for key in _COLUMNS if key in row.keys()}
    raw = data.pop("breakdown_json", None)
    if raw:
        try:
            data["breakdown"] = json.loads(raw)
        except json.JSONDecodeError:
            data["breakdown"] = None
    return data


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


CALCULATION_VERSION = "portfolio_nav_v2"


def _date(value: str) -> str:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("date must be ISO YYYY-MM-DD")
    return value


def validate_live_snapshot_date(value: str | None) -> str:
    when = _date(value or date.today().isoformat())
    if when != date.today().isoformat():
        raise ValueError("current prices cannot be recorded as a past/future snapshot_date")
    return when


def snapshot_summary(summary: dict[str, Any], collection, fx) -> dict[str, Any]:
    """Capture normalized valuation inputs; fetch time is never quote time.

    Toss native position values and timestamps come from one batched quote;
    legacy component values and all legacy totals retain their original prices. Cash
    marks use the balance read's observation time (no market quote is involved).
    KIS holdings and explicit priced_at attributes retain their prior meaning.
    FxRates.as_of is the collection time, not the underlying close timestamp.
    """
    data = dict(summary)
    data["valuation_at"] = collection.fetched_at or _now()
    data["observed_at"] = data["valuation_at"]
    data['nav_evidence_completed_at'] = getattr(collection, 'nav_completed_at', None)
    data["components"] = [dict(item.to_dict(), component_type=kind,
                               priced_at=getattr(item, "priced_at", None),
                               **({'valuation_price_decimal': item.last_price_decimal}
                                  if kind == 'holding' and getattr(item, 'provider', None) == 'toss'
                                  and getattr(item, 'last_price_decimal', None) is not None else {}))
                          for kind, items in (("holding", collection.holdings), ("cash", collection.cash))
                          for item in items]
    for component, item in zip(data['components'], [*collection.holdings, *collection.cash]):
        if component.get('provider') == 'toss' and component['component_type'] == 'holding':
            component['quantity_decimal'] = getattr(item, 'quantity_decimal', None)
    evidence = getattr(collection, 'nav_evidence', {})
    for component in data['components']:
        account = component.get('account')
        account_evidence = evidence.get(account) or {}
        if component['component_type'] == 'holding' and component.get('provider') == 'toss':
            inventory_reasons = getattr(collection, 'nav_source_quality_reasons', {}).get(account, [])
            component.update(daily_nav.toss_holding_valuation(component, account_evidence, source_reasons=inventory_reasons))
            market = getattr(collection, 'nav_holding_markets', {}).get(account, {}).get(component.get('symbol'))
            component['market_calendar'] = account_evidence.get('market_calendars', {}).get(market)
            component['price_time_basis'] = 'toss_prices.timestamp'
        elif account not in evidence:
            continue
        elif component['component_type'] == 'cash':
            state = getattr(collection, 'health', {}).get(f"{account}:{component['currency'].lower()}") or {}
            component['priced_at'] = state.get('last_success_at')
            component['price_time_basis'] = 'balance_observation'
        component.setdefault('reason_codes', []).extend(account_evidence.get('reason_codes', []))
    quote_times = getattr(fx, "priced_at_by_currency", {})
    valid_until = getattr(fx, 'valid_until_by_currency', {})
    read_times = getattr(fx, 'read_at_by_currency', {})
    data["fx_times"] = {currency: {"rate_to_krw": rate, "priced_at": quote_times.get(currency),
                                 **({'rate_decimal': fx.rate_decimal_by_currency[currency]}
                                    if currency in getattr(fx, 'rate_decimal_by_currency', {}) else {}),
                                 "observed_at": fx.as_of,
                                 **({'valid_until': valid_until[currency]} if currency in valid_until else {}),
                                 **({'read_at': read_times[currency]} if currency in read_times else {})}
                        for currency, rate in sorted(fx.rates.items())}
    fx_quality = getattr(fx, 'nav_source_quality_reasons', {})
    for currency, codes in fx_quality.items():
        data['fx_times'].setdefault(currency, {'rate_to_krw': None, 'priced_at': None})['reason_codes'] = codes
    reasons = list(data.get("reason_codes", []))
    for codes in fx_quality.values():
        reasons.extend(codes)
    if collection.failed_sources:
        reasons.append("FAILED_SOURCES")
    if collection.truncated_sources:
        reasons.append("TRUNCATED_SOURCES")
    if fx.missing:
        reasons.append("MISSING_FX_RATE")
    data["collection_complete"] = not (collection.failed_sources or collection.truncated_sources)
    data['collection_source_quality'] = {'failed_sources': collection.failed_sources,
                                         'truncated_sources': collection.truncated_sources,
                                         'reason_codes_by_source': getattr(collection, 'nav_source_quality_reasons', {})}
    # NAV input validation is additive: preserve legacy fetch completeness and
    # the bridge while retaining source ownership for scoped blocking reasons.
    source_reasons = data['collection_source_quality']['reason_codes_by_source']
    for codes in source_reasons.values():
        reasons.extend(codes)
    if source_reasons or fx_quality:
        data['result_status'] = 'partial'
    if not data["collection_complete"]:
        bridge = dict(data.get("nav_bridge") or {})
        bridge["settlement_adjusted_nav_krw"] = None
        reconciliation = dict(bridge.get("reconciliation") or {})
        reconciliation["status"] = "unavailable"
        reconciliation["difference_reasons"] = list(dict.fromkeys(
            reconciliation.get("difference_reasons", []) + ["INCOMPLETE_PORTFOLIO_SOURCES"]))
        bridge["reconciliation"] = reconciliation
        data["nav_bridge"] = bridge
        data["result_status"] = "partial"
    else:
        data.setdefault("result_status", "complete")
    data["reason_codes"] = reasons
    data['account_nav'] = daily_nav.account_nav(collection, data['components'], fx_quality)
    data['valuation_timezone'] = daily_nav.VALUATION_TIMEZONE
    data['mark_quality'] = daily_nav.mark_quality(data)
    data['quality_reason_details'] = daily_nav.quality_reason_details(data, reasons)
    return data


def _snapshot_metadata(summary, when, now):
    valuation_at = summary.get("valuation_at")
    observed_at = summary.get("observed_at", now)
    components = summary.get("components")
    fx_times = summary.get("fx_times")
    reasons = list(summary.get("reason_codes", []))
    status = summary.get("result_status", "partial")
    if "result_status" not in summary:
        reasons.append("SNAPSHOT_SOURCE_QUALITY_UNKNOWN")
    if valuation_at is None:
        reasons.append("UNKNOWN_VALUATION_TIME")
        if when != date.today().isoformat():
            reasons.append("SNAPSHOT_DATE_MISMATCH")
    # Cash timestamps represent balance marks, holdings timestamps market marks.
    times = [valuation_at] + [item.get("priced_at") for item in components or []] + [
        item.get("priced_at") for item in (fx_times or {}).values()]
    if components is None or any(item.get("priced_at") is None for item in components):
        reasons.append("UNKNOWN_COMPONENT_PRICE_TIME")
    if fx_times is None or any(item.get("priced_at") is None for item in fx_times.values()):
        reasons.append("UNKNOWN_FX_PRICE_TIME")
    for index, stamp in enumerate(times):
        if stamp is None:
            continue
        try:
            mark_date = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().date().isoformat()
        except (ValueError, TypeError, AttributeError):
            reasons.append("INVALID_VALUATION_TIME")
            continue
        if (index == 0 and mark_date != when) or mark_date > when:
            reasons.append("SNAPSHOT_DATE_MISMATCH")
    bridge = summary.get("nav_bridge")
    bridge_reasons = (bridge or {}).get("reconciliation", {}).get("difference_reasons", [])
    reasons.extend(bridge_reasons)
    if reasons and status in {"complete", "empty"}:
        status = "partial"
    # kis_capture (scheduled job only) is stored when present and never added as null,
    # so tool-path payloads and summary/history output stay as before 2.13.0.
    return {**{key: summary.get(key) for key in daily_nav.FIELDS},
            **({'kis_capture': summary['kis_capture']} if 'kis_capture' in summary else {}),
            'quality_reason_details': daily_nav.quality_reason_details(summary, reasons),
            "valuation_at": valuation_at, "observed_at": observed_at,
            "methodology": {"basis_scope": "per_account"},
            "components": components, "fx_times": fx_times,
            "calculation_version": CALCULATION_VERSION, "nav_bridge": bridge,
            "settlement_adjusted_nav_krw": (bridge or {}).get("settlement_adjusted_nav_krw"),
            "result_status": status, "reason_codes": list(dict.fromkeys(reasons))}
