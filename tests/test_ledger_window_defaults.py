"""Requested-window completeness must not imply that older history is absent."""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

import server
from journal_metadata import source_metadata
from portfolio_export import DatasetStore
from portfolio_model import FxRates, Transaction, TxType
from portfolio_sources import Collection

HINT = "Only the default window was queried; pass start_date (e.g. 2024-01-01) for older history."
COVERAGE = "requested window only; earlier history may exist outside it"


@pytest.fixture
def history(monkeypatch, tmp_path):
    calls = []
    state = {"truncated": False}
    old = Transaction("toss", "toss:primary", "older", TxType.BUY, "2024-10-10", "USD",
                      symbol="TEST", quantity=1, price=100)
    recent = Transaction("toss", "toss:primary", "recent", TxType.BUY,
                         date.today().isoformat(), "USD", symbol="TEST", quantity=1, price=120)

    def transactions(start, end, *args, **kwargs):
        calls.append((start, end))
        source = "toss:primary:orders"
        rows = [t for t in (old, recent) if start <= t.trade_date <= end]
        truncated = state["truncated"]
        return Collection(transactions=rows,
                          health={source: {"status": "truncated" if truncated else "ok"}},
                          truncated_sources=[source] if truncated else [],
                          source_metadata={source: source_metadata(source, len(rows), complete=not truncated,
                                                                    truncated=truncated, start=start, end=end)},
                          requested_window={"start": start, "end": end},
                          observed_window={"start": min((t.trade_date for t in rows), default=None),
                                           "end": max((t.trade_date for t in rows), default=None)},
                          fetch_complete=not truncated)

    collector = SimpleNamespace(collect_transactions=transactions,
                                collect_positions=lambda *args: Collection(),
                                build_fx=lambda *args: FxRates({"USD": 1300}))
    monkeypatch.setattr(server, "_get_collector", lambda: collector)
    monkeypatch.setattr(server, "_resolve_accounts", lambda _: None)
    monkeypatch.setattr(server, "_get_kis_registry", lambda: SimpleNamespace(default_label=lambda: "primary"))
    store = DatasetStore(tmp_path / "datasets.db")
    monkeypatch.setattr(server, "_get_dataset_store", lambda: store)
    return calls, state


@pytest.mark.parametrize("tool,days", [("get_all_transactions", 90), ("get_toss_trade_history", 90),
                                      ("get_kis_trade_history", 30)])
@pytest.mark.parametrize("truncated", [False, True])
def test_default_recovery_window_is_labeled_without_changing_completeness(history, tool, days, truncated):
    calls, state = history
    state["truncated"] = truncated
    kwargs = {"reconcile": False} if tool == "get_all_transactions" else {"recovery": True}
    result = getattr(server, tool)(**kwargs)
    assert result["ok"]
    data = result["data"]
    assert data["window_defaulted"] is True
    assert data["window_reason_codes"] == [f"DEFAULT_WINDOW_{days}D"]
    assert data["window_hint"] == HINT
    assert calls[-1][0] == (date.today() - timedelta(days=days)).isoformat()
    assert data["requested_window"] == {"start": calls[-1][0], "end": calls[-1][1]}
    assert data["complete"] is (not truncated)
    assert data["fetch_complete"] is (not truncated)
    assert bool(data["truncated_sources"]) is truncated
    assert data["historical_coverage"] == "unknown"
    metadata = next(iter(data["source_metadata"].values()))
    assert metadata["coverage_scope"] == COVERAGE
    assert metadata["complete"] is (not truncated)
    assert metadata["truncated"] is truncated


@pytest.mark.parametrize("tool", ["get_all_transactions", "get_toss_trade_history", "get_kis_trade_history"])
def test_explicit_older_start_returns_older_history_without_default_warning(history, tool):
    kwargs = {"reconcile": False} if tool == "get_all_transactions" else {"recovery": True}
    start = "20241001" if tool == "get_kis_trade_history" else "2024-10-01"
    data = getattr(server, tool)(start_date=start, **kwargs)["data"]
    assert data["window_defaulted"] is False
    assert data["window_reason_codes"] == []
    assert "window_hint" not in data
    assert data["transactions"][0]["trade_date"] == "2024-10-10"
    assert data["complete"] and data["fetch_complete"]
    assert data["truncated_sources"] == []
    assert data["historical_coverage"] == "unknown"


def test_end_only_call_defaults_relative_to_the_end(history):
    data = server.get_all_transactions(end_date="2026-07-01", reconcile=False)["data"]
    assert data["period"] == {"start": "2026-04-02", "end": "2026-07-01"}
    assert data["window_defaulted"] is True
    assert data["window_reason_codes"] == ["DEFAULT_WINDOW_90D"]


@pytest.mark.parametrize("kwargs,reason", [({}, "DEFAULT_WINDOW_YTD"),
                                           ({"end_date": "2026-07-01"}, "DEFAULT_WINDOW_365D")])
def test_transaction_exports_freeze_default_window_flags_on_every_page(history, kwargs, reason):
    data = server.export_portfolio_dataset("transactions", page_size=1, **kwargs)["data"]
    dataset_id = data["dataset_id"]
    manifest = data["manifest"]
    assert data["window_defaulted"] is True
    assert data["window_reason_codes"] == [reason]
    assert data["window_hint"] == HINT
    assert manifest["ledger_window"]["start"] == manifest["reporting_window"]["start"]
    calls_before = len(history[0])
    while data["next_cursor"]:
        data = server.export_portfolio_dataset("transactions", dataset_id=dataset_id,
                                               cursor=data["next_cursor"], page_size=1)["data"]
        assert data["window_defaulted"] is True
        assert data["window_reason_codes"] == [reason]
        assert data["window_hint"] == HINT
    assert len(history[0]) == calls_before


@pytest.mark.parametrize("kwargs", [{"start_date": "2024-10-01"}, {"period": "ALL"}])
def test_explicit_or_all_transaction_exports_have_no_default_warning(history, kwargs):
    data = server.export_portfolio_dataset("transactions", **kwargs)["data"]
    assert data["window_defaulted"] is False
    assert data["window_reason_codes"] == []
    assert "window_hint" not in data
    assert any(row["data"].get("trade_date") == "2024-10-10" for row in data["rows"])


def test_realized_and_trade_review_queries_still_fetch_epoch_history(history):
    for tool in (server.get_realized_pnl, server.get_trade_review):
        result = tool()
        assert result["ok"]
        assert history[0][-1][0] == "2000-01-01"
        assert "window_defaulted" not in result["data"]
    result = server.export_portfolio_dataset("realized_pnl")
    assert result["ok"]
    assert history[0][-1][0] == "2000-01-01"
    assert "window_defaulted" not in result["data"]
