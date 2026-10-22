"""Strict response mutation matrix on collection -> frozen NAV -> scoped NAV.

Real Toss read accessors run against synthetic _get responses, with no client
construction, credentials, network or live database. Each case includes a valid
control and fails on 1a69c20 by an account/quality behavior assertion.
"""
import json
from copy import deepcopy

import pytest
from test_toss_nav import capture, documented_order, selected

from ledger_returns import scope_navs
from portfolio_engine import summarize_portfolio

INPUTS = ('holdings', 'prices', 'buying_power:KRW', 'buying_power:USD', 'orders', 'calendar', 'exchange_rate')
COMMON = ('root-partial', 'root-truncated', 'root-cursor', 'root-error', 'root-errors',
          'nested-partial', 'nested-error', 'nested-cursor', 'missing-result', 'null-result', 'wrong-result-type',
          'wrong-field-type', 'missing-identity', 'null-identity', 'result-partial', 'result-error', 'result-cursor')
CASES = [(source, mutation, 'INVALID_SOURCE_RESPONSE') for source in INPUTS for mutation in COMMON]
CASES += [(source, 'duplicate', 'DUPLICATE_HOLDING' if source == 'holdings' else 'DUPLICATE_QUOTE'
           if source == 'prices' else 'INVALID_SOURCE_RESPONSE') for source in ('holdings', 'prices', 'orders', 'calendar')]
CASES += [
    ('holdings', 'duplicate-other-currency', 'DUPLICATE_HOLDING'),
    ('holdings', 'duplicate-invalid-first', 'DUPLICATE_HOLDING'),
    ('holdings', 'missing-inventory', 'INVALID_SOURCE_RESPONSE'),
    ('holdings', 'null-inventory', 'INVALID_SOURCE_RESPONSE'),
    ('holdings', 'wrong-inventory-type', 'INVALID_SOURCE_RESPONSE'),
    ('holdings', 'missing-currency', 'INVALID_SOURCE_RESPONSE'),
    ('holdings', 'numeric-positive-quantity', 'INVALID_SOURCE_RESPONSE'),
    ('prices', 'duplicate-wrong-currency-first', 'DUPLICATE_QUOTE'),
    ('prices', 'duplicate-wrong-currency-last', 'DUPLICATE_QUOTE'),
    ('prices', 'duplicate-invalid-first', 'DUPLICATE_QUOTE'),
    ('prices', 'duplicate-invalid-last', 'DUPLICATE_QUOTE'),
    ('prices', 'duplicate-third-valid', 'DUPLICATE_QUOTE'),
    ('prices', 'unrequested-row', 'CURRENCY_BINDING_MISMATCH'),
    ('prices', 'wrong-currency', 'CURRENCY_BINDING_MISMATCH'),
    ('prices', 'missing-currency', 'INVALID_SOURCE_RESPONSE'),
    ('buying_power:KRW', 'wrong-currency', 'CURRENCY_BINDING_MISMATCH'),
    ('buying_power:USD', 'wrong-currency', 'CURRENCY_BINDING_MISMATCH'),
    ('buying_power:KRW', 'swapped-currencies', 'CURRENCY_BINDING_MISMATCH'),
    ('calendar', 'wrong-currency', 'CURRENCY_BINDING_MISMATCH'),
    ('exchange_rate', 'wrong-currency', 'CURRENCY_BINDING_MISMATCH'),
    ('exchange_rate', 'wrong-quote-currency', 'CURRENCY_BINDING_MISMATCH'),
    ('exchange_rate', 'missing-expiry', 'INVALID_SOURCE_RESPONSE'),
]
CASES += [(source, 'nondecimal-number', 'INVALID_SOURCE_RESPONSE') for source in
          ('holdings', 'prices', 'buying_power:KRW', 'buying_power:USD', 'exchange_rate')]
CASES += [(source, 'nondatetime-time', 'INVALID_SOURCE_RESPONSE') for source in ('prices', 'orders', 'calendar')]


