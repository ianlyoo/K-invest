"""Who lists and calls which names on the served app (chat / operator / service)."""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.lowlevel.server import request_ctx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_merge_equality as fakes  # noqa: E402

import response_shape  # noqa: E402
import server  # noqa: E402
import tools_v2  # noqa: E402
from portfolio_store import NavStore  # noqa: E402
from service_auth import (  # noqa: E402
    DASHBOARD_READ_TOOLS,
    SERVICE_READ_TOOLS,
    DashboardAccessToken,
    ServiceAccessToken,
)


@pytest.fixture
def stub(monkeypatch, tmp_path):
    return fakes.install_stub(monkeypatch, tmp_path)


CHAT_NAMES = [public for public, _ in tools_v2.CHAT_TOOLS]
NEW_TOOLS = {"get_status", "get_holdings", "get_portfolio_summary_v2", "get_portfolio_returns", "get_transactions",
             "get_trading_pnl", "get_quote_v2", "get_market_info", "get_crypto_market", "get_crypto_positions",
             "get_crypto_account_history", "import_statement"}


def _token(kind=AccessToken, client_id="dcr_synthetic"):
    return kind(token="synthetic", client_id=client_id, scopes=[])


TOKENS = {
    "chat": _token(),
    "chat_manual": _token(client_id="manual-oauth-client"),
    "operator": _token(client_id="static-token-client"),
    "acceptance": _token(client_id="k-invest-client"),
    "service": _token(ServiceAccessToken, "loopback-service"),
    "dashboard": _token(DashboardAccessToken, "dashboard-service"),
}


def _run(principal, coroutine_fn):
    async def inner():
        if principal is None:
            return await coroutine_fn()
        request = SimpleNamespace(scope={"user": AuthenticatedUser(TOKENS[principal])})
        token = request_ctx.set(SimpleNamespace(request=request, request_id="t", meta=None, session=None,
                                                lifespan_context=None, experimental=None))
        try:
            return await coroutine_fn()
        finally:
            request_ctx.reset(token)
    return asyncio.run(inner())


def listed(principal):
    return [tool.name for tool in _run(principal, server.mcp.list_tools)]


def called(principal, name, arguments=None):
    result = _run(principal, lambda: server.mcp.call_tool(name, arguments or {}))
    content, structured = server._result_parts(result)
    assert content[0].text == response_shape.compact_json(structured)
    return structured


def registered():
    return [tool.name for tool in server.mcp._tool_manager.list_tools()]


# ── lists ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("principal", ["chat", "chat_manual"])
def test_chat_clients_list_exactly_the_merged_catalog(principal):
    assert listed(principal) == CHAT_NAMES
    assert len(CHAT_NAMES) == 21 and len(set(CHAT_NAMES)) == 21


@pytest.mark.parametrize("principal", ["operator", "acceptance"])
def test_operator_lists_the_registry_without_the_deprecated_aliases(principal):
    """The aliases leave every client list; no service allowlist names them."""
    names = listed(principal)
    assert names == [name for name in registered() if name not in tools_v2.HIDDEN] and len(names) == 66
    for client in ("chat", "chat_manual", "service", "dashboard"):
        assert not set(listed(client)) & tools_v2.HIDDEN
    assert not (set(SERVICE_READ_TOOLS) | set(DASHBOARD_READ_TOOLS)) & tools_v2.HIDDEN


def test_in_process_registry_list_is_unfiltered():
    assert listed(None) == registered() and len(registered()) == 68


@pytest.mark.parametrize("principal,allowed", [("service", SERVICE_READ_TOOLS), ("dashboard", DASHBOARD_READ_TOOLS)])
def test_service_principals_keep_their_exact_allowlists(principal, allowed):
    assert set(listed(principal)) == set(allowed)
    assert set(allowed) <= set(registered())


