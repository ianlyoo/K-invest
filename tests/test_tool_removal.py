"""Approved catalog removal and surviving provider research, with fakes only."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import server
from tool_help import TOPICS


def test_catalog_contains_only_retained_capabilities():
    expected = json.loads((Path(__file__).parent / "fixtures/retained_tool_names.json").read_text())
    # The 12 merged tools (tools_v2) are added; every retained capability stays registered.
    merged = {"get_status", "get_holdings", "get_portfolio_summary_v2", "get_portfolio_returns", "get_transactions",
              "get_trading_pnl", "get_quote_v2", "get_market_info", "get_crypto_market", "get_crypto_positions",
              "get_crypto_account_history", "import_statement"}
    assert {tool.name for tool in asyncio.run(server.mcp.list_tools())} == set(expected) | merged


def test_stock_snapshot_preserves_quotes_consensus_and_insider_without_runner(monkeypatch):
    metrics = {"analyst_consensus": {"recommendation_key": "buy", "target_mean_price": 202.25}}
    monkeypatch.setattr(server, "compare_quotes", lambda *args, **kwargs: {"data": {"quotes": [{"provider": "toss"}]}})
    monkeypatch.setattr(server, "_get_yf", lambda: SimpleNamespace(get_key_metrics=lambda symbol: metrics))
    monkeypatch.setattr(server, "_get_sec", lambda: SimpleNamespace(get_insider_trades=lambda *args, **kwargs: {"summary": {"transaction_count": 2}}))
    result = server.get_stock_snapshot("AMZN", market="us")
    assert result["ok"]
    assert result["data"] == {
        "symbol": "AMZN", "quote_comparison": {"quotes": [{"provider": "toss"}]},
        "key_metrics": metrics, "insider_summary": {"transaction_count": 2},
        "data_warnings": ["Snapshot combines providers with different sessions and update cadences."],
    }
    assert server.get_key_metrics("AMZN")["data"] == metrics


def test_health_probes_only_surviving_providers(monkeypatch):
    monkeypatch.setattr(server, "_get_toss", lambda: SimpleNamespace(get_prices=lambda symbol: {"price": 202.25}))
    monkeypatch.setattr(server, "_get_kis", lambda: SimpleNamespace(get_overseas_quote=lambda *args, **kwargs: {"price": 202.25}))
    monkeypatch.setattr(server, "_get_sec", lambda: SimpleNamespace(get_cik=lambda symbol: "synthetic"))
    monkeypatch.setattr(server, "_get_yf", lambda: SimpleNamespace(get_key_metrics=lambda symbol: {"price": 202.25}))
    monkeypatch.setattr(server, "_get_binance", lambda: SimpleNamespace(price_ticker=lambda symbol: {"price": 202.25}))
    assert server.health_check()["data"] == {name: {"status": "ok"} for name in (
        "toss_market_data", "kis_market_data", "sec", "yfinance", "binance_futures")}


def test_removed_help_topic_falls_back_to_overview():
    assert "technical" not in TOPICS
    assert server.get_invest_mcp_help("technical")["data"] == server.get_invest_mcp_help("overview")["data"]