def mutate_response(payload, source, mutation):
    if mutation.startswith('root-'):
        marker = {'root-partial': {'partial': True}, 'root-truncated': {'truncated': True},
                  'root-cursor': {'nextCursor': 'synthetic-next'}, 'root-error': {'error': {'code': 'SYNTHETIC_ERROR'}},
                  'root-errors': {'errors': ['SYNTHETIC_ERROR']}}[mutation]
        return {**payload, **marker}
    if mutation.startswith('nested-'):
        # Every nested object is checked, even metadata in unknown extension
        # fields that schema projection or row filtering would otherwise lose.
        marker = {'nested-partial': {'partial': True}, 'nested-error': {'error_code': 'SYNTHETIC_ERROR'},
                  'nested-cursor': {'next_cursor': 'synthetic-next'}}[mutation]
        payload['extension'] = {'levels': [{'metadata': marker}]}
        return payload
    if mutation == 'missing-result':
        payload.pop('result')
        return payload
    if mutation in ('null-result', 'wrong-result-type'):
        payload['result'] = None if mutation == 'null-result' else True
        return payload
    result = payload['result']
    if mutation.startswith('result-'):
        target = result[0] if isinstance(result, list) else result
        marker = {'result-partial': {'partial': True}, 'result-error': {'errors': ['SYNTHETIC_ERROR']},
                  'result-cursor': {'nextCursor': 'synthetic-next'}}[mutation]
        target.update(marker)
        return payload
    if source == 'holdings':
        if mutation in ('missing-inventory', 'null-inventory', 'wrong-inventory-type'):
            if mutation == 'missing-inventory':
                result.pop('items')
            else:
                result['items'] = None if mutation == 'null-inventory' else {}
            return payload
        row, identity = result['items'][0], 'symbol'
    elif source == 'prices':
        row, identity = result[0], 'symbol'
    elif source.startswith('buying_power'):
        row, identity = result, 'currency'
    elif source == 'orders':
        result['orders'] = [documented_order()]
        row, identity = result['orders'][0], 'orderId'
    elif source == 'calendar':
        row, identity = result['today'], 'date'
    else:
        row, identity = result, 'baseCurrency'
    if mutation == 'missing-identity':
        row.pop(identity)
    elif mutation == 'null-identity':
        row[identity] = None
    elif mutation == 'missing-currency':
        row.pop('currency')
    elif mutation == 'numeric-positive-quantity':
        row['quantity'] = 2
    elif mutation == 'wrong-field-type':
        field = {'holdings': 'lastPrice', 'prices': 'lastPrice', 'orders': 'orderedAt',
                 'calendar': 'date', 'exchange_rate': 'rate'}.get(source, 'cashBuyingPower')
        row[field] = True
    elif mutation == 'nondecimal-number':
        field = {'holdings': 'lastPrice', 'prices': 'lastPrice', 'exchange_rate': 'rate'}.get(source, 'cashBuyingPower')
        row[field] = '1_00'
    elif mutation == 'nondatetime-time':
        if source == 'calendar':
            session = result['previousBusinessDay']['regularMarket']
            session['startTime'] = session['startTime'].replace('T', ' ')
        else:
            field = 'timestamp' if source == 'prices' else 'orderedAt'
            row[field] = row[field].replace('T', ' ')
    elif mutation in ('wrong-currency', 'swapped-currencies'):
        if source == 'calendar':
            result['country'] = 'KR'
        else:
            key = 'baseCurrency' if source == 'exchange_rate' else 'currency'
            row[key] = 'KRW' if row[key] == 'USD' else 'USD'
    elif mutation == 'wrong-quote-currency':
        row['quoteCurrency'] = 'USD'
    elif mutation == 'missing-expiry':
        row.pop('validUntil')
    elif mutation == 'unrequested-row':
        result.append({**row, 'symbol': 'UNREQUESTED'})
    elif mutation.startswith('duplicate'):
        if source == 'calendar':
            result['previousBusinessDay'] = deepcopy(result['today'])
            return payload
        rows = result if source == 'prices' else result['items'] if source == 'holdings' else result['orders']
        bad = deepcopy(row)
        if mutation in ('duplicate-other-currency', 'duplicate-wrong-currency-first', 'duplicate-wrong-currency-last'):
            bad['currency'] = 'KRW'
        if 'invalid' in mutation:
            bad['lastPrice'] = None
        rows[:] = [row, bad] if mutation.endswith('last') else [bad, row]
        if mutation == 'duplicate-third-valid':
            rows.append(deepcopy(row))
    else:
        raise AssertionError('Unimplemented synthetic mutation')
    return payload


@pytest.mark.parametrize('source,mutation,reason', CASES, ids=[f'{s}/{m}' for s, m, _ in CASES])
def test_pass6_every_toss_response_requires_complete_bound_unique_typed_evidence(tmp_path, monkeypatch,
                                                                              source, mutation, reason):
    good = capture(tmp_path, monkeypatch, real_response_views=True)
    assert selected(good.row)['total_value_krw'] == 3500
    assert selected(good.row)['reason_codes'] == []
    assert selected(good.row, 'toss')['total_value_krw'] == 6500

    def mutate(kind, payload):
        applies = kind == source or mutation == 'swapped-currencies' and kind.startswith('buying_power:')
        return mutate_response(payload, kind, mutation) if applies else payload
    bad = capture(tmp_path, monkeypatch, nav_mutation=mutate, real_response_views=True)
    account = bad.row['account_nav']['toss:primary']
    assert all(value['value'] is None for value in account['nav_native'].values())
    assert all(reason in value['reason_codes'] for value in account['nav_native'].values())
    assert account['source_status'] == 'partial'
    if source == 'holdings' and mutation.startswith('duplicate'):
        holdings = [component for component in bad.row['components']
                    if component['account'] == 'toss:primary' and component['component_type'] == 'holding']
        assert holdings and all(component['native_market_value']['value'] is None
                                and 'DUPLICATE_HOLDING' in component['reason_codes'] for component in holdings)
    for scope in ('toss_investmcp', 'toss'):
        nav = selected(bad.row, scope)
        assert nav['total_value_krw'] is None and nav['result_status'] == 'partial'
        assert reason in nav['reason_codes']
    assert bad.legacy == summarize_portfolio(bad.collection.holdings, bad.collection.cash, bad.fx)
    assert bad.row['total_value_krw'] == bad.legacy['total_assets_krw']
    if not bad.collection.failed_sources and not bad.collection.truncated_sources:
        assert bad.row['nav_bridge'] == bad.legacy['nav_bridge']
    assert scope_navs([bad.row], 'equity')[0] == bad.row
    if source not in ('calendar', 'exchange_rate'):
        other = scope_navs([bad.row], 'toss_investmcp', account_references={'toss:secondary'})[0]
        assert other['total_value_krw'] == 3000 and other['reason_codes'] == []
    else:
        other = scope_navs([bad.row], 'toss_investmcp', account_references={'toss:secondary'})[0]
        assert other['total_value_krw'] is None and reason in other['reason_codes']
    # The real exchange-rate accessor still serializes exactly its result view;
    # internal envelope evidence adds no client-visible fields.
    response = bad.clients['primary'].get_exchange_rate(base='USD', quote='KRW')
    if isinstance(response, dict):
        assert json.loads(json.dumps(response)) == dict(response)
