"""Clear public-data names and additive compatibility metadata, with fake clients."""
import asyncio
from types import SimpleNamespace

import pytest

import server
from tool_help import TOPICS

PAIRS = (
    ("get_recent_trades", "get_market_trades", {"symbol": "AMZN"}),
    ("get_binance_funding_rate", "get_binance_funding_rate_history", {"symbol": "BTCUSDT", "limit": 3}),
)


@pytest.mark.parametrize("old,new,arguments", PAIRS)
@pytest.mark.parametrize("fails", [False, True])
def test_public_aliases_share_response_and_errors(monkeypatch, old, new, arguments, fails):
    calls = []

    def fake(symbol, **kwargs):
        calls.append((symbol, kwargs))
        if fails:
            raise ValueError("Synthetic invalid input")
        return {"symbol": symbol, "items": [{"price": 202.25}], **kwargs}

    monkeypatch.setattr(server, "_get_toss", lambda: SimpleNamespace(get_recent_trades=fake))
    monkeypatch.setattr(server, "_get_binance", lambda: SimpleNamespace(funding_rates=fake))
    canonical = getattr(server, new)(**arguments)
    alias = getattr(server, old)(**arguments)
    assert "deprecated" not in canonical["meta"]
    assert alias["meta"].pop("deprecated") == {"alias_of": new, "since": "2.6.0"}
    assert alias == canonical
    assert calls == [(arguments["symbol"], {key: value for key, value in arguments.items() if key != "symbol"})] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("old,new,arguments", PAIRS)
async def test_alias_catalog_and_dispatch_preserve_signatures(monkeypatch, old, new, arguments):
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    assert tools[old].description.startswith(f"Deprecated alias of {new}; kept for compatibility.")
    # FastMCP generates a tool-specific schema title; parameter contracts match.
    assert {key: value for key, value in tools[old].inputSchema.items() if key != "title"} == {
        key: value for key, value in tools[new].inputSchema.items() if key != "title"
    }
    for name in (old, new):
        assert tools[name].annotations.readOnlyHint is True
    monkeypatch.setattr(server, "_get_toss", lambda: SimpleNamespace(get_recent_trades=lambda symbol: {"symbol": symbol}))
    monkeypatch.setattr(server, "_get_binance", lambda: SimpleNamespace(funding_rates=lambda symbol, limit=10: {"symbol": symbol, "limit": limit}))
    _, canonical = await server.mcp.call_tool(new, {"symbol": arguments["symbol"]})
    _, alias = await server.mcp.call_tool(old, {"symbol": arguments["symbol"]})
    assert alias["meta"].pop("deprecated") == {"alias_of": new, "since": "2.6.0"}
    assert alias == canonical
    if new.startswith("get_binance"):
        assert canonical["data"]["limit"] == 10


def test_help_groups_by_purpose_without_gating_or_duplicate_capabilities():
    assert "futures" in TOPICS
    data = server.get_invest_mcp_help("all")["data"]
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    assert {claim["name"] for claim in data["tool_contracts"]} == set(tools)
    for heading in ("Portfolio", "Quotes / market data", "Company research (financials)", "Crypto futures (public)", "Diagnostics", "Deprecated aliases"):
        assert heading in data["markdown"]
    for old, new, _ in PAIRS:
        assert f"## `{old}(" not in data["markdown"]
        assert f"## `{new}(" in data["markdown"]
        assert f"`{old}`" in data["markdown"]
    assert "get_market_trades" in server.get_invest_mcp_help("quotes")["data"]["markdown"]
    assert "get_binance_funding_rate_history" in server.get_invest_mcp_help("futures")["data"]["markdown"]
    assert "get_binance_funding_rate_history" not in server.get_invest_mcp_help("quotes")["data"]["markdown"]