def test_chat_list_keeps_each_tool_schema_under_the_public_name():
    tools = {tool.name: tool for tool in _run("chat", server.mcp.list_tools)}
    internal = {tool.name: tool for tool in server.mcp._tool_manager.list_tools()}
    for public, name in tools_v2.CHAT_TOOLS:
        assert tools[public].inputSchema == internal[name].parameters
        assert tools[public].description == internal[name].description


def test_every_registered_name_has_a_chat_route():
    chat_internal = {name for _, name in tools_v2.CHAT_TOOLS}
    for name in registered():
        assert name in chat_internal or name in tools_v2.REPLACED_BY or name in tools_v2.PUBLIC_TO_INTERNAL, name
    assert {name for name in tools_v2.REPLACED_BY.values() if "." not in name} <= set(CHAT_NAMES)


# ── annotations, schemas, descriptions ───────────────────────────────


def test_new_read_tools_are_read_only_and_import_is_the_only_new_write_tool():
    tools = {tool.name: tool for tool in server.mcp._tool_manager.list_tools()}
    for name in NEW_TOOLS - {"import_statement"}:
        assert tools[name].annotations.readOnlyHint is True and tools[name].annotations.destructiveHint is False, name
    assert tools["import_statement"].annotations.readOnlyHint is False
    summary = tools["get_portfolio_summary"]
    assert summary.annotations.readOnlyHint is True
    assert set(summary.parameters["properties"]) == {"include_realized", "detail_level"}


def test_renamed_tools_share_the_generic_output_schema():
    # FastMCP validates output against a schema cached by name for every principal;
    # the public and internal names must validate alike (they differ only in title).
    tools = {tool.name: tool for tool in server.mcp._tool_manager.list_tools()}
    untitled = lambda schema: {k: v for k, v in schema.items() if k != "title"}  # noqa: E731
    for public, internal in tools_v2.PUBLIC_TO_INTERNAL.items():
        assert untitled(tools[public].output_schema) == untitled(tools[internal].output_schema)
        assert tools[public].output_schema["additionalProperties"] is True


def test_new_descriptions_name_no_other_tool_of_ours():
    names = set(registered()) | set(CHAT_NAMES)
    tools = {tool.name: tool for tool in server.mcp._tool_manager.list_tools()}
    for name in NEW_TOOLS:
        words = set(re.findall(r"[a-z_]+", tools[name].description))
        assert not (words & names) - {name}, name


def test_new_tools_need_at_most_two_arguments():
    tools = {tool.name: tool for tool in server.mcp._tool_manager.list_tools()}
    for name in NEW_TOOLS:
        assert len(tools[name].parameters.get("required", [])) <= 2, name


# ── calls ────────────────────────────────────────────────────────────


def test_chat_call_of_an_old_name_runs_it_and_marks_it_deprecated():
    old = called("operator", "get_invest_mcp_help", {"topic": "quotes"})
    chat = called("chat", "get_invest_mcp_help", {"topic": "quotes"})
    assert "deprecated" not in old["meta"]
    assert chat["meta"]["deprecated"] == {"use": "get_status", "removal": "next minor"}
    assert {**chat, "meta": {k: v for k, v in chat["meta"].items() if k != "deprecated"}} == old


def test_hidden_aliases_stay_callable(stub):
    for principal in ("chat", "operator"):
        result = called(principal, "get_recent_trades", {"symbol": "SYNA"})
        assert result["ok"] is True
    assert called("chat", "get_recent_trades", {"symbol": "SYNA"})["meta"]["deprecated"]["use"] == "get_quote"


def test_all_transactions_defaults_to_summary_for_chat_and_full_otherwise(stub):
    arguments = {"start_date": "2026-01-01", "end_date": "2026-10-21"}
    assert called("chat", "get_all_transactions", arguments)["meta"]["detail"]["level"] == "summary"
    assert called("operator", "get_all_transactions", arguments)["meta"]["detail"]["level"] == "full"
    explicit = called("chat", "get_all_transactions", {**arguments, "detail_level": "full"})
    assert explicit["meta"]["detail"]["level"] == "full" and explicit["data"]["transactions"]


