"""The two chat-listed local writers say they are for an explicit request only."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_merge_catalog import _run  # noqa: E402

import server  # noqa: E402
import tools_v2  # noqa: E402
from service_auth import DASHBOARD_READ_TOOLS, SERVICE_READ_TOOLS  # noqa: E402

WRITERS = ("export_portfolio_dataset", "snapshot_portfolio_nav")


def _chat_tools():
    return {tool.name: tool for tool in _run("chat", server.mcp.list_tools)}


def test_writers_say_explicit_request_only_and_point_to_the_series():
    tools = _chat_tools()
    export = " ".join(tools["export_portfolio_dataset"].description.split())
    assert "Only for an explicit request to export, save or freeze a dataset." in export
    assert "get_portfolio_returns(include_series=true), which stores nothing" in export
    snapshot = " ".join(tools["snapshot_portfolio_nav"].description.split())
    assert "Only on an explicit request to store a snapshot now" in snapshot
    assert "scheduled 18:10 KST job stores the daily NAV" in snapshot
    returns = " ".join(tools["get_portfolio_returns"].description.split())
    assert "That is the dated value series; it stores nothing, and no export or snapshot is needed" in returns


def test_writers_keep_their_annotations_schema_and_the_chat_set():
    tools = _chat_tools()
    assert [name for name, _ in tools_v2.CHAT_TOOLS] == list(tools)
    for name in WRITERS:
        hints = tools[name].annotations
        assert (hints.readOnlyHint, hints.destructiveHint, hints.idempotentHint, hints.openWorldHint) == \
            (False, False, False, True), name
    assert set(tools["export_portfolio_dataset"].inputSchema["properties"]) == {
        "dataset", "period", "start_date", "end_date", "dataset_id", "cursor", "format", "page_size", "account",
        "symbol", "input_snapshot_ids"}
    assert set(tools["snapshot_portfolio_nav"].inputSchema["properties"]) == {"snapshot_date"}


def test_service_principals_do_not_see_the_changed_descriptions():
    changed = set(WRITERS) | {"get_portfolio_returns"}
    assert not changed & (SERVICE_READ_TOOLS | DASHBOARD_READ_TOOLS)
    for principal in ("service", "dashboard"):
        for tool in _run(principal, server.mcp.list_tools):
            assert "explicit request" not in (tool.description or ""), (principal, tool.name)
