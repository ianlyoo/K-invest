"""Hyperliquid positions carry a labelled derived mark (synthetic numbers, mocked HTTP)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import crypto_futures_accounts as futures  # noqa: E402

ADDRESS = '0x' + 'b' * 36 + '5678'


def _hyperliquid(positions):
    def handler(request):
        body = json.loads(request.content)
        if body['type'] == 'perpDexs':
            return httpx.Response(200, json=[None])
        if body['type'] == 'userAbstraction':
            return httpx.Response(200, json='default')
        if body['type'] == 'spotClearinghouseState':
            return httpx.Response(200, json={'balances': []})
        return httpx.Response(200, json={'assetPositions': [{'position': p} for p in positions],
                                         'marginSummary': {'accountValue': '500', 'totalMarginUsed': '40'},
                                         'withdrawable': '460'})
    return futures.HyperliquidAccountClient([ADDRESS], http=httpx.Client(transport=httpx.MockTransport(handler)),
                                            sleeper=lambda _: None)


def _positions(positions):
    result = futures.FuturesAccounts(None, lambda: _hyperliquid(positions)).query('positions', 'hyperliquid')
    return {p['symbol']: p for p in result['venues']['hyperliquid']['positions']}


def test_missing_venue_mark_is_derived_from_position_value_and_labelled():
    rows = _positions([
        {'coin': 'SYNL', 'szi': '10', 'entryPx': '70', 'positionValue': '776.15', 'unrealizedPnl': '76.15',
         'liquidationPx': '63.4', 'leverage': {'type': 'isolated', 'value': 10}},
        {'coin': 'SYNS', 'szi': '-4', 'entryPx': '30', 'positionValue': '121', 'unrealizedPnl': '-1',
         'leverage': {'type': 'cross', 'value': 3}},
    ])
    long_row, short_row = rows['SYNL'], rows['SYNS']
    assert long_row['mark_price'] is None  # the venue field stays the venue's own
    assert long_row['field_status']['mark_price']['reason_codes'] == ['FIELD_NOT_RETURNED']
    assert long_row['mark_price_derived'] == {'value': '77.615', 'status': 'derived', 'source': 'positionValue/szi',
                                              'formula': 'abs(positionValue) / abs(szi)', 'reason_codes': []}
    assert short_row['mark_price_derived']['value'] == '30.25'


def test_reported_mark_or_missing_inputs_never_produce_a_derived_mark():
    rows = _positions([
        {'coin': 'SYNR', 'szi': '2', 'markPx': '41', 'positionValue': '82', 'leverage': {'type': 'cross', 'value': 2}},
        {'coin': 'SYNN', 'szi': '2', 'leverage': {'type': 'cross', 'value': 2}},
    ])
    assert rows['SYNR']['mark_price'] == '41'
    assert rows['SYNR']['mark_price_derived'] == {'value': None, 'status': 'unavailable', 'source': None,
                                                  'formula': None, 'reason_codes': ['MARK_REPORTED']}
    assert rows['SYNN']['mark_price_derived']['value'] is None
    assert rows['SYNN']['mark_price_derived']['reason_codes'] == ['MARK_DERIVATION_INPUT_UNAVAILABLE']