def test_public_get_quote_serves_the_merged_tool_to_chat_and_the_old_one_otherwise(stub):
    chat = called("chat", "get_quote", {"symbol": "SYNA"})
    assert [row["symbol"] for row in chat["data"]["quotes"]] == ["SYNA"]
    assert "deprecated" not in chat["meta"]
    assert len(called("chat", "get_quote", {"symbols": ["SYNA", "SYNB"]})["data"]["quotes"]) == 2
    old = called("operator", "get_quote", {"symbol": "SYNA"})
    assert old["data"] == server.get_quote("SYNA")["data"]


def _no_writes(monkeypatch):
    writes = []
    monkeypatch.setattr(NavStore, "upsert_snapshot", lambda self, *a, **k: writes.append("upsert"))
    monkeypatch.setattr(NavStore, "record_attempt", lambda self, *a, **k: writes.append("attempt"))
    return writes


def test_summary_never_writes_a_snapshot_for_any_principal(stub, monkeypatch):
    writes = _no_writes(monkeypatch)
    chat = called("chat", "get_portfolio_summary", {"snapshot": True, "include_realized": False})
    assert set(chat["data"]) == {"totals"}
    for principal in ("operator", None):
        old = called(principal, "get_portfolio_summary", {"snapshot": True, "detail_level": "full"})
        assert old["data"]["snapshot"] == {"stored": False, "reason": "snapshot disabled"}
    assert writes == []


def test_import_statement_applies_only_with_confirm_and_a_preview_id(monkeypatch):
    applied = []
    monkeypatch.setattr(server, "preview_statement_import",
                        lambda broker, file, account: server._ok({"preview_id": "p1", "rows": 2}))
    monkeypatch.setattr(server, "apply_statement_import",
                        lambda preview_id, confirm: applied.append((preview_id, confirm))
                        or server._ok({"preview_id": preview_id, "applied": confirm}))
    assert tools_v2.import_statement(confirm=True)["error"]["code"] == "PREVIEW_ID_REQUIRED"
    assert tools_v2.import_statement()["error"]["code"] == "FILE_REQUIRED"
    step1 = tools_v2.import_statement(file="s.pdf")
    assert set(step1["data"]) == {"preview", "plan"} and applied == [("p1", False)]
    tools_v2.import_statement(preview_id="p1")
    assert applied[-1] == ("p1", False)
    step2 = tools_v2.import_statement(file="s.pdf", preview_id="p1", confirm=True)
    assert applied[-1] == ("p1", True) and step2["data"]["import"]["applied"] is True


def test_service_principal_calls_are_unchanged_by_the_merge(stub):
    allowed = sorted(SERVICE_READ_TOOLS)[0]
    result = _run("service", lambda: server.mcp.call_tool("get_status", {}))
    assert result.isError and result.content[0].text == "Forbidden"
    assert allowed in listed("service")


def test_detail_level_semantics_are_documented_per_new_tool():
    tools = {tool.name: tool for tool in server.mcp._tool_manager.list_tools()}
    public = {tools_v2.PUBLIC_TO_INTERNAL.get(name, name): name for name in tools_v2.DETAIL_LEVELS}
    assert set(public) == NEW_TOOLS
    for name, chat_name in public.items():
        levels, meaning = tools_v2.DETAIL_LEVELS[chat_name]
        schema = tools[name].parameters["properties"].get("detail_level")
        assert (list(levels) if len(levels) > 1 else None) == (schema["enum"] if schema else None), name
        assert meaning and name not in response_shape.POLICIES  # shaped inside tools_v2, not by the call hook


class _EmptyToss:
    def get_prices(self, symbol):
        return []


class _FailingKis:
    def get_overseas_quote(self, symbol, exchange="NASDAQ"):
        raise RuntimeError("synthetic KIS outage")

    get_domestic_quote = get_overseas_quote


