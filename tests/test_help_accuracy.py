"""Help/catalog consistency and provider scope, using no upstream calls."""
import ast
import asyncio
import copy
import json
import re
from pathlib import Path

import pytest

import server


def catalog():
    return {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}


def assert_help_claims_exist(markdown, tools):
    """Every documented invocation must refer to real schema parameters/enums."""
    for snippet in re.findall(r'`([^`]+)`', markdown):
        if not re.match(r'(?:get_|scan_|analyze_|start_|cancel_|snapshot_|export_|compare_|health_check)', snippet):
            continue
        expression = ast.parse(snippet, mode='eval').body
        if isinstance(expression, ast.Name):
            assert expression.id in tools, f'Unknown tool: {expression.id}'
            continue
        assert isinstance(expression, ast.Call) and isinstance(expression.func, ast.Name)
        assert expression.func.id in tools, f'Unknown tool: {expression.func.id}'
        props = tools[expression.func.id].inputSchema['properties']
        assert len(expression.args) <= len(props)
        args = list(zip(props, expression.args)) + [(arg.arg, arg.value) for arg in expression.keywords]
        for parameter, node in args:
            assert parameter in props, f'Unknown parameter: {parameter}'
            if isinstance(node, ast.Name) and node.id == parameter:
                continue  # Required argument placeholder, generated from its name.
            value = ast.literal_eval(node)
            schema = props[parameter]
            if 'enum' in schema:
                assert value in schema['enum'], f'Invalid {parameter}: {value}'
            if 'const' in schema:
                assert value == schema['const']


def test_help_smoke_matches_real_tools_parameters_and_literal_values():
    assert_help_claims_exist(server.get_invest_mcp_help('all')['data']['markdown'], catalog())


@pytest.mark.parametrize('bad', ['get_nonexistent_tool()', 'get_candles(symbol, mystery=3)',
                                  'get_candles(symbol, interval="5m")',
                                  'get_portfolio_history(period="10Y")', 'compare_quotes(symbol, market="jp")'])
def test_help_smoke_detects_invented_tools_parameters_and_enum_values(bad):
    with pytest.raises(AssertionError):
        assert_help_claims_exist('`' + bad + '`', catalog())


def test_help_field_and_enum_claims_are_generated_from_tool_definitions():
    tools = catalog()
    help_data = server.get_invest_mcp_help('all')['data']
    for claim in help_data['tool_contracts']:
        tool = tools[claim['name']]
        assert claim['parameters'] == tool.inputSchema['properties']
        assert claim['required'] == tool.inputSchema.get('required', [])
        assert claim['output_schema'] == tool.outputSchema
    # A schema change must reach the guide without editing a second enum list.
    internal = server.mcp._tool_manager.get_tool('get_candles')
    parameters = copy.deepcopy(internal.parameters)
    try:
        internal.parameters['properties']['interval']['enum'].append('synthetic-interval')
        assert 'synthetic-interval' in server.get_invest_mcp_help('quotes')['data']['markdown']
    finally:
        internal.parameters = parameters


def test_market_trade_and_public_funding_descriptions_prevent_personal_history_substitution():
    tools = catalog()
    trades = tools['get_recent_trades'].description.lower()
    assert 'market trades' in trades and 'personal fills' in trades
    assert 'get_toss_trade_history' in trades and 'get_kis_trade_history' in trades
    funding = tools['get_binance_funding_rate'].description.lower()
    assert 'public' in funding and 'fees you paid' in funding
    assert '펀딩비 히스토리' not in server.get_invest_mcp_help('quotes')['data']['markdown']


def test_sec_description_matches_implemented_capabilities():
    description = catalog()['get_sec_financials'].description.lower()
    for capability in ('five', 'quarter', 'ttm', 'segment', 'unavailable'):
        assert capability in description


def test_health_descriptions_explain_calls_and_distinct_scope():
    tools = catalog()
    passive = tools['get_service_health'].description
    assert 'active' in passive and 'INVALID_MODE' in passive and 'health_check' in passive
    probes = tools['health_check'].description
    for scope in ('AAPL', 'BTCUSDT', 'fallback', 'account'):
        assert scope in probes
    accounts = tools['get_account_health'].description
    for call in ('holdings', 'buying power', 'domestic', 'overseas', 'pension'):
        assert call in accounts


def test_documentation_changes_preserve_frozen_raw_tool_catalog():
    baseline = json.loads((Path(__file__).parent / 'fixtures/baseline_tool_catalog.json').read_text())
    tools = catalog()
    from service_auth import SERVICE_READ_TOOLS

    assert set(baseline["raw_tools"]) == set(SERVICE_READ_TOOLS)
    for name, original in baseline['raw_tools'].items():
        current = tools[name].model_dump(mode='json', exclude_none=True)
        # 2.14.0 intentionally changes hints and appends the ledger disclosure;
        # argument/output schemas and every other description byte stay pinned.
        current.pop('annotations', None)
        current['description'] = current['description'].replace('\n    Fetched broker rows are recorded in the local append-only ledger (no broker writes).', '')
        assert current == {k: v for k, v in original.items() if k != 'annotations'}


def test_statement_import_tools_are_grouped_under_portfolio():
    from tool_help import _topic

    assert _topic('preview_statement_import') == 'portfolio'
    assert _topic('apply_statement_import') == 'portfolio'
