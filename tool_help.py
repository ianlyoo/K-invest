"""Usage help derived from the registered MCP tool definitions."""
from __future__ import annotations

import copy
from typing import Any

FINANCIAL = {'get_sec_financials', 'get_ttm_financials', 'get_insider_trades',
             'get_financials', 'get_key_metrics', 'get_risk_free_rate',
             'get_stock_info', 'get_stock_snapshot'}
DIAGNOSTICS = {'get_account_health', 'get_service_health', 'health_check', 'get_invest_mcp_help'}
DEPRECATED_ALIASES = {'get_recent_trades': 'get_market_trades',
                      'get_binance_funding_rate': 'get_binance_funding_rate_history'}
#: Merged chat catalog: topics of the new names the keyword rules below would misfile.
MERGED_TOPICS = {'get_status': 'diagnostics', 'get_trading_pnl': 'portfolio', 'import_statement': 'portfolio',
                 'get_crypto_market': 'futures', 'get_crypto_positions': 'futures_account',
                 'get_crypto_account_history': 'futures_account', 'get_market_info': 'quotes'}
PURPOSES = {'portfolio': 'Portfolio', 'quotes': 'Quotes / market data',
            'financials': 'Company research (financials)', 'futures': 'Crypto futures (public)',
            'futures_account': 'Crypto futures (your account)', 'diagnostics': 'Diagnostics'}
TOPICS = ('overview', 'quotes', 'portfolio', 'financials', 'futures', 'futures_account', 'diagnostics', 'examples')
INTRO = '''# K-invest usage guide

Read-only investment data; no order execution tools. Market trades and public
funding rates are distinct from personal fills and paid fees. Use the personal
history tools for your own transactions. All responses use ok/data/error/meta;
provider fields may be absent or unavailable. Inspect each tool's warnings and
quality metadata. Tool parameters below come from the registered schemas.

Every result is sent once as structuredContent and once as the same object in
compact JSON text. Large portfolio tools default to detail_level="summary": ID
lists, per-day FX tables, fetched windows and static methodology move to "full",
and meta.detail lists what was omitted. A metadata block repeated in one result
appears once under `shared`; each use reads {"$ref": "#/shared/<name>"}.
'''


def _topic(name: str) -> str:
    name = DEPRECATED_ALIASES.get(name, name)
    if name in MERGED_TOPICS:
        return MERGED_TOPICS[name]
    if name in {'get_crypto_futures_positions', 'get_crypto_futures_fills', 'get_crypto_futures_funding_payments'}:
        return 'futures_account'
    if name.startswith('get_binance_') or name == 'get_crypto_futures_snapshot':
        return 'futures'
    if name in DIAGNOSTICS:
        return 'diagnostics'
    if name in FINANCIAL:
        return 'financials'
    if any(word in name for word in ('portfolio', 'transactions', 'realized_pnl', 'holdings',
                                     'balance', 'buying_power', 'trade_history', 'trade_review', 'statement_import', 'accounts')):
        return 'portfolio'
    return 'quotes'


def build_help(tools, topic: str = 'overview') -> dict[str, Any]:
    """Schemas, invocation examples and field claims share the catalog's source."""
    topic = (topic or 'overview').strip().lower()
    if topic not in TOPICS and topic != 'all':
        topic = 'overview'
    selected = [tool for tool in tools if topic in ('overview', 'all', 'examples') or _topic(tool.name) == topic]
    blocks, contracts, aliases = [INTRO.strip()], [], []
    current_purpose = None
    for tool in sorted(selected, key=lambda item: (list(PURPOSES).index(_topic(item.name)), item.name)):
        schema = tool.parameters
        properties = schema.get('properties', {})
        required = schema.get('required', [])
        contracts.append({'name': tool.name, 'parameters': copy.deepcopy(properties),
                          'required': list(required), 'output_schema': copy.deepcopy(tool.fn_metadata.output_schema)})
        if tool.name in DEPRECATED_ALIASES:
            aliases.append(f'- `{tool.name}` → `{DEPRECATED_ALIASES[tool.name]}`: ' + tool.description.split('\n\n')[0])
            continue
        purpose = _topic(tool.name)
        if purpose != current_purpose:
            blocks.append('# ' + PURPOSES[purpose])
            current_purpose = purpose
        arguments = [name for name in properties if name in required]
        arguments += [f'{name}={value["default"]!r}' for name, value in properties.items()
                      if name not in required and 'default' in value]
        invocation = f'{tool.name}({", ".join(arguments)})'
        blocks.append(f'## `{invocation}`\n\n' + tool.description.split('\n\n')[0])
        for name, prop in properties.items():
            constraints = prop.get('enum', [prop['const']] if 'const' in prop else None)
            if constraints is not None:
                blocks.append(f'- {name}: ' + ', '.join(f'`{value!r}`' for value in constraints))
    if aliases:
        blocks.append('## Deprecated aliases\n\nThese names share the implementations above; responses add meta.deprecated.\n\n' + '\n'.join(aliases))
    return {'markdown': '\n\n'.join(blocks), 'tool_contracts': contracts}
