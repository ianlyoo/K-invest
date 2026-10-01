"""Server tool layer: argument resolution and envelope contract for the new tools."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


# ── _resolve_dates ───────────────────────────────────────


def test_resolve_dates_explicit_range_passes_through():
    from server import _resolve_dates

    assert _resolve_dates("2026-06-01", "2026-06-30", 90) == ("2026-06-01", "2026-06-30")


def test_resolve_dates_defaults_end_to_today_and_start_backwards():
    from server import _resolve_dates

    start, end = _resolve_dates("", "", 30)
    assert end == datetime.now().strftime("%Y-%m-%d")
    assert (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days == 30


def test_resolve_dates_defaults_start_relative_to_given_end():
    from server import _resolve_dates

    assert _resolve_dates("", "2026-06-30", 30)[0] == "2026-05-31"


@pytest.mark.parametrize("start,end", [("20260601", "2026-06-30"), ("2026-06-01", "06/30/2026")])
def test_resolve_dates_rejects_non_iso_input(start, end):
    from server import _resolve_dates

    with pytest.raises(ValueError, match="ISO YYYY-MM-DD"):
        _resolve_dates(start, end, 30)


def test_resolve_dates_rejects_reversed_range():
    from server import _resolve_dates

    with pytest.raises(ValueError, match="is after"):
        _resolve_dates("2026-06-30", "2026-06-01", 30)


# ── _resolve_accounts ────────────────────────────────────


def test_resolve_accounts_all_means_no_filter():
    from server import _resolve_accounts

    assert _resolve_accounts("all") is None
    assert _resolve_accounts("") is None


def test_resolve_accounts_provider_and_label_forms(monkeypatch):
    import server

    monkeypatch.setattr(server, "_get_toss_registry", lambda: _FakeRegistry(["primary", "secondary"]))
    assert server._resolve_accounts("kis") == ["kis:domestic", "kis:overseas"]
    assert server._resolve_accounts("kis:overseas") == ["kis:overseas"]
    assert server._resolve_accounts("toss") == ["toss:primary", "toss:secondary"]
    assert server._resolve_accounts("toss:primary") == ["toss:primary"]
    # A bare label is interpreted as a Toss account, matching get_toss_holdings.
    assert server._resolve_accounts("secondary") == ["toss:secondary"]


def test_resolve_accounts_covers_every_configured_kis_account(monkeypatch):
    import server

    monkeypatch.setattr(server, "_get_kis_registry", lambda: _FakeKISRegistry())

    assert server._resolve_accounts("kis") == ["kis:domestic", "kis:overseas", "kis:pension:domestic"]
    # A pension account label expands to its scopes; overseas is not one of them.
    assert server._resolve_accounts("kis:pension") == ["kis:pension:domestic"]
    assert server._resolve_accounts("kis:pension:domestic") == ["kis:pension:domestic"]


def test_resolve_accounts_falls_back_when_kis_is_unconfigured(monkeypatch):
    import server
    from kinvest_common import NotConfiguredError

    monkeypatch.setattr(
        server, "_get_kis_registry", lambda: (_ for _ in ()).throw(NotConfiguredError("kis", "no creds"))
    )

    assert server._resolve_accounts("kis") == ["kis:domestic", "kis:overseas"]


class _FakeRegistry:
    def __init__(self, labels):
        self._labels = labels

    def account_labels(self):
        return self._labels


class _FakeKISRegistry:
    """A 종합계좌 plus a 연금저축 account, as KISAccountRegistry would report them."""

    def accounts(self):
        return [
            SimpleNamespace(label="primary", supports_overseas=True),
            SimpleNamespace(label="pension", supports_overseas=False),
        ]


# ── Envelope contract ────────────────────────────────────


def test_unconfigured_providers_yield_ok_envelope_with_health(monkeypatch):
    """Every provider failing is a degraded result, not a tool error."""
    import server
    from kinvest_common import NotConfiguredError

    monkeypatch.setattr(server, "_collector", None)
    monkeypatch.setattr(
        server, "_get_toss_registry", lambda: (_ for _ in ()).throw(NotConfiguredError("toss", "no creds"))
    )
    monkeypatch.setattr(
        server, "_get_kis_registry", lambda: (_ for _ in ()).throw(NotConfiguredError("kis", "no creds"))
    )

    result = server.get_consolidated_holdings()
    assert result["ok"] is True
    assert result["data"]["position_count"] == 0
    # An unresolvable registry is reported once per provider, before any account.
    assert result["data"]["health"]["toss"]["status"] == "not_configured"
    assert result["data"]["health"]["kis"]["status"] == "not_configured"
    assert result["error"] is None
    assert result["meta"]["provider"] == "composite"


def test_get_all_transactions_rejects_bad_dates_with_error_envelope(monkeypatch):
    import server

    monkeypatch.setattr(server, "_collector", None)
    result = server.get_all_transactions(start_date="2026/01/01")
    assert result["ok"] is False
    assert result["error"]["code"] == "VALUEERROR"
    assert "ISO YYYY-MM-DD" in result["error"]["message"]


def test_account_health_reports_down_when_nothing_is_configured(monkeypatch):
    import server
    from kinvest_common import NotConfiguredError

    monkeypatch.setattr(server, "_collector", None)
    monkeypatch.setattr(
        server, "_get_toss_registry", lambda: (_ for _ in ()).throw(NotConfiguredError("toss", "no creds"))
    )
    monkeypatch.setattr(server, "_get_kis", lambda: (_ for _ in ()).throw(NotConfiguredError("kis", "no creds")))

    result = server.get_account_health()
    assert result["ok"] is True
    assert result["data"]["overall_status"] == "down"
    assert result["data"]["sources_failed"]


def test_snapshot_refuses_when_a_source_failed(monkeypatch, tmp_path):
    """A partial portfolio must never be written into the NAV history."""
    import server
    from kinvest_common import NotConfiguredError
    from portfolio_store import NavStore

    monkeypatch.setattr(server, "_collector", None)
    monkeypatch.setattr(server, "_nav_store", NavStore(tmp_path / "p.db"))
    monkeypatch.setattr(
        server, "_get_toss_registry", lambda: (_ for _ in ()).throw(NotConfiguredError("toss", "no creds"))
    )
    monkeypatch.setattr(server, "_get_kis", lambda: (_ for _ in ()).throw(NotConfiguredError("kis", "no creds")))

    result = server.snapshot_portfolio_nav()
    assert result["ok"] is False
    assert result["error"]["code"] == "PORTFOLIO_SOURCES_DEGRADED"
    assert result["error"]["retryable"] is True
    assert NavStore(tmp_path / "p.db").count() == 0


def test_portfolio_history_explains_empty_store(monkeypatch, tmp_path):
    import server
    from portfolio_store import NavStore

    monkeypatch.setattr(server, "_nav_store", NavStore(tmp_path / "p.db"))
    monkeypatch.setattr(server, "_risk_free_decimal", lambda: 0.0)

    result = server.get_portfolio_history(period="3M")
    assert result["ok"] is True
    assert result["data"]["snapshot_count"] == 0
    assert "snapshot_portfolio_nav" in result["data"]["note"]


def test_portfolio_history_returns_stored_series(monkeypatch, tmp_path):
    import server
    from portfolio_store import NavStore

    store = NavStore(tmp_path / "p.db")
    today = datetime.now().strftime("%Y-%m-%d")
    store.upsert_snapshot({"total_assets_krw": 1_000_000.0, "position_count": 1}, snapshot_date=today)
    monkeypatch.setattr(server, "_nav_store", store)
    monkeypatch.setattr(server, "_risk_free_decimal", lambda: 0.0)

    result = server.get_portfolio_history(period="MTD", detail_level="full")
    assert result["data"]["snapshot_count"] == 1
    assert result["data"]["latest"]["total_value_krw"] == 1_000_000.0
    assert result["data"]["stored_range"]["total"] == 1


def test_methodology_is_attached_to_portfolio_outputs(monkeypatch):
    import server
    from kinvest_common import NotConfiguredError

    monkeypatch.setattr(server, "_collector", None)
    monkeypatch.setattr(
        server, "_get_toss_registry", lambda: (_ for _ in ()).throw(NotConfiguredError("toss", "no creds"))
    )
    monkeypatch.setattr(server, "_get_kis", lambda: (_ for _ in ()).throw(NotConfiguredError("kis", "no creds")))

    data = server.get_consolidated_holdings()["data"]
    assert data["methodology"]["base_currency"] == "KRW"
    assert "moving average" in data["methodology"]["cost_basis"]


def test_new_tools_are_registered_with_the_mcp_server():
    import asyncio

    import server

    names = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    assert {
        "get_all_transactions",
        "get_consolidated_holdings",
        "get_portfolio_summary",
        "get_realized_pnl",
        "get_portfolio_performance",
        "get_portfolio_history",
        "snapshot_portfolio_nav",
        "get_account_health",
    } <= names