@pytest.mark.parametrize("arguments", [{"symbol": "SYNQ", "market": "us"},
                                       {"symbol": "SYNQ", "market": "us", "detail_level": "summary"}])
def test_service_principals_get_the_unshaped_2_14_1_response(stub, monkeypatch, arguments):
    """The dashboard (a service principal) gets FastMCP's own result: no chat shaping, no re-encoding."""
    from datetime import datetime, timezone

    from mcp.server.fastmcp import FastMCP

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(server, "datetime", Frozen)
    monkeypatch.setattr(server, "_get_toss", _EmptyToss)
    monkeypatch.setattr(server, "_get_kis", _FailingKis)
    monkeypatch.setattr(server, "_known_account_numbers", lambda: set())
    got = _run("dashboard", lambda: server.mcp.call_tool("compare_quotes", dict(arguments)))
    base = _run("dashboard", lambda: FastMCP.call_tool(server.mcp, "compare_quotes",
                                                  {k: v for k, v in arguments.items() if k != "detail_level"}))
    content, structured = server._result_parts(got)
    assert [block.model_dump() for block in content] == [block.model_dump() for block in server._result_parts(base)[0]]
    assert structured == server._result_parts(base)[1]
    assert [quote["data"] for quote in structured["data"]["quotes"]] == [[], None]  # parser requires `data`
    assert "detail" not in structured["meta"] and content[0].text != response_shape.compact_json(structured)


@pytest.mark.parametrize("name", ["get_toss_accounts", "get_kis_accounts"])
def test_loopback_service_service_gets_fastmcp_text_unchanged(stub, name):
    """The loopback-service service principal's text block is FastMCP's own (2.14.1), not re-encoded."""
    from mcp.server.fastmcp import FastMCP

    got = server._result_parts(_run("service", lambda: server.mcp.call_tool(name, {})))
    base = server._result_parts(_run("service", lambda: FastMCP.call_tool(server.mcp, name, {})))
    assert [block.model_dump() for block in got[0]] == [block.model_dump() for block in base[0]] and got[1] == base[1]
    assert got[0][0].text != response_shape.compact_json(got[1])


def test_account_resolution_errors_are_envelopes_not_tool_errors(stub, monkeypatch):
    """A failing account registry gives the old tools' error envelope."""
    def broken():
        raise RuntimeError("synthetic registry failure")

    monkeypatch.setattr(server, "_kis_source_labels", broken)
    env = called("chat", "get_holdings", {"account": "kis"})
    old = server.get_realized_pnl(account="kis")  # an old tool resolving the same filter
    assert env["ok"] is False and env["result_status"] == "unavailable"
    assert env["error"]["code"] == old["error"]["code"]


def test_no_description_offers_a_summary_snapshot():
    """get_portfolio_summary cannot write a snapshot over MCP, so no text offers it."""
    for tool in _run(None, server.mcp.list_tools):
        assert "snapshot=True" not in (tool.description or "") and "snapshot=true" not in (tool.description or "")
    assert "unless snapshot=true" not in server.get_portfolio_summary.__doc__


def test_service_lists_do_not_offer_the_result_layer_detail_level():
    """The service view of an injected tool is its 2.14.1 schema (no detail_level)."""
    dashboard = {tool.name: tool for tool in _run("dashboard", server.mcp.list_tools)}
    local = {tool.name: tool for tool in _run(None, server.mcp.list_tools)}
    assert "detail_level" in local["compare_quotes"].inputSchema["properties"]
    assert "detail_level" not in dashboard["compare_quotes"].inputSchema["properties"]
    for name, tool in dashboard.items():
        expected = local[name].inputSchema["properties"]
        if name in server.mcp._injected_detail:
            expected = {k: v for k, v in expected.items() if k != "detail_level"}
        assert tool.inputSchema["properties"] == expected
