"""health: idle is not an outage; account_count counts broker accounts (synthetic)."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from health_status import HealthStatus  # noqa: E402
from portfolio_sources import Collection  # noqa: E402


def _age(h, source, seconds=1000):
    h.observations[source]["observed_epoch"] -= seconds


def _row(report, source):
    return next(s for s in report["sources"] if s["source"] == source)


def test_sources_idle_after_success_are_labelled_idle_not_outage():
    h = HealthStatus()
    h.record("toss:primary", status="ok", count=3, complete=True)
    h.record("kis:domestic", status="ok_empty", count=0, complete=True)
    for source in list(h.observations):
        _age(h, source)
    report = h.report({"toss": True, "kis": True})
    row = _row(report, "toss:primary")
    assert row["status"] == "stale" and row["stale_reason"] == "idle" and row["last_observed_status"] == "ok"
    assert report["overall_status"] == "stale"
    assert report["stale_reason"] == "idle"
    assert report["stale_breakdown"] == {"idle": 4, "last_attempt_failed": 0}
    assert report["stale_after_seconds"] == 300


def test_aged_failure_is_reported_as_the_failure():
    h = HealthStatus()
    h.record("toss:primary", status="ok", count=3, complete=True)
    h.record("kis:domestic", status="provider_unavailable", error_code="provider_unavailable")
    for source in list(h.observations):
        _age(h, source)
    report = h.report({"toss": True, "kis": True})
    row = _row(report, "kis:domestic")
    assert row["stale_reason"] == "last_attempt_failed" and row["last_observed_status"] == "provider_unavailable"
    assert report["stale_reason"] == "last_attempt_failed"
    assert report["overall_status"] == "partial"


def test_idle_sources_do_not_make_a_fresh_healthy_service_partial():
    h = HealthStatus()
    h.record("toss:primary", status="ok", count=3, complete=True)
    h.record("kis:domestic", status="ok", count=2, complete=True)
    _age(h, "toss:primary")
    _age(h, "toss")
    assert h.report({"toss": True, "kis": True})["overall_status"] == "ok"
    h.record("kis:overseas", status="error", error_code="KIS_API_ERROR")
    assert h.report({"toss": True, "kis": True})["overall_status"] == "partial"


def test_fresh_report_has_no_stale_reason():
    h = HealthStatus()
    h.record("toss:primary", status="ok", count=1, complete=True)
    report = h.report({"toss": True})
    assert report["stale_reason"] is None and report["stale_breakdown"] == {"idle": 0, "last_attempt_failed": 0}
    assert "stale_reason" not in _row(report, "toss:primary")


class _Collector:
    def collect_positions(self):
        sources = ["kis:domestic", "kis:overseas", "kis:overseas:NASD", "kis:pension:domestic",
                   "toss:primary", "toss:primary:krw", "toss:primary:usd",
                   "toss:secondary", "toss:secondary:krw", "toss:secondary:usd"]
        return Collection(health={s: {"account": s, "status": "ok", "last_error": None} for s in sources},
                          fetched_at="2026-10-21T15:00:00+00:00")


class _NavStore:
    def bounds(self):
        return ("2026-01-02", "2026-10-21")


def test_account_count_is_distinct_broker_accounts(monkeypatch):
    import server

    monkeypatch.setattr(server, "_get_collector", lambda: _Collector())
    monkeypatch.setattr(server, "_get_nav_store", lambda: _NavStore())
    data = server.get_account_health()["data"]
    assert data["account_count"] == 4
    assert data["source_count"] == 10
    assert data["broker_accounts"] == ["kis:pension", "kis:primary", "toss:primary", "toss:secondary"]
    assert len(data["accounts"]) == 10


def test_provider_level_failure_is_not_an_account():
    import server

    assert server._broker_account_of_source("toss") is None
    assert server._broker_account_of_source("kis") is None
    assert server._broker_account_of_source("kis:isa:overseas:TKSE") == "kis:isa"
