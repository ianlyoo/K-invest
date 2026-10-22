"""Catalog compatibility and passive dispatch checks, without upstream IO."""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_service_health_catalog_uses_plain_string_and_read_only_hint():
    import server

    tools = await server.mcp.list_tools()
    names = [tool.name for tool in tools]
    assert len(names) == len(set(names)) == 68  # 56 + 12 merged tools (tools_v2)
    health = next(tool for tool in tools if tool.name == "get_service_health")
    quote = next(tool for tool in tools if tool.name == "get_quote")
    schema = health.inputSchema
    assert schema["properties"]["mode"] == {
        "type": "string", "title": "Mode", "default": "passive",
    }
    assert "mode" not in schema.get("required", [])
    assert health.annotations is not None and health.annotations.readOnlyHint is True
    assert health.outputSchema is not None and quote.outputSchema is not None
    assert {k: v for k, v in health.outputSchema.items() if k != "title"} == {
        k: v for k, v in quote.outputSchema.items() if k != "title"
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments,expected_ok", [({}, True), ({"mode": "passive"}, True), ({"mode": "active"}, False)])
async def test_service_health_dispatch_keeps_passive_default_and_validation(monkeypatch, arguments, expected_ok):
    import server
    from health_status import HealthStatus

    def forbidden(*args, **kwargs):
        pytest.fail("Service health must not construct upstream clients")

    for factory in ("_get_toss", "_get_kis", "_get_toss_registry", "_get_kis_registry", "_get_collector", "_get_nav_store"):
        monkeypatch.setattr(server, factory, forbidden)
    monkeypatch.setattr(server, "_health_status", HealthStatus())
    monkeypatch.setattr(server, "_oauth_provider", None)
    _, result = await server.mcp.call_tool("get_service_health", arguments)
    assert result["ok"] is expected_ok
    if expected_ok:
        assert result["meta"]["mode"] == "passive"
    else:
        assert result["error"]["code"] == "INVALID_MODE"
