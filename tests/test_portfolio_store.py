"""NAV snapshot store: idempotent daily upsert, range queries, bad-data refusal."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_store import NavStore, default_db_path


def summary(total: float, **overrides) -> dict:
    base = {
        "total_assets_krw": total,
        "stock_value_krw": total * 0.9,
        "cash_krw": total * 0.1,
        "cost_basis_krw": total * 0.8,
        "unrealized_pnl_krw": total * 0.1,
        "position_count": 3,
        "by_currency": {"KRW": {"weight_pct": 100.0}},
        "by_market": {"KR": {"weight_pct": 100.0}},
        "by_account": {"kis:domestic": {"total_krw": total}},
        "cash_by_currency": {"KRW": total * 0.1},
    }
    base.update(overrides)
    return base


@pytest.fixture
def store(tmp_path) -> NavStore:
    return NavStore(tmp_path / "portfolio.db")


def test_upsert_then_read_back(store):
    stored = store.upsert_snapshot(summary(1_000_000), snapshot_date="2026-08-18", fx_usdkrw=1300.0)
    assert stored["snapshot_date"] == "2026-08-18"
    assert stored["total_value_krw"] == 1_000_000
    assert stored["fx_usdkrw"] == 1300.0
    assert stored["breakdown"]["by_account"]["kis:domestic"]["total_krw"] == 1_000_000
    assert store.count() == 1


def test_second_upsert_same_day_replaces_rather_than_appends(store):
    store.upsert_snapshot(summary(1_000_000), snapshot_date="2026-08-18")
    store.upsert_snapshot(summary(1_250_000), snapshot_date="2026-08-18")
    assert store.count() == 1
    assert store.get_snapshot("2026-08-18")["total_value_krw"] == 1_250_000


def test_created_at_is_preserved_across_updates(store):
    first = store.upsert_snapshot(summary(1_000_000), snapshot_date="2026-08-18")
    updated = store.upsert_snapshot(summary(2_000_000), snapshot_date="2026-08-18")
    assert updated["created_at"] == first["created_at"]
    assert updated["updated_at"] >= first["updated_at"]


def test_range_is_inclusive_and_ordered_oldest_first(store):
    for day, total in (("2026-08-15", 100.0), ("2026-08-16", 110.0), ("2026-08-17", 105.0)):
        store.upsert_snapshot(summary(total), snapshot_date=day)
    rows = store.range("2026-08-15", "2026-08-16")
    assert [row["snapshot_date"] for row in rows] == ["2026-08-15", "2026-08-16"]
    assert store.range("2026-01-01", "2026-12-31")[-1]["snapshot_date"] == "2026-08-17"
    assert store.range("2027-01-01", "2027-12-31") == []


def test_bounds_and_count(store):
    assert store.bounds() == (None, None)
    store.upsert_snapshot(summary(1.0), snapshot_date="2026-08-01")
    store.upsert_snapshot(summary(2.0), snapshot_date="2026-08-18")
    assert store.bounds() == ("2026-08-01", "2026-08-18")
    assert store.count() == 2


def test_refuses_to_store_a_summary_with_no_total(store):
    """A total-provider outage must not record a 0 NAV and corrupt drawdowns."""
    with pytest.raises(ValueError, match="total_assets_krw"):
        store.upsert_snapshot(summary(0.0, total_assets_krw=None))
    assert store.count() == 0


def test_zero_total_is_allowed_when_explicit(store):
    stored = store.upsert_snapshot(summary(0.0), snapshot_date="2026-08-18")
    assert stored["total_value_krw"] == 0.0


def test_missing_snapshot_returns_none(store):
    assert store.get_snapshot("2026-01-01") is None


def test_db_is_created_lazily_with_parent_directories(tmp_path):
    path = tmp_path / "nested" / "deeper" / "portfolio.db"
    store = NavStore(path)
    assert not path.exists()
    store.upsert_snapshot(summary(1.0), snapshot_date="2026-08-18")
    assert path.exists()


def test_default_db_path_honors_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("KINVEST_PORTFOLIO_DB", str(tmp_path / "custom.db"))
    assert default_db_path() == tmp_path / "custom.db"
    monkeypatch.delenv("KINVEST_PORTFOLIO_DB")
    assert default_db_path().name == "portfolio.db"


def test_range_respects_limit(store):
    for day in range(1, 6):
        store.upsert_snapshot(summary(float(day)), snapshot_date=f"2026-08-0{day}")
    assert len(store.range("2026-08-01", "2026-08-31", limit=3)) == 3
