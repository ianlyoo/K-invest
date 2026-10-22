#!/usr/bin/env python3
"""Daily portfolio NAV snapshot store (SQLite).

One row per calendar date, upserted — calling twice in a day refreshes that day's
snapshot rather than appending. This is the only piece of K-invest that persists
state; it holds derived valuations only, never credentials or raw broker payloads.

Location: `$KINVEST_PORTFOLIO_DB`, else `portfolio.db` under `cache_dir()`
(`KINVEST_CACHE_DIR`). Under systemd that path must be in `ReadWritePaths=`.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

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

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        return conn

    def upsert_snapshot(
        self,
        summary: dict[str, Any],
        snapshot_date: str | None = None,
        fx_usdkrw: float | None = None,
    ) -> dict[str, Any]:
        """Write (or refresh) one day's snapshot from a portfolio summary.

        Returns the stored row. A summary with no total (every provider failed) is
        rejected so a broker outage cannot record a 0 NAV and corrupt the history.
        """
        total = summary.get("total_assets_krw")
        if total is None:
            raise ValueError("cannot snapshot a portfolio summary without total_assets_krw")
        when = snapshot_date or date.today().isoformat()
        now = _now()
        breakdown = json.dumps(
            {
                "by_currency": summary.get("by_currency"),
                "by_market": summary.get("by_market"),
                "by_account": summary.get("by_account"),
                "cash_by_currency": summary.get("cash_by_currency"),
            },
            ensure_ascii=False,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO nav_snapshots (
                    snapshot_date, total_value_krw, stock_value_krw, cash_value_krw,
                    cost_basis_krw, unrealized_pnl_krw, position_count, fx_usdkrw,
                    breakdown_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_date) DO UPDATE SET
                    total_value_krw    = excluded.total_value_krw,
                    stock_value_krw    = excluded.stock_value_krw,
                    cash_value_krw     = excluded.cash_value_krw,
                    cost_basis_krw     = excluded.cost_basis_krw,
                    unrealized_pnl_krw = excluded.unrealized_pnl_krw,
                    position_count     = excluded.position_count,
                    fx_usdkrw          = excluded.fx_usdkrw,
                    breakdown_json     = excluded.breakdown_json,
                    updated_at         = excluded.updated_at
                """,
                (
                    when,
                    float(total),
                    summary.get("stock_value_krw"),
                    summary.get("cash_krw"),
                    summary.get("cost_basis_krw"),
                    summary.get("unrealized_pnl_krw"),
                    summary.get("position_count"),
                    fx_usdkrw,
                    breakdown,
                    now,
                    now,
                ),
            )
        stored = self.get_snapshot(when)
        return stored or {"snapshot_date": when, "total_value_krw": float(total)}

    def get_snapshot(self, snapshot_date: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM nav_snapshots WHERE snapshot_date = ?", (snapshot_date,)
            ).fetchone()
        return _row_to_dict(row) if row else None

    def range(self, start: str, end: str, limit: int = 3650) -> list[dict[str, Any]]:
        """Snapshots between two ISO dates, inclusive, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM nav_snapshots
                WHERE snapshot_date >= ? AND snapshot_date <= ?
                ORDER BY snapshot_date ASC LIMIT ?
                """,
                (start, end, limit),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM nav_snapshots").fetchone()[0])

    def bounds(self) -> tuple[str | None, str | None]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MIN(snapshot_date), MAX(snapshot_date) FROM nav_snapshots"
            ).fetchone()
        return (row[0], row[1]) if row else (None, None)


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
