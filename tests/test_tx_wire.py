"""Tool results on the wire (MCP 2025-06-18 structured content) and detail levels.

The text block is the compact JSON of exactly the structuredContent sent; policy tools
default to a summary shaped at the result layer; detail_level="full" keeps the object.
All payloads are synthetic.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import response_shape as rs  # noqa: E402
import server  # noqa: E402


def compact(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def coverage(n):
    return {"status": "partial", "reason_codes": ["SNAPSHOT_GAP"], "observations": n,
            "snapshot_ids": [f"snap-{i}" for i in range(n)], "missing_dates": ["2026-01-02", "2026-01-05"],
            "note": "synthetic coverage block long enough to be shared between metrics"}


def metric(value):
    return {"value": value, "status": "partial", "reason_codes": ["SNAPSHOT_GAP"],
            "input_snapshot_ids": [f"snap-{i}" for i in range(30)], "input_snapshot_id": None,
            "coverage": coverage(30)}


def performance_payload():
    return {"ok": True, "data": {
        "metrics": {"simple_return_pct": metric(1.5), "volatility_annual_pct": metric(None)},
        "input_snapshot_ids": [f"snap-{i}" for i in range(30)],
        "cost_fx": {"policy": "trade_date", "rates": [{"date": f"2026-01-{d:02d}", "rate": 1400.0 + d} for d in range(1, 29)]},
        "methodology": {"basis": "synthetic methodology text"},
        "result_status": "partial"}, "error": None, "meta": {"server_version": "test"}}


def app_with(name, fn):
    app = server.ObservedFastMCP("tx-wire")
    app.add_tool(fn, name=name)
    return app


async def call(app, name, arguments):
    content, structured = await app.call_tool(name, arguments)
    assert len(content) == 1 and isinstance(content[0], TextContent)
    return content[0].text, structured


# ── wire: one compact text block of exactly the structured object ─────────────


@pytest.mark.asyncio
async def test_text_is_compact_json_of_structured_for_every_dict_tool():
    def plain() -> dict[str, Any]:
        return {"ok": True, "data": {"name": "삼성전자", "values": [1, 2.5, None]}, "meta": {}}

    text, structured = await call(app_with("plain_tool", plain), "plain_tool", {})
    assert text == compact(structured) and json.loads(text) == plain()
    assert "\n" not in text


@pytest.mark.asyncio
async def test_string_and_list_tools_keep_fastmcp_text():
    def words() -> str:
        return "configured value"

    app = app_with("words", words)
    content, structured = await app.call_tool("words", {})
    assert content[0].text == "configured value" and structured == {"result": "configured value"}


@pytest.mark.asyncio
async def test_validation_errors_are_unchanged():
    def typed(value: int = 1) -> dict[str, Any]:
        return {"ok": True, "data": value}

    with pytest.raises(ToolError):
        await app_with("typed", typed).call_tool("typed", {"value": "not-a-number"})


# ── detail levels ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_summary_moves_ids_fx_tables_and_methodology_to_full():
    def get_portfolio_performance(period: str = "3M") -> dict[str, Any]:
        return performance_payload()

    app = app_with("get_portfolio_performance", get_portfolio_performance)
    text, summary = await call(app, "get_portfolio_performance", {})
    assert text == compact(summary)
    data = summary["data"]
    assert "input_snapshot_ids" not in data and "methodology" not in data and "rates" not in data["cost_fx"]
    for metric in data["metrics"].values():
        # A metric keeps value/status/reason_codes in place; its coverage is shared once.
        assert {"value", "status", "reason_codes"} <= set(metric)
        assert metric["coverage"] == {"$ref": "#/shared/coverage"}
        assert "input_snapshot_ids" not in metric and "input_snapshot_id" not in metric
    assert data["metrics"]["volatility_annual_pct"]["value"] is None  # a null value is kept with its reasons
    assert "snapshot_ids" not in summary["shared"]["coverage"]
    detail = summary["meta"]["detail"]
    assert detail["level"] == "summary"
    assert detail["omitted"] == {"**.input_snapshot_ids": 90, "**.snapshot_ids": 60,
                                 "**.cost_fx.rates": 28, "data.methodology": 1}
    assert "data.metrics.*.input_snapshot_id" in detail["omitted_empty"]
    assert len(text.encode()) < len(compact(performance_payload()).encode()) / 2


@pytest.mark.asyncio
async def test_full_returns_the_complete_object():
    def get_portfolio_performance(period: str = "3M", detail_level: str = "summary") -> dict[str, Any]:
        return performance_payload()

    app = app_with("get_portfolio_performance", get_portfolio_performance)
    text, full = await call(app, "get_portfolio_performance", {"detail_level": "full"})
    expected = performance_payload()
    expected["meta"]["detail"] = {"level": "full"}
    assert full == expected and text == compact(full)


@pytest.mark.asyncio
async def test_existing_detail_level_parameter_and_default_are_kept():
    seen = []

    def get_all_transactions(detail_level: str = "full") -> dict[str, Any]:
        seen.append(detail_level)
        return {"ok": True, "data": {"transactions": [], "source_metadata": {"toss:primary:orders": {
            "fetched_windows": [{"start": "2026-01-01", "end": "2026-03-31"}]}}}, "meta": {}}

    app = app_with("get_all_transactions", get_all_transactions)
    _, default = await call(app, "get_all_transactions", {})
    _, summary = await call(app, "get_all_transactions", {"detail_level": "summary"})
    assert seen == ["full", "summary"]
    assert default["meta"]["detail"] == {"level": "full"}
    assert "fetched_windows" in default["data"]["source_metadata"]["toss:primary:orders"]
    assert summary["meta"]["detail"]["omitted"] == {"**.fetched_windows": 1}


@pytest.mark.asyncio
async def test_injected_detail_level_is_advertised_and_never_reaches_the_tool():
    seen = []

    def get_realized_pnl(symbol: str = "") -> dict[str, Any]:
        seen.append(symbol)
        return {"ok": True, "data": {"symbols": [{"symbol": "AAA", "currency": "USD", "realized_pnl": 1.0,
                                                  "transaction_ids": ["t1", "t2"]}]}, "meta": {}}

    app = app_with("get_realized_pnl", get_realized_pnl)
    schema = (await app.list_tools())[0].inputSchema
    assert schema["properties"]["detail_level"]["default"] == "summary"
    assert schema["properties"]["detail_level"]["enum"] == ["summary", "full"]
    assert "detail_level" not in schema.get("required", [])
    _, summary = await call(app, "get_realized_pnl", {"symbol": "AAA"})
    _, full = await call(app, "get_realized_pnl", {"symbol": "AAA", "detail_level": "full"})
    assert seen == ["AAA", "AAA"]
    assert summary["data"]["symbols"][0] == {"symbol": "AAA", "currency": "USD", "realized_pnl": 1.0}
    assert full["data"]["symbols"][0]["transaction_ids"] == ["t1", "t2"]
    with pytest.raises(ToolError):
        await app.call_tool("get_realized_pnl", {"detail_level": "everything"})


@pytest.mark.asyncio
async def test_live_catalog_differs_from_2_14_0_only_by_the_injected_detail_level():
    catalog = [t.model_dump(mode="json") for t in await server.mcp.list_tools()]
    injected = sorted(t["name"] for t in catalog
                      if t["inputSchema"]["properties"].get("detail_level", {}).get("description") == rs.DETAIL_DESCRIPTION)
    assert injected == ["compare_quotes", "get_invest_mcp_help", "get_portfolio_summary", "get_realized_pnl"]
    for tool in catalog:
        if tool["name"] in injected:
            tool["inputSchema"]["properties"].pop("detail_level")
    # tools_v2 adds the merged tools and re-registers get_portfolio_summary without its
    # snapshot argument (T0); every other tool stays byte-identical to 2.15.0.
    merged = {"get_status", "get_holdings", "get_portfolio_summary_v2", "get_portfolio_returns", "get_transactions",
              "get_trading_pnl", "get_quote_v2", "get_market_info", "get_crypto_market", "get_crypto_positions",
              "get_crypto_account_history", "import_statement", "get_portfolio_summary"}
    catalog = [tool for tool in catalog if tool["name"] not in merged]
    encoded = json.dumps(catalog, sort_keys=True, ensure_ascii=False).encode()
    # The hash test_tool_offloop pins for 2.14.0 plus the 2.15 description lines (performance/history
    # ledgerless basis, get_trade_review win rates and summary; was 1a2c5c07...).
    # get_portfolio_history no longer names get_portfolio_summary(snapshot=True) (was 18a0c578...).
    # get_realized_pnl's reconcile argument names the drift rule (was 21237a47...).
    # export_portfolio_dataset and snapshot_portfolio_nav are for an explicit request
    # only (ChatGPT I06; was b9ace274...).
    assert hashlib.sha256(encoded).hexdigest() == "150acf8c84b6c67e0ab14c088250085ebc305e9d44a1fd0e69e178ce85b7f768"


@pytest.mark.asyncio
async def test_dashboard_masking_keeps_the_compact_text():
    structured = {"ok": True, "data": {"CANO": "82345678", "note": "from 82345678-01"}, "meta": {}}
    result = ([TextContent(type="text", text=compact(structured))], structured)
    content, masked = server._masked_tool_result(result, set())
    assert content[0].text == compact(masked) and masked["data"]["CANO"] == "••••5678"
    wrapped = server._masked_tool_result(CallToolResult(content=result[0], structuredContent=structured), set())
    assert wrapped.content[0].text == compact(wrapped.structuredContent)


# ── response_shape passes ─────────────────────────────────────────────────────


def test_shape_never_mutates_the_tool_object():
    env = performance_payload()
    before = copy.deepcopy(env)
    rs.shape(env, rs.POLICIES["get_portfolio_performance"], rs.SUMMARY)
    assert env == before


def test_empty_keys_are_dropped_but_metric_fields_and_reasons_are_kept():
    env = {"ok": True, "data": {"rows": [
        {"symbol": "AAA", "value": None, "status": None, "reason_codes": [], "fx_reason": None, "lookthrough": None},
        {"symbol": "BBB", "value": None, "status": None, "reason_codes": [], "fx_reason": None, "lookthrough": {}},
    ]}, "meta": {}}
    out = rs.shape(env, rs.Policy(), rs.SUMMARY)
    assert out["data"]["rows"][0] == {"symbol": "AAA", "value": None, "status": None, "reason_codes": [], "fx_reason": None}
    assert out["meta"]["detail"]["omitted_empty"] == ["data.rows[].lookthrough"]


def test_only_metadata_blocks_are_shared():
    realized = {"realized_pnl_by_currency": {"USD": 12.5}, "sell_count": 2, "status": "ok", "reason_codes": [],
                "diagnostic_pnl_by_currency": {"USD": 12.5}, "padding": "x" * 120}
    calendar = {"timezone": "America/New_York", "session": "regular", "holidays": ["2026-01-19"], "padding": "y" * 120}
    env = {"ok": True, "data": {"symbols": [{"symbol": s, "realized": dict(realized), "market_calendar": dict(calendar)}
                                            for s in ("AAA", "BBB")]}, "meta": {}}
    out = rs.shape(env, rs.Policy(), rs.SUMMARY)
    for row in out["data"]["symbols"]:
        assert row["realized"] == realized
        assert row["market_calendar"] == {"$ref": "#/shared/market_calendar"}
    assert out["shared"] == {"market_calendar": calendar}


def test_zero_balance_crypto_assets_move_to_full_with_their_names():
    zero = {"value": 0.0, "status": "ok", "reason_codes": [], "decimal_value": "0"}
    env = {"ok": True, "data": {"crypto_nav": {"binance": {
        "by_currency": {"USDT": {"value": 10.0, "status": "ok", "reason_codes": [], "decimal_value": "10"},
                        "BNB": dict(zero), "ETH": {**zero, "reason_codes": ["PRICE_MISSING"]}},
        "accounts": [{"asset": "USDT"}, {"asset": "BNB"}, {"asset": "ETH"}]}}}, "meta": {}}
    out = rs.shape(env, rs.POLICIES["get_portfolio_summary"], rs.SUMMARY)
    binance = out["data"]["crypto_nav"]["binance"]
    assert set(binance["by_currency"]) == {"USDT", "ETH"}  # a zero with a reason code stays
    assert binance["accounts"] == [{"asset": "USDT"}, {"asset": "ETH"}]
    assert out["meta"]["detail"]["omitted"]["data.crypto_nav.binance zero-balance assets: BNB"] == 1


def test_reconciled_rows_move_to_full_and_their_reason_codes_are_tallied():
    rows = [{"symbol": f"S{i}", "status": "matched_observed", "reason_codes": ["MISSING_OPENING_POSITION"]}
            for i in range(3)]
    rows.append({"symbol": "BRK", "status": "unexplained", "reason_codes": ["RECONCILIATION_BREAK"]})
    env = {"ok": True, "data": {"reconciliation": {"positions": rows}}, "meta": {}}
    out = rs.shape(env, rs.POLICIES["get_trade_review"], rs.SUMMARY)
    assert out["data"]["reconciliation"]["positions"] == [rows[-1]]
    detail = out["meta"]["detail"]
    assert detail["omitted"]["data.reconciliation.positions[] status=matched_observed"] == 3
    assert detail["omitted_reason_codes"] == {
        "data.reconciliation.positions[] status=matched_observed": {"MISSING_OPENING_POSITION": 3}}


def test_stale_quotes_are_grouped_with_every_position_and_reason_code():
    def quote(account, symbol, status, codes, currency="USD", kind="holding"):
        return {"account": account, "symbol": symbol, "component_type": kind, "currency": currency, "status": status,
                "reason_codes": codes, "priced_at": None, "observed_at": "2026-10-21T15:00:00+00:00", "age_seconds": None}
    quotes = [quote("toss:primary", "AAA", "current", []), quote("toss:primary", "BBB", "unknown", ["UNKNOWN_QUOTE_TIME"]),
              quote("kis:overseas", "BBB", "unknown", ["UNKNOWN_QUOTE_TIME"]),
              quote("kis:domestic", None, "unknown", ["UNKNOWN_QUOTE_TIME"], "KRW", "cash")]
    env = {"ok": True, "data": {"mark_quality": {"max_mark_age_seconds": 5, "quotes": quotes}}, "meta": {}}
    out = rs.shape(env, rs.POLICIES["get_portfolio_summary"], rs.SUMMARY)
    quality = out["data"]["mark_quality"]
    assert "quotes" not in quality and quality["max_mark_age_seconds"] == 5
    assert quality["quote_groups"] == [
        {"status": "unknown", "reason_codes": ["UNKNOWN_QUOTE_TIME"], "component_type": "holding", "currency": "USD",
         "count": 2, "positions": [["toss:primary", "BBB"], ["kis:overseas", "BBB"]]},
        {"status": "unknown", "reason_codes": ["UNKNOWN_QUOTE_TIME"], "component_type": "cash", "currency": "KRW",
         "count": 1, "positions": [["kis:domestic", None]]}]
    omitted = out["meta"]["detail"]["omitted"]
    assert omitted["data.mark_quality.quotes[] status=current"] == 1
    assert omitted["data.mark_quality.quotes[] grouped into data.mark_quality.quote_groups"] == 3
    full = rs.shape(env, rs.POLICIES["get_portfolio_summary"], rs.FULL)
    assert full["data"]["mark_quality"]["quotes"] == quotes


def test_identical_unsettled_blocks_are_shared():
    unsettled = {"KRW": {"receivables": {"value": None, "status": "missing", "reason_codes": ["MISSING_UNSETTLED_RECEIVABLES"]},
                         "payables": {"value": None, "status": "missing", "reason_codes": ["MISSING_UNSETTLED_PAYABLES"]}}}
    env = {"ok": True, "data": {"account_nav": {a: {"unsettled": copy.deepcopy(unsettled)} for a in ("toss:primary", "toss:secondary")}},
           "meta": {}}
    out = rs.shape(env, rs.POLICIES["get_portfolio_summary"], rs.SUMMARY)
    assert out["data"]["account_nav"]["toss:secondary"]["unsettled"] == {"$ref": "#/shared/unsettled"}
    assert out["shared"] == {"unsettled": unsettled}


def test_unknown_tools_and_non_dict_results_pass_through():
    env = {"ok": True, "data": {"fetched_windows": [1, 2]}}
    assert rs.shape(env, None, rs.SUMMARY) is env
    assert rs.shape([1, 2], rs.Policy(), rs.SUMMARY) == [1, 2]
